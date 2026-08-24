from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from research.market_data.providers.base import (
    ProviderNotConfiguredError,
)
from research.market_data.tushare_capability import (
    ClassifiedEndpointError,
    ENDPOINT_ORDER,
    SDK_METHOD_BY_ENDPOINT,
    CrossValidationOutcomeV1,
    Endpoint,
    EndpointResultV1,
    EndpointSpec,
    TushareCapabilityConfigError,
    TushareCapabilityError,
    TushareCapabilityReceiptV1,
    TushareEndpointPayloadError,
    TusharePermissionDeniedError,
    TushareRateLimitedError,
    build_capability_receipt,
    build_cross_validation_outcome,
    build_endpoint_result,
    canonical_json_bytes,
    canonical_sha256,
    classify_endpoint_error,
    load_probe_config,
    normalize_endpoint_result,
    normalize_parameters,
    replay_capability_receipt,
    replay_endpoint_raw,
    strict_json_loads,
    validate_endpoint_result_schema,
    verify_capability_receipt,
)
from research.market_data.validation import (
    SchemaValidationError,
    validate_json_schema,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "configs" / "tushare_capability_probe.v1.json"
RECEIPT_SCHEMA = ROOT / "schemas" / "tushare_capability_receipt.v1.json"
TZ = timezone(timedelta(hours=8))
STARTED = datetime(2026, 8, 24, 9, 0, tzinfo=TZ)
COMPLETED = datetime(2026, 8, 24, 9, 1, tzinfo=TZ)
SHA = "a" * 64


class FakeDataFrame:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows
        self.columns = tuple(
            sorted({field for row in rows for field in row})
        )

    def to_dict(self, orient: str = "dict") -> list[dict[str, object]]:
        if orient != "records":
            raise AssertionError("probe must request records orientation")
        return [dict(row) for row in self._rows]


def _normalize(endpoint: str, rows: list[dict[str, object]], call: int = 0):
    config = load_probe_config(CONFIG_PATH)
    spec = config.spec_for(endpoint)
    return spec, normalize_endpoint_result(spec, FakeDataFrame(rows), spec.parameters[call])


def _build(spec: EndpointSpec, normalized, call: int = 0):
    return build_endpoint_result(
        spec,
        requested_at=STARTED,
        completed_at=COMPLETED,
        sanitized_parameters=spec.parameters[call],
        normalized=normalized,
    )


def _not_configured_receipt():
    config = load_probe_config(CONFIG_PATH)
    results = tuple(
        build_endpoint_result(
            spec,
            requested_at=STARTED,
            completed_at=COMPLETED,
            sanitized_parameters=parameters,
            request_count=0,
            error=ProviderNotConfiguredError("TUSHARE_TOKEN is not configured"),
        )
        for spec, parameters in config.planned_calls()
    )
    cross_validation = build_cross_validation_outcome(
        results,
        status="not_attempted",
        comparison_payload_sha256=canonical_sha256(
            {"status": "cross_validation_not_configured"}
        ),
        not_attempted_reason="daily_not_passed",
    )
    receipt = build_capability_receipt(
        config,
        probe_run_id="tushare-capability-20260824T090000",
        started_at=STARTED,
        completed_at=COMPLETED,
        sdk_version=None,
        python_version="3.12.13",
        credential_status="not_configured",
        probe_code_sha256=SHA,
        git_commit="b473d62f50c15f635e1aebdf5110eb3e63191e12",
        git_worktree_status="dirty",
        endpoint_results=results,
        cross_validation_outcome=cross_validation,
        raw_evidence_manifest_sha256=canonical_sha256([]),
        request_count=0,
    )
    return config, receipt


def _compared_receipt():
    config = load_probe_config(CONFIG_PATH)
    daily_rows = [
        {
            "ts_code": "000333.SZ",
            "trade_date": "20260804",
            "open": 50,
            "high": 51,
            "low": 49,
            "close": 50,
            "pre_close": 49,
            "vol": 100,
            "amount": 5000,
        }
    ]
    results = []
    for spec, parameters in config.planned_calls():
        if spec.endpoint is Endpoint.DAILY:
            normalized = normalize_endpoint_result(
                spec,
                FakeDataFrame(daily_rows),
                parameters,
            )
            result = build_endpoint_result(
                spec,
                requested_at=STARTED,
                completed_at=COMPLETED,
                sanitized_parameters=parameters,
                normalized=normalized,
            )
        else:
            result = build_endpoint_result(
                spec,
                requested_at=STARTED,
                completed_at=COMPLETED,
                sanitized_parameters=parameters,
                request_count=0,
                error=ProviderNotConfiguredError("not used by contract fixture"),
            )
        results.append(result)
    outcome = build_cross_validation_outcome(
        results,
        status="compared",
        comparison_payload_sha256=canonical_sha256(
            {"status": "compared_no_threshold", "overlap_count": 1}
        ),
        tushare_raw_path="raw/daily.01.json",
        baostock_raw_path="raw/cross_validation/baostock_daily.json",
        baostock_raw_sha256="b" * 64,
    )
    receipt = build_capability_receipt(
        config,
        probe_run_id="tushare-cross-validation-contract",
        started_at=STARTED,
        completed_at=COMPLETED,
        sdk_version="1.4.24",
        python_version="3.12.13",
        credential_status="configured",
        probe_code_sha256=SHA,
        git_commit="b473d62f50c15f635e1aebdf5110eb3e63191e12",
        git_worktree_status="dirty",
        endpoint_results=results,
        cross_validation_outcome=outcome,
        raw_evidence_manifest_sha256="c" * 64,
    )
    return config, receipt


class ProbeConfigContractTests(unittest.TestCase):
    def test_fixed_allowlist_mapping_and_bounded_request_plan(self) -> None:
        config = load_probe_config(CONFIG_PATH)
        self.assertEqual(tuple(item.endpoint for item in config.endpoints), ENDPOINT_ORDER)
        self.assertEqual(set(SDK_METHOD_BY_ENDPOINT), set(Endpoint))
        self.assertEqual(
            [spec.sdk_method for spec in config.endpoints],
            [endpoint.value for endpoint in ENDPOINT_ORDER],
        )
        self.assertEqual(config.planned_request_count, 37)
        self.assertEqual(config.cross_validation_request_reserve, 1)
        self.assertLessEqual(
            config.planned_request_count + config.cross_validation_request_reserve,
            config.maximum_request_count,
        )
        self.assertEqual(config.global_stop_after_consecutive_rate_limits, 3)
        self.assertTrue(config.global_stop_on_permission_denied)
        self.assertTrue(config.spec_for("fina_indicator").cross_validation_only)
        self.assertEqual(config.spec_for("daily").parameters[0]["ts_code"], "000333.SZ")

    def test_config_cannot_add_method_or_forbidden_endpoint(self) -> None:
        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        payload["endpoints"][0]["sdk_method"] = "factor_value"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                TushareCapabilityConfigError,
                "fields differ",
            ):
                load_probe_config(path)

        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        payload["endpoints"][0]["endpoint"] = "factor_value"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                TushareCapabilityConfigError,
                "fixed allowlist",
            ):
                load_probe_config(path)

    def test_parameters_reject_credentials_paths_dates_and_unknown_kwargs(self) -> None:
        with self.assertRaises(TushareCapabilityConfigError):
            normalize_parameters("daily", {"token": "do-not-store"})
        with self.assertRaises(TushareCapabilityConfigError):
            normalize_parameters("index_basic", {"name": "C:\\secret\\token.txt"})
        with self.assertRaises(TushareCapabilityConfigError):
            normalize_parameters("daily", {"start_date": "20260230"})
        with self.assertRaises(TushareCapabilityConfigError):
            normalize_parameters("daily", {"fields": "ts_code,trade_date"})

    def test_strict_json_rejects_duplicate_keys_and_nonfinite_constants(self) -> None:
        with self.assertRaisesRegex(TushareCapabilityError, "duplicate JSON key"):
            strict_json_loads('{"a":1,"a":2}')
        with self.assertRaisesRegex(TushareCapabilityError, "non-finite"):
            strict_json_loads('{"a":NaN}')
        with self.assertRaisesRegex(TushareCapabilityError, "non-finite"):
            canonical_json_bytes({"a": float("inf")})


