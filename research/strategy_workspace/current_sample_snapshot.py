"""Controlled 60-name technical-factor snapshot for the non-PIT fallback.

The module deliberately produces no ranking, portfolio, order list, Paper
admission, or real-money decision.  It consumes the already sealed current
CSI 800 diagnostic sample, captures exactly 121 common sessions, and emits
one six-factor cross-section for the sample's frozen market snapshot date.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from agent.current_industry_import import verify_controlled_sample
from research.market_data.contracts import (
    MarketDataRequest,
    canonical_json_bytes,
    sha256_bytes,
)
from research.market_data.providers.base import ProviderPayload
from research.market_data.providers.choice import ChoiceProvider, replay_choice_raw
from research.strategy_workspace.diagnostic import (
    DIAGNOSTIC_FACTOR_IDS,
    DIAGNOSTIC_STATUS,
    DiagnosticPriceBar,
    compute_price_diagnostics,
)


SNAPSHOT_SCHEMA_VERSION = "strategy-current-sample-factor-snapshot-v1"
MANIFEST_SCHEMA_VERSION = "strategy-current-sample-factor-snapshot-manifest-v1"
FIXED_CALENDAR_START = date(2026, 1, 1)
FIXED_SNAPSHOT_DATE = date(2026, 8, 18)
FIXED_INFORMATION_CUTOFF_DATE = date(2026, 8, 19)
FIXED_SESSION_COUNT = 121
FIXED_BENCHMARK_ID = "000906.SH"
CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
SAFETY = {
    "paper_eligibility": False,
    "trade_eligibility": False,
    "real_money_list_allowed": False,
    "live": "not_supported",
}
CODE_BUNDLE_PATHS = (
    "agent/current_industry_import.py",
    "agent/current_sample_snapshot.py",
    "research/market_data/contracts.py",
    "research/market_data/providers/choice.py",
    "research/strategy_workspace/current_sample_snapshot.py",
    "research/strategy_workspace/diagnostic.py",
)


class CurrentSampleSnapshotError(ValueError):
    """Raised when the bounded diagnostic snapshot cannot be reproduced."""


class SnapshotProvider(Protocol):
    adapter_version: str

    def diagnostic_session(self): ...

    def fetch_quality_growth_calendar(
        self, start_date: date, end_date: date
    ) -> ProviderPayload: ...


def _require_exact_keys(
    value: Mapping[str, Any], expected: set[str], label: str
) -> None:
    if set(value) != expected:
        raise CurrentSampleSnapshotError(f"{label} field set differs from the v1 contract")

    def fetch_quality_growth_csd(
        self,
        instrument_id: str,
        start_date: date,
        end_date: date,
        *,
        adjustment: str,
    ) -> ProviderPayload: ...

    def fetch_csi800_price_index_csd(
        self, start_date: date, end_date: date
    ) -> ProviderPayload: ...


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CurrentSampleSnapshotError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_canonical(path: Path, label: str) -> Mapping[str, Any]:
    try:
        raw = path.read_bytes()
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant: {item}")
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CurrentSampleSnapshotError(f"{label} is not strict JSON") from exc
    if not isinstance(value, Mapping) or canonical_json_bytes(value) != raw:
        raise CurrentSampleSnapshotError(f"{label} is not canonical JSON")
    return value


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _code_bundle() -> Mapping[str, Any]:
    root = _repo_root()
    files: dict[str, str] = {}
    for relative in CODE_BUNDLE_PATHS:
        path = root / relative
        if not path.is_file():
            raise CurrentSampleSnapshotError(f"code bundle file is missing: {relative}")
        files[relative] = sha256_bytes(path.read_bytes())
    runtime = f"{platform.python_implementation().lower()}-{platform.python_version()}"
    content = {"files": files, "runtime": runtime}
    return {**content, "bundle_sha256": sha256_bytes(canonical_json_bytes(content))}


def _validated_sample(
    membership_dir: Path,
    industry_dir: Path,
    sample_dir: Path,
) -> tuple[Mapping[str, Any], Mapping[str, Any], str]:
    verified_manifest = verify_controlled_sample(
        membership_dir, industry_dir, sample_dir
    )
    sample_path = sample_dir / "sample.json"
    sample = _load_canonical(sample_path, "controlled diagnostic sample")
    if sample.get("status") != DIAGNOSTIC_STATUS:
        raise CurrentSampleSnapshotError("controlled sample status differs from diagnostic contract")
    if sample.get("market_snapshot_date") != FIXED_SNAPSHOT_DATE.isoformat():
        raise CurrentSampleSnapshotError("controlled sample snapshot date differs from v1")
    if sample.get("information_cutoff_date") != FIXED_INFORMATION_CUTOFF_DATE.isoformat():
        raise CurrentSampleSnapshotError("controlled sample information cutoff differs from v1")
    instruments = sample.get("instrument_ids")
    if (
        not isinstance(instruments, list)
        or len(instruments) != 60
        or len(set(instruments)) != 60
    ):
        raise CurrentSampleSnapshotError("controlled sample must contain exactly 60 names")
    if sample.get("factor_ids") != list(DIAGNOSTIC_FACTOR_IDS):
        raise CurrentSampleSnapshotError("controlled sample factor family drifted")
    if sample.get("safety") != SAFETY:
        raise CurrentSampleSnapshotError("controlled sample safety contract drifted")
    return sample, verified_manifest, sha256_bytes(sample_path.read_bytes())


def _normalized_payload(payload: ProviderPayload) -> Mapping[str, Any]:
    return {
        "fetched_at": payload.fetched_at.isoformat(),
        "upstream_source": payload.upstream_source,
        "issues": [dict(item) for item in payload.issues],
        "records": [dict(item) for item in payload.records],
    }


def _validate_capture_time(payload: ProviderPayload, label: str) -> None:
    local_day = payload.fetched_at.astimezone(CHINA_TZ).date()
    if local_day != FIXED_INFORMATION_CUTOFF_DATE:
        raise CurrentSampleSnapshotError(
            f"{label} was not captured on the frozen information-cutoff date"
        )


def _strict_raw_object(raw: bytes, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant: {item}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CurrentSampleSnapshotError(f"{label} raw evidence is not strict JSON") from exc
    if not isinstance(value, Mapping) or canonical_json_bytes(value) != raw:
        raise CurrentSampleSnapshotError(f"{label} raw evidence is not canonical JSON")
    return value


def _verify_raw_projection(
    *,
    raw: bytes,
    normalized: Mapping[str, Any],
    kind: str,
    instrument_id: str = "",
    start_date: date,
    end_date: date,
    adjustment: str = "none",
) -> None:
    """Prove that each stored normalized record is present in the raw projection."""

    records = normalized.get("records")
    _require_exact_keys(
        normalized,
        {"fetched_at", "upstream_source", "issues", "records"},
        f"{kind} normalized artifact",
    )
    if not isinstance(records, list) or any(not isinstance(item, Mapping) for item in records):
        raise CurrentSampleSnapshotError(f"{kind} normalized records are malformed")
    issues = normalized.get("issues")
    if not isinstance(issues, list) or any(not isinstance(item, Mapping) for item in issues):
        raise CurrentSampleSnapshotError(f"{kind} normalized issues are malformed")
    fetched_at_raw = normalized.get("fetched_at")
    if not isinstance(fetched_at_raw, str):
        raise CurrentSampleSnapshotError(f"{kind} normalized fetched_at is missing")
    try:
        fetched_at = datetime.fromisoformat(fetched_at_raw)
    except ValueError as exc:
        raise CurrentSampleSnapshotError(f"{kind} normalized fetched_at is invalid") from exc
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise CurrentSampleSnapshotError(f"{kind} normalized fetched_at lacks a timezone")
    payload = _strict_raw_object(raw, kind)
    if kind == "calendar":
        if normalized.get("upstream_source") != ChoiceProvider._CALENDAR_UPSTREAM:
            raise CurrentSampleSnapshotError("calendar upstream source drifted")
        if [item.get("code") for item in normalized.get("issues", [])] != [
            "choice_calendar_secondary_not_official",
            "choice_quality_growth_historical_capture_not_formal_truth",
        ]:
            raise CurrentSampleSnapshotError("calendar issue contract drifted")
        request = MarketDataRequest(
            dataset_type="trade_calendar",
            start_date=start_date,
            end_date=end_date,
            retrieval_mode="historical_backfill",
            requested_at=fetched_at,
        )
        try:
            replayed = replay_choice_raw(request, raw, fetched_at)
        except Exception as exc:
            raise CurrentSampleSnapshotError("calendar raw replay failed") from exc
        if canonical_json_bytes(list(replayed)) != canonical_json_bytes(records):
            raise CurrentSampleSnapshotError("calendar raw and normalized records differ")
        return

    raw_records = payload.get("records")
    request = payload.get("request")
    if not isinstance(raw_records, list) or not isinstance(request, Mapping):
        raise CurrentSampleSnapshotError(f"{kind} raw projection is malformed")
    if canonical_json_bytes(raw_records) != canonical_json_bytes(records):
        raise CurrentSampleSnapshotError(f"{kind} raw and normalized records differ")
    if (
        request.get("instrument_id") != instrument_id
        or request.get("start_date") != start_date.isoformat()
        or request.get("end_date") != end_date.isoformat()
        or request.get("adjustment") != adjustment
    ):
        raise CurrentSampleSnapshotError(f"{kind} raw request differs from the frozen plan")
    if kind == "stock" and payload.get("operation") != "quality_growth_fixed_csd":
        raise CurrentSampleSnapshotError("stock raw operation differs from the fixed adapter")
    if kind == "stock":
        _require_exact_keys(payload, {"operation", "request", "records"}, "stock raw")
        _require_exact_keys(
            request,
            {"instrument_id", "start_date", "end_date", "adjustment", "indicators", "options"},
            "stock raw request",
        )
        if (
            request.get("indicators")
            != list(ChoiceProvider._QUALITY_GROWTH_CSD_INDICATORS)
            or request.get("options") != ChoiceProvider._quality_growth_csd_options("qfq")
            or normalized.get("upstream_source")
            != "choice.eastmoney_emquantapi.csd.quality_growth_fixed"
            or [item.get("code") for item in normalized.get("issues", [])]
            != ["choice_quality_growth_historical_capture_not_formal_truth"]
        ):
            raise CurrentSampleSnapshotError("stock fixed adapter contract drifted")
        stock_fields = {
            "instrument_id", "trading_date", "adjustment",
            *(item.lower() for item in ChoiceProvider._QUALITY_GROWTH_CSD_INDICATORS),
        }
        if any(set(item) != stock_fields for item in records):
            raise CurrentSampleSnapshotError("stock record field set drifted")
    if kind == "benchmark":
        _require_exact_keys(
            payload,
            {
                "operation", "raw_semantics", "request", "response_dates",
                "calendar_completeness_status", "records",
            },
            "benchmark raw",
        )
        _require_exact_keys(
            request,
            {
                "series", "instrument_id", "start_date", "end_date", "adjustment",
                "indicators", "options", "fill_policy", "fallback_allowed",
            },
            "benchmark raw request",
        )
        if (
            payload.get("operation") != "choice_fixed_csi800_benchmark_csd"
            or payload.get("raw_semantics") != "canonicalized_sdk_projection"
            or request.get("series") != "price"
            or request.get("indicators")
            != list(ChoiceProvider._CSI800_BENCHMARK_PROBE_INDICATORS)
            or request.get("options") != ChoiceProvider._csi800_benchmark_probe_options()
            or request.get("fill_policy") != "no_fill_returned_dates_only"
            or request.get("fallback_allowed") is not False
            or payload.get("response_dates")
            != [str(item.get("trading_date") or "") for item in records]
            or normalized.get("upstream_source")
            != "choice.eastmoney_emquantapi.csd.csi800_benchmark_fixed_range"
            or [item.get("code") for item in normalized.get("issues", [])]
            != [
                "choice_benchmark_calendar_reconciliation_required",
                "choice_benchmark_not_officially_authenticated",
            ]
        ):
            raise CurrentSampleSnapshotError("benchmark raw projection differs from the fixed adapter")
        benchmark_fields = {
            "series", "instrument_id", "trading_date", "adjustment", "fill_policy",
            "calendar_completeness_status", "point_in_time_eligible",
            "formal_truth_eligible",
            *(item.lower() for item in ChoiceProvider._CSI800_BENCHMARK_PROBE_INDICATORS),
        }
        if any(
            set(item) != benchmark_fields
            or item.get("series") != "price"
            or item.get("fill_policy") != "no_fill_returned_dates_only"
            or item.get("point_in_time_eligible") is not False
            or item.get("formal_truth_eligible") is not False
            for item in records
        ):
            raise CurrentSampleSnapshotError("benchmark record contract drifted")


def _write(path: Path, content: bytes, artifacts: dict[str, str], root: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(content)
    relative = path.relative_to(root).as_posix()
    artifacts[relative] = sha256_bytes(content)


def _write_json(
    path: Path, value: Mapping[str, Any], artifacts: dict[str, str], root: Path
) -> None:
    _write(path, canonical_json_bytes(value), artifacts, root)


def _calendar_sessions(payload: ProviderPayload) -> tuple[str, ...]:
    sessions: list[str] = []
    for record in payload.records:
        if record.get("is_trading_day") is True:
            day = str(record.get("calendar_date") or "")
            try:
                parsed = date.fromisoformat(day)
            except ValueError as exc:
                raise CurrentSampleSnapshotError("Choice calendar contains an invalid date") from exc
            if not FIXED_CALENDAR_START <= parsed <= FIXED_SNAPSHOT_DATE:
                raise CurrentSampleSnapshotError("Choice calendar date is outside the fixed window")
            sessions.append(day)
    if sessions != sorted(set(sessions)) or len(sessions) < FIXED_SESSION_COUNT:
        raise CurrentSampleSnapshotError("Choice calendar has fewer than 121 unique sessions")
    selected = tuple(sessions[-FIXED_SESSION_COUNT:])
    if selected[-1] != FIXED_SNAPSHOT_DATE.isoformat():
        raise CurrentSampleSnapshotError("Choice calendar does not end on the frozen snapshot date")
    return selected


def _price_records(
    payload: ProviderPayload,
    *,
    instrument_id: str,
    expected_sessions: Sequence[str],
    expected_adjustment: str,
) -> tuple[Mapping[str, Any], ...]:
    records = tuple(dict(item) for item in payload.records)
    dates = [str(item.get("trading_date") or "") for item in records]
    if dates != list(expected_sessions):
        raise CurrentSampleSnapshotError(
            f"{instrument_id} does not exactly cover the controlled 121 sessions"
        )
    for item in records:
        if item.get("instrument_id") != instrument_id:
            raise CurrentSampleSnapshotError(f"{instrument_id} record identity drifted")
        if item.get("adjustment") != expected_adjustment:
            raise CurrentSampleSnapshotError(f"{instrument_id} adjustment drifted")
        for field in ("close", "high"):
            try:
                numeric = float(item.get(field))
            except (TypeError, ValueError) as exc:
                raise CurrentSampleSnapshotError(
                    f"{instrument_id} {field} is not numeric"
                ) from exc
            if not numeric > 0:
                raise CurrentSampleSnapshotError(
                    f"{instrument_id} {field} must be positive"
                )
    return records


def _bars(
    instrument_id: str, records: Sequence[Mapping[str, Any]]
) -> tuple[DiagnosticPriceBar, ...]:
    return tuple(
        DiagnosticPriceBar(
            instrument_id=instrument_id,
            trading_date=date.fromisoformat(str(item["trading_date"])),
            close=float(item["close"]),
            high=float(item["high"]),
            available_at=datetime.combine(
                date.fromisoformat(str(item["trading_date"])),
                time(15, 30),
                tzinfo=CHINA_TZ,
            ),
        )
        for item in records
    )


def _snapshot_payload(
    rows: Sequence[Any],
    *,
    sample: Mapping[str, Any],
    sessions: Sequence[str],
) -> Mapping[str, Any]:
    serialized = [
        {
            "instrument_id": row.instrument_id,
            "trading_date": row.trading_date.isoformat(),
            "values": {
                factor_id: float(row.values[factor_id])
                for factor_id in DIAGNOSTIC_FACTOR_IDS
            },
        }
        for row in rows
        if row.trading_date == FIXED_SNAPSHOT_DATE
    ]
    serialized.sort(key=lambda item: item["instrument_id"])
    if len(serialized) != 60 or {item["instrument_id"] for item in serialized} != set(
        sample["instrument_ids"]
    ):
        raise CurrentSampleSnapshotError("factor snapshot does not exactly cover the 60 names")
    content = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "status": DIAGNOSTIC_STATUS,
        "representation": sample.get("representation"),
        "information_cutoff_date": FIXED_INFORMATION_CUTOFF_DATE.isoformat(),
        "snapshot_date": FIXED_SNAPSHOT_DATE.isoformat(),
        "session_count": FIXED_SESSION_COUNT,
        "session_start": sessions[0],
        "session_end": sessions[-1],
        "benchmark_id": FIXED_BENCHMARK_ID,
        "relative_return_basis": (
            "stock_qfq_minus_csi800_price_index_not_total_return"
        ),
        "factor_ids": list(DIAGNOSTIC_FACTOR_IDS),
        "rows": serialized,
        "historical_backtest_run": False,
        "ranking_or_signal_generated": False,
        "source_authenticated": False,
        "formal_truth_eligible": False,
        "safety": dict(SAFETY),
    }
    return {
        **content,
        "snapshot_content_sha256": sha256_bytes(canonical_json_bytes(content)),
    }


def collect_current_sample_snapshot(
    provider: SnapshotProvider,
    *,
    membership_dir: Path | str,
    industry_dir: Path | str,
    sample_dir: Path | str,
    output_dir: Path | str,
) -> Mapping[str, Any]:
    """Capture the fixed 60-name, 121-session diagnostic factor snapshot."""

    membership_path = Path(membership_dir)
    industry_path = Path(industry_dir)
    sample_path = Path(sample_dir)
    destination = Path(output_dir)
    if os.path.lexists(destination):
        raise CurrentSampleSnapshotError("snapshot output directory already exists")
    destination_resolved = destination.resolve(strict=False)
    for label, source in (
        ("membership", membership_path),
        ("industry", industry_path),
        ("sample", sample_path),
    ):
        source_resolved = source.resolve(strict=True)
        if (
            destination_resolved == source_resolved
            or source_resolved in destination_resolved.parents
            or destination_resolved in source_resolved.parents
        ):
            raise CurrentSampleSnapshotError(
                f"snapshot output directory overlaps the controlled {label} input"
            )
    sample, sample_manifest, sample_artifact_sha256 = _validated_sample(
        membership_path, industry_path, sample_path
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.partial-", dir=destination.parent)
    )
    artifacts: dict[str, str] = {}
    try:
        bundle = _code_bundle()
        instruments = tuple(str(item) for item in sample["instrument_ids"])
        with provider.diagnostic_session():
            calendar = provider.fetch_quality_growth_calendar(
                FIXED_CALENDAR_START, FIXED_SNAPSHOT_DATE
            )
            sessions = _calendar_sessions(calendar)
            query_start = date.fromisoformat(sessions[0])
            stock_payloads: dict[str, ProviderPayload] = {}
            for instrument_id in instruments:
                stock_payloads[instrument_id] = provider.fetch_quality_growth_csd(
                    instrument_id,
                    query_start,
                    FIXED_SNAPSHOT_DATE,
                    adjustment="qfq",
                )
            benchmark = provider.fetch_csi800_price_index_csd(
                query_start, FIXED_SNAPSHOT_DATE
            )

        _validate_capture_time(calendar, "calendar")
        for instrument_id, payload in stock_payloads.items():
            _validate_capture_time(payload, instrument_id)
        _validate_capture_time(benchmark, "benchmark")

        calendar_normalized = _normalized_payload(calendar)
        _verify_raw_projection(
            raw=calendar.raw_content,
            normalized=calendar_normalized,
            kind="calendar",
            start_date=FIXED_CALENDAR_START,
            end_date=FIXED_SNAPSHOT_DATE,
        )
        _write(temporary / "raw" / "calendar.json", calendar.raw_content, artifacts, temporary)
        _write_json(
            temporary / "normalized" / "calendar.json",
            calendar_normalized,
            artifacts,
            temporary,
        )
        stock_records: dict[str, tuple[Mapping[str, Any], ...]] = {}
        for instrument_id in instruments:
            payload = stock_payloads[instrument_id]
            normalized = _normalized_payload(payload)
            _verify_raw_projection(
                raw=payload.raw_content,
                normalized=normalized,
                kind="stock",
                instrument_id=instrument_id,
                start_date=query_start,
                end_date=FIXED_SNAPSHOT_DATE,
                adjustment="qfq",
            )
            stock_records[instrument_id] = _price_records(
                payload,
                instrument_id=instrument_id,
                expected_sessions=sessions,
                expected_adjustment="qfq",
            )
            _write(
                temporary / "raw" / "stocks" / f"{instrument_id}.json",
                payload.raw_content,
                artifacts,
                temporary,
            )
            _write_json(
                temporary / "normalized" / "stocks" / f"{instrument_id}.json",
                normalized,
                artifacts,
                temporary,
            )
        benchmark_normalized = _normalized_payload(benchmark)
        _verify_raw_projection(
            raw=benchmark.raw_content,
            normalized=benchmark_normalized,
            kind="benchmark",
            instrument_id=FIXED_BENCHMARK_ID,
            start_date=query_start,
            end_date=FIXED_SNAPSHOT_DATE,
            adjustment="none",
        )
        benchmark_records = _price_records(
            benchmark,
            instrument_id=FIXED_BENCHMARK_ID,
            expected_sessions=sessions,
            expected_adjustment="none",
        )
        _write(
            temporary / "raw" / "benchmark_price.json",
            benchmark.raw_content,
            artifacts,
            temporary,
        )
        _write_json(
            temporary / "normalized" / "benchmark_price.json",
            benchmark_normalized,
            artifacts,
            temporary,
        )

        factor_rows = compute_price_diagnostics(
            (
                bar
                for instrument_id in instruments
                for bar in _bars(instrument_id, stock_records[instrument_id])
            ),
            _bars(FIXED_BENCHMARK_ID, benchmark_records),
            allowed_instrument_ids=instruments,
        )
        snapshot = _snapshot_payload(
            factor_rows, sample=sample, sessions=sessions
        )
        coverage = {
            "status": "complete",
            "required_instrument_count": 60,
            "covered_instrument_count": 60,
            "required_session_count": FIXED_SESSION_COUNT,
            "common_session_count": len(sessions),
            "session_start": sessions[0],
            "session_end": sessions[-1],
            "benchmark_id": FIXED_BENCHMARK_ID,
            "benchmark_session_count": len(benchmark_records),
            "stock_session_counts": {
                instrument_id: len(stock_records[instrument_id])
                for instrument_id in instruments
            },
            "calendar_source_authenticated": False,
            "availability_semantics": "policy_estimated_same_session_1530_asia_shanghai",
            "safety": dict(SAFETY),
        }
        plan_content = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "status": DIAGNOSTIC_STATUS,
            "information_cutoff_date": FIXED_INFORMATION_CUTOFF_DATE.isoformat(),
            "sample_artifact_sha256": sample_artifact_sha256,
            "sample_content_sha256": sample["sample_content_sha256"],
            "sample_payload_sha256": sample["sample_payload_sha256"],
            "sample_manifest_payload_sha256": sample_manifest[
                "manifest_payload_sha256"
            ],
            "instrument_ids": list(instruments),
            "calendar_query_start": FIXED_CALENDAR_START.isoformat(),
            "query_start": sessions[0],
            "query_end": sessions[-1],
            "session_count": FIXED_SESSION_COUNT,
            "stock_adjustment": "qfq",
            "benchmark_id": FIXED_BENCHMARK_ID,
            "benchmark_adjustment": "none",
            "relative_return_basis": (
                "stock_qfq_minus_csi800_price_index_not_total_return"
            ),
            "factor_ids": list(DIAGNOSTIC_FACTOR_IDS),
            "provider_adapter_version": provider.adapter_version,
            "code_bundle": bundle,
            "source_authenticated": False,
            "formal_truth_eligible": False,
            "historical_backtest_run": False,
            "ranking_or_signal_generated": False,
            "safety": dict(SAFETY),
        }
        _write_json(temporary / "plan.json", plan_content, artifacts, temporary)
        _write_json(temporary / "coverage.json", coverage, artifacts, temporary)
        _write_json(temporary / "factor_snapshot.json", snapshot, artifacts, temporary)

        manifest_content = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "status": DIAGNOSTIC_STATUS,
            "artifacts": dict(sorted(artifacts.items())),
            "sample_artifact_sha256": sample_artifact_sha256,
            "snapshot_content_sha256": snapshot["snapshot_content_sha256"],
            "source_authenticated": False,
            "formal_truth_eligible": False,
            "historical_backtest_run": False,
            "ranking_or_signal_generated": False,
            "safety": dict(SAFETY),
        }
        manifest = {
            **manifest_content,
            "manifest_payload_sha256": sha256_bytes(
                canonical_json_bytes(manifest_content)
            ),
        }
        manifest_raw = canonical_json_bytes(manifest)
        with (temporary / "manifest.json").open("xb") as handle:
            handle.write(manifest_raw)
        if _code_bundle() != bundle:
            raise CurrentSampleSnapshotError("snapshot code bundle changed during capture")
        if os.path.lexists(destination):
            raise CurrentSampleSnapshotError("snapshot output appeared during capture")
        os.replace(temporary, destination)
        return manifest
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _actual_files(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }


def _expected_artifact_paths(instruments: Sequence[str]) -> set[str]:
    return {
        "plan.json",
        "coverage.json",
        "factor_snapshot.json",
        "raw/calendar.json",
        "normalized/calendar.json",
        "raw/benchmark_price.json",
        "normalized/benchmark_price.json",
        *(f"raw/stocks/{item}.json" for item in instruments),
        *(f"normalized/stocks/{item}.json" for item in instruments),
    }


def _normalized_artifact(path: Path) -> Mapping[str, Any]:
    payload = _load_canonical(path, path.name)
    records = payload.get("records")
    if not isinstance(records, list) or any(not isinstance(item, Mapping) for item in records):
        raise CurrentSampleSnapshotError("normalized artifact records are malformed")
    return payload


def _records_from_normalized(path: Path) -> tuple[Mapping[str, Any], ...]:
    payload = _normalized_artifact(path)
    records = payload["records"]
    return tuple(dict(item) for item in records)


def verify_current_sample_snapshot(
    *,
    membership_dir: Path | str,
    industry_dir: Path | str,
    sample_dir: Path | str,
    output_dir: Path | str,
) -> Mapping[str, Any]:
    """Verify hashes and recompute the factor snapshot without loading Choice."""

    root = Path(output_dir)
    if not root.is_dir():
        raise CurrentSampleSnapshotError("snapshot output directory is missing")
    sample, sample_manifest, sample_artifact_sha256 = _validated_sample(
        Path(membership_dir), Path(industry_dir), Path(sample_dir)
    )
    manifest = dict(_load_canonical(root / "manifest.json", "snapshot manifest"))
    _require_exact_keys(
        manifest,
        {
            "schema_version", "status", "artifacts", "sample_artifact_sha256",
            "snapshot_content_sha256", "source_authenticated", "formal_truth_eligible",
            "historical_backtest_run", "ranking_or_signal_generated", "safety",
            "manifest_payload_sha256",
        },
        "snapshot manifest",
    )
    declared = manifest.pop("manifest_payload_sha256", None)
    if declared != sha256_bytes(canonical_json_bytes(manifest)):
        raise CurrentSampleSnapshotError("snapshot manifest payload hash mismatch")
    manifest["manifest_payload_sha256"] = declared
    if (
        manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION
        or manifest.get("status") != DIAGNOSTIC_STATUS
        or manifest.get("safety") != SAFETY
        or manifest.get("source_authenticated") is not False
        or manifest.get("formal_truth_eligible") is not False
        or manifest.get("historical_backtest_run") is not False
        or manifest.get("ranking_or_signal_generated") is not False
    ):
        raise CurrentSampleSnapshotError("snapshot manifest safety contract drifted")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping) or not artifacts:
        raise CurrentSampleSnapshotError("snapshot manifest artifacts are missing")
    instruments = tuple(str(item) for item in sample["instrument_ids"])
    expected_artifacts = _expected_artifact_paths(instruments)
    if set(artifacts) != expected_artifacts:
        raise CurrentSampleSnapshotError("snapshot manifest artifact set drifted")
    expected_files = expected_artifacts | {"manifest.json"}
    if _actual_files(root) != expected_files:
        raise CurrentSampleSnapshotError("snapshot artifact file set differs from manifest")
    for relative, expected_hash in artifacts.items():
        path = root / str(relative)
        if sha256_bytes(path.read_bytes()) != expected_hash:
            raise CurrentSampleSnapshotError(f"snapshot artifact hash mismatch: {relative}")

    plan = _load_canonical(root / "plan.json", "snapshot plan")
    coverage = _load_canonical(root / "coverage.json", "snapshot coverage")
    stored_snapshot = _load_canonical(root / "factor_snapshot.json", "factor snapshot")
    _require_exact_keys(
        plan,
        {
            "schema_version", "status", "information_cutoff_date",
            "sample_artifact_sha256", "sample_content_sha256", "sample_payload_sha256",
            "sample_manifest_payload_sha256", "instrument_ids", "calendar_query_start",
            "query_start", "query_end", "session_count", "stock_adjustment",
            "benchmark_id", "benchmark_adjustment", "relative_return_basis", "factor_ids",
            "provider_adapter_version", "code_bundle", "source_authenticated",
            "formal_truth_eligible", "historical_backtest_run", "ranking_or_signal_generated",
            "safety",
        },
        "snapshot plan",
    )
    _require_exact_keys(
        coverage,
        {
            "status", "required_instrument_count", "covered_instrument_count",
            "required_session_count", "common_session_count", "session_start", "session_end",
            "benchmark_id", "benchmark_session_count", "stock_session_counts",
            "calendar_source_authenticated", "availability_semantics", "safety",
        },
        "snapshot coverage",
    )
    _require_exact_keys(
        stored_snapshot,
        {
            "schema_version", "status", "representation", "information_cutoff_date",
            "snapshot_date", "session_count", "session_start", "session_end", "benchmark_id",
            "relative_return_basis", "factor_ids", "rows", "historical_backtest_run",
            "ranking_or_signal_generated", "source_authenticated", "formal_truth_eligible",
            "safety", "snapshot_content_sha256",
        },
        "factor snapshot",
    )
    if (
        plan.get("schema_version") != SNAPSHOT_SCHEMA_VERSION
        or plan.get("status") != DIAGNOSTIC_STATUS
        or plan.get("information_cutoff_date") != FIXED_INFORMATION_CUTOFF_DATE.isoformat()
        or plan.get("sample_artifact_sha256") != sample_artifact_sha256
        or plan.get("sample_content_sha256") != sample["sample_content_sha256"]
        or plan.get("sample_payload_sha256") != sample["sample_payload_sha256"]
        or plan.get("sample_manifest_payload_sha256")
        != sample_manifest["manifest_payload_sha256"]
        or plan.get("instrument_ids") != list(instruments)
        or plan.get("benchmark_id") != FIXED_BENCHMARK_ID
        or plan.get("calendar_query_start") != FIXED_CALENDAR_START.isoformat()
        or plan.get("query_end") != FIXED_SNAPSHOT_DATE.isoformat()
        or plan.get("session_count") != FIXED_SESSION_COUNT
        or plan.get("stock_adjustment") != "qfq"
        or plan.get("benchmark_adjustment") != "none"
        or plan.get("relative_return_basis")
        != "stock_qfq_minus_csi800_price_index_not_total_return"
        or plan.get("factor_ids") != list(DIAGNOSTIC_FACTOR_IDS)
        or plan.get("safety") != SAFETY
        or plan.get("source_authenticated") is not False
        or plan.get("formal_truth_eligible") is not False
        or plan.get("historical_backtest_run") is not False
        or plan.get("ranking_or_signal_generated") is not False
        or plan.get("code_bundle") != _code_bundle()
    ):
        raise CurrentSampleSnapshotError("snapshot plan differs from controlled inputs")
    if (
        coverage.get("status") != "complete"
        or coverage.get("required_instrument_count") != 60
        or coverage.get("covered_instrument_count") != 60
        or coverage.get("required_session_count") != FIXED_SESSION_COUNT
        or coverage.get("common_session_count") != FIXED_SESSION_COUNT
        or coverage.get("benchmark_id") != FIXED_BENCHMARK_ID
        or coverage.get("benchmark_session_count") != FIXED_SESSION_COUNT
        or coverage.get("session_start") != plan.get("query_start")
        or coverage.get("session_end") != FIXED_SNAPSHOT_DATE.isoformat()
        or coverage.get("stock_session_counts")
        != {instrument_id: FIXED_SESSION_COUNT for instrument_id in instruments}
        or coverage.get("calendar_source_authenticated") is not False
        or coverage.get("availability_semantics")
        != "policy_estimated_same_session_1530_asia_shanghai"
        or coverage.get("safety") != SAFETY
    ):
        raise CurrentSampleSnapshotError("snapshot coverage is not complete")
    calendar_normalized = _normalized_artifact(root / "normalized" / "calendar.json")
    _verify_raw_projection(
        raw=(root / "raw" / "calendar.json").read_bytes(),
        normalized=calendar_normalized,
        kind="calendar",
        start_date=FIXED_CALENDAR_START,
        end_date=FIXED_SNAPSHOT_DATE,
    )
    sessions = tuple(
        str(item.get("calendar_date"))
        for item in calendar_normalized["records"]
        if item.get("is_trading_day") is True
    )[-FIXED_SESSION_COUNT:]
    if (
        len(sessions) != FIXED_SESSION_COUNT
        or sessions[0] != plan.get("query_start")
        or sessions[-1] != FIXED_SNAPSHOT_DATE.isoformat()
    ):
        raise CurrentSampleSnapshotError("snapshot normalized calendar differs from plan")
    stock_records: dict[str, tuple[Mapping[str, Any], ...]] = {}
    for instrument_id in instruments:
        normalized_path = root / "normalized" / "stocks" / f"{instrument_id}.json"
        raw_path = root / "raw" / "stocks" / f"{instrument_id}.json"
        normalized = _normalized_artifact(normalized_path)
        _verify_raw_projection(
            raw=raw_path.read_bytes(),
            normalized=normalized,
            kind="stock",
            instrument_id=instrument_id,
            start_date=date.fromisoformat(sessions[0]),
            end_date=FIXED_SNAPSHOT_DATE,
            adjustment="qfq",
        )
        records = tuple(dict(item) for item in normalized["records"])
        stock_records[instrument_id] = _price_records(
            ProviderPayload(
                raw_content=b"verified-offline",
                records=records,
                fetched_at=datetime.now(timezone.utc),
                upstream_source="offline.snapshot.verify",
            ),
            instrument_id=instrument_id,
            expected_sessions=sessions,
            expected_adjustment="qfq",
        )
    benchmark_normalized = _normalized_artifact(
        root / "normalized" / "benchmark_price.json"
    )
    _verify_raw_projection(
        raw=(root / "raw" / "benchmark_price.json").read_bytes(),
        normalized=benchmark_normalized,
        kind="benchmark",
        instrument_id=FIXED_BENCHMARK_ID,
        start_date=date.fromisoformat(sessions[0]),
        end_date=FIXED_SNAPSHOT_DATE,
        adjustment="none",
    )
    benchmark_records = _price_records(
        ProviderPayload(
            raw_content=b"verified-offline",
            records=tuple(dict(item) for item in benchmark_normalized["records"]),
            fetched_at=datetime.now(timezone.utc),
            upstream_source="offline.snapshot.verify",
        ),
        instrument_id=FIXED_BENCHMARK_ID,
        expected_sessions=sessions,
        expected_adjustment="none",
    )
    recomputed_rows = compute_price_diagnostics(
        (
            bar
            for instrument_id in instruments
            for bar in _bars(instrument_id, stock_records[instrument_id])
        ),
        _bars(FIXED_BENCHMARK_ID, benchmark_records),
        allowed_instrument_ids=instruments,
    )
    recomputed = _snapshot_payload(recomputed_rows, sample=sample, sessions=sessions)
    if (
        stored_snapshot.get("schema_version") != SNAPSHOT_SCHEMA_VERSION
        or stored_snapshot.get("status") != DIAGNOSTIC_STATUS
        or stored_snapshot.get("information_cutoff_date")
        != FIXED_INFORMATION_CUTOFF_DATE.isoformat()
        or stored_snapshot.get("snapshot_date") != FIXED_SNAPSHOT_DATE.isoformat()
        or stored_snapshot.get("relative_return_basis")
        != "stock_qfq_minus_csi800_price_index_not_total_return"
        or stored_snapshot.get("source_authenticated") is not False
        or stored_snapshot.get("formal_truth_eligible") is not False
        or stored_snapshot.get("historical_backtest_run") is not False
        or stored_snapshot.get("ranking_or_signal_generated") is not False
        or stored_snapshot.get("safety") != SAFETY
    ):
        raise CurrentSampleSnapshotError("factor snapshot safety contract drifted")
    rows = stored_snapshot.get("rows")
    if (
        not isinstance(rows, list)
        or len(rows) != 60
        or any(
            not isinstance(item, Mapping)
            or set(item) != {"instrument_id", "trading_date", "values"}
            or item.get("trading_date") != FIXED_SNAPSHOT_DATE.isoformat()
            or not isinstance(item.get("values"), Mapping)
            or set(item["values"]) != set(DIAGNOSTIC_FACTOR_IDS)
            for item in rows
        )
    ):
        raise CurrentSampleSnapshotError("factor snapshot rows drifted")
    if canonical_json_bytes(recomputed) != canonical_json_bytes(stored_snapshot):
        raise CurrentSampleSnapshotError("factor snapshot differs from offline recomputation")
    if manifest.get("snapshot_content_sha256") != recomputed["snapshot_content_sha256"]:
        raise CurrentSampleSnapshotError("snapshot content hash differs from manifest")
    return manifest


__all__ = [
    "CurrentSampleSnapshotError",
    "FIXED_BENCHMARK_ID",
    "FIXED_SESSION_COUNT",
    "FIXED_SNAPSHOT_DATE",
    "collect_current_sample_snapshot",
    "verify_current_sample_snapshot",
]
