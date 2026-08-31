"""Create-only P1.5 run/process receipts around the Alpha data lane."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import hashlib
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping
import uuid
from research.market_data import tushare_alpha_feasibility as data_lane
from research.market_data.validation import SchemaValidationError, validate_json_schema
from research.strategy_workspace import alpha_feasibility_reporting as reporting


LOCKED_TEST_STATUS = {"access": "NOT_ACCESSED", "download": "NOT_DOWNLOADED", "run": "NOT_RUN"}
RUN_CLAIM_FILENAME = "p1_5_network_run_claim.json"
RUN_RECEIPT_FILENAME = "p1_5_run_receipt.json"
PROCESS_DIRECTORY = "p1_5_network_processes"
REVIEW_BRANCH_REF = "refs/remotes/review/codex/project-review-20260820"
RECEIPT_SCHEMA_PATH = data_lane.REPOSITORY_ROOT / "schemas" / "tushare_alpha_feasibility_run_receipt.v1.json"
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,95}$")
SAFE_BLOCKER = re.compile(r"^[a-z0-9_]{3,96}$")
PROCESS_ID = re.compile(r"^[0-9a-f]{32}$")
BASELINE_COMMIT_SEMANTICS = "git_head_baseline_only_runtime_bundle_binds_working_tree_overlay"
REQUEST_COUNT_SEMANTICS = "durable_pre_transport_attempt_claim_conservative"
RUNTIME_PATHS = (
    "operations/run_alpha_feasibility.py",
    "operations/p15_alpha_feasibility_run.py",
    "research/market_data/tushare_alpha_feasibility.py",
    "research/strategy_workspace/alpha_feasibility.py",
    "research/strategy_workspace/alpha_feasibility_reporting.py",
)

class P15RunError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code if SAFE_BLOCKER.fullmatch(str(code)) else "unsafe_error_sanitized"
        super().__init__(self.code)

@dataclass(frozen=True, slots=True)
class RunContext:
    plan: data_lane.CollectionPlan
    claim: Mapping[str, Any]
    network_process_count: int
    resumed_request_fingerprint_count: int
    process_id: str


def _self_hash(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    result = dict(value)
    if field in result:
        raise P15RunError("self_hash_field_preexisting")
    result[field] = data_lane.canonical_sha256(result)
    return result


def _publish(path: Path, value: Mapping[str, Any], code: str) -> Path:
    content = data_lane.canonical_json_bytes(value)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(content)
    except FileExistsError:
        try:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
                raise P15RunError(code)
        except OSError as exc:
            raise P15RunError(code) from exc
    except OSError as exc:
        raise P15RunError(code) from exc
    return path


def _read_self_hashed(path: Path, field: str, code: str) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise P15RunError(code)
    try:
        raw = path.read_bytes()
        value = data_lane.strict_json_loads(raw, label="p15_artifact")
    except (OSError, data_lane.AlphaFeasibilityDataError) as exc:
        raise P15RunError(code) from exc
    if not isinstance(value, Mapping) or raw != data_lane.canonical_json_bytes(value):
        raise P15RunError(code)
    unsigned = dict(value)
    declared = unsigned.pop(field, None)
    if declared != data_lane.canonical_sha256(unsigned):
        raise P15RunError(code)
    return value


def _git_sha(repository_root: Path, ref: str, code: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise P15RunError(code) from exc
    value = completed.stdout.strip()
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise P15RunError(code)
    return value


def _working_tree_clean(repository_root: Path) -> bool:
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise P15RunError("git_working_tree_status_unavailable") from exc
    return completed.stdout == ""


def _runtime_bundle(
    repository_root: Path, config_path: Path
) -> Mapping[str, Any]:
    resolved_root = repository_root.resolve()
    config_candidate = (
        config_path if config_path.is_absolute() else resolved_root / config_path
    )
    schema_root = resolved_root / "schemas"
    if schema_root.is_symlink() or not schema_root.is_dir():
        raise P15RunError("runtime_schema_directory_unavailable")
    candidates = [resolved_root / relative for relative in RUNTIME_PATHS]
    candidates.append(config_candidate)
    candidates.extend(sorted(schema_root.glob("*.json"), key=lambda item: item.name))
    files: dict[str, str] = {}
    config_relative: str | None = None
    resolved_config = config_candidate.resolve()
    for candidate in candidates:
        path = candidate.resolve()
        try:
            relative = path.relative_to(resolved_root).as_posix()
        except ValueError as exc:
            raise P15RunError("runtime_implementation_path_invalid") from exc
        if candidate.is_symlink() or not path.is_file():
            raise P15RunError("runtime_implementation_file_unavailable")
        try:
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise P15RunError("runtime_implementation_file_unavailable") from exc
        if path == resolved_config:
            config_relative = relative
    if config_relative is None or not any(path.startswith("schemas/") for path in files):
        raise P15RunError("runtime_config_or_schema_binding_missing")
    return _self_hash(
        {
            "schema_version": "alpha-feasibility-runtime-implementation-bundle.v1",
            "config_path": config_relative,
            "files": dict(sorted(files.items())),
        },
        "bundle_sha256",
    )


def _runtime_identity(
    repository_root: Path, config_path: Path
) -> Mapping[str, Any]:
    baseline = _git_sha(repository_root, "HEAD", "git_head_unavailable")
    return {
        "code_commit_sha": baseline,
        "baseline_commit_sha": baseline,
        "baseline_commit_semantics": BASELINE_COMMIT_SEMANTICS,
        "remote_branch_sha": _git_sha(
            repository_root, REVIEW_BRANCH_REF, "review_ref_unavailable"
        ),
        "working_tree_clean": _working_tree_clean(repository_root),
        "runtime_implementation_bundle": _runtime_bundle(repository_root, config_path),
    }


def _expected_tasks(output_root: Path, plan: data_lane.CollectionPlan) -> tuple[data_lane.CollectionTask, ...]:
    tasks = list(plan.pit_tasks)
    try:
        pit = data_lane._load_existing_pit_result(output_root, plan)
    except data_lane.AlphaFeasibilityDataError as exc:
        raise P15RunError("pit_manifest_invalid") from exc
    if pit is not None and pit.passed:
        tasks.extend(data_lane.build_history_plan(plan, pit.union_instruments))
    return tuple(tasks)


def _completed_count(output_root: Path, plan: data_lane.CollectionPlan) -> int:
    store = data_lane.CreateOnlyTaskStore(output_root)
    count = 0
    for task in _expected_tasks(output_root, plan):
        if store.is_complete(task):
            try:
                store._load_response(task)
            except data_lane.AlphaFeasibilityDataError as exc:
                raise P15RunError("completed_request_artifact_invalid") from exc
            count += 1
    return count


def _process_records(output_root: Path, claim: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    directory = output_root / PROCESS_DIRECTORY
    if not directory.exists():
        return []
    if directory.is_symlink() or not directory.is_dir():
        raise P15RunError("network_process_journal_invalid")
    records: list[Mapping[str, Any]] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        value = _read_self_hashed(path, "process_sha256", "network_process_journal_invalid")
        try:
            started_at = datetime.fromisoformat(str(value.get("started_at")))
        except ValueError as exc:
            raise P15RunError("network_process_journal_invalid") from exc
        if (
            path.name != f"{value.get('process_id')}.started.json"
            or PROCESS_ID.fullmatch(str(value.get("process_id"))) is None
            or value.get("schema_version") != "tushare-alpha-feasibility-network-process.v1"
            or value.get("network_run_id") != claim.get("network_run_id")
            or value.get("run_claim_sha256") != claim.get("claim_sha256")
            or started_at.tzinfo is None
            or type(value.get("completed_request_fingerprint_count_at_start")) is not int
            or value["completed_request_fingerprint_count_at_start"] < 1
            or value.get("locked_test_status") != LOCKED_TEST_STATUS
            or value.get("locked_test_consumed") is not False
        ):
            raise P15RunError("network_process_journal_invalid")
        records.append(value)
    return records


def _load_alpha_report(
    output_root: Path, experiment: Mapping[str, Any]
) -> Mapping[str, Any]:
    path = output_root / reporting.REPORT_FILENAME
    if path.is_symlink() or not path.is_file():
        raise P15RunError("run_receipt_report_invalid")
    try:
        raw = path.read_bytes()
        report = data_lane.strict_json_loads(raw, label="alpha_report")
    except (OSError, data_lane.AlphaFeasibilityDataError) as exc:
        raise P15RunError("run_receipt_report_invalid") from exc
    if (
        not isinstance(report, Mapping)
        or raw != data_lane.canonical_json_bytes(report)
    ):
        raise P15RunError("run_receipt_report_invalid")
    try:
        reporting.verify_alpha_feasibility_report(report, experiment=experiment)
    except reporting.AlphaFeasibilityReportingError as exc:
        raise P15RunError("run_receipt_report_invalid") from exc
    return report


def load_existing_receipt(
    output_root: Path,
    claim: Mapping[str, Any],
    plan: data_lane.CollectionPlan,
    experiment: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    path = output_root / RUN_RECEIPT_FILENAME
    if not path.exists():
        return None
    receipt = _read_self_hashed(path, "receipt_sha256", "run_receipt_invalid")
    try:
        validate_json_schema(receipt, RECEIPT_SCHEMA_PATH)
    except SchemaValidationError as exc:
        raise P15RunError("run_receipt_invalid") from exc
    claim_fields = (
        "network_run_id",
        "collection_plan_sha256",
        "code_commit_sha",
        "baseline_commit_sha",
        "baseline_commit_semantics",
        "remote_branch_sha",
        "working_tree_clean",
        "runtime_implementation_bundle",
    )
    if any(receipt.get(field) != claim.get(field) for field in claim_fields) or (
        receipt.get("run_claim_sha256") != claim.get("claim_sha256")
    ):
        raise P15RunError("run_receipt_invalid")
    report = _load_alpha_report(output_root, experiment)
    if receipt.get("report_sha256") != report.get("report_sha256"):
        raise P15RunError("run_receipt_report_hash_mismatch")
    records = _process_records(output_root, claim)
    process_id = receipt.get("terminal_process_id")
    terminal = [item for item in records if item.get("process_id") == process_id]
    if len(terminal) != 1:
        raise P15RunError("run_receipt_process_evidence_invalid")
    context = RunContext(
        plan,
        claim,
        len(records),
        terminal[0]["completed_request_fingerprint_count_at_start"],
        str(process_id),
    )
    rebuilt, _ = publish_receipt(
        context=context,
        output_root=output_root,
        source=report,
        experiment=experiment,
    )
    if dict(rebuilt) != dict(receipt):
        raise P15RunError("run_receipt_replay_mismatch")
    return rebuilt


def prepare(
    *,
    network_run_id: str,
    p14d_import_root: Path,
    output_root: Path,
    config_path: Path,
    experiment: Mapping[str, Any],
) -> tuple[RunContext | None, Mapping[str, Any] | None]:
    if RUN_ID.fullmatch(network_run_id) is None or network_run_id in {".", ".."}:
        raise P15RunError("network_run_id_invalid")
    plan = data_lane.load_config_and_build_plan(config_path)
    importer = getattr(data_lane, "import_p14d_diagnostic_into_plan", None)
    if not callable(importer):
        raise P15RunError("p14d_import_api_unavailable")
    try:
        imported = importer(diagnostic_root=p14d_import_root, output_root=output_root, plan=plan)
    except data_lane.AlphaFeasibilityDataError as exc:
        raise P15RunError("p14d_import_failed") from exc
    if (
        getattr(imported, "task", None) != plan.pit_tasks[0]
        or getattr(imported, "request_origin", None) != "offline_p14d_import"
        or getattr(imported, "network_request_count", None) != 0
        or not data_lane.CreateOnlyTaskStore(output_root).is_complete(plan.pit_tasks[0])
    ):
        raise P15RunError("p14d_import_result_invalid")
    runtime_identity = _runtime_identity(data_lane.REPOSITORY_ROOT, config_path)
    claim = _self_hash(
        {
            "schema_version": "tushare-alpha-feasibility-run-claim.v2",
            "network_run_id": network_run_id,
            "collection_plan_sha256": plan.plan_sha256,
            **runtime_identity,
            "p14d_task_id": plan.pit_tasks[0].task_id,
            "locked_test_status": dict(LOCKED_TEST_STATUS),
            "locked_test_consumed": False,
        },
        "claim_sha256",
    )
    _publish(output_root / RUN_CLAIM_FILENAME, claim, "network_run_claim_mismatch")
    existing = load_existing_receipt(output_root, claim, plan, experiment)
    if existing is not None:
        return None, existing
    resumed = _completed_count(output_root, plan)
    if resumed < 1:
        raise P15RunError("p14d_import_not_completed")
    process_id = uuid.uuid4().hex
    marker = _self_hash(
        {
            "schema_version": "tushare-alpha-feasibility-network-process.v1",
            "process_id": process_id,
            "started_at": datetime.now(reporting.CHINA_TZ).isoformat(),
            "network_run_id": network_run_id,
            "run_claim_sha256": claim["claim_sha256"],
            "completed_request_fingerprint_count_at_start": resumed,
            "locked_test_status": dict(LOCKED_TEST_STATUS),
            "locked_test_consumed": False,
        },
        "process_sha256",
    )
    _publish(output_root / PROCESS_DIRECTORY / f"{process_id}.started.json", marker, "network_process_journal_unwritable")
    return RunContext(
        plan,
        claim,
        len(_process_records(output_root, claim)),
        resumed,
        process_id,
    ), None


def _coverage_status(value: Any) -> str:
    if value in {"COMPLETE", "complete"}:
        return "complete"
    if value in {"BLOCKED_DATA", "blocked"}:
        return "blocked"
    if value in {"NOT_RUN", "not_run"}:
        return "not_run"
    raise P15RunError("data_coverage_status_invalid")


def _pit_summary(output_root: Path, context: RunContext, declared_sha256: str | None) -> Mapping[str, Any]:
    try:
        result = data_lane._load_existing_pit_result(output_root, context.plan)
    except data_lane.AlphaFeasibilityDataError as exc:
        raise P15RunError("pit_manifest_invalid") from exc
    months = [f"{task.params['start_date'][:4]}-{task.params['start_date'][4:6]}" for task in context.plan.pit_tasks]
    if result is None:
        if declared_sha256 is not None:
            raise P15RunError("pit_manifest_missing")
        return {"months_expected": 73, "months_observed": 0, "snapshot_count": 0, "snapshot_dates": [], "missing_months": months, "union_instrument_count": 0, "zero_weight_count": 0, "weight_sum_min": None, "weight_sum_max": None, "coverage_status": "blocked"}
    manifest = result.manifest
    if manifest.get("manifest_sha256") != declared_sha256:
        raise P15RunError("pit_manifest_hash_mismatch")
    report = result.coverage_report
    snapshots = [
        snapshot
        for monthly in report["monthly_checks"]
        for snapshot in monthly["snapshots"]
    ]
    snapshot_dates = sorted(item["snapshot_date"] for item in snapshots)
    missing_months = [
        item["month"] for item in report["monthly_checks"] if item["status"] != "complete"
    ]
    zero_by_snapshot = {item["snapshot_date"]: item["zero_weight_count"] for item in snapshots}
    sum_by_snapshot = {item["snapshot_date"]: item["weight_sum"] for item in snapshots}
    if (
        report["pit_snapshot_count"] != len(snapshots)
        or report["snapshot_dates"] != snapshot_dates
        or report["missing_months"] != missing_months
        or report["zero_weight_count_by_snapshot"] != zero_by_snapshot
        or report["weight_sum_by_snapshot"] != sum_by_snapshot
    ):
        raise P15RunError("pit_coverage_report_summary_mismatch")
    sums = [Decimal(item["weight_sum"]) for item in snapshots]
    return {
        "months_expected": report["pit_months_expected"],
        "months_observed": report["pit_months_observed"],
        "snapshot_count": report["pit_snapshot_count"],
        "snapshot_dates": snapshot_dates,
        "missing_months": missing_months,
        "union_instrument_count": manifest["union_instrument_count"],
        "zero_weight_count": sum(zero_by_snapshot.values()),
        "weight_sum_min": format(min(sums), "f") if sums else None,
        "weight_sum_max": format(max(sums), "f") if sums else None,
        "coverage_status": "complete" if result.passed else "blocked",
    }


def _verify_runtime_claim(context: RunContext) -> None:
    bundle = context.claim.get("runtime_implementation_bundle")
    config_path = bundle.get("config_path") if isinstance(bundle, Mapping) else None
    if type(config_path) is not str:
        raise P15RunError("runtime_config_binding_invalid")
    current = _runtime_identity(data_lane.REPOSITORY_ROOT, Path(config_path))
    if context.claim.get("collection_plan_sha256") != context.plan.plan_sha256 or any(
        context.claim.get(field) != value for field, value in current.items()
    ):
        raise P15RunError("runtime_identity_changed_during_run")


def publish_receipt(
    *,
    context: RunContext,
    output_root: Path,
    source: Mapping[str, Any],
    experiment: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Path]:
    _verify_runtime_claim(context)
    try:
        reporting.verify_alpha_feasibility_report(source, experiment=experiment)
    except reporting.AlphaFeasibilityReportingError as exc:
        raise P15RunError("run_receipt_report_invalid") from exc
    if source.get("commit_sha") != context.claim.get("code_commit_sha"):
        raise P15RunError("run_receipt_report_baseline_mismatch")
    tasks = _expected_tasks(output_root, context.plan)
    try:
        counts = data_lane.actual_tushare_request_count_by_endpoint(output_root, tasks, plan_sha256=context.plan.plan_sha256)
    except data_lane.AlphaFeasibilityDataError as exc:
        raise P15RunError("request_count_evidence_invalid") from exc
    completed = _completed_count(output_root, context.plan)
    records = _process_records(output_root, context.claim)
    process_count = len(records)
    terminal_process = [
        item for item in records if item.get("process_id") == context.process_id
    ]
    if (
        counts != source["actual_tushare_request_count_by_endpoint"]
        or process_count != context.network_process_count
        or len(terminal_process) != 1
        or terminal_process[0]["completed_request_fingerprint_count_at_start"]
        != context.resumed_request_fingerprint_count
        or not 1 <= context.resumed_request_fingerprint_count <= completed <= len(tasks)
    ):
        raise P15RunError("run_count_evidence_mismatch")
    coverage = {
        "daily_status": _coverage_status(source["daily_coverage_status"]),
        "adj_factor_status": _coverage_status(source["adj_factor_coverage_status"]),
        "index_daily_status": _coverage_status(source["benchmark_coverage_status"]),
        "suspend_d_status": _coverage_status(source["suspension_coverage_status"]),
    }
    market_data_complete = (
        source.get("history_manifest_sha256") is not None
        and set(coverage.values()) == {"complete"}
    )
    pit = _pit_summary(
        output_root, context, source.get("pit_membership_manifest_sha256")
    )
    terminal_status = str(source["terminal_status"])
    development_metrics = source["development_metrics"]
    validation_metrics = source["validation_metrics"]
    concentration_metrics = source["concentration_metrics"]
    blockers = sorted(
        {
            item if SAFE_BLOCKER.fullmatch(str(item)) else "unsafe_blocker_sanitized"
            for item in source["remaining_blockers"]
        }
    )
    completed_terminal = terminal_status in {
        "ALPHA_FEASIBILITY_GO_CANDIDATE",
        "ALPHA_FEASIBILITY_NO_GO",
    }
    if completed_terminal:
        if (
            completed != len(tasks)
            or not market_data_complete
            or pit["coverage_status"] != "complete"
            or pit["months_observed"] != 73
            or pit["snapshot_count"] < 73
            or pit["missing_months"]
            or pit["union_instrument_count"] < 1
            or blockers
            or any(
                item is None
                for item in (
                    development_metrics,
                    validation_metrics,
                    concentration_metrics,
                )
            )
        ):
            raise P15RunError("completed_run_receipt_evidence_incomplete")
    elif terminal_status in {"BLOCKED_DATA", "BLOCKED_ADAPTER_PROTOCOL"}:
        if not blockers or any(
            item is not None
            for item in (
                development_metrics,
                validation_metrics,
                concentration_metrics,
            )
        ):
            raise P15RunError("blocked_run_receipt_evidence_invalid")
    else:
        raise P15RunError("run_receipt_terminal_invalid")
    receipt = _self_hash(
        {
            "schema_version": "tushare-alpha-feasibility-run-receipt.v1",
            "generated_at": source["generated_at"],
            **{
                field: context.claim[field]
                for field in (
                    "code_commit_sha",
                    "baseline_commit_sha",
                    "baseline_commit_semantics",
                    "remote_branch_sha",
                    "working_tree_clean",
                    "runtime_implementation_bundle",
                )
            },
            "collection_plan_sha256": context.plan.plan_sha256,
            "run_claim_sha256": context.claim["claim_sha256"],
            "network_run_id": context.claim["network_run_id"],
            "network_process_count": process_count,
            "terminal_process_id": context.process_id,
            "actual_request_count_by_endpoint": counts,
            "request_count_semantics": REQUEST_COUNT_SEMANTICS,
            "completed_request_fingerprint_count": completed,
            "resumed_request_fingerprint_count": context.resumed_request_fingerprint_count,
            "pit": pit,
            "market_data": {
                "status": "complete" if market_data_complete else "blocked",
                "trade_calendar_status": "complete" if market_data_complete else "blocked",
                **coverage,
                "coverage_start": source["coverage_start"],
                "coverage_end": source["coverage_end"],
                "unexplained_gap_count": source["unexplained_market_data_gap_count"],
            },
            "development_metrics": development_metrics,
            "validation_metrics": validation_metrics,
            "concentration_metrics": concentration_metrics,
            "terminal_status": terminal_status,
            "locked_test_status": dict(LOCKED_TEST_STATUS),
            "locked_test_consumed": False,
            "stock_basic_status": source["stock_basic_status"],
            "stock_basic_request_count": source["stock_basic_request_count"],
            "security_master_pit_status": source["security_master_pit_status"],
            "remaining_blockers": blockers,
            "safety": dict(source["safety"]),
            "report_sha256": source["report_sha256"],
        },
        "receipt_sha256",
    )
    try:
        validate_json_schema(receipt, RECEIPT_SCHEMA_PATH)
    except SchemaValidationError as exc:
        raise P15RunError("run_receipt_schema_invalid") from exc
    path = _publish(output_root / RUN_RECEIPT_FILENAME, receipt, "run_receipt_create_only_mismatch")
    return receipt, path


def recoverable_summary(
    context: RunContext, output_root: Path, interruption_code: str
) -> dict[str, Any]:
    _verify_runtime_claim(context)
    tasks = _expected_tasks(output_root, context.plan)
    try:
        counts = data_lane.actual_tushare_request_count_by_endpoint(
            output_root, tasks, plan_sha256=context.plan.plan_sha256
        )
    except data_lane.AlphaFeasibilityDataError as exc:
        raise P15RunError("request_count_evidence_invalid") from exc
    completed = _completed_count(output_root, context.plan)
    records = _process_records(output_root, context.claim)
    current = [item for item in records if item.get("process_id") == context.process_id]
    if (
        SAFE_BLOCKER.fullmatch(interruption_code) is None
        or len(records) != context.network_process_count
        or len(current) != 1
        or current[0]["completed_request_fingerprint_count_at_start"]
        != context.resumed_request_fingerprint_count
        or not 1 <= context.resumed_request_fingerprint_count <= completed <= len(tasks)
    ):
        raise P15RunError("recoverable_run_evidence_invalid")
    return {
        "status": "recoverable_interruption",
        "terminal_status": None,
        "recoverable_interruption_code": interruption_code,
        **{
            field: context.claim[field]
            for field in (
                "code_commit_sha",
                "baseline_commit_sha",
                "baseline_commit_semantics",
                "remote_branch_sha",
                "working_tree_clean",
                "runtime_implementation_bundle",
                "network_run_id",
            )
        },
        "network_process_count": len(records),
        "actual_request_count_by_endpoint": counts,
        "request_count_semantics": REQUEST_COUNT_SEMANTICS,
        "completed_request_fingerprint_count": completed,
        "resumed_request_fingerprint_count": context.resumed_request_fingerprint_count,
        "locked_test_status": dict(LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
        "run_receipt_created": False,
    }


def summary(receipt: Mapping[str, Any], path: Path) -> dict[str, Any]:
    fields = ("code_commit_sha", "baseline_commit_sha", "baseline_commit_semantics", "remote_branch_sha", "working_tree_clean", "runtime_implementation_bundle", "network_run_id", "network_process_count", "terminal_process_id", "actual_request_count_by_endpoint", "request_count_semantics", "completed_request_fingerprint_count", "resumed_request_fingerprint_count", "pit", "market_data", "development_metrics", "validation_metrics", "concentration_metrics", "terminal_status", "locked_test_status", "locked_test_consumed", "stock_basic_status", "stock_basic_request_count", "security_master_pit_status", "remaining_blockers", "safety")
    return {"status": "blocked" if str(receipt["terminal_status"]).startswith("BLOCKED_") else "completed", **{field: receipt[field] for field in fields}, "run_receipt_path": str(path.resolve())}


__all__ = ["P15RunError", "RunContext", "RUN_RECEIPT_FILENAME", "prepare", "publish_receipt", "recoverable_summary", "summary"]
