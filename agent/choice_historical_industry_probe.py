"""Run the frozen, read-only Choice HISCSIND date-echo probe.

The query contract is intentionally not configurable.  It captures three
stocks on two historical dates solely to determine whether the SDK response
echoes each requested date.  A successful echo is still diagnostic evidence,
not point-in-time industry truth and not a trading permission.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Sequence

from research.market_data.contracts import canonical_json_bytes, sha256_bytes
from research.market_data.providers.base import safe_error_text
from research.market_data.providers.choice import ChoiceProvider


PROBE_VERSION = "choice-historical-csi-industry-probe-v1"
FIXED_INSTRUMENTS = ("000001.SZ", "000333.SZ", "600519.SH")
FIXED_DATES = (date(2024, 6, 28), date(2026, 8, 18))


def collect_fixed_probe(provider: ChoiceProvider) -> dict[str, Any]:
    """Collect the two fixed responses in one read-only Choice session."""

    captures: list[dict[str, Any]] = []
    with provider.diagnostic_session():
        for requested_date in FIXED_DATES:
            payload = provider.fetch_historical_csi_industry_probe(requested_date)
            projection = json.loads(payload.raw_content.decode("utf-8"))
            evidence = projection.get("date_evidence", {})
            if evidence.get("requested_date") != requested_date.isoformat():
                raise ValueError("Choice probe projection requested date drifted")
            captures.append(
                {
                    "requested_date": requested_date.isoformat(),
                    "fetched_at": payload.fetched_at.isoformat(),
                    "upstream_source": payload.upstream_source,
                    "raw_projection_sha256": sha256_bytes(payload.raw_content),
                    "raw_projection": projection,
                    "response_date_evidence": dict(evidence),
                    "issues": [dict(item) for item in payload.issues],
                }
            )

    all_dates_proven = all(
        item["response_date_evidence"].get("historical_date_proven") is True
        for item in captures
    )
    return {
        "artifact_type": "choice_historical_csi_industry_probe",
        "schema_version": "1",
        "probe_version": PROBE_VERSION,
        "provider_id": "choice",
        "adapter_version": provider.adapter_version,
        "query_contract": {
            "instrument_ids": list(FIXED_INSTRUMENTS),
            "requested_dates": [item.isoformat() for item in FIXED_DATES],
            "indicator": "HISCSIND",
            "classification_level": 1,
            "read_only": True,
        },
        "collection_status": "complete",
        "date_evidence_status": (
            "all_response_dates_exactly_echo_requested_date"
            if all_dates_proven
            else "one_or_more_response_dates_not_proven"
        ),
        "historical_date_proven_for_all_requests": all_dates_proven,
        "point_in_time_status": (
            "diagnostic_response_date_echo_only_not_original_pit"
            if all_dates_proven
            else "diagnostic_response_date_not_proven"
        ),
        "formal_strategy_status": "blocked_missing_pit_industry",
        "raw_semantics": "canonicalized_sdk_projection",
        "source_authenticated": False,
        "formal_truth_eligible": False,
        "paper_eligible": False,
        "trade_eligible": False,
        "real_money_candidate": False,
        "live_execution_status": "live_not_supported",
        "captures": captures,
    }


def write_new_artifact(artifact: dict[str, Any], output_path: Path) -> str:
    """Write once without replacing an earlier diagnostic capture."""

    raw = canonical_json_bytes(artifact)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("xb") as handle:
        handle.write(raw)
    return sha256_bytes(raw)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new JSON evidence file; an existing file is never overwritten",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.output.exists():
            raise FileExistsError(
                "Choice historical industry probe output already exists"
            )
        artifact = collect_fixed_probe(ChoiceProvider())
        artifact_sha256 = write_new_artifact(artifact, args.output)
        result = {
            "status": "captured_diagnostic_only",
            "output_path": str(args.output),
            "artifact_sha256": artifact_sha256,
            "collection_status": artifact["collection_status"],
            "date_evidence_status": artifact["date_evidence_status"],
            "historical_date_proven_for_all_requests": artifact[
                "historical_date_proven_for_all_requests"
            ],
            "point_in_time_status": artifact["point_in_time_status"],
            "formal_strategy_status": artifact["formal_strategy_status"],
            "source_authenticated": False,
            "formal_truth_eligible": False,
            "paper_eligible": False,
            "trade_eligible": False,
            "real_money_candidate": False,
            "live_execution_status": "live_not_supported",
        }
        exit_code = 0
    except Exception as exc:
        result = {
            "status": "incomplete",
            "error_type": type(exc).__name__,
            "error_message": safe_error_text(exc),
            "source_authenticated": False,
            "formal_truth_eligible": False,
            "paper_eligible": False,
            "trade_eligible": False,
            "real_money_candidate": False,
            "live_execution_status": "live_not_supported",
        }
        exit_code = 2
    sys.stdout.write(
        json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
