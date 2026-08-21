"""Daily, append-only Paper accounting for adaptive-exposure strategy V2.

This is intentionally independent from :mod:`paper_ledger`.  V1 stores one
record per 20-session alpha decision and therefore cannot prove intervening
daily NAV, a sticky drawdown latch, or repeated exit attempts.  V2 uses one
strict ``daily_session`` record for every controlled trading date.  Every
record is re-derived from the preceding state plus evidence-bound fills; a
caller cannot self-report cash, positions, costs, NAV, drawdown, or exposure.
The opening-window attempts bind ``execution_intent`` while the post-close
state binds ``closing_intent``.  This preserves the lawful sequence where an
alpha intent executes at D open and a newly observed D-close drawdown creates
the zero-exposure exit intent that governs D+1 retries.

The module is accounting evidence only.  It never submits or authorises an
order, and LIVE execution remains permanently unsupported.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from trading.costs import FeeSchedule
from trading.integrity import execution_rule_bundle_sha256
from trading.models import InstrumentRule, PortfolioIntent, PortfolioIntentType

from .adaptive_exposure import FROZEN_ADAPTIVE_EXPOSURE_POLICY_SHA256
from .contracts import canonical_json_bytes, canonical_sha256


PAPER_LEDGER_V2_VERSION = "strategy-paper-ledger-record.v2"
PAPER_LEDGER_V2_PRODUCER = "controlled-paper-ledger-v2"
PAPER_LEDGER_V2_STATUS = "forward-paper-daily-append-only-not-live"
ADAPTIVE_STRATEGY_ID = "a-share-small-account-adaptive-exposure-v2"
ADAPTIVE_POLICY_SCHEMA_VERSION = "strategy-adaptive-exposure-policy.v2"
PORTFOLIO_INTENT_SCHEMA_VERSION = "portfolio-intent.v1"
EXECUTION_COST_BUNDLE_SCHEMA_VERSION = "paper-execution-cost-bundle.v1"
CLOSE_MARK_BUNDLE_SCHEMA_VERSION = "controlled-close-mark-bundle.v1"
PAPER_CLOSE_EXECUTION_EVIDENCE_SCHEMA_VERSION = "paper-close-execution-evidence.v1"

CHINA_STANDARD_TIME = timezone(timedelta(hours=8))
ZERO = Decimal("0")
CENT = Decimal("0.01")
PCT = Decimal("0.00000001")
DRAWDOWN_TRIGGER = Decimal("0.12")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_INSTRUMENT_RE = re.compile(r"^[0-9A-Z][0-9A-Z.]{2,31}$")
_ATTEMPT_STATUSES = frozenset({"FILLED", "PARTIAL", "UNFILLED"})
_BLOCKED_EXIT_REASONS = frozenset(
    {
        "t_plus_one",
        "suspended",
        "limit_down_locked",
        "sellable_quantity",
        "quote_stale",
        "account_position_mismatch",
        "broker_rejected",
    }
)


class PaperLedgerV2Error(ValueError):
    """Raised when daily Paper evidence is missing, stale, or inconsistent."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PaperLedgerV2Error(f"{field_name} must be a timezone-aware datetime")
    return value


def _hash(value: Any, field_name: str) -> str:
    result = str(value).strip()
    if _SHA256_RE.fullmatch(result) is None:
        raise PaperLedgerV2Error(f"{field_name} must be a lowercase SHA-256")
    return result


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PaperLedgerV2Error(f"{field_name} must be non-empty text")
    return value.strip()


def _instrument(value: Any) -> str:
    result = _text(value, "instrument_id").upper()
    if _INSTRUMENT_RE.fullmatch(result) is None:
        raise PaperLedgerV2Error("instrument_id is invalid")
    return result


