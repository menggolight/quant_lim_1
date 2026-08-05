#!/usr/bin/env python3
"""Run the DeepVan daily personal research pipeline end to end."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.deepvan_capture import CaptureResult, ingest_capture, parse_date
from agent.deepvan_visible_text import write_capture_json
from agent.obsidian_dashboard import write_dashboard
from agent.obsidian_writer import write_daily_note
from agent.portfolio_snapshot import find_latest_snapshot, load_portfolio_context


SCORE_SCRIPT = REPO_ROOT / "skills" / "deepvan-daily-action" / "scripts" / "score_daily_action.py"


@dataclass(frozen=True)
class PipelineResult:
    new_count: int
    capture_path: Path
    signal_path: Path
    action_path: Path
    daily_note_path: Path
    dashboard_path: Path
    state_path: Path
    raw_paths: tuple[Path, ...]
    portfolio_path: Path | None


def load_score_module() -> Any:
    spec = importlib.util.spec_from_file_location("score_daily_action", SCORE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load score script: {SCORE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def score_action(signal_path: Path, action_path: Path, portfolio_path: Path | None = None) -> Path:
    score_module = load_score_module()
    data = dict(score_module.load_input(signal_path))
    if portfolio_path is not None and not data.get("current_weights"):
        context = load_portfolio_context(portfolio_path)
        if context.validation_issues:
            joined = "; ".join(context.validation_issues)
            raise ValueError(f"Portfolio snapshot failed reconciliation: {joined}")
        data["current_weights"] = context.weights
        data["portfolio_snapshot_name"] = portfolio_path.name
        data["portfolio_as_of"] = context.as_of
        data["portfolio_risk_flags"] = list(context.risk_flags)
        data["weight_source"] = "portfolio_snapshot"
    elif data.get("current_weights"):
        data["weight_source"] = "signal_input"
    else:
        data["weight_source"] = "historical_default"
    signals = [score_module.Signal.from_dict(item) for item in data.get("signals", [])]
    weights = score_module.normalize_weights(data)
    scores = score_module.score_signals(signals)
    markdown = score_module.render_markdown(data, scores, weights, signals)
    action_path.parent.mkdir(parents=True, exist_ok=True)
    action_path.write_text(markdown, encoding="utf-8")
    return action_path


def resolve_capture_path(
    workspace_root: Path,
    visible_text_path: Path | None,
    capture_json_path: Path | None,
    captured_at: str | None,
    source_mode: str,
) -> Path:
    if capture_json_path:
        return capture_json_path
    if not visible_text_path:
        raise ValueError("Either visible_text_path or capture_json_path is required")
    if not captured_at:
        raise ValueError("captured_at is required when visible_text_path is used")

    day = parse_date(captured_at, visible_text_path.name)
    output_path = workspace_root / "data" / "inbox" / f"deepvan_capture.{day}.json"
    text = visible_text_path.read_text(encoding="utf-8")
    return write_capture_json(text, output_path, captured_at=captured_at, source_mode=source_mode)


def run_daily_pipeline(
    workspace_root: Path,
    vault_root: Path,
    visible_text_path: Path | None = None,
    capture_json_path: Path | None = None,
    captured_at: str | None = None,
    source_mode: str = "browser_visible_text",
) -> PipelineResult:
    capture_path = resolve_capture_path(
        workspace_root=workspace_root,
        visible_text_path=visible_text_path,
        capture_json_path=capture_json_path,
        captured_at=captured_at,
        source_mode=source_mode,
    )
    capture_result: CaptureResult = ingest_capture(capture_path, workspace_root)
    day = parse_date(capture_result.signal_path.name)
    signal_payload = json.loads(capture_result.signal_path.read_text(encoding="utf-8"))
    decision_time = str(signal_payload.get("captured_at") or day)
    portfolio_path = find_latest_snapshot(workspace_root, decision_time)
    action_path = workspace_root / "data" / "actions" / f"{day}.md"
    score_action(capture_result.signal_path, action_path, portfolio_path=portfolio_path)
    daily_note_path = write_daily_note(action_path, vault_root)
    dashboard_path = write_dashboard(workspace_root, vault_root)

    return PipelineResult(
        new_count=capture_result.new_count,
        capture_path=capture_path,
        signal_path=capture_result.signal_path,
        action_path=action_path,
        daily_note_path=daily_note_path,
        dashboard_path=dashboard_path,
        state_path=capture_result.state_path,
        raw_paths=capture_result.raw_paths,
        portfolio_path=portfolio_path,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run DeepVan daily capture, scoring, and Obsidian sync.")
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--visible-text", help="Path to copied/OCR visible text.")
    input_group.add_argument("--capture-json", help="Path to existing capture JSON.")
    parser.add_argument("--workspace", default=".", help="Quant workspace root.")
    parser.add_argument("--vault", default="obsidian-vault", help="Obsidian vault root.")
    parser.add_argument("--captured-at", help="Required for --visible-text, for example 2026-07-07T09:30:00+08:00.")
    parser.add_argument("--source-mode", default="browser_visible_text", help="Source mode label for visible text.")
    args = parser.parse_args()

    workspace_root = Path(args.workspace)
    vault_root = Path(args.vault)
    if not vault_root.is_absolute():
        vault_root = workspace_root / vault_root

    result = run_daily_pipeline(
        workspace_root=workspace_root,
        vault_root=vault_root,
        visible_text_path=Path(args.visible_text) if args.visible_text else None,
        capture_json_path=Path(args.capture_json) if args.capture_json else None,
        captured_at=args.captured_at,
        source_mode=args.source_mode,
    )

    print(f"New items: {result.new_count}")
    print(f"Capture: {result.capture_path}")
    print(f"Signals: {result.signal_path}")
    print(f"Action: {result.action_path}")
    print(f"Obsidian: {result.daily_note_path}")
    print(f"Dashboard: {result.dashboard_path}")
    print(f"Portfolio: {result.portfolio_path or '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
