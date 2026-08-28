from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from operations import run_technical_formal
from research.strategy_workspace import technical_formal_reporting as reporting
from research.strategy_workspace.contracts import canonical_sha256


GENERATED_AT = "2026-08-28T10:00:00+08:00"


def _complete_dataset_evidence() -> dict[str, object]:
    interfaces = {
        "trade_calendar": "trade_cal",
        "raw_daily_bar": "daily",
        "adjustment_factor": "adj_factor",
        "csi800_pit_membership": "index_weight",
        "suspension_history": "suspend_d",
        "price_limit_history": "stk_limit",
        "name_and_st_history": "namechange",
        "security_master": "stock_basic",
        "csi800_price_benchmark": "index_daily",
    }
    datasets = {}
    for dataset_id in reporting.REQUIRED_DATASETS:
        if dataset_id in reporting.WARMUP_REQUIRED_DATASETS:
            coverage_start = "2017-07-01"
        elif dataset_id == "csi800_pit_membership":
            coverage_start = "2017-12-29"
        else:
            coverage_start = "2018-01-01"
        datasets[dataset_id] = {
            "status": "complete",
            "source": "tushare_standard_non_vip",
            "interface": interfaces[dataset_id],
            "record_count": 1,
            "coverage_start": coverage_start,
            "coverage_end": "2025-12-31",
            "missing_dates": [],
            "content_sha256": hashlib.sha256(dataset_id.encode("utf-8")).hexdigest(),
            "issues": [],
        }
    return {
        "datasets": datasets,
        "critical_checks": {
            check_id: True for check_id in reporting.CRITICAL_CHECKS
        },
        "remaining_blockers": [],
    }


def _metrics(multiplier: float = 1.0) -> dict[str, object]:
    return {
        "net_return": 0.10 * multiplier,
        "benchmark_return": 0.05 * multiplier,
        "net_active_return": 0.05 * multiplier,
        "max_drawdown": 0.08,
        "turnover": 1.2,
        "total_cost": 12.5 * multiplier,
        "cost_to_gross_profit": 0.1,
        "exposure_state_distribution": {
            "RISK_OFF": 0.1,
            "DEFENSIVE": 0.2,
            "NEUTRAL": 0.3,
            "RISK_ON": 0.4,
        },
        "cash_day_fraction": 0.25,
        "positive_half_year_count": 4,
        "trade_count": 20,
        "win_rate": 0.55,
        "average_holding_period": 22.5,
        "per_stock_pnl_contribution": {"000001.SZ": 100.0 * multiplier},
        "largest_stock_pnl_share": 0.4,
        "largest_10_days_pnl_share": 0.35,
    }


def _split_results() -> dict[str, object]:
    return {
        "development": {
            "base_cost": _metrics(),
            "stress_cost": _metrics(0.9),
        },
        "validation": {
            "base_cost": _metrics(0.8),
            "stress_cost": _metrics(0.7),
        },
    }


def _resign(payload: dict[str, object], field: str) -> None:
    payload.pop(field, None)
    payload[field] = canonical_sha256(payload)


class TechnicalFormalReportingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.experiment = reporting.load_and_validate_experiment_config()

    def test_four_schemas_and_frozen_experiment_load(self) -> None:
        schemas = reporting.load_and_validate_schemas()
        self.assertEqual(
            set(schemas),
            {
                "technical_formal_dataset_manifest.v1.json",
                "technical_momentum_experiment.v1.json",
                "technical_momentum_backtest_report.v1.json",
                "technical_locked_test_readiness.v1.json",
            },
        )
        self.assertEqual(
            self.experiment["strategy_id"],
            "a-share-technical-momentum-adaptive-v1",
        )
        self.assertEqual(self.experiment["locked_test_status"], "NOT_RUN")
        self.assertIs(self.experiment["locked_test_consumed"], False)

    def test_missing_formal_data_generates_honest_blocked_chain(self) -> None:
        manifest = reporting.build_dataset_coverage_report(
            experiment=self.experiment,
            generated_at=GENERATED_AT,
        )
        report = reporting.build_development_validation_report(
            experiment=self.experiment,
            dataset_manifest=manifest,
            split_results=_split_results(),
            generated_at=GENERATED_AT,
        )
        readiness = reporting.build_locked_test_readiness(
            dataset_manifest=manifest,
            backtest_report=report,
            generated_at=GENERATED_AT,
        )

        self.assertEqual(manifest["data_status"], "BLOCKED")
        self.assertEqual(report["development"]["status"], "NOT_RUN_BLOCKED")
        self.assertEqual(report["validation"]["status"], "NOT_RUN_BLOCKED")
        self.assertIn(
            reporting.CONTROLLED_BACKTEST_BLOCKER,
            report["development"]["blockers"],
        )
        self.assertIn(
            reporting.CONTROLLED_BACKTEST_BLOCKER,
            report["validation"]["blockers"],
        )
        self.assertIsNone(report["development"]["base_cost"])
        self.assertIsNone(report["validation"]["stress_cost"])
        self.assertEqual(readiness["verdict"], "BLOCKED")
        for artifact, hash_field in (
            (manifest, "manifest_sha256"),
            (report, "report_sha256"),
            (readiness, "readiness_sha256"),
        ):
            unsigned = dict(artifact)
            declared = unsigned.pop(hash_field)
            self.assertEqual(declared, canonical_sha256(unsigned))
            self.assertEqual(artifact["locked_test_status"], "NOT_RUN")
            self.assertIs(artifact["locked_test_consumed"], False)
            self.assertEqual(artifact["safety"], reporting.SAFETY)

    def test_complete_caller_claims_cannot_unlock_readiness(self) -> None:
        manifest = reporting.build_dataset_coverage_report(
            experiment=self.experiment,
            dataset_evidence=_complete_dataset_evidence(),
            generated_at=GENERATED_AT,
        )
        report = reporting.build_development_validation_report(
            experiment=self.experiment,
            dataset_manifest=manifest,
            split_results=_split_results(),
            generated_at=GENERATED_AT,
        )
        readiness = reporting.build_locked_test_readiness(
            dataset_manifest=manifest,
            backtest_report=report,
            generated_at=GENERATED_AT,
        )

        self.assertEqual(manifest["data_status"], "BLOCKED")
        self.assertIn(
            reporting.RAW_DATASET_VERIFICATION_BLOCKER,
            manifest["remaining_blockers"],
        )
        self.assertEqual(report["development"]["status"], "NOT_RUN_BLOCKED")
        self.assertEqual(report["validation"]["status"], "NOT_RUN_BLOCKED")
        self.assertIsNone(report["development"]["base_cost"])
        self.assertIsNone(report["validation"]["stress_cost"])
        self.assertEqual(readiness["verdict"], "BLOCKED")
        self.assertIs(readiness["checks"]["formal_dataset_complete"], False)

    def test_pit_bootstrap_and_standard_interface_are_fail_closed(self) -> None:
        evidence = _complete_dataset_evidence()
        pit = evidence["datasets"]["csi800_pit_membership"]
        pit["coverage_start"] = "2018-01-01"
        manifest = reporting.build_dataset_coverage_report(
            experiment=self.experiment,
            dataset_evidence=evidence,
            generated_at=GENERATED_AT,
        )
        self.assertEqual(manifest["data_status"], "BLOCKED")
        self.assertIn(
            "csi800_pit_membership:warmup_or_start_coverage_missing",
            manifest["remaining_blockers"],
        )

        evidence = _complete_dataset_evidence()
        evidence["datasets"]["raw_daily_bar"]["source"] = "tushare_vip"
        manifest = reporting.build_dataset_coverage_report(
            experiment=self.experiment,
            dataset_evidence=evidence,
            generated_at=GENERATED_AT,
        )
        self.assertIn("raw_daily_bar:source_not_allowed", manifest["remaining_blockers"])

        evidence = _complete_dataset_evidence()
        evidence["datasets"]["raw_daily_bar"]["interface"] = "index_weight"
        manifest = reporting.build_dataset_coverage_report(
            experiment=self.experiment,
            dataset_evidence=evidence,
            generated_at=GENERATED_AT,
        )
        self.assertIn(
            "raw_daily_bar:interface_not_allowed_for_dataset",
            manifest["remaining_blockers"],
        )

    def test_warmup_is_required_only_for_signal_inputs(self) -> None:
        evidence = _complete_dataset_evidence()
        manifest = reporting.build_dataset_coverage_report(
            experiment=self.experiment,
            dataset_evidence=evidence,
            generated_at=GENERATED_AT,
        )
        self.assertEqual(manifest["data_status"], "BLOCKED")
        self.assertEqual(
            manifest["remaining_blockers"],
            [reporting.RAW_DATASET_VERIFICATION_BLOCKER],
        )
        self.assertEqual(
            manifest["datasets"]["trade_calendar"]["coverage_start"],
            "2018-01-01",
        )

        evidence["datasets"]["raw_daily_bar"]["coverage_start"] = "2018-01-01"
        manifest = reporting.build_dataset_coverage_report(
            experiment=self.experiment,
            dataset_evidence=evidence,
            generated_at=GENERATED_AT,
        )
        self.assertEqual(manifest["data_status"], "BLOCKED")
        self.assertIn(
            "raw_daily_bar:warmup_or_start_coverage_missing",
            manifest["remaining_blockers"],
        )

    def test_rehashed_semantic_lie_is_rejected(self) -> None:
        manifest = reporting.build_dataset_coverage_report(
            experiment=self.experiment,
            dataset_evidence=_complete_dataset_evidence(),
            generated_at=GENERATED_AT,
        )
        tampered = copy.deepcopy(manifest)
        tampered["datasets"]["raw_daily_bar"]["missing_dates"] = ["2020-02-03"]
        _resign(tampered, "manifest_sha256")
        with self.assertRaisesRegex(
            reporting.TechnicalFormalReportingError,
            "omits derived blockers",
        ):
            reporting.verify_dataset_coverage_report(tampered)

    def test_locked_split_selection_is_rejected_without_interpreting_results(self) -> None:
        manifest = reporting.build_dataset_coverage_report(
            experiment=self.experiment,
            dataset_evidence=_complete_dataset_evidence(),
            generated_at=GENERATED_AT,
        )
        with self.assertRaisesRegex(
            reporting.TechnicalFormalReportingError,
            "Locked Test split is forbidden",
        ):
            reporting.build_development_validation_report(
                experiment=self.experiment,
                dataset_manifest=manifest,
                split_results={"locked_test": {"base_cost": {}, "stress_cost": {}}},
                selected_splits=("locked_test",),
                generated_at=GENERATED_AT,
            )

    def test_readiness_file_builder_reads_exactly_two_prelocked_reports(self) -> None:
        manifest = reporting.build_dataset_coverage_report(
            experiment=self.experiment,
            dataset_evidence=_complete_dataset_evidence(),
            generated_at=GENERATED_AT,
        )
        report = reporting.build_development_validation_report(
            experiment=self.experiment,
            dataset_manifest=manifest,
            split_results=_split_results(),
            generated_at=GENERATED_AT,
        )
        with mock.patch.object(
            reporting,
            "_load_json_file",
            side_effect=[manifest, report],
        ) as loader:
            readiness = reporting.build_locked_test_readiness_from_files(
                dataset_manifest_path="manifest.json",
                backtest_report_path="dev-val.json",
                generated_at=GENERATED_AT,
            )
        self.assertEqual(readiness["verdict"], "BLOCKED")
        self.assertEqual(loader.call_count, 2)
        self.assertEqual(loader.call_args_list[0].args[0], "manifest.json")
        self.assertEqual(loader.call_args_list[1].args[0], "dev-val.json")

    def test_publish_is_create_only_and_emits_exact_artifacts(self) -> None:
        manifest = reporting.build_dataset_coverage_report(
            experiment=self.experiment,
            generated_at=GENERATED_AT,
        )
        report = reporting.build_development_validation_report(
            experiment=self.experiment,
            dataset_manifest=manifest,
            generated_at=GENERATED_AT,
        )
        readiness = reporting.build_locked_test_readiness(
            dataset_manifest=manifest,
            backtest_report=report,
            generated_at=GENERATED_AT,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "formal-reports"
            paths = reporting.publish_formal_reports(
                output_directory=output,
                dataset_manifest=manifest,
                backtest_report=report,
                readiness_report=readiness,
            )
            self.assertEqual(
                set(paths),
                {
                    reporting.DATASET_REPORT_FILENAME,
                    reporting.BACKTEST_REPORT_FILENAME,
                    reporting.READINESS_REPORT_FILENAME,
                },
            )
            self.assertEqual({path.name for path in output.iterdir()}, set(paths))
            with self.assertRaisesRegex(
                reporting.TechnicalFormalReportingError,
                "create_only_output_directory_exists",
            ):
                reporting.publish_formal_reports(
                    output_directory=output,
                    dataset_manifest=manifest,
                    backtest_report=report,
                    readiness_report=readiness,
                )

    def test_rehashed_ready_claim_cannot_bypass_unimplemented_gates(self) -> None:
        manifest = reporting.build_dataset_coverage_report(
            experiment=self.experiment,
            generated_at=GENERATED_AT,
        )
        report = reporting.build_development_validation_report(
            experiment=self.experiment,
            dataset_manifest=manifest,
            generated_at=GENERATED_AT,
        )
        readiness = reporting.build_locked_test_readiness(
            dataset_manifest=manifest,
            backtest_report=report,
            generated_at=GENERATED_AT,
        )
        forged = copy.deepcopy(readiness)
        forged["checks"] = {key: True for key in forged["checks"]}
        forged["remaining_blockers"] = []
        forged["verdict"] = "DATA_READY_FOR_LOCKED_TEST"
        _resign(forged, "readiness_sha256")
        with self.assertRaisesRegex(
            reporting.TechnicalFormalReportingError,
            "raw-data verification gate",
        ):
            reporting.verify_locked_test_readiness(forged)

    def test_cli_rejects_locked_split_before_any_path_or_config_read(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "must-not-exist"
            stderr = io.StringIO()
            with (
                mock.patch.object(
                    run_technical_formal, "load_and_validate_experiment_config"
                ) as load_config,
                mock.patch.object(run_technical_formal, "_load_optional_json") as load_json,
                contextlib.redirect_stderr(stderr),
            ):
                return_code = run_technical_formal.main(
                    [
                        "--split",
                        "locked_test",
                        "--config",
                        "must-not-read.json",
                        "--dataset-evidence",
                        "must-not-read-data.json",
                        "--output-directory",
                        str(output),
                    ]
                )
            self.assertEqual(return_code, 2)
            self.assertIn("locked_test_forbidden_before_data_path_read", stderr.getvalue())
            load_config.assert_not_called()
            load_json.assert_not_called()
            self.assertFalse(output.exists())

    def test_cli_rejects_existing_output_before_input_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stderr = io.StringIO()
            with (
                mock.patch.object(
                    run_technical_formal, "load_and_validate_experiment_config"
                ) as load_config,
                mock.patch.object(run_technical_formal, "_load_optional_json") as load_json,
                contextlib.redirect_stderr(stderr),
            ):
                return_code = run_technical_formal.main(
                    [
                        "--config",
                        "must-not-read.json",
                        "--dataset-evidence",
                        "must-not-read-data.json",
                        "--output-directory",
                        temp_dir,
                    ]
                )
            self.assertEqual(return_code, 2)
            self.assertIn("create_only_output_directory_exists", stderr.getvalue())
            load_config.assert_not_called()
            load_json.assert_not_called()

    def test_cli_blocked_gate_does_not_open_metric_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "blocked"
            stdout = io.StringIO()
            return_code = None
            with contextlib.redirect_stdout(stdout):
                return_code = run_technical_formal.main(
                    [
                        "--output-directory",
                        str(output),
                        "--development-metrics",
                        str(Path(temp_dir) / "locked-results-must-not-open.json"),
                        "--validation-metrics",
                        str(Path(temp_dir) / "validation-must-not-open.json"),
                        "--generated-at",
                        GENERATED_AT,
                    ]
                )
            self.assertEqual(return_code, 1)
            self.assertEqual(json.loads(stdout.getvalue())["verdict"], "BLOCKED")
            self.assertEqual(
                {path.name for path in output.iterdir()},
                {
                    reporting.DATASET_REPORT_FILENAME,
                    reporting.BACKTEST_REPORT_FILENAME,
                    reporting.READINESS_REPORT_FILENAME,
                },
            )


if __name__ == "__main__":
    unittest.main()
