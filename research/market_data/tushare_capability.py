"""Offline-only domain contract for bounded Tushare capability probes.

This module deliberately contains no SDK import, environment access, network
call, storage writer, market-data admission, factor, signal or order code.  It
only defines the fixed read-only endpoint vocabulary, validates the versioned
probe plan, normalizes DataFrame-shaped responses, classifies failures without
persisting exception text, and builds a self-hashed capability-only receipt.

A valid receipt proves local structural consistency only.  It is not source
authentication and cannot grant Market Data, Experiment V3, Paper, trading,
real-money-list or LIVE authority.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .contracts import (
    aware_datetime,
    canonical_json_bytes as _market_data_canonical_json_bytes,
    decimal_text,
    sha256_bytes,
)
from .providers.base import (
    DependencyMissingError,
    NetworkBlockedError,
    ProviderNotConfiguredError,
    classify_unexpected_error,
    safe_error_text,
)
from .validation import SchemaValidationError, validate_json_schema


CONFIG_SCHEMA_VERSION = "tushare-capability-probe-config.v1"
ENDPOINT_RESULT_SCHEMA_VERSION = "tushare-endpoint-result.v1"
CAPABILITY_RECEIPT_SCHEMA_VERSION = "tushare-capability-receipt.v1"
CROSS_VALIDATION_OUTCOME_SCHEMA_VERSION = (
    "tushare-baostock-cross-validation-outcome.v1"
)
CAPABILITY_SCOPE = "capability_probe_only_not_admitted"
PROVIDER_ID = "tushare"
CREDENTIAL_ENVIRONMENT_VARIABLE = "TUSHARE_TOKEN"

FORMAL_DATA_ADMISSION = False
EXPERIMENT_V3_IMPACT = "none"
DAILY_SIGNAL_AUTHORITY = "none"
NEXT_SESSION_ALLOWED = False
PAPER_ELIGIBILITY = False
TRADE_ELIGIBILITY = False
REAL_MONEY_LIST_ALLOWED = False
AUTOMATIC_ORDER_SUBMISSION = False
LIVE_SUPPORTED = False

_ROOT = Path(__file__).resolve().parents[2]
_ENDPOINT_SCHEMA_PATH = _ROOT / "schemas" / "tushare_endpoint_result.v1.json"
_RECEIPT_SCHEMA_PATH = _ROOT / "schemas" / "tushare_capability_receipt.v1.json"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_DATE_COMPACT_RE = re.compile(r"^[0-9]{8}$")
_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,31}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_TUSHARE_DAILY_RAW_PATH_RE = re.compile(r"^raw/daily\.[0-9]{2}\.json$")
_BAOSTOCK_DAILY_RAW_PATH_RE = re.compile(
    r"^raw/cross_validation/baostock_daily\.json$"
)
_SECRET_KEY_RE = re.compile(
    r"(?:token|secret|password|passwd|cookie|authorization|credential|"
    r"api[_-]?key|access[_-]?key|account|user[_-]?id|phone|mobile|otp)",
    re.IGNORECASE,
)


class TushareCapabilityError(ValueError):
    """Base error for malformed capability-only evidence."""


class TushareCapabilityConfigError(TushareCapabilityError):
    """Raised when a probe plan can escape or drift from the fixed contract."""


class TushareEndpointPayloadError(TushareCapabilityError):
    """Raised when an SDK result cannot be normalized safely."""

    status = "invalid_payload"
    failure_code = "invalid_payload"


class TusharePositiveOrNegativeInfinityError(TushareEndpointPayloadError):
    """Raised when an endpoint cell contains positive or negative infinity."""

    failure_code = "positive_or_negative_infinity"


class TushareUnsupportedScalarError(TushareEndpointPayloadError):
    """Raised when an endpoint cell cannot be reduced to a safe JSON scalar."""

    failure_code = "unsupported_scalar"


class TushareDataFrameShapeError(TushareEndpointPayloadError):
    """Raised when an SDK response does not have the frozen DataFrame shape."""

    failure_code = "dataframe_shape_invalid"


class TushareResponseRowLimitExceededError(TushareEndpointPayloadError):
    """Raised when an endpoint response exceeds its configured evidence cap."""

    failure_code = "response_row_limit_exceeded"


class TushareEndpointSchemaError(TushareEndpointPayloadError):
    """Raised when the upstream response shape lacks frozen required fields."""

    status = "schema_drift"
    failure_code = "schema_required_fields_missing"


class TusharePermissionDeniedError(RuntimeError):
    """Typed permission failure for injected adapters and unit tests."""


class TushareRateLimitedError(RuntimeError):
    """Typed rate-limit failure for injected adapters and unit tests."""


class Endpoint(str, Enum):
    TRADE_CAL = "trade_cal"
    STOCK_BASIC = "stock_basic"
    DAILY = "daily"
    DAILY_BASIC = "daily_basic"
    ADJ_FACTOR = "adj_factor"
    SUSPEND_D = "suspend_d"
    STK_LIMIT = "stk_limit"
    NAMECHANGE = "namechange"
    INDEX_BASIC = "index_basic"
    INDEX_DAILY = "index_daily"
    INDEX_WEIGHT = "index_weight"
    INDEX_CLASSIFY = "index_classify"
    INDEX_MEMBER_ALL = "index_member_all"
    INCOME = "income"
    INCOME_VIP = "income_vip"
    BALANCESHEET = "balancesheet"
    BALANCESHEET_VIP = "balancesheet_vip"
    CASHFLOW = "cashflow"
    CASHFLOW_VIP = "cashflow_vip"
    DISCLOSURE_DATE = "disclosure_date"
    FINA_INDICATOR = "fina_indicator"
    DIVIDEND = "dividend"


ENDPOINT_ORDER = tuple(Endpoint)

# The SDK method is code-owned.  A JSON config may select an Endpoint enum but
# can never add a method name or redirect an endpoint through getattr.
SDK_METHOD_BY_ENDPOINT: Mapping[Endpoint, str] = MappingProxyType(
    {endpoint: endpoint.value for endpoint in Endpoint}
)

_COMMON_FINANCIAL_PARAMETERS = frozenset(
    {
        "ts_code",
        "ann_date",
        "f_ann_date",
        "start_date",
        "end_date",
        "period",
        "report_type",
        "comp_type",
    }
)
ALLOWED_PARAMETER_KEYS: Mapping[Endpoint, frozenset[str]] = MappingProxyType(
    {
        Endpoint.TRADE_CAL: frozenset(
            {"exchange", "start_date", "end_date", "is_open"}
        ),
        Endpoint.STOCK_BASIC: frozenset(
            {"ts_code", "name", "exchange", "market", "is_hs", "list_status"}
        ),
        Endpoint.DAILY: frozenset(
            {"ts_code", "trade_date", "start_date", "end_date"}
        ),
        Endpoint.DAILY_BASIC: frozenset(
            {"ts_code", "trade_date", "start_date", "end_date"}
        ),
        Endpoint.ADJ_FACTOR: frozenset(
            {"ts_code", "trade_date", "start_date", "end_date"}
        ),
        Endpoint.SUSPEND_D: frozenset(
            {"ts_code", "trade_date", "start_date", "end_date", "suspend_type"}
        ),
        Endpoint.STK_LIMIT: frozenset(
            {"ts_code", "trade_date", "start_date", "end_date"}
        ),
        Endpoint.NAMECHANGE: frozenset({"ts_code", "start_date", "end_date"}),
        Endpoint.INDEX_BASIC: frozenset(
            {"ts_code", "name", "market", "publisher", "category"}
        ),
        Endpoint.INDEX_DAILY: frozenset(
            {"ts_code", "trade_date", "start_date", "end_date"}
        ),
        Endpoint.INDEX_WEIGHT: frozenset(
            {"index_code", "trade_date", "start_date", "end_date"}
        ),
        Endpoint.INDEX_CLASSIFY: frozenset({"index_code", "level", "src"}),
        Endpoint.INDEX_MEMBER_ALL: frozenset(
            {"l1_code", "l2_code", "l3_code", "ts_code", "is_new"}
        ),
        Endpoint.INCOME: _COMMON_FINANCIAL_PARAMETERS,
        Endpoint.INCOME_VIP: _COMMON_FINANCIAL_PARAMETERS,
        Endpoint.BALANCESHEET: _COMMON_FINANCIAL_PARAMETERS,
        Endpoint.BALANCESHEET_VIP: _COMMON_FINANCIAL_PARAMETERS,
        Endpoint.CASHFLOW: _COMMON_FINANCIAL_PARAMETERS,
        Endpoint.CASHFLOW_VIP: _COMMON_FINANCIAL_PARAMETERS,
        Endpoint.DISCLOSURE_DATE: frozenset(
            {"ts_code", "end_date", "pre_date", "actual_date"}
        ),
        Endpoint.FINA_INDICATOR: frozenset(
            {"ts_code", "ann_date", "start_date", "end_date", "period"}
        ),
        Endpoint.DIVIDEND: frozenset(
            {"ts_code", "ann_date", "record_date", "ex_date", "imp_ann_date"}
        ),
    }
)

ENDPOINT_STATUSES = frozenset(
    {
        "passed",
        "not_configured",
        "dependency_missing",
        "permission_denied",
        "rate_limited",
        "network_blocked",
        "empty_result",
        "schema_drift",
        "invalid_payload",
        "failed",
        "not_run_after_global_stop",
    }
)
PERMISSION_STATUSES = frozenset(
    {"observed_available", "denied", "not_configured", "unknown", "not_applicable"}
)
ERROR_CLASSES = frozenset(
    {
        "dependency",
        "configuration",
        "permission",
        "rate_limit",
        "network",
        "empty",
        "schema",
        "payload",
        "unexpected",
    }
)
PIT_EVIDENCE_STATUSES = frozenset(
    {
        "candidate_fields_present_not_admitted",
        "missing_pit_fields",
        "not_assessed",
        "not_applicable",
    }
)
MIGRATION_CANDIDATE_ROLES = frozenset(
    {
        "phase2_primary_candidate",
        "phase2_validation_candidate",
        "diagnostic_only",
        "blocked",
        "unknown",
    }
)
RECEIPT_STATUSES = frozenset(
    {"passed", "partial", "failed", "not_configured", "dependency_missing", "incomplete"}
)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TushareCapabilityError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise TushareCapabilityError(f"non-finite JSON constant: {value}")


def _reject_nonfinite(value: Any, path: str = "$") -> None:
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TushareCapabilityError(f"{path} contains a non-finite number")
        return
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise TushareCapabilityError(f"{path} contains a non-finite number")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TushareCapabilityError(f"{path} has a non-string JSON key")
            _reject_nonfinite(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_nonfinite(item, f"{path}[{index}]")
        return
    if isinstance(value, (date, datetime)):
        return
    raise TushareCapabilityError(
        f"{path} contains unsupported JSON value {type(value).__name__}"
    )


def _plain_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json_value(item) for item in value]
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON and reject non-finite/foreign values."""

    _reject_nonfinite(value)
    try:
        return _market_data_canonical_json_bytes(_plain_json_value(value))
    except (TypeError, ValueError) as exc:
        raise TushareCapabilityError(f"value cannot be canonicalized: {exc}") from exc


def canonical_sha256(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def strict_json_loads(
    raw: bytes | str,
    *,
    label: str = "JSON",
    require_canonical: bool = False,
) -> Any:
    """Parse strict JSON, rejecting duplicate keys and NaN/Infinity.

    ``require_canonical`` is used for persisted receipts.  Human-maintained
    config files remain free to use indentation, while their identity is the
    hash of the parsed canonical value.
    """

    if isinstance(raw, str):
        encoded = raw.encode("utf-8")
    elif isinstance(raw, bytes):
        encoded = raw
    else:
        raise TushareCapabilityError(f"{label} must be bytes or text")
    try:
        value = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TushareCapabilityError) as exc:
        raise TushareCapabilityError(f"{label} is not strict JSON: {exc}") from exc
    _reject_nonfinite(value)
    if require_canonical and canonical_json_bytes(value) != encoded:
        raise TushareCapabilityError(f"{label} is not canonical JSON")
    return value


def _as_endpoint(value: Endpoint | str) -> Endpoint:
    if isinstance(value, Endpoint):
        return value
    try:
        return Endpoint(str(value).strip())
    except ValueError as exc:
        raise TushareCapabilityConfigError(
            f"endpoint is outside the fixed allowlist: {value!r}"
        ) from exc


def _identifier(value: Any, field_name: str) -> str:
    normalized = str(value).strip()
    if _IDENTIFIER_RE.fullmatch(normalized) is None:
        raise TushareCapabilityError(f"{field_name} is not a valid identifier")
    return normalized


def _sha256(value: Any, field_name: str) -> str:
    normalized = str(value).strip()
    if _SHA256_RE.fullmatch(normalized) is None:
        raise TushareCapabilityError(f"{field_name} must be a lowercase SHA-256")
    return normalized


def _fields(value: Sequence[Any], field_name: str, *, allow_empty: bool) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TushareCapabilityConfigError(f"{field_name} must be an array")
    result = tuple(str(item).strip() for item in value)
    if not allow_empty and not result:
        raise TushareCapabilityConfigError(f"{field_name} must not be empty")
    if len(set(result)) != len(result):
        raise TushareCapabilityConfigError(f"{field_name} contains duplicates")
    if any(_FIELD_RE.fullmatch(item) is None for item in result):
        raise TushareCapabilityConfigError(f"{field_name} contains an invalid field name")
    return result


def _safe_parameter_text(key: str, value: Any) -> str:
    if isinstance(value, bool) or value is None or not isinstance(value, (str, int)):
        raise TushareCapabilityConfigError(
            f"parameter {key} must be a non-empty string or integer"
        )
    text = str(value).strip()
    if not text or len(text) > 64 or _CONTROL_RE.search(text):
        raise TushareCapabilityConfigError(f"parameter {key} is empty or unsafe")
    if (
        "://" in text
        or text.startswith(("/", "\\", "~"))
        or _WINDOWS_ABSOLUTE_RE.match(text)
        or "${" in text
        or "$(" in text
    ):
        raise TushareCapabilityConfigError(
            f"parameter {key} must not contain a URL, path or expression"
        )
    if key.endswith("date") or key in {"start_date", "end_date", "period"}:
        if _DATE_COMPACT_RE.fullmatch(text) is None:
            raise TushareCapabilityConfigError(
                f"parameter {key} must use YYYYMMDD"
            )
        try:
            date(int(text[:4]), int(text[4:6]), int(text[6:]))
        except ValueError as exc:
            raise TushareCapabilityConfigError(
                f"parameter {key} is not a valid date"
            ) from exc
    if key in {
        "ts_code",
        "index_code",
        "l1_code",
        "l2_code",
        "l3_code",
    } and _CODE_RE.fullmatch(text) is None:
        raise TushareCapabilityConfigError(f"parameter {key} is not a valid code")
    enum_values = {
        "list_status": {"L", "D", "P"},
        "is_open": {"0", "1"},
        "is_new": {"Y", "N"},
        "suspend_type": {"S", "R"},
        "level": {"L1", "L2", "L3"},
        "src": {"SW", "SW2021", "CSI"},
        "exchange": {"SSE", "SZSE", "BSE"},
        "is_hs": {"N", "H", "S"},
    }
    if key in enum_values and text not in enum_values[key]:
        raise TushareCapabilityConfigError(
            f"parameter {key} is outside the fixed safe values"
        )
    if key in {"report_type", "comp_type"} and re.fullmatch(r"[0-9]{1,3}", text) is None:
        raise TushareCapabilityConfigError(f"parameter {key} must be a numeric code")
    return text


