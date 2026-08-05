from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from research.broker_report_audit import (
    AuditStore,
    CHINA_TZ,
    ModelValidationError,
    TruthImportError,
    TruthObservation,
    import_truth_observations,
)
from research.broker_report_audit.storage import SCHEMA_VERSION


AVAILABLE = datetime(2025, 1, 10, 10, 0, tzinfo=CHINA_TZ)
FETCHED = datetime(2026, 8, 4, 12, 0, tzinfo=CHINA_TZ)
EVIDENCE_BYTES = b"official-evidence-fixture"
EVIDENCE_HASH = hashlib.sha256(EVIDENCE_BYTES).hexdigest()
SOURCE_DOMAINS = {"stats_nbs_first_release": {"data.stats.gov.cn"}}


def truth(**overrides: object) -> TruthObservation:
    values: dict[str, object] = {
        "dimension": "macro",
        "subject_id": "CN-CPI",
        "target_type": "CPI",
        "forecast_period": "2024-12",
        "unit": "%",
        "basis": "同比",
        "realized_value": Decimal("0.1"),
        "truth_source": "stats_nbs_first_release",
        "available_at": AVAILABLE,
        "fetched_at": FETCHED,
        "first_release": True,
        "revision": False,
        "content_hash": EVIDENCE_HASH,
        "evidence_url": "https://data.stats.gov.cn/easyquery.htm?id=CN-CPI-2024-12",
    }
    values.update(overrides)
    return TruthObservation(**values)  # type: ignore[arg-type]


def import_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "claim_id": "claim-cpi-2024-12",
        "dimension": "macro",
        "subject_id": "CN-CPI",
        "target_type": "CPI",
        "forecast_period": "2024-12",
        "unit": "%",
        "basis": "同比",
        "realized_value": 0.1,
        "truth_source": "stats_nbs_first_release",
        "available_at": "2025-01-10T10:00:00+08:00",
        "fetched_at": "2026-08-04T12:00:00+08:00",
        "first_release": True,
        "revision": False,
        "content_hash": EVIDENCE_HASH,
        "evidence_url": "https://data.stats.gov.cn/easyquery.htm?id=CN-CPI-2024-12",
        "evidence_path": "evidence.bin",
    }
    values.update(overrides)
    return values


class TruthObservationModelTests(unittest.TestCase):
    def test_requires_claim_or_complete_locator_and_explicit_release_kind(self) -> None:
        self.assertTrue(truth().first_release)
        self.assertEqual(truth(claim_id="claim-1", dimension="", subject_id="", target_type="", forecast_period="").claim_id, "claim-1")

        invalid = (
            {"dimension": "", "subject_id": "", "target_type": "", "forecast_period": ""},
            {"subject_id": ""},
            {"first_release": True, "revision": True},
            {"first_release": False, "revision": False},
            {"fetched_at": datetime(2025, 1, 9, 10, 0, tzinfo=CHINA_TZ)},
            {"content_hash": "not-sha256"},
        )
        for override in invalid:
            with self.subTest(override=override), self.assertRaises(ModelValidationError):
                truth(**override)


class TruthStorageTests(unittest.TestCase):
    def test_versions_are_immutable_and_reads_default_to_first_release(self) -> None:
        first = truth()
        revision = truth(
            realized_value=Decimal("0.2"),
            available_at=datetime(2025, 2, 10, 10, 0, tzinfo=CHINA_TZ),
            first_release=False,
            revision=True,
            content_hash="b" * 64,
            truth_source="stats_nbs_first_release",
        )
        self.assertNotEqual(first.observation_id, revision.observation_id)

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "audit.sqlite3"
            with AuditStore(database) as store:
                self.assertEqual(store.insert_truth_observations((first, revision)), 2)
                self.assertFalse(store.insert_truth_observation(first))
                self.assertEqual(tuple(store.iter_truth_observations()), (first,))
                self.assertEqual(
                    tuple(store.iter_truth_observations(first_release=False)),
                    (revision,),
                )
                self.assertEqual(
                    tuple(store.iter_truth_observations(first_release=None)),
                    (first, revision),
                )

            cutoff = datetime(2025, 1, 31, 23, 59, tzinfo=CHINA_TZ)
            with AuditStore(database, decision_time=cutoff) as historical:
                self.assertEqual(
                    tuple(historical.iter_truth_observations(first_release=None)),
                    (),
                )

            fetched_cutoff = datetime(2026, 8, 4, 23, 59, tzinfo=CHINA_TZ)
            with AuditStore(database, decision_time=fetched_cutoff) as captured:
                self.assertEqual(
                    tuple(captured.iter_truth_observations(first_release=None)),
                    (first, revision),
                )

    def test_schema_creation_migrates_an_existing_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "audit.sqlite3"
            AuditStore(database).close()
            connection = sqlite3.connect(database)
            connection.execute("DROP TABLE truth_observations")
            connection.execute(
                "UPDATE audit_meta SET value=? WHERE key='schema_version'",
                (str(SCHEMA_VERSION - 1),),
            )
            connection.commit()
            connection.close()

            with AuditStore(database) as migrated:
                tables = {
                    row[0]
                    for row in migrated.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertIn("truth_observations", tables)
                version = migrated.connection.execute(
                    "SELECT value FROM audit_meta WHERE key='schema_version'"
                ).fetchone()[0]
                self.assertEqual(version, str(SCHEMA_VERSION))


class TruthImportTests(unittest.TestCase):
    def test_json_jsonl_and_csv_import_without_network_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "evidence.bin").write_bytes(EVIDENCE_BYTES)
            row = import_row()
            paths = {
                "json": root / "truth.json",
                "jsonl": root / "truth.jsonl",
                "csv": root / "truth.csv",
            }
            paths["json"].write_text(json.dumps([row]), encoding="utf-8")
            paths["jsonl"].write_text(json.dumps(row) + "\n", encoding="utf-8")
            csv_row = {
                key: (
                    "true"
                    if value is True
                    else "false"
                    if value is False
                    else value
                )
                for key, value in row.items()
            }
            with paths["csv"].open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(csv_row))
                writer.writeheader()
                writer.writerow(csv_row)

            ids = set()
            for kind, path in paths.items():
                with self.subTest(kind=kind):
                    observations = import_truth_observations(
                        path,
                        official_source_allowlist={"stats_nbs_first_release"},
                        official_source_domains=SOURCE_DOMAINS,
                    )
                    self.assertEqual(len(observations), 1)
                    self.assertEqual(observations[0].realized_value, Decimal("0.1"))
                    self.assertFalse(observations[0].evidence_verified)
                    ids.add(observations[0].observation_id)
            self.assertEqual(len(ids), 1)

    def test_import_rejects_unapproved_or_ambiguous_rows(self) -> None:
        cases = (
            (import_row(truth_source="unapproved"), {"stats_nbs_first_release"}),
            (import_row(first_release="true"), {"stats_nbs_first_release"}),
            (import_row(available_at="2025-01-10T10:00:00"), {"stats_nbs_first_release"}),
            (import_row(extra="unexpected"), {"stats_nbs_first_release"}),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "truth.json"
            (Path(directory) / "evidence.bin").write_bytes(EVIDENCE_BYTES)
            for row, allowlist in cases:
                with self.subTest(row=row), self.assertRaises(TruthImportError):
                    path.write_text(json.dumps([row]), encoding="utf-8")
                    import_truth_observations(
                        path,
                        official_source_allowlist=allowlist,
                        official_source_domains=SOURCE_DOMAINS,
                    )


if __name__ == "__main__":
    unittest.main()
