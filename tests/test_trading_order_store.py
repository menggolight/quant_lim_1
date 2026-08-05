import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from trading.costs import FeeSchedule
from trading.models import AccountSnapshot, ExecutionMode, InstrumentRule, MarketQuote, OrderStatus
from trading.order_store import InvalidOrderTransition, OrderStore
from trading.paper import PaperBroker
from trading.planner import build_rebalance_plan
from trading.risk import ExecutionGate, LiveReadiness, RiskLimits


TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 7, 14, 10, 5, tzinfo=TZ)


def D(value: str | int | float) -> Decimal:
    return Decimal(str(value))


def fixture():
    rule = InstrumentRule("ETF_A", "纸面ETF", "ETF", 100, D("0.001"), D("0"), True)
    quote = MarketQuote("ETF_A", D("2"), D("2"), D("2"), NOW)
    fees = FeeSchedule(D("0.0003"), D("5"), D("0"))
    limits = RiskLimits(
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
    account = AccountSnapshot("paper-10k", D("10000"), {})
    plan = build_rebalance_plan(
        account,
        {"ETF_A": D("0.30")},
        {"ETF_A": rule},
        {"ETF_A": quote},
        fees,
        limits,
        NOW,
        bootstrap=True,
        decision_id="order-store-test-001",
    )
    return rule, fees, limits, account, plan


class OrderStoreTest(unittest.TestCase):
    def test_state_and_idempotency_survive_process_restart(self):
        rule, fees, limits, account, plan = fixture()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orders.sqlite"
            store = OrderStore(path)
            approval = ExecutionGate(limits).evaluate(
                ExecutionMode.PAPER,
                plan,
                account,
                NOW,
                Decimal("0"),
                False,
                LiveReadiness(),
            ).approval
            first_broker = PaperBroker(account, {"ETF_A": rule}, fees, order_store=store)
            first = first_broker.execute(plan, approval, NOW)
            store.close()

            restarted_store = OrderStore(path)
            restarted_broker = PaperBroker(first.account, {"ETF_A": rule}, fees, order_store=restarted_store)
            replay = restarted_broker.execute(plan, approval, NOW)
            order_id = plan.orders[0].client_order_id

            self.assertEqual(restarted_store.status(order_id), OrderStatus.FILLED)
            self.assertEqual(replay.account, first.account)
            self.assertEqual(replay.fills[0].status, "DUPLICATE")
            self.assertEqual(len(restarted_store.events(order_id)), 5)
            restarted_store.close()

    def test_invalid_state_transition_is_rejected(self):
        _, _, _, _, plan = fixture()
        with tempfile.TemporaryDirectory() as directory:
            store = OrderStore(Path(directory) / "orders.sqlite")
            store.register_plan(plan, NOW)
            order_id = plan.orders[0].client_order_id

            with self.assertRaises(InvalidOrderTransition):
                store.transition(order_id, OrderStatus.FILLED, NOW)
            store.close()


if __name__ == "__main__":
    unittest.main()
