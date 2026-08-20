"""Offline review package for extractor validation.

The browser page is deliberately a data-entry surface, not an authority.  A
downloaded review JSON cannot unlock validation by itself: finalisation
rebuilds the deterministic sample from the current population, hashes the
current PDF bytes again, binds every current claim payload, and delegates the
gate calculation to :mod:`research.broker_report_audit.validation`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .models import ensure_aware, parse_datetime
from .validation import (
    EVIDENCE_PROVENANCE_FIELDS,
    EXTRACTION_FIELDS,
    METADATA_FIELDS,
    VALIDATION_CONTRACT_VERSION,
    ValidationManifestError,
    claim_validation_payload_sha256,
    load_validation_manifest,
    population_snapshot,
    validate_claim_evidence_bindings,
)


REVIEW_CONTRACT_VERSION = "broker-report-extractor-review.v1"
REVIEW_PACKAGE_VERSION = "broker-report-extractor-review-package.v1"
VALID_DIMENSIONS = ("macro", "industry", "stock")
REVIEW_DECISIONS = frozenset({"pass", "correct", "reject"})
REVIEW_CLAIM_FIELDS = (
    "subject_id",
    "target_type",
    "direction",
    "value_min",
    "value_max",
    "unit",
    "benchmark",
    "forecast_period",
    "horizon_days",
    "evidence_span",
)
EXTRACTION_FIELD_GROUPS = MappingProxyType(
    {
        "variable": ("subject_id", "target_type", "evidence_span"),
        "direction": ("direction",),
        "value": ("value_min", "value_max", "unit", "benchmark"),
        "horizon": ("forecast_period", "horizon_days"),
    }
)


class ValidationReviewError(ValueError):
    """Raised when an offline review package is incomplete or has drifted."""


@dataclass(frozen=True)
class ValidationSelection:
    """Deterministic pre-download selection from the eligible population."""

    report_ids: tuple[str, ...]
    report_ids_by_dimension: Mapping[str, tuple[str, ...]]
    source_population_sha256: str
    eligible_population_count: int


@dataclass(frozen=True)
class ValidationPackageResult:
    """Paths and counts produced by :func:`prepare_validation_package`."""

    output_directory: Path
    html_path: Path
    package_sha256: str
    source_population_sha256: str
    selected_report_ids: tuple[str, ...]
    counts: Mapping[str, int]


@dataclass(frozen=True)
class ValidationFinalizeResult:
    """Validated v3 manifest and recomputed dimension gates."""

    output_directory: Path
    manifest_path: Path
    manifest_sha256: str
    review_sha256: str
    package_sha256: str
    counts: Mapping[str, int]
    gate_result: Mapping[str, Mapping[str, Any]]


def _get(record: Any, name: str, default: Any = "") -> Any:
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _normalise_sha256(value: Any, field: str, *, allow_empty: bool = False) -> str:
    digest = str(value or "").strip().lower()
    if allow_empty and not digest:
        return ""
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValidationReviewError(f"{field} must be a SHA-256 digest")
    return digest


def _review_value(value: Any) -> Any:
    if isinstance(value, datetime):
        ensure_aware(value)
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _strict_http_url(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text.startswith(("http://", "https://")):
        raise ValidationReviewError(f"{field} must be an HTTP(S) URL")
    return text


def _eligible_population(
    reports: Iterable[Any], claims: Iterable[Any]
) -> tuple[list[dict[str, str]], dict[str, Any], dict[str, tuple[Any, ...]]]:
    claims_by_report_mutable: dict[str, list[Any]] = {}
    seen_claim_ids: set[str] = set()
    for claim in claims:
        report_id = str(_get(claim, "report_id") or "").strip()
        claim_id = str(_get(claim, "claim_id") or "").strip()
        if not report_id or not claim_id or claim_id in seen_claim_ids:
            raise ValidationReviewError("claims need globally unique claim_id and report_id")
        seen_claim_ids.add(claim_id)
        claims_by_report_mutable.setdefault(report_id, []).append(claim)

    records: list[dict[str, str]] = []
    report_by_id: dict[str, Any] = {}
    for report in reports:
        report_id = str(_get(report, "report_id") or "").strip()
        dimension = str(_get(report, "dimension") or "").strip().lower()
        if not report_id or dimension not in VALID_DIMENSIONS:
            raise ValidationReviewError("reports need a valid report_id and dimension")
        if report_id in report_by_id:
            raise ValidationReviewError(f"duplicate report_id: {report_id}")
        report_by_id[report_id] = report
        source_url = _strict_http_url(
            _get(report, "source_url"), f"report {report_id} source_url"
        )
        raw_pdf_url = str(_get(report, "pdf_url") or "").strip()
        # Listing records frequently expose only a report-detail URL.  The
        # source adapter resolves the PDF after deterministic selection, so a
        # missing pre-download pdf_url cannot exclude the report population.
        pdf_locator = (
            _strict_http_url(raw_pdf_url, f"report {report_id} pdf_url")
            if raw_pdf_url
            else source_url
        )
        records.append(
            {
                "report_id": report_id,
                "dimension": dimension,
                "source_record_hash": _normalise_sha256(
                    _get(report, "content_hash"), f"report {report_id} content_hash"
                ),
                "source_url": source_url,
                "pdf_url": pdf_locator,
                "available_at": str(_review_value(_get(report, "available_at"))),
            }
        )
    records.sort(key=lambda item: (item["dimension"], item["report_id"]))
    claims_by_report = {
        report_id: tuple(
            sorted(values, key=lambda claim: str(_get(claim, "claim_id")))
        )
        for report_id, values in claims_by_report_mutable.items()
        if report_id in report_by_id
    }
    return records, report_by_id, claims_by_report


def select_validation_reports(
    reports: Iterable[Any],
    claims: Iterable[Any] = (),
    *,
    sample_seed: str,
    count_per_dimension: int = 30,
) -> ValidationSelection:
    """Select exactly ``count_per_dimension`` source reports per layer.

    This function performs no I/O and is intended to run before downloading
    PDFs.  Its output is therefore the authoritative bounded download list.
    """

    seed = str(sample_seed or "").strip()
    if not seed:
        raise ValidationReviewError("sample_seed is required")
    if int(count_per_dimension) != 30:
        raise ValidationReviewError("extractor validation requires exactly 30 reports per dimension")
    # Pre-download selection is based only on the source-report population.
    # Claims are accepted for call-site compatibility but intentionally cannot
    # influence which PDFs are selected.
    del claims
    records, _reports, _claims = _eligible_population(reports, ())
    stable_population = [
        {
            "dimension": record["dimension"],
            "report_id": record["report_id"],
            "source_record_hash": record["source_record_hash"],
            "source_url": record["source_url"],
            "available_at": record["available_at"],
        }
        for record in records
    ]
    source_population_sha256 = _sha256_bytes(_canonical_bytes(stable_population))
    by_dimension: dict[str, tuple[str, ...]] = {}
    for dimension in VALID_DIMENSIONS:
        candidates = [
            record["report_id"]
            for record in records
            if record["dimension"] == dimension
        ]
        candidates.sort(
            key=lambda report_id: (
                _sha256_bytes(f"{seed}|{dimension}|{report_id}".encode("utf-8")),
                report_id,
            )
        )
        if len(candidates) < 30:
            raise ValidationReviewError(
                f"{dimension} has {len(candidates)} eligible reports; 30 are required"
            )
        by_dimension[dimension] = tuple(candidates[:30])
    ordered = tuple(
        report_id
        for dimension in VALID_DIMENSIONS
        for report_id in by_dimension[dimension]
    )
    return ValidationSelection(
        report_ids=ordered,
        report_ids_by_dimension=MappingProxyType(dict(by_dimension)),
        source_population_sha256=source_population_sha256,
        eligible_population_count=len(records),
    )


def _selected_evidence(
    reports: Iterable[Any],
    claims: Iterable[Any],
    pdf_files: Mapping[str, str | Path],
    *,
    sample_seed: str,
) -> tuple[ValidationSelection, list[dict[str, Any]], list[Any], dict[str, bytes]]:
    report_list = tuple(reports)
    claim_list = tuple(claims)
    selection = select_validation_reports(
        report_list, claim_list, sample_seed=sample_seed
    )
    _records, report_by_id, claims_by_report = _eligible_population(
        report_list, claim_list
    )
    supplied_ids = {str(key) for key in pdf_files}
    selected_ids = set(selection.report_ids)
    if supplied_ids != selected_ids:
        raise ValidationReviewError(
            "pdf_files must contain exactly the deterministic 90-report sample"
        )

    selected_reports: list[dict[str, Any]] = []
    selected_claims: list[Any] = []
    pdf_bytes: dict[str, bytes] = {}
    for report_id in selection.report_ids:
        path = Path(pdf_files[report_id])
        if not path.is_file():
            raise ValidationReviewError(f"selected PDF is missing: {report_id}")
        try:
            body = path.read_bytes()
        except OSError as exc:
            raise ValidationReviewError(f"cannot read selected PDF: {report_id}") from exc
        if not body.startswith(b"%PDF-"):
            raise ValidationReviewError(f"selected evidence is not a PDF: {report_id}")
        actual_pdf_sha256 = _sha256_bytes(body)
        report = report_by_id[report_id]
        claimed_pdf_sha256 = _normalise_sha256(
            _get(report, "pdf_sha256"),
            f"report {report_id} pdf_sha256",
            allow_empty=True,
        )
        if claimed_pdf_sha256 and claimed_pdf_sha256 != actual_pdf_sha256:
            raise ValidationReviewError(f"selected PDF hash mismatch: {report_id}")
        published_at = _get(report, "published_at")
        selected_reports.append(
            {
                "report_id": report_id,
                "dimension": str(_get(report, "dimension")).lower(),
                "subject_id": str(_get(report, "subject_id") or ""),
                "source_url": _strict_http_url(
                    _get(report, "source_url"), f"report {report_id} source_url"
                ),
                "pdf_url": (
                    _strict_http_url(
                        _get(report, "pdf_url"), f"report {report_id} pdf_url"
                    )
                    if str(_get(report, "pdf_url") or "").strip()
                    else _strict_http_url(
                        _get(report, "source_url"), f"report {report_id} source_url"
                    )
                ),
                "content_hash": _normalise_sha256(
                    _get(report, "content_hash"), f"report {report_id} content_hash"
                ),
                "pdf_sha256": actual_pdf_sha256,
                "available_at": _review_value(_get(report, "available_at")),
                "metadata": {
                    "broker": _review_value(_get(report, "broker")),
                    "title": _review_value(_get(report, "title")),
                    "date": _review_value(published_at),
                    "subject": _review_value(
                        _get(report, "subject_name") or _get(report, "subject_id")
                    ),
                },
            }
        )
        report_claims = claims_by_report.get(report_id, ())
        selected_claims.extend(report_claims)
        pdf_bytes[report_id] = body
    return selection, selected_reports, selected_claims, pdf_bytes


def _package_descriptor(
    selection: ValidationSelection,
    selected_reports: Iterable[Mapping[str, Any]],
    selected_claims: Iterable[Any],
    *,
    sample_seed: str,
    extractor_version: str,
    extractor_bundle_sha256: str,
    parser_version: str,
    prompt_version: str,
) -> dict[str, Any]:
    versions = {
        "extractor_version": str(extractor_version or "").strip(),
        "extractor_bundle_sha256": _normalise_sha256(
            extractor_bundle_sha256, "extractor_bundle_sha256"
        ),
        "parser_version": str(parser_version or "").strip(),
        "prompt_version": str(prompt_version or "").strip(),
    }
    if not all(versions.values()):
        raise ValidationReviewError("extractor/parser/prompt versions are required")
    claim_by_report: dict[str, list[dict[str, Any]]] = {}
    for claim in selected_claims:
        report_id = str(_get(claim, "report_id"))
        record = {
            "claim_id": str(_get(claim, "claim_id")),
            "claim_payload_sha256": claim_validation_payload_sha256(claim),
            "evidence_span_sha256": _sha256_bytes(
                str(_get(claim, "evidence_span")).encode("utf-8")
            ),
            "fields": {
                field: _review_value(_get(claim, field, None))
                for field in REVIEW_CLAIM_FIELDS
            },
            "immutable_versions": {
                "extractor_version": str(_get(claim, "extractor_version")),
                "evidence_source_kind": str(_get(claim, "evidence_source_kind")),
                "evidence_source_hash": str(_get(claim, "evidence_source_hash")),
                "evidence_parser_version": str(
                    _get(claim, "evidence_parser_version")
                ),
                "evidence_prompt_version": str(
                    _get(claim, "evidence_prompt_version")
                ),
                "extractor_bundle_sha256": str(
                    _get(claim, "extractor_bundle_sha256")
                ),
            },
        }
        claim_by_report.setdefault(report_id, []).append(record)
    samples: list[dict[str, Any]] = []
    for report in selected_reports:
        report_id = str(report["report_id"])
        claims = sorted(
            claim_by_report.get(report_id, ()), key=lambda item: item["claim_id"]
        )
        samples.append({**dict(report), "claims": claims})
    selected_population_hash, _ = population_snapshot(samples)
    return {
        "package_contract_version": REVIEW_PACKAGE_VERSION,
        "review_contract_version": REVIEW_CONTRACT_VERSION,
        "validation_contract_version": VALIDATION_CONTRACT_VERSION,
        "sample_seed": str(sample_seed),
        "source_population_sha256": selection.source_population_sha256,
        "selected_population_snapshot_hash": selected_population_hash,
        **versions,
        "review_metadata_fields": list(METADATA_FIELDS),
        "review_claim_fields": list(REVIEW_CLAIM_FIELDS),
        "extraction_field_groups": {
            key: list(value) for key, value in EXTRACTION_FIELD_GROUPS.items()
        },
        "samples": samples,
    }


def _safe_script_json(value: Any) -> str:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def _render_html(
    descriptor: Mapping[str, Any], package_sha256: str, pdf_bytes: Mapping[str, bytes]
) -> str:
    pdf_data = {
        report_id: "data:application/pdf;base64,"
        + base64.b64encode(body).decode("ascii")
        for report_id, body in pdf_bytes.items()
    }
    template = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; object-src data: blob:; frame-src data: blob:;">
<title>研报抽取人工审核（90份）</title>
<style>
body{font-family:system-ui,"Microsoft YaHei",sans-serif;margin:0;background:#f5f7fb;color:#172033}header{position:sticky;top:0;background:#fff;border-bottom:1px solid #d9dfeb;padding:12px 18px;z-index:2}.grid{display:grid;grid-template-columns:minmax(420px,1fr) minmax(520px,1.25fr);gap:14px;padding:14px}.panel{background:#fff;border:1px solid #d9dfeb;border-radius:10px;padding:14px;min-width:0}.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap}button,input,textarea{font:inherit}button{padding:7px 12px}.progress{font-weight:700}.pdf{width:100%;height:75vh;border:1px solid #ccd4e3}.field{border-top:1px solid #e6eaf2;padding:9px 0}.original{white-space:pre-wrap;background:#f3f5f9;padding:7px;border-radius:5px;margin:5px 0}.decision{display:flex;gap:12px;flex-wrap:wrap}.correction{width:100%;box-sizing:border-box;margin-top:6px}.claim{border:1px solid #dce2ed;border-radius:8px;padding:10px;margin:12px 0}.warn{color:#9b3b00}.meta{font-size:13px;word-break:break-all}a{color:#1556b6}</style></head>
<body><header><div class="toolbar"><button id="prev">上一份</button><button id="next">下一份</button><span id="position"></span><label>reviewer_id <input id="reviewer" autocomplete="off" maxlength="128"></label><span class="progress" id="progress"></span><button id="export" disabled>导出 review JSON</button></div><div class="warn">页面完全离线。只有全部字段完成后才能导出；导出文件仍须由 CLI 重新校验当前 PDF、版本和抽取载荷。</div></header>
<main class="grid"><section class="panel"><h2 id="reportTitle"></h2><div class="meta" id="reportMeta"></div><p><a id="pdfDownload" download>下载/另开当前PDF</a></p><object id="pdf" class="pdf" type="application/pdf"><p>浏览器无法内嵌PDF，请使用上方下载链接。</p></object></section><section class="panel" id="review"></section></main>
<script id="package" type="application/json">__PACKAGE__</script><script id="pdfData" type="application/json">__PDF_DATA__</script>
<script>
"use strict";
const pkg=JSON.parse(document.getElementById("package").textContent);const pdfData=JSON.parse(document.getElementById("pdfData").textContent);const packageSha="__PACKAGE_SHA__";const samples=pkg.samples;let index=0;const state={};
function slot(rid){return state[rid]||(state[rid]={metadata:{},claimSet:null,claims:{}})}
function decisionControl(container,key,current){const wrap=document.createElement("div");wrap.className="field";const original=document.createElement("div");original.className="original";original.textContent=typeof key.original==="string"?key.original:JSON.stringify(key.original);wrap.appendChild(original);const radios=document.createElement("div");radios.className="decision";["pass","correct","reject"].forEach(d=>{const label=document.createElement("label");const input=document.createElement("input");input.type="radio";input.name=key.name;input.value=d;input.checked=!!current&&current.decision===d;input.addEventListener("change",()=>{key.save({decision:d,corrected_value:correction.value});refreshProgress()});label.append(input,document.createTextNode(" "+d));radios.appendChild(label)});wrap.appendChild(radios);const correction=document.createElement("textarea");correction.className="correction";correction.rows=2;correction.placeholder="decision=correct 时填写修正值；pass/reject 保持空白";correction.value=current?current.corrected_value||"":"";correction.addEventListener("input",()=>{const selected=radios.querySelector("input:checked");if(selected)key.save({decision:selected.value,corrected_value:correction.value});refreshProgress()});wrap.appendChild(correction);container.appendChild(wrap)}
function render(){const sample=samples[index],s=slot(sample.report_id);document.getElementById("position").textContent=`${index+1}/${samples.length} · ${sample.dimension}`;document.getElementById("reportTitle").textContent=sample.metadata.title||sample.report_id;document.getElementById("reportMeta").textContent=`report_id=${sample.report_id}\nsource=${sample.source_url}\npdf_sha256=${sample.pdf_sha256}`;const p=pdfData[sample.report_id];document.getElementById("pdf").data=p;document.getElementById("pdfDownload").href=p;document.getElementById("pdfDownload").download=sample.report_id+".pdf";const root=document.getElementById("review");root.replaceChildren();const mh=document.createElement("h3");mh.textContent="元数据审核";root.appendChild(mh);pkg.review_metadata_fields.forEach(field=>{const label=document.createElement("strong");label.textContent=field;root.appendChild(label);decisionControl(root,{name:`m-${sample.report_id}-${field}`,original:sample.metadata[field],save:v=>s.metadata[field]=v},s.metadata[field])});const ch=document.createElement("h3");ch.textContent="主张集合完整性";root.appendChild(ch);decisionControl(root,{name:`set-${sample.report_id}`,original:`当前共 ${sample.claims.length} 条主张；请核对PDF是否有漏抽/多抽`,save:v=>s.claimSet=v},s.claimSet);sample.claims.forEach((claim,ci)=>{const box=document.createElement("div");box.className="claim";const h=document.createElement("h3");h.textContent=`主张 ${ci+1}: ${claim.claim_id}`;box.appendChild(h);const provenance=document.createElement("div");provenance.className="meta";const v=claim.immutable_versions;provenance.textContent=`evidence_source_kind=${v.evidence_source_kind}\nevidence_source_hash=${v.evidence_source_hash}\nextractor=${v.extractor_version}\nparser=${v.evidence_parser_version}\nprompt=${v.evidence_prompt_version}\nextractor_bundle_sha256=${v.extractor_bundle_sha256}`;box.appendChild(provenance);const cs=s.claims[claim.claim_id]||(s.claims[claim.claim_id]={});pkg.review_claim_fields.forEach(field=>{const label=document.createElement("strong");label.textContent=field;box.appendChild(label);decisionControl(box,{name:`c-${claim.claim_id}-${field}`,original:claim.fields[field],save:v=>cs[field]=v},cs[field])});root.appendChild(box)});refreshProgress()}
function validDecision(v){if(!v||!["pass","correct","reject"].includes(v.decision))return false;const text=(v.corrected_value||"").trim();return v.decision==="correct"?text.length>0:text.length===0}
function completeSample(sample){const s=state[sample.report_id];if(!s)return false;if(!pkg.review_metadata_fields.every(f=>validDecision(s.metadata[f])))return false;if(!validDecision(s.claimSet))return false;return sample.claims.every(c=>pkg.review_claim_fields.every(f=>validDecision((s.claims[c.claim_id]||{})[f])))}
function refreshProgress(){const done=samples.filter(completeSample).length;document.getElementById("progress").textContent=`完成 ${done}/${samples.length}`;document.getElementById("export").disabled=done!==samples.length||!document.getElementById("reviewer").value.trim()}
function exportReview(){if(document.getElementById("export").disabled)return;const payload={contract_version:pkg.review_contract_version,package_sha256:packageSha,reviewer_id:document.getElementById("reviewer").value.trim(),reviewed_at:new Date().toISOString(),samples:samples.map(sample=>{const s=state[sample.report_id];return{report_id:sample.report_id,dimension:sample.dimension,metadata_reviews:s.metadata,claim_set_review:s.claimSet,extraction_reviews:sample.claims.map(c=>({claim_id:c.claim_id,fields:s.claims[c.claim_id]}))}})};const blob=new Blob([JSON.stringify(payload,null,2)],{type:"application/json"});const a=document.createElement("a");a.href=URL.createObjectURL(blob);a.download="extractor-review.json";a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)}
document.getElementById("prev").onclick=()=>{index=(index+samples.length-1)%samples.length;render()};document.getElementById("next").onclick=()=>{index=(index+1)%samples.length;render()};document.getElementById("reviewer").addEventListener("input",refreshProgress);document.getElementById("export").onclick=exportReview;render();
</script></body></html>'''
    return (
        template.replace("__PACKAGE__", _safe_script_json(descriptor))
        .replace("__PDF_DATA__", _safe_script_json(pdf_data))
        .replace("__PACKAGE_SHA__", package_sha256)
    )


def _validate_selected_evidence_bindings(
    claims: Iterable[Any],
    reports: Iterable[Any],
    *,
    extractor_version: str,
    extractor_bundle_sha256: str,
    parser_version: str,
    prompt_version: str,
) -> None:
    try:
        validate_claim_evidence_bindings(
            claims,
            reports,
            expected_extractor_version=extractor_version,
            expected_extractor_bundle_sha256=extractor_bundle_sha256,
            expected_parser_version=parser_version,
            expected_prompt_version=prompt_version,
        )
    except ValidationManifestError as exc:
        raise ValidationReviewError(str(exc)) from exc


def prepare_validation_package(
    reports: Iterable[Any],
    claims: Iterable[Any],
    *,
    pdf_files: Mapping[str, str | Path],
    output_directory: str | Path,
    sample_seed: str,
    extractor_version: str,
    extractor_bundle_sha256: str,
    parser_version: str,
    prompt_version: str,
) -> ValidationPackageResult:
    """Create one self-contained HTML page for exactly 90 selected PDFs."""

    report_list, claim_list = tuple(reports), tuple(claims)
    selection, selected_reports, selected_claims, pdf_bytes = _selected_evidence(
        report_list, claim_list, pdf_files, sample_seed=sample_seed
    )
    _validate_selected_evidence_bindings(
        selected_claims,
        selected_reports,
        extractor_version=extractor_version,
        extractor_bundle_sha256=extractor_bundle_sha256,
        parser_version=parser_version,
        prompt_version=prompt_version,
    )
    descriptor = _package_descriptor(
        selection,
        selected_reports,
        selected_claims,
        sample_seed=sample_seed,
        extractor_version=extractor_version,
        extractor_bundle_sha256=extractor_bundle_sha256,
        parser_version=parser_version,
        prompt_version=prompt_version,
    )
    package_sha256 = _sha256_bytes(_canonical_bytes(descriptor))
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    html_path = output / "extractor_validation_review.html"
    html = _render_html(descriptor, package_sha256, pdf_bytes)
    html_path.write_text(html, encoding="utf-8", newline="\n")
    counts = {
        dimension: len(selection.report_ids_by_dimension[dimension])
        for dimension in VALID_DIMENSIONS
    }
    counts["total"] = len(selection.report_ids)
    counts["claims"] = len(selected_claims)
    return ValidationPackageResult(
        output_directory=output,
        html_path=html_path,
        package_sha256=package_sha256,
        source_population_sha256=selection.source_population_sha256,
        selected_report_ids=selection.report_ids,
        counts=MappingProxyType(counts),
    )


def _load_review_json(path: Path) -> tuple[Mapping[str, Any], str]:
    if not path.is_file():
        raise ValidationReviewError(f"review JSON does not exist: {path}")
    body = path.read_bytes()

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=no_duplicates,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite number: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValidationReviewError("review must be strict UTF-8 JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValidationReviewError("review JSON root must be an object")
    return payload, _sha256_bytes(body)


def _validate_decision(value: Any, field: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"decision", "corrected_value"}:
        raise ValidationReviewError(
            f"{field} must contain decision and corrected_value only"
        )
    decision = value.get("decision")
    corrected = value.get("corrected_value")
    if decision not in REVIEW_DECISIONS or not isinstance(corrected, str):
        raise ValidationReviewError(f"{field} has an invalid decision")
    text = corrected.strip()
    if decision == "correct" and not text:
        raise ValidationReviewError(f"{field} correction is empty")
    if decision != "correct" and text:
        raise ValidationReviewError(f"{field} correction is only allowed for correct")
    return {"decision": str(decision), "corrected_value": corrected}


def _review_to_manifest(
    review: Mapping[str, Any],
    review_sha256: str,
    descriptor: Mapping[str, Any],
    package_sha256: str,
    *,
    as_of: datetime,
) -> dict[str, Any]:
    ensure_aware(as_of, "as_of")
    if set(review) != {
        "contract_version",
        "package_sha256",
        "reviewer_id",
        "reviewed_at",
        "samples",
    }:
        raise ValidationReviewError("review JSON top-level fields do not match the contract")
    if review.get("contract_version") != REVIEW_CONTRACT_VERSION:
        raise ValidationReviewError("review contract_version mismatch")
    if review.get("package_sha256") != package_sha256:
        raise ValidationReviewError("review package_sha256 does not match current evidence")
    reviewer = str(review.get("reviewer_id") or "").strip()
    if not (2 <= len(reviewer) <= 128) or any(ord(char) < 32 for char in reviewer):
        raise ValidationReviewError("reviewer_id must contain 2-128 visible characters")
    reviewed_at_raw = review.get("reviewed_at")
    if not isinstance(reviewed_at_raw, str):
        raise ValidationReviewError("reviewed_at must be an ISO-8601 string")
    reviewed_at = parse_datetime(reviewed_at_raw)
    if "T" not in reviewed_at_raw or (
        not reviewed_at_raw.endswith("Z")
        and not re.search(r"[+-]\d\d:\d\d$", reviewed_at_raw)
    ):
        raise ValidationReviewError("reviewed_at must include an explicit timezone")
    if reviewed_at > as_of:
        raise ValidationReviewError("reviewed_at is after the validation cutoff")
    review_samples = review.get("samples")
    if not isinstance(review_samples, list) or len(review_samples) != 90:
        raise ValidationReviewError("review must contain exactly 90 samples")
    by_identity: dict[tuple[str, str], Mapping[str, Any]] = {}
    for index, sample in enumerate(review_samples):
        if not isinstance(sample, Mapping) or set(sample) != {
            "report_id",
            "dimension",
            "metadata_reviews",
            "claim_set_review",
            "extraction_reviews",
        }:
            raise ValidationReviewError(f"review samples[{index}] fields mismatch")
        identity = (str(sample.get("dimension")), str(sample.get("report_id")))
        if identity in by_identity:
            raise ValidationReviewError(f"duplicate review sample: {identity}")
        by_identity[identity] = sample

    manifest_samples: list[dict[str, Any]] = []
    for expected in descriptor["samples"]:
        identity = (str(expected["dimension"]), str(expected["report_id"]))
        sample = by_identity.pop(identity, None)
        if sample is None:
            raise ValidationReviewError(f"missing review sample: {identity}")
        metadata = sample.get("metadata_reviews")
        if not isinstance(metadata, Mapping) or set(metadata) != set(METADATA_FIELDS):
            raise ValidationReviewError(f"metadata review fields mismatch: {identity}")
        metadata_checks = {
            field: _validate_decision(metadata[field], f"{identity}.{field}")[
                "decision"
            ]
            == "pass"
            for field in METADATA_FIELDS
        }
        claim_set = _validate_decision(
            sample.get("claim_set_review"), f"{identity}.claim_set_review"
        )
        extraction = sample.get("extraction_reviews")
        if not isinstance(extraction, list):
            raise ValidationReviewError(f"{identity}.extraction_reviews must be a list")
        extraction_by_id: dict[str, Mapping[str, Any]] = {}
        for review_index, claim_review in enumerate(extraction):
            if not isinstance(claim_review, Mapping) or set(claim_review) != {
                "claim_id",
                "fields",
            }:
                raise ValidationReviewError(
                    f"{identity}.extraction_reviews[{review_index}] fields mismatch"
                )
            claim_id = str(claim_review.get("claim_id") or "")
            if not claim_id or claim_id in extraction_by_id:
                raise ValidationReviewError(f"{identity} has duplicate claim review")
            extraction_by_id[claim_id] = claim_review
        checks: list[dict[str, Any]] = []
        for expected_claim in expected["claims"]:
            claim_id = str(expected_claim["claim_id"])
            claim_review = extraction_by_id.pop(claim_id, None)
            if claim_review is None:
                raise ValidationReviewError(f"missing review for claim {claim_id}")
            fields = claim_review.get("fields")
            if not isinstance(fields, Mapping) or set(fields) != set(REVIEW_CLAIM_FIELDS):
                raise ValidationReviewError(f"claim {claim_id} review fields mismatch")
            decisions = {
                field: _validate_decision(fields[field], f"{claim_id}.{field}")[
                    "decision"
                ]
                for field in REVIEW_CLAIM_FIELDS
            }
            checks.append(
                {
                    "claim_id": claim_id,
                    "target_type": str(expected_claim["fields"]["target_type"]),
                    "evidence_span_sha256": expected_claim["evidence_span_sha256"],
                    "claim_payload_sha256": expected_claim[
                        "claim_payload_sha256"
                    ],
                    **{
                        field: str(expected_claim["immutable_versions"][field])
                        for field in EVIDENCE_PROVENANCE_FIELDS
                    },
                    **{
                        group: all(decisions[field] == "pass" for field in fields_in_group)
                        for group, fields_in_group in EXTRACTION_FIELD_GROUPS.items()
                    },
                }
            )
        if extraction_by_id:
            raise ValidationReviewError(f"review contains unknown claims: {identity}")
        manifest_samples.append(
            {
                "report_id": expected["report_id"],
                "dimension": expected["dimension"],
                "source_url": expected["source_url"],
                "source_record_hash": expected["content_hash"],
                "pdf_document_hash": expected["pdf_sha256"],
                "metadata_checks": metadata_checks,
                "claim_set_complete": claim_set["decision"] == "pass",
                "extraction_checks": checks,
            }
        )
    if by_identity:
        raise ValidationReviewError("review contains samples outside the current selection")
    return {
        "contract_version": VALIDATION_CONTRACT_VERSION,
        "sample_seed": descriptor["sample_seed"],
        "population_snapshot_hash": descriptor["source_population_sha256"],
        "source_population_sha256": descriptor["source_population_sha256"],
        "extractor_version": descriptor["extractor_version"],
        "extractor_bundle_sha256": descriptor["extractor_bundle_sha256"],
        "parser_version": descriptor["parser_version"],
        "prompt_version": descriptor["prompt_version"],
        "reviewer": reviewer,
        "reviewed_at": reviewed_at.isoformat(),
        "review_contract_version": REVIEW_CONTRACT_VERSION,
        "review_package_sha256": package_sha256,
        "review_export_sha256": review_sha256,
        "samples": manifest_samples,
    }


def _population_with_sample_pdf_hashes(
    reports: Iterable[Any], selected_reports: Iterable[Mapping[str, Any]]
) -> tuple[dict[str, Any], ...]:
    """Overlay only the 90 sampled PDF hashes on the full source population."""

    selected = {
        str(report["report_id"]): str(report["pdf_sha256"])
        for report in selected_reports
    }
    population: list[dict[str, Any]] = []
    for report in reports:
        report_id = str(_get(report, "report_id") or "").strip()
        population.append(
            {
                "report_id": report_id,
                "dimension": str(_get(report, "dimension") or "").strip().lower(),
                "subject_id": str(_get(report, "subject_id") or "").strip(),
                "content_hash": str(_get(report, "content_hash") or "").strip(),
                "source_url": str(_get(report, "source_url") or "").strip(),
                "pdf_url": str(_get(report, "pdf_url") or "").strip(),
                "available_at": _review_value(_get(report, "available_at")),
                "pdf_sha256": selected.get(
                    report_id, str(_get(report, "pdf_sha256") or "").strip()
                ),
            }
        )
    return tuple(population)


def finalize_validation_review(
    review_path: str | Path,
    reports: Iterable[Any],
    claims: Iterable[Any],
    *,
    pdf_files: Mapping[str, str | Path],
    output_directory: str | Path,
    as_of: datetime,
    sample_seed: str,
    extractor_version: str,
    extractor_bundle_sha256: str,
    parser_version: str,
    prompt_version: str,
    minimum_field_precision: float = 0.95,
) -> ValidationFinalizeResult:
    """Validate a browser export and atomically emit a passing v3 manifest."""

    report_list, claim_list = tuple(reports), tuple(claims)
    selection, selected_reports, selected_claims, _pdf_bytes = _selected_evidence(
        report_list, claim_list, pdf_files, sample_seed=sample_seed
    )
    _validate_selected_evidence_bindings(
        selected_claims,
        selected_reports,
        extractor_version=extractor_version,
        extractor_bundle_sha256=extractor_bundle_sha256,
        parser_version=parser_version,
        prompt_version=prompt_version,
    )
    descriptor = _package_descriptor(
        selection,
        selected_reports,
        selected_claims,
        sample_seed=sample_seed,
        extractor_version=extractor_version,
        extractor_bundle_sha256=extractor_bundle_sha256,
        parser_version=parser_version,
        prompt_version=prompt_version,
    )
    full_population = _population_with_sample_pdf_hashes(
        report_list, selected_reports
    )
    full_population_hash, _ = population_snapshot(full_population)
    if full_population_hash != descriptor["source_population_sha256"]:
        raise ValidationReviewError(
            "full population changed between selection and finalisation"
        )
    package_sha256 = _sha256_bytes(_canonical_bytes(descriptor))
    review, review_sha256 = _load_review_json(Path(review_path))
    manifest = _review_to_manifest(
        review, review_sha256, descriptor, package_sha256, as_of=as_of
    )
    body = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, indent=2
    ).encode("utf-8") + b"\n"
    manifest_sha256 = _sha256_bytes(body)
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    final_path = output / "extractor_validation.v3.json"
    fd, temporary_name = tempfile.mkstemp(
        prefix=".extractor-validation-", suffix=".json", dir=output
    )
    os.close(fd)
    temporary_path = Path(temporary_name)
    try:
        temporary_path.write_bytes(body)
        gate = load_validation_manifest(
            temporary_path,
            expected_sha256=manifest_sha256,
            as_of=as_of,
            population_reports=full_population,
            population_claims=claim_list,
            expected_extractor_version=extractor_version,
            expected_extractor_bundle_sha256=extractor_bundle_sha256,
            expected_parser_version=parser_version,
            expected_prompt_version=prompt_version,
            minimum_samples_per_dimension=30,
            minimum_field_precision=minimum_field_precision,
        )
        failed = [dimension for dimension in VALID_DIMENSIONS if not gate[dimension]["passed"]]
        if failed:
            raise ValidationReviewError(
                "review does not meet the formal validation thresholds: "
                + ",".join(failed)
            )
        os.replace(temporary_path, final_path)
    except (ValidationManifestError, OSError) as exc:
        raise ValidationReviewError(str(exc)) from exc
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    counts = {
        dimension: int(gate[dimension]["sample_count"])
        for dimension in VALID_DIMENSIONS
    }
    counts["total"] = sum(counts.values())
    counts["claims"] = len(selected_claims)
    return ValidationFinalizeResult(
        output_directory=output,
        manifest_path=final_path,
        manifest_sha256=manifest_sha256,
        review_sha256=review_sha256,
        package_sha256=package_sha256,
        counts=MappingProxyType(counts),
        gate_result=MappingProxyType(
            {dimension: MappingProxyType(dict(values)) for dimension, values in gate.items()}
        ),
    )


__all__ = [
    "EXTRACTION_FIELD_GROUPS",
    "REVIEW_CLAIM_FIELDS",
    "REVIEW_CONTRACT_VERSION",
    "REVIEW_PACKAGE_VERSION",
    "ValidationFinalizeResult",
    "ValidationPackageResult",
    "ValidationReviewError",
    "ValidationSelection",
    "finalize_validation_review",
    "prepare_validation_package",
    "select_validation_reports",
]
