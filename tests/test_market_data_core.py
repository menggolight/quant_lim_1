import copy
import hashlib
import importlib
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from research.market_data.admission import evaluate_admission
from research.market_data.contracts import MarketDataContractError, MarketDataRequest
from research.market_data.providers.akshare import AKShareProvider
from research.market_data.providers.baostock import (
    BaoStockProvider,
    from_baostock_code,
    normalize_a_share_stock_instrument,
    normalize_baostock_instrument,
    to_baostock_code,
)
from research.market_data.providers.base import (
    AllProvidersFailedError,
    BatchValidationError,
    DependencyMissingError,
    EmptyDatasetError,
    IncompleteDatasetError,
    NoTradingDaysError,
    ProviderDisabledError,
    ProviderNotConfiguredError,
    ProviderError,
    ProviderPayload,
    ProviderQueryError,
    UnknownProviderError,
    redact_sensitive_value,
    safe_error_text,
)
from research.market_data.providers.tushare import TushareProvider
from research.market_data.registry import (
    MarketDataRegistry,
    RegistryConfigurationError,
    load_market_data_config,
)
from research.market_data.storage import MarketDataStorage, MarketDataStorageError


ROOT = Path(__file__).resolve().parents[1]
TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 8, 6, 10, 0, tzinfo=TZ)


class FakeResult:
    def __init__(self, fields=(), rows=(), *, error_code="0", error_msg=""):
        self.fields = list(fields)
        self.rows = [list(row) for row in rows]
        self.error_code = error_code
        self.error_msg = error_msg
        self._index = -1

    def next(self):
        self._index += 1
        return self._index < len(self.rows)

    def get_row_data(self):
        return self.rows[self._index]


class FakeBaoStock:
    def __init__(self, *, login=None, daily=None, calendar=None, security=None):
        self.login_result = login or FakeResult(error_code="0")
        self.daily_result = daily or FakeResult()
        self.calendar_result = calendar or FakeResult(
            ["calendar_date", "is_trading_day"],
            [
                ["2026-08-01", "0"],
                ["2026-08-02", "0"],
                ["2026-08-03", "0"],
                ["2026-08-04", "0"],
                ["2026-08-05", "1"],
            ],
        )
        self.security_result = security or FakeResult()
        self.logout_calls = 0
        self.daily_calls = 0

    def login(self):
        return self.login_result

    def logout(self):
        self.logout_calls += 1
        return FakeResult(error_code="0")

    def query_history_k_data_plus(self, *args, **kwargs):
        self.daily_calls += 1
        return self.daily_result

    def query_trade_dates(self, *args, **kwargs):
        return self.calendar_result

    def query_stock_basic(self, *args, **kwargs):
        return self.security_result


class StaticProvider:
    def __init__(self, provider_id, adapter_version, payload=None, error=None):
        self.provider_id = provider_id
        self.adapter_version = adapter_version
        self.upstream_source = f"{provider_id}.test"
        self.supported_datasets = frozenset({"daily_bar"})
        self.payload = payload
        self.error = error
        self.calls = 0

    def fetch(self, request):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.payload


def daily_fields():
    return [
        "date",
        "code",
        "open",
        "high",
        "low",
        "close",
        "preclose",
        "volume",
        "amount",
        "adjustflag",
        "tradestatus",
    ]


def daily_row(day="2026-08-05", code="sz.000333", **changes):
    values = {
        "date": day,
        "code": code,
        "open": "50.00",
        "high": "52.00",
        "low": "49.50",
        "close": "51.00",
        "preclose": "49.80",
        "volume": "100000",
        "amount": "5100000",
        "adjustflag": "3",
        "tradestatus": "1",
    }
    values.update(changes)
    return [values[field] for field in daily_fields()]


def daily_request(mode="historical_backfill"):
    return MarketDataRequest(
        dataset_type="daily_bar",
        instrument_id="000333.SZ",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 5),
        adjustment="none",
        retrieval_mode=mode,
        requested_at=NOW,
    )


def normalized_record(day="2026-08-05", **changes):
    row = {
        "instrument_id": "000333.SZ",
        "trading_date": day,
        "open": "50",
        "high": "52",
        "low": "49.5",
        "close": "51",
        "preclose": "49.8",
        "volume": "100000",
        "amount": "5100000",
        "currency": "CNY",
        "adjustment": "none",
        "trading_status": "traded",
        "available_at": f"{day}T15:30:00+08:00",
        "availability_status": "policy_estimated",
        "source_record_id": f"source-{day}",
    }
    row.update(changes)
    return row


def static_payload(
    records,
    raw=b'{"fixture":true}\n',
    upstream="baostock.query_history_k_data_plus",
):
    return ProviderPayload(
        raw_content=raw,
        records=tuple(records),
        fetched_at=NOW,
        upstream_source=upstream,
    )


