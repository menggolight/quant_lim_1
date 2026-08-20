"""Strict, content-addressed evidence for the CSI factor research boundary.

This module is intentionally separate from the general market-data V1 registry:
the latter has no executable contracts for index levels, a point-in-time CSI
universe, or an official exchange session calendar.  Official admission can
only be minted by :class:`IndexEvidenceService.capture`, which invokes one of
the exact built-in providers.  Caller supplied bytes, provider names, hashes,
or ``official=True`` flags are not accepted.
"""

from __future__ import annotations

import base64
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from .contracts import aware_datetime, canonical_json_bytes, sha256_bytes
from .validation import SchemaValidationError, validate_json_schema


INDEX_LEVEL = "index_level"
CSI_INDUSTRY_UNIVERSE = "csi_industry_universe"
CN_EQUITY_SESSION = "cn_equity_session"
DATASET_TYPES = frozenset({INDEX_LEVEL, CSI_INDUSTRY_UNIVERSE, CN_EQUITY_SESSION})
SCHEMA_VERSIONS = {
    INDEX_LEVEL: "index-level-v1",
    CSI_INDUSTRY_UNIVERSE: "csi-industry-universe-v1",
    CN_EQUITY_SESSION: "cn-equity-session-v1",
}

CHINA_OFFSET = "+08:00"
UNIVERSE_VERSION = "csi-level1-screen-confirm-v1"
BENCHMARK_INDEX_ID = "000985.CSI"

# These codes and semantics are frozen by the current research decision.  They
# remain unverified until the CSI basic-info transport confirms every row.
SCREEN_INDUSTRIES = (
    ("000986.CSI", "energy", "能源"),
    ("000987.CSI", "materials", "原材料"),
    ("000988.CSI", "industrials", "工业"),
    ("000989.CSI", "consumer_discretionary", "可选消费"),
    ("000990.CSI", "consumer_staples", "主要消费"),
    ("000991.CSI", "health_care", "医药卫生"),
    ("932075.CSI", "financials", "金融"),
    ("000993.CSI", "information_technology", "信息技术"),
    ("000994.CSI", "communication_services", "通信服务"),
    ("000995.CSI", "utilities", "公用事业"),
    ("932076.CSI", "real_estate", "房地产"),
)
CONFIRM_INDUSTRIES = (
    ("932077.CSI", "energy", "能源"),
    ("932078.CSI", "materials", "原材料"),
    ("932079.CSI", "industrials", "工业"),
    ("932080.CSI", "consumer_discretionary", "可选消费"),
    ("932081.CSI", "consumer_staples", "主要消费"),
    ("932082.CSI", "health_care", "医药卫生"),
    ("932083.CSI", "financials", "金融"),
    ("932084.CSI", "information_technology", "信息技术"),
    ("932085.CSI", "communication_services", "通信服务"),
    ("932086.CSI", "utilities", "公用事业"),
    ("931775.CSI", "real_estate", "房地产"),
)
SCREEN_INDEX_IDS = tuple(item[0] for item in SCREEN_INDUSTRIES)
CONFIRM_INDEX_IDS = tuple(item[0] for item in CONFIRM_INDUSTRIES)
ALL_INDEX_IDS = SCREEN_INDEX_IDS + CONFIRM_INDEX_IDS + (BENCHMARK_INDEX_ID,)
CSI_INDEX_WHITELIST = frozenset(ALL_INDEX_IDS)
if len(ALL_INDEX_IDS) != 23 or len(CSI_INDEX_WHITELIST) != 23:  # pragma: no cover
    raise RuntimeError("the frozen CSI V1 whitelist must contain 23 unique IDs")

