from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from research.market_data.admission import evaluate_admission
from research.market_data.contracts import (
    MarketDataRequest,
    canonical_json_bytes,
    sha256_bytes,
)
from research.market_data.providers.base import (
    BatchValidationError,
    DependencyMissingError,
    IncompleteDatasetError,
    NetworkBlockedError,
    NoTradingDaysError,
    ProviderNotConfiguredError,
    ProviderQuotaExceededError,
    ProviderError,
    ProviderQueryError,
    UnsupportedDatasetError,
    safe_error_text,
)
from research.market_data.providers.choice import ChoiceProvider
from research.market_data.registry import MarketDataRegistry, load_market_data_config
from research.market_data.storage import MarketDataStorage, MarketDataStorageError


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
NOW = datetime(2026, 8, 11, 10, 30, tzinfo=CHINA_TZ)


def daily_request() -> MarketDataRequest:
    return MarketDataRequest(
        dataset_type="daily_bar",
        instrument_id="000333.SZ",
        start_date=date(2026, 8, 3),
        end_date=date(2026, 8, 7),
        adjustment="qfq",
        retrieval_mode="historical_backfill",
        requested_at=NOW,
    )


def result(error_code=0, error_message="success", **values):
    return SimpleNamespace(
        ErrorCode=error_code,
        ErrorMsg=error_message,
        **values,
    )


def daily_result(*, code="000333.SZ", indicators=None, dates=None, columns=None):
    indicators = (
        list(ChoiceProvider._DAILY_INDICATORS)
        if indicators is None
        else indicators
    )
    dates = ["2026-08-03", "2026-08-04"] if dates is None else dates
    columns = columns if columns is not None else [
        ["50.00", "51.00"],
        ["52.00", "53.00"],
        ["49.00", "50.00"],
        ["51.00", "52.00"],
        ["49.80", "51.00"],
        ["100000", "120000"],
        ["5100000", "6240000"],
    ]
    return result(
        Codes=[code],
        Indicators=indicators,
        Dates=dates,
        Data={code: columns},
    )


def calendar_result(*, dates=None):
    dates = ["2026/8/3", "2026/8/4"] if dates is None else dates
    return result(
        Codes=[""],
        Indicators=["TRADEDATE"],
        Dates=dates,
        Data=list(dates),
    )


class FakeChoiceClient:
    def __init__(self, *, login=None, daily=None, calendar=None, stopped=None):
        self.login = login or result()
        self.daily = daily or daily_result()
        self.calendar = calendar or calendar_result()
        self.stopped = stopped or result()
        self.start_calls = []
        self.csd_calls = []
        self.tradedates_calls = []
        self.stop_calls = 0

    def start(self, options, callback):
        self.start_calls.append((options, callback))
        return self.login

    def csd(self, *args):
        self.csd_calls.append(args)
        return self.daily

    def tradedates(self, *args):
        self.tradedates_calls.append(args)
        return self.calendar

    def stop(self):
        self.stop_calls += 1
        return self.stopped

    def porder(self, *_):
        raise AssertionError("state-changing Choice function must never be called")


def index_request(*, adjustment="none") -> MarketDataRequest:
    return MarketDataRequest(
        dataset_type="daily_bar",
        instrument_id="000300.SH",
        start_date=date(2026, 8, 3),
        end_date=date(2026, 8, 7),
        adjustment=adjustment,
        retrieval_mode="historical_backfill",
        requested_at=NOW,
    )


def trade_calendar_request(*, mode="historical_backfill") -> MarketDataRequest:
    return MarketDataRequest(
        dataset_type="trade_calendar",
        start_date=date(2026, 8, 3),
        end_date=date(2026, 8, 7),
        retrieval_mode=mode,
        requested_at=NOW,
    )


