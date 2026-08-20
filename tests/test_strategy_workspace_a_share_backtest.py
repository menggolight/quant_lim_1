from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from research.strategy_workspace.a_share_backtest import (
    DIAGNOSTIC_SIGNAL_SCOPE,
    FORMAL_SIGNAL_SCOPE,
    AShareBacktestError,
    AShareDailyBar,
    AShareTop2Config,
    BenchmarkTotalReturnBar,
    CloseSignal,
    RankedStockCandidate,
    UnmanagedExternalPosition,
    run_a_share_top2_backtest,
)


D = Decimal


class AShareTop2BacktestTest(unittest.TestCase):
    def candidate(
        self,
        instrument_id: str,
        industry: str,
        score: str = "1",
        percentile: str = "0.99",
        veto: bool = False,
    ) -> RankedStockCandidate:
        return RankedStockCandidate(
            instrument_id,
            industry,
            D(score),
            D(percentile),
            veto,
        )

    def bar(
        self,
        day: date,
        instrument_id: str,
        industry: str,
        price: str = "10",
        **changes,
    ) -> AShareDailyBar:
        values = {
            "instrument_id": instrument_id,
            "trading_date": day,
            "open_price": D(price),
            "close_price": D(price),
            "csi_level1_industry": industry,
            "lot_size": 100,
            "suspended": False,
            "is_st": False,
            "limit_up_locked": False,
            "limit_down_locked": False,
            "listing_days": 250,
            "average_turnover_20d": D("100000000"),
            "eligibility_available_at": datetime.combine(
                day, time(9, 30), timezone(timedelta(hours=8))
            ),
            "eligibility_source_sha256": "a" * 64,
        }
        values.update(changes)
        return AShareDailyBar(**values)

    def bars(self, days, definitions):
        result = [
            self.bar(day, instrument_id, industry, price, **changes)
            for day in days
            for instrument_id, industry, price, changes in definitions
        ]
        if not any(item.instrument_id == "000333.SZ" for item in result):
            result.extend(self.bar(day, "000333.SZ", "家电", "30") for day in days)
        return result

    def external(self) -> UnmanagedExternalPosition:
        return UnmanagedExternalPosition("000333.SZ", 100)

    def benchmark(self, days, *, daily_step: str = "0"):
        step = D(daily_step)
        return tuple(
            BenchmarkTotalReturnBar(
                benchmark_id="H00906.CSI",
                trading_date=day,
                open_level=D("1000") + step * index,
                close_level=D("1000") + step * index,
                available_at=datetime.combine(
                    day, time(16, 0), timezone(timedelta(hours=8))
                ),
                source_sha256="b" * 64,
            )
            for index, day in enumerate(days)
        )

    def run_backtest(self, signals, bars, *, external=None):
        controlled_dates = tuple(sorted({item.trading_date for item in bars}))
        return run_a_share_top2_backtest(
            signals,
            bars,
            controlled_trading_dates=controlled_dates,
            benchmark_bars=self.benchmark(controlled_dates),
            unmanaged_external=(external or self.external(),),
        )

    def test_next_session_open_top2_costs_and_stress(self) -> None:
        d1, d2, d3 = date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6)
        signal = CloseSignal(
            "s1",
            d1,
            (
                self.candidate("A", "金融", "3"),
                self.candidate("B", "医药", "2"),
                self.candidate("C", "消费", "1"),
            ),
        )
        bars = self.bars(
            (d1, d2, d3),
            (
                ("A", "金融", "10", {}),
                ("B", "医药", "20", {}),
                ("C", "消费", "5", {}),
            ),
        )
        result = self.run_backtest((signal,), bars)
        self.assertEqual({item.trading_date for item in result.base.trades}, {d2})
        self.assertEqual(result.base.final_positions, {"A": 300, "B": 100})
        self.assertGreaterEqual(result.base.nav[-1].cash / result.base.nav[-1].strategy_nav, D("0.20"))
        self.assertGreater(result.stress.total_cost, result.base.total_cost)
        self.assertEqual(result.base.trades[0].commission, D("5.0000"))
        self.assertEqual(result.stress.trades[0].commission, D("10.0000"))
        self.assertEqual(result.base.trades[0].transfer_fee, D("0.0300"))

    def test_historical_manual_veto_is_rejected_without_forward_paper_ledger(self) -> None:
        d1, d2 = date(2025, 2, 3), date(2025, 2, 4)
        signal = CloseSignal(
            "veto",
            d1,
            (
                self.candidate("A", "金融", "3", veto=True),
                self.candidate("B", "医药", "2"),
                self.candidate("C", "消费", "1"),
            ),
        )
        bars = self.bars(
            (d1, d2),
            tuple((item, industry, "10", {}) for item, industry in (("A", "金融"), ("B", "医药"), ("C", "消费"))),
        )
        with self.assertRaisesRegex(AShareBacktestError, "manual_veto=false"):
            self.run_backtest((signal,), bars)

    def test_incumbent_manual_veto_cannot_be_injected_into_history(self) -> None:
        days = tuple(date(2025, 2, 10) + timedelta(days=index) for index in range(22))
        signals = (
            CloseSignal("first", days[0], (self.candidate("A", "金融"),)),
            CloseSignal(
                "veto-incumbent",
                days[20],
                (
                    self.candidate("A", "金融", "3", "0.99", veto=True),
                    self.candidate("B", "医药", "2", "0.99"),
                    self.candidate("C", "消费", "1", "0.99"),
                ),
            ),
        )
        with self.assertRaisesRegex(AShareBacktestError, "forward-Paper"):
            self.run_backtest(
                signals,
                self.bars(days, (
                    ("A", "金融", "10", {}),
                    ("B", "医药", "10", {}),
                    ("C", "消费", "10", {}),
                )),
            )

    def test_budget_and_new_buy_gates_skip_deterministically(self) -> None:
        d1, d2 = date(2025, 3, 3), date(2025, 3, 4)
        signal = CloseSignal(
            "skip",
            d1,
            (
                self.candidate("EXPENSIVE", "金融", "5"),
                self.candidate("STOCK_ST", "工业", "4"),
                self.candidate("YOUNG", "材料", "3"),
                self.candidate("ILLIQUID", "能源", "2"),
                self.candidate("OK", "医药", "1"),
            ),
        )
        definitions = (
            ("EXPENSIVE", "金融", "50", {}),
            ("STOCK_ST", "工业", "10", {"is_st": True}),
            ("YOUNG", "材料", "10", {"listing_days": 249}),
            ("ILLIQUID", "能源", "10", {"average_turnover_20d": D("99999999")}),
            ("OK", "医药", "10", {}),
        )
        result = self.run_backtest((signal,), self.bars((d1, d2), definitions))
        self.assertEqual(result.base.final_positions, {"OK": 300})
        codes = {item.code for item in result.base.events}
        self.assertIn("budget_or_industry_skip_next", codes)
        self.assertIn("new_buy_blocked_st", codes)
        self.assertIn("new_buy_blocked_listing_age", codes)
        self.assertIn("new_buy_blocked_liquidity", codes)

    def test_hold_band_and_blocked_exit_preserve_real_exposure(self) -> None:
        days = tuple(date(2025, 4, 1) + timedelta(days=i) for i in range(22))
        d1, d2, execution2 = days[0], days[20], days[21]
        first = CloseSignal("first", d1, (self.candidate("A", "金融", "2"), self.candidate("B", "医药", "1")))
        second = CloseSignal(
            "second",
            d2,
            (
                self.candidate("C", "消费", "3"),
                self.candidate("A", "金融", "1", "0.85"),
                self.candidate("B", "医药", "-1", "0.10"),
            ),
        )
        definitions = (
            ("A", "金融", "10", {}),
            ("B", "医药", "10", {}),
            ("C", "消费", "10", {}),
        )
        bars = self.bars(days[:-1], definitions)
        bars.extend(self.bars((execution2,), (
            ("A", "金融", "10", {}),
            ("B", "医药", "10", {"limit_down_locked": True}),
            ("C", "消费", "10", {}),
        )))
        result = self.run_backtest((first, second), bars)
        self.assertIn("A", result.base.final_positions)
        self.assertIn("B", result.base.final_positions)
        self.assertNotIn("C", result.base.final_positions)
        self.assertTrue(any(item.code == "sell_blocked_limit_down" and item.instrument_id == "B" for item in result.base.events))

    def test_drawdown_latches_cash_target_and_no_new_buys(self) -> None:
        days = tuple(date(2025, 5, 5) + timedelta(days=i) for i in range(22))
        signal1 = CloseSignal("buy", days[0], (self.candidate("A", "金融"),))
        signal2 = CloseSignal("forbidden", days[20], (self.candidate("B", "医药"),))
        bars = []
        for index, day in enumerate(days):
            a_price = "10" if index < 2 else "5"
            bars.extend((self.bar(day, "A", "金融", a_price), self.bar(day, "B", "医药", "10")))
            bars.append(self.bar(day, "000333.SZ", "家电", "30"))
        result = self.run_backtest((signal1, signal2), bars)
        self.assertEqual(result.base.final_positions, {})
        self.assertTrue(any(item.code == "drawdown_stop_latched" for item in result.base.events))
        self.assertTrue(any(item.code == "new_buys_stopped_drawdown" for item in result.base.events))
        sell = next(
            item
            for item in result.base.trades
            if item.side == "SELL" and item.decision_id == "drawdown_cash_target"
        )
        self.assertEqual(sell.trading_date, days[21])
        self.assertEqual(sell.sell_tax, (sell.notional * D("0.0005")).quantize(D("0.0001")))
        self.assertEqual(sell.transfer_fee, (sell.notional * D("0.00001")).quantize(D("0.0001")))

    def test_unmanaged_midea_is_never_claimed_and_counts_in_industry_cap(self) -> None:
        d1, d2 = date(2025, 6, 2), date(2025, 6, 3)
        external = self.external()
        signal = CloseSignal(
            "external",
            d1,
            (
                self.candidate("000333.SZ", "可选消费", "4"),
                self.candidate("OTHER_CONSUMER", "可选消费", "3"),
                self.candidate("FINANCE", "金融", "2"),
            ),
        )
        definitions = (
            ("000333.SZ", "可选消费", "30", {}),
            ("OTHER_CONSUMER", "可选消费", "10", {}),
            ("FINANCE", "金融", "10", {}),
        )
        result = self.run_backtest((signal,), self.bars((d1, d2), definitions), external=external)
        self.assertNotIn("000333.SZ", result.base.final_positions)
        self.assertIn("OTHER_CONSUMER", result.base.final_positions)
        self.assertIn("FINANCE", result.base.final_positions)
        self.assertEqual(result.base.nav[-1].external_value, D("3000.0000"))
        self.assertTrue(any(item.code == "unmanaged_external_not_tradeable" for item in result.base.events))
        self.assertLessEqual(
            (D("3000") + D("10") * result.base.final_positions["OTHER_CONSUMER"])
            / result.base.nav[-1].combined_account_value,
            D("0.45"),
        )

    def test_unmanaged_midea_is_marked_daily_from_quantity_not_fixed_value(self) -> None:
        d1, d2 = date(2025, 6, 9), date(2025, 6, 10)
        bars = (
            self.bar(d1, "000333.SZ", "家电", "30"),
            self.bar(d2, "000333.SZ", "可选消费", "35"),
        )
        result = self.run_backtest((), bars)
        self.assertEqual(result.base.nav[0].external_value, D("3000.0000"))
        self.assertEqual(result.base.nav[1].external_value, D("3500.0000"))

    def test_external_only_industry_breach_does_not_block_other_industry(self) -> None:
        d1, d2 = date(2025, 6, 16), date(2025, 6, 17)
        signal = CloseSignal(
            "dilute",
            d1,
            (self.candidate("FINANCE", "金融"),),
        )
        bars = (
            self.bar(d1, "000333.SZ", "家电", "200"),
            self.bar(d2, "000333.SZ", "家电", "200"),
            self.bar(d1, "FINANCE", "金融", "10"),
            self.bar(d2, "FINANCE", "金融", "10"),
        )
        result = self.run_backtest((signal,), bars)
        self.assertIn("FINANCE", result.base.final_positions)
        self.assertTrue(
            any(
                item.code == "unmanaged_external_industry_over_cap"
                for item in result.base.events
            )
        )
        combined_gate = next(
            item
            for item in result.base.gate_results
            if item.gate_id == "combined_account_industry_cap"
        )
        self.assertTrue(combined_gate.passed)

    def test_retained_stock_losing_eligibility_is_replaced(self) -> None:
        days = tuple(date(2025, 6, 23) + timedelta(days=index) for index in range(22))
        signals = (
            CloseSignal("first", days[0], (self.candidate("A", "金融"),)),
            CloseSignal(
                "second",
                days[20],
                (
                    self.candidate("B", "医药", "2", "0.99"),
                    self.candidate("A", "金融", "1", "0.85"),
                ),
            ),
        )
        bars = self.bars(days[:-1], (
            ("A", "金融", "10", {}),
            ("B", "医药", "10", {}),
        ))
        bars.extend(self.bars((days[-1],), (
            ("A", "金融", "10", {"is_st": True}),
            ("B", "医药", "10", {}),
        )))
        result = self.run_backtest(signals, bars)
        self.assertNotIn("A", result.base.final_positions)
        self.assertIn("B", result.base.final_positions)
        self.assertTrue(
            any(
                item.code == "hold_eligibility_failed"
                and item.instrument_id == "A"
                and item.detail == "new_buy_blocked_st"
                for item in result.base.events
            )
        )

    def test_eligibility_fields_cannot_be_omitted_with_safe_defaults(self) -> None:
        with self.assertRaises(AShareBacktestError):
            AShareDailyBar(
                instrument_id="A",
                trading_date=date(2025, 6, 20),
                open_price=D("10"),
                close_price=D("10"),
                csi_level1_industry="金融",
            )

    def test_insufficient_candidates_hold_less_or_all_cash(self) -> None:
        d1, d2 = date(2025, 7, 1), date(2025, 7, 2)
        signal = CloseSignal("cash", d1, (self.candidate("A", "金融", "-1", "0.99"),))
        result = self.run_backtest(
            (signal,), self.bars((d1, d2), (("A", "金融", "10", {}),))
        )
        self.assertEqual(result.base.final_positions, {})
        self.assertEqual(result.base.nav[-1].cash, D("10000.0000"))

    def test_empty_candidate_signal_exits_to_cash(self) -> None:
        days = tuple(date(2025, 8, 1) + timedelta(days=i) for i in range(22))
        signals = (
            CloseSignal("buy", days[0], (self.candidate("A", "金融"),)),
            CloseSignal("empty", days[20], ()),
        )
        result = self.run_backtest(
            signals,
            self.bars(days, (("A", "金融", "10", {}),)),
        )
        self.assertEqual(result.base.final_positions, {})
        self.assertTrue(any(item.side == "SELL" and item.decision_id == "empty" for item in result.base.trades))

    def test_decisions_must_be_exactly_twenty_controlled_sessions_apart(self) -> None:
        days = tuple(date(2025, 9, 1) + timedelta(days=i) for i in range(22))
        signals = (
            CloseSignal("one", days[0], ()),
            CloseSignal("too_soon", days[19], ()),
        )
        with self.assertRaisesRegex(AShareBacktestError, "exactly 20"):
            self.run_backtest(signals, self.bars(days, (("A", "金融", "10", {}),)))

    def test_calendar_and_unmanaged_midea_are_required_fail_closed_inputs(self) -> None:
        d1, d2, d3 = date(2025, 10, 1), date(2025, 10, 2), date(2025, 10, 3)
        bars = self.bars((d1, d2), (("A", "金融", "10", {}),))
        with self.assertRaisesRegex(AShareBacktestError, "exact controlled"):
            run_a_share_top2_backtest(
                (),
                bars,
                controlled_trading_dates=(d1, d2, d3),
                benchmark_bars=self.benchmark((d1, d2, d3)),
                unmanaged_external=(self.external(),),
            )
        with self.assertRaisesRegex(AShareBacktestError, "100-share Midea"):
            run_a_share_top2_backtest(
                (),
                bars,
                controlled_trading_dates=(d1, d2),
                benchmark_bars=self.benchmark((d1, d2)),
                unmanaged_external=(),
            )

    def test_csi800_total_return_benchmark_is_exact_and_drives_active_return(self) -> None:
        d1, d2 = date(2025, 10, 6), date(2025, 10, 7)
        bars = self.bars((d1, d2), (("A", "金融", "10", {}),))
        result = run_a_share_top2_backtest(
            (),
            bars,
            controlled_trading_dates=(d1, d2),
            benchmark_bars=self.benchmark((d1, d2), daily_step="10"),
            unmanaged_external=(self.external(),),
        )
        self.assertEqual(result.base.benchmark_id, "H00906.CSI")
        self.assertEqual(
            result.base.net_active_return,
            result.base.net_return - result.base.benchmark_total_return,
        )
        self.assertEqual(
            result.stress.net_active_return,
            result.stress.net_return - result.stress.benchmark_total_return,
        )
        with self.assertRaisesRegex(AShareBacktestError, "exact controlled calendar"):
            run_a_share_top2_backtest(
                (),
                bars,
                controlled_trading_dates=(d1, d2),
                benchmark_bars=self.benchmark((d1,)),
                unmanaged_external=(self.external(),),
            )

    def test_held_industry_refreshes_from_each_daily_pit_bar(self) -> None:
        d1, d2, d3 = date(2025, 10, 8), date(2025, 10, 9), date(2025, 10, 10)
        bars = self.bars((d1, d2, d3), (("A", "金融", "10", {}),))
        bars = [
            replace(item, csi_level1_industry="家电")
            if item.instrument_id == "A" and item.trading_date == d3
            else item
            for item in bars
        ]
        result = self.run_backtest(
            (CloseSignal("buy", d1, (self.candidate("A", "金融"),)),),
            bars,
        )
        self.assertTrue(
            any(
                item.code == "strategy_added_combined_industry_cap_breach"
                and item.detail == "家电"
                for item in result.base.events
            )
        )

    def test_policy_values_cannot_be_relaxed(self) -> None:
        with self.assertRaisesRegex(AShareBacktestError, "max_positions is frozen"):
            AShareTop2Config(max_positions=3)
        with self.assertRaisesRegex(AShareBacktestError, "initial_cash is frozen"):
            AShareTop2Config(initial_cash=D("20000"))

    def test_raw_signal_and_raw_runner_cannot_claim_formal_provenance(self) -> None:
        d1, d2 = date(2025, 11, 3), date(2025, 11, 4)
        with self.assertRaisesRegex(AShareBacktestError, "only be derived"):
            CloseSignal(
                "forged",
                d1,
                (),
                signal_scope=FORMAL_SIGNAL_SCOPE,
                evaluation_sha256="1" * 64,
                experiment_spec_sha256="2" * 64,
                member_ids_sha256="3" * 64,
                ranking_sha256="4" * 64,
            )
        result = self.run_backtest(
            (CloseSignal("raw", d1, ()),),
            self.bars((d1, d2), (("A", "金融", "10", {}),)),
        )
        self.assertEqual(result.research_scope, DIAGNOSTIC_SIGNAL_SCOPE)
        self.assertFalse(result.formal_signal_binding)
        self.assertEqual(result.formal_signal_bindings, ())


if __name__ == "__main__":
    unittest.main()
