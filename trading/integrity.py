"""Deterministic fingerprints that bind approvals to exact state."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from trading.models import AccountSnapshot, RebalancePlan


def _digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def account_fingerprint(account: AccountSnapshot) -> str:
    return _digest(
        {
            "strategy_id": account.strategy_id,
            "cash": str(account.cash),
            "snapshot_id": account.snapshot_id,
            "as_of": account.as_of.isoformat() if account.as_of else None,
            "positions": [
                {
                    "instrument_id": instrument_id,
                    "quantity": position.quantity,
                    "sellable_quantity": position.sellable_quantity,
                }
                for instrument_id, position in sorted(account.positions.items())
            ],
        }
    )


def plan_fingerprint(plan: RebalancePlan) -> str:
    return _digest(
        {
            "plan_id": plan.plan_id,
            "decision_id": plan.decision_id,
            "account_fingerprint": plan.account_fingerprint,
            "strategy_id": plan.strategy_id,
            "decision_time": plan.decision_time.isoformat(),
            "strategy_equity": str(plan.strategy_equity),
            "orders": [
                {
                    "client_order_id": order.client_order_id,
                    "instrument_id": order.instrument_id,
                    "side": order.side.value,
                    "quantity": order.quantity,
                    "limit_price": str(order.limit_price),
                    "estimated_fee": str(order.estimated_fee),
                }
                for order in plan.orders
            ],
            "rejections": [
                {"instrument_id": item.instrument_id, "code": item.code, "message": item.message}
                for item in plan.rejections
            ],
            "projected_cash": str(plan.projected_cash),
            "turnover_ratio": str(plan.turnover_ratio),
            "turnover_limit": str(plan.turnover_limit),
            "bootstrap": plan.bootstrap,
        }
    )
