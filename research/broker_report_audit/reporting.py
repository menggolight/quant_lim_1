"""Deterministic reporting for the three-layer broker-report audit.

This module is intentionally storage- and analytics-agnostic.  It accepts
domain dataclasses or plain mappings, renders the fixed V1 artifacts, and
fails closed when evidence is insufficient.  In particular, it never creates
synthetic skill estimates, factor observations, deep-read candidates, or
walk-forward performance.
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from research.reproducibility import git_worktree_state


DIMENSIONS = ("macro", "industry", "stock")
SCOPE_NOTICE = "东方财富公开抓取样本，不代表券商全部报告。"
RESEARCH_NOTICE = "仅供个人研究；不是投资建议，不连接自动下单。"

ARTIFACT_FILENAMES = (
    "macro_accuracy.csv",
    "industry_accuracy.csv",
    "stock_accuracy.csv",
    "broker_skill_cube.csv",
    "three_layer_factor.csv",
    "factor_walk_forward_report.md",
    "three_layer_dashboard.md",
    "deep_read_queue.md",
    "source_coverage.csv",
    "exceptions.csv",
    "run_manifest.json",
)
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

ACCURACY_COLUMNS = (
    "report_id",
    "claim_id",
    "dimension",
    "subject_id",
    "subject_name",
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
    "broker",
    "analyst",
    "team",
    "report_title",
    "report_published_at",
    "truth_source",
    "market_truth_source",
    "truth_unit",
    "truth_basis",
    "truth_change_value",
    "truth_change_basis",
    "market_benchmark_id",
    "market_benchmark_kind",
    "truth_available_at",
    "realized_value",
    "outcome_error",
    "outcome_hit",
    "fundamental_hit",
    "market_return",
    "benchmark_return",
    "market_excess_return",
    "market_hit",
    "market_exclusion_reason",
    "fundamental_truth_eligible",
    "market_truth_eligible",
    "mature",
    "eligible_for_scoring",
    "exclusion_reason",
)

SKILL_COLUMNS = (
    "snapshot_id",
    "as_of",
    "broker",
    "broker_display",
    "analyst",
    "team",
    "dimension",
    "target_type",
    "horizon_days",
    "market_state",
    "industry_id",
    "posterior_skill",
    "conservative_lower_bound",
    "effective_sample_size",
    "sensitivity_365",
    "sensitivity_365_lower_bound",
    "sensitivity_365_effective_sample_size",
    "sensitivity_delta",
    "source_report_ids",
    "rank_eligible",
    "rank",
    "skill_status",
    "exclusion_reason",
)

FACTOR_VALUE_COLUMNS = (
    "macro_objective_factor",
    "macro_report_raw",
    "macro_report_factor",
    "industry_objective_factor",
    "industry_report_raw",
    "industry_report_factor",
    "stock_objective_factor",
    "stock_report_raw",
    "stock_report_factor",
    "macro_industry_interaction",
    "industry_stock_interaction",
)

FACTOR_COLUMNS = (
    "as_of",
    "stock_id",
    *FACTOR_VALUE_COLUMNS,
    "source_snapshot_hash",
    "factor_status",
    "exclusion_reason",
)

COVERAGE_COLUMNS = (
    "as_of",
    "dimension",
    "sample_scope",
    "sample_start",
    "sample_end",
    "report_count",
    "broker_count",
    "analyst_count",
    "claim_count",
    "explicit_scorable_claim_count",
    "mature_outcome_count",
    "scored_outcome_count",
    "excluded_or_immature_count",
    "skill_snapshot_count",
    "rank_eligible_skill_count",
    "extractor_validation_passed",
    "extractor_validation_sample_count",
    "extractor_validation_field_precision",
    "coverage_status",
)

EXCEPTION_COLUMNS = (
    "exception_id",
    "severity",
    "stage",
    "code",
    "dimension",
    "entity_id",
    "report_id",
    "claim_id",
    "message",
    "details",
)


class ReportingError(ValueError):
    """Raised for invalid report bundle inputs."""


@dataclass(frozen=True)
class ReportBundle:
    """Paths and deterministic hashes for one completed report bundle."""

    output_directory: Path
    paths: Mapping[str, Path]
    hashes: Mapping[str, str]
    run_id: str


def _normalise(value: Any) -> Any:
    """Convert dataclasses and domain scalars to canonical JSON-safe values."""

    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _normalise(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _normalise(item) for key, item in value.items()}
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return _normalise(value.value)
    if isinstance(value, (list, tuple)):
        return [_normalise(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_normalise(item) for item in value]
        return sorted(items, key=lambda item: _canonical_json(item))
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "__dict__"):
        return _normalise(vars(value))
    return str(value)


def _record(value: Any) -> dict[str, Any]:
    normalised = _normalise(value)
    if not isinstance(normalised, dict):
        raise ReportingError(f"Expected record mapping, got {type(value).__name__}")
    return normalised


def _records(values: Iterable[Any] | None) -> list[dict[str, Any]]:
    return [_record(value) for value in (values or ())]


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _normalise(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _get(record: Mapping[str, Any] | None, *names: str, default: Any = None) -> Any:
    if record is None:
        return default
    for name in names:
        if name in record and record[name] is not None:
            return record[name]
    return default


def _as_float(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _as_bool(value: Any) -> bool | None:
    if type(value) is bool:
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y"}:
            return True
        if lowered in {"false", "0", "no", "n"}:
            return False
    return None


def _csv_cell(value: Any) -> Any:
    value = _normalise(value)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return _canonical_json(value)
    return value


def _write_csv(path: Path, columns: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(columns),
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({column: _csv_cell(row.get(column)) for column in columns})


def _write_text(path: Path, value: str) -> None:
    path.write_text(value.rstrip() + "\n", encoding="utf-8")


def _identity(record: Mapping[str, Any], *names: str) -> str:
    value = _get(record, *names, default="")
    return str(value or "")


def build_accuracy_rows(
    dimension: str,
    reports: Iterable[Any],
    claims: Iterable[Any],
    outcomes: Iterable[Any],
    *,
    formal_dimension_eligible: bool = True,
    minimum_extraction_confidence: float = 0.95,
) -> list[dict[str, Any]]:
    """Join claims to reports and outcomes without collapsing the three dimensions."""

    if dimension not in DIMENSIONS:
        raise ReportingError(f"Unknown dimension: {dimension}")
    report_rows = _records(reports)
    claim_rows = _records(claims)
    outcome_rows = _records(outcomes)
    report_by_id = {
        _identity(report, "report_id", "id"): report
        for report in report_rows
        if _identity(report, "report_id", "id")
    }
    # If an evaluator emitted more than one version, prefer its latest explicit
    # evaluated/truth timestamp.  This is deterministic and does not infer a hit.
    outcome_rows.sort(
        key=lambda row: (
            _identity(row, "claim_id"),
            _identity(row, "evaluated_at", "truth_available_at"),
            _canonical_json(row),
        )
    )
    outcome_by_claim = {
        _identity(outcome, "claim_id"): outcome
        for outcome in outcome_rows
        if _identity(outcome, "claim_id")
    }

    joined: list[dict[str, Any]] = []
    for claim in claim_rows:
        claim_dimension = _identity(claim, "dimension")
        if claim_dimension != dimension:
            continue
        report_id = _identity(claim, "report_id")
        claim_id = _identity(claim, "claim_id", "id")
        report = report_by_id.get(report_id, {})
        outcome = outcome_by_claim.get(claim_id)
        mature = _as_bool(_get(outcome, "mature")) if outcome else False
        outcome_hit = _as_bool(_get(outcome, "hit")) if outcome else None
        truth_source = _identity(outcome or {}, "truth_source")
        legacy_truth_eligible = _as_bool(
            _get(outcome, "truth_eligible", "official_truth_eligible")
        ) if outcome else None
        realized_value = _get(outcome, "realized_value") if outcome else None
        truth_source_key = truth_source.strip().lower()
        is_market_truth = (
            truth_source_key in {"market_bars", "market_return", "daily_bars", "price_bars"}
            or truth_source_key.startswith("market_")
            or truth_source_key.endswith("_market_bars")
        )
        fundamental_truth_eligible = _as_bool(
            _get(outcome, "fundamental_truth_eligible")
        ) if outcome else None
        if fundamental_truth_eligible is None:
            fundamental_truth_eligible = (
                legacy_truth_eligible
                if legacy_truth_eligible is not None
                else (bool(truth_source) and not is_market_truth)
            )
        market_truth_source = _identity(outcome or {}, "market_truth_source")
        market_exclusion_reason = _identity(outcome or {}, "market_exclusion_reason")
        market_truth_eligible = _as_bool(_get(outcome, "market_truth_eligible")) if outcome else None
        if market_truth_eligible is None:
            market_truth_eligible = (
                not bool(market_exclusion_reason)
                and (
                    is_market_truth
                    or bool(market_truth_source)
                    or _get(outcome, "market_hit") is not None
                    or _get(outcome, "market_return") is not None
                )
            ) if outcome else False
        fundamental_hit = _as_bool(_get(outcome, "fundamental_hit")) if outcome else None
        if fundamental_truth_eligible is not True:
            fundamental_hit = None
        if (
            fundamental_hit is None
            and truth_source
            and not is_market_truth
            and realized_value is not None
            and fundamental_truth_eligible is True
        ):
            # ClaimOutcome.hit is assigned to the truth family that produced it;
            # a price-bar hit must never masquerade as fundamental accuracy.
            fundamental_hit = outcome_hit

        market_return = _as_float(_get(outcome, "market_return")) if outcome else None
        benchmark_return = _as_float(_get(outcome, "benchmark_return")) if outcome else None
        market_excess = _as_float(_get(outcome, "market_excess_return")) if outcome else None
        if market_excess is None and (
            market_return is not None
            and benchmark_return is not None
            and benchmark_return > -1.0
        ):
            market_excess = (1.0 + market_return) / (1.0 + benchmark_return) - 1.0
        market_hit = _as_bool(_get(outcome, "market_hit")) if outcome else None
        if market_hit is None and is_market_truth and market_truth_eligible is True:
            market_hit = outcome_hit
        direction = _as_float(_get(claim, "direction"))
        if (
            market_hit is None
            and market_truth_eligible is True
            and market_excess is not None
            and direction in (-1.0, 1.0)
        ):
            market_hit = direction * market_excess > 0.0

        exclusion_reasons = [
            item
            for item in _identity(outcome or {}, "exclusion_reason").split("|")
            if item
        ]
        if outcome is None:
            exclusion_reasons.append("outcome_missing")
        elif mature is not True:
            exclusion_reasons.append("outcome_not_mature")

        confidence = _as_float(_get(claim, "extraction_confidence"))
        claim_contract_reasons: list[str] = []
        if confidence is None:
            claim_contract_reasons.append("extraction_confidence_missing")
        elif confidence < 0.0 or confidence > 1.0:
            claim_contract_reasons.append("extraction_confidence_invalid")
        elif confidence < minimum_extraction_confidence:
            claim_contract_reasons.append("extraction_confidence_below_threshold")
        if not _identity(claim, "target_type").strip():
            claim_contract_reasons.append("target_type_missing")
        if not _identity(claim, "forecast_period").strip():
            claim_contract_reasons.append("forecast_period_missing")
        horizon = _as_float(_get(claim, "horizon_days"))
        if horizon is None or horizon <= 0.0 or not horizon.is_integer():
            claim_contract_reasons.append("horizon_days_missing_or_invalid")
        claim_contract_eligible = not claim_contract_reasons

        fundamental_scored = fundamental_truth_eligible is True and fundamental_hit is not None
        market_scored = market_truth_eligible is True and market_hit is not None
        eligible = (
            mature is True
            and (fundamental_scored or market_scored)
            and formal_dimension_eligible
            and claim_contract_eligible
        )
        if fundamental_truth_eligible is False:
            exclusion_reasons.append("untrusted_fundamental_truth_source")
        exclusion_reasons.extend(claim_contract_reasons)
        if not formal_dimension_eligible:
            exclusion_reasons.append("extractor_validation_not_passed")
        if mature is True and not (fundamental_scored or market_scored):
            exclusion_reasons.append("mature_but_unscored")
        exclusion_reason = "|".join(dict.fromkeys(exclusion_reasons))

        joined.append(
            {
                "report_id": report_id,
                "claim_id": claim_id,
                "dimension": claim_dimension,
                "subject_id": _get(claim, "subject_id"),
                "subject_name": _get(report, "subject_name"),
                "target_type": _get(claim, "target_type"),
                "direction": _get(claim, "direction"),
                "value_min": _get(claim, "value_min"),
                "value_max": _get(claim, "value_max"),
                "unit": _get(claim, "unit"),
                "benchmark": _get(claim, "benchmark"),
                "forecast_period": _get(claim, "forecast_period"),
                "horizon_days": _get(claim, "horizon_days"),
                "available_at": _get(claim, "available_at"),
                "evidence_span": _get(claim, "evidence_span"),
                "extractor_version": _get(claim, "extractor_version"),
                "extraction_confidence": _get(claim, "extraction_confidence"),
                "broker": _get(report, "broker"),
                "analyst": _get(report, "analyst"),
                "team": _get(report, "team"),
                "report_title": _get(report, "title", "report_title"),
                "report_published_at": _get(report, "published_at", "report_date"),
                "truth_source": truth_source,
                "market_truth_source": market_truth_source,
                "truth_unit": _get(outcome, "truth_unit") if outcome else None,
                "truth_basis": _get(outcome, "truth_basis") if outcome else None,
                "truth_change_value": _get(outcome, "truth_change_value") if outcome else None,
                "truth_change_basis": _get(outcome, "truth_change_basis") if outcome else None,
                "market_benchmark_id": _get(outcome, "market_benchmark_id") if outcome else None,
                "market_benchmark_kind": _get(outcome, "market_benchmark_kind") if outcome else None,
                "truth_available_at": _get(outcome, "truth_available_at") if outcome else None,
                "realized_value": realized_value,
                "outcome_error": _get(outcome, "error") if outcome else None,
                "outcome_hit": outcome_hit,
                "fundamental_hit": fundamental_hit,
                "market_return": market_return,
                "benchmark_return": benchmark_return,
                "market_excess_return": market_excess,
                "market_hit": market_hit,
                "market_exclusion_reason": market_exclusion_reason,
                "fundamental_truth_eligible": fundamental_truth_eligible,
                "market_truth_eligible": market_truth_eligible,
                "mature": mature is True,
                "eligible_for_scoring": eligible,
                "exclusion_reason": exclusion_reason,
            }
        )
    joined.sort(
        key=lambda row: (
            str(row.get("available_at") or ""),
            str(row.get("report_id") or ""),
            str(row.get("claim_id") or ""),
        )
    )
    return joined


def build_skill_rows(
    snapshots: Iterable[Any],
    *,
    minimum_effective_sample_size: float,
) -> list[dict[str, Any]]:
    """Render skill snapshots and rank only evidence-eligible peer groups."""

    rows: list[dict[str, Any]] = []
    for snapshot in _records(snapshots):
        effective_n = _as_float(_get(snapshot, "effective_sample_size"))
        posterior = _as_float(_get(snapshot, "posterior_skill"))
        lower_bound = _as_float(_get(snapshot, "conservative_lower_bound"))
        source_ids = _get(snapshot, "source_report_ids", default=[])
        if isinstance(source_ids, str):
            source_ids = [item for item in source_ids.split("|") if item]
        source_ids = list(source_ids or [])
        eligible = (
            effective_n is not None
            and effective_n >= minimum_effective_sample_size
            and posterior is not None
            and lower_bound is not None
            and bool(source_ids)
        )
        reasons: list[str] = []
        if effective_n is None or effective_n < minimum_effective_sample_size:
            reasons.append("effective_sample_size_below_threshold")
        if posterior is None or lower_bound is None:
            reasons.append("skill_estimate_missing")
        if not source_ids:
            reasons.append("source_reports_missing")
        rows.append(
            {
                "snapshot_id": _get(snapshot, "snapshot_id"),
                "as_of": _get(snapshot, "as_of"),
                "broker": _get(snapshot, "broker"),
                "broker_display": _get(snapshot, "broker_display", default=_get(snapshot, "broker")),
                "analyst": _get(snapshot, "analyst"),
                "team": _get(snapshot, "team"),
                "dimension": _get(snapshot, "dimension"),
                "target_type": _get(snapshot, "target_type"),
                "horizon_days": _get(snapshot, "horizon_days"),
                "market_state": _get(snapshot, "market_state"),
                "industry_id": _get(snapshot, "industry_id"),
                "posterior_skill": posterior,
                "conservative_lower_bound": lower_bound,
                "effective_sample_size": effective_n,
                "sensitivity_365": _get(snapshot, "sensitivity_365"),
                "sensitivity_365_lower_bound": _get(snapshot, "sensitivity_365_lower_bound"),
                "sensitivity_365_effective_sample_size": _get(snapshot, "sensitivity_365_effective_sample_size"),
                "sensitivity_delta": _get(snapshot, "sensitivity_delta"),
                "source_report_ids": sorted(str(item) for item in source_ids),
                "rank_eligible": eligible,
                "rank": None,
                "skill_status": "eligible" if eligible else "coverage_only",
                "exclusion_reason": "|".join(reasons),
            }
        )

    peer_groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for row in rows:
        if not row["rank_eligible"]:
            continue
        key = tuple(
            str(row.get(name) or "")
            for name in ("dimension", "target_type", "horizon_days", "market_state", "industry_id")
        )
        peer_groups.setdefault(key, []).append(row)
    for peers in peer_groups.values():
        peers.sort(
            key=lambda row: (
                -float(row["conservative_lower_bound"]),
                -float(row["effective_sample_size"]),
                str(row.get("broker") or ""),
                str(row.get("analyst") or ""),
                str(row.get("team") or ""),
            )
        )
        previous_lower: float | None = None
        previous_rank = 0
        for index, row in enumerate(peers, start=1):
            current_lower = float(row["conservative_lower_bound"])
            if previous_lower is None or current_lower != previous_lower:
                previous_rank = index
                previous_lower = current_lower
            row["rank"] = previous_rank

    rows.sort(
        key=lambda row: (
            str(row.get("dimension") or ""),
            str(row.get("target_type") or ""),
            str(row.get("horizon_days") or ""),
            str(row.get("market_state") or ""),
            str(row.get("industry_id") or ""),
            0 if row.get("rank_eligible") else 1,
            int(row.get("rank") or 10**9),
            str(row.get("broker") or ""),
            str(row.get("analyst") or ""),
        )
    )
    return rows


def build_factor_rows(
    observations: Iterable[Any],
    *,
    walk_forward_result: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Render only factor observations actually produced by the analytics layer."""

    rows: list[dict[str, Any]] = []
    factor_names = FACTOR_VALUE_COLUMNS
    admitted_for_paper_research = (
        _admission_evidence(walk_forward_result)["status"]
        == "admitted_for_paper_research"
    )
    for observation in _records(observations):
        missing = [name for name in factor_names if _get(observation, name) is None]
        snapshot_hash = _identity(observation, "source_snapshot_hash")
        reasons = []
        if missing:
            reasons.append("missing:" + ",".join(missing))
        if not snapshot_hash:
            reasons.append("source_snapshot_hash_missing")
        complete = not reasons
        if not admitted_for_paper_research:
            reasons.append("walk_forward_not_admitted")
        if complete:
            factor_status = (
                "admitted_for_paper_research"
                if admitted_for_paper_research
                else "observation_complete_but_not_admitted"
            )
        else:
            factor_status = "incomplete_not_admitted"
        rows.append(
            {
                "as_of": _get(observation, "as_of"),
                "stock_id": _get(observation, "stock_id"),
                **{name: _get(observation, name) for name in factor_names},
                "source_snapshot_hash": snapshot_hash,
                "factor_status": factor_status,
                "exclusion_reason": "|".join(reasons),
            }
        )
    rows.sort(key=lambda row: (str(row.get("as_of") or ""), str(row.get("stock_id") or "")))
    return rows


