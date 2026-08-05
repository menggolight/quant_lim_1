"""Narrow read-only broker boundary."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from trading.brokers.models import RawBrokerSnapshot


class BrokerReadPort(Protocol):
    def read_snapshot(self, now: datetime) -> RawBrokerSnapshot:
        """Return one validated, point-in-time broker fact snapshot."""
