"""Deterministic exposure-state engine for Adaptive Exposure V2.

The engine accepts exactly six input categories, a caller-supplied but fully
structured and self-hashed pre-registered hysteresis policy, and explicit
state memory.  It does not choose production thresholds.  Ordinary state
changes require consecutive controlled-session confirmations; data failures,
future observations and account drawdown of at least 12% immediately reduce
the target to ``RISK_OFF``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
import math
import re
from typing import Any, Iterable

from .contracts import canonical_sha256
from .experiment_v3_admission import (
    ExperimentV3AdmissionError,
    ExperimentV3AdmissionReceiptV1,
    verify_experiment_v3_admission_receipt,
    verify_experiment_v3_diagnostic_binding,
)


EXPOSURE_INPUT_SCHEMA_VERSION = "exposure-input-snapshot.v1"
EXPOSURE_POLICY_SCHEMA_VERSION = "exposure-hysteresis-policy.v2"
EXPOSURE_DECISION_SCHEMA_VERSION = "exposure-decision.v2"
EXPOSURE_STATE_MEMORY_SCHEMA_VERSION = "exposure-state-memory.v1"
ACCOUNT_DRAWDOWN_RISK_OFF_TRIGGER = 0.12
CHINA_STANDARD_TIME = timezone(timedelta(hours=8))

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ExposureEngineError(ValueError):
    """Raised when exposure contracts are malformed or cannot be replayed."""


class ExposureState(str, Enum):
    RISK_OFF = "RISK_OFF"
    DEFENSIVE = "DEFENSIVE"
    NEUTRAL = "NEUTRAL"
    RISK_ON = "RISK_ON"


EXPOSURE_BY_STATE: dict[ExposureState, float] = {
    ExposureState.RISK_OFF: 0.0,
    ExposureState.DEFENSIVE: 0.30,
    ExposureState.NEUTRAL: 0.60,
    ExposureState.RISK_ON: 1.0,
}


class ExposureInputCategory(str, Enum):
    CSI800_TOTAL_RETURN_TREND = "CSI800_TOTAL_RETURN_TREND"
    MARKET_BREADTH = "MARKET_BREADTH"
    REALIZED_VOLATILITY = "REALIZED_VOLATILITY"
    MARKET_DRAWDOWN = "MARKET_DRAWDOWN"
    ALPHA_PREDICTION_DISTRIBUTION = "ALPHA_PREDICTION_DISTRIBUTION"
    ACCOUNT_DRAWDOWN = "ACCOUNT_DRAWDOWN"


EXPOSURE_INPUT_CATEGORIES: tuple[ExposureInputCategory, ...] = tuple(ExposureInputCategory)


class ExposureMetricStatus(str, Enum):
    OK = "OK"
    DATA_FAILED = "DATA_FAILED"


class ComparisonOperator(str, Enum):
    LT = "LT"
    LTE = "LTE"
    GT = "GT"
    GTE = "GTE"


class ExposureTransitionStatus(str, Enum):
    HOLD = "HOLD"
    HYSTERESIS_PENDING = "HYSTERESIS_PENDING"
    STATE_CHANGED = "STATE_CHANGED"
    IMMEDIATE_RISK_OFF = "IMMEDIATE_RISK_OFF"


def _aware(value: datetime | None, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ExposureEngineError(f"{field_name} must be timezone-aware")
    return value


def _cst_session_date(value: datetime, field_name: str) -> date:
    """Return the A-share strategy session date for one decision instant."""

    return _aware(value, field_name).astimezone(CHINA_STANDARD_TIME).date()


def _sha(value: str | None, field_name: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ExposureEngineError(f"{field_name} must be a lowercase SHA-256")
    return value


def _identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExposureEngineError(f"{field_name} is required")
    text = value.strip()
    if any(character.isspace() for character in text):
        raise ExposureEngineError(f"{field_name} cannot contain whitespace")
    return text


def _finite(value: float | int, field_name: str) -> float:
    if isinstance(value, bool):
        raise ExposureEngineError(f"{field_name} must be finite")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ExposureEngineError(f"{field_name} must be finite") from exc
    if not math.isfinite(number):
        raise ExposureEngineError(f"{field_name} must be finite")
    return number


def _codes(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({_identifier(value, "reason code") for value in values}))


def _state(value: ExposureState | str, field_name: str) -> ExposureState:
    try:
        return value if isinstance(value, ExposureState) else ExposureState(str(value))
    except ValueError as exc:
        raise ExposureEngineError(f"{field_name} is not an exposure state") from exc


@dataclass(frozen=True, slots=True)
class ExposureMetricV2:
    """One of the six allowed exposure observations or its explicit failure."""

    category: ExposureInputCategory
    status: ExposureMetricStatus
    value: float | None
    observation_session: date | None
    available_at: datetime | None
    source_snapshot_sha256: str | None
    failure_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        try:
            category = self.category if isinstance(self.category, ExposureInputCategory) else ExposureInputCategory(self.category)
            status = self.status if isinstance(self.status, ExposureMetricStatus) else ExposureMetricStatus(self.status)
        except ValueError as exc:
            raise ExposureEngineError("unknown exposure metric category or status") from exc
        codes = _codes(self.failure_codes)
        if self.observation_session is not None and (
            not isinstance(self.observation_session, date) or isinstance(self.observation_session, datetime)
        ):
            raise ExposureEngineError("observation_session must be a date or null")
        if self.available_at is not None:
            _aware(self.available_at, "available_at")
        _sha(self.source_snapshot_sha256, "source_snapshot_sha256", optional=True)
        if status is ExposureMetricStatus.OK:
            if self.value is None or self.observation_session is None or self.available_at is None or self.source_snapshot_sha256 is None:
                raise ExposureEngineError("OK metrics require value, session, available_at and source hash")
            if codes:
                raise ExposureEngineError("OK metrics cannot contain failure codes")
            value = _finite(self.value, "metric value")
            if category in {ExposureInputCategory.MARKET_DRAWDOWN, ExposureInputCategory.ACCOUNT_DRAWDOWN} and not 0.0 <= value <= 1.0:
                raise ExposureEngineError("drawdown metrics must be within [0, 1]")
            object.__setattr__(self, "value", value)
        else:
            if self.value is not None:
                raise ExposureEngineError("failed metrics must not carry a value")
            if not codes:
                raise ExposureEngineError("failed metrics require failure codes")
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "failure_codes", codes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "status": self.status.value,
            "value": self.value,
            "observation_session": (
                None if self.observation_session is None else self.observation_session.isoformat()
            ),
            "available_at": None if self.available_at is None else self.available_at.isoformat(),
            "source_snapshot_sha256": self.source_snapshot_sha256,
            "failure_codes": list(self.failure_codes),
        }


@dataclass(frozen=True, slots=True)
class ExposureInputSnapshotV2:
    decision_at: datetime
    metrics: tuple[ExposureMetricV2, ...]
    schema_version: str = EXPOSURE_INPUT_SCHEMA_VERSION
    input_snapshot_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != EXPOSURE_INPUT_SCHEMA_VERSION:
            raise ExposureEngineError("unsupported exposure input schema")
        _aware(self.decision_at, "decision_at")
        metrics = tuple(self.metrics)
        if any(not isinstance(item, ExposureMetricV2) for item in metrics):
            raise ExposureEngineError("metrics must contain ExposureMetricV2 objects")
        by_category = {item.category: item for item in metrics}
        if len(metrics) != len(EXPOSURE_INPUT_CATEGORIES) or set(by_category) != set(EXPOSURE_INPUT_CATEGORIES):
            raise ExposureEngineError("exposure input must contain exactly the six allowed categories")
        metrics = tuple(by_category[item] for item in EXPOSURE_INPUT_CATEGORIES)
        object.__setattr__(self, "metrics", metrics)
        object.__setattr__(self, "input_snapshot_sha256", canonical_sha256(self.to_content_dict()))

    @property
    def by_category(self) -> dict[ExposureInputCategory, ExposureMetricV2]:
        return {item.category: item for item in self.metrics}

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "decision_at": self.decision_at.isoformat(),
            "metrics": [item.to_dict() for item in self.metrics],
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.to_content_dict()
        payload["input_snapshot_sha256"] = self.input_snapshot_sha256
        return payload


@dataclass(frozen=True, slots=True)
class ExposureConditionV2:
    category: ExposureInputCategory
    operator: ComparisonOperator
    threshold: float

    def __post_init__(self) -> None:
        try:
            category = self.category if isinstance(self.category, ExposureInputCategory) else ExposureInputCategory(self.category)
            operator = self.operator if isinstance(self.operator, ComparisonOperator) else ComparisonOperator(self.operator)
        except ValueError as exc:
            raise ExposureEngineError("unknown exposure condition category or operator") from exc
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "operator", operator)
        object.__setattr__(self, "threshold", _finite(self.threshold, "condition threshold"))

    def matches(self, value: float) -> bool:
        if self.operator is ComparisonOperator.LT:
            return value < self.threshold
        if self.operator is ComparisonOperator.LTE:
            return value <= self.threshold
        if self.operator is ComparisonOperator.GT:
            return value > self.threshold
        return value >= self.threshold

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "operator": self.operator.value,
            "threshold": self.threshold,
        }


@dataclass(frozen=True, slots=True)
class ExposureStateRuleV2:
    """One all-of rule and its pre-registered confirmation count."""

    target_state: ExposureState
    conditions: tuple[ExposureConditionV2, ...]
    required_consecutive_sessions: int
    reason_code: str

    def __post_init__(self) -> None:
        target = _state(self.target_state, "target_state")
        conditions = tuple(self.conditions)
        if not conditions or any(not isinstance(item, ExposureConditionV2) for item in conditions):
            raise ExposureEngineError("state rules require typed conditions")
        categories = tuple(item.category for item in conditions)
        if len(categories) != len(set(categories)):
            raise ExposureEngineError("a rule cannot repeat an input category")
        conditions = tuple(sorted(conditions, key=lambda item: item.category.value))
        if type(self.required_consecutive_sessions) is not int or self.required_consecutive_sessions < 2:
            raise ExposureEngineError("ordinary state changes require at least two confirmations")
        object.__setattr__(self, "target_state", target)
        object.__setattr__(self, "conditions", conditions)
        object.__setattr__(self, "reason_code", _identifier(self.reason_code, "reason_code"))

    def matches(self, values: dict[ExposureInputCategory, float]) -> bool:
        return all(condition.matches(values[condition.category]) for condition in self.conditions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_state": self.target_state.value,
            "conditions": [item.to_dict() for item in self.conditions],
            "required_consecutive_sessions": self.required_consecutive_sessions,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class ExposureHysteresisPolicyV2:
    """Explicit pre-registration artifact; the engine supplies no thresholds."""

    policy_id: str
    policy_version: str
    preregistered_at: datetime
    rules: tuple[ExposureStateRuleV2, ...]
    policy_source_sha256: str
    experiment_spec_sha256: str
    policy_admission_receipt: ExperimentV3AdmissionReceiptV1
    fallback_behavior: str = "HOLD_CURRENT"
    artifact_status: str = "preregistered_frozen"
    schema_version: str = EXPOSURE_POLICY_SCHEMA_VERSION
    policy_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != EXPOSURE_POLICY_SCHEMA_VERSION:
            raise ExposureEngineError("unsupported exposure policy schema")
        if self.fallback_behavior != "HOLD_CURRENT" or self.artifact_status != "preregistered_frozen":
            raise ExposureEngineError("exposure policy must be frozen with HOLD_CURRENT fallback")
        object.__setattr__(self, "policy_id", _identifier(self.policy_id, "policy_id"))
        object.__setattr__(self, "policy_version", _identifier(self.policy_version, "policy_version"))
        _aware(self.preregistered_at, "preregistered_at")
        _sha(self.policy_source_sha256, "policy_source_sha256")
        _sha(self.experiment_spec_sha256, "experiment_spec_sha256")
        if type(self.policy_admission_receipt) is not ExperimentV3AdmissionReceiptV1:
            raise ExposureEngineError(
                "exposure policy requires the exact V3 diagnostic receipt type"
            )
        try:
            verify_experiment_v3_diagnostic_binding(
                self.policy_admission_receipt,
                as_of=self.policy_admission_receipt.issued_at
            )
        except ExperimentV3AdmissionError as exc:
            raise ExposureEngineError(
                f"exposure policy diagnostic binding rejected: {exc}"
            ) from exc
        if (
            self.policy_admission_receipt.experiment_spec_sha256
            != self.experiment_spec_sha256
            or self.policy_admission_receipt.exposure_policy_source_sha256
            != self.policy_source_sha256
            or self.policy_admission_receipt.exposure_policy_frozen_at
            != self.preregistered_at
        ):
            raise ExposureEngineError(
                "exposure policy diagnostic binding mismatch"
            )
        rules = tuple(self.rules)
        if not rules or any(not isinstance(item, ExposureStateRuleV2) for item in rules):
            raise ExposureEngineError("policy requires typed hysteresis rules")
        targets = tuple(item.target_state for item in rules)
        if len(targets) != len(set(targets)):
            raise ExposureEngineError("policy permits at most one rule per target state")
        order = {state: index for index, state in enumerate(ExposureState)}
        rules = tuple(sorted(rules, key=lambda item: order[item.target_state]))
        object.__setattr__(self, "rules", rules)
        object.__setattr__(self, "policy_sha256", canonical_sha256(self.to_content_dict()))

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "artifact_status": self.artifact_status,
            "preregistered_at": self.preregistered_at.isoformat(),
            "fallback_behavior": self.fallback_behavior,
            "policy_source_sha256": self.policy_source_sha256,
            "experiment_spec_sha256": self.experiment_spec_sha256,
            "policy_admission_receipt_sha256": (
                self.policy_admission_receipt.receipt_sha256
            ),
            "rules": [item.to_dict() for item in self.rules],
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.to_content_dict()
        payload["policy_sha256"] = self.policy_sha256
        return payload


@dataclass(frozen=True, slots=True)
class ExposureStateMemoryV2:
    """Hash-bound memory carried from the preceding controlled decision."""

    policy_sha256: str
    current_state: ExposureState
    pending_state: ExposureState | None = None
    pending_consecutive_sessions: int = 0
    last_decision_at: datetime | None = None
    last_input_snapshot_sha256: str | None = None
    schema_version: str = EXPOSURE_STATE_MEMORY_SCHEMA_VERSION
    state_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != EXPOSURE_STATE_MEMORY_SCHEMA_VERSION:
            raise ExposureEngineError("unsupported exposure state-memory schema")
        _sha(self.policy_sha256, "policy_sha256")
        current = _state(self.current_state, "current_state")
        pending = None if self.pending_state is None else _state(self.pending_state, "pending_state")
        if type(self.pending_consecutive_sessions) is not int or self.pending_consecutive_sessions < 0:
            raise ExposureEngineError("pending_consecutive_sessions must be a non-negative integer")
        if pending is None and self.pending_consecutive_sessions != 0:
            raise ExposureEngineError("pending count requires a pending state")
        if pending is not None and (pending is current or self.pending_consecutive_sessions < 1):
            raise ExposureEngineError("pending state must differ from current and have a positive count")
        if (self.last_decision_at is None) != (self.last_input_snapshot_sha256 is None):
            raise ExposureEngineError("last decision time and input hash must be present together")
        if self.last_decision_at is not None:
            _aware(self.last_decision_at, "last_decision_at")
            _sha(self.last_input_snapshot_sha256, "last_input_snapshot_sha256")
        if pending is not None and self.last_decision_at is None:
            raise ExposureEngineError("pending state requires a preceding controlled decision")
        object.__setattr__(self, "current_state", current)
        object.__setattr__(self, "pending_state", pending)
        object.__setattr__(self, "state_sha256", canonical_sha256(self.to_content_dict()))

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "policy_sha256": self.policy_sha256,
            "current_state": self.current_state.value,
            "pending_state": None if self.pending_state is None else self.pending_state.value,
            "pending_consecutive_sessions": self.pending_consecutive_sessions,
            "last_decision_at": (
                None if self.last_decision_at is None else self.last_decision_at.isoformat()
            ),
            "last_input_snapshot_sha256": self.last_input_snapshot_sha256,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.to_content_dict()
        payload["state_sha256"] = self.state_sha256
        return payload


@dataclass(frozen=True, slots=True)
class ExposureDecisionV2:
    decision_at: datetime
    previous_state: ExposureState
    candidate_state: ExposureState
    state: ExposureState
    target_gross_exposure: float
    transition_status: ExposureTransitionStatus
    pending_state: ExposureState | None
    pending_consecutive_sessions: int
    reason_codes: tuple[str, ...]
    input_snapshot_sha256: str
    policy_sha256: str
    previous_state_sha256: str
    next_state_memory: ExposureStateMemoryV2
    schema_version: str = EXPOSURE_DECISION_SCHEMA_VERSION
    decision_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != EXPOSURE_DECISION_SCHEMA_VERSION:
            raise ExposureEngineError("unsupported exposure decision schema")
        _aware(self.decision_at, "decision_at")
        previous = _state(self.previous_state, "previous_state")
        candidate = _state(self.candidate_state, "candidate_state")
        state = _state(self.state, "state")
        try:
            transition = self.transition_status if isinstance(self.transition_status, ExposureTransitionStatus) else ExposureTransitionStatus(self.transition_status)
        except ValueError as exc:
            raise ExposureEngineError("unknown transition status") from exc
        pending = None if self.pending_state is None else _state(self.pending_state, "pending_state")
        if type(self.pending_consecutive_sessions) is not int or self.pending_consecutive_sessions < 0:
            raise ExposureEngineError("pending count must be non-negative")
        reasons = _codes(self.reason_codes)
        if not reasons:
            raise ExposureEngineError("exposure decisions require reason codes")
        target = _finite(self.target_gross_exposure, "target_gross_exposure")
        if target != EXPOSURE_BY_STATE[state]:
            raise ExposureEngineError("target gross exposure does not match the fixed state map")
        for field_name in ("input_snapshot_sha256", "policy_sha256", "previous_state_sha256"):
            _sha(getattr(self, field_name), field_name)
        if not isinstance(self.next_state_memory, ExposureStateMemoryV2):
            raise ExposureEngineError("next_state_memory must be ExposureStateMemoryV2")
        if self.next_state_memory.current_state is not state or self.next_state_memory.policy_sha256 != self.policy_sha256:
            raise ExposureEngineError("next state memory does not bind this decision")
        if self.next_state_memory.last_decision_at != self.decision_at or self.next_state_memory.last_input_snapshot_sha256 != self.input_snapshot_sha256:
            raise ExposureEngineError("next state memory does not bind decision time/input")
        if self.next_state_memory.pending_state is not pending or self.next_state_memory.pending_consecutive_sessions != self.pending_consecutive_sessions:
            raise ExposureEngineError("pending state differs from next state memory")
        object.__setattr__(self, "previous_state", previous)
        object.__setattr__(self, "candidate_state", candidate)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "transition_status", transition)
        object.__setattr__(self, "pending_state", pending)
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "target_gross_exposure", target)
        object.__setattr__(self, "decision_sha256", canonical_sha256(self.to_content_dict()))

    @property
    def state_sha256(self) -> str:
        return self.next_state_memory.state_sha256

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "decision_at": self.decision_at.isoformat(),
            "previous_state": self.previous_state.value,
            "candidate_state": self.candidate_state.value,
            "state": self.state.value,
            "target_gross_exposure": self.target_gross_exposure,
            "transition_status": self.transition_status.value,
            "pending_state": None if self.pending_state is None else self.pending_state.value,
            "pending_consecutive_sessions": self.pending_consecutive_sessions,
            "reason_codes": list(self.reason_codes),
            "input_snapshot_sha256": self.input_snapshot_sha256,
            "policy_sha256": self.policy_sha256,
            "previous_state_sha256": self.previous_state_sha256,
            "state_sha256": self.state_sha256,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.to_content_dict()
        payload["decision_sha256"] = self.decision_sha256
        return payload


def _immediate_risk_off(
    *,
    inputs: ExposureInputSnapshotV2,
    policy: ExposureHysteresisPolicyV2,
    memory: ExposureStateMemoryV2,
    reasons: Iterable[str],
) -> ExposureDecisionV2:
    next_memory = ExposureStateMemoryV2(
        policy_sha256=policy.policy_sha256,
        current_state=ExposureState.RISK_OFF,
        pending_state=None,
        pending_consecutive_sessions=0,
        last_decision_at=inputs.decision_at,
        last_input_snapshot_sha256=inputs.input_snapshot_sha256,
    )
    return ExposureDecisionV2(
        decision_at=inputs.decision_at,
        previous_state=memory.current_state,
        candidate_state=ExposureState.RISK_OFF,
        state=ExposureState.RISK_OFF,
        target_gross_exposure=EXPOSURE_BY_STATE[ExposureState.RISK_OFF],
        transition_status=ExposureTransitionStatus.IMMEDIATE_RISK_OFF,
        pending_state=None,
        pending_consecutive_sessions=0,
        reason_codes=_codes(reasons),
        input_snapshot_sha256=inputs.input_snapshot_sha256,
        policy_sha256=policy.policy_sha256,
        previous_state_sha256=memory.state_sha256,
        next_state_memory=next_memory,
    )


def _decide_exposure(
    inputs: ExposureInputSnapshotV2,
    policy: ExposureHysteresisPolicyV2,
    memory: ExposureStateMemoryV2,
    *,
    diagnostic_only: bool,
) -> ExposureDecisionV2:
    """Apply hard risk overrides, otherwise one pre-registered hysteresis step."""

    if not isinstance(inputs, ExposureInputSnapshotV2):
        raise ExposureEngineError("inputs must be ExposureInputSnapshotV2")
    if not isinstance(policy, ExposureHysteresisPolicyV2):
        raise ExposureEngineError("policy must be ExposureHysteresisPolicyV2")
    if not isinstance(memory, ExposureStateMemoryV2):
        raise ExposureEngineError("memory must be ExposureStateMemoryV2")
    if canonical_sha256(inputs.to_content_dict()) != inputs.input_snapshot_sha256:
        raise ExposureEngineError("exposure input snapshot hash mismatch")
    if canonical_sha256(policy.to_content_dict()) != policy.policy_sha256:
        raise ExposureEngineError("exposure policy hash mismatch")
    if canonical_sha256(memory.to_content_dict()) != memory.state_sha256:
        raise ExposureEngineError("exposure state-memory hash mismatch")
    if memory.policy_sha256 != policy.policy_sha256:
        raise ExposureEngineError("state memory policy hash mismatch")
    if memory.last_decision_at is not None and memory.last_decision_at >= inputs.decision_at:
        raise ExposureEngineError("exposure decisions must advance in time")
    decision_session = _cst_session_date(inputs.decision_at, "decision_at")
    if (
        memory.last_decision_at is not None
        and _cst_session_date(memory.last_decision_at, "last_decision_at")
        >= decision_session
    ):
        raise ExposureEngineError(
            "exposure decisions must advance to a later CST strategy date"
        )
    if memory.pending_state is not None:
        pending_rules = tuple(
            rule for rule in policy.rules if rule.target_state is memory.pending_state
        )
        if (
            len(pending_rules) != 1
            or memory.pending_consecutive_sessions
            >= pending_rules[0].required_consecutive_sessions
        ):
            raise ExposureEngineError(
                "pending exposure memory is unreachable under the frozen policy"
            )
    try:
        verify_experiment_v3_diagnostic_binding(
            policy.policy_admission_receipt,
            as_of=inputs.decision_at
        )
    except ExperimentV3AdmissionError as exc:
        if diagnostic_only:
            raise ExposureEngineError(
                f"exposure diagnostic binding rejected: {exc}"
            ) from exc
    if not diagnostic_only:
        try:
            verify_experiment_v3_admission_receipt(
                policy.policy_admission_receipt,
                as_of=inputs.decision_at,
                experiment_spec_sha256=policy.experiment_spec_sha256,
                exposure_policy_source_sha256=policy.policy_source_sha256,
                exposure_policy_frozen_at=policy.preregistered_at,
            )
        except ExperimentV3AdmissionError:
            # Missing formal Alpha/Experiment admission must never preserve or
            # increase exposure. It does not block the P0.1 pure-risk exit path,
            # and all state-CAS/time invariants above still apply.
            return _immediate_risk_off(
                inputs=inputs,
                policy=policy,
                memory=memory,
                reasons=("FORMAL_EXPERIMENT_V3_ADMISSION_BLOCKED",),
            )

    immediate_reasons: list[str] = []
    if policy.preregistered_at > inputs.decision_at:
        immediate_reasons.append("POLICY_NOT_PREREGISTERED_AT_DECISION")
    values: dict[ExposureInputCategory, float] = {}
    for metric in inputs.metrics:
        if metric.status is ExposureMetricStatus.DATA_FAILED:
            immediate_reasons.extend(
                f"DATA_FAILURE:{metric.category.value}:{code}" for code in metric.failure_codes
            )
            continue
        assert metric.value is not None
        assert metric.observation_session is not None
        assert metric.available_at is not None
        values[metric.category] = metric.value
        if metric.observation_session > decision_session:
            immediate_reasons.append(f"FUTURE_SESSION:{metric.category.value}")
        elif metric.observation_session < decision_session:
            immediate_reasons.append(f"STALE_SESSION:{metric.category.value}")
        if metric.available_at > inputs.decision_at:
            immediate_reasons.append(f"FUTURE_AVAILABLE_AT:{metric.category.value}")
    account_drawdown = values.get(ExposureInputCategory.ACCOUNT_DRAWDOWN)
    if account_drawdown is not None and account_drawdown >= ACCOUNT_DRAWDOWN_RISK_OFF_TRIGGER:
        immediate_reasons.append("ACCOUNT_DRAWDOWN_GTE_12_PERCENT")
    if immediate_reasons:
        return _immediate_risk_off(inputs=inputs, policy=policy, memory=memory, reasons=immediate_reasons)

    matching_rules = tuple(rule for rule in policy.rules if rule.matches(values))
    if len(matching_rules) > 1:
        return _immediate_risk_off(
            inputs=inputs,
            policy=policy,
            memory=memory,
            reasons=("AMBIGUOUS_HYSTERESIS_POLICY_MATCH",),
        )

    if not matching_rules:
        candidate = memory.current_state
        next_memory = ExposureStateMemoryV2(
            policy_sha256=policy.policy_sha256,
            current_state=memory.current_state,
            pending_state=None,
            pending_consecutive_sessions=0,
            last_decision_at=inputs.decision_at,
            last_input_snapshot_sha256=inputs.input_snapshot_sha256,
        )
        return ExposureDecisionV2(
            decision_at=inputs.decision_at,
            previous_state=memory.current_state,
            candidate_state=candidate,
            state=memory.current_state,
            target_gross_exposure=EXPOSURE_BY_STATE[memory.current_state],
            transition_status=ExposureTransitionStatus.HOLD,
            pending_state=None,
            pending_consecutive_sessions=0,
            reason_codes=("NO_RULE_MATCH_HOLD_CURRENT",),
            input_snapshot_sha256=inputs.input_snapshot_sha256,
            policy_sha256=policy.policy_sha256,
            previous_state_sha256=memory.state_sha256,
            next_state_memory=next_memory,
        )

    rule = matching_rules[0]
    candidate = rule.target_state
    if candidate is memory.current_state:
        next_memory = ExposureStateMemoryV2(
            policy_sha256=policy.policy_sha256,
            current_state=memory.current_state,
            pending_state=None,
            pending_consecutive_sessions=0,
            last_decision_at=inputs.decision_at,
            last_input_snapshot_sha256=inputs.input_snapshot_sha256,
        )
        return ExposureDecisionV2(
            decision_at=inputs.decision_at,
            previous_state=memory.current_state,
            candidate_state=candidate,
            state=memory.current_state,
            target_gross_exposure=EXPOSURE_BY_STATE[memory.current_state],
            transition_status=ExposureTransitionStatus.HOLD,
            pending_state=None,
            pending_consecutive_sessions=0,
            reason_codes=(rule.reason_code, "RULE_MATCHED_CURRENT_STATE"),
            input_snapshot_sha256=inputs.input_snapshot_sha256,
            policy_sha256=policy.policy_sha256,
            previous_state_sha256=memory.state_sha256,
            next_state_memory=next_memory,
        )

    pending_count = (
        memory.pending_consecutive_sessions + 1
        if memory.pending_state is candidate
        else 1
    )
    if pending_count >= rule.required_consecutive_sessions:
        next_memory = ExposureStateMemoryV2(
            policy_sha256=policy.policy_sha256,
            current_state=candidate,
            pending_state=None,
            pending_consecutive_sessions=0,
            last_decision_at=inputs.decision_at,
            last_input_snapshot_sha256=inputs.input_snapshot_sha256,
        )
        return ExposureDecisionV2(
            decision_at=inputs.decision_at,
            previous_state=memory.current_state,
            candidate_state=candidate,
            state=candidate,
            target_gross_exposure=EXPOSURE_BY_STATE[candidate],
            transition_status=ExposureTransitionStatus.STATE_CHANGED,
            pending_state=None,
            pending_consecutive_sessions=0,
            reason_codes=(rule.reason_code, "HYSTERESIS_CONFIRMED"),
            input_snapshot_sha256=inputs.input_snapshot_sha256,
            policy_sha256=policy.policy_sha256,
            previous_state_sha256=memory.state_sha256,
            next_state_memory=next_memory,
        )

    next_memory = ExposureStateMemoryV2(
        policy_sha256=policy.policy_sha256,
        current_state=memory.current_state,
        pending_state=candidate,
        pending_consecutive_sessions=pending_count,
        last_decision_at=inputs.decision_at,
        last_input_snapshot_sha256=inputs.input_snapshot_sha256,
    )
    return ExposureDecisionV2(
        decision_at=inputs.decision_at,
        previous_state=memory.current_state,
        candidate_state=candidate,
        state=memory.current_state,
        target_gross_exposure=EXPOSURE_BY_STATE[memory.current_state],
        transition_status=ExposureTransitionStatus.HYSTERESIS_PENDING,
        pending_state=candidate,
        pending_consecutive_sessions=pending_count,
        reason_codes=(
            rule.reason_code,
            f"HYSTERESIS_PENDING_{pending_count}_OF_{rule.required_consecutive_sessions}",
        ),
        input_snapshot_sha256=inputs.input_snapshot_sha256,
        policy_sha256=policy.policy_sha256,
        previous_state_sha256=memory.state_sha256,
        next_state_memory=next_memory,
    )


def decide_exposure(
    inputs: ExposureInputSnapshotV2,
    policy: ExposureHysteresisPolicyV2,
    memory: ExposureStateMemoryV2,
) -> ExposureDecisionV2:
    """Formal entry; blocked admission always yields a RISK_OFF decision."""

    return _decide_exposure(inputs, policy, memory, diagnostic_only=False)


def decide_exposure_diagnostic(
    inputs: ExposureInputSnapshotV2,
    policy: ExposureHysteresisPolicyV2,
    memory: ExposureStateMemoryV2,
) -> ExposureDecisionV2:
    """Research-only hysteresis replay; never formal signal admission."""

    return _decide_exposure(inputs, policy, memory, diagnostic_only=True)


decide_exposure_v2 = decide_exposure


__all__ = [
    "ACCOUNT_DRAWDOWN_RISK_OFF_TRIGGER",
    "EXPOSURE_BY_STATE",
    "EXPOSURE_DECISION_SCHEMA_VERSION",
    "EXPOSURE_INPUT_CATEGORIES",
    "EXPOSURE_INPUT_SCHEMA_VERSION",
    "EXPOSURE_POLICY_SCHEMA_VERSION",
    "EXPOSURE_STATE_MEMORY_SCHEMA_VERSION",
    "ComparisonOperator",
    "ExposureConditionV2",
    "ExposureDecisionV2",
    "ExposureEngineError",
    "ExposureHysteresisPolicyV2",
    "ExposureInputCategory",
    "ExposureInputSnapshotV2",
    "ExposureMetricStatus",
    "ExposureMetricV2",
    "ExposureState",
    "ExposureStateMemoryV2",
    "ExposureStateRuleV2",
    "ExposureTransitionStatus",
    "decide_exposure",
    "decide_exposure_diagnostic",
    "decide_exposure_v2",
]
