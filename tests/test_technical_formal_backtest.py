from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import unittest

from research.strategy_workspace.technical_alpha_shadow_v1 import (
    rank_technical_alpha_shadow,
)
from research.strategy_workspace.technical_exposure_shadow_v1 import DEFAULT_POLICY
from research.strategy_workspace.technical_formal_backtest import (
    BASE_COST,
    ENTRY_PERCENTILE,
    FACTOR_DIRECTIONS,
    FACTOR_IDS,
    FROZEN_EXPOSURE_POLICY,
    HOLD_PERCENTILE,
    STRESS_COST,
    CorporateActionDataGap,
    CorporateActionEntitlement,
    LockedTestAccessForbidden,
    TechnicalDecision,
    TechnicalExecutionEvent,
    TechnicalFormalDataError,
    TechnicalInputPartition,
    TechnicalNavPoint,
    _ClosedLot,
    _Lot,
    _apply_corporate_actions,
    _cost,
    _execute_decision,
    _mark_positions_at_raw_close,
    _members,
    _performance,
    _validate_execution_status_alignment,
    _validate_signal_execution_factor_alignment,
    rank_technical_formal_universe,
    run_technical_formal_backtest,
)
from research.strategy_workspace.technical_formal_data import (
    ExecutionPricePoint,
    PITMembershipRecord,
    PITMonthlySnapshot,
    PITUniverseLoader,
    SignalPricePoint,
    TechnicalExecutionStatus,
)


D = Decimal
CSI800 = "000906.SH"
CN_TZ = timezone(timedelta(hours=8))


def _status(
    instrument_id: str,
    trading_date: date,
    *,
    suspended: bool = False,
    is_st: bool = False,
    price_limit_applicable: bool = False,
    limit_up_locked: bool = False,
    limit_down_locked: bool = False,
    listed: bool = True,
    delisted: bool = False,
) -> TechnicalExecutionStatus:
    return TechnicalExecutionStatus(
        instrument_id=instrument_id,
        trading_date=trading_date,
        suspended=suspended,
        is_st=is_st,
        price_limit_applicable=price_limit_applicable,
        limit_up_price=D("11") if price_limit_applicable else None,
        limit_down_price=D("9") if price_limit_applicable else None,
        limit_up_locked=limit_up_locked,
        limit_down_locked=limit_down_locked,
        listed=listed,
        delisted=delisted,
        lot_size=100,
        t_plus_one=True,
    )


def _execution(
    status: TechnicalExecutionStatus,
    *,
    price: Decimal = D("10"),
    adjustment_factor: Decimal = D("1"),
) -> ExecutionPricePoint:
    if status.limit_up_locked:
        price = status.limit_up_price  # type: ignore[assignment]
    elif status.limit_down_locked:
        price = status.limit_down_price  # type: ignore[assignment]
    return ExecutionPricePoint(
        instrument_id=status.instrument_id,
        trading_date=status.trading_date,
        open=price,
        high=price,
        low=price,
        close=price,
        adjustment_factor=adjustment_factor,
        suspended=status.suspended,
        is_st=status.is_st,
        price_limit_applicable=status.price_limit_applicable,
        limit_up_price=status.limit_up_price,
        limit_down_price=status.limit_down_price,
        limit_up_locked=status.limit_up_locked,
        limit_down_locked=status.limit_down_locked,
        listed=status.listed,
        delisted=status.delisted,
        lot_size=status.lot_size,
        t_plus_one=status.t_plus_one,
    )


def _signal(
    instrument_id: str,
    trading_date: date,
    close: Decimal,
    *,
    adjustment_factor: Decimal = D("1"),
) -> SignalPricePoint:
    return SignalPricePoint(
        instrument_id=instrument_id,
        trading_date=trading_date,
        open=close,
        high=close * D("1.01"),
        low=close * D("0.99"),
        close=close,
        daily_return=None,
        cumulative_total_return_index=close,
        adjustment_factor=adjustment_factor,
    )


