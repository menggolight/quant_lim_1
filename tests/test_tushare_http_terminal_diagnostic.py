from __future__ import annotations

import hashlib
import io
import inspect
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from unittest import mock

from agent import tushare_http_terminal_diagnostic as runner
from research.market_data.tushare_capability import canonical_json_bytes
from research.market_data.tushare_http_terminal import (
    TushareHttpTerminalError,
    verify_http_terminal_diagnostic_receipt,
)


_COUNT_FIELDS = (
    "reserved_request_count",
    "network_call_started_count",
    "response_received_count",
    "terminal_result_count",
    "remote_execution_unknown_count",
    "budget_consumed_count",
)


class _HardCrash(BaseException):
    """Test-only process-loss stand-in that bypasses ``except Exception``."""


class _StepClock:
    def __init__(self) -> None:
        self._value = datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        value = self._value
        self._value += timedelta(milliseconds=1)
        return value


class _Response:
    def __init__(self, body: bytes, *, status_code: int = 200) -> None:
        self.status_code = status_code
        self._body = body
        self.closed = False

    def iter_content(self, *, chunk_size: int) -> list[bytes]:
        if chunk_size <= 0:
            raise AssertionError("chunk_size must be positive")
        midpoint = max(1, len(self._body) // 2)
        return [self._body[:midpoint], self._body[midpoint:]]

    def close(self) -> None:
        self.closed = True


class _Session:
    def __init__(
        self,
        *,
        behavior: str,
        response_body: bytes,
        failure_text: str,
    ) -> None:
        self.behavior = behavior
        self.response_body = response_body
        self.failure_text = failure_text
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append((url, dict(kwargs)))
        if len(self.calls) > 1:
            raise AssertionError("HTTP terminal diagnostic sent more than once")
        if self.behavior == "hard_crash":
            raise _HardCrash(self.failure_text)
        if self.behavior == "runtime_error":
            raise RuntimeError(self.failure_text)
        if self.behavior != "response":
            raise AssertionError(f"unknown fake session behavior: {self.behavior}")
        return _Response(self.response_body)

    def close(self) -> None:
        self.closed = True


class _SessionFactory:
    def __init__(self, session: _Session) -> None:
        self.session = session
        self.calls = 0

    def __call__(self) -> _Session:
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("session factory was called more than once")
        return self.session


class _FailOnceWriter:
    def __init__(
        self,
        *,
        predicate: Callable[[Path], bool],
        failure_text: str,
    ) -> None:
        self._predicate = predicate
        self._failure_text = failure_text
        self.failed = False

    def __call__(self, path: Path, content: bytes) -> None:
        if not self.failed and self._predicate(path):
            self.failed = True
            raise OSError(self._failure_text)
        runner._default_artifact_writer(path, content)


class _BinaryStdout:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def value(self) -> bytes:
        return self.buffer.getvalue()


class TushareHttpTerminalDiagnosticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repository_root = runner.REPOSITORY_ROOT.resolve()
        temporary_parent = (
            cls.repository_root / "data" / "tmp" / "tushare-capability"
        )
        temporary_parent.mkdir(parents=True, exist_ok=True)
        cls._temporary = tempfile.TemporaryDirectory(
            prefix="http-terminal-tests-",
            dir=temporary_parent,
        )
        cls.test_root = Path(cls._temporary.name)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    @staticmethod
    def _git_metadata() -> runner.GitMetadata:
        return runner.GitMetadata("8" * 40, "dirty")

    def _run_arguments(
        self,
        *,
        run_directory: Path,
        token: str,
        clock: _StepClock,
        session_factory: _SessionFactory,
        artifact_writer: Callable[[Path, bytes], None],
        fault_injector: Callable[[str], None],
    ) -> dict[str, Any]:
        return {
            "token": token,
            "run_directory": run_directory,
            "repository_root": self.repository_root,
            "implementation_root": self.repository_root,
            "clock": clock,
            "session_factory": session_factory,
            "git_metadata_provider": self._git_metadata,
            "artifact_writer": artifact_writer,
            "fault_injector": fault_injector,
        }

    def _replay(
        self,
        *,
        run_directory: Path,
        clock: _StepClock,
    ) -> runner.PublishedHttpTerminalDiagnostic:
        with mock.patch.object(
            runner,
            "_new_no_retry_session",
            side_effect=AssertionError("offline replay attempted network setup"),
        ), mock.patch(
            "socket.create_connection",
            side_effect=AssertionError("offline replay attempted a socket"),
        ):
            return runner._replay_http_terminal_diagnostic_for_test(
                run_directory=run_directory,
                repository_root=self.repository_root,
                clock=clock,
            )

    def _assert_exact_counts(
        self,
        result: runner.PublishedHttpTerminalDiagnostic,
        expected: tuple[int, int, int, int, int, int],
    ) -> None:
        receipt = result.receipt
        actual = tuple(getattr(receipt.counts, field) for field in _COUNT_FIELDS)
        self.assertEqual(actual, expected)
        payload = receipt.to_dict()
        self.assertEqual(tuple(payload[field] for field in _COUNT_FIELDS), expected)
        self.assertEqual(
            expected[4],
            expected[1] - expected[2],
            "remote unknown must equal network-started minus response-received",
        )
        self.assertEqual(
            expected[5],
            expected[0],
            "budget consumption must equal the durable reservation count",
        )

    def _assert_canonical_receipt(
        self,
        result: runner.PublishedHttpTerminalDiagnostic,
    ) -> None:
        raw = result.receipt_path.read_bytes()
        verified = verify_http_terminal_diagnostic_receipt(raw)
        self.assertEqual(raw, canonical_json_bytes(verified.to_dict()))
        self.assertEqual(verified, result.receipt)
        self.assertEqual(
            result.receipt_file_sha256,
            hashlib.sha256(raw).hexdigest(),
        )

    def _assert_secret_absent(
        self,
        *,
        run_directory: Path,
        token: str,
        emitted: bytes,
    ) -> None:
        derivatives = (
            token.encode("utf-8"),
            hashlib.sha256(token.encode("utf-8")).hexdigest().encode("ascii"),
            token[:12].encode("utf-8"),
            token[-12:].encode("utf-8"),
        )
        persisted = b"".join(
            path.read_bytes()
            for path in sorted(run_directory.rglob("*"))
            if path.is_file()
        )
        for marker in derivatives:
            self.assertNotIn(marker, persisted)
            self.assertNotIn(marker, emitted)

    def _emit_summary(
        self,
        result: runner.PublishedHttpTerminalDiagnostic,
    ) -> bytes:
        capture = _BinaryStdout()
        with mock.patch.object(runner.sys, "stdout", capture):
            runner._emit(runner._safe_summary(result))
        return capture.value()

    def test_six_failure_points_replay_offline_with_exact_counts(self) -> None:
        cases = (
            (
                "before_reservation",
                "before_request_reserved",
                "response",
                None,
                (0, 0, 0, 1, 0, 0),
                0,
            ),
            (
                "after_reservation_before_network",
                "after_request_reserved",
                "response",
                None,
                (1, 0, 0, 1, 0, 1),
                0,
            ),
            (
                "network_started_without_response",
                None,
                "hard_crash",
                None,
                (1, 1, 0, 1, 1, 1),
                1,
            ),
            (
                "response_received_before_receipt",
                "after_response_received",
                "response",
                None,
                (1, 1, 1, 1, 0, 1),
                1,
            ),
            (
                "receipt_publish_interrupted",
                None,
                "response",
                "receipt",
                (1, 1, 1, 1, 0, 1),
                1,
            ),
            (
                "terminal_publish_interrupted",
                None,
                "runtime_error",
                "terminal",
                (1, 1, 0, 1, 1, 1),
                1,
            ),
        )

        for index, (
            name,
            fault_point,
            session_behavior,
            writer_failure,
            expected_counts,
            expected_http_calls,
        ) in enumerate(cases):
            with self.subTest(name=name):
                token = f"P0SyntheticHttpTerminalSecret_{index}_DoNotPersist_8842"
                token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
                failure_text = f"{token}|{token_hash}|{token[:12]}|{token[-12:]}"
                response_body = canonical_json_bytes(
                    {
                        "code": -2001,
                        "msg": failure_text,
                        "data": None,
                    }
                )
                session = _Session(
                    behavior=session_behavior,
                    response_body=response_body,
                    failure_text=failure_text,
                )
                session_factory = _SessionFactory(session)
                run_directory = self.test_root / f"case-{index}-{name}"
                clock = _StepClock()

                def inject(point: str, *, selected: str | None = fault_point) -> None:
                    if point == selected:
                        raise RuntimeError(failure_text)

                if writer_failure == "receipt":
                    writer: Callable[[Path, bytes], None] = _FailOnceWriter(
                        predicate=lambda path: path.name == runner.RECEIPT_NAME,
                        failure_text=failure_text,
                    )
                elif writer_failure == "terminal":
                    writer = _FailOnceWriter(
                        predicate=lambda path: path.name.endswith("_TERMINAL.json"),
                        failure_text=failure_text,
                    )
                else:
                    writer = runner._default_artifact_writer

                stdout = io.StringIO()
                stderr = io.StringIO()
                arguments = self._run_arguments(
                    run_directory=run_directory,
                    token=token,
                    clock=clock,
                    session_factory=session_factory,
                    artifact_writer=writer,
                    fault_injector=inject,
                )
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    if session_behavior == "hard_crash":
                        with self.assertRaises(_HardCrash):
                            runner._run_live_http_terminal_diagnostic_for_test(**arguments)
                    elif writer_failure in {"receipt", "terminal"}:
                        with self.assertRaises(OSError):
                            runner._run_live_http_terminal_diagnostic_for_test(**arguments)
                    else:
                        runner._run_live_http_terminal_diagnostic_for_test(**arguments)

                self.assertEqual(len(session.calls), expected_http_calls)
                self.assertEqual(session_factory.calls, expected_http_calls)
                calls_before_replay = len(session.calls)
                replayed = self._replay(run_directory=run_directory, clock=clock)
                self.assertTrue(replayed.replayed)
                self.assertEqual(len(session.calls), calls_before_replay)
                self._assert_exact_counts(replayed, expected_counts)
                self._assert_canonical_receipt(replayed)

                emitted = (
                    stdout.getvalue().encode("utf-8")
                    + stderr.getvalue().encode("utf-8")
                    + self._emit_summary(replayed)
                )
                self._assert_secret_absent(
                    run_directory=run_directory,
                    token=token,
                    emitted=emitted,
                )

                if expected_http_calls:
                    url, request = session.calls[0]
                    self.assertEqual(url, "https://api.tushare.pro")
                    self.assertEqual(request["json"]["api_name"], "trade_cal")
                    self.assertEqual(
                        request["json"]["params"],
                        dict(replayed.receipt.runtime_semantic_parameters),
                    )
                    self.assertEqual(request["json"]["fields"], "")
                    self.assertEqual(request["json"]["token"], token)
                    self.assertEqual(request["timeout"], runner.REQUEST_TIMEOUT_SECONDS)
                    self.assertIs(request["allow_redirects"], False)
                    self.assertIs(request["verify"], True)
                    self.assertIs(request["stream"], True)

    def test_http_only_trade_cal_max_one_and_receipt_hash_is_tamper_evident(self) -> None:
        plan = runner.build_http_terminal_plan()
        self.assertEqual(plan["endpoint"], "trade_cal")
        self.assertEqual(plan["channel"], "http")
        self.assertEqual(plan["max_requests"], 1)
        self.assertEqual(plan["planned_request_count"], 1)
        self.assertIs(plan["sdk_imported"], False)
        self.assertIs(plan["daily_allowed"], False)
        self.assertIs(plan["full_capability_probe_allowed"], False)
        self.assertIs(plan["automatic_retries_allowed"], False)

        parser = runner._parser()
        self.assertEqual(
            set(vars(parser.parse_args(["--live"]))),
            {"plan", "live", "replay"},
        )
        help_text = parser.format_help()
        for forbidden_option in ("--endpoint", "--sdk", "--daily", "--full"):
            self.assertNotIn(forbidden_option, help_text)
        for forbidden_symbol in (
            "run_live_probe",
            "_run_sdk_channel",
            "_default_sdk_loader",
            "run_daily",
        ):
            self.assertFalse(hasattr(runner, forbidden_symbol))
        self.assertEqual(
            tuple(inspect.signature(runner.run_live_http_terminal_diagnostic).parameters),
            ("token",),
        )
        self.assertEqual(
            tuple(inspect.signature(runner.replay_http_terminal_diagnostic).parameters),
            (),
        )
        self.assertFalse(hasattr(runner, "_call_with_suppressed_sdk_output"))

        token = "P0SyntheticHttpOnlySecret_NoPersistence_7719"
        body = canonical_json_bytes(
            {"code": -2001, "msg": "没有权限", "data": None}
        )
        session = _Session(
            behavior="response",
            response_body=body,
            failure_text="unused synthetic failure",
        )
        factory = _SessionFactory(session)
        run_directory = self.test_root / "http-only-success"
        clock = _StepClock()
        result = runner._run_live_http_terminal_diagnostic_for_test(
            **self._run_arguments(
                run_directory=run_directory,
                token=token,
                clock=clock,
                session_factory=factory,
                artifact_writer=runner._default_artifact_writer,
                fault_injector=lambda _point: None,
            )
        )
        self.assertEqual(len(session.calls), 1)
        self._assert_exact_counts(result, (1, 1, 1, 1, 0, 1))
        self.assertEqual(result.receipt.http_status, 200)
        self.assertEqual(result.receipt.upstream_code, -2001)
        self.assertEqual(result.receipt.sanitized_message_category, "permission")
        payload = result.receipt.to_dict()
        self.assertEqual(payload["endpoint"], "trade_cal")
        self.assertEqual(payload["transport_channel"], "http")
        self.assertEqual(payload["max_requests"], 1)
        self.assertIs(payload["sdk_ran"], False)
        self.assertIs(payload["automatic_retries_allowed"], False)
        self._assert_canonical_receipt(result)

        second_factory = _SessionFactory(
            _Session(
                behavior="response",
                response_body=body,
                failure_text="must not be called",
            )
        )
        with self.assertRaises(runner.TushareHttpTerminalRunnerError):
            runner._run_live_http_terminal_diagnostic_for_test(
                **self._run_arguments(
                    run_directory=run_directory,
                    token=token,
                    clock=clock,
                    session_factory=second_factory,
                    artifact_writer=runner._default_artifact_writer,
                    fault_injector=lambda _point: None,
                )
            )
        self.assertEqual(second_factory.calls, 0)

        raw = result.receipt_path.read_bytes()
        value = json.loads(raw)
        value["receipt_sha256"] = (
            "0" * 64 if value["receipt_sha256"] != "0" * 64 else "1" * 64
        )
        with self.assertRaises(TushareHttpTerminalError):
            verify_http_terminal_diagnostic_receipt(canonical_json_bytes(value))
        with self.assertRaises(Exception):
            verify_http_terminal_diagnostic_receipt(raw + b"\n")

        emitted = self._emit_summary(result)
        self._assert_secret_absent(
            run_directory=run_directory,
            token=token,
            emitted=emitted,
        )

        busy_directory = self.test_root / "busy-round"
        with runner._exclusive_authorized_round(busy_directory.parent):
            with self.assertRaises(runner.TushareHttpTerminalRoundBusyError):
                runner._replay_http_terminal_diagnostic_for_test(
                    run_directory=busy_directory,
                    repository_root=self.repository_root,
                    clock=clock,
                )

        capture = _BinaryStdout()
        with mock.patch.object(runner.sys, "stdout", capture), mock.patch.object(
            runner,
            "_read_tushare_token",
            return_value=token,
        ), mock.patch.object(
            runner,
            "run_live_http_terminal_diagnostic",
            side_effect=runner.TushareHttpTerminalRoundBusyError("synthetic busy"),
        ), mock.patch.object(
            runner,
            "replay_http_terminal_diagnostic",
        ) as forbidden_replay:
            self.assertEqual(runner.main(["--live"]), 2)
        forbidden_replay.assert_not_called()
        self.assertNotIn(token.encode("utf-8"), capture.value())


if __name__ == "__main__":
    unittest.main()
