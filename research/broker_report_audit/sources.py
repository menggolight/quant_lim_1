"""Network, report and market-data adapters used by the audit pipeline.

Only public HTTP endpoints are used.  The HTTP client is standard-library
based, rate limited, retryable and backed by an immutable content-addressed
cache so the same run can be replayed with ``offline=True``.
"""

from __future__ import annotations

import csv
import html
import http.client
import io
import json
import random
import re
import socket
import ssl
import time
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Collection, Iterator, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPHandler, HTTPSHandler, Request, build_opener, urlopen

from .models import (
    CHINA_TZ,
    DailyBar,
    ModelValidationError,
    ResearchReport,
    TruthObservation,
    decimal_or_none,
    ensure_aware,
    parse_datetime,
    stable_identifier,
)
from .storage import ContentAddressedHttpCache


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 broker-report-audit/1.0"
)

# Exact public-data hosts that have repeatedly selected an unusable IPv6 CDN
# path in the supported Windows environment.  This is deliberately not a
# wildcard: transport policy for an unrelated Eastmoney host must be an
# explicit code change and test decision.
EASTMONEY_IPV4_ONLY_HOSTS = frozenset(
    {
        "datacenter-web.eastmoney.com",
        "pdf.dfcfw.com",
        "17.push2.eastmoney.com",
        "push2.eastmoney.com",
        "push2his.eastmoney.com",
        "reportapi.eastmoney.com",
    }
)


class SourceError(RuntimeError):
    """Base exception for public source adapters."""


class TruthImportError(SourceError):
    """Raised when a local official-truth file violates the strict contract."""


class OfflineCacheMiss(SourceError):
    """Raised when offline mode cannot satisfy a request from cache."""


class HttpStatusError(SourceError):
    """Raised for a final non-successful HTTP response."""

    def __init__(self, status: int, url: str) -> None:
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.url = url


class MalformedResponseError(SourceError):
    """Raised when a source response does not satisfy its documented shape."""


class IncompleteSourceBatchError(SourceError):
    """Raised when a paginated source cannot prove that the batch is complete."""


class UnsupportedInstrumentError(SourceError):
    """Raised when an instrument cannot be mapped to an Eastmoney secid."""


@dataclass(frozen=True)
class HttpResponse:
    url: str
    status: int
    headers: Mapping[str, str]
    body: bytes
    fetched_at: datetime
    content_hash: str
    from_cache: bool

    def __post_init__(self) -> None:
        ensure_aware(self.fetched_at, "fetched_at")
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))

    def text(self, encoding: str | None = None) -> str:
        resolved = encoding or _response_encoding(self.headers) or "utf-8-sig"
        try:
            return self.body.decode(resolved)
        except (LookupError, UnicodeDecodeError) as exc:
            raise MalformedResponseError(
                f"cannot decode response from {self.url} as {resolved}"
            ) from exc

    def json(self) -> Any:
        payload = self.text().strip()
        if not payload:
            raise MalformedResponseError(f"empty JSON response from {self.url}")
        # Eastmoney sometimes wraps JSON in a callback even when the current
        # public endpoint normally returns plain JSON.
        if payload[0] not in "[{":
            callback = re.fullmatch(
                r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\s*\((.*)\)\s*;?",
                payload,
                flags=re.DOTALL,
            )
            if callback is None:
                raise MalformedResponseError(f"invalid JSON wrapper from {self.url}")
            payload = callback.group(1).strip()
            if not payload or payload[0] not in "[{":
                raise MalformedResponseError(f"invalid JSON callback body from {self.url}")

        def reject_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate JSON key: {key}")
                result[key] = value
            return result

        def reject_non_finite(value: str) -> Any:
            raise ValueError(f"non-finite JSON number: {value}")

        try:
            return json.loads(
                payload,
                object_pairs_hook=reject_duplicate_keys,
                parse_constant=reject_non_finite,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise MalformedResponseError(f"invalid JSON from {self.url}") from exc


_TRUTH_IMPORT_FIELDS = frozenset(
    {
        "claim_id",
        "dimension",
        "subject_id",
        "target_type",
        "forecast_period",
        "unit",
        "basis",
        "change_value",
        "change_basis",
        "realized_value",
        "truth_source",
        "available_at",
        "fetched_at",
        "first_release",
        "revision",
        "content_hash",
        "evidence_url",
        "evidence_path",
    }
)
_TRUTH_REQUIRED_FIELDS = frozenset(
    {
        "realized_value",
        "unit",
        "basis",
        "truth_source",
        "available_at",
        "fetched_at",
        "first_release",
        "revision",
        "content_hash",
        "evidence_url",
        "evidence_path",
    }
)


def _strict_truth_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise TruthImportError(f"{field} must be a non-empty ISO-8601 string")
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise TruthImportError(f"{field} is not a valid ISO-8601 timestamp") from exc
    try:
        return ensure_aware(parsed, field)
    except ModelValidationError as exc:
        raise TruthImportError(f"{field} must include an explicit timezone") from exc


def _strict_truth_bool(value: Any, field: str, *, csv_text: bool) -> bool:
    if type(value) is bool:
        return value
    if csv_text and value in ("true", "false"):
        return value == "true"
    expected = "'true' or 'false'" if csv_text else "a JSON boolean"
    raise TruthImportError(f"{field} must be {expected}")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value} is not permitted")


