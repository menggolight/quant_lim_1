import json
import tempfile
import unittest
from pathlib import Path

from agent.obsidian_dashboard import write_dashboard


class ObsidianDashboardTest(unittest.TestCase):
    def test_write_dashboard_renders_latest_status_and_links(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "obsidian-vault"

            signal_path = root / "data" / "signals" / "2026-07-08.deepvan.json"
            signal_path.parent.mkdir(parents=True)
            signal_path.write_text(
                json.dumps(
                    {
                        "date": "2026-07-08",
                        "source_mode": "browser_visible_text",
                        "captured_at": "2026-07-08T09:30:00+08:00",
                        "signals": [
                            {"title": "AI算力扩散观察", "asset": "ai_semiconductor"},
                            {"title": "QD仓位纪律", "asset": "qd"},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            state_path = root / "data" / "state" / "deepvan_capture_state.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "seen_keys": ["id:topic-001", "hash:abc"],
                        "last_seen_at": "2026-07-08T09:20:00+08:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            action_path = root / "data" / "actions" / "2026-07-08.md"
            action_path.parent.mkdir(parents=True)
            action_path.write_text("# DeepVan Daily Action - 2026-07-08\n", encoding="utf-8")

            daily_path = vault / "01-Daily" / "2026-07-08.md"
            daily_path.parent.mkdir(parents=True)
            daily_path.write_text(
                """---
date: 2026-07-08
review_status: open
---

# 2026-07-08 量化投研日志
""",
                encoding="utf-8",
            )

            output = write_dashboard(root, vault)

            self.assertEqual(output, vault / "00-Dashboard" / "量化投研驾驶舱.md")
            content = output.read_text(encoding="utf-8")
            self.assertIn("最新信号日期 | 2026-07-08", content)
            self.assertIn("当日信号数 | 2", content)
            self.assertIn("累计去重主题 | 2", content)
            self.assertIn("最近可见主题时间 | 2026-07-08T09:20:00+08:00", content)
            self.assertIn("[2026-07-08 动作](../../data/actions/2026-07-08.md)", content)
            self.assertIn("[[2026-07-08]]", content)
            self.assertIn("待复盘日志 | 1", content)
            self.assertIn("```dataview", content)

    def test_write_dashboard_ignores_sample_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "obsidian-vault"

            signal_path = root / "data" / "signals" / "2026-07-03.sample.deepvan.json"
            signal_path.parent.mkdir(parents=True)
            signal_path.write_text(
                json.dumps({"date": "2026-07-03", "signals": [{"title": "样例"}]}, ensure_ascii=False),
                encoding="utf-8",
            )
            action_path = root / "data" / "actions" / "2026-07-03.sample.md"
            action_path.parent.mkdir(parents=True)
            action_path.write_text("# sample\n", encoding="utf-8")

            output = write_dashboard(root, vault)

            content = output.read_text(encoding="utf-8")
            self.assertIn("最新信号日期 | -", content)
            self.assertIn("当日信号数 | 0", content)
            self.assertIn("最近动作文件 | -", content)


if __name__ == "__main__":
    unittest.main()