def normalize_parameters(
    endpoint: Endpoint | str,
    parameters: Mapping[str, Any],
) -> Mapping[str, str]:
    """Normalize one fixed endpoint call without accepting arbitrary kwargs."""

    selected = _as_endpoint(endpoint)
    if not isinstance(parameters, Mapping):
        raise TushareCapabilityConfigError("endpoint parameters must be an object")
    allowed = ALLOWED_PARAMETER_KEYS[selected]
    unknown = set(parameters) - allowed
    if unknown:
        raise TushareCapabilityConfigError(
            f"{selected.value} parameters contain unsupported keys: {sorted(unknown)}"
        )
    normalized: dict[str, str] = {}
    for raw_key, raw_value in parameters.items():
        if not isinstance(raw_key, str) or _FIELD_RE.fullmatch(raw_key) is None:
            raise TushareCapabilityConfigError("endpoint parameter key is invalid")
        if _SECRET_KEY_RE.search(raw_key):
            raise TushareCapabilityConfigError(
                "credentials are forbidden in endpoint parameters"
            )
        normalized[raw_key] = _safe_parameter_text(raw_key, raw_value)
    return MappingProxyType(dict(sorted(normalized.items())))


@dataclass(frozen=True, slots=True)
class EndpointSpec:
    endpoint: Endpoint | str
    parameters: tuple[Mapping[str, Any], ...]
    required_fields: tuple[str, ...]
    candidate_primary_key: tuple[str, ...]
    date_fields: tuple[str, ...]
    max_rows: int
    max_calls: int
    required_probe: bool
    pit_critical: bool
    cross_validation_only: bool
    raw_units: Mapping[str, str]
    expected_data_role: str
    migration_candidate_role: str

    def __post_init__(self) -> None:
        endpoint = _as_endpoint(self.endpoint)
        object.__setattr__(self, "endpoint", endpoint)
        if not isinstance(self.parameters, tuple) or not self.parameters:
            raise TushareCapabilityConfigError(
                f"{endpoint.value}.parameters must be a non-empty array"
            )
        normalized_parameters = tuple(
            normalize_parameters(endpoint, item) for item in self.parameters
        )
        fingerprints = [canonical_sha256(dict(item)) for item in normalized_parameters]
        if len(fingerprints) != len(set(fingerprints)):
            raise TushareCapabilityConfigError(
                f"{endpoint.value}.parameters contains duplicate calls"
            )
        object.__setattr__(self, "parameters", normalized_parameters)
        required = _fields(self.required_fields, "required_fields", allow_empty=False)
        primary_key = _fields(
            self.candidate_primary_key,
            "candidate_primary_key",
            allow_empty=False,
        )
        dates = _fields(self.date_fields, "date_fields", allow_empty=True)
        if not set(primary_key).issubset(required):
            raise TushareCapabilityConfigError(
                f"{endpoint.value} candidate_primary_key must be required fields"
            )
        object.__setattr__(self, "required_fields", required)
        object.__setattr__(self, "candidate_primary_key", primary_key)
        object.__setattr__(self, "date_fields", dates)
        if type(self.max_rows) is not int or not 1 <= self.max_rows <= 10000:
            raise TushareCapabilityConfigError("max_rows must be between 1 and 10000")
        if type(self.max_calls) is not int or self.max_calls != len(normalized_parameters):
            raise TushareCapabilityConfigError(
                "max_calls must exactly equal the frozen parameter call count"
            )
        for field_name in ("required_probe", "pit_critical", "cross_validation_only"):
            if type(getattr(self, field_name)) is not bool:
                raise TushareCapabilityConfigError(f"{field_name} must be boolean")
        if not isinstance(self.raw_units, Mapping):
            raise TushareCapabilityConfigError("raw_units must be an object")
        units: dict[str, str] = {}
        for key, value in self.raw_units.items():
            if _FIELD_RE.fullmatch(str(key)) is None:
                raise TushareCapabilityConfigError("raw_units has an invalid field")
            text = str(value).strip()
            if not text or len(text) > 160 or _CONTROL_RE.search(text):
                raise TushareCapabilityConfigError("raw_units has an unsafe value")
            units[str(key)] = text
        object.__setattr__(self, "raw_units", MappingProxyType(dict(sorted(units.items()))))
        role = _identifier(self.expected_data_role, "expected_data_role")
        object.__setattr__(self, "expected_data_role", role)
        migration_role = str(self.migration_candidate_role).strip()
        if migration_role not in MIGRATION_CANDIDATE_ROLES:
            raise TushareCapabilityConfigError(
                "migration_candidate_role is outside the non-admitted enum"
            )
        if endpoint is Endpoint.FINA_INDICATOR and not self.cross_validation_only:
            raise TushareCapabilityConfigError(
                "fina_indicator must remain cross_validation_only"
            )
        if endpoint is Endpoint.FINA_INDICATOR and migration_role not in {
            "phase2_validation_candidate",
            "diagnostic_only",
        }:
            raise TushareCapabilityConfigError(
                "fina_indicator cannot be a primary migration candidate"
            )
        object.__setattr__(self, "migration_candidate_role", migration_role)

    @property
    def sdk_method(self) -> str:
        return SDK_METHOD_BY_ENDPOINT[self.endpoint]  # type: ignore[index]

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint.value,  # type: ignore[union-attr]
            "parameters": [dict(item) for item in self.parameters],
            "required_fields": list(self.required_fields),
            "candidate_primary_key": list(self.candidate_primary_key),
            "date_fields": list(self.date_fields),
            "max_rows": self.max_rows,
            "max_calls": self.max_calls,
            "required_probe": self.required_probe,
            "pit_critical": self.pit_critical,
            "cross_validation_only": self.cross_validation_only,
            "raw_units": dict(self.raw_units),
            "expected_data_role": self.expected_data_role,
            "migration_candidate_role": self.migration_candidate_role,
        }


def requested_fields_for(spec: EndpointSpec) -> tuple[str, ...]:
    """Return the code-owned, deterministic SDK field projection for a spec.

    The config may define the versioned field contract, but it cannot provide
    an SDK ``fields`` kwarg.  Selection and ordering are fixed here as required
    fields, then new date fields, then new candidate-primary-key fields.
    """

    if type(spec) is not EndpointSpec:
        raise TushareCapabilityConfigError(
            "requested fields require the exact EndpointSpec type"
        )
    selected: list[str] = []
    seen: set[str] = set()
    for field_name in (
        *spec.required_fields,
        *spec.date_fields,
        *spec.candidate_primary_key,
    ):
        if field_name not in seen:
            selected.append(field_name)
            seen.add(field_name)
    if not selected:
        raise TushareCapabilityConfigError("requested fields must not be empty")
    return tuple(selected)


_CONFIG_FIELDS = frozenset(
    {
        "schema_version",
        "provider_id",
        "scope",
        "credential_environment_variable",
        "maximum_request_count",
        "cross_validation_request_reserve",
        "minimum_interval_seconds",
        "global_stop_after_consecutive_rate_limits",
        "global_stop_on_permission_denied",
        "endpoints",
    }
)
_SPEC_FIELDS = frozenset(
    {
        "endpoint",
        "parameters",
        "required_fields",
        "candidate_primary_key",
        "date_fields",
        "max_rows",
        "max_calls",
        "required_probe",
        "pit_critical",
        "cross_validation_only",
        "raw_units",
        "expected_data_role",
        "migration_candidate_role",
    }
)


@dataclass(frozen=True, slots=True)
class ProbeConfig:
    endpoints: tuple[EndpointSpec, ...]
    maximum_request_count: int
    cross_validation_request_reserve: int
    minimum_interval_seconds: Decimal | str
    global_stop_after_consecutive_rate_limits: int
    global_stop_on_permission_denied: bool
    schema_version: str = CONFIG_SCHEMA_VERSION
    provider_id: str = PROVIDER_ID
    scope: str = CAPABILITY_SCOPE
    credential_environment_variable: str = CREDENTIAL_ENVIRONMENT_VARIABLE

    def __post_init__(self) -> None:
        if self.schema_version != CONFIG_SCHEMA_VERSION:
            raise TushareCapabilityConfigError("unsupported probe config schema")
        if self.provider_id != PROVIDER_ID or self.scope != CAPABILITY_SCOPE:
            raise TushareCapabilityConfigError(
                "probe config must remain Tushare capability-only"
            )
        if self.credential_environment_variable != CREDENTIAL_ENVIRONMENT_VARIABLE:
            raise TushareCapabilityConfigError(
                "only TUSHARE_TOKEN may configure the capability probe"
            )
        if not isinstance(self.endpoints, tuple) or not all(
            type(item) is EndpointSpec for item in self.endpoints
        ):
            raise TushareCapabilityConfigError(
                "endpoints must use the exact EndpointSpec contract"
            )
        endpoint_ids = tuple(item.endpoint for item in self.endpoints)
        if len(endpoint_ids) != len(set(endpoint_ids)):
            raise TushareCapabilityConfigError("probe config contains duplicate endpoints")
        if set(endpoint_ids) != set(Endpoint):
            missing = sorted(item.value for item in set(Endpoint) - set(endpoint_ids))
            raise TushareCapabilityConfigError(
                f"probe config must include exactly the fixed endpoint allowlist; missing={missing}"
            )
        if type(self.maximum_request_count) is not int or not 1 <= self.maximum_request_count <= 50:
            raise TushareCapabilityConfigError(
                "maximum_request_count must be between 1 and 50"
            )
        if (
            type(self.cross_validation_request_reserve) is not int
            or not 0 <= self.cross_validation_request_reserve <= 2
        ):
            raise TushareCapabilityConfigError(
                "cross_validation_request_reserve must be between 0 and 2"
            )
        planned = sum(item.max_calls for item in self.endpoints)
        if planned + self.cross_validation_request_reserve > self.maximum_request_count:
            raise TushareCapabilityConfigError(
                "frozen calls plus cross-validation reserve exceed maximum_request_count"
            )
        try:
            interval = Decimal(str(self.minimum_interval_seconds))
        except (InvalidOperation, ValueError) as exc:
            raise TushareCapabilityConfigError(
                "minimum_interval_seconds must be a finite decimal"
            ) from exc
        if not interval.is_finite() or interval < Decimal("1") or interval > Decimal("60"):
            raise TushareCapabilityConfigError(
                "minimum_interval_seconds must be conservatively bounded from 1 to 60"
            )
        object.__setattr__(self, "minimum_interval_seconds", interval)
        if (
            type(self.global_stop_after_consecutive_rate_limits) is not int
            or self.global_stop_after_consecutive_rate_limits != 3
        ):
            raise TushareCapabilityConfigError(
                "global rate-limit stop must remain fixed at three consecutive events"
            )
        if self.global_stop_on_permission_denied is not True:
            raise TushareCapabilityConfigError(
                "global permission failure must stop subsequent calls"
            )

    @property
    def planned_request_count(self) -> int:
        return sum(item.max_calls for item in self.endpoints)

    @property
    def config_sha256(self) -> str:
        return canonical_sha256(self.to_dict())

    def planned_calls(self) -> tuple[tuple[EndpointSpec, Mapping[str, str]], ...]:
        return tuple(
            (spec, parameters)
            for spec in self.endpoints
            for parameters in spec.parameters
        )

    def spec_for(self, endpoint: Endpoint | str) -> EndpointSpec:
        selected = _as_endpoint(endpoint)
        for spec in self.endpoints:
            if spec.endpoint is selected:
                return spec
        raise TushareCapabilityConfigError(f"endpoint {selected.value} is not configured")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "provider_id": self.provider_id,
            "scope": self.scope,
            "credential_environment_variable": self.credential_environment_variable,
            "maximum_request_count": self.maximum_request_count,
            "cross_validation_request_reserve": self.cross_validation_request_reserve,
            "minimum_interval_seconds": decimal_text(
                self.minimum_interval_seconds,
                "minimum_interval_seconds",
            ),
            "global_stop_after_consecutive_rate_limits": self.global_stop_after_consecutive_rate_limits,
            "global_stop_on_permission_denied": self.global_stop_on_permission_denied,
            "endpoints": [item.to_dict() for item in self.endpoints],
        }


def _config_from_mapping(value: Mapping[str, Any]) -> ProbeConfig:
    if set(value) != _CONFIG_FIELDS:
        missing = sorted(_CONFIG_FIELDS - set(value))
        unknown = sorted(set(value) - _CONFIG_FIELDS)
        raise TushareCapabilityConfigError(
            f"probe config fields differ from contract; missing={missing}, unknown={unknown}"
        )
    raw_endpoints = value.get("endpoints")
    if not isinstance(raw_endpoints, list):
        raise TushareCapabilityConfigError("endpoints must be an array")
    endpoints: list[EndpointSpec] = []
    for index, raw_spec in enumerate(raw_endpoints):
        if not isinstance(raw_spec, Mapping) or set(raw_spec) != _SPEC_FIELDS:
            raise TushareCapabilityConfigError(
                f"endpoints[{index}] fields differ from the fixed contract"
            )
        raw_parameters = raw_spec.get("parameters")
        if not isinstance(raw_parameters, list) or any(
            not isinstance(item, Mapping) for item in raw_parameters
        ):
            raise TushareCapabilityConfigError(
                f"endpoints[{index}].parameters must be an object array"
            )
        endpoints.append(
            EndpointSpec(
                endpoint=str(raw_spec["endpoint"]),
                parameters=tuple(raw_parameters),
                required_fields=tuple(raw_spec["required_fields"]),
                candidate_primary_key=tuple(raw_spec["candidate_primary_key"]),
                date_fields=tuple(raw_spec["date_fields"]),
                max_rows=raw_spec["max_rows"],
                max_calls=raw_spec["max_calls"],
                required_probe=raw_spec["required_probe"],
                pit_critical=raw_spec["pit_critical"],
                cross_validation_only=raw_spec["cross_validation_only"],
                raw_units=raw_spec["raw_units"],
                expected_data_role=str(raw_spec["expected_data_role"]),
                migration_candidate_role=str(raw_spec["migration_candidate_role"]),
            )
        )
    return ProbeConfig(
        endpoints=tuple(endpoints),
        maximum_request_count=value["maximum_request_count"],
        cross_validation_request_reserve=value["cross_validation_request_reserve"],
        minimum_interval_seconds=value["minimum_interval_seconds"],
        global_stop_after_consecutive_rate_limits=value[
            "global_stop_after_consecutive_rate_limits"
        ],
        global_stop_on_permission_denied=value["global_stop_on_permission_denied"],
        schema_version=str(value["schema_version"]),
        provider_id=str(value["provider_id"]),
        scope=str(value["scope"]),
        credential_environment_variable=str(value["credential_environment_variable"]),
    )


