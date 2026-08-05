"""Pre-trade limits and fail-closed execution gates."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from trading.costs import money
from trading.integrity import account_fingerprint, plan_fingerprint
from trading.models import AccountSnapshot, ExecutionMode, RebalancePlan, Side


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
    live_enable_token: str = ""
    live_adapter_implemented: bool = False

    def __post_init__(self) -> None:
        boolean_fields = (
            "programmatic_report_confirmed",
            "broker_api_authorized",
            "account_fee_schedule_verified",
            "account_reconciled",
            "trading_universe_frozen",
            "live_adapter_implemented",
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
        if type(self.live_enable_token) is not str:
            raise ValueError("live_enable_token must be a string")


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

    def was_issued_by_gate(self) -> bool:
        return self._issuer is _GATE_ISSUER


class ExecutionGate:
    REQUIRED_LIVE_TOKEN = "ENABLE_LIVE_ORDERS"
    MINIMUM_PAPER_CALENDAR_DAYS = 90
    MINIMUM_PAPER_TRADE_EVENTS = 30
    MINIMUM_SHADOW_SESSIONS = 5

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
    ) -> GateResult:
        blocks: list[str] = []
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
        if plan.strategy_equity > self.limits.strategy_capital_limit:
            blocks.append("strategy_capital_limit_exceeded")
        reserve_required = plan.strategy_equity * self.limits.cash_reserve_ratio
        if plan.projected_cash < reserve_required:
            blocks.append("cash_reserve_breached")
        recomputed_cash = account.cash
        recomputed_notional = Decimal("0")
        for order in plan.orders:
            recomputed_notional += order.notional
            if order.side == Side.BUY:
                recomputed_cash -= order.notional + order.estimated_fee
            elif order.side == Side.SELL:
                recomputed_cash += order.notional - order.estimated_fee
            else:
                blocks.append("invalid_order_side")
        recomputed_cash = money(recomputed_cash)
        recomputed_turnover = (recomputed_notional / plan.strategy_equity).quantize(Decimal("0.000001"))
        if recomputed_cash != plan.projected_cash:
            blocks.append("projected_cash_mismatch")
        if recomputed_turnover != plan.turnover_ratio:
            blocks.append("turnover_ratio_mismatch")
        if recomputed_cash < 0:
            blocks.append("projected_cash_negative")
        expected_turnover_limit = (
            self.limits.bootstrap_turnover_ratio
            if plan.bootstrap
            else self.limits.max_daily_turnover_ratio
        )
        if plan.turnover_limit != expected_turnover_limit:
            blocks.append("turnover_limit_mismatch")
        if daily_pnl_ratio <= -self.limits.maximum_daily_loss_ratio:
            blocks.append("daily_loss_limit_reached")
        if plan.turnover_ratio > plan.turnover_limit:
            blocks.append("turnover_limit_exceeded")
        if daily_turnover_ratio_before_plan + plan.turnover_ratio > plan.turnover_limit:
            blocks.append("cumulative_daily_turnover_limit_exceeded")
        if len(plan.orders) > self.limits.max_orders_per_plan:
            blocks.append("order_count_limit_exceeded")
        daily_order_limit = self.limits.max_orders_per_day or self.limits.max_orders_per_plan
        if daily_order_count_before_plan + len(plan.orders) > daily_order_limit:
            blocks.append("cumulative_daily_order_count_exceeded")
        max_order_notional = plan.strategy_equity * self.limits.max_order_notional_ratio
        if any(order.notional > max_order_notional for order in plan.orders):
            blocks.append("order_notional_limit_exceeded")
        if self.limits.allowed_instrument_ids and any(
            order.instrument_id not in self.limits.allowed_instrument_ids for order in plan.orders
        ):
            blocks.append("instrument_not_whitelisted")

        if mode in {ExecutionMode.SHADOW, ExecutionMode.LIVE}:
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

        if mode == ExecutionMode.LIVE:
            if not self.limits.allowed_instrument_ids:
                blocks.append("trading_universe_missing")
            if not readiness.programmatic_report_confirmed:
                blocks.append("programmatic_report_missing")
            if not readiness.broker_api_authorized:
                blocks.append("broker_api_not_authorized")
            if not readiness.account_fee_schedule_verified:
                blocks.append("account_fee_schedule_unverified")
            if not readiness.account_reconciled:
                blocks.append("account_reconciliation_missing")
            if not readiness.trading_universe_frozen:
                blocks.append("trading_universe_not_frozen")
            if readiness.shadow_sessions < self.MINIMUM_SHADOW_SESSIONS:
                blocks.append("shadow_stage_incomplete")
            if readiness.live_enable_token != self.REQUIRED_LIVE_TOKEN:
                blocks.append("live_enable_token_missing")
            if not readiness.live_adapter_implemented:
                blocks.append("live_adapter_not_implemented")

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
            )
        return GateResult(allowed=not blocks, block_codes=tuple(blocks), approval=approval)