def _decimal(value: Any, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise PaperLedgerV2Error(f"{field_name} must be decimal") from exc
    if not result.is_finite():
        raise PaperLedgerV2Error(f"{field_name} must be finite")
    return result


def _money(value: Any, field_name: str) -> Decimal:
    return _decimal(value, field_name).quantize(CENT, rounding=ROUND_HALF_UP)


def _parse_date(value: Any, field_name: str) -> date:
    if not isinstance(value, str):
        raise PaperLedgerV2Error(f"{field_name} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise PaperLedgerV2Error(f"{field_name} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise PaperLedgerV2Error(f"{field_name} must use canonical ISO format")
    return parsed


def _parse_datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise PaperLedgerV2Error(f"{field_name} must be an ISO datetime")
    try:
        return _aware(datetime.fromisoformat(value), field_name)
    except ValueError as exc:
        raise PaperLedgerV2Error(f"{field_name} must be an ISO datetime") from exc


def _strict_object(raw: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise PaperLedgerV2Error(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=pairs)
    except PaperLedgerV2Error:
        raise
    except Exception as exc:
        raise PaperLedgerV2Error("ledger contains invalid JSON") from exc
    if not isinstance(value, dict):
        raise PaperLedgerV2Error("each ledger line must be a JSON object")
    return value


def _keys(value: Mapping[str, Any], expected: set[str], context: str) -> None:
    actual = set(value)
    if actual != expected:
        raise PaperLedgerV2Error(
            f"{context} keys differ: missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )


def _non_negative_decimal(value: Any, field_name: str) -> Decimal:
    result = _decimal(value, field_name)
    if result < ZERO:
        raise PaperLedgerV2Error(f"{field_name} must be non-negative")
    return result


@dataclass(frozen=True)
class CanonicalExecutionCostBundleV1:
    """Exact fees and instrument rules used to replay all Paper costs.

    ``execution_rule_bundle_sha256`` is deliberately recomputed with the same
    canonical function used by planner/Gate/D+1 preflight.  ``cost_bundle_sha256``
    additionally binds this ledger-facing schema.  Neither digest authenticates
    the external provenance of the rule metadata.
    """

    fee_schedule: FeeSchedule
    instrument_rules: Mapping[str, InstrumentRule]
    execution_rule_bundle_sha256: str = field(init=False)
    cost_bundle_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.fee_schedule, FeeSchedule):
            raise PaperLedgerV2Error("execution cost bundle requires FeeSchedule")
        if not isinstance(self.instrument_rules, Mapping):
            raise PaperLedgerV2Error("execution cost bundle rules must be a mapping")
        try:
            digest = execution_rule_bundle_sha256(
                self.fee_schedule,
                self.instrument_rules,
            )
        except (TypeError, ValueError) as exc:
            raise PaperLedgerV2Error(
                "execution cost bundle contains invalid canonical rules"
            ) from exc
        rules = dict(sorted(self.instrument_rules.items()))
        object.__setattr__(self, "instrument_rules", MappingProxyType(rules))
        object.__setattr__(self, "execution_rule_bundle_sha256", digest)
        object.__setattr__(
            self,
            "cost_bundle_sha256",
            canonical_sha256(self.to_content_dict()),
        )

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EXECUTION_COST_BUNDLE_SCHEMA_VERSION,
            "fee_schedule": {
                "commission_rate": self.fee_schedule.commission_rate,
                "minimum_commission": self.fee_schedule.minimum_commission,
                "exchange_fee_rate": self.fee_schedule.exchange_fee_rate,
            },
            "whole_lot_policy": "floor_to_instrument_lot.v1",
            "instrument_rules": [
                {
                    "instrument_id": instrument_id,
                    "name": rule.name,
                    "instrument_type": rule.instrument_type,
                    "lot_size": rule.lot_size,
                    "tick_size": rule.tick_size,
                    "sell_stamp_duty_rate": rule.sell_stamp_duty_rate,
                    "t_plus_one": rule.t_plus_one,
                }
                for instrument_id, rule in self.instrument_rules.items()
            ],
            "execution_rule_bundle_sha256": self.execution_rule_bundle_sha256,
            "provenance_authenticated": False,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.to_content_dict(),
            "cost_bundle_sha256": self.cost_bundle_sha256,
        }

    def validate_attempt(self, attempt: "PaperExecutionAttemptV2") -> None:
        if attempt.execution_cost_bundle_sha256 != self.cost_bundle_sha256:
            raise PaperLedgerV2Error("attempt does not bind the execution cost bundle")
        if (
            attempt.commission_rate != self.fee_schedule.commission_rate
            or attempt.minimum_commission != self.fee_schedule.minimum_commission
            or attempt.transfer_fee_rate != self.fee_schedule.exchange_fee_rate
        ):
            raise PaperLedgerV2Error("attempt fee rates differ from execution cost bundle")
        rule = self.instrument_rules.get(attempt.instrument_id)
        if rule is None:
            raise PaperLedgerV2Error("execution cost bundle misses an attempted instrument")
        if attempt.sell_tax_rate != rule.sell_stamp_duty_rate:
            raise PaperLedgerV2Error("attempt sell tax differs from InstrumentRule")
        if attempt.side == "BUY" and attempt.requested_quantity % rule.lot_size:
            raise PaperLedgerV2Error("Paper BUY request violates the bound whole-lot rule")


@dataclass(frozen=True)
class PaperCloseExecutionEvidenceV1:
    """Typed linkage from the frozen signal through manual fill confirmation."""

    signal_id: str
    signal_sha256: str
    consumption_sha256: str
    fill_bundle_sha256: str
    frozen_execution_rule_bundle_sha256: str
    review_execution_rule_bundle_sha256: str
    execution_cost_bundle_sha256: str
    execution_intent_sha256: str
    execution_evidence_bundle_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "signal_id", _text(self.signal_id, "signal_id"))
        for field_name in (
            "signal_sha256",
            "consumption_sha256",
            "fill_bundle_sha256",
            "frozen_execution_rule_bundle_sha256",
            "review_execution_rule_bundle_sha256",
            "execution_cost_bundle_sha256",
            "execution_intent_sha256",
        ):
            object.__setattr__(
                self,
                field_name,
                _hash(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "execution_evidence_bundle_sha256",
            canonical_sha256(self.to_content_dict()),
        )

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PAPER_CLOSE_EXECUTION_EVIDENCE_SCHEMA_VERSION,
            "signal_id": self.signal_id,
            "signal_sha256": self.signal_sha256,
            "consumption_sha256": self.consumption_sha256,
            "fill_bundle_sha256": self.fill_bundle_sha256,
            "frozen_execution_rule_bundle_sha256": (
                self.frozen_execution_rule_bundle_sha256
            ),
            "review_execution_rule_bundle_sha256": (
                self.review_execution_rule_bundle_sha256
            ),
            "execution_cost_bundle_sha256": self.execution_cost_bundle_sha256,
            "execution_intent_sha256": self.execution_intent_sha256,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.to_content_dict(),
            "execution_evidence_bundle_sha256": (
                self.execution_evidence_bundle_sha256
            ),
        }


@dataclass(frozen=True)
class PaperExecutionAttemptV2:
    """One opening-window attempt, including evidenced partial/non-fills."""

    attempt_id: str
    intent_id: str
    intent_sha256: str
    instrument_id: str
    side: str
    status: str
    requested_quantity: int
    filled_quantity: int
    execution_session: date
    attempted_at: datetime
    reference_open: Decimal
    fill_price: Decimal | None
    evidence_sha256: str
    execution_cost_bundle_sha256: str
    commission_rate: Decimal
    minimum_commission: Decimal
    sell_tax_rate: Decimal
    transfer_fee_rate: Decimal
    blocked_reason: str | None = None
    manual_confirmed: bool = True
    auto_submitted: bool = False
    live_order_id: None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "attempt_id", _text(self.attempt_id, "attempt_id"))
        object.__setattr__(self, "intent_id", _text(self.intent_id, "intent_id"))
        object.__setattr__(self, "intent_sha256", _hash(self.intent_sha256, "intent_sha256"))
        object.__setattr__(self, "instrument_id", _instrument(self.instrument_id))
        side = str(self.side).strip().upper()
        status = str(self.status).strip().upper()
        if side not in {"BUY", "SELL"}:
            raise PaperLedgerV2Error("attempt side must be BUY or SELL")
        if status not in _ATTEMPT_STATUSES:
            raise PaperLedgerV2Error("attempt status is invalid")
        if type(self.requested_quantity) is not int or self.requested_quantity <= 0:
            raise PaperLedgerV2Error("requested_quantity must be a positive integer")
        if type(self.filled_quantity) is not int or not 0 <= self.filled_quantity <= self.requested_quantity:
            raise PaperLedgerV2Error("filled_quantity is invalid")
        attempted_at = _aware(self.attempted_at, "attempted_at")
        if attempted_at.astimezone(CHINA_STANDARD_TIME).date() != self.execution_session:
            raise PaperLedgerV2Error("attempt must occur on its execution_session")
        attempted_time = attempted_at.astimezone(CHINA_STANDARD_TIME).time().replace(tzinfo=None)
        if not time(9, 25) <= attempted_time <= time(9, 35):
            raise PaperLedgerV2Error("Paper attempt must reconcile to the opening window")
        reference = _decimal(self.reference_open, "reference_open")
        if reference <= ZERO:
            raise PaperLedgerV2Error("reference_open must be positive")
        blocked = self.blocked_reason
        fill: Decimal | None = None
        if status == "FILLED":
            if self.filled_quantity != self.requested_quantity or blocked is not None:
                raise PaperLedgerV2Error("FILLED attempt must fill the full request without a block")
        elif status == "PARTIAL":
            if not 0 < self.filled_quantity < self.requested_quantity or blocked not in _BLOCKED_EXIT_REASONS:
                raise PaperLedgerV2Error("PARTIAL attempt requires a supported residual block")
        elif self.filled_quantity != 0 or blocked not in _BLOCKED_EXIT_REASONS:
            raise PaperLedgerV2Error("UNFILLED attempt requires a supported block and zero fill")
        if self.filled_quantity:
            fill = _decimal(self.fill_price, "fill_price")
            if fill <= ZERO:
                raise PaperLedgerV2Error("fill_price must be positive")
        elif self.fill_price is not None:
            raise PaperLedgerV2Error("zero-fill attempts cannot carry a fill_price")
        if self.manual_confirmed is not True or self.auto_submitted is not False:
            raise PaperLedgerV2Error("Paper attempts must be manual and never auto-submitted")
        if self.live_order_id is not None:
            raise PaperLedgerV2Error("LIVE order identifiers are forbidden")
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "attempted_at", attempted_at)
        object.__setattr__(self, "reference_open", reference)
        object.__setattr__(self, "fill_price", fill)
        object.__setattr__(self, "evidence_sha256", _hash(self.evidence_sha256, "evidence_sha256"))
        object.__setattr__(
            self,
            "execution_cost_bundle_sha256",
            _hash(
                self.execution_cost_bundle_sha256,
                "execution_cost_bundle_sha256",
            ),
        )
        for field_name in (
            "commission_rate",
            "minimum_commission",
            "sell_tax_rate",
            "transfer_fee_rate",
        ):
            object.__setattr__(
                self,
                field_name,
                _non_negative_decimal(getattr(self, field_name), field_name),
            )

    @property
    def notional(self) -> Decimal:
        if self.filled_quantity == 0 or self.fill_price is None:
            return ZERO.quantize(CENT)
        return _money(self.fill_price * self.filled_quantity, "notional")

    @property
    def commission(self) -> Decimal:
        if not self.filled_quantity:
            return ZERO.quantize(CENT)
        return max(
            self.minimum_commission.quantize(CENT),
            _money(self.notional * self.commission_rate, "commission"),
        )

    @property
    def sell_tax(self) -> Decimal:
        if not self.filled_quantity or self.side != "SELL":
            return ZERO.quantize(CENT)
        return _money(self.notional * self.sell_tax_rate, "sell_tax")

    @property
    def transfer_fee(self) -> Decimal:
        if not self.filled_quantity:
            return ZERO.quantize(CENT)
        return _money(self.notional * self.transfer_fee_rate, "transfer_fee")

    @property
    def slippage_cost(self) -> Decimal:
        if not self.filled_quantity or self.fill_price is None:
            return ZERO.quantize(CENT)
        return _money(
            abs(self.fill_price - self.reference_open) * self.filled_quantity,
            "slippage_cost",
        )

    @property
    def total_cost(self) -> Decimal:
        return _money(
            self.commission + self.sell_tax + self.transfer_fee + self.slippage_cost,
            "attempt total cost",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **{name: getattr(self, name) for name in self.__dataclass_fields__},
            "notional": self.notional,
            "commission": self.commission,
            "sell_tax": self.sell_tax,
            "transfer_fee": self.transfer_fee,
            "slippage_cost": self.slippage_cost,
            "total_cost": self.total_cost,
        }