class ChoiceProviderTests(unittest.TestCase):
    def setUp(self):
        for target in (
            "research.market_data.provider_access.require_choice_network_access",
            "research.market_data.provider_access.require_choice_diagnostic_session",
            "research.market_data.provider_access.require_choice_offline_research_consumption",
        ):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.config = load_market_data_config()

    def registry(self, provider, root):
        return MarketDataRegistry(
            self.config,
            storage=MarketDataStorage(root, admission_config=self.config),
            providers=(provider,),
        )

    def test_read_only_stock_daily_bar_is_qfq_secondary_and_stops_session(self):
        client = FakeChoiceClient()
        provider = ChoiceProvider(
            sdk_loader=lambda: SimpleNamespace(c=client),
            clock=lambda: NOW,
        )
        with tempfile.TemporaryDirectory() as directory:
            storage = MarketDataStorage(
                Path(directory),
                admission_config=self.config,
                allow_test_receipts=True,
            )
            registry = MarketDataRegistry(
                self.config,
                storage=storage,
                providers=(provider,),
            )
            batch = registry.fetch_diagnostic(
                daily_request(), provider_id="choice"
            )
            batch_path = next(Path(directory).rglob(f"{batch.batch_id}.json"))
            with self.assertRaisesRegex(
                MarketDataStorageError,
                "not locally admitted for research consumption",
            ):
                storage.read_for_research(batch_path)
            self.assertEqual(
                storage.read_for_diagnostics(batch_path).batch_id,
                batch.batch_id,
            )
        self.assertEqual(batch.provider_id, "choice")
        self.assertEqual(
            batch.upstream_source,
            "choice.eastmoney_emquantapi.csd_with_tradedates",
        )
        self.assertEqual(batch.admission_status, "validated_secondary_not_primary")
        self.assertEqual(
            batch.point_in_time_status,
            "historical_backfill_not_original_capture",
        )
        self.assertIn(
            "test_injected_not_formal_evidence",
            {str(issue.get("code")) for issue in batch.issues},
        )
        self.assertEqual(batch.record_count, 2)
        self.assertEqual(batch.records[0]["trading_status"], "unknown")
        self.assertEqual(batch.records[0]["adjustment"], "qfq")
        self.assertEqual(client.stop_calls, 1)
        login_options = client.start_calls[0][0]
        self.assertIn("RecordLoginInfo=0", login_options)

    def test_bounded_registry_diagnostic_reuses_one_login_for_many_fetches(self):
        client = FakeChoiceClient()
        provider = ChoiceProvider(
            sdk_loader=lambda: SimpleNamespace(c=client),
            clock=lambda: NOW,
        )
        with tempfile.TemporaryDirectory() as directory:
            registry = self.registry(provider, Path(directory))
            with registry.diagnostic_session(provider_id="choice"):
                registry.fetch_diagnostic(
                    trade_calendar_request(), provider_id="choice"
                )
                registry.fetch_diagnostic(daily_request(), provider_id="choice")
        self.assertEqual(len(client.start_calls), 1)
        self.assertEqual(client.stop_calls, 1)
        self.assertEqual(len(client.csd_calls), 1)
        self.assertEqual(len(client.tradedates_calls), 2)
        login_options = client.start_calls[0][0]
        self.assertNotIn("UserName", login_options)
        self.assertNotIn("PassWord", login_options)
        self.assertNotIn("PhoneNumber", login_options)
        self.assertNotIn("ForceLogin", login_options)
        csd = client.csd_calls[0]
        self.assertEqual(csd[0], "000333.SZ")
        self.assertIn("AdjustFlag=3", csd[4])
        self.assertIn("filldata=0", csd[4])
        self.assertNotIn("AdjustFlag=2", csd[4])
        self.assertNotIn("AdjustFlag=1", csd[4])
        self.assertIn("Market=CNSESH", client.tradedates_calls[0][2])
        self.assertIn("Market=CNSESZ", client.tradedates_calls[1][2])

    def test_trade_calendar_is_independent_complete_secondary_dataset(self):
        client = FakeChoiceClient(
            calendar=calendar_result(dates=["2026/8/3", "2026/8/5"])
        )
        provider = ChoiceProvider(
            sdk_loader=lambda: SimpleNamespace(c=client), clock=lambda: NOW
        )
        with tempfile.TemporaryDirectory() as directory:
            batch = self.registry(provider, Path(directory)).fetch_diagnostic(
                trade_calendar_request(), provider_id="choice"
            )
        self.assertEqual(batch.record_count, 5)
        self.assertEqual(
            [record["calendar_date"] for record in batch.records],
            [
                "2026-08-03",
                "2026-08-04",
                "2026-08-05",
                "2026-08-06",
                "2026-08-07",
            ],
        )
        self.assertEqual(
            [record["is_trading_day"] for record in batch.records],
            [True, False, True, False, False],
        )
        self.assertEqual(
            batch.upstream_source,
            "choice.eastmoney_emquantapi.tradedates",
        )
        self.assertEqual(batch.admission_status, "validated_secondary_not_primary")
        self.assertEqual(client.csd_calls, [])
        self.assertEqual(len(client.tradedates_calls), 1)
        self.assertEqual(client.stop_calls, 1)

    def test_configured_diagnostic_replay_preserves_formal_gate_and_raw_hashes(self):
        client = FakeChoiceClient()
        with tempfile.TemporaryDirectory() as directory, patch.object(
            ChoiceProvider,
            "_load_sdk",
            return_value=SimpleNamespace(c=client),
        ), patch.object(ChoiceProvider, "_aware_clock", return_value=NOW):
            registry = MarketDataRegistry.configured(storage_root=Path(directory))
            original = registry.fetch_diagnostic(
                daily_request(), provider_id="choice"
            )
            batch_path = next(Path(directory).rglob(f"{original.batch_id}.json"))
            self.assertEqual(
                registry.storage.read_for_diagnostics(batch_path).batch_id,  # type: ignore[union-attr]
                original.batch_id,
            )
            with self.assertRaisesRegex(
                MarketDataStorageError,
                "not locally admitted for research consumption",
            ):
                registry.storage.read_for_research(batch_path)  # type: ignore[union-attr]

            replay_request = MarketDataRequest(
                dataset_type="daily_bar",
                instrument_id="000333.SZ",
                start_date=date(2026, 8, 3),
                end_date=date(2026, 8, 7),
                adjustment="qfq",
                retrieval_mode="offline_replay",
                requested_at=NOW + timedelta(hours=1),
                evidence_cutoff_at=NOW,
            )
            with patch.object(
                registry.provider("choice"),
                "fetch",
                side_effect=AssertionError("offline replay must not call Choice"),
            ):
                replay = registry.fetch_diagnostic(
                    replay_request, provider_id="choice"
                )
                with self.assertRaisesRegex(ProviderError, "research consumption"):
                    registry.fetch(replay_request, provider_id="choice")
            self.assertEqual(replay.normalized_content_sha256, original.normalized_content_sha256)
            self.assertEqual(replay.retrieval_mode, "offline_replay")

            raw_path = next(Path(directory).rglob(f"{original.batch_id}.raw"))
            raw_path.write_bytes(raw_path.read_bytes() + b" ")
            with self.assertRaisesRegex(MarketDataStorageError, "raw evidence hash mismatch"):
                registry.storage.read_for_diagnostics(batch_path)  # type: ignore[union-attr]

    def test_secondary_label_cannot_be_rewritten_to_unlock_formal_read(self):
        client = FakeChoiceClient()
        provider = ChoiceProvider(
            sdk_loader=lambda: SimpleNamespace(c=client), clock=lambda: NOW
        )
        with tempfile.TemporaryDirectory() as directory:
            storage = MarketDataStorage(
                Path(directory),
                admission_config=self.config,
                allow_test_receipts=True,
            )
            registry = MarketDataRegistry(
                self.config,
                storage=storage,
                providers=(provider,),
            )
            batch = registry.fetch_diagnostic(
                daily_request(), provider_id="choice"
            )
            batch_path = next(Path(directory).rglob(f"{batch.batch_id}.json"))
            payload = json.loads(batch_path.read_text(encoding="utf-8"))
            payload["admission_status"] = "validated_research_only"
            batch_raw = canonical_json_bytes(payload)
            batch_path.write_bytes(batch_raw)
            receipt_path = storage.validated_receipt_path(batch_path)
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["batch_file_sha256"] = sha256_bytes(batch_raw)
            receipt_path.write_bytes(canonical_json_bytes(receipt))
            with self.assertRaisesRegex(
                MarketDataStorageError,
                "admission metadata differs from current local policy",
            ):
                storage.read_for_research(batch_path)

    def test_diagnostic_fetch_requires_explicit_choice_provider(self):
        client = FakeChoiceClient()
        with tempfile.TemporaryDirectory() as directory:
            registry = self.registry(
                ChoiceProvider(
                    sdk_loader=lambda: SimpleNamespace(c=client), clock=lambda: NOW
                ),
                Path(directory),
            )
            with self.assertRaisesRegex(
                UnsupportedDatasetError, "provider_id='choice'"
            ):
                registry.fetch_diagnostic(daily_request(), provider_id="baostock")

    def test_sdk_missing_is_dependency_missing(self):
        missing = ModuleNotFoundError("No module named 'EmQuantAPI'", name="EmQuantAPI")
        with patch(
            "research.market_data.providers.choice.importlib.import_module",
            side_effect=missing,
        ):
            with self.assertRaises(DependencyMissingError):
                ChoiceProvider().fetch(daily_request())

    def test_noncanonical_instrument_is_rejected_before_sdk_load(self):
        request = MarketDataRequest(
            dataset_type="daily_bar",
            instrument_id="000333",
            start_date=date(2026, 8, 3),
            end_date=date(2026, 8, 7),
            adjustment="none",
            retrieval_mode="historical_backfill",
            requested_at=NOW,
        )
        with self.assertRaisesRegex(ProviderQueryError, "canonical instrument"):
            ChoiceProvider(
                sdk_loader=lambda: (_ for _ in ()).throw(
                    AssertionError("SDK must not load for a noncanonical code")
                )
            ).fetch(request)

    def test_non_a_share_instruments_are_rejected_before_sdk_load(self):
        for instrument in (
            "900901.SH",
            "200002.SZ",
            "510300.SH",
            "159919.SZ",
            "113001.SH",
            "123001.SZ",
            "000905.SH",
        ):
            with self.subTest(instrument=instrument):
                request = MarketDataRequest(
                    dataset_type="daily_bar",
                    instrument_id=instrument,
                    start_date=date(2026, 8, 3),
                    end_date=date(2026, 8, 7),
                    adjustment="none",
                    retrieval_mode="historical_backfill",
                    requested_at=NOW,
                )
                with self.assertRaisesRegex(ProviderQueryError, "A-share"):
                    ChoiceProvider(
                        sdk_loader=lambda: (_ for _ in ()).throw(
                            AssertionError("SDK must not load for a non-A-share code")
                        )
                    ).fetch(request)

    def test_stock_requires_qfq_without_unadjusted_fallback(self):
        for adjustment in ("none", "hfq"):
            request = MarketDataRequest(
                dataset_type="daily_bar",
                instrument_id="000333.SZ",
                start_date=date(2026, 8, 3),
                end_date=date(2026, 8, 7),
                adjustment=adjustment,
                retrieval_mode="historical_backfill",
                requested_at=NOW,
            )
            with self.subTest(adjustment=adjustment), self.assertRaisesRegex(
                UnsupportedDatasetError, "require qfq"
            ):
                ChoiceProvider(
                    sdk_loader=lambda: (_ for _ in ()).throw(
                        AssertionError("SDK must not load for a forbidden adjustment")
                    )
                ).fetch(request)

    def test_whitelisted_csi300_index_requires_none(self):
        client = FakeChoiceClient(daily=daily_result(code="000300.SH"))
        batch = ChoiceProvider(
            sdk_loader=lambda: SimpleNamespace(c=client), clock=lambda: NOW
        ).fetch(index_request())
        self.assertEqual(batch.records[0]["instrument_id"], "000300.SH")
        self.assertEqual(batch.records[0]["adjustment"], "none")
        self.assertIn("AdjustFlag=1", client.csd_calls[0][4])
        self.assertIn("Market=CNSESH", client.tradedates_calls[0][2])
        with self.assertRaisesRegex(UnsupportedDatasetError, "requires adjustment=none"):
            ChoiceProvider(
                sdk_loader=lambda: (_ for _ in ()).throw(
                    AssertionError("SDK must not load for adjusted index data")
                )
            ).fetch(index_request(adjustment="qfq"))

    def test_unactivated_login_is_not_configured_and_does_not_query(self):
        client = FakeChoiceClient(
            login=result(10001014, "need use LoginActivator.exe tool")
        )
        provider = ChoiceProvider(sdk_loader=lambda: SimpleNamespace(c=client))
        with self.assertRaises(ProviderNotConfiguredError):
            provider.fetch(daily_request())
        self.assertEqual(client.csd_calls, [])
        self.assertEqual(client.tradedates_calls, [])
        self.assertEqual(client.stop_calls, 1)

    def test_official_entitlement_codes_are_not_configured_independent_of_text(self):
        cases = (
            (10001003, "User has no API access"),
            (10001004, "User API access expired"),
            (10001005, "Failed to get user info"),
            (10001012, "insufficient user access"),
            (10001019, "Different activated device"),
            (10001020, "Local activation record expired"),
        )
        for code, message in cases:
            with self.subTest(code=code):
                client = FakeChoiceClient(login=result(code, message))
                with self.assertRaises(ProviderNotConfiguredError):
                    ChoiceProvider(
                        sdk_loader=lambda: SimpleNamespace(c=client)
                    ).fetch(daily_request())
                self.assertEqual(client.stop_calls, 1)

    def test_official_dependency_and_network_codes_are_structured(self):
        cases = (
            (10001006, "DLL version no longer supported", DependencyMissingError),
            (10002003, "localized message", NetworkBlockedError),
            (10000015, "localized message", NetworkBlockedError),
        )
        for code, message, expected in cases:
            with self.subTest(code=code):
                client = FakeChoiceClient(login=result(code, message))
                with self.assertRaises(expected):
                    ChoiceProvider(
                        sdk_loader=lambda: SimpleNamespace(c=client)
                    ).fetch(daily_request())
                self.assertEqual(client.stop_calls, 1)

    def test_data_limit_exceeded_is_a_distinct_quota_failure(self):
        client = FakeChoiceClient(
            daily=result(10001029, "data limit exceeded")
        )
        with self.assertRaises(ProviderQuotaExceededError) as caught:
            ChoiceProvider(
                sdk_loader=lambda: SimpleNamespace(c=client)
            ).fetch(daily_request())
        self.assertEqual(caught.exception.status, "failed")
        self.assertEqual(caught.exception.code, "quota_exhausted")
        self.assertEqual(client.stop_calls, 1)

    def test_network_login_error_is_network_blocked(self):
        client = FakeChoiceClient(login=result(1001, "network connection timed out"))
        provider = ChoiceProvider(sdk_loader=lambda: SimpleNamespace(c=client))
        with self.assertRaises(NetworkBlockedError):
            provider.fetch(daily_request())

    def test_query_error_empty_and_contract_drift_fail_closed_and_stop(self):
        cases = (
            (result(2001, "indicator invalid"), ProviderQueryError),
            (daily_result(dates=[]), IncompleteDatasetError),
            (daily_result(code="600519.SH"), ProviderQueryError),
            (daily_result(indicators=["OPEN"]), ProviderQueryError),
        )
        for daily, expected in cases:
            with self.subTest(expected=expected, daily=daily):
                client = FakeChoiceClient(daily=daily)
                provider = ChoiceProvider(sdk_loader=lambda: SimpleNamespace(c=client))
                with self.assertRaises(expected):
                    provider.fetch(daily_request())
                self.assertEqual(client.stop_calls, 1)

    def test_calendar_completeness_is_fail_closed(self):
        cases = (
            (
                daily_result(dates=[]),
                calendar_result(dates=[]),
                NoTradingDaysError,
            ),
            (
                daily_result(),
                calendar_result(dates=["2026/8/3"]),
                IncompleteDatasetError,
            ),
            (
                daily_result(),
                calendar_result(dates=["2026/8/4", "2026/8/3"]),
                ProviderQueryError,
            ),
            (
                daily_result(),
                calendar_result(dates=[]),
                IncompleteDatasetError,
            ),
        )
        for daily, calendar, expected in cases:
            with self.subTest(expected=expected):
                client = FakeChoiceClient(daily=daily, calendar=calendar)
                provider = ChoiceProvider(sdk_loader=lambda: SimpleNamespace(c=client))
                with self.assertRaises(expected):
                    provider.fetch(daily_request())
                self.assertEqual(client.stop_calls, 1)

    def test_suspension_like_gap_is_excluded_when_status_cannot_be_proven(self):
        one_day_columns = [
            ["50.00"],
            ["52.00"],
            ["49.00"],
            ["51.00"],
            ["49.80"],
            ["100000"],
            ["5100000"],
        ]
        client = FakeChoiceClient(
            daily=daily_result(
                dates=["2026-08-03"], columns=one_day_columns
            ),
            calendar=calendar_result(
                dates=["2026/8/3", "2026/8/4"]
            ),
        )
        with self.assertRaisesRegex(
            IncompleteDatasetError,
            "suspension cannot be distinguished",
        ):
            ChoiceProvider(
                sdk_loader=lambda: SimpleNamespace(c=client), clock=lambda: NOW
            ).fetch(daily_request())
        self.assertEqual(client.stop_calls, 1)

    def test_daily_dates_must_be_unique_and_ascending(self):
        for dates in (
            ["2026-08-04", "2026-08-03"],
            ["2026-08-03", "2026-08-03"],
        ):
            with self.subTest(dates=dates):
                client = FakeChoiceClient(daily=daily_result(dates=dates))
                provider = ChoiceProvider(sdk_loader=lambda: SimpleNamespace(c=client))
                with self.assertRaisesRegex(ProviderQueryError, "strictly ascending"):
                    provider.fetch(daily_request())
                self.assertEqual(client.stop_calls, 1)

    def test_sdk_error_redacts_choice_activation_identifiers(self):
        synthetic_phone = "138" + "0000" + "0000"
        synthetic_verification_code = "123" + "456"
        synthetic_identity_number = "110105" + "19491231" + "002X"
        message = (
            f"PhoneNumber={synthetic_phone} "
            f"VerificationCode={synthetic_verification_code} "
            "account=demo-account UserName=demo-user userInfo=demo-token "
            f"identity_number={synthetic_identity_number}"
        )
        redacted = safe_error_text(message)
        for secret in (
            synthetic_phone,
            synthetic_verification_code,
            "demo-account",
            "demo-user",
            "demo-token",
            synthetic_identity_number,
        ):
            self.assertNotIn(secret, redacted)
        client = FakeChoiceClient(login=result(10001014, message))
        with self.assertRaises(ProviderNotConfiguredError) as raised:
            ChoiceProvider(sdk_loader=lambda: SimpleNamespace(c=client)).fetch(
                daily_request()
            )
        for secret in (
            synthetic_phone,
            synthetic_verification_code,
            "demo-account",
            "demo-user",
            "demo-token",
        ):
            self.assertNotIn(secret, str(raised.exception))
        with tempfile.TemporaryDirectory() as directory:
            quarantined_client = FakeChoiceClient(
                login=result(10001014, message)
            )
            provider = ChoiceProvider(
                sdk_loader=lambda: SimpleNamespace(c=quarantined_client)
            )
            with self.assertRaises(ProviderNotConfiguredError):
                self.registry(provider, Path(directory)).fetch(
                    daily_request(), provider_id="choice"
                )
            persisted = b"\n".join(
                path.read_bytes()
                for path in Path(directory).rglob("*")
                if path.is_file()
            ).decode("utf-8")
            for secret in (
                synthetic_phone,
                synthetic_verification_code,
                "demo-account",
                "demo-user",
                "demo-token",
                synthetic_identity_number,
            ):
                self.assertNotIn(secret, persisted)

    def test_stop_error_is_not_reported_as_success(self):
        client = FakeChoiceClient(stopped=result(2001, "stop failed"))
        provider = ChoiceProvider(
            sdk_loader=lambda: SimpleNamespace(c=client), clock=lambda: NOW
        )
        with self.assertRaisesRegex(ProviderQueryError, "Choice stop failed"):
            provider.fetch(daily_request())

    def test_invalid_ohlc_is_quarantined_by_registry(self):
        malformed = daily_result()
        malformed.Data["000333.SZ"][1][0] = "48.00"
        client = FakeChoiceClient(daily=malformed)
        provider = ChoiceProvider(
            sdk_loader=lambda: SimpleNamespace(c=client), clock=lambda: NOW
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(BatchValidationError):
                self.registry(provider, Path(directory)).fetch(
                    daily_request(), provider_id="choice"
                )

    def test_choice_policy_is_explicit_exception_not_a_legacy_bypass(self):
        self.assertEqual(
            set(self.config["providers"]["choice"]["allowed_sdk_functions"]),
            ChoiceProvider._ALLOWED_SDK_METHODS,
        )
        with self.assertRaisesRegex(ProviderQueryError, "read-only allowlist"):
            ChoiceProvider._sdk_call(FakeChoiceClient(), "porder")
        decision = evaluate_admission(
            daily_request(),
            provider_id="choice",
            upstream_source="choice.eastmoney_emquantapi.csd_with_tradedates",
            synthetic=False,
            config=self.config,
        )
        self.assertEqual(decision.admission_status, "validated_secondary_not_primary")
        issue_codes = {str(issue.get("code")) for issue in decision.issues}
        self.assertIn("licensed_choice_source_not_official_truth", issue_codes)

        spoofed = copy.deepcopy(self.config)
        spoofed["providers"]["tushare"]["allowed_upstream_sources"]["daily_bar"] = [
            "eastmoney.public.spoof"
        ]
        legacy_decision = evaluate_admission(
            daily_request(),
            provider_id="tushare",
            upstream_source="eastmoney.public.spoof",
            synthetic=False,
            config=spoofed,
        )
        self.assertEqual(legacy_decision.admission_status, "diagnostic_only")

    def test_configured_registry_keeps_baostock_default(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = MarketDataRegistry.configured(storage_root=Path(directory))
        self.assertEqual(registry.provider().provider_id, "baostock")
        self.assertEqual(registry.provider("choice").provider_id, "choice")


if __name__ == "__main__":
    unittest.main()
