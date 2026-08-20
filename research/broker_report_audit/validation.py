"""Evidence-backed manual validation gates for report extraction.

The production gate is computed from immutable row-level judgements.  A user
cannot unlock formal scoring by editing a summary boolean in the V1 config.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import ensure_aware, parse_datetime


VALIDATION_CONTRACT_VERSION = "broker-report-extractor-validation.v3"
VALID_DIMENSIONS = ("macro", "industry", "stock")
METADATA_FIELDS = ("broker", "title", "date", "subject")
EXTRACTION_FIELDS = ("variable", "direction", "value", "horizon")
EVIDENCE_PROVENANCE_FIELDS = (
    "extractor_version",
    "evidence_source_kind",
    "evidence_source_hash",
    "evidence_parser_version",
    "evidence_prompt_version",
    "extractor_bundle_sha256",
)


class ValidationManifestError(ValueError):
    """Raised when manual validation evidence is incomplete or unauditable."""


def _get(record: Any, name: str, default: Any = "") -> Any:
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _binding_time(value: Any) -> str:
    if isinstance(value, datetime):
        ensure_aware(value, "available_at")
        return value.isoformat()
    return str(value or "").strip()


def claim_validation_payload_sha256(claim: Any) -> str:
    """Bind manual review to every field that can affect scoring or timing."""

    fields = (
        "claim_id",
        "report_id",
        "dimension",
        "subject_id",
        "target_type",
        "direction",
        "value_min",
        "value_max",
        "unit",
        "benchmark",
        "forecast_period",
        "horizon_days",
        "available_at",
        "evidence_span",
        "extractor_version",
        "extraction_confidence",
        "evidence_source_kind",
        "evidence_source_hash",
        "evidence_parser_version",
        "evidence_prompt_version",
        "extractor_bundle_sha256",
    )
    payload = {field: str(_get(claim, field, "")) for field in fields}
    return _sha256_bytes(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def population_snapshot(
    reports: Iterable[Any],
) -> tuple[str, dict[tuple[str, str], dict[str, str]]]:
    """Bind the full report population without requiring every report PDF.

    Only the deterministic manual-review sample is allowed to require PDF
    bytes.  Requiring ``pdf_sha256`` for the entire public listing population
    would force tens of thousands of downloads and contradict the bounded
    90-report validation contract.  The population identity therefore uses
    stable source-record fields; the returned lookup retains an optional PDF
    hash so :func:`load_validation_manifest` can require it for each selected
    sample.
    """

    records: dict[tuple[str, str], dict[str, str]] = {}
    for report in reports:
        dimension = str(_get(report, "dimension")).lower().strip()
        report_id = str(_get(report, "report_id")).strip()
        if dimension not in VALID_DIMENSIONS or not report_id:
            raise ValidationManifestError("population report has invalid dimension/report_id")
        key = (dimension, report_id)
        if key in records:
            raise ValidationManifestError(f"duplicate population report: {dimension}/{report_id}")
        source_record_hash = str(_get(report, "content_hash")).lower().strip()
        if not re.fullmatch(r"[0-9a-f]{64}", source_record_hash):
            raise ValidationManifestError(
                f"population report {report_id} lacks source record hash"
            )
        pdf_document_hash = str(_get(report, "pdf_sha256")).lower().strip()
        if pdf_document_hash and not re.fullmatch(r"[0-9a-f]{64}", pdf_document_hash):
            raise ValidationManifestError(
                f"population report {report_id} has an invalid PDF document hash"
            )
        available_at_value = _get(report, "available_at")
        if isinstance(available_at_value, datetime):
            ensure_aware(available_at_value, "available_at")
            available_at = available_at_value.isoformat()
        else:
            available_at = str(available_at_value).strip()
        records[key] = {
            "dimension": dimension,
            "report_id": report_id,
            "source_record_hash": source_record_hash,
            "pdf_document_hash": pdf_document_hash,
            "source_url": str(_get(report, "source_url")).strip(),
            "pdf_url": str(_get(report, "pdf_url")).strip(),
            "available_at": available_at,
        }
    canonical = sorted(
        (
            {
                "dimension": item["dimension"],
                "report_id": item["report_id"],
                "source_record_hash": item["source_record_hash"],
                "source_url": item["source_url"],
                "available_at": item["available_at"],
            }
            for item in records.values()
        ),
        key=lambda item: (item["dimension"], item["report_id"]),
    )
    body = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _sha256_bytes(body), records


def deterministic_sample_ids(
    population: Mapping[tuple[str, str], Mapping[str, str]],
    *,
    sample_seed: str,
    count_per_dimension: int,
) -> dict[str, tuple[str, ...]]:
    selected: dict[str, tuple[str, ...]] = {}
    for dimension in VALID_DIMENSIONS:
        candidates = [
            report_id
            for candidate_dimension, report_id in population
            if candidate_dimension == dimension
        ]
        candidates.sort(
            key=lambda report_id: (
                hashlib.sha256(
                    f"{sample_seed}|{dimension}|{report_id}".encode("utf-8")
                ).hexdigest(),
                report_id,
            )
        )
        selected[dimension] = tuple(candidates[: int(count_per_dimension)])
    return selected


def validate_claim_evidence_bindings(
    claims: Iterable[Any],
    reports: Iterable[Any],
    *,
    expected_extractor_version: str,
    expected_extractor_bundle_sha256: str,
    expected_parser_version: str,
    expected_prompt_version: str,
) -> int:
    """Verify every claim against the exact current report/PDF source hash.

    Migrated pre-contract claims deliberately fail this gate.  They remain
    readable for diagnostics, but callers must re-extract them before using
    their outcomes for skill estimation or factor admission.
    """

    expected_versions = {
        "extractor": str(expected_extractor_version or "").strip(),
        "parser": str(expected_parser_version or "").strip(),
        "prompt": str(expected_prompt_version or "").strip(),
    }
    if not all(expected_versions.values()):
        raise ValidationManifestError(
            "expected extractor/parser/prompt versions are required"
        )
    if not re.fullmatch(
        r"[0-9a-f]{64}", str(expected_extractor_bundle_sha256 or "").lower()
    ):
        raise ValidationManifestError("expected extractor bundle hash is invalid")
    report_by_id: dict[str, Any] = {}
    for report in reports:
        report_id = str(_get(report, "report_id")).strip()
        if not report_id or report_id in report_by_id:
            raise ValidationManifestError(
                "claim evidence population has missing or duplicate report_id"
            )
        report_by_id[report_id] = report
    checked = 0
    for claim in claims:
        claim_id = str(_get(claim, "claim_id")).strip()
        report_id = str(_get(claim, "report_id")).strip()
        kind = str(_get(claim, "evidence_source_kind")).strip()
        evidence_hash = str(_get(claim, "evidence_source_hash")).lower().strip()
        extractor_version = str(_get(claim, "extractor_version")).strip()
        parser_version = str(_get(claim, "evidence_parser_version")).strip()
        prompt_version = str(_get(claim, "evidence_prompt_version")).strip()
        bundle_hash = str(
            _get(claim, "extractor_bundle_sha256") or ""
        ).strip().lower()
        if not claim_id or not report_id:
            raise ValidationManifestError("claim evidence has missing claim_id/report_id")
        report = report_by_id.get(report_id)
        if report is None:
            raise ValidationManifestError(
                f"claim {claim_id} references a report outside the current population"
            )
        if not re.fullmatch(r"[0-9a-f]{64}", evidence_hash):
            raise ValidationManifestError(
                f"claim {claim_id} has unverified legacy evidence"
            )
        if not parser_version or not prompt_version:
            raise ValidationManifestError(
                f"claim {claim_id} has unverified parser/prompt versions"
            )
        if extractor_version != expected_versions["extractor"]:
            raise ValidationManifestError(
                f"claim {claim_id} extractor version does not match validation"
            )
        if bundle_hash != str(expected_extractor_bundle_sha256).strip().lower():
            raise ValidationManifestError(
                f"claim {claim_id} extractor bundle does not match validation"
            )
        if kind == "structured/source_record":
            expected_hash = str(_get(report, "content_hash")).lower().strip()
            if parser_version != "source-record-v1" or prompt_version != "none":
                raise ValidationManifestError(
                    f"claim {claim_id} structured evidence version mismatch"
                )
        elif kind == "textual/pdf":
            expected_hash = str(_get(report, "pdf_sha256")).lower().strip()
            if (
                parser_version != expected_versions["parser"]
                or prompt_version != expected_versions["prompt"]
            ):
                raise ValidationManifestError(
                    f"claim {claim_id} PDF parser/prompt version does not match validation"
                )
        else:
            raise ValidationManifestError(
                f"claim {claim_id} has unsupported evidence source kind"
            )
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise ValidationManifestError(
                f"claim {claim_id} current evidence source has no verified hash"
            )
        if evidence_hash != expected_hash:
            raise ValidationManifestError(
                f"claim {claim_id} evidence hash does not match current report source"
            )
        if str(_get(claim, "dimension")).strip() != str(
            _get(report, "dimension")
        ).strip():
            raise ValidationManifestError(
                f"claim {claim_id} dimension does not match its report"
            )
        if str(_get(claim, "subject_id")).strip() != str(
            _get(report, "subject_id")
        ).strip():
            raise ValidationManifestError(
                f"claim {claim_id} subject_id does not match its report"
            )
        if _binding_time(_get(claim, "available_at")) != _binding_time(
            _get(report, "available_at")
        ):
            raise ValidationManifestError(
                f"claim {claim_id} available_at does not match its report"
            )
        checked += 1
    return checked


def load_validation_manifest(
    path: str | Path,
    *,
    expected_sha256: str,
    as_of: datetime,
    population_reports: Iterable[Any],
    population_claims: Iterable[Any],
    expected_extractor_version: str,
    expected_extractor_bundle_sha256: str,
    expected_parser_version: str,
    expected_prompt_version: str,
    minimum_samples_per_dimension: int = 30,
    minimum_field_precision: float = 0.95,
) -> dict[str, dict[str, Any]]:
    """Verify evidence and recompute each dimension's gate from row-level checks."""

    ensure_aware(as_of, "as_of")
    report_rows = tuple(population_reports)
    claim_rows = tuple(population_claims)
    resolved = Path(path)
    if not resolved.is_file():
        raise ValidationManifestError(f"validation manifest does not exist: {resolved}")
    body = resolved.read_bytes()
    actual_hash = _sha256_bytes(body)
    expected = str(expected_sha256 or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or actual_hash != expected:
        raise ValidationManifestError("validation manifest SHA-256 mismatch")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationManifestError("validation manifest must be UTF-8 JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValidationManifestError("validation manifest root must be an object")
    if payload.get("contract_version") != VALIDATION_CONTRACT_VERSION:
        raise ValidationManifestError("validation manifest contract_version mismatch")
    expected_versions = {
        "extractor_version": str(expected_extractor_version or "").strip(),
        "extractor_bundle_sha256": str(
            expected_extractor_bundle_sha256 or ""
        ).strip().lower(),
        "parser_version": str(expected_parser_version or "").strip(),
        "prompt_version": str(expected_prompt_version or "").strip(),
    }
    if not all(expected_versions.values()):
        raise ValidationManifestError("expected extractor/parser/prompt versions are required")
    if not re.fullmatch(
        r"[0-9a-f]{64}", expected_versions["extractor_bundle_sha256"]
    ):
        raise ValidationManifestError("expected extractor bundle hash is invalid")
    for field, expected_value in expected_versions.items():
        if str(payload.get(field) or "") != expected_value:
            raise ValidationManifestError(f"validation manifest {field} mismatch")
    if not str(payload.get("sample_seed") or "").strip():
        raise ValidationManifestError("sample_seed is required")
    population_hash, population = population_snapshot(report_rows)
    if str(payload.get("population_snapshot_hash") or "").lower() != population_hash:
        raise ValidationManifestError("population_snapshot_hash does not match current sample population")
    if not str(payload.get("reviewer") or "").strip():
        raise ValidationManifestError("reviewer is required")
    reviewed_at = parse_datetime(payload.get("reviewed_at"))
    if reviewed_at > as_of:
        raise ValidationManifestError("reviewed_at is after research cutoff")
    samples = payload.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValidationManifestError("samples must be a non-empty list")

    expected_samples = deterministic_sample_ids(
        population,
        sample_seed=str(payload["sample_seed"]),
        count_per_dimension=minimum_samples_per_dimension,
    )
    sampled_report_ids = {
        report_id
        for report_ids in expected_samples.values()
        for report_id in report_ids
    }
    sampled_reports = tuple(
        report
        for report in report_rows
        if str(_get(report, "report_id")).strip() in sampled_report_ids
    )
    sampled_claims = tuple(
        claim
        for claim in claim_rows
        if str(_get(claim, "report_id")).strip() in sampled_report_ids
    )
    validate_claim_evidence_bindings(
        sampled_claims,
        sampled_reports,
        expected_extractor_version=expected_versions["extractor_version"],
        expected_extractor_bundle_sha256=expected_versions[
            "extractor_bundle_sha256"
        ],
        expected_parser_version=expected_versions["parser_version"],
        expected_prompt_version=expected_versions["prompt_version"],
    )
    population_evidence_kinds: dict[str, set[str]] = defaultdict(set)
    for claim in claim_rows:
        dimension = str(_get(claim, "dimension")).strip().lower()
        evidence_kind = str(_get(claim, "evidence_source_kind")).strip()
        if dimension in VALID_DIMENSIONS and evidence_kind:
            population_evidence_kinds[dimension].add(evidence_kind)

    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    claims_by_report: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
    for claim in sampled_claims:
        report_id = str(_get(claim, "report_id")).strip()
        claim_id = str(_get(claim, "claim_id")).strip()
        if not claim_id or claim_id in claims_by_report[report_id]:
            raise ValidationManifestError(
                f"population claims contain missing/duplicate claim_id for {report_id}"
            )
        evidence_span = str(_get(claim, "evidence_span"))
        claims_by_report[report_id][claim_id] = {
            "claim_id": claim_id,
            "target_type": str(_get(claim, "target_type")).strip(),
            "evidence_span_sha256": _sha256_bytes(evidence_span.encode("utf-8")),
            "claim_payload_sha256": claim_validation_payload_sha256(claim),
            "extractor_version": str(_get(claim, "extractor_version")).strip(),
            "evidence_source_kind": str(
                _get(claim, "evidence_source_kind")
            ).strip(),
            "evidence_source_hash": str(
                _get(claim, "evidence_source_hash")
            ).strip().lower(),
            "evidence_parser_version": str(
                _get(claim, "evidence_parser_version")
            ).strip(),
            "evidence_prompt_version": str(
                _get(claim, "evidence_prompt_version")
            ).strip(),
            "extractor_bundle_sha256": str(
                _get(claim, "extractor_bundle_sha256")
            ).strip().lower(),
        }
    seen_report_ids: set[tuple[str, str]] = set()
    for index, sample in enumerate(samples):
        if not isinstance(sample, Mapping):
            raise ValidationManifestError(f"samples[{index}] must be an object")
        unknown = set(sample) - {
            "report_id",
            "dimension",
            "source_url",
            "source_record_hash",
            "pdf_document_hash",
            "metadata_checks",
            "claim_set_complete",
            "extraction_checks",
        }
        if unknown:
            raise ValidationManifestError(f"samples[{index}] has unknown fields: {sorted(unknown)}")
        dimension = str(sample.get("dimension") or "").lower()
        report_id = str(sample.get("report_id") or "").strip()
        if dimension not in VALID_DIMENSIONS or not report_id:
            raise ValidationManifestError(f"samples[{index}] has invalid dimension/report_id")
        identity = (dimension, report_id)
        if identity in seen_report_ids:
            raise ValidationManifestError(f"duplicate validation report: {dimension}/{report_id}")
        seen_report_ids.add(identity)
        source_url = str(sample.get("source_url") or "")
        if not source_url.startswith(("http://", "https://")):
            raise ValidationManifestError(f"samples[{index}].source_url must be HTTP(S)")
        source_record_hash = str(sample.get("source_record_hash") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", source_record_hash):
            raise ValidationManifestError(
                f"samples[{index}].source_record_hash must be SHA-256"
            )
        pdf_document_hash = str(sample.get("pdf_document_hash") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", pdf_document_hash):
            raise ValidationManifestError(
                f"samples[{index}].pdf_document_hash must be SHA-256"
            )
        population_record = population.get(identity)
        if population_record is None:
            raise ValidationManifestError(f"samples[{index}] is outside current population")
        if source_record_hash != population_record["source_record_hash"]:
            raise ValidationManifestError(f"samples[{index}] source record hash mismatch")
        if not re.fullmatch(
            r"[0-9a-f]{64}", population_record["pdf_document_hash"]
        ):
            raise ValidationManifestError(
                f"samples[{index}] current sampled PDF has no verified document hash"
            )
        if pdf_document_hash != population_record["pdf_document_hash"]:
            raise ValidationManifestError(f"samples[{index}] PDF document hash mismatch")
        if source_url != population_record["source_url"]:
            raise ValidationManifestError(f"samples[{index}] source URL mismatch")
        metadata = sample.get("metadata_checks")
        if not isinstance(metadata, Mapping) or set(metadata) != set(METADATA_FIELDS):
            raise ValidationManifestError(
                f"samples[{index}].metadata_checks must contain {METADATA_FIELDS}"
            )
        if any(type(metadata[field]) is not bool for field in METADATA_FIELDS):
            raise ValidationManifestError(f"samples[{index}] metadata checks must be booleans")
        if type(sample.get("claim_set_complete")) is not bool:
            raise ValidationManifestError(
                f"samples[{index}].claim_set_complete must be a boolean"
            )
        checks = sample.get("extraction_checks")
        if not isinstance(checks, list):
            raise ValidationManifestError(f"samples[{index}] needs extraction_checks")
        expected_claims = claims_by_report.get(report_id, {})
        observed_claim_ids: set[str] = set()
        for check_index, check in enumerate(checks):
            expected_fields = {
                "claim_id",
                "target_type",
                "evidence_span_sha256",
                "claim_payload_sha256",
                *EVIDENCE_PROVENANCE_FIELDS,
                *EXTRACTION_FIELDS,
            }
            if not isinstance(check, Mapping) or set(check) != expected_fields:
                raise ValidationManifestError(
                    f"samples[{index}].extraction_checks[{check_index}] must bind one current claim and contain {EXTRACTION_FIELDS}"
                )
            if any(type(check[field]) is not bool for field in EXTRACTION_FIELDS):
                raise ValidationManifestError(
                    f"samples[{index}].extraction_checks[{check_index}] values must be booleans"
                )
            claim_id = str(check.get("claim_id") or "").strip()
            if not claim_id or claim_id in observed_claim_ids:
                raise ValidationManifestError(
                    f"samples[{index}] has missing/duplicate extraction claim_id"
                )
            expected_claim = expected_claims.get(claim_id)
            if expected_claim is None:
                raise ValidationManifestError(
                    f"samples[{index}] reviews a claim outside the current extractor output"
                )
            if str(check.get("target_type") or "").strip() != expected_claim["target_type"]:
                raise ValidationManifestError(
                    f"samples[{index}] target_type does not match current claim"
                )
            evidence_hash = str(check.get("evidence_span_sha256") or "").lower()
            if evidence_hash != expected_claim["evidence_span_sha256"]:
                raise ValidationManifestError(
                    f"samples[{index}] evidence span hash does not match current claim"
                )
            payload_hash = str(check.get("claim_payload_sha256") or "").lower()
            if payload_hash != expected_claim["claim_payload_sha256"]:
                raise ValidationManifestError(
                    f"samples[{index}] claim payload hash does not match current scoring fields"
                )
            for provenance_field in EVIDENCE_PROVENANCE_FIELDS:
                observed_value = str(check.get(provenance_field) or "").strip()
                if provenance_field.endswith("sha256") or provenance_field == "evidence_source_hash":
                    observed_value = observed_value.lower()
                if observed_value != expected_claim[provenance_field]:
                    raise ValidationManifestError(
                        f"samples[{index}] {provenance_field} does not match current claim"
                    )
            observed_claim_ids.add(claim_id)
        if observed_claim_ids != set(expected_claims):
            raise ValidationManifestError(
                f"samples[{index}] extraction checks do not cover the complete current claim set"
            )
        grouped[dimension].append(sample)

    result: dict[str, dict[str, Any]] = {}
    for dimension in VALID_DIMENSIONS:
        dimension_samples = grouped.get(dimension, [])
        observed_ids = tuple(sorted(str(sample["report_id"]) for sample in dimension_samples))
        required_ids = tuple(sorted(expected_samples[dimension]))
        if observed_ids != required_ids:
            raise ValidationManifestError(
                f"{dimension} samples do not match deterministic population sample"
            )
        metadata_total = len(dimension_samples) * len(METADATA_FIELDS)
        metadata_hits = sum(
            int(sample["metadata_checks"][field] is True)
            for sample in dimension_samples
            for field in METADATA_FIELDS
        )
        metadata_rate = metadata_hits / metadata_total if metadata_total else 0.0
        field_precision: dict[str, float] = {}
        field_counts: dict[str, int] = {}
        for field in EXTRACTION_FIELDS:
            decisions = [
                bool(check[field])
                for sample in dimension_samples
                for check in sample["extraction_checks"]
            ]
            field_counts[field] = len(decisions)
            field_precision[field] = (
                sum(decisions) / len(decisions) if decisions else 0.0
            )
        common_passed = (
            len(dimension_samples) >= int(minimum_samples_per_dimension)
            and metadata_rate == 1.0
            and all(
                sample["claim_set_complete"] is True
                for sample in dimension_samples
            )
        )
        observed_kinds = {
            str(check["evidence_source_kind"])
            for sample in dimension_samples
            for check in sample["extraction_checks"]
        }
        # This v3 contract validates the PDF extractor named at the manifest
        # root.  Structured source records may be reviewed alongside it, but
        # they cannot substitute for thirty PDF decisions per field.  Every
        # additional evidence channel present in the sample must independently
        # meet the same threshold as well.
        required_kinds = sorted(
            observed_kinds
            | population_evidence_kinds.get(dimension, set())
            | {"textual/pdf"}
        )
        evidence_source_validation: dict[str, dict[str, Any]] = {}
        for evidence_kind in required_kinds:
            kind_precision: dict[str, float] = {}
            kind_counts: dict[str, int] = {}
            for field in EXTRACTION_FIELDS:
                decisions = [
                    bool(check[field])
                    for sample in dimension_samples
                    for check in sample["extraction_checks"]
                    if check["evidence_source_kind"] == evidence_kind
                ]
                kind_counts[field] = len(decisions)
                kind_precision[field] = (
                    sum(decisions) / len(decisions) if decisions else 0.0
                )
            kind_passed = common_passed and all(
                kind_counts[field] >= int(minimum_samples_per_dimension)
                and kind_precision[field] >= float(minimum_field_precision)
                for field in EXTRACTION_FIELDS
            )
            evidence_source_validation[evidence_kind] = {
                "field_precision": min(kind_precision.values(), default=0.0),
                "field_precision_by_field": kind_precision,
                "field_decision_count": kind_counts,
                "passed": kind_passed,
            }
        passed = common_passed and bool(evidence_source_validation) and all(
            row["passed"] is True
            for row in evidence_source_validation.values()
        )
        result[dimension] = {
            "sample_count": len(dimension_samples),
            "metadata_match_rate": metadata_rate,
            "field_precision": min(field_precision.values(), default=0.0),
            "field_precision_by_field": field_precision,
            "field_decision_count": field_counts,
            "field_precision_by_evidence_kind": {
                kind: dict(values["field_precision_by_field"])
                for kind, values in evidence_source_validation.items()
            },
            "field_decision_count_by_evidence_kind": {
                kind: dict(values["field_decision_count"])
                for kind, values in evidence_source_validation.items()
            },
            "evidence_source_validation": evidence_source_validation,
            "claim_set_complete_rate": (
                sum(sample["claim_set_complete"] is True for sample in dimension_samples)
                / len(dimension_samples)
                if dimension_samples
                else 0.0
            ),
            "passed": passed,
            "manifest_sha256": actual_hash,
            "population_snapshot_hash": population_hash,
            "sample_seed": str(payload["sample_seed"]),
            "reviewer": str(payload["reviewer"]),
            "reviewed_at": reviewed_at.isoformat(),
            "validation_contract_version": VALIDATION_CONTRACT_VERSION,
            "extractor_version": expected_versions["extractor_version"],
            "extractor_bundle_sha256": expected_versions[
                "extractor_bundle_sha256"
            ],
            "parser_version": expected_versions["parser_version"],
            "prompt_version": expected_versions["prompt_version"],
        }
    return result


__all__ = [
    "EVIDENCE_PROVENANCE_FIELDS",
    "EXTRACTION_FIELDS",
    "METADATA_FIELDS",
    "VALIDATION_CONTRACT_VERSION",
    "ValidationManifestError",
    "claim_validation_payload_sha256",
    "deterministic_sample_ids",
    "load_validation_manifest",
    "population_snapshot",
    "validate_claim_evidence_bindings",
]
