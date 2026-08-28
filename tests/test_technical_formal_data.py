from __future__ import annotations

import copy
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from research.strategy_workspace.technical_formal_data import (
    AdjustmentFactorPoint,
    CRITICAL_CHECK_IDS,
    CSI800_INDEX_CODE,
    DatasetInventoryEntry,
    DatasetManifestError,
    DualPriceContractError,
    DualPriceSeries,
    ExecutionPricePoint,
    MANIFEST_COVERAGE_END,
    MANIFEST_COVERAGE_START,
    PITMembershipRecord,
    PITMonthlySnapshot,
    PITUniverseError,
    PITUniverseLoader,
    RawDailyBar,
    SignalPricePoint,
    STANDARD_CLI_VERIFICATION_BLOCKER,
    TECHNICAL_FORMAL_DATASET_IDS,
    TechnicalExecutionStatus,
    TechnicalFormalDataError,
    blocked_dataset_inventory,
    build_blocked_dataset_inventory,
    build_dual_price_series,
    build_technical_formal_dataset_manifest,
    required_dataset_coverage_start,
    validate_execution_status_coverage,
    validate_technical_formal_dataset_manifest,
)
from research.strategy_workspace.technical_formal_reporting import (
    verify_dataset_coverage_report,
)


D = Decimal
STOCK = "600000.SH"


def _bar(day: date, open_: str, high: str, low: str, close: str) -> RawDailyBar:
    return RawDailyBar(STOCK, day, D(open_), D(high), D(low), D(close))


def _status(
    day: date,
    *,
    suspended: bool = False,
    is_st: bool = False,
    applicable: bool = True,
    limit_up_locked: bool = False,
    limit_down_locked: bool = False,
    listed: bool = True,
    delisted: bool = False,
) -> TechnicalExecutionStatus:
    return TechnicalExecutionStatus(
        instrument_id=STOCK,
        trading_date=day,
        suspended=suspended,
        is_st=is_st,
        price_limit_applicable=applicable,
        limit_up_price=D("200") if applicable else None,
        limit_down_price=D("1") if applicable else None,
        limit_up_locked=limit_up_locked,
        limit_down_locked=limit_down_locked,
        listed=listed,
        delisted=delisted,
        lot_size=100,
        t_plus_one=True,
    )