@dataclass(frozen=True)
class PaperPositionMarkV2:
    """One actual post-fill position marked at the controlled session close."""

    instrument_id: str
    quantity: int
    close_price: Decimal
    price_source_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument_id", _instrument(self.instrument_id))
        if type(self.quantity) is not int or self.quantity <= 0:
            raise PaperLedgerV2Error("position quantity must be a positive integer")
        price = _decimal(self.close_price, "close_price")
        if price <= ZERO:
            raise PaperLedgerV2Error("close_price must be positive")
        object.__setattr__(self, "close_price", price)
        object.__setattr__(
            self,
            "price_source_sha256",
            _hash(self.price_source_sha256, "price_source_sha256"),
        )

    @property
    def market_value(self) -> Decimal:
        return _money(self.close_price * self.quantity, "market_value")

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "quantity": self.quantity,
            "close_price": self.close_price,
            "price_source_sha256": self.price_source_sha256,
            "market_value": self.market_value,
        }


@dataclass(frozen=True)
class ControlledCloseMarkBundleV1:
    """Typed, self-hashed same-session close evidence.

    The receipt digest is an evidence binding, not a claim that the source is
    official.  Formal admission remains outside this accounting module.
    """

    session_date: date
    observed_at: datetime
    available_at: datetime
    source: str
    source_receipt_sha256: str
    positions: tuple[PaperPositionMarkV2, ...]
    mark_bundle_sha256: str = field(init=False)

    @staticmethod
    def price_source_sha256_for(
        *,
        session_date: date,
        observed_at: datetime,
        available_at: datetime,
        source: str,
        source_receipt_sha256: str,
        instrument_id: str,
        close_price: Decimal,
    ) -> str:
        return canonical_sha256(
            {
                "scope": "controlled-close-price-evidence.v1",
                "session_date": session_date,
                "observed_at": observed_at,
                "available_at": available_at,
                "source": source,
                "source_receipt_sha256": source_receipt_sha256,
                "instrument_id": instrument_id,
                "close_price": close_price,
            }
        )

    @classmethod
    def from_close_prices(
        cls,
        *,
        session_date: date,
        observed_at: datetime,
        available_at: datetime,
        source: str,
        source_receipt_sha256: str,
        position_closes: Mapping[str, tuple[int, Decimal]],
    ) -> "ControlledCloseMarkBundleV1":
        marks = tuple(
            PaperPositionMarkV2(
                instrument_id=instrument_id,
                quantity=quantity,
                close_price=close_price,
                price_source_sha256=cls.price_source_sha256_for(
                    session_date=session_date,
                    observed_at=observed_at,
                    available_at=available_at,
                    source=source,
                    source_receipt_sha256=source_receipt_sha256,
                    instrument_id=instrument_id,
                    close_price=close_price,
                ),
            )
            for instrument_id, (quantity, close_price) in sorted(
                position_closes.items()
            )
        )
        return cls(
            session_date=session_date,
            observed_at=observed_at,
            available_at=available_at,
            source=source,
            source_receipt_sha256=source_receipt_sha256,
            positions=marks,
        )

    def __post_init__(self) -> None:
        if not isinstance(self.session_date, date) or isinstance(
            self.session_date, datetime
        ):
            raise PaperLedgerV2Error("close mark session_date must be a date")
        observed = _aware(self.observed_at, "close mark observed_at")
        available = _aware(self.available_at, "close mark available_at")
        local_observed = observed.astimezone(CHINA_STANDARD_TIME)
        local_available = available.astimezone(CHINA_STANDARD_TIME)
        if (
            local_observed.date() != self.session_date
            or local_observed.time().replace(tzinfo=None) < time(15, 0)
        ):
            raise PaperLedgerV2Error(
                "close marks must be observed after the bound session close"
            )
        if available < observed or local_available.date() != self.session_date:
            raise PaperLedgerV2Error(
                "close marks must become available on/after observation in the same session"
            )
        source = _text(self.source, "close mark source")
        receipt = _hash(
            self.source_receipt_sha256,
            "close mark source_receipt_sha256",
        )
        positions = tuple(self.positions)
        if any(not isinstance(item, PaperPositionMarkV2) for item in positions):
            raise PaperLedgerV2Error(
                "close mark bundle positions must contain PaperPositionMarkV2"
            )
        if len({item.instrument_id for item in positions}) != len(positions):
            raise PaperLedgerV2Error("close mark bundle positions must be unique")
        for item in positions:
            expected = self.price_source_sha256_for(
                session_date=self.session_date,
                observed_at=observed,
                available_at=available,
                source=source,
                source_receipt_sha256=receipt,
                instrument_id=item.instrument_id,
                close_price=item.close_price,
            )
            if item.price_source_sha256 != expected:
                raise PaperLedgerV2Error(
                    "position price_source_sha256 does not bind the close receipt"
                )
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "available_at", available)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "source_receipt_sha256", receipt)
        object.__setattr__(
            self,
            "positions",
            tuple(sorted(positions, key=lambda item: item.instrument_id)),
        )
        object.__setattr__(
            self,
            "mark_bundle_sha256",
            canonical_sha256(self.to_content_dict()),
        )

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CLOSE_MARK_BUNDLE_SCHEMA_VERSION,
            "session_date": self.session_date,
            "observed_at": self.observed_at,
            "available_at": self.available_at,
            "source": self.source,
            "source_receipt_sha256": self.source_receipt_sha256,
            "positions": [item.to_dict() for item in self.positions],
            "provenance_authenticated": False,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.to_content_dict(),
            "mark_bundle_sha256": self.mark_bundle_sha256,
        }


@dataclass(frozen=True)
class PaperDailySessionDraftV2:
    trading_date: date
    execution_intent: PortfolioIntent
    closing_intent: PortfolioIntent
    attempts: tuple[PaperExecutionAttemptV2, ...]
    execution_cost_bundle: CanonicalExecutionCostBundleV1
    close_mark_bundle: ControlledCloseMarkBundleV1
    execution_evidence: PaperCloseExecutionEvidenceV1

    def __post_init__(self) -> None:
        if not isinstance(self.trading_date, date) or isinstance(self.trading_date, datetime):
            raise PaperLedgerV2Error("trading_date must be a date")
        if not isinstance(self.execution_intent, PortfolioIntent):
            raise PaperLedgerV2Error("execution_intent must be a PortfolioIntent")
        if not isinstance(self.closing_intent, PortfolioIntent):
            raise PaperLedgerV2Error("closing_intent must be a PortfolioIntent")
        if not isinstance(
            self.execution_cost_bundle,
            CanonicalExecutionCostBundleV1,
        ):
            raise PaperLedgerV2Error(
                "execution_cost_bundle must be CanonicalExecutionCostBundleV1"
            )
        if not isinstance(self.close_mark_bundle, ControlledCloseMarkBundleV1):
            raise PaperLedgerV2Error(
                "close_mark_bundle must be ControlledCloseMarkBundleV1"
            )
        if self.close_mark_bundle.session_date != self.trading_date:
            raise PaperLedgerV2Error("close mark bundle date differs from trading_date")
        if not isinstance(self.execution_evidence, PaperCloseExecutionEvidenceV1):
            raise PaperLedgerV2Error(
                "execution_evidence must be PaperCloseExecutionEvidenceV1"
            )
        if (
            self.execution_evidence.execution_intent_sha256
            != self.execution_intent.intent_sha256
        ):
            raise PaperLedgerV2Error(
                "execution evidence does not bind execution_intent"
            )
        if (
            self.execution_evidence.execution_cost_bundle_sha256
            != self.execution_cost_bundle.cost_bundle_sha256
            or self.execution_evidence.review_execution_rule_bundle_sha256
            != self.execution_cost_bundle.execution_rule_bundle_sha256
        ):
            raise PaperLedgerV2Error(
                "execution evidence does not bind the replayed cost/rule bundle"
            )
        attempts = tuple(self.attempts)
        positions = self.close_mark_bundle.positions
        if any(not isinstance(item, PaperExecutionAttemptV2) for item in attempts):
            raise PaperLedgerV2Error("attempts must contain PaperExecutionAttemptV2 values")
        if any(not isinstance(item, PaperPositionMarkV2) for item in positions):
            raise PaperLedgerV2Error("positions must contain PaperPositionMarkV2 values")
        if len({item.attempt_id for item in attempts}) > 1:
            raise PaperLedgerV2Error("one daily portfolio attempt_id must bind all instrument attempts")
        if len({item.instrument_id for item in attempts}) != len(attempts):
            raise PaperLedgerV2Error("only one attempt per instrument is allowed per session")
        if len({item.instrument_id for item in positions}) != len(positions):
            raise PaperLedgerV2Error("position marks must be unique")
        for attempt in attempts:
            self.execution_cost_bundle.validate_attempt(attempt)
        if attempts and (
            self.execution_evidence.frozen_execution_rule_bundle_sha256
            != self.execution_evidence.review_execution_rule_bundle_sha256
        ):
            raise PaperLedgerV2Error(
                "fills are forbidden after execution-rule bundle drift"
            )
        object.__setattr__(self, "attempts", attempts)

    @property
    def positions(self) -> tuple[PaperPositionMarkV2, ...]:
        return self.close_mark_bundle.positions

    @property
    def mark_bundle_sha256(self) -> str:
        return self.close_mark_bundle.mark_bundle_sha256

    @property
    def execution_evidence_bundle_sha256(self) -> str:
        return self.execution_evidence.execution_evidence_bundle_sha256


