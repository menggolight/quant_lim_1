from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import research.market_data.tushare_index_weight_value_diagnostic as diagnostic
import operations.run_tushare_index_weight_value_diagnostic as diagnostic_cli
from research.market_data.tushare_alpha_feasibility import TushareHttpResponse
from research.market_data.validation import validate_json_schema


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "P14DUnitTestCredential1234567890"
NOW = datetime(2026, 8, 30, 0, 1, 2, tzinfo=timezone.utc)


def response_bytes(
    rows: list[list[object]],
    *,
    fields: list[str] | None = None,
    extensions: dict[str, object] | None = None,
    msg: object = None,
) -> bytes:
    value: dict[str, object] = {
        "code": 0,
        "msg": msg,
        "data": {
            "fields": fields or list(diagnostic.REQUESTED_FIELDS),
            "items": rows,
        },
    }
    if extensions:
        value.update(extensions)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class RecordingTransport:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> TushareHttpResponse:
        self.calls.append(dict(kwargs))
        return TushareHttpResponse(http_status=200, body=self.body)


class RaisingTransport:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, **kwargs: object) -> TushareHttpResponse:
        self.calls += 1
        raise diagnostic.alpha_data.AlphaFeasibilityDataError(
            "https_transport_failed"
        )


class IndexWeightValueDiagnosticTests(unittest.TestCase):
    def _root(self, directory: str) -> Path:
        return Path(directory) / "index-weight-value-diagnostic"

    def test_collect_is_one_fixed_request_and_second_collect_fails_before_transport(self) -> None:
        body = response_bytes(
            [["000906.SH", "600000.SH", "20171229", 0.12]]
        )
        transport = RecordingTransport(body)
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = self._root(temporary)
            with patch.object(diagnostic, "DEFAULT_OUTPUT_ROOT", root), patch.object(
                diagnostic.alpha_data,
                "HttpsTushareTransport",
                return_value=transport,
            ):
                result = diagnostic.collect_live_once(
                    token=TOKEN,
                    run_id="first-run",
                    requested_at=NOW,
                )
            self.assertEqual(len(transport.calls), 1)
            call = transport.calls[0]
            self.assertEqual(call["endpoint"], "index_weight")
            self.assertEqual(dict(call["params"]), diagnostic.FIXED_PARAMS)
            self.assertEqual(tuple(call["fields"]), diagnostic.REQUESTED_FIELDS)
            self.assertEqual(result["actual_request_count_by_endpoint"]["index_weight"], 1)
            self.assertTrue(result["raw_response_persisted"])
            run = root / "first-run"
            self.assertEqual(
                json.loads((run / "network_call_started.json").read_bytes())["state"],
                "TRANSPORT_INVOCATION_STARTED",
            )
            self.assertEqual(
                json.loads((run / "network_response_scanned.json").read_bytes())["state"],
                "HTTP_RESPONSE_SCANNED",
            )

            with self.assertRaisesRegex(
                diagnostic.IndexWeightValueDiagnosticError,
                "diagnostic_network_budget_already_consumed",
            ):
                with patch.object(diagnostic, "DEFAULT_OUTPUT_ROOT", root):
                    diagnostic.collect_live_once(
                        token=TOKEN,
                        run_id="second-run",
                        requested_at=NOW,
                    )
            self.assertEqual(len(transport.calls), 1)

    def test_failed_transport_and_preexisting_root_still_consume_or_block_budget(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = self._root(temporary)
            raising = RaisingTransport()
            with patch.object(diagnostic, "DEFAULT_OUTPUT_ROOT", root), patch.object(
                diagnostic.alpha_data,
                "HttpsTushareTransport",
                return_value=raising,
            ):
                with self.assertRaisesRegex(
                    diagnostic.IndexWeightValueDiagnosticError,
                    "https_transport_failed",
                ):
                    diagnostic.collect_live_once(
                        token=TOKEN,
                        run_id="failed-transport",
                        requested_at=NOW,
                    )
            self.assertEqual(raising.calls, 1)
            self.assertTrue(
                (root / "failed-transport" / "network_call_started.json").is_file()
            )

            second = RecordingTransport(
                response_bytes(
                    [["000906.SH", "600000.SH", "20171229", "0.125"]]
                )
            )
            with patch.object(diagnostic, "DEFAULT_OUTPUT_ROOT", root), patch.object(
                diagnostic.alpha_data,
                "HttpsTushareTransport",
                return_value=second,
            ):
                with self.assertRaisesRegex(
                    diagnostic.IndexWeightValueDiagnosticError,
                    "diagnostic_network_budget_already_consumed",
                ):
                    diagnostic.collect_live_once(
                        token=TOKEN,
                        run_id="forbidden-retry",
                        requested_at=NOW,
                    )
            self.assertEqual(second.calls, [])

        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = self._root(temporary)
            root.mkdir()
            never_called = RecordingTransport(
                response_bytes(
                    [["000906.SH", "600000.SH", "20171229", "0.125"]]
                )
            )
            with patch.object(diagnostic, "DEFAULT_OUTPUT_ROOT", root), patch.object(
                diagnostic.alpha_data,
                "HttpsTushareTransport",
                return_value=never_called,
            ):
                with self.assertRaisesRegex(
                    diagnostic.IndexWeightValueDiagnosticError,
                    "diagnostic_network_budget_already_consumed",
                ):
                    diagnostic.collect_live_once(
                        token=TOKEN,
                        run_id="blocked-by-existing-root",
                        requested_at=NOW,
                    )
            self.assertEqual(never_called.calls, [])

    def test_request_reservation_has_exact_closed_fields_and_no_token(self) -> None:
        body = response_bytes(
            [["000906.SH", "600000.SH", "20171229", "0.125"]]
        )
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = self._root(temporary)
            diagnostic._collect_once_for_offline_test(
                token=TOKEN,
                run_id="request-shape",
                output_root=root,
                requested_at=NOW,
                response=body,
            )
            request_path = root / "request-shape" / "request.json"
            raw = request_path.read_bytes()
            self.assertNotIn(TOKEN.encode("utf-8"), raw)
            request = json.loads(raw)
            self.assertEqual(
                set(request),
                {
                    "api_name",
                    "params",
                    "requested_fields",
                    "request_fingerprint",
                    "requested_at",
                    "endpoint",
                    "date_bounds",
                },
            )
            validate_json_schema(request, diagnostic.REQUEST_SCHEMA_PATH)

    def test_raw_bytes_are_exact_create_only_and_profile_can_be_verified_offline(self) -> None:
        body = response_bytes(
            [["000906.SH", "600000.SH", "20171229", 0.125]],
            extensions={"request_id": "safe-request-id", "detail": {"status": "ok"}},
        )
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = self._root(temporary)
            result = diagnostic._collect_once_for_offline_test(
                token=TOKEN,
                run_id="raw-exact",
                output_root=root,
                requested_at=NOW,
                response=body,
            )
            raw_path = root / "raw-exact" / "response.raw.json"
            self.assertEqual(raw_path.read_bytes(), body)
            self.assertEqual(
                result["raw_transport_sha256"], hashlib.sha256(body).hexdigest()
            )
            verified = diagnostic.regenerate_value_profile(
                token=TOKEN,
                run_id="raw-exact",
                output_root=root,
            )
            self.assertEqual(verified["status"], "VALUE_PROFILE_VERIFIED")

    def test_token_echo_is_rejected_after_reservation_and_raw_is_not_written(self) -> None:
        body = response_bytes(
            [["000906.SH", "600000.SH", "20171229", "0.125"]],
            extensions={"detail": TOKEN},
        )
        transport = RecordingTransport(body)
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = self._root(temporary)
            with patch.object(diagnostic, "DEFAULT_OUTPUT_ROOT", root), patch.object(
                diagnostic.alpha_data,
                "HttpsTushareTransport",
                return_value=transport,
            ):
                with self.assertRaisesRegex(
                    diagnostic.IndexWeightValueDiagnosticError, "token_leak_detected"
                ):
                    diagnostic.collect_live_once(
                        token=TOKEN,
                        run_id="token-rejected",
                        requested_at=NOW,
                    )
            run = root / "token-rejected"
            self.assertTrue((run / "request.json").is_file())
            self.assertTrue((run / "network_call_started.json").is_file())
            self.assertFalse((run / "network_response_scanned.json").exists())
            self.assertFalse((run / "response.raw.json").exists())
            self.assertFalse((run / "value_profile.json").exists())
            self.assertEqual(len(transport.calls), 1)
            retry = RecordingTransport(
                response_bytes(
                    [["000906.SH", "600000.SH", "20171229", "0.125"]]
                )
            )
            with patch.object(diagnostic, "DEFAULT_OUTPUT_ROOT", root), patch.object(
                diagnostic.alpha_data,
                "HttpsTushareTransport",
                return_value=retry,
            ):
                with self.assertRaisesRegex(
                    diagnostic.IndexWeightValueDiagnosticError,
                    "diagnostic_network_budget_already_consumed",
                ):
                    diagnostic.collect_live_once(
                        token=TOKEN,
                        run_id="token-retry-forbidden",
                        requested_at=NOW,
                    )
            self.assertEqual(retry.calls, [])

    def test_sensitive_nested_field_is_rejected_before_raw_persistence(self) -> None:
        body = response_bytes(
            [["000906.SH", "600000.SH", "20171229", "0.125"]],
            extensions={"detail": {"nested": {"authorization": "redacted"}}},
        )
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = self._root(temporary)
            with self.assertRaisesRegex(
                diagnostic.IndexWeightValueDiagnosticError,
                "sensitive_field_name_detected",
            ):
                diagnostic._collect_once_for_offline_test(
                    token=TOKEN,
                    run_id="sensitive-key",
                    output_root=root,
                    requested_at=NOW,
                    response=body,
                )
            self.assertFalse((root / "sensitive-key" / "response.raw.json").exists())

    def test_nonfinite_json_and_oversized_response_are_not_persisted(self) -> None:
        prefix = (
            b'{"code":0,"msg":null,"data":{"fields":["index_code",'
            b'"con_code","trade_date","weight"],"items":[["000906.SH",'
            b'"600000.SH","20171229",'
        )
        for run_id, body, expected in (
            ("nan", prefix + b"NaN]]}}", "nonfinite_json_value_detected"),
            ("infinity", prefix + b"Infinity]]}}", "nonfinite_json_value_detected"),
            ("negative-infinity", prefix + b"-Infinity]]}}", "nonfinite_json_value_detected"),
            ("oversized", b"x" * 257, "response_body_too_large"),
        ):
            with self.subTest(run_id=run_id), tempfile.TemporaryDirectory(
                dir=ROOT
            ) as temporary:
                root = self._root(temporary)
                with self.assertRaisesRegex(
                    diagnostic.IndexWeightValueDiagnosticError, expected
                ):
                    diagnostic._collect_once_for_offline_test(
                        token=TOKEN,
                        run_id=run_id,
                        output_root=root,
                        requested_at=NOW,
                        response=body,
                        maximum_response_bytes=(256 if run_id == "oversized" else 1024),
                    )
                self.assertFalse((root / run_id / "response.raw.json").exists())

    def test_post_cutoff_market_date_is_rejected_but_opaque_request_id_is_not(self) -> None:
        bad = response_bytes(
            [["000906.SH", "600000.SH", "20240102", "0.125"]]
        )
        good = response_bytes(
            [["000906.SH", "600000.SH", "20171229", "0.125"]],
            extensions={"request_id": "req-20260830-opaque"},
        )
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = self._root(temporary)
            with self.assertRaisesRegex(
                diagnostic.IndexWeightValueDiagnosticError,
                "post_cutoff_market_data_detected",
            ):
                diagnostic._collect_once_for_offline_test(
                    token=TOKEN,
                    run_id="post-cutoff",
                    output_root=root,
                    requested_at=NOW,
                    response=bad,
                )
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            result = diagnostic._collect_once_for_offline_test(
                token=TOKEN,
                run_id="opaque-extension",
                output_root=self._root(temporary),
                requested_at=NOW,
                response=good,
            )
            self.assertTrue(result["raw_response_persisted"])

    def test_all_nonopaque_post_cutoff_locations_and_structured_request_id_fail_closed(self) -> None:
        base_row = [["000906.SH", "600000.SH", "20171229", "0.125"]]
        payloads = {
            "root-detail": response_bytes(
                base_row, extensions={"detail": "2024-01-02"}
            ),
            "root-key": response_bytes(
                base_row, extensions={"2024-01-02": "value"}
            ),
            "numeric-msg": response_bytes(base_row, msg=20240101.0),
            "year-2100": response_bytes(base_row, extensions={"detail": "21000101"}),
            "year-9999": response_bytes(base_row, extensions={"detail": "99991231"}),
        }
        for run_id, body in payloads.items():
            with self.subTest(run_id=run_id), tempfile.TemporaryDirectory(
                dir=ROOT
            ) as temporary:
                root = self._root(temporary)
                with self.assertRaisesRegex(
                    diagnostic.IndexWeightValueDiagnosticError,
                    "post_cutoff_market_data_detected",
                ):
                    diagnostic._collect_once_for_offline_test(
                        token=TOKEN,
                        run_id=run_id,
                        output_root=root,
                        requested_at=NOW,
                        response=body,
                    )
                self.assertFalse((root / run_id / "response.raw.json").exists())

        structured = response_bytes(
            base_row,
            extensions={"request_id": {"trade_date": "2024-01-02"}},
        )
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = self._root(temporary)
            with self.assertRaisesRegex(
                diagnostic.IndexWeightValueDiagnosticError,
                "request_id_not_safe_opaque_string",
            ):
                diagnostic._collect_once_for_offline_test(
                    token=TOKEN,
                    run_id="structured-request-id",
                    output_root=root,
                    requested_at=NOW,
                    response=structured,
                )
            self.assertFalse(
                (root / "structured-request-id" / "response.raw.json").exists()
            )

    def test_profile_locates_current_scale_predicate_with_zero_based_row(self) -> None:
        body = response_bytes(
            [
                ["000906.SH", "600000.SH", "20171229", 0.125],
                ["000906.SH", "000001.SZ", "20171229", 0.25],
            ]
        )
        root, scan = diagnostic.scan_raw_response(body, token=TOKEN)
        profile = diagnostic.build_value_profile(root, scan_evidence=scan)
        self.assertEqual(profile["first_failing_row_index"], 1)
        self.assertEqual(profile["first_failing_field"], "weight")
        self.assertEqual(profile["first_failing_json_type"], "number")
        self.assertEqual(
            profile["first_failing_predicate"],
            "weight_decimal_scale_below_three",
        )
        self.assertEqual(profile["first_failing_projected_row"]["weight"], 0.25)
        self.assertEqual(profile["weight_profile"]["numeric_number_count"], 2)
        self.assertEqual(profile["weight_profile"]["sum_by_trade_date"], {"20171229": "0.375"})
        validate_json_schema(profile, diagnostic.PROFILE_SCHEMA_PATH)

    def test_profile_preserves_a0fad_failure_after_current_parser_fix(self) -> None:
        body = response_bytes(
            [["000906.SH", "600000.SH", "20171229", 0.12]]
        )
        validated = diagnostic.alpha_data.validate_response_bytes(
            diagnostic._fixed_task(),
            body,
            token=TOKEN,
        )
        self.assertEqual(validated.rows[0]["weight"], "0.12")
        root, scan = diagnostic.scan_raw_response(body, token=TOKEN)
        profile = diagnostic.build_value_profile(root, scan_evidence=scan)
        self.assertEqual(
            profile["first_failing_predicate"],
            "weight_decimal_scale_below_three",
        )

    def test_profile_covers_numeric_strings_zero_bool_null_and_integer_date(self) -> None:
        body = response_bytes(
            [
                ["000906.SH", "600000.SH", 20171229, "0.125"],
                ["000906.SH", "000001.SZ", "20171229", 0],
                ["000906.SH", "000002.SZ", "20171229", True],
                ["000906.SH", "000003.SZ", "20171229", None],
            ]
        )
        root, scan = diagnostic.scan_raw_response(body, token=TOKEN)
        profile = diagnostic.build_value_profile(root, scan_evidence=scan)
        self.assertEqual(profile["first_failing_field"], "trade_date")
        self.assertEqual(profile["first_failing_predicate"], "trade_date_integer_rejected")
        self.assertEqual(profile["trade_date_profile"]["integer_8_digit_count"], 1)
        self.assertEqual(profile["trade_date_profile"]["invalid_date_count"], 1)
        self.assertEqual(
            profile["weight_profile"]["sum_by_trade_date"], {"20171229": "0"}
        )
        weight = profile["weight_profile"]
        self.assertEqual(weight["numeric_string_count"], 1)
        self.assertEqual(weight["numeric_number_count"], 1)
        self.assertEqual(weight["zero_count"], 1)
        self.assertEqual(weight["bool_count"], 1)
        self.assertEqual(weight["null_count"], 1)

    def test_current_predicates_cover_required_value_representations(self) -> None:
        cases = {
            "numeric-number": (
                ["000906.SH", "600000.SH", "20171229", 0.125],
                None,
            ),
            "numeric-string": (
                ["000906.SH", "600000.SH", "20171229", "0.125"],
                None,
            ),
            "integer-date": (
                ["000906.SH", "600000.SH", 20171229, "0.125"],
                "trade_date_integer_rejected",
            ),
            "bool-weight": (
                ["000906.SH", "600000.SH", "20171229", True],
                "weight_bool",
            ),
            "null-weight": (
                ["000906.SH", "600000.SH", "20171229", None],
                "weight_null",
            ),
        }
        for name, (row, predicate) in cases.items():
            with self.subTest(name=name):
                body = response_bytes([row])
                root, scan = diagnostic.scan_raw_response(body, token=TOKEN)
                profile = diagnostic.build_value_profile(root, scan_evidence=scan)
                self.assertEqual(profile["first_failing_predicate"], predicate)

    def test_profile_matches_parser_for_date10_duplicates_and_invalid_weight_precedence(self) -> None:
        date10 = response_bytes(
            [["000906.SH", "600000.SH", "2017-12-29", "0.125"]]
        )
        root, scan = diagnostic.scan_raw_response(date10, token=TOKEN)
        profile = diagnostic.build_value_profile(root, scan_evidence=scan)
        self.assertIsNone(profile["first_failing_predicate"])
        self.assertEqual(profile["trade_date_profile"]["min_date"], "20171229")

        normalized_duplicate = response_bytes(
            [
                ["000906.SH", "600000.SH", "2017-12-29", "0.125"],
                ["000906.SH", "600000.SH", "20171229", "0.125"],
            ]
        )
        root, scan = diagnostic.scan_raw_response(normalized_duplicate, token=TOKEN)
        profile = diagnostic.build_value_profile(root, scan_evidence=scan)
        self.assertEqual(profile["duplicate_primary_key_count"], 1)
        self.assertEqual(profile["first_failing_row_index"], 1)
        self.assertEqual(profile["first_failing_predicate"], "duplicate_primary_key")

        invalid_before_duplicate = response_bytes(
            [
                ["000906.SH", "600000.SH", "20171229", "0.125"],
                ["000906.SH", "600000.SH", "20171229", 0.25],
            ]
        )
        root, scan = diagnostic.scan_raw_response(invalid_before_duplicate, token=TOKEN)
        profile = diagnostic.build_value_profile(root, scan_evidence=scan)
        self.assertEqual(profile["duplicate_primary_key_count"], 0)
        self.assertEqual(
            profile["first_failing_predicate"],
            "weight_decimal_scale_below_three",
        )

    def test_scientific_numbers_nonfinite_strings_and_composites_have_safe_profiles(self) -> None:
        exponent = (
            b'{"code":0,"msg":null,"data":{"fields":["index_code",'
            b'"con_code","trade_date","weight"],"items":[["000906.SH",'
            b'"600000.SH","20171229",1e3]]}}'
        )
        root, scan = diagnostic.scan_raw_response(exponent, token=TOKEN)
        profile = diagnostic.build_value_profile(root, scan_evidence=scan)
        self.assertEqual(profile["weight_profile"]["minimum"], "1000")
        self.assertEqual(
            profile["first_failing_predicate"],
            "weight_decimal_scale_below_three",
        )

        nonfinite_string = response_bytes(
            [["000906.SH", "600000.SH", "20171229", "NaN"]]
        )
        root, scan = diagnostic.scan_raw_response(nonfinite_string, token=TOKEN)
        profile = diagnostic.build_value_profile(root, scan_evidence=scan)
        self.assertEqual(profile["weight_profile"]["nonfinite_count"], 1)
        self.assertEqual(profile["first_failing_predicate"], "weight_nonfinite")

        composite = response_bytes(
            [["000906.SH", "600000.SH", "20171229", [1, 2]]]
        )
        root, scan = diagnostic.scan_raw_response(composite, token=TOKEN)
        profile = diagnostic.build_value_profile(root, scan_evidence=scan)
        self.assertEqual(profile["first_failing_json_type"], "array")
        self.assertEqual(
            profile["first_failing_projected_row"]["weight"]["redacted_json_type"],
            "array",
        )
        validate_json_schema(profile, diagnostic.PROFILE_SCHEMA_PATH)

        numeric_strings = response_bytes(
            [
                ["000906.SH", "600001.SH", "20171229", "+0.125"],
                ["000906.SH", "600002.SH", "20171229", "1e-3"],
            ]
        )
        root, scan = diagnostic.scan_raw_response(numeric_strings, token=TOKEN)
        profile = diagnostic.build_value_profile(root, scan_evidence=scan)
        self.assertEqual(profile["weight_profile"]["numeric_string_count"], 2)
        self.assertEqual(
            profile["weight_profile"]["plain_numeric_string_count"], 0
        )

    def test_profile_weight_sum_uses_exact_decimal_arithmetic(self) -> None:
        weights = [
            "99.999999999999999999999999999301",
            *(["0.000000000000000000000000000001"] * 799),
        ]
        rows = [
            ["000906.SH", f"{600000 + index:06d}.SH", "20171229", weight]
            for index, weight in enumerate(weights)
        ]
        root, scan = diagnostic.scan_raw_response(
            response_bytes(rows),
            token=TOKEN,
        )
        profile = diagnostic.build_value_profile(root, scan_evidence=scan)
        self.assertEqual(
            profile["weight_profile"]["sum_by_trade_date"],
            {"20171229": "100.000000000000000000000000000100"},
        )

    def test_pathological_decimal_exponents_fail_without_expansion(self) -> None:
        numeric_exponent = (
            b'{"code":0,"msg":null,"data":{"fields":["index_code",'
            b'"con_code","trade_date","weight"],"items":[["000906.SH",'
            b'"600000.SH","20171229",1e1000000000]]}}'
        )
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = self._root(temporary)
            with self.assertRaisesRegex(
                diagnostic.IndexWeightValueDiagnosticError,
                "json_decimal_magnitude_unsafe",
            ):
                diagnostic._collect_once_for_offline_test(
                    token=TOKEN,
                    run_id="huge-json-exponent",
                    output_root=root,
                    requested_at=NOW,
                    response=numeric_exponent,
                )
            self.assertFalse(
                (root / "huge-json-exponent" / "response.raw.json").exists()
            )

        string_exponent = response_bytes(
            [["000906.SH", "600000.SH", "20171229", "1e1000000000"]]
        )
        root_value, scan = diagnostic.scan_raw_response(
            string_exponent, token=TOKEN
        )
        profile = diagnostic.build_value_profile(root_value, scan_evidence=scan)
        self.assertEqual(
            profile["weight_profile"]["magnitude_out_of_bounds_count"], 1
        )
        self.assertIsNone(profile["weight_profile"]["minimum"])

    def test_calendar_invalid_predicate_and_wire_bound_row_hash(self) -> None:
        invalid_date = response_bytes(
            [["000906.SH", "600000.SH", "20170230", "0.125"]]
        )
        root, scan = diagnostic.scan_raw_response(invalid_date, token=TOKEN)
        profile = diagnostic.build_value_profile(root, scan_evidence=scan)
        self.assertEqual(
            profile["first_failing_predicate"], "trade_date_calendar_invalid"
        )

        hashes = []
        for lexical_weight in (b"0.2", b"0.20"):
            body = (
                b'{"code":0,"msg":null,"data":{"fields":["index_code",'
                b'"con_code","trade_date","weight"],"items":[["000906.SH",'
                b'"600000.SH","20171229",' + lexical_weight + b"]]}}"
            )
            root, scan = diagnostic.scan_raw_response(body, token=TOKEN)
            profile = diagnostic.build_value_profile(root, scan_evidence=scan)
            hashes.append(profile["first_failing_row_sha256"])
        self.assertNotEqual(hashes[0], hashes[1])

    def test_profile_remains_baseline_bound_across_parser_fix_replay(self) -> None:
        rows = [
            [
                "000906.SH",
                f"{600000 + index:06d}.SH",
                "20171229",
                "0.12" if index < 400 else "0.13",
            ]
            for index in range(800)
        ]
        body = response_bytes(rows)
        original = diagnostic.alpha_data._normalize_response_row

        def accepts_two_decimal_weights(task: object, row: dict[str, object]):
            if getattr(task, "endpoint", None) != "index_weight":
                return original(task, row)
            normalized = dict(row)
            normalized["trade_date"] = str(row["trade_date"]).replace("-", "")
            normalized["weight"] = str(row["weight"])
            return normalized, 0

        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = self._root(temporary)
            diagnostic._collect_once_for_offline_test(
                token=TOKEN,
                run_id="cross-parser-replay",
                output_root=root,
                requested_at=NOW,
                response=body,
            )
            with patch.object(
                diagnostic.alpha_data,
                "_normalize_response_row",
                side_effect=accepts_two_decimal_weights,
            ):
                replay = diagnostic.replay_saved_response(
                    token=TOKEN,
                    run_id="cross-parser-replay",
                    normalization_change="accept_observed_two_decimal_weights",
                    output_root=root,
                )
            self.assertEqual(replay["offline_replay_status"], "DIAGNOSTIC_REPLAY_ACCEPTED")
            self.assertEqual(replay["normalized_weight_sum_by_date"], {"20171229": "100.00"})

    def test_replay_binds_request_profile_and_separates_raw_normalized_hashes(self) -> None:
        rows = [
            ["000906.SH", f"{600000 + index:06d}.SH", "20171229", "0.125"]
            for index in range(800)
        ]
        body = response_bytes(rows)
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = self._root(temporary)
            diagnostic._collect_once_for_offline_test(
                token=TOKEN,
                run_id="replay-success",
                output_root=root,
                requested_at=NOW,
                response=body,
            )
            run = root / "replay-success"
            profile_bytes = (run / "value_profile.json").read_bytes()
            replay = diagnostic.replay_saved_response(
                token=TOKEN,
                run_id="replay-success",
                normalization_change="existing_three_decimal_contract_replay",
                output_root=root,
            )
            self.assertEqual(replay["replay_pass_count"], 2)
            self.assertTrue(replay["deterministic_replay"])
            self.assertEqual(replay["normalized_row_count"], 800)
            self.assertEqual(
                replay["value_profile_sha256"],
                hashlib.sha256(profile_bytes).hexdigest(),
            )
            self.assertNotEqual(
                replay["raw_transport_sha256"],
                replay["normalized_content_sha256"],
            )
            self.assertEqual(replay["locked_test_status"], diagnostic.LOCKED_TEST_STATUS)
            self.assertFalse(replay["locked_test_consumed"])
            forbidden = {
                "tests.test_strategy_workspace_admission",
                "tests.test_strategy_workspace_evaluation",
                "tests.test_strategy_workspace_experiment",
                "tests.test_strategy_workspace_top_decile_backtest",
            }
            self.assertTrue(forbidden.isdisjoint(sys.modules))

    def test_replay_rejects_missing_or_tampered_provenance_and_profile(self) -> None:
        body = response_bytes(
            [["000906.SH", "600000.SH", "20171229", "0.125"]]
        )
        for run_id, target, expected in (
            ("missing-request", "request.json", "collection_provenance_missing"),
            ("tampered-request", "request.json", "request_artifact_invalid"),
            ("missing-profile", "value_profile.json", "value_profile_artifact_missing"),
            ("tampered-profile", "value_profile.json", "deterministic_replay_mismatch"),
        ):
            with self.subTest(run_id=run_id), tempfile.TemporaryDirectory(
                dir=ROOT
            ) as temporary:
                root = self._root(temporary)
                diagnostic._collect_once_for_offline_test(
                    token=TOKEN,
                    run_id=run_id,
                    output_root=root,
                    requested_at=NOW,
                    response=body,
                )
                path = root / run_id / target
                if run_id.startswith("missing"):
                    path.unlink()
                else:
                    path.write_bytes(b"{}\n")
                action = (
                    diagnostic.regenerate_value_profile
                    if "request" in run_id
                    else diagnostic.replay_saved_response
                )
                kwargs: dict[str, object] = {
                    "token": TOKEN,
                    "run_id": run_id,
                    "output_root": root,
                }
                if action is diagnostic.replay_saved_response:
                    kwargs["normalization_change"] = "tamper_guard_replay"
                with self.assertRaisesRegex(
                    diagnostic.IndexWeightValueDiagnosticError, expected
                ):
                    action(**kwargs)

    def test_provenance_rejects_semantic_started_scanned_and_raw_drift(self) -> None:
        body = response_bytes(
            [["000906.SH", "600000.SH", "20171229", "0.125"]]
        )
        cases = {
            "request-time": (
                "request.json",
                "network_start_artifact_invalid",
            ),
            "started-state": (
                "network_call_started.json",
                "network_start_artifact_invalid",
            ),
            "scanned-hash": (
                "network_response_scanned.json",
                "network_response_artifact_invalid",
            ),
            "raw-wire": (
                "response.raw.json",
                "network_response_artifact_invalid",
            ),
        }
        for run_id, (filename, expected) in cases.items():
            with self.subTest(run_id=run_id), tempfile.TemporaryDirectory(
                dir=ROOT
            ) as temporary:
                root = self._root(temporary)
                diagnostic._collect_once_for_offline_test(
                    token=TOKEN,
                    run_id=run_id,
                    output_root=root,
                    requested_at=NOW,
                    response=body,
                )
                path = root / run_id / filename
                if run_id == "raw-wire":
                    path.write_bytes(path.read_bytes() + b" ")
                else:
                    artifact = json.loads(path.read_bytes())
                    if run_id == "request-time":
                        artifact["requested_at"] = "2030-01-01T00:00:00+00:00"
                    elif run_id == "started-state":
                        artifact["state"] = "OLD_OR_UNKNOWN_STATE"
                    else:
                        artifact["raw_transport_sha256"] = "0" * 64
                    path.write_text(
                        json.dumps(artifact, separators=(",", ":")),
                        encoding="utf-8",
                    )
                with self.assertRaisesRegex(
                    diagnostic.IndexWeightValueDiagnosticError, expected
                ):
                    diagnostic.regenerate_value_profile(
                        token=TOKEN,
                        run_id=run_id,
                        output_root=root,
                    )

    def test_live_boundary_has_no_injected_transport_or_output_root(self) -> None:
        live_parameters = set(inspect.signature(diagnostic.collect_live_once).parameters)
        offline_parameters = set(
            inspect.signature(diagnostic._collect_once_for_offline_test).parameters
        )
        self.assertEqual(live_parameters, {"token", "run_id", "requested_at"})
        self.assertNotIn("transport", offline_parameters)
        self.assertIn("response", offline_parameters)

    def test_token_cannot_enter_cli_output_via_run_id_or_change_id(self) -> None:
        for argv in (
            ["profile", "--run-id", TOKEN],
            [
                "replay",
                "--run-id",
                "safe-run-id",
                "--normalization-change",
                TOKEN,
            ],
        ):
            with self.subTest(command=argv[0]):
                sink = io.BytesIO()
                stdout = io.TextIOWrapper(sink, encoding="utf-8")
                with patch.dict(os.environ, {"TUSHARE_TOKEN": TOKEN}), patch.object(
                    sys, "stdout", stdout
                ):
                    self.assertEqual(diagnostic_cli.main(argv), 1)
                    stdout.flush()
                output = sink.getvalue()
                self.assertNotIn(TOKEN.encode("utf-8"), output)
                self.assertEqual(json.loads(output)["status"], "BLOCKED")

    def test_leading_zero_codes_cannot_be_recovered_from_integer(self) -> None:
        body = response_bytes(
            [[906, "600000.SH", "20171229", "0.125"]]
        )
        root, scan = diagnostic.scan_raw_response(body, token=TOKEN)
        profile = diagnostic.build_value_profile(root, scan_evidence=scan)
        self.assertEqual(profile["first_failing_field"], "index_code")
        self.assertEqual(profile["first_failing_json_type"], "integer")
        self.assertEqual(profile["first_failing_predicate"], "index_code_mismatch")
        self.assertEqual(profile["index_code_profile"]["non_string_count"], 1)
        self.assertEqual(profile["index_code_profile"]["unique_values"], [])

    def test_locked_modules_remain_unimported(self) -> None:
        forbidden = {
            "tests.test_strategy_workspace_admission",
            "tests.test_strategy_workspace_evaluation",
            "tests.test_strategy_workspace_experiment",
            "tests.test_strategy_workspace_top_decile_backtest",
        }
        self.assertTrue(forbidden.isdisjoint(sys.modules))


if __name__ == "__main__":
    unittest.main()
