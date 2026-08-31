from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from operations import p15_alpha_feasibility_continuation as continuation
from research.market_data import tushare_alpha_feasibility as data_lane
from research.market_data.validation import validate_json_schema
from research.strategy_workspace import alpha_feasibility_reporting as reporting


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64


def _runtime_bundle(config_relative: str) -> dict[str, object]:
    return continuation._self_hash(
        {
            "schema_version": "alpha-feasibility-continuation-runtime-bundle.v1",
            "config_path": config_relative,
            "files": {
                config_relative: SHA_B,
                "research/strategy_workspace/alpha_feasibility.py": SHA_C,
            },
        },
        "bundle_sha256",
    )


def _immutable_bundle(experiment: dict[str, object]) -> dict[str, object]:
    return continuation._self_hash(
        {
            "experiment_config_canonical_sha256": reporting.canonical_sha256(
                experiment
            ),
            "alpha_engine_sha256": SHA_C,
            "frozen_implementation_files": {
                "research/strategy_workspace/alpha.py": SHA_A,
                "research/strategy_workspace/ranker.py": SHA_B,
                "research/strategy_workspace/exposure.py": SHA_C,
            },
            "dates_sha256": SHA_A,
            "portfolio_sha256": SHA_B,
            "costs_sha256": SHA_C,
            "gate_sha256": SHA_D,
            "locked_policy_sha256": SHA_E,
        },
        "bundle_sha256",
    )