def _exception(
    code: str,
    message: str,
    *,
    severity: str = "warning",
    stage: str = "reporting",
    dimension: str = "",
    entity_id: str = "",
    report_id: str = "",
    claim_id: str = "",
    details: Any = None,
) -> dict[str, Any]:
    natural_key = {
        "severity": severity,
        "stage": stage,
        "code": code,
        "dimension": dimension,
        "entity_id": entity_id,
        "report_id": report_id,
        "claim_id": claim_id,
        "message": message,
        "details": _normalise(details),
    }
    return {
        "exception_id": _sha256_text(_canonical_json(natural_key)),
        **natural_key,
    }


def build_exception_rows(
    supplied: Iterable[Any],
    *,
    reports: Sequence[Mapping[str, Any]],
    claims: Sequence[Mapping[str, Any]],
    accuracy: Mapping[str, Sequence[Mapping[str, Any]]],
    skill_rows: Sequence[Mapping[str, Any]],
    factor_rows: Sequence[Mapping[str, Any]],
    extraction_precision_threshold: float,
) -> list[dict[str, Any]]:
    """Combine explicit pipeline exceptions with deterministic fail-closed checks."""

    rows: list[dict[str, Any]] = []
    for item in _records(supplied):
        natural = {
            "severity": _identity(item, "severity") or "warning",
            "stage": _identity(item, "stage") or "pipeline",
            "code": _identity(item, "code") or "UNSPECIFIED",
            "dimension": _identity(item, "dimension"),
            "entity_id": _identity(item, "entity_id", "subject_id"),
            "report_id": _identity(item, "report_id"),
            "claim_id": _identity(item, "claim_id"),
            "message": _identity(item, "message") or "未提供异常说明",
            "details": _get(item, "details", default={}),
        }
        rows.append(
            {
                "exception_id": _identity(item, "exception_id")
                or _sha256_text(_canonical_json(natural)),
                **natural,
            }
        )

    reports_by_dimension = {
        dimension: [row for row in reports if _identity(row, "dimension") == dimension]
        for dimension in DIMENSIONS
    }
    for dimension in DIMENSIONS:
        if not reports_by_dimension[dimension]:
            rows.append(
                _exception(
                    "NO_REPORTS",
                    "该维度没有可用研报；不生成准确率或来源排名。",
                    stage="coverage",
                    dimension=dimension,
                )
            )
        if not accuracy[dimension]:
            rows.append(
                _exception(
                    "NO_EXTRACTED_CLAIMS",
                    "该维度没有可证伪预测；空泛观点不进入评分。",
                    stage="extraction",
                    dimension=dimension,
                )
            )
        elif not any(row.get("eligible_for_scoring") for row in accuracy[dimension]):
            rows.append(
                _exception(
                    "NO_MATURE_SCORED_OUTCOMES",
                    "该维度没有成熟且可评分的真值结果；不生成能力排名。",
                    stage="evaluation",
                    dimension=dimension,
                )
            )

    for claim in claims:
        confidence = _as_float(_get(claim, "extraction_confidence"))
        if confidence is not None and confidence < extraction_precision_threshold:
            rows.append(
                _exception(
                    "LOW_EXTRACTION_CONFIDENCE",
                    "规则抽取置信度低于正式评分阈值。",
                    stage="extraction",
                    dimension=_identity(claim, "dimension"),
                    report_id=_identity(claim, "report_id"),
                    claim_id=_identity(claim, "claim_id", "id"),
                    details={"confidence": confidence, "threshold": extraction_precision_threshold},
                )
            )

    for dimension, accuracy_rows in accuracy.items():
        for row in accuracy_rows:
            reason = str(row.get("exclusion_reason") or "")
            if reason:
                reason_tokens = set(reason.split("|"))
                code = (
                    "OUTCOME_NOT_MATURE"
                    if "outcome_not_mature" in reason_tokens
                    else "OUTCOME_EXCLUDED"
                )
                if "outcome_missing" in reason_tokens:
                    code = "OUTCOME_MISSING"
                rows.append(
                    _exception(
                        code,
                        "预测尚未形成可用于能力估计的成熟结果。",
                        stage="evaluation",
                        dimension=dimension,
                        report_id=str(row.get("report_id") or ""),
                        claim_id=str(row.get("claim_id") or ""),
                        details={"reason": reason},
                    )
                )

    if not skill_rows:
        rows.append(
            _exception(
                "NO_SKILL_SNAPSHOTS",
                "没有经成熟历史结果估计的技能快照；券商能力表仅保留表头。",
                stage="skill",
            )
        )
    elif not any(row.get("rank_eligible") for row in skill_rows):
        rows.append(
            _exception(
                "NO_RANK_ELIGIBLE_SKILLS",
                "技能快照有效样本不足；不生成名次。",
                stage="skill",
            )
        )
    if not factor_rows:
        rows.append(
            _exception(
                "NO_FACTOR_OBSERVATIONS",
                "没有可审计的三层因子观测；因子表仅保留表头。",
                stage="factor",
            )
        )

    unique = {str(row["exception_id"]): row for row in rows}
    return sorted(
        unique.values(),
        key=lambda row: (
            str(row.get("severity") or ""),
            str(row.get("stage") or ""),
            str(row.get("code") or ""),
            str(row.get("dimension") or ""),
            str(row.get("report_id") or ""),
            str(row.get("claim_id") or ""),
        ),
    )


