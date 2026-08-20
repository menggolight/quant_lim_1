"""Simple, low-frequency, long-only backtesting with explicit costs.

Signals are immutable target universes.  A signal frozen on date ``t`` is
executed at the close of the first controlled trading-calendar session after
``t``.  The engine deliberately supports only equal-weight long positions,
round lots, at most three instruments, and cash.  It is a research backtest;
it has no broker or LIVE execution path.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, ROUND_DOWN
from typing import Any, Iterable, Mapping, Sequence

from .attribution import AttributionReport, build_attribution


ZERO = Decimal("0")
ONE = Decimal("1")
MONEY_QUANTUM = Decimal("0.0001")
BACKTEST_ENGINE_VERSION = "strategy-workspace-backtest.v1"


class BacktestInputError(ValueError):
    """Raised when an input would make the backtest ambiguous or unsafe."""


def _decimal(value: Any, context: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:  # pragma: no cover - defensive boundary
        raise BacktestInputError(f"{context} must be decimal-compatible") from exc
    if not result.is_finite():
        raise BacktestInputError(f"{context} must be finite")
    return result


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM)


def _date(value: Any, context: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except Exception as exc:
        raise BacktestInputError(f"{context} must be an ISO date") from exc


@dataclass(frozen=True)
class DailyClose:
    instrument_id: str
    trading_date: date
    close: Decimal
    lot_size: int = 100

    def __post_init__(self) -> None:
        instrument_id = str(self.instrument_id).strip()
        if not instrument_id:
            raise BacktestInputError("instrument_id must not be empty")
        trading_date = _date(self.trading_date, "trading_date")
        close = _decimal(self.close, "close")
        if close <= 0:
            raise BacktestInputError("close must be positive")
        if type(self.lot_size) is not int or self.lot_size <= 0:
            raise BacktestInputError("lot_size must be a positive integer")
        object.__setattr__(self, "instrument_id", instrument_id)
        object.__setattr__(self, "trading_date", trading_date)
        object.__setattr__(self, "close", close)


@dataclass(frozen=True)
class BenchmarkClose:
    trading_date: date
    close: Decimal

    def __post_init__(self) -> None:
        trading_date = _date(self.trading_date, "benchmark trading_date")
        close = _decimal(self.close, "benchmark close")
        if close <= 0:
            raise BacktestInputError("benchmark close must be positive")
        object.__setattr__(self, "trading_date", trading_date)
        object.__setattr__(self, "close", close)


@dataclass(frozen=True)
class FrozenSignal:
    signal_id: str
    signal_date: date
    instrument_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        signal_id = str(self.signal_id).strip()
        if not signal_id:
            raise BacktestInputError("signal_id must not be empty")
        signal_date = _date(self.signal_date, "signal_date")
        instruments = tuple(str(value).strip() for value in self.instrument_ids)
        if any(not value for value in instruments):
            raise BacktestInputError("signal instruments must not be empty")
        if len(instruments) != len(set(instruments)):
            raise BacktestInputError("signal instruments must be unique")
        if len(instruments) > 3:
            raise BacktestInputError("a frozen signal may contain at most three instruments")
        object.__setattr__(self, "signal_id", signal_id)
        object.__setattr__(self, "signal_date", signal_date)
        object.__setattr__(self, "instrument_ids", instruments)


@dataclass(frozen=True)
class CostModel:
    commission_rate: Decimal = Decimal("0.00018")
    minimum_commission: Decimal = Decimal("5")
    sell_tax_rate: Decimal = ZERO
    transfer_fee_rate: Decimal = ZERO
    slippage_bps: Decimal = ZERO

    def __post_init__(self) -> None:
        for field_name in (
            "commission_rate",
            "minimum_commission",
            "sell_tax_rate",
            "transfer_fee_rate",
            "slippage_bps",
        ):
            value = _decimal(getattr(self, field_name), field_name)
            if value < 0:
                raise BacktestInputError(f"{field_name} must not be negative")
            object.__setattr__(self, field_name, value)
        if self.commission_rate >= ONE:
            raise BacktestInputError("commission_rate must be below one")
        if self.sell_tax_rate >= ONE or self.transfer_fee_rate >= ONE:
            raise BacktestInputError("tax and transfer rates must be below one")
        if self.slippage_bps >= Decimal("10000"):
            raise BacktestInputError("slippage_bps must be below 10000")

    @property
    def slippage_rate(self) -> Decimal:
        return self.slippage_bps / Decimal("10000")

    def fill_price(self, reference_close: Decimal, side: str) -> Decimal:
        if side == "BUY":
            return reference_close * (ONE + self.slippage_rate)
        if side == "SELL":
            return reference_close * (ONE - self.slippage_rate)
        raise BacktestInputError(f"unsupported trade side: {side}")

    def explicit_fees(self, notional: Decimal, side: str) -> tuple[Decimal, Decimal, Decimal]:
        commission = max(notional * self.commission_rate, self.minimum_commission)
        sell_tax = notional * self.sell_tax_rate if side == "SELL" else ZERO
        transfer_fee = notional * self.transfer_fee_rate
        return _money(commission), _money(sell_tax), _money(transfer_fee)


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: Decimal = Decimal("10000")
    cash_reserve_ratio: Decimal = Decimal("0.10")
    max_positions: int = 3
    max_stale_sessions: int = 5
    annualization_sessions: int = 252
    costs: CostModel = field(default_factory=CostModel)

    def __post_init__(self) -> None:
        initial_cash = _decimal(self.initial_cash, "initial_cash")
        cash_reserve_ratio = _decimal(self.cash_reserve_ratio, "cash_reserve_ratio")
        if initial_cash <= 0:
            raise BacktestInputError("initial_cash must be positive")
        if cash_reserve_ratio < 0 or cash_reserve_ratio >= ONE:
            raise BacktestInputError("cash_reserve_ratio must be in [0, 1)")
        if type(self.max_positions) is not int or not 1 <= self.max_positions <= 3:
            raise BacktestInputError("max_positions must be between one and three")
        if (
            type(self.max_stale_sessions) is not int
            or self.max_stale_sessions < 0
        ):
            raise BacktestInputError("max_stale_sessions must be a non-negative integer")
        if type(self.annualization_sessions) is not int or self.annualization_sessions <= 0:
            raise BacktestInputError("annualization_sessions must be positive")
        if not isinstance(self.costs, CostModel):
            raise BacktestInputError("costs must be a CostModel")
        object.__setattr__(self, "initial_cash", initial_cash)
        object.__setattr__(self, "cash_reserve_ratio", cash_reserve_ratio)


@dataclass(frozen=True)
class TradeRecord:
    trade_id: str
    signal_id: str
    signal_date: date
    execution_date: date
    instrument_id: str
    side: str
    quantity: int
    lot_size: int
    reference_close: Decimal
    fill_price: Decimal
    notional: Decimal
    commission: Decimal
    sell_tax: Decimal
    transfer_fee: Decimal
    slippage_cost: Decimal
    total_cost: Decimal
    cash_after: Decimal


@dataclass(frozen=True)
class TradeSkip:
    signal_id: str
    signal_date: date
    execution_date: date | None
    instrument_id: str | None
    side: str | None
    reason_code: str
    detail: str


@dataclass(frozen=True)
class NavPoint:
    trading_date: date
    net_nav: Decimal
    cost_addback_nav: Decimal
    cash: Decimal
    market_value: Decimal
    cumulative_cost: Decimal
    benchmark_close: Decimal | None


@dataclass(frozen=True)
class PerformanceMetrics:
    start_date: date
    end_date: date
    trading_sessions: int
    ending_nav: Decimal
    total_cost: Decimal
    cost_addback_return: float
    net_return: float
    benchmark_return: float | None
    active_return: float | None
    annualized_return: float | None
    annualized_volatility: float | None
    sharpe: float | None
    max_drawdown: float
    turnover: float


@dataclass(frozen=True)
class BacktestResult:
    nav: tuple[NavPoint, ...]
    trades: tuple[TradeRecord, ...]
    skips: tuple[TradeSkip, ...]
    metrics: PerformanceMetrics
    attribution: AttributionReport
    ending_positions: Mapping[str, int]

    @property
    def nav_series(self) -> tuple[NavPoint, ...]:
        return self.nav

    @property
    def trade_ledger(self) -> tuple[TradeRecord, ...]:
        return self.trades

    @property
    def skip_ledger(self) -> tuple[TradeSkip, ...]:
        return self.skips


def _coerce_bar(value: DailyClose | Mapping[str, Any]) -> DailyClose:
    if isinstance(value, DailyClose):
        return value
    if not isinstance(value, Mapping):
        raise BacktestInputError("bars must contain DailyClose objects or mappings")
    return DailyClose(
        instrument_id=str(value.get("instrument_id") or value.get("symbol") or ""),
        trading_date=_date(
            value.get("trading_date", value.get("date")), "bar.trading_date"
        ),
        close=_decimal(value.get("close"), "bar.close"),
        lot_size=int(value.get("lot_size", 100)),
    )


def _coerce_signal(value: FrozenSignal | Mapping[str, Any]) -> FrozenSignal:
    if isinstance(value, FrozenSignal):
        return value
    if not isinstance(value, Mapping):
        raise BacktestInputError("signals must contain FrozenSignal objects or mappings")
    raw_instruments = value.get("instrument_ids", value.get("targets", ()))
    if isinstance(raw_instruments, str):
        raw_instruments = (raw_instruments,)
    return FrozenSignal(
        signal_id=str(value.get("signal_id") or ""),
        signal_date=_date(value.get("signal_date", value.get("frozen_date")), "signal_date"),
        instrument_ids=tuple(raw_instruments or ()),
    )


def _coerce_benchmark(value: BenchmarkClose | Mapping[str, Any]) -> BenchmarkClose:
    if isinstance(value, BenchmarkClose):
        return value
    if not isinstance(value, Mapping):
        raise BacktestInputError("benchmark must contain BenchmarkClose objects or mappings")
    return BenchmarkClose(
        trading_date=_date(
            value.get("trading_date", value.get("date")),
            "benchmark.trading_date",
        ),
        close=_decimal(value.get("close"), "benchmark.close"),
    )


def _index_bars(bars: Iterable[DailyClose | Mapping[str, Any]]) -> dict[tuple[date, str], DailyClose]:
    result: dict[tuple[date, str], DailyClose] = {}
    for raw in bars:
        bar = _coerce_bar(raw)
        key = (bar.trading_date, bar.instrument_id)
        if key in result:
            raise BacktestInputError(
                f"duplicate close for {bar.instrument_id} on {bar.trading_date.isoformat()}"
            )
        result[key] = bar
    if not result:
        raise BacktestInputError("at least one daily close is required")
    return result


def _index_benchmark(
    values: Iterable[BenchmarkClose | Mapping[str, Any]],
) -> dict[date, Decimal]:
    result: dict[date, Decimal] = {}
    for raw in values:
        item = _coerce_benchmark(raw)
        if item.trading_date in result:
            raise BacktestInputError(
                f"duplicate benchmark close on {item.trading_date.isoformat()}"
            )
        result[item.trading_date] = item.close
    return result


def _next_session(calendar: Sequence[date], signal_date: date) -> date | None:
    return next((day for day in calendar if day > signal_date), None)


def _mark_to_market(
    cash: Decimal,
    positions: Mapping[str, int],
    latest_prices: Mapping[str, Decimal],
) -> tuple[Decimal, Decimal]:
    missing = [instrument_id for instrument_id in positions if instrument_id not in latest_prices]
    if missing:
        raise BacktestInputError(
            "held positions lack a current or prior close: " + ",".join(sorted(missing))
        )
    market_value = sum(
        (latest_prices[instrument_id] * quantity for instrument_id, quantity in positions.items()),
        ZERO,
    )
    return cash + market_value, market_value


def _trade_costs(
    *,
    quantity: int,
    reference_close: Decimal,
    side: str,
    costs: CostModel,
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal, Decimal, Decimal]:
    fill_price = costs.fill_price(reference_close, side)
    notional = _money(fill_price * quantity)
    commission, sell_tax, transfer_fee = costs.explicit_fees(notional, side)
    slippage_cost = _money(abs(fill_price - reference_close) * quantity)
    total_cost = _money(commission + sell_tax + transfer_fee + slippage_cost)
    return (
        fill_price,
        notional,
        commission,
        sell_tax,
        transfer_fee,
        slippage_cost,
        total_cost,
    )


def _maximum_affordable_quantity(
    *,
    desired_quantity: int,
    lot_size: int,
    reference_close: Decimal,
    cash: Decimal,
    reserve_floor: Decimal,
    costs: CostModel,
) -> int:
    desired_lots = desired_quantity // lot_size
    low, high = 0, desired_lots
    while low < high:
        middle = (low + high + 1) // 2
        quantity = middle * lot_size
        _, notional, commission, _, transfer_fee, _, _ = _trade_costs(
            quantity=quantity,
            reference_close=reference_close,
            side="BUY",
            costs=costs,
        )
        cash_out = notional + commission + transfer_fee
        if cash - cash_out >= reserve_floor:
            low = middle
        else:
            high = middle - 1
    return low * lot_size


def _execute_rebalance(
    *,
    signal: FrozenSignal,
    execution_date: date,
    exact_bars: Mapping[str, DailyClose],
    latest_prices: Mapping[str, Decimal],
    cash: Decimal,
    positions: dict[str, int],
    config: BacktestConfig,
    trade_sequence: int,
) -> tuple[Decimal, list[TradeRecord], list[TradeSkip], Decimal, int, Decimal]:
    if len(signal.instrument_ids) > config.max_positions:
        raise BacktestInputError(
            f"signal {signal.signal_id} exceeds configured max_positions"
        )
    equity_before, _ = _mark_to_market(cash, positions, latest_prices)
    reserve_floor = equity_before * config.cash_reserve_ratio
    trades: list[TradeRecord] = []
    skips: list[TradeSkip] = []
    total_cost = ZERO
    traded_notional = ZERO

    target_quantities: dict[str, int] = {}
    if signal.instrument_ids:
        target_notional = (
            equity_before * (ONE - config.cash_reserve_ratio) / len(signal.instrument_ids)
        )
        for instrument_id in signal.instrument_ids:
            bar = exact_bars.get(instrument_id)
            if bar is None:
                # An already-held selected instrument is held unchanged when
                # the required execution-session close is missing.  Do not
                # reinterpret missing data as a target of zero and sell it.
                target_quantities[instrument_id] = positions.get(instrument_id, 0)
                skips.append(
                    TradeSkip(
                        signal.signal_id,
                        signal.signal_date,
                        execution_date,
                        instrument_id,
                        "BUY",
                        "missing_execution_price",
                        "target instrument has no close on the required execution session",
                    )
                )
                continue
            lots = int(
                (target_notional / (bar.close * bar.lot_size)).to_integral_value(
                    rounding=ROUND_DOWN
                )
            )
            quantity = lots * bar.lot_size
            target_quantities[instrument_id] = quantity
            if quantity == 0 and positions.get(instrument_id, 0) == 0:
                skips.append(
                    TradeSkip(
                        signal.signal_id,
                        signal.signal_date,
                        execution_date,
                        instrument_id,
                        "BUY",
                        "target_below_one_lot",
                        "equal-weight target cannot purchase one configured round lot",
                    )
                )

    def record_trade(instrument_id: str, side: str, quantity: int, bar: DailyClose) -> None:
        nonlocal cash, total_cost, traded_notional, trade_sequence
        (
            fill_price,
            notional,
            commission,
            sell_tax,
            transfer_fee,
            slippage_cost,
            cost,
        ) = _trade_costs(
            quantity=quantity,
            reference_close=bar.close,
            side=side,
            costs=config.costs,
        )
        explicit_fees = commission + sell_tax + transfer_fee
        if side == "SELL":
            cash = cash + notional - explicit_fees
            positions[instrument_id] = positions.get(instrument_id, 0) - quantity
            if positions[instrument_id] == 0:
                positions.pop(instrument_id)
        else:
            cash = cash - notional - explicit_fees
            positions[instrument_id] = positions.get(instrument_id, 0) + quantity
        cash = _money(cash)
        total_cost += cost
        traded_notional += notional
        trade_sequence += 1
        trades.append(
            TradeRecord(
                trade_id=(
                    f"{signal.signal_id}:{execution_date.isoformat()}:"
                    f"{trade_sequence:04d}:{side}:{instrument_id}"
                ),
                signal_id=signal.signal_id,
                signal_date=signal.signal_date,
                execution_date=execution_date,
                instrument_id=instrument_id,
                side=side,
                quantity=quantity,
                lot_size=bar.lot_size,
                reference_close=bar.close,
                fill_price=fill_price,
                notional=notional,
                commission=commission,
                sell_tax=sell_tax,
                transfer_fee=transfer_fee,
                slippage_cost=slippage_cost,
                total_cost=cost,
                cash_after=cash,
            )
        )

    # Raise cash first.  A holding absent from the frozen signal has target zero.
    sell_candidates = list(positions)
    for instrument_id in sell_candidates:
        current = positions.get(instrument_id, 0)
        target = target_quantities.get(instrument_id, 0)
        quantity = current - target
        if quantity <= 0:
            continue
        bar = exact_bars.get(instrument_id)
        if bar is None:
            skips.append(
                TradeSkip(
                    signal.signal_id,
                    signal.signal_date,
                    execution_date,
                    instrument_id,
                    "SELL",
                    "missing_execution_price",
                    "held instrument cannot be sold without an execution-session close",
                )
            )
            continue
        quantity = quantity // bar.lot_size * bar.lot_size
        if quantity <= 0:
            skips.append(
                TradeSkip(
                    signal.signal_id,
                    signal.signal_date,
                    execution_date,
                    instrument_id,
                    "SELL",
                    "round_lot_no_trade",
                    "sell difference is smaller than one configured round lot",
                )
            )
            continue
        _, notional, commission, sell_tax, transfer_fee, _, _ = _trade_costs(
            quantity=quantity,
            reference_close=bar.close,
            side="SELL",
            costs=config.costs,
        )
        if notional <= commission + sell_tax + transfer_fee:
            skips.append(
                TradeSkip(
                    signal.signal_id,
                    signal.signal_date,
                    execution_date,
                    instrument_id,
                    "SELL",
                    "cost_exceeds_sell_proceeds",
                    "explicit sell costs are not below sale proceeds",
                )
            )
            continue
        record_trade(instrument_id, "SELL", quantity, bar)

    # Preserve the signal order as a deterministic rank preference if round-lot
    # and minimum-commission effects leave insufficient cash for every target.
    for instrument_id in signal.instrument_ids:
        if instrument_id not in target_quantities or instrument_id not in exact_bars:
            continue
        bar = exact_bars[instrument_id]
        desired = target_quantities[instrument_id] - positions.get(instrument_id, 0)
        desired = desired // bar.lot_size * bar.lot_size
        if desired <= 0:
            continue
        affordable = _maximum_affordable_quantity(
            desired_quantity=desired,
            lot_size=bar.lot_size,
            reference_close=bar.close,
            cash=cash,
            reserve_floor=reserve_floor,
            costs=config.costs,
        )
        if affordable <= 0:
            skips.append(
                TradeSkip(
                    signal.signal_id,
                    signal.signal_date,
                    execution_date,
                    instrument_id,
                    "BUY",
                    "insufficient_cash_after_costs",
                    "cash cannot fund one lot and configured costs while preserving the cash reserve",
                )
            )
            continue
        if affordable < desired:
            skips.append(
                TradeSkip(
                    signal.signal_id,
                    signal.signal_date,
                    execution_date,
                    instrument_id,
                    "BUY",
                    "buy_reduced_for_cash_and_costs",
                    f"desired {desired} shares but only {affordable} preserve the cash reserve",
                )
            )
        record_trade(instrument_id, "BUY", affordable, bar)

    if cash < reserve_floor - MONEY_QUANTUM:
        raise BacktestInputError("rebalance breached the configured cash reserve")
    return cash, trades, skips, _money(total_cost), trade_sequence, traded_notional / equity_before


def _performance_metrics(
    nav: Sequence[NavPoint],
    *,
    initial_cash: Decimal,
    annualization_sessions: int,
    turnover: Decimal,
    initial_benchmark_close: Decimal | None,
) -> PerformanceMetrics:
    if not nav:
        raise BacktestInputError("cannot calculate metrics without NAV points")
    ending = nav[-1].net_nav
    total_cost = nav[-1].cumulative_cost
    cost_addback_return = float((ending + total_cost) / initial_cash - ONE)
    net_return = float(ending / initial_cash - ONE)

    benchmark_values = [point.benchmark_close for point in nav if point.benchmark_close is not None]
    benchmark_start = initial_benchmark_close
    if benchmark_start is None and benchmark_values:
        benchmark_start = benchmark_values[0]
    benchmark_return = (
        float(benchmark_values[-1] / benchmark_start - ONE)
        if benchmark_values and benchmark_start is not None
        else None
    )
    active_return = net_return - benchmark_return if benchmark_return is not None else None

    session_count = len(nav)
    if ending <= 0:
        annualized_return = -1.0
    else:
        annualized_return = (float(ending / initial_cash) ** (annualization_sessions / session_count)) - 1.0

    returns: list[float] = []
    previous = initial_cash
    for point in nav:
        returns.append(float(point.net_nav / previous - ONE))
        previous = point.net_nav
    if len(returns) >= 2:
        daily_std = statistics.stdev(returns)
        annualized_volatility = daily_std * math.sqrt(annualization_sessions)
        sharpe = (
            statistics.fmean(returns) / daily_std * math.sqrt(annualization_sessions)
            if daily_std > 0
            else None
        )
    else:
        annualized_volatility = None
        sharpe = None

    peak = initial_cash
    max_drawdown = 0.0
    for point in nav:
        peak = max(peak, point.net_nav)
        drawdown = float(point.net_nav / peak - ONE)
        max_drawdown = min(max_drawdown, drawdown)

    return PerformanceMetrics(
        start_date=nav[0].trading_date,
        end_date=nav[-1].trading_date,
        trading_sessions=session_count,
        ending_nav=ending,
        total_cost=total_cost,
        cost_addback_return=cost_addback_return,
        net_return=net_return,
        benchmark_return=benchmark_return,
        active_return=active_return,
        annualized_return=annualized_return,
        annualized_volatility=annualized_volatility,
        sharpe=sharpe,
        max_drawdown=max_drawdown,
        turnover=float(turnover),
    )


def run_backtest(
    signals: Iterable[FrozenSignal | Mapping[str, Any]],
    bars: Iterable[DailyClose | Mapping[str, Any]],
    *,
    benchmark: Iterable[BenchmarkClose | Mapping[str, Any]] = (),
    trading_calendar: Iterable[date | str] | None = None,
    config: BacktestConfig | None = None,
) -> BacktestResult:
    """Run the frozen-signal, next-session-close long-only backtest."""

    resolved_config = config or BacktestConfig()
    if not isinstance(resolved_config, BacktestConfig):
        raise BacktestInputError("config must be a BacktestConfig")
    indexed_bars = _index_bars(bars)
    indexed_benchmark = _index_benchmark(benchmark)
    materialized_signals = sorted(
        (_coerce_signal(value) for value in signals),
        key=lambda item: (item.signal_date, item.signal_id),
    )
    if len({item.signal_id for item in materialized_signals}) != len(materialized_signals):
        raise BacktestInputError("signal_id values must be unique")

    if trading_calendar is None:
        calendar = sorted({day for day, _ in indexed_bars})
    else:
        calendar = sorted({_date(value, "trading_calendar") for value in trading_calendar})
    if not calendar:
        raise BacktestInputError("trading_calendar must contain at least one session")

    execution_by_date: dict[date, FrozenSignal] = {}
    skips: list[TradeSkip] = []
    for signal in materialized_signals:
        execution_date = _next_session(calendar, signal.signal_date)
        if execution_date is None:
            skips.append(
                TradeSkip(
                    signal.signal_id,
                    signal.signal_date,
                    None,
                    None,
                    None,
                    "no_next_trading_session",
                    "the controlled calendar has no session after the frozen signal date",
                )
            )
            continue
        if execution_date in execution_by_date:
            raise BacktestInputError(
                f"multiple frozen signals resolve to execution date {execution_date.isoformat()}"
            )
        execution_by_date[execution_date] = signal

    if materialized_signals:
        first_signal_date = materialized_signals[0].signal_date
        timeline = [day for day in calendar if day >= first_signal_date]
        if not timeline:
            timeline = [calendar[-1]]
    else:
        timeline = list(calendar)

    bars_by_date: dict[date, dict[str, DailyClose]] = {}
    for (trading_date, instrument_id), bar in indexed_bars.items():
        bars_by_date.setdefault(trading_date, {})[instrument_id] = bar

    initial_benchmark_close: Decimal | None = None
    first_timeline_date = timeline[0]
    prior_benchmark_dates = [day for day in indexed_benchmark if day < first_timeline_date]
    if prior_benchmark_dates:
        initial_benchmark_close = indexed_benchmark[max(prior_benchmark_dates)]

    cash = resolved_config.initial_cash
    positions: dict[str, int] = {}
    latest_prices: dict[str, Decimal] = {}
    last_price_session_index: dict[str, int] = {}
    latest_benchmark = initial_benchmark_close
    last_benchmark_session_index = -1 if initial_benchmark_close is not None else None
    cumulative_cost = ZERO
    cumulative_turnover = ZERO
    trade_sequence = 0
    trades: list[TradeRecord] = []
    nav: list[NavPoint] = []

    # Seed latest closes from data strictly before the visible timeline.
    for day in sorted(day for day in bars_by_date if day < first_timeline_date):
        for instrument_id, bar in bars_by_date[day].items():
            latest_prices[instrument_id] = bar.close

    for session_index, trading_date in enumerate(timeline):
        exact_bars = bars_by_date.get(trading_date, {})
        for instrument_id, bar in exact_bars.items():
            latest_prices[instrument_id] = bar.close
            last_price_session_index[instrument_id] = session_index
        if trading_date in indexed_benchmark:
            latest_benchmark = indexed_benchmark[trading_date]
            last_benchmark_session_index = session_index

        stale_positions = [
            instrument_id
            for instrument_id in positions
            if instrument_id not in last_price_session_index
            or session_index - last_price_session_index[instrument_id]
            > resolved_config.max_stale_sessions
        ]
        if stale_positions:
            raise BacktestInputError(
                "held position price exceeds max_stale_sessions: "
                + ",".join(sorted(stale_positions))
            )
        if (
            indexed_benchmark
            and last_benchmark_session_index is not None
            and session_index - last_benchmark_session_index
            > resolved_config.max_stale_sessions
        ):
            raise BacktestInputError("benchmark price exceeds max_stale_sessions")

        signal = execution_by_date.get(trading_date)
        if signal is not None:
            (
                cash,
                new_trades,
                new_skips,
                execution_cost,
                trade_sequence,
                execution_turnover,
            ) = _execute_rebalance(
                signal=signal,
                execution_date=trading_date,
                exact_bars=exact_bars,
                latest_prices=latest_prices,
                cash=cash,
                positions=positions,
                config=resolved_config,
                trade_sequence=trade_sequence,
            )
            trades.extend(new_trades)
            skips.extend(new_skips)
            cumulative_cost = _money(cumulative_cost + execution_cost)
            cumulative_turnover += execution_turnover

        net_nav, market_value = _mark_to_market(cash, positions, latest_prices)
        net_nav = _money(net_nav)
        market_value = _money(market_value)
        nav.append(
            NavPoint(
                trading_date=trading_date,
                net_nav=net_nav,
                cost_addback_nav=_money(net_nav + cumulative_cost),
                cash=_money(cash),
                market_value=market_value,
                cumulative_cost=cumulative_cost,
                benchmark_close=latest_benchmark,
            )
        )

    attribution = build_attribution(
        nav,
        initial_nav=resolved_config.initial_cash,
        initial_benchmark_close=initial_benchmark_close,
    )
    metrics = _performance_metrics(
        nav,
        initial_cash=resolved_config.initial_cash,
        annualization_sessions=resolved_config.annualization_sessions,
        turnover=cumulative_turnover,
        initial_benchmark_close=initial_benchmark_close,
    )
    return BacktestResult(
        nav=tuple(nav),
        trades=tuple(trades),
        skips=tuple(skips),
        metrics=metrics,
        attribution=attribution,
        ending_positions=dict(sorted(positions.items())),
    )


__all__ = [
    "BACKTEST_ENGINE_VERSION",
    "BacktestConfig",
    "BacktestInputError",
    "BacktestResult",
    "BenchmarkClose",
    "CostModel",
    "DailyClose",
    "FrozenSignal",
    "NavPoint",
    "PerformanceMetrics",
    "TradeRecord",
    "TradeSkip",
    "run_backtest",
]
