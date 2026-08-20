from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from research.broker_report_audit.models import CHINA_TZ
from research.broker_report_audit.validation_review import (
    REVIEW_CLAIM_FIELDS,
    ValidationReviewError,
    finalize_validation_review,
    prepare_validation_package,
    select_validation_reports,
)
from research.broker_report_audit.validation import population_snapshot


AS_OF = datetime(2026, 8, 11, 23, 59, tzinfo=CHINA_TZ)
VERSIONS = {
    "sample_seed": "choice-stage-two-20260811",
    "extractor_version": "rules-v1",
    "extractor_bundle_sha256": "d" * 64,
    "parser_version": "pypdf-test",
    "prompt_version": "none",
}


def fixtures() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    reports: list[dict[str, object]] = []
    claims: list[dict[str, object]] = []
    for dimension_index, dimension in enumerate(("macro", "industry", "stock")):
        for index in range(30):
            report_id = f"{dimension}-{index:02d}"
            reports.append(
                {
                    "report_id": report_id,
                    "dimension": dimension,
                    "subject_id": f"subject-{dimension}-{index}",
                    "subject_name": f"Subject {index}",
                    "title": f"Title {dimension} {index}",
                    "broker": "Test Broker",
                    "published_at": "2025-01-01T08:00:00+08:00",
                    "available_at": "2025-01-01T09:00:00+08:00",
                    "source_url": f"https://example.test/report/{report_id}",
                    "pdf_url": f"https://example.test/pdf/{report_id}.pdf",
                    "content_hash": f"{dimension_index * 100 + index + 1:064x}",
                    "pdf_sha256": "",
                }
            )
            claims.append(
                {
                    "claim_id": f"claim-{report_id}",
                    "report_id": report_id,
                    "dimension": dimension,
                    "subject_id": f"subject-{dimension}-{index}",
                    "target_type": f"{dimension}_target",
                    "direction": 1,
                    "value_min": "1.0",
                    "value_max": "1.2",
                    "unit": "%",
                    "benchmark": "同比",
                    "forecast_period": "2025",
                    "horizon_days": 120,
                    "available_at": "2025-01-01T09:00:00+08:00",
                    "evidence_span": f"evidence {report_id}",
                    "extractor_version": "rules-v1",
                    "extraction_confidence": 1.0,
                    "evidence_source_kind": "textual/pdf",
                    "evidence_source_hash": "a" * 64,
                    "evidence_parser_version": "pypdf-test",
                    "evidence_prompt_version": "none",
                    "extractor_bundle_sha256": "d" * 64,
                }
            )
    return reports, claims