def _decision(
    execution_date: date,
    selected: tuple[str, ...] = (),
    weights: dict[str, Decimal] | None = None,
) -> TechnicalDecision:
    return TechnicalDecision(
        decision_date=execution_date - timedelta(days=1),
        execution_date=execution_date,
        selected_instrument_ids=selected,
        target_weights=weights or {},
        market_state="NEUTRAL",
        target_gross_exposure=D("0.60"),
        eligible_count=800,
        entry_count=len(selected),
    )


class _ExplodingIterable:
    def __iter__(self):
        raise AssertionError("forbidden input was read")


class _CountingSentinel:
    def __init__(self) -> None:
        self.iteration_count = 0

    def __iter__(self):
        self.iteration_count += 1
        raise AssertionError("partition rows were read before metadata rejection")


class TechnicalFormalLockedBoundaryTests(unittest.TestCase):
    def test_locked_split_rejects_before_any_iterable_or_loader_access(self) -> None:
        exploding = _ExplodingIterable()
        with self.assertRaisesRegex(LockedTestAccessForbidden, "NOT_RUN"):
            run_technical_formal_backtest(
                split="locked_test",
                trading_calendar=exploding,
                universe_loader=object(),  # type: ignore[arg-type]
                signal_prices=exploding,
                execution_prices=exploding,
                execution_statuses=exploding,
                benchmark_id=CSI800,
                corporate_actions=exploding,
            )

    def test_loader_with_locked_membership_is_rejected_without_snapshot_scan(self) -> None:
        loader = object.__new__(PITUniverseLoader)
        object.__setattr__(loader, "coverage_start", date(2018, 1, 1))
        object.__setattr__(loader, "coverage_end", date(2025, 12, 31))
        object.__setattr__(loader, "snapshots", _ExplodingIterable())
        exploding = _ExplodingIterable()
        with self.assertRaisesRegex(
            TechnicalFormalDataError, "physically partitioned"
        ):
            run_technical_formal_backtest(
                split="development",
                trading_calendar=(date(2018, 1, 2), date(2018, 1, 3)),
                universe_loader=loader,
                signal_prices=exploding,
                execution_prices=exploding,
                execution_statuses=exploding,
                benchmark_id=CSI800,
                corporate_actions=exploding,
            )

    def test_price_channels_require_exact_types_and_reject_mixing(self) -> None:
        loader = object.__new__(PITUniverseLoader)
        object.__setattr__(loader, "coverage_start", date(2018, 1, 1))
        object.__setattr__(loader, "coverage_end", date(2022, 12, 31))
        object.__setattr__(loader, "snapshots", ())
        day = date(2018, 1, 2)
        status = _status("000001.SZ", day)
        execution = _execution(status)
        with self.assertRaisesRegex(
            TechnicalFormalDataError, "exact SignalPricePoint"
        ):
            run_technical_formal_backtest(
                split="development",
                trading_calendar=TechnicalInputPartition(
                    "trading_calendar",
                    date(2018, 1, 1),
                    date(2022, 12, 31),
                    (day, day + timedelta(days=1)),
                ),
                universe_loader=loader,
                signal_prices=TechnicalInputPartition(
                    "signal_prices",
                    date(2018, 1, 1),
                    date(2022, 12, 31),
                    (execution,),
                ),
                execution_prices=TechnicalInputPartition(
                    "execution_prices",
                    date(2018, 1, 1),
                    date(2022, 12, 31),
                    (),
                ),
                execution_statuses=TechnicalInputPartition(
                    "execution_statuses",
                    date(2018, 1, 1),
                    date(2022, 12, 31),
                    (),
                ),
                benchmark_id=CSI800,
            )

        next_day = day + timedelta(days=1)
        next_status = _status("000001.SZ", next_day)
        executions = {
            (day, "000001.SZ"): _execution(status),
            (next_day, "000001.SZ"): _execution(next_status),
        }
        impossible = {
            (day, "000001.SZ"): SignalPricePoint(
                "000001.SZ", day, D("1"), D("1"), D("1"), D("1"),
                None, D("1"), D("1")
            ),
            (next_day, "000001.SZ"): SignalPricePoint(
                "000001.SZ", next_day, D("2"), D("2"), D("2"), D("2"),
                D("1"), D("2"), D("1")
            ),
        }
        with self.assertRaisesRegex(
            TechnicalFormalDataError, "causal scale mismatch"
        ):
            _validate_signal_execution_factor_alignment(
                signal_by_key=impossible,
                execution_by_key=executions,
            )

    def test_all_date_inputs_reject_over_split_metadata_before_any_row_read(self) -> None:
        dataset_ids = (
            "trading_calendar",
            "signal_prices",
            "execution_prices",
            "execution_statuses",
            "corporate_actions",
        )
        for split, split_start, split_end in (
            ("development", date(2018, 1, 1), date(2022, 12, 31)),
            ("validation", date(2023, 1, 1), date(2023, 12, 31)),
        ):
            for invalid_dataset_id in dataset_ids:
                with self.subTest(split=split, dataset_id=invalid_dataset_id):
                    loader_rows = _CountingSentinel()
                    loader = object.__new__(PITUniverseLoader)
                    object.__setattr__(loader, "coverage_start", split_start)
                    object.__setattr__(loader, "coverage_end", split_end)
                    object.__setattr__(loader, "snapshots", loader_rows)
                    row_sentinels = {
                        dataset_id: _CountingSentinel()
                        for dataset_id in dataset_ids
                    }
                    partitions = {
                        dataset_id: TechnicalInputPartition(
                            dataset_id,
                            split_start,
                            (
                                split_end + timedelta(days=1)
                                if dataset_id == invalid_dataset_id
                                else split_end
                            ),
                            row_sentinels[dataset_id],
                        )
                        for dataset_id in dataset_ids
                    }
                    with self.assertRaisesRegex(
                        TechnicalFormalDataError,
                        rf"{invalid_dataset_id}.*coverage_end crosses split_end",
                    ):
                        run_technical_formal_backtest(
                            split=split,
                            trading_calendar=partitions["trading_calendar"],
                            universe_loader=loader,
                            signal_prices=partitions["signal_prices"],
                            execution_prices=partitions["execution_prices"],
                            execution_statuses=partitions["execution_statuses"],
                            benchmark_id=CSI800,
                            corporate_actions=partitions["corporate_actions"],
                        )
                    self.assertEqual(loader_rows.iteration_count, 0)
                    self.assertTrue(
                        all(
                            sentinel.iteration_count == 0
                            for sentinel in row_sentinels.values()
                        )
                    )


class TechnicalFormalFrozenFormulaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sessions = tuple(
            date(2022, 1, 1) + timedelta(days=index) for index in range(121)
        )
        cls.instrument_ids = tuple(f"{index:06d}.SZ" for index in range(1, 61))
        cls.signal_index: dict[tuple[date, str], SignalPricePoint] = {}
        cls.stock_rows: dict[str, list[dict[str, object]]] = {}
        for instrument_number, instrument_id in enumerate(cls.instrument_ids, start=1):
            rows: list[dict[str, object]] = []
            slope = D("0.00035") + D(instrument_number) * D("0.000003")
            for day_number, session in enumerate(cls.sessions):
                cycle = D((day_number * 3 + instrument_number) % 11 - 5) * D("0.00008")
                close = D("1") + slope * day_number + cycle
                point = _signal(instrument_id, session, close)
                cls.signal_index[(session, instrument_id)] = point
                rows.append(
                    {
                        "trading_date": session,
                        "open": point.open,
                        "high": point.high,
                        "low": point.low,
                        "close": point.close,
                        "trading_status": "traded",
                        "is_st": False,
                    }
                )
            cls.stock_rows[instrument_id] = rows
        cls.benchmark_rows: list[dict[str, object]] = []
        for day_number, session in enumerate(cls.sessions):
            cycle = D((day_number * 5) % 13 - 6) * D("0.00005")
            close = D("1") + D("0.00042") * day_number + cycle
            point = _signal(CSI800, session, close)
            cls.signal_index[(session, CSI800)] = point
            cls.benchmark_rows.append(
                {
                    "trading_date": session,
                    "open": point.open,
                    "high": point.high,
                    "low": point.low,
                    "close": point.close,
                }
            )
        decision_date = cls.sessions[-1]
        cls.status_index = {
            (decision_date, instrument_id): _status(instrument_id, decision_date)
            for instrument_id in cls.instrument_ids
        }

    def test_dynamic_ranker_matches_frozen_sixty_name_shadow_output(self) -> None:
        formal = rank_technical_formal_universe(
            decision_date=self.sessions[-1],
            sessions=self.sessions,
            instrument_ids=self.instrument_ids,
            signal_index=self.signal_index,
            benchmark_id=CSI800,
            status_index=self.status_index,
        )
        legacy = rank_technical_alpha_shadow(
            decision_date=self.sessions[-1],
            sessions=self.sessions,
            instrument_ids=self.instrument_ids,
            stock_rows=self.stock_rows,
            benchmark_rows=self.benchmark_rows,
        )
        self.assertEqual([item.instrument_id for item in formal], [row["instrument_id"] for row in legacy])
        for actual, expected in zip(formal, legacy, strict=True):
            self.assertEqual(actual.rank, expected["rank"])
            self.assertAlmostEqual(actual.percentile or 0, expected["percentile"] or 0, places=14)
            self.assertEqual(actual.entry_eligible, expected["entry_eligible"])
            self.assertEqual(actual.hold_eligible, expected["hold_eligible"])
            self.assertEqual(list(actual.exclusion_codes), expected["exclusion_codes"])
            self.assertAlmostEqual(
                actual.composite_score or 0,
                expected["composite_score"] or 0,
                places=12,
            )
            for factor_id in FACTOR_IDS:
                self.assertAlmostEqual(
                    actual.factors[factor_id],  # type: ignore[index]
                    expected["factors"][factor_id],  # type: ignore[index]
                    places=14,
                )
                self.assertAlmostEqual(
                    actual.z_scores[factor_id],  # type: ignore[index]
                    expected["z_scores"][factor_id],  # type: ignore[index]
                    places=12,
                )

    def test_alpha_weights_and_exposure_thresholds_are_the_frozen_contract(self) -> None:
        self.assertEqual(
            FACTOR_DIRECTIONS,
            {
                "RM20": 1.0,
                "RM60": 1.0,
                "RM120": 1.0,
                "TREND_EFF60": 1.0,
                "DOWNSIDE_VOL60": -1.0,
                "BREAKOUT60": 1.0,
            },
        )
        self.assertEqual(ENTRY_PERCENTILE, D("0.90"))
        self.assertEqual(HOLD_PERCENTILE, D("0.70"))
        self.assertIs(FROZEN_EXPOSURE_POLICY, DEFAULT_POLICY)
        self.assertEqual(FROZEN_EXPOSURE_POLICY["benchmark_trend_sessions"], 60)
        self.assertEqual(FROZEN_EXPOSURE_POLICY["breadth_trend_sessions"], 60)
        self.assertEqual(FROZEN_EXPOSURE_POLICY["realized_vol_sessions"], 20)
        self.assertEqual(
            FROZEN_EXPOSURE_POLICY["gross_exposure"],
            {"RISK_OFF": 0.0, "DEFENSIVE": 0.30, "NEUTRAL": 0.60, "RISK_ON": 1.0},
        )
        self.assertEqual(FROZEN_EXPOSURE_POLICY["risk_off"]["breadth_max"], 0.40)
        self.assertEqual(FROZEN_EXPOSURE_POLICY["defensive"]["breadth_trigger_below"], 0.50)
        self.assertEqual(FROZEN_EXPOSURE_POLICY["risk_on"]["breadth_min"], 0.60)


