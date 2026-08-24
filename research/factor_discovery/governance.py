"""Fail-closed governance contracts for proposed and approved factors.

This module deliberately separates four states:

* :class:`FactorHypothesisV2` is always an LLM research candidate;
* :class:`FactorValidationReceiptV1` records independent validation evidence;
* :class:`ApprovedFactorV1` binds one factor implementation to that receipt;
* :class:`ApprovedFactorRegistryV1` is a deterministic, self-hashed snapshot.

The contracts do not run a backtest, authenticate a receipt issuer, generate an
alpha score, or grant Paper/trading access.  In particular, there is no boolean
``source_authenticated`` or ``validation_passed`` escape hatch.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json
import math
import re
from typing import Any, Mapping, Sequence, cast


FACTOR_HYPOTHESIS_SCHEMA_VERSION = "factor-hypothesis-v2"
FACTOR_VALIDATION_RECEIPT_SCHEMA_VERSION = "factor-validation-receipt.v1"
APPROVED_FACTOR_REGISTRY_SCHEMA_VERSION = "approved-factor-registry.v1"

LLM_CANDIDATE_STATUS = "llm_research_candidate_only"
VALIDATION_RESULT = "passed_pre_registered_independent_validation"
VALIDATION_PARTITION = "validation_only_not_locked_test"
APPROVED_FACTOR_STATUS = "approved_for_frozen_research_only"
REGISTRY_STATUS = "approved_for_frozen_research_only"
REGISTRY_TRUST_BOUNDARY = "controlled_validation_receipts_only"
LIVE_NOT_SUPPORTED = "live_not_supported"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_FACTOR_ID = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_FIELD_ID = re.compile(r"^[a-z][a-z0-9_.]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class FactorGovernanceError(ValueError):
    """Raised when factor governance evidence is malformed or contradictory."""


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return _canonical_value(asdict(value))
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise FactorGovernanceError(
                    "canonical JSON mappings require string keys"
                )
            result[key] = _canonical_value(item)
        return result
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        raise FactorGovernanceError(
            "unordered collections are not canonical JSON values"
        )
    if isinstance(value, datetime):
        return _iso_datetime(_aware(value, "datetime"))
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise FactorGovernanceError("canonical decimals must be finite")
        return format(value, "f")
    if isinstance(value, float) and not math.isfinite(value):
        raise FactorGovernanceError("canonical floats must be finite")
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise FactorGovernanceError(
        f"unsupported canonical JSON value: {type(value).__name__}"
    )


def canonical_json_bytes(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON bytes with normalized UTC timestamps."""

    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    """Return SHA-256 of :func:`canonical_json_bytes`."""

    return sha256(canonical_json_bytes(value)).hexdigest()


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FactorGovernanceError(f"{field_name} must be a non-empty string")
    return value.strip()


def _identifier(value: Any, field_name: str) -> str:
    text = _text(value, field_name)
    if _IDENTIFIER.fullmatch(text) is None:
        raise FactorGovernanceError(f"{field_name} must be a stable identifier")
    return text


def _factor_id(value: Any) -> str:
    text = _text(value, "factor_id").upper()
    if _FACTOR_ID.fullmatch(text) is None:
        raise FactorGovernanceError(
            "factor_id must be an uppercase stable identifier"
        )
    return text


