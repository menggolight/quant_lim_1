"""Cost-aware whole-lot rebalance planner for a small strategy ledger."""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR
from typing import Mapping

from trading.costs import FeeSchedule, money
from trading.integrity import account_fingerprint
from trading.models import (
    AccountSnapshot,
    InstrumentRule,
    MarketQuote,
    OrderIntent,
    PlanRejection,
    Position,
    RebalancePlan,
    Side,
)
from trading.risk import RiskLimits


ZERO = Decimal("0")


def _whole_lots(raw_quantity: Decimal, lot_size: int) -> int:
    lots = (raw_quantity / lot_size).to_integral_value(rounding=ROUND_FLOOR)
    return int(lots) * lot_size


def _order_id(
    strategy_id: str,
    decision_id: str,
    instrument_id: str,
    side: Side,
) -> str:
    raw = "|".join((strategy_id, decision_id, instrument_id, side.value))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _fresh_quote(
    instrument_id: str,
    quote: MarketQuote | None,
    decision_time: datetime,
    limits: RiskLimits,
    rejections: list[PlanRejection],
) -> bool:
    if quote is None:
        rejections.append(PlanRejection(instrument_id, "quote_missing", "缺少可复核行情"))
        return False
    age = (decision_time - quote.as_of).total_seconds()
    if age < 0:
        rejections.append(PlanRejection(instrument_id, "quote_from_future", "行情时间晚于决策时间"))
        return False
    if age > limits.maximum_quote_age_seconds:
        rejections.append(PlanRejection(instrument_id, "quote_stale", f"行情已过期 {age:.0f} 秒"))
        return False
    if quote.suspended:
        rejections.append(PlanRejection(instrument_id, "instrument_suspended", "标的处于停牌状态"))
        return False
    spread_ratio = (quote.ask - quote.bid) / quote.last
    if spread_ratio > limits.maximum_spread_ratio:
        rejections.append(PlanRejection(instrument_id, "spread_too_wide", "买卖价差超过策略上限"))
        return False
    return True


def _strategy_equity(
    account: AccountSnapshot,
    quotes: Mapping[str, MarketQuote],
) -> Decimal:
    equity = account.cash
    for instrument_id, position in account.positions.items():
        quote = quotes.get(instrument_id)
        if quote is None:
            raise ValueError(f"Missing quote for managed position {instrument_id}")
        equity += quote.last * position.quantity
    return money(equity)


