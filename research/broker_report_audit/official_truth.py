"""Controlled official-truth receipts and isolated Choice candidates.

Formal truth will only be created from a response captured by a registered,
read-only source-owned transport.  Those transports are not implemented yet,
so the current public adapters return ``not_configured`` and no receipt can be
converted to formal truth.  There is intentionally no ``verified=True``
argument and no loader that promotes an arbitrary local JSON file.

Choice remains a secondary candidate source.  Its candidate type and SQLite
table have no conversion path to :class:`TruthObservation`.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from .models import (
    TruthObservation,
    decimal_or_none,
    ensure_aware,
    parse_datetime,
    stable_identifier,
)


OFFICIAL_TRUTH_RECEIPT_VERSION = "official-truth-receipt.v1"
CHOICE_TRUTH_CANDIDATE_VERSION = "choice-truth-candidate.v1"
TRUTH_EVIDENCE_SCHEMA_VERSION = 1
OFFICIAL_ADMISSION_STATUS = "not_configured"
CHOICE_CANDIDATE_STATUS = "diagnostic_choice_secondary_not_admitted"
_AUTHORITY = object()
_SECRET_KEY_PATTERN = re.compile(
    r"(?:password|passwd|token|cookie|secret|authorization|api[_-]?key|phone|mobile|account)",
    re.IGNORECASE,
)


class OfficialTruthError(ValueError):
    """Base exception for controlled official truth evidence."""


class OfficialTruthReceiptError(OfficialTruthError):
    """Raised when source, timing, release, or semantic evidence is invalid."""


class OfficialTruthStorageError(OfficialTruthError):
    """Raised on additive evidence-store migration or immutable collisions."""


@dataclass(frozen=True)
class TruthSemanticPolicy:
    unit: str
    basis: str
    observation_kind: str = "scalar"

    def __post_init__(self) -> None:
        if not self.unit or not self.basis:
            raise OfficialTruthReceiptError("truth semantic unit and basis are required")
        if self.observation_kind not in {"scalar", "industry_mapping"}:
            raise OfficialTruthReceiptError("unsupported truth observation kind")


@dataclass(frozen=True)
class OfficialSourcePolicy:
    adapter_id: str
    adapter_version: str
    source_id: str
    truth_source: str
    domains: tuple[str, ...]
    semantics: Mapping[tuple[str, str], TruthSemanticPolicy]

    def __post_init__(self) -> None:
        if not all(
            str(value).strip()
            for value in (
                self.adapter_id,
                self.adapter_version,
                self.source_id,
                self.truth_source,
            )
        ):
            raise OfficialTruthReceiptError("official source policy identity is incomplete")
        domains = tuple(
            str(value).strip().lower().rstrip(".") for value in self.domains
        )
        if not domains:
            raise OfficialTruthReceiptError("official source policy needs domains")
        object.__setattr__(self, "domains", domains)
        object.__setattr__(self, "semantics", MappingProxyType(dict(self.semantics)))


def _semantics(*items: tuple[str, str, str, str, str]) -> Mapping[tuple[str, str], TruthSemanticPolicy]:
    return MappingProxyType(
        {
            (dimension, target_type.casefold()): TruthSemanticPolicy(
                unit=unit, basis=basis, observation_kind=kind
            )
            for dimension, target_type, unit, basis, kind in items
        }
    )


_OFFICIAL_POLICIES: Mapping[str, OfficialSourcePolicy] = MappingProxyType(
    {
        "cninfo_first_disclosure_v1": OfficialSourcePolicy(
            adapter_id="cninfo_first_disclosure_v1",
            adapter_version="1.0.0",
            source_id="CNINFO",
            truth_source="cninfo_first_disclosure",
            domains=("cninfo.com.cn",),
            semantics=_semantics(
                ("stock", "EPS", "CNY/share", "basic_eps_first_disclosed", "scalar"),
                ("stock", "earnings_revision", "CNY/share", "basic_eps_first_disclosed", "scalar"),
            ),
        ),
        "nbs_first_release_v1": OfficialSourcePolicy(
            adapter_id="nbs_first_release_v1",
            adapter_version="1.0.0",
            source_id="NBS_FIRST_RELEASE",
            truth_source="stats_nbs_first_release",
            domains=("stats.gov.cn",),
            semantics=_semantics(
                ("macro", "GDP", "%", "yoy_first_release", "scalar"),
                ("macro", "CPI", "%", "yoy_first_release", "scalar"),
                ("macro", "PPI", "%", "yoy_first_release", "scalar"),
            ),
        ),
        "pboc_statistics_v1": OfficialSourcePolicy(
            adapter_id="pboc_statistics_v1",
            adapter_version="1.0.0",
            source_id="PBOC_STATISTICS",
            truth_source="pboc_statistics",
            domains=("pbc.gov.cn",),
            semantics=_semantics(
                ("macro", "social_financing", "CNY_100m", "monthly_increment_first_release", "scalar"),
                ("macro", "liquidity", "%", "yoy_first_release", "scalar"),
                ("macro", "interest_rate", "%", "official_fixing", "scalar"),
            ),
        ),
        "chinamoney_point_in_time_v1": OfficialSourcePolicy(
            adapter_id="chinamoney_point_in_time_v1",
            adapter_version="1.0.0",
            source_id="CHINAMONEY",
            truth_source="chinamoney_point_in_time",
            domains=("chinamoney.com.cn",),
            semantics=_semantics(
                ("macro", "interest_rate", "%", "official_fixing", "scalar"),
                ("macro", "exchange_rate", "CNY_per_unit", "official_fixing", "scalar"),
            ),
        ),
        "sw_index_point_in_time_v1": OfficialSourcePolicy(
            adapter_id="sw_index_point_in_time_v1",
            adapter_version="1.0.0",
            source_id="SW_INDEX",
            truth_source="sw_index_point_in_time",
            domains=("swsresearch.com",),
            semantics=_semantics(
                ("industry", "industry_membership", "membership", "point_in_time", "industry_mapping"),
            ),
        ),
        "csi_industry_point_in_time_v1": OfficialSourcePolicy(
            adapter_id="csi_industry_point_in_time_v1",
            adapter_version="1.0.0",
            source_id="CSI_INDUSTRY",
            truth_source="csi_industry_point_in_time",
            domains=("csindex.com.cn",),
            semantics=_semantics(
                ("industry", "industry_membership", "membership", "point_in_time", "industry_mapping"),
            ),
        ),
    }
)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _normalise_hash(value: Any, field_name: str) -> str:
    digest = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise OfficialTruthReceiptError(f"{field_name} must be SHA-256")
    return digest


def _strict_timestamp(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise OfficialTruthReceiptError(f"{field_name} must be an ISO-8601 string")
    text = value.strip()
    if "T" not in text or (
        not text.endswith("Z") and not re.search(r"[+-]\d\d:\d\d$", text)
    ):
        raise OfficialTruthReceiptError(
            f"{field_name} must include an explicit timezone"
        )
    try:
        return parse_datetime(text)
    except ValueError as exc:
        raise OfficialTruthReceiptError(f"invalid {field_name}") from exc


def _official_url(value: Any, policy: OfficialSourcePolicy, field_name: str) -> str:
    text = str(value or "").strip()
    parsed = urlsplit(text)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise OfficialTruthReceiptError(f"{field_name} must be an official HTTPS URL")
    hostname = parsed.hostname.lower().rstrip(".")
    if not any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in policy.domains
    ):
        raise OfficialTruthReceiptError(f"{field_name} host is not official for {policy.source_id}")
    return text


def _freeze_request_params(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OfficialTruthReceiptError("request_params must be a mapping")

    def clean(item: Any, path: str) -> Any:
        if item is None or isinstance(item, (str, int, bool)):
            return item
        if isinstance(item, float):
            if not float("-inf") < item < float("inf"):
                raise OfficialTruthReceiptError(f"non-finite request parameter: {path}")
            return item
        if isinstance(item, (list, tuple)):
            return tuple(clean(child, f"{path}[]") for child in item)
        if isinstance(item, Mapping):
            result: dict[str, Any] = {}
            for key, child in item.items():
                if not isinstance(key, str) or not key:
                    raise OfficialTruthReceiptError("request parameter keys must be text")
                if _SECRET_KEY_PATTERN.search(key):
                    raise OfficialTruthReceiptError(
                        f"secret-like request parameter is forbidden: {key}"
                    )
                result[key] = clean(child, f"{path}.{key}")
            return MappingProxyType(result)
        raise OfficialTruthReceiptError(f"unsupported request parameter: {path}")

    return clean(value, "request_params")


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(child) for child in value]
    return value


@dataclass(frozen=True)
class OfficialTruthReceipt:
    """One immutable receipt generated by a registered official adapter."""

    adapter_id: str
    adapter_version: str
    source_id: str
    truth_source: str
    dimension: str
    subject_id: str
    target_type: str
    forecast_period: str
    unit: str
    basis: str
    realized_value: Decimal
    observation_kind: str
    release_kind: str
    release_version: int
    revision_of: str
    endpoint_url: str
    final_url: str
    http_status: int
    transport_provenance_sha256: str
    response_headers_sha256: str
    document_url: str
    request_params: Mapping[str, Any]
    raw_response_sha256: str
    document_sha256: str
    observed_at: datetime
    available_at: datetime
    fetched_at: datetime
    mapping_valid_from: datetime | None = None
    mapping_valid_to: datetime | None = None
    mapping_version: str = ""
    receipt_id: str = ""
    contract_version: str = OFFICIAL_TRUTH_RECEIPT_VERSION
    _authority: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._authority is not _AUTHORITY:
            raise OfficialTruthReceiptError(
                "official receipts must be created by a registered adapter capture"
            )
        if self.contract_version != OFFICIAL_TRUTH_RECEIPT_VERSION:
            raise OfficialTruthReceiptError("official receipt contract version mismatch")
        policy = _OFFICIAL_POLICIES.get(self.adapter_id)
        if policy is None or (
            self.adapter_version,
            self.source_id,
            self.truth_source,
        ) != (policy.adapter_version, policy.source_id, policy.truth_source):
            raise OfficialTruthReceiptError("official adapter identity mismatch")
        if self.dimension not in {"macro", "industry", "stock"}:
            raise OfficialTruthReceiptError("invalid truth dimension")
        for name in ("subject_id", "target_type", "forecast_period", "unit", "basis"):
            if not str(getattr(self, name) or "").strip():
                raise OfficialTruthReceiptError(f"{name} is required")
        semantic = policy.semantics.get((self.dimension, self.target_type.casefold()))
        if semantic is None:
            raise OfficialTruthReceiptError("target is not registered for the official adapter")
        if (self.unit, self.basis, self.observation_kind) != (
            semantic.unit,
            semantic.basis,
            semantic.observation_kind,
        ):
            raise OfficialTruthReceiptError("truth unit/basis/observation kind mismatch")
        if self.release_kind not in {"first_release", "revision"}:
            raise OfficialTruthReceiptError("release_kind must be first_release or revision")
        if isinstance(self.release_version, bool) or self.release_version < 1:
            raise OfficialTruthReceiptError("release_version must be a positive integer")
        if self.release_kind == "first_release":
            if self.release_version != 1 or self.revision_of:
                raise OfficialTruthReceiptError("first release cannot masquerade as a revision")
        elif self.release_version < 2 or not self.revision_of:
            raise OfficialTruthReceiptError("revision requires release_version >=2 and revision_of")
        realized = decimal_or_none(self.realized_value)
        if realized is None:
            raise OfficialTruthReceiptError("realized_value is required")
        object.__setattr__(self, "realized_value", realized)
        object.__setattr__(self, "raw_response_sha256", _normalise_hash(self.raw_response_sha256, "raw_response_sha256"))
        object.__setattr__(self, "document_sha256", _normalise_hash(self.document_sha256, "document_sha256"))
        object.__setattr__(
            self,
            "transport_provenance_sha256",
            _normalise_hash(
                self.transport_provenance_sha256, "transport_provenance_sha256"
            ),
        )
        object.__setattr__(
            self,
            "response_headers_sha256",
            _normalise_hash(self.response_headers_sha256, "response_headers_sha256"),
        )
        object.__setattr__(self, "endpoint_url", _official_url(self.endpoint_url, policy, "endpoint_url"))
        object.__setattr__(self, "final_url", _official_url(self.final_url, policy, "final_url"))
        if isinstance(self.http_status, bool) or self.http_status != 200:
            raise OfficialTruthReceiptError("official transport HTTP status must be 200")
        object.__setattr__(self, "document_url", _official_url(self.document_url, policy, "document_url"))
        object.__setattr__(self, "request_params", _freeze_request_params(self.request_params))
        ensure_aware(self.observed_at, "observed_at")
        ensure_aware(self.available_at, "available_at")
        ensure_aware(self.fetched_at, "fetched_at")
        if not self.observed_at <= self.available_at <= self.fetched_at:
            raise OfficialTruthReceiptError("truth timestamps must satisfy observed <= available <= fetched")
        if self.observation_kind == "industry_mapping":
            if self.mapping_valid_from is None or self.mapping_valid_to is None or not self.mapping_version:
                raise OfficialTruthReceiptError("industry mapping requires validity range and version")
            ensure_aware(self.mapping_valid_from, "mapping_valid_from")
            ensure_aware(self.mapping_valid_to, "mapping_valid_to")
            if self.mapping_valid_from > self.mapping_valid_to:
                raise OfficialTruthReceiptError("industry mapping validity range is reversed")
        elif self.mapping_valid_from is not None or self.mapping_valid_to is not None or self.mapping_version:
            raise OfficialTruthReceiptError("scalar truth cannot carry industry mapping fields")
        expected_id = stable_identifier(
            OFFICIAL_TRUTH_RECEIPT_VERSION,
            self.adapter_id,
            self.adapter_version,
            self.source_id,
            self.truth_source,
            self.dimension,
            self.subject_id,
            self.target_type,
            self.forecast_period,
            self.unit,
            self.basis,
            self.realized_value,
            self.observation_kind,
            self.release_kind,
            self.release_version,
            self.revision_of,
            self.endpoint_url,
            self.final_url,
            self.http_status,
            self.transport_provenance_sha256,
            self.response_headers_sha256,
            self.document_url,
            _sha256(_canonical_bytes(_plain_json(self.request_params))),
            self.raw_response_sha256,
            self.document_sha256,
            self.observed_at.isoformat(),
            self.available_at.isoformat(),
            self.fetched_at.isoformat(),
            self.mapping_valid_from.isoformat() if self.mapping_valid_from else "",
            self.mapping_valid_to.isoformat() if self.mapping_valid_to else "",
            self.mapping_version,
        )
        if self.receipt_id and self.receipt_id != expected_id:
            raise OfficialTruthReceiptError("receipt_id does not match immutable receipt payload")
        object.__setattr__(self, "receipt_id", expected_id)

    @property
    def admission_status(self) -> str:
        return OFFICIAL_ADMISSION_STATUS

    def is_valid_for_decision(self, decision_time: datetime) -> bool:
        ensure_aware(decision_time, "decision_time")
        if self.available_at > decision_time or self.fetched_at > decision_time:
            return False
        if self.observation_kind == "industry_mapping":
            assert self.mapping_valid_from is not None and self.mapping_valid_to is not None
            return self.mapping_valid_from <= decision_time <= self.mapping_valid_to
        return True

    def to_truth_observation(self, *, as_of: datetime) -> TruthObservation:
        """Fail closed until a source-owned transport is implemented."""

        ensure_aware(as_of, "as_of")
        raise OfficialTruthReceiptError(
            "not_configured: no source-owned transport receipt can currently unlock formal truth"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "receipt_id": self.receipt_id,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "source_id": self.source_id,
            "truth_source": self.truth_source,
            "dimension": self.dimension,
            "subject_id": self.subject_id,
            "target_type": self.target_type,
            "forecast_period": self.forecast_period,
            "unit": self.unit,
            "basis": self.basis,
            "realized_value": format(self.realized_value, "f"),
            "observation_kind": self.observation_kind,
            "release_kind": self.release_kind,
            "release_version": self.release_version,
            "revision_of": self.revision_of,
            "endpoint_url": self.endpoint_url,
            "final_url": self.final_url,
            "http_status": self.http_status,
            "transport_provenance_sha256": self.transport_provenance_sha256,
            "response_headers_sha256": self.response_headers_sha256,
            "document_url": self.document_url,
            "request_params": _plain_json(self.request_params),
            "raw_response_sha256": self.raw_response_sha256,
            "document_sha256": self.document_sha256,
            "observed_at": self.observed_at.isoformat(),
            "available_at": self.available_at.isoformat(),
            "fetched_at": self.fetched_at.isoformat(),
            "mapping_valid_from": self.mapping_valid_from.isoformat() if self.mapping_valid_from else "",
            "mapping_valid_to": self.mapping_valid_to.isoformat() if self.mapping_valid_to else "",
            "mapping_version": self.mapping_version,
            "admission_status": OFFICIAL_ADMISSION_STATUS,
        }


@dataclass(frozen=True)
class OfficialTruthCapture:
    receipt: OfficialTruthReceipt
    raw_response: bytes = field(repr=False)
    document_bytes: bytes = field(repr=False)
    _authority: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._authority is not _AUTHORITY or self.receipt._authority is not _AUTHORITY:
            raise OfficialTruthReceiptError("capture was not produced by an official adapter")
        if _sha256(self.raw_response) != self.receipt.raw_response_sha256:
            raise OfficialTruthReceiptError("raw response hash mismatch")
        if _sha256(self.document_bytes) != self.receipt.document_sha256:
            raise OfficialTruthReceiptError("official document hash mismatch")


def _strict_json_object(body: bytes) -> Mapping[str, Any]:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite number: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise OfficialTruthReceiptError("official response must be strict UTF-8 JSON") from exc
    if not isinstance(payload, Mapping):
        raise OfficialTruthReceiptError("official response root must be an object")
    return payload


class OfficialTruthAdapter:
    """Read-only registered adapter boundary for one official source."""

    __slots__ = ("_policy", "_authority")

    def __init__(self, policy: OfficialSourcePolicy, *, _authority: object = None) -> None:
        if _authority is not _AUTHORITY:
            raise OfficialTruthReceiptError("official adapters come from the registry")
        self._policy = policy
        self._authority = _AUTHORITY

    @property
    def adapter_id(self) -> str:
        return self._policy.adapter_id

    def capture_response(
        self,
        *,
        endpoint_url: str,
        request_params: Mapping[str, Any],
        raw_response: bytes,
        document_bytes: bytes,
        fetched_at: datetime,
    ) -> OfficialTruthCapture:
        """Reject caller-supplied bytes; they cannot establish provenance.

        A matching official-looking URL, response hash, local PDF, or caller
        timestamp cannot prove that an official server returned those bytes.
        The source-specific network transports and parsers are deliberately
        still ``not_configured``; until they are implemented, no public API can
        mint a formally admitted receipt.
        """

        del endpoint_url, request_params, raw_response, document_bytes, fetched_at
        raise OfficialTruthReceiptError(
            "not_configured: direct caller-supplied response capture cannot unlock official truth"
        )

    def fetch(self, *, request_params: Mapping[str, Any]) -> OfficialTruthCapture:
        """Fail closed until this adapter owns a real HTTPS transport/parser."""

        _freeze_request_params(request_params)
        raise OfficialTruthReceiptError(
            f"not_configured: controlled transport for {self.adapter_id} is unavailable"
        )


def get_official_truth_adapter(adapter_id: str) -> OfficialTruthAdapter:
    policy = _OFFICIAL_POLICIES.get(str(adapter_id or "").strip())
    if policy is None:
        raise OfficialTruthReceiptError(f"official truth adapter is not configured: {adapter_id}")
    return OfficialTruthAdapter(policy, _authority=_AUTHORITY)


@dataclass(frozen=True)
class ChoiceTruthCandidate:
    subject_id: str
    target_type: str
    forecast_period: str
    value: Decimal
    unit: str
    basis: str
    observed_at: datetime
    available_at: datetime
    fetched_at: datetime
    endpoint_name: str
    request_params: Mapping[str, Any]
    raw_response_sha256: str
    candidate_id: str = ""
    contract_version: str = CHOICE_TRUTH_CANDIDATE_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != CHOICE_TRUTH_CANDIDATE_VERSION:
            raise OfficialTruthReceiptError("Choice candidate contract mismatch")
        for name in ("subject_id", "target_type", "forecast_period", "unit", "basis", "endpoint_name"):
            if not str(getattr(self, name) or "").strip():
                raise OfficialTruthReceiptError(f"Choice candidate {name} is required")
        converted = decimal_or_none(self.value)
        if converted is None:
            raise OfficialTruthReceiptError("Choice candidate value is required")
        object.__setattr__(self, "value", converted)
        ensure_aware(self.observed_at, "observed_at")
        ensure_aware(self.available_at, "available_at")
        ensure_aware(self.fetched_at, "fetched_at")
        if not self.observed_at <= self.available_at <= self.fetched_at:
            raise OfficialTruthReceiptError("Choice timestamps must satisfy observed <= available <= fetched")
        object.__setattr__(self, "request_params", _freeze_request_params(self.request_params))
        object.__setattr__(self, "raw_response_sha256", _normalise_hash(self.raw_response_sha256, "raw_response_sha256"))
        expected = stable_identifier(
            CHOICE_TRUTH_CANDIDATE_VERSION,
            self.subject_id,
            self.target_type,
            self.forecast_period,
            self.value,
            self.unit,
            self.basis,
            self.observed_at.isoformat(),
            self.available_at.isoformat(),
            self.fetched_at.isoformat(),
            self.endpoint_name,
            _sha256(_canonical_bytes(_plain_json(self.request_params))),
            self.raw_response_sha256,
        )
        if self.candidate_id and self.candidate_id != expected:
            raise OfficialTruthReceiptError("Choice candidate_id mismatch")
        object.__setattr__(self, "candidate_id", expected)

    @property
    def admission_status(self) -> str:
        return CHOICE_CANDIDATE_STATUS

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "candidate_id": self.candidate_id,
            "source": "choice",
            "admission_status": CHOICE_CANDIDATE_STATUS,
            "subject_id": self.subject_id,
            "target_type": self.target_type,
            "forecast_period": self.forecast_period,
            "value": format(self.value, "f"),
            "unit": self.unit,
            "basis": self.basis,
            "observed_at": self.observed_at.isoformat(),
            "available_at": self.available_at.isoformat(),
            "fetched_at": self.fetched_at.isoformat(),
            "endpoint_name": self.endpoint_name,
            "request_params": _plain_json(self.request_params),
            "raw_response_sha256": self.raw_response_sha256,
        }


@dataclass(frozen=True)
class ChoiceTruthCandidateCapture:
    candidate: ChoiceTruthCandidate
    raw_response: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.raw_response, bytes) or _sha256(self.raw_response) != self.candidate.raw_response_sha256:
            raise OfficialTruthReceiptError("Choice candidate raw response hash mismatch")


def make_choice_truth_candidate(
    *,
    raw_response: bytes,
    subject_id: str,
    target_type: str,
    forecast_period: str,
    value: Any,
    unit: str,
    basis: str,
    observed_at: datetime,
    available_at: datetime,
    fetched_at: datetime,
    endpoint_name: str,
    request_params: Mapping[str, Any],
) -> ChoiceTruthCandidateCapture:
    """Create a diagnostic Choice candidate with no formal-truth conversion."""

    if not isinstance(raw_response, bytes) or not raw_response:
        raise OfficialTruthReceiptError("Choice raw_response must contain bytes")
    candidate = ChoiceTruthCandidate(
        subject_id=subject_id,
        target_type=target_type,
        forecast_period=forecast_period,
        value=value,
        unit=unit,
        basis=basis,
        observed_at=observed_at,
        available_at=available_at,
        fetched_at=fetched_at,
        endpoint_name=endpoint_name,
        request_params=request_params,
        raw_response_sha256=_sha256(raw_response),
    )
    return ChoiceTruthCandidateCapture(candidate=candidate, raw_response=raw_response)


def install_truth_evidence_schema(connection: sqlite3.Connection) -> None:
    """Install additive receipt/candidate tables without changing audit history."""

    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='truth_evidence_meta'"
    ).fetchone()
    if row:
        version_row = connection.execute(
            "SELECT value FROM truth_evidence_meta WHERE key='schema_version'"
        ).fetchone()
        if version_row is None:
            raise OfficialTruthStorageError("truth evidence schema metadata is missing")
        try:
            version = int(version_row[0])
        except (TypeError, ValueError) as exc:
            raise OfficialTruthStorageError("truth evidence schema version is invalid") from exc
        if version > TRUTH_EVIDENCE_SCHEMA_VERSION:
            raise OfficialTruthStorageError("truth evidence database is newer than this code")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS truth_evidence_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS official_truth_receipts_v1 (
            receipt_id TEXT PRIMARY KEY,
            receipt_json TEXT NOT NULL,
            receipt_sha256 TEXT NOT NULL,
            adapter_id TEXT NOT NULL,
            truth_source TEXT NOT NULL,
            release_kind TEXT NOT NULL,
            observation_kind TEXT NOT NULL,
            available_at TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            raw_response BLOB NOT NULL,
            document_bytes BLOB NOT NULL
        );
        CREATE TABLE IF NOT EXISTS choice_truth_candidates_v1 (
            candidate_id TEXT PRIMARY KEY,
            candidate_json TEXT NOT NULL,
            candidate_sha256 TEXT NOT NULL,
            admission_status TEXT NOT NULL CHECK (
                admission_status='diagnostic_choice_secondary_not_admitted'
            ),
            available_at TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            raw_response BLOB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS official_truth_receipts_available_idx
            ON official_truth_receipts_v1(truth_source, available_at, release_kind);
        CREATE INDEX IF NOT EXISTS choice_truth_candidates_available_idx
            ON choice_truth_candidates_v1(available_at);
        """
    )
    connection.execute(
        "INSERT INTO truth_evidence_meta(key,value) VALUES('schema_version',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(TRUTH_EVIDENCE_SCHEMA_VERSION),),
    )
    connection.commit()


