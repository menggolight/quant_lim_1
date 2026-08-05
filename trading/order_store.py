"""SQLite order journal with restart-safe idempotency and transitions."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from trading.integrity import account_fingerprint
from trading.models import AccountSnapshot, OrderStatus, Position, RebalancePlan


FINAL_STATUSES = {
    OrderStatus.BLOCKED,
    OrderStatus.FILLED,
    OrderStatus.CANCELED,
    OrderStatus.REJECTED,
}

ALLOWED_TRANSITIONS: Mapping[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PLANNED: frozenset({OrderStatus.BLOCKED, OrderStatus.RISK_APPROVED}),
    OrderStatus.RISK_APPROVED: frozenset({OrderStatus.BLOCKED, OrderStatus.SUBMITTING}),
    OrderStatus.SUBMITTING: frozenset(
        {OrderStatus.ACKNOWLEDGED, OrderStatus.REJECTED, OrderStatus.UNKNOWN}
    ),
    OrderStatus.UNKNOWN: frozenset({OrderStatus.ACKNOWLEDGED, OrderStatus.REJECTED}),
    OrderStatus.ACKNOWLEDGED: frozenset(
        {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCEL_PENDING}
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {OrderStatus.FILLED, OrderStatus.CANCEL_PENDING, OrderStatus.CANCELED}
    ),
    OrderStatus.CANCEL_PENDING: frozenset(
        {OrderStatus.CANCELED, OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.UNKNOWN}
    ),
    OrderStatus.BLOCKED: frozenset(),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
}


class InvalidOrderTransition(ValueError):
    pass


@dataclass(frozen=True)
class OrderEvent:
    sequence: int
    client_order_id: str
    previous_status: OrderStatus | None
    status: OrderStatus
    event_time: datetime
    details: dict[str, Any]


@dataclass(frozen=True)
class DailyUsage:
    order_count: int
    notional: Decimal


class OrderStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, timeout=30)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    client_order_id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    instrument_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    limit_price TEXT NOT NULL,
                    estimated_fee TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_accounts (
                    strategy_id TEXT PRIMARY KEY,
                    cash TEXT NOT NULL,
                    positions_json TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    as_of TEXT,
                    fingerprint TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_runtime (
                    strategy_id TEXT PRIMARY KEY,
                    bootstrap_used INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS order_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_order_id TEXT NOT NULL,
                    previous_status TEXT,
                    status TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    FOREIGN KEY(client_order_id) REFERENCES orders(client_order_id)
                )
                """
            )

    def register_plan(self, plan: RebalancePlan, event_time: datetime) -> int:
        inserted = 0
        timestamp = event_time.isoformat()
        with self._connection:
            for order in plan.orders:
                row = self._connection.execute(
                    "SELECT * FROM orders WHERE client_order_id = ?", (order.client_order_id,)
                ).fetchone()
                immutable = (
                    plan.plan_id,
                    plan.strategy_id,
                    order.instrument_id,
                    order.side.value,
                    order.quantity,
                    str(order.limit_price),
                    str(order.estimated_fee),
                )
                if row is not None:
                    stored = tuple(
                        row[key]
                        for key in (
                            "plan_id",
                            "strategy_id",
                            "instrument_id",
                            "side",
                            "quantity",
                            "limit_price",
                            "estimated_fee",
                        )
                    )
                    if stored != immutable:
                        raise ValueError(f"client_order_id collision: {order.client_order_id}")
                    continue
                self._connection.execute(
                    """
                    INSERT INTO orders (
                        client_order_id, plan_id, strategy_id, instrument_id, side,
                        quantity, limit_price, estimated_fee, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (order.client_order_id, *immutable, OrderStatus.PLANNED.value, timestamp, timestamp),
                )
                self._connection.execute(
                    """
                    INSERT INTO order_events (
                        client_order_id, previous_status, status, event_time, details_json
                    ) VALUES (?, NULL, ?, ?, ?)
                    """,
                    (order.client_order_id, OrderStatus.PLANNED.value, timestamp, "{}"),
                )
                inserted += 1
        return inserted

    def status(self, client_order_id: str) -> OrderStatus:
        row = self._connection.execute(
            "SELECT status FROM orders WHERE client_order_id = ?", (client_order_id,)
        ).fetchone()
        if row is None:
            raise KeyError(client_order_id)
        return OrderStatus(row["status"])

    def transition(
        self,
        client_order_id: str,
        status: OrderStatus,
        event_time: datetime,
        details: Mapping[str, Any] | None = None,
    ) -> bool:
        previous = self.status(client_order_id)
        if previous is status:
            return False
        if status not in ALLOWED_TRANSITIONS[previous]:
            raise InvalidOrderTransition(f"{client_order_id}: {previous.value} -> {status.value}")
        timestamp = event_time.isoformat()
        details_json = json.dumps(dict(details or {}), ensure_ascii=False, sort_keys=True)
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE orders SET status = ?, updated_at = ? WHERE client_order_id = ? AND status = ?",
                (status.value, timestamp, client_order_id, previous.value),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"Concurrent order transition detected for {client_order_id}")
            self._connection.execute(
                """
                INSERT INTO order_events (
                    client_order_id, previous_status, status, event_time, details_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (client_order_id, previous.value, status.value, timestamp, details_json),
            )
        return True

    def events(self, client_order_id: str) -> tuple[OrderEvent, ...]:
        rows = self._connection.execute(
            """
            SELECT sequence, client_order_id, previous_status, status, event_time, details_json
            FROM order_events WHERE client_order_id = ? ORDER BY sequence
            """,
            (client_order_id,),
        ).fetchall()
        return tuple(
            OrderEvent(
                sequence=row["sequence"],
                client_order_id=row["client_order_id"],
                previous_status=OrderStatus(row["previous_status"]) if row["previous_status"] else None,
                status=OrderStatus(row["status"]),
                event_time=datetime.fromisoformat(row["event_time"]),
                details=json.loads(row["details_json"]),
            )
            for row in rows
        )

    @staticmethod
    def _account_values(account: AccountSnapshot) -> tuple[str, str, str, str | None, str]:
        positions_json = json.dumps(
            {
                instrument_id: {
                    "quantity": position.quantity,
                    "sellable_quantity": position.sellable_quantity,
                }
                for instrument_id, position in sorted(account.positions.items())
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return (
            str(account.cash),
            positions_json,
            account.snapshot_id,
            account.as_of.isoformat() if account.as_of else None,
            account_fingerprint(account),
        )

    def ensure_paper_account(self, account: AccountSnapshot, event_time: datetime) -> None:
        row = self._connection.execute(
            "SELECT fingerprint FROM paper_accounts WHERE strategy_id = ?", (account.strategy_id,)
        ).fetchone()
        fingerprint = account_fingerprint(account)
        if row is not None:
            if row["fingerprint"] != fingerprint:
                raise ValueError("Paper account differs from the persisted ledger")
            return
        cash, positions_json, snapshot_id, as_of, fingerprint = self._account_values(account)
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO paper_accounts (
                    strategy_id, cash, positions_json, snapshot_id, as_of, fingerprint, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account.strategy_id,
                    cash,
                    positions_json,
                    snapshot_id,
                    as_of,
                    fingerprint,
                    event_time.isoformat(),
                ),
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO strategy_runtime (strategy_id, bootstrap_used) VALUES (?, 0)",
                (account.strategy_id,),
            )

    def load_paper_account(self, strategy_id: str) -> AccountSnapshot:
        row = self._connection.execute(
            "SELECT * FROM paper_accounts WHERE strategy_id = ?", (strategy_id,)
        ).fetchone()
        if row is None:
            raise KeyError(strategy_id)
        raw_positions = json.loads(row["positions_json"])
        return AccountSnapshot(
            strategy_id=strategy_id,
            cash=Decimal(row["cash"]),
            positions={
                instrument_id: Position(
                    instrument_id,
                    quantity=int(values["quantity"]),
                    sellable_quantity=int(values["sellable_quantity"]),
                )
                for instrument_id, values in raw_positions.items()
            },
            snapshot_id=row["snapshot_id"],
            as_of=datetime.fromisoformat(row["as_of"]) if row["as_of"] else None,
        )

    def commit_paper_fill(
        self,
        client_order_id: str,
        event_time: datetime,
        details: Mapping[str, Any],
        account: AccountSnapshot,
        mark_bootstrap_used: bool,
    ) -> None:
        previous = self.status(client_order_id)
        if previous is not OrderStatus.ACKNOWLEDGED:
            raise InvalidOrderTransition(
                f"{client_order_id}: {previous.value} -> {OrderStatus.FILLED.value}"
            )
        timestamp = event_time.isoformat()
        details_json = json.dumps(dict(details), ensure_ascii=False, sort_keys=True)
        cash, positions_json, snapshot_id, as_of, fingerprint = self._account_values(account)
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE orders SET status = ?, updated_at = ? WHERE client_order_id = ? AND status = ?",
                (OrderStatus.FILLED.value, timestamp, client_order_id, previous.value),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"Concurrent paper fill detected for {client_order_id}")
            self._connection.execute(
                """
                INSERT INTO order_events (
                    client_order_id, previous_status, status, event_time, details_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    client_order_id,
                    previous.value,
                    OrderStatus.FILLED.value,
                    timestamp,
                    details_json,
                ),
            )
            self._connection.execute(
                """
                INSERT INTO paper_accounts (
                    strategy_id, cash, positions_json, snapshot_id, as_of, fingerprint, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(strategy_id) DO UPDATE SET
                    cash = excluded.cash,
                    positions_json = excluded.positions_json,
                    snapshot_id = excluded.snapshot_id,
                    as_of = excluded.as_of,
                    fingerprint = excluded.fingerprint,
                    updated_at = excluded.updated_at
                """,
                (
                    account.strategy_id,
                    cash,
                    positions_json,
                    snapshot_id,
                    as_of,
                    fingerprint,
                    timestamp,
                ),
            )
            if mark_bootstrap_used:
                self._connection.execute(
                    """
                    INSERT INTO strategy_runtime (strategy_id, bootstrap_used) VALUES (?, 1)
                    ON CONFLICT(strategy_id) DO UPDATE SET bootstrap_used = 1
                    """,
                    (account.strategy_id,),
                )

    def daily_usage(self, strategy_id: str, local_date: str) -> DailyUsage:
        rows = self._connection.execute(
            """
            SELECT quantity, limit_price FROM orders
            WHERE strategy_id = ? AND status = ? AND substr(updated_at, 1, 10) = ?
            """,
            (strategy_id, OrderStatus.FILLED.value, local_date),
        ).fetchall()
        return DailyUsage(
            order_count=len(rows),
            notional=sum(
                (Decimal(str(row["quantity"])) * Decimal(row["limit_price"]) for row in rows),
                Decimal("0"),
            ),
        )

    def bootstrap_used(self, strategy_id: str) -> bool:
        row = self._connection.execute(
            "SELECT bootstrap_used FROM strategy_runtime WHERE strategy_id = ?", (strategy_id,)
        ).fetchone()
        return bool(row["bootstrap_used"]) if row is not None else False

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "OrderStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