class TechnicalFormalPITTests(unittest.TestCase):
    def test_membership_changes_by_latest_strictly_prior_month_without_backfill(self) -> None:
        base_ids = tuple(
            [f"{600000 + index:06d}.SH" for index in range(400)]
            + [f"{index + 1:06d}.SZ" for index in range(400)]
        )
        replacement = "601999.SH"
        snapshot_dates = [date(2022, 12, 15)] + [
            date(2023, month, 15) for month in range(1, 13)
        ]
        snapshots = []
        for snapshot_date in snapshot_dates:
            members = base_ids
            if snapshot_date >= date(2023, 6, 15):
                members = base_ids[:-1] + (replacement,)
            snapshots.append(
                PITMonthlySnapshot(
                    index_code=CSI800,
                    snapshot_date=snapshot_date,
                    members=tuple(
                        PITMembershipRecord(
                            index_code=CSI800,
                            component_id=instrument_id,
                            snapshot_date=snapshot_date,
                            weight=D("0.125"),
                        )
                        for instrument_id in members
                    ),
                )
            )
        loader = PITUniverseLoader(
            snapshots=tuple(snapshots),
            coverage_start=date(2023, 1, 1),
            coverage_end=date(2023, 12, 31),
        )
        before = _members(loader, date(2023, 6, 10))
        after = _members(loader, date(2023, 7, 3))
        self.assertEqual(len(before), 800)
        self.assertEqual(len(after), 800)
        self.assertNotIn(replacement, before)
        self.assertIn(replacement, after)
        self.assertIn(base_ids[-1], before)
        self.assertNotIn(base_ids[-1], after)


