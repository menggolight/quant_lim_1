"""One-shot, raw-evidence diagnostic for the 2017-12 CSI 800 weights.

This module is deliberately separate from the 73-month Alpha Feasibility
collector.  The live boundary has exactly one fixed request.  A fresh parent
directory is atomically reserved before the request, so a failed, timed-out,
or interrupted run cannot be retried by changing the run id.

The exact response bytes are persisted only after bounded JSON, credential,
sensitive-label, non-finite-number, and post-cutoff market-data checks pass.
Only this module may consume the resulting ``response.raw.json``.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import os
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

import research.market_data.tushare_alpha_feasibility as alpha_data
from research.market_data.validation import SchemaValidationError, validate_json_schema


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = (
    REPOSITORY_ROOT
    / "data"
    / "tmp"
    / "alpha-feasibility"
    / "index-weight-value-diagnostic"
)
REQUEST_SCHEMA_PATH = (
    REPOSITORY_ROOT / "schemas" / "tushare_index_weight_value_request.v1.json"
)
PROFILE_SCHEMA_PATH = (
    REPOSITORY_ROOT / "schemas" / "tushare_index_weight_value_profile.v1.json"
)
REPLAY_SCHEMA_PATH = (
    REPOSITORY_ROOT / "schemas" / "tushare_index_weight_offline_replay.v1.json"
)

ENDPOINT = "index_weight"
INDEX_CODE = "000906.SH"
START_DATE = "20171201"
END_DATE = "20171231"
ABSOLUTE_CUTOFF = "20231231"
BASELINE_PARSER_COMMIT = "a0fadfc890d26be16e1d4c06e556674a59aa4be6"
REQUESTED_FIELDS = ("index_code", "con_code", "trade_date", "weight")
FIXED_PARAMS = {
    "index_code": INDEX_CODE,
    "start_date": START_DATE,
    "end_date": END_DATE,
}
LOCKED_TEST_STATUS = {
    "access": "NOT_ACCESSED",
    "download": "NOT_DOWNLOADED",
    "run": "NOT_RUN",
}
MAXIMUM_RESPONSE_BYTES = 2 * 1024 * 1024
MAXIMUM_DECIMAL_ADJUSTED_EXPONENT = 999
MAXIMUM_DECIMAL_SCALE = 999

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,95}$")
_SAFE_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_TS_CODE = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")
_PIT_CODE = re.compile(r"^\d{6}\.(?:SH|SZ)$")
_DATE8 = re.compile(r"^\d{8}$")
_DATE10 = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PLAIN_DECIMAL = re.compile(r"^-?\d+(?:\.\d+)?$")
_OPAQUE_REQUEST_ID = re.compile(r"^[\x21-\x7e]{1,256}$")
_SAFE_NORMALIZATION_CHANGE = re.compile(r"^[a-z][a-z0-9_]{2,95}$")
_SENSITIVE_LABEL = re.compile(
    r"(?:authorization|cookie|token|secret|password|passwd|credential|"
    r"api[_-]?key|access[_-]?key)",
    re.IGNORECASE,
)
_EMBEDDED_DATE = re.compile(
    r"(?<!\d)(\d{4})[-/]?(0[1-9]|1[0-2])[-/]?(0[1-9]|[12]\d|3[01])(?!\d)"
)


class IndexWeightValueDiagnosticError(RuntimeError):
    """Sanitized diagnostic failure that never contains provider values."""

    def __init__(self, code: str) -> None:
        if re.fullmatch(r"[a-z0-9_]{3,96}", str(code)) is None:
            code = "unsafe_error_sanitized"
        self.code = str(code)
        super().__init__(self.code)


def _safe_run_id(value: Any, *, token: str) -> str:
    if (
        type(value) is not str
        or _RUN_ID.fullmatch(value) is None
        or value in {".", ".."}
        or ".." in value
    ):
        raise IndexWeightValueDiagnosticError("invalid_run_id")
    if token in value:
        raise IndexWeightValueDiagnosticError("run_id_contains_credential")
    return value


def _safe_normalization_change(value: Any, *, token: str) -> str:
    if (
        type(value) is not str
        or _SAFE_NORMALIZATION_CHANGE.fullmatch(value) is None
        or _SENSITIVE_LABEL.search(value) is not None
        or token in value
    ):
        raise IndexWeightValueDiagnosticError("normalization_change_invalid")
    _reject_post_cutoff_market_dates(value)
    return value


def _request_counts() -> dict[str, int]:
    return {
        "index_weight": 1,
        "trade_cal": 0,
        "daily": 0,
        "adj_factor": 0,
        "index_daily": 0,
        "suspend_d": 0,
        "stock_basic": 0,
    }


def _require_timezone_aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise IndexWeightValueDiagnosticError("requested_at_must_be_timezone_aware")
    return value


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) is int:
        return "integer"
    if isinstance(value, Decimal):
        return "number"
    if type(value) is str:
        return "string"
    if type(value) is list:
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return "unknown"


def _transport_json_bytes(value: Any) -> bytes:
    """Use the provider-preserving canonical encoder for diagnostic evidence."""

    try:
        return alpha_data._canonical_transport_json_bytes(value) + b"\n"
    except alpha_data.AlphaFeasibilityDataError as exc:
        raise IndexWeightValueDiagnosticError("diagnostic_json_encoding_failed") from exc


def _write_transport_json_create_only(path: Path, value: Any) -> bytes:
    content = _transport_json_bytes(value)
    alpha_data._write_create_only(path, content)
    return content


def _publish_or_verify_transport_json(path: Path, value: Any) -> bytes:
    content = _transport_json_bytes(value)
    if path.exists():
        try:
            existing = path.read_bytes()
        except OSError as exc:
            raise IndexWeightValueDiagnosticError("existing_artifact_unreadable") from exc
        if existing != content:
            raise IndexWeightValueDiagnosticError("deterministic_replay_mismatch")
        return existing
    alpha_data._write_create_only(path, content)
    return content


def _fixed_task() -> alpha_data.CollectionTask:
    plan = alpha_data.load_config_and_build_plan(
        REPOSITORY_ROOT / "configs" / "a_share_technical_alpha_feasibility.v2.json"
    )
    task = plan.pit_tasks[0]
    if (
        task.endpoint != ENDPOINT
        or dict(task.params) != FIXED_PARAMS
        or tuple(task.fields) != REQUESTED_FIELDS
        or task.scope_instruments
    ):
        raise IndexWeightValueDiagnosticError("fixed_request_contract_drift")
    return task


def _request_payload(requested_at: datetime) -> dict[str, Any]:
    request_core = {
        "api_name": ENDPOINT,
        "params": dict(FIXED_PARAMS),
        "requested_fields": list(REQUESTED_FIELDS),
        "endpoint": ENDPOINT,
        "date_bounds": {
            "start_date": START_DATE,
            "end_date": END_DATE,
            "absolute_cutoff": ABSOLUTE_CUTOFF,
        },
    }
    return {
        "api_name": ENDPOINT,
        "params": dict(FIXED_PARAMS),
        "requested_fields": list(REQUESTED_FIELDS),
        "request_fingerprint": alpha_data.canonical_sha256(request_core),
        "requested_at": _require_timezone_aware(requested_at).isoformat(),
        "endpoint": ENDPOINT,
        "date_bounds": dict(request_core["date_bounds"]),
    }


def _contains_decoded_text(value: Any, needle: str) -> bool:
    if type(value) is str:
        return needle in value
    if isinstance(value, Mapping):
        return any(
            needle in str(key) or _contains_decoded_text(item, needle)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_decoded_text(item, needle) for item in value)
    return False


def _reject_sensitive_labels(value: Any) -> None:
    if type(value) is str:
        if _SENSITIVE_LABEL.search(value) is not None:
            raise IndexWeightValueDiagnosticError("sensitive_label_detected")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if type(key) is not str or _SENSITIVE_LABEL.search(key) is not None:
                raise IndexWeightValueDiagnosticError("sensitive_field_name_detected")
            _reject_sensitive_labels(item)
        return
    if isinstance(value, list):
        for item in value:
            _reject_sensitive_labels(item)


def _reject_post_cutoff_market_dates(value: Any) -> None:
    if type(value) is str:
        candidates = (match.group(0) for match in _EMBEDDED_DATE.finditer(value))
    elif type(value) is int and 10_000_000 <= value <= 99_999_999:
        candidates = (str(value),)
    elif (
        isinstance(value, Decimal)
        and value.is_finite()
        and Decimal("10000000") <= value <= Decimal("99999999")
        and value == value.to_integral_value()
    ):
        candidates = (str(int(value)),)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _reject_post_cutoff_market_dates(key)
            _reject_post_cutoff_market_dates(item)
        return
    elif isinstance(value, list):
        for item in value:
            _reject_post_cutoff_market_dates(item)
        return
    else:
        return
    for candidate in candidates:
        compact = candidate.replace("-", "").replace("/", "")
        if _DATE8.fullmatch(compact) and compact > ABSOLUTE_CUTOFF:
            raise IndexWeightValueDiagnosticError("post_cutoff_market_data_detected")


def _decimal_within_profile_bounds(value: Decimal) -> bool:
    if not value.is_finite():
        return False
    exponent = value.as_tuple().exponent
    return (
        -MAXIMUM_DECIMAL_SCALE <= exponent
        and value.adjusted() <= MAXIMUM_DECIMAL_ADJUSTED_EXPONENT
    )


def _reject_pathological_json_decimals(value: Any) -> None:
    if isinstance(value, Decimal):
        if not _decimal_within_profile_bounds(value):
            raise IndexWeightValueDiagnosticError("json_decimal_magnitude_unsafe")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_pathological_json_decimals(key)
            _reject_pathological_json_decimals(item)
        return
    if isinstance(value, list):
        for item in value:
            _reject_pathological_json_decimals(item)


def _bounded_plain_decimal(value: Decimal) -> str:
    if (
        not value.is_finite()
        or value.adjusted() > MAXIMUM_DECIMAL_ADJUSTED_EXPONENT + 8
        or value.as_tuple().exponent < -MAXIMUM_DECIMAL_SCALE
    ):
        raise IndexWeightValueDiagnosticError("profile_decimal_magnitude_unsafe")
    return format(value, "f")


def scan_raw_response(
    raw: bytes,
    *,
    token: str,
    maximum_response_bytes: int = MAXIMUM_RESPONSE_BYTES,
) -> tuple[Mapping[str, Any], dict[str, Any]]:
    """Return a parsed response only after every pre-persistence scan passes."""

    if type(raw) is not bytes:
        raise IndexWeightValueDiagnosticError("response_not_bytes")
    try:
        validated_token = alpha_data._validate_token(token)
    except alpha_data.AlphaFeasibilityDataError as exc:
        raise IndexWeightValueDiagnosticError("credential_preflight_failed") from exc
    if validated_token.encode("utf-8") in raw:
        raise IndexWeightValueDiagnosticError("token_leak_detected")
    if type(maximum_response_bytes) is not int or maximum_response_bytes <= 0:
        raise IndexWeightValueDiagnosticError("response_size_limit_invalid")
    if len(raw) > min(maximum_response_bytes, MAXIMUM_RESPONSE_BYTES):
        raise IndexWeightValueDiagnosticError("response_body_too_large")
    try:
        parsed = alpha_data.strict_json_loads(raw, label="diagnostic_response")
    except alpha_data.AlphaFeasibilityDataError as exc:
        code = (
            "nonfinite_json_value_detected"
            if exc.code in {"nonfinite_json_number", "nonfinite_decimal"}
            else "response_json_invalid"
        )
        raise IndexWeightValueDiagnosticError(code) from exc
    if not isinstance(parsed, Mapping):
        raise IndexWeightValueDiagnosticError("response_root_not_object")
    _reject_pathological_json_decimals(parsed)
    if alpha_data._contains_unicode_surrogate(parsed):
        raise IndexWeightValueDiagnosticError("unicode_surrogate_detected")
    if _contains_decoded_text(parsed, validated_token):
        raise IndexWeightValueDiagnosticError("token_leak_detected")
    _reject_sensitive_labels(parsed)
    # Only request_id is opaque transport metadata.  Every other root
    # extension is scanned before exact raw bytes may be persisted.
    for key, value in parsed.items():
        _reject_post_cutoff_market_dates(key)
        if key == "request_id":
            if type(value) is not str or _OPAQUE_REQUEST_ID.fullmatch(value) is None:
                raise IndexWeightValueDiagnosticError("request_id_not_safe_opaque_string")
            continue
        _reject_post_cutoff_market_dates(value)
    return parsed, {
        "raw_transport_sha256": hashlib.sha256(raw).hexdigest(),
        "response_byte_count": len(raw),
        "token_leak_check": "PASSED",
        "sensitive_field_name_scan": "PASSED",
        "nonfinite_json_check": "PASSED",
        "response_size_check": "PASSED",
        "post_cutoff_market_date_check": "PASSED",
        "decimal_magnitude_check": "PASSED",
    }


def _require_profile_table(root: Mapping[str, Any]) -> tuple[list[str], list[list[Any]]]:
    if not {"code", "msg", "data"}.issubset(root) or type(root.get("code")) is not int:
        raise IndexWeightValueDiagnosticError("response_semantic_core_invalid")
    if root["code"] != 0 or not isinstance(root.get("data"), Mapping):
        raise IndexWeightValueDiagnosticError("response_not_success_payload")
    data = root["data"]
    fields = data.get("fields")
    items = data.get("items")
    if (
        not isinstance(fields, list)
        or not fields
        or any(
            type(field) is not str
            or _SAFE_FIELD.fullmatch(field) is None
            or _SENSITIVE_LABEL.search(field) is not None
            for field in fields
        )
        or len(fields) != len(set(fields))
    ):
        raise IndexWeightValueDiagnosticError("data_fields_invalid")
    if not set(REQUESTED_FIELDS).issubset(fields):
        raise IndexWeightValueDiagnosticError("data_required_fields_missing")
    if not isinstance(items, list):
        raise IndexWeightValueDiagnosticError("data_items_not_array")
    if any(not isinstance(item, list) or len(item) != len(fields) for item in items):
        raise IndexWeightValueDiagnosticError("data_item_width_mismatch")
    return fields, items


def _parse_trade_date_profile(value: Any) -> tuple[str | None, str | None]:
    if type(value) is str and _DATE8.fullmatch(value):
        compact = value
    elif type(value) is str and _DATE10.fullmatch(value):
        compact = value.replace("-", "")
    else:
        return None, "trade_date_representation_invalid"
    try:
        parsed = datetime.strptime(compact, "%Y%m%d").date()
    except ValueError:
        return None, "trade_date_invalid"
    if not date(2017, 12, 1) <= parsed <= date(2017, 12, 31):
        return compact, "trade_date_out_of_window"
    return compact, None


def _parse_weight_profile(value: Any) -> tuple[Decimal | None, str | None]:
    if type(value) is bool:
        return None, "weight_bool"
    if value is None:
        return None, "weight_null"
    if type(value) is int or isinstance(value, Decimal):
        candidate = value
    elif type(value) is str:
        candidate = value
    else:
        return None, "weight_not_plain_decimal"
    try:
        number = Decimal(str(candidate))
    except InvalidOperation:
        return None, "weight_not_numeric"
    if not number.is_finite():
        return None, "weight_nonfinite"
    if not _decimal_within_profile_bounds(number):
        return None, "weight_decimal_magnitude_unsafe"
    if number < 0:
        return number, "weight_negative"
    return number, None


def _baseline_weight_failure(weight: Any) -> tuple[str, str, str] | None:
    if type(weight) is bool:
        return "weight", "boolean", "weight_bool"
    if weight is None:
        return "weight", "null", "weight_null"
    if not (type(weight) in {str, int} or isinstance(weight, Decimal)):
        return "weight", _json_type(weight), "weight_type_rejected"
    try:
        number = Decimal(str(weight))
    except InvalidOperation:
        return "weight", _json_type(weight), "weight_not_numeric"
    if not number.is_finite():
        return "weight", _json_type(weight), "weight_nonfinite"
    if number < 0:
        return "weight", _json_type(weight), "weight_negative"
    scale = max(0, -number.as_tuple().exponent)
    if scale < 3:
        return "weight", _json_type(weight), "weight_decimal_scale_below_three"
    return None


def _baseline_parser_row_outcome(
    row: Mapping[str, Any],
    *,
    task: alpha_data.CollectionTask,
) -> tuple[tuple[str, str, str] | None, tuple[Any, ...] | None]:
    """Freeze the exact a0fad index_weight row contract for evidence replay."""

    index_code = row["index_code"]
    if index_code != task.params.get("index_code"):
        return ("index_code", _json_type(index_code), "index_code_mismatch"), None

    con_code = row["con_code"]
    if type(con_code) is not str:
        return ("con_code", _json_type(con_code), "con_code_non_string"), None
    if _TS_CODE.fullmatch(con_code) is None:
        return ("con_code", "string", "con_code_format_invalid"), None
    if _PIT_CODE.fullmatch(con_code) is None:
        return ("con_code", "string", "con_code_exchange_not_allowed"), None

    trade_date = row["trade_date"]
    if type(trade_date) is not str:
        predicate = (
            "trade_date_integer_rejected"
            if type(trade_date) is int and type(trade_date) is not bool
            else "trade_date_type_rejected"
        )
        return ("trade_date", _json_type(trade_date), predicate), None
    if _DATE8.fullmatch(trade_date):
        try:
            parsed_date = datetime.strptime(trade_date, "%Y%m%d").date()
        except ValueError:
            return (
                "trade_date",
                "string",
                "trade_date_calendar_invalid",
            ), None
    elif _DATE10.fullmatch(trade_date):
        try:
            parsed_date = date.fromisoformat(trade_date)
        except ValueError:
            return (
                "trade_date",
                "string",
                "trade_date_calendar_invalid",
            ), None
    else:
        return ("trade_date", "string", "trade_date_format_invalid"), None
    compact_date = parsed_date.strftime("%Y%m%d")
    if not task.params["start_date"] <= compact_date <= task.params["end_date"]:
        return ("trade_date", "string", "trade_date_out_of_window"), None

    weight_failure = _baseline_weight_failure(row["weight"])
    if weight_failure is not None:
        return weight_failure, None
    return None, (index_code, compact_date, con_code)


def _value_identity(value: Any) -> tuple[str, bytes]:
    value_type = _json_type(value)
    try:
        content = alpha_data._canonical_transport_json_bytes(value)
    except alpha_data.AlphaFeasibilityDataError:
        content = repr(type(value)).encode("ascii", "replace")
    return value_type, hashlib.sha256(content).digest()


def _safe_projected_value(value: Any) -> Any:
    if value is None or type(value) in {bool, int} or isinstance(value, Decimal):
        return value
    if type(value) is str and len(value) <= 256:
        return value
    value_type, digest = _value_identity(value)
    return {
        "redacted_json_type": value_type,
        "value_sha256": digest.hex(),
    }


def _profile_self_hash(value: Mapping[str, Any]) -> dict[str, Any]:
    unsigned = dict(value)
    if "profile_sha256" in unsigned:
        raise IndexWeightValueDiagnosticError("profile_hash_must_be_derived")
    result = dict(unsigned)
    result["profile_sha256"] = hashlib.sha256(_transport_json_bytes(unsigned)).hexdigest()
    return result


def build_value_profile(
    root: Mapping[str, Any],
    *,
    scan_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    fields, items = _require_profile_table(root)
    positions = {field: index for index, field in enumerate(fields)}
    task = _fixed_task()
    rows = [
        {field: item[positions[field]] for field in REQUESTED_FIELDS}
        for item in items
    ]

    per_type: dict[str, dict[str, int]] = {}
    null_counts: dict[str, int] = {}
    unique_counts: dict[str, int] = {}
    for field in REQUESTED_FIELDS:
        counts = Counter(_json_type(row[field]) for row in rows)
        per_type[field] = dict(sorted(counts.items()))
        null_counts[field] = counts.get("null", 0)
        unique_counts[field] = len({_value_identity(row[field]) for row in rows})

    first_index: int | None = None
    first_field: str | None = None
    first_type: str | None = None
    first_predicate: str | None = None
    first_row: Mapping[str, Any] | None = None
    first_row_sha: str | None = None
    seen_primary: set[tuple[Any, ...]] = set()
    duplicate_primary = 0
    for index, row in enumerate(rows):
        failure, primary = _baseline_parser_row_outcome(row, task=task)
        if failure is None and primary is not None:
            if primary in seen_primary:
                duplicate_primary += 1
                failure = ("primary_key", "object", "duplicate_primary_key")
            else:
                seen_primary.add(primary)
        if first_index is None and failure is not None:
            first_index = index
            first_field, first_type, first_predicate = failure
            first_row = {
                field: _safe_projected_value(row[field])
                for field in REQUESTED_FIELDS
            }
            first_row_sha = alpha_data.canonical_sha256(
                {
                    "raw_transport_sha256": scan_evidence["raw_transport_sha256"],
                    "zero_based_row_index": index,
                    "projected_fields": list(REQUESTED_FIELDS),
                }
            )

    index_values = [row["index_code"] for row in rows]
    index_strings = sorted({value for value in index_values if type(value) is str})
    index_profile = {
        "unique_values": index_strings,
        "non_string_count": sum(type(value) is not str for value in index_values),
        "requested_index_mismatch_count": sum(
            type(value) is not str or value != INDEX_CODE for value in index_values
        ),
        "invalid_code_format_count": sum(
            type(value) is not str or _TS_CODE.fullmatch(value) is None
            for value in index_values
        ),
    }

    con_values = [row["con_code"] for row in rows]
    con_profile = {
        "non_string_count": sum(type(value) is not str for value in con_values),
        "invalid_code_format_count": sum(
            type(value) is not str or _TS_CODE.fullmatch(value) is None
            for value in con_values
        ),
        "SH_count": sum(
            type(value) is str and value.endswith(".SH") for value in con_values
        ),
        "SZ_count": sum(
            type(value) is str and value.endswith(".SZ") for value in con_values
        ),
        "other_exchange_count": sum(
            type(value) is str
            and not value.endswith((".SH", ".SZ"))
            for value in con_values
        ),
    }

    trade_values = [row["trade_date"] for row in rows]
    trade_type_counts = Counter(_json_type(value) for value in trade_values)
    valid_trade_dates: list[str] = []
    invalid_date_count = 0
    outside_count = 0
    for value in trade_values:
        compact, issue = _parse_trade_date_profile(value)
        if compact is not None:
            valid_trade_dates.append(compact)
        if issue == "trade_date_out_of_window":
            outside_count += 1
        elif issue is not None:
            invalid_date_count += 1
    trade_profile = {
        "json_type_counts": dict(sorted(trade_type_counts.items())),
        "format_YYYYMMDD_count": sum(
            type(value) is str and _DATE8.fullmatch(value) is not None
            for value in trade_values
        ),
        "integer_8_digit_count": sum(
            type(value) is int
            and type(value) is not bool
            and 10_000_000 <= value <= 99_999_999
            for value in trade_values
        ),
        "invalid_date_count": invalid_date_count,
        "min_date": min(valid_trade_dates) if valid_trade_dates else None,
        "max_date": max(valid_trade_dates) if valid_trade_dates else None,
        "dates_outside_requested_month": outside_count,
        "unique_trade_dates": sorted(set(valid_trade_dates)),
    }

    weight_values = [row["weight"] for row in rows]
    weight_type_counts = Counter(_json_type(value) for value in weight_values)
    numeric_values: list[Decimal] = []
    weights_by_date: dict[str, list[Decimal]] = {}
    scale_counts: Counter[str] = Counter()
    negative = zero = positive = nonfinite = unsafe_magnitude = 0
    numeric_number = numeric_string = plain_numeric_string = 0
    for row in rows:
        value = row["weight"]
        if type(value) is int or isinstance(value, Decimal):
            numeric_number += 1
        if type(value) is str and _PLAIN_DECIMAL.fullmatch(value) is not None:
            plain_numeric_string += 1
        number, issue = _parse_weight_profile(value)
        if issue == "weight_nonfinite":
            nonfinite += 1
        if issue == "weight_decimal_magnitude_unsafe":
            unsafe_magnitude += 1
        if type(value) is str and number is not None:
            numeric_string += 1
        if number is None or not number.is_finite():
            continue
        numeric_values.append(number)
        scale_counts[str(max(0, -number.as_tuple().exponent))] += 1
        if number < 0:
            negative += 1
        elif number == 0:
            zero += 1
        else:
            positive += 1
        compact_date, date_issue = _parse_trade_date_profile(row["trade_date"])
        if compact_date is not None and date_issue is None:
            weights_by_date.setdefault(compact_date, []).append(number)
    sum_by_date = {
        trade_date: alpha_data._exact_decimal_sum(weights)
        for trade_date, weights in weights_by_date.items()
    }
    weight_profile = {
        "json_type_counts": dict(sorted(weight_type_counts.items())),
        "numeric_number_count": numeric_number,
        "numeric_string_count": numeric_string,
        "plain_numeric_string_count": plain_numeric_string,
        "null_count": weight_type_counts.get("null", 0),
        "bool_count": weight_type_counts.get("boolean", 0),
        "nonfinite_count": nonfinite,
        "magnitude_out_of_bounds_count": unsafe_magnitude,
        "negative_count": negative,
        "zero_count": zero,
        "positive_count": positive,
        "minimum": _bounded_plain_decimal(min(numeric_values)) if numeric_values else None,
        "maximum": _bounded_plain_decimal(max(numeric_values)) if numeric_values else None,
        "sum_by_trade_date": {
            key: _bounded_plain_decimal(sum_by_date[key]) for key in sorted(sum_by_date)
        },
        "decimal_scale_distribution": dict(
            sorted(scale_counts.items(), key=lambda item: int(item[0]))
        ),
    }

    profile = {
        "schema_version": "tushare-index-weight-value-profile.v1",
        "baseline_parser_commit": BASELINE_PARSER_COMMIT,
        "raw_transport_sha256": scan_evidence["raw_transport_sha256"],
        "response_byte_count": scan_evidence["response_byte_count"],
        "token_leak_check": scan_evidence["token_leak_check"],
        "sensitive_field_name_scan": scan_evidence["sensitive_field_name_scan"],
        "nonfinite_json_check": scan_evidence["nonfinite_json_check"],
        "response_size_check": scan_evidence["response_size_check"],
        "post_cutoff_market_date_check": scan_evidence[
            "post_cutoff_market_date_check"
        ],
        "decimal_magnitude_check": scan_evidence["decimal_magnitude_check"],
        "data_row_count": len(items),
        "observed_data_fields": list(fields),
        "per_field_json_type_counts": per_type,
        "per_field_null_counts": null_counts,
        "per_field_unique_counts": unique_counts,
        "duplicate_primary_key_count": duplicate_primary,
        "first_failing_row_index": first_index,
        "first_failing_field": first_field,
        "first_failing_json_type": first_type,
        "first_failing_predicate": first_predicate,
        "first_failing_projected_row": first_row,
        "first_failing_row_sha256": first_row_sha,
        "index_code_profile": index_profile,
        "con_code_profile": con_profile,
        "trade_date_profile": trade_profile,
        "weight_profile": weight_profile,
        "locked_test_status": dict(LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
    }
    result = _profile_self_hash(profile)
    try:
        validate_json_schema(result, PROFILE_SCHEMA_PATH)
    except SchemaValidationError as exc:
        raise IndexWeightValueDiagnosticError("value_profile_schema_invalid") from exc
    return result


def _read_raw_and_profile(
    run_directory: Path,
    *,
    token: str,
) -> tuple[bytes, Mapping[str, Any], dict[str, Any], dict[str, Any]]:
    raw_path = run_directory / "response.raw.json"
    try:
        raw = raw_path.read_bytes()
    except OSError as exc:
        raise IndexWeightValueDiagnosticError("raw_response_unavailable") from exc
    root, scan = scan_raw_response(raw, token=token)
    _verify_collection_provenance(
        run_directory,
        token=token,
        raw_transport_sha256=scan["raw_transport_sha256"],
        response_byte_count=scan["response_byte_count"],
    )
    profile = build_value_profile(root, scan_evidence=scan)
    return raw, root, scan, profile


def _network_started_payload(request: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "tushare-index-weight-network-call.v1",
        "state": "TRANSPORT_INVOCATION_STARTED",
        "endpoint": ENDPOINT,
        "request_fingerprint": request["request_fingerprint"],
        "request_artifact_sha256": hashlib.sha256(
            alpha_data.canonical_json_bytes(request)
        ).hexdigest(),
        "network_process_count": 1,
        "actual_request_count_by_endpoint": _request_counts(),
    }


def _network_scanned_payload(
    request: Mapping[str, Any],
    *,
    scan: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "tushare-index-weight-network-call.v1",
        "state": "HTTP_RESPONSE_SCANNED",
        "endpoint": ENDPOINT,
        "request_fingerprint": request["request_fingerprint"],
        "request_artifact_sha256": hashlib.sha256(
            alpha_data.canonical_json_bytes(request)
        ).hexdigest(),
        "network_process_count": 1,
        "actual_request_count_by_endpoint": _request_counts(),
        "http_status": 200,
        "raw_transport_sha256": scan["raw_transport_sha256"],
        "response_byte_count": scan["response_byte_count"],
    }


def _read_safe_json_artifact(path: Path, *, token: str) -> Mapping[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise IndexWeightValueDiagnosticError("collection_provenance_missing") from exc
    if token.encode("utf-8") in raw:
        raise IndexWeightValueDiagnosticError("collection_provenance_secret_detected")
    try:
        value = alpha_data.strict_json_loads(raw, label="diagnostic_artifact")
    except alpha_data.AlphaFeasibilityDataError as exc:
        raise IndexWeightValueDiagnosticError("collection_provenance_invalid") from exc
    if not isinstance(value, Mapping) or alpha_data._contains_unicode_surrogate(value):
        raise IndexWeightValueDiagnosticError("collection_provenance_invalid")
    return value


def _verify_collection_provenance(
    run_directory: Path,
    *,
    token: str,
    raw_transport_sha256: str,
    response_byte_count: int,
) -> None:
    request = _read_safe_json_artifact(run_directory / "request.json", token=token)
    try:
        validate_json_schema(request, REQUEST_SCHEMA_PATH)
    except SchemaValidationError as exc:
        raise IndexWeightValueDiagnosticError("request_artifact_invalid") from exc
    try:
        request_time = datetime.fromisoformat(str(request["requested_at"]))
        expected_fingerprint = _request_payload(request_time)["request_fingerprint"]
    except (ValueError, IndexWeightValueDiagnosticError) as exc:
        raise IndexWeightValueDiagnosticError("request_artifact_invalid") from exc
    if request["request_fingerprint"] != expected_fingerprint:
        raise IndexWeightValueDiagnosticError("request_artifact_invalid")
    started = _read_safe_json_artifact(
        run_directory / "network_call_started.json", token=token
    )
    if dict(started) != _network_started_payload(request):
        raise IndexWeightValueDiagnosticError("network_start_artifact_invalid")
    scanned = _read_safe_json_artifact(
        run_directory / "network_response_scanned.json", token=token
    )
    expected_scanned = _network_scanned_payload(
        request,
        scan={
            "raw_transport_sha256": raw_transport_sha256,
            "response_byte_count": response_byte_count,
        },
    )
    if dict(scanned) != expected_scanned:
        raise IndexWeightValueDiagnosticError("network_response_artifact_invalid")


def _reserve_collection(
    *,
    token: str,
    run_id: str,
    output_root: Path | str,
    requested_at: datetime | None = None,
) -> tuple[alpha_data.CollectionTask, Path, dict[str, Any]]:
    """Atomically consume the one-shot budget before any transport call."""

    task = _fixed_task()
    safe_run_id = _safe_run_id(run_id, token=token)
    root = Path(output_root).resolve()
    try:
        root.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise IndexWeightValueDiagnosticError("diagnostic_network_budget_already_consumed") from exc
    run_directory = root / safe_run_id
    try:
        run_directory.mkdir(exist_ok=False)
    except OSError as exc:
        raise IndexWeightValueDiagnosticError("diagnostic_run_directory_create_failed") from exc

    current = _require_timezone_aware(requested_at or datetime.now(timezone.utc))
    request = _request_payload(current)
    try:
        validate_json_schema(request, REQUEST_SCHEMA_PATH)
    except SchemaValidationError as exc:
        raise IndexWeightValueDiagnosticError("request_schema_invalid") from exc
    # This is the durable reservation.  Once written, no second collect is
    # allowed even if transport or persistence fails.
    alpha_data._write_json_create_only(
        run_directory / "request.json", request, token=token
    )
    return task, run_directory, request


def _persist_scanned_response(
    *,
    token: str,
    safe_run_id: str,
    run_directory: Path,
    request: Mapping[str, Any],
    response: alpha_data.TushareHttpResponse | bytes,
    maximum_response_bytes: int,
) -> dict[str, Any]:
    """Scan one returned response and persist only safe, bound evidence."""

    if isinstance(response, bytes):
        response = alpha_data.TushareHttpResponse(http_status=200, body=response)
    if not isinstance(response, alpha_data.TushareHttpResponse):
        raise IndexWeightValueDiagnosticError("transport_result_invalid")
    if response.http_status != 200:
        raise IndexWeightValueDiagnosticError("http_status_not_success")

    root_value, scan = scan_raw_response(
        response.body,
        token=token,
        maximum_response_bytes=maximum_response_bytes,
    )
    _write_transport_json_create_only(
        run_directory / "network_response_scanned.json",
        _network_scanned_payload(request, scan=scan),
    )
    raw_path = run_directory / "response.raw.json"
    alpha_data._write_create_only(raw_path, response.body)
    profile = build_value_profile(root_value, scan_evidence=scan)
    _write_transport_json_create_only(run_directory / "value_profile.json", profile)
    return {
        "status": "DIAGNOSTIC_RAW_CAPTURED",
        "run_id": safe_run_id,
        "run_directory": str(run_directory),
        "network_process_count": 1,
        "actual_request_count_by_endpoint": _request_counts(),
        "raw_response_persisted": True,
        "raw_response_path": str(raw_path),
        "raw_transport_sha256": scan["raw_transport_sha256"],
        "response_byte_count": scan["response_byte_count"],
        "token_leak_check": scan["token_leak_check"],
        "value_profile_path": str(run_directory / "value_profile.json"),
        "first_failing_row_index": profile["first_failing_row_index"],
        "first_failing_field": profile["first_failing_field"],
        "first_failing_json_type": profile["first_failing_json_type"],
        "first_failing_predicate": profile["first_failing_predicate"],
        "locked_test_status": dict(LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
    }


def collect_live_once(
    *,
    token: str,
    run_id: str,
    requested_at: datetime | None = None,
) -> dict[str, Any]:
    """The only production network boundary: fixed root, transport, and limits."""

    task, run_directory, request = _reserve_collection(
        token=token,
        run_id=run_id,
        output_root=DEFAULT_OUTPUT_ROOT,
        requested_at=requested_at,
    )
    _write_transport_json_create_only(
        run_directory / "network_call_started.json",
        _network_started_payload(request),
    )
    try:
        response = alpha_data.HttpsTushareTransport()(
            endpoint=task.endpoint,
            params=task.params,
            fields=task.fields,
            token=token,
            timeout_seconds=30,
            maximum_response_bytes=MAXIMUM_RESPONSE_BYTES,
        )
    except alpha_data.AlphaFeasibilityDataError as exc:
        raise IndexWeightValueDiagnosticError(exc.code) from exc
    return _persist_scanned_response(
        token=token,
        safe_run_id=_safe_run_id(run_id, token=token),
        run_directory=run_directory,
        request=request,
        response=response,
        maximum_response_bytes=MAXIMUM_RESPONSE_BYTES,
    )


def _collect_once_for_offline_test(
    *,
    token: str,
    run_id: str,
    output_root: Path | str,
    requested_at: datetime,
    response: alpha_data.TushareHttpResponse | bytes,
    maximum_response_bytes: int = MAXIMUM_RESPONSE_BYTES,
) -> dict[str, Any]:
    """Persist an injected response; this helper has no network capability."""

    _task, run_directory, request = _reserve_collection(
        token=token,
        run_id=run_id,
        output_root=output_root,
        requested_at=requested_at,
    )
    _write_transport_json_create_only(
        run_directory / "network_call_started.json",
        _network_started_payload(request),
    )
    return _persist_scanned_response(
        token=token,
        safe_run_id=_safe_run_id(run_id, token=token),
        run_directory=run_directory,
        request=request,
        response=response,
        maximum_response_bytes=maximum_response_bytes,
    )


def regenerate_value_profile(
    *,
    token: str,
    run_id: str,
    output_root: Path | str = DEFAULT_OUTPUT_ROOT,
) -> dict[str, Any]:
    """Read the saved raw bytes offline and verify the existing profile."""

    run_directory = Path(output_root).resolve() / _safe_run_id(run_id, token=token)
    _raw, _root, _scan, profile = _read_raw_and_profile(run_directory, token=token)
    content = _publish_or_verify_transport_json(
        run_directory / "value_profile.json", profile
    )
    return {
        "status": "VALUE_PROFILE_VERIFIED",
        "run_id": run_id,
        "value_profile_sha256": hashlib.sha256(content).hexdigest(),
        "first_failing_row_index": profile["first_failing_row_index"],
        "first_failing_field": profile["first_failing_field"],
        "first_failing_json_type": profile["first_failing_json_type"],
        "first_failing_predicate": profile["first_failing_predicate"],
        "locked_test_status": dict(LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
    }


def diagnose_current_response_failure(
    *,
    token: str,
    run_id: str,
    output_root: Path | str = DEFAULT_OUTPUT_ROOT,
) -> dict[str, Any]:
    """Locate a current-parser row failure without exposing the raw body."""

    run_directory = Path(output_root).resolve() / _safe_run_id(run_id, token=token)
    _raw, root, _scan, _profile = _read_raw_and_profile(
        run_directory,
        token=token,
    )
    fields, items = _require_profile_table(root)
    positions = {field: index for index, field in enumerate(fields)}
    task = _fixed_task()
    keys: set[tuple[Any, ...]] = set()
    for index, item in enumerate(items):
        projected = {
            field: item[positions[field]] for field in REQUESTED_FIELDS
        }
        try:
            normalized, _isolated = alpha_data._normalize_response_row(task, projected)
        except alpha_data.AlphaFeasibilityDataError as exc:
            return {
                "status": "CURRENT_ROW_FAILURE_LOCATED",
                "zero_based_row_index": index,
                "failure_code": exc.code,
                "projected_row": {
                    field: _safe_projected_value(projected[field])
                    for field in REQUESTED_FIELDS
                },
                "locked_test_status": dict(LOCKED_TEST_STATUS),
                "locked_test_consumed": False,
            }
        if normalized is None:
            return {
                "status": "CURRENT_ROW_FAILURE_LOCATED",
                "zero_based_row_index": index,
                "failure_code": "row_unexpectedly_isolated",
                "projected_row": {
                    field: _safe_projected_value(projected[field])
                    for field in REQUESTED_FIELDS
                },
                "locked_test_status": dict(LOCKED_TEST_STATUS),
                "locked_test_consumed": False,
            }
        key = alpha_data._primary_key(ENDPOINT, normalized)
        if key in keys:
            return {
                "status": "CURRENT_ROW_FAILURE_LOCATED",
                "zero_based_row_index": index,
                "failure_code": "duplicate_response_primary_key",
                "projected_row": {
                    field: _safe_projected_value(projected[field])
                    for field in REQUESTED_FIELDS
                },
                "locked_test_status": dict(LOCKED_TEST_STATUS),
                "locked_test_consumed": False,
            }
        keys.add(key)
    data = root["data"]
    extension_fields = sorted(
        alpha_data._safe_diagnostic_data_field(field)
        for field in set(data) - {"fields", "items"}
    )
    has_more_value = data.get("has_more") if "has_more" in data else None
    count_value = data.get("count") if "count" in data else None
    return {
        "status": "CURRENT_ROWS_ACCEPTED",
        "normalized_row_count": len(keys),
        "data_extension_fields": extension_fields,
        "has_more": (
            has_more_value
            if type(has_more_value) is bool or has_more_value is None
            else f"json_type:{_json_type(has_more_value)}"
        ),
        "count": (
            count_value
            if type(count_value) is int
            else (None if count_value is None else f"json_type:{_json_type(count_value)}")
        ),
        "count_matches_items": (
            type(count_value) is int and count_value == len(items)
            if "count" in data
            else None
        ),
        "locked_test_status": dict(LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
    }


def _normalized_pit_payload(validated: alpha_data.ValidatedResponse) -> dict[str, Any]:
    rows = [dict(row) for row in validated.rows]
    weights_by_date: dict[str, list[Decimal]] = {}
    for row in rows:
        weight = Decimal(str(row["weight"]))
        weights_by_date.setdefault(row["trade_date"], []).append(weight)
    sums = {
        trade_date: alpha_data._exact_decimal_sum(weights)
        for trade_date, weights in weights_by_date.items()
    }
    return {
        "schema_version": "tushare-index-weight-normalized-pit.v1",
        "fields": list(REQUESTED_FIELDS),
        "items": [[row[field] for field in REQUESTED_FIELDS] for row in rows],
        "row_count": len(rows),
        "trade_dates": sorted(sums),
        "weight_sum_by_trade_date": {
            key: format(sums[key], "f") for key in sorted(sums)
        },
        "normalized_content_sha256": validated.normalized_content_sha256,
        "locked_test_status": dict(LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
    }


def _replay_once(raw: bytes, *, token: str) -> tuple[dict[str, Any], bytes]:
    task = _fixed_task()
    try:
        validated = alpha_data.validate_response_bytes(
            task,
            raw,
            token=token,
            maximum_response_bytes=MAXIMUM_RESPONSE_BYTES,
            http_status=200,
        )
    except alpha_data.AlphaFeasibilityDataError as exc:
        current: BaseException = exc
        deepest_code = exc.diagnostic.get("data_failure_category", exc.code)
        while isinstance(current.__cause__, alpha_data.AlphaFeasibilityDataError):
            current = current.__cause__
            deepest_code = current.code
        raise IndexWeightValueDiagnosticError(
            deepest_code
        ) from exc
    payload = _normalized_pit_payload(validated)
    if payload["row_count"] != 800:
        raise IndexWeightValueDiagnosticError("normalized_row_count_not_800")
    if len({tuple(item[:3]) for item in payload["items"]}) != 800:
        raise IndexWeightValueDiagnosticError("normalized_primary_key_duplicate")
    if any(
        type(value) is not str
        or _DATE8.fullmatch(value) is None
        or not START_DATE <= value <= END_DATE
        for value in payload["trade_dates"]
    ):
        raise IndexWeightValueDiagnosticError("normalized_trade_date_invalid")
    return payload, _transport_json_bytes(payload)


def replay_saved_response(
    *,
    token: str,
    run_id: str,
    normalization_change: str,
    output_root: Path | str = DEFAULT_OUTPUT_ROOT,
) -> dict[str, Any]:
    """Replay the same exact bytes twice and publish deterministic evidence."""

    change_id = _safe_normalization_change(normalization_change, token=token)
    run_directory = Path(output_root).resolve() / _safe_run_id(run_id, token=token)
    raw, _root, scan, profile = _read_raw_and_profile(run_directory, token=token)
    profile_path = run_directory / "value_profile.json"
    if not profile_path.is_file():
        raise IndexWeightValueDiagnosticError("value_profile_artifact_missing")
    profile_bytes = _publish_or_verify_transport_json(profile_path, profile)
    first_payload, first_bytes = _replay_once(raw, token=token)
    second_payload, second_bytes = _replay_once(raw, token=token)
    if first_bytes != second_bytes or first_payload != second_payload:
        raise IndexWeightValueDiagnosticError("deterministic_replay_mismatch")
    normalized_path = run_directory / "normalized_pit.json"
    _publish_or_verify_transport_json(normalized_path, first_payload)

    replay = {
        "schema_version": "tushare-index-weight-offline-replay.v1",
        "raw_transport_sha256": scan["raw_transport_sha256"],
        "value_profile_sha256": hashlib.sha256(profile_bytes).hexdigest(),
        "normalization_change": change_id,
        "offline_replay_status": "DIAGNOSTIC_REPLAY_ACCEPTED",
        "replay_pass_count": 2,
        "deterministic_replay": True,
        "normalized_row_count": first_payload["row_count"],
        "normalized_trade_dates": first_payload["trade_dates"],
        "normalized_weight_sum_by_date": first_payload["weight_sum_by_trade_date"],
        "normalized_content_sha256": first_payload["normalized_content_sha256"],
        "normalized_pit_sha256": hashlib.sha256(first_bytes).hexdigest(),
        "terminal_status": "DIAGNOSTIC_REPLAY_ACCEPTED",
        "locked_test_status": dict(LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
    }
    try:
        validate_json_schema(replay, REPLAY_SCHEMA_PATH)
    except SchemaValidationError as exc:
        raise IndexWeightValueDiagnosticError("offline_replay_schema_invalid") from exc
    replay_bytes = _publish_or_verify_transport_json(
        run_directory / "offline_replay.json", replay
    )
    return {
        **replay,
        "offline_replay_path": str(run_directory / "offline_replay.json"),
        "offline_replay_sha256": hashlib.sha256(replay_bytes).hexdigest(),
        "normalized_pit_path": str(normalized_path),
    }


def read_token_from_environment(environ: Mapping[str, str] | None = None) -> str:
    source = os.environ if environ is None else environ
    token = source.get("TUSHARE_TOKEN")
    try:
        return alpha_data._validate_token(token)
    except alpha_data.AlphaFeasibilityDataError as exc:
        raise IndexWeightValueDiagnosticError("credential_preflight_failed") from exc


__all__ = [
    "DEFAULT_OUTPUT_ROOT",
    "ENDPOINT",
    "FIXED_PARAMS",
    "INDEX_CODE",
    "IndexWeightValueDiagnosticError",
    "LOCKED_TEST_STATUS",
    "MAXIMUM_RESPONSE_BYTES",
    "REQUESTED_FIELDS",
    "build_value_profile",
    "collect_live_once",
    "diagnose_current_response_failure",
    "read_token_from_environment",
    "regenerate_value_profile",
    "replay_saved_response",
    "scan_raw_response",
]
