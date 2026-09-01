from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from hashlib import sha256
import json
from types import SimpleNamespace
import unittest
from unittest import mock

from research.market_data import tushare_alpha_feasibility as taf
from research.strategy_workspace import alpha_feasibility as af
from research.strategy_workspace.alpha_feasibility import (
    AlphaFeasibilityDataError,
    AlphaFeasibilityInput,
    BenchmarkBar,
    FACTOR_IDS,
    FROZEN_EXPOSURE_POLICY,
    LOCKED_TEST_STATUS,
    LockedTestAccessForbidden,
    MAX_POSITION_WEIGHT,
    PITAdmissionArtifacts,
    PITMembershipSnapshot,
    ProportionalCostScenario,
    SignalBar,
    SuspensionRecord,
    rank_alpha_feasibility_universe,
    run_alpha_feasibility,
    run_alpha_feasibility_comparison,
    select_pit_membership,
)
from research.strategy_workspace.technical_exposure_shadow_v1 import DEFAULT_POLICY
from research.strategy_workspace.technical_formal_backtest import (
    rank_technical_formal_universe,
)


class AlphaFeasibilityUnavailableDaySemanticsTests(unittest.TestCase):
    @staticmethod
    def _daily(day: str, close: str = "10") -> dict[str, str]:
        return {
            "ts_code": "000979.SZ",
            "trade_date": day,
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "pre_close": close,
            "vol": "100",
            "amount": "1000",
        }

    def test_daily_positive_turnover_wins_over_suspend_event(self) -> None:
        panel = taf.build_total_return_panel(
            ["2018-08-28"],
            ["000979.SZ"],
            [self._daily("20180828")],
            [{"ts_code": "000979.SZ", "trade_date": "20180828", "adj_factor": "1"}],
            [{"ts_code": "000979.SZ", "trade_date": "20180828", "suspend_timing": None, "suspend_type": "S"}],
        )
        self.assertEqual(panel[0]["raw_close"], "10")
        self.assertFalse(panel[0]["is_unavailable_no_daily_bar"])
        self.assertFalse(panel[0]["is_suspended_carry"])

    def test_unheld_missing_daily_is_not_a_candidate(self) -> None:
        sessions = tuple(date(2023, 1, 1) + timedelta(days=index) for index in range(122))
        instrument = "000001.SZ"
        signal_by_key = {
            (session, instrument): SignalBar(session, instrument, D("100"), D("100"))
            for session in sessions
        }
        prepared = SimpleNamespace(
            calendar_position={session: index for index, session in enumerate(sessions)},
            first_membership_date={instrument: sessions[0]},
            first_signal_position={instrument: 0},
            valid_signal_positions={instrument: tuple(range(121))},
            trading_dates=sessions,
            signal_by_key=signal_by_key,
            suspended=frozenset({(sessions[-1], instrument)}),
        )
        record = af._history_eligibility_records(
            prepared,
            decision_date=sessions[-1],
            instrument_ids=(instrument,),
        )[0]
        self.assertFalse(record.eligibility)
        self.assertEqual(record.reason, "unavailable_no_daily_bar")

    def test_held_missing_daily_freezes_prior_value(self) -> None:
        prior = date(2023, 6, 1)
        missing = date(2023, 6, 2)
        instrument = "000001.SZ"
        prepared = SimpleNamespace(
            signal_by_key={
                (prior, instrument): SignalBar(prior, instrument, D("100"), D("100")),
                (missing, instrument): SignalBar(missing, instrument, D("100"), D("100")),
            },
            suspended=frozenset({(missing, instrument)}),
        )
        values, frozen = af._value_held_positions_at_open(
            prepared=prepared,
            decision_date=prior,
            trading_date=missing,
            nav_before=D("1"),
            current_weights={instrument: D("0.4")},
        )
        self.assertEqual(values[instrument], D("0.4"))
        self.assertEqual(frozen, frozenset({instrument}))

    def test_reopened_position_keeps_cumulative_price_change(self) -> None:
        missing = date(2023, 6, 2)
        reopened = date(2023, 6, 5)
        instrument = "000001.SZ"
        prepared = SimpleNamespace(
            signal_by_key={
                (missing, instrument): SignalBar(missing, instrument, D("100"), D("100")),
                (reopened, instrument): SignalBar(reopened, instrument, D("125"), D("125"), D("120")),
            },
            suspended=frozenset({(missing, instrument)}),
        )
        values, frozen = af._value_held_positions_at_open(
            prepared=prepared,
            decision_date=missing,
            trading_date=reopened,
            nav_before=D("1"),
            current_weights={instrument: D("0.4")},
        )
        self.assertEqual(values[instrument], D("0.48"))
        self.assertFalse(frozen)

    def test_off_calendar_adj_factor_is_used_only_by_backward_asof(self) -> None:
        panel = taf.build_total_return_panel(
            ["2018-01-02", "2018-01-03"],
            ["000979.SZ"],
            [self._daily("20180102", "10"), self._daily("20180103", "11")],
            [{"ts_code": "000979.SZ", "trade_date": "20180101", "adj_factor": "2"}],
            [],
        )
        self.assertEqual([row["adj_factor_asof_date"] for row in panel], ["2018-01-01", "2018-01-01"])
        self.assertEqual([row["close"] for row in panel], ["20", "22"])

    def test_2024_metadata_is_rejected_before_any_row_access(self) -> None:
        class Poison:
            def __iter__(self):
                raise AssertionError("2024+ guard accessed rows")

        inputs = AlphaFeasibilityInput(
            coverage_start=date(2017, 7, 1),
            coverage_end=date(2024, 1, 1),
            trading_dates=Poison(),
            memberships=Poison(),
            stock_signal_bars=Poison(),
            benchmark_signal_bars=Poison(),
            suspensions=Poison(),
        )
        with self.assertRaises(LockedTestAccessForbidden):
            run_alpha_feasibility(split="development", inputs=inputs)


