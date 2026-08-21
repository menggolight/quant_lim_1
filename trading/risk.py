"""Pre-trade limits and fail-closed execution gates."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
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
    FULL_EXIT_INTENT_TYPES,
    LIVE_NOT_SUPPORTED_CODE,
    NO_BUY_INTENT_TYPES,
    RISK_REDUCTION_INTENT_TYPES,
    AccountSnapshot,
    ExecutionMode,
    InstrumentRule,
    LiveNotSupportedError,
    MarketQuote,
    OrderRiskDirection,
    PortfolioIntentType,
    RebalancePlan,
    Side,
    is_live_execution_mode,
)


@dataclass(frozen=True)
class RiskLimits:
    strategy_capital_limit: Decimal
    allowed_instrument_types: tuple[str, ...]
    max_positions: int
    max_position_weight: Decimal
    cash_reserve_ratio: Decimal
    minimum_trade_notional: Decimal
    max_orders_per_plan: int
    max_order_notional_ratio: Decimal
    max_daily_turnover_ratio: Decimal
    bootstrap_turnover_ratio: Decimal
    maximum_quote_age_seconds: int
    maximum_daily_loss_ratio: Decimal
    allowed_instrument_ids: tuple[str, ...] = ()
    max_orders_per_day: int | None = None
    maximum_spread_ratio: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        if self.strategy_capital_limit <= 0:
            raise ValueError("strategy_capital_limit must be positive")
        if self.max_positions <= 0 or self.max_orders_per_plan <= 0:
            raise ValueError("position and order limits must be positive")
        if self.max_orders_per_day is not None and self.max_orders_per_day <= 0:
            raise ValueError("max_orders_per_day must be positive")
        for value in (
            self.max_position_weight,
            self.cash_reserve_ratio,
            self.max_order_notional_ratio,
            self.max_daily_turnover_ratio,
            self.bootstrap_turnover_ratio,
            self.maximum_daily_loss_ratio,
            self.maximum_spread_ratio,
        ):
            if value < 0 or value > 1:
                raise ValueError("ratio limits must be between zero and one")


@dataclass(frozen=True)
class LiveReadiness:
    programmatic_report_confirmed: bool = False
    broker_api_authorized: bool = False
    account_fee_schedule_verified: bool = False
    account_reconciled: bool = False
    trading_universe_frozen: bool = False
    paper_started_at: datetime | None = None
    paper_trade_events: int = 0
    shadow_sessions: int = 0

    def __post_init__(self) -> None:
        boolean_fields = (
            "programmatic_report_confirmed",
            "broker_api_authorized",
            "account_fee_schedule_verified",
            "account_reconciled",
            "trading_universe_frozen",
        )
        for field_name in boolean_fields:
            if type(getattr(self, field_name)) is not bool:
                raise ValueError(f"{field_name} must be a boolean")
        for field_name in ("paper_trade_events", "shadow_sessions"):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        if self.paper_started_at is not None and self.paper_started_at.tzinfo is None:
            raise ValueError("paper_started_at must be timezone-aware")


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    block_codes: tuple[str, ...]
    approval: "ExecutionApproval | None" = None


_GATE_ISSUER = object()


@dataclass(frozen=True)
class ExecutionApproval:
    mode: ExecutionMode
    plan_fingerprint: str
    account_fingerprint: str
    issued_at: datetime
    valid_until: datetime
    turnover_limit: Decimal
    max_orders_per_day: int
    _issuer: object = field(repr=False, compare=False)
    execution_rule_bundle_sha256: str = ""

    def __post_init__(self) -> None:
        if is_live_execution_mode(self.mode):
            raise LiveNotSupportedError()

    def was_issued_by_gate(self) -> bool:
        return self._issuer is _GATE_ISSUER


class ExecutionGate:
    MINIMUM_PAPER_CALENDAR_DAYS = 90
    MINIMUM_PAPER_TRADE_EVENTS = 30

    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits

    def evaluate(
        self,
        mode: ExecutionMode,
        plan: RebalancePlan,
        account: AccountSnapshot | None,
        decision_time: datetime,
        daily_pnl_ratio: Decimal,
        kill_switch_active: bool,
        readiness: LiveReadiness,
        daily_turnover_ratio_before_plan: Decimal = Decimal("0"),
        daily_order_count_before_plan: int = 0,
        trusted_quotes: Mapping[str, MarketQuote] | None = None,
        trusted_market_data_sha256: str = "",
        controlled_calendar_sessions: tuple[date, ...] | None = None,
        trusted_fee_schedule: FeeSchedule | None = None,
        trusted_instruments: Mapping[str, InstrumentRule] | None = None,
    ) -> GateResult:
        blocks: list[str] = []
        if is_live_execution_mode(mode):
            return GateResult(False, (LIVE_NOT_SUPPORTED_CODE,), None)
        if not isinstance(mode, ExecutionMode):
            return GateResult(False, ("invalid_execution_mode",), None)
        if not isinstance(readiness, LiveReadiness):
            return GateResult(False, ("invalid_live_readiness",), None)
        if not isinstance(plan, RebalancePlan):
            return GateResult(False, ("invalid_plan",), None)
        if not isinstance(account, AccountSnapshot):
            return GateResult(False, ("account_snapshot_missing",), None)
        if decision_time.tzinfo is None:
            return GateResult(False, ("invalid_decision_time",), None)
        if not isinstance(daily_pnl_ratio, Decimal):
            return GateResult(False, ("invalid_daily_pnl",), None)
        if type(kill_switch_active) is not bool:
            return GateResult(False, ("invalid_kill_switch_state",), None)
        if not isinstance(daily_turnover_ratio_before_plan, Decimal) or daily_turnover_ratio_before_plan < 0:
            blocks.append("invalid_daily_turnover_state")
        if type(daily_order_count_before_plan) is not int or daily_order_count_before_plan < 0:
            blocks.append("invalid_daily_order_count_state")
        if account is None:
            return GateResult(False, ("account_snapshot_missing",), None)
        bound_account = account_fingerprint(account)
        if plan.strategy_id != account.strategy_id:
            blocks.append("strategy_id_mismatch")
        is_v2 = (
            plan.bound_portfolio_intent is not None
            or plan.strategy_id == ADAPTIVE_EXPOSURE_V2_STRATEGY_ID
        )
        intent = plan.bound_portfolio_intent if is_v2 else None
        trusted_quote_map: dict[str, MarketQuote] = {}
        trusted_instrument_map: dict[str, InstrumentRule] = {}
        if is_v2:
            if intent is None:
                blocks.append("bound_portfolio_intent_missing")
            if (
                not plan.intent_id
                or not plan.attempt_id
                or plan.decision_id != plan.attempt_id
            ):
                blocks.append("invalid_intent_attempt_binding")
            if not is_lower_sha256(plan.intent_sha256):
                blocks.append("invalid_intent_sha256")
            if not is_lower_sha256(plan.execution_quote_bundle_sha256):
                blocks.append("execution_quote_bundle_sha256_missing")
            if not is_lower_sha256(plan.execution_rule_bundle_sha256):
                blocks.append("execution_rule_bundle_sha256_missing")
            if intent is not None:
                recomputed_intent_sha256 = intent.intent_sha256
                if plan.intent_sha256 != recomputed_intent_sha256:
                    blocks.append("intent_sha256_mismatch")
                if (
                    not is_lower_sha256(trusted_market_data_sha256)
                    or trusted_market_data_sha256
                    != intent.market_data_sha256
                ):
                    blocks.append("trusted_market_data_binding_missing")
                if (
                    plan.intent_id != intent.intent_id
                    or plan.strategy_id != intent.strategy_id
                    or plan.portfolio_intent_type is not intent.intent_type
                    or plan.target_gross_exposure
                    != intent.target_gross_exposure
                ):
                    blocks.append("portfolio_intent_binding_mismatch")
                if plan.strategy_id == ADAPTIVE_EXPOSURE_V2_STRATEGY_ID:
                    positive_weights = [
                        weight
                        for weight in intent.target_weights.values()
                        if weight > 0
                    ]
                    if (
                        len(positive_weights)
                        > ADAPTIVE_EXPOSURE_V2_MAX_POSITIONS
                    ):
                        blocks.append("adaptive_v2_position_count_exceeded")
                    if any(
                        weight > ADAPTIVE_EXPOSURE_V2_MAX_POSITION_WEIGHT
                        for weight in positive_weights
                    ):
                        blocks.append("adaptive_v2_position_weight_exceeded")
                    if sum(positive_weights, Decimal("0")) > Decimal("1"):
                        blocks.append("adaptive_v2_total_weight_exceeded")
                    if any(
                        weight <= 0 for weight in intent.target_weights.values()
                    ):
                        blocks.append("adaptive_v2_zero_weight_target")
                intent_session = intent.decision_at.date()
                plan_session = plan.decision_time.astimezone(
                    intent.decision_at.tzinfo
                ).date()
                if plan.decision_time < intent.decision_at:
                    blocks.append("plan_precedes_intent")
                if plan_session != intent_session:
                    if intent.intent_type not in RISK_REDUCTION_INTENT_TYPES:
                        blocks.append("alpha_intent_cross_session")
                    if (
                        plan.previous_controlled_session is None
                        or plan.previous_controlled_session >= plan_session
                        or not is_lower_sha256(
                            plan.controlled_calendar_sha256
                        )
                        or not is_lower_sha256(
                            plan.controlled_session_evidence_sha256
                        )
                    ):
                        blocks.append("controlled_session_evidence_missing")
                    else:
                        expected_session_evidence = (
                            controlled_session_evidence_sha256(
                                plan.strategy_id,
                                plan.intent_id,
                                plan.intent_sha256,
                                plan.previous_controlled_session,
                                plan_session,
                                plan.controlled_calendar_sha256,
                            )
                        )
                        if (
                            expected_session_evidence
                            != plan.controlled_session_evidence_sha256
                        ):
                            blocks.append(
                                "controlled_session_evidence_mismatch"
                            )
                    if controlled_calendar_sessions is None:
                        blocks.append("controlled_calendar_payload_missing")
                    else:
                        try:
                            recomputed_calendar_sha256 = (
                                canonical_controlled_calendar_sha256(
                                    controlled_calendar_sessions
                                )
                            )
                        except ValueError:
                            blocks.append("controlled_calendar_payload_invalid")
                        else:
                            if (
                                recomputed_calendar_sha256
                                != plan.controlled_calendar_sha256
                            ):
                                blocks.append(
                                    "controlled_calendar_hash_mismatch"
                                )
                            if (
                                plan.previous_controlled_session is None
                                or not controlled_sessions_are_adjacent(
                                    controlled_calendar_sessions,
                                    plan.previous_controlled_session,
                                    plan_session,
                                )
                            ):
                                blocks.append(
                                    "controlled_session_not_adjacent"
                                )
                    if plan.parent_attempt_id:
                        if not is_lower_sha256(plan.parent_plan_sha256):
                            blocks.append("cross_session_lineage_missing")
                    elif plan.previous_controlled_session == intent_session:
                        pass
                    else:
                        blocks.append("cross_session_lineage_missing")
                    if (
                        not plan.parent_attempt_id
                        and plan.previous_controlled_session != intent_session
                    ):
                        blocks.append(
                            "first_cross_session_previous_session_mismatch"
                        )
                    if (
                        not account.snapshot_id
                        or account.as_of is None
                        or account.as_of > decision_time
                        or account.as_of.astimezone(
                            intent.decision_at.tzinfo
                        ).date()
                        != plan_session
                    ):
                        blocks.append("cross_session_account_not_reconciled")
                else:
                    if bool(plan.parent_attempt_id) != bool(
                        plan.parent_plan_sha256
                    ):
                        blocks.append("invalid_parent_attempt_binding")
                    if (
                        plan.previous_controlled_session is not None
                        or plan.controlled_calendar_sha256
                        or plan.controlled_session_evidence_sha256
                        or controlled_calendar_sessions is not None
                    ):
                        blocks.append("unexpected_controlled_session_evidence")
            expected_v2_plan_id = adaptive_v2_rebalance_plan_id(
                plan.strategy_id,
                plan.intent_id,
                plan.intent_sha256,
                plan.attempt_id,
                plan.parent_attempt_id,
                plan.parent_plan_sha256,
                plan.account_fingerprint,
                (order.client_order_id for order in plan.orders),
                plan.controlled_session_evidence_sha256,
                execution_quote_bundle_sha256=(
                    plan.execution_quote_bundle_sha256
                ),
                execution_rule_bundle_sha256=plan.execution_rule_bundle_sha256,
            )
            if plan.plan_id != expected_v2_plan_id:
                blocks.append("plan_id_binding_mismatch")
            if not isinstance(trusted_quotes, Mapping):
                blocks.append("trusted_quotes_missing")
            else:
                trusted_quote_mapping_valid = True
                for instrument_id, quote in trusted_quotes.items():
                    if (
                        not isinstance(instrument_id, str)
                        or not isinstance(quote, MarketQuote)
                        or quote.instrument_id != instrument_id
                        or any(
                            not isinstance(price, Decimal)
                            for price in (quote.bid, quote.ask, quote.last)
                        )
                        or type(quote.suspended) is not bool
                        or type(quote.buy_blocked) is not bool
                        or type(quote.sell_blocked) is not bool
                    ):
                        blocks.append("trusted_quote_mapping_invalid")
                        trusted_quote_mapping_valid = False
                        continue
                    trusted_quote_map[instrument_id] = quote

                if trusted_quote_mapping_valid:
                    recomputed_execution_quote_bundle_sha256 = (
                        execution_quote_bundle_sha256(trusted_quote_map)
                    )
                    if (
                        recomputed_execution_quote_bundle_sha256
                        != plan.execution_quote_bundle_sha256
                    ):
                        blocks.append(
                            "execution_quote_bundle_sha256_mismatch"
                        )

            if not isinstance(trusted_fee_schedule, FeeSchedule):
                blocks.append("trusted_fee_schedule_missing")
            if not isinstance(trusted_instruments, Mapping):
                blocks.append("trusted_instrument_rules_missing")
            elif isinstance(trusted_fee_schedule, FeeSchedule):
                try:
                    recomputed_execution_rule_bundle_sha256 = (
                        execution_rule_bundle_sha256(
                            trusted_fee_schedule,
                            trusted_instruments,
                        )
                    )
                except ValueError:
                    blocks.append("trusted_execution_rule_bundle_invalid")
                else:
                    trusted_instrument_map = dict(trusted_instruments)
                    if (
                        recomputed_execution_rule_bundle_sha256
                        != plan.execution_rule_bundle_sha256
                    ):
                        blocks.append("execution_rule_bundle_sha256_mismatch")

            required_rule_ids = set(account.positions)
            required_rule_ids.update(order.instrument_id for order in plan.orders)
            if intent is not None:
                required_rule_ids.update(intent.target_weights)
            for instrument_id in sorted(required_rule_ids):
                if instrument_id not in trusted_instrument_map:
                    blocks.append("trusted_instrument_rule_missing")

            required_quote_ids = set(account.positions)
            required_quote_ids.update(
                order.instrument_id for order in plan.orders
            )
            if intent is not None:
                required_quote_ids.update(intent.target_weights)
            for instrument_id in sorted(required_quote_ids):
                quote = trusted_quote_map.get(instrument_id)
                if quote is None:
                    blocks.append("trusted_quote_missing")
                    continue
                quote_age = (
                    plan.decision_time - quote.as_of
                ).total_seconds()
                if quote_age < 0:
                    blocks.append("trusted_quote_from_future")
                elif quote_age > self.limits.maximum_quote_age_seconds:
                    blocks.append("trusted_quote_stale")
        else:
            if controlled_calendar_sessions is not None:
                blocks.append("unexpected_controlled_calendar_payload")
            expected_v1_plan_id = legacy_rebalance_plan_id(
                plan.strategy_id,
                plan.decision_id,
                plan.account_fingerprint,
                (order.client_order_id for order in plan.orders),
            )
            if plan.plan_id != expected_v1_plan_id:
                blocks.append("plan_id_binding_mismatch")
        if plan.account_fingerprint != bound_account:
            blocks.append("stale_account_snapshot")
        plan_age = (decision_time - plan.decision_time).total_seconds()
        if plan_age < 0:
            blocks.append("plan_from_future")
        if plan_age > self.limits.maximum_quote_age_seconds:
            blocks.append("plan_expired")
        if kill_switch_active:
            blocks.append("kill_switch_active")
        if plan.rejections:
            blocks.append("plan_contains_rejections")
        if plan.blocked_exit_reasons and not plan.orders:
            blocks.append("exit_attempt_has_no_executable_orders")
        if plan.strategy_equity > self.limits.strategy_capital_limit:
            blocks.append("strategy_capital_limit_exceeded")
        reserve_required = plan.strategy_equity * self.limits.cash_reserve_ratio
        if plan.projected_cash < reserve_required:
            blocks.append("cash_reserve_breached")
        recomputed_cash = account.cash
        recomputed_notional = Decimal("0")
        recomputed_ordinary_notional = Decimal("0")
        projected_quantities = {
            instrument_id: position.quantity
            for instrument_id, position in account.positions.items()
        }
        projected_sellable = {
            instrument_id: position.sellable_quantity
            for instrument_id, position in account.positions.items()
        }
        for order in plan.orders:
            if is_v2:
                expected_client_order_id = adaptive_v2_client_order_id(
                    plan.strategy_id,
                    plan.intent_id,
                    plan.attempt_id,
                    order.instrument_id,
                    order.side,
                )
                if order.client_order_id != expected_client_order_id:
                    blocks.append("client_order_id_binding_mismatch")
                if (
                    order.intent_id != plan.intent_id
                    or order.attempt_id != plan.attempt_id
                ):
                    blocks.append("order_intent_binding_mismatch")
                if (
                    order.side == Side.BUY
                    and order.risk_direction
                    is not OrderRiskDirection.RISK_INCREASING
                ):
                    blocks.append("invalid_buy_risk_direction")
                if (
                    order.side == Side.BUY
                    and plan.portfolio_intent_type in NO_BUY_INTENT_TYPES
                ):
                    blocks.append("no_buy_intent_contains_buy")
                if order.risk_direction is OrderRiskDirection.FORCED_EXIT and (
                    order.side is not Side.SELL
                    or plan.portfolio_intent_type
                    is not PortfolioIntentType.ACCOUNT_DRAWDOWN_EXIT
                ):
                    blocks.append("invalid_forced_exit_direction")
                if order.risk_direction is OrderRiskDirection.RISK_REDUCING and (
                    order.side is not Side.SELL
                    or plan.portfolio_intent_type
                    not in RISK_REDUCTION_INTENT_TYPES
                    - {PortfolioIntentType.ACCOUNT_DRAWDOWN_EXIT}
                ):
                    blocks.append("invalid_risk_reducing_direction")
            else:
                expected_client_order_id = legacy_client_order_id(
                    plan.strategy_id,
                    plan.decision_id,
                    order.instrument_id,
                    order.side,
                )
                if order.client_order_id != expected_client_order_id:
                    blocks.append("client_order_id_binding_mismatch")
            trusted_quote = trusted_quote_map.get(order.instrument_id)
            trusted_rule = trusted_instrument_map.get(order.instrument_id)
            if is_v2 and trusted_rule is not None:
                if order.quantity % trusted_rule.lot_size:
                    blocks.append("order_quantity_not_whole_lot")
                if order.limit_price % trusted_rule.tick_size:
                    blocks.append("order_price_not_on_tick")
                if (
                    trusted_rule.instrument_type
                    not in self.limits.allowed_instrument_types
                ):
                    blocks.append("instrument_type_not_allowed")
                if isinstance(trusted_fee_schedule, FeeSchedule):
                    expected_fee = trusted_fee_schedule.estimate(
                        order.side,
                        order.notional,
                        trusted_rule,
                    )
                    if expected_fee != order.estimated_fee:
                        blocks.append("order_estimated_fee_mismatch")
            if is_v2 and trusted_quote is not None:
                if trusted_quote.suspended:
                    blocks.append("trusted_quote_suspended")
                if order.side is Side.BUY and trusted_quote.buy_blocked:
                    blocks.append("trusted_quote_buy_blocked")
                if order.side is Side.SELL and trusted_quote.sell_blocked:
                    blocks.append("trusted_quote_sell_blocked")
                if (
                    (trusted_quote.ask - trusted_quote.bid)
                    / trusted_quote.last
                    > self.limits.maximum_spread_ratio
                ):
                    blocks.append("trusted_quote_spread_too_wide")
                expected_price = (
                    trusted_quote.ask
                    if order.side is Side.BUY
                    else trusted_quote.bid
                )
                if order.limit_price != expected_price:
                    blocks.append("order_price_quote_mismatch")
            recomputed_notional += order.notional
            if order.risk_direction not in {
                OrderRiskDirection.RISK_REDUCING,
                OrderRiskDirection.FORCED_EXIT,
            }:
                recomputed_ordinary_notional += order.notional
            if order.side == Side.BUY:
                recomputed_cash -= order.notional + order.estimated_fee
                projected_quantities[order.instrument_id] = (
                    projected_quantities.get(order.instrument_id, 0)
                    + order.quantity
                )
                if is_v2 and (
                    intent is None
                    or intent.target_weights.get(order.instrument_id, Decimal("0"))
                    <= 0
                ):
                    blocks.append("buy_not_in_positive_intent_targets")
            elif order.side == Side.SELL:
                recomputed_cash += order.notional - order.estimated_fee
                previous_quantity = projected_quantities.get(
                    order.instrument_id, 0
                )
                previous_sellable = projected_sellable.get(
                    order.instrument_id, 0
                )
                if (
                    order.quantity > previous_quantity
                    or order.quantity > previous_sellable
                ):
                    blocks.append("sell_exceeds_current_account_position")
                projected_quantities[order.instrument_id] = max(
                    0, previous_quantity - order.quantity
                )
                projected_sellable[order.instrument_id] = max(
                    0, previous_sellable - order.quantity
                )
            else:
                blocks.append("invalid_order_side")
        recomputed_cash = money(recomputed_cash)
        recomputed_turnover = (recomputed_notional / plan.strategy_equity).quantize(Decimal("0.000001"))
        recomputed_ordinary_turnover = (
            recomputed_ordinary_notional / plan.strategy_equity
        ).quantize(Decimal("0.000001"))
        if recomputed_cash != plan.projected_cash:
            blocks.append("projected_cash_mismatch")
        if recomputed_turnover != plan.turnover_ratio:
            blocks.append("turnover_ratio_mismatch")
        if recomputed_ordinary_turnover != plan.ordinary_turnover_ratio:
            blocks.append("ordinary_turnover_ratio_mismatch")
        if recomputed_cash < 0:
            blocks.append("projected_cash_negative")
        if is_v2 and intent is not None:
            required_projection_ids = set(account.positions)
            required_projection_ids.update({
                instrument_id
                for instrument_id, quantity in projected_quantities.items()
                if quantity > 0
            })
            projection_quotes_complete = all(
                instrument_id in trusted_quote_map
                for instrument_id in required_projection_ids
            )
            if projection_quotes_complete:
                recomputed_equity = money(
                    account.cash
                    + sum(
                        (
                            trusted_quote_map[instrument_id].last
                            * position.quantity
                            for instrument_id, position in account.positions.items()
                        ),
                        Decimal("0"),
                    )
                )
                if recomputed_equity != plan.strategy_equity:
                    blocks.append("strategy_equity_quote_mismatch")
                projected_position_value = sum(
                    (
                        trusted_quote_map[instrument_id].last * quantity
                        for instrument_id, quantity in projected_quantities.items()
                        if quantity > 0
                    ),
                    Decimal("0"),
                )
                projected_nav = recomputed_cash + projected_position_value
                if projected_nav <= 0 or recomputed_equity <= 0:
                    blocks.append("projected_nav_not_positive")
                else:
                    recomputed_feasible_exposure = (
                        projected_position_value / projected_nav
                    ).quantize(Decimal("0.000001"))
                    if (
                        recomputed_feasible_exposure
                        != plan.feasible_gross_exposure
                    ):
                        blocks.append("feasible_gross_exposure_mismatch")
                    positive_positions = [
                        instrument_id
                        for instrument_id, quantity in projected_quantities.items()
                        if quantity > 0
                    ]
                    if (
                        plan.strategy_id
                        == ADAPTIVE_EXPOSURE_V2_STRATEGY_ID
                        and intent.intent_type
                        is PortfolioIntentType.ALPHA_REBALANCE
                        and len(positive_positions)
                        > ADAPTIVE_EXPOSURE_V2_MAX_POSITIONS
                    ):
                        blocks.append(
                            "adaptive_v2_projected_position_count_exceeded"
                        )
                    for instrument_id in positive_positions:
                        projected_weight = (
                            trusted_quote_map[instrument_id].last
                            * projected_quantities[instrument_id]
                            / projected_nav
                        )
                        if (
                            plan.strategy_id
                            == ADAPTIVE_EXPOSURE_V2_STRATEGY_ID
                            and intent.intent_type
                            is PortfolioIntentType.ALPHA_REBALANCE
                            and projected_weight
                            > ADAPTIVE_EXPOSURE_V2_MAX_POSITION_WEIGHT
                        ):
                            blocks.append(
                                "adaptive_v2_projected_position_weight_exceeded"
                            )
                        if (
                            intent.intent_type
                            is PortfolioIntentType.ALPHA_REBALANCE
                            and projected_weight
                            > intent.target_weights.get(
                                instrument_id, Decimal("0")
                            )
                        ):
                            blocks.append(
                                "projected_position_exceeds_intent_target"
                            )

                if intent.intent_type in NO_BUY_INTENT_TYPES:
                    if any(
                        projected_quantities.get(instrument_id, 0)
                        > position.quantity
                        for instrument_id, position in account.positions.items()
                    ) or any(
                        quantity > 0 and instrument_id not in account.positions
                        for instrument_id, quantity in projected_quantities.items()
                    ):
                        blocks.append("no_buy_intent_increases_position")

                if intent.intent_type in FULL_EXIT_INTENT_TYPES:
                    sell_orders = {
                        order.instrument_id: order
                        for order in plan.orders
                        if order.side is Side.SELL
                    }
                    blocked_by_instrument: dict[str, object] = {}
                    for item in plan.blocked_exit_reasons:
                        if item.instrument_id in blocked_by_instrument:
                            blocks.append("exit_plan_blocked_reason_duplicate")
                        blocked_by_instrument[item.instrument_id] = item
                    if any(
                        instrument_id not in account.positions
                        for instrument_id in blocked_by_instrument
                    ):
                        blocks.append("exit_plan_unexpected_blocked_position")
                    for instrument_id, position in account.positions.items():
                        quote = trusted_quote_map.get(instrument_id)
                        rule = trusted_instrument_map.get(instrument_id)
                        order = sell_orders.get(instrument_id)
                        blocked_reason = blocked_by_instrument.get(instrument_id)
                        if quote is None or rule is None:
                            blocks.append("exit_plan_coverage_incomplete")
                            continue
                        expected_block_code: str | None = None
                        expected_quantity = min(
                            position.quantity,
                            position.sellable_quantity,
                        )
                        if quote.suspended:
                            expected_block_code = "sell_blocked_suspended"
                            expected_quantity = 0
                        elif expected_quantity <= 0:
                            expected_block_code = (
                                "sell_blocked_t_plus_one"
                                if rule.t_plus_one
                                else "insufficient_sellable_quantity"
                            )
                        elif expected_quantity != position.quantity:
                            lots = (
                                Decimal(expected_quantity) / rule.lot_size
                            ).to_integral_value(rounding=ROUND_FLOOR)
                            expected_quantity = int(lots) * rule.lot_size
                            if expected_quantity <= 0:
                                expected_block_code = (
                                    "insufficient_sellable_quantity"
                                )
                        if (
                            expected_block_code is None
                            and quote.sell_blocked
                        ):
                            expected_block_code = "sell_blocked_limit_down"
                            expected_quantity = 0
                        if expected_quantity > 0:
                            if order is None:
                                blocks.append("exit_plan_coverage_incomplete")
                            elif order.quantity != expected_quantity:
                                blocks.append("exit_plan_sell_quantity_mismatch")
                            residual_quantity = position.quantity - expected_quantity
                            if residual_quantity > 0:
                                expected_residual_code = (
                                    "sell_blocked_t_plus_one"
                                    if rule.t_plus_one
                                    else "insufficient_sellable_quantity"
                                )
                                if blocked_reason is None:
                                    blocks.append("exit_plan_coverage_incomplete")
                                elif (
                                    getattr(blocked_reason, "code", None)
                                    != expected_residual_code
                                ):
                                    blocks.append("exit_plan_block_reason_mismatch")
                            elif blocked_reason is not None:
                                blocks.append("exit_plan_coverage_ambiguous")
                        else:
                            if order is not None:
                                blocks.append("exit_plan_unexpected_sell_order")
                            if blocked_reason is None:
                                blocks.append("exit_plan_coverage_incomplete")
                            elif getattr(blocked_reason, "code", None) != expected_block_code:
                                blocks.append("exit_plan_block_reason_mismatch")
        expected_turnover_limit = (
            self.limits.bootstrap_turnover_ratio
            if plan.bootstrap
            else self.limits.max_daily_turnover_ratio
        )
        if plan.turnover_limit != expected_turnover_limit:
            blocks.append("turnover_limit_mismatch")
        if daily_pnl_ratio <= -self.limits.maximum_daily_loss_ratio:
            if is_v2:
                if any(
                    order.risk_direction is OrderRiskDirection.RISK_INCREASING
                    for order in plan.orders
                ):
                    blocks.append("daily_loss_limit_reached")
            elif (
                plan.portfolio_intent_type
                is not PortfolioIntentType.ACCOUNT_DRAWDOWN_EXIT
            ):
                blocks.append("daily_loss_limit_reached")
        if plan.ordinary_turnover_ratio > plan.turnover_limit:
            blocks.append("turnover_limit_exceeded")
        if daily_turnover_ratio_before_plan + plan.ordinary_turnover_ratio > plan.turnover_limit:
            blocks.append("cumulative_daily_turnover_limit_exceeded")
        if len(plan.orders) > self.limits.max_orders_per_plan:
            blocks.append("order_count_limit_exceeded")
        daily_order_limit = self.limits.max_orders_per_day or self.limits.max_orders_per_plan
        if daily_order_count_before_plan + len(plan.orders) > daily_order_limit:
            blocks.append("cumulative_daily_order_count_exceeded")
        max_order_notional = plan.strategy_equity * self.limits.max_order_notional_ratio
        if any(
            order.notional > max_order_notional
            and order.risk_direction
            not in {OrderRiskDirection.RISK_REDUCING, OrderRiskDirection.FORCED_EXIT}
            for order in plan.orders
        ):
            blocks.append("order_notional_limit_exceeded")
        if self.limits.allowed_instrument_ids and any(
            order.instrument_id not in self.limits.allowed_instrument_ids for order in plan.orders
        ):
            blocks.append("instrument_not_whitelisted")

        if mode is ExecutionMode.SHADOW:
            paper_days = (
                (decision_time - readiness.paper_started_at).days
                if readiness.paper_started_at is not None and readiness.paper_started_at <= decision_time
                else -1
            )
            if (
                paper_days < self.MINIMUM_PAPER_CALENDAR_DAYS
                or readiness.paper_trade_events < self.MINIMUM_PAPER_TRADE_EVENTS
            ):
                blocks.append("paper_stage_incomplete")

        approval = None
        if not blocks:
            approval = ExecutionApproval(
                mode=mode,
                plan_fingerprint=plan_fingerprint(plan),
                account_fingerprint=bound_account,
                issued_at=decision_time,
                valid_until=plan.decision_time
                + timedelta(seconds=self.limits.maximum_quote_age_seconds),
                turnover_limit=plan.turnover_limit,
                max_orders_per_day=self.limits.max_orders_per_day
                or self.limits.max_orders_per_plan,
                _issuer=_GATE_ISSUER,
                execution_rule_bundle_sha256=plan.execution_rule_bundle_sha256,
            )
        return GateResult(allowed=not blocks, block_codes=tuple(blocks), approval=approval)