def _sha(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise FactorGovernanceError(f"{field_name} must be a lowercase SHA-256")
    return value


def _aware(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise FactorGovernanceError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise FactorGovernanceError(f"{field_name} must include a timezone offset")
    return value.astimezone(timezone.utc)


def _parse_datetime(value: Any, field_name: str) -> datetime:
    if isinstance(value, datetime):
        return _aware(value, field_name)
    if not isinstance(value, str):
        raise FactorGovernanceError(f"{field_name} must be an RFC3339 datetime")
    try:
        parsed = datetime.fromisoformat(value.replace("z", "+00:00").replace("Z", "+00:00"))
    except ValueError as exc:
        raise FactorGovernanceError(
            f"{field_name} must be an RFC3339 datetime"
        ) from exc
    return _aware(parsed, field_name)


def _iso_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _false(value: Any, field_name: str) -> bool:
    if type(value) is not bool or value is not False:
        raise FactorGovernanceError(f"{field_name} must remain false")
    return False


def _strings(
    value: Any,
    field_name: str,
    *,
    pattern: re.Pattern[str] | None = None,
) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise FactorGovernanceError(f"{field_name} must be an ordered string array")
    result = tuple(_text(item, f"{field_name} item") for item in value)
    if not result:
        raise FactorGovernanceError(f"{field_name} must not be empty")
    if len(set(result)) != len(result):
        raise FactorGovernanceError(f"{field_name} must be unique")
    if pattern is not None and any(pattern.fullmatch(item) is None for item in result):
        raise FactorGovernanceError(f"{field_name} contains an invalid identifier")
    return tuple(sorted(result))


def _strict_mapping(
    payload: Any,
    expected: frozenset[str],
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise FactorGovernanceError(f"{label} must be an object")
    missing = sorted(expected - set(payload))
    unknown = sorted(set(payload) - expected)
    if missing:
        raise FactorGovernanceError(f"{label} missing fields: {', '.join(missing)}")
    if unknown:
        raise FactorGovernanceError(f"{label} has unknown fields: {', '.join(unknown)}")
    return cast(Mapping[str, Any], payload)


@dataclass(frozen=True, slots=True)
class FactorHypothesisV2:
    """An immutable proposal that cannot itself claim validation or approval."""

    hypothesis_id: str
    factor_id: str
    created_at: datetime
    information_cutoff_at: datetime
    formula: str
    input_fields: tuple[str, ...]
    input_schema_sha256: str
    prediction_target: str
    horizon_trading_days: int
    expected_sign: str
    universe_policy: str
    benchmark_policy: str
    economic_rationale: str
    falsification_conditions: tuple[str, ...]
    status: str = LLM_CANDIDATE_STATUS
    paper_eligibility: bool = False
    trade_eligibility: bool = False
    real_money_list_allowed: bool = False
    live_execution_status: str = LIVE_NOT_SUPPORTED
    schema_version: str = FACTOR_HYPOTHESIS_SCHEMA_VERSION
    formula_sha256: str = field(init=False)
    hypothesis_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != FACTOR_HYPOTHESIS_SCHEMA_VERSION:
            raise FactorGovernanceError("unsupported FactorHypothesis schema")
        hypothesis_id = _identifier(self.hypothesis_id, "hypothesis_id")
        factor_id = _factor_id(self.factor_id)
        created_at = _aware(self.created_at, "created_at")
        information_cutoff_at = _aware(
            self.information_cutoff_at, "information_cutoff_at"
        )
        if information_cutoff_at > created_at:
            raise FactorGovernanceError(
                "information_cutoff_at cannot be after candidate creation"
            )
        formula = _text(self.formula, "formula")
        input_fields = _strings(
            self.input_fields, "input_fields", pattern=_FIELD_ID
        )
        input_schema_sha256 = _sha(
            self.input_schema_sha256, "input_schema_sha256"
        )
        prediction_target = _text(self.prediction_target, "prediction_target")
        if type(self.horizon_trading_days) is not int or self.horizon_trading_days <= 0:
            raise FactorGovernanceError(
                "horizon_trading_days must be a positive integer"
            )
        expected_sign = _text(self.expected_sign, "expected_sign").lower()
        if expected_sign not in {"positive", "negative"}:
            raise FactorGovernanceError(
                "expected_sign must be positive or negative"
            )
        universe_policy = _text(self.universe_policy, "universe_policy")
        benchmark_policy = _text(self.benchmark_policy, "benchmark_policy")
        economic_rationale = _text(self.economic_rationale, "economic_rationale")
        falsification_conditions = _strings(
            self.falsification_conditions, "falsification_conditions"
        )
        if self.status != LLM_CANDIDATE_STATUS:
            raise FactorGovernanceError(
                "FactorHypothesisV2 status is fixed at llm_research_candidate_only"
            )
        _false(self.paper_eligibility, "paper_eligibility")
        _false(self.trade_eligibility, "trade_eligibility")
        _false(self.real_money_list_allowed, "real_money_list_allowed")
        if self.live_execution_status != LIVE_NOT_SUPPORTED:
            raise FactorGovernanceError("LIVE is permanently unsupported")

        object.__setattr__(self, "hypothesis_id", hypothesis_id)
        object.__setattr__(self, "factor_id", factor_id)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "information_cutoff_at", information_cutoff_at)
        object.__setattr__(self, "formula", formula)
        object.__setattr__(self, "input_fields", input_fields)
        object.__setattr__(self, "input_schema_sha256", input_schema_sha256)
        object.__setattr__(self, "prediction_target", prediction_target)
        object.__setattr__(self, "expected_sign", expected_sign)
        object.__setattr__(self, "universe_policy", universe_policy)
        object.__setattr__(self, "benchmark_policy", benchmark_policy)
        object.__setattr__(self, "economic_rationale", economic_rationale)
        object.__setattr__(self, "falsification_conditions", falsification_conditions)
        object.__setattr__(
            self, "formula_sha256", canonical_sha256({"formula": formula})
        )
        object.__setattr__(
            self, "hypothesis_sha256", canonical_sha256(self.to_content_dict())
        )

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "hypothesis_id": self.hypothesis_id,
            "factor_id": self.factor_id,
            "created_at": _iso_datetime(self.created_at),
            "information_cutoff_at": _iso_datetime(self.information_cutoff_at),
            "formula": self.formula,
            "formula_sha256": self.formula_sha256,
            "input_fields": list(self.input_fields),
            "input_schema_sha256": self.input_schema_sha256,
            "prediction_target": self.prediction_target,
            "horizon_trading_days": self.horizon_trading_days,
            "expected_sign": self.expected_sign,
            "universe_policy": self.universe_policy,
            "benchmark_policy": self.benchmark_policy,
            "economic_rationale": self.economic_rationale,
            "falsification_conditions": list(self.falsification_conditions),
            "status": self.status,
            "paper_eligibility": self.paper_eligibility,
            "trade_eligibility": self.trade_eligibility,
            "real_money_list_allowed": self.real_money_list_allowed,
            "live_execution_status": self.live_execution_status,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.to_content_dict(), "hypothesis_sha256": self.hypothesis_sha256}

    def require_candidate(self, *, as_of: datetime) -> "FactorHypothesisV2":
        as_of_utc = _aware(as_of, "as_of")
        if self.created_at > as_of_utc or self.information_cutoff_at > as_of_utc:
            raise FactorGovernanceError("candidate contains a future timestamp")
        rebuilt = FactorHypothesisV2(
            hypothesis_id=self.hypothesis_id,
            factor_id=self.factor_id,
            created_at=self.created_at,
            information_cutoff_at=self.information_cutoff_at,
            formula=self.formula,
            input_fields=self.input_fields,
            input_schema_sha256=self.input_schema_sha256,
            prediction_target=self.prediction_target,
            horizon_trading_days=self.horizon_trading_days,
            expected_sign=self.expected_sign,
            universe_policy=self.universe_policy,
            benchmark_policy=self.benchmark_policy,
            economic_rationale=self.economic_rationale,
            falsification_conditions=self.falsification_conditions,
            status=self.status,
            paper_eligibility=self.paper_eligibility,
            trade_eligibility=self.trade_eligibility,
            real_money_list_allowed=self.real_money_list_allowed,
            live_execution_status=self.live_execution_status,
            schema_version=self.schema_version,
        )
        if rebuilt.to_dict() != self.to_dict():
            raise FactorGovernanceError("candidate self SHA-256 mismatch")
        return self

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any], *, as_of: datetime
    ) -> "FactorHypothesisV2":
        expected = frozenset(
            {
                "schema_version",
                "hypothesis_id",
                "factor_id",
                "created_at",
                "information_cutoff_at",
                "formula",
                "formula_sha256",
                "input_fields",
                "input_schema_sha256",
                "prediction_target",
                "horizon_trading_days",
                "expected_sign",
                "universe_policy",
                "benchmark_policy",
                "economic_rationale",
                "falsification_conditions",
                "status",
                "paper_eligibility",
                "trade_eligibility",
                "real_money_list_allowed",
                "live_execution_status",
                "hypothesis_sha256",
            }
        )
        data = _strict_mapping(payload, expected, "FactorHypothesisV2")
        hypothesis = cls(
            schema_version=cast(str, data["schema_version"]),
            hypothesis_id=cast(str, data["hypothesis_id"]),
            factor_id=cast(str, data["factor_id"]),
            created_at=_parse_datetime(data["created_at"], "created_at"),
            information_cutoff_at=_parse_datetime(
                data["information_cutoff_at"], "information_cutoff_at"
            ),
            formula=cast(str, data["formula"]),
            input_fields=tuple(cast(Sequence[str], data["input_fields"])),
            input_schema_sha256=cast(str, data["input_schema_sha256"]),
            prediction_target=cast(str, data["prediction_target"]),
            horizon_trading_days=cast(int, data["horizon_trading_days"]),
            expected_sign=cast(str, data["expected_sign"]),
            universe_policy=cast(str, data["universe_policy"]),
            benchmark_policy=cast(str, data["benchmark_policy"]),
            economic_rationale=cast(str, data["economic_rationale"]),
            falsification_conditions=tuple(
                cast(Sequence[str], data["falsification_conditions"])
            ),
            status=cast(str, data["status"]),
            paper_eligibility=cast(bool, data["paper_eligibility"]),
            trade_eligibility=cast(bool, data["trade_eligibility"]),
            real_money_list_allowed=cast(bool, data["real_money_list_allowed"]),
            live_execution_status=cast(str, data["live_execution_status"]),
        )
        if data["formula_sha256"] != hypothesis.formula_sha256:
            raise FactorGovernanceError("candidate formula SHA-256 mismatch")
        if data["hypothesis_sha256"] != hypothesis.hypothesis_sha256:
            raise FactorGovernanceError("candidate self SHA-256 mismatch")
        return hypothesis.require_candidate(as_of=as_of)


