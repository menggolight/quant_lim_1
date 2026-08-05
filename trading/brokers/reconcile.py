"""Conservative bridge from broker facts to the strategy-owned ledger."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping

from trading.brokers.models import RawBrokerSnapshot
from trading.models import AccountSnapshot, Position


@dataclass(frozen=True)
class StrategyOwnershipLedger:
    strategy_id: str
    account_binding_id: str
    strategy_cash: Decimal
    baseline_complete: bool
    managed_instrument_ids: tuple[str, ...]
    baseline_quantities: Mapping[str, int] = field(default_factory=dict)
    strategy_positions: Mapping[str, Position] = field(default_factory=dict)
    known_broker_trade_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.strategy_id or not self.account_binding_id:
            raise ValueError("ownership ledger identity must not be empty")
        if self.strategy_cash < 0:
            raise ValueError("strategy_cash must not be negative")
        if type(self.baseline_complete) is not bool:
            raise ValueError("baseline_complete must be a boolean")
        if len(self.managed_instrument_ids) != len(set(self.managed_instrument_ids)):
            raise ValueError("managed instruments must be unique")
        if any(type(value) is not int or value < 0 for value in self.baseline_quantities.values()):
            raise ValueError("baseline quantities must be non-negative integers")
        if any(key != value.instrument_id for key, value in self.strategy_positions.items()):
            raise ValueError("strategy position key mismatch")


@dataclass(frozen=True)
class ReconciliationResult:
    allowed: bool
    block_codes: tuple[str, ...]
    account: AccountSnapshot | None


class ShadowReconciler:
    """Never infer strategy ownership from a broker whitelist or symbol alone.

    This bridge intentionally remains closed until a persistent, audited
    ownership event store can prove that strategy positions came from this
    system's confirmed broker trades.
    """

    def reconcile(
        self,
        raw: RawBrokerSnapshot,
        ledger: StrategyOwnershipLedger,
    ) -> ReconciliationResult:
        blocks: list[str] = []
        if not raw.account_binding_matched:
            blocks.append("broker_account_binding_unmatched")
        if not raw.shape_checked:
            blocks.append("broker_api_shape_unchecked")
        if not raw.source_authenticated:
            blocks.append("broker_snapshot_source_unauthenticated")
        if raw.capture_consistency != "sequential_non_atomic":
            blocks.append("broker_capture_consistency_unknown")
        else:
            blocks.append("broker_capture_not_atomic")
        blocks.append("audited_ownership_store_not_implemented")
        if raw.account_binding_id != ledger.account_binding_id:
            blocks.append("account_binding_mismatch")
        if not ledger.baseline_complete:
            blocks.append("ownership_baseline_missing")
        if raw.open_orders:
            blocks.append("broker_open_orders_present")
        if raw.funds.available_cash < ledger.strategy_cash:
            blocks.append("broker_cash_below_strategy_ledger")

        managed = set(ledger.managed_instrument_ids)
        if any(symbol not in managed for symbol in ledger.strategy_positions):
            blocks.append("strategy_position_outside_managed_universe")
        if any(symbol not in managed for symbol in ledger.baseline_quantities):
            blocks.append("baseline_position_outside_managed_universe")

        for symbol in managed:
            baseline = ledger.baseline_quantities.get(symbol, 0)
            strategy_position = ledger.strategy_positions.get(symbol)
            strategy_quantity = strategy_position.quantity if strategy_position else 0
            actual = raw.positions.get(symbol)
            actual_quantity = actual.quantity if actual else 0
            if actual_quantity != baseline + strategy_quantity:
                blocks.append(f"ownership_ambiguous:{symbol}")
                continue
            if (
                strategy_position is not None
                and actual is not None
                and actual.sellable_quantity < strategy_position.sellable_quantity
            ):
                blocks.append(f"strategy_sellable_exceeds_broker:{symbol}")

        known_trades = set(ledger.known_broker_trade_ids)
        if any(
            trade.instrument_id in managed and trade.broker_trade_id not in known_trades
            for trade in raw.trades
        ):
            blocks.append("ownership_ambiguous:unknown_managed_trade")

        unique_blocks = tuple(dict.fromkeys(blocks))
        if unique_blocks:
            return ReconciliationResult(False, unique_blocks, None)
        account = AccountSnapshot(
            strategy_id=ledger.strategy_id,
            cash=ledger.strategy_cash,
            positions=dict(ledger.strategy_positions),
            snapshot_id=(
                f"htsc:{raw.account_binding_id}:{raw.session_id}:{raw.sequence}:"
                f"{raw.payload_sha256[-12:]}"
            ),
            as_of=raw.completed_at,
        )
        return ReconciliationResult(True, (), account)
