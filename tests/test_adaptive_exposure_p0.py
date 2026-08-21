from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from research.strategy_workspace.contracts import canonical_sha256
from research.market_data.validation import SchemaValidationError, validate_json_schema
from trading.costs import FeeSchedule
from trading.integrity import (
    account_fingerprint,
    adaptive_v2_client_order_id,
    adaptive_v2_rebalance_plan_id,
    canonical_controlled_calendar_sha256,
    execution_quote_bundle_sha256,
    execution_rule_bundle_sha256,
)
from trading.models import (
    LIVE_NOT_SUPPORTED_CODE,
    AccountSnapshot,
    ExecutionMode,
    InstrumentRule,
    MarketQuote,
    OrderRiskDirection,
    OrderStatus,
    PortfolioIntent,
    PortfolioIntentType,
    Position,
    Side,
)
from trading.order_store import ConcurrentPaperAccountUpdate, OrderStore
from trading.paper import PaperBroker
from trading.planner import (
    build_rebalance_plan,
    build_rebalance_plan_from_intent,
    execution_plan_record,
)
from trading.risk import ExecutionGate, LiveReadiness, RiskLimits
from trading.strategy_bridge import (
    SignalEnvelope,
    SignalRejected,
    intent_from_signal,
    targets_from_signal,
)


TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 8, 20, 9, 30, tzinfo=TZ)
STRATEGY_ID = "a-share-small-account-adaptive-exposure-v2"
ZERO = Decimal("0")
EXPOSURE_QUANTUM = Decimal("0.000001")
CONTROLLED_CALENDAR_SESSIONS = (
    NOW.date(),
    (NOW + timedelta(days=1)).date(),
)
CALENDAR_SHA256 = canonical_controlled_calendar_sha256(
    CONTROLLED_CALENDAR_SESSIONS
)


def D(value: str | int | float) -> Decimal:
    return Decimal(str(value))


def sha256_digit(digit: int) -> str:
    return str(digit % 10) * 64


def rule(instrument_id: str) -> InstrumentRule:
    return InstrumentRule(
        instrument_id=instrument_id,
        name=f"测试股票 {instrument_id}",
        instrument_type="EQUITY",
        lot_size=100,
        tick_size=D("0.01"),
        sell_stamp_duty_rate=ZERO,
        t_plus_one=True,
    )


def quote(
    instrument_id: str,
    price: str = "10",
    *,
    as_of: datetime = NOW,
    suspended: bool = False,
    buy_blocked: bool = False,
    sell_blocked: bool = False,
) -> MarketQuote:
    value = D(price)
    return MarketQuote(
        instrument_id=instrument_id,
        bid=value,
        ask=value,
        last=value,
        as_of=as_of,
        suspended=suspended,
        buy_blocked=buy_blocked,
        sell_blocked=sell_blocked,
    )


def fees() -> FeeSchedule:
    return FeeSchedule(
        commission_rate=D("0.0003"),
        minimum_commission=D("5"),
        exchange_fee_rate=ZERO,
    )


def limits(**changes: object) -> RiskLimits:
    values: dict[str, object] = {
        "strategy_capital_limit": D("10000"),
        "allowed_instrument_types": ("EQUITY",),
        "max_positions": 4,
        "max_position_weight": D("0.40"),
        "cash_reserve_ratio": ZERO,
        "minimum_trade_notional": D("100"),
        "max_orders_per_plan": 10,
        "max_order_notional_ratio": D("0.40"),
        "max_daily_turnover_ratio": D("0.25"),
        "bootstrap_turnover_ratio": D("0.90"),
        "maximum_quote_age_seconds": 120,
        "maximum_daily_loss_ratio": D("0.02"),
        "max_orders_per_day": 10,
        "maximum_spread_ratio": D("0.01"),
    }
    values.update(changes)
    return RiskLimits(**values)  # type: ignore[arg-type]


def signal(
    target_weights: dict[str, Decimal],
    *,
    signal_id: str = "adaptive-signal-001",
) -> SignalEnvelope:
    return SignalEnvelope(
        signal_id=signal_id,
        model_id="adaptive-exposure-v2",
        model_admission="approved_for_paper",
        source_kind="controlled_point_in_time_signal",
        available_at=NOW - timedelta(minutes=5),
        frozen_at=NOW - timedelta(minutes=1),
        data_snapshot_hash=sha256_digit(2),
        synthetic=False,
        trade_eligible=True,
        target_weights=target_weights,
    )


def intent(
    intent_type: PortfolioIntentType,
    target_weights: dict[str, Decimal],
    target_gross_exposure: Decimal,
    *,
    intent_id: str = "adaptive-intent-001",
    decision_at: datetime = NOW,
) -> PortfolioIntent:
    return PortfolioIntent(
        intent_id=intent_id,
        strategy_id=STRATEGY_ID,
        intent_type=intent_type,
        decision_at=decision_at,
        available_at=decision_at - timedelta(minutes=5),
        frozen_at=decision_at - timedelta(minutes=1),
        target_gross_exposure=target_gross_exposure,
        target_weights=target_weights,
        reason_codes=(f"test_{intent_type.value.lower()}",),
        signal_sha256=sha256_digit(1),
        market_data_sha256=sha256_digit(2),
        model_sha256=sha256_digit(3),
        risk_state_sha256=sha256_digit(4),
    )


def approve(
    account: AccountSnapshot,
    plan,
    configured_limits: RiskLimits,
    *,
    decision_time: datetime,
    daily_pnl_ratio: Decimal = ZERO,
    daily_turnover_ratio_before_plan: Decimal = ZERO,
    trusted_quotes: dict[str, MarketQuote] | None = None,
    controlled_calendar_sessions: tuple[date, ...] | None = None,
    trusted_fee_schedule: FeeSchedule | None = None,
    trusted_instruments: dict[str, InstrumentRule] | None = None,
):
    if trusted_quotes is None:
        order_prices = {
            order.instrument_id: order.limit_price for order in plan.orders
        }
        required_ids = set(account.positions)
        required_ids.update(order_prices)
        if plan.bound_portfolio_intent is not None:
            required_ids.update(plan.bound_portfolio_intent.target_weights)
        trusted_quotes = {
            instrument_id: quote(
                instrument_id,
                str(order_prices.get(instrument_id, D("10"))),
                as_of=plan.decision_time,
            )
            for instrument_id in required_ids
        }
    if trusted_instruments is None:
        required_rule_ids = set(account.positions)
        required_rule_ids.update(order.instrument_id for order in plan.orders)
        if plan.bound_portfolio_intent is not None:
            required_rule_ids.update(plan.bound_portfolio_intent.target_weights)
        trusted_instruments = {
            instrument_id: rule(instrument_id)
            for instrument_id in required_rule_ids
        }
    if trusted_fee_schedule is None:
        trusted_fee_schedule = fees()
    return ExecutionGate(configured_limits).evaluate(
        mode=ExecutionMode.PAPER,
        plan=plan,
        account=account,
        decision_time=decision_time,
        daily_pnl_ratio=daily_pnl_ratio,
        kill_switch_active=False,
        readiness=LiveReadiness(),
        daily_turnover_ratio_before_plan=daily_turnover_ratio_before_plan,
        trusted_quotes=trusted_quotes,
        trusted_market_data_sha256=(
            plan.bound_portfolio_intent.market_data_sha256
            if plan.bound_portfolio_intent is not None
            else ""
        ),
        controlled_calendar_sessions=controlled_calendar_sessions,
        trusted_fee_schedule=trusted_fee_schedule,
        trusted_instruments=trusted_instruments,
    )