@dataclass(frozen=True, slots=True)
class FactorValidationReceiptV1:
    """Typed independent-validation evidence; no caller-supplied pass boolean."""

    receipt_id: str
    validator_id: str
    factor_id: str
    hypothesis_id: str
    hypothesis_sha256: str
    experiment_spec_sha256: str
    prediction_target: str
    horizon_trading_days: int
    universe_policy: str
    benchmark_policy: str
    hypothesis_created_at: datetime
    formula_sha256: str
    implementation_code_sha256: str
    input_schema_sha256: str
    validation_spec_sha256: str
    validation_dataset_sha256: str
    validation_code_sha256: str
    information_cutoff_at: datetime
    validation_started_at: datetime
    validation_completed_at: datetime
    validation_partition: str = VALIDATION_PARTITION
    result: str = VALIDATION_RESULT
    schema_version: str = FACTOR_VALIDATION_RECEIPT_SCHEMA_VERSION
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != FACTOR_VALIDATION_RECEIPT_SCHEMA_VERSION:
            raise FactorGovernanceError("unsupported validation receipt schema")
        receipt_id = _identifier(self.receipt_id, "receipt_id")
        validator_id = _identifier(self.validator_id, "validator_id")
        factor_id = _factor_id(self.factor_id)
        hypothesis_id = _identifier(self.hypothesis_id, "hypothesis_id")
        hypothesis_sha256 = _sha(self.hypothesis_sha256, "hypothesis_sha256")
        experiment_spec_sha256 = _sha(
            self.experiment_spec_sha256, "experiment_spec_sha256"
        )
        prediction_target = _text(self.prediction_target, "prediction_target")
        if type(self.horizon_trading_days) is not int or self.horizon_trading_days <= 0:
            raise FactorGovernanceError(
                "horizon_trading_days must be a positive integer"
            )
        universe_policy = _text(self.universe_policy, "universe_policy")
        benchmark_policy = _text(self.benchmark_policy, "benchmark_policy")
        hypothesis_created_at = _aware(
            self.hypothesis_created_at, "hypothesis_created_at"
        )
        hashes = {}
        for field_name in (
            "formula_sha256",
            "implementation_code_sha256",
            "input_schema_sha256",
            "validation_spec_sha256",
            "validation_dataset_sha256",
            "validation_code_sha256",
        ):
            hashes[field_name] = _sha(getattr(self, field_name), field_name)
        information_cutoff_at = _aware(
            self.information_cutoff_at, "information_cutoff_at"
        )
        validation_started_at = _aware(
            self.validation_started_at, "validation_started_at"
        )
        validation_completed_at = _aware(
            self.validation_completed_at, "validation_completed_at"
        )
        if not (
            information_cutoff_at
            <= hypothesis_created_at
            <= validation_started_at
            <= validation_completed_at
        ):
            raise FactorGovernanceError(
                "validation receipt timestamps are self-contradictory"
            )
        if self.validation_partition != VALIDATION_PARTITION:
            raise FactorGovernanceError(
                "validation receipt cannot consume a Locked Test partition"
            )
        if self.result != VALIDATION_RESULT:
            raise FactorGovernanceError(
                "validation receipt result is not an approved controlled result"
            )
        object.__setattr__(self, "receipt_id", receipt_id)
        object.__setattr__(self, "validator_id", validator_id)
        object.__setattr__(self, "factor_id", factor_id)
        object.__setattr__(self, "hypothesis_id", hypothesis_id)
        object.__setattr__(self, "hypothesis_sha256", hypothesis_sha256)
        object.__setattr__(self, "experiment_spec_sha256", experiment_spec_sha256)
        object.__setattr__(self, "prediction_target", prediction_target)
        object.__setattr__(self, "universe_policy", universe_policy)
        object.__setattr__(self, "benchmark_policy", benchmark_policy)
        object.__setattr__(self, "hypothesis_created_at", hypothesis_created_at)
        for field_name, value in hashes.items():
            object.__setattr__(self, field_name, value)
        object.__setattr__(self, "information_cutoff_at", information_cutoff_at)
        object.__setattr__(self, "validation_started_at", validation_started_at)
        object.__setattr__(self, "validation_completed_at", validation_completed_at)
        object.__setattr__(self, "receipt_sha256", canonical_sha256(self.to_content_dict()))

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "validator_id": self.validator_id,
            "factor_id": self.factor_id,
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_sha256": self.hypothesis_sha256,
            "experiment_spec_sha256": self.experiment_spec_sha256,
            "prediction_target": self.prediction_target,
            "horizon_trading_days": self.horizon_trading_days,
            "universe_policy": self.universe_policy,
            "benchmark_policy": self.benchmark_policy,
            "hypothesis_created_at": _iso_datetime(self.hypothesis_created_at),
            "formula_sha256": self.formula_sha256,
            "implementation_code_sha256": self.implementation_code_sha256,
            "input_schema_sha256": self.input_schema_sha256,
            "validation_spec_sha256": self.validation_spec_sha256,
            "validation_dataset_sha256": self.validation_dataset_sha256,
            "validation_code_sha256": self.validation_code_sha256,
            "information_cutoff_at": _iso_datetime(self.information_cutoff_at),
            "validation_started_at": _iso_datetime(self.validation_started_at),
            "validation_completed_at": _iso_datetime(self.validation_completed_at),
            "validation_partition": self.validation_partition,
            "result": self.result,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.to_content_dict(), "receipt_sha256": self.receipt_sha256}

    def require_valid(self, *, as_of: datetime) -> "FactorValidationReceiptV1":
        as_of_utc = _aware(as_of, "as_of")
        if self.validation_completed_at > as_of_utc:
            raise FactorGovernanceError("validation receipt contains a future timestamp")
        rebuilt = FactorValidationReceiptV1(
            receipt_id=self.receipt_id,
            validator_id=self.validator_id,
            factor_id=self.factor_id,
            hypothesis_id=self.hypothesis_id,
            hypothesis_sha256=self.hypothesis_sha256,
            experiment_spec_sha256=self.experiment_spec_sha256,
            prediction_target=self.prediction_target,
            horizon_trading_days=self.horizon_trading_days,
            universe_policy=self.universe_policy,
            benchmark_policy=self.benchmark_policy,
            hypothesis_created_at=self.hypothesis_created_at,
            formula_sha256=self.formula_sha256,
            implementation_code_sha256=self.implementation_code_sha256,
            input_schema_sha256=self.input_schema_sha256,
            validation_spec_sha256=self.validation_spec_sha256,
            validation_dataset_sha256=self.validation_dataset_sha256,
            validation_code_sha256=self.validation_code_sha256,
            information_cutoff_at=self.information_cutoff_at,
            validation_started_at=self.validation_started_at,
            validation_completed_at=self.validation_completed_at,
            validation_partition=self.validation_partition,
            result=self.result,
            schema_version=self.schema_version,
        )
        if rebuilt.to_dict() != self.to_dict():
            raise FactorGovernanceError("validation receipt self SHA-256 mismatch")
        return self

    @classmethod
    def from_hypothesis(
        cls,
        hypothesis: FactorHypothesisV2,
        *,
        receipt_id: str,
        validator_id: str,
        experiment_spec_sha256: str,
        implementation_code_sha256: str,
        validation_spec_sha256: str,
        validation_dataset_sha256: str,
        validation_code_sha256: str,
        validation_started_at: datetime,
        validation_completed_at: datetime,
    ) -> "FactorValidationReceiptV1":
        if not isinstance(hypothesis, FactorHypothesisV2):
            raise FactorGovernanceError(
                "validation receipt requires a FactorHypothesisV2 candidate"
            )
        hypothesis.require_candidate(as_of=validation_started_at)
        return cls(
            receipt_id=receipt_id,
            validator_id=validator_id,
            factor_id=hypothesis.factor_id,
            hypothesis_id=hypothesis.hypothesis_id,
            hypothesis_sha256=hypothesis.hypothesis_sha256,
            experiment_spec_sha256=experiment_spec_sha256,
            prediction_target=hypothesis.prediction_target,
            horizon_trading_days=hypothesis.horizon_trading_days,
            universe_policy=hypothesis.universe_policy,
            benchmark_policy=hypothesis.benchmark_policy,
            hypothesis_created_at=hypothesis.created_at,
            formula_sha256=hypothesis.formula_sha256,
            implementation_code_sha256=implementation_code_sha256,
            input_schema_sha256=hypothesis.input_schema_sha256,
            validation_spec_sha256=validation_spec_sha256,
            validation_dataset_sha256=validation_dataset_sha256,
            validation_code_sha256=validation_code_sha256,
            information_cutoff_at=hypothesis.information_cutoff_at,
            validation_started_at=validation_started_at,
            validation_completed_at=validation_completed_at,
        )

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any], *, as_of: datetime
    ) -> "FactorValidationReceiptV1":
        expected = frozenset(
            {
                "schema_version",
                "receipt_id",
                "validator_id",
                "factor_id",
                "hypothesis_id",
                "hypothesis_sha256",
                "experiment_spec_sha256",
                "prediction_target",
                "horizon_trading_days",
                "universe_policy",
                "benchmark_policy",
                "hypothesis_created_at",
                "formula_sha256",
                "implementation_code_sha256",
                "input_schema_sha256",
                "validation_spec_sha256",
                "validation_dataset_sha256",
                "validation_code_sha256",
                "information_cutoff_at",
                "validation_started_at",
                "validation_completed_at",
                "validation_partition",
                "result",
                "receipt_sha256",
            }
        )
        data = _strict_mapping(payload, expected, "FactorValidationReceiptV1")
        receipt = cls(
            schema_version=cast(str, data["schema_version"]),
            receipt_id=cast(str, data["receipt_id"]),
            validator_id=cast(str, data["validator_id"]),
            factor_id=cast(str, data["factor_id"]),
            hypothesis_id=cast(str, data["hypothesis_id"]),
            hypothesis_sha256=cast(str, data["hypothesis_sha256"]),
            experiment_spec_sha256=cast(str, data["experiment_spec_sha256"]),
            prediction_target=cast(str, data["prediction_target"]),
            horizon_trading_days=cast(int, data["horizon_trading_days"]),
            universe_policy=cast(str, data["universe_policy"]),
            benchmark_policy=cast(str, data["benchmark_policy"]),
            hypothesis_created_at=_parse_datetime(
                data["hypothesis_created_at"], "hypothesis_created_at"
            ),
            formula_sha256=cast(str, data["formula_sha256"]),
            implementation_code_sha256=cast(
                str, data["implementation_code_sha256"]
            ),
            input_schema_sha256=cast(str, data["input_schema_sha256"]),
            validation_spec_sha256=cast(str, data["validation_spec_sha256"]),
            validation_dataset_sha256=cast(
                str, data["validation_dataset_sha256"]
            ),
            validation_code_sha256=cast(str, data["validation_code_sha256"]),
            information_cutoff_at=_parse_datetime(
                data["information_cutoff_at"], "information_cutoff_at"
            ),
            validation_started_at=_parse_datetime(
                data["validation_started_at"], "validation_started_at"
            ),
            validation_completed_at=_parse_datetime(
                data["validation_completed_at"], "validation_completed_at"
            ),
            validation_partition=cast(str, data["validation_partition"]),
            result=cast(str, data["result"]),
        )
        if data["receipt_sha256"] != receipt.receipt_sha256:
            raise FactorGovernanceError("validation receipt self SHA-256 mismatch")
        return receipt.require_valid(as_of=as_of)


