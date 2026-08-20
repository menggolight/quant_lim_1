"""Fixed 2026-08-19 close snapshot for the sealed 60-name diagnostic sample.

V2 deliberately preserves the V1 artifact verifier byte-for-byte.  It reuses
the already sealed 2026-08-18 current-universe sample, but captures a new,
append-only 121-session price panel ending on 2026-08-19.  The result remains
diagnostic only: no ranking, signal, portfolio, Paper admission, trade list, or
LIVE capability is produced.
"""

from __future__ import annotations

import os
import platform
import shutil
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from agent.current_industry_import import verify_controlled_sample
from research.market_data.contracts import canonical_json_bytes, sha256_bytes
from research.market_data.providers.base import ProviderPayload
from research.market_data.providers.choice import ChoiceProvider
from research.strategy_workspace.current_sample_snapshot import (
    CurrentSampleSnapshotError,
    _actual_files,
    _bars,
    _expected_artifact_paths,
    _load_canonical,
    _normalized_artifact,
    _normalized_payload,
    _price_records,
    _require_exact_keys,
    _verify_raw_projection,
    _write,
    _write_json,
)
from research.strategy_workspace.diagnostic import (
    DIAGNOSTIC_FACTOR_IDS,
    DIAGNOSTIC_STATUS,
    compute_price_diagnostics,
)


SNAPSHOT_SCHEMA_VERSION = "strategy-current-sample-factor-snapshot-v2"
MANIFEST_SCHEMA_VERSION = "strategy-current-sample-factor-snapshot-manifest-v2"
FIXED_CALENDAR_START = date(2026, 1, 1)
FIXED_SAMPLE_MARKET_SNAPSHOT_DATE = date(2026, 8, 18)
FIXED_SAMPLE_INFORMATION_CUTOFF_DATE = date(2026, 8, 19)
FIXED_TARGET_SNAPSHOT_DATE = date(2026, 8, 19)
FIXED_CAPTURE_INFORMATION_CUTOFF_DATE = date(2026, 8, 20)
FIXED_SESSION_COUNT = 121
FIXED_BENCHMARK_ID = "000906.SH"
CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
AVAILABILITY_SEMANTICS = (
    "historical_backfill_captured_on_frozen_cutoff_not_original_close_capture"
)
SAFETY = {
    "paper_eligibility": False,
    "trade_eligibility": False,
    "real_money_list_allowed": False,
    "live": "not_supported",
}
CODE_BUNDLE_PATHS = (
    "agent/current_industry_import.py",
    "agent/current_sample_snapshot_v2.py",
    "research/market_data/contracts.py",
    "research/market_data/providers/choice.py",
    "research/strategy_workspace/current_sample_snapshot.py",
    "research/strategy_workspace/current_sample_snapshot_v2.py",
    "research/strategy_workspace/diagnostic.py",
)


class SnapshotV2Provider(Protocol):
    adapter_version: str

    def diagnostic_session(self): ...

    def fetch_quality_growth_calendar(
        self, start_date: date, end_date: date
    ) -> ProviderPayload: ...

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
        raise CurrentSampleSnapshotError(
            "controlled sample status differs from diagnostic contract"
        )
    if (
        sample.get("market_snapshot_date")
        != FIXED_SAMPLE_MARKET_SNAPSHOT_DATE.isoformat()
    ):
        raise CurrentSampleSnapshotError(
            "controlled sample market snapshot date differs from the sealed sample"
        )
    if (
        sample.get("information_cutoff_date")
        != FIXED_SAMPLE_INFORMATION_CUTOFF_DATE.isoformat()
    ):
        raise CurrentSampleSnapshotError(
            "controlled sample information cutoff differs from the sealed sample"
        )
    instruments = sample.get("instrument_ids")
    if (
        not isinstance(instruments, list)
        or len(instruments) != 60
        or len(set(instruments)) != 60
    ):
        raise CurrentSampleSnapshotError(
            "controlled sample must contain exactly 60 unique names"
        )
    if sample.get("factor_ids") != list(DIAGNOSTIC_FACTOR_IDS):
        raise CurrentSampleSnapshotError("controlled sample factor family drifted")
    if sample.get("safety") != SAFETY:
        raise CurrentSampleSnapshotError("controlled sample safety contract drifted")
    return sample, verified_manifest, sha256_bytes(sample_path.read_bytes())


