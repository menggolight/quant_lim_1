"""Leakage-safe, hierarchically shrunk broker/analyst skill estimates."""

from __future__ import annotations

import hashlib
import inspect
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

try:
    from .models import SkillSnapshot
except (ImportError, AttributeError):  # pragma: no cover - compatibility path
    SkillSnapshot = None  # type: ignore[assignment,misc]


CHINA_TZ = timezone(timedelta(hours=8))


class SkillEstimationError(ValueError):
    """Raised for invalid skill-estimation inputs."""


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


def _datetime(value: Any, *, end_of_day: bool = False) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, time.max if end_of_day else time.min)
    else:
        text = str(value or "").strip()
        if not text:
            raise SkillEstimationError("timestamp must not be empty")
        if "T" not in text and " " not in text:
            result = datetime.combine(date.fromisoformat(text[:10]), time.max if end_of_day else time.min)
        else:
            result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=CHINA_TZ)
    return result


def _analyst(record: Any) -> str:
    value = _get(record, "analyst", "analysts", default="")
    if isinstance(value, (list, tuple, set)):
        return "|".join(sorted(str(item).strip() for item in value if str(item).strip()))
    return str(value or "").strip()


def _broker_identity(report: Any) -> str:
    """Use publication-time orgCode, then family id, then raw display text."""

    metadata = _get(report, "metadata", default={})
    family_id = (
        str(metadata.get("_broker_family_id") or "").strip()
        if isinstance(metadata, Mapping)
        else ""
    )
    # Source orgCode identifies the publication-time legal/process entity and
    # must not be collapsed by a later merger-family alias.
    broker_code = str(_get(report, "broker_code", default="")).strip()
    return broker_code or family_id or str(_get(report, "broker", default="")).strip()


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, ArithmeticError):
        return None
    return number if math.isfinite(number) else None


def _construct(payload: dict[str, Any]) -> Any:
    if SkillSnapshot is None:
        return payload
    try:
        parameters = inspect.signature(SkillSnapshot).parameters
        return SkillSnapshot(**{key: value for key, value in payload.items() if key in parameters})
    except (TypeError, ValueError):
        return payload


def time_decay_weight(
    event_time: Any,
    as_of: Any,
    half_life_days: float = 730.0,
) -> float:
    """Exponential half-life weight; future events are rejected."""

    if half_life_days <= 0:
        raise SkillEstimationError("half_life_days must be positive")
    event = _datetime(event_time, end_of_day=True)
    decision = _datetime(as_of, end_of_day=True)
    age_days = (decision - event).total_seconds() / 86400.0
    if age_days < 0:
        raise SkillEstimationError("future outcome cannot update skill")
    return 0.5 ** (age_days / half_life_days)


def _event_time(record: Any) -> datetime:
    value = _get(record, "truth_available_at", "evaluated_at", "event_time", "available_at")
    if value is None:
        raise SkillEstimationError("skill record has no point-in-time event timestamp")
    return _datetime(value, end_of_day=True)


def _consensus_key(record: Any) -> tuple[Any, ...]:
    # Correlation is created when opinions are published together, not when a
    # shared realised value is released months later.  Falling back to the
    # truth timestamp keeps legacy aggregate records usable but never merges
    # distinct publication dates in the normal claim/report path.
    opinion_time = _get(record, "available_at", "claim_available_at")
    event = (
        _datetime(opinion_time, end_of_day=True)
        if opinion_time is not None
        else _event_time(record)
    )
    return (
        str(_get(record, "subject_id", default="")),
        str(_get(record, "dimension", default="")),
        str(_get(record, "target_type", default="")),
        int(_get(record, "horizon_days", default=0) or 0),
        int(_get(record, "direction", default=0) or 0),
        event.date().isoformat(),
    )


