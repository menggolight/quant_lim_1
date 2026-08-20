"""Provider protocol and structured failure taxonomy."""

from __future__ import annotations

import os
import re
import socket
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Protocol

from ..contracts import MarketDataRequest, aware_datetime


_SECRET_ENV_NAME = re.compile(
    r"(?:TOKEN|SECRET|PASSWORD|PASSWD|COOKIE|AUTHORIZATION|CREDENTIAL|"
    r"API[_-]?KEY|ACCESS[_-]?KEY|PHONE(?:[_-]?NUMBER)?|MOBILE(?:[_-]?NUMBER)?|"
    r"ACCOUNT(?:[_-]?(?:ID|NO|NUMBER))?|SHAREHOLDER(?:[_-]?ACCOUNT)?|"
    r"USER[_-]?(?:INFO|NAME)|VERIFICATION[_-]?CODE|VERIFY[_-]?CODE|SMS[_-]?CODE|OTP|"
    r"IDENTITY[_-]?(?:ID|NO|NUMBER)|ID[_-]?(?:NO|NUMBER))",
    re.IGNORECASE,
)
_SECRET_KEY_PATTERN = (
    r"[A-Za-z0-9_-]*(?:token|secret|password|passwd|cookie|authorization|"
    r"credential|api[_-]?key|access[_-]?key|phone(?:[_-]?number)?|"
    r"mobile(?:[_-]?number)?|account(?:[_-]?(?:id|no|number))?|"
    r"shareholder(?:[_-]?account)?|user[_-]?(?:info|name)|verification[_-]?code|"
    r"verify[_-]?code|sms[_-]?code|otp|identity[_-]?(?:id|no|number)|"
    r"id[_-]?(?:no|number))"
)
_QUOTED_NAMED_SECRET = re.compile(
    rf"(?i)(\b{_SECRET_KEY_PATTERN}(?:[\"'])?\s*[:=]\s*)([\"'])(.*?)\2"
)
_NAMED_SECRET = re.compile(
    rf"(?i)(\b{_SECRET_KEY_PATTERN}(?:[\"'])?\s*[:=]\s*)([^\s,;}}\]\"']+)"
)
_BEARER_SECRET = re.compile(r"(?i)(\bbearer\s+)([^\s,;}}\]\"']+)")
_TOKEN_IS_SECRET = re.compile(
    r"(?i)(\btoken\b\s+is\s+)(?!not\b|missing\b|required\b|unavailable\b|expired\b|invalid\b)([^\s,;}}\]\"']+)"
)
_TRAILING_TOKEN = re.compile(
    r"(?i)(\btoken\b\s+)(?!is\b|not\b|missing\b|required\b|unavailable\b|expired\b|invalid\b)([^\s,;}}\]\"']+)"
)
_CHINA_MOBILE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_CHINA_ID_NUMBER = re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)")


def safe_error_text(value: object) -> str:
    """Redact credential-shaped values before errors enter logs or evidence."""

    text = str(value)
    secrets = {
        secret
        for name, secret in os.environ.items()
        if _SECRET_ENV_NAME.search(name) and len(secret) >= 4
    }
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = _BEARER_SECRET.sub(r"\1[REDACTED]", text)
    text = _QUOTED_NAMED_SECRET.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]{match.group(2)}",
        text,
    )
    text = _NAMED_SECRET.sub(r"\1[REDACTED]", text)
    text = _TOKEN_IS_SECRET.sub(r"\1[REDACTED]", text)
    text = _TRAILING_TOKEN.sub(r"\1[REDACTED]", text)
    text = _CHINA_MOBILE.sub("[REDACTED]", text)
    return _CHINA_ID_NUMBER.sub("[REDACTED]", text)


