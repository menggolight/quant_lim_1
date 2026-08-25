"""Offline postmortem contract for a failed Tushare diagnostic runner.

This module reconstructs only runner-integrity evidence already present in a
create-only budget slot and in safe local metadata.  It cannot observe either
diagnostic channel, infer an outbound request count, read credentials, access
the network, or grant any market-data or execution authority.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from research.market_data.tushare_capability import canonical_sha256, strict_json_loads
from research.market_data.validation import validate_json_schema


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
POSTMORTEM_SCHEMA = (
    REPOSITORY_ROOT
    / "schemas"
    / "tushare_single_endpoint_diagnostic_postmortem.v3.json"
)
POSTMORTEM_SCHEMA_VERSION = "tushare-single-endpoint-diagnostic-postmortem.v3"
BUDGET_SLOT_SCHEMA_VERSION = "tushare-diagnostic-round-budget-slot-v1"
FAILURE_MARKER_SCHEMA_VERSION = "tushare-diagnostic-round-failure-v1"
POSTMORTEM_SCOPE = "diagnostic_runner_integrity_only"
POSTMORTEM_STATUS = "runner_failed_sealed"
FAILURE_WINDOW = "after_budget_reservation_before_receipt_publish"
CONCLUSION = "capability_probe_bug"
CHANNELS = ("sdk", "http")

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SLOT_FIELDS = {
    "schema_version",
    "slot",
    "endpoint",
    "diagnostic_run_id",
    "reserved_at",
    "reserved_request_count",
    "maximum_round_request_count",
}
_FAILURE_MARKER_FIELDS = {
    "schema_version",
    "round_status",
    "evidence_origin",
    "diagnostic_run_id",
    "endpoint",
    "recorded_at",
    "runner_exception_type",
    "failure_window",
    "budget_slot_sha256",
    "failed_diagnostic_code_sha256",
    "maximum_round_request_count",
    "rerun_permitted",
}


class TushareDiagnosticPostmortemError(RuntimeError):
    """Raised when runner-integrity evidence is incomplete or inconsistent."""


def _aware_iso(value: datetime, label: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TushareDiagnosticPostmortemError(
            f"{label} must be a timezone-aware datetime"
        )
    return value.isoformat()


def _parse_aware(value: Any, label: str) -> datetime:
    if type(value) is not str:
        raise TushareDiagnosticPostmortemError(f"{label} must be a date-time string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TushareDiagnosticPostmortemError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise TushareDiagnosticPostmortemError(f"{label} lacks a UTC offset")
    return parsed


def _sha256(value: Any, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise TushareDiagnosticPostmortemError(
            f"{label} must be a lowercase SHA-256"
        )
    return value


def _validated_budget_slot(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _SLOT_FIELDS:
        raise TushareDiagnosticPostmortemError(
            "budget slot fields differ from the frozen contract"
        )
    slot = dict(value)
    if slot.get("schema_version") != BUDGET_SLOT_SCHEMA_VERSION:
        raise TushareDiagnosticPostmortemError("budget slot schema is invalid")
    if type(slot.get("slot")) is not int or slot["slot"] not in {1, 2}:
        raise TushareDiagnosticPostmortemError("budget slot number is invalid")
    expected_endpoint = "trade_cal" if slot["slot"] == 1 else "daily"
    if slot.get("endpoint") != expected_endpoint:
        raise TushareDiagnosticPostmortemError(
            "budget slot endpoint differs from its fixed sequence"
        )
    if (
        type(slot.get("diagnostic_run_id")) is not str
        or _IDENTIFIER.fullmatch(slot["diagnostic_run_id"]) is None
    ):
        raise TushareDiagnosticPostmortemError("diagnostic run id is invalid")
    _parse_aware(slot.get("reserved_at"), "budget_slot.reserved_at")
    if slot.get("reserved_request_count") != 2:
        raise TushareDiagnosticPostmortemError(
            "reserved request count differs from the fixed two-channel slot"
        )
    if slot.get("maximum_round_request_count") != 4:
        raise TushareDiagnosticPostmortemError(
            "maximum round request count differs from the frozen budget"
        )
    return slot


def _validated_failure_marker(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _FAILURE_MARKER_FIELDS:
        raise TushareDiagnosticPostmortemError(
            "failure marker fields differ from the frozen contract"
        )
    marker = dict(value)
    if marker.get("schema_version") != FAILURE_MARKER_SCHEMA_VERSION:
        raise TushareDiagnosticPostmortemError("failure marker schema is invalid")
    if marker.get("round_status") != "closed_after_runner_failure":
        raise TushareDiagnosticPostmortemError("failure marker status is invalid")
    if marker.get("evidence_origin") not in {
        "runner_exception_boundary",
        "posthoc_observed_cli_failure",
    }:
        raise TushareDiagnosticPostmortemError("failure marker origin is invalid")
    if (
        type(marker.get("diagnostic_run_id")) is not str
        or _IDENTIFIER.fullmatch(marker["diagnostic_run_id"]) is None
    ):
        raise TushareDiagnosticPostmortemError("failure marker run id is invalid")
    if marker.get("endpoint") not in {"trade_cal", "daily"}:
        raise TushareDiagnosticPostmortemError("failure marker endpoint is invalid")
    _parse_aware(marker.get("recorded_at"), "failure_marker.recorded_at")
    if marker.get("runner_exception_type") != "OtherError":
        raise TushareDiagnosticPostmortemError(
            "failure marker exception type is invalid"
        )
    if marker.get("failure_window") != FAILURE_WINDOW:
        raise TushareDiagnosticPostmortemError("failure marker window is invalid")
    _sha256(marker.get("budget_slot_sha256"), "failure_marker.budget_slot_sha256")
    _sha256(
        marker.get("failed_diagnostic_code_sha256"),
        "failure_marker.failed_diagnostic_code_sha256",
    )
    if marker.get("maximum_round_request_count") != 4:
        raise TushareDiagnosticPostmortemError(
            "failure marker maximum request count is invalid"
        )
    if marker.get("rerun_permitted") is not False:
        raise TushareDiagnosticPostmortemError(
            "failure marker must keep the round closed"
        )
    return marker


@dataclass(frozen=True, slots=True)
class UnavailableChannelEvidenceV1:
    """Explicit absence of channel evidence; null is not an observed result."""

    channel: str
    evidence_status: str = "unavailable"
    request_count: None = None
    transport_status: None = None
    http_status: None = None
    upstream_code: None = None
    sdk_exception_type: None = None
    sanitized_message_category: None = None

    def __post_init__(self) -> None:
        if self.channel not in CHANNELS:
            raise TushareDiagnosticPostmortemError("postmortem channel is invalid")
        if self.evidence_status != "unavailable" or any(
            value is not None
            for value in (
                self.request_count,
                self.transport_status,
                self.http_status,
                self.upstream_code,
                self.sdk_exception_type,
                self.sanitized_message_category,
            )
        ):
            raise TushareDiagnosticPostmortemError(
                "unavailable channel evidence must remain null"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "evidence_status": self.evidence_status,
            "request_count": self.request_count,
            "transport_status": self.transport_status,
            "http_status": self.http_status,
            "upstream_code": self.upstream_code,
            "sdk_exception_type": self.sdk_exception_type,
            "sanitized_message_category": self.sanitized_message_category,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "UnavailableChannelEvidenceV1":
        expected = set(cls(channel="sdk").to_dict())
        if not isinstance(value, Mapping) or set(value) != expected:
            raise TushareDiagnosticPostmortemError(
                "postmortem channel fields differ from the contract"
            )
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class SingleEndpointDiagnosticPostmortemV3:
    diagnostic_run_id: str
    recorded_at: datetime
    endpoint: str
    semantic_parameters: None
    budget_slot: Mapping[str, Any]
    budget_slot_sha256: str
    failure_marker: Mapping[str, Any]
    failure_marker_sha256: str
    failed_diagnostic_code_sha256: str
    runner_exception_type: str
    git_commit: str
    git_worktree_status: str
    actual_request_count: None
    actual_request_count_lower_bound: int
    actual_request_count_upper_bound: int
    channels: tuple[UnavailableChannelEvidenceV1, ...]
    receipt_sha256: str

    @property
    def conclusion(self) -> str:
        return CONCLUSION

    @property
    def rerun_permitted(self) -> bool:
        return False

    @property
    def live_supported(self) -> bool:
        return False

    def __post_init__(self) -> None:
        slot = _validated_budget_slot(self.budget_slot)
        if self.diagnostic_run_id != slot["diagnostic_run_id"]:
            raise TushareDiagnosticPostmortemError(
                "postmortem run id differs from the budget slot"
            )
        if self.endpoint != slot["endpoint"]:
            raise TushareDiagnosticPostmortemError(
                "postmortem endpoint differs from the budget slot"
            )
        recorded_at = _parse_aware(
            _aware_iso(self.recorded_at, "recorded_at"), "recorded_at"
        )
        reserved_at = _parse_aware(slot["reserved_at"], "budget_slot.reserved_at")
        if recorded_at < reserved_at:
            raise TushareDiagnosticPostmortemError(
                "postmortem cannot precede budget reservation"
            )
        if self.semantic_parameters is not None:
            raise TushareDiagnosticPostmortemError(
                "failed runner semantic parameters must remain unavailable"
            )
        object.__setattr__(self, "budget_slot", MappingProxyType(slot))
        declared_slot_sha256 = _sha256(
            self.budget_slot_sha256, "budget_slot_sha256"
        )
        if canonical_sha256(slot) != declared_slot_sha256:
            raise TushareDiagnosticPostmortemError("budget slot hash mismatch")
        marker = _validated_failure_marker(self.failure_marker)
        object.__setattr__(self, "failure_marker", MappingProxyType(marker))
        declared_marker_sha256 = _sha256(
            self.failure_marker_sha256, "failure_marker_sha256"
        )
        if canonical_sha256(marker) != declared_marker_sha256:
            raise TushareDiagnosticPostmortemError("failure marker hash mismatch")
        marker_at = _parse_aware(
            marker["recorded_at"], "failure_marker.recorded_at"
        )
        if marker_at < reserved_at or recorded_at < marker_at:
            raise TushareDiagnosticPostmortemError(
                "failure marker timing differs from the sealed sequence"
            )
        if (
            marker["diagnostic_run_id"] != self.diagnostic_run_id
            or marker["endpoint"] != self.endpoint
            or marker["budget_slot_sha256"] != declared_slot_sha256
            or marker["failed_diagnostic_code_sha256"]
            != self.failed_diagnostic_code_sha256
            or marker["runner_exception_type"] != self.runner_exception_type
        ):
            raise TushareDiagnosticPostmortemError(
                "failure marker differs from the sealed postmortem"
            )
        _sha256(
            self.failed_diagnostic_code_sha256,
            "failed_diagnostic_code_sha256",
        )
        if self.runner_exception_type != "OtherError":
            raise TushareDiagnosticPostmortemError(
                "runner exception type must retain the observed safe category"
            )
        if self.git_commit != "unknown" and (
            type(self.git_commit) is not str
            or _GIT_COMMIT.fullmatch(self.git_commit) is None
        ):
            raise TushareDiagnosticPostmortemError("git commit is invalid")
        if self.git_worktree_status not in {"clean", "dirty", "unknown"}:
            raise TushareDiagnosticPostmortemError("git worktree status is invalid")
        if self.actual_request_count is not None:
            raise TushareDiagnosticPostmortemError(
                "actual request count must remain unknown"
            )
        reserved = slot["reserved_request_count"]
        if (
            type(self.actual_request_count_lower_bound) is not int
            or self.actual_request_count_lower_bound != 0
            or type(self.actual_request_count_upper_bound) is not int
            or self.actual_request_count_upper_bound != reserved
            or not 0 <= self.actual_request_count_upper_bound <= 2
        ):
            raise TushareDiagnosticPostmortemError(
                "actual request bounds must be zero through the reserved count"
            )
        if (
            type(self.channels) is not tuple
            or tuple(item.channel for item in self.channels) != CHANNELS
            or any(type(item) is not UnavailableChannelEvidenceV1 for item in self.channels)
        ):
            raise TushareDiagnosticPostmortemError(
                "postmortem requires ordered unavailable sdk,http evidence"
            )
        _sha256(self.receipt_sha256, "receipt_sha256")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": POSTMORTEM_SCHEMA_VERSION,
            "status": POSTMORTEM_STATUS,
            "scope": POSTMORTEM_SCOPE,
            "recorded_at": _aware_iso(self.recorded_at, "recorded_at"),
            "provider_id": "tushare",
            "diagnostic_run_id": self.diagnostic_run_id,
            "endpoint": self.endpoint,
            "semantic_parameters": None,
            "semantic_parameters_evidence_status": "unavailable",
            "budget_slot": dict(self.budget_slot),
            "budget_slot_sha256": self.budget_slot_sha256,
            "failure_marker": dict(self.failure_marker),
            "failure_marker_sha256": self.failure_marker_sha256,
            "failed_diagnostic_code_sha256": self.failed_diagnostic_code_sha256,
            "runner_exception_type": self.runner_exception_type,
            "failure_window": FAILURE_WINDOW,
            "original_receipt_present": False,
            "original_receipt_scope": "fixed_round_root",
            "git_commit": self.git_commit,
            "git_worktree_status": self.git_worktree_status,
            "actual_request_count": self.actual_request_count,
            "actual_request_count_lower_bound": self.actual_request_count_lower_bound,
            "actual_request_count_upper_bound": self.actual_request_count_upper_bound,
            "channels": [item.to_dict() for item in self.channels],
            "conclusion": CONCLUSION,
            "tushare_capability_judgment": "not_made",
            "rerun_permitted": False,
            "formal_data_admission": False,
            "experiment_v3_impact": "none",
            "daily_signal_authority": "none",
            "next_session_allowed": False,
            "paper_eligibility": False,
            "trade_eligibility": False,
            "real_money_list_allowed": False,
            "automatic_order_submission": False,
            "live_supported": False,
            "receipt_sha256": self.receipt_sha256,
        }


def build_diagnostic_postmortem_receipt(
    *,
    budget_slot: Mapping[str, Any],
    budget_slot_sha256: str,
    failure_marker: Mapping[str, Any],
    failure_marker_sha256: str,
    recorded_at: datetime,
    git_commit: str,
    git_worktree_status: str,
) -> SingleEndpointDiagnosticPostmortemV3:
    """Build a self-hashed postmortem without filesystem or network access."""

    slot = _validated_budget_slot(budget_slot)
    marker = _validated_failure_marker(failure_marker)
    channels = tuple(UnavailableChannelEvidenceV1(channel=item) for item in CHANNELS)
    unsigned = {
        "schema_version": POSTMORTEM_SCHEMA_VERSION,
        "status": POSTMORTEM_STATUS,
        "scope": POSTMORTEM_SCOPE,
        "recorded_at": _aware_iso(recorded_at, "recorded_at"),
        "provider_id": "tushare",
        "diagnostic_run_id": slot["diagnostic_run_id"],
        "endpoint": slot["endpoint"],
        "semantic_parameters": None,
        "semantic_parameters_evidence_status": "unavailable",
        "budget_slot": slot,
        "budget_slot_sha256": budget_slot_sha256,
        "failure_marker": marker,
        "failure_marker_sha256": failure_marker_sha256,
        "failed_diagnostic_code_sha256": marker["failed_diagnostic_code_sha256"],
        "runner_exception_type": marker["runner_exception_type"],
        "failure_window": FAILURE_WINDOW,
        "original_receipt_present": False,
        "original_receipt_scope": "fixed_round_root",
        "git_commit": git_commit,
        "git_worktree_status": git_worktree_status,
        "actual_request_count": None,
        "actual_request_count_lower_bound": 0,
        "actual_request_count_upper_bound": slot["reserved_request_count"],
        "channels": [item.to_dict() for item in channels],
        "conclusion": CONCLUSION,
        "tushare_capability_judgment": "not_made",
        "rerun_permitted": False,
        "formal_data_admission": False,
        "experiment_v3_impact": "none",
        "daily_signal_authority": "none",
        "next_session_allowed": False,
        "paper_eligibility": False,
        "trade_eligibility": False,
        "real_money_list_allowed": False,
        "automatic_order_submission": False,
        "live_supported": False,
    }
    receipt = SingleEndpointDiagnosticPostmortemV3(
        diagnostic_run_id=slot["diagnostic_run_id"],
        recorded_at=recorded_at,
        endpoint=slot["endpoint"],
        semantic_parameters=None,
        budget_slot=slot,
        budget_slot_sha256=budget_slot_sha256,
        failure_marker=marker,
        failure_marker_sha256=failure_marker_sha256,
        failed_diagnostic_code_sha256=marker["failed_diagnostic_code_sha256"],
        runner_exception_type=marker["runner_exception_type"],
        git_commit=git_commit,
        git_worktree_status=git_worktree_status,
        actual_request_count=None,
        actual_request_count_lower_bound=0,
        actual_request_count_upper_bound=slot["reserved_request_count"],
        channels=channels,
        receipt_sha256=canonical_sha256(unsigned),
    )
    validate_json_schema(receipt.to_dict(), POSTMORTEM_SCHEMA)
    return receipt


def verify_diagnostic_postmortem_receipt(
    content: bytes | str,
) -> SingleEndpointDiagnosticPostmortemV3:
    """Verify canonical encoding, Schema, domain invariants and self-hash."""

    raw = content.encode("utf-8") if isinstance(content, str) else content
    value = strict_json_loads(
        raw,
        label="Tushare diagnostic postmortem receipt",
        require_canonical=True,
    )
    if not isinstance(value, dict):
        raise TushareDiagnosticPostmortemError("postmortem receipt root must be an object")
    validate_json_schema(value, POSTMORTEM_SCHEMA)
    receipt = SingleEndpointDiagnosticPostmortemV3(
        diagnostic_run_id=value["diagnostic_run_id"],
        recorded_at=_parse_aware(value["recorded_at"], "recorded_at"),
        endpoint=value["endpoint"],
        semantic_parameters=value["semantic_parameters"],
        budget_slot=dict(value["budget_slot"]),
        budget_slot_sha256=value["budget_slot_sha256"],
        failure_marker=dict(value["failure_marker"]),
        failure_marker_sha256=value["failure_marker_sha256"],
        failed_diagnostic_code_sha256=value["failed_diagnostic_code_sha256"],
        runner_exception_type=value["runner_exception_type"],
        git_commit=value["git_commit"],
        git_worktree_status=value["git_worktree_status"],
        actual_request_count=value["actual_request_count"],
        actual_request_count_lower_bound=value["actual_request_count_lower_bound"],
        actual_request_count_upper_bound=value["actual_request_count_upper_bound"],
        channels=tuple(
            UnavailableChannelEvidenceV1.from_dict(item) for item in value["channels"]
        ),
        receipt_sha256=value["receipt_sha256"],
    )
    unsigned = dict(receipt.to_dict())
    declared = unsigned.pop("receipt_sha256")
    if canonical_sha256(unsigned) != declared:
        raise TushareDiagnosticPostmortemError("postmortem receipt hash mismatch")
    return receipt


__all__ = [
    "POSTMORTEM_SCHEMA_VERSION",
    "SingleEndpointDiagnosticPostmortemV3",
    "TushareDiagnosticPostmortemError",
    "UnavailableChannelEvidenceV1",
    "build_diagnostic_postmortem_receipt",
    "verify_diagnostic_postmortem_receipt",
]