class EndpointNormalizationTests(unittest.TestCase):
    def test_daily_normalization_hashes_units_and_allows_new_fields(self) -> None:
        rows = [
            {
                "ts_code": "000333.SZ",
                "trade_date": "20260804",
                "open": 50.0,
                "high": 51.0,
                "low": 49.0,
                "close": 50.5,
                "pre_close": 49.5,
                "vol": 100.0,
                "amount": 5000.0,
                "new_upstream_field": "kept",
            }
        ]
        spec, normalized = _normalize("daily", rows)
        result = _build(spec, normalized)
        self.assertEqual(result.status, "passed")
        self.assertIn("new_upstream_field", result.field_names)
        self.assertEqual(result.row_count, 1)
        self.assertEqual(result.response_shape, "dataframe_records")
        self.assertEqual(result.diagnostics["kind"], "generic")
        validate_endpoint_result_schema(result)
        replayed = replay_endpoint_raw(
            spec,
            normalized.raw_payload,
            expected_result=result,
        )
        self.assertEqual(replayed.raw_payload_sha256, normalized.raw_payload_sha256)

        changed = deepcopy(rows)
        changed[0]["close"] = 50.6
        _, changed_normalized = _normalize("daily", changed)
        self.assertNotEqual(
            normalized.raw_payload_sha256,
            changed_normalized.raw_payload_sha256,
        )
        with self.assertRaisesRegex(TushareCapabilityError, "do not replay"):
            replay_endpoint_raw(
                spec,
                changed_normalized.raw_payload,
                expected_result=result,
            )

    def test_empty_dataframe_is_structured_but_null_and_non_dataframe_fail(self) -> None:
        config = load_probe_config(CONFIG_PATH)
        spec = config.spec_for("daily")
        normalized = normalize_endpoint_result(
            spec,
            FakeDataFrame([]),
            spec.parameters[0],
        )
        result = _build(spec, normalized)
        self.assertEqual(result.status, "empty_result")
        self.assertEqual(result.row_count, 0)
        self.assertIsNotNone(result.raw_payload_sha256)
        for bad in (None, "<html>error</html>", {"rows": []}, [rows := {}]):
            with self.subTest(type=type(bad).__name__):
                with self.assertRaises(TushareEndpointPayloadError):
                    normalize_endpoint_result(spec, bad, spec.parameters[0])

    def test_missing_column_duplicate_key_null_key_invalid_date_and_nan(self) -> None:
        base = {
            "ts_code": "000333.SZ",
            "trade_date": "20260804",
            "open": 50,
            "high": 51,
            "low": 49,
            "close": 50,
            "pre_close": 49,
            "vol": 100,
            "amount": 5000,
        }
        missing = dict(base)
        missing.pop("pre_close")
        spec, normalized = _normalize("daily", [missing])
        self.assertEqual(_build(spec, normalized).status, "schema_drift")

        _, duplicate = _normalize("daily", [dict(base), dict(base)])
        duplicate_result = _build(spec, duplicate)
        self.assertEqual(duplicate_result.status, "invalid_payload")
        self.assertEqual(duplicate_result.duplicate_key_count, 1)

        null_key = dict(base)
        null_key["ts_code"] = None
        _, null_normalized = _normalize("daily", [null_key])
        null_result = _build(spec, null_normalized)
        self.assertEqual(null_result.status, "invalid_payload")
        self.assertEqual(null_result.failure_code, "candidate_primary_key_null")
        self.assertEqual(
            null_result.diagnostics["candidate_primary_key_null_count"],
            1,
        )

        invalid_date = dict(base)
        invalid_date["trade_date"] = "20260230"
        with self.assertRaisesRegex(TushareEndpointPayloadError, "invalid date"):
            _normalize("daily", [invalid_date])
        nonfinite = dict(base)
        nonfinite["close"] = float("nan")
        with self.assertRaisesRegex(TushareEndpointPayloadError, "non-finite"):
            _normalize("daily", [nonfinite])

    def test_index_basic_candidates_and_index_weight_snapshot_diagnostics(self) -> None:
        index_rows = [
            {
                "ts_code": "000906.SH",
                "name": "中证800",
                "market": "CSI",
                "publisher": "中证指数有限公司",
                "category": "规模指数",
                "base_date": "20041231",
                "list_date": "20070115",
            },
            {
                "ts_code": "H00906.CSI",
                "name": "中证800全收益",
                "market": "CSI",
                "publisher": "中证指数有限公司",
                "category": "全收益指数",
                "base_date": "20041231",
                "list_date": "20070115",
            },
        ]
        spec, normalized = _normalize("index_basic", index_rows)
        result = _build(spec, normalized)
        self.assertEqual(result.status, "passed")
        candidates = result.diagnostics["index_basic_candidates"]
        self.assertEqual(len(candidates), 2)
        self.assertTrue(candidates[1]["is_total_return_candidate"])

        weight_rows = [
            {
                "index_code": "000906.SH",
                "con_code": "000001.SZ",
                "trade_date": "20240131",
                "weight": "60",
            },
            {
                "index_code": "000906.SH",
                "con_code": "000002.SZ",
                "trade_date": "20240131",
                "weight": "40",
            },
        ]
        weight_spec, weight_normalized = _normalize("index_weight", weight_rows)
        weight_result = _build(weight_spec, weight_normalized)
        snapshot = weight_result.diagnostics["index_weight_snapshots"][0]
        self.assertEqual(snapshot["unique_component_count"], 2)
        self.assertEqual(snapshot["duplicate_component_count"], 0)
        self.assertEqual(snapshot["weight_sum"], "100")
        self.assertEqual(snapshot["comparison_to_100"], "equal")

    def test_industry_and_financial_coverage_drive_pit_without_granting_admission(self) -> None:
        member_rows = [
            {
                "l1_code": "801010.SI",
                "l1_name": "农林牧渔",
                "l2_code": "801011.SI",
                "l2_name": "种植业",
                "l3_code": "850111.SI",
                "l3_name": "种子",
                "ts_code": "000998.SZ",
                "name": "隆平高科",
                "in_date": "20210101",
                "out_date": None,
                "is_new": "Y",
            }
        ]
        spec, normalized = _normalize("index_member_all", member_rows, call=1)
        result = _build(spec, normalized, call=1)
        self.assertEqual(result.status, "passed")
        industry = result.diagnostics["industry_membership"]
        self.assertEqual(industry["industry_system"], "SW2021")
        self.assertEqual(industry["in_date_coverage"], "1")
        self.assertEqual(industry["out_date_coverage"], "0")
        self.assertEqual(result.pit_evidence_status, "candidate_fields_present_not_admitted")

        income_rows = [
            {
                "ts_code": "000333.SZ",
                "ann_date": "20250430",
                "f_ann_date": None,
                "end_date": "20241231",
                "report_type": "1",
                "comp_type": "1",
                "update_flag": "0",
            }
        ]
        income_spec, income_normalized = _normalize("income", income_rows)
        income_result = _build(income_spec, income_normalized)
        self.assertEqual(income_result.status, "passed")
        self.assertEqual(income_result.pit_evidence_status, "missing_pit_fields")
        financial = income_result.diagnostics["financial"]
        self.assertEqual(financial["ann_date_coverage"], "1")
        self.assertEqual(financial["f_ann_date_coverage"], "0")
        self.assertEqual(financial["earliest_report_period"], "2024-12-31")
        self.assertFalse(income_result.paper_eligibility if hasattr(income_result, "paper_eligibility") else False)

        disclosure_rows = [
            {
                "ts_code": "000333.SZ",
                "ann_date": "20250301",
                "end_date": "20241231",
                "pre_date": "20250331",
                "actual_date": None,
            }
        ]
        disclosure_spec, disclosure_normalized = _normalize(
            "disclosure_date", disclosure_rows
        )
        disclosure_result = _build(disclosure_spec, disclosure_normalized)
        self.assertEqual(disclosure_result.status, "passed")
        self.assertEqual(disclosure_result.pit_evidence_status, "missing_pit_fields")
        self.assertEqual(
            disclosure_result.diagnostics["financial"]["actual_date_coverage"],
            "0",
        )


