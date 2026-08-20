from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
import math
import unittest
from unittest.mock import patch

import numpy as np

from research.strategy_workspace.evaluation import (
    BenchmarkTotalReturnPoint,
    ControlledSourceBinding,
    EvaluationError,
    PitCrossSection,
    PitObservation,
    RETURN_BASIS,
    benchmark_total_return_series_content_sha256,
    evaluate_pit_panel,
    evaluation_result_content_sha256,
    membership_panel_content_sha256,
    prepare_pit_cross_sections,
    trading_calendar_content_sha256,
)
from research.strategy_workspace.experiment import (
    ExperimentSpecV2,
    HISTORICAL_GATE_CONTRACTS,
    QUALITY_GROWTH_FACTOR_CONTRACTS,
    RESIDUALIZATION_CONTROL_CONTRACTS,
    STATISTICAL_CONTRACT,
)
from research.strategy_workspace.quality_growth import (
    QUALITY_GROWTH_FACTOR_SPECS,
    FactorAvailability,
    QualityGrowthFactorValue,
    QualityGrowthSnapshot,
)
from research.strategy_workspace.regression import RegressionError


CN_TZ = timezone(timedelta(hours=8))


def weekday_calendar(start: date, end: date) -> tuple[date, ...]:
    days = []
    current = start
    while current <= end:
        if current.weekday() < 5 and current != date(2018, 1, 1):
            days.append(current)
        current += timedelta(days=1)
    return tuple(days)


CALENDAR = weekday_calendar(date(2017, 1, 1), date(2027, 2, 1))
BENCHMARK_SERIES = tuple(
    BenchmarkTotalReturnPoint(day, 100.0 * (1.0001 ** index))
    for index, day in enumerate(CALENDAR)
)
BENCHMARK_LEVELS = {item.session_date: item.open_level for item in BENCHMARK_SERIES}

DUMMY_BINDING = ControlledSourceBinding(
    experiment_spec_sha256="0" * 64,
    membership_panel_receipt_sha256="1" * 64,
    membership_panel_content_sha256="2" * 64,
    benchmark_instrument_id="H00906.CSI",
    benchmark_instrument_source_receipt_sha256="3" * 64,
    benchmark_total_return_series_content_sha256="4" * 64,
    financial_data_receipt_sha256="5" * 64,
    industry_data_receipt_sha256="6" * 64,
    control_data_receipt_sha256="7" * 64,
)


def session_on_or_after(day: date) -> date:
    return next(item for item in CALENDAR if item >= day)


def factor_snapshot(instrument_id: str, decision_at: datetime, index: int, financial: bool) -> QualityGrowthSnapshot:
    factors = []
    for factor_index, spec in enumerate(QUALITY_GROWTH_FACTOR_SPECS):
        if financial and not spec.financial_applicable:
            factors.append(
                QualityGrowthFactorValue(
                    spec.factor_id,
                    "positive",
                    None,
                    FactorAvailability.NOT_APPLICABLE,
                    "financial_industry_not_applicable",
                )
            )
        else:
            value = (
                math.sin((index + 1) * (factor_index + 1) * 0.37)
                + math.cos(index * 0.13 + factor_index)
                + index * 0.025
            )
            factors.append(
                QualityGrowthFactorValue(
                    spec.factor_id,
                    "positive",
                    value,
                    FactorAvailability.AVAILABLE,
                )
            )
    return QualityGrowthSnapshot(
        instrument_id=instrument_id,
        decision_at=decision_at,
        latest_period_end=date(decision_at.year - 1, 12, 31),
        input_available_at_max=decision_at - timedelta(hours=1),
        industry_is_financial=financial,
        factors=tuple(factors),
        source_record_ids=(f"{instrument_id}:{decision_at.date()}",),
    )


