import json
import tempfile
import unittest
from pathlib import Path

from agent.deepvan_capture import ingest_capture


class DeepVanCaptureTest(unittest.TestCase):
    def test_ingest_capture_writes_only_new_items_and_updates_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            capture_path = root / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "captured_at": "2026-07-07T09:10:00+08:00",
                        "source_mode": "browser_visible",
                        "items": [
                            {
                                "source_id": "topic-old",
                                "published_at": "2026-07-07T08:30:00+08:00",
                                "title": "旧主题",
                                "summary": "已经处理过的内容",
                                "asset": "qd",
                            },
                            {
                                "source_id": "topic-new",
                                "published_at": "2026-07-07T09:00:00+08:00",
                                "title": "AI算力扩散",
                                "summary": "算力链继续扩散，但需要检查估值和拥挤度。",
                                "asset": "ai_semiconductor",
                                "direction": "watch",
                                "strength": 3,
                                "confidence": 2,
                                "horizon": "days",
                                "counter_evidence": "如果半导体冲高回落，降低动作强度。",
                            },
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
                        "seen_keys": ["id:topic-old"],
                        "last_seen_at": "2026-07-07T08:30:00+08:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = ingest_capture(capture_path, root)

            self.assertEqual(result.new_count, 1)
            self.assertEqual(result.signal_path, root / "data" / "signals" / "2026-07-07.deepvan.json")
            raw_files = list((root / "data" / "raw" / "deepvan" / "2026-07-07").glob("*.md"))
            self.assertEqual(len(raw_files), 1)
            raw_content = raw_files[0].read_text(encoding="utf-8")
            self.assertIn("AI算力扩散", raw_content)
            self.assertIn("source_id: topic-new", raw_content)

            signal_data = json.loads(result.signal_path.read_text(encoding="utf-8"))
            self.assertEqual(signal_data["date"], "2026-07-07")
            self.assertEqual(signal_data["source_mode"], "browser_visible")
            self.assertEqual(len(signal_data["signals"]), 1)
            self.assertEqual(signal_data["signals"][0]["asset"], "ai_semiconductor")
            self.assertEqual(signal_data["signals"][0]["counter_evidence"], "如果半导体冲高回落，降低动作强度。")

            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIn("id:topic-old", state["seen_keys"])
            self.assertIn("id:topic-new", state["seen_keys"])
            self.assertEqual(state["last_seen_at"], "2026-07-07T09:00:00+08:00")

    def test_ingest_capture_deduplicates_items_without_source_id_by_content_hash(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            capture_path = root / "capture.json"
            payload = {
                "captured_at": "2026-07-07T21:30:00+08:00",
                "source_mode": "manual_visible_text",
                "items": [
                    {
                        "published_at": "2026-07-07T21:00:00+08:00",
                        "title": "QD仓位纪律",
                        "summary": "海外成长仍强，但现有仓位偏高，新增动作需要更强证据。",
                    }
                ],
            }
            capture_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            first = ingest_capture(capture_path, root)
            second = ingest_capture(capture_path, root)

            self.assertEqual(first.new_count, 1)
            self.assertEqual(second.new_count, 0)
            raw_files = list((root / "data" / "raw" / "deepvan" / "2026-07-07").glob("*.md"))
            self.assertEqual(len(raw_files), 1)
            signal_data = json.loads(first.signal_path.read_text(encoding="utf-8"))
            self.assertEqual(len(signal_data["signals"]), 1)


if __name__ == "__main__":
    unittest.main()
