"""Immutable domain models for the trading execution layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Mapping


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


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
        order_ids = [order.client_order_id for order in self.orders]
        if len(order_ids) != len(set(order_ids)):
            raise ValueError("plan contains duplicate client_order_id")
