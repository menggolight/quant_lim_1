"""Idempotent immediate-fill broker used only for paper validation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime, timezone
from typing import Mapping

from trading.costs import FeeSchedule, money
from trading.integrity import account_fingerprint, plan_fingerprint
from trading.models import (
    AccountSnapshot,
    ExecutionMode,
    InstrumentRule,
    OrderStatus,
    OrderIntent,
    Position,
    RebalancePlan,
    Side,
)
from trading.order_store import FINAL_STATUSES, OrderStore
from trading.risk import ExecutionApproval


@dataclass(frozen=True)
class PaperFill:
    client_order_id: str
    instrument_id: str
    side: Side
    quantity: int
    price: Decimal
    fee: Decimal
    status: str


@dataclass(frozen=True)
class PaperExecutionResult:
    account: AccountSnapshot
    fills: tuple[PaperFill, ...]


class PaperBroker:
    def __init__(
        self,
        account: AccountSnapshot,
        instruments: Mapping[str, InstrumentRule],
        fees: FeeSchedule,
        order_store: OrderStore,
    ) -> None:
        self._account = account
        self._instruments = dict(instruments)
        self._fees = fees
        self._order_store = order_store
        self._order_store.ensure_paper_account(
            account, account.as_of or datetime.now(timezone.utc)
        )

    @property
    def account(self) -> AccountSnapshot:
        return self._account

    def execute(
        self,
        plan: RebalancePlan,
        approval: ExecutionApproval | None,
        execution_time: datetime,
    ) -> PaperExecutionResult:
        if plan.strategy_id != self._account.strategy_id:
            raise ValueError("Plan strategy_id does not match paper account")
        known_statuses: list[OrderStatus] = []
        all_known = True
        for order in plan.orders:
            try:
                known_statuses.append(self._order_store.status(order.client_order_id))
            except KeyError:
                all_known = False
                break
        if all_known and known_statuses and all(status is OrderStatus.FILLED for status in known_statuses):
            self._order_store.register_plan(plan, plan.decision_time)
            return PaperExecutionResult(
                account=self._account,
                fills=tuple(self._fill(order, "DUPLICATE") for order in plan.orders),
            )
        if approval is None or not isinstance(approval, ExecutionApproval):
            raise ValueError("Paper execution requires a gate approval")
        if not approval.was_issued_by_gate():
            raise ValueError("Paper execution approval was not issued by ExecutionGate")
        if approval.mode is not ExecutionMode.PAPER:
            raise ValueError("Paper broker only accepts PAPER approval")
        if execution_time.tzinfo is None:
            raise ValueError("execution_time must be timezone-aware")
        if execution_time < approval.issued_at or execution_time > approval.valid_until:
            raise ValueError("Execution approval is not currently valid")
        if approval.plan_fingerprint != plan_fingerprint(plan):
            raise ValueError("Execution approval does not match the plan")
        current_account_fingerprint = account_fingerprint(self._account)
        if approval.account_fingerprint != current_account_fingerprint:
            raise ValueError("Execution approval does not match the current account")
        if plan.account_fingerprint != current_account_fingerprint:
            raise ValueError("Plan is stale for the current account")

        self._order_store.register_plan(plan, plan.decision_time)
        usage = self._order_store.daily_usage(plan.strategy_id, execution_time.date().isoformat())
        pending_orders = [
            order
            for order in plan.orders
            if self._order_store.status(order.client_order_id) is not OrderStatus.FILLED
        ]
        pending_notional = sum((order.notional for order in pending_orders), Decimal("0"))
        cumulative_turnover = (usage.notional + pending_notional) / plan.strategy_equity
        if cumulative_turnover > approval.turnover_limit:
            raise ValueError("Persisted daily turnover limit would be exceeded")
        if usage.order_count + len(pending_orders) > approval.max_orders_per_day:
            raise ValueError("Persisted daily order-count limit would be exceeded")
        if plan.bootstrap and self._order_store.bootstrap_used(plan.strategy_id):
            raise ValueError("Bootstrap allowance has already been used")
        cash = self._account.cash
        positions = dict(self._account.positions)
        fills: list[PaperFill] = []

        for order in plan.orders:
            stored_status = self._order_store.status(order.client_order_id)
            if stored_status is OrderStatus.FILLED:
                fills.append(self._fill(order, "DUPLICATE"))
                continue
            if stored_status in FINAL_STATUSES:
                raise ValueError(f"Paper order is terminal but not filled: {stored_status.value}")
            if stored_status is not None and stored_status is not OrderStatus.PLANNED:
                raise ValueError(f"Paper order requires reconciliation from {stored_status.value}")
            self._order_store.transition(
                order.client_order_id, OrderStatus.RISK_APPROVED, execution_time
            )
            self._order_store.transition(order.client_order_id, OrderStatus.SUBMITTING, execution_time)
            rule = self._instruments[order.instrument_id]
            fee = self._fees.estimate(order.side, order.notional, rule)
            if fee != order.estimated_fee:
                raise ValueError("Paper fee differs from planned fee")

            position = positions.get(order.instrument_id, Position(order.instrument_id, 0, 0))
            if order.side == Side.BUY:
                total_cost = order.notional + fee
                if total_cost > cash:
                    raise ValueError("Insufficient paper cash")
                cash = money(cash - total_cost)
                positions[order.instrument_id] = Position(
                    order.instrument_id,
                    quantity=position.quantity + order.quantity,
                    sellable_quantity=position.sellable_quantity + (0 if rule.t_plus_one else order.quantity),
                )
            else:
                if order.quantity > position.sellable_quantity:
                    raise ValueError("Paper sell exceeds sellable quantity")
                cash = money(cash + order.notional - fee)
                remaining = position.quantity - order.quantity
                if remaining:
                    positions[order.instrument_id] = Position(
                        order.instrument_id,
                        quantity=remaining,
                        sellable_quantity=position.sellable_quantity - order.quantity,
                    )
                else:
                    positions.pop(order.instrument_id, None)

            next_account = AccountSnapshot(
                strategy_id=self._account.strategy_id,
                cash=money(cash),
                positions=positions,
                snapshot_id=f"paper:{plan.plan_id}:{order.client_order_id}",
                as_of=execution_time,
            )
            self._order_store.transition(
                order.client_order_id, OrderStatus.ACKNOWLEDGED, execution_time
            )
            self._order_store.commit_paper_fill(
                order.client_order_id,
                execution_time,
                details={"paper_fill_price": str(order.limit_price), "paper_fee": str(fee)},
                account=next_account,
                mark_bootstrap_used=plan.bootstrap,
            )
            self._account = next_account
            fills.append(self._fill(order, "FILLED"))

        persisted = self._order_store.load_paper_account(self._account.strategy_id)
        if persisted != self._account:
            raise RuntimeError("Paper account failed persistence reconciliation")
        return PaperExecutionResult(account=self._account, fills=tuple(fills))

    @staticmethod
    def _fill(order: OrderIntent, status: str) -> PaperFill:
        return PaperFill(
            client_order_id=order.client_order_id,
            instrument_id=order.instrument_id,
            side=order.side,
            quantity=order.quantity,
            price=order.limit_price,
            fee=order.estimated_fee,
            status=status,
        )
