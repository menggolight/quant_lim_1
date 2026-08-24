"""Capture or replay one explicit, read-only Choice candidate interface."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Sequence

from research.market_data.choice_candidates import (
    ChoiceCandidateEvidence,
    ChoiceCandidateService,
)
from research.market_data.providers.base import classify_unexpected_error, safe_error_text


DEFAULT_STORAGE_ROOT = Path(".tmp/market_data/choice_candidates")


def _write_output_create_only(path: Path, rendered: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(rendered)
    except FileExistsError as exc:
        raise FileExistsError(
            "Choice candidate probe output already exists; refusing to overwrite"
        ) from exc


def _result(evidence: ChoiceCandidateEvidence, *, mode: str) -> dict[str, Any]:
    return {
        "probe_version": "choice-candidate-probe-v1",
        "mode": mode,
        "provider_id": evidence.provider_id,
        "adapter_version": evidence.adapter_version,
        "query_type": evidence.query_type,
        "status": evidence.status,
        "account_status": "not_assessed",
        "evidence_id": evidence.evidence_id,
        "request_fingerprint": evidence.request_fingerprint,
        "exact_request": json.loads(
            json.dumps(evidence.exact_request, ensure_ascii=False)
        ),
        "fetched_at": evidence.fetched_at.isoformat(),
        "admission_status": evidence.admission_status,
        "point_in_time_status": evidence.point_in_time_status,
        "formal_truth_eligible": evidence.formal_truth_eligible,
        "raw_content_sha256": evidence.raw_content_sha256,
        "normalized_content_sha256": evidence.normalized_content_sha256,
        "record_count": evidence.record_count,
        "issues": [dict(item) for item in evidence.issues],
    }


def run_probe(service: ChoiceCandidateService, args: argparse.Namespace) -> dict[str, Any]:
    online = args.mode == "online"
    if args.interface == "sw2021":
        evidence = (
            service.fetch_sw2021_classification(args.instrument)
            if online
            else service.replay_sw2021_classification(args.instrument)
        )
    elif args.interface == "sector":
        evidence = (
            service.fetch_historical_sector_membership(
                args.sector_code, args.membership_date
            )
            if online
            else service.replay_historical_sector_membership(
                args.sector_code, args.membership_date
            )
        )
    else:
        evidence = (
            service.fetch_edb_publish_dates(
                args.edb_id
            )
            if online
            else service.replay_edb_publish_dates(
                args.edb_id
            )
        )
    return _result(evidence, mode=args.mode)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("online", "offline"),
        required=True,
        help="online captures the SDK; offline only verifies a matching stored capture",
    )
    parser.add_argument(
        "--storage-root", type=Path, default=DEFAULT_STORAGE_ROOT
    )
    parser.add_argument("--output", type=Path)
    subparsers = parser.add_subparsers(dest="interface", required=True)

    sw2021 = subparsers.add_parser("sw2021", help="current SW2021 css candidate")
    sw2021.add_argument("--instrument", required=True)

    sector = subparsers.add_parser(
        "sector", help="requested-date Choice sector membership candidate"
    )
    sector.add_argument("--sector-code", required=True)
    sector.add_argument("--membership-date", type=date.fromisoformat, required=True)

    edb = subparsers.add_parser(
        "edb", help="EDB candidate with publish date plus edbquery metadata"
    )
    edb.add_argument("--edb-id", action="append", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run_probe(ChoiceCandidateService(args.storage_root), args)
    except Exception as exc:
        error = classify_unexpected_error(exc)
        result = {
            "probe_version": "choice-candidate-probe-v1",
            "mode": args.mode,
            "provider_id": "choice",
            "query_type": args.interface,
            "status": error.status,
            "account_status": "not_assessed",
            "formal_truth_eligible": False,
            "error_code": error.code,
            "error_type": type(exc).__name__,
            "error": safe_error_text(error),
        }
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        _write_output_create_only(args.output, rendered)
    sys.stdout.write(rendered)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