def estimate_skill(
    records: Iterable[Any],
    *,
    as_of: Any,
    half_life_days: float = 730.0,
    sensitivity_half_life_days: float = 365.0,
    lookback_years: float = 5.0,
    prior_mean: float = 0.5,
    prior_strength: float = 5.0,
    lower_bound_z: float = 1.645,
    consensus_power: float = 1.0,
    precomputed_consensus_weight_field: str | None = None,
) -> dict[str, Any]:
    """Estimate weighted Bernoulli skill with empirical-Bayes shrinkage.

    ``consensus_power=1`` makes all same-day, same-subject, same-direction
    reports contribute one unit in total, rather than pretending they are
    independent replications.  The returned 365-day estimate is a mandatory
    sensitivity diagnostic, not a separately selectable best result.
    """

    decision = _datetime(as_of, end_of_day=True)
    if not 0.0 <= prior_mean <= 1.0:
        raise SkillEstimationError("prior_mean must lie in [0, 1]")
    if prior_strength < 0 or lookback_years <= 0 or consensus_power < 0:
        raise SkillEstimationError("prior_strength/lookback_years/consensus_power are invalid")
    cutoff = decision - timedelta(days=365.25 * lookback_years)

    valid: list[Any] = []
    for record in records:
        if not bool(_get(record, "mature", default=True)):
            continue
        hit = _get(record, "hit")
        if hit is None:
            continue
        try:
            event = _event_time(record)
        except SkillEstimationError:
            continue
        if cutoff <= event < decision:
            valid.append(record)
    counts = Counter(_consensus_key(record) for record in valid)

    def calculate(half_life: float) -> tuple[float, float, float, list[float]]:
        weighted_hits = 0.0
        total_weight = 0.0
        weights: list[float] = []
        cluster_weights: dict[tuple[Any, ...], list[float]] = defaultdict(list)
        for record in valid:
            cluster_size = max(1, counts[_consensus_key(record)])
            if precomputed_consensus_weight_field:
                raw_discount = _get(
                    record, precomputed_consensus_weight_field, default=None
                )
                try:
                    correlation_discount = float(raw_discount)
                except (TypeError, ValueError) as exc:
                    raise SkillEstimationError(
                        "precomputed consensus weight must be numeric"
                    ) from exc
                if not 0.0 < correlation_discount <= 1.0:
                    raise SkillEstimationError(
                        "precomputed consensus weight must lie in (0, 1]"
                    )
            else:
                correlation_discount = cluster_size ** (-consensus_power)
            weight = time_decay_weight(_event_time(record), decision, half_life) * correlation_discount
            hit_value = 1.0 if bool(_get(record, "hit")) else 0.0
            weighted_hits += weight * hit_value
            total_weight += weight
            weights.append(weight)
            cluster_weights[_consensus_key(record)].append(weight)
        posterior = (
            (weighted_hits + prior_strength * prior_mean) / (total_weight + prior_strength)
            if total_weight + prior_strength > 0
            else prior_mean
        )
        # Correlated reports form one effective cluster when consensus_power=1;
        # when consensus_power=0 they retain their independent effective count.
        squared_weight_mass = 0.0
        for grouped_weights in cluster_weights.values():
            cluster_total = sum(grouped_weights)
            cluster_size = len(grouped_weights)
            cluster_effective_count = cluster_size ** (1.0 - consensus_power)
            squared_weight_mass += cluster_total * cluster_total / max(1.0, cluster_effective_count)
        kish_effective_n = (
            total_weight * total_weight / squared_weight_mass
            if squared_weight_mass
            else 0.0
        )
        # Kish ESS alone is invariant to a uniform time-decay scale.  Capping
        # it by discounted weight mass preserves both correlation and staleness
        # penalties as information loss.
        effective_n = min(kish_effective_n, total_weight)
        information = effective_n + prior_strength
        if information <= 0:
            lower = 0.0
        else:
            z2 = lower_bound_z * lower_bound_z
            denominator = 1.0 + z2 / information
            centre = posterior + z2 / (2.0 * information)
            radius = lower_bound_z * math.sqrt(
                max(0.0, posterior * (1.0 - posterior) / information + z2 / (4.0 * information * information))
            )
            lower = max(0.0, (centre - radius) / denominator)
        return posterior, lower, effective_n, weights

    posterior, lower, effective_n, weights = calculate(half_life_days)
    sensitivity, sensitivity_lower, sensitivity_n, _ = calculate(sensitivity_half_life_days)
    return {
        "posterior_skill": posterior,
        "conservative_lower_bound": lower,
        "effective_sample_size": effective_n,
        "raw_observation_count": len(valid),
        "total_weight": sum(weights),
        "prior_mean": prior_mean,
        "prior_strength": prior_strength,
        "half_life_days": half_life_days,
        "sensitivity_365": sensitivity,
        "sensitivity_365_lower_bound": sensitivity_lower,
        "sensitivity_365_effective_sample_size": sensitivity_n,
        "sensitivity_delta": sensitivity - posterior,
        "source_report_ids": tuple(
            sorted({str(_get(record, "report_id", default="")) for record in valid if _get(record, "report_id")})
        ),
    }


