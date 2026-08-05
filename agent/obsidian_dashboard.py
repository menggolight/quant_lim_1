#!/usr/bin/env python3
"""Render a lightweight Obsidian dashboard for the quant workflow."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


DASHBOARD_PATH = Path("00-Dashboard") / "量化投研驾驶舱.md"
DATE_PATTERN = re.compile(r"(20\d{2}-\d{2}-\d{2})")


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def latest_file(directory: Path, pattern: str) -> Path | None:
    if not directory.exists():
        return None
    files = sorted((item for item in directory.glob(pattern) if ".sample." not in item.name), key=lambda item: item.name)
    return files[-1] if files else None


def date_from_path(path: Path | None) -> str:
    if not path:
        return "-"
    match = DATE_PATTERN.search(path.name)
    return match.group(1) if match else "-"


def count_open_reviews(vault_root: Path) -> int:
    daily_dir = vault_root / "01-Daily"
    if not daily_dir.exists():
        return 0
    total = 0
    for path in daily_dir.glob("*.md"):
        text = path.read_text(encoding="utf-8")
        if "review_status: open" in text:
            total += 1
    return total


def action_link(action_path: Path | None) -> str:
    if not action_path:
        return "-"
    day = date_from_path(action_path)
    return f"[{day} 动作](../../data/actions/{action_path.name})"


def daily_link(day: str) -> str:
    if day == "-":
        return "-"
    return f"[[{day}]]"


def render_dashboard(workspace_root: Path, vault_root: Path) -> str:
    signal_path = latest_file(workspace_root / "data" / "signals", "*.deepvan.json")
    action_path = latest_file(workspace_root / "data" / "actions", "20*.md")
    state = load_json(workspace_root / "data" / "state" / "deepvan_capture_state.json")
    signal_data = load_json(signal_path) if signal_path else {}

    signal_day = str(signal_data.get("date") or date_from_path(signal_path))
    action_day = date_from_path(action_path)
    signals = list(signal_data.get("signals") or [])
    seen_keys = list(state.get("seen_keys") or [])
    last_seen_at = str(state.get("last_seen_at") or "-")
    captured_at = str(signal_data.get("captured_at") or "-")
    source_mode = str(signal_data.get("source_mode") or "-")
    open_reviews = count_open_reviews(vault_root)

    lines = [
        "---",
        "tags:",
        "  - quant/dashboard",
        "  - deepvan/dashboard",
        "---",
        "",
        "# 量化投研驾驶舱",
        "",
        "## 最新状态",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 最新信号日期 | {signal_day} |",
        f"| 当日信号数 | {len(signals)} |",
        f"| 累计去重主题 | {len(seen_keys)} |",
        f"| 最近可见主题时间 | {last_seen_at} |",
        f"| 最近采集时间 | {captured_at} |",
        f"| 来源模式 | {source_mode} |",
        f"| 最近动作文件 | {action_link(action_path)} |",
        f"| 最近 Obsidian 日志 | {daily_link(action_day)} |",
        f"| 待复盘日志 | {open_reviews} |",
        "",
        "## 今日入口",
        "",
        "- [[当前持仓]]",
        "- [[仓位纪律]]",
        "- [[DeepVan经验规则]]",
        "- [[采集安全边界]]",
        "- [[第一性原理]]",
        "- [[对抗式审查]]",
        "- [[周复盘模板]]",
        "",
        "## 最近每日动作",
        "",
        "```dataview",
        "TABLE date, action_status, review_status",
        "FROM \"01-Daily\"",
        "WHERE contains(tags, \"quant/daily\")",
        "SORT date DESC",
        "LIMIT 10",
        "```",
        "",
        "## 待复盘记录",
        "",
        "```dataview",
        "TABLE date, action_status, review_status",
        "FROM \"01-Daily\"",
        "WHERE review_status != \"closed\"",
        "SORT date DESC",
        "```",
        "",
        "## 使用原则",
        "",
        "- Obsidian 负责看得懂、查得回、复盘得了。",
        "- Codex/agent 负责结构化、打分、写入。",
        "- 交易动作必须由人决定，不自动下单。",
        "- 可视化只读本地结果，不参与采集和下单。",
        "",
    ]
    return "\n".join(lines)


def write_dashboard(workspace_root: Path, vault_root: Path) -> Path:
    output_path = vault_root / DASHBOARD_PATH
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_dashboard(workspace_root, vault_root), encoding="utf-8")
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh the Obsidian quant dashboard.")
    parser.add_argument("--workspace", default=".", help="Quant workspace root.")
    parser.add_argument("--vault", default="obsidian-vault", help="Obsidian vault root.")
    args = parser.parse_args()

    workspace_root = Path(args.workspace)
    vault_root = Path(args.vault)
    if not vault_root.is_absolute():
        vault_root = workspace_root / vault_root

    output_path = write_dashboard(workspace_root, vault_root)
    print(f"Wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
