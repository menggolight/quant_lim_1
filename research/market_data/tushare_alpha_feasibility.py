"""Fail-closed Tushare data lane for the Alpha Feasibility experiment.

This module is deliberately independent from the formal small-account backtest
and from the Tushare SDK.  It only speaks the six standard, read-only
endpoints frozen in ``a_share_technical_alpha_feasibility.v2.json``.  The
collector has three important properties:

* configuration and the complete request plan are checked before a credential
  is inspected or a network transport is constructed;
* every P1.5 remote attempt is claimed by a create-only attempt journal; a
  completed fingerprint is replayed, while an interrupted fingerprint can be
  resumed without overwriting any prior attempt evidence;
* untrusted response bytes are bounded and validated before they can become a
  consumer artifact.  A response containing post-2023 data is quarantined by
  hash only and its body is not persisted.

The normalized representation is plain Python dictionaries and lists.  No
DataFrame, SDK object, broker object, account state, or order capability enters
this boundary.
"""

from __future__ import annotations

import calendar
import hashlib
import http.client
import json
import math
import os
import re
import ssl
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from research.market_data.validation import SchemaValidationError, validate_json_schema


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DATA_TMP_ROOT = REPOSITORY_ROOT / "data" / "tmp"
DEFAULT_CONFIG_PATH = (
    REPOSITORY_ROOT / "configs" / "a_share_technical_alpha_feasibility.v2.json"
)
P15_CONFIG_PATH = (
    REPOSITORY_ROOT / "configs" / "a_share_technical_alpha_feasibility.p1_5.json"
)
OFFICIAL_API_HOST = "api.tushare.pro"
OFFICIAL_API_PATH = "/"
OFFICIAL_API_URL = "https://api.tushare.pro"
ABSOLUTE_CUTOFF = date(2023, 12, 31)
PLAN_SCHEMA_VERSION = "tushare-alpha-feasibility-plan.v2"
TASK_SCHEMA_VERSION = "tushare-alpha-feasibility-task.v1"
STARTED_SCHEMA_VERSION = "tushare-alpha-feasibility-task-started.v1"
RECOVERABLE_STARTED_SCHEMA_VERSION = "tushare-alpha-feasibility-task-started.v2"
ATTEMPT_SCHEMA_VERSION = "tushare-alpha-feasibility-attempt.v1"
IMPORT_SCHEMA_VERSION = "tushare-alpha-feasibility-task-import.v1"
PARENT_REUSE_IMPORT_SCHEMA_VERSION = "tushare-alpha-feasibility-task-import.v2"
RESPONSE_SCHEMA_VERSION = "tushare-alpha-feasibility-task-response.v4"
LEGACY_RESPONSE_SCHEMA_VERSIONS = frozenset(
    {
        "tushare-alpha-feasibility-task-response.v2",
        "tushare-alpha-feasibility-task-response.v3",
    }
)
QUARANTINE_SCHEMA_VERSION = "tushare-alpha-feasibility-quarantine.v5"
LEGACY_QUARANTINE_SCHEMA_VERSIONS = frozenset(
    {"tushare-alpha-feasibility-quarantine.v4"}
)
BUSINESS_ERROR_SCHEMA_VERSION = "tushare-alpha-feasibility-business-error.v1"
PIT_REPORT_SCHEMA_VERSION = "pit-membership-coverage-report.v2"
PIT_MANIFEST_SCHEMA_VERSION = "pit-membership-manifest.v2"
P15_PIT_REPORT_SCHEMA_VERSION = "pit-membership-coverage-report.v3"
P15_PIT_MANIFEST_SCHEMA_VERSION = "pit-membership-manifest.v3"
HISTORY_COVERAGE_SCHEMA_VERSION = "tushare-alpha-feasibility-history-coverage.v2"
HISTORY_MANIFEST_SCHEMA_VERSION = "tushare-alpha-feasibility-manifest.v3"
P15_HISTORY_MANIFEST_SCHEMA_VERSION = "tushare-alpha-feasibility-manifest.v4"
HISTORY_MANIFEST_SCHEMA_PATHS = MappingProxyType(
    {
        HISTORY_MANIFEST_SCHEMA_VERSION: REPOSITORY_ROOT
        / "schemas"
        / "tushare_alpha_feasibility_manifest.v3.json",
        P15_HISTORY_MANIFEST_SCHEMA_VERSION: REPOSITORY_ROOT
        / "schemas"
        / "tushare_alpha_feasibility_manifest.v4.json",
    }
)
RESPONSE_SCHEMA_PATH = (
    REPOSITORY_ROOT / "schemas" / "tushare_alpha_feasibility_task_response.v4.json"
)
QUARANTINE_SCHEMA_PATHS = MappingProxyType(
    {
        "tushare-alpha-feasibility-quarantine.v4": REPOSITORY_ROOT
        / "schemas"
        / "tushare_alpha_feasibility_quarantine.v4.json",
        QUARANTINE_SCHEMA_VERSION: REPOSITORY_ROOT
        / "schemas"
        / "tushare_alpha_feasibility_quarantine.v5.json",
    }
)
QUARANTINE_SCHEMA_PATH = QUARANTINE_SCHEMA_PATHS[QUARANTINE_SCHEMA_VERSION]
BUSINESS_ERROR_SCHEMA_PATH = (
    REPOSITORY_ROOT / "schemas" / "tushare_alpha_feasibility_business_error.v1.json"
)
ATTEMPT_SCHEMA_PATH = (
    REPOSITORY_ROOT / "schemas" / "tushare_alpha_feasibility_attempt.v1.json"
)
IMPORT_SCHEMA_PATH = (
    REPOSITORY_ROOT / "schemas" / "tushare_alpha_feasibility_task_import.v1.json"
)
PARENT_REUSE_IMPORT_SCHEMA_PATH = (
    REPOSITORY_ROOT / "schemas" / "tushare_alpha_feasibility_task_import.v2.json"
)
CONTINUATION_CONTROL_ARTIFACTS = MappingProxyType(
    {
        "p1_5_continuation_reuse_manifest.json": (
            "tushare_alpha_feasibility_continuation_reuse_manifest.v1.json",
            "manifest_sha256",
        ),
        "p1_5_continuation_claim.json": (
            "tushare_alpha_feasibility_continuation_claim.v1.json",
            "claim_sha256",
        ),
        "p1_5_continuation_parent_reuse_stage.json": (
            "tushare_alpha_feasibility_continuation_parent_reuse_stage.v1.json",
            "stage_sha256",
        ),
        "p1_5_continuation_network_process.json": (
            "tushare_alpha_feasibility_continuation_network_process.v1.json",
            "marker_sha256",
        ),
    }
)
PIT_REPORT_SCHEMA_PATHS = MappingProxyType(
    {
        PIT_REPORT_SCHEMA_VERSION: REPOSITORY_ROOT
        / "schemas"
        / "pit_membership_coverage_report.v2.json",
        P15_PIT_REPORT_SCHEMA_VERSION: REPOSITORY_ROOT
        / "schemas"
        / "pit_membership_coverage_report.v3.json",
    }
)
PIT_MANIFEST_SCHEMA_PATHS = MappingProxyType(
    {
        PIT_MANIFEST_SCHEMA_VERSION: REPOSITORY_ROOT
        / "schemas"
        / "pit_membership_manifest.v2.json",
        P15_PIT_MANIFEST_SCHEMA_VERSION: REPOSITORY_ROOT
        / "schemas"
        / "pit_membership_manifest.v3.json",
    }
)
P14D_REQUEST_SCHEMA_PATH = (
    REPOSITORY_ROOT / "schemas" / "tushare_index_weight_value_request.v1.json"
)
P14D_PROFILE_SCHEMA_PATH = (
    REPOSITORY_ROOT / "schemas" / "tushare_index_weight_value_profile.v1.json"
)
P14D_REPLAY_SCHEMA_PATH = (
    REPOSITORY_ROOT / "schemas" / "tushare_index_weight_offline_replay.v1.json"
)
EXPERIMENT_SCHEMA_PATHS = MappingProxyType(
    {
        "technical-alpha-feasibility-experiment.v2": REPOSITORY_ROOT
        / "schemas"
        / "technical_alpha_feasibility_experiment.v2.json",
        "technical-alpha-feasibility-experiment.v3": REPOSITORY_ROOT
        / "schemas"
        / "technical_alpha_feasibility_experiment.v3.json",
    }
)

ALLOWED_ENDPOINTS = (
    "trade_cal",
    "index_weight",
    "daily",
    "adj_factor",
    "index_daily",
    "suspend_d",
)
STOCK_BASIC_STATUS = "DEFERRED_NOT_REQUIRED_FOR_ALPHA_FEASIBILITY"
STOCK_BASIC_REQUEST_COUNT = 0
SECURITY_MASTER_PIT_STATUS = "NOT_IMPLEMENTED_NOT_REQUIRED_IN_P1"
MINIMUM_VALID_CONTROLLED_SESSIONS = 121
INSUFFICIENT_HISTORY_STATUS = "ineligible_insufficient_history"
UNEXPLAINED_MARKET_DATA_GAP_STATUS = "unexplained_market_data_gap"
LOCKED_TEST_STATUS = MappingProxyType(
    {"access": "NOT_ACCESSED", "download": "NOT_DOWNLOADED", "run": "NOT_RUN"}
)

EXPECTED_FIELDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "trade_cal": ("exchange", "cal_date", "is_open", "pretrade_date"),
        "index_weight": ("index_code", "con_code", "trade_date", "weight"),
        "daily": (
            "ts_code",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "pre_close",
            "vol",
            "amount",
        ),
        "adj_factor": ("ts_code", "trade_date", "adj_factor"),
        "suspend_d": (
            "ts_code",
            "trade_date",
            "suspend_timing",
            "suspend_type",
        ),
        "index_daily": (
            "ts_code",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "pre_close",
            "change",
            "pct_chg",
            "vol",
            "amount",
        ),
    }
)

# Equality is treated as a potential upstream page truncation.  These limits
# are deliberately conservative and are not used to split or retry a request.
POTENTIAL_TRUNCATION_LIMIT: Mapping[str, int] = MappingProxyType(
    {
        "trade_cal": 10_000,
        "index_weight": 10_000,
        "daily": 6_000,
        "adj_factor": 6_000,
        "suspend_d": 5_000,
        "index_daily": 6_000,
    }
)
MINIMUM_OPEN_SESSIONS_BY_YEAR: Mapping[int, int] = MappingProxyType(
    {2017: 100, 2018: 200, 2019: 200, 2020: 200, 2021: 200, 2022: 200, 2023: 200}
)
MAXIMUM_OPEN_SESSIONS_PER_YEAR = 260

_DATE8 = re.compile(r"^[0-9]{8}$")
_DATE10 = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_MONTH = re.compile(r"^[0-9]{4}-[0-9]{2}$")
_PLAIN_DECIMAL_STRING = re.compile(r"^-?[0-9]+(?:\.[0-9]+)?$")
_TS_CODE = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")
_PIT_COMPONENT_CODE = re.compile(r"^[0-9]{6}\.(?:SH|SZ)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_TASK_ID = re.compile(r"^[a-z_]+-[0-9a-f]{64}$")
_SAFE_DATA_FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_FORBIDDEN_PARAM_KEY = re.compile(
    r"(?:token|secret|password|cookie|credential|authorization|account|order)",
    re.IGNORECASE,
)
_EMBEDDED_DATE = re.compile(
    r"(?<!\d)(20\d{2})[-/]?(0[1-9]|1[0-2])[-/]?(0[1-9]|[12]\d|3[01])(?!\d)"
)
REQUIRED_RESPONSE_ROOT_FIELDS = frozenset({"code", "msg", "data"})
_SAFE_TRANSPORT_EXTENSION_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9._:-]{0,127}$")
_SECRET_TRANSPORT_KEY = re.compile(
    r"(?:token|secret|password|passwd|cookie|authorization|api[_-]?key|access[_-]?key|credential)",
    re.IGNORECASE,
)
_SECRET_STRING_ASSIGNMENT = re.compile(
    r"(?:authorization|cookie|token|secret|password|passwd|credential|"
    r"api[_-]?key|access[_-]?key)\s*(?::|=)\s*\S+",
    re.IGNORECASE,
)
_EMAIL_ADDRESS = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?!\w)")
_CHINA_ID_NUMBER = re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)")
_PHONE_NUMBER = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
_LONG_OPAQUE_TEXT = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_-]{24,}(?![A-Za-z0-9])")
_RETRY_AFTER_TEXT = re.compile(
    r"(?:retry[-_ ]?after|重试间隔|等待)\s*(?::|=|为)?\s*([0-9]{1,3})\s*(?:s|sec|secs|second|seconds|秒)?",
    re.IGNORECASE,
)
MAXIMUM_RESPONSE_BYTES = 2 * 1024 * 1024
MAXIMUM_TRANSPORT_EXTENSIONS_BYTES = 256 * 1024
MAXIMUM_TRANSPORT_EXTENSION_DEPTH = 8
MAXIMUM_TRANSPORT_EXTENSION_FIELDS = 64
MAXIMUM_TRANSPORT_EXTENSION_ELEMENTS = 4096
MAXIMUM_TRANSPORT_EXTENSION_STRING_LENGTH = 65_536
MAXIMUM_REQUIRED_DECIMAL_ADJUSTED_EXPONENT = 999
MAXIMUM_REQUIRED_DECIMAL_SCALE = 999
P15_EXPERIMENT_SCHEMA_VERSION = "technical-alpha-feasibility-experiment.v3"
P15_WEIGHT_HARD_MIN = Decimal("99.5")
P15_WEIGHT_HARD_MAX = Decimal("100.5")
P15_WEIGHT_WARNING_MIN = Decimal("99.95")
P15_WEIGHT_WARNING_MAX = Decimal("100.05")
P15_DEFAULT_MAXIMUM_ATTEMPTS = 3
P15_REQUEST_COUNT_SEMANTICS = "conservative_durable_pre_transport_attempt_claim"
P14D_SOURCE_ARTIFACT_NAMES = (
    "request.json",
    "network_call_started.json",
    "network_response_scanned.json",
    "response.raw.json",
    "value_profile.json",
    "normalized_pit.json",
    "offline_replay.json",
)
RETRYABLE_ATTEMPT_FAILURES = frozenset({"https_transport_failed"})
BUSINESS_ERROR_CLASSIFICATIONS = frozenset(
    {
        "RATE_LIMITED",
        "PERMISSION_DENIED",
        "INVALID_PARAMETER",
        "DATA_UNAVAILABLE",
        "UPSTREAM_SERVER_ERROR",
        "ACCOUNT_OR_QUOTA_LIMIT",
        "UPSTREAM_UNKNOWN_ERROR",
    }
)
RETRYABLE_BUSINESS_ERROR_CLASSIFICATIONS = frozenset(
    {"RATE_LIMITED", "UPSTREAM_SERVER_ERROR"}
)
MINIMUM_REAL_TRANSPORT_INTERVAL_SECONDS = Decimal("12")
DEFAULT_RATE_LIMIT_RETRY_SECONDS = Decimal("65")
MAXIMUM_REASONABLE_RETRY_AFTER_SECONDS = 300
MAXIMUM_SAFE_DETAIL_PROJECTION_DEPTH = 8
MAXIMUM_SAFE_DETAIL_PROJECTION_ELEMENTS = 1024
MAXIMUM_SAFE_DETAIL_PROJECTION_BYTES = 32 * 1024
REDACTED_SECRET_DETAIL_VALUE = "[REDACTED_SECRET_FIELD]"
ADAPTER_PROTOCOL_FAILURES = frozenset(
    {
        "duplicate_json_key",
        "invalid_response_json",
        "http_status_not_success",
        "http_redirect_forbidden",
        "semantic_core_missing",
        "semantic_core_type_invalid",
        "response_body_too_large",
        "transport_extensions_too_large",
        "transport_extensions_too_deep",
        "transport_extension_secret_detected",
        "data_payload_invalid",
        "data_fields_not_array",
        "data_field_name_invalid",
        "data_duplicate_fields",
        "data_required_fields_missing",
        "data_item_width_mismatch",
        "data_required_value_invalid",
        "unknown_non_json_value",
    }
)
_CONTINUATION_NETWORK_PROCESS_AUTHORIZATIONS: set[tuple[str, str]] = set()
_CONTINUATION_NETWORK_PROCESS_AUTHORIZATIONS_LOCK = threading.Lock()
_CONTINUATION_EXECUTION_CAPABILITY = object()


class AlphaFeasibilityDataError(RuntimeError):
    """A sanitized, stable fail-closed data status.

    ``code`` never contains provider messages, response values, file contents,
    or credential-derived material, making it safe to persist in quarantine
    evidence and terminal summaries.
    """

    def __init__(
        self,
        code: str,
        *,
        stage: str = "data",
        diagnostic: Mapping[str, Any] | None = None,
    ) -> None:
        if not re.fullmatch(r"[a-z0-9_]{3,96}", str(code)):
            code = "unsafe_error_sanitized"
        self.code = str(code)
        self.stage = str(stage)
        self.diagnostic = MappingProxyType(dict(diagnostic or {}))
        super().__init__(self.code)


class AmbiguousRemoteExecutionError(AlphaFeasibilityDataError):
    """A started request lacks a durable response and must not be resent."""


@dataclass(frozen=True, slots=True)
class TushareHttpResponse:
    """Bounded HTTP result; the body remains untrusted until validated."""

    http_status: int
    body: bytes

    def __post_init__(self) -> None:
        if type(self.http_status) is not int or not 100 <= self.http_status <= 599:
            raise AlphaFeasibilityDataError("unknown_non_json_value")
        if not isinstance(self.body, bytes):
            raise AlphaFeasibilityDataError("unknown_non_json_value")


@dataclass(frozen=True, slots=True)
class SafeResponseSemantics:
    """Closed, secret-free response semantics shared by success and error paths."""

    business_code: int
    classification: str | None
    sanitized_msg: str
    msg_sha256: str
    detail_type: str | None
    safe_detail_projection: Mapping[str, Any] | None
    detail_sha256: str | None
    request_id_sha256: str | None
    raw_transport_sha256: str
    response_body_sha256: str
    response_byte_count: int
    sanitized_params: Mapping[str, str]
    requested_fields: tuple[str, ...]
    requested_at: datetime
    completed_at: datetime
    retry_after_seconds: int | None

    def __post_init__(self) -> None:
        if type(self.business_code) is not int:
            raise AlphaFeasibilityDataError("business_error_evidence_invalid")
        if self.business_code == 0:
            if self.classification is not None:
                raise AlphaFeasibilityDataError("business_error_evidence_invalid")
        elif self.classification not in BUSINESS_ERROR_CLASSIFICATIONS:
            raise AlphaFeasibilityDataError("business_error_evidence_invalid")
        if (
            type(self.sanitized_msg) is not str
            or len(self.sanitized_msg) > 1000
            or _contains_control_character(self.sanitized_msg)
            or _SECRET_STRING_ASSIGNMENT.search(self.sanitized_msg) is not None
        ):
            raise AlphaFeasibilityDataError("business_error_evidence_invalid")
        for digest in (
            self.msg_sha256,
            self.raw_transport_sha256,
            self.response_body_sha256,
        ):
            if type(digest) is not str or _SHA256.fullmatch(digest) is None:
                raise AlphaFeasibilityDataError("business_error_evidence_invalid")
        if self.raw_transport_sha256 != self.response_body_sha256:
            raise AlphaFeasibilityDataError("business_error_evidence_invalid")
        if type(self.response_byte_count) is not int or self.response_byte_count < 1:
            raise AlphaFeasibilityDataError("business_error_evidence_invalid")
        for digest in (self.detail_sha256, self.request_id_sha256):
            if digest is not None and (
                type(digest) is not str or _SHA256.fullmatch(digest) is None
            ):
                raise AlphaFeasibilityDataError("business_error_evidence_invalid")
        allowed_types = {
            "null",
            "boolean",
            "integer",
            "number",
            "string",
            "array",
            "object",
        }
        projection = self.safe_detail_projection
        if self.detail_type is None:
            if projection is not None or self.detail_sha256 is not None:
                raise AlphaFeasibilityDataError("business_error_evidence_invalid")
        elif self.detail_type not in allowed_types or not isinstance(projection, Mapping):
            raise AlphaFeasibilityDataError("business_error_evidence_invalid")
        else:
            expected_projection_fields = {
                "json_type",
                "value",
                "sanitized_text",
                "semantic_category",
            }
            if (
                set(projection) != expected_projection_fields
                or projection["json_type"] != self.detail_type
                or type(projection["sanitized_text"]) is not str
                or len(projection["sanitized_text"]) > 1000
                or _contains_control_character(projection["sanitized_text"])
                or _SECRET_STRING_ASSIGNMENT.search(projection["sanitized_text"])
                is not None
                or (
                    projection["semantic_category"] is not None
                    and projection["semantic_category"]
                    not in BUSINESS_ERROR_CLASSIFICATIONS
                )
            ):
                raise AlphaFeasibilityDataError("business_error_evidence_invalid")
            _validate_safe_detail_projection_value(projection["value"])
            if len(canonical_json_bytes(projection)) > MAXIMUM_SAFE_DETAIL_PROJECTION_BYTES:
                raise AlphaFeasibilityDataError("business_error_evidence_invalid")
            object.__setattr__(
                self, "safe_detail_projection", MappingProxyType(dict(projection))
            )
        params = dict(self.sanitized_params)
        if (
            any(type(key) is not str or type(value) is not str for key, value in params.items())
            or any(_FORBIDDEN_PARAM_KEY.search(key) for key in params)
            or list(params) != sorted(params)
        ):
            raise AlphaFeasibilityDataError("business_error_evidence_invalid")
        object.__setattr__(self, "sanitized_params", MappingProxyType(params))
        if (
            not self.requested_fields
            or any(type(field) is not str for field in self.requested_fields)
            or len(set(self.requested_fields)) != len(self.requested_fields)
        ):
            raise AlphaFeasibilityDataError("business_error_evidence_invalid")
        for timestamp in (self.requested_at, self.completed_at):
            if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
                raise AlphaFeasibilityDataError("business_error_evidence_invalid")
            if timestamp.utcoffset() != timedelta(0):
                raise AlphaFeasibilityDataError("business_error_evidence_invalid")
        if self.completed_at < self.requested_at:
            raise AlphaFeasibilityDataError("business_error_evidence_invalid")
        if self.retry_after_seconds is not None and (
            type(self.retry_after_seconds) is not int
            or not 1
            <= self.retry_after_seconds
            <= MAXIMUM_REASONABLE_RETRY_AFTER_SECONDS
        ):
            raise AlphaFeasibilityDataError("business_error_evidence_invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "business_code": self.business_code,
            "classification": self.classification,
            "sanitized_msg": self.sanitized_msg,
            "msg_sha256": self.msg_sha256,
            "detail_type": self.detail_type,
            "safe_detail_projection": (
                dict(self.safe_detail_projection)
                if self.safe_detail_projection is not None
                else None
            ),
            "detail_sha256": self.detail_sha256,
            "request_id_sha256": self.request_id_sha256,
            "raw_transport_sha256": self.raw_transport_sha256,
            "response_body_sha256": self.response_body_sha256,
            "response_byte_count": self.response_byte_count,
            "sanitized_params": dict(self.sanitized_params),
            "requested_fields": list(self.requested_fields),
            "requested_at": self.requested_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "retry_after_seconds": self.retry_after_seconds,
        }


@dataclass(frozen=True, slots=True)
class BusinessErrorRetryPolicy:
    classification: str
    maximum_additional_attempts: int
    delay_seconds: Decimal

    def __post_init__(self) -> None:
        if self.classification not in BUSINESS_ERROR_CLASSIFICATIONS:
            raise AlphaFeasibilityDataError("business_retry_policy_invalid")
        if self.maximum_additional_attempts not in {0, 1}:
            raise AlphaFeasibilityDataError("business_retry_policy_invalid")
        if not isinstance(self.delay_seconds, Decimal) or not self.delay_seconds.is_finite():
            raise AlphaFeasibilityDataError("business_retry_policy_invalid")
        if self.maximum_additional_attempts == 0 and self.delay_seconds != 0:
            raise AlphaFeasibilityDataError("business_retry_policy_invalid")
        if (
            self.maximum_additional_attempts == 1
            and self.delay_seconds < MINIMUM_REAL_TRANSPORT_INTERVAL_SECONDS
        ):
            raise AlphaFeasibilityDataError("business_retry_policy_invalid")


def _safe_response_semantics_from_mapping(
    value: Mapping[str, Any],
) -> SafeResponseSemantics:
    expected_fields = {
        "business_code",
        "classification",
        "sanitized_msg",
        "msg_sha256",
        "detail_type",
        "safe_detail_projection",
        "detail_sha256",
        "request_id_sha256",
        "raw_transport_sha256",
        "response_body_sha256",
        "response_byte_count",
        "sanitized_params",
        "requested_fields",
        "requested_at",
        "completed_at",
        "retry_after_seconds",
    }
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        raise AlphaFeasibilityDataError("business_error_evidence_invalid")
    try:
        requested_at = datetime.fromisoformat(str(value["requested_at"]))
        completed_at = datetime.fromisoformat(str(value["completed_at"]))
        semantics = SafeResponseSemantics(
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
            requested_fields=tuple(value["requested_fields"]),
            requested_at=requested_at,
            completed_at=completed_at,
            retry_after_seconds=value["retry_after_seconds"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AlphaFeasibilityDataError("business_error_evidence_invalid") from exc
    if semantics.to_dict() != dict(value):
        raise AlphaFeasibilityDataError("business_error_evidence_invalid")
    return semantics


class TushareTransport(Protocol):
    def __call__(
        self,
        *,
        endpoint: str,
        params: Mapping[str, str],
        fields: Sequence[str],
        token: str,
        timeout_seconds: int,
        maximum_response_bytes: int,
    ) -> TushareHttpResponse | bytes: ...


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AlphaFeasibilityDataError("duplicate_json_key")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise AlphaFeasibilityDataError("nonfinite_json_number")


def _parse_json_integer(value: str) -> int | Decimal:
    # The standard int parser collapses the valid JSON token ``-0`` to 0 and
    # loses the sign before field-level validation can reject signed zero.
    return Decimal(value) if value == "-0" else int(value)


def strict_json_loads(raw: bytes | str, *, label: str = "json") -> Any:
    try:
        return json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_int=_parse_json_integer,
            parse_float=Decimal,
            parse_constant=_reject_nonfinite,
        )
    except AlphaFeasibilityDataError:
        raise
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        ValueError,
    ) as exc:
        raise AlphaFeasibilityDataError(f"invalid_{label}_json") from exc