def _pool_estimate(
    records: list[dict[str, Any]],
    prior: float,
    strength: float,
    as_of: datetime,
    *,
    half_life_days: float,
    sensitivity_half_life_days: float,
    lookback_years: float,
) -> float:
    return float(
        estimate_skill(
            records,
            as_of=as_of,
            half_life_days=half_life_days,
            sensitivity_half_life_days=sensitivity_half_life_days,
            lookback_years=lookback_years,
            prior_mean=prior,
            prior_strength=strength,
        )["posterior_skill"]
    )


def _claim_report_records(
    outcomes: Iterable[Any],
    claims: Iterable[Any] | Mapping[str, Any],
    reports: Iterable[Any] | Mapping[str, Any],
    as_of: datetime,
    lookback_years: float,
) -> list[dict[str, Any]]:
    claim_by_id = (
        dict(claims)
        if isinstance(claims, Mapping)
        else {str(_get(claim, "claim_id", default="")): claim for claim in claims}
    )
    report_by_id = (
        dict(reports)
        if isinstance(reports, Mapping)
        else {str(_get(report, "report_id", default="")): report for report in reports}
    )
    cutoff = as_of - timedelta(days=365.25 * lookback_years)
    records: list[dict[str, Any]] = []
    for outcome in outcomes:
        if not bool(_get(outcome, "mature", default=False)) or _get(outcome, "hit") is None:
            continue
        claim = claim_by_id.get(str(_get(outcome, "claim_id", default="")))
        if claim is None:
            continue
        report = report_by_id.get(str(_get(claim, "report_id", default="")))
        if report is None:
            continue
        truth_time_raw = _get(outcome, "truth_available_at", "evaluated_at")
        claim_time_raw = _get(claim, "available_at")
        if truth_time_raw is None or claim_time_raw is None:
            continue
        truth_time = _datetime(truth_time_raw, end_of_day=True)
        claim_time = _datetime(claim_time_raw, end_of_day=True)
        # A result must be known before the snapshot and strictly after its claim.
        if not (cutoff <= truth_time < as_of) or not claim_time < truth_time:
            continue
        records.append(
            {
                "claim_id": str(_get(claim, "claim_id", default="")),
                "report_id": str(_get(claim, "report_id", default="")),
                "broker": _broker_identity(report),
                "broker_display": str(_get(report, "broker", default="")),
                "analyst": _analyst(report),
                "team": str(_get(report, "team", default="")),
                "dimension": str(_get(claim, "dimension", default="")),
                "target_type": str(_get(claim, "target_type", default="")),
                "horizon_days": int(_get(claim, "horizon_days", default=0) or 0),
                "market_state": str(_get(claim, "market_state", default=_get(report, "market_state", default=""))),
                "industry_id": str(_get(report, "industry_id", "industry_code", default="")),
                "subject_id": str(_get(claim, "subject_id", default="")),
                "direction": int(_get(claim, "direction", default=0) or 0),
                "available_at": claim_time,
                "truth_available_at": truth_time,
                "hit": bool(_get(outcome, "hit")),
                "mature": True,
            }
        )
    return records


