"""Standard CLI for the fixed Choice quality-growth historical batch."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

from research.market_data.choice_quality_growth_batch import (
    collect_choice_quality_growth_batch,
    verify_choice_quality_growth_batch,
)
from research.market_data.providers.base import (
    ProviderError,
    classify_unexpected_error,
    safe_error_text,
)
from research.market_data.providers.choice import ChoiceProvider


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def _aware_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected ISO datetime with timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("datetime must include a timezone offset")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Capture or verify the fixed Choice CSI 800 quality-growth evidence "
            "batch. Outputs remain non-PIT and ineligible for Paper/trading."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect = subparsers.add_parser("collect")
    collect.add_argument("--cutoff-date", required=True, type=_date)
    collect.add_argument("--as-of", required=True, type=_aware_datetime)
    collect.add_argument("--output-root", required=True, type=Path)
    collect.add_argument("--resume", action="store_true")
    verify = subparsers.add_parser("verify")
    verify.add_argument("--manifest", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "collect":
            result = collect_choice_quality_growth_batch(
                provider=ChoiceProvider(),
                cutoff_date=args.cutoff_date,
                as_of=args.as_of,
                output_root=args.output_root,
                resume=args.resume,
            )
            print(
                json.dumps(
                    {
                        "manifest_path": str(result.manifest_path),
                        "manifest_sha256": result.manifest_sha256,
                        "status": result.status,
                        "collection_status": result.collection_status,
                        "blocking_reasons": list(result.blocking_reasons),
                        "source_authenticated": False,
                        "integrity_semantics": "content_integrity_not_source_authentication",
                        "formal_truth_eligible": False,
                        "paper_eligible": False,
                        "trade_eligible": False,
                        "real_money_candidate": False,
                        "live_execution_status": "live_not_supported",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 3 if result.collection_status == "complete" else 2
        verification = verify_choice_quality_growth_batch(args.manifest)
        print(
            json.dumps(
                {
                    "manifest_path": str(verification.manifest_path),
                    "integrity_verified": verification.integrity_verified,
                    "status": verification.status,
                    "collection_status": verification.collection_status,
                    "reasons": list(verification.reasons),
                    "source_authenticated": False,
                    "integrity_semantics": "content_integrity_not_source_authentication",
                    "formal_truth_eligible": False,
                    "paper_eligible": False,
                    "trade_eligible": False,
                    "real_money_candidate": False,
                    "live_execution_status": "live_not_supported",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 3 if verification.integrity_verified else 2
    except Exception as exc:
        error = classify_unexpected_error(exc)
        print(
            json.dumps(
                {
                    "status": (
                        error.status if isinstance(exc, ProviderError) else "incomplete"
                    ),
                    "error_code": error.code,
                    "error_type": type(exc).__name__,
                    "error_message": safe_error_text(error),
                    "source_authenticated": False,
                    "integrity_semantics": "content_integrity_not_source_authentication",
                    "formal_truth_eligible": False,
                    "paper_eligible": False,
                    "trade_eligible": False,
                    "real_money_candidate": False,
                    "live_execution_status": "live_not_supported",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