@dataclass(frozen=True)
class VerifiedPaperLedgerV2:
    path: Path
    header: Mapping[str, Any]
    daily_sessions: tuple[Mapping[str, Any], ...]
    last_record_sha256: str
    file_sha256: str
    byte_length: int


def _intent_payload(intent: PortfolioIntent) -> dict[str, Any]:
    return intent.to_dict()


def _intent_from_payload(value: Any) -> PortfolioIntent:
    if not isinstance(value, dict):
        raise PaperLedgerV2Error("persisted portfolio intent must be an object")
    expected = {
        "schema_version", "intent_id", "strategy_id", "intent_type",
        "decision_at", "available_at", "frozen_at", "target_gross_exposure",
        "target_weights", "reason_codes", "signal_sha256", "market_data_sha256",
        "model_sha256", "risk_state_sha256", "intent_sha256", "live_supported",
    }
    _keys(value, expected, "portfolio intent")
    if value["schema_version"] != PORTFOLIO_INTENT_SCHEMA_VERSION:
        raise PaperLedgerV2Error("portfolio intent schema version is unsupported")
    if value["live_supported"] is not False:
        raise PaperLedgerV2Error("portfolio intent cannot support LIVE")
    if not isinstance(value["target_weights"], dict) or not isinstance(value["reason_codes"], list):
        raise PaperLedgerV2Error("portfolio intent weights/reasons are malformed")
    try:
        intent = PortfolioIntent(
            intent_id=value["intent_id"],
            strategy_id=value["strategy_id"],
            intent_type=PortfolioIntentType(value["intent_type"]),
            decision_at=_parse_datetime(value["decision_at"], "intent decision_at"),
            available_at=_parse_datetime(value["available_at"], "intent available_at"),
            frozen_at=_parse_datetime(value["frozen_at"], "intent frozen_at"),
            target_gross_exposure=_decimal(value["target_gross_exposure"], "target exposure"),
            target_weights={
                key: _decimal(item, f"target weight {key}")
                for key, item in value["target_weights"].items()
            },
            reason_codes=tuple(value["reason_codes"]),
            signal_sha256=value["signal_sha256"],
            market_data_sha256=value["market_data_sha256"],
            model_sha256=value["model_sha256"],
            risk_state_sha256=value["risk_state_sha256"],
        )
    except (TypeError, ValueError) as exc:
        raise PaperLedgerV2Error(f"persisted portfolio intent is invalid: {exc}") from exc
    if intent.intent_sha256 != value["intent_sha256"]:
        raise PaperLedgerV2Error("portfolio intent SHA-256 mismatch")
    return intent


def _attempt_from_payload(value: Any) -> PaperExecutionAttemptV2:
    if not isinstance(value, dict):
        raise PaperLedgerV2Error("persisted attempt must be an object")
    base = set(PaperExecutionAttemptV2.__dataclass_fields__)
    derived = {
        "notional", "commission", "sell_tax", "transfer_fee",
        "slippage_cost", "total_cost",
    }
    _keys(value, base | derived, "persisted attempt")
    attempt = PaperExecutionAttemptV2(
        attempt_id=value["attempt_id"],
        intent_id=value["intent_id"],
        intent_sha256=value["intent_sha256"],
        instrument_id=value["instrument_id"],
        side=value["side"],
        status=value["status"],
        requested_quantity=value["requested_quantity"],
        filled_quantity=value["filled_quantity"],
        execution_session=_parse_date(value["execution_session"], "execution_session"),
        attempted_at=_parse_datetime(value["attempted_at"], "attempted_at"),
        reference_open=_decimal(value["reference_open"], "reference_open"),
        fill_price=(
            None if value["fill_price"] is None
            else _decimal(value["fill_price"], "fill_price")
        ),
        evidence_sha256=value["evidence_sha256"],
        execution_cost_bundle_sha256=value[
            "execution_cost_bundle_sha256"
        ],
        commission_rate=_decimal(value["commission_rate"], "commission_rate"),
        minimum_commission=_decimal(
            value["minimum_commission"], "minimum_commission"
        ),
        sell_tax_rate=_decimal(value["sell_tax_rate"], "sell_tax_rate"),
        transfer_fee_rate=_decimal(
            value["transfer_fee_rate"], "transfer_fee_rate"
        ),
        blocked_reason=value["blocked_reason"],
        manual_confirmed=value["manual_confirmed"],
        auto_submitted=value["auto_submitted"],
        live_order_id=value["live_order_id"],
    )
    expected = json.loads(canonical_json_bytes(attempt.to_dict()))
    if value != expected:
        raise PaperLedgerV2Error("persisted attempt costs do not reconcile")
    return attempt


def _position_from_payload(value: Any) -> PaperPositionMarkV2:
    if not isinstance(value, dict):
        raise PaperLedgerV2Error("persisted position must be an object")
    _keys(
        value,
        {"instrument_id", "quantity", "close_price", "price_source_sha256", "market_value"},
        "persisted position",
    )
    position = PaperPositionMarkV2(
        value["instrument_id"],
        value["quantity"],
        _decimal(value["close_price"], "close_price"),
        value["price_source_sha256"],
    )
    if value != json.loads(canonical_json_bytes(position.to_dict())):
        raise PaperLedgerV2Error("persisted position market value does not reconcile")
    return position


def _cost_bundle_from_payload(value: Any) -> CanonicalExecutionCostBundleV1:
    if not isinstance(value, dict):
        raise PaperLedgerV2Error("persisted execution cost bundle must be an object")
    _keys(
        value,
        {
            "schema_version",
            "fee_schedule",
            "whole_lot_policy",
            "instrument_rules",
            "execution_rule_bundle_sha256",
            "provenance_authenticated",
            "cost_bundle_sha256",
        },
        "persisted execution cost bundle",
    )
    if (
        value["schema_version"] != EXECUTION_COST_BUNDLE_SCHEMA_VERSION
        or value["whole_lot_policy"] != "floor_to_instrument_lot.v1"
        or value["provenance_authenticated"] is not False
    ):
        raise PaperLedgerV2Error("execution cost bundle contract is unsupported")
    fees = value["fee_schedule"]
    rows = value["instrument_rules"]
    if not isinstance(fees, dict) or not isinstance(rows, list):
        raise PaperLedgerV2Error("execution cost bundle payload is malformed")
    _keys(
        fees,
        {"commission_rate", "minimum_commission", "exchange_fee_rate"},
        "execution cost fee schedule",
    )
    rules: dict[str, InstrumentRule] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise PaperLedgerV2Error("execution cost InstrumentRule must be an object")
        _keys(
            row,
            {
                "instrument_id",
                "name",
                "instrument_type",
                "lot_size",
                "tick_size",
                "sell_stamp_duty_rate",
                "t_plus_one",
            },
            "execution cost InstrumentRule",
        )
        instrument_id = _instrument(row["instrument_id"])
        if instrument_id in rules:
            raise PaperLedgerV2Error("execution cost InstrumentRules must be unique")
        try:
            rules[instrument_id] = InstrumentRule(
                instrument_id=instrument_id,
                name=str(row["name"]),
                instrument_type=str(row["instrument_type"]),
                lot_size=row["lot_size"],
                tick_size=_decimal(row["tick_size"], "InstrumentRule.tick_size"),
                sell_stamp_duty_rate=_non_negative_decimal(
                    row["sell_stamp_duty_rate"],
                    "InstrumentRule.sell_stamp_duty_rate",
                ),
                t_plus_one=row["t_plus_one"],
            )
        except (TypeError, ValueError) as exc:
            raise PaperLedgerV2Error("execution cost InstrumentRule is invalid") from exc
    try:
        bundle = CanonicalExecutionCostBundleV1(
            fee_schedule=FeeSchedule(
                commission_rate=_non_negative_decimal(
                    fees["commission_rate"], "commission_rate"
                ),
                minimum_commission=_non_negative_decimal(
                    fees["minimum_commission"], "minimum_commission"
                ),
                exchange_fee_rate=_non_negative_decimal(
                    fees["exchange_fee_rate"], "exchange_fee_rate"
                ),
            ),
            instrument_rules=rules,
        )
    except (TypeError, ValueError) as exc:
        raise PaperLedgerV2Error("execution cost bundle is invalid") from exc
    expected = json.loads(canonical_json_bytes(bundle.to_dict()))
    if value != expected:
        raise PaperLedgerV2Error("execution cost bundle SHA-256/content mismatch")
    return bundle


