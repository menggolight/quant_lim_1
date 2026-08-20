"""PIT cross-sectional evaluation for the frozen quality/growth factor family.

The module is intentionally an evaluator, not a portfolio or execution layer.
It validates complete point-in-time constituent snapshots, applies the frozen
cross-sectional preprocessing recipe, and produces explanatory Fama--MacBeth
tests plus genuinely prior-data Ridge and direction-equal-weight predictions.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import InitVar, dataclass, fields, is_dataclass
from datetime import date, datetime
from enum import Enum
import math
import re
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence

import numpy as np

from .contracts import canonical_sha256
from .experiment import ExperimentSpecV2, STATISTICAL_CONTRACT
from .preprocessing import (
    PreprocessingError,
    rank_cross_section,
    residualize_cross_section,
    winsorize_cross_section,
    zscore_cross_section,
)
from .quality_growth import (
    FINANCIAL_FACTOR_IDS,
    QUALITY_GROWTH_FACTOR_IDS,
    FactorAvailability,
    QualityGrowthSnapshot,
)
from .regression import RegressionError, fama_macbeth, fit_ridge


HORIZON_SESSIONS = 20
RETURN_BASIS = "next_session_open_to_open"
RIDGE_ALPHA = 1.0
CONTROL_NAMES = ("log_float_cap", "earnings_yield", "rm120", "vol60")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FORMAL_EVALUATION_TOKEN = object()
SPLIT_RANGES: Mapping[str, tuple[date, date]] = MappingProxyType(
    {
        "train": (date(2018, 1, 1), date(2022, 12, 31)),
        "validation": (date(2023, 1, 1), date(2023, 12, 31)),
        "locked_test": (date(2024, 1, 1), date(2025, 12, 31)),
        "audit": (date(2026, 1, 1), date(2026, 12, 31)),
    }
)


class EvaluationError(ValueError):
    """Raised when PIT, chronology or cross-section completeness is unsafe."""


def _aware(value: datetime | None, field: str) -> datetime:
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        raise EvaluationError(f"{field} must be timezone-aware")
    return value


def _finite(value: float | int, field: str) -> float:
    if isinstance(value, bool):
        raise EvaluationError(f"{field} must be finite")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise EvaluationError(f"{field} must be finite") from exc
    if not math.isfinite(result):
        raise EvaluationError(f"{field} must be finite")
    return result


def _sha256(value: str, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise EvaluationError(f"{field} must be a lowercase SHA-256")
    return value


@dataclass(frozen=True, slots=True)
class BenchmarkTotalReturnPoint:
    """One controlled open level from the benchmark total-return series."""

    session_date: date
    open_level: float

    def __post_init__(self) -> None:
        if not isinstance(self.session_date, date) or isinstance(
            self.session_date, datetime
        ):
            raise EvaluationError("benchmark session_date must be a date")
        level = _finite(self.open_level, "benchmark open_level")
        if level <= 0.0:
            raise EvaluationError("benchmark open_level must be positive")
        object.__setattr__(self, "open_level", level)


@dataclass(frozen=True, slots=True)
class ControlledSourceBinding:
    """Cryptographic references to the frozen, controlled source bundle.

    This is deliberately a collection of content/receipt hashes rather than a
    caller-supplied readiness flag.  Formal evaluation checks every field
    against ``ExperimentSpecV2`` and derives the calendar/membership/benchmark
    content hashes again from the supplied inputs.
    """

    experiment_spec_sha256: str
    membership_panel_receipt_sha256: str
    membership_panel_content_sha256: str
    benchmark_instrument_id: str
    benchmark_instrument_source_receipt_sha256: str
    benchmark_total_return_series_content_sha256: str
    financial_data_receipt_sha256: str
    industry_data_receipt_sha256: str
    control_data_receipt_sha256: str

    def __post_init__(self) -> None:
        for field in (
            "experiment_spec_sha256",
            "membership_panel_receipt_sha256",
            "membership_panel_content_sha256",
            "benchmark_instrument_source_receipt_sha256",
            "benchmark_total_return_series_content_sha256",
            "financial_data_receipt_sha256",
            "industry_data_receipt_sha256",
            "control_data_receipt_sha256",
        ):
            object.__setattr__(self, field, _sha256(getattr(self, field), field))
        instrument = str(self.benchmark_instrument_id).strip()
        if not instrument:
            raise EvaluationError("benchmark_instrument_id is required")
        object.__setattr__(self, "benchmark_instrument_id", instrument)


def trading_calendar_content_sha256(trading_calendar: Sequence[date]) -> str:
    """Hash the exact ordered controlled trading calendar used by evaluation."""

    calendar = tuple(trading_calendar)
    if not calendar or any(
        not isinstance(item, date) or isinstance(item, datetime) for item in calendar
    ):
        raise EvaluationError("trading_calendar must contain dates")
    if tuple(sorted(set(calendar))) != calendar:
        raise EvaluationError("trading_calendar must be unique and strictly increasing")
    return canonical_sha256(calendar)


def benchmark_total_return_series_content_sha256(
    series: Sequence[BenchmarkTotalReturnPoint],
) -> str:
    """Hash an ordered benchmark total-return open-level series."""

    points = tuple(series)
    if not points or any(not isinstance(item, BenchmarkTotalReturnPoint) for item in points):
        raise EvaluationError(
            "benchmark_total_return_series must contain BenchmarkTotalReturnPoint objects"
        )
    dates = tuple(item.session_date for item in points)
    if tuple(sorted(set(dates))) != dates:
        raise EvaluationError(
            "benchmark total-return sessions must be unique and strictly increasing"
        )
    return canonical_sha256(points)


def _split(
    day: date,
    split_ranges: Mapping[str, tuple[date, date]] = SPLIT_RANGES,
) -> str | None:
    for name, (start, end) in split_ranges.items():
        if start <= day <= end:
            return name
    return None


@dataclass(frozen=True, slots=True)
class PitObservation:
    snapshot: QualityGrowthSnapshot
    industry_id: str
    constituent_available_at: datetime
    industry_available_at: datetime
    controls_available_at: datetime
    log_float_cap: float
    earnings_yield: float
    rm120: float
    vol60: float
    label_start_date: date
    label_end_date: date
    return_basis: str
    forward_total_return_20d: float | None
    benchmark_total_return_20d: float | None
    forward_excess_return_20d: float | None
    outcome_available_at: datetime | None

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, QualityGrowthSnapshot):
            raise EvaluationError("snapshot must be a QualityGrowthSnapshot")
        if not str(self.industry_id).strip():
            raise EvaluationError("industry_id is required")
        for field in (
            "constituent_available_at",
            "industry_available_at",
            "controls_available_at",
        ):
            _aware(getattr(self, field), field)
        for field in CONTROL_NAMES:
            object.__setattr__(self, field, _finite(getattr(self, field), field))
        for field in ("label_start_date", "label_end_date"):
            value = getattr(self, field)
            if not isinstance(value, date) or isinstance(value, datetime):
                raise EvaluationError(f"{field} must be a date")
        if self.label_start_date >= self.label_end_date:
            raise EvaluationError("label_start_date must precede label_end_date")
        if self.return_basis != RETURN_BASIS:
            raise EvaluationError(f"return_basis must be {RETURN_BASIS}")
        outcomes = (
            self.forward_total_return_20d,
            self.benchmark_total_return_20d,
            self.forward_excess_return_20d,
        )
        if all(value is None for value in outcomes):
            if self.outcome_available_at is not None:
                raise EvaluationError("missing outcomes cannot have outcome_available_at")
        elif any(value is None for value in outcomes):
            raise EvaluationError("total, benchmark and excess returns must mature together")
        else:
            total = _finite(self.forward_total_return_20d, "forward_total_return_20d")
            benchmark = _finite(
                self.benchmark_total_return_20d, "benchmark_total_return_20d"
            )
            excess = _finite(self.forward_excess_return_20d, "forward_excess_return_20d")
            if not math.isclose(total - benchmark, excess, rel_tol=0.0, abs_tol=1.0e-12):
                raise EvaluationError("forward excess return must equal total minus benchmark")
            available = _aware(self.outcome_available_at, "outcome_available_at")
            if available.date() < self.label_end_date:
                raise EvaluationError("outcome_available_at cannot precede label_end_date")
            object.__setattr__(self, "forward_total_return_20d", total)
            object.__setattr__(self, "benchmark_total_return_20d", benchmark)
            object.__setattr__(self, "forward_excess_return_20d", excess)

    @property
    def instrument_id(self) -> str:
        return self.snapshot.instrument_id


@dataclass(frozen=True, slots=True)
class PitCrossSection:
    decision_at: datetime
    universe_as_of: date
    universe_available_at: datetime
    universe_version: str
    member_ids: tuple[str, ...]
    observations: tuple[PitObservation, ...]
    source_binding: ControlledSourceBinding

    def __post_init__(self) -> None:
        decision = _aware(self.decision_at, "decision_at")
        universe_available = _aware(self.universe_available_at, "universe_available_at")
        if self.universe_as_of != decision.date():
            raise EvaluationError("universe_as_of must equal the decision date")
        if universe_available > decision:
            raise EvaluationError("future constituent snapshot")
        if not str(self.universe_version).strip():
            raise EvaluationError("universe_version is required")
        members = tuple(str(item).strip() for item in self.member_ids)
        if not members or any(not item for item in members) or len(members) != len(set(members)):
            raise EvaluationError("member_ids must be non-empty and unique")
        observations = tuple(self.observations)
        if any(not isinstance(item, PitObservation) for item in observations):
            raise EvaluationError("observations must contain PitObservation objects")
        if not isinstance(self.source_binding, ControlledSourceBinding):
            raise EvaluationError(
                "formal cross-sections require a ControlledSourceBinding"
            )
        observed = [item.instrument_id for item in observations]
        if len(observed) != len(set(observed)) or set(observed) != set(members):
            raise EvaluationError("cross-section observations must exactly match PIT member_ids")
        outcome_presence = {item.forward_excess_return_20d is not None for item in observations}
        if len(outcome_presence) > 1:
            raise EvaluationError("a complete cross-section cannot mix mature and missing outcomes")
        benchmark_returns = {
            item.benchmark_total_return_20d
            for item in observations
            if item.benchmark_total_return_20d is not None
        }
        if len(benchmark_returns) > 1:
            raise EvaluationError("one cross-section must use one benchmark total return")
        for item in observations:
            if item.snapshot.decision_at != decision:
                raise EvaluationError("factor snapshot decision_at mismatch")
            if item.constituent_available_at > decision:
                raise EvaluationError("future constituent membership")
            if item.industry_available_at > decision:
                raise EvaluationError("future industry classification")
            if item.controls_available_at > decision:
                raise EvaluationError("future risk-control input")
            if item.outcome_available_at is not None and item.outcome_available_at <= decision:
                raise EvaluationError("forward outcome cannot be available at decision time")
        object.__setattr__(self, "member_ids", members)
        object.__setattr__(self, "observations", observations)


def membership_panel_content_sha256(
    cross_sections: Sequence[PitCrossSection],
) -> str:
    """Hash the normalized decision-date constituent panel actually evaluated."""

    sections = tuple(cross_sections)
    if not sections or any(not isinstance(item, PitCrossSection) for item in sections):
        raise EvaluationError("membership panel requires PitCrossSection objects")
    ordered = tuple(sorted(sections, key=lambda item: item.decision_at))
    decision_dates = tuple(item.decision_at.date() for item in ordered)
    if len(decision_dates) != len(set(decision_dates)):
        raise EvaluationError("membership panel has duplicate decision dates")
    payload = tuple(
        {
            "decision_at": item.decision_at,
            "universe_as_of": item.universe_as_of,
            "universe_available_at": item.universe_available_at,
            "universe_version": item.universe_version,
            # Constituent identity is set-valued; canonicalize it explicitly.
            "member_ids": tuple(sorted(item.member_ids)),
        }
        for item in ordered
    )
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class PreparedObservation:
    instrument_id: str
    industry_is_financial: bool
    factor_scores: Mapping[str, float]
    forward_total_return_20d: float | None
    benchmark_total_return_20d: float | None
    forward_excess_return_20d: float | None
    outcome_available_at: datetime | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "factor_scores", MappingProxyType(dict(self.factor_scores)))


@dataclass(frozen=True, slots=True)
class PreparedCrossSection:
    decision_at: datetime
    split: str
    label_start_date: date
    label_end_date: date
    return_basis: str
    observations: tuple[PreparedObservation, ...]
    factor_failures: Mapping[str, str]

    def __post_init__(self) -> None:
        _aware(self.decision_at, "decision_at")
        if self.split not in SPLIT_RANGES:
            raise EvaluationError("prepared split is outside the frozen policy")
        if self.return_basis != RETURN_BASIS:
            raise EvaluationError(f"return_basis must be {RETURN_BASIS}")
        if self.label_start_date >= self.label_end_date:
            raise EvaluationError("label_start_date must precede label_end_date")
        if not self.observations:
            raise EvaluationError("prepared cross-section cannot be empty")
        object.__setattr__(self, "factor_failures", MappingProxyType(dict(self.factor_failures)))


@dataclass(frozen=True, slots=True)
class PanelExclusion:
    decision_date: date
    reason_code: str


@dataclass(frozen=True, slots=True)
class PreparedPanel:
    cross_sections: tuple[PreparedCrossSection, ...]
    exclusions: tuple[PanelExclusion, ...]
    experiment_id: str
    experiment_spec_sha256: str
    source_bundle_sha256: str
    horizon_sessions: int = HORIZON_SESSIONS

    def __post_init__(self) -> None:
        if self.horizon_sessions != HORIZON_SESSIONS:
            raise EvaluationError("prepared panel horizon must remain 20 sessions")
        if not str(self.experiment_id).strip():
            raise EvaluationError("prepared panel experiment_id is required")
        _sha256(self.experiment_spec_sha256, "experiment_spec_sha256")
        _sha256(self.source_bundle_sha256, "source_bundle_sha256")


@dataclass(frozen=True, slots=True)
class FactorTestResult:
    split: str
    factor_id: str
    status: str
    coefficient: float | None
    t_statistic: float | None
    raw_p_value: float | None
    holm_p_value: float | None
    periods: int
    observations: int
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"estimated", "insufficient"}:
            raise EvaluationError("factor test status is invalid")
        numeric = (
            self.coefficient,
            self.t_statistic,
            self.raw_p_value,
            self.holm_p_value,
        )
        if any(value is not None and not math.isfinite(float(value)) for value in numeric):
            raise EvaluationError("factor test statistics must be finite or null")
        if self.status == "estimated" and any(
            value is None
            for value in (self.coefficient, self.t_statistic, self.raw_p_value)
        ):
            raise EvaluationError("estimated factor tests require finite core statistics")
        if self.status == "insufficient" and any(value is not None for value in numeric):
            raise EvaluationError("insufficient factor tests must keep statistics null")
        for field_name in ("raw_p_value", "holm_p_value"):
            value = getattr(self, field_name)
            if value is not None and not 0.0 <= float(value) <= 1.0:
                raise EvaluationError(f"{field_name} must be within [0, 1]")


@dataclass(frozen=True, slots=True)
class OOSPrediction:
    split: str
    decision_date: date
    label_start_date: date
    label_end_date: date
    return_basis: str
    instrument_id: str
    model: str
    prediction: float
    actual_forward_total_return_20d: float | None
    benchmark_total_return_20d: float | None
    actual_forward_excess_return_20d: float | None
    outcome_available_at: datetime | None

    def __post_init__(self) -> None:
        if self.split not in {"validation", "locked_test", "audit"}:
            raise EvaluationError("prediction split must be out-of-sample")
        if self.return_basis != RETURN_BASIS:
            raise EvaluationError(f"return_basis must be {RETURN_BASIS}")
        if self.label_start_date >= self.label_end_date:
            raise EvaluationError("label_start_date must precede label_end_date")
        _finite(self.prediction, "prediction")


@dataclass(frozen=True, slots=True)
class RankICResult:
    split: str
    decision_date: date
    model: str
    rank_ic: float
    observations: int


@dataclass(frozen=True, slots=True)
class TopDecileResult:
    split: str
    decision_date: date
    model: str
    selected_count: int
    selected_instrument_ids: tuple[str, ...]
    selected_weights: Mapping[str, float]
    gross_absolute_return: float
    gross_active_return: float
    top_minus_bottom_active_return: float
    net_absolute_return: float | None = None
    net_active_return: float | None = None
    cost_status: str = "blocked_requires_portfolio_cost_ledger"

    def __post_init__(self) -> None:
        object.__setattr__(self, "selected_instrument_ids", tuple(self.selected_instrument_ids))
        object.__setattr__(self, "selected_weights", MappingProxyType(dict(self.selected_weights)))
        if self.net_absolute_return is not None or self.net_active_return is not None:
            raise EvaluationError("evaluation cannot populate cost-adjusted returns")


@dataclass(frozen=True, slots=True)
class HalfYearResult:
    split: str
    model: str
    half_year: str
    decision_count: int
    mean_gross_absolute_return: float
    mean_gross_active_return: float
    mean_top_minus_bottom_active_return: float
    cost_status: str = "blocked_requires_portfolio_cost_ledger"


@dataclass(frozen=True, slots=True)
class HistoricalGateSummary:
    primary_model: str
    oos_mean_rank_ic: float | None
    oos_rank_ic_positive_fraction: float | None
    locked_holm_direction_correct_factor_count: int
    locked_positive_gross_active_half_years: int
    locked_half_years_observed: int
    cost_gate_pass: bool
    cost_gate_status: str
    historical_gate_pass: bool


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    prepared_panel: PreparedPanel
    factor_tests: tuple[FactorTestResult, ...]
    predictions: tuple[OOSPrediction, ...]
    rank_ic: tuple[RankICResult, ...]
    top_decile: tuple[TopDecileResult, ...]
    half_year: tuple[HalfYearResult, ...]
    historical_gate: HistoricalGateSummary
    experiment_id: str
    experiment_spec_sha256: str
    source_bundle_sha256: str
    ridge_alpha: float = RIDGE_ALPHA
    _formal_evaluation_token: InitVar[object] = None

    def __post_init__(self, _formal_evaluation_token: object) -> None:
        if _formal_evaluation_token is not _FORMAL_EVALUATION_TOKEN:
            raise EvaluationError(
                "EvaluationResult can only be created by formal evaluate_pit_panel"
            )
        if not isinstance(self.prepared_panel, PreparedPanel):
            raise EvaluationError("prepared_panel must be a PreparedPanel")
        if self.experiment_id != self.prepared_panel.experiment_id:
            raise EvaluationError("evaluation experiment_id does not match prepared panel")
        if self.experiment_spec_sha256 != self.prepared_panel.experiment_spec_sha256:
            raise EvaluationError(
                "evaluation experiment_spec_sha256 does not match prepared panel"
            )
        if self.source_bundle_sha256 != self.prepared_panel.source_bundle_sha256:
            raise EvaluationError(
                "evaluation source_bundle_sha256 does not match prepared panel"
            )
        _sha256(self.experiment_spec_sha256, "experiment_spec_sha256")
        _sha256(self.source_bundle_sha256, "source_bundle_sha256")
        if self.ridge_alpha != RIDGE_ALPHA:
            raise EvaluationError("evaluation ridge_alpha must remain 1")


def _factor_value(snapshot: QualityGrowthSnapshot, factor_id: str) -> float | None:
    for item in snapshot.factors:
        if item.factor_id == factor_id:
            if item.availability is FactorAvailability.AVAILABLE:
                return item.value
            return None
    raise EvaluationError(f"snapshot missing factor: {factor_id}")


def _preprocess_cross_section(section: PitCrossSection, split: str) -> PreparedCrossSection:
    scores_by_instrument: dict[str, dict[str, float]] = {
        item.instrument_id: {} for item in section.observations
    }
    failures: dict[str, str] = {}
    ordered_observations = tuple(sorted(section.observations, key=lambda item: item.instrument_id))
    for factor_id in QUALITY_GROWTH_FACTOR_IDS:
        valid: list[PitObservation] = []
        raw_values: list[float] = []
        for item in ordered_observations:
            if item.snapshot.industry_is_financial and factor_id not in FINANCIAL_FACTOR_IDS:
                continue
            value = _factor_value(item.snapshot, factor_id)
            if value is None:
                continue
            valid.append(item)
            raw_values.append(value)
        industries = sorted({item.industry_id for item in valid})
        dummy_industries = industries[1:]
        parameter_count = 1 + len(CONTROL_NAMES) + len(dummy_industries)
        if len(valid) <= parameter_count:
            failures[factor_id] = "insufficient_residual_degrees_of_freedom"
            continue
        controls = []
        for item in valid:
            row = [item.log_float_cap, item.earnings_yield, item.rm120, item.vol60]
            row.extend(1.0 if item.industry_id == industry else 0.0 for industry in dummy_industries)
            controls.append(row)
        names = (*CONTROL_NAMES, *(f"industry:{item}" for item in dummy_industries))
        try:
            clipped = winsorize_cross_section(raw_values, lower_quantile=0.01, upper_quantile=0.99)
            residual = residualize_cross_section(
                clipped,
                controls,
                exposure_names=names,
                fit_intercept=True,
            )
            standardized = zscore_cross_section(residual.residuals)
        except (PreprocessingError, RegressionError) as exc:
            failures[factor_id] = f"preprocessing_failed:{exc}"
            continue
        for item, score in zip(valid, standardized.tolist(), strict=True):
            scores_by_instrument[item.instrument_id][factor_id] = float(score)

    prepared = tuple(
        PreparedObservation(
            instrument_id=item.instrument_id,
            industry_is_financial=item.snapshot.industry_is_financial,
            factor_scores=scores_by_instrument[item.instrument_id],
            forward_total_return_20d=item.forward_total_return_20d,
            benchmark_total_return_20d=item.benchmark_total_return_20d,
            forward_excess_return_20d=item.forward_excess_return_20d,
            outcome_available_at=item.outcome_available_at,
        )
        for item in ordered_observations
    )
    return PreparedCrossSection(
        decision_at=section.decision_at,
        split=split,
        label_start_date=ordered_observations[0].label_start_date,
        label_end_date=ordered_observations[0].label_end_date,
        return_basis=ordered_observations[0].return_basis,
        observations=prepared,
        factor_failures=failures,
    )


def _experiment_split_ranges(
    experiment_content: Mapping[str, object],
) -> Mapping[str, tuple[date, date]]:
    splits = experiment_content["splits"]
    assert isinstance(splits, Mapping)  # validated by ExperimentSpecV2
    result: dict[str, tuple[date, date]] = {}
    for evaluation_name, contract_name in (
        ("train", "train"),
        ("validation", "validation"),
        ("locked_test", "locked_test"),
        ("audit", "second_audit"),
    ):
        interval = splits[contract_name]
        assert isinstance(interval, Mapping)
        result[evaluation_name] = (
            date.fromisoformat(str(interval["start_date"])),
            date.fromisoformat(str(interval["end_date"])),
        )
    return MappingProxyType(result)


def _validate_formal_source_bundle(
    materialized: tuple[PitCrossSection, ...],
    *,
    experiment: ExperimentSpecV2,
    trading_calendar: Sequence[date],
    benchmark_total_return_series: Sequence[BenchmarkTotalReturnPoint],
) -> tuple[
    tuple[date, ...],
    Mapping[date, float],
    Mapping[str, tuple[date, date]],
    date,
    str,
]:
    if not isinstance(experiment, ExperimentSpecV2):
        raise EvaluationError("formal evaluation requires ExperimentSpecV2")
    if not materialized:
        raise EvaluationError("formal evaluation requires a complete decision panel")
    if any(not isinstance(item, PitCrossSection) for item in materialized):
        raise EvaluationError("cross_sections must contain PitCrossSection objects")

    content = experiment.to_content_dict()
    universe = content["universe"]
    benchmark = content["benchmark"]
    target = content["target"]
    splits = content["splits"]
    hashes = content["hashes"]
    assert isinstance(universe, Mapping)
    assert isinstance(benchmark, Mapping)
    assert isinstance(target, Mapping)
    assert isinstance(splits, Mapping)
    assert isinstance(hashes, Mapping)

    calendar = tuple(trading_calendar)
    calendar_hash = trading_calendar_content_sha256(calendar)
    if calendar_hash != target["trading_calendar_content_sha256"]:
        raise EvaluationError("controlled trading calendar hash does not match experiment")
    calendar_index = {day: index for index, day in enumerate(calendar)}

    anchor = date.fromisoformat(str(target["rebalance_anchor_date"]))
    first_train_day = date.fromisoformat(str(splits["train"]["start_date"]))
    try:
        canonical_anchor = next(day for day in calendar if day >= first_train_day)
    except StopIteration as exc:
        raise EvaluationError("controlled calendar does not cover the train start") from exc
    if anchor != canonical_anchor:
        raise EvaluationError(
            "rebalance anchor is not the first controlled session on or after 2018-01-01"
        )
    cutoff = date.fromisoformat(str(splits["preregistration_cutoff"]))
    try:
        anchor_index = calendar_index[anchor]
        cutoff_index = calendar_index[cutoff]
    except KeyError as exc:
        raise EvaluationError("anchor and cutoff must be controlled trading sessions") from exc

    # A decision only enters the historical panel when its full next-open to
    # 20-session-open outcome is observable by the frozen cutoff.  This yields
    # one unambiguous, gap-free grid and prevents a caller from omitting a bad
    # month while retaining later months.
    expected_dates = tuple(
        calendar[index]
        for index in range(anchor_index, cutoff_index + 1, HORIZON_SESSIONS)
        if index + 1 + HORIZON_SESSIONS <= cutoff_index
    )
    actual_dates = tuple(item.decision_at.date() for item in materialized)
    if actual_dates != expected_dates:
        missing = sorted(set(expected_dates) - set(actual_dates))
        unexpected = sorted(set(actual_dates) - set(expected_dates))
        raise EvaluationError(
            "decision panel must exactly match the anchor-based 20-session grid; "
            f"missing={missing}, unexpected={unexpected}"
        )

    membership_hash = membership_panel_content_sha256(materialized)
    if membership_hash != universe["membership_panel_content_sha256"]:
        raise EvaluationError("membership panel content hash does not match experiment")

    points = tuple(benchmark_total_return_series)
    benchmark_hash = benchmark_total_return_series_content_sha256(points)
    if benchmark_hash != benchmark["total_return_series_content_sha256"]:
        raise EvaluationError(
            "benchmark total-return series hash does not match experiment"
        )
    point_dates = tuple(item.session_date for item in points)
    if point_dates != calendar:
        raise EvaluationError(
            "benchmark total-return series must cover the exact controlled calendar"
        )
    benchmark_levels: Mapping[date, float] = MappingProxyType(
        {item.session_date: item.open_level for item in points}
    )

    bindings = {item.source_binding for item in materialized}
    if len(bindings) != 1:
        raise EvaluationError("all cross-sections must use one controlled source binding")
    binding = next(iter(bindings))
    exact_binding = {
        "experiment_spec_sha256": experiment.spec_sha256,
        "membership_panel_receipt_sha256": universe[
            "membership_panel_receipt_sha256"
        ],
        "membership_panel_content_sha256": membership_hash,
        "benchmark_instrument_id": benchmark["instrument_id"],
        "benchmark_instrument_source_receipt_sha256": benchmark[
            "instrument_id_source_receipt_sha256"
        ],
        "benchmark_total_return_series_content_sha256": benchmark_hash,
    }
    for field, expected in exact_binding.items():
        if getattr(binding, field) != expected:
            raise EvaluationError(f"source binding {field} does not match experiment")
    frozen_receipts = set(hashes["data_receipt_sha256"])
    for field in (
        "financial_data_receipt_sha256",
        "industry_data_receipt_sha256",
        "control_data_receipt_sha256",
    ):
        if getattr(binding, field) not in frozen_receipts:
            raise EvaluationError(f"source binding {field} was not preregistered")

    bundle_hash = canonical_sha256(
        {
            "experiment_spec_sha256": experiment.spec_sha256,
            "trading_calendar_content_sha256": calendar_hash,
            "membership_panel_content_sha256": membership_hash,
            "benchmark_total_return_series_content_sha256": benchmark_hash,
            "source_binding": binding,
            "cross_sections": materialized,
        }
    )
    return (
        calendar,
        benchmark_levels,
        _experiment_split_ranges(content),
        cutoff,
        bundle_hash,
    )


def prepare_pit_cross_sections(
    cross_sections: Iterable[PitCrossSection],
    *,
    experiment: ExperimentSpecV2,
    trading_calendar: Sequence[date],
    benchmark_total_return_series: Sequence[BenchmarkTotalReturnPoint],
) -> PreparedPanel:
    """Validate a complete experiment-bound PIT panel and preprocess it.

    This is the formal entry point.  It intentionally has no free
    ``preregistration_cutoff`` or caller readiness flag: anchor, cutoff,
    membership, benchmark and source receipts all come from the immutable
    ``ExperimentSpecV2``.
    """

    materialized = tuple(sorted(cross_sections, key=lambda item: item.decision_at))
    (
        calendar,
        benchmark_levels,
        split_ranges,
        cutoff,
        bundle_hash,
    ) = _validate_formal_source_bundle(
        materialized,
        experiment=experiment,
        trading_calendar=trading_calendar,
        benchmark_total_return_series=benchmark_total_return_series,
    )
    calendar_index = {day: index for index, day in enumerate(calendar)}

    prepared: list[PreparedCrossSection] = []
    exclusions: list[PanelExclusion] = []
    for section in materialized:
        decision_date = section.decision_at.date()
        if decision_date > cutoff:
            raise EvaluationError("decision_at exceeds the frozen preregistration cutoff")
        split = _split(decision_date, split_ranges)
        if split is None:
            raise EvaluationError(f"decision date is outside the frozen split policy: {decision_date}")
        index = calendar_index[decision_date]
        label_start_index = index + 1
        label_end_index = label_start_index + HORIZON_SESSIONS
        expected_label_start = calendar[label_start_index]
        expected_label_end = calendar[label_end_index]
        supplied_label_starts = {item.label_start_date for item in section.observations}
        supplied_label_dates = {item.label_end_date for item in section.observations}
        supplied_return_bases = {item.return_basis for item in section.observations}
        if supplied_label_starts != {expected_label_start}:
            raise EvaluationError("label_start_date must be the next controlled session")
        if supplied_label_dates != {expected_label_end}:
            raise EvaluationError(
                "label_end_date must be 20 controlled sessions after label_start_date"
            )
        if supplied_return_bases != {RETURN_BASIS}:
            raise EvaluationError(f"return_basis must be {RETURN_BASIS}")
        expected_benchmark_return = (
            benchmark_levels[expected_label_end]
            / benchmark_levels[expected_label_start]
            - 1.0
        )
        supplied_benchmark_returns = {
            item.benchmark_total_return_20d
            for item in section.observations
            if item.benchmark_total_return_20d is not None
        }
        if supplied_benchmark_returns and (
            len(supplied_benchmark_returns) != 1
            or not math.isclose(
                float(next(iter(supplied_benchmark_returns))),
                expected_benchmark_return,
                rel_tol=0.0,
                abs_tol=1.0e-12,
            )
        ):
            raise EvaluationError(
                "benchmark forward return does not match the controlled total-return series"
            )
        if _split(expected_label_end, split_ranges) != split:
            exclusions.append(PanelExclusion(decision_date, "boundary_purge_20_sessions"))
            continue
        processed = _preprocess_cross_section(section, split)
        for item in processed.observations:
            applicable = (
                FINANCIAL_FACTOR_IDS
                if item.industry_is_financial
                else QUALITY_GROWTH_FACTOR_IDS
            )
            if any(factor_id not in item.factor_scores for factor_id in applicable):
                raise EvaluationError(
                    "incomplete applicable factor coverage; successful-subset evaluation is forbidden"
                )
        prepared.append(processed)
    return PreparedPanel(
        tuple(prepared),
        tuple(exclusions),
        experiment.experiment_id,
        experiment.spec_sha256,
        bundle_hash,
    )


def _normal_two_sided_p(t_statistic: float) -> float:
    return math.erfc(abs(t_statistic) / math.sqrt(2.0))


def _holm_adjust(raw: Mapping[str, float]) -> dict[str, float]:
    ordered = sorted(raw.items(), key=lambda item: (item[1], item[0]))
    count = len(ordered)
    adjusted: dict[str, float] = {}
    running = 0.0
    for index, (factor_id, p_value) in enumerate(ordered):
        running = max(running, min(1.0, (count - index) * p_value))
        adjusted[factor_id] = running
    return adjusted


def _factor_tests(panel: PreparedPanel, as_of: datetime) -> tuple[FactorTestResult, ...]:
    tests: list[FactorTestResult] = []
    for split in SPLIT_RANGES:
        provisional: list[FactorTestResult] = []
        raw_p: dict[str, float] = {}
        for factor_id in QUALITY_GROWTH_FACTOR_IDS:
            exposures: list[float] = []
            outcomes: list[float] = []
            periods: list[date] = []
            eligible_periods = 0
            for section in panel.cross_sections:
                if section.split != split:
                    continue
                rows = [
                    item
                    for item in section.observations
                    if factor_id in item.factor_scores
                    and item.forward_excess_return_20d is not None
                    and item.outcome_available_at is not None
                    and item.outcome_available_at <= as_of
                ]
                if len(rows) <= 2:
                    continue
                eligible_periods += 1
                for item in rows:
                    exposures.append(item.factor_scores[factor_id])
                    outcomes.append(float(item.forward_excess_return_20d))
                    periods.append(section.decision_at.date())
            if eligible_periods < 2:
                provisional.append(
                    FactorTestResult(
                        split, factor_id, "insufficient", None, None, None, None,
                        eligible_periods, len(outcomes), "fama_macbeth_requires_two_valid_periods",
                    )
                )
                continue
            try:
                result = fama_macbeth(
                    exposures,
                    outcomes,
                    periods,
                    feature_names=(factor_id,),
                )
            except RegressionError as exc:
                provisional.append(
                    FactorTestResult(
                        split, factor_id, "insufficient", None, None, None, None,
                        eligible_periods, len(outcomes), str(exc),
                    )
                )
                continue
            coefficient = float(result.coefficients[0])
            t_statistic = float(result.t_statistics[0])
            if not math.isfinite(coefficient) or not math.isfinite(t_statistic):
                provisional.append(
                    FactorTestResult(
                        split,
                        factor_id,
                        "insufficient",
                        None,
                        None,
                        None,
                        None,
                        eligible_periods,
                        len(outcomes),
                        "non_finite_fama_macbeth_statistic",
                    )
                )
                continue
            p_value = _normal_two_sided_p(t_statistic)
            if not math.isfinite(p_value):
                provisional.append(
                    FactorTestResult(
                        split,
                        factor_id,
                        "insufficient",
                        None,
                        None,
                        None,
                        None,
                        eligible_periods,
                        len(outcomes),
                        "non_finite_fama_macbeth_p_value",
                    )
                )
                continue
            raw_p[factor_id] = p_value
            provisional.append(
                FactorTestResult(
                    split, factor_id, "estimated", coefficient, t_statistic,
                    p_value, None, eligible_periods, len(outcomes), None,
                )
            )
        adjusted = _holm_adjust(raw_p)
        tests.extend(
            FactorTestResult(
                item.split,
                item.factor_id,
                item.status,
                item.coefficient,
                item.t_statistic,
                item.raw_p_value,
                adjusted.get(item.factor_id),
                item.periods,
                item.observations,
                item.reason,
            )
            for item in provisional
        )
    return tuple(tests)


def _features(financial: bool) -> tuple[str, ...]:
    return FINANCIAL_FACTOR_IDS if financial else QUALITY_GROWTH_FACTOR_IDS


def _complete_vector(item: PreparedObservation) -> tuple[float, ...] | None:
    factor_ids = _features(item.industry_is_financial)
    if any(factor_id not in item.factor_scores for factor_id in factor_ids):
        return None
    return tuple(item.factor_scores[factor_id] for factor_id in factor_ids)


def _oos_predictions(panel: PreparedPanel, as_of: datetime) -> tuple[OOSPrediction, ...]:
    predictions: list[OOSPrediction] = []
    sections = panel.cross_sections
    for current in sections:
        if current.split == "train":
            continue
        for financial in (False, True):
            class_observations = [
                item
                for item in current.observations
                if item.industry_is_financial is financial
            ]
            current_rows = [
                (item, _complete_vector(item))
                for item in class_observations
            ]
            if not class_observations:
                continue
            if any(vector is None for _, vector in current_rows):
                raise EvaluationError(
                    "formal OOS prediction lost an applicable factor vector"
                )
            train_x: list[tuple[float, ...]] = []
            train_y: list[float] = []
            for prior in sections:
                if prior.decision_at >= current.decision_at:
                    break
                # V1 freezes the predictive fit to the declared 2018-2022
                # training split.  Validation, locked-test and audit labels
                # may be scored once mature, but must never flow back into a
                # later fit and quietly turn the locked test into an expanding
                # in-sample exercise.
                if prior.split != "train":
                    continue
                for item in prior.observations:
                    if item.industry_is_financial is not financial:
                        continue
                    if (
                        item.forward_excess_return_20d is None
                        or item.outcome_available_at is None
                        or item.outcome_available_at > current.decision_at
                    ):
                        continue
                    vector = _complete_vector(item)
                    if vector is None:
                        continue
                    train_x.append(vector)
                    train_y.append(float(item.forward_excess_return_20d))
            feature_count = len(_features(financial))
            if len(train_y) <= feature_count + 1:
                raise EvaluationError(
                    "formal OOS submodel has insufficient frozen training observations"
                )
            try:
                model = fit_ridge(train_x, train_y, alpha=RIDGE_ALPHA)
            except RegressionError as exc:
                raise EvaluationError(
                    "formal OOS Ridge submodel failed; successful-subset scoring is forbidden"
                ) from exc
            matrix = np.asarray([vector for _, vector in current_rows], dtype=np.float64)
            ridge_values = model.predict(matrix)
            for (item, vector), ridge_value in zip(current_rows, ridge_values.tolist(), strict=True):
                actual_excess = (
                    item.forward_excess_return_20d
                    if item.outcome_available_at is not None
                    and item.outcome_available_at <= as_of
                    else None
                )
                actual_total = (
                    item.forward_total_return_20d if actual_excess is not None else None
                )
                actual_benchmark = (
                    item.benchmark_total_return_20d if actual_excess is not None else None
                )
                common = dict(
                    split=current.split,
                    decision_date=current.decision_at.date(),
                    label_start_date=current.label_start_date,
                    label_end_date=current.label_end_date,
                    return_basis=current.return_basis,
                    instrument_id=item.instrument_id,
                    actual_forward_total_return_20d=actual_total,
                    benchmark_total_return_20d=actual_benchmark,
                    actual_forward_excess_return_20d=actual_excess,
                    outcome_available_at=(
                        item.outcome_available_at if actual_excess is not None else None
                    ),
                )
                predictions.append(
                    OOSPrediction(model="ridge_alpha_1", prediction=float(ridge_value), **common)
                )
                predictions.append(
                    OOSPrediction(
                        model="direction_equal_weight",
                        prediction=float(np.mean(vector)),
                        **common,
                    )
                )
    return tuple(predictions)


def _prediction_diagnostics(
    predictions: tuple[OOSPrediction, ...]
) -> tuple[tuple[RankICResult, ...], tuple[TopDecileResult, ...], tuple[HalfYearResult, ...]]:
    grouped: dict[tuple[str, date, str], list[OOSPrediction]] = defaultdict(list)
    for item in predictions:
        if item.actual_forward_excess_return_20d is not None:
            grouped[(item.split, item.decision_date, item.model)].append(item)
    rank_ic: list[RankICResult] = []
    top_decile: list[TopDecileResult] = []
    for (split, decision_date, model), rows in sorted(grouped.items()):
        if len(rows) < 3:
            continue
        predicted = np.asarray([item.prediction for item in rows], dtype=np.float64)
        actual_excess = np.asarray(
            [float(item.actual_forward_excess_return_20d) for item in rows],
            dtype=np.float64,
        )
        actual_total = np.asarray(
            [float(item.actual_forward_total_return_20d) for item in rows],
            dtype=np.float64,
        )
        benchmark = np.asarray(
            [float(item.benchmark_total_return_20d) for item in rows],
            dtype=np.float64,
        )
        predicted_rank = rank_cross_section(predicted, percentile=True)
        actual_rank = rank_cross_section(actual_excess, percentile=True)
        if float(np.std(predicted_rank)) > 0.0 and float(np.std(actual_rank)) > 0.0:
            coefficient = float(np.corrcoef(predicted_rank, actual_rank)[0, 1])
            rank_ic.append(RankICResult(split, decision_date, model, coefficient, len(rows)))
        selected_count = max(1, math.ceil(len(rows) * 0.10))
        order = np.argsort(predicted, kind="stable")
        selected_indices = order[-selected_count:]
        selected_rows = [rows[int(index)] for index in selected_indices]
        top_absolute = float(np.mean(actual_total[selected_indices]))
        top_active = float(np.mean(actual_excess[selected_indices]))
        bottom_active = float(np.mean(actual_excess[order[:selected_count]]))
        selected_ids = tuple(sorted(item.instrument_id for item in selected_rows))
        equal_weight = 1.0 / selected_count
        top_decile.append(
            TopDecileResult(
                split,
                decision_date,
                model,
                selected_count,
                selected_ids,
                {instrument_id: equal_weight for instrument_id in selected_ids},
                top_absolute,
                top_active,
                top_active - bottom_active,
            )
        )

    half_groups: dict[tuple[str, str, str], list[TopDecileResult]] = defaultdict(list)
    for item in top_decile:
        half = 1 if item.decision_date.month <= 6 else 2
        label = f"{item.decision_date.year}-H{half}"
        half_groups[(item.split, item.model, label)].append(item)
    half_year = tuple(
        HalfYearResult(
            split=key[0],
            model=key[1],
            half_year=key[2],
            decision_count=len(rows),
            mean_gross_absolute_return=float(
                np.mean([item.gross_absolute_return for item in rows])
            ),
            mean_gross_active_return=float(
                np.mean([item.gross_active_return for item in rows])
            ),
            mean_top_minus_bottom_active_return=float(
                np.mean([item.top_minus_bottom_active_return for item in rows])
            ),
        )
        for key, rows in sorted(half_groups.items())
    )
    return tuple(rank_ic), tuple(top_decile), half_year


def _historical_gate_summary(
    factor_tests: tuple[FactorTestResult, ...],
    rank_ic: tuple[RankICResult, ...],
    half_year: tuple[HalfYearResult, ...],
) -> HistoricalGateSummary:
    primary_model = "ridge_alpha_1"
    oos_rank = [
        item.rank_ic
        for item in rank_ic
        if item.model == primary_model
        and item.split in set(STATISTICAL_CONTRACT["rank_ic_evaluation_splits"])
    ]
    significance_splits = set(STATISTICAL_CONTRACT["factor_significance_splits"])
    significant_by_factor: dict[str, set[str]] = defaultdict(set)
    for item in factor_tests:
        if (
            item.split in significance_splits
            and item.status == "estimated"
            and item.coefficient is not None
            and item.coefficient > 0.0
            and item.holm_p_value is not None
            and item.holm_p_value
            <= float(STATISTICAL_CONTRACT["familywise_alpha"])
        ):
            significant_by_factor[item.factor_id].add(item.split)
    # The legacy field name is retained for API compatibility, but the value is
    # now the conservative frozen contract: a factor must pass in both the
    # locked test and the independent audit, not merely in the locked test.
    locked_positive_factors = sum(
        observed_splits == significance_splits
        for observed_splits in significant_by_factor.values()
    )
    locked_half_year = [
        item
        for item in half_year
        if item.split == "locked_test" and item.model == primary_model
    ]
    return HistoricalGateSummary(
        primary_model=primary_model,
        oos_mean_rank_ic=(float(np.mean(oos_rank)) if oos_rank else None),
        oos_rank_ic_positive_fraction=(
            sum(value > 0.0 for value in oos_rank) / len(oos_rank)
            if oos_rank
            else None
        ),
        locked_holm_direction_correct_factor_count=locked_positive_factors,
        locked_positive_gross_active_half_years=sum(
            item.mean_gross_active_return > 0.0 for item in locked_half_year
        ),
        locked_half_years_observed=len(locked_half_year),
        cost_gate_pass=False,
        cost_gate_status="blocked_requires_portfolio_cost_ledger",
        historical_gate_pass=False,
    )


def evaluate_pit_panel(
    cross_sections: Iterable[PitCrossSection],
    *,
    experiment: ExperimentSpecV2,
    trading_calendar: Sequence[date],
    benchmark_total_return_series: Sequence[BenchmarkTotalReturnPoint],
    as_of: datetime,
) -> EvaluationResult:
    """Run the frozen, experiment-bound FMB/HAC and train-only Ridge evaluation."""

    evaluation_time = _aware(as_of, "as_of")
    if not isinstance(experiment, ExperimentSpecV2):
        raise EvaluationError("formal evaluation requires ExperimentSpecV2")
    content = experiment.to_content_dict()
    cutoff_date = date.fromisoformat(content["splits"]["preregistration_cutoff"])
    if evaluation_time.date() < cutoff_date:
        raise EvaluationError("evaluation as_of cannot precede the frozen historical cutoff")
    materialized = tuple(cross_sections)
    if any(
        isinstance(item, PitCrossSection) and item.decision_at > evaluation_time
        for item in materialized
    ):
        raise EvaluationError("cross-section decision_at cannot follow evaluation as_of")
    panel = prepare_pit_cross_sections(
        materialized,
        experiment=experiment,
        trading_calendar=trading_calendar,
        benchmark_total_return_series=benchmark_total_return_series,
    )
    tests = _factor_tests(panel, evaluation_time)
    predictions = _oos_predictions(panel, evaluation_time)
    for section in panel.cross_sections:
        if section.split == "train":
            continue
        expected_ids = {item.instrument_id for item in section.observations}
        decision_date = section.decision_at.date()
        for model_name in ("ridge_alpha_1", "direction_equal_weight"):
            observed_ids = {
                item.instrument_id
                for item in predictions
                if item.decision_date == decision_date
                and item.split == section.split
                and item.model == model_name
            }
            if observed_ids != expected_ids:
                raise EvaluationError(
                    "formal OOS predictions must exactly cover every prepared PIT member"
                )
    rank_ic, top_decile, half_year = _prediction_diagnostics(predictions)
    historical_gate = _historical_gate_summary(tests, rank_ic, half_year)
    return EvaluationResult(
        prepared_panel=panel,
        factor_tests=tests,
        predictions=predictions,
        rank_ic=rank_ic,
        top_decile=top_decile,
        half_year=half_year,
        historical_gate=historical_gate,
        experiment_id=experiment.experiment_id,
        experiment_spec_sha256=experiment.spec_sha256,
        source_bundle_sha256=panel.source_bundle_sha256,
        _formal_evaluation_token=_FORMAL_EVALUATION_TOKEN,
    )


def _evaluation_hashable(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _evaluation_hashable(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, Mapping):
        return {
            str(key): _evaluation_hashable(item)
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [_evaluation_hashable(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise EvaluationError("formal evaluation result contains a non-finite float")
    return value


def evaluation_result_content_sha256(result: EvaluationResult) -> str:
    """Return the canonical content hash of one formally bound result."""

    if not isinstance(result, EvaluationResult):
        raise EvaluationError("result must be an EvaluationResult")
    return canonical_sha256(_evaluation_hashable(result))


__all__ = [
    "BenchmarkTotalReturnPoint",
    "CONTROL_NAMES",
    "ControlledSourceBinding",
    "HORIZON_SESSIONS",
    "RIDGE_ALPHA",
    "SPLIT_RANGES",
    "EvaluationError",
    "EvaluationResult",
    "FactorTestResult",
    "HalfYearResult",
    "HistoricalGateSummary",
    "OOSPrediction",
    "PanelExclusion",
    "PitCrossSection",
    "PitObservation",
    "PreparedCrossSection",
    "PreparedObservation",
    "PreparedPanel",
    "RankICResult",
    "TopDecileResult",
    "benchmark_total_return_series_content_sha256",
    "evaluate_pit_panel",
    "evaluation_result_content_sha256",
    "membership_panel_content_sha256",
    "prepare_pit_cross_sections",
    "trading_calendar_content_sha256",
]
