from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

from operations.run_technical_shadow_mvp import (
    CHINA_TZ,
    CapturedData,
    _load_config,
)
from operations.run_technical_shadow_retrospective import (
    EXECUTION_DATE,
    EXPECTED_ARTIFACTS,
    RETROSPECTIVE_SAFETY,
    STRATEGY_DATE,
    TechnicalShadowRetrospectiveError,
    _publish_retrospective_run,
    build_retrospective_decision,
    build_retrospective_execution,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIG = _load_config(
    ROOT / "configs" / "a_share_technical_shadow_mvp.v1.json"
)


def _row(instrument_id: str, day: date, close: float) -> dict[str, object]:
    return {
        "instrument_id": instrument_id,
        "trading_date": day.isoformat(),
        "open": close,
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
        "preclose": close,
        "volume": 1_000_000.0,
        "amount": close * 1_000_000.0,
        "adjustment": "none",
        "trading_status": "traded",
        "is_st": False,
        "available_at": f"{day.isoformat()}T15:30:00+08:00",
    }


def _captured() -> CapturedData:
    sessions = tuple(
        STRATEGY_DATE - timedelta(days=120 - index)
        for index in range(121)
    )
    benchmark_id = str(CONFIG["data"]["benchmark_id"])
    benchmark_rows = tuple(
        _row(benchmark_id, day, 300.0 - index * 0.2)
        for index, day in enumerate(sessions)
    )
    stock_rows = {
        instrument_id: tuple(
            _row(instrument_id, day, 8.0 + offset * 0.08 + index * 0.002)
            for index, day in enumerate(sessions)
        )
        for offset, instrument_id in enumerate(
            CONFIG["universe"]["instrument_ids"]
        )
    }
    return CapturedData(
        provider_id="fixture",
        provider_kind="test_fixture",
        adapter_version="offline-retrospective-fixture-v1",
        synthetic=True,
        captured_at="2026-08-27T10:00:00+08:00",
        sessions=sessions,
        stock_rows=stock_rows,
        benchmark_rows=benchmark_rows,
        receipts={},
    )


def _source_state() -> dict[str, object]:
    return {
        "state_date": "2026-08-25",
        "previous_trading_date": "2026-08-24",
        "previous_record_sha256": "a" * 64,
        "record_sha256": "b" * 64,
        "cash": "10000.00",
        "positions": {},
        "position_lots": [],
        "sellable_quantities": {},
        "nav": "10000.00",
        "peak_nav": "10000.00",
        "drawdown": 0.0,
        "exposure_state": "RISK_OFF",
        "pending_state": None,
        "hysteresis_count": 0,
    }


def _source_plan() -> dict[str, object]:
    return {
        "plan_status": "NO_ACTION_CASH",
        "decision_date": "2026-08-25",
        "execution_date": STRATEGY_DATE.isoformat(),
        "target_positions": {},
        "selected_instruments": [],
        "actions": [],
    }


def _build(captured: CapturedData):
    return build_retrospective_decision(
        run_id="test-run",
        generated_at=datetime(2026, 8, 27, 11, 0, tzinfo=CHINA_TZ),
        config=CONFIG,
        captured=captured,
        source_state=_source_state(),
        source_plan=_source_plan(),
        source_manifest_sha256="c" * 64,
        allow_test_provider=True,
    )


class TechnicalShadowRetrospectiveTests(unittest.TestCase):
    def test_fixture_is_rejected_by_real_run_default(self) -> None:
        with self.assertRaisesRegex(
            TechnicalShadowRetrospectiveError, "real_baostock_provider_required"
        ):
            build_retrospective_decision(
                run_id="test-run",
                generated_at=datetime(2026, 8, 27, 11, 0, tzinfo=CHINA_TZ),
                config=CONFIG,
                captured=_captured(),
                source_state=_source_state(),
                source_plan=_source_plan(),
                source_manifest_sha256="c" * 64,
            )

    def test_d_plus_one_data_is_rejected_before_ranking(self) -> None:
        captured = _captured()
        first = str(CONFIG["universe"]["instrument_ids"][0])
        changed_rows = dict(captured.stock_rows)
        changed_rows[first] = tuple(changed_rows[first]) + (
            _row(first, EXECUTION_DATE, 99.0),
        )
        with self.assertRaisesRegex(
            TechnicalShadowRetrospectiveError,
            "d_plus_one_stock_data_in_decision_capture",
        ):
            _build(replace(captured, stock_rows=changed_rows))

    def test_missing_stock_session_produces_exclusion_not_key_error(self) -> None:
        captured = _captured()
        first = str(CONFIG["universe"]["instrument_ids"][0])
        changed_rows = dict(captured.stock_rows)
        changed_rows[first] = tuple(changed_rows[first][1:])
        _receipt, ranking, _exposure, _decision, _context = _build(
            replace(captured, stock_rows=changed_rows)
        )
        self.assertEqual(len(ranking["rows"]), 60)
        excluded = next(
            row for row in ranking["rows"] if row["instrument_id"] == first
        )
        self.assertFalse(excluded["eligibility"])
        self.assertIn("missing_common_session", excluded["exclusion_codes"])

    def test_no_action_execution_never_reads_d_plus_one(self) -> None:
        _receipt, _ranking, _exposure, decision, context = _build(_captured())

        def forbidden_capture(**_kwargs: object):
            raise AssertionError("D+1 capture must not run for CASH")

        execution = build_retrospective_execution(
            run_id="test-run",
            generated_at=datetime(2026, 8, 27, 11, 0, tzinfo=CHINA_TZ),
            config=CONFIG,
            decision=decision,
            context=context,
            execution_capture=forbidden_capture,
        )
        self.assertEqual(execution["execution_result"], "NO_ACTION")
        self.assertEqual(execution["simulated_fills"], [])
        self.assertEqual(execution["close_valuation_status"], "PENDING")
        for key, expected in RETROSPECTIVE_SAFETY.items():
            self.assertIs(execution[key], expected)

    def test_publisher_is_create_only_with_exact_artifact_set(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as temporary:
            root = Path(temporary)
            payloads = {
                name: (
                    "report\n" if name.endswith(".md")
                    else {**RETROSPECTIVE_SAFETY, "safety": RETROSPECTIVE_SAFETY}
                )
                for name in EXPECTED_ARTIFACTS
            }
            run_root, _manifest_sha = _publish_retrospective_run(
                output_root=root,
                run_id="fixed-run",
                generated_at=datetime(2026, 8, 27, 11, 0, tzinfo=CHINA_TZ),
                config=CONFIG,
                source_state=_source_state(),
                formal_snapshot_sha256="d" * 64,
                payloads=payloads,
            )
            self.assertEqual(
                {path.name for path in run_root.iterdir()},
                EXPECTED_ARTIFACTS | {"manifest.json"},
            )
            with self.assertRaisesRegex(
                TechnicalShadowRetrospectiveError,
                "create_only_run_directory_exists",
            ):
                _publish_retrospective_run(
                    output_root=root,
                    run_id="fixed-run",
                    generated_at=datetime(
                        2026, 8, 27, 11, 0, tzinfo=CHINA_TZ
                    ),
                    config=CONFIG,
                    source_state=_source_state(),
                    formal_snapshot_sha256="d" * 64,
                    payloads=payloads,
                )


if __name__ == "__main__":
    unittest.main()