def build_skill_snapshots(
    outcomes: Iterable[Any],
    claims: Iterable[Any] | Mapping[str, Any],
    reports: Iterable[Any] | Mapping[str, Any],
    *,
    as_of: Any,
    half_life_days: float = 730.0,
    sensitivity_half_life_days: float = 365.0,
    lookback_years: float = 5.0,
    prior_strength: float = 5.0,
    lower_bound_z: float = 1.645,
) -> list[Any]:
    """Build the source x dimension x target x horizon skill cube.

    Analyst priors pool that analyst's history across employers, while broker
    and team priors remain attached to the publication-time entity.  The final
    exact-cell estimate shrinks toward both personal and process priors.
    """

    decision = _datetime(as_of, end_of_day=True)
    records = _claim_report_records(outcomes, claims, reports, decision, lookback_years)
    if not records:
        return []

    base_fields = ("dimension", "target_type", "horizon_days")
    exact_fields = ("broker", "analyst", "team", *base_fields, "market_state", "industry_id")

    def group(fields: tuple[str, ...]) -> dict[tuple[Any, ...], list[dict[str, Any]]]:
        output: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        for record in records:
            output[tuple(record[field] for field in fields)].append(record)
        return output

    global_groups = group(base_fields)
    broker_groups = group(("broker", *base_fields))
    team_groups = group(("broker", "team", *base_fields))
    analyst_groups = group(("analyst", *base_fields))
    exact_groups = group(exact_fields)
    snapshots: list[Any] = []

    for exact_key in sorted(exact_groups, key=lambda key: tuple(str(part) for part in key)):
        cell = exact_groups[exact_key]
        row = dict(zip(exact_fields, exact_key))
        base_key = tuple(row[field] for field in base_fields)
        global_estimate = estimate_skill(
            global_groups[base_key],
            as_of=decision,
            half_life_days=half_life_days,
            sensitivity_half_life_days=sensitivity_half_life_days,
            lookback_years=lookback_years,
            prior_mean=0.5,
            prior_strength=max(8.0, prior_strength),
            lower_bound_z=lower_bound_z,
        )
        global_prior = float(global_estimate["posterior_skill"])
        broker_key = (row["broker"], *base_key)
        pool_options = {
            "half_life_days": half_life_days,
            "sensitivity_half_life_days": sensitivity_half_life_days,
            "lookback_years": lookback_years,
        }
        broker_prior = _pool_estimate(
            broker_groups[broker_key], global_prior, 8.0, decision, **pool_options
        )
        team_key = (row["broker"], row["team"], *base_key)
        team_prior = (
            _pool_estimate(team_groups[team_key], broker_prior, 5.0, decision, **pool_options)
            if row["team"]
            else broker_prior
        )
        analyst_key = (row["analyst"], *base_key)
        analyst_prior = (
            _pool_estimate(analyst_groups[analyst_key], global_prior, 5.0, decision, **pool_options)
            if row["analyst"]
            else team_prior
        )
        # Personal skill follows the analyst; process skill stays with team/broker.
        hierarchical_prior = 0.67 * analyst_prior + 0.33 * team_prior
        estimate = estimate_skill(
            cell,
            as_of=decision,
            half_life_days=half_life_days,
            sensitivity_half_life_days=sensitivity_half_life_days,
            lookback_years=lookback_years,
            prior_mean=hierarchical_prior,
            prior_strength=prior_strength,
            lower_bound_z=lower_bound_z,
        )
        source_ids = tuple(sorted({record["report_id"] for record in cell if record["report_id"]}))
        broker_displays = sorted(
            {record["broker_display"] for record in cell if record.get("broker_display")}
        )
        identity = "|".join(str(row[field]) for field in exact_fields) + f"|{decision.isoformat()}"
        payload = {
            "as_of": decision,
            "broker": row["broker"],
            "analyst": row["analyst"],
            "team": row["team"],
            "dimension": row["dimension"],
            "target_type": row["target_type"],
            "horizon_days": row["horizon_days"],
            "posterior_skill": estimate["posterior_skill"],
            "conservative_lower_bound": estimate["conservative_lower_bound"],
            "effective_sample_size": estimate["effective_sample_size"],
            "source_report_ids": source_ids,
            "market_state": row["market_state"],
            "industry_id": row["industry_id"],
            "snapshot_id": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
            "broker_display": broker_displays[0] if broker_displays else row["broker"],
            # Kept when a mapping/extended model is used.
            "sensitivity_365": estimate["sensitivity_365"],
            "sensitivity_365_lower_bound": estimate[
                "sensitivity_365_lower_bound"
            ],
            "sensitivity_365_effective_sample_size": estimate[
                "sensitivity_365_effective_sample_size"
            ],
            "sensitivity_delta": estimate["sensitivity_delta"],
            "hierarchical_prior": hierarchical_prior,
        }
        snapshots.append(_construct(payload))
    return snapshots


