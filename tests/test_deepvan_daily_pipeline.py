import tempfile
import unittest
import subprocess
import sys
import json
from pathlib import Path

from agent.deepvan_daily_pipeline import run_daily_pipeline


VISIBLE_TEXT = """
2026-07-07 08:58
AI算力扩散观察
算力链继续扩散，但估值和拥挤度需要检查。
https://t.zsxq.com/topic-001

---

07-07 09:20
QD仓位纪律
海外成长仍强，但现有仓位偏高，新增动作需要更强证据。
"""


class DeepVanDailyPipelineTest(unittest.TestCase):
    def test_pipeline_uses_latest_point_in_time_portfolio_and_blocks_single_stock_add(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            visible_text = root / "visible.txt"
            visible_text.write_text(VISIBLE_TEXT, encoding="utf-8")
            portfolio_path = root / "data" / "portfolio" / "2026-07-07.json"
            portfolio_path.parent.mkdir(parents=True)
            portfolio_path.write_text(
                json.dumps(
                    {
                        "as_of": "2026-07-07T09:00:00+08:00",
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
                                "market_value": 6333.24,
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
                            "known_total_assets": 24797.64,
                            "bucket_market_values": {
                                "a_share_alpha": 16080.00,
                                "qd": 6333.24,
                                "quant": 1902.91,
                                "defense": 236.65,
                                "cash": 244.84,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = run_daily_pipeline(
                workspace_root=root,
                vault_root=root / "obsidian-vault",
                visible_text_path=visible_text,
                captured_at="2026-07-07T09:30:00+08:00",
                source_mode="browser_visible_text",
            )

            self.assertEqual(result.portfolio_path, portfolio_path)
            action = result.action_path.read_text(encoding="utf-8")
            self.assertIn("持仓快照：2026-07-07.json", action)
            self.assertIn("64.8%", action)
            self.assertIn("不新增，优先降集中度", action)

    def test_run_daily_pipeline_from_visible_text_writes_action_and_obsidian_note(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            visible_text = root / "visible.txt"
            visible_text.write_text(VISIBLE_TEXT, encoding="utf-8")

            result = run_daily_pipeline(
                workspace_root=root,
                vault_root=root / "obsidian-vault",
                visible_text_path=visible_text,
                captured_at="2026-07-07T09:30:00+08:00",
                source_mode="browser_visible_text",
            )

            self.assertEqual(result.new_count, 2)
            self.assertEqual(result.capture_path, root / "data" / "inbox" / "deepvan_capture.2026-07-07.json")
            self.assertTrue(result.capture_path.exists())
            self.assertTrue(result.signal_path.exists())
            self.assertTrue(result.action_path.exists())
            self.assertTrue(result.daily_note_path.exists())
            self.assertTrue(result.dashboard_path.exists())

            action = result.action_path.read_text(encoding="utf-8")
            self.assertIn("AI算力扩散观察", action)
            self.assertIn("QD仓位纪律", action)
            self.assertIn("不是自动交易指令", action)

            daily = result.daily_note_path.read_text(encoding="utf-8")
            self.assertIn("## Agent 输出", daily)
            self.assertIn("## 执行记录", daily)

            dashboard = result.dashboard_path.read_text(encoding="utf-8")
            self.assertIn("最新状态", dashboard)
            self.assertIn("当日信号数 | 2", dashboard)

    def test_cli_runs_from_repository_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            visible_text = root / "visible.txt"
            visible_text.write_text(VISIBLE_TEXT, encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    "agent/deepvan_daily_pipeline.py",
                    "--visible-text",
                    str(visible_text),
                    "--workspace",
                    str(root),
                    "--vault",
                    "obsidian-vault",
                    "--captured-at",
                    "2026-07-07T09:30:00+08:00",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                capture_output=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("New items: 2", completed.stdout)
            self.assertTrue((root / "obsidian-vault" / "01-Daily" / "2026-07-07.md").exists())


if __name__ == "__main__":
    unittest.main()
