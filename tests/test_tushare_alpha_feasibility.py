from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

import research.market_data.tushare_alpha_feasibility as taf
import research.market_data.tushare_index_weight_value_diagnostic as p14d

from research.market_data.tushare_alpha_feasibility import (
    ABSOLUTE_CUTOFF,
    AmbiguousRemoteExecutionError,
    AlphaFeasibilityDataError,
    CollectionTask,
    CreateOnlyTaskStore,
    EXPECTED_FIELDS,
    LOCKED_TEST_STATUS,
    build_history_plan,
    build_pit_membership_artifacts,
    build_total_return_panel,
    canonical_json_bytes,
    canonical_sha256,
    load_config_and_build_plan,
    load_normalized_rows,
    run_backfill,
    run_backfill_from_environment,
    select_pit_snapshot_on_or_before,
    validate_history_coverage,
)
from research.market_data.validation import SchemaValidationError, validate_json_schema


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "a_share_technical_alpha_feasibility.v2.json"
P15_CONFIG = ROOT / "configs" / "a_share_technical_alpha_feasibility.p1_5.json"
NOW = datetime(2026, 8, 28, 1, 2, 3, tzinfo=timezone.utc)
TOKEN = "UnitTestCredentialNeverPersist123456"
_REQUEST_ID_UNSET = object()


def response_bytes(
    endpoint: str,
    rows: list[list[object]],
    *,
    request_id: object = _REQUEST_ID_UNSET,
    extensions: dict[str, object] | None = None,
) -> bytes:
    payload = {
        "code": 0,
        "msg": None,
        "data": {"fields": list(EXPECTED_FIELDS[endpoint]), "items": rows},
    }
    if request_id is not _REQUEST_ID_UNSET:
        payload["request_id"] = request_id
    if extensions:
        payload.update(extensions)
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


class FakeTransport:
    def __init__(self, callback):
        self.callback = callback
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs):
        self.calls.append(dict(kwargs))
        return taf.TushareHttpResponse(
            http_status=200,
            body=self.callback(**kwargs),
        )


class InMemoryCompletedStore:
    def __init__(self, rows_for_task):
        self.rows_for_task = rows_for_task

    def is_complete(self, _task):
        return True

    def _load_started(self, _task):
        return {}

    def _load_response(self, task):
        rows = tuple(MappingProxyType(dict(row)) for row in self.rows_for_task(task))
        row_hash = canonical_sha256([dict(row) for row in rows])
        return taf.TaskExecutionResult(
            task=task,
            rows=rows,
            raw_response_sha256=row_hash,
            replayed=True,
            raw_response_persisted=True,
            isolated_future_delist_date_count=0,
            isolated_non_union_row_count=0,
            wire_response_sha256=row_hash,
            response_artifact_sha256=row_hash,
            transport_receipt=MappingProxyType(
                {
                    "observed_root_fields": ["code", "data", "msg"],
                    "semantic_core_fields": ["code", "data", "msg"],
                    "transport_extension_field_names": [],
                    "transport_extension_type_by_field": {},
                    "transport_extension_value_sha256_by_field": {},
                    "transport_extensions_sha256": hashlib.sha256(b"{}").hexdigest(),
                    "transport_extensions_byte_count": 2,
                    "raw_transport_sha256": row_hash,
                    "token_leak_check": "PASSED",
                }
            ),
        )


class ExplodingEnvironment(dict):
    def __init__(self):
        super().__init__()
        self.accessed = False

    def get(self, key, default=None):
        self.accessed = True
        raise AssertionError("credential environment was inspected")


class TusharePlanTests(unittest.TestCase):
    def test_preflight_builds_exactly_one_index_weight_task_for_each_of_73_months(self):
        plan = load_config_and_build_plan(CONFIG)
        self.assertEqual(len(plan.pit_tasks), 73)
        self.assertEqual(len({task.task_id for task in plan.pit_tasks}), 73)
        self.assertEqual(plan.pit_tasks[0].params["start_date"], "20171201")
        self.assertEqual(plan.pit_tasks[0].params["end_date"], "20171231")
        self.assertEqual(plan.pit_tasks[-1].params["start_date"], "20231201")
        self.assertEqual(plan.pit_tasks[-1].params["end_date"], "20231231")
        self.assertTrue(all(task.endpoint == "index_weight" for task in plan.pit_tasks))
        self.assertEqual(plan.config["index"]["minimum_weight_decimal_places"], 0)
        self.assertTrue(
            all(
                max(task.params.get("start_date", "00000000"), task.params.get("end_date", "00000000"))
                <= "20231231"
                for task in plan.pit_tasks
            )
        )

    def test_config_date_guard_runs_before_credential_environment_lookup(self):
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
        config["requests"]["daily"]["params"]["end_date"] = "20240101"
        environment = ExplodingEnvironment()
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            path = Path(temporary) / "invalid.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(AlphaFeasibilityDataError, "post_cutoff_config_date"):
                run_backfill_from_environment(
                    path,
                    Path(temporary) / "output",
                    environ=environment,
                )
        self.assertFalse(environment.accessed)

    def test_history_plan_uses_configured_batches_and_only_union_instruments(self):
        plan = load_config_and_build_plan(CONFIG)
        instruments = ["000001.SZ", "600000.SH", "600001.SH", "600002.SH"]
        tasks = build_history_plan(plan, instruments)
        daily = [task for task in tasks if task.endpoint == "daily"]
        factors = [task for task in tasks if task.endpoint == "adj_factor"]
        suspensions = [task for task in tasks if task.endpoint == "suspend_d"]
        self.assertEqual([len(task.scope_instruments) for task in daily], [3, 1])
        self.assertEqual([len(task.scope_instruments) for task in factors], [1, 1, 1, 1])
        self.assertEqual([len(task.scope_instruments) for task in suspensions], [3, 1])
        self.assertNotIn("stock_basic", {task.endpoint for task in tasks})
        self.assertEqual(
            {task.endpoint for task in tasks},
            {"trade_cal", "daily", "adj_factor", "suspend_d", "index_daily"},
        )
        self.assertEqual(len(tasks), 10)

    def test_stock_basic_request_count_is_structurally_zero(self):
        plan = load_config_and_build_plan(CONFIG)
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            counts = taf.actual_tushare_request_count_by_endpoint(
                temporary,
                (*plan.pit_tasks, *build_history_plan(plan, ["600000.SH"])),
                plan_sha256=plan.plan_sha256,
            )
        self.assertEqual(set(counts), set(taf.ALLOWED_ENDPOINTS))
        self.assertNotIn("stock_basic", counts)
        self.assertEqual(sum(counts.values()), 0)

    def test_stock_basic_zero_rejects_boolean_alias(self):
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
        config["stock_basic_request_count"] = False
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            path = Path(temporary) / "bool-count.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaises(AlphaFeasibilityDataError):
                load_config_and_build_plan(path)


class TushareResponseEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.plan = load_config_and_build_plan(CONFIG)
        history = build_history_plan(self.plan, ["600000.SH"])
        self.task = next(task for task in history if task.endpoint == "trade_cal")
        self.rows = [["SSE", "20170701", 0, "20170630"]]

    def _validate(self, raw: bytes):
        return taf.validate_response_bytes(self.task, raw, token=TOKEN, http_status=200)

    def _payload(self, **changes):
        payload = {
            "code": 0,
            "msg": None,
            "data": {
                "fields": list(EXPECTED_FIELDS["trade_cal"]),
                "items": self.rows,
            },
        }
        payload.update(changes)
        return json.dumps(payload, separators=(",", ":")).encode("utf-8")

    def _assert_data_failure(self, raw: bytes, category: str):
        with self.assertRaises(AlphaFeasibilityDataError) as raised:
            self._validate(raw)
        self.assertEqual(raised.exception.code, "data_payload_invalid")
        self.assertEqual(
            raised.exception.diagnostic["data_failure_category"], category
        )
        return raised.exception

    def _validate_suspend(self, rows: list[list[object]]):
        task = next(
            task
            for task in build_history_plan(self.plan, ["600000.SH"])
            if task.endpoint == "suspend_d"
        )
        return taf.validate_response_bytes(
            task,
            response_bytes("suspend_d", rows),
            token=TOKEN,
            http_status=200,
        )

    def test_suspend_same_day_stop_and_resume_are_distinct_events(self):
        validated = self._validate_suspend(
            [
                ["600000.SH", "20231229", None, "S"],
                ["600000.SH", "20231229", None, "R"],
            ]
        )
        self.assertEqual(len(validated.rows), 2)
        state = taf._aggregate_suspension_events(validated.rows)[
            ("600000.SH", "20231229")
        ]
        self.assertTrue(state["full_session_suspended"])
        self.assertTrue(state["resume_event_present"])
        self.assertEqual(state["unique_event_count"], 2)

    def test_suspend_same_day_distinct_timings_are_retained(self):
        validated = self._validate_suspend(
            [
                ["600000.SH", "20231229", None, "S"],
                ["600000.SH", "20231229", "09:30-10:30", "S"],
            ]
        )
        state = taf._aggregate_suspension_events(validated.rows)[
            ("600000.SH", "20231229")
        ]
        self.assertEqual(len(validated.rows), 2)
        self.assertTrue(state["full_session_suspended"])
        self.assertTrue(state["partial_session_suspended"])
        self.assertEqual(state["unique_event_count"], 2)

    def test_suspend_exact_duplicates_fold_and_count_after_timing_normalization(self):
        validated = self._validate_suspend(
            [
                ["600000.SH", "20231229", " 09:30-10:30 ", "S"],
                ["600000.SH", "20231229", "09:30-10:30", "S"],
            ]
        )
        self.assertEqual(len(validated.rows), 1)
        self.assertEqual(validated.rows[0]["suspend_timing"], "09:30-10:30")
        self.assertEqual(validated.rows[0]["exact_duplicate_count"], 1)
        state = taf._aggregate_suspension_events(validated.rows)[
            ("600000.SH", "20231229")
        ]
        self.assertEqual(state["source_event_count"], 2)
        self.assertEqual(state["unique_event_count"], 1)
        self.assertEqual(state["exact_duplicate_count"], 1)

    def test_semantic_core_and_controlled_extension_shapes_pass(self):
        cases = (
            ({}, (), {}),
            ({"request_id": "req-201712-abc123"}, ("request_id",), {"request_id": "string"}),
            ({"detail": "provider detail"}, ("detail",), {"detail": "string"}),
            ({"detail": {"status": "ok", "attempt": 1}}, ("detail",), {"detail": "object"}),
            ({"detail": ["ok", 1, None]}, ("detail",), {"detail": "array"}),
            (
                {"request_id": "req-pair", "detail": {"status": "ok"}},
                ("detail", "request_id"),
                {"detail": "object", "request_id": "string"},
            ),
            (
                {"trace_id": "trace-2025-01-02-future-extension"},
                ("trace_id",),
                {"trace_id": "string"},
            ),
        )
        normalized_hash = None
        for extensions, expected_names, expected_types in cases:
            with self.subTest(extensions=extensions):
                validated = self._validate(self._payload(**extensions))
                self.assertEqual(validated.semantic_core_fields, ("code", "data", "msg"))
                self.assertEqual(
                    validated.observed_root_fields,
                    tuple(sorted({"code", "msg", "data", *extensions})),
                )
                self.assertEqual(
                    validated.transport_extension_field_names, expected_names
                )
                self.assertEqual(
                    dict(validated.transport_extension_type_by_field), expected_types
                )
                self.assertRegex(validated.transport_extensions_sha256, r"^[0-9a-f]{64}$")
                if normalized_hash is None:
                    normalized_hash = validated.normalized_content_sha256
                self.assertEqual(validated.normalized_content_sha256, normalized_hash)

    def test_missing_semantic_core_and_core_types_fail_closed(self):
        for missing_field in ("code", "msg", "data"):
            payload = json.loads(self._payload(trace_id="safe-extension").decode("utf-8"))
            payload.pop(missing_field)
            raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            with self.subTest(missing_field=missing_field):
                with self.assertRaises(AlphaFeasibilityDataError) as raised:
                    self._validate(raw)
                self.assertEqual(raised.exception.code, "semantic_core_missing")
                self.assertEqual(
                    raised.exception.diagnostic["missing_semantic_core_fields"],
                    [missing_field],
                )

        invalid = (
            (self._payload(code=True), "bool code"),
            (self._payload(msg=7), "non-string msg"),
            (self._payload(data=None), "null success data"),
            (self._payload(code=-1, msg="permission denied", data=[]), "array error data"),
            (b"[]", "non-object root"),
        )
        for raw, label in invalid:
            with self.subTest(label=label):
                with self.assertRaises(AlphaFeasibilityDataError) as raised:
                    self._validate(raw)
                self.assertEqual(raised.exception.code, "semantic_core_type_invalid")

    def test_duplicate_json_keys_and_malformed_json_fail_closed(self):
        with self.assertRaises(AlphaFeasibilityDataError) as duplicate:
            self._validate(b'{"code":0,"code":0,"msg":null,"data":{}}')
        self.assertEqual(duplicate.exception.code, "duplicate_json_key")
        with self.assertRaises(AlphaFeasibilityDataError) as malformed:
            self._validate(b'{"code":0')
        self.assertEqual(malformed.exception.code, "unknown_non_json_value")
        self.assertIn("invalid_response_json", taf.ADAPTER_PROTOCOL_FAILURES)

    def test_nonzero_code_is_classified_and_extensions_do_not_bypass_it(self):
        classifications = (
            (2002, "opaque upstream text", {}, "UPSTREAM_UNKNOWN_ERROR", "unknown"),
            (2002, "corporate serverless concatenated quotable parameterized", {}, "UPSTREAM_UNKNOWN_ERROR", "unknown"),
            (-1, "积分不足 quota", {}, "PERMISSION_DENIED", "permission"),
            (-1, "每分钟访问次数受限且没有权限", {}, "RATE_LIMITED", "rate_limit"),
            (-1, "权限", {}, "PERMISSION_DENIED", "permission"),
            (-1, "额度不足", {"note": "访问过快"}, "RATE_LIMITED", "rate_limit"),
            (-1, "日期格式错误", {"note": "该日期无数据"}, "INVALID_PARAMETER", "invalid_parameter"),
            (-1, "参数", {}, "INVALID_PARAMETER", "invalid_parameter"),
            (-1, "该日期无数据", {"note": "服务异常"}, "DATA_UNAVAILABLE", "data_unavailable"),
            (-1, "系统错误", {"note": "总量限制"}, "UPSTREAM_SERVER_ERROR", "server_internal"),
            (-1, "请求总量已达", {}, "ACCOUNT_OR_QUOTA_LIMIT", "authentication_account"),
            (-1, "总量", {}, "ACCOUNT_OR_QUOTA_LIMIT", "authentication_account"),
            (-1, "额度", {}, "ACCOUNT_OR_QUOTA_LIMIT", "authentication_account"),
        )
        for code, message, detail, classification, category in classifications:
            with self.subTest(classification=classification):
                raw = self._payload(
                    code=code,
                    msg=message,
                    data={"reason": "controlled error object"},
                    request_id="error-request",
                    detail=detail,
                    trace_id="future-extension",
                )
                with self.assertRaises(AlphaFeasibilityDataError) as raised:
                    self._validate(raw)
                self.assertEqual(
                    raised.exception.code,
                    taf._BUSINESS_CLASSIFICATION_TO_FAILURE_CODE[classification],
                )
                self.assertEqual(raised.exception.diagnostic["upstream_code"], code)
                self.assertEqual(
                    raised.exception.diagnostic["upstream_error_category"], category
                )
                self.assertEqual(
                    raised.exception.diagnostic["business_error_classification"],
                    classification,
                )
                diagnostic = json.dumps(
                    dict(raised.exception.diagnostic),
                    sort_keys=True,
                    ensure_ascii=False,
                )
                self.assertNotIn("controlled error object", diagnostic)
                self.assertNotIn("error-request", diagnostic)

    def test_safe_response_semantics_preserves_bounded_nonsecret_detail(self):
        detail = {
            "limit": 100,
            "remaining": 0,
            "reset": "2023-12-31",
            "retry_after": 30,
            "nested": [{"message": "访问频率过高"}],
        }
        raw = self._payload(
            code=-1,
            msg="访问过快",
            data=None,
            request_id="request-id-must-not-persist-in-evidence",
            detail=detail,
        )
        evidence = taf.extract_safe_response_semantics(
            raw,
            task=self.task,
            token=TOKEN,
            requested_at=NOW,
            completed_at=NOW + timedelta(seconds=1),
        )
        self.assertEqual(evidence.classification, "RATE_LIMITED")
        self.assertEqual(evidence.retry_after_seconds, 30)
        projection = evidence.safe_detail_projection
        self.assertEqual(projection["value"], detail)
        self.assertEqual(projection["semantic_category"], "RATE_LIMITED")
        serialized = json.dumps(evidence.to_dict(), ensure_ascii=False)
        self.assertNotIn("request-id-must-not-persist-in-evidence", serialized)
        self.assertEqual(
            evidence.response_body_sha256, hashlib.sha256(raw).hexdigest()
        )
        self.assertEqual(
            evidence.raw_transport_sha256, evidence.response_body_sha256
        )
        self.assertEqual(
            evidence.request_id_sha256,
            hashlib.sha256(
                taf._canonical_transport_json_bytes(
                    "request-id-must-not-persist-in-evidence"
                )
            ).hexdigest(),
        )

        success = taf.extract_safe_response_semantics(
            self._payload(detail={"limit": 100}),
            task=self.task,
            token=TOKEN,
            requested_at=NOW,
            completed_at=NOW,
        )
        self.assertEqual(success.business_code, 0)
        self.assertIsNone(success.classification)
        self.assertEqual(success.safe_detail_projection["value"], {"limit": 100})

    def test_safe_detail_projection_rejects_secret_future_and_bounds(self):
        cases = (
            (
                self._payload(
                    code=-1,
                    msg="server error",
                    data=None,
                    detail={"nested": {"password": "must-not-survive"}},
                ),
                "transport_extension_secret_detected",
            ),
            (
                self._payload(
                    code=-1,
                    msg="server error 2025-01-02",
                    data=None,
                    detail={"limit": 1},
                ),
                "post_cutoff_response_date",
            ),
            (
                self._payload(
                    code=-1,
                    msg="server error",
                    data=None,
                    detail=[0] * (taf.MAXIMUM_SAFE_DETAIL_PROJECTION_ELEMENTS + 1),
                ),
                "safe_detail_projection_too_large",
            ),
        )
        for raw, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(AlphaFeasibilityDataError, expected):
                    taf.extract_safe_response_semantics(
                        raw,
                        task=self.task,
                        token=TOKEN,
                        requested_at=NOW,
                        completed_at=NOW,
                    )

    def test_data_internal_contract_keeps_stable_outer_code_and_precise_subclass(self):
        invalid_payloads = (
            (
                self._payload(data={"fields": list(self.task.fields), "items": self.rows, "extra": 1}),
                "data_required_value_invalid",
            ),
            (
                self._payload(data={"fields": "exchange,cal_date", "items": self.rows}),
                "data_fields_not_array",
            ),
            (
                self._payload(
                    data={"fields": ["exchange", "cal_date", "", "pretrade_date"], "items": self.rows}
                ),
                "data_field_name_invalid",
            ),
            (
                self._payload(
                    data={
                        "fields": ["exchange", "cal_date", "is_open", "is_open"],
                        "items": self.rows,
                    }
                ),
                "data_duplicate_fields",
            ),
            (
                self._payload(
                    data={
                        "fields": ["exchange", "cal_date", "is_open"],
                        "items": [["SSE", "20170701", 0]],
                    }
                ),
                "data_required_fields_missing",
            ),
            (
                self._payload(data={"fields": list(self.task.fields), "items": {}}),
                "data_item_width_mismatch",
            ),
            (
                self._payload(data={"fields": list(self.task.fields), "items": [["SSE"]]}),
                "data_item_width_mismatch",
            ),
            (
                self._payload(data={"fields": list(self.task.fields), "items": self.rows * 2}),
                "duplicate_response_primary_key",
            ),
        )
        for raw, category in invalid_payloads:
            with self.subTest(category=category):
                self._assert_data_failure(raw, category)

    def test_secret_keys_and_token_values_fail_without_persisting_original_values(self):
        secret_cases = (
            ({"secret": "top-level-secret-value"}, "top-level-secret-value"),
            ({"Token": "mixed-case-token-value"}, "mixed-case-token-value"),
            (
                {"detail": {"credential": "nested-credential-value"}},
                "nested-credential-value",
            ),
        )
        for extensions, forbidden in secret_cases:
            with self.subTest(extensions=tuple(extensions)):
                with self.assertRaises(AlphaFeasibilityDataError) as raised:
                    self._validate(self._payload(**extensions))
                self.assertEqual(
                    raised.exception.code, "transport_extension_secret_detected"
                )
                serialized = json.dumps(
                    dict(raised.exception.diagnostic), sort_keys=True
                )
                self.assertNotIn(forbidden, serialized)
                self.assertNotIn(TOKEN, serialized)

        raw = self._payload(detail={"note": TOKEN})
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            store = CreateOnlyTaskStore(temporary)
            with self.assertRaises(AlphaFeasibilityDataError) as raised:
                store.execute(
                    self.task,
                    token=TOKEN,
                    transport=FakeTransport(lambda **_kwargs: raw),
                    timeout_seconds=1,
                    maximum_response_bytes=taf.MAXIMUM_RESPONSE_BYTES,
                )
            self.assertEqual(
                raised.exception.code, "transport_extension_secret_detected"
            )
            self.assertFalse(store.raw_path(self.task).exists())
            self.assertFalse(store.response_path(self.task).exists())
            quarantine = store.quarantine_path(self.task).read_bytes()
            self.assertNotIn(TOKEN.encode("utf-8"), quarantine)
            evidence = json.loads(quarantine)
            self.assertIsNone(evidence["raw_transport_sha256"])
            self.assertEqual(evidence["token_leak_check"], "RAW_LEAK_REJECTED")

    def test_nan_infinity_control_characters_and_invalid_json_values_are_rejected(self):
        lone_surrogate = self._payload(detail="safe").replace(
            b'"detail":"safe"', b'"detail":"\\ud800"'
        )
        invalid = (
            self._payload(detail=float("nan")),
            self._payload(detail=float("inf")),
            self._payload(detail=float("-inf")),
            self._payload(detail="control\nvalue"),
            self._payload(**{"bad\nfield": "value"}),
            lone_surrogate,
        )
        for raw in invalid:
            with self.subTest(raw=raw[-40:]):
                with self.assertRaises(AlphaFeasibilityDataError) as raised:
                    self._validate(raw)
                self.assertEqual(raised.exception.code, "unknown_non_json_value")

    def test_high_precision_decimal_extensions_keep_distinct_hash_identity(self):
        numbers = (
            b"0.1234567890123456789012345678901",
            b"0.1234567890123456789012345678902",
        )
        validated = []
        for number in numbers:
            raw = self._payload(detail=0).replace(b'"detail":0', b'"detail":' + number)
            validated.append(self._validate(raw))
        self.assertNotEqual(
            validated[0].transport_extension_value_sha256_by_field["detail"],
            validated[1].transport_extension_value_sha256_by_field["detail"],
        )
        self.assertNotEqual(
            validated[0].transport_extensions_sha256,
            validated[1].transport_extensions_sha256,
        )
        self.assertEqual(
            validated[0].normalized_content_sha256,
            validated[1].normalized_content_sha256,
        )

    def test_extreme_json_nesting_has_stable_protocol_error(self):
        depth = 1_100
        deeply_nested = self._payload(detail=0).replace(
            b'"detail":0',
            b'"detail":' + (b"[" * depth) + b"0" + (b"]" * depth),
        )
        with self.assertRaises(AlphaFeasibilityDataError) as raised:
            self._validate(deeply_nested)
        # This stdlib build parses 1,100 levels, so the controlled extension
        # depth guard wins. A parser RecursionError is separately normalized
        # to unknown_non_json_value by strict_json_loads.
        self.assertEqual(raised.exception.code, "transport_extensions_too_deep")

    def test_response_and_extension_byte_limits_are_inclusive(self):
        response_limit = taf.MAXIMUM_RESPONSE_BYTES
        empty_message = self._payload(msg="")
        exact = self._payload(msg="m" * (response_limit - len(empty_message)))
        self.assertEqual(len(exact), response_limit)
        self._validate(exact)
        with self.assertRaises(AlphaFeasibilityDataError) as oversized_response:
            self._validate(exact + b" ")
        self.assertEqual(
            oversized_response.exception.code, "response_body_too_large"
        )

        extension_limit = taf.MAXIMUM_TRANSPORT_EXTENSIONS_BYTES
        empty_values = {field: "" for field in ("a", "b", "c", "d")}
        overhead = len(
            json.dumps(
                empty_values, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        )
        remaining = extension_limit - overhead
        lengths = [taf.MAXIMUM_TRANSPORT_EXTENSION_STRING_LENGTH] * 3
        lengths.append(remaining - sum(lengths))
        extensions = {
            field: "x" * length
            for field, length in zip(("a", "b", "c", "d"), lengths)
        }
        accepted = self._validate(self._payload(**extensions))
        self.assertEqual(accepted.transport_extensions_byte_count, extension_limit)
        extensions["d"] += "x"
        with self.assertRaises(AlphaFeasibilityDataError) as oversized_extensions:
            self._validate(self._payload(**extensions))
        self.assertEqual(
            oversized_extensions.exception.code, "transport_extensions_too_large"
        )

    def test_extension_depth_and_field_count_limits_are_inclusive(self):
        def nested_object(depth: int) -> object:
            value: object = "leaf"
            for _ in range(depth):
                value = {"node": value}
            return value

        accepted_depth = self._validate(
            self._payload(detail=nested_object(taf.MAXIMUM_TRANSPORT_EXTENSION_DEPTH))
        )
        self.assertEqual(
            accepted_depth.transport_extension_type_by_field["detail"], "object"
        )
        with self.assertRaises(AlphaFeasibilityDataError) as too_deep:
            self._validate(
                self._payload(
                    detail=nested_object(taf.MAXIMUM_TRANSPORT_EXTENSION_DEPTH + 1)
                )
            )
        self.assertEqual(too_deep.exception.code, "transport_extensions_too_deep")

        fields = {
            f"extension_{index:02d}": index
            for index in range(taf.MAXIMUM_TRANSPORT_EXTENSION_FIELDS)
        }
        accepted_fields = self._validate(self._payload(**fields))
        self.assertEqual(
            len(accepted_fields.transport_extension_field_names),
            taf.MAXIMUM_TRANSPORT_EXTENSION_FIELDS,
        )
        fields["one_more_extension"] = 65
        with self.assertRaises(AlphaFeasibilityDataError) as too_many_fields:
            self._validate(self._payload(**fields))
        self.assertEqual(
            too_many_fields.exception.code, "transport_extensions_too_large"
        )
        too_many_raw = self._payload(**fields)
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            store = CreateOnlyTaskStore(temporary)
            with self.assertRaises(AlphaFeasibilityDataError) as quarantined:
                store.execute(
                    self.task,
                    token=TOKEN,
                    transport=FakeTransport(lambda **_kwargs: too_many_raw),
                    timeout_seconds=1,
                    maximum_response_bytes=taf.MAXIMUM_RESPONSE_BYTES,
                )
            self.assertEqual(
                quarantined.exception.code, "transport_extensions_too_large"
            )
            evidence = json.loads(
                store.quarantine_path(self.task).read_text(encoding="utf-8")
            )
            self.assertEqual(
                evidence["failure_code"], "transport_extensions_too_large"
            )
            self.assertLessEqual(
                len(evidence["transport_extension_field_names"]),
                taf.MAXIMUM_TRANSPORT_EXTENSION_FIELDS,
            )

    def test_extension_element_and_string_limits_are_inclusive(self):
        accepted_elements = self._validate(
            self._payload(
                detail=[0] * (taf.MAXIMUM_TRANSPORT_EXTENSION_ELEMENTS - 1)
            )
        )
        self.assertEqual(
            accepted_elements.transport_extension_type_by_field["detail"], "array"
        )
        with self.assertRaises(AlphaFeasibilityDataError) as too_many_elements:
            self._validate(
                self._payload(detail=[0] * taf.MAXIMUM_TRANSPORT_EXTENSION_ELEMENTS)
            )
        self.assertEqual(
            too_many_elements.exception.code, "transport_extensions_too_large"
        )

        accepted_string = self._validate(
            self._payload(
                detail="x" * taf.MAXIMUM_TRANSPORT_EXTENSION_STRING_LENGTH
            )
        )
        self.assertEqual(
            accepted_string.transport_extension_type_by_field["detail"], "string"
        )
        with self.assertRaises(AlphaFeasibilityDataError) as too_long:
            self._validate(
                self._payload(
                    detail="x"
                    * (taf.MAXIMUM_TRANSPORT_EXTENSION_STRING_LENGTH + 1)
                )
            )
        self.assertEqual(too_long.exception.code, "unknown_non_json_value")

    def test_three_hash_layers_change_independently_and_receipts_omit_values(self):
        extension_sets = (
            {
                "request_id": "req-first-opaque",
                "detail": {"provider_note": "opaque-first-value"},
            },
            {
                "request_id": "req-second-opaque",
                "detail": ["opaque-second-value", 2],
            },
        )
        results = []
        roots = []
        response_artifacts = []
        for extensions in extension_sets:
            raw = response_bytes(
                "trade_cal", self.rows, extensions=dict(extensions)
            )
            root = tempfile.TemporaryDirectory(dir=ROOT)
            self.addCleanup(root.cleanup)
            roots.append(Path(root.name))
            store = CreateOnlyTaskStore(root.name)
            result = store.execute(
                self.task,
                token=TOKEN,
                transport=FakeTransport(lambda **_kwargs: raw),
                timeout_seconds=1,
                maximum_response_bytes=8_388_608,
            )
            results.append(result)
            response_artifacts.append(
                json.loads(store.response_path(self.task).read_text(encoding="utf-8"))
            )
            receipt = dict(result.transport_receipt)
            self.assertEqual(
                set(receipt),
                {
                    "observed_root_fields",
                    "semantic_core_fields",
                    "transport_extension_field_names",
                    "transport_extension_type_by_field",
                    "transport_extension_value_sha256_by_field",
                    "transport_extensions_sha256",
                    "transport_extensions_byte_count",
                    "raw_transport_sha256",
                    "token_leak_check",
                },
            )
            self.assertEqual(
                receipt["transport_extension_field_names"], ["detail", "request_id"]
            )
            artifact_bytes = b"\n".join(
                path.read_bytes()
                for path in Path(root.name).rglob("*")
                if path.is_file()
            )
            detail_value = extensions["detail"]
            detail_text = (
                detail_value["provider_note"]
                if isinstance(detail_value, dict)
                else detail_value[0]
            )
            for original in (extensions["request_id"], detail_text, TOKEN):
                self.assertNotIn(str(original).encode("utf-8"), artifact_bytes)

        self.assertNotEqual(
            results[0].raw_transport_sha256,
            results[1].raw_transport_sha256,
        )
        self.assertNotEqual(
            results[0].transport_extensions_sha256,
            results[1].transport_extensions_sha256,
        )
        self.assertEqual(
            results[0].normalized_content_sha256,
            results[1].normalized_content_sha256,
        )
        expected_normalized_content_sha256 = canonical_sha256(
            {
                "fields": list(self.task.fields),
                "items": [
                    [row[field] for field in self.task.fields]
                    for row in results[0].rows
                ],
            }
        )
        self.assertEqual(
            results[0].normalized_content_sha256,
            expected_normalized_content_sha256,
        )
        self.assertNotEqual(
            results[0].normalized_content_sha256,
            canonical_sha256([dict(row) for row in results[0].rows]),
        )
        self.assertEqual(
            canonical_sha256([dict(row) for row in results[0].rows]),
            canonical_sha256([dict(row) for row in results[1].rows]),
        )
        self.assertEqual(
            results[0].raw_response_sha256,
            results[1].raw_response_sha256,
        )
        self.assertEqual(
            response_artifacts[0]["normalized_rows_sha256"],
            response_artifacts[1]["normalized_rows_sha256"],
        )
        self.assertEqual(
            response_artifacts[0]["normalized_content_sha256"],
            expected_normalized_content_sha256,
        )
        self.assertEqual(
            response_artifacts[0]["normalized_content_sha256"],
            response_artifacts[1]["normalized_content_sha256"],
        )
        self.assertNotEqual(
            results[0].transport_receipt[
                "transport_extension_value_sha256_by_field"
            ]["request_id"],
            results[1].transport_receipt[
                "transport_extension_value_sha256_by_field"
            ]["request_id"],
        )
        normalized = canonical_json_bytes([dict(row) for row in results[0].rows])
        for extensions in extension_sets:
            self.assertNotIn(extensions["request_id"].encode("utf-8"), normalized)
        self.assertNotIn(TOKEN.encode("utf-8"), normalized)

        coverage = taf.HistoryCoverageResult(
            report=MappingProxyType(
                {
                    "daily_coverage_status": "BLOCKED_DATA",
                    "adj_factor_coverage_status": "BLOCKED_DATA",
                    "suspension_coverage_status": "BLOCKED_DATA",
                    "benchmark_coverage_status": "BLOCKED_DATA",
                    "blockers": [{"reason": "test_partial_history"}],
                }
            ),
            passed=False,
            trading_dates=("20170701",),
        )
        pit = taf.PitMembershipResult(
            coverage_report=MappingProxyType({"pit_months_observed": 73}),
            manifest=MappingProxyType(
                {
                    "snapshots": [
                        {
                            "snapshot_date": "2017-12-29",
                            "members": [
                                {"instrument_id": "600000.SH", "weight": "100.000"}
                            ],
                        }
                    ]
                }
            ),
            union_instruments=("600000.SH",),
            passed=True,
        )
        history_manifests = [
            taf.build_history_manifest_from_store(
                self.plan,
                [self.task],
                CreateOnlyTaskStore(root),
                coverage,
                pit_result=pit,
                request_counts={endpoint: 0 for endpoint in taf.ALLOWED_ENDPOINTS},
                generated_at=NOW,
            )
            for root in roots
        ]
        self.assertEqual(
            history_manifests[0]["datasets"]["trade_calendar"][
                "normalized_content_sha256"
            ],
            history_manifests[1]["datasets"]["trade_calendar"][
                "normalized_content_sha256"
            ],
        )
        self.assertEqual(history_manifests[0], history_manifests[1])

    def test_semantic_protocol_failure_stops_after_first_request(self):
        raw = json.dumps(
            {"code": 0, "msg": None, "trace_id": "safe-extension"},
            separators=(",", ":"),
        ).encode("utf-8")
        transport = FakeTransport(lambda **_kwargs: raw)
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            result = run_backfill(
                CONFIG,
                temporary,
                TOKEN,
                transport=transport,
                generated_at=NOW,
                sleeper=lambda _seconds: None,
            )
            pit_evidence = json.loads(
                (Path(temporary) / "pit_membership_coverage_report.json").read_text(
                    encoding="utf-8"
                )
            )
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(result["terminal_status"], "BLOCKED_ADAPTER_PROTOCOL")
        self.assertEqual(result["stage_status"], "BLOCKED_ADAPTER_PROTOCOL")
        self.assertEqual(pit_evidence["terminal_status"], "BLOCKED_ADAPTER_PROTOCOL")
        validate_json_schema(
            pit_evidence,
            ROOT / "schemas" / "pit_membership_coverage_report.v2.json",
        )
        self.assertEqual(result["actual_tushare_request_count_by_endpoint"]["index_weight"], 1)

    def test_first_month_pit_failure_stops_before_remaining_72_requests(self):
        raw = response_bytes(
            "index_weight",
            [["000906.SH", "600000.SH", "20171229", "100.000"]],
        )
        transport = FakeTransport(lambda **_kwargs: raw)
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            result = run_backfill(
                CONFIG,
                temporary,
                TOKEN,
                transport=transport,
                generated_at=NOW,
                sleeper=lambda _seconds: None,
            )

        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(result["stage_status"], "BLOCKED_PIT_MEMBERSHIP")
        self.assertEqual(result["terminal_status"], "BLOCKED_DATA")
        self.assertEqual(
            result["remaining_blockers"],
            ["component_count_requires_controlled_adjustment_evidence"],
        )
        self.assertEqual(
            result["actual_tushare_request_count_by_endpoint"]["index_weight"],
            1,
        )

    def test_valid_first_month_gate_continues_without_repeating_first_request(self):
        first_rows = [
            ["000906.SH", f"{index:06d}.SZ", "20171229", "0.125"]
            for index in range(1, 801)
        ]
        invalid_second = json.dumps(
            {"code": 0, "msg": None},
            separators=(",", ":"),
        ).encode("utf-8")

        def callback(**kwargs):
            if kwargs["params"]["start_date"] == "20171201":
                return response_bytes("index_weight", first_rows)
            return invalid_second

        transport = FakeTransport(callback)
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            result = run_backfill(
                CONFIG,
                temporary,
                TOKEN,
                transport=transport,
                generated_at=NOW,
                sleeper=lambda _seconds: None,
            )

        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(
            [call["params"]["start_date"] for call in transport.calls],
            ["20171201", "20180101"],
        )
        self.assertEqual(result["terminal_status"], "BLOCKED_ADAPTER_PROTOCOL")
        self.assertEqual(
            result["actual_tushare_request_count_by_endpoint"]["index_weight"],
            2,
        )

    def test_request_id_is_excluded_from_normalized_pit_membership_identity(self):
        task = self.plan.pit_tasks[0]
        members = tuple(f"{index:06d}.SZ" for index in range(1, 801))

        def rows_for(pit_task):
            snapshot = datetime.strptime(pit_task.params["end_date"], "%Y%m%d").date()
            while snapshot.weekday() >= 5:
                snapshot -= timedelta(days=1)
            return [
                {
                    "index_code": "000906.SH",
                    "con_code": code,
                    "trade_date": snapshot.strftime("%Y%m%d"),
                    "weight": "0.125",
                }
                for code in members
            ]

        wire_rows = [
            [row[field] for field in task.fields]
            for row in rows_for(task)
        ]
        executions = []
        request_ids = ("pit-request-first", "pit-request-second")
        for request_id in request_ids:
            raw = response_bytes(
                "index_weight",
                wire_rows,
                request_id=request_id,
            )
            root = tempfile.TemporaryDirectory(dir=ROOT)
            self.addCleanup(root.cleanup)
            executions.append(
                CreateOnlyTaskStore(root.name).execute(
                    task,
                    token=TOKEN,
                    transport=FakeTransport(lambda **_kwargs: raw),
                    timeout_seconds=1,
                    maximum_response_bytes=8_388_608,
                )
            )

        manifests = []
        for execution in executions:
            supplied = {
                pit_task.task_id: rows_for(pit_task)
                for pit_task in self.plan.pit_tasks
            }
            supplied[task.task_id] = execution
            manifests.append(
                build_pit_membership_artifacts(
                    self.plan,
                    supplied,
                    generated_at=NOW,
                ).manifest
            )
        self.assertNotEqual(
            executions[0].raw_transport_sha256,
            executions[1].raw_transport_sha256,
        )
        self.assertNotEqual(
            executions[0].transport_extensions_sha256,
            executions[1].transport_extensions_sha256,
        )
        self.assertEqual(
            executions[0].normalized_content_sha256,
            executions[1].normalized_content_sha256,
        )
        self.assertEqual(manifests[0], manifests[1])
        serialized = canonical_json_bytes(manifests[0])
        for request_id in request_ids:
            self.assertNotIn(request_id.encode("utf-8"), serialized)


class TushareIndexWeightSemanticProjectionTests(unittest.TestCase):
    def setUp(self):
        self.plan = load_config_and_build_plan(CONFIG)
        self.task = self.plan.pit_tasks[0]
        self.values = {
            "index_code": "000906.SH",
            "con_code": "600000.SH",
            "trade_date": "20171229",
            "weight": "0.125",
            "index_name": "CSI 800 A",
        }

    def _payload(
        self,
        fields,
        *,
        values=None,
        data_extensions=None,
        root_extensions=None,
        items=None,
    ):
        active = dict(self.values if values is None else values)
        rows = items if items is not None else [[active[field] for field in fields]]
        data = {"fields": list(fields), "items": rows}
        data.update(data_extensions or {})
        payload = {"code": 0, "msg": None, "data": data}
        payload.update(root_extensions or {})
        return json.dumps(payload, separators=(",", ":")).encode("utf-8")

    def _validate(self, raw):
        return taf.validate_response_bytes(
            self.task,
            raw,
            token=TOKEN,
            http_status=200,
        )

    def _assert_category(self, raw, category):
        with self.assertRaises(AlphaFeasibilityDataError) as raised:
            self._validate(raw)
        self.assertEqual(raised.exception.code, "data_payload_invalid")
        self.assertEqual(
            raised.exception.diagnostic["data_failure_category"], category
        )
        return raised.exception

    def test_canonical_reordered_and_extra_fields_project_by_semantic_name(self):
        canonical_fields = list(self.task.fields)
        reordered_fields = ["weight", "index_code", "trade_date", "con_code"]
        extra_fields = [*canonical_fields, "index_name"]
        canonical = self._validate(self._payload(canonical_fields))
        reordered = self._validate(self._payload(reordered_fields))
        extra_a = self._validate(self._payload(extra_fields))
        changed = dict(self.values, index_name="CSI 800 B")
        extra_b = self._validate(self._payload(extra_fields, values=changed))

        expected_row = {
            field: self.values[field] for field in self.task.fields
        }
        for response in (canonical, reordered, extra_a, extra_b):
            self.assertEqual(dict(response.rows[0]), expected_row)
            self.assertEqual(set(response.rows[0]), set(self.task.fields))
            self.assertEqual(response.required_data_fields, self.task.fields)
            self.assertEqual(response.missing_required_data_fields, ())
            self.assertEqual(response.data_row_count, 1)
        self.assertTrue(canonical.field_order_matches_canonical)
        self.assertFalse(reordered.field_order_matches_canonical)
        self.assertTrue(extra_a.field_order_matches_canonical)
        self.assertEqual(extra_a.extra_data_fields, ("index_name",))
        self.assertEqual(
            canonical.normalized_content_sha256,
            reordered.normalized_content_sha256,
        )
        self.assertEqual(
            canonical.normalized_content_sha256,
            extra_a.normalized_content_sha256,
        )
        self.assertEqual(
            extra_a.normalized_content_sha256,
            extra_b.normalized_content_sha256,
        )
        self.assertNotEqual(
            canonical.provider_payload_sha256,
            reordered.provider_payload_sha256,
        )
        self.assertNotEqual(
            canonical.provider_payload_sha256,
            extra_a.provider_payload_sha256,
        )
        self.assertNotEqual(
            extra_a.provider_payload_sha256,
            extra_b.provider_payload_sha256,
        )
        self.assertNotEqual(
            extra_a.extra_data_field_value_sha256_by_field["index_name"],
            extra_b.extra_data_field_value_sha256_by_field["index_name"],
        )

    def test_field_contract_failures_are_precise_and_keep_safe_row_count(self):
        canonical = list(self.task.fields)
        cases = (
            (
                self._payload(
                    canonical[:-1],
                    items=[[self.values[field] for field in canonical[:-1]]],
                ),
                "data_required_fields_missing",
            ),
            (
                self._payload(
                    [*canonical, "weight"],
                    items=[[
                        *[self.values[field] for field in canonical],
                        self.values["weight"],
                    ]],
                ),
                "data_duplicate_fields",
            ),
            (
                self._payload(canonical, items=[["000906.SH"]]),
                "data_item_width_mismatch",
            ),
            (
                self._payload(
                    canonical,
                    data_extensions={"unexpected": "ignored-never"},
                ),
                "data_required_value_invalid",
            ),
            (
                self._payload(
                    canonical,
                    values=dict(self.values, weight="not-a-number"),
                ),
                "data_required_value_invalid",
            ),
        )
        for raw, category in cases:
            with self.subTest(category=category):
                raised = self._assert_category(raw, category)
                self.assertEqual(raised.diagnostic["data_row_count"], 1)
                self.assertEqual(
                    raised.diagnostic["required_data_fields"], canonical
                )

        invalid_name = self._payload(
            ["index_code", "con_code", "trade_date", "token"],
            items=[["000906.SH", "600000.SH", "20171229", "secret-value"]],
        )
        raised = self._assert_category(invalid_name, "data_field_name_invalid")
        serialized = canonical_json_bytes(raised.diagnostic)
        self.assertNotIn(b'"token"', serialized)
        self.assertNotIn(b"secret-value", serialized)
        self.assertRegex(
            raised.diagnostic["observed_data_fields"][-1],
            r"^sha256:[0-9a-f]{64}$",
        )

        invalid_extra = self._payload(
            [*canonical, "bad-field"],
            items=[[
                *[self.values[field] for field in canonical],
                "unsafe-extra-value",
            ]],
        )
        raised = self._assert_category(invalid_extra, "data_field_name_invalid")
        self.assertEqual(raised.diagnostic["missing_required_data_fields"], [])
        self.assertTrue(raised.diagnostic["field_order_matches_canonical"])
        self.assertNotIn(
            b"unsafe-extra-value",
            canonical_json_bytes(raised.diagnostic),
        )

        non_array = {
            "code": 0,
            "msg": None,
            "data": {"fields": "not-an-array", "items": [[1], [2]]},
        }
        raised = self._assert_category(
            json.dumps(non_array, separators=(",", ":")).encode("utf-8"),
            "data_fields_not_array",
        )
        self.assertEqual(raised.diagnostic["data_row_count"], 2)

    def test_only_observed_safe_pagination_metadata_is_accepted(self):
        canonical = list(self.task.fields)
        accepted = self._validate(
            self._payload(
                canonical,
                data_extensions={"has_more": False, "count": 0},
            )
        )
        self.assertEqual(dict(accepted.rows[0])["con_code"], "600000.SH")
        for value, category in (
            (True, "potential_upstream_truncation"),
            (0, "data_required_value_invalid"),
            ("false", "data_required_value_invalid"),
        ):
            with self.subTest(value=value):
                self._assert_category(
                    self._payload(canonical, data_extensions={"has_more": value}),
                    category,
                )
        for value in (True, -1, 1, "0", None):
            with self.subTest(count=value):
                self._assert_category(
                    self._payload(canonical, data_extensions={"count": value}),
                    "data_required_value_invalid",
                )

        without_metadata = self._validate(self._payload(canonical))
        self.assertEqual(
            accepted.normalized_content_sha256,
            without_metadata.normalized_content_sha256,
        )

    def test_reordered_extra_payload_replays_without_network_or_strategy_leak(self):
        fields = ["index_name", "weight", "con_code", "index_code", "trade_date"]
        raw = self._payload(
            fields,
            data_extensions={"has_more": False},
            root_extensions={
                "detail": "safe provider detail",
                "request_id": "semantic-projection-request",
            },
        )
        transport = FakeTransport(lambda **_kwargs: raw)
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            store = CreateOnlyTaskStore(temporary)
            first = store.execute(
                self.task,
                token=TOKEN,
                transport=transport,
                timeout_seconds=1,
                maximum_response_bytes=taf.MAXIMUM_RESPONSE_BYTES,
            )
            never = FakeTransport(
                lambda **_kwargs: self.fail("sealed semantic response must replay")
            )
            replay = store.execute(
                self.task,
                token=TOKEN,
                transport=never,
                timeout_seconds=1,
                maximum_response_bytes=taf.MAXIMUM_RESPONSE_BYTES,
            )
            artifact = json.loads(
                store.response_path(self.task).read_text(encoding="utf-8")
            )
            persisted_provider = json.loads(
                store.raw_path(self.task).read_text(encoding="utf-8")
            )

        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(never.calls, [])
        self.assertTrue(replay.replayed)
        self.assertEqual(first.provider_payload_sha256, replay.provider_payload_sha256)
        self.assertEqual(
            first.normalized_content_sha256,
            replay.normalized_content_sha256,
        )
        self.assertEqual(artifact["schema_version"], taf.RESPONSE_SCHEMA_VERSION)
        self.assertEqual(artifact["observed_data_fields"], fields)
        self.assertEqual(artifact["extra_data_fields"], ["index_name"])
        self.assertNotIn("index_name", artifact["rows"][0])
        self.assertEqual(persisted_provider["data"]["fields"], fields)
        self.assertNotIn("detail", persisted_provider)
        self.assertNotIn("request_id", persisted_provider)

    def test_index_weight_rows_sort_by_full_canonical_primary_key(self):
        fields = list(self.task.fields)
        items = [
            ["000906.SH", "600001.SH", "20171229", "0.125"],
            ["000906.SH", "600000.SH", "20171229", "0.125"],
        ]
        result = self._validate(self._payload(fields, items=items))
        self.assertEqual(
            [row["con_code"] for row in result.rows],
            ["600000.SH", "600001.SH"],
        )

    def test_observed_numeric_weight_scales_normalize_without_row_loss(self):
        fields = list(self.task.fields)
        items = [
            ["000906.SH", "600000.SH", "20171229", 1.2],
            ["000906.SH", "600001.SH", "20171229", 1.23],
            ["000906.SH", "600002.SH", "20171229", 1.234],
        ]
        result = self._validate(self._payload(fields, items=items))
        self.assertEqual(
            [row["weight"] for row in result.rows],
            ["1.2", "1.23", "1.234"],
        )

    def test_index_weight_representation_rules_are_field_specific_and_fail_closed(self):
        fields = list(self.task.fields)
        accepted = (0, 1, "0", "1.2", "1.23", "1.234")
        for offset, weight in enumerate(accepted):
            with self.subTest(accepted=weight):
                values = dict(
                    self.values,
                    con_code=f"{600100 + offset:06d}.SH",
                    weight=weight,
                )
                result = self._validate(self._payload(fields, values=values))
                self.assertEqual(len(result.rows), 1)
                self.assertEqual(result.rows[0]["weight"], str(weight))

        rejected = (
            True,
            None,
            -1,
            -0.0,
            "-0",
            "-0.0",
            "-0.1",
            "+0.125",
            "1e-3",
            "NaN",
            "Infinity",
            "１.２",
            "١.٢",
        )
        for offset, weight in enumerate(rejected):
            with self.subTest(rejected=weight):
                values = dict(
                    self.values,
                    con_code=f"{600200 + offset:06d}.SH",
                    weight=weight,
                )
                self._assert_category(
                    self._payload(fields, values=values),
                    "data_required_value_invalid",
                )

        raw_numeric_forms = (
            (b"-0", None),
            (b"1E+1", "10"),
            (b"0.0000001", "0.0000001"),
        )
        for raw_weight, expected in raw_numeric_forms:
            with self.subTest(raw_weight=raw_weight):
                raw = (
                    b'{"code":0,"msg":null,"data":{"fields":['
                    b'"index_code","con_code","trade_date","weight"],"items":[['
                    b'"000906.SH","600000.SH","20171229",'
                    + raw_weight
                    + b']]}}'
                )
                if expected is None:
                    self._assert_category(raw, "data_required_value_invalid")
                else:
                    result = self._validate(raw)
                    self.assertEqual(result.rows[0]["weight"], expected)

        pathological = (
            b'{"code":0,"msg":null,"data":{"fields":['
            b'"index_code","con_code","trade_date","weight"],"items":[['
            b'"000906.SH","600000.SH","20171229",1e1000000000]]}}'
        )
        self._assert_category(pathological, "data_required_value_invalid")

        for values in (
            dict(self.values, con_code="６０００００.SH"),
            dict(self.values, trade_date="２０１７１２２９"),
        ):
            self._assert_category(
                self._payload(fields, values=values),
                "data_required_value_invalid",
            )

        for trade_date in (20171229, 20171229.0):
            with self.subTest(trade_date=trade_date):
                self._assert_category(
                    self._payload(
                        fields,
                        values=dict(self.values, trade_date=trade_date),
                    ),
                    "data_required_value_invalid",
                )

    def test_observed_mixed_scale_snapshot_uses_each_source_decimal_tolerance(self):
        weights = ["98.1", *(["0.1"] * 5), *(["0.01"] * 67), *(["0.001"] * 727)]
        rows = [
            {
                "index_code": "000906.SH",
                "con_code": f"{600000 + index:06d}.SH",
                "trade_date": "20171229",
                "weight": weight,
            }
            for index, weight in enumerate(weights)
        ]
        total, tolerance = taf._validate_weight_snapshot(rows, expected_count=800)
        self.assertEqual(total, taf.Decimal("99.997"))
        self.assertEqual(tolerance, taf.Decimal("0.05"))

    def test_coarse_zero_weights_cannot_inflate_snapshot_tolerance(self):
        weights = ["0"] * 700 + ["0.1"] * 100
        rows = [
            {
                "index_code": "000906.SH",
                "con_code": f"{600000 + index:06d}.SH",
                "trade_date": "20171229",
                "weight": weight,
            }
            for index, weight in enumerate(weights)
        ]
        with self.assertRaisesRegex(
            taf.AlphaFeasibilityDataError,
            "weight_sum_outside_row_precision_tolerance",
        ):
            taf._validate_weight_snapshot(rows, expected_count=800)

    def test_coarse_positive_weights_cannot_inflate_snapshot_tolerance(self):
        weights = ["0"] * 600 + ["1"] * 200
        rows = [
            {
                "index_code": "000906.SH",
                "con_code": f"{600000 + index:06d}.SH",
                "trade_date": "20171229",
                "weight": weight,
            }
            for index, weight in enumerate(weights)
        ]
        with self.assertRaisesRegex(
            taf.AlphaFeasibilityDataError,
            "weight_sum_outside_row_precision_tolerance",
        ):
            taf._validate_weight_snapshot(rows, expected_count=800)

    def test_high_precision_weight_sum_is_not_rounded_into_tolerance(self):
        weights = [
            "99.999999999999999999999999999301",
            *(["0.000000000000000000000000000001"] * 799),
        ]
        rows = [
            {
                "index_code": "000906.SH",
                "con_code": f"{600000 + index:06d}.SH",
                "trade_date": "20171229",
                "weight": weight,
            }
            for index, weight in enumerate(weights)
        ]
        with self.assertRaisesRegex(
            taf.AlphaFeasibilityDataError,
            "weight_sum_outside_row_precision_tolerance",
        ):
            taf._validate_weight_snapshot(rows, expected_count=800)

    def test_first_failure_summary_uses_precise_safe_field_diagnostic(self):
        fields = ["index_code", "con_code", "trade_date"]
        raw = self._payload(
            fields,
            items=[["000906.SH", "600000.SH", "20171229"]],
            root_extensions={"request_id": "missing-weight-request"},
        )
        transport = FakeTransport(lambda **_kwargs: raw)
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            result = run_backfill(
                CONFIG,
                temporary,
                TOKEN,
                transport=transport,
                generated_at=NOW,
                sleeper=lambda _seconds: None,
            )
            quarantine = json.loads(
                next((Path(temporary) / "quarantine").glob("*.json")).read_text(
                    encoding="utf-8"
                )
            )

        first = result["first_index_weight"]
        self.assertEqual(result["remaining_blockers"], ["data_required_fields_missing"])
        self.assertEqual(result["terminal_status"], "BLOCKED_ADAPTER_PROTOCOL")
        self.assertEqual(first["observed_data_fields"], fields)
        self.assertEqual(first["required_data_fields"], list(self.task.fields))
        self.assertEqual(first["missing_required_data_fields"], ["weight"])
        self.assertEqual(first["extra_data_fields"], [])
        self.assertFalse(first["field_order_matches_canonical"])
        self.assertEqual(first["data_row_count"], 1)
        self.assertRegex(first["provider_payload_sha256"], r"^[0-9a-f]{64}$")
        self.assertIsNone(first["normalized_content_sha256"])
        self.assertEqual(quarantine["data_row_count"], 1)
        self.assertEqual(quarantine["missing_required_data_fields"], ["weight"])
        self.assertNotIn("missing-weight-request", json.dumps(quarantine))


class TushareCreateOnlyTests(unittest.TestCase):
    def setUp(self):
        self.plan = load_config_and_build_plan(CONFIG)
        self.history = build_history_plan(self.plan, ["600000.SH"])

    def _trade_calendar_task(self):
        return next(task for task in self.history if task.endpoint == "trade_cal")

    def _execute(self, store, task, transport):
        source = self.plan.config["source"]
        return store.execute(
            task,
            token=TOKEN,
            transport=transport,
            timeout_seconds=source["request_timeout_seconds"],
            maximum_response_bytes=source["maximum_response_bytes"],
        )

    def _assert_execute_data_failure(self, store, task, transport, category):
        with self.assertRaises(AlphaFeasibilityDataError) as raised:
            self._execute(store, task, transport)
        self.assertEqual(raised.exception.code, "data_payload_invalid")
        self.assertEqual(
            raised.exception.diagnostic["data_failure_category"], category
        )
        return raised.exception

    def test_complete_task_resumes_offline_and_token_never_appears_in_artifacts(self):
        task = self._trade_calendar_task()
        raw = response_bytes(
            "trade_cal",
            [["SSE", "20170701", 0, "20170630"], ["SSE", "20231231", 0, "20231229"]],
        )
        transport = FakeTransport(lambda **_kwargs: raw)
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            store = CreateOnlyTaskStore(temporary)
            first = self._execute(store, task, transport)
            self.assertFalse(first.replayed)
            sealed_response = store.response_path(task).read_bytes()
            sealed_raw = store.raw_path(task).read_bytes()
            never = FakeTransport(lambda **_kwargs: self.fail("network must not run on resume"))
            second = self._execute(store, task, never)
            self.assertTrue(second.replayed)
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(len(never.calls), 0)
            self.assertEqual(store.response_path(task).read_bytes(), sealed_response)
            self.assertEqual(store.raw_path(task).read_bytes(), sealed_raw)
            for path in Path(temporary).rglob("*"):
                if path.is_file():
                    self.assertNotIn(TOKEN.encode("utf-8"), path.read_bytes())
            self.assertEqual(first.raw_response_sha256, hashlib.sha256(raw).hexdigest())

    def test_v2_request_id_only_receipt_replays_read_only_without_rewrite(self):
        task = self._trade_calendar_task()
        rows_on_wire = [["SSE", "20170701", 0, "20170630"]]
        request_id = "sealed-v2-request-id"
        wire = response_bytes("trade_cal", rows_on_wire, request_id=request_id)
        persisted = response_bytes("trade_cal", rows_on_wire)
        rows = [dict(row) for row in taf.validate_response_bytes(task, wire).rows]
        receipt = {
            "http_status": 200,
            "response_byte_count": len(wire),
            "response_body_sha256": hashlib.sha256(wire).hexdigest(),
            "accepted_root_fields": ["code", "data", "msg", "request_id"],
            "request_id_present": True,
            "request_id_sha256": hashlib.sha256(request_id.encode("utf-8")).hexdigest(),
            "token_leak_check": "PASSED",
        }
        sealed = {
            "schema_version": "tushare-alpha-feasibility-task-response.v2",
            "state": "RESPONSE_VALIDATED",
            "task_id": task.task_id,
            "endpoint": task.endpoint,
            "plan_sha256": task.plan_sha256,
            "raw_response_sha256": hashlib.sha256(persisted).hexdigest(),
            "wire_response_sha256": hashlib.sha256(wire).hexdigest(),
            "transport_receipt": receipt,
            "raw_response_persisted": True,
            "normalized_rows_sha256": canonical_sha256(rows),
            "row_count": len(rows),
            "isolated_future_delist_date_count": 0,
            "isolated_non_union_row_count": 0,
            "rows": rows,
            "locked_test_status": dict(LOCKED_TEST_STATUS),
            "locked_test_consumed": False,
        }
        sealed["response_artifact_sha256"] = canonical_sha256(sealed)
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            store = CreateOnlyTaskStore(temporary)
            store.started_path(task).parent.mkdir(parents=True)
            store.raw_path(task).parent.mkdir(parents=True)
            store.started_path(task).write_bytes(
                canonical_json_bytes(taf._started_payload(task))
            )
            store.raw_path(task).write_bytes(persisted)
            store.response_path(task).write_bytes(canonical_json_bytes(sealed))
            before = {
                path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in (
                    store.started_path(task),
                    store.raw_path(task),
                    store.response_path(task),
                )
            }
            never = FakeTransport(
                lambda **_kwargs: self.fail("sealed v2 receipt must not resend")
            )
            replay = self._execute(store, task, never)
            self.assertTrue(replay.replayed)
            self.assertTrue(replay.request_id_present)
            self.assertEqual(replay.raw_transport_sha256, hashlib.sha256(wire).hexdigest())
            self.assertEqual(never.calls, [])
            for path, expected in before.items():
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), expected)

    def test_v3_response_replays_read_only_without_inventing_provider_hash(self):
        task = self._trade_calendar_task()
        raw = response_bytes(
            "trade_cal", [["SSE", "20170701", 0, "20170630"]]
        )
        validated = taf.validate_response_bytes(task, raw)
        rows = [dict(row) for row in validated.rows]
        receipt = {
            "observed_root_fields": ["code", "data", "msg"],
            "semantic_core_fields": ["code", "data", "msg"],
            "transport_extension_field_names": [],
            "transport_extension_type_by_field": {},
            "transport_extension_value_sha256_by_field": {},
            "transport_extensions_sha256": hashlib.sha256(b"{}").hexdigest(),
            "transport_extensions_byte_count": 2,
            "raw_transport_sha256": hashlib.sha256(raw).hexdigest(),
            "token_leak_check": "PASSED",
        }
        sealed = {
            "schema_version": "tushare-alpha-feasibility-task-response.v3",
            "state": "RESPONSE_VALIDATED",
            "task_id": task.task_id,
            "endpoint": task.endpoint,
            "plan_sha256": task.plan_sha256,
            "raw_response_sha256": hashlib.sha256(raw).hexdigest(),
            "wire_response_sha256": hashlib.sha256(raw).hexdigest(),
            "transport_receipt": receipt,
            "raw_response_persisted": True,
            "normalized_rows_sha256": canonical_sha256(rows),
            "normalized_content_sha256": validated.normalized_content_sha256,
            "row_count": len(rows),
            "isolated_future_delist_date_count": 0,
            "isolated_non_union_row_count": 0,
            "rows": rows,
            "locked_test_status": dict(LOCKED_TEST_STATUS),
            "locked_test_consumed": False,
        }
        sealed["response_artifact_sha256"] = canonical_sha256(sealed)
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            store = CreateOnlyTaskStore(temporary)
            store.started_path(task).parent.mkdir(parents=True)
            store.raw_path(task).parent.mkdir(parents=True)
            store.started_path(task).write_bytes(
                canonical_json_bytes(taf._started_payload(task))
            )
            store.raw_path(task).write_bytes(raw)
            store.response_path(task).write_bytes(canonical_json_bytes(sealed))
            before = store.response_path(task).read_bytes()
            replay = self._execute(
                store,
                task,
                FakeTransport(lambda **_kwargs: self.fail("v3 must not resend")),
            )
            self.assertEqual(store.response_path(task).read_bytes(), before)
        self.assertTrue(replay.replayed)
        self.assertIsNone(replay.provider_payload_sha256)
        self.assertEqual(replay.observed_data_fields, task.fields)

    def test_v1_sealed_receipt_is_rejected_read_only_and_never_modified(self):
        task = self._trade_calendar_task()
        raw = response_bytes("trade_cal", [["SSE", "20170701", 0, "20170630"]])
        rows = [dict(row) for row in taf.validate_response_bytes(task, raw).rows]
        legacy = {
            "schema_version": "tushare-alpha-feasibility-task-response.v1",
            "state": "RESPONSE_VALIDATED",
            "task_id": task.task_id,
            "endpoint": task.endpoint,
            "plan_sha256": task.plan_sha256,
            "raw_response_sha256": hashlib.sha256(raw).hexdigest(),
            "wire_response_sha256": hashlib.sha256(raw).hexdigest(),
            "raw_response_persisted": True,
            "normalized_rows_sha256": canonical_sha256(rows),
            "row_count": len(rows),
            "isolated_future_delist_date_count": 0,
            "isolated_non_union_row_count": 0,
            "rows": rows,
            "locked_test_status": dict(LOCKED_TEST_STATUS),
            "locked_test_consumed": False,
        }
        legacy["response_artifact_sha256"] = canonical_sha256(legacy)
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            store = CreateOnlyTaskStore(temporary)
            store.started_path(task).parent.mkdir(parents=True)
            store.raw_path(task).parent.mkdir(parents=True)
            store.started_path(task).write_bytes(canonical_json_bytes(taf._started_payload(task)))
            store.raw_path(task).write_bytes(raw)
            store.response_path(task).write_bytes(canonical_json_bytes(legacy))
            before = {
                path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in (
                    store.started_path(task),
                    store.raw_path(task),
                    store.response_path(task),
                )
            }
            never = FakeTransport(lambda **_kwargs: self.fail("v1 receipt must not resend"))
            with self.assertRaisesRegex(
                AlphaFeasibilityDataError,
                "response_artifact_fields_mismatch",
            ):
                self._execute(store, task, never)
            self.assertEqual(never.calls, [])
            for path, expected in before.items():
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), expected)

    def test_started_without_response_is_ambiguous_and_is_not_resent(self):
        task = self._trade_calendar_task()

        def fail(**_kwargs):
            raise OSError("simulated wire ambiguity")

        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            store = CreateOnlyTaskStore(temporary)
            first = FakeTransport(fail)
            with self.assertRaisesRegex(AlphaFeasibilityDataError, "unclassified_task_failure"):
                self._execute(store, task, first)
            second = FakeTransport(lambda **_kwargs: b"{}")
            with self.assertRaisesRegex(
                AmbiguousRemoteExecutionError, "ambiguous_started_without_response"
            ):
                self._execute(store, task, second)
            self.assertEqual(len(first.calls), 1)
            self.assertEqual(len(second.calls), 0)

    def test_post_cutoff_response_body_is_not_persisted(self):
        task = next(task for task in self.history if task.endpoint == "daily")
        raw = response_bytes(
            "daily",
            [["600000.SH", "20240102", 10, 11, 9, 10, 10, 1, 1]],
        )
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            store = CreateOnlyTaskStore(temporary)
            self._assert_execute_data_failure(
                store,
                task,
                FakeTransport(lambda **_kwargs: raw),
                "post_cutoff_response_date",
            )
            self.assertFalse(store.raw_path(task).exists())
            self.assertFalse(store.response_path(task).exists())
            quarantine = store.quarantine_path(task).read_text(encoding="utf-8")
            self.assertNotIn("20240102", quarantine)
            self.assertIn(hashlib.sha256(raw).hexdigest(), quarantine)
            self.assertEqual(
                json.loads(quarantine)["data_failure_category"],
                "post_cutoff_response_date",
            )

    def test_post_cutoff_date_hidden_in_message_is_quarantined_without_raw(self):
        task = self._trade_calendar_task()
        payload = {
            "code": 0,
            "msg": "upstream snapshot 2024-01-02",
            "data": {
                "fields": list(EXPECTED_FIELDS["trade_cal"]),
                "items": [["SSE", "20170701", 0, "20170630"]],
            },
        }
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            store = CreateOnlyTaskStore(temporary)
            self._assert_execute_data_failure(
                store,
                task,
                FakeTransport(lambda **_kwargs: raw),
                "post_cutoff_response_date",
            )
            self.assertFalse(store.raw_path(task).exists())
            self.assertNotIn(b"2024-01-02", store.quarantine_path(task).read_bytes())

    def test_post_cutoff_date_in_suspend_timing_is_hash_only(self):
        suspend = next(task for task in self.history if task.endpoint == "suspend_d")
        raw = response_bytes(
            "suspend_d",
            [["600000.SH", "20231229", "2024-01-01", "S"]],
        )
        transport = FakeTransport(lambda **_kwargs: raw)
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            store = CreateOnlyTaskStore(temporary)
            self._assert_execute_data_failure(
                store,
                suspend,
                transport,
                "post_cutoff_response_date",
            )
            self.assertEqual(len(transport.calls), 1)
            self.assertFalse(store.raw_path(suspend).exists())
            self.assertFalse(store.response_path(suspend).exists())
            quarantine = store.quarantine_path(suspend).read_bytes()
            self.assertIn(hashlib.sha256(raw).hexdigest().encode("ascii"), quarantine)
            self.assertNotIn(b"2024-01-01", quarantine)

    def test_unsafe_token_and_json_escaped_echo_never_persist(self):
        task = self._trade_calendar_task()
        unsafe = 'UnitTestCredential"Escaped123456789'
        no_call = FakeTransport(lambda **_kwargs: self.fail("transport must not run"))
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            store = CreateOnlyTaskStore(temporary)
            with self.assertRaisesRegex(AlphaFeasibilityDataError, "credential_preflight_failed"):
                store.execute(
                    task,
                    token=unsafe,
                    transport=no_call,
                    timeout_seconds=1,
                    maximum_response_bytes=1024,
                )
            self.assertEqual(no_call.calls, [])
            self.assertFalse(store.started_path(task).exists())

        base = response_bytes(
            "trade_cal", [["SSE", "20170701", 0, "20170630"]]
        ).decode("utf-8")
        escaped = "".join(f"\\u{ord(char):04x}" for char in TOKEN)
        raw = base.replace('"msg":null', f'"msg":"{escaped}"').encode("utf-8")
        self.assertNotIn(TOKEN.encode("utf-8"), raw)
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            store = CreateOnlyTaskStore(temporary)
            with self.assertRaises(AlphaFeasibilityDataError) as raised:
                self._execute(store, task, FakeTransport(lambda **_kwargs: raw))
            self.assertEqual(
                raised.exception.code, "transport_extension_secret_detected"
            )
            self.assertFalse(store.raw_path(task).exists())
            self.assertFalse(store.response_path(task).exists())
            quarantine = store.quarantine_path(task).read_bytes()
            self.assertIn(hashlib.sha256(raw).hexdigest().encode("ascii"), quarantine)
            self.assertNotIn(TOKEN.encode("utf-8"), quarantine)
            self.assertEqual(
                json.loads(quarantine)["token_leak_check"],
                "DECODED_LEAK_REJECTED",
            )

    def test_raw_json_decimal_numbers_are_normalized_without_float_round_trip(self):
        task = next(task for task in self.history if task.endpoint == "daily")
        raw = response_bytes(
            "daily",
            [["600000.SH", "20170703", 10.1, 10.2, 10.0, 10.1, 10.0, 20240101, 15.15]],
        )
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            result = self._execute(
                CreateOnlyTaskStore(temporary), task, FakeTransport(lambda **_kwargs: raw)
            )
        self.assertEqual(result.rows[0]["open"], "10.1")
        self.assertEqual(result.rows[0]["vol"], "20240101")
        self.assertEqual(result.rows[0]["amount"], "15.15")

    def test_trade_calendar_boolean_is_not_accepted_as_integer_flag(self):
        task = self._trade_calendar_task()
        raw = response_bytes(
            "trade_cal", [["SSE", "20170703", True, "20170630"]]
        )
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            self._assert_execute_data_failure(
                CreateOnlyTaskStore(temporary),
                task,
                FakeTransport(lambda **_kwargs: raw),
                "data_required_value_invalid",
            )

    def test_forged_future_task_and_scope_mismatch_never_reach_transport(self):
        valid = next(task for task in self.history if task.endpoint == "daily")
        forged_future = deepcopy(dict(valid.params))
        forged_future["start_date"] = "20240101"
        forged_future["end_date"] = "20240131"
        object.__setattr__(valid, "params", MappingProxyType(forged_future))
        transport = FakeTransport(lambda **_kwargs: self.fail("transport must not run"))
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            store = CreateOnlyTaskStore(temporary)
            with self.assertRaisesRegex(
                AlphaFeasibilityDataError, "endpoint_params_differ_from_contract"
            ):
                self._execute(store, valid, transport)
            self.assertEqual(transport.calls, [])
            self.assertFalse(store.started_path(valid).exists())

        mismatch = next(
            task
            for task in build_history_plan(self.plan, ["600000.SH"])
            if task.endpoint == "daily"
        )
        mismatch_params = dict(mismatch.params)
        mismatch_params["ts_code"] = "600001.SH"
        object.__setattr__(mismatch, "params", MappingProxyType(mismatch_params))
        mismatch_transport = FakeTransport(
            lambda **_kwargs: self.fail("transport must not run")
        )
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            store = CreateOnlyTaskStore(temporary)
            with self.assertRaisesRegex(AlphaFeasibilityDataError, "task_params_scope_mismatch"):
                self._execute(store, mismatch, mismatch_transport)
            self.assertEqual(mismatch_transport.calls, [])
            self.assertFalse(store.started_path(mismatch).exists())

        connection_calls: list[tuple[object, ...]] = []

        def connection_forbidden(*args, **kwargs):
            connection_calls.append((*args, kwargs))
            raise AssertionError("HTTPS connection must not be constructed")

        with patch.object(taf.http.client, "HTTPSConnection", side_effect=connection_forbidden):
            with self.assertRaisesRegex(
                AlphaFeasibilityDataError, "endpoint_params_differ_from_contract"
            ):
                taf.HttpsTushareTransport()(
                    endpoint="daily",
                    params={
                        "ts_code": "600000.SH",
                        "start_date": "20240101",
                        "end_date": "20240131",
                    },
                    fields=EXPECTED_FIELDS["daily"],
                    token=TOKEN,
                    timeout_seconds=1,
                    maximum_response_bytes=1024,
                )
        self.assertEqual(connection_calls, [])

    def test_unbound_loader_rejects_forged_post_cutoff_response_artifact(self):
        forged_row = {
            "ts_code": "600000.SH",
            "trade_date": "20250102",
            "open": "10",
            "high": "10",
            "low": "10",
            "close": "10",
            "pre_close": "10",
            "vol": "1",
            "amount": "1",
        }
        artifact = {
            "schema_version": "tushare-alpha-feasibility-task-response.v1",
            "state": "RESPONSE_VALIDATED",
            "task_id": "daily-" + "a" * 64,
            "endpoint": "daily",
            "plan_sha256": "b" * 64,
            "raw_response_sha256": "c" * 64,
            "wire_response_sha256": "c" * 64,
            "raw_response_persisted": False,
            "normalized_rows_sha256": canonical_sha256([forged_row]),
            "row_count": 1,
            "isolated_future_delist_date_count": 0,
            "rows": [forged_row],
            "locked_test_status": dict(LOCKED_TEST_STATUS),
            "locked_test_consumed": False,
        }
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            task_root = Path(temporary) / "tasks"
            task_root.mkdir()
            (task_root / f"{artifact['task_id']}.response.json").write_bytes(
                canonical_json_bytes(artifact)
            )
            with self.assertRaisesRegex(
                AlphaFeasibilityDataError, "expected_tasks_required_for_plan_bound_load"
            ):
                load_normalized_rows(temporary, endpoint="daily")

    def test_unbound_started_count_is_bound_to_explicit_plan_hash(self):
        task = next(task for task in self.history if task.endpoint == "daily")
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            tasks = Path(temporary) / "tasks"
            tasks.mkdir()
            (tasks / f"{task.task_id}.started.json").write_bytes(
                canonical_json_bytes(taf._started_payload(task))
            )
            counts = taf.actual_tushare_request_count_by_endpoint(
                temporary, plan_sha256=self.plan.plan_sha256
            )
            self.assertEqual(counts["daily"], 1)

        forged = taf._started_payload(task)
        forged["task"]["plan_sha256"] = "f" * 64
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            tasks = Path(temporary) / "tasks"
            tasks.mkdir()
            (tasks / f"{task.task_id}.started.json").write_bytes(
                canonical_json_bytes(forged)
            )
            with self.assertRaisesRegex(
                AlphaFeasibilityDataError, "started_request_outside_current_plan"
            ):
                taf.actual_tushare_request_count_by_endpoint(
                    temporary, plan_sha256=self.plan.plan_sha256
                )

    def test_transport_hash_tampering_is_rejected_and_excluded_from_normalized_identity(self):
        daily = next(task for task in self.history if task.endpoint == "daily")
        daily_raw = response_bytes(
            "daily",
            [["600000.SH", "20170703", 10, 10, 10, 10, 10, 1, 1]],
        )
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            store = CreateOnlyTaskStore(temporary)
            self._execute(store, daily, FakeTransport(lambda **_kwargs: daily_raw))
            artifact = json.loads(store.response_path(daily).read_text(encoding="utf-8"))
            artifact["wire_response_sha256"] = "f" * 64
            unsigned = dict(artifact)
            unsigned.pop("response_artifact_sha256")
            artifact["response_artifact_sha256"] = canonical_sha256(unsigned)
            store.response_path(daily).write_bytes(canonical_json_bytes(artifact))
            with self.assertRaisesRegex(
                AlphaFeasibilityDataError, "response_artifact_semantics_mismatch"
            ):
                store._load_response(daily)



