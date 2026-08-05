"""Broker-level facts kept separate from strategy-owned assets."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Mapping


def _require_non_negative(name: str, value: Decimal | int) -> None:
    if value < 0:
        raise ValueError(f"{name} must not be negative")


@dataclass(frozen=True)
class BrokerFunds:
    available_cash: Decimal
    frozen_cash: Decimal
    hold_cash: Decimal
    total_value: Decimal
    market_value: Decimal
    transferable_cash: Decimal

    def __post_init__(self) -> None:
        for name in (
            "available_cash",
            "frozen_cash",
            "hold_cash",
            "total_value",
            "market_value",
            "transferable_cash",
        ):
            _require_non_negative(name, getattr(self, name))


@dataclass(frozen=True)
class BrokerPosition:
    instrument_id: str
    quantity: int
    sellable_quantity: int
    today_quantity: int
    frozen_quantity: int
    price: Decimal
    market_value: Decimal
    hold_cost: Decimal

    def __post_init__(self) -> None:
        if not self.instrument_id:
            raise ValueError("instrument_id must not be empty")
        for name in (
            "quantity",
            "sellable_quantity",
            "today_quantity",
            "frozen_quantity",
            "price",
            "market_value",
            "hold_cost",
        ):
            _require_non_negative(name, getattr(self, name))
        if self.sellable_quantity > self.quantity:
            raise ValueError("sellable_quantity cannot exceed quantity")


@dataclass(frozen=True)
class BrokerOrder:
    broker_order_id: str
    entrust_no: str
    instrument_id: str
    side: str
    status: str
    quantity: int
    filled_quantity: int
    withdrawn_quantity: int
    entrust_price: Decimal
    average_price: Decimal
    created_at: datetime | None
    cancel_info: str = ""

    def __post_init__(self) -> None:
        if not self.broker_order_id or not self.instrument_id:
            raise ValueError("broker order identity must not be empty")
        if self.side not in {"BUY", "SELL", "UNKNOWN"}:
            raise ValueError("broker order side is invalid")
        for name in ("quantity", "filled_quantity", "withdrawn_quantity"):
            _require_non_negative(name, getattr(self, name))
        if self.filled_quantity > self.quantity:
            raise ValueError("filled_quantity cannot exceed quantity")
        if self.withdrawn_quantity > self.quantity:
            raise ValueError("withdrawn_quantity cannot exceed quantity")
        _require_non_negative("entrust_price", self.entrust_price)
        _require_non_negative("average_price", self.average_price)
        if self.created_at is not None and self.created_at.tzinfo is None:
            raise ValueError("order timestamp must be timezone-aware")


@dataclass(frozen=True)
class BrokerTrade:
    broker_trade_id: str
    broker_order_id: str
    entrust_no: str
    instrument_id: str
    side: str
    quantity: int
    price: Decimal
    business_balance: Decimal
    real_type: str
    traded_at: datetime | None

    def __post_init__(self) -> None:
        if not self.broker_trade_id or not self.instrument_id:
            raise ValueError("broker trade identity must not be empty")
        if self.side not in {"BUY", "SELL", "UNKNOWN"}:
            raise ValueError("broker trade side is invalid")
        _require_non_negative("quantity", self.quantity)
        _require_non_negative("price", self.price)
        _require_non_negative("business_balance", self.business_balance)
        if self.traded_at is not None and self.traded_at.tzinfo is None:
            raise ValueError("trade timestamp must be timezone-aware")


@dataclass(frozen=True)
class RawBrokerSnapshot:
    broker: str
    adapter: str
    environment: str
    api_shape_id: str
    shape_checked: bool
    account_binding_id: str
    account_fingerprint: str
    account_binding_matched: bool
    source_authenticated: bool
    session_id: str
    sequence: int
    started_at: datetime
    completed_at: datetime
    capture_consistency: str
    funds: BrokerFunds
    positions: Mapping[str, BrokerPosition] = field(default_factory=dict)
    open_orders: tuple[BrokerOrder, ...] = ()
    today_orders: tuple[BrokerOrder, ...] = ()
    trades: tuple[BrokerTrade, ...] = ()
    warnings: tuple[str, ...] = ()
    payload_sha256: str = ""

    def __post_init__(self) -> None:
        if self.broker != "HTSC" or self.adapter != "mquant":
            raise ValueError("unexpected broker adapter identity")
        if self.environment != "real_account_read_only":
            raise ValueError("snapshot is not from the read-only environment")
        if any(
            type(value) is not bool
            for value in (
                self.account_binding_matched,
                self.shape_checked,
                self.source_authenticated,
            )
        ):
            raise ValueError("snapshot verification flags must be booleans")
        if self.capture_consistency != "sequential_non_atomic":
            raise ValueError("unsupported broker capture consistency")
        if not self.account_binding_id or not self.account_fingerprint or not self.session_id:
            raise ValueError("snapshot binding and session must not be empty")
        if type(self.sequence) is not int or self.sequence < 1:
            raise ValueError("snapshot sequence must be a positive integer")
        if self.started_at.tzinfo is None or self.completed_at.tzinfo is None:
            raise ValueError("snapshot timestamps must be timezone-aware")
        if self.completed_at < self.started_at:
            raise ValueError("snapshot completed_at precedes started_at")
        if any(key != value.instrument_id for key, value in self.positions.items()):
            raise ValueError("broker position key must match instrument_id")
