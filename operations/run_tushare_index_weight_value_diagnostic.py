"""CLI for the P1.4D one-request index-weight value diagnostic."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import sys
from typing import Any, Mapping, Sequence

from research.market_data.tushare_alpha_feasibility import canonical_json_bytes
from research.market_data.tushare_index_weight_value_diagnostic import (
    IndexWeightValueDiagnosticError,
    collect_live_once,
    read_token_from_environment,
    regenerate_value_profile,
    replay_saved_response,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "One-shot 2017-12 index_weight raw capture and offline replay; "
            "no other endpoint is reachable."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect = subparsers.add_parser("collect")
    collect.add_argument("--run-id", required=True)
    profile = subparsers.add_parser("profile")
    profile.add_argument("--run-id", required=True)
    replay = subparsers.add_parser("replay")
    replay.add_argument("--run-id", required=True)
    replay.add_argument("--normalization-change", required=True)
    return parser


def _emit(value: Mapping[str, Any]) -> None:
    sys.stdout.buffer.write(canonical_json_bytes(dict(value)))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        token = read_token_from_environment()
        if args.command == "collect":
            result = collect_live_once(
                token=token,
                run_id=args.run_id,
                requested_at=datetime.now(timezone.utc),
            )
        elif args.command == "profile":
            result = regenerate_value_profile(token=token, run_id=args.run_id)
        else:
            result = replay_saved_response(
                token=token,
                run_id=args.run_id,
                normalization_change=args.normalization_change,
            )
    except IndexWeightValueDiagnosticError as exc:
        _emit(
            {
                "status": "BLOCKED",
                "failure_code": exc.code,
                "locked_test_status": {
                    "access": "NOT_ACCESSED",
                    "download": "NOT_DOWNLOADED",
                    "run": "NOT_RUN",
                },
                "locked_test_consumed": False,
            }
        )
        return 1
    _emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
