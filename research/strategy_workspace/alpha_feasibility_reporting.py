"""Fail-closed reporting for the pre-Locked Alpha Feasibility experiment.

The module deliberately consumes plain mappings.  It never reads raw market
data or backtest rows, and it never opens a Locked Test path.  Its only jobs are
to bind already-aggregated Development/Validation evidence to the frozen V2
experiment, derive the pre-registered terminal gate, and publish one immutable
self-hashed report.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any

from research.market_data.validation import SchemaValidationError, validate_json_schema
from research.strategy_workspace.contracts import canonical_json_bytes, canonical_sha256


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = (
    REPOSITORY_ROOT / "configs" / "a_share_technical_alpha_feasibility.v2.json"
)
EXPERIMENT_SCHEMA_PATH = (
    REPOSITORY_ROOT / "schemas" / "technical_alpha_feasibility_experiment.v2.json"
)
REPORT_SCHEMA_PATH = (
    REPOSITORY_ROOT / "schemas" / "technical_alpha_feasibility_report.v3.json"
)
ALPHA_FEASIBILITY_ENGINE_PATH = (
    REPOSITORY_ROOT / "research" / "strategy_workspace" / "alpha_feasibility.py"
)

REPORT_FILENAME = "alpha_feasibility_report.json"
REPORT_SCHEMA_VERSION = "technical-alpha-feasibility-report.v3"
EXPERIMENT_ID = "a-share-technical-alpha-feasibility-tushare-p1-v1"
COVERAGE_START = "2017-07-01"
COVERAGE_END = "2023-12-31"
PIT_MONTHS_EXPECTED = 73
CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

STOCK_BASIC_STATUS = "DEFERRED_NOT_REQUIRED_FOR_ALPHA_FEASIBILITY"
STOCK_BASIC_REQUEST_COUNT = 0
SECURITY_MASTER_PIT_STATUS = "NOT_IMPLEMENTED_NOT_REQUIRED_IN_P1"
REQUIRED_INDEX_WEIGHT_FIELDS = (
    "index_code",
    "con_code",
    "trade_date",
    "weight",
)
_SAFE_DATA_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_DIAGNOSTIC_DATA_FIELD = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_]{0,63}|sha256:[0-9a-f]{64}|"
    r"type:(?:null|boolean|integer|number|string|array|object|unknown))$"
)

ALLOWED_ENDPOINTS = (
    "trade_cal",
    "index_weight",
    "daily",
    "adj_factor",
    "index_daily",
    "suspend_d",
)
ADAPTER_PROTOCOL_BLOCKERS = frozenset(
    {
        "duplicate_json_key",
        "semantic_core_missing",
        "semantic_core_type_invalid",
        "response_body_too_large",
        "transport_extensions_too_large",
        "transport_extensions_too_deep",
        "transport_extension_secret_detected",
        "data_payload_invalid",
        "data_fields_not_array",
        "data_field_name_invalid",
        "data_duplicate_fields",
        "data_required_fields_missing",
        "data_item_width_mismatch",
        "data_required_value_invalid",
        "unknown_non_json_value",
    }
)

LOCKED_TEST_STATUS = {
    "access": "NOT_ACCESSED",
    "download": "NOT_DOWNLOADED",
    "run": "NOT_RUN",
}
LOCKED_TEST_CONSUMED = False
SAFETY = {
    "research_status": "research_alpha_feasibility_only",
    "execution_realism": "INCOMPLETE",
    "paper_eligibility": False,
    "trade_eligibility": False,
    "automatic_order_submission": False,
    "live_supported": False,
}

FROZEN_IMPLEMENTATION = {
    "alpha_policy_path": "configs/a_share_technical_shadow_mvp.v1.json",
    "alpha_policy_sha256": "53b7f2b3da72a2d393c18b4fc61afac9e1a3f63c2cd86756cbd1bd0d47eb77ea",
    "alpha_source_path": "research/strategy_workspace/technical_alpha_shadow_v1.py",
    "alpha_source_sha256": "3cd734c8770e5647754fa21d65e8d6a789da3c17958fb2b2b15352268af3d922",
    "ranker_source_path": "research/strategy_workspace/technical_formal_backtest.py",
    "ranker_source_sha256": "33cd919f8928a532caa798341c2656d423e68655befff6792aa4223a281a31d3",
    "exposure_source_path": "research/strategy_workspace/technical_exposure_shadow_v1.py",
    "exposure_source_sha256": "4a204237752dec4797c2f80cf5950d638aa4d638f2ece615a29ace62f14d0ca7",
}
DATES = {
    "signal_warmup_start": "2017-07-01",
    "development_start": "2018-01-01",
    "development_end": "2022-12-31",
    "validation_start": "2023-01-01",
    "validation_end": "2023-12-31",
    "absolute_request_and_consumer_cutoff": "2023-12-31",
    "terminal_session_has_no_cross_cutoff_next_session": True,
}
PORTFOLIO = {
    "fractional_weights": True,
    "initial_nav": "1",
    "max_positions": 3,
    "max_position_weight": "0.40",
    "candidate_shortage_policy": "preserve_cash",
    "gross_exposure": {
        "RISK_OFF": "0.00",
        "DEFENSIVE": "0.30",
        "NEUTRAL": "0.60",
        "RISK_ON": "1.00",
    },
}
FEASIBILITY_TIMING = {
    "decision_time": "D_close",
    "rebalance_time": "D_plus_1_open",
    "held_interval_return": (
        "adjusted_close_to_adjusted_open_then_adjusted_open_to_adjusted_close"
    ),
    "new_target_return": "D_plus_1_adjusted_open_to_adjusted_close",
    "broker_fill_simulation": False,
}
COSTS = {
    "minimum_commission_mode": "not_modeled_for_fractional_normalized_nav",
    "base": {
        "commission_rate": "0.00018",
        "sell_tax_rate": "0.0005",
        "transfer_fee_rate_both_sides": "0.00001",
        "slippage_bps_one_way": "10",
        "commission_multiplier": "1",
    },
    "stress": {
        "commission_rate": "0.00018",
        "sell_tax_rate": "0.0005",
        "transfer_fee_rate_both_sides": "0.00001",
        "slippage_bps_one_way": "20",
        "commission_multiplier": "2",
    },
}
GATE = {
    "validation_base_net_active_return_min_exclusive": "0",
    "validation_stress_net_active_return_min_inclusive": "0",
    "validation_max_drawdown_max_inclusive": "0.12",
    "largest_stock_pnl_share_max_inclusive": "0.50",
    "largest_10_days_pnl_share_max_inclusive": "0.50",
    "concentration_warning_blocks_go_candidate": True,
    "parameter_search_forbidden": True,
    "validation_driven_retraining_forbidden": True,
}

# This is the canonical-JSON hash of the complete checked-in V2 experiment.
# It makes otherwise loose object sections in the JSON Schema fail closed too,
# while remaining insensitive to whitespace and object key ordering.
EXPECTED_EXPERIMENT_CANONICAL_SHA256 = (
    "358e2848d35084c4ec7dbf53abe0086e103992fe9b43c5e9f0464cf1e79f48de"
)

METRIC_FIELDS = (
    "net_return",
    "benchmark_return",
    "net_active_return",
    "max_drawdown",
    "annualized_turnover",
    "total_cost",
    "average_gross_exposure",
    "cash_day_fraction",
    "exposure_state_distribution",
    "trade_or_rebalance_count",
    "positive_month_rate",
    "positive_half_year_count",
    "worst_month",
    "per_stock_pnl_contribution",
    "largest_stock_pnl_share",
    "largest_10_days_pnl_share",
)
SCENARIOS = ("base", "stress")
EXPOSURE_STATES = ("RISK_OFF", "DEFENSIVE", "NEUTRAL", "RISK_ON")
COVERAGE_STATUS_FIELDS = (
    "daily_coverage_status",
    "adj_factor_coverage_status",
    "suspension_coverage_status",
    "benchmark_coverage_status",
)
_DATASET_FOR_STATUS = {
    "daily_coverage_status": "daily",
    "adj_factor_coverage_status": "adj_factor",
    "suspension_coverage_status": "suspension",
    "benchmark_coverage_status": "benchmark",
}
_INSTRUMENT_RE = re.compile(r"^[0-9]{6}\.(?:SH|SZ)$")
_CANONICAL_DECIMAL_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_DATE_RE = re.compile(r"(?<![0-9])([0-9]{4})-([0-9]{2})(?:-([0-9]{2}))?(?![0-9])")
_COMPACT_DATE_RE = re.compile(
    r"(?<![0-9A-Za-z])([0-9]{4})([0-9]{2})([0-9]{2})(?![0-9A-Za-z])"
)
_COMPACT_BLOCKER_DATE_RE = re.compile(
    r"(?<![0-9])(20[0-9]{2})(0[1-9]|1[0-2])([0-3][0-9])(?![0-9])"
)


class AlphaFeasibilityReportingError(ValueError):
    """Raised when reporting evidence weakens or drifts from the frozen gate."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AlphaFeasibilityReportingError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json_file(path: Path | str, label: str) -> dict[str, Any]:
    candidate = Path(path)
    try:
        raw = candidate.read_bytes()
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant: {item}")
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise AlphaFeasibilityReportingError(
            f"{label} must be readable strict UTF-8 JSON"
        ) from exc
    if not isinstance(value, dict):
        raise AlphaFeasibilityReportingError(f"{label} root must be an object")
    return value


