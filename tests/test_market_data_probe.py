import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from agent.market_data_probe import ProbeArguments, main, probe
from research.market_data.contracts import MarketDataBatch, MarketDataRequest
from research.market_data.providers.base import (
    DependencyMissingError,
    NetworkBlockedError,
    ProviderNotConfiguredError,
)


TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 8, 6, 10, 0, tzinfo=TZ)


class FakeRegistry:
    def __init__(self, result):
        self.result = result

    def fetch(self, request, provider_id=None):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class InterruptingRegistry:
    def fetch(self, request, provider_id=None):
        raise KeyboardInterrupt("operator interrupt")


class ChoiceDiagnosticRegistry:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def fetch_diagnostic(self, request, *, provider_id):
        self.requests.append((request, provider_id))
        return self.result


def arguments(provider="baostock"):
    return ProbeArguments(
        provider=provider,
        dataset="daily_bar",
        instrument="000333.SZ",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 5),
        retrieval_mode="historical_backfill",
    )


def batch():
    request = MarketDataRequest(
        dataset_type="daily_bar",
        instrument_id="000333.SZ",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 5),
        retrieval_mode="historical_backfill",
        requested_at=NOW,
    )
    record = {
        "instrument_id": "000333.SZ",
        "trading_date": "2026-08-05",
        "open": "50",
        "high": "52",
        "low": "49",
        "close": "51",
        "preclose": "49.8",
        "volume": "100",
        "amount": "5100",
        "currency": "CNY",
        "adjustment": "none",
        "trading_status": "traded",
        "available_at": "2026-08-05T15:30:00+08:00",
        "availability_status": "policy_estimated",
        "source_record_id": "record-1",
    }
    return MarketDataBatch(
        batch_id="baostock-daily_bar-fixture",
        provider_id="baostock",
        upstream_source="baostock.query_history_k_data_plus",
        dataset_type="daily_bar",
        schema_version="daily-bar-v1",
        adapter_version="baostock-adapter-v1",
        request_fingerprint=request.fingerprint("baostock", "baostock-adapter-v1"),
        request_payload=request.fingerprint_payload(
            "baostock", "baostock-adapter-v1"
        ),
        retrieval_mode="historical_backfill",
        requested_at=NOW,
        fetched_at=NOW,
        available_at_min=datetime(2026, 8, 5, 15, 30, tzinfo=TZ),
        available_at_max=datetime(2026, 8, 5, 15, 30, tzinfo=TZ),
        raw_content_sha256="2" * 64,
        normalized_content_sha256="3" * 64,
        record_count=1,
        completeness_status="complete",
        freshness_status="historical_backfill",
        admission_status="validated_research_only",
        point_in_time_status="historical_backfill_not_original_capture",
        synthetic=False,
        issues=(),
        records=(record,),
    )


