import json
import tempfile
import unittest
from pathlib import Path

from agent.deepvan_visible_text import build_capture, write_capture_json


VISIBLE_TEXT = """
DeepVan的逃生地牢

2026-07-07 08:58
AI算力扩散观察
算力链继续扩散，但估值和拥挤度需要检查。
https://t.zsxq.com/topic-001
赞 12 评论 3

---

07-07 09:20
QD仓位纪律
海外成长仍强，但现有仓位偏高，新增动作需要更强证据。
展开全部
"""


class DeepVanVisibleTextTest(unittest.TestCase):
    def test_build_capture_extracts_visible_topic_blocks(self):
        capture = build_capture(
            VISIBLE_TEXT,
            captured_at="2026-07-07T09:30:00+08:00",
            source_mode="browser_visible_text",
        )

        self.assertEqual(capture["captured_at"], "2026-07-07T09:30:00+08:00")
        self.assertEqual(capture["source_mode"], "browser_visible_text")
        self.assertEqual(len(capture["items"]), 2)

        first = capture["items"][0]
        self.assertEqual(first["source_id"], "topic-001")
        self.assertEqual(first["published_at"], "2026-07-07T08:58:00+08:00")
        self.assertEqual(first["title"], "AI算力扩散观察")
        self.assertIn("估值和拥挤度", first["summary"])
        self.assertEqual(first["asset"], "qd")
        self.assertEqual(first["direction"], "watch")

        second = capture["items"][1]
        self.assertNotIn("source_id", second)
        self.assertEqual(second["published_at"], "2026-07-07T09:20:00+08:00")
        self.assertEqual(second["title"], "QD仓位纪律")
        self.assertIn("新增动作需要更强证据", second["summary"])
        self.assertNotIn("展开全部", second["summary"])

    def test_write_capture_json_writes_parser_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "capture.json"

            result_path = write_capture_json(
                VISIBLE_TEXT,
                output,
                captured_at="2026-07-07T09:30:00+08:00",
                source_mode="computer_use_visible_text",
            )

            self.assertEqual(result_path, output)
            data = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(data["source_mode"], "computer_use_visible_text")
            self.assertEqual(len(data["items"]), 2)


if __name__ == "__main__":
    unittest.main()