class ContinuationFixture:
    def __init__(self, root: Path) -> None:
        self.parent = root / "parent"
        self.child = root / "child"
        self.parent.mkdir()
        self.plan = data_lane.load_config_and_build_plan(reporting.P15_CONFIG_PATH)
        self.experiment = dict(
            reporting.load_and_validate_experiment_config(reporting.P15_CONFIG_PATH)
        )
        config_relative = reporting.P15_CONFIG_PATH.resolve().relative_to(
            data_lane.REPOSITORY_ROOT.resolve()
        ).as_posix()
        self.runtime = _runtime_bundle(config_relative)
        self.immutable = _immutable_bundle(self.experiment)
        parent_binding = {
            "network_run_id": "parent-run-001",
            "run_claim_sha256": SHA_A,
            "receipt_sha256": SHA_B,
            "report_sha256": SHA_C,
            "pit_coverage_report_sha256": SHA_D,
            "pit_manifest_sha256": SHA_E,
            "runtime_bundle_sha256": self.runtime["bundle_sha256"],
            "experiment_config_canonical_sha256": self.immutable[
                "experiment_config_canonical_sha256"
            ],
        }
        reused = []
        for ordinal, task in enumerate(self.plan.pit_tasks[:19], start=1):
            reused.append(
                {
                    "ordinal": ordinal,
                    "request_fingerprint": task.task_id,
                    "task_id": task.task_id,
                    "task_sha256": data_lane.canonical_sha256(task.to_dict()),
                    "endpoint": "index_weight",
                    "month": continuation._task_month(task),
                    "params": dict(task.params),
                    "provenance_kind": (
                        "offline_p14d_import" if ordinal == 1 else "network"
                    ),
                    "started_artifact_sha256": None if ordinal == 1 else SHA_A,
                    "import_artifact_sha256": SHA_B if ordinal == 1 else None,
                    "raw_artifact_sha256": SHA_C,
                    "response_file_sha256": SHA_D,
                    "response_artifact_sha256": SHA_E,
                    "normalized_content_sha256": SHA_F,
                    "attempt_artifact_sha256_by_number": (
                        [] if ordinal == 1 else [SHA_A]
                    ),
                    "network_request_count": 0 if ordinal == 1 else 1,
                }
            )
        first = self.plan.pit_tasks[19]
        first_unfinished = {
            "ordinal": 20,
            "request_fingerprint": first.task_id,
            "task_id": first.task_id,
            "task_sha256": data_lane.canonical_sha256(first.to_dict()),
            "endpoint": "index_weight",
            "month": "2019-07",
            "params": dict(first.params),
            "started_artifact_sha256": SHA_A,
            "raw_transport_sha256": SHA_B,
            "quarantine_artifact_sha256": SHA_C,
            "attempt_artifact_sha256_by_number": [SHA_D],
            "parent_attempt_count": 1,
            "next_attempt_number": 2,
            "maximum_cumulative_attempts": 3,
            "parent_failure_code": "upstream_unknown_error",
            "parent_upstream_code": 40204,
            "parent_classification": "UNCLASSIFIED_PARENT_EVIDENCE",
        }
        self.evidence = continuation._ParentEvidence(
            plan=self.plan,
            experiment=self.experiment,
            parent_binding=parent_binding,
            parent_runtime_bundle=self.runtime,
            parent_actual_request_count_by_endpoint={
                endpoint: 0 for endpoint in reporting.ALLOWED_ENDPOINTS
            },
            reused_tasks=tuple(reused),
            first_unfinished=first_unfinished,
        )

    def prepare(self) -> continuation.ContinuationContext:
        with (
            mock.patch.object(
                continuation, "_validate_parent_evidence", return_value=self.evidence
            ),
            mock.patch.object(
                continuation, "_current_runtime_bundle", return_value=self.runtime
            ),
            mock.patch.object(
                continuation, "_immutable_strategy_bundle", return_value=self.immutable
            ),
        ):
            return continuation.prepare_continuation(
                parent_root=self.parent,
                child_root=self.child,
                continuation_run_id="continuation-run-001",
                prepared_at="2026-08-31T01:00:00+08:00",
            )

    def stage_records(self) -> list[dict[str, object]]:
        records = []
        for ordinal, item in enumerate(self.evidence.reused_tasks, start=1):
            records.append(
                {
                    "ordinal": ordinal,
                    "task_id": item["task_id"],
                    "task_sha256": item["task_sha256"],
                    "parent_raw_artifact_sha256": item["raw_artifact_sha256"],
                    "parent_response_file_sha256": item[
                        "response_file_sha256"
                    ],
                    "child_raw_artifact_sha256": item["raw_artifact_sha256"],
                    "child_response_file_sha256": item[
                        "response_file_sha256"
                    ],
                    "child_import_artifact_sha256": SHA_F,
                    "import_schema_version": "tushare-alpha-feasibility-task-import.v2",
                    "request_origin": "offline_parent_run_reuse",
                    "network_request_count": 0,
                }
            )
        return records

    def first_stage_evidence(self) -> dict[str, object]:
        item = self.evidence.first_unfinished
        return {
            "ordinal": 20,
            "task_id": item["task_id"],
            "task_sha256": item["task_sha256"],
            "parent_started_artifact_sha256": SHA_A,
            "parent_attempt_artifact_sha256_by_number": [SHA_D],
            "child_started_artifact_sha256": SHA_A,
            "child_attempt_artifact_sha256_by_number": [SHA_D],
            "parent_attempt_count": 1,
            "next_attempt_number": 2,
            "parent_terminal_quarantine_copied": False,
        }

    def write_parent_first_attempt(self) -> None:
        store = data_lane.CreateOnlyTaskStore(self.parent)
        task = self.plan.pit_tasks[19]
        for path, value in (
            (store.started_path(task), {"kind": "parent-started"}),
            (store.attempt_path(task, 1), {"kind": "parent-attempt-1"}),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data_lane.canonical_json_bytes(value))


