from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from unittest.mock import MagicMock, patch

from research.broker_report_audit.cli import _extract_pdf_texts
from research.broker_report_audit.models import (
    CHINA_TZ,
    ClaimOutcome,
    DailyBar,
    ResearchClaim,
    ResearchReport,
)
from research.broker_report_audit.storage import (
    AuditStorageError,
    AuditStore,
    ContentAddressedHttpCache,
    SCHEMA_VERSION,
)
from research.broker_report_audit.sources import CachedHttpClient, HttpResponse


def at(day: int, hour: int = 8) -> datetime:
    return datetime(2024, 1, day, hour, tzinfo=CHINA_TZ)


def make_report(*, fetched_at: datetime, digest: str, rating: str) -> ResearchReport:
    return ResearchReport(
        report_id="versioned-report",
        dimension="stock",
        subject_id="000333.SZ",
        subject_name="美的集团",
        industry_id="801110.SI",
        title="版本化研报",
        broker="测试券商",
        analyst="分析师甲",
        published_at=at(1),
        available_at=at(1),
        fetched_at=fetched_at,
        source="eastmoney_public_sample",
        source_url="https://example.test/report",
        pdf_url="https://example.test/report.pdf",
        content_hash=digest,
        pdf_sha256=("a" * 64),
        rating=rating,
        metadata={"rating": rating},
    )


def make_bar(*, fetched_at: datetime, digest: str, close: int) -> DailyBar:
    return DailyBar(
        instrument_id="000333.SZ",
        trade_date=date(2024, 1, 2),
        open=100,
        high=max(100, close),
        low=min(100, close),
        close=close,
        volume=1000,
        amount=100000,
        adjusted_open=100,
        adjusted_high=max(100, close),
        adjusted_low=min(100, close),
        adjusted_close=close,
        suspended=False,
        available_at=at(2, 16),
        source="eastmoney_public.push2his",
        fetched_at=fetched_at,
        content_hash=digest,
    )


def make_claim() -> ResearchClaim:
    return ResearchClaim(
        claim_id="versioned-claim",
        report_id="versioned-report",
        dimension="stock",
        subject_id="000333.SZ",
        target_type="EPS",
        direction=0,
        value_min=Decimal("1.00"),
        value_max=Decimal("1.00"),
        unit="CNY/share",
        benchmark="annual_report_basic_eps",
        forecast_period="2024FY",
        horizon_days=120,
        available_at=at(1),
        evidence_span="预计EPS为1元",
        extractor_version="rules-v1",
        extraction_confidence=0.99,
    )


def make_outcome(*, evaluated_at: datetime, mature: bool, market_return: float) -> ClaimOutcome:
    return ClaimOutcome(
        claim_id="versioned-claim",
        truth_source="market_bars",
        truth_available_at=at(4),
        realized_value=None,
        market_return=market_return,
        benchmark_return=0.01,
        error=None,
        hit=True if mature else None,
        mature=mature,
        exclusion_reason="" if mature else "horizon_not_mature",
        evaluated_at=evaluated_at,
        market_hit=True if mature else None,
        market_excess_return=market_return - 0.01,
        market_truth_source="eastmoney_qfq_daily",
        market_benchmark_id="BK_TEST",
        market_benchmark_kind="industry",
    )


