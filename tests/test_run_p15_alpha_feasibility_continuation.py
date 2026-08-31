from __future__ import annotations

from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from operations import p15_alpha_feasibility_continuation as continuation
from operations import run_p15_alpha_feasibility_continuation as runner
from research.market_data import tushare_alpha_feasibility as data_lane
from research.strategy_workspace import alpha_feasibility_reporting as reporting


@lru_cache(maxsize=1)
def _task() -> data_lane.CollectionTask:
    plan = data_lane.load_config_and_build_plan(reporting.P15_CONFIG_PATH)
    task = plan.pit_tasks[continuation.SUCCESSFUL_PREFIX_COUNT]
    if task.task_id != continuation.FIRST_UNFINISHED_TASK_ID:
        raise AssertionError("frozen first continuation task drifted")
    return task


class _Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 31, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        current = self.current
        self.current += timedelta(seconds=1)
        return current


class P15ContinuationRunnerTests(unittest.TestCase):
    def _verified_parent(self) -> Path:
        parent = (data_lane.REPOSITORY_ROOT / runner.DEFAULT_PARENT_ROOT).resolve()
        if not (parent / continuation.PARENT_RECEIPT_FILENAME).is_file():
            self.skipTest("sealed P1.5 parent fixture is unavailable")
        return parent

    def test_transport_guard_requires_exact_first_fingerprint_and_cutoff(self) -> None:
        calls: list[tuple[str, dict[str, str]]] = []

        def transport(**kwargs):
            calls.append((kwargs["endpoint"], dict(kwargs["params"])))
            return data_lane.TushareHttpResponse(200, b"{}")

        capture = runner._FirstTransportCapture(
            transport=transport,
            first_task=_task(),
            clock=_Clock(),
        )
        with self.assertRaisesRegex(
            runner.P15ContinuationWorkflowError,
            "continuation_first_request_fingerprint_invalid",
        ):
            capture(
                endpoint="index_weight",
                params={
                    "index_code": "000906.SH",
                    "start_date": "20190801",
                    "end_date": "20190831",
                },
                fields=_task().fields,
                token="not-logged",
                timeout_seconds=1,
                maximum_response_bytes=10,
            )
        self.assertEqual(calls, [])

        capture = runner._FirstTransportCapture(
            transport=transport,
            first_task=_task(),
            clock=_Clock(),
        )
        response = capture(
            endpoint="index_weight",
            params=_task().params,
            fields=_task().fields,
            token="not-logged",
            timeout_seconds=1,
            maximum_response_bytes=10,
        )
        self.assertEqual(response.http_status, 200)
        self.assertEqual(calls, [("index_weight", dict(_task().params))])
        self.assertIsNotNone(capture.first_requested_at)
        self.assertIsNotNone(capture.first_completed_at)

        with self.assertRaisesRegex(
            runner.P15ContinuationWorkflowError,
            "continuation_post_cutoff_request_rejected",
        ):
            capture(
                endpoint="daily",
                params={"ts_code": "000001.SZ", "start_date": "20240101"},
                fields=("ts_code", "trade_date"),
                token="not-logged",
                timeout_seconds=1,
                maximum_response_bytes=10,
            )
        self.assertEqual(len(calls), 1)

    def test_continuation_counts_subtract_only_carried_parent_attempt(self) -> None:
        parent = {
            endpoint: (20 if endpoint == "index_weight" else 0)
            for endpoint in reporting.ALLOWED_ENDPOINTS
        }
        child = {
            "trade_cal": 4,
            "index_weight": 56,
            "daily": 7,
            "adj_factor": 8,
            "index_daily": 9,
            "suspend_d": 10,
        }
        context = SimpleNamespace(
            child_root=Path("unused"),
            plan=SimpleNamespace(plan_sha256="2" * 64),
            parent_actual_request_count_by_endpoint=parent,
        )
        with mock.patch.object(
            data_lane,
            "actual_tushare_request_count_by_endpoint",
            return_value=child,
        ):
            new, cumulative = runner._continuation_counts(context)
        self.assertEqual(new["index_weight"], 55)
        self.assertEqual(cumulative["index_weight"], 75)
        self.assertEqual(new["daily"], 7)
        self.assertEqual(cumulative["daily"], 7)

    def test_pit_summary_preserves_reuse_and_new_observation_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = {
                "pit_months_expected": 73,
                "pit_months_observed": 21,
                "missing_months": ["2019-09"],
                "pit_snapshot_count": 21,
                "union_instrument_count": 0,
                "stage_status": "BLOCKED_PIT_MEMBERSHIP",
            }
            manifest["manifest_sha256"] = data_lane.canonical_sha256(manifest)
            path = root / continuation.PARENT_PIT_MANIFEST_FILENAME
            path.write_text(json.dumps(manifest), encoding="utf-8")
            summary, digest = runner._pit_summary(root)
        self.assertEqual(summary["months_reused"], 19)
        self.assertEqual(summary["months_newly_observed"], 2)
        self.assertEqual(summary["months_total_observed"], 21)
        self.assertEqual(summary["coverage_status"], "BLOCKED_PIT_SOURCE_COVERAGE")
        self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_data_terminal_maps_business_semantics_without_numeric_code(self) -> None:
        base = {"stage_status": "BLOCKED_PIT_SOURCE_COVERAGE", "pit_months_observed": 19}
        cases = {
            "RATE_LIMITED": "BLOCKED_UPSTREAM_RATE_LIMIT",
            "PERMISSION_DENIED": "BLOCKED_PROVIDER_PERMISSION",
            "INVALID_PARAMETER": "BLOCKED_INVALID_PARAMETER",
            "UPSTREAM_SERVER_ERROR": "BLOCKED_UPSTREAM_SERVER",
            "ACCOUNT_OR_QUOTA_LIMIT": "BLOCKED_PROVIDER_QUOTA",
            "UPSTREAM_UNKNOWN_ERROR": "BLOCKED_UPSTREAM_UNDOCUMENTED_CODE",
            "DATA_UNAVAILABLE": "BLOCKED_PIT_SOURCE_COVERAGE",
        }
        for classification, expected in cases.items():
            with self.subTest(classification=classification):
                terminal, stage = runner._data_terminal(
                    base,
                    {
                        "endpoint": "index_weight",
                        "business_error_classification": classification,
                        "upstream_code": 40204,
                    },
                )
                self.assertEqual(terminal, expected)
                self.assertEqual(stage, "PIT")
        terminal, stage = runner._data_terminal(
            base,
            {
                "endpoint": "index_weight",
                "business_error_classification": None,
                "failure_code": "https_transport_failed",
            },
        )
        self.assertEqual(terminal, "BLOCKED_DATA")
        self.assertEqual(stage, "PIT")

    def test_nonzero_first_response_is_published_from_real_business_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            child = Path(directory)
            task = _task()
            store = data_lane.CreateOnlyTaskStore(child)
            for path, value in (
                (
                    store.started_path(task),
                    data_lane._started_payload(task, recoverable=True),
                ),
                (store.attempt_path(task, 1), data_lane._attempt_payload(task, 1)),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data_lane.canonical_json_bytes(value))
            raw = json.dumps(
                {
                    "code": 40204,
                    "msg": "not authorized",
                    "data": None,
                    "request_id": "request-id-is-hashed-not-projected",
                    "detail": {"message": "permission denied", "remaining": 0},
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            requested = datetime(2026, 8, 31, 0, 0, 1, tzinfo=timezone.utc)
            completed = datetime(2026, 8, 31, 0, 0, 2, tzinfo=timezone.utc)
            moments = iter((requested, completed))
            with (
                mock.patch.object(data_lane, "_path_is_within_data_tmp", return_value=True),
                self.assertRaisesRegex(
                    data_lane.AlphaFeasibilityDataError,
                    "upstream_permission_error",
                ),
            ):
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
            context = SimpleNamespace(
                child_root=child,
                plan=SimpleNamespace(pit_tasks=tuple([task] * 20)),
                claim={
                    "continuation_run_id": "child-run",
                    "claim_sha256": "c" * 64,
                },
            )
            capture = runner._FirstTransportCapture(
                transport=mock.Mock(), first_task=task, clock=_Clock()
            )
            marker = {
                "started_at": "2026-08-31T00:00:00+00:00",
                "marker_sha256": "d" * 64,
            }
            with mock.patch.object(
                continuation, "_load_network_process_marker", return_value=marker
            ):
                evidence = runner._publish_first_response(
                    context=context,
                    capture=capture,
                    clock=lambda: datetime(
                        2026, 8, 31, 0, 0, 3, tzinfo=timezone.utc
                    ),
                )
            safe = evidence["safe_response_semantics"]
            self.assertEqual(safe["business_code"], 40204)
            self.assertEqual(safe["classification"], "PERMISSION_DENIED")
            self.assertEqual(safe["response_byte_count"], len(raw))
            self.assertNotEqual(safe["request_id_sha256"], None)
            self.assertFalse(evidence["retry_performed"])
            self.assertEqual(evidence["result"], "NOT_RETRYABLE")
            self.assertTrue(
                (child / continuation.FIRST_RESPONSE_EVIDENCE_FILENAME).is_file()
            )

    def test_transport_and_adapter_failures_publish_null_semantic_sidecars(self) -> None:
        cases = (
            (
                "transport",
                lambda **_kwargs: (_ for _ in ()).throw(
                    data_lane.AlphaFeasibilityDataError("https_transport_failed")
                ),
                "NO_RESPONSE_TRANSPORT_INTERRUPTION",
                "https_transport_failed",
                None,
            ),
            (
                "adapter",
                lambda **_kwargs: b"not-json",
                "RESPONSE_REJECTED_ADAPTER_PROTOCOL",
                None,
                len(b"not-json"),
            ),
            (
                "http-status",
                lambda **_kwargs: (_ for _ in ()).throw(
                    data_lane.AlphaFeasibilityDataError(
                        "http_status_not_success"
                    )
                ),
                "RESPONSE_REJECTED_ADAPTER_PROTOCOL",
                "http_status_not_success",
                None,
            ),
            (
                "redirect",
                lambda **_kwargs: (_ for _ in ()).throw(
                    data_lane.AlphaFeasibilityDataError(
                        "http_redirect_forbidden"
                    )
                ),
                "RESPONSE_REJECTED_ADAPTER_PROTOCOL",
                "http_redirect_forbidden",
                None,
            ),
        )
        for label, transport, expected_result, expected_failure, expected_size in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                child = Path(directory)
                task = _task()
                store = data_lane.CreateOnlyTaskStore(child)
                for path, value in (
                    (
                        store.started_path(task),
                        data_lane._started_payload(task, recoverable=True),
                    ),
                    (
                        store.attempt_path(task, 1),
                        data_lane._attempt_payload(task, 1),
                    ),
                ):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data_lane.canonical_json_bytes(value))
                moments = iter(
                    (
                        datetime(2026, 8, 31, 0, 0, 1, tzinfo=timezone.utc),
                        datetime(2026, 8, 31, 0, 0, 2, tzinfo=timezone.utc),
                    )
                )
                capture = runner._FirstTransportCapture(
                    transport=transport,
                    first_task=task,
                    clock=lambda: next(moments),
                )
                with self.assertRaises(data_lane.AlphaFeasibilityDataError):
                    store.execute(
                        task,
                        token="A" * 16,
                        transport=capture,
                        timeout_seconds=30,
                        maximum_response_bytes=data_lane.MAXIMUM_RESPONSE_BYTES,
                        recover_interrupted_attempts=True,
                        maximum_attempts_per_fingerprint=3,
                        persist_full_raw_transport=True,
                        terminalize_transport_interruptions=True,
                    )
                quarantine = json.loads(
                    store.quarantine_path(task).read_text(encoding="utf-8")
                )
                if expected_failure is not None:
                    self.assertEqual(quarantine["failure_code"], expected_failure)
                else:
                    self.assertIn(
                        quarantine["failure_code"],
                        data_lane.ADAPTER_PROTOCOL_FAILURES,
                    )
                context = SimpleNamespace(
                    child_root=child,
                    plan=SimpleNamespace(pit_tasks=tuple([task] * 20)),
                    claim={
                        "continuation_run_id": f"{label}-child-run",
                        "claim_sha256": "e" * 64,
                    },
                )
                marker = {
                    "started_at": "2026-08-31T00:00:00+00:00",
                    "marker_sha256": "f" * 64,
                }
                with mock.patch.object(
                    continuation, "_load_network_process_marker", return_value=marker
                ):
                    evidence = runner._publish_first_response(
                        context=context,
                        capture=capture,
                        clock=lambda: datetime(
                            2026, 8, 31, 0, 0, 3, tzinfo=timezone.utc
                        ),
                    )
                    projection = runner._first_response_summary(evidence)
                    counts = {
                        endpoint: (1 if endpoint == "index_weight" else 0)
                        for endpoint in reporting.ALLOWED_ENDPOINTS
                    }
                    self.assertEqual(
                        continuation._validate_first_continuation_response(
                            context, projection, counts
                        ),
                        projection,
                    )
                safe = evidence["safe_response_semantics"]
                self.assertEqual(evidence["result"], expected_result)
                self.assertIsNone(safe["business_code"])
                self.assertIsNone(safe["classification"])
                self.assertIsNone(safe["sanitized_msg"])
                self.assertEqual(safe["response_byte_count"], expected_size)
                self.assertEqual(
                    safe["raw_transport_sha256"], safe["response_body_sha256"]
                )
                self.assertTrue(
                    (child / continuation.FIRST_RESPONSE_EVIDENCE_FILENAME).is_file()
                )

    def test_empty_response_body_preserves_zero_byte_count_in_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            child = Path(directory)
            task = _task()
            store = data_lane.CreateOnlyTaskStore(child)
            for path, value in (
                (
                    store.started_path(task),
                    data_lane._started_payload(task, recoverable=True),
                ),
                (store.attempt_path(task, 1), data_lane._attempt_payload(task, 1)),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data_lane.canonical_json_bytes(value))
            moments = iter(
                (
                    datetime(2026, 8, 31, 0, 0, 1, tzinfo=timezone.utc),
                    datetime(2026, 8, 31, 0, 0, 2, tzinfo=timezone.utc),
                )
            )
            capture = runner._FirstTransportCapture(
                transport=lambda **_kwargs: b"",
                first_task=task,
                clock=lambda: next(moments),
            )
            with self.assertRaisesRegex(
                data_lane.AlphaFeasibilityDataError, "invalid_response_json"
            ):
                store.execute(
                    task,
                    token="A" * 16,
                    transport=capture,
                    timeout_seconds=30,
                    maximum_response_bytes=data_lane.MAXIMUM_RESPONSE_BYTES,
                    recover_interrupted_attempts=True,
                    maximum_attempts_per_fingerprint=3,
                    persist_full_raw_transport=True,
                    terminalize_transport_interruptions=True,
                )
            context = SimpleNamespace(
                child_root=child,
                plan=SimpleNamespace(pit_tasks=tuple([task] * 20)),
                claim={
                    "continuation_run_id": "empty-body-child-run",
                    "claim_sha256": "1" * 64,
                },
            )
            marker = {
                "started_at": "2026-08-31T00:00:00+00:00",
                "marker_sha256": "2" * 64,
            }
            with mock.patch.object(
                continuation, "_load_network_process_marker", return_value=marker
            ):
                evidence = runner._publish_first_response(
                    context=context,
                    capture=capture,
                    clock=lambda: datetime(
                        2026, 8, 31, 0, 0, 3, tzinfo=timezone.utc
                    ),
                )
            self.assertEqual(
                evidence["result"], "RESPONSE_REJECTED_ADAPTER_PROTOCOL"
            )
            self.assertEqual(
                evidence["safe_response_semantics"]["response_byte_count"], 0
            )

    def test_public_continuation_requires_marker_and_single_process_permit(self) -> None:
        parent = self._verified_parent()
        with tempfile.TemporaryDirectory(dir=data_lane.DATA_TMP_ROOT) as directory:
            child = Path(directory)
            clock = _Clock()
            clock.current = datetime(2026, 8, 31, 6, 0, tzinfo=timezone.utc)
            context = continuation.prepare_continuation(
                parent_root=parent,
                child_root=child,
                config_path=reporting.P15_CONFIG_PATH,
                continuation_run_id="marker-gate-child-run",
                prepared_at=clock(),
            )
            continuation.stage_parent_reuse(context=context, staged_at=clock())
            transport = mock.Mock()
            with self.assertRaisesRegex(
                data_lane.AlphaFeasibilityDataError,
                "continuation_network_process_marker_missing",
            ):
                data_lane.run_parent_reuse_continuation_backfill(
                    reporting.P15_CONFIG_PATH,
                    child,
                    "A" * 16,
                    transport=transport,
                    generated_at=clock(),
                    sleeper=lambda _seconds: None,
                    clock=clock,
                )
            transport.assert_not_called()

            marker, _ = continuation.start_network_process(
                context=context,
                network_process_id="marker-gate-process",
                started_at=clock(),
            )
            plan = data_lane.validate_parent_reuse_continuation_child(
                reporting.P15_CONFIG_PATH,
                child,
            )
            data_lane._consume_parent_reuse_continuation_network_process(
                plan,
                child,
                repository_root=data_lane.REPOSITORY_ROOT,
            )
            with self.assertRaisesRegex(
                data_lane.AlphaFeasibilityDataError,
                "continuation_network_process_authorization_missing",
            ):
                data_lane._consume_parent_reuse_continuation_network_process(
                    plan,
                    child,
                    repository_root=data_lane.REPOSITORY_ROOT,
                )
            self.assertRegex(marker["marker_sha256"], r"^[0-9a-f]{64}$")

    def test_public_run_backfill_rejects_boolean_continuation_bypass(self) -> None:
        transport = mock.Mock()
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(
            data_lane.AlphaFeasibilityDataError,
            "invalid_continuation_execution_setting",
        ):
            data_lane.run_backfill(
                reporting.P15_CONFIG_PATH,
                directory,
                "A" * 16,
                transport=transport,
                _continuation_execution=True,
            )
        transport.assert_not_called()

    def test_fully_resigned_parent_runtime_chain_cannot_replace_exact_parent(self) -> None:
        source = self._verified_parent()
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "resigned-parent"
            parent.mkdir()
            names = (
                continuation.PARENT_CLAIM_FILENAME,
                continuation.PARENT_RECEIPT_FILENAME,
                continuation.PARENT_REPORT_FILENAME,
                continuation.PARENT_PIT_COVERAGE_FILENAME,
                continuation.PARENT_PIT_MANIFEST_FILENAME,
            )
            for name in names:
                (parent / name).write_bytes((source / name).read_bytes())

            claim = json.loads(
                (parent / continuation.PARENT_CLAIM_FILENAME).read_text("utf-8")
            )
            receipt = json.loads(
                (parent / continuation.PARENT_RECEIPT_FILENAME).read_text("utf-8")
            )
            report = json.loads(
                (parent / continuation.PARENT_REPORT_FILENAME).read_text("utf-8")
            )
            forged_reporting_sha = "0" * 64
            runtime = dict(receipt["runtime_implementation_bundle"])
            runtime["files"] = dict(runtime["files"])
            runtime["files"][
                "research/strategy_workspace/alpha_feasibility_reporting.py"
            ] = forged_reporting_sha
            runtime_unsigned = dict(runtime)
            runtime_unsigned.pop("bundle_sha256")
            runtime["bundle_sha256"] = data_lane.canonical_sha256(runtime_unsigned)

            report["reporting_gate_source_sha256"] = forged_reporting_sha
            report_unsigned = dict(report)
            report_unsigned.pop("report_sha256")
            report["report_sha256"] = reporting.canonical_sha256(report_unsigned)

            claim["runtime_implementation_bundle"] = runtime
            claim_unsigned = dict(claim)
            claim_unsigned.pop("claim_sha256")
            claim["claim_sha256"] = data_lane.canonical_sha256(claim_unsigned)

            receipt["runtime_implementation_bundle"] = runtime
            receipt["run_claim_sha256"] = claim["claim_sha256"]
            receipt["report_sha256"] = report["report_sha256"]
            receipt_unsigned = dict(receipt)
            receipt_unsigned.pop("receipt_sha256")
            receipt["receipt_sha256"] = data_lane.canonical_sha256(
                receipt_unsigned
            )
            for name, value in (
                (continuation.PARENT_CLAIM_FILENAME, claim),
                (continuation.PARENT_RECEIPT_FILENAME, receipt),
                (continuation.PARENT_REPORT_FILENAME, report),
            ):
                (parent / name).write_bytes(data_lane.canonical_json_bytes(value))

            with self.assertRaisesRegex(
                continuation.P15ContinuationError,
                "parent_exact_run_anchor_mismatch",
            ):
                continuation._validate_parent_evidence(
                    parent,
                    reporting.P15_CONFIG_PATH,
                )

    def test_real_workflow_seals_adapter_rejections_into_report_and_receipt(self) -> None:
        parent = self._verified_parent()
        nonzero_body = json.dumps(
            {
                "code": 40204,
                "msg": "permission denied",
                "data": None,
                "detail": {"message": "permission denied"},
            },
            separators=(",", ":"),
        ).encode("utf-8")
        cases = (
            ("invalid-json", b"not-json", "invalid_response_json", len(b"not-json")),
            ("empty-body", b"", "invalid_response_json", 0),
            (
                "non-200-body",
                data_lane.TushareHttpResponse(503, nonzero_body),
                "http_status_not_success",
                len(nonzero_body),
            ),
            (
                "non-200-boundary",
                data_lane.AlphaFeasibilityDataError("http_status_not_success"),
                "http_status_not_success",
                None,
            ),
            (
                "redirect-boundary",
                data_lane.AlphaFeasibilityDataError("http_redirect_forbidden"),
                "http_redirect_forbidden",
                None,
            ),
        )
        parent_evidence = continuation._validate_parent_evidence(
            parent,
            reporting.P15_CONFIG_PATH,
        )
        current_runtime = continuation._current_runtime_bundle(
            reporting.P15_CONFIG_PATH,
            parent_evidence.experiment,
        )
        immutable = continuation._immutable_strategy_bundle(
            parent_evidence.experiment,
            current_runtime,
        )
        with (
            mock.patch.object(
                continuation,
                "_validate_parent_evidence",
                return_value=parent_evidence,
            ),
            mock.patch.object(
                continuation,
                "_current_runtime_bundle",
                return_value=current_runtime,
            ),
            mock.patch.object(
                continuation,
                "_immutable_strategy_bundle",
                return_value=immutable,
            ),
        ):
            for label, outcome, failure_code, expected_byte_count in cases:
                with self.subTest(label=label), tempfile.TemporaryDirectory(
                    dir=data_lane.DATA_TMP_ROOT
                ) as directory:
                    child = Path(directory)
                    calls: list[str] = []

                    def transport(**kwargs):
                        calls.append(kwargs["endpoint"])
                        if isinstance(outcome, Exception):
                            raise outcome
                        return outcome

                    clock = _Clock()
                    clock.current = datetime(
                        2026, 8, 31, 6, 0, tzinfo=timezone.utc
                    )
                    exit_code, summary = runner.run_workflow(
                        parent_root=parent,
                        output_root=child,
                        continuation_run_id=f"{label}-workflow-child",
                        network_process_id=f"{label}-workflow-process",
                        generated_at="2026-08-31T05:00:00+00:00",
                        environ={"TUSHARE_TOKEN": "A" * 16},
                        transport=transport,
                        sleeper=lambda _seconds: None,
                        monotonic=lambda: 0.0,
                        clock=clock,
                    )
                    receipt = json.loads(
                        (
                            child / continuation.CONTINUATION_RECEIPT_FILENAME
                        ).read_text(encoding="utf-8")
                    )
                    report = json.loads(
                        (child / reporting.REPORT_FILENAME).read_text(
                            encoding="utf-8"
                        )
                    )
                    first = receipt["first_continuation_response"]
                    terminal = receipt["terminal_failure_evidence"]
                    self.assertEqual(exit_code, 1)
                    self.assertEqual(calls, ["index_weight"])
                    self.assertEqual(
                        summary["terminal_status"], "BLOCKED_ADAPTER_PROTOCOL"
                    )
                    self.assertEqual(
                        receipt["terminal_status"], "BLOCKED_ADAPTER_PROTOCOL"
                    )
                    self.assertEqual(
                        report["terminal_status"], "BLOCKED_ADAPTER_PROTOCOL"
                    )
                    self.assertEqual(
                        first["result"], "RESPONSE_REJECTED_ADAPTER_PROTOCOL"
                    )
                    self.assertIsNone(first["business_code"])
                    self.assertIsNone(first["classification"])
                    self.assertIsNone(first["sanitized_msg"])
                    self.assertIsNone(first["detail_type"])
                    self.assertEqual(
                        first["response_byte_count"], expected_byte_count
                    )
                    self.assertEqual(terminal["failure_code"], failure_code)
                    self.assertIsNone(terminal["classification"])
                    self.assertEqual(
                        receipt["continuation_actual_request_count_by_endpoint"][
                            "index_weight"
                        ],
                        1,
                    )
                    self.assertEqual(receipt["pit"]["months_total_observed"], 19)
                    self.assertFalse(receipt["locked_test_consumed"])
                    self.assertEqual(
                        receipt["locked_test_status"], runner.LOCKED_TEST_STATUS
                    )

    def test_retry_transport_failure_preserves_attempt_two_safe_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            child = Path(directory)
            task = _task()
            store = data_lane.CreateOnlyTaskStore(child)
            for path, value in (
                (
                    store.started_path(task),
                    data_lane._started_payload(task, recoverable=True),
                ),
                (store.attempt_path(task, 1), data_lane._attempt_payload(task, 1)),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data_lane.canonical_json_bytes(value))
            raw = json.dumps(
                {
                    "code": 40204,
                    "msg": "访问频率过高",
                    "data": None,
                    "detail": {"remaining": 0},
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            first_requested = datetime(2026, 8, 31, 0, 0, 1, tzinfo=timezone.utc)
            first_completed = datetime(2026, 8, 31, 0, 0, 2, tzinfo=timezone.utc)
            first_moments = iter((first_requested, first_completed))
            with (
                mock.patch.object(data_lane, "_path_is_within_data_tmp", return_value=True),
                self.assertRaisesRegex(
                    data_lane.AlphaFeasibilityDataError,
                    "upstream_rate_limit_error",
                ),
            ):
                store.execute(
                    task,
                    token="A" * 16,
                    transport=lambda **_kwargs: raw,
                    timeout_seconds=30,
                    maximum_response_bytes=data_lane.MAXIMUM_RESPONSE_BYTES,
                    recover_interrupted_attempts=True,
                    maximum_attempts_per_fingerprint=3,
                    persist_full_raw_transport=True,
                    defer_retryable_business_errors=True,
                    clock=lambda: next(first_moments),
                )
            retry_moments = iter(
                (
                    first_completed + timedelta(seconds=66),
                    first_completed + timedelta(seconds=67),
                )
            )
            with self.assertRaisesRegex(
                data_lane.AlphaFeasibilityDataError, "https_transport_failed"
            ):
                data_lane.retry_business_error_once(
                    store,
                    task,
                    token="A" * 16,
                    transport=lambda **_kwargs: (_ for _ in ()).throw(
                        data_lane.AlphaFeasibilityDataError(
                            "https_transport_failed"
                        )
                    ),
                    timeout_seconds=30,
                    maximum_response_bytes=data_lane.MAXIMUM_RESPONSE_BYTES,
                    maximum_attempts_per_fingerprint=3,
                    terminalize_transport_interruptions=True,
                    clock=lambda: next(retry_moments),
                    sleeper=lambda _seconds: None,
                )
            context = SimpleNamespace(
                child_root=child,
                plan=SimpleNamespace(pit_tasks=tuple([task] * 20)),
                claim={
                    "continuation_run_id": "retry-child-run",
                    "claim_sha256": "1" * 64,
                },
            )
            marker = {
                "started_at": "2026-08-31T00:00:00+00:00",
                "marker_sha256": "2" * 64,
            }
            with mock.patch.object(
                continuation, "_load_network_process_marker", return_value=marker
            ):
                evidence = runner._publish_first_response(
                    context=context,
                    capture=runner._FirstTransportCapture(
                        transport=mock.Mock(), first_task=task, clock=_Clock()
                    ),
                    clock=lambda: first_completed + timedelta(seconds=68),
                )
            safe = evidence["safe_response_semantics"]
            self.assertEqual(safe["classification"], "RATE_LIMITED")
            self.assertEqual(safe["business_code"], 40204)
            self.assertTrue(evidence["retry_performed"])
            self.assertEqual(evidence["result"], "RETRY_FAILED")
            self.assertEqual(
                json.loads(store.quarantine_path(task).read_text("utf-8"))[
                    "terminal_attempt_number"
                ],
                3,
            )

    def test_run_workflow_orders_offline_stage_marker_then_data_runner(self) -> None:
        events: list[str] = []
        task = _task()
        plan = SimpleNamespace(
            pit_tasks=tuple([task] * 20),
            config={"source": {"token_environment_variable": "TUSHARE_TOKEN"}},
            plan_sha256="3" * 64,
        )
        context = SimpleNamespace(
            plan=plan,
            experiment={},
            child_root=Path("child"),
            claim={},
            parent_actual_request_count_by_endpoint={
                endpoint: 0 for endpoint in reporting.ALLOWED_ENDPOINTS
            },
        )
        first_evidence = {"safe_response_semantics": {}}
        report = {"terminal_status": "BLOCKED_DATA"}
        receipt = {
            "terminal_status": "BLOCKED_DATA",
            "continuation_run_id": "child-run",
            "parent": {"network_run_id": "parent-run"},
            "reused_successful_fingerprint_count": 19,
            "continuation_actual_request_count_by_endpoint": {
                endpoint: (1 if endpoint == "index_weight" else 0)
                for endpoint in reporting.ALLOWED_ENDPOINTS
            },
            "resumed_from_month": "2019-07",
            "minimum_transport_interval_seconds": "12",
            "first_continuation_response": {
                "business_code": 0,
                "classification": None,
                "sanitized_msg": "",
                "detail_type": None,
                "safe_detail_projection": None,
                "msg_sha256": "a" * 64,
                "detail_sha256": None,
                "request_id_sha256": None,
                "response_body_sha256": "b" * 64,
                "response_byte_count": 123,
                "retry_performed": False,
                "result": "FIRST_ATTEMPT_SUCCEEDED",
            },
            "pit": {
                "months_expected": 73,
                "months_reused": 19,
                "months_newly_observed": 0,
                "months_total_observed": 19,
                "missing_months": [],
                "snapshot_count": 19,
                "union_instrument_count": 0,
                "coverage_status": "BLOCKED_PIT_SOURCE_COVERAGE",
            },
            "development_metrics": None,
            "validation_metrics": None,
            "concentration_metrics": None,
            "locked_test_status": dict(runner.LOCKED_TEST_STATUS),
            "locked_test_consumed": False,
            "remaining_blockers": ["blocked_data"],
        }

        def prepared(**kwargs):
            events.append("prepare")
            return context

        def staged(**kwargs):
            events.append("stage")
            return {}, Path("stage")

        def marked(**kwargs):
            events.append("marker")
            return {}, Path("marker")

        def preflight(*args, **kwargs):
            events.append("preflight")
            return plan

        def validate_token(value):
            events.append("token")
            self.assertEqual(value, "process-only-token")
            return value

        def data_run(**kwargs):
            events.append("data")
            self.assertEqual(kwargs["token"], "process-only-token")
            self.assertEqual(
                events[:6],
                ["prepare", "stage", "preflight", "token", "marker", "data"],
            )
            return {"stage_status": "BLOCKED_DATA"}

        counts = {
            endpoint: (1 if endpoint == "index_weight" else 0)
            for endpoint in reporting.ALLOWED_ENDPOINTS
        }
        with (
            mock.patch.object(continuation, "prepare_continuation", side_effect=prepared),
            mock.patch.object(continuation, "stage_parent_reuse", side_effect=staged),
            mock.patch.object(
                data_lane,
                "validate_parent_reuse_continuation_child",
                side_effect=preflight,
            ),
            mock.patch.object(
                data_lane,
                "validate_tushare_token_for_process",
                side_effect=validate_token,
            ),
            mock.patch.object(continuation, "start_network_process", side_effect=marked),
            mock.patch.object(
                data_lane,
                "run_parent_reuse_continuation_backfill",
                side_effect=data_run,
            ),
            mock.patch.object(runner, "_publish_first_response", return_value=first_evidence),
            mock.patch.object(runner, "_first_response_summary", return_value={}),
            mock.patch.object(runner, "_continuation_counts", return_value=(counts, counts)),
            mock.patch.object(
                runner,
                "_pit_summary",
                return_value=(
                    {
                        "months_expected": 73,
                        "months_reused": 19,
                        "months_newly_observed": 0,
                        "months_total_observed": 19,
                        "missing_months": [],
                        "snapshot_count": 19,
                        "union_instrument_count": 0,
                        "coverage_status": "BLOCKED_PIT_SOURCE_COVERAGE",
                    },
                    "c" * 64,
                ),
            ),
            mock.patch.object(
                runner,
                "_report_for_result",
                return_value=(report, "BLOCKED_DATA", "HISTORY", ["blocked_data"]),
            ),
            mock.patch.object(runner, "_terminal_quarantine", return_value=(None, None)),
            mock.patch.object(runner, "_completed_fingerprint_count", return_value=19),
            mock.patch.object(
                continuation,
                "publish_continuation_receipt",
                return_value=(receipt, Path("receipt.json")),
            ) as publish,
        ):
            exit_code, summary = runner.run_workflow(
                parent_root=Path("parent"),
                output_root=Path("child"),
                continuation_run_id="child-run",
                network_process_id="network-once",
                environ={"TUSHARE_TOKEN": "process-only-token"},
                transport=mock.Mock(),
                clock=_Clock(),
            )
        self.assertEqual(exit_code, 1)
        self.assertEqual(summary["terminal_status"], "BLOCKED_DATA")
        self.assertEqual(
            events,
            ["prepare", "stage", "preflight", "token", "marker", "data"],
        )
        self.assertEqual(
            publish.call_args.kwargs["continuation_actual_request_count_by_endpoint"],
            counts,
        )

    def test_missing_token_and_preflight_failure_do_not_consume_marker(self) -> None:
        task = _task()
        plan = SimpleNamespace(
            pit_tasks=tuple([task] * 20),
            config={"source": {"token_environment_variable": "TUSHARE_TOKEN"}},
            plan_sha256="4" * 64,
        )
        context = SimpleNamespace(plan=plan)
        with (
            mock.patch.object(
                continuation, "prepare_continuation", return_value=context
            ),
            mock.patch.object(
                continuation, "stage_parent_reuse", return_value=({}, Path("stage"))
            ),
            mock.patch.object(
                data_lane,
                "validate_parent_reuse_continuation_child",
                return_value=plan,
            ),
            mock.patch.object(continuation, "start_network_process") as marker,
            mock.patch.object(
                data_lane, "run_parent_reuse_continuation_backfill"
            ) as data_run,
        ):
            with self.assertRaisesRegex(
                data_lane.AlphaFeasibilityDataError, "credential_preflight_failed"
            ):
                runner.run_workflow(
                    parent_root=Path("parent"),
                    output_root=Path("child"),
                    continuation_run_id="child-run",
                    network_process_id="network-once",
                    environ={},
                    transport=mock.Mock(),
                    clock=_Clock(),
                )
            marker.assert_not_called()
            data_run.assert_not_called()

        with (
            mock.patch.object(
                continuation, "prepare_continuation", return_value=context
            ),
            mock.patch.object(
                continuation, "stage_parent_reuse", return_value=({}, Path("stage"))
            ),
            mock.patch.object(
                data_lane,
                "validate_parent_reuse_continuation_child",
                side_effect=data_lane.AlphaFeasibilityDataError(
                    "continuation_tail_not_pristine"
                ),
            ),
            mock.patch.object(
                data_lane, "validate_tushare_token_for_process"
            ) as token_check,
            mock.patch.object(continuation, "start_network_process") as marker,
        ):
            with self.assertRaisesRegex(
                data_lane.AlphaFeasibilityDataError,
                "continuation_tail_not_pristine",
            ):
                runner.run_workflow(
                    parent_root=Path("parent"),
                    output_root=Path("child"),
                    continuation_run_id="child-run",
                    network_process_id="network-once",
                    environ={"TUSHARE_TOKEN": "not-read"},
                    transport=mock.Mock(),
                    clock=_Clock(),
                )
            token_check.assert_not_called()
            marker.assert_not_called()

    def test_main_never_serializes_provider_text(self) -> None:
        secret = "provider request-id and token must stay hidden"
        failure = data_lane.AlphaFeasibilityDataError(
            "upstream_unknown_error", diagnostic={"provider": secret}
        )
        stderr = io.StringIO()
        with (
            mock.patch.object(runner, "run_workflow", side_effect=failure),
            redirect_stderr(stderr),
        ):
            exit_code = runner.main(
                [
                    "--continuation-run-id",
                    "child-run",
                    "--network-process-id",
                    "network-once",
                ]
            )
        output = stderr.getvalue()
        self.assertEqual(exit_code, 2)
        self.assertNotIn(secret, output)
        self.assertNotIn("provider request-id", output)
        self.assertIn('"error_code": "upstream_unknown_error"', output)
        self.assertIn('"locked_test_consumed": false', output)


if __name__ == "__main__":
    unittest.main()
