"""Cost-aware whole-lot rebalance planner for a small strategy ledger."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, ROUND_FLOOR
from typing import Mapping

from trading.costs import FeeSchedule, money
from trading.integrity import (
    account_fingerprint,
    adaptive_v2_client_order_id,
    adaptive_v2_rebalance_plan_id,
    canonical_controlled_calendar_sha256,
    controlled_session_evidence_sha256,
    controlled_sessions_are_adjacent,
    execution_quote_bundle_sha256,
    execution_rule_bundle_sha256,
    is_lower_sha256,
    legacy_client_order_id,
    legacy_rebalance_plan_id,
    plan_fingerprint,
)
from trading.models import (
    ADAPTIVE_EXPOSURE_V2_MAX_POSITIONS,
    ADAPTIVE_EXPOSURE_V2_MAX_POSITION_WEIGHT,
    ADAPTIVE_EXPOSURE_V2_STRATEGY_ID,
    NO_BUY_INTENT_TYPES,
    RISK_REDUCTION_INTENT_TYPES,
    AccountSnapshot,
    InstrumentRule,
    MarketQuote,
    OrderIntent,
    OrderRiskDirection,
    PlanRejection,
    PortfolioIntent,
    PortfolioIntentType,
    Position,
    RebalancePlan,
    Side,
)
from trading.risk import RiskLimits


ZERO = Decimal("0")


def _whole_lots(raw_quantity: Decimal, lot_size: int) -> int:
    lots = (raw_quantity / lot_size).to_integral_value(rounding=ROUND_FLOOR)
    return int(lots) * lot_size


def _execution_session(value: datetime, reference: datetime) -> date:
    return value.astimezone(reference.tzinfo).date()


def _validate_parent_attempt(
    parent: RebalancePlan,
    portfolio_intent: PortfolioIntent,
    decision_time: datetime,
) -> None:
    if parent.bound_portfolio_intent is None:
        raise ValueError("V2 retry parent attempt must bind its PortfolioIntent")
    if (
        parent.strategy_id != portfolio_intent.strategy_id
        or parent.intent_id != portfolio_intent.intent_id
        or parent.intent_sha256 != portfolio_intent.intent_sha256
        or parent.portfolio_intent_type is not portfolio_intent.intent_type
    ):
        raise ValueError("V2 retry parent attempt does not share the frozen intent")
    if parent.attempt_id != parent.decision_id or not parent.attempt_id:
        raise ValueError("V2 retry parent attempt binding is invalid")
    expected_parent_id = adaptive_v2_rebalance_plan_id(
        parent.strategy_id,
        parent.intent_id,
        parent.intent_sha256,
        parent.attempt_id,
        parent.parent_attempt_id,
        parent.parent_plan_sha256,
        parent.account_fingerprint,
        (order.client_order_id for order in parent.orders),
        parent.controlled_session_evidence_sha256,
        execution_quote_bundle_sha256=(
            parent.execution_quote_bundle_sha256
        ),
        execution_rule_bundle_sha256=parent.execution_rule_bundle_sha256,
    )
    if parent.plan_id != expected_parent_id:
        raise ValueError("V2 retry parent attempt plan binding is invalid")
    if parent.decision_time >= decision_time:
        raise ValueError("V2 retry parent attempt must precede the new attempt")
    if (
        not parent.blocked_exit_reasons
        and parent.feasible_gross_exposure <= parent.target_gross_exposure
    ):
        raise ValueError("V2 retry parent attempt has no residual exit exposure")


def _fresh_quote(
    instrument_id: str,
    quote: MarketQuote | None,
    decision_time: datetime,
    limits: RiskLimits,
    rejections: list[PlanRejection],
    blocked_exit_reasons: list[PlanRejection] | None = None,
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
        target = blocked_exit_reasons if blocked_exit_reasons is not None else rejections
        target.append(PlanRejection(instrument_id, "sell_blocked_suspended", "标的处于停牌状态"))
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
    portfolio_intent: PortfolioIntent | None = None,
    parent_attempt_id: str | None = None,
    parent_attempt: RebalancePlan | None = None,
    previous_controlled_session: date | None = None,
    controlled_calendar_sha256: str = "",
    controlled_calendar_sessions: tuple[date, ...] | None = None,
) -> RebalancePlan:
    if not decision_id or not decision_id.strip():
        raise ValueError("A stable decision_id is required")
    decision_id = decision_id.strip()
    is_v2 = portfolio_intent is not None
    attempt_id = decision_id if is_v2 else ""
    parent_plan_sha256 = ""
    session_evidence_sha256 = ""
    execution_quotes_sha256 = ""
    execution_rules_sha256 = ""
    if type(bootstrap) is not bool:
        raise ValueError("bootstrap must be a boolean")
    if portfolio_intent is not None and not isinstance(portfolio_intent, PortfolioIntent):
        raise ValueError("portfolio_intent must be a PortfolioIntent")
    if portfolio_intent is not None:
        if portfolio_intent.strategy_id != account.strategy_id:
            raise ValueError("Portfolio intent strategy_id does not match account")
        if portfolio_intent.decision_at > decision_time:
            raise ValueError("Portfolio intent decision is from the future")
        intent_session = portfolio_intent.decision_at.date()
        execution_session = _execution_session(
            decision_time, portfolio_intent.decision_at
        )
        cross_session = execution_session != intent_session
        if (
            cross_session
            and portfolio_intent.intent_type not in RISK_REDUCTION_INTENT_TYPES
        ):
            raise ValueError(
                "Ordinary alpha intent must remain in the same execution session"
            )
        if parent_attempt is not None:
            if not isinstance(parent_attempt, RebalancePlan):
                raise ValueError("parent_attempt must be a RebalancePlan")
            _validate_parent_attempt(parent_attempt, portfolio_intent, decision_time)
            if parent_attempt_id is not None and (
                parent_attempt_id != parent_attempt.attempt_id
            ):
                raise ValueError("parent_attempt_id does not match parent attempt")
            parent_attempt_id = parent_attempt.attempt_id
            parent_plan_sha256 = plan_fingerprint(parent_attempt)
        elif parent_attempt_id is not None:
            raise ValueError("V2 retry parent attempt object is required")
        if cross_session:
            if (
                previous_controlled_session is None
                or type(previous_controlled_session) is not date
                or previous_controlled_session >= execution_session
            ):
                raise ValueError(
                    "Cross-session attempt requires valid previous controlled session evidence"
                )
            if not is_lower_sha256(controlled_calendar_sha256):
                raise ValueError(
                    "Cross-session attempt requires controlled calendar SHA-256"
                )
            if controlled_calendar_sessions is None:
                raise ValueError(
                    "Cross-session attempt requires controlled calendar session payload"
                )
            try:
                recomputed_calendar_sha256 = (
                    canonical_controlled_calendar_sha256(
                        controlled_calendar_sessions
                    )
                )
            except ValueError as error:
                raise ValueError(
                    f"Invalid controlled calendar session payload: {error}"
                ) from error
            if recomputed_calendar_sha256 != controlled_calendar_sha256:
                raise ValueError(
                    "Controlled calendar canonical hash does not match the supplied payload"
                )
            if not controlled_sessions_are_adjacent(
                controlled_calendar_sessions,
                previous_controlled_session,
                execution_session,
            ):
                raise ValueError(
                    "Previous and execution sessions must be strictly adjacent in the controlled calendar payload"
                )
            if parent_attempt is not None and (
                _execution_session(
                    parent_attempt.decision_time, portfolio_intent.decision_at
                )
                >= execution_session
            ):
                raise ValueError(
                    "Cross-session retry parent attempt must be from a prior session"
                )
            if parent_attempt is not None and previous_controlled_session != (
                _execution_session(
                    parent_attempt.decision_time, portfolio_intent.decision_at
                )
            ):
                raise ValueError(
                    "Previous controlled session does not match parent attempt session"
                )
            if parent_attempt is None and previous_controlled_session != intent_session:
                raise ValueError(
                    "First cross-session execution must bind the intent controlled session"
                )
            if (
                not account.snapshot_id
                or account.as_of is None
                or account.as_of > decision_time
                or _execution_session(
                    account.as_of, portfolio_intent.decision_at
                )
                != execution_session
            ):
                raise ValueError(
                    "Cross-session retry requires a current reconciled account snapshot"
                )
            session_evidence_sha256 = controlled_session_evidence_sha256(
                portfolio_intent.strategy_id,
                portfolio_intent.intent_id,
                portfolio_intent.intent_sha256,
                previous_controlled_session,
                execution_session,
                controlled_calendar_sha256,
            )
        elif (
            previous_controlled_session is not None
            or controlled_calendar_sha256
            or controlled_calendar_sessions is not None
        ):
            raise ValueError(
                "Same-session attempt must not claim cross-session calendar evidence"
            )
        intent_id = portfolio_intent.intent_id
        intent_sha256 = portfolio_intent.intent_sha256
        intent_type = portfolio_intent.intent_type
        target_gross_exposure = portfolio_intent.target_gross_exposure
    else:
        if parent_attempt_id is not None or parent_attempt is not None:
            raise ValueError("Legacy V1 plans do not support V2 attempt lineage")
        if (
            previous_controlled_session is not None
            or controlled_calendar_sha256
            or controlled_calendar_sessions is not None
        ):
            raise ValueError(
                "Legacy V1 plans do not support controlled session evidence"
            )
        intent_id = ""
        intent_sha256 = ""
        intent_type = PortfolioIntentType.ALPHA_REBALANCE
        target_gross_exposure = sum(
            (Decimal(str(value)) for value in target_weights.values()), ZERO
        )
    reduction_intent = intent_type in RISK_REDUCTION_INTENT_TYPES

    def client_order_id(instrument_id: str, side: Side) -> str:
        if is_v2:
            return adaptive_v2_client_order_id(
                account.strategy_id,
                intent_id,
                attempt_id,
                instrument_id,
                side,
            )
        return legacy_client_order_id(
            account.strategy_id,
            decision_id,
            instrument_id,
            side,
        )
    for key, rule in instruments.items():
        if key != rule.instrument_id:
            raise ValueError(f"Instrument mapping key mismatch: {key} != {rule.instrument_id}")
    for key, quote in quotes.items():
        if key != quote.instrument_id:
            raise ValueError(f"Quote mapping key mismatch: {key} != {quote.instrument_id}")
    if is_v2:
        execution_quotes_sha256 = execution_quote_bundle_sha256(quotes)
        execution_rules_sha256 = execution_rule_bundle_sha256(fees, instruments)
    rejections: list[PlanRejection] = []
    blocked_exit_reasons: list[PlanRejection] = []
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
        if age < 0 or age > limits.maximum_quote_age_seconds or (
            quote.suspended and not reduction_intent
        ):
            raise ValueError(f"Managed-position quote is not safely usable: {instrument_id}")
    equity = _strategy_equity(account, quotes)
    if equity <= 0:
        raise ValueError("Strategy equity must be positive")
    if equity > limits.strategy_capital_limit:
        raise ValueError("Strategy equity exceeds the explicit capital limit")

    normalized_targets = {key: Decimal(str(value)) for key, value in target_weights.items()}
    if portfolio_intent is not None and normalized_targets != dict(portfolio_intent.target_weights):
        raise ValueError("Target weights do not match the bound PortfolioIntent")
    if not normalized_targets and portfolio_intent is None:
        raise ValueError("Ordinary rebalance targets must not be empty")
    if (
        account.strategy_id == ADAPTIVE_EXPOSURE_V2_STRATEGY_ID
        and portfolio_intent is None
    ):
        raise ValueError("Adaptive Exposure V2 requires a bound PortfolioIntent")
    if sum(normalized_targets.values(), ZERO) > target_gross_exposure:
        raise ValueError("Target weights exceed target_gross_exposure")
    positive_targets = [key for key, value in normalized_targets.items() if value > 0]
    runtime_max_positions = limits.max_positions
    runtime_max_position_weight = limits.max_position_weight
    if account.strategy_id == ADAPTIVE_EXPOSURE_V2_STRATEGY_ID:
        runtime_max_positions = min(
            runtime_max_positions, ADAPTIVE_EXPOSURE_V2_MAX_POSITIONS
        )
        runtime_max_position_weight = min(
            runtime_max_position_weight,
            ADAPTIVE_EXPOSURE_V2_MAX_POSITION_WEIGHT,
        )
    if len(positive_targets) > runtime_max_positions:
        raise ValueError(
            f"Positive targets exceed max_positions={runtime_max_positions}"
        )
    if account.strategy_id == ADAPTIVE_EXPOSURE_V2_STRATEGY_ID and any(
        normalized_targets[instrument_id] > runtime_max_position_weight
        for instrument_id in positive_targets
    ):
        raise ValueError(
            f"Target weight exceeds max_position_weight={runtime_max_position_weight}"
        )
    if sum(normalized_targets.values(), ZERO) > Decimal("1") - limits.cash_reserve_ratio:
        raise ValueError("Target weights exceed investable weight after cash reserve")
    if any(value < 0 for value in normalized_targets.values()):
        raise ValueError("Target weights must not be negative")
    if intent_type is PortfolioIntentType.DEFENSIVE_REDUCTION:
        for instrument_id, target_weight in normalized_targets.items():
            position = account.positions.get(instrument_id)
            quote = quotes.get(instrument_id)
            if position is None or quote is None:
                raise ValueError("Defensive reduction cannot add a new position")
            current_weight = quote.last * position.quantity / equity
            if target_weight > current_weight:
                raise ValueError("Defensive reduction cannot increase a position")
    if intent_type in {
        PortfolioIntentType.DATA_FAIL_CLOSED,
        PortfolioIntentType.MANUAL_PAUSE,
    }:
        for instrument_id, target_weight in normalized_targets.items():
            position = account.positions.get(instrument_id)
            quote = quotes.get(instrument_id)
            if position is None or quote is None:
                raise ValueError(f"{intent_type.value} cannot add a new position")
            current_weight = quote.last * position.quantity / equity
            if target_weight > current_weight:
                raise ValueError(f"{intent_type.value} cannot increase a position")

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
        exit_blocks = blocked_exit_reasons if reduction_intent else None
        if not _fresh_quote(
            instrument_id,
            quote,
            decision_time,
            limits,
            rejections,
            exit_blocks,
        ):
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
            target = blocked_exit_reasons if reduction_intent else rejections
            code = (
                "sell_blocked_t_plus_one"
                if reduction_intent and rule.t_plus_one
                else "insufficient_sellable_quantity"
            )
            target.append(
                PlanRejection(instrument_id, code, "T+1或冻结数量导致当前不可卖")
            )
            continue
        if sellable_quantity != position.quantity:
            sellable_quantity = _whole_lots(Decimal(sellable_quantity), rule.lot_size)
        if sellable_quantity <= 0:
            target = blocked_exit_reasons if reduction_intent else rejections
            target.append(
                PlanRejection(instrument_id, "insufficient_sellable_quantity", "不足一个可卖交易单位")
            )
            continue
        if quote.sell_blocked:
            target = blocked_exit_reasons if reduction_intent else rejections
            target.append(PlanRejection(instrument_id, "sell_blocked_limit_down", "卖出方向当前不可申报"))
            continue
        notional = quote.bid * sellable_quantity
        if notional < limits.minimum_trade_notional and not reduction_intent:
            rejections.append(PlanRejection(instrument_id, "below_min_trade_notional", "卖出金额低于费用过滤阈值"))
            continue
        estimated_fee = fees.estimate(Side.SELL, notional, rule)
        if intent_type is PortfolioIntentType.ACCOUNT_DRAWDOWN_EXIT:
            risk_direction = OrderRiskDirection.FORCED_EXIT
        elif reduction_intent:
            risk_direction = OrderRiskDirection.RISK_REDUCING
        else:
            risk_direction = OrderRiskDirection.RISK_NEUTRAL
        order = OrderIntent(
            client_order_id=client_order_id(instrument_id, Side.SELL),
            instrument_id=instrument_id,
            side=Side.SELL,
            quantity=sellable_quantity,
            limit_price=quote.bid,
            estimated_fee=estimated_fee,
            reason=f"目标权重降至 {target_weight:.2%}",
            risk_direction=risk_direction,
            intent_id=intent_id if is_v2 else "",
            attempt_id=attempt_id if is_v2 else "",
        )
        orders.append(order)
        projected_cash = money(projected_cash + notional - estimated_fee)
        if reduction_intent and sellable_quantity < required_quantity:
            blocked_exit_reasons.append(
                PlanRejection(
                    instrument_id,
                    (
                        "sell_blocked_t_plus_one"
                        if rule.t_plus_one
                        else "insufficient_sellable_quantity"
                    ),
                    "部分可卖数量已生成卖单，剩余目标减仓数量当前不可卖",
                )
            )
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
        if target_weight > runtime_max_position_weight:
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
        if intent_type in NO_BUY_INTENT_TYPES:
            rejections.append(
                PlanRejection(
                    instrument_id,
                    "no_buy_intent",
                    f"{intent_type.value} 不允许增加风险或生成买单",
                )
            )
            continue
        if len(orders) >= limits.max_orders_per_plan:
            rejections.append(PlanRejection(instrument_id, "order_count_limit", "订单数量达到本次上限"))
            continue
        rule = instruments[instrument_id]
        quote = quotes[instrument_id]
        if (
            instrument_id not in projected_positions
            and len(projected_positions) >= runtime_max_positions
        ):
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
            client_order_id=client_order_id(instrument_id, Side.BUY),
            instrument_id=instrument_id,
            side=Side.BUY,
            quantity=quantity,
            limit_price=quote.ask,
            estimated_fee=estimated_fee,
            reason=f"补足目标权重 {normalized_targets[instrument_id]:.2%}",
            risk_direction=(
                OrderRiskDirection.RISK_INCREASING
                if is_v2
                else OrderRiskDirection.RISK_NEUTRAL
            ),
            intent_id=intent_id if is_v2 else "",
            attempt_id=attempt_id if is_v2 else "",
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
    ordinary_turnover = sum(
        (
            order.notional
            for order in orders
            if order.risk_direction
            not in {OrderRiskDirection.RISK_REDUCING, OrderRiskDirection.FORCED_EXIT}
        ),
        ZERO,
    )
    turnover_ratio = (turnover / equity).quantize(Decimal("0.000001"))
    ordinary_turnover_ratio = (ordinary_turnover / equity).quantize(Decimal("0.000001"))
    turnover_limit = limits.bootstrap_turnover_ratio if bootstrap else limits.max_daily_turnover_ratio
    if ordinary_turnover_ratio > turnover_limit:
        rejections.append(PlanRejection("*", "turnover_limit_exceeded", "计划换手率超过当日上限"))

    projected_positions_value = sum(
        (
            quotes[instrument_id].last * position.quantity
            for instrument_id, position in projected_positions.items()
        ),
        ZERO,
    )
    projected_nav = projected_cash + projected_positions_value
    feasible_gross_exposure = (
        (projected_positions_value / projected_nav).quantize(Decimal("0.000001"))
        if projected_nav > ZERO
        else ZERO
    )
    if is_v2 and projected_nav > ZERO:
        for instrument_id, position in sorted(projected_positions.items()):
            projected_weight = (
                quotes[instrument_id].last * position.quantity / projected_nav
            )
            if (
                intent_type is PortfolioIntentType.ALPHA_REBALANCE
                and account.strategy_id == ADAPTIVE_EXPOSURE_V2_STRATEGY_ID
                and projected_weight > ADAPTIVE_EXPOSURE_V2_MAX_POSITION_WEIGHT
            ):
                rejections.append(
                    PlanRejection(
                        instrument_id,
                        "position_weight_limit_after_fees",
                        "费用后计划净值口径的单标的权重超过40%硬上限",
                    )
                )
            if (
                intent_type is PortfolioIntentType.ALPHA_REBALANCE
                and portfolio_intent is not None
                and projected_weight
                > portfolio_intent.target_weights.get(instrument_id, ZERO)
            ):
                rejections.append(
                    PlanRejection(
                        instrument_id,
                        "intent_target_exceeded_after_fees",
                        "费用后计划净值口径的单标的权重超过冻结意图目标",
                    )
                )

    bound_account = account_fingerprint(account)
    if is_v2:
        plan_id = adaptive_v2_rebalance_plan_id(
            account.strategy_id,
            intent_id,
            intent_sha256,
            attempt_id,
            parent_attempt_id,
            parent_plan_sha256,
            bound_account,
            (order.client_order_id for order in orders),
            session_evidence_sha256,
            execution_quote_bundle_sha256=execution_quotes_sha256,
            execution_rule_bundle_sha256=execution_rules_sha256,
        )
    else:
        plan_id = legacy_rebalance_plan_id(
            account.strategy_id,
            decision_id,
            bound_account,
            (order.client_order_id for order in orders),
        )
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
        intent_id=intent_id if is_v2 else "",
        intent_sha256=intent_sha256,
        attempt_id=attempt_id if is_v2 else "",
        parent_attempt_id=parent_attempt_id if is_v2 else None,
        portfolio_intent_type=intent_type,
        target_gross_exposure=target_gross_exposure,
        feasible_gross_exposure=feasible_gross_exposure,
        realized_gross_exposure=None,
        ordinary_turnover_ratio=ordinary_turnover_ratio,
        blocked_exit_reasons=tuple(blocked_exit_reasons),
        parent_plan_sha256=parent_plan_sha256,
        bound_portfolio_intent=portfolio_intent,
        previous_controlled_session=previous_controlled_session,
        controlled_calendar_sha256=controlled_calendar_sha256,
        controlled_session_evidence_sha256=session_evidence_sha256,
        execution_quote_bundle_sha256=execution_quotes_sha256,
        execution_rule_bundle_sha256=execution_rules_sha256,
    )


def build_rebalance_plan_from_intent(
    account: AccountSnapshot,
    portfolio_intent: PortfolioIntent,
    instruments: Mapping[str, InstrumentRule],
    quotes: Mapping[str, MarketQuote],
    fees: FeeSchedule,
    limits: RiskLimits,
    decision_time: datetime,
    *,
    attempt_id: str,
    bootstrap: bool = False,
    parent_attempt_id: str | None = None,
    parent_attempt: RebalancePlan | None = None,
    previous_controlled_session: date | None = None,
    controlled_calendar_sha256: str = "",
    controlled_calendar_sessions: tuple[date, ...] | None = None,
) -> RebalancePlan:
    """Build one replay-safe execution attempt for a bound portfolio intent."""

    return build_rebalance_plan(
        account=account,
        target_weights=portfolio_intent.target_weights,
        instruments=instruments,
        quotes=quotes,
        fees=fees,
        limits=limits,
        decision_time=decision_time,
        bootstrap=bootstrap,
        decision_id=attempt_id,
        portfolio_intent=portfolio_intent,
        parent_attempt_id=parent_attempt_id,
        parent_attempt=parent_attempt,
        previous_controlled_session=previous_controlled_session,
        controlled_calendar_sha256=controlled_calendar_sha256,
        controlled_calendar_sessions=controlled_calendar_sessions,
    )


def execution_plan_record(plan: RebalancePlan) -> dict[str, object]:
    """Serialize a V2 planned attempt without inventing realized execution fields."""

    if not plan.intent_sha256:
        raise ValueError("A V2 execution record requires a bound intent SHA-256")
    if not is_lower_sha256(plan.execution_quote_bundle_sha256):
        raise ValueError(
            "A V2 execution record requires an execution quote bundle SHA-256"
        )
    if not is_lower_sha256(plan.execution_rule_bundle_sha256):
        raise ValueError(
            "A V2 execution record requires an execution rule bundle SHA-256"
        )
    execution_session = (
        _execution_session(
            plan.decision_time,
            plan.bound_portfolio_intent.decision_at,
        )
        if plan.bound_portfolio_intent is not None
        else plan.decision_time.date()
    )
    risk_reduction_turnover = plan.turnover_ratio - plan.ordinary_turnover_ratio
    return {
        "schema_version": "portfolio-execution-plan.v2",
        "plan_id": plan.plan_id,
        "strategy_id": plan.strategy_id,
        "intent_id": plan.intent_id,
        "intent_type": plan.portfolio_intent_type.value,
        "intent_sha256": plan.intent_sha256,
        "attempt_id": plan.attempt_id,
        "parent_attempt_id": plan.parent_attempt_id,
        "previous_controlled_session": (
            plan.previous_controlled_session.isoformat()
            if plan.previous_controlled_session is not None
            else None
        ),
        "controlled_calendar_sha256": (
            plan.controlled_calendar_sha256 or None
        ),
        "controlled_session_evidence_sha256": (
            plan.controlled_session_evidence_sha256 or None
        ),
        "execution_quote_bundle_sha256": plan.execution_quote_bundle_sha256,
        "execution_rule_bundle_sha256": plan.execution_rule_bundle_sha256,
        "official_trading_calendar_proven": False,
        "execution_session": execution_session.isoformat(),
        "planned_at": plan.decision_time.isoformat(),
        "account_snapshot_sha256": plan.account_fingerprint,
        "plan_status": (
            "BLOCKED"
            if plan.rejections or (plan.blocked_exit_reasons and not plan.orders)
            else "PLANNED"
        ),
        "target_gross_exposure": str(plan.target_gross_exposure),
        "feasible_gross_exposure": str(plan.feasible_gross_exposure),
        "realized_gross_exposure": None,
        "projected_cash": str(plan.projected_cash),
        "realized_cash": None,
        "orders": [
            {
                "client_order_id": order.client_order_id,
                "instrument_id": order.instrument_id,
                "side": order.side.value,
                "risk_class": order.risk_direction.value,
                "quantity": order.quantity,
                "limit_price": str(order.limit_price),
                "estimated_fee": str(order.estimated_fee),
                "status": "PLANNED",
                "unfilled_reason": None,
            }
            for order in plan.orders
        ],
        "blocked_exit_reasons": [
            {
                "instrument_id": item.instrument_id,
                "code": item.code,
                "message": item.message,
            }
            for item in plan.blocked_exit_reasons
        ],
        "fatal_rejections": [
            {
                "instrument_id": item.instrument_id,
                "code": item.code,
                "message": item.message,
            }
            for item in plan.rejections
        ],
        "ordinary_turnover_ratio": str(plan.ordinary_turnover_ratio),
        "risk_reduction_turnover_ratio": str(risk_reduction_turnover),
        "ordinary_turnover_limit": str(plan.turnover_limit),
        "risk_reduction_exemption_used": risk_reduction_turnover > ZERO,
        "estimated_total_cost": str(
            sum((order.estimated_fee for order in plan.orders), ZERO)
        ),
        "realized_total_cost": None,
        "plan_sha256": plan_fingerprint(plan),
        "live_supported": False,
    }
