import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from trading.config import load_trading_config
from trading.costs import FeeSchedule
from trading.models import (
    AccountSnapshot,
    ExecutionMode,
    InstrumentRule,
    MarketQuote,
    OrderIntent,
    Position,
    Side,
)
from trading.order_store import OrderStore
from trading.paper import PaperBroker
from trading.planner import build_rebalance_plan
from trading.risk import ExecutionGate, LiveReadiness, RiskLimits
from trading.strategy_bridge import SignalEnvelope


ROOT = Path(__file__).resolve().parents[1]
TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 7, 14, 10, 5, tzinfo=TZ)


def D(value) -> Decimal:
    return Decimal(str(value))


def rule(instrument_id="ETF_A") -> InstrumentRule:
    return InstrumentRule(instrument_id, "测试ETF", "ETF", 100, D("0.001"), D("0"), True)


def quote(instrument_id="ETF_A", price="2") -> MarketQuote:
    value = D(price)
    return MarketQuote(instrument_id, value, value, value, NOW)


def fees() -> FeeSchedule:
    return FeeSchedule(D("0.0003"), D("5"), D("0"))


def limits() -> RiskLimits:
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
        max_orders_per_day=3,
    )


def make_plan(account=None, decision_id="decision-001", decision_time=NOW, targets=None):
    account = account or AccountSnapshot("paper-10k", D("10000"), {})
    targets = targets or {"ETF_A": D("0.30")}
    instrument_ids = set(targets) | set(account.positions)
    instruments = {instrument_id: rule(instrument_id) for instrument_id in instrument_ids}
    quotes = {instrument_id: quote(instrument_id) for instrument_id in instrument_ids}
    return account, instruments, build_rebalance_plan(
        account=account,
        target_weights=targets,
        instruments=instruments,
        quotes=quotes,
        fees=fees(),
        limits=limits(),
        decision_time=decision_time,
        bootstrap=True,
        decision_id=decision_id,
    )


def approve(account, plan, decision_time=NOW):
    result = ExecutionGate(limits()).evaluate(
        mode=ExecutionMode.PAPER,
        plan=plan,
        account=account,
        decision_time=decision_time,
        daily_pnl_ratio=D("0"),
        kill_switch_active=False,
        readiness=LiveReadiness(),
    )
    if not result.allowed:
        raise AssertionError(result.block_codes)
    return result.approval


