"""Fail-closed Tushare data lane for the Alpha Feasibility experiment.

This module is deliberately independent from the formal small-account backtest
and from the Tushare SDK.  It only speaks the seven standard, read-only
endpoints frozen in ``a_share_technical_alpha_feasibility.v1.json``.  The
collector has three important properties:

* configuration and the complete request plan are checked before a credential
  is inspected or a network transport is constructed;
* every remote attempt is claimed by a create-only ``*.started.json`` artifact
  and can therefore never be silently retried after an ambiguous crash;
* untrusted response bytes are bounded and validated before they can become a
  consumer artifact.  A response containing post-2023 data is quarantined by
  hash only and its body is not persisted.

The normalized representation is plain Python dictionaries and lists.  No
DataFrame, SDK object, broker object, account state, or order capability enters
this boundary.
"""

from __future__ import annotations

import calendar
import hashlib
import http.client
import json
import math
import os
import re
import ssl
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from research.market_data.validation import SchemaValidationError, validate_json_schema


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = (
    REPOSITORY_ROOT / "configs" / "a_share_technical_alpha_feasibility.v1.json"
)
OFFICIAL_API_HOST = "api.tushare.pro"
OFFICIAL_API_PATH = "/"
OFFICIAL_API_URL = "https://api.tushare.pro"
ABSOLUTE_CUTOFF = date(2023, 12, 31)
PLAN_SCHEMA_VERSION = "tushare-alpha-feasibility-plan.v1"
TASK_SCHEMA_VERSION = "tushare-alpha-feasibility-task.v1"
STARTED_SCHEMA_VERSION = "tushare-alpha-feasibility-task-started.v1"
RESPONSE_SCHEMA_VERSION = "tushare-alpha-feasibility-task-response.v1"
QUARANTINE_SCHEMA_VERSION = "tushare-alpha-feasibility-quarantine.v1"
PIT_REPORT_SCHEMA_VERSION = "pit-membership-coverage-report.v1"
PIT_MANIFEST_SCHEMA_VERSION = "pit-membership-manifest.v1"
HISTORY_MANIFEST_SCHEMA_VERSION = "tushare-alpha-feasibility-manifest.v1"

ALLOWED_ENDPOINTS = (
    "trade_cal",
    "index_weight",
    "daily",
    "adj_factor",
    "index_daily",
    "suspend_d",
    "stock_basic",
)
LOCKED_TEST_STATUS = MappingProxyType(
    {"access": "NOT_ACCESSED", "download": "NOT_DOWNLOADED", "run": "NOT_RUN"}
)

EXPECTED_FIELDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "trade_cal": ("exchange", "cal_date", "is_open", "pretrade_date"),
        "index_weight": ("index_code", "con_code", "trade_date", "weight"),
        "stock_basic": (
            "ts_code",
            "symbol",
            "name",
            "exchange",
            "list_status",
            "list_date",
            "delist_date",
        ),
        "daily": (
            "ts_code",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "pre_close",
            "vol",
            "amount",
        ),
        "adj_factor": ("ts_code", "trade_date", "adj_factor"),
        "suspend_d": (
            "ts_code",
            "trade_date",
            "suspend_timing",
            "suspend_type",
        ),
        "index_daily": (
            "ts_code",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "pre_close",
            "change",
            "pct_chg",
            "vol",
            "amount",
        ),
    }
)

# Equality is treated as a potential upstream page truncation.  These limits
# are deliberately conservative and are not used to split or retry a request.
POTENTIAL_TRUNCATION_LIMIT: Mapping[str, int] = MappingProxyType(
    {
        "trade_cal": 10_000,
        "index_weight": 10_000,
        "stock_basic": 6_000,
        "daily": 6_000,
        "adj_factor": 6_000,
        "suspend_d": 5_000,
        "index_daily": 6_000,
    }
)
MINIMUM_OPEN_SESSIONS_BY_YEAR: Mapping[int, int] = MappingProxyType(
    {2017: 100, 2018: 200, 2019: 200, 2020: 200, 2021: 200, 2022: 200, 2023: 200}
)
MAXIMUM_OPEN_SESSIONS_PER_YEAR = 260

_DATE8 = re.compile(r"^\d{8}$")
_DATE10 = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH = re.compile(r"^\d{4}-\d{2}$")
_TS_CODE = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")
_PIT_COMPONENT_CODE = re.compile(r"^\d{6}\.(?:SH|SZ)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_TASK_ID = re.compile(r"^[a-z_]+-[0-9a-f]{64}$")
_FORBIDDEN_PARAM_KEY = re.compile(
    r"(?:token|secret|password|cookie|credential|account|order)", re.IGNORECASE
)
_EMBEDDED_DATE = re.compile(
    r"(?<!\d)(20\d{2})[-/]?(0[1-9]|1[0-2])[-/]?(0[1-9]|[12]\d|3[01])(?!\d)"
)


class AlphaFeasibilityDataError(RuntimeError):
    """A sanitized, stable fail-closed data status.

    ``code`` never contains provider messages, response values, file contents,
    or credential-derived material, making it safe to persist in quarantine
    evidence and terminal summaries.
    """

    def __init__(self, code: str, *, stage: str = "data") -> None:
        if not re.fullmatch(r"[a-z0-9_]{3,96}", str(code)):
            code = "unsafe_error_sanitized"
        self.code = str(code)
        self.stage = str(stage)
        super().__init__(self.code)


class AmbiguousRemoteExecutionError(AlphaFeasibilityDataError):
    """A started request lacks a durable response and must not be resent."""


class TushareTransport(Protocol):
    def __call__(
        self,
        *,
        endpoint: str,
        params: Mapping[str, str],
        fields: Sequence[str],
        token: str,
        timeout_seconds: int,
        maximum_response_bytes: int,
    ) -> bytes: ...


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AlphaFeasibilityDataError("duplicate_json_key")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise AlphaFeasibilityDataError("nonfinite_json_number")


def strict_json_loads(raw: bytes | str, *, label: str = "json") -> Any:
    try:
        return json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=Decimal,
            parse_constant=_reject_nonfinite,
        )
    except AlphaFeasibilityDataError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AlphaFeasibilityDataError(f"invalid_{label}_json") from exc


