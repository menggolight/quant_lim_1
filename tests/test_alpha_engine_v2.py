from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
import unittest

from research.strategy_workspace.alpha_engine_v2 import (
    FAST_FACTOR_IDS,
    FINANCIAL_FEATURE_IDS,
    NON_FINANCIAL_FEATURE_IDS,
    AlphaEngineError,
    AlphaRunStatus,
    ControlledPitInstrumentV2,
    ControlledPitSnapshotV2,
    ControlledPriceBarV2,
    FrozenAlphaModelV2,
    FrozenLinearSubmodelV2,
    run_alpha_engine,
)
from research.strategy_workspace.quality_growth import QuarterlyFundamental


TZ = timezone(timedelta(hours=8))
SHA = "a" * 64


def _sessions(end: date, count: int = 121) -> tuple[date, ...]:
    values: list[date] = []
    cursor = end
    while len(values) < count:
        if cursor.weekday() < 5:
            values.append(cursor)
        cursor -= timedelta(days=1)
    return tuple(reversed(values))


def _bars(
    instrument_id: str,
    sessions: tuple[date, ...],
    *,
    base: float,
    slope: float,
) -> tuple[ControlledPriceBarV2, ...]:
    result = []
    for index, session in enumerate(sessions):
        close = base + slope * index + 0.03 * (index % 5)
        result.append(
            ControlledPriceBarV2(
                instrument_id=instrument_id,
                session_date=session,
                close=close,
                high=close * 1.01,
                available_at=datetime.combine(session, time(15, 5), tzinfo=TZ),
                source_record_id=f"{instrument_id}:{session.isoformat()}",
                source_record_sha256=SHA,
            )
        )
    return tuple(result)


def _quarter_ends() -> tuple[date, ...]:
    return (
        date(2023, 9, 30),
        date(2023, 12, 31),
        date(2024, 3, 31),
        date(2024, 6, 30),
        date(2024, 9, 30),
        date(2024, 12, 31),
        date(2025, 3, 31),
        date(2025, 6, 30),
        date(2025, 9, 30),
        date(2025, 12, 31),
        date(2026, 3, 31),
        date(2026, 6, 30),
    )


def _fundamentals(
    instrument_id: str,
    *,
    scale: float = 1.0,
    missing_revenue: bool = False,
) -> tuple[QuarterlyFundamental, ...]:
    rows = []
    for index, period_end in enumerate(_quarter_ends()):
        revenue = None if missing_revenue and index == 4 else scale * (1000.0 + 55.0 * index)
        rows.append(
            QuarterlyFundamental(
                instrument_id=instrument_id,
                period_end=period_end,
                first_disclosed_at=datetime.combine(
                    period_end + timedelta(days=30), time(9), tzinfo=TZ
                ),
                source_record_id=f"{instrument_id}:Q:{period_end.isoformat()}",
                source_record_sha256=SHA,
                revision_sequence=1,
                roe=0.10 + 0.002 * index + 0.001 * (index % 2),
                net_profit=scale * (100.0 + 8.0 * index + 3.0 * (index % 3)),
                operating_cash_flow=scale * (130.0 + index),
                operating_profit=scale * (100.0 + index),
                gross_profit=scale * (200.0 + 2.0 * index),
                total_assets=scale * (2000.0 + 20.0 * index),
                total_liabilities=scale * (800.0 + 10.0 * index),
                revenue=revenue,
            )
        )
    return tuple(rows)


def _instrument_input(
    instrument_id: str,
    sessions: tuple[date, ...],
    *,
    scale: float = 1.0,
    missing_revenue: bool = False,
) -> ControlledPitInstrumentV2:
    decision_date = sessions[-1]
    return ControlledPitInstrumentV2(
        instrument_id=instrument_id,
        industry="CSI2021_L1/工业",
        industry_is_financial=False,
        constituent_available_at=datetime.combine(decision_date, time(9), tzinfo=TZ),
        industry_available_at=datetime.combine(decision_date, time(9), tzinfo=TZ),
        fundamentals=_fundamentals(
            instrument_id, scale=scale, missing_revenue=missing_revenue
        ),
        price_bars=_bars(instrument_id, sessions, base=10.0 * scale, slope=0.03 * scale),
    )


def _submodel(submodel_id: str, *, multiplier: float = 1.0) -> FrozenLinearSubmodelV2:
    feature_ids = FINANCIAL_FEATURE_IDS if submodel_id == "financial" else NON_FINANCIAL_FEATURE_IDS
    return FrozenLinearSubmodelV2(
        submodel_id=submodel_id,
        feature_ids=feature_ids,
        intercept=0.0,
        coefficients=tuple(multiplier * (index + 1) / 1000.0 for index in range(len(feature_ids))),
        centers=tuple(0.0 for _ in feature_ids),
        scales=tuple(1.0 for _ in feature_ids),
    )


