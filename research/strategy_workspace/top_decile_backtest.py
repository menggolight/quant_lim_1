"""Cost-aware historical ledger for the frozen Ridge Top-Decile research leg.

This is deliberately *not* the CNY 10,000 Top-2 execution backtest.  The
cross-sectional Top-Decile leg uses CNY 1,000,000 so that per-order minimum
commission can be measured without turning an academic decile portfolio into
an accidental small-account lot-selection test.  It remains historical
research only and exposes no Paper or LIVE transition.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP, localcontext
from enum import Enum
import math
import re
from typing import Any, Mapping, Sequence

from .contracts import canonical_sha256
from .evaluation import (
    RETURN_BASIS,
    EvaluationResult,
    OOSPrediction,
    TopDecileResult,
)


TOP_DECILE_LEDGER_VERSION = "strategy-workspace-top-decile-cost-ledger.v1"
PRIMARY_MODEL = "ridge_alpha_1"
FORMAL_SPLIT = "locked_test"
HORIZON_SESSIONS = 20
RESEARCH_CAPITAL = Decimal("1000000")
RESEARCH_SCOPE = "historical_research_only_not_paper_not_live"

COMMISSION_RATE = Decimal("0.00018")
MINIMUM_COMMISSION = Decimal("5")
SELL_TAX_RATE = Decimal("0.0005")
TRANSFER_FEE_RATE = Decimal("0.00001")
BASE_SLIPPAGE_BPS = Decimal("10")
STRESS_SLIPPAGE_BPS = Decimal("20")
BASE_COMMISSION_MULTIPLIER = Decimal("1")
STRESS_COMMISSION_MULTIPLIER = Decimal("2")

ZERO = Decimal("0")
ONE = Decimal("1")
MONEY = Decimal("0.0001")
_QUANTITY_EPSILON = Decimal("1e-18")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EXPECTED_HALF_YEARS = frozenset({"2024-H1", "2024-H2", "2025-H1", "2025-H2"})


class TopDecileBacktestError(ValueError):
    """Raised when the formal cost ledger cannot be computed without guessing."""


def _decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise TopDecileBacktestError(f"{field_name} must be finite")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:  # pragma: no cover - defensive boundary
        raise TopDecileBacktestError(f"{field_name} must be decimal-compatible") from exc
    if not result.is_finite():
        raise TopDecileBacktestError(f"{field_name} must be finite")
    return result


def _aware(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TopDecileBacktestError(f"{field_name} must be timezone-aware")
    return value


def _plain_date(value: Any, field_name: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TopDecileBacktestError(f"{field_name} must be a date")
    return value


def _source_hash(value: Any, field_name: str) -> str:
    result = str(value or "").strip().lower()
    if _SHA256_RE.fullmatch(result) is None:
        raise TopDecileBacktestError(f"{field_name} must be a SHA-256 digest")
    return result


def _hashable(value: Any) -> Any:
    """Losslessly expose slots dataclasses and mapping proxies to canonical JSON."""

    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _hashable(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _hashable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_hashable(item) for item in value]
    return value


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class ResearchPriceBar:
    """One controlled qfq stock bar used by the historical research ledger."""

    instrument_id: str
    trading_date: date
    open_price: Decimal
    close_price: Decimal
    available_at: datetime
    source_sha256: str
    adjustment: str = "qfq"

    def __post_init__(self) -> None:
        instrument_id = str(self.instrument_id).strip().upper()
        trading_date = _plain_date(self.trading_date, "trading_date")
        open_price = _decimal(self.open_price, "open_price")
        close_price = _decimal(self.close_price, "close_price")
        available_at = _aware(self.available_at, "available_at")
        if not instrument_id:
            raise TopDecileBacktestError("instrument_id is required")
        if open_price <= ZERO or close_price <= ZERO:
            raise TopDecileBacktestError("stock prices must be positive")
        if available_at.date() < trading_date:
            raise TopDecileBacktestError("stock bar cannot be available before it is observed")
        if self.adjustment != "qfq":
            raise TopDecileBacktestError("research price adjustment is frozen to qfq")
        object.__setattr__(self, "instrument_id", instrument_id)
        object.__setattr__(self, "trading_date", trading_date)
        object.__setattr__(self, "open_price", open_price)
        object.__setattr__(self, "close_price", close_price)
        object.__setattr__(self, "available_at", available_at)
        object.__setattr__(self, "source_sha256", _source_hash(self.source_sha256, "source_sha256"))


@dataclass(frozen=True, slots=True)
class BenchmarkTotalReturnBar:
    """One controlled CSI 800 total-return index bar.

    The concrete subject id is intentionally not guessed here.  All bars must
    carry the same upstream-returned id and their 20-session close return must
    reconcile to the benchmark outcomes already bound into ``EvaluationResult``.
    """

    benchmark_id: str
    trading_date: date
    open_level: Decimal
    close_level: Decimal
    available_at: datetime
    source_sha256: str

    def __post_init__(self) -> None:
        benchmark_id = str(self.benchmark_id).strip()
        trading_date = _plain_date(self.trading_date, "trading_date")
        open_level = _decimal(self.open_level, "open_level")
        close_level = _decimal(self.close_level, "close_level")
        available_at = _aware(self.available_at, "available_at")
        if not benchmark_id:
            raise TopDecileBacktestError("benchmark_id is required")
        if open_level <= ZERO or close_level <= ZERO:
            raise TopDecileBacktestError("benchmark total-return levels must be positive")
        if available_at.date() < trading_date:
            raise TopDecileBacktestError("benchmark bar cannot be available before it is observed")
        object.__setattr__(self, "benchmark_id", benchmark_id)
        object.__setattr__(self, "trading_date", trading_date)
        object.__setattr__(self, "open_level", open_level)
        object.__setattr__(self, "close_level", close_level)
        object.__setattr__(self, "available_at", available_at)
        object.__setattr__(self, "source_sha256", _source_hash(self.source_sha256, "source_sha256"))


@dataclass(frozen=True, slots=True)
class TopDecileTrade:
    decision_date: date
    execution_date: date
    instrument_id: str
    side: str
    quantity: Decimal
    reference_open: Decimal
    fill_price: Decimal
    reference_notional: Decimal
    fill_notional: Decimal
    commission: Decimal
    sell_tax: Decimal
    transfer_fee: Decimal
    slippage_cost: Decimal


@dataclass(frozen=True, slots=True)
class TopDecileNavPoint:
    trading_date: date
    nav: Decimal
    drawdown: Decimal


@dataclass(frozen=True, slots=True)
class DecisionWindowResult:
    decision_date: date
    execution_date: date
    exit_date: date
    selected_instrument_ids: tuple[str, ...]
    start_nav: Decimal
    end_nav: Decimal
    net_absolute_return: Decimal
    benchmark_total_return: Decimal
    net_active_return: Decimal


@dataclass(frozen=True, slots=True)
class HalfYearActiveWindow:
    half_year: str
    decision_count: int
    net_absolute_return: Decimal
    benchmark_total_return: Decimal
    net_active_return: Decimal


@dataclass(frozen=True, slots=True)
class TopDecileScenarioResult:
    scenario: str
    start_date: date
    end_date: date
    decision_dates: tuple[date, ...]
    net_absolute_return: Decimal
    benchmark_total_return: Decimal
    net_active_return: Decimal
    half_year_windows: tuple[HalfYearActiveWindow, ...]
    max_drawdown: Decimal
    annualized_one_way_turnover: Decimal
    total_transaction_cost: Decimal
    trades: tuple[TopDecileTrade, ...]
    nav: tuple[TopDecileNavPoint, ...]
    decision_windows: tuple[DecisionWindowResult, ...]
    scenario_sha256: str


@dataclass(frozen=True, slots=True)
class TopDecileGateResult:
    gate_id: str
    passed: bool
    observed: str
    limit: str

    def __post_init__(self) -> None:
        if not str(self.gate_id).strip():
            raise TopDecileBacktestError("gate_id is required")
        if type(self.passed) is not bool:
            raise TopDecileBacktestError("gate result must be computed as boolean")


@dataclass(frozen=True, slots=True)
class TopDecileCostLedgerResult:
    schema_version: str
    research_scope: str
    research_capital: Decimal
    model: str
    split: str
    benchmark_id: str
    decision_dates: tuple[date, ...]
    base: TopDecileScenarioResult
    stress: TopDecileScenarioResult
    gate_results: tuple[TopDecileGateResult, ...]
    configuration_sha256: str
    evaluation_sha256: str
    trading_calendar_sha256: str
    price_data_sha256: str
    benchmark_data_sha256: str
    input_bundle_sha256: str
    result_sha256: str


@dataclass(frozen=True, slots=True)
class _Costs:
    scenario: str
    slippage_bps: Decimal
    commission_multiplier: Decimal

    @property
    def slippage_rate(self) -> Decimal:
        return self.slippage_bps / Decimal("10000")


_BASE_COSTS = _Costs("base", BASE_SLIPPAGE_BPS, BASE_COMMISSION_MULTIPLIER)
_STRESS_COSTS = _Costs("stress", STRESS_SLIPPAGE_BPS, STRESS_COMMISSION_MULTIPLIER)


def _configuration_payload() -> dict[str, Any]:
    return {
        "schema_version": TOP_DECILE_LEDGER_VERSION,
        "research_scope": RESEARCH_SCOPE,
        "research_capital": RESEARCH_CAPITAL,
        "model": PRIMARY_MODEL,
        "split": FORMAL_SPLIT,
        "horizon_sessions": HORIZON_SESSIONS,
        "portfolio": "equal_weight_long_only_fractional_research_units",
        "execution": "decision_close_then_next_controlled_session_open",
        "commission_rate": COMMISSION_RATE,
        "minimum_commission_per_order": MINIMUM_COMMISSION,
        "sell_tax_rate": SELL_TAX_RATE,
        "transfer_fee_rate_both_sides": TRANSFER_FEE_RATE,
        "base_slippage_bps_one_way": BASE_SLIPPAGE_BPS,
        "stress_slippage_bps_one_way": STRESS_SLIPPAGE_BPS,
        "stress_commission_multiplier": STRESS_COMMISSION_MULTIPLIER,
        "paper_supported": False,
        "live_supported": False,
    }


def _validate_calendar(trading_calendar: Sequence[date]) -> tuple[tuple[date, ...], dict[date, int]]:
    calendar = tuple(trading_calendar)
    if not calendar or any(not isinstance(item, date) or isinstance(item, datetime) for item in calendar):
        raise TopDecileBacktestError("trading_calendar must contain dates")
    if tuple(sorted(set(calendar))) != calendar:
        raise TopDecileBacktestError("trading_calendar must be unique and strictly increasing")
    return calendar, {day: index for index, day in enumerate(calendar)}


def _validate_selection(
    evaluation: EvaluationResult,
    calendar: tuple[date, ...],
    calendar_index: Mapping[date, int],
    as_of: datetime,
) -> tuple[TopDecileResult, ...]:
    if not isinstance(evaluation, EvaluationResult):
        raise TopDecileBacktestError("evaluation must be an EvaluationResult")
    if evaluation.ridge_alpha != 1.0 or evaluation.historical_gate.primary_model != PRIMARY_MODEL:
        raise TopDecileBacktestError("formal model must remain Ridge alpha=1")
    prepared_dates = tuple(
        item.decision_at.date()
        for item in evaluation.prepared_panel.cross_sections
        if item.split == FORMAL_SPLIT
    )
    if not prepared_dates:
        raise TopDecileBacktestError("locked-test decisions are missing")
    if len(prepared_dates) != len(set(prepared_dates)):
        raise TopDecileBacktestError("duplicate prepared locked-test decision")
    rows = tuple(
        sorted(
            (
                item
                for item in evaluation.top_decile
                if item.split == FORMAL_SPLIT and item.model == PRIMARY_MODEL
            ),
            key=lambda item: item.decision_date,
        )
    )
    row_dates = tuple(item.decision_date for item in rows)
    if row_dates != prepared_dates:
        raise TopDecileBacktestError("one Top-Decile result is required for every prepared decision")
    indices: list[int] = []
    for decision_date in row_dates:
        try:
            indices.append(calendar_index[decision_date])
        except KeyError as exc:
            raise TopDecileBacktestError("decision is absent from the controlled calendar") from exc
    if any(current - previous != HORIZON_SESSIONS for previous, current in zip(indices, indices[1:])):
        raise TopDecileBacktestError("decisions must stay exactly 20 controlled sessions apart")
    observed_halves = {
        f"{day.year}-H{1 if day.month <= 6 else 2}" for day in row_dates
    }
    if observed_halves != _EXPECTED_HALF_YEARS:
        raise TopDecileBacktestError("locked test must cover all four frozen half-year windows")

    predictions_by_date: dict[date, list[OOSPrediction]] = {}
    for item in evaluation.predictions:
        if item.split == FORMAL_SPLIT and item.model == PRIMARY_MODEL:
            predictions_by_date.setdefault(item.decision_date, []).append(item)
    if set(predictions_by_date) != set(row_dates):
        raise TopDecileBacktestError("prediction decisions do not align with Top-Decile decisions")
    for result in rows:
        prediction_rows = predictions_by_date[result.decision_date]
        prepared_row = next(
            item
            for item in evaluation.prepared_panel.cross_sections
            if item.split == FORMAL_SPLIT
            and item.decision_at.date() == result.decision_date
        )
        prepared_ids = {item.instrument_id for item in prepared_row.observations}
        prediction_ids = {item.instrument_id for item in prediction_rows}
        if prediction_ids != prepared_ids or len(prediction_rows) != len(prepared_ids):
            raise TopDecileBacktestError(
                "Ridge predictions must exactly cover every prepared PIT member"
            )
        if len(prediction_rows) < 3:
            raise TopDecileBacktestError("Top-Decile selection requires at least three predictions")
        for item in prediction_rows:
            if (
                item.actual_forward_total_return_20d is None
                or item.benchmark_total_return_20d is None
                or item.actual_forward_excess_return_20d is None
                or item.outcome_available_at is None
            ):
                raise TopDecileBacktestError("selected decision has an unavailable forward outcome")
            outcome_at = _aware(item.outcome_available_at, "outcome_available_at")
            if outcome_at > as_of:
                raise TopDecileBacktestError("forward outcome is not yet available at ledger as_of")
            decision_index = calendar_index[result.decision_date]
            if decision_index + HORIZON_SESSIONS + 1 >= len(calendar):
                raise TopDecileBacktestError("controlled calendar does not contain the outcome horizon")
            expected_start = calendar[decision_index + 1]
            expected_end = calendar[decision_index + HORIZON_SESSIONS + 1]
            if (
                item.label_start_date != expected_start
                or item.label_end_date != expected_end
                or item.return_basis != RETURN_BASIS
            ):
                raise TopDecileBacktestError(
                    "prediction label must use next-session open to open over 20 sessions"
                )
            if (
                prepared_row.label_start_date != expected_start
                or prepared_row.label_end_date != expected_end
                or prepared_row.return_basis != RETURN_BASIS
            ):
                raise TopDecileBacktestError("prepared label contract is not executable")
            if outcome_at.date() < item.label_end_date:
                raise TopDecileBacktestError("forward outcome availability precedes its label")

        selected_count = max(1, math.ceil(len(prediction_rows) * 0.10))
        order = sorted(range(len(prediction_rows)), key=lambda index: prediction_rows[index].prediction)
        expected_rows = [prediction_rows[index] for index in order[-selected_count:]]
        expected_ids = tuple(sorted(item.instrument_id for item in expected_rows))
        if result.selected_count != selected_count or result.selected_instrument_ids != expected_ids:
            raise TopDecileBacktestError("Top-Decile ids do not match the frozen Ridge ranking")
        if set(result.selected_weights) != set(expected_ids):
            raise TopDecileBacktestError("Top-Decile weights do not match selected ids")
        expected_weight = 1.0 / selected_count
        if any(
            not math.isclose(float(weight), expected_weight, rel_tol=0.0, abs_tol=1.0e-12)
            for weight in result.selected_weights.values()
        ):
            raise TopDecileBacktestError("Top-Decile portfolio must be equal weighted")
        expected_total = sum(float(item.actual_forward_total_return_20d) for item in expected_rows) / selected_count
        expected_active = sum(float(item.actual_forward_excess_return_20d) for item in expected_rows) / selected_count
        if not math.isclose(result.gross_absolute_return, expected_total, rel_tol=0.0, abs_tol=1.0e-12):
            raise TopDecileBacktestError("Top-Decile gross absolute return does not reconcile")
        if not math.isclose(result.gross_active_return, expected_active, rel_tol=0.0, abs_tol=1.0e-12):
            raise TopDecileBacktestError("Top-Decile gross active return does not reconcile")
    return rows


def _index_price_bars(
    bars: Sequence[ResearchPriceBar], as_of: datetime
) -> tuple[dict[tuple[str, date], ResearchPriceBar], tuple[ResearchPriceBar, ...]]:
    materialized = tuple(bars)
    if not materialized or any(not isinstance(item, ResearchPriceBar) for item in materialized):
        raise TopDecileBacktestError("price_bars must contain ResearchPriceBar values")
    index: dict[tuple[str, date], ResearchPriceBar] = {}
    for item in materialized:
        key = (item.instrument_id, item.trading_date)
        if key in index:
            raise TopDecileBacktestError("duplicate stock price bar")
        if item.available_at > as_of:
            raise TopDecileBacktestError("stock price bar is not available at ledger as_of")
        index[key] = item
    return index, tuple(sorted(materialized, key=lambda item: (item.trading_date, item.instrument_id)))


def _index_benchmark_bars(
    bars: Sequence[BenchmarkTotalReturnBar], as_of: datetime
) -> tuple[str, dict[date, BenchmarkTotalReturnBar], tuple[BenchmarkTotalReturnBar, ...]]:
    materialized = tuple(bars)
    if not materialized or any(not isinstance(item, BenchmarkTotalReturnBar) for item in materialized):
        raise TopDecileBacktestError(
            "benchmark_bars must contain BenchmarkTotalReturnBar values"
        )
    benchmark_ids = {item.benchmark_id for item in materialized}
    if len(benchmark_ids) != 1:
        raise TopDecileBacktestError("benchmark bars must use one returned subject id")
    index: dict[date, BenchmarkTotalReturnBar] = {}
    for item in materialized:
        if item.trading_date in index:
            raise TopDecileBacktestError("duplicate benchmark total-return bar")
        if item.available_at > as_of:
            raise TopDecileBacktestError("benchmark bar is not available at ledger as_of")
        index[item.trading_date] = item
    return next(iter(benchmark_ids)), index, tuple(sorted(materialized, key=lambda item: item.trading_date))


def _validate_market_coverage(
    selections: tuple[TopDecileResult, ...],
    evaluation: EvaluationResult,
    calendar: tuple[date, ...],
    calendar_index: Mapping[date, int],
    price_index: Mapping[tuple[str, date], ResearchPriceBar],
    benchmark_index: Mapping[date, BenchmarkTotalReturnBar],
) -> tuple[tuple[date, date], ...]:
    periods: list[tuple[date, date]] = []
    first_decision_index = calendar_index[selections[0].decision_date]
    last_decision_index = calendar_index[selections[-1].decision_date]
    if last_decision_index + HORIZON_SESSIONS + 1 >= len(calendar):
        raise TopDecileBacktestError("calendar is missing the final next-session exit")
    for result in selections:
        decision_index = calendar_index[result.decision_date]
        execution_date = calendar[decision_index + 1]
        exit_date = calendar[decision_index + HORIZON_SESSIONS + 1]
        periods.append((execution_date, exit_date))
        for instrument_id in result.selected_instrument_ids:
            for trading_date in calendar[decision_index : decision_index + HORIZON_SESSIONS + 2]:
                if (instrument_id, trading_date) not in price_index:
                    raise TopDecileBacktestError(
                        f"missing selected-stock price: {instrument_id} {trading_date}"
                    )
    for trading_date in calendar[first_decision_index : last_decision_index + HORIZON_SESSIONS + 2]:
        if trading_date not in benchmark_index:
            raise TopDecileBacktestError(f"missing benchmark total-return bar: {trading_date}")

    predictions_by_date: dict[date, list[OOSPrediction]] = {}
    for item in evaluation.predictions:
        if item.split == FORMAL_SPLIT and item.model == PRIMARY_MODEL:
            predictions_by_date.setdefault(item.decision_date, []).append(item)
    for result, (execution_date, exit_date) in zip(selections, periods, strict=True):
        decision_predictions = predictions_by_date[result.decision_date]
        benchmark_values = {
            float(item.benchmark_total_return_20d) for item in decision_predictions
        }
        if len(benchmark_values) != 1:
            raise TopDecileBacktestError("evaluation decision has inconsistent benchmark returns")
        observed = benchmark_index[exit_date].open_level / benchmark_index[execution_date].open_level - ONE
        expected = next(iter(benchmark_values))
        if not math.isclose(float(observed), expected, rel_tol=0.0, abs_tol=1.0e-10):
            raise TopDecileBacktestError(
                "benchmark total-return bars do not reconcile to EvaluationResult"
            )
        predictions_by_instrument = {item.instrument_id: item for item in decision_predictions}
        for instrument_id in result.selected_instrument_ids:
            expected_stock = float(
                predictions_by_instrument[instrument_id].actual_forward_total_return_20d
            )
            observed_stock = (
                price_index[(instrument_id, exit_date)].open_price
                / price_index[(instrument_id, execution_date)].open_price
                - ONE
            )
            if not math.isclose(
                float(observed_stock), expected_stock, rel_tol=0.0, abs_tol=1.0e-10
            ):
                raise TopDecileBacktestError(
                    "selected-stock qfq bars do not reconcile to EvaluationResult"
                )
    return tuple(periods)


def _fill_price(reference: Decimal, side: str, costs: _Costs) -> Decimal:
    if side == "BUY":
        return reference * (ONE + costs.slippage_rate)
    if side == "SELL":
        return reference * (ONE - costs.slippage_rate)
    raise TopDecileBacktestError(f"unsupported side: {side}")


def _fees(fill_notional: Decimal, side: str, costs: _Costs) -> tuple[Decimal, Decimal, Decimal]:
    commission = max(fill_notional * COMMISSION_RATE, MINIMUM_COMMISSION)
    commission = _money(commission * costs.commission_multiplier)
    sell_tax = _money(fill_notional * SELL_TAX_RATE) if side == "SELL" else ZERO
    transfer = _money(fill_notional * TRANSFER_FEE_RATE)
    return commission, sell_tax, transfer


def _cash_after_targets(
    cash: Decimal,
    positions: Mapping[str, Decimal],
    targets: Mapping[str, Decimal],
    open_prices: Mapping[str, Decimal],
    costs: _Costs,
) -> Decimal:
    result = cash
    for instrument_id in sorted(set(positions) | set(targets)):
        delta = targets.get(instrument_id, ZERO) - positions.get(instrument_id, ZERO)
        if abs(delta) <= _QUANTITY_EPSILON:
            continue
        side = "BUY" if delta > ZERO else "SELL"
        quantity = abs(delta)
        fill = _fill_price(open_prices[instrument_id], side, costs)
        fill_notional = quantity * fill
        commission, sell_tax, transfer = _fees(fill_notional, side, costs)
        if side == "BUY":
            result -= fill_notional + commission + sell_tax + transfer
        else:
            result += fill_notional - commission - sell_tax - transfer
    return result


def _equal_weight_targets(
    cash: Decimal,
    positions: Mapping[str, Decimal],
    selected_ids: tuple[str, ...],
    open_prices: Mapping[str, Decimal],
    costs: _Costs,
) -> dict[str, Decimal]:
    pre_nav = cash + sum(
        quantity * open_prices[instrument_id]
        for instrument_id, quantity in positions.items()
    )
    if pre_nav <= ZERO:
        raise TopDecileBacktestError("portfolio NAV must remain positive")
    low = ZERO
    high = pre_nav
    for _ in range(160):
        gross = (low + high) / Decimal("2")
        per_name = gross / Decimal(len(selected_ids))
        targets = {
            instrument_id: per_name / open_prices[instrument_id]
            for instrument_id in selected_ids
        }
        if _cash_after_targets(cash, positions, targets, open_prices, costs) >= ZERO:
            low = gross
        else:
            high = gross
    per_name = low / Decimal(len(selected_ids))
    return {
        instrument_id: per_name / open_prices[instrument_id]
        for instrument_id in selected_ids
    }


def _execute_targets(
    *,
    decision_date: date,
    execution_date: date,
    cash: Decimal,
    positions: dict[str, Decimal],
    targets: Mapping[str, Decimal],
    open_prices: Mapping[str, Decimal],
    costs: _Costs,
) -> tuple[Decimal, tuple[TopDecileTrade, ...]]:
    trades: list[TopDecileTrade] = []
    deltas = {
        instrument_id: targets.get(instrument_id, ZERO) - positions.get(instrument_id, ZERO)
        for instrument_id in sorted(set(positions) | set(targets))
    }
    # Sells precede buys so an otherwise solvent target never relies on
    # negative intraday cash.
    ordered = sorted(deltas, key=lambda instrument_id: (deltas[instrument_id] > ZERO, instrument_id))
    for instrument_id in ordered:
        delta = deltas[instrument_id]
        if abs(delta) <= _QUANTITY_EPSILON:
            continue
        side = "BUY" if delta > ZERO else "SELL"
        quantity = abs(delta)
        reference = open_prices[instrument_id]
        fill = _fill_price(reference, side, costs)
        reference_notional = quantity * reference
        fill_notional = quantity * fill
        commission, sell_tax, transfer = _fees(fill_notional, side, costs)
        if side == "BUY":
            cash -= fill_notional + commission + sell_tax + transfer
        else:
            cash += fill_notional - commission - sell_tax - transfer
        new_quantity = positions.get(instrument_id, ZERO) + delta
        if abs(new_quantity) <= _QUANTITY_EPSILON:
            positions.pop(instrument_id, None)
        else:
            if new_quantity < ZERO:
                raise TopDecileBacktestError("short position is forbidden")
            positions[instrument_id] = new_quantity
        trades.append(
            TopDecileTrade(
                decision_date=decision_date,
                execution_date=execution_date,
                instrument_id=instrument_id,
                side=side,
                quantity=quantity,
                reference_open=reference,
                fill_price=fill,
                reference_notional=reference_notional,
                fill_notional=fill_notional,
                commission=commission,
                sell_tax=sell_tax,
                transfer_fee=transfer,
                slippage_cost=abs(fill - reference) * quantity,
            )
        )
    if cash < Decimal("-0.0001"):
        raise TopDecileBacktestError("rebalance would create leverage")
    if cash < ZERO:
        cash = ZERO
    return cash, tuple(trades)


def _chain(values: Sequence[Decimal]) -> Decimal:
    result = ONE
    for value in values:
        result *= ONE + value
    return result - ONE


def _half_year_windows(
    windows: Sequence[DecisionWindowResult],
) -> tuple[HalfYearActiveWindow, ...]:
    grouped: dict[str, list[DecisionWindowResult]] = {}
    for item in windows:
        label = f"{item.decision_date.year}-H{1 if item.decision_date.month <= 6 else 2}"
        grouped.setdefault(label, []).append(item)
    if set(grouped) != _EXPECTED_HALF_YEARS:
        raise TopDecileBacktestError("scenario lost a frozen half-year window")
    results: list[HalfYearActiveWindow] = []
    for label in sorted(grouped):
        rows = grouped[label]
        absolute = _chain([item.net_absolute_return for item in rows])
        benchmark = _chain([item.benchmark_total_return for item in rows])
        results.append(
            HalfYearActiveWindow(
                half_year=label,
                decision_count=len(rows),
                net_absolute_return=absolute,
                benchmark_total_return=benchmark,
                net_active_return=absolute - benchmark,
            )
        )
    return tuple(results)


def _run_scenario(
    *,
    costs: _Costs,
    selections: tuple[TopDecileResult, ...],
    periods: tuple[tuple[date, date], ...],
    calendar: tuple[date, ...],
    calendar_index: Mapping[date, int],
    price_index: Mapping[tuple[str, date], ResearchPriceBar],
    benchmark_index: Mapping[date, BenchmarkTotalReturnBar],
) -> TopDecileScenarioResult:
    cash = RESEARCH_CAPITAL
    positions: dict[str, Decimal] = {}
    trades: list[TopDecileTrade] = []
    nav_points: list[TopDecileNavPoint] = []
    decision_windows: list[DecisionWindowResult] = []
    peak = RESEARCH_CAPITAL
    max_drawdown = ZERO

    for period_index, (selection, period) in enumerate(zip(selections, periods, strict=True)):
        execution_date, exit_date = period
        union_ids = set(positions) | set(selection.selected_instrument_ids)
        open_prices = {
            instrument_id: price_index[(instrument_id, execution_date)].open_price
            for instrument_id in union_ids
        }
        start_nav = cash + sum(
            quantity * open_prices[instrument_id]
            for instrument_id, quantity in positions.items()
        )
        targets = _equal_weight_targets(
            cash,
            positions,
            selection.selected_instrument_ids,
            open_prices,
            costs,
        )
        cash, new_trades = _execute_targets(
            decision_date=selection.decision_date,
            execution_date=execution_date,
            cash=cash,
            positions=positions,
            targets=targets,
            open_prices=open_prices,
            costs=costs,
        )
        trades.extend(new_trades)

        execution_index = calendar_index[execution_date]
        exit_index = calendar_index[exit_date]
        for trading_date in calendar[execution_index:exit_index]:
            nav = cash + sum(
                quantity * price_index[(instrument_id, trading_date)].close_price
                for instrument_id, quantity in positions.items()
            )
            if nav <= ZERO:
                raise TopDecileBacktestError("portfolio NAV became non-positive")
            peak = max(peak, nav)
            drawdown = (peak - nav) / peak
            max_drawdown = max(max_drawdown, drawdown)
            nav_points.append(TopDecileNavPoint(trading_date, nav, drawdown))

        exit_open_prices = {
            instrument_id: price_index[(instrument_id, exit_date)].open_price
            for instrument_id in positions
        }
        pre_exit_nav = cash + sum(
            quantity * exit_open_prices[instrument_id]
            for instrument_id, quantity in positions.items()
        )
        if period_index == len(selections) - 1:
            cash, final_trades = _execute_targets(
                decision_date=selection.decision_date,
                execution_date=exit_date,
                cash=cash,
                positions=positions,
                targets={},
                open_prices=exit_open_prices,
                costs=costs,
            )
            trades.extend(final_trades)
            if positions:
                raise TopDecileBacktestError("final research liquidation is incomplete")
            end_nav = cash
            peak = max(peak, end_nav)
            drawdown = (peak - end_nav) / peak
            max_drawdown = max(max_drawdown, drawdown)
            nav_points.append(TopDecileNavPoint(exit_date, end_nav, drawdown))
        else:
            end_nav = pre_exit_nav

        benchmark_return = (
            benchmark_index[exit_date].open_level
            / benchmark_index[execution_date].open_level
            - ONE
        )
        net_return = end_nav / start_nav - ONE
        decision_windows.append(
            DecisionWindowResult(
                decision_date=selection.decision_date,
                execution_date=execution_date,
                exit_date=exit_date,
                selected_instrument_ids=selection.selected_instrument_ids,
                start_nav=start_nav,
                end_nav=end_nav,
                net_absolute_return=net_return,
                benchmark_total_return=benchmark_return,
                net_active_return=net_return - benchmark_return,
            )
        )

    final_nav = cash
    net_absolute_return = final_nav / RESEARCH_CAPITAL - ONE
    first_execution = periods[0][0]
    final_exit = periods[-1][1]
    benchmark_return = (
        benchmark_index[final_exit].open_level
        / benchmark_index[first_execution].open_level
        - ONE
    )
    average_nav = sum((item.nav for item in nav_points), ZERO) / Decimal(len(nav_points))
    elapsed_sessions = Decimal(calendar_index[final_exit] - calendar_index[first_execution])
    total_reference_notional = sum((item.reference_notional for item in trades), ZERO)
    annualized_turnover = (
        Decimal("0.5")
        * total_reference_notional
        / average_nav
        * Decimal("252")
        / elapsed_sessions
    )
    total_cost = sum(
        (
            item.commission
            + item.sell_tax
            + item.transfer_fee
            + item.slippage_cost
            for item in trades
        ),
        ZERO,
    )
    half_year = _half_year_windows(decision_windows)
    payload = {
        "scenario": costs.scenario,
        "start_date": first_execution,
        "end_date": final_exit,
        "decision_dates": [item.decision_date for item in selections],
        "net_absolute_return": net_absolute_return,
        "benchmark_total_return": benchmark_return,
        "net_active_return": net_absolute_return - benchmark_return,
        "half_year_windows": half_year,
        "max_drawdown": max_drawdown,
        "annualized_one_way_turnover": annualized_turnover,
        "total_transaction_cost": total_cost,
        "trades": trades,
        "nav": nav_points,
        "decision_windows": decision_windows,
    }
    scenario_hash = canonical_sha256(_hashable(payload))
    return TopDecileScenarioResult(
        scenario=costs.scenario,
        start_date=first_execution,
        end_date=final_exit,
        decision_dates=tuple(item.decision_date for item in selections),
        net_absolute_return=net_absolute_return,
        benchmark_total_return=benchmark_return,
        net_active_return=net_absolute_return - benchmark_return,
        half_year_windows=half_year,
        max_drawdown=max_drawdown,
        annualized_one_way_turnover=annualized_turnover,
        total_transaction_cost=total_cost,
        trades=tuple(trades),
        nav=tuple(nav_points),
        decision_windows=tuple(decision_windows),
        scenario_sha256=scenario_hash,
    )


def _gates(
    base: TopDecileScenarioResult, stress: TopDecileScenarioResult
) -> tuple[TopDecileGateResult, ...]:
    positive_windows = sum(item.net_active_return > ZERO for item in base.half_year_windows)
    worst_drawdown = max(base.max_drawdown, stress.max_drawdown)
    worst_turnover = max(
        base.annualized_one_way_turnover,
        stress.annualized_one_way_turnover,
    )
    return (
        TopDecileGateResult(
            "top_decile_net_absolute_positive",
            base.net_absolute_return > ZERO,
            str(base.net_absolute_return),
            ">0",
        ),
        TopDecileGateResult(
            "top_decile_net_active_positive",
            base.net_active_return > ZERO,
            str(base.net_active_return),
            ">0",
        ),
        TopDecileGateResult(
            "positive_semiannual_windows_gte_3_of_4",
            len(base.half_year_windows) == 4 and positive_windows >= 3,
            f"{positive_windows}/{len(base.half_year_windows)}",
            ">=3/4",
        ),
        TopDecileGateResult(
            "stress_active_return_non_negative",
            stress.net_active_return >= ZERO,
            str(stress.net_active_return),
            ">=0",
        ),
        TopDecileGateResult(
            "max_drawdown_lte_12pct",
            worst_drawdown <= Decimal("0.12"),
            str(worst_drawdown),
            "<=0.12",
        ),
        TopDecileGateResult(
            "annualized_one_way_turnover_lte_4",
            worst_turnover <= Decimal("4"),
            str(worst_turnover),
            "<=4",
        ),
    )


def run_top_decile_cost_ledger(
    evaluation: EvaluationResult,
    *,
    trading_calendar: Sequence[date],
    price_bars: Sequence[ResearchPriceBar],
    benchmark_bars: Sequence[BenchmarkTotalReturnBar],
    as_of: datetime,
) -> TopDecileCostLedgerResult:
    """Run the locked-test Ridge Top-Decile ledger under base and stress costs.

    Every reported metric is derived from the typed evaluation, supplied
    market series and transaction calculations. Missing or misaligned evidence
    raises ``TopDecileBacktestError``; no successful subset is silently
    retained. The current price-bar input is not yet source-authenticated by a
    controlled Choice adapter, so Stage A separately keeps its data gate red.
    """

    ledger_as_of = _aware(as_of, "as_of")
    calendar, calendar_index = _validate_calendar(trading_calendar)
    selections = _validate_selection(evaluation, calendar, calendar_index, ledger_as_of)
    price_index, normalized_prices = _index_price_bars(price_bars, ledger_as_of)
    benchmark_id, benchmark_index, normalized_benchmark = _index_benchmark_bars(
        benchmark_bars, ledger_as_of
    )
    periods = _validate_market_coverage(
        selections,
        evaluation,
        calendar,
        calendar_index,
        price_index,
        benchmark_index,
    )

    with localcontext() as context:
        context.prec = 42
        base = _run_scenario(
            costs=_BASE_COSTS,
            selections=selections,
            periods=periods,
            calendar=calendar,
            calendar_index=calendar_index,
            price_index=price_index,
            benchmark_index=benchmark_index,
        )
        stress = _run_scenario(
            costs=_STRESS_COSTS,
            selections=selections,
            periods=periods,
            calendar=calendar,
            calendar_index=calendar_index,
            price_index=price_index,
            benchmark_index=benchmark_index,
        )

    configuration_hash = canonical_sha256(_configuration_payload())
    evaluation_hash = canonical_sha256(_hashable(evaluation))
    calendar_hash = canonical_sha256(calendar)
    price_hash = canonical_sha256(_hashable(normalized_prices))
    benchmark_hash = canonical_sha256(_hashable(normalized_benchmark))
    input_hash = canonical_sha256(
        {
            "as_of": ledger_as_of,
            "configuration_sha256": configuration_hash,
            "evaluation_sha256": evaluation_hash,
            "trading_calendar_sha256": calendar_hash,
            "price_data_sha256": price_hash,
            "benchmark_data_sha256": benchmark_hash,
        }
    )
    gates = _gates(base, stress)
    result_payload = {
        "schema_version": TOP_DECILE_LEDGER_VERSION,
        "research_scope": RESEARCH_SCOPE,
        "research_capital": RESEARCH_CAPITAL,
        "model": PRIMARY_MODEL,
        "split": FORMAL_SPLIT,
        "benchmark_id": benchmark_id,
        "decision_dates": [item.decision_date for item in selections],
        "base": base,
        "stress": stress,
        "gate_results": gates,
        "configuration_sha256": configuration_hash,
        "evaluation_sha256": evaluation_hash,
        "trading_calendar_sha256": calendar_hash,
        "price_data_sha256": price_hash,
        "benchmark_data_sha256": benchmark_hash,
        "input_bundle_sha256": input_hash,
    }
    result_hash = canonical_sha256(_hashable(result_payload))
    return TopDecileCostLedgerResult(
        schema_version=TOP_DECILE_LEDGER_VERSION,
        research_scope=RESEARCH_SCOPE,
        research_capital=RESEARCH_CAPITAL,
        model=PRIMARY_MODEL,
        split=FORMAL_SPLIT,
        benchmark_id=benchmark_id,
        decision_dates=tuple(item.decision_date for item in selections),
        base=base,
        stress=stress,
        gate_results=gates,
        configuration_sha256=configuration_hash,
        evaluation_sha256=evaluation_hash,
        trading_calendar_sha256=calendar_hash,
        price_data_sha256=price_hash,
        benchmark_data_sha256=benchmark_hash,
        input_bundle_sha256=input_hash,
        result_sha256=result_hash,
    )


__all__ = [
    "BASE_SLIPPAGE_BPS",
    "BenchmarkTotalReturnBar",
    "DecisionWindowResult",
    "HalfYearActiveWindow",
    "RESEARCH_CAPITAL",
    "RESEARCH_SCOPE",
    "ResearchPriceBar",
    "STRESS_SLIPPAGE_BPS",
    "TOP_DECILE_LEDGER_VERSION",
    "TopDecileBacktestError",
    "TopDecileCostLedgerResult",
    "TopDecileGateResult",
    "TopDecileNavPoint",
    "TopDecileScenarioResult",
    "TopDecileTrade",
    "run_top_decile_cost_ledger",
]