class P15ContinuationTests(unittest.TestCase):
    def test_prepare_binds_prefix_and_closed_retry_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ContinuationFixture(Path(directory))
            context = fixture.prepare()
            policy = context.claim["execution_policy"]
            self.assertNotIn("automatic_retries", policy)
            self.assertEqual(
                policy["retry_count_by_failure_shape"],
                {"RATE_LIMITED": 1, "UPSTREAM_SERVER_ERROR": 1},
            )
            self.assertEqual(policy["other_failure_retry_count"], 0)
            self.assertEqual(policy["rate_limit_fallback_seconds"], 65)
            self.assertEqual(policy["maximum_retry_after_seconds"], 300)
            self.assertEqual(policy["minimum_transport_interval_seconds"], "12")
            self.assertEqual(policy["maximum_cumulative_attempts_per_fingerprint"], 3)
            self.assertEqual(policy["parent_attempt_count"], 1)
            self.assertEqual(policy["next_attempt_number"], 2)
            self.assertEqual(
                context.reuse_manifest["first_unfinished"]["parent_upstream_code"],
                40204,
            )
            self.assertEqual(
                context.reuse_manifest["first_unfinished"]["parent_classification"],
                "UNCLASSIFIED_PARENT_EVIDENCE",
            )
            validate_json_schema(context.claim, continuation.CLAIM_SCHEMA_PATH)
            validate_json_schema(
                context.reuse_manifest, continuation.REUSE_SCHEMA_PATH
            )

    def test_prepare_rejects_nonempty_unclaimed_child(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ContinuationFixture(Path(directory))
            fixture.child.mkdir()
            (fixture.child / "unbound.txt").write_text("unbound", encoding="utf-8")
            with self.assertRaisesRegex(
                continuation.P15ContinuationError,
                "continuation_child_root_not_create_only",
            ):
                fixture.prepare()

    def test_stage_calls_import_v2_for_exact_prefix_and_starts_task_20(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ContinuationFixture(Path(directory))
            context = fixture.prepare()
            fixture.write_parent_first_attempt()
            imported: list[str] = []

            def importer(**kwargs: object) -> None:
                imported.append(kwargs["task"].task_id)  # type: ignore[attr-defined]

            with (
                mock.patch.object(
                    continuation,
                    "_stage_reused_task_records",
                    return_value=fixture.stage_records(),
                ),
                mock.patch.object(
                    continuation,
                    "_stage_first_unfinished_evidence",
                    return_value=fixture.first_stage_evidence(),
                ),
            ):
                stage, _ = continuation.stage_parent_reuse(
                    context=context,
                    staged_at="2026-08-31T01:01:00+08:00",
                    import_parent_reuse_task_v2=importer,
                )
                marker, _ = continuation.start_network_process(
                    context=context,
                    network_process_id="network-process-001",
                    started_at="2026-08-31T01:02:00+08:00",
                )
                with self.assertRaisesRegex(
                    continuation.P15ContinuationError,
                    "continuation_network_process_already_started",
                ):
                    continuation.start_network_process(
                        context=context,
                        network_process_id="network-process-002",
                        started_at="2026-08-31T01:03:00+08:00",
                    )

            self.assertEqual(
                imported,
                [task.task_id for task in fixture.plan.pit_tasks[:19]],
            )
            self.assertEqual(stage["child_completed_prefix_count"], 19)
            self.assertEqual(stage["next_task_ordinal"], 20)
            self.assertFalse(stage["parent_terminal_quarantine_copied"])
            self.assertEqual(marker["network_process_count"], 1)
            self.assertEqual(marker["completed_request_fingerprint_count_at_start"], 19)
            child_store = data_lane.CreateOnlyTaskStore(fixture.child)
            task = fixture.plan.pit_tasks[19]
            self.assertTrue(child_store.started_path(task).is_file())
            self.assertTrue(child_store.attempt_path(task, 1).is_file())
            self.assertFalse(child_store.quarantine_path(task).exists())

    def test_receipt_binds_process_first_response_pit_and_exact_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ContinuationFixture(Path(directory))
            context = fixture.prepare()
            stage = {"stage_sha256": SHA_A}
            marker = {
                "network_process_id": "network-process-001",
                "marker_sha256": SHA_B,
            }
            counts = {endpoint: 0 for endpoint in reporting.ALLOWED_ENDPOINTS}
            counts["index_weight"] = 1
            first_response = {
                "task_id": continuation.FIRST_UNFINISHED_TASK_ID,
                "attempt_number": 2,
                "business_code": 0,
                "classification": None,
                "sanitized_msg": "",
                "detail_type": None,
                "safe_detail_projection": None,
                "msg_sha256": SHA_A,
                "detail_sha256": None,
                "request_id_sha256": None,
                "raw_transport_sha256": SHA_B,
                "response_body_sha256": SHA_B,
                "response_byte_count": 123,
                "requested_at": "2026-08-31T01:02:01+08:00",
                "completed_at": "2026-08-31T01:02:02+08:00",
                "retry_after_seconds": None,
                "retry_performed": False,
                "result": "FIRST_ATTEMPT_SUCCEEDED",
                "evidence_artifact_sha256": SHA_C,
            }
            missing = [
                continuation._task_month(task)
                for task in fixture.plan.pit_tasks[19:]
            ]
            pit = {
                "months_expected": 73,
                "months_reused": 19,
                "months_newly_observed": 0,
                "months_total_observed": 19,
                "missing_months": missing,
                "snapshot_count": 19,
                "union_instrument_count": 0,
                "coverage_status": "BLOCKED_PIT_SOURCE_COVERAGE",
            }
            with (
                mock.patch.object(
                    continuation, "_load_parent_reuse_stage", return_value=stage
                ),
                mock.patch.object(
                    continuation,
                    "_load_network_process_marker",
                    return_value=marker,
                ),
                mock.patch.object(
                    continuation,
                    "_current_runtime_bundle",
                    return_value=fixture.runtime,
                ),
                mock.patch.object(
                    continuation,
                    "_validate_first_continuation_response",
                    return_value=first_response,
                ),
                mock.patch.object(
                    continuation,
                    "_load_first_response_evidence",
                    return_value={"evidence_sha256": SHA_D},
                ),
                mock.patch.object(
                    continuation,
                    "_validate_terminal_failure_evidence",
                    return_value=None,
                ),
            ):
                receipt, _ = continuation.publish_continuation_receipt(
                    context=context,
                    terminal_stage="PIT",
                    terminal_status="BLOCKED_PIT_SOURCE_COVERAGE",
                    generated_at="2026-08-31T01:04:00+08:00",
                    continuation_actual_request_count_by_endpoint=counts,
                    completed_request_fingerprint_count=19,
                    remaining_blockers=["pit_source_coverage_incomplete"],
                    first_continuation_response=first_response,
                    pit=pit,
                    terminal_failure_evidence=None,
                )
            validate_json_schema(receipt, continuation.RECEIPT_SCHEMA_PATH)
            self.assertEqual(receipt["parent_reuse_stage_sha256"], SHA_A)
            self.assertEqual(receipt["network_process_marker_sha256"], SHA_B)
            self.assertEqual(receipt["network_process_count"], 1)
            self.assertEqual(receipt["resumed_from_month"], "2019-07")
            self.assertEqual(receipt["minimum_transport_interval_seconds"], "12")
            self.assertEqual(receipt["first_continuation_response"], first_response)
            self.assertEqual(
                receipt["first_continuation_response_evidence_sha256"], SHA_D
            )
            self.assertEqual(receipt["pit"], pit)
            self.assertFalse(receipt["safety"]["real_money_list_allowed"])

    def test_terminal_status_enum_is_exact(self) -> None:
        schema = json.loads(continuation.RECEIPT_SCHEMA_PATH.read_text("utf-8"))
        self.assertEqual(
            set(schema["properties"]["terminal_status"]["enum"]),
            set(continuation.TERMINAL_STATUSES),
        )
        self.assertNotIn("BLOCKED_HISTORY", continuation.TERMINAL_STATUSES)
        self.assertNotIn("BLOCKED_ALPHA_ENGINE", continuation.TERMINAL_STATUSES)

    def test_data_unavailable_terminal_depends_on_stage(self) -> None:
        self.assertEqual(
            continuation._terminal_status_for_classification(
                "DATA_UNAVAILABLE", terminal_stage="PIT"
            ),
            "BLOCKED_PIT_SOURCE_COVERAGE",
        )
        self.assertEqual(
            continuation._terminal_status_for_classification(
                "DATA_UNAVAILABLE", terminal_stage="HISTORY"
            ),
            "BLOCKED_DATA",
        )

    def test_receipt_projection_must_equal_create_only_safe_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ContinuationFixture(Path(directory))
            context = fixture.prepare()
            task = fixture.plan.pit_tasks[19]
            safe = {
                "business_code": 0,
                "classification": None,
                "sanitized_msg": "accepted",
                "msg_sha256": SHA_A,
                "detail_type": None,
                "safe_detail_projection": None,
                "detail_sha256": None,
                "request_id_sha256": None,
                "raw_transport_sha256": SHA_B,
                "response_body_sha256": SHA_B,
                "response_byte_count": 123,
                "sanitized_params": dict(task.params),
                "requested_fields": list(task.fields),
                "requested_at": "2026-08-31T01:02:01+08:00",
                "completed_at": "2026-08-31T01:02:02+08:00",
                "retry_after_seconds": None,
            }
            sidecar = continuation._self_hash(
                {
                    "schema_version": "tushare-alpha-feasibility-continuation-first-response.v1",
                    "published_at": "2026-08-31T01:02:03+08:00",
                    "continuation_run_id": context.claim["continuation_run_id"],
                    "continuation_claim_sha256": context.claim["claim_sha256"],
                    "network_process_marker_sha256": SHA_C,
                    "task_id": task.task_id,
                    "attempt_number": 2,
                    "safe_response_semantics": safe,
                    "retry_performed": False,
                    "result": "FIRST_ATTEMPT_SUCCEEDED",
                    "evidence_artifact_sha256": SHA_D,
                    "locked_test_status": dict(continuation.LOCKED_TEST_STATUS),
                    "locked_test_consumed": False,
                },
                "evidence_sha256",
            )
            validate_json_schema(
                sidecar, continuation.FIRST_RESPONSE_EVIDENCE_SCHEMA_PATH
            )
            projection = {
                "task_id": task.task_id,
                "attempt_number": 2,
                "business_code": 0,
                "classification": None,
                "sanitized_msg": "accepted",
                "detail_type": None,
                "safe_detail_projection": None,
                "msg_sha256": SHA_A,
                "detail_sha256": None,
                "request_id_sha256": None,
                "raw_transport_sha256": SHA_B,
                "response_body_sha256": SHA_B,
                "response_byte_count": 123,
                "requested_at": safe["requested_at"],
                "completed_at": safe["completed_at"],
                "retry_after_seconds": None,
                "retry_performed": False,
                "result": "FIRST_ATTEMPT_SUCCEEDED",
                "evidence_artifact_sha256": SHA_D,
            }
            with mock.patch.object(
                continuation, "_load_first_response_evidence", return_value=sidecar
            ):
                self.assertEqual(
                    continuation._validate_first_continuation_response(
                        context,
                        projection,
                        {endpoint: 1 for endpoint in reporting.ALLOWED_ENDPOINTS},
                    ),
                    projection,
                )
                tampered = dict(projection)
                tampered["sanitized_msg"] = "caller-overwrite"
                with self.assertRaisesRegex(
                    continuation.P15ContinuationError,
                    "first_continuation_response_binding_invalid",
                ):
                    continuation._validate_first_continuation_response(
                        context,
                        tampered,
                        {endpoint: 1 for endpoint in reporting.ALLOWED_ENDPOINTS},
                    )

    def test_success_sidecar_replays_only_store_validated_raw(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ContinuationFixture(Path(directory))
            context = fixture.prepare()
            task = fixture.plan.pit_tasks[19]
            store = data_lane.CreateOnlyTaskStore(fixture.child)
            for path, value in (
                (store.started_path(task), data_lane._started_payload(task, recoverable=True)),
                (store.attempt_path(task, 1), data_lane._attempt_payload(task, 1)),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data_lane.canonical_json_bytes(value))
            raw = json.dumps(
                {
                    "code": 0,
                    "msg": None,
                    "data": {
                        "fields": list(task.fields),
                        "items": [["000906.SH", "000001.SZ", "20190731", "100"]],
                    },
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            requested = datetime(2026, 8, 31, 1, 2, 1, tzinfo=timezone.utc)
            completed = datetime(2026, 8, 31, 1, 2, 2, tzinfo=timezone.utc)
            moments = iter((requested, completed))
            store.execute(
                task,
                token="A" * 16,
                transport=lambda **_kwargs: raw,
                timeout_seconds=30,
                maximum_response_bytes=data_lane.MAXIMUM_RESPONSE_BYTES,
                recover_interrupted_attempts=True,
                maximum_attempts_per_fingerprint=3,
                persist_full_raw_transport=True,
                clock=lambda: next(moments),
            )
            safe = data_lane.extract_safe_response_semantics(
                raw,
                task=task,
                token=None,
                requested_at=requested,
                completed_at=completed,
            )
            marker = {
                "started_at": "2026-08-31T01:02:00+00:00",
                "marker_sha256": SHA_A,
            }
            with mock.patch.object(
                continuation,
                "_load_network_process_marker",
                return_value=marker,
            ):
                sidecar, _ = (
                    continuation.publish_first_continuation_response_evidence(
                        context=context,
                        safe_response_semantics=safe,
                        retry_performed=False,
                        result="FIRST_ATTEMPT_SUCCEEDED",
                        published_at="2026-08-31T01:02:03+00:00",
                    )
                )
            validate_json_schema(
                sidecar, continuation.FIRST_RESPONSE_EVIDENCE_SCHEMA_PATH
            )
            self.assertEqual(
                sidecar["safe_response_semantics"], safe.to_dict()
            )
            self.assertEqual(
                sidecar["evidence_artifact_sha256"],
                continuation._artifact_sha(
                    store.response_path(task), "test_response_hash"
                ),
            )


if __name__ == "__main__":
    unittest.main()
