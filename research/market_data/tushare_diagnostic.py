"""Typed, secret-free contract for the Tushare single-endpoint diagnostic.

This module is deliberately separate from the capability-receipt V1 bundle so
that adding P0 diagnostics does not invalidate already sealed 22-endpoint runs.
It stores categories and structural metadata only; upstream messages and raw
request/response bodies are never part of the contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from research.market_data.providers.base import safe_error_text
from research.market_data.tushare_capability import (
    canonical_json_bytes,
    canonical_sha256,
    normalize_parameters,
    strict_json_loads,
)
from research.market_data.validation import validate_json_schema


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DIAGNOSTIC_RECEIPT_SCHEMA = (
    REPOSITORY_ROOT
    / "schemas"
    / "tushare_single_endpoint_diagnostic_receipt.v1.json"
)
RECEIPT_SCHEMA_VERSION = "tushare-single-endpoint-diagnostic-receipt.v1"
CHANNEL_SCHEMA_VERSION = "tushare-diagnostic-channel-result.v1"
DIAGNOSTIC_SCOPE = "single_endpoint_transport_diagnostic_only_not_admitted"
SUPPORTED_ENDPOINTS = frozenset({"trade_cal", "daily"})
CHANNELS = ("sdk", "http")
TRANSPORT_STATUSES = frozenset(
    {
        "not_attempted",
        "response_received",
        "dns_failure",
        "connection_failure",
        "timeout",
        "tls_failure",
        "protocol_failure",
        "unknown_failure",
    }
)
MESSAGE_CATEGORIES = frozenset(
    {
        "success",
        "permission",
        "rate_limit",
        "authentication_account",
        "invalid_parameter",
        "server_internal",
        "network_transport",
        "sdk_client",
        "unknown",
        "not_attempted",
    }
)
CHANNEL_OUTCOMES = frozenset(
    {"passed", "upstream_rejected", "transport_failed", "client_failed", "not_attempted"}
)
FINAL_CONCLUSIONS = frozenset(
    {
        "token_or_account_problem",
        "sdk_client_problem",
        "network_transport_problem",
        "capability_probe_bug",
    }
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_TARGET = re.compile(r"^https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~/-]*)?$")


class TushareDiagnosticError(RuntimeError):
    """Raised when diagnostic evidence violates the frozen contract."""


def _aware_iso(value: datetime, label: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TushareDiagnosticError(f"{label} must be timezone-aware")
    return value.isoformat()


def _parse_aware(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise TushareDiagnosticError(f"{label} must be a date-time string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TushareDiagnosticError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise TushareDiagnosticError(f"{label} must be timezone-aware")
    if parsed.isoformat() != value:
        raise TushareDiagnosticError(f"{label} must use canonical ISO format")
    return parsed


def safe_exception_type(error: BaseException | None) -> str | None:
    """Return only a fixed class label, never dynamic names or exception text."""

    if error is None:
        return None
    allowed = {
        "Exception",
        "RuntimeError",
        "ValueError",
        "TypeError",
        "KeyError",
        "AttributeError",
        "ImportError",
        "ModuleNotFoundError",
        "JSONDecodeError",
        "Timeout",
        "ConnectTimeout",
        "ReadTimeout",
        "ConnectionError",
        "ProxyError",
        "SSLError",
        "HTTPError",
        "URLError",
    }
    name = type(error).__name__
    return name if name in allowed else "OtherError"


def normalize_upstream_code(value: Any) -> int | None:
    """Keep only bounded integer codes; booleans and opaque strings are rejected.

    Tushare's response envelope uses a numeric ``code``.  Restricting persisted
    evidence to that shape prevents a malicious or malformed upstream from
    smuggling a credential, message, or token fragment through this field.
    Numeric strings may be accepted at extraction time but are normalized to
    an integer before entering the receipt contract.
    """

    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if type(value) is int:
        return value if -(2**31) <= value <= (2**31 - 1) else None
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"-?(?:0|[1-9][0-9]{0,9})", text):
            parsed = int(text)
            return parsed if -(2**31) <= parsed <= (2**31 - 1) else None
    return None


def _category_from_code(code: int | None) -> str | None:
    """Map only code meanings that are unambiguous in the wire contract.

    Non-zero Tushare business codes are assigned a meaning only when the
    official HTTP contract documents it.  Other codes retain their numeric
    evidence and use the same structured envelope's message for categorization.
    """

    if code == 0:
        return "success"
    # The official HTTP API contract documents 2002 as an interface
    # permission rejection.  Keep this narrow mapping ahead of message and
    # HTTP-status heuristics; do not guess meanings for other business codes.
    if code == 2002:
        return "permission"
    return None


def _category_from_http_status(http_status: int | None) -> str | None:
    if http_status is None:
        return None
    if http_status == 401:
        return "authentication_account"
    if http_status == 403:
        return "permission"
    if http_status == 429:
        return "rate_limit"
    if http_status in {400, 404, 405, 409, 422}:
        return "invalid_parameter"
    if 500 <= http_status <= 599:
        return "server_internal"
    return None


def classify_message_category(
    *,
    upstream_code: int | str | None,
    http_status: int | None,
    message: str | None,
    error: BaseException | None,
    transport_status: str,
    channel: str,
    secret: str = "",
) -> str:
    """Classify without returning or persisting the upstream message.

    Structured ``code`` is authoritative when it has a frozen mapping.  A
    structured response message is next, then HTTP status, followed by
    exception/transport structure.  The caller persists only the returned
    enum and the normalized integer code, never the message.
    """

    normalized_code = normalize_upstream_code(upstream_code)
    by_code = _category_from_code(normalized_code)
    if by_code is not None:
        return by_code
    text = str(message or "")
    if secret:
        text = text.replace(secret, "[REDACTED]")
    lowered = safe_error_text(text).casefold()
    marker_groups = (
        (
            "authentication_account",
            (
                "token invalid",
                "invalid token",
                "token无效",
                "token 无效",
                "token不能为空",
                "token 不能为空",
                "token不存在",
                "token 不存在",
                "token已失效",
                "token 已失效",
                "token expired",
                "请填写token",
                "invalid credential",
                "凭证无效",
                "认证失败",
                "账号异常",
                "账户异常",
                "账户不存在",
                "账号被禁用",
                "account disabled",
                "account invalid",
            ),
        ),
        (
            "permission",
            (
                "permission denied",
                "no permission",
                "no access",
                "not authorized",
                "没有访问该接口的权限",
                "没有权限",
                "无权限",
                "权限不足",
                "积分不足",
            ),
        ),
        (
            "rate_limit",
            (
                "rate limit",
                "too many requests",
                "每分钟",
                "访问频率",
                "频率过高",
                "限频",
            ),
        ),
        (
            "invalid_parameter",
            (
                "invalid parameter",
                "invalid argument",
                "参数错误",
                "参数无效",
                "不包含字段",
                "没有接口",
                "接口不存在",
            ),
        ),
        (
            "server_internal",
            (
                "internal server",
                "server error",
                "service unavailable",
                "系统内部错误",
                "服务器内部",
                "服务不可用",
            ),
        ),
    )
    for category, markers in marker_groups:
        if any(marker in lowered for marker in markers):
            return category

    by_http = _category_from_http_status(http_status)
    if by_http is not None:
        return by_http

    if transport_status not in {"response_received", "not_attempted"}:
        return "network_transport"
    if error is not None:
        module_root = type(error).__module__.split(".", 1)[0]
        if module_root in {"requests", "urllib", "urllib3", "socket", "ssl"}:
            return "network_transport"
        return "sdk_client" if channel == "sdk" else "unknown"
    return "unknown"


@dataclass(frozen=True, slots=True)
class DiagnosticChannelResultV1:
    channel: str
    endpoint: str
    transport_target: str
    diagnostic_attempted: bool
    request_count: int
    requested_at: datetime | None
    completed_at: datetime | None
    transport_status: str
    http_status: int | None
    upstream_code: int | None
    sdk_exception_type: str | None
    sanitized_message_category: str
    outcome: str
    row_count: int
    field_names: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.channel not in CHANNELS:
            raise TushareDiagnosticError("diagnostic channel is invalid")
        if self.endpoint not in SUPPORTED_ENDPOINTS:
            raise TushareDiagnosticError("diagnostic endpoint is not allowed")
        if (
            type(self.transport_target) is not str
            or len(self.transport_target) > 256
            or _SAFE_TARGET.fullmatch(self.transport_target) is None
        ):
            raise TushareDiagnosticError("diagnostic transport target must be a safe HTTPS URL")
        if type(self.diagnostic_attempted) is not bool:
            raise TushareDiagnosticError("diagnostic_attempted must be boolean")
        if type(self.request_count) is not int or self.request_count not in {0, 1}:
            raise TushareDiagnosticError("channel request_count must be zero or one")
        if self.transport_status not in TRANSPORT_STATUSES:
            raise TushareDiagnosticError("channel transport_status is invalid")
        if self.sanitized_message_category not in MESSAGE_CATEGORIES:
            raise TushareDiagnosticError("channel message category is invalid")
        if self.outcome not in CHANNEL_OUTCOMES:
            raise TushareDiagnosticError("channel outcome is invalid")
        if self.http_status is not None and (
            type(self.http_status) is not int or not (100 <= self.http_status <= 599)
        ):
            raise TushareDiagnosticError("channel http_status is invalid")
        normalized_code = normalize_upstream_code(self.upstream_code)
        if normalized_code != self.upstream_code:
            raise TushareDiagnosticError("channel upstream_code is unsafe")
        if self.sdk_exception_type is not None and (
            _IDENTIFIER.fullmatch(self.sdk_exception_type) is None
        ):
            raise TushareDiagnosticError("sdk_exception_type is unsafe")
        if self.channel == "http" and self.sdk_exception_type is not None:
            raise TushareDiagnosticError("HTTP channel cannot report an SDK exception")
        if type(self.row_count) is not int or not (0 <= self.row_count <= 10000):
            raise TushareDiagnosticError("channel row_count is invalid")
        if type(self.field_names) is not tuple or len(set(self.field_names)) != len(
            self.field_names
        ):
            raise TushareDiagnosticError("channel field_names must be a unique tuple")
        if any(
            not isinstance(field, str)
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", field) is None
            for field in self.field_names
        ):
            raise TushareDiagnosticError("channel field_names are unsafe")

        if not self.diagnostic_attempted:
            if self.request_count != 0:
                raise TushareDiagnosticError(
                    "unattempted diagnostic cannot claim an outbound request"
                )
            if self.requested_at is not None or self.completed_at is not None:
                raise TushareDiagnosticError("not-attempted channel cannot have timestamps")
            if (
                self.transport_status != "not_attempted"
                or self.http_status is not None
                or self.upstream_code is not None
                or self.sdk_exception_type is not None
                or self.sanitized_message_category != "not_attempted"
                or self.outcome != "not_attempted"
                or self.row_count != 0
                or self.field_names
            ):
                raise TushareDiagnosticError("not-attempted channel semantics are invalid")
            return

        if self.requested_at is None or self.completed_at is None:
            raise TushareDiagnosticError("attempted diagnostic channel requires timestamps")
        _aware_iso(self.requested_at, "channel.requested_at")
        _aware_iso(self.completed_at, "channel.completed_at")
        if self.completed_at < self.requested_at:
            raise TushareDiagnosticError("channel completed_at precedes requested_at")
        if self.outcome == "not_attempted" or self.sanitized_message_category == "not_attempted":
            raise TushareDiagnosticError(
                "attempted diagnostic channel must have a terminal outcome"
            )
        if self.request_count == 0:
            if (
                self.transport_status != "not_attempted"
                or self.http_status is not None
                or self.upstream_code is not None
                or self.outcome != "client_failed"
                or self.sanitized_message_category not in {"sdk_client", "unknown"}
                or self.row_count != 0
                or self.field_names
            ):
                raise TushareDiagnosticError(
                    "pre-request diagnostic failure semantics are invalid"
                )
            if self.channel == "sdk" and self.sdk_exception_type is None:
                raise TushareDiagnosticError(
                    "pre-request SDK failure requires an exception type"
                )
            return

        if self.transport_status == "not_attempted":
            raise TushareDiagnosticError(
                "an outbound request cannot have not_attempted transport status"
            )
        if self.channel == "http":
            if self.transport_status == "response_received" and self.http_status is None:
                raise TushareDiagnosticError(
                    "HTTP response_received requires http_status"
                )
            if self.transport_status != "response_received" and self.http_status is not None:
                raise TushareDiagnosticError(
                    "HTTP transport failure cannot claim http_status"
                )
        elif self.transport_status != "response_received" and self.http_status is not None:
            raise TushareDiagnosticError(
                "SDK transport failure cannot claim http_status"
            )
        if self.outcome == "passed":
            if (
                self.transport_status != "response_received"
                or self.sanitized_message_category != "success"
                or self.sdk_exception_type is not None
                or self.row_count <= 0
                or not self.field_names
            ):
                raise TushareDiagnosticError("passed channel semantics are invalid")
            if self.channel == "http" and (
                self.upstream_code != 0
                or self.http_status is None
                or not (200 <= self.http_status <= 299)
            ):
                raise TushareDiagnosticError(
                    "passed HTTP channel requires a 2xx response and code zero"
                )
            if self.channel == "sdk" and self.upstream_code not in {None, 0}:
                raise TushareDiagnosticError(
                    "passed SDK channel cannot claim a failing upstream code"
                )
            return

        if self.row_count != 0 or self.field_names:
            raise TushareDiagnosticError("failed channel cannot claim tabular data")
        if self.outcome == "transport_failed":
            if (
                self.transport_status in {"response_received", "not_attempted"}
                or self.http_status is not None
                or self.upstream_code is not None
                or self.sanitized_message_category != "network_transport"
            ):
                raise TushareDiagnosticError("transport failure semantics are invalid")
        elif self.outcome == "upstream_rejected":
            if (
                self.transport_status != "response_received"
                or self.sanitized_message_category
                not in {
                    "permission",
                    "rate_limit",
                    "authentication_account",
                    "invalid_parameter",
                    "server_internal",
                    "unknown",
                }
                or self.upstream_code == 0
            ):
                raise TushareDiagnosticError("upstream rejection semantics are invalid")
        elif self.outcome == "client_failed":
            if (
                self.sanitized_message_category not in {"sdk_client", "unknown"}
                or self.transport_status
                not in {
                    "not_attempted",
                    "response_received",
                    "protocol_failure",
                    "unknown_failure",
                }
            ):
                raise TushareDiagnosticError("client failure category is invalid")
        else:
            raise TushareDiagnosticError("attempted channel outcome is inconsistent")
        if (
            self.channel == "sdk"
            and self.outcome == "transport_failed"
            and self.sdk_exception_type is None
        ):
            raise TushareDiagnosticError(
                "SDK transport failure requires an exception type"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CHANNEL_SCHEMA_VERSION,
            "channel": self.channel,
            "endpoint": self.endpoint,
            "transport_target": self.transport_target,
            "diagnostic_attempted": self.diagnostic_attempted,
            "request_count": self.request_count,
            "requested_at": (
                _aware_iso(self.requested_at, "channel.requested_at")
                if self.requested_at is not None
                else None
            ),
            "completed_at": (
                _aware_iso(self.completed_at, "channel.completed_at")
                if self.completed_at is not None
                else None
            ),
            "transport_status": self.transport_status,
            "http_status": self.http_status,
            "upstream_code": self.upstream_code,
            "sdk_exception_type": self.sdk_exception_type,
            "sanitized_message_category": self.sanitized_message_category,
            "outcome": self.outcome,
            "row_count": self.row_count,
            "field_names": list(self.field_names),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DiagnosticChannelResultV1":
        expected = set(cls(
            channel="sdk",
            endpoint="trade_cal",
            transport_target="https://example.invalid",
            diagnostic_attempted=False,
            request_count=0,
            requested_at=None,
            completed_at=None,
            transport_status="not_attempted",
            http_status=None,
            upstream_code=None,
            sdk_exception_type=None,
            sanitized_message_category="not_attempted",
            outcome="not_attempted",
            row_count=0,
            field_names=(),
        ).to_dict())
        if set(value) != expected or value.get("schema_version") != CHANNEL_SCHEMA_VERSION:
            raise TushareDiagnosticError("diagnostic channel fields differ from contract")
        return cls(
            channel=value["channel"],
            endpoint=value["endpoint"],
            transport_target=value["transport_target"],
            diagnostic_attempted=value["diagnostic_attempted"],
            request_count=value["request_count"],
            requested_at=(
                _parse_aware(value["requested_at"], "channel.requested_at")
                if value["requested_at"] is not None
                else None
            ),
            completed_at=(
                _parse_aware(value["completed_at"], "channel.completed_at")
                if value["completed_at"] is not None
                else None
            ),
            transport_status=value["transport_status"],
            http_status=value["http_status"],
            upstream_code=value["upstream_code"],
            sdk_exception_type=value["sdk_exception_type"],
            sanitized_message_category=value["sanitized_message_category"],
            outcome=value["outcome"],
            row_count=value["row_count"],
            field_names=tuple(value["field_names"]),
        )


def derive_conclusion(channels: Sequence[DiagnosticChannelResultV1]) -> str:
    if tuple(item.channel for item in channels) != CHANNELS:
        raise TushareDiagnosticError("diagnostic channels must be ordered sdk,http")
    sdk, http = channels
    if sdk.endpoint != http.endpoint:
        raise TushareDiagnosticError("diagnostic channels must use the same endpoint")
    if not sdk.diagnostic_attempted or not http.diagnostic_attempted:
        raise TushareDiagnosticError(
            "a conclusion requires both diagnostic channels to be attempted"
        )

    account_categories = {"authentication_account", "permission", "rate_limit"}
    if http.outcome == "passed":
        if sdk.outcome == "passed":
            # The original 37/37 failure is not reproduced by either channel.
            return "capability_probe_bug"
        return "sdk_client_problem"
    if sdk.outcome == "passed":
        # A successful SDK request disproves a general token or network outage;
        # a failing direct channel therefore points back to this diagnostic.
        return "capability_probe_bug"
    if (
        http.sanitized_message_category in account_categories
        or sdk.sanitized_message_category in account_categories
    ):
        return "token_or_account_problem"
    if (
        http.sanitized_message_category == "invalid_parameter"
        or sdk.sanitized_message_category == "invalid_parameter"
    ):
        return "capability_probe_bug"

    if http.sanitized_message_category in {
        "network_transport",
        "server_internal",
    } or http.outcome == "transport_failed":
        return "network_transport_problem"
    return "capability_probe_bug"


@dataclass(frozen=True, slots=True)
class SingleEndpointDiagnosticReceiptV1:
    diagnostic_run_id: str
    status: str
    started_at: datetime
    completed_at: datetime
    endpoint: str
    semantic_parameters: Mapping[str, str]
    same_semantic_parameters: bool
    sdk_version: str
    python_version: str
    credential_status: str
    config_sha256: str
    diagnostic_code_sha256: str
    git_commit: str
    git_worktree_status: str
    maximum_request_budget: int
    planned_request_count: int
    request_count: int
    channels: tuple[DiagnosticChannelResultV1, ...]
    conclusion: str | None
    receipt_sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.diagnostic_run_id) is not str
            or _IDENTIFIER.fullmatch(self.diagnostic_run_id) is None
        ):
            raise TushareDiagnosticError("diagnostic_run_id is invalid")
        if self.status not in {"completed", "not_configured", "dependency_missing", "failed"}:
            raise TushareDiagnosticError("diagnostic receipt status is invalid")
        _aware_iso(self.started_at, "receipt.started_at")
        _aware_iso(self.completed_at, "receipt.completed_at")
        if self.completed_at < self.started_at:
            raise TushareDiagnosticError("receipt completed_at precedes started_at")
        if self.endpoint not in SUPPORTED_ENDPOINTS:
            raise TushareDiagnosticError("receipt endpoint is not allowed")
        normalized = normalize_parameters(self.endpoint, self.semantic_parameters)
        if dict(normalized) != dict(self.semantic_parameters):
            raise TushareDiagnosticError("semantic parameters are not canonical")
        if self.same_semantic_parameters is not True:
            raise TushareDiagnosticError("both channels must use identical semantic parameters")
        if (
            type(self.sdk_version) is not str
            or not self.sdk_version
            or len(self.sdk_version) > 128
        ):
            raise TushareDiagnosticError("sdk_version is required and bounded")
        if (
            type(self.python_version) is not str
            or not self.python_version
            or len(self.python_version) > 128
        ):
            raise TushareDiagnosticError("python_version is required and bounded")
        if self.credential_status not in {"configured", "not_configured"}:
            raise TushareDiagnosticError("credential_status is invalid")
        if (
            type(self.config_sha256) is not str
            or type(self.diagnostic_code_sha256) is not str
            or _SHA256.fullmatch(self.config_sha256) is None
            or _SHA256.fullmatch(self.diagnostic_code_sha256) is None
        ):
            raise TushareDiagnosticError("diagnostic commitments are invalid")
        if type(self.git_commit) is not str or (
            self.git_commit != "unknown"
            and re.fullmatch(r"[0-9a-f]{40}", self.git_commit) is None
        ):
            raise TushareDiagnosticError("git_commit is invalid")
        if self.git_worktree_status not in {"clean", "dirty", "unknown"}:
            raise TushareDiagnosticError("git_worktree_status is invalid")
        if (
            type(self.maximum_request_budget) is not int
            or type(self.planned_request_count) is not int
            or self.maximum_request_budget != 4
            or self.planned_request_count != 2
        ):
            raise TushareDiagnosticError("diagnostic request budget is not frozen")
        if (
            not isinstance(self.channels, tuple)
            or not all(type(item) is DiagnosticChannelResultV1 for item in self.channels)
        ):
            raise TushareDiagnosticError(
                "receipt channels must use exact controlled result types"
            )
        if tuple(item.channel for item in self.channels) != CHANNELS:
            raise TushareDiagnosticError("receipt must contain sdk,http channels")
        if any(item.endpoint != self.endpoint for item in self.channels):
            raise TushareDiagnosticError("receipt channels use different endpoints")
        if type(self.request_count) is not int or self.request_count != sum(
            item.request_count for item in self.channels
        ):
            raise TushareDiagnosticError("receipt request_count differs from channels")
        if not (0 <= self.request_count <= self.planned_request_count <= self.maximum_request_budget):
            raise TushareDiagnosticError("receipt request budget was exceeded")
        if self.status == "completed":
            if (
                not all(item.diagnostic_attempted for item in self.channels)
                or any(item.outcome == "not_attempted" for item in self.channels)
                or self.conclusion not in FINAL_CONCLUSIONS
            ):
                raise TushareDiagnosticError(
                    "completed diagnostic lacks two terminal channel attempts or conclusion"
                )
            if self.conclusion != derive_conclusion(self.channels):
                raise TushareDiagnosticError("diagnostic conclusion differs from evidence")
        elif self.conclusion is not None:
            raise TushareDiagnosticError("incomplete diagnostic cannot claim a conclusion")
        if (
            type(self.receipt_sha256) is not str
            or _SHA256.fullmatch(self.receipt_sha256) is None
        ):
            raise TushareDiagnosticError("receipt_sha256 is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "diagnostic_run_id": self.diagnostic_run_id,
            "status": self.status,
            "scope": DIAGNOSTIC_SCOPE,
            "started_at": _aware_iso(self.started_at, "receipt.started_at"),
            "completed_at": _aware_iso(self.completed_at, "receipt.completed_at"),
            "provider_id": "tushare",
            "endpoint": self.endpoint,
            "semantic_parameters": dict(self.semantic_parameters),
            "same_semantic_parameters": self.same_semantic_parameters,
            "sdk_version": self.sdk_version,
            "python_version": self.python_version,
            "credential_status": self.credential_status,
            "config_sha256": self.config_sha256,
            "diagnostic_code_sha256": self.diagnostic_code_sha256,
            "git_commit": self.git_commit,
            "git_worktree_status": self.git_worktree_status,
            "maximum_request_budget": self.maximum_request_budget,
            "planned_request_count": self.planned_request_count,
            "request_count": self.request_count,
            "channels": [item.to_dict() for item in self.channels],
            "conclusion": self.conclusion,
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


def build_diagnostic_receipt(
    *,
    diagnostic_run_id: str,
    status: str,
    started_at: datetime,
    completed_at: datetime,
    endpoint: str,
    semantic_parameters: Mapping[str, str],
    sdk_version: str,
    python_version: str,
    credential_status: str,
    config_sha256: str,
    diagnostic_code_sha256: str,
    git_commit: str,
    git_worktree_status: str,
    channels: Sequence[DiagnosticChannelResultV1],
    conclusion: str | None = None,
) -> SingleEndpointDiagnosticReceiptV1:
    channel_tuple = tuple(channels)
    if status == "completed" and conclusion is None:
        conclusion = derive_conclusion(channel_tuple)
    payload = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "diagnostic_run_id": diagnostic_run_id,
        "status": status,
        "scope": DIAGNOSTIC_SCOPE,
        "started_at": _aware_iso(started_at, "receipt.started_at"),
        "completed_at": _aware_iso(completed_at, "receipt.completed_at"),
        "provider_id": "tushare",
        "endpoint": endpoint,
        "semantic_parameters": dict(semantic_parameters),
        "same_semantic_parameters": True,
        "sdk_version": sdk_version,
        "python_version": python_version,
        "credential_status": credential_status,
        "config_sha256": config_sha256,
        "diagnostic_code_sha256": diagnostic_code_sha256,
        "git_commit": git_commit,
        "git_worktree_status": git_worktree_status,
        "maximum_request_budget": 4,
        "planned_request_count": 2,
        "request_count": sum(item.request_count for item in channel_tuple),
        "channels": [item.to_dict() for item in channel_tuple],
        "conclusion": conclusion,
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
    receipt_sha256 = canonical_sha256(payload)
    receipt = SingleEndpointDiagnosticReceiptV1(
        diagnostic_run_id=diagnostic_run_id,
        status=status,
        started_at=started_at,
        completed_at=completed_at,
        endpoint=endpoint,
        semantic_parameters=dict(semantic_parameters),
        same_semantic_parameters=True,
        sdk_version=sdk_version,
        python_version=python_version,
        credential_status=credential_status,
        config_sha256=config_sha256,
        diagnostic_code_sha256=diagnostic_code_sha256,
        git_commit=git_commit,
        git_worktree_status=git_worktree_status,
        maximum_request_budget=4,
        planned_request_count=2,
        request_count=sum(item.request_count for item in channel_tuple),
        channels=channel_tuple,
        conclusion=conclusion,
        receipt_sha256=receipt_sha256,
    )
    validate_json_schema(receipt.to_dict(), DIAGNOSTIC_RECEIPT_SCHEMA)
    return receipt


def verify_diagnostic_receipt(
    content: bytes | str,
) -> SingleEndpointDiagnosticReceiptV1:
    raw = content.encode("utf-8") if isinstance(content, str) else content
    value = strict_json_loads(raw, label="Tushare diagnostic receipt")
    if not isinstance(value, dict):
        raise TushareDiagnosticError("diagnostic receipt root must be an object")
    if canonical_json_bytes(value) != raw:
        raise TushareDiagnosticError("diagnostic receipt must be canonical JSON")
    validate_json_schema(value, DIAGNOSTIC_RECEIPT_SCHEMA)
    expected_fields = set(build_diagnostic_receipt(
        diagnostic_run_id="contract-shape",
        status="not_configured",
        started_at=datetime.fromisoformat("2026-01-01T00:00:00+00:00"),
        completed_at=datetime.fromisoformat("2026-01-01T00:00:00+00:00"),
        endpoint="trade_cal",
        semantic_parameters={"exchange": "SSE", "start_date": "20260101", "end_date": "20260102"},
        sdk_version="not_loaded",
        python_version="unknown",
        credential_status="not_configured",
        config_sha256="0" * 64,
        diagnostic_code_sha256="0" * 64,
        git_commit="unknown",
        git_worktree_status="unknown",
        channels=(
            DiagnosticChannelResultV1(
                channel="sdk",
                endpoint="trade_cal",
                transport_target="https://example.invalid",
                diagnostic_attempted=False,
                request_count=0,
                requested_at=None,
                completed_at=None,
                transport_status="not_attempted",
                http_status=None,
                upstream_code=None,
                sdk_exception_type=None,
                sanitized_message_category="not_attempted",
                outcome="not_attempted",
                row_count=0,
                field_names=(),
            ),
            DiagnosticChannelResultV1(
                channel="http",
                endpoint="trade_cal",
                transport_target="https://example.invalid",
                diagnostic_attempted=False,
                request_count=0,
                requested_at=None,
                completed_at=None,
                transport_status="not_attempted",
                http_status=None,
                upstream_code=None,
                sdk_exception_type=None,
                sanitized_message_category="not_attempted",
                outcome="not_attempted",
                row_count=0,
                field_names=(),
            ),
        ),
    ).to_dict())
    if set(value) != expected_fields:
        raise TushareDiagnosticError("diagnostic receipt fields differ from contract")
    receipt = SingleEndpointDiagnosticReceiptV1(
        diagnostic_run_id=value["diagnostic_run_id"],
        status=value["status"],
        started_at=_parse_aware(value["started_at"], "receipt.started_at"),
        completed_at=_parse_aware(value["completed_at"], "receipt.completed_at"),
        endpoint=value["endpoint"],
        semantic_parameters=dict(value["semantic_parameters"]),
        same_semantic_parameters=value["same_semantic_parameters"],
        sdk_version=value["sdk_version"],
        python_version=value["python_version"],
        credential_status=value["credential_status"],
        config_sha256=value["config_sha256"],
        diagnostic_code_sha256=value["diagnostic_code_sha256"],
        git_commit=value["git_commit"],
        git_worktree_status=value["git_worktree_status"],
        maximum_request_budget=value["maximum_request_budget"],
        planned_request_count=value["planned_request_count"],
        request_count=value["request_count"],
        channels=tuple(
            DiagnosticChannelResultV1.from_dict(item) for item in value["channels"]
        ),
        conclusion=value["conclusion"],
        receipt_sha256=value["receipt_sha256"],
    )
    unsigned = dict(receipt.to_dict())
    declared = unsigned.pop("receipt_sha256")
    if canonical_sha256(unsigned) != declared:
        raise TushareDiagnosticError("diagnostic receipt hash mismatch")
    return receipt