class AppendOnlyAuditVersionTests(unittest.TestCase):
    def test_outcome_versions_replay_latest_known_without_falling_forward(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "audit.sqlite3"
            old = make_outcome(evaluated_at=at(5), mature=False, market_return=0.02)
            future = make_outcome(evaluated_at=at(20), mature=True, market_return=0.20)
            with AuditStore(database) as store:
                store.upsert_report(
                    make_report(fetched_at=at(3), digest="b" * 64, rating="增持")
                )
                store.upsert_claim(make_claim())
                store.upsert_outcome(old)
                store.upsert_outcome(future)
                self.assertEqual(tuple(store.iter_outcome_versions()), (old, future))
                self.assertEqual(tuple(store.iter_outcomes()), (future,))

            with AuditStore(database, decision_time=at(10, 23)) as historical:
                self.assertEqual(tuple(historical.iter_outcomes()), (old,))
                self.assertEqual(tuple(historical.iter_outcomes(mature=True)), ())
                self.assertEqual(tuple(historical.iter_outcome_versions()), (old,))

    def test_outcome_exact_replay_is_offline_deterministic_and_collision_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "audit.sqlite3"
            outcome = make_outcome(evaluated_at=at(20), mature=True, market_return=0.20)
            with AuditStore(database) as store:
                store.upsert_report(
                    make_report(fetched_at=at(3), digest="c" * 64, rating="增持")
                )
                store.upsert_claim(make_claim())
                store.upsert_outcome(outcome)
                version_ids_before = tuple(
                    row[0]
                    for row in store.connection.execute(
                        "SELECT outcome_version_id FROM claim_outcome_versions "
                        "ORDER BY outcome_version_id"
                    )
                )
                store.upsert_outcome(outcome)
                version_ids_after = tuple(
                    row[0]
                    for row in store.connection.execute(
                        "SELECT outcome_version_id FROM claim_outcome_versions "
                        "ORDER BY outcome_version_id"
                    )
                )
                self.assertEqual(version_ids_before, version_ids_after)
                self.assertEqual(len(version_ids_after), 1)
                with self.assertRaises(AuditStorageError):
                    store.upsert_outcome(replace(outcome, market_return=0.21))

            with AuditStore(database) as reopened:
                self.assertEqual(tuple(reopened.iter_outcomes()), (outcome,))
                self.assertEqual(
                    tuple(
                        row[0]
                        for row in reopened.connection.execute(
                            "SELECT outcome_version_id FROM claim_outcome_versions "
                            "ORDER BY outcome_version_id"
                        )
                    ),
                    version_ids_before,
                )

    def test_outcome_version_migration_seeds_legacy_latest_and_fails_on_bad_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "audit.sqlite3"
            outcome = make_outcome(evaluated_at=at(5), mature=False, market_return=0.02)
            with AuditStore(database) as store:
                store.upsert_report(
                    make_report(fetched_at=at(3), digest="d" * 64, rating="增持")
                )
                store.upsert_claim(make_claim())
                store.upsert_outcome(outcome)
            connection = sqlite3.connect(database)
            connection.execute("DROP TABLE claim_outcome_versions")
            connection.execute(
                "UPDATE audit_meta SET value = ? WHERE key = 'schema_version'",
                (str(SCHEMA_VERSION - 1),),
            )
            connection.commit()
            connection.close()

            with AuditStore(database) as migrated:
                self.assertEqual(tuple(migrated.iter_outcome_versions()), (outcome,))

            connection = sqlite3.connect(database)
            connection.execute("DROP TABLE claim_outcome_versions")
            connection.execute(
                "CREATE TABLE claim_outcome_versions(outcome_version_id TEXT PRIMARY KEY)"
            )
            connection.execute(
                "UPDATE audit_meta SET value = ? WHERE key = 'schema_version'",
                (str(SCHEMA_VERSION - 1),),
            )
            connection.commit()
            connection.close()
            with self.assertRaises(AuditStorageError):
                AuditStore(database)

    def test_future_schema_version_is_rejected_without_rewriting_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "audit.sqlite3"
            AuditStore(database).close()
            connection = sqlite3.connect(database)
            future_version = SCHEMA_VERSION + 1
            connection.execute(
                "UPDATE audit_meta SET value = ? WHERE key = 'schema_version'",
                (str(future_version),),
            )
            connection.commit()
            connection.close()

            with self.assertRaises(AuditStorageError):
                AuditStore(database)
            connection = sqlite3.connect(database)
            stored = connection.execute(
                "SELECT value FROM audit_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            connection.close()
            self.assertEqual(stored, str(future_version))

    def test_refresh_keeps_report_and_bar_versions_and_replays_old_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "audit.sqlite3"
            report_v1 = make_report(
                fetched_at=at(3), digest="1" * 64, rating="增持"
            )
            report_v2 = replace(
                report_v1,
                fetched_at=at(20),
                content_hash="2" * 64,
                rating="买入",
                metadata={"rating": "买入"},
            )
            bar_v1 = make_bar(fetched_at=at(3), digest="3" * 64, close=101)
            bar_v2 = replace(
                bar_v1,
                fetched_at=at(20),
                content_hash="4" * 64,
                close=105,
                high=105,
                adjusted_close=105,
                adjusted_high=105,
            )

            with AuditStore(database) as store:
                store.upsert_report(report_v1)
                store.upsert_daily_bar(bar_v1)
                # Exact replay is idempotent in the append-only tables.
                store.upsert_report(report_v1)
                store.upsert_daily_bar(bar_v1)
                store.upsert_report(report_v2)
                store.upsert_daily_bar(bar_v2)
                self.assertEqual(len(list(store.iter_report_versions())), 2)
                self.assertEqual(len(list(store.iter_daily_bar_versions())), 2)
                self.assertEqual(tuple(store.iter_reports())[0].content_hash, "2" * 64)
                self.assertEqual(tuple(store.iter_daily_bars())[0].content_hash, "4" * 64)

            with AuditStore(database, decision_time=at(10, 23)) as historical:
                old_report = tuple(historical.iter_reports())
                old_bar = tuple(historical.iter_daily_bars())
                self.assertEqual(len(old_report), 1)
                self.assertEqual(old_report[0].content_hash, "1" * 64)
                self.assertEqual(old_report[0].rating, "增持")
                self.assertEqual(len(old_bar), 1)
                self.assertEqual(old_bar[0].content_hash, "3" * 64)
                self.assertEqual(int(old_bar[0].close), 101)

    def test_explicit_version_as_of_is_strict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "audit.sqlite3"
            report = make_report(
                fetched_at=at(20), digest="5" * 64, rating="买入"
            )
            bar = make_bar(fetched_at=at(20), digest="6" * 64, close=106)
            with AuditStore(database) as store:
                store.upsert_report(report)
                store.upsert_daily_bar(bar)
                self.assertEqual(
                    tuple(store.iter_reports(available_by=at(10, 23)))[0].content_hash,
                    "5" * 64,
                )
                self.assertEqual(
                    tuple(store.iter_daily_bars(available_by=at(10, 23)))[0].content_hash,
                    "6" * 64,
                )
                self.assertEqual(
                    tuple(store.iter_reports(version_as_of=at(10, 23))), ()
                )
                self.assertEqual(
                    tuple(store.iter_daily_bars(version_as_of=at(10, 23))), ()
                )

    def test_decision_bound_store_never_falls_forward_to_future_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "audit.sqlite3"
            report = make_report(
                fetched_at=at(20), digest="9" * 64, rating="买入"
            )
            bar = make_bar(fetched_at=at(20), digest="a" * 64, close=106)
            with AuditStore(database) as store:
                store.upsert_report(report)
                store.upsert_daily_bar(bar)

            with AuditStore(database, decision_time=at(10, 23)) as historical:
                self.assertEqual(tuple(historical.iter_reports()), ())
                self.assertEqual(tuple(historical.iter_daily_bars()), ())

    def test_legacy_snapshots_seed_version_tables_without_data_loss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "audit.sqlite3"
            report = make_report(
                fetched_at=at(3), digest="7" * 64, rating="增持"
            )
            bar = make_bar(fetched_at=at(3), digest="8" * 64, close=102)
            with AuditStore(database) as store:
                store.upsert_report(report)
                store.upsert_daily_bar(bar)
            connection = sqlite3.connect(database)
            connection.execute("DROP TABLE report_versions")
            connection.execute("DROP TABLE daily_bar_versions")
            connection.execute(
                "UPDATE audit_meta SET value = '10' WHERE key = 'schema_version'"
            )
            connection.commit()
            connection.close()

            with AuditStore(database) as migrated:
                report_versions = tuple(migrated.iter_report_versions())
                bar_versions = tuple(migrated.iter_daily_bar_versions())
                self.assertEqual(len(report_versions), 1)
                self.assertEqual(report_versions[0].pdf_sha256, "a" * 64)
                self.assertEqual(len(bar_versions), 1)
                self.assertEqual(bar_versions[0].content_hash, "8" * 64)


class AppendOnlyHttpCacheTests(unittest.TestCase):
    def test_request_refresh_is_versioned_and_as_of_replayable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with ContentAddressedHttpCache(directory) as cache:
                first = cache.put(
                    "request-key",
                    "https://example.test/data",
                    200,
                    {"ETag": "v1"},
                    b"old",
                    at(3),
                )
                cache.put(
                    "request-key",
                    "https://example.test/data",
                    200,
                    {"ETag": "v1"},
                    b"old",
                    at(3),
                )
                second = cache.put(
                    "request-key",
                    "https://example.test/data",
                    200,
                    {"ETag": "v2"},
                    b"new",
                    at(20),
                )

                self.assertEqual(len(tuple(cache.iter_versions("request-key"))), 2)
                self.assertEqual(cache.get("request-key").body, b"new")  # type: ignore[union-attr]
                self.assertEqual(
                    cache.get("request-key", as_of=at(10, 23)).body,  # type: ignore[union-attr]
                    b"old",
                )
                self.assertEqual(
                    cache.get_version(
                        "request-key", content_hash=first.content_hash
                    ).body,  # type: ignore[union-attr]
                    b"old",
                )
                self.assertNotEqual(first.content_hash, second.content_hash)

    def test_client_replay_never_falls_forward_to_future_cache_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with ContentAddressedHttpCache(directory) as cache:
                url = CachedHttpClient.canonical_url("https://example.test/data")
                headers = {
                    "Accept": "application/json,text/html,application/pdf,*/*",
                    "User-Agent": "broker-report-audit-v1/research-only",
                }
                key = CachedHttpClient.request_key(url, headers)
                cache.put(key, url, 200, {}, b"old", at(3))
                cache.put(key, url, 200, {}, b"future", at(20))
                client = CachedHttpClient(
                    cache,
                    offline=True,
                    as_of=at(10, 23),
                    user_agent="broker-report-audit-v1/research-only",
                )
                self.assertEqual(client.get(url).body, b"old")


class PdfCaptureVersioningTests(unittest.TestCase):
    def test_pdf_enrichment_is_visible_only_after_actual_fetch(self) -> None:
        report = replace(
            make_report(fetched_at=at(3), digest="3" * 64, rating="买入"),
            pdf_sha256="",
        )
        pdf_bytes = b"deterministic-pdf-fixture"
        pdf_fetch = datetime(2024, 1, 20, 12, 0, tzinfo=CHINA_TZ)
        response = HttpResponse(
            url=report.pdf_url,
            status=200,
            headers={"Content-Type": "application/pdf"},
            body=pdf_bytes,
            fetched_at=pdf_fetch,
            content_hash=sha256(pdf_bytes).hexdigest(),
            from_cache=True,
        )
        reader = MagicMock()
        reader.is_encrypted = False
        page = MagicMock()
        page.extract_text.return_value = "预计2024年EPS为1.20元"
        reader.pages = [page]
        decision = datetime(2024, 1, 31, 23, 0, tzinfo=CHINA_TZ)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "audit.sqlite3"
            with AuditStore(database) as store:
                store.upsert_report(report)
                with patch(
                    "research.broker_report_audit.sources.EastmoneySource.fetch_pdf",
                    return_value=response,
                ), patch("pypdf.PdfReader", return_value=reader):
                    texts = _extract_pdf_texts(
                        [report],
                        store=store,
                        cache_directory=root / "cache",
                        offline=True,
                        decision=decision,
                        issues=[],
                    )
            self.assertIn(report.report_id, texts)
            with AuditStore(database, decision_time=at(10, 23)) as historical:
                rows = tuple(historical.iter_reports())
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].pdf_sha256, "")
            with AuditStore(database, decision_time=decision) as current_store:
                rows = tuple(current_store.iter_reports())
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].pdf_sha256, sha256(pdf_bytes).hexdigest())


if __name__ == "__main__":
    unittest.main()