def redact_sensitive_value(value: Any) -> Any:
    """Recursively redact strings in structured diagnostic evidence."""

    if isinstance(value, str):
        return safe_error_text(value)
    if isinstance(value, Mapping):
        return {
            str(key): (
                "[REDACTED]"
                if _SECRET_ENV_NAME.search(str(key))
                else redact_sensitive_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(redact_sensitive_value(item) for item in value)
    if isinstance(value, list):
        return [redact_sensitive_value(item) for item in value]
    return value


class ProviderError(RuntimeError):
    status = "failed"
    code = "provider_failed"

    def __init__(self, message: str, *, raw_content: bytes = b"") -> None:
        super().__init__(safe_error_text(message))
        self.raw_content = raw_content


class DependencyMissingError(ProviderError):
    status = "dependency_missing"
    code = "dependency_missing"


class NetworkBlockedError(ProviderError):
    status = "network_blocked"
    code = "network_blocked"


class ProviderNotConfiguredError(ProviderError):
    status = "not_configured"
    code = "not_configured"


class ProviderDisabledError(ProviderError):
    code = "provider_disabled"


class UnknownProviderError(ProviderError):
    code = "unknown_provider"


class UnsupportedDatasetError(ProviderError):
    code = "unsupported_dataset"


class ProviderQueryError(ProviderError):
    code = "provider_query_failed"


class ProviderQuotaExceededError(ProviderQueryError):
    """The upstream account's read quota is exhausted for this capture."""

    code = "quota_exhausted"


class EmptyDatasetError(ProviderError):
    code = "empty_dataset"


class NoTradingDaysError(EmptyDatasetError):
    code = "no_trading_days"


class IncompleteDatasetError(ProviderQueryError):
    code = "incomplete_dataset"


class BatchValidationError(ProviderError):
    code = "batch_validation_failed"


class AllProvidersFailedError(ProviderError):
    code = "all_providers_failed"

    def __init__(self, attempts: tuple[Mapping[str, Any], ...]) -> None:
        super().__init__("all configured providers failed; no default or synthetic data returned")
        self.attempts = attempts


@dataclass(frozen=True)
class ProviderPayload:
    raw_content: bytes
    records: tuple[Mapping[str, Any], ...]
    fetched_at: datetime
    upstream_source: str
    issues: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.raw_content, bytes):
            raise TypeError("provider raw_content must be bytes")
        if not isinstance(self.records, tuple) or not all(
            isinstance(item, Mapping) for item in self.records
        ):
            raise TypeError("provider records must be a tuple of objects")
        object.__setattr__(self, "records", tuple(dict(item) for item in self.records))
        object.__setattr__(
            self,
            "fetched_at",
            aware_datetime(self.fetched_at, "provider.fetched_at"),
        )
        upstream = str(self.upstream_source).strip()
        if not upstream:
            raise ValueError("provider upstream_source must not be empty")
        object.__setattr__(self, "upstream_source", upstream)
        if not isinstance(self.issues, tuple) or not all(
            isinstance(item, Mapping) for item in self.issues
        ):
            raise TypeError("provider issues must be a tuple of objects")
        object.__setattr__(self, "issues", tuple(dict(item) for item in self.issues))


class MarketDataProvider(Protocol):
    provider_id: str
    upstream_source: str
    adapter_version: str
    supported_datasets: frozenset[str]

    def fetch(self, request: MarketDataRequest) -> ProviderPayload: ...


def classify_unexpected_error(error: Exception) -> ProviderError:
    if isinstance(error, ProviderError):
        return error
    message = safe_error_text(error)
    lowered = message.casefold()
    network_markers = (
        "network",
        "connection",
        "timed out",
        "timeout",
        "socket",
        "dns",
        "name resolution",
        "temporary failure in name resolution",
        "getaddrinfo failed",
    )
    network_exception = isinstance(
        error,
        (
            ConnectionError,
            TimeoutError,
            socket.gaierror,
            socket.timeout,
            urllib.error.URLError,
        ),
    )
    network_library = type(error).__module__.split(".", 1)[0] in {
        "aiohttp",
        "httpx",
        "requests",
        "urllib3",
    }
    # A generic OSError usually describes a local filesystem/device failure.
    # Do not relabel those failures as network_blocked merely because both
    # families share the OSError base class.
    if network_exception or (
        network_library and any(marker in lowered for marker in network_markers)
    ):
        return NetworkBlockedError(f"{type(error).__name__}: {message}")
    return ProviderQueryError(f"{type(error).__name__}: {message}")
