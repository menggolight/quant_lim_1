from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from research.broker_report_audit.cli import _active_extractor_bundle_sha256
from research.broker_report_audit.extractors import EXTRACTOR_VERSION, RuleBasedExtractor
from research.broker_report_audit.models import (
    CHINA_TZ,
    ModelValidationError,
    ResearchReport,
)
from research.broker_report_audit.storage import AuditStore
from research.broker_report_audit.validation import (
    ValidationManifestError,
    validate_claim_evidence_bindings,
)


PARSER_VERSION = f"pypdf-test+{EXTRACTOR_VERSION}"


def extractor() -> RuleBasedExtractor:
    return RuleBasedExtractor(parser_version=PARSER_VERSION, prompt_version="none")


def report(*, pdf_sha256: str = "b" * 64, dimension: str = "macro") -> ResearchReport:
    instant = datetime(2025, 1, 2, 8, tzinfo=CHINA_TZ)
    return ResearchReport(
        report_id="provenance-report",
        dimension=dimension,
        subject_id="macro" if dimension == "macro" else "000333.SZ",
        title="预计2025年CPI同比增长2.0%",
        broker="测试券商",
        analyst="甲",
        published_at=instant,
        available_at=instant,
        fetched_at=instant,
        source="fixture",
        content_hash="a" * 64,
        pdf_sha256=pdf_sha256,
        rating="买入" if dimension == "stock" else "",
        source_url="https://example.test/report",
        pdf_url="https://example.test/report.pdf",
    )


