from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.choice_candidate_probe import main as choice_candidate_probe_main
from agent.market_data_probe import ProbeArguments, main as market_data_probe_main, probe
from research.market_data.choice_candidates import ChoiceCandidateService
from research.market_data.choice_quality_growth_batch import (
    collect_choice_quality_growth_batch,
)
from research.market_data.contracts import MarketDataRequest
from research.market_data.index_evidence import INDEX_LEVEL, IndexEvidenceRequest
from research.market_data.provider_access import (
    ProviderAccessPolicyError,
    load_provider_access_policy,
    require_choice_network_access,
)
from research.market_data.providers.base import (
    ProviderAccessExpiredError,
    ProviderAccessPolicyInvalidError,
)
from research.market_data.providers.choice import ChoiceProvider
from research.market_data.providers.choice_index import ChoiceIndexProvider
from research.market_data.registry import MarketDataRegistry, load_market_data_config


NOW = datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc)


def choice_request(mode: str) -> MarketDataRequest:
    return MarketDataRequest(
        dataset_type="daily_bar",
        instrument_id="000333.SZ",
        start_date=date(2026, 8, 18),
        end_date=date(2026, 8, 18),
        adjustment="qfq",
        retrieval_mode=mode,
        requested_at=NOW,
    )


class CountingLoader:
    def __init__(self) -> None:
        self.calls = 0
        self.client = CountingClient()

    def __call__(self):
        self.calls += 1
        return SimpleNamespace(c=self.client)


class CountingClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def start(self, *_args):
        self.calls.append("start")
        return SimpleNamespace(ErrorCode=0, ErrorMsg="success")

    def csd(self, *_args):
        self.calls.append("csd")
        return SimpleNamespace(ErrorCode=0, ErrorMsg="success")


class NeverReadStorage:
    def __init__(self, config) -> None:
        self.admission_config = config
        self.load_calls = 0

    def load_latest_validated(self, **_kwargs):
        self.load_calls += 1
        raise AssertionError("expired Choice offline evidence must not be consumed")

    def load_latest_validated_for_diagnostics(self, **_kwargs):
        self.load_calls += 1
        raise AssertionError("unexpected diagnostic replay")


class CountingBaoStockProvider:
    provider_id = "baostock"
    adapter_version = "baostock-adapter-v1"
    upstream_source = "baostock.query_history_k_data_plus"
    supported_datasets = frozenset({"daily_bar", "trade_calendar", "security_master"})

    def __init__(self) -> None:
        self.fetch_calls = 0

    def fetch(self, _request):
        self.fetch_calls += 1
        raise AssertionError("Choice expiry must not trigger provider fallback")


class ChoiceExpiredAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_market_data_config()

    def assert_expired(self, context) -> ProviderAccessExpiredError:
        error = context.exception
        self.assertIs(type(error), ProviderAccessExpiredError)
        self.assertEqual(error.status, "failed")
        self.assertEqual(error.code, "provider_access_expired")
        self.assertEqual(error.provider_id, "choice")
        self.assertEqual(error.access_status, "expired")
        return error

    def test_v1_policy_is_strict_versioned_and_preserves_old_evidence(self) -> None:
        policy = load_provider_access_policy()
        self.assertEqual(policy.schema_version, "provider-access-policy-v1")
        self.assertEqual(policy.choice.access_status, "expired")
        self.assertFalse(policy.choice.network_fetch_allowed)
        self.assertFalse(policy.choice.diagnostic_session_allowed)
        self.assertFalse(policy.choice.offline_research_consumption_allowed)
        self.assertTrue(policy.choice.historical_evidence_preserved)
        self.assertFalse(policy.choice.automatic_fallback_allowed)
        self.assertFalse(policy.choice.partial_fallback_allowed)

        source = Path("configs/provider_access.v1.json")
        payload = json.loads(source.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            tampered = Path(directory) / "provider_access.v1.json"
            payload["choice"]["network_fetch_allowed"] = True
            tampered.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ProviderAccessPolicyError):
                load_provider_access_policy(tampered)
            payload["choice"]["network_fetch_allowed"] = False
            payload["choice"]["caller_override"] = True
            tampered.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ProviderAccessPolicyError):
                load_provider_access_policy(tampered)

    def test_invalid_or_missing_policy_fails_before_sdk_loader(self) -> None:
        loader = CountingLoader()
        provider = ChoiceProvider(sdk_loader=loader, clock=lambda: NOW)
        with patch(
            "research.market_data.provider_access.load_provider_access_policy",
            side_effect=ProviderAccessPolicyError("missing"),
        ):
            with self.assertRaises(ProviderAccessPolicyInvalidError) as caught:
                provider.fetch(choice_request("historical_backfill"))
        self.assertEqual(caught.exception.code, "provider_access_policy_invalid")
        self.assertEqual(loader.calls, 0)
        self.assertEqual(loader.client.calls, [])

    def test_diagnostic_session_is_rejected_before_sdk_import_or_start(self) -> None:
        loader = CountingLoader()
        provider = ChoiceProvider(sdk_loader=loader, clock=lambda: NOW)
        with self.assertRaises(ProviderAccessExpiredError) as caught:
            with provider.diagnostic_session():
                self.fail("expired Choice session must not open")
        error = self.assert_expired(caught)
        self.assertEqual(error.operation, "diagnostic_session")
        self.assertEqual(loader.calls, 0)
        self.assertEqual(loader.client.calls, [])

        with patch(
            "research.market_data.providers.choice.importlib.import_module",
            side_effect=AssertionError("expired policy must precede SDK import"),
        ) as sdk_import:
            with self.assertRaises(ProviderAccessExpiredError):
                ChoiceProvider(clock=lambda: NOW)._load_sdk()
        sdk_import.assert_not_called()

    def test_live_capture_and_historical_backfill_fail_before_sdk_import(self) -> None:
        for mode in ("live_capture", "historical_backfill"):
            with self.subTest(mode=mode):
                loader = CountingLoader()
                provider = ChoiceProvider(sdk_loader=loader, clock=lambda: NOW)
                with self.assertRaises(ProviderAccessExpiredError) as caught:
                    provider.fetch(choice_request(mode))
                error = self.assert_expired(caught)
                self.assertEqual(error.operation, f"network_fetch_{mode}")
                self.assertEqual(loader.calls, 0)
                self.assertEqual(loader.client.calls, [])

    def test_sdk_start_and_data_methods_are_guarded_even_if_called_directly(self) -> None:
        client = CountingClient()
        for operation, args in (
            ("start", ("options", lambda _value: 1)),
            ("csd", ("000333.SZ", "CLOSE", "2026-08-18", "2026-08-18", "")),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(ProviderAccessExpiredError) as caught:
                    ChoiceProvider._sdk_call(client, operation, *args)
                self.assert_expired(caught)
        self.assertEqual(client.calls, [])

    def test_candidate_and_index_online_paths_fail_before_sdk_import(self) -> None:
        candidate_loader = CountingLoader()
        candidate_provider = ChoiceProvider(
            sdk_loader=candidate_loader, clock=lambda: NOW
        )
        with tempfile.TemporaryDirectory() as directory:
            service = ChoiceCandidateService(
                Path(directory), provider=candidate_provider
            )
            with self.assertRaises(ProviderAccessExpiredError) as caught:
                service.fetch_sw2021_classification("000333.SZ")
            self.assert_expired(caught)
            self.assertEqual(list(Path(directory).rglob("*")), [])
        self.assertEqual(candidate_loader.calls, 0)

        index_loader = CountingLoader()
        request = IndexEvidenceRequest(
            dataset_type=INDEX_LEVEL,
            index_ids=("000986.CSI",),
            start_date=date(2026, 8, 18),
            end_date=date(2026, 8, 18),
            retrieval_mode="historical_backfill",
            requested_at=NOW,
        )
        with self.assertRaises(ProviderAccessExpiredError) as caught:
            ChoiceIndexProvider(
                sdk_loader=index_loader, clock=lambda: NOW
            ).fetch(request)
        self.assert_expired(caught)
        self.assertEqual(index_loader.calls, 0)

    def test_registry_diagnostic_fetch_is_structured_and_does_not_start_sdk(self) -> None:
        loader = CountingLoader()
        registry = MarketDataRegistry(
            self.config,
            providers=(ChoiceProvider(sdk_loader=loader, clock=lambda: NOW),),
        )
        with self.assertRaises(ProviderAccessExpiredError) as caught:
            registry.fetch_diagnostic(
                choice_request("historical_backfill"), provider_id="choice"
            )
        self.assert_expired(caught)
        self.assertEqual(loader.calls, 0)
        result = probe(
            registry,
            ProbeArguments(
                provider="choice",
                dataset="daily_bar",
                instrument="000333.SZ",
                start_date=date(2026, 8, 18),
                end_date=date(2026, 8, 18),
                retrieval_mode="historical_backfill",
                adjustment="qfq",
            ),
            requested_at=NOW,
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "provider_access_expired")
        self.assertEqual(loader.calls, 0)

    def test_formal_offline_research_replay_is_blocked_before_storage_read(self) -> None:
        storage = NeverReadStorage(self.config)
        registry = MarketDataRegistry(
            self.config,
            storage=storage,  # type: ignore[arg-type]
            providers=(ChoiceProvider(sdk_loader=CountingLoader()),),
        )
        with self.assertRaises(ProviderAccessExpiredError) as caught:
            registry.fetch(choice_request("offline_replay"), provider_id="choice")
        error = self.assert_expired(caught)
        self.assertEqual(error.operation, "offline_research_consumption")
        self.assertEqual(storage.load_calls, 0)

    def test_expiry_is_a_global_stop_not_an_automatic_fallback_signal(self) -> None:
        loader = CountingLoader()
        baostock = CountingBaoStockProvider()
        registry = MarketDataRegistry(
            self.config,
            providers=(
                ChoiceProvider(sdk_loader=loader, clock=lambda: NOW),
                baostock,
            ),
        )
        with self.assertRaises(ProviderAccessExpiredError) as caught:
            registry.fetch_with_fallback(
                choice_request("historical_backfill"),
                provider_ids=("choice", "baostock"),
            )
        self.assert_expired(caught)
        self.assertEqual(loader.calls, 0)
        self.assertEqual(baostock.fetch_calls, 0)

    def test_expiry_precedes_registry_resolution_and_stops_fallback(self) -> None:
        cases = ("unregistered", "disabled", "invalid_policy")
        for case in cases:
            with self.subTest(case=case):
                config = json.loads(json.dumps(self.config))
                baostock = CountingBaoStockProvider()
                providers = [baostock]
                if case != "unregistered":
                    providers.insert(0, ChoiceProvider(sdk_loader=CountingLoader()))
                if case == "disabled":
                    config["providers"]["choice"]["enabled"] = False
                registry = MarketDataRegistry(config, providers=tuple(providers))
                policy_patch = (
                    patch(
                        "research.market_data.provider_access.load_provider_access_policy",
                        side_effect=ProviderAccessPolicyError("missing"),
                    )
                    if case == "invalid_policy"
                    else patch(
                        "research.market_data.provider_access.load_provider_access_policy",
                        wraps=load_provider_access_policy,
                    )
                )
                with policy_patch:
                    expected = (
                        ProviderAccessPolicyInvalidError
                        if case == "invalid_policy"
                        else ProviderAccessExpiredError
                    )
                    with self.assertRaises(expected):
                        registry.fetch_with_fallback(
                            choice_request("historical_backfill"),
                            provider_ids=("choice", "baostock"),
                        )
                self.assertEqual(baostock.fetch_calls, 0)

    def test_expired_cli_outputs_are_create_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for cli, arguments in (
                (
                    choice_candidate_probe_main,
                    [
                        "--mode",
                        "online",
                        "--storage-root",
                        str(root / "candidate-storage"),
                        "--output",
                        str(root / "candidate.json"),
                        "sw2021",
                        "--instrument",
                        "000333.SZ",
                    ],
                ),
                (
                    market_data_probe_main,
                    [
                        "--provider",
                        "choice",
                        "--dataset",
                        "daily_bar",
                        "--instrument",
                        "000333.SZ",
                        "--start-date",
                        "2026-08-18",
                        "--end-date",
                        "2026-08-18",
                        "--adjustment",
                        "qfq",
                        "--storage-root",
                        str(root / "market-storage"),
                        "--output",
                        str(root / "market.json"),
                    ],
                ),
            ):
                output = Path(arguments[arguments.index("--output") + 1])
                output.write_bytes(b"preserved-choice-evidence")
                stdout = io.StringIO()
                with redirect_stdout(stdout), self.assertRaises(FileExistsError):
                    cli(arguments)
                self.assertEqual(output.read_bytes(), b"preserved-choice-evidence")
                self.assertEqual(stdout.getvalue(), "")

    def test_expired_batch_attempt_does_not_delete_overwrite_or_create_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for folder in ("raw", "quarantine", "validated", "diagnostic"):
                path = root / folder / "preserved.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"old-{folder}".encode("utf-8"))
            before = {
                item.relative_to(root).as_posix(): (
                    item.read_bytes(),
                    item.stat().st_mtime_ns,
                )
                for item in root.rglob("*")
                if item.is_file()
            }
            with self.assertRaises(ProviderAccessExpiredError) as caught:
                collect_choice_quality_growth_batch(
                    provider=ChoiceProvider(sdk_loader=CountingLoader()),
                    cutoff_date=date(2026, 8, 18),
                    as_of=NOW,
                    output_root=root / "new-batch",
                    clock=lambda: NOW,
                )
            self.assert_expired(caught)
            after = {
                item.relative_to(root).as_posix(): (
                    item.read_bytes(),
                    item.stat().st_mtime_ns,
                )
                for item in root.rglob("*")
                if item.is_file()
            }
            self.assertEqual(after, before)
            self.assertFalse((root / "new-batch").exists())


if __name__ == "__main__":
    unittest.main()
