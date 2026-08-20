"""Versioned source-owned SSE annual-closure calendar adapter.

The exchange does not publish the historical natural-date panel used by the
factor lab as one JSON dataset.  This adapter therefore captures the frozen
SSE annual closure notices (plus the two in-period amendments), parses only
their article bodies, and derives Monday-Friday sessions minus explicitly
published closures.  Any unknown page, redirect, document shape, holiday, or
date expression fails closed.
"""

from __future__ import annotations

import base64
import re
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from ..contracts import canonical_json_bytes, sha256_bytes
from ..index_evidence import (
    CN_EQUITY_SESSION,
    HTTPSResponse,
    IndexEvidenceRequest,
    IndexSourcePayload,
)
from .base import ProviderQueryError, UnsupportedDatasetError, classify_unexpected_error


_SSE_AUTHORITY = object()
_ALLOWED_HOST = "www.sse.com.cn"
_MAX_RESPONSE_BYTES = 2_000_000
_SUPPORTED_START = date(2017, 1, 1)
_SUPPORTED_END = date(2026, 12, 31)
_SAFE_RESPONSE_HEADERS = frozenset(
    {
        "cache-control",
        "content-length",
        "content-type",
        "date",
        "etag",
        "expires",
        "last-modified",
    }
)

# Fixed, source-owned detail pages verified against the SSE annual-closure
# archive.  A new year or changed document requires an adapter version bump.
_ANNUAL_NOTICE_URLS = {
    2017: "https://www.sse.com.cn/disclosure/announcement/general/c/c_20161222_4218613.shtml",
    2018: "https://www.sse.com.cn/disclosure/announcement/general/c/c_20171222_4438363.shtml",
    2019: "https://www.sse.com.cn/disclosure/announcement/general/c/c_20181220_4696473.shtml",
    2020: "https://www.sse.com.cn/disclosure/announcement/general/c/c_20191220_4969627.shtml",
    2021: "https://www.sse.com.cn/disclosure/announcement/general/c/c_20201224_5286949.shtml",
    2022: "https://www.sse.com.cn/disclosure/announcement/general/c/c_20211220_5662606.shtml",
    2023: "https://www.sse.com.cn/disclosure/announcement/general/c/c_20221227_5714458.shtml",
    2024: "https://www.sse.com.cn/disclosure/announcement/general/c/c_20231226_5733939.shtml",
    2025: "https://www.sse.com.cn/disclosure/announcement/general/c/c_20241223_10767108.shtml",
    2026: "https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml",
}
_AMENDMENT_URLS = {
    2019: (
        "labor_day_adjustment",
        "https://www.sse.com.cn/disclosure/announcement/general/c/c_20190418_4771364.shtml",
    ),
    2020: (
        "spring_festival_extension",
        "https://www.sse.com.cn/disclosure/announcement/general/c/c_20200127_4991582.shtml",
    ),
}
_ALLOWED_URLS = frozenset(_ANNUAL_NOTICE_URLS.values()) | frozenset(
    item[1] for item in _AMENDMENT_URLS.values()
)

_EXPECTED_HOLIDAYS = frozenset(
    {"元旦", "春节", "清明节", "劳动节", "端午节", "中秋节", "国庆节"}
)
_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6}
_DATE_FRAGMENT = r"(?:[0-9]{4}年)?[0-9]{1,2}月[0-9]{1,2}日（星期[一二三四五六日]）"
_DATE_TOKEN = re.compile(
    r"^(?:(?P<year>[0-9]{4})年)?(?P<month>[0-9]{1,2})月"
    r"(?P<day>[0-9]{1,2})日（星期(?P<weekday>[一二三四五六日])）$"
)
_CLOSURE = re.compile(
    rf"(?P<start>{_DATE_FRAGMENT})(?:至(?P<end>{_DATE_FRAGMENT}))?休市"
)
_REOPEN = re.compile(rf"(?P<open>{_DATE_FRAGMENT})(?:起照常|正常)开市")
_HOLIDAY_LABEL = re.compile(r"^（[一二三四五六七]）(?P<label>[^：]+)：")
_PUBLICATION_PATH = re.compile(r"^/disclosure/announcement/general/c/c_([0-9]{8})_[0-9]+\.shtml$")


