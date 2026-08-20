"""Additive performance attribution for the strategy workspace backtester.

The accounting identity is intentionally small and auditable::

    cost_addback_pnl + cost_pnl == net_pnl == benchmark_pnl + active_pnl

``cost_pnl`` is negative.  Slippage belongs to costs, so a backtester should
include it in each NAV point's cumulative cost before calling this module.
``cost_addback_pnl`` is an arithmetic attribution on the realised net book;
it is not a separately rebalanced zero-cost counterfactual portfolio.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Iterable, Mapping


ZERO = Decimal("0")


class AttributionError(ValueError):
    """Raised when an attribution series cannot be reconciled."""


@dataclass(frozen=True)
class AttributionPoint:
    trading_date: date
    cost_addback_pnl: Decimal
    cost_pnl: Decimal
    net_pnl: Decimal
    benchmark_pnl: Decimal
    active_pnl: Decimal
    reconciliation_error: Decimal

    @property
    def cost(self) -> Decimal:
        return self.cost_pnl

    @property
    def benchmark(self) -> Decimal:
        return self.benchmark_pnl

    @property
    def active(self) -> Decimal:
        return self.active_pnl


@dataclass(frozen=True)
class AttributionSummary:
    cost_addback_pnl: Decimal
    cost_pnl: Decimal
    net_pnl: Decimal
    benchmark_pnl: Decimal
    active_pnl: Decimal
    reconciliation_error: Decimal

    @property
    def cost(self) -> Decimal:
        return self.cost_pnl

    @property
    def benchmark(self) -> Decimal:
        return self.benchmark_pnl

    @property
    def active(self) -> Decimal:
        return self.active_pnl


@dataclass(frozen=True)
class AttributionReport:
    points: tuple[AttributionPoint, ...]
    summary: AttributionSummary


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _decimal(value: Any, context: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:  # pragma: no cover - defensive contract boundary
        raise AttributionError(f"{context} must be decimal-compatible") from exc
    if not result.is_finite():
        raise AttributionError(f"{context} must be finite")
    return result


def build_attribution(
    nav_points: Iterable[Any],
    *,
    initial_nav: Decimal | int | float | str,
    initial_benchmark_close: Decimal | int | float | str | None = None,
) -> AttributionReport:
    """Build daily additive attribution from ordered NAV points.

    Each input point must expose ``trading_date``, ``net_nav`` and
    ``cumulative_cost``.  ``benchmark_close`` is optional; when it is absent,
    benchmark PnL for that interval is zero.  Missing benchmark observations
    are carried forward only after at least one benchmark close is known.
    """

    previous_nav = _decimal(initial_nav, "initial_nav")
    if previous_nav <= 0:
        raise AttributionError("initial_nav must be positive")
    previous_cost = ZERO
    benchmark_base_close = (
        _decimal(initial_benchmark_close, "initial_benchmark_close")
        if initial_benchmark_close is not None
        else None
    )
    if benchmark_base_close is not None and benchmark_base_close <= 0:
        raise AttributionError("initial_benchmark_close must be positive")
    previous_benchmark = benchmark_base_close
    previous_benchmark_nav = previous_nav if benchmark_base_close is not None else None
    initial_capital = previous_nav

    materialized = list(nav_points)
    points: list[AttributionPoint] = []
    previous_date: date | None = None
    for index, item in enumerate(materialized):
        trading_date = _value(item, "trading_date")
        if not isinstance(trading_date, date):
            raise AttributionError(f"nav_points[{index}].trading_date must be a date")
        if previous_date is not None and trading_date <= previous_date:
            raise AttributionError("nav points must be strictly date-ordered")

        net_nav = _decimal(_value(item, "net_nav"), f"nav_points[{index}].net_nav")
        cumulative_cost = _decimal(
            _value(item, "cumulative_cost", ZERO),
            f"nav_points[{index}].cumulative_cost",
        )
        if net_nav < 0:
            raise AttributionError("net NAV must not be negative")
        if cumulative_cost < previous_cost:
            raise AttributionError("cumulative cost must not decrease")

        interval_cost = cumulative_cost - previous_cost
        net_pnl = net_nav - previous_nav
        cost_pnl = -interval_cost
        cost_addback_pnl = net_pnl + interval_cost

        raw_benchmark = _value(item, "benchmark_close")
        current_benchmark = (
            _decimal(raw_benchmark, f"nav_points[{index}].benchmark_close")
            if raw_benchmark is not None
            else previous_benchmark
        )
        if current_benchmark is not None and current_benchmark <= 0:
            raise AttributionError("benchmark close must be positive")
        if current_benchmark is not None and benchmark_base_close is None:
            # The first benchmark observation establishes the base.  It must
            # not create return before the benchmark was observable.
            benchmark_base_close = current_benchmark
            previous_benchmark_nav = initial_capital
        if current_benchmark is None or benchmark_base_close is None:
            benchmark_pnl = ZERO
        else:
            current_benchmark_nav = initial_capital * current_benchmark / benchmark_base_close
            benchmark_pnl = current_benchmark_nav - (
                previous_benchmark_nav
                if previous_benchmark_nav is not None
                else initial_capital
            )
            previous_benchmark_nav = current_benchmark_nav
        active_pnl = net_pnl - benchmark_pnl
        reconciliation_error = (cost_addback_pnl + cost_pnl) - (
            benchmark_pnl + active_pnl
        )

        points.append(
            AttributionPoint(
                trading_date=trading_date,
                cost_addback_pnl=cost_addback_pnl,
                cost_pnl=cost_pnl,
                net_pnl=net_pnl,
                benchmark_pnl=benchmark_pnl,
                active_pnl=active_pnl,
                reconciliation_error=reconciliation_error,
            )
        )
        previous_nav = net_nav
        previous_cost = cumulative_cost
        previous_benchmark = current_benchmark
        previous_date = trading_date

    cost_addback_total = sum((point.cost_addback_pnl for point in points), ZERO)
    cost_total = sum((point.cost_pnl for point in points), ZERO)
    net_total = sum((point.net_pnl for point in points), ZERO)
    benchmark_total = sum((point.benchmark_pnl for point in points), ZERO)
    active_total = sum((point.active_pnl for point in points), ZERO)
    reconciliation_error = (cost_addback_total + cost_total) - (
        benchmark_total + active_total
    )
    summary = AttributionSummary(
        cost_addback_pnl=cost_addback_total,
        cost_pnl=cost_total,
        net_pnl=net_total,
        benchmark_pnl=benchmark_total,
        active_pnl=active_total,
        reconciliation_error=reconciliation_error,
    )
    return AttributionReport(points=tuple(points), summary=summary)


def reconcile_attribution(report: AttributionReport) -> bool:
    """Return whether daily and total accounting identities reconcile exactly."""

    return report.summary.reconciliation_error == ZERO and all(
        point.reconciliation_error == ZERO for point in report.points
    )


__all__ = [
    "AttributionError",
    "AttributionPoint",
    "AttributionReport",
    "AttributionSummary",
    "build_attribution",
    "reconcile_attribution",
]