def cross_section(
    day: date,
    *,
    outcome: bool = True,
    source_binding: ControlledSourceBinding = DUMMY_BINDING,
) -> PitCrossSection:
    decision_day = session_on_or_after(day)
    decision_at = datetime.combine(decision_day, time(16, 0), tzinfo=CN_TZ)
    decision_index = CALENDAR.index(decision_day)
    label_start = CALENDAR[decision_index + 1]
    label_end = CALENDAR[decision_index + 21]
    observations = []
    member_ids = []
    for index in range(20):
        financial = index >= 16
        instrument_id = f"{index + 1:06d}.SH"
        member_ids.append(instrument_id)
        snapshot = factor_snapshot(instrument_id, decision_at, index, financial)
        factor_sum = sum(value for value in snapshot.values.values() if value is not None)
        excess = factor_sum * 0.01 + index * 0.0002 if outcome else None
        benchmark_return = (
            BENCHMARK_LEVELS[label_end] / BENCHMARK_LEVELS[label_start] - 1.0
            if outcome
            else None
        )
        total_return = benchmark_return + excess if outcome else None
        observations.append(
            PitObservation(
                snapshot=snapshot,
                industry_id=("FIN" if financial else f"IND{index % 4}"),
                constituent_available_at=decision_at - timedelta(hours=2),
                industry_available_at=decision_at - timedelta(hours=2),
                controls_available_at=decision_at - timedelta(hours=1),
                log_float_cap=math.log(10.0 + index),
                earnings_yield=math.sin(index * 0.71) * 0.08 + index * 0.0003,
                rm120=math.cos(index * 0.43) * 0.20 + index * 0.0001,
                vol60=((index * index) % 13) / 100.0 + index * 0.0002,
                label_start_date=label_start,
                label_end_date=label_end,
                return_basis=RETURN_BASIS,
                forward_total_return_20d=total_return,
                benchmark_total_return_20d=benchmark_return,
                forward_excess_return_20d=excess,
                outcome_available_at=(
                    datetime.combine(label_end, time(16, 0), tzinfo=CN_TZ)
                    if outcome
                    else None
                ),
            )
        )
    return PitCrossSection(
        decision_at=decision_at,
        universe_as_of=decision_day,
        universe_available_at=decision_at - timedelta(hours=3),
        universe_version=f"pit-{decision_day.isoformat()}",
        member_ids=tuple(member_ids),
        observations=tuple(observations),
        source_binding=source_binding,
    )


def research_sections(cutoff_day: date = date(2026, 7, 1)) -> tuple[PitCrossSection, ...]:
    start_index = CALENDAR.index(session_on_or_after(date(2018, 1, 1)))
    cutoff_index = CALENDAR.index(cutoff_day)
    return tuple(
        cross_section(CALENDAR[index])
        for index in range(start_index, cutoff_index + 1, 20)
        if index + 21 <= cutoff_index
    )