def load_probe_config(path: Path | str) -> ProbeConfig:
    resolved = Path(path)
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        raise TushareCapabilityConfigError(f"cannot read probe config: {exc}") from exc
    value = strict_json_loads(raw, label="Tushare capability probe config")
    if not isinstance(value, Mapping):
        raise TushareCapabilityConfigError("probe config root must be an object")
    return _config_from_mapping(value)


_KNOWN_OPTIONAL_MISSING_SCALAR_TYPES = frozenset(
    {
        ("pandas._libs.missing", "NAType"),
        ("pandas._libs.tslibs.nattype", "NaTType"),
        ("pandas.api.typing", "NAType"),
        ("pandas.api.typing", "NaTType"),
    }
)


def _is_known_optional_missing_scalar(value: Any) -> bool:
    """Recognize supported missing scalars without importing pandas/numpy."""

    if value is None:
        return True
    if isinstance(value, float):
        return math.isnan(value)
    if isinstance(value, Decimal):
        return value.is_nan()
    value_type = type(value)
    return (value_type.__module__, value_type.__name__) in (
        _KNOWN_OPTIONAL_MISSING_SCALAR_TYPES
    )


def _normalized_cell(value: Any, field_name: str) -> Any:
    if _is_known_optional_missing_scalar(value):
        return None
    # Normalize common numpy scalar types without importing numpy.  The item()
    # result is still passed through this strict function.
    item = getattr(value, "item", None)
    if not isinstance(value, (str, bytes, bool, int, float, Decimal, date, datetime)) and callable(item):
        try:
            extracted = item()
        except Exception as exc:
            raise TushareUnsupportedScalarError(
                f"field {field_name} contains an unsupported scalar"
            ) from exc
        if extracted is not value:
            return _normalized_cell(extracted, field_name)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        if value.is_infinite():
            raise TusharePositiveOrNegativeInfinityError(
                f"field {field_name} contains positive or negative infinity"
            )
        return decimal_text(value, field_name)
    if isinstance(value, float):
        if math.isinf(value):
            raise TusharePositiveOrNegativeInfinityError(
                f"field {field_name} contains positive or negative infinity"
            )
        return decimal_text(value, field_name)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        if _CONTROL_RE.search(value):
            raise TushareEndpointPayloadError(
                f"field {field_name} contains control characters"
            )
        text = value.strip()
        if not text:
            return None
        if text.casefold() in {
            "nan",
            "+nan",
            "-nan",
            "inf",
            "+inf",
            "-inf",
            "infinity",
            "+infinity",
            "-infinity",
        }:
            raise TushareEndpointPayloadError(
                f"field {field_name} contains a non-finite sentinel"
            )
        return text
    raise TushareUnsupportedScalarError(
        f"field {field_name} contains unsupported value {type(value).__name__}"
    )


def _parse_result_date(value: Any, field_name: str) -> date:
    text = str(value).strip()
    try:
        if _DATE_COMPACT_RE.fullmatch(text):
            return date(int(text[:4]), int(text[4:6]), int(text[6:]))
        return date.fromisoformat(text)
    except ValueError as exc:
        raise TushareEndpointPayloadError(
            f"field {field_name} contains an invalid date"
        ) from exc