class DualPriceContractTests(unittest.TestCase):
    def test_ex_dividend_day_has_no_false_loss_and_execution_remains_raw(self) -> None:
        first = date(2020, 6, 1)
        ex_day = date(2020, 6, 2)
        bars = (
            _bar(first, "100", "101", "99", "100"),
            _bar(ex_day, "50", "51", "49", "50"),
        )
        factors = (
            AdjustmentFactorPoint(STOCK, first, D("1")),
            AdjustmentFactorPoint(STOCK, ex_day, D("2")),
        )
        series = build_dual_price_series(
            bars, factors, (_status(first), _status(ex_day))
        )

        self.assertEqual(series.signal[1].daily_return, D("0"))
        self.assertEqual(series.signal[1].close, D("1"))
        self.assertEqual(series.signal[1].high, D("1.02"))
        self.assertEqual(series.execution[1].open, D("50"))
        self.assertEqual(series.execution[1].close, D("50"))
        self.assertEqual(series.execution[1].adjustment_factor, D("2"))
        self.assertIs(type(series.signal[1]), SignalPricePoint)
        self.assertIs(type(series.execution[1]), ExecutionPricePoint)

    def test_future_factor_append_cannot_rewrite_historical_signal(self) -> None:
        first = date(2020, 6, 1)
        second = date(2020, 6, 2)
        future = date(2020, 6, 3)
        bars = (
            _bar(first, "100", "101", "99", "100"),
            _bar(second, "50", "51", "49", "50"),
        )
        states = (_status(first), _status(second))
        current = (
            AdjustmentFactorPoint(STOCK, first, D("1")),
            AdjustmentFactorPoint(STOCK, second, D("2")),
        )
        appended = current + (
            AdjustmentFactorPoint(
                STOCK,
                future,
                D("999"),
                corporate_action_entitled=False,
            ),
        )

        self.assertEqual(
            build_dual_price_series(bars, current, states),
            build_dual_price_series(bars, appended, states),
        )

    def test_arbitrary_observed_start_rebases_total_return_index_to_one(self) -> None:
        first = date(2020, 6, 1)
        second = date(2020, 6, 2)
        third = date(2020, 6, 3)
        series = build_dual_price_series(
            (
                _bar(first, "10", "10.5", "9.5", "10"),
                _bar(second, "11", "11.5", "10.5", "11"),
                _bar(third, "6", "6.5", "5.5", "6"),
            ),
            (
                AdjustmentFactorPoint(STOCK, first, D("1")),
                AdjustmentFactorPoint(STOCK, third, D("2")),
            ),
            (_status(first), _status(second), _status(third)),
            start_date=second,
        )

        self.assertEqual(series.start_date, second)
        self.assertIsNone(series.signal[0].daily_return)
        self.assertEqual(series.signal[0].cumulative_total_return_index, D("1"))
        self.assertEqual(series.signal[1].daily_return, D("12") / D("11") - D("1"))
        self.assertEqual(series.signal[1].close, D("12") / D("11"))

    def test_factor_transition_without_entitlement_fails_closed(self) -> None:
        first = date(2020, 6, 1)
        second = date(2020, 6, 2)
        with self.assertRaises(DualPriceContractError):
            build_dual_price_series(
                (
                    _bar(first, "100", "101", "99", "100"),
                    _bar(second, "50", "51", "49", "50"),
                ),
                (
                    AdjustmentFactorPoint(STOCK, first, D("1")),
                    AdjustmentFactorPoint(
                        STOCK,
                        second,
                        D("2"),
                        corporate_action_entitled=False,
                    ),
                ),
                (_status(first), _status(second)),
            )

    def test_unentitled_intermediate_factor_cannot_hide_between_raw_bars(self) -> None:
        first = date(2020, 6, 1)
        suspended = date(2020, 6, 2)
        third = date(2020, 6, 3)
        with self.assertRaises(DualPriceContractError):
            build_dual_price_series(
                (
                    _bar(first, "100", "101", "99", "100"),
                    _bar(third, "50", "51", "49", "50"),
                ),
                (
                    AdjustmentFactorPoint(STOCK, first, D("1")),
                    AdjustmentFactorPoint(
                        STOCK,
                        suspended,
                        D("1.5"),
                        corporate_action_entitled=False,
                    ),
                    AdjustmentFactorPoint(STOCK, third, D("2")),
                ),
                (
                    _status(first),
                    _status(suspended, suspended=True),
                    _status(third),
                ),
            )

    def test_exact_channel_types_prevent_signal_execution_mix(self) -> None:
        day = date(2020, 6, 1)
        series = build_dual_price_series(
            (_bar(day, "10", "11", "9", "10"),),
            (AdjustmentFactorPoint(STOCK, day, D("1")),),
            (_status(day),),
        )
        with self.assertRaises(DualPriceContractError):
            DualPriceSeries(
                signal=(series.execution[0],),  # type: ignore[arg-type]
                execution=series.execution,
                start_date=day,
            )
        with self.assertRaises(TechnicalFormalDataError):
            ExecutionPricePoint(
                STOCK,
                day,
                10.0,  # type: ignore[arg-type]
                D("11"),
                D("9"),
                D("10"),
                D("1"),
                False,
                False,
                True,
                D("20"),
                D("5"),
                False,
                False,
                True,
                False,
                100,
                True,
            )

    def test_no_limit_session_is_explicit_and_never_invents_bounds(self) -> None:
        day = date(2020, 6, 1)
        series = build_dual_price_series(
            (_bar(day, "10", "12", "8", "11"),),
            (AdjustmentFactorPoint(STOCK, day, D("1")),),
            (_status(day, applicable=False),),
        )
        point = series.execution[0]
        self.assertFalse(point.price_limit_applicable)
        self.assertIsNone(point.limit_up_price)
        self.assertIsNone(point.limit_down_price)
        with self.assertRaises(TechnicalFormalDataError):
            TechnicalExecutionStatus(
                STOCK,
                day,
                False,
                False,
                False,
                None,
                None,
                True,
                False,
                True,
                False,
                100,
                True,
            )

    def test_suspension_status_has_no_synthetic_raw_execution_point(self) -> None:
        first = date(2020, 6, 1)
        suspended = date(2020, 6, 2)
        third = date(2020, 6, 3)
        statuses = (
            _status(first),
            _status(suspended, suspended=True),
            _status(third),
        )
        self.assertEqual(
            validate_execution_status_coverage(statuses, (first, suspended, third)),
            statuses,
        )
        series = build_dual_price_series(
            (
                _bar(first, "10", "11", "9", "10"),
                _bar(third, "11", "12", "10", "11"),
            ),
            (AdjustmentFactorPoint(STOCK, first, D("1")),),
            statuses,
        )
        self.assertEqual(tuple(item.trading_date for item in series.execution), (first, third))
        self.assertNotIn(suspended, {item.trading_date for item in series.execution})

        non_suspended_missing_bar = (
            _status(first),
            _status(suspended),
            _status(third),
        )
        with self.assertRaises(DualPriceContractError):
            build_dual_price_series(
                (
                    _bar(first, "10", "11", "9", "10"),
                    _bar(third, "11", "12", "10", "11"),
                ),
                (AdjustmentFactorPoint(STOCK, first, D("1")),),
                non_suspended_missing_bar,
            )

    def test_status_calendar_and_raw_bar_coverage_fail_closed_independently(self) -> None:
        first = date(2020, 6, 1)
        second = date(2020, 6, 2)
        with self.assertRaises(DualPriceContractError):
            validate_execution_status_coverage((_status(first),), (first, second))
        with self.assertRaises(DualPriceContractError):
            build_dual_price_series(
                (
                    _bar(first, "10", "11", "9", "10"),
                    _bar(second, "11", "12", "10", "11"),
                ),
                (AdjustmentFactorPoint(STOCK, first, D("1")),),
                (_status(first),),
            )