class TechnicalFormalExecutionTests(unittest.TestCase):
    def test_backtest_reexports_exact_status_type_and_rejects_status_drift(self) -> None:
        from research.strategy_workspace import technical_formal_backtest as backtest

        self.assertIs(backtest.TechnicalExecutionStatus, TechnicalExecutionStatus)
        day = date(2023, 1, 3)
        canonical = _status("000001.SZ", day)
        st_status = _status("000001.SZ", day, is_st=True)
        raw = _execution(st_status)
        with self.assertRaisesRegex(TechnicalFormalDataError, "is_st"):
            _validate_execution_status_alignment(
                execution_by_key={(day, "000001.SZ"): raw},
                status_by_key={(day, "000001.SZ"): canonical},
            )

    def test_ipo_no_price_limit_is_explicit_and_aligns(self) -> None:
        day = date(2023, 1, 3)
        status = _status("688001.SH", day, price_limit_applicable=False)
        raw = _execution(status, price=D("52.34"))
        self.assertIsNone(raw.limit_up_price)
        self.assertIsNone(raw.limit_down_price)
        _validate_execution_status_alignment(
            execution_by_key={(day, raw.instrument_id): raw},
            status_by_key={(day, status.instrument_id): status},
        )

    def test_quantity_cost_and_cash_use_raw_open_not_adjusted_factor(self) -> None:
        day = date(2023, 1, 3)
        instrument_id = "000001.SZ"
        status = _status(instrument_id, day)
        raw = _execution(status, price=D("10"), adjustment_factor=D("25"))
        positions: dict[str, list[_Lot]] = {}
        fills = []
        events: list[TechnicalExecutionEvent] = []
        last_factors: dict[str, Decimal] = {}
        cash, _ = _execute_decision(
            decision=_decision(day, (instrument_id,), {instrument_id: D("0.40")}),
            trading_date=day,
            positions=positions,
            cash=D("10000"),
            execution_by_key={(day, instrument_id): raw},
            status_by_key={(day, instrument_id): status},
            scenario=BASE_COST,
            calendar_index={day: 0},
            fills=fills,
            events=events,
            closed_lots=[],
            instrument_cash_flows=defaultdict(lambda: D("0")),
            last_factors=last_factors,
            valuation_closes={},
        )
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].reference_open, D("10"))
        self.assertEqual(fills[0].quantity, 400)
        self.assertEqual(sum(lot.quantity for lot in positions[instrument_id]), 400)
        self.assertEqual(last_factors[instrument_id], D("25"))
        self.assertGreater(cash, D("0"))
        self.assertGreater(
            _cost(side="BUY", quantity=400, reference_open=D("10"), scenario=STRESS_COST)["total_cost"],
            _cost(side="BUY", quantity=400, reference_open=D("10"), scenario=BASE_COST)["total_cost"],
        )

    def test_t_plus_one_limit_down_and_suspension_retain_residual(self) -> None:
        instrument_id = "000001.SZ"
        acquisition_day = date(2023, 1, 3)
        normal = _status(instrument_id, acquisition_day)
        positions = {instrument_id: [_Lot(100, acquisition_day, D("1000"))]}
        events: list[TechnicalExecutionEvent] = []
        _execute_decision(
            decision=_decision(acquisition_day),
            trading_date=acquisition_day,
            positions=positions,
            cash=D("0"),
            execution_by_key={(acquisition_day, instrument_id): _execution(normal)},
            status_by_key={(acquisition_day, instrument_id): normal},
            scenario=BASE_COST,
            calendar_index={acquisition_day: 0},
            fills=[],
            events=events,
            closed_lots=[],
            instrument_cash_flows=defaultdict(lambda: D("0")),
            last_factors={instrument_id: D("1")},
            valuation_closes={instrument_id: D("10")},
        )
        self.assertEqual(sum(lot.quantity for lot in positions[instrument_id]), 100)
        self.assertIn("sell_blocked_t_plus_one", [event.code for event in events])

        limit_day = date(2023, 1, 4)
        limit_down = _status(
            instrument_id,
            limit_day,
            price_limit_applicable=True,
            limit_down_locked=True,
        )
        events = []
        _execute_decision(
            decision=_decision(limit_day),
            trading_date=limit_day,
            positions=positions,
            cash=D("0"),
            execution_by_key={(limit_day, instrument_id): _execution(limit_down)},
            status_by_key={(limit_day, instrument_id): limit_down},
            scenario=BASE_COST,
            calendar_index={acquisition_day: 0, limit_day: 1},
            fills=[],
            events=events,
            closed_lots=[],
            instrument_cash_flows=defaultdict(lambda: D("0")),
            last_factors={instrument_id: D("1")},
            valuation_closes={instrument_id: D("10")},
        )
        self.assertEqual(sum(lot.quantity for lot in positions[instrument_id]), 100)
        self.assertIn("sell_blocked_limit_down", [event.code for event in events])

        suspended_day = date(2023, 1, 5)
        suspended = _status(instrument_id, suspended_day, suspended=True)
        events = []
        _execute_decision(
            decision=_decision(suspended_day),
            trading_date=suspended_day,
            positions=positions,
            cash=D("0"),
            execution_by_key={},
            status_by_key={(suspended_day, instrument_id): suspended},
            scenario=BASE_COST,
            calendar_index={acquisition_day: 0, limit_day: 1, suspended_day: 2},
            fills=[],
            events=events,
            closed_lots=[],
            instrument_cash_flows=defaultdict(lambda: D("0")),
            last_factors={instrument_id: D("1")},
            valuation_closes={instrument_id: D("9")},
        )
        self.assertEqual(sum(lot.quantity for lot in positions[instrument_id]), 100)
        self.assertIn("sell_blocked_suspended", [event.code for event in events])

    def test_suspended_no_bar_uses_raw_close_carry_and_other_missing_bar_fails(self) -> None:
        instrument_id = "000001.SZ"
        day = date(2023, 1, 5)
        positions = {instrument_id: [_Lot(100, date(2023, 1, 3), D("1000"))]}
        events: list[TechnicalExecutionEvent] = []
        suspended = _status(instrument_id, day, suspended=True)
        values = _mark_positions_at_raw_close(
            trading_date=day,
            positions=positions,
            execution_by_key={},
            status_by_key={(day, instrument_id): suspended},
            valuation_closes={instrument_id: D("9.87")},
            events=events,
        )
        self.assertEqual(values[instrument_id], D("987.00"))
        self.assertEqual(events[0].code, "valuation_suspended_carry_forward")
        normal = _status(instrument_id, day)
        with self.assertRaisesRegex(TechnicalFormalDataError, "non-suspended"):
            _mark_positions_at_raw_close(
                trading_date=day,
                positions=positions,
                execution_by_key={},
                status_by_key={(day, instrument_id): normal},
                valuation_closes={instrument_id: D("9.87")},
                events=[],
            )

        next_day = day + timedelta(days=1)
        delisted_status = _status(
            instrument_id,
            next_day,
            listed=False,
            delisted=True,
        )
        with self.assertRaisesRegex(
            TechnicalFormalDataError, "controlled terminal valuation"
        ):
            _mark_positions_at_raw_close(
                trading_date=next_day,
                positions={instrument_id: [_Lot(100, day, D("1000"))]},
                execution_by_key={},
                status_by_key={(next_day, instrument_id): delisted_status},
                valuation_closes={instrument_id: D("10")},
                events=[],
            )

    def test_st_limit_up_and_listing_state_block_new_buys(self) -> None:
        day = date(2023, 1, 3)
        variants = (
            (_status("000001.SZ", day, is_st=True), "buy_blocked_st"),
            (
                _status(
                    "000002.SZ",
                    day,
                    price_limit_applicable=True,
                    limit_up_locked=True,
                ),
                "buy_blocked_limit_up",
            ),
            (
                _status("000003.SZ", day, listed=False),
                "buy_blocked_not_listed",
            ),
        )
        for status, expected_code in variants:
            with self.subTest(expected_code=expected_code):
                events: list[TechnicalExecutionEvent] = []
                _execute_decision(
                    decision=_decision(
                        day,
                        (status.instrument_id,),
                        {status.instrument_id: D("0.40")},
                    ),
                    trading_date=day,
                    positions={},
                    cash=D("10000"),
                    execution_by_key={(day, status.instrument_id): _execution(status)},
                    status_by_key={(day, status.instrument_id): status},
                    scenario=BASE_COST,
                    calendar_index={day: 0},
                    fills=[],
                    events=events,
                    closed_lots=[],
                    instrument_cash_flows=defaultdict(lambda: D("0")),
                    last_factors={},
                    valuation_closes={},
                )
                self.assertIn(expected_code, [event.code for event in events])