def _decimal_rate(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "0"
    value = Decimal(numerator) / Decimal(denominator)
    return decimal_text(value.quantize(Decimal("0.000001")), "null_rate")


@dataclass(frozen=True, slots=True)
class NormalizedEndpointResult:
    endpoint: Endpoint
    sanitized_parameters: Mapping[str, str]
    rows: tuple[Mapping[str, Any], ...]
    raw_payload: bytes
    field_names: tuple[str, ...]
    missing_required_fields: tuple[str, ...]
    duplicate_key_count: int
    null_rates: Mapping[str, str]
    min_date: str | None
    max_date: str | None
    sample_sha256: str
    raw_payload_sha256: str
    diagnostics: Mapping[str, Any]
    diagnostic_failure_code: str | None
    normalization_events: tuple[str, ...]
    response_shape: str = "dataframe_records"

    @property
    def row_count(self) -> int:
        return len(self.rows)


def _field_coverage(rows: Sequence[Mapping[str, Any]], field_name: str) -> str | None:
    if not rows or not any(field_name in row for row in rows):
        return None
    return _decimal_rate(
        sum(row.get(field_name) is not None for row in rows),
        len(rows),
    )


def _empty_diagnostics(kind: str = "generic") -> dict[str, Any]:
    return {
        "kind": kind,
        "candidate_primary_key_null_count": 0,
        "index_basic_candidates": [],
        "index_weight_snapshots": [],
        "industry_membership": None,
        "financial": None,
    }


def _index_basic_diagnostics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    diagnostics = _empty_diagnostics("index_basic")
    candidates: list[dict[str, Any]] = []
    for row in rows:
        name = str(row.get("name") or "").strip()
        compact_name = name.casefold().replace(" ", "")
        if not (
            "中证800" in compact_name
            or "csi800" in compact_name
        ):
            continue
        candidates.append(
            {
                "ts_code": str(row.get("ts_code") or "").strip(),
                "name": name,
                "publisher": (
                    str(row.get("publisher")).strip()
                    if row.get("publisher") is not None
                    else None
                ),
                "category": (
                    str(row.get("category")).strip()
                    if row.get("category") is not None
                    else None
                ),
                "market": (
                    str(row.get("market")).strip()
                    if row.get("market") is not None
                    else None
                ),
                "is_total_return_candidate": any(
                    marker in compact_name
                    for marker in ("全收益", "totalreturn", "tr")
                ),
            }
        )
    diagnostics["index_basic_candidates"] = sorted(
        candidates,
        key=lambda item: (str(item["ts_code"]), str(item["name"])),
    )
    return diagnostics


def _index_weight_diagnostics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    diagnostics = _empty_diagnostics("index_weight")
    by_date: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        raw_date = row.get("trade_date")
        if raw_date is None:
            continue
        rendered_date = _parse_result_date(raw_date, "trade_date").isoformat()
        by_date.setdefault(rendered_date, []).append(row)
    snapshots: list[dict[str, Any]] = []
    for rendered_date, date_rows in sorted(by_date.items()):
        components = [str(row.get("con_code") or "") for row in date_rows]
        unique_components = set(components)
        weights: list[Decimal] = []
        for row in date_rows:
            try:
                weight = Decimal(str(row.get("weight")))
            except (InvalidOperation, ValueError) as exc:
                raise TushareEndpointPayloadError(
                    "index_weight contains an invalid weight"
                ) from exc
            if not weight.is_finite():
                raise TushareEndpointPayloadError(
                    "index_weight contains a non-finite weight"
                )
            weights.append(weight)
        total = sum(weights, Decimal("0"))
        snapshots.append(
            {
                "trade_date": rendered_date,
                "unique_component_count": len(unique_components),
                "duplicate_component_count": len(components) - len(unique_components),
                "weight_sum": decimal_text(total, "weight_sum"),
                "comparison_to_100": (
                    "equal" if total == Decimal("100") else "below" if total < Decimal("100") else "above"
                ),
            }
        )
    diagnostics["index_weight_snapshots"] = snapshots
    return diagnostics


def _industry_membership_diagnostics(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    diagnostics = _empty_diagnostics("industry_membership")
    levels = [
        level
        for level, code_field, name_field in (
            ("L1", "l1_code", "l1_name"),
            ("L2", "l2_code", "l2_name"),
            ("L3", "l3_code", "l3_name"),
        )
        if any(row.get(code_field) is not None or row.get(name_field) is not None for row in rows)
    ]
    diagnostics["industry_membership"] = {
        "industry_system": "SW2021",
        "levels_present": levels,
        "in_date_coverage": _field_coverage(rows, "in_date"),
        "out_date_coverage": _field_coverage(rows, "out_date"),
        "current_member_count": sum(row.get("out_date") is None for row in rows),
        "historical_member_count": sum(row.get("out_date") is not None for row in rows),
    }
    return diagnostics


def _financial_diagnostics(
    rows: Sequence[Mapping[str, Any]],
    *,
    max_rows: int,
) -> dict[str, Any]:
    diagnostics = _empty_diagnostics("financial")
    groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    periods: list[date] = []
    for row in rows:
        ts_code = str(row.get("ts_code") or "unknown")
        end_raw = row.get("end_date")
        end_date = (
            _parse_result_date(end_raw, "end_date").isoformat()
            if end_raw is not None
            else "unknown"
        )
        if end_raw is not None:
            periods.append(_parse_result_date(end_raw, "end_date"))
        report_type = str(row.get("report_type") or "unknown")
        groups.setdefault((ts_code, end_date, report_type), []).append(row)
    version_counts = [
        {
            "ts_code": key[0],
            "end_date": key[1] if key[1] != "unknown" else None,
            "report_type": key[2],
            "version_count": len(group_rows),
        }
        for key, group_rows in sorted(groups.items())
    ]
    comp_groups: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        comp_groups.setdefault(str(row.get("comp_type") or "unknown"), []).append(row)
    comp_structures = [
        {
            "comp_type": comp_type,
            "row_count": len(group_rows),
            "field_names": sorted({field for row in group_rows for field in row}),
        }
        for comp_type, group_rows in sorted(comp_groups.items())
    ]
    multi_version_groups = [
        group_rows for group_rows in groups.values() if len(group_rows) > 1
    ]
    revision_distinguishable: bool | None = None
    if multi_version_groups:
        revision_distinguishable = all(
            len(
                {
                    (
                        row.get("ann_date"),
                        row.get("f_ann_date"),
                        row.get("update_flag"),
                    )
                    for row in group_rows
                }
            )
            == len(group_rows)
            for group_rows in multi_version_groups
        )
    diagnostics["financial"] = {
        "ann_date_coverage": _field_coverage(rows, "ann_date"),
        "f_ann_date_coverage": _field_coverage(rows, "f_ann_date"),
        "actual_date_coverage": _field_coverage(rows, "actual_date"),
        "version_counts": version_counts,
        "earliest_report_period": min(periods).isoformat() if periods else None,
        "latest_report_period": max(periods).isoformat() if periods else None,
        "comp_type_structures": comp_structures,
        "returned_row_count": len(rows),
        "configured_max_rows": max_rows,
        "at_configured_row_limit": len(rows) == max_rows,
        "revision_distinguishable": revision_distinguishable,
    }
    return diagnostics


def _build_endpoint_diagnostics(
    spec: EndpointSpec,
    rows: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], str | None]:
    if spec.endpoint is Endpoint.INDEX_BASIC:
        diagnostics = _index_basic_diagnostics(rows)
        failure = (
            "csi800_candidates_empty"
            if rows and not diagnostics["index_basic_candidates"]
            else None
        )
    elif spec.endpoint is Endpoint.INDEX_WEIGHT:
        diagnostics = _index_weight_diagnostics(rows)
        failure = None
    elif spec.endpoint is Endpoint.INDEX_MEMBER_ALL:
        diagnostics = _industry_membership_diagnostics(rows)
        failure = None
    elif spec.endpoint in {
        Endpoint.INCOME,
        Endpoint.INCOME_VIP,
        Endpoint.BALANCESHEET,
        Endpoint.BALANCESHEET_VIP,
        Endpoint.CASHFLOW,
        Endpoint.CASHFLOW_VIP,
        Endpoint.DISCLOSURE_DATE,
        Endpoint.FINA_INDICATOR,
    }:
        diagnostics = _financial_diagnostics(rows, max_rows=spec.max_rows)
        failure = None
    else:
        diagnostics = _empty_diagnostics()
        failure = None
    canonical_json_bytes(diagnostics)
    return MappingProxyType(diagnostics), failure


def normalize_endpoint_result(
    spec: EndpointSpec,
    response: Any,
    parameters: Mapping[str, Any],
) -> NormalizedEndpointResult:
    """Normalize one DataFrame-shaped SDK response into bounded evidence.

    The function never imports pandas.  A response must expose ``columns`` and
    ``to_dict(orient='records')``; strings, mappings, HTML/error pages and other
    duck types are rejected instead of being misreported as successful data.
    """

    if type(spec) is not EndpointSpec:
        raise TushareCapabilityError("spec must use the exact EndpointSpec type")
    sanitized = normalize_parameters(spec.endpoint, parameters)
    if dict(sanitized) not in [dict(item) for item in spec.parameters]:
        raise TushareCapabilityConfigError(
            "parameters are not one of the frozen calls for this endpoint"
        )
    if response is None:
        raise TushareDataFrameShapeError(
            "endpoint returned null instead of a DataFrame"
        )
    if isinstance(response, (str, bytes, bytearray, Mapping, Sequence)):
        raise TushareDataFrameShapeError(
            "endpoint returned a non-DataFrame payload"
        )
    if not hasattr(response, "columns") or not callable(getattr(response, "to_dict", None)):
        raise TushareDataFrameShapeError(
            "endpoint returned an unsupported response object"
        )
    try:
        raw_rows = response.to_dict(orient="records")
    except Exception as exc:
        raise TushareDataFrameShapeError(
            "endpoint DataFrame could not be converted to records"
        ) from exc
    if not isinstance(raw_rows, list) or any(not isinstance(row, Mapping) for row in raw_rows):
        raise TushareDataFrameShapeError(
            "endpoint DataFrame records are malformed"
        )
    if len(raw_rows) > spec.max_rows:
        raise TushareResponseRowLimitExceededError(
            "endpoint response exceeds the frozen maximum row count"
        )
    rows: list[Mapping[str, Any]] = []
    field_union: set[str] = set()
    normalization_events: set[str] = set()
    for index, raw_row in enumerate(raw_rows):
        row: dict[str, Any] = {}
        for raw_key, raw_value in raw_row.items():
            if not isinstance(raw_key, str) or _FIELD_RE.fullmatch(raw_key) is None:
                raise TushareEndpointPayloadError(
                    f"row {index} contains an invalid field name"
                )
            if raw_key in row:
                raise TushareEndpointPayloadError(
                    f"row {index} contains a duplicate field"
                )
            normalized_value = _normalized_cell(raw_value, raw_key)
            if normalized_value is None:
                normalization_events.add("missing_scalar_normalized")
            row[raw_key] = normalized_value
            field_union.add(raw_key)
        rows.append(MappingProxyType(dict(sorted(row.items()))))
    fields = tuple(sorted(field_union))
    missing_required = tuple(
        field_name
        for field_name in spec.required_fields
        if field_name not in field_union
    )
    seen_keys: set[bytes] = set()
    duplicate_count = 0
    for row in rows:
        key = tuple(row.get(field_name) for field_name in spec.candidate_primary_key)
        encoded = canonical_json_bytes(key)
        if encoded in seen_keys:
            duplicate_count += 1
        else:
            seen_keys.add(encoded)
    null_rates = MappingProxyType(
        {
            field_name: _decimal_rate(
                sum(1 for row in rows if row.get(field_name) is None),
                len(rows),
            )
            for field_name in fields
        }
    )
    parsed_dates = [
        _parse_result_date(row[field_name], field_name)
        for row in rows
        for field_name in spec.date_fields
        if row.get(field_name) is not None
    ]
    minimum = min(parsed_dates).isoformat() if parsed_dates else None
    maximum = max(parsed_dates).isoformat() if parsed_dates else None
    normalized_rows = tuple(rows)
    raw_envelope = {
        "contract_version": "tushare-capability-raw.v1",
        "endpoint": spec.endpoint.value,
        "sanitized_parameters": dict(sanitized),
        "field_names": list(fields),
        "rows": [dict(row) for row in normalized_rows],
    }
    raw_payload = canonical_json_bytes(raw_envelope)
    sample = [dict(row) for row in normalized_rows[:20]]
    diagnostics, diagnostic_failure = _build_endpoint_diagnostics(
        spec,
        normalized_rows,
    )
    primary_key_null_count = sum(
        any(row.get(field_name) is None for field_name in spec.candidate_primary_key)
        for row in normalized_rows
    )
    diagnostics_with_key_quality = dict(diagnostics)
    diagnostics_with_key_quality["candidate_primary_key_null_count"] = (
        primary_key_null_count
    )
    diagnostics = MappingProxyType(diagnostics_with_key_quality)
    if primary_key_null_count:
        diagnostic_failure = "candidate_primary_key_null"
    return NormalizedEndpointResult(
        endpoint=spec.endpoint,
        sanitized_parameters=sanitized,
        rows=normalized_rows,
        raw_payload=raw_payload,
        field_names=fields,
        missing_required_fields=missing_required,
        duplicate_key_count=duplicate_count,
        null_rates=null_rates,
        min_date=minimum,
        max_date=maximum,
        sample_sha256=canonical_sha256(sample),
        raw_payload_sha256=sha256_bytes(raw_payload),
        diagnostics=diagnostics,
        diagnostic_failure_code=diagnostic_failure,
        normalization_events=tuple(sorted(normalization_events)),
    )


class _ReplayFrame:
    """Minimal internal DataFrame shape used only for raw semantic replay."""

    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self._rows = [dict(row) for row in rows]
        self.columns = tuple(
            sorted({field for row in self._rows for field in row})
        )

    def to_dict(self, orient: str = "dict") -> list[dict[str, Any]]:
        if orient != "records":
            raise TushareCapabilityError("raw replay requires record orientation")
        return [dict(row) for row in self._rows]


def replay_endpoint_raw(
    spec: EndpointSpec,
    raw: bytes | str,
    *,
    expected_result: "EndpointResultV1 | None" = None,
) -> NormalizedEndpointResult:
    """Recompute normalized evidence and diagnostics from one canonical raw file."""

    if type(spec) is not EndpointSpec:
        raise TushareCapabilityError("spec must use the exact EndpointSpec type")
    value = strict_json_loads(
        raw,
        label="Tushare endpoint raw evidence",
        require_canonical=True,
    )
    if not isinstance(value, Mapping) or set(value) != {
        "contract_version",
        "endpoint",
        "sanitized_parameters",
        "field_names",
        "rows",
    }:
        raise TushareCapabilityError("endpoint raw evidence envelope is malformed")
    if (
        value.get("contract_version") != "tushare-capability-raw.v1"
        or value.get("endpoint") != spec.endpoint.value
    ):
        raise TushareCapabilityError("endpoint raw evidence identity is invalid")
    parameters = value.get("sanitized_parameters")
    rows = value.get("rows")
    fields = value.get("field_names")
    if not isinstance(parameters, Mapping) or not isinstance(rows, list) or any(
        not isinstance(row, Mapping) for row in rows
    ) or not isinstance(fields, list):
        raise TushareCapabilityError("endpoint raw evidence arrays are malformed")
    normalized = normalize_endpoint_result(
        spec,
        _ReplayFrame(rows),
        parameters,
    )
    encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
    if normalized.raw_payload != encoded:
        raise TushareCapabilityError(
            "endpoint raw evidence does not replay to identical canonical bytes"
        )
    if expected_result is not None:
        if type(expected_result) is not EndpointResultV1:
            raise TushareCapabilityError(
                "expected_result must use the exact EndpointResultV1 type"
            )
        rebuilt = build_endpoint_result(
            spec,
            requested_at=expected_result.requested_at,
            completed_at=expected_result.completed_at,
            sanitized_parameters=parameters,
            request_count=expected_result.request_count,
            normalized=normalized,
            notes=expected_result.notes,
        )
        if EndpointResultV1.to_dict(rebuilt) != EndpointResultV1.to_dict(
            expected_result
        ):
            raise TushareCapabilityError(
                "endpoint result diagnostics do not replay from raw evidence"
            )
    return normalized


@dataclass(frozen=True, slots=True)
class ClassifiedEndpointError:
    status: str
    permission_status: str
    error_class: str
    failure_code: str

    def __post_init__(self) -> None:
        if self.status not in ENDPOINT_STATUSES - {"passed"}:
            raise TushareCapabilityError("classified error has an invalid status")
        if self.permission_status not in PERMISSION_STATUSES:
            raise TushareCapabilityError("classified error permission status is invalid")
        if self.error_class not in ERROR_CLASSES:
            raise TushareCapabilityError("classified error class is invalid")
        _identifier(self.failure_code, "failure_code")


def classify_endpoint_error(error: Exception) -> ClassifiedEndpointError:
    """Classify an exception while discarding its original text.

    Exception text is inspected only after ``safe_error_text`` redaction and is
    never returned or stored in a receipt.  This avoids relying on downstream
    loggers to remember a second secret-scrubbing step.
    """

    if type(error) is TusharePermissionDeniedError:
        return ClassifiedEndpointError(
            "permission_denied", "denied", "permission", "permission_denied"
        )
    if type(error) is TushareRateLimitedError:
        return ClassifiedEndpointError(
            "rate_limited", "unknown", "rate_limit", "rate_limited"
        )
    if isinstance(error, DependencyMissingError) or (
        isinstance(error, ModuleNotFoundError)
        and getattr(error, "name", None) in {None, "tushare"}
    ):
        return ClassifiedEndpointError(
            "dependency_missing", "not_applicable", "dependency", "dependency_missing"
        )
    if isinstance(error, ProviderNotConfiguredError):
        return ClassifiedEndpointError(
            "not_configured", "not_configured", "configuration", "not_configured"
        )
    if isinstance(error, TushareEndpointSchemaError):
        return ClassifiedEndpointError(
            "schema_drift",
            "observed_available",
            "schema",
            error.failure_code,
        )
    if isinstance(error, TushareEndpointPayloadError):
        return ClassifiedEndpointError(
            "invalid_payload", "unknown", "payload", error.failure_code
        )
    redacted = safe_error_text(error).casefold()
    permission_markers = (
        "permission",
        "permission denied",
        "no access",
        "not authorized",
        "无权限",
        "没有访问该接口的权限",
        "没有权限访问该接口",
        "抱歉，您没有权限访问该接口",
        "积分不足",
    )
    rate_markers = (
        "rate limit",
        "too many requests",
        "每分钟",
        "访问频率",
        "频率过高",
        "限频",
    )
    if any(marker in redacted for marker in permission_markers):
        return ClassifiedEndpointError(
            "permission_denied", "denied", "permission", "permission_denied"
        )
    if any(marker in redacted for marker in rate_markers):
        return ClassifiedEndpointError(
            "rate_limited", "unknown", "rate_limit", "rate_limited"
        )
    invalid_parameter_markers = (
        "invalid parameter",
        "parameter error",
        "参数错误",
        "参数不正确",
        "请检查参数",
    )
    if any(marker in redacted for marker in invalid_parameter_markers):
        return ClassifiedEndpointError(
            "invalid_payload", "unknown", "payload", "invalid_parameter"
        )
    provider_error = classify_unexpected_error(error)
    if isinstance(provider_error, NetworkBlockedError):
        return ClassifiedEndpointError(
            "network_blocked", "unknown", "network", "network_blocked"
        )
    return ClassifiedEndpointError(
        "failed", "unknown", "unexpected", "unexpected_endpoint_failure"
    )


_DIAGNOSTIC_FIELDS = frozenset(
    {
        "kind",
        "candidate_primary_key_null_count",
        "index_basic_candidates",
        "index_weight_snapshots",
        "industry_membership",
        "financial",
    }
)


def _diagnostics_to_dict(value: Mapping[str, Any]) -> dict[str, Any]:
    replayed = strict_json_loads(canonical_json_bytes(value), label="endpoint diagnostics")
    if not isinstance(replayed, dict):
        raise TushareCapabilityError("endpoint diagnostics must be an object")
    return replayed


def _validated_diagnostics(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _DIAGNOSTIC_FIELDS:
        raise TushareCapabilityError("endpoint diagnostics fields are malformed")
    payload = _diagnostics_to_dict(value)
    if payload["kind"] not in {
        "generic",
        "index_basic",
        "index_weight",
        "industry_membership",
        "financial",
    }:
        raise TushareCapabilityError("endpoint diagnostics kind is invalid")
    null_key_count = payload["candidate_primary_key_null_count"]
    if type(null_key_count) is not int or null_key_count < 0:
        raise TushareCapabilityError(
            "candidate_primary_key_null_count must be non-negative"
        )
    candidates = payload["index_basic_candidates"]
    if not isinstance(candidates, list):
        raise TushareCapabilityError("index_basic_candidates must be an array")
    for candidate in candidates:
        if not isinstance(candidate, Mapping) or set(candidate) != {
            "ts_code",
            "name",
            "publisher",
            "category",
            "market",
            "is_total_return_candidate",
        }:
            raise TushareCapabilityError("index_basic candidate is malformed")
        if not isinstance(candidate["ts_code"], str) or not isinstance(candidate["name"], str):
            raise TushareCapabilityError("index_basic candidate identity is malformed")
        if any(
            candidate[field_name] is not None
            and not isinstance(candidate[field_name], str)
            for field_name in ("publisher", "category", "market")
        ) or type(candidate["is_total_return_candidate"]) is not bool:
            raise TushareCapabilityError("index_basic candidate metadata is malformed")
    snapshots = payload["index_weight_snapshots"]
    if not isinstance(snapshots, list):
        raise TushareCapabilityError("index_weight_snapshots must be an array")
    for snapshot in snapshots:
        if not isinstance(snapshot, Mapping) or set(snapshot) != {
            "trade_date",
            "unique_component_count",
            "duplicate_component_count",
            "weight_sum",
            "comparison_to_100",
        }:
            raise TushareCapabilityError("index_weight snapshot is malformed")
        try:
            date.fromisoformat(str(snapshot["trade_date"]))
            weight_sum = Decimal(str(snapshot["weight_sum"]))
        except (ValueError, InvalidOperation) as exc:
            raise TushareCapabilityError("index_weight snapshot values are invalid") from exc
        if not weight_sum.is_finite() or snapshot["comparison_to_100"] not in {
            "below",
            "equal",
            "above",
        }:
            raise TushareCapabilityError("index_weight snapshot weight is invalid")
        for field_name in ("unique_component_count", "duplicate_component_count"):
            if type(snapshot[field_name]) is not int or snapshot[field_name] < 0:
                raise TushareCapabilityError("index_weight snapshot count is invalid")
    industry = payload["industry_membership"]
    if industry is not None:
        if not isinstance(industry, Mapping) or set(industry) != {
            "industry_system",
            "levels_present",
            "in_date_coverage",
            "out_date_coverage",
            "current_member_count",
            "historical_member_count",
        }:
            raise TushareCapabilityError("industry membership diagnostics are malformed")
        if industry["industry_system"] != "SW2021" or not isinstance(
            industry["levels_present"], list
        ) or any(level not in {"L1", "L2", "L3"} for level in industry["levels_present"]):
            raise TushareCapabilityError("industry system/levels are invalid")
        for field_name in ("in_date_coverage", "out_date_coverage"):
            coverage = industry[field_name]
            if coverage is not None:
                parsed = Decimal(str(coverage))
                if not parsed.is_finite() or not Decimal("0") <= parsed <= Decimal("1"):
                    raise TushareCapabilityError("industry date coverage is invalid")
        for field_name in ("current_member_count", "historical_member_count"):
            if type(industry[field_name]) is not int or industry[field_name] < 0:
                raise TushareCapabilityError("industry member count is invalid")
    financial = payload["financial"]
    if financial is not None:
        if not isinstance(financial, Mapping) or set(financial) != {
            "ann_date_coverage",
            "f_ann_date_coverage",
            "actual_date_coverage",
            "version_counts",
            "earliest_report_period",
            "latest_report_period",
            "comp_type_structures",
            "returned_row_count",
            "configured_max_rows",
            "at_configured_row_limit",
            "revision_distinguishable",
        }:
            raise TushareCapabilityError("financial diagnostics are malformed")
        for field_name in (
            "ann_date_coverage",
            "f_ann_date_coverage",
            "actual_date_coverage",
        ):
            coverage = financial[field_name]
            if coverage is not None:
                parsed = Decimal(str(coverage))
                if not parsed.is_finite() or not Decimal("0") <= parsed <= Decimal("1"):
                    raise TushareCapabilityError("financial coverage is invalid")
        for field_name in ("earliest_report_period", "latest_report_period"):
            if financial[field_name] is not None:
                date.fromisoformat(str(financial[field_name]))
        versions = financial["version_counts"]
        structures = financial["comp_type_structures"]
        if not isinstance(versions, list) or not isinstance(structures, list):
            raise TushareCapabilityError("financial diagnostic arrays are malformed")
        for item in versions:
            if not isinstance(item, Mapping) or set(item) != {
                "ts_code",
                "end_date",
                "report_type",
                "version_count",
            } or type(item["version_count"]) is not int or item["version_count"] < 1:
                raise TushareCapabilityError("financial version count is malformed")
        for item in structures:
            if not isinstance(item, Mapping) or set(item) != {
                "comp_type",
                "row_count",
                "field_names",
            } or type(item["row_count"]) is not int or item["row_count"] < 1:
                raise TushareCapabilityError("financial comp_type structure is malformed")
            _fields(item["field_names"], "financial.field_names", allow_empty=False)
        for field_name in ("returned_row_count", "configured_max_rows"):
            if type(financial[field_name]) is not int or financial[field_name] < 0:
                raise TushareCapabilityError("financial row limit statistic is invalid")
        revision_flag = financial["revision_distinguishable"]
        if type(financial["at_configured_row_limit"]) is not bool or (
            revision_flag is not None and type(revision_flag) is not bool
        ):
            raise TushareCapabilityError("financial diagnostic flags are invalid")
    expected_presence = {
        "generic": (False, False, False, False),
        "index_basic": (True, False, False, False),
        "index_weight": (False, True, False, False),
        "industry_membership": (False, False, True, False),
        "financial": (False, False, False, True),
    }[payload["kind"]]
    actual_presence = (
        bool(candidates),
        bool(snapshots),
        industry is not None,
        financial is not None,
    )
    # An index candidate/snapshot array may legitimately be empty after an
    # empty result, so only reject data in irrelevant branches.
    if (
        (payload["kind"] != "index_basic" and actual_presence[0])
        or (payload["kind"] != "index_weight" and actual_presence[1])
        or (actual_presence[2] is not expected_presence[2])
        or (actual_presence[3] is not expected_presence[3])
    ):
        raise TushareCapabilityError("diagnostic branches disagree with kind")
    return MappingProxyType(payload)


_ENDPOINT_RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "endpoint",
        "status",
        "permission_status",
        "requested_at",
        "completed_at",
        "sanitized_parameters",
        "request_count",
        "row_count",
        "field_names",
        "required_fields",
        "missing_required_fields",
        "candidate_primary_key",
        "duplicate_key_count",
        "null_rates",
        "min_date",
        "max_date",
        "sample_sha256",
        "raw_payload_sha256",
        "response_shape",
        "rate_limit_or_error_class",
        "failure_code",
        "failure_stage",
        "pit_critical",
        "pit_evidence_status",
        "migration_candidate_role",
        "notes",
        "diagnostics",
    }
)