def _members(snapshot_date: date, *, final_weight: str = "0.141") -> tuple[PITMembershipRecord, ...]:
    return tuple(
        PITMembershipRecord(
            index_code=CSI800_INDEX_CODE,
            component_id=f"{600000 + index:06d}.SH",
            snapshot_date=snapshot_date,
            weight=D(final_weight if index == 799 else "0.125"),
        )
        for index in range(800)
    )


def _snapshot(snapshot_date: date, *, final_weight: str = "0.141") -> PITMonthlySnapshot:
    return PITMonthlySnapshot(
        index_code=CSI800_INDEX_CODE,
        snapshot_date=snapshot_date,
        members=_members(snapshot_date, final_weight=final_weight),
    )


def _index_weight_rows(snapshot_dates: tuple[date, ...]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for snapshot_date in snapshot_dates:
        for index in range(800):
            rows.append(
                {
                    "index_code": CSI800_INDEX_CODE,
                    "con_code": f"{600000 + index:06d}.SH",
                    "trade_date": snapshot_date.strftime("%Y%m%d"),
                    "weight": "0.141" if index == 799 else "0.125",
                }
            )
    return rows


class PITUniverseTests(unittest.TestCase):
    def test_index_weight_candidate_rows_build_strict_monthly_pit_loader(self) -> None:
        bootstrap = date(2019, 12, 31)
        january = date(2020, 1, 31)
        loader = PITUniverseLoader.from_index_weight_rows(
            _index_weight_rows((bootstrap, january)),
            coverage_start=date(2020, 1, 1),
            coverage_end=january,
        )

        self.assertEqual(len(loader.snapshots), 2)
        self.assertEqual(loader.snapshots[0].weight_sum, D("100.016"))
        self.assertEqual(loader.snapshots[0].weight_tolerance, D("0.4000"))
        self.assertEqual(loader.snapshot_strictly_before(date(2020, 1, 2)).snapshot_date, bootstrap)
        self.assertEqual(loader.snapshot_strictly_before(january).snapshot_date, bootstrap)
        self.assertEqual(len(loader.members_strictly_before(date(2020, 1, 2))), 800)

    def test_raw_index_weight_rejects_unknown_float_duplicate_and_disordered_rows(self) -> None:
        bootstrap = date(2019, 12, 31)
        january = date(2020, 1, 31)
        rows = _index_weight_rows((bootstrap, january))

        unknown = [dict(rows[0], unexpected="x")]
        with self.assertRaises(PITUniverseError):
            PITUniverseLoader.from_index_weight_rows(
                unknown,
                coverage_start=date(2020, 1, 1),
                coverage_end=january,
            )

        float_weight = [dict(rows[0], weight=0.125)]  # type: ignore[dict-item]
        with self.assertRaises(PITUniverseError):
            PITUniverseLoader.from_index_weight_rows(
                float_weight,
                coverage_start=date(2020, 1, 1),
                coverage_end=january,
            )

        duplicate = rows[:1] + rows[:1] + rows[1:]
        with self.assertRaises(PITUniverseError):
            PITUniverseLoader.from_index_weight_rows(
                duplicate,
                coverage_start=date(2020, 1, 1),
                coverage_end=january,
            )

        disordered = rows[:799] + [rows[800], rows[799]] + rows[801:]
        with self.assertRaises(PITUniverseError):
            PITUniverseLoader.from_index_weight_rows(
                disordered,
                coverage_start=date(2020, 1, 1),
                coverage_end=january,
            )

    def test_missing_month_snapshot_order_member_count_and_weight_sum_fail_closed(self) -> None:
        bootstrap = _snapshot(date(2019, 12, 31))
        january = _snapshot(date(2020, 1, 31))
        february = _snapshot(date(2020, 2, 28))
        with self.assertRaises(PITUniverseError):
            PITUniverseLoader(
                (bootstrap, february),
                coverage_start=date(2020, 1, 1),
                coverage_end=date(2020, 2, 29),
            )
        with self.assertRaises(PITUniverseError):
            PITUniverseLoader(
                (january, bootstrap),
                coverage_start=date(2020, 1, 1),
                coverage_end=january.snapshot_date,
            )
        with self.assertRaises(PITUniverseError):
            PITMonthlySnapshot(
                CSI800_INDEX_CODE,
                january.snapshot_date,
                january.members[:-1],
            )
        with self.assertRaises(PITUniverseError):
            _snapshot(january.snapshot_date, final_weight="1.000")

        coarse = tuple(
            PITMembershipRecord(
                CSI800_INDEX_CODE,
                f"{index + 1:06d}.SZ",
                january.snapshot_date,
                D("0.100"),
            )
            for index in range(800)
        )
        with self.assertRaisesRegex(PITUniverseError, "weight sum"):
            PITMonthlySnapshot(CSI800_INDEX_CODE, january.snapshot_date, coarse)

        with self.assertRaisesRegex(PITUniverseError, "precision"):
            PITMembershipRecord(
                CSI800_INDEX_CODE,
                STOCK,
                january.snapshot_date,
                D("0.1"),
            )

    def test_duplicate_member_and_nonpositive_weight_fail_closed(self) -> None:
        day = date(2020, 1, 31)
        members = list(_members(day))
        members[-1] = members[0]
        with self.assertRaises(PITUniverseError):
            PITMonthlySnapshot(CSI800_INDEX_CODE, day, tuple(members))
        with self.assertRaises(TechnicalFormalDataError):
            PITMembershipRecord(CSI800_INDEX_CODE, STOCK, day, D("0"))

    def test_decision_date_must_be_in_coverage_and_uses_strict_prior_snapshot(self) -> None:
        loader = PITUniverseLoader(
            (_snapshot(date(2019, 12, 31)), _snapshot(date(2020, 1, 31))),
            coverage_start=date(2020, 1, 1),
            coverage_end=date(2020, 1, 31),
        )
        self.assertEqual(
            loader.snapshot_strictly_before(date(2020, 1, 31)).snapshot_date,
            date(2019, 12, 31),
        )
        with self.assertRaises(PITUniverseError):
            loader.snapshot_strictly_before(date(2020, 2, 1))


def _critical_checks(value: bool = True) -> dict[str, bool]:
    return {check_id: value for check_id in CRITICAL_CHECK_IDS}


_STANDARD_SOURCE_INTERFACE = {
    "trade_calendar": ("tushare_standard_non_vip", "trade_cal"),
    "raw_daily_bar": ("tushare_standard_non_vip", "daily"),
    "adjustment_factor": ("tushare_standard_non_vip", "adj_factor"),
    "csi800_pit_membership": ("tushare_standard_non_vip", "index_weight"),
    "suspension_history": ("tushare_standard_non_vip", "suspend_d"),
    "price_limit_history": ("tushare_standard_non_vip", "stk_limit"),
    "name_and_st_history": ("tushare_standard_non_vip", "namechange"),
    "security_master": ("tushare_standard_non_vip", "stock_basic"),
    "csi800_price_benchmark": ("tushare_standard_non_vip", "index_daily"),
}


def _complete_inventory() -> tuple[DatasetInventoryEntry, ...]:
    return tuple(
        DatasetInventoryEntry(
            dataset_id=dataset_id,
            status="complete",
            source=_STANDARD_SOURCE_INTERFACE[dataset_id][0],
            interface=_STANDARD_SOURCE_INTERFACE[dataset_id][1],
            record_count=1,
            coverage_start=required_dataset_coverage_start(dataset_id),
            coverage_end=MANIFEST_COVERAGE_END,
            missing_dates=(),
            content_sha256="a" * 64,
            issues=(),
        )
        for dataset_id in TECHNICAL_FORMAL_DATASET_IDS
    )


class DatasetManifestTests(unittest.TestCase):
    def test_manual_complete_claims_cannot_unlock_ready_and_hash_interoperates(self) -> None:
        manifest = build_technical_formal_dataset_manifest(
            _complete_inventory(),
            dataset_id="technical-momentum-formal-dataset-v1",
            generated_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
            critical_checks=_critical_checks(),
        )

        self.assertEqual(manifest["data_status"], "BLOCKED")
        self.assertIn(STANDARD_CLI_VERIFICATION_BLOCKER, manifest["remaining_blockers"])
        self.assertEqual(manifest["locked_test_status"], "NOT_RUN")
        self.assertIs(manifest["locked_test_consumed"], False)
        validate_technical_formal_dataset_manifest(manifest)
        verify_dataset_coverage_report(manifest)

    def test_per_dataset_minimum_coverage_prevents_false_ready(self) -> None:
        entries = list(_complete_inventory())
        target = TECHNICAL_FORMAL_DATASET_IDS.index("trade_calendar")
        entries[target] = DatasetInventoryEntry(
            dataset_id="trade_calendar",
            status="complete",
            source="tushare_standard_non_vip",
            interface="trade_cal",
            record_count=1,
            coverage_start=date(2018, 1, 2),
            coverage_end=MANIFEST_COVERAGE_END,
            missing_dates=(),
            content_sha256="b" * 64,
            issues=(),
        )
        manifest = build_technical_formal_dataset_manifest(
            entries,
            dataset_id="technical-momentum-formal-dataset-v1",
            generated_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
            critical_checks=_critical_checks(),
        )
        self.assertEqual(manifest["data_status"], "BLOCKED")
        self.assertIn(
            "trade_calendar:warmup_or_start_coverage_missing",
            manifest["remaining_blockers"],
        )
        verify_dataset_coverage_report(manifest)

    def test_blocked_inventory_names_all_nine_datasets_and_interoperates(self) -> None:
        manifest = build_technical_formal_dataset_manifest(
            build_blocked_dataset_inventory(),
            dataset_id="technical-momentum-formal-dataset-v1",
            generated_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
            critical_checks=_critical_checks(False),
        )
        inventory = blocked_dataset_inventory(manifest)
        self.assertEqual(set(inventory), set(TECHNICAL_FORMAL_DATASET_IDS))
        self.assertEqual(manifest["data_status"], "BLOCKED")
        self.assertTrue(manifest["remaining_blockers"])
        verify_dataset_coverage_report(manifest)

    def test_complete_status_cannot_hide_missing_dates_or_issues(self) -> None:
        with self.assertRaises(DatasetManifestError):
            DatasetInventoryEntry(
                dataset_id="trade_calendar",
                status="complete",
                source="baostock",
                interface="trade_calendar",
                record_count=1,
                coverage_start=MANIFEST_COVERAGE_START,
                coverage_end=MANIFEST_COVERAGE_END,
                missing_dates=("2019-01-02",),
                content_sha256="c" * 64,
                issues=(),
            )
        with self.assertRaises(DatasetManifestError):
            DatasetInventoryEntry(
                dataset_id="trade_calendar",
                status="complete",
                source="baostock",
                interface="trade_calendar",
                record_count=1,
                coverage_start=MANIFEST_COVERAGE_START,
                coverage_end=MANIFEST_COVERAGE_END,
                missing_dates=(),
                content_sha256="c" * 64,
                issues=("hidden_problem",),
            )
        with self.assertRaises(DatasetManifestError):
            build_technical_formal_dataset_manifest(
                _complete_inventory(),
                dataset_id="technical-momentum-formal-dataset-v1",
                generated_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
                critical_checks=_critical_checks(),
                remaining_blockers="caller_string_is_not_an_array",  # type: ignore[arg-type]
            )

    def test_manifest_hash_and_locked_boundary_are_tamper_evident(self) -> None:
        manifest = build_technical_formal_dataset_manifest(
            _complete_inventory(),
            dataset_id="technical-momentum-formal-dataset-v1",
            generated_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
            critical_checks=_critical_checks(),
        )
        changed = copy.deepcopy(manifest)
        changed["datasets"]["raw_daily_bar"]["record_count"] = 2
        with self.assertRaises(DatasetManifestError):
            validate_technical_formal_dataset_manifest(changed)
        consumed = copy.deepcopy(manifest)
        consumed["locked_test_consumed"] = True
        with self.assertRaises(DatasetManifestError):
            validate_technical_formal_dataset_manifest(consumed)


if __name__ == "__main__":
    unittest.main()