class AdaptiveExposureP0Test(unittest.TestCase):
    def test_data_fail_and_manual_pause_cannot_buy_in_planner_or_gate(self) -> None:
        configured_limits = limits()
        account = AccountSnapshot(STRATEGY_ID, D("10000"), {})
        alpha = intent(
            PortfolioIntentType.ALPHA_REBALANCE,
            {"AAA": D("0.21")},
            D("0.21"),
            intent_id="pause-no-buy",
        )
        alpha_plan = build_rebalance_plan_from_intent(
            account,
            alpha,
            {"AAA": rule("AAA")},
            {"AAA": quote("AAA")},
            fees(),
            configured_limits,
            NOW,
            attempt_id="pause-no-buy-attempt",
        )
        self.assertEqual([order.side for order in alpha_plan.orders], [Side.BUY])

        for intent_type in (
            PortfolioIntentType.DATA_FAIL_CLOSED,
            PortfolioIntentType.MANUAL_PAUSE,
        ):
            with self.subTest(intent_type=intent_type):
                paused = intent(
                    intent_type,
                    {"AAA": D("0.21")},
                    D("0.21"),
                    intent_id="pause-no-buy",
                )
                with self.assertRaisesRegex(ValueError, "cannot add a new position"):
                    build_rebalance_plan_from_intent(
                        account,
                        paused,
                        {"AAA": rule("AAA")},
                        {"AAA": quote("AAA")},
                        fees(),
                        configured_limits,
                        NOW,
                        attempt_id="pause-no-buy-attempt",
                    )

                forged = replace(
                    alpha_plan,
                    intent_sha256=paused.intent_sha256,
                    portfolio_intent_type=intent_type,
                    bound_portfolio_intent=paused,
                )
                forged = replace(
                    forged,
                    plan_id=adaptive_v2_rebalance_plan_id(
                        forged.strategy_id,
                        forged.intent_id,
                        forged.intent_sha256,
                        forged.attempt_id,
                        forged.parent_attempt_id,
                        forged.parent_plan_sha256,
                        forged.account_fingerprint,
                        (order.client_order_id for order in forged.orders),
                        forged.controlled_session_evidence_sha256,
                        execution_quote_bundle_sha256=(
                            forged.execution_quote_bundle_sha256
                        ),
                        execution_rule_bundle_sha256=(
                            forged.execution_rule_bundle_sha256
                        ),
                    ),
                )
                gate = approve(
                    account,
                    forged,
                    configured_limits,
                    decision_time=NOW,
                )
                self.assertFalse(gate.allowed)
                self.assertIn("no_buy_intent_contains_buy", gate.block_codes)

    def test_gate_independently_rejects_an_exit_plan_omitting_a_position(self) -> None:
        configured_limits = limits()
        account = AccountSnapshot(
            STRATEGY_ID,
            D("2000"),
            {
                "AAA": Position("AAA", 400, 400),
                "BBB": Position("BBB", 400, 400),
            },
        )
        instruments = {item: rule(item) for item in account.positions}
        execution_quotes = {item: quote(item) for item in account.positions}
        zero_fees = FeeSchedule(ZERO, ZERO, ZERO)
        risk_off = intent(
            PortfolioIntentType.RISK_OFF,
            {},
            ZERO,
            intent_id="exit-coverage",
        )
        full = build_rebalance_plan_from_intent(
            account,
            risk_off,
            instruments,
            execution_quotes,
            zero_fees,
            configured_limits,
            NOW,
            attempt_id="exit-coverage-attempt",
        )
        kept_orders = tuple(
            order for order in full.orders if order.instrument_id == "AAA"
        )
        omitted = replace(
            full,
            orders=kept_orders,
            projected_cash=D("6000.00"),
            turnover_ratio=D("0.400000"),
            feasible_gross_exposure=D("0.400000"),
        )
        omitted = replace(
            omitted,
            plan_id=adaptive_v2_rebalance_plan_id(
                omitted.strategy_id,
                omitted.intent_id,
                omitted.intent_sha256,
                omitted.attempt_id,
                omitted.parent_attempt_id,
                omitted.parent_plan_sha256,
                omitted.account_fingerprint,
                (order.client_order_id for order in omitted.orders),
                omitted.controlled_session_evidence_sha256,
                execution_quote_bundle_sha256=(
                    omitted.execution_quote_bundle_sha256
                ),
                execution_rule_bundle_sha256=(
                    omitted.execution_rule_bundle_sha256
                ),
            ),
        )
        gate = approve(
            account,
            omitted,
            configured_limits,
            decision_time=NOW,
            trusted_quotes=execution_quotes,
            trusted_fee_schedule=zero_fees,
            trusted_instruments=instruments,
        )
        self.assertFalse(gate.allowed)
        self.assertIn("exit_plan_coverage_incomplete", gate.block_codes)

    def test_all_reduction_intents_allow_first_d_plus_one_but_later_need_lineage(self) -> None:
        configured_limits = limits()
        next_session = NOW + timedelta(days=1)
        account = AccountSnapshot(
            STRATEGY_ID,
            D("6000"),
            {"AAA": Position("AAA", 400, 400)},
            snapshot_id="first-d-plus-one",
            as_of=next_session,
        )
        cases = (
            (PortfolioIntentType.NO_ALPHA_CASH, {}, ZERO),
            (
                PortfolioIntentType.DEFENSIVE_REDUCTION,
                {"AAA": D("0.20")},
                D("0.20"),
            ),
            (PortfolioIntentType.RISK_OFF, {}, ZERO),
            (PortfolioIntentType.ACCOUNT_DRAWDOWN_EXIT, {}, ZERO),
        )
        for intent_type, targets, gross in cases:
            with self.subTest(intent_type=intent_type):
                frozen = intent(
                    intent_type,
                    targets,
                    gross,
                    intent_id=f"first-d1-{intent_type.value.lower()}",
                )
                plan = build_rebalance_plan_from_intent(
                    account,
                    frozen,
                    {"AAA": rule("AAA")},
                    {"AAA": quote("AAA", as_of=next_session)},
                    fees(),
                    configured_limits,
                    next_session,
                    attempt_id=f"first-d1-attempt-{intent_type.value.lower()}",
                    previous_controlled_session=NOW.date(),
                    controlled_calendar_sha256=CALENDAR_SHA256,
                    controlled_calendar_sessions=CONTROLLED_CALENDAR_SESSIONS,
                )
                self.assertIsNone(plan.parent_attempt_id)
                gate = approve(
                    account,
                    plan,
                    configured_limits,
                    decision_time=next_session,
                    controlled_calendar_sessions=CONTROLLED_CALENDAR_SESSIONS,
                )
                self.assertTrue(gate.allowed, gate.block_codes)

        third_session = NOW + timedelta(days=2)
        three_sessions = (
            NOW.date(),
            next_session.date(),
            third_session.date(),
        )
        later_account = replace(
            account,
            snapshot_id="later-without-lineage",
            as_of=third_session,
        )
        with self.assertRaisesRegex(ValueError, "First cross-session execution"):
            build_rebalance_plan_from_intent(
                later_account,
                intent(
                    PortfolioIntentType.RISK_OFF,
                    {},
                    ZERO,
                    intent_id="later-needs-lineage",
                ),
                {"AAA": rule("AAA")},
                {"AAA": quote("AAA", as_of=third_session)},
                fees(),
                configured_limits,
                third_session,
                attempt_id="later-without-lineage-attempt",
                previous_controlled_session=next_session.date(),
                controlled_calendar_sha256=(
                    canonical_controlled_calendar_sha256(three_sessions)
                ),
                controlled_calendar_sessions=three_sessions,
            )

    def test_daily_loss_blocks_risk_increase_but_not_pure_reductions(self) -> None:
        configured_limits = limits()
        account = AccountSnapshot(
            STRATEGY_ID,
            D("6000"),
            {"AAA": Position("AAA", 400, 400)},
        )
        cases = (
            (PortfolioIntentType.NO_ALPHA_CASH, {}, ZERO),
            (
                PortfolioIntentType.DEFENSIVE_REDUCTION,
                {"AAA": D("0.20")},
                D("0.20"),
            ),
            (PortfolioIntentType.RISK_OFF, {}, ZERO),
            (PortfolioIntentType.ACCOUNT_DRAWDOWN_EXIT, {}, ZERO),
        )
        for intent_type, targets, gross in cases:
            with self.subTest(intent_type=intent_type):
                frozen = intent(
                    intent_type,
                    targets,
                    gross,
                    intent_id=f"loss-{intent_type.value.lower()}",
                )
                plan = build_rebalance_plan_from_intent(
                    account,
                    frozen,
                    {"AAA": rule("AAA")},
                    {"AAA": quote("AAA")},
                    fees(),
                    configured_limits,
                    NOW,
                    attempt_id=f"loss-attempt-{intent_type.value.lower()}",
                )
                self.assertTrue(all(order.side is Side.SELL for order in plan.orders))
                gate = approve(
                    account,
                    plan,
                    configured_limits,
                    decision_time=NOW,
                    daily_pnl_ratio=D("-0.03"),
                )
                self.assertTrue(gate.allowed, gate.block_codes)

        buy_account = AccountSnapshot(STRATEGY_ID, D("10000"), {})
        buy_intent = intent(
            PortfolioIntentType.ALPHA_REBALANCE,
            {"AAA": D("0.21")},
            D("0.21"),
            intent_id="loss-risk-increase",
        )
        buy_plan = build_rebalance_plan_from_intent(
            buy_account,
            buy_intent,
            {"AAA": rule("AAA")},
            {"AAA": quote("AAA")},
            fees(),
            configured_limits,
            NOW,
            attempt_id="loss-risk-increase-attempt",
        )
        buy_gate = approve(
            buy_account,
            buy_plan,
            configured_limits,
            decision_time=NOW,
            daily_pnl_ratio=D("-0.03"),
        )
        self.assertFalse(buy_gate.allowed)
        self.assertIn("daily_loss_limit_reached", buy_gate.block_codes)

    def test_ordinary_empty_targets_fail_closed(self) -> None:
        with self.assertRaises(SignalRejected) as caught:
            targets_from_signal(signal({}), NOW, ExecutionMode.PAPER)
        self.assertEqual(caught.exception.code, "empty_targets")

        with self.assertRaisesRegex(ValueError, "empty targets"):
            PortfolioIntent(
                intent_id="bad-empty-alpha",
                strategy_id=STRATEGY_ID,
                intent_type=PortfolioIntentType.ALPHA_REBALANCE,
                decision_at=NOW,
                available_at=NOW - timedelta(minutes=5),
                frozen_at=NOW - timedelta(minutes=1),
                target_gross_exposure=ZERO,
                target_weights={},
                reason_codes=("unexpected_empty_alpha",),
                signal_sha256=sha256_digit(1),
                market_data_sha256=sha256_digit(2),
                model_sha256=sha256_digit(3),
                risk_state_sha256=sha256_digit(4),
            )

        with self.assertRaisesRegex(ValueError, "must not be empty"):
            build_rebalance_plan(
                account=AccountSnapshot(STRATEGY_ID, D("10000"), {}),
                target_weights={},
                instruments={},
                quotes={},
                fees=fees(),
                limits=limits(),
                decision_time=NOW,
                decision_id="ordinary-empty-attempt",
            )

    def test_legacy_v1_client_order_id_keeps_original_hash_contract(self) -> None:
        legacy_strategy_id = "small-account-paper-v1"
        decision_id = "legacy-frozen-decision"
        account = AccountSnapshot(legacy_strategy_id, D("10000"), {})
        plan = build_rebalance_plan(
            account=account,
            target_weights={"AAA": D("0.20")},
            instruments={"AAA": rule("AAA")},
            quotes={"AAA": quote("AAA")},
            fees=fees(),
            limits=limits(),
            decision_time=NOW,
            decision_id=decision_id,
        )

        expected = hashlib.sha256(
            f"{legacy_strategy_id}|{decision_id}|AAA|BUY".encode("utf-8")
        ).hexdigest()[:24]
        self.assertEqual(plan.orders[0].client_order_id, expected)
        self.assertEqual(plan.orders[0].intent_id, "")
        self.assertEqual(plan.orders[0].attempt_id, "")

    def test_risk_off_and_no_alpha_cash_accept_explicit_empty_intents(self) -> None:
        for intent_type in (
            PortfolioIntentType.RISK_OFF,
            PortfolioIntentType.NO_ALPHA_CASH,
        ):
            with self.subTest(intent_type=intent_type):
                result = intent_from_signal(
                    signal({}, signal_id=f"cash-{intent_type.value.lower()}"),
                    NOW,
                    ExecutionMode.PAPER,
                    strategy_id=STRATEGY_ID,
                    intent_type=intent_type,
                    target_gross_exposure=ZERO,
                    reason_codes=("explicit_cash_target",),
                    signal_sha256=sha256_digit(1),
                    model_sha256=sha256_digit(3),
                    risk_state_sha256=sha256_digit(4),
                )
                self.assertEqual(result.target_weights, {})
                self.assertEqual(result.target_gross_exposure, ZERO)
                self.assertIs(result.intent_type, intent_type)

    def test_cash_intents_only_sell_and_bypass_ordinary_turnover(self) -> None:
        account = AccountSnapshot(
            STRATEGY_ID,
            D("2000"),
            {
                "AAA": Position("AAA", 400, 400),
                "BBB": Position("BBB", 400, 400),
            },
        )
        instruments = {item: rule(item) for item in account.positions}
        quotes = {item: quote(item) for item in account.positions}
        configured_limits = limits()

        for intent_type in (
            PortfolioIntentType.RISK_OFF,
            PortfolioIntentType.NO_ALPHA_CASH,
            PortfolioIntentType.ACCOUNT_DRAWDOWN_EXIT,
        ):
            with self.subTest(intent_type=intent_type):
                cash_intent = intent(
                    intent_type,
                    {},
                    ZERO,
                    intent_id=f"cash-{intent_type.value.lower()}",
                )
                plan = build_rebalance_plan_from_intent(
                    account,
                    cash_intent,
                    instruments,
                    quotes,
                    fees(),
                    configured_limits,
                    NOW,
                    attempt_id=f"attempt-{intent_type.value.lower()}",
                )

                self.assertEqual({order.side for order in plan.orders}, {Side.SELL})
                expected_direction = (
                    OrderRiskDirection.FORCED_EXIT
                    if intent_type is PortfolioIntentType.ACCOUNT_DRAWDOWN_EXIT
                    else OrderRiskDirection.RISK_REDUCING
                )
                self.assertTrue(
                    all(order.risk_direction is expected_direction for order in plan.orders)
                )
                self.assertEqual(plan.turnover_ratio, D("0.800000"))
                self.assertEqual(plan.ordinary_turnover_ratio, ZERO)
                self.assertEqual(plan.rejections, ())
                gate = approve(
                    account,
                    plan,
                    configured_limits,
                    decision_time=NOW,
                    daily_pnl_ratio=(
                        D("-0.12")
                        if intent_type is PortfolioIntentType.ACCOUNT_DRAWDOWN_EXIT
                        else ZERO
                    ),
                    daily_turnover_ratio_before_plan=D("0.24"),
                )
                self.assertTrue(gate.allowed, gate.block_codes)

    def test_forced_exit_bypasses_persisted_turnover_but_rotation_does_not(self) -> None:
        configured_limits = limits()
        fee_schedule = FeeSchedule(
            commission_rate=ZERO,
            minimum_commission=ZERO,
            exchange_fee_rate=ZERO,
        )
        instruments = {item: rule(item) for item in ("AAA", "BBB", "CCC", "DDD")}
        quotes = {item: quote(item) for item in instruments}
        account = AccountSnapshot(
            STRATEGY_ID,
            D("2000"),
            {"AAA": Position("AAA", 400, 400), "BBB": Position("BBB", 400, 400)},
        )

        ordinary_intent = intent(
            PortfolioIntentType.ALPHA_REBALANCE,
            {"AAA": D("0.40"), "BBB": D("0.40"), "CCC": D("0.10")},
            D("0.90"),
            intent_id="ordinary-before-exit",
        )
        ordinary_plan = build_rebalance_plan_from_intent(
            account,
            ordinary_intent,
            instruments,
            {key: quotes[key] for key in ("AAA", "BBB", "CCC")},
            fee_schedule,
            configured_limits,
            NOW,
            attempt_id="ordinary-attempt",
        )
        ordinary_gate = approve(
            account,
            ordinary_plan,
            configured_limits,
            decision_time=NOW,
            trusted_fee_schedule=fee_schedule,
            trusted_instruments=instruments,
        )
        self.assertTrue(ordinary_gate.allowed, ordinary_gate.block_codes)
        self.assertEqual(ordinary_plan.ordinary_turnover_ratio, D("0.100000"))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "adaptive-orders.sqlite"
            with OrderStore(path) as store:
                broker = PaperBroker(account, instruments, fee_schedule, store)
                first = broker.execute(ordinary_plan, ordinary_gate.approval, NOW)

                exit_time = NOW + timedelta(seconds=1)
                exit_intent = intent(
                    PortfolioIntentType.ACCOUNT_DRAWDOWN_EXIT,
                    {},
                    ZERO,
                    intent_id="drawdown-exit",
                    decision_at=exit_time,
                )
                exit_plan = build_rebalance_plan_from_intent(
                    first.account,
                    exit_intent,
                    instruments,
                    {
                        key: quote(key, as_of=exit_time)
                        for key in first.account.positions
                    },
                    fee_schedule,
                    configured_limits,
                    exit_time,
                    attempt_id="drawdown-exit-attempt",
                )
                # C was bought earlier in the same session and remains T+1
                # unsellable.  The sellable A/B exposure must still be allowed
                # through the ordinary-turnover gate.
                self.assertGreater(exit_plan.turnover_ratio, D("0.79"))
                self.assertEqual(exit_plan.ordinary_turnover_ratio, ZERO)
                self.assertIn(
                    "sell_blocked_t_plus_one",
                    {item.code for item in exit_plan.blocked_exit_reasons},
                )
                exit_gate = approve(
                    first.account,
                    exit_plan,
                    configured_limits,
                    decision_time=exit_time,
                    daily_pnl_ratio=D("-0.12"),
                    daily_turnover_ratio_before_plan=ordinary_plan.turnover_ratio,
                    trusted_fee_schedule=fee_schedule,
                    trusted_instruments=instruments,
                )
                self.assertTrue(exit_gate.allowed, exit_gate.block_codes)

                exited = broker.execute(exit_plan, exit_gate.approval, exit_time)
                usage = store.daily_usage(STRATEGY_ID, NOW.date().isoformat())
                self.assertEqual(set(exited.account.positions), {"CCC"})
                self.assertEqual(exited.account.positions["CCC"].sellable_quantity, 0)
                self.assertEqual(usage.ordinary_notional, D("1000"))
                self.assertEqual(usage.notional, D("9000"))

        rotation_intent = intent(
            PortfolioIntentType.ALPHA_REBALANCE,
            {"CCC": D("0.40"), "DDD": D("0.40")},
            D("0.80"),
            intent_id="ordinary-full-rotation",
        )
        rotation_plan = build_rebalance_plan_from_intent(
            account,
            rotation_intent,
            instruments,
            quotes,
            fee_schedule,
            configured_limits,
            NOW,
            attempt_id="ordinary-full-rotation-attempt",
        )
        self.assertGreater(rotation_plan.ordinary_turnover_ratio, D("0.25"))
        self.assertIn(
            "turnover_limit_exceeded", {item.code for item in rotation_plan.rejections}
        )
        rotation_gate = approve(
            account,
            rotation_plan,
            configured_limits,
            decision_time=NOW,
            trusted_fee_schedule=fee_schedule,
            trusted_instruments=instruments,
        )
        self.assertFalse(rotation_gate.allowed)
        self.assertIn("turnover_limit_exceeded", rotation_gate.block_codes)

    def test_one_blocked_exit_does_not_block_another_sellable_position(self) -> None:
        account = AccountSnapshot(
            STRATEGY_ID,
            D("2000"),
            {"AAA": Position("AAA", 400, 400), "BBB": Position("BBB", 400, 400)},
        )
        instruments = {item: rule(item) for item in account.positions}
        cash_intent = intent(PortfolioIntentType.RISK_OFF, {}, ZERO)
        configured_limits = limits()

        cases = (
            ("suspended", {"suspended": True}, "sell_blocked_suspended"),
            ("limit_down", {"sell_blocked": True}, "sell_blocked_limit_down"),
        )
        for name, flags, expected_code in cases:
            with self.subTest(name=name):
                quotes = {
                    "AAA": quote("AAA"),
                    "BBB": quote("BBB", **flags),
                }
                plan = build_rebalance_plan_from_intent(
                    account,
                    cash_intent,
                    instruments,
                    quotes,
                    fees(),
                    configured_limits,
                    NOW,
                    attempt_id=f"blocked-{name}",
                )
                self.assertEqual([order.instrument_id for order in plan.orders], ["AAA"])
                self.assertEqual(plan.rejections, ())
                self.assertIn(
                    expected_code, {item.code for item in plan.blocked_exit_reasons}
                )
                self.assertEqual(plan.target_gross_exposure, ZERO)
                expected_feasible = (D("4000") / D("9995")).quantize(
                    EXPOSURE_QUANTUM
                )
                self.assertEqual(plan.feasible_gross_exposure, expected_feasible)
                gate = approve(
                    account,
                    plan,
                    configured_limits,
                    decision_time=NOW,
                    trusted_quotes=quotes,
                )
                self.assertTrue(gate.allowed, gate.block_codes)

        fully_blocked = build_rebalance_plan_from_intent(
            account,
            cash_intent,
            instruments,
            {"AAA": quote("AAA", suspended=True), "BBB": quote("BBB", sell_blocked=True)},
            fees(),
            configured_limits,
            NOW,
            attempt_id="fully-blocked-exit",
        )
        self.assertEqual(fully_blocked.orders, ())
        fully_blocked_gate = approve(
            account, fully_blocked, configured_limits, decision_time=NOW
        )
        self.assertFalse(fully_blocked_gate.allowed)
        self.assertIn(
            "exit_attempt_has_no_executable_orders", fully_blocked_gate.block_codes
        )

    def test_full_exit_partial_sellability_covers_order_and_residual(self) -> None:
        account = AccountSnapshot(
            STRATEGY_ID,
            D("6000"),
            {"AAA": Position("AAA", 400, 200)},
        )
        configured_limits = limits()
        execution_quotes = {"AAA": quote("AAA")}
        plan = build_rebalance_plan_from_intent(
            account,
            intent(
                PortfolioIntentType.RISK_OFF,
                {},
                ZERO,
                intent_id="partial-sellability-risk-off",
            ),
            {"AAA": rule("AAA")},
            execution_quotes,
            fees(),
            configured_limits,
            NOW,
            attempt_id="partial-sellability-attempt",
        )

        self.assertEqual([(item.instrument_id, item.quantity) for item in plan.orders], [("AAA", 200)])
        self.assertEqual(
            [(item.instrument_id, item.code) for item in plan.blocked_exit_reasons],
            [("AAA", "sell_blocked_t_plus_one")],
        )
        approved = approve(
            account,
            plan,
            configured_limits,
            decision_time=NOW,
            trusted_quotes=execution_quotes,
        )
        self.assertTrue(approved.allowed, approved.block_codes)

        incomplete = replace(plan, blocked_exit_reasons=())
        rejected = approve(
            account,
            incomplete,
            configured_limits,
            decision_time=NOW,
            trusted_quotes=execution_quotes,
        )
        self.assertFalse(rejected.allowed)
        self.assertIn("exit_plan_coverage_incomplete", rejected.block_codes)

    def test_risk_reduction_allows_residual_overweight_and_position_count(self) -> None:
        positions = {
            "AAA": Position("AAA", 500, 500),
            "BBB": Position("BBB", 100, 100),
            "CCC": Position("CCC", 100, 100),
            "DDD": Position("DDD", 100, 100),
            "EEE": Position("EEE", 100, 100),
        }
        account = AccountSnapshot(STRATEGY_ID, D("1000"), positions)
        execution_quotes = {
            instrument_id: quote(
                instrument_id,
                suspended=instrument_id != "EEE",
            )
            for instrument_id in positions
        }
        configured_limits = limits()
        plan = build_rebalance_plan_from_intent(
            account,
            intent(
                PortfolioIntentType.RISK_OFF,
                {},
                ZERO,
                intent_id="residual-concentration-risk-off",
            ),
            {instrument_id: rule(instrument_id) for instrument_id in positions},
            execution_quotes,
            fees(),
            configured_limits,
            NOW,
            attempt_id="residual-concentration-attempt",
        )

        self.assertEqual([order.instrument_id for order in plan.orders], ["EEE"])
        self.assertEqual(len(plan.blocked_exit_reasons), 4)
        gate = approve(
            account,
            plan,
            configured_limits,
            decision_time=NOW,
            trusted_quotes=execution_quotes,
        )
        self.assertTrue(gate.allowed, gate.block_codes)

    def test_intent_attempt_idempotency_survives_restart_and_next_day_retries(self) -> None:
        account = AccountSnapshot(
            STRATEGY_ID,
            D("2000"),
            {"AAA": Position("AAA", 400, 400), "BBB": Position("BBB", 400, 400)},
        )
        instruments = {item: rule(item) for item in account.positions}
        configured_limits = limits()
        cash_intent = intent(
            PortfolioIntentType.RISK_OFF,
            {},
            ZERO,
            intent_id="sticky-risk-off-intent",
        )
        first_quotes = {
            "AAA": quote("AAA"),
            "BBB": quote("BBB", suspended=True),
        }
        first_plan = build_rebalance_plan_from_intent(
            account,
            cash_intent,
            instruments,
            first_quotes,
            fees(),
            configured_limits,
            NOW,
            attempt_id="attempt-20260820",
        )
        first_gate = approve(
            account,
            first_plan,
            configured_limits,
            decision_time=NOW,
            trusted_quotes=first_quotes,
        )
        self.assertTrue(first_gate.allowed, first_gate.block_codes)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "retry.sqlite"
            with OrderStore(path) as store:
                first_broker = PaperBroker(account, instruments, fees(), store)
                first = first_broker.execute(first_plan, first_gate.approval, NOW)
            with OrderStore(path) as restarted_store:
                restarted = PaperBroker(first.account, instruments, fees(), restarted_store)
                replay = restarted.execute(first_plan, first_gate.approval, NOW)
                self.assertEqual(replay.account, first.account)
                self.assertTrue(all(item.status == "DUPLICATE" for item in replay.fills))

                next_day = NOW + timedelta(days=1)
                reconciled = replace(
                    first.account,
                    snapshot_id="reconciled-20260821",
                    as_of=next_day,
                )
                retry_plan = build_rebalance_plan_from_intent(
                    reconciled,
                    cash_intent,
                    instruments,
                    {"BBB": quote("BBB", as_of=next_day)},
                    fees(),
                    configured_limits,
                    next_day,
                    attempt_id="attempt-20260821",
                    parent_attempt=first_plan,
                    previous_controlled_session=NOW.date(),
                    controlled_calendar_sha256=CALENDAR_SHA256,
                    controlled_calendar_sessions=CONTROLLED_CALENDAR_SESSIONS,
                )
                self.assertEqual(retry_plan.intent_id, first_plan.intent_id)
                self.assertNotEqual(retry_plan.attempt_id, first_plan.attempt_id)
                self.assertNotEqual(
                    retry_plan.orders[0].client_order_id,
                    first_plan.orders[0].client_order_id,
                )
                retry_gate = approve(
                    reconciled,
                    retry_plan,
                    configured_limits,
                    decision_time=next_day,
                    controlled_calendar_sessions=CONTROLLED_CALENDAR_SESSIONS,
                    trusted_instruments=instruments,
                )
                self.assertTrue(retry_gate.allowed, retry_gate.block_codes)
                restarted_store.reconcile_paper_account(
                    reconciled,
                    next_day,
                    expected_fingerprint=account_fingerprint(first.account),
                )
                reconciled_broker = PaperBroker(
                    reconciled, instruments, fees(), restarted_store
                )
                final = reconciled_broker.execute(
                    retry_plan, retry_gate.approval, next_day
                )
                self.assertEqual(final.account.positions, {})

    def test_cross_session_attempts_require_reduction_lineage_and_reconciliation(self) -> None:
        configured_limits = limits(max_positions=99, max_position_weight=D("1"))
        next_day = NOW + timedelta(days=1)
        account = AccountSnapshot(STRATEGY_ID, D("10000"), {})
        alpha_intent = intent(
            PortfolioIntentType.ALPHA_REBALANCE,
            {"AAA": D("0.20")},
            D("0.20"),
            intent_id="same-session-alpha",
        )

        with self.assertRaisesRegex(ValueError, "same execution session"):
            build_rebalance_plan_from_intent(
                account,
                alpha_intent,
                {"AAA": rule("AAA")},
                {"AAA": quote("AAA", as_of=next_day)},
                fees(),
                configured_limits,
                next_day,
                attempt_id="future-alpha-attempt",
            )

        exit_account = AccountSnapshot(
            STRATEGY_ID,
            D("6000"),
            {"AAA": Position("AAA", 400, 400)},
            snapshot_id="initial-exit-account",
            as_of=NOW,
        )
        risk_off = intent(
            PortfolioIntentType.RISK_OFF,
            {},
            ZERO,
            intent_id="cross-session-risk-off",
        )
        first_plan = build_rebalance_plan_from_intent(
            exit_account,
            risk_off,
            {"AAA": rule("AAA")},
            {"AAA": quote("AAA", suspended=True)},
            fees(),
            configured_limits,
            NOW,
            attempt_id="risk-off-day-1",
        )
        reconciled = replace(
            exit_account,
            snapshot_id="reconciled-exit-account",
            as_of=next_day,
        )
        first_d_plus_one = build_rebalance_plan_from_intent(
            reconciled,
            risk_off,
            {"AAA": rule("AAA")},
            {"AAA": quote("AAA", as_of=next_day)},
            fees(),
            configured_limits,
            next_day,
            attempt_id="risk-off-day-2-no-parent",
            previous_controlled_session=NOW.date(),
            controlled_calendar_sha256=CALENDAR_SHA256,
            controlled_calendar_sessions=CONTROLLED_CALENDAR_SESSIONS,
        )
        self.assertIsNone(first_d_plus_one.parent_attempt_id)
        first_d_plus_one_gate = approve(
            reconciled,
            first_d_plus_one,
            configured_limits,
            decision_time=next_day,
            controlled_calendar_sessions=CONTROLLED_CALENDAR_SESSIONS,
        )
        self.assertTrue(
            first_d_plus_one_gate.allowed,
            first_d_plus_one_gate.block_codes,
        )
        with self.assertRaisesRegex(ValueError, "reconciled account snapshot"):
            build_rebalance_plan_from_intent(
                exit_account,
                risk_off,
                {"AAA": rule("AAA")},
                {"AAA": quote("AAA", as_of=next_day)},
                fees(),
                configured_limits,
                next_day,
                attempt_id="risk-off-day-2-stale-account",
                parent_attempt=first_plan,
                previous_controlled_session=NOW.date(),
                controlled_calendar_sha256=CALENDAR_SHA256,
                controlled_calendar_sessions=CONTROLLED_CALENDAR_SESSIONS,
            )

        valid_retry = build_rebalance_plan_from_intent(
            reconciled,
            risk_off,
            {"AAA": rule("AAA")},
            {"AAA": quote("AAA", as_of=next_day)},
            fees(),
            configured_limits,
            next_day,
            attempt_id="risk-off-day-2",
            parent_attempt=first_plan,
            previous_controlled_session=NOW.date(),
            controlled_calendar_sha256=CALENDAR_SHA256,
            controlled_calendar_sessions=CONTROLLED_CALENDAR_SESSIONS,
        )
        self.assertEqual(valid_retry.parent_attempt_id, first_plan.attempt_id)
        self.assertTrue(valid_retry.parent_plan_sha256)
        gate = approve(
            reconciled,
            valid_retry,
            configured_limits,
            decision_time=next_day,
            controlled_calendar_sessions=CONTROLLED_CALENDAR_SESSIONS,
        )
        self.assertTrue(gate.allowed, gate.block_codes)

        disguised_future_alpha = replace(
            build_rebalance_plan_from_intent(
                account,
                alpha_intent,
                {"AAA": rule("AAA")},
                {"AAA": quote("AAA")},
                fees(),
                configured_limits,
                NOW,
                attempt_id="same-day-alpha-attempt",
            ),
            decision_time=next_day,
        )
        disguised_gate = approve(
            account,
            disguised_future_alpha,
            configured_limits,
            decision_time=next_day,
        )
        self.assertFalse(disguised_gate.allowed)
        self.assertIn("alpha_intent_cross_session", disguised_gate.block_codes)

    def test_adaptive_v2_limits_cannot_be_relaxed_by_risk_limits(self) -> None:
        with self.assertRaisesRegex(ValueError, "at most 3"):
            intent(
                PortfolioIntentType.ALPHA_REBALANCE,
                {item: D("0.25") for item in ("AAA", "BBB", "CCC", "DDD")},
                D("1"),
                intent_id="four-by-twenty-five",
            )
        with self.assertRaisesRegex(ValueError, "0.40"):
            intent(
                PortfolioIntentType.ALPHA_REBALANCE,
                {"AAA": D("0.41")},
                D("0.41"),
                intent_id="overweight-single-name",
            )
        with self.assertRaisesRegex(ValueError, "zero-weight target entries"):
            intent(
                PortfolioIntentType.ALPHA_REBALANCE,
                {"AAA": D("0.40"), "UNUSED": ZERO},
                D("0.40"),
                intent_id="zero-weight-schema-bypass",
            )
        with self.assertRaisesRegex(ValueError, "intent_id"):
            intent(
                PortfolioIntentType.ALPHA_REBALANCE,
                {"AAA": D("0.20")},
                D("0.20"),
                intent_id="bad intent id",
            )
        with self.assertRaisesRegex(ValueError, "instrument id"):
            intent(
                PortfolioIntentType.ALPHA_REBALANCE,
                {"aaa": D("0.20")},
                D("0.20"),
                intent_id="bad-instrument-id",
            )
        valid = intent(
            PortfolioIntentType.ALPHA_REBALANCE,
            {"AAA": D("0.20")},
            D("0.20"),
            intent_id="reason-pattern-base",
        )
        with self.assertRaisesRegex(ValueError, "unique"):
            replace(valid, reason_codes=("valid_reason", "valid_reason"))
        with self.assertRaisesRegex(ValueError, "lowercase"):
            replace(valid, reason_codes=("INVALID_REASON",))

        schema = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "schemas"
                / "portfolio_intent.v1.json"
            ).read_text(encoding="utf-8")
        )
        position_weight = schema["$defs"]["positionWeight"]

        def schema_accepts(value: str) -> bool:
            return bool(re.fullmatch(position_weight["pattern"], value)) and not bool(
                re.fullmatch(position_weight["not"]["pattern"], value)
            )

        for accepted in ("0.0001", "0.1", "0.399", "0.4", "0.40"):
            self.assertTrue(schema_accepts(accepted), accepted)
        for rejected in ("0", "0.0", "0.000", "0.4001", "0.5", "0.9"):
            self.assertFalse(schema_accepts(rejected), rejected)

    def test_fee_adjusted_projected_nav_enforces_single_name_and_intent_caps(self) -> None:
        configured_limits = limits(
            max_positions=99,
            max_position_weight=D("1"),
            max_order_notional_ratio=D("1"),
            max_daily_turnover_ratio=D("0.90"),
        )
        account = AccountSnapshot(STRATEGY_ID, D("10000"), {})
        alpha_intent = intent(
            PortfolioIntentType.ALPHA_REBALANCE,
            {"AAA": D("0.40")},
            D("0.40"),
            intent_id="fee-adjusted-weight-cap",
        )
        execution_quotes = {"AAA": quote("AAA")}
        plan = build_rebalance_plan_from_intent(
            account,
            alpha_intent,
            {"AAA": rule("AAA")},
            execution_quotes,
            fees(),
            configured_limits,
            NOW,
            attempt_id="fee-adjusted-weight-attempt",
        )

        self.assertEqual(plan.orders[0].notional, D("4000"))
        self.assertEqual(plan.orders[0].estimated_fee, D("5.00"))
        self.assertEqual(plan.projected_cash, D("5995.00"))
        self.assertEqual(
            {item.code for item in plan.rejections},
            {
                "position_weight_limit_after_fees",
                "intent_target_exceeded_after_fees",
            },
        )
        gate = approve(
            account,
            plan,
            configured_limits,
            decision_time=NOW,
            trusted_quotes=execution_quotes,
        )
        self.assertFalse(gate.allowed)
        self.assertIn(
            "adaptive_v2_projected_position_weight_exceeded",
            gate.block_codes,
        )
        self.assertIn(
            "projected_position_exceeds_intent_target", gate.block_codes
        )

    def test_drawdown_d_plus_one_first_attempt_requires_controlled_session_evidence(self) -> None:
        self.assertEqual(
            CALENDAR_SHA256,
            canonical_sha256(CONTROLLED_CALENDAR_SESSIONS),
        )
        next_session = NOW + timedelta(days=1)
        account = AccountSnapshot(
            STRATEGY_ID,
            D("6000"),
            {"AAA": Position("AAA", 400, 400)},
            snapshot_id="drawdown-d-plus-one-reconciled",
            as_of=next_session,
        )
        drawdown_intent = intent(
            PortfolioIntentType.ACCOUNT_DRAWDOWN_EXIT,
            {},
            ZERO,
            intent_id="drawdown-close-intent",
            decision_at=NOW,
        )
        kwargs = {
            "account": account,
            "portfolio_intent": drawdown_intent,
            "instruments": {"AAA": rule("AAA")},
            "quotes": {"AAA": quote("AAA", as_of=next_session)},
            "fees": fees(),
            "limits": limits(),
            "decision_time": next_session,
            "attempt_id": "drawdown-d-plus-one-first",
        }
        with self.assertRaisesRegex(ValueError, "previous controlled session"):
            build_rebalance_plan_from_intent(**kwargs)
        with self.assertRaisesRegex(ValueError, "calendar session payload"):
            build_rebalance_plan_from_intent(
                **kwargs,
                previous_controlled_session=NOW.date(),
                controlled_calendar_sha256=CALENDAR_SHA256,
            )
        with self.assertRaisesRegex(ValueError, "canonical hash"):
            build_rebalance_plan_from_intent(
                **kwargs,
                previous_controlled_session=NOW.date(),
                controlled_calendar_sha256=sha256_digit(8),
                controlled_calendar_sessions=CONTROLLED_CALENDAR_SESSIONS,
            )
        with self.assertRaisesRegex(ValueError, "strictly adjacent"):
            build_rebalance_plan_from_intent(
                **kwargs,
                previous_controlled_session=NOW.date() - timedelta(days=1),
                controlled_calendar_sha256=canonical_controlled_calendar_sha256(
                    (
                        NOW.date() - timedelta(days=1),
                        NOW.date(),
                        next_session.date(),
                    )
                ),
                controlled_calendar_sessions=(
                    NOW.date() - timedelta(days=1),
                    NOW.date(),
                    next_session.date(),
                ),
            )

        far_session = NOW + timedelta(days=30)
        far_sessions = (
            NOW.date(),
            next_session.date(),
            far_session.date(),
        )
        far_account = replace(
            account,
            snapshot_id="drawdown-far-session-reconciled",
            as_of=far_session,
        )
        with self.assertRaisesRegex(ValueError, "strictly adjacent"):
            build_rebalance_plan_from_intent(
                account=far_account,
                portfolio_intent=drawdown_intent,
                instruments={"AAA": rule("AAA")},
                quotes={"AAA": quote("AAA", as_of=far_session)},
                fees=fees(),
                limits=limits(),
                decision_time=far_session,
                attempt_id="drawdown-d-plus-thirty-bypass",
                previous_controlled_session=NOW.date(),
                controlled_calendar_sha256=(
                    canonical_controlled_calendar_sha256(far_sessions)
                ),
                controlled_calendar_sessions=far_sessions,
            )

        plan = build_rebalance_plan_from_intent(
            **kwargs,
            previous_controlled_session=NOW.date(),
            controlled_calendar_sha256=CALENDAR_SHA256,
            controlled_calendar_sessions=CONTROLLED_CALENDAR_SESSIONS,
        )
        self.assertIsNone(plan.parent_attempt_id)
        self.assertEqual(plan.previous_controlled_session, NOW.date())
        self.assertTrue(plan.controlled_session_evidence_sha256)
        record = execution_plan_record(plan)
        self.assertEqual(
            record["previous_controlled_session"], NOW.date().isoformat()
        )
        self.assertEqual(
            record["controlled_calendar_sha256"], CALENDAR_SHA256
        )
        self.assertFalse(record["official_trading_calendar_proven"])
        missing_payload_gate = approve(
            account,
            plan,
            limits(),
            decision_time=next_session,
        )
        self.assertFalse(missing_payload_gate.allowed)
        self.assertIn(
            "controlled_calendar_payload_missing",
            missing_payload_gate.block_codes,
        )

        gate = approve(
            account,
            plan,
            limits(),
            decision_time=next_session,
            controlled_calendar_sessions=CONTROLLED_CALENDAR_SESSIONS,
        )
        self.assertTrue(gate.allowed, gate.block_codes)

        forged_hash_gate = approve(
            account,
            replace(plan, controlled_calendar_sha256=sha256_digit(8)),
            limits(),
            decision_time=next_session,
            controlled_calendar_sessions=CONTROLLED_CALENDAR_SESSIONS,
        )
        self.assertFalse(forged_hash_gate.allowed)
        self.assertIn(
            "controlled_calendar_hash_mismatch", forged_hash_gate.block_codes
        )

        forged = replace(
            plan,
            previous_controlled_session=NOW.date() - timedelta(days=1),
        )
        forged_gate = approve(
            account,
            forged,
            limits(),
            decision_time=next_session,
            controlled_calendar_sessions=CONTROLLED_CALENDAR_SESSIONS,
        )
        self.assertFalse(forged_gate.allowed)
        self.assertIn(
            "controlled_session_not_adjacent", forged_gate.block_codes
        )

    def test_execution_record_session_uses_bound_intent_timezone(self) -> None:
        local_midnight = datetime(2026, 8, 20, 0, 0, tzinfo=TZ)
        utc_attempt = datetime(2026, 8, 19, 16, 30, tzinfo=timezone.utc)
        alpha_intent = intent(
            PortfolioIntentType.ALPHA_REBALANCE,
            {"AAA": D("0.21")},
            D("0.21"),
            intent_id="timezone-bound-session",
            decision_at=local_midnight,
        )
        execution_quotes = {
            "AAA": quote("AAA", as_of=utc_attempt),
        }
        plan = build_rebalance_plan_from_intent(
            AccountSnapshot(STRATEGY_ID, D("10000"), {}),
            alpha_intent,
            {"AAA": rule("AAA")},
            execution_quotes,
            fees(),
            limits(max_daily_turnover_ratio=D("0.90")),
            utc_attempt,
            attempt_id="timezone-bound-attempt",
        )

        self.assertEqual(plan.decision_time.date().isoformat(), "2026-08-19")
        self.assertEqual(
            execution_plan_record(plan)["execution_session"], "2026-08-20"
        )

    def test_paper_account_reconciliation_uses_compare_and_swap(self) -> None:
        initial = AccountSnapshot(
            STRATEGY_ID,
            D("6000"),
            {"AAA": Position("AAA", 400, 400)},
            snapshot_id="paper-initial",
            as_of=NOW,
        )
        expected = account_fingerprint(initial)
        next_day = NOW + timedelta(days=1)
        stale_refresh = replace(
            initial,
            snapshot_id="paper-stale-refresh",
            as_of=next_day,
        )
        exit_plan = build_rebalance_plan_from_intent(
            initial,
            intent(
                PortfolioIntentType.RISK_OFF,
                {},
                ZERO,
                intent_id="cas-fill-exit",
            ),
            {"AAA": rule("AAA")},
            {"AAA": quote("AAA")},
            fees(),
            limits(),
            NOW,
            attempt_id="cas-fill-attempt",
        )
        exit_gate = approve(
            initial,
            exit_plan,
            limits(),
            decision_time=NOW,
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper-cas.sqlite"
            with OrderStore(path) as first_connection:
                first_connection.ensure_paper_account(initial, NOW)
                with OrderStore(path) as second_connection:
                    filled = PaperBroker(
                        initial,
                        {"AAA": rule("AAA")},
                        fees(),
                        first_connection,
                    ).execute(
                        exit_plan,
                        exit_gate.approval,
                        NOW,
                    )
                    with self.assertRaises(ConcurrentPaperAccountUpdate):
                        second_connection.reconcile_paper_account(
                            stale_refresh,
                            next_day,
                            expected_fingerprint=expected,
                        )
                    self.assertEqual(
                        second_connection.load_paper_account(STRATEGY_ID),
                        filled.account,
                    )

    def test_paper_fill_account_update_uses_atomic_fingerprint_cas(self) -> None:
        initial = AccountSnapshot(
            STRATEGY_ID,
            D("6000"),
            {"AAA": Position("AAA", 400, 400)},
            snapshot_id="fill-cas-initial",
            as_of=NOW,
        )
        frozen = intent(
            PortfolioIntentType.RISK_OFF,
            {},
            ZERO,
            intent_id="fill-cas-intent",
        )
        plan = build_rebalance_plan_from_intent(
            initial,
            frozen,
            {"AAA": rule("AAA")},
            {"AAA": quote("AAA")},
            fees(),
            limits(),
            NOW,
            attempt_id="fill-cas-attempt",
        )
        order = plan.orders[0]
        original_fingerprint = account_fingerprint(initial)
        next_day = NOW + timedelta(days=1)
        refreshed = replace(
            initial,
            snapshot_id="fill-cas-concurrent-refresh",
            as_of=next_day,
        )
        stale_fill_account = AccountSnapshot(
            STRATEGY_ID,
            D("9995.00"),
            {},
            snapshot_id="fill-cas-stale-fill",
            as_of=next_day,
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fill-cas.sqlite"
            with OrderStore(path) as first:
                first.ensure_paper_account(initial, NOW)
                first.register_plan(plan, NOW)
                first.transition(
                    order.client_order_id,
                    OrderStatus.RISK_APPROVED,
                    NOW,
                )
                with OrderStore(path) as second:
                    second.reconcile_paper_account(
                        refreshed,
                        next_day,
                        expected_fingerprint=original_fingerprint,
                    )
                    with self.assertRaises(ConcurrentPaperAccountUpdate):
                        first.commit_paper_fill(
                            order.client_order_id,
                            next_day,
                            {"probe": "stale"},
                            stale_fill_account,
                            False,
                            expected_fingerprint=original_fingerprint,
                        )
                    self.assertEqual(
                        second.load_paper_account(STRATEGY_ID),
                        refreshed,
                    )
                    self.assertIs(
                        second.status(order.client_order_id),
                        OrderStatus.RISK_APPROVED,
                    )
                    self.assertNotIn(
                        OrderStatus.SUBMITTING,
                        {event.status for event in second.events(order.client_order_id)},
                    )

    def test_rule_bundle_binds_fees_all_instruments_and_whole_lot_policy(self) -> None:
        instrument_rules = {"BBB": rule("BBB"), "AAA": rule("AAA")}
        canonical = execution_rule_bundle_sha256(fees(), instrument_rules)
        self.assertEqual(
            canonical,
            execution_rule_bundle_sha256(
                fees(),
                {"AAA": rule("AAA"), "BBB": rule("BBB")},
            ),
        )
        self.assertNotEqual(
            canonical,
            execution_rule_bundle_sha256(
                FeeSchedule(D("0.0003"), D("6"), ZERO),
                instrument_rules,
            ),
        )
        changed_rules = dict(instrument_rules)
        changed_rules["AAA"] = replace(rule("AAA"), lot_size=300)
        self.assertNotEqual(
            canonical,
            execution_rule_bundle_sha256(fees(), changed_rules),
        )

        account = AccountSnapshot(STRATEGY_ID, D("10000"), {})
        frozen = intent(
            PortfolioIntentType.ALPHA_REBALANCE,
            {"AAA": D("0.21")},
            D("0.21"),
            intent_id="rule-bundle-intent",
        )
        one_rule = {"AAA": rule("AAA")}
        plan = build_rebalance_plan_from_intent(
            account,
            frozen,
            one_rule,
            {"AAA": quote("AAA")},
            fees(),
            limits(),
            NOW,
            attempt_id="rule-bundle-attempt",
        )
        self.assertEqual(
            plan.execution_rule_bundle_sha256,
            execution_rule_bundle_sha256(fees(), one_rule),
        )
        record = execution_plan_record(plan)
        self.assertEqual(record["schema_version"], "portfolio-execution-plan.v2")
        self.assertEqual(
            record["execution_rule_bundle_sha256"],
            plan.execution_rule_bundle_sha256,
        )
        schema_root = Path(__file__).resolve().parents[1] / "schemas"
        schema_v1 = json.loads(
            (schema_root / "portfolio_execution_plan.v1.json").read_text(
                encoding="utf-8"
            )
        )
        schema_v2 = json.loads(
            (schema_root / "portfolio_execution_plan.v2.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema_v1["properties"]["schema_version"]["const"],
            "portfolio-execution-plan.v1",
        )
        self.assertNotIn(
            "execution_rule_bundle_sha256",
            schema_v1["properties"],
        )
        self.assertEqual(
            schema_v2["properties"]["schema_version"]["const"],
            "portfolio-execution-plan.v2",
        )
        self.assertIn(
            "execution_rule_bundle_sha256",
            schema_v2["required"],
        )
        validate_json_schema(
            record,
            schema_root / "portfolio_execution_plan.v2.json",
        )
        for no_buy_intent in (
            "DEFENSIVE_REDUCTION",
            "DATA_FAIL_CLOSED",
            "MANUAL_PAUSE",
        ):
            attacked_record = dict(record)
            attacked_record["intent_type"] = no_buy_intent
            with self.assertRaises(SchemaValidationError):
                validate_json_schema(
                    attacked_record,
                    schema_root / "portfolio_execution_plan.v2.json",
                )
        gate = approve(
            account,
            plan,
            limits(),
            decision_time=NOW,
            trusted_instruments={"AAA": changed_rules["AAA"]},
        )
        self.assertFalse(gate.allowed)
        self.assertIn("execution_rule_bundle_sha256_mismatch", gate.block_codes)
        self.assertIn("order_quantity_not_whole_lot", gate.block_codes)

        with OrderStore(":memory:") as store:
            broker = PaperBroker(
                account,
                {"AAA": changed_rules["AAA"]},
                fees(),
                store,
            )
            valid_gate = approve(
                account,
                plan,
                limits(),
                decision_time=NOW,
            )
            with self.assertRaisesRegex(ValueError, "rule bundle"):
                broker.execute(plan, valid_gate.approval, NOW)
            with self.assertRaises(KeyError):
                store.status(plan.orders[0].client_order_id)

    def test_full_batch_preflight_finishes_before_any_submitting_event(self) -> None:
        configured_limits = limits(max_daily_turnover_ratio=D("0.90"))
        account = AccountSnapshot(STRATEGY_ID, D("10000"), {})
        instrument_rules = {item: rule(item) for item in ("AAA", "BBB")}
        execution_quotes = {item: quote(item) for item in instrument_rules}
        frozen = intent(
            PortfolioIntentType.ALPHA_REBALANCE,
            {"AAA": D("0.21"), "BBB": D("0.21")},
            D("0.42"),
            intent_id="batch-preflight-intent",
        )
        plan = build_rebalance_plan_from_intent(
            account,
            frozen,
            instrument_rules,
            execution_quotes,
            fees(),
            configured_limits,
            NOW,
            attempt_id="batch-preflight-attempt",
        )
        gate = approve(
            account,
            plan,
            configured_limits,
            decision_time=NOW,
            trusted_quotes=execution_quotes,
            trusted_instruments=instrument_rules,
        )
        self.assertTrue(gate.allowed, gate.block_codes)
        first_order, second_order = plan.orders

        with OrderStore(":memory:") as store:
            broker = PaperBroker(
                account,
                instrument_rules,
                fees(),
                store,
            )
            store.register_plan(plan, NOW)
            store.transition(
                second_order.client_order_id,
                OrderStatus.BLOCKED,
                NOW,
            )
            with self.assertRaisesRegex(ValueError, "terminal but not filled"):
                broker.execute(plan, gate.approval, NOW)
            self.assertIs(
                store.status(first_order.client_order_id),
                OrderStatus.PLANNED,
            )
            self.assertNotIn(
                OrderStatus.SUBMITTING,
                {event.status for event in store.events(first_order.client_order_id)},
            )

    def test_legacy_orders_table_migration_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy-orders.sqlite"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    """
                    CREATE TABLE orders (
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
                connection.execute(
                    """
                    INSERT INTO orders (
                        client_order_id, plan_id, strategy_id, instrument_id,
                        side, quantity, limit_price, estimated_fee, status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "legacy-order-id",
                        "legacy-plan-id",
                        "small-account-paper-v1",
                        "AAA",
                        "BUY",
                        100,
                        "10",
                        "5.00",
                        OrderStatus.PLANNED.value,
                        NOW.isoformat(),
                        NOW.isoformat(),
                    ),
                )
                connection.commit()

            for _ in range(2):
                with OrderStore(path) as store:
                    self.assertIs(
                        store.status("legacy-order-id"), OrderStatus.PLANNED
                    )

            with closing(sqlite3.connect(path)) as connection:
                migrated_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(orders)"
                    ).fetchall()
                }
                migrated = connection.execute(
                    """
                    SELECT intent_id, attempt_id, risk_direction
                    FROM orders WHERE client_order_id = ?
                    """,
                    ("legacy-order-id",),
                ).fetchone()
            self.assertTrue(
                {"intent_id", "attempt_id", "risk_direction"}
                <= migrated_columns
            )
            self.assertEqual(migrated, ("", "", "RISK_NEUTRAL"))

    def test_full_exposure_target_preserves_real_cash_and_distinct_exposures(self) -> None:
        configured_limits = limits(
            max_positions=99,
            max_position_weight=D("1"),
            max_order_notional_ratio=D("1"),
        )
        account = AccountSnapshot(STRATEGY_ID, D("10000"), {})
        full_intent = intent(
            PortfolioIntentType.ALPHA_REBALANCE,
            {"AAA": D("0.40"), "BBB": D("0.30"), "CCC": D("0.30")},
            D("1"),
            intent_id="full-exposure-target",
        )
        market_quotes = {
            "AAA": quote("AAA", "33.33"),
            "BBB": quote("BBB", "20"),
            "CCC": quote("CCC", "25"),
        }
        plan = build_rebalance_plan_from_intent(
            account,
            full_intent,
            {item: rule(item) for item in market_quotes},
            market_quotes,
            fees(),
            configured_limits,
            NOW,
            attempt_id="full-exposure-attempt",
            bootstrap=True,
        )

        self.assertEqual(plan.target_gross_exposure, D("1"))
        self.assertEqual(len(plan.orders), 3)
        self.assertGreater(plan.projected_cash, ZERO)
        projected_position_value = sum(
            market_quotes[order.instrument_id].last * order.quantity
            for order in plan.orders
        )
        projected_nav = plan.projected_cash + projected_position_value
        expected_feasible = (projected_position_value / projected_nav).quantize(
            EXPOSURE_QUANTUM
        )
        self.assertEqual(plan.feasible_gross_exposure, expected_feasible)
        self.assertLess(plan.feasible_gross_exposure, plan.target_gross_exposure)
        self.assertIsNone(plan.realized_gross_exposure)
        intent_record = full_intent.to_dict()
        plan_record = execution_plan_record(plan)
        self.assertEqual(intent_record["intent_sha256"], plan.intent_sha256)
        self.assertEqual(plan_record["target_gross_exposure"], "1")
        self.assertEqual(
            plan_record["feasible_gross_exposure"],
            str(plan.feasible_gross_exposure),
        )
        self.assertIsNone(plan_record["realized_gross_exposure"])
        self.assertFalse(plan_record["live_supported"])
        self.assertIsNone(plan_record["previous_controlled_session"])
        self.assertFalse(plan_record["official_trading_calendar_proven"])

        gate = approve(
            account,
            plan,
            configured_limits,
            decision_time=NOW,
            trusted_quotes=market_quotes,
        )
        self.assertTrue(gate.allowed, gate.block_codes)
        with tempfile.TemporaryDirectory() as directory:
            with OrderStore(Path(directory) / "full.sqlite") as store:
                result = PaperBroker(
                    account,
                    {item: rule(item) for item in market_quotes},
                    fees(),
                    store,
                ).execute(plan, gate.approval, NOW)
        realized_position_value = sum(
            market_quotes[item].last * position.quantity
            for item, position in result.account.positions.items()
        )
        realized_nav = result.account.cash + realized_position_value
        realized_exposure = (realized_position_value / realized_nav).quantize(
            EXPOSURE_QUANTUM
        )
        self.assertGreater(result.account.cash, ZERO)
        self.assertLess(realized_exposure, plan.target_gross_exposure)
        self.assertEqual(realized_exposure, plan.feasible_gross_exposure)

    def test_v2_gate_recomputes_intent_order_and_plan_bindings(self) -> None:
        configured_limits = limits(max_positions=99, max_position_weight=D("1"))
        account = AccountSnapshot(STRATEGY_ID, D("10000"), {})
        alpha_intent = intent(
            PortfolioIntentType.ALPHA_REBALANCE,
            {"AAA": D("0.21")},
            D("0.21"),
            intent_id="hash-bound-alpha",
        )
        execution_quotes = {"AAA": quote("AAA")}
        plan = build_rebalance_plan_from_intent(
            account,
            alpha_intent,
            {"AAA": rule("AAA")},
            execution_quotes,
            fees(),
            configured_limits,
            NOW,
            attempt_id="hash-bound-attempt",
        )
        self.assertEqual(
            plan.execution_quote_bundle_sha256,
            execution_quote_bundle_sha256(execution_quotes),
        )
        self.assertEqual(
            plan.execution_quote_bundle_sha256,
            execution_quote_bundle_sha256(
                {
                    "AAA": quote(
                        "AAA",
                        "10.0",
                        as_of=NOW.astimezone(timezone.utc),
                    )
                }
            ),
        )
        self.assertEqual(
            execution_plan_record(plan)["execution_quote_bundle_sha256"],
            plan.execution_quote_bundle_sha256,
        )

        correct_gate = approve(
            account,
            plan,
            configured_limits,
            decision_time=NOW,
            trusted_quotes=execution_quotes,
        )
        self.assertTrue(correct_gate.allowed, correct_gate.block_codes)

        missing_execution_quote_hash = replace(
            plan, execution_quote_bundle_sha256=""
        )
        missing_execution_quote_hash_gate = approve(
            account,
            missing_execution_quote_hash,
            configured_limits,
            decision_time=NOW,
            trusted_quotes=execution_quotes,
        )
        self.assertFalse(missing_execution_quote_hash_gate.allowed)
        self.assertIn(
            "execution_quote_bundle_sha256_missing",
            missing_execution_quote_hash_gate.block_codes,
        )

        tampered_quote_gate = approve(
            account,
            plan,
            configured_limits,
            decision_time=NOW,
            trusted_quotes={"AAA": quote("AAA", "11")},
        )
        self.assertFalse(tampered_quote_gate.allowed)
        self.assertIn(
            "execution_quote_bundle_sha256_mismatch",
            tampered_quote_gate.block_codes,
        )

        buy_blocked_quotes = {"AAA": quote("AAA", buy_blocked=True)}
        rehashed_blocked_plan = replace(
            plan,
            execution_quote_bundle_sha256=execution_quote_bundle_sha256(
                buy_blocked_quotes
            ),
        )
        rehashed_blocked_plan = replace(
            rehashed_blocked_plan,
            plan_id=adaptive_v2_rebalance_plan_id(
                rehashed_blocked_plan.strategy_id,
                rehashed_blocked_plan.intent_id,
                rehashed_blocked_plan.intent_sha256,
                rehashed_blocked_plan.attempt_id,
                rehashed_blocked_plan.parent_attempt_id,
                rehashed_blocked_plan.parent_plan_sha256,
                rehashed_blocked_plan.account_fingerprint,
                (
                    order.client_order_id
                    for order in rehashed_blocked_plan.orders
                ),
                rehashed_blocked_plan.controlled_session_evidence_sha256,
                execution_quote_bundle_sha256=(
                    rehashed_blocked_plan.execution_quote_bundle_sha256
                ),
                execution_rule_bundle_sha256=(
                    rehashed_blocked_plan.execution_rule_bundle_sha256
                ),
            ),
        )
        rehashed_blocked_gate = approve(
            account,
            rehashed_blocked_plan,
            configured_limits,
            decision_time=NOW,
            trusted_quotes=buy_blocked_quotes,
        )
        self.assertFalse(rehashed_blocked_gate.allowed)
        self.assertNotIn(
            "execution_quote_bundle_sha256_mismatch",
            rehashed_blocked_gate.block_codes,
        )
        self.assertNotIn(
            "plan_id_binding_mismatch", rehashed_blocked_gate.block_codes
        )
        self.assertIn(
            "trusted_quote_buy_blocked", rehashed_blocked_gate.block_codes
        )

        missing_hash = replace(plan, intent_sha256="")
        missing_gate = approve(
            account, missing_hash, configured_limits, decision_time=NOW
        )
        self.assertFalse(missing_gate.allowed)
        self.assertIn("invalid_intent_sha256", missing_gate.block_codes)

        mismatched_hash = replace(plan, intent_sha256=sha256_digit(9))
        mismatched_gate = approve(
            account, mismatched_hash, configured_limits, decision_time=NOW
        )
        self.assertFalse(mismatched_gate.allowed)
        self.assertIn("intent_sha256_mismatch", mismatched_gate.block_codes)

        forged_order = replace(plan.orders[0], client_order_id="0" * 24)
        forged_order_plan = replace(plan, orders=(forged_order,))
        forged_order_gate = approve(
            account, forged_order_plan, configured_limits, decision_time=NOW
        )
        self.assertFalse(forged_order_gate.allowed)
        self.assertIn(
            "client_order_id_binding_mismatch", forged_order_gate.block_codes
        )

        forged_plan_id = replace(plan, plan_id="0" * 24)
        forged_plan_gate = approve(
            account, forged_plan_id, configured_limits, decision_time=NOW
        )
        self.assertFalse(forged_plan_gate.allowed)
        self.assertIn("plan_id_binding_mismatch", forged_plan_gate.block_codes)

        rehashed_order = replace(
            plan.orders[0],
            instrument_id="BBB",
            client_order_id=adaptive_v2_client_order_id(
                plan.strategy_id,
                plan.intent_id,
                plan.attempt_id,
                "BBB",
                Side.BUY,
            ),
        )
        rehashed_plan = replace(plan, orders=(rehashed_order,))
        rehashed_plan = replace(
            rehashed_plan,
            plan_id=adaptive_v2_rebalance_plan_id(
                rehashed_plan.strategy_id,
                rehashed_plan.intent_id,
                rehashed_plan.intent_sha256,
                rehashed_plan.attempt_id,
                rehashed_plan.parent_attempt_id,
                rehashed_plan.parent_plan_sha256,
                rehashed_plan.account_fingerprint,
                (order.client_order_id for order in rehashed_plan.orders),
                execution_quote_bundle_sha256=(
                    rehashed_plan.execution_quote_bundle_sha256
                ),
                execution_rule_bundle_sha256=(
                    rehashed_plan.execution_rule_bundle_sha256
                ),
            ),
        )
        rehashed_gate = approve(
            account,
            rehashed_plan,
            configured_limits,
            decision_time=NOW,
            trusted_quotes={"AAA": quote("AAA"), "BBB": quote("BBB")},
        )
        self.assertFalse(rehashed_gate.allowed)
        self.assertNotIn(
            "client_order_id_binding_mismatch", rehashed_gate.block_codes
        )
        self.assertNotIn("plan_id_binding_mismatch", rehashed_gate.block_codes)
        self.assertIn(
            "buy_not_in_positive_intent_targets", rehashed_gate.block_codes
        )

        missing_quotes_gate = ExecutionGate(configured_limits).evaluate(
            mode=ExecutionMode.PAPER,
            plan=plan,
            account=account,
            decision_time=NOW,
            daily_pnl_ratio=ZERO,
            kill_switch_active=False,
            readiness=LiveReadiness(),
        )
        self.assertFalse(missing_quotes_gate.allowed)
        self.assertIn("trusted_quotes_missing", missing_quotes_gate.block_codes)
        self.assertIn(
            "trusted_market_data_binding_missing",
            missing_quotes_gate.block_codes,
        )

    def test_account_plan_tampering_and_live_remain_closed(self) -> None:
        configured_limits = limits()
        account = AccountSnapshot(STRATEGY_ID, D("10000"), {})
        alpha_intent = intent(
            PortfolioIntentType.ALPHA_REBALANCE,
            {"AAA": D("0.21")},
            D("0.21"),
        )
        plan = build_rebalance_plan_from_intent(
            account,
            alpha_intent,
            {"AAA": rule("AAA")},
            {"AAA": quote("AAA")},
            fees(),
            configured_limits,
            NOW,
            attempt_id="tamper-attempt",
        )
        gate = approve(account, plan, configured_limits, decision_time=NOW)
        self.assertTrue(gate.allowed, gate.block_codes)

        stale_account = AccountSnapshot(
            STRATEGY_ID,
            D("8000"),
            {"AAA": Position("AAA", 200, 200)},
        )
        stale_gate = approve(
            stale_account, plan, configured_limits, decision_time=NOW
        )
        self.assertFalse(stale_gate.allowed)
        self.assertIn("stale_account_snapshot", stale_gate.block_codes)

        tampered_plan = replace(plan, projected_cash=plan.projected_cash + D("1"))
        tampered_gate = approve(
            account, tampered_plan, configured_limits, decision_time=NOW
        )
        self.assertFalse(tampered_gate.allowed)
        self.assertIn("projected_cash_mismatch", tampered_gate.block_codes)

        live_gate = ExecutionGate(configured_limits).evaluate(
            mode=ExecutionMode.LIVE,
            plan=plan,
            account=account,
            decision_time=NOW,
            daily_pnl_ratio=ZERO,
            kill_switch_active=False,
            readiness=LiveReadiness(),
        )
        self.assertFalse(live_gate.allowed)
        self.assertEqual(live_gate.block_codes, (LIVE_NOT_SUPPORTED_CODE,))
        with self.assertRaises(SignalRejected) as caught:
            intent_from_signal(
                signal({}, signal_id="live-risk-off"),
                NOW,
                ExecutionMode.LIVE,
                strategy_id=STRATEGY_ID,
                intent_type=PortfolioIntentType.RISK_OFF,
                target_gross_exposure=ZERO,
                reason_codes=("must_stay_closed",),
                signal_sha256=sha256_digit(1),
                model_sha256=sha256_digit(3),
                risk_state_sha256=sha256_digit(4),
            )
        self.assertEqual(caught.exception.code, LIVE_NOT_SUPPORTED_CODE)


if __name__ == "__main__":
    unittest.main()