class TechnicalFormalCorporateActionTests(unittest.TestCase):
    def test_factor_change_while_held_fails_without_rights_decomposition(self) -> None:
        instrument_id = "000001.SZ"
        day = date(2023, 6, 1)
        status = _status(instrument_id, day)
        with self.assertRaisesRegex(CorporateActionDataGap, "without matching"):
            _apply_corporate_actions(
                trading_date=day,
                positions={instrument_id: [_Lot(100, date(2023, 1, 3), D("1000"))]},
                last_factors={instrument_id: D("1")},
                execution_by_key={
                    (day, instrument_id): _execution(
                        status, adjustment_factor=D("2")
                    )
                },
                status_by_key={(day, instrument_id): status},
                actions={},
                cash=D("100"),
                instrument_cash_flows=defaultdict(lambda: D("0")),
                events=[],
                valuation_closes={instrument_id: D("10")},
            )

    def test_explicit_rights_keep_raw_nav_exact_on_suspended_no_bar_day(self) -> None:
        instrument_id = "000001.SZ"
        day = date(2023, 6, 1)
        status = _status(instrument_id, day, suspended=True)
        action = CorporateActionEntitlement(
            instrument_id=instrument_id,
            effective_date=day,
            previous_adjustment_factor=D("1"),
            new_adjustment_factor=D("2"),
            share_multiplier=D("2"),
            cash_per_old_share=D("1"),
            available_at=datetime(2023, 6, 1, 9, 0, tzinfo=CN_TZ),
            source_sha256="0" * 64,
        )
        positions = {instrument_id: [_Lot(100, date(2023, 1, 3), D("1000"))]}
        last_factors = {instrument_id: D("1")}
        valuation_closes = {instrument_id: D("10")}
        cash = _apply_corporate_actions(
            trading_date=day,
            positions=positions,
            last_factors=last_factors,
            execution_by_key={},
            status_by_key={(day, instrument_id): status},
            actions={(day, instrument_id): action},
            cash=D("100"),
            instrument_cash_flows=defaultdict(lambda: D("0")),
            events=[],
            valuation_closes=valuation_closes,
        )
        self.assertEqual(cash, D("200.00"))
        self.assertEqual(positions[instrument_id][0].quantity, 200)
        self.assertEqual(positions[instrument_id][0].remaining_cost_basis, D("900"))
        self.assertEqual(last_factors[instrument_id], D("2"))
        self.assertEqual(valuation_closes[instrument_id], D("4.5"))
        self.assertEqual(
            cash + valuation_closes[instrument_id] * 200,
            D("1100.00"),
        )