@dataclass(frozen=True)
class OfficialReceiptIngestResult:
    output_directory: Path
    paths: Mapping[str, Path]
    counts: Mapping[str, int]
    receipt_ids: tuple[str, ...]
    truth_observations: tuple[TruthObservation, ...]


@dataclass(frozen=True)
class ChoiceCandidateIngestResult:
    output_directory: Path
    paths: Mapping[str, Path]
    counts: Mapping[str, int]
    candidate_ids: tuple[str, ...]


def _persist_receipt(
    connection: sqlite3.Connection, capture: OfficialTruthCapture
) -> bool:
    receipt = capture.receipt
    payload = receipt.as_dict()
    body = _canonical_bytes(payload)
    digest = _sha256(body)
    existing = connection.execute(
        "SELECT receipt_json,raw_response,document_bytes FROM official_truth_receipts_v1 WHERE receipt_id=?",
        (receipt.receipt_id,),
    ).fetchone()
    if existing is not None:
        if existing != (body.decode("utf-8"), capture.raw_response, capture.document_bytes):
            raise OfficialTruthStorageError("immutable official receipt collision")
        return False
    connection.execute(
        "INSERT INTO official_truth_receipts_v1("
        "receipt_id,receipt_json,receipt_sha256,adapter_id,truth_source,release_kind,"
        "observation_kind,available_at,fetched_at,raw_response,document_bytes"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            receipt.receipt_id,
            body.decode("utf-8"),
            digest,
            receipt.adapter_id,
            receipt.truth_source,
            receipt.release_kind,
            receipt.observation_kind,
            receipt.available_at.isoformat(),
            receipt.fetched_at.isoformat(),
            capture.raw_response,
            capture.document_bytes,
        ),
    )
    return True