class BackfillLineageAndManifestTests(unittest.TestCase):
    def setUp(self):
        self.plan = load_config_and_build_plan(CONFIG)
        self.pit = taf.PitMembershipResult(
            coverage_report=MappingProxyType(
                {"pit_months_observed": 73, "generated_at": NOW.isoformat()}
            ),
            manifest=MappingProxyType(
                {"manifest_sha256": "a" * 64, "snapshots": []}
            ),
            union_instruments=("600000.SH",),
            passed=True,
        )
        self.coverage = taf.HistoryCoverageResult(
            report=MappingProxyType(
                {
                    "schema_version": taf.HISTORY_COVERAGE_SCHEMA_VERSION,
                    "generated_at": NOW.isoformat(),
                    "coverage_start": "2017-07-01",
                    "coverage_end": "2023-12-31",
                    "daily_coverage_status": "COMPLETE",
                    "adj_factor_coverage_status": "COMPLETE",
                    "suspension_coverage_status": "COMPLETE",
                    "benchmark_coverage_status": "COMPLETE",
                    "stock_basic_status": taf.STOCK_BASIC_STATUS,
                    "stock_basic_request_count": 0,
                    "security_master_pit_status": taf.SECURITY_MASTER_PIT_STATUS,
                    "valid_candidate_count_by_decision": {"2018-01-02": 1},
                    "insufficient_history_count_by_decision": {"2018-01-02": 0},
                    "ineligible_no_initial_price_count": 0,
                    "unexplained_market_data_gap_count": 0,
                }
            ),
            passed=True,
            trading_dates=("20170703",),
        )
        self.history_manifest = MappingProxyType({"manifest_sha256": "b" * 64})

    def test_ready_and_blocked_backfill_results_bind_all_manifest_lineage(self):
        pre_pit = taf._backfill_lineage(self.plan)
        self.assertIsNone(pre_pit["pit_membership_manifest_sha256"])
        self.assertIsNone(pre_pit["history_manifest_sha256"])
        no_network = FakeTransport(lambda **_kwargs: self.fail("network must not run"))
        common = (
            patch.object(taf, "_load_existing_pit_result", return_value=self.pit),
            patch.object(taf, "publish_history_artifacts"),
            patch.object(
                taf,
                "build_history_manifest_from_store",
                return_value=self.history_manifest,
            ),
        )
        with tempfile.TemporaryDirectory(dir=ROOT) as ready_root:
            with common[0], common[1], common[2], patch.object(
                taf, "execute_tasks_bounded"
            ), patch.object(
                taf, "validate_history_coverage_from_store", return_value=self.coverage
            ):
                ready = run_backfill(
                    CONFIG,
                    ready_root,
                    TOKEN,
                    transport=no_network,
                    generated_at=NOW,
                )
        self.assertEqual(ready["stage_status"], "DATA_READY_FOR_ALPHA_FEASIBILITY")
        self.assertEqual(ready["generated_at"], NOW.isoformat())
        self.assertEqual(ready["collection_plan_sha256"], self.plan.plan_sha256)
        self.assertEqual(ready["experiment_config_sha256"], self.plan.config_sha256)
        self.assertEqual(ready["pit_membership_manifest_sha256"], "a" * 64)
        self.assertEqual(ready["history_manifest_sha256"], "b" * 64)

        common = (
            patch.object(taf, "_load_existing_pit_result", return_value=self.pit),
            patch.object(taf, "publish_history_artifacts"),
            patch.object(
                taf,
                "build_history_manifest_from_store",
                return_value=self.history_manifest,
            ),
        )
        with tempfile.TemporaryDirectory(dir=ROOT) as blocked_root:
            with common[0], common[1], common[2], patch.object(
                taf,
                "execute_tasks_bounded",
                side_effect=AlphaFeasibilityDataError("history_transport_failed"),
            ):
                blocked = run_backfill(
                    CONFIG,
                    blocked_root,
                    TOKEN,
                    transport=no_network,
                    generated_at=NOW,
                )
        self.assertEqual(blocked["stage_status"], "BLOCKED_DATA")
        self.assertEqual(blocked["generated_at"], NOW.isoformat())
        self.assertEqual(blocked["collection_plan_sha256"], self.plan.plan_sha256)
        self.assertEqual(blocked["experiment_config_sha256"], self.plan.config_sha256)
        self.assertEqual(blocked["pit_membership_manifest_sha256"], "a" * 64)
        self.assertEqual(blocked["history_manifest_sha256"], "b" * 64)
        self.assertEqual(no_network.calls, [])

    def _valid_history_manifest(self) -> dict[str, object]:
        endpoints = {
            "trade_calendar": "trade_cal",
            "pit_membership": "index_weight",
            "daily": "daily",
            "adj_factor": "adj_factor",
            "suspension": "suspend_d",
            "benchmark": "index_daily",
        }
        datasets = {
            name: {
                "status": "complete",
                "endpoint": endpoint,
                "record_count": 1,
                "coverage_start": "2017-07-01",
                "coverage_end": "2023-12-31",
                "normalized_content_sha256": "d" * 64,
                "issues": [],
            }
            for name, endpoint in endpoints.items()
        }
        unsigned = {
            "schema_version": "tushare-alpha-feasibility-manifest.v3",
            "experiment_id": "a-share-technical-alpha-feasibility-tushare-p1-v1",
            "generated_at": NOW.isoformat(),
            "coverage_start": "2017-07-01",
            "coverage_end": "2023-12-31",
            "actual_tushare_request_count_by_endpoint": {
                endpoint: 0 for endpoint in taf.ALLOWED_ENDPOINTS
            },
            "stock_basic_status": taf.STOCK_BASIC_STATUS,
            "stock_basic_request_count": 0,
            "security_master_pit_status": taf.SECURITY_MASTER_PIT_STATUS,
            "pit_months_expected": 73,
            "pit_months_observed": 73,
            "union_instrument_count": 1,
            "valid_candidate_count_by_decision": {"2023-12-28": 1},
            "insufficient_history_count_by_decision": {"2023-12-28": 0},
            "ineligible_no_initial_price_count": 0,
            "unexplained_market_data_gap_count": 0,
            "datasets": datasets,
            "data_status": "READY",
            "remaining_blockers": [],
            "locked_test_status": dict(LOCKED_TEST_STATUS),
            "locked_test_consumed": False,
            "safety": {
                "research_status": "research_alpha_feasibility_only",
                "execution_realism": "INCOMPLETE",
                "paper_eligibility": False,
                "trade_eligibility": False,
                "automatic_order_submission": False,
                "live_supported": False,
            },
        }
        return {**unsigned, "manifest_sha256": canonical_sha256(unsigned)}

    def test_loader_rejects_schema_valid_resigned_history_manifest_tampering(self):
        expected = self._valid_history_manifest()
        tampered = deepcopy(expected)
        tampered["union_instrument_count"] = 2
        unsigned = dict(tampered)
        unsigned.pop("manifest_sha256")
        tampered["manifest_sha256"] = canonical_sha256(unsigned)
        coverage_artifact = dict(self.coverage.report)
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            (root / "history_manifest.json").write_bytes(canonical_json_bytes(tampered))
            (root / "history_coverage_report.json").write_bytes(
                canonical_json_bytes(coverage_artifact)
            )
            with patch.object(taf, "_load_existing_pit_result", return_value=self.pit), patch.object(
                taf, "validate_history_coverage_from_store", return_value=self.coverage
            ), patch.object(
                taf, "build_history_manifest_from_store", return_value=expected
            ):
                with self.assertRaisesRegex(
                    AlphaFeasibilityDataError, "history_manifest_replay_failed"
                ):
                    taf.load_feasibility_inputs(temporary, CONFIG)


class BoundedHistoryCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = load_config_and_build_plan(CONFIG)
        cls.codes = ("600000.SH", "600001.SH", "600002.SH", "600003.SH")
        cls.tasks = build_history_plan(cls.plan, cls.codes)
        cls.calendar_rows = []
        cls.open_dates = []
        cursor = date(2017, 7, 1)
        terminal = date(2023, 12, 31)
        last_open = None
        while cursor <= terminal:
            is_open = cursor.weekday() < 5 and cursor.day != 1
            pretrade = date(2017, 6, 30) if last_open is None else last_open
            cls.calendar_rows.append(
                {
                    "exchange": "SSE",
                    "cal_date": cursor.strftime("%Y%m%d"),
                    "is_open": int(is_open),
                    "pretrade_date": pretrade.strftime("%Y%m%d"),
                }
            )
            if is_open:
                cls.open_dates.append(cursor)
                last_open = cursor
            cursor += timedelta(days=1)
        first_by_month = {}
        for item in cls.open_dates:
            first_by_month.setdefault(item.strftime("%Y-%m"), item)
        cls.pit_snapshots = [
            {
                "snapshot_date": first_by_month[month].isoformat(),
                "members": [
                    {"instrument_id": code, "weight": "25.000"}
                    for code in cls.codes
                ],
            }
            for month in taf._month_sequence("2017-12", "2023-12")
        ]

    def rows_for_task(self, task, *, omit_daily=None):
        if task.endpoint == "trade_cal":
            return self.calendar_rows
        if task.endpoint == "index_daily":
            return [
                {"ts_code": "000906.SH", "trade_date": item.strftime("%Y%m%d")}
                for item in self.open_dates
            ]
        if task.endpoint == "daily":
            return [
                {"ts_code": code, "trade_date": item.strftime("%Y%m%d")}
                for code in task.scope_instruments
                if code != omit_daily
                for item in self.open_dates
            ]
        if task.endpoint == "adj_factor":
            return [
                {
                    "ts_code": task.scope_instruments[0],
                    "trade_date": item.strftime("%Y%m%d"),
                    "adj_factor": "1",
                }
                for item in self.open_dates
            ]
        if task.endpoint == "suspend_d":
            return []
        self.fail(f"unexpected endpoint {task.endpoint}")

    def test_three_plus_one_batches_pass_and_missing_member_daily_fails_closed(self):
        store = InMemoryCompletedStore(self.rows_for_task)
        result = taf.validate_history_coverage_from_store(
            self.plan,
            self.codes,
            self.tasks,
            store,
            pit_snapshots=self.pit_snapshots,
            generated_at=NOW,
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.report["union_instrument_count"], 4)
        self.assertEqual(
            [
                len(task.scope_instruments)
                for task in self.tasks
                if task.endpoint == "daily"
            ],
            [3, 1],
        )

        missing = InMemoryCompletedStore(
            lambda task: self.rows_for_task(task, omit_daily="600003.SH")
        )
        blocked = taf.validate_history_coverage_from_store(
            self.plan,
            self.codes,
            self.tasks,
            missing,
            pit_snapshots=self.pit_snapshots,
            generated_at=NOW,
        )
        self.assertFalse(blocked.passed)
        self.assertIn(
            "unexplained_market_data_gap",
            {item["reason"] for item in blocked.report["blockers"]},
        )

    def test_recent_member_leading_suspension_and_short_history_are_ineligible(self):
        recent_code = "600003.SH"
        introduction = next(
            date.fromisoformat(item["snapshot_date"])
            for item in self.pit_snapshots
            if item["snapshot_date"].startswith("2023-10")
        )
        snapshots = deepcopy(self.pit_snapshots)
        for snapshot in snapshots:
            if date.fromisoformat(snapshot["snapshot_date"]) < introduction:
                snapshot["members"] = [
                    member
                    for member in snapshot["members"]
                    if member["instrument_id"] != recent_code
                ]

        def recent_rows(task):
            rows = self.rows_for_task(task)
            if task.endpoint in {"daily", "adj_factor"}:
                return [
                    row
                    for row in rows
                    if row["ts_code"] != recent_code
                    or row["trade_date"] > introduction.strftime("%Y%m%d")
                ]
            if task.endpoint == "suspend_d" and recent_code in task.scope_instruments:
                return [
                    {
                        "ts_code": recent_code,
                        "trade_date": introduction.strftime("%Y%m%d"),
                        "suspend_timing": None,
                        "suspend_type": "S",
                    }
                ]
            return rows

        result = taf.validate_history_coverage_from_store(
            self.plan,
            self.codes,
            self.tasks,
            InMemoryCompletedStore(recent_rows),
            pit_snapshots=snapshots,
            generated_at=NOW,
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.report["unexplained_market_data_gap_count"], 0)
        self.assertGreater(
            sum(result.report["insufficient_history_count_by_decision"].values()),
            0,
        )
        self.assertEqual(result.report["ineligible_no_initial_price_count"], 1)

    def test_member_with_no_price_ever_is_ineligible_when_suspension_is_complete(self):
        recent_code = "600003.SH"
        introduction = next(
            date.fromisoformat(item["snapshot_date"])
            for item in self.pit_snapshots
            if item["snapshot_date"].startswith("2023-12")
        )
        snapshots = deepcopy(self.pit_snapshots)
        for snapshot in snapshots:
            if date.fromisoformat(snapshot["snapshot_date"]) < introduction:
                snapshot["members"] = [
                    member
                    for member in snapshot["members"]
                    if member["instrument_id"] != recent_code
                ]

        def no_initial_price_rows(task):
            rows = self.rows_for_task(task)
            if task.endpoint in {"daily", "adj_factor"}:
                return [row for row in rows if row["ts_code"] != recent_code]
            if task.endpoint == "suspend_d" and recent_code in task.scope_instruments:
                return [
                    {
                        "ts_code": recent_code,
                        "trade_date": item.strftime("%Y%m%d"),
                        "suspend_timing": None,
                        "suspend_type": "S",
                    }
                    for item in self.open_dates
                    if item >= introduction
                ]
            return rows

        result = taf.validate_history_coverage_from_store(
            self.plan,
            self.codes,
            self.tasks,
            InMemoryCompletedStore(no_initial_price_rows),
            pit_snapshots=snapshots,
            generated_at=NOW,
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.report["unexplained_market_data_gap_count"], 0)
        self.assertGreater(result.report["ineligible_no_initial_price_count"], 0)


