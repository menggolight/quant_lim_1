#!/usr/bin/env python3
"""Convert visible DeepVan text into the standard capture JSON format."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


FULL_DATETIME_PATTERN = re.compile(r"^(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\s+(\d{1,2}):(\d{2})$")
MONTH_DAY_TIME_PATTERN = re.compile(r"^(\d{1,2})[-/](\d{1,2})\s+(\d{1,2}):(\d{2})$")
URL_PATTERN = re.compile(r"https?://\S+")
TOPIC_ID_PATTERN = re.compile(r"(?:topic[-_/=]?|topics/)([A-Za-z0-9_-]+)", re.IGNORECASE)
UI_NOISE_EXACT = {
    "DeepVan的逃生地牢",
    "展开全部",
    "收起",
    "赞",
    "评论",
    "收藏",
    "分享",
}
UI_NOISE_PREFIX = ("赞 ", "评论 ", "收藏 ", "来自 ", "发布于 ")


def normalize_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = " ".join(raw_line.strip().split())
        if not line:
            continue
        if line == "---":
            lines.append(line)
            continue
        if line in UI_NOISE_EXACT:
            continue
        if any(line.startswith(prefix) for prefix in UI_NOISE_PREFIX):
            continue
        lines.append(line)
    return lines


def captured_year(captured_at: str) -> int:
    match = re.search(r"20\d{2}", captured_at)
    if match:
        return int(match.group(0))
    return datetime.now().year


def parse_published_at(line: str, captured_at: str) -> str | None:
    full = FULL_DATETIME_PATTERN.match(line)
    if full:
        year, month, day, hour, minute = [int(part) for part in full.groups()]
        return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:00+08:00"

    partial = MONTH_DAY_TIME_PATTERN.match(line)
    if partial:
        month, day, hour, minute = [int(part) for part in partial.groups()]
        year = captured_year(captured_at)
        return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:00+08:00"

    return None


def split_blocks(lines: list[str], captured_at: str) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []

    for line in lines:
        if line == "---":
            if current:
                blocks.append(current)
                current = []
            continue

        if parse_published_at(line, captured_at) and current:
            blocks.append(current)
            current = [line]
            continue

        current.append(line)

    if current:
        blocks.append(current)

    return [block for block in blocks if any(parse_published_at(line, captured_at) for line in block)]


def source_id_from_url(url: str) -> str | None:
    path_tail = url.rstrip("/").split("/")[-1]
    if path_tail and path_tail.startswith("topic"):
        return path_tail
    match = TOPIC_ID_PATTERN.search(url)
    if match:
        return match.group(1)
    return None


def item_from_block(block: list[str], captured_at: str) -> dict[str, Any] | None:
    published_at = ""
    content_lines: list[str] = []
    urls: list[str] = []

    for line in block:
        parsed = parse_published_at(line, captured_at)
        if parsed and not published_at:
            published_at = parsed
            continue
        found_urls = URL_PATTERN.findall(line)
        if found_urls:
            urls.extend(found_urls)
            line = URL_PATTERN.sub("", line).strip()
            if not line:
                continue
        if line in UI_NOISE_EXACT or any(line.startswith(prefix) for prefix in UI_NOISE_PREFIX):
            continue
        content_lines.append(line)

    if not published_at or not content_lines:
        return None

    title = content_lines[0]
    summary = "\n".join(content_lines[1:]).strip()
    if not summary:
        summary = title

    item: dict[str, Any] = {
        "published_at": published_at,
        "title": title,
        "summary": summary,
        "asset": "qd",
        "direction": "watch",
        "strength": 1,
        "confidence": 1,
        "horizon": "days",
        "evidence": "来自用户账号可见页面的非逐字摘要或可见文本。",
        "counter_evidence": "若市场走势或后续信息不确认该信号，则不放大动作。",
    }

    if urls:
        item["url"] = urls[0]
        source_id = source_id_from_url(urls[0])
        if source_id:
            item["source_id"] = source_id

    return item


def build_capture(text: str, captured_at: str, source_mode: str = "browser_visible_text") -> dict[str, Any]:
    lines = normalize_lines(text)
    items = [
        item
        for item in (item_from_block(block, captured_at) for block in split_blocks(lines, captured_at))
        if item is not None
    ]
    return {
        "captured_at": captured_at,
        "source_mode": source_mode,
        "items": items,
    }


def write_capture_json(text: str, output_path: Path, captured_at: str, source_mode: str) -> Path:
    capture = build_capture(text, captured_at=captured_at, source_mode=source_mode)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(capture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert visible DeepVan text to capture JSON.")
    parser.add_argument("--input", required=True, help="Path to copied/OCR visible text.")
    parser.add_argument("--output", required=True, help="Path to write capture JSON.")
    parser.add_argument("--captured-at", required=True, help="Capture time, for example 2026-07-07T09:30:00+08:00.")
    parser.add_argument("--source-mode", default="browser_visible_text", help="Source mode label.")
    args = parser.parse_args()

    text = Path(args.input).read_text(encoding="utf-8")
    output_path = write_capture_json(text, Path(args.output), args.captured_at, args.source_mode)
    print(f"Wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
