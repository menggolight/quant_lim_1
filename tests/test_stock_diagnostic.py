from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from research.stock_diagnostic import StockDiagnosticError, seal, verify


ROOT = Path(__file__).resolve().parents[1]
DRAFT = ROOT / "configs/stock_diagnostics/innovation_drug_60td.20260818.v1.json"
SCHEMA = ROOT / "schemas/stock_diagnostic_observation.v1.json"


def canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


class StockDiagnosticTests(unittest.TestCase):
    def _seal(self, directory: str) -> tuple[Path, Path]:
        root = Path(directory)
        with patch(
            "research.stock_diagnostic.git_worktree_state",
            return_value=("a" * 40, True, "b" * 64),
        ):
            return seal(
                DRAFT,
                schema_path=SCHEMA,
                signals_dir=root / "signals",
                actions_dir=root / "actions",
                workspace=ROOT,
                now=datetime.fromisoformat("2026-08-18T10:30:00+08:00"),
            )

    def test_standard_cli_card_seals_and_verifies_one_active_case(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            observation_path, manifest_path = self._seal(directory)
            result = verify(observation_path, manifest_path, schema_path=SCHEMA)
            self.assertEqual(result["verification_status"], "verified_diagnostic_only")
            self.assertEqual(result["active_candidate_ids"], ["688235.SH"])
            self.assertFalse(result["paper_eligibility"])
            self.assertFalse(result["trade_eligibility"])
            self.assertEqual(result["live_execution_status"], "live_not_supported")
            observation = json.loads(observation_path.read_text(encoding="utf-8"))
            checks = {item["instrument_id"]: item for item in observation["pre_entry_check"]["checks"]}
            self.assertEqual(checks["688578.SH"]["status"], "pre_entry_gate_failed")
            self.assertEqual(observation["evaluation"]["entry_status"], "pending_official_close_capture")

    def test_failed_pre_entry_case_cannot_be_relabelled_active(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            observation_path, manifest_path = self._seal(directory)
            observation = json.loads(observation_path.read_text(encoding="utf-8"))
            observation["pre_entry_check"]["active_candidate_ids"].append("688578.SH")
            observation["pre_entry_check"]["checks"][1]["status"] = "active_diagnostic_positive"
            raw = canonical_bytes(observation)
            observation_path.write_bytes(raw)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["outputs"][0]["sha256"] = sha256(raw).hexdigest()
            manifest_path.write_bytes(canonical_bytes(manifest))
            with self.assertRaisesRegex(StockDiagnosticError, "pre-entry status"):
                verify(observation_path, manifest_path, schema_path=SCHEMA)

    def test_trade_or_live_elevation_is_rejected_even_if_manifest_is_resigned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            observation_path, manifest_path = self._seal(directory)
            observation = json.loads(observation_path.read_text(encoding="utf-8"))
            observation["safety"]["trade_eligibility"] = True
            raw = canonical_bytes(observation)
            observation_path.write_bytes(raw)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["outputs"][0]["sha256"] = sha256(raw).hexdigest()
            manifest_path.write_bytes(canonical_bytes(manifest))
            with self.assertRaisesRegex(StockDiagnosticError, "safety boundary"):
                verify(observation_path, manifest_path, schema_path=SCHEMA)

    def test_snapshot_gate_failure_cannot_be_promoted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            draft = json.loads(DRAFT.read_text(encoding="utf-8"))
            draft["candidate_pool"][2]["snapshot_status"] = "selected_diagnostic_positive"
            draft_path = Path(directory) / "draft.json"
            draft_path.write_bytes(canonical_bytes(draft))
            with patch(
                "research.stock_diagnostic.git_worktree_state",
                return_value=("a" * 40, True, "b" * 64),
            ):
                with self.assertRaisesRegex(StockDiagnosticError, "snapshot_status"):
                    seal(
                        draft_path,
                        schema_path=SCHEMA,
                        signals_dir=Path(directory) / "signals",
                        actions_dir=Path(directory) / "actions",
                        workspace=ROOT,
                        now=datetime.fromisoformat("2026-08-18T10:30:00+08:00"),
                    )

    def test_future_information_cutoff_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            draft = json.loads(DRAFT.read_text(encoding="utf-8"))
            draft["information_cutoff_at"] = "2026-08-18T10:17:00+08:00"
            draft_path = Path(directory) / "draft.json"
            draft_path.write_bytes(canonical_bytes(draft))
            with patch(
                "research.stock_diagnostic.git_worktree_state",
                return_value=("a" * 40, True, "b" * 64),
            ):
                with self.assertRaisesRegex(StockDiagnosticError, "must precede decision_time"):
                    seal(
                        draft_path,
                        schema_path=SCHEMA,
                        signals_dir=Path(directory) / "signals",
                        actions_dir=Path(directory) / "actions",
                        workspace=ROOT,
                        now=datetime.fromisoformat("2026-08-18T10:30:00+08:00"),
                    )

    def test_existing_sealed_card_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self._seal(directory)
            with self.assertRaisesRegex(StockDiagnosticError, "refusing to overwrite"):
                self._seal(directory)


if __name__ == "__main__":
    unittest.main()
