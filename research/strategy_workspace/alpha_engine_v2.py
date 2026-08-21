"""Deterministic point-in-time alpha production for Adaptive Exposure V2.

This module is deliberately research-only.  It consumes a typed, self-hashed
PIT snapshot, recomputes the six frozen quality/growth factors and the six
pre-registered close-price timing factors, and applies a train-only frozen
linear model.  It never creates orders or changes an account.

Two failure levels are intentionally different:

* future/common-source contamination fails the complete cross-section closed;
* an instrument with missing PIT fields or factors remains in the output with
  complete exclusion codes and no prediction.

Missing values are never converted to zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
import math
import re
from statistics import stdev
from typing import Any, Iterable, Mapping, Sequence

from .contracts import canonical_sha256
from .quality_growth import (
    FINANCIAL_FACTOR_IDS,
    QUALITY_GROWTH_FACTOR_IDS,
    FactorAvailability,
    QualityGrowthError,
    QuarterlyFundamental,
    compute_quality_growth_snapshot,
)


CONTROLLED_PIT_SNAPSHOT_SCHEMA_VERSION = "controlled-pit-decision-snapshot.v1"
FROZEN_ALPHA_MODEL_SCHEMA_VERSION = "frozen-alpha-model.v1"
ALPHA_RANKING_SCHEMA_VERSION = "alpha-ranking.v2"

# This is the pre-registered six-factor timing family already used by the
# strategy workspace diagnostic path.  The production engine recomputes the
# formulas independently and does not accept caller-supplied factor values.
FAST_FACTOR_IDS: tuple[str, ...] = (
    "RM20",
    "RM60",
    "RM120",
    "TREND_EFF60",
    "DOWNSIDE_VOL60",
    "BREAKOUT60",
)
NON_FINANCIAL_FEATURE_IDS = QUALITY_GROWTH_FACTOR_IDS + FAST_FACTOR_IDS
FINANCIAL_FEATURE_IDS = FINANCIAL_FACTOR_IDS + FAST_FACTOR_IDS
MIN_PRICE_SESSIONS = 121
CHINA_STANDARD_TIME = timezone(timedelta(hours=8))

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_INSTRUMENT = re.compile(r"^[0-9]{6}\.(?:SH|SZ)$")


class AlphaEngineError(ValueError):
    """Raised when a typed alpha contract is internally inconsistent."""


class AlphaRunStatus(str, Enum):
    OK = "OK"
    NO_ALPHA_CASH = "NO_ALPHA_CASH"
    DATA_FAIL_CLOSED = "DATA_FAIL_CLOSED"


def _aware(value: datetime | None, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AlphaEngineError(f"{field_name} must be timezone-aware")
    return value


def _cst_session_date(value: datetime, field_name: str) -> date:
    """Return the A-share strategy session date for one decision instant."""

    return _aware(value, field_name).astimezone(CHINA_STANDARD_TIME).date()


def _optional_aware(value: datetime | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    return _aware(value, field_name)


def _sha(value: str | None, field_name: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise AlphaEngineError(f"{field_name} must be a lowercase SHA-256")
    return value


def _identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AlphaEngineError(f"{field_name} is required")
    text = value.strip()
    if any(character.isspace() for character in text):
        raise AlphaEngineError(f"{field_name} cannot contain whitespace")
    return text


def _instrument(value: Any, field_name: str = "instrument_id") -> str:
    text = _identifier(value, field_name).upper()
    if _INSTRUMENT.fullmatch(text) is None:
        raise AlphaEngineError(f"{field_name} must be an explicit SH/SZ A-share code")
    return text


def _finite(value: float | int, field_name: str) -> float:
    if isinstance(value, bool):
        raise AlphaEngineError(f"{field_name} must be finite")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise AlphaEngineError(f"{field_name} must be finite") from exc
    if not math.isfinite(number):
        raise AlphaEngineError(f"{field_name} must be finite")
    return number


def _ordered_codes(values: Iterable[str]) -> tuple[str, ...]:
    codes = tuple(sorted({_identifier(value, "exclusion code") for value in values}))
    return codes


@dataclass(frozen=True, slots=True)
class ControlledPriceBarV2:
    """One source-bound close bar visible to the decision process."""

    instrument_id: str
    session_date: date
    close: float
    high: float
    available_at: datetime
    source_record_id: str
    source_record_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument_id", _identifier(self.instrument_id, "instrument_id").upper())
        if not isinstance(self.session_date, date) or isinstance(self.session_date, datetime):
            raise AlphaEngineError("session_date must be a date")
        close = _finite(self.close, "close")
        high = _finite(self.high, "high")
        if close <= 0.0 or high <= 0.0 or high < close:
            raise AlphaEngineError("close/high must be positive and high must be >= close")
        object.__setattr__(self, "close", close)
        object.__setattr__(self, "high", high)
        _aware(self.available_at, "available_at")
        object.__setattr__(self, "source_record_id", _identifier(self.source_record_id, "source_record_id"))
        _sha(self.source_record_sha256, "source_record_sha256")

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "session_date": self.session_date.isoformat(),
            "close": self.close,
            "high": self.high,
            "available_at": self.available_at.isoformat(),
            "source_record_id": self.source_record_id,
            "source_record_sha256": self.source_record_sha256,
        }


@dataclass(frozen=True, slots=True)
class ControlledPitInstrumentV2:
    """All PIT inputs for one frozen-universe member.

    Optional PIT fields are retained as ``None`` so the engine can emit a full
    exclusion row instead of silently dropping the member.
    """

    instrument_id: str
    industry: str | None
    industry_is_financial: bool | None
    constituent_available_at: datetime | None
    industry_available_at: datetime | None
    fundamentals: tuple[QuarterlyFundamental, ...] = ()
    price_bars: tuple[ControlledPriceBarV2, ...] = ()

    def __post_init__(self) -> None:
        instrument_id = _instrument(self.instrument_id)
        object.__setattr__(self, "instrument_id", instrument_id)
        if self.industry is not None:
            industry = str(self.industry).strip()
            object.__setattr__(self, "industry", industry or None)
        if self.industry_is_financial is not None and type(self.industry_is_financial) is not bool:
            raise AlphaEngineError("industry_is_financial must be boolean or null")
        _optional_aware(self.constituent_available_at, "constituent_available_at")
        _optional_aware(self.industry_available_at, "industry_available_at")

        fundamentals = tuple(self.fundamentals)
        if any(not isinstance(item, QuarterlyFundamental) for item in fundamentals):
            raise AlphaEngineError("fundamentals must contain QuarterlyFundamental objects")
        if any(item.instrument_id != instrument_id for item in fundamentals):
            raise AlphaEngineError("fundamental instrument_id mismatch")
        fundamentals = tuple(
            sorted(
                fundamentals,
                key=lambda item: (
                    item.period_end,
                    item.first_disclosed_at,
                    item.revision_sequence,
                    item.source_record_id,
                ),
            )
        )
        bars = tuple(self.price_bars)
        if any(not isinstance(item, ControlledPriceBarV2) for item in bars):
            raise AlphaEngineError("price_bars must contain ControlledPriceBarV2 objects")
        if any(item.instrument_id != instrument_id for item in bars):
            raise AlphaEngineError("price-bar instrument_id mismatch")
        bars = tuple(sorted(bars, key=lambda item: (item.session_date, item.source_record_id)))
        object.__setattr__(self, "fundamentals", fundamentals)
        object.__setattr__(self, "price_bars", bars)

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "industry": self.industry,
            "industry_is_financial": self.industry_is_financial,
            "constituent_available_at": (
                None if self.constituent_available_at is None else self.constituent_available_at.isoformat()
            ),
            "industry_available_at": (
                None if self.industry_available_at is None else self.industry_available_at.isoformat()
            ),
            "fundamentals": [
                {
                    "instrument_id": item.instrument_id,
                    "period_end": item.period_end.isoformat(),
                    "first_disclosed_at": item.first_disclosed_at.isoformat(),
                    "source_record_id": item.source_record_id,
                    "source_record_sha256": item.source_record_sha256,
                    "revision_sequence": item.revision_sequence,
                    "flow_basis": item.flow_basis,
                    "statement_scope": item.statement_scope,
                    "currency": item.currency,
                    "roe": item.roe,
                    "net_profit": item.net_profit,
                    "operating_cash_flow": item.operating_cash_flow,
                    "operating_profit": item.operating_profit,
                    "gross_profit": item.gross_profit,
                    "total_assets": item.total_assets,
                    "total_liabilities": item.total_liabilities,
                    "revenue": item.revenue,
                }
                for item in self.fundamentals
            ],
            "price_bars": [item.to_dict() for item in self.price_bars],
        }


@dataclass(frozen=True, slots=True)
class ControlledPitSnapshotV2:
    """A complete frozen-universe decision snapshot with source receipts."""

    decision_at: datetime
    universe_as_of: date
    universe_available_at: datetime
    universe_version: str
    member_ids: tuple[str, ...]
    instruments: tuple[ControlledPitInstrumentV2, ...]
    trading_sessions: tuple[date, ...]
    benchmark_instrument_id: str
    benchmark_price_bars: tuple[ControlledPriceBarV2, ...]
    trading_calendar_receipt_sha256: str
    universe_receipt_sha256: str
    financial_data_receipt_sha256: str
    industry_data_receipt_sha256: str
    price_data_receipt_sha256: str
    schema_version: str = CONTROLLED_PIT_SNAPSHOT_SCHEMA_VERSION
    input_snapshot_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != CONTROLLED_PIT_SNAPSHOT_SCHEMA_VERSION:
            raise AlphaEngineError("unsupported controlled PIT snapshot schema")
        _aware(self.decision_at, "decision_at")
        if not isinstance(self.universe_as_of, date) or isinstance(self.universe_as_of, datetime):
            raise AlphaEngineError("universe_as_of must be a date")
        _aware(self.universe_available_at, "universe_available_at")
        object.__setattr__(self, "universe_version", _identifier(self.universe_version, "universe_version"))

        members = tuple(sorted(_instrument(item, "member_ids item") for item in self.member_ids))
        if not members or len(members) != len(set(members)):
            raise AlphaEngineError("member_ids must be non-empty and unique")
        instruments = tuple(self.instruments)
        if any(not isinstance(item, ControlledPitInstrumentV2) for item in instruments):
            raise AlphaEngineError("instruments must contain ControlledPitInstrumentV2 objects")
        ids = tuple(item.instrument_id for item in instruments)
        if len(ids) != len(set(ids)):
            raise AlphaEngineError("instrument inputs must be unique")
        if not set(ids).issubset(set(members)):
            raise AlphaEngineError("instrument inputs cannot contain non-members")
        instruments = tuple(sorted(instruments, key=lambda item: item.instrument_id))

        sessions = tuple(self.trading_sessions)
        if any(not isinstance(item, date) or isinstance(item, datetime) for item in sessions):
            raise AlphaEngineError("trading_sessions must contain dates")
        benchmark_id = _identifier(self.benchmark_instrument_id, "benchmark_instrument_id").upper()
        benchmark = tuple(self.benchmark_price_bars)
        if any(not isinstance(item, ControlledPriceBarV2) for item in benchmark):
            raise AlphaEngineError("benchmark_price_bars must contain ControlledPriceBarV2 objects")
        if any(item.instrument_id != benchmark_id for item in benchmark):
            raise AlphaEngineError("benchmark price-bar instrument_id mismatch")
        benchmark = tuple(sorted(benchmark, key=lambda item: (item.session_date, item.source_record_id)))
        for field_name in (
            "trading_calendar_receipt_sha256",
            "universe_receipt_sha256",
            "financial_data_receipt_sha256",
            "industry_data_receipt_sha256",
            "price_data_receipt_sha256",
        ):
            _sha(getattr(self, field_name), field_name)
        object.__setattr__(self, "member_ids", members)
        object.__setattr__(self, "instruments", instruments)
        object.__setattr__(self, "trading_sessions", sessions)
        object.__setattr__(self, "benchmark_instrument_id", benchmark_id)
        object.__setattr__(self, "benchmark_price_bars", benchmark)
        object.__setattr__(self, "input_snapshot_sha256", canonical_sha256(self.to_content_dict()))

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "decision_at": self.decision_at.isoformat(),
            "universe_as_of": self.universe_as_of.isoformat(),
            "universe_available_at": self.universe_available_at.isoformat(),
            "universe_version": self.universe_version,
            "member_ids": list(self.member_ids),
            "instruments": [item.to_dict() for item in self.instruments],
            "trading_sessions": [item.isoformat() for item in self.trading_sessions],
            "benchmark_instrument_id": self.benchmark_instrument_id,
            "benchmark_price_bars": [item.to_dict() for item in self.benchmark_price_bars],
            "trading_calendar_receipt_sha256": self.trading_calendar_receipt_sha256,
            "universe_receipt_sha256": self.universe_receipt_sha256,
            "financial_data_receipt_sha256": self.financial_data_receipt_sha256,
            "industry_data_receipt_sha256": self.industry_data_receipt_sha256,
            "price_data_receipt_sha256": self.price_data_receipt_sha256,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.to_content_dict()
        payload["input_snapshot_sha256"] = self.input_snapshot_sha256
        return payload


@dataclass(frozen=True, slots=True)
class FrozenLinearSubmodelV2:
    """Frozen feature transform and coefficients for one industry family."""

    submodel_id: str
    feature_ids: tuple[str, ...]
    intercept: float
    coefficients: tuple[float, ...]
    centers: tuple[float, ...]
    scales: tuple[float, ...]

    def __post_init__(self) -> None:
        submodel_id = _identifier(self.submodel_id, "submodel_id")
        if submodel_id not in {"financial", "non_financial"}:
            raise AlphaEngineError("submodel_id must be financial or non_financial")
        feature_ids = tuple(_identifier(item, "feature_ids item") for item in self.feature_ids)
        expected = FINANCIAL_FEATURE_IDS if submodel_id == "financial" else NON_FINANCIAL_FEATURE_IDS
        if feature_ids != expected:
            raise AlphaEngineError(f"{submodel_id} feature family is not the frozen ordered family")
        coefficients = tuple(_finite(item, "coefficient") for item in self.coefficients)
        centers = tuple(_finite(item, "center") for item in self.centers)
        scales = tuple(_finite(item, "scale") for item in self.scales)
        if not (len(coefficients) == len(centers) == len(scales) == len(feature_ids)):
            raise AlphaEngineError("coefficients, centers and scales must cover every feature")
        if any(item <= 0.0 for item in scales):
            raise AlphaEngineError("feature scales must be positive")
        object.__setattr__(self, "submodel_id", submodel_id)
        object.__setattr__(self, "feature_ids", feature_ids)
        object.__setattr__(self, "intercept", _finite(self.intercept, "intercept"))
        object.__setattr__(self, "coefficients", coefficients)
        object.__setattr__(self, "centers", centers)
        object.__setattr__(self, "scales", scales)

    @classmethod
    def from_mappings(
        cls,
        *,
        submodel_id: str,
        intercept: float,
        coefficients: Mapping[str, float],
        centers: Mapping[str, float],
        scales: Mapping[str, float],
    ) -> "FrozenLinearSubmodelV2":
        feature_ids = FINANCIAL_FEATURE_IDS if submodel_id == "financial" else NON_FINANCIAL_FEATURE_IDS
        for name, values in (("coefficients", coefficients), ("centers", centers), ("scales", scales)):
            if set(values) != set(feature_ids):
                raise AlphaEngineError(f"{name} must exactly cover the {submodel_id} features")
        return cls(
            submodel_id=submodel_id,
            feature_ids=feature_ids,
            intercept=intercept,
            coefficients=tuple(coefficients[item] for item in feature_ids),
            centers=tuple(centers[item] for item in feature_ids),
            scales=tuple(scales[item] for item in feature_ids),
        )

    def score(self, values: Mapping[str, float]) -> tuple[float, float, float]:
        if set(values) != set(self.feature_ids):
            raise AlphaEngineError("model score requires the exact feature family")
        quality_score = 0.0
        timing_score = 0.0
        for feature_id, coefficient, center, scale in zip(
            self.feature_ids, self.coefficients, self.centers, self.scales
        ):
            contribution = coefficient * (_finite(values[feature_id], feature_id) - center) / scale
            if feature_id in FAST_FACTOR_IDS:
                timing_score += contribution
            else:
                quality_score += contribution
        predicted = self.intercept + quality_score + timing_score
        if not all(math.isfinite(item) for item in (predicted, quality_score, timing_score)):
            raise AlphaEngineError("model produced a non-finite score")
        return predicted, quality_score, timing_score

    def to_dict(self) -> dict[str, Any]:
        return {
            "submodel_id": self.submodel_id,
            "feature_ids": list(self.feature_ids),
            "intercept": self.intercept,
            "coefficients": list(self.coefficients),
            "centers": list(self.centers),
            "scales": list(self.scales),
        }


@dataclass(frozen=True, slots=True)
class FrozenAlphaModelV2:
    """A train-only artifact whose identity is derived from its full payload."""

    model_id: str
    model_version: str
    training_window_start: date
    training_window_end: date
    training_data_cutoff_at: datetime
    trained_at: datetime
    frozen_at: datetime
    training_dataset_sha256: str
    training_code_sha256: str
    preprocessing_policy_sha256: str
    model_config_sha256: str
    financial_submodel: FrozenLinearSubmodelV2
    non_financial_submodel: FrozenLinearSubmodelV2
    artifact_status: str = "frozen_train_only"
    training_partition: str = "train_only"
    schema_version: str = FROZEN_ALPHA_MODEL_SCHEMA_VERSION
    model_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != FROZEN_ALPHA_MODEL_SCHEMA_VERSION:
            raise AlphaEngineError("unsupported frozen alpha model schema")
        if self.artifact_status != "frozen_train_only" or self.training_partition != "train_only":
            raise AlphaEngineError("alpha model must be a frozen train-only artifact")
        object.__setattr__(self, "model_id", _identifier(self.model_id, "model_id"))
        object.__setattr__(self, "model_version", _identifier(self.model_version, "model_version"))
        for field_name in ("training_window_start", "training_window_end"):
            value = getattr(self, field_name)
            if not isinstance(value, date) or isinstance(value, datetime):
                raise AlphaEngineError(f"{field_name} must be a date")
        if self.training_window_start > self.training_window_end:
            raise AlphaEngineError("training window is inverted")
        cutoff = _aware(self.training_data_cutoff_at, "training_data_cutoff_at")
        trained = _aware(self.trained_at, "trained_at")
        frozen = _aware(self.frozen_at, "frozen_at")
        if _cst_session_date(cutoff, "training_data_cutoff_at") < self.training_window_end:
            raise AlphaEngineError("training data cutoff precedes training window end")
        if cutoff > trained or trained > frozen:
            raise AlphaEngineError("training cutoff, trained_at and frozen_at are out of order")
        for field_name in (
            "training_dataset_sha256",
            "training_code_sha256",
            "preprocessing_policy_sha256",
            "model_config_sha256",
        ):
            _sha(getattr(self, field_name), field_name)
        if not isinstance(self.financial_submodel, FrozenLinearSubmodelV2) or self.financial_submodel.submodel_id != "financial":
            raise AlphaEngineError("financial_submodel is required")
        if not isinstance(self.non_financial_submodel, FrozenLinearSubmodelV2) or self.non_financial_submodel.submodel_id != "non_financial":
            raise AlphaEngineError("non_financial_submodel is required")
        object.__setattr__(self, "model_sha256", canonical_sha256(self.to_content_dict()))

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "artifact_status": self.artifact_status,
            "training_partition": self.training_partition,
            "training_window_start": self.training_window_start.isoformat(),
            "training_window_end": self.training_window_end.isoformat(),
            "training_data_cutoff_at": self.training_data_cutoff_at.isoformat(),
            "trained_at": self.trained_at.isoformat(),
            "frozen_at": self.frozen_at.isoformat(),
            "training_dataset_sha256": self.training_dataset_sha256,
            "training_code_sha256": self.training_code_sha256,
            "preprocessing_policy_sha256": self.preprocessing_policy_sha256,
            "model_config_sha256": self.model_config_sha256,
            "financial_submodel": self.financial_submodel.to_dict(),
            "non_financial_submodel": self.non_financial_submodel.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.to_content_dict()
        payload["model_sha256"] = self.model_sha256
        return payload


@dataclass(frozen=True, slots=True)
class AlphaPredictionRowV2:
    instrument_id: str
    decision_at: datetime
    predicted_return: float | None
    quality_score: float | None
    timing_score: float | None
    percentile: float | None
    rank: int | None
    industry: str | None
    eligibility: bool
    exclusion_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument_id", _instrument(self.instrument_id))
        _aware(self.decision_at, "decision_at")
        codes = _ordered_codes(self.exclusion_codes)
        object.__setattr__(self, "exclusion_codes", codes)
        if type(self.eligibility) is not bool:
            raise AlphaEngineError("eligibility must be boolean")
        values = (self.predicted_return, self.quality_score, self.timing_score, self.percentile)
        if self.eligibility:
            if codes or any(item is None for item in values):
                raise AlphaEngineError("eligible rows require scores and no exclusion codes")
            normalized = tuple(_finite(item, "eligible score") for item in values)  # type: ignore[arg-type]
            if not 0.0 <= normalized[3] <= 1.0:
                raise AlphaEngineError("percentile must be within [0, 1]")
            if type(self.rank) is not int or self.rank < 1:
                raise AlphaEngineError("eligible rows require a positive rank")
            object.__setattr__(self, "predicted_return", normalized[0])
            object.__setattr__(self, "quality_score", normalized[1])
            object.__setattr__(self, "timing_score", normalized[2])
            object.__setattr__(self, "percentile", normalized[3])
        elif not codes or any(item is not None for item in values) or self.rank is not None:
            raise AlphaEngineError("excluded rows require codes and null ranking fields")

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "decision_at": self.decision_at.isoformat(),
            "predicted_return": self.predicted_return,
            "quality_score": self.quality_score,
            "timing_score": self.timing_score,
            "percentile": self.percentile,
            "rank": self.rank,
            "industry": self.industry,
            "eligibility": self.eligibility,
            "exclusion_codes": list(self.exclusion_codes),
        }


@dataclass(frozen=True, slots=True)
class AlphaRankingV2:
    status: AlphaRunStatus
    decision_at: datetime
    rows: tuple[AlphaPredictionRowV2, ...]
    model_sha256: str
    input_snapshot_sha256: str
    schema_version: str = ALPHA_RANKING_SCHEMA_VERSION
    ranking_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != ALPHA_RANKING_SCHEMA_VERSION:
            raise AlphaEngineError("unsupported alpha ranking schema")
        try:
            status = self.status if isinstance(self.status, AlphaRunStatus) else AlphaRunStatus(self.status)
        except ValueError as exc:
            raise AlphaEngineError("unknown alpha status") from exc
        decision = _aware(self.decision_at, "decision_at")
        rows = tuple(self.rows)
        if any(not isinstance(item, AlphaPredictionRowV2) for item in rows):
            raise AlphaEngineError("rows must contain AlphaPredictionRowV2 objects")
        if not rows or len({item.instrument_id for item in rows}) != len(rows):
            raise AlphaEngineError("ranking must cover a non-empty unique universe")
        if any(item.decision_at != decision for item in rows):
            raise AlphaEngineError("row decision_at mismatch")
        eligible = [item for item in rows if item.eligibility]
        if status is AlphaRunStatus.OK and not eligible:
            raise AlphaEngineError("OK ranking requires eligible instruments")
        if status is not AlphaRunStatus.OK and eligible:
            raise AlphaEngineError("fail-closed/cash ranking cannot contain predictions")
        _sha(self.model_sha256, "model_sha256")
        _sha(self.input_snapshot_sha256, "input_snapshot_sha256")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "rows", rows)
        object.__setattr__(self, "ranking_sha256", canonical_sha256(self.to_content_dict()))

    @property
    def eligible_count(self) -> int:
        return sum(item.eligibility for item in self.rows)

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status.value,
            "decision_at": self.decision_at.isoformat(),
            "eligible_count": self.eligible_count,
            "rows": [item.to_dict() for item in self.rows],
            "model_sha256": self.model_sha256,
            "input_snapshot_sha256": self.input_snapshot_sha256,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.to_content_dict()
        payload["ranking_sha256"] = self.ranking_sha256
        return payload


def _log_return(current: float, previous: float) -> float:
    return math.log(current / previous)


def _factor_codes(prefix: str, reasons: Iterable[str]) -> tuple[str, ...]:
    return tuple(f"{prefix}:{item}" for item in reasons)


def _validate_common_snapshot(snapshot: ControlledPitSnapshotV2) -> tuple[str, ...]:
    """Return batch-level fail-closed reasons without trusting source booleans."""

    decision = snapshot.decision_at
    decision_session = _cst_session_date(decision, "decision_at")
    reasons: list[str] = []
    if canonical_sha256(snapshot.to_content_dict()) != snapshot.input_snapshot_sha256:
        reasons.append("INPUT_SNAPSHOT_HASH_MISMATCH")
    sessions = snapshot.trading_sessions
    if snapshot.universe_as_of != decision_session:
        reasons.append("UNIVERSE_DATE_MISMATCH")
    if snapshot.universe_available_at > decision:
        reasons.append("FUTURE_UNIVERSE_AVAILABLE_AT")
    if not sessions or tuple(sorted(set(sessions))) != sessions:
        reasons.append("INVALID_TRADING_SESSION_CALENDAR")
    else:
        if sessions[-1] != decision_session:
            reasons.append("DECISION_SESSION_NOT_LAST_CONTROLLED_SESSION")
        if any(item > decision_session for item in sessions):
            reasons.append("FUTURE_TRADING_SESSION")

    allowed_sessions = set(sessions)
    all_instrument_bars = tuple(
        bar for instrument in snapshot.instruments for bar in instrument.price_bars
    )
    all_bars = all_instrument_bars + snapshot.benchmark_price_bars
    if any(item.session_date > decision_session for item in all_bars):
        reasons.append("FUTURE_PRICE_SESSION")
    if any(item.available_at > decision for item in all_bars):
        reasons.append("FUTURE_PRICE_AVAILABLE_AT")
    if allowed_sessions and any(item.session_date not in allowed_sessions for item in all_bars):
        reasons.append("PRICE_SESSION_NOT_IN_CONTROLLED_CALENDAR")
    for instrument in snapshot.instruments:
        if instrument.constituent_available_at is not None and instrument.constituent_available_at > decision:
            reasons.append("FUTURE_CONSTITUENT_AVAILABLE_AT")
        if instrument.industry_available_at is not None and instrument.industry_available_at > decision:
            reasons.append("FUTURE_INDUSTRY_AVAILABLE_AT")
        if any(item.period_end > decision_session for item in instrument.fundamentals):
            reasons.append("FUTURE_FUNDAMENTAL_PERIOD")
        if any(item.first_disclosed_at > decision for item in instrument.fundamentals):
            reasons.append("FUTURE_FUNDAMENTAL_AVAILABLE_AT")

    benchmark_dates = tuple(item.session_date for item in snapshot.benchmark_price_bars)
    if len(benchmark_dates) != len(set(benchmark_dates)):
        reasons.append("DUPLICATE_BENCHMARK_SESSION")
    if len(sessions) >= MIN_PRICE_SESSIONS:
        required = sessions[-MIN_PRICE_SESSIONS:]
        if not set(required).issubset(set(benchmark_dates)):
            reasons.append("INCOMPLETE_BENCHMARK_HISTORY")
    else:
        reasons.append("INCOMPLETE_CONTROLLED_PRICE_HISTORY")
    return _ordered_codes(reasons)


def _compute_fast_factors(
    instrument: ControlledPitInstrumentV2,
    snapshot: ControlledPitSnapshotV2,
) -> tuple[dict[str, float], tuple[str, ...]]:
    dates = tuple(item.session_date for item in instrument.price_bars)
    if len(dates) != len(set(dates)):
        return {}, ("DUPLICATE_PRICE_SESSION",)
    if len(snapshot.trading_sessions) < MIN_PRICE_SESSIONS:
        return {}, ("FAST_FACTOR_INSUFFICIENT_HISTORY",)
    required_dates = snapshot.trading_sessions[-MIN_PRICE_SESSIONS:]
    by_date = {item.session_date: item for item in instrument.price_bars}
    benchmark_by_date = {item.session_date: item for item in snapshot.benchmark_price_bars}
    if any(item not in by_date for item in required_dates):
        return {}, ("FAST_FACTOR_INCOMPLETE_STOCK_HISTORY",)
    if any(item not in benchmark_by_date for item in required_dates):
        return {}, ("FAST_FACTOR_INCOMPLETE_BENCHMARK_HISTORY",)
    series = [by_date[item] for item in required_dates]
    benchmark = [benchmark_by_date[item] for item in required_dates]
    current = series[-1]
    values: dict[str, float] = {}
    for lookback in (20, 60, 120):
        values[f"RM{lookback}"] = _log_return(current.close, series[-1 - lookback].close) - _log_return(
            benchmark[-1].close, benchmark[-1 - lookback].close
        )
    path = series[-61:]
    path_length = sum(abs(path[index].close - path[index - 1].close) for index in range(1, 61))
    values["TREND_EFF60"] = 0.0 if path_length == 0.0 else (path[-1].close - path[0].close) / path_length
    downside = [
        min(_log_return(path[index].close, path[index - 1].close), 0.0)
        for index in range(1, 61)
    ]
    values["DOWNSIDE_VOL60"] = stdev(downside)
    prior_high = max(item.high for item in path[:-1])
    values["BREAKOUT60"] = path[-1].close / prior_high - 1.0
    if tuple(values) != FAST_FACTOR_IDS or any(not math.isfinite(item) for item in values.values()):
        return {}, ("FAST_FACTOR_NON_FINITE",)
    return values, ()


def _instrument_features(
    instrument: ControlledPitInstrumentV2,
    snapshot: ControlledPitSnapshotV2,
) -> tuple[dict[str, float], tuple[str, ...]]:
    exclusions: list[str] = []
    if instrument.constituent_available_at is None:
        exclusions.append("MISSING_CONSTITUENT_AVAILABLE_AT")
    if instrument.industry is None:
        exclusions.append("MISSING_PIT_INDUSTRY")
    if instrument.industry_available_at is None:
        exclusions.append("MISSING_INDUSTRY_AVAILABLE_AT")
    if instrument.industry_is_financial is None:
        exclusions.append("MISSING_FINANCIAL_CLASSIFICATION")

    quality_values: dict[str, float] = {}
    if not instrument.fundamentals:
        exclusions.append("MISSING_PIT_FUNDAMENTALS")
    elif instrument.industry_is_financial is not None:
        try:
            quality = compute_quality_growth_snapshot(
                instrument.fundamentals,
                decision_at=snapshot.decision_at,
                industry_is_financial=instrument.industry_is_financial,
            )
        except QualityGrowthError as exc:
            exclusions.append(f"QUALITY_FACTOR_INPUT_INVALID:{type(exc).__name__}")
        else:
            required_quality = FINANCIAL_FACTOR_IDS if instrument.industry_is_financial else QUALITY_GROWTH_FACTOR_IDS
            by_id = {item.factor_id: item for item in quality.factors}
            for factor_id in required_quality:
                factor = by_id[factor_id]
                if factor.availability is not FactorAvailability.AVAILABLE or factor.value is None:
                    reason = factor.reason or factor.availability.value
                    exclusions.append(f"MISSING_FACTOR:{factor_id}:{reason}")
                else:
                    quality_values[factor_id] = factor.value

    fast_values, fast_exclusions = _compute_fast_factors(instrument, snapshot)
    exclusions.extend(fast_exclusions)
    if exclusions:
        return {}, _ordered_codes(exclusions)
    return {**quality_values, **fast_values}, ()


def _excluded_rows(
    snapshot: ControlledPitSnapshotV2,
    codes: Sequence[str],
) -> tuple[AlphaPredictionRowV2, ...]:
    inputs = {item.instrument_id: item for item in snapshot.instruments}
    return tuple(
        AlphaPredictionRowV2(
            instrument_id=instrument_id,
            decision_at=snapshot.decision_at,
            predicted_return=None,
            quality_score=None,
            timing_score=None,
            percentile=None,
            rank=None,
            industry=(inputs[instrument_id].industry if instrument_id in inputs else None),
            eligibility=False,
            exclusion_codes=tuple(codes),
        )
        for instrument_id in snapshot.member_ids
    )


def run_alpha_engine(
    snapshot: ControlledPitSnapshotV2,
    model: FrozenAlphaModelV2,
) -> AlphaRankingV2:
    """Produce one deterministic full-universe ranking without creating orders."""

    if not isinstance(snapshot, ControlledPitSnapshotV2):
        raise AlphaEngineError("snapshot must be ControlledPitSnapshotV2")
    if not isinstance(model, FrozenAlphaModelV2):
        raise AlphaEngineError("model must be FrozenAlphaModelV2")

    common_reasons = list(_validate_common_snapshot(snapshot))
    if canonical_sha256(model.to_content_dict()) != model.model_sha256:
        common_reasons.append("MODEL_HASH_MISMATCH")
    decision_session = _cst_session_date(snapshot.decision_at, "decision_at")
    if model.training_window_end >= decision_session:
        common_reasons.append("MODEL_TRAINING_WINDOW_NOT_PRIOR_TO_DECISION")
    if model.training_data_cutoff_at > snapshot.decision_at:
        common_reasons.append("FUTURE_MODEL_TRAINING_DATA")
    if model.trained_at > snapshot.decision_at or model.frozen_at > snapshot.decision_at:
        common_reasons.append("MODEL_NOT_FROZEN_AT_DECISION")
    common_codes = _ordered_codes(common_reasons)
    if common_codes:
        return AlphaRankingV2(
            status=AlphaRunStatus.DATA_FAIL_CLOSED,
            decision_at=snapshot.decision_at,
            rows=_excluded_rows(snapshot, common_codes),
            model_sha256=model.model_sha256,
            input_snapshot_sha256=snapshot.input_snapshot_sha256,
        )

    inputs = {item.instrument_id: item for item in snapshot.instruments}
    scored: list[tuple[str, str | None, float, float, float]] = []
    excluded: list[AlphaPredictionRowV2] = []
    for instrument_id in snapshot.member_ids:
        instrument = inputs.get(instrument_id)
        if instrument is None:
            excluded.append(
                AlphaPredictionRowV2(
                    instrument_id=instrument_id,
                    decision_at=snapshot.decision_at,
                    predicted_return=None,
                    quality_score=None,
                    timing_score=None,
                    percentile=None,
                    rank=None,
                    industry=None,
                    eligibility=False,
                    exclusion_codes=("MISSING_INSTRUMENT_INPUT",),
                )
            )
            continue
        features, exclusions = _instrument_features(instrument, snapshot)
        if exclusions:
            excluded.append(
                AlphaPredictionRowV2(
                    instrument_id=instrument_id,
                    decision_at=snapshot.decision_at,
                    predicted_return=None,
                    quality_score=None,
                    timing_score=None,
                    percentile=None,
                    rank=None,
                    industry=instrument.industry,
                    eligibility=False,
                    exclusion_codes=exclusions,
                )
            )
            continue
        submodel = model.financial_submodel if instrument.industry_is_financial else model.non_financial_submodel
        predicted, quality_score, timing_score = submodel.score(features)
        scored.append((instrument_id, instrument.industry, predicted, quality_score, timing_score))

    scored.sort(key=lambda item: (-item[2], item[0]))
    eligible_rows: list[AlphaPredictionRowV2] = []
    count = len(scored)
    for index, (instrument_id, industry, predicted, quality_score, timing_score) in enumerate(scored):
        percentile = 1.0 if count == 1 else (count - index - 1) / (count - 1)
        eligible_rows.append(
            AlphaPredictionRowV2(
                instrument_id=instrument_id,
                decision_at=snapshot.decision_at,
                predicted_return=predicted,
                quality_score=quality_score,
                timing_score=timing_score,
                percentile=percentile,
                rank=index + 1,
                industry=industry,
                eligibility=True,
                exclusion_codes=(),
            )
        )
    excluded.sort(key=lambda item: item.instrument_id)
    rows = tuple(eligible_rows + excluded)
    return AlphaRankingV2(
        status=AlphaRunStatus.OK if eligible_rows else AlphaRunStatus.NO_ALPHA_CASH,
        decision_at=snapshot.decision_at,
        rows=rows,
        model_sha256=model.model_sha256,
        input_snapshot_sha256=snapshot.input_snapshot_sha256,
    )


run_alpha_engine_v2 = run_alpha_engine


__all__ = [
    "ALPHA_RANKING_SCHEMA_VERSION",
    "CONTROLLED_PIT_SNAPSHOT_SCHEMA_VERSION",
    "FAST_FACTOR_IDS",
    "FINANCIAL_FEATURE_IDS",
    "FROZEN_ALPHA_MODEL_SCHEMA_VERSION",
    "MIN_PRICE_SESSIONS",
    "NON_FINANCIAL_FEATURE_IDS",
    "AlphaEngineError",
    "AlphaPredictionRowV2",
    "AlphaRankingV2",
    "AlphaRunStatus",
    "ControlledPitInstrumentV2",
    "ControlledPitSnapshotV2",
    "ControlledPriceBarV2",
    "FrozenAlphaModelV2",
    "FrozenLinearSubmodelV2",
    "run_alpha_engine",
    "run_alpha_engine_v2",
]
