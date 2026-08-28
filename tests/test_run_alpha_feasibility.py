from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from operations import run_alpha_feasibility as cli
from research.strategy_workspace import alpha_feasibility as engine


COMMIT_SHA = "33ac5f0e3c484a514288136ac5317830902e2105"
GENERATED_AT = "2026-08-28T12:00:00+08:00"


def _counts() -> dict[str, int]:
    return {
        "trade_cal": 1,
        "index_weight": 73,
        "daily": 1,
        "adj_factor": 1,
        "index_daily": 1,
        "suspend_d": 1,
        "stock_basic": 3,
    }


def _backfill(*, stage: str) -> dict[str, object]:
    blocked = stage != cli.READY_STAGE
    return {
        "schema_version": "tushare-alpha-feasibility-backfill-result.v1",
        "experiment_id": "a-share-technical-alpha-feasibility-tushare-p1-v1",
        "stage_status": stage,
        "terminal_status": "BLOCKED_DATA" if blocked else None,
        "generated_at": GENERATED_AT,
        "actual_tushare_request_count_by_endpoint": _counts(),
        "coverage_start": "2017-07-01",
        "coverage_end": "2023-12-31",
        "pit_months_expected": 73,
        "pit_months_observed": 72 if blocked else 73,
        "union_instrument_count": 0 if blocked else 1,
        "collection_plan_sha256": "1" * 64,
        "pit_membership_manifest_sha256": "2" * 64,
        "history_manifest_sha256": None if blocked else "3" * 64,
        "daily_coverage_status": "BLOCKED_DATA" if blocked else "COMPLETE",
        "adj_factor_coverage_status": "BLOCKED_DATA" if blocked else "COMPLETE",
        "suspension_coverage_status": "BLOCKED_DATA" if blocked else "COMPLETE",
        "benchmark_coverage_status": "BLOCKED_DATA" if blocked else "COMPLETE",
        "remaining_blockers": ["pit_membership_incomplete"] if blocked else [],
        "locked_test_status": dict(cli.LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
        "execution_realism": "INCOMPLETE",
        "trade_eligibility": False,
    }


def _loaded_inputs() -> dict[str, object]:
    return {
        "coverage_start": "2017-07-01",
        "coverage_end": "2023-12-31",
        "trading_dates": ["2017-07-03"],
        "pit_snapshots": [
            {"snapshot_date": "2017-12-29", "members": ["000001.SZ"]}
        ],
        "pit_coverage_report": {"evidence": "coverage"},
        "pit_manifest": {"evidence": "manifest"},
        "signal_bars": [
            {
                "trading_date": "2017-07-03",
                "instrument_id": "000001.SZ",
                "raw_open": "10",
                "adj_factor": "10.8",
                "open": "108",
                "close": "110",
                "high": "111",
            }
        ],
        "benchmark_bars": [
            {"trading_date": "2017-07-03", "close": "100", "high": "101"}
        ],
        "suspensions": [],
        "locked_test_status": dict(cli.LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
        "execution_realism": "INCOMPLETE",
        "trade_eligibility": False,
    }


def _metrics(period: str, *, active: str) -> engine.AlphaFeasibilityMetrics:
    return engine.AlphaFeasibilityMetrics(
        net_return=Decimal("0.08"),
        benchmark_return=Decimal("0.05"),
        net_active_return=Decimal(active),
        max_drawdown=Decimal("0.10"),
        annualized_turnover=Decimal("1.2"),
        total_cost=Decimal("0.006"),
        average_gross_exposure=Decimal("0.60"),
        cash_day_fraction=Decimal("0.25"),
        exposure_state_distribution={
            "RISK_OFF": Decimal("0.10"),
            "DEFENSIVE": Decimal("0.20"),
            "NEUTRAL": Decimal("0.30"),
            "RISK_ON": Decimal("0.40"),
        },
        trade_or_rebalance_count=24,
        positive_month_rate=Decimal("0.58"),
        positive_half_year_count=2,
        worst_month=engine.PeriodActiveReturn(
            period=period,
            net_return=Decimal("-0.04"),
            benchmark_return=Decimal("-0.02"),
            net_active_return=Decimal("-0.02"),
        ),
        per_stock_pnl_contribution={"000001.SZ": Decimal("0.02")},
        largest_stock_pnl_share=Decimal("0.40"),
        largest_10_days_pnl_share=Decimal("0.35"),
    )


def _study() -> SimpleNamespace:
    development = SimpleNamespace(
        base=SimpleNamespace(metrics=_metrics("2020-03", active="0.04")),
        stress=SimpleNamespace(metrics=_metrics("2020-03", active="0.02")),
    )
    validation = SimpleNamespace(
        base=SimpleNamespace(metrics=_metrics("2023-08", active="0.03")),
        stress=SimpleNamespace(metrics=_metrics("2023-08", active="0.01")),
    )
    return SimpleNamespace(development=development, validation=validation)


class RunAlphaFeasibilityCliTests(unittest.TestCase):
    def test_date_and_endpoint_preflight_precede_token_and_output_access(self) -> None:
        source_config = json.loads(cli.reporting.DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
        mutations = {
            "post_cutoff": lambda value: value["dates"].__setitem__(
                "validation_end", "2024-01-01"
            ),
            "forbidden_endpoint": lambda value: value["source"][
                "allowed_endpoints"
            ].append("daily_basic"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                config = copy.deepcopy(source_config)
                mutate(config)
                config_path = root / "unsafe.json"
                config_path.write_text(json.dumps(config), encoding="utf-8")
                output_root = root / "must-not-exist"
                with mock.patch.object(
                    cli.data_lane, "run_backfill_from_environment"
                ) as backfill, mock.patch("sys.stderr", new_callable=io.StringIO):
                    code = cli.main(
                        [
                            "all",
                            "--config",
                            str(config_path),
                            "--output-root",
                            str(output_root),
                        ]
                    )
                self.assertEqual(code, 2)
                backfill.assert_not_called()
                self.assertFalse(output_root.exists())

    def test_pit_block_publishes_blocked_report_and_never_enters_alpha(self) -> None:
        blocked = _backfill(stage="BLOCKED_PIT_MEMBERSHIP")
        blocked["untrusted_provider_text"] = "super-secret-token-value 2024-01-01"
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            cli.data_lane,
            "run_backfill_from_environment",
            return_value=blocked,
        ), mock.patch.object(
            cli.data_lane, "load_feasibility_inputs"
        ) as load_inputs, mock.patch.object(
            cli.engine, "run_alpha_feasibility_study"
        ) as run_alpha, mock.patch.object(
            cli, "_current_commit_sha", return_value=COMMIT_SHA
        ), mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            code = cli.main(
                [
                    "all",
                    "--output-root",
                    temp_dir,
                    "--generated-at",
                    GENERATED_AT,
                ]
            )
            serialized = stdout.getvalue()
            report = json.loads(
                (Path(temp_dir) / "alpha_feasibility_report.json").read_text(
                    encoding="utf-8"
                )
            )
        self.assertEqual(code, 1)
        load_inputs.assert_not_called()
        run_alpha.assert_not_called()
        self.assertEqual(report["terminal_status"], "BLOCKED_DATA")
        self.assertIsNone(report["development_metrics"])
        self.assertIsNone(report["validation_metrics"])
        self.assertEqual(report["locked_test_status"], cli.LOCKED_TEST_STATUS)
        self.assertIs(report["locked_test_consumed"], False)
        self.assertNotIn("super-secret-token-value", serialized)
        self.assertNotIn("2024-01-01", serialized)

    def test_missing_token_after_valid_preflight_is_blocked_data_not_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            cli.data_lane,
            "run_backfill_from_environment",
            side_effect=cli.data_lane.AlphaFeasibilityDataError(
                "missing_tushare_token"
            ),
        ), mock.patch.object(
            cli, "_current_commit_sha", return_value=COMMIT_SHA
        ), mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            code = cli.main(
                [
                    "all",
                    "--output-root",
                    temp_dir,
                    "--generated-at",
                    GENERATED_AT,
                ]
            )
            summary = json.loads(stdout.getvalue())
            report = json.loads(
                (Path(temp_dir) / "alpha_feasibility_report.json").read_text(
                    encoding="utf-8"
                )
            )
        self.assertEqual(code, 1)
        self.assertEqual(summary["terminal_status"], "BLOCKED_DATA")
        self.assertEqual(report["terminal_status"], "BLOCKED_DATA")
        self.assertIn("missing_tushare_token", report["remaining_blockers"])
        self.assertEqual(
            report["actual_tushare_request_count_by_endpoint"],
            {endpoint: 0 for endpoint in cli.reporting.ALLOWED_ENDPOINTS},
        )

    def test_precollection_block_replays_byte_identically_without_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            cli.data_lane,
            "run_backfill_from_environment",
            side_effect=cli.data_lane.AlphaFeasibilityDataError(
                "missing_tushare_token"
            ),
        ), mock.patch.object(
            cli, "_current_commit_sha", return_value=COMMIT_SHA
        ):
            outputs: list[dict[str, object]] = []
            original: bytes | None = None
            for _ in range(2):
                with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    code = cli.main(["all", "--output-root", temp_dir])
                self.assertEqual(code, 1)
                outputs.append(json.loads(stdout.getvalue()))
                current = (
                    Path(temp_dir) / "alpha_feasibility_report.json"
                ).read_bytes()
                if original is None:
                    original = current
                else:
                    self.assertEqual(current, original)
            self.assertEqual(outputs[0], outputs[1])

    def test_completed_wiring_requires_adjusted_open_and_preserves_safety(self) -> None:
        captured: dict[str, engine.AlphaFeasibilityInput] = {}
        loaded = _loaded_inputs()
        loaded["signal_bars"] = iter(loaded["signal_bars"])
        loaded["suspensions"] = iter(loaded["suspensions"])

        def fake_run(*, inputs: engine.AlphaFeasibilityInput) -> SimpleNamespace:
            captured["inputs"] = inputs
            return _study()

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            cli.data_lane,
            "run_backfill_from_environment",
            return_value=_backfill(stage=cli.READY_STAGE),
        ), mock.patch.object(
            cli.data_lane,
            "load_feasibility_inputs",
            return_value=loaded,
        ), mock.patch.object(
            cli.engine, "run_alpha_feasibility_study", side_effect=fake_run
        ), mock.patch.object(
            cli, "_current_commit_sha", return_value=COMMIT_SHA
        ), mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            code = cli.main(
                [
                    "all",
                    "--output-root",
                    temp_dir,
                    "--generated-at",
                    GENERATED_AT,
                ]
            )
            summary = json.loads(stdout.getvalue())
            report = json.loads(
                (Path(temp_dir) / "alpha_feasibility_report.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(code, 0)
        signal_bars = tuple(captured["inputs"].stock_signal_bars)
        self.assertEqual(signal_bars[0].open, Decimal("108"))
        self.assertNotEqual(signal_bars[0].open, signal_bars[0].close)
        self.assertIsInstance(captured["inputs"].pit_admission, engine.PITAdmissionArtifacts)
        self.assertEqual(report["terminal_status"], "ALPHA_FEASIBILITY_GO_CANDIDATE")
        self.assertEqual(summary["terminal_status"], report["terminal_status"])
        self.assertEqual(report["safety"]["execution_realism"], "INCOMPLETE")
        self.assertIs(report["safety"]["paper_eligibility"], False)
        self.assertIs(report["safety"]["trade_eligibility"], False)
        self.assertIs(report["safety"]["automatic_order_submission"], False)
        self.assertEqual(report["locked_test_status"], cli.LOCKED_TEST_STATUS)
        self.assertIs(report["locked_test_consumed"], False)

    def test_missing_adjusted_open_fails_before_alpha_engine(self) -> None:
        loaded = _loaded_inputs()
        loaded["signal_bars"][0].pop("open")
        with self.assertRaisesRegex(cli.AlphaFeasibilityWorkflowError, "adjusted_open_required"):
            inputs = cli.build_alpha_input(loaded)
            tuple(inputs.stock_signal_bars)

    def test_data_command_stops_at_ready_data_without_alpha(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            cli.data_lane,
            "run_backfill_from_environment",
            return_value=_backfill(stage=cli.READY_STAGE),
        ), mock.patch.object(
            cli.data_lane, "load_feasibility_inputs"
        ) as load_inputs, mock.patch.object(
            cli.engine, "run_alpha_feasibility_study"
        ) as run_alpha, mock.patch.object(
            cli, "_current_commit_sha", return_value=COMMIT_SHA
        ), mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            code = cli.main(["data", "--output-root", temp_dir])
            summary = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(summary["stage_status"], cli.READY_STAGE)
        self.assertEqual(summary["locked_test_status"], cli.LOCKED_TEST_STATUS)
        self.assertIs(summary["locked_test_consumed"], False)
        load_inputs.assert_not_called()
        run_alpha.assert_not_called()


if __name__ == "__main__":
    unittest.main()
