"""Pure in-memory Alpha Feasibility backtest for the frozen technical signal.

This module is intentionally narrower than the formal small-account engine:
it has no file, network, broker, order, Paper, whole-lot, ST, price-limit, or
delisting-terminal-value capability.  A decision made at session ``D`` is
applied to the following controlled session with fractional weights.  The
only supported partitions are the pre-registered development and validation
windows; that check and the input coverage metadata check both happen before
any row iterable is touched.

The Alpha ranking is not reimplemented here.  It calls the frozen formal
ranker, while Exposure calls ``compute_technical_shadow_exposure`` with the
module-level ``DEFAULT_POLICY`` object from Technical Shadow V1.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, fields, is_dataclass
from datetime import date
from decimal import Decimal, localcontext
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from . import technical_exposure_shadow_v1 as _exposure_module
from . import technical_formal_backtest as _formal_ranker_module
from .technical_exposure_shadow_v1 import (
    DEFAULT_POLICY as FROZEN_EXPOSURE_POLICY,
    compute_technical_shadow_exposure,
)
from .technical_formal_backtest import (
    ALPHA_LOOKBACK_SESSIONS,
    ENTRY_PERCENTILE,
    ENTRY_SCORE_EXCLUSIVE,
    FACTOR_DIRECTIONS,
    FACTOR_IDS,
    HOLD_PERCENTILE,
    HOLD_SCORE_EXCLUSIVE,
    TechnicalRankRow,
    WINSOR_LOWER,
    WINSOR_UPPER,
    rank_technical_formal_universe,
)


ENGINE_VERSION = "alpha-feasibility.v1"
EXPERIMENT_ID = "a-share-technical-alpha-feasibility-tushare-p1-v1"
RESEARCH_SCOPE = "research_alpha_feasibility_only"
EXECUTION_REALISM = "INCOMPLETE"
LOCKED_TEST_CONSUMED = False
MINIMUM_COMMISSION_MODELED = False
COST_MODEL_SEMANTICS = (
    "fractional_normalized_nav_proportional_costs;"
    "minimum_5_cny_commission_not_modeled"
)

SIGNAL_WARMUP_START = date(2017, 7, 1)
LATEST_ALLOWED_DATE = date(2023, 12, 31)
SPLIT_WINDOWS: Mapping[str, tuple[date, date]] = {
    "development": (date(2018, 1, 1), date(2022, 12, 31)),
    "validation": (date(2023, 1, 1), date(2023, 12, 31)),
}
SPLIT_BOUNDARY_SESSIONS: Mapping[str, tuple[date, date]] = {
    "development": (date(2018, 1, 2), date(2022, 12, 30)),
    "validation": (date(2023, 1, 3), date(2023, 12, 29)),
}
FROZEN_RUNTIME_SOURCE_SHA256: Mapping[str, tuple[object, str]] = {
    "technical_formal_backtest": (
        _formal_ranker_module,
        "33cd919f8928a532caa798341c2656d423e68655befff6792aa4223a281a31d3",
    ),
    "technical_exposure_shadow_v1": (
        _exposure_module,
        "4a204237752dec4797c2f80cf5950d638aa4d638f2ece615a29ace62f14d0ca7",
    ),
}

MAX_POSITIONS = 3
MAX_POSITION_WEIGHT = Decimal("0.40")
ZERO = Decimal("0")
ONE = Decimal("1")
ANNUALIZATION_SESSIONS = Decimal("252")
_EPSILON = Decimal("1e-24")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PIT_WEIGHT_PATTERN = re.compile(
    r"^(?:0(?:\.[0-9]{1,999})?|[1-9][0-9]{0,999}(?:\.[0-9]{1,999})?)$"
)
_PIT_EVIDENCE_DECIMAL_PATTERN = re.compile(
    r"^(?:0(?:\.[0-9]{1,1000})?|[1-9][0-9]{0,1003}(?:\.[0-9]{1,1000})?)$"
)
_P15_PIT_HARD_MIN = Decimal("99.5")
_P15_PIT_HARD_MAX = Decimal("100.5")
_P15_PIT_WARNING_MIN = Decimal("99.95")
_P15_PIT_WARNING_MAX = Decimal("100.05")
_P15_PIT_POLICY_FIELDS: Mapping[str, str] = {
    "weight_sum_hard_min": "99.5",
    "weight_sum_hard_max": "100.5",
    "weight_sum_warning_min": "99.95",
    "weight_sum_warning_max": "100.05",
}
MINIMUM_VALID_CONTROLLED_SESSIONS = ALPHA_LOOKBACK_SESSIONS + 1
INELIGIBLE_INSUFFICIENT_HISTORY = "ineligible_insufficient_history"
INELIGIBLE_NO_INITIAL_PRICE = "ineligible_no_initial_price"


class AlphaFeasibilityError(ValueError):
    """Base class for a fail-closed feasibility error."""


class AlphaFeasibilityDataError(AlphaFeasibilityError):
    """Raised before producing a result when controlled input is incomplete."""


class LockedTestAccessForbidden(AlphaFeasibilityError):
    """Raised before input iteration for any non-development/validation split."""


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:  # pragma: no cover - defensive input boundary
        raise AlphaFeasibilityDataError(
            f"{field} must be decimal-compatible"
        ) from exc
    if not result.is_finite():
        raise AlphaFeasibilityDataError(f"{field} must be finite")
    return result


def _exact_decimal_sum(values: Sequence[Decimal]) -> Decimal:
    """Sum PIT weights exactly within their bounded decimal representation."""

    numbers = tuple(values)
    if not numbers:
        return ZERO
    minimum_exponent = min(number.as_tuple().exponent for number in numbers)
    maximum_adjusted = max(number.adjusted() for number in numbers)
    aligned_digits = maximum_adjusted - minimum_exponent + 1
    carry_digits = len(str(len(numbers)))
    with localcontext() as context:
        context.prec = max(28, aligned_digits + carry_digits)
        return sum(numbers, ZERO)


def _verify_p15_snapshot_policy(
    value: Mapping[str, Any],
    *,
    total_weight: Decimal,
    component_count: Any,
    actual_zero_weight_count: int | None = None,
) -> None:
    """Verify the fixed P1.5 hard/warning policy without scale inference."""

    declared_zero_count = value.get("zero_weight_count")
    expected_warnings = (
        []
        if _P15_PIT_WARNING_MIN <= total_weight <= _P15_PIT_WARNING_MAX
        else ["weight_sum_outside_warning_range"]
    )
    if (
        component_count != 800
        or value.get("weight_tolerance") != "0.5"
        or value.get("component_count_adjustment_evidence") is not None
        or any(
            value.get(field) != expected
            for field, expected in _P15_PIT_POLICY_FIELDS.items()
        )
        or type(declared_zero_count) is not int
        or not 0 <= declared_zero_count <= 800
        or (
            actual_zero_weight_count is not None
            and declared_zero_count != actual_zero_weight_count
        )
        or value.get("warnings") != expected_warnings
        or not _P15_PIT_HARD_MIN <= total_weight <= _P15_PIT_HARD_MAX
    ):
        raise AlphaFeasibilityDataError("P1.5 PIT snapshot policy mismatch")


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if type(value) is date:
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class LockedTestStatus:
    access: str = "NOT_ACCESSED"
    download: str = "NOT_DOWNLOADED"
    run: str = "NOT_RUN"

    def to_dict(self) -> dict[str, str]:
        return _jsonable(self)


LOCKED_TEST_STATUS = LockedTestStatus()


def _verify_frozen_runtime_sources() -> None:
    """Bind the actually executed ranker and Exposure code to frozen bytes."""

    for label, (module, expected_sha256) in FROZEN_RUNTIME_SOURCE_SHA256.items():
        source_path = getattr(module, "__file__", None)
        if type(source_path) is not str or not source_path.endswith(".py"):
            raise AlphaFeasibilityDataError(f"frozen runtime source unavailable:{label}")
        try:
            actual_sha256 = sha256(Path(source_path).read_bytes()).hexdigest()
        except OSError as exc:
            raise AlphaFeasibilityDataError(
                f"frozen runtime source unavailable:{label}"
            ) from exc
        if actual_sha256 != expected_sha256:
            raise AlphaFeasibilityDataError(f"frozen runtime source drift:{label}")


@dataclass(frozen=True, slots=True)
class SignalBar:
    """Causal total-return-index close/high for one stock and session."""

    trading_date: date
    instrument_id: str
    close: Decimal
    high: Decimal
    open: Decimal | None = None

    def __post_init__(self) -> None:
        if type(self.trading_date) is not date:
            raise AlphaFeasibilityDataError("signal trading_date must be an exact date")
        instrument_id = str(self.instrument_id).strip().upper()
        close = _decimal(self.close, "signal close")
        high = _decimal(self.high, "signal high")
        open_value = close if self.open is None else _decimal(self.open, "signal open")
        if (
            not instrument_id
            or close <= ZERO
            or open_value <= ZERO
            or high < max(close, open_value)
        ):
            raise AlphaFeasibilityDataError("signal identity or total-return values are invalid")
        object.__setattr__(self, "instrument_id", instrument_id)
        object.__setattr__(self, "close", close)
        object.__setattr__(self, "high", high)
        object.__setattr__(self, "open", open_value)

    @property
    def cumulative_total_return_index(self) -> Decimal:
        """Compatibility with the frozen formal ranker's signal contract."""

        return self.close