def _model(*, multiplier: float = 1.0) -> FrozenAlphaModelV2:
    return FrozenAlphaModelV2(
        model_id="adaptive-alpha-v2",
        model_version="test-v1",
        training_window_start=date(2018, 1, 1),
        training_window_end=date(2022, 12, 31),
        training_data_cutoff_at=datetime(2023, 2, 1, tzinfo=TZ),
        trained_at=datetime(2023, 2, 2, tzinfo=TZ),
        frozen_at=datetime(2023, 2, 3, tzinfo=TZ),
        training_dataset_sha256="1" * 64,
        training_code_sha256="2" * 64,
        preprocessing_policy_sha256="3" * 64,
        model_config_sha256="4" * 64,
        financial_submodel=_submodel("financial", multiplier=multiplier),
        non_financial_submodel=_submodel("non_financial", multiplier=multiplier),
    )


def _snapshot(
    *,
    instruments: tuple[ControlledPitInstrumentV2, ...] | None = None,
    member_ids: tuple[str, ...] = ("000001.SZ", "600000.SH"),
) -> ControlledPitSnapshotV2:
    sessions = _sessions(date(2026, 8, 18))
    if instruments is None:
        instruments = (
            _instrument_input("000001.SZ", sessions, scale=1.0),
            _instrument_input("600000.SH", sessions, scale=1.3),
        )
    return ControlledPitSnapshotV2(
        decision_at=datetime(2026, 8, 18, 16, 0, tzinfo=TZ),
        universe_as_of=date(2026, 8, 18),
        universe_available_at=datetime(2026, 8, 18, 9, 0, tzinfo=TZ),
        universe_version="CSI800-PIT-20260818",
        member_ids=member_ids,
        instruments=instruments,
        trading_sessions=sessions,
        benchmark_instrument_id="H00906.CSI",
        benchmark_price_bars=_bars("H00906.CSI", sessions, base=5000.0, slope=2.0),
        trading_calendar_receipt_sha256="5" * 64,
        universe_receipt_sha256="6" * 64,
        financial_data_receipt_sha256="7" * 64,
        industry_data_receipt_sha256="8" * 64,
        price_data_receipt_sha256="9" * 64,
    )


