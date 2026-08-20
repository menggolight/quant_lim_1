import copy
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
import unittest

from research.strategy_workspace.evaluation import EvaluationResult, evaluate_pit_panel
from research.strategy_workspace.top_decile_backtest import (
    BenchmarkTotalReturnBar,
    RESEARCH_CAPITAL,
    RESEARCH_SCOPE,
    ResearchPriceBar,
    TopDecileBacktestError,
    run_top_decile_cost_ledger,
)
from tests.test_strategy_workspace_evaluation import (
    BENCHMARK_SERIES,
    CALENDAR,
    formal_fixture,
)


CN_TZ = timezone(timedelta(hours=8))


@lru_cache(maxsize=1)
def make_evaluation() -> EvaluationResult:
    sections, experiment = formal_fixture()
    return evaluate_pit_panel(
        sections,
        experiment=experiment,
        trading_calendar=CALENDAR,
        benchmark_total_return_series=BENCHMARK_SERIES,
        as_of=datetime(2026, 12, 31, 23, 59, tzinfo=CN_TZ),
    )


def locked_selections(evaluation: EvaluationResult):
    return tuple(
        item
        for item in evaluation.top_decile
        if item.split == "locked_test" and item.model == "ridge_alpha_1"
    )


def make_market_data(evaluation: EvaluationResult | None = None):
    evaluation = evaluation or make_evaluation()
    selections = locked_selections(evaluation)
    decisions = tuple(item.decision_date for item in selections)
    first_decision_index = CALENDAR.index(decisions[0])
    last_decision_index = CALENDAR.index(decisions[-1])
    final_exit_index = last_decision_index + 21
    predictions = {
        (item.decision_date, item.instrument_id): item
        for item in evaluation.predictions
        if item.split == "locked_test" and item.model == "ridge_alpha_1"
    }
    open_levels: dict[tuple[str, date], Decimal] = {}
    for selection in selections:
        decision_index = CALENDAR.index(selection.decision_date)
        execution_index = decision_index + 1
        exit_index = decision_index + 21
        for instrument_id in selection.selected_instrument_ids:
            start_day = CALENDAR[execution_index]
            start = open_levels.get((instrument_id, start_day), Decimal("100"))
            expected = Decimal(
                str(
                    predictions[
                        (selection.decision_date, instrument_id)
                    ].actual_forward_total_return_20d
                )
            )
            end = start * (Decimal("1") + expected)
            open_levels[(instrument_id, CALENDAR[decision_index])] = start
            for offset in range(21):
                day = CALENDAR[execution_index + offset]
                open_levels[(instrument_id, day)] = (
                    start + (end - start) * Decimal(offset) / Decimal("20")
                )
    prices = tuple(
        ResearchPriceBar(
            instrument_id=instrument_id,
            trading_date=day,
            open_price=value,
            close_price=value,
            available_at=datetime.combine(day, time(16, 0), CN_TZ),
            source_sha256="a" * 64,
        )
        for (instrument_id, day), value in sorted(
            open_levels.items(), key=lambda item: (item[0][1], item[0][0])
        )
    )
    benchmark = []
    benchmark_levels = {
        item.session_date: Decimal(str(item.open_level))
        for item in BENCHMARK_SERIES
    }
    for index in range(first_decision_index, final_exit_index + 1):
        day = CALENDAR[index]
        benchmark_level = benchmark_levels[day]
        benchmark.append(
            BenchmarkTotalReturnBar(
                benchmark_id="CHOICE_RETURNED_CSI800_TR_ID",
                trading_date=day,
                open_level=benchmark_level,
                close_level=benchmark_level,
                available_at=datetime.combine(day, time(16, 0), CN_TZ),
                source_sha256="b" * 64,
            )
        )
    return prices, tuple(benchmark)


AS_OF = datetime(2026, 1, 30, 23, 59, tzinfo=CN_TZ)