def select_skill_snapshot(
    snapshots: Iterable[Any],
    *,
    as_of: Any,
    claim: Any | None = None,
    report: Any | None = None,
    broker: str | None = None,
    analyst: str | None = None,
    team: str | None = None,
    dimension: str | None = None,
    target_type: str | None = None,
    horizon_days: int | None = None,
    market_state: str | None = None,
    industry_id: str | None = None,
) -> Any | None:
    """Select the latest strictly prior matching snapshot, preferring specificity."""

    decision = _datetime(as_of, end_of_day=True)
    wanted = {
        "broker": broker if broker is not None else _broker_identity(report),
        "analyst": analyst if analyst is not None else _analyst(report),
        "team": team if team is not None else str(_get(report, "team", default="")),
        "dimension": dimension if dimension is not None else str(_get(claim, "dimension", default="")),
        "target_type": target_type if target_type is not None else str(_get(claim, "target_type", default="")),
        "horizon_days": int(horizon_days if horizon_days is not None else (_get(claim, "horizon_days", default=0) or 0)),
        "market_state": market_state or "",
        "industry_id": industry_id if industry_id is not None else str(_get(report, "industry_id", "industry_code", default="")),
    }
    candidates: list[tuple[int, datetime, float, Any]] = []
    required = ("dimension", "target_type", "horizon_days")
    for snapshot in snapshots:
        snapshot_time_raw = _get(snapshot, "as_of")
        if snapshot_time_raw is None:
            continue
        snapshot_time = _datetime(snapshot_time_raw, end_of_day=True)
        if snapshot_time >= decision:
            continue
        if any(
            (
                str(_get(snapshot, field, default="")).lower()
                if field in {"dimension", "target_type"}
                else str(_get(snapshot, field, default=""))
            )
            != (str(wanted[field]).lower() if field in {"dimension", "target_type"} else str(wanted[field]))
            for field in required
        ):
            continue
        specificity = 0
        mismatch = False
        observed_analyst = str(_get(snapshot, "analyst", default="") or "")
        desired_analyst = str(wanted["analyst"] or "")
        personal_history_follows_analyst = bool(
            observed_analyst and desired_analyst and observed_analyst == desired_analyst
        )
        for field in ("broker", "analyst", "team", "market_state", "industry_id"):
            observed = str(_get(snapshot, field, default="") or "")
            desired = str(wanted[field] or "")
            if observed and observed != desired:
                if field in {"broker", "team"} and personal_history_follows_analyst:
                    # Personal history follows an analyst across employers;
                    # exact current-broker/team snapshots still outrank it.
                    continue
                mismatch = True
                break
            if observed and observed == desired:
                specificity += 1
        if mismatch:
            continue
        lower = float(_get(snapshot, "conservative_lower_bound", default=0.0) or 0.0)
        candidates.append((specificity, snapshot_time, lower, snapshot))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return candidates[0][3]


__all__ = [
    "SkillEstimationError",
    "build_skill_snapshots",
    "estimate_skill",
    "select_skill_snapshot",
    "time_decay_weight",
]
