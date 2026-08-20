from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import research.broker_report_audit.official_truth as official_truth_module
from research.broker_report_audit.models import CHINA_TZ
from research.broker_report_audit.official_truth import (
    CHOICE_CANDIDATE_STATUS,
    OfficialTruthReceipt,
    OfficialTruthReceiptError,
    OfficialTruthStorageError,
    get_official_truth_adapter,
    ingest_choice_truth_candidates,
    ingest_official_receipts,
    install_truth_evidence_schema,
    make_choice_truth_candidate,
)
from research.broker_report_audit.storage import AuditStore


FETCHED = datetime(2026, 8, 11, 12, 0, tzinfo=CHINA_TZ)
AS_OF = datetime(2026, 8, 11, 23, 59, tzinfo=CHINA_TZ)


def manual_receipt_kwargs(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "adapter_id": "cninfo_first_disclosure_v1",
        "adapter_version": "1.0.0",
        "source_id": "CNINFO",
        "truth_source": "cninfo_first_disclosure",
        "dimension": "stock",
        "subject_id": "000001.SZ",
        "target_type": "EPS",
        "forecast_period": "2024",
        "unit": "CNY/share",
        "basis": "basic_eps_first_disclosed",
        "realized_value": Decimal("1.23"),
        "observation_kind": "scalar",
        "release_kind": "first_release",
        "release_version": 1,
        "revision_of": "",
        "endpoint_url": "https://www.cninfo.com.cn/new/disclosure/detail",
        "final_url": "https://www.cninfo.com.cn/new/disclosure/detail?id=1",
        "http_status": 200,
        "transport_provenance_sha256": "a" * 64,
        "response_headers_sha256": "b" * 64,
        "document_url": "https://static.cninfo.com.cn/finalpage/report.pdf",
        "request_params": {"stock": "000001"},
        "raw_response_sha256": "c" * 64,
        "document_sha256": "d" * 64,
        "observed_at": datetime(2024, 12, 31, 23, 59, tzinfo=CHINA_TZ),
        "available_at": datetime(2025, 3, 20, 18, 0, tzinfo=CHINA_TZ),
        "fetched_at": FETCHED,
    }
    values.update(overrides)
    return values


