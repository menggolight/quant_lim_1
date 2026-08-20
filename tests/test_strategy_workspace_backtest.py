from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from research.strategy_workspace.attribution import reconcile_attribution
from research.strategy_workspace.backtest import (
    BacktestConfig,
    BacktestInputError,
    BenchmarkClose,
    CostModel,
    DailyClose,
    FrozenSignal,
    run_backtest,
)


D = Decimal


class StrategyWorkspaceBacktestTest(unittest.TestCase):
    def bar(self, day: int, instrument: str, close: str, lot_size: int = 100) -> DailyClose:
        return DailyClose(instrument, date(2026, 1, day), D(close), lot_size)

    def test_signal_executes_only_at_next_session_close_and_costs_reconcile(self) -> None:
        calendar = [date(2026, 1, day) for day in (2, 3, 4)]
        result = run_backtest(
            [FrozenSignal("s1", date(2026, 1, 2), ("ETF",))],
            [
                self.bar(2, "ETF", "10"),
                self.bar(3, "ETF", "10"),
                self.bar(4, "ETF", "11"),
            ],
            benchmark=[
                BenchmarkClose(date(2026, 1, 2), D("100")),
                BenchmarkClose(date(2026, 1, 3), D("102")),
                BenchmarkClose(date(2026, 1, 4), D("104")),
            ],
            trading_calendar=calendar,
        )

        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade.execution_date, date(2026, 1, 3))
        self.assertEqual(trade.quantity, 800)
        self.assertEqual(trade.quantity % trade.lot_size, 0)
        self.assertEqual(trade.commission, D("5.0000"))
        self.assertEqual(result.nav[0].net_nav, D("10000.0000"))
        self.assertEqual(result.nav[-1].net_nav, D("10795.0000"))
        self.assertEqual(result.metrics.total_cost, D("5.0000"))
        self.assertAlmostEqual(result.metrics.cost_addback_return, 0.08)
        self.assertAlmostEqual(result.metrics.net_return, 0.0795)
        self.assertAlmostEqual(result.metrics.benchmark_return, 0.04)
        self.assertTrue(reconcile_attribution(result.attribution))
        summary = result.attribution.summary
        self.assertEqual(
            summary.cost_addback_pnl + summary.cost_pnl,
            summary.net_pnl,
        )
        self.assertEqual(summary.benchmark_pnl + summary.active_pnl, summary.net_pnl)
        self.assertEqual(summary.cost, summary.cost_pnl)
        self.assertEqual(summary.benchmark, summary.benchmark_pnl)
        self.assertEqual(summary.active, summary.active_pnl)
        self.assertEqual(summary.benchmark_pnl, D("400"))
        self.assertEqual(summary.active_pnl, D("395"))

    def test_equal_weight_targets_cap_positions_and_honor_each_lot_size(self) -> None:
        instruments = (("A", 100), ("B", 10), ("C", 1))
        bars = [
            self.bar(day, instrument, "10", lot_size)
            for day in (2, 3, 4)
            for instrument, lot_size in instruments
        ]
        result = run_backtest(
            [FrozenSignal("three", date(2026, 1, 2), ("A", "B", "C"))],
            bars,
            trading_calendar=[date(2026, 1, day) for day in (2, 3, 4)],
        )

        self.assertLessEqual(len(result.ending_positions), 3)
        lot_by_id = dict(instruments)
        for instrument_id, quantity in result.ending_positions.items():
            self.assertEqual(quantity % lot_by_id[instrument_id], 0)
        self.assertGreaterEqual(result.nav[-1].cash, D("1000"))
        self.assertTrue(any(skip.reason_code == "buy_reduced_for_cash_and_costs" for skip in result.skips))

    def test_sell_tax_transfer_fee_and_slippage_are_in_trade_cost_and_nav(self) -> None:
        costs = CostModel(
            sell_tax_rate=D("0.001"),
            transfer_fee_rate=D("0.00001"),
            slippage_bps=D("10"),
        )
        result = run_backtest(
            [
                FrozenSignal("buy", date(2026, 1, 2), ("ETF",)),
                FrozenSignal("exit", date(2026, 1, 3), ()),
            ],
            [
                self.bar(2, "ETF", "10"),
                self.bar(3, "ETF", "10"),
                self.bar(4, "ETF", "11"),
                self.bar(5, "ETF", "11"),
            ],
            trading_calendar=[date(2026, 1, day) for day in (2, 3, 4, 5)],
            config=BacktestConfig(costs=costs),
        )

        self.assertEqual([trade.side for trade in result.trades], ["BUY", "SELL"])
        buy, sell = result.trades
        self.assertGreater(buy.fill_price, buy.reference_close)
        self.assertLess(sell.fill_price, sell.reference_close)
        self.assertEqual(buy.sell_tax, D("0.0000"))
        self.assertGreater(sell.sell_tax, D("0"))
        self.assertGreater(buy.transfer_fee, D("0"))
        self.assertEqual(
            result.metrics.total_cost,
            buy.total_cost + sell.total_cost,
        )
        self.assertTrue(reconcile_attribution(result.attribution))

    def test_missing_execution_price_is_skipped_without_using_prior_close(self) -> None:
        result = run_backtest(
            [FrozenSignal("missing", date(2026, 1, 2), ("A",))],
            [self.bar(2, "A", "10"), self.bar(4, "A", "11")],
            trading_calendar=[date(2026, 1, day) for day in (2, 3, 4)],
        )
        self.assertFalse(result.trades)
        self.assertEqual(result.skips[0].reason_code, "missing_execution_price")
        self.assertEqual(result.nav[-1].net_nav, D("10000.0000"))

    def test_missing_price_does_not_turn_a_selected_holding_into_a_sell(self) -> None:
        result = run_backtest(
            [
                FrozenSignal("buy", date(2026, 1, 2), ("A",)),
                FrozenSignal("keep", date(2026, 1, 3), ("A",)),
            ],
            [
                self.bar(2, "A", "10"),
                self.bar(3, "A", "10"),
                self.bar(4, "B", "10"),
                self.bar(5, "A", "10"),
            ],
            trading_calendar=[date(2026, 1, day) for day in (2, 3, 4, 5)],
        )
        self.assertEqual([trade.side for trade in result.trades], ["BUY"])
        self.assertIn("A", result.ending_positions)
        keep_skips = [item for item in result.skips if item.signal_id == "keep"]
        self.assertEqual([item.reason_code for item in keep_skips], ["missing_execution_price"])

    def test_target_below_one_lot_and_cash_shortfall_have_distinct_reasons(self) -> None:
        below_lot = run_backtest(
            [FrozenSignal("lot", date(2026, 1, 2), ("A",))],
            [self.bar(2, "A", "200"), self.bar(3, "A", "200")],
            trading_calendar=[date(2026, 1, 2), date(2026, 1, 3)],
        )
        self.assertIn("target_below_one_lot", {item.reason_code for item in below_lot.skips})

        cash_short = run_backtest(
            [FrozenSignal("cash", date(2026, 1, 2), ("A",))],
            [self.bar(2, "A", "90"), self.bar(3, "A", "90")],
            trading_calendar=[date(2026, 1, 2), date(2026, 1, 3)],
        )
        self.assertIn(
            "insufficient_cash_after_costs",
            {item.reason_code for item in cash_short.skips},
        )
        self.assertFalse(cash_short.trades)

    def test_signal_after_last_session_is_recorded_not_backdated(self) -> None:
        result = run_backtest(
            [FrozenSignal("late", date(2026, 1, 4), ("A",))],
            [self.bar(2, "A", "10"), self.bar(3, "A", "10")],
            trading_calendar=[date(2026, 1, 2), date(2026, 1, 3)],
        )
        self.assertFalse(result.trades)
        self.assertEqual(result.skips[0].reason_code, "no_next_trading_session")

    def test_more_than_three_targets_and_ambiguous_execution_fail_closed(self) -> None:
        with self.assertRaisesRegex(BacktestInputError, "at most three"):
            FrozenSignal("too-many", date(2026, 1, 2), ("A", "B", "C", "D"))

        bars = [self.bar(day, "A", "10") for day in (2, 3, 4)]
        with self.assertRaisesRegex(BacktestInputError, "multiple frozen signals"):
            run_backtest(
                [
                    FrozenSignal("first", date(2026, 1, 1), ("A",)),
                    FrozenSignal("second", date(2026, 1, 1), ("A",)),
                ],
                bars,
                trading_calendar=[date(2026, 1, day) for day in (2, 3, 4)],
            )

    def test_metrics_include_turnover_volatility_sharpe_and_drawdown(self) -> None:
        result = run_backtest(
            [FrozenSignal("s", date(2026, 1, 2), ("A",))],
            [
                self.bar(2, "A", "10"),
                self.bar(3, "A", "10"),
                self.bar(4, "A", "12"),
                self.bar(5, "A", "8"),
            ],
            trading_calendar=[date(2026, 1, day) for day in (2, 3, 4, 5)],
        )
        self.assertGreater(result.metrics.turnover, 0)
        self.assertIsNotNone(result.metrics.annualized_return)
        self.assertIsNotNone(result.metrics.annualized_volatility)
        self.assertIsNotNone(result.metrics.sharpe)
        self.assertLess(result.metrics.max_drawdown, 0)

    def test_held_position_cannot_be_marked_on_an_indefinitely_stale_close(self) -> None:
        with self.assertRaisesRegex(BacktestInputError, "max_stale_sessions"):
            run_backtest(
                [FrozenSignal("buy", date(2026, 1, 2), ("A",))],
                [self.bar(2, "A", "10"), self.bar(3, "A", "10")],
                trading_calendar=[date(2026, 1, day) for day in (2, 3, 4, 5, 6)],
                config=BacktestConfig(max_stale_sessions=2),
            )


if __name__ == "__main__":
    unittest.main()