def import_truth_observations(
    path: str | Path,
    *,
    official_source_allowlist: Collection[str],
    official_source_domains: Mapping[str, Collection[str]],
) -> tuple[TruthObservation, ...]:
    """Strictly import official truth from a local JSON, JSONL or CSV file.

    JSON/JSONL booleans must be native booleans; CSV booleans must be exactly
    ``true`` or ``false``.  Timestamps must include an explicit timezone.  The
    function performs no network access.  Every row must bind its claimed hash
    to a local evidence blob colocated with the import file, and its URL host
    must match the configured official domain for that source.
    """

    if isinstance(official_source_allowlist, (str, bytes)):
        raise TruthImportError("official_source_allowlist must be a collection of names")
    allowlist_values = tuple(official_source_allowlist)
    if not allowlist_values or any(
        not isinstance(value, str) or not value.strip() for value in allowlist_values
    ):
        raise TruthImportError("official_source_allowlist must not be empty")
    allowed_sources = frozenset(value.strip() for value in allowlist_values)
    if not isinstance(official_source_domains, Mapping):
        raise TruthImportError("official_source_domains must be a mapping")
    source_domains: dict[str, frozenset[str]] = {}
    for source in allowed_sources:
        raw_domains = official_source_domains.get(source, ())
        if isinstance(raw_domains, (str, bytes)):
            raw_domains = (raw_domains,)
        domains = frozenset(
            str(value).strip().lower().rstrip(".")
            for value in raw_domains
            if str(value).strip()
        )
        if not domains:
            raise TruthImportError(
                f"official source {source!r} has no configured evidence domain"
            )
        source_domains[source] = domains

    source_path = Path(path)
    if not source_path.is_file():
        raise TruthImportError(f"truth import file does not exist: {source_path}")
    suffix = source_path.suffix.lower()
    if suffix not in {".json", ".jsonl", ".csv"}:
        raise TruthImportError("truth import file must be JSON, JSONL, or CSV")
    try:
        text = source_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise TruthImportError(f"cannot read truth import file: {source_path}") from exc

    rows: list[Mapping[str, Any]] = []
    try:
        if suffix == ".json":
            payload = json.loads(
                text,
                parse_float=Decimal,
                parse_int=Decimal,
                parse_constant=_reject_json_constant,
            )
            if not isinstance(payload, list):
                raise TruthImportError("JSON truth import root must be an array")
            rows = payload
        elif suffix == ".jsonl":
            for line_number, line in enumerate(text.splitlines(), start=1):
                if not line.strip():
                    continue
                value = json.loads(
                    line,
                    parse_float=Decimal,
                    parse_int=Decimal,
                    parse_constant=_reject_json_constant,
                )
                if not isinstance(value, dict):
                    raise TruthImportError(
                        f"JSONL line {line_number} must contain an object"
                    )
                rows.append(value)
        else:
            reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
            headers = reader.fieldnames
            if not headers:
                raise TruthImportError("CSV truth import requires a header row")
            if len(headers) != len(set(headers)):
                raise TruthImportError("CSV truth import contains duplicate headers")
            unknown_headers = set(headers) - _TRUTH_IMPORT_FIELDS
            missing_headers = _TRUTH_REQUIRED_FIELDS - set(headers)
            if unknown_headers or missing_headers:
                raise TruthImportError(
                    "CSV truth import header mismatch: "
                    f"unknown={sorted(unknown_headers)}, missing={sorted(missing_headers)}"
                )
            rows = list(reader)
    except TruthImportError:
        raise
    except (csv.Error, json.JSONDecodeError, ValueError) as exc:
        raise TruthImportError(f"malformed {suffix[1:].upper()} truth import") from exc

    if not rows:
        raise TruthImportError("truth import file contains no observations")

    observations: list[TruthObservation] = []
    seen_ids: set[str] = set()
    csv_text = suffix == ".csv"
    for row_number, raw in enumerate(rows, start=1):
        if not isinstance(raw, Mapping):
            raise TruthImportError(f"row {row_number} must be an object")
        if any(not isinstance(key, str) for key in raw):
            raise TruthImportError(f"row {row_number} contains a non-string field name")
        fields = set(raw)
        unknown = fields - _TRUTH_IMPORT_FIELDS
        missing = _TRUTH_REQUIRED_FIELDS - fields
        if unknown or missing:
            raise TruthImportError(
                f"row {row_number} schema mismatch: "
                f"unknown={sorted(unknown)}, missing={sorted(missing)}"
            )
        if None in raw or any(value is None for value in raw.values()):
            raise TruthImportError(f"row {row_number} contains a missing CSV value")
        for field in (
            "claim_id",
            "dimension",
            "subject_id",
            "target_type",
            "forecast_period",
            "unit",
            "basis",
            "change_basis",
            "truth_source",
            "available_at",
            "fetched_at",
            "content_hash",
            "evidence_url",
            "evidence_path",
        ):
            if field in raw and not isinstance(raw[field], str):
                raise TruthImportError(f"row {row_number} {field} must be a string")

        truth_source = str(raw["truth_source"]).strip()
        if truth_source not in allowed_sources:
            raise TruthImportError(
                f"row {row_number} truth_source {truth_source!r} is not allowlisted"
            )
        locator_fields = ("dimension", "subject_id", "target_type", "forecast_period")
        if any(not str(raw.get(field, "")).strip() for field in locator_fields):
            raise TruthImportError(
                f"row {row_number} requires a complete dimension/subject/target/period locator"
            )
        if not str(raw["unit"]).strip() or not str(raw["basis"]).strip():
            raise TruthImportError(
                f"row {row_number} unit and basis must be non-empty"
            )
        evidence_url = str(raw["evidence_url"]).strip()
        parsed_url = urlsplit(evidence_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise TruthImportError(
                f"row {row_number} evidence_url must be an HTTP(S) URL"
            )
        hostname = (parsed_url.hostname or "").lower().rstrip(".")
        if not any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in source_domains[truth_source]
        ):
            raise TruthImportError(
                f"row {row_number} evidence_url host is not configured for "
                f"truth_source {truth_source!r}"
            )
        evidence_path_raw = str(raw["evidence_path"]).strip()
        if not evidence_path_raw:
            raise TruthImportError(f"row {row_number} evidence_path must not be empty")
        evidence_root = source_path.parent.resolve()
        evidence_path = (evidence_root / evidence_path_raw).resolve()
        try:
            evidence_path.relative_to(evidence_root)
        except ValueError as exc:
            raise TruthImportError(
                f"row {row_number} evidence_path must stay beside the import manifest"
            ) from exc
        if not evidence_path.is_file():
            raise TruthImportError(
                f"row {row_number} evidence file does not exist: {evidence_path_raw}"
            )
        try:
            evidence_digest = sha256(evidence_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise TruthImportError(
                f"row {row_number} cannot read evidence file: {evidence_path_raw}"
            ) from exc
        claimed_digest = str(raw["content_hash"]).strip().lower()
        if evidence_digest != claimed_digest:
            raise TruthImportError(
                f"row {row_number} content_hash does not match evidence bytes"
            )
        try:
            observation = TruthObservation(
                claim_id=str(raw.get("claim_id", "")).strip(),
                dimension=str(raw.get("dimension", "")).strip(),
                subject_id=str(raw.get("subject_id", "")).strip(),
                target_type=str(raw.get("target_type", "")).strip(),
                forecast_period=str(raw.get("forecast_period", "")).strip(),
                unit=str(raw.get("unit", "")).strip(),
                basis=str(raw.get("basis", "")).strip(),
                change_value=raw.get("change_value", ""),
                change_basis=str(raw.get("change_basis", "")).strip(),
                realized_value=raw["realized_value"],
                truth_source=truth_source,
                available_at=_strict_truth_datetime(raw["available_at"], "available_at"),
                fetched_at=_strict_truth_datetime(raw["fetched_at"], "fetched_at"),
                first_release=_strict_truth_bool(
                    raw["first_release"], "first_release", csv_text=csv_text
                ),
                revision=_strict_truth_bool(
                    raw["revision"], "revision", csv_text=csv_text
                ),
                content_hash=str(raw["content_hash"]).strip(),
                evidence_url=evidence_url,
                # A local manifest can prove byte integrity and catch a wrong
                # host, but it cannot prove that those bytes were the response
                # served by the claimed official URL.  Controlled source
                # adapters may set this flag only after binding a cached
                # response version; local truth inputs remain diagnostic.
                evidence_verified=False,
            )
        except TruthImportError:
            raise
        except (ModelValidationError, ValueError, TypeError) as exc:
            raise TruthImportError(f"row {row_number}: {exc}") from exc
        if observation.observation_id in seen_ids:
            raise TruthImportError(
                f"row {row_number} duplicates an earlier immutable observation"
            )
        seen_ids.add(observation.observation_id)
        observations.append(observation)
    return tuple(observations)


def _response_encoding(headers: Mapping[str, str]) -> str | None:
    content_type = next(
        (value for key, value in headers.items() if key.lower() == "content-type"), ""
    )
    match = re.search(r"charset=([\w.-]+)", content_type, flags=re.IGNORECASE)
    return match.group(1) if match else None


def _normalise_network_host(host: str) -> str:
    """Return a comparable DNS host without changing the request URL."""

    value = str(host).strip().rstrip(".")
    if (
        not value
        or "*" in value
        or ":" in value
        or "/" in value
        or any(character.isspace() for character in value)
    ):
        raise ValueError(f"invalid exact network host: {host!r}")
    try:
        normalised = value.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError(f"invalid exact network host: {host!r}") from exc
    labels = normalised.split(".")
    if any(
        not label
        or len(label) > 63
        or re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) is None
        for label in labels
    ):
        raise ValueError(f"invalid exact network host: {host!r}")
    return normalised


