from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from research.broker_report_audit.models import CHINA_TZ
from research.broker_report_audit.validation import (
    ValidationManifestError,
    claim_validation_payload_sha256,
    load_validation_manifest,
    population_snapshot,
)


def population_reports() -> list[dict[str, str]]:
    return [
        {
            "report_id": f"{dimension}-{index}",
            "dimension": dimension,
            "source_url": f"https://example.test/{dimension}/{index}",
            "content_hash": f"{index + 1:064x}",
            "pdf_url": f"https://example.test/{dimension}/{index}.pdf",
            "pdf_sha256": f"{index + 101:064x}",
            "available_at": "2025-01-01T09:30:00+08:00",
        }
        for dimension in ("macro", "industry", "stock")
        for index in range(30)
    ]


def population_claims() -> list[dict[str, str]]:
    return [
        {
            "claim_id": f"claim-{dimension}-{index}",
            "report_id": f"{dimension}-{index}",
            "target_type": f"{dimension}_target",
            "evidence_span": f"evidence-{dimension}-{index}",
        }
        for dimension in ("macro", "industry", "stock")
        for index in range(30)
    ]


def manifest() -> dict[str, object]:
    samples = []
    for dimension in ("macro", "industry", "stock"):
        for index in range(30):
            samples.append(
                {
                    "report_id": f"{dimension}-{index}",
                    "dimension": dimension,
                    "source_url": f"https://example.test/{dimension}/{index}",
                    "source_record_hash": f"{index + 1:064x}",
                    "pdf_document_hash": f"{index + 101:064x}",
                    "metadata_checks": {
                        "broker": True,
                        "title": True,
                        "date": True,
                        "subject": True,
                    },
                    "claim_set_complete": True,
                    "extraction_checks": [
                        {
                            "claim_id": f"claim-{dimension}-{index}",
                            "target_type": f"{dimension}_target",
                            "evidence_span_sha256": hashlib.sha256(
                                f"evidence-{dimension}-{index}".encode("utf-8")
                            ).hexdigest(),
                            "claim_payload_sha256": claim_validation_payload_sha256(
                                population_claims()[
                                    (0 if dimension == "macro" else 30 if dimension == "industry" else 60)
                                    + index
                                ]
                            ),
                            "variable": True,
                            "direction": True,
                            "value": True,
                            "horizon": True,
                        }
                    ],
                }
            )
    return {
        "contract_version": "broker-report-extractor-validation.v3",
        "sample_seed": "20260804",
        "population_snapshot_hash": population_snapshot(population_reports())[0],
        "extractor_version": "rules-v1",
        "extractor_bundle_sha256": "d" * 64,
        "parser_version": "pypdf-test",
        "prompt_version": "none",
        "reviewer": "manual-reviewer",
        "reviewed_at": "2026-08-04T10:00:00+08:00",
        "samples": samples,
    }


