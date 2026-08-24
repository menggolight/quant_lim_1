"""Content-addressed Choice candidate evidence that is never formal truth.

This module deliberately sits outside :mod:`research.market_data.storage`.
Choice ``css``, ``sector`` and ``edb`` responses can help diagnose current
classification, requested-date membership and publication-date candidates,
but they do not prove an official first release or a point-in-time mapping.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import provider_access
from .contracts import aware_datetime, canonical_json_bytes, sha256_bytes
from .providers.baostock import normalize_a_share_stock_instrument
from .providers.base import (
    ProviderError,
    ProviderQueryError,
    classify_unexpected_error,
    safe_error_text,
)
from .providers.choice import ChoiceProvider


CONTRACT_VERSION = "choice-candidate-evidence-v1"
ADMISSION_STATUS = "diagnostic_current_only"
SUPPORTED_QUERY_TYPES = frozenset(
    {
        "sw2021_classification",
        "historical_sector_membership",
        "edb_publish_dates",
    }
)
SUPPORTED_STATUSES = frozenset(
    {"passed", "dependency_missing", "network_blocked", "not_configured", "failed"}
)
_EVIDENCE_FIELDS = frozenset(
    {
        "contract_version",
        "evidence_id",
        "provider_id",
        "adapter_version",
        "query_type",
        "request_fingerprint",
        "exact_request",
        "fetched_at",
        "status",
        "admission_status",
        "point_in_time_status",
        "formal_truth_eligible",
        "raw_content_sha256",
        "normalized_content_sha256",
        "record_count",
        "issues",
        "records",
    }
)
_CODE = re.compile(r"^(?:[036]\d{5}\.(?:SH|SZ))$")
_SECTOR_CODE = re.compile(r"^[A-Z0-9_]{3,40}$")
_EDB_ID = re.compile(r"^EM[A-Z][0-9]{8}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_CSS_INDICATOR = "SW2021"
_CSS_OPTIONS = "Ispandas=0,RowIndex=1,RECVtimeout=30"
_SECTOR_OPTIONS = "Ispandas=0,RowIndex=1,RECVtimeout=30"
_EDB_METADATA_INDICATORS = (
    "ID",
    "NAME",
    "UNIT",
    "SOURCE",
    "REGION",
    "FREQUENCY",
    "STARTDATE",
    "ENDDATE",
    "UPDATETIME",
)
_EDB_METADATA_OPTIONS = "Ispandas=0,RowIndex=1,RECVtimeout=30"
_EDB_OPTIONS = "IsPublishDate=1,RowIndex=1,Ispandas=0,RECVtimeout=30"


class ChoiceCandidateError(ValueError):
    """Raised when candidate evidence or its replay contract is malformed."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_json(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant: {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ChoiceCandidateError(f"{label} is not strict JSON: {exc}") from exc
    if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
        raise ChoiceCandidateError(f"{label} is not canonical JSON")
    return value


def _iso_date(value: Any, label: str) -> str:
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            pass
    raise ChoiceCandidateError(f"{label} must be a valid date")


def _canonical_instrument(value: str) -> str:
    text = str(value).strip().upper()
    if _CODE.fullmatch(text) is None:
        raise ChoiceCandidateError("instrument must be a canonical SH/SZ A-share code")
    try:
        normalized = normalize_a_share_stock_instrument(text)
    except ValueError as exc:
        raise ChoiceCandidateError(
            "instrument must be a canonical SH/SZ A-share code"
        ) from exc
    if normalized != text:
        raise ChoiceCandidateError("instrument must already be canonical")
    return text


def _canonical_sector_code(value: str) -> str:
    text = str(value).strip().upper()
    if _SECTOR_CODE.fullmatch(text) is None:
        raise ChoiceCandidateError("sector_code is not a supported Choice sector key")
    return text


