"""Three-layer research factors, deterministic deep-read ranking and OOS tests.

There is intentionally no macro/industry/stock weighted-average score.  The
three report factors stay separate and only the two preregistered adjacent
interactions are produced.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from .evaluation import episode_deduplicate
from .skills import build_skill_snapshots, select_skill_snapshot

try:
    from .models import FactorObservation
except (ImportError, AttributeError):  # pragma: no cover - compatibility path
    FactorObservation = None  # type: ignore[assignment,misc]


CHINA_TZ = timezone(timedelta(hours=8))

# Every baseline retains the objective stock layer; report features are added
# only by B1/B2/M1 according to the preregistered comparison.
MODEL_FEATURES: dict[str, tuple[str, ...]] = {
    "B0": ("macro_objective_factor", "industry_objective_factor", "stock_objective_factor"),
    "B1": (
        "macro_objective_factor",
        "industry_objective_factor",
        "stock_objective_factor",
        "macro_report_raw",
        "industry_report_raw",
        "stock_report_raw",
    ),
    "B2": (
        "macro_objective_factor",
        "industry_objective_factor",
        "stock_objective_factor",
        "stock_report_factor",
    ),
    "M1": (
        "macro_objective_factor",
        "macro_report_factor",
        "industry_objective_factor",
        "industry_report_factor",
        "stock_objective_factor",
        "stock_report_factor",
        "macro_industry_interaction",
        "industry_stock_interaction",
    ),
}

# Absolute rating and EPS levels are retained as ResearchClaim rows for the
# independent audit tables, but they are not revisions.  Admitting them here
# would turn a maintained Buy into a positive change and an absolute EPS value
# into a fabricated neutral revision.
STOCK_REPORT_FACTOR_TARGET_TYPES = frozenset(
    {
        "rating_change",
        "target_price",
        "earnings_revision",
        "eps_revision",
    }
)


class FactorError(ValueError):
    """Base error for invalid or time-inconsistent factor research inputs."""


FACTOR_ROW_CONTRACT_VERSION = "broker-report-factor-row.v1"
INTERNAL_LABEL_CONTRACT_VERSION = "broker-report-internal-label.v1"


@dataclass(frozen=True)
class InternalFactorResearchBatch:
    """Rows whose labels were recomputed from local, point-in-time inputs.

    This type is deliberately produced only by
    :func:`build_internal_factor_research_rows`.  External CSV/JSON rows never
    become an instance of it and therefore cannot turn on the admission gate.
    """

    rows: tuple[dict[str, Any], ...]
    trading_calendar: tuple[date, ...]
    evidence_hash: str
    bar_sources: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()


def _get(record: Any, *names: str, default: Any = None) -> Any:
    if record is None:
        return default
    for name in names:
        if isinstance(record, Mapping) and name in record:
            value = record[name]
        else:
            value = getattr(record, name, None)
        if value is not None:
            return value
    return default


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, ArithmeticError):
        return None
    return number if math.isfinite(number) else None


def _datetime(value: Any, *, end_of_day: bool = False) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, time.max if end_of_day else time.min)
    else:
        text = str(value or "").strip()
        if not text:
            raise FactorError("timestamp must not be empty")
        if "T" not in text and " " not in text:
            result = datetime.combine(date.fromisoformat(text[:10]), time.max if end_of_day else time.min)
        else:
            result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=CHINA_TZ)
    return result


def _construct_observation(payload: dict[str, Any]) -> Any:
    if FactorObservation is None:
        return payload
    try:
        parameters = inspect.signature(FactorObservation).parameters
        return FactorObservation(**{key: value for key, value in payload.items() if key in parameters})
    except (TypeError, ValueError):
        return payload


def _analyst(report: Any) -> str:
    value = _get(report, "analyst", "analysts", default="")
    if isinstance(value, (list, tuple, set)):
        return "|".join(sorted(str(item).strip() for item in value if str(item).strip()))
    return str(value or "").strip()


def claim_signal(claim: Any, *, reference_value: Any | None = None) -> float:
    """Map an explicit claim to a bounded signed signal without hindsight."""

    direction = int(_get(claim, "direction", default=0) or 0)
    direction = 1 if direction > 0 else -1 if direction < 0 else 0
    target_type = str(_get(claim, "target_type", default="")).lower()
    low = _float(_get(claim, "value_min"))
    high = _float(_get(claim, "value_max"))
    midpoint = None
    if low is not None or high is not None:
        low = high if low is None else low
        high = low if high is None else high
        assert low is not None and high is not None
        midpoint = (low + high) / 2.0

    reference = _float(reference_value)
    if target_type in {"target_price", "price_target"} and midpoint is not None and reference and reference > 0:
        return max(-1.0, min(1.0, midpoint / reference - 1.0))
    unit = str(_get(claim, "unit", default="")).lower()
    if midpoint is not None and unit in {"%", "pct", "percent", "percentage"}:
        magnitude = abs(midpoint) / 100.0 if abs(midpoint) > 1.0 else abs(midpoint)
        signed = direction * magnitude if direction else midpoint / (100.0 if abs(midpoint) > 1.0 else 1.0)
        return max(-1.0, min(1.0, signed))
    return float(direction)


def _report_maps(reports: Iterable[Any] | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(reports, Mapping):
        return dict(reports)
    return {str(_get(report, "report_id", default="")): report for report in reports}


def _cluster_key(claim: Any) -> tuple[Any, ...]:
    available = _datetime(_get(claim, "available_at"), end_of_day=True)
    return (
        str(_get(claim, "dimension", default="")),
        str(_get(claim, "subject_id", default="")),
        str(_get(claim, "target_type", default="")),
        int(_get(claim, "horizon_days", default=0) or 0),
        int(_get(claim, "direction", default=0) or 0),
        available.date().isoformat(),
    )


def _layer_factor(
    claims: Iterable[Any],
    report_by_id: Mapping[str, Any],
    snapshots: Iterable[Any],
    as_of: datetime,
    reference_values: Mapping[str, Any] | None,
) -> tuple[
    float | None,
    float | None,
    tuple[str, ...],
    tuple[dict[str, Any], ...],
    tuple[dict[str, Any], ...],
    list[str],
]:
    valid: list[tuple[Any, Any, float]] = []
    exclusions: list[str] = []
    reference_provenance: dict[str, dict[str, Any]] = {}
    for claim in claims:
        available_raw = _get(claim, "available_at")
        if available_raw is None:
            exclusions.append(f"{_get(claim, 'claim_id', default='?')}:missing_available_at")
            continue
        available = _datetime(available_raw, end_of_day=True)
        if available > as_of:
            exclusions.append(f"{_get(claim, 'claim_id', default='?')}:future_claim")
            continue
        horizon_days = int(_get(claim, "horizon_days", default=0) or 0)
        maximum_age = math.ceil(horizon_days * 7.0 / 5.0) + 7 if horizon_days > 0 else 0
        if maximum_age and (as_of.date() - available.date()).days > maximum_age:
            exclusions.append(f"{_get(claim, 'claim_id', default='?')}:expired_claim")
            continue
        report = report_by_id.get(str(_get(claim, "report_id", default="")))
        subject_id = str(_get(claim, "subject_id", default=""))
        target_type = str(_get(claim, "target_type", default="")).lower()
        reference = (reference_values or {}).get(subject_id)
        if target_type in {"target_price", "price_target"}:
            if not isinstance(reference, Mapping):
                exclusions.append(
                    f"{_get(claim, 'claim_id', default='?')}:missing_auditable_reference_price"
                )
                continue
            reference_available = _get(reference, "available_at")
            reference_source = str(_get(reference, "source", default="") or "").strip()
            reference_hash = str(
                _get(reference, "content_hash", default="") or ""
            ).lower()
            reference_value = _float(_get(reference, "value", "close", "price"))
            if (
                reference_available is None
                or _datetime(reference_available, end_of_day=True) > as_of
                or not reference_source
                or not re.fullmatch(r"[0-9a-f]{64}", reference_hash)
                or reference_value is None
                or reference_value <= 0
            ):
                exclusions.append(
                    f"{_get(claim, 'claim_id', default='?')}:invalid_or_future_reference_price"
                )
                continue
            reference_time = _datetime(reference_available, end_of_day=True)
            reference_provenance[subject_id] = {
                "subject_id": subject_id,
                "value": reference_value,
                "available_at": reference_time.isoformat(),
                "source": reference_source,
                "content_hash": reference_hash,
            }
            reference = reference_value
        elif isinstance(reference, Mapping):
            reference = _get(reference, "value", "close", "price")
        valid.append((claim, report, claim_signal(claim, reference_value=reference)))
    if not valid:
        return None, None, (), (), tuple(reference_provenance.values()), exclusions

    clusters: dict[tuple[Any, ...], list[tuple[float, float | None, str]]] = defaultdict(list)
    snapshot_ids: set[str] = set()
    snapshot_provenance: dict[str, dict[str, Any]] = {}
    for claim, report, signal in valid:
        claim_available = _datetime(_get(claim, "available_at"), end_of_day=True)
        current_report_id = str(_get(claim, "report_id", default=""))
        prior_snapshots = [
            snapshot
            for snapshot in snapshots
            if current_report_id
            not in {
                str(source_report_id)
                for source_report_id in (
                    _get(snapshot, "source_report_ids", default=()) or ()
                )
            }
        ]
        snapshot = select_skill_snapshot(
            prior_snapshots,
            # Skill must be frozen before this claim became executable.  Using
            # the factor observation time would allow a mature claim to teach
            # the model how heavily to weight itself while it is still active.
            as_of=claim_available,
            claim=claim,
            report=report,
        )
        if snapshot is None:
            skill = 0.0
            weighted_signal = None
            exclusions.append(
                f"{_get(claim, 'claim_id', default='?')}:missing_strictly_prior_skill"
            )
        else:
            skill = _float(_get(snapshot, "conservative_lower_bound")) or 0.0
            snapshot_id = str(_get(snapshot, "snapshot_id", default=""))
            if snapshot_id:
                snapshot_ids.add(snapshot_id)
            canonical_snapshot = {
                "snapshot_id": snapshot_id,
                "as_of": str(_get(snapshot, "as_of", default="")),
                "posterior_skill": _float(_get(snapshot, "posterior_skill")),
                "conservative_lower_bound": _float(
                    _get(snapshot, "conservative_lower_bound")
                ),
                "effective_sample_size": _float(
                    _get(snapshot, "effective_sample_size")
                ),
                "source_report_ids": sorted(
                    str(value)
                    for value in (_get(snapshot, "source_report_ids", default=()) or ())
                ),
            }
            canonical_key = json.dumps(
                canonical_snapshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            snapshot_provenance[canonical_key] = canonical_snapshot
            reliability = max(0.0, min(1.0, 2.0 * skill - 1.0))
            weighted_signal = signal * reliability
        clusters[_cluster_key(claim)].append(
            (signal, weighted_signal, str(_get(claim, "claim_id", default="")))
        )

    # A same-day consensus cluster contributes once irrespective of source count.
    raw_clusters: list[float] = []
    skill_clusters: list[float] = []
    for values in clusters.values():
        raw_clusters.append(sum(value[0] for value in values) / len(values))
        skill_values = [value[1] for value in values if value[1] is not None]
        if skill_values:
            skill_clusters.append(sum(skill_values) / len(skill_values))
    raw = sum(raw_clusters) / len(raw_clusters)
    weighted = sum(skill_clusters) / len(skill_clusters) if skill_clusters else None
    return (
        weighted,
        raw,
        tuple(sorted(snapshot_ids)),
        tuple(snapshot_provenance[key] for key in sorted(snapshot_provenance)),
        tuple(reference_provenance[key] for key in sorted(reference_provenance)),
        exclusions,
    )


def build_factor_components(
    *,
    as_of: Any,
    stock_id: str,
    macro_claims: Iterable[Any] = (),
    industry_claims: Iterable[Any] = (),
    stock_claims: Iterable[Any] = (),
    reports: Iterable[Any] | Mapping[str, Any] = (),
    skill_snapshots: Iterable[Any] = (),
    macro_objective_factor: float = 0.0,
    industry_objective_factor: float = 0.0,
    stock_objective_factor: float = 0.0,
    objective_provenance: Mapping[str, Any] | None = None,
    reference_values: Mapping[str, Any] | None = None,
    deduplicate: bool = True,
) -> dict[str, Any]:
    """Build independent report factors and only adjacent-layer interactions."""

    decision = _datetime(as_of, end_of_day=True)
    report_by_id = _report_maps(reports)
    snapshots = list(skill_snapshots)
    layers = {
        "macro": list(macro_claims),
        "industry": list(industry_claims),
        "stock": list(stock_claims),
    }
    if deduplicate:
        deduplicated: dict[str, list[Any]] = {}
        for name, values in layers.items():
            episode_values = episode_deduplicate(
                values,
                report_by_id,
                keep="last",
            )
            latest_by_source: dict[tuple[Any, ...], Any] = {}
            for claim in episode_values:
                report = report_by_id.get(str(_get(claim, "report_id", default="")))
                source_key = (
                    str(_get(report, "broker_code", default=""))
                    or str(_get(report, "broker", default=""))
                    or str(_get(claim, "report_id", default="")),
                    _analyst(report),
                    str(_get(claim, "dimension", default="")),
                    str(_get(claim, "subject_id", default="")),
                    str(_get(claim, "target_type", default="")),
                    int(_get(claim, "horizon_days", default=0) or 0),
                )
                previous = latest_by_source.get(source_key)
                if previous is None or _datetime(
                    _get(claim, "available_at"), end_of_day=True
                ) > _datetime(_get(previous, "available_at"), end_of_day=True):
                    latest_by_source[source_key] = claim
            deduplicated[name] = list(latest_by_source.values())
        layers = deduplicated
    stock_factor_exclusions: list[str] = []
    admissible_stock_claims: list[Any] = []
    for claim in layers["stock"]:
        target_type = str(_get(claim, "target_type", default="")).strip().lower()
        if target_type not in STOCK_REPORT_FACTOR_TARGET_TYPES:
            stock_factor_exclusions.append(
                f"{_get(claim, 'claim_id', default='?')}:"
                f"not_admissible_stock_change_signal:{target_type or 'missing'}"
            )
            continue
        admissible_stock_claims.append(claim)
    layers["stock"] = admissible_stock_claims
    factor_values: dict[str, float | None] = {}
    raw_values: dict[str, float | None] = {}
    snapshot_ids: set[str] = set()
    selected_snapshot_provenance: dict[str, dict[str, Any]] = {}
    selected_reference_provenance: dict[str, dict[str, Any]] = {}
    exclusions: list[str] = list(stock_factor_exclusions)
    for layer, values in layers.items():
        if not values:
            exclusions.append(f"{layer}:missing_current_claims")
        weighted, raw, ids, layer_snapshots, layer_references, layer_exclusions = _layer_factor(
            values,
            report_by_id,
            snapshots,
            decision,
            reference_values,
        )
        factor_values[f"{layer}_report_factor"] = weighted
        raw_values[f"{layer}_report_raw"] = raw
        snapshot_ids.update(ids)
        for snapshot_record in layer_snapshots:
            canonical_key = json.dumps(
                snapshot_record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            selected_snapshot_provenance[canonical_key] = snapshot_record
        for reference_record in layer_references:
            canonical_key = json.dumps(
                reference_record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            selected_reference_provenance[canonical_key] = reference_record
        exclusions.extend(layer_exclusions)

    macro_report = factor_values["macro_report_factor"]
    industry_report = factor_values["industry_report_factor"]
    stock_report = factor_values["stock_report_factor"]
    macro_industry_interaction = (
        macro_report * industry_report
        if macro_report is not None and industry_report is not None
        else None
    )
    industry_stock_interaction = (
        industry_report * stock_report
        if industry_report is not None and stock_report is not None
        else None
    )
    provenance = {
        "as_of": decision.isoformat(),
        "stock_id": str(stock_id),
        "objective": {
            "macro": float(macro_objective_factor),
            "industry": float(industry_objective_factor),
            "stock": float(stock_objective_factor),
        },
        "objective_provenance": dict(objective_provenance or {}),
        "claims": sorted(
            (
                str(_get(claim, "claim_id", default="")),
                str(_get(claim, "report_id", default="")),
                str(_get(claim, "available_at", default="")),
                int(_get(claim, "direction", default=0) or 0),
                str(_get(claim, "value_min", default="")),
                str(_get(claim, "value_max", default="")),
            )
            for values in layers.values()
            for claim in values
        ),
        "reports": sorted(
            (
                report_id,
                str(_get(report, "content_hash", "raw_hash", default="")),
                str(_get(report, "available_at", "published_at", default="")),
            )
            for report_id, report in report_by_id.items()
            if any(report_id == str(_get(claim, "report_id", default="")) for values in layers.values() for claim in values)
        ),
        "snapshots": [
            selected_snapshot_provenance[key]
            for key in sorted(selected_snapshot_provenance)
        ],
        "reference_values": [
            selected_reference_provenance[key]
            for key in sorted(selected_reference_provenance)
        ],
        "features": {
            "macro_objective_factor": float(macro_objective_factor),
            "industry_objective_factor": float(industry_objective_factor),
            "stock_objective_factor": float(stock_objective_factor),
            **raw_values,
            **factor_values,
            "macro_industry_interaction": macro_industry_interaction,
            "industry_stock_interaction": industry_stock_interaction,
        },
    }
    snapshot_hash = hashlib.sha256(
        json.dumps(
            provenance,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    payload = {
        "contract_version": FACTOR_ROW_CONTRACT_VERSION,
        "as_of": decision,
        "stock_id": str(stock_id),
        "macro_objective_factor": float(macro_objective_factor),
        "macro_report_factor": macro_report,
        "industry_objective_factor": float(industry_objective_factor),
        "industry_report_factor": industry_report,
        "stock_objective_factor": float(stock_objective_factor),
        "stock_report_factor": stock_report,
        "macro_industry_interaction": macro_industry_interaction,
        "industry_stock_interaction": industry_stock_interaction,
        "source_snapshot_hash": snapshot_hash,
        "source_snapshot_payload": provenance,
        **raw_values,
        "source_snapshot_ids": tuple(sorted(snapshot_ids)),
        "exclusions": tuple(exclusions),
    }
    return payload


def build_factor_observation(**kwargs: Any) -> Any:
    """Typed/model-compatible wrapper around :func:`build_factor_components`."""

    return _construct_observation(build_factor_components(**kwargs))


def build_factor_observations(
    specifications: Iterable[Mapping[str, Any]],
    *,
    as_of: Any,
    reports: Iterable[Any] | Mapping[str, Any] = (),
    skill_snapshots: Iterable[Any] = (),
    claims: Iterable[Any] = (),
    outcomes: Iterable[Any] = (),
    snapshots: Iterable[Any] | None = None,
    objective_factors: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
    outcomes_are_trusted: bool = False,
    return_components: bool = False,
) -> list[Any]:
    """Build stock observations from point-in-time objective specifications.

    A specification must provide macro, industry and stock objective factors,
    each either as ``{"value": ..., "available_at": ..., "source": ...}``
    or as a scalar accompanied by ``<name>_available_at`` and
    ``<name>_source``.  Missing/future objective inputs reject the batch
    instead of silently becoming zero.
    """

    del objective_factors  # accepted for stable CLI adapters
    decision = _datetime(as_of, end_of_day=True)
    report_values = list(reports.values()) if isinstance(reports, Mapping) else list(reports)
    report_by_id = _report_maps(report_values)
    claim_values = list(claims)
    snapshot_values = list(skill_snapshots) or list(snapshots or [])
    outcome_values = list(outcomes)
    if outcome_values and outcomes_are_trusted:
        skill_config = dict((config or {}).get("skill", {}))
        prior_times = sorted(
            {
                _datetime(_get(claim, "available_at"), end_of_day=True)
                - timedelta(microseconds=1)
                for claim in claim_values
                if _get(claim, "available_at") is not None
                and _datetime(_get(claim, "available_at"), end_of_day=True) <= decision
            }
        )
        for snapshot_time in prior_times:
            snapshot_values.extend(
                build_skill_snapshots(
                    outcome_values,
                    claim_values,
                    report_values,
                    as_of=snapshot_time,
                    half_life_days=float(skill_config.get("half_life_days", 730.0)),
                    sensitivity_half_life_days=float(
                        skill_config.get("sensitivity_half_life_days", 365.0)
                    ),
                    lookback_years=float(
                        skill_config.get("maximum_lookback_years", 5.0)
                    ),
                )
            )

    def objective_value(
        specification: Mapping[str, Any], name: str, observation_time: datetime
    ) -> tuple[float, dict[str, str]]:
        raw = specification.get(name)
        if isinstance(raw, Mapping):
            available = _get(raw, "available_at")
            source = str(_get(raw, "source", default="") or "").strip()
            value = _float(_get(raw, "value", "score", "signal"))
        else:
            available = specification.get(f"{name}_available_at")
            source = str(specification.get(f"{name}_source") or "").strip()
            value = _float(raw)
        if available is None or value is None or not source:
            raise FactorError(
                f"{name} requires numeric value, available_at and source"
            )
        available_time = _datetime(available, end_of_day=True)
        if available_time > observation_time:
            raise FactorError(
                f"{name} is future information at {observation_time.isoformat()}"
            )
        return value, {
            "available_at": available_time.isoformat(),
            "source": source,
        }

    output: list[Any] = []
    for specification in specifications:
        forbidden_overrides = {
            name
            for name in (
                "macro_claims",
                "industry_claims",
                "stock_claims",
                "reference_values",
            )
            if name in specification
        }
        if forbidden_overrides:
            raise FactorError(
                "factor specifications may not embed claims or price references: "
                + ", ".join(sorted(forbidden_overrides))
            )
        observation_time = _datetime(
            specification.get("as_of", decision), end_of_day=True
        )
        if observation_time > decision:
            raise FactorError("factor specification as_of exceeds command decision time")
        stock_id = str(specification.get("stock_id") or specification.get("subject_id") or "").strip()
        if not stock_id:
            raise FactorError("factor specification requires stock_id")
        industry_id = str(
            specification.get("industry_id") or specification.get("industry_code") or ""
        ).strip()
        macro_claims = [
            claim
            for claim in claim_values
            if str(_get(claim, "dimension", default="")).lower() == "macro"
        ]
        industry_claims = [
            claim
            for claim in claim_values
            if str(_get(claim, "dimension", default="")).lower() == "industry"
            and industry_id
            and str(_get(claim, "subject_id", default="")) == industry_id
        ]
        stock_claims = [
            claim
            for claim in claim_values
            if str(_get(claim, "dimension", default="")).lower() == "stock"
            and str(_get(claim, "subject_id", default="")) == stock_id
        ]
        macro_objective, macro_provenance = objective_value(
            specification, "macro_objective_factor", observation_time
        )
        industry_objective, industry_provenance = objective_value(
            specification, "industry_objective_factor", observation_time
        )
        stock_objective, stock_provenance = objective_value(
            specification, "stock_objective_factor", observation_time
        )
        component_kwargs = dict(
                as_of=observation_time,
                stock_id=stock_id,
                macro_claims=macro_claims,
                industry_claims=industry_claims,
                stock_claims=stock_claims,
                reports=report_by_id or report_values,
                skill_snapshots=snapshot_values,
                macro_objective_factor=macro_objective,
                industry_objective_factor=industry_objective,
                stock_objective_factor=stock_objective,
                objective_provenance={
                    "macro": macro_provenance,
                    "industry": industry_provenance,
                    "stock": stock_provenance,
                },
                reference_values=None,
                deduplicate=bool(specification.get("deduplicate", True)),
            )
        output.append(
            build_factor_components(**component_kwargs)
            if return_components
            else build_factor_observation(**component_kwargs)
        )
    return output


def build_model_feature_sets(
    observation: Any,
    *,
    raw_components: Mapping[str, Any] | None = None,
    stock_objective_factor: Any | None = None,
) -> dict[str, dict[str, float] | None]:
    """Return B0/B1/B2/M1 inputs, using None for unverifiable feature sets."""

    merged: dict[str, Any] = {}
    if isinstance(observation, Mapping):
        merged.update(observation)
    else:
        for field in {name for fields in MODEL_FEATURES.values() for name in fields}:
            merged[field] = _get(observation, field)
    if raw_components:
        merged.update(raw_components)
    if stock_objective_factor is not None:
        merged["stock_objective_factor"] = stock_objective_factor

    result: dict[str, dict[str, float] | None] = {}
    for model_name, fields in MODEL_FEATURES.items():
        feature_names = list(fields)
        values = {name: _float(merged.get(name)) for name in feature_names}
        result[model_name] = (
            {name: float(value) for name, value in values.items() if value is not None}
            if all(value is not None for value in values.values())
            else None
        )
    return result


def validate_walk_forward_input_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    sample_start: Any,
    sample_end: Any,
    evaluation_as_of: Any,
    label_field: str = "stock_excess_vs_industry_20d",
    secondary_label_field: str = "stock_excess_vs_industry_60d",
    date_field: str = "as_of",
    industry_field: str = "industry_id",
    require_internal_label_provenance: bool = False,
) -> list[dict[str, Any]]:
    """Validate external factor rows before they can affect admission.

    A numeric CSV assembled by hand is useful for diagnostics, but it is not
    auditable evidence.  Admission-capable rows therefore carry the canonical
    factor snapshot payload whose SHA-256 is stored on the row, point-in-time
    provenance for every layer, and explicit relative-industry label metadata.
    """

    start_date = _datetime(sample_start).date()
    end_date = _datetime(sample_end, end_of_day=True).date()
    evaluation_time = _datetime(evaluation_as_of, end_of_day=True)
    required_features = tuple(
        sorted({name for names in MODEL_FEATURES.values() for name in names})
    )
    validated: list[dict[str, Any]] = []

    def require_timestamp(
        value: Any,
        *,
        field: str,
        not_after: datetime,
        strictly_before: bool = False,
    ) -> datetime:
        if value in (None, ""):
            raise FactorError(f"{field} is required")
        parsed = _datetime(value, end_of_day=True)
        if parsed > not_after or (strictly_before and parsed >= not_after):
            relation = "strictly before" if strictly_before else "not after"
            raise FactorError(f"{field} must be {relation} {not_after.isoformat()}")
        return parsed

    def validate_label(
        row: Mapping[str, Any],
        *,
        row_time: datetime,
        field_name: str,
        horizon: int,
        prefix: str,
        optional: bool,
    ) -> None:
        label_value = _float(row.get(field_name))
        if label_value is None:
            if optional and row.get(field_name) in (None, ""):
                return
            raise FactorError(f"{field_name} must be finite numeric relative-industry excess return")
        if str(row.get(f"{prefix}label_name") or field_name) != field_name:
            raise FactorError(f"{prefix}label_name must equal {field_name}")
        if str(row.get(f"{prefix}label_definition") or "") != "stock_excess_vs_industry_geometric":
            raise FactorError(
                f"{prefix}label_definition must be stock_excess_vs_industry_geometric"
            )
        if int(row.get(f"{prefix}label_horizon_days") or 0) != horizon:
            raise FactorError(f"{prefix}label_horizon_days must equal {horizon}")
        label_end = _datetime(row.get(f"{prefix}label_end"), end_of_day=True)
        if label_end <= row_time:
            raise FactorError(f"{prefix}label_end must be after factor as_of")
        label_available = require_timestamp(
            row.get(f"{prefix}label_available_at"),
            field=f"{prefix}label_available_at",
            not_after=evaluation_time,
        )
        if label_available < label_end:
            raise FactorError(f"{prefix}label_available_at cannot precede label_end")
        if not str(row.get(f"{prefix}label_source") or "").strip():
            raise FactorError(f"{prefix}label_source is required")
        if not str(row.get(f"{prefix}benchmark_id") or "").strip():
            raise FactorError(f"{prefix}benchmark_id is required")
        if require_internal_label_provenance:
            provenance = row.get(f"{prefix}label_provenance")
            provenance_hash = str(
                row.get(f"{prefix}label_provenance_hash") or ""
            ).lower()
            if not isinstance(provenance, Mapping):
                raise FactorError(f"{prefix}label_provenance is required")
            if (
                not re.fullmatch(r"[0-9a-f]{64}", provenance_hash)
                or _canonical_hash(provenance) != provenance_hash
            ):
                raise FactorError(f"{prefix}label_provenance hash mismatch")
            if provenance.get("contract_version") != INTERNAL_LABEL_CONTRACT_VERSION:
                raise FactorError(f"{prefix}label_provenance contract mismatch")
            if int(provenance.get("horizon_sessions") or 0) != horizon:
                raise FactorError(f"{prefix}label_provenance horizon mismatch")
            if str(provenance.get("benchmark_id") or "") != str(
                row.get(f"{prefix}benchmark_id") or ""
            ):
                raise FactorError(f"{prefix}label_provenance benchmark mismatch")
            mapping = provenance.get("industry_mapping")
            if (
                not isinstance(mapping, Mapping)
                or str(mapping.get("industry_id") or "")
                != str(row.get(industry_field) or "")
                or not re.fullmatch(
                    r"[0-9a-f]{64}",
                    str(mapping.get("content_hash") or "").lower(),
                )
            ):
                raise FactorError(f"{prefix}label_provenance mapping mismatch")
            bars = provenance.get("bars")
            if not isinstance(bars, Sequence) or len(bars) != 4:
                raise FactorError(f"{prefix}label_provenance requires four endpoint bars")
            for bar_index, bar in enumerate(bars):
                if (
                    not isinstance(bar, Mapping)
                    or not re.fullmatch(
                        r"[0-9a-f]{64}", str(bar.get("content_hash") or "").lower()
                    )
                    or not str(bar.get("source") or "").strip()
                ):
                    raise FactorError(
                        f"{prefix}label_provenance.bars[{bar_index}] is incomplete"
                    )
            stock_start, stock_end, benchmark_start, benchmark_end = bars
            start_open = _float(stock_start.get("evaluation_open"))
            end_close = _float(stock_end.get("evaluation_close"))
            benchmark_open = _float(benchmark_start.get("evaluation_open"))
            benchmark_close = _float(benchmark_end.get("evaluation_close"))
            if (
                start_open is None
                or end_close is None
                or benchmark_open is None
                or benchmark_close is None
                or min(start_open, end_close, benchmark_open, benchmark_close) <= 0
            ):
                raise FactorError(f"{prefix}label_provenance contains invalid prices")
            recomputed = (end_close / start_open) / (
                benchmark_close / benchmark_open
            ) - 1.0
            if not math.isclose(
                float(label_value), recomputed, rel_tol=1e-12, abs_tol=1e-12
            ):
                raise FactorError(f"{prefix}label differs from endpoint bars")

    for index, raw_row in enumerate(rows):
        row = dict(raw_row)
        prefix = f"row[{index}]"
        if row.get("contract_version") != FACTOR_ROW_CONTRACT_VERSION:
            raise FactorError(
                f"{prefix}.contract_version must be {FACTOR_ROW_CONTRACT_VERSION}"
            )
        stock_id = str(row.get("stock_id") or "").strip()
        industry_id = str(row.get(industry_field) or "").strip()
        if not stock_id or not industry_id or industry_id.upper() == "UNKNOWN":
            raise FactorError(f"{prefix} requires stock_id and mapped {industry_field}")
        row_time = _datetime(row.get(date_field), end_of_day=True)
        if not (start_date <= row_time.date() <= end_date):
            raise FactorError(f"{prefix}.{date_field} is outside preregistered backfill window")
        if row_time > evaluation_time:
            raise FactorError(f"{prefix}.{date_field} is after evaluation cutoff")

        snapshot_hash = str(row.get("source_snapshot_hash") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", snapshot_hash):
            raise FactorError(f"{prefix}.source_snapshot_hash must be a SHA-256 hex digest")
        snapshot_payload = row.get("source_snapshot_payload")
        if isinstance(snapshot_payload, str):
            try:
                snapshot_payload = json.loads(snapshot_payload)
            except json.JSONDecodeError as exc:
                raise FactorError(f"{prefix}.source_snapshot_payload is invalid JSON") from exc
        if not isinstance(snapshot_payload, Mapping):
            raise FactorError(f"{prefix}.source_snapshot_payload must be an object")
        canonical_payload = json.dumps(
            snapshot_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        if hashlib.sha256(canonical_payload).hexdigest() != snapshot_hash:
            raise FactorError(f"{prefix}.source_snapshot_hash does not match payload")
        if str(snapshot_payload.get("stock_id") or "") != stock_id:
            raise FactorError(f"{prefix}.source snapshot stock_id mismatch")
        snapshot_time = _datetime(snapshot_payload.get("as_of"), end_of_day=True)
        if snapshot_time != row_time:
            raise FactorError(f"{prefix}.source snapshot as_of mismatch")

        provenance = snapshot_payload.get("objective_provenance")
        if not isinstance(provenance, Mapping):
            raise FactorError(f"{prefix}.objective_provenance is required")
        for layer in ("macro", "industry", "stock"):
            item = provenance.get(layer)
            if not isinstance(item, Mapping) or not str(item.get("source") or "").strip():
                raise FactorError(f"{prefix}.{layer} objective source is required")
            require_timestamp(
                item.get("available_at"),
                field=f"{prefix}.{layer}.available_at",
                not_after=row_time,
            )

        for claim_index, claim_record in enumerate(snapshot_payload.get("claims") or []):
            if not isinstance(claim_record, Sequence) or len(claim_record) < 3:
                raise FactorError(f"{prefix}.claims[{claim_index}] has invalid provenance")
            require_timestamp(
                claim_record[2],
                field=f"{prefix}.claims[{claim_index}].available_at",
                not_after=row_time,
            )
        for report_index, report_record in enumerate(snapshot_payload.get("reports") or []):
            if not isinstance(report_record, Sequence) or len(report_record) < 3:
                raise FactorError(f"{prefix}.reports[{report_index}] has invalid provenance")
            require_timestamp(
                report_record[2],
                field=f"{prefix}.reports[{report_index}].available_at",
                not_after=row_time,
            )
        for snapshot_index, skill_record in enumerate(snapshot_payload.get("snapshots") or []):
            if not isinstance(skill_record, Mapping):
                raise FactorError(f"{prefix}.snapshots[{snapshot_index}] has invalid provenance")
            require_timestamp(
                skill_record.get("as_of"),
                field=f"{prefix}.snapshots[{snapshot_index}].as_of",
                not_after=row_time,
                strictly_before=True,
            )
        for reference_index, reference_record in enumerate(
            snapshot_payload.get("reference_values") or []
        ):
            if not isinstance(reference_record, Mapping):
                raise FactorError(
                    f"{prefix}.reference_values[{reference_index}] has invalid provenance"
                )
            if (
                (_float(reference_record.get("value")) or 0.0) <= 0
                or not str(reference_record.get("source") or "").strip()
                or not re.fullmatch(
                    r"[0-9a-f]{64}",
                    str(reference_record.get("content_hash") or "").lower(),
                )
            ):
                raise FactorError(
                    f"{prefix}.reference_values[{reference_index}] is incomplete"
                )
            require_timestamp(
                reference_record.get("available_at"),
                field=f"{prefix}.reference_values[{reference_index}].available_at",
                not_after=row_time,
            )

        payload_features = snapshot_payload.get("features")
        if not isinstance(payload_features, Mapping):
            raise FactorError(f"{prefix}.source snapshot features are required")
        for feature_name in required_features:
            row_value = _float(row.get(feature_name))
            payload_value = _float(payload_features.get(feature_name))
            if row_value is None or payload_value is None:
                raise FactorError(f"{prefix}.{feature_name} must be finite and complete")
            if not math.isclose(row_value, payload_value, rel_tol=1e-12, abs_tol=1e-12):
                raise FactorError(f"{prefix}.{feature_name} differs from hashed snapshot")

        validate_label(
            row,
            row_time=row_time,
            field_name=label_field,
            horizon=20,
            prefix="",
            optional=False,
        )
        validate_label(
            row,
            row_time=row_time,
            field_name=secondary_label_field,
            horizon=60,
            prefix="secondary_",
            optional=True,
        )
        validated.append(row)
    return validated


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _calendar_dates(values: Iterable[Any]) -> tuple[date, ...]:
    parsed: list[date] = []
    for index, value in enumerate(values):
        raw = _get(value, "date", "trade_date", "as_of", default=value)
        try:
            parsed.append(_datetime(raw).date())
        except (FactorError, TypeError, ValueError) as exc:
            raise FactorError(f"trading_calendar[{index}] is not a valid session date") from exc
    if not parsed:
        raise FactorError("an explicit non-empty trading_calendar is required")
    if len(parsed) != len(set(parsed)):
        raise FactorError("trading_calendar contains duplicate session dates")
    if parsed != sorted(parsed):
        raise FactorError("trading_calendar must be strictly increasing")
    return tuple(parsed)


def _bar_record(bar: Any) -> dict[str, Any]:
    instrument_id = str(_get(bar, "instrument_id", default="") or "").strip()
    source = str(_get(bar, "source", default="") or "").strip()
    content_hash = str(_get(bar, "content_hash", default="") or "").lower()
    trade_day = _datetime(_get(bar, "trade_date")).date()
    available = _datetime(_get(bar, "available_at"), end_of_day=True)
    fetched = _datetime(_get(bar, "fetched_at"), end_of_day=True)
    if not instrument_id or not source or not re.fullmatch(r"[0-9a-f]{64}", content_hash):
        raise FactorError("daily bar requires instrument_id, source and SHA-256 content_hash")

    adjusted_open = _float(_get(bar, "adjusted_open"))
    adjusted_close = _float(_get(bar, "adjusted_close"))
    raw_open = _float(_get(bar, "open"))
    raw_close = _float(_get(bar, "close"))
    open_value = adjusted_open if adjusted_open is not None else raw_open
    close_value = adjusted_close if adjusted_close is not None else raw_close
    if open_value is None or close_value is None or min(open_value, close_value) <= 0:
        raise FactorError("daily bar requires positive evaluation open and close")
    return {
        "instrument_id": instrument_id,
        "trade_date": trade_day.isoformat(),
        "evaluation_open": open_value,
        "evaluation_close": close_value,
        "price_basis": "adjusted" if adjusted_open is not None and adjusted_close is not None else "raw_unadjusted_fallback",
        "suspended": bool(_get(bar, "suspended", default=False)),
        "available_at": available.isoformat(),
        "fetched_at": fetched.isoformat(),
        "source": source,
        "content_hash": content_hash,
    }


def _industry_mapping(
    specification: Mapping[str, Any],
    *,
    stock_id: str,
    observation_time: datetime,
) -> dict[str, Any]:
    raw = specification.get("industry_mapping")
    if not isinstance(raw, Mapping):
        raise FactorError("industry_mapping with point-in-time provenance is required")
    mapping = dict(raw)
    supplied_hash = str(mapping.pop("content_hash", "") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", supplied_hash):
        raise FactorError("industry_mapping.content_hash must be a SHA-256 digest")
    if _canonical_hash(mapping) != supplied_hash:
        raise FactorError("industry_mapping.content_hash does not match its canonical payload")
    mapped_stock = str(mapping.get("stock_id") or "").strip()
    industry_id = str(mapping.get("industry_id") or "").strip()
    benchmark_id = str(
        mapping.get("benchmark_id") or mapping.get("industry_instrument_id") or ""
    ).strip()
    source = str(mapping.get("source") or "").strip()
    if mapped_stock != stock_id or not industry_id or not benchmark_id or not source:
        raise FactorError(
            "industry_mapping requires the same stock_id, industry_id, benchmark_id and source"
        )
    available = _datetime(mapping.get("available_at"), end_of_day=True)
    effective_from = _datetime(mapping.get("effective_from"), end_of_day=False)
    effective_to_raw = mapping.get("effective_to")
    effective_to = (
        _datetime(effective_to_raw, end_of_day=True)
        if effective_to_raw not in (None, "")
        else None
    )
    if available > observation_time or effective_from > observation_time:
        raise FactorError("industry_mapping is not point-in-time available/effective")
    if effective_to is not None and observation_time > effective_to:
        raise FactorError("industry_mapping has expired before factor as_of")
    return {
        **mapping,
        "content_hash": supplied_hash,
        "stock_id": mapped_stock,
        "industry_id": industry_id,
        "benchmark_id": benchmark_id,
        "available_at": available.isoformat(),
        "effective_from": effective_from.isoformat(),
        "effective_to": effective_to.isoformat() if effective_to else "",
        "source": source,
    }


def build_internal_factor_research_rows(
    factor_components: Iterable[Mapping[str, Any]],
    specifications: Iterable[Mapping[str, Any]],
    daily_bars: Iterable[Any],
    *,
    trading_calendar: Iterable[Any],
    evaluation_as_of: Any,
    sample_start: Any,
    sample_end: Any,
    horizons: Sequence[int] = (20, 60),
) -> InternalFactorResearchBatch:
    """Recompute stock-vs-industry labels from local immutable evidence.

    The decision observation must be the final exchange session in its ISO
    week.  Returns start at the following session's open and end at the close
    of the requested 20th/60th future session.  A static industry name/code is
    insufficient: every specification must carry a hashed, effective-dated
    stock-to-industry benchmark mapping.
    """

    calendar = _calendar_dates(trading_calendar)
    calendar_index = {day: index for index, day in enumerate(calendar)}
    last_session_by_week: dict[tuple[int, int], date] = {}
    for day in calendar:
        iso = day.isocalendar()
        last_session_by_week[(iso.year, iso.week)] = day
    evaluation_time = _datetime(evaluation_as_of, end_of_day=True)
    first_day = _datetime(sample_start).date()
    last_day = _datetime(sample_end, end_of_day=True).date()
    requested_horizons = tuple(int(value) for value in horizons)
    if requested_horizons != (20, 60):
        raise FactorError("V1 internal labels are frozen to the 20/60 session horizons")

    specification_by_key: dict[tuple[str, date], Mapping[str, Any]] = {}
    duplicate_specifications: set[tuple[str, date]] = set()
    for specification in specifications:
        stock_id = str(
            specification.get("stock_id") or specification.get("subject_id") or ""
        ).strip()
        if not stock_id:
            continue
        observation_day = _datetime(
            specification.get("as_of", evaluation_time), end_of_day=True
        ).date()
        key = (stock_id, observation_day)
        if key in specification_by_key:
            duplicate_specifications.add(key)
        specification_by_key[key] = specification

    bar_by_key: dict[tuple[str, date], dict[str, Any]] = {}
    ambiguous_bars: set[tuple[str, date]] = set()
    for bar in daily_bars:
        record = _bar_record(bar)
        key = (record["instrument_id"], date.fromisoformat(record["trade_date"]))
        previous = bar_by_key.get(key)
        if previous is not None and _canonical_hash(previous) != _canonical_hash(record):
            ambiguous_bars.add(key)
        else:
            bar_by_key[key] = record

    required_features = tuple(
        sorted({name for fields in MODEL_FEATURES.values() for name in fields})
    )
    rows: list[dict[str, Any]] = []
    exclusions: list[str] = []
    seen_components: set[tuple[str, date]] = set()
    used_bar_hashes: set[str] = set()
    used_bar_sources: set[str] = set()
    used_mapping_hashes: set[str] = set()
    for component in factor_components:
        stock_id = str(component.get("stock_id") or "").strip()
        try:
            observation_time = _datetime(component.get("as_of"), end_of_day=True)
        except (FactorError, TypeError, ValueError):
            exclusions.append(f"{stock_id or '?'}:invalid_factor_as_of")
            continue
        observation_day = observation_time.date()
        key = (stock_id, observation_day)
        if key in seen_components:
            exclusions.append(f"{stock_id}:{observation_day}:duplicate_factor_observation")
            continue
        seen_components.add(key)
        if not stock_id or not (first_day <= observation_day <= last_day):
            exclusions.append(f"{stock_id or '?'}:{observation_day}:outside_backfill_window")
            continue
        if key in duplicate_specifications:
            exclusions.append(f"{stock_id}:{observation_day}:ambiguous_objective_specification")
            continue
        specification = specification_by_key.get(key)
        if specification is None:
            exclusions.append(f"{stock_id}:{observation_day}:missing_objective_specification")
            continue
        session_index = calendar_index.get(observation_day)
        iso = observation_day.isocalendar()
        if (
            session_index is None
            or last_session_by_week.get((iso.year, iso.week)) != observation_day
        ):
            exclusions.append(f"{stock_id}:{observation_day}:not_weekly_final_session")
            continue
        if any(_float(component.get(name)) is None for name in required_features):
            exclusions.append(f"{stock_id}:{observation_day}:incomplete_factor_features")
            continue
        snapshot_payload = component.get("source_snapshot_payload")
        snapshot_hash = str(component.get("source_snapshot_hash") or "").lower()
        if (
            not isinstance(snapshot_payload, Mapping)
            or not re.fullmatch(r"[0-9a-f]{64}", snapshot_hash)
            or _canonical_hash(snapshot_payload) != snapshot_hash
        ):
            exclusions.append(f"{stock_id}:{observation_day}:invalid_factor_snapshot")
            continue
        try:
            mapping = _industry_mapping(
                specification,
                stock_id=stock_id,
                observation_time=observation_time,
            )
        except FactorError as exc:
            exclusions.append(f"{stock_id}:{observation_day}:{exc}")
            continue
        benchmark_id = str(mapping["benchmark_id"])
        label_fields: dict[str, Any] = {}
        label_failed = False
        for horizon in requested_horizons:
            start_position = session_index + 1
            end_position = session_index + horizon
            if end_position >= len(calendar):
                exclusions.append(f"{stock_id}:{observation_day}:immature_{horizon}d_label")
                label_failed = True
                break
            start_day = calendar[start_position]
            end_day = calendar[end_position]
            bar_keys = (
                (stock_id, start_day),
                (stock_id, end_day),
                (benchmark_id, start_day),
                (benchmark_id, end_day),
            )
            if any(bar_key in ambiguous_bars for bar_key in bar_keys):
                exclusions.append(f"{stock_id}:{observation_day}:ambiguous_bar_version_{horizon}d")
                label_failed = True
                break
            records = [bar_by_key.get(bar_key) for bar_key in bar_keys]
            if any(record is None for record in records):
                exclusions.append(f"{stock_id}:{observation_day}:missing_exact_bar_{horizon}d")
                label_failed = True
                break
            stock_start, stock_end, benchmark_start, benchmark_end = records
            assert stock_start and stock_end and benchmark_start and benchmark_end
            if stock_start["suspended"] or stock_end["suspended"]:
                exclusions.append(f"{stock_id}:{observation_day}:suspended_endpoint_{horizon}d")
                label_failed = True
                break
            if any(_datetime(record["fetched_at"]) > evaluation_time for record in records):
                exclusions.append(f"{stock_id}:{observation_day}:bar_fetched_after_evaluation_cutoff")
                label_failed = True
                break
            label_available = max(_datetime(record["available_at"]) for record in records)
            if label_available > evaluation_time:
                exclusions.append(f"{stock_id}:{observation_day}:immature_{horizon}d_label")
                label_failed = True
                break
            stock_growth = stock_end["evaluation_close"] / stock_start["evaluation_open"]
            benchmark_growth = (
                benchmark_end["evaluation_close"] / benchmark_start["evaluation_open"]
            )
            if min(stock_growth, benchmark_growth) <= 0:
                exclusions.append(f"{stock_id}:{observation_day}:invalid_growth_{horizon}d")
                label_failed = True
                break
            excess = stock_growth / benchmark_growth - 1.0
            provenance = {
                "contract_version": INTERNAL_LABEL_CONTRACT_VERSION,
                "stock_id": stock_id,
                "industry_id": mapping["industry_id"],
                "benchmark_id": benchmark_id,
                "factor_as_of": observation_time.isoformat(),
                "horizon_sessions": horizon,
                "start_session": start_day.isoformat(),
                "end_session": end_day.isoformat(),
                "execution_basis": "next_session_open_to_horizon_session_close",
                "return_definition": "stock_excess_vs_industry_geometric",
                "industry_mapping": mapping,
                "bars": records,
                "trading_calendar_hash": _canonical_hash(
                    [day.isoformat() for day in calendar]
                ),
            }
            provenance_hash = _canonical_hash(provenance)
            prefix = "" if horizon == 20 else "secondary_"
            field_name = f"stock_excess_vs_industry_{horizon}d"
            label_fields.update(
                {
                    field_name: excess,
                    f"{prefix}label_name": field_name,
                    f"{prefix}label_definition": "stock_excess_vs_industry_geometric",
                    f"{prefix}label_horizon_days": horizon,
                    f"{prefix}label_start": start_day.isoformat(),
                    f"{prefix}label_end": datetime.combine(
                        end_day, time(15, 0), tzinfo=CHINA_TZ
                    ).isoformat(),
                    f"{prefix}label_available_at": label_available.isoformat(),
                    f"{prefix}label_source": "internal_local_daily_bars",
                    f"{prefix}benchmark_id": benchmark_id,
                    f"{prefix}label_provenance": provenance,
                    f"{prefix}label_provenance_hash": provenance_hash,
                }
            )
            used_bar_hashes.update(record["content_hash"] for record in records)
            used_bar_sources.update(record["source"] for record in records)
            used_mapping_hashes.add(str(mapping["content_hash"]))
        if label_failed:
            continue
        row = {
            key: value
            for key, value in component.items()
            if key not in {"source_snapshot_ids", "exclusions"}
        }
        row.update(
            {
                "contract_version": FACTOR_ROW_CONTRACT_VERSION,
                "as_of": observation_time.isoformat(),
                "stock_id": stock_id,
                "industry_id": mapping["industry_id"],
                **label_fields,
            }
        )
        rows.append(row)

    rows.sort(key=lambda row: (str(row["as_of"]), str(row["stock_id"])))
    evidence_payload = {
        "contract_version": INTERNAL_LABEL_CONTRACT_VERSION,
        "calendar": [day.isoformat() for day in calendar],
        "row_hashes": [_canonical_hash(row) for row in rows],
        "bar_content_hashes": sorted(used_bar_hashes),
        "bar_sources": sorted(used_bar_sources),
        "industry_mapping_hashes": sorted(used_mapping_hashes),
    }
    return InternalFactorResearchBatch(
        rows=tuple(rows),
        trading_calendar=calendar,
        evidence_hash=_canonical_hash(evidence_payload),
        bar_sources=tuple(sorted(used_bar_sources)),
        exclusions=tuple(exclusions),
    )


def validate_internal_factor_research_batch(
    batch: InternalFactorResearchBatch,
) -> InternalFactorResearchBatch:
    """Recompute the immutable batch attestation before enabling admission."""

    if not isinstance(batch, InternalFactorResearchBatch):
        raise FactorError("internal factor research batch type is required")
    calendar = _calendar_dates(batch.trading_calendar)
    bar_hashes: set[str] = set()
    bar_sources: set[str] = set()
    mapping_hashes: set[str] = set()
    for row_index, row in enumerate(batch.rows):
        if not isinstance(row, Mapping):
            raise FactorError(f"internal batch row[{row_index}] is not an object")
        for prefix in ("", "secondary_"):
            provenance = row.get(f"{prefix}label_provenance")
            if not isinstance(provenance, Mapping):
                raise FactorError(
                    f"internal batch row[{row_index}] missing {prefix}label_provenance"
                )
            mapping = provenance.get("industry_mapping")
            if not isinstance(mapping, Mapping):
                raise FactorError("internal batch label mapping is missing")
            mapping_hash = str(mapping.get("content_hash") or "").lower()
            if not re.fullmatch(r"[0-9a-f]{64}", mapping_hash):
                raise FactorError("internal batch label mapping hash is invalid")
            mapping_hashes.add(mapping_hash)
            bars = provenance.get("bars")
            if not isinstance(bars, Sequence) or len(bars) != 4:
                raise FactorError("internal batch label endpoint bars are incomplete")
            for bar in bars:
                if not isinstance(bar, Mapping):
                    raise FactorError("internal batch bar evidence is invalid")
                bar_hash = str(bar.get("content_hash") or "").lower()
                source = str(bar.get("source") or "").strip()
                if not re.fullmatch(r"[0-9a-f]{64}", bar_hash) or not source:
                    raise FactorError("internal batch bar hash/source is invalid")
                bar_hashes.add(bar_hash)
                bar_sources.add(source)
    evidence_payload = {
        "contract_version": INTERNAL_LABEL_CONTRACT_VERSION,
        "calendar": [day.isoformat() for day in calendar],
        "row_hashes": [_canonical_hash(row) for row in batch.rows],
        "bar_content_hashes": sorted(bar_hashes),
        "bar_sources": sorted(bar_sources),
        "industry_mapping_hashes": sorted(mapping_hashes),
    }
    if _canonical_hash(evidence_payload) != batch.evidence_hash:
        raise FactorError("internal factor research batch evidence hash mismatch")
    if tuple(sorted(bar_sources)) != tuple(sorted(batch.bar_sources)):
        raise FactorError("internal factor research batch bar source mismatch")
    return batch


def _explicitness(claims: Sequence[Any]) -> float:
    if not claims:
        return 0.0
    scores = []
    for claim in claims:
        fields = (
            bool(str(_get(claim, "target_type", default="")).strip()),
            bool(int(_get(claim, "horizon_days", default=0) or 0) > 0),
            bool(str(_get(claim, "forecast_period", default="")).strip()),
            bool(str(_get(claim, "evidence_span", default="")).strip()),
        )
        scores.append(sum(fields) / len(fields))
    return sum(scores) / len(scores)


def _report_change(report: Any, prior: Any | None, claims: Sequence[Any], prior_claims: Sequence[Any]) -> float:
    if str(_get(report, "rating_change", default="")).strip():
        return 1.0
    if prior is None:
        return 0.0
    current_rating = str(_get(report, "rating", "rating_norm", default=""))
    prior_rating = str(_get(prior, "rating", "rating_norm", default=""))
    if current_rating and prior_rating and current_rating != prior_rating:
        return 1.0
    current_signature = sorted(
        (str(_get(claim, "target_type", default="")), int(_get(claim, "direction", default=0) or 0), str(_get(claim, "value_min", default="")), str(_get(claim, "value_max", default="")))
        for claim in claims
    )
    prior_signature = sorted(
        (str(_get(claim, "target_type", default="")), int(_get(claim, "direction", default=0) or 0), str(_get(claim, "value_min", default="")), str(_get(claim, "value_max", default="")))
        for claim in prior_claims
    )
    return 1.0 if current_signature != prior_signature else 0.0


def rank_deep_reads(
    reports: Iterable[Any],
    claims: Iterable[Any],
    skill_snapshots: Iterable[Any] = (),
    *,
    as_of: Any,
    objective_signals: Mapping[Any, Any] | None = None,
    decision_sensitivity: Mapping[str, Any] | None = None,
    limit: int = 20,
    outcomes: Iterable[Any] = (),
    snapshots: Iterable[Any] | None = None,
    factor_observations: Iterable[Any] = (),
    factors: Iterable[Any] = (),
    config: Mapping[str, Any] | None = None,
    outcomes_are_trusted: bool = False,
) -> list[dict[str, Any]]:
    """Lexicographic six-key deep-read ranking; no fabricated total score."""

    if limit < 0:
        raise FactorError("limit must be non-negative")
    decision = _datetime(as_of, end_of_day=True)
    snapshot_values = list(skill_snapshots) or list(snapshots or [])
    report_values = list(reports)
    claim_values = list(claims)
    outcome_values = list(outcomes)
    if outcome_values and outcomes_are_trusted:
        skill_config = dict((config or {}).get("skill", {}))
        prior_times = sorted(
            {
                _datetime(_get(claim, "available_at"), end_of_day=True)
                - timedelta(microseconds=1)
                for claim in claim_values
                if _get(claim, "available_at") is not None
                and _datetime(_get(claim, "available_at"), end_of_day=True) <= decision
            }
        )
        for snapshot_time in prior_times:
            snapshot_values.extend(
                build_skill_snapshots(
                    outcome_values,
                    claim_values,
                    report_values,
                    as_of=snapshot_time,
                    half_life_days=float(skill_config.get("half_life_days", 730.0)),
                    sensitivity_half_life_days=float(
                        skill_config.get("sensitivity_half_life_days", 365.0)
                    ),
                    lookback_years=float(
                        skill_config.get("maximum_lookback_years", 5.0)
                    ),
                )
            )
    resolved_objectives: dict[Any, Any] = dict(objective_signals or {})
    factor_values = list(factor_observations) or list(factors)
    for factor in factor_values:
        factor_time = _get(factor, "as_of")
        if factor_time is None or _datetime(factor_time, end_of_day=True) > decision:
            continue
        stock_id = str(_get(factor, "stock_id", default=""))
        if stock_id:
            stock_objective = _float(_get(factor, "stock_objective_factor"))
            if stock_objective is not None:
                resolved_objectives.setdefault(("stock", stock_id), stock_objective)
    claims_by_report: dict[str, list[Any]] = defaultdict(list)
    for claim in claim_values:
        available = _get(claim, "available_at")
        if available is None:
            continue
        claim_time = _datetime(available, end_of_day=True)
        horizon_days = int(_get(claim, "horizon_days", default=0) or 0)
        maximum_age = math.ceil(horizon_days * 7.0 / 5.0) + 7 if horizon_days > 0 else 0
        if claim_time > decision:
            continue
        if maximum_age and (decision.date() - claim_time.date()).days > maximum_age:
            continue
        claims_by_report[str(_get(claim, "report_id", default=""))].append(claim)

    valid_reports = [
        report
        for report in report_values
        if _get(report, "available_at", "published_at") is not None
        and _datetime(_get(report, "available_at", "published_at"), end_of_day=True) <= decision
    ]
    valid_reports.sort(key=lambda report: _datetime(_get(report, "available_at", "published_at"), end_of_day=True))
    prior_by_series: dict[tuple[str, str, str], Any] = {}
    enriched: list[dict[str, Any]] = []
    for report in valid_reports:
        report_id = str(_get(report, "report_id", default=""))
        report_claims = claims_by_report.get(report_id, [])
        if not report_claims:
            continue
        series = (
            str(_get(report, "broker", "broker_code", default="")),
            _analyst(report),
            str(_get(report, "dimension", default="")),
            str(_get(report, "subject_id", "industry_id", default="")),
        )
        prior = prior_by_series.get(series)
        prior_claims = claims_by_report.get(str(_get(prior, "report_id", default="")), []) if prior else []
        signals = [claim_signal(claim) for claim in report_claims]
        mean_signal = sum(signals) / len(signals)
        skill_values: list[float] = []
        for claim in report_claims:
            report_id = str(_get(claim, "report_id", default=""))
            uncontaminated_snapshots = [
                snapshot
                for snapshot in snapshot_values
                if report_id
                not in {
                    str(source_report_id)
                    for source_report_id in (
                        _get(snapshot, "source_report_ids", default=()) or ()
                    )
                }
            ]
            snapshot = select_skill_snapshot(
                uncontaminated_snapshots,
                as_of=_datetime(_get(claim, "available_at"), end_of_day=True),
                claim=claim,
                report=report,
            )
            if snapshot is not None:
                value = _float(_get(snapshot, "conservative_lower_bound"))
                if value is not None:
                    skill_values.append(value)
        skill_lower = max(skill_values) if skill_values else 0.0
        subject = str(_get(report, "subject_id", "industry_id", default=""))
        dimension = str(_get(report, "dimension", default=""))
        objective_record = resolved_objectives.get((dimension, subject))
        if objective_record is None:
            objective_record = resolved_objectives.get(subject)
        if isinstance(objective_record, Mapping):
            objective_available = _get(objective_record, "available_at")
            if objective_available is None or _datetime(objective_available, end_of_day=True) > decision:
                objective = None
            else:
                objective = _float(_get(objective_record, "value", "signal", "score"))
        else:
            objective = _float(objective_record)
        conflict = abs(mean_signal - objective) if objective is not None and mean_signal * objective < 0 else 0.0
        sensitivity = _float((decision_sensitivity or {}).get(report_id))
        if sensitivity is None:
            sensitivity = abs(mean_signal) * max(0.0, 2.0 * skill_lower - 1.0)
        change = _report_change(report, prior, report_claims, prior_claims)
        explicit = _explicitness(report_claims)
        evidence_complete = sum(
            1.0
            for claim in report_claims
            if str(_get(claim, "evidence_span", default="")).strip()
            and (_float(_get(claim, "extraction_confidence")) or 0.0) >= 0.95
        ) / len(report_claims)
        sort_key = (sensitivity, conflict, change, skill_lower, explicit, evidence_complete)
        reasons: list[str] = []
        if sensitivity > 0:
            reasons.append("可能改变当前行业或个股排序")
        if conflict > 0:
            reasons.append("与客观数据方向冲突，需核对证据")
        if change > 0:
            reasons.append("相对上一份报告出现评级、数值或方向变化")
        if skill_lower > 0:
            reasons.append("来源历史技能保守下界可验证")
        enriched.append(
            {
                "report_id": report_id,
                "title": str(_get(report, "title", default="")),
                "broker": str(_get(report, "broker", "broker_code", default="")),
                "analyst": _analyst(report),
                "dimension": dimension,
                "subject_id": subject,
                "available_at": _get(report, "available_at", "published_at"),
                "sort_key": sort_key,
                "decision_sensitivity": sensitivity,
                "conflict": conflict,
                "changed": bool(change),
                "skill_lower_bound": skill_lower,
                "explicitness": explicit,
                "evidence_completeness": evidence_complete,
                "why_worth_read": "；".join(reasons) or "预测明确但当前决策影响有限",
                "may_change": f"{dimension or '研究'}层的{subject or '当前'}判断",
                # Reporting-compatible names; these are aliases, not a score.
                "why_read": "；".join(reasons) or "预测明确但当前决策影响有限",
                "might_change": f"{dimension or '研究'}层的{subject or '当前'}判断",
                "conflict_degree": conflict,
                "change_degree": change,
                "source_skill_lower_bound": skill_lower,
                "falsifiability": explicit,
                "source_url": str(_get(report, "source_url", "pdf_url", default="")),
                "_mean_signal": mean_signal,
            }
        )
        prior_by_series[series] = report

    high_skill_by_subject: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in enriched:
        if float(item["skill_lower_bound"]) > 0.5:
            high_skill_by_subject[(item["dimension"], item["subject_id"])].append(item)
    for item in enriched:
        peers = [
            peer
            for peer in high_skill_by_subject[(item["dimension"], item["subject_id"])]
            if peer["report_id"] != item["report_id"]
        ]
        weights = [max(0.0, 2.0 * float(peer["skill_lower_bound"]) - 1.0) for peer in peers]
        total_weight = sum(weights)
        if total_weight > 0:
            consensus = sum(
                float(peer["_mean_signal"]) * weight
                for peer, weight in zip(peers, weights)
            ) / total_weight
            report_signal = float(item["_mean_signal"])
            if report_signal * consensus < 0:
                consensus_conflict = abs(report_signal - consensus)
                if consensus_conflict > float(item["conflict_degree"]):
                    item["conflict"] = consensus_conflict
                    item["conflict_degree"] = consensus_conflict
                    item["sort_key"] = (
                        item["sort_key"][0],
                        consensus_conflict,
                        *item["sort_key"][2:],
                    )
                    reason = "与其他高技能来源方向冲突，需核对分歧证据"
                    item["why_read"] = f"{item['why_read']}；{reason}"
                    item["why_worth_read"] = f"{item['why_worth_read']}；{reason}"

    # Latest unchanged report wins within a duplicate current-reading episode.
    latest_by_signature: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in enriched:
        signature = (
            item["broker"],
            item["analyst"],
            item["dimension"],
            item["subject_id"],
            tuple(round(float(value), 10) for value in item["sort_key"][1:]),
        )
        latest_by_signature[signature] = item
    ranked = list(latest_by_signature.values())
    ranked.sort(
        key=lambda item: (
            tuple(item["sort_key"]),
            _datetime(item["available_at"], end_of_day=True),
            item["report_id"],
        ),
        reverse=True,
    )
    selected = ranked[:limit]
    for item in selected:
        item.pop("_mean_signal", None)
    return selected


def _add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    month_days = (31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    return date(year, month, min(value.day, month_days[month - 1]))


def _purge_cutoff(
    boundary: date,
    trading_calendar: Sequence[date],
    sessions: int,
) -> date | None:
    prior_sessions = [day for day in trading_calendar if day < boundary]
    if sessions <= 0:
        return boundary
    if len(prior_sessions) < sessions:
        return None
    return prior_sessions[-sessions]


def walk_forward_splits(
    dates: Iterable[Any],
    *,
    train_months: int = 36,
    validation_months: int = 6,
    test_months: int = 6,
    step_months: int = 6,
    purge_embargo_days: int = 120,
    frozen_months: int = 12,
    trading_calendar: Iterable[Any] | None = None,
) -> list[dict[str, Any]]:
    """Create expanding walk-forward splits with label-overlap purging.

    The last ``frozen_months`` are never used to define development windows.
    Purging removes the final N trading dates from train and validation, so a
    120-day outcome cannot overlap the following segment.
    """

    parsed_dates: set[date] = set()
    for item in dates:
        raw = _get(item, "date", "as_of", "trade_date", default=item)
        try:
            parsed_dates.add(_datetime(raw).date())
        except (FactorError, TypeError, ValueError):
            continue
    unique_dates = sorted(parsed_dates)
    if not unique_dates:
        return []
    if trading_calendar is None:
        calendar_dates = unique_dates
        gaps = [
            (right - left).days
            for left, right in zip(unique_dates, unique_dates[1:])
        ]
        if gaps and sorted(gaps)[len(gaps) // 2] > 4:
            raise FactorError(
                "sparse observations require an explicit trading_calendar for session purge"
            )
    else:
        parsed_calendar: set[date] = set()
        for item in trading_calendar:
            raw = _get(item, "date", "as_of", "trade_date", default=item)
            try:
                parsed_calendar.add(_datetime(raw).date())
            except (FactorError, TypeError, ValueError):
                continue
        calendar_dates = sorted(parsed_calendar)
        if not calendar_dates:
            raise FactorError("trading_calendar must contain valid session dates")
    if min(train_months, validation_months, test_months, step_months) <= 0 or purge_embargo_days < 0:
        raise FactorError("walk-forward window parameters are invalid")
    frozen_start = _add_months(unique_dates[-1], -frozen_months) if frozen_months > 0 else unique_dates[-1] + timedelta(days=1)
    development = [day for day in unique_dates if day < frozen_start]
    if not development:
        return []

    splits: list[dict[str, Any]] = []
    origin = development[0]
    window = 0
    while True:
        train_start = _add_months(origin, window * step_months)
        train_end = _add_months(train_start, train_months)
        validation_end = _add_months(train_end, validation_months)
        test_end = _add_months(validation_end, test_months)
        if test_end > frozen_start:
            break
        train_raw = [day for day in development if train_start <= day < train_end]
        validation_raw = [day for day in development if train_end <= day < validation_end]
        test = [day for day in development if validation_end <= day < test_end]
        if purge_embargo_days:
            train_cutoff = _purge_cutoff(
                train_end, calendar_dates, purge_embargo_days
            )
            validation_cutoff = _purge_cutoff(
                validation_end, calendar_dates, purge_embargo_days
            )
            train = (
                [day for day in train_raw if day < train_cutoff]
                if train_cutoff is not None
                else []
            )
            validation = (
                [day for day in validation_raw if day < validation_cutoff]
                if validation_cutoff is not None
                else []
            )
        else:
            train = train_raw
            validation = validation_raw
        splits.append(
            {
                "window": window + 1,
                "train_dates": tuple(train),
                "validation_dates": tuple(validation),
                "test_dates": tuple(test),
                "train_period": (train_start, train_end),
                "validation_period": (train_end, validation_end),
                "test_period": (validation_end, test_end),
                "purge_embargo_days": purge_embargo_days,
            }
        )
        window += 1
    return splits


def _ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: (values[index], index))
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(order):
        end = cursor + 1
        while end < len(order) and values[order[end]] == values[order[cursor]]:
            end += 1
        average = (cursor + end - 1) / 2.0
        for position in range(cursor, end):
            ranks[order[position]] = average
        cursor = end
    return ranks


def rank_ic(predictions: Sequence[float], realized: Sequence[float]) -> float | None:
    if len(predictions) != len(realized) or len(predictions) < 2:
        return None
    left = _ranks(predictions)
    right = _ranks(realized)
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right))
    denominator = math.sqrt(
        sum((x - left_mean) ** 2 for x in left) * sum((y - right_mean) ** 2 for y in right)
    )
    return numerator / denominator if denominator else None


def mean_cross_sectional_rank_ic(
    predictions: Sequence[float],
    realized: Sequence[float],
    dates: Sequence[Any],
) -> float | None:
    """Mean same-date Rank IC, preventing market time trends from posing as alpha."""

    if not (len(predictions) == len(realized) == len(dates)):
        raise FactorError("predictions, realized and dates must have equal length")
    grouped: dict[date, list[int]] = defaultdict(list)
    for index, raw_date in enumerate(dates):
        grouped[_datetime(raw_date).date()].append(index)
    values: list[float] = []
    for indices in grouped.values():
        if len(indices) < 2:
            continue
        value = rank_ic(
            [predictions[index] for index in indices],
            [realized[index] for index in indices],
        )
        if value is not None:
            values.append(value)
    return sum(values) / len(values) if values else None


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    size = len(vector)
    augmented = [matrix[row][:] + [vector[row]] for row in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            augmented[pivot][column] = 1e-12
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            multiplier = augmented[row][column]
            augmented[row] = [
                value - multiplier * pivot_value
                for value, pivot_value in zip(augmented[row], augmented[column])
            ]
    return [augmented[row][-1] for row in range(size)]


def _standardize_fit(rows: Sequence[Sequence[float]]) -> tuple[list[float], list[float]]:
    columns = len(rows[0])
    means = [sum(row[column] for row in rows) / len(rows) for column in range(columns)]
    scales = []
    for column in range(columns):
        variance = sum((row[column] - means[column]) ** 2 for row in rows) / len(rows)
        scales.append(math.sqrt(variance) or 1.0)
    return means, scales


def _standardize(rows: Sequence[Sequence[float]], means: Sequence[float], scales: Sequence[float]) -> list[list[float]]:
    return [[(value - means[column]) / scales[column] for column, value in enumerate(row)] for row in rows]


def _ridge_fit(rows: Sequence[Sequence[float]], labels: Sequence[float], alpha: float) -> list[float]:
    augmented = [[1.0, *row] for row in rows]
    columns = len(augmented[0])
    gram = [[sum(row[i] * row[j] for row in augmented) for j in range(columns)] for i in range(columns)]
    for index in range(1, columns):  # do not regularize intercept
        gram[index][index] += alpha
    cross = [sum(row[index] * label for row, label in zip(augmented, labels)) for index in range(columns)]
    return _solve(gram, cross)


def _logistic_fit(rows: Sequence[Sequence[float]], labels: Sequence[float], alpha: float) -> list[float]:
    weights = [0.0] * (len(rows[0]) + 1)
    binary = [1.0 if label > 0 else 0.0 for label in labels]
    learning_rate = 0.1
    for _ in range(300):
        gradients = [0.0] * len(weights)
        for row, target in zip(rows, binary):
            augmented = [1.0, *row]
            linear = max(-35.0, min(35.0, sum(weight * value for weight, value in zip(weights, augmented))))
            probability = 1.0 / (1.0 + math.exp(-linear))
            for index, value in enumerate(augmented):
                gradients[index] += (probability - target) * value
        for index in range(len(weights)):
            penalty = 0.0 if index == 0 else alpha * weights[index]
            weights[index] -= learning_rate * (gradients[index] / len(rows) + penalty / len(rows))
        learning_rate *= 0.995
    return weights


def _predict(rows: Sequence[Sequence[float]], weights: Sequence[float], logistic: bool) -> list[float]:
    output = []
    for row in rows:
        linear = weights[0] + sum(weight * value for weight, value in zip(weights[1:], row))
        output.append(1.0 / (1.0 + math.exp(-max(-35.0, min(35.0, linear)))) if logistic else linear)
    return output


def _portfolio_metrics(
    predictions: Sequence[float],
    realized: Sequence[float],
    industries: Sequence[str],
    dates: Sequence[Any],
    cost_bps: float,
    stock_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    if not (len(predictions) == len(realized) == len(industries) == len(dates)):
        raise FactorError("portfolio inputs must have equal length")
    if stock_ids is not None and len(stock_ids) != len(predictions):
        raise FactorError("stock_ids must match portfolio input length")
    grouped: dict[date, list[int]] = defaultdict(list)
    for index, raw_date in enumerate(dates):
        grouped[_datetime(raw_date).date()].append(index)

    gross_by_date: list[float] = []
    net_by_date: list[float] = []
    contributions: dict[str, float] = defaultdict(float)
    evaluated_dates = 0
    turnovers: list[float] = []
    previous_positions: dict[str, float] = {}
    for trade_day in sorted(grouped):
        indices = grouped[trade_day]
        if len(indices) < 2:
            continue
        bucket = max(1, len(indices) // 5)
        order = sorted(indices, key=lambda index: (predictions[index], index))
        short_indices = order[:bucket]
        long_indices = order[-bucket:]
        daily_gross = (
            sum(realized[index] for index in long_indices) / len(long_indices)
            - sum(realized[index] for index in short_indices) / len(short_indices)
        )
        if stock_ids is None:
            # Compatibility for callers that do not provide security identity.
            # The research pipeline always supplies IDs and uses real turnover.
            turnover = 2.0
        else:
            current_positions: dict[str, float] = defaultdict(float)
            for index in long_indices:
                current_positions[str(stock_ids[index])] += 1.0 / len(long_indices)
            for index in short_indices:
                current_positions[str(stock_ids[index])] -= 1.0 / len(short_indices)
            turnover = sum(
                abs(current_positions.get(stock_id, 0.0) - previous_positions.get(stock_id, 0.0))
                for stock_id in set(current_positions) | set(previous_positions)
            )
            previous_positions = dict(current_positions)
        daily_net = daily_gross - turnover * cost_bps / 10000.0
        gross_by_date.append(daily_gross)
        net_by_date.append(daily_net)
        turnovers.append(turnover)
        evaluated_dates += 1
        for index in long_indices:
            contributions[industries[index]] += realized[index] / len(long_indices)
        for index in short_indices:
            contributions[industries[index]] -= realized[index] / len(short_indices)

    gross = sum(gross_by_date) / len(gross_by_date) if gross_by_date else None
    net = sum(net_by_date) / len(net_by_date) if net_by_date else None
    total_absolute = sum(abs(value) for value in contributions.values())
    concentration = max((abs(value) for value in contributions.values()), default=0.0) / total_absolute if total_absolute else 1.0
    return {
        "gross_group_return": gross,
        "cost_after_group_return": net,
        "max_industry_contribution_share": concentration,
        "industry_contributions": dict(sorted(contributions.items())),
        "portfolio_rebalance_count": evaluated_dates,
        "average_one_way_turnover": (
            sum(turnovers) / len(turnovers) if turnovers else None
        ),
        "maximum_one_way_turnover": max(turnovers, default=None),
    }


def admission_decision(
    window_results: Sequence[Mapping[str, Any]],
    *,
    min_windows: int = 4,
    min_incremental_windows: int = 3,
    max_industry_contribution_share: float = 0.50,
) -> dict[str, Any]:
    reasons: list[str] = []
    valid = [
        window
        for window in window_results
        if isinstance(window.get("M1"), Mapping)
        and window["M1"].get("status", "evaluated") == "evaluated"
        and window["M1"].get("rank_ic") is not None
    ]
    if len(valid) < min_windows:
        reasons.append(f"insufficient_oos_windows:{len(valid)}<{min_windows}")
    m1_ics = [float(window["M1"]["rank_ic"]) for window in valid if window["M1"].get("rank_ic") is not None]
    mean_ic = sum(m1_ics) / len(m1_ics) if m1_ics else None
    if mean_ic is None or mean_ic <= 0:
        reasons.append("mean_m1_rank_ic_not_positive")
    incremental = 0
    for window in valid:
        m1_ic = window["M1"].get("rank_ic")
        baselines = [window.get(name, {}).get("rank_ic") for name in ("B0", "B1", "B2") if isinstance(window.get(name), Mapping)]
        baselines = [float(value) for value in baselines if value is not None]
        if m1_ic is not None and baselines and float(m1_ic) > max(baselines):
            incremental += 1
    if incremental < min_incremental_windows:
        reasons.append(f"incremental_windows:{incremental}<{min_incremental_windows}")
    concentrations = [
        float(window["M1"].get("max_industry_contribution_share", 1.0)) for window in valid
    ]
    max_concentration = max(concentrations, default=1.0)
    if max_concentration > max_industry_contribution_share:
        reasons.append("single_industry_contribution_too_high")
    net_returns = [float(window["M1"].get("cost_after_group_return", 0.0)) for window in valid]
    if not net_returns or sum(net_returns) / len(net_returns) <= 0:
        reasons.append("mean_cost_after_group_return_not_positive")
    return {
        "status": "admitted" if not reasons else "not_admitted",
        "admitted": not reasons,
        "reasons": reasons,
        "window_count": len(valid),
        "mean_m1_rank_ic": mean_ic,
        "incremental_window_count": incremental,
        "max_industry_contribution_share": max_concentration,
    }


def walk_forward_evaluate(
    rows: Iterable[Any],
    *,
    label_field: str = "target_return_20d",
    date_field: str = "as_of",
    industry_field: str = "industry_id",
    model: str = "ridge",
    alphas: Sequence[float] = (0.1, 1.0, 10.0),
    cost_bps: float = 10.0,
    train_months: int = 36,
    validation_months: int = 6,
    test_months: int = 6,
    step_months: int = 6,
    purge_embargo_days: int = 120,
    frozen_months: int = 12,
    trading_calendar: Iterable[Any] | None = None,
    admission_evidence_verified: bool = False,
    rebalance_frequency: str = "weekly",
    minimum_stocks_per_rebalance_date: int = 2,
    minimum_industries_per_rebalance_date: int = 2,
    minimum_train_rebalance_dates: int = 1,
    minimum_validation_rebalance_dates: int = 1,
    minimum_test_rebalance_dates: int = 1,
) -> dict[str, Any]:
    """Low-DOF Ridge/Logistic rolling OOS comparison for B0/B1/B2/M1."""

    if admission_evidence_verified is not False:
        raise FactorError(
            "a caller-supplied boolean cannot certify admission evidence; "
            "formal admission requires a store-backed provenance verifier"
        )
    if model not in {"ridge", "logistic"}:
        raise FactorError("model must be 'ridge' or 'logistic'")
    if not alphas or any(float(alpha) < 0 for alpha in alphas):
        raise FactorError("alphas must be a non-empty sequence of non-negative values")
    if rebalance_frequency != "weekly":
        raise FactorError("V1 rebalance_frequency is frozen to weekly")
    sample_minima = (
        minimum_stocks_per_rebalance_date,
        minimum_industries_per_rebalance_date,
        minimum_train_rebalance_dates,
        minimum_validation_rebalance_dates,
        minimum_test_rebalance_dates,
    )
    if any(int(value) <= 0 for value in sample_minima):
        raise FactorError("all preregistered sample minimums must be positive")
    materialized = list(rows)
    calendar_values = list(trading_calendar) if trading_calendar is not None else None
    # One and only one cross-section is allowed per ISO week.  With an
    # exchange calendar this is the actual final session of the week; dense
    # diagnostic input without a calendar uses its last observed date.
    observed_dates = sorted(
        {
            _datetime(_get(row, date_field)).date()
            for row in materialized
            if _get(row, date_field) is not None
        }
    )
    if calendar_values is None:
        observed_gaps = [
            (right - left).days
            for left, right in zip(observed_dates, observed_dates[1:])
        ]
        if not observed_gaps or sorted(observed_gaps)[len(observed_gaps) // 2] <= 4:
            # Dense diagnostic inputs can supply their own observed sessions
            # for purge arithmetic. Sparse weekly rows still fail closed.
            calendar_values = list(observed_dates)
    weekly_session: dict[tuple[int, int], date] = {}
    weekly_source = (
        sorted({_datetime(_get(item, "date", "trade_date", "as_of", default=item)).date() for item in calendar_values})
        if calendar_values
        else observed_dates
    )
    for day in weekly_source:
        iso = day.isocalendar()
        weekly_session[(iso.year, iso.week)] = day
    materialized = [
        row
        for row in materialized
        if (
            (lambda day: weekly_session.get((day.isocalendar().year, day.isocalendar().week)) == day)(
                _datetime(_get(row, date_field)).date()
            )
        )
    ]
    splits = walk_forward_splits(
        [_get(row, date_field) for row in materialized if _get(row, date_field) is not None],
        train_months=train_months,
        validation_months=validation_months,
        test_months=test_months,
        step_months=step_months,
        purge_embargo_days=purge_embargo_days,
        frozen_months=frozen_months,
        trading_calendar=calendar_values,
    )
    if not splits:
        decision = admission_decision([])
        decision["evidence_verified"] = bool(admission_evidence_verified)
        if not admission_evidence_verified:
            decision["reasons"] = list(
                dict.fromkeys(
                    [
                        *decision["reasons"],
                        "external_or_unrecomputed_labels_are_diagnostic_only",
                    ]
                )
            )
        return {"status": "not_admitted", "reason": "insufficient_date_span", "windows": [], "admission": decision}

    dated_rows: dict[date, list[Any]] = defaultdict(list)
    for row in materialized:
        value = _get(row, date_field)
        if value is not None:
            dated_rows[_datetime(value).date()].append(row)

    all_feature_names = tuple(
        sorted({name for names in MODEL_FEATURES.values() for name in names})
    )

    def common_samples(
        values: Sequence[Any],
    ) -> list[tuple[Any, float, str, date, str]]:
        """Use one complete-case universe for every competing model."""

        candidates: list[tuple[Any, float, str, date, str]] = []
        for row in values:
            label = _float(_get(row, label_field))
            industry = str(_get(row, industry_field, default="")).strip()
            stock_id = str(_get(row, "stock_id", default="")).strip()
            if label is None or any(
                _float(_get(row, feature_name)) is None
                for feature_name in all_feature_names
            ) or not stock_id or not industry or industry.upper() == "UNKNOWN":
                continue
            candidates.append(
                (
                    row,
                    label,
                    industry,
                    _datetime(_get(row, date_field)).date(),
                    stock_id,
                )
            )
        grouped: dict[date, list[tuple[Any, float, str, date, str]]] = defaultdict(list)
        for sample in candidates:
            grouped[sample[3]].append(sample)
        samples: list[tuple[Any, float, str, date, str]] = []
        for day in sorted(grouped):
            cross_section = grouped[day]
            stock_ids = [sample[4] for sample in cross_section]
            industries = {sample[2] for sample in cross_section}
            if (
                len(stock_ids) != len(set(stock_ids))
                or len(stock_ids) < int(minimum_stocks_per_rebalance_date)
                or len(industries) < int(minimum_industries_per_rebalance_date)
            ):
                continue
            samples.extend(cross_section)
        return samples

    def model_samples(
        samples: Sequence[tuple[Any, float, str, date, str]],
        feature_names: Sequence[str],
    ) -> list[tuple[list[float], float, str, date, str]]:
        return [
            (
                [float(_float(_get(row, name))) for name in feature_names],
                label,
                industry,
                sample_date,
                stock_id,
            )
            for row, label, industry, sample_date, stock_id in samples
        ]

    window_results: list[dict[str, Any]] = []
    for split in splits:
        result: dict[str, Any] = {"window": split["window"], "periods": {key: split[key] for key in ("train_period", "validation_period", "test_period")}}
        segment_rows = {
            name: [row for day in split[f"{name}_dates"] for row in dated_rows.get(day, [])]
            for name in ("train", "validation", "test")
        }
        common_prepared = {
            name: common_samples(values) for name, values in segment_rows.items()
        }
        minimum_dates = {
            "train": int(minimum_train_rebalance_dates),
            "validation": int(minimum_validation_rebalance_dates),
            "test": int(minimum_test_rebalance_dates),
        }
        segment_date_counts = {
            name: len({sample[3] for sample in samples})
            for name, samples in common_prepared.items()
        }
        for feature_set in ("B0", "B1", "B2", "M1"):
            names = MODEL_FEATURES[feature_set]
            prepared = {
                segment: model_samples(samples, names)
                for segment, samples in common_prepared.items()
            }
            failing_segments = [
                name
                for name in ("train", "validation", "test")
                if segment_date_counts[name] < minimum_dates[name]
            ]
            if failing_segments:
                result[feature_set] = {
                    "status": "not_evaluated",
                    "reason": "insufficient_preregistered_rebalance_dates",
                    "rebalance_date_counts": segment_date_counts,
                    "minimum_rebalance_dates": minimum_dates,
                }
                continue
            train_x = [sample[0] for sample in prepared["train"]]
            train_y = [sample[1] for sample in prepared["train"]]
            means, scales = _standardize_fit(train_x)
            train_scaled = _standardize(train_x, means, scales)
            validation_scaled = _standardize([sample[0] for sample in prepared["validation"]], means, scales)
            validation_y = [sample[1] for sample in prepared["validation"]]
            logistic = model == "logistic"
            candidates: list[tuple[float, float, list[float]]] = []
            for alpha in alphas:
                weights = _logistic_fit(train_scaled, train_y, float(alpha)) if logistic else _ridge_fit(train_scaled, train_y, float(alpha))
                predictions = _predict(validation_scaled, weights, logistic)
                score = mean_cross_sectional_rank_ic(
                    predictions,
                    validation_y,
                    [sample[3] for sample in prepared["validation"]],
                )
                candidates.append((float(score) if score is not None else -math.inf, -float(alpha), weights))
            candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
            selected_score, negative_alpha, _ = candidates[0]
            selected_alpha = -negative_alpha
            combined = prepared["train"] + prepared["validation"]
            combined_x = [sample[0] for sample in combined]
            combined_y = [sample[1] for sample in combined]
            final_means, final_scales = _standardize_fit(combined_x)
            combined_scaled = _standardize(combined_x, final_means, final_scales)
            weights = _logistic_fit(combined_scaled, combined_y, selected_alpha) if logistic else _ridge_fit(combined_scaled, combined_y, selected_alpha)
            test_scaled = _standardize([sample[0] for sample in prepared["test"]], final_means, final_scales)
            test_y = [sample[1] for sample in prepared["test"]]
            predictions = _predict(test_scaled, weights, logistic)
            ic = mean_cross_sectional_rank_ic(
                predictions,
                test_y,
                [sample[3] for sample in prepared["test"]],
            )
            metrics = _portfolio_metrics(
                predictions,
                test_y,
                [sample[2] for sample in prepared["test"]],
                [sample[3] for sample in prepared["test"]],
                cost_bps,
                [sample[4] for sample in prepared["test"]],
            )
            if ic is None or int(metrics.get("portfolio_rebalance_count", 0)) == 0:
                result[feature_set] = {
                    "status": "not_evaluated",
                    "reason": "insufficient_cross_sectional_test_dates",
                    "train_count": len(prepared["train"]),
                    "validation_count": len(prepared["validation"]),
                    "test_count": len(prepared["test"]),
                }
                continue
            result[feature_set] = {
                "status": "evaluated",
                "rank_ic": ic,
                "validation_rank_ic": None if selected_score == -math.inf else selected_score,
                "alpha": selected_alpha,
                "train_count": len(prepared["train"]),
                "validation_count": len(prepared["validation"]),
                "test_count": len(prepared["test"]),
                **metrics,
            }
        window_results.append(result)
    # Final continuous 12-month holdout.  Alpha is selected only from earlier
    # validation windows (or the preregistered first alpha when none completed);
    # frozen labels never participate in model or hyperparameter selection.
    all_dates = sorted(dated_rows)
    if calendar_values is None:
        calendar_dates = all_dates
    else:
        calendar_dates = sorted(
            {
                _datetime(
                    _get(item, "date", "as_of", "trade_date", default=item)
                ).date()
                for item in calendar_values
            }
        )
    frozen_result: dict[str, Any] = {"window": "frozen_12m", "frozen": True}
    if all_dates and frozen_months > 0:
        frozen_start = _add_months(all_dates[-1], -frozen_months)
        frozen_train_start = _add_months(frozen_start, -train_months)
        development_dates = [
            day for day in all_dates if frozen_train_start <= day < frozen_start
        ]
        frozen_dates = [day for day in all_dates if day >= frozen_start]
        if purge_embargo_days:
            frozen_train_cutoff = _purge_cutoff(
                frozen_start, calendar_dates, purge_embargo_days
            )
            development_dates = [
                day
                for day in development_dates
                if frozen_train_cutoff is not None and day < frozen_train_cutoff
            ]
        frozen_result["periods"] = {
            "train_period": (
                development_dates[0] if development_dates else None,
                development_dates[-1] if development_dates else None,
            ),
            "test_period": (
                frozen_dates[0] if frozen_dates else None,
                frozen_dates[-1] if frozen_dates else None,
            ),
        }
        development_rows = [row for day in development_dates for row in dated_rows.get(day, [])]
        frozen_rows = [row for day in frozen_dates for row in dated_rows.get(day, [])]
        common_train_samples = common_samples(development_rows)
        common_test_samples = common_samples(frozen_rows)
        frozen_train_dates = len({sample[3] for sample in common_train_samples})
        frozen_test_dates = len({sample[3] for sample in common_test_samples})
        for feature_set in ("B0", "B1", "B2", "M1"):
            names = MODEL_FEATURES[feature_set]
            train_samples = model_samples(common_train_samples, names)
            test_samples = model_samples(common_test_samples, names)
            if (
                frozen_train_dates < int(minimum_train_rebalance_dates)
                or frozen_test_dates < int(minimum_test_rebalance_dates)
            ):
                frozen_result[feature_set] = {
                    "status": "not_evaluated",
                    "reason": "insufficient_preregistered_rebalance_dates",
                    "rebalance_date_counts": {
                        "train": frozen_train_dates,
                        "test": frozen_test_dates,
                    },
                }
                continue
            prior_alpha_scores: dict[float, list[float]] = defaultdict(list)
            for window in window_results:
                metrics = window.get(feature_set)
                if not isinstance(metrics, Mapping) or metrics.get("status") != "evaluated":
                    continue
                alpha_value = _float(metrics.get("alpha"))
                validation_score = _float(metrics.get("validation_rank_ic"))
                if alpha_value is not None and validation_score is not None:
                    prior_alpha_scores[alpha_value].append(validation_score)
            if prior_alpha_scores:
                selected_alpha = max(
                    prior_alpha_scores,
                    key=lambda alpha: (
                        sum(prior_alpha_scores[alpha]) / len(prior_alpha_scores[alpha]),
                        -alpha,
                    ),
                )
                alpha_source = "development_validation_windows"
            else:
                selected_alpha = float(alphas[0])
                alpha_source = "preregistered_default"
            train_x = [sample[0] for sample in train_samples]
            train_y = [sample[1] for sample in train_samples]
            means, scales = _standardize_fit(train_x)
            train_scaled = _standardize(train_x, means, scales)
            logistic = model == "logistic"
            weights = (
                _logistic_fit(train_scaled, train_y, selected_alpha)
                if logistic
                else _ridge_fit(train_scaled, train_y, selected_alpha)
            )
            test_scaled = _standardize([sample[0] for sample in test_samples], means, scales)
            test_y = [sample[1] for sample in test_samples]
            predictions = _predict(test_scaled, weights, logistic)
            frozen_ic = mean_cross_sectional_rank_ic(
                predictions,
                test_y,
                [sample[3] for sample in test_samples],
            )
            frozen_portfolio = _portfolio_metrics(
                predictions,
                test_y,
                [sample[2] for sample in test_samples],
                [sample[3] for sample in test_samples],
                cost_bps,
                [sample[4] for sample in test_samples],
            )
            if (
                frozen_ic is None
                or int(frozen_portfolio.get("portfolio_rebalance_count", 0)) == 0
            ):
                frozen_result[feature_set] = {
                    "status": "not_evaluated",
                    "reason": "insufficient_cross_sectional_test_dates",
                    "train_count": len(train_samples),
                    "validation_count": 0,
                    "test_count": len(test_samples),
                }
                continue
            frozen_result[feature_set] = {
                "status": "evaluated",
                "rank_ic": frozen_ic,
                "alpha": selected_alpha,
                "alpha_source": alpha_source,
                "train_count": len(train_samples),
                "validation_count": 0,
                "test_count": len(test_samples),
                **frozen_portfolio,
            }
    else:
        for feature_set in ("B0", "B1", "B2", "M1"):
            frozen_result[feature_set] = {
                "status": "not_evaluated",
                "reason": "frozen_test_disabled_or_missing_dates",
            }

    # The final frozen twelve months are an additional holdout gate.  They do
    # not count as one of the four rolling development OOS windows.
    admission = admission_decision(window_results)
    frozen_m1 = frozen_result.get("M1")
    if not isinstance(frozen_m1, Mapping) or frozen_m1.get("status") != "evaluated":
        admission["reasons"].append("frozen_test_not_evaluated")
    else:
        frozen_ic = _float(frozen_m1.get("rank_ic"))
        if frozen_ic is None or frozen_ic <= 0:
            admission["reasons"].append("frozen_m1_rank_ic_not_positive")
        frozen_baselines = [
            _float(frozen_result.get(name, {}).get("rank_ic"))
            for name in ("B0", "B1", "B2")
            if isinstance(frozen_result.get(name), Mapping)
            and frozen_result[name].get("status") == "evaluated"
        ]
        frozen_baselines = [value for value in frozen_baselines if value is not None]
        if not frozen_baselines or frozen_ic is None or frozen_ic <= max(frozen_baselines):
            admission["reasons"].append("frozen_m1_not_incremental")
        if float(frozen_m1.get("cost_after_group_return", 0.0)) <= 0:
            admission["reasons"].append("frozen_cost_after_group_return_not_positive")
        if float(frozen_m1.get("max_industry_contribution_share", 1.0)) > 0.50:
            admission["reasons"].append("frozen_single_industry_contribution_too_high")
    admission["reasons"] = list(dict.fromkeys(admission["reasons"]))
    if not admission_evidence_verified:
        admission["reasons"].append(
            "external_or_unrecomputed_labels_are_diagnostic_only"
        )
        admission["evidence_verified"] = False
    else:
        admission["evidence_verified"] = True
    admission["reasons"] = list(dict.fromkeys(admission["reasons"]))
    if admission["reasons"]:
        admission["admitted"] = False
        admission["status"] = "not_admitted"
    return {
        "status": admission["status"],
        "model": model,
        "feature_sets": MODEL_FEATURES,
        "purge_embargo_days": purge_embargo_days,
        "frozen_months": frozen_months,
        "rebalance_frequency": rebalance_frequency,
        "sample_gates": {
            "minimum_stocks_per_rebalance_date": int(minimum_stocks_per_rebalance_date),
            "minimum_industries_per_rebalance_date": int(minimum_industries_per_rebalance_date),
            "minimum_train_rebalance_dates": int(minimum_train_rebalance_dates),
            "minimum_validation_rebalance_dates": int(minimum_validation_rebalance_dates),
            "minimum_test_rebalance_dates": int(minimum_test_rebalance_dates),
        },
        "windows": window_results,
        "frozen_test": frozen_result,
        "admission": admission,
    }


__all__ = [
    "FACTOR_ROW_CONTRACT_VERSION",
    "INTERNAL_LABEL_CONTRACT_VERSION",
    "MODEL_FEATURES",
    "STOCK_REPORT_FACTOR_TARGET_TYPES",
    "FactorError",
    "InternalFactorResearchBatch",
    "admission_decision",
    "build_factor_components",
    "build_factor_observation",
    "build_factor_observations",
    "build_internal_factor_research_rows",
    "build_model_feature_sets",
    "claim_signal",
    "rank_deep_reads",
    "rank_ic",
    "validate_walk_forward_input_rows",
    "validate_internal_factor_research_batch",
    "mean_cross_sectional_rank_ic",
    "walk_forward_evaluate",
    "walk_forward_splits",
]
