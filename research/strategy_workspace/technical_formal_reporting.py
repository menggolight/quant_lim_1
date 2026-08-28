"""Fail-closed reports for the formal Technical Momentum pre-Locked path.

This module never opens stock-level Locked Test data.  It accepts only a
dataset coverage summary and Development/Validation metric summaries, binds
them into canonical, self-hashed artifacts, and derives readiness from those
two artifacts alone.
"""

from __future__ import annotations

import json
import math
from collections.abc import Collection
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

from research.market_data.validation import (
    SchemaValidationError,
    validate_json_schema,
)
from research.strategy_workspace.contracts import (
    canonical_json_bytes,
    canonical_sha256,
)
from research.strategy_workspace.technical_formal_data import (
    DATASET_STANDARD_INTERFACES,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_ROOT = REPOSITORY_ROOT / "schemas"
DEFAULT_CONFIG_PATH = (
    REPOSITORY_ROOT / "configs" / "a_share_technical_momentum_adaptive.v1.json"
)

DATASET_SCHEMA_PATH = SCHEMA_ROOT / "technical_formal_dataset_manifest.v1.json"
EXPERIMENT_SCHEMA_PATH = SCHEMA_ROOT / "technical_momentum_experiment.v1.json"
BACKTEST_SCHEMA_PATH = SCHEMA_ROOT / "technical_momentum_backtest_report.v1.json"
READINESS_SCHEMA_PATH = SCHEMA_ROOT / "technical_locked_test_readiness.v1.json"

STRATEGY_ID = "a-share-technical-momentum-adaptive-v1"
LOCKED_TEST_STATUS = "NOT_RUN"
LOCKED_TEST_CONSUMED = False
CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

DATASET_REPORT_FILENAME = "dataset_coverage_report.json"
BACKTEST_REPORT_FILENAME = "development_validation_backtest_report.json"
READINESS_REPORT_FILENAME = "locked_test_readiness_report.json"

REQUIRED_DATASETS = (
    "trade_calendar",
    "raw_daily_bar",
    "adjustment_factor",
    "csi800_pit_membership",
    "suspension_history",
    "price_limit_history",
    "name_and_st_history",
    "security_master",
    "csi800_price_benchmark",
)
WARMUP_REQUIRED_DATASETS = frozenset(
    {"raw_daily_bar", "adjustment_factor", "csi800_price_benchmark"}
)
PIT_BOOTSTRAP_LATEST = "2017-12-31"
RAW_DATASET_VERIFICATION_IMPLEMENTED = False
RAW_DATASET_VERIFICATION_BLOCKER = (
    "standard_cli_raw_dataset_verification_not_implemented"
)
CONTROLLED_BACKTEST_IMPLEMENTED = False
CONTROLLED_BACKTEST_BLOCKER = "standard_cli_controlled_backtest_not_implemented"
FROZEN_IMPLEMENTATION = {
    "alpha_policy_path": "configs/a_share_technical_shadow_mvp.v1.json",
    "alpha_policy_sha256": "53b7f2b3da72a2d393c18b4fc61afac9e1a3f63c2cd86756cbd1bd0d47eb77ea",
    "alpha_source_path": "research/strategy_workspace/technical_alpha_shadow_v1.py",
    "alpha_source_sha256": "3cd734c8770e5647754fa21d65e8d6a789da3c17958fb2b2b15352268af3d922",
    "exposure_source_path": "research/strategy_workspace/technical_exposure_shadow_v1.py",
    "exposure_source_sha256": "4a204237752dec4797c2f80cf5950d638aa4d638f2ece615a29ace62f14d0ca7",
}
CRITICAL_CHECKS = (
    "date_order_valid",
    "no_duplicate_primary_keys",
    "pit_membership_complete",
    "adjustment_point_in_time_valid",
    "dual_price_isolated",
    "execution_states_complete",
    "corporate_action_entitlements_complete",
)
ALLOWED_SPLITS = ("development", "validation")
METRIC_FIELDS = (
    "net_return",
    "benchmark_return",
    "net_active_return",
    "max_drawdown",
    "turnover",
    "total_cost",
    "cost_to_gross_profit",
    "exposure_state_distribution",
    "cash_day_fraction",
    "positive_half_year_count",
    "trade_count",
    "win_rate",
    "average_holding_period",
    "per_stock_pnl_contribution",
    "largest_stock_pnl_share",
    "largest_10_days_pnl_share",
)
SAFETY = {
    "paper_eligibility": False,
    "trade_eligibility": False,
    "real_money_list_allowed": False,
    "automatic_order_submission": False,
    "live_supported": False,
}
ALLOWED_STANDARD_INTERFACES = {
    "baostock": frozenset(
        {
            "query_trade_dates",
            "query_history_k_data_plus",
            "query_adjust_factor",
            "query_stock_basic",
        }
    ),
    "tushare_standard_non_vip": frozenset(
        {
            "trade_cal",
            "daily",
            "adj_factor",
            "index_weight",
            "suspend_d",
            "stk_limit",
            "namechange",
            "stock_basic",
            "index_daily",
        }
    ),
}

_SCHEMA_IDS = {
    DATASET_SCHEMA_PATH: "technical_formal_dataset_manifest.v1.json",
    EXPERIMENT_SCHEMA_PATH: "technical_momentum_experiment.v1.json",
    BACKTEST_SCHEMA_PATH: "technical_momentum_backtest_report.v1.json",
    READINESS_SCHEMA_PATH: "technical_locked_test_readiness.v1.json",
}


class TechnicalFormalReportingError(ValueError):
    """Raised when a reporting input weakens the formal fail-closed contract."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TechnicalFormalReportingError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json_bytes(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant: {item}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise TechnicalFormalReportingError(f"{label} is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise TechnicalFormalReportingError(f"{label} root must be an object")
    return value


def _load_json_file(path: Path | str, label: str) -> dict[str, Any]:
    candidate = Path(path)
    try:
        raw = candidate.read_bytes()
    except OSError as exc:
        raise TechnicalFormalReportingError(f"unable to read {label}") from exc
    return _load_json_bytes(raw, label)


def _unique_strings(values: Sequence[Any], field: str) -> list[str]:
    result: list[str] = []
    for value in values:
        if type(value) is not str or not value or value != value.strip():
            raise TechnicalFormalReportingError(
                f"{field} values must be exact non-empty strings"
            )
        result.append(value)
    if len(result) != len(set(result)):
        raise TechnicalFormalReportingError(f"{field} values must be unique")
    return sorted(result)


def _timestamp(value: datetime | str | None) -> str:
    if value is None:
        parsed = datetime.now(CHINA_TZ)
    elif isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise TechnicalFormalReportingError(
                "generated_at must be an ISO date-time"
            ) from exc
    else:
        raise TechnicalFormalReportingError("generated_at must be a date-time")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TechnicalFormalReportingError("generated_at must include a timezone offset")
    return parsed.isoformat()


def _file_sha256(path: Path) -> str:
    digest = sha256()
    try:
        with path.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                digest.update(block)
    except OSError as exc:
        raise TechnicalFormalReportingError("unable to hash frozen implementation") from exc
    return digest.hexdigest()


def _self_hashed(payload: Mapping[str, Any], field: str) -> dict[str, Any]:
    if field in payload:
        raise TechnicalFormalReportingError(f"{field} must be derived, not supplied")
    result = dict(payload)
    result[field] = canonical_sha256(result)
    return result


def _verify_self_hash(payload: Mapping[str, Any], field: str) -> None:
    declared = payload.get(field)
    unsigned = dict(payload)
    unsigned.pop(field, None)
    if declared != canonical_sha256(unsigned):
        raise TechnicalFormalReportingError(f"{field} mismatch")


def load_and_validate_schemas() -> Mapping[str, Mapping[str, Any]]:
    """Load the four local schemas and reject identity or dialect drift."""

    schemas: dict[str, Mapping[str, Any]] = {}
    for path, expected_id in _SCHEMA_IDS.items():
        payload = _load_json_file(path, f"schema {path.name}")
        if payload.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            raise TechnicalFormalReportingError(f"schema dialect drifted: {path.name}")
        if payload.get("$id") != expected_id or payload.get("type") != "object":
            raise TechnicalFormalReportingError(f"schema identity drifted: {path.name}")
        schemas[path.name] = payload
    return schemas


def _decimal_text(value: Any) -> str:
    from decimal import Decimal, InvalidOperation

    if isinstance(value, bool):
        raise TechnicalFormalReportingError("boolean is not a decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise TechnicalFormalReportingError("invalid decimal in frozen config") from exc
    if not parsed.is_finite():
        raise TechnicalFormalReportingError("non-finite decimal in frozen config")
    return format(parsed.normalize(), "f")


def _validate_frozen_config(payload: Mapping[str, Any]) -> None:
    frozen = payload.get("frozen_implementation")
    if not isinstance(frozen, Mapping) or set(frozen) != {
        "alpha_policy_path",
        "alpha_policy_sha256",
        "alpha_source_path",
        "alpha_source_sha256",
        "exposure_source_path",
        "exposure_source_sha256",
    }:
        raise TechnicalFormalReportingError("frozen_implementation is incomplete")
    if dict(frozen) != FROZEN_IMPLEMENTATION:
        raise TechnicalFormalReportingError(
            "frozen implementation paths or baseline hashes drifted"
        )
    for path_field, hash_field in (
        ("alpha_policy_path", "alpha_policy_sha256"),
        ("alpha_source_path", "alpha_source_sha256"),
        ("exposure_source_path", "exposure_source_sha256"),
    ):
        relative = Path(str(frozen[path_field]))
        if relative.is_absolute() or ".." in relative.parts:
            raise TechnicalFormalReportingError("frozen path must remain repository-relative")
        path = (REPOSITORY_ROOT / relative).resolve()
        try:
            path.relative_to(REPOSITORY_ROOT.resolve())
        except ValueError as exc:
            raise TechnicalFormalReportingError("frozen path leaves repository") from exc
        if _file_sha256(path) != str(frozen[hash_field]):
            raise TechnicalFormalReportingError(f"frozen source hash drifted: {path_field}")

    shadow = _load_json_file(
        REPOSITORY_ROOT / str(frozen["alpha_policy_path"]),
        "frozen Technical Shadow policy",
    )
    alpha = payload.get("alpha")
    exposure = payload.get("exposure")
    portfolio = payload.get("portfolio")
    costs = payload.get("costs")
    if not all(isinstance(item, Mapping) for item in (alpha, exposure, portfolio, costs)):
        raise TechnicalFormalReportingError("frozen experiment sections are malformed")
    assert isinstance(alpha, Mapping)
    assert isinstance(exposure, Mapping)
    assert isinstance(portfolio, Mapping)
    assert isinstance(costs, Mapping)

    shadow_alpha = shadow.get("alpha")
    shadow_exposure = shadow.get("exposure")
    shadow_portfolio = shadow.get("portfolio")
    shadow_costs = shadow.get("costs")
    if not all(
        isinstance(item, Mapping)
        for item in (shadow_alpha, shadow_exposure, shadow_portfolio, shadow_costs)
    ):
        raise TechnicalFormalReportingError("frozen Technical Shadow policy is malformed")
    assert isinstance(shadow_alpha, Mapping)
    assert isinstance(shadow_exposure, Mapping)
    assert isinstance(shadow_portfolio, Mapping)
    assert isinstance(shadow_costs, Mapping)
    for field in (
        "factor_ids",
        "zscore_ddof",
        "directions",
    ):
        if alpha.get(field) != shadow_alpha.get(field):
            raise TechnicalFormalReportingError(f"alpha.{field} drifted from Shadow")
    for field in (
        "winsor_lower_quantile",
        "winsor_upper_quantile",
        "entry_score_min_exclusive",
        "entry_percentile_min",
        "hold_score_min_exclusive",
        "hold_percentile_min",
    ):
        if _decimal_text(alpha.get(field)) != _decimal_text(shadow_alpha.get(field)):
            raise TechnicalFormalReportingError(f"alpha.{field} drifted from Shadow")

    for field in (
        "benchmark_trend_sessions",
        "breadth_trend_sessions",
        "realized_vol_sessions",
        "annualization_sessions",
        "failure_state",
    ):
        if exposure.get(field) != shadow_exposure.get(field):
            raise TechnicalFormalReportingError(f"exposure.{field} drifted from Shadow")
    for group in ("risk_off", "defensive", "risk_on", "gross_exposure"):
        current = exposure.get(group)
        original = shadow_exposure.get(group)
        if not isinstance(current, Mapping) or not isinstance(original, Mapping):
            raise TechnicalFormalReportingError(f"exposure.{group} is malformed")
        if set(current) != set(original) or any(
            _decimal_text(current[key]) != _decimal_text(original[key]) for key in current
        ):
            raise TechnicalFormalReportingError(f"exposure.{group} drifted from Shadow")

    for field in (
        "max_positions",
        "lot_size",
        "leverage_allowed",
        "short_selling_allowed",
        "candidate_shortage_policy",
    ):
        if portfolio.get(field) != shadow_portfolio.get(field):
            raise TechnicalFormalReportingError(f"portfolio.{field} drifted from Shadow")
    for field in ("initial_cash", "max_position_weight"):
        if _decimal_text(portfolio.get(field)) != _decimal_text(shadow_portfolio.get(field)):
            raise TechnicalFormalReportingError(f"portfolio.{field} drifted from Shadow")

    base = costs.get("base")
    stress = costs.get("stress")
    if not isinstance(base, Mapping) or not isinstance(stress, Mapping):
        raise TechnicalFormalReportingError("cost scenarios are malformed")
    for field in (
        "commission_rate",
        "minimum_commission",
        "sell_tax_rate",
        "transfer_fee_rate_both_sides",
        "slippage_bps_one_way",
    ):
        if _decimal_text(base.get(field)) != _decimal_text(shadow_costs.get(field)):
            raise TechnicalFormalReportingError(f"costs.base.{field} drifted from Shadow")
    if _decimal_text(base.get("commission_multiplier")) != "1":
        raise TechnicalFormalReportingError("base commission multiplier must remain one")
    for field in (
        "commission_rate",
        "minimum_commission",
        "sell_tax_rate",
        "transfer_fee_rate_both_sides",
    ):
        if _decimal_text(stress.get(field)) != _decimal_text(base.get(field)):
            raise TechnicalFormalReportingError(f"costs.stress.{field} drifted")
    if (
        _decimal_text(stress.get("slippage_bps_one_way")) != "20"
        or _decimal_text(stress.get("commission_multiplier")) != "2"
    ):
        raise TechnicalFormalReportingError("stress cost scenario drifted")

    if payload.get("locked_test_status") != LOCKED_TEST_STATUS:
        raise TechnicalFormalReportingError("Locked Test status must remain NOT_RUN")
    if payload.get("locked_test_consumed") is not LOCKED_TEST_CONSUMED:
        raise TechnicalFormalReportingError("Locked Test consumed flag must remain false")
    if payload.get("safety") != SAFETY:
        raise TechnicalFormalReportingError("safety flags must all remain false")
    splits = payload.get("splits")
    required_splits = {
        "development",
        "validation",
        "locked_test",
        "split_account_policy",
    }
    if not isinstance(splits, Mapping) or set(splits) != required_splits:
        raise TechnicalFormalReportingError("experiment splits are incomplete")
    if splits["locked_test"].get("allowed_to_run") is not False:
        raise TechnicalFormalReportingError("Locked Test must remain forbidden")

    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise TechnicalFormalReportingError("formal data contract is malformed")
    if tuple(data.get("required_datasets", ())) != REQUIRED_DATASETS:
        raise TechnicalFormalReportingError("formal dataset scope drifted")
    if data.get("locked_partition_may_be_backtested") is not False:
        raise TechnicalFormalReportingError("Locked partition backtest must remain forbidden")
    allowed_sources = data.get("allowed_sources")
    if not isinstance(allowed_sources, Mapping) or set(allowed_sources) != set(
        ALLOWED_STANDARD_INTERFACES
    ):
        raise TechnicalFormalReportingError("allowed formal sources drifted")
    for source, expected_interfaces in ALLOWED_STANDARD_INTERFACES.items():
        interfaces = allowed_sources[source]
        if not isinstance(interfaces, Sequence) or isinstance(interfaces, (str, bytes)):
            raise TechnicalFormalReportingError("allowed formal interfaces are malformed")
        if set(interfaces) != set(expected_interfaces):
            raise TechnicalFormalReportingError("allowed formal interfaces drifted")
    forbidden_sources = data.get("forbidden_sources")
    if not isinstance(forbidden_sources, Sequence) or isinstance(
        forbidden_sources, (str, bytes)
    ) or "tushare_vip" not in forbidden_sources:
        raise TechnicalFormalReportingError("Tushare VIP must remain forbidden")

    dual_price = payload.get("dual_price")
    expected_dual_price = {
        "signal_return_formula": (
            "raw_close_t*adj_factor_t/(raw_close_t_minus_1*adj_factor_t_minus_1)-1"
        ),
        "signal_ohlc_formula": (
            "raw_ohlc_t*adj_factor_t/base_raw_close_times_factor"
        ),
        "future_adjustment_factor_forbidden": True,
        "execution_price_basis": "raw_unadjusted_open_close",
        "adjusted_price_for_quantity_cash_or_nav_forbidden": True,
        "held_factor_change_without_entitlement_policy": "fail_closed",
    }
    if not isinstance(dual_price, Mapping) or dict(dual_price) != expected_dual_price:
        raise TechnicalFormalReportingError("dual-price contract is malformed")

    execution = payload.get("execution")
    if not isinstance(execution, Mapping):
        raise TechnicalFormalReportingError("execution contract is malformed")
    required_execution_flags = (
        "new_buy_requires_not_suspended",
        "new_buy_requires_not_st",
        "new_buy_requires_not_limit_up_locked",
        "sell_requires_not_suspended",
        "sell_requires_not_limit_down_locked",
        "sell_requires_t_plus_one",
        "blocked_sell_retains_residual_position",
        "listed_and_not_delisted_required",
    )
    if any(execution.get(field) is not True for field in required_execution_flags):
        raise TechnicalFormalReportingError("execution fail-closed flags drifted")
    if execution.get("automatic_order_submission") is not False:
        raise TechnicalFormalReportingError("automatic order submission must remain disabled")


def load_and_validate_experiment_config(
    path: Path | str = DEFAULT_CONFIG_PATH,
) -> dict[str, Any]:
    """Load the new experiment config and bind it to frozen Shadow sources."""

    load_and_validate_schemas()
    payload = _load_json_file(path, "Technical Momentum experiment config")
    try:
        validate_json_schema(payload, EXPERIMENT_SCHEMA_PATH)
    except SchemaValidationError as exc:
        raise TechnicalFormalReportingError("experiment config schema validation failed") from exc
    _validate_frozen_config(payload)
    return payload


def _missing_dataset(name: str) -> dict[str, Any]:
    return {
        "status": "missing",
        "source": None,
        "interface": None,
        "record_count": 0,
        "coverage_start": None,
        "coverage_end": None,
        "missing_dates": [],
        "content_sha256": None,
        "issues": [f"formal_{name}_not_supplied"],
    }


def _normalize_descriptor(name: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TechnicalFormalReportingError(f"dataset {name} descriptor must be an object")
    expected = {
        "status",
        "source",
        "interface",
        "record_count",
        "coverage_start",
        "coverage_end",
        "missing_dates",
        "content_sha256",
        "issues",
    }
    if set(value) != expected:
        raise TechnicalFormalReportingError(f"dataset {name} descriptor fields drifted")
    if not isinstance(value.get("missing_dates"), Sequence) or isinstance(
        value.get("missing_dates"), (str, bytes)
    ):
        raise TechnicalFormalReportingError(f"dataset {name} missing_dates must be an array")
    if not isinstance(value.get("issues"), Sequence) or isinstance(
        value.get("issues"), (str, bytes)
    ):
        raise TechnicalFormalReportingError(f"dataset {name} issues must be an array")
    result = dict(value)
    result["missing_dates"] = _unique_strings(value["missing_dates"], f"{name}.missing_dates")
    result["issues"] = _unique_strings(value["issues"], f"{name}.issues")
    return result


def _descriptor_blockers(
    name: str,
    descriptor: Mapping[str, Any],
    *,
    required_start: str,
    required_end: str,
) -> list[str]:
    blockers: list[str] = []
    if descriptor.get("status") != "complete":
        blockers.append(f"{name}:status_{descriptor.get('status', 'unknown')}")
    if descriptor.get("record_count") in {None, 0}:
        blockers.append(f"{name}:no_records")
    if not descriptor.get("source") or not descriptor.get("interface"):
        blockers.append(f"{name}:source_or_interface_missing")
    if descriptor.get("content_sha256") is None:
        blockers.append(f"{name}:content_hash_missing")
    start = descriptor.get("coverage_start")
    end = descriptor.get("coverage_end")
    if start is None or str(start) > required_start:
        blockers.append(f"{name}:warmup_or_start_coverage_missing")
    if end is None or str(end) < required_end:
        blockers.append(f"{name}:end_coverage_missing")
    if descriptor.get("missing_dates"):
        blockers.append(f"{name}:missing_dates")
    if descriptor.get("issues"):
        blockers.append(f"{name}:issues_present")
    return blockers


def _source_blockers(
    name: str,
    descriptor: Mapping[str, Any],
    allowed_sources: Mapping[str, Any],
) -> list[str]:
    source = descriptor.get("source")
    interface = descriptor.get("interface")
    if source is None and interface is None:
        return []
    endpoints = allowed_sources.get(source)
    if not isinstance(endpoints, Collection) or isinstance(
        endpoints, (str, bytes, Mapping)
    ):
        return [f"{name}:source_not_allowed"]
    if interface not in endpoints:
        return [f"{name}:interface_not_allowed_for_source"]
    allowed_for_dataset = DATASET_STANDARD_INTERFACES.get(name, frozenset())
    if (source, interface) not in allowed_for_dataset:
        return [f"{name}:interface_not_allowed_for_dataset"]
    return []


def _required_dataset_start(
    dataset_id: str, *, coverage_start: str, warmup_start: str
) -> str:
    if dataset_id in WARMUP_REQUIRED_DATASETS:
        return warmup_start
    if dataset_id == "csi800_pit_membership":
        return PIT_BOOTSTRAP_LATEST
    return coverage_start


def build_dataset_coverage_report(
    *,
    experiment: Mapping[str, Any],
    dataset_evidence: Mapping[str, Any] | None = None,
    generated_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Build a complete nine-dataset manifest, defaulting safely to BLOCKED."""

    _validate_frozen_config(experiment)
    evidence = {} if dataset_evidence is None else dataset_evidence
    if not isinstance(evidence, Mapping):
        raise TechnicalFormalReportingError("dataset_evidence must be an object")
    unknown = set(evidence) - {"datasets", "critical_checks", "remaining_blockers"}
    if unknown:
        raise TechnicalFormalReportingError("dataset_evidence fields drifted")
    supplied_datasets = evidence.get("datasets", {})
    if not isinstance(supplied_datasets, Mapping):
        raise TechnicalFormalReportingError("dataset_evidence.datasets must be an object")
    if set(supplied_datasets) - set(REQUIRED_DATASETS):
        raise TechnicalFormalReportingError("unexpected formal dataset supplied")

    datasets = {
        name: (
            _normalize_descriptor(name, supplied_datasets[name])
            if name in supplied_datasets
            else _missing_dataset(name)
        )
        for name in REQUIRED_DATASETS
    }
    supplied_checks = evidence.get("critical_checks", {})
    if not isinstance(supplied_checks, Mapping):
        raise TechnicalFormalReportingError("critical_checks must be an object")
    if set(supplied_checks) - set(CRITICAL_CHECKS):
        raise TechnicalFormalReportingError("unexpected critical check supplied")
    checks: dict[str, bool] = {}
    for name in CRITICAL_CHECKS:
        value = supplied_checks.get(name, False)
        if type(value) is not bool:
            raise TechnicalFormalReportingError(f"critical check {name} must be boolean")
        checks[name] = value

    caller_blockers = evidence.get("remaining_blockers", [])
    if not isinstance(caller_blockers, Sequence) or isinstance(
        caller_blockers, (str, bytes)
    ):
        raise TechnicalFormalReportingError("remaining_blockers must be an array")
    blockers = _unique_strings(caller_blockers, "remaining_blockers")
    if not RAW_DATASET_VERIFICATION_IMPLEMENTED:
        # Source labels, hashes, and caller-supplied booleans are claims, not
        # official evidence.  Until the standard CLI validates the nine raw
        # datasets itself, none of them may unlock formal readiness.
        blockers.append(RAW_DATASET_VERIFICATION_BLOCKER)
    required_start = str(experiment["data"]["warmup_start"])
    coverage_start = str(experiment["data"]["coverage_start"])
    required_end = str(experiment["data"]["coverage_end"])
    allowed_sources = experiment["data"].get("allowed_sources", {})
    if not isinstance(allowed_sources, Mapping):
        raise TechnicalFormalReportingError("allowed_sources must be an object")
    for name, descriptor in datasets.items():
        blockers.extend(
            _descriptor_blockers(
                name,
                descriptor,
                required_start=_required_dataset_start(
                    name,
                    coverage_start=coverage_start,
                    warmup_start=required_start,
                ),
                required_end=required_end,
            )
        )
        blockers.extend(_source_blockers(name, descriptor, allowed_sources))
    blockers.extend(f"critical_check_failed:{name}" for name, passed in checks.items() if not passed)
    blockers = sorted(set(blockers))
    ready = not blockers

    payload = {
        "schema_version": "technical-formal-dataset-manifest.v1",
        "dataset_id": "technical-momentum-formal-dataset-v1",
        "strategy_id": STRATEGY_ID,
        "generated_at": _timestamp(generated_at),
        "coverage_start": str(experiment["data"]["coverage_start"]),
        "coverage_end": required_end,
        "warmup_start": required_start,
        "datasets": datasets,
        "critical_checks": checks,
        "data_status": "READY" if ready else "BLOCKED",
        "remaining_blockers": blockers,
        "locked_test_status": LOCKED_TEST_STATUS,
        "locked_test_consumed": LOCKED_TEST_CONSUMED,
        "safety": dict(SAFETY),
    }
    result = _self_hashed(payload, "manifest_sha256")
    verify_dataset_coverage_report(result)
    return result


def _manifest_ready(payload: Mapping[str, Any]) -> bool:
    datasets = payload["datasets"]
    return (
        payload.get("data_status") == "READY"
        and not payload.get("remaining_blockers")
        and all(datasets[name]["status"] == "complete" for name in REQUIRED_DATASETS)
        and all(payload["critical_checks"].get(name) is True for name in CRITICAL_CHECKS)
    )


def verify_dataset_coverage_report(payload: Mapping[str, Any]) -> None:
    try:
        validate_json_schema(payload, DATASET_SCHEMA_PATH)
    except SchemaValidationError as exc:
        raise TechnicalFormalReportingError("dataset manifest schema validation failed") from exc
    _verify_self_hash(payload, "manifest_sha256")
    if set(payload.get("critical_checks", {})) != set(CRITICAL_CHECKS):
        raise TechnicalFormalReportingError("dataset manifest critical checks drifted")
    semantic_blockers: list[str] = []
    if not RAW_DATASET_VERIFICATION_IMPLEMENTED:
        semantic_blockers.append(RAW_DATASET_VERIFICATION_BLOCKER)
    for name in REQUIRED_DATASETS:
        semantic_blockers.extend(
            _descriptor_blockers(
                name,
                payload["datasets"][name],
                required_start=_required_dataset_start(
                    name,
                    coverage_start=str(payload["coverage_start"]),
                    warmup_start=str(payload["warmup_start"]),
                ),
                required_end=str(payload["coverage_end"]),
            )
        )
        semantic_blockers.extend(
            _source_blockers(name, payload["datasets"][name], ALLOWED_STANDARD_INTERFACES)
        )
    semantic_blockers.extend(
        f"critical_check_failed:{name}"
        for name in CRITICAL_CHECKS
        if payload["critical_checks"][name] is not True
    )
    declared_blockers = set(payload.get("remaining_blockers", ()))
    missing_blockers = set(semantic_blockers) - declared_blockers
    if missing_blockers:
        raise TechnicalFormalReportingError("dataset manifest omits derived blockers")
    ready = _manifest_ready(payload)
    if (payload.get("data_status") == "READY") is not ready:
        raise TechnicalFormalReportingError("dataset manifest readiness semantics drifted")
    if not ready and not declared_blockers:
        raise TechnicalFormalReportingError("blocked dataset manifest must state blockers")


def _normalize_metrics(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(METRIC_FIELDS):
        raise TechnicalFormalReportingError(f"{field} metrics fields drifted")
    for key, item in value.items():
        if isinstance(item, float) and not math.isfinite(item):
            raise TechnicalFormalReportingError(f"{field}.{key} must be finite")
    return dict(value)


def _normalize_split_results(
    split_results: Mapping[str, Any] | None,
) -> Mapping[str, Mapping[str, Any]]:
    supplied = {} if split_results is None else split_results
    if not isinstance(supplied, Mapping):
        raise TechnicalFormalReportingError("split_results must be an object")
    if set(supplied) - set(ALLOWED_SPLITS):
        raise TechnicalFormalReportingError("Locked or unknown split results are forbidden")
    normalized: dict[str, Mapping[str, Any]] = {}
    for split, value in supplied.items():
        if not isinstance(value, Mapping) or set(value) != {"base_cost", "stress_cost"}:
            raise TechnicalFormalReportingError(f"{split} result fields drifted")
        normalized[split] = {
            "base_cost": _normalize_metrics(value["base_cost"], f"{split}.base_cost"),
            "stress_cost": _normalize_metrics(value["stress_cost"], f"{split}.stress_cost"),
        }
    return normalized


def build_development_validation_report(
    *,
    experiment: Mapping[str, Any],
    dataset_manifest: Mapping[str, Any],
    split_results: Mapping[str, Any] | None = None,
    selected_splits: Sequence[str] = ALLOWED_SPLITS,
    generated_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Report only Development/Validation; incomplete data prevents metric use."""

    _validate_frozen_config(experiment)
    verify_dataset_coverage_report(dataset_manifest)
    selected = tuple(selected_splits)
    if not selected or len(selected) != len(set(selected)):
        raise TechnicalFormalReportingError("selected_splits must be non-empty and unique")
    if any(split not in ALLOWED_SPLITS for split in selected):
        raise TechnicalFormalReportingError("Locked Test split is forbidden")

    data_ready = _manifest_ready(dataset_manifest)
    normalized_results = (
        _normalize_split_results(split_results)
        if data_ready and CONTROLLED_BACKTEST_IMPLEMENTED
        else {}
    )
    if set(normalized_results) - set(selected):
        raise TechnicalFormalReportingError("result supplied for an unselected split")

    split_payloads: dict[str, dict[str, Any]] = {}
    all_blockers = list(dataset_manifest["remaining_blockers"])
    for split in ALLOWED_SPLITS:
        blockers: list[str] = []
        metrics = normalized_results.get(split)
        if not data_ready:
            blockers.append("formal_dataset_blocked")
        if not CONTROLLED_BACKTEST_IMPLEMENTED:
            blockers.append(CONTROLLED_BACKTEST_BLOCKER)
        elif split not in selected:
            blockers.append("split_not_requested")
        elif metrics is None:
            blockers.append("development_or_validation_metrics_not_supplied")
        completed = not blockers
        if not completed:
            all_blockers.extend(f"{split}:{item}" for item in blockers)
        contract = experiment["splits"][split]
        split_payloads[split] = {
            "split": split,
            "start": str(contract["start"]),
            "end": str(contract["end"]),
            "status": "COMPLETED" if completed else "NOT_RUN_BLOCKED",
            "base_cost": metrics["base_cost"] if completed and metrics else None,
            "stress_cost": metrics["stress_cost"] if completed and metrics else None,
            "blockers": sorted(blockers),
        }

    payload = {
        "schema_version": "technical-momentum-backtest-report.v1",
        "strategy_id": STRATEGY_ID,
        "generated_at": _timestamp(generated_at),
        "development": split_payloads["development"],
        "validation": split_payloads["validation"],
        "locked_test_status": LOCKED_TEST_STATUS,
        "locked_test_consumed": LOCKED_TEST_CONSUMED,
        "remaining_blockers": sorted(set(all_blockers)),
        "safety": dict(SAFETY),
    }
    result = _self_hashed(payload, "report_sha256")
    verify_development_validation_report(result)
    return result


def verify_development_validation_report(payload: Mapping[str, Any]) -> None:
    try:
        validate_json_schema(payload, BACKTEST_SCHEMA_PATH)
    except SchemaValidationError as exc:
        raise TechnicalFormalReportingError("backtest report schema validation failed") from exc
    _verify_self_hash(payload, "report_sha256")
    if not CONTROLLED_BACKTEST_IMPLEMENTED:
        for split in ALLOWED_SPLITS:
            if payload[split].get("status") == "COMPLETED":
                raise TechnicalFormalReportingError(
                    "controlled backtest gate is not implemented"
                )
            if CONTROLLED_BACKTEST_BLOCKER not in payload[split].get("blockers", ()):
                raise TechnicalFormalReportingError(
                    "backtest report omits controlled-engine blocker"
                )
    completed_count = 0
    fixed_dates = {
        "development": ("2018-01-01", "2022-12-31"),
        "validation": ("2023-01-01", "2023-12-31"),
    }
    required_top_blockers: set[str] = set()
    for split in ALLOWED_SPLITS:
        section = payload[split]
        if section.get("split") != split:
            raise TechnicalFormalReportingError("backtest split identity drifted")
        if (section.get("start"), section.get("end")) != fixed_dates[split]:
            raise TechnicalFormalReportingError("backtest split dates drifted")
        completed = section.get("status") == "COMPLETED"
        if completed:
            completed_count += 1
            if section.get("base_cost") is None or section.get("stress_cost") is None:
                raise TechnicalFormalReportingError("completed split is missing metrics")
            if section.get("blockers"):
                raise TechnicalFormalReportingError("completed split cannot have blockers")
            _normalize_metrics(section["base_cost"], f"{split}.base_cost")
            _normalize_metrics(section["stress_cost"], f"{split}.stress_cost")
        elif (
            section.get("base_cost") is not None
            or section.get("stress_cost") is not None
            or not section.get("blockers")
        ):
            raise TechnicalFormalReportingError("blocked split semantics drifted")
        else:
            required_top_blockers.update(
                f"{split}:{blocker}" for blocker in section["blockers"]
            )
    if required_top_blockers - set(payload.get("remaining_blockers", ())):
        raise TechnicalFormalReportingError("backtest report omits split blockers")
    if completed_count == 2 and payload.get("remaining_blockers"):
        raise TechnicalFormalReportingError("completed report cannot retain blockers")
    if completed_count < 2 and not payload.get("remaining_blockers"):
        raise TechnicalFormalReportingError("blocked report must state blockers")


def _readiness_checks(
    dataset_manifest: Mapping[str, Any],
    backtest_report: Mapping[str, Any],
) -> dict[str, bool]:
    datasets = dataset_manifest["datasets"]
    critical = dataset_manifest["critical_checks"]
    return {
        "formal_dataset_complete": _manifest_ready(dataset_manifest),
        "pit_membership_complete": (
            datasets["csi800_pit_membership"]["status"] == "complete"
            and critical["pit_membership_complete"] is True
        ),
        "adjustment_safe": all(
            (
                datasets["raw_daily_bar"]["status"] == "complete",
                datasets["adjustment_factor"]["status"] == "complete",
                critical["adjustment_point_in_time_valid"] is True,
                critical["dual_price_isolated"] is True,
                critical["corporate_action_entitlements_complete"] is True,
            )
        ),
        "execution_status_complete": (
            critical["execution_states_complete"] is True
            and all(
                datasets[name]["status"] == "complete"
                for name in (
                    "raw_daily_bar",
                    "suspension_history",
                    "price_limit_history",
                    "name_and_st_history",
                    "security_master",
                )
            )
        ),
        "development_completed": backtest_report["development"]["status"] == "COMPLETED",
        "validation_completed": backtest_report["validation"]["status"] == "COMPLETED",
        "locked_test_not_run": (
            dataset_manifest["locked_test_status"] == LOCKED_TEST_STATUS
            and dataset_manifest["locked_test_consumed"] is False
            and backtest_report["locked_test_status"] == LOCKED_TEST_STATUS
            and backtest_report["locked_test_consumed"] is False
        ),
    }


def build_locked_test_readiness(
    *,
    dataset_manifest: Mapping[str, Any],
    backtest_report: Mapping[str, Any],
    generated_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Derive readiness only from manifest and Development/Validation report."""

    verify_dataset_coverage_report(dataset_manifest)
    verify_development_validation_report(backtest_report)
    checks = _readiness_checks(dataset_manifest, backtest_report)
    blockers = list(dataset_manifest["remaining_blockers"])
    blockers.extend(backtest_report["remaining_blockers"])
    blockers.extend(f"readiness_check_failed:{name}" for name, passed in checks.items() if not passed)
    blockers = sorted(set(blockers))
    verdict = "DATA_READY_FOR_LOCKED_TEST" if all(checks.values()) and not blockers else "BLOCKED"
    payload = {
        "schema_version": "technical-locked-test-readiness.v1",
        "strategy_id": STRATEGY_ID,
        "generated_at": _timestamp(generated_at),
        "verdict": verdict,
        "dataset_manifest_sha256": dataset_manifest["manifest_sha256"],
        "backtest_report_sha256": backtest_report["report_sha256"],
        "checks": checks,
        "locked_test_status": LOCKED_TEST_STATUS,
        "locked_test_consumed": LOCKED_TEST_CONSUMED,
        "remaining_blockers": blockers,
        "safety": dict(SAFETY),
    }
    result = _self_hashed(payload, "readiness_sha256")
    verify_locked_test_readiness(result)
    return result


def verify_locked_test_readiness(payload: Mapping[str, Any]) -> None:
    try:
        validate_json_schema(payload, READINESS_SCHEMA_PATH)
    except SchemaValidationError as exc:
        raise TechnicalFormalReportingError("readiness schema validation failed") from exc
    _verify_self_hash(payload, "readiness_sha256")
    checks = payload.get("checks", {})
    expected_checks = {
        "formal_dataset_complete",
        "pit_membership_complete",
        "adjustment_safe",
        "execution_status_complete",
        "development_completed",
        "validation_completed",
        "locked_test_not_run",
    }
    if set(checks) != expected_checks:
        raise TechnicalFormalReportingError("readiness checks drifted")
    if (
        not RAW_DATASET_VERIFICATION_IMPLEMENTED
        and checks.get("formal_dataset_complete") is not False
    ):
        raise TechnicalFormalReportingError("readiness bypasses raw-data verification gate")
    if not CONTROLLED_BACKTEST_IMPLEMENTED and (
        checks.get("development_completed") is not False
        or checks.get("validation_completed") is not False
    ):
        raise TechnicalFormalReportingError("readiness bypasses controlled-backtest gate")
    failed_check_blockers = {
        f"readiness_check_failed:{name}"
        for name, passed in checks.items()
        if passed is not True
    }
    if failed_check_blockers - set(payload.get("remaining_blockers", ())):
        raise TechnicalFormalReportingError("readiness report omits failed checks")
    derived_ready = bool(checks) and all(checks.values()) and not payload.get("remaining_blockers")
    if (payload.get("verdict") == "DATA_READY_FOR_LOCKED_TEST") is not derived_ready:
        raise TechnicalFormalReportingError("readiness verdict semantics drifted")


def build_locked_test_readiness_from_files(
    *,
    dataset_manifest_path: Path | str,
    backtest_report_path: Path | str,
    generated_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Read exactly the two pre-Locked reports; never open a data partition."""

    dataset_manifest = _load_json_file(dataset_manifest_path, "dataset manifest")
    backtest_report = _load_json_file(backtest_report_path, "Development/Validation report")
    return build_locked_test_readiness(
        dataset_manifest=dataset_manifest,
        backtest_report=backtest_report,
        generated_at=generated_at,
    )


def publish_formal_reports(
    *,
    output_directory: Path | str,
    dataset_manifest: Mapping[str, Any],
    backtest_report: Mapping[str, Any],
    readiness_report: Mapping[str, Any],
) -> Mapping[str, Path]:
    """Create exactly three canonical artifacts in a new directory."""

    verify_dataset_coverage_report(dataset_manifest)
    verify_development_validation_report(backtest_report)
    verify_locked_test_readiness(readiness_report)
    if readiness_report["dataset_manifest_sha256"] != dataset_manifest["manifest_sha256"]:
        raise TechnicalFormalReportingError("readiness dataset binding mismatch")
    if readiness_report["backtest_report_sha256"] != backtest_report["report_sha256"]:
        raise TechnicalFormalReportingError("readiness backtest binding mismatch")
    expected_checks = _readiness_checks(dataset_manifest, backtest_report)
    if readiness_report["checks"] != expected_checks:
        raise TechnicalFormalReportingError("readiness checks do not match bound reports")
    expected_blockers = list(dataset_manifest["remaining_blockers"])
    expected_blockers.extend(backtest_report["remaining_blockers"])
    expected_blockers.extend(
        f"readiness_check_failed:{name}"
        for name, passed in expected_checks.items()
        if not passed
    )
    if readiness_report["remaining_blockers"] != sorted(set(expected_blockers)):
        raise TechnicalFormalReportingError("readiness blockers do not match bound reports")

    output = Path(output_directory)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        output.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise TechnicalFormalReportingError("create_only_output_directory_exists") from exc

    payloads = {
        DATASET_REPORT_FILENAME: dataset_manifest,
        BACKTEST_REPORT_FILENAME: backtest_report,
        READINESS_REPORT_FILENAME: readiness_report,
    }
    paths: dict[str, Path] = {}
    try:
        for filename, payload in payloads.items():
            path = output / filename
            with path.open("xb") as handle:
                handle.write(canonical_json_bytes(payload) + b"\n")
            paths[filename] = path
    except OSError as exc:
        raise TechnicalFormalReportingError("unable to publish formal reports") from exc
    return paths


__all__ = [
    "ALLOWED_SPLITS",
    "BACKTEST_REPORT_FILENAME",
    "CONTROLLED_BACKTEST_BLOCKER",
    "CRITICAL_CHECKS",
    "DATASET_REPORT_FILENAME",
    "DEFAULT_CONFIG_PATH",
    "METRIC_FIELDS",
    "PIT_BOOTSTRAP_LATEST",
    "RAW_DATASET_VERIFICATION_BLOCKER",
    "READINESS_REPORT_FILENAME",
    "REQUIRED_DATASETS",
    "SAFETY",
    "TechnicalFormalReportingError",
    "build_dataset_coverage_report",
    "build_development_validation_report",
    "build_locked_test_readiness",
    "build_locked_test_readiness_from_files",
    "load_and_validate_experiment_config",
    "load_and_validate_schemas",
    "publish_formal_reports",
    "verify_dataset_coverage_report",
    "verify_development_validation_report",
    "verify_locked_test_readiness",
    "WARMUP_REQUIRED_DATASETS",
]
