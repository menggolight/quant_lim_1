"""Explicit transaction-cost model used by planning and paper fills."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from trading.models import InstrumentRule, Side


CENT = Decimal("0.01")


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class FeeSchedule:
    commission_rate: Decimal
    minimum_commission: Decimal
    exchange_fee_rate: Decimal

    def __post_init__(self) -> None:
        if min(self.commission_rate, self.minimum_commission, self.exchange_fee_rate) < 0:
            raise ValueError("fee inputs must not be negative")

    def estimate(self, side: Side, notional: Decimal, instrument: InstrumentRule) -> Decimal:
        if not isinstance(side, Side):
            raise ValueError("fee side must be a Side enum")
        if notional <= 0:
            return Decimal("0.00")
        commission = max(self.minimum_commission, notional * self.commission_rate)
        exchange_fee = notional * self.exchange_fee_rate
        stamp_duty = notional * instrument.sell_stamp_duty_rate if side == Side.SELL else Decimal("0")
        return money(commission + exchange_fee + stamp_duty)