def _create_ipv4_connection(
    address: tuple[str, int],
    timeout: float | object = socket._GLOBAL_DEFAULT_TIMEOUT,
    source_address: tuple[str, int] | None = None,
) -> socket.socket:
    """Create one TCP connection using A records only.

    This mirrors the relevant behaviour of ``socket.create_connection`` while
    pinning only the address family.  The original DNS host is retained by the
    HTTP connection, so HTTPS SNI and certificate hostname verification remain
    unchanged.
    """

    host, port = address
    last_error: OSError | None = None
    addresses = socket.getaddrinfo(
        host,
        port,
        family=socket.AF_INET,
        type=socket.SOCK_STREAM,
    )
    for family, socket_type, protocol, _canonical_name, socket_address in addresses:
        connection: socket.socket | None = None
        try:
            connection = socket.socket(family, socket_type, protocol)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                connection.settimeout(timeout)  # type: ignore[arg-type]
            if source_address:
                connection.bind(source_address)
            connection.connect(socket_address)
            return connection
        except OSError as exc:
            last_error = exc
            if connection is not None:
                connection.close()
    if last_error is not None:
        raise last_error
    raise OSError(f"getaddrinfo returned no IPv4 addresses for {host!r}")


def _selective_create_connection(
    address: tuple[str, int],
    timeout: float | object,
    source_address: tuple[str, int] | None,
    *,
    ipv4_only_hosts: Collection[str],
) -> socket.socket:
    try:
        host = _normalise_network_host(address[0])
    except ValueError:
        # Parsed IPv6 literals and proxy-specific endpoint forms are not DNS
        # allowlist entries.  Preserve the system resolver for those routes.
        return socket.create_connection(address, timeout, source_address)  # type: ignore[arg-type]
    if host in ipv4_only_hosts:
        return _create_ipv4_connection(address, timeout, source_address)
    return socket.create_connection(address, timeout, source_address)  # type: ignore[arg-type]


class _SelectiveIPv4HTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args: Any, ipv4_only_hosts: Collection[str], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._ipv4_only_hosts = frozenset(ipv4_only_hosts)
        self._create_connection = self._create_selective_connection

    def _create_selective_connection(
        self,
        address: tuple[str, int],
        timeout: float | object = socket._GLOBAL_DEFAULT_TIMEOUT,
        source_address: tuple[str, int] | None = None,
    ) -> socket.socket:
        return _selective_create_connection(
            address,
            timeout,
            source_address,
            ipv4_only_hosts=self._ipv4_only_hosts,
        )


class _SelectiveIPv4HTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args: Any, ipv4_only_hosts: Collection[str], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._ipv4_only_hosts = frozenset(ipv4_only_hosts)
        self._create_connection = self._create_selective_connection

    def _create_selective_connection(
        self,
        address: tuple[str, int],
        timeout: float | object = socket._GLOBAL_DEFAULT_TIMEOUT,
        source_address: tuple[str, int] | None = None,
    ) -> socket.socket:
        return _selective_create_connection(
            address,
            timeout,
            source_address,
            ipv4_only_hosts=self._ipv4_only_hosts,
        )


class _SelectiveIPv4HTTPHandler(HTTPHandler):
    def __init__(self, ipv4_only_hosts: Collection[str]) -> None:
        super().__init__()
        self._ipv4_only_hosts = frozenset(ipv4_only_hosts)

    def http_open(self, request: Request) -> Any:
        def connection_factory(host: str, **kwargs: Any) -> _SelectiveIPv4HTTPConnection:
            return _SelectiveIPv4HTTPConnection(
                host,
                ipv4_only_hosts=self._ipv4_only_hosts,
                **kwargs,
            )

        return self.do_open(connection_factory, request)


class _SelectiveIPv4HTTPSHandler(HTTPSHandler):
    def __init__(self, ipv4_only_hosts: Collection[str]) -> None:
        super().__init__()
        self._ipv4_only_hosts = frozenset(ipv4_only_hosts)

    def https_open(self, request: Request) -> Any:
        def connection_factory(host: str, **kwargs: Any) -> _SelectiveIPv4HTTPSConnection:
            return _SelectiveIPv4HTTPSConnection(
                host,
                ipv4_only_hosts=self._ipv4_only_hosts,
                **kwargs,
            )

        return self.do_open(connection_factory, request, context=self._context)