def _file_sha256(path: Path) -> str:
    digest = sha256()
    try:
        with path.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                digest.update(block)
    except OSError as exc:
        raise AlphaFeasibilityReportingError(
            f"unable to hash frozen implementation: {path.as_posix()}"
        ) from exc
    return digest.hexdigest()


def _require_exact(
    experiment: Mapping[str, Any], field: str, expected: Any
) -> None:
    if experiment.get(field) != expected:
        raise AlphaFeasibilityReportingError(f"experiment config {field} drift")


def _require_exact_typed(
    supplied: Mapping[str, Any],
    field: str,
    expected: Any,
    *,
    context: str,
) -> Any:
    value = supplied.get(field)
    if type(value) is not type(expected) or value != expected:
        raise AlphaFeasibilityReportingError(f"{context} {field} drift")
    return value


def validate_experiment_config(experiment: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the whole V2 config and the bytes of all frozen files."""

    if not isinstance(experiment, Mapping):
        raise AlphaFeasibilityReportingError("experiment config must be a mapping")
    candidate = dict(experiment)
    try:
        validate_json_schema(candidate, EXPERIMENT_SCHEMA_PATH)
    except SchemaValidationError as exc:
        raise AlphaFeasibilityReportingError(
            f"experiment config schema violation: {exc}"
        ) from exc

    _require_exact(candidate, "schema_version", "technical-alpha-feasibility-experiment.v2")
    _require_exact(candidate, "experiment_id", EXPERIMENT_ID)
    _require_exact(candidate, "strategy_id", "a-share-technical-momentum-adaptive-v1")
    _require_exact(candidate, "research_status", "research_alpha_feasibility_only")
    _require_exact_typed(
        candidate,
        "stock_basic_status",
        STOCK_BASIC_STATUS,
        context="experiment config",
    )
    _require_exact_typed(
        candidate,
        "stock_basic_request_count",
        STOCK_BASIC_REQUEST_COUNT,
        context="experiment config",
    )
    _require_exact_typed(
        candidate,
        "security_master_pit_status",
        SECURITY_MASTER_PIT_STATUS,
        context="experiment config",
    )
    _require_exact(candidate, "frozen_implementation", FROZEN_IMPLEMENTATION)
    _require_exact(candidate, "dates", DATES)
    _require_exact(candidate, "portfolio", PORTFOLIO)
    _require_exact(candidate, "feasibility_timing", FEASIBILITY_TIMING)
    _require_exact(candidate, "costs", COSTS)
    _require_exact(candidate, "gate", GATE)
    _require_exact(candidate, "locked_test_status", LOCKED_TEST_STATUS)
    _require_exact(candidate, "locked_test_consumed", LOCKED_TEST_CONSUMED)
    _require_exact(candidate, "safety", SAFETY)

    source = candidate.get("source")
    requests = candidate.get("requests")
    if not isinstance(source, Mapping) or tuple(source.get("allowed_endpoints", ())) != ALLOWED_ENDPOINTS:
        raise AlphaFeasibilityReportingError("experiment config allowed endpoints drift")
    if not isinstance(requests, Mapping) or set(requests) != set(ALLOWED_ENDPOINTS):
        raise AlphaFeasibilityReportingError("experiment config request endpoints drift")
    if canonical_sha256(candidate) != EXPECTED_EXPERIMENT_CANONICAL_SHA256:
        raise AlphaFeasibilityReportingError("experiment config canonical content drift")

    for prefix in ("alpha_policy", "alpha_source", "ranker_source", "exposure_source"):
        relative_path = FROZEN_IMPLEMENTATION[f"{prefix}_path"]
        expected_hash = FROZEN_IMPLEMENTATION[f"{prefix}_sha256"]
        frozen_path = (REPOSITORY_ROOT / relative_path).resolve()
        try:
            frozen_path.relative_to(REPOSITORY_ROOT.resolve())
        except ValueError as exc:
            raise AlphaFeasibilityReportingError(
                f"frozen implementation path leaves repository: {relative_path}"
            ) from exc
        if _file_sha256(frozen_path) != expected_hash:
            raise AlphaFeasibilityReportingError(
                f"frozen implementation hash drift: {relative_path}"
            )
    return candidate


def load_and_validate_experiment_config(
    config_path: Path | str = DEFAULT_CONFIG_PATH,
) -> dict[str, Any]:
    """Read strict JSON and validate it against both Schema and frozen truth."""

    return validate_experiment_config(_load_json_file(config_path, "experiment config"))


def _resolve_experiment(
    experiment: Mapping[str, Any] | None,
    config_path: Path | str,
) -> dict[str, Any]:
    if experiment is None:
        return load_and_validate_experiment_config(config_path)
    return validate_experiment_config(experiment)


def _timestamp(value: datetime | str | None) -> str:
    if value is None:
        parsed = datetime.now(CHINA_TZ)
    elif isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError as exc:
            raise AlphaFeasibilityReportingError(
                "generated_at must be an ISO date-time"
            ) from exc
    else:
        raise AlphaFeasibilityReportingError("generated_at must be a date-time")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AlphaFeasibilityReportingError("generated_at must include a timezone offset")
    return parsed.isoformat()


def _commit_sha(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise AlphaFeasibilityReportingError(
            "commit_sha must be an exact lower-case 40-character Git SHA"
        )
    return value


def _sha256_or_none(value: Any, field: str, *, allow_none: bool) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise AlphaFeasibilityReportingError(
            f"{field} must be a lower-case SHA-256"
        )
    return value


def _runtime_provenance() -> dict[str, str]:
    from research.strategy_workspace import alpha_feasibility as engine

    if engine.ENGINE_VERSION != "alpha-feasibility.v1":
        raise AlphaFeasibilityReportingError("alpha feasibility engine version drift")
    return {
        "experiment_config_canonical_sha256": EXPECTED_EXPERIMENT_CANONICAL_SHA256,
        "alpha_feasibility_engine_version": engine.ENGINE_VERSION,
        "alpha_feasibility_engine_sha256": _file_sha256(
            ALPHA_FEASIBILITY_ENGINE_PATH
        ),
        "reporting_gate_source_sha256": _file_sha256(Path(__file__).resolve()),
    }


def _decimal_number(value: Any, field: str) -> Decimal:
    if isinstance(value, str):
        if _CANONICAL_DECIMAL_RE.fullmatch(value) is None:
            raise AlphaFeasibilityReportingError(
                f"{field} must be a canonical finite decimal"
            )
        numeric = Decimal(value)
    elif isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise AlphaFeasibilityReportingError(f"{field} must be a finite number")
    else:
        numeric = value if isinstance(value, Decimal) else Decimal(str(value))
    if not numeric.is_finite():
        raise AlphaFeasibilityReportingError(f"{field} must be a finite number")
    return numeric


def _canonical_decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _finite_number(value: Any, field: str) -> str:
    return _canonical_decimal(_decimal_number(value, field))


def _bounded_number(
    value: Any,
    field: str,
    minimum: Decimal | int | str,
    maximum: Decimal | int | str | None = None,
) -> str:
    numeric = _decimal_number(value, field)
    minimum_decimal = Decimal(str(minimum))
    maximum_decimal = None if maximum is None else Decimal(str(maximum))
    if (
        numeric < minimum_decimal
        or maximum_decimal is not None
        and numeric > maximum_decimal
    ):
        suffix = f" and <= {maximum}" if maximum is not None else ""
        raise AlphaFeasibilityReportingError(
            f"{field} must be >= {minimum}{suffix}"
        )
    return _canonical_decimal(numeric)


def _nonnegative_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AlphaFeasibilityReportingError(f"{field} must be a non-negative integer")
    return value


def _unique_strings(values: Any, field: str) -> list[str]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise AlphaFeasibilityReportingError(f"{field} must be an array")
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value or value != value.strip():
            raise AlphaFeasibilityReportingError(
                f"{field} must contain exact non-empty strings"
            )
        result.append(value)
    if len(result) != len(set(result)):
        raise AlphaFeasibilityReportingError(f"{field} values must be unique")
    return sorted(result)


def _safe_field_array(values: Any, field: str) -> list[str]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise AlphaFeasibilityReportingError(f"{field} must be an array")
    result: list[str] = []
    for value in values:
        if type(value) is not str or _SAFE_DATA_FIELD.fullmatch(value) is None:
            raise AlphaFeasibilityReportingError(
                f"{field} contains an unsafe data field name"
            )
        result.append(value)
    if len(result) != len(set(result)):
        raise AlphaFeasibilityReportingError(f"{field} values must be unique")
    return result


def _diagnostic_field_array(values: Any, field: str) -> list[str]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise AlphaFeasibilityReportingError(f"{field} must be an array")
    result: list[str] = []
    for value in values:
        if type(value) is not str or _DIAGNOSTIC_DATA_FIELD.fullmatch(value) is None:
            raise AlphaFeasibilityReportingError(
                f"{field} contains an unsafe diagnostic field name"
            )
        result.append(value)
    return result


def _first_index_weight(value: Any, *, allow_none: bool) -> dict[str, Any] | None:
    if value is None:
        if allow_none:
            return None
        raise AlphaFeasibilityReportingError("first_index_weight is required")
    required_keys = {
        "observed_data_fields",
        "required_data_fields",
        "missing_required_data_fields",
        "extra_data_fields",
        "field_order_matches_canonical",
        "data_row_count",
        "provider_payload_sha256",
        "normalized_content_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != required_keys:
        raise AlphaFeasibilityReportingError(
            "first_index_weight must contain the exact safe evidence fields"
        )
    observed = _diagnostic_field_array(
        value["observed_data_fields"], "first_index_weight.observed_data_fields"
    )
    required = _safe_field_array(
        value["required_data_fields"], "first_index_weight.required_data_fields"
    )
    missing = _safe_field_array(
        value["missing_required_data_fields"],
        "first_index_weight.missing_required_data_fields",
    )
    extra = _safe_field_array(
        value["extra_data_fields"], "first_index_weight.extra_data_fields"
    )
    if tuple(required) != REQUIRED_INDEX_WEIGHT_FIELDS:
        raise AlphaFeasibilityReportingError(
            "first_index_weight required fields differ from the canonical projection"
        )
    safe_observed = [
        field for field in observed if _SAFE_DATA_FIELD.fullmatch(field) is not None
    ]
    observed_set = set(safe_observed)
    required_set = set(required)
    if (
        not set(missing).issubset(required_set)
        or set(extra) != observed_set - required_set
        or set(missing) != required_set - observed_set
    ):
        raise AlphaFeasibilityReportingError(
            "first_index_weight field diagnostics are inconsistent"
        )
    order_matches = value["field_order_matches_canonical"]
    if type(order_matches) is not bool:
        raise AlphaFeasibilityReportingError(
            "first_index_weight.field_order_matches_canonical must be boolean"
        )
    observed_required_order = [
        field for field in safe_observed if field in required_set
    ]
    if order_matches != (observed_required_order == required):
        raise AlphaFeasibilityReportingError(
            "first_index_weight field-order diagnostic is inconsistent"
        )
    provider_hash = _sha256_or_none(
        value["provider_payload_sha256"],
        "first_index_weight.provider_payload_sha256",
        allow_none=True,
    )
    normalized_hash = _sha256_or_none(
        value["normalized_content_sha256"],
        "first_index_weight.normalized_content_sha256",
        allow_none=True,
    )
    if normalized_hash is not None and (missing or provider_hash is None):
        raise AlphaFeasibilityReportingError(
            "normalized first index_weight evidence requires a complete provider payload"
        )
    return {
        "observed_data_fields": observed,
        "required_data_fields": required,
        "missing_required_data_fields": missing,
        "extra_data_fields": extra,
        "field_order_matches_canonical": order_matches,
        "data_row_count": _nonnegative_integer(
            value["data_row_count"], "first_index_weight.data_row_count"
        ),
        "provider_payload_sha256": provider_hash,
        "normalized_content_sha256": normalized_hash,
    }


def normalize_first_index_weight_evidence(
    value: Any, *, allow_none: bool = False
) -> dict[str, Any] | None:
    """Expose the report's safe first-request evidence boundary to the runner."""

    return _first_index_weight(value, allow_none=allow_none)


def _request_counts(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != set(ALLOWED_ENDPOINTS):
        raise AlphaFeasibilityReportingError(
            "actual_tushare_request_count_by_endpoint must contain exactly the six allowed endpoints"
        )
    return {
        endpoint: _nonnegative_integer(value[endpoint], f"request count {endpoint}")
        for endpoint in ALLOWED_ENDPOINTS
    }


def _fixed_data_contract(
    summary: Mapping[str, Any], *, blocked_defaults: bool
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field, expected in (
        ("stock_basic_status", STOCK_BASIC_STATUS),
        ("stock_basic_request_count", STOCK_BASIC_REQUEST_COUNT),
        ("security_master_pit_status", SECURITY_MASTER_PIT_STATUS),
    ):
        if field not in summary:
            if not blocked_defaults:
                raise AlphaFeasibilityReportingError(
                    f"data summary {field} is missing"
                )
            result[field] = expected
            continue
        result[field] = _require_exact_typed(
            summary,
            field,
            expected,
            context="data summary",
        )
    return result


def _decision_count_map(value: Any, field: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise AlphaFeasibilityReportingError(f"{field} must be a mapping")
    cutoff = date.fromisoformat(COVERAGE_END)
    normalized: dict[str, int] = {}
    for decision_key, count in value.items():
        if not isinstance(decision_key, str):
            raise AlphaFeasibilityReportingError(
                f"{field} keys must be ISO dates"
            )
        try:
            decision_date = date.fromisoformat(decision_key)
        except ValueError as exc:
            raise AlphaFeasibilityReportingError(
                f"{field} keys must be valid ISO dates"
            ) from exc
        if decision_date.isoformat() != decision_key:
            raise AlphaFeasibilityReportingError(
                f"{field} keys must be canonical ISO dates"
            )
        if decision_date > cutoff:
            raise AlphaFeasibilityReportingError(
                f"{field} contains a decision date after {COVERAGE_END}"
            )
        normalized[decision_key] = _nonnegative_integer(
            count,
            f"{field}[{decision_key}]",
        )
    return {key: normalized[key] for key in sorted(normalized)}


def _coverage_status(summary: Mapping[str, Any], field: str, *, blocked_defaults: bool) -> str:
    supplied = summary.get(field)
    if supplied is None:
        datasets = summary.get("datasets")
        if isinstance(datasets, Mapping):
            dataset = datasets.get(_DATASET_FOR_STATUS[field])
            if isinstance(dataset, Mapping):
                raw_status = dataset.get("status")
                supplied = "complete" if raw_status == "complete" else "blocked"
    supplied = {
        "COMPLETE": "complete",
        "BLOCKED_DATA": "blocked",
        "NOT_RUN": "not_run",
    }.get(supplied, supplied)
    if supplied is None and blocked_defaults:
        supplied = "not_run"
    if supplied not in {"complete", "blocked", "not_run"}:
        raise AlphaFeasibilityReportingError(f"{field} is invalid or missing")
    return supplied


def _normalize_data_summary(
    supplied: Mapping[str, Any] | None,
    *,
    blocked_defaults: bool,
) -> dict[str, Any]:
    summary: Mapping[str, Any] = {} if supplied is None else supplied
    if not isinstance(summary, Mapping):
        raise AlphaFeasibilityReportingError("data_summary must be a mapping")

    if summary.get("coverage_start", COVERAGE_START) != COVERAGE_START:
        raise AlphaFeasibilityReportingError("coverage_start differs from frozen boundary")
    if summary.get("coverage_end", COVERAGE_END) != COVERAGE_END:
        raise AlphaFeasibilityReportingError("coverage_end differs from frozen boundary")
    if summary.get("pit_months_expected", PIT_MONTHS_EXPECTED) != PIT_MONTHS_EXPECTED:
        raise AlphaFeasibilityReportingError("pit_months_expected differs from frozen range")

    fixed_data_contract = _fixed_data_contract(
        summary,
        blocked_defaults=blocked_defaults,
    )
    default_counts = {endpoint: 0 for endpoint in ALLOWED_ENDPOINTS}
    raw_counts = summary.get("actual_tushare_request_count_by_endpoint")
    if raw_counts is None and blocked_defaults:
        raw_counts = default_counts
    counts = _request_counts(raw_counts)

    pit_observed = summary.get("pit_months_observed", 0 if blocked_defaults else None)
    union_count = summary.get("union_instrument_count", 0 if blocked_defaults else None)
    pit_observed = _nonnegative_integer(pit_observed, "pit_months_observed")
    union_count = _nonnegative_integer(union_count, "union_instrument_count")
    if pit_observed > PIT_MONTHS_EXPECTED:
        raise AlphaFeasibilityReportingError("pit_months_observed exceeds expected months")

    raw_valid_candidate_counts = summary.get("valid_candidate_count_by_decision")
    raw_insufficient_history_counts = summary.get(
        "insufficient_history_count_by_decision"
    )
    if blocked_defaults:
        if raw_valid_candidate_counts is None:
            raw_valid_candidate_counts = {}
        if raw_insufficient_history_counts is None:
            raw_insufficient_history_counts = {}
    valid_candidate_counts = _decision_count_map(
        raw_valid_candidate_counts,
        "valid_candidate_count_by_decision",
    )
    insufficient_history_counts = _decision_count_map(
        raw_insufficient_history_counts,
        "insufficient_history_count_by_decision",
    )
    no_initial_price_count = _nonnegative_integer(
        summary.get(
            "ineligible_no_initial_price_count",
            0 if blocked_defaults else None,
        ),
        "ineligible_no_initial_price_count",
    )
    unexplained_gap_count = _nonnegative_integer(
        summary.get(
            "unexplained_market_data_gap_count",
            0 if blocked_defaults else None,
        ),
        "unexplained_market_data_gap_count",
    )

    blocked_status = summary.get("data_status") in {
        "BLOCKED_PIT_MEMBERSHIP",
        "BLOCKED_DATA",
        "BLOCKED_ADAPTER_PROTOCOL",
    } or summary.get("terminal_status") in {
        "BLOCKED_DATA",
        "BLOCKED_ADAPTER_PROTOCOL",
    }
    first_index_weight = _first_index_weight(
        summary.get("first_index_weight"),
        allow_none=blocked_defaults or blocked_status,
    )

    allow_missing_provenance = (
        blocked_defaults
        or summary.get("data_status")
        in {"BLOCKED_PIT_MEMBERSHIP", "BLOCKED_DATA", "BLOCKED_ADAPTER_PROTOCOL"}
        or summary.get("terminal_status")
        in {"BLOCKED_DATA", "BLOCKED_ADAPTER_PROTOCOL"}
    )
    provenance = {
        "collection_plan_sha256": _sha256_or_none(
            summary.get("collection_plan_sha256"),
            "collection_plan_sha256",
            allow_none=allow_missing_provenance,
        ),
        "pit_membership_manifest_sha256": _sha256_or_none(
            summary.get("pit_membership_manifest_sha256"),
            "pit_membership_manifest_sha256",
            allow_none=allow_missing_provenance,
        ),
        "history_manifest_sha256": _sha256_or_none(
            summary.get("history_manifest_sha256"),
            "history_manifest_sha256",
            allow_none=allow_missing_provenance,
        ),
    }

    if "locked_test_status" in summary and summary["locked_test_status"] != LOCKED_TEST_STATUS:
        raise AlphaFeasibilityReportingError("data summary locked_test_status drift")
    if "locked_test_consumed" in summary and summary["locked_test_consumed"] is not False:
        raise AlphaFeasibilityReportingError("data summary consumed Locked Test data")
    if "safety" in summary and summary["safety"] != SAFETY:
        raise AlphaFeasibilityReportingError("data summary safety drift")
    data_status = summary.get("data_status")
    if data_status is not None and data_status not in {
        "READY",
        "BLOCKED_PIT_MEMBERSHIP",
        "BLOCKED_DATA",
        "BLOCKED_ADAPTER_PROTOCOL",
    }:
        raise AlphaFeasibilityReportingError("data summary data_status is invalid")

    return {
        "actual_tushare_request_count_by_endpoint": counts,
        "first_index_weight": first_index_weight,
        **fixed_data_contract,
        "coverage_start": COVERAGE_START,
        "coverage_end": COVERAGE_END,
        "pit_months_expected": PIT_MONTHS_EXPECTED,
        "pit_months_observed": pit_observed,
        "union_instrument_count": union_count,
        "valid_candidate_count_by_decision": valid_candidate_counts,
        "insufficient_history_count_by_decision": insufficient_history_counts,
        "ineligible_no_initial_price_count": no_initial_price_count,
        "unexplained_market_data_gap_count": unexplained_gap_count,
        **provenance,
        **{
            field: _coverage_status(summary, field, blocked_defaults=blocked_defaults)
            for field in COVERAGE_STATUS_FIELDS
        },
        "data_status": data_status,
        "remaining_blockers": _unique_strings(
            summary.get("remaining_blockers", []),
            "data_summary.remaining_blockers",
        ),
    }


def _worst_month(value: Any, split: str, scenario: str) -> dict[str, Any]:
    field = f"{split}.{scenario}.worst_month"
    expected = {"month", "net_return", "benchmark_return", "net_active_return"}
    if not isinstance(value, Mapping) or set(value) != expected:
        raise AlphaFeasibilityReportingError(f"{field} must contain exactly {sorted(expected)}")
    month = value["month"]
    if not isinstance(month, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}", month) is None:
        raise AlphaFeasibilityReportingError(f"{field}.month must be YYYY-MM")
    start = DATES[f"{split}_start"][:7]
    end = DATES[f"{split}_end"][:7]
    if month < start or month > end:
        raise AlphaFeasibilityReportingError(f"{field}.month is outside the {split} split")
    return {
        "month": month,
        "net_return": _finite_number(value["net_return"], f"{field}.net_return"),
        "benchmark_return": _finite_number(
            value["benchmark_return"], f"{field}.benchmark_return"
        ),
        "net_active_return": _finite_number(
            value["net_active_return"], f"{field}.net_active_return"
        ),
    }


def _normalize_metrics(value: Any, split: str, scenario: str) -> dict[str, Any]:
    field = f"{split}.{scenario}"
    if not isinstance(value, Mapping) or set(value) != set(METRIC_FIELDS):
        missing = sorted(set(METRIC_FIELDS) - set(value) if isinstance(value, Mapping) else METRIC_FIELDS)
        unknown = sorted(set(value) - set(METRIC_FIELDS)) if isinstance(value, Mapping) else []
        raise AlphaFeasibilityReportingError(
            f"{field} metrics mismatch; missing={missing}, unknown={unknown}"
        )

    distribution = value["exposure_state_distribution"]
    if not isinstance(distribution, Mapping) or set(distribution) != set(EXPOSURE_STATES):
        raise AlphaFeasibilityReportingError(
            f"{field}.exposure_state_distribution must contain exactly four frozen states"
        )
    normalized_distribution = {
        state: _bounded_number(
            distribution[state],
            f"{field}.exposure_state_distribution.{state}",
            Decimal("0"),
            Decimal("1"),
        )
        for state in EXPOSURE_STATES
    }
    distribution_total = sum(
        (Decimal(item) for item in normalized_distribution.values()),
        Decimal("0"),
    )
    if abs(distribution_total - Decimal("1")) > Decimal("1e-9"):
        raise AlphaFeasibilityReportingError(
            f"{field}.exposure_state_distribution must sum to 1"
        )

    contributions = value["per_stock_pnl_contribution"]
    if not isinstance(contributions, Mapping):
        raise AlphaFeasibilityReportingError(
            f"{field}.per_stock_pnl_contribution must be a mapping"
        )
    normalized_contributions: dict[str, str] = {}
    for instrument, contribution in sorted(contributions.items()):
        if not isinstance(instrument, str) or _INSTRUMENT_RE.fullmatch(instrument) is None:
            raise AlphaFeasibilityReportingError(
                f"{field}.per_stock_pnl_contribution has an invalid instrument"
            )
        normalized_contributions[instrument] = _finite_number(
            contribution, f"{field}.per_stock_pnl_contribution.{instrument}"
        )

    shares: dict[str, str | None] = {}
    for share_field in ("largest_stock_pnl_share", "largest_10_days_pnl_share"):
        share = value[share_field]
        shares[share_field] = (
            None
            if share is None
            else _bounded_number(
                share,
                f"{field}.{share_field}",
                Decimal("0"),
                Decimal("1"),
            )
        )

    return {
        "net_return": _finite_number(value["net_return"], f"{field}.net_return"),
        "benchmark_return": _finite_number(
            value["benchmark_return"], f"{field}.benchmark_return"
        ),
        "net_active_return": _finite_number(
            value["net_active_return"], f"{field}.net_active_return"
        ),
        "max_drawdown": _bounded_number(
            value["max_drawdown"], f"{field}.max_drawdown", Decimal("0")
        ),
        "annualized_turnover": _bounded_number(
            value["annualized_turnover"],
            f"{field}.annualized_turnover",
            Decimal("0"),
        ),
        "total_cost": _bounded_number(
            value["total_cost"], f"{field}.total_cost", Decimal("0")
        ),
        "average_gross_exposure": _bounded_number(
            value["average_gross_exposure"],
            f"{field}.average_gross_exposure",
            Decimal("0"),
            Decimal("1"),
        ),
        "cash_day_fraction": _bounded_number(
            value["cash_day_fraction"],
            f"{field}.cash_day_fraction",
            Decimal("0"),
            Decimal("1"),
        ),
        "exposure_state_distribution": normalized_distribution,
        "trade_or_rebalance_count": _nonnegative_integer(
            value["trade_or_rebalance_count"], f"{field}.trade_or_rebalance_count"
        ),
        "positive_month_rate": _bounded_number(
            value["positive_month_rate"],
            f"{field}.positive_month_rate",
            Decimal("0"),
            Decimal("1"),
        ),
        "positive_half_year_count": _nonnegative_integer(
            value["positive_half_year_count"], f"{field}.positive_half_year_count"
        ),
        "worst_month": _worst_month(value["worst_month"], split, scenario),
        "per_stock_pnl_contribution": normalized_contributions,
        **shares,
    }


def _normalize_scenarios(value: Any, split: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping) or set(value) != set(SCENARIOS):
        raise AlphaFeasibilityReportingError(
            f"{split}_metrics must contain exactly base and stress"
        )
    return {
        scenario: _normalize_metrics(value[scenario], split, scenario)
        for scenario in SCENARIOS
    }


def build_concentration_metrics(
    validation_metrics: Mapping[str, Any],
    *,
    experiment: Mapping[str, Any] | None = None,
    config_path: Path | str = DEFAULT_CONFIG_PATH,
) -> dict[str, Any]:
    """Return the worst Validation concentration across base and stress."""

    frozen = _resolve_experiment(experiment, config_path)
    validation = _normalize_scenarios(validation_metrics, "validation")
    stock_threshold = Decimal(
        frozen["gate"]["largest_stock_pnl_share_max_inclusive"]
    )
    days_threshold = Decimal(
        frozen["gate"]["largest_10_days_pnl_share_max_inclusive"]
    )
    issues: list[str] = []

    def worst(field: str, threshold: Decimal) -> str | None:
        values: list[Decimal] = []
        missing = False
        for scenario in SCENARIOS:
            value = validation[scenario][field]
            if value is None:
                missing = True
                issues.append(f"validation_{scenario}_{field}_missing")
            else:
                exact_value = Decimal(value)
                values.append(exact_value)
                if exact_value > threshold:
                    issues.append(f"validation_{scenario}_{field}_exceeds_threshold")
        return None if missing else _canonical_decimal(max(values))

    largest_stock = worst("largest_stock_pnl_share", stock_threshold)
    largest_days = worst("largest_10_days_pnl_share", days_threshold)
    issues = sorted(set(issues))
    return {
        "largest_stock_pnl_share": largest_stock,
        "largest_stock_threshold": _canonical_decimal(stock_threshold),
        "largest_10_days_pnl_share": largest_days,
        "largest_10_days_threshold": _canonical_decimal(days_threshold),
        "gate_passed": not issues,
        "issues": issues,
    }


def evaluate_terminal_status(
    validation_metrics: Mapping[str, Any] | None,
    *,
    data_complete: bool,
    experiment: Mapping[str, Any] | None = None,
    config_path: Path | str = DEFAULT_CONFIG_PATH,
) -> str:
    """Apply only the pre-registered gate; no parameter search is performed."""

    if type(data_complete) is not bool:
        raise AlphaFeasibilityReportingError("data_complete must be a boolean")
    if not data_complete:
        return "BLOCKED_DATA"
    if validation_metrics is None:
        raise AlphaFeasibilityReportingError("complete data requires validation metrics")
    frozen = _resolve_experiment(experiment, config_path)
    validation = _normalize_scenarios(validation_metrics, "validation")
    concentration = build_concentration_metrics(validation, experiment=frozen)
    drawdown_limit = Decimal(frozen["gate"]["validation_max_drawdown_max_inclusive"])
    base_active = Decimal(str(validation["base"]["net_active_return"]))
    stress_active = Decimal(str(validation["stress"]["net_active_return"]))
    drawdown_passed = all(
        Decimal(str(validation[scenario]["max_drawdown"])) <= drawdown_limit
        for scenario in SCENARIOS
    )
    gate_passed = (
        base_active > Decimal("0")
        and stress_active >= Decimal("0")
        and drawdown_passed
        and concentration["gate_passed"] is True
    )
    return (
        "ALPHA_FEASIBILITY_GO_CANDIDATE"
        if gate_passed
        else "ALPHA_FEASIBILITY_NO_GO"
    )


def _merge_blockers(summary: Mapping[str, Any], supplied: Any) -> list[str]:
    explicit = [] if supplied is None else _unique_strings(supplied, "remaining_blockers")
    combined = sorted(set(summary["remaining_blockers"]) | set(explicit))
    if not combined:
        raise AlphaFeasibilityReportingError(
            "BLOCKED_DATA report requires at least one remaining blocker"
        )
    return combined


def _report_base(
    *,
    experiment: Mapping[str, Any],
    commit_sha: str,
    data_summary: Mapping[str, Any],
    generated_at: datetime | str | None,
) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "experiment_id": experiment["experiment_id"],
        "generated_at": _timestamp(generated_at),
        "commit_sha": _commit_sha(commit_sha),
        "actual_tushare_request_count_by_endpoint": data_summary[
            "actual_tushare_request_count_by_endpoint"
        ],
        "first_index_weight": data_summary["first_index_weight"],
        "stock_basic_status": data_summary["stock_basic_status"],
        "stock_basic_request_count": data_summary["stock_basic_request_count"],
        "security_master_pit_status": data_summary["security_master_pit_status"],
        "coverage_start": data_summary["coverage_start"],
        "coverage_end": data_summary["coverage_end"],
        "pit_months_expected": data_summary["pit_months_expected"],
        "pit_months_observed": data_summary["pit_months_observed"],
        "union_instrument_count": data_summary["union_instrument_count"],
        "valid_candidate_count_by_decision": data_summary[
            "valid_candidate_count_by_decision"
        ],
        "insufficient_history_count_by_decision": data_summary[
            "insufficient_history_count_by_decision"
        ],
        "ineligible_no_initial_price_count": data_summary[
            "ineligible_no_initial_price_count"
        ],
        "unexplained_market_data_gap_count": data_summary[
            "unexplained_market_data_gap_count"
        ],
        "collection_plan_sha256": data_summary["collection_plan_sha256"],
        "pit_membership_manifest_sha256": data_summary[
            "pit_membership_manifest_sha256"
        ],
        "history_manifest_sha256": data_summary["history_manifest_sha256"],
        **_runtime_provenance(),
        **{field: data_summary[field] for field in COVERAGE_STATUS_FIELDS},
    }


def _self_hash(payload: Mapping[str, Any]) -> dict[str, Any]:
    if "report_sha256" in payload:
        raise AlphaFeasibilityReportingError("report_sha256 must be derived")
    result = dict(payload)
    result["report_sha256"] = canonical_sha256(result)
    return result


def _reject_secret_or_forbidden_data_date(report: Mapping[str, Any]) -> None:
    token = os.environ.get("TUSHARE_TOKEN", "")
    serialized = canonical_json_bytes(report).decode("utf-8")
    if token and token in serialized:
        raise AlphaFeasibilityReportingError("report would persist TUSHARE_TOKEN")

    def walk(value: Any, path: str) -> None:
        if path == "generated_at":
            return
        if isinstance(value, Mapping):
            for key, child in value.items():
                walk(child, f"{path}.{key}" if path else str(key))
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")
        elif isinstance(value, str):
            matches = list(_DATE_RE.finditer(value)) + list(
                _COMPACT_DATE_RE.finditer(value)
            )
            if path.startswith("remaining_blockers["):
                matches.extend(_COMPACT_BLOCKER_DATE_RE.finditer(value))
            for match in matches:
                month = f"{match.group(1)}-{match.group(2)}"
                if month > "2023-12":
                    raise AlphaFeasibilityReportingError(
                        f"report contains an out-of-bound data date at {path}"
                    )

    walk(report, "")


def build_blocked_alpha_feasibility_report(
    *,
    commit_sha: str,
    data_summary: Mapping[str, Any] | None = None,
    remaining_blockers: Sequence[str] | None = None,
    experiment: Mapping[str, Any] | None = None,
    config_path: Path | str = DEFAULT_CONFIG_PATH,
    generated_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Build an honest blocked report without accepting any metrics."""

    frozen = _resolve_experiment(experiment, config_path)
    data = _normalize_data_summary(data_summary, blocked_defaults=True)
    terminal_status = (
        "BLOCKED_ADAPTER_PROTOCOL"
        if data["data_status"] == "BLOCKED_ADAPTER_PROTOCOL"
        else "BLOCKED_DATA"
    )
    payload = {
        **_report_base(
            experiment=frozen,
            commit_sha=commit_sha,
            data_summary=data,
            generated_at=generated_at,
        ),
        "development_metrics": None,
        "validation_metrics": None,
        "concentration_metrics": None,
        "terminal_status": terminal_status,
        "locked_test_status": dict(LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
        "remaining_blockers": _merge_blockers(data, remaining_blockers),
        "safety": dict(SAFETY),
    }
    report = _self_hash(payload)
    verify_alpha_feasibility_report(report, experiment=frozen)
    return report


def _assert_completed_data(data: Mapping[str, Any]) -> None:
    if data["data_status"] not in {None, "READY"}:
        raise AlphaFeasibilityReportingError("completed report requires data_status READY")
    if data["pit_months_observed"] != PIT_MONTHS_EXPECTED:
        raise AlphaFeasibilityReportingError("completed report requires all PIT months")
    if data["union_instrument_count"] <= 0:
        raise AlphaFeasibilityReportingError("completed report requires a non-empty union")
    first_index_weight = data["first_index_weight"]
    if (
        first_index_weight is None
        or first_index_weight["missing_required_data_fields"]
        or first_index_weight["provider_payload_sha256"] is None
        or first_index_weight["normalized_content_sha256"] is None
    ):
        raise AlphaFeasibilityReportingError(
            "completed report requires complete first index_weight field evidence"
        )
    valid_candidate_counts = data["valid_candidate_count_by_decision"]
    insufficient_history_counts = data["insufficient_history_count_by_decision"]
    if not valid_candidate_counts or not insufficient_history_counts:
        raise AlphaFeasibilityReportingError(
            "completed report requires non-empty decision count maps"
        )
    if set(valid_candidate_counts) != set(insufficient_history_counts):
        raise AlphaFeasibilityReportingError(
            "completed report decision count map keys must match exactly"
        )
    if any(
        valid_candidate_counts[decision]
        + insufficient_history_counts[decision]
        > data["union_instrument_count"]
        for decision in valid_candidate_counts
    ):
        raise AlphaFeasibilityReportingError(
            "completed report decision counts cannot exceed the instrument union"
        )
    if data["unexplained_market_data_gap_count"] != 0:
        raise AlphaFeasibilityReportingError(
            "completed report requires zero unexplained market-data gaps"
        )
    if any(
        data[field] is None
        for field in (
            "collection_plan_sha256",
            "pit_membership_manifest_sha256",
            "history_manifest_sha256",
        )
    ):
        raise AlphaFeasibilityReportingError(
            "completed report requires bound collection and manifest provenance"
        )
    if any(data[field] != "complete" for field in COVERAGE_STATUS_FIELDS):
        raise AlphaFeasibilityReportingError("completed report requires complete data coverage")
    if data["remaining_blockers"]:
        raise AlphaFeasibilityReportingError("completed report cannot retain data blockers")


def build_completed_alpha_feasibility_report(
    *,
    commit_sha: str,
    data_summary: Mapping[str, Any],
    development_metrics: Mapping[str, Any],
    validation_metrics: Mapping[str, Any],
    experiment: Mapping[str, Any] | None = None,
    config_path: Path | str = DEFAULT_CONFIG_PATH,
    generated_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Build a GO-candidate or NO-GO report from complete pre-Locked evidence."""

    frozen = _resolve_experiment(experiment, config_path)
    data = _normalize_data_summary(data_summary, blocked_defaults=False)
    _assert_completed_data(data)
    development = _normalize_scenarios(development_metrics, "development")
    validation = _normalize_scenarios(validation_metrics, "validation")
    concentration = build_concentration_metrics(validation, experiment=frozen)
    terminal_status = evaluate_terminal_status(
        validation,
        data_complete=True,
        experiment=frozen,
    )
    payload = {
        **_report_base(
            experiment=frozen,
            commit_sha=commit_sha,
            data_summary=data,
            generated_at=generated_at,
        ),
        "development_metrics": development,
        "validation_metrics": validation,
        "concentration_metrics": concentration,
        "terminal_status": terminal_status,
        "locked_test_status": dict(LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
        "remaining_blockers": [],
        "safety": dict(SAFETY),
    }
    report = _self_hash(payload)
    verify_alpha_feasibility_report(report, experiment=frozen)
    return report


def verify_alpha_feasibility_report(
    report: Mapping[str, Any],
    *,
    experiment: Mapping[str, Any] | None = None,
    config_path: Path | str = DEFAULT_CONFIG_PATH,
) -> None:
    """Verify Schema, self-hash, safety constants, and all derived semantics."""

    if not isinstance(report, Mapping):
        raise AlphaFeasibilityReportingError("report must be a mapping")
    candidate = dict(report)
    declared_hash = candidate.get("report_sha256")
    unsigned = dict(candidate)
    unsigned.pop("report_sha256", None)
    if declared_hash != canonical_sha256(unsigned):
        raise AlphaFeasibilityReportingError("report_sha256 mismatch")
    try:
        validate_json_schema(candidate, REPORT_SCHEMA_PATH)
    except SchemaValidationError as exc:
        raise AlphaFeasibilityReportingError(f"report schema violation: {exc}") from exc

    frozen = _resolve_experiment(experiment, config_path)
    _reject_secret_or_forbidden_data_date(candidate)
    _commit_sha(candidate["commit_sha"])
    runtime_provenance = _runtime_provenance()
    if any(candidate.get(field) != value for field, value in runtime_provenance.items()):
        raise AlphaFeasibilityReportingError("report runtime provenance drift")
    data = _normalize_data_summary(candidate, blocked_defaults=False)
    _request_counts(candidate["actual_tushare_request_count_by_endpoint"])
    if candidate["locked_test_status"] != LOCKED_TEST_STATUS:
        raise AlphaFeasibilityReportingError("report locked_test_status drift")
    if candidate["locked_test_consumed"] is not False:
        raise AlphaFeasibilityReportingError("report consumed Locked Test data")
    if candidate["safety"] != SAFETY:
        raise AlphaFeasibilityReportingError("report safety drift")
    blockers = _unique_strings(candidate["remaining_blockers"], "remaining_blockers")

    terminal = candidate["terminal_status"]
    if terminal in {"BLOCKED_DATA", "BLOCKED_ADAPTER_PROTOCOL"}:
        if any(
            candidate[field] is not None
            for field in (
                "development_metrics",
                "validation_metrics",
                "concentration_metrics",
            )
        ):
            raise AlphaFeasibilityReportingError("blocked report must not contain metrics")
        if not blockers:
            raise AlphaFeasibilityReportingError("blocked report requires blockers")
        expected_blocked_terminal = (
            "BLOCKED_ADAPTER_PROTOCOL"
            if any(blocker in ADAPTER_PROTOCOL_BLOCKERS for blocker in blockers)
            else "BLOCKED_DATA"
        )
        if terminal != expected_blocked_terminal:
            raise AlphaFeasibilityReportingError("blocked terminal_status derived result drift")
        return

    _assert_completed_data(data)
    if blockers:
        raise AlphaFeasibilityReportingError("completed report cannot contain blockers")
    development = _normalize_scenarios(candidate["development_metrics"], "development")
    validation = _normalize_scenarios(candidate["validation_metrics"], "validation")
    expected_concentration = build_concentration_metrics(validation, experiment=frozen)
    if candidate["concentration_metrics"] != expected_concentration:
        raise AlphaFeasibilityReportingError("concentration_metrics derived result drift")
    expected_terminal = evaluate_terminal_status(
        validation,
        data_complete=True,
        experiment=frozen,
    )
    if terminal != expected_terminal:
        raise AlphaFeasibilityReportingError("terminal_status derived result drift")
    if candidate["development_metrics"] != development or candidate["validation_metrics"] != validation:
        raise AlphaFeasibilityReportingError("normalized metric result drift")


def publish_alpha_feasibility_report(
    output_directory: Path | str,
    report: Mapping[str, Any],
    *,
    experiment: Mapping[str, Any] | None = None,
    config_path: Path | str = DEFAULT_CONFIG_PATH,
) -> Path:
    """Create the fixed report, replaying only an existing byte-identical file."""

    verify_alpha_feasibility_report(
        report,
        experiment=experiment,
        config_path=config_path,
    )
    _reject_secret_or_forbidden_data_date(report)
    directory = Path(output_directory)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AlphaFeasibilityReportingError("unable to create report directory") from exc
    target = directory / REPORT_FILENAME
    content = canonical_json_bytes(report) + b"\n"

    try:
        with target.open("xb") as handle:
            handle.write(content)
        return target
    except FileExistsError:
        try:
            existing = target.read_bytes()
        except OSError as exc:
            raise AlphaFeasibilityReportingError("unable to verify existing report") from exc
        if existing == content:
            return target
        raise AlphaFeasibilityReportingError(
            "create_only_report_exists_with_different_bytes"
        )
    except OSError as exc:
        raise AlphaFeasibilityReportingError("unable to publish report") from exc


# Short aliases keep the runner adapter unambiguous without coupling this module
# to a collector or engine class.
build_blocked_report = build_blocked_alpha_feasibility_report
build_completed_report = build_completed_alpha_feasibility_report
derive_terminal_status = evaluate_terminal_status
verify_report = verify_alpha_feasibility_report
publish_report = publish_alpha_feasibility_report


__all__ = [
    "ALLOWED_ENDPOINTS",
    "AlphaFeasibilityReportingError",
    "COVERAGE_END",
    "COVERAGE_START",
    "EXPERIMENT_ID",
    "LOCKED_TEST_CONSUMED",
    "LOCKED_TEST_STATUS",
    "METRIC_FIELDS",
    "PIT_MONTHS_EXPECTED",
    "REPORT_FILENAME",
    "SAFETY",
    "SECURITY_MASTER_PIT_STATUS",
    "STOCK_BASIC_REQUEST_COUNT",
    "STOCK_BASIC_STATUS",
    "build_blocked_alpha_feasibility_report",
    "build_blocked_report",
    "build_completed_alpha_feasibility_report",
    "build_completed_report",
    "build_concentration_metrics",
    "derive_terminal_status",
    "evaluate_terminal_status",
    "load_and_validate_experiment_config",
    "normalize_first_index_weight_evidence",
    "publish_alpha_feasibility_report",
    "publish_report",
    "validate_experiment_config",
    "verify_alpha_feasibility_report",
    "verify_report",
]