class MarketDataProbeTest(unittest.TestCase):
    def test_nonempty_validated_batch_is_reported_as_passed(self):
        result = probe(
            FakeRegistry(batch()),
            arguments(),
            requested_at=NOW,
        )
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["record_count"], 1)
        self.assertEqual(result["date_range"], {"start": "2026-08-05", "end": "2026-08-05"})
        self.assertEqual(result["evidence_mode"], "test_injected")
        self.assertEqual(result["upstream_source"], "baostock.query_history_k_data_plus")
        self.assertEqual(result["adapter_version"], "baostock-adapter-v1")
        self.assertRegex(result["request_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertEqual(result["completeness_status"], "complete")

    def test_dependency_network_and_configuration_failures_are_distinct(self):
        cases = (
            (DependencyMissingError("missing SDK"), "dependency_missing"),
            (NetworkBlockedError("socket blocked"), "network_blocked"),
            (ProviderNotConfiguredError("token missing"), "not_configured"),
        )
        for error, expected in cases:
            with self.subTest(expected=expected):
                result = probe(
                    FakeRegistry(error),
                    arguments(),
                    requested_at=NOW,
                )
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["record_count"], 0)
                self.assertEqual(result["admission_status"], "failed")
                for field in (
                    "upstream_source",
                    "adapter_version",
                    "schema_version",
                    "request_fingerprint",
                    "date_range",
                    "raw_content_sha256",
                    "normalized_content_sha256",
                ):
                    self.assertIn(field, result)

    def test_failure_output_redacts_token_values(self):
        result = probe(
            FakeRegistry(ProviderNotConfiguredError("token is SUPERSECRET")),
            arguments("tushare"),
            requested_at=NOW,
        )
        rendered = json.dumps(result)
        self.assertNotIn("SUPERSECRET", rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_failure_output_redacts_quoted_mapping_credentials(self):
        result = probe(
            FakeRegistry(
                ProviderNotConfiguredError(
                    "sdk error {'api_key': 'SUPERSECRET', 'reason': 'missing'}"
                )
            ),
            arguments("tushare"),
            requested_at=NOW,
        )
        rendered = json.dumps(result)
        self.assertNotIn("SUPERSECRET", rendered)
        self.assertIn("[REDACTED]", result["error"])

    def test_unexpected_local_os_errors_are_failed_not_network_blocked(self):
        for error in (PermissionError("access denied"), OSError("local disk I/O failed")):
            with self.subTest(error=type(error).__name__):
                result = probe(
                    FakeRegistry(error),
                    arguments(),
                    requested_at=NOW,
                )
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["error_code"], "provider_query_failed")

    def test_unexpected_connection_and_timeout_errors_are_network_blocked(self):
        for error in (ConnectionError("connection refused"), TimeoutError("timed out")):
            with self.subTest(error=type(error).__name__):
                result = probe(
                    FakeRegistry(error),
                    arguments(),
                    requested_at=NOW,
                )
                self.assertEqual(result["status"], "network_blocked")
                self.assertEqual(result["error_code"], "network_blocked")

    def test_probe_does_not_swallow_process_control_exceptions(self):
        with self.assertRaises(KeyboardInterrupt):
            probe(
                InterruptingRegistry(),
                arguments(),
                requested_at=NOW,
            )

    def test_programmatic_caller_cannot_label_fake_registry_as_real(self):
        fake = FakeRegistry(batch())
        fake.evidence_mode = "configured_runtime"
        result = probe(fake, arguments(), requested_at=NOW)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["evidence_mode"], "test_injected")

    def test_choice_probe_uses_explicit_diagnostic_boundary_and_adjustment(self):
        registry = ChoiceDiagnosticRegistry(batch())
        choice_arguments = ProbeArguments(
            provider="choice",
            dataset="daily_bar",
            instrument="000333.SZ",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 5),
            retrieval_mode="historical_backfill",
            adjustment="qfq",
        )
        result = probe(registry, choice_arguments, requested_at=NOW)
        self.assertEqual(result["status"], "passed")
        request, provider_id = registry.requests[0]
        self.assertEqual(provider_id, "choice")
        self.assertEqual(request.adjustment, "qfq")
        self.assertEqual(result["adjustment"], "qfq")

    def test_cli_accepts_explicit_adjustment(self):
        stdout = io.StringIO()
        registry = ChoiceDiagnosticRegistry(batch())
        with patch(
            "agent.market_data_probe.MarketDataRegistry.configured",
            return_value=registry,
        ), redirect_stdout(stdout):
            exit_code = main(
                [
                    "--provider",
                    "choice",
                    "--dataset",
                    "daily_bar",
                    "--instrument",
                    "000333.SZ",
                    "--start-date",
                    "2026-08-01",
                    "--end-date",
                    "2026-08-05",
                    "--adjustment",
                    "qfq",
                ]
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["adjustment"], "qfq")

    def test_main_registry_initialization_failure_is_structured_json(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch(
            "agent.market_data_probe.MarketDataRegistry.configured",
            side_effect=PermissionError("cannot read local config"),
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(
                [
                    "--provider",
                    "baostock",
                    "--dataset",
                    "daily_bar",
                    "--instrument",
                    "000333.SZ",
                    "--start-date",
                    "2026-08-01",
                    "--end-date",
                    "2026-08-05",
                ]
            )

        result = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "provider_query_failed")
        self.assertEqual(result["error_type"], "PermissionError")
        self.assertEqual(result["record_count"], 0)
        self.assertEqual(stderr.getvalue(), "")

    def test_main_marks_offline_replay_as_cache_evidence(self):
        stdout = io.StringIO()
        with patch(
            "agent.market_data_probe.MarketDataRegistry.configured",
            return_value=FakeRegistry(DependencyMissingError("cache miss")),
        ), redirect_stdout(stdout):
            exit_code = main(
                [
                    "--provider",
                    "baostock",
                    "--dataset",
                    "daily_bar",
                    "--instrument",
                    "000333.SZ",
                    "--start-date",
                    "2026-08-01",
                    "--end-date",
                    "2026-08-05",
                    "--retrieval-mode",
                    "offline_replay",
                ]
            )
        result = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(result["evidence_mode"], "test_injected")


if __name__ == "__main__":
    unittest.main()