def ingest_official_receipts(
    captures: Iterable[OfficialTruthCapture],
    *,
    database_path: str | Path,
    as_of: datetime,
) -> OfficialReceiptIngestResult:
    """Persist controlled captures and return formal first-release scalars.

    The returned observations still need to be appended through
    :class:`AuditStore` by the standard CLI.  This function never accepts a
    path to caller-authored receipt JSON and never writes ``truth_observations``
    directly.
    """

    ensure_aware(as_of, "as_of")
    items = tuple(captures)
    if any(not isinstance(item, OfficialTruthCapture) for item in items):
        raise OfficialTruthReceiptError(
            "ingest_official_receipts accepts controlled capture objects only"
        )
    if items:
        raise OfficialTruthReceiptError(
            "not_configured: official source transports are not implemented"
        )
    database = Path(database_path)
    database.parent.mkdir(parents=True, exist_ok=True)
    inserted = 0
    observations: list[TruthObservation] = []
    receipt_ids: list[str] = []
    connection = sqlite3.connect(database)
    try:
        install_truth_evidence_schema(connection)
        connection.execute("BEGIN")
        for capture in items:
            receipt = capture.receipt
            if receipt.fetched_at > as_of or receipt.available_at > as_of:
                raise OfficialTruthReceiptError("official receipt contains future information")
            inserted += int(_persist_receipt(connection, capture))
            receipt_ids.append(receipt.receipt_id)
            if receipt.observation_kind == "scalar" and receipt.release_kind == "first_release":
                observations.append(receipt.to_truth_observation(as_of=as_of))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    counts = MappingProxyType(
        {
            "received": len(items),
            "inserted": inserted,
            "unchanged": len(items) - inserted,
            "formal_scalar_first_release": len(observations),
            "revision_or_mapping": len(items) - len(observations),
        }
    )
    return OfficialReceiptIngestResult(
        output_directory=database.parent,
        paths=MappingProxyType({"database": database}),
        counts=counts,
        receipt_ids=tuple(receipt_ids),
        truth_observations=tuple(observations),
    )


