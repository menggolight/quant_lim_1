from __future__ import annotations

import unittest
from copy import copy
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from research.strategy_workspace.a_share_backtest import (
    DIAGNOSTIC_SIGNAL_SCOPE,
    AShareBacktestError,
    AShareDailyBar,
    PolicyGateResult,
    UnmanagedExternalPosition,
    a_share_backtest_comparison_content_sha256,
    derive_formal_a_share_top2_close_signals,
    formal_signal_bindings_from_evaluation,
    run_formal_a_share_top2_backtest,
)
from research.strategy_workspace.admission import (
    REQUIRED_FORWARD_PAPER_GATE_IDS,
    REQUIRED_HISTORICAL_GATE_IDS,
    LiveNotSupportedError,
    PaperAdmissionError,
    PaperTrackRecord,
    build_historical_gate_result,
    evaluate_manual_real_money_candidate,
    evaluate_paper_admission,
)
from research.strategy_workspace.evaluation import evaluate_pit_panel
from research.strategy_workspace.top_decile_backtest import (
    BenchmarkTotalReturnBar,
    ResearchPriceBar,
    run_top_decile_cost_ledger,
)

# These fixtures are themselves exercised through the real evaluator and cost
# ledger. Admission tests must not manufacture a green HistoricalGateSummary or
# caller-owned historical gate booleans.
from tests.test_strategy_workspace_choice_gate import _receipt
from tests.test_strategy_workspace_evaluation import (
    BENCHMARK_SERIES,
    CALENDAR,
    formal_fixture,
)


CN_TZ = timezone(timedelta(hours=8))
D = Decimal


def controlled_cost_ledger_inputs(evaluation):
    locked = tuple(
        item
        for item in evaluation.top_decile
        if item.split == "locked_test" and item.model == "ridge_alpha_1"
    )
    prediction_index = {
        (item.decision_date, item.instrument_id): item
        for item in evaluation.predictions
        if item.split == "locked_test" and item.model == "ridge_alpha_1"
    }
    levels = {}
    for selection in locked:
        decision_index = CALENDAR.index(selection.decision_date)
        entry_index = decision_index + 1
        exit_index = decision_index + 21
        for instrument_id in selection.selected_instrument_ids:
            entry_date = CALENDAR[entry_index]
            entry = levels.get((instrument_id, entry_date), D("100"))
            prediction = prediction_index[(selection.decision_date, instrument_id)]
            total_return = D(str(prediction.actual_forward_total_return_20d))
            exit_level = entry * (D("1") + total_return)
            levels.setdefault((instrument_id, selection.decision_date), entry)
            for offset, calendar_index in enumerate(
                range(entry_index, exit_index + 1)
            ):
                level = entry + (exit_level - entry) * D(offset) / D("20")
                key = (instrument_id, CALENDAR[calendar_index])
                if key in levels:
                    if abs(levels[key] - level) > D("0.0000000001"):
                        raise AssertionError("formal fixture price path is inconsistent")
                else:
                    levels[key] = level
    price_bars = tuple(
        ResearchPriceBar(
            instrument_id=instrument_id,
            trading_date=day,
            open_price=level,
            close_price=level,
            available_at=datetime.combine(day, time(16, 0), CN_TZ),
            source_sha256="a" * 64,
        )
        for (instrument_id, day), level in sorted(levels.items())
    )
    first_index = CALENDAR.index(locked[0].decision_date)
    last_index = CALENDAR.index(locked[-1].decision_date) + 21
    benchmark_by_date = {item.session_date: item for item in BENCHMARK_SERIES}
    benchmark_bars = tuple(
        BenchmarkTotalReturnBar(
            benchmark_id="H00906.CSI",
            trading_date=day,
            open_level=D(str(benchmark_by_date[day].open_level)),
            close_level=D(str(benchmark_by_date[day].open_level)),
            available_at=datetime.combine(day, time(16, 0), CN_TZ),
            source_sha256="b" * 64,
        )
        for day in CALENDAR[first_index : last_index + 1]
    )
    return locked, price_bars, benchmark_bars


