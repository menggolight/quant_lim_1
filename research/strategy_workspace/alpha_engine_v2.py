"""Deterministic point-in-time alpha production for Adaptive Exposure V2.

This module is deliberately research-only.  It consumes a typed, self-hashed
PIT snapshot, recomputes the frozen quality/growth and pre-registered
close-price timing factors, and applies an Experiment-V3 diagnostic train-only
model that is not formally admitted. Financial and non-financial raw scores are transformed onto one common
forward-return target by a frozen train-only calibration before comparison.
It never creates orders or changes an account.

Two failure levels are intentionally different:

* future/common-source contamination fails the complete cross-section closed;
* an instrument with missing PIT fields or factors remains in the output with
  complete exclusion codes and no prediction.

A model self-hash is not admission evidence. Formal scoring also requires a
controlled Experiment V3 loader, which is currently blocked.

Missing values are never converted to zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
import math
from pathlib import Path
import re
from statistics import stdev
from typing import Any, Iterable, Mapping, Sequence

from research.factor_discovery.governance import ApprovedFactorRegistryV1

from .contracts import canonical_sha256
from .experiment_v3_admission import (
    ExperimentV3AdmissionError,
    ExperimentV3AdmissionReceiptV1,
    verify_experiment_v3_admission_receipt,
    verify_experiment_v3_diagnostic_binding,
)
from .quality_growth import (
    FINANCIAL_FACTOR_IDS,
    QUALITY_GROWTH_FACTOR_IDS,
    FactorAvailability,
    QualityGrowthError,
    QuarterlyFundamental,
    compute_quality_growth_snapshot,
)


CONTROLLED_PIT_SNAPSHOT_SCHEMA_VERSION = "controlled-pit-decision-snapshot.v1"
FROZEN_ALPHA_MODEL_SCHEMA_VERSION = "frozen-alpha-model.v2"
FROZEN_ALPHA_CALIBRATION_SCHEMA_VERSION = "frozen-alpha-calibration.v1"
ALPHA_MODEL_TRAINING_RECEIPT_SCHEMA_VERSION = "alpha-model-training-receipt.v1"
ALPHA_MODEL_ADMISSION_RECEIPT_SCHEMA_VERSION = "alpha-model-admission-receipt.v1"
ALPHA_RUNTIME_BUILD_MANIFEST_SCHEMA_VERSION = "alpha-runtime-build-manifest.v1"
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
    DIAGNOSTIC_ONLY_NOT_ADMITTED = "DIAGNOSTIC_ONLY_NOT_ADMITTED"


def compute_alpha_runtime_code_sha256() -> str:
    """Hash the exact factor/scoring source files executed by this runtime."""

    workspace_root = Path(__file__).resolve().parents[2]
    relative_paths = (
        "research/strategy_workspace/alpha_engine_v2.py",
        "research/strategy_workspace/quality_growth.py",
    )
    files = []
    for relative_path in relative_paths:
        payload = (workspace_root / relative_path).read_bytes()
        files.append(
            {
                "path": relative_path,
                "file_sha256": sha256(payload).hexdigest(),
            }
        )
    return canonical_sha256(
        {"scope": "alpha-runtime-code-build.v1", "files": files}
    )


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


def _positive_integer(value: Any, field_name: str) -> int:
    if type(value) is not int or value < 1:
        raise AlphaEngineError(f"{field_name} must be a positive integer")
    return value


def _date_value(value: Any, field_name: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise AlphaEngineError(f"{field_name} must be a date")
    return value


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
class AlphaFactorRuntimeBindingV1:
    """Exact approved factor semantics expected by the runtime build."""

    factor_id: str
    formula_sha256: str
    implementation_code_sha256: str
    input_schema_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "factor_id", _identifier(self.factor_id, "factor_id"))
        for field_name in (
            "formula_sha256",
            "implementation_code_sha256",
            "input_schema_sha256",
        ):
            _sha(getattr(self, field_name), field_name)

    def to_dict(self) -> dict[str, str]:
        return {
            "factor_id": self.factor_id,
            "formula_sha256": self.formula_sha256,
            "implementation_code_sha256": self.implementation_code_sha256,
            "input_schema_sha256": self.input_schema_sha256,
        }


@dataclass(frozen=True, slots=True)
class AlphaRuntimeBuildManifestV1:
    """Frozen build manifest binding runtime factors to approved semantics."""

    manifest_id: str
    built_at: datetime
    experiment_spec_sha256: str
    approved_factor_registry_sha256: str
    prediction_target: str
    prediction_horizon_sessions: int
    universe_policy: str
    benchmark_policy: str
    runtime_code_sha256: str
    factor_bindings: tuple[AlphaFactorRuntimeBindingV1, ...]
    schema_version: str = ALPHA_RUNTIME_BUILD_MANIFEST_SCHEMA_VERSION
    manifest_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != ALPHA_RUNTIME_BUILD_MANIFEST_SCHEMA_VERSION:
            raise AlphaEngineError("unsupported alpha runtime build manifest schema")
        object.__setattr__(self, "manifest_id", _identifier(self.manifest_id, "manifest_id"))
        object.__setattr__(self, "built_at", _aware(self.built_at, "built_at"))
        for field_name in (
            "experiment_spec_sha256",
            "approved_factor_registry_sha256",
            "runtime_code_sha256",
        ):
            _sha(getattr(self, field_name), field_name)
        object.__setattr__(self, "prediction_target", _identifier(self.prediction_target, "prediction_target"))
        _positive_integer(self.prediction_horizon_sessions, "prediction_horizon_sessions")
        object.__setattr__(self, "universe_policy", _identifier(self.universe_policy, "universe_policy"))
        object.__setattr__(self, "benchmark_policy", _identifier(self.benchmark_policy, "benchmark_policy"))
        bindings = tuple(self.factor_bindings)
        if not bindings or any(
            not isinstance(item, AlphaFactorRuntimeBindingV1) for item in bindings
        ):
            raise AlphaEngineError("runtime build manifest requires typed factor bindings")
        bindings = tuple(sorted(bindings, key=lambda item: item.factor_id))
        factor_ids = tuple(item.factor_id for item in bindings)
        if len(set(factor_ids)) != len(factor_ids):
            raise AlphaEngineError("runtime build manifest factor_ids must be unique")
        object.__setattr__(self, "factor_bindings", bindings)
        object.__setattr__(self, "manifest_sha256", canonical_sha256(self.to_content_dict()))

    def require_valid(self, *, as_of: datetime) -> "AlphaRuntimeBuildManifestV1":
        if self.built_at > _aware(as_of, "as_of"):
            raise AlphaEngineError("runtime build manifest is future-dated")
        if canonical_sha256(self.to_content_dict()) != self.manifest_sha256:
            raise AlphaEngineError("runtime build manifest hash mismatch")
        return self

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "manifest_id": self.manifest_id,
            "built_at": self.built_at.isoformat(),
            "experiment_spec_sha256": self.experiment_spec_sha256,
            "approved_factor_registry_sha256": self.approved_factor_registry_sha256,
            "prediction_target": self.prediction_target,
            "prediction_horizon_sessions": self.prediction_horizon_sessions,
            "universe_policy": self.universe_policy,
            "benchmark_policy": self.benchmark_policy,
            "runtime_code_sha256": self.runtime_code_sha256,
            "factor_bindings": [item.to_dict() for item in self.factor_bindings],
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.to_content_dict(), "manifest_sha256": self.manifest_sha256}


@dataclass(frozen=True, slots=True)
class FrozenAlphaCalibrationV1:
    """Train-only affine calibration onto one common forward-return target.

    Separate financial/non-financial raw models are not directly comparable.
    Both therefore require a frozen transform fitted only on the declared
    training window and onto the same target and horizon before ranking.
    """

    calibration_id: str
    target_id: str
    prediction_horizon_sessions: int
    experiment_spec_sha256: str
    approved_factor_registry_sha256: str
    universe_policy: str
    benchmark_policy: str
    fitting_window_start: date
    fitting_window_end: date
    fitting_data_cutoff_at: datetime
    fitted_at: datetime
    financial_intercept: float
    financial_slope: float
    non_financial_intercept: float
    non_financial_slope: float
    calibration_dataset_sha256: str
    calibration_code_sha256: str
    calibration_config_sha256: str
    calibration_method: str = "submodel_affine_common_target"
    fitting_partition: str = "train_only"
    schema_version: str = FROZEN_ALPHA_CALIBRATION_SCHEMA_VERSION
    calibration_receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != FROZEN_ALPHA_CALIBRATION_SCHEMA_VERSION:
            raise AlphaEngineError("unsupported frozen alpha calibration schema")
        if self.calibration_method != "submodel_affine_common_target":
            raise AlphaEngineError("unsupported alpha calibration method")
        if self.fitting_partition != "train_only":
            raise AlphaEngineError("alpha calibration must be fitted on train_only")
        object.__setattr__(self, "calibration_id", _identifier(self.calibration_id, "calibration_id"))
        object.__setattr__(self, "target_id", _identifier(self.target_id, "target_id"))
        _positive_integer(self.prediction_horizon_sessions, "prediction_horizon_sessions")
        for field_name in (
            "experiment_spec_sha256",
            "approved_factor_registry_sha256",
        ):
            _sha(getattr(self, field_name), field_name)
        object.__setattr__(self, "universe_policy", _identifier(self.universe_policy, "universe_policy"))
        object.__setattr__(self, "benchmark_policy", _identifier(self.benchmark_policy, "benchmark_policy"))
        start = _date_value(self.fitting_window_start, "fitting_window_start")
        end = _date_value(self.fitting_window_end, "fitting_window_end")
        if start > end:
            raise AlphaEngineError("calibration fitting window is inverted")
        cutoff = _aware(self.fitting_data_cutoff_at, "fitting_data_cutoff_at")
        fitted = _aware(self.fitted_at, "fitted_at")
        if _cst_session_date(cutoff, "fitting_data_cutoff_at") < end:
            raise AlphaEngineError("calibration cutoff precedes fitting window end")
        if cutoff > fitted:
            raise AlphaEngineError("calibration cutoff must not follow fitted_at")
        for field_name in (
            "financial_intercept",
            "financial_slope",
            "non_financial_intercept",
            "non_financial_slope",
        ):
            value = _finite(getattr(self, field_name), field_name)
            if field_name.endswith("_slope") and value <= 0.0:
                raise AlphaEngineError("calibration slopes must be positive")
            object.__setattr__(self, field_name, value)
        for field_name in (
            "calibration_dataset_sha256",
            "calibration_code_sha256",
            "calibration_config_sha256",
        ):
            _sha(getattr(self, field_name), field_name)
        object.__setattr__(
            self,
            "calibration_receipt_sha256",
            canonical_sha256(self.to_content_dict()),
        )

    def calibrate(
        self,
        submodel_id: str,
        raw_prediction: float,
        raw_quality_score: float,
        raw_timing_score: float,
    ) -> tuple[float, float, float]:
        if submodel_id == "financial":
            intercept, slope = self.financial_intercept, self.financial_slope
        elif submodel_id == "non_financial":
            intercept, slope = self.non_financial_intercept, self.non_financial_slope
        else:
            raise AlphaEngineError("calibration requires a known submodel_id")
        predicted = intercept + slope * _finite(raw_prediction, "raw_prediction")
        quality = slope * _finite(raw_quality_score, "raw_quality_score")
        timing = slope * _finite(raw_timing_score, "raw_timing_score")
        if not all(math.isfinite(item) for item in (predicted, quality, timing)):
            raise AlphaEngineError("calibration produced a non-finite score")
        return predicted, quality, timing

    def require_valid(self) -> "FrozenAlphaCalibrationV1":
        if canonical_sha256(self.to_content_dict()) != self.calibration_receipt_sha256:
            raise AlphaEngineError("alpha calibration receipt hash mismatch")
        return self

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "calibration_id": self.calibration_id,
            "calibration_method": self.calibration_method,
            "fitting_partition": self.fitting_partition,
            "target_id": self.target_id,
            "prediction_horizon_sessions": self.prediction_horizon_sessions,
            "experiment_spec_sha256": self.experiment_spec_sha256,
            "approved_factor_registry_sha256": self.approved_factor_registry_sha256,
            "universe_policy": self.universe_policy,
            "benchmark_policy": self.benchmark_policy,
            "fitting_window_start": self.fitting_window_start.isoformat(),
            "fitting_window_end": self.fitting_window_end.isoformat(),
            "fitting_data_cutoff_at": self.fitting_data_cutoff_at.isoformat(),
            "fitted_at": self.fitted_at.isoformat(),
            "financial_intercept": self.financial_intercept,
            "financial_slope": self.financial_slope,
            "non_financial_intercept": self.non_financial_intercept,
            "non_financial_slope": self.non_financial_slope,
            "calibration_dataset_sha256": self.calibration_dataset_sha256,
            "calibration_code_sha256": self.calibration_code_sha256,
            "calibration_config_sha256": self.calibration_config_sha256,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.to_content_dict()
        payload["calibration_receipt_sha256"] = self.calibration_receipt_sha256
        return payload


def compute_alpha_submodel_bundle_sha256(
    financial_submodel: FrozenLinearSubmodelV2,
    non_financial_submodel: FrozenLinearSubmodelV2,
) -> str:
    return canonical_sha256(
        {
            "financial_submodel": financial_submodel.to_dict(),
            "non_financial_submodel": non_financial_submodel.to_dict(),
        }
    )


@dataclass(frozen=True, slots=True)
class AlphaModelTrainingReceiptV1:
    """Content-bound evidence for the train-only model fitting step."""

    receipt_id: str
    issued_at: datetime
    model_id: str
    model_version: str
    experiment_spec_sha256: str
    approved_factor_registry_sha256: str
    prediction_target: str
    prediction_horizon_sessions: int
    universe_policy: str
    benchmark_policy: str
    training_window_start: date
    training_window_end: date
    training_data_cutoff_at: datetime
    trained_at: datetime
    training_dataset_sha256: str
    training_code_sha256: str
    preprocessing_policy_sha256: str
    model_config_sha256: str
    submodel_bundle_sha256: str
    runtime_build_manifest_sha256: str
    status: str = "completed_train_only"
    schema_version: str = ALPHA_MODEL_TRAINING_RECEIPT_SCHEMA_VERSION
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != ALPHA_MODEL_TRAINING_RECEIPT_SCHEMA_VERSION:
            raise AlphaEngineError("unsupported alpha model training receipt schema")
        if self.status != "completed_train_only":
            raise AlphaEngineError("model training receipt must be completed_train_only")
        object.__setattr__(self, "receipt_id", _identifier(self.receipt_id, "receipt_id"))
        object.__setattr__(self, "model_id", _identifier(self.model_id, "model_id"))
        object.__setattr__(self, "model_version", _identifier(self.model_version, "model_version"))
        issued = _aware(self.issued_at, "issued_at")
        start = _date_value(self.training_window_start, "training_window_start")
        end = _date_value(self.training_window_end, "training_window_end")
        if start > end:
            raise AlphaEngineError("training receipt window is inverted")
        cutoff = _aware(self.training_data_cutoff_at, "training_data_cutoff_at")
        trained = _aware(self.trained_at, "trained_at")
        if _cst_session_date(cutoff, "training_data_cutoff_at") < end:
            raise AlphaEngineError("training receipt cutoff precedes training window end")
        if cutoff > trained or trained > issued:
            raise AlphaEngineError("training receipt timestamps are out of order")
        _positive_integer(self.prediction_horizon_sessions, "prediction_horizon_sessions")
        object.__setattr__(self, "prediction_target", _identifier(self.prediction_target, "prediction_target"))
        object.__setattr__(self, "universe_policy", _identifier(self.universe_policy, "universe_policy"))
        object.__setattr__(self, "benchmark_policy", _identifier(self.benchmark_policy, "benchmark_policy"))
        for field_name in (
            "experiment_spec_sha256",
            "approved_factor_registry_sha256",
            "training_dataset_sha256",
            "training_code_sha256",
            "preprocessing_policy_sha256",
            "model_config_sha256",
            "submodel_bundle_sha256",
            "runtime_build_manifest_sha256",
        ):
            _sha(getattr(self, field_name), field_name)
        object.__setattr__(self, "receipt_sha256", canonical_sha256(self.to_content_dict()))

    def require_valid(self, *, as_of: datetime) -> "AlphaModelTrainingReceiptV1":
        if canonical_sha256(self.to_content_dict()) != self.receipt_sha256:
            raise AlphaEngineError("model training receipt hash mismatch")
        if self.issued_at > _aware(as_of, "as_of"):
            raise AlphaEngineError("model training receipt is future-dated")
        return self

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "status": self.status,
            "issued_at": self.issued_at.isoformat(),
            "model_id": self.model_id,
            "model_version": self.model_version,
            "experiment_spec_sha256": self.experiment_spec_sha256,
            "approved_factor_registry_sha256": self.approved_factor_registry_sha256,
            "prediction_target": self.prediction_target,
            "prediction_horizon_sessions": self.prediction_horizon_sessions,
            "universe_policy": self.universe_policy,
            "benchmark_policy": self.benchmark_policy,
            "training_window_start": self.training_window_start.isoformat(),
            "training_window_end": self.training_window_end.isoformat(),
            "training_data_cutoff_at": self.training_data_cutoff_at.isoformat(),
            "trained_at": self.trained_at.isoformat(),
            "training_dataset_sha256": self.training_dataset_sha256,
            "training_code_sha256": self.training_code_sha256,
            "preprocessing_policy_sha256": self.preprocessing_policy_sha256,
            "model_config_sha256": self.model_config_sha256,
            "submodel_bundle_sha256": self.submodel_bundle_sha256,
            "runtime_build_manifest_sha256": self.runtime_build_manifest_sha256,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.to_content_dict()
        payload["receipt_sha256"] = self.receipt_sha256
        return payload


@dataclass(frozen=True, slots=True)
class AlphaModelAdmissionReceiptV1:
    """Diagnostic model review bound to the complete frozen candidate."""

    receipt_id: str
    issued_at: datetime
    model_id: str
    model_version: str
    model_candidate_sha256: str
    experiment_spec_sha256: str
    approved_factor_registry_sha256: str
    prediction_target: str
    model_training_receipt_sha256: str
    calibration_receipt_sha256: str
    prediction_horizon_sessions: int
    universe_policy: str
    benchmark_policy: str
    runtime_build_manifest_sha256: str
    status: str = "validated_for_experiment_v3_diagnostic_only_not_formally_admitted"
    schema_version: str = ALPHA_MODEL_ADMISSION_RECEIPT_SCHEMA_VERSION
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != ALPHA_MODEL_ADMISSION_RECEIPT_SCHEMA_VERSION:
            raise AlphaEngineError("unsupported alpha model admission receipt schema")
        if self.status != "validated_for_experiment_v3_diagnostic_only_not_formally_admitted":
            raise AlphaEngineError("model review status cannot claim formal V3 admission")
        object.__setattr__(self, "receipt_id", _identifier(self.receipt_id, "receipt_id"))
        object.__setattr__(self, "model_id", _identifier(self.model_id, "model_id"))
        object.__setattr__(self, "model_version", _identifier(self.model_version, "model_version"))
        _aware(self.issued_at, "issued_at")
        _positive_integer(self.prediction_horizon_sessions, "prediction_horizon_sessions")
        object.__setattr__(self, "prediction_target", _identifier(self.prediction_target, "prediction_target"))
        object.__setattr__(self, "universe_policy", _identifier(self.universe_policy, "universe_policy"))
        object.__setattr__(self, "benchmark_policy", _identifier(self.benchmark_policy, "benchmark_policy"))
        for field_name in (
            "model_candidate_sha256",
            "experiment_spec_sha256",
            "approved_factor_registry_sha256",
            "model_training_receipt_sha256",
            "calibration_receipt_sha256",
            "runtime_build_manifest_sha256",
        ):
            _sha(getattr(self, field_name), field_name)
        object.__setattr__(self, "receipt_sha256", canonical_sha256(self.to_content_dict()))

    def require_valid(self, *, as_of: datetime) -> "AlphaModelAdmissionReceiptV1":
        if canonical_sha256(self.to_content_dict()) != self.receipt_sha256:
            raise AlphaEngineError("model admission receipt hash mismatch")
        if self.issued_at > _aware(as_of, "as_of"):
            raise AlphaEngineError("model admission receipt is future-dated")
        return self

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "status": self.status,
            "issued_at": self.issued_at.isoformat(),
            "model_id": self.model_id,
            "model_version": self.model_version,
            "model_candidate_sha256": self.model_candidate_sha256,
            "experiment_spec_sha256": self.experiment_spec_sha256,
            "approved_factor_registry_sha256": self.approved_factor_registry_sha256,
            "prediction_target": self.prediction_target,
            "model_training_receipt_sha256": self.model_training_receipt_sha256,
            "calibration_receipt_sha256": self.calibration_receipt_sha256,
            "prediction_horizon_sessions": self.prediction_horizon_sessions,
            "universe_policy": self.universe_policy,
            "benchmark_policy": self.benchmark_policy,
            "runtime_build_manifest_sha256": self.runtime_build_manifest_sha256,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.to_content_dict()
        payload["receipt_sha256"] = self.receipt_sha256
        return payload


def _alpha_model_candidate_content(
    *,
    model_id: str,
    model_version: str,
    artifact_status: str,
    training_partition: str,
    training_window_start: date,
    training_window_end: date,
    training_data_cutoff_at: datetime,
    trained_at: datetime,
    frozen_at: datetime,
    training_dataset_sha256: str,
    training_code_sha256: str,
    preprocessing_policy_sha256: str,
    model_config_sha256: str,
    experiment_spec_sha256: str,
    approved_factor_registry_sha256: str,
    prediction_target: str,
    prediction_horizon_sessions: int,
    universe_policy: str,
    benchmark_policy: str,
    runtime_build_manifest: AlphaRuntimeBuildManifestV1,
    calibration_artifact: FrozenAlphaCalibrationV1,
    model_training_receipt: AlphaModelTrainingReceiptV1,
    financial_submodel: FrozenLinearSubmodelV2,
    non_financial_submodel: FrozenLinearSubmodelV2,
) -> dict[str, Any]:
    return {
        "schema_version": FROZEN_ALPHA_MODEL_SCHEMA_VERSION,
        "model_id": model_id,
        "model_version": model_version,
        "artifact_status": artifact_status,
        "training_partition": training_partition,
        "training_window_start": training_window_start.isoformat(),
        "training_window_end": training_window_end.isoformat(),
        "training_data_cutoff_at": training_data_cutoff_at.isoformat(),
        "trained_at": trained_at.isoformat(),
        "frozen_at": frozen_at.isoformat(),
        "training_dataset_sha256": training_dataset_sha256,
        "training_code_sha256": training_code_sha256,
        "preprocessing_policy_sha256": preprocessing_policy_sha256,
        "model_config_sha256": model_config_sha256,
        "experiment_spec_sha256": experiment_spec_sha256,
        "approved_factor_registry_sha256": approved_factor_registry_sha256,
        "prediction_target": prediction_target,
        "prediction_horizon_sessions": prediction_horizon_sessions,
        "universe_policy": universe_policy,
        "benchmark_policy": benchmark_policy,
        "runtime_build_manifest": runtime_build_manifest.to_dict(),
        "runtime_build_manifest_sha256": runtime_build_manifest.manifest_sha256,
        "calibration_artifact": calibration_artifact.to_dict(),
        "calibration_receipt_sha256": calibration_artifact.calibration_receipt_sha256,
        "model_training_receipt": model_training_receipt.to_dict(),
        "model_training_receipt_sha256": model_training_receipt.receipt_sha256,
        "financial_submodel": financial_submodel.to_dict(),
        "non_financial_submodel": non_financial_submodel.to_dict(),
    }


def compute_alpha_model_candidate_sha256(**kwargs: Any) -> str:
    """Hash the pre-admission candidate using the production canonical shape."""

    return canonical_sha256(_alpha_model_candidate_content(**kwargs))


@dataclass(frozen=True, slots=True)
class FrozenAlphaModelV2:
    """Experiment-V3-bound diagnostic model; legacy v1 is rejected at runtime."""

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
    experiment_spec_sha256: str
    approved_factor_registry_sha256: str
    prediction_target: str
    prediction_horizon_sessions: int
    universe_policy: str
    benchmark_policy: str
    runtime_build_manifest: AlphaRuntimeBuildManifestV1
    calibration_artifact: FrozenAlphaCalibrationV1
    model_training_receipt: AlphaModelTrainingReceiptV1
    model_admission_receipt: AlphaModelAdmissionReceiptV1
    financial_submodel: FrozenLinearSubmodelV2
    non_financial_submodel: FrozenLinearSubmodelV2
    artifact_status: str = "frozen_train_only_calibrated_research_candidate"
    training_partition: str = "train_only"
    schema_version: str = FROZEN_ALPHA_MODEL_SCHEMA_VERSION
    calibration_receipt_sha256: str = field(init=False)
    model_training_receipt_sha256: str = field(init=False)
    model_admission_receipt_sha256: str = field(init=False)
    model_candidate_sha256: str = field(init=False)
    model_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != FROZEN_ALPHA_MODEL_SCHEMA_VERSION:
            raise AlphaEngineError("unsupported frozen alpha model schema; legacy v1 is not admitted")
        if (
            self.artifact_status != "frozen_train_only_calibrated_research_candidate"
            or self.training_partition != "train_only"
        ):
            raise AlphaEngineError(
                "alpha model must remain a train-only calibrated research candidate"
            )
        object.__setattr__(self, "model_id", _identifier(self.model_id, "model_id"))
        object.__setattr__(self, "model_version", _identifier(self.model_version, "model_version"))
        start = _date_value(self.training_window_start, "training_window_start")
        end = _date_value(self.training_window_end, "training_window_end")
        if start > end:
            raise AlphaEngineError("training window is inverted")
        cutoff = _aware(self.training_data_cutoff_at, "training_data_cutoff_at")
        trained = _aware(self.trained_at, "trained_at")
        frozen = _aware(self.frozen_at, "frozen_at")
        if _cst_session_date(cutoff, "training_data_cutoff_at") < end:
            raise AlphaEngineError("training data cutoff precedes training window end")
        if cutoff > trained or trained > frozen:
            raise AlphaEngineError("training cutoff, trained_at and frozen_at are out of order")
        _positive_integer(self.prediction_horizon_sessions, "prediction_horizon_sessions")
        for field_name in (
            "training_dataset_sha256",
            "training_code_sha256",
            "preprocessing_policy_sha256",
            "model_config_sha256",
            "experiment_spec_sha256",
            "approved_factor_registry_sha256",
        ):
            _sha(getattr(self, field_name), field_name)
        object.__setattr__(self, "prediction_target", _identifier(self.prediction_target, "prediction_target"))
        object.__setattr__(self, "universe_policy", _identifier(self.universe_policy, "universe_policy"))
        object.__setattr__(self, "benchmark_policy", _identifier(self.benchmark_policy, "benchmark_policy"))
        if not isinstance(self.runtime_build_manifest, AlphaRuntimeBuildManifestV1):
            raise AlphaEngineError("typed alpha runtime build manifest is required")
        self.runtime_build_manifest.require_valid(as_of=frozen)
        manifest_expected = {
            "experiment_spec_sha256": self.experiment_spec_sha256,
            "approved_factor_registry_sha256": self.approved_factor_registry_sha256,
            "prediction_target": self.prediction_target,
            "prediction_horizon_sessions": self.prediction_horizon_sessions,
            "universe_policy": self.universe_policy,
            "benchmark_policy": self.benchmark_policy,
        }
        if any(
            getattr(self.runtime_build_manifest, key) != value
            for key, value in manifest_expected.items()
        ):
            raise AlphaEngineError("runtime build manifest policy binding mismatch")
        if (
            self.runtime_build_manifest.runtime_code_sha256
            != compute_alpha_runtime_code_sha256()
        ):
            raise AlphaEngineError(
                "runtime build manifest does not match executing factor code"
            )
        if (
            not isinstance(self.financial_submodel, FrozenLinearSubmodelV2)
            or self.financial_submodel.submodel_id != "financial"
        ):
            raise AlphaEngineError("financial_submodel is required")
        if (
            not isinstance(self.non_financial_submodel, FrozenLinearSubmodelV2)
            or self.non_financial_submodel.submodel_id != "non_financial"
        ):
            raise AlphaEngineError("non_financial_submodel is required")
        if not isinstance(self.calibration_artifact, FrozenAlphaCalibrationV1):
            raise AlphaEngineError("typed frozen calibration artifact is required")
        self.calibration_artifact.require_valid()
        if self.calibration_artifact.prediction_horizon_sessions != self.prediction_horizon_sessions:
            raise AlphaEngineError("calibration prediction horizon mismatch")
        calibration_expected = {
            "target_id": self.prediction_target,
            "experiment_spec_sha256": self.experiment_spec_sha256,
            "approved_factor_registry_sha256": self.approved_factor_registry_sha256,
            "universe_policy": self.universe_policy,
            "benchmark_policy": self.benchmark_policy,
        }
        if any(
            getattr(self.calibration_artifact, key) != value
            for key, value in calibration_expected.items()
        ):
            raise AlphaEngineError("calibration target/policy binding mismatch")
        if (
            self.calibration_artifact.fitting_window_start != start
            or self.calibration_artifact.fitting_window_end != end
            or self.calibration_artifact.fitting_data_cutoff_at > cutoff
            or self.calibration_artifact.fitted_at > frozen
        ):
            raise AlphaEngineError("calibration is not bound to the common train-only window")
        if not isinstance(self.model_training_receipt, AlphaModelTrainingReceiptV1):
            raise AlphaEngineError("typed model training receipt is required")
        if not isinstance(self.model_admission_receipt, AlphaModelAdmissionReceiptV1):
            raise AlphaEngineError("typed model admission receipt is required")
        self.model_training_receipt.require_valid(as_of=frozen)
        bundle_sha256 = compute_alpha_submodel_bundle_sha256(
            self.financial_submodel,
            self.non_financial_submodel,
        )
        training_expected = {
            "model_id": self.model_id,
            "model_version": self.model_version,
            "experiment_spec_sha256": self.experiment_spec_sha256,
            "approved_factor_registry_sha256": self.approved_factor_registry_sha256,
            "prediction_target": self.prediction_target,
            "prediction_horizon_sessions": self.prediction_horizon_sessions,
            "universe_policy": self.universe_policy,
            "benchmark_policy": self.benchmark_policy,
            "training_window_start": start,
            "training_window_end": end,
            "training_data_cutoff_at": cutoff,
            "trained_at": trained,
            "training_dataset_sha256": self.training_dataset_sha256,
            "training_code_sha256": self.training_code_sha256,
            "preprocessing_policy_sha256": self.preprocessing_policy_sha256,
            "model_config_sha256": self.model_config_sha256,
            "submodel_bundle_sha256": bundle_sha256,
            "runtime_build_manifest_sha256": self.runtime_build_manifest.manifest_sha256,
        }
        if any(getattr(self.model_training_receipt, key) != value for key, value in training_expected.items()):
            raise AlphaEngineError("model training receipt does not bind the frozen model")
        object.__setattr__(
            self,
            "calibration_receipt_sha256",
            self.calibration_artifact.calibration_receipt_sha256,
        )
        object.__setattr__(
            self,
            "model_training_receipt_sha256",
            self.model_training_receipt.receipt_sha256,
        )
        candidate_sha256 = canonical_sha256(self.to_candidate_content_dict())
        object.__setattr__(self, "model_candidate_sha256", candidate_sha256)
        self.model_admission_receipt.require_valid(as_of=frozen)
        if not (
            self.model_training_receipt.issued_at
            <= self.model_admission_receipt.issued_at
            <= frozen
        ):
            raise AlphaEngineError("model review receipt timestamps are out of order")
        if self.calibration_artifact.fitted_at > self.model_admission_receipt.issued_at:
            raise AlphaEngineError("model review cannot precede calibration fitting")
        admission_expected = {
            "model_id": self.model_id,
            "model_version": self.model_version,
            "model_candidate_sha256": candidate_sha256,
            "experiment_spec_sha256": self.experiment_spec_sha256,
            "approved_factor_registry_sha256": self.approved_factor_registry_sha256,
            "prediction_target": self.prediction_target,
            "model_training_receipt_sha256": self.model_training_receipt_sha256,
            "calibration_receipt_sha256": self.calibration_receipt_sha256,
            "prediction_horizon_sessions": self.prediction_horizon_sessions,
            "universe_policy": self.universe_policy,
            "benchmark_policy": self.benchmark_policy,
            "runtime_build_manifest_sha256": self.runtime_build_manifest.manifest_sha256,
        }
        if any(getattr(self.model_admission_receipt, key) != value for key, value in admission_expected.items()):
            raise AlphaEngineError("model admission receipt does not bind the frozen candidate")
        object.__setattr__(
            self,
            "model_admission_receipt_sha256",
            self.model_admission_receipt.receipt_sha256,
        )
        object.__setattr__(self, "model_sha256", canonical_sha256(self.to_content_dict()))

    def to_candidate_content_dict(self) -> dict[str, Any]:
        return _alpha_model_candidate_content(
            model_id=self.model_id,
            model_version=self.model_version,
            artifact_status=self.artifact_status,
            training_partition=self.training_partition,
            training_window_start=self.training_window_start,
            training_window_end=self.training_window_end,
            training_data_cutoff_at=self.training_data_cutoff_at,
            trained_at=self.trained_at,
            frozen_at=self.frozen_at,
            training_dataset_sha256=self.training_dataset_sha256,
            training_code_sha256=self.training_code_sha256,
            preprocessing_policy_sha256=self.preprocessing_policy_sha256,
            model_config_sha256=self.model_config_sha256,
            experiment_spec_sha256=self.experiment_spec_sha256,
            approved_factor_registry_sha256=self.approved_factor_registry_sha256,
            prediction_target=self.prediction_target,
            prediction_horizon_sessions=self.prediction_horizon_sessions,
            universe_policy=self.universe_policy,
            benchmark_policy=self.benchmark_policy,
            runtime_build_manifest=self.runtime_build_manifest,
            calibration_artifact=self.calibration_artifact,
            model_training_receipt=self.model_training_receipt,
            financial_submodel=self.financial_submodel,
            non_financial_submodel=self.non_financial_submodel,
        )

    def to_content_dict(self) -> dict[str, Any]:
        payload = self.to_candidate_content_dict()
        payload.update(
            {
                "model_candidate_sha256": self.model_candidate_sha256,
                "model_admission_receipt": self.model_admission_receipt.to_dict(),
                "model_admission_receipt_sha256": self.model_admission_receipt_sha256,
            }
        )
        return payload

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
        if status not in {
            AlphaRunStatus.OK,
            AlphaRunStatus.DIAGNOSTIC_ONLY_NOT_ADMITTED,
        } and eligible:
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


def _validate_model_admission_evidence(
    snapshot: ControlledPitSnapshotV2,
    model: FrozenAlphaModelV2,
    approved_factor_registry: ApprovedFactorRegistryV1 | None,
    experiment_v3_admission_receipt: ExperimentV3AdmissionReceiptV1 | None,
) -> tuple[str, ...]:
    """Validate evidence objects instead of trusting caller booleans or hashes."""

    reasons: list[str] = []
    try:
        model.calibration_artifact.require_valid()
        model.model_training_receipt.require_valid(as_of=snapshot.decision_at)
        model.model_admission_receipt.require_valid(as_of=snapshot.decision_at)
    except (AlphaEngineError, ValueError, TypeError):
        reasons.append("INVALID_MODEL_INTERNAL_RECEIPT")
    if canonical_sha256(model.to_candidate_content_dict()) != model.model_candidate_sha256:
        reasons.append("MODEL_CANDIDATE_HASH_MISMATCH")
    if (
        model.runtime_build_manifest.runtime_code_sha256
        != compute_alpha_runtime_code_sha256()
    ):
        reasons.append("RUNTIME_CODE_HASH_MISMATCH")
    if model.model_admission_receipt.model_candidate_sha256 != model.model_candidate_sha256:
        reasons.append("MODEL_ADMISSION_CANDIDATE_MISMATCH")

    if not isinstance(approved_factor_registry, ApprovedFactorRegistryV1):
        reasons.append("MISSING_TYPED_APPROVED_FACTOR_REGISTRY")
    else:
        try:
            approved_factor_registry.require_valid(as_of=snapshot.decision_at)
        except (ValueError, TypeError):
            reasons.append("INVALID_APPROVED_FACTOR_REGISTRY")
        else:
            if approved_factor_registry.registry_sha256 != model.approved_factor_registry_sha256:
                reasons.append("MODEL_FACTOR_REGISTRY_HASH_MISMATCH")
            registry_expected = {
                "experiment_spec_sha256": model.experiment_spec_sha256,
                "prediction_target": model.prediction_target,
                "horizon_trading_days": model.prediction_horizon_sessions,
                "universe_policy": model.universe_policy,
                "benchmark_policy": model.benchmark_policy,
            }
            if any(
                getattr(approved_factor_registry, key) != value
                for key, value in registry_expected.items()
            ):
                reasons.append("APPROVED_FACTOR_REGISTRY_POLICY_MISMATCH")
            if approved_factor_registry.frozen_at > model.training_data_cutoff_at:
                reasons.append("FACTOR_REGISTRY_NOT_FROZEN_BEFORE_TRAINING")
            required_ids = tuple(
                sorted(set(model.financial_submodel.feature_ids) | set(model.non_financial_submodel.feature_ids))
            )
            if tuple(approved_factor_registry.approved_factor_ids) != required_ids:
                reasons.append("APPROVED_FACTOR_REGISTRY_FEATURE_MISMATCH")
            expected_runtime_bindings = tuple(
                (
                    factor.factor_id,
                    factor.formula_sha256,
                    factor.implementation_code_sha256,
                    factor.input_schema_sha256,
                )
                for factor in approved_factor_registry.factors
            )
            actual_runtime_bindings = tuple(
                (
                    binding.factor_id,
                    binding.formula_sha256,
                    binding.implementation_code_sha256,
                    binding.input_schema_sha256,
                )
                for binding in model.runtime_build_manifest.factor_bindings
            )
            if actual_runtime_bindings != expected_runtime_bindings:
                reasons.append("RUNTIME_BUILD_MANIFEST_FACTOR_BINDING_MISMATCH")
            if model.runtime_build_manifest.built_at > model.training_data_cutoff_at:
                reasons.append("RUNTIME_BUILD_MANIFEST_AFTER_TRAINING_CUTOFF")

    if type(experiment_v3_admission_receipt) is not ExperimentV3AdmissionReceiptV1:
        reasons.append("MISSING_TYPED_EXPERIMENT_V3_ADMISSION_RECEIPT")
    else:
        try:
            verify_experiment_v3_diagnostic_binding(
                experiment_v3_admission_receipt,
                as_of=snapshot.decision_at
            )
        except (ValueError, TypeError):
            reasons.append("INVALID_EXPERIMENT_V3_ADMISSION_RECEIPT")
        else:
            receipt_expected = {
                "experiment_spec_sha256": model.experiment_spec_sha256,
                "approved_factor_registry_sha256": model.approved_factor_registry_sha256,
                "model_training_receipt_sha256": model.model_training_receipt_sha256,
                "model_admission_receipt_sha256": model.model_admission_receipt_sha256,
                "model_sha256": model.model_sha256,
                "calibration_receipt_sha256": model.calibration_receipt_sha256,
                "calibration_horizon_sessions": model.prediction_horizon_sessions,
                "model_frozen_at": model.frozen_at,
            }
            if any(
                getattr(experiment_v3_admission_receipt, key) != value
                for key, value in receipt_expected.items()
            ):
                reasons.append("EXPERIMENT_V3_ADMISSION_BINDING_MISMATCH")
            if isinstance(approved_factor_registry, ApprovedFactorRegistryV1) and (
                experiment_v3_admission_receipt.approved_factor_registry_frozen_at
                != approved_factor_registry.frozen_at
            ):
                reasons.append("EXPERIMENT_V3_ADMISSION_REGISTRY_TIME_MISMATCH")
            if experiment_v3_admission_receipt.issued_at < model.frozen_at:
                reasons.append("EXPERIMENT_V3_ADMISSION_BEFORE_MODEL_FREEZE")
            try:
                verify_experiment_v3_admission_receipt(
                    experiment_v3_admission_receipt,
                    as_of=snapshot.decision_at,
                    experiment_spec_sha256=model.experiment_spec_sha256,
                    approved_factor_registry_sha256=model.approved_factor_registry_sha256,
                    approved_factor_registry_frozen_at=(
                        approved_factor_registry.frozen_at
                        if isinstance(approved_factor_registry, ApprovedFactorRegistryV1)
                        else None
                    ),
                    model_training_receipt_sha256=model.model_training_receipt_sha256,
                    model_admission_receipt_sha256=model.model_admission_receipt_sha256,
                    model_sha256=model.model_sha256,
                    model_frozen_at=model.frozen_at,
                    calibration_receipt_sha256=model.calibration_receipt_sha256,
                    calibration_horizon_sessions=model.prediction_horizon_sessions,
                )
            except ExperimentV3AdmissionError:
                reasons.append("FORMAL_EXPERIMENT_V3_ADMISSION_BLOCKED")
    return _ordered_codes(reasons)


def _run_alpha_engine(
    snapshot: ControlledPitSnapshotV2,
    model: FrozenAlphaModelV2,
    approved_factor_registry: ApprovedFactorRegistryV1 | None = None,
    experiment_v3_admission_receipt: ExperimentV3AdmissionReceiptV1 | None = None,
    *,
    diagnostic_only: bool,
) -> AlphaRankingV2:
    """Produce one deterministic full-universe ranking without creating orders."""

    if not isinstance(snapshot, ControlledPitSnapshotV2):
        raise AlphaEngineError("snapshot must be ControlledPitSnapshotV2")
    if not isinstance(model, FrozenAlphaModelV2):
        raise AlphaEngineError("model must be FrozenAlphaModelV2")

    common_reasons = list(_validate_common_snapshot(snapshot))
    if canonical_sha256(model.to_content_dict()) != model.model_sha256:
        common_reasons.append("MODEL_HASH_MISMATCH")
    common_reasons.extend(
        _validate_model_admission_evidence(
            snapshot,
            model,
            approved_factor_registry,
            experiment_v3_admission_receipt,
        )
    )
    decision_session = _cst_session_date(snapshot.decision_at, "decision_at")
    if model.training_window_end >= decision_session:
        common_reasons.append("MODEL_TRAINING_WINDOW_NOT_PRIOR_TO_DECISION")
    if model.training_data_cutoff_at > snapshot.decision_at:
        common_reasons.append("FUTURE_MODEL_TRAINING_DATA")
    if model.trained_at > snapshot.decision_at or model.frozen_at > snapshot.decision_at:
        common_reasons.append("MODEL_NOT_FROZEN_AT_DECISION")
    if model.calibration_artifact.fitted_at > snapshot.decision_at:
        common_reasons.append("MODEL_CALIBRATION_NOT_FITTED_AT_DECISION")
    common_codes = _ordered_codes(common_reasons)
    if diagnostic_only and common_codes == ("FORMAL_EXPERIMENT_V3_ADMISSION_BLOCKED",):
        common_codes = ()
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
        raw_prediction, raw_quality_score, raw_timing_score = submodel.score(features)
        predicted, quality_score, timing_score = model.calibration_artifact.calibrate(
            submodel.submodel_id,
            raw_prediction,
            raw_quality_score,
            raw_timing_score,
        )
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
        status=(
            AlphaRunStatus.DIAGNOSTIC_ONLY_NOT_ADMITTED
            if diagnostic_only
            else (AlphaRunStatus.OK if eligible_rows else AlphaRunStatus.NO_ALPHA_CASH)
        ),
        decision_at=snapshot.decision_at,
        rows=rows,
        model_sha256=model.model_sha256,
        input_snapshot_sha256=snapshot.input_snapshot_sha256,
    )


def run_alpha_engine(
    snapshot: ControlledPitSnapshotV2,
    model: FrozenAlphaModelV2,
    approved_factor_registry: ApprovedFactorRegistryV1 | None = None,
    experiment_v3_admission_receipt: ExperimentV3AdmissionReceiptV1 | None = None,
) -> AlphaRankingV2:
    """Formal signal entry; blocked admission always returns fail-closed."""

    return _run_alpha_engine(
        snapshot,
        model,
        approved_factor_registry,
        experiment_v3_admission_receipt,
        diagnostic_only=False,
    )


def run_alpha_engine_diagnostic(
    snapshot: ControlledPitSnapshotV2,
    model: FrozenAlphaModelV2,
    approved_factor_registry: ApprovedFactorRegistryV1,
    experiment_v3_admission_receipt: ExperimentV3AdmissionReceiptV1,
) -> AlphaRankingV2:
    """Research diagnostic scoring; output is explicitly never admitted."""

    return _run_alpha_engine(
        snapshot,
        model,
        approved_factor_registry,
        experiment_v3_admission_receipt,
        diagnostic_only=True,
    )


run_alpha_engine_v2 = run_alpha_engine


__all__ = [
    "ALPHA_MODEL_ADMISSION_RECEIPT_SCHEMA_VERSION",
    "ALPHA_MODEL_TRAINING_RECEIPT_SCHEMA_VERSION",
    "ALPHA_RUNTIME_BUILD_MANIFEST_SCHEMA_VERSION",
    "ALPHA_RANKING_SCHEMA_VERSION",
    "CONTROLLED_PIT_SNAPSHOT_SCHEMA_VERSION",
    "FAST_FACTOR_IDS",
    "FINANCIAL_FEATURE_IDS",
    "FROZEN_ALPHA_CALIBRATION_SCHEMA_VERSION",
    "FROZEN_ALPHA_MODEL_SCHEMA_VERSION",
    "MIN_PRICE_SESSIONS",
    "NON_FINANCIAL_FEATURE_IDS",
    "AlphaEngineError",
    "AlphaFactorRuntimeBindingV1",
    "AlphaModelAdmissionReceiptV1",
    "AlphaModelTrainingReceiptV1",
    "AlphaRuntimeBuildManifestV1",
    "AlphaPredictionRowV2",
    "AlphaRankingV2",
    "AlphaRunStatus",
    "ControlledPitInstrumentV2",
    "ControlledPitSnapshotV2",
    "ControlledPriceBarV2",
    "FrozenAlphaCalibrationV1",
    "FrozenAlphaModelV2",
    "FrozenLinearSubmodelV2",
    "compute_alpha_model_candidate_sha256",
    "compute_alpha_runtime_code_sha256",
    "compute_alpha_submodel_bundle_sha256",
    "run_alpha_engine",
    "run_alpha_engine_diagnostic",
    "run_alpha_engine_v2",
]