def experiment_content(
    sections: tuple[PitCrossSection, ...],
    *,
    cutoff_day: date,
) -> dict[str, object]:
    factors = [
        {**dict(item), "required_fields": list(item["required_fields"])}
        for item in QUALITY_GROWTH_FACTOR_CONTRACTS
    ]
    return {
        "schema_version": "strategy-experiment-v2",
        "experiment_id": "quality-growth-evaluation-test",
        "created_at": f"{cutoff_day.isoformat()}T20:00:00+08:00",
        "status": "preregistered_frozen",
        "universe": {
            "universe_id": "csi800-pit-panel",
            "membership_dataset_id": "CSI800_PIT",
            "effective_interval": {
                "start_date": "2018-01-01",
                "end_date": cutoff_day.isoformat(),
            },
            "selection_rule": "membership_effective_at_decision",
            "backfill_policy": "forbid_current_constituent_backfill",
            "membership_panel_receipt_sha256": "1" * 64,
            "membership_panel_content_sha256": membership_panel_content_sha256(sections),
        },
        "benchmark": {
            "instrument_id": "H00906.CSI",
            "provider_id": "choice",
            "return_basis": "total_return",
            "instrument_id_source_receipt_sha256": "3" * 64,
            "total_return_series_content_sha256": (
                benchmark_total_return_series_content_sha256(BENCHMARK_SERIES)
            ),
        },
        "target": {
            "target_id": "excess-return-20-session",
            "horizon_trading_sessions": 20,
            "definition": "future_20_session_open_to_open_excess_total_return",
            "signal_cutoff": "decision_session_close",
            "entry_policy": "next_trading_session_open",
            "exit_policy": "rebalance_open_after_20_trading_sessions",
            "benchmark_alignment": "same_sessions_same_return_basis",
            "rebalance_anchor_date": "2018-01-02",
            "rebalance_anchor_rule": (
                "first_controlled_session_on_or_after_2018-01-01"
            ),
            "trading_calendar_content_sha256": trading_calendar_content_sha256(CALENDAR),
        },
        "factors": factors,
        "controls": [dict(item) for item in RESIDUALIZATION_CONTROL_CONTRACTS],
        "splits": {
            "train": {"start_date": "2018-01-01", "end_date": "2022-12-31"},
            "validation": {"start_date": "2023-01-01", "end_date": "2023-12-31"},
            "locked_test": {"start_date": "2024-01-01", "end_date": "2025-12-31"},
            "second_audit": {
                "start_date": "2026-01-01",
                "end_date": cutoff_day.isoformat(),
            },
            "preregistration_cutoff": cutoff_day.isoformat(),
            "purge_sessions": 20,
            "locked_test_freshness": "fresh_unconsumed",
            "second_audit_freshness": "fresh_unconsumed",
        },
        "ridge": {
            "model": "ridge",
            "alpha": "1",
            "fit_intercept": True,
            "standardization": "cross_sectional_train_parameters_only",
            "fit_scope": "train_only",
        },
        "statistics": {
            key: list(value) if isinstance(value, tuple) else value
            for key, value in STATISTICAL_CONTRACT.items()
        },
        "cost": {
            "currency": "CNY",
            "commission_rate": "0.00018",
            "minimum_commission": "5",
            "sell_stamp_tax_rate": "0.0005",
            "transfer_fee_rate_both_sides": "0.00001",
            "base_slippage_bps_one_way": "10",
            "stress_slippage_bps_one_way": "20",
            "stress_commission_multiplier": "2",
            "historical_rate_replay": False,
        },
        "portfolio": {
            "initial_capital": "10000",
            "top_decile_research_capital": "1000000",
            "lot_size_policy": "per_instrument_metadata",
            "max_positions": 2,
            "max_weight_per_position": "0.4",
            "cash_reserve_weight": "0.2",
            "rebalance_sessions": 20,
            "max_drawdown": "0.12",
            "entry_top_fraction": "0.05",
            "hold_top_fraction": "0.2",
            "positive_prediction_required": True,
            "manual_veto_policy": "leave_cash_no_substitute",
            "selected_positions_industry_policy": "distinct_level1_industry",
            "combined_account_level1_industry_cap": "0.45",
            "annual_one_way_turnover_cap": "4",
            "trial_duration_months": 12,
            "paper_decision_points": 12,
            "execution_mode": "paper_only",
            "long_only": True,
            "unmanaged_external_assets": [
                {
                    "instrument_id": "000333.SZ",
                    "quantity": 100,
                    "status": "unmanaged_external",
                    "level1_industry_code": "home-appliances",
                    "industry_source_receipt_sha256": "9" * 64,
                }
            ],
        },
        "gates": [
            {
                **dict(gate),
                "scope": "validation_and_all_audits",
                "failure_action": "reject",
            }
            for gate in HISTORICAL_GATE_CONTRACTS
        ],
        "hashes": {
            "data_receipt_sha256": ["5" * 64, "6" * 64, "7" * 64],
            "code_sha256": "a" * 64,
            "config_sha256": "b" * 64,
        },
        "consumed_test_intervals": [],
    }


def formal_fixture(
    cutoff_day: date = date(2026, 7, 1),
) -> tuple[tuple[PitCrossSection, ...], ExperimentSpecV2]:
    provisional = research_sections(cutoff_day)
    spec = ExperimentSpecV2.create(
        experiment_content(provisional, cutoff_day=cutoff_day)
    )
    binding = ControlledSourceBinding(
        experiment_spec_sha256=spec.spec_sha256,
        membership_panel_receipt_sha256="1" * 64,
        membership_panel_content_sha256=membership_panel_content_sha256(provisional),
        benchmark_instrument_id="H00906.CSI",
        benchmark_instrument_source_receipt_sha256="3" * 64,
        benchmark_total_return_series_content_sha256=(
            benchmark_total_return_series_content_sha256(BENCHMARK_SERIES)
        ),
        financial_data_receipt_sha256="5" * 64,
        industry_data_receipt_sha256="6" * 64,
        control_data_receipt_sha256="7" * 64,
    )
    sections = tuple(replace(section, source_binding=binding) for section in provisional)
    return sections, spec