class ClaimEvidenceProvenanceTests(unittest.TestCase):
    def test_structured_and_textual_claims_bind_distinct_source_hashes(self) -> None:
        claim_extractor = extractor()
        structured = claim_extractor.extract(report(dimension="stock"))[0]
        self.assertEqual(structured.evidence_source_kind, "structured/source_record")
        self.assertEqual(structured.evidence_source_hash, "a" * 64)
        self.assertTrue(structured.evidence_is_bound)

        textual = claim_extractor.extract(
            report(), "预计2025年CPI同比增长2.0%。"
        )[0]
        self.assertEqual(textual.evidence_source_kind, "textual/pdf")
        self.assertEqual(textual.evidence_source_hash, "b" * 64)
        self.assertEqual(textual.evidence_parser_version, PARSER_VERSION)
        self.assertEqual(textual.evidence_prompt_version, "none")
        self.assertTrue(textual.evidence_is_bound)

    def test_replaced_pdf_changes_claim_identity(self) -> None:
        claim_extractor = extractor()
        original_report = report()
        replaced_report = replace(original_report, pdf_sha256="c" * 64)
        original = claim_extractor.extract(
            original_report, "预计2025年CPI同比增长2.0%。"
        )[0]
        replaced_claim = claim_extractor.extract(
            replaced_report, "预计2025年CPI同比增长2.0%。"
        )[0]
        self.assertNotEqual(original.claim_id, replaced_claim.claim_id)
        self.assertNotEqual(
            original.evidence_source_hash, replaced_claim.evidence_source_hash
        )

    def test_textual_extraction_without_pdf_hash_fails_closed(self) -> None:
        with self.assertRaisesRegex(ModelValidationError, "evidence_source_hash"):
            extractor().extract(
                replace(report(), pdf_sha256=""),
                "预计2025年CPI同比增长2.0%。",
            )

    def test_storage_round_trip_preserves_evidence_binding(self) -> None:
        source_report = report()
        claim = extractor().extract(
            source_report, "预计2025年CPI同比增长2.0%。"
        )[0]
        with tempfile.TemporaryDirectory() as directory:
            with AuditStore(Path(directory) / "audit.sqlite3") as store:
                store.upsert_report(source_report)
                store.upsert_claim(claim)
                loaded_report = next(store.iter_reports())
                loaded_claim = next(store.iter_claims())
        self.assertEqual(loaded_report.pdf_sha256, source_report.pdf_sha256)
        self.assertEqual(loaded_claim.evidence_source_kind, "textual/pdf")
        self.assertEqual(loaded_claim.evidence_source_hash, source_report.pdf_sha256)
        self.assertEqual(loaded_claim.evidence_parser_version, PARSER_VERSION)
        self.assertEqual(loaded_claim.evidence_prompt_version, "none")
        self.assertTrue(loaded_claim.evidence_is_bound)

    def test_pdf_hash_enrichment_is_not_lost_after_listing_upsert(self) -> None:
        listing_only = replace(report(), pdf_sha256="")
        enriched = replace(listing_only, pdf_sha256="b" * 64)
        with tempfile.TemporaryDirectory() as directory:
            with AuditStore(Path(directory) / "audit.sqlite3") as store:
                store.upsert_report(listing_only)
                store.upsert_report(enriched)
                current = next(store.iter_reports())
                versions = list(store.iter_report_versions(report_id=report().report_id))
        self.assertEqual(current.pdf_sha256, "b" * 64)
        self.assertEqual(
            {item.pdf_sha256 for item in versions}, {"", "b" * 64}
        )

    def test_runtime_binding_gate_rejects_legacy_or_replaced_sources(self) -> None:
        source_report = report()
        claim = extractor().extract(
            source_report, "预计2025年CPI同比增长2.0%。"
        )[0]
        self.assertEqual(
            validate_claim_evidence_bindings(
                [claim],
                [source_report],
                expected_extractor_version=EXTRACTOR_VERSION,
                expected_extractor_bundle_sha256=_active_extractor_bundle_sha256(),
                expected_parser_version=PARSER_VERSION,
                expected_prompt_version="none",
            ),
            1,
        )
        with self.assertRaisesRegex(
            ValidationManifestError, "does not match current report source"
        ):
            validate_claim_evidence_bindings(
                [claim],
                [replace(source_report, pdf_sha256="c" * 64)],
                expected_extractor_version=EXTRACTOR_VERSION,
                expected_extractor_bundle_sha256=_active_extractor_bundle_sha256(),
                expected_parser_version=PARSER_VERSION,
                expected_prompt_version="none",
            )
        legacy = replace(
            claim,
            evidence_source_kind="legacy/unverified",
            evidence_source_hash="",
            evidence_parser_version="",
            evidence_prompt_version="",
            extractor_bundle_sha256="",
        )
        with self.assertRaisesRegex(
            ValidationManifestError, "unverified legacy evidence"
        ):
            validate_claim_evidence_bindings(
                [legacy],
                [source_report],
                expected_extractor_version=EXTRACTOR_VERSION,
                expected_extractor_bundle_sha256=_active_extractor_bundle_sha256(),
                expected_parser_version=PARSER_VERSION,
                expected_prompt_version="none",
            )

    def test_pre_contract_claim_migrates_as_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE reports (
                    report_id TEXT PRIMARY KEY, dimension TEXT NOT NULL,
                    subject_id TEXT NOT NULL, subject_name TEXT NOT NULL,
                    industry_id TEXT NOT NULL, title TEXT NOT NULL,
                    broker TEXT NOT NULL, broker_code TEXT NOT NULL,
                    analyst TEXT NOT NULL, team TEXT NOT NULL,
                    published_at TEXT NOT NULL, available_at TEXT NOT NULL,
                    fetched_at TEXT NOT NULL, timestamp_quality TEXT NOT NULL,
                    source TEXT NOT NULL, source_url TEXT NOT NULL,
                    pdf_url TEXT NOT NULL, content_hash TEXT NOT NULL,
                    rating TEXT NOT NULL, rating_change TEXT NOT NULL,
                    target_price_min TEXT, target_price_max TEXT,
                    metadata_json TEXT NOT NULL
                );
                CREATE TABLE claims (
                    claim_id TEXT PRIMARY KEY, report_id TEXT NOT NULL,
                    dimension TEXT NOT NULL, subject_id TEXT NOT NULL,
                    target_type TEXT NOT NULL, direction INTEGER NOT NULL,
                    value_min TEXT, value_max TEXT, unit TEXT NOT NULL,
                    benchmark TEXT NOT NULL, forecast_period TEXT NOT NULL,
                    horizon_days INTEGER NOT NULL, available_at TEXT NOT NULL,
                    evidence_span TEXT NOT NULL, extractor_version TEXT NOT NULL,
                    extraction_confidence REAL NOT NULL
                );
                """
            )
            timestamp = "2025-01-02T00:00:00+00:00"
            connection.execute(
                "INSERT INTO reports VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "legacy-report", "macro", "macro", "", "", "旧报告",
                    "旧券商", "", "", "", timestamp, timestamp, timestamp,
                    "date_only", "fixture", "https://example.test/legacy", "",
                    "a" * 64, "", "", None, None, "{}",
                ),
            )
            connection.execute(
                "INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "legacy-claim", "legacy-report", "macro", "macro", "CPI", 1,
                    None, None, "%", "同比", "2025FY", 120, timestamp,
                    "预计CPI上升", "rule-v0", 0.95,
                ),
            )
            connection.commit()
            connection.close()

            with AuditStore(path) as store:
                loaded_report = next(store.iter_reports())
                loaded_claim = next(store.iter_claims())
        self.assertEqual(loaded_report.pdf_sha256, "")
        self.assertEqual(loaded_claim.evidence_source_kind, "legacy/unverified")
        self.assertEqual(loaded_claim.evidence_source_hash, "")
        self.assertEqual(loaded_claim.evidence_parser_version, "")
        self.assertEqual(loaded_claim.evidence_prompt_version, "")
        self.assertFalse(loaded_claim.evidence_is_bound)


if __name__ == "__main__":
    unittest.main()
