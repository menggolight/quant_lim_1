from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from operations import p15_alpha_feasibility_run as p15
from operations import run_alpha_feasibility as cli


SHA_A = "a" * 40
SHA_B = "b" * 40
PROCESS_ID = "1" * 32
EXPERIMENT_V3 = {"schema_version": "technical-alpha-feasibility-experiment.v3"}
P15_CONFIG = (
    p15.data_lane.REPOSITORY_ROOT
    / "configs"
    / "a_share_technical_alpha_feasibility.p1_5.json"
)
RUNTIME_BUNDLE = {
    "schema_version": "alpha-feasibility-runtime-implementation-bundle.v1",
    "config_path": "configs/test.json",
    "files": {
        **{path: "9" * 64 for path in p15.RUNTIME_PATHS},
        "configs/test.json": "9" * 64,
        "schemas/test.json": "9" * 64,
    },
    "bundle_sha256": "8" * 64,
}
ZERO_COUNTS = {
    "trade_cal": 0,
    "index_weight": 0,
    "daily": 0,
    "adj_factor": 0,
    "index_daily": 0,
    "suspend_d": 0,
}


def _plan() -> SimpleNamespace:
    task = SimpleNamespace(task_id="index_weight-p14d", params={"start_date": "20171201"})
    return SimpleNamespace(plan_sha256="c" * 64, pit_tasks=(task,))


def _claim() -> dict[str, object]:
    return {
        "network_run_id": "p15-test-run",
        "collection_plan_sha256": "c" * 64,
        "code_commit_sha": SHA_A,
        "baseline_commit_sha": SHA_A,
        "baseline_commit_semantics": p15.BASELINE_COMMIT_SEMANTICS,
        "remote_branch_sha": SHA_B,
        "working_tree_clean": False,
        "runtime_implementation_bundle": RUNTIME_BUNDLE,
        "claim_sha256": "d" * 64,
    }


def _blocked_source(
    experiment: dict[str, object],
    remaining_blockers: tuple[str, ...] = ("pit_membership_incomplete",),
) -> dict[str, object]:
    return p15.reporting.build_blocked_alpha_feasibility_report(
        commit_sha=SHA_A,
        data_summary={
            "actual_tushare_request_count_by_endpoint": dict(ZERO_COUNTS),
            "collection_plan_sha256": "c" * 64,
            "daily_coverage_status": "blocked",
            "adj_factor_coverage_status": "blocked",
            "benchmark_coverage_status": "blocked",
            "suspension_coverage_status": "blocked",
            "data_status": "BLOCKED_PIT_MEMBERSHIP",
            "remaining_blockers": list(remaining_blockers),
        },
        experiment=experiment,
        generated_at="2026-08-31T12:00:00+08:00",
    )