def ingest_choice_truth_candidates(
    captures: Iterable[ChoiceTruthCandidateCapture],
    *,
    database_path: str | Path,
) -> ChoiceCandidateIngestResult:
    """Persist Choice candidates only in their diagnostic table."""

    items = tuple(captures)
    if any(not isinstance(item, ChoiceTruthCandidateCapture) for item in items):
        raise OfficialTruthReceiptError("Choice ingestion accepts candidate captures only")
    database = Path(database_path)
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    inserted = 0
    candidate_ids: list[str] = []
    try:
        install_truth_evidence_schema(connection)
        connection.execute("BEGIN")
        for capture in items:
            candidate = capture.candidate
            body = _canonical_bytes(candidate.as_dict())
            existing = connection.execute(
                "SELECT candidate_json,raw_response FROM choice_truth_candidates_v1 WHERE candidate_id=?",
                (candidate.candidate_id,),
            ).fetchone()
            if existing is not None:
                if existing != (body.decode("utf-8"), capture.raw_response):
                    raise OfficialTruthStorageError("immutable Choice candidate collision")
            else:
                connection.execute(
                    "INSERT INTO choice_truth_candidates_v1("
                    "candidate_id,candidate_json,candidate_sha256,admission_status,available_at,fetched_at,raw_response"
                    ") VALUES(?,?,?,?,?,?,?)",
                    (
                        candidate.candidate_id,
                        body.decode("utf-8"),
                        _sha256(body),
                        CHOICE_CANDIDATE_STATUS,
                        candidate.available_at.isoformat(),
                        candidate.fetched_at.isoformat(),
                        capture.raw_response,
                    ),
                )
                inserted += 1
            candidate_ids.append(candidate.candidate_id)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return ChoiceCandidateIngestResult(
        output_directory=database.parent,
        paths=MappingProxyType({"database": database}),
        counts=MappingProxyType(
            {
                "received": len(items),
                "inserted": inserted,
                "unchanged": len(items) - inserted,
                "formal_truth_written": 0,
            }
        ),
        candidate_ids=tuple(candidate_ids),
    )


__all__ = [
    "CHOICE_CANDIDATE_STATUS",
    "CHOICE_TRUTH_CANDIDATE_VERSION",
    "OFFICIAL_ADMISSION_STATUS",
    "OFFICIAL_TRUTH_RECEIPT_VERSION",
    "ChoiceCandidateIngestResult",
    "ChoiceTruthCandidate",
    "ChoiceTruthCandidateCapture",
    "OfficialReceiptIngestResult",
    "OfficialTruthAdapter",
    "OfficialTruthCapture",
    "OfficialTruthError",
    "OfficialTruthReceipt",
    "OfficialTruthReceiptError",
    "OfficialTruthStorageError",
    "get_official_truth_adapter",
    "ingest_choice_truth_candidates",
    "ingest_official_receipts",
    "install_truth_evidence_schema",
    "make_choice_truth_candidate",
]
