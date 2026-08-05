#!/usr/bin/env python3
"""Ingest DeepVan visible captures into raw notes and signal JSON.

This module does not log in, scrape behind access controls, or trade. It
accepts content already visible to the user and turns it into a deduplicated
personal research trail.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_STATE_PATH = Path("data/state/deepvan_capture_state.json")
DATE_PATTERN = re.compile(r"(20\d{2}-\d{2}-\d{2})")
SAFE_NAME_PATTERN = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._-]+")


@dataclass(frozen=True)
class CaptureResult:
    new_count: int
    signal_path: Path
    state_path: Path
    raw_paths: tuple[Path, ...]


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def normalize_space(value: Any) -> str:
    return " ".join(str(value or "").split())


def parse_date(value: Any, fallback: str | None = None) -> str:
    text = str(value or fallback or "")
    match = DATE_PATTERN.search(text)
    if match:
        return match.group(1)
    return datetime.now().date().isoformat()


def parse_datetime_sort_key(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text).isoformat()
    except ValueError:
        return text


def topic_key(item: dict[str, Any]) -> str:
    source_id = normalize_space(item.get("source_id"))
    if source_id:
        return f"id:{source_id}"

    url = normalize_space(item.get("url"))
    if url:
        return f"url:{url}"

    fingerprint = "\n".join(
        [
            normalize_space(item.get("published_at")),
            normalize_space(item.get("title")),
            normalize_space(item.get("summary")),
        ]
    )
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16]
    return f"hash:{digest}"


def safe_filename(key: str, title: str) -> str:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]
    cleaned = SAFE_NAME_PATTERN.sub("_", normalize_space(title))[:36].strip("_")
    if cleaned:
        return f"{digest}_{cleaned}.md"
    return f"{digest}.md"


def load_state(state_path: Path) -> dict[str, Any]:
    if not state_path.exists():
        return {"seen_keys": [], "last_seen_at": ""}
    state = load_json(state_path)
    if not isinstance(state.get("seen_keys"), list):
        state["seen_keys"] = []
    state.setdefault("last_seen_at", "")
    return state


def signal_from_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": str(item.get("title") or "未命名DeepVan信号"),
        "asset": str(item.get("asset") or "qd"),
        "direction": str(item.get("direction") or "watch"),
        "strength": int(item.get("strength") or 1),
        "confidence": int(item.get("confidence") or 1),
        "horizon": str(item.get("horizon") or "days"),
        "summary": str(item.get("summary") or ""),
        "evidence": str(item.get("evidence") or ""),
        "counter_evidence": str(item.get("counter_evidence") or ""),
    }


def render_raw_note(item: dict[str, Any], key: str, capture: dict[str, Any]) -> str:
    title = str(item.get("title") or "未命名DeepVan主题")
    source_id = str(item.get("source_id") or "")
    published_at = str(item.get("published_at") or "")
    captured_at = str(capture.get("captured_at") or "")
    source_mode = str(capture.get("source_mode") or "manual_visible_text")
    url = str(item.get("url") or "")
    summary = str(item.get("summary") or "")
    evidence = str(item.get("evidence") or "")

    lines = [
        "---",
        "source: deepvan",
        f"source_mode: {source_mode}",
        f"source_id: {source_id}",
        f"published_at: {published_at}",
        f"captured_at: {captured_at}",
        f"key: {key}",
    ]
    if url:
        lines.append(f"url: {url}")
    lines.extend(
        [
            "---",
            "",
            f"# {title}",
            "",
            "## 摘要",
            "",
            summary or "-",
            "",
            "## 证据",
            "",
            evidence or "-",
            "",
            "## 对抗式检查",
            "",
            str(item.get("counter_evidence") or "-"),
            "",
        ]
    )
    return "\n".join(lines)


def merge_signal_file(signal_path: Path, base_payload: dict[str, Any], new_signals: list[dict[str, Any]]) -> None:
    if signal_path.exists():
        existing = load_json(signal_path)
        signals = list(existing.get("signals") or [])
        existing.update(base_payload)
        existing["signals"] = signals + new_signals
        write_json(signal_path, existing)
        return

    payload = dict(base_payload)
    payload["signals"] = new_signals
    write_json(signal_path, payload)


def ingest_capture(capture_path: Path, workspace_root: Path, state_path: Path | None = None) -> CaptureResult:
    capture = load_json(capture_path)
    items = list(capture.get("items") or [])
    if not isinstance(items, list):
        raise ValueError("capture.items must be a list")

    resolved_state_path = state_path or workspace_root / DEFAULT_STATE_PATH
    state = load_state(resolved_state_path)
    seen_keys = set(str(key) for key in state.get("seen_keys", []))

    capture_date = parse_date(capture.get("captured_at"), capture_path.name)
    signal_path = workspace_root / "data" / "signals" / f"{capture_date}.deepvan.json"
    raw_dir = workspace_root / "data" / "raw" / "deepvan" / capture_date
    raw_dir.mkdir(parents=True, exist_ok=True)

    new_signals: list[dict[str, Any]] = []
    raw_paths: list[Path] = []
    newest_seen_at = str(state.get("last_seen_at") or "")

    for item in items:
        if not isinstance(item, dict):
            continue
        key = topic_key(item)
        if key in seen_keys:
            continue

        title = str(item.get("title") or "未命名DeepVan主题")
        raw_path = raw_dir / safe_filename(key, title)
        raw_path.write_text(render_raw_note(item, key, capture), encoding="utf-8")
        raw_paths.append(raw_path)
        new_signals.append(signal_from_item(item))
        seen_keys.add(key)

        published_at = str(item.get("published_at") or capture.get("captured_at") or "")
        if parse_datetime_sort_key(published_at) >= parse_datetime_sort_key(newest_seen_at):
            newest_seen_at = published_at

    state["seen_keys"] = sorted(seen_keys)
    state["last_seen_at"] = newest_seen_at
    write_json(resolved_state_path, state)

    if new_signals:
        merge_signal_file(
            signal_path,
            {
                "date": capture_date,
                "source_mode": str(capture.get("source_mode") or "manual_visible_text"),
                "captured_at": str(capture.get("captured_at") or ""),
            },
            new_signals,
        )
    elif not signal_path.exists():
        write_json(
            signal_path,
            {
                "date": capture_date,
                "source_mode": str(capture.get("source_mode") or "manual_visible_text"),
                "captured_at": str(capture.get("captured_at") or ""),
                "signals": [],
            },
        )

    return CaptureResult(
        new_count=len(new_signals),
        signal_path=signal_path,
        state_path=resolved_state_path,
        raw_paths=tuple(raw_paths),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest visible DeepVan captures into the quant workspace.")
    parser.add_argument("--input", required=True, help="Path to structured DeepVan capture JSON.")
    parser.add_argument("--workspace", default=".", help="Quant workspace root.")
    parser.add_argument("--state", help="Optional explicit capture state JSON path.")
    args = parser.parse_args()

    result = ingest_capture(
        Path(args.input),
        Path(args.workspace),
        Path(args.state) if args.state else None,
    )
    print(f"New items: {result.new_count}")
    print(f"Signals: {result.signal_path}")
    print(f"State: {result.state_path}")
    for raw_path in result.raw_paths:
        print(f"Raw: {raw_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