class P15RunTests(unittest.TestCase):
    def test_parser_accepts_explicit_p15_arguments(self) -> None:
        args = cli.build_parser().parse_args(
            [
                "all",
                "--network-run-id",
                "p15-local-001",
                "--p14d-import-root",
                "diagnostic-root",
            ]
        )
        self.assertEqual(args.network_run_id, "p15-local-001")
        self.assertEqual(args.p14d_import_root, Path("diagnostic-root"))

    def test_incomplete_p15_arguments_fail_before_config_or_network(self) -> None:
        with (
            mock.patch.object(
                cli.reporting,
                "load_and_validate_experiment_config",
                return_value=EXPERIMENT_V3,
            ) as preflight,
            mock.patch.object(cli.data_lane, "run_backfill_from_environment") as network,
        ):
            with self.assertRaisesRegex(
                cli.AlphaFeasibilityWorkflowError,
                "p15_run_arguments_required",
            ):
                cli.run_workflow(
                    command="all",
                    config_path=Path("unused.json"),
                    output_root=Path("unused"),
                    network_run_id="p15-local-001",
                )
        preflight.assert_called_once()
        network.assert_not_called()

    def test_import_failure_stops_before_network_lane(self) -> None:
        with (
            mock.patch.object(cli.reporting, "load_and_validate_experiment_config", return_value=EXPERIMENT_V3),
            mock.patch.object(p15, "prepare", side_effect=p15.P15RunError("p14d_import_failed")),
            mock.patch.object(cli.data_lane, "run_backfill_from_environment") as network,
        ):
            with self.assertRaisesRegex(p15.P15RunError, "p14d_import_failed"):
                cli.run_workflow(
                    command="all",
                    config_path=Path("config.json"),
                    output_root=Path("output"),
                    network_run_id="p15-local-001",
                    p14d_import_root=Path("diagnostic"),
                )
        network.assert_not_called()

    def test_p15_data_command_is_rejected_before_process_prepare(self) -> None:
        with (
            mock.patch.object(
                cli.reporting,
                "load_and_validate_experiment_config",
                return_value=EXPERIMENT_V3,
            ),
            mock.patch.object(p15, "prepare") as prepare,
        ):
            with self.assertRaisesRegex(
                cli.AlphaFeasibilityWorkflowError,
                "p15_data_command_not_supported",
            ):
                cli.run_workflow(
                    command="data",
                    config_path=Path("config.json"),
                    output_root=Path("output"),
                    network_run_id="p15-local-001",
                    p14d_import_root=Path("diagnostic"),
                )
        prepare.assert_not_called()

    def test_cli_transport_interruption_is_recoverable_without_terminal_artifacts(self) -> None:
        context = p15.RunContext(_plan(), _claim(), 1, 1, PROCESS_ID)
        recoverable = {
            "status": "recoverable_interruption",
            "terminal_status": None,
            "run_receipt_created": False,
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                cli.reporting,
                "load_and_validate_experiment_config",
                return_value=EXPERIMENT_V3,
            ),
            mock.patch.object(p15, "prepare", return_value=(context, None)),
            mock.patch.object(
                cli.data_lane,
                "run_backfill_from_environment",
                side_effect=cli.data_lane.AlphaFeasibilityDataError(
                    "https_transport_failed"
                ),
            ),
            mock.patch.object(
                p15, "recoverable_summary", return_value=recoverable
            ) as resume,
            mock.patch.object(cli.reporting, "publish_alpha_feasibility_report") as publish,
            mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as stdout,
        ):
            code = cli.main(
                [
                    "all",
                    "--config",
                    "config.json",
                    "--output-root",
                    directory,
                    "--network-run-id",
                    "p15-local-001",
                    "--p14d-import-root",
                    "diagnostic",
                ]
            )
        self.assertEqual(code, cli.RECOVERABLE_EXIT_CODE)
        self.assertEqual(json.loads(stdout.getvalue()), recoverable)
        resume.assert_called_once_with(context, Path(directory), "https_transport_failed")
        publish.assert_not_called()
        self.assertFalse((Path(directory) / p15.RUN_RECEIPT_FILENAME).exists())
        self.assertFalse((Path(directory) / cli.reporting.REPORT_FILENAME).exists())

    def test_prepare_records_each_process_and_counts_resume(self) -> None:
        plan = _plan()
        imported = SimpleNamespace(
            task=plan.pit_tasks[0],
            request_origin="offline_p14d_import",
            network_request_count=0,
        )
        store = mock.Mock()
        store.is_complete.return_value = True
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                mock.patch.object(p15.data_lane, "load_config_and_build_plan", return_value=plan),
                mock.patch.object(p15.data_lane, "import_p14d_diagnostic_into_plan", return_value=imported),
                mock.patch.object(p15.data_lane, "CreateOnlyTaskStore", return_value=store),
                mock.patch.object(p15, "_completed_count", side_effect=(1, 7)),
                mock.patch.object(p15, "_runtime_identity", return_value={key: value for key, value in _claim().items() if key not in {"network_run_id", "collection_plan_sha256", "claim_sha256"}}),
                mock.patch.dict(os.environ, {"TUSHARE_TOKEN": "must-not-persist"}),
            ):
                first, receipt = p15.prepare(
                    network_run_id="p15-local-001",
                    p14d_import_root=root / "diagnostic",
                    output_root=root,
                    config_path=root / "config.json",
                    experiment=EXPERIMENT_V3,
                )
                second, receipt2 = p15.prepare(
                    network_run_id="p15-local-001",
                    p14d_import_root=root / "diagnostic",
                    output_root=root,
                    config_path=root / "config.json",
                    experiment=EXPERIMENT_V3,
                )
            self.assertIsNone(receipt)
            self.assertIsNone(receipt2)
            self.assertEqual(first.network_process_count, 1)
            self.assertEqual(first.resumed_request_fingerprint_count, 1)
            self.assertEqual(second.network_process_count, 2)
            self.assertEqual(second.resumed_request_fingerprint_count, 7)
            artifacts = b"".join(path.read_bytes() for path in root.rglob("*.json"))
            self.assertNotIn(b"must-not-persist", artifacts)

            repository = root / "repository"
            for relative in p15.RUNTIME_PATHS:
                target = repository / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(relative, encoding="utf-8")
            config = repository / "configs" / "p15.json"
            schema = repository / "schemas" / "receipt.json"
            config.parent.mkdir(parents=True, exist_ok=True)
            schema.parent.mkdir(parents=True, exist_ok=True)
            config.write_text("{}", encoding="utf-8")
            schema.write_text("{}", encoding="utf-8")
            original = p15._runtime_bundle(repository, config)
            config_claim = root / "config-claim.json"
            p15._publish(
                config_claim,
                p15._self_hash(
                    {"runtime_implementation_bundle": original}, "claim_sha256"
                ),
                "network_run_claim_mismatch",
            )
            config.write_text('{"changed":true}', encoding="utf-8")
            with self.assertRaisesRegex(p15.P15RunError, "network_run_claim_mismatch"):
                p15._publish(
                    config_claim,
                    p15._self_hash(
                        {
                            "runtime_implementation_bundle": p15._runtime_bundle(
                                repository, config
                            )
                        },
                        "claim_sha256",
                    ),
                    "network_run_claim_mismatch",
                )
            schema_claim = root / "schema-claim.json"
            p15._publish(
                schema_claim,
                p15._self_hash(
                    {
                        "runtime_implementation_bundle": p15._runtime_bundle(
                            repository, config
                        )
                    },
                    "claim_sha256",
                ),
                "network_run_claim_mismatch",
            )
            schema.write_text('{"changed":true}', encoding="utf-8")
            with self.assertRaisesRegex(p15.P15RunError, "network_run_claim_mismatch"):
                p15._publish(
                    schema_claim,
                    p15._self_hash(
                        {
                            "runtime_implementation_bundle": p15._runtime_bundle(
                                repository, config
                            )
                        },
                        "claim_sha256",
                    ),
                    "network_run_claim_mismatch",
                )

    def test_receipt_is_schema_valid_create_only_and_safe(self) -> None:
        context = p15.RunContext(_plan(), _claim(), 1, 1, PROCESS_ID)
        experiment = p15.reporting.load_and_validate_experiment_config(P15_CONFIG)
        source = _blocked_source(experiment)
        pit = {
            "months_expected": 73,
            "months_observed": 0,
            "snapshot_count": 0,
            "snapshot_dates": [],
            "missing_months": ["2017-12"],
            "union_instrument_count": 0,
            "zero_weight_count": 0,
            "weight_sum_min": None,
            "weight_sum_max": None,
            "coverage_status": "blocked",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                mock.patch.object(p15, "_expected_tasks", return_value=(object(),)),
                mock.patch.object(p15, "_completed_count", return_value=1),
                mock.patch.object(p15, "_verify_runtime_claim"),
                mock.patch.object(p15, "_process_records", return_value=[{"process_id": PROCESS_ID, "completed_request_fingerprint_count_at_start": 1}]),
                mock.patch.object(p15, "_pit_summary", return_value=pit),
                mock.patch.object(
                    p15.data_lane,
                    "actual_tushare_request_count_by_endpoint",
                    return_value=dict(ZERO_COUNTS),
                ),
            ):
                report_path = p15.reporting.publish_alpha_feasibility_report(
                    root,
                    source,
                    experiment=experiment,
                )
                report_bytes = report_path.read_bytes()
                self.assertEqual(
                    report_bytes,
                    p15.data_lane.canonical_json_bytes(source),
                )
                self.assertTrue(report_bytes.endswith(b"\n"))
                self.assertFalse(report_bytes.endswith(b"\n\n"))
                first, path = p15.publish_receipt(
                    context=context,
                    output_root=root,
                    source=source,
                    experiment=experiment,
                )
                second, same_path = p15.publish_receipt(
                    context=context,
                    output_root=root,
                    source=source,
                    experiment=experiment,
                )
                changed = _blocked_source(
                    experiment,
                    ("pit_membership_incomplete", "upstream_unknown_error"),
                )
                with self.assertRaisesRegex(p15.P15RunError, "run_receipt_create_only_mismatch"):
                    p15.publish_receipt(
                        context=context,
                        output_root=root,
                        source=changed,
                        experiment=experiment,
                    )
                before_replay = {
                    item.relative_to(root): item.read_bytes()
                    for item in root.rglob("*")
                    if item.is_file()
                }
                replayed = p15.load_existing_receipt(
                    root, context.claim, context.plan, experiment
                )
                after_replay = {
                    item.relative_to(root): item.read_bytes()
                    for item in root.rglob("*")
                    if item.is_file()
                }
                self.assertEqual(after_replay, before_replay)
                tampered_report = changed
                report_path.write_bytes(
                    p15.data_lane.canonical_json_bytes(tampered_report)
                )
                with self.assertRaisesRegex(
                    p15.P15RunError, "run_receipt_report_hash_mismatch"
                ):
                    p15.load_existing_receipt(
                        root, context.claim, context.plan, experiment
                    )
            self.assertEqual(path, same_path)
            self.assertEqual(first, second)
            self.assertEqual(replayed, first)
            self.assertEqual(first["actual_request_count_by_endpoint"]["index_weight"], 0)
            self.assertEqual(first["market_data"]["coverage_end"], "2023-12-31")
            self.assertFalse(first["locked_test_consumed"])
            self.assertFalse(first["safety"]["real_money_list_allowed"])
            self.assertEqual(first["request_count_semantics"], p15.REQUEST_COUNT_SEMANTICS)
            expanded = json.loads(json.dumps(first))
            expanded["pit"]["snapshot_count"] = 74
            expanded["pit"]["snapshot_dates"] = [
                (date(2017, 7, 1) + timedelta(days=offset)).isoformat()
                for offset in range(74)
            ]
            expanded.pop("receipt_sha256")
            expanded["receipt_sha256"] = p15.data_lane.canonical_sha256(expanded)
            p15.validate_json_schema(expanded, p15.RECEIPT_SCHEMA_PATH)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["receipt_sha256"], first["receipt_sha256"])

    def test_blocked_pit_summary_rebuilds_partial_coverage_report(self) -> None:
        snapshots = [
            {"snapshot_date": "2017-12-22", "zero_weight_count": 2, "weight_sum": "99.98"},
            {"snapshot_date": "2017-12-29", "zero_weight_count": 3, "weight_sum": "100.02"},
        ]
        report = {
            "pit_months_expected": 73,
            "pit_months_observed": 1,
            "pit_snapshot_count": 2,
            "snapshot_dates": ["2017-12-22", "2017-12-29"],
            "missing_months": ["2018-01"],
            "zero_weight_count_by_snapshot": {"2017-12-22": 2, "2017-12-29": 3},
            "weight_sum_by_snapshot": {"2017-12-22": "99.98", "2017-12-29": "100.02"},
            "monthly_checks": [
                {"month": "2017-12", "status": "complete", "snapshots": snapshots},
                {"month": "2018-01", "status": "missing", "snapshots": []},
            ],
        }
        result = SimpleNamespace(
            coverage_report=report,
            manifest={"manifest_sha256": "7" * 64, "union_instrument_count": 0},
            passed=False,
        )
        context = p15.RunContext(_plan(), _claim(), 1, 1, PROCESS_ID)
        with mock.patch.object(
            p15.data_lane, "_load_existing_pit_result", return_value=result
        ):
            summary = p15._pit_summary(Path("output"), context, "7" * 64)
        self.assertEqual(summary["snapshot_count"], 2)
        self.assertEqual(summary["zero_weight_count"], 5)
        self.assertEqual(summary["weight_sum_min"], "99.98")
        self.assertEqual(summary["weight_sum_max"], "100.02")
        self.assertEqual(summary["coverage_status"], "blocked")


if __name__ == "__main__":
    unittest.main()