@dataclass(frozen=True, slots=True)
class BenchmarkBar:
    """Causal total-return-index close/high for CSI 800."""

    trading_date: date
    close: Decimal
    high: Decimal

    def __post_init__(self) -> None:
        if type(self.trading_date) is not date:
            raise AlphaFeasibilityDataError("benchmark trading_date must be an exact date")
        close = _decimal(self.close, "benchmark close")
        high = _decimal(self.high, "benchmark high")
        if close <= ZERO or high < close:
            raise AlphaFeasibilityDataError("benchmark total-return values are invalid")
        object.__setattr__(self, "close", close)
        object.__setattr__(self, "high", high)


@dataclass(frozen=True, slots=True)
class PITMembershipSnapshot:
    """One already-admitted CSI 800 membership snapshot."""

    snapshot_date: date
    members: Sequence[str]

    def __post_init__(self) -> None:
        if type(self.snapshot_date) is not date:
            raise AlphaFeasibilityDataError("snapshot_date must be an exact date")
        members = tuple(str(item).strip().upper() for item in self.members)
        if not members or any(not item for item in members) or len(members) != len(set(members)):
            raise AlphaFeasibilityDataError("PIT members must be non-empty and unique")
        object.__setattr__(self, "members", members)


@dataclass(frozen=True, slots=True)
class PITAdmissionArtifacts:
    """Self-hashed PIT coverage and membership evidence from the data gate."""

    coverage_report: Mapping[str, Any]
    manifest: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class SuspensionRecord:
    trading_date: date
    instrument_id: str

    def __post_init__(self) -> None:
        if type(self.trading_date) is not date:
            raise AlphaFeasibilityDataError("suspension trading_date must be an exact date")
        instrument_id = str(self.instrument_id).strip().upper()
        if not instrument_id:
            raise AlphaFeasibilityDataError("suspension instrument_id is required")
        object.__setattr__(self, "instrument_id", instrument_id)


@dataclass(frozen=True, slots=True)
class AlphaFeasibilityInput:
    """Lazy, metadata-first in-memory input.

    ``__post_init__`` deliberately does not iterate any row collection.  The
    runner first rejects an unsafe split and unsafe coverage metadata, then
    materializes and validates all rows at the engine boundary.
    """

    coverage_start: date
    coverage_end: date
    trading_dates: Iterable[date]
    memberships: Iterable[PITMembershipSnapshot]
    stock_signal_bars: Iterable[SignalBar]
    benchmark_signal_bars: Iterable[BenchmarkBar]
    suspensions: Iterable[SuspensionRecord]
    benchmark_id: str = "000906.SH"
    pit_admission: PITAdmissionArtifacts | None = None


@dataclass(frozen=True, slots=True)
class HistoryEligibilityRecord:
    """One causal history-eligibility conclusion for a PIT member/decision."""

    decision_date: date
    instrument_id: str
    eligibility: bool
    reason: str | None

    def __post_init__(self) -> None:
        if type(self.decision_date) is not date:
            raise AlphaFeasibilityDataError(
                "history eligibility decision_date must be an exact date"
            )
        instrument_id = str(self.instrument_id).strip().upper()
        if not instrument_id or type(self.eligibility) is not bool:
            raise AlphaFeasibilityDataError("history eligibility identity is invalid")
        allowed_reasons = {
            INELIGIBLE_INSUFFICIENT_HISTORY,
            INELIGIBLE_NO_INITIAL_PRICE,
        }
        if self.eligibility:
            if self.reason is not None:
                raise AlphaFeasibilityDataError(
                    "eligible history record cannot carry an ineligible reason"
                )
        elif self.reason not in allowed_reasons:
            raise AlphaFeasibilityDataError(
                "ineligible history record requires a controlled reason"
            )
        object.__setattr__(self, "instrument_id", instrument_id)