class TopDecileCostLedgerTests(unittest.TestCase):
    def test_cost_ledger_is_next_open_complete_hashed_and_research_only(self) -> None:
        evaluation = make_evaluation()
        prices, benchmark = make_market_data()
        result = run_top_decile_cost_ledger(
            evaluation,
            trading_calendar=CALENDAR,
            price_bars=prices,
            benchmark_bars=benchmark,
            as_of=AS_OF,
        )

        self.assertEqual(result.research_capital, RESEARCH_CAPITAL)
        self.assertEqual(result.research_scope, RESEARCH_SCOPE)
        self.assertEqual(result.model, "ridge_alpha_1")
        self.assertEqual(result.split, "locked_test")
        self.assertEqual(result.benchmark_id, "CHOICE_RETURNED_CSI800_TR_ID")
        self.assertEqual(len(result.base.half_year_windows), 4)
        self.assertEqual(
            {item.half_year for item in result.base.half_year_windows},
            {"2024-H1", "2024-H2", "2025-H1", "2025-H2"},
        )
        first_window = result.base.decision_windows[0]
        self.assertEqual(
            first_window.execution_date,
            CALENDAR[CALENDAR.index(first_window.decision_date) + 1],
        )
        self.assertEqual(
            first_window.exit_date,
            CALENDAR[CALENDAR.index(first_window.decision_date) + 21],
        )
        self.assertGreater(result.base.net_absolute_return, Decimal("0"))
        self.assertGreater(result.base.net_active_return, Decimal("0"))
        self.assertGreater(result.stress.total_transaction_cost, result.base.total_transaction_cost)
        self.assertGreater(result.base.trades[0].commission, Decimal("0"))
        self.assertTrue(any(item.side == "SELL" and item.sell_tax > 0 for item in result.base.trades))
        self.assertEqual(
            {item.gate_id for item in result.gate_results},
            {
                "top_decile_net_absolute_positive",
                "top_decile_net_active_positive",
                "positive_semiannual_windows_gte_3_of_4",
                "stress_active_return_non_negative",
                "max_drawdown_lte_12pct",
                "annualized_one_way_turnover_lte_4",
            },
        )
        self.assertTrue(all(item.passed for item in result.gate_results))
        for digest in (
            result.configuration_sha256,
            result.evaluation_sha256,
            result.trading_calendar_sha256,
            result.price_data_sha256,
            result.benchmark_data_sha256,
            result.input_bundle_sha256,
            result.result_sha256,
            result.base.scenario_sha256,
            result.stress.scenario_sha256,
        ):
            self.assertEqual(len(digest), 64)

    def test_missing_selected_stock_price_fails_instead_of_using_successful_subset(self) -> None:
        evaluation = make_evaluation()
        prices, benchmark = make_market_data()
        with self.assertRaisesRegex(TopDecileBacktestError, "missing selected-stock price"):
            run_top_decile_cost_ledger(
                evaluation,
                trading_calendar=CALENDAR,
                price_bars=prices[1:],
                benchmark_bars=benchmark,
                as_of=AS_OF,
            )

    def test_missing_decision_and_tampered_selection_both_fail_closed(self) -> None:
        evaluation = make_evaluation()
        prices, benchmark = make_market_data()
        missing_target = locked_selections(evaluation)[-1]
        missing = copy.copy(evaluation)
        object.__setattr__(
            missing,
            "top_decile",
            tuple(item for item in evaluation.top_decile if item is not missing_target),
        )
        with self.assertRaisesRegex(TopDecileBacktestError, "every prepared decision"):
            run_top_decile_cost_ledger(
                missing,
                trading_calendar=CALENDAR,
                price_bars=prices,
                benchmark_bars=benchmark,
                as_of=AS_OF,
            )
        first = locked_selections(evaluation)[0]
        replacement_id = next(
            item.instrument_id
            for item in evaluation.predictions
            if item.decision_date == first.decision_date
            and item.split == first.split
            and item.model == first.model
            and item.instrument_id not in first.selected_instrument_ids
        )
        tampered_first = replace(
            first,
            selected_instrument_ids=(replacement_id,),
            selected_weights={replacement_id: 1.0},
        )
        tampered = copy.copy(evaluation)
        object.__setattr__(
            tampered,
            "top_decile",
            tuple(
                tampered_first if item is first else item
                for item in evaluation.top_decile
            ),
        )
        with self.assertRaisesRegex(TopDecileBacktestError, "frozen Ridge ranking"):
            run_top_decile_cost_ledger(
                tampered,
                trading_calendar=CALENDAR,
                price_bars=prices,
                benchmark_bars=benchmark,
                as_of=AS_OF,
            )

        incomplete_predictions = copy.copy(evaluation)
        first_prediction = next(
            item
            for item in evaluation.predictions
            if item.split == "locked_test" and item.model == "ridge_alpha_1"
        )
        object.__setattr__(
            incomplete_predictions,
            "predictions",
            tuple(item for item in evaluation.predictions if item is not first_prediction),
        )
        with self.assertRaisesRegex(TopDecileBacktestError, "every prepared PIT member"):
            run_top_decile_cost_ledger(
                incomplete_predictions,
                trading_calendar=CALENDAR,
                price_bars=prices,
                benchmark_bars=benchmark,
                as_of=AS_OF,
            )

    def test_future_market_data_and_benchmark_misalignment_fail_closed(self) -> None:
        evaluation = make_evaluation()
        prices, benchmark = make_market_data()
        future_prices = (
            replace(prices[0], available_at=AS_OF + timedelta(days=1)),
            *prices[1:],
        )
        with self.assertRaisesRegex(TopDecileBacktestError, "not available"):
            run_top_decile_cost_ledger(
                evaluation,
                trading_calendar=CALENDAR,
                price_bars=future_prices,
                benchmark_bars=benchmark,
                as_of=AS_OF,
            )

        first_decision = locked_selections(evaluation)[0].decision_date
        first_label = CALENDAR[CALENDAR.index(first_decision) + 21]
        bad_benchmark = tuple(
            replace(item, open_level=item.open_level * Decimal("1.01"))
            if item.trading_date == first_label
            else item
            for item in benchmark
        )
        with self.assertRaisesRegex(TopDecileBacktestError, "do not reconcile"):
            run_top_decile_cost_ledger(
                evaluation,
                trading_calendar=CALENDAR,
                price_bars=prices,
                benchmark_bars=bad_benchmark,
                as_of=AS_OF,
            )

    def test_qfq_and_one_benchmark_subject_are_mandatory(self) -> None:
        selected = locked_selections(make_evaluation())[0].selected_instrument_ids[0]
        with self.assertRaisesRegex(TopDecileBacktestError, "frozen to qfq"):
            ResearchPriceBar(
                instrument_id=selected,
                trading_date=date(2024, 1, 3),
                open_price=Decimal("100"),
                close_price=Decimal("100"),
                available_at=datetime(2024, 1, 3, 16, 0, tzinfo=CN_TZ),
                source_sha256="a" * 64,
                adjustment="none",
            )
        evaluation = make_evaluation()
        prices, benchmark = make_market_data()
        mixed = (replace(benchmark[0], benchmark_id="OTHER"), *benchmark[1:])
        with self.assertRaisesRegex(TopDecileBacktestError, "one returned subject id"):
            run_top_decile_cost_ledger(
                evaluation,
                trading_calendar=CALENDAR,
                price_bars=prices,
                benchmark_bars=mixed,
                as_of=AS_OF,
            )


if __name__ == "__main__":
    unittest.main()
