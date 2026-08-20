"""Small, versioned price-factor catalog for bounded strategy discovery."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from .contracts import (
    DiscoveryContractError,
    ExpectedSign,
    FactorDefinition,
    canonical_sha256,
)


DEFAULT_FACTOR_CATALOG: tuple[FactorDefinition, ...] = (
    FactorDefinition(
        factor_id="RM20",
        name="20-day momentum",
        mechanism_path="price.momentum",
        lookback_days=20,
        required_fields=("benchmark_close", "close"),
        expected_sign=ExpectedSign.POSITIVE,
        formula="log(close/lag(close,20))-log(benchmark_close/lag(benchmark_close,20))",
        description="20-session log return relative to the frozen benchmark.",
    ),
    FactorDefinition(
        factor_id="RM60",
        name="60-day momentum",
        mechanism_path="price.momentum",
        lookback_days=60,
        required_fields=("benchmark_close", "close"),
        expected_sign=ExpectedSign.POSITIVE,
        formula="log(close/lag(close,60))-log(benchmark_close/lag(benchmark_close,60))",
        description="60-session log return relative to the frozen benchmark.",
    ),
    FactorDefinition(
        factor_id="RM120",
        name="120-day momentum",
        mechanism_path="price.momentum",
        lookback_days=120,
        required_fields=("benchmark_close", "close"),
        expected_sign=ExpectedSign.POSITIVE,
        formula="log(close/lag(close,120))-log(benchmark_close/lag(benchmark_close,120))",
        description="120-session log return relative to the frozen benchmark.",
    ),
    FactorDefinition(
        factor_id="REV20",
        name="20-day reversal",
        mechanism_path="price.reversal",
        lookback_days=20,
        required_fields=("close",),
        expected_sign=ExpectedSign.POSITIVE,
        formula="-(close / lag(close,20) - 1)",
        description="The sign is fixed so larger values mean deeper prior losses.",
    ),
    FactorDefinition(
        factor_id="TREND_EFF60",
        name="60-day directional trend efficiency",
        mechanism_path="price.trend",
        lookback_days=60,
        required_fields=("close",),
        expected_sign=ExpectedSign.POSITIVE,
        formula="(close - lag(close,60)) / sum(abs(diff(close)),60)",
        description="Directional displacement divided by total path length.",
    ),
    FactorDefinition(
        factor_id="DOWNSIDE_VOL60",
        name="60-day downside volatility",
        mechanism_path="risk.downside",
        lookback_days=60,
        required_fields=("close",),
        expected_sign=ExpectedSign.NEGATIVE,
        formula="std(min(log(close / lag(close,1)),0),60)",
        description="Dispersion of negative daily log returns only.",
    ),
    FactorDefinition(
        factor_id="BREAKOUT60",
        name="60-day prior-high breakout proximity",
        mechanism_path="price.trend",
        lookback_days=60,
        required_fields=("close", "high"),
        expected_sign=ExpectedSign.POSITIVE,
        formula="close / rolling_max(lag(high,1),60) - 1",
        description="Current close relative to the preceding 60-day high.",
    ),
)

_CATALOG_BY_ID: Mapping[str, FactorDefinition] = MappingProxyType(
    {item.factor_id: item for item in DEFAULT_FACTOR_CATALOG}
)

if len(_CATALOG_BY_ID) != len(DEFAULT_FACTOR_CATALOG):
    raise RuntimeError("default factor catalog contains duplicate factor_ids")

DEFAULT_CATALOG_SHA256 = canonical_sha256(
    [item.to_dict() for item in DEFAULT_FACTOR_CATALOG]
)


def get_factor(factor_id: str) -> FactorDefinition:
    """Return a catalog factor or fail closed on an unknown identifier."""

    normalized = str(factor_id or "").strip().upper()
    try:
        return _CATALOG_BY_ID[normalized]
    except KeyError as exc:
        raise DiscoveryContractError(f"unknown factor_id: {normalized!r}") from exc


def factors_for_mechanism(mechanism_path: str) -> tuple[FactorDefinition, ...]:
    """Return the fixed family for one mechanism in catalog order."""

    normalized = str(mechanism_path or "").strip().lower()
    return tuple(
        item
        for item in DEFAULT_FACTOR_CATALOG
        if item.mechanism_path == normalized
    )


__all__ = [
    "DEFAULT_CATALOG_SHA256",
    "DEFAULT_FACTOR_CATALOG",
    "factors_for_mechanism",
    "get_factor",
]
