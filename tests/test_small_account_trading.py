import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from trading.costs import FeeSchedule
from trading.models import (
    AccountSnapshot,
    ExecutionMode,
    InstrumentRule,
    MarketQuote,
    Position,
    Side,
)
from trading.paper import PaperBroker
from trading.order_store import OrderStore
from trading.planner import build_rebalance_plan as _build_rebalance_plan
from trading.risk import ExecutionGate, LiveReadiness, RiskLimits


TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 7, 14, 10, 5, tzinfo=TZ)


def build_rebalance_plan(*args, **kwargs):
    kwargs.setdefault("decision_id", "test-decision-001")
    return _build_rebalance_plan(*args, **kwargs)


def D(value: str | int | float) -> Decimal:
    return Decimal(str(value))


def etf(instrument_id: str, name: str = "测试ETF") -> InstrumentRule:
    return InstrumentRule(
        instrument_id=instrument_id,
        name=name,
        instrument_type="ETF",
        lot_size=100,
        tick_size=D("0.001"),
        sell_stamp_duty_rate=D("0"),
        t_plus_one=True,
    )


def quote(instrument_id: str, price: str) -> MarketQuote:
    value = D(price)
    return MarketQuote(
        instrument_id=instrument_id,
        bid=value,
        ask=value,
        last=value,
        as_of=NOW,
    )


def quote_with_spread(instrument_id: str, bid: str, ask: str, last: str) -> MarketQuote:
    return MarketQuote(
        instrument_id=instrument_id,
        bid=D(bid),
        ask=D(ask),
        last=D(last),
        as_of=NOW,
    )


def fee_schedule() -> FeeSchedule:
    return FeeSchedule(
        commission_rate=D("0.0003"),
        minimum_commission=D("5"),
        exchange_fee_rate=D("0"),
    )


def risk_limits() -> RiskLimits:
    return RiskLimits(
        strategy_capital_limit=D("10000"),
        allowed_instrument_types=("ETF",),
        max_positions=3,
        max_position_weight=D("0.35"),
        cash_reserve_ratio=D("0.10"),
        minimum_trade_notional=D("2000"),
        max_orders_per_plan=3,
        max_order_notional_ratio=D("0.35"),
        max_daily_turnover_ratio=D("0.25"),
        bootstrap_turnover_ratio=D("0.90"),
        maximum_quote_age_seconds=60,
        maximum_daily_loss_ratio=D("0.02"),
    )