@dataclass(frozen=True, slots=True)
class ApprovedFactorV1:
    """One research-only factor bound to exact code, inputs and validation."""

    factor_id: str
    hypothesis_id: str
    hypothesis_sha256: str
    experiment_spec_sha256: str
    prediction_target: str
    horizon_trading_days: int
    universe_policy: str
    benchmark_policy: str
    formula_sha256: str
    implementation_code_sha256: str
    input_schema_sha256: str
    validation_receipt: FactorValidationReceiptV1
    approved_at: datetime
    approval_status: str = APPROVED_FACTOR_STATUS
    paper_eligibility: bool = False
    trade_eligibility: bool = False
    real_money_list_allowed: bool = False
    live_execution_status: str = LIVE_NOT_SUPPORTED
    approved_factor_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        factor_id = _factor_id(self.factor_id)
        hypothesis_id = _identifier(self.hypothesis_id, "hypothesis_id")
        hypothesis_sha256 = _sha(self.hypothesis_sha256, "hypothesis_sha256")
        experiment_spec_sha256 = _sha(
            self.experiment_spec_sha256, "experiment_spec_sha256"
        )
        prediction_target = _text(self.prediction_target, "prediction_target")
        if type(self.horizon_trading_days) is not int or self.horizon_trading_days <= 0:
            raise FactorGovernanceError(
                "horizon_trading_days must be a positive integer"
            )
        universe_policy = _text(self.universe_policy, "universe_policy")
        benchmark_policy = _text(self.benchmark_policy, "benchmark_policy")
        formula_sha256 = _sha(self.formula_sha256, "formula_sha256")
        implementation_code_sha256 = _sha(
            self.implementation_code_sha256, "implementation_code_sha256"
        )
        input_schema_sha256 = _sha(
            self.input_schema_sha256, "input_schema_sha256"
        )
        if not isinstance(self.validation_receipt, FactorValidationReceiptV1):
            if isinstance(self.validation_receipt, FactorHypothesisV2):
                raise FactorGovernanceError(
                    "candidate cannot be directly upgraded into an approved factor"
                )
            raise FactorGovernanceError(
                "approved factor requires a typed FactorValidationReceiptV1"
            )
        receipt = self.validation_receipt
        bindings = {
            "factor_id": (factor_id, receipt.factor_id),
            "hypothesis_id": (hypothesis_id, receipt.hypothesis_id),
            "hypothesis_sha256": (hypothesis_sha256, receipt.hypothesis_sha256),
            "experiment_spec_sha256": (
                experiment_spec_sha256,
                receipt.experiment_spec_sha256,
            ),
            "prediction_target": (prediction_target, receipt.prediction_target),
            "horizon_trading_days": (
                self.horizon_trading_days,
                receipt.horizon_trading_days,
            ),
            "universe_policy": (universe_policy, receipt.universe_policy),
            "benchmark_policy": (benchmark_policy, receipt.benchmark_policy),
            "formula_sha256": (formula_sha256, receipt.formula_sha256),
            "implementation_code_sha256": (
                implementation_code_sha256,
                receipt.implementation_code_sha256,
            ),
            "input_schema_sha256": (
                input_schema_sha256,
                receipt.input_schema_sha256,
            ),
        }
        mismatches = [name for name, pair in bindings.items() if pair[0] != pair[1]]
        if mismatches:
            raise FactorGovernanceError(
                "approved factor contradicts validation receipt: "
                + ", ".join(mismatches)
            )
        approved_at = _aware(self.approved_at, "approved_at")
        if approved_at < receipt.validation_completed_at:
            raise FactorGovernanceError(
                "approved_at cannot precede validation completion"
            )
        if self.approval_status != APPROVED_FACTOR_STATUS:
            raise FactorGovernanceError(
                "approved factor status is fixed at approved_for_frozen_research_only"
            )
        _false(self.paper_eligibility, "paper_eligibility")
        _false(self.trade_eligibility, "trade_eligibility")
        _false(self.real_money_list_allowed, "real_money_list_allowed")
        if self.live_execution_status != LIVE_NOT_SUPPORTED:
            raise FactorGovernanceError("LIVE is permanently unsupported")
        object.__setattr__(self, "factor_id", factor_id)
        object.__setattr__(self, "hypothesis_id", hypothesis_id)
        object.__setattr__(self, "hypothesis_sha256", hypothesis_sha256)
        object.__setattr__(self, "experiment_spec_sha256", experiment_spec_sha256)
        object.__setattr__(self, "prediction_target", prediction_target)
        object.__setattr__(self, "universe_policy", universe_policy)
        object.__setattr__(self, "benchmark_policy", benchmark_policy)
        object.__setattr__(self, "formula_sha256", formula_sha256)
        object.__setattr__(
            self, "implementation_code_sha256", implementation_code_sha256
        )
        object.__setattr__(self, "input_schema_sha256", input_schema_sha256)
        object.__setattr__(self, "approved_at", approved_at)
        object.__setattr__(
            self,
            "approved_factor_sha256",
            canonical_sha256(self.to_content_dict()),
        )

    @classmethod
    def from_validation_receipt(
        cls,
        receipt: FactorValidationReceiptV1,
        *,
        approved_at: datetime,
    ) -> "ApprovedFactorV1":
        if not isinstance(receipt, FactorValidationReceiptV1):
            raise FactorGovernanceError(
                "candidate direct upgrade is forbidden; validation receipt required"
            )
        receipt.require_valid(as_of=approved_at)
        return cls(
            factor_id=receipt.factor_id,
            hypothesis_id=receipt.hypothesis_id,
            hypothesis_sha256=receipt.hypothesis_sha256,
            experiment_spec_sha256=receipt.experiment_spec_sha256,
            prediction_target=receipt.prediction_target,
            horizon_trading_days=receipt.horizon_trading_days,
            universe_policy=receipt.universe_policy,
            benchmark_policy=receipt.benchmark_policy,
            formula_sha256=receipt.formula_sha256,
            implementation_code_sha256=receipt.implementation_code_sha256,
            input_schema_sha256=receipt.input_schema_sha256,
            validation_receipt=receipt,
            approved_at=approved_at,
        )

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "factor_id": self.factor_id,
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_sha256": self.hypothesis_sha256,
            "experiment_spec_sha256": self.experiment_spec_sha256,
            "prediction_target": self.prediction_target,
            "horizon_trading_days": self.horizon_trading_days,
            "universe_policy": self.universe_policy,
            "benchmark_policy": self.benchmark_policy,
            "formula_sha256": self.formula_sha256,
            "implementation_code_sha256": self.implementation_code_sha256,
            "input_schema_sha256": self.input_schema_sha256,
            "validation_receipt": self.validation_receipt.to_dict(),
            "approved_at": _iso_datetime(self.approved_at),
            "approval_status": self.approval_status,
            "paper_eligibility": self.paper_eligibility,
            "trade_eligibility": self.trade_eligibility,
            "real_money_list_allowed": self.real_money_list_allowed,
            "live_execution_status": self.live_execution_status,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.to_content_dict(),
            "approved_factor_sha256": self.approved_factor_sha256,
        }

    def require_valid(self, *, as_of: datetime) -> "ApprovedFactorV1":
        as_of_utc = _aware(as_of, "as_of")
        if self.approved_at > as_of_utc:
            raise FactorGovernanceError("approved factor contains a future timestamp")
        self.validation_receipt.require_valid(as_of=self.approved_at)
        rebuilt = ApprovedFactorV1(
            factor_id=self.factor_id,
            hypothesis_id=self.hypothesis_id,
            hypothesis_sha256=self.hypothesis_sha256,
            experiment_spec_sha256=self.experiment_spec_sha256,
            prediction_target=self.prediction_target,
            horizon_trading_days=self.horizon_trading_days,
            universe_policy=self.universe_policy,
            benchmark_policy=self.benchmark_policy,
            formula_sha256=self.formula_sha256,
            implementation_code_sha256=self.implementation_code_sha256,
            input_schema_sha256=self.input_schema_sha256,
            validation_receipt=self.validation_receipt,
            approved_at=self.approved_at,
            approval_status=self.approval_status,
            paper_eligibility=self.paper_eligibility,
            trade_eligibility=self.trade_eligibility,
            real_money_list_allowed=self.real_money_list_allowed,
            live_execution_status=self.live_execution_status,
        )
        if rebuilt.to_dict() != self.to_dict():
            raise FactorGovernanceError("approved factor self SHA-256 mismatch")
        return self

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any], *, as_of: datetime
    ) -> "ApprovedFactorV1":
        expected = frozenset(
            {
                "factor_id",
                "hypothesis_id",
                "hypothesis_sha256",
                "experiment_spec_sha256",
                "prediction_target",
                "horizon_trading_days",
                "universe_policy",
                "benchmark_policy",
                "formula_sha256",
                "implementation_code_sha256",
                "input_schema_sha256",
                "validation_receipt",
                "approved_at",
                "approval_status",
                "paper_eligibility",
                "trade_eligibility",
                "real_money_list_allowed",
                "live_execution_status",
                "approved_factor_sha256",
            }
        )
        data = _strict_mapping(payload, expected, "ApprovedFactorV1")
        approved_at = _parse_datetime(data["approved_at"], "approved_at")
        receipt = FactorValidationReceiptV1.from_dict(
            cast(Mapping[str, Any], data["validation_receipt"]), as_of=approved_at
        )
        factor = cls(
            factor_id=cast(str, data["factor_id"]),
            hypothesis_id=cast(str, data["hypothesis_id"]),
            hypothesis_sha256=cast(str, data["hypothesis_sha256"]),
            experiment_spec_sha256=cast(str, data["experiment_spec_sha256"]),
            prediction_target=cast(str, data["prediction_target"]),
            horizon_trading_days=cast(int, data["horizon_trading_days"]),
            universe_policy=cast(str, data["universe_policy"]),
            benchmark_policy=cast(str, data["benchmark_policy"]),
            formula_sha256=cast(str, data["formula_sha256"]),
            implementation_code_sha256=cast(
                str, data["implementation_code_sha256"]
            ),
            input_schema_sha256=cast(str, data["input_schema_sha256"]),
            validation_receipt=receipt,
            approved_at=approved_at,
            approval_status=cast(str, data["approval_status"]),
            paper_eligibility=cast(bool, data["paper_eligibility"]),
            trade_eligibility=cast(bool, data["trade_eligibility"]),
            real_money_list_allowed=cast(bool, data["real_money_list_allowed"]),
            live_execution_status=cast(str, data["live_execution_status"]),
        )
        if data["approved_factor_sha256"] != factor.approved_factor_sha256:
            raise FactorGovernanceError("approved factor self SHA-256 mismatch")
        return factor.require_valid(as_of=as_of)


