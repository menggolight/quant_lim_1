"""One-shot D-close to next-official-session manual signal adapter.

This module freezes a :class:`PortfolioIntent` plus its portfolio-construction
evidence after D close, binds the exact next trading session from a structured
calendar receipt, and permits one persistent D+1 preflight.  It does not place
orders.  A hash proves content consistency only; official-source trust is
derived from an independently supplied receipt allowlist/registry, never from
a caller-provided boolean or source-name string.

Normal Alpha/cash decisions and explicit risk reductions use separate factory
functions.  A risk exit cannot be smuggled through the Alpha adapter.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from trading.costs import FeeSchedule
from trading.integrity import (
    account_fingerprint,
    execution_quote_bundle_sha256,
    execution_rule_bundle_sha256,
)
from trading.models import (
    AccountSnapshot,
    InstrumentRule,
    MarketQuote,
    PortfolioIntent,
    PortfolioIntentType,
    Side,
)

from .contracts import canonical_json_bytes, canonical_sha256
from .portfolio_constructor_v2 import (
    ConstructorCostPolicy,
    ConstructionActionType,
    PortfolioConstructionResult,
    PortfolioConstructorPolicy,
    STRATEGY_ID,
)


CALENDAR_RECEIPT_SCHEMA_VERSION = "official-calendar-receipt.v1"
CALENDAR_REGISTRY_SCHEMA_VERSION = "official-calendar-registry.v1"
NEXT_SESSION_SIGNAL_SCHEMA_VERSION = "next-session-signal.v1"
NEXT_SESSION_CONSUMPTION_SCHEMA_VERSION = "next-session-consumption.v1"
NEXT_SESSION_CONSUMPTION_REGISTRY_DIR = "consumptions"
NEXT_SESSION_MANUAL_FILL_REGISTRY_DIR = "manual-fills"
NEXT_SESSION_REGISTRY_ROOT = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "portfolio"
    / STRATEGY_ID
    / ".next-session-registry.v1"
)
ZERO = Decimal("0")
BPS = Decimal("10000")
CHINA_STANDARD_TIME = timezone(timedelta(hours=8))
OPENING_REVIEW_START = time(9, 25)
OPENING_REVIEW_END = time(9, 35)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_INSTRUMENT_RE = re.compile(r"^[0-9A-Z][0-9A-Z.]{2,31}$")


class NextSessionSignalError(ValueError):
    """Base class for fail-closed next-session errors."""


class NextSessionAlreadyConsumed(NextSessionSignalError):
    """Raised when the create-only consumption CAS has already won elsewhere."""


class NextSessionSignalConflict(NextSessionSignalError):
    """Raised when a frozen signal path already contains different bytes."""


class NextSessionChannel(str, Enum):
    ALPHA = "ALPHA_NEXT_SESSION"
    RISK_REDUCTION = "RISK_REDUCTION_NEXT_SESSION"


class InstructionStatus(str, Enum):
    READY_FOR_MANUAL_EXECUTION = "READY_FOR_MANUAL_EXECUTION"
    CANCELED = "CANCELED"
    HOLD = "HOLD"
    CASH = "CASH"


_ALPHA_TYPES = frozenset(
    {PortfolioIntentType.ALPHA_REBALANCE}
)
_RISK_TYPES = frozenset(
    {
        PortfolioIntentType.NO_ALPHA_CASH,
        PortfolioIntentType.DEFENSIVE_REDUCTION,
        PortfolioIntentType.RISK_OFF,
        PortfolioIntentType.ACCOUNT_DRAWDOWN_EXIT,
    }
)


def _aware(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise NextSessionSignalError(f"{field_name} must be timezone-aware")
    return value


def _sha256(value: str, field_name: str) -> str:
    normalized = str(value)
    if _SHA256_RE.fullmatch(normalized) is None:
        raise NextSessionSignalError(f"{field_name} must be a lowercase SHA-256")
    return normalized


def _identifier(value: str, field_name: str) -> str:
    normalized = str(value).strip()
    if _IDENTIFIER_RE.fullmatch(normalized) is None:
        raise NextSessionSignalError(f"{field_name} is not a valid identifier")
    return normalized


def _decimal(value: Any, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise NextSessionSignalError(f"{field_name} must be decimal") from exc
    if not result.is_finite():
        raise NextSessionSignalError(f"{field_name} must be finite")
    return result


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise NextSessionSignalError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _verify_embedded_hash(
    payload: Mapping[str, Any],
    *,
    hash_field: str,
    label: str,
) -> str:
    if hash_field not in payload:
        raise NextSessionSignalError(f"{label} is missing {hash_field}")
    expected = _sha256(str(payload[hash_field]), f"{label}.{hash_field}")
    content = {key: value for key, value in payload.items() if key != hash_field}
    if canonical_sha256(content) != expected:
        raise NextSessionSignalError(f"{label} hash mismatch")
    return expected


def _require_exact_keys(
    payload: Mapping[str, Any],
    expected: set[str],
    label: str,
) -> None:
    actual = set(payload)
    if actual != expected:
        raise NextSessionSignalError(
            f"{label} keys differ: missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )


def _intent_from_embedded(payload: Mapping[str, Any]) -> PortfolioIntent:
    expected = {
        "schema_version",
        "intent_id",
        "strategy_id",
        "decision_at",
        "available_at",
        "frozen_at",
        "intent_type",
        "target_gross_exposure",
        "target_weights",
        "reason_codes",
        "signal_sha256",
        "market_data_sha256",
        "model_sha256",
        "risk_state_sha256",
        "intent_sha256",
        "live_supported",
    }
    _require_exact_keys(payload, expected, "embedded PortfolioIntent")
    if (
        payload["schema_version"] != "portfolio-intent.v1"
        or payload["strategy_id"] != STRATEGY_ID
        or payload["live_supported"] is not False
        or not isinstance(payload["target_weights"], Mapping)
        or not isinstance(payload["reason_codes"], (list, tuple))
    ):
        raise NextSessionSignalError("embedded PortfolioIntent contract drifted")
    try:
        intent = PortfolioIntent(
            intent_id=str(payload["intent_id"]),
            strategy_id=str(payload["strategy_id"]),
            intent_type=PortfolioIntentType(str(payload["intent_type"])),
            decision_at=datetime.fromisoformat(str(payload["decision_at"])),
            available_at=datetime.fromisoformat(str(payload["available_at"])),
            frozen_at=datetime.fromisoformat(str(payload["frozen_at"])),
            target_gross_exposure=_decimal(
                payload["target_gross_exposure"], "intent.target_gross_exposure"
            ),
            target_weights={
                str(key): _decimal(value, "intent.target_weight")
                for key, value in payload["target_weights"].items()
            },
            reason_codes=tuple(str(item) for item in payload["reason_codes"]),
            signal_sha256=str(payload["signal_sha256"]),
            market_data_sha256=str(payload["market_data_sha256"]),
            model_sha256=str(payload["model_sha256"]),
            risk_state_sha256=str(payload["risk_state_sha256"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise NextSessionSignalError("embedded PortfolioIntent is malformed") from exc
    if intent.intent_sha256 != _sha256(
        str(payload["intent_sha256"]), "intent.intent_sha256"
    ):
        raise NextSessionSignalError("embedded PortfolioIntent hash mismatch")
    if canonical_json_bytes(intent.to_dict()) != canonical_json_bytes(payload):
        raise NextSessionSignalError("embedded PortfolioIntent is not canonical")
    return intent


def _policy_from_embedded(payload: Mapping[str, Any]) -> PortfolioConstructorPolicy:
    expected = {
        "schema_version",
        "status",
        "policy_id",
        "frozen_at",
        "selection_method",
        "weighting_method",
        "improvement_rule",
        "max_positions",
        "max_position_weight",
        "entry_percentile_min",
        "hold_percentile_min",
        "no_trade_threshold",
        "maximum_execution_price_deviation",
        "maximum_quote_age_seconds",
        "maximum_account_age_seconds",
        "costs",
        "manual_execution_required",
        "automatic_submission",
        "live_supported",
        "policy_sha256",
    }
    _require_exact_keys(payload, expected, "embedded constructor policy")
    costs = payload.get("costs")
    if not isinstance(costs, Mapping):
        raise NextSessionSignalError("embedded constructor costs are malformed")
    _require_exact_keys(
        costs,
        {
            "commission_rate",
            "minimum_commission",
            "sell_tax_rate",
            "transfer_fee_rate",
            "slippage_bps_one_way",
        },
        "embedded constructor costs",
    )
    if (
        payload["schema_version"] != "portfolio-constructor-policy.v1"
        or payload["status"] != "frozen_pre_registered"
        or payload["selection_method"]
        != "incumbent_hold_band_then_ranked_entry_band"
        or payload["weighting_method"]
        != "equal_weight_capped_then_whole_lot_floor"
        or payload["improvement_rule"]
        != "strictly_greater_than_complete_cost_plus_threshold"
        or payload["manual_execution_required"] is not True
        or payload["automatic_submission"] is not False
        or payload["live_supported"] is not False
    ):
        raise NextSessionSignalError("embedded constructor policy contract drifted")
    try:
        policy = PortfolioConstructorPolicy(
            policy_id=str(payload["policy_id"]),
            frozen_at=datetime.fromisoformat(str(payload["frozen_at"])),
            max_positions=int(payload["max_positions"]),
            max_position_weight=_decimal(
                payload["max_position_weight"], "policy.max_position_weight"
            ),
            entry_percentile_min=_decimal(
                payload["entry_percentile_min"], "policy.entry_percentile_min"
            ),
            hold_percentile_min=_decimal(
                payload["hold_percentile_min"], "policy.hold_percentile_min"
            ),
            no_trade_threshold=_decimal(
                payload["no_trade_threshold"], "policy.no_trade_threshold"
            ),
            maximum_execution_price_deviation=_decimal(
                payload["maximum_execution_price_deviation"],
                "policy.maximum_execution_price_deviation",
            ),
            maximum_quote_age_seconds=int(payload["maximum_quote_age_seconds"]),
            maximum_account_age_seconds=int(
                payload["maximum_account_age_seconds"]
            ),
            costs=ConstructorCostPolicy(
                commission_rate=_decimal(
                    costs["commission_rate"], "policy.costs.commission_rate"
                ),
                minimum_commission=_decimal(
                    costs["minimum_commission"], "policy.costs.minimum_commission"
                ),
                sell_tax_rate=_decimal(
                    costs["sell_tax_rate"], "policy.costs.sell_tax_rate"
                ),
                transfer_fee_rate=_decimal(
                    costs["transfer_fee_rate"], "policy.costs.transfer_fee_rate"
                ),
                slippage_bps_one_way=_decimal(
                    costs["slippage_bps_one_way"],
                    "policy.costs.slippage_bps_one_way",
                ),
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise NextSessionSignalError("embedded constructor policy is malformed") from exc
    if policy.policy_sha256 != _sha256(
        str(payload["policy_sha256"]), "policy.policy_sha256"
    ):
        raise NextSessionSignalError("embedded constructor policy hash mismatch")
    if canonical_json_bytes(policy.to_dict()) != canonical_json_bytes(payload):
        raise NextSessionSignalError("embedded constructor policy is not canonical")
    return policy


@dataclass(frozen=True, slots=True)
class OfficialCalendarReceipt:
    """Structured calendar payload; source status is not self-asserted here."""

    receipt_id: str
    adapter_id: str
    adapter_version: str
    source_id: str
    source_document_sha256: str
    issued_at: datetime
    available_at: datetime
    trading_sessions: tuple[date, ...]
    payload_sha256: str = field(init=False)
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        receipt_id = _identifier(self.receipt_id, "receipt_id")
        adapter_id = _identifier(self.adapter_id, "adapter_id")
        adapter_version = _identifier(self.adapter_version, "adapter_version")
        source_id = _identifier(self.source_id, "source_id")
        source_document_sha256 = _sha256(
            self.source_document_sha256, "source_document_sha256"
        )
        issued_at = _aware(self.issued_at, "issued_at")
        available_at = _aware(self.available_at, "available_at")
        if available_at > issued_at:
            raise NextSessionSignalError("calendar available_at must not follow issued_at")
        sessions = tuple(self.trading_sessions)
        if not sessions or any(type(item) is not date for item in sessions):
            raise NextSessionSignalError("trading_sessions must contain dates")
        if sessions != tuple(sorted(set(sessions))):
            raise NextSessionSignalError(
                "trading_sessions must be unique and strictly ascending"
            )
        object.__setattr__(self, "receipt_id", receipt_id)
        object.__setattr__(self, "adapter_id", adapter_id)
        object.__setattr__(self, "adapter_version", adapter_version)
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "source_document_sha256", source_document_sha256)
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "available_at", available_at)
        object.__setattr__(self, "trading_sessions", sessions)
        payload_hash = canonical_sha256(
            {
                "scope": "official-calendar-payload.v1",
                "trading_sessions": sessions,
            }
        )
        object.__setattr__(self, "payload_sha256", payload_hash)
        object.__setattr__(self, "receipt_sha256", canonical_sha256(self.to_content_dict()))

    def next_session_after(self, strategy_date: date) -> date:
        try:
            index = self.trading_sessions.index(strategy_date)
        except ValueError as exc:
            raise NextSessionSignalError(
                "strategy_date is absent from the official calendar receipt"
            ) from exc
        if index + 1 >= len(self.trading_sessions):
            raise NextSessionSignalError(
                "official calendar receipt does not include the next trading session"
            )
        return self.trading_sessions[index + 1]

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CALENDAR_RECEIPT_SCHEMA_VERSION,
            "receipt_id": self.receipt_id,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "source_id": self.source_id,
            "source_document_sha256": self.source_document_sha256,
            "issued_at": self.issued_at,
            "available_at": self.available_at,
            "trading_sessions": list(self.trading_sessions),
            "payload_sha256": self.payload_sha256,
            "trust_statement": "hash_consistency_only_registry_controls_source_allowlist",
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.to_content_dict()
        payload["receipt_sha256"] = self.receipt_sha256
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OfficialCalendarReceipt":
        if not isinstance(payload, Mapping):
            raise NextSessionSignalError("calendar receipt must be an object")
        if payload.get("schema_version") != CALENDAR_RECEIPT_SCHEMA_VERSION:
            raise NextSessionSignalError("unsupported calendar receipt schema")
        try:
            receipt = cls(
                receipt_id=str(payload["receipt_id"]),
                adapter_id=str(payload["adapter_id"]),
                adapter_version=str(payload["adapter_version"]),
                source_id=str(payload["source_id"]),
                source_document_sha256=str(payload["source_document_sha256"]),
                issued_at=datetime.fromisoformat(str(payload["issued_at"])),
                available_at=datetime.fromisoformat(str(payload["available_at"])),
                trading_sessions=tuple(
                    date.fromisoformat(str(item)) for item in payload["trading_sessions"]
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise NextSessionSignalError("calendar receipt is malformed") from exc
        if str(payload.get("payload_sha256")) != receipt.payload_sha256:
            raise NextSessionSignalError("calendar payload hash mismatch")
        if str(payload.get("receipt_sha256")) != receipt.receipt_sha256:
            raise NextSessionSignalError("calendar receipt hash mismatch")
        if payload.get("trust_statement") != (
            "hash_consistency_only_registry_controls_source_allowlist"
        ):
            raise NextSessionSignalError("calendar receipt trust statement drifted")
        return receipt


@dataclass(frozen=True, slots=True)
class CalendarRegistryEntry:
    receipt_sha256: str
    receipt_id: str
    adapter_id: str
    adapter_version: str
    source_id: str
    source_document_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "receipt_sha256", _sha256(self.receipt_sha256, "receipt_sha256"))
        for field_name in ("receipt_id", "adapter_id", "adapter_version", "source_id"):
            object.__setattr__(self, field_name, _identifier(getattr(self, field_name), field_name))
        object.__setattr__(
            self,
            "source_document_sha256",
            _sha256(self.source_document_sha256, "source_document_sha256"),
        )

    @classmethod
    def from_receipt(cls, receipt: OfficialCalendarReceipt) -> "CalendarRegistryEntry":
        if not isinstance(receipt, OfficialCalendarReceipt):
            raise NextSessionSignalError("registry entry requires OfficialCalendarReceipt")
        return cls(
            receipt_sha256=receipt.receipt_sha256,
            receipt_id=receipt.receipt_id,
            adapter_id=receipt.adapter_id,
            adapter_version=receipt.adapter_version,
            source_id=receipt.source_id,
            source_document_sha256=receipt.source_document_sha256,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "receipt_sha256": self.receipt_sha256,
            "receipt_id": self.receipt_id,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "source_id": self.source_id,
            "source_document_sha256": self.source_document_sha256,
        }


@dataclass(frozen=True, slots=True)
class OfficialCalendarRegistry:
    """Exact receipt allowlist supplied by the controlled runtime boundary."""

    registry_id: str
    frozen_at: datetime
    entries: tuple[CalendarRegistryEntry, ...]
    registry_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        registry_id = _identifier(self.registry_id, "registry_id")
        frozen_at = _aware(self.frozen_at, "frozen_at")
        entries = tuple(sorted(self.entries, key=lambda item: item.receipt_sha256))
        if not entries or any(not isinstance(item, CalendarRegistryEntry) for item in entries):
            raise NextSessionSignalError(
                "calendar registry requires typed allowlist entries"
            )
        hashes = [item.receipt_sha256 for item in entries]
        if len(hashes) != len(set(hashes)):
            raise NextSessionSignalError("calendar registry receipt hashes must be unique")
        object.__setattr__(self, "registry_id", registry_id)
        object.__setattr__(self, "frozen_at", frozen_at)
        object.__setattr__(self, "entries", entries)
        object.__setattr__(self, "registry_sha256", canonical_sha256(self.to_content_dict()))

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CALENDAR_REGISTRY_SCHEMA_VERSION,
            "registry_id": self.registry_id,
            "frozen_at": self.frozen_at,
            "entries": [item.to_dict() for item in self.entries],
            "trust_boundary": "controlled_exact_receipt_allowlist",
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.to_content_dict()
        payload["registry_sha256"] = self.registry_sha256
        return payload

    def verify(self, receipt: OfficialCalendarReceipt) -> CalendarRegistryEntry:
        if not isinstance(receipt, OfficialCalendarReceipt):
            raise NextSessionSignalError(
                "official calendar verification requires a structured receipt"
            )
        matches = [item for item in self.entries if item.receipt_sha256 == receipt.receipt_sha256]
        if len(matches) != 1:
            raise NextSessionSignalError(
                "calendar receipt is not present in the controlled registry allowlist"
            )
        entry = matches[0]
        expected = CalendarRegistryEntry.from_receipt(receipt)
        if entry != expected:
            raise NextSessionSignalError("calendar registry metadata mismatch")
        return entry


@dataclass(frozen=True, slots=True)
class NextSessionSignal:
    signal_id: str
    channel: NextSessionChannel
    strategy_date: date
    execution_date: date
    frozen_at: datetime
    portfolio_intent: Mapping[str, Any]
    construction: Mapping[str, Any]
    constructor_policy: Mapping[str, Any]
    data_snapshot_sha256: str
    model_sha256: str
    policy_sha256: str
    intent_sha256: str
    expected_account_state_sha256: str
    calendar_receipt: Mapping[str, Any]
    calendar_receipt_sha256: str
    calendar_registry_sha256: str
    execution_rule_bundle_sha256: str
    frozen_reference_prices: Mapping[str, Decimal]
    maximum_execution_price_deviation: Decimal
    signal_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "signal_id", _identifier(self.signal_id, "signal_id"))
        if not isinstance(self.channel, NextSessionChannel):
            raise NextSessionSignalError("channel must be NextSessionChannel")
        if type(self.strategy_date) is not date or type(self.execution_date) is not date:
            raise NextSessionSignalError("strategy_date and execution_date must be dates")
        if self.execution_date <= self.strategy_date:
            raise NextSessionSignalError("execution_date must follow strategy_date")
        object.__setattr__(self, "frozen_at", _aware(self.frozen_at, "frozen_at"))
        for field_name in (
            "data_snapshot_sha256",
            "model_sha256",
            "policy_sha256",
            "intent_sha256",
            "expected_account_state_sha256",
            "calendar_receipt_sha256",
            "calendar_registry_sha256",
            "execution_rule_bundle_sha256",
        ):
            object.__setattr__(self, field_name, _sha256(getattr(self, field_name), field_name))
        deviation = _decimal(
            self.maximum_execution_price_deviation,
            "maximum_execution_price_deviation",
        )
        if deviation < ZERO:
            raise NextSessionSignalError("execution-price deviation must not be negative")
        prices = {str(key): _decimal(value, "frozen_reference_price") for key, value in self.frozen_reference_prices.items()}
        if any(value <= ZERO for value in prices.values()):
            raise NextSessionSignalError("frozen reference prices must be positive")
        object.__setattr__(self, "maximum_execution_price_deviation", deviation)
        object.__setattr__(self, "frozen_reference_prices", MappingProxyType(dict(sorted(prices.items()))))
        for field_name in (
            "portfolio_intent",
            "construction",
            "constructor_policy",
            "calendar_receipt",
        ):
            canonical_payload = json.loads(
                canonical_json_bytes(getattr(self, field_name)).decode("utf-8"),
                object_pairs_hook=_strict_object,
            )
            object.__setattr__(self, field_name, _freeze(canonical_payload))
        object.__setattr__(self, "signal_sha256", canonical_sha256(self.to_content_dict()))

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": NEXT_SESSION_SIGNAL_SCHEMA_VERSION,
            "signal_id": self.signal_id,
            "strategy_id": STRATEGY_ID,
            "channel": self.channel.value,
            "strategy_date": self.strategy_date,
            "execution_date": self.execution_date,
            "frozen_at": self.frozen_at,
            "portfolio_intent": _thaw(self.portfolio_intent),
            "construction": _thaw(self.construction),
            "constructor_policy": _thaw(self.constructor_policy),
            "data_snapshot_sha256": self.data_snapshot_sha256,
            "model_sha256": self.model_sha256,
            "policy_sha256": self.policy_sha256,
            "intent_sha256": self.intent_sha256,
            "expected_account_state_sha256": self.expected_account_state_sha256,
            "calendar_receipt": _thaw(self.calendar_receipt),
            "calendar_receipt_sha256": self.calendar_receipt_sha256,
            "calendar_registry_sha256": self.calendar_registry_sha256,
            "execution_rule_bundle_sha256": self.execution_rule_bundle_sha256,
            "execution_rule_trust_statement": (
                "hash_consistency_only_metadata_source_not_authenticated"
            ),
            "frozen_reference_prices": dict(self.frozen_reference_prices),
            "maximum_execution_price_deviation": self.maximum_execution_price_deviation,
            "one_shot": True,
            "manual_execution_required": True,
            "automatic_submission": False,
            "paper_eligibility": False,
            "trade_eligibility": False,
            "live_supported": False,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.to_content_dict()
        payload["signal_sha256"] = self.signal_sha256
        return payload

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        registry: OfficialCalendarRegistry,
    ) -> "NextSessionSignal":
        _require_exact_keys(
            payload,
            {
                "schema_version",
                "signal_id",
                "strategy_id",
                "channel",
                "strategy_date",
                "execution_date",
                "frozen_at",
                "portfolio_intent",
                "construction",
                "constructor_policy",
                "data_snapshot_sha256",
                "model_sha256",
                "policy_sha256",
                "intent_sha256",
                "expected_account_state_sha256",
                "calendar_receipt",
                "calendar_receipt_sha256",
                "calendar_registry_sha256",
                "execution_rule_bundle_sha256",
                "execution_rule_trust_statement",
                "frozen_reference_prices",
                "maximum_execution_price_deviation",
                "one_shot",
                "manual_execution_required",
                "automatic_submission",
                "paper_eligibility",
                "trade_eligibility",
                "live_supported",
                "signal_sha256",
            },
            "next-session signal",
        )
        if payload.get("schema_version") != NEXT_SESSION_SIGNAL_SCHEMA_VERSION:
            raise NextSessionSignalError("unsupported next-session signal schema")
        if payload.get("strategy_id") != STRATEGY_ID:
            raise NextSessionSignalError("next-session strategy_id mismatch")
        if (
            payload.get("one_shot") is not True
            or payload.get("manual_execution_required") is not True
            or payload.get("automatic_submission") is not False
            or payload.get("paper_eligibility") is not False
            or payload.get("trade_eligibility") is not False
            or payload.get("live_supported") is not False
        ):
            raise NextSessionSignalError("next-session safety boundary drifted")
        try:
            receipt = OfficialCalendarReceipt.from_dict(payload["calendar_receipt"])
            registry.verify(receipt)
            signal = cls(
                signal_id=str(payload["signal_id"]),
                channel=NextSessionChannel(str(payload["channel"])),
                strategy_date=date.fromisoformat(str(payload["strategy_date"])),
                execution_date=date.fromisoformat(str(payload["execution_date"])),
                frozen_at=datetime.fromisoformat(str(payload["frozen_at"])),
                portfolio_intent=payload["portfolio_intent"],
                construction=payload["construction"],
                constructor_policy=payload["constructor_policy"],
                data_snapshot_sha256=str(payload["data_snapshot_sha256"]),
                model_sha256=str(payload["model_sha256"]),
                policy_sha256=str(payload["policy_sha256"]),
                intent_sha256=str(payload["intent_sha256"]),
                expected_account_state_sha256=str(payload["expected_account_state_sha256"]),
                calendar_receipt=payload["calendar_receipt"],
                calendar_receipt_sha256=str(payload["calendar_receipt_sha256"]),
                calendar_registry_sha256=str(payload["calendar_registry_sha256"]),
                execution_rule_bundle_sha256=str(
                    payload["execution_rule_bundle_sha256"]
                ),
                frozen_reference_prices=payload["frozen_reference_prices"],
                maximum_execution_price_deviation=_decimal(
                    payload["maximum_execution_price_deviation"],
                    "maximum_execution_price_deviation",
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise NextSessionSignalError("next-session signal is malformed") from exc
        if payload.get("execution_rule_trust_statement") != (
            "hash_consistency_only_metadata_source_not_authenticated"
        ):
            raise NextSessionSignalError("execution-rule trust statement drifted")
        if signal.execution_date != receipt.next_session_after(signal.strategy_date):
            raise NextSessionSignalError("signal execution_date is not receipt-adjacent D+1")
        if signal.calendar_receipt_sha256 != receipt.receipt_sha256:
            raise NextSessionSignalError("signal calendar receipt binding mismatch")
        if signal.calendar_registry_sha256 != registry.registry_sha256:
            raise NextSessionSignalError("signal calendar registry binding mismatch")
        _verify_embedded_hash(
            _thaw(signal.construction),
            hash_field="construction_sha256",
            label="construction",
        )
        _verify_embedded_hash(
            _thaw(signal.constructor_policy),
            hash_field="policy_sha256",
            label="constructor_policy",
        )
        stored_hash = _sha256(str(payload.get("signal_sha256")), "signal_sha256")
        if signal.signal_sha256 != stored_hash:
            raise NextSessionSignalError("next-session signal hash mismatch")
        _validate_persisted_signal_semantics(signal)
        return signal


def _embedded_decimal(value: Any, field_name: str) -> Decimal:
    if not isinstance(value, str):
        raise NextSessionSignalError(f"{field_name} must use canonical decimal text")
    return _decimal(value, field_name)


def _validate_construction_payload(payload: Mapping[str, Any]) -> None:
    expected = {
        "schema_version",
        "strategy_id",
        "decision_at",
        "requested_intent_type",
        "intent_type",
        "input_snapshot_sha256",
        "model_sha256",
        "constructor_policy_sha256",
        "constructor_input_sha256",
        "account_state_sha256",
        "current_cash",
        "current_quantities",
        "current_nav",
        "current_gross_exposure",
        "current_stock_weights",
        "target_gross_exposure",
        "target_stock_weights",
        "feasible_gross_exposure",
        "feasible_stock_weights",
        "feasible_quantities",
        "projected_cash",
        "expected_improvement",
        "proposed_expected_cost",
        "expected_cost",
        "expected_cost_ratio",
        "required_improvement",
        "alpha_trade_allowed",
        "actions",
        "exclusions",
        "reason_codes",
        "manual_execution_required",
        "automatic_submission",
        "paper_eligibility",
        "trade_eligibility",
        "real_money_list_allowed",
        "live_supported",
        "construction_sha256",
    }
    _require_exact_keys(payload, expected, "embedded construction")
    if (
        payload["schema_version"] != "portfolio-construction-result.v2"
        or payload["strategy_id"] != STRATEGY_ID
        or payload["manual_execution_required"] is not True
        or payload["automatic_submission"] is not False
        or payload["paper_eligibility"] is not False
        or payload["trade_eligibility"] is not False
        or payload["real_money_list_allowed"] is not False
        or payload["live_supported"] is not False
        or type(payload["alpha_trade_allowed"]) is not bool
    ):
        raise NextSessionSignalError("embedded construction contract drifted")
    try:
        _aware(datetime.fromisoformat(str(payload["decision_at"])), "construction.decision_at")
        PortfolioIntentType(str(payload["requested_intent_type"]))
        PortfolioIntentType(str(payload["intent_type"]))
    except (TypeError, ValueError) as exc:
        raise NextSessionSignalError("embedded construction identity is malformed") from exc
    for field_name in (
        "input_snapshot_sha256",
        "model_sha256",
        "constructor_policy_sha256",
        "constructor_input_sha256",
        "account_state_sha256",
    ):
        _sha256(str(payload[field_name]), f"construction.{field_name}")
    for field_name in (
        "current_cash",
        "current_nav",
        "current_gross_exposure",
        "target_gross_exposure",
        "feasible_gross_exposure",
        "projected_cash",
        "proposed_expected_cost",
        "expected_cost",
        "expected_cost_ratio",
        "required_improvement",
    ):
        _embedded_decimal(payload[field_name], f"construction.{field_name}")
    if payload["expected_improvement"] is not None:
        _embedded_decimal(
            payload["expected_improvement"], "construction.expected_improvement"
        )
    for field_name in (
        "current_quantities",
        "current_stock_weights",
        "target_stock_weights",
        "feasible_stock_weights",
        "feasible_quantities",
    ):
        if not isinstance(payload[field_name], Mapping):
            raise NextSessionSignalError(
                f"construction.{field_name} must be an object"
            )
    for field_name in ("current_quantities", "feasible_quantities"):
        for instrument_id, quantity in payload[field_name].items():
            if _INSTRUMENT_RE.fullmatch(str(instrument_id)) is None:
                raise NextSessionSignalError("construction quantity instrument is invalid")
            if type(quantity) is not int or quantity < 0:
                raise NextSessionSignalError("construction quantity is invalid")
    for field_name in (
        "current_stock_weights",
        "target_stock_weights",
        "feasible_stock_weights",
    ):
        for instrument_id, weight in payload[field_name].items():
            if _INSTRUMENT_RE.fullmatch(str(instrument_id)) is None:
                raise NextSessionSignalError("construction weight instrument is invalid")
            parsed = _embedded_decimal(weight, "construction stock weight")
            if not ZERO <= parsed <= Decimal("1"):
                raise NextSessionSignalError("construction stock weight is invalid")
    actions = payload["actions"]
    if not isinstance(actions, list) or not actions:
        raise NextSessionSignalError("embedded construction actions are malformed")
    action_keys = {
        "action",
        "instrument_id",
        "order_quantity",
        "whole_lots",
        "odd_lot_quantity",
        "current_quantity",
        "target_quantity",
        "lot_size",
        "reference_price",
        "target_weight",
        "feasible_weight",
        "estimated_cost",
        "reason_codes",
    }
    for item in actions:
        if not isinstance(item, Mapping):
            raise NextSessionSignalError("construction action must be an object")
        _require_exact_keys(item, action_keys, "construction action")
        try:
            action = ConstructionActionType(str(item["action"]))
        except ValueError as exc:
            raise NextSessionSignalError("construction action type is invalid") from exc
        instrument_id = item["instrument_id"]
        if action is ConstructionActionType.CASH:
            if instrument_id is not None or item["lot_size"] is not None:
                raise NextSessionSignalError("CASH action cannot name an instrument")
        elif (
            not isinstance(instrument_id, str)
            or _INSTRUMENT_RE.fullmatch(instrument_id) is None
            or type(item["lot_size"]) is not int
            or item["lot_size"] <= 0
        ):
            raise NextSessionSignalError("construction action instrument/lot is invalid")
        for field_name in (
            "order_quantity",
            "whole_lots",
            "odd_lot_quantity",
            "current_quantity",
            "target_quantity",
        ):
            if type(item[field_name]) is not int or item[field_name] < 0:
                raise NextSessionSignalError("construction action quantity is invalid")
        if action in {ConstructionActionType.BUY, ConstructionActionType.SELL}:
            if item["order_quantity"] <= 0:
                raise NextSessionSignalError("trade action requires positive quantity")
        elif item["order_quantity"] != 0:
            raise NextSessionSignalError("non-trade action cannot carry order quantity")
        for field_name in (
            "reference_price",
            "target_weight",
            "feasible_weight",
            "estimated_cost",
        ):
            _embedded_decimal(item[field_name], f"construction action {field_name}")
        if not isinstance(item["reason_codes"], list):
            raise NextSessionSignalError("construction action reasons are malformed")
    exclusions = payload["exclusions"]
    if not isinstance(exclusions, list):
        raise NextSessionSignalError("construction exclusions are malformed")
    for item in exclusions:
        if not isinstance(item, Mapping):
            raise NextSessionSignalError("construction exclusion must be an object")
        _require_exact_keys(item, {"instrument_id", "codes"}, "construction exclusion")
        if (
            _INSTRUMENT_RE.fullmatch(str(item["instrument_id"])) is None
            or not isinstance(item["codes"], list)
            or not item["codes"]
        ):
            raise NextSessionSignalError("construction exclusion is malformed")
    if not isinstance(payload["reason_codes"], list):
        raise NextSessionSignalError("construction reason_codes are malformed")
    _verify_embedded_hash(
        payload,
        hash_field="construction_sha256",
        label="construction",
    )


def _validate_persisted_signal_semantics(signal: NextSessionSignal) -> None:
    intent_payload = _thaw(signal.portfolio_intent)
    construction = _thaw(signal.construction)
    policy_payload = _thaw(signal.constructor_policy)
    if not all(
        isinstance(item, Mapping)
        for item in (intent_payload, construction, policy_payload)
    ):
        raise NextSessionSignalError("frozen signal payloads must be objects")
    intent = _intent_from_embedded(intent_payload)
    policy = _policy_from_embedded(policy_payload)
    _validate_construction_payload(construction)

    expected_types = (
        _ALPHA_TYPES
        if signal.channel is NextSessionChannel.ALPHA
        else _RISK_TYPES
    )
    if intent.intent_type not in expected_types:
        raise NextSessionSignalError(
            "persisted signal channel and PortfolioIntent type differ"
        )
    action_rows = construction["actions"]
    has_buy = any(item["action"] == "BUY" for item in action_rows)
    if signal.channel is NextSessionChannel.RISK_REDUCTION and has_buy:
        raise NextSessionSignalError("persisted risk signal cannot contain BUY")
    if intent.intent_type is PortfolioIntentType.NO_ALPHA_CASH and has_buy:
        raise NextSessionSignalError("NO_ALPHA_CASH signal cannot contain BUY")
    construction_decision_at = _aware(
        datetime.fromisoformat(str(construction["decision_at"])),
        "construction.decision_at",
    )
    local_decision = intent.decision_at.astimezone(CHINA_STANDARD_TIME)
    if (
        local_decision.date() != signal.strategy_date
        or local_decision.time().replace(tzinfo=None) < time(15, 0)
        or construction_decision_at != intent.decision_at
        or signal.frozen_at != intent.frozen_at
    ):
        raise NextSessionSignalError("persisted signal decision boundary drifted")
    if (
        str(construction["intent_type"]) != intent.intent_type.value
        or str(construction["input_snapshot_sha256"])
        != intent.market_data_sha256
        or str(construction["model_sha256"]) != intent.model_sha256
        or str(construction["constructor_policy_sha256"])
        != policy.policy_sha256
        or str(construction["construction_sha256"])
        != intent.signal_sha256
        or _embedded_decimal(
            construction["target_gross_exposure"],
            "construction.target_gross_exposure",
        )
        != intent.target_gross_exposure
        or {
            str(key): _embedded_decimal(value, "construction feasible weight")
            for key, value in construction["feasible_stock_weights"].items()
        }
        != dict(intent.target_weights)
    ):
        raise NextSessionSignalError("persisted intent/construction binding mismatch")
    if (
        signal.data_snapshot_sha256
        != str(construction["input_snapshot_sha256"])
        or signal.model_sha256 != str(construction["model_sha256"])
        or signal.policy_sha256 != policy.policy_sha256
        or signal.intent_sha256 != intent.intent_sha256
        or signal.expected_account_state_sha256
        != str(construction["account_state_sha256"])
        or signal.maximum_execution_price_deviation
        != policy.maximum_execution_price_deviation
    ):
        raise NextSessionSignalError("persisted signal top-level binding mismatch")
    trade_prices = {
        str(item["instrument_id"]): _embedded_decimal(
            item["reference_price"], "construction trade reference_price"
        )
        for item in action_rows
        if item["action"] in {"BUY", "SELL"}
    }
    if trade_prices != dict(signal.frozen_reference_prices):
        raise NextSessionSignalError("frozen reference prices do not match trade actions")
    identity_hash = canonical_sha256(
        {
            "scope": "next-session-signal-id.v1",
            "strategy_id": STRATEGY_ID,
            "strategy_date": signal.strategy_date,
            "execution_date": signal.execution_date,
            "channel": signal.channel.value,
            "intent_sha256": intent.intent_sha256,
            "construction_sha256": str(construction["construction_sha256"]),
            "calendar_receipt_sha256": signal.calendar_receipt_sha256,
            "execution_rule_bundle_sha256": signal.execution_rule_bundle_sha256,
        }
    )
    if signal.signal_id != f"next-session:{identity_hash[:24]}":
        raise NextSessionSignalError("persisted signal_id semantic binding mismatch")


def _validate_factory_inputs(
    *,
    intent: PortfolioIntent,
    construction: PortfolioConstructionResult,
    policy: PortfolioConstructorPolicy,
    receipt: OfficialCalendarReceipt,
    registry: OfficialCalendarRegistry,
    fees: FeeSchedule,
    instrument_rules: Mapping[str, InstrumentRule],
    channel: NextSessionChannel,
) -> tuple[date, date, str]:
    if not isinstance(intent, PortfolioIntent):
        raise NextSessionSignalError("intent must be PortfolioIntent")
    if intent.strategy_id != STRATEGY_ID:
        raise NextSessionSignalError("intent strategy_id mismatch")
    if not isinstance(construction, PortfolioConstructionResult):
        raise NextSessionSignalError("construction must be PortfolioConstructionResult")
    if not isinstance(policy, PortfolioConstructorPolicy):
        raise NextSessionSignalError("policy must be PortfolioConstructorPolicy")
    if not isinstance(registry, OfficialCalendarRegistry):
        raise NextSessionSignalError(
            "calendar trust requires OfficialCalendarRegistry, not a boolean or string"
        )
    registry.verify(receipt)
    if registry.frozen_at > intent.decision_at:
        raise NextSessionSignalError("calendar registry must be frozen before decision")
    if receipt.available_at > intent.decision_at:
        raise NextSessionSignalError("calendar receipt was unavailable at decision time")
    expected_types = _ALPHA_TYPES if channel is NextSessionChannel.ALPHA else _RISK_TYPES
    if intent.intent_type not in expected_types:
        raise NextSessionSignalError(
            "intent type is not permitted on this next-session channel"
        )
    if construction.intent_type is not intent.intent_type:
        raise NextSessionSignalError("constructor and PortfolioIntent types differ")
    if construction.decision_at != intent.decision_at:
        raise NextSessionSignalError("constructor and PortfolioIntent decision_at differ")
    if construction.input_snapshot_sha256 != intent.market_data_sha256:
        raise NextSessionSignalError("intent market data hash does not bind construction")
    if construction.model_sha256 != intent.model_sha256:
        raise NextSessionSignalError("intent model hash does not bind construction")
    if construction.constructor_policy_sha256 != policy.policy_sha256:
        raise NextSessionSignalError("constructor policy hash mismatch")
    if intent.signal_sha256 != construction.construction_sha256:
        raise NextSessionSignalError("PortfolioIntent signal hash must bind construction")
    if intent.target_gross_exposure != construction.target_gross_exposure:
        raise NextSessionSignalError("PortfolioIntent target exposure differs from construction")
    intent_weights = {key: Decimal(str(value)) for key, value in intent.target_weights.items()}
    if intent_weights != dict(construction.feasible_stock_weights):
        raise NextSessionSignalError("PortfolioIntent weights must equal feasible construction weights")
    if channel is NextSessionChannel.RISK_REDUCTION and any(
        item.action is ConstructionActionType.BUY for item in construction.actions
    ):
        raise NextSessionSignalError("risk next-session signal cannot contain BUY")
    try:
        rule_bundle_sha256 = execution_rule_bundle_sha256(fees, instrument_rules)
    except (TypeError, ValueError) as exc:
        raise NextSessionSignalError(
            "canonical execution fee/instrument-rule bundle is invalid"
        ) from exc
    if policy.costs.commission_rate != fees.commission_rate:
        raise NextSessionSignalError(
            "constructor commission_rate differs from canonical FeeSchedule"
        )
    if policy.costs.minimum_commission != fees.minimum_commission:
        raise NextSessionSignalError(
            "constructor minimum_commission differs from canonical FeeSchedule"
        )
    if policy.costs.transfer_fee_rate != fees.exchange_fee_rate:
        raise NextSessionSignalError(
            "constructor transfer_fee_rate differs from canonical FeeSchedule"
        )
    for action in construction.actions:
        if (
            action.action not in {ConstructionActionType.BUY, ConstructionActionType.SELL}
            or action.instrument_id is None
        ):
            continue
        rule = instrument_rules.get(action.instrument_id)
        if rule is None:
            raise NextSessionSignalError(
                "canonical execution bundle misses a traded InstrumentRule"
            )
        if action.lot_size != rule.lot_size:
            raise NextSessionSignalError(
                "constructor lot_size differs from canonical InstrumentRule"
            )
        if policy.costs.sell_tax_rate != rule.sell_stamp_duty_rate:
            raise NextSessionSignalError(
                "constructor sell_tax_rate differs from canonical InstrumentRule"
            )
        if (
            action.action is ConstructionActionType.BUY
            and action.order_quantity % rule.lot_size != 0
        ):
            raise NextSessionSignalError(
                "frozen BUY quantity is not a whole lot under canonical InstrumentRule"
            )
    local_decision = intent.decision_at.astimezone(CHINA_STANDARD_TIME)
    if local_decision.time().replace(tzinfo=None) < time(15, 0):
        raise NextSessionSignalError(
            "next-session signal must be frozen at or after the D-session close"
        )
    strategy_date = local_decision.date()
    execution_date = receipt.next_session_after(strategy_date)
    return strategy_date, execution_date, rule_bundle_sha256


def _create_signal(
    *,
    intent: PortfolioIntent,
    construction: PortfolioConstructionResult,
    policy: PortfolioConstructorPolicy,
    receipt: OfficialCalendarReceipt,
    registry: OfficialCalendarRegistry,
    fees: FeeSchedule,
    instrument_rules: Mapping[str, InstrumentRule],
    channel: NextSessionChannel,
) -> NextSessionSignal:
    strategy_date, execution_date, rule_bundle_sha256 = _validate_factory_inputs(
        intent=intent,
        construction=construction,
        policy=policy,
        receipt=receipt,
        registry=registry,
        fees=fees,
        instrument_rules=instrument_rules,
        channel=channel,
    )
    trade_prices = {
        item.instrument_id: item.reference_price
        for item in construction.actions
        if item.action in {ConstructionActionType.BUY, ConstructionActionType.SELL}
        and item.instrument_id is not None
    }
    identity_hash = canonical_sha256(
        {
            "scope": "next-session-signal-id.v1",
            "strategy_id": STRATEGY_ID,
            "strategy_date": strategy_date,
            "execution_date": execution_date,
            "channel": channel.value,
            "intent_sha256": intent.intent_sha256,
            "construction_sha256": construction.construction_sha256,
            "calendar_receipt_sha256": receipt.receipt_sha256,
            "execution_rule_bundle_sha256": rule_bundle_sha256,
        }
    )
    return NextSessionSignal(
        signal_id=f"next-session:{identity_hash[:24]}",
        channel=channel,
        strategy_date=strategy_date,
        execution_date=execution_date,
        frozen_at=intent.frozen_at,
        portfolio_intent=intent.to_dict(),
        construction=construction.to_dict(),
        constructor_policy=policy.to_dict(),
        data_snapshot_sha256=construction.input_snapshot_sha256,
        model_sha256=construction.model_sha256,
        policy_sha256=policy.policy_sha256,
        intent_sha256=intent.intent_sha256,
        expected_account_state_sha256=construction.account_state_sha256,
        calendar_receipt=receipt.to_dict(),
        calendar_receipt_sha256=receipt.receipt_sha256,
        calendar_registry_sha256=registry.registry_sha256,
        execution_rule_bundle_sha256=rule_bundle_sha256,
        frozen_reference_prices=trade_prices,
        maximum_execution_price_deviation=(
            policy.maximum_execution_price_deviation
        ),
    )


def create_alpha_next_session_signal(
    *,
    intent: PortfolioIntent,
    construction: PortfolioConstructionResult,
    policy: PortfolioConstructorPolicy,
    receipt: OfficialCalendarReceipt,
    registry: OfficialCalendarRegistry,
    fees: FeeSchedule,
    instrument_rules: Mapping[str, InstrumentRule],
) -> NextSessionSignal:
    """Create a normal Alpha D+1 signal; rejects all cash/risk exits."""

    return _create_signal(
        intent=intent,
        construction=construction,
        policy=policy,
        receipt=receipt,
        registry=registry,
        fees=fees,
        instrument_rules=instrument_rules,
        channel=NextSessionChannel.ALPHA,
    )


def create_risk_next_session_signal(
    *,
    intent: PortfolioIntent,
    construction: PortfolioConstructionResult,
    policy: PortfolioConstructorPolicy,
    receipt: OfficialCalendarReceipt,
    registry: OfficialCalendarRegistry,
    fees: FeeSchedule,
    instrument_rules: Mapping[str, InstrumentRule],
) -> NextSessionSignal:
    """Create an explicit reduction-only D+1 signal; never an Alpha alias."""

    return _create_signal(
        intent=intent,
        construction=construction,
        policy=policy,
        receipt=receipt,
        registry=registry,
        fees=fees,
        instrument_rules=instrument_rules,
        channel=NextSessionChannel.RISK_REDUCTION,
    )


def _write_create_only(path: Path, payload: Mapping[str, Any], label: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = canonical_json_bytes(payload) + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise NextSessionAlreadyConsumed(f"{label} already exists; refusing overwrite") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        # The exclusive path is deliberately retained on write failure.  It is
        # safer to require manual recovery than to permit a second consumer.
        raise
    return path


def _registry_directories() -> tuple[Path, Path, Path]:
    """Return the fixed repository/strategy one-shot registry directories."""

    configured_root = Path(NEXT_SESSION_REGISTRY_ROOT)
    if configured_root.is_symlink():
        raise NextSessionSignalError(
            "fixed next-session registry root must not be a symlink"
        )
    root = configured_root.resolve(strict=False)
    return (
        root,
        root / NEXT_SESSION_CONSUMPTION_REGISTRY_DIR,
        root / NEXT_SESSION_MANUAL_FILL_REGISTRY_DIR,
    )


def _ensure_registry_directories() -> tuple[Path, Path, Path]:
    root, consumptions, manual_fills = _registry_directories()
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir() or root.is_symlink():
        raise NextSessionSignalError(
            "fixed next-session registry root must be a regular directory"
        )
    for directory in (consumptions, manual_fills):
        directory.mkdir(exist_ok=True)
        if not directory.is_dir() or directory.is_symlink():
            raise NextSessionSignalError(
                "fixed next-session registry child must be a regular directory"
            )
    return root, consumptions, manual_fills


def write_new_next_session_signal(path: str | Path, signal: NextSessionSignal) -> Path:
    if not isinstance(signal, NextSessionSignal):
        raise NextSessionSignalError("signal must be NextSessionSignal")
    _validate_persisted_signal_semantics(signal)
    _ensure_registry_directories()
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = canonical_json_bytes(signal.to_dict()) + b"\n"
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        try:
            existing = target.read_bytes()
        except OSError as read_exc:
            raise NextSessionSignalConflict(
                "frozen next-session signal exists but cannot be verified"
            ) from read_exc
        if existing == encoded:
            return target
        raise NextSessionSignalConflict(
            "frozen next-session signal path contains different bytes"
        ) from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        # Retain a partially-created path for explicit operator recovery; an
        # automatic overwrite could silently replace a different decision.
        raise
    return target


def read_next_session_signal(
    path: str | Path,
    *,
    registry: OfficialCalendarRegistry,
) -> NextSessionSignal:
    source = Path(path)
    try:
        raw = source.read_bytes()
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
    except NextSessionSignalError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NextSessionSignalError("cannot read next-session signal") from exc
    if not isinstance(payload, Mapping):
        raise NextSessionSignalError("next-session signal root must be an object")
    if raw != canonical_json_bytes(payload) + b"\n":
        raise NextSessionSignalError("next-session signal bytes are not canonical")
    return NextSessionSignal.from_dict(payload, registry=registry)


@dataclass(frozen=True, slots=True)
class ManualExecutionInstruction:
    action: str
    instrument_id: str | None
    quantity: int
    execution_lot_size: int | None
    frozen_reference_price: Decimal
    observed_execution_price: Decimal | None
    maximum_execution_price_deviation: Decimal
    observed_price_deviation: Decimal | None
    estimated_cost: Decimal
    status: InstructionStatus
    cancel_conditions: tuple[str, ...]

    def __post_init__(self) -> None:
        action = str(self.action).strip().upper()
        if action not in {"BUY", "SELL", "HOLD", "CASH"}:
            raise NextSessionSignalError("manual instruction action is invalid")
        if not isinstance(self.status, InstructionStatus):
            raise NextSessionSignalError("manual instruction status is invalid")
        if type(self.quantity) is not int or self.quantity < 0:
            raise NextSessionSignalError("manual instruction quantity is invalid")
        if self.execution_lot_size is not None and (
            type(self.execution_lot_size) is not int
            or self.execution_lot_size <= 0
        ):
            raise NextSessionSignalError("manual instruction lot size is invalid")
        frozen = _decimal(
            self.frozen_reference_price, "frozen_reference_price"
        )
        observed = (
            None
            if self.observed_execution_price is None
            else _decimal(
                self.observed_execution_price, "observed_execution_price"
            )
        )
        maximum_deviation = _decimal(
            self.maximum_execution_price_deviation,
            "maximum_execution_price_deviation",
        )
        observed_deviation = (
            None
            if self.observed_price_deviation is None
            else _decimal(
                self.observed_price_deviation, "observed_price_deviation"
            )
        )
        estimated_cost = _decimal(self.estimated_cost, "estimated_cost")
        if maximum_deviation < ZERO or estimated_cost < ZERO:
            raise NextSessionSignalError(
                "manual instruction deviation/cost must be non-negative"
            )
        conditions = tuple(str(item).strip() for item in self.cancel_conditions)
        if any(not item for item in conditions) or len(conditions) != len(
            set(conditions)
        ):
            raise NextSessionSignalError(
                "manual instruction cancel conditions are invalid"
            )
        conditions = tuple(sorted(conditions))
        instrument_id = self.instrument_id
        if action in {"BUY", "SELL"}:
            if (
                not isinstance(instrument_id, str)
                or _INSTRUMENT_RE.fullmatch(instrument_id) is None
                or self.quantity <= 0
                or frozen <= ZERO
                or self.status
                not in {
                    InstructionStatus.READY_FOR_MANUAL_EXECUTION,
                    InstructionStatus.CANCELED,
                }
            ):
                raise NextSessionSignalError(
                    "manual trade instruction contract is invalid"
                )
            if self.status is InstructionStatus.READY_FOR_MANUAL_EXECUTION and (
                self.execution_lot_size is None
                or observed is None
                or conditions
            ):
                raise NextSessionSignalError(
                    "ready manual trade instruction is incomplete"
                )
            if self.status is InstructionStatus.CANCELED and not conditions:
                raise NextSessionSignalError(
                    "canceled manual trade requires a cancel condition"
                )
        elif action == "HOLD":
            if (
                not isinstance(instrument_id, str)
                or _INSTRUMENT_RE.fullmatch(instrument_id) is None
                or self.quantity != 0
                or self.status is not InstructionStatus.HOLD
                or conditions
            ):
                raise NextSessionSignalError("HOLD instruction is malformed")
        elif (
            instrument_id is not None
            or self.quantity != 0
            or self.status is not InstructionStatus.CASH
            or conditions
        ):
            raise NextSessionSignalError("CASH instruction is malformed")
        if observed is not None and observed <= ZERO:
            raise NextSessionSignalError(
                "observed execution price must be positive"
            )
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "frozen_reference_price", frozen)
        object.__setattr__(self, "observed_execution_price", observed)
        object.__setattr__(
            self, "maximum_execution_price_deviation", maximum_deviation
        )
        object.__setattr__(self, "observed_price_deviation", observed_deviation)
        object.__setattr__(self, "estimated_cost", estimated_cost)
        object.__setattr__(self, "cancel_conditions", conditions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "instrument_id": self.instrument_id,
            "quantity": self.quantity,
            "execution_lot_size": self.execution_lot_size,
            "frozen_reference_price": str(self.frozen_reference_price),
            "observed_execution_price": (
                str(self.observed_execution_price)
                if self.observed_execution_price is not None
                else None
            ),
            "maximum_execution_price_deviation": str(
                self.maximum_execution_price_deviation
            ),
            "observed_price_deviation": (
                str(self.observed_price_deviation)
                if self.observed_price_deviation is not None
                else None
            ),
            "estimated_cost": str(self.estimated_cost),
            "status": self.status.value,
            "cancel_conditions": list(self.cancel_conditions),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ManualExecutionInstruction":
        _require_exact_keys(
            payload,
            {
                "action",
                "instrument_id",
                "quantity",
                "execution_lot_size",
                "frozen_reference_price",
                "observed_execution_price",
                "maximum_execution_price_deviation",
                "observed_price_deviation",
                "estimated_cost",
                "status",
                "cancel_conditions",
            },
            "manual execution instruction",
        )
        try:
            conditions = payload["cancel_conditions"]
            if not isinstance(conditions, list):
                raise TypeError("cancel_conditions must be a list")
            return cls(
                action=str(payload["action"]),
                instrument_id=(
                    None
                    if payload["instrument_id"] is None
                    else str(payload["instrument_id"])
                ),
                quantity=payload["quantity"],
                execution_lot_size=payload["execution_lot_size"],
                frozen_reference_price=_decimal(
                    payload["frozen_reference_price"],
                    "frozen_reference_price",
                ),
                observed_execution_price=(
                    None
                    if payload["observed_execution_price"] is None
                    else _decimal(
                        payload["observed_execution_price"],
                        "observed_execution_price",
                    )
                ),
                maximum_execution_price_deviation=_decimal(
                    payload["maximum_execution_price_deviation"],
                    "maximum_execution_price_deviation",
                ),
                observed_price_deviation=(
                    None
                    if payload["observed_price_deviation"] is None
                    else _decimal(
                        payload["observed_price_deviation"],
                        "observed_price_deviation",
                    )
                ),
                estimated_cost=_decimal(payload["estimated_cost"], "estimated_cost"),
                status=InstructionStatus(str(payload["status"])),
                cancel_conditions=tuple(conditions),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise NextSessionSignalError(
                "manual execution instruction is malformed"
            ) from exc


@dataclass(frozen=True, slots=True)
class NextSessionConsumption:
    signal_id: str
    signal_sha256: str
    execution_date: date
    checked_at: datetime
    account_fingerprint: str
    account_state_sha256: str
    execution_quote_bundle_sha256: str
    execution_rule_bundle_sha256: str
    expected_cost: Decimal
    status: str
    instructions: tuple[ManualExecutionInstruction, ...]
    cancel_reasons: tuple[str, ...]
    consumption_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "signal_id", _identifier(self.signal_id, "signal_id"))
        for field_name in (
            "signal_sha256",
            "account_fingerprint",
            "account_state_sha256",
            "execution_quote_bundle_sha256",
            "execution_rule_bundle_sha256",
        ):
            object.__setattr__(
                self, field_name, _sha256(getattr(self, field_name), field_name)
            )
        if type(self.execution_date) is not date:
            raise NextSessionSignalError("consumption execution_date is invalid")
        object.__setattr__(self, "checked_at", _aware(self.checked_at, "checked_at"))
        expected_cost = _decimal(self.expected_cost, "expected_cost")
        if expected_cost < ZERO:
            raise NextSessionSignalError("consumption expected_cost is invalid")
        instructions = tuple(self.instructions)
        if any(
            not isinstance(item, ManualExecutionInstruction)
            for item in instructions
        ):
            raise NextSessionSignalError(
                "consumption instructions must be typed"
            )
        named_ids = [
            item.instrument_id
            for item in instructions
            if item.instrument_id is not None
        ]
        if len(named_ids) != len(set(named_ids)):
            raise NextSessionSignalError(
                "consumption instructions must have unique instruments"
            )
        cancel_reasons = tuple(str(item).strip() for item in self.cancel_reasons)
        if any(not item for item in cancel_reasons) or len(cancel_reasons) != len(
            set(cancel_reasons)
        ):
            raise NextSessionSignalError("consumption cancel reasons are invalid")
        cancel_reasons = tuple(sorted(cancel_reasons))
        ready = sum(
            item.status is InstructionStatus.READY_FOR_MANUAL_EXECUTION
            for item in instructions
        )
        canceled = sum(
            item.status is InstructionStatus.CANCELED for item in instructions
        )
        trade_count = ready + canceled
        expected_status = (
            "NO_ORDERS_HOLD_OR_CASH"
            if trade_count == 0
            else "READY_FOR_MANUAL_EXECUTION"
            if ready == trade_count
            else "PARTIALLY_READY_FOR_MANUAL_EXECUTION"
            if ready > 0
            else "CANCELED"
        )
        if self.status != expected_status:
            raise NextSessionSignalError("consumption status is inconsistent")
        recomputed_cost = sum(
            (
                item.estimated_cost
                for item in instructions
                if item.status is InstructionStatus.READY_FOR_MANUAL_EXECUTION
            ),
            ZERO,
        )
        if expected_cost != recomputed_cost:
            raise NextSessionSignalError("consumption expected_cost is inconsistent")
        instruction_reasons = {
            reason for item in instructions for reason in item.cancel_conditions
        }
        if not instruction_reasons.issubset(set(cancel_reasons)):
            raise NextSessionSignalError(
                "consumption cancel reasons omit instruction evidence"
            )
        object.__setattr__(self, "expected_cost", expected_cost)
        object.__setattr__(self, "instructions", instructions)
        object.__setattr__(self, "cancel_reasons", cancel_reasons)
        object.__setattr__(self, "consumption_sha256", canonical_sha256(self.to_content_dict()))

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": NEXT_SESSION_CONSUMPTION_SCHEMA_VERSION,
            "signal_id": self.signal_id,
            "signal_sha256": self.signal_sha256,
            "execution_date": self.execution_date,
            "checked_at": self.checked_at,
            "account_fingerprint": self.account_fingerprint,
            "account_state_sha256": self.account_state_sha256,
            "execution_quote_bundle_sha256": self.execution_quote_bundle_sha256,
            "execution_rule_bundle_sha256": self.execution_rule_bundle_sha256,
            "expected_cost": self.expected_cost,
            "status": self.status,
            "instructions": [item.to_dict() for item in self.instructions],
            "cancel_reasons": list(self.cancel_reasons),
            "one_shot_consumed": True,
            "manual_execution_required": True,
            "automatic_submission": False,
            "execution_authority": "none",
            "paper_eligibility": False,
            "trade_eligibility": False,
            "live_supported": False,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.to_content_dict()
        payload["consumption_sha256"] = self.consumption_sha256
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "NextSessionConsumption":
        _require_exact_keys(
            payload,
            {
                "schema_version",
                "signal_id",
                "signal_sha256",
                "execution_date",
                "checked_at",
                "account_fingerprint",
                "account_state_sha256",
                "execution_quote_bundle_sha256",
                "execution_rule_bundle_sha256",
                "expected_cost",
                "status",
                "instructions",
                "cancel_reasons",
                "one_shot_consumed",
                "manual_execution_required",
                "automatic_submission",
                "execution_authority",
                "paper_eligibility",
                "trade_eligibility",
                "live_supported",
                "consumption_sha256",
            },
            "next-session consumption",
        )
        if (
            payload.get("schema_version") != NEXT_SESSION_CONSUMPTION_SCHEMA_VERSION
            or payload.get("one_shot_consumed") is not True
            or payload.get("manual_execution_required") is not True
            or payload.get("automatic_submission") is not False
            or payload.get("execution_authority") != "none"
            or payload.get("paper_eligibility") is not False
            or payload.get("trade_eligibility") is not False
            or payload.get("live_supported") is not False
        ):
            raise NextSessionSignalError(
                "next-session consumption safety boundary drifted"
            )
        try:
            raw_instructions = payload["instructions"]
            raw_reasons = payload["cancel_reasons"]
            if not isinstance(raw_instructions, list) or not isinstance(
                raw_reasons, list
            ):
                raise TypeError("consumption arrays are malformed")
            result = cls(
                signal_id=str(payload["signal_id"]),
                signal_sha256=str(payload["signal_sha256"]),
                execution_date=date.fromisoformat(str(payload["execution_date"])),
                checked_at=datetime.fromisoformat(str(payload["checked_at"])),
                account_fingerprint=str(payload["account_fingerprint"]),
                account_state_sha256=str(payload["account_state_sha256"]),
                execution_quote_bundle_sha256=str(
                    payload["execution_quote_bundle_sha256"]
                ),
                execution_rule_bundle_sha256=str(
                    payload["execution_rule_bundle_sha256"]
                ),
                expected_cost=_decimal(payload["expected_cost"], "expected_cost"),
                status=str(payload["status"]),
                instructions=tuple(
                    ManualExecutionInstruction.from_dict(item)
                    for item in raw_instructions
                ),
                cancel_reasons=tuple(raw_reasons),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise NextSessionSignalError(
                "next-session consumption is malformed"
            ) from exc
        stored_hash = _sha256(
            str(payload.get("consumption_sha256")), "consumption_sha256"
        )
        if result.consumption_sha256 != stored_hash:
            raise NextSessionSignalError(
                "next-session consumption hash mismatch"
            )
        return result


def _current_account_state_sha256(account: AccountSnapshot) -> str:
    return canonical_sha256(
        {
            "scope": "portfolio-constructor-account-state.v1",
            "strategy_id": account.strategy_id,
            "cash": account.cash,
            "positions": {
                key: position.quantity for key, position in sorted(account.positions.items())
            },
        }
    )


def _trade_actions(signal: NextSessionSignal) -> tuple[Mapping[str, Any], ...]:
    actions = _thaw(signal.construction).get("actions")
    if not isinstance(actions, list):
        raise NextSessionSignalError("frozen construction actions are malformed")
    if any(not isinstance(item, Mapping) for item in actions):
        raise NextSessionSignalError("frozen construction action item is malformed")
    return tuple(
        item for item in actions if item.get("action") in {"BUY", "SELL", "HOLD", "CASH"}
    )


def _validate_consumption_against_signal(
    signal: NextSessionSignal,
    consumption: NextSessionConsumption,
) -> None:
    """Bind every persisted instruction back to the frozen construction.

    D+1 quotes, account state, and rule hashes remain outputs of the controlled
    preflight.  This validator prevents a self-hashed file placed directly in
    the central slot from changing the frozen action set or enlarging a trade.
    """

    _validate_persisted_signal_semantics(signal)
    if (
        consumption.signal_id != signal.signal_id
        or consumption.signal_sha256 != signal.signal_sha256
        or consumption.execution_date != signal.execution_date
    ):
        raise NextSessionSignalError(
            "next-session consumption does not bind the frozen signal"
        )
    local_checked = consumption.checked_at.astimezone(CHINA_STANDARD_TIME)
    if (
        local_checked.date() != signal.execution_date
        or not OPENING_REVIEW_START
        <= local_checked.time().replace(tzinfo=None)
        <= OPENING_REVIEW_END
    ):
        raise NextSessionSignalError(
            "persisted consumption is outside the frozen opening review window"
        )
    frozen_actions = _trade_actions(signal)
    if len(consumption.instructions) != len(frozen_actions):
        raise NextSessionSignalError(
            "consumption instructions do not completely cover frozen actions"
        )
    for frozen_action, instruction in zip(
        frozen_actions,
        consumption.instructions,
    ):
        expected_action = str(frozen_action["action"])
        expected_instrument = frozen_action.get("instrument_id")
        expected_quantity = int(frozen_action.get("order_quantity", 0))
        expected_reference = _decimal(
            frozen_action.get("reference_price", "0"),
            "construction reference_price",
        )
        if (
            instruction.action != expected_action
            or instruction.instrument_id != expected_instrument
            or instruction.quantity != expected_quantity
            or instruction.frozen_reference_price != expected_reference
            or instruction.maximum_execution_price_deviation
            != signal.maximum_execution_price_deviation
        ):
            raise NextSessionSignalError(
                "consumption instruction changed a frozen construction action"
            )
        if expected_action in {"BUY", "SELL"}:
            expected_lot = frozen_action.get("lot_size")
            if type(expected_lot) is not int or expected_lot <= 0:
                raise NextSessionSignalError(
                    "frozen trade action has an invalid lot size"
                )
            if instruction.status is InstructionStatus.READY_FOR_MANUAL_EXECUTION:
                if instruction.execution_lot_size != expected_lot:
                    raise NextSessionSignalError(
                        "READY instruction lot differs from the frozen action"
                    )
            elif instruction.execution_lot_size != expected_lot:
                supported_lot_drift = {
                    "execution_instrument_rule_missing",
                    "instrument_lot_rule_changed_since_freeze",
                }
                if (
                    consumption.execution_rule_bundle_sha256
                    == signal.execution_rule_bundle_sha256
                    or not supported_lot_drift.intersection(
                        instruction.cancel_conditions
                    )
                ):
                    raise NextSessionSignalError(
                        "canceled instruction rewrote the frozen lot without evidence"
                    )
            if expected_action == "BUY":
                if signal.channel is NextSessionChannel.RISK_REDUCTION:
                    raise NextSessionSignalError(
                        "risk consumption cannot contain BUY"
                    )
                if instruction.observed_execution_price is not None:
                    expected_deviation = (
                        instruction.observed_execution_price
                        / expected_reference
                        - Decimal("1")
                    )
                    if instruction.observed_price_deviation != expected_deviation:
                        raise NextSessionSignalError(
                            "BUY observed deviation is inconsistent"
                        )
                    if (
                        expected_deviation
                        > signal.maximum_execution_price_deviation
                        and (
                            instruction.status is not InstructionStatus.CANCELED
                            or "buy_price_above_frozen_deviation_limit"
                            not in instruction.cancel_conditions
                        )
                    ):
                        raise NextSessionSignalError(
                            "BUY above the frozen ceiling was not canceled"
                        )
            elif instruction.observed_price_deviation is not None:
                raise NextSessionSignalError(
                    "SELL instruction cannot invent a BUY price deviation"
                )
        elif (
            instruction.execution_lot_size is not None
            or instruction.observed_execution_price is not None
            or instruction.observed_price_deviation is not None
            or instruction.estimated_cost != ZERO
        ):
            raise NextSessionSignalError(
                "HOLD/CASH consumption instruction changed frozen no-trade semantics"
            )
    if (
        consumption.execution_rule_bundle_sha256
        != signal.execution_rule_bundle_sha256
        and "execution_rule_bundle_mismatch" not in consumption.cancel_reasons
    ):
        raise NextSessionSignalError(
            "execution-rule drift lacks a persisted cancellation"
        )


def _preflight(
    signal: NextSessionSignal,
    *,
    registry: OfficialCalendarRegistry,
    account: AccountSnapshot,
    quotes: Mapping[str, MarketQuote],
    fees: FeeSchedule,
    instrument_rules: Mapping[str, InstrumentRule],
    checked_at: datetime,
) -> NextSessionConsumption:
    _validate_persisted_signal_semantics(signal)
    checked_at = _aware(checked_at, "checked_at")
    local_checked = checked_at.astimezone(CHINA_STANDARD_TIME)
    if local_checked.date() != signal.execution_date:
        raise NextSessionSignalError(
            "next-session signal may only be consumed on its bound D+1 date"
        )
    local_checked_time = local_checked.time().replace(tzinfo=None)
    if not OPENING_REVIEW_START <= local_checked_time <= OPENING_REVIEW_END:
        raise NextSessionSignalError(
            "next-session signal may only be consumed in the frozen 09:25-09:35 CST opening review window"
        )
    receipt = OfficialCalendarReceipt.from_dict(_thaw(signal.calendar_receipt))
    registry.verify(receipt)
    if registry.registry_sha256 != signal.calendar_registry_sha256:
        raise NextSessionSignalError("calendar registry changed after signal freeze")
    if receipt.next_session_after(signal.strategy_date) != signal.execution_date:
        raise NextSessionSignalError("calendar receipt no longer proves adjacent D+1")
    if not isinstance(account, AccountSnapshot) or account.strategy_id != STRATEGY_ID:
        raise NextSessionSignalError("D+1 account snapshot is absent or strategy-mismatched")
    if account.as_of is None:
        raise NextSessionSignalError("D+1 account snapshot requires as_of")
    account_as_of = _aware(account.as_of, "account.as_of")
    if (
        account_as_of.astimezone(CHINA_STANDARD_TIME).date()
        != signal.execution_date
        or account_as_of > checked_at
    ):
        raise NextSessionSignalError("D+1 account snapshot time is invalid")
    policy = _thaw(signal.constructor_policy)
    max_account_age = int(policy["maximum_account_age_seconds"])
    max_quote_age = int(policy["maximum_quote_age_seconds"])
    account_too_old = (checked_at - account_as_of).total_seconds() > max_account_age
    account_state = _current_account_state_sha256(account)
    global_cancel_reasons: set[str] = set()
    if account_too_old:
        global_cancel_reasons.add("account_snapshot_stale")
    if account_state != signal.expected_account_state_sha256:
        global_cancel_reasons.add("account_state_mismatch")
    try:
        rule_bundle_hash = execution_rule_bundle_sha256(fees, instrument_rules)
    except (TypeError, ValueError) as exc:
        raise NextSessionSignalError(
            "D+1 canonical execution fee/instrument-rule bundle is invalid"
        ) from exc
    if rule_bundle_hash != signal.execution_rule_bundle_sha256:
        global_cancel_reasons.add("execution_rule_bundle_mismatch")

    actions = _trade_actions(signal)
    trade_ids = {
        str(item["instrument_id"])
        for item in actions
        if item["action"] in {"BUY", "SELL"}
    }
    if set(quotes) != trade_ids:
        global_cancel_reasons.add("execution_quote_bundle_incomplete_or_extra")
    try:
        quote_hash = execution_quote_bundle_sha256(quotes)
    except (TypeError, ValueError) as exc:
        raise NextSessionSignalError("D+1 execution quote bundle is invalid") from exc

    preliminary: list[ManualExecutionInstruction] = []
    projected_cash = account.cash
    any_trade_canceled = False
    for action in actions:
        action_name = str(action["action"])
        instrument_id = action.get("instrument_id")
        quantity = int(action.get("order_quantity", 0))
        reference = _decimal(action.get("reference_price", "0"), "reference_price")
        if action_name == "HOLD":
            preliminary.append(
                ManualExecutionInstruction(
                    action=action_name,
                    instrument_id=str(instrument_id),
                    quantity=0,
                    execution_lot_size=None,
                    frozen_reference_price=reference,
                    observed_execution_price=None,
                    maximum_execution_price_deviation=signal.maximum_execution_price_deviation,
                    observed_price_deviation=None,
                    estimated_cost=ZERO,
                    status=InstructionStatus.HOLD,
                    cancel_conditions=(),
                )
            )
            continue
        if action_name == "CASH":
            preliminary.append(
                ManualExecutionInstruction(
                    action=action_name,
                    instrument_id=None,
                    quantity=0,
                    execution_lot_size=None,
                    frozen_reference_price=ZERO,
                    observed_execution_price=None,
                    maximum_execution_price_deviation=signal.maximum_execution_price_deviation,
                    observed_price_deviation=None,
                    estimated_cost=ZERO,
                    status=InstructionStatus.CASH,
                    cancel_conditions=(),
                )
            )
            continue

        cancel: set[str] = set(global_cancel_reasons)
        quote = quotes.get(str(instrument_id))
        rule = instrument_rules.get(str(instrument_id))
        frozen_lot_size = action.get("lot_size")
        observed_price: Decimal | None = None
        deviation: Decimal | None = None
        estimated_cost = ZERO
        if rule is None:
            cancel.add("execution_instrument_rule_missing")
        else:
            if frozen_lot_size != rule.lot_size:
                cancel.add("instrument_lot_rule_changed_since_freeze")
            if action_name == "BUY" and quantity % rule.lot_size != 0:
                cancel.add("buy_quantity_not_whole_lot_under_d_plus_one_rule")
        if quote is None:
            cancel.add("execution_quote_missing")
        else:
            if quote.instrument_id != instrument_id:
                cancel.add("execution_quote_instrument_mismatch")
            if (
                quote.as_of.astimezone(CHINA_STANDARD_TIME).date()
                != signal.execution_date
                or quote.as_of > checked_at
            ):
                cancel.add("execution_quote_time_invalid")
            elif (checked_at - quote.as_of).total_seconds() > max_quote_age:
                cancel.add("execution_quote_stale")
            if quote.suspended:
                cancel.add("instrument_suspended")
            if action_name == "BUY":
                observed_price = quote.ask
                if quote.buy_blocked:
                    cancel.add("buy_blocked")
                deviation = observed_price / reference - Decimal("1")
                if deviation > signal.maximum_execution_price_deviation:
                    cancel.add("buy_price_above_frozen_deviation_limit")
            else:
                observed_price = quote.bid
                if quote.sell_blocked:
                    cancel.add("sell_blocked")
                position = account.positions.get(str(instrument_id))
                if position is None or position.quantity < quantity:
                    cancel.add("account_position_quantity_insufficient")
                elif position.sellable_quantity < quantity:
                    cancel.add("sellable_quantity_insufficient")

        if not cancel and observed_price is not None and rule is not None:
            notional = observed_price * quantity
            costs = policy["costs"]
            canonical_fee = fees.estimate(
                Side.BUY if action_name == "BUY" else Side.SELL,
                notional,
                rule,
            )
            slippage = notional * _decimal(
                costs["slippage_bps_one_way"], "slippage_bps_one_way"
            ) / BPS
            estimated_cost = canonical_fee + slippage
            projected_cash += (
                -notional - estimated_cost
                if action_name == "BUY"
                else notional - estimated_cost
            )
        status = (
            InstructionStatus.CANCELED
            if cancel
            else InstructionStatus.READY_FOR_MANUAL_EXECUTION
        )
        any_trade_canceled = any_trade_canceled or bool(cancel)
        preliminary.append(
            ManualExecutionInstruction(
                action=action_name,
                instrument_id=str(instrument_id),
                quantity=quantity,
                execution_lot_size=rule.lot_size if rule is not None else None,
                frozen_reference_price=reference,
                observed_execution_price=observed_price,
                maximum_execution_price_deviation=signal.maximum_execution_price_deviation,
                observed_price_deviation=deviation,
                estimated_cost=estimated_cost,
                status=status,
                cancel_conditions=tuple(sorted(cancel)),
            )
        )

    if projected_cash < ZERO:
        global_cancel_reasons.add("projected_cash_negative_at_execution_quotes")
        any_trade_canceled = True

    # An ordinary Alpha rotation is atomic at this adapter boundary: if one
    # leg is canceled, no other BUY/SELL leg is presented as ready.  Pure cash
    # reduction and explicit risk reduction keep independent safe sell legs.
    atomic_alpha = (
        signal.channel is NextSessionChannel.ALPHA
        and _thaw(signal.portfolio_intent).get("intent_type") == "ALPHA_REBALANCE"
    )
    if atomic_alpha and any_trade_canceled:
        global_cancel_reasons.add("alpha_rebalance_atomic_preflight_failed")
    if projected_cash < ZERO:
        global_cancel_reasons.add("alpha_rebalance_atomic_preflight_failed")

    instructions: list[ManualExecutionInstruction] = []
    for item in preliminary:
        if item.action in {"BUY", "SELL"} and (
            (atomic_alpha and any_trade_canceled) or projected_cash < ZERO
        ):
            conditions = tuple(
                sorted(set(item.cancel_conditions) | global_cancel_reasons)
            )
            instructions.append(
                ManualExecutionInstruction(
                    action=item.action,
                    instrument_id=item.instrument_id,
                    quantity=item.quantity,
                    execution_lot_size=item.execution_lot_size,
                    frozen_reference_price=item.frozen_reference_price,
                    observed_execution_price=item.observed_execution_price,
                    maximum_execution_price_deviation=item.maximum_execution_price_deviation,
                    observed_price_deviation=item.observed_price_deviation,
                    estimated_cost=item.estimated_cost,
                    status=InstructionStatus.CANCELED,
                    cancel_conditions=conditions,
                )
            )
        else:
            instructions.append(item)

    ready = sum(
        item.status is InstructionStatus.READY_FOR_MANUAL_EXECUTION
        for item in instructions
    )
    canceled = sum(item.status is InstructionStatus.CANCELED for item in instructions)
    trade_count = ready + canceled
    if trade_count == 0:
        status = "NO_ORDERS_HOLD_OR_CASH"
    elif ready == trade_count:
        status = "READY_FOR_MANUAL_EXECUTION"
    elif ready > 0:
        status = "PARTIALLY_READY_FOR_MANUAL_EXECUTION"
    else:
        status = "CANCELED"
    all_cancel_reasons = set(global_cancel_reasons)
    for item in instructions:
        all_cancel_reasons.update(item.cancel_conditions)
    expected_cost = sum(
        (
            item.estimated_cost
            for item in instructions
            if item.status is InstructionStatus.READY_FOR_MANUAL_EXECUTION
        ),
        ZERO,
    )
    result = NextSessionConsumption(
        signal_id=signal.signal_id,
        signal_sha256=signal.signal_sha256,
        execution_date=signal.execution_date,
        checked_at=checked_at,
        account_fingerprint=account_fingerprint(account),
        account_state_sha256=account_state,
        execution_quote_bundle_sha256=quote_hash,
        execution_rule_bundle_sha256=rule_bundle_hash,
        expected_cost=expected_cost,
        status=status,
        instructions=tuple(instructions),
        cancel_reasons=tuple(sorted(all_cancel_reasons)),
    )
    _validate_consumption_against_signal(signal, result)
    return result


def consume_next_session_signal(
    signal_path: str | Path,
    consumption_path: str | Path,
    *,
    registry: OfficialCalendarRegistry,
    account: AccountSnapshot,
    quotes: Mapping[str, MarketQuote],
    fees: FeeSchedule,
    instrument_rules: Mapping[str, InstrumentRule],
    checked_at: datetime,
) -> NextSessionConsumption:
    """Run one D+1 preflight and atomically persist its one-shot outcome."""

    signal = read_next_session_signal(signal_path, registry=registry)
    _, consumption_registry, _ = _ensure_registry_directories()
    expected_consumption_path = canonical_next_session_consumption_path(
        signal_path,
        signal.signal_sha256,
    )
    if expected_consumption_path.parent != consumption_registry:
        raise NextSessionSignalError(
            "consumption path escaped the fixed strategy registry"
        )
    requested_consumption_path = Path(consumption_path)
    if (
        requested_consumption_path.resolve(strict=False)
        != expected_consumption_path.resolve(strict=False)
    ):
        raise NextSessionSignalError(
            "consumption_path must be the canonical signal-hash-derived one-shot path"
        )
    result = _preflight(
        signal,
        registry=registry,
        account=account,
        quotes=quotes,
        fees=fees,
        instrument_rules=instrument_rules,
        checked_at=checked_at,
    )
    _write_create_only(
        expected_consumption_path,
        result.to_dict(),
        "next-session consumption",
    )
    return result


def canonical_next_session_consumption_path(
    signal_path: str | Path,
    signal_sha256: str,
) -> Path:
    """Return the sole repository/strategy CAS slot for a signal.

    ``signal_sha256`` hashes the complete frozen signal content, including its
    ``signal_id``.  ``signal_path`` is retained for API compatibility but has
    no authority over the registry location.  A copied, renamed, hard-linked,
    or independently published copy therefore resolves to the same fixed slot.
    """

    if not isinstance(signal_path, (str, Path)):
        raise NextSessionSignalError("signal_path must be path-like")
    digest = _sha256(signal_sha256, "signal_sha256")
    _, consumption_registry, _ = _registry_directories()
    return consumption_registry / f"{digest}.consumed.json"


def canonical_manual_fill_bundle_path(consumption_sha256: str) -> Path:
    """Return the only permitted Stage-11 fill-bundle CAS path."""

    digest = _sha256(consumption_sha256, "consumption_sha256")
    _, _, manual_fill_registry = _registry_directories()
    return manual_fill_registry / f"{digest}.manual-fill.json"


def read_next_session_consumption(
    path: str | Path,
    *,
    signal: NextSessionSignal,
) -> NextSessionConsumption:
    """Strictly reload and bind the immutable central consumption artifact.

    The fixed registry's filesystem ACL is the writer trust boundary.  This
    reader binds frozen actions and validates the recorded D+1 hashes, but it
    cannot re-fetch the account/quote payloads from hashes alone.
    """

    if not isinstance(signal, NextSessionSignal):
        raise NextSessionSignalError("signal must be NextSessionSignal")
    expected = canonical_next_session_consumption_path(
        "registry-bound-signal",
        signal.signal_sha256,
    )
    source = Path(path)
    if source.resolve(strict=False) != expected.resolve(strict=False):
        raise NextSessionSignalError(
            "consumption path is not the fixed signal-hash-derived slot"
        )
    if not source.is_file() or source.is_symlink():
        raise NextSessionSignalError(
            "immutable next-session consumption artifact is absent or unsafe"
        )
    try:
        raw = source.read_bytes()
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
    except NextSessionSignalError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NextSessionSignalError(
            "cannot read next-session consumption"
        ) from exc
    if not isinstance(payload, Mapping):
        raise NextSessionSignalError(
            "next-session consumption root must be an object"
        )
    if raw != canonical_json_bytes(payload) + b"\n":
        raise NextSessionSignalError(
            "next-session consumption bytes are not canonical"
        )
    result = NextSessionConsumption.from_dict(payload)
    _validate_consumption_against_signal(signal, result)
    return result


__all__ = [
    "CALENDAR_RECEIPT_SCHEMA_VERSION",
    "CALENDAR_REGISTRY_SCHEMA_VERSION",
    "CalendarRegistryEntry",
    "InstructionStatus",
    "ManualExecutionInstruction",
    "NEXT_SESSION_CONSUMPTION_SCHEMA_VERSION",
    "NEXT_SESSION_CONSUMPTION_REGISTRY_DIR",
    "NEXT_SESSION_MANUAL_FILL_REGISTRY_DIR",
    "NEXT_SESSION_REGISTRY_ROOT",
    "NEXT_SESSION_SIGNAL_SCHEMA_VERSION",
    "NextSessionAlreadyConsumed",
    "NextSessionChannel",
    "NextSessionConsumption",
    "NextSessionSignal",
    "NextSessionSignalError",
    "NextSessionSignalConflict",
    "OfficialCalendarReceipt",
    "OfficialCalendarRegistry",
    "canonical_manual_fill_bundle_path",
    "canonical_next_session_consumption_path",
    "consume_next_session_signal",
    "create_alpha_next_session_signal",
    "create_risk_next_session_signal",
    "read_next_session_signal",
    "read_next_session_consumption",
    "write_new_next_session_signal",
]