def _canonical_edb_ids(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ChoiceCandidateError("edb_ids must be a sequence, not one string")
    result = tuple(str(item).strip().upper() for item in values)
    if not result or len(result) > 100:
        raise ChoiceCandidateError("edb_ids must contain between 1 and 100 IDs")
    if len(set(result)) != len(result):
        raise ChoiceCandidateError("edb_ids must be unique")
    if any(_EDB_ID.fullmatch(item) is None for item in result):
        raise ChoiceCandidateError("edb_ids contain an unsupported identifier")
    return result


def sw2021_request(instrument_id: str) -> dict[str, Any]:
    instrument = _canonical_instrument(instrument_id)
    return {
        "query_type": "sw2021_classification",
        "sdk_calls": [
            {
                "method": "css",
                "args": [instrument, _CSS_INDICATOR, _CSS_OPTIONS],
            }
        ],
    }


def sector_membership_request(sector_code: str, membership_date: date | str) -> dict[str, Any]:
    sector = _canonical_sector_code(sector_code)
    requested_date = _iso_date(membership_date, "membership_date")
    return {
        "query_type": "historical_sector_membership",
        "sdk_calls": [
            {
                "method": "sector",
                "args": [sector, requested_date, _SECTOR_OPTIONS],
            }
        ],
    }


def edb_publish_dates_request(edb_ids: Sequence[str]) -> dict[str, Any]:
    identifiers = _canonical_edb_ids(edb_ids)
    joined = ",".join(identifiers)
    return {
        "query_type": "edb_publish_dates",
        "sdk_calls": [
            {
                "method": "edbquery",
                "args": [
                    joined,
                    ",".join(_EDB_METADATA_INDICATORS),
                    _EDB_METADATA_OPTIONS,
                ],
            },
            {"method": "edb", "args": [joined, _EDB_OPTIONS]},
        ],
    }


def _validate_exact_request(query_type: str, request: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(request, Mapping) or set(request) != {"query_type", "sdk_calls"}:
        raise ChoiceCandidateError("exact_request fields differ from the candidate contract")
    if request.get("query_type") != query_type or query_type not in SUPPORTED_QUERY_TYPES:
        raise ChoiceCandidateError("exact_request query_type mismatch")
    calls = request.get("sdk_calls")
    if not isinstance(calls, list) or any(not isinstance(item, Mapping) for item in calls):
        raise ChoiceCandidateError("exact_request sdk_calls must be an array of objects")
    try:
        if query_type == "sw2021_classification":
            if len(calls) != 1 or set(calls[0]) != {"method", "args"}:
                raise ChoiceCandidateError("SW2021 requires exactly one css call")
            args = calls[0]["args"]
            if calls[0]["method"] != "css" or not isinstance(args, list) or len(args) != 3:
                raise ChoiceCandidateError("SW2021 css signature mismatch")
            expected = sw2021_request(str(args[0]))
        elif query_type == "historical_sector_membership":
            if len(calls) != 1 or set(calls[0]) != {"method", "args"}:
                raise ChoiceCandidateError("sector membership requires one sector call")
            args = calls[0]["args"]
            if calls[0]["method"] != "sector" or not isinstance(args, list) or len(args) != 3:
                raise ChoiceCandidateError("sector signature mismatch")
            expected = sector_membership_request(str(args[0]), str(args[1]))
        else:
            if len(calls) != 2 or any(set(item) != {"method", "args"} for item in calls):
                raise ChoiceCandidateError("EDB requires edbquery followed by edb")
            metadata_args = calls[0]["args"]
            data_args = calls[1]["args"]
            if (
                calls[0]["method"] != "edbquery"
                or calls[1]["method"] != "edb"
                or not isinstance(metadata_args, list)
                or len(metadata_args) != 3
                or not isinstance(data_args, list)
                or len(data_args) != 2
                or metadata_args[0] != data_args[0]
            ):
                raise ChoiceCandidateError("EDB SDK signatures mismatch")
            expected = edb_publish_dates_request(str(data_args[0]).split(","))
    except (KeyError, TypeError, IndexError) as exc:
        raise ChoiceCandidateError("exact_request SDK arguments are malformed") from exc
    canonical = json.loads(canonical_json_bytes(request).decode("utf-8"))
    if canonical != expected:
        raise ChoiceCandidateError("exact_request contains non-fixed SDK options or indicators")
    return canonical


def _json_safe_sdk(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ProviderQueryError("Choice candidate response contains a non-finite float")
        return format(value, ".15g")
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        if value.tzinfo is not None and value.utcoffset() is not None:
            return value.isoformat()
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            text = str(key)
            if text in result:
                raise ProviderQueryError("Choice candidate response has duplicate map keys")
            result[text] = _json_safe_sdk(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_json_safe_sdk(item) for item in value]
    raise ProviderQueryError(
        f"Choice candidate response contains unsupported {type(value).__name__}"
    )


def _sdk_response_payload(response: Any) -> dict[str, Any]:
    required = ("ErrorCode", "ErrorMsg", "Codes", "Indicators", "Dates", "Data")
    if any(not hasattr(response, field) for field in required):
        raise ProviderQueryError("Choice candidate response envelope is incomplete")
    return {
        "ErrorCode": str(getattr(response, "ErrorCode")).strip(),
        "ErrorMsg": safe_error_text(getattr(response, "ErrorMsg")),
        "Codes": _json_safe_sdk(getattr(response, "Codes")),
        "Indicators": _json_safe_sdk(getattr(response, "Indicators")),
        "Dates": _json_safe_sdk(getattr(response, "Dates")),
        "Data": _json_safe_sdk(getattr(response, "Data")),
    }


def _response_envelope(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "ErrorCode",
        "ErrorMsg",
        "Codes",
        "Indicators",
        "Dates",
        "Data",
    }:
        raise ChoiceCandidateError(f"{label} response fields drifted")
    if value.get("ErrorCode") != "0" or not isinstance(value.get("ErrorMsg"), str):
        raise ChoiceCandidateError(f"{label} raw response is not successful")
    for field in ("Codes", "Indicators", "Dates"):
        if not isinstance(value.get(field), list) or any(
            not isinstance(item, str) for item in value[field]
        ):
            raise ChoiceCandidateError(f"{label} {field} must be a string array")
    return dict(value)


def _normalize_sw2021(response: Mapping[str, Any], request: Mapping[str, Any], fetched_at: str) -> list[dict[str, Any]]:
    args = request["sdk_calls"][0]["args"]
    instrument = str(args[0])
    if response["Codes"] != [instrument] or [str(item).upper() for item in response["Indicators"]] != [_CSS_INDICATOR]:
        raise ChoiceCandidateError("Choice css returned the wrong code or SW2021 indicator")
    data = response.get("Data")
    if not isinstance(data, Mapping) or set(data) != {instrument}:
        raise ChoiceCandidateError("Choice css Data code map drifted")
    values = data[instrument]
    if (
        not isinstance(values, list)
        or len(values) != 1
        or values[0] is None
        or isinstance(values[0], (bool, list, tuple, dict, set))
    ):
        raise ChoiceCandidateError("Choice css SW2021 value shape drifted")
    classification = str(values[0]).strip()
    if not classification or classification.casefold() in {
        "none",
        "null",
        "nan",
        "n/a",
        "na",
        "--",
    }:
        raise ChoiceCandidateError("Choice css returned an empty SW2021 classification")
    return [
        {
            "instrument_id": instrument,
            "taxonomy": _CSS_INDICATOR,
            "classification_raw": classification,
            "observed_at": fetched_at,
            "availability_status": ADMISSION_STATUS,
            "formal_truth_eligible": False,
        }
    ]


def _normalize_sector(response: Mapping[str, Any], request: Mapping[str, Any], fetched_at: str) -> list[dict[str, Any]]:
    args = request["sdk_calls"][0]["args"]
    sector_code = str(args[0])
    membership_date = str(args[1])
    codes = [str(item).upper() for item in response["Codes"]]
    normalized = [_canonical_instrument(item) for item in codes]
    if not normalized or normalized != sorted(set(normalized)):
        raise ChoiceCandidateError("Choice sector members must be unique and ascending")
    indicators = [str(item).upper() for item in response["Indicators"]]
    if indicators != ["SECUCODE", "SECURITYSHORTNAME"]:
        raise ChoiceCandidateError("Choice sector indicator contract drifted")
    if response["Dates"] != [membership_date]:
        raise ChoiceCandidateError("Choice sector returned an unexpected date")
    values = response.get("Data")
    if (
        not isinstance(values, list)
        or len(values) != len(normalized) * len(indicators)
        or any(not isinstance(item, str) for item in values)
    ):
        raise ChoiceCandidateError("Choice sector flattened Data shape drifted")
    names: list[str] = []
    for index, instrument in enumerate(normalized):
        returned_code = str(values[index * 2]).strip().upper()
        name = str(values[index * 2 + 1]).strip()
        if returned_code != instrument or not name:
            raise ChoiceCandidateError("Choice sector Data rows disagree with Codes")
        names.append(name)
    return [
        {
            "sector_code": sector_code,
            "requested_membership_date": membership_date,
            "instrument_id": member,
            "security_short_name": names[index],
            "observed_at": fetched_at,
            "availability_status": ADMISSION_STATUS,
            "historical_pit_proven": False,
            "formal_truth_eligible": False,
        }
        for index, member in enumerate(normalized)
    ]


def _metadata_rows(response: Mapping[str, Any], identifiers: list[str]) -> dict[str, dict[str, str]]:
    indicators = [str(item).upper() for item in response["Indicators"]]
    if indicators != list(_EDB_METADATA_INDICATORS):
        raise ChoiceCandidateError("Choice edbquery returned the wrong metadata indicators")
    codes = [str(item).upper() for item in response["Codes"]]
    if codes != identifiers:
        raise ChoiceCandidateError("Choice edbquery returned different EDB IDs")
    data = response.get("Data")
    if not isinstance(data, Mapping) or set(str(key).upper() for key in data) != set(identifiers):
        raise ChoiceCandidateError("Choice edbquery Data code map drifted")
    result: dict[str, dict[str, str]] = {}
    for identifier in identifiers:
        values = data.get(identifier)
        if not isinstance(values, list):
            raise ChoiceCandidateError("Choice edbquery metadata row must be an array")
        if len(values) == 1 and isinstance(values[0], list):
            values = values[0]
        elif len(values) == len(indicators) and all(
            isinstance(item, list) and len(item) == 1 for item in values
        ):
            values = [item[0] for item in values]
        if len(values) != len(indicators) or any(isinstance(item, (list, dict)) for item in values):
            raise ChoiceCandidateError("Choice edbquery metadata width drifted")
        row = {indicator.lower(): str(value).strip() for indicator, value in zip(indicators, values)}
        if row["id"].upper() != identifier:
            raise ChoiceCandidateError("Choice edbquery metadata ID disagrees with its key")
        result[identifier] = row
    return result


def _normalize_edb(
    metadata_response: Mapping[str, Any],
    data_response: Mapping[str, Any],
    request: Mapping[str, Any],
    fetched_at: str,
) -> list[dict[str, Any]]:
    identifiers = str(request["sdk_calls"][1]["args"][0]).split(",")
    metadata = _metadata_rows(metadata_response, identifiers)
    codes = [str(item).upper() for item in data_response["Codes"]]
    if codes != identifiers:
        raise ChoiceCandidateError("Choice edb returned different EDB IDs")
    indicators = [str(item).upper() for item in data_response["Indicators"]]
    if (
        len(indicators) < 2
        or len(set(indicators)) != len(indicators)
        or "PUBLISHDATE" not in indicators
    ):
        raise ChoiceCandidateError("Choice edb must return PUBLISHDATE and a value indicator")
    dates = [_iso_date(item, "Choice edb observation date") for item in data_response["Dates"]]
    if not dates or dates not in (sorted(set(dates)), sorted(set(dates), reverse=True)):
        raise ChoiceCandidateError(
            "Choice edb dates must be non-empty, unique and monotonic"
        )
    data = data_response.get("Data")
    if not isinstance(data, Mapping) or set(str(key).upper() for key in data) != set(identifiers):
        raise ChoiceCandidateError("Choice edb Data code map drifted")
    records: list[dict[str, Any]] = []
    publish_index = indicators.index("PUBLISHDATE")
    for identifier in identifiers:
        columns = data.get(identifier)
        if not isinstance(columns, list) or len(columns) != len(indicators):
            raise ChoiceCandidateError("Choice edb indicator column width drifted")
        if any(not isinstance(column, list) or len(column) != len(dates) for column in columns):
            raise ChoiceCandidateError("Choice edb date column width drifted")
        for index, observation_date in enumerate(dates):
            raw_publish = columns[publish_index][index]
            publish_date = (
                None
                if raw_publish is None or str(raw_publish).strip() == ""
                else _iso_date(raw_publish, "Choice edb publish date")
            )
            values = {
                indicator.lower(): str(columns[column_index][index]).strip()
                for column_index, indicator in enumerate(indicators)
                if indicator != "PUBLISHDATE"
            }
            records.append(
                {
                    "edb_id": identifier,
                    "observation_date": observation_date,
                    "publish_date": publish_date,
                    "values": values,
                    "metadata": metadata[identifier],
                    "observed_at": fetched_at,
                    "availability_status": ADMISSION_STATUS,
                    "first_release_proven": False,
                    "formal_truth_eligible": False,
                }
            )
    return records


def replay_candidate_raw(
    query_type: str,
    exact_request: Mapping[str, Any],
    raw_content: bytes,
) -> tuple[Mapping[str, Any], ...]:
    """Strictly replay one Choice candidate raw capture."""

    request = _validate_exact_request(query_type, exact_request)
    payload = _strict_json(raw_content, "Choice candidate raw evidence")
    if payload.get("contract_version") != CONTRACT_VERSION or payload.get("query_type") != query_type:
        raise ChoiceCandidateError("Choice candidate raw envelope version/type mismatch")
    if payload.get("exact_request") != request:
        raise ChoiceCandidateError("Choice candidate raw request mismatch")
    if set(payload) == {
        "contract_version",
        "query_type",
        "exact_request",
        "fetched_at",
        "failure",
    }:
        aware_datetime(payload.get("fetched_at"), "fetched_at")
        failure = payload.get("failure")
        if (
            not isinstance(failure, Mapping)
            or set(failure) != {"status", "code", "error_type", "message", "rejected_response"}
            or failure.get("status") not in SUPPORTED_STATUSES - {"passed"}
            or not all(isinstance(failure.get(field), str) for field in ("code", "error_type", "message"))
            or failure.get("rejected_response") is not None
            and not isinstance(failure.get("rejected_response"), Mapping)
        ):
            raise ChoiceCandidateError("Choice candidate failure envelope is malformed")
        return ()
    if set(payload) != {
        "contract_version",
        "query_type",
        "exact_request",
        "fetched_at",
        "responses",
    }:
        raise ChoiceCandidateError("Choice candidate success envelope is malformed")
    fetched_at = aware_datetime(payload.get("fetched_at"), "fetched_at").isoformat()
    responses = payload.get("responses")
    if not isinstance(responses, Mapping):
        raise ChoiceCandidateError("Choice candidate responses must be an object")
    if query_type == "sw2021_classification":
        if set(responses) != {"css"}:
            raise ChoiceCandidateError("SW2021 response must contain css only")
        records = _normalize_sw2021(_response_envelope(responses["css"], "css"), request, fetched_at)
    elif query_type == "historical_sector_membership":
        if set(responses) != {"sector"}:
            raise ChoiceCandidateError("sector response must contain sector only")
        records = _normalize_sector(_response_envelope(responses["sector"], "sector"), request, fetched_at)
    else:
        if set(responses) != {"edbquery", "edb"}:
            raise ChoiceCandidateError("EDB response must contain edbquery and edb")
        records = _normalize_edb(
            _response_envelope(responses["edbquery"], "edbquery"),
            _response_envelope(responses["edb"], "edb"),
            request,
            fetched_at,
        )
    if not records:
        raise ChoiceCandidateError("Choice candidate response normalized to no records")
    return tuple(records)


@dataclass(frozen=True)
class ChoiceCandidateEvidence:
    evidence_id: str
    provider_id: str
    adapter_version: str
    query_type: str
    request_fingerprint: str
    exact_request: Mapping[str, Any]
    fetched_at: datetime
    status: str
    admission_status: str
    point_in_time_status: str
    formal_truth_eligible: bool
    raw_content_sha256: str
    normalized_content_sha256: str
    record_count: int
    issues: tuple[Mapping[str, Any], ...]
    records: tuple[Mapping[str, Any], ...]
    contract_version: str = CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != CONTRACT_VERSION:
            raise ChoiceCandidateError("unsupported candidate contract_version")
        if self.provider_id != ChoiceProvider.provider_id:
            raise ChoiceCandidateError("candidate provider_id must be choice")
        if self.adapter_version != ChoiceProvider.adapter_version:
            raise ChoiceCandidateError("candidate adapter_version mismatch")
        request = _validate_exact_request(self.query_type, self.exact_request)
        object.__setattr__(self, "exact_request", request)
        expected_request_hash = sha256_bytes(canonical_json_bytes(request))
        if self.request_fingerprint != expected_request_hash:
            raise ChoiceCandidateError("candidate request_fingerprint mismatch")
        object.__setattr__(self, "fetched_at", aware_datetime(self.fetched_at, "fetched_at"))
        if self.status not in SUPPORTED_STATUSES:
            raise ChoiceCandidateError("unsupported candidate status")
        if self.admission_status != ADMISSION_STATUS or self.point_in_time_status != ADMISSION_STATUS:
            raise ChoiceCandidateError("Choice candidates must remain diagnostic_current_only")
        if self.formal_truth_eligible is not False:
            raise ChoiceCandidateError("Choice candidate can never be formal truth eligible")
        if _SHA256.fullmatch(self.raw_content_sha256) is None or _SHA256.fullmatch(self.normalized_content_sha256) is None:
            raise ChoiceCandidateError("candidate content hashes must be lowercase SHA-256")
        if type(self.record_count) is not int or self.record_count != len(self.records):
            raise ChoiceCandidateError("candidate record_count mismatch")
        if self.status == "passed" and self.record_count == 0:
            raise ChoiceCandidateError("passed candidate evidence must contain records")
        if self.status != "passed" and self.record_count != 0:
            raise ChoiceCandidateError("failed candidate evidence cannot contain records")
        if any(not isinstance(item, Mapping) for item in self.records + self.issues):
            raise ChoiceCandidateError("candidate records/issues must be objects")
        identity = self.to_dict(include_evidence_id=False)
        if self.evidence_id != sha256_bytes(canonical_json_bytes(identity)):
            raise ChoiceCandidateError("candidate evidence_id mismatch")

    def to_dict(self, *, include_evidence_id: bool = True) -> dict[str, Any]:
        result = {
            "contract_version": self.contract_version,
            "provider_id": self.provider_id,
            "adapter_version": self.adapter_version,
            "query_type": self.query_type,
            "request_fingerprint": self.request_fingerprint,
            "exact_request": json.loads(canonical_json_bytes(self.exact_request).decode("utf-8")),
            "fetched_at": self.fetched_at.isoformat(),
            "status": self.status,
            "admission_status": self.admission_status,
            "point_in_time_status": self.point_in_time_status,
            "formal_truth_eligible": self.formal_truth_eligible,
            "raw_content_sha256": self.raw_content_sha256,
            "normalized_content_sha256": self.normalized_content_sha256,
            "record_count": self.record_count,
            "issues": [dict(item) for item in self.issues],
            "records": [dict(item) for item in self.records],
        }
        if include_evidence_id:
            result["evidence_id"] = self.evidence_id
        return result

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ChoiceCandidateEvidence":
        if set(payload) != _EVIDENCE_FIELDS:
            raise ChoiceCandidateError("candidate evidence fields differ from the contract")
        issues = payload.get("issues")
        records = payload.get("records")
        if not isinstance(issues, list) or not isinstance(records, list):
            raise ChoiceCandidateError("candidate issues/records must be arrays")
        return cls(
            contract_version=str(payload.get("contract_version") or ""),
            evidence_id=str(payload.get("evidence_id") or ""),
            provider_id=str(payload.get("provider_id") or ""),
            adapter_version=str(payload.get("adapter_version") or ""),
            query_type=str(payload.get("query_type") or ""),
            request_fingerprint=str(payload.get("request_fingerprint") or ""),
            exact_request=payload.get("exact_request"),  # type: ignore[arg-type]
            fetched_at=aware_datetime(payload.get("fetched_at"), "fetched_at"),  # type: ignore[arg-type]
            status=str(payload.get("status") or ""),
            admission_status=str(payload.get("admission_status") or ""),
            point_in_time_status=str(payload.get("point_in_time_status") or ""),
            formal_truth_eligible=payload.get("formal_truth_eligible"),  # type: ignore[arg-type]
            raw_content_sha256=str(payload.get("raw_content_sha256") or ""),
            normalized_content_sha256=str(payload.get("normalized_content_sha256") or ""),
            record_count=payload.get("record_count"),  # type: ignore[arg-type]
            issues=tuple(issues),
            records=tuple(records),
        )


def _make_evidence(
    *,
    query_type: str,
    exact_request: Mapping[str, Any],
    fetched_at: datetime,
    status: str,
    raw_content: bytes,
    records: Sequence[Mapping[str, Any]],
    issues: Sequence[Mapping[str, Any]],
) -> ChoiceCandidateEvidence:
    request = _validate_exact_request(query_type, exact_request)
    normalized = canonical_json_bytes([dict(item) for item in records])
    fields: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "provider_id": ChoiceProvider.provider_id,
        "adapter_version": ChoiceProvider.adapter_version,
        "query_type": query_type,
        "request_fingerprint": sha256_bytes(canonical_json_bytes(request)),
        "exact_request": request,
        "fetched_at": aware_datetime(fetched_at, "fetched_at").isoformat(),
        "status": status,
        "admission_status": ADMISSION_STATUS,
        "point_in_time_status": ADMISSION_STATUS,
        "formal_truth_eligible": False,
        "raw_content_sha256": sha256_bytes(raw_content),
        "normalized_content_sha256": sha256_bytes(normalized),
        "record_count": len(records),
        "issues": [dict(item) for item in issues],
        "records": [dict(item) for item in records],
    }
    evidence_id = sha256_bytes(canonical_json_bytes(fields))
    return ChoiceCandidateEvidence.from_dict({**fields, "evidence_id": evidence_id})


class ChoiceCandidateStorage:
    """Persist and replay only content-addressed, non-admitted Choice evidence."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() != content:
                raise ChoiceCandidateError("content-addressed candidate path collision")
            return
        descriptor, temporary = tempfile.mkstemp(prefix=".candidate-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def raw_path(self, raw_sha256: str) -> Path:
        return self.root / "raw" / "choice" / f"{raw_sha256}.raw"

    def evidence_path(self, evidence: ChoiceCandidateEvidence) -> Path:
        return (
            self.root
            / "evidence"
            / "choice"
            / evidence.query_type
            / evidence.request_fingerprint
            / f"{evidence.evidence_id}.json"
        )

    def persist(self, evidence: ChoiceCandidateEvidence, raw_content: bytes) -> Path:
        records = replay_candidate_raw(evidence.query_type, evidence.exact_request, raw_content)
        if sha256_bytes(raw_content) != evidence.raw_content_sha256:
            raise ChoiceCandidateError("candidate raw hash mismatch")
        if canonical_json_bytes([dict(item) for item in records]) != canonical_json_bytes(
            [dict(item) for item in evidence.records]
        ):
            raise ChoiceCandidateError("candidate normalized replay mismatch")
        if sha256_bytes(canonical_json_bytes([dict(item) for item in records])) != evidence.normalized_content_sha256:
            raise ChoiceCandidateError("candidate normalized hash mismatch")
        path = self.evidence_path(evidence)
        self._atomic_write(self.raw_path(evidence.raw_content_sha256), raw_content)
        self._atomic_write(path, canonical_json_bytes(evidence.to_dict()))
        return path

    def read(self, path: Path | str) -> tuple[ChoiceCandidateEvidence, bytes]:
        resolved = Path(path).resolve()
        root = (self.root / "evidence" / "choice").resolve()
        try:
            relative = resolved.relative_to(root)
        except ValueError as exc:
            raise ChoiceCandidateError("candidate replay must read from its evidence root") from exc
        raw = resolved.read_bytes()
        payload = _strict_json(raw, "Choice candidate evidence")
        evidence = ChoiceCandidateEvidence.from_dict(payload)
        expected = Path(
            evidence.query_type,
            evidence.request_fingerprint,
            f"{evidence.evidence_id}.json",
        )
        if relative != expected:
            raise ChoiceCandidateError("candidate evidence path metadata mismatch")
        raw_path = self.raw_path(evidence.raw_content_sha256)
        if not raw_path.is_file():
            raise ChoiceCandidateError("candidate raw evidence is missing")
        raw_content = raw_path.read_bytes()
        replayed = replay_candidate_raw(evidence.query_type, evidence.exact_request, raw_content)
        if sha256_bytes(raw_content) != evidence.raw_content_sha256:
            raise ChoiceCandidateError("candidate raw evidence hash mismatch")
        normalized = canonical_json_bytes([dict(item) for item in replayed])
        if sha256_bytes(normalized) != evidence.normalized_content_sha256:
            raise ChoiceCandidateError("candidate normalized evidence hash mismatch")
        if normalized != canonical_json_bytes([dict(item) for item in evidence.records]):
            raise ChoiceCandidateError("candidate records differ from raw replay")
        return evidence, raw_content

    def load_latest(
        self, query_type: str, exact_request: Mapping[str, Any]
    ) -> tuple[ChoiceCandidateEvidence, bytes]:
        request = _validate_exact_request(query_type, exact_request)
        fingerprint = sha256_bytes(canonical_json_bytes(request))
        directory = self.root / "evidence" / "choice" / query_type / fingerprint
        candidates: list[tuple[ChoiceCandidateEvidence, bytes]] = []
        for path in directory.glob("*.json") if directory.is_dir() else ():
            candidates.append(self.read(path))
        if not candidates:
            raise ChoiceCandidateError("offline candidate replay cache miss")
        return max(candidates, key=lambda item: (item[0].fetched_at, item[0].evidence_id))


class ChoiceCandidateService:
    """Three explicit Choice candidate APIs; there is no arbitrary dispatcher."""

    def __init__(
        self,
        storage: ChoiceCandidateStorage | Path | str,
        *,
        provider: ChoiceProvider | None = None,
    ) -> None:
        self.storage = (
            storage if isinstance(storage, ChoiceCandidateStorage) else ChoiceCandidateStorage(storage)
        )
        self.provider = provider or ChoiceProvider()

    def fetch_sw2021_classification(self, instrument_id: str) -> ChoiceCandidateEvidence:
        request = sw2021_request(instrument_id)
        return self._capture("sw2021_classification", request)

    def fetch_historical_sector_membership(
        self, sector_code: str, membership_date: date | str
    ) -> ChoiceCandidateEvidence:
        request = sector_membership_request(sector_code, membership_date)
        return self._capture("historical_sector_membership", request)

    def fetch_edb_publish_dates(
        self,
        edb_ids: Sequence[str],
    ) -> ChoiceCandidateEvidence:
        request = edb_publish_dates_request(edb_ids)

        return self._capture("edb_publish_dates", request)

    def replay_sw2021_classification(self, instrument_id: str) -> ChoiceCandidateEvidence:
        return self.storage.load_latest(
            "sw2021_classification", sw2021_request(instrument_id)
        )[0]

    def replay_historical_sector_membership(
        self, sector_code: str, membership_date: date | str
    ) -> ChoiceCandidateEvidence:
        return self.storage.load_latest(
            "historical_sector_membership",
            sector_membership_request(sector_code, membership_date),
        )[0]

    def replay_edb_publish_dates(
        self,
        edb_ids: Sequence[str],
    ) -> ChoiceCandidateEvidence:
        return self.storage.load_latest(
            "edb_publish_dates",
            edb_publish_dates_request(edb_ids),
        )[0]

    def _call_response(self, client: Any, operation: str, *args: Any) -> dict[str, Any]:
        # Every operation name is a literal owned by one of the three public methods.
        response = self.provider._sdk_call(client, operation, *args)
        self.provider._raise_sdk_error(response, operation)
        return _sdk_response_payload(response)

    def _capture(
        self,
        query_type: str,
        exact_request: Mapping[str, Any],
    ) -> ChoiceCandidateEvidence:
        provider_access.require_choice_network_access(
            f"candidate_diagnostic_fetch_{query_type}"
        )
        request = _validate_exact_request(query_type, exact_request)
        client: Any = None
        session_attempted = False
        rejected_response: Mapping[str, Any] | None = None
        responses: dict[str, Any] = {}
        fetched_at: datetime | None = None
        try:
            sdk = self.provider._load_sdk()
            client = getattr(sdk, "c", None)
            if client is None:
                raise ProviderQueryError("Choice SDK does not expose its client contract")
            session_attempted = True
            login = self.provider._sdk_call(
                client,
                "start",
                self.provider._LOGIN_OPTIONS,
                self.provider._quiet_log,
            )
            self.provider._raise_sdk_error(login, "start")
            # The validated request contract fixes both the methods and every
            # argument.  This loop is not a caller-controlled SDK dispatcher.
            for call in request["sdk_calls"]:
                method = str(call["method"])
                responses[method] = self._call_response(
                    client, method, *call["args"]
                )
            fetched_at = self.provider._aware_clock()
            raw_content = canonical_json_bytes(
                {
                    "contract_version": CONTRACT_VERSION,
                    "query_type": query_type,
                    "exact_request": request,
                    "fetched_at": fetched_at.isoformat(),
                    "responses": responses,
                }
            )
            rejected_response = _strict_json(raw_content, "candidate response")
            records = replay_candidate_raw(query_type, request, raw_content)
            stopped = self.provider._sdk_call(client, "stop")
            session_attempted = False
            self.provider._raise_sdk_error(stopped, "stop")
            evidence = _make_evidence(
                query_type=query_type,
                exact_request=request,
                fetched_at=fetched_at,
                status="passed",
                raw_content=raw_content,
                records=records,
                issues=(
                    {
                        "code": "choice_candidate_not_formal_truth",
                        "severity": "warning",
                        "message": (
                            "Choice aggregation is diagnostic_current_only and does not "
                            "prove official first release or point-in-time availability"
                        ),
                    },
                ),
            )
            self.storage.persist(evidence, raw_content)
            return evidence
        except Exception as exc:
            if session_attempted and client is not None:
                try:
                    self.provider._sdk_call(client, "stop")
                except Exception:
                    pass
            error = classify_unexpected_error(exc)
            if rejected_response is None and responses:
                rejected_response = {
                    "contract_version": CONTRACT_VERSION,
                    "query_type": query_type,
                    "exact_request": request,
                    "responses": responses,
                }
            if (
                rejected_response is None
                and isinstance(exc, ProviderError)
                and exc.raw_content
            ):
                try:
                    rejected_response = {
                        "provider_error": _strict_json(
                            exc.raw_content, "Choice provider error response"
                        )
                    }
                except ChoiceCandidateError:
                    rejected_response = {
                        "provider_error_raw_sha256": sha256_bytes(exc.raw_content)
                    }
            if fetched_at is None:
                try:
                    fetched_at = self.provider._aware_clock()
                except Exception:
                    fetched_at = datetime.now().astimezone()
            failure_raw = canonical_json_bytes(
                {
                    "contract_version": CONTRACT_VERSION,
                    "query_type": query_type,
                    "exact_request": request,
                    "fetched_at": fetched_at.isoformat(),
                    "failure": {
                        "status": error.status,
                        "code": error.code,
                        "error_type": type(exc).__name__,
                        "message": safe_error_text(error),
                        "rejected_response": rejected_response,
                    },
                }
            )
            evidence = _make_evidence(
                query_type=query_type,
                exact_request=request,
                fetched_at=fetched_at,
                status=error.status,
                raw_content=failure_raw,
                records=(),
                issues=(
                    {
                        "code": error.code,
                        "severity": "error",
                        "message": safe_error_text(error),
                    },
                ),
            )
            self.storage.persist(evidence, failure_raw)
            return evidence


__all__ = [
    "ADMISSION_STATUS",
    "CONTRACT_VERSION",
    "ChoiceCandidateError",
    "ChoiceCandidateEvidence",
    "ChoiceCandidateService",
    "ChoiceCandidateStorage",
    "edb_publish_dates_request",
    "replay_candidate_raw",
    "sector_membership_request",
    "sw2021_request",
]