class CachedHttpClient:
    """Cache-first GET client with bounded retries and monotonic rate limiting."""

    def __init__(
        self,
        cache: ContentAddressedHttpCache,
        *,
        offline: bool = False,
        as_of: datetime | None = None,
        rate_limit_seconds: float = 1.0,
        max_retries: int = 3,
        timeout: float = 30.0,
        user_agent: str = DEFAULT_USER_AGENT,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        ipv4_only_hosts: Collection[str] = (),
        request_opener: Callable[..., Any] | None = None,
    ) -> None:
        if rate_limit_seconds < 0:
            raise ValueError("rate_limit_seconds cannot be negative")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.cache = cache
        self.offline = bool(offline)
        if as_of is not None:
            ensure_aware(as_of, "as_of")
        self.as_of = as_of
        self.rate_limit_seconds = float(rate_limit_seconds)
        self.max_retries = int(max_retries)
        self.timeout = float(timeout)
        self.user_agent = user_agent
        self._clock = clock
        self._sleep = sleeper
        self._last_request_at: float | None = None
        self.ipv4_only_hosts = frozenset(
            _normalise_network_host(host) for host in ipv4_only_hosts
        )
        self._opener: Any = None
        if request_opener is not None:
            self._request_opener = request_opener
        elif self.ipv4_only_hosts:
            self._opener = build_opener(
                _SelectiveIPv4HTTPHandler(self.ipv4_only_hosts),
                _SelectiveIPv4HTTPSHandler(self.ipv4_only_hosts),
            )
            self._request_opener = self._opener.open
        else:
            self._request_opener = urlopen

    def close(self) -> None:
        if self._opener is not None:
            self._opener.close()

    @staticmethod
    def canonical_url(url: str, params: Mapping[str, Any] | None = None) -> str:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError(f"unsupported URL: {url!r}")
        query: list[tuple[str, str]] = list(parse_qsl(parts.query, keep_blank_values=True))
        for key, value in (params or {}).items():
            if isinstance(value, (tuple, list)):
                query.extend((str(key), str(item)) for item in value)
            elif value is not None:
                query.append((str(key), str(value)))
        query.sort(key=lambda item: (item[0], item[1]))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))

    @staticmethod
    def request_key(url: str, headers: Mapping[str, str] | None = None) -> str:
        canonical_headers = "\n".join(
            f"{key.lower()}:{value.strip()}"
            for key, value in sorted((headers or {}).items(), key=lambda item: item[0].lower())
            if key.lower() in {"accept", "referer"}
        )
        return sha256(f"GET\n{url}\n{canonical_headers}".encode("utf-8")).hexdigest()

    def _rate_limit(self) -> None:
        now = self._clock()
        if self._last_request_at is not None:
            remaining = self.rate_limit_seconds - (now - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request_at = self._clock()

    def get(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        refresh: bool = False,
    ) -> HttpResponse:
        resolved_url = self.canonical_url(url, params)
        resolved_headers = {
            "Accept": "application/json,text/html,application/pdf,*/*",
            "User-Agent": self.user_agent,
            **dict(headers or {}),
        }
        request_key = self.request_key(resolved_url, resolved_headers)
        cached = self.cache.get(request_key, as_of=self.as_of)
        if cached is not None and (self.offline or not refresh):
            return HttpResponse(
                url=cached.url,
                status=cached.status,
                headers=cached.headers,
                body=cached.body,
                fetched_at=cached.fetched_at,
                content_hash=cached.content_hash,
                from_cache=True,
            )
        if self.offline:
            raise OfflineCacheMiss(f"offline cache miss for {resolved_url}")

        final_error: BaseException | None = None
        attempts = self.max_retries + 1
        for attempt in range(attempts):
            self._rate_limit()
            request = Request(resolved_url, headers=resolved_headers, method="GET")
            try:
                with self._request_opener(request, timeout=self.timeout) as response:
                    status = int(getattr(response, "status", response.getcode()))
                    body = response.read()
                    response_headers = {str(k): str(v) for k, v in response.headers.items()}
                if not 200 <= status < 300:
                    raise HttpStatusError(status, resolved_url)
                fetched_at = datetime.now(timezone.utc)
                cached_entry = self.cache.put(
                    request_key,
                    resolved_url,
                    status,
                    response_headers,
                    body,
                    fetched_at,
                )
                if self.as_of is not None and fetched_at > self.as_of:
                    raise SourceError(
                        "network response was fetched after the requested point-in-time cutoff"
                    )
                return HttpResponse(
                    url=resolved_url,
                    status=status,
                    headers=response_headers,
                    body=body,
                    fetched_at=fetched_at,
                    content_hash=cached_entry.content_hash,
                    from_cache=False,
                )
            except HTTPError as exc:
                final_error = exc
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt + 1 >= attempts:
                    raise HttpStatusError(int(exc.code), resolved_url) from exc
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                delay = _retry_delay(attempt, retry_after)
            except ssl.SSLCertVerificationError as exc:
                raise SourceError(
                    f"TLS certificate verification failed for {resolved_url}"
                ) from exc
            except (URLError, TimeoutError, socket.timeout, OSError) as exc:
                if _exception_chain_contains(exc, ssl.SSLCertVerificationError):
                    raise SourceError(
                        f"TLS certificate verification failed for {resolved_url}"
                    ) from exc
                final_error = exc
                if attempt + 1 >= attempts:
                    break
                delay = _retry_delay(attempt, None)
            self._sleep(delay)
        raise SourceError(f"request failed after {attempts} attempts: {resolved_url}") from final_error


def _exception_chain_contains(error: BaseException, expected: type[BaseException]) -> bool:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        if isinstance(current, expected):
            return True
        seen.add(id(current))
        reason = getattr(current, "reason", None)
        if isinstance(reason, BaseException) and id(reason) not in seen:
            current = reason
        else:
            current = current.__cause__ or current.__context__
    return False


def _retry_delay(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return min(60.0, max(0.0, float(retry_after)))
        except ValueError:
            pass
    return min(30.0, (2.0**attempt) + random.uniform(0.0, 0.25))


def _as_date(value: date | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"invalid date: {value!r}") from exc


def _canonical_json_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _source_decimal(value: Any) -> Decimal | None:
    if value is None or str(value).strip().lower() in {"", "-", "--", "null", "none", "nan"}:
        return None
    return decimal_or_none(value)


def _next_weekday_open(day: date) -> datetime:
    candidate = day + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return datetime.combine(candidate, datetime_time(9, 30), tzinfo=CHINA_TZ)


class EastmoneySource:
    """Normalise Eastmoney's stock, industry, strategy and macro report APIs."""

    LIST_URL = "https://reportapi.eastmoney.com/report/list"
    INSTITUTION_URL = "https://reportapi.eastmoney.com/report/jg"
    PDF_TEMPLATE = "https://pdf.dfcfw.com/pdf/H3_{info_code}_1.pdf"
    _CATEGORY_SPECS = {
        "stock": ((0, "stock"),),
        "industry": ((1, "industry"),),
        "strategy": ((2, "strategy"),),
        "macro": ((3, "macro"), (2, "strategy")),
    }

    def __init__(
        self,
        client: CachedHttpClient,
        *,
        source_name: str = "eastmoney_public",
        broker_aliases: Mapping[str, Sequence[str]] | None = None,
        config: Mapping[str, Any] | None = None,
        trading_calendar: Sequence[date] | None = None,
        trading_calendar_verified: bool = False,
    ) -> None:
        self.client = client
        self.source_name = source_name
        self._trading_calendar = tuple(sorted(set(trading_calendar or ())))
        self._trading_calendar_verified = trading_calendar_verified is True
        configured_aliases = broker_aliases
        if configured_aliases is None and isinstance(config, Mapping):
            candidate = config.get("broker_aliases")
            configured_aliases = candidate if isinstance(candidate, Mapping) else None
        history = config.get("entity_history", {}) if isinstance(config, Mapping) else {}
        ambiguous_values = (
            history.get("do_not_collapse_ambiguous_successor_aliases", ())
            if isinstance(history, Mapping)
            else ()
        )
        self._ambiguous_broker_aliases = {
            str(value).strip()
            for value in ambiguous_values
            if str(value).strip()
        }
        alias_candidates: dict[str, set[str]] = {}
        for canonical, aliases in (configured_aliases or {}).items():
            canonical_text = str(canonical).strip()
            if not canonical_text:
                continue
            values: Sequence[Any]
            if isinstance(aliases, Sequence) and not isinstance(aliases, (str, bytes)):
                values = aliases
            else:
                values = ()
            for value in (canonical_text, *values):
                alias = str(value).strip()
                if alias:
                    alias_candidates.setdefault(alias, set()).add(canonical_text)
        self._broker_families = {
            alias: next(iter(families))
            for alias, families in alias_candidates.items()
            if len(families) == 1 and alias not in self._ambiguous_broker_aliases
        }
        self._duplicate_broker_aliases = {
            alias for alias, families in alias_candidates.items() if len(families) > 1
        }

    def _date_only_available_at(self, published_day: date) -> tuple[datetime, str]:
        candidates = [day for day in self._trading_calendar if day > published_day]
        if candidates:
            quality = (
                "date_only_exchange_calendar"
                if self._trading_calendar_verified
                else "date_only_local_calendar_unverified"
            )
            return (
                datetime.combine(candidates[0], datetime_time(9, 30), tzinfo=CHINA_TZ),
                quality,
            )
        # Preserve metadata coverage when no verified exchange calendar was
        # supplied, but make the uncertainty explicit so formal CLI gates can
        # exclude the report.  A weekday approximation is not a trading day.
        return _next_weekday_open(published_day), "date_only_calendar_unverified"

    def _broker_family(self, *source_names: str) -> tuple[str, str]:
        for name in source_names:
            candidate = name.strip()
            if not candidate:
                continue
            if candidate in self._ambiguous_broker_aliases or candidate in self._duplicate_broker_aliases:
                return "", "ambiguous"
            family = self._broker_families.get(candidate)
            if family:
                return family, "alias_match"
        return "", "unmatched"

    def iter_reports(
        self,
        dimension: str,
        start_date: date | str,
        end_date: date | str,
        *,
        page_size: int = 100,
        max_pages: int | None = None,
        subject_id: str = "",
        include_strategy: bool = True,
        as_of: datetime | None = None,
        refresh: bool = False,
    ) -> Iterator[ResearchReport]:
        """Yield all normalised reports in a date range, following pagination.

        ``dimension='macro'`` includes strategy reports by default because the
        V1 model treats market strategy as a macro-layer report signal.  The
        original category is retained in ``metadata['_report_category']``.
        """

        if dimension not in self._CATEGORY_SPECS:
            raise ValueError("dimension must be stock, industry, macro, or strategy")
        begin = _as_date(start_date)
        end = _as_date(end_date)
        if begin > end:
            raise ValueError("start_date cannot be after end_date")
        if not 1 <= int(page_size) <= 5000:
            raise ValueError("page_size must be in [1, 5000]")
        if max_pages is not None and max_pages <= 0:
            raise ValueError("max_pages must be positive")
        decision_time = ensure_aware(as_of, "as_of") if as_of else None
        specs = self._CATEGORY_SPECS[dimension]
        if dimension == "macro" and not include_strategy:
            specs = ((3, "macro"),)

        seen: set[str] = set()
        complete_batch: list[ResearchReport] = []
        for query_type, category in specs:
            endpoint = self.LIST_URL if query_type in (0, 1) else self.INSTITUTION_URL
            page = 1
            total_pages: int | None = None
            while total_pages is None or page <= total_pages:
                if max_pages is not None and page > max_pages:
                    break
                response = self.client.get(
                    endpoint,
                    self._report_params(
                        query_type,
                        begin,
                        end,
                        page,
                        int(page_size),
                        subject_id,
                    ),
                    headers={"Referer": "https://data.eastmoney.com/report/"},
                    refresh=refresh,
                )
                payload = response.json()
                if not isinstance(payload, dict):
                    raise MalformedResponseError("Eastmoney report response must be an object")
                raw_rows = payload.get("data")
                if not isinstance(raw_rows, list):
                    raise MalformedResponseError("Eastmoney report response lacks data[]")
                current_total_pages = _strict_source_count(
                    payload.get("TotalPage"), "TotalPage"
                )
                if total_pages is None:
                    total_pages = current_total_pages
                elif current_total_pages != total_pages:
                    raise IncompleteSourceBatchError(
                        "Eastmoney report TotalPage changed during pagination"
                    )
                if max_pages is not None and total_pages > max_pages:
                    raise IncompleteSourceBatchError(
                        "Eastmoney report pagination exceeds max_pages"
                    )
                if total_pages == 0:
                    if raw_rows:
                        raise MalformedResponseError(
                            "Eastmoney report response has rows with TotalPage=0"
                        )
                    break
                if not raw_rows:
                    raise IncompleteSourceBatchError(
                        "Eastmoney report pagination ended before TotalPage"
                    )
                current_year = payload.get("currentYear")
                for raw in raw_rows:
                    if not isinstance(raw, dict):
                        raise MalformedResponseError("Eastmoney report record must be an object")
                    report = self._normalise_report(
                        raw,
                        query_type=query_type,
                        category=category,
                        response=response,
                        current_year=current_year,
                    )
                    if report.report_id in seen:
                        raise IncompleteSourceBatchError(
                            f"Eastmoney report pagination repeated {report.report_id}"
                        )
                    seen.add(report.report_id)
                    if decision_time is not None and report.available_at > decision_time:
                        # One date-only row may execute at the next trading-day
                        # open after the research cutoff.  Isolate that row so
                        # tuple(fetch_reports(...)) keeps earlier valid rows.
                        continue
                    complete_batch.append(report)
                if page >= total_pages:
                    break
                page += 1
        yield from complete_batch

    def fetch_reports(self, *args: Any, **kwargs: Any) -> tuple[ResearchReport, ...]:
        return tuple(self.iter_reports(*args, **kwargs))

    @staticmethod
    def _report_params(
        query_type: int,
        begin: date,
        end: date,
        page: int,
        page_size: int,
        subject_id: str,
    ) -> dict[str, str]:
        params = {
            "pageSize": str(page_size),
            "beginTime": begin.isoformat(),
            "endTime": end.isoformat(),
            "pageNo": str(page),
            "pageNum": str(page),
            "pageNumber": str(page),
            "p": str(page),
            "fields": "",
            "qType": str(query_type),
            "orgCode": "",
            "rcode": "",
        }
        if query_type in (0, 1):
            params.update(
                {
                    "industryCode": subject_id if query_type == 1 and subject_id else "*",
                    "industry": "*",
                    "rating": "*",
                    "ratingChange": "*",
                    "code": subject_id if query_type == 0 and subject_id else "*",
                }
            )
        else:
            params["author"] = ""
        return params

    def _normalise_report(
        self,
        raw: Mapping[str, Any],
        *,
        query_type: int,
        category: str,
        response: HttpResponse,
        current_year: Any,
    ) -> ResearchReport:
        title = str(raw.get("title") or "").strip()
        broker = str(raw.get("orgSName") or raw.get("orgName") or "").strip()
        if not title or not broker:
            raise MalformedResponseError("report record lacks title or broker")
        published_raw = str(raw.get("publishDate") or "").strip()
        if not published_raw:
            raise MalformedResponseError("report record lacks publishDate")
        published = parse_datetime(published_raw)
        date_only = (
            published.hour == 0
            and published.minute == 0
            and published.second == 0
            and published.microsecond == 0
        )
        if date_only:
            available, timestamp_quality = self._date_only_available_at(
                published.date()
            )
        else:
            available = published
            timestamp_quality = "source_timestamp"

        info_code = str(raw.get("infoCode") or "").strip()
        source_id = str(raw.get("id") or info_code).strip()
        if not source_id:
            source_id = stable_identifier(query_type, broker, title, published.isoformat())
        report_id = f"eastmoney:{category}:{source_id}"
        analyst = str(raw.get("researcher") or "").strip()
        if not analyst:
            author = raw.get("author")
            if isinstance(author, Sequence) and not isinstance(author, (str, bytes)):
                analyst = ",".join(str(item).split(".", 1)[-1] for item in author)
            else:
                analyst = str(author or "").strip()

        if category == "stock":
            subject_id = str(raw.get("stockCode") or "").strip()
            subject_name = str(raw.get("stockName") or "").strip()
            industry_id = str(raw.get("indvInduCode") or raw.get("industryCode") or "").strip()
        elif category == "industry":
            subject_id = str(raw.get("industryCode") or "").strip()
            subject_name = str(raw.get("industryName") or "").strip()
            industry_id = subject_id
        elif category == "strategy":
            subject_id, subject_name, industry_id = "market_strategy", "市场策略", ""
        else:
            subject_id, subject_name, industry_id = "macro", "宏观经济", ""

        encoded_url = str(raw.get("encodeUrl") or "").strip()
        if query_type == 0:
            source_url = (
                "https://data.eastmoney.com/report/zw_stock.jshtml?"
                + urlencode({"infocode": info_code})
            )
        elif query_type == 1:
            source_url = (
                "https://data.eastmoney.com/report/zw_industry.jshtml?"
                + urlencode({"infocode": info_code})
            )
        elif query_type == 2:
            source_url = (
                "https://data.eastmoney.com/report/zw_strategy.jshtml?"
                + urlencode({"encodeUrl": encoded_url})
            )
        else:
            source_url = (
                "https://data.eastmoney.com/report/zw_macresearch.jshtml?"
                + urlencode({"encodeUrl": encoded_url})
            )
        pdf_url = self.PDF_TEMPLATE.format(info_code=info_code) if info_code else ""
        target_low = _source_decimal(raw.get("indvAimPriceL"))
        target_high = _source_decimal(raw.get("indvAimPriceT"))
        if target_low is not None and target_high is not None and target_low > target_high:
            target_low, target_high = target_high, target_low

        metadata = dict(raw)
        broker_family_id, broker_family_status = self._broker_family(
            broker, str(raw.get("orgName") or "")
        )
        metadata.update(
            {
                "_qtype": query_type,
                "_report_category": category,
                "_listing_url": response.url,
                "_listing_content_hash": response.content_hash,
                "_current_year": current_year,
                "_broker_family_id": broker_family_id,
                "_broker_family_match_status": broker_family_status,
            }
        )
        return ResearchReport(
            report_id=report_id,
            dimension="macro" if category in ("macro", "strategy") else category,
            subject_id=subject_id,
            subject_name=subject_name,
            industry_id=industry_id,
            title=title,
            broker=broker,
            broker_code=str(raw.get("orgCode") or "").strip(),
            analyst=analyst,
            team=analyst,
            published_at=published,
            available_at=available,
            fetched_at=response.fetched_at,
            timestamp_quality=timestamp_quality,
            source=self.source_name,
            source_url=source_url,
            pdf_url=pdf_url,
            content_hash=_canonical_json_hash(raw),
            rating=str(raw.get("sRatingName") or raw.get("emRatingName") or "").strip(),
            rating_change=str(raw.get("ratingChange") or "").strip(),
            target_price_min=target_low,
            target_price_max=target_high,
            metadata=metadata,
        )

    def resolve_pdf_url(self, report: ResearchReport, *, refresh: bool = False) -> str:
        """Return a listing PDF URL or parse it from the public detail page."""

        if report.pdf_url:
            return report.pdf_url
        if not report.source_url:
            raise MalformedResponseError(f"report {report.report_id} has no detail URL")
        response = self.client.get(
            report.source_url,
            headers={"Referer": "https://data.eastmoney.com/report/"},
            refresh=refresh,
        )
        document = html.unescape(response.text())
        patterns = (
            r"href\s*=\s*[\"']([^\"']+\.pdf(?:\?[^\"']*)?)[\"']",
            r"(https?://pdf\.dfcfw\.com/[^\"'<>\\]+\.pdf(?:\?[^\"'<>\\]*)?)",
        )
        for pattern in patterns:
            match = re.search(pattern, document, flags=re.IGNORECASE)
            if match:
                return urljoin(report.source_url, match.group(1).replace("\\/", "/"))
        raise MalformedResponseError(f"PDF URL not found for {report.report_id}")

    def fetch_pdf(self, report: ResearchReport, *, refresh: bool = False) -> HttpResponse:
        pdf_url = self.resolve_pdf_url(report, refresh=refresh)
        response = self.client.get(
            pdf_url,
            headers={"Referer": report.source_url or "https://data.eastmoney.com/report/"},
            refresh=refresh,
        )
        if not response.body.startswith(b"%PDF-"):
            raise MalformedResponseError(f"non-PDF payload for {report.report_id}")
        return response


@dataclass(frozen=True)
class ActualEpsObservation:
    instrument_id: str
    report_period: date
    value: Decimal
    available_at: datetime
    source: str
    fetched_at: datetime
    content_hash: str
    provisional: bool = True
    authoritative: bool = False

    def __post_init__(self) -> None:
        ensure_aware(self.available_at, "available_at")
        ensure_aware(self.fetched_at, "fetched_at")
        converted = decimal_or_none(self.value)
        if converted is None:
            raise ValueError("EPS value is required")
        object.__setattr__(self, "value", converted)
        if self.authoritative:
            raise ValueError(
                "Eastmoney F10 EPS is provisional; official first-release truth "
                "must be imported from CNINFO"
            )


@dataclass(frozen=True)
class IndustryBoardRecord:
    """One normalized row from Eastmoney's public industry-board snapshot."""

    board_id: str
    board_name: str
    source_page_position: int
    metrics: Mapping[str, Decimal | None]
    source_content_hash: str

    def __post_init__(self) -> None:
        if not self.board_id.strip() or not self.board_name.strip():
            raise ValueError("industry board id and name are required")
        if self.source_page_position <= 0:
            raise ValueError("industry board source_page_position must be positive")
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_content_hash):
            raise ValueError("source_content_hash must be a lowercase SHA-256")
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))


