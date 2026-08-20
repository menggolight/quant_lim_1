from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.factor_evidence_probe import (
    CHOICE_CAPTURE_INDEX_IDS,
    CHOICE_RECONCILIATION_INDEX_IDS,
    CHOICE_SCREEN_INDEX_IDS,
    CSI_CONFIRM_INDEX_IDS,
    FactorEvidenceProbeError,
    ProbeQuery,
    SOURCE_POLICIES,
    build_parser,
    main,
    run_probe,
)
from research.market_data.providers.base import ProviderQuotaExceededError
from research.market_data.contracts import canonical_json_bytes, sha256_bytes


TZ = timezone(timedelta(hours=8))
REQUESTED_AT = datetime(2026, 8, 13, 9, 0, tzinfo=TZ)
FETCHED_AT = datetime(2026, 8, 13, 9, 1, tzinfo=TZ)


class FakeProvider:
    def __init__(
        self,
        payload: object,
        *,
        provider_id: str = "choice_index",
        adapter_version: str = "choice-index-v1",
    ) -> None:
        self.payload = payload
        self.provider_id = provider_id
        self.adapter_version = adapter_version
        self.requests: list[object] = []

    def fetch(self, request: object) -> object:
        self.requests.append(request)
        return self.payload


def choice_records() -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "schema_version": "index-level-v1",
            "index_id": index_id,
            "trading_date": "2026-08-12",
            "open": None,
            "high": None,
            "low": None,
            "close": str(100 + offset),
            "currency": "CNY",
            "basis": "index_points_unadjusted",
            "available_at": "2026-08-12T16:30:00+08:00",
            "availability_status": "policy_estimated",
            "source_record_id": f"choice:{index_id}:2026-08-12",
        }
        for offset, index_id in enumerate(sorted(CHOICE_CAPTURE_INDEX_IDS))
    )


def make_payload(records: tuple[dict[str, object], ...] | None = None) -> object:
    values = records or choice_records()
    return SimpleNamespace(
        raw_content=canonical_json_bytes(
            {"source": "Choice", "records": [dict(item) for item in values]}
        ),
        records=values,
        fetched_at=FETCHED_AT,
        upstream_source="Choice index quote interface",
        availability_status="observed_at_capture",
        point_in_time_status="current_capture_only",
    )


class FactorEvidenceProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repository = Path(self.temp.name) / "repo"
        self.repository.mkdir()
        self.output = self.repository / "data" / "factor_evidence"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def query(
        self,
        *,
        mode: str = "online",
        cutoff: datetime | None = None,
    ) -> ProbeQuery:
        return ProbeQuery(
            source="choice",
            mode=mode,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 8, 12),
            requested_at=REQUESTED_AT if mode == "online" else REQUESTED_AT + timedelta(days=1),
            evidence_cutoff_at=cutoff,
        )

    def capture(self) -> tuple[dict[str, object], FakeProvider]:
        provider = FakeProvider(make_payload())
        request_marker = object()
        result = run_probe(
            self.query(),
            self.output,
            provider_loader=lambda source: provider,
            request_factory=lambda query: request_marker,
            repository_root=self.repository,
        )
        self.assertEqual(provider.requests, [request_marker])
        return result, provider

    def test_fixed_choice_capture_has_screen_reconciliation_and_one_benchmark(self) -> None:
        self.assertEqual(len(CHOICE_SCREEN_INDEX_IDS), 12)
        self.assertEqual(len(CHOICE_RECONCILIATION_INDEX_IDS), 12)
        self.assertEqual(len(CHOICE_CAPTURE_INDEX_IDS), 23)
        self.assertEqual(len(CSI_CONFIRM_INDEX_IDS), 12)
        self.assertEqual(CHOICE_SCREEN_INDEX_IDS[-1], "000985.CSI")
        self.assertEqual(CSI_CONFIRM_INDEX_IDS[-1], "000985.CSI")
        self.assertEqual(
            set(CHOICE_SCREEN_INDEX_IDS) & set(CSI_CONFIRM_INDEX_IDS),
            {"000985.CSI"},
        )
        self.assertEqual(
            set(CHOICE_CAPTURE_INDEX_IDS),
            set(CHOICE_SCREEN_INDEX_IDS) | set(CHOICE_RECONCILIATION_INDEX_IDS),
        )
        self.assertEqual(SOURCE_POLICIES["sse"].index_ids, ())

    def test_online_capture_writes_content_addressed_four_layer_bundle(self) -> None:
        result, _ = self.capture()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["research_admission_status"], "not_admitted_probe_only")
        for field in (
            "raw_path",
            "normalized_path",
            "receipt_path",
            "checkpoint_path",
        ):
            self.assertTrue((self.output / str(result[field])).is_file(), field)
        raw_path = self.output / str(result["raw_path"])
        normalized_path = self.output / str(result["normalized_path"])
        receipt_path = self.output / str(result["receipt_path"])
        checkpoint_path = self.output / str(result["checkpoint_path"])
        self.assertEqual(sha256_bytes(raw_path.read_bytes()), result["raw_content_sha256"])
        self.assertEqual(
            sha256_bytes(normalized_path.read_bytes()),
            result["normalized_content_sha256"],
        )
        self.assertEqual(sha256_bytes(receipt_path.read_bytes()), result["receipt_sha256"])
        self.assertEqual(
            sha256_bytes(checkpoint_path.read_bytes()), result["checkpoint_sha256"]
        )
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["provider_id"], "choice_index")
        self.assertEqual(receipt["request"]["index_ids"], list(CHOICE_CAPTURE_INDEX_IDS))
        self.assertEqual(receipt["screen_index_ids"], list(CHOICE_SCREEN_INDEX_IDS))
        self.assertEqual(
            receipt["reconciliation_index_ids"],
            list(CHOICE_RECONCILIATION_INDEX_IDS),
        )
        self.assertFalse(receipt["formal_truth_eligible"])
        normalized = json.loads(normalized_path.read_text(encoding="utf-8"))
        self.assertEqual(len(normalized), 23)

    def test_online_capture_is_idempotent_for_identical_evidence(self) -> None:
        first, _ = self.capture()
        second, _ = self.capture()
        self.assertEqual(first["receipt_sha256"], second["receipt_sha256"])
        self.assertEqual(first["checkpoint_sha256"], second["checkpoint_sha256"])

    def test_online_capture_persists_optional_provider_transport_receipt(self) -> None:
        payload = make_payload()
        payload.transport_receipt = {  # type: ignore[attr-defined]
            "receipt_version": "choice-transport-v1",
            "final_url_host": "choice.example.invalid",
            "response_header_sha256": "a" * 64,
        }
        provider = FakeProvider(payload)
        result = run_probe(
            self.query(),
            self.output,
            provider_loader=lambda source: provider,
            request_factory=lambda query: object(),
            repository_root=self.repository,
        )
        self.assertEqual(result["transport_receipt_status"], "present")
        transport_path = self.output / str(result["transport_receipt_path"])
        self.assertEqual(
            sha256_bytes(transport_path.read_bytes()),
            result["transport_receipt_sha256"],
        )
        receipt = json.loads(
            (self.output / str(result["receipt_path"])).read_text(encoding="utf-8")
        )
        self.assertEqual(
            receipt["transport_receipt_sha256"],
            result["transport_receipt_sha256"],
        )

    def test_offline_verification_never_loads_provider_or_request_contract(self) -> None:
        online, _ = self.capture()

        def forbidden(_: object) -> object:
            self.fail("offline mode must not load a provider or request contract")

        offline = run_probe(
            self.query(
                mode="offline",
                cutoff=FETCHED_AT + timedelta(minutes=1),
            ),
            self.output,
            provider_loader=forbidden,
            request_factory=forbidden,
            repository_root=self.repository,
            allow_test_evidence=True,
        )
        self.assertEqual(offline["status"], "passed")
        self.assertEqual(
            offline["verified_online_receipt_sha256"], online["receipt_sha256"]
        )
        self.assertNotEqual(offline["receipt_sha256"], online["receipt_sha256"])

    def test_offline_rejects_future_capture_at_explicit_cutoff(self) -> None:
        self.capture()
        with self.assertRaisesRegex(FactorEvidenceProbeError, "cache miss"):
            run_probe(
                self.query(
                    mode="offline",
                    cutoff=FETCHED_AT - timedelta(seconds=1),
                ),
                self.output,
                repository_root=self.repository,
                allow_test_evidence=True,
            )

    def test_offline_detects_raw_tampering(self) -> None:
        online, _ = self.capture()
        (self.output / str(online["raw_path"])).write_bytes(b"tampered")
        with self.assertRaisesRegex(FactorEvidenceProbeError, "raw evidence"):
            run_probe(
                self.query(
                    mode="offline",
                    cutoff=FETCHED_AT + timedelta(minutes=1),
                ),
                self.output,
                repository_root=self.repository,
                allow_test_evidence=True,
            )

    def test_offline_default_refuses_test_injected_capture(self) -> None:
        self.capture()
        with self.assertRaisesRegex(FactorEvidenceProbeError, "cache miss"):
            run_probe(
                self.query(
                    mode="offline",
                    cutoff=FETCHED_AT + timedelta(minutes=1),
                ),
                self.output,
                repository_root=self.repository,
            )

    def test_online_rejects_incomplete_or_non_whitelisted_index_set(self) -> None:
        incomplete = choice_records()[:-1]
        provider = FakeProvider(make_payload(incomplete))
        with self.assertRaisesRegex(FactorEvidenceProbeError, "complete fixed"):
            run_probe(
                self.query(),
                self.output,
                provider_loader=lambda source: provider,
                request_factory=lambda query: object(),
                repository_root=self.repository,
            )

    def test_online_rejects_payload_without_source_owned_raw_bytes(self) -> None:
        provider = FakeProvider(
            SimpleNamespace(
                raw_content=b"",
                records=choice_records(),
                fetched_at=FETCHED_AT,
                upstream_source="Choice",
            )
        )
        with self.assertRaisesRegex(FactorEvidenceProbeError, "raw_content"):
            run_probe(
                self.query(),
                self.output,
                provider_loader=lambda source: provider,
                request_factory=lambda query: object(),
                repository_root=self.repository,
            )

    def test_sse_calendar_capture_requires_complete_natural_date_range(self) -> None:
        records = tuple(
            {
                "schema_version": "cn-equity-session-v1",
                "calendar_date": value,
                "is_trading_day": value == "2026-08-13",
                "session_open_at": f"{value}T09:30:00+08:00" if value == "2026-08-13" else None,
                "session_close_at": f"{value}T15:00:00+08:00" if value == "2026-08-13" else None,
                "available_at": "2026-08-12T18:00:00+08:00",
                "availability_status": "policy_estimated",
                "source_record_id": f"sse:{value}",
            }
            for value in ("2026-08-12", "2026-08-13")
        )
        payload = SimpleNamespace(
            raw_content=canonical_json_bytes({"sessions": records}),
            records=records,
            fetched_at=FETCHED_AT,
            upstream_source="SSE official calendar",
            availability_status="policy_estimated",
            point_in_time_status="known_as_captured",
        )
        provider = FakeProvider(
            payload,
            provider_id="sse_calendar",
            adapter_version="sse-calendar-v1",
        )
        query = ProbeQuery(
            source="sse",
            mode="online",
            start_date=date(2026, 8, 12),
            end_date=date(2026, 8, 13),
            requested_at=REQUESTED_AT,
        )
        result = run_probe(
            query,
            self.output,
            provider_loader=lambda source: provider,
            request_factory=lambda value: object(),
            repository_root=self.repository,
        )
        self.assertEqual(result["record_count"], 2)
        self.assertEqual(result["index_ids"], [])

        incomplete_provider = FakeProvider(
            SimpleNamespace(**{**payload.__dict__, "records": records[:-1]}),
            provider_id="sse_calendar",
            adapter_version="sse-calendar-v1",
        )
        with self.assertRaisesRegex(FactorEvidenceProbeError, "every natural date"):
            run_probe(
                query,
                self.repository / "data" / "incomplete_calendar",
                provider_loader=lambda source: incomplete_provider,
                request_factory=lambda value: object(),
                repository_root=self.repository,
            )

    def test_csi_confirmation_capture_uses_only_current_twelve_ids(self) -> None:
        records = tuple(
            {
                "schema_version": "index-level-v1",
                "index_id": index_id,
                "trading_date": "2026-08-12",
                "open": "99.0",
                "high": "101.0",
                "low": "98.0",
                "close": "100.0",
                "currency": "CNY",
                "basis": "index_points_unadjusted",
                "available_at": "2026-08-12T18:00:00+08:00",
                "availability_status": "known",
                "source_record_id": f"csi:{index_id}:2026-08-12",
            }
            for index_id in sorted(CSI_CONFIRM_INDEX_IDS)
        )
        payload = SimpleNamespace(
            raw_content=canonical_json_bytes({"records": records}),
            records=records,
            fetched_at=FETCHED_AT,
            upstream_source="CSI official index file",
            availability_status="known",
            point_in_time_status="known_as_captured",
        )
        provider = FakeProvider(
            payload,
            provider_id="csi_official",
            adapter_version="csi-official-v1",
        )
        query = ProbeQuery(
            source="csi",
            mode="online",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 8, 12),
            requested_at=REQUESTED_AT,
        )
        result = run_probe(
            query,
            self.output,
            provider_loader=lambda source: provider,
            request_factory=lambda value: object(),
            repository_root=self.repository,
        )
        self.assertEqual(result["index_ids"], list(CSI_CONFIRM_INDEX_IDS))
        receipt = json.loads(
            (self.output / str(result["receipt_path"])).read_text(encoding="utf-8")
        )
        self.assertEqual(receipt["screen_index_ids"], [])
        self.assertEqual(receipt["reconciliation_index_ids"], [])

    def test_online_rejects_credential_shaped_structured_fields_before_write(self) -> None:
        records = list(choice_records())
        records[0] = {**records[0], "access_token": "must-not-be-written"}
        provider = FakeProvider(make_payload(tuple(records)))
        with self.assertRaisesRegex(FactorEvidenceProbeError, "credential-shaped"):
            run_probe(
                self.query(),
                self.output,
                provider_loader=lambda source: provider,
                request_factory=lambda query: object(),
                repository_root=self.repository,
            )
        self.assertFalse(self.output.exists())

    def test_output_root_must_remain_in_repository(self) -> None:
        outside = Path(self.temp.name) / "outside"
        with self.assertRaisesRegex(FactorEvidenceProbeError, "inside the repository"):
            run_probe(
                self.query(),
                outside,
                provider_loader=lambda source: FakeProvider(make_payload()),
                request_factory=lambda query: object(),
                repository_root=self.repository,
            )

    def test_offline_query_requires_explicit_cutoff(self) -> None:
        with self.assertRaisesRegex(FactorEvidenceProbeError, "requires an explicit"):
            self.query(mode="offline")

    def test_cli_requires_source_mode_dates_and_output_root(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([])

    def test_cli_provider_failure_persists_redacted_checkpoint(self) -> None:
        argv = [
            "--source",
            "choice",
            "--mode",
            "online",
            "--start-date",
            "2026-08-12",
            "--end-date",
            "2026-08-12",
            "--requested-at",
            "2026-08-13T09:00:00+08:00",
            "--output-root",
            str(self.output),
        ]
        with patch(
            "agent.factor_evidence_probe.run_probe",
            side_effect=ProviderQuotaExceededError("data limit exceeded"),
        ), patch("agent.factor_evidence_probe.REPOSITORY_ROOT", self.repository):
            self.assertEqual(main(argv), 1)
        checkpoints = list((self.output / "checkpoints").glob("**/*.json"))
        self.assertEqual(len(checkpoints), 1)
        checkpoint = json.loads(checkpoints[0].read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["status"], "failed")
        self.assertEqual(checkpoint["failure_code"], "quota_exhausted")
        self.assertIsNone(checkpoint["receipt_path"])


if __name__ == "__main__":
    unittest.main()
