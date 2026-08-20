"""Immutable ExperimentSpec v2 contract for stock-factor preregistration.

The contract freezes every degree of freedom needed to evaluate one experiment.
It deliberately contains no realised returns, fitted coefficients, winners, or
gate outcomes.  A spec is content-addressed and can only be persisted to a new
path so an observed test interval cannot be silently re-described later.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .contracts import canonical_json_bytes, canonical_sha256


SCHEMA_VERSION = "strategy-experiment-v2"
STATUS = "preregistered_frozen"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_FIELD = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FACTOR = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_INSTRUMENT = re.compile(r"^[0-9]{6}\.(?:SH|SZ)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

QUALITY_GROWTH_FACTOR_CONTRACTS: tuple[dict[str, Any], ...] = (
    {
        "factor_id": "QG_ROE_STABILITY",
        "formula": "latest_quarter_roe - sample_std(last_12_quarters_roe,ddof=1)",
        "expected_sign": "positive",
        "financial_applicability": "all",
        "required_fields": ["return_on_equity"],
    },
    {
        "factor_id": "QG_EARNINGS_TREND_DEVIATION",
        "formula": "(latest_quarter_net_profit - preceding_8q_ols_trend_prediction) / sample_std(preceding_8q_ols_residuals,ddof=1)",
        "expected_sign": "positive",
        "financial_applicability": "all",
        "required_fields": ["net_profit_attributable"],
    },
    {
        "factor_id": "QG_CASH_EARNINGS_QUALITY",
        "formula": "(ttm_operating_cash_flow - ttm_operating_profit) / latest_total_assets",
        "expected_sign": "positive",
        "financial_applicability": "non_financial_only",
        "required_fields": ["operating_cash_flow", "operating_profit", "total_assets"],
    },
    {
        "factor_id": "QG_CASH_DEBT_COVERAGE",
        "formula": "ttm_operating_cash_flow / latest_total_liabilities",
        "expected_sign": "positive",
        "financial_applicability": "non_financial_only",
        "required_fields": ["operating_cash_flow", "total_liabilities"],
    },
    {
        "factor_id": "QG_GROSS_PROFITABILITY",
        "formula": "ttm_gross_profit / mean(latest_total_assets,total_assets_4q_ago)",
        "expected_sign": "positive",
        "financial_applicability": "non_financial_only",
        "required_fields": ["gross_profit", "total_assets"],
    },
    {
        "factor_id": "QG_REVENUE_GROWTH_STABILITY",
        "formula": "mean(last_8_quarterly_yoy_revenue_growth) - sample_std(last_8_quarterly_yoy_revenue_growth,ddof=1)",
        "expected_sign": "positive",
        "financial_applicability": "non_financial_only",
        "required_fields": ["revenue"],
    },
)

RESIDUALIZATION_CONTROL_CONTRACTS: tuple[dict[str, Any], ...] = (
    {
        "control_id": "csi_level1_industry_dummies",
        "field": "industry_code",
        "transform": "one_hot",
        "availability_lag_sessions": 0,
    },
    {
        "control_id": "log_float_market_cap",
        "field": "float_market_cap",
        "transform": "natural_log",
        "availability_lag_sessions": 0,
    },
    {
        "control_id": "earnings_yield",
        "field": "earnings_yield",
        "transform": "identity",
        "availability_lag_sessions": 0,
    },
    {
        "control_id": "rm120",
        "field": "adjusted_close",
        "transform": "return_120_sessions",
        "availability_lag_sessions": 0,
    },
    {
        "control_id": "volatility_60",
        "field": "daily_return",
        "transform": "sample_std_60_sessions",
        "availability_lag_sessions": 0,
    },
)

HISTORICAL_GATE_CONTRACTS: tuple[dict[str, str], ...] = (
    {"gate_id": "data_pit_complete", "metric": "data_pit_complete", "operator": "gte", "threshold": "1"},
    {"gate_id": "top_decile_net_absolute_positive", "metric": "top_decile_net_absolute_return", "operator": "gt", "threshold": "0"},
    {"gate_id": "top_decile_net_active_positive", "metric": "top_decile_net_active_return", "operator": "gt", "threshold": "0"},
    {"gate_id": "top2_net_absolute_positive", "metric": "top2_lot_net_absolute_return", "operator": "gt", "threshold": "0"},
    {"gate_id": "top2_net_active_positive", "metric": "top2_lot_net_active_return", "operator": "gt", "threshold": "0"},
    {"gate_id": "oos_rank_ic_stable", "metric": "oos_rank_ic_positive_and_stable", "operator": "gte", "threshold": "1"},
    {"gate_id": "corrected_significant_factor_count_gte_2", "metric": "corrected_independent_factor_count", "operator": "gte", "threshold": "2"},
    {"gate_id": "positive_semiannual_windows_gte_3_of_4", "metric": "positive_half_year_window_count_of_4", "operator": "gte", "threshold": "3"},
    {"gate_id": "stress_active_return_non_negative", "metric": "stress_net_active_return", "operator": "gte", "threshold": "0"},
    {"gate_id": "max_drawdown_lte_12pct", "metric": "max_drawdown", "operator": "lte", "threshold": "0.12"},
    {"gate_id": "annualized_one_way_turnover_lte_4", "metric": "annualized_one_way_turnover", "operator": "lte", "threshold": "4"},
)

STATISTICAL_CONTRACT: Mapping[str, Any] = MappingProxyType(
    {
        "fama_macbeth_hac_lag_rule": "andrews_automatic_floor_4_t_over_100_pow_2_over_9",
        "fama_macbeth_min_periods": 2,
        "multiple_testing": "holm",
        "familywise_alpha": "0.05",
        "rank_ic_evaluation_splits": ("validation", "locked_test", "audit"),
        "rank_ic_mean_threshold": "0",
        "rank_ic_positive_fraction_threshold": "0.5",
        "factor_significance_splits": ("locked_test", "audit"),
        "ridge_submodel_policy": "financial_2_factor_nonfinancial_6_factor",
    }
)

# Keep the in-process source of truth immutable as well as the persisted spec.
QUALITY_GROWTH_FACTOR_CONTRACTS = tuple(
    MappingProxyType(
        {
            **item,
            "required_fields": tuple(item["required_fields"]),
        }
    )
    for item in QUALITY_GROWTH_FACTOR_CONTRACTS
)
RESIDUALIZATION_CONTROL_CONTRACTS = tuple(
    MappingProxyType(dict(item)) for item in RESIDUALIZATION_CONTROL_CONTRACTS
)
HISTORICAL_GATE_CONTRACTS = tuple(
    MappingProxyType(dict(item)) for item in HISTORICAL_GATE_CONTRACTS
)


class ExperimentContractError(ValueError):
    """Raised when an experiment is mutable, ambiguous, or internally invalid."""


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ExperimentContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ExperimentContractError(f"{label} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ExperimentContractError(f"{label} keys must be strings")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise ExperimentContractError(
            f"{label} fields differ: missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )


def _sequence(value: Any, label: str) -> tuple[Any, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ExperimentContractError(f"{label} must be an array")
    return tuple(value)


def _text(value: Any, label: str, *, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExperimentContractError(f"{label} must be a non-empty string")
    result = value.strip()
    if pattern is not None and pattern.fullmatch(result) is None:
        raise ExperimentContractError(f"{label} has an invalid format")
    return result


def _enum(value: Any, allowed: set[str], label: str) -> str:
    result = _text(value, label)
    if result not in allowed:
        raise ExperimentContractError(f"{label} must be one of {sorted(allowed)}")
    return result


def _sha(value: Any, label: str) -> str:
    return _text(value, label, pattern=_SHA256)


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ExperimentContractError(f"{label} must be an integer >= {minimum}")
    return value


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise ExperimentContractError(f"{label} must be a boolean")
    return value


def _decimal_text(
    value: Any,
    label: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
    strictly_positive: bool = False,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExperimentContractError(f"{label} must be a decimal string")
    try:
        number = Decimal(value.strip())
    except InvalidOperation as exc:
        raise ExperimentContractError(f"{label} must be a decimal string") from exc
    if not number.is_finite():
        raise ExperimentContractError(f"{label} must be finite")
    if strictly_positive and number <= 0:
        raise ExperimentContractError(f"{label} must be > 0")
    if minimum is not None and number < minimum:
        raise ExperimentContractError(f"{label} must be >= {minimum}")
    if maximum is not None and number > maximum:
        raise ExperimentContractError(f"{label} must be <= {maximum}")
    normalized = format(number, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return "0" if normalized in {"-0", ""} else normalized


def _datetime_text(value: Any, label: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ExperimentContractError(f"{label} must be an ISO datetime") from exc
    else:
        raise ExperimentContractError(f"{label} must be an ISO datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExperimentContractError(f"{label} must include a timezone offset")
    return parsed.isoformat()


def _date_text(value: Any, label: str) -> str:
    if isinstance(value, datetime):
        raise ExperimentContractError(f"{label} must be a date, not a datetime")
    if isinstance(value, date):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ExperimentContractError(f"{label} must be an ISO date") from exc
        if parsed.isoformat() != value:
            raise ExperimentContractError(f"{label} must use canonical YYYY-MM-DD")
    else:
        raise ExperimentContractError(f"{label} must be an ISO date")
    return parsed.isoformat()


def _sorted_unique_texts(
    value: Any,
    label: str,
    *,
    pattern: re.Pattern[str] | None = None,
    minimum: int = 0,
) -> list[str]:
    items = [_text(item, f"{label} item", pattern=pattern) for item in _sequence(value, label)]
    if len(items) < minimum:
        raise ExperimentContractError(f"{label} requires at least {minimum} items")
    if len(set(items)) != len(items):
        raise ExperimentContractError(f"{label} items must be unique")
    return sorted(items)


def _normalize_interval(value: Any, label: str) -> dict[str, str]:
    item = _mapping(value, label)
    _exact_keys(item, {"start_date", "end_date"}, label)
    start = _date_text(item["start_date"], f"{label}.start_date")
    end = _date_text(item["end_date"], f"{label}.end_date")
    if start > end:
        raise ExperimentContractError(f"{label} start_date must not exceed end_date")
    return {"start_date": start, "end_date": end}


def _normalise_content(value: Any) -> dict[str, Any]:
    root = _mapping(value, "experiment")
    expected_root = {
        "schema_version",
        "experiment_id",
        "created_at",
        "status",
        "universe",
        "benchmark",
        "target",
        "factors",
        "controls",
        "splits",
        "ridge",
        "statistics",
        "cost",
        "portfolio",
        "gates",
        "hashes",
        "consumed_test_intervals",
    }
    _exact_keys(root, expected_root, "experiment")
    if root["schema_version"] != SCHEMA_VERSION:
        raise ExperimentContractError(f"schema_version must be {SCHEMA_VERSION}")
    if root["status"] != STATUS:
        raise ExperimentContractError(f"status must be {STATUS}")
    normalized_created_at = _datetime_text(root["created_at"], "created_at")

    universe = _mapping(root["universe"], "universe")
    _exact_keys(
        universe,
        {
            "universe_id",
            "membership_dataset_id",
            "effective_interval",
            "selection_rule",
            "backfill_policy",
            "membership_panel_receipt_sha256",
            "membership_panel_content_sha256",
        },
        "universe",
    )
    if universe["membership_dataset_id"] != "CSI800_PIT":
        raise ExperimentContractError("universe.membership_dataset_id must be CSI800_PIT")
    if universe["selection_rule"] != "membership_effective_at_decision":
        raise ExperimentContractError(
            "universe.selection_rule must be membership_effective_at_decision"
        )
    if universe["backfill_policy"] != "forbid_current_constituent_backfill":
        raise ExperimentContractError(
            "universe.backfill_policy must forbid_current_constituent_backfill"
        )
    normalized_universe = {
        "universe_id": _text(universe["universe_id"], "universe.universe_id", pattern=_IDENTIFIER),
        "membership_dataset_id": "CSI800_PIT",
        "effective_interval": _normalize_interval(
            universe["effective_interval"], "universe.effective_interval"
        ),
        "selection_rule": "membership_effective_at_decision",
        "backfill_policy": "forbid_current_constituent_backfill",
        "membership_panel_receipt_sha256": _sha(
            universe["membership_panel_receipt_sha256"],
            "universe.membership_panel_receipt_sha256",
        ),
        "membership_panel_content_sha256": _sha(
            universe["membership_panel_content_sha256"],
            "universe.membership_panel_content_sha256",
        ),
    }

    benchmark = _mapping(root["benchmark"], "benchmark")
    _exact_keys(
        benchmark,
        {
            "instrument_id",
            "provider_id",
            "return_basis",
            "instrument_id_source_receipt_sha256",
            "total_return_series_content_sha256",
        },
        "benchmark",
    )
    if benchmark["return_basis"] != "total_return":
        raise ExperimentContractError("benchmark.return_basis must be total_return")
    if benchmark["provider_id"] != "choice":
        raise ExperimentContractError("benchmark.provider_id must be choice")
    normalized_benchmark = {
        "instrument_id": _text(
            benchmark["instrument_id"], "benchmark.instrument_id", pattern=_IDENTIFIER
        ),
        "provider_id": "choice",
        "return_basis": "total_return",
        "instrument_id_source_receipt_sha256": _sha(
            benchmark["instrument_id_source_receipt_sha256"],
            "benchmark.instrument_id_source_receipt_sha256",
        ),
        "total_return_series_content_sha256": _sha(
            benchmark["total_return_series_content_sha256"],
            "benchmark.total_return_series_content_sha256",
        ),
    }

    target = _mapping(root["target"], "target")
    _exact_keys(
        target,
        {
            "target_id",
            "horizon_trading_sessions",
            "definition",
            "signal_cutoff",
            "entry_policy",
            "exit_policy",
            "benchmark_alignment",
            "rebalance_anchor_date",
            "rebalance_anchor_rule",
            "trading_calendar_content_sha256",
        },
        "target",
    )
    required_target = {
        "definition": "future_20_session_open_to_open_excess_total_return",
        "signal_cutoff": "decision_session_close",
        "entry_policy": "next_trading_session_open",
        "exit_policy": "rebalance_open_after_20_trading_sessions",
        "benchmark_alignment": "same_sessions_same_return_basis",
        "rebalance_anchor_rule": "first_controlled_session_on_or_after_2018-01-01",
    }
    if type(target["horizon_trading_sessions"]) is not int or target["horizon_trading_sessions"] != 20:
        raise ExperimentContractError("target.horizon_trading_sessions must be exactly 20")
    for key, expected in required_target.items():
        if target[key] != expected:
            raise ExperimentContractError(f"target.{key} must be {expected}")
    anchor_date = _date_text(target["rebalance_anchor_date"], "target.rebalance_anchor_date")
    if anchor_date != "2018-01-02":
        raise ExperimentContractError(
            "target.rebalance_anchor_date must be the frozen 2018-01-02 A-share session"
        )
    normalized_target = {
        "target_id": _text(target["target_id"], "target.target_id", pattern=_IDENTIFIER),
        "horizon_trading_sessions": 20,
        "rebalance_anchor_date": anchor_date,
        "trading_calendar_content_sha256": _sha(
            target["trading_calendar_content_sha256"],
            "target.trading_calendar_content_sha256",
        ),
        **required_target,
    }

    normalized_factors: list[dict[str, Any]] = []
    for index, raw_factor in enumerate(_sequence(root["factors"], "factors")):
        factor = _mapping(raw_factor, f"factors[{index}]")
        _exact_keys(
            factor,
            {
                "factor_id",
                "formula",
                "expected_sign",
                "financial_applicability",
                "required_fields",
            },
            f"factors[{index}]",
        )
        normalized_factors.append(
            {
                "factor_id": _text(factor["factor_id"], "factor.factor_id", pattern=_FACTOR),
                "formula": _text(factor["formula"], "factor.formula"),
                "expected_sign": _enum(
                    factor["expected_sign"], {"positive", "negative"}, "factor.expected_sign"
                ),
                "financial_applicability": _enum(
                    factor["financial_applicability"],
                    {"all", "non_financial_only"},
                    "factor.financial_applicability",
                ),
                "required_fields": _sorted_unique_texts(
                    factor["required_fields"],
                    "factor.required_fields",
                    pattern=_FIELD,
                    minimum=1,
                ),
            }
        )
    if len(normalized_factors) != 6:
        raise ExperimentContractError("factors must contain exactly six definitions")
    factor_ids = [item["factor_id"] for item in normalized_factors]
    if len(set(factor_ids)) != len(factor_ids):
        raise ExperimentContractError("factor_ids must be unique")
    normalized_factors.sort(key=lambda item: item["factor_id"])
    expected_factors = sorted(
        (dict(item) for item in QUALITY_GROWTH_FACTOR_CONTRACTS),
        key=lambda item: item["factor_id"],
    )
    for item in expected_factors:
        item["required_fields"] = sorted(item["required_fields"])
    if normalized_factors != expected_factors:
        raise ExperimentContractError(
            "factors must exactly match the frozen V1 quality-growth family"
        )

    normalized_controls: list[dict[str, Any]] = []
    for index, raw_control in enumerate(_sequence(root["controls"], "controls")):
        control = _mapping(raw_control, f"controls[{index}]")
        _exact_keys(
            control,
            {"control_id", "field", "transform", "availability_lag_sessions"},
            f"controls[{index}]",
        )
        normalized_controls.append(
            {
                "control_id": _text(
                    control["control_id"], "control.control_id", pattern=_IDENTIFIER
                ),
                "field": _text(control["field"], "control.field", pattern=_FIELD),
                "transform": _text(control["transform"], "control.transform", pattern=_IDENTIFIER),
                "availability_lag_sessions": _integer(
                    control["availability_lag_sessions"],
                    "control.availability_lag_sessions",
                    minimum=0,
                ),
            }
        )
    if not normalized_controls:
        raise ExperimentContractError("controls must not be empty")
    control_ids = [item["control_id"] for item in normalized_controls]
    if len(set(control_ids)) != len(control_ids):
        raise ExperimentContractError("control_ids must be unique")
    normalized_controls.sort(key=lambda item: item["control_id"])
    expected_controls = sorted(
        (dict(item) for item in RESIDUALIZATION_CONTROL_CONTRACTS),
        key=lambda item: item["control_id"],
    )
    if normalized_controls != expected_controls:
        raise ExperimentContractError(
            "controls must exactly match industry, log-cap, earnings-yield, RM120, and VOL60"
        )

    splits = _mapping(root["splits"], "splits")
    _exact_keys(
        splits,
        {
            "train",
            "validation",
            "locked_test",
            "second_audit",
            "preregistration_cutoff",
            "purge_sessions",
            "locked_test_freshness",
            "second_audit_freshness",
        },
        "splits",
    )
    train = _normalize_interval(splits["train"], "splits.train")
    validation = _normalize_interval(splits["validation"], "splits.validation")
    locked_test = _normalize_interval(splits["locked_test"], "splits.locked_test")
    second_audit = _normalize_interval(splits["second_audit"], "splits.second_audit")
    cutoff = _date_text(
        splits["preregistration_cutoff"], "splits.preregistration_cutoff"
    )
    expected_intervals = {
        "train": {"start_date": "2018-01-01", "end_date": "2022-12-31"},
        "validation": {"start_date": "2023-01-01", "end_date": "2023-12-31"},
        "locked_test": {"start_date": "2024-01-01", "end_date": "2025-12-31"},
    }
    actual_intervals = {
        "train": train,
        "validation": validation,
        "locked_test": locked_test,
    }
    for split_name, expected_interval in expected_intervals.items():
        if actual_intervals[split_name] != expected_interval:
            raise ExperimentContractError(
                f"splits.{split_name} must be frozen at {expected_interval}"
            )
    if second_audit != {"start_date": "2026-01-01", "end_date": cutoff}:
        raise ExperimentContractError(
            "splits.second_audit must run from 2026-01-01 through preregistration_cutoff"
        )
    if cutoff > normalized_created_at[:10]:
        raise ExperimentContractError(
            "splits.preregistration_cutoff cannot be after experiment created_at"
        )
    if normalized_universe["effective_interval"] != {
        "start_date": "2018-01-01",
        "end_date": cutoff,
    }:
        raise ExperimentContractError(
            "universe.effective_interval must cover 2018-01-01 through preregistration_cutoff"
        )
    purge_sessions = _integer(splits["purge_sessions"], "splits.purge_sessions")
    if purge_sessions != 20:
        raise ExperimentContractError("splits.purge_sessions must be exactly 20")
    normalized_splits = {
        "train": train,
        "validation": validation,
        "locked_test": locked_test,
        "second_audit": second_audit,
        "preregistration_cutoff": cutoff,
        "purge_sessions": 20,
        "locked_test_freshness": _enum(
            splits["locked_test_freshness"],
            {"fresh_unconsumed", "retrospective_consumed"},
            "splits.locked_test_freshness",
        ),
        "second_audit_freshness": _enum(
            splits["second_audit_freshness"],
            {"fresh_unconsumed", "retrospective_consumed"},
            "splits.second_audit_freshness",
        ),
    }

    ridge = _mapping(root["ridge"], "ridge")
    _exact_keys(
        ridge,
        {"model", "alpha", "fit_intercept", "standardization", "fit_scope"},
        "ridge",
    )
    if ridge["model"] != "ridge":
        raise ExperimentContractError("ridge.model must be ridge")
    if ridge["standardization"] != "cross_sectional_train_parameters_only":
        raise ExperimentContractError(
            "ridge.standardization must be cross_sectional_train_parameters_only"
        )
    alpha = _decimal_text(ridge["alpha"], "ridge.alpha", strictly_positive=True)
    if Decimal(alpha) != Decimal("1"):
        raise ExperimentContractError("ridge.alpha must be fixed at 1")
    if ridge["fit_scope"] != "train_only":
        raise ExperimentContractError("ridge.fit_scope must be train_only")
    normalized_ridge = {
        "model": "ridge",
        "alpha": "1",
        "fit_intercept": _boolean(ridge["fit_intercept"], "ridge.fit_intercept"),
        "standardization": "cross_sectional_train_parameters_only",
        "fit_scope": "train_only",
    }
    if normalized_ridge["fit_intercept"] is not True:
        raise ExperimentContractError("ridge.fit_intercept must be true")

    statistics = _mapping(root["statistics"], "statistics")
    if set(statistics) != set(STATISTICAL_CONTRACT):
        raise ExperimentContractError("statistics fields differ from the frozen contract")
    normalized_statistics = {
        "fama_macbeth_hac_lag_rule": _text(
            statistics["fama_macbeth_hac_lag_rule"],
            "statistics.fama_macbeth_hac_lag_rule",
            pattern=_IDENTIFIER,
        ),
        "fama_macbeth_min_periods": _integer(
            statistics["fama_macbeth_min_periods"],
            "statistics.fama_macbeth_min_periods",
            minimum=2,
        ),
        "multiple_testing": _text(
            statistics["multiple_testing"],
            "statistics.multiple_testing",
            pattern=_IDENTIFIER,
        ),
        "familywise_alpha": _decimal_text(
            statistics["familywise_alpha"],
            "statistics.familywise_alpha",
            strictly_positive=True,
            maximum=Decimal("1"),
        ),
        "rank_ic_evaluation_splits": [
            _text(
                item,
                "statistics.rank_ic_evaluation_splits item",
                pattern=_IDENTIFIER,
            )
            for item in _sequence(
                statistics["rank_ic_evaluation_splits"],
                "statistics.rank_ic_evaluation_splits",
            )
        ],
        "rank_ic_mean_threshold": _decimal_text(
            statistics["rank_ic_mean_threshold"],
            "statistics.rank_ic_mean_threshold",
        ),
        "rank_ic_positive_fraction_threshold": _decimal_text(
            statistics["rank_ic_positive_fraction_threshold"],
            "statistics.rank_ic_positive_fraction_threshold",
            minimum=Decimal("0"),
            maximum=Decimal("1"),
        ),
        "factor_significance_splits": [
            _text(
                item,
                "statistics.factor_significance_splits item",
                pattern=_IDENTIFIER,
            )
            for item in _sequence(
                statistics["factor_significance_splits"],
                "statistics.factor_significance_splits",
            )
        ],
        "ridge_submodel_policy": _text(
            statistics["ridge_submodel_policy"],
            "statistics.ridge_submodel_policy",
            pattern=_IDENTIFIER,
        ),
    }
    expected_statistics = {
        key: list(value) if isinstance(value, tuple) else value
        for key, value in STATISTICAL_CONTRACT.items()
    }
    if normalized_statistics != expected_statistics:
        raise ExperimentContractError("statistics must exactly match the frozen contract")

    cost = _mapping(root["cost"], "cost")
    _exact_keys(
        cost,
        {
            "currency",
            "commission_rate",
            "minimum_commission",
            "sell_stamp_tax_rate",
            "transfer_fee_rate_both_sides",
            "base_slippage_bps_one_way",
            "stress_slippage_bps_one_way",
            "stress_commission_multiplier",
            "historical_rate_replay",
        },
        "cost",
    )
    if cost["currency"] != "CNY":
        raise ExperimentContractError("cost.currency must be CNY")
    normalized_cost = {
        "currency": "CNY",
        "commission_rate": _decimal_text(
            cost["commission_rate"], "cost.commission_rate", minimum=Decimal("0")
        ),
        "minimum_commission": _decimal_text(
            cost["minimum_commission"], "cost.minimum_commission", minimum=Decimal("0")
        ),
        "sell_stamp_tax_rate": _decimal_text(
            cost["sell_stamp_tax_rate"], "cost.sell_stamp_tax_rate", minimum=Decimal("0")
        ),
        "transfer_fee_rate_both_sides": _decimal_text(
            cost["transfer_fee_rate_both_sides"],
            "cost.transfer_fee_rate_both_sides",
            minimum=Decimal("0"),
        ),
        "base_slippage_bps_one_way": _decimal_text(
            cost["base_slippage_bps_one_way"],
            "cost.base_slippage_bps_one_way",
            minimum=Decimal("0"),
        ),
        "stress_slippage_bps_one_way": _decimal_text(
            cost["stress_slippage_bps_one_way"],
            "cost.stress_slippage_bps_one_way",
            minimum=Decimal("0"),
        ),
        "stress_commission_multiplier": _decimal_text(
            cost["stress_commission_multiplier"],
            "cost.stress_commission_multiplier",
            strictly_positive=True,
        ),
        "historical_rate_replay": _boolean(
            cost["historical_rate_replay"], "cost.historical_rate_replay"
        ),
    }
    exact_costs = {
        "commission_rate": Decimal("0.00018"),
        "minimum_commission": Decimal("5"),
        "sell_stamp_tax_rate": Decimal("0.0005"),
        "transfer_fee_rate_both_sides": Decimal("0.00001"),
        "base_slippage_bps_one_way": Decimal("10"),
        "stress_slippage_bps_one_way": Decimal("20"),
        "stress_commission_multiplier": Decimal("2"),
    }
    for field_name, expected in exact_costs.items():
        if Decimal(normalized_cost[field_name]) != expected:
            raise ExperimentContractError(f"cost.{field_name} must be {expected}")
    if normalized_cost["historical_rate_replay"] is not False:
        raise ExperimentContractError("cost.historical_rate_replay must be false")

    portfolio = _mapping(root["portfolio"], "portfolio")
    _exact_keys(
        portfolio,
        {
            "initial_capital",
            "top_decile_research_capital",
            "lot_size_policy",
            "max_positions",
            "max_weight_per_position",
            "cash_reserve_weight",
            "rebalance_sessions",
            "max_drawdown",
            "entry_top_fraction",
            "hold_top_fraction",
            "positive_prediction_required",
            "manual_veto_policy",
            "selected_positions_industry_policy",
            "combined_account_level1_industry_cap",
            "annual_one_way_turnover_cap",
            "trial_duration_months",
            "paper_decision_points",
            "execution_mode",
            "long_only",
            "unmanaged_external_assets",
        },
        "portfolio",
    )
    initial_capital = _decimal_text(
        portfolio["initial_capital"], "portfolio.initial_capital", strictly_positive=True
    )
    if Decimal(initial_capital) != Decimal("10000"):
        raise ExperimentContractError("portfolio.initial_capital must be 10000")
    top_decile_research_capital = _decimal_text(
        portfolio["top_decile_research_capital"],
        "portfolio.top_decile_research_capital",
        strictly_positive=True,
    )
    if Decimal(top_decile_research_capital) != Decimal("1000000"):
        raise ExperimentContractError(
            "portfolio.top_decile_research_capital must be 1000000"
        )
    max_weight = _decimal_text(
        portfolio["max_weight_per_position"],
        "portfolio.max_weight_per_position",
        strictly_positive=True,
        maximum=Decimal("1"),
    )
    cash_reserve = _decimal_text(
        portfolio["cash_reserve_weight"],
        "portfolio.cash_reserve_weight",
        minimum=Decimal("0"),
        maximum=Decimal("1"),
    )
    if Decimal(max_weight) != Decimal("0.4"):
        raise ExperimentContractError("portfolio.max_weight_per_position must be 0.40")
    if Decimal(cash_reserve) != Decimal("0.2"):
        raise ExperimentContractError("portfolio.cash_reserve_weight must be 0.20")
    max_drawdown = _decimal_text(
        portfolio["max_drawdown"],
        "portfolio.max_drawdown",
        strictly_positive=True,
        maximum=Decimal("1"),
    )
    if _boolean(portfolio["long_only"], "portfolio.long_only") is not True:
        raise ExperimentContractError("portfolio.long_only must be true")
    if (
        _boolean(
            portfolio["positive_prediction_required"],
            "portfolio.positive_prediction_required",
        )
        is not True
    ):
        raise ExperimentContractError(
            "portfolio.positive_prediction_required must be true"
        )
    exact_text_fields = {
        "lot_size_policy": "per_instrument_metadata",
        "selected_positions_industry_policy": "distinct_level1_industry",
        "manual_veto_policy": "leave_cash_no_substitute",
        "execution_mode": "paper_only",
    }
    for field_name, expected in exact_text_fields.items():
        if portfolio[field_name] != expected:
            raise ExperimentContractError(f"portfolio.{field_name} must be {expected}")
    exact_integers = {
        "max_positions": 2,
        "rebalance_sessions": 20,
        "trial_duration_months": 12,
        "paper_decision_points": 12,
    }
    for field_name, expected in exact_integers.items():
        if _integer(portfolio[field_name], f"portfolio.{field_name}", minimum=1) != expected:
            raise ExperimentContractError(f"portfolio.{field_name} must be exactly {expected}")
    industry_cap = _decimal_text(
        portfolio["combined_account_level1_industry_cap"],
        "portfolio.combined_account_level1_industry_cap",
        strictly_positive=True,
        maximum=Decimal("1"),
    )
    if Decimal(industry_cap) != Decimal("0.45"):
        raise ExperimentContractError(
            "portfolio.combined_account_level1_industry_cap must be 0.45"
        )
    turnover_cap = _decimal_text(
        portfolio["annual_one_way_turnover_cap"],
        "portfolio.annual_one_way_turnover_cap",
        strictly_positive=True,
    )
    if Decimal(turnover_cap) != Decimal("4"):
        raise ExperimentContractError(
            "portfolio.annual_one_way_turnover_cap must be 4"
        )
    if Decimal(max_drawdown) != Decimal("0.12"):
        raise ExperimentContractError("portfolio.max_drawdown must be 0.12")
    entry_top_fraction = _decimal_text(
        portfolio["entry_top_fraction"],
        "portfolio.entry_top_fraction",
        strictly_positive=True,
        maximum=Decimal("1"),
    )
    if Decimal(entry_top_fraction) != Decimal("0.05"):
        raise ExperimentContractError("portfolio.entry_top_fraction must be 0.05")
    hold_top_fraction = _decimal_text(
        portfolio["hold_top_fraction"],
        "portfolio.hold_top_fraction",
        strictly_positive=True,
        maximum=Decimal("1"),
    )
    if Decimal(hold_top_fraction) != Decimal("0.20"):
        raise ExperimentContractError("portfolio.hold_top_fraction must be 0.20")
    raw_external_assets = _sequence(
        portfolio["unmanaged_external_assets"],
        "portfolio.unmanaged_external_assets",
    )
    if len(raw_external_assets) != 1:
        raise ExperimentContractError(
            "portfolio.unmanaged_external_assets must contain only the Midea position"
        )
    external_asset = _mapping(
        raw_external_assets[0], "portfolio.unmanaged_external_assets[0]"
    )
    _exact_keys(
        external_asset,
        {
            "instrument_id",
            "quantity",
            "status",
            "level1_industry_code",
            "industry_source_receipt_sha256",
        },
        "portfolio.unmanaged_external_assets[0]",
    )
    if external_asset["instrument_id"] != "000333.SZ":
        raise ExperimentContractError("unmanaged external instrument must be 000333.SZ")
    if _integer(external_asset["quantity"], "unmanaged_external.quantity", minimum=1) != 100:
        raise ExperimentContractError("unmanaged external Midea quantity must be 100")
    if external_asset["status"] != "unmanaged_external":
        raise ExperimentContractError("unmanaged external status must be unmanaged_external")
    normalized_external_asset = {
        "instrument_id": "000333.SZ",
        "quantity": 100,
        "status": "unmanaged_external",
        "level1_industry_code": _text(
            external_asset["level1_industry_code"],
            "unmanaged_external.level1_industry_code",
            pattern=_IDENTIFIER,
        ),
        "industry_source_receipt_sha256": _sha(
            external_asset["industry_source_receipt_sha256"],
            "unmanaged_external.industry_source_receipt_sha256",
        ),
    }
    normalized_portfolio = {
        "initial_capital": initial_capital,
        "top_decile_research_capital": top_decile_research_capital,
        "lot_size_policy": "per_instrument_metadata",
        "max_positions": 2,
        "max_weight_per_position": max_weight,
        "cash_reserve_weight": cash_reserve,
        "rebalance_sessions": 20,
        "max_drawdown": max_drawdown,
        "entry_top_fraction": entry_top_fraction,
        "hold_top_fraction": hold_top_fraction,
        "positive_prediction_required": True,
        "manual_veto_policy": "leave_cash_no_substitute",
        "selected_positions_industry_policy": "distinct_level1_industry",
        "combined_account_level1_industry_cap": industry_cap,
        "annual_one_way_turnover_cap": turnover_cap,
        "trial_duration_months": 12,
        "paper_decision_points": 12,
        "execution_mode": "paper_only",
        "long_only": True,
        "unmanaged_external_assets": [normalized_external_asset],
    }

    normalized_gates: list[dict[str, str]] = []
    for index, raw_gate in enumerate(_sequence(root["gates"], "gates")):
        gate = _mapping(raw_gate, f"gates[{index}]")
        _exact_keys(
            gate,
            {"gate_id", "metric", "operator", "threshold", "scope", "failure_action"},
            f"gates[{index}]",
        )
        normalized_gates.append(
            {
                "gate_id": _text(gate["gate_id"], "gate.gate_id", pattern=_IDENTIFIER),
                "metric": _text(gate["metric"], "gate.metric", pattern=_IDENTIFIER),
                "operator": _enum(gate["operator"], {"gt", "gte", "lt", "lte"}, "gate.operator"),
                "threshold": _decimal_text(gate["threshold"], "gate.threshold"),
                "scope": _enum(
                    gate["scope"],
                    {"validation_only", "validation_and_all_audits"},
                    "gate.scope",
                ),
                "failure_action": _enum(gate["failure_action"], {"reject"}, "gate.failure_action"),
            }
        )
    if not normalized_gates:
        raise ExperimentContractError("gates must not be empty")
    gate_ids = [item["gate_id"] for item in normalized_gates]
    if len(set(gate_ids)) != len(gate_ids):
        raise ExperimentContractError("gate_ids must be unique")
    normalized_gates.sort(key=lambda item: item["gate_id"])
    expected_gates = []
    for frozen_gate in HISTORICAL_GATE_CONTRACTS:
        expected_gates.append(
            {
                **frozen_gate,
                "scope": "validation_and_all_audits",
                "failure_action": "reject",
            }
        )
    expected_gates.sort(key=lambda item: item["gate_id"])
    if normalized_gates != expected_gates:
        raise ExperimentContractError(
            "gates must exactly match all frozen V1 historical admission gates"
        )

    hashes = _mapping(root["hashes"], "hashes")
    _exact_keys(hashes, {"data_receipt_sha256", "code_sha256", "config_sha256"}, "hashes")
    normalized_hashes = {
        "data_receipt_sha256": _sorted_unique_texts(
            hashes["data_receipt_sha256"],
            "hashes.data_receipt_sha256",
            pattern=_SHA256,
            minimum=1,
        ),
        "code_sha256": _sha(hashes["code_sha256"], "hashes.code_sha256"),
        "config_sha256": _sha(hashes["config_sha256"], "hashes.config_sha256"),
    }

    consumed: list[dict[str, str]] = []
    for index, raw_interval in enumerate(
        _sequence(root["consumed_test_intervals"], "consumed_test_intervals")
    ):
        interval = _mapping(raw_interval, f"consumed_test_intervals[{index}]")
        _exact_keys(
            interval,
            {"start_date", "end_date", "source_experiment_id", "source_run_sha256"},
            f"consumed_test_intervals[{index}]",
        )
        start = _date_text(interval["start_date"], "consumed.start_date")
        end = _date_text(interval["end_date"], "consumed.end_date")
        if start > end:
            raise ExperimentContractError("consumed test interval start must not exceed end")
        consumed.append(
            {
                "start_date": start,
                "end_date": end,
                "source_experiment_id": _text(
                    interval["source_experiment_id"],
                    "consumed.source_experiment_id",
                    pattern=_IDENTIFIER,
                ),
                "source_run_sha256": _sha(
                    interval["source_run_sha256"], "consumed.source_run_sha256"
                ),
            }
        )
    consumed.sort(
        key=lambda item: (
            item["start_date"],
            item["end_date"],
            item["source_experiment_id"],
            item["source_run_sha256"],
        )
    )
    identities = [tuple(item.values()) for item in consumed]
    if len(set(identities)) != len(identities):
        raise ExperimentContractError("consumed_test_intervals must not contain duplicates")
    for split_name in ("locked_test", "second_audit"):
        audit_interval = normalized_splits[split_name]
        overlaps_consumed = any(
            item["start_date"] <= audit_interval["end_date"]
            and item["end_date"] >= audit_interval["start_date"]
            for item in consumed
        )
        freshness = normalized_splits[f"{split_name}_freshness"]
        if freshness == "fresh_unconsumed" and overlaps_consumed:
            raise ExperimentContractError(
                f"fresh {split_name} split overlaps a consumed test interval"
            )
        if freshness == "retrospective_consumed" and not overlaps_consumed:
            raise ExperimentContractError(
                f"retrospective {split_name} requires a disclosed overlapping consumed interval"
            )

    return {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": _text(root["experiment_id"], "experiment_id", pattern=_IDENTIFIER),
        "created_at": normalized_created_at,
        "status": STATUS,
        "universe": normalized_universe,
        "benchmark": normalized_benchmark,
        "target": normalized_target,
        "factors": normalized_factors,
        "controls": normalized_controls,
        "splits": normalized_splits,
        "ridge": normalized_ridge,
        "statistics": normalized_statistics,
        "cost": normalized_cost,
        "portfolio": normalized_portfolio,
        "gates": normalized_gates,
        "hashes": normalized_hashes,
        "consumed_test_intervals": consumed,
    }


@dataclass(frozen=True)
class ExperimentSpecV2:
    """Immutable canonical experiment payload plus its self-excluding hash."""

    _content_bytes: bytes
    spec_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self._content_bytes, bytes):
            raise ExperimentContractError("experiment content must be bytes")
        try:
            parsed = json.loads(
                self._content_bytes.decode("utf-8"),
                object_pairs_hook=_strict_json_object,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ExperimentContractError(f"non-finite JSON constant: {value}")
                ),
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExperimentContractError("experiment content is not strict JSON") from exc
        normalized = _normalise_content(parsed)
        canonical = canonical_json_bytes(normalized)
        if canonical != self._content_bytes:
            raise ExperimentContractError("experiment content is not canonical")
        if _SHA256.fullmatch(self.spec_sha256) is None:
            raise ExperimentContractError("spec_sha256 has an invalid format")
        if canonical_sha256(normalized) != self.spec_sha256:
            raise ExperimentContractError("spec_sha256 mismatch")

    @classmethod
    def create(cls, content: Mapping[str, Any]) -> "ExperimentSpecV2":
        """Validate and canonicalize content that does not contain ``spec_sha256``."""

        if "spec_sha256" in content:
            raise ExperimentContractError("create content must not contain spec_sha256")
        normalized = _normalise_content(content)
        return cls(canonical_json_bytes(normalized), canonical_sha256(normalized))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExperimentSpecV2":
        root = _mapping(payload, "experiment payload")
        if "spec_sha256" not in root:
            raise ExperimentContractError("experiment payload is missing spec_sha256")
        content = dict(root)
        declared = _sha(content.pop("spec_sha256"), "spec_sha256")
        normalized = _normalise_content(content)
        if canonical_json_bytes(content) != canonical_json_bytes(normalized):
            raise ExperimentContractError("persisted experiment payload is not canonical")
        return cls(canonical_json_bytes(normalized), declared)

    @property
    def experiment_id(self) -> str:
        return str(self.to_content_dict()["experiment_id"])

    def to_content_dict(self) -> dict[str, Any]:
        return json.loads(self._content_bytes.decode("utf-8"))

    def to_dict(self) -> dict[str, Any]:
        payload = self.to_content_dict()
        payload["spec_sha256"] = self.spec_sha256
        return payload


def write_new_experiment_spec(path: str | Path, spec: ExperimentSpecV2) -> Path:
    """Persist one preregistration without ever replacing an existing file."""

    if not isinstance(spec, ExperimentSpecV2):
        raise ExperimentContractError("spec must be an ExperimentSpecV2")
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    content = canonical_json_bytes(spec.to_dict()) + b"\n"
    try:
        with destination.open("xb") as stream:
            stream.write(content)
    except FileExistsError as exc:
        raise ExperimentContractError(
            f"refusing to overwrite preregistration: {destination}"
        ) from exc
    return destination


def read_experiment_spec(path: str | Path) -> ExperimentSpecV2:
    source = Path(path)
    try:
        raw = source.read_bytes()
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ExperimentContractError(f"non-finite JSON constant: {value}")
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExperimentContractError(f"cannot read experiment spec: {source}") from exc
    return ExperimentSpecV2.from_dict(_mapping(payload, "experiment payload"))


__all__ = [
    "ExperimentContractError",
    "ExperimentSpecV2",
    "HISTORICAL_GATE_CONTRACTS",
    "QUALITY_GROWTH_FACTOR_CONTRACTS",
    "RESIDUALIZATION_CONTROL_CONTRACTS",
    "STATISTICAL_CONTRACT",
    "SCHEMA_VERSION",
    "STATUS",
    "read_experiment_spec",
    "write_new_experiment_spec",
]