class ErrorAndReceiptContractTests(unittest.TestCase):
    def test_pre_request_initialization_failures_are_zero_request_and_explicit(self) -> None:
        config = load_probe_config(CONFIG_PATH)
        spec = config.spec_for("daily")
        cases = (
            ClassifiedEndpointError(
                "permission_denied", "denied", "permission", "permission_denied"
            ),
            ClassifiedEndpointError(
                "rate_limited", "unknown", "rate_limit", "rate_limited"
            ),
            ClassifiedEndpointError(
                "network_blocked", "unknown", "network", "network_blocked"
            ),
            ClassifiedEndpointError(
                "failed", "unknown", "unexpected", "endpoint_failed"
            ),
        )
        for classified in cases:
            with self.subTest(status=classified.status):
                result = build_endpoint_result(
                    spec,
                    requested_at=STARTED,
                    completed_at=STARTED,
                    sanitized_parameters=spec.parameters[0],
                    request_count=0,
                    error=classified,
                )
                self.assertEqual(result.request_count, 0)
                self.assertEqual(
                    result.failure_stage,
                    "pre_request_initialization",
                )
                self.assertEqual(result.response_shape, "none")
                self.assertEqual(result.row_count, 0)
                validate_endpoint_result_schema(result)

    def test_failure_stage_prevents_zero_request_response_or_post_request_forgery(self) -> None:
        rows = [
            {
                "ts_code": "000333.SZ",
                "trade_date": "20260804",
                "open": 50,
                "high": 51,
                "low": 49,
                "close": 50,
                "pre_close": 49,
                "vol": 100,
                "amount": 5000,
            }
        ]
        spec, normalized = _normalize("daily", rows)
        passed = _build(spec, normalized)
        payload = passed.to_dict()
        payload["request_count"] = 0
        payload["failure_stage"] = "pre_request_initialization"
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(
                payload,
                ROOT / "schemas" / "tushare_endpoint_result.v1.json",
            )
        with self.assertRaisesRegex(
            TushareCapabilityError,
            "inconsistent|request_count=1",
        ):
            EndpointResultV1.from_dict(payload)

        initialization = build_endpoint_result(
            spec,
            requested_at=STARTED,
            completed_at=STARTED,
            sanitized_parameters=spec.parameters[0],
            request_count=0,
            error=ClassifiedEndpointError(
                "network_blocked", "unknown", "network", "network_blocked"
            ),
        )
        payload = initialization.to_dict()
        payload["request_count"] = 1
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(
                payload,
                ROOT / "schemas" / "tushare_endpoint_result.v1.json",
            )
        with self.assertRaisesRegex(
            TushareCapabilityError,
            "post-request failure",
        ):
            EndpointResultV1.from_dict(payload)

        global_stop = build_endpoint_result(
            spec,
            requested_at=STARTED,
            completed_at=STARTED,
            sanitized_parameters=spec.parameters[0],
            request_count=0,
            status="not_run_after_global_stop",
        )
        self.assertEqual(global_stop.failure_stage, "global_stop")
        payload = global_stop.to_dict()
        payload["failure_stage"] = "pre_request_initialization"
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(
                payload,
                ROOT / "schemas" / "tushare_endpoint_result.v1.json",
            )
        with self.assertRaises(TushareCapabilityError):
            EndpointResultV1.from_dict(payload)

    def test_full_initialization_rate_limit_receipt_keeps_request_count_zero(self) -> None:
        config = load_probe_config(CONFIG_PATH)
        classified = ClassifiedEndpointError(
            "rate_limited", "unknown", "rate_limit", "rate_limited"
        )
        results = tuple(
            build_endpoint_result(
                spec,
                requested_at=STARTED,
                completed_at=STARTED,
                sanitized_parameters=parameters,
                request_count=0,
                error=classified,
            )
            for spec, parameters in config.planned_calls()
        )
        cross_validation = build_cross_validation_outcome(
            results,
            status="not_attempted",
            comparison_payload_sha256=canonical_sha256(
                {"status": "cross_validation_not_configured"}
            ),
            not_attempted_reason="daily_not_passed",
        )
        receipt = build_capability_receipt(
            config,
            probe_run_id="initialization-rate-limit",
            started_at=STARTED,
            completed_at=COMPLETED,
            sdk_version="not_loaded",
            python_version="3.12.13",
            credential_status="configured",
            probe_code_sha256=SHA,
            git_commit="b473d62f50c15f635e1aebdf5110eb3e63191e12",
            git_worktree_status="dirty",
            endpoint_results=results,
            cross_validation_outcome=cross_validation,
            raw_evidence_manifest_sha256=canonical_sha256([]),
            request_count=0,
        )
        self.assertEqual(receipt.request_count, 0)
        self.assertEqual(receipt.rate_limit_events, 1)
        self.assertEqual(receipt.status, "failed")
        verify_capability_receipt(receipt, config=config)

    def test_error_classification_is_structured_and_discards_exception_text(self) -> None:
        secret = "super-secret-token-value"
        permission = classify_endpoint_error(
            RuntimeError(f"permission denied token={secret}")
        )
        self.assertEqual(permission.status, "permission_denied")
        self.assertNotIn(secret, repr(permission))
        self.assertEqual(
            classify_endpoint_error(TusharePermissionDeniedError("x")).status,
            "permission_denied",
        )
        self.assertEqual(
            classify_endpoint_error(TushareRateLimitedError("x")).status,
            "rate_limited",
        )
        self.assertEqual(
            classify_endpoint_error(ConnectionError("network down")).status,
            "network_blocked",
        )

    def test_receipt_schema_hash_and_canonical_semantic_replay(self) -> None:
        config, receipt = _not_configured_receipt()
        self.assertEqual(receipt.status, "not_configured")
        self.assertEqual(receipt.scope, "capability_probe_only_not_admitted")
        self.assertFalse(receipt.formal_data_admission)
        self.assertFalse(receipt.paper_eligibility)
        self.assertFalse(receipt.trade_eligibility)
        self.assertFalse(receipt.real_money_list_allowed)
        self.assertFalse(receipt.automatic_order_submission)
        self.assertFalse(receipt.live_supported)
        validate_json_schema(receipt.to_dict(), RECEIPT_SCHEMA)
        raw = canonical_json_bytes(receipt.to_dict())
        replayed = replay_capability_receipt(raw, config=config, as_of=COMPLETED)
        self.assertEqual(replayed.to_dict(), receipt.to_dict())
        self.assertEqual(
            verify_capability_receipt(receipt, config=config),
            receipt,
        )
        with self.assertRaisesRegex(TushareCapabilityError, "not canonical"):
            replay_capability_receipt(
                json.dumps(receipt.to_dict(), indent=2, ensure_ascii=False),
                config=config,
            )

    def test_compared_cross_validation_binds_daily_and_both_raw_artifacts(self) -> None:
        config, receipt = _compared_receipt()
        outcome = receipt.cross_validation_outcome
        self.assertIs(type(outcome), CrossValidationOutcomeV1)
        self.assertTrue(outcome.attempted)
        self.assertEqual(outcome.status, "compared")
        self.assertEqual(outcome.request_count, 1)
        self.assertEqual(
            outcome.baostock_raw_path,
            "raw/cross_validation/baostock_daily.json",
        )
        daily = next(
            item for item in receipt.endpoint_results if item.endpoint is Endpoint.DAILY
        )
        self.assertEqual(outcome.tushare_raw_sha256, daily.raw_payload_sha256)
        self.assertEqual(
            outcome.tushare_daily_endpoint_result_sha256,
            canonical_sha256(EndpointResultV1.to_dict(daily)),
        )
        self.assertEqual(receipt.request_count, 2)
        validate_json_schema(receipt.to_dict(), RECEIPT_SCHEMA)
        replayed = replay_capability_receipt(
            canonical_json_bytes(receipt.to_dict()),
            config=config,
            as_of=COMPLETED,
        )
        self.assertEqual(replayed.to_dict(), receipt.to_dict())

    def test_deleted_compared_raw_or_request_count_rewrite_fails_closed(self) -> None:
        config, receipt = _compared_receipt()
        payload = deepcopy(receipt.to_dict())
        payload["cross_validation_outcome"]["baostock_raw_path"] = None
        payload["cross_validation_outcome"]["baostock_raw_sha256"] = None
        content = deepcopy(payload)
        content.pop("receipt_sha256")
        payload["receipt_sha256"] = canonical_sha256(content)
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(payload, RECEIPT_SCHEMA)
        with self.assertRaisesRegex(TushareCapabilityError, "incomplete"):
            TushareCapabilityReceiptV1.from_dict(payload)

        payload = deepcopy(receipt.to_dict())
        payload["request_count"] -= 1
        content = deepcopy(payload)
        content.pop("receipt_sha256")
        payload["receipt_sha256"] = canonical_sha256(content)
        with self.assertRaisesRegex(TushareCapabilityError, "does not equal"):
            TushareCapabilityReceiptV1.from_dict(payload)

        # A fully rewritten local package can only downgrade this to an
        # explicit failure; it cannot retain a compared/success claim without
        # both raw hashes.  Local self-hashes are not historical signatures.
        daily = next(
            item for item in receipt.endpoint_results if item.endpoint is Endpoint.DAILY
        )
        failed = build_cross_validation_outcome(
            receipt.endpoint_results,
            status="failed",
            comparison_payload_sha256=canonical_sha256(
                {"status": "cross_validation_not_configured", "failure_code": "x"}
            ),
            tushare_raw_path="raw/daily.01.json",
            failure_code="baostock_capture_failed",
        )
        self.assertEqual(failed.status, "failed")
        self.assertIsNone(failed.baostock_raw_sha256)
        self.assertEqual(failed.tushare_raw_sha256, daily.raw_payload_sha256)
        self.assertEqual(failed.admission_effect, "none")
        self.assertEqual(
            failed.integrity_scope,
            "local_consistency_not_external_authentication",
        )

    def test_cross_validation_reason_and_daily_binding_replay_strictly(self) -> None:
        config, receipt = _not_configured_receipt()
        payload = deepcopy(receipt.to_dict())
        payload["cross_validation_outcome"]["not_attempted_reason"] = "global_stop"
        content = deepcopy(payload)
        content.pop("receipt_sha256")
        payload["receipt_sha256"] = canonical_sha256(content)
        with self.assertRaisesRegex(TushareCapabilityError, "does not replay"):
            TushareCapabilityReceiptV1.from_dict(payload)

        payload = deepcopy(receipt.to_dict())
        payload["cross_validation_outcome"][
            "tushare_daily_endpoint_result_sha256"
        ] = "d" * 64
        content = deepcopy(payload)
        content.pop("receipt_sha256")
        payload["receipt_sha256"] = canonical_sha256(content)
        with self.assertRaisesRegex(TushareCapabilityError, "hash mismatch"):
            TushareCapabilityReceiptV1.from_dict(payload)
        verify_capability_receipt(receipt, config=config)

    def test_safety_tamper_rejected_by_schema_and_python(self) -> None:
        _, receipt = _not_configured_receipt()
        payload = deepcopy(receipt.to_dict())
        payload["trade_eligibility"] = True
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(payload, RECEIPT_SCHEMA)
        with self.assertRaisesRegex(TushareCapabilityError, "forbidden"):
            TushareCapabilityReceiptV1.from_dict(payload)

        payload = deepcopy(receipt.to_dict())
        payload["endpoint_results"][0]["status"] = "trade_eligible"
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(payload, RECEIPT_SCHEMA)

    def test_receipt_rejects_hash_drift_config_drift_and_contract_subclasses(self) -> None:
        config, receipt = _not_configured_receipt()
        payload = deepcopy(receipt.to_dict())
        payload["git_worktree_status"] = "clean"
        with self.assertRaisesRegex(TushareCapabilityError, "hash mismatch"):
            TushareCapabilityReceiptV1.from_dict(payload)

        config_payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        config_payload["minimum_interval_seconds"] = "4"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "changed.json"
            path.write_text(json.dumps(config_payload), encoding="utf-8")
            changed_config = load_probe_config(path)
        with self.assertRaisesRegex(TushareCapabilityError, "config_sha256"):
            verify_capability_receipt(receipt, config=changed_config)

        class ForgedReceipt(TushareCapabilityReceiptV1):
            def require_valid(self, **kwargs):  # pragma: no cover - must not dispatch
                return self

        forged = ForgedReceipt(
            probe_run_id=receipt.probe_run_id,
            status=receipt.status,
            started_at=receipt.started_at,
            completed_at=receipt.completed_at,
            sdk_version=receipt.sdk_version,
            python_version=receipt.python_version,
            credential_status=receipt.credential_status,
            config_sha256=receipt.config_sha256,
            probe_code_sha256=receipt.probe_code_sha256,
            git_commit=receipt.git_commit,
            git_worktree_status=receipt.git_worktree_status,
            endpoint_results=receipt.endpoint_results,
            required_probe_summary=receipt.required_probe_summary,
            optional_probe_summary=receipt.optional_probe_summary,
            cross_validation_outcome=receipt.cross_validation_outcome,
            request_count=receipt.request_count,
            rate_limit_events=receipt.rate_limit_events,
            raw_evidence_manifest_sha256=receipt.raw_evidence_manifest_sha256,
        )
        with self.assertRaisesRegex(TushareCapabilityError, "exact controlled"):
            verify_capability_receipt(forged, config=config)

        class ForgedCrossValidation(CrossValidationOutcomeV1):
            def to_dict(self):  # pragma: no cover - must not dispatch
                return {"status": "compared"}

        forged_cross_validation = ForgedCrossValidation.from_dict(
            receipt.cross_validation_outcome.to_dict()
        )
        with self.assertRaisesRegex(TushareCapabilityError, "exact controlled type"):
            TushareCapabilityReceiptV1(
                probe_run_id=receipt.probe_run_id,
                status=receipt.status,
                started_at=receipt.started_at,
                completed_at=receipt.completed_at,
                sdk_version=receipt.sdk_version,
                python_version=receipt.python_version,
                credential_status=receipt.credential_status,
                config_sha256=receipt.config_sha256,
                probe_code_sha256=receipt.probe_code_sha256,
                git_commit=receipt.git_commit,
                git_worktree_status=receipt.git_worktree_status,
                endpoint_results=receipt.endpoint_results,
                required_probe_summary=receipt.required_probe_summary,
                optional_probe_summary=receipt.optional_probe_summary,
                cross_validation_outcome=forged_cross_validation,
                request_count=receipt.request_count,
                rate_limit_events=receipt.rate_limit_events,
                raw_evidence_manifest_sha256=receipt.raw_evidence_manifest_sha256,
            )

        class ForgedEndpoint(EndpointResultV1):
            def to_dict(self):  # pragma: no cover - must not dispatch
                return {"status": "passed"}

        forged_endpoint = ForgedEndpoint.from_dict(
            receipt.endpoint_results[0].to_dict()
        )
        with self.assertRaisesRegex(TushareCapabilityError, "exact EndpointResult"):
            TushareCapabilityReceiptV1(
                probe_run_id="forged-endpoint-receipt",
                status=receipt.status,
                started_at=receipt.started_at,
                completed_at=receipt.completed_at,
                sdk_version=receipt.sdk_version,
                python_version=receipt.python_version,
                credential_status=receipt.credential_status,
                config_sha256=receipt.config_sha256,
                probe_code_sha256=receipt.probe_code_sha256,
                git_commit=receipt.git_commit,
                git_worktree_status=receipt.git_worktree_status,
                endpoint_results=(forged_endpoint,),
                required_probe_summary=receipt.required_probe_summary,
                optional_probe_summary=receipt.optional_probe_summary,
                cross_validation_outcome=receipt.cross_validation_outcome,
                request_count=0,
                rate_limit_events=0,
                raw_evidence_manifest_sha256=receipt.raw_evidence_manifest_sha256,
            )


if __name__ == "__main__":
    unittest.main()
