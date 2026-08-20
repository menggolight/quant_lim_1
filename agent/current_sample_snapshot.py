"""Collect or verify the fixed current-CSI800 60-name factor snapshot.

This command is diagnostic only.  It cannot accept caller-selected stocks,
dates, fields, benchmarks, rankings, or eligibility flags, and it never emits
orders or a real-money list.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from research.market_data.providers.base import ProviderError, safe_error_text
from research.market_data.providers.choice import ChoiceProvider
from research.strategy_workspace.current_sample_snapshot import (
    CurrentSampleSnapshotError,
    collect_current_sample_snapshot,
    verify_current_sample_snapshot,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("collect", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--membership-dir", type=Path, required=True)
        command.add_argument("--industry-dir", type=Path, required=True)
        command.add_argument("--sample-dir", type=Path, required=True)
        command.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "collect":
            result = collect_current_sample_snapshot(
                ChoiceProvider(),
                membership_dir=args.membership_dir,
                industry_dir=args.industry_dir,
                sample_dir=args.sample_dir,
                output_dir=args.output_dir,
            )
        else:
            result = verify_current_sample_snapshot(
                membership_dir=args.membership_dir,
                industry_dir=args.industry_dir,
                sample_dir=args.sample_dir,
                output_dir=args.output_dir,
            )
    except (CurrentSampleSnapshotError, ProviderError, OSError, ValueError) as exc:
        parser.error(safe_error_text(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
