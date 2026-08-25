from __future__ import annotations

import inspect
import json
import unittest
from datetime import datetime, timezone

from research.market_data.tushare_capability import (
    TushareCapabilityError,
    canonical_json_bytes,
    canonical_sha256,
)
from research.market_data.tushare_diagnostic_postmortem import (
    CONCLUSION,
    SingleEndpointDiagnosticPostmortemV3,
    TushareDiagnosticPostmortemError,
    build_diagnostic_postmortem_receipt,
    verify_diagnostic_postmortem_receipt,
)
from research.market_data.validation import SchemaValidationError


def _slot(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "tushare-diagnostic-round-budget-slot-v1",
        "slot": 1,
        "endpoint": "trade_cal",
        "diagnostic_run_id": "20260825T071648409093Z",
        "reserved_at": "2026-08-25T15:16:48.409093+08:00",
        "reserved_request_count": 2,
        "maximum_round_request_count": 4,
    }
    value.update(overrides)
    return value


def _marker(slot: dict[str, object], **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "tushare-diagnostic-round-failure-v1",
        "round_status": "closed_after_runner_failure",
        "evidence_origin": "runner_exception_boundary",
        "diagnostic_run_id": slot["diagnostic_run_id"],
        "endpoint": slot["endpoint"],
        "recorded_at": "2026-08-25T15:30:00+08:00",
        "runner_exception_type": "OtherError",
        "failure_window": "after_budget_reservation_before_receipt_publish",
        "budget_slot_sha256": canonical_sha256(slot),
        "failed_diagnostic_code_sha256": "b" * 64,
        "maximum_round_request_count": 4,
        "rerun_permitted": False,
    }
    value.update(overrides)
    return value


def _build(**overrides: object) -> SingleEndpointDiagnosticPostmortemV3:
    slot = overrides.pop("budget_slot", _slot())
    assert isinstance(slot, dict)
    marker = overrides.pop("failure_marker", _marker(slot))
    assert isinstance(marker, dict)
    arguments: dict[str, object] = {
        "budget_slot": slot,
        "budget_slot_sha256": canonical_sha256(slot),
        "failure_marker": marker,
        "failure_marker_sha256": canonical_sha256(marker),
        "recorded_at": datetime(2026, 8, 25, 8, 0, tzinfo=timezone.utc),
        "git_commit": "a" * 40,
        "git_worktree_status": "dirty",
    }
    arguments.update(overrides)
    return build_diagnostic_postmortem_receipt(**arguments)  # type: ignore[arg-type]


