"""Run the independent, pre-Locked Tushare Alpha Feasibility workflow.

The command has no Paper, broker, account, order, or Locked Test capability.
It first validates the frozen experiment (including the seven-endpoint
allowlist and the absolute 2023-12-31 cutoff), and only then delegates token
lookup and create-only collection to the data lane.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
import json
import re
import subprocess
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from research.market_data import tushare_alpha_feasibility as data_lane
from research.strategy_workspace import alpha_feasibility as engine
from research.strategy_workspace import alpha_feasibility_reporting as reporting


DEFAULT_OUTPUT_ROOT = Path("data/tmp/alpha-feasibility/tushare-p1-v1")
PRECOLLECTION_BLOCKED_EVIDENCE_FILENAME = "precollection_blocked_evidence.json"
SAFE_BLOCKER = re.compile(r"^[a-z0-9_]{3,96}$")
COMPACT_DATE = re.compile(
    r"(?<![0-9])(20[0-9]{2})(0[1-9]|1[0-2])([0-3][0-9])(?![0-9])"
)
READY_STAGE = "DATA_READY_FOR_ALPHA_FEASIBILITY"
BLOCKED_STAGES = frozenset(
    {"BLOCKED_PIT_MEMBERSHIP", "BLOCKED_DATA", "BLOCKED_ADAPTER_PROTOCOL"}
)
LOCKED_TEST_STATUS = {
    "access": "NOT_ACCESSED",
    "download": "NOT_DOWNLOADED",
    "run": "NOT_RUN",
}
_COVERAGE_FIELDS = (
    "daily_coverage_status",
    "adj_factor_coverage_status",
    "suspension_coverage_status",
    "benchmark_coverage_status",
)


class AlphaFeasibilityWorkflowError(RuntimeError):
    """Sanitized integration failure with no provider or credential text."""

    def __init__(self, code: str) -> None:
        safe = code if SAFE_BLOCKER.fullmatch(str(code)) else "unsafe_error_sanitized"
        self.code = safe
        super().__init__(safe)


def _stable_precollection_blocked_timestamp(
    *,
    output_root: Path,
    requested: datetime | None,
    collection_plan_sha256: str,
    blocker: str,
) -> datetime:
    """Create/replay a timestamp claim for failures before collector artifacts."""

    target = output_root / PRECOLLECTION_BLOCKED_EVIDENCE_FILENAME
    if target.exists():
        try:
            value = data_lane.strict_json_loads(
                target.read_bytes(), label="precollection_blocked_evidence"
            )
        except (OSError, ValueError) as exc:
            raise AlphaFeasibilityWorkflowError(
                "precollection_blocked_evidence_unreadable"
            ) from exc
        if not isinstance(value, Mapping):
            raise AlphaFeasibilityWorkflowError(
                "precollection_blocked_evidence_invalid"
            )
        unsigned = dict(value)
        declared_hash = unsigned.pop("evidence_sha256", None)
        if (
            set(value)
            != {
                "schema_version",
                "generated_at",
                "collection_plan_sha256",
                "blocker",
                "locked_test_status",
                "locked_test_consumed",
                "evidence_sha256",
            }
            or value.get("schema_version")
            != "alpha-feasibility-precollection-blocked-evidence.v1"
            or value.get("collection_plan_sha256") != collection_plan_sha256
            or value.get("blocker") != blocker
            or value.get("locked_test_status") != LOCKED_TEST_STATUS
            or value.get("locked_test_consumed") is not False
            or declared_hash != data_lane.canonical_sha256(unsigned)
        ):
            raise AlphaFeasibilityWorkflowError(
                "precollection_blocked_evidence_invalid"
            )
        parsed = _parse_generated_at(value.get("generated_at"))
        if parsed is None:
            raise AlphaFeasibilityWorkflowError(
                "precollection_blocked_evidence_invalid"
            )
        if requested is not None and requested.isoformat() != parsed.isoformat():
            raise AlphaFeasibilityWorkflowError(
                "precollection_blocked_timestamp_drift"
            )
        return parsed

    generated = requested or datetime.now(reporting.CHINA_TZ)
    payload = {
        "schema_version": "alpha-feasibility-precollection-blocked-evidence.v1",
        "generated_at": generated.isoformat(),
        "collection_plan_sha256": collection_plan_sha256,
        "blocker": blocker,
        "locked_test_status": dict(LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
    }
    payload["evidence_sha256"] = data_lane.canonical_sha256(payload)
    content = data_lane.canonical_json_bytes(payload) + b"\n"
    try:
        output_root.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as handle:
            handle.write(content)
    except FileExistsError:
        return _stable_precollection_blocked_timestamp(
            output_root=output_root,
            requested=requested,
            collection_plan_sha256=collection_plan_sha256,
            blocker=blocker,
        )
    except OSError as exc:
        raise AlphaFeasibilityWorkflowError(
            "precollection_blocked_evidence_unwritable"
        ) from exc
    return generated


def _parse_generated_at(value: str | None) -> datetime | None:
    if value is None:
        return None
    if type(value) is not str:
        raise AlphaFeasibilityWorkflowError("generated_at_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError as exc:
        raise AlphaFeasibilityWorkflowError("generated_at_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AlphaFeasibilityWorkflowError("generated_at_timezone_required")
    return parsed


def _current_commit_sha(repository_root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AlphaFeasibilityWorkflowError("git_head_unavailable") from exc
    value = completed.stdout.strip()
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise AlphaFeasibilityWorkflowError("git_head_invalid")
    return value


def _coverage_status(value: Any) -> str:
    mapping = {
        "COMPLETE": "complete",
        "complete": "complete",
        "BLOCKED_DATA": "blocked",
        "blocked": "blocked",
        "NOT_RUN": "not_run",
        "not_run": "not_run",
    }
    try:
        return mapping[value]
    except (KeyError, TypeError) as exc:
        raise AlphaFeasibilityWorkflowError("data_coverage_status_invalid") from exc


def _safe_blockers(value: Any, *, fallback: str | None = None) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise AlphaFeasibilityWorkflowError("data_blockers_invalid")
    blockers: set[str] = set()
    for item in value:
        candidate = str(item)
        if SAFE_BLOCKER.fullmatch(candidate) is None:
            blockers.add("unsafe_blocker_sanitized")
            continue
        future_compact_date = any(
            f"{match.group(1)}-{match.group(2)}" > "2023-12"
            for match in COMPACT_DATE.finditer(candidate)
        )
        blockers.add(
            "post_cutoff_data_rejected" if future_compact_date else candidate
        )
    if fallback is not None and not blockers:
        blockers.add(fallback)
    return sorted(blockers)


def _reporting_data_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    """Whitelist and normalize the collector summary for the report boundary."""

    if not isinstance(result, Mapping):
        raise AlphaFeasibilityWorkflowError("data_result_invalid")
    stage = result.get("stage_status")
    if stage not in BLOCKED_STAGES | {READY_STAGE}:
        raise AlphaFeasibilityWorkflowError("data_stage_status_invalid")
    if result.get("locked_test_status") != LOCKED_TEST_STATUS:
        raise AlphaFeasibilityWorkflowError("locked_test_status_drift")
    if result.get("locked_test_consumed") is not False:
        raise AlphaFeasibilityWorkflowError("locked_test_consumed")
    if result.get("execution_realism") != "INCOMPLETE":
        raise AlphaFeasibilityWorkflowError("execution_realism_drift")
    if result.get("trade_eligibility") is not False:
        raise AlphaFeasibilityWorkflowError("trade_eligibility_drift")

    counts = result.get("actual_tushare_request_count_by_endpoint")
    if not isinstance(counts, Mapping) or set(counts) != set(reporting.ALLOWED_ENDPOINTS):
        raise AlphaFeasibilityWorkflowError("request_count_endpoints_invalid")
    safe_counts: dict[str, int] = {}
    for endpoint in reporting.ALLOWED_ENDPOINTS:
        value = counts[endpoint]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AlphaFeasibilityWorkflowError("request_count_invalid")
        safe_counts[endpoint] = value

    if result.get("coverage_start") != reporting.COVERAGE_START:
        raise AlphaFeasibilityWorkflowError("coverage_start_drift")
    if result.get("coverage_end") != reporting.COVERAGE_END:
        raise AlphaFeasibilityWorkflowError("coverage_end_drift")
    if result.get("pit_months_expected") != reporting.PIT_MONTHS_EXPECTED:
        raise AlphaFeasibilityWorkflowError("pit_months_expected_drift")

    pit_observed = result.get("pit_months_observed")
    union_count = result.get("union_instrument_count")
    for value, code in (
        (pit_observed, "pit_months_observed_invalid"),
        (union_count, "union_instrument_count_invalid"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AlphaFeasibilityWorkflowError(code)
    if pit_observed > reporting.PIT_MONTHS_EXPECTED:
        raise AlphaFeasibilityWorkflowError("pit_months_observed_invalid")

    provenance: dict[str, str | None] = {}
    for field in (
        "collection_plan_sha256",
        "pit_membership_manifest_sha256",
        "history_manifest_sha256",
    ):
        value = result.get(field)
        if value is not None and (
            not isinstance(value, str)
            or re.fullmatch(r"[0-9a-f]{64}", value) is None
        ):
            raise AlphaFeasibilityWorkflowError(f"{field}_invalid")
        provenance[field] = value

    blocked = stage in BLOCKED_STAGES
    terminal = result.get("terminal_status")
    expected_blocked_terminal = (
        "BLOCKED_ADAPTER_PROTOCOL"
        if stage == "BLOCKED_ADAPTER_PROTOCOL"
        else "BLOCKED_DATA"
    )
    if blocked and terminal != expected_blocked_terminal:
        raise AlphaFeasibilityWorkflowError("blocked_terminal_status_invalid")
    if not blocked and terminal is not None:
        raise AlphaFeasibilityWorkflowError("ready_terminal_status_invalid")
    blockers = _safe_blockers(
        result.get("remaining_blockers", []),
        fallback=stage.casefold() if blocked else None,
    )
    if not blocked and blockers:
        raise AlphaFeasibilityWorkflowError("ready_data_has_blockers")
    if not blocked and any(value is None for value in provenance.values()):
        raise AlphaFeasibilityWorkflowError("ready_data_provenance_incomplete")

    return {
        "actual_tushare_request_count_by_endpoint": safe_counts,
        "coverage_start": reporting.COVERAGE_START,
        "coverage_end": reporting.COVERAGE_END,
        "pit_months_expected": reporting.PIT_MONTHS_EXPECTED,
        "pit_months_observed": pit_observed,
        "union_instrument_count": union_count,
        **provenance,
        **{field: _coverage_status(result.get(field)) for field in _COVERAGE_FIELDS},
        "data_status": "READY" if stage == READY_STAGE else stage,
        "remaining_blockers": blockers,
        "locked_test_status": dict(LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
        "safety": dict(reporting.SAFETY),
    }


def _as_date(value: Any, field: str) -> date:
    if type(value) is date:
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise AlphaFeasibilityWorkflowError(f"{field}_invalid") from exc
    else:
        raise AlphaFeasibilityWorkflowError(f"{field}_invalid")
    if parsed > engine.LATEST_ALLOWED_DATE:
        raise AlphaFeasibilityWorkflowError("post_cutoff_input_rejected")
    return parsed


def _require_mapping(value: Any, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AlphaFeasibilityWorkflowError(code)
    return value


def build_alpha_input(value: Mapping[str, Any]) -> engine.AlphaFeasibilityInput:
    """Convert the verified data-lane dictionaries into the engine contract."""

    payload = _require_mapping(value, "feasibility_inputs_invalid")
    if payload.get("locked_test_status") != LOCKED_TEST_STATUS:
        raise AlphaFeasibilityWorkflowError("loaded_locked_test_status_drift")
    if payload.get("locked_test_consumed") is not False:
        raise AlphaFeasibilityWorkflowError("loaded_locked_test_consumed")
    if payload.get("execution_realism") != "INCOMPLETE":
        raise AlphaFeasibilityWorkflowError("loaded_execution_realism_drift")
    if payload.get("trade_eligibility") is not False:
        raise AlphaFeasibilityWorkflowError("loaded_trade_eligibility_drift")

    pit_report = _require_mapping(
        payload.get("pit_coverage_report"), "pit_coverage_report_invalid"
    )
    pit_manifest = _require_mapping(payload.get("pit_manifest"), "pit_manifest_invalid")
    pit_admission = engine.PITAdmissionArtifacts(
        coverage_report=pit_report,
        manifest=pit_manifest,
    )

    snapshots: list[engine.PITMembershipSnapshot] = []
    raw_snapshots = payload.get("pit_snapshots")
    if not isinstance(raw_snapshots, Sequence) or isinstance(raw_snapshots, (str, bytes)):
        raise AlphaFeasibilityWorkflowError("pit_snapshots_invalid")
    for raw in raw_snapshots:
        item = _require_mapping(raw, "pit_snapshot_invalid")
        members = item.get("members")
        if not isinstance(members, Sequence) or isinstance(members, (str, bytes)):
            raise AlphaFeasibilityWorkflowError("pit_snapshot_members_invalid")
        snapshots.append(
            engine.PITMembershipSnapshot(
                snapshot_date=_as_date(item.get("snapshot_date"), "pit_snapshot_date"),
                members=tuple(str(member) for member in members),
            )
        )

    raw_signal_bars = payload.get("signal_bars")
    if not isinstance(raw_signal_bars, Iterable) or isinstance(
        raw_signal_bars, (str, bytes, Mapping)
    ):
        raise AlphaFeasibilityWorkflowError("signal_bars_invalid")

    def signal_bars() -> Iterable[engine.SignalBar]:
        for raw in raw_signal_bars:
            item = _require_mapping(raw, "signal_bar_invalid")
            # ``open`` is the adjusted D+1 execution value produced by the
            # data lane.  Never let SignalBar's compatibility default use close.
            if "open" not in item or item["open"] is None:
                raise AlphaFeasibilityWorkflowError("adjusted_open_required")
            instrument_id = item.get("instrument_id", item.get("ts_code"))
            yield engine.SignalBar(
                trading_date=_as_date(item.get("trading_date"), "signal_date"),
                instrument_id=str(instrument_id or ""),
                close=item.get("close"),
                high=item.get("high"),
                open=item["open"],
            )

    benchmark_bars: list[engine.BenchmarkBar] = []
    raw_benchmark_bars = payload.get("benchmark_bars")
    if not isinstance(raw_benchmark_bars, Sequence) or isinstance(
        raw_benchmark_bars, (str, bytes)
    ):
        raise AlphaFeasibilityWorkflowError("benchmark_bars_invalid")
    for raw in raw_benchmark_bars:
        item = _require_mapping(raw, "benchmark_bar_invalid")
        benchmark_bars.append(
            engine.BenchmarkBar(
                trading_date=_as_date(item.get("trading_date"), "benchmark_date"),
                close=item.get("close"),
                high=item.get("high"),
            )
        )

    raw_suspensions = payload.get("suspensions")
    if not isinstance(raw_suspensions, Iterable) or isinstance(
        raw_suspensions, (str, bytes, Mapping)
    ):
        raise AlphaFeasibilityWorkflowError("suspensions_invalid")

    def suspensions() -> Iterable[engine.SuspensionRecord]:
        for raw in raw_suspensions:
            item = _require_mapping(raw, "suspension_invalid")
            yield engine.SuspensionRecord(
                trading_date=_as_date(item.get("trading_date"), "suspension_date"),
                instrument_id=str(item.get("instrument_id", item.get("ts_code")) or ""),
            )

    raw_trading_dates = payload.get("trading_dates")
    if not isinstance(raw_trading_dates, Sequence) or isinstance(
        raw_trading_dates, (str, bytes)
    ):
        raise AlphaFeasibilityWorkflowError("trading_dates_invalid")
    return engine.AlphaFeasibilityInput(
        coverage_start=_as_date(payload.get("coverage_start"), "coverage_start"),
        coverage_end=_as_date(payload.get("coverage_end"), "coverage_end"),
        trading_dates=tuple(
            _as_date(item, "trading_date") for item in raw_trading_dates
        ),
        memberships=tuple(snapshots),
        stock_signal_bars=signal_bars(),
        benchmark_signal_bars=tuple(benchmark_bars),
        suspensions=suspensions(),
        pit_admission=pit_admission,
    )


def _metric_mapping(metrics: engine.AlphaFeasibilityMetrics) -> dict[str, Any]:
    # The engine owns the exact sixteen-field report projection, including
    # ``worst_month.period -> month`` and omission of its internal counters.
    # Its JSON-safe representation uses decimal strings; convert only the
    # known numeric leaves back to Decimal for the reporting numeric boundary.
    payload = metrics.to_dict()
    scalar_fields = (
        "net_return",
        "benchmark_return",
        "net_active_return",
        "max_drawdown",
        "annualized_turnover",
        "total_cost",
        "average_gross_exposure",
        "cash_day_fraction",
        "positive_month_rate",
    )
    for field in scalar_fields:
        payload[field] = Decimal(str(payload[field]))
    payload["exposure_state_distribution"] = {
        state: Decimal(str(value))
        for state, value in payload["exposure_state_distribution"].items()
    }
    payload["per_stock_pnl_contribution"] = {
        instrument: Decimal(str(value))
        for instrument, value in payload["per_stock_pnl_contribution"].items()
    }
    payload["worst_month"] = {
        **payload["worst_month"],
        **{
            field: Decimal(str(payload["worst_month"][field]))
            for field in ("net_return", "benchmark_return", "net_active_return")
        },
    }
    for field in ("largest_stock_pnl_share", "largest_10_days_pnl_share"):
        if payload[field] is not None:
            payload[field] = Decimal(str(payload[field]))
    return payload


def _study_metrics(
    comparison: engine.AlphaFeasibilityComparison,
) -> dict[str, Mapping[str, Any]]:
    return {
        "base": _metric_mapping(comparison.base.metrics),
        "stress": _metric_mapping(comparison.stress.metrics),
    }


def _blocked_after_ready(
    summary: Mapping[str, Any], blocker: str
) -> dict[str, Any]:
    return {
        **summary,
        "data_status": "BLOCKED_DATA",
        "remaining_blockers": [
            blocker if SAFE_BLOCKER.fullmatch(blocker) else "unsafe_blocker_sanitized"
        ],
    }


def _safe_report_summary(report: Mapping[str, Any], report_path: Path) -> dict[str, Any]:
    fields = (
        "commit_sha",
        "actual_tushare_request_count_by_endpoint",
        "coverage_start",
        "coverage_end",
        "pit_months_expected",
        "pit_months_observed",
        "union_instrument_count",
        "collection_plan_sha256",
        "pit_membership_manifest_sha256",
        "history_manifest_sha256",
        "experiment_config_canonical_sha256",
        "alpha_feasibility_engine_version",
        "alpha_feasibility_engine_sha256",
        "reporting_gate_source_sha256",
        *_COVERAGE_FIELDS,
        "development_metrics",
        "validation_metrics",
        "concentration_metrics",
        "terminal_status",
        "locked_test_status",
        "locked_test_consumed",
        "remaining_blockers",
    )
    terminal = report["terminal_status"]
    return {
        "status": (
            "blocked"
            if terminal in {"BLOCKED_DATA", "BLOCKED_ADAPTER_PROTOCOL"}
            else "completed"
        ),
        **{field: report[field] for field in fields},
        "report_path": str(report_path.resolve()),
    }


def run_workflow(
    *,
    command: str,
    config_path: Path,
    output_root: Path,
    generated_at: str | None = None,
) -> tuple[int, dict[str, Any]]:
    if command not in {"data", "all"}:
        raise AlphaFeasibilityWorkflowError("command_invalid")

    # This is intentionally the first filesystem/config action.  It proves
    # frozen dates, endpoint allowlist, schemas, and implementation hashes
    # before the collector may inspect TUSHARE_TOKEN or touch output_root.
    experiment = reporting.load_and_validate_experiment_config(config_path)
    timestamp = _parse_generated_at(generated_at)
    try:
        result = data_lane.run_backfill_from_environment(
            config_path=config_path,
            output_root=output_root,
            generated_at=timestamp,
        )
    except data_lane.AlphaFeasibilityDataError as exc:
        # Config preflight already passed.  Protocol-envelope failures retain
        # their explicit adapter terminal; all other lane failures remain
        # BLOCKED_DATA. Counts remain conservative durable claims.
        plan = data_lane.load_config_and_build_plan(config_path)
        try:
            counts = data_lane.actual_tushare_request_count_by_endpoint(
                output_root,
                plan_sha256=plan.plan_sha256,
            )
            blocker = exc.code
        except data_lane.AlphaFeasibilityDataError:
            counts = {endpoint: 0 for endpoint in reporting.ALLOWED_ENDPOINTS}
            blocker = "request_count_evidence_unavailable"
        evidence_timestamp = _stable_precollection_blocked_timestamp(
            output_root=output_root,
            requested=timestamp,
            collection_plan_sha256=plan.plan_sha256,
            blocker=blocker,
        )
        adapter_blocked = blocker in data_lane.ADAPTER_PROTOCOL_FAILURES
        blocked_result = {
            "stage_status": (
                "BLOCKED_ADAPTER_PROTOCOL" if adapter_blocked else "BLOCKED_DATA"
            ),
            "terminal_status": (
                "BLOCKED_ADAPTER_PROTOCOL" if adapter_blocked else "BLOCKED_DATA"
            ),
            "generated_at": evidence_timestamp.isoformat(),
            "actual_tushare_request_count_by_endpoint": counts,
            "coverage_start": reporting.COVERAGE_START,
            "coverage_end": reporting.COVERAGE_END,
            "pit_months_expected": reporting.PIT_MONTHS_EXPECTED,
            "pit_months_observed": 0,
            "union_instrument_count": 0,
            "collection_plan_sha256": plan.plan_sha256,
            "pit_membership_manifest_sha256": None,
            "history_manifest_sha256": None,
            **{field: "BLOCKED_DATA" for field in _COVERAGE_FIELDS},
            "remaining_blockers": [blocker],
            "locked_test_status": dict(LOCKED_TEST_STATUS),
            "locked_test_consumed": False,
            "execution_realism": "INCOMPLETE",
            "trade_eligibility": False,
        }
        data_summary = _reporting_data_summary(blocked_result)
        commit_sha = _current_commit_sha(data_lane.REPOSITORY_ROOT)
        report = reporting.build_blocked_alpha_feasibility_report(
            commit_sha=commit_sha,
            data_summary=data_summary,
            experiment=experiment,
            generated_at=blocked_result["generated_at"],
        )
        report_path = reporting.publish_alpha_feasibility_report(
            output_root,
            report,
            experiment=experiment,
        )
        return 1, _safe_report_summary(report, report_path)
    data_summary = _reporting_data_summary(result)
    commit_sha = _current_commit_sha(data_lane.REPOSITORY_ROOT)
    evidence_timestamp = _parse_generated_at(result.get("generated_at"))
    if evidence_timestamp is None:
        raise AlphaFeasibilityWorkflowError("data_generated_at_missing")

    if result["stage_status"] in BLOCKED_STAGES:
        report = reporting.build_blocked_alpha_feasibility_report(
            commit_sha=commit_sha,
            data_summary=data_summary,
            experiment=experiment,
            generated_at=evidence_timestamp,
        )
        report_path = reporting.publish_alpha_feasibility_report(
            output_root,
            report,
            experiment=experiment,
        )
        return 1, _safe_report_summary(report, report_path)

    if command == "data":
        return 0, {
            "status": "data_ready",
            "commit_sha": commit_sha,
            **{
                field: data_summary[field]
                for field in (
                    "actual_tushare_request_count_by_endpoint",
                    "coverage_start",
                    "coverage_end",
                    "pit_months_expected",
                    "pit_months_observed",
                    "union_instrument_count",
                    *_COVERAGE_FIELDS,
                    "locked_test_status",
                    "locked_test_consumed",
                    "remaining_blockers",
                )
            },
            "stage_status": READY_STAGE,
        }

    try:
        loaded = data_lane.load_feasibility_inputs(
            output_root=output_root,
            config_path=config_path,
        )
        inputs = build_alpha_input(loaded)
        study = engine.run_alpha_feasibility_study(inputs=inputs)
    except (
        AlphaFeasibilityWorkflowError,
        data_lane.AlphaFeasibilityDataError,
        engine.AlphaFeasibilityError,
    ) as exc:
        blocker = getattr(exc, "code", "alpha_input_replay_blocked")
        blocked_summary = _blocked_after_ready(data_summary, str(blocker))
        report = reporting.build_blocked_alpha_feasibility_report(
            commit_sha=commit_sha,
            data_summary=blocked_summary,
            experiment=experiment,
            generated_at=evidence_timestamp,
        )
        report_path = reporting.publish_alpha_feasibility_report(
            output_root,
            report,
            experiment=experiment,
        )
        return 1, _safe_report_summary(report, report_path)

    development_metrics = _study_metrics(study.development)
    validation_metrics = _study_metrics(study.validation)
    report = reporting.build_completed_alpha_feasibility_report(
        commit_sha=commit_sha,
        data_summary=data_summary,
        development_metrics=development_metrics,
        validation_metrics=validation_metrics,
        experiment=experiment,
        generated_at=evidence_timestamp,
    )
    report_path = reporting.publish_alpha_feasibility_report(
        output_root,
        report,
        experiment=experiment,
    )
    return 0, _safe_report_summary(report, report_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("command", nargs="?", choices=("data", "all"), default="all")
    parser.add_argument("--config", type=Path, default=reporting.DEFAULT_CONFIG_PATH)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--generated-at")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        exit_code, summary = run_workflow(
            command=args.command,
            config_path=args.config,
            output_root=args.output_root,
            generated_at=args.generated_at,
        )
    except (
        AlphaFeasibilityWorkflowError,
        data_lane.AlphaFeasibilityDataError,
        engine.AlphaFeasibilityError,
        reporting.AlphaFeasibilityReportingError,
        OSError,
        ValueError,
    ) as exc:
        # Never serialize provider text, raw response values, filesystem
        # contents, or credential-derived material on the failure path.
        sys.stderr.write(
            json.dumps(
                {
                    "status": "failed",
                    "reason": "alpha_feasibility_workflow_failed",
                    "error_type": type(exc).__name__,
                    "locked_test_status": dict(LOCKED_TEST_STATUS),
                    "locked_test_consumed": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
        return 2
    sys.stdout.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AlphaFeasibilityWorkflowError",
    "DEFAULT_OUTPUT_ROOT",
    "build_alpha_input",
    "build_parser",
    "main",
    "run_workflow",
]
