"""Immutable domain models for the trading execution layer."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Mapping


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class PortfolioIntentType(str, Enum):
    ALPHA_REBALANCE = "ALPHA_REBALANCE"
    NO_ALPHA_CASH = "NO_ALPHA_CASH"
    DEFENSIVE_REDUCTION = "DEFENSIVE_REDUCTION"
    RISK_OFF = "RISK_OFF"
    ACCOUNT_DRAWDOWN_EXIT = "ACCOUNT_DRAWDOWN_EXIT"
    DATA_FAIL_CLOSED = "DATA_FAIL_CLOSED"
    MANUAL_PAUSE = "MANUAL_PAUSE"


class OrderRiskDirection(str, Enum):
    RISK_INCREASING = "RISK_INCREASING"
    RISK_NEUTRAL = "RISK_NEUTRAL"
    RISK_REDUCING = "RISK_REDUCING"
    FORCED_EXIT = "FORCED_EXIT"


ADAPTIVE_EXPOSURE_V2_STRATEGY_ID = "a-share-small-account-adaptive-exposure-v2"
ADAPTIVE_EXPOSURE_V2_MAX_POSITIONS = 3
ADAPTIVE_EXPOSURE_V2_MAX_POSITION_WEIGHT = Decimal("0.40")
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_INSTRUMENT_ID_PATTERN = re.compile(r"^[0-9A-Z][0-9A-Z.]{2,31}$")
_REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


EMPTY_CASH_INTENT_TYPES = frozenset(
    {
        PortfolioIntentType.NO_ALPHA_CASH,
        PortfolioIntentType.RISK_OFF,
        PortfolioIntentType.ACCOUNT_DRAWDOWN_EXIT,
    }
)


class ExecutionMode(str, Enum):
    PAPER = "PAPER"
    SHADOW = "SHADOW"
    LIVE = "LIVE"


LIVE_NOT_SUPPORTED_CODE = "live_not_supported"
LIVE_NOT_SUPPORTED_MESSAGE = "LIVE execution is not supported by this repository"


class LiveNotSupportedError(ValueError):
    code = LIVE_NOT_SUPPORTED_CODE

    def __init__(self) -> None:
        super().__init__(LIVE_NOT_SUPPORTED_MESSAGE)


def is_live_execution_mode(value: object) -> bool:
    """Recognize legacy enum/string LIVE inputs so every boundary can reject them."""

    return isinstance(value, str) and value.strip().upper() == ExecutionMode.LIVE.value


class OrderStatus(str, Enum):
    PLANNED = "PLANNED"
    BLOCKED = "BLOCKED"
    RISK_APPROVED = "RISK_APPROVED"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PortfolioIntent:
    """A provenance-bound portfolio target; never an executable order by itself."""

    intent_id: str
    strategy_id: str
    intent_type: PortfolioIntentType
    decision_at: datetime
    available_at: datetime
    frozen_at: datetime
    target_gross_exposure: Decimal
    target_weights: Mapping[str, Decimal] = field(default_factory=dict)
    reason_codes: tuple[str, ...] = ()
    signal_sha256: str = ""
    market_data_sha256: str = ""
    model_sha256: str = ""
    risk_state_sha256: str = ""

    def __post_init__(self) -> None:
        if not self.intent_id.strip() or not self.strategy_id.strip():
            raise ValueError("intent_id and strategy_id must not be empty")
        if not isinstance(self.intent_type, PortfolioIntentType):
            raise ValueError("intent_type must be a PortfolioIntentType")
        for field_name in ("decision_at", "available_at", "frozen_at"):
            if getattr(self, field_name).tzinfo is None:
                raise ValueError(f"{field_name} must be timezone-aware")
        if not self.available_at <= self.frozen_at <= self.decision_at:
            raise ValueError("intent timeline must satisfy available_at <= frozen_at <= decision_at")
        target = Decimal(str(self.target_gross_exposure))
        weights = {
            str(instrument_id): Decimal(str(weight))
            for instrument_id, weight in self.target_weights.items()
        }
        if target < 0 or target > 1:
            raise ValueError("target_gross_exposure must be between zero and one")
        if any(not instrument_id.strip() for instrument_id in weights):
            raise ValueError("target instrument ids must not be empty")
        if any(weight < 0 or weight > 1 for weight in weights.values()):
            raise ValueError("target weights must be between zero and one")
        if self.strategy_id == ADAPTIVE_EXPOSURE_V2_STRATEGY_ID:
            if _IDENTIFIER_PATTERN.fullmatch(self.intent_id) is None:
                raise ValueError("Adaptive Exposure V2 intent_id violates identifier pattern")
            invalid_instruments = [
                instrument_id
                for instrument_id in weights
                if _INSTRUMENT_ID_PATTERN.fullmatch(instrument_id) is None
            ]
            if invalid_instruments:
                raise ValueError(
                    "Adaptive Exposure V2 instrument id violates Schema pattern"
                )
            if len(weights) > ADAPTIVE_EXPOSURE_V2_MAX_POSITIONS:
                raise ValueError(
                    "Adaptive Exposure V2 target_weights allows at most 3 keys"
                )
            if any(weight == 0 for weight in weights.values()):
                raise ValueError(
                    "Adaptive Exposure V2 does not allow zero-weight target entries"
                )
            positive_weights = [weight for weight in weights.values() if weight > 0]
            if len(positive_weights) > ADAPTIVE_EXPOSURE_V2_MAX_POSITIONS:
                raise ValueError(
                    "Adaptive Exposure V2 allows at most 3 positive positions"
                )
            if any(
                weight > ADAPTIVE_EXPOSURE_V2_MAX_POSITION_WEIGHT
                for weight in positive_weights
            ):
                raise ValueError(
                    "Adaptive Exposure V2 position weight must not exceed 0.40"
                )
        if sum(weights.values(), Decimal("0")) > target:
            raise ValueError("target weights exceed target_gross_exposure")
        if not weights and self.intent_type not in EMPTY_CASH_INTENT_TYPES:
            raise ValueError("empty targets require an explicit cash or exit intent")
        if self.intent_type in EMPTY_CASH_INTENT_TYPES and (weights or target != 0):
            raise ValueError("cash and exit intents require zero exposure and empty targets")
        if not self.reason_codes or any(not str(code).strip() for code in self.reason_codes):
            raise ValueError("reason_codes must contain non-empty values")
        if self.strategy_id == ADAPTIVE_EXPOSURE_V2_STRATEGY_ID:
            normalized_reason_codes = tuple(str(code) for code in self.reason_codes)
            if len(set(normalized_reason_codes)) != len(normalized_reason_codes):
                raise ValueError("Adaptive Exposure V2 reason_codes must be unique")
            if any(
                _REASON_CODE_PATTERN.fullmatch(code) is None
                for code in normalized_reason_codes
            ):
                raise ValueError(
                    "Adaptive Exposure V2 reason code violates lowercase Schema pattern"
                )
        for field_name in (
            "signal_sha256",
            "market_data_sha256",
            "model_sha256",
            "risk_state_sha256",
        ):
            value = str(getattr(self, field_name))
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError(f"{field_name} must be a lowercase SHA-256")
        object.__setattr__(self, "target_gross_exposure", target)
        object.__setattr__(self, "target_weights", MappingProxyType(weights))
        object.__setattr__(self, "reason_codes", tuple(str(code) for code in self.reason_codes))

    @property
    def intent_sha256(self) -> str:
        payload = {
            "schema_version": "portfolio-intent.v1",
            "intent_id": self.intent_id,
            "strategy_id": self.strategy_id,
            "decision_at": self.decision_at.isoformat(),
            "available_at": self.available_at.isoformat(),
            "frozen_at": self.frozen_at.isoformat(),
            "intent_type": self.intent_type.value,
            "target_gross_exposure": str(self.target_gross_exposure),
            "target_weights": {
                key: str(value) for key, value in sorted(self.target_weights.items())
            },
            "reason_codes": list(self.reason_codes),
            "signal_sha256": self.signal_sha256,
            "market_data_sha256": self.market_data_sha256,
            "model_sha256": self.model_sha256,
            "risk_state_sha256": self.risk_state_sha256,
            "live_supported": False,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "portfolio-intent.v1",
            "intent_id": self.intent_id,
            "strategy_id": self.strategy_id,
            "decision_at": self.decision_at.isoformat(),
            "available_at": self.available_at.isoformat(),
            "frozen_at": self.frozen_at.isoformat(),
            "intent_type": self.intent_type.value,
            "target_gross_exposure": str(self.target_gross_exposure),
            "target_weights": {
                key: str(value) for key, value in sorted(self.target_weights.items())
            },
            "reason_codes": list(self.reason_codes),
            "signal_sha256": self.signal_sha256,
            "market_data_sha256": self.market_data_sha256,
            "model_sha256": self.model_sha256,
            "risk_state_sha256": self.risk_state_sha256,
            "intent_sha256": self.intent_sha256,
            "live_supported": False,
        }


@dataclass(frozen=True)
class InstrumentRule:
    instrument_id: str
    name: str
    instrument_type: str
    lot_size: int
    tick_size: Decimal
    sell_stamp_duty_rate: Decimal
    t_plus_one: bool

    def __post_init__(self) -> None:
        if self.lot_size <= 0:
            raise ValueError("lot_size must be positive")
        if self.tick_size <= 0:
            raise ValueError("tick_size must be positive")


@dataclass(frozen=True)
class MarketQuote:
    instrument_id: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    as_of: datetime
    suspended: bool = False
    buy_blocked: bool = False
    sell_blocked: bool = False

    def __post_init__(self) -> None:
        if min(self.bid, self.ask, self.last) <= 0:
            raise ValueError("quote prices must be positive")
        if self.bid > self.ask:
            raise ValueError("crossed quote is not accepted")
        if self.as_of.tzinfo is None:
            raise ValueError("quote timestamp must be timezone-aware")


@dataclass(frozen=True)
class Position:
    instrument_id: str
    quantity: int
    sellable_quantity: int

    def __post_init__(self) -> None:
        if self.quantity < 0 or self.sellable_quantity < 0:
            raise ValueError("position quantities must not be negative")
        if self.sellable_quantity > self.quantity:
            raise ValueError("sellable_quantity cannot exceed quantity")


@dataclass(frozen=True)
class AccountSnapshot:
    """Strategy-only ledger; unrelated long-term holdings must not be included."""

    strategy_id: str
    cash: Decimal
    positions: Mapping[str, Position] = field(default_factory=dict)
    snapshot_id: str = ""
    as_of: datetime | None = None

    def __post_init__(self) -> None:
        if self.cash < 0:
            raise ValueError("cash must not be negative")
        if any(key != position.instrument_id for key, position in self.positions.items()):
            raise ValueError("position key must match instrument_id")
        if self.as_of is not None and self.as_of.tzinfo is None:
            raise ValueError("account as_of must be timezone-aware")


@dataclass(frozen=True)
class OrderIntent:
    client_order_id: str
    instrument_id: str
    side: Side
    quantity: int
    limit_price: Decimal
    estimated_fee: Decimal
    reason: str
    risk_direction: OrderRiskDirection = OrderRiskDirection.RISK_NEUTRAL
    intent_id: str = ""
    attempt_id: str = ""

    def __post_init__(self) -> None:
        if not self.client_order_id:
            raise ValueError("client_order_id must not be empty")
        if self.quantity <= 0:
            raise ValueError("order quantity must be positive")
        if self.limit_price <= 0:
            raise ValueError("limit price must be positive")
        if self.estimated_fee < 0:
            raise ValueError("estimated fee must not be negative")
        if not isinstance(self.side, Side):
            raise ValueError("order side must be a Side enum")
        if not isinstance(self.risk_direction, OrderRiskDirection):
            raise ValueError("risk_direction must be an OrderRiskDirection enum")

    @property
    def notional(self) -> Decimal:
        return self.limit_price * self.quantity


@dataclass(frozen=True)
class PlanRejection:
    instrument_id: str
    code: str
    message: str


@dataclass(frozen=True)
class RebalancePlan:
    plan_id: str
    decision_id: str
    account_fingerprint: str
    strategy_id: str
    decision_time: datetime
    strategy_equity: Decimal
    orders: tuple[OrderIntent, ...]
    rejections: tuple[PlanRejection, ...]
    projected_cash: Decimal
    turnover_ratio: Decimal
    turnover_limit: Decimal
    bootstrap: bool
    intent_id: str = ""
    intent_sha256: str = ""
    attempt_id: str = ""
    parent_attempt_id: str | None = None
    portfolio_intent_type: PortfolioIntentType = PortfolioIntentType.ALPHA_REBALANCE
    target_gross_exposure: Decimal = Decimal("0")
    feasible_gross_exposure: Decimal = Decimal("0")
    realized_gross_exposure: Decimal | None = None
    ordinary_turnover_ratio: Decimal = Decimal("0")
    blocked_exit_reasons: tuple[PlanRejection, ...] = ()
    parent_plan_sha256: str = ""
    bound_portfolio_intent: PortfolioIntent | None = field(default=None, repr=False)
    previous_controlled_session: date | None = None
    controlled_calendar_sha256: str = ""
    controlled_session_evidence_sha256: str = ""
    execution_quote_bundle_sha256: str = ""

    def __post_init__(self) -> None:
        if not self.decision_id or not self.account_fingerprint:
            raise ValueError("plan must bind a decision and account snapshot")
        if type(self.bootstrap) is not bool:
            raise ValueError("bootstrap must be a boolean")
        if self.strategy_equity <= 0:
            raise ValueError("strategy_equity must be positive")
        if self.projected_cash < 0:
            raise ValueError("projected_cash must not be negative")
        if self.turnover_ratio < 0 or self.turnover_limit < 0:
            raise ValueError("turnover values must not be negative")
        if not isinstance(self.portfolio_intent_type, PortfolioIntentType):
            raise ValueError("portfolio_intent_type must be a PortfolioIntentType")
        if self.bound_portfolio_intent is not None and not isinstance(
            self.bound_portfolio_intent, PortfolioIntent
        ):
            raise ValueError("bound_portfolio_intent must be a PortfolioIntent")
        if (
            self.previous_controlled_session is not None
            and type(self.previous_controlled_session) is not date
        ):
            raise ValueError("previous_controlled_session must be a date")
        if not isinstance(self.execution_quote_bundle_sha256, str):
            raise ValueError("execution_quote_bundle_sha256 must be a string")
        if self.parent_attempt_id is not None and self.parent_attempt_id == self.attempt_id:
            raise ValueError("parent_attempt_id must differ from attempt_id")
        for field_name in ("target_gross_exposure", "feasible_gross_exposure"):
            value = getattr(self, field_name)
            if value < 0 or value > 1:
                raise ValueError(f"{field_name} must be between zero and one")
        if self.ordinary_turnover_ratio < 0:
            raise ValueError("ordinary_turnover_ratio must not be negative")
        if self.realized_gross_exposure is not None and not (
            Decimal("0") <= self.realized_gross_exposure <= Decimal("1")
        ):
            raise ValueError("realized_gross_exposure must be between zero and one")
        order_ids = [order.client_order_id for order in self.orders]
        if len(order_ids) != len(set(order_ids)):
            raise ValueError("plan contains duplicate client_order_id")
