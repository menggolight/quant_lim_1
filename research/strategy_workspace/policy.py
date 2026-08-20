"""Strict loader for the frozen A-share quality-growth policy."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from .contracts import canonical_sha256
from .diagnostic import DIAGNOSTIC_FACTOR_IDS, DIAGNOSTIC_STATUS
from .experiment import QUALITY_GROWTH_FACTOR_CONTRACTS


DEFAULT_POLICY_PATH = Path("configs/strategy_quality_growth.v1.json")
QUALITY_FACTOR_IDS = (
    "QG_ROE_STABILITY",
    "QG_EARNINGS_TREND_DEVIATION",
    "QG_CASH_EARNINGS_QUALITY",
    "QG_CASH_DEBT_COVERAGE",
    "QG_GROSS_PROFITABILITY",
    "QG_REVENUE_GROWTH_STABILITY",
)


class QualityGrowthPolicyError(ValueError):
    """Raised when a configuration weakens the expert-decided contract."""


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise QualityGrowthPolicyError(f"{field} must be an object")
    return value


def _decimal(value: Any, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise QualityGrowthPolicyError(f"{field} must be a decimal") from exc
    if not parsed.is_finite():
        raise QualityGrowthPolicyError(f"{field} must be finite")
    return parsed


def _require_equal(actual: Any, expected: Any, field: str) -> None:
    if actual != expected:
        raise QualityGrowthPolicyError(f"{field} must remain {expected!r}")


@dataclass(frozen=True)
class QualityGrowthPolicy:
    raw: Mapping[str, Any]
    policy_sha256: str

    @property
    def strategy_id(self) -> str:
        return str(self.raw["strategy_id"])

    @property
    def research_status(self) -> str:
        return str(self.raw["research_status"])

    @property
    def factor_ids(self) -> tuple[str, ...]:
        return tuple(str(item["factor_id"]) for item in self.raw["factors"])

    def to_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.raw, ensure_ascii=False))


def validate_quality_growth_policy(payload: Mapping[str, Any]) -> QualityGrowthPolicy:
    """Reject configuration drift that would weaken the V1 safety or research gates."""

    _require_equal(payload.get("schema_version"), "strategy-quality-growth-policy.v1", "schema_version")
    _require_equal(payload.get("execution_status"), "research_only", "execution_status")
    _require_equal(payload.get("research_status"), "blocked_missing_pit_data", "research_status")

    data = _mapping(payload.get("data_policy"), "data_policy")
    _require_equal(data.get("provider_id"), "choice", "data_policy.provider_id")
    _require_equal(data.get("universe_id"), "CSI800_PIT", "data_policy.universe_id")
    _require_equal(data.get("price_index_id"), "000906.CSI", "data_policy.price_index_id")
    _require_equal(data.get("financial_start_date"), "2014-01-01", "data_policy.financial_start_date")
    _require_equal(data.get("price_start_date"), "2017-01-01", "data_policy.price_start_date")
    if data.get("total_return_benchmark_id") is not None:
        raise QualityGrowthPolicyError(
            "total_return_benchmark_id must remain null until a Choice receipt verifies it"
        )
    for field in (
        "single_source_required",
        "total_return_benchmark_must_be_provider_verified",
        "forbid_current_membership_backfill",
        "forbid_success_only_subset",
        "forbid_mixed_provider_fill",
        "first_disclosure_only",
        "qfq_does_not_prove_corporate_actions",
    ):
        _require_equal(data.get(field), True, f"data_policy.{field}")

    target = _mapping(payload.get("target"), "target")
    _require_equal(target.get("horizon_sessions"), 20, "target.horizon_sessions")
    _require_equal(target.get("rebalance_sessions"), 20, "target.rebalance_sessions")
    _require_equal(target.get("signal_time"), "after_close", "target.signal_time")
    _require_equal(
        target.get("execution_time"),
        "next_trading_session_open",
        "target.execution_time",
    )

    factors = payload.get("factors")
    if not isinstance(factors, list) or any(not isinstance(item, Mapping) for item in factors):
        raise QualityGrowthPolicyError("factors must be an object array")
    _require_equal(tuple(item.get("factor_id") for item in factors), QUALITY_FACTOR_IDS, "factor_ids")
    if any(item.get("expected_sign") != "positive" for item in factors):
        raise QualityGrowthPolicyError("all quality-growth factor directions must remain positive")
    expected_factor_contracts = {
        item["factor_id"]: {
            "formula": item["formula"],
            "financial_applicability": item["financial_applicability"],
        }
        for item in QUALITY_GROWTH_FACTOR_CONTRACTS
    }
    for item in factors:
        factor_id = str(item["factor_id"])
        for field, expected in expected_factor_contracts[factor_id].items():
            _require_equal(item.get(field), expected, f"factors.{factor_id}.{field}")

    preprocessing = _mapping(payload.get("preprocessing"), "preprocessing")
    _require_equal(_decimal(preprocessing.get("winsor_lower_quantile"), "preprocessing.winsor_lower_quantile"), Decimal("0.01"), "preprocessing.winsor_lower_quantile")
    _require_equal(_decimal(preprocessing.get("winsor_upper_quantile"), "preprocessing.winsor_upper_quantile"), Decimal("0.99"), "preprocessing.winsor_upper_quantile")
    _require_equal(
        tuple(preprocessing.get("residualize_controls", ())),
        (
            "csi_level1_industry_dummies",
            "log_float_market_cap",
            "earnings_yield",
            "rm120",
            "volatility_60",
        ),
        "preprocessing.residualize_controls",
    )
    _require_equal(preprocessing.get("post_residualization"), "cross_sectional_zscore", "preprocessing.post_residualization")
    _require_equal(preprocessing.get("missing_value_policy"), "drop_never_zero_fill", "preprocessing.missing_value_policy")

    models = _mapping(payload.get("models"), "models")
    _require_equal(models.get("single_factor"), "fama_macbeth_newey_west", "models.single_factor")
    _require_equal(models.get("multi_factor"), "ridge", "models.multi_factor")
    _require_equal(_decimal(models.get("ridge_alpha"), "models.ridge_alpha"), Decimal("1"), "models.ridge_alpha")

    splits = _mapping(payload.get("splits"), "splits")
    _require_equal(tuple(splits.get("train", ())), ("2018-01-01", "2022-12-31"), "splits.train")
    _require_equal(tuple(splits.get("validation", ())), ("2023-01-01", "2023-12-31"), "splits.validation")
    _require_equal(tuple(splits.get("locked_test", ())), ("2024-01-01", "2025-12-31"), "splits.locked_test")
    _require_equal(tuple(splits.get("second_audit", ())), ("2026-01-01", "PRE_REGISTRATION_CUTOFF"), "splits.second_audit")
    _require_equal(splits.get("purge_sessions"), 20, "splits.purge_sessions")

    portfolio = _mapping(payload.get("portfolio"), "portfolio")
    expected_portfolio = {
        "initial_cash": Decimal("10000"),
        "top_decile_research_capital": Decimal("1000000"),
        "max_positions": 2,
        "max_position_weight": Decimal("0.40"),
        "minimum_cash_weight": Decimal("0.20"),
        "entry_top_fraction": Decimal("0.05"),
        "hold_top_fraction": Decimal("0.20"),
        "combined_account_industry_cap": Decimal("0.45"),
    }
    for field, expected in expected_portfolio.items():
        actual = portfolio.get(field)
        if isinstance(expected, Decimal):
            actual = _decimal(actual, f"portfolio.{field}")
        _require_equal(actual, expected, f"portfolio.{field}")
    _require_equal(portfolio.get("distinct_csi_level1_industries"), True, "portfolio.distinct_csi_level1_industries")
    _require_equal(portfolio.get("positive_prediction_required"), True, "portfolio.positive_prediction_required")
    _require_equal(portfolio.get("manual_veto_policy"), "leave_cash_no_substitute", "portfolio.manual_veto_policy")
    _require_equal(portfolio.get("lot_size_policy"), "per_instrument_metadata", "portfolio.lot_size_policy")
    external = portfolio.get("unmanaged_external_assets")
    if not isinstance(external, list) or len(external) != 1:
        raise QualityGrowthPolicyError("exactly one unmanaged external Midea position is required")
    _require_equal(external[0].get("instrument_id"), "000333.SZ", "unmanaged_external.instrument_id")
    _require_equal(external[0].get("quantity"), 100, "unmanaged_external.quantity")
    _require_equal(external[0].get("status"), "unmanaged_external", "unmanaged_external.status")

    costs = _mapping(payload.get("costs"), "costs")
    expected_costs = {
        "commission_rate": "0.00018",
        "minimum_commission": "5",
        "sell_tax_rate": "0.0005",
        "transfer_fee_rate_both_sides": "0.00001",
        "base_slippage_bps_one_way": "10",
        "stress_slippage_bps_one_way": "20",
        "stress_commission_multiplier": "2",
    }
    for field, expected in expected_costs.items():
        _require_equal(_decimal(costs.get(field), f"costs.{field}"), Decimal(expected), f"costs.{field}")
    _require_equal(costs.get("historical_rate_replay"), False, "costs.historical_rate_replay")

    eligibility = _mapping(payload.get("eligibility"), "eligibility")
    _require_equal(eligibility.get("minimum_listing_sessions"), 250, "eligibility.minimum_listing_sessions")
    _require_equal(_decimal(eligibility.get("minimum_average_amount_20"), "eligibility.minimum_average_amount_20"), Decimal("100000000"), "eligibility.minimum_average_amount_20")
    for field in (
        "new_buy_requires_not_st",
        "new_buy_requires_not_suspended",
        "new_buy_requires_not_limit_locked",
    ):
        _require_equal(eligibility.get(field), True, f"eligibility.{field}")
    _require_equal(
        tuple(eligibility.get("sell_constraints", ())),
        ("t_plus_one", "suspension", "limit_down_locked"),
        "eligibility.sell_constraints",
    )

    risk = _mapping(payload.get("risk"), "risk")
    _require_equal(_decimal(risk.get("drawdown_freeze"), "risk.drawdown_freeze"), Decimal("0.12"), "risk.drawdown_freeze")
    _require_equal(_decimal(risk.get("maximum_annualized_one_way_turnover"), "risk.maximum_annualized_one_way_turnover"), Decimal("4"), "risk.maximum_annualized_one_way_turnover")
    _require_equal(risk.get("leverage_allowed"), False, "risk.leverage_allowed")
    _require_equal(risk.get("short_selling_allowed"), False, "risk.short_selling_allowed")
    _require_equal(
        risk.get("drawdown_action"),
        "freeze_new_buys_and_target_cash_next_tradable_decision",
        "risk.drawdown_action",
    )

    historical = _mapping(payload.get("historical_gates"), "historical_gates")
    expected_historical = {
        "data_complete_and_pit": True,
        "top_decile_net_absolute_return_positive": True,
        "top_decile_net_active_return_positive": True,
        "top2_lot_net_absolute_return_positive": True,
        "top2_lot_net_active_return_positive": True,
        "oos_rank_ic_positive_and_stable": True,
        "minimum_corrected_independent_factors": 2,
        "minimum_positive_half_year_windows": 3,
        "half_year_window_count": 4,
        "stress_net_active_return_nonnegative": True,
        "maximum_drawdown": "0.12",
        "maximum_annualized_one_way_turnover": "4",
        "failure_policy": "reject_without_retuning",
    }
    if set(historical) != set(expected_historical):
        raise QualityGrowthPolicyError("historical_gates fields differ from the frozen contract")
    for field, expected in expected_historical.items():
        actual = historical[field]
        if field in {"maximum_drawdown", "maximum_annualized_one_way_turnover"}:
            actual = _decimal(actual, f"historical_gates.{field}")
            expected = Decimal(expected)
        _require_equal(actual, expected, f"historical_gates.{field}")

    paper = _mapping(payload.get("paper"), "paper")
    _require_equal(paper.get("minimum_calendar_months"), 12, "paper.minimum_calendar_months")
    _require_equal(paper.get("minimum_decision_points"), 12, "paper.minimum_decision_points")
    _require_equal(paper.get("configuration_changes_allowed"), False, "paper.configuration_changes_allowed")
    _require_equal(paper.get("passing_status"), "manual_real_money_candidate", "paper.passing_status")
    _require_equal(paper.get("automatic_order_submission"), False, "paper.automatic_order_submission")

    fallback = _mapping(payload.get("fallback"), "fallback")
    _require_equal(fallback.get("status"), DIAGNOSTIC_STATUS, "fallback.status")
    _require_equal(fallback.get("sample_size"), 60, "fallback.sample_size")
    _require_equal(tuple(fallback.get("factor_ids", ())), DIAGNOSTIC_FACTOR_IDS, "fallback.factor_ids")
    _require_equal(fallback.get("paper_eligibility"), False, "fallback.paper_eligibility")
    _require_equal(fallback.get("real_money_list_allowed"), False, "fallback.real_money_list_allowed")

    safety = _mapping(payload.get("safety"), "safety")
    _require_equal(safety.get("paper_eligibility"), False, "safety.paper_eligibility")
    _require_equal(safety.get("trade_eligibility"), False, "safety.trade_eligibility")
    _require_equal(safety.get("live"), "not_supported", "safety.live")

    normalized = json.loads(json.dumps(payload, ensure_ascii=False))
    return QualityGrowthPolicy(raw=normalized, policy_sha256=canonical_sha256(normalized))


def load_quality_growth_policy(path: Path | str = DEFAULT_POLICY_PATH) -> QualityGrowthPolicy:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QualityGrowthPolicyError(f"cannot read quality-growth policy: {path}") from exc
    if not isinstance(payload, Mapping):
        raise QualityGrowthPolicyError("quality-growth policy root must be an object")
    return validate_quality_growth_policy(payload)


__all__ = [
    "DEFAULT_POLICY_PATH",
    "QUALITY_FACTOR_IDS",
    "QualityGrowthPolicy",
    "QualityGrowthPolicyError",
    "load_quality_growth_policy",
    "validate_quality_growth_policy",
]
