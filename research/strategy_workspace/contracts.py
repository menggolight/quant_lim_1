"""Frozen contracts for bounded, pre-outcome strategy discovery.

The objects in this module intentionally contain no labels, realised returns,
backtest scores, or winner fields.  Discovery is therefore structurally
separate from evaluation: a plan must be frozen before an evaluator can use it.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
from typing import Any, ClassVar, Mapping, Sequence


ALLOWED_LOOKBACK_DAYS = frozenset({20, 60, 120})
MAX_MECHANISMS_PER_THESIS = 2
MAX_FACTORS_PER_MECHANISM = 3
MAX_FACTORS_PER_PLAN = 6

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_FACTOR_ID_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_MECHANISM_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class DiscoveryContractError(ValueError):
    """Raised when a discovery object would widen or weaken the frozen policy."""


class ExpectedSign(str, Enum):
    """Pre-registered direction between a factor value and future return."""

    POSITIVE = "positive"
    NEGATIVE = "negative"


class DiscoveryStatus(str, Enum):
    """Explicit states; only ``FROZEN`` is eligible for downstream research."""

    CANDIDATES_GENERATED = "candidates_generated_not_frozen"
    FROZEN = "frozen_research_only"
    BLOCKED = "blocked"


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return _canonical_value(asdict(value))
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("canonical JSON mappings require string keys")
            normalized[key] = _canonical_value(item)
        return normalized
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        raise TypeError("unordered collections are not canonical JSON values")
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise DiscoveryContractError(
                "canonical JSON datetime values must include a timezone offset"
            )
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise DiscoveryContractError("canonical JSON decimals must be finite")
        return format(value, "f")
    if isinstance(value, float) and not math.isfinite(value):
        raise DiscoveryContractError("canonical JSON floats must be finite")
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON bytes without whitespace or NaN values."""

    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    """Hash the canonical JSON representation of ``value``."""

    return sha256(canonical_json_bytes(value)).hexdigest()


def _nonempty_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DiscoveryContractError(f"{field_name} must be a non-empty string")
    return value.strip()


