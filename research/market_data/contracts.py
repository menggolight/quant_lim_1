"""Versioned contracts shared by market-data providers and consumers."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from types import MappingProxyType
from typing import Any, Mapping, Sequence


DATASET_SCHEMA_VERSIONS = {
    "daily_bar": "daily-bar-v1",
    "trade_calendar": "trade-calendar-v1",
    "security_master": "security-master-v1",
    "industry_classification": "industry-classification-v1",
    "financial_indicator": "financial-indicator-v1",
}
RETRIEVAL_MODES = frozenset({"live_capture", "historical_backfill", "offline_replay"})
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")
COMPLETENESS_STATUSES = frozenset({"complete", "incomplete", "empty", "failed"})
FRESHNESS_STATUSES = frozenset(
    {
        "live_capture",
        "historical_backfill",
        "replayed",
        "current_snapshot",
        "not_assessed",
        "unknown",
        "stale",
    }
)
ADMISSION_STATUSES = frozenset(
    {
        "validated_research_only",
        "validated_secondary_not_primary",
        "validated_optional_source",
        "admitted_for_research",
        "diagnostic_only",
        "diagnostic_current_only",
        "research_only_unless_disclosure_time_present",
        "rejected_synthetic",
        "rejected_provider_not_allowlisted",
        "rejected_provider_disabled",
        "rejected_provider_dataset_undeclared",
        "rejected_unexpected_upstream",
        "quarantined",
        "not_admitted",
        "failed",
    }
)
POINT_IN_TIME_STATUSES = frozenset(
    {
        "policy_estimated_availability",
        "current_snapshot_not_pit",
        "historical_backfill_not_original_capture",
        "offline_replay_of_validated_capture",
        "not_applicable",
        "not_admitted",
        "unknown",
        "diagnostic_current_only",
        "research_only_not_pit",
    }
)
BATCH_FIELDS = frozenset(
    {
        "batch_id",
        "provider_id",
        "upstream_source",
        "dataset_type",
        "schema_version",
        "adapter_version",
        "request_fingerprint",
        "request_payload",
        "retrieval_mode",
        "requested_at",
        "fetched_at",
        "available_at_min",
        "available_at_max",
        "raw_content_sha256",
        "normalized_content_sha256",
        "record_count",
        "completeness_status",
        "freshness_status",
        "admission_status",
        "point_in_time_status",
        "synthetic",
        "issues",
        "records",
    }
)


class MarketDataContractError(ValueError):
    """Raised when a cross-module market-data contract is malformed."""


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise MarketDataContractError("datetime values must include a timezone offset")
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return decimal_text(value, "decimal")
    raise TypeError(f"cannot serialize {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize deterministic evidence bytes without lossy float conversion."""

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=_json_default,
        )
        + "\n"
    ).encode("utf-8")


def sha256_bytes(raw: bytes) -> str:
    return sha256(raw).hexdigest()


def aware_datetime(value: datetime | str, field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise MarketDataContractError(f"{field_name} must be an ISO datetime") from exc
    else:
        raise MarketDataContractError(f"{field_name} must be a datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MarketDataContractError(f"{field_name} must include a timezone offset")
    return parsed


def iso_date(value: date | str | None, field_name: str, *, required: bool = False) -> date | None:
    if value is None or value == "":
        if required:
            raise MarketDataContractError(f"{field_name} is required")
        return None
    if isinstance(value, datetime):
        raise MarketDataContractError(f"{field_name} must be a date, not datetime")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise MarketDataContractError(f"{field_name} must be an ISO date") from exc
    raise MarketDataContractError(f"{field_name} must be a date")


def decimal_text(value: Any, field_name: str) -> str:
    if isinstance(value, bool) or isinstance(value, float) and not math.isfinite(value):
        raise MarketDataContractError(f"{field_name} must be a finite decimal")
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError) as exc:
        raise MarketDataContractError(f"{field_name} must be a finite decimal") from exc
    if not parsed.is_finite():
        raise MarketDataContractError(f"{field_name} must be a finite decimal")
    text = format(parsed, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))


def _ensure_sha256(value: str, field_name: str) -> str:
    text = str(value).strip().lower()
    if SHA256_PATTERN.fullmatch(text) is None:
        raise MarketDataContractError(f"{field_name} must be a lowercase SHA-256")
    return text