D = Decimal
BENCHMARK = "000906.SH"


def _months() -> tuple[str, ...]:
    result: list[str] = []
    year, month = 2017, 12
    while (year, month) <= (2023, 12):
        result.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year += 1
            month = 1
    return tuple(result)


def _self_hash(payload: dict, field: str) -> dict:
    result = dict(payload)
    raw = (
        json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    result[field] = sha256(raw).hexdigest()
    return result


def _pit_fixture(
    instrument_ids: tuple[str, ...],
    *,
    same_month_early: tuple[str, tuple[str, ...]] | None = None,
    weights_override: list[str] | None = None,
) -> tuple[tuple[PITMembershipSnapshot, ...], PITAdmissionArtifacts]:
    if len(instrument_ids) != 30:
        raise AssertionError("fixture weight vector assumes 30 instruments")
    if same_month_early is not None and len(same_month_early[1]) != 30:
        raise AssertionError("early fixture must also contain 30 instruments")
    weights = (
        list(weights_override)
        if weights_override is not None
        else ["3.343"] + ["3.333"] * (len(instrument_ids) - 1)
    )
    if len(weights) != len(instrument_ids):
        raise AssertionError("weight vector must match fixture instruments")
    weight_sum = str(af._exact_decimal_sum([D(value) for value in weights]))
    positive_places = [
        max(0, -D(value).as_tuple().exponent)
        for value in weights
        if D(value) != D("0")
    ]
    weight_tolerance = str(
        D("0")
        if not positive_places
        else D("0.5") * (D(10) ** (-min(positive_places)))
    )
    memberships: list[PITMembershipSnapshot] = []
    checks: list[dict] = []
    snapshots: list[dict] = []
    adjustment_reason = "controlled synthetic unit-test fixture; production evidence required"
    for month in _months():
        month_snapshots: list[tuple[str, tuple[str, ...]]] = []
        if same_month_early is not None and same_month_early[0] == month:
            month_snapshots.append((f"{month}-05", same_month_early[1]))
            month_snapshots.append((f"{month}-20", instrument_ids))
        else:
            month_snapshots.append((f"{month}-01", instrument_ids))
        coverage_snapshots: list[dict] = []
        for snapshot_date, snapshot_members in month_snapshots:
            memberships.append(
                PITMembershipSnapshot(date.fromisoformat(snapshot_date), snapshot_members)
            )
            coverage_snapshots.append(
                {
                    "snapshot_date": snapshot_date,
                    "component_count": len(snapshot_members),
                    "weight_sum": weight_sum,
                    "weight_tolerance": weight_tolerance,
                    "component_count_adjustment_evidence": adjustment_reason,
                    "valid": True,
                    "issues": [],
                }
            )
            snapshots.append(
                {
                    "month": month,
                    "snapshot_date": snapshot_date,
                    "members": [
                        {"instrument_id": instrument_id, "weight": weight}
                        for instrument_id, weight in zip(
                            snapshot_members, weights, strict=True
                        )
                    ],
                    "weight_sum": weight_sum,
                    "weight_tolerance": weight_tolerance,
                    "component_count_adjustment_evidence": adjustment_reason,
                    "source_response_sha256": "b" * 64,
                }
            )
        checks.append(
            {
                "month": month,
                "request_artifact_sha256": "a" * 64,
                "response_sha256": "b" * 64,
                "snapshots": coverage_snapshots,
                "selected_snapshot_date": month_snapshots[-1][0],
                "status": "complete",
                "issues": [],
            }
        )
    union_instruments = sorted(
        {
            member
            for membership in memberships
            for member in membership.members
        }
    )
    locked = {"access": "NOT_ACCESSED", "download": "NOT_DOWNLOADED", "run": "NOT_RUN"}
    report = _self_hash(
        {
            "schema_version": "pit-membership-coverage-report.v2",
            "experiment_id": "a-share-technical-alpha-feasibility-tushare-p1-v1",
            "generated_at": "2026-08-28T00:00:00+00:00",
            "index_code": BENCHMARK,
            "pit_months_expected": 73,
            "pit_months_observed": 73,
            "monthly_checks": checks,
            "stage_status": "PIT_MEMBERSHIP_READY",
            "terminal_status": None,
            "remaining_blockers": [],
            "locked_test_status": locked,
            "locked_test_consumed": False,
        },
        "report_sha256",
    )
    manifest = _self_hash(
        {
            "schema_version": "pit-membership-manifest.v2",
            "experiment_id": "a-share-technical-alpha-feasibility-tushare-p1-v1",
            "generated_at": "2026-08-28T00:00:00+00:00",
            "index_code": BENCHMARK,
            "coverage_start_month": "2017-12",
            "coverage_end_month": "2023-12",
            "pit_months_expected": 73,
            "pit_months_observed": 73,
            "snapshots": snapshots,
            "union_instrument_count": len(union_instruments),
            "union_instrument_ids": union_instruments,
            "stage_status": "PIT_MEMBERSHIP_READY",
            "remaining_blockers": [],
            "locked_test_status": locked,
            "locked_test_consumed": False,
        },
        "manifest_sha256",
    )
    return tuple(memberships), PITAdmissionArtifacts(report, manifest)


def _p15_pit_fixture() -> tuple[
    tuple[PITMembershipSnapshot, ...], PITAdmissionArtifacts
]:
    instrument_ids = tuple(f"{index:06d}.SZ" for index in range(1, 801))
    weights = ["0", *(["0.125"] * 798), "0.19"]
    weight_sum = str(af._exact_decimal_sum([D(value) for value in weights]))
    if weight_sum != "99.940":
        raise AssertionError("P1.5 fixture warning sum drift")
    weight_policy = {
        "zero_weight_count": 1,
        "weight_sum_hard_min": "99.5",
        "weight_sum_hard_max": "100.5",
        "weight_sum_warning_min": "99.95",
        "weight_sum_warning_max": "100.05",
        "warnings": ["weight_sum_outside_warning_range"],
    }
    members = [
        {"instrument_id": instrument_id, "weight": weight}
        for instrument_id, weight in zip(instrument_ids, weights, strict=True)
    ]
    memberships: list[PITMembershipSnapshot] = []
    checks: list[dict] = []
    snapshots: list[dict] = []
    snapshot_dates: list[str] = []
    for month in _months():
        dates = [f"{month}-20"]
        if month == "2017-12":
            dates.insert(0, f"{month}-05")
        coverage_snapshots: list[dict] = []
        for snapshot_date in dates:
            memberships.append(
                PITMembershipSnapshot(date.fromisoformat(snapshot_date), instrument_ids)
            )
            snapshot_dates.append(snapshot_date)
            coverage_snapshots.append(
                {
                    "snapshot_date": snapshot_date,
                    "component_count": 800,
                    "weight_sum": weight_sum,
                    "weight_tolerance": "0.5",
                    "component_count_adjustment_evidence": None,
                    "valid": True,
                    "issues": [],
                    **weight_policy,
                }
            )
            snapshots.append(
                {
                    "month": month,
                    "snapshot_date": snapshot_date,
                    "members": members,
                    "weight_sum": weight_sum,
                    "weight_tolerance": "0.5",
                    "component_count_adjustment_evidence": None,
                    "source_response_sha256": "b" * 64,
                    **weight_policy,
                }
            )
        checks.append(
            {
                "month": month,
                "request_artifact_sha256": "a" * 64,
                "response_sha256": "b" * 64,
                "snapshots": coverage_snapshots,
                "selected_snapshot_date": dates[-1],
                "status": "complete",
                "issues": [],
            }
        )
    summary = {
        "pit_snapshot_count": len(snapshots),
        "snapshot_dates": snapshot_dates,
        "missing_months": [],
        "duplicate_member_count": 0,
        "zero_weight_count_by_snapshot": {
            snapshot_date: 1 for snapshot_date in snapshot_dates
        },
        "weight_sum_by_snapshot": {
            snapshot_date: weight_sum for snapshot_date in snapshot_dates
        },
    }
    locked = {
        "access": "NOT_ACCESSED",
        "download": "NOT_DOWNLOADED",
        "run": "NOT_RUN",
    }
    report = _self_hash(
        {
            "schema_version": "pit-membership-coverage-report.v3",
            "experiment_id": "a-share-technical-alpha-feasibility-tushare-p1-v1",
            "generated_at": "2026-08-31T00:00:00+00:00",
            "index_code": BENCHMARK,
            "pit_months_expected": 73,
            "pit_months_observed": 73,
            "monthly_checks": checks,
            "stage_status": "PIT_MEMBERSHIP_READY",
            "terminal_status": None,
            "remaining_blockers": [],
            "locked_test_status": locked,
            "locked_test_consumed": False,
            **summary,
        },
        "report_sha256",
    )
    manifest = _self_hash(
        {
            "schema_version": "pit-membership-manifest.v3",
            "experiment_id": "a-share-technical-alpha-feasibility-tushare-p1-v1",
            "generated_at": "2026-08-31T00:00:00+00:00",
            "index_code": BENCHMARK,
            "coverage_start_month": "2017-12",
            "coverage_end_month": "2023-12",
            "pit_months_expected": 73,
            "pit_months_observed": 73,
            "snapshots": snapshots,
            "union_instrument_count": len(instrument_ids),
            "union_instrument_ids": list(instrument_ids),
            "stage_status": "PIT_MEMBERSHIP_READY",
            "remaining_blockers": [],
            "locked_test_status": locked,
            "locked_test_consumed": False,
            **summary,
        },
        "manifest_sha256",
    )
    return tuple(memberships), PITAdmissionArtifacts(report, manifest)


def _weekdays(start: date, end: date) -> tuple[date, ...]:
    result: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5 and current != date(2023, 1, 2):
            result.append(current)
        current += timedelta(days=1)
    return tuple(result)


class _PoisonIterable:
    def __iter__(self):
        raise AssertionError("input iterable must not be touched")


class AlphaFeasibilityDateGuardTests(unittest.TestCase):
    def _poison_input(self, *, coverage_end: date) -> AlphaFeasibilityInput:
        poison = _PoisonIterable()
        return AlphaFeasibilityInput(
            coverage_start=date(2022, 1, 1),
            coverage_end=coverage_end,
            trading_dates=poison,
            memberships=poison,
            stock_signal_bars=poison,
            benchmark_signal_bars=poison,
            suspensions=poison,
        )

    def test_forbidden_split_is_rejected_before_any_input_iteration(self) -> None:
        with self.assertRaises(LockedTestAccessForbidden):
            run_alpha_feasibility_comparison(
                split="locked_test",
                inputs=self._poison_input(coverage_end=date(2023, 12, 31)),
            )

    def test_future_coverage_is_rejected_before_any_input_iteration(self) -> None:
        with self.assertRaises(LockedTestAccessForbidden):
            run_alpha_feasibility_comparison(
                split="validation",
                inputs=self._poison_input(coverage_end=date(2024, 1, 1)),
            )

    def test_cost_parameters_cannot_be_tuned_through_the_feasibility_api(self) -> None:
        with self.assertRaisesRegex(AlphaFeasibilityDataError, "cost scenarios are frozen"):
            ProportionalCostScenario("base", commission_rate=D("0.00017"))

    def test_scenario_duck_type_cannot_bypass_frozen_costs(self) -> None:
        with self.assertRaisesRegex(AlphaFeasibilityDataError, "exact frozen"):
            run_alpha_feasibility(
                split="validation",
                inputs=self._poison_input(coverage_end=date(2023, 12, 31)),
                scenario=SimpleNamespace(name="base", buy_rate=D("0"), sell_rate=D("0")),
            )


class AlphaFeasibilityPITTests(unittest.TestCase):
    def test_same_day_snapshot_is_visible_but_future_snapshot_never_backfills(self) -> None:
        snapshots = (
            PITMembershipSnapshot(date(2022, 12, 1), ("A", "B")),
            PITMembershipSnapshot(date(2023, 1, 3), ("B", "C")),
        )
        self.assertEqual(
            select_pit_membership(snapshots, date(2023, 1, 3)),
            ("B", "C"),
        )
        self.assertEqual(
            select_pit_membership(snapshots, date(2023, 1, 2)),
            ("A", "B"),
        )
        with self.assertRaisesRegex(AlphaFeasibilityDataError, "no PIT membership"):
            select_pit_membership(snapshots, date(2022, 11, 30))

    def test_mixed_scale_and_zero_weight_manifest_remains_admissible(self) -> None:
        instrument_ids = tuple(f"{index + 1:06d}.SZ" for index in range(30))
        weights = ["0", *(["3.3"] * 20), *(["3.8"] * 8), "3.6"]
        memberships, admission = _pit_fixture(
            instrument_ids,
            weights_override=weights,
        )
        af._verify_pit_admission(
            SimpleNamespace(pit_admission=admission),
            memberships,
        )

    def test_p15_v3_data_ready_all_snapshots_are_admitted_by_engine(self) -> None:
        memberships, admission = _p15_pit_fixture()
        self.assertEqual(len(memberships), 74)
        self.assertEqual(admission.manifest["pit_snapshot_count"], 74)
        self.assertEqual(
            admission.manifest["snapshots"][0]["warnings"],
            ["weight_sum_outside_warning_range"],
        )
        af._verify_pit_admission(
            SimpleNamespace(pit_admission=admission),
            memberships,
        )

    def test_p15_v3_hard_range_and_warning_metadata_fail_closed(self) -> None:
        memberships, admission = _p15_pit_fixture()
        for name, field, replacement in (
            ("hard_range", "weight_sum", "99.4"),
            ("warning", "warnings", []),
            ("component_count", "component_count", 799),
        ):
            with self.subTest(name=name):
                report = json.loads(json.dumps(admission.coverage_report))
                report["monthly_checks"][0]["snapshots"][0][field] = replacement
                report.pop("report_sha256")
                forged = PITAdmissionArtifacts(
                    coverage_report=_self_hash(report, "report_sha256"),
                    manifest=admission.manifest,
                )
                with self.assertRaisesRegex(
                    AlphaFeasibilityDataError,
                    "P1.5 PIT snapshot policy mismatch",
                ):
                    af._verify_pit_admission(
                        SimpleNamespace(pit_admission=forged),
                        memberships,
                    )

    def test_p15_v3_negative_weight_is_rejected(self) -> None:
        memberships, admission = _p15_pit_fixture()
        manifest = json.loads(json.dumps(admission.manifest))
        manifest["snapshots"][0]["members"][0]["weight"] = "-0.1"
        manifest.pop("manifest_sha256")
        forged = PITAdmissionArtifacts(
            coverage_report=admission.coverage_report,
            manifest=_self_hash(manifest, "manifest_sha256"),
        )
        with self.assertRaisesRegex(
            AlphaFeasibilityDataError,
            "PIT member weight format is invalid",
        ):
            af._verify_pit_admission(
                SimpleNamespace(pit_admission=forged),
                memberships,
            )

    def test_manifest_cannot_inflate_zero_weight_rounding_tolerance(self) -> None:
        instrument_ids = tuple(f"{index + 1:06d}.SZ" for index in range(30))
        weights = ["0", *(["3.3"] * 20), *(["3.8"] * 8), "3.6"]
        memberships, admission = _pit_fixture(
            instrument_ids,
            weights_override=weights,
        )
        manifest = json.loads(json.dumps(admission.manifest))
        for snapshot in manifest["snapshots"]:
            snapshot["weight_tolerance"] = "400"
        manifest.pop("manifest_sha256")
        manifest = _self_hash(manifest, "manifest_sha256")

        with self.assertRaisesRegex(
            AlphaFeasibilityDataError,
            "PIT manifest weight check failed",
        ):
            af._verify_pit_admission(
                SimpleNamespace(
                    pit_admission=PITAdmissionArtifacts(
                        coverage_report=admission.coverage_report,
                        manifest=manifest,
                    )
                ),
                memberships,
            )

    def test_high_precision_weight_sum_is_not_rounded_into_tolerance(self) -> None:
        weights = [
            "100.000000000000000000000000000001",
            *(["0.000000000000000000000000000001"] * 29),
        ]
        exact = af._exact_decimal_sum([D(value) for value in weights])
        self.assertEqual(exact, D("100.000000000000000000000000000030"))

        instrument_ids = tuple(f"{index + 1:06d}.SZ" for index in range(30))
        memberships, admission = _pit_fixture(
            instrument_ids,
            weights_override=weights,
        )
        with self.assertRaisesRegex(
            AlphaFeasibilityDataError,
            "PIT manifest weight check failed",
        ):
            af._verify_pit_admission(
                SimpleNamespace(pit_admission=admission),
                memberships,
            )

    def test_consumer_rejects_weights_outside_adapter_decimal_domain(self) -> None:
        instrument_ids = tuple(f"{index + 1:06d}.SZ" for index in range(30))
        cases = (
            ["3.343" + "0" * 997, *(["3.333"] * 29)],
            ["03.343", *(["3.333"] * 29)],
        )
        for weights in cases:
            with self.subTest(first_weight_length=len(weights[0])):
                memberships, admission = _pit_fixture(
                    instrument_ids,
                    weights_override=weights,
                )
                with self.assertRaisesRegex(
                    AlphaFeasibilityDataError,
                    "PIT member weight format is invalid",
                ):
                    af._verify_pit_admission(
                        SimpleNamespace(pit_admission=admission),
                        memberships,
                    )

    def test_consumer_explicitly_rejects_legacy_pit_v1_artifacts(self) -> None:
        instrument_ids = tuple(f"{index + 1:06d}.SZ" for index in range(30))
        memberships, admission = _pit_fixture(instrument_ids)
        report = json.loads(json.dumps(admission.coverage_report))
        manifest = json.loads(json.dumps(admission.manifest))
        report["schema_version"] = "pit-membership-coverage-report.v1"
        manifest["schema_version"] = "pit-membership-manifest.v1"
        report.pop("report_sha256")
        manifest.pop("manifest_sha256")
        legacy = PITAdmissionArtifacts(
            coverage_report=_self_hash(report, "report_sha256"),
            manifest=_self_hash(manifest, "manifest_sha256"),
        )

        with self.assertRaisesRegex(
            AlphaFeasibilityDataError,
            "PIT admission status is not complete",
        ):
            af._verify_pit_admission(
                SimpleNamespace(pit_admission=legacy),
                memberships,
            )


class AlphaFeasibilityEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sessions = _weekdays(date(2022, 6, 1), date(2023, 12, 29))
        cls.instrument_ids = tuple(f"{index + 1:06d}.SZ" for index in range(30))
        cls.memberships, cls.pit_admission = _pit_fixture(cls.instrument_ids)
        cls.stock_bars: list[SignalBar] = []
        cls.signal_index: dict[tuple[date, str], SignalBar] = {}
        cls.benchmark_bars: list[BenchmarkBar] = []
        for day_number, session in enumerate(cls.sessions):
            benchmark_close = D("100") + D(day_number) * D("0.02")
            benchmark = BenchmarkBar(
                session,
                benchmark_close,
                benchmark_close * D("1.001"),
            )
            cls.benchmark_bars.append(benchmark)
            cls.signal_index[(session, BENCHMARK)] = SignalBar(
                session,
                BENCHMARK,
                benchmark.close,
                benchmark.high,
            )
            for stock_number, instrument_id in enumerate(cls.instrument_ids):
                slope = D("0.01") + D(stock_number) * D("0.001")
                close = D("80") + D(day_number) * slope
                bar = SignalBar(session, instrument_id, close, close * D("1.001"))
                cls.stock_bars.append(bar)
                cls.signal_index[(session, instrument_id)] = bar
        cls.inputs = AlphaFeasibilityInput(
            coverage_start=date(2017, 7, 1),
            coverage_end=date(2023, 12, 31),
            trading_dates=cls.sessions,
            memberships=cls.memberships,
            stock_signal_bars=cls.stock_bars,
            benchmark_signal_bars=cls.benchmark_bars,
            suspensions=(),
            pit_admission=cls.pit_admission,
        )
        cls.comparison = run_alpha_feasibility_comparison(
            split="validation",
            inputs=cls.inputs,
        )

    def test_rank_is_exactly_the_frozen_formal_ranker(self) -> None:
        decision_date = self.sessions[150]
        sessions = self.sessions[:151]
        suspended: set[tuple[date, str]] = set()
        actual = rank_alpha_feasibility_universe(
            decision_date=decision_date,
            sessions=sessions,
            instrument_ids=self.instrument_ids,
            signal_index=self.signal_index,
            benchmark_id=BENCHMARK,
            suspended=suspended,
        )
        statuses = {
            (decision_date, instrument_id): SimpleNamespace(
                suspended=False,
                is_st=False,
                listed=True,
                delisted=False,
            )
            for instrument_id in self.instrument_ids
        }
        expected = rank_technical_formal_universe(
            decision_date=decision_date,
            sessions=sessions,
            instrument_ids=self.instrument_ids,
            signal_index=self.signal_index,  # type: ignore[arg-type]
            benchmark_id=BENCHMARK,
            status_index=statuses,  # type: ignore[arg-type]
        )
        self.assertEqual(actual, expected)
        self.assertEqual(tuple(actual[0].factors or ()), FACTOR_IDS)
        self.assertIs(FROZEN_EXPOSURE_POLICY, DEFAULT_POLICY)

    def test_same_month_snapshots_are_all_admitted_and_selected_causally(self) -> None:
        early_only = "900001.SH"
        early_members = self.instrument_ids[:-1] + (early_only,)
        memberships, admission = _pit_fixture(
            self.instrument_ids,
            same_month_early=("2023-01", early_members),
        )
        self.assertEqual(len(memberships), 74)
        self.assertEqual(
            select_pit_membership(memberships, date(2023, 1, 10)),
            early_members,
        )
        self.assertEqual(
            select_pit_membership(memberships, date(2023, 1, 20)),
            self.instrument_ids,
        )
        extra_bars = []
        for day_number, session in enumerate(self.sessions):
            close = D("75") + D(day_number) * D("0.015")
            extra_bars.append(
                SignalBar(session, early_only, close, close * D("1.001"))
            )
        inputs = AlphaFeasibilityInput(
            coverage_start=date(2017, 7, 1),
            coverage_end=date(2023, 12, 31),
            trading_dates=self.sessions,
            memberships=memberships,
            stock_signal_bars=(*self.stock_bars, *extra_bars),
            benchmark_signal_bars=self.benchmark_bars,
            suspensions=(),
            pit_admission=admission,
        )
        result = run_alpha_feasibility(split="validation", inputs=inputs)
        self.assertTrue(result.nav)
        self.assertEqual(admission.manifest["union_instrument_count"], 31)
        self.assertIn(early_only, admission.manifest["union_instrument_ids"])

    def test_new_member_history_ineligibility_does_not_block_cross_section(self) -> None:
        new_member = "900001.SH"
        introduction = date(2023, 10, 5)
        replacement_members = self.instrument_ids[:-1] + (new_member,)
        memberships, admission = _pit_fixture(
            self.instrument_ids,
            same_month_early=("2023-10", replacement_members),
        )
        first_price_date = next(day for day in self.sessions if day > introduction)
        new_member_bars = []
        for day_number, session in enumerate(
            day for day in self.sessions if day >= first_price_date
        ):
            close = D("70") + D(day_number) * D("0.01")
            new_member_bars.append(
                SignalBar(session, new_member, close, close * D("1.001"))
            )
        inputs = AlphaFeasibilityInput(
            coverage_start=date(2017, 7, 1),
            coverage_end=date(2023, 12, 31),
            trading_dates=self.sessions,
            memberships=memberships,
            stock_signal_bars=(*self.stock_bars, *new_member_bars),
            benchmark_signal_bars=self.benchmark_bars,
            suspensions=(SuspensionRecord(introduction, new_member),),
            pit_admission=admission,
        )

        prepared = af._prepare(inputs, required_end=date(2023, 12, 29))
        introduction_records = af._history_eligibility_records(
            prepared,
            decision_date=introduction,
            instrument_ids=replacement_members,
        )
        self.assertEqual(len(introduction_records), len(replacement_members))
        first_record = next(
            item for item in introduction_records if item.instrument_id == new_member
        )
        self.assertIs(first_record.eligibility, False)
        self.assertEqual(first_record.reason, "ineligible_no_initial_price")
        self.assertNotIn((introduction, new_member), prepared.signal_by_key)

        next_records = af._history_eligibility_records(
            prepared,
            decision_date=first_price_date,
            instrument_ids=replacement_members,
        )
        next_record = next(
            item for item in next_records if item.instrument_id == new_member
        )
        self.assertIs(next_record.eligibility, False)
        self.assertEqual(next_record.reason, "ineligible_insufficient_history")

        original_ranker = af.rank_alpha_feasibility_universe
        with mock.patch.object(
            af,
            "rank_alpha_feasibility_universe",
            wraps=original_ranker,
        ) as ranker:
            result = af.run_alpha_feasibility(split="validation", inputs=inputs)
        relevant_calls = [
            item
            for item in ranker.call_args_list
            if introduction <= item.kwargs["decision_date"] < date(2023, 10, 20)
        ]
        self.assertTrue(relevant_calls)
        self.assertTrue(
            all(
                new_member not in item.kwargs["instrument_ids"]
                for item in relevant_calls
            )
        )
        self.assertTrue(result.nav)

    def test_every_manifest_snapshot_requires_one_matching_monthly_check(self) -> None:
        early_members = self.instrument_ids[:-1] + ("900001.SH",)
        memberships, admission = _pit_fixture(
            self.instrument_ids,
            same_month_early=("2023-01", early_members),
        )
        report = json.loads(json.dumps(admission.coverage_report))
        report.pop("report_sha256")
        january = next(
            item for item in report["monthly_checks"] if item["month"] == "2023-01"
        )
        january["snapshots"] = january["snapshots"][1:]
        altered = AlphaFeasibilityInput(
            coverage_start=date(2017, 7, 1),
            coverage_end=date(2023, 12, 31),
            trading_dates=self.sessions,
            memberships=memberships,
            stock_signal_bars=_PoisonIterable(),
            benchmark_signal_bars=_PoisonIterable(),
            suspensions=_PoisonIterable(),
            pit_admission=PITAdmissionArtifacts(
                _self_hash(report, "report_sha256"),
                admission.manifest,
            ),
        )
        with self.assertRaisesRegex(
            AlphaFeasibilityDataError,
            "coverage and manifest snapshot differ",
        ):
            run_alpha_feasibility(split="validation", inputs=altered)

    def test_fractional_portfolio_never_exceeds_three_names_or_forty_percent(self) -> None:
        decisions = self.comparison.base.decisions
        self.assertTrue(decisions)
        self.assertTrue(any(len(item.target_weights) == 3 for item in decisions))
        for decision in decisions:
            self.assertLessEqual(len(decision.target_weights), 3)
            self.assertTrue(
                all(weight <= MAX_POSITION_WEIGHT for weight in decision.target_weights.values())
            )
            self.assertIn(
                decision.target_gross_exposure,
                {D("0"), D("0.3"), D("0.6"), D("1.0")},
            )

    def test_stress_cost_is_not_below_base_and_minimum_fee_is_disclosed_off(self) -> None:
        self.assertGreaterEqual(
            self.comparison.stress.metrics.total_cost,
            self.comparison.base.metrics.total_cost,
        )
        self.assertFalse(self.comparison.base.minimum_commission_modeled)
        self.assertIn("minimum_5_cny", self.comparison.base.cost_model_semantics)

    def test_metrics_and_per_stock_contribution_reconcile_to_normalized_nav(self) -> None:
        for result in (self.comparison.base, self.comparison.stress):
            metrics = result.metrics
            payload = metrics.to_dict()
            self.assertAlmostEqual(
                float(sum(metrics.per_stock_pnl_contribution.values(), D("0"))),
                float(metrics.net_return),
                places=12,
            )
            self.assertAlmostEqual(float(result.nav[-1].nav - D("1")), float(metrics.net_return), places=12)
            self.assertGreaterEqual(metrics.max_drawdown, D("0"))
            self.assertLessEqual(metrics.max_drawdown, D("1"))
            self.assertGreaterEqual(metrics.positive_month_rate, D("0"))
            self.assertLessEqual(metrics.positive_month_rate, D("1"))
            self.assertAlmostEqual(
                float(sum(metrics.exposure_state_distribution.values(), D("0"))),
                1.0,
                places=12,
            )
            self.assertNotIn("trade_or_rebalance_count", payload)
            self.assertEqual(
                payload["rebalance_count"], metrics.trade_or_rebalance_count
            )
            gross_profit = metrics.net_return + metrics.total_cost
            if gross_profit > D("0"):
                self.assertEqual(
                    D(payload["cost_to_gross_profit"]),
                    metrics.cost_to_gross_profit,
                )
            else:
                self.assertIsNone(payload["cost_to_gross_profit"])

    def test_cost_to_gross_profit_is_null_for_zero_or_negative_gross_profit(self) -> None:
        metrics = self.comparison.base.metrics
        zero_denominator = replace(metrics, net_return=-metrics.total_cost)
        negative_denominator = replace(
            metrics,
            net_return=-metrics.total_cost - D("0.01"),
        )

        self.assertIsNone(zero_denominator.cost_to_gross_profit)
        self.assertIsNone(zero_denominator.to_dict()["cost_to_gross_profit"])
        self.assertIsNone(negative_denominator.cost_to_gross_profit)
        self.assertIsNone(negative_denominator.to_dict()["cost_to_gross_profit"])

    def test_locked_and_execution_semantics_are_fixed_and_serializable(self) -> None:
        payload = self.comparison.to_dict()
        serialized_metrics = payload["base"]["metrics"]
        self.assertEqual(
            payload["locked_test_status"],
            {"access": "NOT_ACCESSED", "download": "NOT_DOWNLOADED", "run": "NOT_RUN"},
        )
        self.assertEqual(LOCKED_TEST_STATUS.access, "NOT_ACCESSED")
        self.assertFalse(payload["locked_test_consumed"])
        self.assertEqual(payload["execution_realism"], "INCOMPLETE")
        self.assertFalse(payload["trade_eligibility"])
        self.assertIn("rebalance_count", serialized_metrics)
        self.assertIn("cost_to_gross_profit", serialized_metrics)
        self.assertNotIn("trade_or_rebalance_count", serialized_metrics)

    def test_held_non_suspended_missing_return_fails_closed(self) -> None:
        first_report_date = next(day for day in self.sessions if day.year == 2023)
        selected = self.comparison.base.decisions[0].selected_instrument_ids[0]
        altered = AlphaFeasibilityInput(
            coverage_start=date(2017, 7, 1),
            coverage_end=date(2023, 12, 31),
            trading_dates=self.sessions,
            memberships=self.memberships,
            stock_signal_bars=(
                bar
                for bar in self.stock_bars
                if not (
                    bar.instrument_id == selected
                    and bar.trading_date == first_report_date
                )
            ),
            benchmark_signal_bars=self.benchmark_bars,
            suspensions=(),
            pit_admission=self.pit_admission,
        )
        with self.assertRaisesRegex(
            AlphaFeasibilityDataError,
            "internal missing session",
        ):
            run_alpha_feasibility(split="validation", inputs=altered)

    def test_suspension_uses_prior_economic_value_instead_of_future_fill(self) -> None:
        first_report_date = next(day for day in self.sessions if day.year == 2023)
        selected = self.comparison.base.decisions[0].selected_instrument_ids[0]
        altered = AlphaFeasibilityInput(
            coverage_start=date(2017, 7, 1),
            coverage_end=date(2023, 12, 31),
            trading_dates=self.sessions,
            memberships=self.memberships,
            stock_signal_bars=(
                bar
                for bar in self.stock_bars
                if not (
                    bar.instrument_id == selected
                    and bar.trading_date == first_report_date
                )
            ),
            benchmark_signal_bars=self.benchmark_bars,
            suspensions=(SuspensionRecord(first_report_date, selected),),
            pit_admission=self.pit_admission,
        )
        result = run_alpha_feasibility(split="validation", inputs=altered)
        self.assertTrue(result.nav)

    def test_suspension_evidence_overrides_same_day_vendor_bar(self) -> None:
        first_report_date = next(day for day in self.sessions if day.year == 2023)
        selected = self.comparison.base.decisions[0].selected_instrument_ids
        altered = AlphaFeasibilityInput(
            coverage_start=date(2017, 7, 1),
            coverage_end=date(2023, 12, 31),
            trading_dates=self.sessions,
            memberships=self.memberships,
            stock_signal_bars=self.stock_bars,
            benchmark_signal_bars=self.benchmark_bars,
            suspensions=tuple(
                SuspensionRecord(first_report_date, instrument_id)
                for instrument_id in selected
            ),
            pit_admission=self.pit_admission,
        )
        result = run_alpha_feasibility(split="validation", inputs=altered)
        self.assertLessEqual(
            abs(result.nav[0].daily_pnl + result.rebalances[0].total_cost),
            D("1e-24"),
        )

    def test_internal_warmup_gap_fails_closed_before_ranking(self) -> None:
        missing_day = date(2022, 12, 1)
        altered = AlphaFeasibilityInput(
            coverage_start=date(2017, 7, 1),
            coverage_end=date(2023, 12, 31),
            trading_dates=self.sessions,
            memberships=self.memberships,
            stock_signal_bars=(
                bar
                for bar in self.stock_bars
                if not (
                    bar.instrument_id == self.instrument_ids[0]
                    and bar.trading_date == missing_day
                )
            ),
            benchmark_signal_bars=self.benchmark_bars,
            suspensions=(),
            pit_admission=self.pit_admission,
        )
        with self.assertRaisesRegex(AlphaFeasibilityDataError, "internal missing session"):
            run_alpha_feasibility(split="validation", inputs=altered)

    def test_truncated_validation_calendar_cannot_claim_full_period(self) -> None:
        cutoff = date(2023, 1, 3)
        sessions = tuple(day for day in self.sessions if day <= cutoff)
        altered = AlphaFeasibilityInput(
            coverage_start=date(2017, 7, 1),
            coverage_end=date(2023, 12, 31),
            trading_dates=sessions,
            memberships=self.memberships,
            stock_signal_bars=(bar for bar in self.stock_bars if bar.trading_date <= cutoff),
            benchmark_signal_bars=(
                bar for bar in self.benchmark_bars if bar.trading_date <= cutoff
            ),
            suspensions=(),
            pit_admission=self.pit_admission,
        )
        with self.assertRaisesRegex(AlphaFeasibilityDataError, "boundary sessions"):
            run_alpha_feasibility(split="validation", inputs=altered)

    def test_alpha_cannot_run_without_verified_73_month_pit_artifacts(self) -> None:
        altered = AlphaFeasibilityInput(
            coverage_start=date(2017, 7, 1),
            coverage_end=date(2023, 12, 31),
            trading_dates=self.sessions,
            memberships=self.memberships,
            stock_signal_bars=self.stock_bars,
            benchmark_signal_bars=self.benchmark_bars,
            suspensions=(),
        )
        with self.assertRaisesRegex(AlphaFeasibilityDataError, "admission artifacts"):
            run_alpha_feasibility(split="validation", inputs=altered)


if __name__ == "__main__":
    unittest.main()