def _string_sequence(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise DiscoveryContractError(f"{field_name} must be an ordered string array")
    if any(not isinstance(item, str) for item in value):
        raise DiscoveryContractError(f"{field_name} must contain only strings")
    return tuple(item.strip() for item in value)


@dataclass(frozen=True)
class ThesisSpec:
    """One subjective viewpoint translated into at most two mechanisms."""

    thesis_id: str
    viewpoint: str
    mechanisms: tuple[str, ...]
    horizon_days: int

    def __post_init__(self) -> None:
        thesis_id = _nonempty_text(self.thesis_id, "thesis_id")
        if _IDENTIFIER_RE.fullmatch(thesis_id) is None:
            raise DiscoveryContractError("thesis_id is not a valid identifier")
        viewpoint = _nonempty_text(self.viewpoint, "viewpoint")
        mechanisms = tuple(
            item.lower() for item in _string_sequence(self.mechanisms, "mechanisms")
        )
        if not 1 <= len(mechanisms) <= MAX_MECHANISMS_PER_THESIS:
            raise DiscoveryContractError(
                f"a thesis requires 1-{MAX_MECHANISMS_PER_THESIS} mechanisms"
            )
        if any(_MECHANISM_RE.fullmatch(item) is None for item in mechanisms):
            raise DiscoveryContractError("mechanisms contain an invalid path")
        if len(set(mechanisms)) != len(mechanisms):
            raise DiscoveryContractError("mechanisms must be unique")
        if type(self.horizon_days) is not int or self.horizon_days <= 0:
            raise DiscoveryContractError("horizon_days must be one positive integer")
        object.__setattr__(self, "thesis_id", thesis_id)
        object.__setattr__(self, "viewpoint", viewpoint)
        object.__setattr__(self, "mechanisms", mechanisms)

    @property
    def mechanism_paths(self) -> tuple[str, ...]:
        return self.mechanisms

    @property
    def statement(self) -> str:
        return self.viewpoint

    @property
    def content_sha256(self) -> str:
        return canonical_sha256(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "thesis_id": self.thesis_id,
            "viewpoint": self.viewpoint,
            "mechanisms": list(self.mechanisms),
            "horizon_days": self.horizon_days,
        }


@dataclass(frozen=True)
class FactorDefinition:
    """A catalog factor whose formula and direction are fixed before testing."""

    factor_id: str
    name: str
    mechanism_path: str
    lookback_days: int
    required_fields: tuple[str, ...]
    expected_sign: ExpectedSign
    formula: str
    description: str = ""

    def __post_init__(self) -> None:
        factor_id = _nonempty_text(self.factor_id, "factor_id").upper()
        if _FACTOR_ID_RE.fullmatch(factor_id) is None:
            raise DiscoveryContractError("factor_id is not a valid uppercase identifier")
        name = _nonempty_text(self.name, "name")
        mechanism_path = _nonempty_text(
            self.mechanism_path, "mechanism_path"
        ).lower()
        if _MECHANISM_RE.fullmatch(mechanism_path) is None:
            raise DiscoveryContractError("mechanism_path is invalid")
        if type(self.lookback_days) is not int or self.lookback_days not in ALLOWED_LOOKBACK_DAYS:
            raise DiscoveryContractError(
                "lookback_days must be exactly one of 20, 60, or 120"
            )
        required_fields = tuple(
            sorted(
                item.lower()
                for item in _string_sequence(self.required_fields, "required_fields")
            )
        )
        if not required_fields:
            raise DiscoveryContractError("required_fields must not be empty")
        if any(_FIELD_RE.fullmatch(item) is None for item in required_fields):
            raise DiscoveryContractError("required_fields contain an invalid field name")
        if len(set(required_fields)) != len(required_fields):
            raise DiscoveryContractError("required_fields must be unique")
        try:
            expected_sign = (
                self.expected_sign
                if isinstance(self.expected_sign, ExpectedSign)
                else ExpectedSign(str(self.expected_sign).strip().lower())
            )
        except ValueError as exc:
            raise DiscoveryContractError("expected_sign must be positive or negative") from exc
        formula = _nonempty_text(self.formula, "formula")
        description = str(self.description or "").strip()
        object.__setattr__(self, "factor_id", factor_id)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "mechanism_path", mechanism_path)
        object.__setattr__(self, "required_fields", required_fields)
        object.__setattr__(self, "expected_sign", expected_sign)
        object.__setattr__(self, "formula", formula)
        object.__setattr__(self, "description", description)

    @property
    def content_sha256(self) -> str:
        return canonical_sha256(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "factor_id": self.factor_id,
            "name": self.name,
            "mechanism_path": self.mechanism_path,
            "lookback_days": self.lookback_days,
            "required_fields": list(self.required_fields),
            "expected_sign": self.expected_sign.value,
            "formula": self.formula,
            "description": self.description,
        }


