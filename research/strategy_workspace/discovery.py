"""Deterministic candidate generation and pre-outcome plan freezing.

Neither public function accepts labels, realised returns, backtest results, or
model scores.  ``freeze_plan`` freezes the entire generated family; it cannot
be used to keep only a retrospectively successful candidate.
"""

from __future__ import annotations

from collections.abc import Sequence

from .catalog import DEFAULT_FACTOR_CATALOG
from .contracts import (
    MAX_FACTORS_PER_MECHANISM,
    MAX_FACTORS_PER_PLAN,
    DiscoveryContractError,
    DiscoveryPlan,
    DiscoveryStatus,
    FactorDefinition,
    ThesisSpec,
)


def _validated_catalog(
    catalog: Sequence[FactorDefinition],
) -> tuple[FactorDefinition, ...]:
    if not isinstance(catalog, Sequence) or isinstance(catalog, (str, bytes)):
        raise DiscoveryContractError(
            "catalog must be an ordered FactorDefinition array"
        )
    normalized = tuple(catalog)
    if any(not isinstance(item, FactorDefinition) for item in normalized):
        raise DiscoveryContractError(
            "catalog must contain only FactorDefinition objects"
        )
    factor_ids = [item.factor_id for item in normalized]
    if len(set(factor_ids)) != len(factor_ids):
        raise DiscoveryContractError("catalog factor_ids must be unique")
    return normalized


def generate_candidates(
    thesis: ThesisSpec,
    catalog: Sequence[FactorDefinition] = DEFAULT_FACTOR_CATALOG,
) -> DiscoveryPlan:
    """Map declared mechanisms to one bounded, deterministic factor family.

    A requested mechanism with no catalog entry, or more than three entries,
    returns a ``BLOCKED`` plan.  The generator never truncates an oversized
    family silently because catalog ordering is not research evidence.
    """

    if not isinstance(thesis, ThesisSpec):
        raise DiscoveryContractError("thesis must be a ThesisSpec")
    definitions = _validated_catalog(catalog)
    selected: list[FactorDefinition] = []
    blocked_reasons: list[str] = []
    for mechanism_path in thesis.mechanisms:
        matching = tuple(
            item
            for item in definitions
            if item.mechanism_path == mechanism_path
        )
        if not matching:
            blocked_reasons.append(f"catalog_missing_mechanism:{mechanism_path}")
            continue
        if len(matching) > MAX_FACTORS_PER_MECHANISM:
            blocked_reasons.append(
                f"mechanism_factor_cap_exceeded:{mechanism_path}:"
                f"{len(matching)}>{MAX_FACTORS_PER_MECHANISM}"
            )
            continue
        selected.extend(matching)
    if len(selected) > MAX_FACTORS_PER_PLAN:
        blocked_reasons.append(
            f"plan_factor_cap_exceeded:{len(selected)}>{MAX_FACTORS_PER_PLAN}"
        )
        selected = []
    if blocked_reasons:
        return DiscoveryPlan(
            thesis=thesis,
            factors=tuple(selected),
            status=DiscoveryStatus.BLOCKED,
            blocked_reasons=tuple(blocked_reasons),
        )
    return DiscoveryPlan(
        thesis=thesis,
        factors=tuple(selected),
        status=DiscoveryStatus.CANDIDATES_GENERATED,
    )


def freeze_plan(plan: DiscoveryPlan) -> DiscoveryPlan:
    """Freeze all generated candidates without accepting evaluation evidence."""

    if not isinstance(plan, DiscoveryPlan):
        raise DiscoveryContractError("plan must be a DiscoveryPlan")
    if plan.status is DiscoveryStatus.BLOCKED:
        raise DiscoveryContractError(
            "blocked discovery plans cannot be frozen: "
            + ",".join(plan.blocked_reasons)
        )
    if plan.status is DiscoveryStatus.FROZEN:
        return plan.require_frozen()
    if plan.status is not DiscoveryStatus.CANDIDATES_GENERATED:
        raise DiscoveryContractError("only generated candidate plans can be frozen")
    frozen = DiscoveryPlan(
        thesis=plan.thesis,
        factors=plan.factors,
        status=DiscoveryStatus.FROZEN,
    )
    return frozen.require_frozen()


__all__ = ["freeze_plan", "generate_candidates"]