class TushareDiagnosticPostmortemTests(unittest.TestCase):
    def test_build_and_verify_canonical_secret_free_receipt(self) -> None:
        receipt = _build()

        self.assertEqual(receipt.actual_request_count, None)
        self.assertEqual(receipt.actual_request_count_lower_bound, 0)
        self.assertEqual(receipt.actual_request_count_upper_bound, 2)
        self.assertEqual(receipt.conclusion, CONCLUSION)
        self.assertEqual([item.channel for item in receipt.channels], ["sdk", "http"])
        for item in receipt.channels:
            self.assertEqual(item.evidence_status, "unavailable")
            self.assertTrue(
                all(
                    value is None
                    for value in (
                        item.request_count,
                        item.transport_status,
                        item.http_status,
                        item.upstream_code,
                        item.sdk_exception_type,
                        item.sanitized_message_category,
                    )
                )
            )

        payload = receipt.to_dict()
        self.assertEqual(payload["status"], "runner_failed_sealed")
        self.assertIsNone(payload["semantic_parameters"])
        self.assertEqual(
            payload["semantic_parameters_evidence_status"], "unavailable"
        )
        self.assertFalse(payload["original_receipt_present"])
        self.assertEqual(payload["original_receipt_scope"], "fixed_round_root")
        self.assertEqual(payload["conclusion"], "capability_probe_bug")
        self.assertEqual(payload["scope"], "diagnostic_runner_integrity_only")
        self.assertEqual(payload["tushare_capability_judgment"], "not_made")
        self.assertFalse(payload["rerun_permitted"])
        for key in (
            "formal_data_admission",
            "next_session_allowed",
            "paper_eligibility",
            "trade_eligibility",
            "real_money_list_allowed",
            "automatic_order_submission",
            "live_supported",
        ):
            self.assertFalse(payload[key])

        encoded = canonical_json_bytes(payload)
        verified = verify_diagnostic_postmortem_receipt(encoded)
        self.assertEqual(verified.receipt_sha256, receipt.receipt_sha256)

    def test_receipt_is_deterministic_and_does_not_mutate_inputs(self) -> None:
        slot = _slot()
        marker = _marker(slot)
        original_slot = dict(slot)
        original_marker = dict(marker)
        first = _build(budget_slot=slot, failure_marker=marker)
        second = _build(budget_slot=slot, failure_marker=marker)
        self.assertEqual(first.receipt_sha256, second.receipt_sha256)
        self.assertEqual(slot, original_slot)
        self.assertEqual(marker, original_marker)

    def test_slot_hash_and_budget_relationships_are_verified(self) -> None:
        with self.assertRaises(TushareDiagnosticPostmortemError):
            _build(budget_slot_sha256="0" * 64)
        with self.assertRaises(TushareDiagnosticPostmortemError):
            _build(budget_slot=_slot(reserved_request_count=1))
        with self.assertRaises(TushareDiagnosticPostmortemError):
            _build(budget_slot=_slot(reserved_request_count=3))
        with self.assertRaises(TushareDiagnosticPostmortemError):
            _build(budget_slot=_slot(slot=2, endpoint="trade_cal"))

    def test_request_or_response_evidence_cannot_be_fabricated(self) -> None:
        value = _build().to_dict()
        value["channels"][0]["request_count"] = 0
        value["channels"][0]["transport_status"] = "not_attempted"
        value["actual_request_count"] = 0
        with self.assertRaises(SchemaValidationError):
            verify_diagnostic_postmortem_receipt(canonical_json_bytes(value))

    def test_hash_tampering_and_noncanonical_json_are_rejected(self) -> None:
        value = _build().to_dict()
        value["git_worktree_status"] = "clean"
        with self.assertRaises(TushareDiagnosticPostmortemError):
            verify_diagnostic_postmortem_receipt(canonical_json_bytes(value))

        pretty = json.dumps(_build().to_dict(), ensure_ascii=False, indent=2)
        with self.assertRaises(TushareCapabilityError):
            verify_diagnostic_postmortem_receipt(pretty)

    def test_only_observed_outer_exception_category_is_accepted(self) -> None:
        slot = _slot()
        with self.assertRaises(TushareDiagnosticPostmortemError):
            _build(failure_marker=_marker(slot, runner_exception_type="RuntimeError"))

    def test_failure_marker_hash_and_cross_bindings_are_verified(self) -> None:
        slot = _slot()
        marker = _marker(slot)
        with self.assertRaises(TushareDiagnosticPostmortemError):
            _build(failure_marker_sha256="0" * 64)
        with self.assertRaises(TushareDiagnosticPostmortemError):
            _build(
                budget_slot=slot,
                failure_marker=_marker(slot, diagnostic_run_id="other-run"),
            )
        with self.assertRaises(TushareDiagnosticPostmortemError):
            _build(
                budget_slot=slot,
                failure_marker=_marker(slot, budget_slot_sha256="0" * 64),
            )
        with self.assertRaises(TushareDiagnosticPostmortemError):
            _build(
                budget_slot=slot,
                failure_marker=_marker(
                    slot, recorded_at="2026-08-25T15:00:00+08:00"
                ),
            )

    def test_recorded_at_is_aware_and_not_before_reservation(self) -> None:
        with self.assertRaises(TushareDiagnosticPostmortemError):
            _build(recorded_at=datetime(2026, 8, 25, 8, 0))
        with self.assertRaises(TushareDiagnosticPostmortemError):
            _build(
                recorded_at=datetime(2026, 8, 25, 7, 0, tzinfo=timezone.utc)
            )

    def test_builder_has_no_credential_or_network_interface(self) -> None:
        parameter_names = set(inspect.signature(build_diagnostic_postmortem_receipt).parameters)
        self.assertFalse(
            parameter_names
            & {"token", "credential", "session", "sdk_loader", "url", "output_path"}
        )
        self.assertNotIn("semantic_parameters", parameter_names)


if __name__ == "__main__":
    unittest.main()
