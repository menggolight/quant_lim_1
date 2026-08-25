from __future__ import annotations

import hashlib
import json
import multiprocessing
import socket
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from agent import tushare_single_endpoint_diagnostic as runner
from research.market_data.tushare_capability import canonical_json_bytes
from research.market_data.tushare_diagnostic import (
    DiagnosticChannelResultV1,
    TushareDiagnosticError,
    build_diagnostic_receipt,
    classify_message_category,
    derive_conclusion,
    normalize_upstream_code,
    safe_exception_type,
    verify_diagnostic_receipt,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONFIG = REPOSITORY_ROOT / "configs" / "tushare_capability_probe.v1.json"
POLICY = REPOSITORY_ROOT / "configs" / "provider_access.v1.json"


def _attempt_round_lock(root: str, results: object) -> None:
    try:
        with runner._exclusive_round_execution(Path(root)):
            results.put("acquired")  # type: ignore[attr-defined]
    except runner.TushareSingleEndpointDiagnosticError:
        results.put("blocked")  # type: ignore[attr-defined]


class _Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 25, 6, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        current = self.value
        self.value += timedelta(microseconds=1)
        return current


class _FakeResponse:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self.status_code = status_code
        self._wire = canonical_json_bytes(payload)
        self._content = self._wire
        self._content_consumed = False
        self.closed = False

    def iter_content(self, chunk_size: int = 65_536):
        for offset in range(0, len(self._wire), chunk_size):
            yield self._wire[offset : offset + chunk_size]

    @property
    def text(self) -> str:
        return self._content.decode("utf-8")

    def close(self) -> None:
        self.closed = True

    def __bool__(self) -> bool:
        return self.status_code < 400


class _FakeSession:
    def __init__(self, response_or_error: object) -> None:
        self.response_or_error = response_or_error
        self.calls: list[dict[str, object]] = []
        self.closed = False

    def post(self, url: str, **kwargs: object) -> object:
        self.calls.append({"url": url, **kwargs})
        if isinstance(self.response_or_error, BaseException):
            raise self.response_or_error
        return self.response_or_error

    def close(self) -> None:
        self.closed = True


class _FakeFrame:
    def __init__(self, fields: list[str], items: list[list[object]]) -> None:
        self.columns = fields
        self.shape = (len(items), len(fields))


class _FakeSdkClient:
    def __init__(self, module: SimpleNamespace, token: str) -> None:
        self.module = module
        self.token = token
        self._DataApi__http_url = "http://unsafe.invalid"

    def _query(self, endpoint: str, parameters: dict[str, str]) -> _FakeFrame:
        parameters.setdefault("ts_type_name", self._DataApi__http_url)
        response = self.module.requests.post(
            f"{self._DataApi__http_url}/{endpoint}",
            json={
                "api_name": endpoint,
                "token": self.token,
                "params": parameters,
                "fields": "",
            },
            timeout=30,
        )
        if not response:
            return _FakeFrame([], [])
        envelope = json.loads(response.text)
        if envelope["code"] != 0:
            raise Exception(envelope.get("msg", ""))
        data = envelope["data"]
        return _FakeFrame(data["fields"], data["items"])

    def trade_cal(self, **parameters: str) -> _FakeFrame:
        return self._query("trade_cal", dict(parameters))

    def daily(self, **parameters: str) -> _FakeFrame:
        return self._query("daily", dict(parameters))


def _fake_sdk(module: SimpleNamespace, token: str) -> SimpleNamespace:
    return SimpleNamespace(
        __version__="1.4.29-test",
        pro_api=lambda supplied: _FakeSdkClient(module, supplied),
    )


def _result(
    channel: str,
    kind: str,
    *,
    endpoint: str = "trade_cal",
) -> DiagnosticChannelResultV1:
    started = datetime(2026, 8, 25, 6, 0, tzinfo=timezone.utc)
    ended = started + timedelta(seconds=1)
    if kind == "passed":
        return DiagnosticChannelResultV1(
            channel=channel,
            endpoint=endpoint,
            transport_target=runner.OFFICIAL_HTTPS_API_URL,
            diagnostic_attempted=True,
            request_count=1,
            requested_at=started,
            completed_at=ended,
            transport_status="response_received",
            http_status=200,
            upstream_code=0,
            sdk_exception_type=None,
            sanitized_message_category="success",
            outcome="passed",
            row_count=1,
            field_names=("cal_date",),
        )
    if kind == "permission":
        return DiagnosticChannelResultV1(
            channel=channel,
            endpoint=endpoint,
            transport_target=runner.OFFICIAL_HTTPS_API_URL,
            diagnostic_attempted=True,
            request_count=1,
            requested_at=started,
            completed_at=ended,
            transport_status="response_received",
            http_status=200,
            upstream_code=2002,
            sdk_exception_type="Exception" if channel == "sdk" else None,
            sanitized_message_category="permission",
            outcome="upstream_rejected",
            row_count=0,
            field_names=(),
        )
    if kind == "network":
        return DiagnosticChannelResultV1(
            channel=channel,
            endpoint=endpoint,
            transport_target=runner.OFFICIAL_HTTPS_API_URL,
            diagnostic_attempted=True,
            request_count=1,
            requested_at=started,
            completed_at=ended,
            transport_status="connection_failure",
            http_status=None,
            upstream_code=None,
            sdk_exception_type="ConnectionError" if channel == "sdk" else None,
            sanitized_message_category="network_transport",
            outcome="transport_failed",
            row_count=0,
            field_names=(),
        )
    if kind == "sdk_preflight":
        return DiagnosticChannelResultV1(
            channel=channel,
            endpoint=endpoint,
            transport_target=runner.OFFICIAL_HTTPS_API_URL,
            diagnostic_attempted=True,
            request_count=0,
            requested_at=started,
            completed_at=ended,
            transport_status="not_attempted",
            http_status=None,
            upstream_code=None,
            sdk_exception_type="RuntimeError",
            sanitized_message_category="sdk_client",
            outcome="client_failed",
            row_count=0,
            field_names=(),
        )
    raise AssertionError(kind)


class TushareDiagnosticContractTests(unittest.TestCase):
    def test_structured_code_precedes_conflicting_message_and_http_status(self) -> None:
        self.assertEqual(
            classify_message_category(
                upstream_code=2002,
                http_status=429,
                message="invalid parameter",
                error=None,
                transport_status="response_received",
                channel="http",
            ),
            "permission",
        )
        self.assertEqual(
            classify_message_category(
                upstream_code=0,
                http_status=500,
                message="permission denied",
                error=None,
                transport_status="response_received",
                channel="http",
            ),
            "success",
        )

    def test_structured_message_maps_all_required_categories(self) -> None:
        cases = {
            "invalid token": "authentication_account",
            "permission denied": "permission",
            "too many requests": "rate_limit",
            "invalid parameter": "invalid_parameter",
            "internal server error": "server_internal",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(
                    classify_message_category(
                        upstream_code=9999,
                        http_status=200,
                        message=message,
                        error=None,
                        transport_status="response_received",
                        channel="http",
                    ),
                    expected,
                )

    def test_http_status_fallback_maps_required_categories(self) -> None:
        for status, expected in {
            401: "authentication_account",
            403: "permission",
            429: "rate_limit",
            422: "invalid_parameter",
            503: "server_internal",
        }.items():
            with self.subTest(status=status):
                self.assertEqual(
                    classify_message_category(
                        upstream_code=None,
                        http_status=status,
                        message=None,
                        error=None,
                        transport_status="response_received",
                        channel="http",
                    ),
                    expected,
                )

    def test_upstream_code_is_integer_only_and_bool_is_rejected(self) -> None:
        self.assertEqual(normalize_upstream_code("2002"), 2002)
        self.assertIsNone(normalize_upstream_code(True))
        self.assertIsNone(normalize_upstream_code("token-fragment"))
        with self.assertRaises(TushareDiagnosticError):
            DiagnosticChannelResultV1(
                channel="http",
                endpoint="trade_cal",
                transport_target=runner.OFFICIAL_HTTPS_API_URL,
                diagnostic_attempted=True,
                request_count=1,
                requested_at=datetime.now(timezone.utc),
                completed_at=datetime.now(timezone.utc),
                transport_status="response_received",
                http_status=200,
                upstream_code=True,
                sdk_exception_type=None,
                sanitized_message_category="unknown",
                outcome="upstream_rejected",
                row_count=0,
                field_names=(),
            )

    def test_exception_type_is_fixed_and_does_not_echo_dynamic_class_name(self) -> None:
        dynamic = type("SecretPrefixFromToken", (Exception,), {})()
        self.assertEqual(safe_exception_type(dynamic), "OtherError")

    def test_four_way_conclusion_matrix(self) -> None:
        self.assertEqual(
            derive_conclusion((_result("sdk", "passed"), _result("http", "passed"))),
            "capability_probe_bug",
        )
        self.assertEqual(
            derive_conclusion(
                (_result("sdk", "sdk_preflight"), _result("http", "passed"))
            ),
            "sdk_client_problem",
        )
        self.assertEqual(
            derive_conclusion(
                (_result("sdk", "permission"), _result("http", "permission"))
            ),
            "token_or_account_problem",
        )
        self.assertEqual(
            derive_conclusion((_result("sdk", "network"), _result("http", "network"))),
            "network_transport_problem",
        )

    def test_receipt_round_trip_allows_preflight_failure_without_fake_request(self) -> None:
        started = datetime(2026, 8, 25, 6, 0, tzinfo=timezone.utc)
        receipt = build_diagnostic_receipt(
            diagnostic_run_id="contract-test",
            status="completed",
            started_at=started,
            completed_at=started + timedelta(seconds=2),
            endpoint="trade_cal",
            semantic_parameters={
                "end_date": "20260821",
                "exchange": "SSE",
                "start_date": "20260701",
            },
            sdk_version="not_loaded",
            python_version="3.12.13",
            credential_status="configured",
            config_sha256="1" * 64,
            diagnostic_code_sha256="2" * 64,
            git_commit="3" * 40,
            git_worktree_status="dirty",
            channels=(
                _result("sdk", "sdk_preflight"),
                _result("http", "passed"),
            ),
        )
        self.assertEqual(receipt.request_count, 1)
        encoded = canonical_json_bytes(receipt.to_dict())
        verified = verify_diagnostic_receipt(encoded)
        self.assertEqual(verified.conclusion, "sdk_client_problem")


class TushareDiagnosticRunnerTests(unittest.TestCase):
    def _output_root(self, temporary_path: str) -> Path:
        return Path(temporary_path) / "output"

    def _temporary_root(self) -> tempfile.TemporaryDirectory[str]:
        base = REPOSITORY_ROOT / "data" / "tmp" / "tushare-capability"
        base.mkdir(parents=True, exist_ok=True)
        return tempfile.TemporaryDirectory(prefix="diagnostic-test-", dir=base)

    def test_offline_plan_is_single_endpoint_and_side_effect_free(self) -> None:
        plan = runner.build_diagnostic_plan(
            endpoint="trade_cal",
            config_path=CONFIG,
            access_policy_path=POLICY,
        )
        self.assertEqual(plan["endpoint"], "trade_cal")
        self.assertEqual(plan["planned_request_count"], 2)
        self.assertEqual(plan["maximum_session_request_budget"], 4)
        self.assertEqual(
            plan["semantic_parameters"],
            {
                "end_date": "20260821",
                "exchange": "SSE",
                "start_date": "20260701",
            },
        )
        self.assertFalse(plan["credential_accessed"])
        self.assertFalse(plan["sdk_imported"])
        self.assertFalse(plan["network_accessed"])
        source = (REPOSITORY_ROOT / "agent" / "tushare_single_endpoint_diagnostic.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("run_live_probe", source)

    def test_live_cli_missing_clipboard_token_stops_before_budget_or_network(self) -> None:
        with (
            mock.patch.object(runner, "_read_tushare_token", return_value=""),
            mock.patch.object(runner, "run_live_diagnostic") as live,
            mock.patch.object(runner, "_emit_safe") as emit,
        ):
            exit_code = runner.main(
                [
                    "--live",
                    "--endpoint",
                    "trade_cal",
                    "--config",
                    str(CONFIG),
                    "--provider-access-policy",
                    str(POLICY),
                ]
            )
        self.assertEqual(exit_code, 2)
        live.assert_not_called()
        payload = emit.call_args.args[0]
        self.assertEqual(payload["credential_status"], "not_configured")
        self.assertEqual(payload["request_count"], 0)
        self.assertFalse(payload["round_budget_reserved"])
        self.assertFalse(payload["network_accessed"])

    def test_live_cli_malformed_clipboard_input_stops_before_budget_or_network(self) -> None:
        with (
            mock.patch.object(runner, "_read_tushare_token", return_value="trade_cal"),
            mock.patch.object(runner, "run_live_diagnostic") as live,
            mock.patch.object(runner, "_emit_safe") as emit,
        ):
            exit_code = runner.main(
                [
                    "--live",
                    "--endpoint",
                    "trade_cal",
                    "--config",
                    str(CONFIG),
                    "--provider-access-policy",
                    str(POLICY),
                ]
            )
        self.assertEqual(exit_code, 2)
        live.assert_not_called()
        payload = emit.call_args.args[0]
        self.assertEqual(payload["credential_status"], "rejected_by_local_preflight")
        self.assertEqual(payload["request_count"], 0)
        self.assertFalse(payload["round_budget_reserved"])
        self.assertFalse(payload["network_accessed"])

    def test_direct_live_call_rejects_malformed_secret_before_side_effects(self) -> None:
        with self._temporary_root() as temporary:
            output_root = self._output_root(temporary)
            session_factory = mock.Mock()
            with self.assertRaises(runner.TushareSingleEndpointDiagnosticError):
                runner.run_live_diagnostic(
                    endpoint="trade_cal",
                    token="trade_cal",
                    config_path=CONFIG,
                    access_policy_path=POLICY,
                    output_root=output_root,
                    session_factory=session_factory,
                )
            session_factory.assert_not_called()
            self.assertFalse(output_root.exists())

    def test_live_output_and_budget_roots_cannot_diverge(self) -> None:
        session_factory = mock.Mock()
        with self._temporary_root() as temporary:
            output_root = self._output_root(temporary)
            budget_root = Path(temporary) / "separate-budget"
            with self.assertRaises(runner.TushareSingleEndpointDiagnosticError):
                runner.run_live_diagnostic(
                    endpoint="trade_cal",
                    token="P0FixedRootSecretValue_20260825",
                    config_path=CONFIG,
                    access_policy_path=POLICY,
                    output_root=output_root,
                    budget_root=budget_root,
                    session_factory=session_factory,
                )
            session_factory.assert_not_called()
            self.assertFalse(output_root.exists())
            self.assertFalse(budget_root.exists())

    def test_live_fake_transport_sends_identical_payloads_once_and_seals_receipt(self) -> None:
        token = "P0SecretTokenValue_ThisMustNeverPersist_8472"
        envelope = {
            "code": 0,
            "msg": None,
            "data": {
                "fields": ["exchange", "cal_date", "is_open", "pretrade_date"],
                "items": [["SSE", "20260821", 1, "20260820"]],
            },
        }
        sdk_session = _FakeSession(_FakeResponse(envelope))
        http_session = _FakeSession(_FakeResponse(envelope))
        sessions = [sdk_session, http_session]
        client_module = SimpleNamespace(requests=object())
        fake_sdk = _fake_sdk(client_module, token)
        with self._temporary_root() as temporary:
            receipt, path = runner.run_live_diagnostic(
                endpoint="trade_cal",
                token=token,
                config_path=CONFIG,
                access_policy_path=POLICY,
                output_root=self._output_root(temporary),
                clock=_Clock(),
                sdk_loader=lambda: fake_sdk,
                sdk_client_module_loader=lambda: client_module,
                session_factory=lambda: sessions.pop(0),
                git_metadata_provider=lambda: runner.GitMetadata("4" * 40, "dirty"),
            )
            self.assertEqual(receipt.request_count, 2)
            self.assertEqual(receipt.conclusion, "capability_probe_bug")
            self.assertEqual([item.outcome for item in receipt.channels], ["passed", "passed"])
            self.assertEqual(len(sdk_session.calls), 1)
            self.assertEqual(len(http_session.calls), 1)
            sdk_call = sdk_session.calls[0]
            http_call = http_session.calls[0]
            self.assertEqual(sdk_call["url"], runner.OFFICIAL_HTTPS_API_URL)
            self.assertEqual(http_call["url"], runner.OFFICIAL_HTTPS_API_URL)
            self.assertEqual(sdk_call["json"], http_call["json"])
            self.assertNotIn("ts_type_name", sdk_call["json"]["params"])
            for call in (sdk_call, http_call):
                self.assertIs(call["allow_redirects"], False)
                self.assertIs(call["verify"], True)
                self.assertIs(call["stream"], True)
                self.assertEqual(call["timeout"], 30)
            raw = path.read_bytes()
            self.assertNotIn(token.encode("utf-8"), raw)
            self.assertNotIn(hashlib.sha256(token.encode("utf-8")).hexdigest().encode(), raw)
            self.assertNotIn(token[:12].encode("utf-8"), raw)
            self.assertNotIn(token[-12:].encode("utf-8"), raw)
            self.assertEqual(verify_diagnostic_receipt(raw).receipt_sha256, receipt.receipt_sha256)

    def test_uncaught_reserved_runner_failure_closes_round_before_postmortem(self) -> None:
        token = "P0RunnerFailureSecretValue_7712"
        envelope = {
            "code": 0,
            "msg": None,
            "data": {
                "fields": ["exchange", "cal_date", "is_open", "pretrade_date"],
                "items": [["SSE", "20260821", 1, "20260820"]],
            },
        }
        sessions = [
            _FakeSession(_FakeResponse(envelope)),
            _FakeSession(_FakeResponse(envelope)),
        ]
        client_module = SimpleNamespace(requests=object())
        fake_sdk = _fake_sdk(client_module, token)
        with self._temporary_root() as temporary:
            root = self._output_root(temporary)
            with self.assertRaises(RuntimeError):
                runner.run_live_diagnostic(
                    endpoint="trade_cal",
                    token=token,
                    config_path=CONFIG,
                    access_policy_path=POLICY,
                    output_root=root,
                    clock=_Clock(),
                    sdk_loader=lambda: fake_sdk,
                    sdk_client_module_loader=lambda: client_module,
                    session_factory=lambda: sessions.pop(0),
                    git_metadata_provider=lambda: (_ for _ in ()).throw(
                        RuntimeError("synthetic local failure")
                    ),
                )
            marker = runner._read_round_failure_marker(
                root / runner._ROUND_FAILURE_MARKER_NAME
            )
            self.assertEqual(marker["evidence_origin"], "runner_exception_boundary")
            self.assertEqual(marker["runner_exception_type"], "OtherError")
            self.assertFalse(marker["rerun_permitted"])
            with self.assertRaises(runner.TushareSingleEndpointDiagnosticError):
                runner._reserve_round_budget(
                    budget_root=root,
                    endpoint="daily",
                    run_id="must-stay-closed",
                    reserved_at=datetime(2026, 8, 25, 7, 0, tzinfo=timezone.utc),
                )

    def test_sdk_preflight_failure_still_runs_http_and_counts_one_real_request(self) -> None:
        token = "P0PreflightSecretValue_8831"
        envelope = {
            "code": 0,
            "msg": None,
            "data": {
                "fields": ["exchange", "cal_date", "is_open", "pretrade_date"],
                "items": [["SSE", "20260821", 1, "20260820"]],
            },
        }
        http_session = _FakeSession(_FakeResponse(envelope))
        with self._temporary_root() as temporary:
            receipt, _ = runner.run_live_diagnostic(
                endpoint="trade_cal",
                token=token,
                config_path=CONFIG,
                access_policy_path=POLICY,
                output_root=self._output_root(temporary),
                clock=_Clock(),
                sdk_loader=lambda: (_ for _ in ()).throw(RuntimeError("untrusted")),
                session_factory=lambda: http_session,
                git_metadata_provider=lambda: runner.GitMetadata("5" * 40, "dirty"),
            )
            self.assertEqual(receipt.request_count, 1)
            self.assertEqual(receipt.channels[0].request_count, 0)
            self.assertEqual(receipt.channels[0].outcome, "client_failed")
            self.assertEqual(receipt.channels[1].request_count, 1)
            self.assertEqual(receipt.conclusion, "sdk_client_problem")

    def test_sdk_preflight_network_attempt_is_blocked_before_dns_and_not_counted(self) -> None:
        token = "P0PreflightNetworkSecretValue_3344"
        envelope = {
            "code": 0,
            "msg": None,
            "data": {
                "fields": ["exchange", "cal_date", "is_open", "pretrade_date"],
                "items": [["SSE", "20260821", 1, "20260820"]],
            },
        }
        http_session = _FakeSession(_FakeResponse(envelope))

        def unsafe_loader() -> object:
            socket.getaddrinfo("api.tushare.pro", 443)
            raise AssertionError("network gate did not stop SDK preflight")

        with self._temporary_root() as temporary:
            receipt, _ = runner.run_live_diagnostic(
                endpoint="trade_cal",
                token=token,
                config_path=CONFIG,
                access_policy_path=POLICY,
                output_root=self._output_root(temporary),
                clock=_Clock(),
                sdk_loader=unsafe_loader,
                session_factory=lambda: http_session,
                git_metadata_provider=lambda: runner.GitMetadata("c" * 40, "dirty"),
            )
            self.assertEqual(receipt.request_count, 1)
            self.assertEqual(receipt.channels[0].request_count, 0)
            self.assertEqual(receipt.channels[0].outcome, "client_failed")
            self.assertEqual(receipt.channels[1].outcome, "passed")
            self.assertEqual(receipt.conclusion, "sdk_client_problem")

    def test_installed_sdk_is_forced_through_fake_safe_transport_without_network(self) -> None:
        try:
            sdk = runner._default_sdk_loader()
        except Exception as exc:  # pragma: no cover - dependency is optional elsewhere
            self.skipTest(f"Tushare SDK unavailable: {type(exc).__name__}")
        token = "P0InstalledSdkSecretValue_5177"
        envelope = {
            "code": 0,
            "msg": None,
            "data": {
                "fields": ["exchange", "cal_date", "is_open", "pretrade_date"],
                "items": [["SSE", "20260821", 1, "20260820"]],
            },
        }
        sdk_session = _FakeSession(_FakeResponse(envelope))
        http_session = _FakeSession(_FakeResponse(envelope))
        sessions = [sdk_session, http_session]
        with self._temporary_root() as temporary:
            receipt, _ = runner.run_live_diagnostic(
                endpoint="trade_cal",
                token=token,
                config_path=CONFIG,
                access_policy_path=POLICY,
                output_root=self._output_root(temporary),
                clock=_Clock(),
                sdk_loader=lambda: sdk,
                session_factory=lambda: sessions.pop(0),
                git_metadata_provider=lambda: runner.GitMetadata("7" * 40, "dirty"),
            )
            self.assertEqual(receipt.sdk_version, "1.4.29")
            self.assertEqual(receipt.request_count, 2)
            self.assertEqual([item.outcome for item in receipt.channels], ["passed", "passed"])
            self.assertEqual(len(sdk_session.calls), 1)
            self.assertEqual(sdk_session.calls[0]["url"], runner.OFFICIAL_HTTPS_API_URL)
            self.assertNotIn("ts_type_name", sdk_session.calls[0]["json"]["params"])

    def test_permission_code_produces_token_or_account_conclusion(self) -> None:
        token = "P0PermissionSecretValue_1122"
        envelope = {
            "code": 2002,
            "msg": "invalid parameter text must lose to code",
            "data": None,
        }
        sdk_session = _FakeSession(_FakeResponse(envelope))
        http_session = _FakeSession(_FakeResponse(envelope))
        sessions = [sdk_session, http_session]
        client_module = SimpleNamespace(requests=object())
        fake_sdk = _fake_sdk(client_module, token)
        with self._temporary_root() as temporary:
            receipt, path = runner.run_live_diagnostic(
                endpoint="trade_cal",
                token=token,
                config_path=CONFIG,
                access_policy_path=POLICY,
                output_root=self._output_root(temporary),
                clock=_Clock(),
                sdk_loader=lambda: fake_sdk,
                sdk_client_module_loader=lambda: client_module,
                session_factory=lambda: sessions.pop(0),
                git_metadata_provider=lambda: runner.GitMetadata("6" * 40, "dirty"),
            )
            self.assertEqual(receipt.conclusion, "token_or_account_problem")
            self.assertEqual(
                [item.sanitized_message_category for item in receipt.channels],
                ["permission", "permission"],
            )
            self.assertNotIn(token.encode("utf-8"), path.read_bytes())

    def test_second_send_is_rejected_before_another_network_call(self) -> None:
        response = _FakeResponse(
            {
                "code": 0,
                "msg": None,
                "data": {"fields": ["cal_date"], "items": [["20260821"]]},
            }
        )
        session = _FakeSession(response)
        observation = runner._WireObservation(
            diagnostic_attempted=True,
            requested_at=datetime.now(timezone.utc),
        )
        payload = {
            "api_name": "trade_cal",
            "token": "test-only-secret",
            "params": {
                "exchange": "SSE",
                "start_date": "20260701",
                "end_date": "20260821",
            },
            "fields": "",
        }
        runner._send_once(
            observation,
            session,
            payload=payload,
            expected_fields=("cal_date",),
        )
        with self.assertRaises(runner._RequestBudgetExceeded):
            runner._send_once(
                observation,
                session,
                payload=payload,
                expected_fields=("cal_date",),
            )
        self.assertEqual(observation.request_count, 1)
        self.assertEqual(len(session.calls), 1)

    def test_partial_fields_and_malformed_rows_are_not_accepted_as_success(self) -> None:
        expected = ("exchange", "cal_date", "is_open", "pretrade_date")
        partial = canonical_json_bytes(
            {
                "code": 0,
                "msg": None,
                "data": {
                    "fields": ["exchange", "cal_date"],
                    "items": [["SSE", "20260821"]],
                },
            }
        )
        _, _, rows, fields, valid = runner._extract_envelope(
            partial,
            expected_fields=expected,
        )
        self.assertEqual((rows, fields, valid), (0, (), False))

        malformed = canonical_json_bytes(
            {
                "code": 0,
                "msg": None,
                "data": {
                    "fields": list(expected),
                    "items": [["SSE", "20260821"]],
                },
            }
        )
        _, _, rows, fields, valid = runner._extract_envelope(
            malformed,
            expected_fields=expected,
        )
        self.assertEqual((rows, fields, valid), (0, (), False))

    def test_incomplete_trade_cal_slot_cannot_authorize_daily(self) -> None:
        instant = datetime(2026, 8, 25, 6, 0, tzinfo=timezone.utc)
        with self._temporary_root() as temporary:
            root = self._output_root(temporary)
            first = runner._reserve_round_budget(
                budget_root=root,
                endpoint="trade_cal",
                run_id="budget-trade-cal",
                reserved_at=instant,
            )
            self.assertTrue(first.is_file())
            with self.assertRaises(runner.TushareSingleEndpointDiagnosticError):
                runner._reserve_round_budget(
                    budget_root=root,
                    endpoint="trade_cal",
                    run_id="budget-trade-cal-repeat",
                    reserved_at=instant,
                )
            with self.assertRaises(runner.TushareSingleEndpointDiagnosticError):
                runner._reserve_round_budget(
                    budget_root=root,
                    endpoint="daily",
                    run_id="budget-daily-without-terminal-receipt",
                    reserved_at=instant,
                )
            self.assertFalse((root / ".p0-round-budget-slot-2.json").exists())

    def test_verified_terminal_trade_cal_receipt_authorizes_daily_once(self) -> None:
        token = "P0TerminalReceiptSecretValue_4401"
        envelope = {
            "code": 0,
            "msg": None,
            "data": {
                "fields": ["exchange", "cal_date", "is_open", "pretrade_date"],
                "items": [["SSE", "20260821", 1, "20260820"]],
            },
        }
        sdk_session = _FakeSession(_FakeResponse(envelope))
        http_session = _FakeSession(_FakeResponse(envelope))
        sessions = [sdk_session, http_session]
        client_module = SimpleNamespace(requests=object())
        fake_sdk = _fake_sdk(client_module, token)
        with self._temporary_root() as temporary:
            root = self._output_root(temporary)
            runner.run_live_diagnostic(
                endpoint="trade_cal",
                token=token,
                config_path=CONFIG,
                access_policy_path=POLICY,
                output_root=root,
                clock=_Clock(),
                sdk_loader=lambda: fake_sdk,
                sdk_client_module_loader=lambda: client_module,
                session_factory=lambda: sessions.pop(0),
                git_metadata_provider=lambda: runner.GitMetadata("4" * 40, "dirty"),
            )
            second = runner._reserve_round_budget(
                budget_root=root,
                endpoint="daily",
                run_id="budget-daily",
                reserved_at=datetime(2026, 8, 25, 7, 0, tzinfo=timezone.utc),
            )
            self.assertTrue(second.is_file())
            with self.assertRaises(runner.TushareSingleEndpointDiagnosticError):
                runner._reserve_round_budget(
                    budget_root=root,
                    endpoint="daily",
                    run_id="budget-daily-repeat",
                    reserved_at=datetime(2026, 8, 25, 7, 1, tzinfo=timezone.utc),
                )

    def test_round_execution_lock_is_cross_process_fail_closed(self) -> None:
        with self._temporary_root() as temporary:
            root = self._output_root(temporary)
            context = multiprocessing.get_context("spawn")
            results = context.Queue()
            with runner._exclusive_round_execution(root):
                process = context.Process(
                    target=_attempt_round_lock,
                    args=(str(root), results),
                )
                process.start()
                self.assertEqual(results.get(timeout=15), "blocked")
                process.join(timeout=15)
            self.assertEqual(process.exitcode, 0)

    def test_reserved_failure_can_be_sealed_offline_without_claiming_requests(self) -> None:
        instant = datetime(2026, 8, 25, 6, 0, tzinfo=timezone.utc)
        with self._temporary_root() as temporary:
            root = self._output_root(temporary)
            slot_path = runner._reserve_round_budget(
                budget_root=root,
                endpoint="trade_cal",
                run_id="failed-unsealed-run",
                reserved_at=instant,
            )
            runner._publish_round_failure_marker(
                budget_slot_path=slot_path,
                endpoint="trade_cal",
                run_id="failed-unsealed-run",
                failed_at=instant + timedelta(seconds=1),
                runner_exception_type="OtherError",
                failed_diagnostic_code_sha256="a" * 64,
            )
            with self.assertRaises(runner.TushareSingleEndpointDiagnosticError):
                runner._reserve_round_budget(
                    budget_root=root,
                    endpoint="daily",
                    run_id="daily-must-not-run-after-failure",
                    reserved_at=instant + timedelta(seconds=2),
                )
            receipt, path = runner.finalize_reserved_failure_postmortem(
                endpoint="trade_cal",
                output_root=root,
                clock=lambda: instant + timedelta(seconds=2),
                git_metadata_provider=lambda: runner.GitMetadata("b" * 40, "dirty"),
            )
            self.assertTrue(path.is_file())
            self.assertEqual(receipt.conclusion, "capability_probe_bug")
            self.assertIsNone(receipt.actual_request_count)
            self.assertEqual(receipt.actual_request_count_lower_bound, 0)
            self.assertEqual(receipt.actual_request_count_upper_bound, 2)
            self.assertTrue(
                all(
                    item.transport_status is None
                    and item.http_status is None
                    and item.upstream_code is None
                    and item.sdk_exception_type is None
                    and item.sanitized_message_category is None
                    for item in receipt.channels
                )
            )
            with self.assertRaises(runner.TushareSingleEndpointDiagnosticError):
                runner.finalize_reserved_failure_postmortem(
                    endpoint="trade_cal",
                    output_root=root,
                    clock=lambda: instant + timedelta(seconds=2),
                    git_metadata_provider=lambda: runner.GitMetadata(
                        "b" * 40, "dirty"
                    ),
                )

    def test_postmortem_requires_runner_failure_marker(self) -> None:
        instant = datetime(2026, 8, 25, 6, 0, tzinfo=timezone.utc)
        with self._temporary_root() as temporary:
            root = self._output_root(temporary)
            runner._reserve_round_budget(
                budget_root=root,
                endpoint="trade_cal",
                run_id="still-potentially-running",
                reserved_at=instant,
            )
            with self.assertRaises(runner.TushareSingleEndpointDiagnosticError):
                runner.finalize_reserved_failure_postmortem(
                    endpoint="trade_cal",
                    output_root=root,
                    clock=_Clock(),
                    git_metadata_provider=lambda: runner.GitMetadata(
                        "b" * 40, "dirty"
                    ),
                )
            self.assertFalse((root / "still-potentially-running").exists())

    def test_postmortem_rejects_any_completed_receipt_in_fixed_root(self) -> None:
        instant = datetime(2026, 8, 25, 6, 0, tzinfo=timezone.utc)
        with self._temporary_root() as temporary:
            root = self._output_root(temporary)
            slot_path = runner._reserve_round_budget(
                budget_root=root,
                endpoint="trade_cal",
                run_id="conflicting-completed-run",
                reserved_at=instant,
            )
            runner._publish_round_failure_marker(
                budget_slot_path=slot_path,
                endpoint="trade_cal",
                run_id="conflicting-completed-run",
                failed_at=instant + timedelta(seconds=1),
                runner_exception_type="OtherError",
                failed_diagnostic_code_sha256="a" * 64,
            )
            run_directory = root / "conflicting-completed-run"
            run_directory.mkdir()
            (run_directory / "diagnostic_receipt.json").write_bytes(b"conflict")
            with self.assertRaises(runner.TushareSingleEndpointDiagnosticError):
                runner.finalize_reserved_failure_postmortem(
                    endpoint="trade_cal",
                    output_root=root,
                    clock=lambda: instant + timedelta(seconds=2),
                    git_metadata_provider=lambda: runner.GitMetadata(
                        "b" * 40, "dirty"
                    ),
                )
            self.assertFalse(
                (run_directory / "diagnostic_postmortem.sealed.v3.json").exists()
            )

    def test_sdk_false_response_and_empty_data_still_complete_both_channels(self) -> None:
        cases = (
            (
                403,
                {"code": 2002, "msg": "permission denied", "data": None},
                "token_or_account_problem",
                "permission",
            ),
            (
                429,
                {"code": 9001, "msg": "too many requests", "data": None},
                "token_or_account_problem",
                "rate_limit",
            ),
            (
                503,
                {"code": 9002, "msg": "service unavailable", "data": None},
                "network_transport_problem",
                "server_internal",
            ),
            (
                200,
                {
                    "code": 0,
                    "msg": None,
                    "data": {
                        "fields": ["exchange", "cal_date", "is_open", "pretrade_date"],
                        "items": [],
                    },
                },
                "capability_probe_bug",
                "sdk_client",
            ),
        )
        for index, (status, envelope, conclusion, sdk_category) in enumerate(cases):
            with self.subTest(status=status):
                token = f"P0FalseResponseSecret_{index}_9917"
                sdk_session = _FakeSession(_FakeResponse(envelope, status_code=status))
                http_session = _FakeSession(_FakeResponse(envelope, status_code=status))
                sessions = [sdk_session, http_session]
                client_module = SimpleNamespace(requests=object())
                fake_sdk = _fake_sdk(client_module, token)
                with self._temporary_root() as temporary:
                    receipt, _ = runner.run_live_diagnostic(
                        endpoint="trade_cal",
                        token=token,
                        config_path=CONFIG,
                        access_policy_path=POLICY,
                        output_root=self._output_root(temporary),
                        clock=_Clock(),
                        sdk_loader=lambda: fake_sdk,
                        sdk_client_module_loader=lambda: client_module,
                        session_factory=lambda: sessions.pop(0),
                        git_metadata_provider=lambda: runner.GitMetadata(
                            "89ab"[index] * 40,
                            "dirty",
                        ),
                    )
                    self.assertEqual(receipt.request_count, 2)
                    self.assertEqual(receipt.conclusion, conclusion)
                    self.assertIsNone(receipt.channels[0].sdk_exception_type)
                    self.assertEqual(
                        receipt.channels[0].sanitized_message_category,
                        sdk_category,
                    )
                    self.assertEqual(len(sdk_session.calls), 1)
                    self.assertEqual(len(http_session.calls), 1)


if __name__ == "__main__":
    unittest.main()