@dataclass(frozen=True)
class DiscoveryPlan:
    """A bounded factor family in a fail-closed discovery state."""

    SCHEMA_VERSION: ClassVar[str] = "strategy-workspace-discovery-plan.v1"

    thesis: ThesisSpec
    factors: tuple[FactorDefinition, ...]
    status: DiscoveryStatus = DiscoveryStatus.CANDIDATES_GENERATED
    blocked_reasons: tuple[str, ...] = ()
    plan_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.thesis, ThesisSpec):
            raise DiscoveryContractError("thesis must be a ThesisSpec")
        if not isinstance(self.factors, Sequence) or isinstance(
            self.factors, (str, bytes)
        ):
            raise DiscoveryContractError("factors must be an ordered FactorDefinition array")
        factors = tuple(self.factors)
        if any(not isinstance(item, FactorDefinition) for item in factors):
            raise DiscoveryContractError("factors must contain only FactorDefinition objects")
        if len(factors) > MAX_FACTORS_PER_PLAN:
            raise DiscoveryContractError(
                f"a discovery plan permits at most {MAX_FACTORS_PER_PLAN} factors"
            )
        factor_ids = [item.factor_id for item in factors]
        if len(set(factor_ids)) != len(factor_ids):
            raise DiscoveryContractError("factor_ids must be unique within a plan")
        thesis_mechanisms = set(self.thesis.mechanisms)
        if any(item.mechanism_path not in thesis_mechanisms for item in factors):
            raise DiscoveryContractError(
                "every factor mechanism_path must be declared by the thesis"
            )
        for mechanism_path in self.thesis.mechanisms:
            count = sum(
                item.mechanism_path == mechanism_path for item in factors
            )
            if count > MAX_FACTORS_PER_MECHANISM:
                raise DiscoveryContractError(
                    f"mechanism {mechanism_path!r} exceeds the "
                    f"{MAX_FACTORS_PER_MECHANISM}-factor cap"
                )
        mechanism_order = {
            path: index for index, path in enumerate(self.thesis.mechanisms)
        }
        factors = tuple(
            sorted(
                factors,
                key=lambda item: (
                    mechanism_order[item.mechanism_path],
                    item.factor_id,
                ),
            )
        )
        try:
            status = (
                self.status
                if isinstance(self.status, DiscoveryStatus)
                else DiscoveryStatus(str(self.status).strip())
            )
        except ValueError as exc:
            raise DiscoveryContractError("unknown discovery status") from exc
        blocked_reasons = tuple(
            sorted(
                set(
                    _nonempty_text(item, "blocked_reasons item")
                    for item in _string_sequence(
                        self.blocked_reasons, "blocked_reasons"
                    )
                )
            )
        )
        if status is DiscoveryStatus.BLOCKED:
            if not blocked_reasons:
                raise DiscoveryContractError("blocked plans require blocked_reasons")
        else:
            if blocked_reasons:
                raise DiscoveryContractError(
                    "non-blocked plans cannot contain blocked_reasons"
                )
            if not factors:
                raise DiscoveryContractError("generated or frozen plans require factors")
        object.__setattr__(self, "factors", factors)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "blocked_reasons", blocked_reasons)
        object.__setattr__(self, "plan_sha256", canonical_sha256(self.to_content_dict()))

    @property
    def factor_ids(self) -> tuple[str, ...]:
        return tuple(item.factor_id for item in self.factors)

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        return self.factor_ids

    @property
    def horizon_days(self) -> int:
        return self.thesis.horizon_days

    @property
    def is_frozen(self) -> bool:
        return self.status is DiscoveryStatus.FROZEN

    def require_frozen(self) -> "DiscoveryPlan":
        if not self.is_frozen:
            raise DiscoveryContractError(
                f"discovery plan is not frozen: {self.status.value}"
            )
        if canonical_sha256(self.to_content_dict()) != self.plan_sha256:
            raise DiscoveryContractError("discovery plan SHA-256 mismatch")
        return self

    def to_content_dict(self) -> dict[str, Any]:
        """Return the exact payload bound by ``plan_sha256``."""

        return {
            "schema_version": self.SCHEMA_VERSION,
            "thesis": self.thesis.to_dict(),
            "horizon_days": self.horizon_days,
            "factors": [item.to_dict() for item in self.factors],
            "status": self.status.value,
            "blocked_reasons": list(self.blocked_reasons),
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.to_content_dict()
        payload["plan_sha256"] = self.plan_sha256
        return payload


__all__ = [
    "ALLOWED_LOOKBACK_DAYS",
    "MAX_FACTORS_PER_MECHANISM",
    "MAX_FACTORS_PER_PLAN",
    "MAX_MECHANISMS_PER_THESIS",
    "DiscoveryContractError",
    "DiscoveryPlan",
    "DiscoveryStatus",
    "ExpectedSign",
    "FactorDefinition",
    "ThesisSpec",
    "canonical_json_bytes",
    "canonical_sha256",
]