def pdf_files(root: Path, report_ids: tuple[str, ...]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for index, report_id in enumerate(report_ids):
        path = root / f"{report_id}.pdf"
        path.write_bytes(f"%PDF-1.4\nfixture {index} {report_id}\n%%EOF".encode())
        result[report_id] = path
    return result


def all_pass_review(
    report_ids: tuple[str, ...], reports: list[dict[str, object]], package_sha256: str
) -> dict[str, object]:
    dimension_by_id = {str(report["report_id"]): str(report["dimension"]) for report in reports}
    decision = {"decision": "pass", "corrected_value": ""}
    return {
        "contract_version": "broker-report-extractor-review.v1",
        "package_sha256": package_sha256,
        "reviewer_id": "reviewer-test",
        "reviewed_at": "2026-08-11T10:00:00+08:00",
        "samples": [
            {
                "report_id": report_id,
                "dimension": dimension_by_id[report_id],
                "metadata_reviews": {
                    field: dict(decision)
                    for field in ("broker", "title", "date", "subject")
                },
                "claim_set_review": dict(decision),
                "extraction_reviews": [
                    {
                        "claim_id": f"claim-{report_id}",
                        "fields": {
                            field: dict(decision) for field in REVIEW_CLAIM_FIELDS
                        },
                    }
                ],
            }
            for report_id in report_ids
        ],
    }


class ValidationReviewPackageTests(unittest.TestCase):
    def _prepare(self, root: Path):
        reports, claims = fixtures()
        selection = select_validation_reports(reports, claims, **{key: VERSIONS[key] for key in ("sample_seed",)})
        files = pdf_files(root, selection.report_ids)
        report_by_id = {str(report["report_id"]): report for report in reports}
        claims_by_report = {str(claim["report_id"]): claim for claim in claims}
        for report_id, path in files.items():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            report_by_id[report_id]["pdf_sha256"] = digest
            claims_by_report[report_id]["evidence_source_hash"] = digest
        result = prepare_validation_package(
            reports,
            claims,
            pdf_files=files,
            output_directory=root / "package",
            **VERSIONS,
        )
        return reports, claims, selection, files, result

    def test_prepares_one_offline_html_for_exactly_ninety_pdfs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reports, claims, selection, files, result = self._prepare(Path(directory))
            html = result.html_path.read_text(encoding="utf-8")
        self.assertEqual(len(selection.report_ids), 90)
        self.assertEqual(result.counts["macro"], 30)
        self.assertEqual(result.counts["industry"], 30)
        self.assertEqual(result.counts["stock"], 30)
        self.assertEqual(set(files), set(result.selected_report_ids))
        self.assertIn("data:application/pdf;base64,", html)
        self.assertIn("只有全部字段完成后才能导出", html)
        self.assertIn("evidence_source_kind=", html)
        self.assertNotIn("<script src=", html)
        self.assertNotIn("<link rel=", html)

    def test_rejects_extra_pdf_outside_the_bounded_sample(self) -> None:
        reports, claims = fixtures()
        selection = select_validation_reports(reports, claims, sample_seed=VERSIONS["sample_seed"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = pdf_files(root, selection.report_ids)
            extra = root / "extra.pdf"
            extra.write_bytes(b"%PDF-1.4\nextra")
            files["not-selected"] = extra
            with self.assertRaisesRegex(ValidationReviewError, "exactly"):
                prepare_validation_package(
                    reports,
                    claims,
                    pdf_files=files,
                    output_directory=root / "package",
                    **VERSIONS,
                )

    def test_selection_does_not_require_existing_claims_or_pdf_url(self) -> None:
        reports, claims = fixtures()
        for report in reports:
            report["pdf_url"] = ""
        sparse_claims = [
            claim
            for claim in claims
            if claim["dimension"] != "macro"
            or int(str(claim["report_id"]).rsplit("-", 1)[1]) < 23
        ]
        selection = select_validation_reports(
            reports, sparse_claims, sample_seed=VERSIONS["sample_seed"]
        )
        self.assertEqual(len(selection.report_ids_by_dimension["macro"]), 30)

    def test_selection_hash_matches_runtime_population_for_datetime_models(self) -> None:
        reports, claims = fixtures()
        for report in reports:
            report["available_at"] = datetime(2025, 1, 1, 9, 0, tzinfo=CHINA_TZ)
        selection = select_validation_reports(
            reports, claims, sample_seed=VERSIONS["sample_seed"]
        )
        self.assertEqual(
            selection.source_population_sha256,
            population_snapshot(reports)[0],
        )

    def test_finalizes_complete_review_to_passing_v3_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports, claims, selection, files, package = self._prepare(root)
            review = all_pass_review(selection.report_ids, reports, package.package_sha256)
            review_path = root / "review.json"
            review_path.write_text(json.dumps(review), encoding="utf-8")
            result = finalize_validation_review(
                review_path,
                reports,
                claims,
                pdf_files=files,
                output_directory=root / "final",
                as_of=AS_OF,
                **VERSIONS,
            )
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(result.counts["total"], 90)
        self.assertEqual(len(manifest["samples"]), 90)
        self.assertTrue(all(result.gate_result[dimension]["passed"] for dimension in ("macro", "industry", "stock")))
        self.assertEqual(manifest["review_export_sha256"], result.review_sha256)
        check = manifest["samples"][0]["extraction_checks"][0]
        self.assertEqual(check["evidence_source_kind"], "textual/pdf")
        self.assertEqual(len(check["evidence_source_hash"]), 64)

    def test_incomplete_tampered_or_version_drifted_review_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports, claims, selection, files, package = self._prepare(root)
            base = all_pass_review(selection.report_ids, reports, package.package_sha256)
            cases: list[tuple[str, dict[str, object], dict[str, object]]] = []
            incomplete = copy.deepcopy(base)
            incomplete["samples"] = incomplete["samples"][:-1]  # type: ignore[index]
            cases.append(("incomplete", incomplete, {}))
            tampered = copy.deepcopy(base)
            tampered["package_sha256"] = "0" * 64
            cases.append(("tampered", tampered, {}))
            cases.append(("version", copy.deepcopy(base), {"parser_version": "changed-parser"}))
            for label, review, overrides in cases:
                with self.subTest(label=label):
                    review_path = root / f"{label}.json"
                    review_path.write_text(json.dumps(review), encoding="utf-8")
                    arguments = dict(VERSIONS)
                    arguments.update(overrides)
                    with self.assertRaises(ValidationReviewError):
                        finalize_validation_review(
                            review_path,
                            reports,
                            claims,
                            pdf_files=files,
                            output_directory=root / f"final-{label}",
                            as_of=AS_OF,
                            **arguments,
                        )

    def test_pdf_replacement_and_sub_95_percent_precision_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports, claims, selection, files, package = self._prepare(root)
            review = all_pass_review(selection.report_ids, reports, package.package_sha256)
            review_path = root / "review.json"
            review_path.write_text(json.dumps(review), encoding="utf-8")
            first_pdf = files[selection.report_ids[0]]
            original = first_pdf.read_bytes()
            first_pdf.write_bytes(original + b"\nreplacement")
            with self.assertRaisesRegex(ValidationReviewError, "PDF hash mismatch"):
                finalize_validation_review(
                    review_path,
                    reports,
                    claims,
                    pdf_files=files,
                    output_directory=root / "replaced",
                    as_of=AS_OF,
                    **VERSIONS,
                )
            first_pdf.write_bytes(original)
            stock_samples = [
                sample
                for sample in review["samples"]  # type: ignore[index]
                if sample["dimension"] == "stock"
            ]
            for sample in stock_samples[:2]:
                sample["extraction_reviews"][0]["fields"]["target_type"] = {  # type: ignore[index]
                    "decision": "correct",
                    "corrected_value": "corrected_target",
                }
            review_path.write_text(json.dumps(review), encoding="utf-8")
            with self.assertRaisesRegex(ValidationReviewError, "thresholds"):
                finalize_validation_review(
                    review_path,
                    reports,
                    claims,
                    pdf_files=files,
                    output_directory=root / "low-precision",
                    as_of=AS_OF,
                    **VERSIONS,
                )

    def test_public_finalizer_rejects_claim_not_bound_to_selected_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports, claims, selection, files, package = self._prepare(root)
            review = all_pass_review(
                selection.report_ids, reports, package.package_sha256
            )
            review_path = root / "review.json"
            review_path.write_text(json.dumps(review), encoding="utf-8")
            claims[0]["evidence_source_hash"] = "f" * 64
            with self.assertRaisesRegex(
                ValidationReviewError, "evidence hash does not match"
            ):
                finalize_validation_review(
                    review_path,
                    reports,
                    claims,
                    pdf_files=files,
                    output_directory=root / "mismatched-evidence",
                    as_of=AS_OF,
                    **VERSIONS,
                )


if __name__ == "__main__":
    unittest.main()