def _validate_capture_time(payload: ProviderPayload, label: str) -> None:
    if payload.fetched_at.tzinfo is None or payload.fetched_at.utcoffset() is None:
        raise CurrentSampleSnapshotError(
            f"{label} capture timestamp lacks a timezone"
        )
    local_day = payload.fetched_at.astimezone(CHINA_TZ).date()
    if local_day != FIXED_CAPTURE_INFORMATION_CUTOFF_DATE:
        raise CurrentSampleSnapshotError(
            f"{label} was not captured on the frozen V2 information-cutoff date"
        )


def _validate_normalized_capture_time(
    normalized: Mapping[str, Any], label: str
) -> None:
    raw = normalized.get("fetched_at")
    if not isinstance(raw, str):
        raise CurrentSampleSnapshotError(f"{label} normalized fetched_at is missing")
    try:
        fetched_at = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise CurrentSampleSnapshotError(
            f"{label} normalized fetched_at is invalid"
        ) from exc
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise CurrentSampleSnapshotError(
            f"{label} normalized fetched_at lacks a timezone"
        )
    if fetched_at.astimezone(CHINA_TZ).date() != FIXED_CAPTURE_INFORMATION_CUTOFF_DATE:
        raise CurrentSampleSnapshotError(
            f"{label} normalized capture date differs from the frozen V2 cutoff"
        )


def _calendar_sessions(payload: ProviderPayload) -> tuple[str, ...]:
    sessions: list[str] = []
    for record in payload.records:
        if record.get("is_trading_day") is True:
            day = str(record.get("calendar_date") or "")
            try:
                parsed = date.fromisoformat(day)
            except ValueError as exc:
                raise CurrentSampleSnapshotError(
                    "Choice calendar contains an invalid date"
                ) from exc
            if not FIXED_CALENDAR_START <= parsed <= FIXED_TARGET_SNAPSHOT_DATE:
                raise CurrentSampleSnapshotError(
                    "Choice calendar date is outside the fixed V2 window"
                )
            sessions.append(day)
    if sessions != sorted(set(sessions)) or len(sessions) < FIXED_SESSION_COUNT:
        raise CurrentSampleSnapshotError(
            "Choice calendar has fewer than 121 unique sessions"
        )
    selected = tuple(sessions[-FIXED_SESSION_COUNT:])
    if selected[-1] != FIXED_TARGET_SNAPSHOT_DATE.isoformat():
        raise CurrentSampleSnapshotError(
            "Choice calendar does not end on the frozen V2 snapshot date"
        )
    return selected


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
        if row.trading_date == FIXED_TARGET_SNAPSHOT_DATE
    ]
    serialized.sort(key=lambda item: item["instrument_id"])
    if len(serialized) != 60 or {
        item["instrument_id"] for item in serialized
    } != set(sample["instrument_ids"]):
        raise CurrentSampleSnapshotError(
            "V2 factor snapshot does not exactly cover the sealed 60 names"
        )
    content = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "status": DIAGNOSTIC_STATUS,
        "representation": sample.get("representation"),
        "sample_market_snapshot_date": FIXED_SAMPLE_MARKET_SNAPSHOT_DATE.isoformat(),
        "sample_information_cutoff_date": (
            FIXED_SAMPLE_INFORMATION_CUTOFF_DATE.isoformat()
        ),
        "capture_information_cutoff_date": (
            FIXED_CAPTURE_INFORMATION_CUTOFF_DATE.isoformat()
        ),
        "snapshot_date": FIXED_TARGET_SNAPSHOT_DATE.isoformat(),
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


