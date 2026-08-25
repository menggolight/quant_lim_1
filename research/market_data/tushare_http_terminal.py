"""Pure domain contract for one crash-replayable Tushare HTTP diagnostic.

The event chain is designed for create-only persistence by an outer runner.
This module performs no filesystem writes, environment access, SDK import, or
network call.  Persisted semantics never include credentials, credential
derivatives, raw response bodies, upstream messages, or exception text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from research.market_data.tushare_capability import (
    TushareCapabilityError,
    canonical_sha256,
    normalize_parameters,
    strict_json_loads,
)
from research.market_data.validation import validate_json_schema


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EVENT_SCHEMA = REPOSITORY_ROOT / "schemas" / "tushare_http_diagnostic_event.v1.json"
RECEIPT_SCHEMA = (
    REPOSITORY_ROOT
    / "schemas"
    / "tushare_http_terminal_diagnostic_receipt.v1.json"
)
EVENT_SCHEMA_VERSION = "tushare-http-diagnostic-event.v1"
RECEIPT_SCHEMA_VERSION = "tushare-http-terminal-diagnostic-receipt.v1"
OFFICIAL_HTTPS_API_URL = "https://api.tushare.pro"
EVENT_TYPES = (
    "RUN_CREATED",
    "REQUEST_RESERVED",
    "NETWORK_CALL_STARTED",
    "RESPONSE_RECEIVED",
    "TERMINAL",
)
TERMINAL_REASONS = {
    "stopped_before_reservation",
    "stopped_before_network",
    "remote_execution_unknown",
    "response_received",
}
MESSAGE_CATEGORIES = {
    "success",
    "permission",
    "rate_limit",
    "authentication_account",
    "invalid_parameter",
    "server_internal",
    "unknown",
}

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class TushareHttpTerminalError(RuntimeError):
    """Raised when HTTP journal or terminal receipt evidence is inconsistent."""


def _aware_iso(value: datetime, label: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TushareHttpTerminalError(f"{label} must be timezone-aware")
    return value.isoformat()


def _parse_aware(value: Any, label: str) -> datetime:
    if type(value) is not str:
        raise TushareHttpTerminalError(f"{label} must be a date-time string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TushareHttpTerminalError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise TushareHttpTerminalError(f"{label} lacks a UTC offset")
    return parsed


def _sha256(value: Any, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise TushareHttpTerminalError(f"{label} must be a lowercase SHA-256")
    return value


def _identifier(value: Any, label: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise TushareHttpTerminalError(f"{label} is invalid")
    return value


def _normalized_runtime_parameters(value: Mapping[str, Any]) -> Mapping[str, str]:
    try:
        normalized = normalize_parameters("trade_cal", value)
    except TushareCapabilityError as exc:
        raise TushareHttpTerminalError("runtime parameters are unsafe") from exc
    if not normalized or dict(normalized) != dict(value):
        raise TushareHttpTerminalError("runtime parameters are not canonical")
    return MappingProxyType(dict(normalized))


def runtime_semantics_sha256(parameters: Mapping[str, Any]) -> str:
    """Hash credential-free request semantics, never the actual wire payload."""

    normalized = _normalized_runtime_parameters(parameters)
    return canonical_sha256(
        {
            "endpoint": "trade_cal",
            "transport_channel": "http",
            "transport_target": OFFICIAL_HTTPS_API_URL,
            "runtime_semantic_parameters": dict(normalized),
            "fields": "",
        }
    )


@dataclass(frozen=True, slots=True)
class HttpTerminalCountsV1:
    reserved_request_count: int
    network_call_started_count: int
    response_received_count: int
    terminal_result_count: int
    remote_execution_unknown_count: int
    budget_consumed_count: int

    def __post_init__(self) -> None:
        values = (
            self.reserved_request_count,
            self.network_call_started_count,
            self.response_received_count,
            self.terminal_result_count,
            self.remote_execution_unknown_count,
            self.budget_consumed_count,
        )
        if any(type(value) is not int or value not in {0, 1} for value in values):
            raise TushareHttpTerminalError("terminal counts must be exact zero-or-one integers")
        if self.terminal_result_count != 1:
            raise TushareHttpTerminalError("terminal_result_count must equal one")
        if not (
            self.response_received_count
            <= self.network_call_started_count
            <= self.reserved_request_count
        ):
            raise TushareHttpTerminalError("HTTP lifecycle counts are not monotonic")
        if self.remote_execution_unknown_count != (
            self.network_call_started_count - self.response_received_count
        ):
            raise TushareHttpTerminalError(
                "remote_execution_unknown_count must equal started minus response"
            )
        if self.budget_consumed_count != self.reserved_request_count:
            raise TushareHttpTerminalError(
                "budget_consumed_count must equal reserved_request_count"
            )

    def to_dict(self) -> dict[str, int]:
        return {
            "reserved_request_count": self.reserved_request_count,
            "network_call_started_count": self.network_call_started_count,
            "response_received_count": self.response_received_count,
            "terminal_result_count": self.terminal_result_count,
            "remote_execution_unknown_count": self.remote_execution_unknown_count,
            "budget_consumed_count": self.budget_consumed_count,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HttpTerminalCountsV1":
        expected = set(
            cls(
                reserved_request_count=0,
                network_call_started_count=0,
                response_received_count=0,
                terminal_result_count=1,
                remote_execution_unknown_count=0,
                budget_consumed_count=0,
            ).to_dict()
        )
        if not isinstance(value, Mapping) or set(value) != expected:
            raise TushareHttpTerminalError("terminal count fields differ from contract")
        return cls(**dict(value))


def _event_payload(
    *,
    diagnostic_run_id: str,
    request_id: str,
    sequence: int,
    event_type: str,
    recorded_at: datetime,
    previous_event_sha256: str | None,
    runtime_semantics_hash: str,
    diagnostic_code_sha256: str,
    git_commit: str,
    git_worktree_status: str,
    expected_fields: Sequence[str],
    runtime_semantic_parameters: Mapping[str, str] | None,
    transport_status: str | None,
    http_status: int | None,
    upstream_code: int | None,
    sanitized_message_category: str | None,
    terminal_reason: str | None,
    counts: HttpTerminalCountsV1 | None,
) -> dict[str, Any]:
    return {
        "schema_version": EVENT_SCHEMA_VERSION,
        "diagnostic_run_id": diagnostic_run_id,
        "request_id": request_id,
        "sequence": sequence,
        "event_type": event_type,
        "recorded_at": _aware_iso(recorded_at, "event.recorded_at"),
        "previous_event_sha256": previous_event_sha256,
        "runtime_semantics_sha256": runtime_semantics_hash,
        "diagnostic_code_sha256": diagnostic_code_sha256,
        "git_commit": git_commit,
        "git_worktree_status": git_worktree_status,
        "expected_fields": list(expected_fields),
        "provider_id": "tushare",
        "endpoint": "trade_cal",
        "transport_channel": "http",
        "transport_target": OFFICIAL_HTTPS_API_URL,
        "max_requests": 1,
        "sdk_ran": False,
        "automatic_retries_allowed": False,
        "runtime_semantic_parameters": (
            dict(runtime_semantic_parameters)
            if runtime_semantic_parameters is not None
            else None
        ),
        "transport_status": transport_status,
        "http_status": http_status,
        "upstream_code": upstream_code,
        "sanitized_message_category": sanitized_message_category,
        "terminal_reason": terminal_reason,
        "counts": counts.to_dict() if counts is not None else None,
    }


@dataclass(frozen=True, slots=True)
class TushareHttpDiagnosticEventV1:
    diagnostic_run_id: str
    request_id: str
    sequence: int
    event_type: str
    recorded_at: datetime
    previous_event_sha256: str | None
    runtime_semantics_sha256: str
    diagnostic_code_sha256: str
    git_commit: str
    git_worktree_status: str
    expected_fields: tuple[str, ...]
    runtime_semantic_parameters: Mapping[str, str] | None
    transport_status: str | None
    http_status: int | None
    upstream_code: int | None
    sanitized_message_category: str | None
    terminal_reason: str | None
    counts: HttpTerminalCountsV1 | None
    event_sha256: str

    def __post_init__(self) -> None:
        _identifier(self.diagnostic_run_id, "diagnostic_run_id")
        _identifier(self.request_id, "request_id")
        if type(self.sequence) is not int or not 1 <= self.sequence <= 5:
            raise TushareHttpTerminalError("event sequence is invalid")
        if self.event_type not in EVENT_TYPES:
            raise TushareHttpTerminalError("event type is invalid")
        _aware_iso(self.recorded_at, "event.recorded_at")
        if self.previous_event_sha256 is not None:
            _sha256(self.previous_event_sha256, "previous_event_sha256")
        _sha256(self.runtime_semantics_sha256, "runtime_semantics_sha256")
        _sha256(self.diagnostic_code_sha256, "diagnostic_code_sha256")
        if self.git_commit != "unknown" and (
            type(self.git_commit) is not str
            or _GIT_COMMIT.fullmatch(self.git_commit) is None
        ):
            raise TushareHttpTerminalError("git_commit is invalid")
        if self.git_worktree_status not in {"clean", "dirty", "unknown"}:
            raise TushareHttpTerminalError("git_worktree_status is invalid")
        fields = tuple(self.expected_fields)
        if (
            not fields
            or len(fields) > 64
            or len(set(fields)) != len(fields)
            or any(type(field) is not str or _FIELD.fullmatch(field) is None for field in fields)
        ):
            raise TushareHttpTerminalError("expected_fields are invalid")
        object.__setattr__(self, "expected_fields", fields)
        if self.upstream_code is not None and (
            type(self.upstream_code) is not int
            or not -(2**31) <= self.upstream_code <= 2**31 - 1
        ):
            raise TushareHttpTerminalError("upstream_code is invalid")

        response_values = (
            self.transport_status,
            self.http_status,
            self.upstream_code,
            self.sanitized_message_category,
        )
        if self.event_type == "RUN_CREATED":
            if self.sequence != 1 or self.previous_event_sha256 is not None:
                raise TushareHttpTerminalError("RUN_CREATED must be the first event")
            if self.runtime_semantic_parameters is None:
                raise TushareHttpTerminalError("RUN_CREATED requires runtime parameters")
            normalized = _normalized_runtime_parameters(self.runtime_semantic_parameters)
            object.__setattr__(self, "runtime_semantic_parameters", normalized)
            if runtime_semantics_sha256(normalized) != self.runtime_semantics_sha256:
                raise TushareHttpTerminalError("runtime semantics hash mismatch")
            if any(value is not None for value in response_values):
                raise TushareHttpTerminalError("RUN_CREATED cannot contain response evidence")
            if self.terminal_reason is not None or self.counts is not None:
                raise TushareHttpTerminalError("RUN_CREATED cannot contain terminal evidence")
        else:
            if self.previous_event_sha256 is None:
                raise TushareHttpTerminalError("non-initial event requires previous hash")
            if self.runtime_semantic_parameters is not None:
                raise TushareHttpTerminalError(
                    "runtime parameters may appear only in RUN_CREATED"
                )
            if self.event_type == "RESPONSE_RECEIVED":
                if (
                    self.transport_status != "response_received"
                    or type(self.http_status) is not int
                    or not 100 <= self.http_status <= 599
                    or self.sanitized_message_category not in MESSAGE_CATEGORIES
                ):
                    raise TushareHttpTerminalError("response evidence is invalid")
                if self.upstream_code == 0 and self.sanitized_message_category != "success":
                    raise TushareHttpTerminalError(
                        "successful upstream code requires success classification"
                    )
                if self.upstream_code == 2002 and self.sanitized_message_category != "permission":
                    raise TushareHttpTerminalError(
                        "permission upstream code requires permission classification"
                    )
                if self.terminal_reason is not None or self.counts is not None:
                    raise TushareHttpTerminalError(
                        "RESPONSE_RECEIVED cannot contain terminal evidence"
                    )
            elif self.event_type == "TERMINAL":
                if any(value is not None for value in response_values):
                    raise TushareHttpTerminalError(
                        "TERMINAL cannot manufacture response evidence"
                    )
                if self.terminal_reason not in TERMINAL_REASONS or self.counts is None:
                    raise TushareHttpTerminalError("terminal evidence is incomplete")
            elif any(value is not None for value in response_values) or (
                self.terminal_reason is not None or self.counts is not None
            ):
                raise TushareHttpTerminalError(
                    "lifecycle event contains evidence from another state"
                )

        _sha256(self.event_sha256, "event_sha256")
        if canonical_sha256(self.unsigned_dict()) != self.event_sha256:
            raise TushareHttpTerminalError("event hash mismatch")

    def unsigned_dict(self) -> dict[str, Any]:
        return _event_payload(
            diagnostic_run_id=self.diagnostic_run_id,
            request_id=self.request_id,
            sequence=self.sequence,
            event_type=self.event_type,
            recorded_at=self.recorded_at,
            previous_event_sha256=self.previous_event_sha256,
            runtime_semantics_hash=self.runtime_semantics_sha256,
            diagnostic_code_sha256=self.diagnostic_code_sha256,
            git_commit=self.git_commit,
            git_worktree_status=self.git_worktree_status,
            expected_fields=self.expected_fields,
            runtime_semantic_parameters=self.runtime_semantic_parameters,
            transport_status=self.transport_status,
            http_status=self.http_status,
            upstream_code=self.upstream_code,
            sanitized_message_category=self.sanitized_message_category,
            terminal_reason=self.terminal_reason,
            counts=self.counts,
        )

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "event_sha256": self.event_sha256}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TushareHttpDiagnosticEventV1":
        counts_value = value.get("counts")
        return cls(
            diagnostic_run_id=value["diagnostic_run_id"],
            request_id=value["request_id"],
            sequence=value["sequence"],
            event_type=value["event_type"],
            recorded_at=_parse_aware(value["recorded_at"], "event.recorded_at"),
            previous_event_sha256=value["previous_event_sha256"],
            runtime_semantics_sha256=value["runtime_semantics_sha256"],
            diagnostic_code_sha256=value["diagnostic_code_sha256"],
            git_commit=value["git_commit"],
            git_worktree_status=value["git_worktree_status"],
            expected_fields=tuple(value["expected_fields"]),
            runtime_semantic_parameters=value["runtime_semantic_parameters"],
            transport_status=value["transport_status"],
            http_status=value["http_status"],
            upstream_code=value["upstream_code"],
            sanitized_message_category=value["sanitized_message_category"],
            terminal_reason=value["terminal_reason"],
            counts=(
                HttpTerminalCountsV1.from_dict(counts_value)
                if counts_value is not None
                else None
            ),
            event_sha256=value["event_sha256"],
        )


def _build_event(**values: Any) -> TushareHttpDiagnosticEventV1:
    unsigned = _event_payload(**values)
    event = TushareHttpDiagnosticEventV1(
        diagnostic_run_id=values["diagnostic_run_id"],
        request_id=values["request_id"],
        sequence=values["sequence"],
        event_type=values["event_type"],
        recorded_at=values["recorded_at"],
        previous_event_sha256=values["previous_event_sha256"],
        runtime_semantics_sha256=values["runtime_semantics_hash"],
        diagnostic_code_sha256=values["diagnostic_code_sha256"],
        git_commit=values["git_commit"],
        git_worktree_status=values["git_worktree_status"],
        expected_fields=tuple(values["expected_fields"]),
        runtime_semantic_parameters=values["runtime_semantic_parameters"],
        transport_status=values["transport_status"],
        http_status=values["http_status"],
        upstream_code=values["upstream_code"],
        sanitized_message_category=values["sanitized_message_category"],
        terminal_reason=values["terminal_reason"],
        counts=values["counts"],
        event_sha256=canonical_sha256(unsigned),
    )
    validate_json_schema(event.to_dict(), EVENT_SCHEMA)
    return event


def build_http_run_created_event(
    *,
    diagnostic_run_id: str,
    request_id: str,
    recorded_at: datetime,
    runtime_semantic_parameters: Mapping[str, Any],
    diagnostic_code_sha256: str,
    git_commit: str,
    git_worktree_status: str,
    expected_fields: Sequence[str],
) -> TushareHttpDiagnosticEventV1:
    """Create the first event; it is the sole runtime-parameter source."""

    normalized = _normalized_runtime_parameters(runtime_semantic_parameters)
    return _build_event(
        diagnostic_run_id=diagnostic_run_id,
        request_id=request_id,
        sequence=1,
        event_type="RUN_CREATED",
        recorded_at=recorded_at,
        previous_event_sha256=None,
        runtime_semantics_hash=runtime_semantics_sha256(normalized),
        diagnostic_code_sha256=diagnostic_code_sha256,
        git_commit=git_commit,
        git_worktree_status=git_worktree_status,
        expected_fields=expected_fields,
        runtime_semantic_parameters=normalized,
        transport_status=None,
        http_status=None,
        upstream_code=None,
        sanitized_message_category=None,
        terminal_reason=None,
        counts=None,
    )


def _derived_terminal(events: Sequence[TushareHttpDiagnosticEventV1]) -> tuple[str, HttpTerminalCountsV1]:
    kinds = {event.event_type for event in events}
    reserved = int("REQUEST_RESERVED" in kinds)
    started = int("NETWORK_CALL_STARTED" in kinds)
    response = int("RESPONSE_RECEIVED" in kinds)
    if response:
        reason = "response_received"
    elif started:
        reason = "remote_execution_unknown"
    elif reserved:
        reason = "stopped_before_network"
    else:
        reason = "stopped_before_reservation"
    return reason, HttpTerminalCountsV1(
        reserved_request_count=reserved,
        network_call_started_count=started,
        response_received_count=response,
        terminal_result_count=1,
        remote_execution_unknown_count=started - response,
        budget_consumed_count=reserved,
    )


def _validate_chain(
    events: Sequence[TushareHttpDiagnosticEventV1],
    *,
    require_terminal: bool,
) -> tuple[TushareHttpDiagnosticEventV1, ...]:
    chain = tuple(events)
    if not chain or any(type(event) is not TushareHttpDiagnosticEventV1 for event in chain):
        raise TushareHttpTerminalError("event chain is empty or untyped")
    allowed_shapes = {
        ("RUN_CREATED",),
        ("RUN_CREATED", "TERMINAL"),
        ("RUN_CREATED", "REQUEST_RESERVED"),
        ("RUN_CREATED", "REQUEST_RESERVED", "TERMINAL"),
        ("RUN_CREATED", "REQUEST_RESERVED", "NETWORK_CALL_STARTED"),
        ("RUN_CREATED", "REQUEST_RESERVED", "NETWORK_CALL_STARTED", "TERMINAL"),
        ("RUN_CREATED", "REQUEST_RESERVED", "NETWORK_CALL_STARTED", "RESPONSE_RECEIVED"),
        (
            "RUN_CREATED",
            "REQUEST_RESERVED",
            "NETWORK_CALL_STARTED",
            "RESPONSE_RECEIVED",
            "TERMINAL",
        ),
    }
    shape = tuple(event.event_type for event in chain)
    if shape not in allowed_shapes or (require_terminal and shape[-1] != "TERMINAL"):
        raise TushareHttpTerminalError("event chain transition is invalid")
    first = chain[0]
    for index, event in enumerate(chain):
        if event.sequence != index + 1:
            raise TushareHttpTerminalError("event sequences are not contiguous")
        if event.diagnostic_run_id != first.diagnostic_run_id or event.request_id != first.request_id:
            raise TushareHttpTerminalError("event identity changed within the chain")
        if event.runtime_semantics_sha256 != first.runtime_semantics_sha256:
            raise TushareHttpTerminalError("runtime semantics changed within the chain")
        if (
            event.diagnostic_code_sha256 != first.diagnostic_code_sha256
            or event.git_commit != first.git_commit
            or event.git_worktree_status != first.git_worktree_status
            or event.expected_fields != first.expected_fields
        ):
            raise TushareHttpTerminalError("runtime context changed within the chain")
        if index and event.previous_event_sha256 != chain[index - 1].event_sha256:
            raise TushareHttpTerminalError("event previous hash does not match")
        if index and event.recorded_at < chain[index - 1].recorded_at:
            raise TushareHttpTerminalError("event time moved backwards")
        validate_json_schema(event.to_dict(), EVENT_SCHEMA)
    if shape[-1] == "TERMINAL":
        reason, counts = _derived_terminal(chain[:-1])
        if chain[-1].terminal_reason != reason or chain[-1].counts != counts:
            raise TushareHttpTerminalError("terminal result differs from its event prefix")
    return chain


def append_http_diagnostic_event(
    events: Sequence[TushareHttpDiagnosticEventV1],
    *,
    event_type: str,
    recorded_at: datetime,
    http_status: int | None = None,
    upstream_code: int | None = None,
    sanitized_message_category: str | None = None,
) -> TushareHttpDiagnosticEventV1:
    """Build the next immutable event from the validated persisted prefix."""

    chain = _validate_chain(events, require_terminal=False)
    if chain[-1].event_type == "TERMINAL":
        raise TushareHttpTerminalError("terminal event chain cannot be extended")
    next_types = {
        "RUN_CREATED": {"REQUEST_RESERVED", "TERMINAL"},
        "REQUEST_RESERVED": {"NETWORK_CALL_STARTED", "TERMINAL"},
        "NETWORK_CALL_STARTED": {"RESPONSE_RECEIVED", "TERMINAL"},
        "RESPONSE_RECEIVED": {"TERMINAL"},
    }
    if event_type not in next_types[chain[-1].event_type]:
        raise TushareHttpTerminalError("requested event is not a valid next state")
    if event_type == "RESPONSE_RECEIVED":
        transport_status = "response_received"
        terminal_reason = None
        counts = None
    elif event_type == "TERMINAL":
        if any(value is not None for value in (http_status, upstream_code, sanitized_message_category)):
            raise TushareHttpTerminalError("terminal event cannot accept response evidence")
        transport_status = None
        terminal_reason, counts = _derived_terminal(chain)
    else:
        if any(value is not None for value in (http_status, upstream_code, sanitized_message_category)):
            raise TushareHttpTerminalError("lifecycle event cannot accept response evidence")
        transport_status = None
        terminal_reason = None
        counts = None
    event = _build_event(
        diagnostic_run_id=chain[0].diagnostic_run_id,
        request_id=chain[0].request_id,
        sequence=len(chain) + 1,
        event_type=event_type,
        recorded_at=recorded_at,
        previous_event_sha256=chain[-1].event_sha256,
        runtime_semantics_hash=chain[0].runtime_semantics_sha256,
        diagnostic_code_sha256=chain[0].diagnostic_code_sha256,
        git_commit=chain[0].git_commit,
        git_worktree_status=chain[0].git_worktree_status,
        expected_fields=chain[0].expected_fields,
        runtime_semantic_parameters=None,
        transport_status=transport_status,
        http_status=http_status,
        upstream_code=upstream_code,
        sanitized_message_category=sanitized_message_category,
        terminal_reason=terminal_reason,
        counts=counts,
    )
    _validate_chain((*chain, event), require_terminal=event_type == "TERMINAL")
    return event


def verify_http_diagnostic_event(content: bytes | str) -> TushareHttpDiagnosticEventV1:
    value = strict_json_loads(
        content,
        label="Tushare HTTP diagnostic event",
        require_canonical=True,
    )
    if not isinstance(value, dict):
        raise TushareHttpTerminalError("event root must be an object")
    validate_json_schema(value, EVENT_SCHEMA)
    return TushareHttpDiagnosticEventV1.from_dict(value)


@dataclass(frozen=True, slots=True)
class TushareHttpTerminalDiagnosticReceiptV1:
    diagnostic_run_id: str
    request_id: str
    started_at: datetime
    completed_at: datetime
    runtime_semantic_parameters: Mapping[str, str]
    runtime_semantics_sha256: str
    diagnostic_code_sha256: str
    git_commit: str
    git_worktree_status: str
    expected_fields: tuple[str, ...]
    event_chain: tuple[TushareHttpDiagnosticEventV1, ...]
    journal_head_sha256: str
    counts: HttpTerminalCountsV1
    terminal_reason: str
    response_evidence_status: str
    transport_status: str | None
    http_status: int | None
    upstream_code: int | None
    sanitized_message_category: str | None
    receipt_sha256: str

    @property
    def reserved_request_count(self) -> int:
        return self.counts.reserved_request_count

    @property
    def network_call_started_count(self) -> int:
        return self.counts.network_call_started_count

    @property
    def response_received_count(self) -> int:
        return self.counts.response_received_count

    @property
    def terminal_result_count(self) -> int:
        return self.counts.terminal_result_count

    @property
    def remote_execution_unknown_count(self) -> int:
        return self.counts.remote_execution_unknown_count

    @property
    def budget_consumed_count(self) -> int:
        return self.counts.budget_consumed_count

    def __post_init__(self) -> None:
        chain = _validate_chain(self.event_chain, require_terminal=True)
        first, terminal = chain[0], chain[-1]
        if self.diagnostic_run_id != first.diagnostic_run_id or self.request_id != first.request_id:
            raise TushareHttpTerminalError("receipt identity differs from event chain")
        if self.started_at != first.recorded_at or self.completed_at != terminal.recorded_at:
            raise TushareHttpTerminalError("receipt time differs from event chain")
        normalized = _normalized_runtime_parameters(self.runtime_semantic_parameters)
        object.__setattr__(self, "runtime_semantic_parameters", normalized)
        if dict(normalized) != dict(first.runtime_semantic_parameters or {}):
            raise TushareHttpTerminalError("receipt parameters differ from RUN_CREATED")
        if self.runtime_semantics_sha256 != first.runtime_semantics_sha256:
            raise TushareHttpTerminalError("receipt runtime semantics hash differs")
        if (
            self.diagnostic_code_sha256 != first.diagnostic_code_sha256
            or self.git_commit != first.git_commit
            or self.git_worktree_status != first.git_worktree_status
            or tuple(self.expected_fields) != first.expected_fields
        ):
            raise TushareHttpTerminalError("receipt runtime context differs from RUN_CREATED")
        object.__setattr__(self, "expected_fields", first.expected_fields)
        if self.journal_head_sha256 != terminal.event_sha256:
            raise TushareHttpTerminalError("journal head differs from TERMINAL event")
        if terminal.counts != self.counts or terminal.terminal_reason != self.terminal_reason:
            raise TushareHttpTerminalError("receipt terminal result differs from journal")
        response = next((event for event in chain if event.event_type == "RESPONSE_RECEIVED"), None)
        expected_status = (
            "available"
            if response is not None
            else (
                "unavailable_after_network_start"
                if self.counts.network_call_started_count
                else "unavailable_before_network"
            )
        )
        if self.response_evidence_status != expected_status:
            raise TushareHttpTerminalError("response evidence status is inconsistent")
        expected_values = (
            (response.transport_status, response.http_status, response.upstream_code, response.sanitized_message_category)
            if response is not None
            else (None, None, None, None)
        )
        if (
            self.transport_status,
            self.http_status,
            self.upstream_code,
            self.sanitized_message_category,
        ) != expected_values:
            raise TushareHttpTerminalError("receipt response evidence differs from journal")
        _sha256(self.receipt_sha256, "receipt_sha256")

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "status": "terminal",
            "scope": "single_http_terminal_diagnostic_only_not_admitted",
            "provider_id": "tushare",
            "endpoint": "trade_cal",
            "transport_channel": "http",
            "transport_target": OFFICIAL_HTTPS_API_URL,
            "max_requests": 1,
            "sdk_ran": False,
            "automatic_retries_allowed": False,
            "diagnostic_run_id": self.diagnostic_run_id,
            "request_id": self.request_id,
            "started_at": _aware_iso(self.started_at, "receipt.started_at"),
            "completed_at": _aware_iso(self.completed_at, "receipt.completed_at"),
            "runtime_semantic_parameters": dict(self.runtime_semantic_parameters),
            "runtime_semantics_sha256": self.runtime_semantics_sha256,
            "diagnostic_code_sha256": self.diagnostic_code_sha256,
            "git_commit": self.git_commit,
            "git_worktree_status": self.git_worktree_status,
            "expected_fields": list(self.expected_fields),
            "event_chain": [event.to_dict() for event in self.event_chain],
            "journal_head_sha256": self.journal_head_sha256,
            **self.counts.to_dict(),
            "terminal_reason": self.terminal_reason,
            "response_evidence_status": self.response_evidence_status,
            "transport_status": self.transport_status,
            "http_status": self.http_status,
            "upstream_code": self.upstream_code,
            "sanitized_message_category": self.sanitized_message_category,
            "tushare_capability_judgment": "not_made",
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

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "receipt_sha256": self.receipt_sha256}


def build_http_terminal_diagnostic_receipt(
    *,
    events: Sequence[TushareHttpDiagnosticEventV1],
) -> TushareHttpTerminalDiagnosticReceiptV1:
    """Build a terminal receipt solely from the persisted RUN_CREATED chain."""

    chain = _validate_chain(events, require_terminal=True)
    first, terminal = chain[0], chain[-1]
    response = next((event for event in chain if event.event_type == "RESPONSE_RECEIVED"), None)
    counts = terminal.counts
    if counts is None or terminal.terminal_reason is None:
        raise TushareHttpTerminalError("terminal event lacks a result")
    evidence_status = (
        "available"
        if response is not None
        else (
            "unavailable_after_network_start"
            if counts.network_call_started_count
            else "unavailable_before_network"
        )
    )
    response_values = (
        (response.transport_status, response.http_status, response.upstream_code, response.sanitized_message_category)
        if response is not None
        else (None, None, None, None)
    )
    values = dict(
        diagnostic_run_id=first.diagnostic_run_id,
        request_id=first.request_id,
        started_at=first.recorded_at,
        completed_at=terminal.recorded_at,
        runtime_semantic_parameters=dict(first.runtime_semantic_parameters or {}),
        runtime_semantics_sha256=first.runtime_semantics_sha256,
        diagnostic_code_sha256=first.diagnostic_code_sha256,
        git_commit=first.git_commit,
        git_worktree_status=first.git_worktree_status,
        expected_fields=first.expected_fields,
        event_chain=chain,
        journal_head_sha256=terminal.event_sha256,
        counts=counts,
        terminal_reason=terminal.terminal_reason,
        response_evidence_status=evidence_status,
        transport_status=response_values[0],
        http_status=response_values[1],
        upstream_code=response_values[2],
        sanitized_message_category=response_values[3],
    )
    provisional = TushareHttpTerminalDiagnosticReceiptV1(
        **values,
        receipt_sha256="0" * 64,
    )
    receipt = TushareHttpTerminalDiagnosticReceiptV1(
        **values,
        receipt_sha256=canonical_sha256(provisional.unsigned_dict()),
    )
    validate_json_schema(receipt.to_dict(), RECEIPT_SCHEMA)
    return receipt


def verify_http_terminal_diagnostic_receipt(
    content: bytes | str,
) -> TushareHttpTerminalDiagnosticReceiptV1:
    value = strict_json_loads(
        content,
        label="Tushare HTTP terminal diagnostic receipt",
        require_canonical=True,
    )
    if not isinstance(value, dict):
        raise TushareHttpTerminalError("terminal receipt root must be an object")
    validate_json_schema(value, RECEIPT_SCHEMA)
    events = tuple(TushareHttpDiagnosticEventV1.from_dict(item) for item in value["event_chain"])
    counts = HttpTerminalCountsV1(
        reserved_request_count=value["reserved_request_count"],
        network_call_started_count=value["network_call_started_count"],
        response_received_count=value["response_received_count"],
        terminal_result_count=value["terminal_result_count"],
        remote_execution_unknown_count=value["remote_execution_unknown_count"],
        budget_consumed_count=value["budget_consumed_count"],
    )
    receipt = TushareHttpTerminalDiagnosticReceiptV1(
        diagnostic_run_id=value["diagnostic_run_id"],
        request_id=value["request_id"],
        started_at=_parse_aware(value["started_at"], "receipt.started_at"),
        completed_at=_parse_aware(value["completed_at"], "receipt.completed_at"),
        runtime_semantic_parameters=value["runtime_semantic_parameters"],
        runtime_semantics_sha256=value["runtime_semantics_sha256"],
        diagnostic_code_sha256=value["diagnostic_code_sha256"],
        git_commit=value["git_commit"],
        git_worktree_status=value["git_worktree_status"],
        expected_fields=tuple(value["expected_fields"]),
        event_chain=events,
        journal_head_sha256=value["journal_head_sha256"],
        counts=counts,
        terminal_reason=value["terminal_reason"],
        response_evidence_status=value["response_evidence_status"],
        transport_status=value["transport_status"],
        http_status=value["http_status"],
        upstream_code=value["upstream_code"],
        sanitized_message_category=value["sanitized_message_category"],
        receipt_sha256=value["receipt_sha256"],
    )
    if canonical_sha256(receipt.unsigned_dict()) != receipt.receipt_sha256:
        raise TushareHttpTerminalError("terminal receipt hash mismatch")
    return receipt


__all__ = [
    "EVENT_SCHEMA_VERSION",
    "RECEIPT_SCHEMA_VERSION",
    "HttpTerminalCountsV1",
    "TushareHttpDiagnosticEventV1",
    "TushareHttpTerminalDiagnosticReceiptV1",
    "TushareHttpTerminalError",
    "append_http_diagnostic_event",
    "build_http_run_created_event",
    "build_http_terminal_diagnostic_receipt",
    "runtime_semantics_sha256",
    "verify_http_diagnostic_event",
    "verify_http_terminal_diagnostic_receipt",
]