def _close_mark_bundle_from_payload(value: Any) -> ControlledCloseMarkBundleV1:
    if not isinstance(value, dict):
        raise PaperLedgerV2Error("persisted close mark bundle must be an object")
    _keys(
        value,
        {
            "schema_version",
            "session_date",
            "observed_at",
            "available_at",
            "source",
            "source_receipt_sha256",
            "positions",
            "provenance_authenticated",
            "mark_bundle_sha256",
        },
        "persisted close mark bundle",
    )
    if (
        value["schema_version"] != CLOSE_MARK_BUNDLE_SCHEMA_VERSION
        or value["provenance_authenticated"] is not False
        or not isinstance(value["positions"], list)
    ):
        raise PaperLedgerV2Error("close mark bundle contract is unsupported")
    bundle = ControlledCloseMarkBundleV1(
        session_date=_parse_date(value["session_date"], "close session_date"),
        observed_at=_parse_datetime(value["observed_at"], "close observed_at"),
        available_at=_parse_datetime(value["available_at"], "close available_at"),
        source=value["source"],
        source_receipt_sha256=value["source_receipt_sha256"],
        positions=tuple(
            _position_from_payload(item) for item in value["positions"]
        ),
    )
    expected = json.loads(canonical_json_bytes(bundle.to_dict()))
    if value != expected:
        raise PaperLedgerV2Error("close mark bundle SHA-256/content mismatch")
    return bundle


def _execution_evidence_from_payload(value: Any) -> PaperCloseExecutionEvidenceV1:
    if not isinstance(value, dict):
        raise PaperLedgerV2Error("persisted execution evidence must be an object")
    expected_keys = {
        "schema_version",
        "signal_id",
        "signal_sha256",
        "consumption_sha256",
        "fill_bundle_sha256",
        "frozen_execution_rule_bundle_sha256",
        "review_execution_rule_bundle_sha256",
        "execution_cost_bundle_sha256",
        "execution_intent_sha256",
        "execution_evidence_bundle_sha256",
    }
    _keys(value, expected_keys, "persisted execution evidence")
    if value["schema_version"] != PAPER_CLOSE_EXECUTION_EVIDENCE_SCHEMA_VERSION:
        raise PaperLedgerV2Error("execution evidence contract is unsupported")
    evidence = PaperCloseExecutionEvidenceV1(
        signal_id=value["signal_id"],
        signal_sha256=value["signal_sha256"],
        consumption_sha256=value["consumption_sha256"],
        fill_bundle_sha256=value["fill_bundle_sha256"],
        frozen_execution_rule_bundle_sha256=value[
            "frozen_execution_rule_bundle_sha256"
        ],
        review_execution_rule_bundle_sha256=value[
            "review_execution_rule_bundle_sha256"
        ],
        execution_cost_bundle_sha256=value["execution_cost_bundle_sha256"],
        execution_intent_sha256=value["execution_intent_sha256"],
    )
    if value != json.loads(canonical_json_bytes(evidence.to_dict())):
        raise PaperLedgerV2Error("execution evidence SHA-256/content mismatch")
    return evidence


def _header_content(
    *,
    strategy_id: str,
    policy_schema_version: str,
    policy_sha256: str,
    calendar: Sequence[date],
    initial_cash: Decimal,
    created_at: datetime,
) -> dict[str, Any]:
    return {
        "schema_version": PAPER_LEDGER_V2_VERSION,
        "producer": PAPER_LEDGER_V2_PRODUCER,
        "status": PAPER_LEDGER_V2_STATUS,
        "created_at": created_at,
        "strategy_id": strategy_id,
        "policy_schema_version": policy_schema_version,
        "policy_sha256": policy_sha256,
        "portfolio_intent_schema_version": PORTFOLIO_INTENT_SCHEMA_VERSION,
        "execution_cost_bundle_schema_version": (
            EXECUTION_COST_BUNDLE_SCHEMA_VERSION
        ),
        "close_mark_bundle_schema_version": CLOSE_MARK_BUNDLE_SCHEMA_VERSION,
        "execution_evidence_schema_version": (
            PAPER_CLOSE_EXECUTION_EVIDENCE_SCHEMA_VERSION
        ),
        "controlled_trading_dates": tuple(calendar),
        "controlled_calendar_sha256": canonical_sha256(tuple(calendar)),
        "initial_cash": initial_cash,
        "drawdown_trigger": DRAWDOWN_TRIGGER,
        "exposure_definition": {
            "target_gross_exposure": "closing_intent_requested_target",
            "feasible_gross_exposure": "closing_intent_feasible_weight_sum",
            "realized_gross_exposure": "actual_post_fill_close_mark",
            "fields_are_semantically_distinct": True,
        },
        "manual_execution_required": True,
        "auto_submit": False,
        "live_supported": False,
        "execution_authority": "none",
    }