@dataclass(frozen=True)
class MarketDataRequest:
    dataset_type: str
    requested_at: datetime
    retrieval_mode: str
    instrument_id: str = ""
    start_date: date | None = None
    end_date: date | None = None
    adjustment: str = "none"
    parameters: Mapping[str, Any] = field(default_factory=dict)
    evidence_cutoff_at: datetime | None = None

    def __post_init__(self) -> None:
        dataset = str(self.dataset_type).strip()
        if dataset not in DATASET_SCHEMA_VERSIONS:
            raise MarketDataContractError(f"unsupported dataset_type: {dataset!r}")
        object.__setattr__(self, "dataset_type", dataset)
        object.__setattr__(self, "requested_at", aware_datetime(self.requested_at, "requested_at"))
        mode = str(self.retrieval_mode).strip()
        if mode not in RETRIEVAL_MODES:
            raise MarketDataContractError(f"unsupported retrieval_mode: {mode!r}")
        object.__setattr__(self, "retrieval_mode", mode)
        cutoff = (
            aware_datetime(self.evidence_cutoff_at, "evidence_cutoff_at")
            if self.evidence_cutoff_at is not None
            else None
        )
        if cutoff is not None and mode != "offline_replay":
            raise MarketDataContractError(
                "evidence_cutoff_at is only valid for offline_replay"
            )
        if cutoff is not None and cutoff > self.requested_at:
            raise MarketDataContractError(
                "evidence_cutoff_at cannot be after requested_at"
            )
        object.__setattr__(self, "evidence_cutoff_at", cutoff)
        instrument = str(self.instrument_id or "").strip().upper()
        object.__setattr__(self, "instrument_id", instrument)
        start = iso_date(self.start_date, "start_date")
        end = iso_date(self.end_date, "end_date")
        if start is not None and end is not None and start > end:
            raise MarketDataContractError("start_date cannot be after end_date")
        if dataset in {"daily_bar", "trade_calendar"} and (start is None or end is None):
            raise MarketDataContractError(f"{dataset} requires start_date and end_date")
        if dataset in {"daily_bar", "security_master"} and not instrument:
            raise MarketDataContractError(f"{dataset} requires instrument_id")
        adjustment = str(self.adjustment or "none").strip().lower()
        if adjustment not in {"none", "qfq", "hfq"}:
            raise MarketDataContractError("adjustment must be none, qfq, or hfq")
        if dataset != "daily_bar" and adjustment != "none":
            raise MarketDataContractError("adjustment is only valid for daily_bar")
        object.__setattr__(self, "start_date", start)
        object.__setattr__(self, "end_date", end)
        object.__setattr__(self, "adjustment", adjustment)
        if not isinstance(self.parameters, Mapping):
            raise MarketDataContractError("parameters must be an object")
        if self.parameters:
            raise MarketDataContractError(
                "market-data-v1 request parameters must be empty; credentials and "
                "provider-specific options are not part of the V1 contract"
            )
        object.__setattr__(self, "parameters", MappingProxyType({}))

    def fingerprint_payload(self, provider_id: str, adapter_version: str) -> dict[str, Any]:
        """Bind the query, not the capture mode/time, so it can be replayed offline."""

        return {
            "provider_id": str(provider_id),
            "adapter_version": str(adapter_version),
            "dataset_type": self.dataset_type,
            "schema_version": DATASET_SCHEMA_VERSIONS[self.dataset_type],
            "instrument_id": self.instrument_id or None,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "adjustment": self.adjustment,
            "parameters": {},
        }

    def fingerprint(self, provider_id: str, adapter_version: str) -> str:
        return sha256_bytes(canonical_json_bytes(self.fingerprint_payload(provider_id, adapter_version)))


