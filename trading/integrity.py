"""Deterministic fingerprints that bind approvals to exact state."""

from __future__ import annotations

import hashlib
import json
from datetime import date, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping

from trading.costs import FeeSchedule
from trading.models import (
    AccountSnapshot,
    InstrumentRule,
    MarketQuote,
    RebalancePlan,
    Side,
)


def _digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _short_pipe_digest(parts: Iterable[str]) -> str:
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def is_lower_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _canonical_decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def canonical_controlled_calendar_sha256(
    controlled_sessions: tuple[date, ...],
) -> str:
    """Hash one strictly ordered internal session payload.

    This binds the supplied payload only; it does not authenticate an exchange
    calendar or prove that omitted dates are non-trading days.
    """

    if type(controlled_sessions) is not tuple or len(controlled_sessions) < 2:
        raise ValueError(
            "controlled calendar session payload must be a tuple with at least two dates"
        )
    if any(type(session) is not date for session in controlled_sessions):
        raise ValueError("controlled calendar sessions must contain exact date values")
    if any(
        previous >= current
        for previous, current in zip(
            controlled_sessions, controlled_sessions[1:]
        )
    ):
        raise ValueError(
            "controlled calendar sessions must be unique and strictly increasing"
        )
    encoded = json.dumps(
        [session.isoformat() for session in controlled_sessions],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def controlled_sessions_are_adjacent(
    controlled_sessions: tuple[date, ...],
    previous_session: date,
    execution_session: date,
) -> bool:
    """Return whether execution immediately follows previous in the payload."""

    canonical_controlled_calendar_sha256(controlled_sessions)
    if type(previous_session) is not date or type(execution_session) is not date:
        return False
    try:
        previous_index = controlled_sessions.index(previous_session)
    except ValueError:
        return False
    return (
        previous_index + 1 < len(controlled_sessions)
        and controlled_sessions[previous_index + 1] == execution_session
    )


def execution_quote_bundle_sha256(
    quotes: Mapping[str, MarketQuote],
) -> str:
    """Hash the exact normalized quote mapping consumed by the planner."""

    if not isinstance(quotes, Mapping):
        raise ValueError("execution quote bundle must be a mapping")
    quote_items = list(quotes.items())
    if any(
        not isinstance(instrument_id, str)
        or not isinstance(quote, MarketQuote)
        or quote.instrument_id != instrument_id
        or any(
            not isinstance(price, Decimal)
            for price in (quote.bid, quote.ask, quote.last)
        )
        for instrument_id, quote in quote_items
    ):
        raise ValueError("execution quote bundle mapping is invalid")
    records: list[dict[str, object]] = []
    for instrument_id, quote in sorted(quote_items, key=lambda item: item[0]):
        if (
            type(quote.suspended) is not bool
            or type(quote.buy_blocked) is not bool
            or type(quote.sell_blocked) is not bool
        ):
            raise ValueError("execution quote bundle flags must be booleans")

        records.append(
            {
                "instrument_id": instrument_id,
                "bid": _canonical_decimal(quote.bid),
                "ask": _canonical_decimal(quote.ask),
                "last": _canonical_decimal(quote.last),
                "as_of": quote.as_of.astimezone(timezone.utc).isoformat(),
                "suspended": quote.suspended,
                "buy_blocked": quote.buy_blocked,
                "sell_blocked": quote.sell_blocked,
            }
        )
    return _digest(
        {
            "scope": "execution-quote-bundle.v1",
            "quotes": records,
        }
    )


def execution_rule_bundle_sha256(
    fees: FeeSchedule,
    instruments: Mapping[str, InstrumentRule],
) -> str:
    """Hash the exact fee and instrument-rule bundle used for execution.

    The bundle deliberately includes every supplied ``InstrumentRule`` and a
    versioned whole-lot policy.  Planner, Gate, and PaperBroker must receive the
    same canonical bundle; a matching digest proves content identity only, not
    that the metadata came from an official registry.
    """

    if not isinstance(fees, FeeSchedule):
        raise ValueError("execution rule bundle requires a FeeSchedule")
    if any(
        not isinstance(value, Decimal) or not value.is_finite()
        for value in (
            fees.commission_rate,
            fees.minimum_commission,
            fees.exchange_fee_rate,
        )
    ):
        raise ValueError("execution rule bundle fee values must be Decimal")
    if not isinstance(instruments, Mapping):
        raise ValueError("execution rule bundle instruments must be a mapping")
    records: list[dict[str, object]] = []
    for instrument_id, instrument in sorted(instruments.items(), key=lambda item: item[0]):
        if (
            not isinstance(instrument_id, str)
            or not isinstance(instrument, InstrumentRule)
            or instrument.instrument_id != instrument_id
            or not isinstance(instrument.name, str)
            or not isinstance(instrument.instrument_type, str)
            or type(instrument.lot_size) is not int
            or not isinstance(instrument.tick_size, Decimal)
            or not isinstance(instrument.sell_stamp_duty_rate, Decimal)
            or not instrument.tick_size.is_finite()
            or not instrument.sell_stamp_duty_rate.is_finite()
            or type(instrument.t_plus_one) is not bool
        ):
            raise ValueError("execution rule bundle mapping is invalid")
        records.append(
            {
                "instrument_id": instrument_id,
                "name": instrument.name,
                "instrument_type": instrument.instrument_type,
                "lot_size": instrument.lot_size,
                "tick_size": _canonical_decimal(instrument.tick_size),
                "sell_stamp_duty_rate": _canonical_decimal(
                    instrument.sell_stamp_duty_rate
                ),
                "t_plus_one": instrument.t_plus_one,
            }
        )
    return _digest(
        {
            "scope": "execution-rule-bundle.v1",
            "fee_schedule": {
                "commission_rate": _canonical_decimal(fees.commission_rate),
                "minimum_commission": _canonical_decimal(
                    fees.minimum_commission
                ),
                "exchange_fee_rate": _canonical_decimal(fees.exchange_fee_rate),
            },
            "whole_lot_policy": "floor_to_instrument_lot.v1",
            "instrument_rules": records,
        }
    )


def legacy_client_order_id(
    strategy_id: str,
    decision_id: str,
    instrument_id: str,
    side: Side,
) -> str:
    """Frozen V1 idempotency key; changing this breaks existing journals."""

    return _short_pipe_digest(
        (strategy_id, decision_id, instrument_id, side.value)
    )


def adaptive_v2_client_order_id(
    strategy_id: str,
    intent_id: str,
    attempt_id: str,
    instrument_id: str,
    side: Side,
) -> str:
    return _short_pipe_digest(
        (strategy_id, intent_id, attempt_id, instrument_id, side.value)
    )


def legacy_rebalance_plan_id(
    strategy_id: str,
    decision_id: str,
    bound_account_sha256: str,
    client_order_ids: Iterable[str],
) -> str:
    return _short_pipe_digest(
        (strategy_id, decision_id, bound_account_sha256, *client_order_ids)
    )


def adaptive_v2_rebalance_plan_id(
    strategy_id: str,
    intent_id: str,
    intent_sha256: str,
    attempt_id: str,
    parent_attempt_id: str | None,
    parent_plan_sha256: str,
    bound_account_sha256: str,
    client_order_ids: Iterable[str],
    controlled_session_evidence_sha256: str = "",
    execution_quote_bundle_sha256: str = "",
    execution_rule_bundle_sha256: str = "",
) -> str:
    return _short_pipe_digest(
        (
            strategy_id,
            intent_id,
            intent_sha256,
            attempt_id,
            parent_attempt_id or "",
            parent_plan_sha256,
            controlled_session_evidence_sha256,
            execution_quote_bundle_sha256,
            execution_rule_bundle_sha256,
            bound_account_sha256,
            *client_order_ids,
        )
    )


def controlled_session_evidence_sha256(
    strategy_id: str,
    intent_id: str,
    intent_sha256: str,
    previous_controlled_session: date,
    execution_session: date,
    controlled_calendar_sha256: str,
) -> str:
    """Bind an internal controlled-session assertion; not official-calendar proof."""

    return _digest(
        {
            "scope": "internal-controlled-session-chain.v1",
            "strategy_id": strategy_id,
            "intent_id": intent_id,
            "intent_sha256": intent_sha256,
            "previous_controlled_session": previous_controlled_session.isoformat(),
            "execution_session": execution_session.isoformat(),
            "controlled_calendar_sha256": controlled_calendar_sha256,
            "official_calendar_proven": False,
        }
    )


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
    payload: dict[str, Any] = {
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
                    "risk_direction": order.risk_direction.value,
                    "intent_id": order.intent_id,
                    "attempt_id": order.attempt_id,
                }
                for order in plan.orders
            ],
            "rejections": [
                {"instrument_id": item.instrument_id, "code": item.code, "message": item.message}
                for item in plan.rejections
            ],
            "projected_cash": str(plan.projected_cash),
            "turnover_ratio": str(plan.turnover_ratio),
            "ordinary_turnover_ratio": str(plan.ordinary_turnover_ratio),
            "turnover_limit": str(plan.turnover_limit),
            "bootstrap": plan.bootstrap,
            "intent_id": plan.intent_id,
            "intent_sha256": plan.intent_sha256,
            "attempt_id": plan.attempt_id,
            "parent_attempt_id": plan.parent_attempt_id,
            "parent_plan_sha256": plan.parent_plan_sha256,
            "previous_controlled_session": (
                plan.previous_controlled_session.isoformat()
                if plan.previous_controlled_session is not None
                else None
            ),
            "controlled_calendar_sha256": plan.controlled_calendar_sha256,
            "controlled_session_evidence_sha256": (
                plan.controlled_session_evidence_sha256
            ),
            "execution_quote_bundle_sha256": (
                plan.execution_quote_bundle_sha256
            ),
            "portfolio_intent_type": plan.portfolio_intent_type.value,
            "target_gross_exposure": str(plan.target_gross_exposure),
            "feasible_gross_exposure": str(plan.feasible_gross_exposure),
            "realized_gross_exposure": (
                str(plan.realized_gross_exposure)
                if plan.realized_gross_exposure is not None
                else None
            ),
            "blocked_exit_reasons": [
                {"instrument_id": item.instrument_id, "code": item.code, "message": item.message}
                for item in plan.blocked_exit_reasons
            ],
            "bound_portfolio_intent": (
                plan.bound_portfolio_intent.to_dict()
                if plan.bound_portfolio_intent is not None
                else None
            ),
        }
    if plan.execution_rule_bundle_sha256:
        payload["execution_rule_bundle_sha256"] = (
            plan.execution_rule_bundle_sha256
        )
    return _digest(payload)