def _canonical_evidence_sha256(value: Mapping[str, Any]) -> str:
    raw = (
        json.dumps(
            _jsonable(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    return sha256(raw).hexdigest()


def _pit_months() -> tuple[str, ...]:
    result: list[str] = []
    year, month = 2017, 12
    while (year, month) <= (2023, 12):
        result.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year += 1
            month = 1
    return tuple(result)


def _unsigned_self_hash(
    artifact: Mapping[str, Any], *, field: str, label: str
) -> dict[str, Any]:
    candidate = dict(artifact)
    claimed = candidate.pop(field, None)
    if (
        type(claimed) is not str
        or _SHA256_PATTERN.fullmatch(claimed) is None
        or claimed != _canonical_evidence_sha256(candidate)
    ):
        raise AlphaFeasibilityDataError(f"{label} self-hash verification failed")
    return candidate


def _verify_pit_admission(
    inputs: AlphaFeasibilityInput,
    memberships: Sequence[PITMembershipSnapshot],
) -> None:
    admission = inputs.pit_admission
    if type(admission) is not PITAdmissionArtifacts:
        raise AlphaFeasibilityDataError("verified PIT admission artifacts are required")
    if not isinstance(admission.coverage_report, Mapping) or not isinstance(
        admission.manifest, Mapping
    ):
        raise AlphaFeasibilityDataError("PIT admission artifacts must be mappings")
    report = dict(admission.coverage_report)
    manifest = dict(admission.manifest)
    _unsigned_self_hash(report, field="report_sha256", label="PIT coverage report")
    _unsigned_self_hash(manifest, field="manifest_sha256", label="PIT manifest")

    schema_pair = (
        report.get("schema_version"),
        manifest.get("schema_version"),
    )
    if schema_pair == (
        "pit-membership-coverage-report.v2",
        "pit-membership-manifest.v2",
    ):
        p15 = False
    elif schema_pair == (
        "pit-membership-coverage-report.v3",
        "pit-membership-manifest.v3",
    ):
        p15 = True
    else:
        raise AlphaFeasibilityDataError("PIT admission status is not complete")

    fixed = (
        report.get("experiment_id") == EXPERIMENT_ID
        and manifest.get("experiment_id") == EXPERIMENT_ID
        and report.get("index_code") == "000906.SH"
        and manifest.get("index_code") == "000906.SH"
        and report.get("pit_months_expected") == 73
        and report.get("pit_months_observed") == 73
        and manifest.get("pit_months_expected") == 73
        and manifest.get("pit_months_observed") == 73
        and report.get("stage_status") == "PIT_MEMBERSHIP_READY"
        and report.get("terminal_status") is None
        and manifest.get("stage_status") == "PIT_MEMBERSHIP_READY"
        and report.get("remaining_blockers") == []
        and manifest.get("remaining_blockers") == []
        and report.get("locked_test_status") == LOCKED_TEST_STATUS.to_dict()
        and manifest.get("locked_test_status") == LOCKED_TEST_STATUS.to_dict()
        and report.get("locked_test_consumed") is False
        and manifest.get("locked_test_consumed") is False
        and manifest.get("coverage_start_month") == "2017-12"
        and manifest.get("coverage_end_month") == "2023-12"
    )
    if not fixed:
        raise AlphaFeasibilityDataError("PIT admission status is not complete")

    checks = report.get("monthly_checks")
    snapshots = manifest.get("snapshots")
    union_ids = manifest.get("union_instrument_ids")
    if (
        not isinstance(checks, list)
        or not isinstance(snapshots, list)
        or not isinstance(union_ids, list)
        or len(checks) != 73
        or len(snapshots) < 73
        or len(memberships) != len(snapshots)
    ):
        raise AlphaFeasibilityDataError(
            "PIT admission does not contain every snapshot for 73 months"
        )
    expected_months = _pit_months()
    if tuple(item.get("month") for item in checks if isinstance(item, Mapping)) != expected_months:
        raise AlphaFeasibilityDataError("PIT coverage months are not exact or ordered")

    checks_by_month: dict[str, Mapping[str, Any]] = {}
    unmatched_valid_checks: dict[str, dict[str, Mapping[str, Any]]] = {}
    for month, check in zip(expected_months, checks, strict=True):
        if not isinstance(check, Mapping):
            raise AlphaFeasibilityDataError("PIT monthly evidence must be mappings")
        if (
            check.get("status") != "complete"
            or check.get("issues") != []
            or type(check.get("request_artifact_sha256")) is not str
            or _SHA256_PATTERN.fullmatch(check["request_artifact_sha256"]) is None
            or type(check.get("response_sha256")) is not str
            or _SHA256_PATTERN.fullmatch(check["response_sha256"]) is None
        ):
            raise AlphaFeasibilityDataError("PIT monthly request evidence is incomplete")
        coverage_snapshots = check.get("snapshots")
        if not isinstance(coverage_snapshots, list) or not coverage_snapshots:
            raise AlphaFeasibilityDataError("PIT monthly coverage snapshots are missing")
        previous_coverage_date: date | None = None
        valid_by_date: dict[str, Mapping[str, Any]] = {}
        for coverage_snapshot in coverage_snapshots:
            if not isinstance(coverage_snapshot, Mapping):
                raise AlphaFeasibilityDataError("PIT coverage snapshot must be a mapping")
            coverage_date_text = coverage_snapshot.get("snapshot_date")
            try:
                coverage_date = date.fromisoformat(str(coverage_date_text))
            except ValueError as exc:
                raise AlphaFeasibilityDataError("PIT coverage snapshot date is invalid") from exc
            if (
                coverage_date.strftime("%Y-%m") != month
                or (
                    previous_coverage_date is not None
                    and coverage_date <= previous_coverage_date
                )
            ):
                raise AlphaFeasibilityDataError(
                    "PIT coverage snapshots are not causal, ordered, and unique"
                )
            previous_coverage_date = coverage_date
            valid = coverage_snapshot.get("valid")
            issues = coverage_snapshot.get("issues")
            if type(valid) is not bool or not isinstance(issues, list):
                raise AlphaFeasibilityDataError("PIT coverage snapshot status is invalid")
            if p15:
                declared_sum_text = coverage_snapshot.get("weight_sum")
                if (
                    type(declared_sum_text) is not str
                    or len(declared_sum_text) > 2004
                    or _PIT_EVIDENCE_DECIMAL_PATTERN.fullmatch(declared_sum_text) is None
                    or valid is not True
                    or issues != []
                ):
                    raise AlphaFeasibilityDataError(
                        "P1.5 PIT coverage snapshot is not fully admitted"
                    )
                _verify_p15_snapshot_policy(
                    coverage_snapshot,
                    total_weight=_decimal(declared_sum_text, "PIT weight sum"),
                    component_count=coverage_snapshot.get("component_count"),
                )
            if valid:
                if issues != []:
                    raise AlphaFeasibilityDataError(
                        "valid PIT coverage snapshot contains issues"
                    )
                valid_by_date[str(coverage_date_text)] = coverage_snapshot
            elif not issues:
                raise AlphaFeasibilityDataError(
                    "invalid PIT coverage snapshot lacks an issue"
                )
        if not valid_by_date or check.get("selected_snapshot_date") != next(
            reversed(valid_by_date)
        ):
            raise AlphaFeasibilityDataError(
                "PIT monthly selected snapshot is not the latest legal snapshot"
            )
        checks_by_month[month] = check
        unmatched_valid_checks[month] = valid_by_date

    observed_union: set[str] = set()
    previous_snapshot_date: date | None = None
    observed_month_counts = {month: 0 for month in expected_months}
    for snapshot, supplied in zip(snapshots, memberships, strict=True):
        if not isinstance(snapshot, Mapping):
            raise AlphaFeasibilityDataError("PIT manifest snapshot must be a mapping")
        snapshot_date_text = snapshot.get("snapshot_date")
        try:
            snapshot_date = date.fromisoformat(str(snapshot_date_text))
        except ValueError as exc:
            raise AlphaFeasibilityDataError("PIT snapshot date is invalid") from exc
        month = snapshot.get("month")
        if (
            type(month) is not str
            or month not in checks_by_month
            or snapshot_date.strftime("%Y-%m") != month
            or supplied.snapshot_date != snapshot_date
            or (previous_snapshot_date is not None and snapshot_date <= previous_snapshot_date)
        ):
            raise AlphaFeasibilityDataError(
                "PIT manifest snapshots are not causal, ordered, and unique"
            )
        previous_snapshot_date = snapshot_date
        observed_month_counts[month] += 1
        check = checks_by_month[month]

        members = snapshot.get("members")
        if not isinstance(members, list) or not members:
            raise AlphaFeasibilityDataError("PIT snapshot members are missing")
        identifiers: list[str] = []
        weights: list[Decimal] = []
        coarsest_nonzero_places: int | None = None
        for member in members:
            if not isinstance(member, Mapping):
                raise AlphaFeasibilityDataError("PIT member evidence must be a mapping")
            identifier = str(member.get("instrument_id", "")).strip().upper()
            weight_text = member.get("weight")
            if not re.fullmatch(r"[0-9]{6}\.(?:SH|SZ)", identifier):
                raise AlphaFeasibilityDataError("PIT member code is invalid")
            if (
                type(weight_text) is not str
                or _PIT_WEIGHT_PATTERN.fullmatch(weight_text) is None
            ):
                raise AlphaFeasibilityDataError("PIT member weight format is invalid")
            weight = _decimal(weight_text, "PIT member weight")
            if (
                weight < ZERO
                or weight.as_tuple().exponent < -999
                or weight.adjusted() > 999
                or weight_text != format(weight, "f")
            ):
                raise AlphaFeasibilityDataError("PIT member weight is negative")
            identifiers.append(identifier)
            weights.append(weight)
            if weight != ZERO:
                places = max(0, -weight.as_tuple().exponent)
                coarsest_nonzero_places = (
                    places
                    if coarsest_nonzero_places is None
                    else min(coarsest_nonzero_places, places)
                )
        total_weight = _exact_decimal_sum(weights)
        if len(identifiers) != len(set(identifiers)) or tuple(identifiers) != tuple(
            supplied.members
        ):
            raise AlphaFeasibilityDataError("PIT manifest and engine membership differ")
        tolerance_text = snapshot.get("weight_tolerance")
        declared_sum_text = snapshot.get("weight_sum")
        if (
            type(tolerance_text) is not str
            or len(tolerance_text) > 2004
            or _PIT_EVIDENCE_DECIMAL_PATTERN.fullmatch(tolerance_text) is None
            or type(declared_sum_text) is not str
            or len(declared_sum_text) > 2004
            or _PIT_EVIDENCE_DECIMAL_PATTERN.fullmatch(declared_sum_text) is None
        ):
            raise AlphaFeasibilityDataError("PIT manifest weight check failed")
        tolerance = _decimal(tolerance_text, "PIT weight tolerance")
        declared_sum = _decimal(declared_sum_text, "PIT weight sum")
        if p15:
            _verify_p15_snapshot_policy(
                snapshot,
                total_weight=total_weight,
                component_count=len(identifiers),
                actual_zero_weight_count=sum(weight == ZERO for weight in weights),
            )
            if tolerance != Decimal("0.5") or declared_sum != total_weight:
                raise AlphaFeasibilityDataError("PIT manifest weight check failed")
        else:
            expected_tolerance = (
                ZERO
                if coarsest_nonzero_places is None
                else Decimal("0.5") * (Decimal(10) ** (-coarsest_nonzero_places))
            )
            weight_sum_difference = abs(
                _exact_decimal_sum((total_weight, Decimal("-100")))
            )
            if (
                tolerance < ZERO
                or tolerance != expected_tolerance
                or declared_sum != total_weight
                or weight_sum_difference > tolerance
            ):
                raise AlphaFeasibilityDataError("PIT manifest weight check failed")
        adjustment_reason = snapshot.get("component_count_adjustment_evidence")
        if not p15 and len(identifiers) != 800 and (
            type(adjustment_reason) is not str or not adjustment_reason.strip()
        ):
            raise AlphaFeasibilityDataError(
                "non-800 PIT snapshot lacks controlled adjustment evidence"
            )
        source_response_sha256 = snapshot.get("source_response_sha256")
        if (
            type(source_response_sha256) is not str
            or _SHA256_PATTERN.fullmatch(source_response_sha256) is None
            or source_response_sha256 != check.get("response_sha256")
        ):
            raise AlphaFeasibilityDataError(
                "PIT manifest snapshot response evidence does not reconcile"
            )
        selected_check = unmatched_valid_checks[month].pop(
            str(snapshot_date_text), None
        )
        if (
            selected_check is None
            or selected_check.get("valid") is not True
            or selected_check.get("component_count") != len(identifiers)
            or selected_check.get("weight_sum") != snapshot.get("weight_sum")
            or selected_check.get("weight_tolerance") != snapshot.get("weight_tolerance")
            or selected_check.get("component_count_adjustment_evidence") != adjustment_reason
            or p15
            and any(
                selected_check.get(field) != snapshot.get(field)
                for field in (
                    "zero_weight_count",
                    "weight_sum_hard_min",
                    "weight_sum_hard_max",
                    "weight_sum_warning_min",
                    "weight_sum_warning_max",
                    "warnings",
                )
            )
        ):
            raise AlphaFeasibilityDataError("PIT coverage and manifest snapshot differ")
        observed_union.update(identifiers)

    if any(count == 0 for count in observed_month_counts.values()) or any(
        by_date for by_date in unmatched_valid_checks.values()
    ):
        raise AlphaFeasibilityDataError(
            "PIT coverage and manifest snapshots do not reconcile one-to-one"
        )

    expected_union = sorted(observed_union)
    if (
        union_ids != expected_union
        or manifest.get("union_instrument_count") != len(expected_union)
    ):
        raise AlphaFeasibilityDataError("PIT manifest union does not reconcile")
    if p15:
        snapshot_dates = [str(snapshot["snapshot_date"]) for snapshot in snapshots]
        zero_weight_counts = {
            str(snapshot["snapshot_date"]): snapshot["zero_weight_count"]
            for snapshot in snapshots
        }
        weight_sums = {
            str(snapshot["snapshot_date"]): snapshot["weight_sum"]
            for snapshot in snapshots
        }
        p15_summary = {
            "pit_snapshot_count": len(snapshots),
            "snapshot_dates": snapshot_dates,
            "missing_months": [],
            "duplicate_member_count": 0,
            "zero_weight_count_by_snapshot": zero_weight_counts,
            "weight_sum_by_snapshot": weight_sums,
        }
        if any(
            report.get(field) != expected or manifest.get(field) != expected
            for field, expected in p15_summary.items()
        ):
            raise AlphaFeasibilityDataError("P1.5 PIT summary does not reconcile")


@dataclass(frozen=True, slots=True)
class ProportionalCostScenario:
    name: str
    commission_rate: Decimal = Decimal("0.00018")
    sell_tax_rate: Decimal = Decimal("0.0005")
    transfer_fee_rate: Decimal = Decimal("0.00001")
    slippage_bps_one_way: Decimal = Decimal("10")
    commission_multiplier: Decimal = ONE

    def __post_init__(self) -> None:
        if self.name not in {"base", "stress"}:
            raise AlphaFeasibilityDataError("cost scenario must be base or stress")
        for field_name in (
            "commission_rate",
            "sell_tax_rate",
            "transfer_fee_rate",
            "slippage_bps_one_way",
            "commission_multiplier",
        ):
            value = _decimal(getattr(self, field_name), field_name)
            if value < ZERO:
                raise AlphaFeasibilityDataError(f"{field_name} must be non-negative")
            object.__setattr__(self, field_name, value)
        if self.commission_multiplier < ONE:
            raise AlphaFeasibilityDataError("commission_multiplier must be >= 1")
        expected = {
            "base": (
                Decimal("0.00018"),
                Decimal("0.0005"),
                Decimal("0.00001"),
                Decimal("10"),
                Decimal("1"),
            ),
            "stress": (
                Decimal("0.00018"),
                Decimal("0.0005"),
                Decimal("0.00001"),
                Decimal("20"),
                Decimal("2"),
            ),
        }[self.name]
        actual = (
            self.commission_rate,
            self.sell_tax_rate,
            self.transfer_fee_rate,
            self.slippage_bps_one_way,
            self.commission_multiplier,
        )
        if actual != expected:
            raise AlphaFeasibilityDataError("proportional cost scenarios are frozen")

    @property
    def buy_rate(self) -> Decimal:
        return (
            self.commission_rate * self.commission_multiplier
            + self.transfer_fee_rate
            + self.slippage_bps_one_way / Decimal("10000")
        )

    @property
    def sell_rate(self) -> Decimal:
        return self.buy_rate + self.sell_tax_rate


BASE_COST = ProportionalCostScenario("base")
STRESS_COST = ProportionalCostScenario(
    "stress",
    slippage_bps_one_way=Decimal("20"),
    commission_multiplier=Decimal("2"),
)


@dataclass(frozen=True, slots=True)
class AlphaFeasibilityDecision:
    decision_date: date
    execution_date: date
    selected_instrument_ids: tuple[str, ...]
    target_weights: Mapping[str, Decimal]
    market_state: str
    target_gross_exposure: Decimal
    realized_target_weight: Decimal
    eligible_count: int
    entry_count: int


@dataclass(frozen=True, slots=True)
class AlphaFeasibilityRebalance:
    decision_date: date
    execution_date: date
    prior_weights: Mapping[str, Decimal]
    target_weights: Mapping[str, Decimal]
    absolute_turnover: Decimal
    total_cost: Decimal
    cost_by_instrument: Mapping[str, Decimal]


@dataclass(frozen=True, slots=True)
class AlphaFeasibilityNavPoint:
    trading_date: date
    nav: Decimal
    daily_pnl: Decimal
    daily_return: Decimal
    benchmark_daily_return: Decimal
    gross_exposure: Decimal
    market_state: str
    cumulative_cost: Decimal
    weights: Mapping[str, Decimal]


@dataclass(frozen=True, slots=True)
class PeriodActiveReturn:
    period: str
    net_return: Decimal
    benchmark_return: Decimal
    net_active_return: Decimal


@dataclass(frozen=True, slots=True)
class AlphaFeasibilityMetrics:
    net_return: Decimal
    benchmark_return: Decimal
    net_active_return: Decimal
    max_drawdown: Decimal
    annualized_turnover: Decimal
    total_cost: Decimal
    average_gross_exposure: Decimal
    cash_day_fraction: Decimal
    exposure_state_distribution: Mapping[str, Decimal]
    trade_or_rebalance_count: int
    positive_month_rate: Decimal
    positive_half_year_count: int
    worst_month: PeriodActiveReturn
    per_stock_pnl_contribution: Mapping[str, Decimal]
    largest_stock_pnl_share: Decimal | None
    largest_10_days_pnl_share: Decimal | None

    @property
    def cost_to_gross_profit(self) -> Decimal | None:
        """Return proportional costs divided by pre-cost cumulative PnL.

        The normalized-NAV accounting is additive: ``net_return`` already
        includes every rebalance cost, so pre-cost cumulative PnL is exactly
        ``net_return + total_cost``.  A zero or negative denominator is not an
        economically meaningful cost/profit ratio and is represented as
        ``None`` rather than zero, infinity, or a signed value.
        """

        gross_profit = self.net_return + self.total_cost
        if gross_profit <= ZERO:
            return None
        with localcontext() as context:
            context.prec = 50
            return self.total_cost / gross_profit

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonable(self)
        payload["rebalance_count"] = payload.pop("trade_or_rebalance_count")
        payload["cost_to_gross_profit"] = _jsonable(self.cost_to_gross_profit)
        worst = dict(payload["worst_month"])
        worst["month"] = worst.pop("period")
        payload["worst_month"] = worst
        return payload


@dataclass(frozen=True, slots=True)
class AlphaFeasibilityScenarioResult:
    engine_version: str
    research_scope: str
    scenario: str
    split: str
    start_date: date
    end_date: date
    metrics: AlphaFeasibilityMetrics
    nav: tuple[AlphaFeasibilityNavPoint, ...]
    decisions: tuple[AlphaFeasibilityDecision, ...]
    rebalances: tuple[AlphaFeasibilityRebalance, ...]
    cost_model_semantics: str = COST_MODEL_SEMANTICS
    minimum_commission_modeled: bool = MINIMUM_COMMISSION_MODELED
    execution_realism: str = EXECUTION_REALISM
    trade_eligibility: bool = False
    paper_eligibility: bool = False
    automatic_order_submission: bool = False
    locked_test_status: LockedTestStatus = LOCKED_TEST_STATUS
    locked_test_consumed: bool = LOCKED_TEST_CONSUMED

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonable(self)
        payload["metrics"] = self.metrics.to_dict()
        return payload


@dataclass(frozen=True, slots=True)
class AlphaFeasibilityComparison:
    split: str
    base: AlphaFeasibilityScenarioResult
    stress: AlphaFeasibilityScenarioResult
    execution_realism: str = EXECUTION_REALISM
    trade_eligibility: bool = False
    locked_test_status: LockedTestStatus = LOCKED_TEST_STATUS
    locked_test_consumed: bool = LOCKED_TEST_CONSUMED

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonable(self)
        payload["base"] = self.base.to_dict()
        payload["stress"] = self.stress.to_dict()
        return payload


@dataclass(frozen=True, slots=True)
class AlphaFeasibilityStudy:
    development: AlphaFeasibilityComparison
    validation: AlphaFeasibilityComparison
    execution_realism: str = EXECUTION_REALISM
    trade_eligibility: bool = False
    locked_test_status: LockedTestStatus = LOCKED_TEST_STATUS
    locked_test_consumed: bool = LOCKED_TEST_CONSUMED

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonable(self)
        payload["development"] = self.development.to_dict()
        payload["validation"] = self.validation.to_dict()
        return payload


@dataclass(frozen=True, slots=True)
class _PreparedInput:
    coverage_start: date
    coverage_end: date
    trading_dates: tuple[date, ...]
    memberships: tuple[PITMembershipSnapshot, ...]
    signal_by_key: Mapping[tuple[date, str], SignalBar]
    benchmark_id: str
    suspended: frozenset[tuple[date, str]]
    calendar_position: Mapping[date, int]
    first_signal_position: Mapping[str, int]
    first_membership_date: Mapping[str, date]


@dataclass(frozen=True, slots=True)
class _RankStatus:
    suspended: bool
    is_st: bool = False
    listed: bool = True
    delisted: bool = False


def _split_window(split: str) -> tuple[date, date]:
    """Reject forbidden/unknown splits without touching any input object."""

    if type(split) is not str or split not in SPLIT_WINDOWS:
        raise LockedTestAccessForbidden(
            "only development and validation are supported; locked test remains NOT_ACCESSED"
        )
    return SPLIT_WINDOWS[split]


def _guard_input_metadata(
    inputs: AlphaFeasibilityInput,
    *,
    required_end: date,
) -> None:
    """Validate coverage metadata without iterating any contained collection."""

    if type(inputs) is not AlphaFeasibilityInput:
        raise AlphaFeasibilityDataError("inputs must be AlphaFeasibilityInput")
    if type(inputs.coverage_start) is not date or type(inputs.coverage_end) is not date:
        raise AlphaFeasibilityDataError("input coverage metadata must use exact dates")
    if inputs.coverage_start < SIGNAL_WARMUP_START:
        raise LockedTestAccessForbidden("coverage begins before the authorized warmup boundary")
    if inputs.coverage_end > LATEST_ALLOWED_DATE:
        raise LockedTestAccessForbidden("coverage crosses 2023-12-31; locked data not accessed")
    if inputs.coverage_start > inputs.coverage_end:
        raise AlphaFeasibilityDataError("coverage_start follows coverage_end")
    if inputs.coverage_end < required_end:
        raise AlphaFeasibilityDataError("coverage metadata does not reach the requested split end")
    benchmark_id = str(inputs.benchmark_id).strip().upper()
    if benchmark_id != "000906.SH":
        raise AlphaFeasibilityDataError("benchmark_id must remain frozen at 000906.SH")


def _check_row_date(value: date, label: str, inputs: AlphaFeasibilityInput) -> None:
    if type(value) is not date:
        raise AlphaFeasibilityDataError(f"{label} must use an exact date")
    if value < SIGNAL_WARMUP_START or value > LATEST_ALLOWED_DATE:
        raise LockedTestAccessForbidden(
            f"{label} lies outside 2017-07-01..2023-12-31"
        )
    if value < inputs.coverage_start or value > inputs.coverage_end:
        raise AlphaFeasibilityDataError(f"{label} lies outside declared coverage")


def _prepare(inputs: AlphaFeasibilityInput, *, required_end: date) -> _PreparedInput:
    _guard_input_metadata(inputs, required_end=required_end)
    _verify_frozen_runtime_sources()

    trading_dates: list[date] = []
    for trading_date in inputs.trading_dates:
        _check_row_date(trading_date, "trading calendar date", inputs)
        trading_dates.append(trading_date)
    if not trading_dates or trading_dates != sorted(trading_dates):
        raise AlphaFeasibilityDataError("trading calendar must be non-empty and ordered")
    if len(trading_dates) != len(set(trading_dates)):
        raise AlphaFeasibilityDataError("trading calendar contains duplicates")
    calendar_set = set(trading_dates)

    memberships: list[PITMembershipSnapshot] = []
    union_members: set[str] = set()
    first_membership_date: dict[str, date] = {}
    for snapshot in inputs.memberships:
        if type(snapshot) is not PITMembershipSnapshot:
            raise AlphaFeasibilityDataError("memberships require PITMembershipSnapshot rows")
        _check_row_date(snapshot.snapshot_date, "PIT snapshot_date", inputs)
        memberships.append(snapshot)
        union_members.update(snapshot.members)
        for instrument_id in snapshot.members:
            first_membership_date.setdefault(instrument_id, snapshot.snapshot_date)
    if not memberships:
        raise AlphaFeasibilityDataError("PIT memberships are empty")
    snapshot_dates = [item.snapshot_date for item in memberships]
    if snapshot_dates != sorted(snapshot_dates) or len(snapshot_dates) != len(set(snapshot_dates)):
        raise AlphaFeasibilityDataError("PIT snapshots must be strictly ordered and unique")
    _verify_pit_admission(inputs, memberships)

    stock_by_key: dict[tuple[date, str], SignalBar] = {}
    for bar in inputs.stock_signal_bars:
        if type(bar) is not SignalBar:
            raise AlphaFeasibilityDataError("stock_signal_bars require SignalBar rows")
        _check_row_date(bar.trading_date, "stock signal date", inputs)
        if bar.trading_date not in calendar_set:
            raise AlphaFeasibilityDataError("stock signal date is not in the controlled calendar")
        if bar.instrument_id not in union_members:
            raise AlphaFeasibilityDataError(
                f"stock signal is outside the historical PIT union:{bar.instrument_id}"
            )
        key = (bar.trading_date, bar.instrument_id)
        if key in stock_by_key:
            raise AlphaFeasibilityDataError(f"duplicate stock signal:{key}")
        stock_by_key[key] = bar

    benchmark_id = str(inputs.benchmark_id).strip().upper()
    benchmark_by_date: dict[date, BenchmarkBar] = {}
    for bar in inputs.benchmark_signal_bars:
        if type(bar) is not BenchmarkBar:
            raise AlphaFeasibilityDataError("benchmark_signal_bars require BenchmarkBar rows")
        _check_row_date(bar.trading_date, "benchmark signal date", inputs)
        if bar.trading_date not in calendar_set:
            raise AlphaFeasibilityDataError("benchmark signal date is not in the controlled calendar")
        if bar.trading_date in benchmark_by_date:
            raise AlphaFeasibilityDataError(f"duplicate benchmark signal:{bar.trading_date}")
        benchmark_by_date[bar.trading_date] = bar
    missing_benchmark = [item for item in trading_dates if item not in benchmark_by_date]
    if missing_benchmark:
        raise AlphaFeasibilityDataError(
            f"benchmark lacks a controlled session:{missing_benchmark[0].isoformat()}"
        )

    suspended: set[tuple[date, str]] = set()
    for item in inputs.suspensions:
        if type(item) is not SuspensionRecord:
            raise AlphaFeasibilityDataError("suspensions require SuspensionRecord rows")
        _check_row_date(item.trading_date, "suspension date", inputs)
        if item.trading_date not in calendar_set:
            raise AlphaFeasibilityDataError("suspension date is not in the controlled calendar")
        if item.instrument_id not in union_members:
            raise AlphaFeasibilityDataError(
                f"suspension is outside the historical PIT union:{item.instrument_id}"
            )
        key = (item.trading_date, item.instrument_id)
        if key in suspended:
            raise AlphaFeasibilityDataError(f"duplicate suspension record:{key}")
        suspended.add(key)

    # A missing suspension session carries the previous economic close, not a
    # future factor or a future price.  Its synthetic high equals that carried
    # close, so BREAKOUT never sees a made-up intraday extreme.
    by_instrument_dates: dict[str, set[date]] = defaultdict(set)
    for trading_date, instrument_id in stock_by_key:
        by_instrument_dates[instrument_id].add(trading_date)
    for trading_date, instrument_id in suspended:
        by_instrument_dates[instrument_id].add(trading_date)
    for instrument_id, relevant_dates in by_instrument_dates.items():
        last_close: Decimal | None = None
        for trading_date in sorted(relevant_dates):
            key = (trading_date, instrument_id)
            bar = stock_by_key.get(key)
            if key in suspended:
                if last_close is None:
                    # A full-session suspension before the first observable
                    # economic value is an explicit ineligibility conclusion,
                    # never a zero or forward-filled signal value.
                    continue
                stock_by_key[key] = SignalBar(
                    trading_date=trading_date,
                    instrument_id=instrument_id,
                    close=last_close,
                    high=last_close,
                    open=last_close,
                )
            elif bar is not None:
                last_close = bar.close

    # Once an instrument enters its observable listed history, every
    # controlled session through its last observation must contain either a
    # real bar or a same-day suspension-derived carry.  This prevents a warmup
    # gap from silently changing the cross-sectional ranker's eligible set.
    calendar_position = {item: index for index, item in enumerate(trading_dates)}
    first_signal_position: dict[str, int] = {}
    signal_dates_by_instrument: dict[str, list[date]] = defaultdict(list)
    for trading_date, instrument_id in stock_by_key:
        signal_dates_by_instrument[instrument_id].append(trading_date)
    for instrument_id, signal_dates in signal_dates_by_instrument.items():
        ordered_dates = sorted(signal_dates)
        first_index = calendar_position[ordered_dates[0]]
        last_index = calendar_position[ordered_dates[-1]]
        first_signal_position[instrument_id] = first_index
        for trading_date in trading_dates[first_index : last_index + 1]:
            if (trading_date, instrument_id) not in stock_by_key:
                raise AlphaFeasibilityDataError(
                    "non-suspended instrument history has an internal missing session:"
                    f"{trading_date}:{instrument_id}"
                )

    signal_by_key: dict[tuple[date, str], SignalBar] = dict(stock_by_key)
    for trading_date, bar in benchmark_by_date.items():
        signal_by_key[(trading_date, benchmark_id)] = SignalBar(
            trading_date=trading_date,
            instrument_id=benchmark_id,
            close=bar.close,
            high=bar.high,
        )
    return _PreparedInput(
        inputs.coverage_start,
        inputs.coverage_end,
        tuple(trading_dates),
        tuple(memberships),
        signal_by_key,
        benchmark_id,
        frozenset(suspended),
        calendar_position,
        first_signal_position,
        first_membership_date,
    )


def select_pit_membership(
    snapshots: Sequence[PITMembershipSnapshot],
    decision_date: date,
) -> tuple[str, ...]:
    """Return the latest snapshot visible on or before ``decision_date``."""

    if type(decision_date) is not date:
        raise AlphaFeasibilityDataError("decision_date must be an exact date")
    if decision_date < SIGNAL_WARMUP_START or decision_date > LATEST_ALLOWED_DATE:
        raise LockedTestAccessForbidden("PIT decision date crosses the authorized boundary")
    visible = [item for item in snapshots if item.snapshot_date <= decision_date]
    if not visible:
        raise AlphaFeasibilityDataError(
            f"no PIT membership visible by decision date:{decision_date.isoformat()}"
        )
    latest = max(visible, key=lambda item: item.snapshot_date)
    return tuple(latest.members)


def _history_eligibility_records(
    prepared: _PreparedInput,
    *,
    decision_date: date,
    instrument_ids: Sequence[str],
) -> tuple[HistoryEligibilityRecord, ...]:
    """Conclude history eligibility for every PIT member without scoring it."""

    ids = tuple(str(value).strip().upper() for value in instrument_ids)
    if not ids or any(not value for value in ids) or len(ids) != len(set(ids)):
        raise AlphaFeasibilityDataError(
            "history eligibility PIT members must be non-empty and unique"
        )
    decision_position = prepared.calendar_position.get(decision_date)
    if decision_position is None:
        raise AlphaFeasibilityDataError(
            "history eligibility decision is outside the controlled calendar"
        )

    records: list[HistoryEligibilityRecord] = []
    for instrument_id in ids:
        first_membership = prepared.first_membership_date.get(instrument_id)
        if first_membership is None or first_membership > decision_date:
            raise AlphaFeasibilityDataError(
                "PIT member lacks a causal first-membership conclusion:"
                f"{decision_date}:{instrument_id}"
            )
        decision_key = (decision_date, instrument_id)
        first_signal_position = prepared.first_signal_position.get(instrument_id)
        if decision_key not in prepared.signal_by_key:
            no_signal_yet = (
                first_signal_position is None
                or first_signal_position > decision_position
            )
            if (
                no_signal_yet
                and (first_membership, instrument_id) in prepared.suspended
                and decision_key in prepared.suspended
            ):
                records.append(
                    HistoryEligibilityRecord(
                        decision_date,
                        instrument_id,
                        False,
                        INELIGIBLE_NO_INITIAL_PRICE,
                    )
                )
                continue
            if decision_key in prepared.suspended:
                raise AlphaFeasibilityDataError(
                    "suspended PIT member lacks prior economic value:"
                    f"{decision_date}:{instrument_id}"
                )
            raise AlphaFeasibilityDataError(
                "non-suspended PIT member lacks decision-date signal:"
                f"{decision_date}:{instrument_id}"
            )
        if first_signal_position is None or first_signal_position > decision_position:
            raise AlphaFeasibilityDataError(
                "decision-date signal lacks a causal first observation:"
                f"{decision_date}:{instrument_id}"
            )
        valid_session_count = decision_position - first_signal_position + 1
        records.append(
            HistoryEligibilityRecord(
                decision_date,
                instrument_id,
                valid_session_count >= MINIMUM_VALID_CONTROLLED_SESSIONS,
                (
                    None
                    if valid_session_count >= MINIMUM_VALID_CONTROLLED_SESSIONS
                    else INELIGIBLE_INSUFFICIENT_HISTORY
                ),
            )
        )

    if (
        len(records) != len(ids)
        or {item.instrument_id for item in records} != set(ids)
    ):
        raise AlphaFeasibilityDataError(
            "not every PIT member has one history eligibility conclusion"
        )
    return tuple(records)


def rank_alpha_feasibility_universe(
    *,
    decision_date: date,
    sessions: Sequence[date],
    instrument_ids: Sequence[str],
    signal_index: Mapping[tuple[date, str], SignalBar],
    benchmark_id: str,
    suspended: frozenset[tuple[date, str]] | set[tuple[date, str]] = frozenset(),
) -> tuple[TechnicalRankRow, ...]:
    """Call the formal frozen six-factor ranker without changing semantics."""

    if type(decision_date) is not date:
        raise AlphaFeasibilityDataError("decision_date must be an exact date")
    if decision_date < SIGNAL_WARMUP_START or decision_date > LATEST_ALLOWED_DATE:
        raise LockedTestAccessForbidden("ranking decision date crosses the authorized boundary")
    if (
        not sessions
        or any(type(item) is not date for item in sessions)
        or tuple(sessions) != tuple(sorted(sessions))
        or len(sessions) != len(set(sessions))
        or sessions[-1] != decision_date
        or any(item > LATEST_ALLOWED_DATE for item in sessions)
    ):
        raise AlphaFeasibilityDataError("ranking sessions must be causal, ordered, and unique")
    status_index = {
        (decision_date, instrument_id): _RankStatus(
            suspended=(decision_date, instrument_id) in suspended
        )
        for instrument_id in instrument_ids
    }
    return rank_technical_formal_universe(
        decision_date=decision_date,
        sessions=sessions,
        instrument_ids=instrument_ids,
        signal_index=signal_index,  # type: ignore[arg-type]
        benchmark_id=benchmark_id,
        status_index=status_index,  # type: ignore[arg-type]
    )


def _validate_split_coverage(
    prepared: _PreparedInput,
    *,
    split: str,
) -> tuple[date, ...]:
    start, end = SPLIT_WINDOWS[split]
    report_dates = tuple(day for day in prepared.trading_dates if start <= day <= end)
    if not report_dates:
        raise AlphaFeasibilityDataError(f"{split} has no controlled trading sessions")
    expected_first, expected_last = SPLIT_BOUNDARY_SESSIONS[split]
    if report_dates[0] != expected_first or report_dates[-1] != expected_last:
        raise AlphaFeasibilityDataError(
            f"{split} controlled calendar does not cover both frozen boundary sessions"
        )
    first_index = prepared.trading_dates.index(report_dates[0])
    if first_index < ALPHA_LOOKBACK_SESSIONS + 1:
        raise AlphaFeasibilityDataError(
            f"{split} lacks 121 sessions plus the prior decision session"
        )
    checked_dates = (prepared.trading_dates[first_index - 1],) + report_dates
    for checked_date in checked_dates:
        members = select_pit_membership(prepared.memberships, checked_date)
        records = _history_eligibility_records(
            prepared,
            decision_date=checked_date,
            instrument_ids=members,
        )
        if {item.instrument_id for item in records} != set(members):
            raise AlphaFeasibilityDataError(
                "split coverage lacks a history eligibility conclusion"
            )
    return report_dates


def _validate_frozen_cost_scenario(scenario: Any) -> ProportionalCostScenario:
    if type(scenario) is not ProportionalCostScenario:
        raise AlphaFeasibilityDataError(
            "scenario must be an exact frozen ProportionalCostScenario"
        )
    expected = BASE_COST if scenario.name == "base" else STRESS_COST
    if scenario != expected:
        raise AlphaFeasibilityDataError("proportional cost scenarios are frozen")
    return scenario


def _build_decision(
    *,
    prepared: _PreparedInput,
    decision_date: date,
    execution_date: date,
    current_weights: Mapping[str, Decimal],
    current_nav: Decimal,
    peak_nav: Decimal,
) -> AlphaFeasibilityDecision:
    sessions = tuple(day for day in prepared.trading_dates if day <= decision_date)
    members = select_pit_membership(prepared.memberships, decision_date)
    history_records = _history_eligibility_records(
        prepared,
        decision_date=decision_date,
        instrument_ids=members,
    )
    alpha_members = tuple(
        item.instrument_id for item in history_records if item.eligibility
    )
    scored_rows = (
        rank_alpha_feasibility_universe(
            decision_date=decision_date,
            sessions=sessions,
            instrument_ids=alpha_members,
            signal_index=prepared.signal_by_key,
            benchmark_id=prepared.benchmark_id,
            suspended=prepared.suspended,
        )
        if alpha_members
        else ()
    )
    history_ineligible_rows = tuple(
        TechnicalRankRow(
            instrument_id=item.instrument_id,
            factors=None,
            z_scores=None,
            composite_score=None,
            rank=None,
            percentile=None,
            eligibility=False,
            entry_eligible=False,
            hold_eligible=False,
            exclusion_codes=(str(item.reason),),
        )
        for item in history_records
        if not item.eligibility
    )
    ranking = tuple(
        sorted(
            (*scored_rows, *history_ineligible_rows),
            key=lambda row: (
                row.rank is None,
                row.rank if row.rank is not None else 10**9,
                row.instrument_id,
            ),
        )
    )
    if len(ranking) != len(members) or {
        row.instrument_id for row in ranking
    } != set(members):
        raise AlphaFeasibilityDataError(
            "not every PIT member has one final eligibility conclusion"
        )
    required = sessions[-(ALPHA_LOOKBACK_SESSIONS + 1):]
    benchmark_rows = [
        {"close": str(prepared.signal_by_key[(day, prepared.benchmark_id)].close)}
        for day in required
    ]
    eligible_ids = {row.instrument_id for row in ranking if row.eligibility}
    eligible_rows = [
        [
            {"close": str(prepared.signal_by_key[(day, instrument_id)].close)}
            for day in required
        ]
        for instrument_id in members
        if instrument_id in eligible_ids
    ]
    exposure = compute_technical_shadow_exposure(
        benchmark_rows=benchmark_rows,
        eligible_stock_rows=eligible_rows,
        current_nav=float(current_nav),
        peak_nav=float(peak_nav),
        policy=FROZEN_EXPOSURE_POLICY,
    )
    target_exposure = _decimal(exposure["target_gross_exposure"], "target exposure")
    by_id = {row.instrument_id: row for row in ranking}
    incumbents = [
        instrument_id
        for instrument_id, weight in current_weights.items()
        if weight > _EPSILON
        and instrument_id in by_id
        and by_id[instrument_id].hold_eligible
    ]
    incumbents.sort(key=lambda item: (by_id[item].rank or 10**9, item))
    entries = [
        row.instrument_id
        for row in ranking
        if row.entry_eligible and row.instrument_id not in incumbents
    ]
    selected = tuple((incumbents + entries)[:MAX_POSITIONS])
    if target_exposure <= ZERO:
        selected = ()
    target_weights: dict[str, Decimal] = {}
    if selected:
        per_weight = min(
            MAX_POSITION_WEIGHT,
            target_exposure / Decimal(len(selected)),
        )
        target_weights = {instrument_id: per_weight for instrument_id in selected}
    if len(target_weights) > MAX_POSITIONS or any(
        weight > MAX_POSITION_WEIGHT for weight in target_weights.values()
    ):
        raise AlphaFeasibilityError("portfolio construction breached frozen limits")
    return AlphaFeasibilityDecision(
        decision_date=decision_date,
        execution_date=execution_date,
        selected_instrument_ids=selected,
        target_weights=target_weights,
        market_state=str(exposure["market_state"]),
        target_gross_exposure=target_exposure,
        realized_target_weight=sum(target_weights.values(), ZERO),
        eligible_count=len(eligible_ids),
        entry_count=sum(row.entry_eligible for row in ranking),
    )


def _cost_for_rebalance(
    *,
    decision: AlphaFeasibilityDecision,
    prior_weights: Mapping[str, Decimal],
    current_nav: Decimal,
    scenario: ProportionalCostScenario,
) -> AlphaFeasibilityRebalance:
    identifiers = set(prior_weights) | set(decision.target_weights)
    turnover = ZERO
    cost_by_instrument: dict[str, Decimal] = {}
    for instrument_id in sorted(identifiers):
        delta = decision.target_weights.get(instrument_id, ZERO) - prior_weights.get(
            instrument_id, ZERO
        )
        if abs(delta) <= _EPSILON:
            continue
        turnover += abs(delta)
        rate = scenario.buy_rate if delta > ZERO else scenario.sell_rate
        cost_by_instrument[instrument_id] = current_nav * abs(delta) * rate
    total_cost = sum(cost_by_instrument.values(), ZERO)
    if total_cost >= current_nav:
        raise AlphaFeasibilityError("proportional costs consume normalized NAV")
    return AlphaFeasibilityRebalance(
        decision_date=decision.decision_date,
        execution_date=decision.execution_date,
        prior_weights=dict(prior_weights),
        target_weights=dict(decision.target_weights),
        absolute_turnover=turnover,
        total_cost=total_cost,
        cost_by_instrument=cost_by_instrument,
    )


def _period_returns(
    nav: Sequence[AlphaFeasibilityNavPoint],
    *,
    half_year: bool,
) -> tuple[PeriodActiveReturn, ...]:
    groups: dict[str, list[AlphaFeasibilityNavPoint]] = defaultdict(list)
    for point in nav:
        period = (
            f"{point.trading_date.year}-H{1 if point.trading_date.month <= 6 else 2}"
            if half_year
            else f"{point.trading_date.year}-{point.trading_date.month:02d}"
        )
        groups[period].append(point)
    result: list[PeriodActiveReturn] = []
    for period, points in groups.items():
        strategy_growth = ONE
        benchmark_growth = ONE
        for point in points:
            strategy_growth *= ONE + point.daily_return
            benchmark_growth *= ONE + point.benchmark_daily_return
        net = strategy_growth - ONE
        benchmark = benchmark_growth - ONE
        result.append(PeriodActiveReturn(period, net, benchmark, net - benchmark))
    return tuple(result)


def _metrics(
    *,
    nav: Sequence[AlphaFeasibilityNavPoint],
    rebalances: Sequence[AlphaFeasibilityRebalance],
    contributions: Mapping[str, Decimal],
) -> AlphaFeasibilityMetrics:
    if not nav:
        raise AlphaFeasibilityError("metrics require daily NAV")
    net_return = nav[-1].nav - ONE
    benchmark_growth = ONE
    for point in nav:
        benchmark_growth *= ONE + point.benchmark_daily_return
    benchmark_return = benchmark_growth - ONE
    peak = ONE
    maximum_drawdown = ZERO
    for point in nav:
        peak = max(peak, point.nav)
        maximum_drawdown = max(maximum_drawdown, ONE - point.nav / peak)

    total_turnover = sum((item.absolute_turnover for item in rebalances), ZERO)
    total_cost = sum((item.total_cost for item in rebalances), ZERO)
    annualized_turnover = (
        Decimal("0.5")
        * total_turnover
        * ANNUALIZATION_SESSIONS
        / Decimal(len(nav))
    )
    exposure_states = Counter(point.market_state for point in nav)
    exposure_distribution = {
        state: Decimal(exposure_states.get(state, 0)) / Decimal(len(nav))
        for state in ("RISK_OFF", "DEFENSIVE", "NEUTRAL", "RISK_ON")
    }
    average_exposure = sum((point.gross_exposure for point in nav), ZERO) / Decimal(
        len(nav)
    )
    cash_fraction = Decimal(
        sum(point.gross_exposure <= _EPSILON for point in nav)
    ) / Decimal(len(nav))

    months = _period_returns(nav, half_year=False)
    half_years = _period_returns(nav, half_year=True)
    worst_month = min(months, key=lambda item: (item.net_active_return, item.period))
    positive_month_rate = Decimal(
        sum(item.net_active_return > ZERO for item in months)
    ) / Decimal(len(months))
    positive_half_year_count = sum(
        item.net_active_return > ZERO for item in half_years
    )

    contribution_total = sum(contributions.values(), ZERO)
    if abs(contribution_total - net_return) > Decimal("1e-18"):
        raise AlphaFeasibilityError("per-stock PnL contribution does not reconcile to NAV")
    absolute_total = sum((abs(value) for value in contributions.values()), ZERO)
    largest_stock = (
        max(abs(value) for value in contributions.values()) / absolute_total
        if absolute_total > ZERO
        else None
    )
    positive_days = sorted(
        (point.daily_pnl for point in nav if point.daily_pnl > ZERO), reverse=True
    )
    largest_ten_days = (
        sum(positive_days[:10], ZERO) / sum(positive_days, ZERO)
        if positive_days
        else None
    )
    return AlphaFeasibilityMetrics(
        net_return=net_return,
        benchmark_return=benchmark_return,
        net_active_return=net_return - benchmark_return,
        max_drawdown=maximum_drawdown,
        annualized_turnover=annualized_turnover,
        total_cost=total_cost,
        average_gross_exposure=average_exposure,
        cash_day_fraction=cash_fraction,
        exposure_state_distribution=exposure_distribution,
        trade_or_rebalance_count=sum(
            item.absolute_turnover > _EPSILON for item in rebalances
        ),
        positive_month_rate=positive_month_rate,
        positive_half_year_count=positive_half_year_count,
        worst_month=worst_month,
        per_stock_pnl_contribution=dict(sorted(contributions.items())),
        largest_stock_pnl_share=largest_stock,
        largest_10_days_pnl_share=largest_ten_days,
    )


def _run_prepared_scenario(
    *,
    split: str,
    prepared: _PreparedInput,
    scenario: ProportionalCostScenario,
) -> AlphaFeasibilityScenarioResult:
    scenario = _validate_frozen_cost_scenario(scenario)
    report_dates = _validate_split_coverage(prepared, split=split)
    first_index = prepared.trading_dates.index(report_dates[0])
    prior_date = prepared.trading_dates[first_index - 1]
    nav_value = ONE
    peak_nav = ONE
    current_weights: dict[str, Decimal] = {}
    contributions: dict[str, Decimal] = defaultdict(lambda: ZERO)
    cumulative_cost = ZERO
    nav_points: list[AlphaFeasibilityNavPoint] = []
    decisions: list[AlphaFeasibilityDecision] = []
    rebalances: list[AlphaFeasibilityRebalance] = []

    pending = _build_decision(
        prepared=prepared,
        decision_date=prior_date,
        execution_date=report_dates[0],
        current_weights=current_weights,
        current_nav=nav_value,
        peak_nav=peak_nav,
    )
    decisions.append(pending)
    for index, trading_date in enumerate(report_dates):
        nav_before = nav_value
        prior_close_values = {
            instrument_id: nav_before * weight
            for instrument_id, weight in current_weights.items()
        }
        cash_at_open = nav_before * (ONE - sum(current_weights.values(), ZERO))
        open_values: dict[str, Decimal] = {}
        for instrument_id, prior_value in prior_close_values.items():
            previous = prepared.signal_by_key.get((pending.decision_date, instrument_id))
            current = prepared.signal_by_key.get((trading_date, instrument_id))
            if previous is None or current is None:
                raise AlphaFeasibilityDataError(
                    "held position lacks causal close-to-open signal:"
                    f"{pending.decision_date}:{trading_date}:{instrument_id}"
                )
            open_value = prior_value * current.open / previous.close
            open_values[instrument_id] = open_value
            contributions[instrument_id] += open_value - prior_value
        nav_at_open = cash_at_open + sum(open_values.values(), ZERO)
        if nav_at_open <= ZERO:
            raise AlphaFeasibilityError("normalized NAV is non-positive at open")
        open_weights = {
            instrument_id: value / nav_at_open
            for instrument_id, value in open_values.items()
            if value > ZERO
        }
        rebalance = _cost_for_rebalance(
            decision=pending,
            prior_weights=open_weights,
            current_nav=nav_at_open,
            scenario=scenario,
        )
        rebalances.append(rebalance)
        cumulative_cost += rebalance.total_cost
        for instrument_id, cost in rebalance.cost_by_instrument.items():
            contributions[instrument_id] -= cost
        nav_after_cost = nav_at_open - rebalance.total_cost

        asset_values: dict[str, Decimal] = {}
        for instrument_id, weight in pending.target_weights.items():
            current = prepared.signal_by_key.get((trading_date, instrument_id))
            if current is None:
                key = (trading_date, instrument_id)
                if key in prepared.suspended:
                    raise AlphaFeasibilityDataError(
                        "held suspended position lacks causal carry value:"
                        f"{trading_date}:{instrument_id}"
                    )
                raise AlphaFeasibilityDataError(
                    "held non-suspended position lacks signal return:"
                    f"{trading_date}:{instrument_id}"
                )
            instrument_return = current.close / current.open - ONE
            start_value = nav_after_cost * weight
            instrument_pnl = start_value * instrument_return
            contributions[instrument_id] += instrument_pnl
            asset_values[instrument_id] = start_value + instrument_pnl

        cash_value = nav_after_cost * (
            ONE - sum(pending.target_weights.values(), ZERO)
        )
        nav_value = cash_value + sum(asset_values.values(), ZERO)
        if nav_value <= ZERO:
            raise AlphaFeasibilityError("normalized NAV is non-positive")
        current_weights = {
            instrument_id: value / nav_value
            for instrument_id, value in asset_values.items()
            if value > ZERO
        }
        gross_exposure = sum(current_weights.values(), ZERO)
        benchmark_previous = prepared.signal_by_key[
            (pending.decision_date, prepared.benchmark_id)
        ]
        benchmark_current = prepared.signal_by_key[(trading_date, prepared.benchmark_id)]
        benchmark_daily_return = benchmark_current.close / benchmark_previous.close - ONE
        daily_pnl = nav_value - nav_before
        nav_points.append(
            AlphaFeasibilityNavPoint(
                trading_date=trading_date,
                nav=nav_value,
                daily_pnl=daily_pnl,
                daily_return=nav_value / nav_before - ONE,
                benchmark_daily_return=benchmark_daily_return,
                gross_exposure=gross_exposure,
                market_state=pending.market_state,
                cumulative_cost=cumulative_cost,
                weights=dict(current_weights),
            )
        )
        peak_nav = max(peak_nav, nav_value)
        if index + 1 < len(report_dates):
            pending = _build_decision(
                prepared=prepared,
                decision_date=trading_date,
                execution_date=report_dates[index + 1],
                current_weights=current_weights,
                current_nav=nav_value,
                peak_nav=peak_nav,
            )
            decisions.append(pending)

    result_metrics = _metrics(
        nav=nav_points,
        rebalances=rebalances,
        contributions=contributions,
    )
    start, end = SPLIT_WINDOWS[split]
    return AlphaFeasibilityScenarioResult(
        engine_version=ENGINE_VERSION,
        research_scope=RESEARCH_SCOPE,
        scenario=scenario.name,
        split=split,
        start_date=start,
        end_date=end,
        metrics=result_metrics,
        nav=tuple(nav_points),
        decisions=tuple(decisions),
        rebalances=tuple(rebalances),
    )


def run_alpha_feasibility(
    *,
    split: str,
    inputs: AlphaFeasibilityInput,
    scenario: ProportionalCostScenario = BASE_COST,
) -> AlphaFeasibilityScenarioResult:
    """Run one safe split/scenario after metadata-first date rejection."""

    _, split_end = _split_window(split)
    scenario = _validate_frozen_cost_scenario(scenario)
    prepared = _prepare(inputs, required_end=split_end)
    return _run_prepared_scenario(split=split, prepared=prepared, scenario=scenario)


def run_alpha_feasibility_comparison(
    *,
    split: str,
    inputs: AlphaFeasibilityInput,
) -> AlphaFeasibilityComparison:
    """Run frozen base and stress proportional costs on one independent split."""

    _, split_end = _split_window(split)
    prepared = _prepare(inputs, required_end=split_end)
    base = _run_prepared_scenario(split=split, prepared=prepared, scenario=BASE_COST)
    stress = _run_prepared_scenario(split=split, prepared=prepared, scenario=STRESS_COST)
    return AlphaFeasibilityComparison(split=split, base=base, stress=stress)


def run_alpha_feasibility_study(
    *,
    inputs: AlphaFeasibilityInput,
) -> AlphaFeasibilityStudy:
    """Run both pre-registered splits without any locked-test code path."""

    # Keep these explicit guards ahead of input materialization.  There is no
    # caller-provided split and therefore no way for this entry point to select
    # 2024-2025.
    _split_window("development")
    _split_window("validation")
    prepared = _prepare(inputs, required_end=SPLIT_WINDOWS["validation"][1])
    development = AlphaFeasibilityComparison(
        split="development",
        base=_run_prepared_scenario(
            split="development", prepared=prepared, scenario=BASE_COST
        ),
        stress=_run_prepared_scenario(
            split="development", prepared=prepared, scenario=STRESS_COST
        ),
    )
    validation = AlphaFeasibilityComparison(
        split="validation",
        base=_run_prepared_scenario(
            split="validation", prepared=prepared, scenario=BASE_COST
        ),
        stress=_run_prepared_scenario(
            split="validation", prepared=prepared, scenario=STRESS_COST
        ),
    )
    return AlphaFeasibilityStudy(development=development, validation=validation)


__all__ = [
    "ALPHA_LOOKBACK_SESSIONS",
    "AlphaFeasibilityComparison",
    "AlphaFeasibilityDataError",
    "AlphaFeasibilityDecision",
    "AlphaFeasibilityError",
    "AlphaFeasibilityInput",
    "AlphaFeasibilityMetrics",
    "AlphaFeasibilityNavPoint",
    "AlphaFeasibilityRebalance",
    "AlphaFeasibilityScenarioResult",
    "AlphaFeasibilityStudy",
    "BASE_COST",
    "BenchmarkBar",
    "COST_MODEL_SEMANTICS",
    "ENGINE_VERSION",
    "ENTRY_PERCENTILE",
    "ENTRY_SCORE_EXCLUSIVE",
    "EXECUTION_REALISM",
    "FACTOR_DIRECTIONS",
    "FACTOR_IDS",
    "FROZEN_EXPOSURE_POLICY",
    "HOLD_PERCENTILE",
    "HOLD_SCORE_EXCLUSIVE",
    "HistoryEligibilityRecord",
    "INELIGIBLE_INSUFFICIENT_HISTORY",
    "INELIGIBLE_NO_INITIAL_PRICE",
    "LATEST_ALLOWED_DATE",
    "LOCKED_TEST_CONSUMED",
    "LOCKED_TEST_STATUS",
    "LockedTestAccessForbidden",
    "LockedTestStatus",
    "MAX_POSITIONS",
    "MAX_POSITION_WEIGHT",
    "MINIMUM_VALID_CONTROLLED_SESSIONS",
    "MINIMUM_COMMISSION_MODELED",
    "PITAdmissionArtifacts",
    "PITMembershipSnapshot",
    "PeriodActiveReturn",
    "ProportionalCostScenario",
    "RESEARCH_SCOPE",
    "SIGNAL_WARMUP_START",
    "SPLIT_BOUNDARY_SESSIONS",
    "SPLIT_WINDOWS",
    "STRESS_COST",
    "SignalBar",
    "SuspensionRecord",
    "WINSOR_LOWER",
    "WINSOR_UPPER",
    "rank_alpha_feasibility_universe",
    "run_alpha_feasibility",
    "run_alpha_feasibility_comparison",
    "run_alpha_feasibility_study",
    "select_pit_membership",
]
