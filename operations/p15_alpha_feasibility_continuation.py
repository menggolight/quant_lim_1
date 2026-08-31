"""Offline, parent-bound preparation for the P1.5C continuation run.

This module never opens a network connection.  It validates the sealed P1.5
parent run, proves that exactly the first nineteen PIT request fingerprints are
complete, binds every reused byte by hash, and creates a new child-root claim.
The later network executor must consume that claim; it must not reconstruct or
weaken the evidence checks implemented here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence

from operations import p15_alpha_feasibility_run as p15_run
from research.market_data import tushare_alpha_feasibility as data_lane
from research.market_data.validation import (
    SchemaValidationError,
    validate_json_schema,
)
from research.strategy_workspace import alpha_feasibility_reporting as reporting


LOCKED_TEST_STATUS = {
    "access": "NOT_ACCESSED",
    "download": "NOT_DOWNLOADED",
    "run": "NOT_RUN",
}
REUSE_MANIFEST_FILENAME = "p1_5_continuation_reuse_manifest.json"
CONTINUATION_CLAIM_FILENAME = "p1_5_continuation_claim.json"
PARENT_REUSE_STAGE_FILENAME = "p1_5_continuation_parent_reuse_stage.json"
NETWORK_PROCESS_FILENAME = "p1_5_continuation_network_process.json"
FIRST_RESPONSE_EVIDENCE_FILENAME = (
    "p1_5_continuation_first_response_evidence.json"
)
CONTINUATION_RECEIPT_FILENAME = "p1_5_continuation_receipt.json"
PARENT_CLAIM_FILENAME = p15_run.RUN_CLAIM_FILENAME
PARENT_RECEIPT_FILENAME = p15_run.RUN_RECEIPT_FILENAME
PARENT_REPORT_FILENAME = reporting.REPORT_FILENAME
PARENT_PIT_COVERAGE_FILENAME = "pit_membership_coverage_report.json"
PARENT_PIT_MANIFEST_FILENAME = "pit_membership_manifest.json"

REUSE_SCHEMA_PATH = (
    data_lane.REPOSITORY_ROOT
    / "schemas"
    / "tushare_alpha_feasibility_continuation_reuse_manifest.v1.json"
)
CLAIM_SCHEMA_PATH = (
    data_lane.REPOSITORY_ROOT
    / "schemas"
    / "tushare_alpha_feasibility_continuation_claim.v1.json"
)
RECEIPT_SCHEMA_PATH = (
    data_lane.REPOSITORY_ROOT
    / "schemas"
    / "tushare_alpha_feasibility_continuation_receipt.v1.json"
)
PARENT_REUSE_STAGE_SCHEMA_PATH = (
    data_lane.REPOSITORY_ROOT
    / "schemas"
    / "tushare_alpha_feasibility_continuation_parent_reuse_stage.v1.json"
)
NETWORK_PROCESS_SCHEMA_PATH = (
    data_lane.REPOSITORY_ROOT
    / "schemas"
    / "tushare_alpha_feasibility_continuation_network_process.v1.json"
)
FIRST_RESPONSE_EVIDENCE_SCHEMA_PATH = (
    data_lane.REPOSITORY_ROOT
    / "schemas"
    / "tushare_alpha_feasibility_continuation_first_response.v1.json"
)

SUCCESSFUL_PREFIX_COUNT = 19
FIRST_UNFINISHED_ORDINAL = 20
FIRST_UNFINISHED_TASK_ID = (
    "index_weight-"
    "63ab4e7a5df236828d4f750812809bc78a6e1f8e9cfdad64c8128431da6a741b"
)
FIRST_UNFINISHED_PARAMS = {
    "index_code": "000906.SH",
    "start_date": "20190701",
    "end_date": "20190731",
}
PARENT_UPSTREAM_CODE = 40204
PARENT_FAILURE_CODE = "upstream_unknown_error"
PARENT_CLASSIFICATION = "UNCLASSIFIED_PARENT_EVIDENCE"
MINIMUM_TRANSPORT_INTERVAL_SECONDS = "12"
RATE_LIMIT_FALLBACK_SECONDS = 65
MAXIMUM_RETRY_AFTER_SECONDS = 300
MAXIMUM_CUMULATIVE_ATTEMPTS = 3
PARENT_ATTEMPT_COUNT = 1
EXPECTED_PARENT_CLAIM_SHA256 = (
    "72aa7dd6c350bb440861a2b999e67798a24fcad564c3139d69b684407039c96c"
)
EXPECTED_PARENT_RECEIPT_SHA256 = (
    "ba24c9dbbd601b4af9c6b33516cbbd159efdced5ce2e15102983f59253d775ba"
)
EXPECTED_PARENT_REPORT_SHA256 = (
    "54bffc92ed3a2166ee6855567db651643d4ce231581293678d5c6a0597bf3f39"
)
EXPECTED_PARENT_RUNTIME_BUNDLE_SHA256 = (
    "313fa5d07db00c735763fd61e8cfd55ad4570425fc0402f93debe1c48b0cbc91"
)
EXPECTED_PARENT_REPORTING_SOURCE_SHA256 = (
    "e3976bbd294a0dc4fbcd642dbbc536d0bfaa495fae71c6f1e67dff0503e442f7"
)
EXPECTED_PARENT_PIT_COVERAGE_SHA256 = (
    "35e9337bc51b62fd7049d64e4c3247fd3ad867d8abcc25666dd3e79aaa2473fd"
)
EXPECTED_PARENT_PIT_MANIFEST_SHA256 = (
    "33d2dfd739953580ac292acfba8bdd4481519205c9484fc1848c55e3673f5ebb"
)

TERMINAL_STATUSES = frozenset(
    {
        "COMPLETED",
        "BLOCKED_UPSTREAM_RATE_LIMIT",
        "BLOCKED_PROVIDER_PERMISSION",
        "BLOCKED_PROVIDER_QUOTA",
        "BLOCKED_INVALID_PARAMETER",
        "BLOCKED_PIT_SOURCE_COVERAGE",
        "BLOCKED_UPSTREAM_SERVER",
        "BLOCKED_UPSTREAM_UNDOCUMENTED_CODE",
        "BLOCKED_DATA",
        "BLOCKED_ADAPTER_PROTOCOL",
    }
)
TERMINAL_STAGES = frozenset(
    {"PIT", "HISTORY", "ALPHA_INPUT", "ALPHA_ENGINE", "REPORT"}
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_CODE = re.compile(r"^[a-z0-9_]{3,96}$")
_ALLOWED_CHILD_FILES = frozenset(
    {
        REUSE_MANIFEST_FILENAME,
        CONTINUATION_CLAIM_FILENAME,
        PARENT_REUSE_STAGE_FILENAME,
        NETWORK_PROCESS_FILENAME,
        FIRST_RESPONSE_EVIDENCE_FILENAME,
        CONTINUATION_RECEIPT_FILENAME,
    }
)
_ALLOWED_CHILD_DIRECTORIES = frozenset(
    {"tasks", "raw", "attempts", "raw_errors", "business_errors", "quarantine"}
)


class P15ContinuationError(RuntimeError):
    """Raised when continuation evidence is incomplete or does not bind."""

    def __init__(self, code: str) -> None:
        self.code = code if _SAFE_CODE.fullmatch(str(code)) else "unsafe_error_sanitized"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class ContinuationContext:
    """Verified, immutable inputs for the later continuation executor."""

    parent_root: Path
    child_root: Path
    plan: data_lane.CollectionPlan
    experiment: Mapping[str, Any]
    reuse_manifest: Mapping[str, Any]
    claim: Mapping[str, Any]
    parent_actual_request_count_by_endpoint: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class _ParentEvidence:
    plan: data_lane.CollectionPlan
    experiment: Mapping[str, Any]
    parent_binding: Mapping[str, str]
    parent_runtime_bundle: Mapping[str, Any]
    parent_actual_request_count_by_endpoint: Mapping[str, int]
    reused_tasks: tuple[Mapping[str, Any], ...]
    first_unfinished: Mapping[str, Any]


def _file_sha256(path: Path, code: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise P15ContinuationError(code)
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise P15ContinuationError(code) from exc


def _canonical_object(path: Path, code: str) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise P15ContinuationError(code)
    try:
        raw = path.read_bytes()
        value = data_lane.strict_json_loads(raw, label=code)
    except (OSError, data_lane.AlphaFeasibilityDataError) as exc:
        raise P15ContinuationError(code) from exc
    if not isinstance(value, Mapping) or raw != data_lane.canonical_json_bytes(value):
        raise P15ContinuationError(code)
    return value


def _verify_self_hash(
    value: Mapping[str, Any], field: str, code: str, *, newline: bool = True
) -> str:
    unsigned = dict(value)
    declared = unsigned.pop(field, None)
    expected = (
        data_lane.canonical_sha256(unsigned)
        if newline
        else reporting.canonical_sha256(unsigned)
    )
    if declared != expected or not isinstance(declared, str):
        raise P15ContinuationError(code)
    return declared


def _timestamp(value: datetime | str, code: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise P15ContinuationError(code) from exc
    else:
        raise P15ContinuationError(code)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise P15ContinuationError(code)
    return parsed.isoformat()


def _sha256_or_none(value: Any, code: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise P15ContinuationError(code)
    return value


def _validate_locked(value: Mapping[str, Any], code: str) -> None:
    if (
        value.get("locked_test_status") != LOCKED_TEST_STATUS
        or value.get("locked_test_consumed") is not False
    ):
        raise P15ContinuationError(code)


def _validate_roots(parent_root: Path, child_root: Path) -> tuple[Path, Path]:
    parent_candidate = Path(parent_root)
    child_candidate = Path(child_root)
    if (
        parent_candidate.is_symlink()
        or not parent_candidate.is_dir()
        or child_candidate.is_symlink()
    ):
        raise P15ContinuationError("continuation_root_invalid")
    parent = parent_candidate.resolve()
    child = child_candidate.resolve()
    if parent == child or child.is_relative_to(parent) or parent.is_relative_to(child):
        raise P15ContinuationError("continuation_roots_must_be_disjoint")
    if child_candidate.exists():
        if not child_candidate.is_dir():
            raise P15ContinuationError("continuation_child_root_invalid")
        try:
            entries = tuple(child_candidate.iterdir())
        except OSError as exc:
            raise P15ContinuationError("continuation_child_root_invalid") from exc
        names = {entry.name for entry in entries}
        prepared_names = {REUSE_MANIFEST_FILENAME, CONTINUATION_CLAIM_FILENAME}
        if names.isdisjoint(prepared_names):
            if entries:
                raise P15ContinuationError("continuation_child_root_not_create_only")
        elif not prepared_names.issubset(names):
            raise P15ContinuationError("continuation_child_root_not_create_only")
        if any(
            entry.is_symlink() or not (entry.is_file() or entry.is_dir())
            for entry in entries
        ):
            raise P15ContinuationError("continuation_child_root_not_create_only")
    return parent, child


def _publish(path: Path, value: Mapping[str, Any], code: str) -> Path:
    content = data_lane.canonical_json_bytes(value)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(content)
    except FileExistsError:
        try:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
                raise P15ContinuationError(code)
        except OSError as exc:
            raise P15ContinuationError(code) from exc
    except OSError as exc:
        raise P15ContinuationError(code) from exc
    return path


def _self_hash(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    if field in value:
        raise P15ContinuationError("continuation_self_hash_field_preexisting")
    result = dict(value)
    result[field] = data_lane.canonical_sha256(result)
    return result


def _validate_runtime_bundle(
    value: Any, code: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "config_path",
        "files",
        "bundle_sha256",
    }:
        raise P15ContinuationError(code)
    if not isinstance(value.get("schema_version"), str) or not value["schema_version"]:
        raise P15ContinuationError(code)
    config_path = value.get("config_path")
    files = value.get("files")
    if (
        not isinstance(config_path, str)
        or Path(config_path).is_absolute()
        or ".." in Path(config_path).parts
        or not isinstance(files, Mapping)
        or not files
    ):
        raise P15ContinuationError(code)
    for relative, digest in files.items():
        if (
            not isinstance(relative, str)
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
        ):
            raise P15ContinuationError(code)
    _verify_self_hash(value, "bundle_sha256", code)
    return value


def _current_runtime_bundle(
    config_path: Path, experiment: Mapping[str, Any]
) -> Mapping[str, Any]:
    root = data_lane.REPOSITORY_ROOT.resolve()
    config_candidate = config_path if config_path.is_absolute() else root / config_path
    candidates = [root / relative for relative in p15_run.RUNTIME_PATHS]
    candidates.append(root / "operations" / "p15_alpha_feasibility_continuation.py")
    candidates.append(
        root / "operations" / "run_p15_alpha_feasibility_continuation.py"
    )
    candidates.append(config_candidate)
    frozen = experiment["frozen_implementation"]
    for prefix in ("alpha", "ranker", "exposure"):
        candidates.append(root / frozen[f"{prefix}_source_path"])
    candidates.extend(sorted((root / "schemas").glob("*.json"), key=lambda item: item.name))

    files: dict[str, str] = {}
    config_relative: str | None = None
    resolved_config = config_candidate.resolve()
    for candidate in candidates:
        if candidate.is_symlink() or not candidate.is_file():
            raise P15ContinuationError("continuation_runtime_file_unavailable")
        resolved = candidate.resolve()
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError as exc:
            raise P15ContinuationError("continuation_runtime_path_invalid") from exc
        files[relative] = _file_sha256(
            resolved, "continuation_runtime_file_unavailable"
        )
        if resolved == resolved_config:
            config_relative = relative
    if config_relative is None:
        raise P15ContinuationError("continuation_runtime_config_missing")
    return _self_hash(
        {
            "schema_version": "alpha-feasibility-continuation-runtime-bundle.v1",
            "config_path": config_relative,
            "files": dict(sorted(files.items())),
        },
        "bundle_sha256",
    )


def _immutable_strategy_bundle(
    experiment: Mapping[str, Any], current_runtime: Mapping[str, Any]
) -> Mapping[str, Any]:
    frozen = experiment["frozen_implementation"]
    frozen_files: dict[str, str] = {}
    for prefix in ("alpha", "ranker", "exposure"):
        relative = frozen[f"{prefix}_source_path"]
        declared = frozen[f"{prefix}_source_sha256"]
        actual = current_runtime["files"].get(relative)
        if actual != declared:
            raise P15ContinuationError("immutable_strategy_source_drift")
        frozen_files[relative] = declared
    engine_relative = "research/strategy_workspace/alpha_feasibility.py"
    engine_hash = current_runtime["files"].get(engine_relative)
    if not isinstance(engine_hash, str):
        raise P15ContinuationError("immutable_alpha_engine_missing")
    locked_policy = {
        "locked_test_status": experiment["locked_test_status"],
        "locked_test_consumed": experiment["locked_test_consumed"],
        "safety": experiment["safety"],
    }
    return _self_hash(
        {
            "experiment_config_canonical_sha256": reporting.canonical_sha256(
                experiment
            ),
            "alpha_engine_sha256": engine_hash,
            "frozen_implementation_files": dict(sorted(frozen_files.items())),
            "dates_sha256": reporting.canonical_sha256(experiment["dates"]),
            "portfolio_sha256": reporting.canonical_sha256(
                experiment["portfolio"]
            ),
            "costs_sha256": reporting.canonical_sha256(experiment["costs"]),
            "gate_sha256": reporting.canonical_sha256(experiment["gate"]),
            "locked_policy_sha256": reporting.canonical_sha256(locked_policy),
        },
        "bundle_sha256",
    )


def _validate_request_counts(value: Any, code: str) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != set(reporting.ALLOWED_ENDPOINTS):
        raise P15ContinuationError(code)
    result: dict[str, int] = {}
    for endpoint in reporting.ALLOWED_ENDPOINTS:
        count = value[endpoint]
        if type(count) is not int or count < 0:
            raise P15ContinuationError(code)
        result[endpoint] = count
    return result


def _artifact_sha(path: Path, code: str) -> str:
    return _file_sha256(path, code)


def _task_month(task: data_lane.CollectionTask) -> str:
    start = task.params.get("start_date")
    end = task.params.get("end_date")
    if (
        type(start) is not str
        or type(end) is not str
        or len(start) != 8
        or len(end) != 8
        or start[:6] != end[:6]
    ):
        raise P15ContinuationError("continuation_task_month_invalid")
    return f"{start[:4]}-{start[4:6]}"


def _reuse_item(
    store: data_lane.CreateOnlyTaskStore,
    task: data_lane.CollectionTask,
    ordinal: int,
) -> tuple[Mapping[str, Any], data_lane.TaskExecutionResult]:
    try:
        result = store._load_response(task)
        attempts = store._load_attempts(task)
    except data_lane.AlphaFeasibilityDataError as exc:
        raise P15ContinuationError("parent_reused_task_invalid") from exc
    started = store.started_path(task)
    imported = store.import_path(task)
    raw = store.raw_path(task)
    response = store.response_path(task)
    if store.quarantine_path(task).exists():
        raise P15ContinuationError("parent_completed_task_quarantined")
    if ordinal == 1:
        if result.request_origin != "offline_p14d_import" or attempts:
            raise P15ContinuationError("parent_p14d_prefix_invalid")
    elif result.request_origin != "network" or len(attempts) != result.network_request_count:
        raise P15ContinuationError("parent_network_prefix_invalid")
    if result.network_request_count > MAXIMUM_CUMULATIVE_ATTEMPTS:
        raise P15ContinuationError("parent_attempt_budget_invalid")
    try:
        response_value = data_lane.strict_json_loads(
            response.read_bytes(), label="parent_response"
        )
    except (OSError, data_lane.AlphaFeasibilityDataError) as exc:
        raise P15ContinuationError("parent_response_invalid") from exc
    if not isinstance(response_value, Mapping):
        raise P15ContinuationError("parent_response_invalid")
    attempt_hashes = [
        _artifact_sha(
            store.attempt_path(task, number), "parent_attempt_artifact_invalid"
        )
        for number in range(1, len(attempts) + 1)
    ]
    return (
        {
            "ordinal": ordinal,
            "request_fingerprint": task.task_id,
            "task_id": task.task_id,
            "task_sha256": data_lane.canonical_sha256(task.to_dict()),
            "endpoint": task.endpoint,
            "month": _task_month(task),
            "params": dict(task.params),
            "provenance_kind": result.request_origin,
            "started_artifact_sha256": (
                _artifact_sha(started, "parent_started_artifact_invalid")
                if started.is_file()
                else None
            ),
            "import_artifact_sha256": (
                _artifact_sha(imported, "parent_import_artifact_invalid")
                if imported.is_file()
                else None
            ),
            "raw_artifact_sha256": _artifact_sha(
                raw, "parent_raw_artifact_invalid"
            ),
            "response_file_sha256": _artifact_sha(
                response, "parent_response_invalid"
            ),
            "response_artifact_sha256": response_value[
                "response_artifact_sha256"
            ],
            "normalized_content_sha256": result.normalized_content_sha256,
            "attempt_artifact_sha256_by_number": attempt_hashes,
            "network_request_count": result.network_request_count,
        },
        result,
    )


def _first_unfinished_evidence(
    store: data_lane.CreateOnlyTaskStore, task: data_lane.CollectionTask
) -> Mapping[str, Any]:
    if (
        task.task_id != FIRST_UNFINISHED_TASK_ID
        or task.endpoint != "index_weight"
        or dict(task.params) != FIRST_UNFINISHED_PARAMS
        or store.is_complete(task)
        or store.import_path(task).exists()
        or store.response_path(task).exists()
    ):
        raise P15ContinuationError("first_unfinished_task_mismatch")
    try:
        store._load_started(task)
        attempts = store._load_attempts(task)
        quarantine = _canonical_object(
            store.quarantine_path(task), "parent_quarantine_invalid"
        )
    except data_lane.AlphaFeasibilityDataError as exc:
        raise P15ContinuationError("first_unfinished_evidence_invalid") from exc
    quarantine_schema_by_version = {
        "tushare-alpha-feasibility-quarantine.v4": (
            data_lane.REPOSITORY_ROOT
            / "schemas"
            / "tushare_alpha_feasibility_quarantine.v4.json"
        ),
        "tushare-alpha-feasibility-quarantine.v5": (
            data_lane.REPOSITORY_ROOT
            / "schemas"
            / "tushare_alpha_feasibility_quarantine.v5.json"
        ),
    }
    quarantine_schema = quarantine_schema_by_version.get(
        quarantine.get("schema_version")
    )
    if quarantine_schema is None or not quarantine_schema.is_file():
        raise P15ContinuationError("parent_quarantine_schema_unsupported")
    try:
        validate_json_schema(quarantine, quarantine_schema)
    except SchemaValidationError as exc:
        raise P15ContinuationError("parent_quarantine_invalid") from exc
    if (
        len(attempts) != PARENT_ATTEMPT_COUNT
        or quarantine.get("failure_code") != PARENT_FAILURE_CODE
        or quarantine.get("task_id") != task.task_id
        or quarantine.get("endpoint") != task.endpoint
        or quarantine.get("plan_sha256") != task.plan_sha256
        or quarantine.get("upstream_code") != PARENT_UPSTREAM_CODE
        or quarantine.get("upstream_error_category") != "unknown"
        or quarantine.get("raw_transport_sha256") is None
    ):
        raise P15ContinuationError("first_unfinished_evidence_invalid")
    return {
        "ordinal": FIRST_UNFINISHED_ORDINAL,
        "request_fingerprint": task.task_id,
        "task_id": task.task_id,
        "task_sha256": data_lane.canonical_sha256(task.to_dict()),
        "endpoint": task.endpoint,
        "month": _task_month(task),
        "params": dict(task.params),
        "started_artifact_sha256": _artifact_sha(
            store.started_path(task), "parent_started_artifact_invalid"
        ),
        "raw_transport_sha256": quarantine["raw_transport_sha256"],
        "quarantine_artifact_sha256": _artifact_sha(
            store.quarantine_path(task), "parent_quarantine_invalid"
        ),
        "attempt_artifact_sha256_by_number": [
            _artifact_sha(
                store.attempt_path(task, 1), "parent_attempt_artifact_invalid"
            )
        ],
        "parent_attempt_count": PARENT_ATTEMPT_COUNT,
        "next_attempt_number": PARENT_ATTEMPT_COUNT + 1,
        "maximum_cumulative_attempts": MAXIMUM_CUMULATIVE_ATTEMPTS,
        "parent_failure_code": PARENT_FAILURE_CODE,
        "parent_upstream_code": PARENT_UPSTREAM_CODE,
        "parent_classification": PARENT_CLASSIFICATION,
    }


def _validate_parent_evidence(
    parent_root: Path, config_path: Path
) -> _ParentEvidence:
    try:
        experiment = reporting.load_and_validate_experiment_config(config_path)
        plan = data_lane.load_config_and_build_plan(config_path)
    except (
        reporting.AlphaFeasibilityReportingError,
        data_lane.AlphaFeasibilityDataError,
    ) as exc:
        raise P15ContinuationError("continuation_config_invalid") from exc
    if experiment.get("schema_version") != "technical-alpha-feasibility-experiment.v3":
        raise P15ContinuationError("continuation_requires_p15_v3")

    parent_claim = _canonical_object(
        parent_root / PARENT_CLAIM_FILENAME, "parent_run_claim_invalid"
    )
    parent_receipt = _canonical_object(
        parent_root / PARENT_RECEIPT_FILENAME, "parent_receipt_invalid"
    )
    parent_report = _canonical_object(
        parent_root / PARENT_REPORT_FILENAME, "parent_report_invalid"
    )
    coverage = _canonical_object(
        parent_root / PARENT_PIT_COVERAGE_FILENAME, "parent_pit_coverage_invalid"
    )
    manifest = _canonical_object(
        parent_root / PARENT_PIT_MANIFEST_FILENAME, "parent_pit_manifest_invalid"
    )

    if parent_claim.get("schema_version") != "tushare-alpha-feasibility-run-claim.v2":
        raise P15ContinuationError("parent_run_claim_invalid")
    parent_claim_sha = _verify_self_hash(
        parent_claim, "claim_sha256", "parent_run_claim_invalid"
    )
    _validate_locked(parent_claim, "parent_locked_policy_invalid")

    try:
        validate_json_schema(parent_receipt, p15_run.RECEIPT_SCHEMA_PATH)
    except SchemaValidationError as exc:
        raise P15ContinuationError("parent_receipt_invalid") from exc
    parent_receipt_sha = _verify_self_hash(
        parent_receipt, "receipt_sha256", "parent_receipt_invalid"
    )
    _validate_locked(parent_receipt, "parent_locked_policy_invalid")

    parent_runtime = _validate_runtime_bundle(
        parent_receipt.get("runtime_implementation_bundle"),
        "parent_runtime_bundle_invalid",
    )
    if parent_claim.get("runtime_implementation_bundle") != parent_runtime:
        raise P15ContinuationError("parent_runtime_bundle_mismatch")
    parent_reporting_sha = parent_runtime["files"].get(
        "research/strategy_workspace/alpha_feasibility_reporting.py"
    )
    if not isinstance(parent_reporting_sha, str) or _SHA256.fullmatch(
        parent_reporting_sha
    ) is None:
        raise P15ContinuationError("parent_runtime_bundle_invalid")
    for field in (
        "network_run_id",
        "collection_plan_sha256",
        "code_commit_sha",
        "baseline_commit_sha",
        "baseline_commit_semantics",
        "remote_branch_sha",
        "working_tree_clean",
    ):
        if parent_receipt.get(field) != parent_claim.get(field):
            raise P15ContinuationError("parent_claim_receipt_mismatch")
    if (
        parent_receipt.get("run_claim_sha256") != parent_claim_sha
        or parent_receipt.get("collection_plan_sha256") != plan.plan_sha256
    ):
        raise P15ContinuationError("parent_claim_receipt_mismatch")

    parent_report_sha = _verify_self_hash(
        parent_report, "report_sha256", "parent_report_invalid", newline=False
    )
    if (
        parent_claim_sha != EXPECTED_PARENT_CLAIM_SHA256
        or parent_receipt_sha != EXPECTED_PARENT_RECEIPT_SHA256
        or parent_report_sha != EXPECTED_PARENT_REPORT_SHA256
        or parent_runtime.get("bundle_sha256")
        != EXPECTED_PARENT_RUNTIME_BUNDLE_SHA256
        or parent_reporting_sha != EXPECTED_PARENT_REPORTING_SOURCE_SHA256
        or parent_report.get("reporting_gate_source_sha256")
        != EXPECTED_PARENT_REPORTING_SOURCE_SHA256
    ):
        raise P15ContinuationError("parent_exact_run_anchor_mismatch")
    semantic_report = dict(parent_report)
    semantic_report["reporting_gate_source_sha256"] = reporting._runtime_provenance(
        experiment
    )["reporting_gate_source_sha256"]
    semantic_report["report_sha256"] = reporting.canonical_sha256(
        {
            key: value
            for key, value in semantic_report.items()
            if key != "report_sha256"
        }
    )
    try:
        reporting.verify_alpha_feasibility_report(
            semantic_report, experiment=experiment
        )
    except reporting.AlphaFeasibilityReportingError as exc:
        raise P15ContinuationError("parent_report_invalid") from exc
    if (
        parent_receipt.get("report_sha256") != parent_report_sha
        or parent_report.get("commit_sha") != parent_receipt.get("code_commit_sha")
        or parent_report.get("experiment_config_canonical_sha256")
        != reporting.canonical_sha256(experiment)
        or parent_report.get("terminal_status") != "BLOCKED_DATA"
        or parent_receipt.get("terminal_status") != "BLOCKED_DATA"
        or PARENT_FAILURE_CODE not in parent_report.get("remaining_blockers", ())
        or PARENT_FAILURE_CODE not in parent_receipt.get("remaining_blockers", ())
        or any(
            value is not None
            for value in (
                parent_report.get("development_metrics"),
                parent_report.get("validation_metrics"),
                parent_report.get("concentration_metrics"),
                parent_receipt.get("development_metrics"),
                parent_receipt.get("validation_metrics"),
                parent_receipt.get("concentration_metrics"),
            )
        )
    ):
        raise P15ContinuationError("parent_terminal_evidence_invalid")
    _validate_locked(parent_report, "parent_locked_policy_invalid")

    try:
        pit_result = data_lane._load_existing_pit_result(parent_root, plan)
    except data_lane.AlphaFeasibilityDataError as exc:
        raise P15ContinuationError("parent_pit_artifact_invalid") from exc
    if pit_result is None or pit_result.passed:
        raise P15ContinuationError("parent_pit_terminal_invalid")
    coverage_sha = _verify_self_hash(
        coverage, "report_sha256", "parent_pit_coverage_invalid"
    )
    manifest_sha = _verify_self_hash(
        manifest, "manifest_sha256", "parent_pit_manifest_invalid"
    )
    if (
        coverage_sha != EXPECTED_PARENT_PIT_COVERAGE_SHA256
        or manifest_sha != EXPECTED_PARENT_PIT_MANIFEST_SHA256
        or parent_report.get("pit_membership_manifest_sha256") != manifest_sha
        or parent_report.get("pit_months_observed") != SUCCESSFUL_PREFIX_COUNT
        or parent_report.get("union_instrument_count") != 0
        or parent_report.get("history_manifest_sha256") is not None
        or coverage.get("pit_months_expected") != 73
        or coverage.get("pit_months_observed") != SUCCESSFUL_PREFIX_COUNT
        or coverage.get("pit_snapshot_count") != SUCCESSFUL_PREFIX_COUNT
        or coverage.get("stage_status") != "BLOCKED_PIT_MEMBERSHIP"
        or coverage.get("terminal_status") != "BLOCKED_DATA"
        or manifest.get("pit_months_observed") != SUCCESSFUL_PREFIX_COUNT
        or manifest.get("pit_snapshot_count") != SUCCESSFUL_PREFIX_COUNT
        or manifest.get("stage_status") != "BLOCKED_PIT_MEMBERSHIP"
        or manifest.get("union_instrument_count") != 0
        or manifest.get("snapshots") != []
        or coverage.get("missing_months", [None])[0] != "2019-07"
        or manifest.get("missing_months", [None])[0] != "2019-07"
    ):
        raise P15ContinuationError("parent_pit_lineage_mismatch")
    _validate_locked(coverage, "parent_locked_policy_invalid")
    _validate_locked(manifest, "parent_locked_policy_invalid")

    counts = _validate_request_counts(
        parent_receipt.get("actual_request_count_by_endpoint"),
        "parent_request_counts_invalid",
    )
    if counts != parent_report.get("actual_tushare_request_count_by_endpoint"):
        raise P15ContinuationError("parent_request_counts_mismatch")
    if parent_receipt.get("completed_request_fingerprint_count") != SUCCESSFUL_PREFIX_COUNT:
        raise P15ContinuationError("parent_completed_count_invalid")

    store = data_lane.CreateOnlyTaskStore(parent_root)
    complete_indices = [
        index
        for index, task in enumerate(plan.pit_tasks)
        if store.is_complete(task)
    ]
    if complete_indices != list(range(SUCCESSFUL_PREFIX_COUNT)):
        raise P15ContinuationError("parent_completed_prefix_not_contiguous")
    reused: list[Mapping[str, Any]] = []
    results: dict[str, data_lane.TaskExecutionResult] = {}
    for ordinal, task in enumerate(
        plan.pit_tasks[:SUCCESSFUL_PREFIX_COUNT], start=1
    ):
        item, result = _reuse_item(store, task, ordinal)
        reused.append(item)
        results[task.task_id] = result
    first_unfinished = _first_unfinished_evidence(
        store, plan.pit_tasks[SUCCESSFUL_PREFIX_COUNT]
    )

    try:
        generated = datetime.fromisoformat(
            str(coverage["generated_at"]).replace("Z", "+00:00")
        )
        rebuilt = data_lane.build_pit_membership_artifacts(
            plan,
            results,
            generated_at=generated,
            blocked_terminal_status=str(coverage["terminal_status"]),
        )
    except (ValueError, data_lane.AlphaFeasibilityDataError) as exc:
        raise P15ContinuationError("parent_pit_replay_failed") from exc
    if (
        dict(rebuilt.coverage_report) != dict(coverage)
        or dict(rebuilt.manifest) != dict(manifest)
    ):
        raise P15ContinuationError("parent_pit_replay_mismatch")

    dummy_context = p15_run.RunContext(
        plan=plan,
        claim=parent_claim,
        network_process_count=parent_receipt["network_process_count"],
        resumed_request_fingerprint_count=parent_receipt[
            "resumed_request_fingerprint_count"
        ],
        process_id=parent_receipt["terminal_process_id"],
    )
    try:
        expected_pit = p15_run._pit_summary(
            parent_root, dummy_context, manifest_sha
        )
    except p15_run.P15RunError as exc:
        raise P15ContinuationError("parent_pit_summary_invalid") from exc
    if parent_receipt.get("pit") != expected_pit:
        raise P15ContinuationError("parent_pit_summary_mismatch")

    config_relative = parent_runtime["config_path"]
    engine_relative = "research/strategy_workspace/alpha_feasibility.py"
    if (
        parent_runtime["files"].get(config_relative)
        != _file_sha256(Path(config_path).resolve(), "continuation_config_invalid")
        or parent_runtime["files"].get(engine_relative)
        != parent_report.get("alpha_feasibility_engine_sha256")
    ):
        raise P15ContinuationError("parent_immutable_runtime_mismatch")

    parent_binding = {
        "network_run_id": parent_receipt["network_run_id"],
        "run_claim_sha256": parent_claim_sha,
        "receipt_sha256": parent_receipt_sha,
        "report_sha256": parent_report_sha,
        "pit_coverage_report_sha256": coverage_sha,
        "pit_manifest_sha256": manifest_sha,
        "runtime_bundle_sha256": parent_runtime["bundle_sha256"],
        "experiment_config_canonical_sha256": parent_report[
            "experiment_config_canonical_sha256"
        ],
    }
    return _ParentEvidence(
        plan=plan,
        experiment=experiment,
        parent_binding=parent_binding,
        parent_runtime_bundle=parent_runtime,
        parent_actual_request_count_by_endpoint=counts,
        reused_tasks=tuple(reused),
        first_unfinished=first_unfinished,
    )


def prepare_continuation(
    *,
    parent_root: Path | str,
    child_root: Path | str,
    config_path: Path | str = reporting.P15_CONFIG_PATH,
    continuation_run_id: str,
    prepared_at: datetime | str,
) -> ContinuationContext:
    """Validate the sealed parent and create an offline child claim.

    The function is byte-idempotent for identical inputs and fails closed for
    any pre-existing child byte that differs.  It neither reads a credential
    nor calls a transport.
    """

    if (
        p15_run.RUN_ID.fullmatch(str(continuation_run_id)) is None
        or continuation_run_id in {".", ".."}
    ):
        raise P15ContinuationError("continuation_run_id_invalid")
    timestamp = _timestamp(prepared_at, "continuation_prepared_at_invalid")
    parent, child = _validate_roots(Path(parent_root), Path(child_root))
    config = Path(config_path)
    evidence = _validate_parent_evidence(parent, config)
    current_runtime = _current_runtime_bundle(config, evidence.experiment)
    immutable = _immutable_strategy_bundle(evidence.experiment, current_runtime)
    engine_relative = "research/strategy_workspace/alpha_feasibility.py"
    if (
        evidence.parent_runtime_bundle["files"].get(engine_relative)
        != immutable["alpha_engine_sha256"]
        or evidence.parent_binding["experiment_config_canonical_sha256"]
        != immutable["experiment_config_canonical_sha256"]
    ):
        raise P15ContinuationError("immutable_parent_child_drift")

    reuse_manifest = _self_hash(
        {
            "schema_version": "tushare-alpha-feasibility-continuation-reuse-manifest.v1",
            "generated_at": timestamp,
            "experiment_id": evidence.experiment["experiment_id"],
            "collection_plan_sha256": evidence.plan.plan_sha256,
            "parent": dict(evidence.parent_binding),
            "successful_prefix_count": SUCCESSFUL_PREFIX_COUNT,
            "reused_tasks": [dict(item) for item in evidence.reused_tasks],
            "first_unfinished": dict(evidence.first_unfinished),
            "locked_test_status": dict(LOCKED_TEST_STATUS),
            "locked_test_consumed": False,
        },
        "manifest_sha256",
    )
    try:
        validate_json_schema(reuse_manifest, REUSE_SCHEMA_PATH)
    except SchemaValidationError as exc:
        raise P15ContinuationError("continuation_reuse_schema_invalid") from exc

    claim = _self_hash(
        {
            "schema_version": "tushare-alpha-feasibility-continuation-claim.v1",
            "prepared_at": timestamp,
            "continuation_run_id": continuation_run_id,
            "experiment_id": evidence.experiment["experiment_id"],
            "strategy_id": evidence.experiment["strategy_id"],
            "collection_plan_sha256": evidence.plan.plan_sha256,
            "experiment_config_canonical_sha256": reporting.canonical_sha256(
                evidence.experiment
            ),
            "parent": dict(evidence.parent_binding),
            "reuse_manifest_sha256": reuse_manifest["manifest_sha256"],
            "successful_prefix_count": SUCCESSFUL_PREFIX_COUNT,
            "first_unfinished_task_id": FIRST_UNFINISHED_TASK_ID,
            "parent_runtime_implementation_bundle": dict(
                evidence.parent_runtime_bundle
            ),
            "current_runtime_implementation_bundle": dict(current_runtime),
            "immutable_strategy_bundle": dict(immutable),
            "execution_policy": {
                "minimum_transport_interval_seconds": MINIMUM_TRANSPORT_INTERVAL_SECONDS,
                "rate_limit_fallback_seconds": RATE_LIMIT_FALLBACK_SECONDS,
                "maximum_retry_after_seconds": MAXIMUM_RETRY_AFTER_SECONDS,
                "retry_count_by_failure_shape": {
                    "RATE_LIMITED": 1,
                    "UPSTREAM_SERVER_ERROR": 1,
                },
                "other_failure_retry_count": 0,
                "maximum_cumulative_attempts_per_fingerprint": MAXIMUM_CUMULATIVE_ATTEMPTS,
                "parent_attempt_count": PARENT_ATTEMPT_COUNT,
                "next_attempt_number": PARENT_ATTEMPT_COUNT + 1,
                "first_request": {
                    "endpoint": "index_weight",
                    **FIRST_UNFINISHED_PARAMS,
                },
                "parent_terminal_quarantine_present": True,
                "parent_failure_classification": PARENT_CLASSIFICATION,
                "classification_inference_forbidden": True,
            },
            "locked_test_status": dict(LOCKED_TEST_STATUS),
            "locked_test_consumed": False,
        },
        "claim_sha256",
    )
    try:
        validate_json_schema(claim, CLAIM_SCHEMA_PATH)
    except SchemaValidationError as exc:
        raise P15ContinuationError("continuation_claim_schema_invalid") from exc

    _publish(
        child / REUSE_MANIFEST_FILENAME,
        reuse_manifest,
        "continuation_reuse_create_only_mismatch",
    )
    _publish(
        child / CONTINUATION_CLAIM_FILENAME,
        claim,
        "continuation_claim_create_only_mismatch",
    )
    return ContinuationContext(
        parent_root=parent,
        child_root=child,
        plan=evidence.plan,
        experiment=evidence.experiment,
        reuse_manifest=reuse_manifest,
        claim=claim,
        parent_actual_request_count_by_endpoint=evidence.parent_actual_request_count_by_endpoint,
    )


def load_prepared_continuation(
    *,
    parent_root: Path | str,
    child_root: Path | str,
    config_path: Path | str = reporting.P15_CONFIG_PATH,
) -> ContinuationContext:
    """Re-verify both parent and child preparation without weakening replay."""

    child = Path(child_root)
    claim = _canonical_object(
        child / CONTINUATION_CLAIM_FILENAME, "continuation_claim_invalid"
    )
    reuse = _canonical_object(
        child / REUSE_MANIFEST_FILENAME, "continuation_reuse_invalid"
    )
    try:
        validate_json_schema(claim, CLAIM_SCHEMA_PATH)
        validate_json_schema(reuse, REUSE_SCHEMA_PATH)
    except SchemaValidationError as exc:
        raise P15ContinuationError("continuation_preparation_schema_invalid") from exc
    _verify_self_hash(claim, "claim_sha256", "continuation_claim_invalid")
    _verify_self_hash(reuse, "manifest_sha256", "continuation_reuse_invalid")
    if claim.get("reuse_manifest_sha256") != reuse.get("manifest_sha256"):
        raise P15ContinuationError("continuation_claim_reuse_mismatch")
    return prepare_continuation(
        parent_root=parent_root,
        child_root=child_root,
        config_path=config_path,
        continuation_run_id=str(claim["continuation_run_id"]),
        prepared_at=str(claim["prepared_at"]),
    )


ParentReuseImporter = Callable[..., Any]


def _default_parent_reuse_importer(**kwargs: Any) -> Any:
    importer = getattr(data_lane, "import_parent_reuse_task_v2", None)
    if not callable(importer):
        raise P15ContinuationError("parent_reuse_import_v2_unavailable")
    try:
        return importer(**kwargs)
    except data_lane.AlphaFeasibilityDataError as exc:
        raise P15ContinuationError("parent_reuse_import_v2_failed") from exc


def _copy_canonical_artifact(source: Path, destination: Path, code: str) -> None:
    value = _canonical_object(source, code)
    _publish(destination, value, code)


def _stage_reused_task_records(
    context: ContinuationContext,
) -> list[Mapping[str, Any]]:
    parent_store = data_lane.CreateOnlyTaskStore(context.parent_root)
    child_store = data_lane.CreateOnlyTaskStore(context.child_root)
    items = context.reuse_manifest["reused_tasks"]
    records: list[Mapping[str, Any]] = []
    for ordinal, (task, item) in enumerate(
        zip(context.plan.pit_tasks[:SUCCESSFUL_PREFIX_COUNT], items, strict=True),
        start=1,
    ):
        if (
            item.get("ordinal") != ordinal
            or item.get("task_id") != task.task_id
            or item.get("task_sha256") != data_lane.canonical_sha256(task.to_dict())
            or child_store.started_path(task).exists()
            or child_store.quarantine_path(task).exists()
            or not child_store.is_complete(task)
        ):
            raise P15ContinuationError("parent_reuse_child_prefix_invalid")
        try:
            result = child_store._load_response(task)
        except data_lane.AlphaFeasibilityDataError as exc:
            raise P15ContinuationError("parent_reuse_child_response_invalid") from exc
        imported = _canonical_object(
            child_store.import_path(task), "parent_reuse_child_import_invalid"
        )
        import_sha = _verify_self_hash(
            imported,
            "import_artifact_sha256",
            "parent_reuse_child_import_invalid",
        )
        parent_raw_sha = _artifact_sha(
            parent_store.raw_path(task), "parent_reuse_parent_raw_invalid"
        )
        parent_response_sha = _artifact_sha(
            parent_store.response_path(task),
            "parent_reuse_parent_response_invalid",
        )
        child_raw_sha = _artifact_sha(
            child_store.raw_path(task), "parent_reuse_child_raw_invalid"
        )
        child_response_sha = _artifact_sha(
            child_store.response_path(task),
            "parent_reuse_child_response_invalid",
        )
        if (
            imported.get("schema_version")
            != "tushare-alpha-feasibility-task-import.v2"
            or result.request_origin != "offline_parent_run_reuse"
            or result.network_request_count != 0
            or parent_raw_sha != item.get("raw_artifact_sha256")
            or parent_response_sha != item.get("response_file_sha256")
            or child_raw_sha != parent_raw_sha
            or child_response_sha != parent_response_sha
        ):
            raise P15ContinuationError("parent_reuse_child_binding_invalid")
        records.append(
            {
                "ordinal": ordinal,
                "task_id": task.task_id,
                "task_sha256": item["task_sha256"],
                "parent_raw_artifact_sha256": parent_raw_sha,
                "parent_response_file_sha256": parent_response_sha,
                "child_raw_artifact_sha256": child_raw_sha,
                "child_response_file_sha256": child_response_sha,
                "child_import_artifact_sha256": import_sha,
                "import_schema_version": imported["schema_version"],
                "request_origin": result.request_origin,
                "network_request_count": result.network_request_count,
            }
        )
    return records


def _stage_first_unfinished_evidence(
    context: ContinuationContext, *, require_pristine_tail: bool
) -> Mapping[str, Any]:
    parent_store = data_lane.CreateOnlyTaskStore(context.parent_root)
    child_store = data_lane.CreateOnlyTaskStore(context.child_root)
    task = context.plan.pit_tasks[SUCCESSFUL_PREFIX_COUNT]
    source = context.reuse_manifest["first_unfinished"]
    try:
        child_store._load_started(task)
        child_attempts = child_store._load_attempts(task)
    except data_lane.AlphaFeasibilityDataError as exc:
        raise P15ContinuationError("parent_reuse_first_unfinished_invalid") from exc
    parent_started_sha = _artifact_sha(
        parent_store.started_path(task), "parent_started_artifact_invalid"
    )
    parent_attempt_sha = _artifact_sha(
        parent_store.attempt_path(task, 1), "parent_attempt_artifact_invalid"
    )
    child_started_sha = _artifact_sha(
        child_store.started_path(task), "parent_reuse_child_started_invalid"
    )
    child_attempt_sha = _artifact_sha(
        child_store.attempt_path(task, 1), "parent_reuse_child_attempt_invalid"
    )
    if (
        task.task_id != FIRST_UNFINISHED_TASK_ID
        or len(child_attempts) < PARENT_ATTEMPT_COUNT
        or parent_started_sha != source.get("started_artifact_sha256")
        or source.get("attempt_artifact_sha256_by_number") != [parent_attempt_sha]
        or child_started_sha != parent_started_sha
        or child_attempt_sha != parent_attempt_sha
        or child_store.import_path(task).exists()
    ):
        raise P15ContinuationError("parent_reuse_first_unfinished_invalid")
    if require_pristine_tail:
        if (
            len(child_attempts) != PARENT_ATTEMPT_COUNT
            or child_store.response_path(task).exists()
            or child_store.raw_path(task).exists()
            or child_store.quarantine_path(task).exists()
        ):
            raise P15ContinuationError("parent_reuse_first_unfinished_not_pristine")
        for later in context.plan.pit_tasks[SUCCESSFUL_PREFIX_COUNT + 1 :]:
            later_attempt_dir = (
                context.child_root / "attempts" / later.task_id
            )
            if any(
                path.exists()
                for path in (
                    child_store.started_path(later),
                    child_store.import_path(later),
                    child_store.response_path(later),
                    child_store.raw_path(later),
                    child_store.quarantine_path(later),
                    later_attempt_dir,
                )
            ):
                raise P15ContinuationError("parent_reuse_tail_not_pristine")
    return {
        "ordinal": FIRST_UNFINISHED_ORDINAL,
        "task_id": task.task_id,
        "task_sha256": source["task_sha256"],
        "parent_started_artifact_sha256": parent_started_sha,
        "parent_attempt_artifact_sha256_by_number": [parent_attempt_sha],
        "child_started_artifact_sha256": child_started_sha,
        "child_attempt_artifact_sha256_by_number": [child_attempt_sha],
        "parent_attempt_count": PARENT_ATTEMPT_COUNT,
        "next_attempt_number": PARENT_ATTEMPT_COUNT + 1,
        "parent_terminal_quarantine_copied": False,
    }


def _load_parent_reuse_stage(
    context: ContinuationContext, *, require_pristine_tail: bool = False
) -> Mapping[str, Any]:
    stage = _canonical_object(
        context.child_root / PARENT_REUSE_STAGE_FILENAME,
        "parent_reuse_stage_invalid",
    )
    try:
        validate_json_schema(stage, PARENT_REUSE_STAGE_SCHEMA_PATH)
    except SchemaValidationError as exc:
        raise P15ContinuationError("parent_reuse_stage_schema_invalid") from exc
    _verify_self_hash(stage, "stage_sha256", "parent_reuse_stage_invalid")
    if (
        stage.get("continuation_run_id") != context.claim["continuation_run_id"]
        or stage.get("continuation_claim_sha256") != context.claim["claim_sha256"]
        or stage.get("reuse_manifest_sha256")
        != context.reuse_manifest["manifest_sha256"]
        or stage.get("collection_plan_sha256") != context.plan.plan_sha256
        or stage.get("parent") != context.claim["parent"]
        or stage.get("reused_task_artifacts")
        != _stage_reused_task_records(context)
        or stage.get("first_unfinished_evidence")
        != _stage_first_unfinished_evidence(
            context, require_pristine_tail=require_pristine_tail
        )
    ):
        raise P15ContinuationError("parent_reuse_stage_binding_invalid")
    _validate_locked(stage, "parent_reuse_stage_locked_policy_invalid")
    return stage


def stage_parent_reuse(
    *,
    context: ContinuationContext,
    staged_at: datetime | str,
    import_parent_reuse_task_v2: ParentReuseImporter | None = None,
) -> tuple[Mapping[str, Any], Path]:
    """Materialize the parent prefix and attempt #1 without copying quarantine.

    The injected/default importer is the only data-lane writer for the v2
    parent-reuse marker.  Its keyword contract is ``task``, ``parent_root``,
    ``child_root``, ``parent_binding`` and ``reuse_evidence``.  This function
    then independently verifies every resulting byte through the task store.
    """

    timestamp = _timestamp(staged_at, "parent_reuse_staged_at_invalid")
    stage_path = context.child_root / PARENT_REUSE_STAGE_FILENAME
    if stage_path.exists():
        stage = _load_parent_reuse_stage(
            context,
            require_pristine_tail=not (
                context.child_root / NETWORK_PROCESS_FILENAME
            ).exists(),
        )
        return stage, stage_path
    if (
        (context.child_root / NETWORK_PROCESS_FILENAME).exists()
        or (context.child_root / CONTINUATION_RECEIPT_FILENAME).exists()
    ):
        raise P15ContinuationError("parent_reuse_stage_missing_after_execution")

    importer = import_parent_reuse_task_v2 or _default_parent_reuse_importer
    for task, item in zip(
        context.plan.pit_tasks[:SUCCESSFUL_PREFIX_COUNT],
        context.reuse_manifest["reused_tasks"],
        strict=True,
    ):
        try:
            importer(
                task=task,
                parent_root=context.parent_root,
                child_root=context.child_root,
                parent_binding=context.claim["parent"],
                reuse_evidence=item,
            )
        except P15ContinuationError:
            raise
        except data_lane.AlphaFeasibilityDataError as exc:
            raise P15ContinuationError("parent_reuse_import_v2_failed") from exc
        except Exception as exc:
            raise P15ContinuationError("parent_reuse_import_v2_protocol_invalid") from exc

    parent_store = data_lane.CreateOnlyTaskStore(context.parent_root)
    child_store = data_lane.CreateOnlyTaskStore(context.child_root)
    first_unfinished = context.plan.pit_tasks[SUCCESSFUL_PREFIX_COUNT]
    if child_store.quarantine_path(first_unfinished).exists():
        raise P15ContinuationError("parent_terminal_quarantine_must_not_be_copied")
    _copy_canonical_artifact(
        parent_store.started_path(first_unfinished),
        child_store.started_path(first_unfinished),
        "parent_reuse_started_create_only_mismatch",
    )
    _copy_canonical_artifact(
        parent_store.attempt_path(first_unfinished, 1),
        child_store.attempt_path(first_unfinished, 1),
        "parent_reuse_attempt_create_only_mismatch",
    )
    reused_records = _stage_reused_task_records(context)
    first_evidence = _stage_first_unfinished_evidence(
        context, require_pristine_tail=True
    )
    stage = _self_hash(
        {
            "schema_version": "tushare-alpha-feasibility-continuation-parent-reuse-stage.v1",
            "staged_at": timestamp,
            "continuation_run_id": context.claim["continuation_run_id"],
            "continuation_claim_sha256": context.claim["claim_sha256"],
            "reuse_manifest_sha256": context.reuse_manifest["manifest_sha256"],
            "collection_plan_sha256": context.plan.plan_sha256,
            "parent": dict(context.claim["parent"]),
            "successful_prefix_count": SUCCESSFUL_PREFIX_COUNT,
            "reused_task_artifacts": reused_records,
            "first_unfinished_evidence": first_evidence,
            "child_completed_prefix_count": SUCCESSFUL_PREFIX_COUNT,
            "first_unfinished_task_id": FIRST_UNFINISHED_TASK_ID,
            "next_task_ordinal": FIRST_UNFINISHED_ORDINAL,
            "parent_terminal_quarantine_copied": False,
            "locked_test_status": dict(LOCKED_TEST_STATUS),
            "locked_test_consumed": False,
        },
        "stage_sha256",
    )
    try:
        validate_json_schema(stage, PARENT_REUSE_STAGE_SCHEMA_PATH)
    except SchemaValidationError as exc:
        raise P15ContinuationError("parent_reuse_stage_schema_invalid") from exc
    _publish(stage_path, stage, "parent_reuse_stage_create_only_mismatch")
    return _load_parent_reuse_stage(context, require_pristine_tail=True), stage_path


def _load_network_process_marker(
    context: ContinuationContext,
) -> Mapping[str, Any]:
    marker = _canonical_object(
        context.child_root / NETWORK_PROCESS_FILENAME,
        "continuation_network_process_marker_invalid",
    )
    try:
        validate_json_schema(marker, NETWORK_PROCESS_SCHEMA_PATH)
    except SchemaValidationError as exc:
        raise P15ContinuationError(
            "continuation_network_process_marker_schema_invalid"
        ) from exc
    _verify_self_hash(
        marker, "marker_sha256", "continuation_network_process_marker_invalid"
    )
    stage = _load_parent_reuse_stage(context)
    if (
        marker.get("continuation_run_id") != context.claim["continuation_run_id"]
        or marker.get("continuation_claim_sha256") != context.claim["claim_sha256"]
        or marker.get("reuse_manifest_sha256")
        != context.reuse_manifest["manifest_sha256"]
        or marker.get("parent_reuse_stage_sha256") != stage["stage_sha256"]
    ):
        raise P15ContinuationError("continuation_network_process_binding_invalid")
    _validate_locked(marker, "continuation_network_process_locked_policy_invalid")
    return marker


def start_network_process(
    *,
    context: ContinuationContext,
    network_process_id: str,
    started_at: datetime | str,
) -> tuple[Mapping[str, Any], Path]:
    """Consume the one-shot network-process authorization with one marker."""

    marker_path = context.child_root / NETWORK_PROCESS_FILENAME
    if marker_path.exists():
        raise P15ContinuationError("continuation_network_process_already_started")
    if (
        p15_run.RUN_ID.fullmatch(str(network_process_id)) is None
        or network_process_id in {".", ".."}
    ):
        raise P15ContinuationError("continuation_network_process_id_invalid")
    timestamp = _timestamp(started_at, "continuation_network_process_started_at_invalid")
    stage = _load_parent_reuse_stage(context, require_pristine_tail=True)
    marker = _self_hash(
        {
            "schema_version": "tushare-alpha-feasibility-continuation-network-process.v1",
            "network_process_id": network_process_id,
            "started_at": timestamp,
            "continuation_run_id": context.claim["continuation_run_id"],
            "continuation_claim_sha256": context.claim["claim_sha256"],
            "reuse_manifest_sha256": context.reuse_manifest["manifest_sha256"],
            "parent_reuse_stage_sha256": stage["stage_sha256"],
            "completed_request_fingerprint_count_at_start": SUCCESSFUL_PREFIX_COUNT,
            "first_unfinished_task_id": FIRST_UNFINISHED_TASK_ID,
            "next_attempt_number": PARENT_ATTEMPT_COUNT + 1,
            "network_process_count": 1,
            "locked_test_status": dict(LOCKED_TEST_STATUS),
            "locked_test_consumed": False,
        },
        "marker_sha256",
    )
    try:
        validate_json_schema(marker, NETWORK_PROCESS_SCHEMA_PATH)
    except SchemaValidationError as exc:
        raise P15ContinuationError(
            "continuation_network_process_marker_schema_invalid"
        ) from exc
    _publish(
        marker_path,
        marker,
        "continuation_network_process_create_only_mismatch",
    )
    try:
        data_lane._arm_parent_reuse_continuation_network_process(
            context.child_root,
            marker["marker_sha256"],
        )
    except data_lane.AlphaFeasibilityDataError as exc:
        raise P15ContinuationError(
            "continuation_network_process_authorization_invalid"
        ) from exc
    return marker, marker_path


def _first_response_result_contract(
    *,
    safe: Mapping[str, Any],
    retry_performed: bool,
    result: str,
) -> None:
    code = safe.get("business_code")
    classification = safe.get("classification")
    if code is None:
        null_semantic_fields = (
            "classification",
            "sanitized_msg",
            "msg_sha256",
            "detail_type",
            "safe_detail_projection",
            "detail_sha256",
            "request_id_sha256",
            "retry_after_seconds",
        )
        raw_sha = safe.get("raw_transport_sha256")
        response_sha = safe.get("response_body_sha256")
        byte_count = safe.get("response_byte_count")
        no_response = result == "NO_RESPONSE_TRANSPORT_INTERRUPTION"
        rejected_response = result in {
            "RESPONSE_REJECTED_ADAPTER_PROTOCOL",
            "RESPONSE_REJECTED_DATA_VALIDATION",
        }
        response_evidence_valid = (
            raw_sha == response_sha
            and (
                raw_sha is None
                or (type(raw_sha) is str and _SHA256.fullmatch(raw_sha) is not None)
            )
            and (
                (byte_count is None and raw_sha is None)
                or (type(byte_count) is int and byte_count >= 0)
            )
        )
        valid = bool(
            all(safe.get(field) is None for field in null_semantic_fields)
            and retry_performed is False
            and (
                no_response
                and raw_sha is None
                and response_sha is None
                and byte_count is None
                or rejected_response and response_evidence_valid
            )
        )
    elif code == 0:
        valid = (
            classification is None
            and retry_performed is False
            and result == "FIRST_ATTEMPT_SUCCEEDED"
            and safe.get("retry_after_seconds") is None
        )
    else:
        retryable = classification in {"RATE_LIMITED", "UPSTREAM_SERVER_ERROR"}
        valid = (
            classification in data_lane.BUSINESS_ERROR_CLASSIFICATIONS
            and retry_performed is retryable
            and result
            in (
                {"RETRY_SUCCEEDED", "RETRY_FAILED"}
                if retryable
                else {"NOT_RETRYABLE"}
            )
            and (
                classification == "RATE_LIMITED"
                or safe.get("retry_after_seconds") is None
            )
        )
    if not valid:
        raise P15ContinuationError("first_continuation_response_inconsistent")


def _verify_first_response_evidence(
    context: ContinuationContext, evidence: Mapping[str, Any]
) -> None:
    marker = _load_network_process_marker(context)
    task = context.plan.pit_tasks[SUCCESSFUL_PREFIX_COUNT]
    store = data_lane.CreateOnlyTaskStore(context.child_root)
    safe = evidence["safe_response_semantics"]
    try:
        attempts = store._load_attempts(task)
        requested_at = datetime.fromisoformat(str(safe["requested_at"]))
        completed_at = datetime.fromisoformat(str(safe["completed_at"]))
        published_at = datetime.fromisoformat(str(evidence["published_at"]))
        process_started_at = datetime.fromisoformat(str(marker["started_at"]))
        reconstructed = (
            data_lane.SafeResponseSemantics(
                business_code=safe["business_code"],
                classification=safe["classification"],
                sanitized_msg=safe["sanitized_msg"],
                msg_sha256=safe["msg_sha256"],
                detail_type=safe["detail_type"],
                safe_detail_projection=safe["safe_detail_projection"],
                detail_sha256=safe["detail_sha256"],
                request_id_sha256=safe["request_id_sha256"],
                raw_transport_sha256=safe["raw_transport_sha256"],
                response_body_sha256=safe["response_body_sha256"],
                response_byte_count=safe["response_byte_count"],
                sanitized_params=safe["sanitized_params"],
                requested_fields=tuple(safe["requested_fields"]),
                requested_at=requested_at,
                completed_at=completed_at,
                retry_after_seconds=safe["retry_after_seconds"],
            )
            if safe["business_code"] is not None
            else None
        )
    except (data_lane.AlphaFeasibilityDataError, TypeError, ValueError) as exc:
        raise P15ContinuationError("first_response_evidence_time_invalid") from exc
    if (
        requested_at.tzinfo is None
        or completed_at.tzinfo is None
        or published_at.tzinfo is None
        or process_started_at.tzinfo is None
        or not process_started_at <= requested_at <= completed_at <= published_at
        or (reconstructed is not None and reconstructed.to_dict() != safe)
        or len(attempts) < 2
        or safe.get("sanitized_params") != dict(task.params)
        or safe.get("requested_fields") != list(task.fields)
    ):
        raise P15ContinuationError("first_response_evidence_time_invalid")
    _first_response_result_contract(
        safe=safe,
        retry_performed=evidence["retry_performed"],
        result=evidence["result"],
    )
    if safe["business_code"] is None:
        quarantine_path = store.quarantine_path(task)
        quarantine = _canonical_object(
            quarantine_path, "first_response_transport_quarantine_invalid"
        )
        failure_code = quarantine.get("failure_code")
        expected_result = (
            "NO_RESPONSE_TRANSPORT_INTERRUPTION"
            if failure_code in data_lane.RETRYABLE_ATTEMPT_FAILURES
            else "RESPONSE_REJECTED_ADAPTER_PROTOCOL"
            if failure_code in data_lane.ADAPTER_PROTOCOL_FAILURES
            else "RESPONSE_REJECTED_DATA_VALIDATION"
        )
        expected_raw_sha = (
            None
            if expected_result == "NO_RESPONSE_TRANSPORT_INTERRUPTION"
            else quarantine.get("raw_transport_sha256")
        )
        expected_byte_count = (
            None
            if expected_result == "NO_RESPONSE_TRANSPORT_INTERRUPTION"
            else quarantine.get("response_byte_count")
        )
        if (
            len(attempts) != 2
            or store.is_complete(task)
            or store._load_business_errors(task)
            or quarantine.get("task_id") != task.task_id
            or quarantine.get("terminal_attempt_number") != 2
            or quarantine.get("business_error_classification") is not None
            or evidence["result"] != expected_result
            or safe.get("raw_transport_sha256") != expected_raw_sha
            or safe.get("response_body_sha256") != expected_raw_sha
            or safe.get("response_byte_count") != expected_byte_count
            or evidence["evidence_artifact_sha256"]
            != _artifact_sha(
                quarantine_path, "first_response_transport_quarantine_invalid"
            )
        ):
            raise P15ContinuationError(
                "first_response_transport_interruption_invalid"
            )
    elif safe["business_code"] == 0:
        if len(attempts) != 2 or not store.is_complete(task):
            raise P15ContinuationError("first_continuation_attempt_invalid")
        try:
            loaded = store._load_response(task)
            raw = store.raw_path(task).read_bytes()
            replayed_safe = data_lane.extract_safe_response_semantics(
                raw,
                task=task,
                token=None,
                requested_at=requested_at,
                completed_at=completed_at,
            )
        except (OSError, data_lane.AlphaFeasibilityDataError) as exc:
            raise P15ContinuationError(
                "first_response_success_replay_invalid"
            ) from exc
        response_path = store.response_path(task)
        if (
            loaded.request_origin != "network"
            or loaded.network_request_count != 2
            or loaded.wire_response_sha256 != safe["response_body_sha256"]
            or replayed_safe.to_dict() != safe
            or evidence["evidence_artifact_sha256"]
            != _artifact_sha(response_path, "first_response_artifact_invalid")
        ):
            raise P15ContinuationError("first_response_success_binding_invalid")
    else:
        business_error_path = store.business_error_path(task, 2)
        artifact = _canonical_object(
            business_error_path, "first_response_business_error_invalid"
        )
        if (
            artifact.get("attempt_number") != 2
            or artifact.get("evidence") != safe
            or evidence["evidence_artifact_sha256"]
            != _artifact_sha(
                business_error_path, "first_response_business_error_invalid"
            )
        ):
            raise P15ContinuationError("first_response_business_error_invalid")
        if evidence["retry_performed"]:
            if len(attempts) != 3:
                raise P15ContinuationError("first_continuation_retry_result_invalid")
            succeeded = store.is_complete(task)
            if succeeded != (evidence["result"] == "RETRY_SUCCEEDED"):
                raise P15ContinuationError("first_continuation_retry_result_invalid")
            if not succeeded and not store.quarantine_path(task).is_file():
                raise P15ContinuationError("first_continuation_retry_result_invalid")
        elif (
            len(attempts) != 2
            or store.is_complete(task)
            or not store.quarantine_path(task).is_file()
        ):
            raise P15ContinuationError("first_continuation_retry_result_invalid")


def publish_first_continuation_response_evidence(
    *,
    context: ContinuationContext,
    safe_response_semantics: data_lane.SafeResponseSemantics | None,
    retry_performed: bool,
    result: str,
    published_at: datetime | str,
    requested_at: datetime | str | None = None,
    completed_at: datetime | str | None = None,
) -> tuple[Mapping[str, Any], Path]:
    """Seal the already-scanned first continuation response as a sidecar."""

    if safe_response_semantics is not None and not isinstance(
        safe_response_semantics, data_lane.SafeResponseSemantics
    ):
        raise P15ContinuationError("first_response_safe_semantics_required")
    timestamp = _timestamp(published_at, "first_response_published_at_invalid")
    marker = _load_network_process_marker(context)
    task = context.plan.pit_tasks[SUCCESSFUL_PREFIX_COUNT]
    store = data_lane.CreateOnlyTaskStore(context.child_root)
    if safe_response_semantics is None:
        if requested_at is None or completed_at is None:
            raise P15ContinuationError("first_response_transport_timing_required")
        requested_text = _timestamp(
            requested_at, "first_response_requested_at_invalid"
        )
        completed_text = _timestamp(
            completed_at, "first_response_completed_at_invalid"
        )
        quarantine = _canonical_object(
            store.quarantine_path(task),
            "first_response_transport_quarantine_invalid",
        )
        response_rejected = result in {
            "RESPONSE_REJECTED_ADAPTER_PROTOCOL",
            "RESPONSE_REJECTED_DATA_VALIDATION",
        }
        raw_sha = quarantine.get("raw_transport_sha256") if response_rejected else None
        byte_count = quarantine.get("response_byte_count") if response_rejected else None
        safe = {
            "business_code": None,
            "classification": None,
            "sanitized_msg": None,
            "msg_sha256": None,
            "detail_type": None,
            "safe_detail_projection": None,
            "detail_sha256": None,
            "request_id_sha256": None,
            "raw_transport_sha256": raw_sha,
            "response_body_sha256": raw_sha,
            "response_byte_count": byte_count,
            "sanitized_params": dict(task.params),
            "requested_fields": list(task.fields),
            "requested_at": requested_text,
            "completed_at": completed_text,
            "retry_after_seconds": None,
        }
    else:
        if requested_at is not None or completed_at is not None:
            raise P15ContinuationError("first_response_transport_timing_unexpected")
        safe = safe_response_semantics.to_dict()
    _first_response_result_contract(
        safe=safe,
        retry_performed=retry_performed,
        result=result,
    )
    evidence_path = (
        store.quarantine_path(task)
        if safe["business_code"] is None
        else store.response_path(task)
        if safe["business_code"] == 0
        else store.business_error_path(task, 2)
    )
    evidence = _self_hash(
        {
            "schema_version": "tushare-alpha-feasibility-continuation-first-response.v1",
            "published_at": timestamp,
            "continuation_run_id": context.claim["continuation_run_id"],
            "continuation_claim_sha256": context.claim["claim_sha256"],
            "network_process_marker_sha256": marker["marker_sha256"],
            "task_id": task.task_id,
            "attempt_number": 2,
            "safe_response_semantics": safe,
            "retry_performed": retry_performed,
            "result": result,
            "evidence_artifact_sha256": _artifact_sha(
                evidence_path, "first_response_artifact_invalid"
            ),
            "locked_test_status": dict(LOCKED_TEST_STATUS),
            "locked_test_consumed": False,
        },
        "evidence_sha256",
    )
    try:
        validate_json_schema(evidence, FIRST_RESPONSE_EVIDENCE_SCHEMA_PATH)
    except SchemaValidationError as exc:
        raise P15ContinuationError("first_response_evidence_schema_invalid") from exc
    _verify_first_response_evidence(context, evidence)
    path = _publish(
        context.child_root / FIRST_RESPONSE_EVIDENCE_FILENAME,
        evidence,
        "first_response_evidence_create_only_mismatch",
    )
    return evidence, path


def _load_first_response_evidence(
    context: ContinuationContext,
) -> Mapping[str, Any]:
    evidence = _canonical_object(
        context.child_root / FIRST_RESPONSE_EVIDENCE_FILENAME,
        "first_response_evidence_invalid",
    )
    try:
        validate_json_schema(evidence, FIRST_RESPONSE_EVIDENCE_SCHEMA_PATH)
    except SchemaValidationError as exc:
        raise P15ContinuationError("first_response_evidence_schema_invalid") from exc
    _verify_self_hash(
        evidence, "evidence_sha256", "first_response_evidence_invalid"
    )
    marker = _load_network_process_marker(context)
    if (
        evidence.get("continuation_run_id") != context.claim["continuation_run_id"]
        or evidence.get("continuation_claim_sha256") != context.claim["claim_sha256"]
        or evidence.get("network_process_marker_sha256") != marker["marker_sha256"]
    ):
        raise P15ContinuationError("first_response_evidence_binding_invalid")
    _validate_locked(evidence, "first_response_evidence_locked_policy_invalid")
    _verify_first_response_evidence(context, evidence)
    return evidence


def _validate_pit_receipt_summary(
    context: ContinuationContext,
    value: Mapping[str, Any],
    *,
    terminal_stage: str,
    terminal_status: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise P15ContinuationError("continuation_pit_summary_invalid")
    expected_keys = {
        "months_expected",
        "months_reused",
        "months_newly_observed",
        "months_total_observed",
        "missing_months",
        "snapshot_count",
        "union_instrument_count",
        "coverage_status",
    }
    if set(value) != expected_keys:
        raise P15ContinuationError("continuation_pit_summary_invalid")
    for field in (
        "months_expected",
        "months_reused",
        "months_newly_observed",
        "months_total_observed",
        "snapshot_count",
        "union_instrument_count",
    ):
        if type(value[field]) is not int or value[field] < 0:
            raise P15ContinuationError("continuation_pit_summary_invalid")
    missing = value["missing_months"]
    if (
        value["months_expected"] != len(context.plan.pit_tasks)
        or value["months_reused"] != SUCCESSFUL_PREFIX_COUNT
        or not isinstance(missing, list)
        or len(set(missing)) != len(missing)
        or any(
            type(month) is not str
            or re.fullmatch(r"20(?:19|20|21|22|23)-(?:0[1-9]|1[0-2])", month)
            is None
            for month in missing
        )
        or value["months_newly_observed"] + SUCCESSFUL_PREFIX_COUNT
        != value["months_total_observed"]
        or value["months_total_observed"] + len(missing)
        != len(context.plan.pit_tasks)
        or value["months_total_observed"] > len(context.plan.pit_tasks)
        or missing
        != [
            _task_month(task)
            for task in context.plan.pit_tasks[value["months_total_observed"] :]
        ]
        or value["snapshot_count"] < value["months_total_observed"]
    ):
        raise P15ContinuationError("continuation_pit_summary_inconsistent")
    complete = value["coverage_status"] == "COMPLETE"
    if value["coverage_status"] not in {
        "COMPLETE",
        "BLOCKED_PIT_SOURCE_COVERAGE",
    }:
        raise P15ContinuationError("continuation_pit_status_invalid")
    if complete != (
        value["months_total_observed"] == 73
        and not missing
        and value["snapshot_count"] >= 73
        and value["union_instrument_count"] > 0
    ):
        raise P15ContinuationError("continuation_pit_summary_inconsistent")
    if terminal_stage != "PIT" and not complete:
        raise P15ContinuationError("continuation_post_pit_without_complete_pit")
    if terminal_status == "COMPLETED" and not complete:
        raise P15ContinuationError("continuation_completed_pit_incomplete")
    if (
        terminal_status == "BLOCKED_PIT_SOURCE_COVERAGE"
        and (terminal_stage != "PIT" or complete)
    ):
        raise P15ContinuationError("continuation_pit_terminal_mismatch")
    return dict(value)


def _validate_first_continuation_response(
    context: ContinuationContext,
    value: Mapping[str, Any],
    continuation_counts: Mapping[str, int],
) -> dict[str, Any]:
    evidence = _load_first_response_evidence(context)
    safe = evidence["safe_response_semantics"]
    expected = {
        "task_id": evidence["task_id"],
        "attempt_number": evidence["attempt_number"],
        "business_code": safe["business_code"],
        "classification": safe["classification"],
        "sanitized_msg": safe["sanitized_msg"],
        "detail_type": safe["detail_type"],
        "safe_detail_projection": safe["safe_detail_projection"],
        "msg_sha256": safe["msg_sha256"],
        "detail_sha256": safe["detail_sha256"],
        "request_id_sha256": safe["request_id_sha256"],
        "raw_transport_sha256": safe["raw_transport_sha256"],
        "response_body_sha256": safe["response_body_sha256"],
        "response_byte_count": safe["response_byte_count"],
        "requested_at": safe["requested_at"],
        "completed_at": safe["completed_at"],
        "retry_after_seconds": safe["retry_after_seconds"],
        "retry_performed": evidence["retry_performed"],
        "result": evidence["result"],
        "evidence_artifact_sha256": evidence["evidence_artifact_sha256"],
    }
    minimum_requests = 2 if evidence["retry_performed"] else 1
    if (
        not isinstance(value, Mapping)
        or dict(value) != expected
        or continuation_counts["index_weight"] < minimum_requests
    ):
        raise P15ContinuationError("first_continuation_response_binding_invalid")
    return expected


_CLASSIFICATION_TO_TERMINAL_STATUS = {
    "RATE_LIMITED": "BLOCKED_UPSTREAM_RATE_LIMIT",
    "PERMISSION_DENIED": "BLOCKED_PROVIDER_PERMISSION",
    "ACCOUNT_OR_QUOTA_LIMIT": "BLOCKED_PROVIDER_QUOTA",
    "INVALID_PARAMETER": "BLOCKED_INVALID_PARAMETER",
    "UPSTREAM_SERVER_ERROR": "BLOCKED_UPSTREAM_SERVER",
    "UPSTREAM_UNKNOWN_ERROR": "BLOCKED_UPSTREAM_UNDOCUMENTED_CODE",
}
_TERMINALS_REQUIRING_FAILURE_ARTIFACT = frozenset(
    {
        "BLOCKED_UPSTREAM_RATE_LIMIT",
        "BLOCKED_PROVIDER_PERMISSION",
        "BLOCKED_PROVIDER_QUOTA",
        "BLOCKED_INVALID_PARAMETER",
        "BLOCKED_UPSTREAM_SERVER",
        "BLOCKED_UPSTREAM_UNDOCUMENTED_CODE",
        "BLOCKED_ADAPTER_PROTOCOL",
    }
)


def _terminal_status_for_classification(
    classification: str, *, terminal_stage: str
) -> str:
    if classification not in data_lane.BUSINESS_ERROR_CLASSIFICATIONS:
        raise P15ContinuationError("terminal_failure_classification_mismatch")
    if classification == "DATA_UNAVAILABLE":
        return (
            "BLOCKED_PIT_SOURCE_COVERAGE"
            if terminal_stage == "PIT"
            else "BLOCKED_DATA"
        )
    return _CLASSIFICATION_TO_TERMINAL_STATUS[classification]


def _validate_terminal_failure_evidence(
    context: ContinuationContext,
    value: Mapping[str, Any] | None,
    *,
    terminal_status: str,
    terminal_stage: str,
) -> Mapping[str, Any] | None:
    if terminal_status == "COMPLETED":
        if value is not None:
            raise P15ContinuationError("completed_terminal_failure_evidence_present")
        return None
    if value is None:
        if terminal_status in _TERMINALS_REQUIRING_FAILURE_ARTIFACT:
            raise P15ContinuationError("terminal_failure_evidence_missing")
        return None
    if not isinstance(value, Mapping) or set(value) != {
        "task_id",
        "attempt_number",
        "failure_code",
        "classification",
        "attempt_artifact_sha256",
        "quarantine_artifact_sha256",
        "business_error_artifact_sha256",
        "raw_error_artifact_sha256",
    }:
        raise P15ContinuationError("terminal_failure_evidence_invalid")
    task_id = value["task_id"]
    attempt_number = value["attempt_number"]
    if (
        type(task_id) is not str
        or re.fullmatch(r"[a-z_]+-[0-9a-f]{64}", task_id) is None
        or type(attempt_number) is not int
        or not 1 <= attempt_number <= MAXIMUM_CUMULATIVE_ATTEMPTS
        or type(value["failure_code"]) is not str
        or _SAFE_CODE.fullmatch(value["failure_code"]) is None
    ):
        raise P15ContinuationError("terminal_failure_evidence_invalid")
    attempt_path = (
        context.child_root
        / "attempts"
        / task_id
        / f"{attempt_number:06d}.started.json"
    )
    quarantine_path = context.child_root / "quarantine" / f"{task_id}.json"
    quarantine = _canonical_object(
        quarantine_path, "terminal_quarantine_artifact_invalid"
    )
    if (
        _artifact_sha(attempt_path, "terminal_attempt_artifact_invalid")
        != value["attempt_artifact_sha256"]
        or _artifact_sha(quarantine_path, "terminal_quarantine_artifact_invalid")
        != value["quarantine_artifact_sha256"]
        or quarantine.get("task_id") != task_id
        or quarantine.get("failure_code") != value["failure_code"]
    ):
        raise P15ContinuationError("terminal_failure_evidence_binding_invalid")
    classification = value["classification"]
    if classification is not None:
        expected_terminal = _terminal_status_for_classification(
            classification, terminal_stage=terminal_stage
        )
        if (
            quarantine.get("business_error_classification") != classification
            or expected_terminal != terminal_status
        ):
            raise P15ContinuationError("terminal_failure_classification_mismatch")
    elif terminal_status not in {"BLOCKED_ADAPTER_PROTOCOL", "BLOCKED_DATA"}:
        raise P15ContinuationError("terminal_failure_classification_missing")
    optional_paths = {
        "business_error_artifact_sha256": (
            context.child_root
            / "business_errors"
            / task_id
            / f"{attempt_number:06d}.json"
        ),
        "raw_error_artifact_sha256": (
            context.child_root
            / "raw_errors"
            / task_id
            / f"{attempt_number:06d}.json"
        ),
    }
    for field, path in optional_paths.items():
        declared = _sha256_or_none(value[field], "terminal_failure_hash_invalid")
        if (declared is None) != (not path.is_file()):
            raise P15ContinuationError("terminal_failure_evidence_binding_invalid")
        if declared is not None and _artifact_sha(
            path, "terminal_failure_artifact_invalid"
        ) != declared:
            raise P15ContinuationError("terminal_failure_evidence_binding_invalid")
    return dict(value)


def publish_continuation_receipt(
    *,
    context: ContinuationContext,
    terminal_stage: str,
    terminal_status: str,
    generated_at: datetime | str,
    continuation_actual_request_count_by_endpoint: Mapping[str, int],
    completed_request_fingerprint_count: int,
    remaining_blockers: Sequence[str],
    first_continuation_response: Mapping[str, Any],
    pit: Mapping[str, Any],
    terminal_failure_evidence: Mapping[str, Any] | None,
    child_report: Mapping[str, Any] | None = None,
    child_pit_manifest_sha256: str | None = None,
    child_history_manifest_sha256: str | None = None,
) -> tuple[Mapping[str, Any], Path]:
    """Publish one create-only continuation receipt from explicit evidence.

    This API deliberately does not classify provider failures.  The caller must
    supply one exact terminal enum from the controlled execution boundary.
    """

    if terminal_stage not in TERMINAL_STAGES:
        raise P15ContinuationError("continuation_terminal_stage_invalid")
    if terminal_status not in TERMINAL_STATUSES:
        raise P15ContinuationError("continuation_terminal_status_invalid")
    timestamp = _timestamp(generated_at, "continuation_generated_at_invalid")
    stage = _load_parent_reuse_stage(context)
    process_marker = _load_network_process_marker(context)
    current_runtime = _current_runtime_bundle(
        Path(context.claim["current_runtime_implementation_bundle"]["config_path"]),
        context.experiment,
    )
    if current_runtime != context.claim["current_runtime_implementation_bundle"]:
        raise P15ContinuationError("continuation_runtime_drift")

    continuation_counts = _validate_request_counts(
        continuation_actual_request_count_by_endpoint,
        "continuation_request_counts_invalid",
    )
    parent_counts = _validate_request_counts(
        context.parent_actual_request_count_by_endpoint,
        "parent_request_counts_invalid",
    )
    cumulative_counts = {
        endpoint: parent_counts[endpoint] + continuation_counts[endpoint]
        for endpoint in reporting.ALLOWED_ENDPOINTS
    }
    first_response = _validate_first_continuation_response(
        context, first_continuation_response, continuation_counts
    )
    first_response_evidence = _load_first_response_evidence(context)
    pit_summary = _validate_pit_receipt_summary(
        context,
        pit,
        terminal_stage=terminal_stage,
        terminal_status=terminal_status,
    )
    terminal_failure = _validate_terminal_failure_evidence(
        context,
        terminal_failure_evidence,
        terminal_status=terminal_status,
        terminal_stage=terminal_stage,
    )
    if (
        type(completed_request_fingerprint_count) is not int
        or completed_request_fingerprint_count < SUCCESSFUL_PREFIX_COUNT
    ):
        raise P15ContinuationError("continuation_completed_count_invalid")
    blockers = sorted(set(remaining_blockers))
    if any(type(item) is not str or _SAFE_CODE.fullmatch(item) is None for item in blockers):
        raise P15ContinuationError("continuation_blocker_invalid")

    report_sha: str | None = None
    development = validation = concentration = None
    if child_report is not None:
        try:
            reporting.verify_alpha_feasibility_report(
                child_report, experiment=context.experiment
            )
        except reporting.AlphaFeasibilityReportingError as exc:
            raise P15ContinuationError("continuation_report_invalid") from exc
        report_sha = child_report["report_sha256"]
        development = child_report["development_metrics"]
        validation = child_report["validation_metrics"]
        concentration = child_report["concentration_metrics"]
        report_path = context.child_root / reporting.REPORT_FILENAME
        persisted = _canonical_object(report_path, "continuation_report_invalid")
        if dict(persisted) != dict(child_report):
            raise P15ContinuationError("continuation_report_invalid")

    pit_sha = _sha256_or_none(
        child_pit_manifest_sha256, "continuation_pit_manifest_sha_invalid"
    )
    history_sha = _sha256_or_none(
        child_history_manifest_sha256,
        "continuation_history_manifest_sha_invalid",
    )
    if pit_sha is not None:
        persisted_pit = _canonical_object(
            context.child_root / PARENT_PIT_MANIFEST_FILENAME,
            "continuation_pit_manifest_invalid",
        )
        if (
            _verify_self_hash(
                persisted_pit,
                "manifest_sha256",
                "continuation_pit_manifest_invalid",
            )
            != pit_sha
            or persisted_pit.get("pit_months_observed")
            != pit_summary["months_total_observed"]
            or persisted_pit.get("pit_snapshot_count")
            != pit_summary["snapshot_count"]
            or persisted_pit.get("missing_months") != pit_summary["missing_months"]
            or persisted_pit.get("union_instrument_count")
            != pit_summary["union_instrument_count"]
        ):
            raise P15ContinuationError("continuation_pit_manifest_sha_mismatch")
    if history_sha is not None:
        persisted_history = _canonical_object(
            context.child_root / "history_manifest.json",
            "continuation_history_manifest_invalid",
        )
        if _verify_self_hash(
            persisted_history,
            "manifest_sha256",
            "continuation_history_manifest_invalid",
        ) != history_sha:
            raise P15ContinuationError("continuation_history_manifest_sha_mismatch")
    if terminal_stage != "PIT" and pit_sha is None:
        raise P15ContinuationError("continuation_post_pit_manifest_missing")
    if terminal_status == "COMPLETED":
        if (
            terminal_stage != "REPORT"
            or child_report is None
            or child_report.get("terminal_status")
            not in {
                "ALPHA_FEASIBILITY_GO_CANDIDATE",
                "ALPHA_FEASIBILITY_NO_GO",
            }
            or pit_sha is None
            or history_sha is None
            or blockers
        ):
            raise P15ContinuationError("continuation_completed_evidence_incomplete")
    elif not blockers or any(
        value is not None for value in (development, validation, concentration)
    ):
        raise P15ContinuationError("continuation_blocked_evidence_invalid")

    receipt = _self_hash(
        {
            "schema_version": "tushare-alpha-feasibility-continuation-receipt.v1",
            "generated_at": timestamp,
            "continuation_run_id": context.claim["continuation_run_id"],
            "continuation_claim_sha256": context.claim["claim_sha256"],
            "reuse_manifest_sha256": context.reuse_manifest["manifest_sha256"],
            "parent": dict(context.claim["parent"]),
            "parent_reuse_stage_sha256": stage["stage_sha256"],
            "network_process_id": process_marker["network_process_id"],
            "network_process_marker_sha256": process_marker["marker_sha256"],
            "network_process_count": 1,
            "current_runtime_implementation_bundle": dict(current_runtime),
            "terminal_stage": terminal_stage,
            "terminal_status": terminal_status,
            "parent_actual_request_count_by_endpoint": parent_counts,
            "continuation_actual_request_count_by_endpoint": continuation_counts,
            "cumulative_actual_request_count_by_endpoint": cumulative_counts,
            "request_count_semantics": "conservative_durable_pre_transport_attempt_claim",
            "reused_successful_fingerprint_count": SUCCESSFUL_PREFIX_COUNT,
            "completed_request_fingerprint_count": completed_request_fingerprint_count,
            "first_unfinished_task_id": FIRST_UNFINISHED_TASK_ID,
            "resumed_from_month": "2019-07",
            "minimum_transport_interval_seconds": MINIMUM_TRANSPORT_INTERVAL_SECONDS,
            "parent_attempt_count": PARENT_ATTEMPT_COUNT,
            "maximum_cumulative_attempts": MAXIMUM_CUMULATIVE_ATTEMPTS,
            "first_continuation_response": first_response,
            "first_continuation_response_evidence_sha256": first_response_evidence[
                "evidence_sha256"
            ],
            "pit": pit_summary,
            "terminal_failure_evidence": terminal_failure,
            "child_report_sha256": report_sha,
            "child_pit_manifest_sha256": pit_sha,
            "child_history_manifest_sha256": history_sha,
            "development_metrics": development,
            "validation_metrics": validation,
            "concentration_metrics": concentration,
            "remaining_blockers": blockers,
            "safety": dict(reporting.SAFETY),
            "locked_test_status": dict(LOCKED_TEST_STATUS),
            "locked_test_consumed": False,
        },
        "receipt_sha256",
    )
    try:
        validate_json_schema(receipt, RECEIPT_SCHEMA_PATH)
    except SchemaValidationError as exc:
        raise P15ContinuationError("continuation_receipt_schema_invalid") from exc
    path = _publish(
        context.child_root / CONTINUATION_RECEIPT_FILENAME,
        receipt,
        "continuation_receipt_create_only_mismatch",
    )
    return receipt, path


__all__ = [
    "CONTINUATION_CLAIM_FILENAME",
    "CONTINUATION_RECEIPT_FILENAME",
    "ContinuationContext",
    "FIRST_UNFINISHED_PARAMS",
    "FIRST_UNFINISHED_TASK_ID",
    "FIRST_RESPONSE_EVIDENCE_FILENAME",
    "LOCKED_TEST_STATUS",
    "MAXIMUM_CUMULATIVE_ATTEMPTS",
    "MAXIMUM_RETRY_AFTER_SECONDS",
    "MINIMUM_TRANSPORT_INTERVAL_SECONDS",
    "NETWORK_PROCESS_FILENAME",
    "PARENT_REUSE_STAGE_FILENAME",
    "P15ContinuationError",
    "RATE_LIMIT_FALLBACK_SECONDS",
    "REUSE_MANIFEST_FILENAME",
    "SUCCESSFUL_PREFIX_COUNT",
    "TERMINAL_STATUSES",
    "load_prepared_continuation",
    "prepare_continuation",
    "publish_first_continuation_response_evidence",
    "publish_continuation_receipt",
    "stage_parent_reuse",
    "start_network_process",
]
