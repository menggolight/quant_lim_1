from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from operations.run_technical_shadow_mvp import (
    CapturedData,
    TechnicalShadowRunError,
    _cash_reason_codes,
    _execute_targets,
    _execution_cost,
    _load_config,
    _plan_targets,
    run_replay,
    validate_source_provenance,
)
from research.strategy_workspace.technical_alpha_shadow_v1 import (
    TechnicalAlphaShadowError,
    rank_technical_alpha_shadow,
)
from research.strategy_workspace.technical_exposure_shadow_v1 import (
    compute_technical_shadow_exposure,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "configs" / "a_share_technical_shadow_mvp.v1.json"


def _sessions(count: int = 123) -> tuple[date, ...]:
    start = date(2026, 1, 1)
    return tuple(start + timedelta(days=index) for index in range(count))


def _row(item: str, day: date, close: float, *, open_price: float | None = None) -> dict:
    open_value = close if open_price is None else open_price
    return {
        "instrument_id": item,
        "trading_date": day.isoformat(),
        "open": open_value,
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
        "preclose": close,
        "volume": 1000000.0,
        "amount": close * 1000000,
        "adjustment": "none",
        "trading_status": "traded",
        "is_st": False,
        "available_at": f"{day.isoformat()}T15:30:00+08:00",
    }


def _alpha_inputs(count: int = 121):
    config = _load_config(CONFIG_PATH)
    ids = tuple(config["universe"]["instrument_ids"])
    sessions = _sessions(count)
    benchmark = tuple(_row("000906.SH", day, 100 + index * 0.05) for index, day in enumerate(sessions))
    stocks = {}
    for offset, item in enumerate(ids):
        drift = (offset - 29.5) * 0.0004
        stocks[item] = tuple(
            _row(item, day, 10 + index * (0.02 + drift) + 0.01 * ((index + offset) % 5))
            for index, day in enumerate(sessions)
        )
    return config, ids, sessions, stocks, benchmark


class TechnicalAlphaShadowTests(unittest.TestCase):
    def test_future_data_is_rejected(self):
        _, ids, sessions, stocks, benchmark = _alpha_inputs()
        future = sessions[-1] + timedelta(days=1)
        changed = dict(stocks)
        changed[ids[0]] = stocks[ids[0]] + (_row(ids[0], future, 15),)
        with self.assertRaisesRegex(TechnicalAlphaShadowError, "future_data_rejected"):
            rank_technical_alpha_shadow(
                decision_date=sessions[-1], sessions=sessions, instrument_ids=ids,
                stock_rows=changed, benchmark_rows=benchmark,
            )

    def test_missing_data_excludes_without_zero_fill(self):
        _, ids, sessions, stocks, benchmark = _alpha_inputs()
        changed = dict(stocks)
        changed[ids[0]] = stocks[ids[0]][1:]
        result = rank_technical_alpha_shadow(
            decision_date=sessions[-1], sessions=sessions, instrument_ids=ids,
            stock_rows=changed, benchmark_rows=benchmark,
        )
        row = next(item for item in result if item["instrument_id"] == ids[0])
        self.assertFalse(row["eligibility"])
        self.assertIsNone(row["factors"])
        self.assertIn("missing_common_session", row["exclusion_codes"])

    def test_same_input_is_deterministic_and_returns_full_pool(self):
        _, ids, sessions, stocks, benchmark = _alpha_inputs()
        kwargs = dict(
            decision_date=sessions[-1], sessions=sessions, instrument_ids=ids,
            stock_rows=stocks, benchmark_rows=benchmark,
        )
        first = rank_technical_alpha_shadow(**kwargs)
        second = rank_technical_alpha_shadow(**kwargs)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 60)
        self.assertEqual({item["rank"] for item in first}, set(range(1, 61)))


class PortfolioReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = _load_config(CONFIG_PATH)

    def test_no_candidate_means_all_cash(self):
        ranking = [{"instrument_id": "000001.SZ", "entry_eligible": False, "hold_eligible": False, "rank": 1}]
        targets, selected = _plan_targets(
            ranking=ranking, positions={}, nav=Decimal("10000"), target_exposure=1,
            max_positions=3, max_weight=Decimal("0.40"), lot_size=100,
            close_by_id={"000001.SZ": Decimal("10")},
        )
        self.assertEqual(targets, {})
        self.assertEqual(selected, [])
        self.assertEqual(
            _cash_reason_codes(
                ranking=ranking, positions={}, selected=selected, target_exposure=1,
            ),
            ["NO_ALPHA_CASH"],
        )

    def test_risk_off_with_alpha_candidate_is_not_labeled_no_alpha(self):
        ranking = [{
            "instrument_id": "000001.SZ", "entry_eligible": True,
            "hold_eligible": True, "rank": 1,
        }]
        self.assertEqual(
            _cash_reason_codes(
                ranking=ranking, positions={}, selected=[], target_exposure=0,
            ),
            ["RISK_OFF_CASH"],
        )

    def test_whole_lot_and_cash_never_overdraft(self):
        positions, cash, fills, _ = _execute_targets(
            targets={"000001.SZ": 1000}, positions={}, cash=Decimal("1200"),
            execution_rows={"000001.SZ": {"open": 9.99, "trading_status": "traded", "is_st": False}},
            config=self.config, buy_order=["000001.SZ"],
        )
        self.assertGreaterEqual(cash, 0)
        self.assertEqual(positions.get("000001.SZ", 0) % 100, 0)
        self.assertLess(positions.get("000001.SZ", 0), 1000)
        self.assertEqual(fills[0]["market_open_price"], "9.99")

    def test_d_signal_executes_at_d_plus_1_open_with_slippage(self):
        positions, _, fills, _ = _execute_targets(
            targets={"000001.SZ": 100}, positions={}, cash=Decimal("10000"),
            execution_rows={"000001.SZ": {"open": 10, "trading_status": "traded", "is_st": False}},
            config=self.config, buy_order=["000001.SZ"],
        )
        self.assertEqual(positions, {"000001.SZ": 100})
        self.assertEqual(fills[0]["market_open_price"], "10.00")
        self.assertEqual(fills[0]["simulated_fill_price"], "10.01")

    def test_account_state_is_continuous_across_sell(self):
        positions, cash, _, _ = _execute_targets(
            targets={"000001.SZ": 100}, positions={}, cash=Decimal("10000"),
            execution_rows={"000001.SZ": {"open": 10, "trading_status": "traded", "is_st": False}},
            config=self.config,
        )
        after_buy = cash
        positions, cash, fills, _ = _execute_targets(
            targets={"000001.SZ": 0}, positions=positions, cash=cash,
            execution_rows={"000001.SZ": {"open": 11, "trading_status": "traded", "is_st": False}},
            config=self.config,
        )
        self.assertEqual(positions, {})
        self.assertGreater(cash, after_buy)
        self.assertEqual(fills[0]["action"], "SELL")

    def test_cost_components_recompute(self):
        cost = _execution_cost(
            side="SELL", quantity=100, open_price=Decimal("10"), config=self.config
        )
        self.assertEqual(cost["execution_price"], Decimal("9.990"))
        expected = cost["commission"] + cost["transfer_fee"] + cost["sell_tax"] + cost["slippage"]
        self.assertEqual(cost["total_cost"], expected)
        self.assertGreaterEqual(cost["commission"], Decimal("5"))

    def test_mock_cannot_be_labeled_real_provider(self):
        with self.assertRaisesRegex(TechnicalShadowRunError, "mock_or_synthetic"):
            validate_source_provenance(provider_id="mock", provider_kind="real_provider", synthetic=True)

    def test_exposure_data_failure_is_risk_off(self):
        result = compute_technical_shadow_exposure(
            benchmark_rows=[], eligible_stock_rows=[], current_nav=10000, peak_nav=10000,
            policy=self.config["exposure"],
        )
        self.assertEqual(result["market_state"], "RISK_OFF")
        self.assertEqual(result["target_gross_exposure"], 0.0)
        self.assertTrue(result["data_fail_closed"])

    def test_run_directory_is_create_only_and_never_emits_orders(self):
        config, ids, sessions, stocks, benchmark = _alpha_inputs(122)
        captured = CapturedData(
            provider_id="mock", provider_kind="test_fixture", adapter_version="test-v1",
            synthetic=True, captured_at="2026-08-26T16:00:00+08:00", sessions=sessions,
            stock_rows=stocks, benchmark_rows=benchmark, receipts={},
        )
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary)
            run_root, summary = run_replay(
                config=config, captured=captured, recent_completed_sessions=1,
                initial_cash=Decimal("10000"), output_root=output_root, run_id="fixed",
            )
            self.assertFalse(summary["automatic_order_submission"])
            decision = json.loads(next((run_root / "daily").glob("*.decision.json")).read_text(encoding="utf-8"))
            self.assertFalse(decision["automatic_order_submission"])
            self.assertFalse(any(path.name.lower().startswith("order") for path in run_root.rglob("*")))
            with self.assertRaisesRegex(TechnicalShadowRunError, "create_only_run_directory_exists"):
                run_replay(
                    config=config, captured=captured, recent_completed_sessions=1,
                    initial_cash=Decimal("10000"), output_root=output_root, run_id="fixed",
                )


if __name__ == "__main__":
    unittest.main()
