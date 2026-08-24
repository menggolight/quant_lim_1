"""Immutable Daily-to-next-session publication boundary.

The fixed registry, rather than a caller-held dataclass or a self-declared
hash, is the authority for every artifact used by the next-session adapter.
The current V1 contract deliberately has no Alpha-authorized state because
the formal Experiment V3 loader is not implemented.  It can only publish a
blocked daily record or a reduction-only record that is independently checked
to contain no BUY action.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from research.market_data.validation import SchemaValidationError, validate_json_schema
from trading.costs import FeeSchedule
from trading.integrity import account_fingerprint, execution_rule_bundle_sha256
from trading.models import (
    ADAPTIVE_EXPOSURE_V2_STRATEGY_ID,
    AccountSnapshot,
    InstrumentRule,
    Position,
)

from .contracts import canonical_json_bytes, canonical_sha256


DAILY_SIGNAL_ADMISSION_SCHEMA_VERSION = "daily-signal-admission-receipt.v1"
DAILY_SIGNAL_PUBLICATION_SCHEMA_VERSION = "daily-signal-publication-receipt.v1"
DAILY_SIGNAL_PUBLICATION_REGISTRY_ID = "adaptive-exposure-v2-daily-publication-registry.v1"
DAILY_SIGNAL_PUBLICATION_REGISTRY_ROOT = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "portfolio"
    / ADAPTIVE_EXPOSURE_V2_STRATEGY_ID
    / ".daily-signal-publication-registry.v1"
)
FORMAL_V3_LOADER_BLOCKED = "blocked_not_implemented"
_SCHEMA_ROOT = Path(__file__).resolve().parents[2] / "schemas"
_DAILY_DECISION_SCHEMA_PATH = _SCHEMA_ROOT / "daily_strategy_decision.v2.json"
_EXPOSURE_DECISION_SCHEMA_PATH = _SCHEMA_ROOT / "exposure_decision.v2.json"
_ALPHA_RANKING_SCHEMA_PATH = _SCHEMA_ROOT / "alpha_ranking.v2.json"
_EXPOSURE_TARGET_BY_STATE = {
    "RISK_OFF": Decimal("0"),
    "DEFENSIVE": Decimal("0.30"),
    "NEUTRAL": Decimal("0.60"),
    "RISK_ON": Decimal("1.00"),
}
_RISK_INTENT_TYPES = frozenset(
    {
        "RISK_OFF",
        "DEFENSIVE_REDUCTION",
        "NO_ALPHA_CASH",
        "ACCOUNT_DRAWDOWN_EXIT",
    }
)
_DAILY_SAFETY_FLAGS = {
    "returns_net_of_full_costs": True,
    "automatic_order_submission": False,
    "paper_eligibility": False,
    "trade_eligibility": False,
    "real_money_list_allowed": False,
    "live_supported": False,
}
_AUTHORITY_SAFETY_FLAGS = {
    "buy_allowed": False,
    "automatic_submission": False,
    "paper_eligibility": False,
    "trade_eligibility": False,
    "live_supported": False,
}

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_RISK_ARTIFACT_NAMES = frozenset(
    {
        "alpha-ranking",
        "alpha-model",
        "approved-factor-registry",
        "exposure-decision",
        "exposure-state",
        "portfolio-construction",
        "portfolio-intent",
        "exposure-policy",
        "constructor-policy",
        "experiment-v3-admission",
        "account-snapshot",
        "calendar-receipt",
        "calendar-registry",
        "execution-rule-bundle",
        "daily-decision",
        "authority-receipt",
        "failure-receipt",
    }
)
_BLOCKED_ARTIFACT_NAMES = frozenset(
    {
        "daily-decision",
        "authority-receipt",
        "failure-receipt",
        "received-input-commitments",
    }
)


def _artifact_names(authority: DailySignalAuthority) -> frozenset[str]:
    return (
        _RISK_ARTIFACT_NAMES
        if authority is DailySignalAuthority.RISK_REDUCTION_ONLY
        else _BLOCKED_ARTIFACT_NAMES
    )


class DailySignalPublicationError(ValueError):
    """Fail-closed daily publication error."""


class DailySignalPublicationConflict(DailySignalPublicationError):
    """A fixed registry slot already contains different bytes."""


class DailySignalAuthority(str, Enum):
    BLOCKED = "BLOCKED"
    RISK_REDUCTION_ONLY = "RISK_REDUCTION_ONLY"


def _aware(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise DailySignalPublicationError(f"{field_name} must be timezone-aware")
    return value


def _sha256(value: Any, field_name: str) -> str:
    text = str(value)
    if _SHA256_RE.fullmatch(text) is None:
        raise DailySignalPublicationError(f"{field_name} must be a lowercase SHA-256")
    return text


def _identifier(value: Any, field_name: str) -> str:
    text = str(value).strip()
    if _IDENTIFIER_RE.fullmatch(text) is None:
        raise DailySignalPublicationError(f"{field_name} is not a valid identifier")
    return text


def _decimal(value: Any, field_name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DailySignalPublicationError(f"{field_name} must be decimal") from exc
    if not parsed.is_finite():
        raise DailySignalPublicationError(f"{field_name} must be finite")
    return parsed


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DailySignalPublicationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _require_exact_keys(payload: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(payload)
    if actual != expected:
        raise DailySignalPublicationError(
            f"{label} keys differ: missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )


@dataclass(frozen=True, slots=True)
class DailySignalAdmissionReceiptV1:
    strategy_date: date
    execution_date: date | None
    frozen_at: datetime
    authority: DailySignalAuthority
    intent_type: str
    alpha_ranking_sha256: str
    model_sha256: str
    approved_factor_registry_sha256: str
    model_admission_receipt_sha256: str
    exposure_decision_sha256: str
    exposure_state_sha256: str
    exposure_state: str
    exposure_target_gross: Decimal
    construction_sha256: str
    intent_sha256: str
    exposure_policy_sha256: str
    constructor_policy_sha256: str
    combined_policy_sha256: str
    account_state_sha256: str
    account_fingerprint: str
    calendar_receipt_sha256: str
    calendar_registry_sha256: str
    execution_rule_bundle_sha256: str
    daily_decision_sha256: str
    experiment_admission_receipt_sha256: str
    formal_v3_loader_status: str = FORMAL_V3_LOADER_BLOCKED
    authority_receipt_sha256: str = ""
    failure_receipt_sha256: str | None = None
    strategy_id: str = ADAPTIVE_EXPOSURE_V2_STRATEGY_ID
    schema_version: str = DAILY_SIGNAL_ADMISSION_SCHEMA_VERSION
    admission_receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != DAILY_SIGNAL_ADMISSION_SCHEMA_VERSION:
            raise DailySignalPublicationError("unsupported daily admission schema")
        if self.strategy_id != ADAPTIVE_EXPOSURE_V2_STRATEGY_ID:
            raise DailySignalPublicationError("daily admission strategy_id mismatch")
        if type(self.strategy_date) is not date or (
            self.execution_date is not None and type(self.execution_date) is not date
        ):
            raise DailySignalPublicationError("daily admission dates are invalid")
        if self.execution_date is not None and self.execution_date <= self.strategy_date:
            raise DailySignalPublicationError("execution_date must follow strategy_date")
        object.__setattr__(self, "frozen_at", _aware(self.frozen_at, "frozen_at"))
        if not isinstance(self.authority, DailySignalAuthority):
            raise DailySignalPublicationError("authority must be DailySignalAuthority")
        intent_type = _identifier(self.intent_type, "intent_type")
        exposure_state = _identifier(self.exposure_state, "exposure_state")
        object.__setattr__(self, "intent_type", intent_type)
        object.__setattr__(self, "exposure_state", exposure_state)
        for field_name in (
            "alpha_ranking_sha256",
            "model_sha256",
            "approved_factor_registry_sha256",
            "model_admission_receipt_sha256",
            "exposure_decision_sha256",
            "exposure_state_sha256",
            "construction_sha256",
            "intent_sha256",
            "exposure_policy_sha256",
            "constructor_policy_sha256",
            "combined_policy_sha256",
            "account_state_sha256",
            "account_fingerprint",
            "calendar_receipt_sha256",
            "calendar_registry_sha256",
            "execution_rule_bundle_sha256",
            "daily_decision_sha256",
            "experiment_admission_receipt_sha256",
        ):
            object.__setattr__(self, field_name, _sha256(getattr(self, field_name), field_name))
        target = _decimal(self.exposure_target_gross, "exposure_target_gross")
        if not Decimal("0") <= target <= Decimal("1"):
            raise DailySignalPublicationError("exposure_target_gross must be in [0, 1]")
        object.__setattr__(self, "exposure_target_gross", target)
        if self.formal_v3_loader_status != FORMAL_V3_LOADER_BLOCKED:
            raise DailySignalPublicationError(
                "Daily publication v1 cannot claim a completed formal V3 loader"
            )
        object.__setattr__(
            self,
            "authority_receipt_sha256",
            _sha256(self.authority_receipt_sha256, "authority_receipt_sha256"),
        )
        failure_hash = (
            None
            if self.failure_receipt_sha256 is None
            else _sha256(self.failure_receipt_sha256, "failure_receipt_sha256")
        )
        if self.authority is DailySignalAuthority.BLOCKED and failure_hash is None:
            raise DailySignalPublicationError(
                "blocked daily admission requires a failure receipt"
            )
        object.__setattr__(self, "failure_receipt_sha256", failure_hash)
        if self.authority is DailySignalAuthority.RISK_REDUCTION_ONLY:
            if self.execution_date is None:
                raise DailySignalPublicationError(
                    "reduction-only admission requires an exact execution_date"
                )
            if self.intent_type not in {
                "RISK_OFF",
                "DEFENSIVE_REDUCTION",
                "NO_ALPHA_CASH",
                "ACCOUNT_DRAWDOWN_EXIT",
            }:
                raise DailySignalPublicationError(
                    "reduction-only admission requires a frozen risk intent"
                )
        object.__setattr__(
            self,
            "admission_receipt_sha256",
            canonical_sha256(self.to_content_dict()),
        )

    @property
    def next_session_allowed(self) -> bool:
        return self.authority is DailySignalAuthority.RISK_REDUCTION_ONLY

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "strategy_id": self.strategy_id,
            "strategy_date": self.strategy_date,
            "execution_date": self.execution_date,
            "frozen_at": self.frozen_at,
            "authority": self.authority.value,
            "intent_type": self.intent_type,
            "alpha_ranking_sha256": self.alpha_ranking_sha256,
            "model_sha256": self.model_sha256,
            "approved_factor_registry_sha256": self.approved_factor_registry_sha256,
            "model_admission_receipt_sha256": self.model_admission_receipt_sha256,
            "exposure_decision_sha256": self.exposure_decision_sha256,
            "exposure_state_sha256": self.exposure_state_sha256,
            "exposure_state": self.exposure_state,
            "exposure_target_gross": self.exposure_target_gross,
            "construction_sha256": self.construction_sha256,
            "intent_sha256": self.intent_sha256,
            "exposure_policy_sha256": self.exposure_policy_sha256,
            "constructor_policy_sha256": self.constructor_policy_sha256,
            "combined_policy_sha256": self.combined_policy_sha256,
            "account_state_sha256": self.account_state_sha256,
            "account_fingerprint": self.account_fingerprint,
            "calendar_receipt_sha256": self.calendar_receipt_sha256,
            "calendar_registry_sha256": self.calendar_registry_sha256,
            "execution_rule_bundle_sha256": self.execution_rule_bundle_sha256,
            "daily_decision_sha256": self.daily_decision_sha256,
            "experiment_admission_receipt_sha256": self.experiment_admission_receipt_sha256,
            "formal_v3_loader_status": self.formal_v3_loader_status,
            "authority_receipt_sha256": self.authority_receipt_sha256,
            "failure_receipt_sha256": self.failure_receipt_sha256,
            "next_session_allowed": self.next_session_allowed,
            "automatic_submission": False,
            "paper_eligibility": False,
            "trade_eligibility": False,
            "live_supported": False,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.to_content_dict(),
            "admission_receipt_sha256": self.admission_receipt_sha256,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DailySignalAdmissionReceiptV1":
        expected = set(cls._content_keys()) | {"admission_receipt_sha256"}
        _require_exact_keys(payload, expected, "daily admission receipt")
        if (
            payload.get("next_session_allowed")
            != (payload.get("authority") == DailySignalAuthority.RISK_REDUCTION_ONLY.value)
            or payload.get("automatic_submission") is not False
            or payload.get("paper_eligibility") is not False
            or payload.get("trade_eligibility") is not False
            or payload.get("live_supported") is not False
        ):
            raise DailySignalPublicationError("daily admission safety boundary drifted")
        try:
            receipt = cls(
                strategy_date=date.fromisoformat(str(payload["strategy_date"])),
                execution_date=(
                    None
                    if payload["execution_date"] is None
                    else date.fromisoformat(str(payload["execution_date"]))
                ),
                frozen_at=datetime.fromisoformat(str(payload["frozen_at"])),
                authority=DailySignalAuthority(str(payload["authority"])),
                intent_type=str(payload["intent_type"]),
                alpha_ranking_sha256=str(payload["alpha_ranking_sha256"]),
                model_sha256=str(payload["model_sha256"]),
                approved_factor_registry_sha256=str(payload["approved_factor_registry_sha256"]),
                model_admission_receipt_sha256=str(payload["model_admission_receipt_sha256"]),
                exposure_decision_sha256=str(payload["exposure_decision_sha256"]),
                exposure_state_sha256=str(payload["exposure_state_sha256"]),
                exposure_state=str(payload["exposure_state"]),
                exposure_target_gross=_decimal(payload["exposure_target_gross"], "exposure_target_gross"),
                construction_sha256=str(payload["construction_sha256"]),
                intent_sha256=str(payload["intent_sha256"]),
                exposure_policy_sha256=str(payload["exposure_policy_sha256"]),
                constructor_policy_sha256=str(payload["constructor_policy_sha256"]),
                combined_policy_sha256=str(payload["combined_policy_sha256"]),
                account_state_sha256=str(payload["account_state_sha256"]),
                account_fingerprint=str(payload["account_fingerprint"]),
                calendar_receipt_sha256=str(payload["calendar_receipt_sha256"]),
                calendar_registry_sha256=str(payload["calendar_registry_sha256"]),
                execution_rule_bundle_sha256=str(payload["execution_rule_bundle_sha256"]),
                daily_decision_sha256=str(payload["daily_decision_sha256"]),
                experiment_admission_receipt_sha256=str(payload["experiment_admission_receipt_sha256"]),
                formal_v3_loader_status=str(payload["formal_v3_loader_status"]),
                authority_receipt_sha256=str(payload["authority_receipt_sha256"]),
                failure_receipt_sha256=(
                    None
                    if payload["failure_receipt_sha256"] is None
                    else str(payload["failure_receipt_sha256"])
                ),
                strategy_id=str(payload["strategy_id"]),
                schema_version=str(payload["schema_version"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DailySignalPublicationError("daily admission receipt is malformed") from exc
        if receipt.admission_receipt_sha256 != _sha256(
            payload["admission_receipt_sha256"], "admission_receipt_sha256"
        ):
            raise DailySignalPublicationError("daily admission receipt hash mismatch")
        return receipt

    @staticmethod
    def _content_keys() -> tuple[str, ...]:
        return (
            "schema_version", "strategy_id", "strategy_date", "execution_date",
            "frozen_at", "authority", "intent_type", "alpha_ranking_sha256",
            "model_sha256", "approved_factor_registry_sha256",
            "model_admission_receipt_sha256", "exposure_decision_sha256",
            "exposure_state_sha256", "exposure_state", "exposure_target_gross",
            "construction_sha256", "intent_sha256", "exposure_policy_sha256",
            "constructor_policy_sha256", "combined_policy_sha256",
            "account_state_sha256", "account_fingerprint",
            "calendar_receipt_sha256", "calendar_registry_sha256",
            "execution_rule_bundle_sha256", "daily_decision_sha256",
            "experiment_admission_receipt_sha256", "formal_v3_loader_status",
            "authority_receipt_sha256", "failure_receipt_sha256",
            "next_session_allowed",
            "automatic_submission", "paper_eligibility", "trade_eligibility",
            "live_supported",
        )


@dataclass(frozen=True, slots=True)
class DailySignalPublicationReceiptV1:
    strategy_date: date
    execution_date: date | None
    published_at: datetime
    authority: DailySignalAuthority
    admission_receipt_sha256: str
    daily_decision_sha256: str
    artifact_sha256s: Mapping[str, str]
    registry_id: str = DAILY_SIGNAL_PUBLICATION_REGISTRY_ID
    schema_version: str = DAILY_SIGNAL_PUBLICATION_SCHEMA_VERSION
    publication_receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != DAILY_SIGNAL_PUBLICATION_SCHEMA_VERSION:
            raise DailySignalPublicationError("unsupported daily publication schema")
        if self.registry_id != DAILY_SIGNAL_PUBLICATION_REGISTRY_ID:
            raise DailySignalPublicationError("daily publication registry_id mismatch")
        if type(self.strategy_date) is not date or (
            self.execution_date is not None and type(self.execution_date) is not date
        ):
            raise DailySignalPublicationError("daily publication dates are invalid")
        object.__setattr__(self, "published_at", _aware(self.published_at, "published_at"))
        if not isinstance(self.authority, DailySignalAuthority):
            raise DailySignalPublicationError("publication authority is invalid")
        object.__setattr__(
            self,
            "admission_receipt_sha256",
            _sha256(self.admission_receipt_sha256, "admission_receipt_sha256"),
        )
        object.__setattr__(
            self,
            "daily_decision_sha256",
            _sha256(self.daily_decision_sha256, "daily_decision_sha256"),
        )
        hashes = {str(key): _sha256(value, f"artifact_sha256s.{key}") for key, value in self.artifact_sha256s.items()}
        if set(hashes) != _artifact_names(self.authority):
            raise DailySignalPublicationError("daily publication artifact set is incomplete or extra")
        object.__setattr__(self, "artifact_sha256s", MappingProxyType(dict(sorted(hashes.items()))))
        object.__setattr__(
            self,
            "publication_receipt_sha256",
            canonical_sha256(self.to_content_dict()),
        )

    @property
    def next_session_allowed(self) -> bool:
        return self.authority is DailySignalAuthority.RISK_REDUCTION_ONLY

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "registry_id": self.registry_id,
            "strategy_date": self.strategy_date,
            "execution_date": self.execution_date,
            "published_at": self.published_at,
            "authority": self.authority.value,
            "admission_receipt_sha256": self.admission_receipt_sha256,
            "daily_decision_sha256": self.daily_decision_sha256,
            "artifact_sha256s": dict(self.artifact_sha256s),
            "next_session_allowed": self.next_session_allowed,
            "immutable_create_only": True,
            "automatic_submission": False,
            "paper_eligibility": False,
            "trade_eligibility": False,
            "live_supported": False,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.to_content_dict(),
            "publication_receipt_sha256": self.publication_receipt_sha256,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DailySignalPublicationReceiptV1":
        expected = {
            "schema_version", "registry_id", "strategy_date", "execution_date",
            "published_at", "authority", "admission_receipt_sha256",
            "daily_decision_sha256", "artifact_sha256s", "next_session_allowed",
            "immutable_create_only", "automatic_submission", "paper_eligibility",
            "trade_eligibility", "live_supported", "publication_receipt_sha256",
        }
        _require_exact_keys(payload, expected, "daily publication receipt")
        if (
            payload.get("next_session_allowed")
            != (payload.get("authority") == DailySignalAuthority.RISK_REDUCTION_ONLY.value)
            or payload.get("immutable_create_only") is not True
            or payload.get("automatic_submission") is not False
            or payload.get("paper_eligibility") is not False
            or payload.get("trade_eligibility") is not False
            or payload.get("live_supported") is not False
            or not isinstance(payload.get("artifact_sha256s"), Mapping)
        ):
            raise DailySignalPublicationError("daily publication safety boundary drifted")
        try:
            receipt = cls(
                strategy_date=date.fromisoformat(str(payload["strategy_date"])),
                execution_date=(
                    None
                    if payload["execution_date"] is None
                    else date.fromisoformat(str(payload["execution_date"]))
                ),
                published_at=datetime.fromisoformat(str(payload["published_at"])),
                authority=DailySignalAuthority(str(payload["authority"])),
                admission_receipt_sha256=str(payload["admission_receipt_sha256"]),
                daily_decision_sha256=str(payload["daily_decision_sha256"]),
                artifact_sha256s={str(key): str(value) for key, value in payload["artifact_sha256s"].items()},
                registry_id=str(payload["registry_id"]),
                schema_version=str(payload["schema_version"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DailySignalPublicationError("daily publication receipt is malformed") from exc
        if receipt.publication_receipt_sha256 != _sha256(
            payload["publication_receipt_sha256"], "publication_receipt_sha256"
        ):
            raise DailySignalPublicationError("daily publication receipt hash mismatch")
        return receipt


@dataclass(frozen=True, slots=True)
class LoadedDailySignalPublicationV1:
    admission: DailySignalAdmissionReceiptV1
    publication: DailySignalPublicationReceiptV1
    artifacts: Mapping[str, Mapping[str, Any]]


def _registry_entry_directory(strategy_date: date) -> Path:
    if type(strategy_date) is not date:
        raise DailySignalPublicationError("strategy_date must be date")
    root = Path(DAILY_SIGNAL_PUBLICATION_REGISTRY_ROOT)
    if root.is_symlink():
        raise DailySignalPublicationError("daily publication registry root must not be symlink")
    return root.resolve(strict=False) / strategy_date.isoformat()


def _write_immutable(path: Path, payload: bytes) -> None:
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise DailySignalPublicationError(
            "immutable daily publication parent is absent or unsafe"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise DailySignalPublicationError("daily publication registry entry must not be symlink")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        try:
            existing = path.read_bytes()
        except OSError as read_exc:
            raise DailySignalPublicationConflict("cannot verify immutable daily publication") from read_exc
        if existing == payload and path.is_file() and not path.is_symlink():
            return
        raise DailySignalPublicationConflict(
            f"immutable daily publication differs: {path.name}"
        ) from exc
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _read_canonical_object(path: Path, label: str) -> tuple[bytes, Mapping[str, Any]]:
    if not path.is_file() or path.is_symlink():
        raise DailySignalPublicationError(f"{label} is absent or unsafe in fixed registry")
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
    except DailySignalPublicationError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DailySignalPublicationError(f"cannot read {label} from fixed registry") from exc
    if not isinstance(payload, Mapping) or raw != canonical_json_bytes(payload) + b"\n":
        raise DailySignalPublicationError(f"{label} bytes are not canonical")
    return raw, payload


def _validate_versioned_schema(
    payload: Mapping[str, Any],
    *,
    schema_path: Path,
    label: str,
) -> None:
    try:
        validate_json_schema(payload, schema_path)
    except SchemaValidationError as exc:
        raise DailySignalPublicationError(
            f"{label} does not satisfy its frozen JSON Schema: {exc}"
        ) from exc


def _validate_authority_receipt_semantics(
    admission: DailySignalAdmissionReceiptV1,
    authority: Mapping[str, Any],
) -> None:
    expected_execution_date = (
        None
        if admission.execution_date is None
        else admission.execution_date.isoformat()
    )
    common_mismatches = (
        authority.get("schema_version") != "daily-signal-authority-receipt.v1",
        authority.get("strategy_id") != ADAPTIVE_EXPOSURE_V2_STRATEGY_ID,
        str(authority.get("strategy_date")) != admission.strategy_date.isoformat(),
        authority.get("execution_date") != expected_execution_date,
        str(authority.get("frozen_at")) != admission.frozen_at.isoformat(),
        authority.get("authority") != admission.authority.value,
        authority.get("intent_type") != admission.intent_type,
        authority.get("formal_v3_loader_status") != FORMAL_V3_LOADER_BLOCKED,
        any(authority.get(key) is not value for key, value in _AUTHORITY_SAFETY_FLAGS.items()),
    )
    if any(common_mismatches):
        raise DailySignalPublicationError(
            "daily authority receipt identity or safety boundary drifted"
        )
    if admission.authority is DailySignalAuthority.RISK_REDUCTION_ONLY:
        if (
            authority.get("construction_sha256") != admission.construction_sha256
            or "failure_receipt_sha256" in authority
        ):
            raise DailySignalPublicationError(
                "risk authority receipt is not bound to the frozen construction"
            )
    elif (
        authority.get("failure_receipt_sha256") is not None
        and authority.get("failure_receipt_sha256")
        != admission.failure_receipt_sha256
    ):
        raise DailySignalPublicationError(
            "blocked authority receipt failure binding drifted"
        )


def _validate_daily_decision_semantics(
    admission: DailySignalAdmissionReceiptV1,
    daily: Mapping[str, Any],
    failure: Mapping[str, Any],
    authority: Mapping[str, Any],
) -> None:
    _validate_versioned_schema(
        daily,
        schema_path=_DAILY_DECISION_SCHEMA_PATH,
        label="daily-decision",
    )
    if canonical_sha256(
        {key: value for key, value in daily.items() if key != "decision_sha256"}
    ) != admission.daily_decision_sha256:
        raise DailySignalPublicationError("daily decision embedded hash does not verify")
    if any(daily.get(key) is not value for key, value in _DAILY_SAFETY_FLAGS.items()):
        raise DailySignalPublicationError("daily decision safety flags drifted")
    expected_execution_date = (
        None
        if admission.execution_date is None
        else admission.execution_date.isoformat()
    )
    if (
        daily.get("schema_version") != "daily-strategy-decision.v2"
        or daily.get("strategy_id") != ADAPTIVE_EXPOSURE_V2_STRATEGY_ID
        or str(daily.get("strategy_date")) != admission.strategy_date.isoformat()
        or daily.get("execution_date") != expected_execution_date
        or daily.get("portfolio_intent_type") != admission.intent_type
        or daily.get("model_sha256") != admission.model_sha256
        or daily.get("policy_sha256") != admission.combined_policy_sha256
        or daily.get("intent_sha256") != admission.intent_sha256
    ):
        raise DailySignalPublicationError(
            "daily decision identity/model/policy/intent binding drifted"
        )
    _validate_authority_receipt_semantics(admission, authority)

    status = str(daily.get("decision_status"))
    data_status = str(daily.get("data_status"))
    daily_failure_codes = daily.get("failure_codes")
    receipt_failure_codes = failure.get("failure_codes")
    if admission.authority is DailySignalAuthority.BLOCKED:
        failure_schema = failure.get("schema_version")
        blocked_failure_safe = (
            failure.get("buy_allowed") is False
            if failure_schema != "daily-pipeline-failure-receipt.v1"
            else (
                failure.get("orders_allowed") is False
                and failure.get("automatic_submission") is False
                and failure.get("paper_eligibility") is False
                and failure.get("trade_eligibility") is False
                and failure.get("live_supported") is False
            )
        )
        if (
            status != "BLOCKED"
            or data_status
            not in {"DATA_UPDATE_FAILED", "DATA_FAIL_CLOSED", "MODEL_ADMISSION_BLOCKED"}
            or daily.get("failed_stage") != failure.get("failed_stage")
            or not isinstance(daily_failure_codes, list)
            or daily_failure_codes != receipt_failure_codes
            or daily.get("failure_receipt_sha256")
            != admission.failure_receipt_sha256
            or failure.get("failure_receipt_sha256")
            != admission.failure_receipt_sha256
            or not blocked_failure_safe
        ):
            raise DailySignalPublicationError(
                "blocked authority is not backed by one formal BLOCKED Daily decision"
            )
        return

    if status not in {
        "READY_FOR_NEXT_SESSION_REVIEW",
        "NO_TRADE",
        "DATA_FAIL_CLOSED",
    }:
        raise DailySignalPublicationError(
            "risk authority cannot be attached to this Daily decision status"
        )
    if daily.get("portfolio_intent_type") not in _RISK_INTENT_TYPES:
        raise DailySignalPublicationError(
            "risk authority requires a formal risk-reduction intent type"
        )
    if data_status not in {"CONTROLLED_PIT_OK", "NO_ELIGIBLE_ALPHA", "DATA_FAIL_CLOSED"}:
        raise DailySignalPublicationError(
            "risk authority has an unsupported Daily data_status"
        )
    if (status == "DATA_FAIL_CLOSED") != (data_status == "DATA_FAIL_CLOSED"):
        raise DailySignalPublicationError(
            "Daily DATA_FAIL_CLOSED status and data_status must agree"
        )
    buy_orders = daily.get("buy_orders")
    sell_orders = daily.get("sell_orders")
    if buy_orders != [] or not isinstance(sell_orders, list):
        raise DailySignalPublicationError(
            "risk Daily decision must contain no BUY and a typed SELL list"
        )
    if status == "READY_FOR_NEXT_SESSION_REVIEW" and not sell_orders:
        raise DailySignalPublicationError(
            "ready risk Daily decision requires at least one frozen SELL"
        )
    if status == "NO_TRADE" and sell_orders:
        raise DailySignalPublicationError(
            "NO_TRADE risk Daily decision cannot contain SELL orders"
        )
    if (
        daily.get("failed_stage") is not None
        or daily_failure_codes != []
        or daily.get("failure_receipt_sha256") is not None
    ):
        raise DailySignalPublicationError(
            "risk Daily decision cannot carry BLOCKED failure fields"
        )
    failure_content = {
        key: value for key, value in failure.items() if key != "failure_receipt_sha256"
    }
    if (
        failure.get("schema_version")
        != "daily-signal-publication-failure-receipt.v1"
        or failure.get("strategy_id") != ADAPTIVE_EXPOSURE_V2_STRATEGY_ID
        or str(failure.get("strategy_date")) != admission.strategy_date.isoformat()
        or failure.get("failed_stage") is not None
        or receipt_failure_codes != []
        or failure.get("authority_receipt_sha256")
        != admission.authority_receipt_sha256
        or failure.get("orders_allowed") is not True
        or failure.get("buy_allowed") is not False
        or canonical_sha256(failure_content)
        != failure.get("failure_receipt_sha256")
    ):
        raise DailySignalPublicationError(
            "risk publication failure receipt is not the formal no-failure receipt"
        )


def _validate_exposure_intent_graph(
    admission: DailySignalAdmissionReceiptV1,
    artifacts: Mapping[str, Mapping[str, Any]],
) -> None:
    exposure = artifacts["exposure-decision"]
    _validate_versioned_schema(
        exposure,
        schema_path=_EXPOSURE_DECISION_SCHEMA_PATH,
        label="exposure-decision",
    )
    state = str(exposure.get("state"))
    if state not in _EXPOSURE_TARGET_BY_STATE:
        raise DailySignalPublicationError("exposure decision state is unsupported")
    exposure_target = _decimal(
        exposure.get("target_gross_exposure"), "exposure target"
    )
    if exposure_target != _EXPOSURE_TARGET_BY_STATE[state]:
        raise DailySignalPublicationError(
            "exposure state does not match the frozen target map"
        )
    construction = artifacts["portfolio-construction"]
    intent = artifacts["portfolio-intent"]
    daily = artifacts["daily-decision"]
    ranking = artifacts["alpha-ranking"]
    _validate_versioned_schema(
        ranking,
        schema_path=_ALPHA_RANKING_SCHEMA_PATH,
        label="alpha-ranking",
    )
    rows = ranking.get("rows")
    assert isinstance(rows, list)  # guaranteed by the frozen Schema above
    row_ids = [str(item["instrument_id"]) for item in rows]
    eligible_count = sum(item.get("eligibility") is True for item in rows)
    ranking_status = str(ranking.get("status"))
    if (
        len(row_ids) != len(set(row_ids))
        or any(item.get("decision_at") != ranking.get("decision_at") for item in rows)
        or ranking.get("eligible_count") != eligible_count
        or (ranking_status == "OK" and eligible_count == 0)
        or (
            ranking_status in {"NO_ALPHA_CASH", "DATA_FAIL_CLOSED"}
            and eligible_count != 0
        )
    ):
        raise DailySignalPublicationError(
            "alpha-ranking formal universe/status semantics drifted"
        )
    current_gross = _decimal(
        construction.get("current_gross_exposure"), "construction current gross"
    )
    construction_target = _decimal(
        construction.get("target_gross_exposure"), "construction target"
    )
    intent_target = _decimal(intent.get("target_gross_exposure"), "intent target")
    daily_target = _decimal(daily.get("target_gross_exposure"), "daily target")
    intent_type = str(intent.get("intent_type"))
    if (
        daily.get("market_regime") != state
        or admission.exposure_state != state
        or admission.exposure_target_gross != exposure_target
        or intent_type != admission.intent_type
        or construction.get("intent_type") != intent_type
        or daily.get("portfolio_intent_type") != intent_type
        or not (construction_target == intent_target == daily_target)
    ):
        raise DailySignalPublicationError(
            "ExposureDecision, construction, intent, and Daily decision graph drifted"
        )

    # This mirrors operations.daily_pipeline._requested_intent.  Account
    # drawdown is intentionally checked first: formal Alpha admission may be
    # blocked, but a zero-target account-risk exit must remain publishable.
    daily_data_failure = daily.get("data_status") == "DATA_FAIL_CLOSED"
    if intent_type == "ACCOUNT_DRAWDOWN_EXIT":
        expected_intent = "ACCOUNT_DRAWDOWN_EXIT"
        expected_target = Decimal("0")
        if state != "RISK_OFF":
            raise DailySignalPublicationError(
                "ACCOUNT_DRAWDOWN_EXIT requires a RISK_OFF ExposureDecision"
            )
    elif daily_data_failure or ranking_status == "DATA_FAIL_CLOSED":
        expected_intent = "RISK_OFF"
        expected_target = Decimal("0")
        if state != "RISK_OFF":
            raise DailySignalPublicationError(
                "data failure requires an immediate RISK_OFF ExposureDecision"
            )
    elif ranking_status == "NO_ALPHA_CASH":
        expected_intent = "NO_ALPHA_CASH"
        expected_target = Decimal("0")
    elif state == "RISK_OFF":
        expected_intent = "RISK_OFF"
        expected_target = Decimal("0")
    elif exposure_target < current_gross:
        expected_intent = "DEFENSIVE_REDUCTION"
        expected_target = exposure_target
    else:
        expected_intent = "ALPHA_REBALANCE"
        expected_target = exposure_target
    if intent_type != expected_intent or construction_target != expected_target:
        raise DailySignalPublicationError(
            "risk intent/target is unreachable under the frozen Exposure selection graph"
        )
    if expected_intent == "ALPHA_REBALANCE":
        raise DailySignalPublicationError(
            "Alpha rebalance cannot use reduction-only Daily authority"
        )


def _validate_artifact_bindings(
    admission: DailySignalAdmissionReceiptV1,
    artifacts: Mapping[str, Mapping[str, Any]],
) -> None:
    if set(artifacts) != _artifact_names(admission.authority):
        raise DailySignalPublicationError("daily publication artifact set drifted")
    if admission.authority is DailySignalAuthority.BLOCKED:
        daily = artifacts["daily-decision"]
        failure = artifacts["failure-receipt"]
        authority = artifacts["authority-receipt"]
        commitments = artifacts["received-input-commitments"]
        _validate_daily_decision_semantics(
            admission,
            daily,
            failure,
            authority,
        )
        if (
            str(daily.get("decision_sha256")) != admission.daily_decision_sha256
            or daily.get("decision_status") != "BLOCKED"
            or str(daily.get("failure_receipt_sha256"))
            != admission.failure_receipt_sha256
            or str(failure.get("failure_receipt_sha256"))
            != admission.failure_receipt_sha256
            or not failure.get("failed_stage")
            or not isinstance(failure.get("failure_codes"), list)
            or not failure.get("failure_codes")
            or str(authority.get("authority_receipt_sha256"))
            != admission.authority_receipt_sha256
            or authority.get("authority") != DailySignalAuthority.BLOCKED.value
            or commitments.get("next_session_allowed") is not False
            or canonical_sha256(
                {
                    key: value
                    for key, value in failure.items()
                    if key != "failure_receipt_sha256"
                }
            )
            != admission.failure_receipt_sha256
            or canonical_sha256(
                {
                    key: value
                    for key, value in authority.items()
                    if key != "authority_receipt_sha256"
                }
            )
            != admission.authority_receipt_sha256
            or canonical_sha256(
                {key: value for key, value in daily.items() if key != "decision_sha256"}
            )
            != admission.daily_decision_sha256
        ):
            raise DailySignalPublicationError(
                "blocked daily publication evidence is incomplete or contradictory"
            )
        return
    expected_declared = {
        "alpha-ranking": ("ranking_sha256", admission.alpha_ranking_sha256),
        "alpha-model": ("model_sha256", admission.model_sha256),
        "approved-factor-registry": ("registry_sha256", admission.approved_factor_registry_sha256),
        "exposure-decision": ("decision_sha256", admission.exposure_decision_sha256),
        "exposure-state": ("state_sha256", admission.exposure_state_sha256),
        "portfolio-construction": ("construction_sha256", admission.construction_sha256),
        "portfolio-intent": ("intent_sha256", admission.intent_sha256),
        "exposure-policy": ("policy_sha256", admission.exposure_policy_sha256),
        "constructor-policy": ("policy_sha256", admission.constructor_policy_sha256),
        "experiment-v3-admission": ("receipt_sha256", admission.experiment_admission_receipt_sha256),
        "account-snapshot": ("account_state_sha256", admission.account_state_sha256),
        "calendar-receipt": ("receipt_sha256", admission.calendar_receipt_sha256),
        "calendar-registry": ("registry_sha256", admission.calendar_registry_sha256),
        "execution-rule-bundle": ("execution_rule_bundle_sha256", admission.execution_rule_bundle_sha256),
        "daily-decision": ("decision_sha256", admission.daily_decision_sha256),
        "authority-receipt": ("authority_receipt_sha256", admission.authority_receipt_sha256),
    }
    for name, (field_name, _) in expected_declared.items():
        payload = artifacts[name]
        if name not in {"account-snapshot", "execution-rule-bundle"} and canonical_sha256(
            {key: value for key, value in payload.items() if key != field_name}
        ) != str(payload.get(field_name)):
            raise DailySignalPublicationError(
                f"fixed-registry {name} embedded hash does not verify"
            )
    for name, (field_name, expected) in expected_declared.items():
        payload = artifacts[name]
        if str(payload.get(field_name)) != expected:
            raise DailySignalPublicationError(
                f"fixed-registry {name} does not match admission {field_name}"
            )
    failure_payload = artifacts["failure-receipt"]
    if admission.authority is DailySignalAuthority.BLOCKED:
        if (
            str(failure_payload.get("failure_receipt_sha256"))
            != admission.failure_receipt_sha256
            or not failure_payload.get("failed_stage")
            or not isinstance(failure_payload.get("failure_codes"), list)
            or not failure_payload.get("failure_codes")
        ):
            raise DailySignalPublicationError(
                "blocked publication failure receipt is incomplete"
            )
    account_payload = artifacts["account-snapshot"]
    if str(account_payload.get("account_fingerprint")) != admission.account_fingerprint:
        raise DailySignalPublicationError("fixed-registry account fingerprint mismatch")
    try:
        reconstructed_account = AccountSnapshot(
            strategy_id=str(account_payload["strategy_id"]),
            cash=_decimal(account_payload["cash"], "account cash"),
            positions={
                str(instrument_id): Position(
                    instrument_id=str(instrument_id),
                    quantity=int(quantity),
                    sellable_quantity=int(
                        account_payload["sellable_positions"][instrument_id]
                    ),
                )
                for instrument_id, quantity in account_payload["positions"].items()
            },
            snapshot_id=str(account_payload["snapshot_id"]),
            as_of=datetime.fromisoformat(str(account_payload["as_of"])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DailySignalPublicationError("fixed-registry account body is malformed") from exc
    expected_account_state = canonical_sha256(
        {
            "scope": "portfolio-constructor-account-state.v1",
            "strategy_id": reconstructed_account.strategy_id,
            "cash": reconstructed_account.cash,
            "positions": {
                key: position.quantity
                for key, position in sorted(reconstructed_account.positions.items())
            },
        }
    )
    if (
        account_fingerprint(reconstructed_account) != admission.account_fingerprint
        or expected_account_state != admission.account_state_sha256
    ):
        raise DailySignalPublicationError("fixed-registry account body hash mismatch")
    rule_payload = artifacts["execution-rule-bundle"]
    try:
        fee_payload = rule_payload["fee_schedule"]
        reconstructed_fees = FeeSchedule(
            commission_rate=_decimal(fee_payload["commission_rate"], "commission_rate"),
            minimum_commission=_decimal(fee_payload["minimum_commission"], "minimum_commission"),
            exchange_fee_rate=_decimal(fee_payload["exchange_fee_rate"], "exchange_fee_rate"),
        )
        reconstructed_rules = {
            str(item["instrument_id"]): InstrumentRule(
                instrument_id=str(item["instrument_id"]),
                name=str(item["name"]),
                instrument_type=str(item["instrument_type"]),
                lot_size=int(item["lot_size"]),
                tick_size=_decimal(item["tick_size"], "tick_size"),
                sell_stamp_duty_rate=_decimal(
                    item["sell_stamp_duty_rate"], "sell_stamp_duty_rate"
                ),
                t_plus_one=item["t_plus_one"],
            )
            for item in rule_payload["instrument_rules"]
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise DailySignalPublicationError("fixed-registry rule body is malformed") from exc
    if (
        execution_rule_bundle_sha256(reconstructed_fees, reconstructed_rules)
        != admission.execution_rule_bundle_sha256
    ):
        raise DailySignalPublicationError("fixed-registry rule body hash mismatch")
    exposure_payload = artifacts["exposure-decision"]
    if (
        str(exposure_payload.get("state")) != admission.exposure_state
        or _decimal(exposure_payload.get("target_gross_exposure"), "exposure target")
        != admission.exposure_target_gross
    ):
        raise DailySignalPublicationError("fixed-registry exposure state/target mismatch")
    construction = artifacts["portfolio-construction"]
    intent = artifacts["portfolio-intent"]
    daily = artifacts["daily-decision"]
    ranking = artifacts["alpha-ranking"]
    model = artifacts["alpha-model"]
    factor_registry = artifacts["approved-factor-registry"]
    exposure_policy = artifacts["exposure-policy"]
    constructor_policy = artifacts["constructor-policy"]
    experiment_receipt = artifacts["experiment-v3-admission"]
    exposure_state_payload = artifacts["exposure-state"]
    _validate_daily_decision_semantics(
        admission,
        daily,
        failure_payload,
        artifacts["authority-receipt"],
    )
    _validate_exposure_intent_graph(admission, artifacts)
    try:
        ranking_decision_at = datetime.fromisoformat(str(ranking["decision_at"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise DailySignalPublicationError("ranking decision_at is malformed") from exc
    semantic_checks = (
        ("ranking_model", str(ranking.get("model_sha256")) != admission.model_sha256),
        ("model_factor_registry", str(model.get("approved_factor_registry_sha256")) != admission.approved_factor_registry_sha256),
        ("model_admission", str(model.get("model_admission_receipt_sha256")) != admission.model_admission_receipt_sha256),
        ("factor_registry", str(factor_registry.get("registry_sha256")) != admission.approved_factor_registry_sha256),
        ("experiment_factor_registry", str(experiment_receipt.get("approved_factor_registry_sha256")) != admission.approved_factor_registry_sha256),
        ("experiment_model_admission", str(experiment_receipt.get("model_admission_receipt_sha256")) != admission.model_admission_receipt_sha256),
        ("experiment_model", str(experiment_receipt.get("model_sha256")) != admission.model_sha256),
        ("exposure_decision_policy", str(exposure_payload.get("policy_sha256")) != admission.exposure_policy_sha256),
        ("exposure_policy", str(exposure_policy.get("policy_sha256")) != admission.exposure_policy_sha256),
        ("constructor_policy", str(constructor_policy.get("policy_sha256")) != admission.constructor_policy_sha256),
        ("ranking_decision_at", str(ranking.get("decision_at")) != str(construction.get("decision_at"))),
        ("exposure_decision_at", str(exposure_payload.get("decision_at")) != str(construction.get("decision_at"))),
        ("strategy_date", ranking_decision_at.date() != admission.strategy_date),
        ("construction_model", str(construction.get("model_sha256")) != admission.model_sha256),
        ("construction_cash", _decimal(construction.get("current_cash"), "construction current cash") != reconstructed_account.cash),
        ("construction_intent_type", str(construction.get("intent_type")) != admission.intent_type),
        ("construction_input", str(construction.get("input_snapshot_sha256")) != str(intent.get("market_data_sha256"))),
        ("construction_policy", str(construction.get("constructor_policy_sha256")) != admission.constructor_policy_sha256),
        ("construction_intent_target", _decimal(construction.get("target_gross_exposure"), "construction target") != _decimal(intent.get("target_gross_exposure"), "intent target")),
        ("construction_intent_weights", construction.get("feasible_stock_weights") != intent.get("target_weights")),
        ("intent_model", str(intent.get("model_sha256")) != admission.model_sha256),
        ("intent_type", str(intent.get("intent_type")) != admission.intent_type),
        ("intent_risk_state", str(intent.get("risk_state_sha256")) != admission.exposure_state_sha256),
        ("exposure_state_decision_at", str(exposure_state_payload.get("last_decision_at")) != str(exposure_payload.get("decision_at"))),
        ("exposure_state_policy", str(exposure_state_payload.get("policy_sha256")) != admission.exposure_policy_sha256),
        ("daily_strategy_date", str(daily.get("strategy_date")) != admission.strategy_date.isoformat()),
        ("daily_execution_date", daily.get("execution_date") != (admission.execution_date.isoformat() if admission.execution_date else None)),
        ("daily_model", str(daily.get("model_sha256")) != admission.model_sha256),
        ("daily_policy", str(daily.get("policy_sha256")) != admission.combined_policy_sha256),
        ("daily_intent", str(daily.get("intent_sha256")) != admission.intent_sha256),
    )
    mismatches = [name for name, failed in semantic_checks if failed]
    if admission.authority is DailySignalAuthority.RISK_REDUCTION_ONLY:
        risk_checks = (
            ("daily_target", _decimal(daily.get("target_gross_exposure"), "daily target") != _decimal(construction.get("target_gross_exposure"), "construction target")),
            ("daily_feasible", _decimal(daily.get("feasible_gross_exposure"), "daily feasible") != _decimal(construction.get("feasible_gross_exposure"), "construction feasible")),
            ("daily_target_weights", daily.get("target_stock_weights") != construction.get("target_stock_weights")),
            ("daily_feasible_weights", daily.get("feasible_stock_weights") != construction.get("feasible_stock_weights")),
            ("daily_current_quantities", daily.get("current_lot_quantities") != construction.get("current_quantities")),
        )
        mismatches.extend(name for name, failed in risk_checks if failed)
    elif str(daily.get("failure_receipt_sha256")) != admission.failure_receipt_sha256:
        mismatches.append("blocked_failure_receipt")
    if mismatches:
        raise DailySignalPublicationError(
            "daily cross-artifact semantic binding mismatch: " + ",".join(mismatches)
        )
    actions = construction.get("actions")
    if not isinstance(actions, list):
        raise DailySignalPublicationError("construction actions are missing")
    instrument_actions = [
        str(item.get("instrument_id"))
        for item in actions
        if isinstance(item, Mapping) and item.get("instrument_id") is not None
    ]
    if len(instrument_actions) != len(set(instrument_actions)):
        raise DailySignalPublicationError(
            "construction has duplicate actions for one instrument"
        )
    if sum(
        isinstance(item, Mapping) and item.get("action") == "CASH"
        for item in actions
    ) != 1:
        raise DailySignalPublicationError("construction must contain exactly one CASH action")
    if admission.authority is DailySignalAuthority.RISK_REDUCTION_ONLY:
        if any(
            not isinstance(item, Mapping)
            or item.get("action") not in {"SELL", "HOLD", "CASH"}
            for item in actions
        ):
            raise DailySignalPublicationError(
                "reduction-only daily publication contains a non-reduction action"
            )
        positions = artifacts["account-snapshot"].get("positions")
        if not isinstance(positions, Mapping):
            raise DailySignalPublicationError(
                "reduction-only account snapshot positions are missing"
            )
        current = {
            str(key): int(value) for key, value in positions.items()
            if type(value) is int and value >= 0
        }
        if len(current) != len(positions):
            raise DailySignalPublicationError(
                "reduction-only account positions are malformed"
            )
        feasible = construction.get("feasible_quantities")
        if not isinstance(feasible, Mapping):
            raise DailySignalPublicationError(
                "reduction-only feasible quantities are missing"
            )
        if any(
            type(quantity) is not int
            or quantity < 0
            or quantity > current.get(str(instrument_id), 0)
            for instrument_id, quantity in feasible.items()
        ):
            raise DailySignalPublicationError(
                "reduction-only construction increases an account quantity"
            )
        construction_current = construction.get("current_quantities")
        if not isinstance(construction_current, Mapping) or {
            str(key): int(value) for key, value in construction_current.items()
        } != {key: value for key, value in current.items() if value > 0}:
            raise DailySignalPublicationError(
                "reduction-only construction current quantities differ from account"
            )
        action_by_id = {
            str(item.get("instrument_id")): item
            for item in actions
            if item.get("action") in {"SELL", "HOLD"}
        }
        if set(action_by_id) != {key for key, value in current.items() if value > 0}:
            raise DailySignalPublicationError(
                "reduction-only actions do not completely cover current holdings"
            )
        for item in actions:
            if item.get("action") == "SELL":
                instrument_id = str(item.get("instrument_id"))
                current_quantity = current.get(instrument_id, 0)
                if (
                    type(item.get("current_quantity")) is not int
                    or item.get("current_quantity") != current_quantity
                    or type(item.get("target_quantity")) is not int
                    or item.get("target_quantity") > current_quantity
                    or item.get("order_quantity")
                    != current_quantity - item.get("target_quantity")
                ):
                    raise DailySignalPublicationError(
                        "reduction-only SELL is not bound to the account snapshot"
                    )
        daily_sells = daily.get("sell_orders")
        if not isinstance(daily_sells, list):
            raise DailySignalPublicationError("risk daily decision sell_orders are missing")
        expected_sells = {
            str(item.get("instrument_id")): int(item.get("order_quantity"))
            for item in actions
            if item.get("action") == "SELL"
        }
        actual_sells = {
            str(item.get("instrument_id")): int(item.get("quantity"))
            for item in daily_sells
            if isinstance(item, Mapping)
            and type(item.get("quantity")) is int
        }
        if expected_sells != actual_sells:
            raise DailySignalPublicationError(
                "risk daily decision orders differ from construction"
            )


def _publish_daily_signal_bundle_from_daily_pipeline(
    *,
    admission: DailySignalAdmissionReceiptV1,
    artifacts: Mapping[str, Mapping[str, Any]],
) -> DailySignalPublicationReceiptV1:
    """Create or byte-identically replay one fixed-registry publication."""

    if type(admission) is not DailySignalAdmissionReceiptV1:
        raise DailySignalPublicationError("admission must be the exact V1 type")
    normalized: dict[str, Mapping[str, Any]] = {}
    for key, value in artifacts.items():
        if not isinstance(value, Mapping):
            raise DailySignalPublicationError(
                f"daily publication artifact {key!s} must be an object"
            )
        try:
            canonical_value = json.loads(
                canonical_json_bytes(value).decode("utf-8"),
                object_pairs_hook=_strict_object,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise DailySignalPublicationError(
                f"daily publication artifact {key!s} is not canonicalizable"
            ) from exc
        if not isinstance(canonical_value, Mapping):
            raise DailySignalPublicationError(
                f"daily publication artifact {key!s} must remain an object"
            )
        normalized[str(key)] = canonical_value
    validate_daily_signal_publication_contract(admission, normalized)
    artifact_hashes = {
        name: canonical_sha256(payload) for name, payload in normalized.items()
    }
    publication = DailySignalPublicationReceiptV1(
        strategy_date=admission.strategy_date,
        execution_date=admission.execution_date,
        published_at=admission.frozen_at,
        authority=admission.authority,
        admission_receipt_sha256=admission.admission_receipt_sha256,
        daily_decision_sha256=admission.daily_decision_sha256,
        artifact_sha256s=artifact_hashes,
    )
    entry = _registry_entry_directory(admission.strategy_date)
    root = entry.parent
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir() or root.is_symlink():
        raise DailySignalPublicationError(
            "fixed daily publication root is not a regular directory"
        )
    try:
        entry.mkdir()
    except FileExistsError as exc:
        if not entry.is_dir() or entry.is_symlink():
            raise DailySignalPublicationConflict(
                "daily publication date slot is occupied by an unsafe entry"
            ) from exc
        try:
            loaded = load_daily_signal_publication(publication)
        except DailySignalPublicationError as load_exc:
            raise DailySignalPublicationConflict(
                "daily publication date slot is incomplete or contradictory; "
                "manual recovery is required"
            ) from load_exc
        if loaded.publication == publication:
            return publication
        raise DailySignalPublicationConflict(
            "daily publication date slot is occupied"
        ) from exc
    if not entry.is_dir() or entry.is_symlink():
        raise DailySignalPublicationError(
            "new daily publication date slot is unsafe"
        )
    for name, payload in sorted(normalized.items()):
        _write_immutable(entry / f"{name}.json", canonical_json_bytes(payload) + b"\n")
    _write_immutable(
        entry / "daily-signal-admission.json",
        canonical_json_bytes(admission.to_dict()) + b"\n",
    )
    _write_immutable(
        entry / "daily-signal-publication.json",
        canonical_json_bytes(publication.to_dict()) + b"\n",
    )
    # The date-slot directory is claimed create-only above.  COMMITTED is
    # deliberately written last; a crash leaves a poisoned, fail-closed slot
    # that cannot be replayed or executed until an operator recovers it.
    _write_immutable(
        entry / "COMMITTED",
        canonical_json_bytes(
            {
                "publication_receipt_sha256": publication.publication_receipt_sha256,
                "complete_file_count": len(_artifact_names(admission.authority)) + 2,
            }
        )
        + b"\n",
    )
    load_daily_signal_publication(publication)
    return publication


def validate_daily_signal_publication_contract(
    admission: DailySignalAdmissionReceiptV1,
    artifacts: Mapping[str, Mapping[str, Any]],
) -> None:
    """Recheck the complete Daily authority graph without trusting a loader.

    The next-session adapter invokes this after loading the fixed registry so a
    substituted loader result still cannot weaken the Daily v2/status/exposure
    contract.  This function grants no Alpha, Paper, trade, or LIVE authority.
    """

    if type(admission) is not DailySignalAdmissionReceiptV1:
        raise DailySignalPublicationError("daily admission must be the exact V1 type")
    if not isinstance(artifacts, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, Mapping)
        for key, value in artifacts.items()
    ):
        raise DailySignalPublicationError("daily publication artifacts are malformed")
    _validate_artifact_bindings(admission, artifacts)


def load_daily_signal_publication(
    receipt: DailySignalPublicationReceiptV1,
) -> LoadedDailySignalPublicationV1:
    """Reload every canonical byte from the fixed registry and re-bind it."""

    if type(receipt) is not DailySignalPublicationReceiptV1:
        raise DailySignalPublicationError(
            "publication receipt must be the exact V1 type"
        )
    entry = _registry_entry_directory(receipt.strategy_date)
    if not entry.is_dir() or entry.is_symlink():
        raise DailySignalPublicationError("fixed daily publication entry is absent or unsafe")
    expected_files = {
        *(f"{name}.json" for name in _artifact_names(receipt.authority)),
        "daily-signal-admission.json",
        "daily-signal-publication.json",
        "COMMITTED",
    }
    actual_files = {item.name for item in entry.iterdir()}
    if actual_files != expected_files:
        raise DailySignalPublicationError("fixed daily publication entry file set drifted")
    _, commit_payload = _read_canonical_object(entry / "COMMITTED", "daily publication commit marker")
    publication_raw, publication_payload = _read_canonical_object(
        entry / "daily-signal-publication.json", "daily publication receipt"
    )
    loaded_publication = DailySignalPublicationReceiptV1.from_dict(publication_payload)
    if (
        commit_payload.get("publication_receipt_sha256")
        != loaded_publication.publication_receipt_sha256
        or commit_payload.get("complete_file_count")
        != len(_artifact_names(receipt.authority)) + 2
    ):
        raise DailySignalPublicationError("daily publication commit marker mismatch")
    if (
        publication_raw != canonical_json_bytes(receipt.to_dict()) + b"\n"
        or loaded_publication != receipt
    ):
        raise DailySignalPublicationError("caller publication differs from fixed registry")
    _, admission_payload = _read_canonical_object(
        entry / "daily-signal-admission.json", "daily admission receipt"
    )
    admission = DailySignalAdmissionReceiptV1.from_dict(admission_payload)
    if (
        admission.admission_receipt_sha256 != loaded_publication.admission_receipt_sha256
        or admission.strategy_date != loaded_publication.strategy_date
        or admission.execution_date != loaded_publication.execution_date
        or admission.authority is not loaded_publication.authority
        or admission.daily_decision_sha256 != loaded_publication.daily_decision_sha256
    ):
        raise DailySignalPublicationError("daily admission/publication binding mismatch")
    artifacts: dict[str, Mapping[str, Any]] = {}
    for name in sorted(_artifact_names(receipt.authority)):
        _, payload = _read_canonical_object(entry / f"{name}.json", name)
        if canonical_sha256(payload) != loaded_publication.artifact_sha256s[name]:
            raise DailySignalPublicationError(f"fixed-registry {name} content hash mismatch")
        artifacts[name] = payload
    validate_daily_signal_publication_contract(admission, artifacts)
    return LoadedDailySignalPublicationV1(
        admission=admission,
        publication=loaded_publication,
        artifacts=MappingProxyType(artifacts),
    )


def load_daily_signal_publication_for_date(
    strategy_date: date,
) -> LoadedDailySignalPublicationV1:
    """Load the sole atomically committed receipt for one strategy date."""

    entry = _registry_entry_directory(strategy_date)
    _, payload = _read_canonical_object(
        entry / "daily-signal-publication.json", "daily publication receipt"
    )
    return load_daily_signal_publication(
        DailySignalPublicationReceiptV1.from_dict(payload)
    )


__all__ = [
    "DAILY_SIGNAL_ADMISSION_SCHEMA_VERSION",
    "DAILY_SIGNAL_PUBLICATION_REGISTRY_ID",
    "DAILY_SIGNAL_PUBLICATION_REGISTRY_ROOT",
    "DAILY_SIGNAL_PUBLICATION_SCHEMA_VERSION",
    "DailySignalAdmissionReceiptV1",
    "DailySignalAuthority",
    "DailySignalPublicationConflict",
    "DailySignalPublicationError",
    "DailySignalPublicationReceiptV1",
    "LoadedDailySignalPublicationV1",
    "load_daily_signal_publication",
    "load_daily_signal_publication_for_date",
    "validate_daily_signal_publication_contract",
]