@dataclass(frozen=True)
class MarketDataBatch:
    batch_id: str
    provider_id: str
    upstream_source: str
    dataset_type: str
    schema_version: str
    adapter_version: str
    request_fingerprint: str
    request_payload: Mapping[str, Any]
    retrieval_mode: str
    requested_at: datetime
    fetched_at: datetime
    available_at_min: datetime | None
    available_at_max: datetime | None
    raw_content_sha256: str
    normalized_content_sha256: str
    record_count: int
    completeness_status: str
    freshness_status: str
    admission_status: str
    point_in_time_status: str
    synthetic: bool
    issues: tuple[Mapping[str, Any], ...]
    records: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        for field_name in ("batch_id", "provider_id", "upstream_source", "dataset_type", "schema_version", "adapter_version"):
            if not str(getattr(self, field_name)).strip():
                raise MarketDataContractError(f"{field_name} must not be empty")
        for field_name in ("batch_id", "provider_id", "adapter_version"):
            if IDENTIFIER_PATTERN.fullmatch(str(getattr(self, field_name))) is None:
                raise MarketDataContractError(f"{field_name} is not a valid identifier")
        if self.dataset_type not in DATASET_SCHEMA_VERSIONS:
            raise MarketDataContractError("unsupported batch dataset_type")
        if self.schema_version != DATASET_SCHEMA_VERSIONS[self.dataset_type]:
            raise MarketDataContractError("schema_version does not match dataset_type")
        if self.retrieval_mode not in RETRIEVAL_MODES:
            raise MarketDataContractError("unsupported batch retrieval_mode")
        object.__setattr__(self, "requested_at", aware_datetime(self.requested_at, "requested_at"))
        object.__setattr__(self, "fetched_at", aware_datetime(self.fetched_at, "fetched_at"))
        if self.retrieval_mode != "offline_replay" and self.fetched_at < self.requested_at:
            raise MarketDataContractError("fetched_at cannot precede requested_at")
        minimum = aware_datetime(self.available_at_min, "available_at_min") if self.available_at_min else None
        maximum = aware_datetime(self.available_at_max, "available_at_max") if self.available_at_max else None
        if (minimum is None) != (maximum is None):
            raise MarketDataContractError("available_at_min and available_at_max must both be set or null")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise MarketDataContractError("available_at_min cannot follow available_at_max")
        if maximum is not None and maximum > self.fetched_at:
            raise MarketDataContractError("available_at_max cannot follow fetched_at")
        object.__setattr__(self, "available_at_min", minimum)
        object.__setattr__(self, "available_at_max", maximum)
        for field_name in ("request_fingerprint", "raw_content_sha256", "normalized_content_sha256"):
            object.__setattr__(self, field_name, _ensure_sha256(getattr(self, field_name), field_name))
        if not isinstance(self.request_payload, Mapping):
            raise MarketDataContractError("request_payload must be an object")
        request_payload = dict(self.request_payload)
        expected_request_fields = {
            "provider_id",
            "adapter_version",
            "dataset_type",
            "schema_version",
            "instrument_id",
            "start_date",
            "end_date",
            "adjustment",
            "parameters",
        }
        if set(request_payload) != expected_request_fields:
            raise MarketDataContractError("request_payload fields differ from the V1 contract")
        request = MarketDataRequest(
            dataset_type=str(request_payload.get("dataset_type") or ""),
            requested_at=self.requested_at,
            retrieval_mode=self.retrieval_mode,
            instrument_id=str(request_payload.get("instrument_id") or ""),
            start_date=request_payload.get("start_date"),  # type: ignore[arg-type]
            end_date=request_payload.get("end_date"),  # type: ignore[arg-type]
            adjustment=str(request_payload.get("adjustment") or "none"),
            parameters=request_payload.get("parameters", {}),  # type: ignore[arg-type]
        )
        expected_payload = request.fingerprint_payload(
            self.provider_id, self.adapter_version
        )
        if request_payload != expected_payload:
            raise MarketDataContractError(
                "request_payload does not match batch provider/dataset contract"
            )
        if request.fingerprint(self.provider_id, self.adapter_version) != self.request_fingerprint:
            raise MarketDataContractError("request_fingerprint does not match request_payload")
        object.__setattr__(self, "request_payload", _freeze_mapping(request_payload))
        if type(self.record_count) is not int or self.record_count < 0:
            raise MarketDataContractError("record_count must be a non-negative integer")
        if self.record_count != len(self.records):
            raise MarketDataContractError("record_count does not match records")
        if type(self.synthetic) is not bool:
            raise MarketDataContractError("synthetic must be a boolean")
        if self.completeness_status not in COMPLETENESS_STATUSES:
            raise MarketDataContractError("unsupported completeness_status")
        if self.freshness_status not in FRESHNESS_STATUSES:
            raise MarketDataContractError("unsupported freshness_status")
        if self.admission_status not in ADMISSION_STATUSES:
            raise MarketDataContractError("unsupported admission_status")
        if self.point_in_time_status not in POINT_IN_TIME_STATUSES:
            raise MarketDataContractError("unsupported point_in_time_status")
        if self.record_count == 0 and self.completeness_status == "complete":
            raise MarketDataContractError("an empty batch cannot be complete")
        if self.admission_status in {"validated_research_only", "admitted_for_research"}:
            if self.completeness_status != "complete" or self.synthetic:
                raise MarketDataContractError("research-readable batches must be complete and non-synthetic")
            if minimum is None or maximum is None:
                raise MarketDataContractError("research-readable batches require availability bounds")
        for issue in self.issues:
            if not isinstance(issue, Mapping):
                raise MarketDataContractError("batch issues must contain objects")
            if not str(issue.get("code") or "").strip() or issue.get("severity") not in {
                "info",
                "warning",
                "error",
            }:
                raise MarketDataContractError("batch issue requires code and supported severity")
        if any(not isinstance(record, Mapping) for record in self.records):
            raise MarketDataContractError("batch records must contain objects")
        object.__setattr__(self, "issues", tuple(_freeze_mapping(item) for item in self.issues))
        object.__setattr__(self, "records", tuple(_freeze_mapping(item) for item in self.records))

    def to_dict(self, *, include_records: bool = True) -> dict[str, Any]:
        result = {
            "batch_id": self.batch_id,
            "provider_id": self.provider_id,
            "upstream_source": self.upstream_source,
            "dataset_type": self.dataset_type,
            "schema_version": self.schema_version,
            "adapter_version": self.adapter_version,
            "request_fingerprint": self.request_fingerprint,
            "request_payload": dict(self.request_payload),
            "retrieval_mode": self.retrieval_mode,
            "requested_at": self.requested_at.isoformat(),
            "fetched_at": self.fetched_at.isoformat(),
            "available_at_min": self.available_at_min.isoformat() if self.available_at_min else None,
            "available_at_max": self.available_at_max.isoformat() if self.available_at_max else None,
            "raw_content_sha256": self.raw_content_sha256,
            "normalized_content_sha256": self.normalized_content_sha256,
            "record_count": self.record_count,
            "completeness_status": self.completeness_status,
            "freshness_status": self.freshness_status,
            "admission_status": self.admission_status,
            "point_in_time_status": self.point_in_time_status,
            "synthetic": self.synthetic,
            "issues": [dict(item) for item in self.issues],
        }
        if include_records:
            result["records"] = [dict(item) for item in self.records]
        return result

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "MarketDataBatch":
        if set(payload) != BATCH_FIELDS:
            missing = sorted(BATCH_FIELDS - set(payload))
            unknown = sorted(set(payload) - BATCH_FIELDS)
            raise MarketDataContractError(
                f"batch envelope fields differ from Schema; missing={missing}, unknown={unknown}"
            )
        records = payload.get("records")
        issues = payload.get("issues")
        if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
            raise MarketDataContractError("batch records must be an array")
        if not isinstance(issues, Sequence) or isinstance(issues, (str, bytes)):
            raise MarketDataContractError("batch issues must be an array")
        if any(not isinstance(item, Mapping) for item in records):
            raise MarketDataContractError("batch records must contain objects")
        if any(not isinstance(item, Mapping) for item in issues):
            raise MarketDataContractError("batch issues must contain objects")
        return cls(
            batch_id=str(payload.get("batch_id") or ""),
            provider_id=str(payload.get("provider_id") or ""),
            upstream_source=str(payload.get("upstream_source") or ""),
            dataset_type=str(payload.get("dataset_type") or ""),
            schema_version=str(payload.get("schema_version") or ""),
            adapter_version=str(payload.get("adapter_version") or ""),
            request_fingerprint=str(payload.get("request_fingerprint") or ""),
            request_payload=payload.get("request_payload"),  # type: ignore[arg-type]
            retrieval_mode=str(payload.get("retrieval_mode") or ""),
            requested_at=aware_datetime(payload.get("requested_at"), "requested_at"),  # type: ignore[arg-type]
            fetched_at=aware_datetime(payload.get("fetched_at"), "fetched_at"),  # type: ignore[arg-type]
            available_at_min=(
                aware_datetime(payload.get("available_at_min"), "available_at_min")
                if payload.get("available_at_min")
                else None
            ),
            available_at_max=(
                aware_datetime(payload.get("available_at_max"), "available_at_max")
                if payload.get("available_at_max")
                else None
            ),
            raw_content_sha256=str(payload.get("raw_content_sha256") or ""),
            normalized_content_sha256=str(payload.get("normalized_content_sha256") or ""),
            record_count=payload.get("record_count"),  # type: ignore[arg-type]
            completeness_status=str(payload.get("completeness_status") or ""),
            freshness_status=str(payload.get("freshness_status") or ""),
            admission_status=str(payload.get("admission_status") or ""),
            point_in_time_status=str(payload.get("point_in_time_status") or ""),
            synthetic=payload.get("synthetic"),  # type: ignore[arg-type]
            issues=tuple(issues),  # type: ignore[arg-type]
            records=tuple(records),  # type: ignore[arg-type]
        )
