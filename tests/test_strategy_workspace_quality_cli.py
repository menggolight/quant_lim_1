from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timezone
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

from research.strategy_workspace.choice_gate import (
    CapabilityVerification,
    ChoiceCapability,
    ChoiceCapabilityItem,
    ChoiceCapabilityReceipt,
    ChoiceProviderId,
    MembershipBackfillPolicy,
    RevisionPolicy,
    SourcePolicy,
    UniverseCompletionPolicy,
)
from research.strategy_workspace.cli import main


ROOT = Path(__file__).resolve().parents[1]


def _invoke(arguments: list[str]) -> tuple[int, str, str]:
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        result = main(arguments)
    return result, stdout.getvalue(), stderr.getvalue()


class StrategyWorkspaceQualityCliTests(unittest.TestCase):
    def test_quality_status_binds_real_failed_probes_and_returns_blocked(self) -> None:
        capability = ROOT / "data" / "tmp" / "strategy-workspace" / "quality-growth-v1" / "capability"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "current_status.json"
            code, stdout, stderr = _invoke(
                [
                    "quality-status",
                    "--policy",
                    str(ROOT / "configs" / "strategy_quality_growth.v1.json"),
                    "--daily-bar-probe",
                    str(capability / "choice_daily_bar.json"),
                    "--trade-calendar-probe",
                    str(capability / "choice_trade_calendar.json"),
                    "--historical-sector-probe",
                    str(capability / "choice_historical_sector.json"),
                    "--output",
                    str(output),
                ]
            )
            self.assertEqual(code, 2)
            self.assertEqual(stderr, "")
            self.assertIn("blocked_missing_pit_data", stdout)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["formal_status"], "blocked_missing_pit_data")
            self.assertFalse(payload["safety"]["paper_eligibility"])
            self.assertEqual(
                payload["runtime_probes"]["qfq_daily_bar"]["status"],
                "not_configured",
            )

    def test_choice_gate_cannot_promote_an_unverified_typed_receipt(self) -> None:
        observed_at = datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc)
        receipt = ChoiceCapabilityReceipt(
            receipt_id="all-missing",
            generated_at=observed_at,
            coverage_cutoff=date(2026, 8, 18),
            capabilities=tuple(
                ChoiceCapabilityItem(
                    capability=capability,
                    verification=CapabilityVerification.MISSING,
                    provider_id=ChoiceProviderId.CHOICE,
                )
                for capability in ChoiceCapability
            ),
            source_policy=SourcePolicy.SINGLE_SOURCE_ONLY,
            universe_completion_policy=UniverseCompletionPolicy.COMPLETE_FROZEN_UNIVERSE_ONLY,
            membership_backfill_policy=MembershipBackfillPolicy.HISTORICAL_AS_OF_ONLY,
            revision_policy=RevisionPolicy.FIRST_DISCLOSURE_APPEND_ONLY,
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "receipt.json"
            output = Path(directory) / "gate.json"
            source.write_text(
                json.dumps(receipt.to_dict(), ensure_ascii=False), encoding="utf-8"
            )
            code, stdout, stderr = _invoke(
                ["choice-gate", "--receipt", str(source), "--output", str(output)]
            )
            self.assertEqual(code, 2)
            self.assertEqual(stderr, "")
            self.assertIn("blocked_missing_pit_data", stdout)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertFalse(payload["safety"]["paper_eligibility"])
            self.assertFalse(payload["evaluation"]["formal_truth_eligibility"])

    def test_legacy_caller_authored_fallback_is_rejected(self) -> None:
        universe = {
            "schema_version": "strategy-workspace-current-universe-input.v1",
            "as_of": "2026-08-18",
            "source_universe_id": "CSI800_CURRENT",
            "membership_basis": "current_not_pit",
            "membership_receipt_sha256": "a" * 64,
            "membership_content_sha256": "b" * 64,
            "industry_mapping_receipt_sha256": "c" * 64,
            "industry_mapping_content_sha256": "d" * 64,
            "members": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "universe.json"
            output = Path(directory) / "sample.json"
            source.write_text(json.dumps(universe), encoding="utf-8")
            arguments = [
                "fallback-sample",
                "--universe",
                str(source),
                "--output",
                str(output),
            ]
            code, stdout, stderr = _invoke(arguments)
            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")
            self.assertIn("uncontrolled current-universe JSON is disabled", stderr)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