@dataclass(frozen=True)
class IndustryBoardBatch:
    """An all-or-nothing, completeness-checked industry-board snapshot."""

    records: tuple[IndustryBoardRecord, ...]
    expected_total: int
    pages_fetched: int
    first_fetched_at: datetime
    last_fetched_at: datetime
    source: str
    source_url: str
    content_hash: str
    all_from_cache: bool

    def __post_init__(self) -> None:
        ensure_aware(self.first_fetched_at, "first_fetched_at")
        ensure_aware(self.last_fetched_at, "last_fetched_at")
        if self.last_fetched_at < self.first_fetched_at:
            raise ValueError("industry batch fetch interval is reversed")
        if self.expected_total < 0 or len(self.records) != self.expected_total:
            raise ValueError("industry batch must contain exactly expected_total records")
        if self.pages_fetched <= 0:
            raise ValueError("industry batch pages_fetched must be positive")
        if not re.fullmatch(r"[0-9a-f]{64}", self.content_hash):
            raise ValueError("industry batch content_hash must be a lowercase SHA-256")


class EastmoneyIndustryBoardSource:
    """Fetch a complete public Eastmoney industry-board snapshot.

    No partial page set is returned.  The source's declared total must remain
    stable, every board code must be unique, and all pages must have been
    captured within ``max_fetch_span_seconds``.  This adapter is diagnostic;
    it does not make the public snapshot an admitted official industry truth.
    """

    # AKShare's current industry-board adapter uses Eastmoney's numbered 17
    # public node for this exact board-list route.
    SNAPSHOT_URL = "https://17.push2.eastmoney.com/api/qt/clist/get"
    FIELD_MAP = MappingProxyType(
        {
            "f2": "last_price",
            "f3": "change_pct",
            "f4": "change_amount",
            "f8": "turnover_rate",
            "f20": "total_market_cap",
            "f21": "free_float_market_cap",
            "f24": "change_60d_pct",
            "f25": "change_ytd_pct",
        }
    )

    def __init__(
        self,
        client: CachedHttpClient,
        *,
        source_name: str = "eastmoney_public.push2",
    ) -> None:
        self.client = client
        self.source_name = source_name

    def fetch_snapshot(
        self,
        *,
        page_size: int = 100,
        max_pages: int = 20,
        max_fetch_span_seconds: float = 120.0,
        refresh: bool = True,
        require_live: bool = True,
        allow_empty: bool = False,
    ) -> IndustryBoardBatch:
        if not 1 <= int(page_size) <= 5000:
            raise ValueError("page_size must be in [1, 5000]")
        if max_pages <= 0:
            raise ValueError("max_pages must be positive")
        if max_fetch_span_seconds < 0:
            raise ValueError("max_fetch_span_seconds cannot be negative")

        expected_total: int | None = None
        records: list[IndustryBoardRecord] = []
        seen_codes: set[str] = set()
        responses: list[HttpResponse] = []
        page = 1
        while True:
            if page > max_pages:
                raise IncompleteSourceBatchError(
                    "Eastmoney industry snapshot exceeded max_pages before completion"
                )
            response = self.client.get(
                self.SNAPSHOT_URL,
                {
                    "pn": str(page),
                    "pz": str(int(page_size)),
                    "po": "1",
                    "np": "1",
                    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                    "fltt": "2",
                    "invt": "2",
                    "fid": "f3",
                    "fs": "m:90 t:2 f:!50",
                    "fields": "f2,f3,f4,f8,f12,f14,f20,f21,f24,f25",
                },
                headers={"Referer": "https://quote.eastmoney.com/center/boardlist.html"},
                refresh=refresh,
            )
            responses.append(response)
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("rc") != 0:
                raise MalformedResponseError(
                    "Eastmoney industry response must be an rc=0 object"
                )
            data = payload.get("data")
            if not isinstance(data, dict):
                raise MalformedResponseError("Eastmoney industry response lacks data object")
            current_total = _strict_source_count(data.get("total"), "data.total")
            if expected_total is None:
                expected_total = current_total
            elif current_total != expected_total:
                raise IncompleteSourceBatchError(
                    "Eastmoney industry total changed during pagination"
                )
            raw_rows = data.get("diff")
            if not isinstance(raw_rows, list):
                raise MalformedResponseError("Eastmoney industry response lacks data.diff[]")
            if expected_total == 0:
                if raw_rows:
                    raise MalformedResponseError(
                        "Eastmoney industry response has rows with data.total=0"
                    )
                if not allow_empty:
                    raise IncompleteSourceBatchError(
                        "Eastmoney industry snapshot unexpectedly declared zero boards"
                    )
                break
            if not raw_rows:
                raise IncompleteSourceBatchError(
                    "Eastmoney industry pagination ended before declared total"
                )
            for raw in raw_rows:
                if not isinstance(raw, dict):
                    raise MalformedResponseError(
                        "Eastmoney industry record must be an object"
                    )
                board_id = str(raw.get("f12") or "").strip()
                board_name = str(raw.get("f14") or "").strip()
                if not board_id or not board_name:
                    raise MalformedResponseError(
                        "Eastmoney industry record lacks f12 code or f14 name"
                    )
                if board_id in seen_codes:
                    raise IncompleteSourceBatchError(
                        f"Eastmoney industry pagination repeated board {board_id}"
                    )
                seen_codes.add(board_id)
                records.append(
                    IndustryBoardRecord(
                        board_id=board_id,
                        board_name=board_name,
                        # This is an observed page position, not a durable
                        # global rank: live values can move between page calls.
                        source_page_position=len(records) + 1,
                        metrics={
                            metric_name: _source_decimal(raw.get(field_name))
                            for field_name, metric_name in self.FIELD_MAP.items()
                        },
                        source_content_hash=response.content_hash,
                    )
                )
            if len(records) > expected_total:
                raise IncompleteSourceBatchError(
                    "Eastmoney industry rows exceed the declared total"
                )
            if len(records) == expected_total:
                break
            page += 1

        if expected_total is None:
            raise MalformedResponseError("Eastmoney industry total was not observed")
        first_fetched_at = min(response.fetched_at for response in responses)
        last_fetched_at = max(response.fetched_at for response in responses)
        cache_modes = {response.from_cache for response in responses}
        if len(cache_modes) > 1:
            raise IncompleteSourceBatchError(
                "Eastmoney industry snapshot mixes cached and live pages"
            )
        if require_live and True in cache_modes:
            raise IncompleteSourceBatchError(
                "Eastmoney current industry snapshot was served from replay cache"
            )
        if (last_fetched_at - first_fetched_at).total_seconds() > max_fetch_span_seconds:
            raise IncompleteSourceBatchError(
                "Eastmoney industry pages were captured too far apart for one snapshot"
            )
        batch_hash = _canonical_json_hash(
            {
                "expected_total": expected_total,
                "page_hashes": [response.content_hash for response in responses],
                "record_ids": [record.board_id for record in records],
            }
        )
        return IndustryBoardBatch(
            records=tuple(records),
            expected_total=expected_total,
            pages_fetched=len(responses),
            first_fetched_at=first_fetched_at,
            last_fetched_at=last_fetched_at,
            source=self.source_name,
            source_url=self.SNAPSHOT_URL,
            content_hash=batch_hash,
            all_from_cache=cache_modes == {True},
        )


