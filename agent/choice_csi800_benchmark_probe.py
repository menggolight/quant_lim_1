"""Capture the frozen one-day Choice CSI 800 benchmark capability probe.

The CLI first verifies two local Choice metadata snapshots byte-for-byte, then
queries only 000906.SH (price) and H00906.CSI (total return) for 2026-08-18 on
the unadjusted basis.  N00906.CSI is recorded as a distinct net-return alias
but is never queried or used as fallback.  The result remains diagnostic and
does not authenticate an official CSI benchmark identity.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from research.market_data.contracts import canonical_json_bytes, sha256_bytes
from research.market_data.providers.base import (
    ProviderError,
    classify_unexpected_error,
    safe_error_text,
)
from research.market_data.providers.choice import ChoiceProvider


PROBE_VERSION = "choice-csi800-benchmark-probe-v1"
ISPE_METADATA_PATH = Path(
    r"C:\Eastmoney\Choice\data\sdata\NecessaryData\ISPE_BLOCKTREE"
)
RELATION_METADATA_PATH = Path(
    r"C:\Eastmoney\Choice\data\sdata\NecessaryData\IND-BKZSDYGX2"
)
ISPE_METADATA_SHA256 = (
    "1f8dbcbd996272c5d0afd3adc3e421585e78b49692190caee8acf13f5c4a29a4"
)
RELATION_METADATA_SHA256 = (
    "540d3f9a79c30a8c66bf0e76480e2c62682852f8d968a863af56fb3b99964152"
)
ISPE_RECORD_SPECS: tuple[Mapping[str, Any], ...] = (
    {
        "role": "price",
        "record_index_zero_based": 22998,
        "expected": {
            "UNIQUEID": "905009001000906",
            "PARENTID": "905009001",
            "CODE": "000906",
            "BLOCK": "1",
            "CATEGORY": "0",
            "UNIQUECODE": "000906.SH",
        },
    },
    {
        "role": "total_return",
        "record_index_zero_based": 23087,
        "expected": {
            "UNIQUEID": "9050090019100906",
            "PARENTID": "905009001",
            "CODE": "H00906",
            "BLOCK": "1",
            "CATEGORY": "0",
            "UNIQUECODE": "H00906.CSI",
        },
    },
    {
        "role": "net_return_excluded_distinct_series",
        "record_index_zero_based": 23114,
        "expected": {
            "UNIQUEID": "9050090019300906",
            "PARENTID": "905009001",
            "CODE": "N00906",
            "BLOCK": "1",
            "CATEGORY": "0",
            "UNIQUECODE": "N00906.CSI",
        },
    },
)
RELATION_LINE_SPECS: tuple[Mapping[str, Any], ...] = (
    {
        "role": "price",
        "line_number_one_based": 7603,
        "line_sha256": (
            "fbefb4ea83690ddc5b0193928e6ffed9fbf4cdec277015c9e86fd21c0b55e783"
        ),
        "expected_ascii_fields": ("009006039", "1000157827", "000906.SH"),
    },
    {
        "role": "total_return",
        "line_number_one_based": 2324,
        "line_sha256": (
            "e396fde77130ba64b121be18e019a42edb1c37d82c1bcf1ddb38e3389382d596"
        ),
        "expected_ascii_fields": ("009006039", "1000157183", "H00906.CSI"),
    },
    {
        "role": "net_return_excluded_distinct_series",
        "line_number_one_based": 2875,
        "line_sha256": (
            "8bc2e650c49e55f197f8eeaa693215bd28c79c7b4f0148dbad610fb6d8c3585c"
        ),
        "expected_ascii_fields": ("009006039", "1000163163", "N00906.CSI"),
    },
)


def verify_metadata_binding(
    *,
    ispe_path: Path,
    relation_path: Path,
    expected_ispe_sha256: str,
    expected_relation_sha256: str,
    ispe_record_specs: tuple[Mapping[str, Any], ...],
    relation_line_specs: tuple[Mapping[str, Any], ...],
) -> dict[str, Any]:
    """Verify metadata bytes and return a non-authoritative alias receipt."""

    ispe_raw = ispe_path.read_bytes()
    relation_raw = relation_path.read_bytes()
    actual_ispe_sha256 = sha256_bytes(ispe_raw)
    actual_relation_sha256 = sha256_bytes(relation_raw)
    if actual_ispe_sha256 != expected_ispe_sha256:
        raise ValueError("Choice ISPE_BLOCKTREE metadata SHA-256 drifted")
    if actual_relation_sha256 != expected_relation_sha256:
        raise ValueError("Choice IND-BKZSDYGX2 metadata SHA-256 drifted")

    parsed = json.loads(ispe_raw.decode("utf-8"))
    records = parsed.get("ISPE_BLOCKTREE")
    if not isinstance(records, list):
        raise ValueError("Choice ISPE_BLOCKTREE metadata shape drifted")
    ispe_evidence: list[dict[str, Any]] = []
    for spec in ispe_record_specs:
        index = int(spec["record_index_zero_based"])
        if not 0 <= index < len(records) or not isinstance(records[index], dict):
            raise ValueError("Choice ISPE_BLOCKTREE fixed record is unavailable")
        expected = dict(spec["expected"])
        projection = {key: records[index].get(key) for key in expected}
        if projection != expected:
            raise ValueError("Choice ISPE_BLOCKTREE alias record drifted")
        ispe_evidence.append(
            {
                "role": str(spec["role"]),
                "record_index_zero_based": index,
                "record_projection": projection,
            }
        )

    lines = relation_raw.splitlines()
    relation_evidence: list[dict[str, Any]] = []
    for spec in relation_line_specs:
        line_number = int(spec["line_number_one_based"])
        if not 1 <= line_number <= len(lines):
            raise ValueError("Choice IND-BKZSDYGX2 fixed line is unavailable")
        line = lines[line_number - 1]
        line_sha256 = sha256_bytes(line)
        if line_sha256 != str(spec["line_sha256"]):
            raise ValueError("Choice IND-BKZSDYGX2 alias line drifted")
        fields = line.split(b"$")
        if len(fields) < 3:
            raise ValueError("Choice IND-BKZSDYGX2 alias line shape drifted")
        ascii_fields = tuple(item.decode("ascii") for item in fields[:3])
        if ascii_fields != tuple(spec["expected_ascii_fields"]):
            raise ValueError("Choice IND-BKZSDYGX2 alias fields drifted")
        relation_evidence.append(
            {
                "role": str(spec["role"]),
                "line_number_one_based": line_number,
                "line_sha256": line_sha256,
                "ascii_fields": list(ascii_fields),
                "non_ascii_label_bytes_hex": fields[3].hex() if len(fields) > 3 else "",
            }
        )

    return {
        "status": "verified_local_choice_metadata_snapshot",
        "semantics": "local_choice_alias_mapping_not_official_csi_authentication",
        "ispe_blocktree": {
            "path": str(ispe_path),
            "file_sha256": actual_ispe_sha256,
            "records": ispe_evidence,
        },
        "industry_index_relation": {
            "path": str(relation_path),
            "file_sha256": actual_relation_sha256,
            "records": relation_evidence,
        },
    }


def verify_fixed_metadata_binding() -> dict[str, Any]:
    return verify_metadata_binding(
        ispe_path=ISPE_METADATA_PATH,
        relation_path=RELATION_METADATA_PATH,
        expected_ispe_sha256=ISPE_METADATA_SHA256,
        expected_relation_sha256=RELATION_METADATA_SHA256,
        ispe_record_specs=ISPE_RECORD_SPECS,
        relation_line_specs=RELATION_LINE_SPECS,
    )


def collect_fixed_probe(
    provider: ChoiceProvider, metadata_binding: Mapping[str, Any]
) -> dict[str, Any]:
    with provider.diagnostic_session():
        payload = provider.fetch_csi800_benchmark_probe()
    projection = json.loads(payload.raw_content.decode("utf-8"))
    expected_series = [
        {"series": "price", "instrument_id": "000906.SH"},
        {"series": "total_return", "instrument_id": "H00906.CSI"},
    ]
    if projection.get("request", {}).get("series") != expected_series:
        raise ValueError("Choice CSI 800 benchmark probe series contract drifted")
    all_dates_proven = (
        projection.get("historical_date_proven_for_all_series") is True
    )
    return {
        "artifact_type": "choice_csi800_benchmark_probe",
        "schema_version": "1",
        "probe_version": PROBE_VERSION,
        "provider_id": "choice",
        "adapter_version": provider.adapter_version,
        "metadata_binding": dict(metadata_binding),
        "metadata_identity_status": (
            "choice_local_aliases_verified_not_official_benchmark_identity"
        ),
        "collection_status": "complete",
        "benchmark_capability_status": (
            "fixed_price_and_total_return_aliases_returned_expected_date"
            if all_dates_proven
            else "one_or_more_fixed_aliases_response_date_not_proven"
        ),
        "historical_date_proven_for_all_series": all_dates_proven,
        "point_in_time_status": (
            "diagnostic_response_date_echo_only_not_original_pit"
            if all_dates_proven
            else "diagnostic_response_date_not_proven"
        ),
        "raw_semantics": "canonicalized_sdk_projection",
        "source_authenticated": False,
        "official_benchmark_identity_authenticated": False,
        "formal_truth_eligible": False,
        "paper_eligible": False,
        "trade_eligible": False,
        "real_money_candidate": False,
        "live_execution_status": "live_not_supported",
        "capture": {
            "fetched_at": payload.fetched_at.isoformat(),
            "upstream_source": payload.upstream_source,
            "raw_projection_sha256": sha256_bytes(payload.raw_content),
            "raw_projection": projection,
            "issues": [dict(item) for item in payload.issues],
        },
    }


def write_new_artifact(artifact: Mapping[str, Any], output_path: Path) -> str:
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
            raise FileExistsError("Choice CSI 800 benchmark probe output already exists")
        metadata_binding = verify_fixed_metadata_binding()
        artifact = collect_fixed_probe(ChoiceProvider(), metadata_binding)
        artifact_sha256 = write_new_artifact(artifact, args.output)
        result = {
            "status": "captured_diagnostic_only",
            "output_path": str(args.output),
            "artifact_sha256": artifact_sha256,
            "collection_status": artifact["collection_status"],
            "benchmark_capability_status": artifact[
                "benchmark_capability_status"
            ],
            "historical_date_proven_for_all_series": artifact[
                "historical_date_proven_for_all_series"
            ],
            "metadata_identity_status": artifact["metadata_identity_status"],
            "official_benchmark_identity_authenticated": False,
            "formal_truth_eligible": False,
            "paper_eligible": False,
            "trade_eligible": False,
            "real_money_candidate": False,
            "live_execution_status": "live_not_supported",
        }
        exit_code = 0
    except Exception as exc:
        error = classify_unexpected_error(exc)
        result = {
            "status": error.status if isinstance(exc, ProviderError) else "incomplete",
            "error_code": error.code,
            "error_type": type(exc).__name__,
            "error_message": safe_error_text(error),
            "official_benchmark_identity_authenticated": False,
            "formal_truth_eligible": False,
            "paper_eligible": False,
            "trade_eligible": False,
            "real_money_candidate": False,
            "live_execution_status": "live_not_supported",
        }
        exit_code = 2
    sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