class ExecutionBoundaryTest(unittest.TestCase):
    def test_string_live_mode_cannot_bypass_live_gates(self):
        account, _, plan = make_plan()
        result = ExecutionGate(limits()).evaluate(
            mode="LIVE",  # type: ignore[arg-type]
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

        self.assertFalse(result.allowed)
        self.assertEqual(result.block_codes, ("invalid_execution_mode",))
        self.assertIsNone(result.approval)

    def test_string_false_cannot_impersonate_live_readiness(self):
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            LiveReadiness(
                programmatic_report_confirmed="false",  # type: ignore[arg-type]
                broker_api_authorized="false",  # type: ignore[arg-type]
                account_fee_schedule_verified="false",  # type: ignore[arg-type]
                account_reconciled="false",  # type: ignore[arg-type]
                trading_universe_frozen="false",  # type: ignore[arg-type]
                live_adapter_implemented="false",  # type: ignore[arg-type]
            )

    def test_string_false_cannot_impersonate_trade_eligibility(self):
        with self.assertRaisesRegex(ValueError, "signal flags must be booleans"):
            SignalEnvelope(
                signal_id="bad-signal",
                model_id="model",
                model_admission="approved_for_live",
                source_kind="point_in_time_market_data",
                available_at=NOW,
                frozen_at=NOW,
                data_snapshot_hash="sha256:test",
                synthetic=False,
                trade_eligible="false",  # type: ignore[arg-type]
                target_weights={"ETF_A": D("0.30")},
            )

    def test_string_false_cannot_enable_bootstrap_allowance(self):
        account = AccountSnapshot("paper-10k", D("10000"), {})
        with self.assertRaisesRegex(ValueError, "bootstrap must be a boolean"):
            build_rebalance_plan(
                account=account,
                target_weights={"ETF_A": D("0.30")},
                instruments={"ETF_A": rule("ETF_A")},
                quotes={"ETF_A": quote("ETF_A")},
                fees=fees(),
                limits=limits(),
                decision_time=NOW,
                bootstrap="false",  # type: ignore[arg-type]
                decision_id="bad-bootstrap",
            )

    def test_negative_daily_usage_cannot_expand_limits(self):
        account, _, plan = make_plan()
        result = ExecutionGate(limits()).evaluate(
            ExecutionMode.PAPER,
            plan,
            account,
            NOW,
            D("0"),
            False,
            LiveReadiness(),
            daily_turnover_ratio_before_plan=D("-1"),
            daily_order_count_before_plan=-1,
        )

        self.assertFalse(result.allowed)
        self.assertIn("invalid_daily_turnover_state", result.block_codes)
        self.assertIn("invalid_daily_order_count_state", result.block_codes)

    def test_string_buy_side_is_rejected_at_model_boundary(self):
        with self.assertRaisesRegex(ValueError, "Side enum"):
            OrderIntent(
                client_order_id="bad-side",
                instrument_id="ETF_A",
                side="BUY",  # type: ignore[arg-type]
                quantity=100,
                limit_price=D("2"),
                estimated_fee=D("5"),
                reason="invalid input",
            )

    def test_broker_refuses_a_plan_without_gate_approval(self):
        account, instruments, plan = make_plan()
        with tempfile.TemporaryDirectory() as directory:
            with OrderStore(Path(directory) / "orders.sqlite") as store:
                broker = PaperBroker(account, instruments, fees(), store)
                with self.assertRaisesRegex(ValueError, "gate approval"):
                    broker.execute(plan, None, NOW)

    def test_gate_recomputes_cash_and_turnover_instead_of_trusting_plan(self):
        account, _, plan = make_plan()
        tampered = replace(plan, projected_cash=D("1000"), turnover_ratio=D("0"))

        result = ExecutionGate(limits()).evaluate(
            ExecutionMode.PAPER,
            tampered,
            account,
            NOW,
            D("0"),
            False,
            LiveReadiness(),
        )

        self.assertFalse(result.allowed)
        self.assertIn("projected_cash_mismatch", result.block_codes)
        self.assertIn("turnover_ratio_mismatch", result.block_codes)

    def test_approval_is_bound_to_the_exact_account_snapshot(self):
        account, instruments, plan = make_plan()
        approval = approve(account, plan)
        stale_account = AccountSnapshot(
            "paper-10k",
            D("8000"),
            {"ETF_A": Position("ETF_A", 1000, 1000)},
        )
        with tempfile.TemporaryDirectory() as directory:
            with OrderStore(Path(directory) / "orders.sqlite") as store:
                broker = PaperBroker(stale_account, instruments, fees(), store)
                with self.assertRaisesRegex(ValueError, "current account"):
                    broker.execute(plan, approval, NOW)

    def test_persisted_paper_ledger_rejects_an_old_restart_snapshot(self):
        account, instruments, plan = make_plan()
        approval = approve(account, plan)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orders.sqlite"
            with OrderStore(path) as store:
                first = PaperBroker(account, instruments, fees(), store).execute(plan, approval, NOW)
                self.assertNotEqual(first.account, account)
            with OrderStore(path) as restarted_store:
                with self.assertRaisesRegex(ValueError, "persisted ledger"):
                    PaperBroker(account, instruments, fees(), restarted_store)

    def test_same_frozen_decision_keeps_stable_order_ids(self):
        account, _, first = make_plan(decision_id="frozen-signal-123", decision_time=NOW)
        _, _, second = make_plan(
            account=account,
            decision_id="frozen-signal-123",
            decision_time=NOW + timedelta(seconds=1),
        )

        self.assertEqual(first.plan_id, second.plan_id)
        self.assertEqual(
            [order.client_order_id for order in first.orders],
            [order.client_order_id for order in second.orders],
        )

    def test_mapping_key_mismatch_is_rejected(self):
        account = AccountSnapshot("paper-10k", D("10000"), {})
        with self.assertRaisesRegex(ValueError, "mapping key mismatch"):
            build_rebalance_plan(
                account=account,
                target_weights={"ETF_A": D("0.30")},
                instruments={"ETF_A": rule("ETF_B")},
                quotes={"ETF_A": quote("ETF_A")},
                fees=fees(),
                limits=limits(),
                decision_time=NOW,
                bootstrap=True,
                decision_id="mapping-test",
            )

    def test_config_rejects_string_false_as_boolean(self):
        payload = json.loads(
            (ROOT / "configs" / "small_account_trading.v1.json").read_text(encoding="utf-8")
        )
        payload["live_readiness"]["live_order_submission_enabled"] = "false"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "JSON boolean"):
                load_trading_config(path)

    def test_bootstrap_allowance_is_persisted_and_one_time(self):
        account, instruments, first_plan = make_plan(decision_id="bootstrap-1")
        first_approval = approve(account, first_plan)
        with tempfile.TemporaryDirectory() as directory:
            with OrderStore(Path(directory) / "orders.sqlite") as store:
                broker = PaperBroker(account, instruments, fees(), store)
                first = broker.execute(first_plan, first_approval, NOW)

                second_account = first.account
                second_instruments = {"ETF_A": rule("ETF_A"), "ETF_B": rule("ETF_B")}
                second_quotes = {"ETF_A": quote("ETF_A"), "ETF_B": quote("ETF_B")}
                second_plan = build_rebalance_plan(
                    account=second_account,
                    target_weights={"ETF_A": D("0.30"), "ETF_B": D("0.30")},
                    instruments=second_instruments,
                    quotes=second_quotes,
                    fees=fees(),
                    limits=limits(),
                    decision_time=NOW + timedelta(seconds=1),
                    bootstrap=True,
                    decision_id="bootstrap-2",
                )
                second_approval = approve(
                    second_account, second_plan, NOW + timedelta(seconds=1)
                )

                with self.assertRaisesRegex(ValueError, "already been used"):
                    broker.execute(second_plan, second_approval, NOW + timedelta(seconds=1))


if __name__ == "__main__":
    unittest.main()
