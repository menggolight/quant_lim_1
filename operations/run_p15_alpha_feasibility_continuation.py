"""Run the one-shot, parent-bound P1.5C Tushare continuation.

The command performs the offline parent verification and create-only reuse
stage before consuming one network-process marker.  It then executes only the
frozen six-endpoint P1.5 plan through the strict continuation data runner.
The 2017-12 through 2019-06 prefix is replayed locally; the first transport
call is required to be the frozen 2019-07 ``index_weight`` fingerprint.

This module has no Paper, broker, account, order, LIVE, or Locked Test path.
Provider response text is never written to stdout or stderr by this boundary.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any

from operations import p15_alpha_feasibility_continuation as continuation
from operations import run_alpha_feasibility as alpha_workflow
from research.market_data import tushare_alpha_feasibility as data_lane
from research.strategy_workspace import alpha_feasibility as engine
from research.strategy_workspace import alpha_feasibility_reporting as reporting


DEFAULT_PARENT_ROOT = Path(
    "data/tmp/alpha-feasibility/tushare-p1-5-2017-2023-20260831"
)
DEFAULT_OUTPUT_ROOT = Path(
    "data/tmp/alpha-feasibility/tushare-p1-5c-continuation-2019-07-20260831"
)
SAFE_CODE = re.compile(r"^[a-z0-9_]{3,96}$")
COMPACT_DATE = re.compile(r"(?<![0-9])(20[0-9]{6})(?![0-9])")
LOCKED_TEST_STATUS = {
    "access": "NOT_ACCESSED",
    "download": "NOT_DOWNLOADED",
    "run": "NOT_RUN",
}
_PARENT_ATTEMPT_COUNTS = {
    "trade_cal": 0,
    "index_weight": 1,
    "daily": 0,
    "adj_factor": 0,
    "index_daily": 0,
    "suspend_d": 0,
}
_CLASSIFICATION_TO_TERMINAL = {
    "RATE_LIMITED": "BLOCKED_UPSTREAM_RATE_LIMIT",
    "PERMISSION_DENIED": "BLOCKED_PROVIDER_PERMISSION",
    "INVALID_PARAMETER": "BLOCKED_INVALID_PARAMETER",
    "UPSTREAM_SERVER_ERROR": "BLOCKED_UPSTREAM_SERVER",
    "ACCOUNT_OR_QUOTA_LIMIT": "BLOCKED_PROVIDER_QUOTA",
    "UPSTREAM_UNKNOWN_ERROR": "BLOCKED_UPSTREAM_UNDOCUMENTED_CODE",
}
_DATA_BLOCKED_STAGES = frozenset(
    {
        "BLOCKED_PIT_MEMBERSHIP",
        "BLOCKED_PIT_SOURCE_COVERAGE",
        "BLOCKED_DATA",
        "BLOCKED_ADAPTER_PROTOCOL",
    }
)


class P15ContinuationWorkflowError(RuntimeError):
    """Sanitized orchestration error which cannot carry provider text."""

    def __init__(self, code: str) -> None:
        self.code = code if SAFE_CODE.fullmatch(str(code)) else "unsafe_error_sanitized"
        super().__init__(self.code)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime, code: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise P15ContinuationWorkflowError(code)
    return value.astimezone(timezone.utc)


def _file_sha256(path: Path, code: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise P15ContinuationWorkflowError(code)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise P15ContinuationWorkflowError(code) from exc
    return digest.hexdigest()


def _load_object(path: Path, code: str) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise P15ContinuationWorkflowError(code)
    try:
        value = data_lane.strict_json_loads(path.read_bytes(), label=code)
    except (OSError, data_lane.AlphaFeasibilityDataError, ValueError) as exc:
        raise P15ContinuationWorkflowError(code) from exc
    if not isinstance(value, Mapping):
        raise P15ContinuationWorkflowError(code)
    return value


class _FirstTransportCapture:
    """Defend the endpoint/date envelope and retain only first-call timing."""

    def __init__(
        self,
        *,
        transport: data_lane.TushareTransport,
        first_task: data_lane.CollectionTask,
        clock: Callable[[], datetime],
    ) -> None:
        self._transport = transport
        self._first_task = first_task
        self._clock = clock
        self.call_count = 0
        self.first_requested_at: datetime | None = None
        self.first_completed_at: datetime | None = None

    def __call__(
        self,
        *,
        endpoint: str,
        params: Mapping[str, str],
        fields: Sequence[str],
        token: str,
        timeout_seconds: int,
        maximum_response_bytes: int,
    ) -> data_lane.TushareHttpResponse | bytes:
        if endpoint not in reporting.ALLOWED_ENDPOINTS:
            raise P15ContinuationWorkflowError("continuation_endpoint_forbidden")
        for supplied in params.values():
            if type(supplied) is not str:
                raise P15ContinuationWorkflowError("continuation_parameter_invalid")
            for match in COMPACT_DATE.finditer(supplied):
                if match.group(1) > "20231231":
                    raise P15ContinuationWorkflowError(
                        "continuation_post_cutoff_request_rejected"
                    )
        if self.call_count == 0 and (
            endpoint != self._first_task.endpoint
            or dict(params) != dict(self._first_task.params)
            or tuple(fields) != self._first_task.fields
        ):
            raise P15ContinuationWorkflowError(
                "continuation_first_request_fingerprint_invalid"
            )
        first_call = self.call_count == 0
        self.call_count += 1
        requested_at = _utc(self._clock(), "continuation_clock_invalid")
        try:
            response = self._transport(
                endpoint=endpoint,
                params=params,
                fields=fields,
                token=token,
                timeout_seconds=timeout_seconds,
                maximum_response_bytes=maximum_response_bytes,
            )
        except Exception:
            completed_at = _utc(self._clock(), "continuation_clock_invalid")
            if first_call:
                self.first_requested_at = requested_at
                self.first_completed_at = completed_at
            raise
        completed_at = _utc(self._clock(), "continuation_clock_invalid")
        if first_call:
            self.first_requested_at = requested_at
            self.first_completed_at = completed_at
        return response


def _safe_semantics_from_mapping(
    value: Mapping[str, Any],
) -> data_lane.SafeResponseSemantics:
    try:
        requested_at = datetime.fromisoformat(str(value["requested_at"]))
        completed_at = datetime.fromisoformat(str(value["completed_at"]))
        requested_fields = tuple(value["requested_fields"])
        return data_lane.SafeResponseSemantics(
            business_code=value["business_code"],
            classification=value["classification"],
            sanitized_msg=value["sanitized_msg"],
            msg_sha256=value["msg_sha256"],
            detail_type=value["detail_type"],
            safe_detail_projection=value["safe_detail_projection"],
            detail_sha256=value["detail_sha256"],
            request_id_sha256=value["request_id_sha256"],
            raw_transport_sha256=value["raw_transport_sha256"],
            response_body_sha256=value["response_body_sha256"],
            response_byte_count=value["response_byte_count"],
            sanitized_params=value["sanitized_params"],
            requested_fields=requested_fields,
            requested_at=requested_at,
            completed_at=completed_at,
            retry_after_seconds=value["retry_after_seconds"],
        )
    except (KeyError, TypeError, ValueError, data_lane.AlphaFeasibilityDataError) as exc:
        raise P15ContinuationWorkflowError(
            "first_response_safe_semantics_invalid"
        ) from exc


def _publish_first_response(
    *,
    context: continuation.ContinuationContext,
    capture: _FirstTransportCapture,
    clock: Callable[[], datetime],
) -> Mapping[str, Any]:
    task = context.plan.pit_tasks[continuation.SUCCESSFUL_PREFIX_COUNT]
    store = data_lane.CreateOnlyTaskStore(context.child_root)
    try:
        attempts = store._load_attempts(task)
        business_errors = store._load_business_errors(task)
    except data_lane.AlphaFeasibilityDataError as exc:
        raise P15ContinuationWorkflowError(
            "first_response_evidence_unavailable"
        ) from exc
    if len(attempts) < 2:
        raise P15ContinuationWorkflowError("first_response_evidence_unavailable")

    first_business_error = next(
        (
            item
            for item in business_errors
            if item.get("attempt_number") == continuation.PARENT_ATTEMPT_COUNT + 1
        ),
        None,
    )
    if first_business_error is not None:
        evidence = first_business_error.get("evidence")
        if not isinstance(evidence, Mapping):
            raise P15ContinuationWorkflowError(
                "first_response_safe_semantics_invalid"
            )
        safe = _safe_semantics_from_mapping(evidence)
        retry_performed = len(attempts) == continuation.MAXIMUM_CUMULATIVE_ATTEMPTS
        result = (
            "RETRY_SUCCEEDED"
            if retry_performed and store.is_complete(task)
            else "RETRY_FAILED"
            if retry_performed
            else "NOT_RETRYABLE"
        )
    else:
        if capture.first_requested_at is None or capture.first_completed_at is None:
            raise P15ContinuationWorkflowError(
                "first_response_success_evidence_unavailable"
            )
        if (
            len(attempts) == continuation.PARENT_ATTEMPT_COUNT + 1
            and store.is_complete(task)
        ):
            try:
                raw = store.raw_path(task).read_bytes()
                safe = data_lane.extract_safe_response_semantics(
                    raw,
                    task=task,
                    token=None,
                    requested_at=capture.first_requested_at,
                    completed_at=capture.first_completed_at,
                )
            except (OSError, data_lane.AlphaFeasibilityDataError) as exc:
                raise P15ContinuationWorkflowError(
                    "first_response_success_evidence_unavailable"
                ) from exc
            retry_performed = False
            result = "FIRST_ATTEMPT_SUCCEEDED"
        elif (
            len(attempts) == continuation.PARENT_ATTEMPT_COUNT + 1
            and not store.is_complete(task)
            and store.quarantine_path(task).is_file()
        ):
            quarantine = _load_object(
                store.quarantine_path(task),
                "first_response_transport_quarantine_invalid",
            )
            failure_code = quarantine.get("failure_code")
            safe = None
            retry_performed = False
            result = (
                "NO_RESPONSE_TRANSPORT_INTERRUPTION"
                if failure_code in data_lane.RETRYABLE_ATTEMPT_FAILURES
                else "RESPONSE_REJECTED_ADAPTER_PROTOCOL"
                if failure_code in data_lane.ADAPTER_PROTOCOL_FAILURES
                else "RESPONSE_REJECTED_DATA_VALIDATION"
            )
        else:
            raise P15ContinuationWorkflowError(
                "first_response_success_evidence_unavailable"
            )

    published_at = _utc(clock(), "continuation_clock_invalid")
    evidence, _ = continuation.publish_first_continuation_response_evidence(
        context=context,
        safe_response_semantics=safe,
        retry_performed=retry_performed,
        result=result,
        published_at=published_at,
        requested_at=(capture.first_requested_at if safe is None else None),
        completed_at=(capture.first_completed_at if safe is None else None),
    )
    return evidence


def _first_response_summary(evidence: Mapping[str, Any]) -> dict[str, Any]:
    safe = evidence.get("safe_response_semantics")
    if not isinstance(safe, Mapping):
        raise P15ContinuationWorkflowError("first_response_evidence_invalid")
    return {
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


def _continuation_counts(
    context: continuation.ContinuationContext,
) -> tuple[dict[str, int], dict[str, int]]:
    child_counts = data_lane.actual_tushare_request_count_by_endpoint(
        context.child_root,
        plan_sha256=context.plan.plan_sha256,
    )
    continuation_counts: dict[str, int] = {}
    for endpoint in reporting.ALLOWED_ENDPOINTS:
        count = child_counts[endpoint] - _PARENT_ATTEMPT_COUNTS[endpoint]
        if type(count) is not int or count < 0:
            raise P15ContinuationWorkflowError(
                "continuation_request_count_invalid"
            )
        continuation_counts[endpoint] = count
    if continuation_counts["index_weight"] < 1:
        raise P15ContinuationWorkflowError(
            "continuation_first_request_count_missing"
        )
    cumulative = {
        endpoint: context.parent_actual_request_count_by_endpoint[endpoint]
        + continuation_counts[endpoint]
        for endpoint in reporting.ALLOWED_ENDPOINTS
    }
    return continuation_counts, cumulative


def _pit_summary(root: Path) -> tuple[dict[str, Any], str]:
    path = root / continuation.PARENT_PIT_MANIFEST_FILENAME
    manifest = _load_object(path, "continuation_pit_manifest_invalid")
    try:
        expected = manifest["pit_months_expected"]
        observed = manifest["pit_months_observed"]
        missing = list(manifest["missing_months"])
        snapshots = manifest["pit_snapshot_count"]
        union_count = manifest["union_instrument_count"]
        complete = manifest["stage_status"] == "PIT_MEMBERSHIP_READY"
    except (KeyError, TypeError) as exc:
        raise P15ContinuationWorkflowError(
            "continuation_pit_manifest_invalid"
        ) from exc
    if (
        type(expected) is not int
        or expected != reporting.PIT_MONTHS_EXPECTED
        or type(observed) is not int
        or observed < continuation.SUCCESSFUL_PREFIX_COUNT
        or observed > expected
        or type(snapshots) is not int
        or snapshots < 0
        or type(union_count) is not int
        or union_count < 0
        or any(type(month) is not str for month in missing)
    ):
        raise P15ContinuationWorkflowError("continuation_pit_manifest_invalid")
    manifest_sha = manifest.get("manifest_sha256")
    if type(manifest_sha) is not str or re.fullmatch(r"[0-9a-f]{64}", manifest_sha) is None:
        raise P15ContinuationWorkflowError("continuation_pit_manifest_invalid")
    return (
        {
            "months_expected": expected,
            "months_reused": continuation.SUCCESSFUL_PREFIX_COUNT,
            "months_newly_observed": observed - continuation.SUCCESSFUL_PREFIX_COUNT,
            "months_total_observed": observed,
            "missing_months": missing,
            "snapshot_count": snapshots,
            "union_instrument_count": union_count,
            "coverage_status": (
                "COMPLETE" if complete else "BLOCKED_PIT_SOURCE_COVERAGE"
            ),
        },
        manifest_sha,
    )


def _completed_fingerprint_count(root: Path) -> int:
    task_root = root / "tasks"
    if task_root.is_symlink() or not task_root.is_dir():
        raise P15ContinuationWorkflowError("continuation_task_store_invalid")
    paths = tuple(task_root.glob("*.response.json"))
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise P15ContinuationWorkflowError("continuation_task_store_invalid")
    count = len(paths)
    if count < continuation.SUCCESSFUL_PREFIX_COUNT:
        raise P15ContinuationWorkflowError(
            "continuation_completed_fingerprint_count_invalid"
        )
    return count


def _terminal_quarantine(
    context: continuation.ContinuationContext,
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
    root = context.child_root / "quarantine"
    if not root.exists():
        return None, None
    if root.is_symlink() or not root.is_dir():
        raise P15ContinuationWorkflowError("continuation_quarantine_invalid")
    paths = tuple(sorted(root.glob("*.json")))
    if len(paths) > 1:
        raise P15ContinuationWorkflowError("continuation_quarantine_invalid")
    if not paths:
        return None, None
    path = paths[0]
    value = _load_object(path, "continuation_quarantine_invalid")
    task_id = value.get("task_id")
    attempt = value.get("terminal_attempt_number")
    failure = value.get("failure_code")
    classification = value.get("business_error_classification")
    if (
        type(task_id) is not str
        or type(attempt) is not int
        or attempt < 1
        or type(failure) is not str
        or SAFE_CODE.fullmatch(failure) is None
        or (
            classification is not None
            and classification not in data_lane.BUSINESS_ERROR_CLASSIFICATIONS
        )
    ):
        raise P15ContinuationWorkflowError("continuation_quarantine_invalid")
    attempt_path = (
        context.child_root
        / "attempts"
        / task_id
        / f"{attempt:06d}.started.json"
    )
    business_path = (
        context.child_root / "business_errors" / task_id / f"{attempt:06d}.json"
    )
    raw_path = context.child_root / "raw_errors" / task_id / f"{attempt:06d}.json"
    evidence = {
        "task_id": task_id,
        "attempt_number": attempt,
        "failure_code": failure,
        "classification": classification,
        "attempt_artifact_sha256": _file_sha256(
            attempt_path, "continuation_attempt_artifact_invalid"
        ),
        "quarantine_artifact_sha256": _file_sha256(
            path, "continuation_quarantine_invalid"
        ),
        "business_error_artifact_sha256": (
            _file_sha256(business_path, "continuation_business_error_invalid")
            if business_path.is_file() and not business_path.is_symlink()
            else None
        ),
        "raw_error_artifact_sha256": (
            _file_sha256(raw_path, "continuation_raw_error_invalid")
            if raw_path.is_file() and not raw_path.is_symlink()
            else None
        ),
    }
    return value, evidence


def _data_terminal(
    result: Mapping[str, Any],
    quarantine: Mapping[str, Any] | None,
) -> tuple[str, str]:
    stage_status = result.get("stage_status")
    pit_complete = result.get("pit_months_observed") == reporting.PIT_MONTHS_EXPECTED
    terminal_stage = (
        "PIT"
        if not pit_complete
        or (quarantine is not None and quarantine.get("endpoint") == "index_weight")
        else "HISTORY"
    )
    classification = (
        quarantine.get("business_error_classification")
        if quarantine is not None
        else None
    )
    if (
        quarantine is not None
        and quarantine.get("failure_code") in data_lane.RETRYABLE_ATTEMPT_FAILURES
    ):
        return "BLOCKED_DATA", terminal_stage
    if classification == "DATA_UNAVAILABLE":
        return (
            "BLOCKED_PIT_SOURCE_COVERAGE"
            if terminal_stage == "PIT"
            else "BLOCKED_DATA",
            terminal_stage,
        )
    if classification is not None:
        return _CLASSIFICATION_TO_TERMINAL[classification], terminal_stage
    if stage_status == "BLOCKED_ADAPTER_PROTOCOL":
        return "BLOCKED_ADAPTER_PROTOCOL", terminal_stage
    if terminal_stage == "PIT":
        return "BLOCKED_PIT_SOURCE_COVERAGE", terminal_stage
    return "BLOCKED_DATA", terminal_stage


def _safe_blockers(value: Any, fallback: str) -> list[str]:
    supplied = value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else ()
    result = {
        str(item) if SAFE_CODE.fullmatch(str(item)) else "unsafe_blocker_sanitized"
        for item in supplied
    }
    if not result:
        result.add(fallback)
    return sorted(result)


def _report_for_result(
    *,
    context: continuation.ContinuationContext,
    result: Mapping[str, Any],
    cumulative_counts: Mapping[str, int],
) -> tuple[Mapping[str, Any], str, str, list[str]]:
    report_result = dict(result)
    report_result["actual_tushare_request_count_by_endpoint"] = dict(
        cumulative_counts
    )
    if report_result.get("stage_status") == "BLOCKED_PIT_SOURCE_COVERAGE":
        report_result["stage_status"] = "BLOCKED_PIT_MEMBERSHIP"
        report_result["terminal_status"] = "BLOCKED_DATA"
    data_summary = alpha_workflow._reporting_data_summary(report_result)
    commit_sha = alpha_workflow._current_commit_sha(data_lane.REPOSITORY_ROOT)
    generated_at = alpha_workflow._parse_generated_at(result.get("generated_at"))
    if generated_at is None:
        raise P15ContinuationWorkflowError("data_generated_at_missing")

    if result.get("stage_status") in _DATA_BLOCKED_STAGES:
        quarantine, _ = _terminal_quarantine(context)
        terminal_status, terminal_stage = _data_terminal(result, quarantine)
        blockers = _safe_blockers(
            data_summary.get("remaining_blockers"), terminal_status.casefold()
        )
        report = reporting.build_blocked_alpha_feasibility_report(
            commit_sha=commit_sha,
            data_summary=data_summary,
            experiment=context.experiment,
            generated_at=generated_at,
        )
        reporting.publish_alpha_feasibility_report(
            context.child_root, report, experiment=context.experiment
        )
        return report, terminal_status, terminal_stage, blockers

    try:
        loaded = data_lane.load_feasibility_inputs(
            output_root=context.child_root,
            config_path=Path(
                context.claim["current_runtime_implementation_bundle"]["config_path"]
            ),
        )
        inputs = alpha_workflow.build_alpha_input(loaded)
    except (
        P15ContinuationWorkflowError,
        alpha_workflow.AlphaFeasibilityWorkflowError,
        data_lane.AlphaFeasibilityDataError,
        engine.AlphaFeasibilityError,
    ) as exc:
        blocker = getattr(exc, "code", "alpha_input_replay_blocked")
        blocked_summary = alpha_workflow._blocked_after_ready(
            data_summary, str(blocker)
        )
        report = reporting.build_blocked_alpha_feasibility_report(
            commit_sha=commit_sha,
            data_summary=blocked_summary,
            experiment=context.experiment,
            generated_at=generated_at,
        )
        reporting.publish_alpha_feasibility_report(
            context.child_root, report, experiment=context.experiment
        )
        return report, "BLOCKED_DATA", "ALPHA_INPUT", list(
            report["remaining_blockers"]
        )
    try:
        study = engine.run_alpha_feasibility_study(inputs=inputs)
    except engine.AlphaFeasibilityError as exc:
        blocker = getattr(exc, "code", "alpha_engine_blocked")
        blocked_summary = alpha_workflow._blocked_after_ready(
            data_summary, str(blocker)
        )
        report = reporting.build_blocked_alpha_feasibility_report(
            commit_sha=commit_sha,
            data_summary=blocked_summary,
            experiment=context.experiment,
            generated_at=generated_at,
        )
        reporting.publish_alpha_feasibility_report(
            context.child_root, report, experiment=context.experiment
        )
        return report, "BLOCKED_DATA", "ALPHA_ENGINE", list(
            report["remaining_blockers"]
        )
    report = reporting.build_completed_alpha_feasibility_report(
        commit_sha=commit_sha,
        data_summary=data_summary,
        development_metrics=alpha_workflow._study_metrics(study.development),
        validation_metrics=alpha_workflow._study_metrics(study.validation),
        experiment=context.experiment,
        generated_at=generated_at,
    )
    reporting.publish_alpha_feasibility_report(
        context.child_root, report, experiment=context.experiment
    )
    return report, "COMPLETED", "REPORT", []


def _receipt_summary(receipt: Mapping[str, Any], path: Path) -> dict[str, Any]:
    first = receipt["first_continuation_response"]
    pit = receipt["pit"]
    return {
        "status": "completed" if receipt["terminal_status"] == "COMPLETED" else "blocked",
        "continuation_run_id": receipt["continuation_run_id"],
        "parent_run_id": receipt["parent"]["network_run_id"],
        "reused_successful_fingerprint_count": receipt[
            "reused_successful_fingerprint_count"
        ],
        "new_network_attempt_count_by_endpoint": receipt[
            "continuation_actual_request_count_by_endpoint"
        ],
        "resumed_from_month": receipt["resumed_from_month"],
        "minimum_interval_seconds": receipt[
            "minimum_transport_interval_seconds"
        ],
        "first_continuation_response": {
            "business_code": first["business_code"],
            "classified_error": first["classification"],
            "sanitized_msg": first["sanitized_msg"],
            "detail_json_type": first["detail_type"],
            "detail_safe_projection": first["safe_detail_projection"],
            "msg_sha256": first["msg_sha256"],
            "detail_sha256": first["detail_sha256"],
            "request_id_sha256": first["request_id_sha256"],
            "response_body_sha256": first["response_body_sha256"],
            "response_byte_count": first["response_byte_count"],
            "retry_performed": first["retry_performed"],
            "retry_result": first["result"],
        },
        "pit": {
            "months_expected": pit["months_expected"],
            "months_reused": pit["months_reused"],
            "months_newly_observed": pit["months_newly_observed"],
            "months_total_observed": pit["months_total_observed"],
            "missing_months": pit["missing_months"],
            "snapshot_count": pit["snapshot_count"],
            "union_instrument_count": pit["union_instrument_count"],
            "coverage_status": pit["coverage_status"],
        },
        "development_metrics": receipt["development_metrics"],
        "validation_metrics": receipt["validation_metrics"],
        "concentration_metrics": receipt["concentration_metrics"],
        "terminal_status": receipt["terminal_status"],
        "locked_test_status": receipt["locked_test_status"],
        "locked_test_consumed": receipt["locked_test_consumed"],
        "remaining_blockers": receipt["remaining_blockers"],
        "receipt_path": str(path.resolve()),
    }


def run_workflow(
    *,
    parent_root: Path,
    output_root: Path,
    continuation_run_id: str,
    network_process_id: str,
    config_path: Path = reporting.P15_CONFIG_PATH,
    generated_at: str | None = None,
    environ: Mapping[str, str] | None = None,
    transport: data_lane.TushareTransport | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    clock: Callable[[], datetime] = _utc_now,
) -> tuple[int, dict[str, Any]]:
    """Execute preparation, reuse, one network process, report, and receipt."""

    requested_generated_at = alpha_workflow._parse_generated_at(generated_at)
    prepared_at = _utc(clock(), "continuation_clock_invalid")
    context = continuation.prepare_continuation(
        parent_root=parent_root,
        child_root=output_root,
        config_path=config_path,
        continuation_run_id=continuation_run_id,
        prepared_at=prepared_at,
    )
    continuation.stage_parent_reuse(
        context=context,
        staged_at=_utc(clock(), "continuation_clock_invalid"),
    )
    validated_plan = data_lane.validate_parent_reuse_continuation_child(
        config_path,
        output_root,
    )
    if validated_plan.plan_sha256 != context.plan.plan_sha256:
        raise P15ContinuationWorkflowError("continuation_preflight_plan_mismatch")
    environment = os.environ if environ is None else environ
    variable = context.plan.config["source"]["token_environment_variable"]
    token = data_lane.validate_tushare_token_for_process(environment.get(variable))
    active_transport = transport or data_lane.HttpsTushareTransport()
    capture = _FirstTransportCapture(
        transport=active_transport,
        first_task=context.plan.pit_tasks[continuation.SUCCESSFUL_PREFIX_COUNT],
        clock=clock,
    )
    continuation.start_network_process(
        context=context,
        network_process_id=network_process_id,
        started_at=_utc(clock(), "continuation_clock_invalid"),
    )
    try:
        result = data_lane.run_parent_reuse_continuation_backfill(
            config_path=config_path,
            child_root=output_root,
            token=token,  # validated inside the data boundary after preflight
            transport=capture,
            generated_at=requested_generated_at,
            sleeper=sleeper,
            monotonic=monotonic,
            clock=clock,
        )
    except Exception:
        # If the first provider response became durable before a later
        # interruption, seal its already-scanned semantics.  Never invent a
        # response sidecar for transport ambiguity or pre-response failure.
        try:
            _publish_first_response(context=context, capture=capture, clock=clock)
        except Exception:
            pass
        raise

    first_evidence = _publish_first_response(
        context=context, capture=capture, clock=clock
    )
    continuation_counts, cumulative_counts = _continuation_counts(context)
    pit, pit_file_sha = _pit_summary(context.child_root)
    report, terminal_status, terminal_stage, blockers = _report_for_result(
        context=context,
        result=result,
        cumulative_counts=cumulative_counts,
    )
    quarantine, failure_evidence = _terminal_quarantine(context)
    if terminal_status == "BLOCKED_PIT_SOURCE_COVERAGE" and (
        quarantine is None
        or quarantine.get("business_error_classification") != "DATA_UNAVAILABLE"
    ):
        failure_evidence = None
    history_path = context.child_root / "history_manifest.json"
    history_file_sha = None
    if history_path.is_file() and not history_path.is_symlink():
        history_manifest = _load_object(
            history_path, "continuation_history_manifest_invalid"
        )
        declared_history_sha = history_manifest.get("manifest_sha256")
        if (
            type(declared_history_sha) is not str
            or re.fullmatch(r"[0-9a-f]{64}", declared_history_sha) is None
        ):
            raise P15ContinuationWorkflowError(
                "continuation_history_manifest_invalid"
            )
        history_file_sha = declared_history_sha
    receipt, receipt_path = continuation.publish_continuation_receipt(
        context=context,
        terminal_stage=terminal_stage,
        terminal_status=terminal_status,
        generated_at=_utc(clock(), "continuation_clock_invalid"),
        continuation_actual_request_count_by_endpoint=continuation_counts,
        completed_request_fingerprint_count=_completed_fingerprint_count(
            context.child_root
        ),
        remaining_blockers=blockers,
        first_continuation_response=_first_response_summary(first_evidence),
        pit=pit,
        terminal_failure_evidence=failure_evidence,
        child_report=report,
        child_pit_manifest_sha256=pit_file_sha,
        child_history_manifest_sha256=history_file_sha,
    )
    return (
        0 if terminal_status == "COMPLETED" else 1,
        _receipt_summary(receipt, receipt_path),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--parent-root", type=Path, default=DEFAULT_PARENT_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--config", type=Path, default=reporting.P15_CONFIG_PATH)
    parser.add_argument("--continuation-run-id", required=True)
    parser.add_argument("--network-process-id", required=True)
    parser.add_argument("--generated-at")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        exit_code, summary = run_workflow(
            parent_root=args.parent_root,
            output_root=args.output_root,
            config_path=args.config,
            continuation_run_id=args.continuation_run_id,
            network_process_id=args.network_process_id,
            generated_at=args.generated_at,
        )
    except (
        P15ContinuationWorkflowError,
        continuation.P15ContinuationError,
        alpha_workflow.AlphaFeasibilityWorkflowError,
        data_lane.AlphaFeasibilityDataError,
        engine.AlphaFeasibilityError,
        reporting.AlphaFeasibilityReportingError,
        OSError,
        ValueError,
    ) as exc:
        code = getattr(exc, "code", "continuation_workflow_failed")
        safe_code = code if SAFE_CODE.fullmatch(str(code)) else "unsafe_error_sanitized"
        sys.stderr.write(
            json.dumps(
                {
                    "status": "failed",
                    "reason": "p15_continuation_workflow_failed",
                    "error_code": safe_code,
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
    "DEFAULT_OUTPUT_ROOT",
    "DEFAULT_PARENT_ROOT",
    "P15ContinuationWorkflowError",
    "build_parser",
    "main",
    "run_workflow",
]
