import tempfile
import unittest
from pathlib import Path

from agent.obsidian_writer import write_daily_note


SAMPLE_ACTION = """# DeepVan Daily Action - 2026-07-07

- 来源模式：manual_summary
- 说明：本文件是个人投研动作建议，不是自动交易指令。

## 今日结论

- QD / 海外：维持（信号支持当前暴露）
- A股量化：观察（等待更多确认）

## 分数

| 模块 | 当前暴露 | 分数 | 状态 | 建议 |
|---|---:|---:|---|---|
| QD / 海外 | 68.0% | 58.0 | 观察 | 维持 |

## 星球信号

| 信号 | 资产 | 方向 | 强度 | 置信度 | 时效 | 摘要 |
|---|---|---|---:|---:|---|---|
| 示例信号 | qd | hold | 3 | 3 | days | 示例摘要 |

## 对抗式审查

- 是否过度解读：否

## 今日禁止动作

- 不追高

## 明日反证

- 如果纳指走弱，降低QD动作强度。
"""


class ObsidianWriterTest(unittest.TestCase):
    def test_write_daily_note_creates_properties_and_review_sections(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            action_path = root / "data" / "actions" / "2026-07-07.md"
            action_path.parent.mkdir(parents=True)
            action_path.write_text(SAMPLE_ACTION, encoding="utf-8")

            output_path = write_daily_note(action_path, root / "obsidian-vault")

            self.assertEqual(output_path, root / "obsidian-vault" / "01-Daily" / "2026-07-07.md")
            content = output_path.read_text(encoding="utf-8")
            self.assertIn("date: 2026-07-07", content)
            self.assertIn("tags:", content)
            self.assertIn("quant/daily", content)
            self.assertIn("action_status: pending", content)
            self.assertIn("review_status: open", content)
            self.assertIn("## Agent 输出", content)
            self.assertIn("## 执行记录", content)
            self.assertIn("## 结果复盘", content)
            self.assertIn("DeepVan Daily Action - 2026-07-07", content)

    def test_write_daily_note_preserves_existing_execution_record(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault_root = root / "obsidian-vault"
            existing_path = vault_root / "01-Daily" / "2026-07-07.md"
            existing_path.parent.mkdir(parents=True)
            existing_path.write_text(
                """---
date: 2026-07-07
tags:
  - quant/daily
action_status: executed
review_status: open
---

# 2026-07-07 量化投研日志

## 执行记录

- 是否执行：是
- 实际动作：小幅补防守
- 执行理由：降低QD集中度

## Agent 输出

旧内容
""",
                encoding="utf-8",
            )
            action_path = root / "data" / "actions" / "2026-07-07.md"
            action_path.parent.mkdir(parents=True)
            action_path.write_text(SAMPLE_ACTION, encoding="utf-8")

            output_path = write_daily_note(action_path, vault_root)

            content = output_path.read_text(encoding="utf-8")
            self.assertIn("- 是否执行：是", content)
            self.assertIn("- 实际动作：小幅补防守", content)
            self.assertIn("DeepVan Daily Action - 2026-07-07", content)
            self.assertNotIn("旧内容", content)


if __name__ == "__main__":
    unittest.main()