def _json_safe(value: Any) -> Any:
    if value is None or type(value) in {str, bool, int}:
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise AlphaFeasibilityDataError("nonfinite_decimal")
        return str(value)
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise AlphaFeasibilityDataError("non_string_json_key")
        return {key: _json_safe(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AlphaFeasibilityDataError("nonfinite_float")
        # Floats are not accepted in persisted evidence.  Their binary
        # representation is not an upstream decimal contract.
        raise AlphaFeasibilityDataError("float_not_allowed_in_evidence")
    raise AlphaFeasibilityDataError("unsupported_json_value")


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            _json_safe(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _parse_date(value: Any, label: str) -> date:
    if type(value) is not str:
        raise AlphaFeasibilityDataError(f"invalid_{label}")
    try:
        if _DATE8.fullmatch(value):
            parsed = datetime.strptime(value, "%Y%m%d").date()
        elif _DATE10.fullmatch(value):
            parsed = date.fromisoformat(value)
        else:
            raise ValueError
    except ValueError as exc:
        raise AlphaFeasibilityDataError(f"invalid_{label}") from exc
    return parsed


def _compact(value: date) -> str:
    return value.strftime("%Y%m%d")


def _iso(value: date) -> str:
    return value.isoformat()


def _month_sequence(first: str, last: str) -> tuple[str, ...]:
    if not _MONTH.fullmatch(first) or not _MONTH.fullmatch(last):
        raise AlphaFeasibilityDataError("invalid_pit_month_boundary")
    first_date = date.fromisoformat(first + "-01")
    last_date = date.fromisoformat(last + "-01")
    if first_date > last_date:
        raise AlphaFeasibilityDataError("reversed_pit_month_boundary")
    months: list[str] = []
    cursor = first_date
    while cursor <= last_date:
        months.append(cursor.strftime("%Y-%m"))
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    return tuple(months)


def _month_bounds(month: str) -> tuple[date, date]:
    start = date.fromisoformat(month + "-01")
    end = date(start.year, start.month, calendar.monthrange(start.year, start.month)[1])
    return start, end


def _scan_date_literals(value: Any) -> None:
    if isinstance(value, Mapping):
        for _key, item in value.items():
            _scan_date_literals(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _scan_date_literals(item)
        return
    if type(value) is str and (_DATE8.fullmatch(value) or _DATE10.fullmatch(value)):
        if _parse_date(value, "config_date") > ABSOLUTE_CUTOFF:
            raise AlphaFeasibilityDataError("post_cutoff_config_date")


def _reject_embedded_post_cutoff_date(value: Any, code: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_embedded_post_cutoff_date(key, code)
            _reject_embedded_post_cutoff_date(item, code)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _reject_embedded_post_cutoff_date(item, code)
        return
    if type(value) is str:
        for match in _EMBEDDED_DATE.finditer(value):
            try:
                parsed = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                continue
            if parsed > ABSOLUTE_CUTOFF:
                raise AlphaFeasibilityDataError(code)


def _contains_decoded_text(value: Any, needle: str) -> bool:
    if isinstance(value, Mapping):
        return any(
            _contains_decoded_text(key, needle) or _contains_decoded_text(item, needle)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_decoded_text(item, needle) for item in value)
    return type(value) is str and needle in value


def _reject_response_post_cutoff_dates(
    task: "CollectionTask",
    root: Mapping[str, Any],
) -> None:
    """Scan every successful response string before normalization/persistence.

    ``stock_basic.delist_date`` is the sole field-aware exception: the frozen
    contract explicitly isolates a post-cutoff delisting date to ``null`` and
    persists only the sanitized envelope.  Every other key/value string,
    including security names and suspension timing, is scanned recursively.
    """

    if task.endpoint != "stock_basic":
        _reject_embedded_post_cutoff_date(root, "post_cutoff_response_date")
        return
    data = root["data"]
    fields = data["fields"]
    items = data["items"]
    delist_index = fields.index("delist_date")
    _reject_embedded_post_cutoff_date(
        {key: value for key, value in root.items() if key != "data"},
        "post_cutoff_response_date",
    )
    _reject_embedded_post_cutoff_date(
        {key: value for key, value in data.items() if key != "items"},
        "post_cutoff_response_date",
    )
    for item in items:
        if not isinstance(item, list) or len(item) != len(fields):
            # The row-shape validator below will fail closed before persistence.
            continue
        code = item[fields.index("ts_code")]
        if type(code) is str and code not in task.scope_instruments:
            # stock_basic is an all-market endpoint. Unrelated rows are never
            # strategy inputs and their bodies are never persisted; retain only
            # their aggregate isolation count and the wire response hash.
            continue
        for index, value in enumerate(item):
            if index != delist_index:
                _reject_embedded_post_cutoff_date(value, "post_cutoff_response_date")


def _require_mapping(value: Any, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AlphaFeasibilityDataError(code)
    return value


def _require_exact_fields(value: Any, endpoint: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(type(item) is not str for item in value):
        raise AlphaFeasibilityDataError("invalid_config_fields")
    fields = tuple(value)
    if fields != EXPECTED_FIELDS[endpoint] or len(set(fields)) != len(fields):
        raise AlphaFeasibilityDataError("config_fields_differ_from_contract")
    return fields


def _verify_frozen_implementation(config: Mapping[str, Any], repository_root: Path) -> None:
    frozen = _require_mapping(
        config.get("frozen_implementation"), "missing_frozen_implementation"
    )
    pairs = (
        ("alpha_policy_path", "alpha_policy_sha256"),
        ("alpha_source_path", "alpha_source_sha256"),
        ("ranker_source_path", "ranker_source_sha256"),
        ("exposure_source_path", "exposure_source_sha256"),
    )
    for path_key, hash_key in pairs:
        relative = frozen.get(path_key)
        expected = frozen.get(hash_key)
        if (
            type(relative) is not str
            or type(expected) is not str
            or _SHA256.fullmatch(expected) is None
        ):
            raise AlphaFeasibilityDataError("invalid_frozen_implementation_binding")
        path = (repository_root / relative).resolve()
        try:
            path.relative_to(repository_root.resolve())
            raw = path.read_bytes()
        except (ValueError, OSError) as exc:
            raise AlphaFeasibilityDataError("frozen_implementation_unavailable") from exc
        if hashlib.sha256(raw).hexdigest() != expected:
            raise AlphaFeasibilityDataError("frozen_implementation_hash_mismatch")


def validate_experiment_config(
    config: Mapping[str, Any], *, repository_root: Path | str = REPOSITORY_ROOT
) -> Mapping[str, Any]:
    """Validate the complete immutable experiment boundary without a token."""

    if not isinstance(config, Mapping):
        raise AlphaFeasibilityDataError("config_root_not_object")
    _scan_date_literals(config)
    _reject_embedded_post_cutoff_date(config, "post_cutoff_config_date")
    if config.get("schema_version") != "technical-alpha-feasibility-experiment.v1":
        raise AlphaFeasibilityDataError("unexpected_experiment_schema")
    if config.get("research_status") != "research_alpha_feasibility_only":
        raise AlphaFeasibilityDataError("unexpected_research_status")
    source = _require_mapping(config.get("source"), "missing_source_config")
    if source.get("transport_target") != OFFICIAL_API_URL:
        raise AlphaFeasibilityDataError("unsafe_transport_target")
    if source.get("provider") != "tushare_standard_non_vip":
        raise AlphaFeasibilityDataError("unsafe_provider")
    if source.get("token_environment_variable") != "TUSHARE_TOKEN":
        raise AlphaFeasibilityDataError("token_environment_variable_changed")
    interval_value, _interval_text = _decimal(
        source.get("minimum_request_interval_seconds"),
        "minimum_request_interval_seconds",
        minimum=Decimal("0"),
    )
    if interval_value != Decimal("0.13"):
        raise AlphaFeasibilityDataError("request_interval_differs_from_contract")
    endpoints = source.get("allowed_endpoints")
    if (
        not isinstance(endpoints, list)
        or len(endpoints) != len(ALLOWED_ENDPOINTS)
        or set(endpoints) != set(ALLOWED_ENDPOINTS)
        or len(set(endpoints)) != len(endpoints)
    ):
        raise AlphaFeasibilityDataError("endpoint_allowlist_differs_from_contract")
    if any(endpoint.endswith("_vip") for endpoint in endpoints):
        raise AlphaFeasibilityDataError("vip_endpoint_forbidden")
    if source.get("forbidden_endpoint_suffix") != "_vip":
        raise AlphaFeasibilityDataError("vip_guard_missing")
    if source.get("redirects_allowed") is not False:
        raise AlphaFeasibilityDataError("redirects_must_be_disabled")
    if source.get("automatic_retries") != 0:
        raise AlphaFeasibilityDataError("automatic_retries_must_be_zero")
    if type(source.get("request_timeout_seconds")) is not int or not (
        1 <= source["request_timeout_seconds"] <= 60
    ):
        raise AlphaFeasibilityDataError("unsafe_request_timeout")
    if type(source.get("maximum_response_bytes")) is not int or not (
        1_024 <= source["maximum_response_bytes"] <= 16_777_216
    ):
        raise AlphaFeasibilityDataError("unsafe_response_limit")
    for key in (
        "token_persistence_forbidden",
        "field_level_fallback_forbidden",
        "baostock_field_level_fallback_forbidden",
    ):
        if source.get(key) is not True:
            raise AlphaFeasibilityDataError("source_safety_guard_missing")

    dates = _require_mapping(config.get("dates"), "missing_dates_config")
    required_dates = {
        key: _parse_date(dates.get(key), key)
        for key in (
            "signal_warmup_start",
            "development_start",
            "development_end",
            "validation_start",
            "validation_end",
            "absolute_request_and_consumer_cutoff",
        )
    }
    exact_dates = {
        "signal_warmup_start": date(2017, 7, 1),
        "development_start": date(2018, 1, 1),
        "development_end": date(2022, 12, 31),
        "validation_start": date(2023, 1, 1),
        "validation_end": date(2023, 12, 31),
        "absolute_request_and_consumer_cutoff": date(2023, 12, 31),
    }
    if required_dates != exact_dates:
        raise AlphaFeasibilityDataError("experiment_dates_differ_from_frozen_contract")
    if not (
        required_dates["signal_warmup_start"]
        <= required_dates["development_start"]
        <= required_dates["development_end"]
        < required_dates["validation_start"]
        <= required_dates["validation_end"]
        == required_dates["absolute_request_and_consumer_cutoff"]
        == ABSOLUTE_CUTOFF
    ):
        raise AlphaFeasibilityDataError("experiment_date_partition_invalid")
    if dates.get("terminal_session_has_no_cross_cutoff_next_session") is not True:
        raise AlphaFeasibilityDataError("cross_cutoff_session_guard_missing")

    index = _require_mapping(config.get("index"), "missing_index_config")
    months = _month_sequence(index.get("pit_first_month"), index.get("pit_last_month"))
    if months != _month_sequence("2017-12", "2023-12") or len(months) != 73:
        raise AlphaFeasibilityDataError("pit_month_plan_must_equal_73")
    if index.get("index_code") != "000906.SH":
        raise AlphaFeasibilityDataError("unexpected_index_code")
    if index.get("one_request_per_calendar_month") is not True:
        raise AlphaFeasibilityDataError("monthly_request_guard_missing")
    if index.get("future_snapshot_backfill_forbidden") is not True:
        raise AlphaFeasibilityDataError("future_snapshot_guard_missing")
    if index.get("expected_component_count") != 800:
        raise AlphaFeasibilityDataError("unexpected_component_count")
    if index.get("minimum_weight_decimal_places") != 3:
        raise AlphaFeasibilityDataError("unexpected_weight_precision")
    evidence_hashes = index.get("controlled_adjustment_evidence_sha256s", [])
    if (
        not isinstance(evidence_hashes, list)
        or len(evidence_hashes) != len(set(evidence_hashes))
        or any(type(item) is not str or _SHA256.fullmatch(item) is None for item in evidence_hashes)
    ):
        raise AlphaFeasibilityDataError("controlled_adjustment_evidence_registry_invalid")
    if evidence_hashes:
        # V1 has no durable, create-only evidence artifact/replay contract.
        # Refuse the half-supported branch rather than allow a first run that
        # cannot be reproduced by the loader without caller memory.
        raise AlphaFeasibilityDataError("controlled_adjustment_evidence_not_supported")

    requests = _require_mapping(config.get("requests"), "missing_request_config")
    if set(requests) != set(ALLOWED_ENDPOINTS):
        raise AlphaFeasibilityDataError("request_endpoint_set_differs")
    for endpoint in ALLOWED_ENDPOINTS:
        request = _require_mapping(requests.get(endpoint), "invalid_endpoint_request")
        _require_exact_fields(request.get("fields"), endpoint)
        params = _require_mapping(request.get("params"), "invalid_endpoint_params")
        if any(_FORBIDDEN_PARAM_KEY.search(str(key)) for key in params):
            raise AlphaFeasibilityDataError("credential_like_request_parameter")
    exact_params = {
        "trade_cal": {"exchange": "SSE", "start_date": "20170701", "end_date": "20231231"},
        "index_weight": {"index_code": "000906.SH"},
        "stock_basic": {"exchange": ""},
        "daily": {"start_date": "20170701", "end_date": "20231231"},
        "adj_factor": {"start_date": "20170701", "end_date": "20231231"},
        "suspend_d": {"start_date": "20170701", "end_date": "20231231"},
        "index_daily": {
            "ts_code": "000906.SH",
            "start_date": "20170701",
            "end_date": "20231231",
        },
    }
    for endpoint, expected_params in exact_params.items():
        if dict(requests[endpoint]["params"]) != expected_params:
            raise AlphaFeasibilityDataError("request_window_differs_from_frozen_contract")
    expected_batch_sizes = {"daily": 3, "adj_factor": 1, "suspend_d": 3}
    for endpoint, expected_size in expected_batch_sizes.items():
        if requests[endpoint].get("instrument_batch_size") != expected_size:
            raise AlphaFeasibilityDataError("history_batch_size_differs_from_contract")
    statuses = requests["stock_basic"].get("list_statuses")
    if statuses != ["L", "D", "P"]:
        raise AlphaFeasibilityDataError("stock_basic_statuses_must_equal_l_d_p")
    if config.get("locked_test_status") != dict(LOCKED_TEST_STATUS):
        raise AlphaFeasibilityDataError("locked_test_status_changed")
    if config.get("locked_test_consumed") is not False:
        raise AlphaFeasibilityDataError("locked_test_consumed_changed")
    safety = _require_mapping(config.get("safety"), "missing_safety_config")
    if (
        safety.get("execution_realism") != "INCOMPLETE"
        or safety.get("paper_eligibility") is not False
        or safety.get("trade_eligibility") is not False
        or safety.get("automatic_order_submission") is not False
        or safety.get("live_supported") is not False
    ):
        raise AlphaFeasibilityDataError("execution_safety_boundary_changed")
    _verify_frozen_implementation(config, Path(repository_root))
    return MappingProxyType(dict(config))


def _validate_wire_request_contract(
    endpoint: str,
    params: Mapping[str, str],
    fields: Sequence[str],
    *,
    scope_instruments: Sequence[str] | None = None,
) -> None:
    """Unbypassable network-boundary validation for one authorized request."""

    if endpoint not in ALLOWED_ENDPOINTS or endpoint.endswith("_vip"):
        raise AlphaFeasibilityDataError("endpoint_not_allowed")
    if tuple(fields) != EXPECTED_FIELDS[endpoint]:
        raise AlphaFeasibilityDataError("task_fields_differ_from_contract")
    values = dict(params)
    if any(type(key) is not str or type(value) is not str for key, value in values.items()):
        raise AlphaFeasibilityDataError("task_params_must_be_strings")
    fixed_window = {"start_date": "20170701", "end_date": "20231231"}
    if endpoint == "trade_cal":
        expected = {"exchange": "SSE", **fixed_window}
    elif endpoint == "index_daily":
        expected = {"ts_code": "000906.SH", **fixed_window}
    elif endpoint == "stock_basic":
        if values.get("list_status") not in {"L", "D", "P"}:
            raise AlphaFeasibilityDataError("stock_basic_status_not_allowed")
        expected = {"exchange": "", "list_status": values["list_status"]}
    elif endpoint == "index_weight":
        if set(values) != {"index_code", "start_date", "end_date"}:
            raise AlphaFeasibilityDataError("index_weight_params_differ_from_contract")
        if values.get("index_code") != "000906.SH":
            raise AlphaFeasibilityDataError("unexpected_index_code")
        start = _parse_date(values.get("start_date"), "index_weight_start")
        end = _parse_date(values.get("end_date"), "index_weight_end")
        expected_start, expected_end = _month_bounds(start.strftime("%Y-%m"))
        if (
            start != expected_start
            or end != expected_end
            or not date(2017, 12, 1) <= start <= date(2023, 12, 1)
            or end > ABSOLUTE_CUTOFF
        ):
            raise AlphaFeasibilityDataError("index_weight_month_window_invalid")
        expected = values
    else:
        codes_text = values.get("ts_code")
        if type(codes_text) is not str:
            raise AlphaFeasibilityDataError("history_request_scope_missing")
        codes = tuple(codes_text.split(","))
        maximum = 1 if endpoint == "adj_factor" else 3
        if (
            not 1 <= len(codes) <= maximum
            or len(set(codes)) != len(codes)
            or tuple(sorted(codes)) != codes
            or any(_TS_CODE.fullmatch(code) is None for code in codes)
        ):
            raise AlphaFeasibilityDataError("history_request_scope_invalid")
        expected = {**fixed_window, "ts_code": codes_text}
        if scope_instruments is not None and tuple(scope_instruments) != codes:
            raise AlphaFeasibilityDataError("task_params_scope_mismatch")
    if values != expected:
        raise AlphaFeasibilityDataError("endpoint_params_differ_from_contract")
    if scope_instruments is not None:
        scope = tuple(scope_instruments)
        if endpoint in {"daily", "adj_factor", "suspend_d"}:
            if not scope:
                raise AlphaFeasibilityDataError("task_scope_missing")
        elif endpoint == "stock_basic":
            if not scope:
                raise AlphaFeasibilityDataError("stock_basic_union_scope_missing")
        elif scope:
            raise AlphaFeasibilityDataError("unexpected_task_scope")


def _validate_collection_task_contract(task: Any) -> None:
    _validate_wire_request_contract(
        task.endpoint,
        task.params,
        task.fields,
        scope_instruments=task.scope_instruments,
    )


@dataclass(frozen=True, slots=True)
class CollectionTask:
    endpoint: str
    params: Mapping[str, str]
    fields: tuple[str, ...]
    plan_sha256: str
    scope_instruments: tuple[str, ...] = ()
    task_id: str = ""

    def __post_init__(self) -> None:
        if self.endpoint not in ALLOWED_ENDPOINTS or self.endpoint.endswith("_vip"):
            raise AlphaFeasibilityDataError("endpoint_not_allowed")
        params = dict(self.params)
        if any(type(key) is not str or type(value) is not str for key, value in params.items()):
            raise AlphaFeasibilityDataError("task_params_must_be_strings")
        if any(_FORBIDDEN_PARAM_KEY.search(key) for key in params):
            raise AlphaFeasibilityDataError("credential_like_task_parameter")
        if tuple(self.fields) != EXPECTED_FIELDS[self.endpoint]:
            raise AlphaFeasibilityDataError("task_fields_differ_from_contract")
        if _SHA256.fullmatch(self.plan_sha256) is None:
            raise AlphaFeasibilityDataError("invalid_plan_sha256")
        scope = tuple(sorted(self.scope_instruments))
        if len(set(scope)) != len(scope) or any(_TS_CODE.fullmatch(code) is None for code in scope):
            raise AlphaFeasibilityDataError("invalid_task_instrument_scope")
        semantic = {
            "schema_version": TASK_SCHEMA_VERSION,
            "endpoint": self.endpoint,
            "params": params,
            "fields": list(self.fields),
            "plan_sha256": self.plan_sha256,
            "scope_instruments_sha256": canonical_sha256(list(scope)),
        }
        expected_id = f"{self.endpoint}-{canonical_sha256(semantic)}"
        if self.task_id and self.task_id != expected_id:
            raise AlphaFeasibilityDataError("task_id_semantics_mismatch")
        object.__setattr__(self, "params", MappingProxyType(params))
        object.__setattr__(self, "scope_instruments", scope)
        object.__setattr__(self, "task_id", expected_id)
        _validate_collection_task_contract(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": TASK_SCHEMA_VERSION,
            "task_id": self.task_id,
            "endpoint": self.endpoint,
            "params": dict(self.params),
            "fields": list(self.fields),
            "plan_sha256": self.plan_sha256,
            "scope_instruments_sha256": canonical_sha256(list(self.scope_instruments)),
        }


@dataclass(frozen=True, slots=True)
class CollectionPlan:
    config: Mapping[str, Any]
    config_sha256: str
    plan_sha256: str
    pit_tasks: tuple[CollectionTask, ...]


def _task(
    endpoint: str,
    params: Mapping[str, Any],
    fields: Sequence[str],
    plan_sha256: str,
    *,
    scope_instruments: Sequence[str] = (),
) -> CollectionTask:
    normalized: dict[str, str] = {}
    for key, value in params.items():
        if type(value) not in {str, int}:
            raise AlphaFeasibilityDataError("non_scalar_task_parameter")
        normalized[str(key)] = str(value)
    return CollectionTask(
        endpoint=endpoint,
        params=normalized,
        fields=tuple(fields),
        plan_sha256=plan_sha256,
        scope_instruments=tuple(scope_instruments),
    )


def load_config_and_build_plan(
    config_path: Path | str = DEFAULT_CONFIG_PATH,
    *,
    repository_root: Path | str = REPOSITORY_ROOT,
) -> CollectionPlan:
    """Read and validate config, then construct exactly 73 PIT tasks.

    No environment lookup, network construction, output-directory access, or
    historical data access happens in this function.
    """

    path = Path(config_path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise AlphaFeasibilityDataError("experiment_config_unavailable") from exc
    config = validate_experiment_config(
        _require_mapping(strict_json_loads(raw, label="config"), "config_root_not_object"),
        repository_root=repository_root,
    )
    config_sha = hashlib.sha256(raw).hexdigest()
    months = _month_sequence(
        config["index"]["pit_first_month"], config["index"]["pit_last_month"]
    )
    semantics = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "config_sha256": config_sha,
        "absolute_cutoff": _iso(ABSOLUTE_CUTOFF),
        "allowed_endpoints": list(ALLOWED_ENDPOINTS),
        "pit_months": list(months),
        "locked_test_status": dict(LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
    }
    plan_sha = canonical_sha256(semantics)
    base = config["requests"]["index_weight"]
    tasks: list[CollectionTask] = []
    for month in months:
        start, end = _month_bounds(month)
        if end > ABSOLUTE_CUTOFF:
            raise AlphaFeasibilityDataError("post_cutoff_pit_task")
        params = dict(base["params"])
        params.update({"start_date": _compact(start), "end_date": _compact(end)})
        tasks.append(_task("index_weight", params, base["fields"], plan_sha))
    if len(tasks) != 73 or len({task.task_id for task in tasks}) != 73:
        raise AlphaFeasibilityDataError("pit_task_plan_not_exactly_73")
    return CollectionPlan(config=config, config_sha256=config_sha, plan_sha256=plan_sha, pit_tasks=tuple(tasks))


def build_history_plan(
    plan: CollectionPlan, union_instruments: Iterable[str]
) -> tuple[CollectionTask, ...]:
    """Build the minimal post-PIT history plan for the exact member union."""

    instruments = tuple(sorted(set(union_instruments)))
    if not instruments or any(_TS_CODE.fullmatch(code) is None for code in instruments):
        raise AlphaFeasibilityDataError("invalid_or_empty_union")
    requests = plan.config["requests"]
    tasks: list[CollectionTask] = []
    stock = requests["stock_basic"]
    for status in stock["list_statuses"]:
        params = dict(stock["params"])
        params["list_status"] = status
        tasks.append(
            _task(
                "stock_basic",
                params,
                stock["fields"],
                plan.plan_sha256,
                scope_instruments=instruments,
            )
        )
    trade_cal = requests["trade_cal"]
    tasks.append(_task("trade_cal", trade_cal["params"], trade_cal["fields"], plan.plan_sha256))
    index_daily = requests["index_daily"]
    tasks.append(
        _task("index_daily", index_daily["params"], index_daily["fields"], plan.plan_sha256)
    )
    for endpoint in ("daily", "adj_factor", "suspend_d"):
        request = requests[endpoint]
        batch_size = request["instrument_batch_size"]
        for offset in range(0, len(instruments), batch_size):
            batch = instruments[offset : offset + batch_size]
            params = dict(request["params"])
            params["ts_code"] = ",".join(batch)
            tasks.append(
                _task(
                    endpoint,
                    params,
                    request["fields"],
                    plan.plan_sha256,
                    scope_instruments=batch,
                )
            )
    if len({task.task_id for task in tasks}) != len(tasks):
        raise AlphaFeasibilityDataError("duplicate_history_task")
    for task in tasks:
        for key in ("start_date", "end_date"):
            if key in task.params and _parse_date(task.params[key], "task_date") > ABSOLUTE_CUTOFF:
                raise AlphaFeasibilityDataError("post_cutoff_history_task")
    return tuple(tasks)


class HttpsTushareTransport:
    """Minimal standard-library HTTPS POST transport with no retry/redirect."""

    def __call__(
        self,
        *,
        endpoint: str,
        params: Mapping[str, str],
        fields: Sequence[str],
        token: str,
        timeout_seconds: int,
        maximum_response_bytes: int,
    ) -> bytes:
        # Keep this guard at the physical network boundary as well as on
        # CollectionTask construction.  A caller must not be able to bypass
        # the frozen endpoint/date/field contract by invoking the transport
        # directly.
        _validate_wire_request_contract(endpoint, params, fields)
        _validate_token(token)
        payload = json.dumps(
            {
                "api_name": endpoint,
                "token": token,
                "params": dict(params),
                "fields": ",".join(fields),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        connection = http.client.HTTPSConnection(
            OFFICIAL_API_HOST,
            port=443,
            timeout=timeout_seconds,
            context=ssl.create_default_context(),
        )
        try:
            connection.request(
                "POST",
                OFFICIAL_API_PATH,
                body=payload,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Content-Length": str(len(payload)),
                    "Accept": "application/json",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            if 300 <= response.status <= 399:
                raise AlphaFeasibilityDataError("http_redirect_forbidden")
            if response.status != 200:
                raise AlphaFeasibilityDataError("http_status_not_success")
            raw = response.read(maximum_response_bytes + 1)
            if len(raw) > maximum_response_bytes:
                raise AlphaFeasibilityDataError("response_size_limit_exceeded")
            return raw
        except AlphaFeasibilityDataError:
            raise
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            raise AlphaFeasibilityDataError("https_transport_failed") from exc
        finally:
            connection.close()


def _validate_token(token: Any) -> str:
    if (
        type(token) is not str
        or not 16 <= len(token) <= 128
        or re.fullmatch(r"[A-Za-z0-9]+", token) is None
    ):
        raise AlphaFeasibilityDataError("credential_preflight_failed")
    return token


def _decimal(value: Any, label: str, *, minimum: Decimal | None = None) -> tuple[Decimal, str]:
    if isinstance(value, bool) or value is None:
        raise AlphaFeasibilityDataError(f"invalid_{label}")
    if isinstance(value, Decimal):
        number = value
        text = str(value)
    elif type(value) in {str, int}:
        text = str(value)
        try:
            number = Decimal(text)
        except InvalidOperation as exc:
            raise AlphaFeasibilityDataError(f"invalid_{label}") from exc
    else:
        raise AlphaFeasibilityDataError(f"invalid_{label}")
    if not number.is_finite() or (minimum is not None and number < minimum):
        raise AlphaFeasibilityDataError(f"invalid_{label}")
    return number, text


def _decimal_places(text: str) -> int:
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise AlphaFeasibilityDataError("invalid_decimal_precision") from exc
    return max(0, -value.as_tuple().exponent)


def _normalized_code(value: Any, label: str = "ts_code") -> str:
    if type(value) is not str or _TS_CODE.fullmatch(value) is None:
        raise AlphaFeasibilityDataError(f"invalid_{label}")
    return value


def _response_date_window(task: CollectionTask, value: str, label: str) -> date:
    parsed = _parse_date(value, label)
    if parsed > ABSOLUTE_CUTOFF:
        raise AlphaFeasibilityDataError("post_cutoff_response_date")
    if "start_date" in task.params and parsed < _parse_date(task.params["start_date"], "task_start"):
        raise AlphaFeasibilityDataError("response_date_before_request_window")
    if "end_date" in task.params and parsed > _parse_date(task.params["end_date"], "task_end"):
        raise AlphaFeasibilityDataError("response_date_after_request_window")
    return parsed


def _normalize_response_row(
    task: CollectionTask, row: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, int]:
    """Return a safe row and count of isolated post-cutoff delist dates."""

    endpoint = task.endpoint
    result = dict(row)
    isolated = 0
    if endpoint == "trade_cal":
        if type(result["exchange"]) is not str or result["exchange"] != task.params.get("exchange"):
            raise AlphaFeasibilityDataError("response_exchange_not_requested")
        parsed = _response_date_window(task, result["cal_date"], "calendar_date")
        result["cal_date"] = _compact(parsed)
        is_open = result["is_open"]
        if not (
            (type(is_open) is int and is_open in {0, 1})
            or (type(is_open) is str and is_open in {"0", "1"})
        ):
            raise AlphaFeasibilityDataError("invalid_is_open")
        result["is_open"] = int(is_open)
        if result["pretrade_date"] not in {None, ""}:
            pretrade = _parse_date(result["pretrade_date"], "pretrade_date")
            if pretrade > parsed or pretrade > ABSOLUTE_CUTOFF:
                raise AlphaFeasibilityDataError("invalid_pretrade_date")
            result["pretrade_date"] = _compact(pretrade)
        else:
            result["pretrade_date"] = None
    elif endpoint == "index_weight":
        if result["index_code"] != task.params.get("index_code"):
            raise AlphaFeasibilityDataError("response_index_not_requested")
        result["con_code"] = _normalized_code(result["con_code"], "component_code")
        if _PIT_COMPONENT_CODE.fullmatch(result["con_code"]) is None:
            raise AlphaFeasibilityDataError("pit_component_exchange_not_allowed")
        parsed = _response_date_window(task, result["trade_date"], "trade_date")
        result["trade_date"] = _compact(parsed)
        weight, text = _decimal(result["weight"], "weight", minimum=Decimal("0"))
        if _decimal_places(text) < 3:
            raise AlphaFeasibilityDataError("weight_precision_below_three_decimals")
        result["weight"] = text
        if weight < 0:  # kept explicit for the PIT contract
            raise AlphaFeasibilityDataError("negative_weight")
    elif endpoint == "stock_basic":
        code = _normalized_code(result["ts_code"])
        if code not in task.scope_instruments:
            return None, 0
        if result["list_status"] != task.params.get("list_status"):
            raise AlphaFeasibilityDataError("stock_status_not_requested")
        if type(result["symbol"]) is not str or result["symbol"] != code.split(".")[0]:
            raise AlphaFeasibilityDataError("stock_symbol_code_mismatch")
        exchange_by_suffix = {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}
        if result["exchange"] != exchange_by_suffix[code.split(".")[1]]:
            raise AlphaFeasibilityDataError("stock_exchange_code_mismatch")
        listed = _parse_date(result["list_date"], "list_date")
        if listed > ABSOLUTE_CUTOFF:
            raise AlphaFeasibilityDataError("post_cutoff_list_date")
        result["list_date"] = _compact(listed)
        delisted = result["delist_date"]
        if delisted in {None, ""}:
            result["delist_date"] = None
        else:
            delisted_date = _parse_date(delisted, "delist_date")
            if delisted_date > ABSOLUTE_CUTOFF:
                # This metadata is known now but was not available inside the
                # experiment window.  Do not persist the future date itself.
                result["delist_date"] = None
                isolated = 1
            else:
                if delisted_date < listed:
                    raise AlphaFeasibilityDataError("delist_before_list_date")
                result["delist_date"] = _compact(delisted_date)
        if type(result["name"]) is not str:
            raise AlphaFeasibilityDataError("invalid_stock_name")
    elif endpoint in {"daily", "adj_factor", "suspend_d"}:
        code = _normalized_code(result["ts_code"])
        if code not in task.scope_instruments:
            raise AlphaFeasibilityDataError("response_instrument_not_requested")
        parsed = _response_date_window(task, result["trade_date"], "trade_date")
        result["trade_date"] = _compact(parsed)
        if endpoint == "daily":
            for field in ("open", "high", "low", "close", "pre_close", "vol", "amount"):
                number, text = _decimal(result[field], field, minimum=Decimal("0"))
                result[field] = text
                if field in {"open", "high", "low", "close", "pre_close"} and number <= 0:
                    raise AlphaFeasibilityDataError("nonpositive_daily_price")
            high = Decimal(result["high"])
            low = Decimal(result["low"])
            if high < low or high < Decimal(result["open"]) or high < Decimal(result["close"]):
                raise AlphaFeasibilityDataError("invalid_daily_ohlc")
            if low > Decimal(result["open"]) or low > Decimal(result["close"]):
                raise AlphaFeasibilityDataError("invalid_daily_ohlc")
        elif endpoint == "adj_factor":
            factor, text = _decimal(result["adj_factor"], "adj_factor", minimum=Decimal("0"))
            if factor <= 0:
                raise AlphaFeasibilityDataError("nonpositive_adj_factor")
            result["adj_factor"] = text
        else:
            if type(result["suspend_type"]) is not str or result["suspend_type"] not in {"S", "R"}:
                raise AlphaFeasibilityDataError("invalid_suspend_type")
            if result["suspend_timing"] is not None and type(result["suspend_timing"]) is not str:
                raise AlphaFeasibilityDataError("invalid_suspend_timing")
    elif endpoint == "index_daily":
        if result["ts_code"] != task.params.get("ts_code"):
            raise AlphaFeasibilityDataError("response_index_not_requested")
        parsed = _response_date_window(task, result["trade_date"], "trade_date")
        result["trade_date"] = _compact(parsed)
        for field in (
            "open",
            "high",
            "low",
            "close",
            "pre_close",
            "change",
            "pct_chg",
            "vol",
            "amount",
        ):
            minimum = Decimal("0") if field not in {"change", "pct_chg"} else None
            number, text = _decimal(result[field], field, minimum=minimum)
            result[field] = text
            if field in {"open", "high", "low", "close", "pre_close"} and number <= 0:
                raise AlphaFeasibilityDataError("nonpositive_index_price")
    else:  # pragma: no cover - CollectionTask rejects this first
        raise AlphaFeasibilityDataError("endpoint_not_allowed")
    return result, isolated


def _primary_key(endpoint: str, row: Mapping[str, Any]) -> tuple[Any, ...]:
    if endpoint == "trade_cal":
        return row["exchange"], row["cal_date"]
    if endpoint == "index_weight":
        return row["index_code"], row["trade_date"], row["con_code"]
    if endpoint == "stock_basic":
        return (row["ts_code"],)
    return row["ts_code"], row["trade_date"]


def _row_sort_key(endpoint: str, row: Mapping[str, Any]) -> tuple[Any, ...]:
    if endpoint == "trade_cal":
        return row["cal_date"], row["exchange"]
    if endpoint == "index_weight":
        return row["trade_date"], row["con_code"]
    if endpoint == "stock_basic":
        return (row["ts_code"],)
    return row["trade_date"], row["ts_code"]


@dataclass(frozen=True, slots=True)
class ValidatedResponse:
    rows: tuple[Mapping[str, Any], ...]
    raw_response_sha256: str
    isolated_future_delist_date_count: int
    isolated_non_union_row_count: int


def validate_response_bytes(
    task: CollectionTask,
    raw: bytes,
    *,
    token: str | None = None,
    maximum_response_bytes: int = 8_388_608,
) -> ValidatedResponse:
    """Validate bounded response bytes before any body can be persisted."""

    if not isinstance(raw, bytes):
        raise AlphaFeasibilityDataError("transport_response_not_bytes")
    if len(raw) > maximum_response_bytes:
        raise AlphaFeasibilityDataError("response_size_limit_exceeded")
    if token is not None and token.encode("utf-8") in raw:
        # Do not even compute a digest of a credential-bearing response.
        raise AlphaFeasibilityDataError("credential_echo_in_response")
    raw_sha = hashlib.sha256(raw).hexdigest()
    root = strict_json_loads(raw, label="response")
    if not isinstance(root, Mapping):
        raise AlphaFeasibilityDataError("response_root_not_object")
    if token is not None and _contains_decoded_text(root, token):
        raise AlphaFeasibilityDataError("credential_echo_in_response")
    if set(root) not in ({"code", "msg", "data"}, {"request_id", "code", "msg", "data"}):
        raise AlphaFeasibilityDataError("response_root_fields_differ_from_contract")
    if root.get("msg") is not None and type(root.get("msg")) is not str:
        raise AlphaFeasibilityDataError("response_message_type_invalid")
    if "request_id" in root and type(root["request_id"]) is not str:
        raise AlphaFeasibilityDataError("response_request_id_type_invalid")
    _reject_embedded_post_cutoff_date(
        {key: root[key] for key in ("msg", "request_id") if key in root},
        "post_cutoff_response_date",
    )
    code = root.get("code")
    if type(code) is not int or code != 0:
        raise AlphaFeasibilityDataError("upstream_response_not_success")
    data = root.get("data")
    if not isinstance(data, Mapping):
        raise AlphaFeasibilityDataError("response_data_not_object")
    if set(data) != {"fields", "items"}:
        raise AlphaFeasibilityDataError("response_data_fields_differ_from_contract")
    fields = data.get("fields")
    items = data.get("items")
    if not isinstance(fields, list) or tuple(fields) != task.fields:
        raise AlphaFeasibilityDataError("response_fields_differ_from_contract")
    if not isinstance(items, list):
        raise AlphaFeasibilityDataError("response_items_not_array")
    _reject_response_post_cutoff_dates(task, root)
    if len(items) >= POTENTIAL_TRUNCATION_LIMIT[task.endpoint]:
        raise AlphaFeasibilityDataError("potential_upstream_truncation")
    normalized: list[Mapping[str, Any]] = []
    isolated = 0
    isolated_non_union = 0
    keys: set[tuple[Any, ...]] = set()
    for item in items:
        if not isinstance(item, list) or len(item) != len(fields):
            raise AlphaFeasibilityDataError("response_row_shape_invalid")
        row, row_isolated = _normalize_response_row(task, dict(zip(fields, item)))
        isolated += row_isolated
        if row is None:
            isolated_non_union += 1
            continue
        key = _primary_key(task.endpoint, row)
        if key in keys:
            raise AlphaFeasibilityDataError("duplicate_response_primary_key")
        keys.add(key)
        normalized.append(MappingProxyType(row))
    normalized.sort(key=lambda row: _row_sort_key(task.endpoint, row))
    return ValidatedResponse(
        rows=tuple(normalized),
        raw_response_sha256=raw_sha,
        isolated_future_delist_date_count=isolated,
        isolated_non_union_row_count=isolated_non_union,
    )


def _write_create_only(path: Path, content: bytes) -> None:
    """Atomically publish bytes without ever replacing an existing artifact."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".alpha-feasibility-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            if handle.write(content) != len(content):
                raise AlphaFeasibilityDataError("short_artifact_write")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise AlphaFeasibilityDataError("create_only_artifact_exists") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _guard_artifact_secret(content: bytes, token: str | None) -> None:
    if not token:
        return
    if token.encode("utf-8") in content:
        raise AlphaFeasibilityDataError("credential_persistence_forbidden")
    try:
        decoded = strict_json_loads(content, label="artifact_secret_guard")
    except AlphaFeasibilityDataError:
        # Direct byte matching remains the safe fallback for non-JSON content;
        # validated provider responses and all collector artifacts are JSON.
        return
    if _contains_decoded_text(decoded, token):
        raise AlphaFeasibilityDataError("credential_persistence_forbidden")


def _write_json_create_only(path: Path, value: Any, *, token: str | None = None) -> bytes:
    content = canonical_json_bytes(value)
    _guard_artifact_secret(content, token)
    _write_create_only(path, content)
    return content


def _publish_or_verify_identical(
    path: Path, value: Any, *, token: str | None = None
) -> bytes:
    content = canonical_json_bytes(value)
    _guard_artifact_secret(content, token)
    if path.exists():
        try:
            existing = path.read_bytes()
        except OSError as exc:
            raise AlphaFeasibilityDataError("existing_artifact_unreadable") from exc
        if existing != content:
            raise AlphaFeasibilityDataError("existing_artifact_content_mismatch")
        return existing
    _write_create_only(path, content)
    return content


@dataclass(frozen=True, slots=True)
class TaskExecutionResult:
    task: CollectionTask
    rows: tuple[Mapping[str, Any], ...]
    raw_response_sha256: str
    replayed: bool
    raw_response_persisted: bool
    isolated_future_delist_date_count: int
    isolated_non_union_row_count: int
    wire_response_sha256: str
    response_artifact_sha256: str


class CreateOnlyTaskStore:
    """Create-only request journal and normalized response store."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def started_path(self, task: CollectionTask) -> Path:
        return self.root / "tasks" / f"{task.task_id}.started.json"

    def response_path(self, task: CollectionTask) -> Path:
        return self.root / "tasks" / f"{task.task_id}.response.json"

    def quarantine_path(self, task: CollectionTask) -> Path:
        return self.root / "quarantine" / f"{task.task_id}.json"

    def raw_path(self, task: CollectionTask) -> Path:
        return self.root / "raw" / f"{task.task_id}.json"

    def is_complete(self, task: CollectionTask) -> bool:
        return self.started_path(task).is_file() and self.response_path(task).is_file()

    def _load_started(self, task: CollectionTask) -> Mapping[str, Any]:
        try:
            value = strict_json_loads(self.started_path(task).read_bytes(), label="started_artifact")
        except OSError as exc:
            raise AlphaFeasibilityDataError("started_artifact_unreadable") from exc
        if not isinstance(value, Mapping) or value != {
            "schema_version": STARTED_SCHEMA_VERSION,
            "state": "NETWORK_CALL_STARTED",
            "request_count": 1,
            "task": task.to_dict(),
            "locked_test_status": dict(LOCKED_TEST_STATUS),
            "locked_test_consumed": False,
        }:
            raise AlphaFeasibilityDataError("started_artifact_semantics_mismatch")
        return value

    def _load_response(self, task: CollectionTask) -> TaskExecutionResult:
        try:
            raw = self.response_path(task).read_bytes()
            value = strict_json_loads(raw, label="response_artifact")
        except OSError as exc:
            raise AlphaFeasibilityDataError("response_artifact_unreadable") from exc
        if not isinstance(value, Mapping):
            raise AlphaFeasibilityDataError("response_artifact_not_object")
        expected_keys = {
            "schema_version",
            "state",
            "task_id",
            "endpoint",
            "plan_sha256",
            "raw_response_sha256",
            "wire_response_sha256",
            "raw_response_persisted",
            "normalized_rows_sha256",
            "row_count",
            "isolated_future_delist_date_count",
            "isolated_non_union_row_count",
            "rows",
            "locked_test_status",
            "locked_test_consumed",
            "response_artifact_sha256",
        }
        if set(value) != expected_keys:
            raise AlphaFeasibilityDataError("response_artifact_fields_mismatch")
        if (
            value["schema_version"] != RESPONSE_SCHEMA_VERSION
            or value["state"] != "RESPONSE_VALIDATED"
            or value["task_id"] != task.task_id
            or value["endpoint"] != task.endpoint
            or value["plan_sha256"] != task.plan_sha256
            or type(value["raw_response_sha256"]) is not str
            or _SHA256.fullmatch(value["raw_response_sha256"]) is None
            or type(value["wire_response_sha256"]) is not str
            or _SHA256.fullmatch(value["wire_response_sha256"]) is None
            or type(value["raw_response_persisted"]) is not bool
            or type(value["isolated_future_delist_date_count"]) is not int
            or value["isolated_future_delist_date_count"] < 0
            or type(value["isolated_non_union_row_count"]) is not int
            or value["isolated_non_union_row_count"] < 0
            or value["locked_test_status"] != dict(LOCKED_TEST_STATUS)
            or value["locked_test_consumed"] is not False
            or not isinstance(value["rows"], list)
            or value["row_count"] != len(value["rows"])
            or value["normalized_rows_sha256"] != canonical_sha256(value["rows"])
        ):
            raise AlphaFeasibilityDataError("response_artifact_semantics_mismatch")
        unsigned_response = dict(value)
        declared_response_hash = unsigned_response.pop("response_artifact_sha256", None)
        if (
            type(declared_response_hash) is not str
            or _SHA256.fullmatch(declared_response_hash) is None
            or declared_response_hash != canonical_sha256(unsigned_response)
        ):
            raise AlphaFeasibilityDataError("response_artifact_hash_mismatch")
        if value["raw_response_persisted"]:
            try:
                persisted_raw = self.raw_path(task).read_bytes()
            except OSError as exc:
                raise AlphaFeasibilityDataError("raw_response_artifact_unavailable") from exc
            if hashlib.sha256(persisted_raw).hexdigest() != value["raw_response_sha256"]:
                raise AlphaFeasibilityDataError("raw_response_hash_mismatch")
            if (
                task.endpoint != "stock_basic"
                and value["wire_response_sha256"] != value["raw_response_sha256"]
            ):
                raise AlphaFeasibilityDataError("wire_response_hash_mismatch")
            replay = validate_response_bytes(task, persisted_raw)
            if [dict(row) for row in replay.rows] != value["rows"]:
                raise AlphaFeasibilityDataError("raw_response_replay_mismatch")
        else:
            if self.raw_path(task).exists():
                raise AlphaFeasibilityDataError("unexpected_raw_response_artifact")
            if task.endpoint != "stock_basic" and value["isolated_future_delist_date_count"] == 0:
                raise AlphaFeasibilityDataError("missing_required_raw_response_artifact")
            seen: set[tuple[Any, ...]] = set()
            for persisted_row in value["rows"]:
                if not isinstance(persisted_row, Mapping) or set(persisted_row) != set(task.fields):
                    raise AlphaFeasibilityDataError("normalized_row_fields_mismatch")
                replayed_row, isolated = _normalize_response_row(task, persisted_row)
                if replayed_row is None or isolated:
                    raise AlphaFeasibilityDataError("normalized_row_replay_mismatch")
                key = _primary_key(task.endpoint, replayed_row)
                if key in seen or dict(replayed_row) != dict(persisted_row):
                    raise AlphaFeasibilityDataError("normalized_row_replay_mismatch")
                seen.add(key)
        return TaskExecutionResult(
            task=task,
            rows=tuple(MappingProxyType(dict(row)) for row in value["rows"]),
            raw_response_sha256=value["raw_response_sha256"],
            replayed=True,
            raw_response_persisted=value["raw_response_persisted"],
            isolated_future_delist_date_count=value["isolated_future_delist_date_count"],
            isolated_non_union_row_count=value["isolated_non_union_row_count"],
            wire_response_sha256=value["wire_response_sha256"],
            response_artifact_sha256=value["response_artifact_sha256"],
        )

    def execute(
        self,
        task: CollectionTask,
        *,
        token: str,
        transport: TushareTransport,
        timeout_seconds: int,
        maximum_response_bytes: int,
    ) -> TaskExecutionResult:
        # Revalidate even frozen dataclasses at the store boundary so a forged
        # or deserialized task cannot create a durable started claim or reach
        # an injected transport.
        _validate_collection_task_contract(task)
        _validate_token(token)
        started_exists = self.started_path(task).exists()
        response_exists = self.response_path(task).exists()
        if response_exists and not started_exists:
            raise AlphaFeasibilityDataError("response_without_started_artifact")
        if started_exists:
            self._load_started(task)
            if response_exists:
                return self._load_response(task)
            raise AmbiguousRemoteExecutionError("ambiguous_started_without_response")

        started = _started_payload(task)
        _write_json_create_only(self.started_path(task), started, token=token)
        raw_sha: str | None = None
        try:
            raw = transport(
                endpoint=task.endpoint,
                params=task.params,
                fields=task.fields,
                token=token,
                timeout_seconds=timeout_seconds,
                maximum_response_bytes=maximum_response_bytes,
            )
            if isinstance(raw, bytes) and token.encode("utf-8") not in raw:
                raw_sha = hashlib.sha256(raw).hexdigest()
            validated = validate_response_bytes(
                task,
                raw,
                token=token,
                maximum_response_bytes=maximum_response_bytes,
            )
            rows = [dict(row) for row in validated.rows]
            persisted_response = raw
            if task.endpoint == "stock_basic":
                # The wire body is all-market and can include now-known future
                # metadata. Persist a complete cutoff-safe response envelope
                # for the requested union so its declared response hash remains
                # offline replayable without retaining unrelated instruments.
                persisted_response = canonical_json_bytes(
                    {
                        "code": 0,
                        "msg": None,
                        "data": {
                            "fields": list(task.fields),
                            "items": [
                                [row[field] for field in task.fields] for row in rows
                            ],
                        },
                    }
                )
            _guard_artifact_secret(persisted_response, token)
            _write_create_only(self.raw_path(task), persisted_response)
            persisted_sha = hashlib.sha256(persisted_response).hexdigest()
            response = {
                "schema_version": RESPONSE_SCHEMA_VERSION,
                "state": "RESPONSE_VALIDATED",
                "task_id": task.task_id,
                "endpoint": task.endpoint,
                "plan_sha256": task.plan_sha256,
                "raw_response_sha256": persisted_sha,
                "wire_response_sha256": validated.raw_response_sha256,
                "raw_response_persisted": True,
                "normalized_rows_sha256": canonical_sha256(rows),
                "row_count": len(rows),
                "isolated_future_delist_date_count": validated.isolated_future_delist_date_count,
                "isolated_non_union_row_count": validated.isolated_non_union_row_count,
                "rows": rows,
                "locked_test_status": dict(LOCKED_TEST_STATUS),
                "locked_test_consumed": False,
            }
            response["response_artifact_sha256"] = canonical_sha256(response)
            _write_json_create_only(self.response_path(task), response, token=token)
            return TaskExecutionResult(
                task=task,
                rows=validated.rows,
                raw_response_sha256=persisted_sha,
                replayed=False,
                raw_response_persisted=True,
                isolated_future_delist_date_count=validated.isolated_future_delist_date_count,
                isolated_non_union_row_count=validated.isolated_non_union_row_count,
                wire_response_sha256=validated.raw_response_sha256,
                response_artifact_sha256=response["response_artifact_sha256"],
            )
        except Exception as exc:
            code = exc.code if isinstance(exc, AlphaFeasibilityDataError) else "unclassified_task_failure"
            quarantine = {
                "schema_version": QUARANTINE_SCHEMA_VERSION,
                "state": "RESPONSE_QUARANTINED",
                "task_id": task.task_id,
                "endpoint": task.endpoint,
                "plan_sha256": task.plan_sha256,
                "reason": code,
                "raw_response_sha256": raw_sha,
                "raw_response_persisted": False,
                "locked_test_status": dict(LOCKED_TEST_STATUS),
                "locked_test_consumed": False,
            }
            if not self.quarantine_path(task).exists():
                _write_json_create_only(self.quarantine_path(task), quarantine, token=token)
            if isinstance(exc, AlphaFeasibilityDataError):
                raise
            raise AlphaFeasibilityDataError("unclassified_task_failure") from exc


def _started_payload(task: CollectionTask) -> dict[str, Any]:
    return {
        "schema_version": STARTED_SCHEMA_VERSION,
        "state": "NETWORK_CALL_STARTED",
        "request_count": 1,
        "task": task.to_dict(),
        "locked_test_status": dict(LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
    }


def _self_hashed(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    if field in value:
        raise AlphaFeasibilityDataError("self_hash_field_must_be_derived")
    result = dict(value)
    result[field] = canonical_sha256(result)
    return result


def actual_tushare_request_count_by_endpoint(
    output_root: Path | str,
    expected_tasks: Sequence[CollectionTask] | None = None,
    *,
    plan_sha256: str | None = None,
) -> dict[str, int]:
    """Derive conservative request counts only from durable started claims."""

    if plan_sha256 is not None and (
        type(plan_sha256) is not str or _SHA256.fullmatch(plan_sha256) is None
    ):
        raise AlphaFeasibilityDataError("invalid_plan_sha256")
    counts = {endpoint: 0 for endpoint in ALLOWED_ENDPOINTS}
    task_directory = Path(output_root) / "tasks"
    if not task_directory.exists():
        return counts
    expected = (
        {task.task_id: task for task in expected_tasks}
        if expected_tasks is not None
        else None
    )
    if expected is not None:
        expected_plan_hashes = {task.plan_sha256 for task in expected.values()}
        if len(expected_plan_hashes) > 1 or (
            plan_sha256 is not None
            and bool(expected_plan_hashes)
            and expected_plan_hashes != {plan_sha256}
        ):
            raise AlphaFeasibilityDataError("expected_task_plan_mismatch")
    observed_ids: set[str] = set()
    for path in task_directory.glob("*.started.json"):
        try:
            value = strict_json_loads(path.read_bytes(), label="started_artifact")
        except OSError as exc:
            raise AlphaFeasibilityDataError("started_artifact_unreadable") from exc
        if (
            not isinstance(value, Mapping)
            or value.get("schema_version") != STARTED_SCHEMA_VERSION
            or value.get("state") != "NETWORK_CALL_STARTED"
            or value.get("request_count") != 1
            or not isinstance(value.get("task"), Mapping)
            or value["task"].get("endpoint") not in counts
            or value["task"].get("task_id") is None
            or type(value["task"].get("plan_sha256")) is not str
            or _SHA256.fullmatch(value["task"]["plan_sha256"]) is None
            or path.name != f"{value['task']['task_id']}.started.json"
        ):
            raise AlphaFeasibilityDataError("invalid_started_request_evidence")
        task_id = value["task"]["task_id"]
        if task_id in observed_ids:
            raise AlphaFeasibilityDataError("duplicate_started_request_evidence")
        observed_ids.add(task_id)
        if expected is not None:
            task = expected.get(task_id)
            if task is None or value != _started_payload(task):
                raise AlphaFeasibilityDataError("started_request_outside_current_plan")
        elif plan_sha256 is not None and value["task"]["plan_sha256"] != plan_sha256:
            raise AlphaFeasibilityDataError("started_request_outside_current_plan")
        counts[value["task"]["endpoint"]] += 1
    return counts


def execute_tasks(
    tasks: Sequence[CollectionTask],
    *,
    store: CreateOnlyTaskStore,
    token: str,
    transport: TushareTransport,
    timeout_seconds: int,
    maximum_response_bytes: int,
    minimum_request_interval_seconds: Decimal = Decimal("0"),
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[TaskExecutionResult, ...]:
    results: list[TaskExecutionResult] = []
    last_network_call: float | None = None
    interval = float(minimum_request_interval_seconds)
    for task in tasks:
        will_replay = store.is_complete(task)
        if not will_replay and last_network_call is not None and interval > 0:
            delay = interval - (monotonic() - last_network_call)
            if delay > 0:
                sleeper(delay)
        result = store.execute(
            task,
            token=token,
            transport=transport,
            timeout_seconds=timeout_seconds,
            maximum_response_bytes=maximum_response_bytes,
        )
        if not result.replayed:
            last_network_call = monotonic()
        results.append(result)
    return tuple(results)


def execute_tasks_bounded(
    tasks: Sequence[CollectionTask],
    *,
    store: CreateOnlyTaskStore,
    token: str,
    transport: TushareTransport,
    timeout_seconds: int,
    maximum_response_bytes: int,
    minimum_request_interval_seconds: Decimal = Decimal("0"),
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Execute tasks while retaining at most one normalized response in memory."""

    last_network_call: float | None = None
    interval = float(minimum_request_interval_seconds)
    for task in tasks:
        will_replay = store.is_complete(task)
        if not will_replay and last_network_call is not None and interval > 0:
            delay = interval - (monotonic() - last_network_call)
            if delay > 0:
                sleeper(delay)
        result = store.execute(
            task,
            token=token,
            transport=transport,
            timeout_seconds=timeout_seconds,
            maximum_response_bytes=maximum_response_bytes,
        )
        if not result.replayed:
            last_network_call = monotonic()
        # Do not append result: its rows are already durably content-addressed.
        del result


def _generated_at(value: datetime | None) -> str:
    current = value or datetime.now(timezone.utc)
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise AlphaFeasibilityDataError("generated_at_must_be_timezone_aware")
    return current.isoformat()


def _is_full_session_suspension(row: Mapping[str, Any] | None) -> bool:
    if not row or row.get("suspend_type") != "S":
        return False
    timing = row.get("suspend_timing")
    if timing is None:
        return True
    if type(timing) is not str:
        return False
    return timing.strip() in {"", "全天", "全日", "全天停牌", "全日停牌"}


def _validate_weight_snapshot(
    rows: Sequence[Mapping[str, Any]], *, expected_count: int
) -> tuple[Decimal, Decimal]:
    codes = [row["con_code"] for row in rows]
    if any(type(code) is not str or _PIT_COMPONENT_CODE.fullmatch(code) is None for code in codes):
        raise AlphaFeasibilityDataError("pit_component_exchange_not_allowed", stage="pit")
    if len(codes) != len(set(codes)):
        raise AlphaFeasibilityDataError("duplicate_component_in_snapshot", stage="pit")
    if len(rows) == 0:
        raise AlphaFeasibilityDataError("empty_pit_snapshot", stage="pit")
    total = Decimal("0")
    tolerance = Decimal("0")
    for row in rows:
        weight, text = _decimal(row["weight"], "weight", minimum=Decimal("0"))
        places = _decimal_places(text)
        if places < 3:
            raise AlphaFeasibilityDataError("weight_precision_below_three_decimals", stage="pit")
        total += weight
        tolerance += Decimal("0.5") * (Decimal(10) ** (-places))
    if abs(total - Decimal("100")) > tolerance:
        raise AlphaFeasibilityDataError("weight_sum_outside_row_precision_tolerance", stage="pit")
    return total, tolerance


@dataclass(frozen=True, slots=True)
class PitMembershipResult:
    coverage_report: Mapping[str, Any]
    manifest: Mapping[str, Any]
    union_instruments: tuple[str, ...]
    passed: bool


def build_pit_membership_artifacts(
    plan: CollectionPlan,
    results: Mapping[str, TaskExecutionResult | Sequence[Mapping[str, Any]]],
    *,
    adjustment_evidence: Mapping[str, Any] | None = None,
    generated_at: datetime | None = None,
) -> PitMembershipResult:
    """Validate 73 monthly responses and retain every legal PIT snapshot."""

    if adjustment_evidence is not None:
        raise AlphaFeasibilityDataError(
            "controlled_adjustment_evidence_not_supported", stage="pit"
        )

    expected_count = int(plan.config["index"]["expected_component_count"])
    month_details: list[dict[str, Any]] = []
    selected_snapshots: list[dict[str, Any]] = []
    blockers: list[str] = []
    observed = 0
    previous_selected: date | None = None
    for task in plan.pit_tasks:
        month = _parse_date(task.params["start_date"], "pit_start").strftime("%Y-%m")
        value = results.get(task.task_id)
        request_sha = hashlib.sha256(canonical_json_bytes(_started_payload(task))).hexdigest()
        if value is None:
            issue = f"{month}:missing_month_response"
            blockers.append(issue)
            month_details.append(
                {
                    "month": month,
                    "request_artifact_sha256": request_sha,
                    "response_sha256": hashlib.sha256(b"").hexdigest(),
                    "snapshots": [],
                    "selected_snapshot_date": None,
                    "status": "missing",
                    "issues": ["missing_month_response"],
                }
            )
            continue
        rows = value.rows if isinstance(value, TaskExecutionResult) else tuple(value)
        response_sha = (
            value.raw_response_sha256
            if isinstance(value, TaskExecutionResult)
            else canonical_sha256([dict(row) for row in rows])
        )
        observed += 1
        grouped: dict[str, list[Mapping[str, Any]]] = {}
        snapshot_checks: list[dict[str, Any]] = []
        try:
            for row in rows:
                if row.get("index_code") != plan.config["index"]["index_code"]:
                    raise AlphaFeasibilityDataError("pit_index_code_mismatch", stage="pit")
                snapshot = _compact(_response_date_window(task, row.get("trade_date"), "pit_trade_date"))
                grouped.setdefault(snapshot, []).append(row)
            if not grouped:
                raise AlphaFeasibilityDataError("no_snapshot_in_month", stage="pit")
            valid_candidates: list[dict[str, Any]] = []
            candidate_failures: list[str] = []
            for snapshot in sorted(grouped):
                snapshot_rows = grouped[snapshot]
                try:
                    total, tolerance = _validate_weight_snapshot(
                        snapshot_rows, expected_count=expected_count
                    )
                    count = len(snapshot_rows)
                    adjustment_reason = None
                    if count != expected_count:
                        raise AlphaFeasibilityDataError(
                            "component_count_requires_controlled_adjustment_evidence",
                            stage="pit",
                        )
                    snapshot_checks.append(
                        {
                            "snapshot_date": _iso(_parse_date(snapshot, "snapshot_date")),
                            "component_count": count,
                            "weight_sum": str(total),
                            "weight_tolerance": str(tolerance),
                            "valid": True,
                            "issues": [],
                            "component_count_adjustment_evidence": adjustment_reason,
                        }
                    )
                    valid_candidates.append(
                        {
                            "month": month,
                            "snapshot_date": _iso(_parse_date(snapshot, "snapshot_date")),
                            "weight_sum": str(total),
                            "weight_tolerance": str(tolerance),
                            "members": sorted(
                                (
                                    {
                                        "instrument_id": row["con_code"],
                                        "weight": str(row["weight"]),
                                    }
                                    for row in snapshot_rows
                                ),
                                key=lambda item: item["instrument_id"],
                            ),
                            "source_response_sha256": response_sha,
                            "component_count_adjustment_evidence": adjustment_reason,
                        }
                    )
                except AlphaFeasibilityDataError as exc:
                    candidate_failures.append(exc.code)
                    try:
                        invalid_weights = [
                            _decimal(row["weight"], "weight", minimum=Decimal("0"))
                            for row in snapshot_rows
                        ]
                        invalid_total = sum((item[0] for item in invalid_weights), Decimal("0"))
                        invalid_tolerance = sum(
                            (
                                Decimal("0.5")
                                * (Decimal(10) ** (-_decimal_places(item[1])))
                                for item in invalid_weights
                            ),
                            Decimal("0"),
                        )
                    except AlphaFeasibilityDataError:
                        invalid_total = Decimal("0")
                        invalid_tolerance = Decimal("0")
                    snapshot_checks.append(
                        {
                            "snapshot_date": _iso(_parse_date(snapshot, "snapshot_date")),
                            "component_count": len(snapshot_rows),
                            "weight_sum": str(invalid_total),
                            "weight_tolerance": str(invalid_tolerance),
                            "valid": False,
                            "issues": [exc.code],
                            "component_count_adjustment_evidence": None,
                        }
                    )
            if not valid_candidates:
                raise AlphaFeasibilityDataError(
                    candidate_failures[-1] if candidate_failures else "no_legal_snapshot_in_month",
                    stage="pit",
                )
            for candidate in valid_candidates:
                candidate_date = _parse_date(
                    candidate["snapshot_date"], "selected_snapshot"
                )
                if previous_selected is not None and candidate_date <= previous_selected:
                    raise AlphaFeasibilityDataError(
                        "selected_snapshot_dates_not_strictly_ordered", stage="pit"
                    )
                previous_selected = candidate_date
                selected_snapshots.append(candidate)
            selected = valid_candidates[-1]
            month_details.append(
                {
                    "month": month,
                    "request_artifact_sha256": request_sha,
                    "response_sha256": response_sha,
                    "snapshots": snapshot_checks,
                    "selected_snapshot_date": selected["snapshot_date"],
                    "status": "complete",
                    "issues": [],
                }
            )
        except AlphaFeasibilityDataError as exc:
            blockers.append(f"{month}:{exc.code}")
            month_details.append(
                {
                    "month": month,
                    "request_artifact_sha256": request_sha,
                    "response_sha256": response_sha,
                    "snapshots": snapshot_checks,
                    "selected_snapshot_date": None,
                    "status": "invalid",
                    "issues": [exc.code],
                }
            )

    passed = (
        not blockers
        and observed == len(plan.pit_tasks)
        and len(month_details) == len(plan.pit_tasks)
        and all(item["status"] == "complete" for item in month_details)
        and len(selected_snapshots) >= len(plan.pit_tasks)
    )
    union = tuple(
        sorted(
            {
                member["instrument_id"]
                for snapshot in selected_snapshots
                for member in snapshot["members"]
            }
        )
    ) if passed else ()
    blockers = sorted(set(blockers))
    report = _self_hashed(
        {
            "schema_version": PIT_REPORT_SCHEMA_VERSION,
            "experiment_id": plan.config["experiment_id"],
            "generated_at": _generated_at(generated_at),
            "index_code": plan.config["index"]["index_code"],
            "pit_months_expected": 73,
            "pit_months_observed": observed,
            "monthly_checks": month_details,
            "stage_status": "PIT_MEMBERSHIP_READY" if passed else "BLOCKED_PIT_MEMBERSHIP",
            "terminal_status": None if passed else "BLOCKED_DATA",
            "remaining_blockers": blockers,
            "locked_test_status": dict(LOCKED_TEST_STATUS),
            "locked_test_consumed": False,
        },
        "report_sha256",
    )
    manifest = _self_hashed(
        {
            "schema_version": PIT_MANIFEST_SCHEMA_VERSION,
            "experiment_id": plan.config["experiment_id"],
            "generated_at": report["generated_at"],
            "index_code": plan.config["index"]["index_code"],
            "coverage_start_month": plan.config["index"]["pit_first_month"],
            "coverage_end_month": plan.config["index"]["pit_last_month"],
            "pit_months_expected": 73,
            "pit_months_observed": observed,
            "snapshots": selected_snapshots if passed else [],
            "union_instrument_count": len(union),
            "union_instrument_ids": list(union),
            "stage_status": report["stage_status"],
            "remaining_blockers": blockers,
            "locked_test_status": dict(LOCKED_TEST_STATUS),
            "locked_test_consumed": False,
        },
        "manifest_sha256",
    )
    return PitMembershipResult(
        coverage_report=MappingProxyType(report),
        manifest=MappingProxyType(manifest),
        union_instruments=union,
        passed=passed,
    )


def publish_pit_membership_artifacts(
    output_root: Path | str,
    result: PitMembershipResult,
    *,
    token: str | None = None,
) -> tuple[Path, Path]:
    root = Path(output_root)
    report_path = root / "pit_membership_coverage_report.json"
    manifest_path = root / "pit_membership_manifest.json"
    _publish_or_verify_identical(report_path, result.coverage_report, token=token)
    _publish_or_verify_identical(manifest_path, result.manifest, token=token)
    return report_path, manifest_path


def select_pit_snapshot_on_or_before(
    snapshots: Sequence[Mapping[str, Any]], decision_date: date | str
) -> Mapping[str, Any]:
    """Causal PIT lookup; a future month can never backfill an earlier date."""

    decision = _parse_date(decision_date, "decision_date") if isinstance(decision_date, str) else decision_date
    if not isinstance(decision, date) or decision > ABSOLUTE_CUTOFF:
        raise AlphaFeasibilityDataError("post_cutoff_decision_date", stage="pit")
    all_dates = [
        _parse_date(snapshot.get("snapshot_date"), "snapshot_date")
        for snapshot in snapshots
    ]
    if all_dates != sorted(all_dates) or len(set(all_dates)) != len(all_dates):
        raise AlphaFeasibilityDataError(
            "pit_snapshot_dates_not_strictly_ordered", stage="pit"
        )
    eligible = [
        snapshot
        for snapshot in snapshots
        if _parse_date(snapshot.get("snapshot_date"), "snapshot_date") <= decision
    ]
    if not eligible:
        raise AlphaFeasibilityDataError("no_pit_snapshot_on_or_before_decision_date", stage="pit")
    return eligible[-1]


def _validate_pit_snapshot_month_coverage(
    plan: CollectionPlan,
    snapshots: Sequence[Mapping[str, Any]],
) -> tuple[date, ...]:
    dates = tuple(
        _parse_date(snapshot.get("snapshot_date"), "pit_snapshot_date")
        for snapshot in snapshots
    )
    if dates != tuple(sorted(dates)) or len(set(dates)) != len(dates):
        raise AlphaFeasibilityDataError(
            "pit_snapshot_dates_not_strictly_ordered", stage="pit"
        )
    expected_months = set(
        _month_sequence(
            plan.config["index"]["pit_first_month"],
            plan.config["index"]["pit_last_month"],
        )
    )
    observed_months = {item.strftime("%Y-%m") for item in dates}
    if observed_months != expected_months:
        raise AlphaFeasibilityDataError("pit_snapshot_month_coverage_invalid", stage="pit")
    return dates


def load_normalized_rows(
    output_root: Path | str,
    endpoint: str | None = None,
    *,
    plan_sha256: str | None = None,
    expected_tasks: Sequence[CollectionTask] | None = None,
) -> list[dict[str, Any]]:
    """Load only normalized response artifacts, never unrelated raw data."""

    if endpoint is not None and endpoint not in ALLOWED_ENDPOINTS:
        raise AlphaFeasibilityDataError("endpoint_not_allowed")
    if expected_tasks is not None:
        tasks = tuple(expected_tasks)
        if plan_sha256 is not None and any(task.plan_sha256 != plan_sha256 for task in tasks):
            raise AlphaFeasibilityDataError("expected_task_plan_mismatch")
        store = CreateOnlyTaskStore(output_root)
        verified: list[dict[str, Any]] = []
        for task in tasks:
            if endpoint is not None and task.endpoint != endpoint:
                continue
            if not store.is_complete(task):
                raise AlphaFeasibilityDataError("expected_task_artifact_incomplete")
            store._load_started(task)
            result = store._load_response(task)
            verified.extend(dict(row) for row in result.rows)
        return verified
    # Directory enumeration is not an authorization boundary.  Consumers must
    # supply the exact preflight-built tasks so a self-consistent forged
    # response (including post-cutoff rows) can never become loadable merely by
    # appearing under ``tasks/``.
    raise AlphaFeasibilityDataError("expected_tasks_required_for_plan_bound_load")


def _unique_rows(
    endpoint: str, rows: Sequence[Mapping[str, Any]]
) -> dict[tuple[Any, ...], Mapping[str, Any]]:
    result: dict[tuple[Any, ...], Mapping[str, Any]] = {}
    for row in rows:
        key = _primary_key(endpoint, row)
        if key in result:
            raise AlphaFeasibilityDataError("duplicate_primary_key_across_tasks")
        result[key] = row
    return result


@dataclass(frozen=True, slots=True)
class HistoryCoverageResult:
    report: Mapping[str, Any]
    passed: bool
    trading_dates: tuple[str, ...]


def validate_history_coverage(
    plan: CollectionPlan,
    union_instruments: Sequence[str],
    rows_by_endpoint: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    pit_snapshots: Sequence[Mapping[str, Any]] | None = None,
    generated_at: datetime | None = None,
) -> HistoryCoverageResult:
    """Fail closed on missing history, except same-day ``S`` suspensions."""

    required = {"stock_basic", "trade_cal", "daily", "adj_factor", "suspend_d", "index_daily"}
    if not required.issubset(rows_by_endpoint):
        missing = sorted(required - set(rows_by_endpoint))
        report = {
            "schema_version": "tushare-alpha-feasibility-history-coverage.v1",
            "generated_at": _generated_at(generated_at),
            "stage_status": "BLOCKED_DATA",
            "coverage_start": plan.config["dates"]["signal_warmup_start"],
            "coverage_end": plan.config["dates"]["validation_end"],
            "daily_coverage_status": "BLOCKED_DATA",
            "adj_factor_coverage_status": "BLOCKED_DATA",
            "suspension_coverage_status": "BLOCKED_DATA",
            "benchmark_coverage_status": "BLOCKED_DATA",
            "blockers": [{"reason": "missing_endpoint_artifacts", "endpoints": missing}],
            "locked_test_status": dict(LOCKED_TEST_STATUS),
            "locked_test_consumed": False,
        }
        return HistoryCoverageResult(report=MappingProxyType(report), passed=False, trading_dates=())

    union = tuple(sorted(set(union_instruments)))
    if not union or len(union) != len(union_instruments):
        raise AlphaFeasibilityDataError("invalid_union_for_coverage")
    union_set = set(union)
    blockers: list[dict[str, Any]] = []
    warmup = _parse_date(plan.config["dates"]["signal_warmup_start"], "warmup_start")
    end = _parse_date(plan.config["dates"]["validation_end"], "validation_end")

    calendar_rows = list(rows_by_endpoint["trade_cal"])
    try:
        calendar_index = _unique_rows("trade_cal", calendar_rows)
        calendar_dates = sorted(_parse_date(key[1], "calendar_date") for key in calendar_index)
        expected_calendar_dates: list[date] = []
        cursor = warmup
        while cursor <= end:
            expected_calendar_dates.append(cursor)
            cursor += timedelta(days=1)
        if calendar_dates != expected_calendar_dates:
            raise AlphaFeasibilityDataError("trade_calendar_window_incomplete")
        ordered_calendar = sorted(calendar_rows, key=lambda item: item["cal_date"])
        last_open: date | None = None
        open_values: list[str] = []
        for row in ordered_calendar:
            current = _parse_date(row["cal_date"], "calendar_date")
            pretrade = (
                _parse_date(row["pretrade_date"], "pretrade_date")
                if row.get("pretrade_date")
                else None
            )
            if last_open is not None and pretrade != last_open:
                raise AlphaFeasibilityDataError("trade_calendar_pretrade_mapping_invalid")
            if int(row["is_open"]) == 1:
                open_values.append(_compact(current))
                last_open = current
        open_dates = tuple(open_values)
        if not open_dates or tuple(sorted(set(open_dates))) != open_dates:
            raise AlphaFeasibilityDataError("open_calendar_not_strictly_ordered")
        open_by_year: dict[int, int] = {}
        for item in open_dates:
            session = _parse_date(item, "open_session")
            if session.weekday() >= 5:
                raise AlphaFeasibilityDataError("weekend_marked_as_open_session")
            open_by_year[session.year] = open_by_year.get(session.year, 0) + 1
        for year, minimum in MINIMUM_OPEN_SESSIONS_BY_YEAR.items():
            count = open_by_year.get(year, 0)
            if count < minimum or count > MAXIMUM_OPEN_SESSIONS_PER_YEAR:
                raise AlphaFeasibilityDataError("implausible_annual_open_session_count")
        # A next-session mapping is formed only within the authorized window.
        # The last session is deliberately terminal and never maps into 2024.
        next_session = {open_dates[index]: open_dates[index + 1] for index in range(len(open_dates) - 1)}
        if any(_parse_date(value, "next_session") > ABSOLUTE_CUTOFF for value in next_session.values()):
            raise AlphaFeasibilityDataError("cross_cutoff_next_session")
    except AlphaFeasibilityDataError as exc:
        blockers.append({"reason": exc.code})
        open_dates = ()

    benchmark_status = "COMPLETE"
    if open_dates:
        try:
            benchmark_index = _unique_rows("index_daily", rows_by_endpoint["index_daily"])
            benchmark_dates = {
                key[1] for key, row in benchmark_index.items() if row["ts_code"] == "000906.SH"
            }
            if benchmark_dates != set(open_dates):
                raise AlphaFeasibilityDataError("benchmark_calendar_session_set_mismatch")
            if pit_snapshots is not None:
                pit_dates = _validate_pit_snapshot_month_coverage(plan, pit_snapshots)
                if not {_compact(item) for item in pit_dates}.issubset(benchmark_dates):
                    raise AlphaFeasibilityDataError("pit_snapshot_not_on_controlled_open_session")
        except AlphaFeasibilityDataError as exc:
            benchmark_status = "BLOCKED_DATA"
            blockers.append({"reason": exc.code})
    else:
        benchmark_status = "BLOCKED_DATA"

    basic_rows = list(rows_by_endpoint["stock_basic"])
    basic_by_code: dict[str, Mapping[str, Any]] = {}
    try:
        for row in basic_rows:
            code = _normalized_code(row["ts_code"])
            if code not in union_set:
                raise AlphaFeasibilityDataError("stock_basic_contains_non_union_instrument")
            if code in basic_by_code:
                raise AlphaFeasibilityDataError("duplicate_stock_basic_across_statuses")
            basic_by_code[code] = row
        if set(basic_by_code) != union_set:
            raise AlphaFeasibilityDataError("stock_basic_union_incomplete")
        if pit_snapshots is not None:
            for snapshot in pit_snapshots:
                snapshot_date = _parse_date(
                    snapshot.get("snapshot_date"), "pit_snapshot_date"
                )
                members = snapshot.get("members")
                if not isinstance(members, (list, tuple)):
                    raise AlphaFeasibilityDataError("pit_snapshot_members_invalid")
                for member in members:
                    code = (
                        member.get("instrument_id")
                        if isinstance(member, Mapping)
                        else member
                    )
                    if code not in basic_by_code:
                        raise AlphaFeasibilityDataError("pit_member_missing_stock_basic")
                    if _parse_date(basic_by_code[code]["list_date"], "list_date") > snapshot_date:
                        raise AlphaFeasibilityDataError("pit_member_not_listed_by_snapshot_date")
    except AlphaFeasibilityDataError as exc:
        blockers.append({"reason": exc.code})

    daily_status = "COMPLETE"
    suspension_status = "COMPLETE"
    adj_status = "COMPLETE"
    daily_index: dict[tuple[str, str], Mapping[str, Any]] = {}
    suspend_index: dict[tuple[str, str], Mapping[str, Any]] = {}
    adj_by_code: dict[str, list[tuple[date, Decimal]]] = {code: [] for code in union}
    try:
        daily_index = _unique_rows("daily", rows_by_endpoint["daily"])
        if any(key[0] not in union_set for key in daily_index):
            raise AlphaFeasibilityDataError("daily_contains_non_union_instrument")
        if any(key[1] not in set(open_dates) for key in daily_index):
            raise AlphaFeasibilityDataError("daily_row_not_on_open_session")
    except AlphaFeasibilityDataError as exc:
        daily_status = "BLOCKED_DATA"
        blockers.append({"reason": exc.code})
    try:
        suspend_index = _unique_rows("suspend_d", rows_by_endpoint["suspend_d"])
        if any(key[0] not in union_set for key in suspend_index):
            raise AlphaFeasibilityDataError("suspension_contains_non_union_instrument")
        if any(key[1] not in set(open_dates) for key in suspend_index):
            raise AlphaFeasibilityDataError("suspension_row_not_on_open_session")
    except AlphaFeasibilityDataError as exc:
        suspension_status = "BLOCKED_DATA"
        blockers.append({"reason": exc.code})
    try:
        adj_index = _unique_rows("adj_factor", rows_by_endpoint["adj_factor"])
        if any(key[0] not in union_set for key in adj_index):
            raise AlphaFeasibilityDataError("adj_factor_contains_non_union_instrument")
        if any(key[1] not in set(open_dates) for key in adj_index):
            raise AlphaFeasibilityDataError("adj_factor_row_not_on_open_session")
        for (code, trade_date), row in adj_index.items():
            factor, _ = _decimal(row["adj_factor"], "adj_factor", minimum=Decimal("0"))
            if factor <= 0:
                raise AlphaFeasibilityDataError("nonpositive_adj_factor")
            adj_by_code[code].append((_parse_date(trade_date, "adj_trade_date"), factor))
        for values in adj_by_code.values():
            values.sort(key=lambda item: item[0])
    except AlphaFeasibilityDataError as exc:
        adj_status = "BLOCKED_DATA"
        blockers.append({"reason": exc.code})

    missing_daily: list[tuple[str, str]] = []
    missing_adj: list[tuple[str, str]] = []
    unexplained_missing: list[tuple[str, str]] = []
    if open_dates and set(basic_by_code) == union_set:
        for code in union:
            basic = basic_by_code[code]
            listed = max(warmup, _parse_date(basic["list_date"], "list_date"))
            delisted = (
                min(end, _parse_date(basic["delist_date"], "delist_date"))
                if basic.get("delist_date")
                else end
            )
            factors = adj_by_code.get(code, [])
            factor_index = 0
            latest_factor: Decimal | None = None
            has_prior_economic_value = False
            for trade_date_text in open_dates:
                trade_date = _parse_date(trade_date_text, "trade_date")
                if trade_date < listed or trade_date > delisted:
                    continue
                key = (code, trade_date_text)
                if key not in daily_index:
                    missing_daily.append(key)
                    suspension = suspend_index.get(key)
                    if not _is_full_session_suspension(suspension) or not has_prior_economic_value:
                        unexplained_missing.append(key)
                else:
                    has_prior_economic_value = True
                while factor_index < len(factors) and factors[factor_index][0] <= trade_date:
                    latest_factor = factors[factor_index][1]
                    factor_index += 1
                # The standard Tushare adj_factor endpoint is a daily series.
                # Requiring a same-session observation detects silent middle or
                # tail truncation; the panel still performs a causal as-of join
                # and therefore never consumes a future factor.
                if key in daily_index and key not in adj_index:
                    missing_adj.append(key)
    if unexplained_missing:
        daily_status = "BLOCKED_DATA"
        suspension_status = "BLOCKED_DATA"
        blockers.append(
            {
                "reason": "non_suspension_missing_daily",
                "count": len(unexplained_missing),
                "sample": [list(item) for item in unexplained_missing[:20]],
            }
        )
    if missing_adj:
        adj_status = "BLOCKED_DATA"
        blockers.append(
            {
                "reason": "missing_causal_adj_factor",
                "count": len(missing_adj),
                "sample": [list(item) for item in missing_adj[:20]],
            }
        )
    passed = not blockers
    report = {
        "schema_version": "tushare-alpha-feasibility-history-coverage.v1",
        "experiment_id": plan.config["experiment_id"],
        "generated_at": _generated_at(generated_at),
        "stage_status": "PASS" if passed else "BLOCKED_DATA",
        "coverage_start": _iso(warmup),
        "coverage_end": _iso(end),
        "union_instrument_count": len(union),
        "open_session_count": len(open_dates),
        "daily_coverage_status": daily_status,
        "adj_factor_coverage_status": adj_status,
        "suspension_coverage_status": suspension_status,
        "benchmark_coverage_status": benchmark_status,
        "same_day_suspension_explained_missing_daily_count": len(missing_daily)
        - len(unexplained_missing),
        "non_suspension_missing_daily_count": len(unexplained_missing),
        "missing_causal_adj_factor_count": len(missing_adj),
        "terminal_session_next_session": None,
        "blockers": blockers,
        "locked_test_status": dict(LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
    }
    return HistoryCoverageResult(
        report=MappingProxyType(report), passed=passed, trading_dates=open_dates
    )


def validate_history_coverage_from_store(
    plan: CollectionPlan,
    union_instruments: Sequence[str],
    tasks: Sequence[CollectionTask],
    store: CreateOnlyTaskStore,
    *,
    pit_snapshots: Sequence[Mapping[str, Any]],
    generated_at: datetime | None = None,
) -> HistoryCoverageResult:
    """Bounded-memory history coverage replay, one 3-stock batch at a time."""

    by_endpoint = {
        endpoint: [task for task in tasks if task.endpoint == endpoint]
        for endpoint in ALLOWED_ENDPOINTS
    }

    def load_task(task: CollectionTask) -> TaskExecutionResult:
        if not store.is_complete(task):
            raise AlphaFeasibilityDataError("expected_task_artifact_incomplete")
        store._load_started(task)
        return store._load_response(task)

    if len(by_endpoint["trade_cal"]) != 1 or len(by_endpoint["index_daily"]) != 1:
        raise AlphaFeasibilityDataError("global_history_task_plan_invalid")
    calendar_rows = list(load_task(by_endpoint["trade_cal"][0]).rows)
    benchmark_rows = list(load_task(by_endpoint["index_daily"][0]).rows)
    basic_rows: list[Mapping[str, Any]] = []
    for task in by_endpoint["stock_basic"]:
        basic_rows.extend(load_task(task).rows)
    basic_by_code = {row["ts_code"]: row for row in basic_rows}
    if set(basic_by_code) != set(union_instruments) or len(basic_by_code) != len(basic_rows):
        raise AlphaFeasibilityDataError("stock_basic_union_incomplete")
    adj_by_code = {
        task.scope_instruments[0]: task
        for task in by_endpoint["adj_factor"]
        if len(task.scope_instruments) == 1
    }
    suspend_by_scope = {task.scope_instruments: task for task in by_endpoint["suspend_d"]}
    if set(adj_by_code) != set(union_instruments):
        raise AlphaFeasibilityDataError("adj_factor_task_plan_incomplete")

    union_set = set(union_instruments)

    def pit_snapshots_for_scope(scope: Sequence[str]) -> list[dict[str, Any]]:
        scope_set = set(scope)
        filtered: list[dict[str, Any]] = []
        for snapshot in pit_snapshots:
            if not isinstance(snapshot, Mapping):
                raise AlphaFeasibilityDataError("pit_snapshot_members_invalid")
            members = snapshot.get("members")
            if not isinstance(members, (list, tuple)):
                raise AlphaFeasibilityDataError("pit_snapshot_members_invalid")
            scoped_members: list[Any] = []
            for member in members:
                if isinstance(member, Mapping):
                    code = member.get("instrument_id")
                elif type(member) is str:
                    code = member
                else:
                    raise AlphaFeasibilityDataError("pit_snapshot_members_invalid")
                if code not in union_set:
                    raise AlphaFeasibilityDataError("pit_member_missing_stock_basic")
                if code in scope_set:
                    scoped_members.append(member)
            filtered.append({**dict(snapshot), "members": scoped_members})
        return filtered

    partials: list[HistoryCoverageResult] = []
    for daily_task in by_endpoint["daily"]:
        scope = daily_task.scope_instruments
        suspend_task = suspend_by_scope.get(scope)
        if suspend_task is None:
            raise AlphaFeasibilityDataError("suspension_task_scope_mismatch")
        daily_rows = list(load_task(daily_task).rows)
        adj_rows: list[Mapping[str, Any]] = []
        for code in scope:
            adj_rows.extend(load_task(adj_by_code[code]).rows)
        suspension_rows = list(load_task(suspend_task).rows)
        partials.append(
            validate_history_coverage(
                plan,
                list(scope),
                {
                    "stock_basic": [basic_by_code[code] for code in scope],
                    "trade_cal": calendar_rows,
                    "daily": daily_rows,
                    "adj_factor": adj_rows,
                    "suspend_d": suspension_rows,
                    "index_daily": benchmark_rows,
                },
                pit_snapshots=pit_snapshots_for_scope(scope),
                generated_at=generated_at,
            )
        )
    covered = {code for task in by_endpoint["daily"] for code in task.scope_instruments}
    if covered != set(union_instruments) or not partials:
        raise AlphaFeasibilityDataError("daily_task_plan_incomplete")
    trading_dates = partials[0].trading_dates
    if any(part.trading_dates != trading_dates for part in partials):
        raise AlphaFeasibilityDataError("batch_calendar_replay_mismatch")
    blockers = sorted(
        {
            str(item.get("reason", "history_coverage_incomplete"))
            for part in partials
            for item in part.report.get("blockers", [])
            if isinstance(item, Mapping)
        }
    )
    passed = all(part.passed for part in partials)
    report = {
        "schema_version": "tushare-alpha-feasibility-history-coverage.v1",
        "experiment_id": plan.config["experiment_id"],
        "generated_at": _generated_at(generated_at),
        "stage_status": "PASS" if passed else "BLOCKED_DATA",
        "coverage_start": plan.config["dates"]["signal_warmup_start"],
        "coverage_end": plan.config["dates"]["validation_end"],
        "union_instrument_count": len(union_instruments),
        "open_session_count": len(trading_dates),
        "daily_coverage_status": (
            "COMPLETE"
            if all(part.report["daily_coverage_status"] == "COMPLETE" for part in partials)
            else "BLOCKED_DATA"
        ),
        "adj_factor_coverage_status": (
            "COMPLETE"
            if all(part.report["adj_factor_coverage_status"] == "COMPLETE" for part in partials)
            else "BLOCKED_DATA"
        ),
        "suspension_coverage_status": (
            "COMPLETE"
            if all(part.report["suspension_coverage_status"] == "COMPLETE" for part in partials)
            else "BLOCKED_DATA"
        ),
        "benchmark_coverage_status": (
            "COMPLETE"
            if all(part.report["benchmark_coverage_status"] == "COMPLETE" for part in partials)
            else "BLOCKED_DATA"
        ),
        "same_day_suspension_explained_missing_daily_count": sum(
            int(part.report["same_day_suspension_explained_missing_daily_count"])
            for part in partials
        ),
        "non_suspension_missing_daily_count": sum(
            int(part.report["non_suspension_missing_daily_count"]) for part in partials
        ),
        "missing_causal_adj_factor_count": sum(
            int(part.report["missing_causal_adj_factor_count"]) for part in partials
        ),
        "terminal_session_next_session": None,
        "blockers": [{"reason": item} for item in blockers],
        "locked_test_status": dict(LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
    }
    return HistoryCoverageResult(
        report=MappingProxyType(report), passed=passed, trading_dates=trading_dates
    )


def build_history_manifest(
    plan: CollectionPlan,
    tasks: Sequence[CollectionTask],
    results: Mapping[str, TaskExecutionResult],
    coverage: HistoryCoverageResult,
    *,
    pit_result: PitMembershipResult | None = None,
    request_counts: Mapping[str, int] | None = None,
    generated_at: datetime | None = None,
) -> Mapping[str, Any]:
    expected_by_endpoint = {
        endpoint: sum(task.endpoint == endpoint for task in tasks)
        for endpoint in ALLOWED_ENDPOINTS
    }
    results_by_endpoint: dict[str, list[Mapping[str, Any]]] = {
        endpoint: [] for endpoint in ALLOWED_ENDPOINTS
    }
    completed_by_endpoint = {endpoint: 0 for endpoint in ALLOWED_ENDPOINTS}
    for result in results.values():
        completed_by_endpoint[result.task.endpoint] += 1
        results_by_endpoint[result.task.endpoint].extend(result.rows)

    status_by_endpoint = {
        "trade_cal": "complete" if coverage.passed else "partial",
        "daily": "complete" if coverage.report.get("daily_coverage_status") == "COMPLETE" else "invalid",
        "adj_factor": "complete" if coverage.report.get("adj_factor_coverage_status") == "COMPLETE" else "invalid",
        "suspend_d": "complete" if coverage.report.get("suspension_coverage_status") == "COMPLETE" else "invalid",
        "index_daily": "complete" if coverage.report.get("benchmark_coverage_status") == "COMPLETE" else "invalid",
        "stock_basic": (
            "complete"
            if completed_by_endpoint["stock_basic"] == expected_by_endpoint["stock_basic"]
            else "partial"
        ),
    }
    for endpoint in tuple(status_by_endpoint):
        if completed_by_endpoint[endpoint] == 0:
            status_by_endpoint[endpoint] = "missing"
        elif completed_by_endpoint[endpoint] < expected_by_endpoint[endpoint]:
            status_by_endpoint[endpoint] = "partial"

    endpoint_date_field = {
        "trade_cal": "cal_date",
        "daily": "trade_date",
        "adj_factor": "trade_date",
        "suspend_d": "trade_date",
        "index_daily": "trade_date",
    }

    def dataset(endpoint: str, status: str) -> dict[str, Any]:
        rows = [dict(row) for row in results_by_endpoint[endpoint]]
        date_field = endpoint_date_field.get(endpoint)
        dates = sorted(
            _parse_date(row[date_field], "dataset_date")
            for row in rows
            if date_field is not None and row.get(date_field)
        )
        return {
            "status": status,
            "endpoint": endpoint,
            "record_count": len(rows),
            "coverage_start": _iso(dates[0]) if dates else None,
            "coverage_end": _iso(dates[-1]) if dates else None,
            "normalized_content_sha256": canonical_sha256(rows) if rows else None,
            "issues": [] if status == "complete" else [f"{endpoint}_{status}"],
        }

    pit_ready = pit_result is not None and pit_result.passed
    pit_snapshots = list(pit_result.manifest["snapshots"]) if pit_ready else []
    datasets = {
        "trade_calendar": dataset("trade_cal", status_by_endpoint["trade_cal"]),
        "pit_membership": {
            "status": "complete" if pit_ready else "missing",
            "endpoint": "index_weight",
            "record_count": sum(len(item["members"]) for item in pit_snapshots),
            "coverage_start": (
                plan.config["index"]["pit_first_month"] + "-01" if pit_ready else None
            ),
            "coverage_end": (
                max(item["snapshot_date"] for item in pit_snapshots) if pit_snapshots else None
            ),
            "normalized_content_sha256": canonical_sha256(pit_snapshots) if pit_snapshots else None,
            "issues": [] if pit_ready else ["pit_membership_missing"],
        },
        "security_master": dataset("stock_basic", status_by_endpoint["stock_basic"]),
        "daily": dataset("daily", status_by_endpoint["daily"]),
        "adj_factor": dataset("adj_factor", status_by_endpoint["adj_factor"]),
        "suspension": dataset("suspend_d", status_by_endpoint["suspend_d"]),
        "benchmark": dataset("index_daily", status_by_endpoint["index_daily"]),
    }
    # Security master has point metadata rather than a time-series date field.
    if datasets["security_master"]["status"] == "complete":
        datasets["security_master"]["coverage_start"] = plan.config["dates"]["signal_warmup_start"]
        datasets["security_master"]["coverage_end"] = plan.config["dates"]["validation_end"]
    blockers = sorted(
        {
            str(item.get("reason", "history_coverage_incomplete"))
            for item in coverage.report.get("blockers", [])
            if isinstance(item, Mapping)
        }
    )
    complete = coverage.passed and len(results) == len(tasks) and pit_ready
    counts = (
        {endpoint: int(request_counts.get(endpoint, 0)) for endpoint in ALLOWED_ENDPOINTS}
        if request_counts is not None
        else {
            endpoint: (73 if endpoint == "index_weight" and pit_ready else 0)
            + completed_by_endpoint[endpoint]
            for endpoint in ALLOWED_ENDPOINTS
        }
    )
    manifest = _self_hashed(
        {
            "schema_version": HISTORY_MANIFEST_SCHEMA_VERSION,
            "experiment_id": plan.config["experiment_id"],
            "generated_at": _generated_at(generated_at),
            "coverage_start": plan.config["dates"]["signal_warmup_start"],
            "coverage_end": plan.config["dates"]["validation_end"],
            "actual_tushare_request_count_by_endpoint": counts,
            "pit_months_expected": 73,
            "pit_months_observed": (
                int(pit_result.coverage_report["pit_months_observed"]) if pit_result else 0
            ),
            "union_instrument_count": len(pit_result.union_instruments) if pit_result else 0,
            "datasets": datasets,
            "data_status": "READY" if complete else "BLOCKED_DATA",
            "remaining_blockers": blockers if blockers else ([] if complete else ["history_tasks_incomplete"]),
            "locked_test_status": dict(LOCKED_TEST_STATUS),
            "locked_test_consumed": False,
            "safety": {
                "research_status": "research_alpha_feasibility_only",
                "execution_realism": "INCOMPLETE",
                "paper_eligibility": False,
                "trade_eligibility": False,
                "automatic_order_submission": False,
                "live_supported": False,
            },
        },
        "manifest_sha256",
    )
    return MappingProxyType(manifest)


def build_history_manifest_from_store(
    plan: CollectionPlan,
    tasks: Sequence[CollectionTask],
    store: CreateOnlyTaskStore,
    coverage: HistoryCoverageResult,
    *,
    pit_result: PitMembershipResult,
    request_counts: Mapping[str, int],
    generated_at: datetime | None = None,
) -> Mapping[str, Any]:
    """Build a Merkle-style dataset manifest without retaining all rows."""

    summaries: dict[str, list[dict[str, Any]]] = {endpoint: [] for endpoint in ALLOWED_ENDPOINTS}
    for task in tasks:
        if not store.is_complete(task):
            continue
        store._load_started(task)
        result = store._load_response(task)
        summaries[task.endpoint].append(
            {
                "task_id": task.task_id,
                "row_count": len(result.rows),
                "normalized_rows_sha256": canonical_sha256([dict(row) for row in result.rows]),
                "raw_response_sha256": result.raw_response_sha256,
                "wire_response_sha256": result.wire_response_sha256,
                "response_artifact_sha256": result.response_artifact_sha256,
                "isolated_future_delist_date_count": (
                    result.isolated_future_delist_date_count
                ),
                "isolated_non_union_row_count": result.isolated_non_union_row_count,
            }
        )
        del result

    expected = {
        endpoint: sum(task.endpoint == endpoint for task in tasks)
        for endpoint in ALLOWED_ENDPOINTS
    }
    coverage_status = {
        "trade_cal": "COMPLETE" if coverage.trading_dates else "BLOCKED_DATA",
        "stock_basic": "COMPLETE" if len(summaries["stock_basic"]) == expected["stock_basic"] else "BLOCKED_DATA",
        "daily": coverage.report.get("daily_coverage_status"),
        "adj_factor": coverage.report.get("adj_factor_coverage_status"),
        "suspend_d": coverage.report.get("suspension_coverage_status"),
        "index_daily": coverage.report.get("benchmark_coverage_status"),
    }

    def dataset(endpoint: str) -> dict[str, Any]:
        entries = summaries[endpoint]
        if not entries:
            status = "missing"
        elif len(entries) < expected[endpoint]:
            status = "partial"
        elif coverage_status[endpoint] == "COMPLETE":
            status = "complete"
        else:
            status = "invalid"
        return {
            "status": status,
            "endpoint": endpoint,
            "record_count": sum(int(item["row_count"]) for item in entries),
            "coverage_start": plan.config["dates"]["signal_warmup_start"] if entries else None,
            "coverage_end": plan.config["dates"]["validation_end"] if entries else None,
            "normalized_content_sha256": canonical_sha256(entries) if entries else None,
            "issues": [] if status == "complete" else [f"{endpoint}_{status}"],
        }

    pit_snapshots = list(pit_result.manifest["snapshots"])
    datasets = {
        "trade_calendar": dataset("trade_cal"),
        "pit_membership": {
            "status": "complete" if pit_result.passed else "missing",
            "endpoint": "index_weight",
            "record_count": sum(len(item["members"]) for item in pit_snapshots),
            "coverage_start": min(item["snapshot_date"] for item in pit_snapshots) if pit_snapshots else None,
            "coverage_end": max(item["snapshot_date"] for item in pit_snapshots) if pit_snapshots else None,
            "normalized_content_sha256": canonical_sha256(pit_snapshots) if pit_snapshots else None,
            "issues": [] if pit_result.passed else ["pit_membership_missing"],
        },
        "security_master": dataset("stock_basic"),
        "daily": dataset("daily"),
        "adj_factor": dataset("adj_factor"),
        "suspension": dataset("suspend_d"),
        "benchmark": dataset("index_daily"),
    }
    blockers = sorted(
        {
            str(item.get("reason", "history_coverage_incomplete"))
            for item in coverage.report.get("blockers", [])
            if isinstance(item, Mapping)
        }
    )
    complete = coverage.passed and all(
        len(summaries[endpoint]) == expected[endpoint]
        for endpoint in ("trade_cal", "stock_basic", "daily", "adj_factor", "suspend_d", "index_daily")
    )
    return MappingProxyType(
        _self_hashed(
            {
                "schema_version": HISTORY_MANIFEST_SCHEMA_VERSION,
                "experiment_id": plan.config["experiment_id"],
                "generated_at": _generated_at(generated_at),
                "coverage_start": plan.config["dates"]["signal_warmup_start"],
                "coverage_end": plan.config["dates"]["validation_end"],
                "actual_tushare_request_count_by_endpoint": {
                    endpoint: int(request_counts.get(endpoint, 0)) for endpoint in ALLOWED_ENDPOINTS
                },
                "pit_months_expected": 73,
                "pit_months_observed": int(pit_result.coverage_report["pit_months_observed"]),
                "union_instrument_count": len(pit_result.union_instruments),
                "datasets": datasets,
                "data_status": "READY" if complete else "BLOCKED_DATA",
                "remaining_blockers": blockers if blockers else ([] if complete else ["history_tasks_incomplete"]),
                "locked_test_status": dict(LOCKED_TEST_STATUS),
                "locked_test_consumed": False,
                "safety": {
                    "research_status": "research_alpha_feasibility_only",
                    "execution_realism": "INCOMPLETE",
                    "paper_eligibility": False,
                    "trade_eligibility": False,
                    "automatic_order_submission": False,
                    "live_supported": False,
                },
            },
            "manifest_sha256",
        )
    )


def publish_history_artifacts(
    output_root: Path | str,
    coverage: HistoryCoverageResult,
    manifest: Mapping[str, Any],
    *,
    token: str | None = None,
) -> tuple[Path, Path]:
    root = Path(output_root)
    coverage_path = root / "history_coverage_report.json"
    manifest_path = root / "history_manifest.json"
    _publish_or_verify_identical(coverage_path, coverage.report, token=token)
    _publish_or_verify_identical(manifest_path, manifest, token=token)
    return coverage_path, manifest_path


def build_total_return_panel(
    trading_dates: Iterable[date | str],
    stock_basic_rows: Sequence[Mapping[str, Any]],
    daily_rows: Sequence[Mapping[str, Any]],
    adj_factor_rows: Sequence[Mapping[str, Any]],
    suspension_rows: Sequence[Mapping[str, Any]],
    *,
    coverage_start: date | str | None = None,
    coverage_end: date | str | None = None,
) -> list[dict[str, Any]]:
    """Build causal signal values from ``raw_close * as-of adj_factor``.

    A suspended open session carries the previous economic value only with a
    same-day ``suspend_type == 'S'`` record.  All other missing daily bars fail
    closed.  ``raw_open`` and ``raw_close`` remain separate from the adjusted
    signal ``close`` and ``high`` fields consumed by the research engine.
    """

    parsed_dates = tuple(
        item if isinstance(item, date) else _parse_date(item, "trading_date")
        for item in trading_dates
    )
    if not parsed_dates or tuple(sorted(set(parsed_dates))) != parsed_dates:
        raise AlphaFeasibilityDataError("trading_dates_not_strictly_ordered")
    start = (
        parsed_dates[0]
        if coverage_start is None
        else coverage_start
        if isinstance(coverage_start, date)
        else _parse_date(coverage_start, "coverage_start")
    )
    end = (
        parsed_dates[-1]
        if coverage_end is None
        else coverage_end
        if isinstance(coverage_end, date)
        else _parse_date(coverage_end, "coverage_end")
    )
    if start > end or end > ABSOLUTE_CUTOFF:
        raise AlphaFeasibilityDataError("panel_date_boundary_invalid")

    basics: dict[str, Mapping[str, Any]] = {}
    for row in stock_basic_rows:
        code = _normalized_code(row["ts_code"])
        if code in basics:
            raise AlphaFeasibilityDataError("duplicate_stock_basic_across_statuses")
        basics[code] = row
    daily = _unique_rows("daily", daily_rows)
    suspensions = _unique_rows("suspend_d", suspension_rows)
    adj_index = _unique_rows("adj_factor", adj_factor_rows)
    if any(code not in basics for code, _trade_date in daily):
        raise AlphaFeasibilityDataError("daily_contains_non_panel_instrument")
    if any(code not in basics for code, _trade_date in suspensions):
        raise AlphaFeasibilityDataError("suspension_contains_non_panel_instrument")
    for endpoint_rows, date_field in (
        (daily_rows, "trade_date"),
        (adj_factor_rows, "trade_date"),
        (suspension_rows, "trade_date"),
    ):
        if any(_parse_date(row[date_field], date_field) > ABSOLUTE_CUTOFF for row in endpoint_rows):
            raise AlphaFeasibilityDataError("post_cutoff_panel_input")
    factors: dict[str, list[tuple[date, Decimal, str]]] = {code: [] for code in basics}
    for (code, trade_date), row in adj_index.items():
        if code not in basics:
            raise AlphaFeasibilityDataError("adj_factor_contains_non_panel_instrument")
        factor, text = _decimal(row["adj_factor"], "adj_factor", minimum=Decimal("0"))
        if factor <= 0:
            raise AlphaFeasibilityDataError("nonpositive_adj_factor")
        factors[code].append((_parse_date(trade_date, "adj_trade_date"), factor, text))
    for values in factors.values():
        values.sort(key=lambda item: item[0])

    panel: list[dict[str, Any]] = []
    for code in sorted(basics):
        basic = basics[code]
        listed = max(start, _parse_date(basic["list_date"], "list_date"))
        delisted = (
            min(end, _parse_date(basic["delist_date"], "delist_date"))
            if basic.get("delist_date")
            else end
        )
        factor_values = factors[code]
        factor_cursor = 0
        latest_factor: tuple[date, Decimal, str] | None = None
        previous_economic_value: Decimal | None = None
        for trading_date in parsed_dates:
            if trading_date < listed or trading_date > delisted:
                continue
            while (
                factor_cursor < len(factor_values)
                and factor_values[factor_cursor][0] <= trading_date
            ):
                latest_factor = factor_values[factor_cursor]
                factor_cursor += 1
            key = (code, _compact(trading_date))
            bar = daily.get(key)
            suspension = suspensions.get(key)
            if _is_full_session_suspension(suspension):
                if previous_economic_value is None:
                    raise AlphaFeasibilityDataError("suspension_without_prior_economic_value")
                raw_values: dict[str, str | None] = {
                    "open": None,
                    "high": None,
                    "low": None,
                    "close": None,
                }
                if bar is not None:
                    # Preserve a simultaneously returned provider bar as raw
                    # evidence, but never consume it as the suspension-day
                    # signal value.
                    for field in raw_values:
                        _number, raw_values[field] = _decimal(
                            bar[field], field, minimum=Decimal("0")
                        )
                economic_value = previous_economic_value
                adjusted_high = previous_economic_value
                row = {
                    "trading_date": _iso(trading_date),
                    "trade_date": _compact(trading_date),
                    "ts_code": code,
                    "instrument_id": code,
                    "raw_open": raw_values["open"],
                    "raw_high": raw_values["high"],
                    "raw_low": raw_values["low"],
                    "raw_close": raw_values["close"],
                    "adj_factor": latest_factor[2] if latest_factor is not None else None,
                    "adj_factor_asof_date": (
                        _iso(latest_factor[0]) if latest_factor is not None else None
                    ),
                    "close": str(economic_value),
                    "high": str(adjusted_high),
                    "open": str(economic_value),
                    "adjusted_value": str(economic_value),
                    "daily_total_return": "0",
                    "is_suspended_carry": True,
                    "suspend_type": "S",
                }
            elif bar is None:
                raise AlphaFeasibilityDataError("non_suspension_missing_daily")
            else:
                if latest_factor is None:
                    raise AlphaFeasibilityDataError("missing_causal_adj_factor")
                raw_close, raw_close_text = _decimal(
                    bar["close"], "close", minimum=Decimal("0")
                )
                raw_high, raw_high_text = _decimal(
                    bar["high"], "high", minimum=Decimal("0")
                )
                raw_open, raw_open_text = _decimal(
                    bar["open"], "open", minimum=Decimal("0")
                )
                raw_low, raw_low_text = _decimal(
                    bar["low"], "low", minimum=Decimal("0")
                )
                economic_value = raw_close * latest_factor[1]
                adjusted_high = raw_high * latest_factor[1]
                adjusted_open = raw_open * latest_factor[1]
                daily_return = (
                    None
                    if previous_economic_value is None
                    else economic_value / previous_economic_value - Decimal("1")
                )
                row = {
                    "trading_date": _iso(trading_date),
                    "trade_date": _compact(trading_date),
                    "ts_code": code,
                    "instrument_id": code,
                    "raw_open": raw_open_text,
                    "raw_high": raw_high_text,
                    "raw_low": raw_low_text,
                    "raw_close": raw_close_text,
                    "adj_factor": latest_factor[2],
                    "adj_factor_asof_date": _iso(latest_factor[0]),
                    "close": str(economic_value),
                    "high": str(adjusted_high),
                    "open": str(adjusted_open),
                    "adjusted_value": str(economic_value),
                    "daily_total_return": None if daily_return is None else str(daily_return),
                    "is_suspended_carry": False,
                    "suspend_type": None,
                }
            panel.append(row)
            previous_economic_value = economic_value
    panel.sort(key=lambda row: (row["trading_date"], row["ts_code"]))
    return panel


def _load_json_object(path: Path, code: str) -> Mapping[str, Any]:
    try:
        value = strict_json_loads(path.read_bytes(), label="artifact")
    except OSError as exc:
        raise AlphaFeasibilityDataError(code) from exc
    if not isinstance(value, Mapping):
        raise AlphaFeasibilityDataError(code)
    return value


def _load_existing_pit_result(
    output_root: Path, plan: CollectionPlan
) -> PitMembershipResult | None:
    report_path = output_root / "pit_membership_coverage_report.json"
    manifest_path = output_root / "pit_membership_manifest.json"
    if not report_path.exists() and not manifest_path.exists():
        return None
    if not report_path.is_file() or not manifest_path.is_file():
        raise AlphaFeasibilityDataError("incomplete_pit_artifact_pair", stage="pit")
    report = _load_json_object(report_path, "pit_report_unreadable")
    manifest = _load_json_object(manifest_path, "pit_manifest_unreadable")
    unsigned_report = dict(report)
    report_hash = unsigned_report.pop("report_sha256", None)
    unsigned_manifest = dict(manifest)
    manifest_hash = unsigned_manifest.pop("manifest_sha256", None)
    if (
        report.get("schema_version") != PIT_REPORT_SCHEMA_VERSION
        or manifest.get("schema_version") != PIT_MANIFEST_SCHEMA_VERSION
        or report_hash != canonical_sha256(unsigned_report)
        or manifest_hash != canonical_sha256(unsigned_manifest)
        or report.get("locked_test_status") != dict(LOCKED_TEST_STATUS)
        or manifest.get("locked_test_status") != dict(LOCKED_TEST_STATUS)
        or report.get("locked_test_consumed") is not False
        or manifest.get("locked_test_consumed") is not False
    ):
        raise AlphaFeasibilityDataError("pit_artifact_verification_failed", stage="pit")
    passed = (
        report.get("stage_status") == "PIT_MEMBERSHIP_READY"
        and manifest.get("stage_status") == "PIT_MEMBERSHIP_READY"
    )
    snapshots = manifest.get("snapshots")
    if not isinstance(snapshots, list):
        raise AlphaFeasibilityDataError("pit_snapshot_manifest_invalid", stage="pit")
    union = tuple(
        sorted(
            {
                member.get("instrument_id")
                for item in snapshots
                for member in item.get("members", [])
                if isinstance(member, Mapping)
            }
        )
    )
    if passed:
        _validate_pit_snapshot_month_coverage(plan, snapshots)
        if (
            len(union) != manifest.get("union_instrument_count")
            or list(union) != manifest.get("union_instrument_ids")
        ):
            raise AlphaFeasibilityDataError("pit_union_manifest_invalid", stage="pit")
    if passed:
        generated_text = report.get("generated_at")
        if type(generated_text) is not str:
            raise AlphaFeasibilityDataError("pit_generated_at_invalid", stage="pit")
        try:
            generated = datetime.fromisoformat(generated_text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AlphaFeasibilityDataError("pit_generated_at_invalid", stage="pit") from exc
        if generated.tzinfo is None:
            raise AlphaFeasibilityDataError("pit_generated_at_invalid", stage="pit")
        store = CreateOnlyTaskStore(output_root)
        replayed = _completed_results(store, plan.pit_tasks)
        if len(replayed) != 73:
            raise AlphaFeasibilityDataError("pit_task_evidence_incomplete", stage="pit")
        rebuilt = build_pit_membership_artifacts(
            plan,
            replayed,
            generated_at=generated,
        )
        if (
            dict(rebuilt.coverage_report) != dict(report)
            or dict(rebuilt.manifest) != dict(manifest)
            or rebuilt.union_instruments != union
        ):
            raise AlphaFeasibilityDataError("pit_artifact_replay_mismatch", stage="pit")
    return PitMembershipResult(
        coverage_report=MappingProxyType(dict(report)),
        manifest=MappingProxyType(dict(manifest)),
        union_instruments=union if passed else (),
        passed=passed,
    )


def _completed_results(
    store: CreateOnlyTaskStore, tasks: Sequence[CollectionTask]
) -> dict[str, TaskExecutionResult]:
    completed: dict[str, TaskExecutionResult] = {}
    for task in tasks:
        if store.is_complete(task):
            store._load_started(task)
            completed[task.task_id] = store._load_response(task)
    return completed


def _backfill_lineage(
    plan: CollectionPlan,
    *,
    pit_result: PitMembershipResult | None = None,
    history_manifest: Mapping[str, Any] | None = None,
) -> dict[str, str | None]:
    pit_hash = pit_result.manifest.get("manifest_sha256") if pit_result is not None else None
    history_hash = history_manifest.get("manifest_sha256") if history_manifest is not None else None
    for value, code in (
        (plan.plan_sha256, "invalid_collection_plan_sha256"),
        (plan.config_sha256, "invalid_experiment_config_sha256"),
        (pit_hash, "invalid_pit_membership_manifest_sha256"),
        (history_hash, "invalid_history_manifest_sha256"),
    ):
        if value is not None and (type(value) is not str or _SHA256.fullmatch(value) is None):
            raise AlphaFeasibilityDataError(code)
    return {
        "collection_plan_sha256": plan.plan_sha256,
        "experiment_config_sha256": plan.config_sha256,
        "pit_membership_manifest_sha256": pit_hash,
        "history_manifest_sha256": history_hash,
    }


def _blocked_summary(
    plan: CollectionPlan,
    output_root: Path,
    *,
    stage_status: str,
    blocker: str,
    pit_result: PitMembershipResult | None = None,
    coverage: HistoryCoverageResult | None = None,
    history_manifest: Mapping[str, Any] | None = None,
    expected_tasks: Sequence[CollectionTask] | None = None,
) -> dict[str, Any]:
    artifact_generated_at = (
        coverage.report.get("generated_at")
        if coverage is not None
        else pit_result.coverage_report.get("generated_at")
        if pit_result is not None
        else None
    )
    return {
        "schema_version": "tushare-alpha-feasibility-backfill-result.v1",
        "experiment_id": plan.config["experiment_id"],
        "stage_status": stage_status,
        "terminal_status": "BLOCKED_DATA",
        "generated_at": artifact_generated_at,
        **_backfill_lineage(
            plan,
            pit_result=pit_result,
            history_manifest=history_manifest,
        ),
        "actual_tushare_request_count_by_endpoint": actual_tushare_request_count_by_endpoint(
            output_root,
            expected_tasks,
            plan_sha256=plan.plan_sha256,
        ),
        "request_count_semantics": "durable_network_call_started_claim",
        "coverage_start": plan.config["dates"]["signal_warmup_start"],
        "coverage_end": plan.config["dates"]["validation_end"],
        "pit_months_expected": 73,
        "pit_months_observed": (
            pit_result.coverage_report.get("pit_months_observed", 0) if pit_result else 0
        ),
        "union_instrument_count": len(pit_result.union_instruments) if pit_result else 0,
        "daily_coverage_status": (
            coverage.report.get("daily_coverage_status", "BLOCKED_DATA")
            if coverage
            else "BLOCKED_DATA"
        ),
        "adj_factor_coverage_status": (
            coverage.report.get("adj_factor_coverage_status", "BLOCKED_DATA")
            if coverage
            else "BLOCKED_DATA"
        ),
        "suspension_coverage_status": (
            coverage.report.get("suspension_coverage_status", "BLOCKED_DATA")
            if coverage
            else "BLOCKED_DATA"
        ),
        "benchmark_coverage_status": (
            coverage.report.get("benchmark_coverage_status", "BLOCKED_DATA")
            if coverage
            else "BLOCKED_DATA"
        ),
        "remaining_blockers": [blocker],
        "locked_test_status": dict(LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
        "execution_realism": "INCOMPLETE",
        "trade_eligibility": False,
    }


def _blocked_history_coverage(
    plan: CollectionPlan,
    union_count: int,
    blocker: str,
    generated_at: datetime,
) -> HistoryCoverageResult:
    report = {
        "schema_version": "tushare-alpha-feasibility-history-coverage.v1",
        "experiment_id": plan.config["experiment_id"],
        "generated_at": _generated_at(generated_at),
        "stage_status": "BLOCKED_DATA",
        "coverage_start": plan.config["dates"]["signal_warmup_start"],
        "coverage_end": plan.config["dates"]["validation_end"],
        "union_instrument_count": union_count,
        "open_session_count": 0,
        "daily_coverage_status": "BLOCKED_DATA",
        "adj_factor_coverage_status": "BLOCKED_DATA",
        "suspension_coverage_status": "BLOCKED_DATA",
        "benchmark_coverage_status": "BLOCKED_DATA",
        "same_day_suspension_explained_missing_daily_count": 0,
        "non_suspension_missing_daily_count": 0,
        "missing_causal_adj_factor_count": 0,
        "terminal_session_next_session": None,
        "blockers": [{"reason": blocker}],
        "locked_test_status": dict(LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
    }
    return HistoryCoverageResult(report=MappingProxyType(report), passed=False, trading_dates=())


def run_backfill(
    config_path: Path | str,
    output_root: Path | str,
    token: str,
    transport: TushareTransport | None = None,
    generated_at: datetime | None = None,
    *,
    repository_root: Path | str = REPOSITORY_ROOT,
    adjustment_evidence: Mapping[str, Any] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Run PIT first and plan stock history only after all 73 months pass.

    This is a thin data-stage orchestrator.  It never runs the Alpha engine,
    Development, Validation, Paper, broker, account, or Locked Test paths.
    """

    # The full config, source allowlist, frozen code hashes, 73 PIT requests,
    # and all hard date bounds are proven before the credential is inspected.
    plan = load_config_and_build_plan(config_path, repository_root=repository_root)
    if adjustment_evidence is not None:
        raise AlphaFeasibilityDataError(
            "controlled_adjustment_evidence_not_supported", stage="pit"
        )
    safe_token = _validate_token(token)
    root = Path(output_root)
    store = CreateOnlyTaskStore(root)
    source = plan.config["source"]
    active_transport = transport or HttpsTushareTransport()
    timestamp = generated_at or datetime.now(timezone.utc)
    interval, _ = _decimal(
        source["minimum_request_interval_seconds"],
        "minimum_request_interval_seconds",
        minimum=Decimal("0"),
    )

    pit_result = _load_existing_pit_result(root, plan)
    if pit_result is None:
        try:
            pit_executions = execute_tasks(
                plan.pit_tasks,
                store=store,
                token=safe_token,
                transport=active_transport,
                timeout_seconds=source["request_timeout_seconds"],
                maximum_response_bytes=source["maximum_response_bytes"],
                minimum_request_interval_seconds=interval,
                sleeper=sleeper,
                monotonic=monotonic,
            )
            pit_results = {result.task.task_id: result for result in pit_executions}
        except AlphaFeasibilityDataError as exc:
            pit_results = _completed_results(store, plan.pit_tasks)
            pit_result = build_pit_membership_artifacts(
                plan,
                pit_results,
                adjustment_evidence=adjustment_evidence,
                generated_at=timestamp,
            )
            publish_pit_membership_artifacts(root, pit_result, token=safe_token)
            return _blocked_summary(
                plan,
                root,
                stage_status="BLOCKED_PIT_MEMBERSHIP",
                blocker=exc.code,
                pit_result=pit_result,
                expected_tasks=plan.pit_tasks,
            )
        pit_result = build_pit_membership_artifacts(
            plan,
            pit_results,
            adjustment_evidence=adjustment_evidence,
            generated_at=timestamp,
        )
        publish_pit_membership_artifacts(root, pit_result, token=safe_token)
    if not pit_result.passed:
        return _blocked_summary(
            plan,
            root,
            stage_status="BLOCKED_PIT_MEMBERSHIP",
            blocker="pit_membership_incomplete",
            pit_result=pit_result,
            expected_tasks=plan.pit_tasks,
        )

    history_tasks = build_history_plan(plan, pit_result.union_instruments)
    existing_history_coverage = root / "history_coverage_report.json"
    existing_history_manifest = root / "history_manifest.json"
    if existing_history_coverage.exists() or existing_history_manifest.exists():
        if not existing_history_coverage.is_file() or not existing_history_manifest.is_file():
            raise AlphaFeasibilityDataError("incomplete_history_artifact_pair")
        existing_coverage_value = _load_json_object(
            existing_history_coverage, "history_coverage_unavailable"
        )
        generated_text = existing_coverage_value.get("generated_at")
        if type(generated_text) is not str:
            raise AlphaFeasibilityDataError("history_generated_at_invalid")
        try:
            existing_timestamp = datetime.fromisoformat(generated_text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AlphaFeasibilityDataError("history_generated_at_invalid") from exc
        if existing_timestamp.tzinfo is None:
            raise AlphaFeasibilityDataError("history_generated_at_invalid")
        timestamp = existing_timestamp
    try:
        execute_tasks_bounded(
            history_tasks,
            store=store,
            token=safe_token,
            transport=active_transport,
            timeout_seconds=source["request_timeout_seconds"],
            maximum_response_bytes=source["maximum_response_bytes"],
            minimum_request_interval_seconds=interval,
            sleeper=sleeper,
            monotonic=monotonic,
        )
        blocker: str | None = None
    except AlphaFeasibilityDataError as exc:
        blocker = exc.code
    if blocker is None:
        try:
            coverage = validate_history_coverage_from_store(
                plan,
                pit_result.union_instruments,
                history_tasks,
                store,
                pit_snapshots=pit_result.manifest["snapshots"],
                generated_at=timestamp,
            )
        except AlphaFeasibilityDataError as exc:
            blocker = exc.code
            coverage = _blocked_history_coverage(
                plan, len(pit_result.union_instruments), blocker, timestamp
            )
    else:
        coverage = _blocked_history_coverage(
            plan, len(pit_result.union_instruments), blocker, timestamp
        )
    history_manifest = build_history_manifest_from_store(
        plan,
        history_tasks,
        store,
        coverage,
        pit_result=pit_result,
        request_counts=actual_tushare_request_count_by_endpoint(
            root,
            (*plan.pit_tasks, *history_tasks),
            plan_sha256=plan.plan_sha256,
        ),
        generated_at=timestamp,
    )
    publish_history_artifacts(root, coverage, history_manifest, token=safe_token)
    if blocker is not None or not coverage.passed:
        return _blocked_summary(
            plan,
            root,
            stage_status="BLOCKED_DATA",
            blocker=blocker or "history_coverage_incomplete",
            pit_result=pit_result,
            coverage=coverage,
            history_manifest=history_manifest,
            expected_tasks=(*plan.pit_tasks, *history_tasks),
        )
    return {
        "schema_version": "tushare-alpha-feasibility-backfill-result.v1",
        "experiment_id": plan.config["experiment_id"],
        "stage_status": "DATA_READY_FOR_ALPHA_FEASIBILITY",
        "terminal_status": None,
        "generated_at": coverage.report["generated_at"],
        **_backfill_lineage(
            plan,
            pit_result=pit_result,
            history_manifest=history_manifest,
        ),
        "actual_tushare_request_count_by_endpoint": actual_tushare_request_count_by_endpoint(
            root,
            (*plan.pit_tasks, *history_tasks),
            plan_sha256=plan.plan_sha256,
        ),
        "request_count_semantics": "durable_network_call_started_claim",
        "coverage_start": coverage.report["coverage_start"],
        "coverage_end": coverage.report["coverage_end"],
        "pit_months_expected": 73,
        "pit_months_observed": pit_result.coverage_report["pit_months_observed"],
        "union_instrument_count": len(pit_result.union_instruments),
        "daily_coverage_status": coverage.report["daily_coverage_status"],
        "adj_factor_coverage_status": coverage.report["adj_factor_coverage_status"],
        "suspension_coverage_status": coverage.report["suspension_coverage_status"],
        "benchmark_coverage_status": coverage.report["benchmark_coverage_status"],
        "remaining_blockers": [],
        "locked_test_status": dict(LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
        "execution_realism": "INCOMPLETE",
        "trade_eligibility": False,
    }


def run_backfill_from_environment(
    config_path: Path | str,
    output_root: Path | str,
    transport: TushareTransport | None = None,
    generated_at: datetime | None = None,
    *,
    repository_root: Path | str = REPOSITORY_ROOT,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Environment wrapper whose credential lookup happens after preflight."""

    plan = load_config_and_build_plan(config_path, repository_root=repository_root)
    environment = os.environ if environ is None else environ
    variable = plan.config["source"]["token_environment_variable"]
    token = environment.get(variable)
    return run_backfill(
        config_path,
        output_root,
        _validate_token(token),
        transport=transport,
        generated_at=generated_at,
        repository_root=repository_root,
    )


def load_feasibility_inputs(
    output_root: Path | str,
    config_path: Path | str = DEFAULT_CONFIG_PATH,
    *,
    repository_root: Path | str = REPOSITORY_ROOT,
) -> dict[str, Any]:
    """Load verified, cutoff-safe dictionaries for the independent engine."""

    plan = load_config_and_build_plan(config_path, repository_root=repository_root)
    root = Path(output_root)
    pit = _load_existing_pit_result(root, plan)
    if pit is None or not pit.passed:
        raise AlphaFeasibilityDataError("pit_membership_not_admitted", stage="pit")
    manifest = _load_json_object(root / "history_manifest.json", "history_manifest_unavailable")
    coverage_artifact = _load_json_object(
        root / "history_coverage_report.json", "history_coverage_unavailable"
    )
    try:
        validate_json_schema(
            manifest,
            Path(repository_root) / "schemas" / "tushare_alpha_feasibility_manifest.v1.json",
        )
    except SchemaValidationError as exc:
        raise AlphaFeasibilityDataError("history_manifest_schema_invalid") from exc
    unsigned_manifest = dict(manifest)
    declared_manifest_hash = unsigned_manifest.pop("manifest_sha256", None)
    if (
        manifest.get("schema_version") != HISTORY_MANIFEST_SCHEMA_VERSION
        or manifest.get("data_status") != "READY"
        or declared_manifest_hash != canonical_sha256(unsigned_manifest)
        or manifest.get("locked_test_status") != dict(LOCKED_TEST_STATUS)
        or manifest.get("locked_test_consumed") is not False
    ):
        raise AlphaFeasibilityDataError("history_manifest_verification_failed")
    expected_history_tasks = build_history_plan(plan, pit.union_instruments)
    store = CreateOnlyTaskStore(root)
    generated_text = coverage_artifact.get("generated_at")
    if type(generated_text) is not str:
        raise AlphaFeasibilityDataError("history_generated_at_invalid")
    try:
        coverage_generated_at = datetime.fromisoformat(generated_text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AlphaFeasibilityDataError("history_generated_at_invalid") from exc
    coverage = validate_history_coverage_from_store(
        plan,
        pit.union_instruments,
        expected_history_tasks,
        store,
        pit_snapshots=pit.manifest["snapshots"],
        generated_at=coverage_generated_at,
    )
    if not coverage.passed or dict(coverage.report) != dict(coverage_artifact):
        raise AlphaFeasibilityDataError("history_coverage_replay_failed")
    rebuilt_manifest = build_history_manifest_from_store(
        plan,
        expected_history_tasks,
        store,
        coverage,
        pit_result=pit,
        request_counts=actual_tushare_request_count_by_endpoint(
            root,
            (*plan.pit_tasks, *expected_history_tasks),
            plan_sha256=plan.plan_sha256,
        ),
        generated_at=coverage_generated_at,
    )
    if dict(rebuilt_manifest) != dict(manifest):
        raise AlphaFeasibilityDataError("history_manifest_replay_failed")

    by_endpoint = {
        endpoint: [task for task in expected_history_tasks if task.endpoint == endpoint]
        for endpoint in ALLOWED_ENDPOINTS
    }
    basic_rows: list[Mapping[str, Any]] = []
    for task in by_endpoint["stock_basic"]:
        basic_rows.extend(store._load_response(task).rows)
    basic_by_code = {row["ts_code"]: row for row in basic_rows}
    adj_by_code = {task.scope_instruments[0]: task for task in by_endpoint["adj_factor"]}
    suspend_by_scope = {task.scope_instruments: task for task in by_endpoint["suspend_d"]}

    def signal_rows() -> Iterable[dict[str, Any]]:
        for daily_task in by_endpoint["daily"]:
            scope = daily_task.scope_instruments
            daily_rows = store._load_response(daily_task).rows
            adj_rows: list[Mapping[str, Any]] = []
            for code in scope:
                adj_rows.extend(store._load_response(adj_by_code[code]).rows)
            suspension_rows = store._load_response(suspend_by_scope[scope]).rows
            batch = build_total_return_panel(
                coverage.trading_dates,
                [basic_by_code[code] for code in scope],
                daily_rows,
                adj_rows,
                suspension_rows,
                coverage_start=plan.config["dates"]["signal_warmup_start"],
                coverage_end=plan.config["dates"]["validation_end"],
            )
            yield from batch

    benchmark_rows = store._load_response(by_endpoint["index_daily"][0]).rows
    benchmark_bars = [
        {
            "trading_date": _iso(_parse_date(row["trade_date"], "benchmark_date")),
            "ts_code": row["ts_code"],
            "close": row["close"],
            "high": row["high"],
        }
        for row in sorted(benchmark_rows, key=lambda item: item["trade_date"])
    ]

    def suspension_records() -> Iterable[dict[str, Any]]:
        for task in by_endpoint["suspend_d"]:
            for row in store._load_response(task).rows:
                if _is_full_session_suspension(row):
                    yield {
                        "trading_date": _iso(_parse_date(row["trade_date"], "suspension_date")),
                        "ts_code": row["ts_code"],
                        "instrument_id": row["ts_code"],
                        "suspend_type": row["suspend_type"],
                    }
    snapshots = [
        {
            "snapshot_date": _iso(_parse_date(item["snapshot_date"], "snapshot_date")),
            "members": [member["instrument_id"] for member in item["members"]],
        }
        for item in pit.manifest["snapshots"]
    ]
    return {
        "coverage_start": coverage.report["coverage_start"],
        "coverage_end": coverage.report["coverage_end"],
        "trading_dates": [
            _iso(_parse_date(item, "trading_date")) for item in coverage.trading_dates
        ],
        "pit_snapshots": snapshots,
        "pit_coverage_report": _json_safe(pit.coverage_report),
        "pit_manifest": _json_safe(pit.manifest),
        "signal_bars": signal_rows(),
        "benchmark_bars": benchmark_bars,
        "suspensions": suspension_records(),
        "locked_test_status": dict(LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
        "execution_realism": "INCOMPLETE",
        "trade_eligibility": False,
    }