def _strict_source_count(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise MalformedResponseError(f"{field} must be a non-negative integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
        parsed = int(value.strip())
    else:
        raise MalformedResponseError(f"{field} must be a non-negative integer")
    if parsed < 0:
        raise MalformedResponseError(f"{field} must be a non-negative integer")
    return parsed


class MarketSource(Protocol):
    """Narrow adapter boundary used by outcome evaluation."""

    def daily_bars(
        self,
        instrument_id: str,
        start_date: date | str,
        end_date: date | str,
        *,
        as_of: datetime,
        adjust: str = "qfq",
        refresh: bool = False,
    ) -> tuple[DailyBar, ...]: ...

    def actual_eps(
        self,
        instrument_id: str,
        report_period: date | str,
        *,
        as_of: datetime,
        refresh: bool = False,
    ) -> ActualEpsObservation | None: ...


class EastmoneyMarketSource:
    """Eastmoney daily bars plus *provisional* reported basic EPS.

    ``actual_eps`` is useful for coverage diagnostics only.  It is explicitly
    non-authoritative and must not replace the first disclosed annual-report
    EPS imported from CNINFO for formal broker scoring.
    """

    KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    FUNDAMENTAL_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

    def __init__(self, client: CachedHttpClient, *, source_name: str = "eastmoney_public") -> None:
        self.client = client
        self.source_name = source_name
        self.last_issues: list[dict[str, Any]] = []

    @staticmethod
    def secid(instrument_id: str) -> str:
        text = instrument_id.strip().upper()
        if re.fullmatch(r"(?:0|1|2|90)\.[A-Z0-9]+", text):
            return text
        suffix_match = re.fullmatch(r"(?P<code>\d{6})\.(?P<market>SH|SZ|BJ)", text)
        if suffix_match:
            code = suffix_match.group("code")
            market = suffix_match.group("market")
            EastmoneyMarketSource._validate_code_market(code, market)
            return f"{'1' if market == 'SH' else '0'}.{code}"
        prefix_match = re.fullmatch(r"(?P<market>SH|SZ|BJ)(?P<code>\d{6})", text)
        if prefix_match:
            code = prefix_match.group("code")
            market = prefix_match.group("market")
            EastmoneyMarketSource._validate_code_market(code, market)
            return f"{'1' if market == 'SH' else '0'}.{code}"
        if text.startswith("BK"):
            return f"90.{text}"
        if re.fullmatch(r"\d{6}", text):
            market = "1" if text[0] in "569" else "0"
            return f"{market}.{text}"
        raise UnsupportedInstrumentError(f"cannot map Eastmoney secid: {instrument_id!r}")

    @staticmethod
    def _validate_code_market(code: str, market: str) -> None:
        # Prefix 0 and 9 are ambiguous because they also contain exchange
        # indices/B-shares.  Explicit suffixes resolve those cases; the rules
        # below still reject every unambiguous cross-market mismatch.
        allowed = {
            "0": frozenset({"SZ", "SH"}),
            "1": frozenset({"SZ"}),
            "2": frozenset({"SZ"}),
            "3": frozenset({"SZ"}),
            "4": frozenset({"BJ"}),
            "5": frozenset({"SH"}),
            "6": frozenset({"SH"}),
            "8": frozenset({"BJ"}),
            "9": frozenset({"SH", "BJ"}),
        }
        if market not in allowed.get(code[0], frozenset()):
            raise UnsupportedInstrumentError(
                f"instrument code {code} is inconsistent with .{market} market suffix"
            )

    def _kline_response(
        self,
        instrument_id: str,
        begin: date,
        end: date,
        fqt: int,
        refresh: bool,
    ) -> tuple[dict[date, tuple[Decimal, ...]], HttpResponse]:
        response = self.client.get(
            self.KLINE_URL,
            {
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "ut": "7eea3edcaed734bea9cbfc24409ed989",
                "klt": "101",
                "fqt": str(fqt),
                "secid": self.secid(instrument_id),
                "beg": begin.strftime("%Y%m%d"),
                "end": end.strftime("%Y%m%d"),
                "lmt": "100000",
            },
            headers={"Referer": "https://quote.eastmoney.com/"},
            refresh=refresh,
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise MalformedResponseError("Eastmoney kline response must be an object")
        data = payload.get("data")
        if data is None:
            return {}, response
        if not isinstance(data, dict) or not isinstance(data.get("klines"), list):
            raise MalformedResponseError("Eastmoney kline response lacks data.klines[]")
        parsed: dict[date, tuple[Decimal, ...]] = {}
        for line in data["klines"]:
            fields = str(line).split(",")
            if len(fields) < 7:
                raise MalformedResponseError(f"invalid Eastmoney kline row: {line!r}")
            try:
                day = date.fromisoformat(fields[0])
                parsed[day] = tuple(Decimal(item) for item in fields[1:7])
            except (ValueError, ArithmeticError) as exc:
                raise MalformedResponseError(f"invalid Eastmoney kline row: {line!r}") from exc
        return parsed, response

    def daily_bars(
        self,
        instrument_id: str,
        start_date: date | str,
        end_date: date | str,
        *,
        as_of: datetime,
        adjust: str = "qfq",
        refresh: bool = False,
    ) -> tuple[DailyBar, ...]:
        begin = _as_date(start_date)
        end = _as_date(end_date)
        if begin > end:
            raise ValueError("start_date cannot be after end_date")
        decision_time = ensure_aware(as_of, "as_of")
        self.last_issues = []
        adjust_map = {"": 0, "none": 0, "qfq": 1, "hfq": 2}
        if adjust not in adjust_map:
            raise ValueError("adjust must be '', 'none', 'qfq', or 'hfq'")
        raw, raw_response = self._kline_response(instrument_id, begin, end, 0, refresh)
        adjusted: dict[date, tuple[Decimal, ...]] = {}
        adjusted_response: HttpResponse | None = None
        if adjust_map[adjust]:
            try:
                adjusted, adjusted_response = self._kline_response(
                    instrument_id, begin, end, adjust_map[adjust], refresh
                )
            except SourceError as exc:
                # Raw bars remain usable for coverage and non-corporate-action
                # intervals, but callers must see the explicit downgrade.
                self.last_issues.append(
                    {
                        "severity": "warning",
                        "code": "ADJUSTED_PRICE_UNAVAILABLE",
                        "instrument_id": instrument_id,
                        "requested_adjustment": adjust,
                        "message": str(exc),
                    }
                )
        combined_hash = sha256(
            (raw_response.content_hash + (adjusted_response.content_hash if adjusted_response else ""))
            .encode("ascii")
        ).hexdigest()
        fetched_at = max(
            response.fetched_at
            for response in (raw_response, adjusted_response)
            if response is not None
        )
        bars: list[DailyBar] = []
        for day, values in sorted(raw.items()):
            # Eastmoney fields: open, close, high, low, volume, amount.
            open_, close, high, low, volume, amount = values
            available_at = datetime.combine(day, datetime_time(15, 10), tzinfo=CHINA_TZ)
            if available_at > decision_time:
                continue
            adjusted_values = adjusted.get(day)
            bars.append(
                DailyBar(
                    instrument_id=instrument_id,
                    trade_date=day,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                    amount=amount,
                    adjusted_open=adjusted_values[0] if adjusted_values else None,
                    adjusted_close=adjusted_values[1] if adjusted_values else None,
                    adjusted_high=adjusted_values[2] if adjusted_values else None,
                    adjusted_low=adjusted_values[3] if adjusted_values else None,
                    suspended=volume == 0,
                    available_at=available_at,
                    source=f"{self.source_name}.push2his",
                    fetched_at=fetched_at,
                    content_hash=combined_hash,
                )
            )
        return tuple(bars)

    def actual_eps(
        self,
        instrument_id: str,
        report_period: date | str,
        *,
        as_of: datetime,
        refresh: bool = False,
    ) -> ActualEpsObservation | None:
        period = _as_date(report_period)
        decision_time = ensure_aware(as_of, "as_of")
        code = re.sub(r"\D", "", instrument_id)[-6:]
        if len(code) != 6:
            raise UnsupportedInstrumentError(f"invalid A-share code: {instrument_id!r}")
        response = self.client.get(
            self.FUNDAMENTAL_URL,
            {
                "reportName": "RPT_LICO_FN_CPD",
                "columns": "ALL",
                "filter": f'(SECURITY_CODE="{code}")',
                "pageNumber": "1",
                "pageSize": "100",
                "sortColumns": "REPORTDATE,SECURITY_CODE",
                "sortTypes": "-1,1",
                "source": "WEB",
                "client": "WEB",
            },
            headers={"Referer": "https://data.eastmoney.com/"},
            refresh=refresh,
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise MalformedResponseError("Eastmoney F10 response must be an object")
        result = payload.get("result")
        if result is None:
            if payload.get("success") is False:
                raise MalformedResponseError(
                    f"Eastmoney F10 rejected query: {payload.get('message') or payload.get('code')}"
                )
            return None
        if not isinstance(result, dict):
            raise MalformedResponseError("Eastmoney F10 result must be an object")
        rows = result.get("data")
        if rows is None:
            return None
        if not isinstance(rows, list):
            raise MalformedResponseError("Eastmoney F10 response lacks result.data[]")
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            report_date_raw = raw.get("REPORT_DATE") or raw.get("REPORTDATE")
            if not report_date_raw:
                continue
            try:
                candidate_period = parse_datetime(str(report_date_raw)).date()
            except ValueError:
                continue
            if candidate_period != period:
                continue
            value = next(
                (
                    _source_decimal(raw.get(field))
                    for field in ("BASIC_EPS", "EPSJB", "BASIC_EPS_CS")
                    if raw.get(field) not in (None, "")
                ),
                None,
            )
            notice_raw = raw.get("NOTICE_DATE") or raw.get("UPDATE_DATE")
            # Without a first-publication time the row is not point-in-time
            # verifiable and is deliberately excluded.
            if value is None or not notice_raw:
                return None
            notice = parse_datetime(str(notice_raw))
            if notice.hour == notice.minute == notice.second == 0:
                available_at = datetime.combine(
                    notice.date(), datetime_time(23, 59, 59), tzinfo=CHINA_TZ
                )
            else:
                available_at = notice
            if available_at > decision_time:
                return None
            return ActualEpsObservation(
                instrument_id=instrument_id,
                report_period=period,
                value=value,
                available_at=available_at,
                source=(
                    f"{self.source_name}.provisional_non_authoritative."
                    "RPT_LICO_FN_CPD"
                ),
                fetched_at=response.fetched_at,
                content_hash=_canonical_json_hash(raw),
                provisional=True,
                authoritative=False,
            )
        return None