@dataclass(frozen=True, slots=True)
class ApprovedFactorRegistryV1:
    """Deterministic snapshot of independently validated research factors."""

    registry_id: str
    frozen_at: datetime
    experiment_spec_sha256: str
    prediction_target: str
    horizon_trading_days: int
    universe_policy: str
    benchmark_policy: str
    factors: tuple[ApprovedFactorV1, ...]
    registry_status: str = REGISTRY_STATUS
    trust_boundary: str = REGISTRY_TRUST_BOUNDARY
    paper_eligibility: bool = False
    trade_eligibility: bool = False
    real_money_list_allowed: bool = False
    live_execution_status: str = LIVE_NOT_SUPPORTED
    schema_version: str = APPROVED_FACTOR_REGISTRY_SCHEMA_VERSION
    registry_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != APPROVED_FACTOR_REGISTRY_SCHEMA_VERSION:
            raise FactorGovernanceError("unsupported ApprovedFactorRegistry schema")
        registry_id = _identifier(self.registry_id, "registry_id")
        frozen_at = _aware(self.frozen_at, "frozen_at")
        experiment_spec_sha256 = _sha(
            self.experiment_spec_sha256, "experiment_spec_sha256"
        )
        prediction_target = _text(self.prediction_target, "prediction_target")
        if type(self.horizon_trading_days) is not int or self.horizon_trading_days <= 0:
            raise FactorGovernanceError(
                "horizon_trading_days must be a positive integer"
            )
        universe_policy = _text(self.universe_policy, "universe_policy")
        benchmark_policy = _text(self.benchmark_policy, "benchmark_policy")
        if not isinstance(self.factors, Sequence) or isinstance(
            self.factors, (str, bytes)
        ):
            raise FactorGovernanceError("factors must be an approved-factor array")
        factors = tuple(self.factors)
        if not factors:
            raise FactorGovernanceError("approved factor registry cannot be empty")
        if any(not isinstance(item, ApprovedFactorV1) for item in factors):
            if any(isinstance(item, FactorHypothesisV2) for item in factors):
                raise FactorGovernanceError(
                    "candidate cannot be directly upgraded into the registry"
                )
            raise FactorGovernanceError(
                "registry accepts only typed ApprovedFactorV1 entries"
            )
        factors = tuple(sorted(factors, key=lambda item: item.factor_id))
        factor_ids = tuple(item.factor_id for item in factors)
        formula_hashes = tuple(item.formula_sha256 for item in factors)
        receipt_hashes = tuple(
            item.validation_receipt.receipt_sha256 for item in factors
        )
        if len(set(factor_ids)) != len(factor_ids):
            raise FactorGovernanceError(
                "duplicate factor_id payloads are forbidden in a registry"
            )
        if len(set(formula_hashes)) != len(formula_hashes):
            raise FactorGovernanceError(
                "duplicate formula payloads under different factor_ids are forbidden"
            )
        if len(set(receipt_hashes)) != len(receipt_hashes):
            raise FactorGovernanceError(
                "a validation receipt cannot be replayed for multiple entries"
            )
        if self.registry_status != REGISTRY_STATUS:
            raise FactorGovernanceError(
                "registry status is fixed at approved_for_frozen_research_only"
            )
        if self.trust_boundary != REGISTRY_TRUST_BOUNDARY:
            raise FactorGovernanceError(
                "registry trust boundary cannot be widened by the caller"
            )
        _false(self.paper_eligibility, "paper_eligibility")
        _false(self.trade_eligibility, "trade_eligibility")
        _false(self.real_money_list_allowed, "real_money_list_allowed")
        if self.live_execution_status != LIVE_NOT_SUPPORTED:
            raise FactorGovernanceError("LIVE is permanently unsupported")
        for factor in factors:
            factor.require_valid(as_of=frozen_at)
            bindings = {
                "experiment_spec_sha256": experiment_spec_sha256,
                "prediction_target": prediction_target,
                "horizon_trading_days": self.horizon_trading_days,
                "universe_policy": universe_policy,
                "benchmark_policy": benchmark_policy,
            }
            mismatches = [
                name
                for name, expected in bindings.items()
                if getattr(factor, name) != expected
            ]
            if mismatches:
                raise FactorGovernanceError(
                    "registry factor policy binding mismatch: "
                    + ", ".join(mismatches)
                )
        object.__setattr__(self, "registry_id", registry_id)
        object.__setattr__(self, "frozen_at", frozen_at)
        object.__setattr__(self, "experiment_spec_sha256", experiment_spec_sha256)
        object.__setattr__(self, "prediction_target", prediction_target)
        object.__setattr__(self, "universe_policy", universe_policy)
        object.__setattr__(self, "benchmark_policy", benchmark_policy)
        object.__setattr__(self, "factors", factors)
        object.__setattr__(
            self, "registry_sha256", canonical_sha256(self.to_content_dict())
        )

    @property
    def approved_factor_ids(self) -> tuple[str, ...]:
        return tuple(item.factor_id for item in self.factors)

    def get(self, factor_id: str) -> ApprovedFactorV1:
        normalized = _factor_id(factor_id)
        for item in self.factors:
            if item.factor_id == normalized:
                return item
        raise FactorGovernanceError(f"factor_id is not approved: {normalized}")

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "registry_id": self.registry_id,
            "frozen_at": _iso_datetime(self.frozen_at),
            "experiment_spec_sha256": self.experiment_spec_sha256,
            "prediction_target": self.prediction_target,
            "horizon_trading_days": self.horizon_trading_days,
            "universe_policy": self.universe_policy,
            "benchmark_policy": self.benchmark_policy,
            "factors": [item.to_dict() for item in self.factors],
            "registry_status": self.registry_status,
            "trust_boundary": self.trust_boundary,
            "paper_eligibility": self.paper_eligibility,
            "trade_eligibility": self.trade_eligibility,
            "real_money_list_allowed": self.real_money_list_allowed,
            "live_execution_status": self.live_execution_status,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.to_content_dict(), "registry_sha256": self.registry_sha256}

    def require_valid(self, *, as_of: datetime) -> "ApprovedFactorRegistryV1":
        as_of_utc = _aware(as_of, "as_of")
        if self.frozen_at > as_of_utc:
            raise FactorGovernanceError("registry contains a future timestamp")
        rebuilt = ApprovedFactorRegistryV1(
            registry_id=self.registry_id,
            frozen_at=self.frozen_at,
            experiment_spec_sha256=self.experiment_spec_sha256,
            prediction_target=self.prediction_target,
            horizon_trading_days=self.horizon_trading_days,
            universe_policy=self.universe_policy,
            benchmark_policy=self.benchmark_policy,
            factors=self.factors,
            registry_status=self.registry_status,
            trust_boundary=self.trust_boundary,
            paper_eligibility=self.paper_eligibility,
            trade_eligibility=self.trade_eligibility,
            real_money_list_allowed=self.real_money_list_allowed,
            live_execution_status=self.live_execution_status,
            schema_version=self.schema_version,
        )
        if rebuilt.to_dict() != self.to_dict():
            raise FactorGovernanceError("registry self SHA-256 mismatch")
        return self

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any], *, as_of: datetime
    ) -> "ApprovedFactorRegistryV1":
        expected = frozenset(
            {
                "schema_version",
                "registry_id",
                "frozen_at",
                "experiment_spec_sha256",
                "prediction_target",
                "horizon_trading_days",
                "universe_policy",
                "benchmark_policy",
                "factors",
                "registry_status",
                "trust_boundary",
                "paper_eligibility",
                "trade_eligibility",
                "real_money_list_allowed",
                "live_execution_status",
                "registry_sha256",
            }
        )
        data = _strict_mapping(payload, expected, "ApprovedFactorRegistryV1")
        frozen_at = _parse_datetime(data["frozen_at"], "frozen_at")
        raw_factors = data["factors"]
        if not isinstance(raw_factors, Sequence) or isinstance(
            raw_factors, (str, bytes)
        ):
            raise FactorGovernanceError("factors must be an array")
        factors = tuple(
            ApprovedFactorV1.from_dict(
                cast(Mapping[str, Any], item), as_of=frozen_at
            )
            for item in raw_factors
        )
        registry = cls(
            schema_version=cast(str, data["schema_version"]),
            registry_id=cast(str, data["registry_id"]),
            frozen_at=frozen_at,
            experiment_spec_sha256=cast(str, data["experiment_spec_sha256"]),
            prediction_target=cast(str, data["prediction_target"]),
            horizon_trading_days=cast(int, data["horizon_trading_days"]),
            universe_policy=cast(str, data["universe_policy"]),
            benchmark_policy=cast(str, data["benchmark_policy"]),
            factors=factors,
            registry_status=cast(str, data["registry_status"]),
            trust_boundary=cast(str, data["trust_boundary"]),
            paper_eligibility=cast(bool, data["paper_eligibility"]),
            trade_eligibility=cast(bool, data["trade_eligibility"]),
            real_money_list_allowed=cast(bool, data["real_money_list_allowed"]),
            live_execution_status=cast(str, data["live_execution_status"]),
        )
        if data["registry_sha256"] != registry.registry_sha256:
            raise FactorGovernanceError("registry self SHA-256 mismatch")
        return registry.require_valid(as_of=as_of)


__all__ = [
    "APPROVED_FACTOR_REGISTRY_SCHEMA_VERSION",
    "APPROVED_FACTOR_STATUS",
    "FACTOR_HYPOTHESIS_SCHEMA_VERSION",
    "FACTOR_VALIDATION_RECEIPT_SCHEMA_VERSION",
    "LIVE_NOT_SUPPORTED",
    "LLM_CANDIDATE_STATUS",
    "REGISTRY_STATUS",
    "REGISTRY_TRUST_BOUNDARY",
    "VALIDATION_PARTITION",
    "VALIDATION_RESULT",
    "ApprovedFactorRegistryV1",
    "ApprovedFactorV1",
    "FactorGovernanceError",
    "FactorHypothesisV2",
    "FactorValidationReceiptV1",
    "canonical_json_bytes",
    "canonical_sha256",
]
