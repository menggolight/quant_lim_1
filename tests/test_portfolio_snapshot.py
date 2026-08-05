import json
import tempfile
import unittest
from pathlib import Path

from agent.portfolio_snapshot import (
    find_latest_snapshot,
    load_portfolio_context,
    validate_snapshot,
    weights_from_snapshot,
)


def write_snapshot(path: Path, as_of: str, qd_value: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "as_of": as_of,
        "positions": [
            {
                "instrument_id": "000333.SZ",
                "instrument_type": "equity",
                "asset_bucket": "a_share_alpha",
                "market_value": 16080.00,
            },
            {
                "instrument_id": "FUND:QD",
                "instrument_type": "fund",
                "asset_bucket": "qd",
                "market_value": qd_value,
            },
            {
                "instrument_id": "FUND:QUANT",
                "instrument_type": "fund",
                "asset_bucket": "quant",
                "market_value": 1902.91,
            },
            {
                "instrument_id": "FUND:DEFENSE",
                "instrument_type": "fund",
                "asset_bucket": "defense",
                "market_value": 236.65,
            },
        ],
        "cash": 244.84,
        "portfolio_summary": {
            "known_total_assets": 16080.00 + qd_value + 1902.91 + 236.65 + 244.84,
            "bucket_market_values": {
                "a_share_alpha": 16080.00,
                "qd": qd_value,
                "quant": 1902.91,
                "defense": 236.65,
                "cash": 244.84,
            },
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


class PortfolioSnapshotTest(unittest.TestCase):
    def test_find_latest_snapshot_respects_decision_time(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            earlier = root / "data" / "portfolio" / "2026-07-13.v2.json"
            later = root / "data" / "portfolio" / "2026-07-13.v3.json"
            write_snapshot(earlier, "2026-07-13T18:40:00+08:00", 6000.00)
            write_snapshot(later, "2026-07-13T18:46:00+08:00", 6333.24)

            self.assertEqual(
                find_latest_snapshot(root, "2026-07-13T18:45:00+08:00"),
                earlier,
            )
            self.assertEqual(
                find_latest_snapshot(root, "2026-07-13T20:00:00+08:00"),
                later,
            )
            self.assertIsNone(find_latest_snapshot(root, "2026-07-12T20:00:00+08:00"))

    def test_weights_combine_cash_with_defense_and_reconcile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "data" / "portfolio" / "2026-07-13.v3.json"
            write_snapshot(path, "2026-07-13T18:46:00+08:00", 6333.24)

            payload = json.loads(path.read_text(encoding="utf-8"))
            weights = weights_from_snapshot(payload)

            self.assertAlmostEqual(weights["a_share_alpha"], 64.8449, places=3)
            self.assertAlmostEqual(weights["qd"], 25.5397, places=3)
            self.assertAlmostEqual(weights["quant"], 7.6737, places=3)
            self.assertAlmostEqual(weights["defense"], 1.9417, places=3)
            self.assertEqual(validate_snapshot(payload), [])

            context = load_portfolio_context(path)
            self.assertEqual(context.path, path)
            self.assertTrue(any("单只股票" in flag for flag in context.risk_flags))
            self.assertTrue(any("防守与现金" in flag for flag in context.risk_flags))

    def test_validation_reports_a_broken_total(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "data" / "portfolio" / "2026-07-13.json"
            write_snapshot(path, "2026-07-13T18:40:00+08:00", 6000.00)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["portfolio_summary"]["known_total_assets"] += 100.00

            issues = validate_snapshot(payload)

            self.assertTrue(any("总资产" in issue for issue in issues))


if __name__ == "__main__":
    unittest.main()
