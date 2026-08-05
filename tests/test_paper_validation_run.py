import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from trading.paper_run import run_synthetic_validation


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 7, 14, 10, 5, tzinfo=timezone(timedelta(hours=8)))


class PaperValidationRunTest(unittest.TestCase):
    def test_run_is_explicitly_synthetic_paper_only_and_audited(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_synthetic_validation(
                config_path=ROOT / "configs" / "small_account_trading.v1.json",
                output_root=Path(directory),
                decision_time=NOW,
            )
            payload = json.loads(result.json_path.read_text(encoding="utf-8"))

            self.assertTrue(payload["synthetic"])
            self.assertEqual(payload["execution_mode"], "PAPER")
            self.assertFalse(payload["live_orders_submitted"])
            self.assertEqual(payload["research_bridge_check"]["block_code"], "synthetic_signal")
            self.assertEqual(len(payload["plan"]["orders"]), 3)
            self.assertEqual(len(payload["fills"]), 3)
            self.assertGreaterEqual(Decimal(payload["account_after"]["cash"]), Decimal("1000"))
            self.assertEqual(payload["total_estimated_fee"], "15.00")
            self.assertTrue(result.markdown_path.exists())
            self.assertTrue(result.order_store_path.exists())


if __name__ == "__main__":
    unittest.main()