class ValidationManifestTests(unittest.TestCase):
    def _write(self, root: Path, payload: dict[str, object]) -> tuple[Path, str]:
        path = root / "validation.json"
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        path.write_bytes(body)
        return path, hashlib.sha256(body).hexdigest()

    def test_gate_is_recomputed_from_ninety_row_level_samples(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, digest = self._write(Path(directory), manifest())
            result = load_validation_manifest(
                path,
                expected_sha256=digest,
                as_of=datetime(2026, 8, 4, 23, 59, tzinfo=CHINA_TZ),
                population_reports=population_reports(),
                population_claims=population_claims(),
                expected_extractor_version="rules-v1",
                expected_extractor_bundle_sha256="d" * 64,
                expected_parser_version="pypdf-test",
                expected_prompt_version="none",
            )
        self.assertTrue(all(result[dimension]["passed"] for dimension in result))
        self.assertEqual(result["stock"]["sample_count"], 30)
        self.assertEqual(result["stock"]["metadata_match_rate"], 1.0)
        self.assertEqual(
            result["stock"]["validation_contract_version"],
            "broker-report-extractor-validation.v3",
        )
        self.assertEqual(result["stock"]["extractor_version"], "rules-v1")
        self.assertEqual(result["stock"]["parser_version"], "pypdf-test")
        self.assertEqual(result["stock"]["prompt_version"], "none")

    def test_summary_cannot_be_unlocked_by_editing_a_boolean(self) -> None:
        payload = manifest()
        stock_samples = [
            sample
            for sample in payload["samples"]  # type: ignore[index]
            if sample["dimension"] == "stock"
        ]
        stock_samples[0]["extraction_checks"][0]["value"] = False
        stock_samples[1]["extraction_checks"][0]["value"] = False
        with tempfile.TemporaryDirectory() as directory:
            path, digest = self._write(Path(directory), payload)
            result = load_validation_manifest(
                path,
                expected_sha256=digest,
                as_of=datetime(2026, 8, 4, 23, 59, tzinfo=CHINA_TZ),
                population_reports=population_reports(),
                population_claims=population_claims(),
                expected_extractor_version="rules-v1",
                expected_extractor_bundle_sha256="d" * 64,
                expected_parser_version="pypdf-test",
                expected_prompt_version="none",
            )
        self.assertFalse(result["stock"]["passed"])
        self.assertLess(result["stock"]["field_precision_by_field"]["value"], 0.95)

    def test_manifest_hash_tampering_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, _ = self._write(Path(directory), manifest())
            with self.assertRaises(ValidationManifestError):
                load_validation_manifest(
                    path,
                    expected_sha256="0" * 64,
                    as_of=datetime(2026, 8, 4, 23, 59, tzinfo=CHINA_TZ),
                    population_reports=population_reports(),
                    population_claims=population_claims(),
                    expected_extractor_version="rules-v1",
                    expected_extractor_bundle_sha256="d" * 64,
                    expected_parser_version="pypdf-test",
                    expected_prompt_version="none",
                )

    def test_replaced_pdf_invalidates_prior_manual_validation(self) -> None:
        reports = population_reports()
        payload = manifest()
        reports[0]["pdf_sha256"] = "f" * 64
        with tempfile.TemporaryDirectory() as directory:
            path, digest = self._write(Path(directory), payload)
            with self.assertRaisesRegex(
                ValidationManifestError, "population_snapshot_hash"
            ):
                load_validation_manifest(
                    path,
                    expected_sha256=digest,
                    as_of=datetime(2026, 8, 4, 23, 59, tzinfo=CHINA_TZ),
                    population_reports=reports,
                    population_claims=population_claims(),
                    expected_extractor_version="rules-v1",
                    expected_extractor_bundle_sha256="d" * 64,
                    expected_parser_version="pypdf-test",
                    expected_prompt_version="none",
                )

    def test_population_without_pdf_hash_fails_closed(self) -> None:
        reports = population_reports()
        reports[0]["pdf_sha256"] = ""
        with self.assertRaisesRegex(ValidationManifestError, "PDF document hash"):
            population_snapshot(reports)

    def test_every_current_claim_must_be_reviewed(self) -> None:
        payload = manifest()
        claims = population_claims()
        claims.append(
            {
                "claim_id": "unreviewed-extra-claim",
                "report_id": "stock-0",
                "target_type": "stock_extra",
                "evidence_span": "an extra extractor output",
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            path, digest = self._write(Path(directory), payload)
            with self.assertRaisesRegex(
                ValidationManifestError, "complete current claim set"
            ):
                load_validation_manifest(
                    path,
                    expected_sha256=digest,
                    as_of=datetime(2026, 8, 4, 23, 59, tzinfo=CHINA_TZ),
                    population_reports=population_reports(),
                    population_claims=claims,
                    expected_extractor_version="rules-v1",
                    expected_extractor_bundle_sha256="d" * 64,
                    expected_parser_version="pypdf-test",
                    expected_prompt_version="none",
                )


if __name__ == "__main__":
    unittest.main()
