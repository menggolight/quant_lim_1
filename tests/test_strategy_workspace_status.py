from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from research.strategy_workspace.policy import load_quality_growth_policy
from research.strategy_workspace.status import StatusArtifactError, build_current_status


class StrategyWorkspaceStatusTests(unittest.TestCase):
    @staticmethod
    def _write(path: Path, payload: dict[str, object]) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_real_probe_shapes_remain_formal_and_fallback_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            daily = root / "daily.json"
            calendar = root / "calendar.json"
            sector = root / "sector.json"
            common = {
                "probe_version": "market-data-probe-v1",
                "provider_id": "choice",
                "adapter_version": "choice-emquantapi-adapter-v2",
                "evidence_mode": "real_provider",
                "status": "not_configured",
                "error_code": "not_configured",
                "record_count": 0,
                "request_fingerprint": "a" * 64,
                "admission_status": "failed",
                "point_in_time_status": "not_admitted",
            }
            self._write(daily, {**common, "dataset_type": "daily_bar", "adjustment": "qfq"})
            self._write(calendar, {**common, "dataset_type": "trade_calendar", "adjustment": "none"})
            self._write(
                sector,
                {
                    "probe_version": "choice-candidate-probe-v1",
                    "provider_id": "choice",
                    "adapter_version": "choice-emquantapi-adapter-v2",
                    "query_type": "historical_sector_membership",
                    "mode": "online",
                    "status": "not_configured",
                    "record_count": 0,
                    "request_fingerprint": "b" * 64,
                    "admission_status": "diagnostic_current_only",
                    "point_in_time_status": "diagnostic_current_only",
                    "formal_truth_eligible": False,
                    "issues": [{"code": "not_configured"}],
                },
            )
            result = build_current_status(
                load_quality_growth_policy(),
                daily_bar_probe=daily,
                trade_calendar_probe=calendar,
                historical_sector_probe=sector,
            ).to_dict()

            self.assertEqual(result["formal_status"], "blocked_missing_pit_data")
            self.assertIn("first_disclosure_financials", result["controlled_adapter_missing_capabilities"])
            self.assertIn(
                "daily_pit_nav_and_drawdown_marks",
                result["forward_paper_missing_capabilities"],
            )
            self.assertTrue(
                result["interpretation"]["append_only_paper_accounting_ledger_implemented"]
            )
            self.assertFalse(
                result["interpretation"]["manual_real_money_candidate_reachable"]
            )
            self.assertFalse(result["safety"]["paper_eligibility"])
            self.assertRegex(result["status_sha256"], r"^[0-9a-f]{64}$")

    def test_synthetic_or_wrong_provider_probe_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bad = root / "bad.json"
            self._write(
                bad,
                {
                    "probe_version": "market-data-probe-v1",
                    "provider_id": "baostock",
                    "dataset_type": "daily_bar",
                    "evidence_mode": "synthetic",
                    "adjustment": "qfq",
                    "status": "passed",
                },
            )
            with self.assertRaises(StatusArtifactError):
                build_current_status(
                    load_quality_growth_policy(),
                    daily_bar_probe=bad,
                    trade_calendar_probe=bad,
                    historical_sector_probe=bad,
                )


if __name__ == "__main__":
    unittest.main()