class P15DataLaneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = load_config_and_build_plan(P15_CONFIG)
        cls.task = cls.plan.pit_tasks[0]

    @staticmethod
    def _rows(*, last_weight: str = "0.25", repeated_weight: str = "0.125"):
        rows = [
            {
                "index_code": "000906.SH",
                "con_code": f"{number:06d}.{'SH' if number % 2 == 0 else 'SZ'}",
                "trade_date": "20171229",
                "weight": (
                    "0"
                    if number == 1
                    else last_weight
                    if number == 800
                    else repeated_weight
                ),
            }
            for number in range(1, 801)
        ]
        return rows

    @staticmethod
    def _raw(rows, **extensions):
        payload = {
            "code": 0,
            "msg": None,
            "data": {
                "fields": list(EXPECTED_FIELDS["index_weight"]),
                "items": [
                    [row[field] for field in EXPECTED_FIELDS["index_weight"]]
                    for row in rows
                ],
            },
            **extensions,
        }
        return json.dumps(payload, separators=(",", ":")).encode("utf-8")

    def _stage_synthetic_continuation_prefix(self, root):
        """Stage replayable tasks 1-19 plus the inherited task-20 attempt."""

        store = CreateOnlyTaskStore(root)
        for task in self.plan.pit_tasks[:19]:
            rows = self._rows()
            for row in rows:
                row["trade_date"] = task.params["end_date"]
            store.execute(
                task,
                token=TOKEN,
                transport=FakeTransport(lambda **_kwargs: self._raw(rows)),
                timeout_seconds=30,
                maximum_response_bytes=2097152,
                persist_full_raw_transport=True,
            )
        task20 = self.plan.pit_tasks[19]
        taf._write_json_create_only(
            store.started_path(task20),
            taf._started_payload(task20, recoverable=True),
            token=TOKEN,
        )
        taf._write_json_create_only(
            store.attempt_path(task20, 1),
            taf._attempt_payload(task20, 1),
            token=TOKEN,
        )
        return store

    def test_fixed_weight_hard_gate_warning_and_zero_evidence(self):
        warning_rows = self._rows(last_weight="0.19")
        result = build_pit_membership_artifacts(
            self.plan, {self.task.task_id: warning_rows}, generated_at=NOW
        )
        first = result.coverage_report["monthly_checks"][0]
        snapshot = first["snapshots"][0]
        self.assertEqual(first["status"], "complete")
        self.assertEqual(snapshot["component_count"], 800)
        self.assertEqual(snapshot["weight_sum"], "99.940")
        self.assertEqual(snapshot["zero_weight_count"], 1)
        self.assertEqual(snapshot["warnings"], ["weight_sum_outside_warning_range"])
        self.assertEqual(result.coverage_report["duplicate_member_count"], 0)

        hard_failure = build_pit_membership_artifacts(
            self.plan,
            {
                self.task.task_id: self._rows(
                    repeated_weight="0.124", last_weight="0.538"
                )
            },
            generated_at=NOW,
        )
        failed = hard_failure.coverage_report["monthly_checks"][0]
        self.assertEqual(failed["status"], "invalid")
        self.assertIn("weight_sum_outside_hard_range", failed["issues"])

    def test_p15_manifest_carries_count_semantics_and_real_money_guard(self):
        coverage = taf.HistoryCoverageResult(
            report=MappingProxyType(
                {
                    "daily_coverage_status": "BLOCKED_DATA",
                    "adj_factor_coverage_status": "BLOCKED_DATA",
                    "suspension_coverage_status": "BLOCKED_DATA",
                    "benchmark_coverage_status": "BLOCKED_DATA",
                    "unexplained_market_data_gap_count": 0,
                    "blockers": [{"reason": "fixture_incomplete"}],
                }
            ),
            passed=False,
            trading_dates=(),
        )
        counts = {endpoint: 0 for endpoint in taf.ALLOWED_ENDPOINTS}
        manifest = taf.build_history_manifest(
            self.plan,
            (),
            {},
            coverage,
            request_counts=counts,
            generated_at=NOW,
        )
        self.assertEqual(
            manifest["schema_version"], "tushare-alpha-feasibility-manifest.v4"
        )
        self.assertEqual(
            manifest["request_count_semantics"],
            "conservative_durable_pre_transport_attempt_claim",
        )
        self.assertIs(manifest["safety"]["real_money_list_allowed"], False)
        validate_json_schema(
            manifest, ROOT / "schemas" / "tushare_alpha_feasibility_manifest.v4.json"
        )

    def test_full_raw_resume_attempt_count_and_terminal_quarantine(self):
        raw = self._raw(
            self._rows(), request_id="req-import-safe", detail={"status": "ok"}
        )
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            store = CreateOnlyTaskStore(temporary)

            def interrupted(**_kwargs):
                raise AlphaFeasibilityDataError("https_transport_failed")

            with self.assertRaisesRegex(AlphaFeasibilityDataError, "https_transport_failed"):
                store.execute(
                    self.task,
                    token=TOKEN,
                    transport=FakeTransport(interrupted),
                    timeout_seconds=30,
                    maximum_response_bytes=2097152,
                    recover_interrupted_attempts=True,
                    maximum_attempts_per_fingerprint=3,
                    persist_full_raw_transport=True,
                )
            self.assertFalse(store.quarantine_path(self.task).exists())
            completed = store.execute(
                self.task,
                token=TOKEN,
                transport=FakeTransport(lambda **_kwargs: raw),
                timeout_seconds=30,
                maximum_response_bytes=2097152,
                recover_interrupted_attempts=True,
                maximum_attempts_per_fingerprint=3,
                persist_full_raw_transport=True,
            )
            self.assertEqual(completed.network_request_count, 2)
            self.assertEqual(store.raw_path(self.task).read_bytes(), raw)
            never = FakeTransport(lambda **_kwargs: self.fail("completed task resent"))
            replayed = store.execute(
                self.task,
                token="",
                transport=never,
                timeout_seconds=30,
                maximum_response_bytes=2097152,
                recover_interrupted_attempts=True,
                maximum_attempts_per_fingerprint=3,
                persist_full_raw_transport=True,
            )
            self.assertTrue(replayed.replayed)
            self.assertEqual(len(never.calls), 0)
            self.assertEqual(
                taf.actual_tushare_request_count_by_endpoint(
                    temporary, self.plan.pit_tasks, plan_sha256=self.plan.plan_sha256
                )["index_weight"],
                2,
            )

        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            store = CreateOnlyTaskStore(temporary)
            bad = b'{"code":-2001,"msg":"permission denied","data":null}'
            first = FakeTransport(lambda **_kwargs: bad)
            with self.assertRaises(AlphaFeasibilityDataError):
                store.execute(
                    self.task,
                    token=TOKEN,
                    transport=first,
                    timeout_seconds=30,
                    maximum_response_bytes=2097152,
                    recover_interrupted_attempts=True,
                    maximum_attempts_per_fingerprint=3,
                    persist_full_raw_transport=True,
                )
            second = FakeTransport(lambda **_kwargs: self.fail("quarantine retried"))
            with self.assertRaises(AlphaFeasibilityDataError):
                store.execute(
                    self.task,
                    token=TOKEN,
                    transport=second,
                    timeout_seconds=30,
                    maximum_response_bytes=2097152,
                    recover_interrupted_attempts=True,
                    maximum_attempts_per_fingerprint=3,
                    persist_full_raw_transport=True,
                )
            self.assertEqual(len(first.calls), 1)
            self.assertEqual(len(second.calls), 0)

    def test_business_error_evidence_and_single_rate_retry_are_durable(self):
        first_requested = NOW
        first_completed = NOW + timedelta(seconds=1)
        first_clock_values = iter((first_requested, first_completed))
        error_raw = json.dumps(
            {
                "code": -1,
                "msg": "访问过快",
                "data": None,
                "request_id": "full-request-id-must-only-exist-in-deep-scanned-raw",
                "detail": {
                    "limit": 100,
                    "remaining": 0,
                    "reset": "2023-12-31",
                    "retry_after": 15,
                    "message": "访问频率过高",
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        success_raw = self._raw(self._rows(), request_id="success-request-id")
        with tempfile.TemporaryDirectory(dir=taf.DATA_TMP_ROOT) as temporary:
            store = CreateOnlyTaskStore(temporary)
            with self.assertRaisesRegex(
                AlphaFeasibilityDataError, "upstream_rate_limit_error"
            ):
                store.execute(
                    self.task,
                    token=TOKEN,
                    transport=FakeTransport(lambda **_kwargs: error_raw),
                    timeout_seconds=30,
                    maximum_response_bytes=2097152,
                    recover_interrupted_attempts=True,
                    maximum_attempts_per_fingerprint=3,
                    persist_full_raw_transport=True,
                    clock=lambda: next(first_clock_values),
                    defer_retryable_business_errors=True,
                )
            self.assertFalse(store.quarantine_path(self.task).exists())
            self.assertEqual(store.raw_error_path(self.task, 1).read_bytes(), error_raw)
            business_error = json.loads(
                store.business_error_path(self.task, 1).read_text(encoding="utf-8")
            )
            validate_json_schema(
                business_error,
                ROOT / "schemas" / "tushare_alpha_feasibility_business_error.v1.json",
            )
            evidence = business_error["evidence"]
            self.assertEqual(evidence["classification"], "RATE_LIMITED")
            self.assertEqual(evidence["retry_after_seconds"], 15)
            self.assertEqual(evidence["safe_detail_projection"]["value"]["limit"], 100)
            self.assertNotIn(
                "full-request-id-must-only-exist-in-deep-scanned-raw",
                json.dumps(business_error, ensure_ascii=False),
            )
            self.assertEqual(
                evidence["response_body_sha256"], hashlib.sha256(error_raw).hexdigest()
            )
            self.assertEqual(evidence["raw_transport_sha256"], evidence["response_body_sha256"])
            self.assertEqual(evidence["response_byte_count"], len(error_raw))
            original_business_error_bytes = store.business_error_path(
                self.task, 1
            ).read_bytes()
            resigned = json.loads(original_business_error_bytes)
            resigned["evidence"]["safe_detail_projection"]["value"][
                "message"
            ] = "password=forged"
            unsigned = dict(resigned)
            unsigned.pop("business_error_artifact_sha256")
            resigned["business_error_artifact_sha256"] = canonical_sha256(unsigned)
            store.business_error_path(self.task, 1).write_bytes(
                canonical_json_bytes(resigned)
            )
            with self.assertRaisesRegex(
                AlphaFeasibilityDataError, "business_error_evidence_invalid"
            ):
                store._load_business_errors(self.task)
            store.business_error_path(self.task, 1).write_bytes(
                original_business_error_bytes
            )

            sleep_calls = []
            retry_clock_values = iter(
                (
                    first_completed + timedelta(seconds=5),
                    first_completed + timedelta(seconds=15),
                    first_completed + timedelta(seconds=16),
                )
            )
            completed = taf.retry_business_error_once(
                store,
                self.task,
                token=TOKEN,
                transport=FakeTransport(lambda **_kwargs: success_raw),
                timeout_seconds=30,
                maximum_response_bytes=2097152,
                maximum_attempts_per_fingerprint=3,
                clock=lambda: next(retry_clock_values),
                sleeper=sleep_calls.append,
            )
            self.assertEqual(sleep_calls, [10.0])
            self.assertEqual(completed.network_request_count, 2)
            self.assertTrue(store.is_complete(self.task))
            self.assertEqual(
                taf.actual_tushare_request_count_by_endpoint(
                    temporary,
                    self.plan.pit_tasks,
                    plan_sha256=self.plan.plan_sha256,
                )["index_weight"],
                2,
            )
            with self.assertRaisesRegex(
                AlphaFeasibilityDataError, "business_error_retry_already_consumed"
            ):
                taf.retry_business_error_once(
                    store,
                    self.task,
                    token=TOKEN,
                    transport=FakeTransport(lambda **_kwargs: success_raw),
                    timeout_seconds=30,
                    maximum_response_bytes=2097152,
                    maximum_attempts_per_fingerprint=3,
                    clock=lambda: first_completed + timedelta(seconds=30),
                    sleeper=lambda _seconds: None,
                )

        self.assertEqual(
            taf.business_error_retry_policy(
                "RATE_LIMITED", retry_after_seconds=999
            ).delay_seconds,
            taf.Decimal("65"),
        )
        self.assertEqual(
            taf.business_error_retry_policy(
                "UPSTREAM_SERVER_ERROR", retry_after_seconds=None
            ).delay_seconds,
            taf.Decimal("12"),
        )
        self.assertEqual(
            taf.business_error_retry_policy(
                "PERMISSION_DENIED", retry_after_seconds=None
            ).maximum_additional_attempts,
            0,
        )

    def test_execute_tasks_continuation_consumes_parent_attempt_and_server_retry(self):
        server_error = json.dumps(
            {
                "code": -1,
                "msg": "系统错误",
                "data": None,
                "detail": {"message": "服务异常"},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        success = self._raw(self._rows())
        with tempfile.TemporaryDirectory(dir=taf.DATA_TMP_ROOT) as temporary:
            store = CreateOnlyTaskStore(temporary)
            taf._write_json_create_only(
                store.started_path(self.task),
                taf._started_payload(self.task, recoverable=True),
                token=TOKEN,
            )
            taf._write_json_create_only(
                store.attempt_path(self.task, 1),
                taf._attempt_payload(self.task, 1),
                token=TOKEN,
            )
            bodies = iter((server_error, success))
            transport = FakeTransport(lambda **_kwargs: next(bodies))
            clock_values = iter(
                (
                    NOW,
                    NOW + timedelta(seconds=1),
                    NOW + timedelta(seconds=1),
                    NOW + timedelta(seconds=13),
                    NOW + timedelta(seconds=14),
                )
            )
            sleeps = []
            results = taf.execute_tasks(
                (self.task,),
                store=store,
                token=TOKEN,
                transport=transport,
                timeout_seconds=30,
                maximum_response_bytes=2097152,
                recover_interrupted_attempts=True,
                maximum_attempts_per_fingerprint=3,
                persist_full_raw_transport=True,
                minimum_request_interval_seconds=taf.Decimal("12"),
                business_error_retry_once=True,
                clock=lambda: next(clock_values),
                sleeper=sleeps.append,
            )
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].network_request_count, 3)
            self.assertEqual(len(transport.calls), 2)
            self.assertEqual(sleeps, [12.0])
            self.assertTrue(store.attempt_path(self.task, 3).is_file())
            self.assertFalse(store.quarantine_path(self.task).exists())

    def test_legacy_quarantine_remains_terminal_and_continuation_shape_is_gated(self):
        permission_error = json.dumps(
            {"code": -1, "msg": "积分不足", "data": None},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            store = CreateOnlyTaskStore(temporary)
            with self.assertRaisesRegex(
                AlphaFeasibilityDataError, "upstream_permission_error"
            ):
                store.execute(
                    self.task,
                    token=TOKEN,
                    transport=FakeTransport(lambda **_kwargs: permission_error),
                    timeout_seconds=30,
                    maximum_response_bytes=2097152,
                    recover_interrupted_attempts=True,
                    maximum_attempts_per_fingerprint=3,
                    persist_full_raw_transport=True,
                )
            current = json.loads(
                store.quarantine_path(self.task).read_text(encoding="utf-8")
            )
            v4_schema = json.loads(
                (
                    ROOT
                    / "schemas"
                    / "tushare_alpha_feasibility_quarantine.v4.json"
                ).read_text(encoding="utf-8")
            )
            legacy = {
                key: current[key]
                for key in v4_schema["properties"]
                if key in current
            }
            legacy["schema_version"] = "tushare-alpha-feasibility-quarantine.v4"
            legacy["raw_response_persisted"] = False
            store.quarantine_path(self.task).write_bytes(
                canonical_json_bytes(legacy)
            )
            never = FakeTransport(lambda **_kwargs: self.fail("legacy retried"))
            with self.assertRaisesRegex(
                AlphaFeasibilityDataError, "upstream_permission_error"
            ):
                taf.retry_business_error_once(
                    store,
                    self.task,
                    token=TOKEN,
                    transport=never,
                    timeout_seconds=30,
                    maximum_response_bytes=2097152,
                    maximum_attempts_per_fingerprint=3,
                )
            self.assertEqual(len(never.calls), 0)

    def test_continuation_data_unavailable_maps_to_pit_source_coverage(self):
        unavailable = json.dumps(
            {
                "code": -1,
                "msg": "该日期无数据",
                "data": None,
                "detail": {"message": "历史区间不支持"},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        with tempfile.TemporaryDirectory(dir=taf.DATA_TMP_ROOT) as temporary:
            self._stage_synthetic_continuation_prefix(temporary)
            transport = FakeTransport(lambda **_kwargs: unavailable)
            with (
                patch.object(
                    taf, "_validate_parent_reuse_continuation_child", return_value=None
                ),
                patch.object(
                    taf,
                    "_consume_parent_reuse_continuation_network_process",
                    return_value=None,
                ),
            ):
                result = run_backfill(
                    P15_CONFIG,
                    temporary,
                    TOKEN,
                    transport=transport,
                    generated_at=NOW,
                    sleeper=lambda _seconds: None,
                    _continuation_execution=taf._CONTINUATION_EXECUTION_CAPABILITY,
                )
            self.assertEqual(result["stage_status"], "BLOCKED_PIT_SOURCE_COVERAGE")
            self.assertEqual(result["terminal_status"], "BLOCKED_DATA")
            self.assertEqual(
                result["remaining_blockers"],
                ["upstream_data_unavailable_error"],
            )
            self.assertEqual(len(transport.calls), 1)

    def test_continuation_task20_snapshot_gate_precedes_task21_request(self):
        with tempfile.TemporaryDirectory(dir=taf.DATA_TMP_ROOT) as temporary:
            store = self._stage_synthetic_continuation_prefix(temporary)
            task20 = self.plan.pit_tasks[19]
            task21 = self.plan.pit_tasks[20]
            invalid_rows = self._rows()[:-1]
            for row in invalid_rows:
                row["trade_date"] = task20.params["end_date"]
            transport = FakeTransport(
                lambda **_kwargs: self._raw(invalid_rows)
            )
            with (
                patch.object(
                    taf, "_validate_parent_reuse_continuation_child", return_value=None
                ),
                patch.object(
                    taf,
                    "_consume_parent_reuse_continuation_network_process",
                    return_value=None,
                ),
            ):
                result = run_backfill(
                    P15_CONFIG,
                    temporary,
                    TOKEN,
                    transport=transport,
                    generated_at=NOW,
                    sleeper=lambda _seconds: None,
                    _continuation_execution=taf._CONTINUATION_EXECUTION_CAPABILITY,
                )
            self.assertEqual(result["stage_status"], "BLOCKED_PIT_MEMBERSHIP")
            self.assertEqual(len(transport.calls), 1)
            self.assertTrue(store.is_complete(task20))
            self.assertFalse(store.started_path(task21).exists())
            self.assertFalse((store.root / "attempts" / task21.task_id).exists())

    def test_continuation_initial_no_response_is_terminal_attempt2(self):
        with tempfile.TemporaryDirectory(dir=taf.DATA_TMP_ROOT) as temporary:
            store = self._stage_synthetic_continuation_prefix(temporary)
            task20 = self.plan.pit_tasks[19]

            def interrupted(**_kwargs):
                raise AlphaFeasibilityDataError("https_transport_failed")

            transport = FakeTransport(interrupted)
            with (
                patch.object(
                    taf, "_validate_parent_reuse_continuation_child", return_value=None
                ),
                patch.object(
                    taf,
                    "_consume_parent_reuse_continuation_network_process",
                    return_value=None,
                ),
            ):
                result = run_backfill(
                    P15_CONFIG,
                    temporary,
                    TOKEN,
                    transport=transport,
                    generated_at=NOW,
                    sleeper=lambda _seconds: None,
                    _continuation_execution=taf._CONTINUATION_EXECUTION_CAPABILITY,
                )
            self.assertEqual(result["terminal_status"], "BLOCKED_DATA")
            self.assertEqual(len(transport.calls), 1)
            quarantine = json.loads(
                store.quarantine_path(task20).read_text(encoding="utf-8")
            )
            self.assertEqual(
                quarantine["state"],
                "TRANSPORT_ATTEMPT_QUARANTINED_NO_RESPONSE",
            )
            self.assertEqual(quarantine["failure_code"], "https_transport_failed")
            self.assertEqual(quarantine["terminal_attempt_number"], 2)
            for field in (
                "raw_transport_sha256",
                "http_status",
                "response_byte_count",
                "upstream_code",
                "business_error_classification",
                "business_error_evidence",
                "business_error_artifact_sha256",
                "provider_payload_sha256",
                "normalized_content_sha256",
            ):
                self.assertIsNone(quarantine[field], field)
            self.assertFalse(quarantine["raw_response_persisted"])
            self.assertEqual(store._terminal_quarantine_code(task20), "https_transport_failed")

            forged = deepcopy(quarantine)
            forged["raw_transport_sha256"] = "0" * 64
            with self.assertRaises(SchemaValidationError):
                validate_json_schema(
                    forged,
                    ROOT / "schemas" / "tushare_alpha_feasibility_quarantine.v5.json",
                )

    def test_continuation_retry_no_response_is_terminal_attempt3(self):
        rate_error = json.dumps(
            {"code": -1, "msg": "访问频率过高", "data": None},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        with tempfile.TemporaryDirectory(dir=taf.DATA_TMP_ROOT) as temporary:
            store = self._stage_synthetic_continuation_prefix(temporary)
            task20 = self.plan.pit_tasks[19]
            call_count = 0

            def rate_then_interrupt(**_kwargs):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return rate_error
                raise AlphaFeasibilityDataError("https_transport_failed")

            transport = FakeTransport(rate_then_interrupt)
            clock_values = iter(
                (
                    NOW,
                    NOW + timedelta(seconds=1),
                    NOW + timedelta(seconds=1),
                    NOW + timedelta(seconds=66),
                )
            )
            with (
                patch.object(
                    taf, "_validate_parent_reuse_continuation_child", return_value=None
                ),
                patch.object(
                    taf,
                    "_consume_parent_reuse_continuation_network_process",
                    return_value=None,
                ),
            ):
                result = run_backfill(
                    P15_CONFIG,
                    temporary,
                    TOKEN,
                    transport=transport,
                    generated_at=NOW,
                    sleeper=lambda _seconds: None,
                    _continuation_execution=taf._CONTINUATION_EXECUTION_CAPABILITY,
                    _clock=lambda: next(clock_values),
                )
            self.assertEqual(result["terminal_status"], "BLOCKED_DATA")
            self.assertEqual(len(transport.calls), 2)
            self.assertTrue(store.business_error_path(task20, 2).is_file())
            quarantine = json.loads(
                store.quarantine_path(task20).read_text(encoding="utf-8")
            )
            self.assertEqual(
                quarantine["state"],
                "TRANSPORT_ATTEMPT_QUARANTINED_NO_RESPONSE",
            )
            self.assertEqual(quarantine["terminal_attempt_number"], 3)
            self.assertIsNone(quarantine["business_error_evidence"])
            self.assertFalse(quarantine["raw_response_persisted"])
            self.assertEqual(store._terminal_quarantine_code(task20), "https_transport_failed")

    def test_continuation_later_task_no_response_is_terminal(self):
        with tempfile.TemporaryDirectory(dir=taf.DATA_TMP_ROOT) as temporary:
            store = self._stage_synthetic_continuation_prefix(temporary)
            task20 = self.plan.pit_tasks[19]
            task21 = self.plan.pit_tasks[20]
            task22 = self.plan.pit_tasks[21]
            task20_rows = self._rows()
            for row in task20_rows:
                row["trade_date"] = task20.params["end_date"]
            call_count = 0

            def success_then_interrupt(**_kwargs):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return self._raw(task20_rows)
                raise AlphaFeasibilityDataError("https_transport_failed")

            transport = FakeTransport(success_then_interrupt)
            with (
                patch.object(
                    taf, "_validate_parent_reuse_continuation_child", return_value=None
                ),
                patch.object(
                    taf,
                    "_consume_parent_reuse_continuation_network_process",
                    return_value=None,
                ),
            ):
                result = run_backfill(
                    P15_CONFIG,
                    temporary,
                    TOKEN,
                    transport=transport,
                    generated_at=NOW,
                    sleeper=lambda _seconds: None,
                    _continuation_execution=taf._CONTINUATION_EXECUTION_CAPABILITY,
                    _clock=lambda: NOW,
                )
            self.assertEqual(result["terminal_status"], "BLOCKED_DATA")
            self.assertEqual(len(transport.calls), 2)
            quarantine = json.loads(
                store.quarantine_path(task21).read_text(encoding="utf-8")
            )
            self.assertEqual(
                quarantine["state"],
                "TRANSPORT_ATTEMPT_QUARANTINED_NO_RESPONSE",
            )
            self.assertEqual(quarantine["terminal_attempt_number"], 1)
            self.assertFalse(store.started_path(task22).exists())

        with tempfile.TemporaryDirectory(dir=taf.DATA_TMP_ROOT) as temporary:
            never = FakeTransport(lambda **_kwargs: self.fail("gate reached network"))
            with self.assertRaisesRegex(
                AlphaFeasibilityDataError, "continuation_reuse_prefix_incomplete"
            ):
                taf.run_parent_reuse_continuation_backfill(
                    P15_CONFIG,
                    temporary,
                    TOKEN,
                    transport=never,
                    generated_at=NOW,
                    sleeper=lambda _seconds: None,
                )
            self.assertEqual(len(never.calls), 0)

    def test_p15_full_raw_rejects_secret_assignment_and_future_trace(self):
        cases = (
            ({"detail": "password=abc"}, "transport_extension_secret_detected"),
            ({"trace_id": "trace-2025-01-02"}, "post_cutoff_response_date"),
        )
        for extensions, expected in cases:
            with self.subTest(extensions=extensions), tempfile.TemporaryDirectory(
                dir=ROOT
            ) as temporary:
                store = CreateOnlyTaskStore(temporary)
                raw = self._raw(self._rows(), **extensions)
                with self.assertRaisesRegex(AlphaFeasibilityDataError, expected):
                    store.execute(
                        self.task,
                        token=TOKEN,
                        transport=FakeTransport(lambda **_kwargs: raw),
                        timeout_seconds=30,
                        maximum_response_bytes=2097152,
                        recover_interrupted_attempts=True,
                        maximum_attempts_per_fingerprint=3,
                        persist_full_raw_transport=True,
                    )
                self.assertFalse(store.raw_path(self.task).exists())
                self.assertFalse(store.response_path(self.task).exists())
                self.assertTrue(store.quarantine_path(self.task).exists())

    def test_request_count_rejects_orphan_attempt_directory(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            orphan = Path(temporary) / "attempts" / ("index_weight-" + "f" * 64)
            orphan.mkdir(parents=True)
            (orphan / "000001.started.json").write_bytes(
                canonical_json_bytes({"self_signed": True})
            )
            with self.assertRaisesRegex(
                AlphaFeasibilityDataError, "orphan_attempt_request_evidence"
            ):
                taf.actual_tushare_request_count_by_endpoint(
                    temporary,
                    self.plan.pit_tasks,
                    plan_sha256=self.plan.plan_sha256,
                )

    def test_p14d_import_is_plan_bound_complete_and_zero_request(self):
        def build_bundle(raw):
            parsed, scan = p14d.scan_raw_response(raw, token=TOKEN)
            profile = p14d.build_value_profile(parsed, scan_evidence=scan)
            request = p14d._request_payload(NOW)
            started = p14d._network_started_payload(request)
            scanned = p14d._network_scanned_payload(request, scan=scan)
            validated = taf.validate_response_bytes(self.task, raw, token=TOKEN)
            normalized = p14d._normalized_pit_payload(validated)
            profile_bytes = p14d._transport_json_bytes(profile)
            normalized_bytes = p14d._transport_json_bytes(normalized)
            replay = {
                "schema_version": "tushare-index-weight-offline-replay.v1",
                "raw_transport_sha256": scan["raw_transport_sha256"],
                "value_profile_sha256": hashlib.sha256(profile_bytes).hexdigest(),
                "normalization_change": "p15_plan_bound_import",
                "offline_replay_status": "DIAGNOSTIC_REPLAY_ACCEPTED",
                "replay_pass_count": 2,
                "deterministic_replay": True,
                "normalized_row_count": normalized["row_count"],
                "normalized_trade_dates": normalized["trade_dates"],
                "normalized_weight_sum_by_date": normalized[
                    "weight_sum_by_trade_date"
                ],
                "normalized_content_sha256": normalized[
                    "normalized_content_sha256"
                ],
                "normalized_pit_sha256": hashlib.sha256(
                    normalized_bytes
                ).hexdigest(),
                "terminal_status": "DIAGNOSTIC_REPLAY_ACCEPTED",
                "locked_test_status": dict(LOCKED_TEST_STATUS),
                "locked_test_consumed": False,
            }
            return {
                "request.json": p14d._transport_json_bytes(request),
                "network_call_started.json": p14d._transport_json_bytes(started),
                "network_response_scanned.json": p14d._transport_json_bytes(scanned),
                "response.raw.json": raw,
                "value_profile.json": profile_bytes,
                "normalized_pit.json": normalized_bytes,
                "offline_replay.json": p14d._transport_json_bytes(replay),
            }, request, normalized

        def write_bundle(directory, artifacts):
            directory.mkdir()
            for name, content in artifacts.items():
                (directory / name).write_bytes(content)

        accepted_artifacts, request, normalized = build_bundle(
            self._raw(self._rows(), request_id="req-p14d-safe")
        )
        accepted_hashes = {
            name: hashlib.sha256(content).hexdigest()
            for name, content in accepted_artifacts.items()
        }
        accepted_binding = {
            "bundle_sha256": taf.canonical_sha256(accepted_hashes),
            "request_fingerprint": request["request_fingerprint"],
            "raw_transport_sha256": accepted_hashes["response.raw.json"],
            "normalized_content_sha256": normalized["normalized_content_sha256"],
            "source_artifact_sha256_by_name": accepted_hashes,
        }
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            source = Path(temporary) / "accepted-p14d"
            output = Path(temporary) / "output"
            write_bundle(source, accepted_artifacts)

            import_schema = json.loads(
                (
                    ROOT
                    / "schemas"
                    / "tushare_alpha_feasibility_task_import.v1.json"
                ).read_text(encoding="utf-8")
            )
            import_schema["properties"]["accepted_bundle_sha256"]["const"] = (
                accepted_binding["bundle_sha256"]
            )
            import_schema["properties"]["request_fingerprint"]["const"] = (
                accepted_binding["request_fingerprint"]
            )
            import_schema["properties"]["raw_transport_sha256"]["const"] = (
                accepted_binding["raw_transport_sha256"]
            )
            import_schema["properties"]["normalized_content_sha256"]["const"] = (
                accepted_binding["normalized_content_sha256"]
            )
            for name, digest in accepted_hashes.items():
                import_schema["properties"]["source_artifact_sha256_by_name"][
                    "properties"
                ][name]["const"] = digest
            import_schema_path = Path(temporary) / "synthetic-import-schema.json"
            import_schema_path.write_text(
                json.dumps(import_schema), encoding="utf-8"
            )
            (Path(temporary) / "pit_membership_coverage_report.v1.json").write_bytes(
                (
                    ROOT / "schemas" / "pit_membership_coverage_report.v1.json"
                ).read_bytes()
            )

            never = FakeTransport(lambda **_kwargs: self.fail("import resent"))
            with (
                patch.object(
                    taf, "_p14d_bundle_binding", return_value=accepted_binding
                ),
                patch.object(taf, "IMPORT_SCHEMA_PATH", import_schema_path),
            ):
                imported = taf.import_p14d_diagnostic_into_plan(
                    diagnostic_root=source,
                    output_root=output,
                    plan=self.plan,
                )
                store = CreateOnlyTaskStore(output)
                self.assertTrue(store.is_complete(self.task))
                self.assertEqual(imported.request_origin, "offline_p14d_import")
                self.assertEqual(imported.network_request_count, 0)
                raw_before = store.raw_path(self.task).read_bytes()
                response_before = store.response_path(self.task).read_bytes()
                self.assertEqual(
                    raw_before, accepted_artifacts["response.raw.json"]
                )
                imported_evidence = json.loads(
                    store.import_path(self.task).read_text(encoding="utf-8")
                )
                self.assertEqual(
                    imported_evidence["accepted_bundle_sha256"],
                    accepted_binding["bundle_sha256"],
                )
                self.assertEqual(
                    imported_evidence["source_artifact_sha256_by_name"],
                    accepted_hashes,
                )
                self.assertEqual(
                    taf.actual_tushare_request_count_by_endpoint(
                        output,
                        self.plan.pit_tasks,
                        plan_sha256=self.plan.plan_sha256,
                    )["index_weight"],
                    0,
                )
                replayed = store.execute(
                    self.task,
                    token="",
                    transport=never,
                    timeout_seconds=30,
                    maximum_response_bytes=2097152,
                )
                self.assertTrue(replayed.replayed)
                self.assertEqual(replayed.request_origin, "offline_p14d_import")
                self.assertEqual(len(never.calls), 0)
                self.assertEqual(store.raw_path(self.task).read_bytes(), raw_before)
                self.assertEqual(
                    store.response_path(self.task).read_bytes(), response_before
                )

            # This alternate bundle is internally re-signed and passes the old
            # self-consistency chain, but it differs from the frozen binding.
            forged_source = Path(temporary) / "forged-p14d"
            forged_artifacts, _, _ = build_bundle(
                self._raw(self._rows(), request_id="req-p14d-forged")
            )
            write_bundle(forged_source, forged_artifacts)
            with patch.object(
                taf, "_p14d_bundle_binding", return_value=accepted_binding
            ):
                with self.assertRaisesRegex(
                    AlphaFeasibilityDataError,
                    "p14d_import_bundle_binding_mismatch",
                ):
                    taf.import_p14d_diagnostic_into_plan(
                        diagnostic_root=forged_source,
                        output_root=Path(temporary) / "forged-output",
                        plan=self.plan,
                    )

    def test_parent_run_reuse_v2_is_parent_bound_and_zero_request(self):
        with tempfile.TemporaryDirectory(dir=taf.DATA_TMP_ROOT) as temporary:
            root = Path(temporary)
            parent = root / "parent"
            child = root / "child"
            parent.mkdir()
            child.mkdir()
            parent_store = CreateOnlyTaskStore(parent)
            raw = self._raw(self._rows(), request_id="parent-success-request")
            parent_result = parent_store.execute(
                self.task,
                token=TOKEN,
                transport=FakeTransport(lambda **_kwargs: raw),
                timeout_seconds=30,
                maximum_response_bytes=2097152,
                recover_interrupted_attempts=True,
                maximum_attempts_per_fingerprint=3,
                persist_full_raw_transport=True,
            )
            parent_binding = {
                "network_run_id": "parent-run-001",
                "run_claim_sha256": "1" * 64,
                "receipt_sha256": "0" * 64,
                "report_sha256": "2" * 64,
                "pit_coverage_report_sha256": "3" * 64,
                "pit_manifest_sha256": "4" * 64,
                "runtime_bundle_sha256": "5" * 64,
                "experiment_config_canonical_sha256": "6" * 64,
            }
            receipt = taf._self_hashed(
                {
                    "network_run_id": parent_binding["network_run_id"],
                    "collection_plan_sha256": self.task.plan_sha256,
                    "run_claim_sha256": parent_binding["run_claim_sha256"],
                    "report_sha256": parent_binding["report_sha256"],
                    "locked_test_status": dict(LOCKED_TEST_STATUS),
                    "locked_test_consumed": False,
                },
                "receipt_sha256",
            )
            parent_binding["receipt_sha256"] = receipt["receipt_sha256"]
            (parent / "p1_5_run_receipt.json").write_bytes(
                canonical_json_bytes(receipt)
            )
            response_bytes_on_disk = parent_store.response_path(self.task).read_bytes()
            response = json.loads(response_bytes_on_disk)
            reuse = {
                "ordinal": 1,
                "request_fingerprint": self.task.task_id,
                "task_id": self.task.task_id,
                "task_sha256": canonical_sha256(self.task.to_dict()),
                "endpoint": self.task.endpoint,
                "month": "2017-12",
                "params": dict(self.task.params),
                "provenance_kind": "network",
                "started_artifact_sha256": hashlib.sha256(
                    parent_store.started_path(self.task).read_bytes()
                ).hexdigest(),
                "import_artifact_sha256": None,
                "raw_artifact_sha256": hashlib.sha256(raw).hexdigest(),
                "response_file_sha256": hashlib.sha256(
                    response_bytes_on_disk
                ).hexdigest(),
                "response_artifact_sha256": response["response_artifact_sha256"],
                "normalized_content_sha256": parent_result.normalized_content_sha256,
                "attempt_artifact_sha256_by_number": [
                    hashlib.sha256(
                        parent_store.attempt_path(self.task, 1).read_bytes()
                    ).hexdigest()
                ],
                "network_request_count": 1,
            }
            imported = taf.import_parent_reuse_task_v2(
                task=self.task,
                parent_root=parent,
                child_root=child,
                parent_binding=parent_binding,
                reuse_evidence=reuse,
            )
            child_store = CreateOnlyTaskStore(child)
            self.assertTrue(child_store.is_complete(self.task))
            self.assertEqual(imported.request_origin, "offline_parent_run_reuse")
            self.assertEqual(imported.network_request_count, 0)
            self.assertEqual(child_store.raw_path(self.task).read_bytes(), raw)
            self.assertEqual(
                child_store.response_path(self.task).read_bytes(),
                response_bytes_on_disk,
            )
            self.assertEqual(
                taf.actual_tushare_request_count_by_endpoint(
                    child,
                    self.plan.pit_tasks,
                    plan_sha256=self.plan.plan_sha256,
                )["index_weight"],
                0,
            )
            marker_path = child_store.import_path(self.task)
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            self.assertEqual(
                marker["schema_version"],
                "tushare-alpha-feasibility-task-import.v2",
            )
            self.assertEqual(
                marker["parent_binding"]["receipt_sha256"],
                receipt["receipt_sha256"],
            )
            marker["parent_raw_artifact_sha256"] = "f" * 64
            unsigned = dict(marker)
            unsigned.pop("import_artifact_sha256")
            marker["import_artifact_sha256"] = canonical_sha256(unsigned)
            marker_path.write_bytes(canonical_json_bytes(marker))
            with self.assertRaisesRegex(
                AlphaFeasibilityDataError,
                "parent_reuse_import_semantics_mismatch",
            ):
                child_store._load_response(self.task)


class PitMembershipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = load_config_and_build_plan(CONFIG)
        cls.members = tuple(f"{index:06d}.SZ" for index in range(1, 801))

    def valid_rows(self, task):
        snapshot = datetime.strptime(task.params["end_date"], "%Y%m%d").date()
        while snapshot.weekday() >= 5:
            snapshot -= timedelta(days=1)
        return [
            {
                "index_code": "000906.SH",
                "con_code": code,
                "trade_date": snapshot.strftime("%Y%m%d"),
                "weight": "0.125",
            }
            for code in self.members
        ]

    def test_73_legal_months_produce_union_and_causal_manifest(self):
        rows = {task.task_id: self.valid_rows(task) for task in self.plan.pit_tasks}
        result = build_pit_membership_artifacts(self.plan, rows, generated_at=NOW)
        self.assertTrue(result.passed)
        self.assertEqual(result.coverage_report["pit_months_expected"], 73)
        self.assertEqual(result.coverage_report["pit_months_observed"], 73)
        self.assertEqual(len(result.manifest["snapshots"]), 73)
        self.assertEqual(len(result.union_instruments), 800)
        validate_json_schema(
            result.coverage_report, ROOT / "schemas" / "pit_membership_coverage_report.v2.json"
        )
        # The local Schema validator's generic uniqueItems check is quadratic;
        # validate one full 800-member snapshot rather than repeating the same
        # structural check across all 73 months.
        schema_manifest = dict(result.manifest)
        schema_manifest["snapshots"] = [result.manifest["snapshots"][0]]
        validate_json_schema(
            schema_manifest, ROOT / "schemas" / "pit_membership_manifest.v2.json"
        )
        self.assertIsNone(
            result.manifest["snapshots"][0]["component_count_adjustment_evidence"]
        )
        decision = select_pit_snapshot_on_or_before(
            result.manifest["snapshots"], "2018-01-15"
        )
        self.assertEqual(decision["snapshot_date"], "2017-12-29")

    def test_mixed_decimal_scales_produce_schema_valid_manifest(self):
        weights = ["98.1", *(["0.1"] * 5), *(["0.01"] * 67), *(["0.001"] * 727)]
        rows = {}
        for task in self.plan.pit_tasks:
            rows[task.task_id] = [
                {**row, "weight": weight}
                for row, weight in zip(self.valid_rows(task), weights, strict=True)
            ]
        result = build_pit_membership_artifacts(self.plan, rows, generated_at=NOW)

        self.assertTrue(result.passed)
        self.assertEqual(result.manifest["snapshots"][0]["weight_sum"], "99.997")
        self.assertEqual(result.manifest["snapshots"][0]["weight_tolerance"], "0.05")
        schema_manifest = dict(result.manifest)
        schema_manifest["snapshots"] = [result.manifest["snapshots"][0]]
        validate_json_schema(
            schema_manifest,
            ROOT / "schemas" / "pit_membership_manifest.v2.json",
        )

    def test_scale_seven_tolerance_is_plain_and_schema_valid(self):
        rows = {
            task.task_id: [
                {**row, "weight": "0.1250000"}
                for row in self.valid_rows(task)
            ]
            for task in self.plan.pit_tasks
        }
        result = build_pit_membership_artifacts(self.plan, rows, generated_at=NOW)

        self.assertTrue(result.passed)
        first = result.coverage_report["monthly_checks"][0]["snapshots"][0]
        self.assertEqual(first["weight_sum"], "100.0000000")
        self.assertEqual(first["weight_tolerance"], "0.00000005")
        validate_json_schema(
            result.coverage_report,
            ROOT / "schemas" / "pit_membership_coverage_report.v2.json",
        )
        schema_manifest = dict(result.manifest)
        schema_manifest["snapshots"] = [result.manifest["snapshots"][0]]
        validate_json_schema(
            schema_manifest,
            ROOT / "schemas" / "pit_membership_manifest.v2.json",
        )
        oversized = deepcopy(schema_manifest)
        oversized["snapshots"][0]["members"][0]["weight"] = (
            "3.343" + "0" * 997
        )
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(
                oversized,
                ROOT / "schemas" / "pit_membership_manifest.v2.json",
            )

    def test_invalid_snapshot_evidence_uses_exact_sum_and_fixed_tolerance(self):
        cases = (
            (["0"] * 600 + ["1"] * 200, "200", taf.Decimal("0.5")),
            (
                [
                    "99.999999999999999999999999999301",
                    *(["0.000000000000000000000000000001"] * 799),
                ],
                "100.000000000000000000000000000100",
                taf.Decimal("5e-31"),
            ),
        )
        for weights, expected_total, expected_tolerance in cases:
            with self.subTest(expected_total=expected_total):
                rows = {
                    task.task_id: self.valid_rows(task)
                    for task in self.plan.pit_tasks
                }
                first_task = self.plan.pit_tasks[0]
                rows[first_task.task_id] = [
                    {**row, "weight": weight}
                    for row, weight in zip(
                        self.valid_rows(first_task), weights, strict=True
                    )
                ]
                result = build_pit_membership_artifacts(
                    self.plan,
                    rows,
                    generated_at=NOW,
                )

                self.assertFalse(result.passed)
                snapshot = result.coverage_report["monthly_checks"][0]["snapshots"][0]
                self.assertEqual(snapshot["weight_sum"], expected_total)
                self.assertEqual(
                    taf.Decimal(snapshot["weight_tolerance"]),
                    expected_tolerance,
                )
                self.assertNotIn("E", snapshot["weight_tolerance"])
                validate_json_schema(
                    result.coverage_report,
                    ROOT / "schemas" / "pit_membership_coverage_report.v2.json",
                )

    def test_large_invalid_snapshot_sum_stays_inside_evidence_schema_bound(self):
        rows = {
            task.task_id: self.valid_rows(task)
            for task in self.plan.pit_tasks
        }
        first_task = self.plan.pit_tasks[0]
        snapshot_date = self.valid_rows(first_task)[0]["trade_date"]
        rows[first_task.task_id] = [
            {
                "index_code": "000906.SH",
                "con_code": f"{600000 + index:06d}.SH",
                "trade_date": snapshot_date,
                "weight": "9" * 1000,
            }
            for index in range(1001)
        ]
        result = build_pit_membership_artifacts(
            self.plan,
            rows,
            generated_at=NOW,
        )

        self.assertFalse(result.passed)
        snapshot = result.coverage_report["monthly_checks"][0]["snapshots"][0]
        self.assertEqual(len(snapshot["weight_sum"]), 1004)
        self.assertEqual(snapshot["weight_tolerance"], "0.5")
        validate_json_schema(
            result.coverage_report,
            ROOT / "schemas" / "pit_membership_coverage_report.v2.json",
        )

    def test_missing_month_blocks_and_does_not_form_union(self):
        rows = {task.task_id: self.valid_rows(task) for task in self.plan.pit_tasks[1:]}
        result = build_pit_membership_artifacts(self.plan, rows, generated_at=NOW)
        self.assertFalse(result.passed)
        self.assertEqual(result.coverage_report["stage_status"], "BLOCKED_PIT_MEMBERSHIP")
        self.assertEqual(result.coverage_report["terminal_status"], "BLOCKED_DATA")
        self.assertEqual(result.union_instruments, ())

    def test_duplicate_component_and_bad_weight_sum_block_month(self):
        rows = {task.task_id: self.valid_rows(task) for task in self.plan.pit_tasks}
        first = self.plan.pit_tasks[0]
        broken = list(rows[first.task_id])
        broken[1] = dict(broken[0])
        rows[first.task_id] = broken
        result = build_pit_membership_artifacts(self.plan, rows, generated_at=NOW)
        self.assertFalse(result.passed)
        self.assertEqual(result.coverage_report["monthly_checks"][0]["status"], "invalid")

    def test_caller_self_assertion_cannot_waive_non_800_without_frozen_registry(self):
        rows = {task.task_id: self.valid_rows(task) for task in self.plan.pit_tasks}
        first = self.plan.pit_tasks[0]
        rows[first.task_id] = rows[first.task_id][:-1]
        rows[first.task_id][-1] = {
            **rows[first.task_id][-1],
            "weight": "0.250",
        }
        unsigned = {
            "schema_version": "controlled-index-company-adjustment-evidence.v1",
            "source": "CSI_INDEX_COMPANY_OFFICIAL",
            "month": "2017-12",
            "snapshot_date": "2017-12-29",
            "observed_component_count": 799,
            "reason": "claimed adjustment",
            "source_document_sha256": "a" * 64,
        }
        evidence = dict(unsigned)
        from research.market_data.tushare_alpha_feasibility import canonical_sha256

        evidence["evidence_sha256"] = canonical_sha256(unsigned)
        result = build_pit_membership_artifacts(self.plan, rows, generated_at=NOW)
        self.assertFalse(result.passed)
        with self.assertRaisesRegex(
            AlphaFeasibilityDataError, "controlled_adjustment_evidence_not_supported"
        ):
            build_pit_membership_artifacts(
                self.plan,
                rows,
                adjustment_evidence={"2017-12": evidence},
                generated_at=NOW,
            )

    def test_all_legal_same_month_snapshots_feed_union_and_causal_lookup(self):
        rows = {task.task_id: self.valid_rows(task) for task in self.plan.pit_tasks}
        first = self.plan.pit_tasks[0]
        early_members = list(self.valid_rows(first))
        for item in early_members:
            item["trade_date"] = "20171215"
        early_members[-1] = {
            **early_members[-1],
            "con_code": "000801.SZ",
        }
        rows[first.task_id] = [*early_members, *rows[first.task_id]]
        result = build_pit_membership_artifacts(self.plan, rows, generated_at=NOW)
        self.assertTrue(result.passed)
        self.assertEqual(len(result.manifest["snapshots"]), 74)
        self.assertEqual(len(result.union_instruments), 801)
        self.assertIn("000801.SZ", result.union_instruments)
        early = select_pit_snapshot_on_or_before(
            result.manifest["snapshots"], "2017-12-20"
        )
        self.assertEqual(early["snapshot_date"], "2017-12-15")
        early_codes = {member["instrument_id"] for member in early["members"]}
        self.assertIn("000801.SZ", early_codes)
        self.assertNotIn("000800.SZ", early_codes)

    def test_future_snapshot_cannot_backfill_prior_decision(self):
        snapshots = [{"snapshot_date": "20180131", "members": ["600000.SH"]}]
        with self.assertRaisesRegex(
            AlphaFeasibilityDataError, "no_pit_snapshot_on_or_before_decision_date"
        ):
            select_pit_snapshot_on_or_before(snapshots, "2018-01-01")


class TotalReturnAndCoverageTests(unittest.TestCase):
    def setUp(self):
        self.plan = load_config_and_build_plan(CONFIG)
        self.basic = ["600000.SH"]
        self.daily = [
            {
                "ts_code": "600000.SH",
                "trade_date": "20170703",
                "open": "100",
                "high": "101",
                "low": "99",
                "close": "100",
                "pre_close": "100",
                "vol": "1",
                "amount": "1",
            },
            {
                "ts_code": "600000.SH",
                "trade_date": "20170704",
                "open": "50",
                "high": "50.5",
                "low": "49.5",
                "close": "50",
                "pre_close": "100",
                "vol": "1",
                "amount": "1",
            },
        ]
        self.factors = [
            {"ts_code": "600000.SH", "trade_date": "20170703", "adj_factor": "1"},
            {"ts_code": "600000.SH", "trade_date": "20170704", "adj_factor": "2"},
        ]
        self.suspensions = [
            {
                "ts_code": "600000.SH",
                "trade_date": "20170705",
                "suspend_timing": None,
                "suspend_type": "S",
            }
        ]

    def test_adjusted_value_removes_mechanical_split_jump_and_suspension_carries(self):
        panel = build_total_return_panel(
            ["20170703", "20170704", "20170705"],
            self.basic,
            self.daily,
            self.factors,
            self.suspensions,
        )
        self.assertEqual(panel[0]["raw_close"], "100")
        self.assertEqual(panel[0]["close"], "100")
        self.assertEqual(panel[0]["open"], "100")
        self.assertEqual(panel[1]["raw_close"], "50")
        self.assertEqual(panel[1]["close"], "100")
        self.assertEqual(panel[1]["daily_total_return"], "0")
        self.assertTrue(panel[2]["is_suspended_carry"])
        self.assertEqual(panel[2]["close"], "100")
        self.assertEqual(panel[2]["open"], "100")
        self.assertEqual(panel[2]["daily_total_return"], "0")

        same_day_bar = [
            *self.daily,
            {
                "ts_code": "600000.SH",
                "trade_date": "20170705",
                "open": "60",
                "high": "61",
                "low": "59",
                "close": "60",
                "pre_close": "50",
                "vol": "1",
                "amount": "1",
            },
        ]
        same_day_factor = [
            *self.factors,
            {"ts_code": "600000.SH", "trade_date": "20170705", "adj_factor": "2"},
        ]
        with self.assertRaisesRegex(
            AlphaFeasibilityDataError, "suspension_daily_bar_conflict"
        ):
            build_total_return_panel(
                ["20170703", "20170704", "20170705"],
                self.basic,
                same_day_bar,
                same_day_factor,
                self.suspensions,
            )

        leading_suspension = build_total_return_panel(
            ["20170703", "20170704"],
            self.basic,
            [{**self.daily[0], "vol": "0"}, self.daily[1]],
            self.factors,
            [
                {
                    "ts_code": "600000.SH",
                    "trade_date": "20170703",
                    "suspend_timing": None,
                    "suspend_type": "S",
                }
            ],
        )
        self.assertEqual(
            [row["trading_date"] for row in leading_suspension],
            ["2017-07-04"],
        )

        no_initial_price = build_total_return_panel(
            ["20170703", "20170704"],
            self.basic,
            [],
            [],
            [
                {
                    "ts_code": "600000.SH",
                    "trade_date": trading_date,
                    "suspend_timing": None,
                    "suspend_type": "S",
                }
                for trading_date in ("20170703", "20170704")
            ],
        )
        self.assertEqual(no_initial_price, [])

    def test_non_suspension_missing_daily_fails_closed(self):
        later_bar = {
            **self.daily[-1],
            "trade_date": "20170705",
        }
        later_factor = {
            **self.factors[-1],
            "trade_date": "20170705",
        }
        with self.assertRaisesRegex(
            AlphaFeasibilityDataError, "unexplained_market_data_gap"
        ):
            build_total_return_panel(
                ["20170703", "20170704", "20170705"],
                self.basic,
                [self.daily[0], later_bar],
                [self.factors[0], later_factor],
                [],
            )

    def test_intraday_suspension_cannot_explain_a_missing_daily_bar(self):
        later_bar = {**self.daily[-1], "trade_date": "20170705"}
        later_factor = {**self.factors[-1], "trade_date": "20170705"}
        intraday = [
            {
                "ts_code": "600000.SH",
                "trade_date": "20170704",
                "suspend_timing": "09:30-10:30",
                "suspend_type": "S",
            }
        ]
        with self.assertRaisesRegex(
            AlphaFeasibilityDataError, "unexplained_market_data_gap"
        ):
            build_total_return_panel(
                ["20170703", "20170704", "20170705"],
                self.basic,
                [self.daily[0], later_bar],
                [self.factors[0], later_factor],
                intraday,
            )

    def test_history_coverage_accepts_only_same_day_suspension_explanation(self):
        calendar_rows = []
        cursor = date(2017, 7, 1)
        terminal = date(2023, 12, 31)
        first_open = date(2017, 7, 3)
        last_open = None
        open_dates = []
        while cursor <= terminal:
            # A deterministic complete calendar fixture: weekdays except the
            # first calendar day of a month (standing in for controlled market
            # holidays).  This yields a realistic 200-260 sessions per full
            # year without pretending two isolated dates are full coverage.
            is_open = cursor.weekday() < 5 and cursor.day != 1
            pretrade = date(2017, 6, 30) if last_open is None else last_open
            calendar_rows.append(
                {
                    "exchange": "SSE",
                    "cal_date": cursor.strftime("%Y%m%d"),
                    "is_open": int(is_open),
                    "pretrade_date": pretrade.strftime("%Y%m%d"),
                }
            )
            if is_open:
                last_open = cursor
                open_dates.append(cursor)
            cursor += timedelta(days=1)
        daily_rows = [
            {
                "ts_code": "600000.SH",
                "trade_date": item.strftime("%Y%m%d"),
                "open": "10",
                "high": "10.1",
                "low": "9.9",
                "close": "10",
                "pre_close": "10",
                "vol": "1",
                "amount": "1",
            }
            for item in open_dates
            if item != open_dates[-2]
        ]
        first_by_month = {}
        for item in open_dates:
            first_by_month.setdefault(item.strftime("%Y-%m"), item)
        pit_snapshots = [
            {
                "snapshot_date": first_by_month[month].isoformat(),
                "members": [{"instrument_id": "600000.SH", "weight": "100.000"}],
            }
            for month in taf._month_sequence("2017-12", "2023-12")
        ]
        rows = {
            "trade_cal": calendar_rows,
            "daily": daily_rows,
            "adj_factor": [
                {
                    "ts_code": "600000.SH",
                    "trade_date": item.strftime("%Y%m%d"),
                    "adj_factor": "1",
                }
                for item in open_dates
                if item != open_dates[-2]
            ],
            "suspend_d": [
                {
                    "ts_code": "600000.SH",
                    "trade_date": open_dates[-2].strftime("%Y%m%d"),
                    "suspend_timing": None,
                    "suspend_type": "S",
                }
            ],
            "index_daily": [
                {"ts_code": "000906.SH", "trade_date": item.strftime("%Y%m%d")}
                for item in open_dates
            ],
        }
        result = validate_history_coverage(
            self.plan,
            ["600000.SH"],
            rows,
            pit_snapshots=pit_snapshots,
            generated_at=NOW,
        )
        self.assertTrue(result.passed)
        self.assertEqual(
            result.report["same_day_suspension_explained_missing_daily_count"], 1
        )
        broken = deepcopy(rows)
        broken["suspend_d"] = []
        blocked = validate_history_coverage(
            self.plan,
            ["600000.SH"],
            broken,
            pit_snapshots=pit_snapshots,
            generated_at=NOW,
        )
        self.assertFalse(blocked.passed)
        self.assertEqual(blocked.report["daily_coverage_status"], "BLOCKED_DATA")
        intraday = deepcopy(rows)
        intraday["suspend_d"][0]["suspend_timing"] = "09:30-10:30"
        intraday_blocked = validate_history_coverage(
            self.plan,
            ["600000.SH"],
            intraday,
            pit_snapshots=pit_snapshots,
            generated_at=NOW,
        )
        self.assertFalse(intraday_blocked.passed)
        truncated_factor = deepcopy(rows)
        del truncated_factor["adj_factor"][0]
        factor_blocked = validate_history_coverage(
            self.plan,
            ["600000.SH"],
            truncated_factor,
            pit_snapshots=pit_snapshots,
            generated_at=NOW,
        )
        self.assertFalse(factor_blocked.passed)
        self.assertEqual(
            factor_blocked.report["adj_factor_coverage_status"], "BLOCKED_DATA"
        )


if __name__ == "__main__":
    unittest.main()