def build_coverage_rows(
    *,
    as_of: str,
    reports: Sequence[Mapping[str, Any]],
    claims: Sequence[Mapping[str, Any]],
    accuracy: Mapping[str, Sequence[Mapping[str, Any]]],
    skill_rows: Sequence[Mapping[str, Any]],
    extractor_validation: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build honest coverage counts, including zero-count dimensions."""

    rows: list[dict[str, Any]] = []
    for dimension in DIMENSIONS:
        dimension_reports = [row for row in reports if _identity(row, "dimension") == dimension]
        dimension_claims = [row for row in claims if _identity(row, "dimension") == dimension]
        dimension_accuracy = list(accuracy[dimension])
        dimension_skills = [row for row in skill_rows if _identity(row, "dimension") == dimension]
        dates = sorted(
            str(_get(row, "published_at", "report_date") or "")[:10]
            for row in dimension_reports
            if _get(row, "published_at", "report_date")
        )
        explicit = sum(
            1
            for row in dimension_claims
            if _get(row, "target_type")
            and _get(row, "forecast_period")
            and _as_float(_get(row, "horizon_days")) not in (None, 0.0)
        )
        mature = sum(1 for row in dimension_accuracy if row.get("mature") is True)
        scored = sum(1 for row in dimension_accuracy if row.get("eligible_for_scoring") is True)
        coverage_status = "scored" if scored else ("claims_only" if dimension_claims else ("reports_only" if dimension_reports else "empty"))
        validation = _get(extractor_validation or {}, dimension, default={})
        if not isinstance(validation, Mapping):
            validation = {}
        rows.append(
            {
                "as_of": as_of,
                "dimension": dimension,
                "sample_scope": SCOPE_NOTICE.rstrip("。"),
                "sample_start": dates[0] if dates else "",
                "sample_end": dates[-1] if dates else "",
                "report_count": len(dimension_reports),
                "broker_count": len({_identity(row, "broker") for row in dimension_reports if _identity(row, "broker")}),
                "analyst_count": len({_identity(row, "analyst") for row in dimension_reports if _identity(row, "analyst")}),
                "claim_count": len(dimension_claims),
                "explicit_scorable_claim_count": explicit,
                "mature_outcome_count": mature,
                "scored_outcome_count": scored,
                "excluded_or_immature_count": max(0, len(dimension_accuracy) - scored),
                "skill_snapshot_count": len(dimension_skills),
                "rank_eligible_skill_count": sum(1 for row in dimension_skills if row.get("rank_eligible") is True),
                "extractor_validation_passed": _as_bool(_get(validation, "passed")) is True,
                "extractor_validation_sample_count": _get(validation, "sample_count"),
                "extractor_validation_field_precision": _get(validation, "field_precision"),
                "coverage_status": coverage_status,
            }
        )
    return rows


def _md(value: Any) -> str:
    text = str(value if value not in (None, "") else "-")
    return text.replace("|", "\\|").replace("\n", " ")


def _admission_evidence(result: Mapping[str, Any] | None) -> dict[str, Any]:
    if not result:
        return {
            "status": "not_evaluated",
            "checks": {
                "at_least_four_oos_windows": False,
                "positive_mean_rank_ic": False,
                "at_least_three_positive_incremental_windows": False,
                "costs_included": False,
                "not_single_industry_dominated": False,
            },
            "windows": [],
            "reason": "没有可验证的 Walk-forward 结果，M1 不进入模拟研究层。",
        }
    admission_raw = _get(result, "admission", default={})
    admission = _record(admission_raw) if isinstance(admission_raw, Mapping) else {}
    windows_raw = _get(result, "windows", "out_of_sample_windows", default=[])
    windows = list(windows_raw) if isinstance(windows_raw, (list, tuple)) else []
    mean_rank_ic = _as_float(
        _get(
            admission,
            "mean_m1_rank_ic",
            "mean_rank_ic",
            default=_get(result, "mean_rank_ic", "m1_mean_rank_ic"),
        )
    )
    if mean_rank_ic is None:
        window_ics: list[float] = []
        for window in windows:
            window_row = _record(window)
            m1 = _get(window_row, "M1")
            metric_row = _record(m1) if isinstance(m1, Mapping) else window_row
            value = _as_float(_get(metric_row, "rank_ic", "m1_rank_ic"))
            if value is not None:
                window_ics.append(value)
        mean_rank_ic = sum(window_ics) / len(window_ics) if window_ics else None
    positive_incremental = _as_float(
        _get(
            admission,
            "incremental_window_count",
            default=_get(result, "positive_incremental_windows", "m1_positive_incremental_windows"),
        )
    )
    if positive_incremental is None:
        incremental_count = 0
        for window in windows:
            window_row = _record(window)
            m1 = _get(window_row, "M1")
            if isinstance(m1, Mapping):
                m1_ic = _as_float(_get(m1, "rank_ic"))
                baseline_ics = [
                    _as_float(_get(_record(_get(window_row, model, default={})), "rank_ic"))
                    for model in ("B0", "B1", "B2")
                ]
                valid_baselines = [value for value in baseline_ics if value is not None]
                if m1_ic is not None and valid_baselines and m1_ic > max(valid_baselines):
                    incremental_count += 1
            elif (_as_float(_get(window_row, "incremental_return", "m1_incremental")) or 0.0) > 0.0:
                incremental_count += 1
        positive_incremental = float(incremental_count)
    evaluated_m1: list[dict[str, Any]] = []
    for window in windows:
        window_row = _record(window)
        m1 = _get(window_row, "M1")
        if isinstance(m1, Mapping) and _get(m1, "status", default="evaluated") == "evaluated":
            evaluated_m1.append(_record(m1))
    explicit_cost_flag = _as_bool(_get(result, "costs_included", "cost_adjusted"))
    costs_included = (
        explicit_cost_flag is True
        or bool(evaluated_m1)
        and all("cost_after_group_return" in row for row in evaluated_m1)
    )
    dominated = _as_bool(_get(result, "single_industry_dominated", "dominated_by_single_industry"))
    max_concentration = _as_float(
        _get(admission, "max_industry_contribution_share", default=_get(result, "max_industry_contribution_share"))
    )
    not_dominated = dominated is False or (
        max_concentration is not None and max_concentration <= 0.50
    )
    verified_window_count = int(
        _as_float(_get(admission, "window_count"))
        or len(evaluated_m1)
        or len(windows)
    )
    checks = {
        "at_least_four_oos_windows": verified_window_count >= 4,
        "positive_mean_rank_ic": mean_rank_ic is not None and mean_rank_ic > 0.0,
        "at_least_three_positive_incremental_windows": positive_incremental >= 3.0,
        "costs_included": costs_included,
        "not_single_industry_dominated": not_dominated,
    }
    explicit_admitted = _as_bool(
        _get(
            admission,
            "admitted",
            default=_get(result, "admitted", "eligible_for_paper_research"),
        )
    ) is True
    # A public writer must never turn caller-supplied performance numbers into
    # formal admission. Only a controlled, provenance-verifying evaluator may
    # set this evidence flag; current V1 intentionally leaves it false.
    evidence_verified = _as_bool(_get(admission, "evidence_verified")) is True
    checks["admission_evidence_verified"] = evidence_verified
    # V1 has no store-backed verifier capable of minting this attestation yet.
    # Keep the public renderer incapable of admitting M1, even if a caller
    # fabricates both booleans and attractive performance numbers.
    admitted = False
    return {
        "status": "admitted_for_paper_research" if admitted else "not_admitted",
        "checks": checks,
        "windows": [_record(window) for window in windows],
        "mean_rank_ic": mean_rank_ic,
        "positive_incremental_windows": int(positive_incremental),
        "frozen_test": _get(result, "frozen_test", default={}),
        "reason": (
            "全部预注册门槛通过，仅获准进入模拟研究层；不代表可实盘交易。"
            if admitted
            else "至少一个预注册门槛或显式准入证据缺失，M1 不进入模拟研究层。"
        ),
    }


def render_walk_forward_report(as_of: str, result: Mapping[str, Any] | None) -> str:
    evidence = _admission_evidence(result)
    lines = [
        f"# 三层因子 Walk-forward 报告 - {as_of}",
        "",
        f"- 状态：`{evidence['status']}`",
        f"- 结论：{evidence['reason']}",
        f"- 样本边界：{SCOPE_NOTICE}",
        f"- 使用边界：{RESEARCH_NOTICE}",
        "",
        "## 预注册准入检查",
        "",
        "| 检查 | 通过 |",
        "|---|---|",
    ]
    labels = {
        "at_least_four_oos_windows": "至少 4 个样本外窗口",
        "positive_mean_rank_ic": "M1 平均 Rank IC 为正",
        "at_least_three_positive_incremental_windows": "至少 3 个窗口增量为正",
        "costs_included": "已计入交易成本",
        "not_single_industry_dominated": "收益不由单一行业贡献",
        "admission_evidence_verified": "原始标签与输入证据已由受控流程验证",
    }
    for key, passed in evidence["checks"].items():
        lines.append(f"| {labels[key]} | {'是' if passed else '否'} |")
    lines.extend(
        [
            "",
            "## 样本外窗口",
            "",
            "| 窗口 | 模型 | Rank IC | 增量 | 成本后收益 | 最大行业贡献 |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    if evidence["windows"]:
        for index, window in enumerate(evidence["windows"], start=1):
            window_row = _record(window)
            nested_models = [model for model in ("B0", "B1", "B2", "M1") if isinstance(_get(window_row, model), Mapping)]
            if nested_models:
                baseline_ics = [
                    value
                    for value in (
                        _as_float(_get(_record(_get(window_row, model)), "rank_ic"))
                        for model in ("B0", "B1", "B2")
                        if isinstance(_get(window_row, model), Mapping)
                    )
                    if value is not None
                ]
                for model in nested_models:
                    metrics = _record(_get(window_row, model))
                    rank_ic = _as_float(_get(metrics, "rank_ic"))
                    incremental = (
                        rank_ic - max(baseline_ics)
                        if model == "M1" and rank_ic is not None and baseline_ics
                        else None
                    )
                    lines.append(
                        "| {window} | {model} | {rank_ic} | {incremental} | {cost_return} | {industry} |".format(
                            window=_md(_get(window_row, "window_id", "window", default=index)),
                            model=model,
                            rank_ic=_md(rank_ic),
                            incremental=_md(incremental),
                            cost_return=_md(_get(metrics, "cost_after_group_return", "cost_adjusted_return", "net_return")),
                            industry=_md(_get(metrics, "max_industry_contribution_share", "largest_industry_contribution")),
                        )
                    )
            else:
                lines.append(
                    "| {window} | {model} | {rank_ic} | {incremental} | {cost_return} | {industry} |".format(
                        window=_md(_get(window_row, "window_id", "window", default=index)),
                        model=_md(_get(window_row, "model", default="M1")),
                        rank_ic=_md(_get(window_row, "rank_ic", "m1_rank_ic")),
                        incremental=_md(_get(window_row, "incremental_return", "m1_incremental")),
                        cost_return=_md(_get(window_row, "cost_adjusted_return", "net_return")),
                        industry=_md(_get(window_row, "largest_industry_contribution")),
                    )
                )
        frozen = evidence.get("frozen_test")
        if isinstance(frozen, Mapping):
            frozen_row = _record(frozen)
            for model in ("B0", "B1", "B2", "M1"):
                metrics = _get(frozen_row, model)
                if not isinstance(metrics, Mapping):
                    continue
                metric_row = _record(metrics)
                lines.append(
                    f"| frozen_12m | {model} | {_md(_get(metric_row, 'rank_ic'))} | - | "
                    f"{_md(_get(metric_row, 'cost_after_group_return'))} | "
                    f"{_md(_get(metric_row, 'max_industry_contribution_share'))} |"
                )
    else:
        lines.append("| - | - | - | - | - | - |")
        lines.extend(["", "> 无结果不是零收益，而是尚未完成可验证评估。"])
    auxiliary = _get(result or {}, "auxiliary_60d", default={})
    lines.extend(["", "## 60 日辅助目标", ""])
    if isinstance(auxiliary, Mapping) and auxiliary:
        auxiliary_row = _record(auxiliary)
        auxiliary_admission = _get(auxiliary_row, "admission", default={})
        auxiliary_status = _get(
            auxiliary_admission if isinstance(auxiliary_admission, Mapping) else {},
            "status",
            default=_get(auxiliary_row, "status", default="not_evaluated"),
        )
        lines.append(f"- 状态：`{_md(auxiliary_status)}`")
        lines.append(f"- 窗口数：{_md(len(_get(auxiliary_row, 'windows', default=[]) or []))}")
        lines.append("- 说明：60 日结果仅作辅助，不得替代 20 日主目标准入。")
    else:
        lines.append("- 状态：`not_evaluated`；不影响 20 日主目标的独立准入判断。")
    return "\n".join(lines)


def _deep_read_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    def descending(name: str, *aliases: str) -> float:
        value = _as_float(_get(row, name, *aliases))
        return -(value if value is not None else -math.inf)

    explicit_key = _get(row, "priority_key")
    if isinstance(explicit_key, (list, tuple)):
        numeric = []
        for value in explicit_key[:6]:
            converted = _as_float(value)
            numeric.append(-(converted if converted is not None else -math.inf))
        numeric.extend([math.inf] * (6 - len(numeric)))
        return (*numeric, _identity(row, "report_id", "id"))
    return (
        descending("decision_sensitivity"),
        descending("conflict_degree", "conflict_score"),
        descending("change_degree", "change_score"),
        descending("source_skill_lower_bound", "conservative_lower_bound"),
        descending("falsifiability"),
        descending("evidence_completeness"),
        _identity(row, "report_id", "id"),
    )


def select_deep_reads(
    candidates: Iterable[Any],
    reports: Iterable[Any],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Apply the frozen lexicographic priority and evidence-completeness gate."""

    report_by_id = {
        _identity(row, "report_id", "id"): row
        for row in _records(reports)
        if _identity(row, "report_id", "id")
    }
    selected: list[dict[str, Any]] = []
    candidate_rows = _records(candidates)
    candidate_rows.sort(key=_deep_read_sort_key)
    for candidate in candidate_rows:
        report_id = _identity(candidate, "report_id", "id")
        report = report_by_id.get(report_id, {})
        if _as_bool(_get(candidate, "eligible")) is False:
            continue
        if _as_bool(_get(candidate, "is_redundant_consensus")) is True:
            continue
        why = _identity(candidate, "why_read", "reason", "selection_reason")
        might_change = _identity(
            candidate,
            "might_change",
            "judgment_affected",
            "decision_that_may_change",
        )
        if not report_id or not why or not might_change:
            continue
        selected.append(
            {
                "rank": len(selected) + 1,
                "report_id": report_id,
                "dimension": _get(candidate, "dimension", default=_get(report, "dimension")),
                "broker": _get(candidate, "broker", default=_get(report, "broker")),
                "analyst": _get(candidate, "analyst", default=_get(report, "analyst")),
                "title": _get(candidate, "title", default=_get(report, "title")),
                "source_url": _get(candidate, "source_url", default=_get(report, "source_url", "pdf_url")),
                "why_read": why,
                "might_change": might_change,
                "priority_evidence": {
                    key: _get(candidate, key)
                    for key in (
                        "decision_sensitivity",
                        "conflict_degree",
                        "change_degree",
                        "source_skill_lower_bound",
                        "falsifiability",
                        "evidence_completeness",
                    )
                    if _get(candidate, key) is not None
                },
            }
        )
        if len(selected) >= limit:
            break
    return selected


def render_deep_read_queue(as_of: str, rows: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        f"# 当前研报深读清单 - {as_of}",
        "",
        f"- 样本边界：{SCOPE_NOTICE}",
        "- 排序：决策敏感度、冲突、观点变化、来源技能下界、可证伪性、证据完整性依次比较；不合成总分。",
        f"- 使用边界：{RESEARCH_NOTICE}",
        "",
    ]
    if not rows:
        lines.extend(
            [
                "## 暂无合格研报",
                "",
                "没有同时具备原文证据、明确入选原因和可改变判断说明的候选；不以标题或缺失数据生成虚假优先级。",
            ]
        )
        return "\n".join(lines)
    lines.extend(
        [
            "| 顺序 | 维度 | 券商/分析师 | 报告 | 为什么值得读 | 可能改变的判断 |",
            "|---:|---|---|---|---|---|",
        ]
    )
    for row in rows:
        title = _md(row.get("title"))
        source_url = str(row.get("source_url") or "").strip()
        if source_url.startswith(("https://", "http://")):
            title = f"[{title}]({source_url})"
        source = "/".join(
            part for part in (_md(row.get("broker")), _md(row.get("analyst"))) if part != "-"
        ) or "-"
        lines.append(
            f"| {_md(row.get('rank'))} | {_md(row.get('dimension'))} | {source} | {title} | "
            f"{_md(row.get('why_read'))} | {_md(row.get('might_change'))} |"
        )
    return "\n".join(lines)


def render_dashboard(
    as_of: str,
    dashboard: Mapping[str, Any] | None,
    deep_reads: Sequence[Mapping[str, Any]],
) -> str:
    """Render three layers separately; absent evidence remains unknown."""

    data = _record(dashboard) if dashboard else {}
    macro = _get(data, "macro_environment", "macro_state") or "未知（证据不足）"
    industries = _get(data, "industry_opportunities", "industries", default=[])
    stocks = _get(data, "stock_rankings", "stocks", default=[])
    conflicts = _get(data, "conflicts", "three_layer_conflicts", default=[])
    if not isinstance(industries, list):
        industries = []
    if not isinstance(stocks, list):
        stocks = []
    if not isinstance(conflicts, list):
        conflicts = []

    lines = [
        f"# 宏观—行业—个股三层驾驶舱 - {as_of}",
        "",
        f"- 数据范围：{SCOPE_NOTICE}",
        f"- 使用边界：{RESEARCH_NOTICE}",
        "- 三层分别展示，不使用固定 30%/30%/40% 总分。",
        "",
        "## 宏观环境",
        "",
        f"**{_md(macro)}**",
        "",
        "## 行业机会",
        "",
        "| 行业 | 状态 | 客观证据 | 研报证据 |",
        "|---|---|---|---|",
    ]
    if industries:
        for item in industries:
            row = _record(item)
            lines.append(
                f"| {_md(_get(row, 'industry_name', 'industry_id'))} | "
                f"{_md(_get(row, 'state', 'opportunity'))} | "
                f"{_md(_get(row, 'objective_evidence'))} | {_md(_get(row, 'report_evidence'))} |"
            )
    else:
        lines.append("| - | 未知（证据不足） | - | - |")
    lines.extend(
        [
            "",
            "## 行业内个股",
            "",
            "| 行业 | 个股 | 相对排名 | 证据状态 |",
            "|---|---|---:|---|",
        ]
    )
    if stocks:
        for item in stocks:
            row = _record(item)
            lines.append(
                f"| {_md(_get(row, 'industry_name', 'industry_id'))} | "
                f"{_md(_get(row, 'stock_name', 'stock_id'))} | {_md(_get(row, 'rank'))} | "
                f"{_md(_get(row, 'evidence_status', 'status'))} |"
            )
    else:
        lines.append("| - | - | - | 未知（证据不足） |")
    lines.extend(["", "## 三层冲突", ""])
    if conflicts:
        for item in conflicts:
            if isinstance(item, Mapping):
                row = _record(item)
                lines.append(
                    f"- {_md(_get(row, 'subject', 'stock_id', 'industry_id'))}："
                    f"{_md(_get(row, 'message', 'conflict'))}；需核对："
                    f"{_md(_get(row, 'evidence_to_check', 'evidence'))}"
                )
            else:
                lines.append(f"- {_md(item)}")
    else:
        lines.append("- 暂无可验证冲突；这表示证据不足或未发现冲突，不表示三层一致。")
    lines.extend(["", "## 深读清单", ""])
    if deep_reads:
        for row in deep_reads:
            lines.append(
                f"- {_md(row.get('rank'))}. {_md(row.get('title'))}：{_md(row.get('why_read'))}"
            )
        lines.append("")
        lines.append("完整说明见 `deep_read_queue.md`。")
    else:
        lines.append("- 暂无满足证据门槛的深读候选。")
    return "\n".join(lines)


def build_dashboard_data(records: Iterable[Any]) -> dict[str, Any]:
    """Collect only explicit dashboard labels; never infer states from factors.

    Callers may pass factor specifications, research rows, or enriched deep-read
    records.  A label is shown only when the input names it.  Conflicting macro
    labels become an explicit conflict rather than an averaged state.
    """

    rows = _records(records)
    macro_states = sorted(
        {
            str(_get(row, "macro_environment", "macro_state")).strip()
            for row in rows
            if str(_get(row, "macro_environment", "macro_state", default="")).strip()
        }
    )
    conflicts: list[Any] = []
    if len(macro_states) == 1:
        macro_environment = macro_states[0]
    else:
        macro_environment = "未知（证据不足）"
        if len(macro_states) > 1:
            conflicts.append(
                {
                    "subject": "宏观环境",
                    "message": "输入包含互相冲突的显式宏观状态",
                    "evidence_to_check": "、".join(macro_states),
                }
            )

    industries_by_key: dict[str, dict[str, Any]] = {}
    stocks_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        industry_id = str(_get(row, "industry_id", default="") or "").strip()
        industry_name = str(_get(row, "industry_name", default="") or "").strip()
        industry_state = str(
            _get(row, "industry_state", "industry_opportunity", default="") or ""
        ).strip()
        if (industry_id or industry_name) and industry_state:
            key = industry_id or industry_name
            candidate = {
                "industry_id": industry_id,
                "industry_name": industry_name or industry_id,
                "state": industry_state,
                "objective_evidence": _get(row, "industry_objective_evidence", "objective_evidence"),
                "report_evidence": _get(row, "industry_report_evidence", "report_evidence"),
            }
            previous = industries_by_key.get(key)
            if previous is not None and previous["state"] != candidate["state"]:
                conflicts.append(
                    {
                        "subject": candidate["industry_name"],
                        "message": "输入包含互相冲突的显式行业状态",
                        "evidence_to_check": f"{previous['state']} / {candidate['state']}",
                    }
                )
                industries_by_key.pop(key, None)
            elif key not in industries_by_key:
                industries_by_key[key] = candidate

        stock_id = str(_get(row, "stock_id", "subject_id", default="") or "").strip()
        explicit_rank = _get(row, "stock_rank", "relative_rank")
        if stock_id and explicit_rank not in (None, ""):
            stock_key = (industry_id, stock_id)
            stocks_by_key[stock_key] = {
                "industry_id": industry_id,
                "industry_name": industry_name or industry_id,
                "stock_id": stock_id,
                "stock_name": _get(row, "stock_name", "subject_name", default=stock_id),
                "rank": explicit_rank,
                "evidence_status": _get(row, "stock_evidence_status", "evidence_status", "status"),
            }

        explicit_conflicts = _get(row, "three_layer_conflicts", "conflicts", default=[])
        if isinstance(explicit_conflicts, list):
            conflicts.extend(explicit_conflicts)
        elif isinstance(explicit_conflicts, str) and explicit_conflicts.strip():
            conflicts.append(explicit_conflicts.strip())

    industries = [industries_by_key[key] for key in sorted(industries_by_key)]
    stocks = sorted(
        stocks_by_key.values(),
        key=lambda row: (
            str(row.get("industry_id") or ""),
            str(row.get("rank") or ""),
            str(row.get("stock_id") or ""),
        ),
    )
    # Canonical JSON removes exact duplicate explicit conflicts without
    # reinterpreting their meaning.
    conflict_map = {_canonical_json(item): item for item in conflicts}
    return {
        "macro_environment": macro_environment,
        "industry_opportunities": industries,
        "stock_rankings": stocks,
        "conflicts": [conflict_map[key] for key in sorted(conflict_map)],
    }


def write_report_bundle(
    output_directory: Path | str,
    *,
    as_of: str,
    command: str,
    config: Mapping[str, Any],
    reports: Iterable[Any] = (),
    claims: Iterable[Any] = (),
    outcomes: Iterable[Any] = (),
    skill_snapshots: Iterable[Any] = (),
    factor_observations: Iterable[Any] = (),
    walk_forward_result: Mapping[str, Any] | None = None,
    dashboard: Mapping[str, Any] | None = None,
    deep_read_candidates: Iterable[Any] = (),
    exceptions: Iterable[Any] = (),
    parameters: Mapping[str, Any] | None = None,
    additional_input_snapshot: Mapping[str, Any] | None = None,
    report_source: Mapping[str, Any] | None = None,
    market_data_batches: Iterable[Mapping[str, Any]] = (),
) -> ReportBundle:
    """Write the complete deterministic fixed artifact set.

    The function always writes all eleven artifacts.  Empty inputs yield
    explicit coverage and exception records rather than placeholder rankings.
    """

    output_path = Path(output_directory)
    repository_commit, working_tree_dirty, git_diff_sha256 = git_worktree_state(
        REPOSITORY_ROOT
    )
    if working_tree_dirty is None:
        raise ReportingError(
            "Git working-tree state is unavailable; formal report generation is refused"
        )
    if working_tree_dirty and git_diff_sha256 is None:
        raise ReportingError(
            "dirty working tree cannot generate a formal report without git_diff_sha256"
        )
    output_path.mkdir(parents=True, exist_ok=True)
    report_rows = _records(reports)
    claim_rows = _records(claims)
    outcome_rows = _records(outcomes)
    snapshot_rows = _records(skill_snapshots)
    factor_input_rows = _records(factor_observations)
    supplied_exceptions = _records(exceptions)
    deep_read_input_rows = _records(deep_read_candidates)
    resolved_report_source = _normalise(report_source or {})
    resolved_market_batches = sorted(
        (
            _normalise(
                {
                    str(key): value
                    for key, value in dict(batch).items()
                    if str(key) != "records"
                }
            )
            for batch in market_data_batches
        ),
        key=_canonical_json,
    )

    skill_config = _get(config, "skill", default={})
    acceptance_config = _get(config, "acceptance", default={})
    deep_read_config = _get(config, "deep_read", default={})
    minimum_n = _as_float(_get(skill_config, "minimum_effective_sample_size_for_ranking")) or 5.0
    extraction_threshold = _as_float(_get(acceptance_config, "minimum_extraction_precision")) or 0.95
    maximum_deep_reads = int(_as_float(_get(deep_read_config, "maximum_limit")) or 20)
    requested_limit = int(_as_float(_get(parameters or {}, "limit")) or maximum_deep_reads)
    resolved_limit = max(0, min(maximum_deep_reads, requested_limit))

    extractor_validation = _get(acceptance_config, "extractor_validation", default={})

    def sha256_text(value: Any) -> bool:
        text = str(value or "").strip().lower()
        return len(text) == 64 and all(character in "0123456789abcdef" for character in text)

    def dimension_admitted(dimension: str) -> bool:
        if not isinstance(extractor_validation, Mapping) or dimension not in extractor_validation:
            return False
        validation = _get(extractor_validation, dimension, default={})
        if not isinstance(validation, Mapping):
            return False
        sample_count = _as_float(_get(validation, "sample_count")) or 0.0
        precision = _as_float(_get(validation, "field_precision"))
        metadata_match = _as_float(_get(validation, "metadata_match_rate"))
        return (
            _as_bool(_get(validation, "passed")) is True
            and sample_count >= 30.0
            and precision is not None
            and precision >= extraction_threshold
            and metadata_match is not None
            and metadata_match >= 1.0
            and _get(validation, "validation_contract_version")
            == "broker-report-extractor-validation.v3"
            and sha256_text(_get(validation, "manifest_sha256"))
            and sha256_text(_get(validation, "extractor_bundle_sha256"))
            and bool(str(_get(validation, "extractor_version") or "").strip())
            and bool(str(_get(validation, "parser_version") or "").strip())
            and bool(str(_get(validation, "prompt_version") or "").strip())
        )

    accuracy = {
        dimension: build_accuracy_rows(
            dimension,
            report_rows,
            claim_rows,
            outcome_rows,
            formal_dimension_eligible=dimension_admitted(dimension),
            minimum_extraction_confidence=extraction_threshold,
        )
        for dimension in DIMENSIONS
    }
    skill_rows = build_skill_rows(snapshot_rows, minimum_effective_sample_size=minimum_n)
    factor_rows = build_factor_rows(
        factor_input_rows,
        walk_forward_result=walk_forward_result,
    )
    deep_read_rows = select_deep_reads(
        deep_read_input_rows,
        report_rows,
        limit=resolved_limit,
    )
    coverage_rows = build_coverage_rows(
        as_of=as_of,
        reports=report_rows,
        claims=claim_rows,
        accuracy=accuracy,
        skill_rows=skill_rows,
        extractor_validation=extractor_validation if isinstance(extractor_validation, Mapping) else {},
    )
    exception_rows = build_exception_rows(
        supplied_exceptions,
        reports=report_rows,
        claims=claim_rows,
        accuracy=accuracy,
        skill_rows=skill_rows,
        factor_rows=factor_rows,
        extraction_precision_threshold=extraction_threshold,
    )

    paths = {name: output_path / name for name in ARTIFACT_FILENAMES}
    for dimension in DIMENSIONS:
        _write_csv(paths[f"{dimension}_accuracy.csv"], ACCURACY_COLUMNS, accuracy[dimension])
    _write_csv(paths["broker_skill_cube.csv"], SKILL_COLUMNS, skill_rows)
    _write_csv(paths["three_layer_factor.csv"], FACTOR_COLUMNS, factor_rows)
    _write_text(paths["factor_walk_forward_report.md"], render_walk_forward_report(as_of, walk_forward_result))
    _write_text(paths["three_layer_dashboard.md"], render_dashboard(as_of, dashboard, deep_read_rows))
    _write_text(paths["deep_read_queue.md"], render_deep_read_queue(as_of, deep_read_rows))
    _write_csv(paths["source_coverage.csv"], COVERAGE_COLUMNS, coverage_rows)
    _write_csv(paths["exceptions.csv"], EXCEPTION_COLUMNS, exception_rows)

    output_hashes = {
        name: _sha256_file(path)
        for name, path in paths.items()
        if name != "run_manifest.json"
    }
    input_snapshot = {
        "reports": report_rows,
        "claims": claim_rows,
        "outcomes": outcome_rows,
        "skill_snapshots": snapshot_rows,
        "factor_observations": factor_input_rows,
        "walk_forward_result": walk_forward_result,
        "dashboard": dashboard,
        "deep_read_candidates": deep_read_input_rows,
        "exceptions": supplied_exceptions,
        "additional_inputs": additional_input_snapshot or {},
        "source_evidence": {
            "report_source": resolved_report_source,
            "market_data_batches": resolved_market_batches,
        },
    }
    config_hash = _sha256_text(_canonical_json(config))
    source_snapshot_hash = _sha256_text(_canonical_json(input_snapshot))
    stable_parameters = {
        str(key): _normalise(value)
        for key, value in (parameters or {}).items()
        if str(key) not in {"db", "cache_dir", "output_dir", "config"}
    }
    run_identity = {
        "schema_version": _get(config, "schema_version", default="1.0"),
        "model_id": _get(config, "model_id", default="broker-report-audit-v1"),
        "command": command,
        "as_of": as_of,
        "config_sha256": config_hash,
        "source_snapshot_sha256": source_snapshot_hash,
        "repository_commit": repository_commit,
        "working_tree_dirty_at_generation": working_tree_dirty,
        "git_diff_sha256": git_diff_sha256,
        "parameters": stable_parameters,
        "output_sha256": output_hashes,
    }
    run_id = _sha256_text(_canonical_json(run_identity))
    manifest = {
        **run_identity,
        "run_id": run_id,
        "research_cutoff": f"{as_of}T23:59:59+08:00",
        "sample_scope": SCOPE_NOTICE.rstrip("。"),
        "report_source": resolved_report_source,
        "market_data_batches": resolved_market_batches,
        "source_snapshot": {
            "sha256": source_snapshot_hash,
            "report_source": resolved_report_source,
            "market_data_batches": resolved_market_batches,
            "market_data_records_omitted": True,
        },
        "research_only": True,
        "automatic_trading_enabled": False,
        "counts": {
            "reports": len(report_rows),
            "claims": len(claim_rows),
            "outcomes": len(outcome_rows),
            "skill_snapshots": len(snapshot_rows),
            "rank_eligible_skills": sum(1 for row in skill_rows if row.get("rank_eligible") is True),
            "factor_observations": len(factor_rows),
            "deep_read_items": len(deep_read_rows),
            "exceptions": len(exception_rows),
        },
        "artifacts": [
            {"name": name, "sha256": output_hashes[name]}
            for name in ARTIFACT_FILENAMES
            if name != "run_manifest.json"
        ],
        "manifest_hash_policy": "manifest excludes its own hash",
    }
    paths["run_manifest.json"].write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    all_hashes = {**output_hashes, "run_manifest.json": _sha256_file(paths["run_manifest.json"])}
    return ReportBundle(
        output_directory=output_path,
        paths=paths,
        hashes=all_hashes,
        run_id=run_id,
    )


__all__ = [
    "ACCURACY_COLUMNS",
    "ARTIFACT_FILENAMES",
    "COVERAGE_COLUMNS",
    "EXCEPTION_COLUMNS",
    "FACTOR_COLUMNS",
    "ReportBundle",
    "ReportingError",
    "SKILL_COLUMNS",
    "build_accuracy_rows",
    "build_coverage_rows",
    "build_dashboard_data",
    "build_factor_rows",
    "build_skill_rows",
    "render_dashboard",
    "render_deep_read_queue",
    "render_walk_forward_report",
    "select_deep_reads",
    "write_report_bundle",
]
