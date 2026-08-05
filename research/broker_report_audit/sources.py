"""Network, report and market-data adapters used by the audit pipeline.

Only public HTTP endpoints are used.  The HTTP client is standard-library
based, rate limited, retryable and backed by an immutable content-addressed
cache so the same run can be replayed with ``offline=True``.
"""

from __future__ import annotations

import csv
import html
import io
import json
import random
import re
import socket
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
from urllib.request import Request, urlopen

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
        # Eastmoney sometimes wraps JSON in a callback even when the current
        # public endpoint normally returns plain JSON.
        if payload and payload[0] not in "[{":
            left = payload.find("(")
            right = payload.rfind(")")
            if left >= 0 and right > left:
                payload = payload[left + 1 : right]
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
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
                with urlopen(request, timeout=self.timeout) as response:
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
            except (URLError, TimeoutError, socket.timeout, OSError) as exc:
                final_error = exc
                if attempt + 1 >= attempts:
                    break
                delay = _retry_delay(attempt, None)
            self._sleep(delay)
        raise SourceError(f"request failed after {attempts} attempts: {resolved_url}") from final_error


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
                try:
                    total_pages = max(0, int(payload.get("TotalPage", 0)))
                except (TypeError, ValueError) as exc:
                    raise MalformedResponseError("invalid Eastmoney TotalPage") from exc
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
                        continue
                    if decision_time is not None and report.available_at > decision_time:
                        # One date-only row may execute at the next trading-day
                        # open after the research cutoff.  Isolate that row so
                        # tuple(fetch_reports(...)) keeps earlier valid rows.
                        continue
                    seen.add(report.report_id)
                    yield report
                if not raw_rows or page >= total_pages:
                    break
                page += 1

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