class _SourceOwnedHTTPS:
    """The only transport capable of minting an SSE source attestation."""

    def get(self, url: str) -> HTTPSResponse:
        request = urllib.request.Request(
            url,
            headers={"Accept": "text/html", "User-Agent": "quant-research-os/1"},
            method="GET",
        )
        try:
            response = urllib.request.urlopen(request, timeout=30)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            body = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(body) > _MAX_RESPONSE_BYTES:
                raise ProviderQueryError("SSE notice response exceeds the fixed size limit")
            return HTTPSResponse(
                final_url=response.geturl(),
                status=int(response.status),
                headers=dict(response.headers.items()),
                body=body,
            )


def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
    values: set[str] = set()
    for key, value in attrs:
        if key.casefold() in {"class", "id"} and value:
            values.update(value.split())
    return values


class _SSEArticleParser(HTMLParser):
    """Extract one SSE article title, publication date, and body paragraphs."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._depth = 0
        self._article_depth: int | None = None
        self._body_depth: int | None = None
        self._title_depth: int | None = None
        self._date_depth: int | None = None
        self._paragraph_depth: int | None = None
        self._title_parts: list[str] = []
        self._date_parts: list[str] = []
        self._paragraph_parts: list[str] = []
        self.paragraphs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._depth += 1
        markers = _classes(attrs)
        if tag == "div" and "article-infor" in markers:
            if self._article_depth is not None:
                raise ProviderQueryError("SSE notice contains multiple nested article containers")
            self._article_depth = self._depth
        if self._article_depth is not None:
            if tag == "div" and "allZoom" in markers:
                if self._body_depth is not None:
                    raise ProviderQueryError("SSE notice contains multiple article body containers")
                self._body_depth = self._depth
            elif tag == "h2" and self._title_depth is None:
                self._title_depth = self._depth
            elif tag == "i" and self._date_depth is None:
                self._date_depth = self._depth
        if self._body_depth is not None and tag == "p":
            if self._paragraph_depth is not None:
                # Older SSE pages contain HTML4-style adjacent ``<p>`` tags
                # without explicit closing tags.  A new paragraph implicitly
                # closes the prior one; preserving that browser behavior is
                # deterministic and does not broaden the parsed article area.
                text = _clean_text("".join(self._paragraph_parts))
                if text:
                    self.paragraphs.append(text)
            self._paragraph_depth = self._depth
            self._paragraph_parts = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._title_depth is not None:
            self._title_parts.append(data)
        if self._date_depth is not None:
            self._date_parts.append(data)
        if self._paragraph_depth is not None:
            self._paragraph_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._paragraph_depth == self._depth and tag == "p":
            text = _clean_text("".join(self._paragraph_parts))
            if text:
                self.paragraphs.append(text)
            self._paragraph_depth = None
            self._paragraph_parts = []
        if self._title_depth == self._depth and tag == "h2":
            self._title_depth = None
        if self._date_depth == self._depth and tag == "i":
            self._date_depth = None
        if self._body_depth == self._depth and tag == "div":
            self._body_depth = None
        if self._article_depth == self._depth and tag == "div":
            self._article_depth = None
        self._depth = max(0, self._depth - 1)

    @property
    def title(self) -> str:
        return _clean_text("".join(self._title_parts))

    @property
    def publication_text(self) -> str:
        return _clean_text("".join(self._date_parts))


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", "", value.replace("\xa0", "").replace("\u3000", ""))


def _article(body: bytes, endpoint: str) -> tuple[str, date, tuple[str, ...]]:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ProviderQueryError("SSE notice is not strict UTF-8 HTML", raw_content=body) from exc
    parser = _SSEArticleParser()
    try:
        parser.feed(text)
        parser.close()
    except ProviderQueryError:
        raise
    except Exception as exc:
        raise ProviderQueryError("SSE notice HTML cannot be parsed", raw_content=body) from exc
    if not parser.title or not parser.paragraphs:
        raise ProviderQueryError("SSE notice article title/body is missing", raw_content=body)
    dates = re.findall(r"(?<![0-9])[0-9]{4}-[0-9]{2}-[0-9]{2}(?![0-9])", parser.publication_text)
    if len(dates) != 1:
        raise ProviderQueryError("SSE notice publication date is missing or ambiguous", raw_content=body)
    try:
        published = date.fromisoformat(dates[0])
    except ValueError as exc:
        raise ProviderQueryError("SSE notice publication date is invalid", raw_content=body) from exc
    path_match = _PUBLICATION_PATH.fullmatch(urlsplit(endpoint).path)
    if path_match is None or path_match.group(1) != published.strftime("%Y%m%d"):
        raise ProviderQueryError("SSE notice path and publication date disagree", raw_content=body)
    return parser.title, published, tuple(parser.paragraphs)


def _dated_token(value: str, default_year: int) -> date:
    match = _DATE_TOKEN.fullmatch(value)
    if match is None:
        raise ProviderQueryError("SSE notice contains an unsupported date expression")
    try:
        result = date(
            int(match.group("year") or default_year),
            int(match.group("month")),
            int(match.group("day")),
        )
    except ValueError as exc:
        raise ProviderQueryError("SSE notice contains an invalid calendar date") from exc
    if result.weekday() != _WEEKDAY[match.group("weekday")]:
        raise ProviderQueryError("SSE notice date and printed weekday disagree")
    return result


def _closure_expression(text: str, year: int) -> tuple[set[date], date]:
    closures = list(_CLOSURE.finditer(text))
    reopens = list(_REOPEN.finditer(text))
    if len(closures) != 1 or len(reopens) != 1:
        raise ProviderQueryError("SSE holiday paragraph has ambiguous closure/reopening expressions")
    start = _dated_token(closures[0].group("start"), year)
    end = _dated_token(closures[0].group("end") or closures[0].group("start"), year)
    reopened = _dated_token(reopens[0].group("open"), year)
    if end < start or (end - start).days > 15:
        raise ProviderQueryError("SSE holiday closure interval is invalid")
    if reopened <= end or (reopened - end).days > 3 or reopened.weekday() >= 5:
        raise ProviderQueryError("SSE holiday reopening date is invalid")
    weekend_sections = re.findall(r"另外，(.+?)为周末休市", text)
    if len(weekend_sections) > 1:
        raise ProviderQueryError("SSE holiday paragraph has ambiguous weekend clauses")
    if weekend_sections:
        weekend_tokens = re.findall(_DATE_FRAGMENT, weekend_sections[0])
        if not weekend_tokens or any(_dated_token(item, year).weekday() < 5 for item in weekend_tokens):
            raise ProviderQueryError("SSE notice labels a weekday as a weekend closure")
    return {
        start + timedelta(days=offset)
        for offset in range((end - start).days + 1)
    }, reopened


def _annual_closures(year: int, title: str, paragraphs: tuple[str, ...]) -> set[date]:
    allowed_titles = {
        f"关于上海证券交易所{year}年全年休市安排的通知",
        f"关于上海证券交易所{year}年部分节假日休市安排的通知",
    }
    if title not in allowed_titles:
        raise ProviderQueryError("SSE annual notice title differs from the versioned contract")
    labels: set[str] = set()
    closed: set[date] = set()
    holiday_paragraphs = 0
    for paragraph in paragraphs:
        label_match = _HOLIDAY_LABEL.match(paragraph)
        if label_match is None:
            continue
        names = tuple(label_match.group("label").split("、"))
        if not names or any(name not in _EXPECTED_HOLIDAYS or name in labels for name in names):
            raise ProviderQueryError("SSE annual notice has an unknown or duplicate holiday label")
        labels.update(names)
        additions, _ = _closure_expression(paragraph, year)
        closed.update(additions)
        holiday_paragraphs += 1
    if labels != _EXPECTED_HOLIDAYS or holiday_paragraphs not in {6, 7}:
        raise ProviderQueryError("SSE annual notice does not cover the complete holiday set")
    if not any(item.year == year and item.weekday() < 5 for item in closed):
        raise ProviderQueryError("SSE annual notice yields no weekday closures")
    return closed


def _amendment_closures(
    year: int,
    kind: str,
    title: str,
    paragraphs: tuple[str, ...],
    annual: set[date],
) -> set[date]:
    text = "".join(paragraphs)
    if year == 2019 and kind == "labor_day_adjustment":
        if title != "关于调整2019年劳动节休市安排的公告":
            raise ProviderQueryError("SSE 2019 amendment title differs from the versioned contract")
        candidates = [item for item in paragraphs if "休市安排：" in item]
        if len(candidates) != 1:
            raise ProviderQueryError("SSE 2019 amendment body is missing or ambiguous")
        additions, reopened = _closure_expression(candidates[0], year)
        expected = {date(2019, 5, day) for day in range(1, 5)}
        if additions != expected or reopened != date(2019, 5, 6):
            raise ProviderQueryError("SSE 2019 amendment semantics differ from the versioned contract")
        return additions
    if year == 2020 and kind == "spring_festival_extension":
        if title != "关于调整2020年春节休市相关安排的公告":
            raise ProviderQueryError("SSE 2020 amendment title differs from the versioned contract")
        endpoint_match = re.search(rf"延长2020年春节休市至(?P<end>{_DATE_FRAGMENT})，(?P<open>{_DATE_FRAGMENT})正常开市", text)
        if endpoint_match is None or "原定于2020年1月31日实施的业务" not in text:
            raise ProviderQueryError("SSE 2020 amendment body differs from the versioned contract")
        end = _dated_token(endpoint_match.group("end"), year)
        reopened = _dated_token(endpoint_match.group("open"), year)
        if end != date(2020, 2, 2) or reopened != date(2020, 2, 3):
            raise ProviderQueryError("SSE 2020 amendment dates differ from the versioned contract")
        if date(2020, 1, 30) not in annual or date(2020, 1, 31) in annual:
            raise ProviderQueryError("SSE 2020 annual/amendment sequence is inconsistent")
        return {
            date(2020, 1, 31) + timedelta(days=offset)
            for offset in range(3)
        }
    raise ProviderQueryError("SSE amendment is outside the versioned contract")


class SSECalendarProvider:
    provider_id = "sse_calendar"
    source_id = "sse_official"
    adapter_version = "sse-calendar-adapter-v2"
    upstream_source = "sse.source_owned_https.annual_closure_notices_v2"
    supported_datasets = frozenset({CN_EQUITY_SESSION})

    def __init__(
        self,
        *,
        transport: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._source_owned_transport = transport is None
        self._transport = _SourceOwnedHTTPS() if transport is None else transport
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _aware_clock(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ProviderQueryError("SSE calendar provider clock must include a timezone offset")
        return value

    def _capture(
        self,
        endpoint: str,
        role: str,
        fetched_at: datetime,
        transport_mode: str,
    ) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
        if endpoint not in _ALLOWED_URLS:
            raise ProviderQueryError("SSE source URL is outside the versioned allowlist")
        getter = getattr(self._transport, "get", None)
        if not callable(getter):
            raise ProviderQueryError("SSE transport must expose get(url)")
        try:
            response = getter(endpoint)
        except Exception as exc:
            raise classify_unexpected_error(exc) from exc
        if not isinstance(response, HTTPSResponse):
            raise ProviderQueryError("SSE transport returned the wrong response type")
        expected = urlsplit(endpoint)
        final = urlsplit(response.final_url)
        if (
            expected.scheme != "https"
            or expected.hostname != _ALLOWED_HOST
            or expected.query
            or expected.fragment
            or final.scheme != "https"
            or final.hostname != _ALLOWED_HOST
            or final.path != expected.path
            or final.query
            or final.fragment
        ):
            raise ProviderQueryError("SSE endpoint or redirect differs from the fixed official path")
        if response.status != 200:
            raise ProviderQueryError(f"SSE HTTPS status is {response.status}", raw_content=response.body)
        if len(response.body) > _MAX_RESPONSE_BYTES:
            raise ProviderQueryError("SSE notice response exceeds the fixed size limit")
        content_type = next(
            (value for key, value in response.headers.items() if key.casefold() == "content-type"),
            "",
        ).casefold()
        if "text/html" not in content_type:
            raise ProviderQueryError("SSE notice Content-Type is not HTML", raw_content=response.body)
        normalized_headers = {
            str(key).casefold(): str(value).strip()
            for key, value in response.headers.items()
            if str(key).casefold() in _SAFE_RESPONSE_HEADERS
        }
        receipt = {
            "source_id": self.source_id,
            "endpoint_url": endpoint,
            "final_url": response.final_url,
            "final_host": final.hostname,
            "http_status": response.status,
            "response_headers_sha256": sha256_bytes(canonical_json_bytes(normalized_headers)),
            "body_sha256": sha256_bytes(response.body),
            "fetched_at": fetched_at.isoformat(),
            "transport_mode": transport_mode,
        }
        capture = {
            "role": role,
            "url": endpoint,
            "body_base64": base64.b64encode(response.body).decode("ascii"),
            "response_headers": normalized_headers,
        }
        return response.body, capture, receipt

    def fetch(self, request: IndexEvidenceRequest) -> IndexSourcePayload:
        if request.dataset_type != CN_EQUITY_SESSION:
            raise UnsupportedDatasetError(f"SSE calendar does not support {request.dataset_type!r}")
        if request.retrieval_mode == "offline_replay":
            raise UnsupportedDatasetError("SSE online provider cannot perform offline replay")
        assert request.start_date is not None and request.end_date is not None
        if request.start_date < _SUPPORTED_START or request.end_date > _SUPPORTED_END:
            raise ProviderQueryError(
                "SSE calendar adapter v2 supports only 2017-01-01 through 2026-12-31"
            )
        years = range(request.start_date.year, request.end_date.year + 1)
        fetched_at = self._aware_clock()
        record_available_at = fetched_at.astimezone(timezone(timedelta(hours=8))).isoformat()
        transport_mode = "source_owned_https" if self._source_owned_transport else "test_injected_https"
        captures: list[dict[str, Any]] = []
        receipts: list[dict[str, Any]] = []
        closures: set[date] = set()
        for year in years:
            endpoint = _ANNUAL_NOTICE_URLS.get(year)
            if endpoint is None:
                raise ProviderQueryError("SSE request year is outside the frozen annual notice map")
            body, capture, receipt = self._capture(
                endpoint, f"annual_closure_notice_{year}", fetched_at, transport_mode
            )
            captures.append(capture)
            receipts.append(receipt)
            title, _, paragraphs = _article(body, endpoint)
            annual = _annual_closures(year, title, paragraphs)
            closures.update(annual)
            amendment = _AMENDMENT_URLS.get(year)
            if amendment is not None:
                kind, amendment_endpoint = amendment
                body, capture, receipt = self._capture(
                    amendment_endpoint, f"amendment_{kind}", fetched_at, transport_mode
                )
                captures.append(capture)
                receipts.append(receipt)
                title, _, paragraphs = _article(body, amendment_endpoint)
                closures.update(_amendment_closures(year, kind, title, paragraphs, annual))

        records: list[dict[str, Any]] = []
        cursor = request.start_date
        while cursor <= request.end_date:
            day = cursor.isoformat()
            is_open = cursor.weekday() < 5 and cursor not in closures
            records.append(
                {
                    "schema_version": "cn-equity-session-v1",
                    "calendar_date": day,
                    "is_trading_day": is_open,
                    "session_open_at": f"{day}T09:30:00+08:00" if is_open else None,
                    "session_close_at": f"{day}T15:00:00+08:00" if is_open else None,
                    "available_at": record_available_at,
                    "availability_status": "historical_backfill_not_original_capture",
                    "source_record_id": f"sse-annual-closure-v2:{day}",
                }
            )
            cursor += timedelta(days=1)
        raw_content = canonical_json_bytes(
            {
                "contract_version": "sse-calendar-raw-v2",
                "request": request.fingerprint_payload(self.provider_id, self.adapter_version),
                "captures": captures,
                "transport_receipts": receipts,
            }
        )
        return IndexSourcePayload(
            raw_content=raw_content,
            records=tuple(records),
            fetched_at=fetched_at,
            upstream_source=self.upstream_source,
            point_in_time_status="historical_backfill_not_original_capture",
            capture_mode=transport_mode,
            transport_receipts=tuple(receipts),
            _authority=_SSE_AUTHORITY if self._source_owned_transport else None,
        )

    def _attests_source_owned(self, payload: IndexSourcePayload) -> bool:
        return bool(
            self._source_owned_transport
            and type(self._transport) is _SourceOwnedHTTPS
            and payload._authority is _SSE_AUTHORITY
            and payload.capture_mode == "source_owned_https"
            and payload.transport_receipts
            and all(
                receipt.get("transport_mode") == "source_owned_https"
                for receipt in payload.transport_receipts
            )
        )


__all__ = ["SSECalendarProvider"]
