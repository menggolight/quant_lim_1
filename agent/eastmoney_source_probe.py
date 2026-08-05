"""Live, read-only health probe for the public Eastmoney adapters.

The probe never updates a sealed market observation.  It writes a separate
diagnostic JSON so a historical failure remains historical evidence while the
next collection run can decide whether the source is currently usable.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from research.broker_report_audit.models import CHINA_TZ
from research.broker_report_audit.sources import (
    EASTMONEY_IPV4_ONLY_HOSTS,
    CachedHttpClient,
    EastmoneyIndustryBoardSource,
    EastmoneyMarketSource,
)
from research.broker_report_audit.storage import HttpCache


DEFAULT_CACHE = Path(".tmp") / "eastmoney_source_probe_cache"
DEFAULT_OUTPUT = Path(".tmp") / "eastmoney_source_probe.json"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PROTECTED_OUTPUT_ROOTS = (
    REPOSITORY_ROOT / "data" / "actions",
    REPOSITORY_ROOT / "data" / "signals",
    REPOSITORY_ROOT / "data" / "reports" / "market_observation",
)


def _date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must use YYYY-MM-DD") from exc


def _error_result(exc: BaseException) -> dict[str, Any]:
    current = exc
    seen: set[int] = set()
    while id(current) not in seen:
        seen.add(id(current))
        reason = getattr(current, "reason", None)
        candidate = (
            reason
            if isinstance(reason, BaseException)
            else current.__cause__ or current.__context__
        )
        if not isinstance(candidate, BaseException) or id(candidate) in seen:
            break
        current = candidate
    result = {
        "status": "failed",
        "error_type": type(exc).__name__,
        "error": str(exc),
    }
    if current is not exc:
        result.update(
            {
                "root_error_type": type(current).__name__,
                "root_error": str(current),
            }
        )
    return result


def _validated_output_path(output_path: Path) -> Path:
    resolved = output_path.resolve()
    if any(
        resolved == protected.resolve()
        or resolved.is_relative_to(protected.resolve())
        for protected in PROTECTED_OUTPUT_ROOTS
    ):
        raise ValueError(
            "probe output cannot target controlled observations, signals, or actions"
        )
    if not resolved.exists():
        return resolved
    if not resolved.is_file():
        raise ValueError("probe output path already exists and is not a file")
    try:
        existing = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("refusing to overwrite a non-probe output file") from exc
    if not isinstance(existing, dict) or (
        existing.get("schema_version") != "eastmoney-source-probe-v0.1"
        or existing.get("purpose") != "transport_and_completeness_diagnostic_only"
        or existing.get("safety_status") != "research_only_not_trade_eligible"
    ):
        raise ValueError("refusing to overwrite a non-probe output file")
    return resolved


def run_probe(
    *,
    stock_id: str,
    start_date: date,
    end_date: date,
    expected_last_date: date,
    cache_directory: Path,
    output_path: Path,
    timeout: float = 30.0,
    rate_limit_seconds: float = 1.5,
) -> tuple[dict[str, Any], int]:
    if start_date > end_date:
        raise ValueError("start_date cannot be after end_date")
    if not start_date <= expected_last_date <= end_date:
        raise ValueError("expected_last_date must fall inside the requested window")
    output_path = _validated_output_path(output_path)

    started_at = datetime.now(CHINA_TZ)
    checks: dict[str, dict[str, Any]] = {}
    cache = HttpCache(cache_directory)
    client = CachedHttpClient(
        cache,
        offline=False,
        rate_limit_seconds=rate_limit_seconds,
        max_retries=1,
        timeout=timeout,
        user_agent="quant-eastmoney-source-probe/0.1 research-only",
        ipv4_only_hosts=EASTMONEY_IPV4_ONLY_HOSTS,
    )
    try:
        market_source = EastmoneyMarketSource(client)
        try:
            bars = market_source.daily_bars(
                stock_id,
                start_date,
                end_date,
                as_of=datetime.now(CHINA_TZ),
                adjust="qfq",
                refresh=True,
            )
            if not bars:
                raise ValueError("market history returned no mature daily bars")
            if market_source.last_issues:
                raise ValueError(
                    "market history returned adapter warnings: "
                    + json.dumps(market_source.last_issues, ensure_ascii=False)
                )
            if any(bar.adjusted_close is None for bar in bars):
                raise ValueError("adjusted history does not cover every returned raw bar")
            observed_last_date = max(bar.trade_date for bar in bars)
            if observed_last_date != expected_last_date:
                raise ValueError(
                    "market history last date mismatch: "
                    f"expected {expected_last_date}, got {observed_last_date}"
                )
            checks["market_history"] = {
                "status": "passed",
                "instrument_id": stock_id,
                "requested_start_date": start_date.isoformat(),
                "requested_end_date": end_date.isoformat(),
                "expected_last_date": expected_last_date.isoformat(),
                "first_trade_date": min(bar.trade_date for bar in bars).isoformat(),
                "last_trade_date": observed_last_date.isoformat(),
                "bar_count": len(bars),
                "adjusted_bar_count": sum(
                    bar.adjusted_close is not None for bar in bars
                ),
                "source": bars[0].source,
                "content_hash": bars[0].content_hash,
            }
        except Exception as exc:
            checks["market_history"] = _error_result(exc)

        industry_source = EastmoneyIndustryBoardSource(client)
        try:
            batch = industry_source.fetch_snapshot(
                page_size=100,
                max_pages=20,
                max_fetch_span_seconds=120.0,
                refresh=True,
                require_live=True,
            )
            checks["industry_board"] = {
                "status": "passed",
                "expected_total": batch.expected_total,
                "unique_board_count": len(
                    {record.board_id for record in batch.records}
                ),
                "pages_fetched": batch.pages_fetched,
                "first_fetched_at": batch.first_fetched_at.isoformat(),
                "last_fetched_at": batch.last_fetched_at.isoformat(),
                "all_from_cache": batch.all_from_cache,
                "source": batch.source,
                "source_url": batch.source_url,
                "content_hash": batch.content_hash,
                "sample_board_ids": [
                    record.board_id for record in batch.records[:5]
                ],
            }
        except Exception as exc:
            checks["industry_board"] = _error_result(exc)
    finally:
        client.close()
        cache.close()

    passed = all(check.get("status") == "passed" for check in checks.values())
    result = {
        "schema_version": "eastmoney-source-probe-v0.1",
        "purpose": "transport_and_completeness_diagnostic_only",
        "safety_status": "research_only_not_trade_eligible",
        "started_at": started_at.isoformat(),
        "completed_at": datetime.now(CHINA_TZ).isoformat(),
        "overall_status": "passed" if passed else "failed",
        "transport_policy": {
            "mode": "direct_connection_exact_host_ipv4_only",
            "hosts": sorted(EASTMONEY_IPV4_ONLY_HOSTS),
            "proxy_caveat": (
                "When an HTTP proxy is configured, the system connects to the proxy; "
                "the proxy controls the origin address family."
            ),
            "tls_verification": "enabled",
        },
        "checks": checks,
        "limitations": [
            (
                "This proves current transport, industry pagination completeness, and the "
                "caller-supplied history last-date gate; it does not verify every trading day."
            ),
            "It does not admit Eastmoney as official industry or fundamental truth.",
            "It does not modify sealed observations, factors, signals, or orders.",
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(output_path)
    return result, 0 if passed else 1


def build_parser() -> argparse.ArgumentParser:
    yesterday = datetime.now(CHINA_TZ).date() - timedelta(days=1)
    parser = argparse.ArgumentParser(
        prog="python -m agent.eastmoney_source_probe",
        description="Probe Eastmoney public history and complete industry-board access.",
    )
    parser.add_argument("--stock", default="000333.SZ", help="A-share instrument id.")
    parser.add_argument(
        "--start-date",
        default=(yesterday - timedelta(days=35)).isoformat(),
        help="History window start (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--end-date",
        default=yesterday.isoformat(),
        help="History window end (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--expected-last-date",
        required=True,
        help="Exact exchange trading date required as the last returned bar.",
    )
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--rate-limit", type=float, default=1.5)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result, exit_code = run_probe(
            stock_id=args.stock,
            start_date=_date(args.start_date, "start_date"),
            end_date=_date(args.end_date, "end_date"),
            expected_last_date=_date(args.expected_last_date, "expected_last_date"),
            cache_directory=Path(args.cache_dir),
            output_path=Path(args.output),
            timeout=args.timeout,
            rate_limit_seconds=args.rate_limit,
        )
    except Exception as exc:
        parser.error(str(exc))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