class AlphaEngineV2Tests(unittest.TestCase):
    def test_repeated_frozen_inputs_are_byte_deterministic(self) -> None:
        snapshot = _snapshot()
        model = _model()
        first = run_alpha_engine(snapshot, model)
        second = run_alpha_engine(snapshot, model)
        self.assertEqual(first.status, AlphaRunStatus.OK)
        self.assertEqual(len(first.rows), 2)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.ranking_sha256, second.ranking_sha256)
        self.assertEqual(first.rows[0].percentile, 1.0)
        self.assertEqual(first.rows[1].percentile, 0.0)
        json.dumps(first.to_dict(), ensure_ascii=False)
        json.dumps(snapshot.to_dict(), ensure_ascii=False)
        json.dumps(model.to_dict(), ensure_ascii=False)

    def test_model_artifact_self_hash_changes_with_coefficients(self) -> None:
        self.assertNotEqual(_model(multiplier=1.0).model_sha256, _model(multiplier=2.0).model_sha256)

    def test_tampered_model_payload_fails_complete_universe_closed(self) -> None:
        model = _model()
        object.__setattr__(model, "model_version", "tampered")
        result = run_alpha_engine(_snapshot(), model)
        self.assertEqual(result.status, AlphaRunStatus.DATA_FAIL_CLOSED)
        self.assertTrue(all("MODEL_HASH_MISMATCH" in row.exclusion_codes for row in result.rows))

    def test_future_price_available_at_fails_complete_universe_closed(self) -> None:
        snapshot = _snapshot()
        first_input = snapshot.instruments[0]
        bars = list(first_input.price_bars)
        bars[-1] = replace(
            bars[-1], available_at=snapshot.decision_at + timedelta(seconds=1)
        )
        attacked_input = replace(first_input, price_bars=tuple(bars))
        attacked = replace(
            snapshot,
            instruments=(attacked_input, snapshot.instruments[1]),
        )
        result = run_alpha_engine(attacked, _model())
        self.assertEqual(result.status, AlphaRunStatus.DATA_FAIL_CLOSED)
        self.assertTrue(all(not item.eligibility for item in result.rows))
        self.assertTrue(
            all("FUTURE_PRICE_AVAILABLE_AT" in item.exclusion_codes for item in result.rows)
        )

    def test_future_fundamental_record_fails_complete_universe_closed(self) -> None:
        snapshot = _snapshot()
        first_input = snapshot.instruments[0]
        future = replace(
            first_input.fundamentals[-1],
            first_disclosed_at=snapshot.decision_at + timedelta(days=1),
        )
        attacked_input = replace(
            first_input,
            fundamentals=first_input.fundamentals[:-1] + (future,),
        )
        attacked = replace(snapshot, instruments=(attacked_input, snapshot.instruments[1]))
        result = run_alpha_engine(attacked, _model())
        self.assertEqual(result.status, AlphaRunStatus.DATA_FAIL_CLOSED)
        self.assertIn("FUTURE_FUNDAMENTAL_AVAILABLE_AT", result.rows[0].exclusion_codes)

    def test_plus14_decision_uses_the_cst_strategy_date(self) -> None:
        snapshot = _snapshot()
        plus_14 = timezone(timedelta(hours=14))
        decision_at = datetime(2026, 8, 19, 5, 30, tzinfo=plus_14)

        result = run_alpha_engine(
            replace(snapshot, decision_at=decision_at),
            _model(),
        )

        self.assertEqual(decision_at.astimezone(TZ).date(), date(2026, 8, 18))
        self.assertEqual(result.status, AlphaRunStatus.OK)
        self.assertTrue(all(item.eligibility for item in result.rows))

    def test_plus14_raw_date_cannot_admit_a_future_cst_session(self) -> None:
        plus_14 = timezone(timedelta(hours=14))
        decision_at = datetime(2026, 8, 19, 5, 30, tzinfo=plus_14)
        claimed_available_at = decision_at - timedelta(minutes=1)
        future_sessions = _sessions(date(2026, 8, 19))

        def retimed_input(instrument_id: str, scale: float) -> ControlledPitInstrumentV2:
            value = _instrument_input(instrument_id, future_sessions, scale=scale)
            return replace(
                value,
                constituent_available_at=claimed_available_at,
                industry_available_at=claimed_available_at,
                price_bars=tuple(
                    replace(bar, available_at=claimed_available_at)
                    for bar in value.price_bars
                ),
            )

        base = _snapshot()
        attacked = replace(
            base,
            decision_at=decision_at,
            universe_as_of=date(2026, 8, 19),
            universe_available_at=claimed_available_at,
            trading_sessions=future_sessions,
            instruments=(
                retimed_input("000001.SZ", 1.0),
                retimed_input("600000.SH", 1.3),
            ),
            benchmark_price_bars=tuple(
                replace(bar, available_at=claimed_available_at)
                for bar in _bars(
                    "H00906.CSI", future_sessions, base=5000.0, slope=2.0
                )
            ),
        )

        result = run_alpha_engine(attacked, _model())

        self.assertEqual(decision_at.astimezone(TZ).date(), date(2026, 8, 18))
        self.assertEqual(result.status, AlphaRunStatus.DATA_FAIL_CLOSED)
        self.assertTrue(
            all("FUTURE_TRADING_SESSION" in item.exclusion_codes for item in result.rows)
        )
        self.assertTrue(
            all("FUTURE_PRICE_SESSION" in item.exclusion_codes for item in result.rows)
        )

    def test_missing_factor_excludes_instrument_without_zero_fill(self) -> None:
        sessions = _sessions(date(2026, 8, 18))
        missing = _instrument_input(
            "000001.SZ", sessions, scale=1.0, missing_revenue=True
        )
        valid = _instrument_input("600000.SH", sessions, scale=1.2)
        result = run_alpha_engine(_snapshot(instruments=(missing, valid)), _model())
        self.assertEqual(result.status, AlphaRunStatus.OK)
        by_id = {item.instrument_id: item for item in result.rows}
        excluded = by_id["000001.SZ"]
        self.assertFalse(excluded.eligibility)
        self.assertIsNone(excluded.predicted_return)
        self.assertTrue(
            any(code.startswith("MISSING_FACTOR:QG_REVENUE_GROWTH_STABILITY:") for code in excluded.exclusion_codes)
        )
        self.assertTrue(by_id["600000.SH"].eligibility)

    def test_missing_member_input_is_retained_and_no_eligible_means_cash(self) -> None:
        snapshot = _snapshot(instruments=())
        result = run_alpha_engine(snapshot, _model())
        self.assertEqual(result.status, AlphaRunStatus.NO_ALPHA_CASH)
        self.assertEqual(tuple(item.instrument_id for item in result.rows), snapshot.member_ids)
        self.assertTrue(
            all(item.exclusion_codes == ("MISSING_INSTRUMENT_INPUT",) for item in result.rows)
        )

    def test_only_typed_snapshot_and_exact_feature_family_are_accepted(self) -> None:
        with self.assertRaises(AlphaEngineError):
            run_alpha_engine({}, _model())  # type: ignore[arg-type]
        with self.assertRaises(AlphaEngineError):
            FrozenLinearSubmodelV2(
                submodel_id="financial",
                feature_ids=FAST_FACTOR_IDS,
                intercept=0.0,
                coefficients=(0.0,) * 6,
                centers=(0.0,) * 6,
                scales=(1.0,) * 6,
            )

    def test_new_schemas_are_valid_json(self) -> None:
        root = Path(__file__).resolve().parents[1] / "schemas"
        for name in (
            "controlled_pit_decision_snapshot.v1.json",
            "frozen_alpha_model.v1.json",
            "alpha_ranking.v2.json",
        ):
            payload = json.loads((root / name).read_text(encoding="utf-8"))
            self.assertEqual(payload["$schema"], "https://json-schema.org/draft/2020-12/schema")


if __name__ == "__main__":
    unittest.main()
