"""Collect or verify the fixed 2026-08-19 60-name diagnostic snapshot.

The command accepts no caller-selected dates, stocks, fields, benchmark,
ranking, signal, or eligibility flags.  Its outputs are permanently
diagnostic and cannot authorize Paper, trading, real-money lists, or LIVE.
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
)
from research.strategy_workspace.current_sample_snapshot_v2 import (
    collect_current_sample_snapshot_v2,
    verify_current_sample_snapshot_v2,
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
            result = collect_current_sample_snapshot_v2(
                ChoiceProvider(),
                membership_dir=args.membership_dir,
                industry_dir=args.industry_dir,
                sample_dir=args.sample_dir,
                output_dir=args.output_dir,
            )
        else:
            result = verify_current_sample_snapshot_v2(
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
