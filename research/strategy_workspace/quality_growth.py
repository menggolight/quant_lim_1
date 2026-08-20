"""Point-in-time quality/growth factor definitions and calculations.

The six factors in this module are deliberately small and pre-registered.  A
calculation consumes only first-disclosure quarterly observations that were
available by ``decision_at``.  Missing inputs, incomplete windows and invalid
denominators remain missing; they are never converted to zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
import math
import re
from typing import Iterable, Mapping

import numpy as np

from .regression import RegressionError, fit_ols


class QualityGrowthError(ValueError):
    """Raised when PIT identity or chronology is ambiguous."""


class FactorAvailability(str, Enum):
    AVAILABLE = "available"
    MISSING = "missing"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class QualityGrowthFactorSpec:
    factor_id: str
    formula: str
    expected_sign: str = "positive"
    financial_applicable: bool = True


QUALITY_GROWTH_FACTOR_SPECS: tuple[QualityGrowthFactorSpec, ...] = (
    QualityGrowthFactorSpec(
        "QG_ROE_STABILITY",
        "latest_quarter_roe - sample_std(last_12_quarters_roe, ddof=1)",
    ),
    QualityGrowthFactorSpec(
        "QG_EARNINGS_TREND_DEVIATION",
        "(latest_quarter_net_profit - OLS_trend_prediction(preceding_8_quarters_net_profit)) / sample_std(preceding_8_OLS_residuals, ddof=1)",
    ),
    QualityGrowthFactorSpec(
        "QG_CASH_EARNINGS_QUALITY",
        "(TTM_operating_cash_flow - TTM_operating_profit) / latest_total_assets",
        financial_applicable=False,
    ),
    QualityGrowthFactorSpec(
        "QG_CASH_DEBT_COVERAGE",
        "TTM_operating_cash_flow / latest_total_liabilities",
        financial_applicable=False,
    ),
    QualityGrowthFactorSpec(
        "QG_GROSS_PROFITABILITY",
        "TTM_gross_profit / mean(latest_total_assets, total_assets_4q_ago)",
        financial_applicable=False,
    ),
    QualityGrowthFactorSpec(
        "QG_REVENUE_GROWTH_STABILITY",
        "mean(last_8_quarterly_yoy_revenue_growth) - sample_std(last_8_quarterly_yoy_revenue_growth, ddof=1)",
        financial_applicable=False,
    ),
)

QUALITY_GROWTH_FACTOR_IDS = tuple(item.factor_id for item in QUALITY_GROWTH_FACTOR_SPECS)
FINANCIAL_FACTOR_IDS = tuple(
    item.factor_id for item in QUALITY_GROWTH_FACTOR_SPECS if item.financial_applicable
)
FLOW_BASIS = "single_quarter"
STATEMENT_SCOPE = "consolidated"
CURRENCY = "CNY"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise QualityGrowthError(f"{field} must be timezone-aware")
    return value


def _quarter_ordinal(value: date) -> int:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise QualityGrowthError("period_end must be a date")
    quarter_ends = {(3, 31): 1, (6, 30): 2, (9, 30): 3, (12, 31): 4}
    quarter = quarter_ends.get((value.month, value.day))
    if quarter is None:
        raise QualityGrowthError("period_end must be a calendar quarter end")
    return value.year * 4 + quarter


def _optional_number(value: float | int | None, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise QualityGrowthError(f"{field} must be numeric or null")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise QualityGrowthError(f"{field} must be numeric or null") from exc


@dataclass(frozen=True, slots=True)
class QuarterlyFundamental:
    """One quarterly observation as it first appeared to the market.

    Flow fields, including ``revenue``, are single-quarter values.  Revenue
    growth is derived here from the first-disclosure revenue history so a
    caller cannot inject an unbound, later-revised growth series.
    """

    instrument_id: str
    period_end: date
    first_disclosed_at: datetime
    source_record_id: str
    source_record_sha256: str
    revision_sequence: int
    flow_basis: str = FLOW_BASIS
    statement_scope: str = STATEMENT_SCOPE
    currency: str = CURRENCY
    roe: float | None = None
    net_profit: float | None = None
    operating_cash_flow: float | None = None
    operating_profit: float | None = None
    gross_profit: float | None = None
    total_assets: float | None = None
    total_liabilities: float | None = None
    revenue: float | None = None

    def __post_init__(self) -> None:
        instrument_id = str(self.instrument_id).strip()
        source_record_id = str(self.source_record_id).strip()
        if not instrument_id or not source_record_id:
            raise QualityGrowthError("instrument_id and source_record_id are required")
        _quarter_ordinal(self.period_end)
        disclosed = _aware(self.first_disclosed_at, "first_disclosed_at")
        if disclosed.date() < self.period_end:
            raise QualityGrowthError("first_disclosed_at cannot precede period_end")
        if _SHA256.fullmatch(str(self.source_record_sha256)) is None:
            raise QualityGrowthError("source_record_sha256 must be a SHA-256 digest")
        if type(self.revision_sequence) is not int or self.revision_sequence < 1:
            raise QualityGrowthError("revision_sequence must be a positive integer")
        if self.flow_basis != FLOW_BASIS:
            raise QualityGrowthError(f"flow_basis must be {FLOW_BASIS}")
        if self.statement_scope != STATEMENT_SCOPE:
            raise QualityGrowthError(f"statement_scope must be {STATEMENT_SCOPE}")
        if self.currency != CURRENCY:
            raise QualityGrowthError(f"currency must be {CURRENCY}")
        for field in (
            "roe",
            "net_profit",
            "operating_cash_flow",
            "operating_profit",
            "gross_profit",
            "total_assets",
            "total_liabilities",
            "revenue",
        ):
            object.__setattr__(self, field, _optional_number(getattr(self, field), field))
        object.__setattr__(self, "instrument_id", instrument_id)
        object.__setattr__(self, "source_record_id", source_record_id)


@dataclass(frozen=True, slots=True)
class QualityGrowthFactorValue:
    factor_id: str
    expected_sign: str
    value: float | None
    availability: FactorAvailability
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.factor_id not in QUALITY_GROWTH_FACTOR_IDS:
            raise QualityGrowthError(f"unknown quality/growth factor: {self.factor_id}")
        if self.expected_sign != "positive":
            raise QualityGrowthError("quality/growth expected_sign must be positive")
        if self.availability is FactorAvailability.AVAILABLE:
            if self.value is None or not math.isfinite(self.value):
                raise QualityGrowthError("available factor values must be finite")
            if self.reason is not None:
                raise QualityGrowthError("available factor values cannot have a reason")
        elif self.value is not None:
            raise QualityGrowthError("missing or not-applicable factors must remain null")


@dataclass(frozen=True, slots=True)
class QualityGrowthSnapshot:
    instrument_id: str
    decision_at: datetime
    latest_period_end: date | None
    input_available_at_max: datetime | None
    industry_is_financial: bool
    factors: tuple[QualityGrowthFactorValue, ...]
    source_record_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        decision = _aware(self.decision_at, "decision_at")
        if self.latest_period_end is not None and self.latest_period_end > decision.date():
            raise QualityGrowthError("latest_period_end cannot follow decision_at")
        if self.input_available_at_max is not None:
            available = _aware(self.input_available_at_max, "input_available_at_max")
            if available > decision:
                raise QualityGrowthError("factor input was unavailable at decision_at")
        if type(self.industry_is_financial) is not bool:
            raise QualityGrowthError("industry_is_financial must be boolean")
        if tuple(item.factor_id for item in self.factors) != QUALITY_GROWTH_FACTOR_IDS:
            raise QualityGrowthError("snapshot must contain the exact ordered six-factor family")
        specs = {item.factor_id: item for item in QUALITY_GROWTH_FACTOR_SPECS}
        for item in self.factors:
            must_be_na = self.industry_is_financial and not specs[item.factor_id].financial_applicable
            if must_be_na and item.availability is not FactorAvailability.NOT_APPLICABLE:
                raise QualityGrowthError("financial factor subset must be explicitly not_applicable")
            if not must_be_na and item.availability is FactorAvailability.NOT_APPLICABLE:
                raise QualityGrowthError("not_applicable is reserved for the financial factor subset")
        if len(self.source_record_ids) != len(set(self.source_record_ids)):
            raise QualityGrowthError("snapshot source_record_ids must be unique")
        if bool(self.source_record_ids) != (self.input_available_at_max is not None):
            raise QualityGrowthError(
                "input_available_at_max must accompany visible source records"
            )

    @property
    def values(self) -> Mapping[str, float | None]:
        return {item.factor_id: item.value for item in self.factors}

    @property
    def availability(self) -> Mapping[str, FactorAvailability]:
        return {item.factor_id: item.availability for item in self.factors}


def _contiguous_tail(
    records: tuple[QuarterlyFundamental, ...], count: int
) -> tuple[QuarterlyFundamental, ...] | None:
    if len(records) < count:
        return None
    tail = records[-count:]
    ordinals = [_quarter_ordinal(item.period_end) for item in tail]
    if any(current != previous + 1 for previous, current in zip(ordinals, ordinals[1:])):
        return None
    return tail


def _finite_field(
    records: tuple[QuarterlyFundamental, ...], field: str
) -> np.ndarray | None:
    raw = [getattr(item, field) for item in records]
    if any(value is None or not math.isfinite(value) for value in raw):
        return None
    return np.asarray(raw, dtype=np.float64)


def _available(factor_id: str, value: float) -> QualityGrowthFactorValue:
    return QualityGrowthFactorValue(
        factor_id=factor_id,
        expected_sign="positive",
        value=float(value),
        availability=FactorAvailability.AVAILABLE,
    )


def _missing(factor_id: str, reason: str) -> QualityGrowthFactorValue:
    return QualityGrowthFactorValue(
        factor_id=factor_id,
        expected_sign="positive",
        value=None,
        availability=FactorAvailability.MISSING,
        reason=reason,
    )


def _not_applicable(factor_id: str) -> QualityGrowthFactorValue:
    return QualityGrowthFactorValue(
        factor_id=factor_id,
        expected_sign="positive",
        value=None,
        availability=FactorAvailability.NOT_APPLICABLE,
        reason="financial_industry_not_applicable",
    )


def _compute_values(
    records: tuple[QuarterlyFundamental, ...], *, industry_is_financial: bool
) -> tuple[QualityGrowthFactorValue, ...]:
    results: dict[str, QualityGrowthFactorValue] = {}

    roe_window = _contiguous_tail(records, 12)
    roe = _finite_field(roe_window, "roe") if roe_window else None
    if roe is None:
        results["QG_ROE_STABILITY"] = _missing("QG_ROE_STABILITY", "window_or_value_missing")
    else:
        results["QG_ROE_STABILITY"] = _available(
            "QG_ROE_STABILITY", roe[-1] - float(np.std(roe, ddof=1))
        )

    earnings_window = _contiguous_tail(records, 9)
    earnings = _finite_field(earnings_window, "net_profit") if earnings_window else None
    if earnings is None:
        results["QG_EARNINGS_TREND_DEVIATION"] = _missing(
            "QG_EARNINGS_TREND_DEVIATION", "window_or_value_missing"
        )
    else:
        preceding = earnings[:-1]
        try:
            fit = fit_ols(np.arange(8, dtype=np.float64), preceding)
        except RegressionError:
            results["QG_EARNINGS_TREND_DEVIATION"] = _missing(
                "QG_EARNINGS_TREND_DEVIATION", "trend_fit_failed"
            )
        else:
            residual_std = float(np.std(fit.residuals, ddof=1))
            residual_floor = float(
                np.finfo(np.float64).eps
                * max(1.0, float(np.max(np.abs(preceding))))
                * 16.0
            )
            if not math.isfinite(residual_std) or residual_std <= residual_floor:
                results["QG_EARNINGS_TREND_DEVIATION"] = _missing(
                    "QG_EARNINGS_TREND_DEVIATION", "residual_std_non_positive"
                )
            else:
                prediction = float(fit.predict(np.asarray([8.0]))[0])
                results["QG_EARNINGS_TREND_DEVIATION"] = _available(
                    "QG_EARNINGS_TREND_DEVIATION",
                    (earnings[-1] - prediction) / residual_std,
                )

    financial_only_excluded = {
        "QG_CASH_EARNINGS_QUALITY",
        "QG_CASH_DEBT_COVERAGE",
        "QG_GROSS_PROFITABILITY",
        "QG_REVENUE_GROWTH_STABILITY",
    }
    if industry_is_financial:
        for factor_id in financial_only_excluded:
            results[factor_id] = _not_applicable(factor_id)
    else:
        ttm = _contiguous_tail(records, 4)
        assets_latest = records[-1].total_assets if records else None
        cash_flow = _finite_field(ttm, "operating_cash_flow") if ttm else None
        operating_profit = _finite_field(ttm, "operating_profit") if ttm else None
        gross_profit = _finite_field(ttm, "gross_profit") if ttm else None

        if (
            cash_flow is None
            or operating_profit is None
            or assets_latest is None
            or not math.isfinite(assets_latest)
            or assets_latest <= 0.0
        ):
            results["QG_CASH_EARNINGS_QUALITY"] = _missing(
                "QG_CASH_EARNINGS_QUALITY", "window_or_denominator_invalid"
            )
        else:
            results["QG_CASH_EARNINGS_QUALITY"] = _available(
                "QG_CASH_EARNINGS_QUALITY",
                (float(np.sum(cash_flow)) - float(np.sum(operating_profit))) / assets_latest,
            )

        liabilities = records[-1].total_liabilities if records else None
        if (
            cash_flow is None
            or liabilities is None
            or not math.isfinite(liabilities)
            or liabilities <= 0.0
        ):
            results["QG_CASH_DEBT_COVERAGE"] = _missing(
                "QG_CASH_DEBT_COVERAGE", "window_or_denominator_invalid"
            )
        else:
            results["QG_CASH_DEBT_COVERAGE"] = _available(
                "QG_CASH_DEBT_COVERAGE", float(np.sum(cash_flow)) / liabilities
            )

        asset_window = _contiguous_tail(records, 5)
        assets_then = asset_window[0].total_assets if asset_window else None
        if (
            gross_profit is None
            or assets_latest is None
            or assets_then is None
            or not math.isfinite(assets_latest)
            or not math.isfinite(assets_then)
            or assets_latest <= 0.0
            or assets_then <= 0.0
        ):
            results["QG_GROSS_PROFITABILITY"] = _missing(
                "QG_GROSS_PROFITABILITY", "window_or_denominator_invalid"
            )
        else:
            average_assets = (assets_latest + assets_then) / 2.0
            results["QG_GROSS_PROFITABILITY"] = _available(
                "QG_GROSS_PROFITABILITY", float(np.sum(gross_profit)) / average_assets
            )

    if not industry_is_financial:
        # Eight quarterly YoY observations require twelve contiguous raw
        # revenue quarters.  We intentionally derive them from the same
        # first-disclosure records used by every other factor.
        revenue_window = _contiguous_tail(records, 12)
        revenue = _finite_field(revenue_window, "revenue") if revenue_window else None
        if revenue is None or np.any(revenue[:8] <= 0.0):
            results["QG_REVENUE_GROWTH_STABILITY"] = _missing(
                "QG_REVENUE_GROWTH_STABILITY", "window_or_denominator_invalid"
            )
        else:
            revenue_growth = revenue[4:] / revenue[:8] - 1.0
            results["QG_REVENUE_GROWTH_STABILITY"] = _available(
                "QG_REVENUE_GROWTH_STABILITY",
                float(np.mean(revenue_growth) - np.std(revenue_growth, ddof=1)),
            )

    return tuple(results[factor_id] for factor_id in QUALITY_GROWTH_FACTOR_IDS)


def compute_quality_growth_snapshot(
    records: Iterable[QuarterlyFundamental],
    *,
    decision_at: datetime,
    industry_is_financial: bool,
) -> QualityGrowthSnapshot:
    """Calculate the exact six-factor family from PIT first disclosures."""

    decision = _aware(decision_at, "decision_at")
    if type(industry_is_financial) is not bool:
        raise QualityGrowthError("industry_is_financial must be boolean")
    materialized = tuple(records)
    if any(not isinstance(item, QuarterlyFundamental) for item in materialized):
        raise QualityGrowthError("records must contain QuarterlyFundamental objects")
    instrument_ids = {item.instrument_id for item in materialized}
    if len(instrument_ids) != 1:
        raise QualityGrowthError("records must describe exactly one instrument")
    instrument_id = next(iter(instrument_ids))

    visible = tuple(
        item
        for item in materialized
        if item.revision_sequence == 1 and item.first_disclosed_at <= decision
    )
    by_period: dict[date, QuarterlyFundamental] = {}
    for item in visible:
        if item.period_end in by_period:
            raise QualityGrowthError(
                f"duplicate first-disclosure period: {item.period_end.isoformat()}"
            )
        by_period[item.period_end] = item
    ordered = tuple(sorted(by_period.values(), key=lambda item: item.period_end))
    if ordered:
        factors = _compute_values(ordered, industry_is_financial=industry_is_financial)
    else:
        factors = tuple(
            _not_applicable(item.factor_id)
            if industry_is_financial and not item.financial_applicable
            else _missing(item.factor_id, "no_visible_first_disclosure")
            for item in QUALITY_GROWTH_FACTOR_SPECS
        )
    return QualityGrowthSnapshot(
        instrument_id=instrument_id,
        decision_at=decision,
        latest_period_end=ordered[-1].period_end if ordered else None,
        input_available_at_max=(
            max(item.first_disclosed_at for item in ordered) if ordered else None
        ),
        industry_is_financial=industry_is_financial,
        factors=factors,
        source_record_ids=tuple(item.source_record_id for item in ordered),
    )


__all__ = [
    "CURRENCY",
    "FINANCIAL_FACTOR_IDS",
    "FLOW_BASIS",
    "QUALITY_GROWTH_FACTOR_IDS",
    "QUALITY_GROWTH_FACTOR_SPECS",
    "FactorAvailability",
    "QualityGrowthError",
    "QualityGrowthFactorSpec",
    "QualityGrowthFactorValue",
    "QualityGrowthSnapshot",
    "QuarterlyFundamental",
    "STATEMENT_SCOPE",
    "compute_quality_growth_snapshot",
]