class PitEvaluationTests(unittest.TestCase):
    def test_formal_submodel_failure_cannot_fall_back_to_successful_subset(self) -> None:
        sections, spec = formal_fixture()
        with patch(
            "research.strategy_workspace.evaluation.fit_ridge",
            side_effect=RegressionError("forced failure"),
        ):
            with self.assertRaisesRegex(EvaluationError, "successful-subset"):
                evaluate_pit_panel(
                    sections,
                    experiment=spec,
                    trading_calendar=CALENDAR,
                    benchmark_total_return_series=BENCHMARK_SERIES,
                    as_of=datetime(2026, 12, 31, 23, 59, tzinfo=CN_TZ),
                )

    def test_preprocessing_produces_zscores_and_keeps_financial_na_out(self) -> None:
        sections, spec = formal_fixture()
        panel = prepare_pit_cross_sections(
            sections,
            experiment=spec,
            trading_calendar=CALENDAR,
            benchmark_total_return_series=BENCHMARK_SERIES,
        )
        prepared = next(
            item for item in panel.cross_sections if item.decision_at.year == 2019
        )
        self.assertFalse(prepared.factor_failures)
        for factor_id in (
            "QG_CASH_EARNINGS_QUALITY",
            "QG_CASH_DEBT_COVERAGE",
            "QG_GROSS_PROFITABILITY",
        ):
            scores = [
                item.factor_scores[factor_id]
                for item in prepared.observations
                if not item.industry_is_financial
            ]
            self.assertAlmostEqual(float(np.mean(scores)), 0.0, places=10)
            self.assertAlmostEqual(float(np.std(scores)), 1.0, places=10)
            self.assertTrue(
                all(
                    factor_id not in item.factor_scores
                    for item in prepared.observations
                    if item.industry_is_financial
                )
            )

    def test_fixed_split_fmb_holm_ridge_baseline_and_oos_metrics(self) -> None:
        sections, spec = formal_fixture()
        result = evaluate_pit_panel(
            sections,
            experiment=spec,
            trading_calendar=CALENDAR,
            benchmark_total_return_series=BENCHMARK_SERIES,
            as_of=datetime(2026, 12, 31, 23, 59, tzinfo=CN_TZ),
        )
        self.assertEqual(result.experiment_spec_sha256, spec.spec_sha256)
        self.assertEqual(result.source_bundle_sha256, result.prepared_panel.source_bundle_sha256)
        self.assertEqual(len(result.factor_tests), 24)
        self.assertIn("estimated", {item.status for item in result.factor_tests})
        for item in result.factor_tests:
            for value in (
                item.coefficient,
                item.t_statistic,
                item.raw_p_value,
                item.holm_p_value,
            ):
                self.assertTrue(value is None or math.isfinite(value))
            if item.status == "estimated":
                self.assertIsNotNone(item.raw_p_value)
                self.assertIsNotNone(item.holm_p_value)
                self.assertGreaterEqual(item.holm_p_value, item.raw_p_value)
            else:
                self.assertEqual(item.reason, "non_finite_fama_macbeth_statistic")
                self.assertIsNone(item.coefficient)
                self.assertIsNone(item.t_statistic)
        first_hash = evaluation_result_content_sha256(result)
        second_hash = evaluation_result_content_sha256(result)
        self.assertRegex(first_hash, r"^[0-9a-f]{64}$")
        self.assertEqual(first_hash, second_hash)
        self.assertEqual(
            {item.model for item in result.predictions},
            {"ridge_alpha_1", "direction_equal_weight"},
        )
        self.assertEqual(
            {item.split for item in result.predictions},
            {"validation", "locked_test", "audit"},
        )
        self.assertTrue(result.rank_ic)
        self.assertTrue(result.top_decile)
        self.assertTrue(result.half_year)
        self.assertTrue(all(item.selected_count == 2 for item in result.top_decile))
        self.assertTrue(all(item.net_active_return is None for item in result.top_decile))
        self.assertTrue(
            all(item.cost_status == "blocked_requires_portfolio_cost_ledger" for item in result.top_decile)
        )
        self.assertTrue(all(abs(sum(item.selected_weights.values()) - 1.0) < 1e-12 for item in result.top_decile))
        self.assertFalse(result.historical_gate.cost_gate_pass)
        self.assertFalse(result.historical_gate.historical_gate_pass)
        self.assertIsNotNone(result.historical_gate.oos_mean_rank_ic)

    def test_complete_pit_constituent_and_availability_checks_fail_closed(self) -> None:
        section = cross_section(date(2023, 3, 1))
        with self.assertRaisesRegex(EvaluationError, "exactly match PIT member_ids"):
            replace(section, observations=section.observations[:-1])
        with self.assertRaisesRegex(EvaluationError, "future constituent snapshot"):
            replace(section, universe_available_at=section.decision_at + timedelta(seconds=1))
        future_industry = replace(
            section.observations[0],
            industry_available_at=section.decision_at + timedelta(seconds=1),
        )
        with self.assertRaisesRegex(EvaluationError, "future industry classification"):
            replace(
                section,
                observations=(future_industry, *section.observations[1:]),
            )
        with self.assertRaisesRegex(EvaluationError, "total minus benchmark"):
            replace(
                section.observations[0],
                forward_excess_return_20d=(
                    float(section.observations[0].forward_excess_return_20d) + 0.01
                ),
            )

        first = section.observations[0]
        factors = list(first.snapshot.factors)
        factors[0] = QualityGrowthFactorValue(
            factors[0].factor_id,
            "positive",
            None,
            FactorAvailability.MISSING,
            "source_value_missing",
        )
        incomplete = replace(
            first,
            snapshot=replace(first.snapshot, factors=tuple(factors)),
        )
        sections, spec = formal_fixture()
        target_index = next(
            index
            for index, item in enumerate(sections)
            if item.decision_at.year == 2023 and item.decision_at.month >= 3
        )
        target = sections[target_index]
        incomplete_target = replace(
            target.observations[0],
            snapshot=replace(
                target.observations[0].snapshot,
                factors=tuple(factors),
            ),
        )
        modified = list(sections)
        modified[target_index] = replace(
            target,
            observations=(incomplete_target, *target.observations[1:]),
        )
        with self.assertRaisesRegex(EvaluationError, "successful-subset"):
            prepare_pit_cross_sections(
                modified,
                experiment=spec,
                trading_calendar=CALENDAR,
                benchmark_total_return_series=BENCHMARK_SERIES,
            )

    def test_label_must_be_exactly_twenty_sessions_and_boundary_is_purged(self) -> None:
        sections, spec = formal_fixture()
        target_index = next(
            index
            for index, item in enumerate(sections)
            if item.decision_at.year == 2023 and item.decision_at.month >= 3
        )
        section = sections[target_index]
        wrong = replace(
            section.observations[0],
            label_end_date=section.observations[0].label_end_date - timedelta(days=1),
        )
        wrong_sections = list(sections)
        wrong_sections[target_index] = replace(
            section, observations=(wrong, *section.observations[1:])
        )
        with self.assertRaisesRegex(EvaluationError, "20 controlled sessions after"):
            prepare_pit_cross_sections(
                wrong_sections,
                experiment=spec,
                trading_calendar=CALENDAR,
                benchmark_total_return_series=BENCHMARK_SERIES,
            )

        wrong_start = replace(
            section.observations[0],
            label_start_date=section.observations[0].snapshot.decision_at.date(),
        )
        wrong_start_sections = list(sections)
        wrong_start_sections[target_index] = replace(
            section, observations=(wrong_start, *section.observations[1:])
        )
        with self.assertRaisesRegex(EvaluationError, "next controlled session"):
            prepare_pit_cross_sections(
                wrong_start_sections,
                experiment=spec,
                trading_calendar=CALENDAR,
                benchmark_total_return_series=BENCHMARK_SERIES,
            )

        panel = prepare_pit_cross_sections(
            sections,
            experiment=spec,
            trading_calendar=CALENDAR,
            benchmark_total_return_series=BENCHMARK_SERIES,
        )
        self.assertTrue(panel.cross_sections)
        self.assertIn(
            "boundary_purge_20_sessions",
            {item.reason_code for item in panel.exclusions},
        )

    def test_as_of_masks_unmatured_audit_outcomes_without_blocking_prediction(self) -> None:
        cutoff_day = date(2026, 3, 2)
        provisional = research_sections(cutoff_day)
        audit = provisional[-1]
        missing_observations = tuple(
            replace(
                item,
                forward_total_return_20d=None,
                benchmark_total_return_20d=None,
                forward_excess_return_20d=None,
                outcome_available_at=None,
            )
            for item in audit.observations
        )
        provisional = (
            *provisional[:-1],
            replace(audit, observations=missing_observations),
        )
        spec = ExperimentSpecV2.create(
            experiment_content(provisional, cutoff_day=cutoff_day)
        )
        binding = ControlledSourceBinding(
            experiment_spec_sha256=spec.spec_sha256,
            membership_panel_receipt_sha256="1" * 64,
            membership_panel_content_sha256=membership_panel_content_sha256(provisional),
            benchmark_instrument_id="H00906.CSI",
            benchmark_instrument_source_receipt_sha256="3" * 64,
            benchmark_total_return_series_content_sha256=(
                benchmark_total_return_series_content_sha256(BENCHMARK_SERIES)
            ),
            financial_data_receipt_sha256="5" * 64,
            industry_data_receipt_sha256="6" * 64,
            control_data_receipt_sha256="7" * 64,
        )
        sections = tuple(replace(item, source_binding=binding) for item in provisional)
        audit = sections[-1]
        result = evaluate_pit_panel(
            sections,
            experiment=spec,
            trading_calendar=CALENDAR,
            benchmark_total_return_series=BENCHMARK_SERIES,
            as_of=datetime.combine(cutoff_day, time(23, 0), tzinfo=CN_TZ),
        )
        audit_predictions = [
            item
            for item in result.predictions
            if item.decision_date == audit.decision_at.date()
        ]
        self.assertTrue(audit_predictions)
        self.assertTrue(all(item.actual_forward_excess_return_20d is None for item in audit_predictions))
        self.assertFalse(
            any(item.decision_date == audit.decision_at.date() for item in result.rank_ic)
        )

    def test_locked_predictions_never_refit_on_validation_labels(self) -> None:
        sections, spec = formal_fixture()
        as_of = datetime(2026, 12, 31, 23, 59, tzinfo=CN_TZ)
        baseline = evaluate_pit_panel(
            sections,
            experiment=spec,
            trading_calendar=CALENDAR,
            benchmark_total_return_series=BENCHMARK_SERIES,
            as_of=as_of,
        )
        validation_index = next(
            index
            for index, section in enumerate(sections)
            if section.decision_at.year == 2023
        )
        validation = sections[validation_index]
        poisoned_observations = tuple(
            replace(
                item,
                forward_total_return_20d=(
                    float(item.benchmark_total_return_20d) + 10.0 - row_index
                ),
                forward_excess_return_20d=10.0 - row_index,
            )
            for row_index, item in enumerate(validation.observations)
        )
        poisoned_sections = list(sections)
        poisoned_sections[validation_index] = replace(
            validation, observations=poisoned_observations
        )
        poisoned = evaluate_pit_panel(
            poisoned_sections,
            experiment=spec,
            trading_calendar=CALENDAR,
            benchmark_total_return_series=BENCHMARK_SERIES,
            as_of=as_of,
        )

        def locked_ridge(result):
            return {
                (item.decision_date, item.instrument_id): item.prediction
                for item in result.predictions
                if item.split == "locked_test" and item.model == "ridge_alpha_1"
            }

        self.assertTrue(locked_ridge(baseline))
        self.assertEqual(locked_ridge(baseline), locked_ridge(poisoned))

    def test_decision_cadence_and_preregistration_cutoff_are_frozen(self) -> None:
        sections, spec = formal_fixture()
        with self.assertRaisesRegex(EvaluationError, "exactly match.*20-session grid"):
            prepare_pit_cross_sections(
                (*sections[:10], *sections[11:]),
                experiment=spec,
                trading_calendar=CALENDAR,
                benchmark_total_return_series=BENCHMARK_SERIES,
            )

    def test_formal_binding_rejects_membership_calendar_benchmark_and_anchor_drift(self) -> None:
        sections, spec = formal_fixture()

        wrong_member = replace(
            sections[0],
            member_ids=(*sections[0].member_ids[:-1], "999999.SH"),
            observations=(
                *sections[0].observations[:-1],
                replace(
                    sections[0].observations[-1],
                    snapshot=replace(
                        sections[0].observations[-1].snapshot,
                        instrument_id="999999.SH",
                    ),
                ),
            ),
        )
        with self.assertRaisesRegex(EvaluationError, "membership panel content hash"):
            prepare_pit_cross_sections(
                (wrong_member, *sections[1:]),
                experiment=spec,
                trading_calendar=CALENDAR,
                benchmark_total_return_series=BENCHMARK_SERIES,
            )

        unbound = replace(
            sections[0].source_binding,
            membership_panel_content_sha256="f" * 64,
        )
        with self.assertRaisesRegex(EvaluationError, "source binding membership_panel"):
            prepare_pit_cross_sections(
                tuple(replace(item, source_binding=unbound) for item in sections),
                experiment=spec,
                trading_calendar=CALENDAR,
                benchmark_total_return_series=BENCHMARK_SERIES,
            )

        unregistered_receipt = replace(
            sections[0].source_binding,
            financial_data_receipt_sha256="8" * 64,
        )
        with self.assertRaisesRegex(EvaluationError, "was not preregistered"):
            prepare_pit_cross_sections(
                tuple(
                    replace(item, source_binding=unregistered_receipt)
                    for item in sections
                ),
                experiment=spec,
                trading_calendar=CALENDAR,
                benchmark_total_return_series=BENCHMARK_SERIES,
            )

        wrong_calendar = (*CALENDAR[:100], *CALENDAR[101:])
        with self.assertRaisesRegex(EvaluationError, "calendar hash"):
            prepare_pit_cross_sections(
                sections,
                experiment=spec,
                trading_calendar=wrong_calendar,
                benchmark_total_return_series=BENCHMARK_SERIES,
            )

        wrong_benchmark = list(BENCHMARK_SERIES)
        wrong_benchmark[100] = replace(
            wrong_benchmark[100], open_level=wrong_benchmark[100].open_level + 1.0
        )
        with self.assertRaisesRegex(EvaluationError, "benchmark total-return series hash"):
            prepare_pit_cross_sections(
                sections,
                experiment=spec,
                trading_calendar=CALENDAR,
                benchmark_total_return_series=wrong_benchmark,
            )

        target = sections[20]
        drifted_observations = tuple(
            replace(
                item,
                forward_total_return_20d=float(item.forward_total_return_20d) + 0.01,
                benchmark_total_return_20d=(
                    float(item.benchmark_total_return_20d) + 0.01
                ),
            )
            for item in target.observations
        )
        drifted_sections = list(sections)
        drifted_sections[20] = replace(target, observations=drifted_observations)
        with self.assertRaisesRegex(EvaluationError, "controlled total-return series"):
            prepare_pit_cross_sections(
                drifted_sections,
                experiment=spec,
                trading_calendar=CALENDAR,
                benchmark_total_return_series=BENCHMARK_SERIES,
            )

        shifted_content = experiment_content(
            research_sections(), cutoff_day=date(2026, 7, 1)
        )
        shifted_content["target"]["rebalance_anchor_date"] = "2018-01-03"
        with self.assertRaises(Exception):
            ExperimentSpecV2.create(shifted_content)


if __name__ == "__main__":
    unittest.main()