def _json_safe(value: Any) -> Any:
    if value is None or type(value) in {str, bool, int}:
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise AlphaFeasibilityDataError("nonfinite_decimal")
        return str(value)
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise AlphaFeasibilityDataError("non_string_json_key")
        return {key: _json_safe(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AlphaFeasibilityDataError("nonfinite_float")
        # Floats are not accepted in persisted evidence.  Their binary
        # representation is not an upstream decimal contract.
        raise AlphaFeasibilityDataError("float_not_allowed_in_evidence")
    raise AlphaFeasibilityDataError("unsupported_json_value")


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            _json_safe(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _contains_control_character(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _contains_unicode_surrogate(value: Any) -> bool:
    """Reject lone UTF-16 surrogate code points accepted by Python's JSON parser."""

    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            for key, item in current.items():
                pending.extend((key, item))
        elif isinstance(current, (list, tuple)):
            pending.extend(current)
        elif type(current) is str and any(
            unicodedata.category(character) == "Cs" for character in current
        ):
            return True
    return False


def _transport_json_type(value: Any) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) is int:
        return "integer"
    if isinstance(value, Decimal):
        return "number"
    if type(value) is str:
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    raise AlphaFeasibilityDataError("unknown_non_json_value")


def _canonical_decimal_number(value: Decimal) -> str:
    if not value.is_finite():
        raise AlphaFeasibilityDataError("unknown_non_json_value")
    if value == 0:
        return "0"
    sign, digits_tuple, exponent = value.as_tuple()
    digits = "".join(str(digit) for digit in digits_tuple)
    while len(digits) > 1 and digits.endswith("0"):
        digits = digits[:-1]
        exponent += 1
    if len(digits) > MAXIMUM_TRANSPORT_EXTENSION_STRING_LENGTH:
        raise AlphaFeasibilityDataError("transport_extensions_too_large")
    prefix = "-" if sign else ""
    return prefix + digits + (f"e{exponent}" if exponent else "")


def _canonical_transport_json_bytes(value: Any) -> bytes:
    """Canonicalize untrusted JSON while preserving number/string identity."""

    value_type = _transport_json_type(value)
    if value_type == "null":
        return b"null"
    if value_type == "boolean":
        return b"true" if value else b"false"
    if value_type == "integer":
        return _canonical_decimal_number(Decimal(value)).encode("ascii")
    if value_type == "number":
        return _canonical_decimal_number(value).encode("ascii")
    if value_type == "string":
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if value_type == "array":
        return b"[" + b",".join(_canonical_transport_json_bytes(item) for item in value) + b"]"
    if any(type(key) is not str for key in value):
        raise AlphaFeasibilityDataError("unknown_non_json_value")
    items = []
    for key in sorted(value):
        if type(key) is not str:
            raise AlphaFeasibilityDataError("unknown_non_json_value")
        encoded_key = json.dumps(key, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        items.append(encoded_key + b":" + _canonical_transport_json_bytes(value[key]))
    return b"{" + b",".join(items) + b"}"


@dataclass(frozen=True, slots=True)
class TransportExtensionsMetadata:
    field_names: tuple[str, ...]
    type_by_field: Mapping[str, str]
    value_sha256_by_field: Mapping[str, str]
    sha256: str
    byte_count: int


def _validate_transport_extensions(
    extensions: Mapping[str, Any],
    *,
    token: str | None,
) -> TransportExtensionsMetadata:
    if not isinstance(extensions, Mapping):
        raise AlphaFeasibilityDataError("unknown_non_json_value")
    if len(extensions) > MAXIMUM_TRANSPORT_EXTENSION_FIELDS:
        raise AlphaFeasibilityDataError("transport_extensions_too_large")

    element_count = 0

    def validate_value(value: Any, *, depth: int) -> None:
        nonlocal element_count
        element_count += 1
        if element_count > MAXIMUM_TRANSPORT_EXTENSION_ELEMENTS:
            raise AlphaFeasibilityDataError("transport_extensions_too_large")
        value_type = _transport_json_type(value)
        if value_type == "string":
            if (
                len(value) > MAXIMUM_TRANSPORT_EXTENSION_STRING_LENGTH
                or len(value.encode("utf-8")) > MAXIMUM_TRANSPORT_EXTENSION_STRING_LENGTH
                or _contains_control_character(value)
            ):
                raise AlphaFeasibilityDataError("unknown_non_json_value")
            if token and token in value:
                raise AlphaFeasibilityDataError("transport_extension_secret_detected")
            return
        if value_type in {"null", "boolean", "integer", "number"}:
            return
        if depth > MAXIMUM_TRANSPORT_EXTENSION_DEPTH:
            raise AlphaFeasibilityDataError("transport_extensions_too_deep")
        if value_type == "array":
            for item in value:
                validate_value(item, depth=depth + 1)
            return
        for key, item in value.items():
            if (
                type(key) is not str
                or not key
                or len(key) > MAXIMUM_TRANSPORT_EXTENSION_STRING_LENGTH
                or len(key.encode("utf-8")) > MAXIMUM_TRANSPORT_EXTENSION_STRING_LENGTH
                or _contains_control_character(key)
            ):
                raise AlphaFeasibilityDataError("unknown_non_json_value")
            if _SECRET_TRANSPORT_KEY.search(key) is not None:
                raise AlphaFeasibilityDataError("transport_extension_secret_detected")
            validate_value(item, depth=depth + 1)

    names = tuple(sorted(extensions))
    type_by_field: dict[str, str] = {}
    value_sha256_by_field: dict[str, str] = {}
    for field in names:
        if (
            type(field) is not str
            or _SAFE_TRANSPORT_EXTENSION_FIELD.fullmatch(field) is None
            or _contains_control_character(field)
            or field in REQUIRED_RESPONSE_ROOT_FIELDS
        ):
            raise AlphaFeasibilityDataError("unknown_non_json_value")
        if _SECRET_TRANSPORT_KEY.search(field) is not None:
            raise AlphaFeasibilityDataError("transport_extension_secret_detected")
        value = extensions[field]
        validate_value(value, depth=1)
        canonical_value = _canonical_transport_json_bytes(value)
        type_by_field[field] = _transport_json_type(value)
        value_sha256_by_field[field] = hashlib.sha256(canonical_value).hexdigest()

    canonical_extensions = _canonical_transport_json_bytes(extensions)
    if len(canonical_extensions) > MAXIMUM_TRANSPORT_EXTENSIONS_BYTES:
        raise AlphaFeasibilityDataError("transport_extensions_too_large")
    return TransportExtensionsMetadata(
        field_names=names,
        type_by_field=MappingProxyType(type_by_field),
        value_sha256_by_field=MappingProxyType(value_sha256_by_field),
        sha256=hashlib.sha256(canonical_extensions).hexdigest(),
        byte_count=len(canonical_extensions),
    )


def _parse_date(value: Any, label: str) -> date:
    if type(value) is not str:
        raise AlphaFeasibilityDataError(f"invalid_{label}")
    try:
        if _DATE8.fullmatch(value):
            parsed = datetime.strptime(value, "%Y%m%d").date()
        elif _DATE10.fullmatch(value):
            parsed = date.fromisoformat(value)
        else:
            raise ValueError
    except ValueError as exc:
        raise AlphaFeasibilityDataError(f"invalid_{label}") from exc
    return parsed


def _compact(value: date) -> str:
    return value.strftime("%Y%m%d")


def _iso(value: date) -> str:
    return value.isoformat()


def _month_sequence(first: str, last: str) -> tuple[str, ...]:
    if not _MONTH.fullmatch(first) or not _MONTH.fullmatch(last):
        raise AlphaFeasibilityDataError("invalid_pit_month_boundary")
    first_date = date.fromisoformat(first + "-01")
    last_date = date.fromisoformat(last + "-01")
    if first_date > last_date:
        raise AlphaFeasibilityDataError("reversed_pit_month_boundary")
    months: list[str] = []
    cursor = first_date
    while cursor <= last_date:
        months.append(cursor.strftime("%Y-%m"))
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    return tuple(months)


def _month_bounds(month: str) -> tuple[date, date]:
    start = date.fromisoformat(month + "-01")
    end = date(start.year, start.month, calendar.monthrange(start.year, start.month)[1])
    return start, end


def _scan_date_literals(value: Any) -> None:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            pending.extend(current.values())
        elif isinstance(current, (list, tuple)):
            pending.extend(current)
        elif type(current) is str and (
            _DATE8.fullmatch(current) or _DATE10.fullmatch(current)
        ):
            if _parse_date(current, "config_date") > ABSOLUTE_CUTOFF:
                raise AlphaFeasibilityDataError("post_cutoff_config_date")


def _reject_embedded_post_cutoff_date(value: Any, code: str) -> None:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            for key, item in current.items():
                pending.extend((key, item))
        elif isinstance(current, (list, tuple)):
            pending.extend(current)
        elif type(current) is str:
            for match in _EMBEDDED_DATE.finditer(current):
                try:
                    parsed = date(
                        int(match.group(1)),
                        int(match.group(2)),
                        int(match.group(3)),
                    )
                except ValueError:
                    continue
                if parsed > ABSOLUTE_CUTOFF:
                    raise AlphaFeasibilityDataError(code)


def _reject_p15_full_raw_tree(
    value: Any,
    *,
    token: str | None,
) -> None:
    """Reject secrets, controls, non-finite values and post-cutoff dates.

    This deliberately applies only to the P1.5 full-transport persistence
    boundary.  V2 continues to validate its normalized semantic projection
    under the pre-existing rules.
    """

    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            for key, item in current.items():
                if type(key) is not str:
                    raise AlphaFeasibilityDataError("unknown_non_json_value")
                if _contains_control_character(key):
                    raise AlphaFeasibilityDataError("p15_full_raw_control_character_detected")
                if _SECRET_TRANSPORT_KEY.search(key) is not None:
                    raise AlphaFeasibilityDataError("transport_extension_secret_detected")
                pending.extend((key, item))
            continue
        if isinstance(current, (list, tuple)):
            pending.extend(current)
            continue
        if type(current) is str:
            if _contains_control_character(current):
                raise AlphaFeasibilityDataError("p15_full_raw_control_character_detected")
            if token and token in current:
                raise AlphaFeasibilityDataError("transport_extension_secret_detected")
            if _SECRET_STRING_ASSIGNMENT.search(current) is not None:
                raise AlphaFeasibilityDataError("transport_extension_secret_detected")
            _reject_embedded_post_cutoff_date(current, "post_cutoff_response_date")
            continue
        if isinstance(current, Decimal):
            if not current.is_finite():
                raise AlphaFeasibilityDataError("nonfinite_json_number")
            if (
                current == current.to_integral_value()
                and Decimal("10000000") <= current <= Decimal("99999999")
            ):
                candidate = str(int(current))
            else:
                continue
        elif type(current) is int and 10_000_000 <= current <= 99_999_999:
            candidate = str(current)
        elif isinstance(current, float):
            if not math.isfinite(current):
                raise AlphaFeasibilityDataError("nonfinite_json_number")
            continue
        else:
            continue
        try:
            parsed = datetime.strptime(candidate, "%Y%m%d").date()
        except ValueError:
            continue
        if parsed > ABSOLUTE_CUTOFF:
            raise AlphaFeasibilityDataError("post_cutoff_response_date")


_BUSINESS_ERROR_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "RATE_LIMITED",
        (
            "rate limit",
            "rate",
            "too many requests",
            "too many",
            "frequency",
            "每分钟",
            "访问频率",
            "频率",
            "频率过高",
            "访问过快",
            "请求过于频繁",
            "限频",
        ),
    ),
    (
        "PERMISSION_DENIED",
        (
            "permission denied",
            "no permission",
            "no access",
            "not authorized",
            "没有访问该接口的权限",
            "没有权限",
            "无权限",
            "权限不足",
            "权限",
            "积分不足",
        ),
    ),
    (
        "INVALID_PARAMETER",
        (
            "invalid parameter",
            "invalid argument",
            "missing parameter",
            "invalid date format",
            "参数错误",
            "参数无效",
            "参数不正确",
            "参数缺失",
            "缺少参数",
            "参数",
            "日期格式",
            "不包含字段",
            "没有接口",
            "接口不存在",
        ),
    ),
    (
        "DATA_UNAVAILABLE",
        (
            "data unavailable",
            "no data available",
            "data not available",
            "暂无数据",
            "无可用数据",
            "无数据",
            "该日期无数据",
            "历史区间不支持",
            "数据不存在",
            "数据暂不可用",
        ),
    ),
    (
        "UPSTREAM_SERVER_ERROR",
        (
            "internal server",
            "server error",
            "server",
            "service unavailable",
            "bad gateway",
            "gateway timeout",
            "系统错误",
            "系统内部错误",
            "内部错误",
            "服务器内部",
            "服务异常",
            "服务不可用",
        ),
    ),
    (
        "ACCOUNT_OR_QUOTA_LIMIT",
        (
            "token invalid",
            "invalid token",
            "token无效",
            "token 无效",
            "token expired",
            "invalid credential",
            "account disabled",
            "account invalid",
            "账号异常",
            "账户异常",
            "账户不存在",
            "账号被禁用",
            "quota",
            "配额",
            "额度",
            "额度不足",
            "额度用尽",
            "总量",
            "总量限制",
            "总量已达",
            "请求总量",
            "次数已用完",
            "次数用完",
        ),
    ),
)

_BUSINESS_CLASSIFICATION_TO_FAILURE_CODE = MappingProxyType(
    {
        "RATE_LIMITED": "upstream_rate_limit_error",
        "PERMISSION_DENIED": "upstream_permission_error",
        "INVALID_PARAMETER": "upstream_invalid_parameter_error",
        "DATA_UNAVAILABLE": "upstream_data_unavailable_error",
        "UPSTREAM_SERVER_ERROR": "upstream_server_internal_error",
        "ACCOUNT_OR_QUOTA_LIMIT": "upstream_authentication_account_error",
        "UPSTREAM_UNKNOWN_ERROR": "upstream_unknown_error",
    }
)

_BUSINESS_CLASSIFICATION_TO_LEGACY_CATEGORY = MappingProxyType(
    {
        "RATE_LIMITED": "rate_limit",
        "PERMISSION_DENIED": "permission",
        "INVALID_PARAMETER": "invalid_parameter",
        "DATA_UNAVAILABLE": "data_unavailable",
        "UPSTREAM_SERVER_ERROR": "server_internal",
        "ACCOUNT_OR_QUOTA_LIMIT": "authentication_account",
        "UPSTREAM_UNKNOWN_ERROR": "unknown",
    }
)


def _sanitize_provider_text(
    value: Any,
    *,
    token: str | None,
    request_id: str | None,
) -> str:
    if type(value) is not str:
        return ""
    text = unicodedata.normalize("NFKC", value)
    for secret in (token, request_id):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = _SECRET_STRING_ASSIGNMENT.sub("[REDACTED_SECRET_ASSIGNMENT]", text)
    text = _EMAIL_ADDRESS.sub("[REDACTED_EMAIL]", text)
    text = _CHINA_ID_NUMBER.sub("[REDACTED_ID]", text)
    text = _PHONE_NUMBER.sub("[REDACTED_PHONE]", text)
    text = _LONG_OPAQUE_TEXT.sub("[REDACTED_OPAQUE]", text)
    text = _redact_post_cutoff_dates(text)
    text = " ".join(text.split())
    return text[:1000]


def _redact_post_cutoff_dates(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        try:
            parsed = date(
                int(match.group(1)), int(match.group(2)), int(match.group(3))
            )
        except ValueError:
            return match.group(0)
        return (
            "[REDACTED_POST_CUTOFF_DATE]"
            if parsed > ABSOLUTE_CUTOFF
            else match.group(0)
        )

    return _EMBEDDED_DATE.sub(replace, value)


def _safe_detail_projection_value(
    value: Any,
    *,
    token: str | None,
    request_id: str | None,
) -> Any:
    element_count = 0

    def project(current: Any, depth: int) -> Any:
        nonlocal element_count
        if depth > MAXIMUM_SAFE_DETAIL_PROJECTION_DEPTH:
            raise AlphaFeasibilityDataError("safe_detail_projection_too_deep")
        element_count += 1
        if element_count > MAXIMUM_SAFE_DETAIL_PROJECTION_ELEMENTS:
            raise AlphaFeasibilityDataError("safe_detail_projection_too_large")
        if isinstance(current, Mapping):
            result: dict[str, Any] = {}
            redacted_secret_field = False
            for key in sorted(current):
                if type(key) is not str or _contains_control_character(key):
                    raise AlphaFeasibilityDataError(
                        "safe_detail_projection_invalid"
                    )
                if _SECRET_TRANSPORT_KEY.search(key) is not None:
                    redacted_secret_field = True
                    continue
                safe_key = _sanitize_provider_text(
                    key, token=token, request_id=request_id
                )
                if not safe_key or safe_key in result:
                    raise AlphaFeasibilityDataError(
                        "safe_detail_projection_invalid"
                    )
                result[safe_key] = project(current[key], depth + 1)
            if redacted_secret_field:
                result["[REDACTED_SECRET_FIELDS]"] = REDACTED_SECRET_DETAIL_VALUE
            return result
        if isinstance(current, (list, tuple)):
            return [project(item, depth + 1) for item in current]
        if type(current) is str:
            return _sanitize_provider_text(
                current, token=token, request_id=request_id
            )
        if current is None or type(current) in {bool, int}:
            return current
        if isinstance(current, Decimal):
            if not current.is_finite():
                raise AlphaFeasibilityDataError("safe_detail_projection_invalid")
            return format(current, "f")
        raise AlphaFeasibilityDataError("safe_detail_projection_invalid")

    projected = project(value, 0)
    if len(canonical_json_bytes(projected)) > MAXIMUM_SAFE_DETAIL_PROJECTION_BYTES:
        raise AlphaFeasibilityDataError("safe_detail_projection_too_large")
    return projected


def _validate_safe_detail_projection_value(value: Any) -> None:
    element_count = 0

    def validate(current: Any, depth: int) -> None:
        nonlocal element_count
        if depth > MAXIMUM_SAFE_DETAIL_PROJECTION_DEPTH:
            raise AlphaFeasibilityDataError("business_error_evidence_invalid")
        element_count += 1
        if element_count > MAXIMUM_SAFE_DETAIL_PROJECTION_ELEMENTS:
            raise AlphaFeasibilityDataError("business_error_evidence_invalid")
        if isinstance(current, Mapping):
            for key, item in current.items():
                if (
                    type(key) is not str
                    or _contains_control_character(key)
                    or (
                        key != "[REDACTED_SECRET_FIELDS]"
                        and _SECRET_TRANSPORT_KEY.search(key) is not None
                    )
                ):
                    raise AlphaFeasibilityDataError(
                        "business_error_evidence_invalid"
                    )
                validate(item, depth + 1)
            return
        if isinstance(current, (list, tuple)):
            for item in current:
                validate(item, depth + 1)
            return
        if type(current) is str:
            if (
                _contains_control_character(current)
                or _SECRET_STRING_ASSIGNMENT.search(current) is not None
            ):
                raise AlphaFeasibilityDataError(
                    "business_error_evidence_invalid"
                )
            _reject_embedded_post_cutoff_date(
                current, "business_error_evidence_invalid"
            )
            return
        if current is None or type(current) in {bool, int}:
            return
        raise AlphaFeasibilityDataError("business_error_evidence_invalid")

    validate(value, 0)


def _detail_semantic_text(
    value: Any,
    *,
    token: str | None,
    request_id: str | None,
) -> str:
    fragments: list[str] = []
    pending = [value]
    while pending and sum(len(item) for item in fragments) < 4000:
        current = pending.pop()
        if isinstance(current, Mapping):
            for key in sorted(current, reverse=True):
                fragments.append(str(key))
                pending.append(current[key])
        elif isinstance(current, (list, tuple)):
            pending.extend(reversed(current))
        elif type(current) is str:
            fragments.append(current)
        elif type(current) is int or isinstance(current, Decimal):
            fragments.append(str(current))
    return _sanitize_provider_text(
        " ".join(fragments), token=token, request_id=request_id
    )


def _classify_business_error_semantics(
    sanitized_msg: str,
    sanitized_detail: str,
) -> str:
    """Classify only provider text semantics; the numeric code is never consulted."""

    text = f"{sanitized_msg}\n{sanitized_detail}".casefold()
    for category, markers in _BUSINESS_ERROR_MARKERS:
        if any(_semantic_marker_present(text, marker) for marker in markers):
            return category
    return "UPSTREAM_UNKNOWN_ERROR"


def _semantic_marker_present(text: str, marker: str) -> bool:
    if marker.isascii() and re.fullmatch(r"[a-z ]+", marker) is not None:
        return re.search(rf"(?<![a-z]){re.escape(marker)}(?![a-z])", text) is not None
    return marker in text


def _reasonable_retry_after_seconds(value: Any) -> int | None:
    if type(value) is int:
        parsed = value
    elif type(value) is str and re.fullmatch(r"[0-9]{1,3}", value.strip()):
        parsed = int(value.strip())
    else:
        return None
    if 1 <= parsed <= MAXIMUM_REASONABLE_RETRY_AFTER_SECONDS:
        return parsed
    return None


def _extract_retry_after_seconds(detail: Any, semantic_text: str) -> int | None:
    pending = [detail]
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            for key in sorted(current, reverse=True):
                value = current[key]
                if str(key).casefold() in {
                    "retry_after",
                    "retry_after_seconds",
                    "retry-after",
                }:
                    parsed = _reasonable_retry_after_seconds(value)
                    if parsed is not None:
                        return parsed
                pending.append(value)
        elif isinstance(current, (list, tuple)):
            pending.extend(reversed(current))
    match = _RETRY_AFTER_TEXT.search(semantic_text)
    if match is None:
        return None
    return _reasonable_retry_after_seconds(match.group(1))


def _utc_timestamp(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise AlphaFeasibilityDataError(f"invalid_{label}")
    return value.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _path_is_within_data_tmp(path: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(DATA_TMP_ROOT.resolve(strict=False))
    except ValueError:
        return False
    return True


def extract_safe_response_semantics(
    raw: bytes,
    *,
    task: "CollectionTask",
    token: str | None,
    requested_at: datetime,
    completed_at: datetime,
) -> SafeResponseSemantics:
    """Extract a closed safe summary after the complete P1.5 tree scan.

    The function performs no I/O.  It is intentionally usable for both
    successful ``code == 0`` responses and non-zero business responses.
    """

    _validate_collection_task_contract(task)
    if not isinstance(raw, bytes) or len(raw) > MAXIMUM_RESPONSE_BYTES:
        raise AlphaFeasibilityDataError("response_body_too_large")
    if token and token.encode("utf-8") in raw:
        raise AlphaFeasibilityDataError("transport_extension_secret_detected")
    root = strict_json_loads(raw, label="response")
    if not isinstance(root, Mapping):
        raise AlphaFeasibilityDataError("semantic_core_type_invalid")
    if _contains_unicode_surrogate(root):
        raise AlphaFeasibilityDataError("unknown_non_json_value")
    if token and _contains_decoded_text(root, token):
        raise AlphaFeasibilityDataError("transport_extension_secret_detected")
    _reject_p15_full_raw_tree(root, token=token)
    if not REQUIRED_RESPONSE_ROOT_FIELDS.issubset(root):
        raise AlphaFeasibilityDataError("semantic_core_missing")
    code = root["code"]
    msg = root["msg"]
    if type(code) is not int or (msg is not None and type(msg) is not str):
        raise AlphaFeasibilityDataError("semantic_core_type_invalid")
    extensions = {
        key: value for key, value in root.items() if key not in REQUIRED_RESPONSE_ROOT_FIELDS
    }
    extension_metadata = _validate_transport_extensions(extensions, token=token)
    request_id_value = extensions.get("request_id")
    request_id = request_id_value if type(request_id_value) is str else None
    detail_present = "detail" in extensions
    detail = extensions.get("detail")
    sanitized_msg = _sanitize_provider_text(msg, token=token, request_id=request_id)
    sanitized_detail = (
        _detail_semantic_text(detail, token=token, request_id=request_id)
        if detail_present
        else ""
    )
    classification = (
        None
        if code == 0
        else _classify_business_error_semantics(sanitized_msg, sanitized_detail)
    )
    detail_category = (
        None
        if code == 0 or not detail_present
        else _classify_business_error_semantics("", sanitized_detail)
    )
    if detail_category == "UPSTREAM_UNKNOWN_ERROR":
        detail_category = None
    retry_after_seconds = (
        _extract_retry_after_seconds(detail, f"{sanitized_msg}\n{sanitized_detail}")
        if classification == "RATE_LIMITED"
        else None
    )
    requested_utc = _utc_timestamp(requested_at, "requested_at")
    completed_utc = _utc_timestamp(completed_at, "completed_at")
    if completed_utc < requested_utc:
        raise AlphaFeasibilityDataError("invalid_completed_at")
    return SafeResponseSemantics(
        business_code=code,
        classification=classification,
        sanitized_msg=sanitized_msg,
        msg_sha256=hashlib.sha256(_canonical_transport_json_bytes(msg)).hexdigest(),
        detail_type=(
            extension_metadata.type_by_field["detail"] if detail_present else None
        ),
        safe_detail_projection=(
            {
                "json_type": extension_metadata.type_by_field["detail"],
                "value": _safe_detail_projection_value(
                    detail, token=token, request_id=request_id
                ),
                "sanitized_text": sanitized_detail,
                "semantic_category": detail_category,
            }
            if detail_present
            else None
        ),
        detail_sha256=(
            extension_metadata.value_sha256_by_field["detail"]
            if detail_present
            else None
        ),
        request_id_sha256=extension_metadata.value_sha256_by_field.get("request_id"),
        raw_transport_sha256=hashlib.sha256(raw).hexdigest(),
        response_body_sha256=hashlib.sha256(raw).hexdigest(),
        response_byte_count=len(raw),
        sanitized_params=MappingProxyType(dict(sorted(task.params.items()))),
        requested_fields=tuple(task.fields),
        requested_at=requested_utc,
        completed_at=completed_utc,
        retry_after_seconds=retry_after_seconds,
    )


def business_error_retry_policy(
    classification: str,
    *,
    retry_after_seconds: int | None,
    minimum_request_interval_seconds: Decimal = MINIMUM_REAL_TRANSPORT_INTERVAL_SECONDS,
) -> BusinessErrorRetryPolicy:
    if classification not in BUSINESS_ERROR_CLASSIFICATIONS:
        raise AlphaFeasibilityDataError("business_retry_policy_invalid")
    if (
        not isinstance(minimum_request_interval_seconds, Decimal)
        or not minimum_request_interval_seconds.is_finite()
        or minimum_request_interval_seconds < MINIMUM_REAL_TRANSPORT_INTERVAL_SECONDS
    ):
        raise AlphaFeasibilityDataError("business_retry_interval_too_short")
    if classification not in RETRYABLE_BUSINESS_ERROR_CLASSIFICATIONS:
        return BusinessErrorRetryPolicy(classification, 0, Decimal("0"))
    if classification == "RATE_LIMITED":
        accepted_retry_after = _reasonable_retry_after_seconds(retry_after_seconds)
        delay = (
            Decimal(accepted_retry_after)
            if accepted_retry_after is not None
            else DEFAULT_RATE_LIMIT_RETRY_SECONDS
        )
        delay = max(delay, minimum_request_interval_seconds)
    else:
        delay = minimum_request_interval_seconds
    return BusinessErrorRetryPolicy(classification, 1, delay)


def _contains_decoded_text(value: Any, needle: str) -> bool:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            for key, item in current.items():
                pending.extend((key, item))
        elif isinstance(current, (list, tuple)):
            pending.extend(current)
        elif type(current) is str and needle in current:
            return True
    return False


def _reject_response_post_cutoff_dates(
    task: "CollectionTask",
    root: Mapping[str, Any],
) -> None:
    """Scan every successful in-scope data payload before persistence."""

    _reject_embedded_post_cutoff_date(
        root["data"],
        "post_cutoff_response_date",
    )


def _require_mapping(value: Any, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AlphaFeasibilityDataError(code)
    return value


def _require_exact_fields(value: Any, endpoint: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(type(item) is not str for item in value):
        raise AlphaFeasibilityDataError("invalid_config_fields")
    fields = tuple(value)
    if fields != EXPECTED_FIELDS[endpoint] or len(set(fields)) != len(fields):
        raise AlphaFeasibilityDataError("config_fields_differ_from_contract")
    return fields


def _verify_frozen_implementation(config: Mapping[str, Any], repository_root: Path) -> None:
    frozen = _require_mapping(
        config.get("frozen_implementation"), "missing_frozen_implementation"
    )
    pairs = (
        ("alpha_policy_path", "alpha_policy_sha256"),
        ("alpha_source_path", "alpha_source_sha256"),
        ("ranker_source_path", "ranker_source_sha256"),
        ("exposure_source_path", "exposure_source_sha256"),
    )
    for path_key, hash_key in pairs:
        relative = frozen.get(path_key)
        expected = frozen.get(hash_key)
        if (
            type(relative) is not str
            or type(expected) is not str
            or _SHA256.fullmatch(expected) is None
        ):
            raise AlphaFeasibilityDataError("invalid_frozen_implementation_binding")
        path = (repository_root / relative).resolve()
        try:
            path.relative_to(repository_root.resolve())
            raw = path.read_bytes()
        except (ValueError, OSError) as exc:
            raise AlphaFeasibilityDataError("frozen_implementation_unavailable") from exc
        if hashlib.sha256(raw).hexdigest() != expected:
            raise AlphaFeasibilityDataError("frozen_implementation_hash_mismatch")


def validate_experiment_config(
    config: Mapping[str, Any], *, repository_root: Path | str = REPOSITORY_ROOT
) -> Mapping[str, Any]:
    """Validate the complete immutable experiment boundary without a token."""

    if not isinstance(config, Mapping):
        raise AlphaFeasibilityDataError("config_root_not_object")
    _scan_date_literals(config)
    _reject_embedded_post_cutoff_date(config, "post_cutoff_config_date")
    experiment_schema = config.get("schema_version")
    if experiment_schema not in EXPERIMENT_SCHEMA_PATHS:
        raise AlphaFeasibilityDataError("unexpected_experiment_schema")
    try:
        validate_json_schema(config, EXPERIMENT_SCHEMA_PATHS[experiment_schema])
    except SchemaValidationError as exc:
        raise AlphaFeasibilityDataError("experiment_schema_invalid") from exc
    p15 = experiment_schema == P15_EXPERIMENT_SCHEMA_VERSION
    if config.get("research_status") != "research_alpha_feasibility_only":
        raise AlphaFeasibilityDataError("unexpected_research_status")
    source = _require_mapping(config.get("source"), "missing_source_config")
    if source.get("transport_target") != OFFICIAL_API_URL:
        raise AlphaFeasibilityDataError("unsafe_transport_target")
    if source.get("provider") != "tushare_standard_non_vip":
        raise AlphaFeasibilityDataError("unsafe_provider")
    if source.get("token_environment_variable") != "TUSHARE_TOKEN":
        raise AlphaFeasibilityDataError("token_environment_variable_changed")
    interval_value, _interval_text = _decimal(
        source.get("minimum_request_interval_seconds"),
        "minimum_request_interval_seconds",
        minimum=Decimal("0"),
    )
    if interval_value != Decimal("0.13"):
        raise AlphaFeasibilityDataError("request_interval_differs_from_contract")
    endpoints = source.get("allowed_endpoints")
    if (
        not isinstance(endpoints, list)
        or len(endpoints) != len(ALLOWED_ENDPOINTS)
        or set(endpoints) != set(ALLOWED_ENDPOINTS)
        or len(set(endpoints)) != len(endpoints)
    ):
        raise AlphaFeasibilityDataError("endpoint_allowlist_differs_from_contract")
    if any(endpoint.endswith("_vip") for endpoint in endpoints):
        raise AlphaFeasibilityDataError("vip_endpoint_forbidden")
    if source.get("forbidden_endpoint_suffix") != "_vip":
        raise AlphaFeasibilityDataError("vip_guard_missing")
    if source.get("redirects_allowed") is not False:
        raise AlphaFeasibilityDataError("redirects_must_be_disabled")
    if source.get("automatic_retries") != 0:
        raise AlphaFeasibilityDataError("automatic_retries_must_be_zero")
    if p15:
        if (
            source.get("interrupted_fingerprint_recovery")
            != "create_only_attempt_journal"
            or source.get("terminal_quarantine_retry_forbidden") is not True
            or source.get("complete_raw_transport_persistence") is not True
            or type(source.get("maximum_attempts_per_fingerprint")) is not int
            or not 1 <= source["maximum_attempts_per_fingerprint"] <= 10
            or source.get("request_count_semantics") != P15_REQUEST_COUNT_SEMANTICS
        ):
            raise AlphaFeasibilityDataError("p15_attempt_contract_invalid")
        _p14d_bundle_binding(config)
    if type(source.get("request_timeout_seconds")) is not int or not (
        1 <= source["request_timeout_seconds"] <= 60
    ):
        raise AlphaFeasibilityDataError("unsafe_request_timeout")
    if type(source.get("maximum_response_bytes")) is not int or not (
        1_024 <= source["maximum_response_bytes"] <= MAXIMUM_RESPONSE_BYTES
    ):
        raise AlphaFeasibilityDataError("unsafe_response_limit")
    for key in (
        "token_persistence_forbidden",
        "field_level_fallback_forbidden",
        "baostock_field_level_fallback_forbidden",
    ):
        if source.get(key) is not True:
            raise AlphaFeasibilityDataError("source_safety_guard_missing")

    dates = _require_mapping(config.get("dates"), "missing_dates_config")
    required_dates = {
        key: _parse_date(dates.get(key), key)
        for key in (
            "signal_warmup_start",
            "development_start",
            "development_end",
            "validation_start",
            "validation_end",
            "absolute_request_and_consumer_cutoff",
        )
    }
    exact_dates = {
        "signal_warmup_start": date(2017, 7, 1),
        "development_start": date(2018, 1, 1),
        "development_end": date(2022, 12, 31),
        "validation_start": date(2023, 1, 1),
        "validation_end": date(2023, 12, 31),
        "absolute_request_and_consumer_cutoff": date(2023, 12, 31),
    }
    if required_dates != exact_dates:
        raise AlphaFeasibilityDataError("experiment_dates_differ_from_frozen_contract")
    if not (
        required_dates["signal_warmup_start"]
        <= required_dates["development_start"]
        <= required_dates["development_end"]
        < required_dates["validation_start"]
        <= required_dates["validation_end"]
        == required_dates["absolute_request_and_consumer_cutoff"]
        == ABSOLUTE_CUTOFF
    ):
        raise AlphaFeasibilityDataError("experiment_date_partition_invalid")
    if dates.get("terminal_session_has_no_cross_cutoff_next_session") is not True:
        raise AlphaFeasibilityDataError("cross_cutoff_session_guard_missing")

    index = _require_mapping(config.get("index"), "missing_index_config")
    months = _month_sequence(index.get("pit_first_month"), index.get("pit_last_month"))
    if months != _month_sequence("2017-12", "2023-12") or len(months) != 73:
        raise AlphaFeasibilityDataError("pit_month_plan_must_equal_73")
    if index.get("index_code") != "000906.SH":
        raise AlphaFeasibilityDataError("unexpected_index_code")
    if index.get("one_request_per_calendar_month") is not True:
        raise AlphaFeasibilityDataError("monthly_request_guard_missing")
    if index.get("future_snapshot_backfill_forbidden") is not True:
        raise AlphaFeasibilityDataError("future_snapshot_guard_missing")
    if index.get("expected_component_count") != 800:
        raise AlphaFeasibilityDataError("unexpected_component_count")
    if index.get("minimum_weight_decimal_places") != 0:
        raise AlphaFeasibilityDataError("unexpected_weight_precision")
    if p15:
        expected_weight_policy = {
            "weight_sum_hard_min": "99.5",
            "weight_sum_hard_max": "100.5",
            "weight_sum_warning_min": "99.95",
            "weight_sum_warning_max": "100.05",
        }
        if any(index.get(key) != value for key, value in expected_weight_policy.items()):
            raise AlphaFeasibilityDataError("p15_weight_sum_policy_invalid")
    evidence_hashes = index.get("controlled_adjustment_evidence_sha256s", [])
    if (
        not isinstance(evidence_hashes, list)
        or len(evidence_hashes) != len(set(evidence_hashes))
        or any(type(item) is not str or _SHA256.fullmatch(item) is None for item in evidence_hashes)
    ):
        raise AlphaFeasibilityDataError("controlled_adjustment_evidence_registry_invalid")
    if evidence_hashes:
        # V1 has no durable, create-only evidence artifact/replay contract.
        # Refuse the half-supported branch rather than allow a first run that
        # cannot be reproduced by the loader without caller memory.
        raise AlphaFeasibilityDataError("controlled_adjustment_evidence_not_supported")

    requests = _require_mapping(config.get("requests"), "missing_request_config")
    if set(requests) != set(ALLOWED_ENDPOINTS):
        raise AlphaFeasibilityDataError("request_endpoint_set_differs")
    for endpoint in ALLOWED_ENDPOINTS:
        request = _require_mapping(requests.get(endpoint), "invalid_endpoint_request")
        _require_exact_fields(request.get("fields"), endpoint)
        params = _require_mapping(request.get("params"), "invalid_endpoint_params")
        if any(_FORBIDDEN_PARAM_KEY.search(str(key)) for key in params):
            raise AlphaFeasibilityDataError("credential_like_request_parameter")
    exact_params = {
        "trade_cal": {"exchange": "SSE", "start_date": "20170701", "end_date": "20231231"},
        "index_weight": {"index_code": "000906.SH"},
        "daily": {"start_date": "20170701", "end_date": "20231231"},
        "adj_factor": {"start_date": "20170701", "end_date": "20231231"},
        "suspend_d": {"start_date": "20170701", "end_date": "20231231"},
        "index_daily": {
            "ts_code": "000906.SH",
            "start_date": "20170701",
            "end_date": "20231231",
        },
    }
    for endpoint, expected_params in exact_params.items():
        if dict(requests[endpoint]["params"]) != expected_params:
            raise AlphaFeasibilityDataError("request_window_differs_from_frozen_contract")
    expected_batch_sizes = {"daily": 3, "adj_factor": 1, "suspend_d": 3}
    for endpoint, expected_size in expected_batch_sizes.items():
        if requests[endpoint].get("instrument_batch_size") != expected_size:
            raise AlphaFeasibilityDataError("history_batch_size_differs_from_contract")
    if config.get("stock_basic_status") != STOCK_BASIC_STATUS:
        raise AlphaFeasibilityDataError("stock_basic_status_changed")
    stock_basic_request_count = config.get("stock_basic_request_count")
    if (
        type(stock_basic_request_count) is not int
        or stock_basic_request_count != STOCK_BASIC_REQUEST_COUNT
    ):
        raise AlphaFeasibilityDataError("stock_basic_request_count_must_be_zero")
    if config.get("security_master_pit_status") != SECURITY_MASTER_PIT_STATUS:
        raise AlphaFeasibilityDataError("security_master_pit_status_changed")
    total_return = _require_mapping(
        config.get("signal_total_return"), "missing_signal_total_return_config"
    )
    if (
        total_return.get("minimum_valid_controlled_sessions")
        != MINIMUM_VALID_CONTROLLED_SESSIONS
        or total_return.get("insufficient_history_policy")
        != INSUFFICIENT_HISTORY_STATUS
        or total_return.get("non_suspension_missing_daily_policy")
        != "unexplained_market_data_gap_fail_closed"
    ):
        raise AlphaFeasibilityDataError("history_eligibility_contract_changed")
    if config.get("locked_test_status") != dict(LOCKED_TEST_STATUS):
        raise AlphaFeasibilityDataError("locked_test_status_changed")
    if config.get("locked_test_consumed") is not False:
        raise AlphaFeasibilityDataError("locked_test_consumed_changed")
    safety = _require_mapping(config.get("safety"), "missing_safety_config")
    if (
        safety.get("execution_realism") != "INCOMPLETE"
        or safety.get("paper_eligibility") is not False
        or safety.get("trade_eligibility") is not False
        or safety.get("automatic_order_submission") is not False
        or safety.get("live_supported") is not False
        or (p15 and safety.get("real_money_list_allowed") is not False)
    ):
        raise AlphaFeasibilityDataError("execution_safety_boundary_changed")
    _verify_frozen_implementation(config, Path(repository_root))
    return MappingProxyType(dict(config))


def _p15_enabled(config_or_plan: Mapping[str, Any] | "CollectionPlan") -> bool:
    config = (
        config_or_plan.config
        if isinstance(config_or_plan, CollectionPlan)
        else config_or_plan
    )
    return config.get("schema_version") == P15_EXPERIMENT_SCHEMA_VERSION


def _p14d_bundle_binding(config_or_plan: Mapping[str, Any] | "CollectionPlan") -> Mapping[str, Any]:
    config = (
        config_or_plan.config
        if isinstance(config_or_plan, CollectionPlan)
        else config_or_plan
    )
    source = _require_mapping(config.get("source"), "missing_source_config")
    binding = _require_mapping(
        source.get("accepted_p14d_bundle"), "p14d_bundle_binding_missing"
    )
    artifact_hashes = _require_mapping(
        binding.get("source_artifact_sha256_by_name"),
        "p14d_bundle_binding_invalid",
    )
    if (
        set(artifact_hashes) != set(P14D_SOURCE_ARTIFACT_NAMES)
        or any(
            type(artifact_hashes.get(name)) is not str
            or _SHA256.fullmatch(artifact_hashes[name]) is None
            for name in P14D_SOURCE_ARTIFACT_NAMES
        )
        or type(binding.get("bundle_sha256")) is not str
        or binding["bundle_sha256"] != canonical_sha256(dict(artifact_hashes))
        or any(
            type(binding.get(key)) is not str
            or _SHA256.fullmatch(binding[key]) is None
            for key in (
                "request_fingerprint",
                "raw_transport_sha256",
                "normalized_content_sha256",
            )
        )
    ):
        raise AlphaFeasibilityDataError("p14d_bundle_binding_invalid")
    return binding


def _pit_schema_versions(plan: "CollectionPlan") -> tuple[str, str]:
    if _p15_enabled(plan):
        return P15_PIT_REPORT_SCHEMA_VERSION, P15_PIT_MANIFEST_SCHEMA_VERSION
    return PIT_REPORT_SCHEMA_VERSION, PIT_MANIFEST_SCHEMA_VERSION


def _history_manifest_schema_version(plan: "CollectionPlan") -> str:
    return (
        P15_HISTORY_MANIFEST_SCHEMA_VERSION
        if _p15_enabled(plan)
        else HISTORY_MANIFEST_SCHEMA_VERSION
    )


def _request_count_semantics(plan: "CollectionPlan") -> str:
    return (
        P15_REQUEST_COUNT_SEMANTICS
        if _p15_enabled(plan)
        else "durable_network_call_started_claim"
    )


def _manifest_safety(plan: "CollectionPlan") -> dict[str, Any]:
    safety = {
        "research_status": "research_alpha_feasibility_only",
        "execution_realism": "INCOMPLETE",
        "paper_eligibility": False,
        "trade_eligibility": False,
        "automatic_order_submission": False,
        "live_supported": False,
    }
    if _p15_enabled(plan):
        safety["real_money_list_allowed"] = False
    return safety


def _attempt_settings(plan: "CollectionPlan") -> tuple[bool, int]:
    if not _p15_enabled(plan):
        return False, 1
    maximum = plan.config["source"]["maximum_attempts_per_fingerprint"]
    return True, int(maximum)


def _validate_wire_request_contract(
    endpoint: str,
    params: Mapping[str, str],
    fields: Sequence[str],
    *,
    scope_instruments: Sequence[str] | None = None,
) -> None:
    """Unbypassable network-boundary validation for one authorized request."""

    if endpoint not in ALLOWED_ENDPOINTS or endpoint.endswith("_vip"):
        raise AlphaFeasibilityDataError("endpoint_not_allowed")
    if tuple(fields) != EXPECTED_FIELDS[endpoint]:
        raise AlphaFeasibilityDataError("task_fields_differ_from_contract")
    values = dict(params)
    if any(type(key) is not str or type(value) is not str for key, value in values.items()):
        raise AlphaFeasibilityDataError("task_params_must_be_strings")
    fixed_window = {"start_date": "20170701", "end_date": "20231231"}
    if endpoint == "trade_cal":
        expected = {"exchange": "SSE", **fixed_window}
    elif endpoint == "index_daily":
        expected = {"ts_code": "000906.SH", **fixed_window}
    elif endpoint == "index_weight":
        if set(values) != {"index_code", "start_date", "end_date"}:
            raise AlphaFeasibilityDataError("index_weight_params_differ_from_contract")
        if values.get("index_code") != "000906.SH":
            raise AlphaFeasibilityDataError("unexpected_index_code")
        start = _parse_date(values.get("start_date"), "index_weight_start")
        end = _parse_date(values.get("end_date"), "index_weight_end")
        expected_start, expected_end = _month_bounds(start.strftime("%Y-%m"))
        if (
            start != expected_start
            or end != expected_end
            or not date(2017, 12, 1) <= start <= date(2023, 12, 1)
            or end > ABSOLUTE_CUTOFF
        ):
            raise AlphaFeasibilityDataError("index_weight_month_window_invalid")
        expected = values
    else:
        codes_text = values.get("ts_code")
        if type(codes_text) is not str:
            raise AlphaFeasibilityDataError("history_request_scope_missing")
        codes = tuple(codes_text.split(","))
        maximum = 1 if endpoint == "adj_factor" else 3
        if (
            not 1 <= len(codes) <= maximum
            or len(set(codes)) != len(codes)
            or tuple(sorted(codes)) != codes
            or any(_TS_CODE.fullmatch(code) is None for code in codes)
        ):
            raise AlphaFeasibilityDataError("history_request_scope_invalid")
        expected = {**fixed_window, "ts_code": codes_text}
        if scope_instruments is not None and tuple(scope_instruments) != codes:
            raise AlphaFeasibilityDataError("task_params_scope_mismatch")
    if values != expected:
        raise AlphaFeasibilityDataError("endpoint_params_differ_from_contract")
    if scope_instruments is not None:
        scope = tuple(scope_instruments)
        if endpoint in {"daily", "adj_factor", "suspend_d"}:
            if not scope:
                raise AlphaFeasibilityDataError("task_scope_missing")
        elif scope:
            raise AlphaFeasibilityDataError("unexpected_task_scope")


def _validate_collection_task_contract(task: Any) -> None:
    _validate_wire_request_contract(
        task.endpoint,
        task.params,
        task.fields,
        scope_instruments=task.scope_instruments,
    )


@dataclass(frozen=True, slots=True)
class CollectionTask:
    endpoint: str
    params: Mapping[str, str]
    fields: tuple[str, ...]
    plan_sha256: str
    scope_instruments: tuple[str, ...] = ()
    task_id: str = ""

    def __post_init__(self) -> None:
        if self.endpoint not in ALLOWED_ENDPOINTS or self.endpoint.endswith("_vip"):
            raise AlphaFeasibilityDataError("endpoint_not_allowed")
        params = dict(self.params)
        if any(type(key) is not str or type(value) is not str for key, value in params.items()):
            raise AlphaFeasibilityDataError("task_params_must_be_strings")
        if any(_FORBIDDEN_PARAM_KEY.search(key) for key in params):
            raise AlphaFeasibilityDataError("credential_like_task_parameter")
        if tuple(self.fields) != EXPECTED_FIELDS[self.endpoint]:
            raise AlphaFeasibilityDataError("task_fields_differ_from_contract")
        if _SHA256.fullmatch(self.plan_sha256) is None:
            raise AlphaFeasibilityDataError("invalid_plan_sha256")
        scope = tuple(sorted(self.scope_instruments))
        if len(set(scope)) != len(scope) or any(_TS_CODE.fullmatch(code) is None for code in scope):
            raise AlphaFeasibilityDataError("invalid_task_instrument_scope")
        semantic = {
            "schema_version": TASK_SCHEMA_VERSION,
            "endpoint": self.endpoint,
            "params": params,
            "fields": list(self.fields),
            "plan_sha256": self.plan_sha256,
            "scope_instruments_sha256": canonical_sha256(list(scope)),
        }
        expected_id = f"{self.endpoint}-{canonical_sha256(semantic)}"
        if self.task_id and self.task_id != expected_id:
            raise AlphaFeasibilityDataError("task_id_semantics_mismatch")
        object.__setattr__(self, "params", MappingProxyType(params))
        object.__setattr__(self, "scope_instruments", scope)
        object.__setattr__(self, "task_id", expected_id)
        _validate_collection_task_contract(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": TASK_SCHEMA_VERSION,
            "task_id": self.task_id,
            "endpoint": self.endpoint,
            "params": dict(self.params),
            "fields": list(self.fields),
            "plan_sha256": self.plan_sha256,
            "scope_instruments_sha256": canonical_sha256(list(self.scope_instruments)),
        }


@dataclass(frozen=True, slots=True)
class CollectionPlan:
    config: Mapping[str, Any]
    config_sha256: str
    plan_sha256: str
    pit_tasks: tuple[CollectionTask, ...]


def _task(
    endpoint: str,
    params: Mapping[str, Any],
    fields: Sequence[str],
    plan_sha256: str,
    *,
    scope_instruments: Sequence[str] = (),
) -> CollectionTask:
    normalized: dict[str, str] = {}
    for key, value in params.items():
        if type(value) not in {str, int}:
            raise AlphaFeasibilityDataError("non_scalar_task_parameter")
        normalized[str(key)] = str(value)
    return CollectionTask(
        endpoint=endpoint,
        params=normalized,
        fields=tuple(fields),
        plan_sha256=plan_sha256,
        scope_instruments=tuple(scope_instruments),
    )


def load_config_and_build_plan(
    config_path: Path | str = DEFAULT_CONFIG_PATH,
    *,
    repository_root: Path | str = REPOSITORY_ROOT,
) -> CollectionPlan:
    """Read and validate config, then construct exactly 73 PIT tasks.

    No environment lookup, network construction, output-directory access, or
    historical data access happens in this function.
    """

    path = Path(config_path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise AlphaFeasibilityDataError("experiment_config_unavailable") from exc
    config = validate_experiment_config(
        _require_mapping(strict_json_loads(raw, label="config"), "config_root_not_object"),
        repository_root=repository_root,
    )
    config_sha = hashlib.sha256(raw).hexdigest()
    months = _month_sequence(
        config["index"]["pit_first_month"], config["index"]["pit_last_month"]
    )
    semantics = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "config_sha256": config_sha,
        "absolute_cutoff": _iso(ABSOLUTE_CUTOFF),
        "allowed_endpoints": list(ALLOWED_ENDPOINTS),
        "pit_months": list(months),
        "locked_test_status": dict(LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
    }
    plan_sha = canonical_sha256(semantics)
    base = config["requests"]["index_weight"]
    tasks: list[CollectionTask] = []
    for month in months:
        start, end = _month_bounds(month)
        if end > ABSOLUTE_CUTOFF:
            raise AlphaFeasibilityDataError("post_cutoff_pit_task")
        params = dict(base["params"])
        params.update({"start_date": _compact(start), "end_date": _compact(end)})
        tasks.append(_task("index_weight", params, base["fields"], plan_sha))
    if len(tasks) != 73 or len({task.task_id for task in tasks}) != 73:
        raise AlphaFeasibilityDataError("pit_task_plan_not_exactly_73")
    return CollectionPlan(config=config, config_sha256=config_sha, plan_sha256=plan_sha, pit_tasks=tuple(tasks))


def build_history_plan(
    plan: CollectionPlan, union_instruments: Iterable[str]
) -> tuple[CollectionTask, ...]:
    """Build the minimal post-PIT history plan for the exact member union."""

    instruments = tuple(sorted(set(union_instruments)))
    if not instruments or any(_TS_CODE.fullmatch(code) is None for code in instruments):
        raise AlphaFeasibilityDataError("invalid_or_empty_union")
    requests = plan.config["requests"]
    tasks: list[CollectionTask] = []
    trade_cal = requests["trade_cal"]
    tasks.append(_task("trade_cal", trade_cal["params"], trade_cal["fields"], plan.plan_sha256))
    index_daily = requests["index_daily"]
    tasks.append(
        _task("index_daily", index_daily["params"], index_daily["fields"], plan.plan_sha256)
    )
    for endpoint in ("daily", "adj_factor", "suspend_d"):
        request = requests[endpoint]
        batch_size = request["instrument_batch_size"]
        for offset in range(0, len(instruments), batch_size):
            batch = instruments[offset : offset + batch_size]
            params = dict(request["params"])
            params["ts_code"] = ",".join(batch)
            tasks.append(
                _task(
                    endpoint,
                    params,
                    request["fields"],
                    plan.plan_sha256,
                    scope_instruments=batch,
                )
            )
    if len({task.task_id for task in tasks}) != len(tasks):
        raise AlphaFeasibilityDataError("duplicate_history_task")
    for task in tasks:
        for key in ("start_date", "end_date"):
            if key in task.params and _parse_date(task.params[key], "task_date") > ABSOLUTE_CUTOFF:
                raise AlphaFeasibilityDataError("post_cutoff_history_task")
    return tuple(tasks)


class HttpsTushareTransport:
    """Minimal standard-library HTTPS POST transport with no retry/redirect."""

    def __call__(
        self,
        *,
        endpoint: str,
        params: Mapping[str, str],
        fields: Sequence[str],
        token: str,
        timeout_seconds: int,
        maximum_response_bytes: int,
    ) -> TushareHttpResponse:
        # Keep this guard at the physical network boundary as well as on
        # CollectionTask construction.  A caller must not be able to bypass
        # the frozen endpoint/date/field contract by invoking the transport
        # directly.
        _validate_wire_request_contract(endpoint, params, fields)
        _validate_token(token)
        payload = json.dumps(
            {
                "api_name": endpoint,
                "token": token,
                "params": dict(params),
                "fields": ",".join(fields),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        connection = http.client.HTTPSConnection(
            OFFICIAL_API_HOST,
            port=443,
            timeout=timeout_seconds,
            context=ssl.create_default_context(),
        )
        try:
            connection.request(
                "POST",
                OFFICIAL_API_PATH,
                body=payload,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Content-Length": str(len(payload)),
                    "Accept": "application/json",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            if 300 <= response.status <= 399:
                raise AlphaFeasibilityDataError("http_redirect_forbidden")
            if response.status != 200:
                raise AlphaFeasibilityDataError("http_status_not_success")
            effective_maximum = min(maximum_response_bytes, MAXIMUM_RESPONSE_BYTES)
            raw = response.read(effective_maximum + 1)
            if len(raw) > effective_maximum:
                raise AlphaFeasibilityDataError("response_body_too_large")
            return TushareHttpResponse(http_status=response.status, body=raw)
        except AlphaFeasibilityDataError:
            raise
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            raise AlphaFeasibilityDataError("https_transport_failed") from exc
        finally:
            connection.close()


def _validate_token(token: Any) -> str:
    if (
        type(token) is not str
        or not 16 <= len(token) <= 128
        or re.fullmatch(r"[A-Za-z0-9]+", token) is None
    ):
        raise AlphaFeasibilityDataError("credential_preflight_failed")
    return token


def validate_tushare_token_for_process(value: Any) -> str:
    """Validate an in-memory credential without logging or persistence."""

    return _validate_token(value)


def _decimal(value: Any, label: str, *, minimum: Decimal | None = None) -> tuple[Decimal, str]:
    if isinstance(value, bool) or value is None:
        raise AlphaFeasibilityDataError(f"invalid_{label}")
    if isinstance(value, Decimal):
        number = value
        text = str(value)
    elif type(value) in {str, int}:
        text = str(value)
        try:
            number = Decimal(text)
        except InvalidOperation as exc:
            raise AlphaFeasibilityDataError(f"invalid_{label}") from exc
    else:
        raise AlphaFeasibilityDataError(f"invalid_{label}")
    if not number.is_finite() or (minimum is not None and number < minimum):
        raise AlphaFeasibilityDataError(f"invalid_{label}")
    return number, text


def _index_weight_decimal(value: Any) -> tuple[Decimal, str]:
    if type(value) is str and _PLAIN_DECIMAL_STRING.fullmatch(value) is None:
        raise AlphaFeasibilityDataError("invalid_weight")
    number, _text = _decimal(value, "weight", minimum=Decimal("0"))
    if number.is_zero() and number.is_signed():
        raise AlphaFeasibilityDataError("invalid_weight")
    if (
        number.as_tuple().exponent < -MAXIMUM_REQUIRED_DECIMAL_SCALE
        or number.adjusted() > MAXIMUM_REQUIRED_DECIMAL_ADJUSTED_EXPONENT
    ):
        raise AlphaFeasibilityDataError("invalid_weight")
    return number, format(number, "f")


def _decimal_places(text: str) -> int:
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise AlphaFeasibilityDataError("invalid_decimal_precision") from exc
    return max(0, -value.as_tuple().exponent)


def _exact_decimal_sum(values: Sequence[Decimal]) -> Decimal:
    """Sum bounded Decimals without the process-wide context rounding rows."""

    numbers = tuple(values)
    if not numbers:
        return Decimal("0")
    minimum_exponent = min(number.as_tuple().exponent for number in numbers)
    maximum_adjusted = max(number.adjusted() for number in numbers)
    aligned_digits = maximum_adjusted - minimum_exponent + 1
    carry_digits = len(str(len(numbers)))
    with localcontext() as context:
        context.prec = max(28, aligned_digits + carry_digits)
        return sum(numbers, Decimal("0"))


def _normalized_code(value: Any, label: str = "ts_code") -> str:
    if type(value) is not str or _TS_CODE.fullmatch(value) is None:
        raise AlphaFeasibilityDataError(f"invalid_{label}")
    return value


def _response_date_window(task: CollectionTask, value: str, label: str) -> date:
    parsed = _parse_date(value, label)
    if parsed > ABSOLUTE_CUTOFF:
        raise AlphaFeasibilityDataError("post_cutoff_response_date")
    if "start_date" in task.params and parsed < _parse_date(task.params["start_date"], "task_start"):
        raise AlphaFeasibilityDataError("response_date_before_request_window")
    if "end_date" in task.params and parsed > _parse_date(task.params["end_date"], "task_end"):
        raise AlphaFeasibilityDataError("response_date_after_request_window")
    return parsed


def _normalize_response_row(
    task: CollectionTask, row: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, int]:
    """Return a safe row and count of isolated post-cutoff delist dates."""

    endpoint = task.endpoint
    result = dict(row)
    isolated = 0
    if endpoint == "trade_cal":
        if type(result["exchange"]) is not str or result["exchange"] != task.params.get("exchange"):
            raise AlphaFeasibilityDataError("response_exchange_not_requested")
        parsed = _response_date_window(task, result["cal_date"], "calendar_date")
        result["cal_date"] = _compact(parsed)
        is_open = result["is_open"]
        if not (
            (type(is_open) is int and is_open in {0, 1})
            or (type(is_open) is str and is_open in {"0", "1"})
        ):
            raise AlphaFeasibilityDataError("invalid_is_open")
        result["is_open"] = int(is_open)
        pretrade_value = result["pretrade_date"]
        if pretrade_value is not None and pretrade_value != "":
            pretrade = _parse_date(pretrade_value, "pretrade_date")
            if pretrade > parsed or pretrade > ABSOLUTE_CUTOFF:
                raise AlphaFeasibilityDataError("invalid_pretrade_date")
            result["pretrade_date"] = _compact(pretrade)
        else:
            result["pretrade_date"] = None
    elif endpoint == "index_weight":
        if result["index_code"] != task.params.get("index_code"):
            raise AlphaFeasibilityDataError("response_index_not_requested")
        result["con_code"] = _normalized_code(result["con_code"], "component_code")
        if _PIT_COMPONENT_CODE.fullmatch(result["con_code"]) is None:
            raise AlphaFeasibilityDataError("pit_component_exchange_not_allowed")
        parsed = _response_date_window(task, result["trade_date"], "trade_date")
        result["trade_date"] = _compact(parsed)
        weight, text = _index_weight_decimal(result["weight"])
        result["weight"] = text
        if weight < 0:  # kept explicit for the PIT contract
            raise AlphaFeasibilityDataError("negative_weight")
    elif endpoint == "stock_basic":
        code = _normalized_code(result["ts_code"])
        if code not in task.scope_instruments:
            return None, 0
        if result["list_status"] != task.params.get("list_status"):
            raise AlphaFeasibilityDataError("stock_status_not_requested")
        if type(result["symbol"]) is not str or result["symbol"] != code.split(".")[0]:
            raise AlphaFeasibilityDataError("stock_symbol_code_mismatch")
        exchange_by_suffix = {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}
        if result["exchange"] != exchange_by_suffix[code.split(".")[1]]:
            raise AlphaFeasibilityDataError("stock_exchange_code_mismatch")
        listed = _parse_date(result["list_date"], "list_date")
        if listed > ABSOLUTE_CUTOFF:
            raise AlphaFeasibilityDataError("post_cutoff_list_date")
        result["list_date"] = _compact(listed)
        delisted = result["delist_date"]
        if delisted is None or delisted == "":
            result["delist_date"] = None
        else:
            delisted_date = _parse_date(delisted, "delist_date")
            if delisted_date > ABSOLUTE_CUTOFF:
                # This metadata is known now but was not available inside the
                # experiment window.  Do not persist the future date itself.
                result["delist_date"] = None
                isolated = 1
            else:
                if delisted_date < listed:
                    raise AlphaFeasibilityDataError("delist_before_list_date")
                result["delist_date"] = _compact(delisted_date)
        if type(result["name"]) is not str:
            raise AlphaFeasibilityDataError("invalid_stock_name")
    elif endpoint in {"daily", "adj_factor", "suspend_d"}:
        code = _normalized_code(result["ts_code"])
        if code not in task.scope_instruments:
            raise AlphaFeasibilityDataError("response_instrument_not_requested")
        parsed = _response_date_window(task, result["trade_date"], "trade_date")
        result["trade_date"] = _compact(parsed)
        if endpoint == "daily":
            for field in ("open", "high", "low", "close", "pre_close", "vol", "amount"):
                number, text = _decimal(result[field], field, minimum=Decimal("0"))
                result[field] = text
                if field in {"open", "high", "low", "close", "pre_close"} and number <= 0:
                    raise AlphaFeasibilityDataError("nonpositive_daily_price")
            high = Decimal(result["high"])
            low = Decimal(result["low"])
            if high < low or high < Decimal(result["open"]) or high < Decimal(result["close"]):
                raise AlphaFeasibilityDataError("invalid_daily_ohlc")
            if low > Decimal(result["open"]) or low > Decimal(result["close"]):
                raise AlphaFeasibilityDataError("invalid_daily_ohlc")
        elif endpoint == "adj_factor":
            factor, text = _decimal(result["adj_factor"], "adj_factor", minimum=Decimal("0"))
            if factor <= 0:
                raise AlphaFeasibilityDataError("nonpositive_adj_factor")
            result["adj_factor"] = text
        else:
            if type(result["suspend_type"]) is not str or result["suspend_type"] not in {"S", "R"}:
                raise AlphaFeasibilityDataError("invalid_suspend_type")
            timing = result["suspend_timing"]
            if timing is not None and type(timing) is not str:
                raise AlphaFeasibilityDataError("invalid_suspend_timing")
            if type(timing) is str:
                timing = timing.strip()
                result["suspend_timing"] = timing or None
    elif endpoint == "index_daily":
        if result["ts_code"] != task.params.get("ts_code"):
            raise AlphaFeasibilityDataError("response_index_not_requested")
        parsed = _response_date_window(task, result["trade_date"], "trade_date")
        result["trade_date"] = _compact(parsed)
        for field in (
            "open",
            "high",
            "low",
            "close",
            "pre_close",
            "change",
            "pct_chg",
            "vol",
            "amount",
        ):
            minimum = Decimal("0") if field not in {"change", "pct_chg"} else None
            number, text = _decimal(result[field], field, minimum=minimum)
            result[field] = text
            if field in {"open", "high", "low", "close", "pre_close"} and number <= 0:
                raise AlphaFeasibilityDataError("nonpositive_index_price")
    else:  # pragma: no cover - CollectionTask rejects this first
        raise AlphaFeasibilityDataError("endpoint_not_allowed")
    return result, isolated


def _primary_key(endpoint: str, row: Mapping[str, Any]) -> tuple[Any, ...]:
    if endpoint == "trade_cal":
        return row["exchange"], row["cal_date"]
    if endpoint == "index_weight":
        return row["index_code"], row["trade_date"], row["con_code"]
    if endpoint == "stock_basic":
        return (row["ts_code"],)
    if endpoint == "suspend_d":
        return (
            row["ts_code"],
            row["trade_date"],
            row["suspend_type"],
            row["suspend_timing"],
        )
    return row["ts_code"], row["trade_date"]


def _row_sort_key(endpoint: str, row: Mapping[str, Any]) -> tuple[Any, ...]:
    if endpoint == "trade_cal":
        return row["cal_date"], row["exchange"]
    if endpoint == "index_weight":
        return row["index_code"], row["trade_date"], row["con_code"]
    if endpoint == "stock_basic":
        return (row["ts_code"],)
    if endpoint == "suspend_d":
        return (
            row["trade_date"],
            row["ts_code"],
            row["suspend_type"],
            row["suspend_timing"] is not None,
            row["suspend_timing"] or "",
        )
    return row["trade_date"], row["ts_code"]


@dataclass(frozen=True, slots=True)
class ValidatedResponse:
    rows: tuple[Mapping[str, Any], ...]
    raw_response_sha256: str
    normalized_content_sha256: str
    observed_root_fields: tuple[str, ...]
    semantic_core_fields: tuple[str, ...]
    transport_extension_field_names: tuple[str, ...]
    transport_extension_type_by_field: Mapping[str, str]
    transport_extension_value_sha256_by_field: Mapping[str, str]
    transport_extensions_sha256: str
    transport_extensions_byte_count: int
    response_byte_count: int
    isolated_future_delist_date_count: int
    isolated_non_union_row_count: int
    observed_data_fields: tuple[str, ...]
    required_data_fields: tuple[str, ...]
    missing_required_data_fields: tuple[str, ...]
    extra_data_fields: tuple[str, ...]
    field_order_matches_canonical: bool
    provider_payload_sha256: str
    extra_data_field_value_sha256_by_field: Mapping[str, str]
    data_row_count: int

    @property
    def accepted_root_fields(self) -> tuple[str, ...]:
        """Compatibility alias for callers that only need observed field names."""

        return self.observed_root_fields

    @property
    def request_id_present(self) -> bool:
        return "request_id" in self.transport_extension_field_names

    @property
    def request_id_sha256(self) -> str | None:
        return self.transport_extension_value_sha256_by_field.get("request_id")


def _normalized_content_sha256(
    task: CollectionTask,
    rows: Sequence[Mapping[str, Any]],
) -> str:
    """Bind normalized identity to the endpoint's fixed fields and item values."""

    return canonical_sha256(
        {
            "fields": list(task.fields),
            "items": [[row[field] for field in task.fields] for row in rows],
        }
    )


def _provider_payload_sha256(fields: Sequence[str], items: Sequence[Any]) -> str:
    """Bind the provider's semantic table payload without transport metadata."""

    try:
        encoded = _canonical_transport_json_bytes(
            {"fields": list(fields), "items": list(items)}
        )
    except (AlphaFeasibilityDataError, RecursionError) as exc:
        raise AlphaFeasibilityDataError("data_required_value_invalid") from exc
    return hashlib.sha256(encoded).hexdigest()


def _extra_data_field_value_hashes(
    fields: Sequence[str],
    items: Sequence[Sequence[Any]],
    extra_fields: Sequence[str],
) -> Mapping[str, str]:
    positions = {field: index for index, field in enumerate(fields)}
    hashes: dict[str, str] = {}
    try:
        for field in extra_fields:
            payload = {
                "field": field,
                "values": [item[positions[field]] for item in items],
            }
            hashes[field] = hashlib.sha256(
                _canonical_transport_json_bytes(payload)
            ).hexdigest()
    except (AlphaFeasibilityDataError, RecursionError) as exc:
        raise AlphaFeasibilityDataError("data_required_value_invalid") from exc
    return MappingProxyType(hashes)


def _safe_diagnostic_data_field(value: Any) -> str:
    if (
        type(value) is str
        and _SAFE_DATA_FIELD_NAME.fullmatch(value) is not None
        and _SECRET_TRANSPORT_KEY.search(value) is None
    ):
        return value
    if type(value) is str:
        return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
    try:
        return "type:" + _transport_json_type(value)
    except AlphaFeasibilityDataError:
        return "type:unknown"


def _validated_data_evidence(value: Mapping[str, Any], task: CollectionTask) -> None:
    """Reject re-signed response artifacts whose field evidence is inconsistent."""

    observed = value.get("observed_data_fields")
    required = value.get("required_data_fields")
    missing = value.get("missing_required_data_fields")
    extra = value.get("extra_data_fields")
    extra_hashes = value.get("extra_data_field_value_sha256_by_field")
    if (
        not isinstance(observed, list)
        or any(
            type(field) is not str
            or _SAFE_DATA_FIELD_NAME.fullmatch(field) is None
            or _SECRET_TRANSPORT_KEY.search(field) is not None
            for field in observed
        )
        or len(observed) != len(set(observed))
        or not set(task.fields).issubset(observed)
        or required != list(task.fields)
        or missing != []
        or not isinstance(extra, list)
        or extra != [field for field in observed if field not in set(task.fields)]
        or any(field in task.fields for field in extra)
        or type(value.get("field_order_matches_canonical")) is not bool
        or value["field_order_matches_canonical"]
        != (tuple(field for field in observed if field in set(task.fields)) == task.fields)
        or type(value.get("provider_payload_sha256")) is not str
        or _SHA256.fullmatch(value["provider_payload_sha256"]) is None
        or not isinstance(extra_hashes, Mapping)
        or set(extra_hashes) != set(extra)
        or any(
            type(digest) is not str or _SHA256.fullmatch(digest) is None
            for digest in extra_hashes.values()
        )
        or type(value.get("data_row_count")) is not int
        or value["data_row_count"] < 0
        or value["data_row_count"]
        != value.get("row_count", -1) + value.get("isolated_non_union_row_count", -1)
    ):
        raise AlphaFeasibilityDataError("response_artifact_data_evidence_mismatch")


def _safe_diagnostic_root_field(value: str) -> str:
    if (
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", value) is not None
        and _SECRET_TRANSPORT_KEY.search(value) is None
    ):
        return value
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _response_diagnostic(
    root: Mapping[str, Any],
    *,
    raw_sha256: str,
    response_byte_count: int,
    http_status: int,
    token_leak_check: str,
    extensions: TransportExtensionsMetadata | None = None,
) -> dict[str, Any]:
    observed = set(root)
    missing = REQUIRED_RESPONSE_ROOT_FIELDS - observed
    extension_fields = observed - REQUIRED_RESPONSE_ROOT_FIELDS
    safe_extension_fields = sorted(
        _safe_diagnostic_root_field(key) for key in extension_fields
    )[:MAXIMUM_TRANSPORT_EXTENSION_FIELDS]
    return {
        "http_status": http_status,
        "response_byte_count": response_byte_count,
        "raw_transport_sha256": raw_sha256,
        "observed_root_fields": sorted(
            _safe_diagnostic_root_field(key) for key in observed
        ),
        "semantic_core_fields": sorted(REQUIRED_RESPONSE_ROOT_FIELDS & observed),
        "missing_semantic_core_fields": sorted(missing),
        "transport_extension_field_names": safe_extension_fields,
        "transport_extension_type_by_field": (
            dict(extensions.type_by_field) if extensions is not None else {}
        ),
        "transport_extension_value_sha256_by_field": (
            dict(extensions.value_sha256_by_field) if extensions is not None else {}
        ),
        "transport_extensions_sha256": extensions.sha256 if extensions is not None else None,
        "transport_extensions_byte_count": (
            extensions.byte_count if extensions is not None else None
        ),
        "token_leak_check": token_leak_check,
    }


def _raise_response_error(
    code: str,
    diagnostic: Mapping[str, Any],
) -> None:
    raise AlphaFeasibilityDataError(code, diagnostic=diagnostic)


def validate_response_bytes(
    task: CollectionTask,
    raw: bytes,
    *,
    token: str | None = None,
    maximum_response_bytes: int = MAXIMUM_RESPONSE_BYTES,
    http_status: int = 200,
    require_full_raw_safety: bool = False,
) -> ValidatedResponse:
    """Validate bounded response bytes before any body can be persisted."""

    if not isinstance(raw, bytes):
        raise AlphaFeasibilityDataError("unknown_non_json_value")
    if type(http_status) is not int or not 100 <= http_status <= 599:
        raise AlphaFeasibilityDataError("unknown_non_json_value")
    if http_status != 200:
        raise AlphaFeasibilityDataError("http_status_not_success")
    if type(maximum_response_bytes) is not int or maximum_response_bytes <= 0:
        raise AlphaFeasibilityDataError("unknown_non_json_value")
    if type(require_full_raw_safety) is not bool:
        raise AlphaFeasibilityDataError("unknown_non_json_value")
    if len(raw) > min(maximum_response_bytes, MAXIMUM_RESPONSE_BYTES):
        raise AlphaFeasibilityDataError("response_body_too_large")
    if token is not None and token.encode("utf-8") in raw:
        # Do not even compute a digest of a credential-bearing response.
        raise AlphaFeasibilityDataError("transport_extension_secret_detected")
    raw_sha = hashlib.sha256(raw).hexdigest()
    try:
        root = strict_json_loads(raw, label="response")
    except AlphaFeasibilityDataError as exc:
        if exc.code == "duplicate_json_key":
            raise
        raise AlphaFeasibilityDataError("unknown_non_json_value") from exc
    if not isinstance(root, Mapping):
        raise AlphaFeasibilityDataError("semantic_core_type_invalid")
    if _contains_unicode_surrogate(root):
        raise AlphaFeasibilityDataError("unknown_non_json_value")
    if token is not None and _contains_decoded_text(root, token):
        raise AlphaFeasibilityDataError("transport_extension_secret_detected")
    if require_full_raw_safety:
        _reject_p15_full_raw_tree(root, token=token)
    diagnostic = _response_diagnostic(
        root,
        raw_sha256=raw_sha,
        response_byte_count=len(raw),
        http_status=http_status,
        token_leak_check="PASSED",
    )
    observed = set(root)
    if not REQUIRED_RESPONSE_ROOT_FIELDS.issubset(observed):
        _raise_response_error("semantic_core_missing", diagnostic)
    code = root["code"]
    if type(code) is not int:
        _raise_response_error("semantic_core_type_invalid", diagnostic)
    if root["msg"] is not None and type(root["msg"]) is not str:
        _raise_response_error("semantic_core_type_invalid", diagnostic)
    data = root["data"]
    if code == 0 and not isinstance(data, Mapping):
        _raise_response_error("semantic_core_type_invalid", diagnostic)
    if code != 0 and data is not None and not isinstance(data, Mapping):
        _raise_response_error("semantic_core_type_invalid", diagnostic)

    transport_extensions = {
        key: value for key, value in root.items() if key not in REQUIRED_RESPONSE_ROOT_FIELDS
    }
    try:
        extension_metadata = _validate_transport_extensions(
            transport_extensions,
            token=token,
        )
    except AlphaFeasibilityDataError as exc:
        if exc.code in ADAPTER_PROTOCOL_FAILURES:
            raise AlphaFeasibilityDataError(exc.code, diagnostic=diagnostic) from exc
        raise AlphaFeasibilityDataError("unknown_non_json_value", diagnostic=diagnostic) from exc
    diagnostic = _response_diagnostic(
        root,
        raw_sha256=raw_sha,
        response_byte_count=len(raw),
        http_status=http_status,
        token_leak_check="PASSED",
        extensions=extension_metadata,
    )
    if code != 0:
        if data is not None:
            try:
                _validate_transport_extensions({"error": data}, token=token)
            except AlphaFeasibilityDataError as exc:
                raise AlphaFeasibilityDataError(exc.code, diagnostic=diagnostic) from exc
        request_id_value = transport_extensions.get("request_id")
        request_id = request_id_value if type(request_id_value) is str else None
        detail_present = "detail" in transport_extensions
        detail = transport_extensions.get("detail")
        sanitized_msg = _sanitize_provider_text(
            root["msg"], token=token, request_id=request_id
        )
        sanitized_detail = (
            _detail_semantic_text(detail, token=token, request_id=request_id)
            if detail_present
            else ""
        )
        classification = _classify_business_error_semantics(
            sanitized_msg, sanitized_detail
        )
        detail_category = (
            _classify_business_error_semantics("", sanitized_detail)
            if detail_present
            else None
        )
        if detail_category == "UPSTREAM_UNKNOWN_ERROR":
            detail_category = None
        category = _BUSINESS_CLASSIFICATION_TO_LEGACY_CATEGORY[classification]
        safe_evidence_diagnostic = (
            {
                "sanitized_msg": sanitized_msg,
                "msg_sha256": hashlib.sha256(
                    _canonical_transport_json_bytes(root["msg"])
                ).hexdigest(),
                "detail_type": (
                    extension_metadata.type_by_field["detail"]
                    if detail_present
                    else None
                ),
                "safe_detail_projection": (
                    {
                        "json_type": extension_metadata.type_by_field["detail"],
                        "value": _safe_detail_projection_value(
                            detail, token=token, request_id=request_id
                        ),
                        "sanitized_text": sanitized_detail,
                        "semantic_category": detail_category,
                    }
                    if detail_present
                    else None
                ),
                "detail_sha256": (
                    extension_metadata.value_sha256_by_field["detail"]
                    if detail_present
                    else None
                ),
                "request_id_sha256": extension_metadata.value_sha256_by_field.get(
                    "request_id"
                ),
                "response_body_sha256": raw_sha,
                "retry_after_seconds": (
                    _extract_retry_after_seconds(
                        detail, f"{sanitized_msg}\n{sanitized_detail}"
                    )
                    if classification == "RATE_LIMITED"
                    else None
                ),
            }
            if require_full_raw_safety
            else {}
        )
        diagnostic = {
            **diagnostic,
            "upstream_code": code,
            "upstream_error_category": category,
            "business_error_classification": classification,
            **safe_evidence_diagnostic,
        }
        _raise_response_error(
            _BUSINESS_CLASSIFICATION_TO_FAILURE_CODE[classification], diagnostic
        )

    # A successful semantic core must not smuggle a post-cutoff observation in
    # its message. Error messages are classified above by code/msg and are
    # quarantined without being interpreted as market data.
    try:
        _reject_embedded_post_cutoff_date(
            root["msg"],
            "post_cutoff_response_date",
        )
    except AlphaFeasibilityDataError as exc:
        raise AlphaFeasibilityDataError(
            "data_payload_invalid",
            diagnostic={**diagnostic, "data_failure_category": exc.code},
        ) from exc

    required_fields = task.fields
    required_set = set(required_fields)
    data_evidence: dict[str, Any] = {
        "observed_data_fields": [],
        "required_data_fields": list(required_fields),
        "missing_required_data_fields": list(required_fields),
        "extra_data_fields": [],
        "field_order_matches_canonical": False,
        "provider_payload_sha256": None,
        "normalized_content_sha256": None,
        "extra_data_field_value_sha256_by_field": {},
        "data_row_count": 0,
    }
    try:
        fields = data.get("fields")
        items = data.get("items")
        if isinstance(items, list):
            data_evidence["data_row_count"] = len(items)
        if not isinstance(fields, list):
            raise AlphaFeasibilityDataError("data_fields_not_array")
        data_evidence["observed_data_fields"] = [
            _safe_diagnostic_data_field(field) for field in fields
        ]
        safe_field_names = [
            field
            for field in fields
            if type(field) is str
            and _SAFE_DATA_FIELD_NAME.fullmatch(field) is not None
            and _SECRET_TRANSPORT_KEY.search(field) is None
        ]
        data_evidence["missing_required_data_fields"] = [
            field for field in required_fields if field not in safe_field_names
        ]
        data_evidence["extra_data_fields"] = list(
            dict.fromkeys(field for field in safe_field_names if field not in required_set)
        )
        # The diagnostic is only about the relative order of recognizable
        # required fields.  An unsafe extra field is represented by a safe
        # hash/type placeholder, but must not make an otherwise canonical
        # required-field order disagree with the reporting boundary.
        data_evidence["field_order_matches_canonical"] = (
            tuple(field for field in safe_field_names if field in required_set)
            == required_fields
        )
        if isinstance(items, list):
            data_evidence["provider_payload_sha256"] = _provider_payload_sha256(
                fields, items
            )
        if len(safe_field_names) != len(fields):
            raise AlphaFeasibilityDataError("data_field_name_invalid")
        if len(fields) != len(set(fields)):
            raise AlphaFeasibilityDataError("data_duplicate_fields")
        if data_evidence["missing_required_data_fields"]:
            raise AlphaFeasibilityDataError("data_required_fields_missing")
        if not isinstance(items, list):
            raise AlphaFeasibilityDataError("data_item_width_mismatch")
        for item in items:
            if not isinstance(item, list) or len(item) != len(fields):
                raise AlphaFeasibilityDataError("data_item_width_mismatch")
        data_evidence["extra_data_field_value_sha256_by_field"] = dict(
            _extra_data_field_value_hashes(
                fields,
                items,
                data_evidence["extra_data_fields"],
            )
        )

        data_extensions = set(data) - {"fields", "items"}
        if data_extensions - {"has_more", "count"}:
            raise AlphaFeasibilityDataError("data_required_value_invalid")
        if "has_more" in data:
            if type(data["has_more"]) is not bool:
                raise AlphaFeasibilityDataError("data_required_value_invalid")
            if data["has_more"]:
                raise AlphaFeasibilityDataError("potential_upstream_truncation")
        if "count" in data and (
            type(data["count"]) is not int or data["count"] != 0
        ):
            raise AlphaFeasibilityDataError("data_required_value_invalid")

        _reject_response_post_cutoff_dates(task, root)
        if len(items) >= POTENTIAL_TRUNCATION_LIMIT[task.endpoint]:
            raise AlphaFeasibilityDataError("potential_upstream_truncation")
        normalized: list[Mapping[str, Any]] = []
        isolated = 0
        isolated_non_union = 0
        keys: set[tuple[Any, ...]] = set()
        for item in items:
            observed_row = dict(zip(fields, item))
            projected_row = {
                field: observed_row[field] for field in required_fields
            }
            try:
                row, row_isolated = _normalize_response_row(task, projected_row)
            except AlphaFeasibilityDataError as exc:
                raise AlphaFeasibilityDataError("data_required_value_invalid") from exc
            isolated += row_isolated
            if row is None:
                isolated_non_union += 1
                continue
            key = _primary_key(task.endpoint, row)
            if key in keys:
                if task.endpoint != "suspend_d":
                    raise AlphaFeasibilityDataError("duplicate_response_primary_key")
                for index, existing in enumerate(normalized):
                    if _primary_key(task.endpoint, existing) == key:
                        folded = dict(existing)
                        folded["exact_duplicate_count"] = (
                            int(folded.get("exact_duplicate_count", 0)) + 1
                        )
                        normalized[index] = MappingProxyType(folded)
                        break
                continue
            keys.add(key)
            normalized.append(MappingProxyType(row))
    except AlphaFeasibilityDataError as exc:
        data_diagnostic = {
            **diagnostic,
            **data_evidence,
            "data_failure_category": exc.code,
        }
        raise AlphaFeasibilityDataError(
            "data_payload_invalid",
            diagnostic=data_diagnostic,
        ) from exc
    normalized.sort(key=lambda row: _row_sort_key(task.endpoint, row))
    normalized_rows = [dict(row) for row in normalized]
    normalized_content_sha256 = _normalized_content_sha256(task, normalized_rows)
    data_evidence["normalized_content_sha256"] = normalized_content_sha256
    return ValidatedResponse(
        rows=tuple(normalized),
        raw_response_sha256=raw_sha,
        normalized_content_sha256=normalized_content_sha256,
        observed_root_fields=tuple(sorted(observed)),
        semantic_core_fields=tuple(sorted(REQUIRED_RESPONSE_ROOT_FIELDS)),
        transport_extension_field_names=extension_metadata.field_names,
        transport_extension_type_by_field=extension_metadata.type_by_field,
        transport_extension_value_sha256_by_field=(
            extension_metadata.value_sha256_by_field
        ),
        transport_extensions_sha256=extension_metadata.sha256,
        transport_extensions_byte_count=extension_metadata.byte_count,
        response_byte_count=len(raw),
        isolated_future_delist_date_count=isolated,
        isolated_non_union_row_count=isolated_non_union,
        observed_data_fields=tuple(fields),
        required_data_fields=required_fields,
        missing_required_data_fields=(),
        extra_data_fields=tuple(data_evidence["extra_data_fields"]),
        field_order_matches_canonical=bool(
            data_evidence["field_order_matches_canonical"]
        ),
        provider_payload_sha256=str(data_evidence["provider_payload_sha256"]),
        extra_data_field_value_sha256_by_field=MappingProxyType(
            dict(data_evidence["extra_data_field_value_sha256_by_field"])
        ),
        data_row_count=int(data_evidence["data_row_count"]),
    )


def _write_create_only(path: Path, content: bytes) -> None:
    """Atomically publish bytes without ever replacing an existing artifact."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".alpha-feasibility-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            if handle.write(content) != len(content):
                raise AlphaFeasibilityDataError("short_artifact_write")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise AlphaFeasibilityDataError("create_only_artifact_exists") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _guard_artifact_secret(content: bytes, token: str | None) -> None:
    if not token:
        return
    if token.encode("utf-8") in content:
        raise AlphaFeasibilityDataError("credential_persistence_forbidden")
    try:
        decoded = strict_json_loads(content, label="artifact_secret_guard")
    except AlphaFeasibilityDataError:
        # Direct byte matching remains the safe fallback for non-JSON content;
        # validated provider responses and all collector artifacts are JSON.
        return
    if _contains_decoded_text(decoded, token):
        raise AlphaFeasibilityDataError("credential_persistence_forbidden")


def _write_json_create_only(path: Path, value: Any, *, token: str | None = None) -> bytes:
    content = canonical_json_bytes(value)
    _guard_artifact_secret(content, token)
    _write_create_only(path, content)
    return content


def _publish_or_verify_identical(
    path: Path, value: Any, *, token: str | None = None
) -> bytes:
    content = canonical_json_bytes(value)
    _guard_artifact_secret(content, token)
    if path.exists():
        try:
            existing = path.read_bytes()
        except OSError as exc:
            raise AlphaFeasibilityDataError("existing_artifact_unreadable") from exc
        if existing != content:
            raise AlphaFeasibilityDataError("existing_artifact_content_mismatch")
        return existing
    _write_create_only(path, content)
    return content


def _publish_bytes_or_verify_identical(
    path: Path, content: bytes, *, token: str | None = None
) -> bytes:
    _guard_artifact_secret(content, token)
    if path.exists():
        try:
            existing = path.read_bytes()
        except OSError as exc:
            raise AlphaFeasibilityDataError("existing_artifact_unreadable") from exc
        if existing != content:
            raise AlphaFeasibilityDataError("existing_artifact_content_mismatch")
        return existing
    _write_create_only(path, content)
    return content


@dataclass(frozen=True, slots=True)
class TaskExecutionResult:
    task: CollectionTask
    rows: tuple[Mapping[str, Any], ...]
    raw_response_sha256: str
    replayed: bool
    raw_response_persisted: bool
    isolated_future_delist_date_count: int
    isolated_non_union_row_count: int
    wire_response_sha256: str
    response_artifact_sha256: str
    transport_receipt: Mapping[str, Any]
    observed_data_fields: tuple[str, ...] = ()
    required_data_fields: tuple[str, ...] = ()
    missing_required_data_fields: tuple[str, ...] = ()
    extra_data_fields: tuple[str, ...] = ()
    field_order_matches_canonical: bool = False
    provider_payload_sha256: str | None = None
    extra_data_field_value_sha256_by_field: Mapping[str, str] | None = None
    data_row_count: int = 0
    request_origin: str = "network"
    network_request_count: int = 1

    @property
    def raw_transport_sha256(self) -> str:
        key = (
            "raw_transport_sha256"
            if "raw_transport_sha256" in self.transport_receipt
            else "response_body_sha256"
        )
        return str(self.transport_receipt[key])

    @property
    def accepted_root_fields(self) -> tuple[str, ...]:
        key = (
            "observed_root_fields"
            if "observed_root_fields" in self.transport_receipt
            else "accepted_root_fields"
        )
        return tuple(self.transport_receipt[key])

    @property
    def request_id_present(self) -> bool:
        if "transport_extension_field_names" in self.transport_receipt:
            return "request_id" in self.transport_receipt[
                "transport_extension_field_names"
            ]
        return bool(self.transport_receipt["request_id_present"])

    @property
    def detail_present(self) -> bool:
        return (
            "transport_extension_field_names" in self.transport_receipt
            and "detail" in self.transport_receipt["transport_extension_field_names"]
        )

    @property
    def normalized_content_sha256(self) -> str:
        return _normalized_content_sha256(self.task, self.rows)

    @property
    def transport_extensions_sha256(self) -> str | None:
        value = self.transport_receipt.get("transport_extensions_sha256")
        return str(value) if value is not None else None


def _validate_transport_receipt(value: Any) -> Mapping[str, Any]:
    legacy_expected = {
        "http_status",
        "response_byte_count",
        "response_body_sha256",
        "accepted_root_fields",
        "request_id_present",
        "request_id_sha256",
        "token_leak_check",
    }
    expected = {
        "observed_root_fields",
        "semantic_core_fields",
        "transport_extension_field_names",
        "transport_extension_type_by_field",
        "transport_extension_value_sha256_by_field",
        "transport_extensions_sha256",
        "transport_extensions_byte_count",
        "raw_transport_sha256",
        "token_leak_check",
    }
    if not isinstance(value, Mapping) or set(value) not in (expected, legacy_expected):
        raise AlphaFeasibilityDataError("transport_receipt_fields_mismatch")
    if set(value) == expected:
        root_fields = value["observed_root_fields"]
        semantic_fields = value["semantic_core_fields"]
        extension_fields = value["transport_extension_field_names"]
        extension_types = value["transport_extension_type_by_field"]
        extension_hashes = value["transport_extension_value_sha256_by_field"]
        allowed_types = {"null", "boolean", "integer", "number", "string", "array", "object"}
        if (
            not isinstance(root_fields, list)
            or any(type(field) is not str for field in root_fields)
            or root_fields != sorted(set(root_fields))
            or semantic_fields != sorted(REQUIRED_RESPONSE_ROOT_FIELDS)
            or not isinstance(extension_fields, list)
            or any(type(field) is not str for field in extension_fields)
            or extension_fields != sorted(set(extension_fields))
            or len(extension_fields) > MAXIMUM_TRANSPORT_EXTENSION_FIELDS
            or any(
                _SAFE_TRANSPORT_EXTENSION_FIELD.fullmatch(field) is None
                or _SECRET_TRANSPORT_KEY.search(field) is not None
                for field in extension_fields
            )
            or set(root_fields) != REQUIRED_RESPONSE_ROOT_FIELDS | set(extension_fields)
            or not isinstance(extension_types, Mapping)
            or set(extension_types) != set(extension_fields)
            or any(type(item) is not str or item not in allowed_types for item in extension_types.values())
            or not isinstance(extension_hashes, Mapping)
            or set(extension_hashes) != set(extension_fields)
            or any(type(item) is not str or _SHA256.fullmatch(item) is None for item in extension_hashes.values())
            or type(value["transport_extensions_sha256"]) is not str
            or _SHA256.fullmatch(value["transport_extensions_sha256"]) is None
            or type(value["transport_extensions_byte_count"]) is not int
            or not 2 <= value["transport_extensions_byte_count"] <= MAXIMUM_TRANSPORT_EXTENSIONS_BYTES
            or (
                not extension_fields
                and (
                    value["transport_extensions_sha256"]
                    != hashlib.sha256(b"{}").hexdigest()
                    or value["transport_extensions_byte_count"] != 2
                )
            )
            or type(value["raw_transport_sha256"]) is not str
            or _SHA256.fullmatch(value["raw_transport_sha256"]) is None
            or value["token_leak_check"] != "PASSED"
        ):
            raise AlphaFeasibilityDataError("transport_receipt_semantics_mismatch")
        return MappingProxyType(dict(value))

    root_fields = value["accepted_root_fields"]
    request_id_present = value["request_id_present"]
    request_id_sha256 = value["request_id_sha256"]
    if (
        value["http_status"] != 200
        or type(value["response_byte_count"]) is not int
        or value["response_byte_count"] <= 0
        or type(value["response_body_sha256"]) is not str
        or _SHA256.fullmatch(value["response_body_sha256"]) is None
        or not isinstance(root_fields, list)
        or any(type(field) is not str for field in root_fields)
        or root_fields != sorted(set(root_fields))
        or set(root_fields)
        not in (
            REQUIRED_RESPONSE_ROOT_FIELDS,
            REQUIRED_RESPONSE_ROOT_FIELDS | {"request_id"},
        )
        or type(request_id_present) is not bool
        or request_id_present != ("request_id" in root_fields)
        or (
            request_id_present
            and (
                type(request_id_sha256) is not str
                or _SHA256.fullmatch(request_id_sha256) is None
            )
        )
        or (not request_id_present and request_id_sha256 is not None)
        or value["token_leak_check"] != "PASSED"
    ):
        raise AlphaFeasibilityDataError("transport_receipt_semantics_mismatch")
    return MappingProxyType(dict(value))


def _transport_receipt_payload(validated: ValidatedResponse) -> dict[str, Any]:
    receipt = {
        "observed_root_fields": list(validated.observed_root_fields),
        "semantic_core_fields": list(validated.semantic_core_fields),
        "transport_extension_field_names": list(
            validated.transport_extension_field_names
        ),
        "transport_extension_type_by_field": dict(
            validated.transport_extension_type_by_field
        ),
        "transport_extension_value_sha256_by_field": dict(
            validated.transport_extension_value_sha256_by_field
        ),
        "transport_extensions_sha256": validated.transport_extensions_sha256,
        "transport_extensions_byte_count": validated.transport_extensions_byte_count,
        "raw_transport_sha256": validated.raw_response_sha256,
        "token_leak_check": "PASSED",
    }
    _validate_transport_receipt(receipt)
    return receipt


def _response_payload(
    task: CollectionTask,
    validated: ValidatedResponse,
    *,
    persisted_sha256: str,
) -> dict[str, Any]:
    rows = [dict(row) for row in validated.rows]
    response = {
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "state": "RESPONSE_VALIDATED",
        "task_id": task.task_id,
        "endpoint": task.endpoint,
        "plan_sha256": task.plan_sha256,
        "raw_response_sha256": persisted_sha256,
        "wire_response_sha256": validated.raw_response_sha256,
        "transport_receipt": _transport_receipt_payload(validated),
        "raw_response_persisted": True,
        "normalized_rows_sha256": canonical_sha256(rows),
        "normalized_content_sha256": validated.normalized_content_sha256,
        "observed_data_fields": list(validated.observed_data_fields),
        "required_data_fields": list(validated.required_data_fields),
        "missing_required_data_fields": list(validated.missing_required_data_fields),
        "extra_data_fields": list(validated.extra_data_fields),
        "field_order_matches_canonical": validated.field_order_matches_canonical,
        "provider_payload_sha256": validated.provider_payload_sha256,
        "extra_data_field_value_sha256_by_field": dict(
            validated.extra_data_field_value_sha256_by_field
        ),
        "data_row_count": validated.data_row_count,
        "row_count": len(rows),
        "isolated_future_delist_date_count": (
            validated.isolated_future_delist_date_count
        ),
        "isolated_non_union_row_count": validated.isolated_non_union_row_count,
        "rows": rows,
        "locked_test_status": dict(LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
    }
    response["response_artifact_sha256"] = canonical_sha256(response)
    try:
        validate_json_schema(response, RESPONSE_SCHEMA_PATH)
    except SchemaValidationError as exc:
        raise AlphaFeasibilityDataError("response_artifact_schema_invalid") from exc
    return response


class CreateOnlyTaskStore:
    """Create-only request journal and normalized response store."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def started_path(self, task: CollectionTask) -> Path:
        return self.root / "tasks" / f"{task.task_id}.started.json"

    def response_path(self, task: CollectionTask) -> Path:
        return self.root / "tasks" / f"{task.task_id}.response.json"

    def import_path(self, task: CollectionTask) -> Path:
        return self.root / "tasks" / f"{task.task_id}.import.json"

    def attempt_path(self, task: CollectionTask, attempt_number: int) -> Path:
        return (
            self.root
            / "attempts"
            / task.task_id
            / f"{attempt_number:06d}.started.json"
        )

    def quarantine_path(self, task: CollectionTask) -> Path:
        return self.root / "quarantine" / f"{task.task_id}.json"

    def raw_path(self, task: CollectionTask) -> Path:
        return self.root / "raw" / f"{task.task_id}.json"

    def raw_error_path(self, task: CollectionTask, attempt_number: int) -> Path:
        return (
            self.root
            / "raw_errors"
            / task.task_id
            / f"{attempt_number:06d}.json"
        )

    def business_error_path(
        self, task: CollectionTask, attempt_number: int
    ) -> Path:
        return (
            self.root
            / "business_errors"
            / task.task_id
            / f"{attempt_number:06d}.json"
        )

    def is_complete(self, task: CollectionTask) -> bool:
        provenance_count = sum(
            path.is_file() for path in (self.started_path(task), self.import_path(task))
        )
        return provenance_count == 1 and self.response_path(task).is_file()

    def _load_started(self, task: CollectionTask) -> Mapping[str, Any]:
        try:
            value = strict_json_loads(self.started_path(task).read_bytes(), label="started_artifact")
        except OSError as exc:
            raise AlphaFeasibilityDataError("started_artifact_unreadable") from exc
        if not isinstance(value, Mapping) or value not in (
            _started_payload(task, recoverable=False),
            _started_payload(task, recoverable=True),
        ):
            raise AlphaFeasibilityDataError("started_artifact_semantics_mismatch")
        return value

    def _load_import(self, task: CollectionTask) -> Mapping[str, Any]:
        try:
            value = strict_json_loads(
                self.import_path(task).read_bytes(), label="import_artifact"
            )
        except OSError as exc:
            raise AlphaFeasibilityDataError("import_artifact_unreadable") from exc
        if not isinstance(value, Mapping):
            raise AlphaFeasibilityDataError("import_artifact_invalid")
        schema_version = value.get("schema_version")
        schema_path = (
            IMPORT_SCHEMA_PATH
            if schema_version == IMPORT_SCHEMA_VERSION
            else PARENT_REUSE_IMPORT_SCHEMA_PATH
            if schema_version == PARENT_REUSE_IMPORT_SCHEMA_VERSION
            else None
        )
        if schema_path is None:
            raise AlphaFeasibilityDataError("import_artifact_schema_invalid")
        try:
            validate_json_schema(value, schema_path)
        except SchemaValidationError as exc:
            raise AlphaFeasibilityDataError("import_artifact_schema_invalid") from exc
        unsigned = dict(value)
        declared = unsigned.pop("import_artifact_sha256", None)
        if (
            value.get("task_id") != task.task_id
            or value.get("endpoint") != task.endpoint
            or value.get("plan_sha256") != task.plan_sha256
            or value.get("task") != task.to_dict()
            or value.get("network_request_count") != 0
            or declared != canonical_sha256(unsigned)
        ):
            raise AlphaFeasibilityDataError("import_artifact_semantics_mismatch")
        if schema_version == PARENT_REUSE_IMPORT_SCHEMA_VERSION:
            try:
                raw_bytes = self.raw_path(task).read_bytes()
                response_bytes = self.response_path(task).read_bytes()
                response = strict_json_loads(
                    response_bytes, label="parent_reuse_response_artifact"
                )
            except OSError as exc:
                raise AlphaFeasibilityDataError(
                    "parent_reuse_artifact_unreadable"
                ) from exc
            if not isinstance(response, Mapping):
                raise AlphaFeasibilityDataError(
                    "parent_reuse_response_artifact_invalid"
                )
            response_unsigned = dict(response)
            response_artifact_sha256 = response_unsigned.pop(
                "response_artifact_sha256", None
            )
            raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
            response_file_sha256 = hashlib.sha256(response_bytes).hexdigest()
            parent_kind = value.get("parent_provenance_kind")
            parent_attempt_hashes = value.get(
                "parent_attempt_artifact_sha256_by_number"
            )
            parent_request_count = value.get("parent_network_request_count")
            if (
                value.get("state") != "PARENT_RUN_TASK_REUSED"
                or value.get("task_sha256") != canonical_sha256(task.to_dict())
                or value.get("parent_raw_artifact_sha256") != raw_sha256
                or value.get("raw_transport_sha256") != raw_sha256
                or value.get("parent_response_file_sha256")
                != response_file_sha256
                or value.get("parent_response_artifact_sha256")
                != response_artifact_sha256
                or response_artifact_sha256 != canonical_sha256(response_unsigned)
                or response.get("task_id") != task.task_id
                or response.get("endpoint") != task.endpoint
                or response.get("plan_sha256") != task.plan_sha256
                or response.get("raw_response_sha256") != raw_sha256
                or response.get("normalized_content_sha256")
                != value.get("normalized_content_sha256")
                or not isinstance(parent_attempt_hashes, list)
                or (
                    parent_kind == "offline_p14d_import"
                    and (parent_request_count != 0 or parent_attempt_hashes)
                )
                or (
                    parent_kind == "network"
                    and (
                        type(parent_request_count) is not int
                        or parent_request_count < 1
                        or len(parent_attempt_hashes) != parent_request_count
                    )
                )
            ):
                raise AlphaFeasibilityDataError(
                    "parent_reuse_import_semantics_mismatch"
                )
        return value

    def _load_attempts(self, task: CollectionTask) -> tuple[Mapping[str, Any], ...]:
        directory = self.root / "attempts" / task.task_id
        if not directory.exists():
            return ()
        if not directory.is_dir() or directory.is_symlink():
            raise AlphaFeasibilityDataError("attempt_journal_invalid")
        paths = sorted(directory.iterdir())
        if any(not path.is_file() or path.is_symlink() for path in paths):
            raise AlphaFeasibilityDataError("attempt_journal_invalid")
        attempts: list[Mapping[str, Any]] = []
        for number, path in enumerate(paths, start=1):
            expected_path = self.attempt_path(task, number)
            if path != expected_path:
                raise AlphaFeasibilityDataError("attempt_journal_not_contiguous")
            try:
                value = strict_json_loads(path.read_bytes(), label="attempt_artifact")
            except OSError as exc:
                raise AlphaFeasibilityDataError("attempt_artifact_unreadable") from exc
            if not isinstance(value, Mapping):
                raise AlphaFeasibilityDataError("attempt_artifact_invalid")
            try:
                validate_json_schema(value, ATTEMPT_SCHEMA_PATH)
            except SchemaValidationError as exc:
                raise AlphaFeasibilityDataError("attempt_artifact_schema_invalid") from exc
            if dict(value) != _attempt_payload(task, number):
                raise AlphaFeasibilityDataError("attempt_artifact_semantics_mismatch")
            attempts.append(value)
        return tuple(attempts)

    def _load_business_errors(
        self, task: CollectionTask
    ) -> tuple[Mapping[str, Any], ...]:
        directory = self.root / "business_errors" / task.task_id
        raw_directory = self.root / "raw_errors" / task.task_id
        if not directory.exists():
            if raw_directory.exists():
                if (
                    not raw_directory.is_dir()
                    or raw_directory.is_symlink()
                    or any(raw_directory.iterdir())
                ):
                    raise AlphaFeasibilityDataError(
                        "business_error_journal_incomplete"
                    )
            return ()
        if not directory.is_dir() or directory.is_symlink():
            raise AlphaFeasibilityDataError("business_error_journal_invalid")
        paths = sorted(directory.iterdir())
        if any(not path.is_file() or path.is_symlink() for path in paths):
            raise AlphaFeasibilityDataError("business_error_journal_invalid")
        errors: list[Mapping[str, Any]] = []
        seen_attempts: set[int] = set()
        for path in paths:
            match = re.fullmatch(r"([0-9]{6})\.json", path.name)
            if match is None:
                raise AlphaFeasibilityDataError("business_error_journal_invalid")
            attempt_number = int(match.group(1))
            if attempt_number < 1 or attempt_number in seen_attempts:
                raise AlphaFeasibilityDataError("business_error_journal_invalid")
            seen_attempts.add(attempt_number)
            if path != self.business_error_path(task, attempt_number):
                raise AlphaFeasibilityDataError("business_error_journal_invalid")
            if not self.attempt_path(task, attempt_number).is_file():
                raise AlphaFeasibilityDataError("business_error_without_attempt_claim")
            try:
                value = strict_json_loads(
                    path.read_bytes(), label="business_error_artifact"
                )
            except OSError as exc:
                raise AlphaFeasibilityDataError(
                    "business_error_artifact_unreadable"
                ) from exc
            if not isinstance(value, Mapping):
                raise AlphaFeasibilityDataError("business_error_artifact_invalid")
            try:
                validate_json_schema(value, BUSINESS_ERROR_SCHEMA_PATH)
            except SchemaValidationError as exc:
                raise AlphaFeasibilityDataError(
                    "business_error_artifact_schema_invalid"
                ) from exc
            unsigned = dict(value)
            declared = unsigned.pop("business_error_artifact_sha256", None)
            evidence = value.get("evidence")
            if not isinstance(evidence, Mapping):
                raise AlphaFeasibilityDataError("business_error_evidence_invalid")
            semantics = _safe_response_semantics_from_mapping(evidence)
            raw_path = self.raw_error_path(task, attempt_number)
            try:
                raw_error = raw_path.read_bytes()
                raw_sha256 = hashlib.sha256(raw_error).hexdigest()
            except OSError as exc:
                raise AlphaFeasibilityDataError(
                    "business_error_raw_artifact_unreadable"
                ) from exc
            if (
                value.get("task_id") != task.task_id
                or value.get("endpoint") != task.endpoint
                or value.get("plan_sha256") != task.plan_sha256
                or value.get("task_sha256") != canonical_sha256(task.to_dict())
                or value.get("attempt_number") != attempt_number
                or value.get("network_request_count") != 1
                or value.get("request_count_semantics")
                != P15_REQUEST_COUNT_SEMANTICS
                or value.get("raw_error_artifact_sha256") != raw_sha256
                or evidence.get("response_body_sha256") != raw_sha256
                or evidence.get("raw_transport_sha256") != raw_sha256
                or evidence.get("response_byte_count") != len(raw_error)
                or evidence.get("sanitized_params") != dict(task.params)
                or evidence.get("requested_fields") != list(task.fields)
                or declared != canonical_sha256(unsigned)
            ):
                raise AlphaFeasibilityDataError(
                    "business_error_artifact_semantics_mismatch"
                )
            replayed_semantics = extract_safe_response_semantics(
                raw_error,
                task=task,
                token=None,
                requested_at=semantics.requested_at,
                completed_at=semantics.completed_at,
            )
            if replayed_semantics.to_dict() != dict(evidence):
                raise AlphaFeasibilityDataError(
                    "business_error_artifact_semantics_mismatch"
                )
            errors.append(value)
        if (
            not raw_directory.is_dir()
            or raw_directory.is_symlink()
            or {path.name for path in raw_directory.iterdir()}
            != {f"{item['attempt_number']:06d}.json" for item in errors}
        ):
            raise AlphaFeasibilityDataError("business_error_journal_incomplete")
        return tuple(sorted(errors, key=lambda item: item["attempt_number"]))

    def _terminal_quarantine_code(self, task: CollectionTask) -> str | None:
        path = self.quarantine_path(task)
        if not path.exists():
            return None
        try:
            value = strict_json_loads(path.read_bytes(), label="quarantine_artifact")
        except OSError as exc:
            raise AlphaFeasibilityDataError("quarantine_artifact_unreadable") from exc
        if not isinstance(value, Mapping):
            raise AlphaFeasibilityDataError("quarantine_artifact_invalid")
        schema_version = value.get("schema_version")
        schema_path = QUARANTINE_SCHEMA_PATHS.get(schema_version)
        if schema_path is None:
            raise AlphaFeasibilityDataError("quarantine_artifact_schema_invalid")
        try:
            validate_json_schema(value, schema_path)
        except SchemaValidationError as exc:
            raise AlphaFeasibilityDataError("quarantine_artifact_schema_invalid") from exc
        if (
            value.get("task_id") != task.task_id
            or value.get("endpoint") != task.endpoint
            or value.get("plan_sha256") != task.plan_sha256
        ):
            raise AlphaFeasibilityDataError("quarantine_artifact_semantics_mismatch")
        code = value.get("failure_code")
        if type(code) is not str:
            raise AlphaFeasibilityDataError("quarantine_artifact_invalid")
        if schema_version == QUARANTINE_SCHEMA_VERSION:
            if value.get("state") == "TRANSPORT_ATTEMPT_QUARANTINED_NO_RESPONSE":
                attempts = self._load_attempts(task)
                if (
                    code not in RETRYABLE_ATTEMPT_FAILURES
                    or value.get("reason") != code
                    or value.get("terminal_attempt_number") != len(attempts)
                    or not attempts
                ):
                    raise AlphaFeasibilityDataError(
                        "quarantine_artifact_semantics_mismatch"
                    )
            evidence = value.get("business_error_evidence")
            classification = value.get("business_error_classification")
            attempt_number = value.get("terminal_attempt_number")
            artifact_sha256 = value.get("business_error_artifact_sha256")
            raw_persisted = value.get("raw_response_persisted")
            if evidence is None:
                if classification is not None or artifact_sha256 is not None or raw_persisted:
                    raise AlphaFeasibilityDataError(
                        "quarantine_artifact_semantics_mismatch"
                    )
            else:
                safe_semantics = _safe_response_semantics_from_mapping(evidence)
                if (
                    not isinstance(evidence, Mapping)
                    or classification != evidence.get("classification")
                    or value.get("upstream_code") != evidence.get("business_code")
                    or value.get("raw_transport_sha256")
                    != evidence.get("response_body_sha256")
                    or evidence.get("raw_transport_sha256")
                    != evidence.get("response_body_sha256")
                    or evidence.get("sanitized_params") != dict(task.params)
                    or evidence.get("requested_fields") != list(task.fields)
                    or safe_semantics.classification != classification
                ):
                    raise AlphaFeasibilityDataError(
                        "quarantine_artifact_semantics_mismatch"
                    )
                if raw_persisted:
                    errors = {
                        item["attempt_number"]: item
                        for item in self._load_business_errors(task)
                    }
                    artifact = errors.get(attempt_number)
                    if (
                        artifact is None
                        or artifact.get("business_error_artifact_sha256")
                        != artifact_sha256
                        or artifact.get("evidence") != evidence
                    ):
                        raise AlphaFeasibilityDataError(
                            "quarantine_artifact_semantics_mismatch"
                        )
                elif artifact_sha256 is not None:
                    raise AlphaFeasibilityDataError(
                        "quarantine_artifact_semantics_mismatch"
                    )
        return code

    def _load_response(self, task: CollectionTask) -> TaskExecutionResult:
        started_exists = self.started_path(task).is_file()
        import_exists = self.import_path(task).is_file()
        if started_exists == import_exists:
            raise AlphaFeasibilityDataError("response_provenance_invalid")
        if import_exists:
            imported = self._load_import(task)
            request_origin = (
                "offline_parent_run_reuse"
                if imported.get("schema_version")
                == PARENT_REUSE_IMPORT_SCHEMA_VERSION
                else "offline_p14d_import"
            )
            network_request_count = 0
        else:
            started = self._load_started(task)
            request_origin = "network"
            network_request_count = (
                len(self._load_attempts(task))
                if started.get("schema_version") == RECOVERABLE_STARTED_SCHEMA_VERSION
                else 1
            )
        try:
            raw = self.response_path(task).read_bytes()
            value = strict_json_loads(raw, label="response_artifact")
        except OSError as exc:
            raise AlphaFeasibilityDataError("response_artifact_unreadable") from exc
        if not isinstance(value, Mapping):
            raise AlphaFeasibilityDataError("response_artifact_not_object")
        expected_keys = {
            "schema_version",
            "state",
            "task_id",
            "endpoint",
            "plan_sha256",
            "raw_response_sha256",
            "wire_response_sha256",
            "transport_receipt",
            "raw_response_persisted",
            "normalized_rows_sha256",
            "row_count",
            "isolated_future_delist_date_count",
            "isolated_non_union_row_count",
            "rows",
            "locked_test_status",
            "locked_test_consumed",
            "response_artifact_sha256",
        }
        normalized_hash_keys = {"normalized_content_sha256"}
        data_evidence_keys = {
            "observed_data_fields",
            "required_data_fields",
            "missing_required_data_fields",
            "extra_data_fields",
            "field_order_matches_canonical",
            "provider_payload_sha256",
            "extra_data_field_value_sha256_by_field",
            "data_row_count",
        }
        response_schema = value.get("schema_version")
        if response_schema == RESPONSE_SCHEMA_VERSION:
            expected_keys_for_schema = expected_keys | normalized_hash_keys | data_evidence_keys
        elif response_schema == "tushare-alpha-feasibility-task-response.v3":
            expected_keys_for_schema = expected_keys | normalized_hash_keys
        else:
            expected_keys_for_schema = expected_keys
        if set(value) != expected_keys_for_schema:
            raise AlphaFeasibilityDataError("response_artifact_fields_mismatch")
        if response_schema == RESPONSE_SCHEMA_VERSION:
            try:
                validate_json_schema(value, RESPONSE_SCHEMA_PATH)
            except SchemaValidationError as exc:
                raise AlphaFeasibilityDataError("response_artifact_schema_invalid") from exc
            _validated_data_evidence(value, task)
        transport_receipt = _validate_transport_receipt(value["transport_receipt"])
        receipt_is_legacy = "response_body_sha256" in transport_receipt
        receipt_wire_sha256 = transport_receipt[
            "response_body_sha256" if receipt_is_legacy else "raw_transport_sha256"
        ]
        if (
            response_schema
            not in ({RESPONSE_SCHEMA_VERSION} | LEGACY_RESPONSE_SCHEMA_VERSIONS)
            or receipt_is_legacy
            != (response_schema == "tushare-alpha-feasibility-task-response.v2")
            or value["state"] != "RESPONSE_VALIDATED"
            or value["task_id"] != task.task_id
            or value["endpoint"] != task.endpoint
            or value["plan_sha256"] != task.plan_sha256
            or type(value["raw_response_sha256"]) is not str
            or _SHA256.fullmatch(value["raw_response_sha256"]) is None
            or type(value["wire_response_sha256"]) is not str
            or _SHA256.fullmatch(value["wire_response_sha256"]) is None
            or value["wire_response_sha256"] != receipt_wire_sha256
            or type(value["raw_response_persisted"]) is not bool
            or type(value["isolated_future_delist_date_count"]) is not int
            or value["isolated_future_delist_date_count"] < 0
            or type(value["isolated_non_union_row_count"]) is not int
            or value["isolated_non_union_row_count"] < 0
            or value["locked_test_status"] != dict(LOCKED_TEST_STATUS)
            or value["locked_test_consumed"] is not False
            or not isinstance(value["rows"], list)
            or value["row_count"] != len(value["rows"])
            or value["normalized_rows_sha256"] != canonical_sha256(value["rows"])
            or (
                response_schema
                in {
                    RESPONSE_SCHEMA_VERSION,
                    "tushare-alpha-feasibility-task-response.v3",
                }
                and value["normalized_content_sha256"]
                != _normalized_content_sha256(task, value["rows"])
            )
        ):
            raise AlphaFeasibilityDataError("response_artifact_semantics_mismatch")
        unsigned_response = dict(value)
        declared_response_hash = unsigned_response.pop("response_artifact_sha256", None)
        if (
            type(declared_response_hash) is not str
            or _SHA256.fullmatch(declared_response_hash) is None
            or declared_response_hash != canonical_sha256(unsigned_response)
        ):
            raise AlphaFeasibilityDataError("response_artifact_hash_mismatch")
        if response_schema == RESPONSE_SCHEMA_VERSION:
            observed_data_fields = tuple(value["observed_data_fields"])
            required_data_fields = tuple(value["required_data_fields"])
            missing_required_data_fields = tuple(value["missing_required_data_fields"])
            extra_data_fields = tuple(value["extra_data_fields"])
            field_order_matches_canonical = value["field_order_matches_canonical"]
            provider_payload_sha256 = value["provider_payload_sha256"]
            extra_data_field_value_sha256_by_field = MappingProxyType(
                dict(value["extra_data_field_value_sha256_by_field"])
            )
            data_row_count = value["data_row_count"]
        else:
            observed_data_fields = task.fields
            required_data_fields = task.fields
            missing_required_data_fields = ()
            extra_data_fields = ()
            field_order_matches_canonical = True
            provider_payload_sha256 = None
            extra_data_field_value_sha256_by_field = MappingProxyType({})
            data_row_count = len(value["rows"])
        if value["raw_response_persisted"]:
            try:
                persisted_raw = self.raw_path(task).read_bytes()
            except OSError as exc:
                raise AlphaFeasibilityDataError("raw_response_artifact_unavailable") from exc
            if hashlib.sha256(persisted_raw).hexdigest() != value["raw_response_sha256"]:
                raise AlphaFeasibilityDataError("raw_response_hash_mismatch")
            if (
                task.endpoint != "stock_basic"
                and not (
                    transport_receipt["request_id_present"]
                    if receipt_is_legacy
                    else transport_receipt["transport_extension_field_names"]
                )
                and value["wire_response_sha256"] != value["raw_response_sha256"]
            ):
                raise AlphaFeasibilityDataError("wire_response_hash_mismatch")
            replay = validate_response_bytes(task, persisted_raw)
            if [dict(row) for row in replay.rows] != value["rows"]:
                raise AlphaFeasibilityDataError("raw_response_replay_mismatch")
            if response_schema == RESPONSE_SCHEMA_VERSION:
                replay_evidence = {
                    "observed_data_fields": list(replay.observed_data_fields),
                    "required_data_fields": list(replay.required_data_fields),
                    "missing_required_data_fields": list(
                        replay.missing_required_data_fields
                    ),
                    "extra_data_fields": list(replay.extra_data_fields),
                    "field_order_matches_canonical": replay.field_order_matches_canonical,
                    "provider_payload_sha256": replay.provider_payload_sha256,
                    "extra_data_field_value_sha256_by_field": dict(
                        replay.extra_data_field_value_sha256_by_field
                    ),
                    "data_row_count": replay.data_row_count,
                }
                if any(value[key] != replay_evidence[key] for key in data_evidence_keys):
                    raise AlphaFeasibilityDataError("raw_response_data_evidence_mismatch")
        else:
            if self.raw_path(task).exists():
                raise AlphaFeasibilityDataError("unexpected_raw_response_artifact")
            if task.endpoint != "stock_basic" and value["isolated_future_delist_date_count"] == 0:
                raise AlphaFeasibilityDataError("missing_required_raw_response_artifact")
            seen: set[tuple[Any, ...]] = set()
            for persisted_row in value["rows"]:
                if not isinstance(persisted_row, Mapping) or set(persisted_row) != set(task.fields):
                    raise AlphaFeasibilityDataError("normalized_row_fields_mismatch")
                replayed_row, isolated = _normalize_response_row(task, persisted_row)
                if replayed_row is None or isolated:
                    raise AlphaFeasibilityDataError("normalized_row_replay_mismatch")
                key = _primary_key(task.endpoint, replayed_row)
                if key in seen or dict(replayed_row) != dict(persisted_row):
                    raise AlphaFeasibilityDataError("normalized_row_replay_mismatch")
                seen.add(key)
        return TaskExecutionResult(
            task=task,
            rows=tuple(MappingProxyType(dict(row)) for row in value["rows"]),
            raw_response_sha256=value["raw_response_sha256"],
            replayed=True,
            raw_response_persisted=value["raw_response_persisted"],
            isolated_future_delist_date_count=value["isolated_future_delist_date_count"],
            isolated_non_union_row_count=value["isolated_non_union_row_count"],
            wire_response_sha256=value["wire_response_sha256"],
            response_artifact_sha256=value["response_artifact_sha256"],
            transport_receipt=transport_receipt,
            observed_data_fields=observed_data_fields,
            required_data_fields=required_data_fields,
            missing_required_data_fields=missing_required_data_fields,
            extra_data_fields=extra_data_fields,
            field_order_matches_canonical=field_order_matches_canonical,
            provider_payload_sha256=provider_payload_sha256,
            extra_data_field_value_sha256_by_field=(
                extra_data_field_value_sha256_by_field
            ),
            data_row_count=data_row_count,
            request_origin=request_origin,
            network_request_count=network_request_count,
        )

    def execute(
        self,
        task: CollectionTask,
        *,
        token: str,
        transport: TushareTransport,
        timeout_seconds: int,
        maximum_response_bytes: int,
        recover_interrupted_attempts: bool = False,
        maximum_attempts_per_fingerprint: int = 1,
        persist_full_raw_transport: bool = False,
        clock: Callable[[], datetime] = _utc_now,
        authorized_business_retry_parent_attempt: int | None = None,
        defer_retryable_business_errors: bool = False,
        terminalize_transport_interruptions: bool = False,
    ) -> TaskExecutionResult:
        # Revalidate even frozen dataclasses at the store boundary so a forged
        # or deserialized task cannot create a durable started claim or reach
        # an injected transport.
        _validate_collection_task_contract(task)
        started_exists = self.started_path(task).exists()
        import_exists = self.import_path(task).exists()
        response_exists = self.response_path(task).exists()
        if started_exists and import_exists:
            raise AlphaFeasibilityDataError("multiple_response_provenance_artifacts")
        if response_exists:
            if not (started_exists or import_exists):
                raise AlphaFeasibilityDataError("response_without_provenance_artifact")
            return self._load_response(task)
        if import_exists:
            self._load_import(task)
            raise AlphaFeasibilityDataError("incomplete_import_artifact")
        if type(recover_interrupted_attempts) is not bool:
            raise AlphaFeasibilityDataError("invalid_attempt_recovery_setting")
        if (
            type(maximum_attempts_per_fingerprint) is not int
            or not 1 <= maximum_attempts_per_fingerprint <= 10
        ):
            raise AlphaFeasibilityDataError("invalid_maximum_attempts")
        if type(persist_full_raw_transport) is not bool:
            raise AlphaFeasibilityDataError("invalid_raw_persistence_setting")
        if type(defer_retryable_business_errors) is not bool:
            raise AlphaFeasibilityDataError("invalid_business_retry_setting")
        if type(terminalize_transport_interruptions) is not bool:
            raise AlphaFeasibilityDataError("invalid_transport_interruption_setting")
        if terminalize_transport_interruptions and (
            not recover_interrupted_attempts or not persist_full_raw_transport
        ):
            raise AlphaFeasibilityDataError("invalid_transport_interruption_setting")
        if not callable(clock):
            raise AlphaFeasibilityDataError("invalid_business_retry_clock")
        if authorized_business_retry_parent_attempt is not None and (
            type(authorized_business_retry_parent_attempt) is not int
            or authorized_business_retry_parent_attempt < 1
        ):
            raise AlphaFeasibilityDataError("invalid_business_retry_authorization")
        _validate_token(token)

        attempts: tuple[Mapping[str, Any], ...] = ()
        business_errors: tuple[Mapping[str, Any], ...] = ()
        if started_exists:
            started = self._load_started(task)
            recoverable_started = (
                started.get("schema_version") == RECOVERABLE_STARTED_SCHEMA_VERSION
            )
            if not recoverable_started or not recover_interrupted_attempts:
                raise AmbiguousRemoteExecutionError("ambiguous_started_without_response")
            terminal_code = self._terminal_quarantine_code(task)
            if terminal_code is not None:
                raise AlphaFeasibilityDataError(terminal_code)
            attempts = self._load_attempts(task)
            business_errors = self._load_business_errors(task)
            if business_errors:
                last_business_error = business_errors[-1]
                parent_attempt = last_business_error["attempt_number"]
                if authorized_business_retry_parent_attempt != parent_attempt:
                    raise AlphaFeasibilityDataError(
                        "business_error_retry_authorization_required"
                    )
                if len(attempts) != parent_attempt:
                    raise AlphaFeasibilityDataError(
                        "business_error_retry_already_consumed"
                    )
                evidence = last_business_error["evidence"]
                policy = business_error_retry_policy(
                    evidence["classification"],
                    retry_after_seconds=evidence["retry_after_seconds"],
                )
                if policy.maximum_additional_attempts != 1:
                    raise AlphaFeasibilityDataError(
                        "business_error_not_retryable"
                    )
            elif authorized_business_retry_parent_attempt is not None:
                raise AlphaFeasibilityDataError(
                    "business_error_retry_parent_missing"
                )
            if self.raw_path(task).exists():
                try:
                    persisted_raw = self.raw_path(task).read_bytes()
                except OSError as exc:
                    raise AlphaFeasibilityDataError(
                        "raw_response_artifact_unavailable"
                    ) from exc
                recovered = validate_response_bytes(
                    task,
                    persisted_raw,
                    token=token,
                    maximum_response_bytes=maximum_response_bytes,
                    http_status=200,
                    require_full_raw_safety=persist_full_raw_transport,
                )
                response = _response_payload(
                    task,
                    recovered,
                    persisted_sha256=hashlib.sha256(persisted_raw).hexdigest(),
                )
                _write_json_create_only(self.response_path(task), response, token=token)
                return self._load_response(task)
            if len(attempts) >= maximum_attempts_per_fingerprint:
                raise AlphaFeasibilityDataError("maximum_attempts_exhausted")
        elif (
            self.raw_path(task).exists()
            or self.quarantine_path(task).exists()
            or (self.root / "business_errors" / task.task_id).exists()
            or (self.root / "raw_errors" / task.task_id).exists()
        ):
            raise AlphaFeasibilityDataError("orphan_task_artifact")
        elif authorized_business_retry_parent_attempt is not None:
            raise AlphaFeasibilityDataError("business_error_retry_parent_missing")

        if not started_exists:
            started = _started_payload(
                task, recoverable=recover_interrupted_attempts
            )
            _write_json_create_only(self.started_path(task), started, token=token)
        attempt_number = len(attempts) + 1 if recover_interrupted_attempts else 1
        if recover_interrupted_attempts:
            _write_json_create_only(
                self.attempt_path(task, attempt_number),
                _attempt_payload(task, attempt_number),
                token=token,
            )
        raw_sha: str | None = None
        http_status: int | None = None
        response_byte_count: int | None = None
        token_leak_check = "NOT_CONFIRMED"
        validated: ValidatedResponse | None = None
        raw: bytes | None = None
        safe_semantics: SafeResponseSemantics | None = None
        try:
            requested_at = _utc_timestamp(clock(), "requested_at")
            transport_response = transport(
                endpoint=task.endpoint,
                params=task.params,
                fields=task.fields,
                token=token,
                timeout_seconds=timeout_seconds,
                maximum_response_bytes=maximum_response_bytes,
            )
            completed_at = _utc_timestamp(clock(), "completed_at")
            if isinstance(transport_response, bytes):
                # Backward-compatible offline injection boundary.  The real
                # HTTPS transport always supplies the observed status.
                transport_response = TushareHttpResponse(
                    http_status=200,
                    body=transport_response,
                )
            if not isinstance(transport_response, TushareHttpResponse):
                raise AlphaFeasibilityDataError("unknown_non_json_value")
            http_status = transport_response.http_status
            raw = transport_response.body
            response_byte_count = len(raw)
            if token.encode("utf-8") not in raw:
                raw_sha = hashlib.sha256(raw).hexdigest()
                token_leak_check = "RAW_BYTES_PASSED"
            if http_status != 200:
                # An HTTP status rejected by the transport contract is not a
                # provider business response.  Bind only transport metadata;
                # never accept or classify message/detail semantics from it.
                raise AlphaFeasibilityDataError("http_status_not_success")
            if persist_full_raw_transport:
                safe_semantics = extract_safe_response_semantics(
                    raw,
                    task=task,
                    token=token,
                    requested_at=requested_at,
                    completed_at=completed_at,
                )
                token_leak_check = "PASSED"
            validated = validate_response_bytes(
                task,
                raw,
                token=token,
                maximum_response_bytes=maximum_response_bytes,
                http_status=http_status,
                require_full_raw_safety=persist_full_raw_transport,
            )
            token_leak_check = "PASSED"
            rows = [dict(row) for row in validated.rows]
            persisted_response = raw
            if (
                not persist_full_raw_transport
                and (
                    task.endpoint == "stock_basic"
                    or validated.transport_extension_field_names
                )
            ):
                # Transport extensions are never normalized data and their raw
                # values do not enter ordinary replay evidence. ``stock_basic``
                # also removes non-union/future metadata under its existing
                # field-aware isolation rule.
                parsed_root = _require_mapping(
                    strict_json_loads(raw, label="response"),
                    "semantic_core_type_invalid",
                )
                parsed_data = _require_mapping(
                    parsed_root.get("data"),
                    "semantic_core_type_invalid",
                )
                persisted_data = {
                    "fields": parsed_data["fields"],
                    "items": parsed_data["items"],
                }
                if "has_more" in parsed_data:
                    persisted_data["has_more"] = parsed_data["has_more"]
                persisted_response = _canonical_transport_json_bytes(
                    {"code": 0, "msg": None, "data": persisted_data}
                )
            _guard_artifact_secret(persisted_response, token)
            _write_create_only(self.raw_path(task), persisted_response)
            persisted_sha = hashlib.sha256(persisted_response).hexdigest()
            response = _response_payload(
                task, validated, persisted_sha256=persisted_sha
            )
            _write_json_create_only(self.response_path(task), response, token=token)
            return TaskExecutionResult(
                task=task,
                rows=validated.rows,
                raw_response_sha256=persisted_sha,
                replayed=False,
                raw_response_persisted=True,
                isolated_future_delist_date_count=validated.isolated_future_delist_date_count,
                isolated_non_union_row_count=validated.isolated_non_union_row_count,
                wire_response_sha256=validated.raw_response_sha256,
                response_artifact_sha256=response["response_artifact_sha256"],
                transport_receipt=MappingProxyType(
                    dict(response["transport_receipt"])
                ),
                observed_data_fields=validated.observed_data_fields,
                required_data_fields=validated.required_data_fields,
                missing_required_data_fields=validated.missing_required_data_fields,
                extra_data_fields=validated.extra_data_fields,
                field_order_matches_canonical=(
                    validated.field_order_matches_canonical
                ),
                provider_payload_sha256=validated.provider_payload_sha256,
                extra_data_field_value_sha256_by_field=(
                    validated.extra_data_field_value_sha256_by_field
                ),
                data_row_count=validated.data_row_count,
                request_origin="network",
                network_request_count=(
                    len(attempts) + 1 if recover_interrupted_attempts else 1
                ),
            )
        except Exception as exc:
            code = exc.code if isinstance(exc, AlphaFeasibilityDataError) else "unclassified_task_failure"
            diagnostic = (
                dict(exc.diagnostic)
                if isinstance(exc, AlphaFeasibilityDataError)
                else {}
            )
            if (
                recover_interrupted_attempts
                and code in RETRYABLE_ATTEMPT_FAILURES
                and not terminalize_transport_interruptions
            ):
                raise
            business_error_artifact: Mapping[str, Any] | None = None
            business_error_artifact_sha256: str | None = None
            business_error_evidence: Mapping[str, Any] | None = None
            if (
                safe_semantics is not None
                and safe_semantics.business_code != 0
                and safe_semantics.classification is not None
            ):
                expected_code = _BUSINESS_CLASSIFICATION_TO_FAILURE_CODE[
                    safe_semantics.classification
                ]
                if code != expected_code:
                    raise AlphaFeasibilityDataError(
                        "business_error_evidence_mismatch"
                    ) from exc
                business_error_evidence = safe_semantics.to_dict()
                if (
                    raw is not None
                    and recover_interrupted_attempts
                    and persist_full_raw_transport
                    and _path_is_within_data_tmp(self.root)
                ):
                    _guard_artifact_secret(raw, token)
                    _write_create_only(
                        self.raw_error_path(task, attempt_number), raw
                    )
                    business_error_artifact = _business_error_payload(
                        task,
                        attempt_number=attempt_number,
                        evidence=safe_semantics,
                        raw_error_artifact_sha256=safe_semantics.response_body_sha256,
                    )
                    try:
                        validate_json_schema(
                            business_error_artifact, BUSINESS_ERROR_SCHEMA_PATH
                        )
                    except SchemaValidationError as schema_exc:
                        raise AlphaFeasibilityDataError(
                            "business_error_artifact_schema_invalid"
                        ) from schema_exc
                    _write_json_create_only(
                        self.business_error_path(task, attempt_number),
                        business_error_artifact,
                        token=token,
                    )
                    business_error_artifact_sha256 = business_error_artifact[
                        "business_error_artifact_sha256"
                    ]
                    policy = business_error_retry_policy(
                        safe_semantics.classification,
                        retry_after_seconds=safe_semantics.retry_after_seconds,
                    )
                    if (
                        defer_retryable_business_errors
                        and
                        policy.maximum_additional_attempts == 1
                        and not business_errors
                        and attempt_number < maximum_attempts_per_fingerprint
                    ):
                        raise
            if (
                code == "transport_extension_secret_detected"
                and "token_leak_check" not in diagnostic
            ):
                token_leak_check = (
                    "DECODED_LEAK_REJECTED"
                    if token_leak_check == "RAW_BYTES_PASSED"
                    else "RAW_LEAK_REJECTED"
                )
            observed_root_fields = diagnostic.get(
                "observed_root_fields",
                list(validated.observed_root_fields) if validated is not None else [],
            )
            no_response_interruption = (
                terminalize_transport_interruptions
                and code in RETRYABLE_ATTEMPT_FAILURES
                and raw is None
            )
            quarantine = {
                "schema_version": QUARANTINE_SCHEMA_VERSION,
                "state": (
                    "TRANSPORT_ATTEMPT_QUARANTINED_NO_RESPONSE"
                    if no_response_interruption
                    else "RESPONSE_QUARANTINED"
                ),
                "task_id": task.task_id,
                "endpoint": task.endpoint,
                "plan_sha256": task.plan_sha256,
                "reason": code,
                "failure_code": code,
                "raw_transport_sha256": diagnostic.get("raw_transport_sha256", raw_sha),
                "http_status": diagnostic.get("http_status", http_status),
                "response_byte_count": diagnostic.get(
                    "response_byte_count", response_byte_count
                ),
                "observed_root_fields": observed_root_fields,
                "semantic_core_fields": diagnostic.get("semantic_core_fields", []),
                "missing_semantic_core_fields": diagnostic.get(
                    "missing_semantic_core_fields", []
                ),
                "transport_extension_field_names": diagnostic.get(
                    "transport_extension_field_names", []
                ),
                "transport_extension_type_by_field": diagnostic.get(
                    "transport_extension_type_by_field", {}
                ),
                "transport_extension_value_sha256_by_field": diagnostic.get(
                    "transport_extension_value_sha256_by_field", {}
                ),
                "transport_extensions_sha256": diagnostic.get(
                    "transport_extensions_sha256"
                ),
                "transport_extensions_byte_count": diagnostic.get(
                    "transport_extensions_byte_count"
                ),
                "upstream_code": diagnostic.get("upstream_code"),
                "upstream_error_category": diagnostic.get(
                    "upstream_error_category"
                ),
                "business_error_classification": (
                    safe_semantics.classification
                    if safe_semantics is not None
                    and safe_semantics.business_code != 0
                    else None
                ),
                "business_error_evidence": business_error_evidence,
                "business_error_artifact_sha256": (
                    business_error_artifact_sha256
                ),
                "terminal_attempt_number": attempt_number,
                "data_failure_category": diagnostic.get("data_failure_category"),
                "observed_data_fields": diagnostic.get(
                    "observed_data_fields",
                    list(validated.observed_data_fields) if validated is not None else [],
                ),
                "required_data_fields": diagnostic.get(
                    "required_data_fields", list(task.fields)
                ),
                "missing_required_data_fields": diagnostic.get(
                    "missing_required_data_fields",
                    (
                        list(validated.missing_required_data_fields)
                        if validated is not None
                        else []
                        if no_response_interruption
                        else list(task.fields)
                    ),
                ),
                "extra_data_fields": diagnostic.get(
                    "extra_data_fields",
                    list(validated.extra_data_fields) if validated is not None else [],
                ),
                "field_order_matches_canonical": diagnostic.get(
                    "field_order_matches_canonical",
                    (
                        validated.field_order_matches_canonical
                        if validated is not None
                        else False
                    ),
                ),
                "provider_payload_sha256": diagnostic.get(
                    "provider_payload_sha256",
                    validated.provider_payload_sha256 if validated is not None else None,
                ),
                "normalized_content_sha256": diagnostic.get(
                    "normalized_content_sha256",
                    (
                        validated.normalized_content_sha256
                        if validated is not None
                        else None
                    ),
                ),
                "extra_data_field_value_sha256_by_field": diagnostic.get(
                    "extra_data_field_value_sha256_by_field",
                    (
                        dict(validated.extra_data_field_value_sha256_by_field)
                        if validated is not None
                        else {}
                    ),
                ),
                "data_row_count": diagnostic.get(
                    "data_row_count",
                    validated.data_row_count if validated is not None else 0,
                ),
                "token_leak_check": diagnostic.get(
                    "token_leak_check", token_leak_check
                ),
                "raw_response_persisted": business_error_artifact is not None,
                "locked_test_status": dict(LOCKED_TEST_STATUS),
                "locked_test_consumed": False,
            }
            if not self.quarantine_path(task).exists():
                try:
                    validate_json_schema(quarantine, QUARANTINE_SCHEMA_PATH)
                except SchemaValidationError as schema_exc:
                    raise AlphaFeasibilityDataError(
                        "quarantine_artifact_schema_invalid"
                    ) from schema_exc
                _write_json_create_only(self.quarantine_path(task), quarantine, token=token)
            if isinstance(exc, AlphaFeasibilityDataError):
                raise
            raise AlphaFeasibilityDataError("unclassified_task_failure") from exc


def retry_business_error_once(
    store: CreateOnlyTaskStore,
    task: CollectionTask,
    *,
    token: str,
    transport: TushareTransport,
    timeout_seconds: int,
    maximum_response_bytes: int,
    maximum_attempts_per_fingerprint: int,
    minimum_request_interval_seconds: Decimal = MINIMUM_REAL_TRANSPORT_INTERVAL_SECONDS,
    clock: Callable[[], datetime] = _utc_now,
    sleeper: Callable[[float], None] = time.sleep,
    terminalize_transport_interruptions: bool = False,
) -> TaskExecutionResult:
    """Consume the sole durable business-error retry for one fingerprint."""

    if not isinstance(store, CreateOnlyTaskStore):
        raise AlphaFeasibilityDataError("invalid_business_retry_store")
    _validate_collection_task_contract(task)
    if not callable(clock) or not callable(sleeper):
        raise AlphaFeasibilityDataError("invalid_business_retry_clock")
    terminal_code = store._terminal_quarantine_code(task)
    if terminal_code is not None:
        raise AlphaFeasibilityDataError(terminal_code)
    started = store._load_started(task)
    if started.get("schema_version") != RECOVERABLE_STARTED_SCHEMA_VERSION:
        raise AlphaFeasibilityDataError("business_error_retry_parent_missing")
    attempts = store._load_attempts(task)
    business_errors = store._load_business_errors(task)
    if not business_errors:
        raise AlphaFeasibilityDataError("business_error_retry_parent_missing")
    parent = business_errors[-1]
    parent_attempt_number = parent["attempt_number"]
    if len(attempts) != parent_attempt_number:
        raise AlphaFeasibilityDataError("business_error_retry_already_consumed")
    if len(business_errors) != 1:
        raise AlphaFeasibilityDataError("business_error_retry_already_consumed")
    evidence = parent["evidence"]
    policy = business_error_retry_policy(
        evidence["classification"],
        retry_after_seconds=evidence["retry_after_seconds"],
        minimum_request_interval_seconds=minimum_request_interval_seconds,
    )
    if policy.maximum_additional_attempts != 1:
        raise AlphaFeasibilityDataError("business_error_not_retryable")
    if parent_attempt_number >= maximum_attempts_per_fingerprint:
        raise AlphaFeasibilityDataError("maximum_attempts_exhausted")
    try:
        completed_at = datetime.fromisoformat(evidence["completed_at"])
    except (TypeError, ValueError) as exc:
        raise AlphaFeasibilityDataError("business_error_evidence_invalid") from exc
    completed_at = _utc_timestamp(completed_at, "completed_at")
    now = _utc_timestamp(clock(), "retry_clock")
    elapsed_seconds = Decimal(str((now - completed_at).total_seconds()))
    remaining_seconds = policy.delay_seconds - max(elapsed_seconds, Decimal("0"))
    if remaining_seconds > 0:
        sleeper(float(remaining_seconds))
    return store.execute(
        task,
        token=token,
        transport=transport,
        timeout_seconds=timeout_seconds,
        maximum_response_bytes=maximum_response_bytes,
        recover_interrupted_attempts=True,
        maximum_attempts_per_fingerprint=maximum_attempts_per_fingerprint,
        persist_full_raw_transport=True,
        clock=clock,
        authorized_business_retry_parent_attempt=parent_attempt_number,
        terminalize_transport_interruptions=terminalize_transport_interruptions,
    )


def _started_payload(
    task: CollectionTask, *, recoverable: bool = False
) -> dict[str, Any]:
    if recoverable:
        return {
            "schema_version": RECOVERABLE_STARTED_SCHEMA_VERSION,
            "state": "REQUEST_FINGERPRINT_REGISTERED",
            "request_count": 0,
            "task": task.to_dict(),
            "locked_test_status": dict(LOCKED_TEST_STATUS),
            "locked_test_consumed": False,
        }
    return {
        "schema_version": STARTED_SCHEMA_VERSION,
        "state": "NETWORK_CALL_STARTED",
        "request_count": 1,
        "task": task.to_dict(),
        "locked_test_status": dict(LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
    }


def _attempt_payload(task: CollectionTask, attempt_number: int) -> dict[str, Any]:
    if type(attempt_number) is not int or attempt_number < 1:
        raise AlphaFeasibilityDataError("invalid_attempt_number")
    return {
        "schema_version": ATTEMPT_SCHEMA_VERSION,
        "state": "NETWORK_CALL_ATTEMPT_STARTED",
        "attempt_number": attempt_number,
        "request_count": 1,
        "request_count_semantics": P15_REQUEST_COUNT_SEMANTICS,
        "task_id": task.task_id,
        "endpoint": task.endpoint,
        "plan_sha256": task.plan_sha256,
        "task_sha256": canonical_sha256(task.to_dict()),
        "locked_test_status": dict(LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
    }


def _business_error_payload(
    task: CollectionTask,
    *,
    attempt_number: int,
    evidence: SafeResponseSemantics,
    raw_error_artifact_sha256: str,
) -> dict[str, Any]:
    if evidence.business_code == 0 or evidence.classification is None:
        raise AlphaFeasibilityDataError("business_error_evidence_invalid")
    return _self_hashed(
        {
            "schema_version": BUSINESS_ERROR_SCHEMA_VERSION,
            "state": "BUSINESS_ERROR_RESPONSE_SCANNED",
            "task_id": task.task_id,
            "endpoint": task.endpoint,
            "plan_sha256": task.plan_sha256,
            "task_sha256": canonical_sha256(task.to_dict()),
            "attempt_number": attempt_number,
            "network_request_count": 1,
            "request_count_semantics": P15_REQUEST_COUNT_SEMANTICS,
            "evidence": evidence.to_dict(),
            "raw_error_artifact_sha256": raw_error_artifact_sha256,
            "raw_error_persisted": True,
            "locked_test_status": dict(LOCKED_TEST_STATUS),
            "locked_test_consumed": False,
        },
        "business_error_artifact_sha256",
    )


def _self_hashed(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    if field in value:
        raise AlphaFeasibilityDataError("self_hash_field_must_be_derived")
    result = dict(value)
    result[field] = canonical_sha256(result)
    return result


def actual_tushare_request_count_by_endpoint(
    output_root: Path | str,
    expected_tasks: Sequence[CollectionTask] | None = None,
    *,
    plan_sha256: str | None = None,
) -> dict[str, int]:
    """Derive request counts from legacy starts or P1.5 attempt journals."""

    if plan_sha256 is not None and (
        type(plan_sha256) is not str or _SHA256.fullmatch(plan_sha256) is None
    ):
        raise AlphaFeasibilityDataError("invalid_plan_sha256")
    counts = {endpoint: 0 for endpoint in ALLOWED_ENDPOINTS}
    store = CreateOnlyTaskStore(output_root)
    task_directory = store.root / "tasks"
    attempt_root = store.root / "attempts"
    if not task_directory.exists():
        if attempt_root.exists():
            if (
                not attempt_root.is_dir()
                or attempt_root.is_symlink()
                or any(attempt_root.iterdir())
            ):
                raise AlphaFeasibilityDataError("orphan_attempt_request_evidence")
        return counts
    expected = (
        {task.task_id: task for task in expected_tasks}
        if expected_tasks is not None
        else None
    )
    if expected is not None:
        expected_plan_hashes = {task.plan_sha256 for task in expected.values()}
        if len(expected_plan_hashes) > 1 or (
            plan_sha256 is not None
            and bool(expected_plan_hashes)
            and expected_plan_hashes != {plan_sha256}
        ):
            raise AlphaFeasibilityDataError("expected_task_plan_mismatch")
    observed_ids: set[str] = set()
    recoverable_attempt_task_ids: set[str] = set()
    for path in task_directory.glob("*.started.json"):
        try:
            value = strict_json_loads(path.read_bytes(), label="started_artifact")
        except OSError as exc:
            raise AlphaFeasibilityDataError("started_artifact_unreadable") from exc
        if (
            not isinstance(value, Mapping)
            or value.get("schema_version")
            not in {STARTED_SCHEMA_VERSION, RECOVERABLE_STARTED_SCHEMA_VERSION}
            or not isinstance(value.get("task"), Mapping)
            or value["task"].get("endpoint") not in counts
            or value["task"].get("task_id") is None
            or type(value["task"].get("plan_sha256")) is not str
            or _SHA256.fullmatch(value["task"]["plan_sha256"]) is None
            or path.name != f"{value['task']['task_id']}.started.json"
        ):
            raise AlphaFeasibilityDataError("invalid_started_request_evidence")
        task_id = value["task"]["task_id"]
        if task_id in observed_ids:
            raise AlphaFeasibilityDataError("duplicate_started_request_evidence")
        observed_ids.add(task_id)
        if expected is not None:
            task = expected.get(task_id)
            if task is None or value not in (
                _started_payload(task, recoverable=False),
                _started_payload(task, recoverable=True),
            ):
                raise AlphaFeasibilityDataError("started_request_outside_current_plan")
        elif plan_sha256 is not None and value["task"]["plan_sha256"] != plan_sha256:
            raise AlphaFeasibilityDataError("started_request_outside_current_plan")
        if value["schema_version"] == STARTED_SCHEMA_VERSION:
            if (
                value.get("state") != "NETWORK_CALL_STARTED"
                or value.get("request_count") != 1
            ):
                raise AlphaFeasibilityDataError("invalid_started_request_evidence")
            counts[value["task"]["endpoint"]] += 1
            continue
        if (
            value.get("state") != "REQUEST_FINGERPRINT_REGISTERED"
            or value.get("request_count") != 0
        ):
            raise AlphaFeasibilityDataError("invalid_started_request_evidence")
        recoverable_attempt_task_ids.add(task_id)
        attempt_directory = store.root / "attempts" / task_id
        if attempt_directory.exists() and (
            not attempt_directory.is_dir() or attempt_directory.is_symlink()
        ):
            raise AlphaFeasibilityDataError("invalid_attempt_request_evidence")
        attempt_paths = sorted(attempt_directory.iterdir()) if attempt_directory.exists() else []
        if any(not path.is_file() or path.is_symlink() for path in attempt_paths):
            raise AlphaFeasibilityDataError("invalid_attempt_request_evidence")
        for number, attempt_path in enumerate(attempt_paths, start=1):
            if attempt_path != store.root / "attempts" / task_id / f"{number:06d}.started.json":
                raise AlphaFeasibilityDataError("invalid_attempt_request_evidence")
            try:
                attempt = strict_json_loads(
                    attempt_path.read_bytes(), label="attempt_artifact"
                )
            except OSError as exc:
                raise AlphaFeasibilityDataError("attempt_artifact_unreadable") from exc
            expected_attempt = {
                "schema_version": ATTEMPT_SCHEMA_VERSION,
                "state": "NETWORK_CALL_ATTEMPT_STARTED",
                "attempt_number": number,
                "request_count": 1,
                "request_count_semantics": P15_REQUEST_COUNT_SEMANTICS,
                "task_id": task_id,
                "endpoint": value["task"]["endpoint"],
                "plan_sha256": value["task"]["plan_sha256"],
                "task_sha256": canonical_sha256(value["task"]),
                "locked_test_status": dict(LOCKED_TEST_STATUS),
                "locked_test_consumed": False,
            }
            if not isinstance(attempt, Mapping) or dict(attempt) != expected_attempt:
                raise AlphaFeasibilityDataError("invalid_attempt_request_evidence")
            try:
                validate_json_schema(attempt, ATTEMPT_SCHEMA_PATH)
            except SchemaValidationError as exc:
                raise AlphaFeasibilityDataError(
                    "invalid_attempt_request_evidence"
                ) from exc
        counts[value["task"]["endpoint"]] += len(attempt_paths)
    if attempt_root.exists():
        if not attempt_root.is_dir() or attempt_root.is_symlink():
            raise AlphaFeasibilityDataError("invalid_attempt_request_evidence")
        for entry in attempt_root.iterdir():
            if (
                not entry.is_dir()
                or entry.is_symlink()
                or entry.name not in recoverable_attempt_task_ids
            ):
                raise AlphaFeasibilityDataError("orphan_attempt_request_evidence")
    for path in task_directory.glob("*.import.json"):
        task_id = path.name[: -len(".import.json")]
        if task_id in observed_ids:
            raise AlphaFeasibilityDataError("multiple_request_provenance_artifacts")
        if expected is None:
            try:
                imported = strict_json_loads(path.read_bytes(), label="import_artifact")
            except OSError as exc:
                raise AlphaFeasibilityDataError("import_artifact_unreadable") from exc
            if (
                not isinstance(imported, Mapping)
                or imported.get("task_id") != task_id
                or imported.get("network_request_count") != 0
                or (
                    plan_sha256 is not None
                    and imported.get("plan_sha256") != plan_sha256
                )
            ):
                raise AlphaFeasibilityDataError("import_outside_current_plan")
        else:
            task = expected.get(task_id)
            if task is None:
                raise AlphaFeasibilityDataError("import_outside_current_plan")
            store._load_import(task)
        observed_ids.add(task_id)
    return counts


def _strict_request_counts(value: Mapping[str, int]) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != set(ALLOWED_ENDPOINTS):
        raise AlphaFeasibilityDataError("request_count_endpoint_set_invalid")
    counts: dict[str, int] = {}
    for endpoint in ALLOWED_ENDPOINTS:
        count = value[endpoint]
        if type(count) is not int or count < 0:
            raise AlphaFeasibilityDataError("request_count_value_invalid")
        counts[endpoint] = count
    return counts


def import_parent_reuse_task_v2(
    *,
    task: CollectionTask,
    parent_root: Path | str,
    child_root: Path | str,
    parent_binding: Mapping[str, Any],
    reuse_evidence: Mapping[str, Any],
) -> TaskExecutionResult:
    """Create one zero-request child provenance marker from a sealed parent task."""

    _validate_collection_task_contract(task)
    parent_path = Path(parent_root)
    child_path = Path(child_root)
    if (
        parent_path.is_symlink()
        or not parent_path.is_dir()
        or child_path.is_symlink()
        or not isinstance(parent_binding, Mapping)
        or not isinstance(reuse_evidence, Mapping)
    ):
        raise AlphaFeasibilityDataError("parent_reuse_root_invalid")
    parent_resolved = parent_path.resolve()
    child_resolved = child_path.resolve(strict=False)
    if (
        parent_resolved == child_resolved
        or child_resolved.is_relative_to(parent_resolved)
        or parent_resolved.is_relative_to(child_resolved)
    ):
        raise AlphaFeasibilityDataError("parent_reuse_roots_not_disjoint")

    try:
        receipt_bytes = (parent_path / "p1_5_run_receipt.json").read_bytes()
        receipt = strict_json_loads(receipt_bytes, label="parent_reuse_receipt")
    except OSError as exc:
        raise AlphaFeasibilityDataError("parent_reuse_receipt_unreadable") from exc
    if (
        not isinstance(receipt, Mapping)
        or receipt_bytes != canonical_json_bytes(receipt)
    ):
        raise AlphaFeasibilityDataError("parent_reuse_receipt_invalid")
    unsigned_receipt = dict(receipt)
    receipt_sha256 = unsigned_receipt.pop("receipt_sha256", None)
    if (
        receipt_sha256 != canonical_sha256(unsigned_receipt)
        or parent_binding.get("receipt_sha256") != receipt_sha256
        or receipt.get("collection_plan_sha256") != task.plan_sha256
        or receipt.get("network_run_id") != parent_binding.get("network_run_id")
        or receipt.get("run_claim_sha256")
        != parent_binding.get("run_claim_sha256")
        or receipt.get("report_sha256") != parent_binding.get("report_sha256")
        or receipt.get("locked_test_status") != dict(LOCKED_TEST_STATUS)
        or receipt.get("locked_test_consumed") is not False
    ):
        raise AlphaFeasibilityDataError("parent_reuse_receipt_binding_mismatch")

    parent_store = CreateOnlyTaskStore(parent_path)
    if parent_store.quarantine_path(task).exists() or not parent_store.is_complete(task):
        raise AlphaFeasibilityDataError("parent_reuse_task_not_complete")
    parent_result = parent_store._load_response(task)
    try:
        parent_raw = parent_store.raw_path(task).read_bytes()
        parent_response_bytes = parent_store.response_path(task).read_bytes()
        parent_response = strict_json_loads(
            parent_response_bytes, label="parent_reuse_response"
        )
    except OSError as exc:
        raise AlphaFeasibilityDataError("parent_reuse_artifact_unreadable") from exc
    if not isinstance(parent_response, Mapping):
        raise AlphaFeasibilityDataError("parent_reuse_response_invalid")
    replay = validate_response_bytes(
        task, parent_raw, token=None, require_full_raw_safety=True
    )
    if [dict(row) for row in replay.rows] != [dict(row) for row in parent_result.rows]:
        raise AlphaFeasibilityDataError("parent_reuse_raw_replay_mismatch")

    parent_raw_sha256 = hashlib.sha256(parent_raw).hexdigest()
    parent_response_file_sha256 = hashlib.sha256(
        parent_response_bytes
    ).hexdigest()
    task_sha256 = canonical_sha256(task.to_dict())
    if parent_result.request_origin == "offline_p14d_import":
        provenance_path = parent_store.import_path(task)
        expected_started_sha256 = None
        expected_import_sha256 = hashlib.sha256(
            provenance_path.read_bytes()
        ).hexdigest()
        attempt_hashes: list[str] = []
    elif parent_result.request_origin == "network":
        provenance_path = parent_store.started_path(task)
        expected_started_sha256 = hashlib.sha256(
            provenance_path.read_bytes()
        ).hexdigest()
        expected_import_sha256 = None
        attempts = parent_store._load_attempts(task)
        attempt_hashes = [
            hashlib.sha256(
                parent_store.attempt_path(task, number).read_bytes()
            ).hexdigest()
            for number in range(1, len(attempts) + 1)
        ]
    else:
        raise AlphaFeasibilityDataError("parent_reuse_provenance_invalid")
    provenance_sha256 = hashlib.sha256(provenance_path.read_bytes()).hexdigest()
    expected_reuse_fields = {
        "ordinal",
        "request_fingerprint",
        "task_id",
        "task_sha256",
        "endpoint",
        "month",
        "params",
        "provenance_kind",
        "started_artifact_sha256",
        "import_artifact_sha256",
        "raw_artifact_sha256",
        "response_file_sha256",
        "response_artifact_sha256",
        "normalized_content_sha256",
        "attempt_artifact_sha256_by_number",
        "network_request_count",
    }
    expected_month = (
        f"{task.params['start_date'][:4]}-{task.params['start_date'][4:6]}"
        if "start_date" in task.params
        else None
    )
    if (
        set(reuse_evidence) != expected_reuse_fields
        or type(reuse_evidence.get("ordinal")) is not int
        or reuse_evidence["ordinal"] < 1
        or reuse_evidence.get("request_fingerprint") != task.task_id
        or reuse_evidence.get("task_id") != task.task_id
        or reuse_evidence.get("task_sha256") != task_sha256
        or reuse_evidence.get("endpoint") != task.endpoint
        or reuse_evidence.get("month") != expected_month
        or reuse_evidence.get("params") != dict(task.params)
        or reuse_evidence.get("provenance_kind") != parent_result.request_origin
        or reuse_evidence.get("started_artifact_sha256")
        != expected_started_sha256
        or reuse_evidence.get("import_artifact_sha256")
        != expected_import_sha256
        or reuse_evidence.get("raw_artifact_sha256") != parent_raw_sha256
        or reuse_evidence.get("response_file_sha256")
        != parent_response_file_sha256
        or reuse_evidence.get("response_artifact_sha256")
        != parent_response.get("response_artifact_sha256")
        or reuse_evidence.get("normalized_content_sha256")
        != parent_result.normalized_content_sha256
        or reuse_evidence.get("attempt_artifact_sha256_by_number")
        != attempt_hashes
        or reuse_evidence.get("network_request_count")
        != parent_result.network_request_count
    ):
        raise AlphaFeasibilityDataError("parent_reuse_evidence_mismatch")

    child_store = CreateOnlyTaskStore(child_path)
    if child_store.started_path(task).exists() or child_store.quarantine_path(task).exists():
        raise AlphaFeasibilityDataError("parent_reuse_child_provenance_conflict")
    if child_store.is_complete(task):
        result = child_store._load_response(task)
        if result.request_origin != "offline_parent_run_reuse":
            raise AlphaFeasibilityDataError("parent_reuse_child_provenance_conflict")
        return result
    _publish_bytes_or_verify_identical(child_store.raw_path(task), parent_raw)
    _publish_bytes_or_verify_identical(
        child_store.response_path(task), parent_response_bytes
    )
    imported = _self_hashed(
        {
            "schema_version": PARENT_REUSE_IMPORT_SCHEMA_VERSION,
            "state": "PARENT_RUN_TASK_REUSED",
            "task_id": task.task_id,
            "endpoint": task.endpoint,
            "plan_sha256": task.plan_sha256,
            "task": task.to_dict(),
            "task_sha256": task_sha256,
            "parent_binding": dict(parent_binding),
            "parent_provenance_kind": parent_result.request_origin,
            "parent_provenance_artifact_sha256": provenance_sha256,
            "parent_attempt_artifact_sha256_by_number": attempt_hashes,
            "parent_network_request_count": parent_result.network_request_count,
            "parent_raw_artifact_sha256": parent_raw_sha256,
            "parent_response_file_sha256": parent_response_file_sha256,
            "parent_response_artifact_sha256": parent_response[
                "response_artifact_sha256"
            ],
            "raw_transport_sha256": parent_raw_sha256,
            "normalized_content_sha256": parent_result.normalized_content_sha256,
            "network_request_count": 0,
            "locked_test_status": dict(LOCKED_TEST_STATUS),
            "locked_test_consumed": False,
        },
        "import_artifact_sha256",
    )
    try:
        validate_json_schema(imported, PARENT_REUSE_IMPORT_SCHEMA_PATH)
    except SchemaValidationError as exc:
        raise AlphaFeasibilityDataError("import_artifact_schema_invalid") from exc
    _write_json_create_only(child_store.import_path(task), imported)
    result = child_store._load_response(task)
    if result.request_origin != "offline_parent_run_reuse" or result.network_request_count != 0:
        raise AlphaFeasibilityDataError("parent_reuse_child_semantics_mismatch")
    return result


def import_p14d_diagnostic_into_plan(
    *,
    diagnostic_root: Path | str,
    output_root: Path | str,
    plan: CollectionPlan,
) -> TaskExecutionResult:
    """Bind a complete P1.4D raw diagnostic to P1.5's first PIT task.

    The import performs no credential lookup and no network call.  Every
    source artifact, hash edge and both deterministic replays are checked
    before a create-only import provenance marker makes the task complete.
    """

    if not isinstance(plan, CollectionPlan) or not _p15_enabled(plan):
        raise AlphaFeasibilityDataError("p14d_import_requires_p15_plan")
    task = plan.pit_tasks[0]
    if (
        task.endpoint != "index_weight"
        or dict(task.params)
        != {
            "index_code": "000906.SH",
            "start_date": "20171201",
            "end_date": "20171231",
        }
        or task.fields != EXPECTED_FIELDS["index_weight"]
    ):
        raise AlphaFeasibilityDataError("p14d_import_task_contract_drift")
    binding = _p14d_bundle_binding(plan)
    source_root = Path(diagnostic_root)
    source_bytes: dict[str, bytes] = {}
    source_values: dict[str, Mapping[str, Any]] = {}
    for name in P14D_SOURCE_ARTIFACT_NAMES:
        try:
            content = (source_root / name).read_bytes()
        except OSError as exc:
            raise AlphaFeasibilityDataError("p14d_import_artifact_missing") from exc
        source_bytes[name] = content
        if name != "response.raw.json":
            parsed = strict_json_loads(content, label="p14d_import_artifact")
            if not isinstance(parsed, Mapping):
                raise AlphaFeasibilityDataError("p14d_import_artifact_invalid")
            source_values[name] = parsed
    source_hashes = {
        name: hashlib.sha256(source_bytes[name]).hexdigest()
        for name in P14D_SOURCE_ARTIFACT_NAMES
    }
    if (
        source_hashes != dict(binding["source_artifact_sha256_by_name"])
        or canonical_sha256(source_hashes) != binding["bundle_sha256"]
    ):
        raise AlphaFeasibilityDataError("p14d_import_bundle_binding_mismatch")

    request = source_values["request.json"]
    profile = source_values["value_profile.json"]
    replay = source_values["offline_replay.json"]
    try:
        validate_json_schema(request, P14D_REQUEST_SCHEMA_PATH)
        validate_json_schema(profile, P14D_PROFILE_SCHEMA_PATH)
        validate_json_schema(replay, P14D_REPLAY_SCHEMA_PATH)
    except SchemaValidationError as exc:
        raise AlphaFeasibilityDataError("p14d_import_schema_invalid") from exc
    try:
        requested_at = datetime.fromisoformat(str(request["requested_at"]))
    except ValueError as exc:
        raise AlphaFeasibilityDataError("p14d_import_request_time_invalid") from exc
    if requested_at.tzinfo is None:
        raise AlphaFeasibilityDataError("p14d_import_request_time_invalid")

    raw = source_bytes["response.raw.json"]
    raw_sha = hashlib.sha256(raw).hexdigest()
    if (
        request.get("request_fingerprint") != binding["request_fingerprint"]
        or raw_sha != binding["raw_transport_sha256"]
    ):
        raise AlphaFeasibilityDataError("p14d_import_bundle_binding_mismatch")
    request_sha = hashlib.sha256(canonical_json_bytes(request)).hexdigest()
    request_counts = {
        "index_weight": 1,
        "trade_cal": 0,
        "daily": 0,
        "adj_factor": 0,
        "index_daily": 0,
        "suspend_d": 0,
        "stock_basic": 0,
    }
    started_expected = {
        "schema_version": "tushare-index-weight-network-call.v1",
        "state": "TRANSPORT_INVOCATION_STARTED",
        "endpoint": "index_weight",
        "request_fingerprint": request["request_fingerprint"],
        "request_artifact_sha256": request_sha,
        "network_process_count": 1,
        "actual_request_count_by_endpoint": request_counts,
    }
    scanned_expected = {
        **started_expected,
        "state": "HTTP_RESPONSE_SCANNED",
        "http_status": 200,
        "raw_transport_sha256": raw_sha,
        "response_byte_count": len(raw),
    }
    if (
        dict(source_values["network_call_started.json"]) != started_expected
        or dict(source_values["network_response_scanned.json"]) != scanned_expected
    ):
        raise AlphaFeasibilityDataError("p14d_import_provenance_invalid")
    unsigned_profile = dict(profile)
    declared_profile_sha = unsigned_profile.pop("profile_sha256", None)
    if (
        declared_profile_sha
        != hashlib.sha256(
            _canonical_transport_json_bytes(unsigned_profile) + b"\n"
        ).hexdigest()
        or profile.get("raw_transport_sha256") != raw_sha
        or profile.get("response_byte_count") != len(raw)
        or replay.get("raw_transport_sha256") != raw_sha
        or replay.get("value_profile_sha256")
        != hashlib.sha256(source_bytes["value_profile.json"]).hexdigest()
        or replay.get("normalized_pit_sha256")
        != hashlib.sha256(source_bytes["normalized_pit.json"]).hexdigest()
    ):
        raise AlphaFeasibilityDataError("p14d_import_hash_chain_invalid")

    first = validate_response_bytes(
        task, raw, token=None, require_full_raw_safety=True
    )
    second = validate_response_bytes(
        task, raw, token=None, require_full_raw_safety=True
    )
    if (
        first.normalized_content_sha256 != second.normalized_content_sha256
        or first.normalized_content_sha256 != binding["normalized_content_sha256"]
        or [dict(row) for row in first.rows] != [dict(row) for row in second.rows]
        or len(first.rows) != 800
    ):
        raise AlphaFeasibilityDataError("p14d_import_replay_invalid")
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in first.rows:
        grouped.setdefault(str(row["trade_date"]), []).append(row)
    for snapshot_rows in grouped.values():
        if len(snapshot_rows) != 800:
            raise AlphaFeasibilityDataError("p14d_import_snapshot_count_invalid")
        _validate_weight_snapshot(snapshot_rows, expected_count=800, p15=True)
    rows = [dict(row) for row in first.rows]
    weights_by_date: dict[str, list[Decimal]] = {}
    for row in rows:
        weights_by_date.setdefault(row["trade_date"], []).append(
            _index_weight_decimal(row["weight"])[0]
        )
    normalized_expected = {
        "schema_version": "tushare-index-weight-normalized-pit.v1",
        "fields": list(EXPECTED_FIELDS["index_weight"]),
        "items": [
            [row[field] for field in EXPECTED_FIELDS["index_weight"]]
            for row in rows
        ],
        "row_count": len(rows),
        "trade_dates": sorted(weights_by_date),
        "weight_sum_by_trade_date": {
            key: format(_exact_decimal_sum(weights_by_date[key]), "f")
            for key in sorted(weights_by_date)
        },
        "normalized_content_sha256": first.normalized_content_sha256,
        "locked_test_status": dict(LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
    }
    if (
        dict(source_values["normalized_pit.json"]) != normalized_expected
        or replay.get("normalized_row_count") != len(rows)
        or replay.get("normalized_trade_dates") != normalized_expected["trade_dates"]
        or replay.get("normalized_weight_sum_by_date")
        != normalized_expected["weight_sum_by_trade_date"]
        or replay.get("normalized_content_sha256")
        != first.normalized_content_sha256
    ):
        raise AlphaFeasibilityDataError("p14d_import_normalized_replay_mismatch")

    store = CreateOnlyTaskStore(output_root)
    if store.started_path(task).exists():
        raise AlphaFeasibilityDataError("p14d_import_conflicts_with_network_provenance")
    if store.is_complete(task):
        return store._load_response(task)
    _publish_bytes_or_verify_identical(store.raw_path(task), raw)
    response = _response_payload(task, first, persisted_sha256=raw_sha)
    _publish_or_verify_identical(store.response_path(task), response)
    imported = _self_hashed(
        {
            "schema_version": IMPORT_SCHEMA_VERSION,
            "state": "P14D_DIAGNOSTIC_IMPORTED",
            "task_id": task.task_id,
            "endpoint": task.endpoint,
            "plan_sha256": task.plan_sha256,
            "task": task.to_dict(),
            "accepted_bundle_sha256": binding["bundle_sha256"],
            "request_fingerprint": binding["request_fingerprint"],
            "source_artifact_sha256_by_name": source_hashes,
            "raw_transport_sha256": raw_sha,
            "normalized_content_sha256": first.normalized_content_sha256,
            "network_request_count": 0,
            "locked_test_status": dict(LOCKED_TEST_STATUS),
            "locked_test_consumed": False,
        },
        "import_artifact_sha256",
    )
    try:
        validate_json_schema(imported, IMPORT_SCHEMA_PATH)
    except SchemaValidationError as exc:
        raise AlphaFeasibilityDataError("import_artifact_schema_invalid") from exc
    _write_json_create_only(store.import_path(task), imported)
    return store._load_response(task)


def execute_tasks(
    tasks: Sequence[CollectionTask],
    *,
    store: CreateOnlyTaskStore,
    token: str,
    transport: TushareTransport,
    timeout_seconds: int,
    maximum_response_bytes: int,
    recover_interrupted_attempts: bool = False,
    maximum_attempts_per_fingerprint: int = 1,
    persist_full_raw_transport: bool = False,
    minimum_request_interval_seconds: Decimal = Decimal("0"),
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    business_error_retry_once: bool = False,
    clock: Callable[[], datetime] = _utc_now,
    terminalize_transport_interruptions: bool = False,
) -> tuple[TaskExecutionResult, ...]:
    if (
        type(business_error_retry_once) is not bool
        or type(terminalize_transport_interruptions) is not bool
    ):
        raise AlphaFeasibilityDataError("invalid_business_retry_setting")
    if business_error_retry_once and (
        minimum_request_interval_seconds < MINIMUM_REAL_TRANSPORT_INTERVAL_SECONDS
    ):
        raise AlphaFeasibilityDataError("business_retry_interval_too_short")
    results: list[TaskExecutionResult] = []
    last_network_call: float | None = None
    interval = float(minimum_request_interval_seconds)
    for task in tasks:
        will_replay = store.is_complete(task)
        if not will_replay and last_network_call is not None and interval > 0:
            delay = interval - (monotonic() - last_network_call)
            if delay > 0:
                sleeper(delay)
        try:
            result = store.execute(
                task,
                token=token,
                transport=transport,
                timeout_seconds=timeout_seconds,
                maximum_response_bytes=maximum_response_bytes,
                recover_interrupted_attempts=recover_interrupted_attempts,
                maximum_attempts_per_fingerprint=maximum_attempts_per_fingerprint,
                persist_full_raw_transport=persist_full_raw_transport,
                clock=clock,
                defer_retryable_business_errors=business_error_retry_once,
                terminalize_transport_interruptions=terminalize_transport_interruptions,
            )
        except AlphaFeasibilityDataError as exc:
            if (
                not business_error_retry_once
                or exc.code
                not in {
                    _BUSINESS_CLASSIFICATION_TO_FAILURE_CODE["RATE_LIMITED"],
                    _BUSINESS_CLASSIFICATION_TO_FAILURE_CODE[
                        "UPSTREAM_SERVER_ERROR"
                    ],
                }
            ):
                raise
            result = retry_business_error_once(
                store,
                task,
                token=token,
                transport=transport,
                timeout_seconds=timeout_seconds,
                maximum_response_bytes=maximum_response_bytes,
                maximum_attempts_per_fingerprint=maximum_attempts_per_fingerprint,
                minimum_request_interval_seconds=minimum_request_interval_seconds,
                clock=clock,
                sleeper=sleeper,
                terminalize_transport_interruptions=terminalize_transport_interruptions,
            )
        if not result.replayed:
            last_network_call = monotonic()
        results.append(result)
    return tuple(results)


def execute_tasks_bounded(
    tasks: Sequence[CollectionTask],
    *,
    store: CreateOnlyTaskStore,
    token: str,
    transport: TushareTransport,
    timeout_seconds: int,
    maximum_response_bytes: int,
    recover_interrupted_attempts: bool = False,
    maximum_attempts_per_fingerprint: int = 1,
    persist_full_raw_transport: bool = False,
    minimum_request_interval_seconds: Decimal = Decimal("0"),
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    business_error_retry_once: bool = False,
    clock: Callable[[], datetime] = _utc_now,
    terminalize_transport_interruptions: bool = False,
) -> None:
    """Execute tasks while retaining at most one normalized response in memory."""

    if (
        type(business_error_retry_once) is not bool
        or type(terminalize_transport_interruptions) is not bool
    ):
        raise AlphaFeasibilityDataError("invalid_business_retry_setting")
    if business_error_retry_once and (
        minimum_request_interval_seconds < MINIMUM_REAL_TRANSPORT_INTERVAL_SECONDS
    ):
        raise AlphaFeasibilityDataError("business_retry_interval_too_short")
    last_network_call: float | None = None
    interval = float(minimum_request_interval_seconds)
    for task in tasks:
        will_replay = store.is_complete(task)
        if not will_replay and last_network_call is not None and interval > 0:
            delay = interval - (monotonic() - last_network_call)
            if delay > 0:
                sleeper(delay)
        try:
            result = store.execute(
                task,
                token=token,
                transport=transport,
                timeout_seconds=timeout_seconds,
                maximum_response_bytes=maximum_response_bytes,
                recover_interrupted_attempts=recover_interrupted_attempts,
                maximum_attempts_per_fingerprint=maximum_attempts_per_fingerprint,
                persist_full_raw_transport=persist_full_raw_transport,
                clock=clock,
                defer_retryable_business_errors=business_error_retry_once,
                terminalize_transport_interruptions=terminalize_transport_interruptions,
            )
        except AlphaFeasibilityDataError as exc:
            if (
                not business_error_retry_once
                or exc.code
                not in {
                    _BUSINESS_CLASSIFICATION_TO_FAILURE_CODE["RATE_LIMITED"],
                    _BUSINESS_CLASSIFICATION_TO_FAILURE_CODE[
                        "UPSTREAM_SERVER_ERROR"
                    ],
                }
            ):
                raise
            result = retry_business_error_once(
                store,
                task,
                token=token,
                transport=transport,
                timeout_seconds=timeout_seconds,
                maximum_response_bytes=maximum_response_bytes,
                maximum_attempts_per_fingerprint=maximum_attempts_per_fingerprint,
                minimum_request_interval_seconds=minimum_request_interval_seconds,
                clock=clock,
                sleeper=sleeper,
                terminalize_transport_interruptions=terminalize_transport_interruptions,
            )
        if not result.replayed:
            last_network_call = monotonic()
        # Do not append result: its rows are already durably content-addressed.
        del result


def _generated_at(value: datetime | None) -> str:
    current = value or datetime.now(timezone.utc)
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise AlphaFeasibilityDataError("generated_at_must_be_timezone_aware")
    return current.isoformat()


def _is_full_session_suspension(row: Mapping[str, Any] | None) -> bool:
    if not row:
        return False
    if "full_session_suspended" in row:
        return row.get("full_session_suspended") is True
    if row.get("suspend_type") != "S":
        return False
    timing = row.get("suspend_timing")
    if timing is None:
        return True
    if type(timing) is not str:
        return False
    return timing.strip() in {"", "全天", "全日", "全天停牌", "全日停牌"}


def _validate_weight_snapshot(
    rows: Sequence[Mapping[str, Any]], *, expected_count: int, p15: bool = False
) -> tuple[Decimal, Decimal]:
    codes = [row["con_code"] for row in rows]
    if any(type(code) is not str or _PIT_COMPONENT_CODE.fullmatch(code) is None for code in codes):
        raise AlphaFeasibilityDataError("pit_component_exchange_not_allowed", stage="pit")
    if len(codes) != len(set(codes)):
        raise AlphaFeasibilityDataError("duplicate_component_in_snapshot", stage="pit")
    if len(rows) == 0:
        raise AlphaFeasibilityDataError("empty_pit_snapshot", stage="pit")
    coarsest_nonzero_places: int | None = None
    weights: list[Decimal] = []
    for row in rows:
        weight, text = _index_weight_decimal(row["weight"])
        places = _decimal_places(text)
        weights.append(weight)
        # A reported zero is retained as membership evidence, but it must not
        # determine aggregate precision.  Use one half-unit at the coarsest
        # non-zero precision so row count cannot inflate the sum allowance.
        if weight != 0:
            coarsest_nonzero_places = (
                places
                if coarsest_nonzero_places is None
                else min(coarsest_nonzero_places, places)
            )
    total = _exact_decimal_sum(weights)
    if p15:
        if not P15_WEIGHT_HARD_MIN <= total <= P15_WEIGHT_HARD_MAX:
            raise AlphaFeasibilityDataError(
                "weight_sum_outside_hard_range", stage="pit"
            )
        # Kept in the legacy field for downstream compatibility.  P1.5's
        # actual acceptance contract is the fixed inclusive hard range.
        return total, Decimal("0.5")
    tolerance = (
        Decimal("0")
        if coarsest_nonzero_places is None
        else Decimal("0.5") * (Decimal(10) ** (-coarsest_nonzero_places))
    )
    difference = abs(_exact_decimal_sum((total, Decimal("-100"))))
    if difference > tolerance:
        raise AlphaFeasibilityDataError("weight_sum_outside_row_precision_tolerance", stage="pit")
    return total, tolerance


def _zero_weight_count(rows: Sequence[Mapping[str, Any]]) -> int:
    count = 0
    for row in rows:
        try:
            if _index_weight_decimal(row.get("weight"))[0] == 0:
                count += 1
        except AlphaFeasibilityDataError:
            continue
    return count


@dataclass(frozen=True, slots=True)
class PitMembershipResult:
    coverage_report: Mapping[str, Any]
    manifest: Mapping[str, Any]
    union_instruments: tuple[str, ...]
    passed: bool


def build_pit_membership_artifacts(
    plan: CollectionPlan,
    results: Mapping[str, TaskExecutionResult | Sequence[Mapping[str, Any]]],
    *,
    adjustment_evidence: Mapping[str, Any] | None = None,
    generated_at: datetime | None = None,
    blocked_terminal_status: str = "BLOCKED_DATA",
) -> PitMembershipResult:
    """Validate 73 monthly responses and retain every legal PIT snapshot."""

    if adjustment_evidence is not None:
        raise AlphaFeasibilityDataError(
            "controlled_adjustment_evidence_not_supported", stage="pit"
        )
    if blocked_terminal_status not in {"BLOCKED_DATA", "BLOCKED_ADAPTER_PROTOCOL"}:
        raise AlphaFeasibilityDataError("pit_blocked_terminal_status_invalid", stage="pit")

    p15 = _p15_enabled(plan)
    report_schema_version, manifest_schema_version = _pit_schema_versions(plan)
    expected_count = int(plan.config["index"]["expected_component_count"])
    month_details: list[dict[str, Any]] = []
    selected_snapshots: list[dict[str, Any]] = []
    blockers: list[str] = []
    observed = 0
    duplicate_member_count = 0
    previous_selected: date | None = None
    for task in plan.pit_tasks:
        month = _parse_date(task.params["start_date"], "pit_start").strftime("%Y-%m")
        value = results.get(task.task_id)
        request_sha = hashlib.sha256(
            canonical_json_bytes(_started_payload(task, recoverable=p15))
        ).hexdigest()
        if value is None:
            issue = f"{month}:missing_month_response"
            blockers.append(issue)
            month_details.append(
                {
                    "month": month,
                    "request_artifact_sha256": request_sha,
                    "response_sha256": hashlib.sha256(b"").hexdigest(),
                    "snapshots": [],
                    "selected_snapshot_date": None,
                    "status": "missing",
                    "issues": ["missing_month_response"],
                }
            )
            continue
        rows = value.rows if isinstance(value, TaskExecutionResult) else tuple(value)
        # PIT membership identity is normalized content identity.  Transport
        # metadata (including request_id and the raw wire hash) remains in the
        # task receipt and must not alter this snapshot hash.
        response_sha = canonical_sha256([dict(row) for row in rows])
        observed += 1
        grouped: dict[str, list[Mapping[str, Any]]] = {}
        snapshot_checks: list[dict[str, Any]] = []
        try:
            for row in rows:
                if row.get("index_code") != plan.config["index"]["index_code"]:
                    raise AlphaFeasibilityDataError("pit_index_code_mismatch", stage="pit")
                snapshot = _compact(_response_date_window(task, row.get("trade_date"), "pit_trade_date"))
                grouped.setdefault(snapshot, []).append(row)
            if not grouped:
                raise AlphaFeasibilityDataError("no_snapshot_in_month", stage="pit")
            valid_candidates: list[dict[str, Any]] = []
            candidate_failures: list[str] = []
            for snapshot in sorted(grouped):
                snapshot_rows = grouped[snapshot]
                snapshot_codes = [row.get("con_code") for row in snapshot_rows]
                duplicate_member_count += len(snapshot_codes) - len(
                    set(snapshot_codes)
                )
                try:
                    total, tolerance = _validate_weight_snapshot(
                        snapshot_rows, expected_count=expected_count, p15=p15
                    )
                    count = len(snapshot_rows)
                    adjustment_reason = None
                    if count != expected_count:
                        raise AlphaFeasibilityDataError(
                            "component_count_requires_controlled_adjustment_evidence",
                            stage="pit",
                        )
                    zero_weight_count = _zero_weight_count(snapshot_rows)
                    warnings = (
                        ["weight_sum_outside_warning_range"]
                        if p15
                        and not P15_WEIGHT_WARNING_MIN
                        <= total
                        <= P15_WEIGHT_WARNING_MAX
                        else []
                    )
                    snapshot_check = {
                            "snapshot_date": _iso(_parse_date(snapshot, "snapshot_date")),
                            "component_count": count,
                            "weight_sum": format(total, "f"),
                            "weight_tolerance": format(tolerance, "f"),
                            "valid": True,
                            "issues": [],
                            "component_count_adjustment_evidence": adjustment_reason,
                        }
                    candidate = {
                            "month": month,
                            "snapshot_date": _iso(_parse_date(snapshot, "snapshot_date")),
                            "weight_sum": format(total, "f"),
                            "weight_tolerance": format(tolerance, "f"),
                            "members": sorted(
                                (
                                    {
                                        "instrument_id": row["con_code"],
                                        "weight": _index_weight_decimal(
                                            row["weight"]
                                        )[1],
                                    }
                                    for row in snapshot_rows
                                ),
                                key=lambda item: item["instrument_id"],
                            ),
                            "source_response_sha256": response_sha,
                            "component_count_adjustment_evidence": adjustment_reason,
                        }
                    if p15:
                        p15_weight_evidence = {
                            "zero_weight_count": zero_weight_count,
                            "weight_sum_hard_min": format(P15_WEIGHT_HARD_MIN, "f"),
                            "weight_sum_hard_max": format(P15_WEIGHT_HARD_MAX, "f"),
                            "weight_sum_warning_min": format(
                                P15_WEIGHT_WARNING_MIN, "f"
                            ),
                            "weight_sum_warning_max": format(
                                P15_WEIGHT_WARNING_MAX, "f"
                            ),
                            "warnings": warnings,
                        }
                        snapshot_check.update(p15_weight_evidence)
                        candidate.update(p15_weight_evidence)
                    snapshot_checks.append(snapshot_check)
                    valid_candidates.append(candidate)
                except AlphaFeasibilityDataError as exc:
                    candidate_failures.append(exc.code)
                    try:
                        invalid_weights = [
                            _index_weight_decimal(row["weight"])
                            for row in snapshot_rows
                        ]
                        invalid_total = _exact_decimal_sum(
                            [item[0] for item in invalid_weights]
                        )
                        invalid_nonzero_places = [
                            _decimal_places(text)
                            for weight, text in invalid_weights
                            if weight != 0
                        ]
                        invalid_tolerance = (
                            Decimal("0")
                            if not invalid_nonzero_places
                            else Decimal("0.5")
                            * (
                                Decimal(10)
                                ** (-min(invalid_nonzero_places))
                            )
                        )
                    except AlphaFeasibilityDataError:
                        invalid_total = Decimal("0")
                        invalid_tolerance = Decimal("0")
                    invalid_snapshot = {
                            "snapshot_date": _iso(_parse_date(snapshot, "snapshot_date")),
                            "component_count": len(snapshot_rows),
                            "weight_sum": format(invalid_total, "f"),
                            "weight_tolerance": format(invalid_tolerance, "f"),
                            "valid": False,
                            "issues": [exc.code],
                            "component_count_adjustment_evidence": None,
                        }
                    if p15:
                        invalid_snapshot.update(
                            {
                                "zero_weight_count": _zero_weight_count(
                                    snapshot_rows
                                ),
                                "weight_sum_hard_min": format(
                                    P15_WEIGHT_HARD_MIN, "f"
                                ),
                                "weight_sum_hard_max": format(
                                    P15_WEIGHT_HARD_MAX, "f"
                                ),
                                "weight_sum_warning_min": format(
                                    P15_WEIGHT_WARNING_MIN, "f"
                                ),
                                "weight_sum_warning_max": format(
                                    P15_WEIGHT_WARNING_MAX, "f"
                                ),
                                "warnings": [],
                            }
                        )
                    snapshot_checks.append(invalid_snapshot)
            if p15 and candidate_failures:
                # P1.5 retains and validates every actual trade_date.  A valid
                # sibling snapshot cannot hide an invalid snapshot in-month.
                raise AlphaFeasibilityDataError(
                    candidate_failures[-1], stage="pit"
                )
            if not valid_candidates:
                raise AlphaFeasibilityDataError(
                    candidate_failures[-1] if candidate_failures else "no_legal_snapshot_in_month",
                    stage="pit",
                )
            for candidate in valid_candidates:
                candidate_date = _parse_date(
                    candidate["snapshot_date"], "selected_snapshot"
                )
                if previous_selected is not None and candidate_date <= previous_selected:
                    raise AlphaFeasibilityDataError(
                        "selected_snapshot_dates_not_strictly_ordered", stage="pit"
                    )
                previous_selected = candidate_date
                selected_snapshots.append(candidate)
            selected = valid_candidates[-1]
            month_details.append(
                {
                    "month": month,
                    "request_artifact_sha256": request_sha,
                    "response_sha256": response_sha,
                    "snapshots": snapshot_checks,
                    "selected_snapshot_date": selected["snapshot_date"],
                    "status": "complete",
                    "issues": [],
                }
            )
        except AlphaFeasibilityDataError as exc:
            blockers.append(f"{month}:{exc.code}")
            month_details.append(
                {
                    "month": month,
                    "request_artifact_sha256": request_sha,
                    "response_sha256": response_sha,
                    "snapshots": snapshot_checks,
                    "selected_snapshot_date": None,
                    "status": "invalid",
                    "issues": [exc.code],
                }
            )

    passed = (
        not blockers
        and observed == len(plan.pit_tasks)
        and len(month_details) == len(plan.pit_tasks)
        and all(item["status"] == "complete" for item in month_details)
        and len(selected_snapshots) >= len(plan.pit_tasks)
    )
    union = tuple(
        sorted(
            {
                member["instrument_id"]
                for snapshot in selected_snapshots
                for member in snapshot["members"]
            }
        )
    ) if passed else ()
    blockers = sorted(set(blockers))
    all_snapshot_checks = [
        snapshot
        for month_detail in month_details
        for snapshot in month_detail["snapshots"]
    ]
    p15_summary = (
        {
            "pit_snapshot_count": len(all_snapshot_checks),
            "snapshot_dates": sorted(
                snapshot["snapshot_date"] for snapshot in all_snapshot_checks
            ),
            "missing_months": [
                item["month"] for item in month_details if item["status"] != "complete"
            ],
            "duplicate_member_count": duplicate_member_count,
            "zero_weight_count_by_snapshot": {
                snapshot["snapshot_date"]: snapshot["zero_weight_count"]
                for snapshot in all_snapshot_checks
            },
            "weight_sum_by_snapshot": {
                snapshot["snapshot_date"]: snapshot["weight_sum"]
                for snapshot in all_snapshot_checks
            },
        }
        if p15
        else {}
    )
    report = _self_hashed(
        {
            "schema_version": report_schema_version,
            "experiment_id": plan.config["experiment_id"],
            "generated_at": _generated_at(generated_at),
            "index_code": plan.config["index"]["index_code"],
            "pit_months_expected": 73,
            "pit_months_observed": observed,
            "monthly_checks": month_details,
            "stage_status": "PIT_MEMBERSHIP_READY" if passed else "BLOCKED_PIT_MEMBERSHIP",
            "terminal_status": None if passed else blocked_terminal_status,
            "remaining_blockers": blockers,
            "locked_test_status": dict(LOCKED_TEST_STATUS),
            "locked_test_consumed": False,
            **p15_summary,
        },
        "report_sha256",
    )
    manifest = _self_hashed(
        {
            "schema_version": manifest_schema_version,
            "experiment_id": plan.config["experiment_id"],
            "generated_at": report["generated_at"],
            "index_code": plan.config["index"]["index_code"],
            "coverage_start_month": plan.config["index"]["pit_first_month"],
            "coverage_end_month": plan.config["index"]["pit_last_month"],
            "pit_months_expected": 73,
            "pit_months_observed": observed,
            "snapshots": selected_snapshots if passed else [],
            "union_instrument_count": len(union),
            "union_instrument_ids": list(union),
            "stage_status": report["stage_status"],
            "remaining_blockers": blockers,
            "locked_test_status": dict(LOCKED_TEST_STATUS),
            "locked_test_consumed": False,
            **p15_summary,
        },
        "manifest_sha256",
    )
    return PitMembershipResult(
        coverage_report=MappingProxyType(report),
        manifest=MappingProxyType(manifest),
        union_instruments=union,
        passed=passed,
    )


def publish_pit_membership_artifacts(
    output_root: Path | str,
    result: PitMembershipResult,
    *,
    token: str | None = None,
) -> tuple[Path, Path]:
    root = Path(output_root)
    report_path = root / "pit_membership_coverage_report.json"
    manifest_path = root / "pit_membership_manifest.json"
    report_schema = result.coverage_report.get("schema_version")
    manifest_schema = result.manifest.get("schema_version")
    try:
        validate_json_schema(
            result.coverage_report, PIT_REPORT_SCHEMA_PATHS[report_schema]
        )
        validate_json_schema(
            result.manifest, PIT_MANIFEST_SCHEMA_PATHS[manifest_schema]
        )
    except (KeyError, SchemaValidationError) as exc:
        raise AlphaFeasibilityDataError("pit_artifact_schema_invalid", stage="pit") from exc
    _publish_or_verify_identical(report_path, result.coverage_report, token=token)
    _publish_or_verify_identical(manifest_path, result.manifest, token=token)
    return report_path, manifest_path


def select_pit_snapshot_on_or_before(
    snapshots: Sequence[Mapping[str, Any]], decision_date: date | str
) -> Mapping[str, Any]:
    """Causal PIT lookup; a future month can never backfill an earlier date."""

    decision = _parse_date(decision_date, "decision_date") if isinstance(decision_date, str) else decision_date
    if not isinstance(decision, date) or decision > ABSOLUTE_CUTOFF:
        raise AlphaFeasibilityDataError("post_cutoff_decision_date", stage="pit")
    all_dates = [
        _parse_date(snapshot.get("snapshot_date"), "snapshot_date")
        for snapshot in snapshots
    ]
    if all_dates != sorted(all_dates) or len(set(all_dates)) != len(all_dates):
        raise AlphaFeasibilityDataError(
            "pit_snapshot_dates_not_strictly_ordered", stage="pit"
        )
    eligible = [
        snapshot
        for snapshot in snapshots
        if _parse_date(snapshot.get("snapshot_date"), "snapshot_date") <= decision
    ]
    if not eligible:
        raise AlphaFeasibilityDataError("no_pit_snapshot_on_or_before_decision_date", stage="pit")
    return eligible[-1]


def _validate_pit_snapshot_month_coverage(
    plan: CollectionPlan,
    snapshots: Sequence[Mapping[str, Any]],
) -> tuple[date, ...]:
    dates = tuple(
        _parse_date(snapshot.get("snapshot_date"), "pit_snapshot_date")
        for snapshot in snapshots
    )
    if dates != tuple(sorted(dates)) or len(set(dates)) != len(dates):
        raise AlphaFeasibilityDataError(
            "pit_snapshot_dates_not_strictly_ordered", stage="pit"
        )
    expected_months = set(
        _month_sequence(
            plan.config["index"]["pit_first_month"],
            plan.config["index"]["pit_last_month"],
        )
    )
    observed_months = {item.strftime("%Y-%m") for item in dates}
    if observed_months != expected_months:
        raise AlphaFeasibilityDataError("pit_snapshot_month_coverage_invalid", stage="pit")
    return dates


def load_normalized_rows(
    output_root: Path | str,
    endpoint: str | None = None,
    *,
    plan_sha256: str | None = None,
    expected_tasks: Sequence[CollectionTask] | None = None,
) -> list[dict[str, Any]]:
    """Load only normalized response artifacts, never unrelated raw data."""

    if endpoint is not None and endpoint not in ALLOWED_ENDPOINTS:
        raise AlphaFeasibilityDataError("endpoint_not_allowed")
    if expected_tasks is not None:
        tasks = tuple(expected_tasks)
        if plan_sha256 is not None and any(task.plan_sha256 != plan_sha256 for task in tasks):
            raise AlphaFeasibilityDataError("expected_task_plan_mismatch")
        store = CreateOnlyTaskStore(output_root)
        verified: list[dict[str, Any]] = []
        for task in tasks:
            if endpoint is not None and task.endpoint != endpoint:
                continue
            if not store.is_complete(task):
                raise AlphaFeasibilityDataError("expected_task_artifact_incomplete")
            result = store._load_response(task)
            verified.extend(dict(row) for row in result.rows)
        return verified
    # Directory enumeration is not an authorization boundary.  Consumers must
    # supply the exact preflight-built tasks so a self-consistent forged
    # response (including post-cutoff rows) can never become loadable merely by
    # appearing under ``tasks/``.
    raise AlphaFeasibilityDataError("expected_tasks_required_for_plan_bound_load")


def _unique_rows(
    endpoint: str, rows: Sequence[Mapping[str, Any]]
) -> dict[tuple[Any, ...], Mapping[str, Any]]:
    result: dict[tuple[Any, ...], Mapping[str, Any]] = {}
    for row in rows:
        key = _primary_key(endpoint, row)
        if key in result:
            raise AlphaFeasibilityDataError("duplicate_primary_key_across_tasks")
        result[key] = row
    return result


def _aggregate_suspension_events(
    rows: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], Mapping[str, Any]]:
    """Fold exact events and expose one conservative state per stock-day."""

    events_by_day: dict[
        tuple[str, str], dict[tuple[str, str, str, str | None], int]
    ] = {}
    for observed in rows:
        code = _normalized_code(observed.get("ts_code"))
        trade_date = _compact(_parse_date(observed.get("trade_date"), "trade_date"))
        suspend_type = observed.get("suspend_type")
        if type(suspend_type) is not str or suspend_type not in {"S", "R"}:
            raise AlphaFeasibilityDataError("invalid_suspend_type")
        timing = observed.get("suspend_timing")
        if timing is not None and type(timing) is not str:
            raise AlphaFeasibilityDataError("invalid_suspend_timing")
        if type(timing) is str:
            timing = timing.strip() or None
        duplicate_count = observed.get("exact_duplicate_count", 0)
        if type(duplicate_count) is not int or duplicate_count < 0:
            raise AlphaFeasibilityDataError("invalid_exact_duplicate_count")
        day = (code, trade_date)
        identity = (code, trade_date, suspend_type, timing)
        event_counts = events_by_day.setdefault(day, {})
        event_counts[identity] = event_counts.get(identity, 0) + 1 + duplicate_count

    result: dict[tuple[str, str], Mapping[str, Any]] = {}
    full_markers = {None, "全天", "全日", "全天停牌", "全日停牌"}
    for day, event_counts in events_by_day.items():
        identities = tuple(event_counts)
        source_event_count = sum(event_counts.values())
        unique_event_count = len(identities)
        result[day] = MappingProxyType(
            {
                "ts_code": day[0],
                "trade_date": day[1],
                "full_session_suspended": any(
                    event[2] == "S" and event[3] in full_markers
                    for event in identities
                ),
                "partial_session_suspended": any(
                    event[2] == "S" and event[3] not in full_markers
                    for event in identities
                ),
                "resume_event_present": any(
                    event[2] == "R" for event in identities
                ),
                "source_event_count": source_event_count,
                "unique_event_count": unique_event_count,
                "exact_duplicate_count": source_event_count - unique_event_count,
            }
        )
    return result


def _usable_daily_bar(row: Mapping[str, Any] | None) -> bool:
    """Return whether a normalized bar is an actual traded-price fact."""

    if row is None:
        return False
    values: dict[str, Decimal] = {}
    for field in ("open", "high", "low", "close"):
        values[field], _ = _decimal(row.get(field), field, minimum=Decimal("0"))
    volume, _ = _decimal(row.get("vol"), "vol", minimum=Decimal("0"))
    amount, _ = _decimal(row.get("amount"), "amount", minimum=Decimal("0"))
    return (
        all(value > 0 for value in values.values())
        and values["high"] >= max(values["open"], values["close"], values["low"])
        and values["low"] <= min(values["open"], values["close"], values["high"])
        and (volume > 0 or amount > 0)
    )


def _suspension_daily_bar_conflict_keys(
    daily: Mapping[tuple[str, str], Mapping[str, Any]],
    suspensions: Mapping[tuple[str, str], Mapping[str, Any]],
) -> set[tuple[str, str]]:
    conflicts: set[tuple[str, str]] = set()
    for key, suspension in suspensions.items():
        bar = daily.get(key)
        if _is_full_session_suspension(suspension) and _usable_daily_bar(bar):
            conflicts.add(key)
    return conflicts


@dataclass(frozen=True, slots=True)
class HistoryCoverageResult:
    report: Mapping[str, Any]
    passed: bool
    trading_dates: tuple[str, ...]


def validate_history_coverage(
    plan: CollectionPlan,
    union_instruments: Sequence[str],
    rows_by_endpoint: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    pit_snapshots: Sequence[Mapping[str, Any]] | None = None,
    generated_at: datetime | None = None,
) -> HistoryCoverageResult:
    """Fail closed on missing history, except same-day ``S`` suspensions."""

    required = {"trade_cal", "daily", "adj_factor", "suspend_d", "index_daily"}
    if not required.issubset(rows_by_endpoint):
        missing = sorted(required - set(rows_by_endpoint))
        report = {
            "schema_version": HISTORY_COVERAGE_SCHEMA_VERSION,
            "generated_at": _generated_at(generated_at),
            "stage_status": "BLOCKED_DATA",
            "coverage_start": plan.config["dates"]["signal_warmup_start"],
            "coverage_end": plan.config["dates"]["validation_end"],
            "daily_coverage_status": "BLOCKED_DATA",
            "adj_factor_coverage_status": "BLOCKED_DATA",
            "suspension_coverage_status": "BLOCKED_DATA",
            "benchmark_coverage_status": "BLOCKED_DATA",
            "stock_basic_status": STOCK_BASIC_STATUS,
            "stock_basic_request_count": STOCK_BASIC_REQUEST_COUNT,
            "security_master_pit_status": SECURITY_MASTER_PIT_STATUS,
            "history_eligibility_status": INSUFFICIENT_HISTORY_STATUS,
            "valid_candidate_count_by_decision": {},
            "insufficient_history_count_by_decision": {},
            "ineligible_no_initial_price_count": 0,
            "unexplained_market_data_gap_count": 0,
            "unavailable_stock_day_count": 0,
            "suspension_daily_bar_conflict_count": 0,
            "off_calendar_adj_factor_count": 0,
            "blockers": [{"reason": "missing_endpoint_artifacts", "endpoints": missing}],
            "locked_test_status": dict(LOCKED_TEST_STATUS),
            "locked_test_consumed": False,
        }
        return HistoryCoverageResult(report=MappingProxyType(report), passed=False, trading_dates=())

    union = tuple(sorted(set(union_instruments)))
    if not union or len(union) != len(union_instruments):
        raise AlphaFeasibilityDataError("invalid_union_for_coverage")
    union_set = set(union)
    blockers: list[dict[str, Any]] = []
    warmup = _parse_date(plan.config["dates"]["signal_warmup_start"], "warmup_start")
    end = _parse_date(plan.config["dates"]["validation_end"], "validation_end")

    calendar_rows = list(rows_by_endpoint["trade_cal"])
    try:
        calendar_index = _unique_rows("trade_cal", calendar_rows)
        calendar_dates = sorted(_parse_date(key[1], "calendar_date") for key in calendar_index)
        expected_calendar_dates: list[date] = []
        cursor = warmup
        while cursor <= end:
            expected_calendar_dates.append(cursor)
            cursor += timedelta(days=1)
        if calendar_dates != expected_calendar_dates:
            raise AlphaFeasibilityDataError("trade_calendar_window_incomplete")
        ordered_calendar = sorted(calendar_rows, key=lambda item: item["cal_date"])
        last_open: date | None = None
        open_values: list[str] = []
        for row in ordered_calendar:
            current = _parse_date(row["cal_date"], "calendar_date")
            pretrade = (
                _parse_date(row["pretrade_date"], "pretrade_date")
                if row.get("pretrade_date")
                else None
            )
            if last_open is not None and pretrade != last_open:
                raise AlphaFeasibilityDataError("trade_calendar_pretrade_mapping_invalid")
            if int(row["is_open"]) == 1:
                open_values.append(_compact(current))
                last_open = current
        open_dates = tuple(open_values)
        if not open_dates or tuple(sorted(set(open_dates))) != open_dates:
            raise AlphaFeasibilityDataError("open_calendar_not_strictly_ordered")
        open_by_year: dict[int, int] = {}
        for item in open_dates:
            session = _parse_date(item, "open_session")
            if session.weekday() >= 5:
                raise AlphaFeasibilityDataError("weekend_marked_as_open_session")
            open_by_year[session.year] = open_by_year.get(session.year, 0) + 1
        for year, minimum in MINIMUM_OPEN_SESSIONS_BY_YEAR.items():
            count = open_by_year.get(year, 0)
            if count < minimum or count > MAXIMUM_OPEN_SESSIONS_PER_YEAR:
                raise AlphaFeasibilityDataError("implausible_annual_open_session_count")
        # A next-session mapping is formed only within the authorized window.
        # The last session is deliberately terminal and never maps into 2024.
        next_session = {open_dates[index]: open_dates[index + 1] for index in range(len(open_dates) - 1)}
        if any(_parse_date(value, "next_session") > ABSOLUTE_CUTOFF for value in next_session.values()):
            raise AlphaFeasibilityDataError("cross_cutoff_next_session")
    except AlphaFeasibilityDataError as exc:
        blockers.append({"reason": exc.code})
        open_dates = ()

    benchmark_status = "COMPLETE"
    if open_dates:
        try:
            benchmark_index = _unique_rows("index_daily", rows_by_endpoint["index_daily"])
            benchmark_dates = {
                key[1] for key, row in benchmark_index.items() if row["ts_code"] == "000906.SH"
            }
            if benchmark_dates != set(open_dates):
                raise AlphaFeasibilityDataError("benchmark_calendar_session_set_mismatch")
            if pit_snapshots is not None:
                pit_dates = _validate_pit_snapshot_month_coverage(plan, pit_snapshots)
                if not {_compact(item) for item in pit_dates}.issubset(benchmark_dates):
                    raise AlphaFeasibilityDataError("pit_snapshot_not_on_controlled_open_session")
        except AlphaFeasibilityDataError as exc:
            benchmark_status = "BLOCKED_DATA"
            blockers.append({"reason": exc.code})
    else:
        benchmark_status = "BLOCKED_DATA"

    snapshot_members_by_date: list[tuple[date, tuple[str, ...]]] = []
    try:
        if pit_snapshots is None:
            raise AlphaFeasibilityDataError("pit_snapshots_required_for_history_coverage")
        _validate_pit_snapshot_month_coverage(plan, pit_snapshots)
        for snapshot in pit_snapshots:
            snapshot_date = _parse_date(
                snapshot.get("snapshot_date"), "pit_snapshot_date"
            )
            raw_members = snapshot.get("members")
            if not isinstance(raw_members, (list, tuple)):
                raise AlphaFeasibilityDataError("pit_snapshot_members_invalid")
            members: list[str] = []
            for member in raw_members:
                code = (
                    member.get("instrument_id")
                    if isinstance(member, Mapping)
                    else member
                )
                if type(code) is not str or _PIT_COMPONENT_CODE.fullmatch(code) is None:
                    raise AlphaFeasibilityDataError("pit_snapshot_member_code_invalid")
                if code not in union_set:
                    raise AlphaFeasibilityDataError(
                        "pit_member_outside_index_weight_union"
                    )
                members.append(code)
            if len(members) != len(set(members)):
                raise AlphaFeasibilityDataError("pit_snapshot_members_duplicate")
            snapshot_members_by_date.append((snapshot_date, tuple(sorted(members))))
    except AlphaFeasibilityDataError as exc:
        blockers.append({"reason": exc.code})

    daily_status = "COMPLETE"
    suspension_status = "COMPLETE"
    adj_status = "COMPLETE"
    daily_index: dict[tuple[str, str], Mapping[str, Any]] = {}
    suspend_index: dict[tuple[str, str], Mapping[str, Any]] = {}
    adj_index: dict[tuple[str, str], Mapping[str, Any]] = {}
    adj_by_code: dict[str, list[tuple[date, Decimal]]] = {code: [] for code in union}
    try:
        daily_index = _unique_rows("daily", rows_by_endpoint["daily"])
        # A PIT member can legitimately have no daily row yet when its first
        # inclusion is a full-session suspension with no initial price.  Such
        # members are concluded below as ``ineligible_no_initial_price``.
        # Rows outside the PIT union remain a hard scope mismatch.
        if not {key[0] for key in daily_index}.issubset(union_set):
            raise AlphaFeasibilityDataError("index_weight_daily_code_mismatch")
        if any(key[1] not in set(open_dates) for key in daily_index):
            raise AlphaFeasibilityDataError("daily_row_not_on_open_session")
    except AlphaFeasibilityDataError as exc:
        daily_status = "BLOCKED_DATA"
        blockers.append({"reason": exc.code})
    try:
        suspend_index = _aggregate_suspension_events(rows_by_endpoint["suspend_d"])
        if any(key[0] not in union_set for key in suspend_index):
            raise AlphaFeasibilityDataError("suspension_contains_non_union_instrument")
        if any(key[1] not in set(open_dates) for key in suspend_index):
            raise AlphaFeasibilityDataError("suspension_row_not_on_open_session")
    except AlphaFeasibilityDataError as exc:
        suspension_status = "BLOCKED_DATA"
        blockers.append({"reason": exc.code})
    suspension_daily_bar_conflicts = _suspension_daily_bar_conflict_keys(
        daily_index, suspend_index
    )
    off_calendar_adj_factor_count = 0
    try:
        adj_index = _unique_rows("adj_factor", rows_by_endpoint["adj_factor"])
        if any(key[0] not in union_set for key in adj_index):
            raise AlphaFeasibilityDataError("adj_factor_contains_non_union_instrument")
        off_calendar_adj_factor_count = sum(
            key[1] not in set(open_dates) for key in adj_index
        )
        for (code, trade_date), row in adj_index.items():
            factor, _ = _decimal(row["adj_factor"], "adj_factor", minimum=Decimal("0"))
            if factor <= 0:
                raise AlphaFeasibilityDataError("nonpositive_adj_factor")
            adj_by_code[code].append((_parse_date(trade_date, "adj_trade_date"), factor))
        for values in adj_by_code.values():
            values.sort(key=lambda item: item[0])
    except AlphaFeasibilityDataError as exc:
        adj_status = "BLOCKED_DATA"
        blockers.append({"reason": exc.code})

    missing_adj: set[tuple[str, str]] = set()
    for key in daily_index:
        code, trade_date_text = key
        trade_date = _parse_date(trade_date_text, "daily_trade_date")
        if not adj_by_code[code] or adj_by_code[code][0][0] > trade_date:
            missing_adj.add(key)
    explained_suspension_missing: set[tuple[str, str]] = set()
    unexplained_missing: set[tuple[str, str]] = set()
    valid_candidate_count_by_decision: dict[str, int] = {}
    insufficient_history_count_by_decision: dict[str, int] = {}
    ineligible_no_initial_price_count = 0
    if (
        open_dates
        and snapshot_members_by_date
        and daily_status == "COMPLETE"
        and suspension_status == "COMPLETE"
        and adj_status == "COMPLETE"
    ):
        try:
            open_date_values = tuple(
                _parse_date(item, "open_session") for item in open_dates
            )
            open_position = {
                trading_date: index for index, trading_date in enumerate(open_date_values)
            }
            development_start = _parse_date(
                plan.config["dates"]["development_start"], "development_start"
            )
            report_dates = tuple(
                item for item in open_date_values if development_start <= item <= end
            )
            if len(report_dates) < 2 or open_position[report_dates[0]] == 0:
                raise AlphaFeasibilityDataError("decision_calendar_window_incomplete")
            first_decision = open_date_values[open_position[report_dates[0]] - 1]
            decision_dates = (first_decision, *report_dates[:-1])

            valid_signal_keys: set[tuple[str, str]] = set()
            first_real_position: dict[str, int] = {}
            for code in union:
                for position, trade_date_text in enumerate(open_dates):
                    key = (code, trade_date_text)
                    if (
                        key in daily_index
                        and key not in missing_adj
                        and _usable_daily_bar(daily_index[key])
                    ):
                        first_real_position.setdefault(code, position)
                        valid_signal_keys.add(key)

            first_membership_position: dict[str, int] = {}
            for snapshot_date, members in snapshot_members_by_date:
                if snapshot_date not in open_position:
                    raise AlphaFeasibilityDataError(
                        "pit_snapshot_not_on_controlled_open_session"
                    )
                snapshot_position = open_position[snapshot_date]
                for code in members:
                    first_membership_position.setdefault(code, snapshot_position)

            # Missing observations remain diagnostic stock-days.  They no
            # longer invalidate unrelated instruments or the complete dataset.
            for code in union:
                first_position = first_real_position.get(code)
                observed_positions = [
                    position
                    for position, trade_date_text in enumerate(open_dates)
                    if (code, trade_date_text) in daily_index
                    or (code, trade_date_text) in suspend_index
                ]
                if first_position is None or not observed_positions:
                    continue
                for position in range(first_position, max(observed_positions) + 1):
                    key = (code, open_dates[position])
                    if key not in valid_signal_keys:
                        if _is_full_session_suspension(suspend_index.get(key)):
                            explained_suspension_missing.add(key)
                        else:
                            unexplained_missing.add(key)

            valid_observation_prefix_by_code: dict[str, list[int]] = {}
            for code in union:
                running = 0
                prefix: list[int] = []
                for trade_date_text in open_dates:
                    running += (code, trade_date_text) in valid_signal_keys
                    prefix.append(running)
                valid_observation_prefix_by_code[code] = prefix

            for decision_date in decision_dates:
                visible = [
                    item
                    for item in snapshot_members_by_date
                    if item[0] <= decision_date
                ]
                if not visible:
                    raise AlphaFeasibilityDataError(
                        "no_pit_snapshot_visible_for_decision"
                    )
                members = max(visible, key=lambda item: item[0])[1]
                decision_position = open_position[decision_date]
                valid_count = 0
                insufficient_count = 0
                no_initial_price_count = 0
                unavailable_count = 0
                for code in members:
                    decision_key = (code, _compact(decision_date))
                    valid_observations = valid_observation_prefix_by_code[code][
                        decision_position
                    ]
                    if valid_observations < MINIMUM_VALID_CONTROLLED_SESSIONS:
                        insufficient_count += 1
                    elif decision_key not in valid_signal_keys:
                        unavailable_count += 1
                        if _is_full_session_suspension(
                            suspend_index.get(decision_key)
                        ):
                            explained_suspension_missing.add(decision_key)
                        elif decision_key not in missing_adj:
                            unexplained_missing.add(decision_key)
                    else:
                        valid_count += 1
                if (
                    valid_count
                    + insufficient_count
                    + no_initial_price_count
                    + unavailable_count
                    != len(members)
                ):
                    raise AlphaFeasibilityDataError(
                        "pit_member_eligibility_conclusions_incomplete"
                    )
                decision_text = _iso(decision_date)
                valid_candidate_count_by_decision[decision_text] = valid_count
                insufficient_history_count_by_decision[
                    decision_text
                ] = insufficient_count
                ineligible_no_initial_price_count += no_initial_price_count

        except AlphaFeasibilityDataError as exc:
            blockers.append({"reason": exc.code})
    passed = not blockers
    report = {
        "schema_version": HISTORY_COVERAGE_SCHEMA_VERSION,
        "experiment_id": plan.config["experiment_id"],
        "generated_at": _generated_at(generated_at),
        "stage_status": "PASS" if passed else "BLOCKED_DATA",
        "coverage_start": _iso(warmup),
        "coverage_end": _iso(end),
        "union_instrument_count": len(union),
        "open_session_count": len(open_dates),
        "daily_coverage_status": daily_status,
        "adj_factor_coverage_status": adj_status,
        "suspension_coverage_status": suspension_status,
        "benchmark_coverage_status": benchmark_status,
        "stock_basic_status": STOCK_BASIC_STATUS,
        "stock_basic_request_count": STOCK_BASIC_REQUEST_COUNT,
        "security_master_pit_status": SECURITY_MASTER_PIT_STATUS,
        "history_eligibility_status": INSUFFICIENT_HISTORY_STATUS,
        "valid_candidate_count_by_decision": valid_candidate_count_by_decision,
        "insufficient_history_count_by_decision": (
            insufficient_history_count_by_decision
        ),
        "ineligible_no_initial_price_count": ineligible_no_initial_price_count,
        "same_day_suspension_explained_missing_daily_count": len(
            explained_suspension_missing
        ),
        "non_suspension_missing_daily_count": len(unexplained_missing),
        "unexplained_market_data_gap_count": len(unexplained_missing),
        "missing_causal_adj_factor_count": len(missing_adj),
        "unavailable_stock_day_count": len(
            explained_suspension_missing | unexplained_missing | missing_adj
        ),
        "suspension_daily_bar_conflict_count": len(
            suspension_daily_bar_conflicts
        ),
        "off_calendar_adj_factor_count": off_calendar_adj_factor_count,
        "terminal_session_next_session": None,
        "blockers": blockers,
        "locked_test_status": dict(LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
    }
    return HistoryCoverageResult(
        report=MappingProxyType(report), passed=passed, trading_dates=open_dates
    )


def validate_history_coverage_from_store(
    plan: CollectionPlan,
    union_instruments: Sequence[str],
    tasks: Sequence[CollectionTask],
    store: CreateOnlyTaskStore,
    *,
    pit_snapshots: Sequence[Mapping[str, Any]],
    generated_at: datetime | None = None,
) -> HistoryCoverageResult:
    """Bounded-memory history coverage replay, one 3-stock batch at a time."""

    by_endpoint = {
        endpoint: [task for task in tasks if task.endpoint == endpoint]
        for endpoint in ALLOWED_ENDPOINTS
    }

    def load_task(task: CollectionTask) -> TaskExecutionResult:
        if not store.is_complete(task):
            raise AlphaFeasibilityDataError("expected_task_artifact_incomplete")
        return store._load_response(task)

    if len(by_endpoint["trade_cal"]) != 1 or len(by_endpoint["index_daily"]) != 1:
        raise AlphaFeasibilityDataError("global_history_task_plan_invalid")
    calendar_rows = list(load_task(by_endpoint["trade_cal"][0]).rows)
    benchmark_rows = list(load_task(by_endpoint["index_daily"][0]).rows)
    adj_by_code = {
        task.scope_instruments[0]: task
        for task in by_endpoint["adj_factor"]
        if len(task.scope_instruments) == 1
    }
    suspend_by_scope = {task.scope_instruments: task for task in by_endpoint["suspend_d"]}
    if set(adj_by_code) != set(union_instruments):
        raise AlphaFeasibilityDataError("adj_factor_task_plan_incomplete")

    union_set = set(union_instruments)

    def pit_snapshots_for_scope(scope: Sequence[str]) -> list[dict[str, Any]]:
        scope_set = set(scope)
        filtered: list[dict[str, Any]] = []
        for snapshot in pit_snapshots:
            if not isinstance(snapshot, Mapping):
                raise AlphaFeasibilityDataError("pit_snapshot_members_invalid")
            members = snapshot.get("members")
            if not isinstance(members, (list, tuple)):
                raise AlphaFeasibilityDataError("pit_snapshot_members_invalid")
            scoped_members: list[Any] = []
            for member in members:
                if isinstance(member, Mapping):
                    code = member.get("instrument_id")
                elif type(member) is str:
                    code = member
                else:
                    raise AlphaFeasibilityDataError("pit_snapshot_members_invalid")
                if code not in union_set:
                    raise AlphaFeasibilityDataError(
                        "pit_member_outside_index_weight_union"
                    )
                if code in scope_set:
                    scoped_members.append(member)
            filtered.append({**dict(snapshot), "members": scoped_members})
        return filtered

    partials: list[HistoryCoverageResult] = []
    for daily_task in by_endpoint["daily"]:
        scope = daily_task.scope_instruments
        suspend_task = suspend_by_scope.get(scope)
        if suspend_task is None:
            raise AlphaFeasibilityDataError("suspension_task_scope_mismatch")
        daily_rows = list(load_task(daily_task).rows)
        adj_rows: list[Mapping[str, Any]] = []
        for code in scope:
            adj_rows.extend(load_task(adj_by_code[code]).rows)
        suspension_rows = list(load_task(suspend_task).rows)
        partials.append(
            validate_history_coverage(
                plan,
                list(scope),
                {
                    "trade_cal": calendar_rows,
                    "daily": daily_rows,
                    "adj_factor": adj_rows,
                    "suspend_d": suspension_rows,
                    "index_daily": benchmark_rows,
                },
                pit_snapshots=pit_snapshots_for_scope(scope),
                generated_at=generated_at,
            )
        )
    covered = {code for task in by_endpoint["daily"] for code in task.scope_instruments}
    if covered != set(union_instruments) or not partials:
        raise AlphaFeasibilityDataError("daily_task_plan_incomplete")
    trading_dates = partials[0].trading_dates
    if any(part.trading_dates != trading_dates for part in partials):
        raise AlphaFeasibilityDataError("batch_calendar_replay_mismatch")
    blocker_reasons = sorted(
        {
            str(item.get("reason", "history_coverage_incomplete"))
            for part in partials
            for item in part.report.get("blockers", [])
            if isinstance(item, Mapping)
        }
    )
    passed = all(part.passed for part in partials)
    decision_keys = sorted(
        {
            key
            for part in partials
            for key in part.report.get("valid_candidate_count_by_decision", {})
        }
        | {
            key
            for part in partials
            for key in part.report.get("insufficient_history_count_by_decision", {})
        }
    )
    valid_candidate_count_by_decision = {
        key: sum(
            int(part.report.get("valid_candidate_count_by_decision", {}).get(key, 0))
            for part in partials
        )
        for key in decision_keys
    }
    insufficient_history_count_by_decision = {
        key: sum(
            int(
                part.report.get("insufficient_history_count_by_decision", {}).get(
                    key, 0
                )
            )
            for part in partials
        )
        for key in decision_keys
    }
    ineligible_no_initial_price_count = sum(
        int(part.report.get("ineligible_no_initial_price_count", 0))
        for part in partials
    )
    if passed and not decision_keys:
        raise AlphaFeasibilityDataError("decision_history_counts_missing")
    if passed:
        expected_decision_keys = set(decision_keys)
        for part in partials:
            if set(part.report.get("valid_candidate_count_by_decision", {})) != (
                expected_decision_keys
            ) or set(
                part.report.get("insufficient_history_count_by_decision", {})
            ) != expected_decision_keys:
                raise AlphaFeasibilityDataError("decision_history_count_keys_mismatch")
    report = {
        "schema_version": HISTORY_COVERAGE_SCHEMA_VERSION,
        "experiment_id": plan.config["experiment_id"],
        "generated_at": _generated_at(generated_at),
        "stage_status": "PASS" if passed else "BLOCKED_DATA",
        "coverage_start": plan.config["dates"]["signal_warmup_start"],
        "coverage_end": plan.config["dates"]["validation_end"],
        "union_instrument_count": len(union_instruments),
        "open_session_count": len(trading_dates),
        "daily_coverage_status": (
            "COMPLETE"
            if all(part.report["daily_coverage_status"] == "COMPLETE" for part in partials)
            else "BLOCKED_DATA"
        ),
        "adj_factor_coverage_status": (
            "COMPLETE"
            if all(part.report["adj_factor_coverage_status"] == "COMPLETE" for part in partials)
            else "BLOCKED_DATA"
        ),
        "suspension_coverage_status": (
            "COMPLETE"
            if all(part.report["suspension_coverage_status"] == "COMPLETE" for part in partials)
            else "BLOCKED_DATA"
        ),
        "benchmark_coverage_status": (
            "COMPLETE"
            if all(part.report["benchmark_coverage_status"] == "COMPLETE" for part in partials)
            else "BLOCKED_DATA"
        ),
        "stock_basic_status": STOCK_BASIC_STATUS,
        "stock_basic_request_count": STOCK_BASIC_REQUEST_COUNT,
        "security_master_pit_status": SECURITY_MASTER_PIT_STATUS,
        "history_eligibility_status": INSUFFICIENT_HISTORY_STATUS,
        "valid_candidate_count_by_decision": valid_candidate_count_by_decision,
        "insufficient_history_count_by_decision": (
            insufficient_history_count_by_decision
        ),
        "ineligible_no_initial_price_count": ineligible_no_initial_price_count,
        "same_day_suspension_explained_missing_daily_count": sum(
            int(part.report["same_day_suspension_explained_missing_daily_count"])
            for part in partials
        ),
        "non_suspension_missing_daily_count": sum(
            int(part.report["non_suspension_missing_daily_count"]) for part in partials
        ),
        "unexplained_market_data_gap_count": sum(
            int(part.report["unexplained_market_data_gap_count"])
            for part in partials
        ),
        "missing_causal_adj_factor_count": sum(
            int(part.report["missing_causal_adj_factor_count"]) for part in partials
        ),
        "unavailable_stock_day_count": sum(
            int(part.report["unavailable_stock_day_count"]) for part in partials
        ),
        "suspension_daily_bar_conflict_count": sum(
            int(part.report["suspension_daily_bar_conflict_count"])
            for part in partials
        ),
        "off_calendar_adj_factor_count": sum(
            int(part.report["off_calendar_adj_factor_count"]) for part in partials
        ),
        "terminal_session_next_session": None,
        "blockers": [
            (
                {
                    "reason": UNEXPLAINED_MARKET_DATA_GAP_STATUS,
                    "count": sum(
                        int(part.report["unexplained_market_data_gap_count"])
                        for part in partials
                    ),
                    "sample": [
                        list(item)
                        for item in sorted(
                            {
                                tuple(sample)
                                for part in partials
                                for blocker in part.report.get("blockers", [])
                                if isinstance(blocker, Mapping)
                                and blocker.get("reason")
                                == UNEXPLAINED_MARKET_DATA_GAP_STATUS
                                for sample in blocker.get("sample", [])
                                if isinstance(sample, (list, tuple))
                                and len(sample) == 2
                            }
                        )[:20]
                    ],
                }
                if reason == UNEXPLAINED_MARKET_DATA_GAP_STATUS
                else {"reason": reason}
            )
            for reason in blocker_reasons
        ],
        "locked_test_status": dict(LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
    }
    return HistoryCoverageResult(
        report=MappingProxyType(report), passed=passed, trading_dates=trading_dates
    )


def build_history_manifest(
    plan: CollectionPlan,
    tasks: Sequence[CollectionTask],
    results: Mapping[str, TaskExecutionResult],
    coverage: HistoryCoverageResult,
    *,
    pit_result: PitMembershipResult | None = None,
    request_counts: Mapping[str, int] | None = None,
    generated_at: datetime | None = None,
) -> Mapping[str, Any]:
    expected_by_endpoint = {
        endpoint: sum(task.endpoint == endpoint for task in tasks)
        for endpoint in ALLOWED_ENDPOINTS
    }
    results_by_endpoint: dict[str, list[Mapping[str, Any]]] = {
        endpoint: [] for endpoint in ALLOWED_ENDPOINTS
    }
    completed_by_endpoint = {endpoint: 0 for endpoint in ALLOWED_ENDPOINTS}
    for result in results.values():
        completed_by_endpoint[result.task.endpoint] += 1
        results_by_endpoint[result.task.endpoint].extend(result.rows)

    status_by_endpoint = {
        "trade_cal": "complete" if coverage.passed else "partial",
        "daily": "complete" if coverage.report.get("daily_coverage_status") == "COMPLETE" else "invalid",
        "adj_factor": "complete" if coverage.report.get("adj_factor_coverage_status") == "COMPLETE" else "invalid",
        "suspend_d": "complete" if coverage.report.get("suspension_coverage_status") == "COMPLETE" else "invalid",
        "index_daily": "complete" if coverage.report.get("benchmark_coverage_status") == "COMPLETE" else "invalid",
    }
    for endpoint in tuple(status_by_endpoint):
        if completed_by_endpoint[endpoint] == 0:
            status_by_endpoint[endpoint] = "missing"
        elif completed_by_endpoint[endpoint] < expected_by_endpoint[endpoint]:
            status_by_endpoint[endpoint] = "partial"

    endpoint_date_field = {
        "trade_cal": "cal_date",
        "daily": "trade_date",
        "adj_factor": "trade_date",
        "suspend_d": "trade_date",
        "index_daily": "trade_date",
    }

    def dataset(endpoint: str, status: str) -> dict[str, Any]:
        rows = [dict(row) for row in results_by_endpoint[endpoint]]
        date_field = endpoint_date_field.get(endpoint)
        dates = sorted(
            _parse_date(row[date_field], "dataset_date")
            for row in rows
            if date_field is not None and row.get(date_field)
        )
        return {
            "status": status,
            "endpoint": endpoint,
            "record_count": len(rows),
            "coverage_start": _iso(dates[0]) if dates else None,
            "coverage_end": _iso(dates[-1]) if dates else None,
            "normalized_content_sha256": canonical_sha256(rows) if rows else None,
            "issues": [] if status == "complete" else [f"{endpoint}_{status}"],
        }

    pit_ready = pit_result is not None and pit_result.passed
    pit_snapshots = list(pit_result.manifest["snapshots"]) if pit_ready else []
    datasets = {
        "trade_calendar": dataset("trade_cal", status_by_endpoint["trade_cal"]),
        "pit_membership": {
            "status": "complete" if pit_ready else "missing",
            "endpoint": "index_weight",
            "record_count": sum(len(item["members"]) for item in pit_snapshots),
            "coverage_start": (
                plan.config["index"]["pit_first_month"] + "-01" if pit_ready else None
            ),
            "coverage_end": (
                max(item["snapshot_date"] for item in pit_snapshots) if pit_snapshots else None
            ),
            "normalized_content_sha256": canonical_sha256(pit_snapshots) if pit_snapshots else None,
            "issues": [] if pit_ready else ["pit_membership_missing"],
        },
        "daily": dataset("daily", status_by_endpoint["daily"]),
        "adj_factor": dataset("adj_factor", status_by_endpoint["adj_factor"]),
        "suspension": dataset("suspend_d", status_by_endpoint["suspend_d"]),
        "benchmark": dataset("index_daily", status_by_endpoint["index_daily"]),
    }
    blockers = sorted(
        {
            str(item.get("reason", "history_coverage_incomplete"))
            for item in coverage.report.get("blockers", [])
            if isinstance(item, Mapping)
        }
    )
    complete = coverage.passed and len(results) == len(tasks) and pit_ready
    if complete and int(coverage.report.get("unexplained_market_data_gap_count", 0)) != 0:
        raise AlphaFeasibilityDataError("ready_history_contains_unexplained_gap")
    counts = (
        _strict_request_counts(request_counts)
        if request_counts is not None
        else {
            endpoint: (73 if endpoint == "index_weight" and pit_ready else 0)
            + completed_by_endpoint[endpoint]
            for endpoint in ALLOWED_ENDPOINTS
        }
    )
    manifest = _self_hashed(
        {
            "schema_version": _history_manifest_schema_version(plan),
            "experiment_id": plan.config["experiment_id"],
            "generated_at": _generated_at(generated_at),
            "coverage_start": plan.config["dates"]["signal_warmup_start"],
            "coverage_end": plan.config["dates"]["validation_end"],
            "actual_tushare_request_count_by_endpoint": counts,
            **(
                {"request_count_semantics": P15_REQUEST_COUNT_SEMANTICS}
                if _p15_enabled(plan)
                else {}
            ),
            "stock_basic_status": STOCK_BASIC_STATUS,
            "stock_basic_request_count": STOCK_BASIC_REQUEST_COUNT,
            "security_master_pit_status": SECURITY_MASTER_PIT_STATUS,
            "pit_months_expected": 73,
            "pit_months_observed": (
                int(pit_result.coverage_report["pit_months_observed"]) if pit_result else 0
            ),
            "union_instrument_count": len(pit_result.union_instruments) if pit_result else 0,
            "valid_candidate_count_by_decision": dict(
                coverage.report.get("valid_candidate_count_by_decision", {})
            ),
            "insufficient_history_count_by_decision": dict(
                coverage.report.get("insufficient_history_count_by_decision", {})
            ),
            "ineligible_no_initial_price_count": int(
                coverage.report.get("ineligible_no_initial_price_count", 0)
            ),
            "unexplained_market_data_gap_count": int(
                coverage.report.get("unexplained_market_data_gap_count", 0)
            ),
            "datasets": datasets,
            "data_status": "READY" if complete else "BLOCKED_DATA",
            "remaining_blockers": blockers if blockers else ([] if complete else ["history_tasks_incomplete"]),
            "locked_test_status": dict(LOCKED_TEST_STATUS),
            "locked_test_consumed": False,
            "safety": _manifest_safety(plan),
        },
        "manifest_sha256",
    )
    return MappingProxyType(manifest)


def build_history_manifest_from_store(
    plan: CollectionPlan,
    tasks: Sequence[CollectionTask],
    store: CreateOnlyTaskStore,
    coverage: HistoryCoverageResult,
    *,
    pit_result: PitMembershipResult,
    request_counts: Mapping[str, int],
    generated_at: datetime | None = None,
) -> Mapping[str, Any]:
    """Build a Merkle-style dataset manifest without retaining all rows."""

    summaries: dict[str, list[dict[str, Any]]] = {endpoint: [] for endpoint in ALLOWED_ENDPOINTS}
    for task in tasks:
        if not store.is_complete(task):
            continue
        result = store._load_response(task)
        summaries[task.endpoint].append(
            {
                "task_id": task.task_id,
                "row_count": len(result.rows),
                "normalized_rows_sha256": canonical_sha256([dict(row) for row in result.rows]),
                "raw_response_sha256": result.raw_response_sha256,
                "wire_response_sha256": result.wire_response_sha256,
                "response_artifact_sha256": result.response_artifact_sha256,
                "isolated_future_delist_date_count": (
                    result.isolated_future_delist_date_count
                ),
                "isolated_non_union_row_count": result.isolated_non_union_row_count,
            }
        )
        del result

    expected = {
        endpoint: sum(task.endpoint == endpoint for task in tasks)
        for endpoint in ALLOWED_ENDPOINTS
    }
    coverage_status = {
        "trade_cal": "COMPLETE" if coverage.trading_dates else "BLOCKED_DATA",
        "daily": coverage.report.get("daily_coverage_status"),
        "adj_factor": coverage.report.get("adj_factor_coverage_status"),
        "suspend_d": coverage.report.get("suspension_coverage_status"),
        "index_daily": coverage.report.get("benchmark_coverage_status"),
    }

    def dataset(endpoint: str) -> dict[str, Any]:
        entries = summaries[endpoint]
        normalized_entries = [
            {
                "task_id": item["task_id"],
                "row_count": item["row_count"],
                "normalized_rows_sha256": item["normalized_rows_sha256"],
            }
            for item in entries
        ]
        if not entries:
            status = "missing"
        elif len(entries) < expected[endpoint]:
            status = "partial"
        elif coverage_status[endpoint] == "COMPLETE":
            status = "complete"
        else:
            status = "invalid"
        return {
            "status": status,
            "endpoint": endpoint,
            "record_count": sum(int(item["row_count"]) for item in entries),
            "coverage_start": plan.config["dates"]["signal_warmup_start"] if entries else None,
            "coverage_end": plan.config["dates"]["validation_end"] if entries else None,
            # Raw/wire/receipt hashes are transport provenance, not dataset
            # content identity.  In particular request_id cannot influence the
            # Experiment input content hash.
            "normalized_content_sha256": (
                canonical_sha256(normalized_entries) if normalized_entries else None
            ),
            "issues": [] if status == "complete" else [f"{endpoint}_{status}"],
        }

    pit_snapshots = list(pit_result.manifest["snapshots"])
    datasets = {
        "trade_calendar": dataset("trade_cal"),
        "pit_membership": {
            "status": "complete" if pit_result.passed else "missing",
            "endpoint": "index_weight",
            "record_count": sum(len(item["members"]) for item in pit_snapshots),
            "coverage_start": min(item["snapshot_date"] for item in pit_snapshots) if pit_snapshots else None,
            "coverage_end": max(item["snapshot_date"] for item in pit_snapshots) if pit_snapshots else None,
            "normalized_content_sha256": canonical_sha256(pit_snapshots) if pit_snapshots else None,
            "issues": [] if pit_result.passed else ["pit_membership_missing"],
        },
        "daily": dataset("daily"),
        "adj_factor": dataset("adj_factor"),
        "suspension": dataset("suspend_d"),
        "benchmark": dataset("index_daily"),
    }
    blockers = sorted(
        {
            str(item.get("reason", "history_coverage_incomplete"))
            for item in coverage.report.get("blockers", [])
            if isinstance(item, Mapping)
        }
    )
    complete = coverage.passed and all(
        len(summaries[endpoint]) == expected[endpoint]
        for endpoint in ("trade_cal", "daily", "adj_factor", "suspend_d", "index_daily")
    )
    return MappingProxyType(
        _self_hashed(
            {
                "schema_version": _history_manifest_schema_version(plan),
                "experiment_id": plan.config["experiment_id"],
                "generated_at": _generated_at(generated_at),
                "coverage_start": plan.config["dates"]["signal_warmup_start"],
                "coverage_end": plan.config["dates"]["validation_end"],
                "actual_tushare_request_count_by_endpoint": _strict_request_counts(
                    request_counts
                ),
                **(
                    {"request_count_semantics": P15_REQUEST_COUNT_SEMANTICS}
                    if _p15_enabled(plan)
                    else {}
                ),
                "stock_basic_status": STOCK_BASIC_STATUS,
                "stock_basic_request_count": STOCK_BASIC_REQUEST_COUNT,
                "security_master_pit_status": SECURITY_MASTER_PIT_STATUS,
                "pit_months_expected": 73,
                "pit_months_observed": int(pit_result.coverage_report["pit_months_observed"]),
                "union_instrument_count": len(pit_result.union_instruments),
                "valid_candidate_count_by_decision": dict(
                    coverage.report.get("valid_candidate_count_by_decision", {})
                ),
                "insufficient_history_count_by_decision": dict(
                    coverage.report.get(
                        "insufficient_history_count_by_decision", {}
                    )
                ),
                "ineligible_no_initial_price_count": int(
                    coverage.report.get("ineligible_no_initial_price_count", 0)
                ),
                "unexplained_market_data_gap_count": int(
                    coverage.report.get("unexplained_market_data_gap_count", 0)
                ),
                "datasets": datasets,
                "data_status": "READY" if complete else "BLOCKED_DATA",
                "remaining_blockers": blockers if blockers else ([] if complete else ["history_tasks_incomplete"]),
                "locked_test_status": dict(LOCKED_TEST_STATUS),
                "locked_test_consumed": False,
                "safety": _manifest_safety(plan),
            },
            "manifest_sha256",
        )
    )


def publish_history_artifacts(
    output_root: Path | str,
    coverage: HistoryCoverageResult,
    manifest: Mapping[str, Any],
    *,
    token: str | None = None,
) -> tuple[Path, Path]:
    root = Path(output_root)
    coverage_path = root / "history_coverage_report.json"
    manifest_path = root / "history_manifest.json"
    _publish_or_verify_identical(coverage_path, coverage.report, token=token)
    _publish_or_verify_identical(manifest_path, manifest, token=token)
    return coverage_path, manifest_path


def build_total_return_panel(
    trading_dates: Iterable[date | str],
    instrument_ids: Sequence[str],
    daily_rows: Sequence[Mapping[str, Any]],
    adj_factor_rows: Sequence[Mapping[str, Any]],
    suspension_rows: Sequence[Mapping[str, Any]],
    *,
    coverage_start: date | str | None = None,
    coverage_end: date | str | None = None,
) -> list[dict[str, Any]]:
    """Build causal signal values from ``raw_close * as-of adj_factor``.

    A usable positive-turnover daily bar is the price fact even when a
    ``suspend_d`` event claims a full-session suspension.  After the first
    causal adjusted price, any unavailable stock-day carries that last value
    for valuation while remaining explicitly unavailable to the selector.
    """

    parsed_dates = tuple(
        item if isinstance(item, date) else _parse_date(item, "trading_date")
        for item in trading_dates
    )
    if not parsed_dates or tuple(sorted(set(parsed_dates))) != parsed_dates:
        raise AlphaFeasibilityDataError("trading_dates_not_strictly_ordered")
    start = (
        parsed_dates[0]
        if coverage_start is None
        else coverage_start
        if isinstance(coverage_start, date)
        else _parse_date(coverage_start, "coverage_start")
    )
    end = (
        parsed_dates[-1]
        if coverage_end is None
        else coverage_end
        if isinstance(coverage_end, date)
        else _parse_date(coverage_end, "coverage_end")
    )
    if start > end or end > ABSOLUTE_CUTOFF:
        raise AlphaFeasibilityDataError("panel_date_boundary_invalid")

    instruments = tuple(sorted(instrument_ids))
    if (
        not instruments
        or len(instruments) != len(set(instruments))
        or any(_PIT_COMPONENT_CODE.fullmatch(code) is None for code in instruments)
    ):
        raise AlphaFeasibilityDataError("invalid_panel_instrument_union")
    instrument_set = set(instruments)
    daily = _unique_rows("daily", daily_rows)
    suspensions = _aggregate_suspension_events(suspension_rows)
    adj_index = _unique_rows("adj_factor", adj_factor_rows)
    if not {code for code, _trade_date in daily}.issubset(instrument_set):
        raise AlphaFeasibilityDataError("index_weight_daily_code_mismatch")
    if any(code not in instrument_set for code, _trade_date in suspensions):
        raise AlphaFeasibilityDataError("suspension_contains_non_panel_instrument")
    for endpoint_rows, date_field in (
        (daily_rows, "trade_date"),
        (adj_factor_rows, "trade_date"),
        (suspension_rows, "trade_date"),
    ):
        if any(_parse_date(row[date_field], date_field) > ABSOLUTE_CUTOFF for row in endpoint_rows):
            raise AlphaFeasibilityDataError("post_cutoff_panel_input")
    factors: dict[str, list[tuple[date, Decimal, str]]] = {
        code: [] for code in instruments
    }
    for (code, trade_date), row in adj_index.items():
        if code not in instrument_set:
            raise AlphaFeasibilityDataError("adj_factor_contains_non_panel_instrument")
        factor, text = _decimal(row["adj_factor"], "adj_factor", minimum=Decimal("0"))
        if factor <= 0:
            raise AlphaFeasibilityDataError("nonpositive_adj_factor")
        factors[code].append((_parse_date(trade_date, "adj_trade_date"), factor, text))
    for values in factors.values():
        values.sort(key=lambda item: item[0])

    panel: list[dict[str, Any]] = []
    for code in instruments:
        factor_values = factors[code]
        real_dates = []
        for instrument, trade_date in daily:
            if instrument != code or not _usable_daily_bar(daily[(instrument, trade_date)]):
                continue
            parsed_trade_date = _parse_date(trade_date, "daily_trade_date")
            if factor_values and factor_values[0][0] <= parsed_trade_date:
                real_dates.append(parsed_trade_date)
        if not real_dates:
            # The engine emits an insufficient-history conclusion for every
            # applicable PIT decision without inventing a price.
            continue
        first_real = max(start, min(real_dates))
        factor_cursor = 0
        latest_factor: tuple[date, Decimal, str] | None = None
        previous_economic_value: Decimal | None = None
        for trading_date in parsed_dates:
            if trading_date < first_real or trading_date > end:
                continue
            while (
                factor_cursor < len(factor_values)
                and factor_values[factor_cursor][0] <= trading_date
            ):
                latest_factor = factor_values[factor_cursor]
                factor_cursor += 1
            key = (code, _compact(trading_date))
            bar = daily.get(key)
            suspension = suspensions.get(key)
            usable_bar = _usable_daily_bar(bar) and latest_factor is not None
            if not usable_bar:
                if previous_economic_value is None:
                    continue
                raw_values: dict[str, str | None] = {
                    "open": None,
                    "high": None,
                    "low": None,
                    "close": None,
                }
                if bar is not None:
                    for field in raw_values:
                        _number, raw_values[field] = _decimal(
                            bar[field], field, minimum=Decimal("0")
                        )
                economic_value = previous_economic_value
                adjusted_high = previous_economic_value
                row = {
                    "trading_date": _iso(trading_date),
                    "trade_date": _compact(trading_date),
                    "ts_code": code,
                    "instrument_id": code,
                    "raw_open": raw_values["open"],
                    "raw_high": raw_values["high"],
                    "raw_low": raw_values["low"],
                    "raw_close": raw_values["close"],
                    "adj_factor": latest_factor[2] if latest_factor is not None else None,
                    "adj_factor_asof_date": (
                        _iso(latest_factor[0]) if latest_factor is not None else None
                    ),
                    "close": str(economic_value),
                    "high": str(adjusted_high),
                    "open": str(economic_value),
                    "adjusted_value": str(economic_value),
                    "daily_total_return": "0",
                    "is_suspended_carry": _is_full_session_suspension(suspension),
                    "is_unavailable_no_daily_bar": True,
                    "suspend_type": (
                        "S" if _is_full_session_suspension(suspension) else None
                    ),
                }
            else:
                assert bar is not None and latest_factor is not None
                raw_close, raw_close_text = _decimal(
                    bar["close"], "close", minimum=Decimal("0")
                )
                raw_high, raw_high_text = _decimal(
                    bar["high"], "high", minimum=Decimal("0")
                )
                raw_open, raw_open_text = _decimal(
                    bar["open"], "open", minimum=Decimal("0")
                )
                raw_low, raw_low_text = _decimal(
                    bar["low"], "low", minimum=Decimal("0")
                )
                economic_value = raw_close * latest_factor[1]
                adjusted_high = raw_high * latest_factor[1]
                adjusted_open = raw_open * latest_factor[1]
                daily_return = (
                    None
                    if previous_economic_value is None
                    else economic_value / previous_economic_value - Decimal("1")
                )
                row = {
                    "trading_date": _iso(trading_date),
                    "trade_date": _compact(trading_date),
                    "ts_code": code,
                    "instrument_id": code,
                    "raw_open": raw_open_text,
                    "raw_high": raw_high_text,
                    "raw_low": raw_low_text,
                    "raw_close": raw_close_text,
                    "adj_factor": latest_factor[2],
                    "adj_factor_asof_date": _iso(latest_factor[0]),
                    "close": str(economic_value),
                    "high": str(adjusted_high),
                    "open": str(adjusted_open),
                    "adjusted_value": str(economic_value),
                    "daily_total_return": None if daily_return is None else str(daily_return),
                    "is_suspended_carry": False,
                    "is_unavailable_no_daily_bar": False,
                    "suspend_type": None,
                }
            panel.append(row)
            previous_economic_value = economic_value
    panel.sort(key=lambda row: (row["trading_date"], row["ts_code"]))
    return panel


def _load_json_object(path: Path, code: str) -> Mapping[str, Any]:
    try:
        value = strict_json_loads(path.read_bytes(), label="artifact")
    except OSError as exc:
        raise AlphaFeasibilityDataError(code) from exc
    if not isinstance(value, Mapping):
        raise AlphaFeasibilityDataError(code)
    return value


def _load_existing_pit_result(
    output_root: Path, plan: CollectionPlan
) -> PitMembershipResult | None:
    report_path = output_root / "pit_membership_coverage_report.json"
    manifest_path = output_root / "pit_membership_manifest.json"
    if not report_path.exists() and not manifest_path.exists():
        return None
    if not report_path.is_file() or not manifest_path.is_file():
        raise AlphaFeasibilityDataError("incomplete_pit_artifact_pair", stage="pit")
    report = _load_json_object(report_path, "pit_report_unreadable")
    manifest = _load_json_object(manifest_path, "pit_manifest_unreadable")
    unsigned_report = dict(report)
    report_hash = unsigned_report.pop("report_sha256", None)
    unsigned_manifest = dict(manifest)
    manifest_hash = unsigned_manifest.pop("manifest_sha256", None)
    expected_report_schema, expected_manifest_schema = _pit_schema_versions(plan)
    if (
        report.get("schema_version") != expected_report_schema
        or manifest.get("schema_version") != expected_manifest_schema
        or report_hash != canonical_sha256(unsigned_report)
        or manifest_hash != canonical_sha256(unsigned_manifest)
        or report.get("locked_test_status") != dict(LOCKED_TEST_STATUS)
        or manifest.get("locked_test_status") != dict(LOCKED_TEST_STATUS)
        or report.get("locked_test_consumed") is not False
        or manifest.get("locked_test_consumed") is not False
    ):
        raise AlphaFeasibilityDataError("pit_artifact_verification_failed", stage="pit")
    try:
        validate_json_schema(report, PIT_REPORT_SCHEMA_PATHS[expected_report_schema])
        validate_json_schema(
            manifest, PIT_MANIFEST_SCHEMA_PATHS[expected_manifest_schema]
        )
    except SchemaValidationError as exc:
        raise AlphaFeasibilityDataError("pit_artifact_schema_invalid", stage="pit") from exc
    passed = (
        report.get("stage_status") == "PIT_MEMBERSHIP_READY"
        and manifest.get("stage_status") == "PIT_MEMBERSHIP_READY"
    )
    snapshots = manifest.get("snapshots")
    if not isinstance(snapshots, list):
        raise AlphaFeasibilityDataError("pit_snapshot_manifest_invalid", stage="pit")
    union = tuple(
        sorted(
            {
                member.get("instrument_id")
                for item in snapshots
                for member in item.get("members", [])
                if isinstance(member, Mapping)
            }
        )
    )
    if passed:
        _validate_pit_snapshot_month_coverage(plan, snapshots)
        if (
            len(union) != manifest.get("union_instrument_count")
            or list(union) != manifest.get("union_instrument_ids")
        ):
            raise AlphaFeasibilityDataError("pit_union_manifest_invalid", stage="pit")
    if passed:
        generated_text = report.get("generated_at")
        if type(generated_text) is not str:
            raise AlphaFeasibilityDataError("pit_generated_at_invalid", stage="pit")
        try:
            generated = datetime.fromisoformat(generated_text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AlphaFeasibilityDataError("pit_generated_at_invalid", stage="pit") from exc
        if generated.tzinfo is None:
            raise AlphaFeasibilityDataError("pit_generated_at_invalid", stage="pit")
        store = CreateOnlyTaskStore(output_root)
        replayed = _completed_results(store, plan.pit_tasks)
        if len(replayed) != 73:
            raise AlphaFeasibilityDataError("pit_task_evidence_incomplete", stage="pit")
        rebuilt = build_pit_membership_artifacts(
            plan,
            replayed,
            generated_at=generated,
        )
        if (
            dict(rebuilt.coverage_report) != dict(report)
            or dict(rebuilt.manifest) != dict(manifest)
            or rebuilt.union_instruments != union
        ):
            raise AlphaFeasibilityDataError("pit_artifact_replay_mismatch", stage="pit")
    return PitMembershipResult(
        coverage_report=MappingProxyType(dict(report)),
        manifest=MappingProxyType(dict(manifest)),
        union_instruments=union if passed else (),
        passed=passed,
    )


def _completed_results(
    store: CreateOnlyTaskStore, tasks: Sequence[CollectionTask]
) -> dict[str, TaskExecutionResult]:
    completed: dict[str, TaskExecutionResult] = {}
    for task in tasks:
        if store.is_complete(task):
            completed[task.task_id] = store._load_response(task)
    return completed


def _backfill_lineage(
    plan: CollectionPlan,
    *,
    pit_result: PitMembershipResult | None = None,
    history_manifest: Mapping[str, Any] | None = None,
) -> dict[str, str | None]:
    pit_hash = pit_result.manifest.get("manifest_sha256") if pit_result is not None else None
    history_hash = history_manifest.get("manifest_sha256") if history_manifest is not None else None
    for value, code in (
        (plan.plan_sha256, "invalid_collection_plan_sha256"),
        (plan.config_sha256, "invalid_experiment_config_sha256"),
        (pit_hash, "invalid_pit_membership_manifest_sha256"),
        (history_hash, "invalid_history_manifest_sha256"),
    ):
        if value is not None and (type(value) is not str or _SHA256.fullmatch(value) is None):
            raise AlphaFeasibilityDataError(code)
    return {
        "collection_plan_sha256": plan.plan_sha256,
        "experiment_config_sha256": plan.config_sha256,
        "pit_membership_manifest_sha256": pit_hash,
        "history_manifest_sha256": history_hash,
    }


def _public_data_failure_code(exc: AlphaFeasibilityDataError) -> str:
    if exc.code == "data_payload_invalid":
        category = exc.diagnostic.get("data_failure_category")
        if type(category) is str and re.fullmatch(r"[a-z0-9_]{3,96}", category):
            return category
    return exc.code


def _first_index_weight_summary(
    plan: CollectionPlan,
    store: CreateOnlyTaskStore,
    *,
    diagnostic: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    task = plan.pit_tasks[0]
    result: TaskExecutionResult | None = None
    if store.is_complete(task):
        result = store._load_response(task)
    if result is not None:
        return {
            "observed_data_fields": list(result.observed_data_fields),
            "required_data_fields": list(result.required_data_fields or task.fields),
            "missing_required_data_fields": list(
                result.missing_required_data_fields
            ),
            "extra_data_fields": list(result.extra_data_fields),
            "field_order_matches_canonical": result.field_order_matches_canonical,
            "data_row_count": result.data_row_count,
            "provider_payload_sha256": result.provider_payload_sha256,
            "normalized_content_sha256": result.normalized_content_sha256,
        }
    safe = dict(diagnostic or {})
    return {
        "observed_data_fields": list(safe.get("observed_data_fields", [])),
        "required_data_fields": list(
            safe.get("required_data_fields", list(task.fields))
        ),
        "missing_required_data_fields": list(
            safe.get("missing_required_data_fields", list(task.fields))
        ),
        "extra_data_fields": list(safe.get("extra_data_fields", [])),
        "field_order_matches_canonical": bool(
            safe.get("field_order_matches_canonical", False)
        ),
        "data_row_count": int(safe.get("data_row_count", 0)),
        "provider_payload_sha256": safe.get("provider_payload_sha256"),
        "normalized_content_sha256": safe.get("normalized_content_sha256"),
    }


def _blocked_summary(
    plan: CollectionPlan,
    output_root: Path,
    *,
    stage_status: str,
    blocker: str,
    pit_result: PitMembershipResult | None = None,
    coverage: HistoryCoverageResult | None = None,
    history_manifest: Mapping[str, Any] | None = None,
    expected_tasks: Sequence[CollectionTask] | None = None,
    first_index_weight: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    artifact_generated_at = (
        coverage.report.get("generated_at")
        if coverage is not None
        else pit_result.coverage_report.get("generated_at")
        if pit_result is not None
        else None
    )
    return {
        "schema_version": "tushare-alpha-feasibility-backfill-result.v1",
        "experiment_id": plan.config["experiment_id"],
        "stage_status": stage_status,
        "terminal_status": (
            "BLOCKED_ADAPTER_PROTOCOL"
            if stage_status == "BLOCKED_ADAPTER_PROTOCOL"
            else "BLOCKED_DATA"
        ),
        "generated_at": artifact_generated_at,
        **_backfill_lineage(
            plan,
            pit_result=pit_result,
            history_manifest=history_manifest,
        ),
        "actual_tushare_request_count_by_endpoint": actual_tushare_request_count_by_endpoint(
            output_root,
            expected_tasks,
            plan_sha256=plan.plan_sha256,
        ),
        "first_index_weight": dict(
            first_index_weight
            or _first_index_weight_summary(plan, CreateOnlyTaskStore(output_root))
        ),
        "stock_basic_status": STOCK_BASIC_STATUS,
        "stock_basic_request_count": STOCK_BASIC_REQUEST_COUNT,
        "security_master_pit_status": SECURITY_MASTER_PIT_STATUS,
        "request_count_semantics": _request_count_semantics(plan),
        "coverage_start": plan.config["dates"]["signal_warmup_start"],
        "coverage_end": plan.config["dates"]["validation_end"],
        "pit_months_expected": 73,
        "pit_months_observed": (
            pit_result.coverage_report.get("pit_months_observed", 0) if pit_result else 0
        ),
        "union_instrument_count": len(pit_result.union_instruments) if pit_result else 0,
        "valid_candidate_count_by_decision": (
            dict(coverage.report.get("valid_candidate_count_by_decision", {}))
            if coverage
            else {}
        ),
        "insufficient_history_count_by_decision": (
            dict(
                coverage.report.get("insufficient_history_count_by_decision", {})
            )
            if coverage
            else {}
        ),
        "ineligible_no_initial_price_count": (
            int(coverage.report.get("ineligible_no_initial_price_count", 0))
            if coverage
            else 0
        ),
        "unexplained_market_data_gap_count": (
            int(coverage.report.get("unexplained_market_data_gap_count", 0))
            if coverage
            else 0
        ),
        "daily_coverage_status": (
            coverage.report.get("daily_coverage_status", "BLOCKED_DATA")
            if coverage
            else "BLOCKED_DATA"
        ),
        "adj_factor_coverage_status": (
            coverage.report.get("adj_factor_coverage_status", "BLOCKED_DATA")
            if coverage
            else "BLOCKED_DATA"
        ),
        "suspension_coverage_status": (
            coverage.report.get("suspension_coverage_status", "BLOCKED_DATA")
            if coverage
            else "BLOCKED_DATA"
        ),
        "benchmark_coverage_status": (
            coverage.report.get("benchmark_coverage_status", "BLOCKED_DATA")
            if coverage
            else "BLOCKED_DATA"
        ),
        "remaining_blockers": [blocker],
        "locked_test_status": dict(LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
        "execution_realism": "INCOMPLETE",
        "trade_eligibility": False,
    }


def _blocked_history_coverage(
    plan: CollectionPlan,
    union_count: int,
    blocker: str,
    generated_at: datetime,
) -> HistoryCoverageResult:
    report = {
        "schema_version": HISTORY_COVERAGE_SCHEMA_VERSION,
        "experiment_id": plan.config["experiment_id"],
        "generated_at": _generated_at(generated_at),
        "stage_status": "BLOCKED_DATA",
        "coverage_start": plan.config["dates"]["signal_warmup_start"],
        "coverage_end": plan.config["dates"]["validation_end"],
        "union_instrument_count": union_count,
        "open_session_count": 0,
        "daily_coverage_status": "BLOCKED_DATA",
        "adj_factor_coverage_status": "BLOCKED_DATA",
        "suspension_coverage_status": "BLOCKED_DATA",
        "benchmark_coverage_status": "BLOCKED_DATA",
        "stock_basic_status": STOCK_BASIC_STATUS,
        "stock_basic_request_count": STOCK_BASIC_REQUEST_COUNT,
        "security_master_pit_status": SECURITY_MASTER_PIT_STATUS,
        "history_eligibility_status": INSUFFICIENT_HISTORY_STATUS,
        "valid_candidate_count_by_decision": {},
        "insufficient_history_count_by_decision": {},
        "ineligible_no_initial_price_count": 0,
        "unexplained_market_data_gap_count": 0,
        "same_day_suspension_explained_missing_daily_count": 0,
        "non_suspension_missing_daily_count": 0,
        "missing_causal_adj_factor_count": 0,
        "unavailable_stock_day_count": 0,
        "suspension_daily_bar_conflict_count": 0,
        "off_calendar_adj_factor_count": 0,
        "terminal_session_next_session": None,
        "blockers": [{"reason": blocker}],
        "locked_test_status": dict(LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
    }
    return HistoryCoverageResult(report=MappingProxyType(report), passed=False, trading_dates=())


def _validate_parent_reuse_continuation_child(
    plan: CollectionPlan,
    child_root: Path,
) -> None:
    if (
        not _p15_enabled(plan)
        or child_root.is_symlink()
        or not child_root.is_dir()
        or not _path_is_within_data_tmp(child_root)
    ):
        raise AlphaFeasibilityDataError("continuation_child_root_invalid")
    if len(plan.pit_tasks) != 73:
        raise AlphaFeasibilityDataError("continuation_plan_invalid")
    store = CreateOnlyTaskStore(child_root)
    prefix = plan.pit_tasks[:19]
    first_unfinished = plan.pit_tasks[19]
    tail = plan.pit_tasks[20:]
    parent_bindings: set[str] = set()
    for task in prefix:
        if (
            store.started_path(task).exists()
            or store.quarantine_path(task).exists()
            or not store.import_path(task).is_file()
            or not store.response_path(task).is_file()
            or not store.raw_path(task).is_file()
        ):
            raise AlphaFeasibilityDataError(
                "continuation_reuse_prefix_incomplete"
            )
        result = store._load_response(task)
        imported = store._load_import(task)
        if (
            result.request_origin != "offline_parent_run_reuse"
            or result.network_request_count != 0
            or imported.get("schema_version")
            != PARENT_REUSE_IMPORT_SCHEMA_VERSION
        ):
            raise AlphaFeasibilityDataError(
                "continuation_reuse_prefix_invalid"
            )
        parent_bindings.add(canonical_sha256(imported["parent_binding"]))
    if len(parent_bindings) != 1:
        raise AlphaFeasibilityDataError("continuation_parent_binding_mismatch")

    if (
        not store.started_path(first_unfinished).is_file()
        or store.import_path(first_unfinished).exists()
        or store.response_path(first_unfinished).exists()
        or store.raw_path(first_unfinished).exists()
        or store.quarantine_path(first_unfinished).exists()
        or (child_root / "business_errors" / first_unfinished.task_id).exists()
        or (child_root / "raw_errors" / first_unfinished.task_id).exists()
    ):
        raise AlphaFeasibilityDataError(
            "continuation_first_unfinished_invalid"
        )
    started = store._load_started(first_unfinished)
    attempts = store._load_attempts(first_unfinished)
    if (
        started.get("schema_version") != RECOVERABLE_STARTED_SCHEMA_VERSION
        or len(attempts) != 1
        or attempts[0] != _attempt_payload(first_unfinished, 1)
    ):
        raise AlphaFeasibilityDataError(
            "continuation_parent_attempt_invalid"
        )

    for task in tail:
        artifacts = (
            store.started_path(task),
            store.import_path(task),
            store.response_path(task),
            store.raw_path(task),
            store.quarantine_path(task),
            child_root / "attempts" / task.task_id,
            child_root / "business_errors" / task.task_id,
            child_root / "raw_errors" / task.task_id,
        )
        if any(path.exists() for path in artifacts):
            raise AlphaFeasibilityDataError("continuation_tail_not_pristine")

    expected_task_files = {
        *(f"{task.task_id}.import.json" for task in prefix),
        *(f"{task.task_id}.response.json" for task in prefix),
        f"{first_unfinished.task_id}.started.json",
    }
    task_directory = child_root / "tasks"
    observed_task_files = (
        {path.name for path in task_directory.iterdir()}
        if task_directory.is_dir() and not task_directory.is_symlink()
        else set()
    )
    raw_directory = child_root / "raw"
    expected_raw_files = {f"{task.task_id}.json" for task in prefix}
    observed_raw_files = (
        {path.name for path in raw_directory.iterdir()}
        if raw_directory.is_dir() and not raw_directory.is_symlink()
        else set()
    )
    attempt_root = child_root / "attempts"
    observed_attempt_task_ids = (
        {path.name for path in attempt_root.iterdir()}
        if attempt_root.is_dir() and not attempt_root.is_symlink()
        else set()
    )
    if (
        observed_task_files != expected_task_files
        or observed_raw_files != expected_raw_files
        or observed_attempt_task_ids != {first_unfinished.task_id}
        or any(
            (child_root / name).exists()
            for name in (
                "pit_membership_coverage_report.json",
                "pit_membership_manifest.json",
                "history_coverage_report.json",
                "history_manifest.json",
            )
        )
    ):
        raise AlphaFeasibilityDataError("continuation_child_artifact_set_invalid")
    counts = actual_tushare_request_count_by_endpoint(
        child_root, plan.pit_tasks, plan_sha256=plan.plan_sha256
    )
    if counts != {
        "trade_cal": 0,
        "index_weight": 1,
        "daily": 0,
        "adj_factor": 0,
        "index_daily": 0,
        "suspend_d": 0,
    }:
        raise AlphaFeasibilityDataError("continuation_parent_request_count_invalid")


def validate_parent_reuse_continuation_child(
    config_path: Path | str,
    child_root: Path | str,
    *,
    repository_root: Path | str = REPOSITORY_ROOT,
) -> CollectionPlan:
    """Offline preflight for the exact parent-reuse continuation boundary."""

    plan = load_config_and_build_plan(config_path, repository_root=repository_root)
    _validate_parent_reuse_continuation_child(plan, Path(child_root))
    return plan


def _arm_parent_reuse_continuation_network_process(
    child_root: Path | str,
    marker_sha256: str,
) -> None:
    """Arm one process-local continuation authorization after marker creation."""

    root = Path(child_root)
    if (
        root.is_symlink()
        or type(marker_sha256) is not str
        or _SHA256.fullmatch(marker_sha256) is None
    ):
        raise AlphaFeasibilityDataError(
            "continuation_network_process_authorization_invalid"
        )
    key = (str(root.resolve(strict=False)), marker_sha256)
    with _CONTINUATION_NETWORK_PROCESS_AUTHORIZATIONS_LOCK:
        if key in _CONTINUATION_NETWORK_PROCESS_AUTHORIZATIONS:
            raise AlphaFeasibilityDataError(
                "continuation_network_process_authorization_already_armed"
            )
        _CONTINUATION_NETWORK_PROCESS_AUTHORIZATIONS.add(key)


def _load_continuation_control_artifact(
    root: Path,
    filename: str,
    *,
    repository_root: Path,
) -> Mapping[str, Any]:
    schema_name, hash_field = CONTINUATION_CONTROL_ARTIFACTS[filename]
    path = root / filename
    if path.is_symlink() or not path.is_file():
        code = (
            "continuation_network_process_marker_missing"
            if filename == "p1_5_continuation_network_process.json"
            and not path.exists()
            and not path.is_symlink()
            else "continuation_network_process_binding_invalid"
        )
        raise AlphaFeasibilityDataError(code)
    try:
        raw = path.read_bytes()
        value = strict_json_loads(raw, label="continuation_control_artifact")
        if not isinstance(value, Mapping) or raw != canonical_json_bytes(value):
            raise AlphaFeasibilityDataError(
                "continuation_network_process_binding_invalid"
            )
        validate_json_schema(value, repository_root / "schemas" / schema_name)
    except (
        OSError,
        SchemaValidationError,
        AlphaFeasibilityDataError,
    ) as exc:
        if (
            isinstance(exc, AlphaFeasibilityDataError)
            and exc.code == "continuation_network_process_binding_invalid"
        ):
            raise
        raise AlphaFeasibilityDataError(
            "continuation_network_process_binding_invalid"
        ) from exc
    unsigned = dict(value)
    declared = unsigned.pop(hash_field, None)
    if declared != canonical_sha256(unsigned):
        raise AlphaFeasibilityDataError(
            "continuation_network_process_binding_invalid"
        )
    return value


def _consume_parent_reuse_continuation_network_process(
    plan: CollectionPlan,
    child_root: Path,
    *,
    repository_root: Path,
) -> None:
    """Validate marker lineage and consume its same-process one-shot permit."""

    reuse = _load_continuation_control_artifact(
        child_root,
        "p1_5_continuation_reuse_manifest.json",
        repository_root=repository_root,
    )
    claim = _load_continuation_control_artifact(
        child_root,
        "p1_5_continuation_claim.json",
        repository_root=repository_root,
    )
    stage = _load_continuation_control_artifact(
        child_root,
        "p1_5_continuation_parent_reuse_stage.json",
        repository_root=repository_root,
    )
    marker = _load_continuation_control_artifact(
        child_root,
        "p1_5_continuation_network_process.json",
        repository_root=repository_root,
    )
    if len(plan.pit_tasks) <= 19:
        raise AlphaFeasibilityDataError(
            "continuation_network_process_binding_invalid"
        )
    first_unfinished_task_id = plan.pit_tasks[19].task_id
    locked = dict(LOCKED_TEST_STATUS)
    if (
        reuse.get("collection_plan_sha256") != plan.plan_sha256
        or claim.get("collection_plan_sha256") != plan.plan_sha256
        or stage.get("collection_plan_sha256") != plan.plan_sha256
        or claim.get("reuse_manifest_sha256") != reuse.get("manifest_sha256")
        or claim.get("parent") != reuse.get("parent")
        or claim.get("successful_prefix_count") != 19
        or claim.get("first_unfinished_task_id") != first_unfinished_task_id
        or stage.get("continuation_run_id") != claim.get("continuation_run_id")
        or stage.get("continuation_claim_sha256") != claim.get("claim_sha256")
        or stage.get("reuse_manifest_sha256") != reuse.get("manifest_sha256")
        or stage.get("parent") != claim.get("parent")
        or stage.get("child_completed_prefix_count") != 19
        or stage.get("first_unfinished_task_id") != first_unfinished_task_id
        or stage.get("next_task_ordinal") != 20
        or marker.get("continuation_run_id") != claim.get("continuation_run_id")
        or marker.get("continuation_claim_sha256") != claim.get("claim_sha256")
        or marker.get("reuse_manifest_sha256") != reuse.get("manifest_sha256")
        or marker.get("parent_reuse_stage_sha256") != stage.get("stage_sha256")
        or marker.get("completed_request_fingerprint_count_at_start") != 19
        or marker.get("first_unfinished_task_id") != first_unfinished_task_id
        or marker.get("next_attempt_number") != 2
        or marker.get("network_process_count") != 1
        or any(
            artifact.get("locked_test_status") != locked
            or artifact.get("locked_test_consumed") is not False
            for artifact in (reuse, claim, stage, marker)
        )
    ):
        raise AlphaFeasibilityDataError(
            "continuation_network_process_binding_invalid"
        )
    key = (str(child_root.resolve(strict=False)), marker["marker_sha256"])
    with _CONTINUATION_NETWORK_PROCESS_AUTHORIZATIONS_LOCK:
        if key not in _CONTINUATION_NETWORK_PROCESS_AUTHORIZATIONS:
            raise AlphaFeasibilityDataError(
                "continuation_network_process_authorization_missing"
            )
        _CONTINUATION_NETWORK_PROCESS_AUTHORIZATIONS.remove(key)


def run_parent_reuse_continuation_backfill(
    config_path: Path | str,
    child_root: Path | str,
    token: str,
    transport: TushareTransport | None = None,
    generated_at: datetime | None = None,
    *,
    repository_root: Path | str = REPOSITORY_ROOT,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    clock: Callable[[], datetime] = _utc_now,
) -> dict[str, Any]:
    """Continue a sealed P1.5 child with 12-second spacing and one safe retry.

    The return value is the ordinary ``run_backfill`` result mapping.  A PIT
    ``DATA_UNAVAILABLE`` business response is surfaced as
    ``BLOCKED_PIT_SOURCE_COVERAGE``; other status semantics are unchanged.
    """

    return run_backfill(
        config_path,
        Path(child_root),
        token,
        transport=transport,
        generated_at=generated_at,
        repository_root=repository_root,
        sleeper=sleeper,
        monotonic=monotonic,
        _continuation_execution=_CONTINUATION_EXECUTION_CAPABILITY,
        _clock=clock,
    )


def run_backfill(
    config_path: Path | str,
    output_root: Path | str,
    token: str,
    transport: TushareTransport | None = None,
    generated_at: datetime | None = None,
    *,
    repository_root: Path | str = REPOSITORY_ROOT,
    adjustment_evidence: Mapping[str, Any] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    _continuation_execution: object = False,
    _clock: Callable[[], datetime] = _utc_now,
) -> dict[str, Any]:
    """Run PIT first and plan stock history only after all 73 months pass.

    This is a thin data-stage orchestrator.  It never runs the Alpha engine,
    Development, Validation, Paper, broker, account, or Locked Test paths.
    """

    # The full config, source allowlist, frozen code hashes, 73 PIT requests,
    # and all hard date bounds are proven before the credential is inspected.
    plan = load_config_and_build_plan(config_path, repository_root=repository_root)
    if not (
        _continuation_execution is False
        or _continuation_execution is _CONTINUATION_EXECUTION_CAPABILITY
    ) or not callable(_clock):
        raise AlphaFeasibilityDataError("invalid_continuation_execution_setting")
    continuation_execution = (
        _continuation_execution is _CONTINUATION_EXECUTION_CAPABILITY
    )
    if adjustment_evidence is not None:
        raise AlphaFeasibilityDataError(
            "controlled_adjustment_evidence_not_supported", stage="pit"
        )
    root = Path(output_root)
    if continuation_execution:
        # The internal continuation flag must not provide a bypass around the
        # public entry point's exact-19 reuse / attempt-1 / pristine-tail gate.
        _validate_parent_reuse_continuation_child(plan, root)
        _consume_parent_reuse_continuation_network_process(
            plan,
            root,
            repository_root=Path(repository_root),
        )
    safe_token = _validate_token(token)
    store = CreateOnlyTaskStore(root)
    source = plan.config["source"]
    recover_attempts, maximum_attempts = _attempt_settings(plan)
    persist_full_raw_transport = _p15_enabled(plan)
    active_transport = transport or HttpsTushareTransport()
    timestamp = generated_at or datetime.now(timezone.utc)
    interval, _ = _decimal(
        source["minimum_request_interval_seconds"],
        "minimum_request_interval_seconds",
        minimum=Decimal("0"),
    )
    if continuation_execution:
        interval = MINIMUM_REAL_TRANSPORT_INTERVAL_SECONDS

    pit_network_performed = False
    pit_result = _load_existing_pit_result(root, plan)
    if pit_result is None:
        try:
            def execute_pit_subset(
                tasks: Sequence[CollectionTask],
            ) -> tuple[TaskExecutionResult, ...]:
                return execute_tasks(
                    tasks,
                    store=store,
                    token=safe_token,
                    transport=active_transport,
                    timeout_seconds=source["request_timeout_seconds"],
                    maximum_response_bytes=source["maximum_response_bytes"],
                    recover_interrupted_attempts=recover_attempts,
                    maximum_attempts_per_fingerprint=maximum_attempts,
                    persist_full_raw_transport=persist_full_raw_transport,
                    minimum_request_interval_seconds=interval,
                    sleeper=sleeper,
                    monotonic=monotonic,
                    business_error_retry_once=continuation_execution,
                    clock=_clock,
                    terminalize_transport_interruptions=continuation_execution,
                )

            # A fresh run probes task 1.  A parent-reuse continuation first
            # replays the sealed 19-task prefix, then makes task 20 the sole
            # network probe.  Its 2019-07 snapshot must pass before task 21 is
            # even eligible to create an attempt claim.
            probe_index = 19 if continuation_execution else 0
            prefix_pit_executions = execute_pit_subset(
                plan.pit_tasks[:probe_index]
            )
            probe_pit_executions = execute_pit_subset(
                plan.pit_tasks[probe_index : probe_index + 1]
            )
            probed_pit_executions = (
                *prefix_pit_executions,
                *probe_pit_executions,
            )
            first_pit_probe = build_pit_membership_artifacts(
                plan,
                {result.task.task_id: result for result in probed_pit_executions},
                adjustment_evidence=adjustment_evidence,
                generated_at=timestamp,
            )
            first_month_check = first_pit_probe.coverage_report["monthly_checks"][
                probe_index
            ]
            if first_month_check["status"] != "complete":
                issues = first_month_check.get("issues", [])
                blocker = (
                    issues[0]
                    if isinstance(issues, list)
                    and issues
                    and type(issues[0]) is str
                    else "first_pit_month_invalid"
                )
                raise AlphaFeasibilityDataError(blocker, stage="pit")

            # Splitting the first task into its own semantic/PIT gate must not
            # weaken the configured inter-request spacing in the same process.
            if (
                len(plan.pit_tasks) > probe_index + 1
                and not probe_pit_executions[0].replayed
                and interval > 0
            ):
                sleeper(float(interval))
            remaining_pit_executions = execute_pit_subset(
                plan.pit_tasks[probe_index + 1 :]
            )
            pit_executions = (*probed_pit_executions, *remaining_pit_executions)
            pit_network_performed = any(
                not result.replayed for result in pit_executions
            )
            pit_results = {result.task.task_id: result for result in pit_executions}
        except AlphaFeasibilityDataError as exc:
            if (
                recover_attempts
                and exc.code in RETRYABLE_ATTEMPT_FAILURES
                and not continuation_execution
            ):
                # A normal transport interruption is resumable evidence, not
                # a sealed PIT verdict.  Leave the attempt journal in place.
                raise
            pit_results = _completed_results(store, plan.pit_tasks)
            public_blocker = _public_data_failure_code(exc)
            adapter_protocol_blocked = (
                exc.code in ADAPTER_PROTOCOL_FAILURES
                or public_blocker in ADAPTER_PROTOCOL_FAILURES
            )
            pit_result = build_pit_membership_artifacts(
                plan,
                pit_results,
                adjustment_evidence=adjustment_evidence,
                generated_at=timestamp,
                blocked_terminal_status=(
                    "BLOCKED_ADAPTER_PROTOCOL"
                    if adapter_protocol_blocked
                    else "BLOCKED_DATA"
                ),
            )
            publish_pit_membership_artifacts(root, pit_result, token=safe_token)
            return _blocked_summary(
                plan,
                root,
                stage_status=(
                    "BLOCKED_ADAPTER_PROTOCOL"
                    if adapter_protocol_blocked
                    else "BLOCKED_PIT_SOURCE_COVERAGE"
                    if continuation_execution
                    and public_blocker == "upstream_data_unavailable_error"
                    else "BLOCKED_PIT_MEMBERSHIP"
                ),
                blocker=public_blocker,
                pit_result=pit_result,
                expected_tasks=plan.pit_tasks,
                first_index_weight=_first_index_weight_summary(
                    plan, store, diagnostic=exc.diagnostic
                ),
            )
        pit_result = build_pit_membership_artifacts(
            plan,
            pit_results,
            adjustment_evidence=adjustment_evidence,
            generated_at=timestamp,
        )
        publish_pit_membership_artifacts(root, pit_result, token=safe_token)
    if not pit_result.passed:
        return _blocked_summary(
            plan,
            root,
            stage_status="BLOCKED_PIT_MEMBERSHIP",
            blocker="pit_membership_incomplete",
            pit_result=pit_result,
            expected_tasks=plan.pit_tasks,
        )

    history_tasks = build_history_plan(plan, pit_result.union_instruments)
    if (
        continuation_execution
        and pit_network_performed
        and any(not store.is_complete(task) for task in history_tasks)
    ):
        # ``execute_tasks_bounded`` has its own interval clock.  Preserve the
        # physical boundary between the last PIT transport and the first
        # history transport by sleeping one full conservative interval here.
        sleeper(float(MINIMUM_REAL_TRANSPORT_INTERVAL_SECONDS))
    existing_history_coverage = root / "history_coverage_report.json"
    existing_history_manifest = root / "history_manifest.json"
    if existing_history_coverage.exists() or existing_history_manifest.exists():
        if not existing_history_coverage.is_file() or not existing_history_manifest.is_file():
            raise AlphaFeasibilityDataError("incomplete_history_artifact_pair")
        existing_coverage_value = _load_json_object(
            existing_history_coverage, "history_coverage_unavailable"
        )
        generated_text = existing_coverage_value.get("generated_at")
        if type(generated_text) is not str:
            raise AlphaFeasibilityDataError("history_generated_at_invalid")
        try:
            existing_timestamp = datetime.fromisoformat(generated_text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AlphaFeasibilityDataError("history_generated_at_invalid") from exc
        if existing_timestamp.tzinfo is None:
            raise AlphaFeasibilityDataError("history_generated_at_invalid")
        timestamp = existing_timestamp
    try:
        execute_tasks_bounded(
            history_tasks,
            store=store,
            token=safe_token,
            transport=active_transport,
            timeout_seconds=source["request_timeout_seconds"],
            maximum_response_bytes=source["maximum_response_bytes"],
            recover_interrupted_attempts=recover_attempts,
            maximum_attempts_per_fingerprint=maximum_attempts,
            persist_full_raw_transport=persist_full_raw_transport,
            minimum_request_interval_seconds=interval,
            sleeper=sleeper,
            monotonic=monotonic,
            business_error_retry_once=continuation_execution,
            clock=_clock,
            terminalize_transport_interruptions=continuation_execution,
        )
        blocker: str | None = None
        adapter_protocol_blocked = False
    except AlphaFeasibilityDataError as exc:
        if (
            recover_attempts
            and exc.code in RETRYABLE_ATTEMPT_FAILURES
            and not continuation_execution
        ):
            # Do not publish a terminal history blocker for a resumable
            # transport interruption.
            raise
        blocker = _public_data_failure_code(exc)
        adapter_protocol_blocked = (
            exc.code in ADAPTER_PROTOCOL_FAILURES
            or blocker in ADAPTER_PROTOCOL_FAILURES
        )
    if blocker is None:
        try:
            coverage = validate_history_coverage_from_store(
                plan,
                pit_result.union_instruments,
                history_tasks,
                store,
                pit_snapshots=pit_result.manifest["snapshots"],
                generated_at=timestamp,
            )
        except AlphaFeasibilityDataError as exc:
            blocker = exc.code
            adapter_protocol_blocked = False
            coverage = _blocked_history_coverage(
                plan, len(pit_result.union_instruments), blocker, timestamp
            )
    else:
        coverage = _blocked_history_coverage(
            plan, len(pit_result.union_instruments), blocker, timestamp
        )
    history_manifest = build_history_manifest_from_store(
        plan,
        history_tasks,
        store,
        coverage,
        pit_result=pit_result,
        request_counts=actual_tushare_request_count_by_endpoint(
            root,
            (*plan.pit_tasks, *history_tasks),
            plan_sha256=plan.plan_sha256,
        ),
        generated_at=timestamp,
    )
    publish_history_artifacts(root, coverage, history_manifest, token=safe_token)
    if blocker is not None or not coverage.passed:
        return _blocked_summary(
            plan,
            root,
            stage_status=(
                "BLOCKED_ADAPTER_PROTOCOL"
                if adapter_protocol_blocked
                else "BLOCKED_DATA"
            ),
            blocker=blocker or "history_coverage_incomplete",
            pit_result=pit_result,
            coverage=coverage,
            history_manifest=history_manifest,
            expected_tasks=(*plan.pit_tasks, *history_tasks),
        )
    return {
        "schema_version": "tushare-alpha-feasibility-backfill-result.v1",
        "experiment_id": plan.config["experiment_id"],
        "stage_status": "DATA_READY_FOR_ALPHA_FEASIBILITY",
        "terminal_status": None,
        "generated_at": coverage.report["generated_at"],
        **_backfill_lineage(
            plan,
            pit_result=pit_result,
            history_manifest=history_manifest,
        ),
        "actual_tushare_request_count_by_endpoint": actual_tushare_request_count_by_endpoint(
            root,
            (*plan.pit_tasks, *history_tasks),
            plan_sha256=plan.plan_sha256,
        ),
        "first_index_weight": _first_index_weight_summary(plan, store),
        "stock_basic_status": STOCK_BASIC_STATUS,
        "stock_basic_request_count": STOCK_BASIC_REQUEST_COUNT,
        "security_master_pit_status": SECURITY_MASTER_PIT_STATUS,
        "request_count_semantics": _request_count_semantics(plan),
        "coverage_start": coverage.report["coverage_start"],
        "coverage_end": coverage.report["coverage_end"],
        "pit_months_expected": 73,
        "pit_months_observed": pit_result.coverage_report["pit_months_observed"],
        "union_instrument_count": len(pit_result.union_instruments),
        "valid_candidate_count_by_decision": dict(
            coverage.report["valid_candidate_count_by_decision"]
        ),
        "insufficient_history_count_by_decision": dict(
            coverage.report["insufficient_history_count_by_decision"]
        ),
        "ineligible_no_initial_price_count": int(
            coverage.report["ineligible_no_initial_price_count"]
        ),
        "unexplained_market_data_gap_count": int(
            coverage.report["unexplained_market_data_gap_count"]
        ),
        "daily_coverage_status": coverage.report["daily_coverage_status"],
        "adj_factor_coverage_status": coverage.report["adj_factor_coverage_status"],
        "suspension_coverage_status": coverage.report["suspension_coverage_status"],
        "benchmark_coverage_status": coverage.report["benchmark_coverage_status"],
        "remaining_blockers": [],
        "locked_test_status": dict(LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
        "execution_realism": "INCOMPLETE",
        "trade_eligibility": False,
    }


def run_backfill_from_environment(
    config_path: Path | str,
    output_root: Path | str,
    transport: TushareTransport | None = None,
    generated_at: datetime | None = None,
    *,
    repository_root: Path | str = REPOSITORY_ROOT,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Environment wrapper whose credential lookup happens after preflight."""

    plan = load_config_and_build_plan(config_path, repository_root=repository_root)
    environment = os.environ if environ is None else environ
    variable = plan.config["source"]["token_environment_variable"]
    token = environment.get(variable)
    return run_backfill(
        config_path,
        output_root,
        _validate_token(token),
        transport=transport,
        generated_at=generated_at,
        repository_root=repository_root,
    )


def load_feasibility_inputs(
    output_root: Path | str,
    config_path: Path | str = DEFAULT_CONFIG_PATH,
    *,
    repository_root: Path | str = REPOSITORY_ROOT,
) -> dict[str, Any]:
    """Load verified, cutoff-safe dictionaries for the independent engine."""

    plan = load_config_and_build_plan(config_path, repository_root=repository_root)
    root = Path(output_root)
    pit = _load_existing_pit_result(root, plan)
    if pit is None or not pit.passed:
        raise AlphaFeasibilityDataError("pit_membership_not_admitted", stage="pit")
    manifest = _load_json_object(root / "history_manifest.json", "history_manifest_unavailable")
    coverage_artifact = _load_json_object(
        root / "history_coverage_report.json", "history_coverage_unavailable"
    )
    if coverage_artifact.get("schema_version") != HISTORY_COVERAGE_SCHEMA_VERSION:
        raise AlphaFeasibilityDataError("history_coverage_schema_version_invalid")
    expected_history_manifest_schema = _history_manifest_schema_version(plan)
    try:
        validate_json_schema(
            manifest,
            Path(repository_root)
            / "schemas"
            / HISTORY_MANIFEST_SCHEMA_PATHS[expected_history_manifest_schema].name,
        )
    except SchemaValidationError as exc:
        raise AlphaFeasibilityDataError("history_manifest_schema_invalid") from exc
    unsigned_manifest = dict(manifest)
    declared_manifest_hash = unsigned_manifest.pop("manifest_sha256", None)
    if (
        manifest.get("schema_version") != expected_history_manifest_schema
        or manifest.get("data_status") != "READY"
        or declared_manifest_hash != canonical_sha256(unsigned_manifest)
        or manifest.get("locked_test_status") != dict(LOCKED_TEST_STATUS)
        or manifest.get("locked_test_consumed") is not False
    ):
        raise AlphaFeasibilityDataError("history_manifest_verification_failed")
    expected_history_tasks = build_history_plan(plan, pit.union_instruments)
    store = CreateOnlyTaskStore(root)
    generated_text = coverage_artifact.get("generated_at")
    if type(generated_text) is not str:
        raise AlphaFeasibilityDataError("history_generated_at_invalid")
    try:
        coverage_generated_at = datetime.fromisoformat(generated_text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AlphaFeasibilityDataError("history_generated_at_invalid") from exc
    coverage = validate_history_coverage_from_store(
        plan,
        pit.union_instruments,
        expected_history_tasks,
        store,
        pit_snapshots=pit.manifest["snapshots"],
        generated_at=coverage_generated_at,
    )
    if not coverage.passed or dict(coverage.report) != dict(coverage_artifact):
        raise AlphaFeasibilityDataError("history_coverage_replay_failed")
    rebuilt_manifest = build_history_manifest_from_store(
        plan,
        expected_history_tasks,
        store,
        coverage,
        pit_result=pit,
        request_counts=actual_tushare_request_count_by_endpoint(
            root,
            (*plan.pit_tasks, *expected_history_tasks),
            plan_sha256=plan.plan_sha256,
        ),
        generated_at=coverage_generated_at,
    )
    if dict(rebuilt_manifest) != dict(manifest):
        raise AlphaFeasibilityDataError("history_manifest_replay_failed")

    by_endpoint = {
        endpoint: [task for task in expected_history_tasks if task.endpoint == endpoint]
        for endpoint in ALLOWED_ENDPOINTS
    }
    adj_by_code = {task.scope_instruments[0]: task for task in by_endpoint["adj_factor"]}
    suspend_by_scope = {task.scope_instruments: task for task in by_endpoint["suspend_d"]}

    def panel_for_task(daily_task: CollectionTask) -> list[dict[str, Any]]:
        scope = daily_task.scope_instruments
        daily_rows = store._load_response(daily_task).rows
        adj_rows: list[Mapping[str, Any]] = []
        for code in scope:
            adj_rows.extend(store._load_response(adj_by_code[code]).rows)
        suspension_rows = store._load_response(suspend_by_scope[scope]).rows
        return build_total_return_panel(
            coverage.trading_dates,
            list(scope),
            daily_rows,
            adj_rows,
            suspension_rows,
            coverage_start=plan.config["dates"]["signal_warmup_start"],
            coverage_end=plan.config["dates"]["validation_end"],
        )

    def signal_rows() -> Iterable[dict[str, Any]]:
        for daily_task in by_endpoint["daily"]:
            yield from panel_for_task(daily_task)

    benchmark_rows = store._load_response(by_endpoint["index_daily"][0]).rows
    benchmark_bars = [
        {
            "trading_date": _iso(_parse_date(row["trade_date"], "benchmark_date")),
            "ts_code": row["ts_code"],
            "close": row["close"],
            "high": row["high"],
        }
        for row in sorted(benchmark_rows, key=lambda item: item["trade_date"])
    ]

    def suspension_records() -> Iterable[dict[str, Any]]:
        # The engine's legacy ``suspensions`` input is the non-tradable-day
        # channel.  Availability is derived from daily+causal-adj evidence;
        # suspend_d is diagnostic only and cannot override a usable bar.
        for daily_task in by_endpoint["daily"]:
            for row in panel_for_task(daily_task):
                if row["is_unavailable_no_daily_bar"]:
                    yield {
                        "trading_date": row["trading_date"],
                        "ts_code": row["ts_code"],
                        "instrument_id": row["ts_code"],
                        "suspend_type": row["suspend_type"],
                    }
    snapshots = [
        {
            "snapshot_date": _iso(_parse_date(item["snapshot_date"], "snapshot_date")),
            "members": [member["instrument_id"] for member in item["members"]],
        }
        for item in pit.manifest["snapshots"]
    ]
    return {
        "coverage_start": coverage.report["coverage_start"],
        "coverage_end": coverage.report["coverage_end"],
        "trading_dates": [
            _iso(_parse_date(item, "trading_date")) for item in coverage.trading_dates
        ],
        "pit_snapshots": snapshots,
        "pit_coverage_report": _json_safe(pit.coverage_report),
        "pit_manifest": _json_safe(pit.manifest),
        "signal_bars": signal_rows(),
        "benchmark_bars": benchmark_bars,
        "suspensions": suspension_records(),
        "locked_test_status": dict(LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
        "execution_realism": "INCOMPLETE",
        "trade_eligibility": False,
    }