def collect_current_sample_snapshot_v2(
    provider: SnapshotV2Provider,
    *,
    membership_dir: Path | str,
    industry_dir: Path | str,
    sample_dir: Path | str,
    output_dir: Path | str,
) -> Mapping[str, Any]:
    """Capture the fixed 2026-08-19 diagnostic cross-section append-only."""

    membership_path = Path(membership_dir)
    industry_path = Path(industry_dir)
    sample_path = Path(sample_dir)
    destination = Path(output_dir)
    if os.path.lexists(destination):
        raise CurrentSampleSnapshotError("V2 snapshot output directory already exists")
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
                f"V2 output directory overlaps the controlled {label} input"
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
                FIXED_CALENDAR_START, FIXED_TARGET_SNAPSHOT_DATE
            )
            sessions = _calendar_sessions(calendar)
            query_start = date.fromisoformat(sessions[0])
            stock_payloads: dict[str, ProviderPayload] = {}
            for instrument_id in instruments:
                stock_payloads[instrument_id] = provider.fetch_quality_growth_csd(
                    instrument_id,
                    query_start,
                    FIXED_TARGET_SNAPSHOT_DATE,
                    adjustment="qfq",
                )
            benchmark = provider.fetch_csi800_price_index_csd(
                query_start, FIXED_TARGET_SNAPSHOT_DATE
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
            end_date=FIXED_TARGET_SNAPSHOT_DATE,
        )
        _write(
            temporary / "raw" / "calendar.json",
            calendar.raw_content,
            artifacts,
            temporary,
        )
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
                end_date=FIXED_TARGET_SNAPSHOT_DATE,
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
            end_date=FIXED_TARGET_SNAPSHOT_DATE,
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
            "availability_semantics": AVAILABILITY_SEMANTICS,
            "safety": dict(SAFETY),
        }
        plan_content = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "status": DIAGNOSTIC_STATUS,
            "sample_market_snapshot_date": (
                FIXED_SAMPLE_MARKET_SNAPSHOT_DATE.isoformat()
            ),
            "sample_information_cutoff_date": (
                FIXED_SAMPLE_INFORMATION_CUTOFF_DATE.isoformat()
            ),
            "capture_information_cutoff_date": (
                FIXED_CAPTURE_INFORMATION_CUTOFF_DATE.isoformat()
            ),
            "target_snapshot_date": FIXED_TARGET_SNAPSHOT_DATE.isoformat(),
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
        with (temporary / "manifest.json").open("xb") as handle:
            handle.write(canonical_json_bytes(manifest))
        if _code_bundle() != bundle:
            raise CurrentSampleSnapshotError(
                "V2 snapshot code bundle changed during capture"
            )
        if os.path.lexists(destination):
            raise CurrentSampleSnapshotError("V2 snapshot output appeared during capture")
        os.replace(temporary, destination)
        return manifest
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def verify_current_sample_snapshot_v2(
    *,
    membership_dir: Path | str,
    industry_dir: Path | str,
    sample_dir: Path | str,
    output_dir: Path | str,
) -> Mapping[str, Any]:
    """Verify all V2 hashes and recompute the snapshot without loading Choice."""

    root = Path(output_dir)
    if not root.is_dir():
        raise CurrentSampleSnapshotError("V2 snapshot output directory is missing")
    sample, sample_manifest, sample_artifact_sha256 = _validated_sample(
        Path(membership_dir), Path(industry_dir), Path(sample_dir)
    )
    manifest = dict(_load_canonical(root / "manifest.json", "V2 snapshot manifest"))
    _require_exact_keys(
        manifest,
        {
            "schema_version",
            "status",
            "artifacts",
            "sample_artifact_sha256",
            "snapshot_content_sha256",
            "source_authenticated",
            "formal_truth_eligible",
            "historical_backtest_run",
            "ranking_or_signal_generated",
            "safety",
            "manifest_payload_sha256",
        },
        "V2 snapshot manifest",
    )
    declared = manifest.pop("manifest_payload_sha256", None)
    if declared != sha256_bytes(canonical_json_bytes(manifest)):
        raise CurrentSampleSnapshotError("V2 snapshot manifest payload hash mismatch")
    manifest["manifest_payload_sha256"] = declared
    if (
        manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION
        or manifest.get("status") != DIAGNOSTIC_STATUS
        or manifest.get("safety") != SAFETY
        or manifest.get("source_authenticated") is not False
        or manifest.get("formal_truth_eligible") is not False
        or manifest.get("historical_backtest_run") is not False
        or manifest.get("ranking_or_signal_generated") is not False
        or manifest.get("sample_artifact_sha256") != sample_artifact_sha256
    ):
        raise CurrentSampleSnapshotError("V2 snapshot manifest safety contract drifted")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping) or not artifacts:
        raise CurrentSampleSnapshotError("V2 snapshot manifest artifacts are missing")
    instruments = tuple(str(item) for item in sample["instrument_ids"])
    expected_artifacts = _expected_artifact_paths(instruments)
    if set(artifacts) != expected_artifacts:
        raise CurrentSampleSnapshotError("V2 snapshot manifest artifact set drifted")
    if _actual_files(root) != expected_artifacts | {"manifest.json"}:
        raise CurrentSampleSnapshotError(
            "V2 snapshot artifact file set differs from manifest"
        )
    for relative, expected_hash in artifacts.items():
        if sha256_bytes((root / str(relative)).read_bytes()) != expected_hash:
            raise CurrentSampleSnapshotError(
                f"V2 snapshot artifact hash mismatch: {relative}"
            )

    plan = _load_canonical(root / "plan.json", "V2 snapshot plan")
    coverage = _load_canonical(root / "coverage.json", "V2 snapshot coverage")
    stored_snapshot = _load_canonical(
        root / "factor_snapshot.json", "V2 factor snapshot"
    )
    _require_exact_keys(
        plan,
        {
            "schema_version",
            "status",
            "sample_market_snapshot_date",
            "sample_information_cutoff_date",
            "capture_information_cutoff_date",
            "target_snapshot_date",
            "sample_artifact_sha256",
            "sample_content_sha256",
            "sample_payload_sha256",
            "sample_manifest_payload_sha256",
            "instrument_ids",
            "calendar_query_start",
            "query_start",
            "query_end",
            "session_count",
            "stock_adjustment",
            "benchmark_id",
            "benchmark_adjustment",
            "relative_return_basis",
            "factor_ids",
            "provider_adapter_version",
            "code_bundle",
            "source_authenticated",
            "formal_truth_eligible",
            "historical_backtest_run",
            "ranking_or_signal_generated",
            "safety",
        },
        "V2 snapshot plan",
    )
    _require_exact_keys(
        coverage,
        {
            "status",
            "required_instrument_count",
            "covered_instrument_count",
            "required_session_count",
            "common_session_count",
            "session_start",
            "session_end",
            "benchmark_id",
            "benchmark_session_count",
            "stock_session_counts",
            "calendar_source_authenticated",
            "availability_semantics",
            "safety",
        },
        "V2 snapshot coverage",
    )
    _require_exact_keys(
        stored_snapshot,
        {
            "schema_version",
            "status",
            "representation",
            "sample_market_snapshot_date",
            "sample_information_cutoff_date",
            "capture_information_cutoff_date",
            "snapshot_date",
            "session_count",
            "session_start",
            "session_end",
            "benchmark_id",
            "relative_return_basis",
            "factor_ids",
            "rows",
            "historical_backtest_run",
            "ranking_or_signal_generated",
            "source_authenticated",
            "formal_truth_eligible",
            "safety",
            "snapshot_content_sha256",
        },
        "V2 factor snapshot",
    )
    if (
        plan.get("schema_version") != SNAPSHOT_SCHEMA_VERSION
        or plan.get("status") != DIAGNOSTIC_STATUS
        or plan.get("sample_market_snapshot_date")
        != FIXED_SAMPLE_MARKET_SNAPSHOT_DATE.isoformat()
        or plan.get("sample_information_cutoff_date")
        != FIXED_SAMPLE_INFORMATION_CUTOFF_DATE.isoformat()
        or plan.get("capture_information_cutoff_date")
        != FIXED_CAPTURE_INFORMATION_CUTOFF_DATE.isoformat()
        or plan.get("target_snapshot_date")
        != FIXED_TARGET_SNAPSHOT_DATE.isoformat()
        or plan.get("sample_artifact_sha256") != sample_artifact_sha256
        or plan.get("sample_content_sha256") != sample["sample_content_sha256"]
        or plan.get("sample_payload_sha256") != sample["sample_payload_sha256"]
        or plan.get("sample_manifest_payload_sha256")
        != sample_manifest["manifest_payload_sha256"]
        or plan.get("instrument_ids") != list(instruments)
        or plan.get("benchmark_id") != FIXED_BENCHMARK_ID
        or plan.get("calendar_query_start") != FIXED_CALENDAR_START.isoformat()
        or plan.get("query_end") != FIXED_TARGET_SNAPSHOT_DATE.isoformat()
        or plan.get("session_count") != FIXED_SESSION_COUNT
        or plan.get("stock_adjustment") != "qfq"
        or plan.get("benchmark_adjustment") != "none"
        or plan.get("relative_return_basis")
        != "stock_qfq_minus_csi800_price_index_not_total_return"
        or plan.get("factor_ids") != list(DIAGNOSTIC_FACTOR_IDS)
        or plan.get("provider_adapter_version") != ChoiceProvider.adapter_version
        or plan.get("code_bundle") != _code_bundle()
        or plan.get("source_authenticated") is not False
        or plan.get("formal_truth_eligible") is not False
        or plan.get("historical_backtest_run") is not False
        or plan.get("ranking_or_signal_generated") is not False
        or plan.get("safety") != SAFETY
    ):
        raise CurrentSampleSnapshotError(
            "V2 snapshot plan differs from the controlled inputs"
        )
    if (
        coverage.get("status") != "complete"
        or coverage.get("required_instrument_count") != 60
        or coverage.get("covered_instrument_count") != 60
        or coverage.get("required_session_count") != FIXED_SESSION_COUNT
        or coverage.get("common_session_count") != FIXED_SESSION_COUNT
        or coverage.get("benchmark_id") != FIXED_BENCHMARK_ID
        or coverage.get("benchmark_session_count") != FIXED_SESSION_COUNT
        or coverage.get("session_start") != plan.get("query_start")
        or coverage.get("session_end")
        != FIXED_TARGET_SNAPSHOT_DATE.isoformat()
        or coverage.get("stock_session_counts")
        != {instrument_id: FIXED_SESSION_COUNT for instrument_id in instruments}
        or coverage.get("calendar_source_authenticated") is not False
        or coverage.get("availability_semantics") != AVAILABILITY_SEMANTICS
        or coverage.get("safety") != SAFETY
    ):
        raise CurrentSampleSnapshotError("V2 snapshot coverage is not complete")

    calendar_normalized = _normalized_artifact(
        root / "normalized" / "calendar.json"
    )
    _validate_normalized_capture_time(calendar_normalized, "calendar")
    _verify_raw_projection(
        raw=(root / "raw" / "calendar.json").read_bytes(),
        normalized=calendar_normalized,
        kind="calendar",
        start_date=FIXED_CALENDAR_START,
        end_date=FIXED_TARGET_SNAPSHOT_DATE,
    )
    sessions = tuple(
        str(item.get("calendar_date"))
        for item in calendar_normalized["records"]
        if item.get("is_trading_day") is True
    )[-FIXED_SESSION_COUNT:]
    if (
        len(sessions) != FIXED_SESSION_COUNT
        or sessions[0] != plan.get("query_start")
        or sessions[-1] != FIXED_TARGET_SNAPSHOT_DATE.isoformat()
    ):
        raise CurrentSampleSnapshotError(
            "V2 normalized calendar differs from the plan"
        )

    stock_records: dict[str, tuple[Mapping[str, Any], ...]] = {}
    for instrument_id in instruments:
        normalized_path = root / "normalized" / "stocks" / f"{instrument_id}.json"
        raw_path = root / "raw" / "stocks" / f"{instrument_id}.json"
        normalized = _normalized_artifact(normalized_path)
        _validate_normalized_capture_time(normalized, instrument_id)
        _verify_raw_projection(
            raw=raw_path.read_bytes(),
            normalized=normalized,
            kind="stock",
            instrument_id=instrument_id,
            start_date=date.fromisoformat(sessions[0]),
            end_date=FIXED_TARGET_SNAPSHOT_DATE,
            adjustment="qfq",
        )
        records = tuple(dict(item) for item in normalized["records"])
        stock_records[instrument_id] = _price_records(
            ProviderPayload(
                raw_content=b"verified-offline",
                records=records,
                fetched_at=datetime.now(timezone.utc),
                upstream_source="offline.snapshot.v2.verify",
            ),
            instrument_id=instrument_id,
            expected_sessions=sessions,
            expected_adjustment="qfq",
        )

    benchmark_normalized = _normalized_artifact(
        root / "normalized" / "benchmark_price.json"
    )
    _validate_normalized_capture_time(benchmark_normalized, "benchmark")
    _verify_raw_projection(
        raw=(root / "raw" / "benchmark_price.json").read_bytes(),
        normalized=benchmark_normalized,
        kind="benchmark",
        instrument_id=FIXED_BENCHMARK_ID,
        start_date=date.fromisoformat(sessions[0]),
        end_date=FIXED_TARGET_SNAPSHOT_DATE,
        adjustment="none",
    )
    benchmark_records = _price_records(
        ProviderPayload(
            raw_content=b"verified-offline",
            records=tuple(
                dict(item) for item in benchmark_normalized["records"]
            ),
            fetched_at=datetime.now(timezone.utc),
            upstream_source="offline.snapshot.v2.verify",
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
    recomputed = _snapshot_payload(
        recomputed_rows, sample=sample, sessions=sessions
    )
    if (
        stored_snapshot.get("schema_version") != SNAPSHOT_SCHEMA_VERSION
        or stored_snapshot.get("status") != DIAGNOSTIC_STATUS
        or stored_snapshot.get("sample_market_snapshot_date")
        != FIXED_SAMPLE_MARKET_SNAPSHOT_DATE.isoformat()
        or stored_snapshot.get("sample_information_cutoff_date")
        != FIXED_SAMPLE_INFORMATION_CUTOFF_DATE.isoformat()
        or stored_snapshot.get("capture_information_cutoff_date")
        != FIXED_CAPTURE_INFORMATION_CUTOFF_DATE.isoformat()
        or stored_snapshot.get("snapshot_date")
        != FIXED_TARGET_SNAPSHOT_DATE.isoformat()
        or stored_snapshot.get("relative_return_basis")
        != "stock_qfq_minus_csi800_price_index_not_total_return"
        or stored_snapshot.get("source_authenticated") is not False
        or stored_snapshot.get("formal_truth_eligible") is not False
        or stored_snapshot.get("historical_backtest_run") is not False
        or stored_snapshot.get("ranking_or_signal_generated") is not False
        or stored_snapshot.get("safety") != SAFETY
    ):
        raise CurrentSampleSnapshotError("V2 factor snapshot safety contract drifted")
    rows = stored_snapshot.get("rows")
    if (
        not isinstance(rows, list)
        or len(rows) != 60
        or any(
            not isinstance(item, Mapping)
            or set(item) != {"instrument_id", "trading_date", "values"}
            or item.get("trading_date")
            != FIXED_TARGET_SNAPSHOT_DATE.isoformat()
            or not isinstance(item.get("values"), Mapping)
            or set(item["values"]) != set(DIAGNOSTIC_FACTOR_IDS)
            for item in rows
        )
    ):
        raise CurrentSampleSnapshotError("V2 factor snapshot rows drifted")
    if canonical_json_bytes(recomputed) != canonical_json_bytes(stored_snapshot):
        raise CurrentSampleSnapshotError(
            "V2 factor snapshot differs from offline recomputation"
        )
    if manifest.get("snapshot_content_sha256") != recomputed[
        "snapshot_content_sha256"
    ]:
        raise CurrentSampleSnapshotError(
            "V2 snapshot content hash differs from manifest"
        )
    return manifest


__all__ = [
    "FIXED_CAPTURE_INFORMATION_CUTOFF_DATE",
    "FIXED_SAMPLE_INFORMATION_CUTOFF_DATE",
    "FIXED_SAMPLE_MARKET_SNAPSHOT_DATE",
    "FIXED_TARGET_SNAPSHOT_DATE",
    "collect_current_sample_snapshot_v2",
    "verify_current_sample_snapshot_v2",
]
