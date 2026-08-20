"""Command line interface for the research-only CSI-11 factor laboratory."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Sequence

from .engine import EvidenceBundle, ExperimentRunner, FactorLabError


def _add_config(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        default=ExperimentRunner.DEFAULT_HYPOTHESIS_PATH,
    )


def _add_evidence(
    parser: argparse.ArgumentParser, *, repeatable_index_receipt: bool = False
) -> None:
    parser.add_argument("--evidence", type=Path)
    parser.add_argument(
        "--index-receipt",
        type=Path,
        action="append" if repeatable_index_receipt else "store",
    )
    parser.add_argument("--calendar-receipt", type=Path)
    parser.add_argument("--evidence-root", type=Path)


def _load_evidence(
    runner: ExperimentRunner, args: argparse.Namespace, stage: str
) -> EvidenceBundle:
    direct = args.evidence is not None
    probe_values = (
        args.index_receipt,
        args.calendar_receipt,
        args.evidence_root,
    )
    if direct and any(value is not None for value in probe_values):
        raise FactorLabError("use either --evidence or probe receipt arguments, not both")
    if direct:
        return EvidenceBundle.from_json(args.evidence)
    if any(value is None for value in probe_values):
        raise FactorLabError(
            "evidence requires --evidence or all of --index-receipt, "
            "--calendar-receipt and --evidence-root"
        )
    return runner.load_probe_evidence(
        stage=stage,
        index_receipt=args.index_receipt,
        calendar_receipt=args.calendar_receipt,
        evidence_root=args.evidence_root,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory = subparsers.add_parser("inventory")
    _add_config(inventory)

    preregister = subparsers.add_parser("preregister")
    _add_config(preregister)
    preregister.add_argument("--output-dir", type=Path, required=True)

    screen = subparsers.add_parser("screen")
    _add_config(screen)
    _add_evidence(screen, repeatable_index_receipt=True)
    screen.add_argument("--output-dir", type=Path, required=True)

    confirm = subparsers.add_parser("confirm")
    _add_config(confirm)
    _add_evidence(confirm)
    confirm.add_argument("--screen-run", type=Path, required=True)
    confirm.add_argument("--screen-evidence", type=Path)
    confirm.add_argument("--screen-index-receipt", type=Path, action="append")
    confirm.add_argument("--screen-calendar-receipt", type=Path)
    confirm.add_argument("--screen-evidence-root", type=Path)
    confirm.add_argument("--output-dir", type=Path, required=True)

    weekly = subparsers.add_parser("weekly")
    _add_config(weekly)
    _add_evidence(weekly)
    weekly.add_argument("--confirmed-run", type=Path, required=True)
    weekly.add_argument("--as-of", type=date.fromisoformat)
    weekly.add_argument("--output-dir", type=Path, required=True)

    verify = subparsers.add_parser("verify")
    _add_config(verify)
    verify.add_argument("--run-dir", type=Path, required=True)
    return parser


def _screen_evidence(runner: ExperimentRunner, args: argparse.Namespace) -> EvidenceBundle:
    direct = args.screen_evidence is not None
    probe = (
        args.screen_index_receipt,
        args.screen_calendar_receipt,
        args.screen_evidence_root,
    )
    if direct and any(value is not None for value in probe):
        raise FactorLabError("screen evidence accepts one input form only")
    if direct:
        return EvidenceBundle.from_json(args.screen_evidence)
    if any(value is None for value in probe):
        raise FactorLabError(
            "confirm requires --screen-evidence or all three --screen-*-receipt/root arguments"
        )
    return runner.load_probe_evidence(
        stage="screen",
        index_receipt=args.screen_index_receipt,
        calendar_receipt=args.screen_calendar_receipt,
        evidence_root=args.screen_evidence_root,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        runner = ExperimentRunner(args.config)
        if args.command == "inventory":
            result = runner.inventory()
        elif args.command == "preregister":
            result = runner.preregister(args.output_dir)
        elif args.command == "screen":
            result = runner.screen(
                _load_evidence(runner, args, "screen"), args.output_dir
            )
        elif args.command == "confirm":
            result = runner.confirm(
                _load_evidence(runner, args, "confirm"),
                screen_evidence=_screen_evidence(runner, args),
                screen_run=args.screen_run,
                output_dir=args.output_dir,
            )
        elif args.command == "weekly":
            result = runner.weekly(
                _load_evidence(runner, args, "weekly"),
                confirmed_run=args.confirmed_run,
                output_dir=args.output_dir,
                as_of=args.as_of,
            )
        else:
            result = runner.verify(args.run_dir)
    except (FactorLabError, OSError, ValueError) as exc:
        sys.stderr.write(
            json.dumps(
                {"status": "failed", "error_type": type(exc).__name__, "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
        return 1
    sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