class MarketDataCoreTest(unittest.TestCase):
    def setUp(self):
        self.config = load_market_data_config(ROOT / "configs" / "market_data.v1.json")

    def registry(self, provider, root):
        return MarketDataRegistry(
            self.config,
            storage=MarketDataStorage(root, allow_test_receipts=True),
            providers=(provider,),
        )

    def test_baostock_code_mapping_is_strict_and_bidirectional(self):
        self.assertEqual(to_baostock_code("000333.SZ"), "sz.000333")
        self.assertEqual(to_baostock_code("600519.SH"), "sh.600519")
        self.assertEqual(to_baostock_code("000300.SH"), "sh.000300")
        self.assertEqual(to_baostock_code("000001.SZ"), "sz.000001")
        self.assertEqual(from_baostock_code("sz.000333"), "000333.SZ")
        with self.assertRaises(ValueError):
            to_baostock_code("600519.SZ")
        with self.assertRaises(ValueError):
            to_baostock_code("000333.SH")
        with self.assertRaises(ValueError):
            to_baostock_code("430001.BJ")

    def test_baostock_instrument_aliases_share_one_cross_market_validator(self):
        self.assertEqual(normalize_baostock_instrument("000333"), "000333.SZ")
        self.assertEqual(normalize_baostock_instrument("600519"), "600519.SH")
        self.assertEqual(normalize_baostock_instrument("SZ000333"), "000333.SZ")
        self.assertEqual(normalize_baostock_instrument("1.600519"), "600519.SH")
        self.assertEqual(normalize_baostock_instrument("000300.SH"), "000300.SH")
        for invalid in ("430001", "000333.SH", "600519.SZ", "000333.BJ", "BK0001"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    normalize_baostock_instrument(invalid)

    def test_a_share_stock_normalization_rejects_other_security_types(self):
        self.assertEqual(normalize_a_share_stock_instrument("000333"), "000333.SZ")
        self.assertEqual(normalize_a_share_stock_instrument("600519"), "600519.SH")
        for invalid in (
            "159919",
            "510300",
            "900901",
            "200002",
            "113001",
            "123001",
            "000300.SH",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    normalize_a_share_stock_instrument(invalid)

    def test_normal_baostock_daily_batch_is_unadjusted_validated_and_hashed(self):
        sdk = FakeBaoStock(daily=FakeResult(daily_fields(), [daily_row()]))
        provider = BaoStockProvider(sdk_loader=lambda: sdk, clock=lambda: NOW)
        with tempfile.TemporaryDirectory() as directory:
            batch = self.registry(provider, directory).fetch(daily_request())
            self.assertEqual(batch.provider_id, "baostock")
            self.assertEqual(batch.upstream_source, "baostock.query_history_k_data_plus")
            self.assertEqual(batch.record_count, 1)
            self.assertEqual(batch.records[0]["adjustment"], "none")
            self.assertEqual(batch.admission_status, "validated_research_only")
            self.assertEqual(batch.point_in_time_status, "historical_backfill_not_original_capture")
            self.assertNotEqual(batch.raw_content_sha256, batch.normalized_content_sha256)
            self.assertEqual(sdk.logout_calls, 1)

    def test_login_and_query_errors_fail_and_logout(self):
        login_sdk = FakeBaoStock(login=FakeResult(error_code="100", error_msg="login failed"))
        with self.assertRaises(ProviderQueryError):
            BaoStockProvider(sdk_loader=lambda: login_sdk).fetch(daily_request())
        self.assertEqual(login_sdk.logout_calls, 1)

        query_sdk = FakeBaoStock(
            daily=FakeResult(daily_fields(), error_code="200", error_msg="query failed")
        )
        with self.assertRaises(ProviderQueryError):
            BaoStockProvider(sdk_loader=lambda: query_sdk).fetch(daily_request())
        self.assertEqual(query_sdk.logout_calls, 1)

    def test_daily_adjustflag_and_tradestatus_are_fail_closed(self):
        cases = (
            ("adjustflag", "2"),
            ("tradestatus", "unexpected"),
        )
        for field, value in cases:
            sdk = FakeBaoStock(
                daily=FakeResult(daily_fields(), [daily_row(**{field: value})])
            )
            provider = BaoStockProvider(sdk_loader=lambda: sdk, clock=lambda: NOW)
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ProviderQueryError) as caught:
                    self.registry(provider, directory).fetch(daily_request())
                self.assertTrue(caught.exception.raw_content)
                self.assertTrue(any((Path(directory) / "quarantine").rglob("*.json")))
                self.assertFalse(any((Path(directory) / "validated").rglob("*.json")))

    def test_empty_result_is_not_passed(self):
        sdk = FakeBaoStock(daily=FakeResult(daily_fields(), []))
        provider = BaoStockProvider(sdk_loader=lambda: sdk, clock=lambda: NOW)
        with tempfile.TemporaryDirectory() as directory:
            registry = self.registry(provider, directory)
            with self.assertRaises(IncompleteDatasetError):
                registry.fetch(daily_request())
            self.assertTrue(any((Path(directory) / "quarantine").rglob("*.json")))

    def test_empty_daily_distinguishes_no_exchange_trading_days(self):
        sdk = FakeBaoStock(
            daily=FakeResult(daily_fields(), []),
            calendar=FakeResult(
                ["calendar_date", "is_trading_day"],
                [["2026-08-01", "0"], ["2026-08-02", "0"]],
            ),
        )
        request = MarketDataRequest(
            dataset_type="daily_bar",
            instrument_id="000333.SZ",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 2),
            adjustment="none",
            retrieval_mode="historical_backfill",
            requested_at=NOW,
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(NoTradingDaysError):
                self.registry(
                    BaoStockProvider(sdk_loader=lambda: sdk, clock=lambda: NOW),
                    directory,
                ).fetch(request)

    def test_daily_subset_cannot_claim_complete_against_trade_calendar(self):
        sdk = FakeBaoStock(
            daily=FakeResult(daily_fields(), [daily_row()]),
            calendar=FakeResult(
                ["calendar_date", "is_trading_day"],
                [
                    ["2026-08-01", "0"],
                    ["2026-08-02", "0"],
                    ["2026-08-03", "0"],
                    ["2026-08-04", "1"],
                    ["2026-08-05", "1"],
                ],
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(IncompleteDatasetError, "missing=.*2026-08-04"):
                self.registry(
                    BaoStockProvider(sdk_loader=lambda: sdk, clock=lambda: NOW),
                    directory,
                ).fetch(daily_request())
            self.assertFalse(any((Path(directory) / "validated").rglob("*.json")))

    def test_duplicate_date_and_wrong_security_are_quarantined(self):
        duplicate = FakeBaoStock(
            daily=FakeResult(daily_fields(), [daily_row(), daily_row()])
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(BatchValidationError):
                self.registry(
                    BaoStockProvider(sdk_loader=lambda: duplicate, clock=lambda: NOW),
                    directory,
                ).fetch(daily_request())
            quarantine = next((Path(directory) / "quarantine").rglob("*.json"))
            with self.assertRaises(MarketDataStorageError):
                MarketDataStorage(directory).read_for_research(quarantine)

        wrong = FakeBaoStock(
            daily=FakeResult(daily_fields(), [daily_row(code="sh.600519")])
        )
        with self.assertRaises(ProviderQueryError):
            BaoStockProvider(sdk_loader=lambda: wrong, clock=lambda: NOW).fetch(daily_request())

    def test_invalid_ohlc_negative_amount_missing_field_and_invalid_number_fail(self):
        cases = (
            normalized_record(high="49"),
            normalized_record(amount="-1"),
            {key: value for key, value in normalized_record().items() if key != "preclose"},
            normalized_record(volume="NaN"),
        )
        for index, record in enumerate(cases):
            provider = StaticProvider(
                "baostock",
                "baostock-adapter-v1",
                payload=static_payload([record], raw=f"case-{index}".encode()),
            )
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(BatchValidationError):
                    self.registry(provider, directory).fetch(daily_request())
                self.assertFalse(any((Path(directory) / "validated").rglob("*.json")))

    def test_error_html_cannot_pollute_validated_cache(self):
        provider = StaticProvider(
            "baostock",
            "baostock-adapter-v1",
            payload=static_payload(
                [normalized_record(close="<html>error</html>")],
                raw=b"<html>upstream error</html>",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(BatchValidationError):
                self.registry(provider, directory).fetch(daily_request())
            self.assertTrue(any((Path(directory) / "raw").rglob("*.raw")))
            self.assertFalse(any((Path(directory) / "validated").rglob("*.json")))

    def test_research_read_recomputes_schema_domain_and_admission(self):
        for mutation, message in (
            (
                lambda payload: payload.__setitem__(
                    "admission_status", "admitted_for_research"
                ),
                "admission metadata",
            ),
            (
                lambda payload: payload.__setitem__("unexpected", True),
                "envelope fields",
            ),
            (
                lambda payload: payload["records"][0].__setitem__("high", "49"),
                "normalized hash mismatch",
            ),
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                provider = StaticProvider(
                    "baostock",
                    "baostock-adapter-v1",
                    payload=static_payload([normalized_record()]),
                )
                registry = self.registry(provider, directory)
                registry.fetch(daily_request())
                path = next((Path(directory) / "validated").rglob("*.json"))
                payload = json.loads(path.read_text(encoding="utf-8"))
                mutation(payload)
                path.write_text(json.dumps(payload), encoding="utf-8")
                receipt_path = path.with_suffix(".receipt")
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                receipt["batch_file_sha256"] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
                with self.assertRaisesRegex(MarketDataStorageError, message):
                    MarketDataStorage(
                        directory, allow_test_receipts=True
                    ).read_for_research(path)

    def test_validated_storage_requires_registry_receipt_and_write_path(self):
        raw = b'{"fixture":true}\n'
        provider = StaticProvider(
            "baostock",
            "baostock-adapter-v1",
            payload=static_payload([normalized_record()], raw=raw),
        )
        with tempfile.TemporaryDirectory() as directory:
            storage = MarketDataStorage(directory)
            batch = self.registry(provider, directory).fetch(daily_request())
            with self.assertRaisesRegex(
                MarketDataStorageError, "persisted through MarketDataRegistry"
            ):
                storage.persist_validated(batch, raw)

            path = next((Path(directory) / "validated").rglob("*.json"))
            receipt_path = path.with_suffix(".receipt")
            receipt_path.unlink()
            with self.assertRaisesRegex(MarketDataStorageError, "Registry receipt"):
                MarketDataStorage(
                    directory, allow_test_receipts=True
                ).read_for_research(path)

    def test_atomic_evidence_write_is_concurrency_safe_and_collision_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "nested" / "receipt"
            content = b"same immutable evidence"
            with ThreadPoolExecutor(max_workers=8) as executor:
                list(
                    executor.map(
                        lambda _index: MarketDataStorage._atomic_write(
                            target, content
                        ),
                        range(32),
                    )
                )
            self.assertEqual(target.read_bytes(), content)
            self.assertEqual(list(target.parent.glob(".md-*")), [])
            with self.assertRaisesRegex(
                MarketDataStorageError, "non-identical evidence"
            ):
                MarketDataStorage._atomic_write(target, b"different evidence")

    def test_configured_baostock_reader_replays_raw_before_research_use(self):
        sdk = FakeBaoStock(daily=FakeResult(daily_fields(), [daily_row()]))
        provider = BaoStockProvider(sdk_loader=lambda: sdk, clock=lambda: NOW)
        with tempfile.TemporaryDirectory() as directory, patch(
            "research.market_data.providers.BaoStockProvider",
            return_value=provider,
        ):
            registry = MarketDataRegistry.configured(storage_root=directory)
            batch = registry.fetch(daily_request())
            path = next((Path(directory) / "validated").rglob("*.json"))
            key = path.parent.name
            raw_path = (
                Path(directory)
                / "raw"
                / batch.provider_id
                / batch.dataset_type
                / key
                / f"{batch.batch_id}.raw"
            )
            forged_raw = b"unrelated self-signed content"
            raw_path.write_bytes(forged_raw)

            envelope = json.loads(path.read_text(encoding="utf-8"))
            envelope["raw_content_sha256"] = hashlib.sha256(forged_raw).hexdigest()
            path.write_text(json.dumps(envelope), encoding="utf-8")
            receipt_path = path.with_suffix(".receipt")
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["raw_content_sha256"] = envelope["raw_content_sha256"]
            receipt["batch_file_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

            with self.assertRaisesRegex(MarketDataStorageError, "raw evidence"):
                MarketDataStorage(directory).read_for_research(path)

    def test_configured_baostock_offline_replay_uses_saved_raw_without_sdk_call(self):
        sdk = FakeBaoStock(daily=FakeResult(daily_fields(), [daily_row()]))
        provider = BaoStockProvider(sdk_loader=lambda: sdk, clock=lambda: NOW)
        with tempfile.TemporaryDirectory() as directory, patch(
            "research.market_data.providers.BaoStockProvider",
            return_value=provider,
        ):
            registry = MarketDataRegistry.configured(storage_root=directory)
            original = registry.fetch(daily_request("historical_backfill"))
            replay_request = MarketDataRequest(
                dataset_type="daily_bar",
                instrument_id="000333.SZ",
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 5),
                retrieval_mode="offline_replay",
                requested_at=NOW + timedelta(minutes=1),
            )
            replay = registry.fetch(replay_request)
            later_replay = registry.fetch(
                MarketDataRequest(
                    dataset_type="daily_bar",
                    instrument_id="000333.SZ",
                    start_date=date(2026, 8, 1),
                    end_date=date(2026, 8, 5),
                    retrieval_mode="offline_replay",
                    requested_at=NOW + timedelta(minutes=2),
                )
            )
        self.assertEqual(sdk.daily_calls, 1)
        self.assertEqual(replay.batch_id, later_replay.batch_id)
        self.assertEqual(replay.raw_content_sha256, original.raw_content_sha256)
        self.assertEqual(
            replay.normalized_content_sha256,
            original.normalized_content_sha256,
        )
        self.assertEqual(
            replay.point_in_time_status,
            "historical_backfill_not_original_capture",
        )

    def test_offline_evidence_cutoff_selects_latest_eligible_capture(self):
        provider = StaticProvider(
            "baostock",
            "baostock-adapter-v1",
            payload=static_payload([normalized_record()], raw=b"older"),
        )
        with tempfile.TemporaryDirectory() as directory:
            registry = self.registry(provider, directory)
            older = registry.fetch(daily_request("historical_backfill"))
            provider.payload = ProviderPayload(
                raw_content=b"newer",
                records=(normalized_record(close="52"),),
                fetched_at=NOW + timedelta(days=2),
                upstream_source="baostock.query_history_k_data_plus",
            )
            newer = registry.fetch(daily_request("historical_backfill"))
            replay = registry.fetch(
                MarketDataRequest(
                    dataset_type="daily_bar",
                    instrument_id="000333.SZ",
                    start_date=date(2026, 8, 1),
                    end_date=date(2026, 8, 5),
                    retrieval_mode="offline_replay",
                    requested_at=NOW + timedelta(days=3),
                    evidence_cutoff_at=NOW + timedelta(days=1),
                )
            )
        self.assertNotEqual(older.batch_id, newer.batch_id)
        self.assertEqual(replay.raw_content_sha256, older.raw_content_sha256)
        self.assertEqual(
            replay.normalized_content_sha256,
            older.normalized_content_sha256,
        )
        self.assertEqual(provider.calls, 2)

    def test_malformed_provider_payload_is_structured_and_quarantined(self):
        provider = StaticProvider(
            "baostock",
            "baostock-adapter-v1",
            payload={"records": []},
        )
        with tempfile.TemporaryDirectory() as directory:
            registry = self.registry(provider, directory)
            with self.assertRaisesRegex(BatchValidationError, "ProviderPayload") as caught:
                registry.fetch(daily_request())
            self.assertTrue(hasattr(caught.exception, "quarantine"))

    def test_quarantine_write_failure_does_not_replace_provider_status(self):
        provider = StaticProvider(
            "baostock",
            "baostock-adapter-v1",
            error=DependencyMissingError("missing SDK"),
        )
        with tempfile.TemporaryDirectory() as directory:
            storage = MarketDataStorage(directory, allow_test_receipts=True)
            registry = MarketDataRegistry(
                self.config,
                storage=storage,
                providers=(provider,),
            )
            with patch.object(
                storage,
                "persist_quarantine",
                side_effect=OSError("disk unavailable"),
            ), self.assertRaises(DependencyMissingError) as caught:
                registry.fetch(daily_request())
        self.assertEqual(caught.exception.status, "dependency_missing")
        self.assertIn("disk unavailable", caught.exception.quarantine_error)

    def test_allow_test_receipts_requires_a_real_boolean(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(MarketDataStorageError, "must be a boolean"):
                MarketDataStorage(directory, allow_test_receipts="false")  # type: ignore[arg-type]

    def test_trade_calendar_and_security_master(self):
        calendar_sdk = FakeBaoStock(
            calendar=FakeResult(
                ["calendar_date", "is_trading_day"],
                [["2026-08-01", "0"], ["2026-08-02", "0"], ["2026-08-03", "1"]],
            )
        )
        calendar_request = MarketDataRequest(
            dataset_type="trade_calendar",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 3),
            retrieval_mode="historical_backfill",
            requested_at=NOW,
        )
        with tempfile.TemporaryDirectory() as directory:
            batch = self.registry(
                BaoStockProvider(sdk_loader=lambda: calendar_sdk, clock=lambda: NOW),
                directory,
            ).fetch(calendar_request)
            self.assertEqual(batch.record_count, 3)
            self.assertTrue(batch.records[-1]["is_trading_day"])

        security_sdk = FakeBaoStock(
            security=FakeResult(
                ["code", "code_name", "ipoDate", "outDate", "type", "status"],
                [["sz.000333", "美的集团", "2013-09-18", "", "1", "1"]],
            )
        )
        security_request = MarketDataRequest(
            dataset_type="security_master",
            instrument_id="000333.SZ",
            retrieval_mode="live_capture",
            requested_at=NOW,
        )
        with tempfile.TemporaryDirectory() as directory:
            batch = self.registry(
                BaoStockProvider(sdk_loader=lambda: security_sdk, clock=lambda: NOW),
                directory,
            ).fetch(security_request)
            self.assertEqual(batch.records[0]["security_name"], "美的集团")
            self.assertEqual(batch.point_in_time_status, "current_snapshot_not_pit")

    def test_trade_calendar_requires_each_natural_date_exactly_once(self):
        cases = (
            [
                ["2026-08-01", "0"],
                ["2026-08-03", "1"],
            ],
            [
                ["2026-08-01", "0"],
                ["2026-08-02", "0"],
                ["2026-08-02", "0"],
                ["2026-08-03", "1"],
            ],
            [
                ["2026-08-01", "0"],
                ["2026-08-02", "0"],
                ["2026-08-03", "1"],
                ["2026-08-04", "1"],
            ],
        )
        request = MarketDataRequest(
            dataset_type="trade_calendar",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 3),
            retrieval_mode="historical_backfill",
            requested_at=NOW,
        )
        for rows in cases:
            sdk = FakeBaoStock(
                calendar=FakeResult(["calendar_date", "is_trading_day"], rows)
            )
            provider = BaoStockProvider(sdk_loader=lambda: sdk, clock=lambda: NOW)
            with self.subTest(rows=rows), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ProviderQueryError) as caught:
                    self.registry(provider, directory).fetch(request)
                self.assertIn("incomplete", str(caught.exception))
                self.assertFalse(any((Path(directory) / "validated").rglob("*.json")))

    def test_sdk_missing_is_lazy_and_explicit(self):
        importlib.import_module("research.market_data")
        provider = BaoStockProvider()
        missing = ModuleNotFoundError("No module named 'baostock'", name="baostock")
        with patch(
            "research.market_data.providers.baostock.importlib.import_module",
            side_effect=missing,
        ):
            with self.assertRaises(DependencyMissingError):
                provider.fetch(daily_request())

    def test_tushare_token_missing_does_not_break_baostock(self):
        tushare = TushareProvider(environ={})
        sdk = FakeBaoStock(daily=FakeResult(daily_fields(), [daily_row()]))
        baostock = BaoStockProvider(sdk_loader=lambda: sdk, clock=lambda: NOW)
        with tempfile.TemporaryDirectory() as directory:
            registry = MarketDataRegistry(
                self.config,
                storage=MarketDataStorage(directory),
                providers=(tushare, baostock),
            )
            with self.assertRaises(ProviderNotConfiguredError):
                registry.fetch(daily_request(), provider_id="tushare")
            self.assertEqual(registry.fetch(daily_request()).provider_id, "baostock")

    def test_unknown_and_disabled_providers_fail_closed(self):
        registry = MarketDataRegistry(self.config, providers=(AKShareProvider(),))
        with self.assertRaises(UnknownProviderError):
            registry.provider("made_up")
        with self.assertRaises(ProviderDisabledError):
            registry.provider("akshare")

    def test_provider_id_cannot_bypass_dataset_and_upstream_policy(self):
        impersonator = StaticProvider(
            "baostock",
            "baostock-adapter-v1",
            payload=static_payload(
                [normalized_record()], upstream="attacker.baostock.daily"
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(BatchValidationError, "unexpected_upstream_source"):
                self.registry(impersonator, directory).fetch(daily_request())
            self.assertFalse(any((Path(directory) / "validated").rglob("*.json")))

        industry_request = MarketDataRequest(
            dataset_type="industry_classification",
            retrieval_mode="live_capture",
            requested_at=NOW,
        )
        admission = evaluate_admission(
            industry_request,
            provider_id="baostock",
            upstream_source="baostock.query_stock_industry",
            synthetic=False,
            config=self.config,
        )
        self.assertEqual(
            admission.admission_status, "rejected_provider_dataset_undeclared"
        )

    def test_akshare_eastmoney_declaration_cannot_be_admitted(self):
        with self.assertRaises(ValueError):
            AKShareProvider.validate_dataset_declaration(
                api_name="stock_zh_a_hist_em",
                upstream_source="Eastmoney",
                admission_status="admitted_for_research",
            )
        AKShareProvider.validate_dataset_declaration(
            api_name="stock_zh_a_hist_em",
            upstream_source="Eastmoney",
            admission_status="diagnostic_only",
        )

    def test_whole_batch_fallback_never_mixes_partial_primary(self):
        primary = StaticProvider(
            "baostock",
            "baostock-adapter-v1",
            payload=static_payload(
                [normalized_record("2026-08-04"), normalized_record("2026-08-04")],
                raw=b"partial-primary",
            ),
        )
        secondary = StaticProvider(
            "tushare",
            "tushare-adapter-v1",
            payload=static_payload([normalized_record()], raw=b"complete-secondary", upstream="tushare.pro.daily"),
        )
        with tempfile.TemporaryDirectory() as directory:
            registry = MarketDataRegistry(
                self.config,
                storage=MarketDataStorage(directory),
                providers=(primary, secondary),
            )
            batch = registry.fetch_with_fallback(
                daily_request(), provider_ids=("baostock", "tushare")
            )
            self.assertEqual(batch.provider_id, "tushare")
            self.assertEqual(batch.record_count, 1)
            self.assertNotIn("2026-08-04", {item["trading_date"] for item in batch.records})

    def test_all_sources_failed_returns_structured_failure_without_defaults(self):
        primary = StaticProvider(
            "baostock",
            "baostock-adapter-v1",
            error=ProviderQueryError("primary failed"),
        )
        secondary = StaticProvider(
            "tushare",
            "tushare-adapter-v1",
            error=ProviderNotConfiguredError("token missing"),
        )
        registry = MarketDataRegistry(self.config, providers=(primary, secondary))
        with self.assertRaises(AllProvidersFailedError) as caught:
            registry.fetch_with_fallback(
                daily_request(), provider_ids=("baostock", "tushare")
            )
        self.assertEqual(len(caught.exception.attempts), 2)
        self.assertEqual(caught.exception.attempts[1]["status"], "not_configured")

    def test_raw_and_normalized_hashes_bind_content(self):
        first = StaticProvider(
            "baostock",
            "baostock-adapter-v1",
            payload=static_payload([normalized_record()], raw=b"raw-one"),
        )
        second = StaticProvider(
            "baostock",
            "baostock-adapter-v1",
            payload=static_payload([normalized_record(close="51.1")], raw=b"raw-two"),
        )
        with tempfile.TemporaryDirectory() as directory:
            left = self.registry(first, directory).fetch(daily_request())
            right = self.registry(second, directory).fetch(daily_request())
        self.assertNotEqual(left.raw_content_sha256, right.raw_content_sha256)
        self.assertNotEqual(left.normalized_content_sha256, right.normalized_content_sha256)

    def test_historical_backfill_and_offline_replay_are_not_conflated(self):
        provider = StaticProvider(
            "baostock",
            "baostock-adapter-v1",
            payload=static_payload([normalized_record()]),
        )
        with tempfile.TemporaryDirectory() as directory:
            registry = self.registry(provider, directory)
            original = registry.fetch(daily_request("historical_backfill"))
            replay = registry.fetch(daily_request("offline_replay"))
            self.assertEqual(original.normalized_content_sha256, replay.normalized_content_sha256)
            self.assertEqual(replay.retrieval_mode, "offline_replay")
            self.assertEqual(
                replay.point_in_time_status,
                "historical_backfill_not_original_capture",
            )
            lineage = [
                issue for issue in replay.issues if issue.get("code") == "offline_replay"
            ]
            self.assertEqual(len(lineage), 1)
            self.assertEqual(
                lineage[0]["details"]["source_batch_id"], original.batch_id
            )
            self.assertEqual(
                lineage[0]["details"]["source_point_in_time_status"],
                "historical_backfill_not_original_capture",
            )
            self.assertEqual(replay.requested_at, original.requested_at)
            later_request = MarketDataRequest(
                dataset_type="daily_bar",
                instrument_id="000333.SZ",
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 5),
                retrieval_mode="offline_replay",
                requested_at=NOW + timedelta(hours=1),
            )
            later_replay = registry.fetch(later_request)
            self.assertEqual(later_replay.batch_id, replay.batch_id)
            self.assertEqual(
                later_replay.normalized_content_sha256,
                replay.normalized_content_sha256,
            )
            self.assertEqual(provider.calls, 1)

    def test_offline_replay_cannot_be_backdated_before_source_fetch(self):
        provider = StaticProvider(
            "baostock",
            "baostock-adapter-v1",
            payload=static_payload([normalized_record()]),
        )
        with tempfile.TemporaryDirectory() as directory:
            registry = self.registry(provider, directory)
            registry.fetch(daily_request("historical_backfill"))
            backdated = MarketDataRequest(
                dataset_type="daily_bar",
                instrument_id="000333.SZ",
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 5),
                retrieval_mode="offline_replay",
                requested_at=NOW - timedelta(seconds=1),
            )
            with self.assertRaisesRegex(ProviderError, "evidence cutoff"):
                registry.fetch(backdated)
        self.assertEqual(provider.calls, 1)

    def test_live_capture_cannot_disguise_a_historical_window(self):
        provider = StaticProvider(
            "baostock",
            "baostock-adapter-v1",
            payload=static_payload([normalized_record()]),
        )
        with tempfile.TemporaryDirectory() as directory:
            registry = self.registry(provider, directory)
            with self.assertRaisesRegex(BatchValidationError, "historical dates"):
                registry.fetch(daily_request("live_capture"))

    def test_v1_request_rejects_all_provider_specific_parameters(self):
        with self.assertRaisesRegex(MarketDataContractError, "must be empty"):
            MarketDataRequest(
                dataset_type="daily_bar",
                instrument_id="000333.SZ",
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 5),
                retrieval_mode="historical_backfill",
                requested_at=NOW,
                parameters={"nested": {"TUSHARE_TOKEN": "SUPERSECRET"}},
            )
        with self.assertRaisesRegex(MarketDataContractError, "only valid"):
            MarketDataRequest(
                dataset_type="daily_bar",
                instrument_id="000333.SZ",
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 5),
                retrieval_mode="historical_backfill",
                requested_at=NOW,
                evidence_cutoff_at=NOW,
            )

    def test_error_text_and_structured_issues_redact_credentials(self):
        samples = (
            "invalid token SUPERSECRET",
            "token is SUPERSECRET",
            "TUSHARE_TOKEN=SUPERSECRET",
            "Authorization: Bearer SUPERSECRET",
            '{"api_key":"SUPERSECRET"}',
            "{'token': 'SUPERSECRET'}",
            '{"authorization":"Bearer SUPERSECRET"}',
        )
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertNotIn("SUPERSECRET", safe_error_text(sample))
        redacted = redact_sensitive_value(
            {"message": "bad token SUPERSECRET", "token": "SUPERSECRET"}
        )
        self.assertNotIn("SUPERSECRET", json.dumps(redacted))

    def test_configured_registry_requires_evidence_storage(self):
        with self.assertRaisesRegex(RegistryConfigurationError, "requires evidence storage"):
            MarketDataRegistry.configured(storage_root=None)

    def test_same_provider_input_has_deterministic_normalized_hash(self):
        first = StaticProvider(
            "baostock",
            "baostock-adapter-v1",
            payload=static_payload([normalized_record()], raw=b"same"),
        )
        second = StaticProvider(
            "baostock",
            "baostock-adapter-v1",
            payload=static_payload([copy.deepcopy(normalized_record())], raw=b"same"),
        )
        with tempfile.TemporaryDirectory() as left_dir, tempfile.TemporaryDirectory() as right_dir:
            left = self.registry(first, left_dir).fetch(daily_request())
            right = self.registry(second, right_dir).fetch(daily_request())
        self.assertEqual(left.raw_content_sha256, right.raw_content_sha256)
        self.assertEqual(left.normalized_content_sha256, right.normalized_content_sha256)

    def test_admission_rejects_unexpected_upstream_independently(self):
        decision = evaluate_admission(
            daily_request(),
            provider_id="baostock",
            upstream_source="baostock.claimed_but_not_configured",
            synthetic=False,
            config=self.config,
        )
        self.assertEqual(decision.admission_status, "rejected_unexpected_upstream")
        self.assertEqual(decision.issues[0]["code"], "unexpected_upstream_source")

        expanded_config = copy.deepcopy(self.config)
        expanded_config["providers"]["impersonator"] = copy.deepcopy(
            expanded_config["providers"]["baostock"]
        )
        not_allowlisted = evaluate_admission(
            daily_request(),
            provider_id="impersonator",
            upstream_source="baostock.query_history_k_data_plus",
            synthetic=False,
            config=expanded_config,
        )
        self.assertEqual(
            not_allowlisted.admission_status, "rejected_provider_not_allowlisted"
        )

    def test_financial_data_without_disclosure_time_is_never_pit_admitted(self):
        config = copy.deepcopy(self.config)
        config["providers"]["baostock"]["datasets"].append("financial_indicator")
        config["providers"]["baostock"]["allowed_upstream_sources"][
            "financial_indicator"
        ] = ["baostock.query_profit_data"]
        request = MarketDataRequest(
            dataset_type="financial_indicator",
            instrument_id="000333.SZ",
            retrieval_mode="historical_backfill",
            requested_at=NOW,
        )
        decision = evaluate_admission(
            request,
            provider_id="baostock",
            upstream_source="baostock.query_profit_data",
            synthetic=False,
            config=config,
        )
        self.assertEqual(
            decision.admission_status,
            "research_only_unless_disclosure_time_present",
        )
        self.assertEqual(decision.point_in_time_status, "research_only_not_pit")
        self.assertNotIn(
            decision.admission_status,
            {"validated_research_only", "admitted_for_research"},
        )

    def test_config_and_schema_files_are_machine_readable(self):
        self.assertEqual(self.config["default_provider"], "baostock")
        for name in (
            "market_data_batch.v1.json",
            "daily_bar.v1.json",
            "trade_calendar.v1.json",
            "security_master.v1.json",
        ):
            payload = json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))
            self.assertEqual(payload["$schema"], "https://json-schema.org/draft/2020-12/schema")


if __name__ == "__main__":
    unittest.main()