class ControlledAdmissionChainTest(unittest.TestCase):
    CODE_HASH = "c" * 64

    @classmethod
    def setUpClass(cls) -> None:
        sections, experiment = formal_fixture()
        cls.experiment = experiment
        cls.evaluation = evaluate_pit_panel(
            sections,
            experiment=experiment,
            trading_calendar=CALENDAR,
            benchmark_total_return_series=BENCHMARK_SERIES,
            as_of=datetime(2026, 12, 31, 23, 59, tzinfo=CN_TZ),
        )
        locked, prices, cls.benchmark = controlled_cost_ledger_inputs(cls.evaluation)
        cls.top_decile = run_top_decile_cost_ledger(
            cls.evaluation,
            trading_calendar=CALENDAR,
            price_bars=prices,
            benchmark_bars=cls.benchmark,
            as_of=datetime(2026, 12, 31, 23, 59, tzinfo=CN_TZ),
        )
        controlled_dates = tuple(item.trading_date for item in cls.benchmark)
        locked_prepared = tuple(
            item
            for item in cls.evaluation.prepared_panel.cross_sections
            if item.split == "locked_test"
        )
        member_ids = tuple(
            sorted(
                {
                    item.instrument_id
                    for section in locked_prepared
                    for item in section.observations
                }
            )
        )
        bars = []
        for day in controlled_dates:
            for instrument_id in member_ids:
                bars.append(
                    AShareDailyBar(
                        instrument_id=instrument_id,
                        trading_date=day,
                        open_price=D("10"),
                        close_price=D("10"),
                        csi_level1_industry=f"PIT-{instrument_id}",
                        lot_size=100,
                        suspended=False,
                        is_st=False,
                        limit_up_locked=False,
                        limit_down_locked=False,
                        listing_days=1000,
                        average_turnover_20d=D("1000000000"),
                        eligibility_available_at=datetime.combine(
                            day, time(9, 30), CN_TZ
                        ),
                        eligibility_source_sha256="7" * 64,
                    )
                )
            bars.append(
                AShareDailyBar(
                    instrument_id="000333.SZ",
                    trading_date=day,
                    open_price=D("30"),
                    close_price=D("30"),
                    csi_level1_industry="家电",
                    lot_size=100,
                    suspended=False,
                    is_st=False,
                    limit_up_locked=False,
                    limit_down_locked=False,
                    listing_days=1000,
                    average_turnover_20d=D("1000000000"),
                    eligibility_available_at=datetime.combine(
                        day, time(9, 30), CN_TZ
                    ),
                    eligibility_source_sha256="7" * 64,
                )
            )
        cls.a_share_bars = tuple(bars)
        cls.full_benchmark = tuple(
            BenchmarkTotalReturnBar(
                benchmark_id="H00906.CSI",
                trading_date=item.session_date,
                open_level=D(str(item.open_level)),
                close_level=D(str(item.open_level)),
                available_at=datetime.combine(
                    item.session_date, time(16, 0), CN_TZ
                ),
                source_sha256="b" * 64,
            )
            for item in BENCHMARK_SERIES
        )
        cls.formal_signals = derive_formal_a_share_top2_close_signals(
            cls.evaluation, cls.a_share_bars
        )
        cls.backtest = run_formal_a_share_top2_backtest(
            cls.evaluation,
            cls.experiment,
            cls.a_share_bars,
            trading_calendar=CALENDAR,
            benchmark_bars=cls.full_benchmark,
            unmanaged_external=(UnmanagedExternalPosition("000333.SZ", 100),),
        )
        cls.receipt = _receipt()

    @classmethod
    def admission_values(cls):
        return {
            "backtest_result": cls.backtest,
            "top_decile_result": cls.top_decile,
            "choice_receipt": cls.receipt,
            "evaluation_result": cls.evaluation,
            "experiment_spec": cls.experiment,
            "experiment_sha256": cls.evaluation.experiment_spec_sha256,
            "code_sha256": cls.CODE_HASH,
        }

    @staticmethod
    def rehash_backtest(backtest):
        return replace(
            backtest,
            backtest_sha256=a_share_backtest_comparison_content_sha256(backtest),
        )

    def test_formal_close_signals_are_complete_internal_ridge_rankings(self) -> None:
        locked_prepared = tuple(
            item
            for item in self.evaluation.prepared_panel.cross_sections
            if item.split == "locked_test"
        )
        self.assertEqual(len(self.formal_signals), len(locked_prepared))
        first = self.formal_signals[0]
        self.assertEqual(len(first.candidates), len(locked_prepared[0].observations))
        self.assertEqual(first.candidates[0].percentile, D("1"))
        self.assertEqual(
            sum(item.percentile >= D("0.95") for item in first.candidates), 1
        )
        self.assertEqual(
            sum(item.percentile >= D("0.80") for item in first.candidates), 4
        )
        expected_first = min(
            (
                item
                for item in self.evaluation.predictions
                if item.split == "locked_test"
                and item.model == "ridge_alpha_1"
                and item.decision_date == first.signal_date
            ),
            key=lambda item: (-item.prediction, item.instrument_id),
        )
        self.assertEqual(first.candidates[0].instrument_id, expected_first.instrument_id)
        self.assertEqual(first.evaluation_sha256, self.backtest.evaluation_sha256)
        self.assertFalse(self.backtest.controlled_execution_bar_adapter_verified)

    def test_successful_prediction_subset_cannot_become_formal_top2(self) -> None:
        tampered = copy(self.evaluation)
        first_locked_ridge = next(
            item
            for item in self.evaluation.predictions
            if item.split == "locked_test" and item.model == "ridge_alpha_1"
        )
        object.__setattr__(
            tampered,
            "predictions",
            tuple(
                item
                for item in self.evaluation.predictions
                if item is not first_locked_ridge
            ),
        )
        with self.assertRaisesRegex(AShareBacktestError, "exactly match"):
            formal_signal_bindings_from_evaluation(tampered)

    def test_raw_diagnostic_top2_cannot_enter_historical_gates(self) -> None:
        diagnostic = replace(
            self.backtest,
            research_scope=DIAGNOSTIC_SIGNAL_SCOPE,
            formal_signal_binding=False,
            formal_signal_bindings=(),
            evaluation_sha256=None,
            experiment_spec_sha256=None,
            evaluation_source_bundle_sha256=None,
            benchmark_series_sha256=None,
        )
        diagnostic = self.rehash_backtest(diagnostic)
        with self.assertRaisesRegex(PaperAdmissionError, "diagnostic/raw"):
            build_historical_gate_result(
                backtest_result=diagnostic,
                top_decile_result=self.top_decile,
                evaluation_result=self.evaluation,
                experiment_spec=self.experiment,
                choice_receipt=self.receipt,
            )

    def test_formal_benchmark_calendar_and_ranking_receipts_are_rechecked(self) -> None:
        altered_series = self.rehash_backtest(
            replace(self.backtest, benchmark_series_sha256="0" * 64)
        )
        with self.assertRaisesRegex(PaperAdmissionError, "benchmark series"):
            build_historical_gate_result(
                backtest_result=altered_series,
                top_decile_result=self.top_decile,
                evaluation_result=self.evaluation,
                experiment_spec=self.experiment,
                choice_receipt=self.receipt,
            )
        altered_receipts = list(self.backtest.formal_signal_bindings)
        altered_receipts[0] = replace(
            altered_receipts[0], ranking_sha256="0" * 64
        )
        altered_ranking = self.rehash_backtest(
            replace(self.backtest, formal_signal_bindings=tuple(altered_receipts))
        )
        with self.assertRaisesRegex(PaperAdmissionError, "member/ranking"):
            build_historical_gate_result(
                backtest_result=altered_ranking,
                top_decile_result=self.top_decile,
                evaluation_result=self.evaluation,
                experiment_spec=self.experiment,
                choice_receipt=self.receipt,
            )
        altered_external = self.rehash_backtest(
            replace(self.backtest, unmanaged_external_sha256="0" * 64)
        )
        with self.assertRaisesRegex(PaperAdmissionError, "Midea"):
            build_historical_gate_result(
                backtest_result=altered_external,
                top_decile_result=self.top_decile,
                evaluation_result=self.evaluation,
                experiment_spec=self.experiment,
                choice_receipt=self.receipt,
            )
        self.assertEqual(
            self.backtest.trading_calendar_sha256,
            self.top_decile.trading_calendar_sha256,
        )
        self.assertEqual(
            self.backtest.base.benchmark_data_sha256,
            self.top_decile.benchmark_data_sha256,
        )
        self.assertEqual(self.backtest.base.start_date, self.top_decile.base.start_date)
        self.assertEqual(self.backtest.base.end_date, self.top_decile.base.end_date)

    def test_real_results_are_the_only_source_of_exact_eleven_gates(self) -> None:
        history = build_historical_gate_result(
            backtest_result=self.backtest,
            top_decile_result=self.top_decile,
            evaluation_result=self.evaluation,
            experiment_spec=self.experiment,
            choice_receipt=self.receipt,
        )
        self.assertEqual(
            tuple(item.gate_id for item in history.gate_results),
            REQUIRED_HISTORICAL_GATE_IDS,
        )
        gates = {item.gate_id: item for item in history.gate_results}
        self.assertFalse(gates["data_pit_complete"].passed)
        self.assertIn(
            "blocked_missing_controlled_stock_bar_bundle",
            gates["data_pit_complete"].observed,
        )
        self.assertIn(
            "blocked_missing_controlled_top_decile_price_bundle",
            gates["data_pit_complete"].observed,
        )
        self.assertFalse(gates["top2_net_absolute_positive"].passed)
        self.assertFalse(gates["top2_net_active_positive"].passed)
        raw_rank = tuple(
            D(str(item.rank_ic))
            for item in self.evaluation.rank_ic
            if item.model == "ridge_alpha_1"
            and item.split in {"validation", "locked_test", "audit"}
        )
        expected_rank_gate = (
            sum(raw_rank, D("0")) / D(len(raw_rank)) > 0
            and D(sum(item > 0 for item in raw_rank)) / D(len(raw_rank)) >= D("0.5")
        )
        self.assertEqual(gates["oos_rank_ic_stable"].passed, expected_rank_gate)
        self.assertFalse(gates["corrected_significant_factor_count_gte_2"].passed)
        self.assertEqual(history.top_decile_result_sha256, self.top_decile.result_sha256)
        self.assertEqual(history.backtest_sha256, self.backtest.backtest_sha256)

    def test_stage_a_rejects_current_real_chain_instead_of_issuing_fake_paper(self) -> None:
        decision = evaluate_paper_admission(**self.admission_values())
        self.assertFalse(decision.admitted)
        self.assertEqual(decision.status, "paper_admission_rejected")
        self.assertIsNone(decision.certificate)
        self.assertIn("historical_gate_failed:data_pit_complete", decision.reasons)
        self.assertIn("historical_gate_failed:top2_net_active_positive", decision.reasons)

    def test_caller_cannot_supply_historical_gate_result_or_issue_time(self) -> None:
        with self.assertRaisesRegex(TypeError, "history"):
            evaluate_paper_admission(history=object(), **self.admission_values())
        with self.assertRaisesRegex(TypeError, "issued_at"):
            evaluate_paper_admission(
                issued_at=datetime.now(timezone.utc), **self.admission_values()
            )

    def test_top_decile_hash_and_evaluation_binding_are_verified(self) -> None:
        with self.assertRaisesRegex(PaperAdmissionError, "research capital"):
            build_historical_gate_result(
                backtest_result=self.backtest,
                top_decile_result=replace(
                    self.top_decile, research_capital=D("999999")
                ),
                evaluation_result=self.evaluation,
                experiment_spec=self.experiment,
                choice_receipt=self.receipt,
            )
        with self.assertRaisesRegex(PaperAdmissionError, "configuration"):
            build_historical_gate_result(
                backtest_result=self.backtest,
                top_decile_result=replace(
                    self.top_decile, configuration_sha256="0" * 64
                ),
                evaluation_result=self.evaluation,
                experiment_spec=self.experiment,
                choice_receipt=self.receipt,
            )
        with self.assertRaisesRegex(PaperAdmissionError, "ledger SHA-256 mismatch"):
            build_historical_gate_result(
                backtest_result=self.backtest,
                top_decile_result=replace(self.top_decile, result_sha256="0" * 64),
                evaluation_result=self.evaluation,
                experiment_spec=self.experiment,
                choice_receipt=self.receipt,
            )

    def test_stage_a_experiment_hash_must_come_from_formal_evaluation(self) -> None:
        values = self.admission_values()
        values["experiment_sha256"] = "0" * 64
        with self.assertRaisesRegex(PaperAdmissionError, "formal EvaluationResult"):
            evaluate_paper_admission(**values)
        altered_sections, altered_experiment = formal_fixture(date(2026, 3, 2))
        altered_evaluation = evaluate_pit_panel(
            altered_sections,
            experiment=altered_experiment,
            trading_calendar=CALENDAR,
            benchmark_total_return_series=BENCHMARK_SERIES,
            as_of=datetime(2026, 12, 31, 23, 59, tzinfo=CN_TZ),
        )
        with self.assertRaisesRegex(PaperAdmissionError, "not bound"):
            build_historical_gate_result(
                backtest_result=self.backtest,
                top_decile_result=self.top_decile,
                evaluation_result=altered_evaluation,
                experiment_spec=altered_experiment,
                choice_receipt=self.receipt,
            )

    def test_benchmark_tamper_is_rejected_before_gate_calculation(self) -> None:
        altered_base = replace(self.backtest.base, benchmark_id="OTHER")
        with self.assertRaisesRegex(PaperAdmissionError, "SHA-256 mismatch"):
            build_historical_gate_result(
                backtest_result=replace(self.backtest, base=altered_base),
                top_decile_result=self.top_decile,
                evaluation_result=self.evaluation,
                experiment_spec=self.experiment,
                choice_receipt=self.receipt,
            )

    def test_forward_gate_contract_and_live_boundary_remain_frozen(self) -> None:
        self.assertEqual(len(REQUIRED_FORWARD_PAPER_GATE_IDS), 6)
        with self.assertRaisesRegex(LiveNotSupportedError, "live_not_supported"):
            evaluate_paper_admission(
                requested_mode="LIVE", **self.admission_values()
            )
        with self.assertRaisesRegex(LiveNotSupportedError, "live_not_supported"):
            evaluate_manual_real_money_candidate(
                None,
                None,
                as_of=datetime.now(timezone.utc),
                requested_mode="LIVE",
            )

    def test_paper_track_record_requires_exact_controlled_gate_shape(self) -> None:
        dates = tuple(item.trading_date for item in self.backtest.base.nav)
        decisions = tuple(dates[index] for index in range(0, min(len(dates), 240), 20))
        gates = tuple(
            PolicyGateResult(item, True, "derived", "frozen")
            for item in REQUIRED_FORWARD_PAPER_GATE_IDS
        )
        record = PaperTrackRecord(
            paper_certificate_sha256="a" * 64,
            start_date=dates[0],
            end_date=dates[-1],
            decision_dates=decisions,
            controlled_trading_dates=dates,
            configuration_hashes=tuple(
                self.backtest.base.configuration_sha256 for _ in decisions
            ),
            max_drawdown=D("0.01"),
            gate_results=gates,
        )
        self.assertEqual(
            tuple(item.gate_id for item in record.gate_results),
            REQUIRED_FORWARD_PAPER_GATE_IDS,
        )


if __name__ == "__main__":
    unittest.main()