def build_rebalance_plan(
    account: AccountSnapshot,
    target_weights: Mapping[str, Decimal],
    instruments: Mapping[str, InstrumentRule],
    quotes: Mapping[str, MarketQuote],
    fees: FeeSchedule,
    limits: RiskLimits,
    decision_time: datetime,
    bootstrap: bool = False,
    decision_id: str | None = None,
) -> RebalancePlan:
    if not decision_id or not decision_id.strip():
        raise ValueError("A stable decision_id is required")
    if type(bootstrap) is not bool:
        raise ValueError("bootstrap must be a boolean")
    for key, rule in instruments.items():
        if key != rule.instrument_id:
            raise ValueError(f"Instrument mapping key mismatch: {key} != {rule.instrument_id}")
    for key, quote in quotes.items():
        if key != quote.instrument_id:
            raise ValueError(f"Quote mapping key mismatch: {key} != {quote.instrument_id}")
    rejections: list[PlanRejection] = []
    orders: list[OrderIntent] = []
    for instrument_id in account.positions:
        rule = instruments.get(instrument_id)
        quote = quotes.get(instrument_id)
        if rule is None:
            raise ValueError(f"Strategy account instrument rule missing: {instrument_id}")
        if rule.instrument_type not in limits.allowed_instrument_types:
            raise ValueError(f"Strategy account contains non-strategy instrument: {instrument_id}")
        if limits.allowed_instrument_ids and instrument_id not in limits.allowed_instrument_ids:
            raise ValueError(f"Strategy account contains non-whitelisted instrument: {instrument_id}")
        if quote is None:
            raise ValueError(f"Missing quote for managed position {instrument_id}")
        age = (decision_time - quote.as_of).total_seconds()
        if age < 0 or age > limits.maximum_quote_age_seconds or quote.suspended:
            raise ValueError(f"Managed-position quote is not safely usable: {instrument_id}")
    equity = _strategy_equity(account, quotes)
    if equity <= 0:
        raise ValueError("Strategy equity must be positive")
    if equity > limits.strategy_capital_limit:
        raise ValueError("Strategy equity exceeds the explicit capital limit")

    normalized_targets = {key: Decimal(str(value)) for key, value in target_weights.items()}
    positive_targets = [key for key, value in normalized_targets.items() if value > 0]
    if len(positive_targets) > limits.max_positions:
        raise ValueError(f"Positive targets exceed max_positions={limits.max_positions}")
    if sum(normalized_targets.values(), ZERO) > Decimal("1") - limits.cash_reserve_ratio:
        raise ValueError("Target weights exceed investable weight after cash reserve")
    if any(value < 0 for value in normalized_targets.values()):
        raise ValueError("Target weights must not be negative")

    projected_cash = account.cash
    projected_positions = dict(account.positions)
    reserve_cash = money(equity * limits.cash_reserve_ratio)
    max_order_notional = equity * limits.max_order_notional_ratio

    # Sell managed positions first. Unmanaged long-term holdings never enter this ledger.
    for instrument_id, position in sorted(account.positions.items()):
        rule = instruments.get(instrument_id)
        quote = quotes.get(instrument_id)
        if rule is None:
            rejections.append(PlanRejection(instrument_id, "instrument_rule_missing", "策略持仓缺少交易规则"))
            continue
        if rule.instrument_type not in limits.allowed_instrument_types:
            rejections.append(
                PlanRejection(instrument_id, "instrument_type_not_allowed", f"不允许自动交易 {rule.instrument_type}")
            )
            continue
        if limits.allowed_instrument_ids and instrument_id not in limits.allowed_instrument_ids:
            rejections.append(PlanRejection(instrument_id, "instrument_not_whitelisted", "标的不在冻结白名单"))
            continue
        if not _fresh_quote(instrument_id, quote, decision_time, limits, rejections):
            continue
        assert quote is not None
        target_weight = normalized_targets.get(instrument_id, ZERO)
        desired_value = equity * target_weight
        current_value = quote.last * position.quantity
        if current_value - desired_value < limits.minimum_trade_notional:
            continue
        desired_quantity = _whole_lots(desired_value / quote.last, rule.lot_size)
        required_quantity = max(0, position.quantity - desired_quantity)
        sellable_quantity = min(required_quantity, position.sellable_quantity)
        if sellable_quantity <= 0:
            rejections.append(
                PlanRejection(instrument_id, "insufficient_sellable_quantity", "T+1或冻结数量导致当前不可卖")
            )
            continue
        if sellable_quantity != position.quantity:
            sellable_quantity = _whole_lots(Decimal(sellable_quantity), rule.lot_size)
        if sellable_quantity <= 0:
            rejections.append(
                PlanRejection(instrument_id, "insufficient_sellable_quantity", "不足一个可卖交易单位")
            )
            continue
        if quote.sell_blocked:
            rejections.append(PlanRejection(instrument_id, "sell_blocked", "卖出方向当前不可申报"))
            continue
        notional = quote.bid * sellable_quantity
        if notional < limits.minimum_trade_notional:
            rejections.append(PlanRejection(instrument_id, "below_min_trade_notional", "卖出金额低于费用过滤阈值"))
            continue
        estimated_fee = fees.estimate(Side.SELL, notional, rule)
        order = OrderIntent(
            client_order_id=_order_id(
                account.strategy_id, decision_id, instrument_id, Side.SELL
            ),
            instrument_id=instrument_id,
            side=Side.SELL,
            quantity=sellable_quantity,
            limit_price=quote.bid,
            estimated_fee=estimated_fee,
            reason=f"目标权重降至 {target_weight:.2%}",
        )
        orders.append(order)
        projected_cash = money(projected_cash + notional - estimated_fee)
        remaining = position.quantity - sellable_quantity
        if remaining:
            projected_positions[instrument_id] = Position(
                instrument_id,
                quantity=remaining,
                sellable_quantity=max(0, position.sellable_quantity - sellable_quantity),
            )
        else:
            projected_positions.pop(instrument_id, None)

    # Buy the largest underweights first so scarce cash is allocated deterministically.
    buy_candidates: list[tuple[Decimal, str]] = []
    for instrument_id, target_weight in normalized_targets.items():
        if target_weight <= 0:
            continue
        rule = instruments.get(instrument_id)
        quote = quotes.get(instrument_id)
        if rule is None:
            rejections.append(PlanRejection(instrument_id, "instrument_rule_missing", "缺少交易规则"))
            continue
        if rule.instrument_type not in limits.allowed_instrument_types:
            rejections.append(
                PlanRejection(instrument_id, "instrument_type_not_allowed", f"不允许自动交易 {rule.instrument_type}")
            )
            continue
        if limits.allowed_instrument_ids and instrument_id not in limits.allowed_instrument_ids:
            rejections.append(PlanRejection(instrument_id, "instrument_not_whitelisted", "标的不在冻结白名单"))
            continue
        if target_weight > limits.max_position_weight:
            rejections.append(PlanRejection(instrument_id, "position_weight_limit", "目标权重超过单标的上限"))
            continue
        if not _fresh_quote(instrument_id, quote, decision_time, limits, rejections):
            continue
        assert quote is not None
        current_position = projected_positions.get(instrument_id, Position(instrument_id, 0, 0))
        delta = equity * target_weight - quote.last * current_position.quantity
        if delta >= limits.minimum_trade_notional:
            buy_candidates.append((delta, instrument_id))

    buy_candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    for delta, instrument_id in buy_candidates:
        if len(orders) >= limits.max_orders_per_plan:
            rejections.append(PlanRejection(instrument_id, "order_count_limit", "订单数量达到本次上限"))
            continue
        rule = instruments[instrument_id]
        quote = quotes[instrument_id]
        if instrument_id not in projected_positions and len(projected_positions) >= limits.max_positions:
            rejections.append(PlanRejection(instrument_id, "max_position_count", "现有持仓导致无法新增标的"))
            continue
        if quote.buy_blocked:
            rejections.append(PlanRejection(instrument_id, "buy_blocked", "买入方向当前不可申报"))
            continue
        available_cash = max(ZERO, projected_cash - reserve_cash)
        budget = min(delta, max_order_notional, available_cash)
        quantity = _whole_lots(budget / quote.ask, rule.lot_size)
        while quantity > 0:
            notional = quote.ask * quantity
            estimated_fee = fees.estimate(Side.BUY, notional, rule)
            if notional + estimated_fee <= available_cash:
                break
            quantity -= rule.lot_size
        if quantity <= 0:
            rejections.append(
                PlanRejection(
                    instrument_id,
                    "minimum_lot_unaffordable",
                    f"一手需 {money(quote.ask * rule.lot_size)} 元，目标或现金不足",
                )
            )
            continue
        notional = quote.ask * quantity
        if notional < limits.minimum_trade_notional:
            rejections.append(PlanRejection(instrument_id, "below_min_trade_notional", "买入金额低于费用过滤阈值"))
            continue
        estimated_fee = fees.estimate(Side.BUY, notional, rule)
        order = OrderIntent(
            client_order_id=_order_id(account.strategy_id, decision_id, instrument_id, Side.BUY),
            instrument_id=instrument_id,
            side=Side.BUY,
            quantity=quantity,
            limit_price=quote.ask,
            estimated_fee=estimated_fee,
            reason=f"补足目标权重 {normalized_targets[instrument_id]:.2%}",
        )
        orders.append(order)
        projected_cash = money(projected_cash - notional - estimated_fee)
        old = projected_positions.get(instrument_id, Position(instrument_id, 0, 0))
        newly_sellable = quantity if not rule.t_plus_one else 0
        projected_positions[instrument_id] = Position(
            instrument_id,
            quantity=old.quantity + quantity,
            sellable_quantity=old.sellable_quantity + newly_sellable,
        )

    turnover = sum((order.notional for order in orders), ZERO)
    turnover_ratio = (turnover / equity).quantize(Decimal("0.000001"))
    turnover_limit = limits.bootstrap_turnover_ratio if bootstrap else limits.max_daily_turnover_ratio
    if turnover_ratio > turnover_limit:
        rejections.append(PlanRejection("*", "turnover_limit_exceeded", "计划换手率超过当日上限"))

    bound_account = account_fingerprint(account)
    plan_seed = "|".join(
        [account.strategy_id, decision_id, bound_account, *(order.client_order_id for order in orders)]
    )
    plan_id = hashlib.sha256(plan_seed.encode("utf-8")).hexdigest()[:24]
    return RebalancePlan(
        plan_id=plan_id,
        decision_id=decision_id,
        account_fingerprint=bound_account,
        strategy_id=account.strategy_id,
        decision_time=decision_time,
        strategy_equity=equity,
        orders=tuple(orders),
        rejections=tuple(rejections),
        projected_cash=money(projected_cash),
        turnover_ratio=turnover_ratio,
        turnover_limit=turnover_limit,
        bootstrap=bootstrap,
    )
