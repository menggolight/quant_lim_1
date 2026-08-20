"""Source-owned, read-only HTTPS adapter for CSI index evidence.

An injected transport is useful for deterministic parser tests, but is always
labelled ``test_injected_https`` and cannot unlock research admission.  Only
the private default HTTPS transport can carry this module's in-memory source
authority into :class:`IndexEvidenceService`.
"""

from __future__ import annotations

import base64
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from ..contracts import canonical_json_bytes, sha256_bytes
from ..index_evidence import (
    CSI_INDUSTRY_UNIVERSE,
    INDEX_LEVEL,
    HTTPSResponse,
    IndexEvidenceRequest,
    IndexSourcePayload,
    frozen_universe_records,
    strict_json,
)
from .base import NetworkBlockedError, ProviderQueryError, UnsupportedDatasetError, classify_unexpected_error


_CSI_AUTHORITY = object()
_ALLOWED_HOST = "www.csindex.com.cn"
_PERFORMANCE_ENDPOINT = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
_BASIC_INFO_ENDPOINT = "https://www.csindex.com.cn/csindex-home/indexInfo/index-basic-info"
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
_NAME_TOKENS = {
    "energy": ("能源",),
    "materials": ("原材料", "材料"),
    "industrials": ("工业",),
    "consumer_discretionary": ("可选消费",),
    "consumer_staples": ("主要消费",),
    "health_care": ("医药卫生", "医药"),
    "financials": ("金融",),
    "information_technology": ("信息技术",),
    "communication_services": ("通信服务", "通信"),
    "utilities": ("公用事业",),
    "real_estate": ("房地产",),
    "all_share_benchmark": ("中证全指",),
}


class _SourceOwnedHTTPS:
    def get(self, url: str) -> HTTPSResponse:
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "quant-research-os/1"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return HTTPSResponse(
                    final_url=str(response.geturl()),
                    status=int(response.status),
                    headers=dict(response.headers.items()),
                    body=response.read(),
                )
        except Exception as exc:
            raise classify_unexpected_error(exc) from exc


def _rows(payload: Any) -> list[Mapping[str, Any]]:
    value = payload
    if isinstance(value, Mapping) and "data" in value:
        value = value["data"]
    if isinstance(value, Mapping) and "list" in value:
        value = value["list"]
    if isinstance(value, Mapping):
        value = [value]
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ProviderQueryError("CSI response does not contain a strict data row array")
    return list(value)


def _field(row: Mapping[str, Any], names: tuple[str, ...], *, required: bool = True) -> Any:
    found = [row[name] for name in names if name in row]
    if len(found) > 1 and len({str(item) for item in found}) != 1:
        raise ProviderQueryError("CSI response carries conflicting field aliases")
    if not found:
        if required:
            raise ProviderQueryError(f"CSI response is missing required field {names[0]}")
        return None
    return found[0]


