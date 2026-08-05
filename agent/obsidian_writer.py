#!/usr/bin/env python3
"""Write Quant Agent action memos into an Obsidian vault."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


DATE_PATTERN = re.compile(r"(20\d{2}-\d{2}-\d{2})")


def extract_date(text: str, fallback_name: str) -> str:
    """Extract an ISO date from memo text or filename."""
    match = DATE_PATTERN.search(text)
    if match:
        return match.group(1)
    match = DATE_PATTERN.search(fallback_name)
    if match:
        return match.group(1)
    raise ValueError("Could not find YYYY-MM-DD date in action memo")


def extract_section(content: str, heading: str) -> str:
    """Return a markdown section by h2 heading, without the heading line."""
    marker = f"## {heading}"
    start = content.find(marker)
    if start == -1:
        return ""
    body_start = content.find("\n", start)
    if body_start == -1:
        return ""
    next_heading = content.find("\n## ", body_start + 1)
    if next_heading == -1:
        return content[body_start + 1 :].strip()
    return content[body_start + 1 : next_heading].strip()


def default_execution_record() -> str:
    return """## 执行记录

- 是否执行：待定
- 实际动作：
- 执行时间：
- 执行理由：
- 没有执行的原因：
"""


def default_review_section() -> str:
    return """## 结果复盘

- 1日结果：
- 3日结果：
- 7日结果：
- 30日结果：
- 判断是否正确：
- 错误来源：
- 规则更新：
"""


def existing_section(content: str, heading: str, default: str) -> str:
    body = extract_section(content, heading)
    if not body:
        return default
    return f"## {heading}\n\n{body.strip()}\n"


def render_daily_note(date_text: str, action_memo: str, existing_content: str | None = None) -> str:
    execution = existing_section(existing_content or "", "执行记录", default_execution_record())
    review = existing_section(existing_content or "", "结果复盘", default_review_section())

    return f"""---
date: {date_text}
tags:
  - quant/daily
  - deepvan/action
action_status: pending
review_status: open
source: deepvan-daily-action
---

# {date_text} 量化投研日志

[[量化投研驾驶舱]] | [[当前持仓]] | [[仓位纪律]] | [[对抗式审查]]

## Agent 输出

{action_memo.strip()}

{execution.strip()}

{review.strip()}

## 人工备注

-
"""


def write_daily_note(action_path: Path, vault_root: Path) -> Path:
    """Create or update an Obsidian daily note from an action memo."""
    action_memo = action_path.read_text(encoding="utf-8")
    date_text = extract_date(action_memo, action_path.name)
    daily_dir = vault_root / "01-Daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    output_path = daily_dir / f"{date_text}.md"
    existing = output_path.read_text(encoding="utf-8") if output_path.exists() else None
    output_path.write_text(render_daily_note(date_text, action_memo, existing), encoding="utf-8")
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync a Quant Agent action memo into Obsidian.")
    parser.add_argument("--action", required=True, help="Path to data/actions/YYYY-MM-DD.md")
    parser.add_argument("--vault", required=True, help="Path to Obsidian vault root")
    args = parser.parse_args()
    output = write_daily_note(Path(args.action), Path(args.vault))
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