@dataclass(frozen=True, slots=True)
class EndpointResultV1:
    endpoint: Endpoint | str
    status: str
    permission_status: str
    requested_at: datetime | str
    completed_at: datetime | str
    sanitized_parameters: Mapping[str, Any]
    request_count: int
    row_count: int
    field_names: tuple[str, ...]
    required_fields: tuple[str, ...]
    missing_required_fields: tuple[str, ...]
    candidate_primary_key: tuple[str, ...]
    duplicate_key_count: int
    null_rates: Mapping[str, str]
    min_date: str | None
    max_date: str | None
    sample_sha256: str | None
    raw_payload_sha256: str | None
    response_shape: str
    rate_limit_or_error_class: str | None
    failure_code: str | None
    failure_stage: str
    pit_critical: bool
    pit_evidence_status: str
    migration_candidate_role: str
    notes: tuple[str, ...]
    diagnostics: Mapping[str, Any]
    schema_version: str = ENDPOINT_RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != ENDPOINT_RESULT_SCHEMA_VERSION:
            raise TushareCapabilityError("unsupported endpoint result schema")
        endpoint = _as_endpoint(self.endpoint)
        object.__setattr__(self, "endpoint", endpoint)
        if self.status not in ENDPOINT_STATUSES:
            raise TushareCapabilityError("endpoint result status is invalid")
        if self.permission_status not in PERMISSION_STATUSES:
            raise TushareCapabilityError("endpoint permission_status is invalid")
        requested = aware_datetime(self.requested_at, "requested_at")
        completed = aware_datetime(self.completed_at, "completed_at")
        if completed < requested:
            raise TushareCapabilityError("endpoint completed_at precedes requested_at")
        object.__setattr__(self, "requested_at", requested)
        object.__setattr__(self, "completed_at", completed)
        params = normalize_parameters(endpoint, self.sanitized_parameters)
        object.__setattr__(self, "sanitized_parameters", params)
        if type(self.request_count) is not int or self.request_count not in {0, 1}:
            raise TushareCapabilityError("endpoint request_count must be zero or one")
        if type(self.row_count) is not int or self.row_count < 0:
            raise TushareCapabilityError("endpoint row_count must be non-negative")
        if type(self.duplicate_key_count) is not int or not 0 <= self.duplicate_key_count <= self.row_count:
            raise TushareCapabilityError("duplicate_key_count is invalid")
        field_names = _fields(self.field_names, "field_names", allow_empty=True)
        required = _fields(self.required_fields, "required_fields", allow_empty=False)
        missing = _fields(
            self.missing_required_fields,
            "missing_required_fields",
            allow_empty=True,
        )
        primary = _fields(
            self.candidate_primary_key,
            "candidate_primary_key",
            allow_empty=False,
        )
        if not set(missing).issubset(required) or not set(primary).issubset(required):
            raise TushareCapabilityError(
                "missing/key fields must be subsets of required_fields"
            )
        object.__setattr__(self, "field_names", field_names)
        object.__setattr__(self, "required_fields", required)
        object.__setattr__(self, "missing_required_fields", missing)
        object.__setattr__(self, "candidate_primary_key", primary)
        if not isinstance(self.null_rates, Mapping) or set(self.null_rates) != set(field_names):
            raise TushareCapabilityError("null_rates must exactly cover field_names")
        rates: dict[str, str] = {}
        for key, value in self.null_rates.items():
            try:
                parsed = Decimal(str(value))
            except (InvalidOperation, ValueError) as exc:
                raise TushareCapabilityError("null_rates must contain decimals") from exc
            if not parsed.is_finite() or not Decimal("0") <= parsed <= Decimal("1"):
                raise TushareCapabilityError("null_rates must be between zero and one")
            rates[str(key)] = decimal_text(parsed, "null_rate")
        object.__setattr__(self, "null_rates", MappingProxyType(dict(sorted(rates.items()))))
        minimum = None
        maximum = None
        if self.min_date is not None:
            minimum = date.fromisoformat(str(self.min_date)).isoformat()
        if self.max_date is not None:
            maximum = date.fromisoformat(str(self.max_date)).isoformat()
        if (minimum is None) != (maximum is None) or (
            minimum is not None and maximum is not None and minimum > maximum
        ):
            raise TushareCapabilityError("endpoint date bounds are invalid")
        object.__setattr__(self, "min_date", minimum)
        object.__setattr__(self, "max_date", maximum)
        for field_name in ("sample_sha256", "raw_payload_sha256"):
            raw_value = getattr(self, field_name)
            object.__setattr__(
                self,
                field_name,
                _sha256(raw_value, field_name) if raw_value is not None else None,
            )
        if self.response_shape not in {"dataframe_records", "none"}:
            raise TushareCapabilityError("response_shape is invalid")
        if (
            self.rate_limit_or_error_class is not None
            and self.rate_limit_or_error_class not in ERROR_CLASSES
        ):
            raise TushareCapabilityError("rate_limit_or_error_class is invalid")
        if self.failure_code is not None:
            object.__setattr__(
                self,
                "failure_code",
                _identifier(self.failure_code, "failure_code"),
            )
        if self.failure_stage not in {
            "none",
            "pre_request_initialization",
            "endpoint_request",
            "global_stop",
        }:
            raise TushareCapabilityError("failure_stage is invalid")
        if type(self.pit_critical) is not bool:
            raise TushareCapabilityError("pit_critical must be boolean")
        if self.pit_evidence_status not in PIT_EVIDENCE_STATUSES:
            raise TushareCapabilityError("pit_evidence_status is invalid")
        if self.migration_candidate_role not in MIGRATION_CANDIDATE_ROLES:
            raise TushareCapabilityError("migration_candidate_role is invalid")
        if not isinstance(self.notes, tuple) or len(self.notes) > 20:
            raise TushareCapabilityError("notes must be a bounded array")
        safe_notes: list[str] = []
        for note in self.notes:
            safe_note = safe_error_text(note).strip()
            if not safe_note or len(safe_note) > 500 or _CONTROL_RE.search(safe_note):
                raise TushareCapabilityError("endpoint note is empty or unsafe")
            safe_notes.append(safe_note)
        object.__setattr__(self, "notes", tuple(safe_notes))
        diagnostics = _validated_diagnostics(self.diagnostics)
        object.__setattr__(self, "diagnostics", diagnostics)
        self._require_semantics()

    def _require_semantics(self) -> None:
        diagnostic_kinds = {
            Endpoint.INDEX_BASIC: "index_basic",
            Endpoint.INDEX_WEIGHT: "index_weight",
            Endpoint.INDEX_MEMBER_ALL: "industry_membership",
            Endpoint.INCOME: "financial",
            Endpoint.INCOME_VIP: "financial",
            Endpoint.BALANCESHEET: "financial",
            Endpoint.BALANCESHEET_VIP: "financial",
            Endpoint.CASHFLOW: "financial",
            Endpoint.CASHFLOW_VIP: "financial",
            Endpoint.DISCLOSURE_DATE: "financial",
            Endpoint.FINA_INDICATOR: "financial",
        }
        expected_kind = diagnostic_kinds.get(self.endpoint, "generic")
        if self.response_shape == "dataframe_records" and self.diagnostics["kind"] != expected_kind:
            raise TushareCapabilityError(
                "endpoint diagnostics kind differs from endpoint contract"
            )
        if self.response_shape == "none" and self.diagnostics["kind"] != "generic":
            raise TushareCapabilityError(
                "non-response endpoint must not carry data diagnostics"
            )
        if self.status == "passed":
            if (
                self.request_count != 1
                or self.row_count <= 0
                or self.permission_status != "observed_available"
                or self.missing_required_fields
                or self.duplicate_key_count
                or self.sample_sha256 is None
                or self.raw_payload_sha256 is None
                or self.rate_limit_or_error_class is not None
                or self.failure_code is not None
                or self.response_shape != "dataframe_records"
                or self.diagnostics["candidate_primary_key_null_count"] != 0
                or self.failure_stage != "none"
            ):
                raise TushareCapabilityError("passed endpoint result is inconsistent")
            if self.endpoint is Endpoint.INDEX_BASIC and not self.diagnostics[
                "index_basic_candidates"
            ]:
                raise TushareCapabilityError(
                    "passed index_basic result requires a CSI 800 candidate"
                )
            if self.endpoint is Endpoint.INDEX_WEIGHT and not self.diagnostics[
                "index_weight_snapshots"
            ]:
                raise TushareCapabilityError(
                    "passed index_weight result requires dated snapshots"
                )
        if self.status == "empty_result":
            if (
                self.request_count != 1
                or self.row_count != 0
                or self.permission_status != "observed_available"
                or self.failure_code != "empty_result"
                or self.rate_limit_or_error_class != "empty"
                or self.sample_sha256 is None
                or self.raw_payload_sha256 is None
                or self.failure_stage != "endpoint_request"
            ):
                raise TushareCapabilityError("empty endpoint result is inconsistent")
        if self.status == "schema_drift" and (
            self.request_count != 1
            or self.row_count <= 0
            or not self.missing_required_fields
            or self.failure_code
            not in {
                "required_fields_missing",
                "schema_required_fields_missing",
            }
            or self.rate_limit_or_error_class != "schema"
            or self.failure_stage != "endpoint_request"
        ):
            raise TushareCapabilityError("schema_drift endpoint result is inconsistent")
        if self.status == "not_run_after_global_stop" and (
            self.request_count != 0
            or self.row_count != 0
            or self.response_shape != "none"
            or self.failure_code != "global_stop"
            or self.failure_stage != "global_stop"
        ):
            raise TushareCapabilityError("global-stop endpoint result is inconsistent")
        response_statuses = {
            "passed",
            "empty_result",
            "schema_drift",
            "invalid_payload",
        }
        if self.status in response_statuses and self.request_count != 1:
            raise TushareCapabilityError(
                "response-derived endpoint status requires request_count=1"
            )
        if self.request_count == 0:
            allowed_pre_request = {
                "not_configured",
                "dependency_missing",
                "permission_denied",
                "rate_limited",
                "network_blocked",
                "failed",
            }
            if self.status == "not_run_after_global_stop":
                if self.failure_stage != "global_stop":
                    raise TushareCapabilityError(
                        "not-run endpoint must use global_stop failure_stage"
                    )
            elif self.status not in allowed_pre_request or self.failure_stage != "pre_request_initialization":
                raise TushareCapabilityError(
                    "zero request_count requires a pre-request initialization failure"
                )
        elif self.status == "not_run_after_global_stop":
            raise TushareCapabilityError(
                "not_run_after_global_stop cannot claim an endpoint request"
            )
        elif self.status != "passed" and self.failure_stage != "endpoint_request":
            raise TushareCapabilityError(
                "post-request failure must use endpoint_request failure_stage"
            )
        if self.status != "passed" and self.failure_code is None:
            raise TushareCapabilityError("non-passed endpoint result requires failure_code")
        if self.failure_code == "candidate_primary_key_null" and self.diagnostics[
            "candidate_primary_key_null_count"
        ] <= 0:
            raise TushareCapabilityError("null primary-key failure lacks diagnostics")
        if self.failure_code == "csi800_candidates_empty" and self.diagnostics[
            "index_basic_candidates"
        ]:
            raise TushareCapabilityError("empty candidate failure contradicts diagnostics")
        expected_pit = _pit_evidence_status_from_values(
            endpoint=self.endpoint,
            pit_critical=self.pit_critical,
            status=self.status,
            diagnostics=self.diagnostics,
        )
        if self.pit_evidence_status != expected_pit:
            raise TushareCapabilityError(
                "pit_evidence_status does not replay from structured coverage"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "endpoint": self.endpoint.value,  # type: ignore[union-attr]
            "status": self.status,
            "permission_status": self.permission_status,
            "requested_at": self.requested_at.isoformat(),  # type: ignore[union-attr]
            "completed_at": self.completed_at.isoformat(),  # type: ignore[union-attr]
            "sanitized_parameters": dict(self.sanitized_parameters),
            "request_count": self.request_count,
            "row_count": self.row_count,
            "field_names": list(self.field_names),
            "required_fields": list(self.required_fields),
            "missing_required_fields": list(self.missing_required_fields),
            "candidate_primary_key": list(self.candidate_primary_key),
            "duplicate_key_count": self.duplicate_key_count,
            "null_rates": dict(self.null_rates),
            "min_date": self.min_date,
            "max_date": self.max_date,
            "sample_sha256": self.sample_sha256,
            "raw_payload_sha256": self.raw_payload_sha256,
            "response_shape": self.response_shape,
            "rate_limit_or_error_class": self.rate_limit_or_error_class,
            "failure_code": self.failure_code,
            "failure_stage": self.failure_stage,
            "pit_critical": self.pit_critical,
            "pit_evidence_status": self.pit_evidence_status,
            "migration_candidate_role": self.migration_candidate_role,
            "notes": list(self.notes),
            "diagnostics": _diagnostics_to_dict(self.diagnostics),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EndpointResultV1":
        if set(value) != _ENDPOINT_RESULT_FIELDS:
            raise TushareCapabilityError("endpoint result fields differ from Schema")
        return cls(
            endpoint=str(value["endpoint"]),
            status=str(value["status"]),
            permission_status=str(value["permission_status"]),
            requested_at=value["requested_at"],
            completed_at=value["completed_at"],
            sanitized_parameters=value["sanitized_parameters"],
            request_count=value["request_count"],
            row_count=value["row_count"],
            field_names=tuple(value["field_names"]),
            required_fields=tuple(value["required_fields"]),
            missing_required_fields=tuple(value["missing_required_fields"]),
            candidate_primary_key=tuple(value["candidate_primary_key"]),
            duplicate_key_count=value["duplicate_key_count"],
            null_rates=value["null_rates"],
            min_date=value["min_date"],
            max_date=value["max_date"],
            sample_sha256=value["sample_sha256"],
            raw_payload_sha256=value["raw_payload_sha256"],
            response_shape=str(value["response_shape"]),
            rate_limit_or_error_class=value["rate_limit_or_error_class"],
            failure_code=value["failure_code"],
            failure_stage=str(value["failure_stage"]),
            pit_critical=value["pit_critical"],
            pit_evidence_status=str(value["pit_evidence_status"]),
            migration_candidate_role=str(value["migration_candidate_role"]),
            notes=tuple(value["notes"]),
            diagnostics=value["diagnostics"],
            schema_version=str(value["schema_version"]),
        )


def _derive_pit_evidence_status(
    spec: EndpointSpec,
    status: str,
    diagnostics: Mapping[str, Any],
) -> str:
    return _pit_evidence_status_from_values(
        endpoint=spec.endpoint,
        pit_critical=spec.pit_critical,
        status=status,
        diagnostics=diagnostics,
    )


def _pit_evidence_status_from_values(
    *,
    endpoint: Endpoint,
    pit_critical: bool,
    status: str,
    diagnostics: Mapping[str, Any],
) -> str:
    if not pit_critical:
        return "not_applicable"
    if status == "schema_drift":
        return "missing_pit_fields"
    if status != "passed":
        return "not_assessed"
    financial = diagnostics.get("financial")
    if isinstance(financial, Mapping):
        required_coverage = [financial.get("ann_date_coverage")]
        if endpoint in {
            Endpoint.INCOME,
            Endpoint.INCOME_VIP,
            Endpoint.BALANCESHEET,
            Endpoint.BALANCESHEET_VIP,
            Endpoint.CASHFLOW,
            Endpoint.CASHFLOW_VIP,
        }:
            required_coverage.append(financial.get("f_ann_date_coverage"))
        if endpoint is Endpoint.DISCLOSURE_DATE:
            required_coverage.append(financial.get("actual_date_coverage"))
        if any(value is None or Decimal(str(value)) < Decimal("1") for value in required_coverage):
            return "missing_pit_fields"
    industry = diagnostics.get("industry_membership")
    if isinstance(industry, Mapping) and industry.get("in_date_coverage") != "1":
        return "missing_pit_fields"
    return "candidate_fields_present_not_admitted"


def build_endpoint_result(
    spec: EndpointSpec,
    *,
    requested_at: datetime | str,
    completed_at: datetime | str,
    sanitized_parameters: Mapping[str, Any],
    request_count: int = 1,
    normalized: NormalizedEndpointResult | None = None,
    error: Exception | ClassifiedEndpointError | None = None,
    status: str | None = None,
    notes: Sequence[str] = (),
) -> EndpointResultV1:
    """Build one result for one frozen endpoint/parameter call."""

    if type(spec) is not EndpointSpec:
        raise TushareCapabilityError("spec must use the exact EndpointSpec type")
    parameters = normalize_parameters(spec.endpoint, sanitized_parameters)
    if dict(parameters) not in [dict(item) for item in spec.parameters]:
        raise TushareCapabilityConfigError(
            "endpoint result parameters are outside the frozen plan"
        )
    if normalized is not None and type(normalized) is not NormalizedEndpointResult:
        raise TushareCapabilityError(
            "normalized result must use the exact domain type"
        )
    if normalized is not None and (
        normalized.endpoint is not spec.endpoint
        or dict(normalized.sanitized_parameters) != dict(parameters)
    ):
        raise TushareCapabilityError("normalized result differs from its endpoint call")
    if normalized is not None and error is not None:
        raise TushareCapabilityError("endpoint result cannot contain data and an exception")

    classified: ClassifiedEndpointError | None = None
    if error is not None:
        classified = (
            error if type(error) is ClassifiedEndpointError else classify_endpoint_error(error)
        )
    if status is not None:
        if status not in ENDPOINT_STATUSES:
            raise TushareCapabilityError("explicit endpoint status is invalid")
        if classified is not None and status != classified.status:
            raise TushareCapabilityError(
                "explicit endpoint status differs from classified error"
            )

    if normalized is not None:
        derived_status = (
            "empty_result"
            if normalized.row_count == 0
            else "schema_drift"
            if normalized.missing_required_fields
            else "invalid_payload"
            if normalized.duplicate_key_count or normalized.diagnostic_failure_code
            else "passed"
        )
        selected_status = status or derived_status
        if selected_status != derived_status:
            raise TushareCapabilityError(
                "explicit endpoint status differs from normalized evidence"
            )
        permission = "observed_available"
        if selected_status == "passed":
            error_class = None
            failure_code = None
            failure_stage = "none"
        elif selected_status == "empty_result":
            error_class = "empty"
            failure_code = "empty_result"
            failure_stage = "endpoint_request"
        elif selected_status == "schema_drift":
            error_class = "schema"
            failure_code = "schema_required_fields_missing"
            failure_stage = "endpoint_request"
        else:
            error_class = "payload"
            failure_code = (
                normalized.diagnostic_failure_code
                or "duplicate_candidate_primary_key"
            )
            failure_stage = "endpoint_request"
        row_count = normalized.row_count
        field_names = normalized.field_names
        missing = normalized.missing_required_fields
        duplicate_count = normalized.duplicate_key_count
        null_rates = normalized.null_rates
        min_date = normalized.min_date
        max_date = normalized.max_date
        sample_hash = normalized.sample_sha256
        raw_hash = normalized.raw_payload_sha256
        response_shape = normalized.response_shape
        diagnostics = normalized.diagnostics
    else:
        if classified is None and status == "not_run_after_global_stop":
            classified = ClassifiedEndpointError(
                "not_run_after_global_stop",
                "unknown",
                "unexpected",
                "global_stop",
            )
        if classified is None:
            raise TushareCapabilityError(
                "endpoint result requires normalized evidence or a classified failure"
            )
        selected_status = classified.status
        permission = classified.permission_status
        error_class = classified.error_class
        failure_code = classified.failure_code
        row_count = 0
        field_names = ()
        missing = ()
        duplicate_count = 0
        null_rates = MappingProxyType({})
        min_date = None
        max_date = None
        sample_hash = None
        raw_hash = None
        response_shape = "none"
        diagnostics = MappingProxyType(_empty_diagnostics())
        failure_stage = (
            "global_stop"
            if selected_status == "not_run_after_global_stop"
            else "pre_request_initialization"
            if request_count == 0
            else "endpoint_request"
        )

    pit_status = _derive_pit_evidence_status(
        spec,
        selected_status,
        diagnostics,
    )
    return EndpointResultV1(
        endpoint=spec.endpoint,
        status=selected_status,
        permission_status=permission,
        requested_at=requested_at,
        completed_at=completed_at,
        sanitized_parameters=parameters,
        request_count=request_count,
        row_count=row_count,
        field_names=field_names,
        required_fields=spec.required_fields,
        missing_required_fields=missing,
        candidate_primary_key=spec.candidate_primary_key,
        duplicate_key_count=duplicate_count,
        null_rates=null_rates,
        min_date=min_date,
        max_date=max_date,
        sample_sha256=sample_hash,
        raw_payload_sha256=raw_hash,
        response_shape=response_shape,
        rate_limit_or_error_class=error_class,
        failure_code=failure_code,
        failure_stage=failure_stage,
        pit_critical=spec.pit_critical,
        pit_evidence_status=pit_status,
        migration_candidate_role=spec.migration_candidate_role,
        notes=tuple(notes),
        diagnostics=diagnostics,
    )


@dataclass(frozen=True, slots=True)
class ProbeSummary:
    planned: int
    passed: int
    failed: int
    not_run: int
    complete: bool

    def __post_init__(self) -> None:
        for field_name in ("planned", "passed", "failed", "not_run"):
            if type(getattr(self, field_name)) is not int or getattr(self, field_name) < 0:
                raise TushareCapabilityError("probe summary counts must be non-negative")
        if type(self.complete) is not bool:
            raise TushareCapabilityError("probe summary complete must be boolean")
        if self.passed + self.failed + self.not_run != self.planned:
            raise TushareCapabilityError("probe summary counts do not add to planned")
        if self.complete is not (self.not_run == 0):
            raise TushareCapabilityError("probe summary complete flag is inconsistent")

    def to_dict(self) -> dict[str, Any]:
        return {
            "planned": self.planned,
            "passed": self.passed,
            "failed": self.failed,
            "not_run": self.not_run,
            "complete": self.complete,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProbeSummary":
        if set(value) != {"planned", "passed", "failed", "not_run", "complete"}:
            raise TushareCapabilityError("probe summary is malformed")
        return cls(
            planned=value["planned"],
            passed=value["passed"],
            failed=value["failed"],
            not_run=value["not_run"],
            complete=value["complete"],
        )


_CROSS_VALIDATION_OUTCOME_FIELDS = frozenset(
    {
        "schema_version",
        "attempted",
        "status",
        "request_count",
        "dataset",
        "providers",
        "tushare_daily_endpoint_result_sha256",
        "comparison_payload_sha256",
        "tushare_raw_path",
        "tushare_raw_sha256",
        "baostock_raw_path",
        "baostock_raw_sha256",
        "failure_code",
        "not_attempted_reason",
        "independent_batches",
        "records_merged",
        "missing_values_filled_across_providers",
        "automatic_difference_threshold_configured",
        "threshold_status",
        "integrity_scope",
        "admission_effect",
    }
)


def _fixed_raw_evidence_path(
    value: Any,
    field_name: str,
    pattern: re.Pattern[str],
) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise TushareCapabilityError(f"{field_name} is outside the fixed raw path")
    return value


@dataclass(frozen=True, slots=True)
class CrossValidationOutcomeV1:
    """Typed Tushare/BaoStock comparison outcome embedded in the receipt.

    The hashes make the *current* local evidence package fail closed when a
    compared artifact is missing or relabelled.  ``integrity_scope`` is
    deliberately explicit: a local self-hash is not an external signature or
    an append-only history anchor.
    """

    attempted: bool
    status: str
    request_count: int
    tushare_daily_endpoint_result_sha256: str
    comparison_payload_sha256: str
    tushare_raw_path: str | None
    tushare_raw_sha256: str | None
    baostock_raw_path: str | None
    baostock_raw_sha256: str | None
    failure_code: str | None
    not_attempted_reason: str | None
    schema_version: str = CROSS_VALIDATION_OUTCOME_SCHEMA_VERSION
    dataset: str = "daily_bar_small_sample"
    providers: tuple[str, str] = ("tushare", "baostock")
    independent_batches: bool = True
    records_merged: bool = False
    missing_values_filled_across_providers: bool = False
    automatic_difference_threshold_configured: bool = False
    threshold_status: str = "not_configured"
    integrity_scope: str = "local_consistency_not_external_authentication"
    admission_effect: str = "none"

    def __post_init__(self) -> None:
        if self.schema_version != CROSS_VALIDATION_OUTCOME_SCHEMA_VERSION:
            raise TushareCapabilityError(
                "unsupported cross-validation outcome schema"
            )
        if type(self.attempted) is not bool:
            raise TushareCapabilityError(
                "cross-validation attempted must be boolean"
            )
        if self.status not in {"compared", "failed", "not_attempted"}:
            raise TushareCapabilityError("cross-validation status is invalid")
        if type(self.request_count) is not int or self.request_count not in {0, 1}:
            raise TushareCapabilityError(
                "cross-validation request_count must be zero or one"
            )
        if (
            type(self.dataset) is not str
            or type(self.providers) is not tuple
            or any(type(item) is not str for item in self.providers)
            or self.dataset != "daily_bar_small_sample"
            or self.providers != ("tushare", "baostock")
        ):
            raise TushareCapabilityError(
                "cross-validation dataset/providers are not frozen"
            )
        for field_name in (
            "tushare_daily_endpoint_result_sha256",
            "comparison_payload_sha256",
        ):
            if type(getattr(self, field_name)) is not str:
                raise TushareCapabilityError(
                    f"{field_name} must use the exact string type"
                )
            object.__setattr__(
                self,
                field_name,
                _sha256(getattr(self, field_name), field_name),
            )
        raw_pairs = (
            (
                "tushare_raw_path",
                "tushare_raw_sha256",
                _TUSHARE_DAILY_RAW_PATH_RE,
            ),
            (
                "baostock_raw_path",
                "baostock_raw_sha256",
                _BAOSTOCK_DAILY_RAW_PATH_RE,
            ),
        )
        for path_field, hash_field, pattern in raw_pairs:
            path_value = getattr(self, path_field)
            hash_value = getattr(self, hash_field)
            if (path_value is None) != (hash_value is None):
                raise TushareCapabilityError(
                    f"{path_field} and {hash_field} must be present together"
                )
            if path_value is not None:
                object.__setattr__(
                    self,
                    path_field,
                    _fixed_raw_evidence_path(path_value, path_field, pattern),
                )
                object.__setattr__(
                    self,
                    hash_field,
                    _sha256(hash_value, hash_field),
                )
        if self.failure_code is not None:
            if type(self.failure_code) is not str:
                raise TushareCapabilityError(
                    "failure_code must use the exact string type"
                )
            object.__setattr__(
                self,
                "failure_code",
                _identifier(self.failure_code, "failure_code"),
            )
        if self.not_attempted_reason is not None and (
            type(self.not_attempted_reason) is not str
            or self.not_attempted_reason
            not in {
                "daily_not_passed",
                "global_stop",
                "reserve_unavailable",
            }
        ):
            raise TushareCapabilityError(
                "cross-validation not_attempted_reason is invalid"
            )
        if (
            self.independent_batches is not True
            or self.records_merged is not False
            or self.missing_values_filled_across_providers is not False
            or self.automatic_difference_threshold_configured is not False
            or self.threshold_status != "not_configured"
            or self.integrity_scope
            != "local_consistency_not_external_authentication"
            or self.admission_effect != "none"
        ):
            raise TushareCapabilityError(
                "cross-validation safety semantics are not frozen"
            )
        if self.status == "compared":
            if (
                self.attempted is not True
                or self.request_count != 1
                or self.tushare_raw_path is None
                or self.baostock_raw_path is None
                or self.failure_code is not None
                or self.not_attempted_reason is not None
            ):
                raise TushareCapabilityError(
                    "compared cross-validation outcome is incomplete"
                )
        elif self.status == "failed":
            if (
                self.attempted is not True
                or self.request_count != 1
                or self.tushare_raw_path is None
                or self.failure_code is None
                or self.not_attempted_reason is not None
            ):
                raise TushareCapabilityError(
                    "failed cross-validation outcome is incomplete"
                )
        elif (
            self.attempted is not False
            or self.request_count != 0
            or self.baostock_raw_path is not None
            or self.failure_code is not None
            or self.not_attempted_reason is None
        ):
            raise TushareCapabilityError(
                "not-attempted cross-validation outcome is inconsistent"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "attempted": self.attempted,
            "status": self.status,
            "request_count": self.request_count,
            "dataset": self.dataset,
            "providers": list(self.providers),
            "tushare_daily_endpoint_result_sha256": (
                self.tushare_daily_endpoint_result_sha256
            ),
            "comparison_payload_sha256": self.comparison_payload_sha256,
            "tushare_raw_path": self.tushare_raw_path,
            "tushare_raw_sha256": self.tushare_raw_sha256,
            "baostock_raw_path": self.baostock_raw_path,
            "baostock_raw_sha256": self.baostock_raw_sha256,
            "failure_code": self.failure_code,
            "not_attempted_reason": self.not_attempted_reason,
            "independent_batches": self.independent_batches,
            "records_merged": self.records_merged,
            "missing_values_filled_across_providers": (
                self.missing_values_filled_across_providers
            ),
            "automatic_difference_threshold_configured": (
                self.automatic_difference_threshold_configured
            ),
            "threshold_status": self.threshold_status,
            "integrity_scope": self.integrity_scope,
            "admission_effect": self.admission_effect,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CrossValidationOutcomeV1":
        if set(value) != _CROSS_VALIDATION_OUTCOME_FIELDS:
            raise TushareCapabilityError(
                "cross-validation outcome fields differ from Schema"
            )
        providers = value.get("providers")
        if not isinstance(providers, list):
            raise TushareCapabilityError(
                "cross-validation providers must be an array"
            )
        return cls(
            attempted=value["attempted"],
            status=value["status"],
            request_count=value["request_count"],
            tushare_daily_endpoint_result_sha256=value[
                "tushare_daily_endpoint_result_sha256"
            ],
            comparison_payload_sha256=value["comparison_payload_sha256"],
            tushare_raw_path=value["tushare_raw_path"],
            tushare_raw_sha256=value["tushare_raw_sha256"],
            baostock_raw_path=value["baostock_raw_path"],
            baostock_raw_sha256=value["baostock_raw_sha256"],
            failure_code=value["failure_code"],
            not_attempted_reason=value["not_attempted_reason"],
            schema_version=value["schema_version"],
            dataset=value["dataset"],
            providers=tuple(providers),  # type: ignore[arg-type]
            independent_batches=value["independent_batches"],
            records_merged=value["records_merged"],
            missing_values_filled_across_providers=value[
                "missing_values_filled_across_providers"
            ],
            automatic_difference_threshold_configured=value[
                "automatic_difference_threshold_configured"
            ],
            threshold_status=value["threshold_status"],
            integrity_scope=value["integrity_scope"],
            admission_effect=value["admission_effect"],
        )


def build_cross_validation_outcome(
    endpoint_results: Sequence[EndpointResultV1],
    *,
    status: str,
    comparison_payload_sha256: str,
    tushare_raw_path: str | None = None,
    baostock_raw_path: str | None = None,
    baostock_raw_sha256: str | None = None,
    failure_code: str | None = None,
    not_attempted_reason: str | None = None,
) -> CrossValidationOutcomeV1:
    """Bind one typed cross-validation outcome to the frozen daily result."""

    results = tuple(endpoint_results)
    if not all(type(item) is EndpointResultV1 for item in results):
        raise TushareCapabilityError(
            "endpoint_results must use exact EndpointResultV1 objects"
        )
    daily_results = tuple(
        item for item in results if item.endpoint is Endpoint.DAILY
    )
    if len(daily_results) != 1:
        raise TushareCapabilityError(
            "cross-validation outcome requires exactly one daily endpoint result"
        )
    daily = daily_results[0]
    if (tushare_raw_path is None) != (daily.raw_payload_sha256 is None):
        raise TushareCapabilityError(
            "Tushare daily raw path must match daily raw evidence availability"
        )
    return CrossValidationOutcomeV1(
        attempted=status != "not_attempted",
        status=status,
        request_count=0 if status == "not_attempted" else 1,
        tushare_daily_endpoint_result_sha256=canonical_sha256(
            EndpointResultV1.to_dict(daily)
        ),
        comparison_payload_sha256=comparison_payload_sha256,
        tushare_raw_path=tushare_raw_path,
        tushare_raw_sha256=daily.raw_payload_sha256,
        baostock_raw_path=baostock_raw_path,
        baostock_raw_sha256=baostock_raw_sha256,
        failure_code=failure_code,
        not_attempted_reason=not_attempted_reason,
    )


def _verify_cross_validation_outcome_against_results(
    outcome: CrossValidationOutcomeV1,
    results: Sequence[EndpointResultV1],
) -> None:
    if type(outcome) is not CrossValidationOutcomeV1:
        raise TushareCapabilityError(
            "cross_validation_outcome must use the exact controlled type"
        )
    CrossValidationOutcomeV1.__post_init__(outcome)
    daily_results = tuple(
        item for item in results if item.endpoint is Endpoint.DAILY
    )
    if len(daily_results) != 1:
        raise TushareCapabilityError(
            "cross-validation outcome requires exactly one daily endpoint result"
        )
    daily = daily_results[0]
    if outcome.tushare_daily_endpoint_result_sha256 != canonical_sha256(
        EndpointResultV1.to_dict(daily)
    ):
        raise TushareCapabilityError(
            "cross-validation daily endpoint result hash mismatch"
        )
    if outcome.tushare_raw_sha256 != daily.raw_payload_sha256:
        raise TushareCapabilityError(
            "cross-validation Tushare raw hash differs from daily result"
        )
    if (outcome.tushare_raw_path is None) != (
        daily.raw_payload_sha256 is None
    ):
        raise TushareCapabilityError(
            "cross-validation Tushare raw path availability is inconsistent"
        )
    has_global_stop = any(
        item.status == "not_run_after_global_stop" for item in results
    )
    if outcome.status in {"compared", "failed"} and daily.status != "passed":
        raise TushareCapabilityError(
            "attempted cross-validation requires a passed Tushare daily result"
        )
    if outcome.status != "not_attempted":
        return
    if has_global_stop:
        expected_reason = "global_stop"
    elif daily.status != "passed":
        expected_reason = "daily_not_passed"
    else:
        expected_reason = "reserve_unavailable"
    if outcome.not_attempted_reason != expected_reason:
        raise TushareCapabilityError(
            "cross-validation not-attempted reason does not replay"
        )


_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "probe_run_id",
        "status",
        "scope",
        "started_at",
        "completed_at",
        "provider_id",
        "sdk_version",
        "python_version",
        "credential_status",
        "config_sha256",
        "probe_code_sha256",
        "git_commit",
        "git_worktree_status",
        "endpoint_results",
        "required_probe_summary",
        "optional_probe_summary",
        "cross_validation_outcome",
        "request_count",
        "rate_limit_events",
        "raw_evidence_manifest_sha256",
        "formal_data_admission",
        "experiment_v3_impact",
        "daily_signal_authority",
        "next_session_allowed",
        "paper_eligibility",
        "trade_eligibility",
        "real_money_list_allowed",
        "automatic_order_submission",
        "live_supported",
        "receipt_sha256",
    }
)


@dataclass(frozen=True, slots=True)
class TushareCapabilityReceiptV1:
    probe_run_id: str
    status: str
    started_at: datetime | str
    completed_at: datetime | str
    sdk_version: str | None
    python_version: str
    credential_status: str
    config_sha256: str
    probe_code_sha256: str
    git_commit: str
    git_worktree_status: str
    endpoint_results: tuple[EndpointResultV1, ...]
    required_probe_summary: ProbeSummary
    optional_probe_summary: ProbeSummary
    cross_validation_outcome: CrossValidationOutcomeV1
    request_count: int
    rate_limit_events: int
    raw_evidence_manifest_sha256: str
    schema_version: str = CAPABILITY_RECEIPT_SCHEMA_VERSION
    scope: str = CAPABILITY_SCOPE
    provider_id: str = PROVIDER_ID
    formal_data_admission: bool = FORMAL_DATA_ADMISSION
    experiment_v3_impact: str = EXPERIMENT_V3_IMPACT
    daily_signal_authority: str = DAILY_SIGNAL_AUTHORITY
    next_session_allowed: bool = NEXT_SESSION_ALLOWED
    paper_eligibility: bool = PAPER_ELIGIBILITY
    trade_eligibility: bool = TRADE_ELIGIBILITY
    real_money_list_allowed: bool = REAL_MONEY_LIST_ALLOWED
    automatic_order_submission: bool = AUTOMATIC_ORDER_SUBMISSION
    live_supported: bool = LIVE_SUPPORTED
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        self._validate_semantics()
        object.__setattr__(self, "receipt_sha256", canonical_sha256(self.to_content_dict()))

    def _validate_semantics(self) -> None:
        if self.schema_version != CAPABILITY_RECEIPT_SCHEMA_VERSION:
            raise TushareCapabilityError("unsupported capability receipt schema")
        if self.status not in RECEIPT_STATUSES:
            raise TushareCapabilityError("capability receipt status is invalid")
        if self.scope != CAPABILITY_SCOPE or self.provider_id != PROVIDER_ID:
            raise TushareCapabilityError("receipt must remain Tushare capability-only")
        if (
            self.formal_data_admission is not False
            or self.experiment_v3_impact != EXPERIMENT_V3_IMPACT
            or self.daily_signal_authority != DAILY_SIGNAL_AUTHORITY
            or self.next_session_allowed is not False
            or self.paper_eligibility is not False
            or self.trade_eligibility is not False
            or self.real_money_list_allowed is not False
            or self.automatic_order_submission is not False
            or self.live_supported is not False
        ):
            raise TushareCapabilityError(
                "capability receipt grants forbidden admission or execution authority"
            )
        object.__setattr__(self, "probe_run_id", _identifier(self.probe_run_id, "probe_run_id"))
        started = aware_datetime(self.started_at, "started_at")
        completed = aware_datetime(self.completed_at, "completed_at")
        if completed < started:
            raise TushareCapabilityError("receipt completed_at precedes started_at")
        object.__setattr__(self, "started_at", started)
        object.__setattr__(self, "completed_at", completed)
        for field_name in ("sdk_version", "python_version"):
            value = getattr(self, field_name)
            if value is None and field_name == "sdk_version":
                continue
            text = str(value).strip()
            if not text or len(text) > 128 or _CONTROL_RE.search(text):
                raise TushareCapabilityError(f"{field_name} is empty or unsafe")
            object.__setattr__(self, field_name, text)
        if self.credential_status not in {"configured", "not_configured"}:
            raise TushareCapabilityError("credential_status is invalid")
        for field_name in (
            "config_sha256",
            "probe_code_sha256",
            "raw_evidence_manifest_sha256",
        ):
            object.__setattr__(self, field_name, _sha256(getattr(self, field_name), field_name))
        commit = str(self.git_commit).strip()
        if commit != "unknown" and _GIT_COMMIT_RE.fullmatch(commit) is None:
            raise TushareCapabilityError("git_commit must be a full lowercase commit or unknown")
        object.__setattr__(self, "git_commit", commit)
        if self.git_worktree_status not in {"clean", "dirty", "unknown"}:
            raise TushareCapabilityError("git_worktree_status is invalid")
        if not isinstance(self.endpoint_results, tuple) or not all(
            type(item) is EndpointResultV1 for item in self.endpoint_results
        ):
            raise TushareCapabilityError(
                "endpoint_results must use the exact EndpointResultV1 type"
            )
        identities = [
            (item.endpoint, canonical_sha256(dict(item.sanitized_parameters)))
            for item in self.endpoint_results
        ]
        if len(identities) != len(set(identities)):
            raise TushareCapabilityError(
                "receipt contains duplicate endpoint/parameter results"
            )
        _verify_cross_validation_outcome_against_results(
            self.cross_validation_outcome,
            self.endpoint_results,
        )
        if type(self.required_probe_summary) is not ProbeSummary or type(self.optional_probe_summary) is not ProbeSummary:
            raise TushareCapabilityError("receipt summaries must use exact ProbeSummary types")
        if type(self.request_count) is not int or self.request_count < 0:
            raise TushareCapabilityError("request_count must be non-negative")
        initialization_rate_limit = any(
            item.status == "rate_limited"
            and item.failure_stage == "pre_request_initialization"
            for item in self.endpoint_results
        )
        endpoint_rate_limit_events = sum(
            item.status == "rate_limited"
            and item.failure_stage == "endpoint_request"
            for item in self.endpoint_results
        )
        expected_rate_limit_events = endpoint_rate_limit_events + int(
            initialization_rate_limit
        )
        if (
            type(self.rate_limit_events) is not int
            or self.rate_limit_events != expected_rate_limit_events
        ):
            raise TushareCapabilityError("rate_limit_events is invalid")
        expected_request_count = sum(
            item.request_count for item in self.endpoint_results
        ) + self.cross_validation_outcome.request_count
        if self.request_count != expected_request_count:
            raise TushareCapabilityError(
                "top-level request_count does not equal endpoint plus cross-validation requests"
            )

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "probe_run_id": self.probe_run_id,
            "status": self.status,
            "scope": self.scope,
            "started_at": self.started_at.isoformat(),  # type: ignore[union-attr]
            "completed_at": self.completed_at.isoformat(),  # type: ignore[union-attr]
            "provider_id": self.provider_id,
            "sdk_version": self.sdk_version,
            "python_version": self.python_version,
            "credential_status": self.credential_status,
            "config_sha256": self.config_sha256,
            "probe_code_sha256": self.probe_code_sha256,
            "git_commit": self.git_commit,
            "git_worktree_status": self.git_worktree_status,
            "endpoint_results": [
                EndpointResultV1.to_dict(item) for item in self.endpoint_results
            ],
            "required_probe_summary": self.required_probe_summary.to_dict(),
            "optional_probe_summary": self.optional_probe_summary.to_dict(),
            "cross_validation_outcome": CrossValidationOutcomeV1.to_dict(
                self.cross_validation_outcome
            ),
            "request_count": self.request_count,
            "rate_limit_events": self.rate_limit_events,
            "raw_evidence_manifest_sha256": self.raw_evidence_manifest_sha256,
            "formal_data_admission": self.formal_data_admission,
            "experiment_v3_impact": self.experiment_v3_impact,
            "daily_signal_authority": self.daily_signal_authority,
            "next_session_allowed": self.next_session_allowed,
            "paper_eligibility": self.paper_eligibility,
            "trade_eligibility": self.trade_eligibility,
            "real_money_list_allowed": self.real_money_list_allowed,
            "automatic_order_submission": self.automatic_order_submission,
            "live_supported": self.live_supported,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.to_content_dict()
        payload["receipt_sha256"] = self.receipt_sha256
        return payload

    def require_valid(
        self,
        *,
        config: ProbeConfig | None = None,
        as_of: datetime | str | None = None,
    ) -> "TushareCapabilityReceiptV1":
        if type(self) is not TushareCapabilityReceiptV1:
            raise TushareCapabilityError(
                "capability receipt must use the exact controlled contract type"
            )
        TushareCapabilityReceiptV1._validate_semantics(self)
        if canonical_sha256(TushareCapabilityReceiptV1.to_content_dict(self)) != self.receipt_sha256:
            raise TushareCapabilityError("capability receipt hash mismatch")
        if as_of is not None and self.completed_at > aware_datetime(as_of, "as_of"):
            raise TushareCapabilityError("capability receipt is future-dated")
        if config is not None:
            _verify_receipt_against_config(self, config)
        return self

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TushareCapabilityReceiptV1":
        if set(value) != _RECEIPT_FIELDS:
            raise TushareCapabilityError("capability receipt fields differ from Schema")
        raw_results = value.get("endpoint_results")
        if not isinstance(raw_results, list) or any(
            not isinstance(item, Mapping) for item in raw_results
        ):
            raise TushareCapabilityError("endpoint_results must be an object array")
        required = value.get("required_probe_summary")
        optional = value.get("optional_probe_summary")
        cross_validation = value.get("cross_validation_outcome")
        if (
            not isinstance(required, Mapping)
            or not isinstance(optional, Mapping)
            or not isinstance(cross_validation, Mapping)
        ):
            raise TushareCapabilityError("receipt summaries must be objects")
        receipt = cls(
            probe_run_id=str(value["probe_run_id"]),
            status=str(value["status"]),
            started_at=value["started_at"],
            completed_at=value["completed_at"],
            sdk_version=value["sdk_version"],
            python_version=str(value["python_version"]),
            credential_status=str(value["credential_status"]),
            config_sha256=str(value["config_sha256"]),
            probe_code_sha256=str(value["probe_code_sha256"]),
            git_commit=str(value["git_commit"]),
            git_worktree_status=str(value["git_worktree_status"]),
            endpoint_results=tuple(
                EndpointResultV1.from_dict(item) for item in raw_results
            ),
            required_probe_summary=ProbeSummary.from_dict(required),
            optional_probe_summary=ProbeSummary.from_dict(optional),
            cross_validation_outcome=CrossValidationOutcomeV1.from_dict(
                cross_validation
            ),
            request_count=value["request_count"],
            rate_limit_events=value["rate_limit_events"],
            raw_evidence_manifest_sha256=str(value["raw_evidence_manifest_sha256"]),
            schema_version=str(value["schema_version"]),
            scope=str(value["scope"]),
            provider_id=str(value["provider_id"]),
            formal_data_admission=value["formal_data_admission"],
            experiment_v3_impact=str(value["experiment_v3_impact"]),
            daily_signal_authority=str(value["daily_signal_authority"]),
            next_session_allowed=value["next_session_allowed"],
            paper_eligibility=value["paper_eligibility"],
            trade_eligibility=value["trade_eligibility"],
            real_money_list_allowed=value["real_money_list_allowed"],
            automatic_order_submission=value["automatic_order_submission"],
            live_supported=value["live_supported"],
        )
        if receipt.receipt_sha256 != value.get("receipt_sha256"):
            raise TushareCapabilityError("capability receipt hash mismatch")
        return receipt


def _summary(results: Sequence[EndpointResultV1]) -> ProbeSummary:
    not_run = sum(item.status == "not_run_after_global_stop" for item in results)
    passed = sum(item.status == "passed" for item in results)
    return ProbeSummary(
        planned=len(results),
        passed=passed,
        failed=len(results) - passed - not_run,
        not_run=not_run,
        complete=not_run == 0,
    )


def _derive_receipt_status(
    credential_status: str,
    results: Sequence[EndpointResultV1],
    cross_validation_outcome: CrossValidationOutcomeV1,
) -> str:
    if credential_status == "not_configured":
        return "not_configured"
    if not results or any(item.status == "not_run_after_global_stop" for item in results):
        return "incomplete"
    if all(item.status == "passed" for item in results):
        if cross_validation_outcome.status == "compared":
            return "passed"
        if cross_validation_outcome.status == "failed":
            return "partial"
        return "incomplete"
    if all(item.status == "dependency_missing" for item in results):
        return "dependency_missing"
    if any(item.status == "passed" for item in results):
        return "partial"
    return "failed"


def _verify_receipt_against_config(
    receipt: TushareCapabilityReceiptV1,
    config: ProbeConfig,
) -> None:
    if type(config) is not ProbeConfig:
        raise TushareCapabilityError("config must use the exact ProbeConfig type")
    if receipt.config_sha256 != config.config_sha256:
        raise TushareCapabilityError("receipt config_sha256 mismatch")
    planned = config.planned_calls()
    if len(receipt.endpoint_results) != len(planned):
        raise TushareCapabilityError(
            "receipt must contain one result for every frozen endpoint call"
        )
    for index, ((spec, parameters), result) in enumerate(
        zip(planned, receipt.endpoint_results, strict=True)
    ):
        if result.endpoint is not spec.endpoint or dict(result.sanitized_parameters) != dict(parameters):
            raise TushareCapabilityError(
                f"endpoint_results[{index}] differs from frozen call order"
            )
        if (
            result.required_fields != spec.required_fields
            or result.candidate_primary_key != spec.candidate_primary_key
            or result.pit_critical is not spec.pit_critical
            or result.migration_candidate_role != spec.migration_candidate_role
        ):
            raise TushareCapabilityError(
                f"endpoint_results[{index}] metadata differs from config"
            )
    required = tuple(
        result
        for (spec, _), result in zip(planned, receipt.endpoint_results, strict=True)
        if spec.required_probe
    )
    optional = tuple(
        result
        for (spec, _), result in zip(planned, receipt.endpoint_results, strict=True)
        if not spec.required_probe
    )
    if receipt.required_probe_summary != _summary(required) or receipt.optional_probe_summary != _summary(optional):
        raise TushareCapabilityError("receipt probe summaries do not replay")
    endpoint_requests = sum(item.request_count for item in receipt.endpoint_results)
    if receipt.cross_validation_outcome.request_count > config.cross_validation_request_reserve:
        raise TushareCapabilityError(
            "cross-validation request_count exceeds the frozen reserve"
        )
    if (
        receipt.cross_validation_outcome.not_attempted_reason
        == "reserve_unavailable"
        and config.cross_validation_request_reserve >= 1
        and endpoint_requests < config.maximum_request_count
    ):
        raise TushareCapabilityError(
            "cross-validation reserve was available but reported unavailable"
        )
    if receipt.request_count > config.maximum_request_count:
        raise TushareCapabilityError("receipt request_count exceeds configured maximum")
    expected_status = _derive_receipt_status(
        receipt.credential_status,
        receipt.endpoint_results,
        receipt.cross_validation_outcome,
    )
    if receipt.status != expected_status:
        raise TushareCapabilityError("receipt status does not replay from endpoint results")


def build_capability_receipt(
    config: ProbeConfig,
    *,
    probe_run_id: str,
    started_at: datetime | str,
    completed_at: datetime | str,
    sdk_version: str | None,
    python_version: str,
    credential_status: str,
    probe_code_sha256: str,
    git_commit: str,
    git_worktree_status: str,
    endpoint_results: Sequence[EndpointResultV1],
    cross_validation_outcome: CrossValidationOutcomeV1,
    raw_evidence_manifest_sha256: str,
    request_count: int | None = None,
    rate_limit_events: int | None = None,
    status: str | None = None,
) -> TushareCapabilityReceiptV1:
    if type(config) is not ProbeConfig:
        raise TushareCapabilityError("config must use the exact ProbeConfig type")
    results = tuple(endpoint_results)
    if not all(type(item) is EndpointResultV1 for item in results):
        raise TushareCapabilityError(
            "endpoint_results must use exact EndpointResultV1 objects"
        )
    if type(cross_validation_outcome) is not CrossValidationOutcomeV1:
        raise TushareCapabilityError(
            "cross_validation_outcome must use exact CrossValidationOutcomeV1"
        )
    planned = config.planned_calls()
    required = tuple(
        result
        for (spec, _), result in zip(planned, results, strict=False)
        if spec.required_probe
    )
    optional = tuple(
        result
        for (spec, _), result in zip(planned, results, strict=False)
        if not spec.required_probe
    )
    selected_status = status or _derive_receipt_status(
        credential_status,
        results,
        cross_validation_outcome,
    )
    selected_request_count = (
        sum(item.request_count for item in results)
        + cross_validation_outcome.request_count
        if request_count is None
        else request_count
    )
    selected_rate_events = (
        sum(
            item.status == "rate_limited"
            and item.failure_stage == "endpoint_request"
            for item in results
        )
        + int(
            any(
                item.status == "rate_limited"
                and item.failure_stage == "pre_request_initialization"
                for item in results
            )
        )
        if rate_limit_events is None
        else rate_limit_events
    )
    receipt = TushareCapabilityReceiptV1(
        probe_run_id=probe_run_id,
        status=selected_status,
        started_at=started_at,
        completed_at=completed_at,
        sdk_version=sdk_version,
        python_version=python_version,
        credential_status=credential_status,
        config_sha256=config.config_sha256,
        probe_code_sha256=probe_code_sha256,
        git_commit=git_commit,
        git_worktree_status=git_worktree_status,
        endpoint_results=results,
        required_probe_summary=_summary(required),
        optional_probe_summary=_summary(optional),
        cross_validation_outcome=cross_validation_outcome,
        request_count=selected_request_count,
        rate_limit_events=selected_rate_events,
        raw_evidence_manifest_sha256=raw_evidence_manifest_sha256,
    )
    return verify_capability_receipt(receipt, config=config)


def replay_capability_receipt(
    raw: bytes | str,
    *,
    config: ProbeConfig | None = None,
    as_of: datetime | str | None = None,
) -> TushareCapabilityReceiptV1:
    value = strict_json_loads(
        raw,
        label="Tushare capability receipt",
        require_canonical=True,
    )
    if not isinstance(value, Mapping):
        raise TushareCapabilityError("capability receipt root must be an object")
    try:
        validate_json_schema(value, _RECEIPT_SCHEMA_PATH)
    except SchemaValidationError as exc:
        raise TushareCapabilityError(f"capability receipt Schema failed: {exc}") from exc
    receipt = TushareCapabilityReceiptV1.from_dict(value)
    if canonical_json_bytes(TushareCapabilityReceiptV1.to_dict(receipt)) != (
        raw.encode("utf-8") if isinstance(raw, str) else raw
    ):
        raise TushareCapabilityError("capability receipt semantic replay differs")
    return verify_capability_receipt(receipt, config=config, as_of=as_of)


def verify_capability_receipt(
    receipt: TushareCapabilityReceiptV1 | Mapping[str, Any] | bytes | str,
    *,
    config: ProbeConfig | None = None,
    as_of: datetime | str | None = None,
) -> TushareCapabilityReceiptV1:
    """Verify canonical/schema/domain/hash/config semantics without dispatch."""

    if isinstance(receipt, (bytes, str)):
        return replay_capability_receipt(receipt, config=config, as_of=as_of)
    if isinstance(receipt, Mapping):
        try:
            validate_json_schema(receipt, _RECEIPT_SCHEMA_PATH)
        except SchemaValidationError as exc:
            raise TushareCapabilityError(
                f"capability receipt Schema failed: {exc}"
            ) from exc
        controlled = TushareCapabilityReceiptV1.from_dict(receipt)
    else:
        if type(receipt) is not TushareCapabilityReceiptV1:
            raise TushareCapabilityError(
                "capability receipt must use the exact controlled contract type"
            )
        controlled = receipt
    return TushareCapabilityReceiptV1.require_valid(
        controlled,
        config=config,
        as_of=as_of,
    )


def validate_endpoint_result_schema(result: EndpointResultV1 | Mapping[str, Any]) -> None:
    payload = EndpointResultV1.to_dict(result) if type(result) is EndpointResultV1 else result
    try:
        validate_json_schema(payload, _ENDPOINT_SCHEMA_PATH)
    except SchemaValidationError as exc:
        raise TushareCapabilityError(f"endpoint result Schema failed: {exc}") from exc


__all__ = [
    "ALLOWED_PARAMETER_KEYS",
    "AUTOMATIC_ORDER_SUBMISSION",
    "CAPABILITY_RECEIPT_SCHEMA_VERSION",
    "CAPABILITY_SCOPE",
    "CONFIG_SCHEMA_VERSION",
    "CROSS_VALIDATION_OUTCOME_SCHEMA_VERSION",
    "ClassifiedEndpointError",
    "CrossValidationOutcomeV1",
    "DAILY_SIGNAL_AUTHORITY",
    "ENDPOINT_ORDER",
    "ENDPOINT_RESULT_SCHEMA_VERSION",
    "ENDPOINT_STATUSES",
    "Endpoint",
    "EndpointResultV1",
    "EndpointSpec",
    "FORMAL_DATA_ADMISSION",
    "LIVE_SUPPORTED",
    "MIGRATION_CANDIDATE_ROLES",
    "NEXT_SESSION_ALLOWED",
    "NormalizedEndpointResult",
    "PAPER_ELIGIBILITY",
    "ProbeConfig",
    "ProbeSummary",
    "REAL_MONEY_LIST_ALLOWED",
    "SDK_METHOD_BY_ENDPOINT",
    "TRADE_ELIGIBILITY",
    "TushareCapabilityConfigError",
    "TushareCapabilityError",
    "TushareCapabilityReceiptV1",
    "TushareDataFrameShapeError",
    "TushareEndpointPayloadError",
    "TushareEndpointSchemaError",
    "TusharePositiveOrNegativeInfinityError",
    "TusharePermissionDeniedError",
    "TushareRateLimitedError",
    "TushareResponseRowLimitExceededError",
    "TushareUnsupportedScalarError",
    "build_capability_receipt",
    "build_cross_validation_outcome",
    "build_endpoint_result",
    "canonical_json_bytes",
    "canonical_sha256",
    "classify_endpoint_error",
    "load_probe_config",
    "normalize_endpoint_result",
    "normalize_parameters",
    "requested_fields_for",
    "replay_capability_receipt",
    "replay_endpoint_raw",
    "strict_json_loads",
    "validate_endpoint_result_schema",
    "verify_capability_receipt",
]