class CSIOfficialProvider:
    provider_id = "csi_official"
    source_id = "csi_official"
    adapter_version = "csi-official-adapter-v1"
    upstream_source = "csindex.source_owned_https"
    supported_datasets = frozenset({INDEX_LEVEL, CSI_INDUSTRY_UNIVERSE})

    def __init__(
        self,
        *,
        transport: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._transport = _SourceOwnedHTTPS() if transport is None else transport
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _aware_clock(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ProviderQueryError("CSI provider clock must include a timezone offset")
        return value

    def _get(self, endpoint_url: str) -> tuple[HTTPSResponse, dict[str, Any]]:
        if urlsplit(endpoint_url).scheme != "https" or urlsplit(endpoint_url).hostname != _ALLOWED_HOST:
            raise ProviderQueryError("CSI endpoint is outside the fixed HTTPS host allowlist")
        getter = getattr(self._transport, "get", None)
        if not callable(getter):
            raise ProviderQueryError("CSI transport must expose get(url)")
        try:
            response = getter(endpoint_url)
        except (NetworkBlockedError, ProviderQueryError):
            raise
        except Exception as exc:
            raise classify_unexpected_error(exc) from exc
        if not isinstance(response, HTTPSResponse):
            raise ProviderQueryError("CSI transport returned the wrong response type")
        final = urlsplit(response.final_url)
        if final.scheme != "https" or final.hostname != _ALLOWED_HOST:
            raise ProviderQueryError("CSI redirect left the official HTTPS host")
        if response.status != 200:
            raise ProviderQueryError(f"CSI HTTPS status is {response.status}", raw_content=response.body)
        content_type = next(
            (value for key, value in response.headers.items() if key.casefold() == "content-type"),
            "",
        ).casefold()
        if "json" not in content_type:
            raise ProviderQueryError("CSI response Content-Type is not JSON", raw_content=response.body)
        fetched_at = self._aware_clock()
        normalized_headers = {
            str(key).casefold(): str(value).strip()
            for key, value in response.headers.items()
            if str(key).casefold() in _SAFE_RESPONSE_HEADERS
        }
        receipt = {
            "source_id": self.source_id,
            "endpoint_url": endpoint_url,
            "final_url": response.final_url,
            "final_host": final.hostname,
            "http_status": response.status,
            "response_headers_sha256": sha256_bytes(canonical_json_bytes(normalized_headers)),
            "body_sha256": sha256_bytes(response.body),
            "fetched_at": fetched_at.isoformat(),
            "transport_mode": (
                "source_owned_https"
                if type(self._transport) is _SourceOwnedHTTPS
                else "test_injected_https"
            ),
        }
        return response, receipt

    def fetch(self, request: IndexEvidenceRequest) -> IndexSourcePayload:
        if request.dataset_type not in self.supported_datasets:
            raise UnsupportedDatasetError(f"CSI official provider does not support {request.dataset_type!r}")
        if request.retrieval_mode == "offline_replay":
            raise UnsupportedDatasetError("CSI online provider cannot perform offline replay")
        captures: list[dict[str, Any]] = []
        receipts: list[dict[str, Any]] = []
        records: list[dict[str, Any]] = []
        names: dict[str, str] = {}
        semantics = {
            row["index_id"]: row["industry_key"]
            for row in frozen_universe_records(
                available_at=self._aware_clock().isoformat(),
                source_status="unverified_until_probe",
            )
        }
        for index_id in request.index_ids:
            code = index_id[:6]
            if request.dataset_type == INDEX_LEVEL:
                query = urllib.parse.urlencode(
                    {
                        "indexCode": code,
                        "startDate": request.start_date.strftime("%Y%m%d"),  # type: ignore[union-attr]
                        "endDate": request.end_date.strftime("%Y%m%d"),  # type: ignore[union-attr]
                    }
                )
                endpoint = f"{_PERFORMANCE_ENDPOINT}?{query}"
            else:
                endpoint = f"{_BASIC_INFO_ENDPOINT}/{code}"
            response, receipt = self._get(endpoint)
            payload = strict_json(response.body, "CSI response")
            rows = _rows(payload)
            receipts.append(receipt)
            captures.append(
                {
                    "index_id": index_id,
                    "body_base64": base64.b64encode(response.body).decode("ascii"),
                    "body_sha256": receipt["body_sha256"],
                    "response_headers": {
                        str(key).casefold(): str(value).strip()
                        for key, value in response.headers.items()
                        if str(key).casefold() in _SAFE_RESPONSE_HEADERS
                    },
                }
            )
            if request.dataset_type == INDEX_LEVEL:
                seen_dates: set[str] = set()
                for raw in rows:
                    returned_code = str(_field(raw, ("indexCode", "index_code", "indexcode"))).strip()
                    if returned_code != code:
                        raise ProviderQueryError("CSI performance response returned another index code")
                    day = str(_field(raw, ("tradeDate", "trading_date", "date"))).strip()
                    try:
                        parsed_day = datetime.strptime(
                            day.replace("/", "-"),
                            "%Y%m%d" if re.fullmatch(r"[0-9]{8}", day) else "%Y-%m-%d",
                        ).date()
                    except ValueError as exc:
                        raise ProviderQueryError("CSI performance response has an invalid date") from exc
                    day = parsed_day.isoformat()
                    if day in seen_dates:
                        raise ProviderQueryError("CSI performance response contains a duplicate date")
                    seen_dates.add(day)
                    raw_open = _field(raw, ("open", "openValue", "open_value"), required=False)
                    raw_high = _field(raw, ("high", "highValue", "high_value"), required=False)
                    raw_low = _field(raw, ("low", "lowValue", "low_value"), required=False)
                    optional_prices = (raw_open, raw_high, raw_low)
                    if any(item in (None, "") for item in optional_prices) and any(
                        item not in (None, "") for item in optional_prices
                    ):
                        raise ProviderQueryError("CSI open/high/low are partially missing")
                    records.append(
                        {
                            "schema_version": "index-level-v1",
                            "index_id": index_id,
                            "trading_date": day,
                            "open": None if raw_open in (None, "") else str(raw_open).strip(),
                            "high": None if raw_high in (None, "") else str(raw_high).strip(),
                            "low": None if raw_low in (None, "") else str(raw_low).strip(),
                            "close": str(_field(raw, ("close", "closeValue", "close_value"))).strip(),
                            "currency": "CNY",
                            "basis": "index_points_unadjusted",
                            "available_at": f"{day}T15:30:00+08:00",
                            "availability_status": "policy_estimated",
                            "source_record_id": f"csi-index-perf:{code}:{day}",
                        }
                    )
            else:
                if len(rows) != 1:
                    raise ProviderQueryError("CSI basic-info response must contain exactly one row")
                raw = rows[0]
                returned_code = str(_field(raw, ("indexCode", "index_code", "indexcode"))).strip()
                if returned_code != code:
                    raise ProviderQueryError("CSI basic-info response returned another index code")
                name = str(_field(raw, ("indexName", "index_name", "indexname"))).strip()
                if not name:
                    raise ProviderQueryError("CSI basic-info response has an empty name")
                if not any(token in name for token in _NAME_TOKENS[str(semantics[index_id])]):
                    raise ProviderQueryError("CSI basic-info name does not confirm the frozen semantic role")
                names[index_id] = name
        fetched_at = self._aware_clock()
        if request.dataset_type == CSI_INDUSTRY_UNIVERSE:
            records = list(
                frozen_universe_records(
                    available_at=fetched_at.isoformat(),
                    source_status="verified_official_basic_info",
                    official_names=names,
                )
            )
        if request.dataset_type == INDEX_LEVEL:
            records.sort(
                key=lambda row: (str(row["index_id"]), str(row["trading_date"]))
            )
        raw_content = canonical_json_bytes(
            {
                "contract_version": "csi-official-raw-v1",
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
            point_in_time_status=(
                "historical_backfill_not_original_capture"
                if request.dataset_type == INDEX_LEVEL
                else "current_snapshot_not_pit"
            ),
            capture_mode=(
                "source_owned_https"
                if type(self._transport) is _SourceOwnedHTTPS
                else "test_injected_https"
            ),
            transport_receipts=tuple(receipts),
            _authority=(
                _CSI_AUTHORITY if type(self._transport) is _SourceOwnedHTTPS else None
            ),
        )

    def _attests_source_owned(self, payload: IndexSourcePayload) -> bool:
        return (
            type(self._transport) is _SourceOwnedHTTPS
            and payload._authority is _CSI_AUTHORITY
            and payload.capture_mode == "source_owned_https"
        )


__all__ = ["CSIOfficialProvider"]
