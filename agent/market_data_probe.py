"""Run one real, read-only market-data provider probe and print structured JSON."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from research.market_data.contracts import (
    DATASET_SCHEMA_VERSIONS,
    MarketDataBatch,
    MarketDataRequest,
)
from research.market_data.providers.base import (
    classify_unexpected_error,
    redact_sensitive_value,
    safe_error_text,
)
from research.market_data.registry import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_STORAGE_ROOT,
    MarketDataRegistry,
)


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


@dataclass(frozen=True)
class ProbeArguments:
    provider: str
    dataset: str
    instrument: str
    start_date: date | None
    end_date: date | None
    retrieval_mode: str
    adjustment: str = "none"


def _date_range(batch: MarketDataBatch) -> dict[str, str | None]:
    field = {
        "daily_bar": "trading_date",
        "trade_calendar": "calendar_date",
        "security_master": "list_date",
    }.get(batch.dataset_type)
    values = sorted(
        str(record.get(field) or "")
        for record in batch.records
        if field and record.get(field)
    )
    return {
        "start": values[0] if values else None,
        "end": values[-1] if values else None,
    }


def _probe_base(
    arguments: ProbeArguments,
    requested_at: datetime,
    evidence_mode: str,
) -> dict[str, Any]:
    return {
        "probe_version": "market-data-probe-v1",
        "evidence_mode": evidence_mode,
        "provider_id": arguments.provider,
        "dataset_type": arguments.dataset,
        "instrument_id": arguments.instrument or None,
        "requested_at": requested_at.isoformat(),
        "retrieval_mode": arguments.retrieval_mode,
        "adjustment": arguments.adjustment,
        "upstream_source": None,
        "configured_upstream_sources": [],
        "adapter_version": None,
        "schema_version": DATASET_SCHEMA_VERSIONS.get(arguments.dataset),
        "request_fingerprint": None,
        "fetched_at": None,
        "record_count": 0,
        "date_range": {
            "start": arguments.start_date.isoformat() if arguments.start_date else None,
            "end": arguments.end_date.isoformat() if arguments.end_date else None,
        },
        "raw_content_sha256": None,
        "normalized_content_sha256": None,
        "completeness_status": "failed",
        "admission_status": "failed",
        "point_in_time_status": "not_admitted",
        "issues": [],
    }


def _failed_probe_result(base: dict[str, Any], exc: Exception) -> dict[str, Any]:
    error = classify_unexpected_error(exc)
    result = {
        **base,
        "status": error.status,
        "error_code": error.code,
        "error_type": type(exc).__name__,
        "error": safe_error_text(error),
        "issues": [
            {
                "code": error.code,
                "severity": "error",
                "message": safe_error_text(error),
            }
        ],
    }
    quarantine = getattr(error, "quarantine", None)
    if isinstance(quarantine, dict):
        result["quarantine"] = quarantine
    return result


def probe(
    registry: MarketDataRegistry,
    arguments: ProbeArguments,
    *,
    requested_at: datetime | None = None,
) -> dict[str, Any]:
    requested = requested_at or datetime.now(CHINA_TZ)
    registry_mode = (
        registry.evidence_mode
        if isinstance(registry, MarketDataRegistry)
        else "test_injected"
    )
    evidence_mode = (
        "validated_cache_replay"
        if registry_mode == "configured_runtime"
        and arguments.retrieval_mode == "offline_replay"
        else "real_provider"
        if registry_mode == "configured_runtime"
        else "test_injected"
    )
    base = _probe_base(arguments, requested, evidence_mode)
    try:
        config = getattr(registry, "config", {})
        if isinstance(config, dict):
            policy = config.get("providers", {}).get(arguments.provider, {})
            if isinstance(policy, dict):
                allowed = policy.get("allowed_upstream_sources", {}).get(
                    arguments.dataset, []
                )
                if isinstance(allowed, list):
                    base["configured_upstream_sources"] = [
                        str(item) for item in allowed
                    ]
                configured_adapter = policy.get("adapter_version")
                if isinstance(configured_adapter, str) and configured_adapter:
                    base["adapter_version"] = configured_adapter
        request = MarketDataRequest(
            dataset_type=arguments.dataset,
            instrument_id=arguments.instrument,
            start_date=arguments.start_date,
            end_date=arguments.end_date,
            retrieval_mode=arguments.retrieval_mode,
            adjustment=arguments.adjustment,
            requested_at=requested,
        )
        provider_resolver = getattr(registry, "provider", None)
        if callable(provider_resolver):
            provider = provider_resolver(arguments.provider)
            base.update(
                {
                    "adapter_version": provider.adapter_version,
                    "request_fingerprint": request.fingerprint(
                        provider.provider_id, provider.adapter_version
                    ),
                }
            )
        if arguments.provider == "choice":
            diagnostic_fetch = getattr(registry, "fetch_diagnostic", None)
            if not callable(diagnostic_fetch):
                raise RuntimeError(
                    "Choice probes require the explicit diagnostic registry boundary"
                )
            batch = diagnostic_fetch(request, provider_id="choice")
        else:
            batch = registry.fetch(request, provider_id=arguments.provider)
        if batch.record_count <= 0:
            raise RuntimeError("validated probe batch is unexpectedly empty")
        return {
            **base,
            "status": "passed",
            "batch_id": batch.batch_id,
            "upstream_source": batch.upstream_source,
            "adapter_version": batch.adapter_version,
            "schema_version": batch.schema_version,
            "request_fingerprint": batch.request_fingerprint,
            "fetched_at": batch.fetched_at.isoformat(),
            "record_count": batch.record_count,
            "date_range": _date_range(batch),
            "raw_content_sha256": batch.raw_content_sha256,
            "normalized_content_sha256": batch.normalized_content_sha256,
            "available_at_min": batch.available_at_min.isoformat() if batch.available_at_min else None,
            "available_at_max": batch.available_at_max.isoformat() if batch.available_at_max else None,
            "admission_status": batch.admission_status,
            "completeness_status": batch.completeness_status,
            "point_in_time_status": batch.point_in_time_status,
            "synthetic": batch.synthetic,
            "issues": [redact_sensitive_value(dict(item)) for item in batch.issues],
        }
    except Exception as exc:
        return _failed_probe_result(base, exc)


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _write_output_create_only(path: Path, rendered: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(rendered)
    except FileExistsError as exc:
        raise FileExistsError(
            "market-data probe output already exists; refusing to overwrite"
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True)
    parser.add_argument(
        "--dataset",
        required=True,
        choices=("daily_bar", "trade_calendar", "security_master"),
    )
    parser.add_argument("--instrument", default="")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument(
        "--adjustment",
        choices=("none", "qfq", "hfq"),
        default="none",
        help="Daily-bar adjustment; Choice stocks require qfq and 000300.SH requires none.",
    )
    parser.add_argument(
        "--retrieval-mode",
        choices=("live_capture", "historical_backfill", "offline_replay"),
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--storage-root", type=Path, default=DEFAULT_STORAGE_ROOT)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    mode = args.retrieval_mode or (
        "live_capture" if args.dataset == "security_master" else "historical_backfill"
    )
    try:
        arguments = ProbeArguments(
            provider=args.provider,
            dataset=args.dataset,
            instrument=args.instrument,
            start_date=_parse_date(args.start_date),
            end_date=_parse_date(args.end_date),
            retrieval_mode=mode,
            adjustment=args.adjustment,
        )
    except ValueError as exc:
        parser.error(str(exc))
        return 2
    requested_at = datetime.now(CHINA_TZ)
    evidence_mode = (
        "validated_cache_replay" if mode == "offline_replay" else "real_provider"
    )
    try:
        registry = MarketDataRegistry.configured(
            args.config,
            storage_root=args.storage_root,
        )
        result = probe(
            registry,
            arguments,
            requested_at=requested_at,
        )
    except Exception as exc:
        result = _failed_probe_result(
            _probe_base(arguments, requested_at, evidence_mode),
            exc,
        )
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        _write_output_create_only(args.output, rendered)
    sys.stdout.write(rendered)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
