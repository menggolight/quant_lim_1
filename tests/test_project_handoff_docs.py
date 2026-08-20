from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
STATUS_PATH = ROOT / "docs" / "STATUS.md"
DECISIONS_PATH = ROOT / "docs" / "DECISIONS.md"


class ProjectHandoffDocsTests(unittest.TestCase):
    def test_handoff_files_and_agent_protocol_exist(self) -> None:
        self.assertTrue(STATUS_PATH.is_file())
        self.assertTrue(DECISIONS_PATH.is_file())

        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("## 项目交接协议", agents)
        self.assertIn("docs/STATUS.md", agents)
        self.assertIn("docs/DECISIONS.md", agents)

    def test_status_has_each_required_section_exactly_once(self) -> None:
        status = STATUS_PATH.read_text(encoding="utf-8")
        required = (
            "快照元数据",
            "当前目标",
            "本轮完成",
            "关键变更文件",
            "验证证据",
            "已知问题与阻塞",
            "安全状态",
            "待决策",
            "下一步",
            "建议外部审查范围",
        )
        for title in required:
            with self.subTest(title=title):
                self.assertEqual(status.count(f"## {title}\n"), 1)

        self.assertIn("交接快照，不替代", status)
        self.assertIn("live_not_supported", status)

    def test_decision_log_has_rules_index_and_stable_record_id(self) -> None:
        decisions = DECISIONS_PATH.read_text(encoding="utf-8")
        self.assertEqual(decisions.count("## 使用规则\n"), 1)
        self.assertEqual(decisions.count("## 决策索引\n"), 1)
        self.assertRegex(decisions, re.compile(r"^## D-\d{8}-\d{2} .+$", re.MULTILINE))

    def test_root_and_docs_indexes_link_both_handoff_files(self) -> None:
        root_readme = (ROOT / "README.md").read_text(encoding="utf-8")
        docs_readme = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")

        self.assertIn("[项目交接状态](docs/STATUS.md)", root_readme)
        self.assertIn("[项目决策记录](docs/DECISIONS.md)", root_readme)
        self.assertIn("[项目交接状态](STATUS.md)", docs_readme)
        self.assertIn("[项目决策记录](DECISIONS.md)", docs_readme)


if __name__ == "__main__":
    unittest.main()