def _header_from_record(record: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    _keys(record, {"record_type", "content", "record_sha256"}, "header record")
    if record["record_type"] != "header" or not isinstance(record["content"], dict):
        raise PaperLedgerV2Error("first ledger record must be a header")
    content = record["content"]
    expected_keys = {
        "schema_version", "producer", "status", "created_at", "strategy_id",
        "policy_schema_version", "policy_sha256", "portfolio_intent_schema_version",
        "execution_cost_bundle_schema_version",
        "close_mark_bundle_schema_version",
        "execution_evidence_schema_version",
        "controlled_trading_dates", "controlled_calendar_sha256", "initial_cash",
        "drawdown_trigger", "exposure_definition", "manual_execution_required",
        "auto_submit", "live_supported", "execution_authority",
    }
    _keys(content, expected_keys, "header content")
    if (
        content["schema_version"] != PAPER_LEDGER_V2_VERSION
        or content["producer"] != PAPER_LEDGER_V2_PRODUCER
        or content["status"] != PAPER_LEDGER_V2_STATUS
        or content["strategy_id"] != ADAPTIVE_STRATEGY_ID
        or content["policy_schema_version"] != ADAPTIVE_POLICY_SCHEMA_VERSION
        or content["portfolio_intent_schema_version"] != PORTFOLIO_INTENT_SCHEMA_VERSION
        or content["execution_cost_bundle_schema_version"]
        != EXECUTION_COST_BUNDLE_SCHEMA_VERSION
        or content["close_mark_bundle_schema_version"]
        != CLOSE_MARK_BUNDLE_SCHEMA_VERSION
        or content["execution_evidence_schema_version"]
        != PAPER_CLOSE_EXECUTION_EVIDENCE_SCHEMA_VERSION
    ):
        raise PaperLedgerV2Error("Paper ledger V2 header contract is unsupported")
    if (
        content["manual_execution_required"] is not True
        or content["auto_submit"] is not False
        or content["live_supported"] is not False
        or content["execution_authority"] != "none"
    ):
        raise PaperLedgerV2Error("Paper ledger V2 safety boundary was altered")
    _hash(content["policy_sha256"], "policy_sha256")
    if content["policy_sha256"] != FROZEN_ADAPTIVE_EXPOSURE_POLICY_SHA256:
        raise PaperLedgerV2Error("Paper ledger V2 policy SHA-256 is not the frozen repository policy")
    _hash(content["controlled_calendar_sha256"], "controlled_calendar_sha256")
    _parse_datetime(content["created_at"], "created_at")
    calendar_raw = content["controlled_trading_dates"]
    if not isinstance(calendar_raw, list):
        raise PaperLedgerV2Error("controlled_trading_dates must be an array")
    calendar = tuple(_parse_date(item, "controlled trading date") for item in calendar_raw)
    if not calendar or tuple(sorted(calendar)) != calendar or len(set(calendar)) != len(calendar):
        raise PaperLedgerV2Error("controlled trading calendar is invalid")
    if canonical_sha256(calendar) != content["controlled_calendar_sha256"]:
        raise PaperLedgerV2Error("controlled trading calendar SHA-256 mismatch")
    if _money(content["initial_cash"], "initial_cash") != Decimal("10000.00"):
        raise PaperLedgerV2Error("Paper V2 initial cash is frozen to CNY 10,000")
    if _decimal(content["drawdown_trigger"], "drawdown_trigger") != DRAWDOWN_TRIGGER:
        raise PaperLedgerV2Error("drawdown trigger must remain 12%")
    expected_exposure = {
        "target_gross_exposure": "closing_intent_requested_target",
        "feasible_gross_exposure": "closing_intent_feasible_weight_sum",
        "realized_gross_exposure": "actual_post_fill_close_mark",
        "fields_are_semantically_distinct": True,
    }
    if content["exposure_definition"] != expected_exposure:
        raise PaperLedgerV2Error("exposure definitions were altered")
    expected_hash = canonical_sha256({"record_type": "header", "content": content})
    if record["record_sha256"] != expected_hash:
        raise PaperLedgerV2Error("Paper ledger V2 header SHA-256 mismatch")
    return content, expected_hash


def _blocked_exit_payload(
    attempt: PaperExecutionAttemptV2,
    residual_quantity: int,
) -> dict[str, Any]:
    return {
        "attempt_id": attempt.attempt_id,
        "instrument_id": attempt.instrument_id,
        "reason": attempt.blocked_reason,
        "residual_quantity": residual_quantity,
    }


def _derive_daily_content(
    draft: PaperDailySessionDraftV2,
    *,
    header: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
    recorded_at: datetime,
    prior_attempt_ids: set[str],
) -> dict[str, Any]:
    calendar = tuple(_parse_date(item, "controlled trading date") for item in header["controlled_trading_dates"])
    expected_index = 0 if previous is None else int(previous["calendar_index"]) + 1
    if expected_index >= len(calendar) or draft.trading_date != calendar[expected_index]:
        raise PaperLedgerV2Error("daily_session must be the next controlled trading date")
    local_recorded = recorded_at.astimezone(CHINA_STANDARD_TIME)
    if local_recorded.date() != draft.trading_date or local_recorded.time().replace(tzinfo=None) < time(15, 0):
        raise PaperLedgerV2Error("daily_session must be appended after its same-session close without backfill")
    if draft.close_mark_bundle.available_at > recorded_at:
        raise PaperLedgerV2Error("close mark bundle was unavailable when the ledger was recorded")
    if previous is None:
        previous_recorded = _parse_datetime(header["created_at"], "header created_at")
    else:
        previous_recorded = _parse_datetime(previous["recorded_at"], "previous recorded_at")
    if recorded_at <= previous_recorded:
        raise PaperLedgerV2Error("daily_session records must be appended chronologically")

    execution_intent = draft.execution_intent
    closing_intent = draft.closing_intent
    for label, intent in (
        ("execution_intent", execution_intent),
        ("closing_intent", closing_intent),
    ):
        if intent.strategy_id != header["strategy_id"]:
            raise PaperLedgerV2Error(f"{label} strategy_id differs from the ledger header")
        if intent.decision_at > recorded_at:
            raise PaperLedgerV2Error(f"{label} cannot be from the future")
        if intent.decision_at.astimezone(CHINA_STANDARD_TIME).date() > draft.trading_date:
            raise PaperLedgerV2Error(f"{label} decision date is after the daily session")
    if (
        execution_intent.intent_id == closing_intent.intent_id
        and execution_intent.intent_sha256 != closing_intent.intent_sha256
    ):
        raise PaperLedgerV2Error("one intent_id cannot identify different execution and closing payloads")
    same_intent = (
        execution_intent.intent_id == closing_intent.intent_id
        and execution_intent.intent_sha256 == closing_intent.intent_sha256
    )
    previous_closing_intent = (
        _intent_from_payload(previous["closing_intent"])
        if previous is not None
        else None
    )
    if previous_closing_intent is not None and (
        execution_intent.intent_id != previous_closing_intent.intent_id
        or execution_intent.intent_sha256 != previous_closing_intent.intent_sha256
    ):
        raise PaperLedgerV2Error("execution_intent must continue the previous closing_intent")
    if not same_intent:
        local_closing_decision = closing_intent.decision_at.astimezone(CHINA_STANDARD_TIME)
        if (
            local_closing_decision.date() != draft.trading_date
            or local_closing_decision.time().replace(tzinfo=None) < time(15, 0)
            or closing_intent.decision_at < execution_intent.decision_at
        ):
            raise PaperLedgerV2Error(
                "a changed closing_intent must be formed at or after the same-session close"
            )

    attempts = tuple(draft.attempts)
    replayed = {item.attempt_id for item in attempts} & prior_attempt_ids
    if replayed:
        raise PaperLedgerV2Error(f"attempt replay is forbidden: {sorted(replayed)}")
    if any(item.execution_session != draft.trading_date for item in attempts):
        raise PaperLedgerV2Error("all attempts must use the current controlled session")
    if any(item.attempted_at > recorded_at for item in attempts):
        raise PaperLedgerV2Error("attempt evidence cannot be from the future")
    for attempt in attempts:
        if attempt.intent_id != execution_intent.intent_id:
            raise PaperLedgerV2Error("attempt intent_id differs from execution_intent")
        if attempt.intent_sha256 != execution_intent.intent_sha256:
            raise PaperLedgerV2Error("attempt intent SHA-256 differs from execution_intent")
        if attempt.attempted_at <= execution_intent.decision_at:
            raise PaperLedgerV2Error("attempt predates the intent it claims to execute")

    previous_positions = {
        item["instrument_id"]: int(item["quantity"])
        for item in (previous["positions"] if previous is not None else [])
    }
    quantities = dict(previous_positions)
    cash = (
        _money(previous["cash"], "previous cash")
        if previous is not None
        else _money(header["initial_cash"], "initial cash")
    )
    session_cost = ZERO
    for attempt in attempts:
        current_quantity = quantities.get(attempt.instrument_id, 0)
        fees = attempt.commission + attempt.sell_tax + attempt.transfer_fee
        if attempt.side == "BUY":
            quantities[attempt.instrument_id] = current_quantity + attempt.filled_quantity
            cash -= attempt.notional + fees
        else:
            if attempt.requested_quantity > current_quantity or attempt.filled_quantity > current_quantity:
                raise PaperLedgerV2Error("SELL attempt exceeds the actual preceding position")
            remaining = current_quantity - attempt.filled_quantity
            if remaining:
                quantities[attempt.instrument_id] = remaining
            else:
                quantities.pop(attempt.instrument_id, None)
            cash += attempt.notional - fees
        session_cost += attempt.total_cost
    cash = _money(cash, "recomputed cash")
    if cash < ZERO:
        raise PaperLedgerV2Error("Paper V2 cannot use leverage or negative cash")

    marked = {item.instrument_id: item for item in draft.positions}
    marked_quantities = {key: item.quantity for key, item in marked.items()}
    if marked_quantities != quantities:
        raise PaperLedgerV2Error("position marks do not reconcile to prior positions plus real fills")
    positions_value = sum((item.market_value for item in marked.values()), ZERO)
    nav = _money(cash + positions_value, "strategy_nav")
    if nav <= ZERO:
        raise PaperLedgerV2Error("strategy NAV must remain positive")
    previous_peak = (
        _money(previous["peak_nav"], "previous peak_nav")
        if previous is not None
        else _money(header["initial_cash"], "initial cash")
    )
    peak_nav = max(previous_peak, nav)
    drawdown = ((peak_nav - nav) / peak_nav).quantize(PCT)
    previous_latched = bool(previous["risk_latched"]) if previous is not None else False
    # The latch is terminal for this ledger's lifetime.  Becoming flat only
    # clears ``exit_pending``; it cannot silently restore buy authority.  A
    # reset/new-ledger workflow is deliberately outside this P0 runtime.
    risk_latched = previous_latched or drawdown >= DRAWDOWN_TRIGGER
    triggered_now = risk_latched and not previous_latched
    previous_trigger_date = previous["risk_trigger_date"] if previous is not None else None
    risk_trigger_date = draft.trading_date if triggered_now else previous_trigger_date

    if triggered_now:
        local_decision = closing_intent.decision_at.astimezone(CHINA_STANDARD_TIME)
        if (
            closing_intent.intent_type is not PortfolioIntentType.ACCOUNT_DRAWDOWN_EXIT
            or closing_intent.target_gross_exposure != ZERO
            or local_decision.date() != draft.trading_date
            or local_decision.time().replace(tzinfo=None) < time(15, 0)
        ):
            raise PaperLedgerV2Error(
                "a 12% close drawdown must create a same-session ACCOUNT_DRAWDOWN_EXIT intent"
            )
    elif closing_intent.intent_type is PortfolioIntentType.ACCOUNT_DRAWDOWN_EXIT and not previous_latched:
        raise PaperLedgerV2Error("ACCOUNT_DRAWDOWN_EXIT requires the 12% drawdown latch")
    if not previous_latched and execution_intent.intent_type is PortfolioIntentType.ACCOUNT_DRAWDOWN_EXIT:
        raise PaperLedgerV2Error("execution_intent cannot predate the 12% drawdown latch")

    if previous_latched:
        if any(item.side == "BUY" for item in attempts):
            raise PaperLedgerV2Error("BUY attempts are forbidden after risk_latched")
        if (
            closing_intent.intent_id != execution_intent.intent_id
            or closing_intent.intent_sha256 != execution_intent.intent_sha256
        ):
            raise PaperLedgerV2Error("risk-latched closing_intent must remain the execution exit intent")
        if execution_intent.target_gross_exposure != ZERO or closing_intent.target_gross_exposure != ZERO:
            raise PaperLedgerV2Error("risk_latched requires zero target gross exposure")
        if (
            execution_intent.intent_type is not PortfolioIntentType.ACCOUNT_DRAWDOWN_EXIT
            or closing_intent.intent_type is not PortfolioIntentType.ACCOUNT_DRAWDOWN_EXIT
        ):
            raise PaperLedgerV2Error("risk_latched requires ACCOUNT_DRAWDOWN_EXIT intents")
        if previous_positions:
            by_instrument = {item.instrument_id: item for item in attempts}
            if set(by_instrument) != set(previous_positions):
                raise PaperLedgerV2Error("every residual position requires exactly one daily exit retry")
            for instrument_id, previous_quantity in previous_positions.items():
                attempt = by_instrument[instrument_id]
                if (
                    attempt.side != "SELL"
                    or attempt.requested_quantity != previous_quantity
                    or attempt.intent_id != execution_intent.intent_id
                    or attempt.intent_sha256 != execution_intent.intent_sha256
                ):
                    raise PaperLedgerV2Error("daily forced-exit retry does not bind the full residual position")
        elif attempts:
            raise PaperLedgerV2Error("a flat risk-latched account cannot create further attempts")

    blocked_exits: list[dict[str, Any]] = []
    for attempt in attempts:
        if attempt.side != "SELL" or attempt.blocked_reason is None:
            continue
        residual = quantities.get(attempt.instrument_id, 0)
        if residual <= 0:
            raise PaperLedgerV2Error("a blocked exit must leave a real residual position")
        blocked_exits.append(_blocked_exit_payload(attempt, residual))
    if previous_latched and quantities:
        blocked_ids = {item["instrument_id"] for item in blocked_exits}
        if blocked_ids != set(quantities):
            raise PaperLedgerV2Error("every residual forced-exit position needs a blocked_exit_reason")

    realized = (positions_value / nav).quantize(PCT)
    feasible = sum(closing_intent.target_weights.values(), ZERO).quantize(PCT)
    if not ZERO <= feasible <= closing_intent.target_gross_exposure:
        raise PaperLedgerV2Error(
            "closing intent feasible weights exceed the requested target exposure"
        )
    if not ZERO <= realized <= Decimal("1"):
        raise PaperLedgerV2Error("realized gross exposure must remain between zero and one")
    if quantities and realized == ZERO:
        raise PaperLedgerV2Error("non-empty positions cannot claim zero realized exposure")
    cumulative_cost = _money(
        (
            _money(
                previous["cumulative_transaction_cost"],
                "previous cumulative transaction cost",
            )
            if previous is not None
            else ZERO
        )
        + session_cost,
        "cumulative transaction cost",
    )
    content = {
        "trading_date": draft.trading_date,
        "calendar_index": expected_index,
        "recorded_at": recorded_at,
        "policy_sha256": header["policy_sha256"],
        "controlled_calendar_sha256": header["controlled_calendar_sha256"],
        "mark_bundle_sha256": draft.mark_bundle_sha256,
        "execution_cost_bundle": draft.execution_cost_bundle.to_dict(),
        "close_mark_bundle": draft.close_mark_bundle.to_dict(),
        "execution_evidence": draft.execution_evidence.to_dict(),
        "execution_evidence_bundle_sha256": (
            draft.execution_evidence_bundle_sha256
        ),
        "execution_intent": _intent_payload(execution_intent),
        "closing_intent": _intent_payload(closing_intent),
        "attempts": [item.to_dict() for item in attempts],
        "positions": [item.to_dict() for item in sorted(marked.values(), key=lambda value: value.instrument_id)],
        "cash": cash,
        "strategy_positions_value": _money(positions_value, "positions value"),
        "strategy_nav": nav,
        "peak_nav": peak_nav,
        "drawdown": drawdown,
        "target_gross_exposure": closing_intent.target_gross_exposure.quantize(PCT),
        "feasible_gross_exposure": feasible,
        "realized_gross_exposure": realized,
        "blocked_exit_reasons": sorted(
            blocked_exits,
            key=lambda item: (item["instrument_id"], item["attempt_id"]),
        ),
        "session_transaction_cost": _money(session_cost, "session transaction cost"),
        "cumulative_transaction_cost": cumulative_cost,
        "risk_latched": risk_latched,
        "risk_trigger_date": risk_trigger_date,
        "exit_pending": risk_latched and bool(quantities),
        "manual_execution_required": True,
        "auto_submit": False,
        "live_supported": False,
        "execution_authority": "none",
    }
    return json.loads(canonical_json_bytes(content))


def _daily_draft_from_content(content: Mapping[str, Any]) -> PaperDailySessionDraftV2:
    expected = {
        "trading_date", "calendar_index", "recorded_at", "policy_sha256",
        "controlled_calendar_sha256", "mark_bundle_sha256",
        "execution_cost_bundle", "close_mark_bundle",
        "execution_evidence",
        "execution_evidence_bundle_sha256", "execution_intent",
        "closing_intent",
        "attempts", "positions", "cash", "strategy_positions_value", "strategy_nav",
        "peak_nav", "drawdown", "target_gross_exposure", "feasible_gross_exposure",
        "realized_gross_exposure", "blocked_exit_reasons", "session_transaction_cost",
        "cumulative_transaction_cost", "risk_latched", "risk_trigger_date", "exit_pending",
        "manual_execution_required", "auto_submit", "live_supported", "execution_authority",
    }
    _keys(content, expected, "daily_session content")
    if not isinstance(content["attempts"], list) or not isinstance(content["positions"], list):
        raise PaperLedgerV2Error("daily_session attempts and positions must be arrays")
    if not isinstance(content["blocked_exit_reasons"], list):
        raise PaperLedgerV2Error("blocked_exit_reasons must be an array")
    cost_bundle = _cost_bundle_from_payload(content["execution_cost_bundle"])
    close_bundle = _close_mark_bundle_from_payload(content["close_mark_bundle"])
    execution_evidence = _execution_evidence_from_payload(
        content["execution_evidence"]
    )
    if content["mark_bundle_sha256"] != close_bundle.mark_bundle_sha256:
        raise PaperLedgerV2Error("daily mark bundle summary hash differs from payload")
    if content["positions"] != json.loads(
        canonical_json_bytes([item.to_dict() for item in close_bundle.positions])
    ):
        raise PaperLedgerV2Error("daily position summary differs from close mark bundle")
    if (
        content["execution_evidence_bundle_sha256"]
        != execution_evidence.execution_evidence_bundle_sha256
    ):
        raise PaperLedgerV2Error(
            "daily execution evidence summary hash differs from payload"
        )
    return PaperDailySessionDraftV2(
        trading_date=_parse_date(content["trading_date"], "trading_date"),
        execution_intent=_intent_from_payload(content["execution_intent"]),
        closing_intent=_intent_from_payload(content["closing_intent"]),
        attempts=tuple(_attempt_from_payload(item) for item in content["attempts"]),
        execution_cost_bundle=cost_bundle,
        close_mark_bundle=close_bundle,
        execution_evidence=execution_evidence,
    )


def _write_line(
    path: Path,
    record: Mapping[str, Any],
    *,
    exclusive: bool = False,
    expected_size: int | None = None,
) -> None:
    data = canonical_json_bytes(record) + b"\n"
    flags = os.O_WRONLY | getattr(os, "O_BINARY", 0)
    flags |= os.O_CREAT | os.O_EXCL if exclusive else os.O_APPEND
    if not exclusive and expected_size is not None and path.stat().st_size != expected_size:
        raise PaperLedgerV2Error("ledger changed concurrently before append")
    try:
        descriptor = os.open(str(path), flags, 0o600)
    except FileExistsError as exc:
        raise PaperLedgerV2Error("Paper ledger V2 already exists") from exc
    try:
        written = os.write(descriptor, data)
        if written != len(data):
            raise PaperLedgerV2Error("short write while appending Paper ledger V2")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def create_or_verify_paper_ledger_v2(
    path: str | Path,
    *,
    strategy_id: str,
    policy_schema_version: str,
    policy_sha256: str,
    controlled_trading_dates: Sequence[date],
    initial_cash: Decimal = Decimal("10000"),
) -> VerifiedPaperLedgerV2:
    """Create the V2 header once, or verify an exact existing binding."""

    if strategy_id != ADAPTIVE_STRATEGY_ID:
        raise PaperLedgerV2Error(f"strategy_id must remain {ADAPTIVE_STRATEGY_ID}")
    if policy_schema_version != ADAPTIVE_POLICY_SCHEMA_VERSION:
        raise PaperLedgerV2Error(
            f"policy_schema_version must remain {ADAPTIVE_POLICY_SCHEMA_VERSION}"
        )
    policy_hash = _hash(policy_sha256, "policy_sha256")
    if policy_hash != FROZEN_ADAPTIVE_EXPOSURE_POLICY_SHA256:
        raise PaperLedgerV2Error("policy_sha256 must match the frozen repository policy")
    calendar = tuple(controlled_trading_dates)
    if (
        not calendar
        or any(not isinstance(item, date) or isinstance(item, datetime) for item in calendar)
        or tuple(sorted(calendar)) != calendar
        or len(set(calendar)) != len(calendar)
    ):
        raise PaperLedgerV2Error("controlled trading calendar must be chronological and unique")
    cash = _money(initial_cash, "initial_cash")
    if cash != Decimal("10000.00"):
        raise PaperLedgerV2Error("Paper V2 initial cash is frozen to CNY 10,000")
    target = Path(path)
    if target.exists():
        verified = verify_paper_ledger_v2(target)
        expected = _header_content(
            strategy_id=strategy_id,
            policy_schema_version=policy_schema_version,
            policy_sha256=policy_hash,
            calendar=calendar,
            initial_cash=cash,
            created_at=_parse_datetime(verified.header["created_at"], "created_at"),
        )
        if dict(verified.header) != json.loads(canonical_json_bytes(expected)):
            raise PaperLedgerV2Error("existing Paper ledger V2 header differs from the requested contract")
        return verified
    if not target.parent.exists() or not target.parent.is_dir():
        raise PaperLedgerV2Error("Paper ledger V2 parent directory must already exist")
    content = _header_content(
        strategy_id=strategy_id,
        policy_schema_version=policy_schema_version,
        policy_sha256=policy_hash,
        calendar=calendar,
        initial_cash=cash,
        created_at=_now(),
    )
    record = {
        "record_type": "header",
        "content": content,
    }
    record["record_sha256"] = canonical_sha256(record)
    _write_line(target, record, exclusive=True)
    return verify_paper_ledger_v2(target)


def verify_paper_ledger_v2(
    path: str | Path,
    *,
    as_of: datetime | None = None,
) -> VerifiedPaperLedgerV2:
    """Recompute the full hash chain and every daily accounting transition."""

    target = Path(path)
    if not target.is_file() or target.is_symlink():
        raise PaperLedgerV2Error("Paper ledger V2 must be a regular non-symlink file")
    raw = target.read_bytes()
    if not raw or not raw.endswith(b"\n"):
        raise PaperLedgerV2Error("Paper ledger V2 must end with one complete newline record")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise PaperLedgerV2Error("Paper ledger V2 must be UTF-8") from exc
    if any(not line for line in lines):
        raise PaperLedgerV2Error("Paper ledger V2 cannot contain blank records")
    records = tuple(_strict_object(line) for line in lines)
    header, previous_hash = _header_from_record(records[0])
    effective_as_of = _aware(as_of, "as_of") if as_of is not None else _now()
    if _parse_datetime(header["created_at"], "created_at") > effective_as_of:
        raise PaperLedgerV2Error("Paper ledger V2 header is from the future")
    sessions: list[Mapping[str, Any]] = []
    attempt_ids: set[str] = set()
    previous: Mapping[str, Any] | None = None
    for index, record in enumerate(records[1:], 1):
        _keys(
            record,
            {"record_type", "previous_record_sha256", "content", "record_sha256"},
            f"record {index}",
        )
        if record["record_type"] != "daily_session" or not isinstance(record["content"], dict):
            raise PaperLedgerV2Error("Paper ledger V2 accepts only daily_session records after its header")
        if record["previous_record_sha256"] != previous_hash:
            raise PaperLedgerV2Error("Paper ledger V2 hash chain is broken")
        expected_hash = canonical_sha256(
            {
                "record_type": "daily_session",
                "previous_record_sha256": previous_hash,
                "content": record["content"],
            }
        )
        if record["record_sha256"] != expected_hash:
            raise PaperLedgerV2Error("Paper ledger V2 record SHA-256 mismatch")
        recorded_at = _parse_datetime(record["content"].get("recorded_at"), "recorded_at")
        if recorded_at > effective_as_of:
            raise PaperLedgerV2Error("Paper ledger V2 contains a future daily session")
        draft = _daily_draft_from_content(record["content"])
        derived = _derive_daily_content(
            draft,
            header=header,
            previous=previous,
            recorded_at=recorded_at,
            prior_attempt_ids=set(attempt_ids),
        )
        if record["content"] != derived:
            raise PaperLedgerV2Error("persisted daily_session does not match semantic replay")
        attempt_ids.update(item.attempt_id for item in draft.attempts)
        sessions.append(record["content"])
        previous = record["content"]
        previous_hash = expected_hash
    return VerifiedPaperLedgerV2(
        path=target,
        header=header,
        daily_sessions=tuple(sessions),
        last_record_sha256=previous_hash,
        file_sha256=sha256(raw).hexdigest(),
        byte_length=len(raw),
    )


def append_paper_daily_session_v2(
    path: str | Path,
    draft: PaperDailySessionDraftV2,
    *,
    expected_previous_sha256: str | None = None,
) -> VerifiedPaperLedgerV2:
    """Append exactly one same-day close record; replays are rejected."""

    if not isinstance(draft, PaperDailySessionDraftV2):
        raise PaperLedgerV2Error("draft must be a PaperDailySessionDraftV2")
    recorded_at = _now()
    current = verify_paper_ledger_v2(path, as_of=recorded_at)
    if expected_previous_sha256 is not None and (
        _hash(expected_previous_sha256, "expected_previous_sha256")
        != current.last_record_sha256
    ):
        raise PaperLedgerV2Error("stale Paper ledger V2 append cursor")
    prior_attempt_ids = {
        item["attempt_id"]
        for session in current.daily_sessions
        for item in session["attempts"]
    }
    previous = current.daily_sessions[-1] if current.daily_sessions else None
    content = _derive_daily_content(
        draft,
        header=current.header,
        previous=previous,
        recorded_at=recorded_at,
        prior_attempt_ids=prior_attempt_ids,
    )
    record = {
        "record_type": "daily_session",
        "previous_record_sha256": current.last_record_sha256,
        "content": content,
    }
    record["record_sha256"] = canonical_sha256(record)
    _write_line(
        current.path,
        record,
        expected_size=current.byte_length,
    )
    return verify_paper_ledger_v2(current.path, as_of=recorded_at)


__all__ = [
    "ADAPTIVE_POLICY_SCHEMA_VERSION",
    "ADAPTIVE_STRATEGY_ID",
    "CLOSE_MARK_BUNDLE_SCHEMA_VERSION",
    "CanonicalExecutionCostBundleV1",
    "ControlledCloseMarkBundleV1",
    "EXECUTION_COST_BUNDLE_SCHEMA_VERSION",
    "PAPER_LEDGER_V2_PRODUCER",
    "PAPER_LEDGER_V2_STATUS",
    "PAPER_LEDGER_V2_VERSION",
    "PAPER_CLOSE_EXECUTION_EVIDENCE_SCHEMA_VERSION",
    "PaperCloseExecutionEvidenceV1",
    "PaperDailySessionDraftV2",
    "PaperExecutionAttemptV2",
    "PaperLedgerV2Error",
    "PaperPositionMarkV2",
    "VerifiedPaperLedgerV2",
    "append_paper_daily_session_v2",
    "create_or_verify_paper_ledger_v2",
    "verify_paper_ledger_v2",
]