class TechnicalFormalMetricTests(unittest.TestCase):
    def test_requested_metrics_reconcile_to_raw_nav(self) -> None:
        instrument_id = "000001.SZ"
        nav = (
            TechnicalNavPoint(
                date(2023, 1, 3),
                D("100"),
                D("0"),
                D("100"),
                D("0"),
                D("0"),
                D("100"),
                "RISK_OFF",
                D("0"),
                D("0"),
            ),
            TechnicalNavPoint(
                date(2023, 6, 30),
                D("0"),
                D("110"),
                D("110"),
                D("10"),
                D("0"),
                D("105"),
                "RISK_ON",
                D("1"),
                D("1"),
            ),
        )
        metrics = _performance(
            initial_cash=D("100"),
            initial_benchmark_close=D("100"),
            nav=nav,
            fills=(),
            closed_lots=(_ClosedLot(100, D("10"), 5),),
            exposure_counts=Counter({"RISK_OFF": 1, "RISK_ON": 1}),
            instrument_cash_flows={instrument_id: D("-100")},
            ending_market_values={instrument_id: D("110")},
            total_reference_notional=D("200"),
        )
        self.assertEqual(metrics.net_return, D("0.10000000"))
        self.assertEqual(metrics.benchmark_return, D("0.05000000"))
        self.assertEqual(metrics.net_active_return, D("0.05000000"))
        self.assertEqual(metrics.max_drawdown, D("0E-8"))
        self.assertEqual(metrics.total_cost, D("0.00"))
        self.assertEqual(metrics.cost_to_gross_profit, D("0E-8"))
        self.assertEqual(metrics.cash_day_fraction, D("0.50000000"))
        self.assertEqual(metrics.positive_half_year_count, 1)
        self.assertEqual(metrics.trade_count, 1)
        self.assertEqual(metrics.win_rate, D("1.00000000"))
        self.assertEqual(metrics.average_holding_period, D("5.00000000"))
        self.assertEqual(metrics.per_stock_pnl_contribution, {instrument_id: D("10.00")})
        self.assertEqual(metrics.largest_stock_pnl_share, D("1.00000000"))
        self.assertEqual(metrics.largest_10_days_pnl_share, D("1.00000000"))
        self.assertEqual(
            metrics.exposure_state_distribution["RISK_OFF"],
            D("0.50000000"),
        )


if __name__ == "__main__":
    unittest.main()