class SmallAccountTradingTest(unittest.TestCase):
    def test_bootstrap_plan_uses_three_etfs_whole_lots_and_keeps_cash_reserve(self):
        instruments = {
            "ETF_A": etf("ETF_A", "行业ETF A"),
            "ETF_B": etf("ETF_B", "行业ETF B"),
            "ETF_C": etf("ETF_C", "行业ETF C"),
        }
        quotes = {
            "ETF_A": quote("ETF_A", "2.000"),
            "ETF_B": quote("ETF_B", "1.500"),
            "ETF_C": quote("ETF_C", "0.800"),
        }
        account = AccountSnapshot(strategy_id="paper-10k", cash=D("10000"), positions={})

        plan = build_rebalance_plan(
            account=account,
            target_weights={"ETF_A": D("0.30"), "ETF_B": D("0.30"), "ETF_C": D("0.30")},
            instruments=instruments,
            quotes=quotes,
            fees=fee_schedule(),
            limits=risk_limits(),
            decision_time=NOW,
            bootstrap=True,
        )

        self.assertEqual(len(plan.orders), 3)
        self.assertTrue(all(order.side is Side.BUY for order in plan.orders))
        self.assertTrue(all(order.quantity % 100 == 0 for order in plan.orders))
        self.assertTrue(all(order.estimated_fee == D("5.00") for order in plan.orders))
        self.assertGreaterEqual(plan.projected_cash, D("1000"))
        self.assertLessEqual(plan.turnover_ratio, D("0.90"))
        self.assertTrue(all(order.notional <= D("3500") for order in plan.orders))

    def test_expensive_minimum_lot_and_single_stock_are_rejected(self):
        limits = risk_limits()
        account = AccountSnapshot(strategy_id="paper-10k", cash=D("10000"), positions={})
        expensive_etf = etf("ETF_EXPENSIVE", "高价ETF")
        stock = InstrumentRule(
            instrument_id="STOCK_X",
            name="高价股票",
            instrument_type="EQUITY",
            lot_size=100,
            tick_size=D("0.01"),
            sell_stamp_duty_rate=D("0.0005"),
            t_plus_one=True,
        )

        plan = build_rebalance_plan(
            account=account,
            target_weights={"ETF_EXPENSIVE": D("0.30"), "STOCK_X": D("0.30")},
            instruments={"ETF_EXPENSIVE": expensive_etf, "STOCK_X": stock},
            quotes={"ETF_EXPENSIVE": quote("ETF_EXPENSIVE", "120"), "STOCK_X": quote("STOCK_X", "80")},
            fees=fee_schedule(),
            limits=limits,
            decision_time=NOW,
            bootstrap=True,
        )

        self.assertEqual(plan.orders, ())
        codes = {item.code for item in plan.rejections}
        self.assertIn("minimum_lot_unaffordable", codes)
        self.assertIn("instrument_type_not_allowed", codes)

    def test_frozen_whitelist_rejects_an_otherwise_valid_etf(self):
        limits = replace(risk_limits(), allowed_instrument_ids=("ETF_A",))
        account = AccountSnapshot(strategy_id="paper-10k", cash=D("10000"), positions={})

        plan = build_rebalance_plan(
            account=account,
            target_weights={"ETF_B": D("0.30")},
            instruments={"ETF_B": etf("ETF_B")},
            quotes={"ETF_B": quote("ETF_B", "2")},
            fees=fee_schedule(),
            limits=limits,
            decision_time=NOW,
            bootstrap=True,
        )

        self.assertEqual(plan.orders, ())
        self.assertIn("instrument_not_whitelisted", {item.code for item in plan.rejections})

    def test_wide_spread_is_rejected_before_order_planning(self):
        limits = replace(risk_limits(), maximum_spread_ratio=D("0.003"))
        account = AccountSnapshot(strategy_id="paper-10k", cash=D("10000"), positions={})

        plan = build_rebalance_plan(
            account=account,
            target_weights={"ETF_A": D("0.30")},
            instruments={"ETF_A": etf("ETF_A")},
            quotes={"ETF_A": quote_with_spread("ETF_A", "1.99", "2.01", "2.00")},
            fees=fee_schedule(),
            limits=limits,
            decision_time=NOW,
            bootstrap=True,
        )

        self.assertEqual(plan.orders, ())
        self.assertIn("spread_too_wide", {item.code for item in plan.rejections})

    def test_t_plus_one_sellable_quantity_is_never_bypassed(self):
        account = AccountSnapshot(
            strategy_id="paper-10k",
            cash=D("7000"),
            positions={"ETF_A": Position("ETF_A", quantity=1500, sellable_quantity=0)},
        )

        plan = build_rebalance_plan(
            account=account,
            target_weights={"ETF_A": D("0")},
            instruments={"ETF_A": etf("ETF_A")},
            quotes={"ETF_A": quote("ETF_A", "2")},
            fees=fee_schedule(),
            limits=risk_limits(),
            decision_time=NOW,
            bootstrap=False,
        )

        self.assertFalse(any(order.side is Side.SELL for order in plan.orders))
        self.assertIn("insufficient_sellable_quantity", {item.code for item in plan.rejections})

    def test_non_etf_position_is_never_liquidated_by_the_strategy(self):
        account = AccountSnapshot(
            strategy_id="paper-10k",
            cash=D("2000"),
            positions={"000333.SZ": Position("000333.SZ", quantity=100, sellable_quantity=100)},
        )
        midea = InstrumentRule(
            instrument_id="000333.SZ",
            name="非策略长期持仓",
            instrument_type="EQUITY",
            lot_size=100,
            tick_size=D("0.01"),
            sell_stamp_duty_rate=D("0.0005"),
            t_plus_one=True,
        )

        with self.assertRaisesRegex(ValueError, "non-strategy instrument"):
            build_rebalance_plan(
                account=account,
                target_weights={"000333.SZ": D("0")},
                instruments={"000333.SZ": midea},
                quotes={"000333.SZ": quote("000333.SZ", "80")},
                fees=fee_schedule(),
                limits=risk_limits(),
                decision_time=NOW,
            )

    def test_strategy_equity_cannot_exceed_the_explicit_capital_allocation(self):
        account = AccountSnapshot(strategy_id="paper-10k", cash=D("10000.01"), positions={})

        with self.assertRaisesRegex(ValueError, "capital limit"):
            build_rebalance_plan(
                account=account,
                target_weights={},
                instruments={},
                quotes={},
                fees=fee_schedule(),
                limits=risk_limits(),
                decision_time=NOW,
            )

    def test_existing_unsellable_position_prevents_a_fourth_position(self):
        account = AccountSnapshot(
            strategy_id="paper-10k",
            cash=D("8000"),
            positions={"ETF_OLD": Position("ETF_OLD", quantity=1000, sellable_quantity=0)},
        )
        instruments = {
            "ETF_OLD": etf("ETF_OLD"),
            "ETF_A": etf("ETF_A"),
            "ETF_B": etf("ETF_B"),
            "ETF_C": etf("ETF_C"),
        }
        quotes = {instrument_id: quote(instrument_id, "2") for instrument_id in instruments}

        plan = build_rebalance_plan(
            account=account,
            target_weights={"ETF_A": D("0.21"), "ETF_B": D("0.21"), "ETF_C": D("0.21")},
            instruments=instruments,
            quotes=quotes,
            fees=fee_schedule(),
            limits=risk_limits(),
            decision_time=NOW,
            bootstrap=True,
        )

        self.assertLessEqual(len(plan.orders), 2)
        self.assertIn("max_position_count", {item.code for item in plan.rejections})

    def test_execution_gate_blocks_kill_switch_and_live_permanently(self):
        account = AccountSnapshot(strategy_id="paper-10k", cash=D("10000"), positions={})
        plan = build_rebalance_plan(
            account=account,
            target_weights={"ETF_A": D("0.30")},
            instruments={"ETF_A": etf("ETF_A")},
            quotes={"ETF_A": quote("ETF_A", "2")},
            fees=fee_schedule(),
            limits=risk_limits(),
            decision_time=NOW,
            bootstrap=True,
        )
        gate = ExecutionGate(risk_limits())

        paper = gate.evaluate(
            mode=ExecutionMode.PAPER,
            plan=plan,
            account=account,
            decision_time=NOW,
            daily_pnl_ratio=D("0"),
            kill_switch_active=False,
            readiness=LiveReadiness(),
        )
        live = gate.evaluate(
            mode=ExecutionMode.LIVE,
            plan=plan,
            account=account,
            decision_time=NOW,
            daily_pnl_ratio=D("0"),
            kill_switch_active=False,
            readiness=LiveReadiness(),
        )
        killed = gate.evaluate(
            mode=ExecutionMode.PAPER,
            plan=plan,
            account=account,
            decision_time=NOW,
            daily_pnl_ratio=D("0"),
            kill_switch_active=True,
            readiness=LiveReadiness(),
        )

        self.assertTrue(paper.allowed)
        self.assertFalse(live.allowed)
        self.assertEqual(live.block_codes, ("live_not_supported",))
        self.assertIsNone(live.approval)
        self.assertFalse(killed.allowed)
        self.assertIn("kill_switch_active", set(killed.block_codes))

    def test_live_gate_stays_closed_with_forged_readiness_and_allowlist(self):
        account = AccountSnapshot(strategy_id="paper-10k", cash=D("10000"), positions={})
        plan = build_rebalance_plan(
            account=account,
            target_weights={"ETF_A": D("0.30")},
            instruments={"ETF_A": etf("ETF_A")},
            quotes={"ETF_A": quote("ETF_A", "2")},
            fees=fee_schedule(),
            limits=risk_limits(),
            decision_time=NOW,
            bootstrap=True,
        )
        live_limits = replace(risk_limits(), allowed_instrument_ids=("ETF_A",))
        readiness = LiveReadiness(
            programmatic_report_confirmed=True,
            broker_api_authorized=True,
            account_fee_schedule_verified=True,
            account_reconciled=True,
            trading_universe_frozen=True,
            paper_started_at=NOW - timedelta(days=91),
            paper_trade_events=30,
            shadow_sessions=5,
        )

        result = ExecutionGate(live_limits).evaluate(
            mode=ExecutionMode.LIVE,
            plan=plan,
            account=account,
            decision_time=NOW,
            daily_pnl_ratio=D("0"),
            kill_switch_active=False,
            readiness=readiness,
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.block_codes, ("live_not_supported",))
        self.assertIsNone(result.approval)

    def test_shadow_gate_remains_available_after_paper_stage(self):
        account = AccountSnapshot(strategy_id="paper-10k", cash=D("10000"), positions={})
        plan = build_rebalance_plan(
            account=account,
            target_weights={"ETF_A": D("0.30")},
            instruments={"ETF_A": etf("ETF_A")},
            quotes={"ETF_A": quote("ETF_A", "2")},
            fees=fee_schedule(),
            limits=risk_limits(),
            decision_time=NOW,
            bootstrap=True,
        )

        result = ExecutionGate(risk_limits()).evaluate(
            mode=ExecutionMode.SHADOW,
            plan=plan,
            account=account,
            decision_time=NOW,
            daily_pnl_ratio=D("0"),
            kill_switch_active=False,
            readiness=LiveReadiness(
                paper_started_at=NOW - timedelta(days=91),
                paper_trade_events=30,
            ),
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.block_codes, ())
        self.assertIsNotNone(result.approval)
        self.assertIs(result.approval.mode, ExecutionMode.SHADOW)

    def test_any_partial_plan_rejection_blocks_execution(self):
        account = AccountSnapshot(strategy_id="paper-10k", cash=D("10000"), positions={})
        plan = build_rebalance_plan(
            account=account,
            target_weights={"ETF_A": D("0.30"), "ETF_EXPENSIVE": D("0.30")},
            instruments={"ETF_A": etf("ETF_A"), "ETF_EXPENSIVE": etf("ETF_EXPENSIVE")},
            quotes={"ETF_A": quote("ETF_A", "2"), "ETF_EXPENSIVE": quote("ETF_EXPENSIVE", "120")},
            fees=fee_schedule(),
            limits=risk_limits(),
            decision_time=NOW,
            bootstrap=True,
        )

        result = ExecutionGate(risk_limits()).evaluate(
            mode=ExecutionMode.PAPER,
            plan=plan,
            account=account,
            decision_time=NOW,
            daily_pnl_ratio=D("0"),
            kill_switch_active=False,
            readiness=LiveReadiness(),
        )

        self.assertGreater(len(plan.orders), 0)
        self.assertGreater(len(plan.rejections), 0)
        self.assertFalse(result.allowed)
        self.assertIn("plan_contains_rejections", result.block_codes)

    def test_daily_limits_are_cumulative_across_multiple_plans(self):
        account = AccountSnapshot(strategy_id="paper-10k", cash=D("10000"), positions={})
        plan = build_rebalance_plan(
            account=account,
            target_weights={"ETF_A": D("0.30")},
            instruments={"ETF_A": etf("ETF_A")},
            quotes={"ETF_A": quote("ETF_A", "2")},
            fees=fee_schedule(),
            limits=risk_limits(),
            decision_time=NOW,
            bootstrap=True,
        )

        result = ExecutionGate(risk_limits()).evaluate(
            mode=ExecutionMode.PAPER,
            plan=plan,
            account=account,
            decision_time=NOW,
            daily_pnl_ratio=D("0"),
            kill_switch_active=False,
            readiness=LiveReadiness(),
            daily_turnover_ratio_before_plan=D("0.65"),
            daily_order_count_before_plan=3,
        )

        self.assertFalse(result.allowed)
        self.assertIn("cumulative_daily_turnover_limit_exceeded", result.block_codes)
        self.assertIn("cumulative_daily_order_count_exceeded", result.block_codes)

    def test_paper_broker_is_idempotent_and_reconciles_cash(self):
        account = AccountSnapshot(strategy_id="paper-10k", cash=D("10000"), positions={})
        instruments = {"ETF_A": etf("ETF_A"), "ETF_B": etf("ETF_B"), "ETF_C": etf("ETF_C")}
        quotes = {"ETF_A": quote("ETF_A", "2"), "ETF_B": quote("ETF_B", "1.5"), "ETF_C": quote("ETF_C", "0.8")}
        plan = build_rebalance_plan(
            account=account,
            target_weights={"ETF_A": D("0.30"), "ETF_B": D("0.30"), "ETF_C": D("0.30")},
            instruments=instruments,
            quotes=quotes,
            fees=fee_schedule(),
            limits=risk_limits(),
            decision_time=NOW,
            bootstrap=True,
        )
        gate = ExecutionGate(risk_limits()).evaluate(
            mode=ExecutionMode.PAPER,
            plan=plan,
            account=account,
            decision_time=NOW,
            daily_pnl_ratio=D("0"),
            kill_switch_active=False,
            readiness=LiveReadiness(),
        )
        with tempfile.TemporaryDirectory() as directory:
            store = OrderStore(Path(directory) / "orders.sqlite")
            broker = PaperBroker(
                account=account,
                instruments=instruments,
                fees=fee_schedule(),
                order_store=store,
            )

            first = broker.execute(plan, gate.approval, NOW)
            second = broker.execute(plan, gate.approval, NOW)
            store.close()

            self.assertEqual(first.account.cash, plan.projected_cash)
            self.assertEqual(sum(position.quantity for position in first.account.positions.values()), sum(order.quantity for order in plan.orders))
            self.assertTrue(all(fill.status == "FILLED" for fill in first.fills))
            self.assertTrue(all(fill.status == "DUPLICATE" for fill in second.fills))
            self.assertEqual(second.account, first.account)


if __name__ == "__main__":
    unittest.main()