class OfficialTruthReceiptTests(unittest.TestCase):
    def test_public_adapter_cannot_self_sign_caller_bytes_or_local_json(self) -> None:
        adapter = get_official_truth_adapter("cninfo_first_disclosure_v1")
        caller_json = b'{"evidence_verified":true,"unit":"CNY/share"}'
        with self.assertRaisesRegex(OfficialTruthReceiptError, "not_configured"):
            adapter.capture_response(
                endpoint_url="https://www.cninfo.com.cn/new/disclosure/detail",
                request_params={"stock": "000001"},
                raw_response=caller_json,
                document_bytes=b"%PDF-1.4\ncaller-authored",
                fetched_at=FETCHED,
            )
        with self.assertRaisesRegex(OfficialTruthReceiptError, "not_configured"):
            adapter.fetch(request_params={"stock": "000001"})

    def test_manual_receipt_boolean_domain_hash_or_revision_cannot_unlock(self) -> None:
        attempts = (
            manual_receipt_kwargs(),
            manual_receipt_kwargs(endpoint_url="https://evil.example/fake"),
            manual_receipt_kwargs(unit="%"),
            manual_receipt_kwargs(
                release_kind="revision", release_version=2, revision_of="prior"
            ),
            manual_receipt_kwargs(
                available_at=datetime(2026, 8, 12, 9, 0, tzinfo=CHINA_TZ)
            ),
        )
        for values in attempts:
            with self.subTest(values=values), self.assertRaisesRegex(
                OfficialTruthReceiptError, "registered adapter"
            ):
                OfficialTruthReceipt(**values)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            OfficialTruthReceipt(  # type: ignore[call-arg]
                **manual_receipt_kwargs(), evidence_verified=True
            )

    def test_even_internal_shape_is_not_admitted_without_real_transport(self) -> None:
        receipt = OfficialTruthReceipt(
            **manual_receipt_kwargs(),
            _authority=official_truth_module._AUTHORITY,  # type: ignore[attr-defined]
        )
        self.assertEqual(receipt.admission_status, "not_configured")
        with self.assertRaisesRegex(OfficialTruthReceiptError, "not_configured"):
            receipt.to_truth_observation(as_of=AS_OF)
        with self.assertRaisesRegex(OfficialTruthReceiptError, "unit/basis"):
            OfficialTruthReceipt(
                **manual_receipt_kwargs(unit="%"),
                _authority=official_truth_module._AUTHORITY,  # type: ignore[attr-defined]
            )
        with self.assertRaisesRegex(OfficialTruthReceiptError, "masquerade"):
            OfficialTruthReceipt(
                **manual_receipt_kwargs(
                    release_version=2, revision_of="prior-receipt"
                ),
                _authority=official_truth_module._AUTHORITY,  # type: ignore[attr-defined]
            )
        with self.assertRaisesRegex(OfficialTruthReceiptError, "timestamps"):
            OfficialTruthReceipt(
                **manual_receipt_kwargs(
                    available_at=datetime(2026, 8, 12, 9, 0, tzinfo=CHINA_TZ)
                ),
                _authority=official_truth_module._AUTHORITY,  # type: ignore[attr-defined]
            )
        mapping = OfficialTruthReceipt(
            **manual_receipt_kwargs(
                adapter_id="sw_index_point_in_time_v1",
                adapter_version="1.0.0",
                source_id="SW_INDEX",
                truth_source="sw_index_point_in_time",
                dimension="industry",
                target_type="industry_membership",
                unit="membership",
                basis="point_in_time",
                realized_value=Decimal("1"),
                observation_kind="industry_mapping",
                endpoint_url="https://www.swsresearch.com/institute_sw/allIndex/releasedIndex",
                final_url="https://www.swsresearch.com/institute_sw/allIndex/releasedIndex?date=2024",
                document_url="https://www.swsresearch.com/index/mapping.pdf",
                observed_at=datetime(2023, 12, 31, 23, 59, tzinfo=CHINA_TZ),
                available_at=datetime(2024, 1, 1, 0, 0, tzinfo=CHINA_TZ),
                mapping_valid_from=datetime(2024, 1, 1, 0, 0, tzinfo=CHINA_TZ),
                mapping_valid_to=datetime(2024, 12, 31, 23, 59, tzinfo=CHINA_TZ),
                mapping_version="SW2021-2024",
            ),
            _authority=official_truth_module._AUTHORITY,  # type: ignore[attr-defined]
        )
        self.assertFalse(mapping.is_valid_for_decision(AS_OF))

    def test_ingestion_accepts_controlled_capture_objects_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(OfficialTruthReceiptError, "capture objects"):
                ingest_official_receipts(  # type: ignore[arg-type]
                    ({"evidence_verified": True},),
                    database_path=Path(directory) / "audit.sqlite3",
                    as_of=AS_OF,
                )

    def test_additive_store_keeps_choice_candidates_out_of_formal_truth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "audit.sqlite3"
            AuditStore(database).close()
            raw = b"choice secondary response"
            choice = make_choice_truth_candidate(
                raw_response=raw,
                subject_id="000001.SZ",
                target_type="EPS",
                forecast_period="2024",
                value="1.22",
                unit="CNY/share",
                basis="aggregated_current_only",
                observed_at=datetime(2024, 12, 31, 23, 59, tzinfo=CHINA_TZ),
                available_at=datetime(2025, 3, 21, 9, 0, tzinfo=CHINA_TZ),
                fetched_at=FETCHED,
                endpoint_name="edb",
                request_params={"indicator": "candidate"},
            )
            result = ingest_choice_truth_candidates((choice,), database_path=database)
            connection = sqlite3.connect(database)
            try:
                candidate_status = connection.execute(
                    "SELECT admission_status FROM choice_truth_candidates_v1"
                ).fetchone()[0]
                formal_count = connection.execute(
                    "SELECT COUNT(*) FROM truth_observations"
                ).fetchone()[0]
            finally:
                connection.close()
        self.assertEqual(result.counts["formal_truth_written"], 0)
        self.assertEqual(candidate_status, CHOICE_CANDIDATE_STATUS)
        self.assertEqual(formal_count, 0)

    def test_choice_hash_or_status_cannot_be_promoted(self) -> None:
        raw = b"choice response"
        candidate = make_choice_truth_candidate(
            raw_response=raw,
            subject_id="000001.SZ",
            target_type="EPS",
            forecast_period="2024",
            value="1.22",
            unit="CNY/share",
            basis="aggregated_current_only",
            observed_at=datetime(2024, 12, 31, 23, 59, tzinfo=CHINA_TZ),
            available_at=datetime(2025, 3, 21, 9, 0, tzinfo=CHINA_TZ),
            fetched_at=FETCHED,
            endpoint_name="edb",
            request_params={"indicator": "candidate"},
        ).candidate
        self.assertEqual(candidate.raw_response_sha256, hashlib.sha256(raw).hexdigest())
        self.assertFalse(hasattr(candidate, "to_truth_observation"))
        self.assertNotIn("evidence_verified", candidate.as_dict())

    def test_future_addon_schema_is_rejected_before_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "future.sqlite3"
            connection = sqlite3.connect(database)
            connection.execute(
                "CREATE TABLE truth_evidence_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO truth_evidence_meta(key,value) VALUES('schema_version','99')"
            )
            connection.commit()
            with self.assertRaisesRegex(OfficialTruthStorageError, "newer"):
                install_truth_evidence_schema(connection)
            connection.close()


if __name__ == "__main__":
    unittest.main()