_INDEX_ID = re.compile(r"^[0-9]{6}\.CSI$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
_SCHEMA_ROOT = Path(__file__).resolve().parents[2] / "schemas"
_SCHEMA_PATHS = {
    INDEX_LEVEL: _SCHEMA_ROOT / "index_level.v1.json",
    CSI_INDUSTRY_UNIVERSE: _SCHEMA_ROOT / "csi_industry_universe.v1.json",
    CN_EQUITY_SESSION: _SCHEMA_ROOT / "cn_equity_session.v1.json",
}
_WRITE_PERMIT = object()


class IndexEvidenceError(ValueError):
    """The strict index evidence contract was not satisfied."""


class IndexEvidenceStorageError(IndexEvidenceError):
    """Stored raw, normalized, or receipt evidence is missing or inconsistent."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise IndexEvidenceError(f"duplicate JSON key is forbidden: {key}")
        value[key] = item
    return value


def strict_json(raw: bytes, label: str) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                IndexEvidenceError(f"non-finite JSON constant: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IndexEvidenceError(f"{label} is not strict UTF-8 JSON") from exc


def _date(value: Any, field_name: str, *, required: bool = True) -> date | None:
    if value in (None, "") and not required:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise IndexEvidenceError(f"{field_name} must be an ISO date") from exc


def _index_ids(values: Sequence[str] | None, *, default_all: bool) -> tuple[str, ...]:
    supplied = ALL_INDEX_IDS if not values and default_all else tuple(values or ())
    normalized = tuple(str(item).strip().upper() for item in supplied)
    if len(normalized) != len(set(normalized)):
        raise IndexEvidenceError("index_ids must be unique")
    if any(_INDEX_ID.fullmatch(item) is None for item in normalized):
        raise IndexEvidenceError("index_ids must use canonical NNNNNN.CSI form")
    unknown = sorted(set(normalized) - CSI_INDEX_WHITELIST)
    if unknown:
        raise IndexEvidenceError(f"index_ids are outside the frozen CSI whitelist: {unknown}")
    # Caller order is not semantic.  Persist the frozen semantic order.
    selected = set(normalized)
    return tuple(item for item in ALL_INDEX_IDS if item in selected)


@dataclass(frozen=True)
class IndexEvidenceRequest:
    dataset_type: str
    requested_at: datetime
    retrieval_mode: str
    index_ids: Sequence[str] = field(default_factory=tuple)
    start_date: date | str | None = None
    end_date: date | str | None = None
    evidence_cutoff_at: datetime | None = None

    def __post_init__(self) -> None:
        dataset = str(self.dataset_type).strip()
        if dataset not in DATASET_TYPES:
            raise IndexEvidenceError(f"unsupported index dataset: {dataset!r}")
        object.__setattr__(self, "dataset_type", dataset)
        requested = aware_datetime(self.requested_at, "requested_at")
        object.__setattr__(self, "requested_at", requested)
        mode = str(self.retrieval_mode).strip()
        if mode not in {"live_capture", "historical_backfill", "offline_replay"}:
            raise IndexEvidenceError("unsupported retrieval_mode")
        object.__setattr__(self, "retrieval_mode", mode)
        cutoff = (
            aware_datetime(self.evidence_cutoff_at, "evidence_cutoff_at")
            if self.evidence_cutoff_at is not None
            else None
        )
        if mode == "offline_replay":
            if cutoff is None:
                cutoff = requested
        elif cutoff is not None:
            raise IndexEvidenceError("evidence_cutoff_at is only valid for offline_replay")
        if cutoff is not None and cutoff > requested:
            raise IndexEvidenceError("evidence_cutoff_at cannot follow requested_at")
        object.__setattr__(self, "evidence_cutoff_at", cutoff)

        start = _date(self.start_date, "start_date", required=False)
        end = _date(self.end_date, "end_date", required=False)
        if dataset in {INDEX_LEVEL, CN_EQUITY_SESSION} and (start is None or end is None):
            raise IndexEvidenceError(f"{dataset} requires start_date and end_date")
        if dataset == CSI_INDUSTRY_UNIVERSE and (start is not None or end is not None):
            raise IndexEvidenceError("csi_industry_universe does not accept a date window")
        if start is not None and end is not None and start > end:
            raise IndexEvidenceError("start_date cannot follow end_date")
        object.__setattr__(self, "start_date", start)
        object.__setattr__(self, "end_date", end)

        if dataset == CN_EQUITY_SESSION:
            if self.index_ids:
                raise IndexEvidenceError("cn_equity_session does not accept index_ids")
            ids: tuple[str, ...] = ()
        else:
            ids = _index_ids(self.index_ids, default_all=True)
            if dataset == CSI_INDUSTRY_UNIVERSE and ids != ALL_INDEX_IDS:
                raise IndexEvidenceError("CSI universe capture requires all 23 frozen IDs")
        object.__setattr__(self, "index_ids", ids)

    def fingerprint_payload(self, provider_id: str, adapter_version: str) -> dict[str, Any]:
        return {
            "provider_id": str(provider_id),
            "adapter_version": str(adapter_version),
            "dataset_type": self.dataset_type,
            "schema_version": SCHEMA_VERSIONS[self.dataset_type],
            "index_ids": list(self.index_ids),
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
        }

    def fingerprint(self, provider_id: str, adapter_version: str) -> str:
        return sha256_bytes(canonical_json_bytes(self.fingerprint_payload(provider_id, adapter_version)))


@dataclass(frozen=True)
class HTTPSResponse:
    """A transport return value; constructing it does not attest provenance."""

    final_url: str
    status: int
    headers: Mapping[str, str]
    body: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.body, bytes):
            raise IndexEvidenceError("HTTPS response body must be bytes")
        if type(self.status) is not int:
            raise IndexEvidenceError("HTTPS response status must be an integer")
        object.__setattr__(self, "headers", MappingProxyType({str(k): str(v) for k, v in self.headers.items()}))


@dataclass(frozen=True)
class IndexSourcePayload:
    raw_content: bytes
    records: tuple[Mapping[str, Any], ...]
    fetched_at: datetime
    upstream_source: str
    point_in_time_status: str
    capture_mode: str
    transport_receipts: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    _authority: object | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.raw_content, bytes):
            raise IndexEvidenceError("raw_content must be bytes")
        if not isinstance(self.records, tuple) or any(not isinstance(item, Mapping) for item in self.records):
            raise IndexEvidenceError("records must be a tuple of objects")
        object.__setattr__(self, "records", tuple(dict(item) for item in self.records))
        object.__setattr__(self, "fetched_at", aware_datetime(self.fetched_at, "fetched_at"))
        if not str(self.upstream_source).strip():
            raise IndexEvidenceError("upstream_source must not be empty")
        if self.capture_mode not in {
            "licensed_read_only_secondary", "source_owned_https", "test_injected_https"
        }:
            raise IndexEvidenceError("unsupported capture_mode")
        object.__setattr__(self, "transport_receipts", tuple(dict(item) for item in self.transport_receipts))

    @property
    def availability_status(self) -> str:
        """Aggregate record availability without accepting a caller assertion."""

        statuses = {str(item.get("availability_status") or "") for item in self.records}
        if len(statuses) != 1 or "" in statuses:
            return "mixed_or_unknown"
        return next(iter(statuses))


def _positive_decimal(value: Any, field_name: str) -> str:
    if isinstance(value, bool) or value is None:
        raise IndexEvidenceError(f"{field_name} must be a positive decimal")
    text = str(value).strip()
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise IndexEvidenceError(f"{field_name} is not a decimal") from exc
    if not number.is_finite() or number <= 0:
        raise IndexEvidenceError(f"{field_name} must be positive and finite")
    return format(number, "f")


def _validate_records(request: IndexEvidenceRequest, records: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    normalized = tuple(dict(item) for item in records)
    if not normalized:
        raise IndexEvidenceError(f"{request.dataset_type} result is empty")
    try:
        for record in normalized:
            validate_json_schema(record, _SCHEMA_PATHS[request.dataset_type])
    except SchemaValidationError as exc:
        raise IndexEvidenceError(f"normalized record Schema failed: {exc}") from exc

    if request.dataset_type == INDEX_LEVEL:
        keys: set[tuple[str, str]] = set()
        prior: tuple[str, str] | None = None
        requested_ids = set(request.index_ids)
        seen_ids: set[str] = set()
        for row in normalized:
            index_id = str(row["index_id"])
            day = _date(row["trading_date"], "trading_date")
            assert day is not None
            if index_id not in requested_ids:
                raise IndexEvidenceError("index level returned an unrequested index")
            if request.start_date and day < request.start_date or request.end_date and day > request.end_date:
                raise IndexEvidenceError("index level is outside the requested window")
            _positive_decimal(row["close"], "close")
            for field_name in ("open", "high", "low"):
                if row[field_name] is not None:
                    _positive_decimal(row[field_name], field_name)
            present_ohlc = [row[field_name] for field_name in ("open", "high", "low")]
            if any(item is None for item in present_ohlc) and any(item is not None for item in present_ohlc):
                raise IndexEvidenceError("open/high/low must be all present or all null")
            if all(item is not None for item in present_ohlc):
                opening = Decimal(str(row["open"]))
                high = Decimal(str(row["high"]))
                low = Decimal(str(row["low"]))
                close = Decimal(str(row["close"]))
                if high < max(opening, low, close) or low > min(opening, high, close):
                    raise IndexEvidenceError("index OHLC relationships are invalid")
            available = aware_datetime(row["available_at"], "available_at")
            if row["availability_status"] == "policy_estimated":
                expected = f"{day.isoformat()}T15:30:00+08:00"
                if available.isoformat() != expected:
                    raise IndexEvidenceError("policy-estimated index level must use 15:30 Asia/Shanghai")
            key = (index_id, day.isoformat())
            if key in keys or prior is not None and key <= prior:
                raise IndexEvidenceError("index levels must be unique and sorted by index/date")
            keys.add(key)
            prior = key
            seen_ids.add(index_id)
        if seen_ids != requested_ids:
            raise IndexEvidenceError("index level response does not cover every requested index")
    elif request.dataset_type == CSI_INDUSTRY_UNIVERSE:
        if len(normalized) != 23 or {str(row["index_id"]) for row in normalized} != CSI_INDEX_WHITELIST:
            raise IndexEvidenceError("CSI universe must contain the exact 23-ID whitelist")
        expected = frozen_universe_records(
            available_at=str(normalized[0]["available_at"]),
            source_status=str(normalized[0]["source_status"]),
            official_names={str(row["index_id"]): str(row["official_index_name"]) for row in normalized},
        )
        if tuple(normalized) != expected:
            raise IndexEvidenceError("CSI universe roles or semantic mapping differ from the frozen V1 contract")
    else:
        assert request.start_date is not None and request.end_date is not None
        expected_days: list[date] = []
        current = request.start_date
        while current <= request.end_date:
            expected_days.append(current)
            current += timedelta(days=1)
        actual = [_date(row["calendar_date"], "calendar_date") for row in normalized]
        if actual != expected_days:
            raise IndexEvidenceError("SSE calendar must cover every requested natural date exactly once")
        for row in normalized:
            if type(row["is_trading_day"]) is not bool:
                raise IndexEvidenceError("is_trading_day must be a boolean")
            open_expected = f"{row['calendar_date']}T09:30:00+08:00" if row["is_trading_day"] else None
            close_expected = f"{row['calendar_date']}T15:00:00+08:00" if row["is_trading_day"] else None
            if row["session_open_at"] != open_expected or row["session_close_at"] != close_expected:
                raise IndexEvidenceError("SSE session times do not match the strict CN equity contract")
    return normalized


def frozen_universe_records(
    *,
    available_at: str,
    source_status: str = "unverified_until_probe",
    official_names: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], ...]:
    aware = aware_datetime(available_at, "available_at").isoformat()
    if source_status not in {"unverified_until_probe", "verified_official_basic_info"}:
        raise IndexEvidenceError("unsupported CSI universe source_status")
    names = dict(official_names or {})
    records: list[dict[str, Any]] = []
    for role, rows in (("screen", SCREEN_INDUSTRIES), ("confirm", CONFIRM_INDUSTRIES)):
        for order, (index_id, key, name) in enumerate(rows, start=1):
            records.append(
                {
                    "schema_version": SCHEMA_VERSIONS[CSI_INDUSTRY_UNIVERSE],
                    "universe_version": UNIVERSE_VERSION,
                    "index_id": index_id,
                    "official_index_code": index_id[:6],
                    "official_index_name": names.get(index_id, name),
                    "industry_key": key,
                    "industry_name_cn": name,
                    "series_role": role,
                    "semantic_order": order,
                    "source_id": "csi_official",
                    "source_status": source_status,
                    "available_at": aware,
                    "source_record_id": f"csi-basic-info:{index_id[:6]}:{source_status}",
                }
            )
    records.append(
        {
            "schema_version": SCHEMA_VERSIONS[CSI_INDUSTRY_UNIVERSE],
            "universe_version": UNIVERSE_VERSION,
            "index_id": BENCHMARK_INDEX_ID,
            "official_index_code": "000985",
            "official_index_name": names.get(BENCHMARK_INDEX_ID, "中证全指"),
            "industry_key": "all_share_benchmark",
            "industry_name_cn": "中证全指",
            "series_role": "benchmark",
            "semantic_order": 0,
            "source_id": "csi_official",
            "source_status": source_status,
            "available_at": aware,
            "source_record_id": f"csi-basic-info:000985:{source_status}",
        }
    )
    return tuple(records)


@dataclass(frozen=True)
class IndexEvidenceBundle:
    source_id: str
    provider_id: str
    adapter_version: str
    dataset_type: str
    request_fingerprint: str
    request_payload: Mapping[str, Any]
    retrieval_mode: str
    fetched_at: datetime
    raw_content_sha256: str
    normalized_content_sha256: str
    admission_status: str
    point_in_time_status: str
    transport_receipts: tuple[Mapping[str, Any], ...]
    records: tuple[Mapping[str, Any], ...]
    evidence_id: str = ""
    schema_version: str = "index-evidence-bundle-v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "fetched_at", aware_datetime(self.fetched_at, "fetched_at"))
        for name in ("raw_content_sha256", "normalized_content_sha256", "request_fingerprint"):
            if _SHA256.fullmatch(str(getattr(self, name))) is None:
                raise IndexEvidenceError(f"{name} must be a lowercase SHA-256")
        object.__setattr__(self, "request_payload", MappingProxyType(dict(self.request_payload)))
        object.__setattr__(self, "transport_receipts", tuple(dict(item) for item in self.transport_receipts))
        object.__setattr__(self, "records", tuple(dict(item) for item in self.records))
        expected = sha256_bytes(canonical_json_bytes(self._identity_payload()))
        if self.evidence_id and self.evidence_id != expected:
            raise IndexEvidenceError("evidence_id does not match immutable bundle content")
        object.__setattr__(self, "evidence_id", expected)

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "provider_id": self.provider_id,
            "adapter_version": self.adapter_version,
            "dataset_type": self.dataset_type,
            "request_fingerprint": self.request_fingerprint,
            "request_payload": dict(self.request_payload),
            "retrieval_mode": self.retrieval_mode,
            "fetched_at": self.fetched_at.isoformat(),
            "raw_content_sha256": self.raw_content_sha256,
            "normalized_content_sha256": self.normalized_content_sha256,
            "record_count": len(self.records),
            "completeness_status": "complete",
            "admission_status": self.admission_status,
            "point_in_time_status": self.point_in_time_status,
            "transport_receipts": [dict(item) for item in self.transport_receipts],
            "records": [dict(item) for item in self.records],
        }

    def to_dict(self) -> dict[str, Any]:
        return {"evidence_id": self.evidence_id, **self._identity_payload()}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "IndexEvidenceBundle":
        expected = {
            "schema_version", "evidence_id", "source_id", "provider_id", "adapter_version",
            "dataset_type", "request_fingerprint", "request_payload", "retrieval_mode", "fetched_at",
            "raw_content_sha256", "normalized_content_sha256", "record_count", "completeness_status",
            "admission_status", "point_in_time_status", "transport_receipts", "records",
        }
        if set(value) != expected or value.get("schema_version") != "index-evidence-bundle-v1":
            raise IndexEvidenceError("stored bundle envelope is malformed")
        records = value.get("records")
        receipts = value.get("transport_receipts")
        if not isinstance(records, list) or not isinstance(receipts, list):
            raise IndexEvidenceError("stored bundle arrays are malformed")
        if value.get("record_count") != len(records) or value.get("completeness_status") != "complete":
            raise IndexEvidenceError("stored bundle completeness metadata is invalid")
        return cls(
            evidence_id=str(value["evidence_id"]),
            source_id=str(value["source_id"]), provider_id=str(value["provider_id"]),
            adapter_version=str(value["adapter_version"]), dataset_type=str(value["dataset_type"]),
            request_fingerprint=str(value["request_fingerprint"]),
            request_payload=value["request_payload"], retrieval_mode=str(value["retrieval_mode"]),
            fetched_at=aware_datetime(value["fetched_at"], "fetched_at"),
            raw_content_sha256=str(value["raw_content_sha256"]),
            normalized_content_sha256=str(value["normalized_content_sha256"]),
            admission_status=str(value["admission_status"]),
            point_in_time_status=str(value["point_in_time_status"]),
            transport_receipts=tuple(receipts), records=tuple(records),
        )


class IndexEvidenceStorage:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()

    @staticmethod
    def _segment(value: str) -> str:
        text = str(value)
        if _SAFE_SEGMENT.fullmatch(text) is None:
            raise IndexEvidenceStorageError("unsafe storage segment")
        return text

    @staticmethod
    def _atomic(path: Path, body: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() != body:
                raise IndexEvidenceStorageError("refusing to replace non-identical evidence")
            return
        descriptor, temporary_name = tempfile.mkstemp(prefix=".ie-", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != body:
                    raise IndexEvidenceStorageError("concurrent non-identical evidence collision")
        finally:
            temporary.unlink(missing_ok=True)

    def _bucket(self, bundle: IndexEvidenceBundle) -> Path:
        return self.root / "evidence" / self._segment(bundle.provider_id) / self._segment(bundle.dataset_type) / bundle.request_fingerprint

    def persist(self, bundle: IndexEvidenceBundle, raw: bytes, *, _permit: object | None = None) -> Path:
        if _permit is not _WRITE_PERMIT:
            raise IndexEvidenceStorageError("evidence must be written through IndexEvidenceService.capture")
        if sha256_bytes(raw) != bundle.raw_content_sha256:
            raise IndexEvidenceStorageError("raw hash differs from bundle")
        path = self._bucket(bundle) / f"{bundle.evidence_id}.json"
        raw_path = self.root / "raw" / f"{bundle.raw_content_sha256}.raw"
        receipt_path = path.with_suffix(".receipt")
        body = canonical_json_bytes(bundle.to_dict())
        receipt = {
            "receipt_version": "index-evidence-storage-receipt-v1",
            "writer": "IndexEvidenceService",
            "evidence_id": bundle.evidence_id,
            "bundle_sha256": sha256_bytes(body),
            "raw_content_sha256": bundle.raw_content_sha256,
            "normalized_content_sha256": bundle.normalized_content_sha256,
            "provider_identity": f"{bundle.provider_id}:{bundle.adapter_version}",
            "transport_receipts_sha256": sha256_bytes(canonical_json_bytes([dict(item) for item in bundle.transport_receipts])),
            "admission_status": bundle.admission_status,
        }
        self._atomic(raw_path, raw)
        self._atomic(path, body)
        self._atomic(receipt_path, canonical_json_bytes(receipt))
        return path

    def load(self, provider_id: str, request_fingerprint: str, dataset_type: str, *, as_of: datetime) -> tuple[IndexEvidenceBundle, bytes, Path]:
        bucket = self.root / "evidence" / self._segment(provider_id) / self._segment(dataset_type) / request_fingerprint
        candidates: list[tuple[IndexEvidenceBundle, Path]] = []
        for path in bucket.glob("*.json") if bucket.is_dir() else ():
            raw_value = strict_json(path.read_bytes(), "stored bundle")
            if not isinstance(raw_value, Mapping):
                raise IndexEvidenceStorageError("stored bundle must be an object")
            bundle = IndexEvidenceBundle.from_dict(raw_value)
            if bundle.fetched_at <= as_of:
                candidates.append((bundle, path))
        if not candidates:
            raise IndexEvidenceStorageError("offline replay cache miss at or before as_of")
        bundle, path = max(candidates, key=lambda item: (item[0].fetched_at, item[0].evidence_id))
        raw_path = self.root / "raw" / f"{bundle.raw_content_sha256}.raw"
        receipt_path = path.with_suffix(".receipt")
        if not raw_path.is_file() or not receipt_path.is_file():
            raise IndexEvidenceStorageError("stored bundle is missing raw evidence or receipt")
        raw = raw_path.read_bytes()
        if sha256_bytes(raw) != bundle.raw_content_sha256:
            raise IndexEvidenceStorageError("stored raw evidence hash mismatch")
        if sha256_bytes(canonical_json_bytes([dict(item) for item in bundle.records])) != bundle.normalized_content_sha256:
            raise IndexEvidenceStorageError("stored normalized evidence hash mismatch")
        receipt = strict_json(receipt_path.read_bytes(), "storage receipt")
        expected_receipt = {
            "receipt_version": "index-evidence-storage-receipt-v1",
            "writer": "IndexEvidenceService",
            "evidence_id": bundle.evidence_id,
            "bundle_sha256": sha256_bytes(path.read_bytes()),
            "raw_content_sha256": bundle.raw_content_sha256,
            "normalized_content_sha256": bundle.normalized_content_sha256,
            "provider_identity": f"{bundle.provider_id}:{bundle.adapter_version}",
            "transport_receipts_sha256": sha256_bytes(canonical_json_bytes([dict(item) for item in bundle.transport_receipts])),
            "admission_status": bundle.admission_status,
        }
        if receipt != expected_receipt:
            raise IndexEvidenceStorageError("storage receipt does not match bundle evidence")
        _validate_transport_receipts(bundle)
        _validate_raw_transport_binding(bundle, raw)
        for record in bundle.records:
            available = record.get("available_at")
            if available and aware_datetime(available, "record.available_at") > as_of:
                raise IndexEvidenceStorageError("offline bundle contains evidence unavailable at as_of")
        return bundle, raw, path


def _validate_transport_receipts(bundle: IndexEvidenceBundle) -> None:
    official_hosts = {
        "csi_official": frozenset({"www.csindex.com.cn"}),
        "sse_calendar": frozenset({"www.sse.com.cn"}),
    }
    if bundle.provider_id not in official_hosts:
        if bundle.transport_receipts:
            raise IndexEvidenceStorageError("non-official provider cannot carry HTTPS attestation receipts")
        return
    if not bundle.transport_receipts:
        raise IndexEvidenceStorageError("official bundle is missing HTTPS transport receipts")
    allowed = official_hosts[bundle.provider_id]
    for receipt in bundle.transport_receipts:
        required = {
            "source_id", "endpoint_url", "final_url", "final_host", "http_status",
            "response_headers_sha256", "body_sha256", "fetched_at", "transport_mode",
        }
        if set(receipt) != required:
            raise IndexEvidenceStorageError("official transport receipt is malformed")
        endpoint = urlsplit(str(receipt["endpoint_url"]))
        final = urlsplit(str(receipt["final_url"]))
        if endpoint.scheme != "https" or final.scheme != "https" or final.hostname not in allowed:
            raise IndexEvidenceStorageError("official receipt URL/host is outside the allowlist")
        if receipt["final_host"] != final.hostname or receipt["http_status"] != 200:
            raise IndexEvidenceStorageError("official receipt final host/status mismatch")
        for field_name in ("response_headers_sha256", "body_sha256"):
            if _SHA256.fullmatch(str(receipt[field_name])) is None:
                raise IndexEvidenceStorageError("official receipt hashes are invalid")
        expected_mode = (
            "source_owned_https"
            if bundle.admission_status == "admitted_for_research"
            else "test_injected_https"
        )
        if receipt["transport_mode"] != expected_mode:
            raise IndexEvidenceStorageError("official receipt mode disagrees with admission")


def _validate_raw_transport_binding(bundle: IndexEvidenceBundle, raw: bytes) -> None:
    """Bind official receipt hashes to stored response bytes and headers."""

    if bundle.provider_id not in {"csi_official", "sse_calendar"}:
        return
    payload = strict_json(raw, "official raw evidence")
    if not isinstance(payload, Mapping):
        raise IndexEvidenceStorageError("official raw evidence must be an object")
    if bundle.provider_id == "csi_official":
        if set(payload) != {"contract_version", "request", "captures", "transport_receipts"}:
            raise IndexEvidenceStorageError("CSI raw evidence envelope is malformed")
        captures = payload.get("captures")
        receipts = payload.get("transport_receipts")
        if not isinstance(captures, list) or not isinstance(receipts, list):
            raise IndexEvidenceStorageError("CSI raw evidence arrays are malformed")
    else:
        if set(payload) != {"contract_version", "request", "captures", "transport_receipts"}:
            raise IndexEvidenceStorageError("SSE raw evidence envelope is malformed")
        if payload.get("contract_version") != "sse-calendar-raw-v2":
            raise IndexEvidenceStorageError("SSE raw evidence contract version is unsupported")
        if payload.get("request") != bundle.request_payload:
            raise IndexEvidenceStorageError("SSE raw evidence request differs from the bundle")
        captures = payload.get("captures")
        receipts = payload.get("transport_receipts")
        if not isinstance(captures, list) or not isinstance(receipts, list):
            raise IndexEvidenceStorageError("SSE raw evidence arrays are malformed")
        seen_urls: set[str] = set()
        for capture, receipt in zip(captures, receipts, strict=False):
            if not isinstance(capture, Mapping) or set(capture) != {
                "role", "url", "body_base64", "response_headers"
            }:
                raise IndexEvidenceStorageError("SSE raw capture is malformed")
            url = str(capture.get("url"))
            if url in seen_urls or not str(capture.get("role") or "").strip():
                raise IndexEvidenceStorageError("SSE raw capture URL/role is duplicate or empty")
            seen_urls.add(url)
            if not isinstance(receipt, Mapping) or receipt.get("endpoint_url") != url:
                raise IndexEvidenceStorageError("SSE raw capture URL differs from its receipt")
    if receipts != [dict(item) for item in bundle.transport_receipts] or len(captures) != len(receipts):
        raise IndexEvidenceStorageError("raw transport receipts differ from the bundle")
    for capture, receipt in zip(captures, receipts, strict=True):
        if not isinstance(capture, Mapping) or not isinstance(receipt, Mapping):
            raise IndexEvidenceStorageError("official raw capture is malformed")
        try:
            body = base64.b64decode(str(capture["body_base64"]), validate=True)
        except Exception as exc:
            raise IndexEvidenceStorageError("official raw response body is not valid base64") from exc
        headers = capture.get("response_headers")
        if not isinstance(headers, Mapping):
            raise IndexEvidenceStorageError("official raw response headers are missing")
        normalized_headers = {str(key).casefold(): str(value).strip() for key, value in headers.items()}
        if sha256_bytes(body) != receipt.get("body_sha256"):
            raise IndexEvidenceStorageError("official response body hash mismatch")
        if sha256_bytes(canonical_json_bytes(normalized_headers)) != receipt.get("response_headers_sha256"):
            raise IndexEvidenceStorageError("official response header hash mismatch")


class IndexEvidenceService:
    """The only writer that can turn a built-in provider capture into a bundle."""

    def __init__(self, storage_root: Path | str) -> None:
        self.storage = IndexEvidenceStorage(storage_root)

    @staticmethod
    def _provider_contract(provider: Any) -> tuple[str, str, str]:
        from .providers.choice_index import ChoiceIndexProvider
        from .providers.csi_official import CSIOfficialProvider
        from .providers.sse_calendar import SSECalendarProvider

        exact = {
            ChoiceIndexProvider: ("choice_index", "choice_licensed_secondary", "choice-index-adapter-v1"),
            CSIOfficialProvider: ("csi_official", "csi_official", "csi-official-adapter-v1"),
            SSECalendarProvider: ("sse_calendar", "sse_official", "sse-calendar-adapter-v2"),
        }
        contract = exact.get(type(provider))
        if contract is None:
            raise IndexEvidenceError("IndexEvidenceService accepts exact built-in providers only")
        if (provider.provider_id, provider.source_id, provider.adapter_version) != contract:
            raise IndexEvidenceError("provider identity differs from the built-in contract")
        return contract

    def capture(self, provider: Any, request: IndexEvidenceRequest) -> IndexEvidenceBundle:
        if request.retrieval_mode == "offline_replay":
            raise IndexEvidenceError("capture does not accept offline_replay requests")
        provider_id, source_id, adapter_version = self._provider_contract(provider)
        payload = provider.fetch(request)
        if not isinstance(payload, IndexSourcePayload):
            raise IndexEvidenceError("built-in provider returned the wrong payload type")
        records = _validate_records(request, payload.records)
        if payload.fetched_at < request.requested_at:
            raise IndexEvidenceError("provider fetched_at precedes requested_at")
        official = provider_id in {"csi_official", "sse_calendar"}
        if official:
            source_owned = bool(provider._attests_source_owned(payload))
            admission = "admitted_for_research" if source_owned else "test_injected_not_admitted"
        else:
            if payload.capture_mode != "licensed_read_only_secondary":
                raise IndexEvidenceError("Choice index payload has an invalid capture mode")
            admission = "validated_secondary_not_primary"
        if request.dataset_type == CSI_INDUSTRY_UNIVERSE and admission == "admitted_for_research":
            if any(row["source_status"] != "verified_official_basic_info" for row in records):
                raise IndexEvidenceError("unverified CSI codebook cannot be admitted")
        bundle = IndexEvidenceBundle(
            source_id=source_id,
            provider_id=provider_id,
            adapter_version=adapter_version,
            dataset_type=request.dataset_type,
            request_fingerprint=request.fingerprint(provider_id, adapter_version),
            request_payload=request.fingerprint_payload(provider_id, adapter_version),
            retrieval_mode=request.retrieval_mode,
            fetched_at=payload.fetched_at,
            raw_content_sha256=sha256_bytes(payload.raw_content),
            normalized_content_sha256=sha256_bytes(canonical_json_bytes([dict(item) for item in records])),
            admission_status=admission,
            point_in_time_status=payload.point_in_time_status,
            transport_receipts=payload.transport_receipts,
            records=records,
        )
        _validate_transport_receipts(bundle)
        _validate_raw_transport_binding(bundle, payload.raw_content)
        self.storage.persist(bundle, payload.raw_content, _permit=_WRITE_PERMIT)
        return bundle

    def replay(self, provider_id: str, request: IndexEvidenceRequest, *, as_of: datetime | None = None) -> IndexEvidenceBundle:
        if request.retrieval_mode != "offline_replay":
            raise IndexEvidenceError("replay requires retrieval_mode=offline_replay")
        cutoff = aware_datetime(as_of or request.evidence_cutoff_at or request.requested_at, "as_of")
        adapter_versions = {
            "choice_index": "choice-index-adapter-v1",
            "csi_official": "csi-official-adapter-v1",
            "sse_calendar": "sse-calendar-adapter-v2",
        }
        adapter = adapter_versions.get(provider_id)
        if adapter is None:
            raise IndexEvidenceError("offline replay provider is not built in")
        fingerprint = request.fingerprint(provider_id, adapter)
        bundle, _, _ = self.storage.load(provider_id, fingerprint, request.dataset_type, as_of=cutoff)
        if bundle.request_payload != request.fingerprint_payload(provider_id, adapter):
            raise IndexEvidenceStorageError("offline bundle request payload mismatch")
        _validate_records(request, bundle.records)
        return bundle


def load_index_panel(
    storage_root: Path | str,
    provider_id: str,
    request: IndexEvidenceRequest,
    as_of: datetime,
) -> dict[str, Any]:
    """Return a strict JSON-serializable bundle for Factor Lab."""

    return IndexEvidenceService(storage_root).replay(
        provider_id, request, as_of=as_of
    ).to_dict()


__all__ = [
    "ALL_INDEX_IDS", "BENCHMARK_INDEX_ID", "CN_EQUITY_SESSION",
    "CONFIRM_INDEX_IDS", "CSI_INDEX_WHITELIST", "CSI_INDUSTRY_UNIVERSE",
    "HTTPSResponse", "INDEX_LEVEL", "IndexEvidenceBundle", "IndexEvidenceError",
    "IndexEvidenceRequest", "IndexEvidenceService", "IndexEvidenceStorageError",
    "IndexSourcePayload", "SCHEMA_VERSIONS", "SCREEN_INDEX_IDS", "UNIVERSE_VERSION",
    "frozen_universe_records", "load_index_panel", "strict_json",
]
