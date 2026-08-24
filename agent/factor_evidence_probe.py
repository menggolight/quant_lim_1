"""Capture or verify fixed factor-input evidence without trading/account access.

The probe deliberately exposes only three fixed source policies.  Online mode
lazy-loads one read-only provider and stores the returned raw bytes plus its
normalized records.  Offline mode never imports a provider: it verifies a
previous online receipt and writes a separate replay receipt/checkpoint.

This boundary is evidence collection only.  A successful receipt remains
``not_admitted_probe_only`` until the downstream schema, point-in-time and
research-admission checks have passed.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from research.market_data.contracts import (
    aware_datetime,
    canonical_json_bytes,
    sha256_bytes,
)
from research.market_data.providers.base import classify_unexpected_error, safe_error_text


PROBE_VERSION = "factor-evidence-probe-v1"
BUNDLE_VERSION = "factor-evidence-bundle-v1"
RECEIPT_VERSION = "factor-evidence-receipt-v1"
CHECKPOINT_VERSION = "factor-evidence-checkpoint-v1"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_SENSITIVE_KEY = re.compile(
    r"(?:token|secret|password|passwd|cookie|authorization|credential|"
    r"api[_-]?key|access[_-]?key|account[_-]?(?:id|no|number)|"
    r"shareholder[_-]?account|user[_-]?info|verification[_-]?code|otp)",
    re.IGNORECASE,
)
_SENSITIVE_ASSIGNMENT = re.compile(
    rb"(?i)(?:token|secret|password|passwd|cookie|authorization|credential|"
    rb"api[_-]?key|access[_-]?key|account[_-]?(?:id|no|number)|"
    rb"shareholder[_-]?account|user[_-]?info|verification[_-]?code|otp)"
    rb"[\"']?\s*[:=]\s*[\"']?(?!null\b|none\b|missing\b|unavailable\b|"
    rb"not[_ -]?configured\b|\[redacted\])[^\s,;}\]\"']{4,}",
)
_ONLINE_RECEIPT_FIELDS = frozenset(
    {
        "receipt_version",
        "probe_version",
        "evidence_mode",
        "mode",
        "source",
        "source_role",
        "provider_id",
        "provider_adapter_identity",
        "adapter_version",
        "upstream_source",
        "dataset_type",
        "index_ids",
        "screen_index_ids",
        "reconciliation_index_ids",
        "request_fingerprint",
        "request",
        "requested_at",
        "fetched_at",
        "availability_status",
        "point_in_time_status",
        "research_admission_status",
        "formal_truth_eligible",
        "record_count",
        "raw_content_sha256",
        "raw_path",
        "normalized_content_sha256",
        "normalized_path",
        "transport_receipt_status",
        "transport_receipt_sha256",
        "transport_receipt_path",
    }
)

# Choice captures the 11-code legacy screening set, the 11-code current CSI
# reconciliation set and their shared 000985 benchmark in one frozen call (23
# unique identifiers).  CSI confirmation captures only the current 11 plus the
# benchmark (12).  There is intentionally no CLI option for arbitrary IDs.
CHOICE_SCREEN_INDEX_IDS = (
    "000986.CSI",
    "000987.CSI",
    "000988.CSI",
    "000989.CSI",
    "000990.CSI",
    "000991.CSI",
    "932075.CSI",
    "932076.CSI",
    "000993.CSI",
    "000994.CSI",
    "000995.CSI",
    "000985.CSI",
)
CSI_CONFIRM_INDEX_IDS = (
    "932077.CSI",
    "932078.CSI",
    "932079.CSI",
    "932080.CSI",
    "932081.CSI",
    "932082.CSI",
    "932083.CSI",
    "931775.CSI",
    "932084.CSI",
    "932085.CSI",
    "932086.CSI",
    "000985.CSI",
)
CHOICE_RECONCILIATION_INDEX_IDS = CSI_CONFIRM_INDEX_IDS
CHOICE_CAPTURE_INDEX_IDS = tuple(
    dict.fromkeys(
        CHOICE_SCREEN_INDEX_IDS[:-1]
        + CHOICE_RECONCILIATION_INDEX_IDS[:-1]
        + ("000985.CSI",)
    )
)


class FactorEvidenceProbeError(RuntimeError):
    """Fail-closed error at the probe/evidence boundary."""


@dataclass(frozen=True)
class SourcePolicy:
    source: str
    dataset_type: str
    index_ids: tuple[str, ...]
    provider_module: str
    provider_class: str
    provider_ids: frozenset[str]
    role: str


SOURCE_POLICIES: Mapping[str, SourcePolicy] = {
    "choice": SourcePolicy(
        source="choice",
        dataset_type="index_level",
        index_ids=CHOICE_CAPTURE_INDEX_IDS,
        provider_module="research.market_data.providers.choice_index",
        provider_class="ChoiceIndexProvider",
        provider_ids=frozenset({"choice", "choice_index"}),
        role="screening_candidate",
    ),
    "csi": SourcePolicy(
        source="csi",
        dataset_type="index_level",
        index_ids=CSI_CONFIRM_INDEX_IDS,
        provider_module="research.market_data.providers.csi_official",
        provider_class="CSIOfficialProvider",
        provider_ids=frozenset({"csi", "csi_official"}),
        role="official_confirmation_candidate",
    ),
    "sse": SourcePolicy(
        source="sse",
        dataset_type="cn_equity_session",
        index_ids=(),
        provider_module="research.market_data.providers.sse_calendar",
        provider_class="SSECalendarProvider",
        provider_ids=frozenset({"sse", "sse_calendar"}),
        role="official_calendar_candidate",
    ),
}


@dataclass(frozen=True)
class ProbeQuery:
    source: str
    mode: str
    start_date: date
    end_date: date
    requested_at: datetime
    evidence_cutoff_at: datetime | None = None

    def __post_init__(self) -> None:
        source = str(self.source).strip().lower()
        mode = str(self.mode).strip().lower()
        if source not in SOURCE_POLICIES:
            raise FactorEvidenceProbeError(f"unsupported source: {source!r}")
        if mode not in {"online", "offline"}:
            raise FactorEvidenceProbeError(f"unsupported mode: {mode!r}")
        if not isinstance(self.start_date, date) or not isinstance(self.end_date, date):
            raise FactorEvidenceProbeError("start_date and end_date must be dates")
        if self.start_date > self.end_date:
            raise FactorEvidenceProbeError("start_date cannot be after end_date")
        requested = aware_datetime(self.requested_at, "requested_at")
        cutoff = (
            aware_datetime(self.evidence_cutoff_at, "evidence_cutoff_at")
            if self.evidence_cutoff_at is not None
            else None
        )
        if mode == "online" and cutoff is not None:
            raise FactorEvidenceProbeError(
                "evidence_cutoff_at is only valid in offline mode"
            )
        if mode == "offline" and cutoff is None:
            raise FactorEvidenceProbeError(
                "offline mode requires an explicit evidence_cutoff_at"
            )
        if cutoff is not None and cutoff > requested:
            raise FactorEvidenceProbeError(
                "evidence_cutoff_at cannot be after requested_at"
            )
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "requested_at", requested)
        object.__setattr__(self, "evidence_cutoff_at", cutoff)

    @property
    def policy(self) -> SourcePolicy:
        return SOURCE_POLICIES[self.source]

    def fingerprint_payload(self) -> dict[str, Any]:
        """Return the fixed query identity, excluding capture/replay time."""

        return {
            "bundle_version": BUNDLE_VERSION,
            "source": self.source,
            "dataset_type": self.policy.dataset_type,
            "index_ids": list(self.policy.index_ids),
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
        }

    @property
    def request_fingerprint(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.fingerprint_payload()))


@dataclass(frozen=True)
class AdaptedPayload:
    raw_content: bytes
    records: tuple[Mapping[str, Any], ...]
    fetched_at: datetime
    upstream_source: str
    availability_status: str
    point_in_time_status: str
    transport_receipt: Mapping[str, Any] | None


ProviderLoader = Callable[[str], object]
RequestFactory = Callable[[ProbeQuery], object]


def _strict_json(raw: bytes, label: str) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise FactorEvidenceProbeError(
                    f"{label} contains duplicate JSON key: {key}"
                )
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise FactorEvidenceProbeError(
            f"{label} contains non-finite JSON number: {value}"
        )

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FactorEvidenceProbeError(f"{label} is not strict UTF-8 JSON") from exc


def _assert_no_sensitive_keys(value: Any, label: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _SENSITIVE_KEY.search(str(key)):
                raise FactorEvidenceProbeError(
                    f"{label} contains a credential-shaped field name"
                )
            _assert_no_sensitive_keys(item, label)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_no_sensitive_keys(item, label)


def _safe_segment(value: str, label: str) -> str:
    text = str(value).strip()
    if _SAFE_ID.fullmatch(text) is None:
        raise FactorEvidenceProbeError(f"unsafe {label}")
    return text


def _ensure_under_repository(
    path: Path | str,
    *,
    repository_root: Path | str = REPOSITORY_ROOT,
) -> Path:
    root = Path(repository_root).resolve()
    resolved = Path(path).resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise FactorEvidenceProbeError(
            "probe output_root must remain inside the repository"
        ) from exc
    if not relative.parts:
        raise FactorEvidenceProbeError("probe output_root cannot be the repository root")
    return resolved


def _atomic_write(path: Path, content: bytes) -> None:
    """Publish append-only evidence without replacing a concurrent writer."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not path.is_file() or path.read_bytes() != content:
            raise FactorEvidenceProbeError(
                "refusing non-identical content-addressed evidence collision"
            )
        return
    descriptor, temporary_name = tempfile.mkstemp(prefix=".factor-evidence-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if not path.is_file() or path.read_bytes() != content:
                raise FactorEvidenceProbeError(
                    "refusing non-identical content-addressed evidence collision"
                )
    finally:
        temporary.unlink(missing_ok=True)


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _content_path(root: Path, layer: str, source: str, digest: str, suffix: str) -> Path:
    if _SHA256.fullmatch(digest) is None:
        raise FactorEvidenceProbeError("invalid content digest")
    return root / layer / source / digest[:2] / f"{digest}{suffix}"


def _receipt_path(root: Path, query: ProbeQuery, digest: str) -> Path:
    return (
        root
        / "receipts"
        / query.source
        / query.policy.dataset_type
        / query.request_fingerprint
        / digest[:2]
        / f"{digest}.json"
    )


def _checkpoint_path(root: Path, query: ProbeQuery, digest: str) -> Path:
    return (
        root
        / "checkpoints"
        / query.source
        / query.policy.dataset_type
        / query.request_fingerprint
        / digest[:2]
        / f"{digest}.json"
    )


def _default_provider_loader(source: str) -> object:
    policy = SOURCE_POLICIES[source]
    module = importlib.import_module(policy.provider_module)
    provider_type = getattr(module, policy.provider_class, None)
    if not isinstance(provider_type, type):
        raise FactorEvidenceProbeError(
            f"fixed provider class is unavailable: {policy.provider_class}"
        )
    return provider_type()


def _default_request_factory(query: ProbeQuery) -> object:
    """Lazy-load the fixed index-evidence request contract for online mode."""

    module = importlib.import_module("research.market_data.index_evidence")
    request_type = getattr(module, "IndexEvidenceRequest", None)
    if not isinstance(request_type, type):
        raise FactorEvidenceProbeError("IndexEvidenceRequest is unavailable")
    return request_type(
        dataset_type=query.policy.dataset_type,
        index_ids=query.policy.index_ids,
        start_date=query.start_date,
        end_date=query.end_date,
        requested_at=query.requested_at,
        retrieval_mode="historical_backfill",
        evidence_cutoff_at=None,
    )


def _payload_field(payload: object, name: str, default: Any = None) -> Any:
    if isinstance(payload, Mapping):
        return payload.get(name, default)
    return getattr(payload, name, default)


def _nonempty_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise FactorEvidenceProbeError(f"provider record {label} must not be empty")
    return text


def _record_date(value: Any, label: str) -> date:
    if not isinstance(value, str):
        raise FactorEvidenceProbeError(f"provider record {label} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise FactorEvidenceProbeError(
            f"provider record {label} must be an ISO date"
        ) from exc


def _positive_decimal_text(value: Any, label: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise FactorEvidenceProbeError(
            f"provider record {label} must be a decimal string"
        )
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise FactorEvidenceProbeError(
            f"provider record {label} must be a decimal string"
        ) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise FactorEvidenceProbeError(
            f"provider record {label} must be finite and positive"
        )


def _available_at(value: Any, label: str) -> datetime:
    parsed = aware_datetime(value, label)
    if parsed.utcoffset() != timedelta(hours=8):
        raise FactorEvidenceProbeError(
            f"provider record {label} must use the Asia/Shanghai offset"
        )
    return parsed


def _validate_index_records(
    records: tuple[Mapping[str, Any], ...], query: ProbeQuery
) -> None:
    fields = {
        "schema_version",
        "index_id",
        "trading_date",
        "open",
        "high",
        "low",
        "close",
        "currency",
        "basis",
        "available_at",
        "availability_status",
        "source_record_id",
    }
    keys: list[tuple[str, str]] = []
    observed_ids: set[str] = set()
    source_record_ids: set[str] = set()
    for record in records:
        if set(record) != fields:
            raise FactorEvidenceProbeError(
                "index_level record fields differ from the fixed normalized contract"
            )
        if record.get("schema_version") != "index-level-v1":
            raise FactorEvidenceProbeError("index_level schema_version mismatch")
        index_id = _nonempty_text(record.get("index_id"), "index_id")
        if index_id not in query.policy.index_ids:
            raise FactorEvidenceProbeError(
                "provider returned a missing or non-whitelisted index_id"
            )
        trading_date = _record_date(record.get("trading_date"), "trading_date")
        if trading_date < query.start_date or trading_date > query.end_date:
            raise FactorEvidenceProbeError(
                "provider returned an index record outside the fixed date range"
            )
        _positive_decimal_text(record.get("open"), "open", nullable=True)
        _positive_decimal_text(record.get("high"), "high", nullable=True)
        _positive_decimal_text(record.get("low"), "low", nullable=True)
        _positive_decimal_text(record.get("close"), "close")
        if record.get("currency") != "CNY":
            raise FactorEvidenceProbeError("index_level currency must be CNY")
        if record.get("basis") != "index_points_unadjusted":
            raise FactorEvidenceProbeError(
                "index_level basis must be index_points_unadjusted"
            )
        _available_at(record.get("available_at"), "available_at")
        _nonempty_text(record.get("availability_status"), "availability_status")
        source_record_id = _nonempty_text(
            record.get("source_record_id"), "source_record_id"
        )
        if source_record_id in source_record_ids:
            raise FactorEvidenceProbeError("duplicate source_record_id in index records")
        source_record_ids.add(source_record_id)
        observed_ids.add(index_id)
        keys.append((index_id, trading_date.isoformat()))
    if len(keys) != len(set(keys)):
        raise FactorEvidenceProbeError("duplicate index_id/trading_date record")
    if keys != sorted(keys):
        raise FactorEvidenceProbeError(
            "index_level records must be sorted by index_id and trading_date"
        )
    if observed_ids != set(query.policy.index_ids):
        raise FactorEvidenceProbeError(
            "provider response does not cover the complete fixed index whitelist"
        )


def _validate_calendar_records(
    records: tuple[Mapping[str, Any], ...], query: ProbeQuery
) -> None:
    fields = {
        "schema_version",
        "calendar_date",
        "is_trading_day",
        "session_open_at",
        "session_close_at",
        "available_at",
        "availability_status",
        "source_record_id",
    }
    observed_dates: list[date] = []
    source_record_ids: set[str] = set()
    for record in records:
        if set(record) != fields:
            raise FactorEvidenceProbeError(
                "calendar record fields differ from the fixed normalized contract"
            )
        if record.get("schema_version") != "cn-equity-session-v1":
            raise FactorEvidenceProbeError("calendar schema_version mismatch")
        calendar_date = _record_date(record.get("calendar_date"), "calendar_date")
        if calendar_date < query.start_date or calendar_date > query.end_date:
            raise FactorEvidenceProbeError(
                "provider returned a calendar record outside the fixed date range"
            )
        trading_day = record.get("is_trading_day")
        if type(trading_day) is not bool:
            raise FactorEvidenceProbeError("calendar is_trading_day must be boolean")
        expected_open = (
            f"{calendar_date.isoformat()}T09:30:00+08:00" if trading_day else None
        )
        expected_close = (
            f"{calendar_date.isoformat()}T15:00:00+08:00" if trading_day else None
        )
        if record.get("session_open_at") != expected_open:
            raise FactorEvidenceProbeError("calendar session_open_at mismatch")
        if record.get("session_close_at") != expected_close:
            raise FactorEvidenceProbeError("calendar session_close_at mismatch")
        _available_at(record.get("available_at"), "available_at")
        _nonempty_text(record.get("availability_status"), "availability_status")
        source_record_id = _nonempty_text(
            record.get("source_record_id"), "source_record_id"
        )
        if source_record_id in source_record_ids:
            raise FactorEvidenceProbeError(
                "duplicate source_record_id in calendar records"
            )
        source_record_ids.add(source_record_id)
        observed_dates.append(calendar_date)
    if observed_dates != sorted(observed_dates) or len(observed_dates) != len(
        set(observed_dates)
    ):
        raise FactorEvidenceProbeError(
            "calendar records must contain unique sorted calendar_date values"
        )
    expected_dates: list[date] = []
    current = query.start_date
    while current <= query.end_date:
        expected_dates.append(current)
        current += timedelta(days=1)
    if observed_dates != expected_dates:
        raise FactorEvidenceProbeError(
            "calendar records must cover every natural date in the fixed range"
        )


def _adapt_payload(payload: object, query: ProbeQuery) -> AdaptedPayload:
    raw_content = _payload_field(payload, "raw_content")
    records = _payload_field(payload, "records")
    fetched_at = _payload_field(payload, "fetched_at")
    upstream_source = str(_payload_field(payload, "upstream_source") or "").strip()
    availability_status = str(
        _payload_field(payload, "availability_status", "not_assessed")
    ).strip()
    point_in_time_status = str(
        _payload_field(payload, "point_in_time_status", "not_assessed")
    ).strip()
    transport_receipt = _payload_field(payload, "transport_receipt")
    if transport_receipt is None:
        transport_receipt = _payload_field(payload, "receipt")
    if transport_receipt is None:
        transport_receipts = _payload_field(payload, "transport_receipts")
        if transport_receipts:
            if not isinstance(transport_receipts, (tuple, list)) or any(
                not isinstance(item, Mapping) for item in transport_receipts
            ):
                raise FactorEvidenceProbeError(
                    "provider transport_receipts must be a non-empty object array"
                )
            transport_receipt = {
                "schema_version": "factor-evidence-transport-receipts-v1",
                "receipts": [dict(item) for item in transport_receipts],
            }
    if not isinstance(raw_content, bytes) or not raw_content:
        raise FactorEvidenceProbeError(
            "provider payload must include non-empty source-owned raw_content bytes"
        )
    if not isinstance(records, (tuple, list)) or not records:
        raise FactorEvidenceProbeError("provider payload must include non-empty records")
    if any(not isinstance(item, Mapping) for item in records):
        raise FactorEvidenceProbeError("provider records must be objects")
    normalized_records = tuple(dict(item) for item in records)
    _assert_no_sensitive_keys(normalized_records, "provider records")
    # Raw bytes are preserved byte-for-byte.  When the response is JSON, reject
    # credential-shaped keys before writing it; non-JSON official CSV/HTML is
    # still allowed and remains attributable through its raw hash.
    if _SENSITIVE_ASSIGNMENT.search(raw_content):
        raise FactorEvidenceProbeError(
            "provider raw_content contains credential-shaped assignment text"
        )
    stripped_raw = raw_content.lstrip()
    if stripped_raw.startswith((b"{", b"[")):
        parsed_raw = _strict_json(raw_content, "provider raw_content")
        _assert_no_sensitive_keys(parsed_raw, "provider raw_content")
    if not upstream_source:
        raise FactorEvidenceProbeError("provider upstream_source must not be empty")
    if not availability_status or not point_in_time_status:
        raise FactorEvidenceProbeError("provider evidence statuses must not be empty")
    normalized_transport_receipt: Mapping[str, Any] | None = None
    if transport_receipt is not None:
        if not isinstance(transport_receipt, Mapping) or not transport_receipt:
            raise FactorEvidenceProbeError(
                "provider transport_receipt must be a non-empty object"
            )
        normalized_transport_receipt = dict(transport_receipt)
        _assert_no_sensitive_keys(
            normalized_transport_receipt, "provider transport_receipt"
        )
        try:
            rendered_transport = canonical_json_bytes(normalized_transport_receipt)
        except (TypeError, ValueError) as exc:
            raise FactorEvidenceProbeError(
                "provider transport_receipt must contain only canonical JSON values"
            ) from exc
        if _SENSITIVE_ASSIGNMENT.search(rendered_transport):
            raise FactorEvidenceProbeError(
                "provider transport_receipt contains credential-shaped assignment text"
            )
    fetched = aware_datetime(fetched_at, "provider.fetched_at")
    if query.policy.dataset_type == "index_level":
        _validate_index_records(normalized_records, query)
    else:
        _validate_calendar_records(normalized_records, query)
    return AdaptedPayload(
        raw_content=raw_content,
        records=normalized_records,
        fetched_at=fetched,
        upstream_source=upstream_source,
        availability_status=availability_status,
        point_in_time_status=point_in_time_status,
        transport_receipt=normalized_transport_receipt,
    )


def _provider_metadata(provider: object, policy: SourcePolicy) -> tuple[str, str]:
    provider_id = str(getattr(provider, "provider_id", "") or "").strip()
    adapter_version = str(getattr(provider, "adapter_version", "") or "").strip()
    if provider_id not in policy.provider_ids:
        raise FactorEvidenceProbeError("provider_id differs from the fixed source policy")
    return provider_id, _safe_segment(adapter_version, "adapter_version")


def _persist_receipt_and_checkpoint(
    *,
    root: Path,
    query: ProbeQuery,
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    receipt_bytes = canonical_json_bytes(dict(receipt))
    receipt_sha256 = sha256_bytes(receipt_bytes)
    receipt_path = _receipt_path(root, query, receipt_sha256)
    _atomic_write(receipt_path, receipt_bytes)
    checkpoint = {
        "checkpoint_version": CHECKPOINT_VERSION,
        "source": query.source,
        "dataset_type": query.policy.dataset_type,
        "mode": query.mode,
        "request_fingerprint": query.request_fingerprint,
        "receipt_sha256": receipt_sha256,
        "receipt_path": _relative(receipt_path, root),
        "status": "completed",
    }
    checkpoint_bytes = canonical_json_bytes(checkpoint)
    checkpoint_sha256 = sha256_bytes(checkpoint_bytes)
    checkpoint_path = _checkpoint_path(root, query, checkpoint_sha256)
    _atomic_write(checkpoint_path, checkpoint_bytes)
    return {
        "receipt_sha256": receipt_sha256,
        "receipt_path": _relative(receipt_path, root),
        "checkpoint_sha256": checkpoint_sha256,
        "checkpoint_path": _relative(checkpoint_path, root),
    }


def _persist_failed_checkpoint(
    *,
    root: Path,
    query: ProbeQuery,
    error: Exception,
) -> dict[str, Any]:
    """Persist a redacted, content-addressed failure without inventing a receipt."""

    status = str(getattr(error, "status", "failed") or "failed")
    code = str(getattr(error, "code", type(error).__name__) or type(error).__name__)
    checkpoint = {
        "checkpoint_version": CHECKPOINT_VERSION,
        "source": query.source,
        "dataset_type": query.policy.dataset_type,
        "mode": query.mode,
        "request_fingerprint": query.request_fingerprint,
        "requested_at": query.requested_at.isoformat(),
        "status": "failed",
        "failure_status": status,
        "failure_code": code,
        "error_type": type(error).__name__,
        "error": safe_error_text(error),
        "receipt_sha256": None,
        "receipt_path": None,
    }
    checkpoint_bytes = canonical_json_bytes(checkpoint)
    checkpoint_sha256 = sha256_bytes(checkpoint_bytes)
    checkpoint_path = _checkpoint_path(root, query, checkpoint_sha256)
    _atomic_write(checkpoint_path, checkpoint_bytes)
    return {
        "checkpoint_sha256": checkpoint_sha256,
        "checkpoint_path": _relative(checkpoint_path, root),
        "failure_status": status,
        "failure_code": code,
    }


def _online_probe(
    query: ProbeQuery,
    root: Path,
    *,
    provider_loader: ProviderLoader,
    request_factory: RequestFactory,
    evidence_mode: str,
) -> dict[str, Any]:
    provider = provider_loader(query.source)
    provider_id, adapter_version = _provider_metadata(provider, query.policy)
    fetch = getattr(provider, "fetch", None)
    if not callable(fetch):
        raise FactorEvidenceProbeError("fixed provider does not expose fetch(request)")
    request = request_factory(query)
    payload = _adapt_payload(fetch(request), query)
    normalized_bytes = canonical_json_bytes([dict(item) for item in payload.records])
    raw_sha256 = sha256_bytes(payload.raw_content)
    normalized_sha256 = sha256_bytes(normalized_bytes)
    raw_path = _content_path(root, "raw", query.source, raw_sha256, ".raw")
    normalized_path = _content_path(
        root, "normalized", query.source, normalized_sha256, ".json"
    )
    _atomic_write(raw_path, payload.raw_content)
    _atomic_write(normalized_path, normalized_bytes)
    transport_receipt_sha256: str | None = None
    transport_receipt_path: Path | None = None
    if payload.transport_receipt is not None:
        transport_bytes = canonical_json_bytes(dict(payload.transport_receipt))
        transport_receipt_sha256 = sha256_bytes(transport_bytes)
        transport_receipt_path = _content_path(
            root,
            "transport_receipts",
            query.source,
            transport_receipt_sha256,
            ".json",
        )
        _atomic_write(transport_receipt_path, transport_bytes)
    receipt = {
        "receipt_version": RECEIPT_VERSION,
        "probe_version": PROBE_VERSION,
        "evidence_mode": evidence_mode,
        "mode": "online",
        "source": query.source,
        "source_role": query.policy.role,
        "provider_id": provider_id,
        "provider_adapter_identity": (
            f"{query.policy.provider_module}.{query.policy.provider_class}"
        ),
        "adapter_version": adapter_version,
        "upstream_source": payload.upstream_source,
        "dataset_type": query.policy.dataset_type,
        "index_ids": list(query.policy.index_ids),
        "screen_index_ids": (
            list(CHOICE_SCREEN_INDEX_IDS) if query.source == "choice" else []
        ),
        "reconciliation_index_ids": (
            list(CHOICE_RECONCILIATION_INDEX_IDS)
            if query.source == "choice"
            else []
        ),
        "request_fingerprint": query.request_fingerprint,
        "request": query.fingerprint_payload(),
        "requested_at": query.requested_at.isoformat(),
        "fetched_at": payload.fetched_at.isoformat(),
        "availability_status": payload.availability_status,
        "point_in_time_status": payload.point_in_time_status,
        "research_admission_status": "not_admitted_probe_only",
        "formal_truth_eligible": False,
        "record_count": len(payload.records),
        "raw_content_sha256": raw_sha256,
        "raw_path": _relative(raw_path, root),
        "normalized_content_sha256": normalized_sha256,
        "normalized_path": _relative(normalized_path, root),
        "transport_receipt_status": (
            "present" if transport_receipt_path is not None else "missing"
        ),
        "transport_receipt_sha256": transport_receipt_sha256,
        "transport_receipt_path": (
            _relative(transport_receipt_path, root)
            if transport_receipt_path is not None
            else None
        ),
    }
    paths = _persist_receipt_and_checkpoint(root=root, query=query, receipt=receipt)
    return {
        "status": "passed",
        "probe_version": PROBE_VERSION,
        "mode": query.mode,
        "source": query.source,
        "source_role": query.policy.role,
        "dataset_type": query.policy.dataset_type,
        "index_ids": list(query.policy.index_ids),
        "request_fingerprint": query.request_fingerprint,
        "record_count": len(payload.records),
        "research_admission_status": "not_admitted_probe_only",
        "evidence_mode": evidence_mode,
        "raw_content_sha256": raw_sha256,
        "raw_path": _relative(raw_path, root),
        "normalized_content_sha256": normalized_sha256,
        "normalized_path": _relative(normalized_path, root),
        "transport_receipt_status": receipt["transport_receipt_status"],
        "transport_receipt_sha256": transport_receipt_sha256,
        "transport_receipt_path": receipt["transport_receipt_path"],
        **paths,
    }


def _validate_online_receipt(
    receipt: Any,
    *,
    query: ProbeQuery,
    root: Path,
    receipt_path: Path,
) -> tuple[dict[str, Any], Path, Path]:
    if not isinstance(receipt, Mapping):
        raise FactorEvidenceProbeError("stored receipt must be an object")
    value = dict(receipt)
    if set(value) != _ONLINE_RECEIPT_FIELDS:
        raise FactorEvidenceProbeError("stored receipt fields differ from the contract")
    expected = {
        "receipt_version": RECEIPT_VERSION,
        "probe_version": PROBE_VERSION,
        "mode": "online",
        "source": query.source,
        "source_role": query.policy.role,
        "provider_adapter_identity": (
            f"{query.policy.provider_module}.{query.policy.provider_class}"
        ),
        "dataset_type": query.policy.dataset_type,
        "index_ids": list(query.policy.index_ids),
        "screen_index_ids": (
            list(CHOICE_SCREEN_INDEX_IDS) if query.source == "choice" else []
        ),
        "reconciliation_index_ids": (
            list(CHOICE_RECONCILIATION_INDEX_IDS)
            if query.source == "choice"
            else []
        ),
        "request_fingerprint": query.request_fingerprint,
        "request": query.fingerprint_payload(),
        "research_admission_status": "not_admitted_probe_only",
        "formal_truth_eligible": False,
    }
    for field, expected_value in expected.items():
        if value.get(field) != expected_value:
            raise FactorEvidenceProbeError(f"stored receipt {field} mismatch")
    if value.get("evidence_mode") not in {"configured_runtime", "test_injected"}:
        raise FactorEvidenceProbeError("stored receipt evidence_mode mismatch")
    if str(value.get("provider_id") or "") not in query.policy.provider_ids:
        raise FactorEvidenceProbeError("stored receipt provider_id mismatch")
    _safe_segment(str(value.get("adapter_version") or ""), "adapter_version")
    if not str(value.get("upstream_source") or "").strip():
        raise FactorEvidenceProbeError("stored receipt upstream_source is empty")
    if not str(value.get("availability_status") or "").strip():
        raise FactorEvidenceProbeError("stored receipt availability_status is empty")
    if not str(value.get("point_in_time_status") or "").strip():
        raise FactorEvidenceProbeError("stored receipt point_in_time_status is empty")
    aware_datetime(value.get("requested_at"), "receipt.requested_at")
    digest = receipt_path.stem
    if _SHA256.fullmatch(digest) is None or sha256_bytes(receipt_path.read_bytes()) != digest:
        raise FactorEvidenceProbeError("stored receipt content hash mismatch")
    raw_digest = str(value.get("raw_content_sha256") or "")
    normalized_digest = str(value.get("normalized_content_sha256") or "")
    if _SHA256.fullmatch(raw_digest) is None or _SHA256.fullmatch(normalized_digest) is None:
        raise FactorEvidenceProbeError("stored receipt contains invalid content hashes")
    raw_path = _content_path(root, "raw", query.source, raw_digest, ".raw")
    normalized_path = _content_path(
        root, "normalized", query.source, normalized_digest, ".json"
    )
    if value.get("raw_path") != _relative(raw_path, root):
        raise FactorEvidenceProbeError("stored receipt raw_path mismatch")
    if value.get("normalized_path") != _relative(normalized_path, root):
        raise FactorEvidenceProbeError("stored receipt normalized_path mismatch")
    if not raw_path.is_file() or sha256_bytes(raw_path.read_bytes()) != raw_digest:
        raise FactorEvidenceProbeError("stored raw evidence is missing or corrupted")
    if (
        not normalized_path.is_file()
        or sha256_bytes(normalized_path.read_bytes()) != normalized_digest
    ):
        raise FactorEvidenceProbeError(
            "stored normalized evidence is missing or corrupted"
        )
    records = _strict_json(normalized_path.read_bytes(), "stored normalized evidence")
    if not isinstance(records, list) or any(not isinstance(item, Mapping) for item in records):
        raise FactorEvidenceProbeError("stored normalized evidence must be a list of objects")
    if value.get("record_count") != len(records) or not records:
        raise FactorEvidenceProbeError("stored receipt record_count mismatch")
    _assert_no_sensitive_keys(records, "stored normalized evidence")
    transport_status = value.get("transport_receipt_status")
    transport_digest = value.get("transport_receipt_sha256")
    transport_relative = value.get("transport_receipt_path")
    if transport_status == "missing":
        if transport_digest is not None or transport_relative is not None:
            raise FactorEvidenceProbeError(
                "stored missing transport receipt contains a path or hash"
            )
    elif transport_status == "present":
        if not isinstance(transport_digest, str) or _SHA256.fullmatch(transport_digest) is None:
            raise FactorEvidenceProbeError("stored transport receipt hash is invalid")
        transport_path = _content_path(
            root,
            "transport_receipts",
            query.source,
            transport_digest,
            ".json",
        )
        if transport_relative != _relative(transport_path, root):
            raise FactorEvidenceProbeError("stored transport receipt path mismatch")
        if (
            not transport_path.is_file()
            or sha256_bytes(transport_path.read_bytes()) != transport_digest
        ):
            raise FactorEvidenceProbeError(
                "stored transport receipt is missing or corrupted"
            )
        transport_value = _strict_json(
            transport_path.read_bytes(), "stored transport receipt"
        )
        if not isinstance(transport_value, Mapping) or not transport_value:
            raise FactorEvidenceProbeError(
                "stored transport receipt must be a non-empty object"
            )
        _assert_no_sensitive_keys(transport_value, "stored transport receipt")
    else:
        raise FactorEvidenceProbeError("stored transport receipt status mismatch")
    return value, raw_path, normalized_path


def _offline_probe(
    query: ProbeQuery, root: Path, *, allow_test_evidence: bool
) -> dict[str, Any]:
    receipt_root = (
        root
        / "receipts"
        / query.source
        / query.policy.dataset_type
        / query.request_fingerprint
    )
    candidates: list[tuple[datetime, str, dict[str, Any], Path, Path]] = []
    for receipt_path in sorted(receipt_root.glob("*/*.json")) if receipt_root.is_dir() else ():
        raw = receipt_path.read_bytes()
        receipt = _strict_json(raw, "stored receipt")
        if not isinstance(receipt, Mapping) or receipt.get("mode") != "online":
            continue
        value, raw_path, normalized_path = _validate_online_receipt(
            receipt, query=query, root=root, receipt_path=receipt_path
        )
        if value.get("evidence_mode") == "test_injected" and not allow_test_evidence:
            continue
        fetched_at = aware_datetime(value.get("fetched_at"), "receipt.fetched_at")
        if fetched_at <= query.evidence_cutoff_at:  # type: ignore[operator]
            candidates.append(
                (fetched_at, receipt_path.stem, value, raw_path, normalized_path)
            )
    if not candidates:
        raise FactorEvidenceProbeError(
            "offline evidence cache miss at the explicit evidence cutoff"
        )
    fetched_at, online_receipt_sha256, online_receipt, raw_path, normalized_path = max(
        candidates, key=lambda item: (item[0], item[1])
    )
    receipt = {
        "receipt_version": RECEIPT_VERSION,
        "probe_version": PROBE_VERSION,
        "evidence_mode": online_receipt.get("evidence_mode"),
        "mode": "offline",
        "source": query.source,
        "source_role": query.policy.role,
        "provider_id": online_receipt["provider_id"],
        "provider_adapter_identity": online_receipt[
            "provider_adapter_identity"
        ],
        "adapter_version": online_receipt["adapter_version"],
        "upstream_source": online_receipt["upstream_source"],
        "dataset_type": query.policy.dataset_type,
        "index_ids": list(query.policy.index_ids),
        "request_fingerprint": query.request_fingerprint,
        "request": query.fingerprint_payload(),
        "requested_at": query.requested_at.isoformat(),
        "evidence_cutoff_at": query.evidence_cutoff_at.isoformat(),  # type: ignore[union-attr]
        "fetched_at": fetched_at.isoformat(),
        "captured_fetched_at": fetched_at.isoformat(),
        "availability_status": online_receipt["availability_status"],
        "point_in_time_status": online_receipt["point_in_time_status"],
        "verified_online_receipt_sha256": online_receipt_sha256,
        "raw_content_sha256": online_receipt["raw_content_sha256"],
        "raw_path": _relative(raw_path, root),
        "normalized_content_sha256": online_receipt["normalized_content_sha256"],
        "normalized_path": _relative(normalized_path, root),
        "transport_receipt_status": online_receipt["transport_receipt_status"],
        "transport_receipt_sha256": online_receipt["transport_receipt_sha256"],
        "transport_receipt_path": online_receipt["transport_receipt_path"],
        "record_count": online_receipt["record_count"],
        "research_admission_status": "not_admitted_probe_only",
        "formal_truth_eligible": False,
        "verification_status": "hashes_and_fixed_request_verified",
    }
    paths = _persist_receipt_and_checkpoint(root=root, query=query, receipt=receipt)
    return {
        "status": "passed",
        "probe_version": PROBE_VERSION,
        "mode": query.mode,
        "source": query.source,
        "source_role": query.policy.role,
        "dataset_type": query.policy.dataset_type,
        "index_ids": list(query.policy.index_ids),
        "request_fingerprint": query.request_fingerprint,
        "record_count": online_receipt["record_count"],
        "research_admission_status": "not_admitted_probe_only",
        "evidence_mode": online_receipt.get("evidence_mode"),
        "verified_online_receipt_sha256": online_receipt_sha256,
        "raw_content_sha256": online_receipt["raw_content_sha256"],
        "raw_path": _relative(raw_path, root),
        "normalized_content_sha256": online_receipt["normalized_content_sha256"],
        "normalized_path": _relative(normalized_path, root),
        "transport_receipt_status": online_receipt["transport_receipt_status"],
        "transport_receipt_sha256": online_receipt["transport_receipt_sha256"],
        "transport_receipt_path": online_receipt["transport_receipt_path"],
        **paths,
    }


def run_probe(
    query: ProbeQuery,
    output_root: Path | str,
    *,
    provider_loader: ProviderLoader | None = None,
    request_factory: RequestFactory | None = None,
    repository_root: Path | str = REPOSITORY_ROOT,
    allow_test_evidence: bool = False,
) -> dict[str, Any]:
    """Run one fixed online capture or local-only offline verification."""

    if type(allow_test_evidence) is not bool:
        raise FactorEvidenceProbeError("allow_test_evidence must be a boolean")
    root = _ensure_under_repository(output_root, repository_root=repository_root)
    if query.mode == "offline":
        return _offline_probe(
            query, root, allow_test_evidence=allow_test_evidence
        )
    if (provider_loader is None) != (request_factory is None):
        raise FactorEvidenceProbeError(
            "test injection requires both provider_loader and request_factory"
        )
    evidence_mode = (
        "configured_runtime"
        if provider_loader is None and request_factory is None
        else "test_injected"
    )
    return _online_probe(
        query,
        root,
        provider_loader=provider_loader or _default_provider_loader,
        request_factory=request_factory or _default_request_factory,
        evidence_mode=evidence_mode,
    )


def _parse_aware_datetime(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
        return aware_datetime(parsed, label)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            f"{label} must be an ISO datetime with timezone offset"
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=tuple(SOURCE_POLICIES), required=True)
    parser.add_argument("--mode", choices=("online", "offline"), required=True)
    parser.add_argument("--start-date", type=date.fromisoformat, required=True)
    parser.add_argument("--end-date", type=date.fromisoformat, required=True)
    parser.add_argument(
        "--requested-at",
        type=lambda value: _parse_aware_datetime(value, "requested_at"),
        help="controlled execution timestamp; defaults to the current aware local time",
    )
    parser.add_argument(
        "--evidence-cutoff-at",
        type=lambda value: _parse_aware_datetime(value, "evidence_cutoff_at"),
        help="required in offline mode; only captures fetched on/before this time qualify",
    )
    parser.add_argument("--output-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root: Path | None = None
    query: ProbeQuery | None = None
    try:
        query = ProbeQuery(
            source=args.source,
            mode=args.mode,
            start_date=args.start_date,
            end_date=args.end_date,
            requested_at=args.requested_at or datetime.now().astimezone(),
            evidence_cutoff_at=args.evidence_cutoff_at,
        )
        root = _ensure_under_repository(
            args.output_root,
            repository_root=REPOSITORY_ROOT,
        )
        result = run_probe(query, root)
    except Exception as exc:
        error = classify_unexpected_error(exc)
        failure_paths: dict[str, Any] = {}
        if query is not None and root is not None:
            try:
                failure_paths = _persist_failed_checkpoint(
                    root=root,
                    query=query,
                    error=exc,
                )
            except Exception:
                # A failure to persist must not obscure the original provider
                # failure or leak additional filesystem details to stdout.
                failure_paths = {"checkpoint_status": "persistence_failed"}
        result = {
            "status": error.status,
            "probe_version": PROBE_VERSION,
            "mode": args.mode,
            "source": args.source,
            "research_admission_status": "not_admitted_probe_only",
            "error_code": error.code,
            "error_type": type(exc).__name__,
            "error": safe_error_text(error),
            **failure_paths,
        }
    sys.stdout.write(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
