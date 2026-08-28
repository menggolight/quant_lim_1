"""Formal development/validation backtest for frozen technical momentum.

This module is deliberately independent from the quality-growth and historical
``locked_test`` engines.  It accepts only causal signal-price points and raw
execution-price points from :mod:`technical_formal_data`, rejects every split
other than ``development`` and ``validation`` before touching input iterables,
and has no Paper, broker, order-routing, or LIVE capability.

The six-factor formula, entry/hold bands, and Exposure policy are semantic
copies of the frozen Technical Shadow V1 contract.  Tests compare their output
directly so a later change cannot silently turn this formal path into a new
Alpha or a new Exposure policy.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
import math
from statistics import pstdev, stdev
from typing import Any, Iterable, Mapping, Sequence

from .technical_exposure_shadow_v1 import (
    DEFAULT_POLICY as FROZEN_EXPOSURE_POLICY,
    compute_technical_shadow_exposure,
)
from .technical_formal_data import (
    ExecutionPricePoint,
    PITUniverseLoader,
    SignalPricePoint,
    TechnicalExecutionStatus,
)


STRATEGY_ID = "a-share-technical-momentum-adaptive-v1"
ENGINE_VERSION = "technical-formal-backtest.v1"
LOCKED_TEST_STATUS = "NOT_RUN"
LOCKED_TEST_CONSUMED = False

FACTOR_IDS = (
    "RM20",
    "RM60",
    "RM120",
    "TREND_EFF60",
    "DOWNSIDE_VOL60",
    "BREAKOUT60",
)
FACTOR_DIRECTIONS = {
    "RM20": 1.0,
    "RM60": 1.0,
    "RM120": 1.0,
    "TREND_EFF60": 1.0,
    "DOWNSIDE_VOL60": -1.0,
    "BREAKOUT60": 1.0,
}
_EXECUTION_STATUS_FIELDS = (
    "suspended",
    "is_st",
    "price_limit_applicable",
    "limit_up_price",
    "limit_down_price",
    "limit_up_locked",
    "limit_down_locked",
    "listed",
    "delisted",
    "lot_size",
    "t_plus_one",
)
ALPHA_LOOKBACK_SESSIONS = 120
WINSOR_LOWER = 0.01
WINSOR_UPPER = 0.99
ENTRY_PERCENTILE = Decimal("0.90")
HOLD_PERCENTILE = Decimal("0.70")
ENTRY_SCORE_EXCLUSIVE = Decimal("0")
HOLD_SCORE_EXCLUSIVE = Decimal("0")

INITIAL_CASH = Decimal("10000.00")
MAX_POSITIONS = 3
MAX_POSITION_WEIGHT = Decimal("0.40")
LOT_SIZE = 100

CENT = Decimal("0.01")
PCT = Decimal("0.00000001")
CHANNEL_RELATIVE_TOLERANCE = Decimal("1e-20")
ZERO = Decimal("0")
ONE = Decimal("1")
CHINA_TZ = timezone(timedelta(hours=8))

SPLIT_WINDOWS: Mapping[str, tuple[date, date]] = {
    "development": (date(2018, 1, 1), date(2022, 12, 31)),
    "validation": (date(2023, 1, 1), date(2023, 12, 31)),
}
_PARTITION_IDS = (
    "trading_calendar",
    "signal_prices",
    "execution_prices",
    "execution_statuses",
    "corporate_actions",
)


class TechnicalFormalBacktestError(ValueError):
    """Base error for a non-recoverable formal backtest contract failure."""


class LockedTestAccessForbidden(TechnicalFormalBacktestError):
    """Raised before any data access when a caller asks for a forbidden split."""


class TechnicalFormalDataError(TechnicalFormalBacktestError):
    """Raised when data are incomplete, non-PIT, duplicated, or inconsistent."""


class CorporateActionDataGap(TechnicalFormalDataError):
    """Raised when raw-NAV accounting cannot decompose an adjustment change."""


@dataclass(frozen=True, slots=True)
class TechnicalInputPartition:
    """Metadata-first physical partition whose rows remain completely lazy."""

    dataset_id: str
    coverage_start: date
    coverage_end: date
    rows: Iterable[Any]

    def __post_init__(self) -> None:
        if type(self.dataset_id) is not str or self.dataset_id not in _PARTITION_IDS:
            raise TechnicalFormalDataError("input partition dataset_id is unsupported")
        if type(self.coverage_start) is not date or type(self.coverage_end) is not date:
            raise TechnicalFormalDataError(
                "input partition coverage requires exact date metadata"
            )
        if self.coverage_start > self.coverage_end:
            raise TechnicalFormalDataError(
                "input partition coverage_start follows coverage_end"
            )


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:  # pragma: no cover - defensive trust boundary
        raise TechnicalFormalBacktestError(f"{field} must be decimal-compatible") from exc
    if not result.is_finite():
        raise TechnicalFormalBacktestError(f"{field} must be finite")
    return result


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise TechnicalFormalDataError("quantile requires observations")
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (
        ordered[upper] - ordered[lower]
    ) * (position - lower)


def _signal_close(point: SignalPricePoint) -> float:
    value = float(point.cumulative_total_return_index)
    if not math.isfinite(value) or value <= 0:
        raise TechnicalFormalDataError("signal cumulative index must be positive")
    return value


def _signal_high(point: SignalPricePoint) -> float:
    # SignalPricePoint guarantees that OHLC share one causal total-return-index
    # scale.  BREAKOUT60 must therefore consume this signal high directly and
    # must never reach across to raw execution OHLC.
    value = float(point.high)
    if not math.isfinite(value) or value <= 0:
        raise TechnicalFormalDataError("signal high must be positive")
    return value


@dataclass(frozen=True, slots=True)
class CorporateActionEntitlement:
    """PIT decomposition required to keep a raw-price position ledger.

    A scalar adjustment factor is intentionally insufficient.  A factor change
    while shares are held must bind both the share multiplier and cash amount,
    even when one component is neutral.
    """

    instrument_id: str
    effective_date: date
    previous_adjustment_factor: Decimal
    new_adjustment_factor: Decimal
    share_multiplier: Decimal
    cash_per_old_share: Decimal
    available_at: datetime
    source_sha256: str

    def __post_init__(self) -> None:
        instrument_id = str(self.instrument_id).strip().upper()
        if not instrument_id or type(self.effective_date) is not date:
            raise TechnicalFormalBacktestError("corporate-action identity/date is invalid")
        previous = _decimal(
            self.previous_adjustment_factor, "previous_adjustment_factor"
        )
        new = _decimal(self.new_adjustment_factor, "new_adjustment_factor")
        multiplier = _decimal(self.share_multiplier, "share_multiplier")
        cash = _decimal(self.cash_per_old_share, "cash_per_old_share")
        if min(previous, new, multiplier) <= ZERO or cash < ZERO:
            raise TechnicalFormalBacktestError("corporate-action values are invalid")
        if previous == new:
            raise TechnicalFormalBacktestError("corporate action requires a factor change")
        if multiplier == ONE and cash == ZERO:
            raise TechnicalFormalBacktestError(
                "factor change requires a non-neutral entitlement decomposition"
            )
        if (
            not isinstance(self.available_at, datetime)
            or self.available_at.tzinfo is None
            or self.available_at.utcoffset() is None
        ):
            raise TechnicalFormalBacktestError("corporate action available_at must be aware")
        cutoff = datetime.combine(self.effective_date, time(9, 30), CHINA_TZ)
        if self.available_at > cutoff:
            raise TechnicalFormalBacktestError(
                "corporate-action entitlement was unavailable by the effective open"
            )
        source = str(self.source_sha256).strip().lower()
        if len(source) != 64 or any(ch not in "0123456789abcdef" for ch in source):
            raise TechnicalFormalBacktestError("corporate action source_sha256 is invalid")
        object.__setattr__(self, "instrument_id", instrument_id)
        object.__setattr__(self, "previous_adjustment_factor", previous)
        object.__setattr__(self, "new_adjustment_factor", new)
        object.__setattr__(self, "share_multiplier", multiplier)
        object.__setattr__(self, "cash_per_old_share", cash)
        object.__setattr__(self, "source_sha256", source)


@dataclass(frozen=True, slots=True)
class TechnicalCostScenario:
    name: str
    commission_rate: Decimal = Decimal("0.00018")
    minimum_commission: Decimal = Decimal("5")
    sell_tax_rate: Decimal = Decimal("0.0005")
    transfer_fee_rate: Decimal = Decimal("0.00001")
    slippage_bps_one_way: Decimal = Decimal("10")
    commission_multiplier: Decimal = ONE

    def __post_init__(self) -> None:
        if self.name not in {"base", "stress"}:
            raise TechnicalFormalBacktestError("cost scenario must be base or stress")
        for field in (
            "commission_rate",
            "minimum_commission",
            "sell_tax_rate",
            "transfer_fee_rate",
            "slippage_bps_one_way",
            "commission_multiplier",
        ):
            value = _decimal(getattr(self, field), field)
            if value < ZERO:
                raise TechnicalFormalBacktestError(f"{field} must be non-negative")
            object.__setattr__(self, field, value)
        if self.commission_multiplier < ONE:
            raise TechnicalFormalBacktestError("commission multiplier must be >= 1")


BASE_COST = TechnicalCostScenario("base")
STRESS_COST = TechnicalCostScenario(
    "stress",
    slippage_bps_one_way=Decimal("20"),
    commission_multiplier=Decimal("2"),
)


@dataclass(frozen=True, slots=True)
class TechnicalRankRow:
    instrument_id: str
    factors: Mapping[str, float] | None
    z_scores: Mapping[str, float] | None
    composite_score: float | None
    rank: int | None
    percentile: float | None
    eligibility: bool
    entry_eligible: bool
    hold_eligible: bool
    exclusion_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TechnicalDecision:
    decision_date: date
    execution_date: date
    selected_instrument_ids: tuple[str, ...]
    target_weights: Mapping[str, Decimal]
    market_state: str
    target_gross_exposure: Decimal
    eligible_count: int
    entry_count: int


@dataclass(frozen=True, slots=True)
class TechnicalFill:
    trading_date: date
    decision_date: date
    instrument_id: str
    side: str
    quantity: int
    reference_open: Decimal
    fill_price: Decimal
    reference_notional: Decimal
    fill_notional: Decimal
    commission: Decimal
    sell_tax: Decimal
    transfer_fee: Decimal
    slippage_cost: Decimal
    total_cost: Decimal
    cash_delta: Decimal


@dataclass(frozen=True, slots=True)
class TechnicalExecutionEvent:
    trading_date: date
    code: str
    instrument_id: str = ""
    detail: str = ""


@dataclass(frozen=True, slots=True)
class TechnicalNavPoint:
    trading_date: date
    cash: Decimal
    market_value: Decimal
    nav: Decimal
    daily_pnl: Decimal
    cumulative_cost: Decimal
    benchmark_close: Decimal
    market_state: str
    target_gross_exposure: Decimal
    realized_gross_exposure: Decimal


@dataclass(frozen=True, slots=True)
class TechnicalPerformanceMetrics:
    net_return: Decimal
    benchmark_return: Decimal
    net_active_return: Decimal
    max_drawdown: Decimal
    turnover: Decimal
    total_cost: Decimal
    cost_to_gross_profit: Decimal | None
    exposure_state_distribution: Mapping[str, Decimal]
    cash_day_fraction: Decimal
    positive_half_year_count: int
    half_year_count: int
    trade_count: int
    fill_count: int
    win_rate: Decimal | None
    average_holding_period: Decimal | None
    per_stock_pnl_contribution: Mapping[str, Decimal]
    largest_stock_pnl_share: Decimal | None
    largest_10_days_pnl_share: Decimal | None


@dataclass(frozen=True, slots=True)
class TechnicalScenarioResult:
    scenario: str
    split: str
    start_date: date
    end_date: date
    metrics: TechnicalPerformanceMetrics
    nav: tuple[TechnicalNavPoint, ...]
    fills: tuple[TechnicalFill, ...]
    events: tuple[TechnicalExecutionEvent, ...]
    ending_positions: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class TechnicalBacktestComparison:
    strategy_id: str
    engine_version: str
    split: str
    base: TechnicalScenarioResult
    stress: TechnicalScenarioResult
    locked_test_status: str = LOCKED_TEST_STATUS
    locked_test_consumed: bool = LOCKED_TEST_CONSUMED
    paper_eligibility: bool = False
    trade_eligibility: bool = False
    automatic_order_submission: bool = False
    live_supported: bool = False


@dataclass(slots=True)
class _Lot:
    quantity: int
    acquired_on: date
    remaining_cost_basis: Decimal


@dataclass(frozen=True, slots=True)
class _ClosedLot:
    quantity: int
    pnl: Decimal
    holding_sessions: int


def _factor_values(
    points: Sequence[SignalPricePoint],
    benchmark: Sequence[SignalPricePoint],
) -> dict[str, float]:
    closes = [_signal_close(item) for item in points]
    highs = [_signal_high(item) for item in points]
    benchmark_closes = [_signal_close(item) for item in benchmark]
    values: dict[str, float] = {}
    for lookback in (20, 60, 120):
        values[f"RM{lookback}"] = math.log(
            closes[-1] / closes[-1 - lookback]
        ) - math.log(
            benchmark_closes[-1] / benchmark_closes[-1 - lookback]
        )
    path = closes[-61:]
    path_length = sum(
        abs(path[index] - path[index - 1]) for index in range(1, 61)
    )
    values["TREND_EFF60"] = (
        0.0 if path_length == 0 else (path[-1] - path[0]) / path_length
    )
    downside = [
        min(math.log(path[index] / path[index - 1]), 0.0)
        for index in range(1, 61)
    ]
    values["DOWNSIDE_VOL60"] = stdev(downside)
    values["BREAKOUT60"] = closes[-1] / max(highs[-61:-1]) - 1.0
    if any(not math.isfinite(value) for value in values.values()):
        raise TechnicalFormalDataError("non-finite technical factor")
    return {factor: values[factor] for factor in FACTOR_IDS}


def rank_technical_formal_universe(
    *,
    decision_date: date,
    sessions: Sequence[date],
    instrument_ids: Sequence[str],
    signal_index: Mapping[tuple[date, str], SignalPricePoint],
    benchmark_id: str,
    status_index: Mapping[tuple[date, str], TechnicalExecutionStatus],
) -> tuple[TechnicalRankRow, ...]:
    """Rank any PIT universe using the exact frozen six-factor semantics."""

    ids = tuple(str(value).strip().upper() for value in instrument_ids)
    if not ids or len(ids) != len(set(ids)):
        raise TechnicalFormalDataError("PIT universe must be non-empty and unique")
    required = tuple(sessions[-(ALPHA_LOOKBACK_SESSIONS + 1):])
    if len(required) != ALPHA_LOOKBACK_SESSIONS + 1 or required[-1] != decision_date:
        raise TechnicalFormalDataError("technical ranking requires 121 ending sessions")
    benchmark: list[SignalPricePoint] = []
    for day in required:
        point = signal_index.get((day, benchmark_id))
        if point is None:
            raise TechnicalFormalDataError(
                f"benchmark missing required signal session:{day.isoformat()}"
            )
        benchmark.append(point)

    exclusions_by_id: dict[str, set[str]] = {item: set() for item in ids}
    factor_rows: dict[str, dict[str, float]] = {}
    for instrument_id in ids:
        status = status_index.get((decision_date, instrument_id))
        if status is None:
            raise TechnicalFormalDataError(
                f"missing execution status:{decision_date}:{instrument_id}"
            )
        if status.suspended:
            exclusions_by_id[instrument_id].add("suspended_on_decision_date")
        if status.is_st:
            exclusions_by_id[instrument_id].add("st_on_decision_date")
        if not status.listed or status.delisted:
            exclusions_by_id[instrument_id].add("not_listed_on_decision_date")
        points: list[SignalPricePoint] = []
        for day in required:
            point = signal_index.get((day, instrument_id))
            if point is None:
                exclusions_by_id[instrument_id].add("missing_common_session")
                break
            points.append(point)
        if not exclusions_by_id[instrument_id]:
            try:
                factor_rows[instrument_id] = _factor_values(points, benchmark)
            except (TechnicalFormalBacktestError, ValueError, ZeroDivisionError):
                exclusions_by_id[instrument_id].add("invalid_signal_series")

    z_by_id = {instrument_id: {} for instrument_id in factor_rows}
    for factor in FACTOR_IDS:
        values = [factor_rows[item][factor] for item in factor_rows]
        if not values:
            break
        lower = _quantile(values, WINSOR_LOWER)
        upper = _quantile(values, WINSOR_UPPER)
        clipped = {
            item: min(max(factor_rows[item][factor], lower), upper)
            for item in factor_rows
        }
        mean = sum(clipped.values()) / len(clipped)
        sigma = pstdev(clipped.values())
        for item, value in clipped.items():
            z_by_id[item][factor] = 0.0 if sigma == 0 else (value - mean) / sigma

    scores = {
        item: sum(
            FACTOR_DIRECTIONS[factor] * z_by_id[item][factor]
            for factor in FACTOR_IDS
        )
        for item in factor_rows
        if len(z_by_id[item]) == len(FACTOR_IDS)
    }
    ranked_ids = sorted(scores, key=lambda item: (-scores[item], item))
    rank_by_id = {item: index + 1 for index, item in enumerate(ranked_ids)}
    denominator = max(len(ranked_ids) - 1, 1)
    rows: list[TechnicalRankRow] = []
    for instrument_id in ids:
        if instrument_id not in scores:
            rows.append(
                TechnicalRankRow(
                    instrument_id,
                    None,
                    None,
                    None,
                    None,
                    None,
                    False,
                    False,
                    False,
                    tuple(sorted(exclusions_by_id[instrument_id])),
                )
            )
            continue
        rank = rank_by_id[instrument_id]
        percentile = (len(ranked_ids) - rank) / denominator
        score = scores[instrument_id]
        entry = score > float(ENTRY_SCORE_EXCLUSIVE) and percentile >= float(
            ENTRY_PERCENTILE
        )
        hold = score > float(HOLD_SCORE_EXCLUSIVE) and percentile >= float(
            HOLD_PERCENTILE
        )
        exclusions = set(exclusions_by_id[instrument_id])
        if not entry:
            exclusions.add("below_entry_threshold")
        if not hold:
            exclusions.add("below_hold_threshold")
        rows.append(
            TechnicalRankRow(
                instrument_id,
                factor_rows[instrument_id],
                z_by_id[instrument_id],
                score,
                rank,
                percentile,
                True,
                entry,
                hold,
                tuple(sorted(exclusions)),
            )
        )
    return tuple(
        sorted(
            rows,
            key=lambda row: (
                row.rank is None,
                row.rank if row.rank is not None else 10**9,
                row.instrument_id,
            ),
        )
    )


def _cost(
    *,
    side: str,
    quantity: int,
    reference_open: Decimal,
    scenario: TechnicalCostScenario,
) -> dict[str, Decimal]:
    if side not in {"BUY", "SELL"} or quantity <= 0 or reference_open <= ZERO:
        raise TechnicalFormalBacktestError("invalid fill request")
    slippage = scenario.slippage_bps_one_way / Decimal("10000")
    fill = _money(
        reference_open * (ONE + slippage if side == "BUY" else ONE - slippage)
    )
    reference_notional = _money(reference_open * quantity)
    fill_notional = _money(fill * quantity)
    commission = _money(
        max(
            scenario.minimum_commission,
            fill_notional * scenario.commission_rate,
        )
        * scenario.commission_multiplier
    )
    sell_tax = (
        _money(fill_notional * scenario.sell_tax_rate)
        if side == "SELL"
        else ZERO.quantize(CENT)
    )
    transfer = _money(fill_notional * scenario.transfer_fee_rate)
    slippage_cost = _money(abs(fill - reference_open) * quantity)
    explicit = commission + sell_tax + transfer
    total = _money(explicit + slippage_cost)
    cash_delta = (
        _money(-(fill_notional + explicit))
        if side == "BUY"
        else _money(fill_notional - explicit)
    )
    return {
        "fill_price": fill,
        "reference_notional": reference_notional,
        "fill_notional": fill_notional,
        "commission": commission,
        "sell_tax": sell_tax,
        "transfer_fee": transfer,
        "slippage_cost": slippage_cost,
        "total_cost": total,
        "cash_delta": cash_delta,
    }


def _position_quantities(positions: Mapping[str, list[_Lot]]) -> dict[str, int]:
    return {
        instrument_id: sum(lot.quantity for lot in lots)
        for instrument_id, lots in positions.items()
        if sum(lot.quantity for lot in lots) > 0
    }


def _mark_positions_at_raw_close(
    *,
    trading_date: date,
    positions: Mapping[str, list[_Lot]],
    execution_by_key: Mapping[tuple[date, str], ExecutionPricePoint],
    status_by_key: Mapping[tuple[date, str], TechnicalExecutionStatus],
    valuation_closes: dict[str, Decimal],
    events: list[TechnicalExecutionEvent],
) -> dict[str, Decimal]:
    """Value shares with raw closes; only suspension permits stale carry."""

    values: dict[str, Decimal] = {}
    for instrument_id, quantity in _position_quantities(positions).items():
        key = (trading_date, instrument_id)
        point = execution_by_key.get(key)
        status = status_by_key.get(key)
        if status is None:
            raise TechnicalFormalDataError(
                f"held position lacks status:{trading_date}:{instrument_id}"
            )
        if point is not None:
            valuation_closes[instrument_id] = _decimal(
                point.close, "execution close"
            )
        elif status.suspended and instrument_id in valuation_closes:
            events.append(
                TechnicalExecutionEvent(
                    trading_date,
                    "valuation_suspended_carry_forward",
                    instrument_id,
                )
            )
        elif status.delisted or not status.listed:
            raise TechnicalFormalDataError(
                "delisted residual lacks controlled terminal valuation:"
                f"{trading_date}:{instrument_id}"
            )
        else:
            raise TechnicalFormalDataError(
                "held non-suspended position lacks raw close:"
                f"{trading_date}:{instrument_id}"
            )
        values[instrument_id] = _money(
            valuation_closes[instrument_id] * quantity
        )
    return values


def _index_exact(
    values: Iterable[Any],
    expected_type: type,
    label: str,
) -> tuple[Any, ...]:
    result = tuple(values)
    if any(type(item) is not expected_type for item in result):
        raise TechnicalFormalDataError(f"{label} requires exact {expected_type.__name__}")
    return result


def _validate_input_partition_metadata(
    partition: TechnicalInputPartition,
    *,
    expected_dataset_id: str,
    split_start: date,
    split_end: date,
) -> None:
    """Validate only immutable metadata; never inspect or iterate ``rows``."""

    if type(partition) is not TechnicalInputPartition:
        raise TechnicalFormalDataError(
            f"{expected_dataset_id} requires exact TechnicalInputPartition"
        )
    if partition.dataset_id != expected_dataset_id:
        raise TechnicalFormalDataError(
            f"input partition identity mismatch:{expected_dataset_id}"
        )
    if partition.coverage_end > split_end:
        raise TechnicalFormalDataError(
            f"{expected_dataset_id} partition coverage_end crosses split_end"
        )
    if partition.coverage_end != split_end:
        raise TechnicalFormalDataError(
            f"{expected_dataset_id} partition must end exactly at split_end"
        )
    if partition.coverage_start > split_start:
        raise TechnicalFormalDataError(
            f"{expected_dataset_id} partition does not cover split_start"
        )


def _partition_rows(
    partition: TechnicalInputPartition,
    *,
    expected_type: type,
    date_field: str | None,
) -> tuple[Any, ...]:
    """Materialize only after every sibling partition passed metadata gates."""

    rows = _index_exact(partition.rows, expected_type, partition.dataset_id)
    dates = (
        rows
        if date_field is None
        else tuple(getattr(item, date_field) for item in rows)
    )
    if any(
        trading_date < partition.coverage_start
        or trading_date > partition.coverage_end
        for trading_date in dates
    ):
        raise TechnicalFormalDataError(
            f"{partition.dataset_id} row lies outside declared partition coverage"
        )
    return rows


def _signal_index(
    values: Sequence[SignalPricePoint],
) -> dict[tuple[date, str], SignalPricePoint]:
    result: dict[tuple[date, str], SignalPricePoint] = {}
    for point in values:
        key = (point.trading_date, point.instrument_id)
        if key in result:
            raise TechnicalFormalDataError(f"duplicate signal point:{key}")
        result[key] = point
    return result


def _execution_index(
    values: Sequence[ExecutionPricePoint],
) -> dict[tuple[date, str], ExecutionPricePoint]:
    result: dict[tuple[date, str], ExecutionPricePoint] = {}
    for point in values:
        key = (point.trading_date, point.instrument_id)
        if key in result:
            raise TechnicalFormalDataError(f"duplicate execution point:{key}")
        result[key] = point
    return result


def _status_index(
    values: Sequence[TechnicalExecutionStatus],
) -> dict[tuple[date, str], TechnicalExecutionStatus]:
    result: dict[tuple[date, str], TechnicalExecutionStatus] = {}
    for item in values:
        key = (item.trading_date, item.instrument_id)
        if key in result:
            raise TechnicalFormalDataError(f"duplicate execution status:{key}")
        result[key] = item
    return result


def _validate_execution_status_alignment(
    *,
    execution_by_key: Mapping[tuple[date, str], ExecutionPricePoint],
    status_by_key: Mapping[tuple[date, str], TechnicalExecutionStatus],
) -> None:
    """Reject any raw row whose embedded gates drift from independent status.

    Status is intentionally an independent calendar because a suspended
    session can have no raw bar.  Whenever a raw bar *does* exist, however,
    its embedded gates must be byte-for-byte equivalent to that status row.
    """

    for key, point in execution_by_key.items():
        status = status_by_key.get(key)
        if status is None:
            raise TechnicalFormalDataError(
                f"raw execution point lacks independent status:{key}"
            )
        mismatched = tuple(
            field
            for field in _EXECUTION_STATUS_FIELDS
            if getattr(point, field) != getattr(status, field)
        )
        if mismatched:
            raise TechnicalFormalDataError(
                "raw execution/status mismatch:"
                f"{key}:{','.join(mismatched)}"
            )


def _validate_signal_execution_factor_alignment(
    *,
    signal_by_key: Mapping[tuple[date, str], SignalPricePoint],
    execution_by_key: Mapping[tuple[date, str], ExecutionPricePoint],
) -> None:
    """Bind the two price channels without ever equating their price levels."""

    if set(signal_by_key) != set(execution_by_key):
        raise TechnicalFormalDataError(
            "signal/execution raw-bar key coverage mismatch"
        )

    common_by_instrument: dict[
        str, list[tuple[date, SignalPricePoint, ExecutionPricePoint]]
    ] = defaultdict(list)
    for key in signal_by_key.keys() & execution_by_key.keys():
        signal = signal_by_key[key]
        execution = execution_by_key[key]
        if signal.adjustment_factor != execution.adjustment_factor:
            raise TechnicalFormalDataError(
                f"signal/execution adjustment-factor mismatch:{key}"
            )
        common_by_instrument[key[1]].append((key[0], signal, execution))

    def materially_different(left: Decimal, right: Decimal) -> bool:
        scale = max(abs(left), abs(right), ONE)
        return abs(left - right) > scale * CHANNEL_RELATIVE_TOLERANCE

    for instrument_id, rows in common_by_instrument.items():
        rows.sort(key=lambda item: item[0])
        _, anchor_signal, anchor_execution = rows[0]
        anchor_adjusted_close = (
            anchor_execution.close * anchor_execution.adjustment_factor
        )
        anchor_signal_close = anchor_signal.close
        prior_adjusted_close: Decimal | None = None
        for trading_date, signal, execution in rows:
            adjusted_close = execution.close * execution.adjustment_factor
            for field in ("open", "high", "low", "close"):
                signal_cross = getattr(signal, field) * anchor_adjusted_close
                raw_cross = (
                    getattr(execution, field)
                    * execution.adjustment_factor
                    * anchor_signal_close
                )
                if materially_different(signal_cross, raw_cross):
                    raise TechnicalFormalDataError(
                        "signal/execution causal scale mismatch:"
                        f"{trading_date}:{instrument_id}:{field}"
                    )
            if prior_adjusted_close is not None:
                expected_return = adjusted_close / prior_adjusted_close - ONE
                if signal.daily_return is None or materially_different(
                    signal.daily_return, expected_return
                ):
                    raise TechnicalFormalDataError(
                        "signal daily return does not reconcile to raw close and factor:"
                        f"{trading_date}:{instrument_id}"
                    )
            prior_adjusted_close = adjusted_close


def _action_index(
    values: Sequence[CorporateActionEntitlement],
) -> dict[tuple[date, str], CorporateActionEntitlement]:
    result: dict[tuple[date, str], CorporateActionEntitlement] = {}
    for item in values:
        key = (item.effective_date, item.instrument_id)
        if key in result:
            raise TechnicalFormalDataError(f"duplicate corporate action:{key}")
        result[key] = item
    return result


def _members(loader: PITUniverseLoader, decision_date: date) -> tuple[str, ...]:
    raw = loader.members_strictly_before(decision_date)
    members = tuple(
        str(getattr(item, "component_id", item)).strip().upper() for item in raw
    )
    if len(members) != 800 or len(set(members)) != 800:
        raise TechnicalFormalDataError(
            "formal CSI800 PIT snapshot must contain exactly 800 unique members"
        )
    return members


def _validate_report_execution_coverage(
    *,
    report_dates: Sequence[date],
    universe_loader: PITUniverseLoader,
    execution_by_key: Mapping[tuple[date, str], ExecutionPricePoint],
    status_by_key: Mapping[tuple[date, str], TechnicalExecutionStatus],
    benchmark_id: str,
) -> None:
    """Fail closed on missing daily states or unexplained missing raw bars."""

    for trading_date in report_dates:
        benchmark_key = (trading_date, benchmark_id)
        if benchmark_key not in execution_by_key:
            raise TechnicalFormalDataError(
                f"benchmark missing raw execution bar:{trading_date}"
            )
        if benchmark_key not in status_by_key:
            raise TechnicalFormalDataError(
                f"benchmark missing execution status:{trading_date}"
            )
        for instrument_id in _members(universe_loader, trading_date):
            key = (trading_date, instrument_id)
            status = status_by_key.get(key)
            if status is None:
                raise TechnicalFormalDataError(
                    f"PIT member missing execution status:{trading_date}:{instrument_id}"
                )
            if key not in execution_by_key and not status.suspended:
                raise TechnicalFormalDataError(
                    "non-suspended PIT member missing raw execution bar:"
                    f"{trading_date}:{instrument_id}"
                )


def _build_decision(
    *,
    split_calendar: Sequence[date],
    all_calendar: Sequence[date],
    decision_date: date,
    execution_date: date,
    universe_loader: PITUniverseLoader,
    signal_by_key: Mapping[tuple[date, str], SignalPricePoint],
    status_by_key: Mapping[tuple[date, str], TechnicalExecutionStatus],
    benchmark_id: str,
    positions: Mapping[str, list[_Lot]],
    current_nav: Decimal,
    peak_nav: Decimal,
) -> TechnicalDecision:
    del split_calendar  # split membership is enforced by the caller's next date.
    members = _members(universe_loader, decision_date)
    history_sessions = tuple(day for day in all_calendar if day <= decision_date)
    ranking = rank_technical_formal_universe(
        decision_date=decision_date,
        sessions=history_sessions,
        instrument_ids=members,
        signal_index=signal_by_key,
        benchmark_id=benchmark_id,
        status_index=status_by_key,
    )
    required = tuple(history_sessions[-(ALPHA_LOOKBACK_SESSIONS + 1):])
    benchmark_rows = [
        {"close": str(_signal_close(signal_by_key[(day, benchmark_id)]))}
        for day in required
    ]
    eligible_ids = {item.instrument_id for item in ranking if item.eligibility}
    eligible_rows = [
        [
            {"close": str(_signal_close(signal_by_key[(day, instrument_id)]))}
            for day in required
        ]
        for instrument_id in members
        if instrument_id in eligible_ids
    ]
    exposure = compute_technical_shadow_exposure(
        benchmark_rows=benchmark_rows,
        eligible_stock_rows=eligible_rows,
        current_nav=float(current_nav),
        peak_nav=float(peak_nav),
        policy=FROZEN_EXPOSURE_POLICY,
    )
    target_exposure = _decimal(
        exposure["target_gross_exposure"], "target_gross_exposure"
    )
    quantities = _position_quantities(positions)
    by_id = {row.instrument_id: row for row in ranking}
    incumbents = [
        item
        for item in quantities
        if item in by_id and by_id[item].hold_eligible
    ]
    incumbents.sort(key=lambda item: (by_id[item].rank or 10**9, item))
    entries = [
        row.instrument_id
        for row in ranking
        if row.entry_eligible and row.instrument_id not in incumbents
    ]
    selected = tuple((incumbents + entries)[:MAX_POSITIONS])
    if target_exposure <= ZERO:
        selected = ()
    target_weights: dict[str, Decimal] = {}
    if selected:
        per_weight = min(
            MAX_POSITION_WEIGHT,
            target_exposure / Decimal(len(selected)),
        )
        target_weights = {item: per_weight for item in selected}
    return TechnicalDecision(
        decision_date=decision_date,
        execution_date=execution_date,
        selected_instrument_ids=selected,
        target_weights=target_weights,
        market_state=str(exposure["market_state"]),
        target_gross_exposure=target_exposure,
        eligible_count=len(eligible_ids),
        entry_count=sum(item.entry_eligible for item in ranking),
    )


def _apply_corporate_actions(
    *,
    trading_date: date,
    positions: dict[str, list[_Lot]],
    last_factors: dict[str, Decimal],
    execution_by_key: Mapping[tuple[date, str], ExecutionPricePoint],
    status_by_key: Mapping[tuple[date, str], TechnicalExecutionStatus],
    actions: Mapping[tuple[date, str], CorporateActionEntitlement],
    cash: Decimal,
    instrument_cash_flows: dict[str, Decimal],
    events: list[TechnicalExecutionEvent],
    valuation_closes: dict[str, Decimal],
) -> Decimal:
    for instrument_id in sorted(positions):
        key = (trading_date, instrument_id)
        status = status_by_key.get(key)
        if status is None:
            raise TechnicalFormalDataError(
                f"held instrument lacks execution status:{trading_date}:{instrument_id}"
            )
        previous_factor = last_factors.get(instrument_id)
        if previous_factor is None:
            raise CorporateActionDataGap(
                f"held instrument lacks prior adjustment factor:{instrument_id}"
            )
        entitlement = actions.get(key)
        if entitlement is not None:
            if entitlement.previous_adjustment_factor != previous_factor:
                raise CorporateActionDataGap(
                    f"corporate-action prior-factor mismatch:{trading_date}:{instrument_id}"
                )
            old_quantity = sum(lot.quantity for lot in positions[instrument_id])
            for lot in positions[instrument_id]:
                old_lot_quantity = lot.quantity
                changed = Decimal(old_lot_quantity) * entitlement.share_multiplier
                integral = changed.to_integral_value()
                if changed != integral or integral <= ZERO:
                    raise CorporateActionDataGap(
                        f"corporate action creates non-integral shares:{instrument_id}"
                    )
                # Cash entitlement is part of economic lot PnL.  Reducing the
                # analytical basis avoids classifying a dividend-funded winner
                # as a losing round trip; raw cash/NAV accounting remains below.
                lot.remaining_cost_basis -= (
                    Decimal(old_lot_quantity) * entitlement.cash_per_old_share
                )
                lot.quantity = int(integral)
            cash_credit = _money(
                Decimal(old_quantity) * entitlement.cash_per_old_share
            )
            cash = _money(cash + cash_credit)
            instrument_cash_flows[instrument_id] = _money(
                instrument_cash_flows[instrument_id] + cash_credit
            )
            if instrument_id in valuation_closes:
                ex_entitlement_mark = (
                    valuation_closes[instrument_id]
                    - entitlement.cash_per_old_share
                ) / entitlement.share_multiplier
                if ex_entitlement_mark <= ZERO:
                    raise CorporateActionDataGap(
                        f"corporate action implies non-positive carry mark:{instrument_id}"
                    )
                valuation_closes[instrument_id] = ex_entitlement_mark
            previous_factor = entitlement.new_adjustment_factor
            last_factors[instrument_id] = previous_factor
            events.append(
                TechnicalExecutionEvent(
                    trading_date,
                    "corporate_action_entitlement_applied",
                    instrument_id,
                    f"share_multiplier={entitlement.share_multiplier},cash={cash_credit}",
                )
            )

        point = execution_by_key.get(key)
        if point is None:
            if status.suspended:
                # No price or factor is invented for a suspended no-bar day.
                continue
            if status.delisted or not status.listed:
                raise TechnicalFormalDataError(
                    "delisted residual lacks controlled terminal valuation:"
                    f"{trading_date}:{instrument_id}"
                )
            raise TechnicalFormalDataError(
                f"held non-suspended instrument lacks raw bar:{trading_date}:{instrument_id}"
            )
        current_factor = _decimal(point.adjustment_factor, "adjustment_factor")
        if current_factor != previous_factor:
            raise CorporateActionDataGap(
                "held position crossed a non-unit adjustment-factor change "
                f"without matching entitlement decomposition:{trading_date}:{instrument_id}"
            )
    return cash


def _remove_fifo(
    *,
    lots: list[_Lot],
    quantity: int,
    net_sell_proceeds: Decimal,
    trading_date: date,
    calendar_index: Mapping[date, int],
) -> list[_ClosedLot]:
    remaining = quantity
    proceeds_per_share = net_sell_proceeds / Decimal(quantity)
    closed: list[_ClosedLot] = []
    for lot in list(lots):
        if remaining <= 0:
            break
        if lot.acquired_on >= trading_date:
            continue
        take = min(remaining, lot.quantity)
        basis = lot.remaining_cost_basis * Decimal(take) / Decimal(lot.quantity)
        pnl = proceeds_per_share * Decimal(take) - basis
        closed.append(
            _ClosedLot(
                take,
                pnl,
                calendar_index[trading_date] - calendar_index[lot.acquired_on],
            )
        )
        lot.quantity -= take
        lot.remaining_cost_basis -= basis
        remaining -= take
        if lot.quantity == 0:
            lots.remove(lot)
    if remaining:
        raise TechnicalFormalBacktestError("FIFO removal exceeded sellable quantity")
    return closed


def _execute_decision(
    *,
    decision: TechnicalDecision,
    trading_date: date,
    positions: dict[str, list[_Lot]],
    cash: Decimal,
    execution_by_key: Mapping[tuple[date, str], ExecutionPricePoint],
    status_by_key: Mapping[tuple[date, str], TechnicalExecutionStatus],
    scenario: TechnicalCostScenario,
    calendar_index: Mapping[date, int],
    fills: list[TechnicalFill],
    events: list[TechnicalExecutionEvent],
    closed_lots: list[_ClosedLot],
    instrument_cash_flows: dict[str, Decimal],
    last_factors: dict[str, Decimal],
    valuation_closes: Mapping[str, Decimal],
) -> tuple[Decimal, Decimal]:
    if decision.execution_date != trading_date:
        raise TechnicalFormalBacktestError("decision/execution date mismatch")
    quantities = _position_quantities(positions)
    open_nav = cash
    for instrument_id, quantity in quantities.items():
        key = (trading_date, instrument_id)
        status = status_by_key.get(key)
        if status is None:
            raise TechnicalFormalDataError(
                f"held position lacks status:{trading_date}:{instrument_id}"
            )
        point = execution_by_key.get(key)
        if point is not None:
            mark = _decimal(point.open, "execution open")
        elif status.suspended and instrument_id in valuation_closes:
            mark = valuation_closes[instrument_id]
        else:
            raise TechnicalFormalDataError(
                f"held non-suspended position lacks raw open:{trading_date}:{instrument_id}"
            )
        open_nav += mark * quantity
    if open_nav <= ZERO:
        raise TechnicalFormalBacktestError("open NAV must remain positive")

    targets: dict[str, int] = {item: 0 for item in quantities}
    for instrument_id, weight in decision.target_weights.items():
        key = (trading_date, instrument_id)
        status = status_by_key.get(key)
        if status is None:
            raise TechnicalFormalDataError(
                f"target lacks execution status:{trading_date}:{instrument_id}"
            )
        point = execution_by_key.get(key)
        if point is None:
            if status.suspended:
                targets[instrument_id] = quantities.get(instrument_id, 0)
                events.append(
                    TechnicalExecutionEvent(
                        trading_date,
                        "execution_suspended_no_raw_open",
                        instrument_id,
                    )
                )
                continue
            raise TechnicalFormalDataError(
                f"non-suspended target lacks raw open:{trading_date}:{instrument_id}"
            )
        reference = _decimal(point.open, "execution open")
        raw = open_nav * weight / reference
        targets[instrument_id] = (
            int((raw / LOT_SIZE).to_integral_value(rounding=ROUND_DOWN))
            * LOT_SIZE
        )

    total_reference_notional = ZERO

    def append_fill(
        instrument_id: str,
        side: str,
        quantity: int,
        reference: Decimal,
        cost: Mapping[str, Decimal],
    ) -> None:
        nonlocal total_reference_notional
        total_reference_notional += cost["reference_notional"]
        fills.append(
            TechnicalFill(
                trading_date,
                decision.decision_date,
                instrument_id,
                side,
                quantity,
                reference,
                cost["fill_price"],
                cost["reference_notional"],
                cost["fill_notional"],
                cost["commission"],
                cost["sell_tax"],
                cost["transfer_fee"],
                cost["slippage_cost"],
                cost["total_cost"],
                cost["cash_delta"],
            )
        )

    # Existing exposure is reduced first.  Blocked exits remain in positions
    # and consume slots before any new BUY is considered.
    for instrument_id in sorted(tuple(quantities)):
        current = _position_quantities(positions).get(instrument_id, 0)
        desired = max(0, current - targets.get(instrument_id, 0))
        if desired <= 0:
            continue
        key = (trading_date, instrument_id)
        status = status_by_key.get(key)
        point = execution_by_key.get(key)
        if status is None:
            raise TechnicalFormalDataError(
                f"held position lacks execution status:{trading_date}:{instrument_id}"
            )
        blocked = None
        if status.suspended:
            blocked = "sell_blocked_suspended"
        elif status.limit_down_locked:
            blocked = "sell_blocked_limit_down"
        elif status.delisted or not status.listed:
            blocked = "sell_blocked_delisted"
        if blocked:
            events.append(TechnicalExecutionEvent(trading_date, blocked, instrument_id))
            continue
        if point is None:
            raise TechnicalFormalDataError(
                f"held non-suspended position lacks raw open:{trading_date}:{instrument_id}"
            )
        sellable = sum(
            lot.quantity
            for lot in positions[instrument_id]
            if lot.acquired_on < trading_date
        )
        quantity = min(desired, sellable)
        if quantity <= 0:
            events.append(
                TechnicalExecutionEvent(
                    trading_date, "sell_blocked_t_plus_one", instrument_id
                )
            )
            continue
        if quantity != current:
            rounded = quantity // LOT_SIZE * LOT_SIZE
            if rounded <= 0:
                events.append(
                    TechnicalExecutionEvent(
                        trading_date,
                        "sell_blocked_whole_lot",
                        instrument_id,
                    )
                )
                continue
            quantity = rounded
        if quantity < desired:
            events.append(
                TechnicalExecutionEvent(
                    trading_date,
                    "partial_exit_residual_retained",
                    instrument_id,
                    f"desired={desired},sellable={quantity}",
                )
            )
        reference = _decimal(point.open, "execution open")
        cost = _cost(
            side="SELL",
            quantity=quantity,
            reference_open=reference,
            scenario=scenario,
        )
        cash = _money(cash + cost["cash_delta"])
        instrument_cash_flows[instrument_id] = _money(
            instrument_cash_flows[instrument_id] + cost["cash_delta"]
        )
        closed_lots.extend(
            _remove_fifo(
                lots=positions[instrument_id],
                quantity=quantity,
                net_sell_proceeds=cost["cash_delta"],
                trading_date=trading_date,
                calendar_index=calendar_index,
            )
        )
        if not positions[instrument_id]:
            del positions[instrument_id]
            last_factors.pop(instrument_id, None)
        append_fill(instrument_id, "SELL", quantity, reference, cost)

    for instrument_id in decision.selected_instrument_ids:
        current = _position_quantities(positions).get(instrument_id, 0)
        desired = targets.get(instrument_id, current) - current
        desired = desired // LOT_SIZE * LOT_SIZE
        if desired <= 0:
            continue
        key = (trading_date, instrument_id)
        status = status_by_key.get(key)
        point = execution_by_key.get(key)
        if status is None:
            raise TechnicalFormalDataError(
                f"buy target lacks execution status:{trading_date}:{instrument_id}"
            )
        blocked = None
        if status.suspended:
            blocked = "buy_blocked_suspended"
        elif status.limit_up_locked:
            blocked = "buy_blocked_limit_up"
        elif status.is_st:
            blocked = "buy_blocked_st"
        elif not status.listed or status.delisted:
            blocked = "buy_blocked_not_listed"
        elif instrument_id not in positions and len(positions) >= MAX_POSITIONS:
            blocked = "buy_blocked_residual_position_consumes_slot"
        if blocked:
            events.append(TechnicalExecutionEvent(trading_date, blocked, instrument_id))
            continue
        if point is None:
            raise TechnicalFormalDataError(
                f"non-suspended buy target lacks raw open:{trading_date}:{instrument_id}"
            )
        reference = _decimal(point.open, "execution open")
        quantity = desired
        cost: Mapping[str, Decimal] | None = None
        while quantity > 0:
            candidate = _cost(
                side="BUY",
                quantity=quantity,
                reference_open=reference,
                scenario=scenario,
            )
            if -candidate["cash_delta"] <= cash:
                cost = candidate
                break
            quantity -= LOT_SIZE
        if quantity <= 0 or cost is None:
            events.append(
                TechnicalExecutionEvent(
                    trading_date, "buy_blocked_cash_or_whole_lot", instrument_id
                )
            )
            continue
        cash = _money(cash + cost["cash_delta"])
        instrument_cash_flows[instrument_id] = _money(
            instrument_cash_flows[instrument_id] + cost["cash_delta"]
        )
        positions.setdefault(instrument_id, []).append(
            _Lot(
                quantity,
                trading_date,
                cost["fill_notional"]
                + cost["commission"]
                + cost["transfer_fee"],
            )
        )
        last_factors[instrument_id] = _decimal(
            point.adjustment_factor, "adjustment_factor"
        )
        append_fill(instrument_id, "BUY", quantity, reference, cost)
    if cash < ZERO:
        raise TechnicalFormalBacktestError("execution created negative cash")
    return cash, total_reference_notional


def _performance(
    *,
    initial_cash: Decimal,
    initial_benchmark_close: Decimal,
    nav: Sequence[TechnicalNavPoint],
    fills: Sequence[TechnicalFill],
    closed_lots: Sequence[_ClosedLot],
    exposure_counts: Counter[str],
    instrument_cash_flows: Mapping[str, Decimal],
    ending_market_values: Mapping[str, Decimal],
    total_reference_notional: Decimal,
) -> TechnicalPerformanceMetrics:
    if not nav:
        raise TechnicalFormalBacktestError("performance requires daily NAV")
    ending_nav = nav[-1].nav
    net_return = ending_nav / initial_cash - ONE
    benchmark_return = nav[-1].benchmark_close / initial_benchmark_close - ONE
    net_active = net_return - benchmark_return
    peak = initial_cash
    maximum_drawdown = ZERO
    for point in nav:
        peak = max(peak, point.nav)
        maximum_drawdown = min(maximum_drawdown, point.nav / peak - ONE)
    average_nav = sum((point.nav for point in nav), ZERO) / Decimal(len(nav))
    turnover = (
        Decimal("0.5") * total_reference_notional / average_nav
        if average_nav > ZERO
        else ZERO
    )
    total_cost = sum((item.total_cost for item in fills), ZERO)
    gross_profit = ending_nav - initial_cash + total_cost
    cost_to_gross = total_cost / gross_profit if gross_profit > ZERO else None

    exposure_total = sum(exposure_counts.values())
    distribution = {
        state: (
            Decimal(exposure_counts.get(state, 0)) / Decimal(exposure_total)
            if exposure_total
            else ZERO
        ).quantize(PCT)
        for state in ("RISK_OFF", "DEFENSIVE", "NEUTRAL", "RISK_ON")
    }
    cash_fraction = (
        Decimal(sum(point.market_value == ZERO for point in nav))
        / Decimal(len(nav))
    )

    half_year_groups: dict[str, list[int]] = defaultdict(list)
    for index, point in enumerate(nav):
        label = f"{point.trading_date.year}-H{1 if point.trading_date.month <= 6 else 2}"
        half_year_groups[label].append(index)
    positive_half_years = 0
    previous_end_index: int | None = None
    for label in sorted(half_year_groups):
        indices = half_year_groups[label]
        end = nav[indices[-1]]
        if previous_end_index is None:
            start_nav = initial_cash
            start_benchmark = initial_benchmark_close
        else:
            start_nav = nav[previous_end_index].nav
            start_benchmark = nav[previous_end_index].benchmark_close
        active = (end.nav / start_nav - ONE) - (
            end.benchmark_close / start_benchmark - ONE
        )
        positive_half_years += active > ZERO
        previous_end_index = indices[-1]

    win_rate = (
        Decimal(sum(item.pnl > ZERO for item in closed_lots))
        / Decimal(len(closed_lots))
        if closed_lots
        else None
    )
    closed_quantity = sum(item.quantity for item in closed_lots)
    average_holding = (
        sum(
            Decimal(item.holding_sessions * item.quantity) for item in closed_lots
        )
        / Decimal(closed_quantity)
        if closed_quantity
        else None
    )

    instruments = set(instrument_cash_flows) | set(ending_market_values)
    contributions = {
        item: _money(
            instrument_cash_flows.get(item, ZERO)
            + ending_market_values.get(item, ZERO)
        )
        for item in sorted(instruments)
    }
    contribution_total = sum(contributions.values(), ZERO)
    if abs(contribution_total - (ending_nav - initial_cash)) > CENT:
        raise TechnicalFormalBacktestError("per-stock PnL does not reconcile to NAV")
    absolute_total = sum((abs(value) for value in contributions.values()), ZERO)
    largest_stock = (
        max(abs(value) for value in contributions.values()) / absolute_total
        if absolute_total > ZERO
        else None
    )
    positive_days = sorted(
        (point.daily_pnl for point in nav if point.daily_pnl > ZERO),
        reverse=True,
    )
    largest_ten_days = (
        sum(positive_days[:10], ZERO) / sum(positive_days, ZERO)
        if positive_days
        else None
    )
    return TechnicalPerformanceMetrics(
        net_return=net_return.quantize(PCT),
        benchmark_return=benchmark_return.quantize(PCT),
        net_active_return=net_active.quantize(PCT),
        max_drawdown=maximum_drawdown.quantize(PCT),
        turnover=turnover.quantize(PCT),
        total_cost=_money(total_cost),
        cost_to_gross_profit=(
            cost_to_gross.quantize(PCT) if cost_to_gross is not None else None
        ),
        exposure_state_distribution=distribution,
        cash_day_fraction=cash_fraction.quantize(PCT),
        positive_half_year_count=positive_half_years,
        half_year_count=len(half_year_groups),
        trade_count=len(closed_lots),
        fill_count=len(fills),
        win_rate=win_rate.quantize(PCT) if win_rate is not None else None,
        average_holding_period=(
            average_holding.quantize(PCT)
            if average_holding is not None
            else None
        ),
        per_stock_pnl_contribution=contributions,
        largest_stock_pnl_share=(
            largest_stock.quantize(PCT) if largest_stock is not None else None
        ),
        largest_10_days_pnl_share=(
            largest_ten_days.quantize(PCT)
            if largest_ten_days is not None
            else None
        ),
    )


def _run_scenario(
    *,
    split: str,
    all_calendar: Sequence[date],
    report_dates: Sequence[date],
    universe_loader: PITUniverseLoader,
    signal_by_key: Mapping[tuple[date, str], SignalPricePoint],
    execution_by_key: Mapping[tuple[date, str], ExecutionPricePoint],
    status_by_key: Mapping[tuple[date, str], TechnicalExecutionStatus],
    action_by_key: Mapping[tuple[date, str], CorporateActionEntitlement],
    benchmark_id: str,
    scenario: TechnicalCostScenario,
) -> TechnicalScenarioResult:
    positions: dict[str, list[_Lot]] = {}
    last_factors: dict[str, Decimal] = {}
    cash = INITIAL_CASH
    peak_nav = INITIAL_CASH
    pending: TechnicalDecision | None = None
    fills: list[TechnicalFill] = []
    events: list[TechnicalExecutionEvent] = []
    nav: list[TechnicalNavPoint] = []
    closed_lots: list[_ClosedLot] = []
    instrument_cash_flows: dict[str, Decimal] = defaultdict(lambda: ZERO)
    cumulative_cost = ZERO
    total_reference_notional = ZERO
    exposure_counts: Counter[str] = Counter()
    calendar_index = {day: index for index, day in enumerate(all_calendar)}
    valuation_closes: dict[str, Decimal] = {}
    ending_market_values: dict[str, Decimal] = {}

    first_benchmark = execution_by_key.get((report_dates[0], benchmark_id))
    if first_benchmark is None:
        raise TechnicalFormalDataError("benchmark missing first raw execution bar")
    initial_benchmark_close = _decimal(
        first_benchmark.close, "initial benchmark close"
    )
    previous_nav = INITIAL_CASH

    for index, trading_date in enumerate(report_dates):
        cash = _apply_corporate_actions(
            trading_date=trading_date,
            positions=positions,
            last_factors=last_factors,
            execution_by_key=execution_by_key,
            status_by_key=status_by_key,
            actions=action_by_key,
            cash=cash,
            instrument_cash_flows=instrument_cash_flows,
            events=events,
            valuation_closes=valuation_closes,
        )
        if pending is not None:
            before = len(fills)
            cash, reference_notional = _execute_decision(
                decision=pending,
                trading_date=trading_date,
                positions=positions,
                cash=cash,
                execution_by_key=execution_by_key,
                status_by_key=status_by_key,
                scenario=scenario,
                calendar_index=calendar_index,
                fills=fills,
                events=events,
                closed_lots=closed_lots,
                instrument_cash_flows=instrument_cash_flows,
                last_factors=last_factors,
                valuation_closes=valuation_closes,
            )
            total_reference_notional += reference_notional
            cumulative_cost += sum(
                (item.total_cost for item in fills[before:]), ZERO
            )

        quantities = _position_quantities(positions)
        ending_market_values = _mark_positions_at_raw_close(
            trading_date=trading_date,
            positions=positions,
            execution_by_key=execution_by_key,
            status_by_key=status_by_key,
            valuation_closes=valuation_closes,
            events=events,
        )
        market_value = sum(ending_market_values.values(), ZERO)
        current_nav = _money(cash + market_value)
        if current_nav <= ZERO:
            raise TechnicalFormalBacktestError("strategy NAV became non-positive")
        peak_nav = max(peak_nav, current_nav)

        next_date = report_dates[index + 1] if index + 1 < len(report_dates) else None
        # Compute the same-day state for a complete state distribution.  A
        # decision is persisted only when its D+1 remains inside this split.
        state_decision = _build_decision(
            split_calendar=report_dates,
            all_calendar=all_calendar,
            decision_date=trading_date,
            execution_date=next_date or trading_date,
            universe_loader=universe_loader,
            signal_by_key=signal_by_key,
            status_by_key=status_by_key,
            benchmark_id=benchmark_id,
            positions=positions,
            current_nav=current_nav,
            peak_nav=peak_nav,
        )
        exposure_counts[state_decision.market_state] += 1
        pending = state_decision if next_date is not None else None

        benchmark = execution_by_key.get((trading_date, benchmark_id))
        if benchmark is None:
            raise TechnicalFormalDataError(
                f"benchmark missing raw close:{trading_date}"
            )
        nav.append(
            TechnicalNavPoint(
                trading_date,
                _money(cash),
                _money(market_value),
                current_nav,
                _money(current_nav - previous_nav),
                _money(cumulative_cost),
                _decimal(benchmark.close, "benchmark close"),
                state_decision.market_state,
                state_decision.target_gross_exposure,
                (market_value / current_nav).quantize(PCT),
            )
        )
        previous_nav = current_nav

    quantities = _position_quantities(positions)
    metrics = _performance(
        initial_cash=INITIAL_CASH,
        initial_benchmark_close=initial_benchmark_close,
        nav=nav,
        fills=fills,
        closed_lots=closed_lots,
        exposure_counts=exposure_counts,
        instrument_cash_flows=instrument_cash_flows,
        ending_market_values=ending_market_values,
        total_reference_notional=total_reference_notional,
    )
    return TechnicalScenarioResult(
        scenario=scenario.name,
        split=split,
        start_date=report_dates[0],
        end_date=report_dates[-1],
        metrics=metrics,
        nav=tuple(nav),
        fills=tuple(fills),
        events=tuple(events),
        ending_positions=dict(sorted(quantities.items())),
    )


def run_technical_formal_backtest(
    *,
    split: str,
    trading_calendar: TechnicalInputPartition,
    universe_loader: PITUniverseLoader,
    signal_prices: TechnicalInputPartition,
    execution_prices: TechnicalInputPartition,
    execution_statuses: TechnicalInputPartition,
    benchmark_id: str,
    corporate_actions: TechnicalInputPartition | None = None,
) -> TechnicalBacktestComparison:
    """Run base and stress scenarios for development or validation only.

    The split guard intentionally precedes *all* iteration, type checks, loader
    method calls, or indexing.  A sentinel iterable therefore proves that a
    ``locked_test`` request cannot even read one input row.
    """

    if split not in SPLIT_WINDOWS:
        if str(split) == "locked_test":
            raise LockedTestAccessForbidden(
                "locked_test is NOT_RUN and cannot be read or executed"
            )
        raise LockedTestAccessForbidden(
            "only development and validation splits are permitted"
        )

    window_start, window_end = SPLIT_WINDOWS[split]
    if type(universe_loader) is not PITUniverseLoader:
        raise TechnicalFormalDataError("universe_loader requires exact PITUniverseLoader")
    # Reading coverage metadata is permitted; traversing a loader that still
    # contains 2024-2025 membership is not.  Callers must construct a physical
    # development/validation PIT partition before this engine can select even
    # one snapshot.
    if universe_loader.coverage_end != window_end:
        raise TechnicalFormalDataError(
            "PIT loader must be physically partitioned exactly to split_end"
        )
    if universe_loader.coverage_start > window_start:
        raise TechnicalFormalDataError(
            "PIT loader does not cover split_start"
        )

    supplied_partitions = (
        (trading_calendar, "trading_calendar"),
        (signal_prices, "signal_prices"),
        (execution_prices, "execution_prices"),
        (execution_statuses, "execution_statuses"),
    ) + (
        ((corporate_actions, "corporate_actions"),)
        if corporate_actions is not None
        else ()
    )
    # This entire pass is metadata-only.  A late invalid sibling therefore
    # cannot cause an earlier partition to be materialized first.
    for partition, dataset_id in supplied_partitions:
        _validate_input_partition_metadata(
            partition,  # type: ignore[arg-type]
            expected_dataset_id=dataset_id,
            split_start=window_start,
            split_end=window_end,
        )

    benchmark = str(benchmark_id).strip().upper()
    if not benchmark:
        raise TechnicalFormalDataError("benchmark_id is required")
    calendar = _partition_rows(
        trading_calendar,
        expected_type=date,
        date_field=None,
    )
    if (
        not calendar
        or tuple(sorted(calendar)) != calendar
        or len(set(calendar)) != len(calendar)
    ):
        raise TechnicalFormalDataError(
            "trading calendar must be non-empty, exact-date, unique, and ordered"
        )
    report_dates = tuple(
        day for day in calendar if window_start <= day <= window_end
    )
    if len(report_dates) < 2:
        raise TechnicalFormalDataError("split requires at least two trading sessions")

    signals = _partition_rows(
        signal_prices,
        expected_type=SignalPricePoint,
        date_field="trading_date",
    )
    executions = _partition_rows(
        execution_prices,
        expected_type=ExecutionPricePoint,
        date_field="trading_date",
    )
    statuses = _partition_rows(
        execution_statuses,
        expected_type=TechnicalExecutionStatus,
        date_field="trading_date",
    )
    actions = (
        _partition_rows(
            corporate_actions,
            expected_type=CorporateActionEntitlement,
            date_field="effective_date",
        )
        if corporate_actions is not None
        else ()
    )

    signal_by_key = _signal_index(signals)
    execution_by_key = _execution_index(executions)
    status_by_key = _status_index(statuses)
    action_by_key = _action_index(actions)
    _validate_execution_status_alignment(
        execution_by_key=execution_by_key,
        status_by_key=status_by_key,
    )
    _validate_signal_execution_factor_alignment(
        signal_by_key=signal_by_key,
        execution_by_key=execution_by_key,
    )
    _validate_report_execution_coverage(
        report_dates=report_dates,
        universe_loader=universe_loader,
        execution_by_key=execution_by_key,
        status_by_key=status_by_key,
        benchmark_id=benchmark,
    )
    base = _run_scenario(
        split=split,
        all_calendar=calendar,
        report_dates=report_dates,
        universe_loader=universe_loader,
        signal_by_key=signal_by_key,
        execution_by_key=execution_by_key,
        status_by_key=status_by_key,
        action_by_key=action_by_key,
        benchmark_id=benchmark,
        scenario=BASE_COST,
    )
    stress = _run_scenario(
        split=split,
        all_calendar=calendar,
        report_dates=report_dates,
        universe_loader=universe_loader,
        signal_by_key=signal_by_key,
        execution_by_key=execution_by_key,
        status_by_key=status_by_key,
        action_by_key=action_by_key,
        benchmark_id=benchmark,
        scenario=STRESS_COST,
    )
    return TechnicalBacktestComparison(
        strategy_id=STRATEGY_ID,
        engine_version=ENGINE_VERSION,
        split=split,
        base=base,
        stress=stress,
    )


__all__ = [
    "BASE_COST",
    "CorporateActionDataGap",
    "CorporateActionEntitlement",
    "ENGINE_VERSION",
    "FACTOR_DIRECTIONS",
    "FACTOR_IDS",
    "FROZEN_EXPOSURE_POLICY",
    "LOCKED_TEST_CONSUMED",
    "LOCKED_TEST_STATUS",
    "LockedTestAccessForbidden",
    "STRESS_COST",
    "STRATEGY_ID",
    "TechnicalBacktestComparison",
    "TechnicalCostScenario",
    "TechnicalDecision",
    "TechnicalExecutionEvent",
    "TechnicalExecutionStatus",
    "TechnicalFill",
    "TechnicalFormalBacktestError",
    "TechnicalFormalDataError",
    "TechnicalInputPartition",
    "TechnicalNavPoint",
    "TechnicalPerformanceMetrics",
    "TechnicalRankRow",
    "TechnicalScenarioResult",
    "rank_technical_formal_universe",
    "run_technical_formal_backtest",
]
