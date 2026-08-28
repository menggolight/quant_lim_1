"""Pure domain contracts for the formal technical-momentum data path.

This module deliberately performs no I/O and has no Provider dependency.  It
keeps the corporate-action-adjusted signal channel separate from the raw
execution channel, validates monthly point-in-time CSI 800 membership, and
builds the fail-closed nine-dataset manifest used before any locked test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .contracts import canonical_sha256


STRATEGY_ID = "a-share-technical-momentum-adaptive-v1"
CSI800_INDEX_CODE = "000906.SH"
STANDARD_CLI_VERIFICATION_BLOCKER = (
    "standard_cli_raw_dataset_verification_not_implemented"
)
MANIFEST_SCHEMA_VERSION = "technical-formal-dataset-manifest.v1"
MANIFEST_COVERAGE_START = date(2018, 1, 1)
MANIFEST_COVERAGE_END = date(2025, 12, 31)
MANIFEST_WARMUP_START = date(2017, 7, 1)
PIT_BOOTSTRAP_LATEST = date(2017, 12, 31)
MIN_INDEX_WEIGHT_DECIMAL_PLACES = 3

TECHNICAL_FORMAL_DATASET_IDS = (
    "trade_calendar",
    "raw_daily_bar",
    "adjustment_factor",
    "csi800_pit_membership",
    "suspension_history",
    "price_limit_history",
    "name_and_st_history",
    "security_master",
    "csi800_price_benchmark",
)

CRITICAL_CHECK_IDS = (
    "date_order_valid",
    "no_duplicate_primary_keys",
    "pit_membership_complete",
    "adjustment_point_in_time_valid",
    "dual_price_isolated",
    "execution_states_complete",
    "corporate_action_entitlements_complete",
)

_WARMUP_DATASET_IDS = frozenset(
    {"raw_daily_bar", "adjustment_factor", "csi800_price_benchmark"}
)
ALLOWED_STANDARD_INTERFACES = {
    "baostock": frozenset(
        {
            "query_trade_dates",
            "query_history_k_data_plus",
            "query_adjust_factor",
            "query_stock_basic",
        }
    ),
    "tushare_standard_non_vip": frozenset(
        {
            "trade_cal",
            "daily",
            "adj_factor",
            "index_weight",
            "suspend_d",
            "stk_limit",
            "namechange",
            "stock_basic",
            "index_daily",
        }
    ),
}
DATASET_STANDARD_INTERFACES = {
    "trade_calendar": frozenset(
        {("baostock", "query_trade_dates"), ("tushare_standard_non_vip", "trade_cal")}
    ),
    "raw_daily_bar": frozenset(
        {
            ("baostock", "query_history_k_data_plus"),
            ("tushare_standard_non_vip", "daily"),
        }
    ),
    "adjustment_factor": frozenset(
        {
            ("baostock", "query_adjust_factor"),
            ("tushare_standard_non_vip", "adj_factor"),
        }
    ),
    "csi800_pit_membership": frozenset(
        {("tushare_standard_non_vip", "index_weight")}
    ),
    "suspension_history": frozenset(
        {("tushare_standard_non_vip", "suspend_d")}
    ),
    "price_limit_history": frozenset(
        {("tushare_standard_non_vip", "stk_limit")}
    ),
    "name_and_st_history": frozenset(
        {("tushare_standard_non_vip", "namechange")}
    ),
    "security_master": frozenset(
        {
            ("baostock", "query_stock_basic"),
            ("tushare_standard_non_vip", "stock_basic"),
        }
    ),
    "csi800_price_benchmark": frozenset(
        {
            ("baostock", "query_history_k_data_plus"),
            ("tushare_standard_non_vip", "index_daily"),
        }
    ),
}

_INSTRUMENT = re.compile(r"^[0-9]{6}\.(?:SH|SZ)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_A_SHARE_SH_PREFIXES = ("600", "601", "603", "605", "688", "689")
_A_SHARE_SZ_PREFIXES = ("000", "001", "002", "003", "300", "301")
_ZERO = Decimal("0")
_ONE = Decimal("1")
_HUNDRED = Decimal("100")


class TechnicalFormalDataError(ValueError):
    """Raised when a formal technical-data domain contract is violated."""


class DualPriceContractError(TechnicalFormalDataError):
    """Raised when signal and execution price channels cannot be constructed."""


class PITUniverseError(TechnicalFormalDataError):
    """Raised when historical CSI 800 membership is incomplete or malformed."""


class DatasetManifestError(TechnicalFormalDataError):
    """Raised when the nine-dataset manifest is inconsistent."""


def _exact_text(value: Any, field_name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise TechnicalFormalDataError(f"{field_name} must be an exact non-empty string")
    return value


def _exact_date(value: Any, field_name: str) -> date:
    # datetime subclasses date; exact type is required at the domain boundary.
    if type(value) is not date:
        raise TechnicalFormalDataError(f"{field_name} must use the exact date type")
    return value


def _exact_datetime(value: Any, field_name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise TechnicalFormalDataError(
            f"{field_name} must use the exact timezone-aware datetime type"
        )
    return value


def _exact_bool(value: Any, field_name: str) -> bool:
    if type(value) is not bool:
        raise TechnicalFormalDataError(f"{field_name} must use the exact bool type")
    return value


def _exact_nonnegative_int(value: Any, field_name: str) -> int:
    if type(value) is not int or value < 0:
        raise TechnicalFormalDataError(f"{field_name} must be a non-negative integer")
    return value


def _decimal(value: Any, field_name: str, *, positive: bool = False) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise TechnicalFormalDataError(f"{field_name} must use an exact finite Decimal")
    if positive and value <= _ZERO:
        raise TechnicalFormalDataError(f"{field_name} must be positive")
    return value


def _instrument(value: Any, field_name: str = "instrument_id") -> str:
    text = _exact_text(value, field_name)
    if _INSTRUMENT.fullmatch(text) is None:
        raise TechnicalFormalDataError(f"{field_name} must be a canonical SH/SZ code")
    return text


def _a_share_instrument(value: Any, field_name: str = "component_id") -> str:
    text = _instrument(value, field_name)
    code, exchange = text.split(".", 1)
    allowed = (
        code.startswith(_A_SHARE_SH_PREFIXES)
        if exchange == "SH"
        else code.startswith(_A_SHARE_SZ_PREFIXES)
    )
    if not allowed:
        raise TechnicalFormalDataError(f"{field_name} is not an admitted A-share stock")
    return text


def _validate_ohlc(
    open_price: Any,
    high_price: Any,
    low_price: Any,
    close_price: Any,
    *,
    prefix: str,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    open_value = _decimal(open_price, f"{prefix}.open", positive=True)
    high_value = _decimal(high_price, f"{prefix}.high", positive=True)
    low_value = _decimal(low_price, f"{prefix}.low", positive=True)
    close_value = _decimal(close_price, f"{prefix}.close", positive=True)
    if high_value < max(open_value, low_value, close_value):
        raise TechnicalFormalDataError(f"{prefix}.high does not cover OHLC")
    if low_value > min(open_value, high_value, close_value):
        raise TechnicalFormalDataError(f"{prefix}.low does not cover OHLC")
    return open_value, high_value, low_value, close_value


@dataclass(frozen=True)
class RawDailyBar:
    """One unadjusted daily OHLC observation used by both derived channels."""

    instrument_id: str
    trading_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument_id", _instrument(self.instrument_id))
        object.__setattr__(self, "trading_date", _exact_date(self.trading_date, "trading_date"))
        open_value, high_value, low_value, close_value = _validate_ohlc(
            self.open, self.high, self.low, self.close, prefix="raw_daily_bar"
        )
        object.__setattr__(self, "open", open_value)
        object.__setattr__(self, "high", high_value)
        object.__setattr__(self, "low", low_value)
        object.__setattr__(self, "close", close_value)


@dataclass(frozen=True)
class AdjustmentFactorPoint:
    """A factor value that becomes usable no earlier than ``effective_date``.

    ``corporate_action_entitled`` is only a data-integrity acknowledgement for
    the factor transition.  It is not a cash/share entitlement ledger and must
    not be used by portfolio accounting as one.
    """

    instrument_id: str
    effective_date: date
    factor: Decimal
    corporate_action_entitled: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument_id", _instrument(self.instrument_id))
        object.__setattr__(
            self, "effective_date", _exact_date(self.effective_date, "effective_date")
        )
        object.__setattr__(self, "factor", _decimal(self.factor, "factor", positive=True))
        object.__setattr__(
            self,
            "corporate_action_entitled",
            _exact_bool(self.corporate_action_entitled, "corporate_action_entitled"),
        )


@dataclass(frozen=True)
class SignalPricePoint:
    """Causally adjusted signal OHLC on a total-return-index scale."""

    instrument_id: str
    trading_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    daily_return: Decimal | None
    cumulative_total_return_index: Decimal
    adjustment_factor: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument_id", _instrument(self.instrument_id))
        object.__setattr__(self, "trading_date", _exact_date(self.trading_date, "trading_date"))
        open_value, high_value, low_value, close_value = _validate_ohlc(
            self.open, self.high, self.low, self.close, prefix="signal_price"
        )
        object.__setattr__(self, "open", open_value)
        object.__setattr__(self, "high", high_value)
        object.__setattr__(self, "low", low_value)
        object.__setattr__(self, "close", close_value)
        if self.daily_return is not None:
            daily_return = _decimal(self.daily_return, "daily_return")
            if daily_return <= -_ONE:
                raise TechnicalFormalDataError("daily_return must be greater than -1")
            object.__setattr__(self, "daily_return", daily_return)
        cumulative = _decimal(
            self.cumulative_total_return_index,
            "cumulative_total_return_index",
            positive=True,
        )
        if cumulative != close_value:
            raise TechnicalFormalDataError(
                "signal close must equal cumulative_total_return_index"
            )
        object.__setattr__(self, "cumulative_total_return_index", cumulative)
        object.__setattr__(
            self,
            "adjustment_factor",
            _decimal(self.adjustment_factor, "adjustment_factor", positive=True),
        )


@dataclass(frozen=True)
class TechnicalExecutionStatus:
    """Decision-safe execution state assembled from the four status histories.

    ``t_plus_one`` describes the frozen settlement rule, not whether a specific
    portfolio lot is sellable.  The backtest must still compare a lot's
    acquisition session with ``trading_date``.  Keeping that rule explicit here
    prevents an execution row from silently falling back to same-day selling.
    """

    instrument_id: str
    trading_date: date
    suspended: bool
    is_st: bool
    price_limit_applicable: bool
    limit_up_price: Decimal | None
    limit_down_price: Decimal | None
    limit_up_locked: bool
    limit_down_locked: bool
    listed: bool
    delisted: bool
    lot_size: int
    t_plus_one: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument_id", _instrument(self.instrument_id))
        object.__setattr__(self, "trading_date", _exact_date(self.trading_date, "trading_date"))
        for field_name in (
            "suspended",
            "is_st",
            "price_limit_applicable",
            "limit_up_locked",
            "limit_down_locked",
            "listed",
            "delisted",
            "t_plus_one",
        ):
            object.__setattr__(
                self, field_name, _exact_bool(getattr(self, field_name), field_name)
            )
        if self.price_limit_applicable:
            limit_up = _decimal(self.limit_up_price, "limit_up_price", positive=True)
            limit_down = _decimal(self.limit_down_price, "limit_down_price", positive=True)
            if limit_up <= limit_down:
                raise TechnicalFormalDataError("limit_up_price must exceed limit_down_price")
        else:
            if self.limit_up_price is not None or self.limit_down_price is not None:
                raise TechnicalFormalDataError(
                    "non-applicable price limit requires null upper/lower bounds"
                )
            if self.limit_up_locked or self.limit_down_locked:
                raise TechnicalFormalDataError(
                    "non-applicable price limit cannot claim a lock"
                )
            limit_up = None
            limit_down = None
        if self.limit_up_locked and self.limit_down_locked:
            raise TechnicalFormalDataError("both price-limit locks cannot be active")
        if self.suspended and (self.limit_up_locked or self.limit_down_locked):
            raise TechnicalFormalDataError("a suspended session cannot claim a price-limit lock")
        if self.listed and self.delisted:
            raise TechnicalFormalDataError("listed and delisted cannot both be true")
        if type(self.lot_size) is not int or self.lot_size != 100:
            raise TechnicalFormalDataError("lot_size must use the frozen 100-share board lot")
        if not self.t_plus_one:
            raise TechnicalFormalDataError("the formal A-share path requires T+1")
        object.__setattr__(self, "limit_up_price", limit_up)
        object.__setattr__(self, "limit_down_price", limit_down)


@dataclass(frozen=True)
class ExecutionPricePoint:
    """Raw unadjusted OHLC plus complete execution gates for one session."""

    instrument_id: str
    trading_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    adjustment_factor: Decimal
    suspended: bool
    is_st: bool
    price_limit_applicable: bool
    limit_up_price: Decimal | None
    limit_down_price: Decimal | None
    limit_up_locked: bool
    limit_down_locked: bool
    listed: bool
    delisted: bool
    lot_size: int
    t_plus_one: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument_id", _instrument(self.instrument_id))
        object.__setattr__(self, "trading_date", _exact_date(self.trading_date, "trading_date"))
        open_value, high_value, low_value, close_value = _validate_ohlc(
            self.open, self.high, self.low, self.close, prefix="execution_price"
        )
        object.__setattr__(self, "open", open_value)
        object.__setattr__(self, "high", high_value)
        object.__setattr__(self, "low", low_value)
        object.__setattr__(self, "close", close_value)
        object.__setattr__(
            self,
            "adjustment_factor",
            _decimal(self.adjustment_factor, "adjustment_factor", positive=True),
        )
        for field_name in (
            "suspended",
            "is_st",
            "price_limit_applicable",
            "limit_up_locked",
            "limit_down_locked",
            "listed",
            "delisted",
            "t_plus_one",
        ):
            object.__setattr__(
                self, field_name, _exact_bool(getattr(self, field_name), field_name)
            )
        if self.price_limit_applicable:
            limit_up = _decimal(self.limit_up_price, "limit_up_price", positive=True)
            limit_down = _decimal(self.limit_down_price, "limit_down_price", positive=True)
            if limit_up <= limit_down:
                raise TechnicalFormalDataError("limit_up_price must exceed limit_down_price")
            if high_value > limit_up or low_value < limit_down:
                raise TechnicalFormalDataError("raw OHLC lies outside the declared price limits")
            if self.limit_up_locked and open_value != limit_up:
                raise TechnicalFormalDataError(
                    "limit_up_locked requires raw open at the upper limit"
                )
            if self.limit_down_locked and open_value != limit_down:
                raise TechnicalFormalDataError(
                    "limit_down_locked requires raw open at the lower limit"
                )
        else:
            if self.limit_up_price is not None or self.limit_down_price is not None:
                raise TechnicalFormalDataError(
                    "non-applicable price limit requires null upper/lower bounds"
                )
            if self.limit_up_locked or self.limit_down_locked:
                raise TechnicalFormalDataError(
                    "non-applicable price limit cannot claim a lock"
                )
            limit_up = None
            limit_down = None
        if self.limit_up_locked and self.limit_down_locked:
            raise TechnicalFormalDataError("both price-limit locks cannot be active")
        if self.suspended and (self.limit_up_locked or self.limit_down_locked):
            raise TechnicalFormalDataError("a suspended session cannot claim a price-limit lock")
        if self.listed and self.delisted:
            raise TechnicalFormalDataError("listed and delisted cannot both be true")
        if type(self.lot_size) is not int or self.lot_size != 100:
            raise TechnicalFormalDataError("lot_size must use the frozen 100-share board lot")
        if not self.t_plus_one:
            raise TechnicalFormalDataError("the formal A-share path requires T+1")
        object.__setattr__(self, "limit_up_price", limit_up)
        object.__setattr__(self, "limit_down_price", limit_down)


@dataclass(frozen=True)
class DualPriceSeries:
    """Paired signal and execution channels for the same raw observations."""

    signal: tuple[SignalPricePoint, ...]
    execution: tuple[ExecutionPricePoint, ...]
    start_date: date

    def __post_init__(self) -> None:
        if type(self.signal) is not tuple or not self.signal:
            raise DualPriceContractError("signal must be a non-empty exact tuple")
        if type(self.execution) is not tuple or not self.execution:
            raise DualPriceContractError("execution must be a non-empty exact tuple")
        if any(type(item) is not SignalPricePoint for item in self.signal):
            raise DualPriceContractError("signal channel accepts exact SignalPricePoint values")
        if any(type(item) is not ExecutionPricePoint for item in self.execution):
            raise DualPriceContractError(
                "execution channel accepts exact ExecutionPricePoint values"
            )
        if len(self.signal) != len(self.execution):
            raise DualPriceContractError("signal and execution channels must align exactly")
        start = _exact_date(self.start_date, "start_date")
        object.__setattr__(self, "start_date", start)
        for signal, execution in zip(self.signal, self.execution, strict=True):
            if (
                signal.instrument_id != execution.instrument_id
                or signal.trading_date != execution.trading_date
            ):
                raise DualPriceContractError(
                    "signal and execution channels differ by instrument or date"
                )
        if self.signal[0].trading_date != start:
            raise DualPriceContractError("series does not start on start_date")
        if self.signal[0].daily_return is not None:
            raise DualPriceContractError("the rebased first point must not claim a prior return")
        if self.signal[0].close != _ONE:
            raise DualPriceContractError("the rebased first signal close must equal 1")


def _strictly_ordered_raw_bars(raw_bars: Sequence[RawDailyBar]) -> tuple[RawDailyBar, ...]:
    rows = tuple(raw_bars)
    if not rows or any(type(item) is not RawDailyBar for item in rows):
        raise DualPriceContractError("raw_bars must contain exact RawDailyBar values")
    instrument = rows[0].instrument_id
    prior: date | None = None
    for row in rows:
        if row.instrument_id != instrument:
            raise DualPriceContractError("raw_bars must contain one instrument")
        if prior is not None and row.trading_date <= prior:
            raise DualPriceContractError("raw_bars must be unique and strictly ascending")
        prior = row.trading_date
    return rows


def _strictly_ordered_factors(
    adjustment_factors: Sequence[AdjustmentFactorPoint],
    instrument_id: str,
) -> tuple[AdjustmentFactorPoint, ...]:
    factors = tuple(adjustment_factors)
    if not factors or any(type(item) is not AdjustmentFactorPoint for item in factors):
        raise DualPriceContractError(
            "adjustment_factors must contain exact AdjustmentFactorPoint values"
        )
    prior: date | None = None
    for item in factors:
        if item.instrument_id != instrument_id:
            raise DualPriceContractError("adjustment_factors must match raw instrument")
        if prior is not None and item.effective_date <= prior:
            raise DualPriceContractError(
                "adjustment_factors must be unique and strictly ascending"
            )
        prior = item.effective_date
    return factors


def _strictly_ordered_execution_states(
    execution_states: Sequence[TechnicalExecutionStatus],
    raw_rows: tuple[RawDailyBar, ...],
) -> tuple[TechnicalExecutionStatus, ...]:
    states = tuple(execution_states)
    if not states or any(type(item) is not TechnicalExecutionStatus for item in states):
        raise DualPriceContractError(
            "execution_states must contain exact TechnicalExecutionStatus values"
        )
    instrument_id = raw_rows[0].instrument_id
    prior: date | None = None
    for item in states:
        if item.instrument_id != instrument_id:
            raise DualPriceContractError("execution_states must match raw instrument")
        if prior is not None and item.trading_date <= prior:
            raise DualPriceContractError(
                "execution_states must be unique and strictly ascending"
            )
        prior = item.trading_date
    states_by_date = {item.trading_date: item for item in states}
    missing_raw_states = tuple(
        item.trading_date for item in raw_rows if item.trading_date not in states_by_date
    )
    if missing_raw_states:
        raise DualPriceContractError(
            f"execution state is missing for raw bar dates: {missing_raw_states}"
        )
    raw_dates = {item.trading_date for item in raw_rows}
    invalid_missing_bars = tuple(
        item.trading_date
        for item in states
        if item.trading_date not in raw_dates
        and item.listed
        and not item.delisted
        and not item.suspended
    )
    if invalid_missing_bars:
        raise DualPriceContractError(
            "listed non-suspended execution sessions are missing raw bars: "
            f"{invalid_missing_bars}"
        )
    return states


def validate_execution_status_coverage(
    execution_states: Sequence[TechnicalExecutionStatus],
    trading_dates: Sequence[date],
) -> tuple[TechnicalExecutionStatus, ...]:
    """Require an independent status row for every controlled trading session.

    A suspended session remains in this returned status calendar even when no
    raw daily bar exists.  This function never creates a price observation.
    """

    states = tuple(execution_states)
    if not states or any(type(item) is not TechnicalExecutionStatus for item in states):
        raise DualPriceContractError(
            "execution_states must contain exact TechnicalExecutionStatus values"
        )
    dates = tuple(trading_dates)
    if not dates or any(type(item) is not date for item in dates):
        raise DualPriceContractError("trading_dates must contain exact date values")
    if any(current <= prior for prior, current in zip(dates, dates[1:])):
        raise DualPriceContractError("trading_dates must be unique and strictly ascending")
    if any(
        current.trading_date <= prior.trading_date
        for prior, current in zip(states, states[1:])
    ):
        raise DualPriceContractError(
            "execution_states must be unique and strictly ascending"
        )
    if tuple(item.trading_date for item in states) != dates:
        raise DualPriceContractError(
            "execution_states must independently cover every controlled trading date"
        )
    instrument_id = states[0].instrument_id
    if any(item.instrument_id != instrument_id for item in states):
        raise DualPriceContractError("execution_states must contain one instrument")
    return states


def build_dual_price_series(
    raw_bars: Sequence[RawDailyBar],
    adjustment_factors: Sequence[AdjustmentFactorPoint],
    execution_states: Sequence[TechnicalExecutionStatus],
    *,
    start_date: date | None = None,
) -> DualPriceSeries:
    """Build causal signal OHLC and untouched execution OHLC.

    For a bar at ``t`` only a factor with ``effective_date <= t`` is selected.
    A factor appended after the last selected bar therefore cannot alter either
    historical returns or historical levels.  Signal OHLC are all transformed
    by the same dated factor and anchor denominator, so ``high`` remains on the
    same causal scale as ``close`` for the frozen BREAKOUT60 formula.
    """

    rows = _strictly_ordered_raw_bars(raw_bars)
    factors = _strictly_ordered_factors(adjustment_factors, rows[0].instrument_id)
    states = _strictly_ordered_execution_states(execution_states, rows)
    anchor = rows[0].trading_date if start_date is None else _exact_date(start_date, "start_date")
    dates = {row.trading_date for row in rows}
    if anchor not in dates:
        raise DualPriceContractError("start_date must be an observed raw trading date")
    selected_rows = tuple(row for row in rows if row.trading_date >= anchor)

    causal_factors = tuple(
        item for item in factors if item.effective_date <= selected_rows[-1].trading_date
    )
    for previous, current in zip(causal_factors, causal_factors[1:]):
        if (
            current.factor != previous.factor
            and not current.corporate_action_entitled
        ):
            raise DualPriceContractError(
                "adjustment factor changed without a confirmed corporate-action entitlement"
            )

    factor_index = -1
    selected: list[tuple[RawDailyBar, AdjustmentFactorPoint]] = []
    for row in selected_rows:
        while (
            factor_index + 1 < len(factors)
            and factors[factor_index + 1].effective_date <= row.trading_date
        ):
            factor_index += 1
        if factor_index < 0:
            raise DualPriceContractError(
                f"missing effective adjustment factor for {row.trading_date.isoformat()}"
            )
        selected.append((row, factors[factor_index]))

    anchor_bar, anchor_factor = selected[0]
    denominator = anchor_bar.close * anchor_factor.factor
    if denominator <= _ZERO or not denominator.is_finite():
        raise DualPriceContractError("signal anchor denominator is invalid")

    signal: list[SignalPricePoint] = []
    execution: list[ExecutionPricePoint] = []
    prior_adjusted_close: Decimal | None = None
    states_by_date = {item.trading_date: item for item in states}
    for row, factor_point in selected:
        factor = factor_point.factor
        adjusted_close = row.close * factor
        daily_return = (
            None
            if prior_adjusted_close is None
            else adjusted_close / prior_adjusted_close - _ONE
        )
        signal_close = adjusted_close / denominator
        signal.append(
            SignalPricePoint(
                instrument_id=row.instrument_id,
                trading_date=row.trading_date,
                open=row.open * factor / denominator,
                high=row.high * factor / denominator,
                low=row.low * factor / denominator,
                close=signal_close,
                daily_return=daily_return,
                cumulative_total_return_index=signal_close,
                adjustment_factor=factor,
            )
        )
        execution_state = states_by_date[row.trading_date]
        execution.append(
            ExecutionPricePoint(
                instrument_id=row.instrument_id,
                trading_date=row.trading_date,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                adjustment_factor=factor,
                suspended=execution_state.suspended,
                is_st=execution_state.is_st,
                price_limit_applicable=execution_state.price_limit_applicable,
                limit_up_price=execution_state.limit_up_price,
                limit_down_price=execution_state.limit_down_price,
                limit_up_locked=execution_state.limit_up_locked,
                limit_down_locked=execution_state.limit_down_locked,
                listed=execution_state.listed,
                delisted=execution_state.delisted,
                lot_size=execution_state.lot_size,
                t_plus_one=execution_state.t_plus_one,
            )
        )
        prior_adjusted_close = adjusted_close
    return DualPriceSeries(tuple(signal), tuple(execution), anchor)


def _decimal_rounding_tolerance(value: Decimal) -> Decimal:
    """Return half one unit in the last *reported* Decimal place."""

    exponent = value.as_tuple().exponent
    quantum = _ONE.scaleb(exponent)
    return abs(quantum) / Decimal("2")


@dataclass(frozen=True)
class PITMembershipRecord:
    index_code: str
    component_id: str
    snapshot_date: date
    weight: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "index_code", _instrument(self.index_code, "index_code"))
        object.__setattr__(
            self, "component_id", _a_share_instrument(self.component_id, "component_id")
        )
        object.__setattr__(
            self, "snapshot_date", _exact_date(self.snapshot_date, "snapshot_date")
        )
        weight = _decimal(self.weight, "weight", positive=True)
        # The candidate index_weight sample reports three decimal places. A
        # coarser source value makes the derived aggregate rounding interval
        # too wide to prove that 800 weights close to 100 percent. Preserve
        # source precision and fail closed instead of accepting, for example,
        # 800 values of 0.1 whose sum is only 80.
        decimal_places = max(0, -weight.as_tuple().exponent)
        if decimal_places < MIN_INDEX_WEIGHT_DECIMAL_PLACES:
            raise PITUniverseError(
                "index_weight precision must retain at least three decimal places"
            )
        object.__setattr__(self, "weight", weight)


@dataclass(frozen=True)
class PITMonthlySnapshot:
    index_code: str
    snapshot_date: date
    members: tuple[PITMembershipRecord, ...]
    weight_sum: Decimal = field(init=False)
    weight_tolerance: Decimal = field(init=False)

    def __post_init__(self) -> None:
        index_code = _instrument(self.index_code, "index_code")
        if index_code != CSI800_INDEX_CODE:
            raise PITUniverseError("PIT universe only admits CSI 800 index_code=000906.SH")
        snapshot_date = _exact_date(self.snapshot_date, "snapshot_date")
        if type(self.members) is not tuple:
            raise PITUniverseError("members must use the exact tuple type")
        if len(self.members) != 800:
            raise PITUniverseError("each CSI 800 snapshot must contain exactly 800 members")
        if any(type(item) is not PITMembershipRecord for item in self.members):
            raise PITUniverseError("members must contain exact PITMembershipRecord values")
        component_ids: set[str] = set()
        weight_sum = _ZERO
        tolerance = _ZERO
        for item in self.members:
            if item.index_code != index_code or item.snapshot_date != snapshot_date:
                raise PITUniverseError("snapshot member index/date does not match its envelope")
            if item.component_id in component_ids:
                raise PITUniverseError("snapshot contains a duplicate component")
            component_ids.add(item.component_id)
            weight_sum += item.weight
            tolerance += _decimal_rounding_tolerance(item.weight)
        if abs(weight_sum - _HUNDRED) > tolerance:
            raise PITUniverseError(
                "snapshot weight sum exceeds tolerance derived from raw Decimal precision"
            )
        object.__setattr__(self, "index_code", index_code)
        object.__setattr__(self, "snapshot_date", snapshot_date)
        object.__setattr__(self, "weight_sum", weight_sum)
        object.__setattr__(self, "weight_tolerance", tolerance)


def _month_keys(start: date, end: date) -> tuple[tuple[int, int], ...]:
    year, month = start.year, start.month
    output: list[tuple[int, int]] = []
    while (year, month) <= (end.year, end.month):
        output.append((year, month))
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
    return tuple(output)


def _previous_month_key(value: date) -> tuple[int, int]:
    if value.month == 1:
        return value.year - 1, 12
    return value.year, value.month - 1


def _index_weight_date(value: Any, field_name: str) -> date:
    if type(value) is date:
        return value
    if type(value) is not str or value != value.strip():
        raise PITUniverseError(f"{field_name} must be an exact date or date string")
    try:
        if re.fullmatch(r"[0-9]{8}", value):
            return datetime.strptime(value, "%Y%m%d").date()
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            return date.fromisoformat(value)
    except ValueError as exc:
        raise PITUniverseError(f"{field_name} is not a valid calendar date") from exc
    raise PITUniverseError(f"{field_name} must use YYYYMMDD or YYYY-MM-DD")


def _index_weight_decimal(value: Any) -> Decimal:
    if type(value) is Decimal:
        parsed = value
    elif type(value) is not str or not value or value != value.strip():
        raise PITUniverseError(
            "index_weight weight must be Decimal or exact decimal text, never float"
        )
    else:
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise PITUniverseError("index_weight weight is not decimal text") from exc
    if not parsed.is_finite() or parsed <= _ZERO:
        raise PITUniverseError("index_weight weight must be positive and finite")
    return parsed


@dataclass(frozen=True)
class PITUniverseLoader:
    """Fail-closed monthly CSI 800 universe selector."""

    snapshots: tuple[PITMonthlySnapshot, ...]
    coverage_start: date
    coverage_end: date

    def __post_init__(self) -> None:
        start = _exact_date(self.coverage_start, "coverage_start")
        end = _exact_date(self.coverage_end, "coverage_end")
        if start > end:
            raise PITUniverseError("coverage_start cannot follow coverage_end")
        if type(self.snapshots) is not tuple or not self.snapshots:
            raise PITUniverseError("snapshots must be a non-empty exact tuple")
        if any(type(item) is not PITMonthlySnapshot for item in self.snapshots):
            raise PITUniverseError("snapshots must contain exact PITMonthlySnapshot values")
        prior: date | None = None
        month_map: dict[tuple[int, int], PITMonthlySnapshot] = {}
        index_code = self.snapshots[0].index_code
        for snapshot in self.snapshots:
            if snapshot.index_code != index_code:
                raise PITUniverseError("all snapshots must use one index code")
            if prior is not None and snapshot.snapshot_date <= prior:
                raise PITUniverseError("snapshot dates must be strictly ascending")
            month_key = (snapshot.snapshot_date.year, snapshot.snapshot_date.month)
            if month_key in month_map:
                raise PITUniverseError("more than one membership snapshot exists for a month")
            month_map[month_key] = snapshot
            prior = snapshot.snapshot_date
        expected = set(_month_keys(start, end))
        expected.add(_previous_month_key(start))
        actual = set(month_map)
        if actual != expected:
            missing = sorted(expected - actual)
            unexpected = sorted(actual - expected)
            raise PITUniverseError(
                f"monthly PIT coverage is incomplete: missing={missing}, unexpected={unexpected}"
            )
        object.__setattr__(self, "coverage_start", start)
        object.__setattr__(self, "coverage_end", end)

    @classmethod
    def from_index_weight_rows(
        cls,
        rows: Sequence[Mapping[str, Any]],
        *,
        coverage_start: date,
        coverage_end: date,
    ) -> "PITUniverseLoader":
        """Parse strict non-VIP ``index_weight`` candidate rows.

        Accepted raw envelopes use exactly ``index_code``, ``con_code``,
        ``weight`` and one of ``trade_date``/``snapshot_date``.  Decimal text is
        parsed without quantization so the provider's reported precision drives
        the weight-sum tolerance.
        """

        supplied = tuple(rows)
        if not supplied or any(not isinstance(item, Mapping) for item in supplied):
            raise PITUniverseError("index_weight rows must be a non-empty mapping sequence")
        records_by_date: dict[date, list[PITMembershipRecord]] = {}
        seen_keys: set[tuple[str, date, str]] = set()
        prior_date: date | None = None
        chosen_date_field: str | None = None
        for row_number, supplied_row in enumerate(supplied, start=1):
            row = dict(supplied_row)
            date_fields = {name for name in ("trade_date", "snapshot_date") if name in row}
            if len(date_fields) != 1:
                raise PITUniverseError(
                    f"index_weight row {row_number} must contain exactly one date field"
                )
            date_field = next(iter(date_fields))
            if chosen_date_field is None:
                chosen_date_field = date_field
            elif date_field != chosen_date_field:
                raise PITUniverseError("index_weight rows mix date field conventions")
            expected_fields = {"index_code", "con_code", "weight", date_field}
            if set(row) != expected_fields:
                raise PITUniverseError(
                    f"index_weight row {row_number} has missing or unknown fields"
                )
            if row["index_code"] != CSI800_INDEX_CODE:
                raise PITUniverseError(
                    "index_weight row is not the CSI 800 index_code=000906.SH"
                )
            snapshot_date = _index_weight_date(row[date_field], date_field)
            if prior_date is not None and snapshot_date < prior_date:
                raise PITUniverseError("index_weight rows are not date ordered")
            prior_date = snapshot_date
            try:
                component_id = _a_share_instrument(row["con_code"], "con_code")
            except TechnicalFormalDataError as exc:
                raise PITUniverseError(
                    f"index_weight row {row_number} has an invalid con_code"
                ) from exc
            primary_key = (CSI800_INDEX_CODE, snapshot_date, component_id)
            if primary_key in seen_keys:
                raise PITUniverseError("index_weight rows contain a duplicate primary key")
            seen_keys.add(primary_key)
            record = PITMembershipRecord(
                index_code=CSI800_INDEX_CODE,
                component_id=component_id,
                snapshot_date=snapshot_date,
                weight=_index_weight_decimal(row["weight"]),
            )
            records_by_date.setdefault(snapshot_date, []).append(record)
        snapshots = tuple(
            PITMonthlySnapshot(
                index_code=CSI800_INDEX_CODE,
                snapshot_date=snapshot_date,
                members=tuple(records),
            )
            for snapshot_date, records in records_by_date.items()
        )
        return cls(
            snapshots=snapshots,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
        )

    def snapshot_strictly_before(self, decision_date: date) -> PITMonthlySnapshot:
        decision = _exact_date(decision_date, "decision_date")
        if not self.coverage_start <= decision <= self.coverage_end:
            raise PITUniverseError("decision_date lies outside declared PIT coverage")
        candidates = tuple(
            snapshot for snapshot in self.snapshots if snapshot.snapshot_date < decision
        )
        if not candidates:
            raise PITUniverseError(
                "no PIT membership snapshot exists strictly before decision_date"
            )
        return candidates[-1]

    def members_strictly_before(
        self, decision_date: date
    ) -> tuple[PITMembershipRecord, ...]:
        # Return the original immutable records.  Their weights are never
        # normalized to force an exact sum of 100.
        return self.snapshot_strictly_before(decision_date).members


@dataclass(frozen=True)
class DatasetInventoryEntry:
    dataset_id: str
    status: str
    source: str | None
    interface: str | None
    record_count: int
    coverage_start: date | None
    coverage_end: date | None
    missing_dates: tuple[str, ...]
    content_sha256: str | None
    issues: tuple[str, ...]

    def __post_init__(self) -> None:
        dataset_id = _exact_text(self.dataset_id, "dataset_id")
        if dataset_id not in TECHNICAL_FORMAL_DATASET_IDS:
            raise DatasetManifestError(f"unsupported technical dataset: {dataset_id}")
        status = _exact_text(self.status, "status")
        if status not in {"complete", "missing", "partial", "invalid"}:
            raise DatasetManifestError("dataset status is unsupported")
        for field_name in ("source", "interface"):
            value = getattr(self, field_name)
            if value is not None:
                _exact_text(value, field_name)
        count = _exact_nonnegative_int(self.record_count, "record_count")
        start = (
            _exact_date(self.coverage_start, "coverage_start")
            if self.coverage_start is not None
            else None
        )
        end = (
            _exact_date(self.coverage_end, "coverage_end")
            if self.coverage_end is not None
            else None
        )
        if (start is None) != (end is None) or start is not None and start > end:  # type: ignore[operator]
            raise DatasetManifestError("dataset coverage dates are inconsistent")
        if type(self.missing_dates) is not tuple or any(
            type(item) is not str or not item or item != item.strip()
            for item in self.missing_dates
        ):
            raise DatasetManifestError("missing_dates must be an exact tuple of strings")
        if len(set(self.missing_dates)) != len(self.missing_dates):
            raise DatasetManifestError("missing_dates must be unique")
        try:
            parsed_missing_dates = tuple(
                date.fromisoformat(item) for item in self.missing_dates
            )
        except ValueError as exc:
            raise DatasetManifestError("missing_dates must use ISO calendar dates") from exc
        if parsed_missing_dates != tuple(sorted(parsed_missing_dates)):
            raise DatasetManifestError("missing_dates must be strictly date ordered")
        if self.content_sha256 is not None and (
            type(self.content_sha256) is not str
            or _SHA256.fullmatch(self.content_sha256) is None
        ):
            raise DatasetManifestError("content_sha256 must be a lowercase SHA-256")
        if type(self.issues) is not tuple or any(
            type(item) is not str or not item or item != item.strip() for item in self.issues
        ):
            raise DatasetManifestError("issues must be an exact tuple of strings")
        if len(set(self.issues)) != len(self.issues):
            raise DatasetManifestError("issues must be unique")
        if status == "complete" and (
            self.source is None
            or self.interface is None
            or count == 0
            or start is None
            or self.content_sha256 is None
            or self.missing_dates
            or self.issues
        ):
            raise DatasetManifestError("complete dataset evidence is incomplete")
        if status != "complete" and not self.issues:
            raise DatasetManifestError("non-complete dataset evidence must explain its blockers")
        object.__setattr__(self, "dataset_id", dataset_id)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "record_count", count)
        object.__setattr__(self, "coverage_start", start)
        object.__setattr__(self, "coverage_end", end)

    def to_manifest_value(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source": self.source,
            "interface": self.interface,
            "record_count": self.record_count,
            "coverage_start": self.coverage_start.isoformat() if self.coverage_start else None,
            "coverage_end": self.coverage_end.isoformat() if self.coverage_end else None,
            "missing_dates": list(self.missing_dates),
            "content_sha256": self.content_sha256,
            "issues": list(self.issues),
        }


def build_blocked_dataset_inventory(
    *,
    default_issue: str = "formal_dataset_not_collected",
    issues_by_dataset: Mapping[str, Sequence[str]] | None = None,
) -> tuple[DatasetInventoryEntry, ...]:
    """Create an explicit nine-dataset missing inventory, never an empty claim."""

    default = _exact_text(default_issue, "default_issue")
    supplied = dict(issues_by_dataset or {})
    unknown = set(supplied) - set(TECHNICAL_FORMAL_DATASET_IDS)
    if unknown:
        raise DatasetManifestError(f"unknown dataset blockers: {sorted(unknown)}")
    entries: list[DatasetInventoryEntry] = []
    for dataset_id in TECHNICAL_FORMAL_DATASET_IDS:
        supplied_issues = supplied.get(dataset_id, (default,))
        if isinstance(supplied_issues, (str, bytes)) or not isinstance(
            supplied_issues, Sequence
        ):
            raise DatasetManifestError("dataset blocker overrides must be arrays")
        raw_issues = tuple(supplied_issues)
        if not raw_issues:
            raw_issues = (default,)
        entries.append(
            DatasetInventoryEntry(
                dataset_id=dataset_id,
                status="missing",
                source=None,
                interface=None,
                record_count=0,
                coverage_start=None,
                coverage_end=None,
                missing_dates=(),
                content_sha256=None,
                issues=raw_issues,
            )
        )
    return tuple(entries)


def _critical_checks(value: Mapping[str, bool]) -> dict[str, bool]:
    if not isinstance(value, Mapping) or set(value) != set(CRITICAL_CHECK_IDS):
        raise DatasetManifestError("critical_checks must contain the exact frozen keys")
    output: dict[str, bool] = {}
    for check_id in CRITICAL_CHECK_IDS:
        output[check_id] = _exact_bool(value[check_id], f"critical_checks.{check_id}")
    return output


def _dataset_entry_blockers(
    entry: DatasetInventoryEntry,
    *,
    required_start: date,
    required_end: date,
) -> tuple[str, ...]:
    """Use the same semantic blocker vocabulary as the reporting verifier."""

    dataset_id = entry.dataset_id
    blockers: list[str] = []
    if entry.status != "complete":
        blockers.append(f"{dataset_id}:status_{entry.status}")
    if entry.record_count == 0:
        blockers.append(f"{dataset_id}:no_records")
    if entry.source is None or entry.interface is None:
        blockers.append(f"{dataset_id}:source_or_interface_missing")
    if entry.content_sha256 is None:
        blockers.append(f"{dataset_id}:content_hash_missing")
    if entry.coverage_start is None or entry.coverage_start > required_start:
        blockers.append(f"{dataset_id}:warmup_or_start_coverage_missing")
    if entry.coverage_end is None or entry.coverage_end < required_end:
        blockers.append(f"{dataset_id}:end_coverage_missing")
    if entry.missing_dates:
        blockers.append(f"{dataset_id}:missing_dates")
    if entry.issues:
        blockers.append(f"{dataset_id}:issues_present")
    return tuple(blockers)


def _dataset_source_blockers(entry: DatasetInventoryEntry) -> tuple[str, ...]:
    if entry.source is None and entry.interface is None:
        return ()
    endpoints = ALLOWED_STANDARD_INTERFACES.get(entry.source or "")
    if endpoints is None:
        return (f"{entry.dataset_id}:source_not_allowed",)
    if entry.interface not in endpoints:
        return (f"{entry.dataset_id}:interface_not_allowed_for_source",)
    if (entry.source, entry.interface) not in DATASET_STANDARD_INTERFACES[
        entry.dataset_id
    ]:
        return (f"{entry.dataset_id}:interface_not_allowed_for_dataset",)
    return ()


def required_dataset_coverage_start(dataset_id: str) -> date:
    """Return the frozen earliest evidence boundary for one formal dataset."""

    resolved = _exact_text(dataset_id, "dataset_id")
    if resolved not in TECHNICAL_FORMAL_DATASET_IDS:
        raise DatasetManifestError(f"unsupported technical dataset: {resolved}")
    if resolved in _WARMUP_DATASET_IDS:
        return MANIFEST_WARMUP_START
    if resolved == "csi800_pit_membership":
        # The preceding-month snapshot is needed by the strict-before selector
        # for the first 2018 decision.  Monthly in-range completeness is a
        # separate PITUniverseLoader / critical-check invariant.
        return PIT_BOOTSTRAP_LATEST
    return MANIFEST_COVERAGE_START


def build_technical_formal_dataset_manifest(
    entries: Sequence[DatasetInventoryEntry],
    *,
    dataset_id: str,
    generated_at: datetime,
    critical_checks: Mapping[str, bool],
    remaining_blockers: Sequence[str] = (),
    coverage_start: date = MANIFEST_COVERAGE_START,
    coverage_end: date = MANIFEST_COVERAGE_END,
    warmup_start: date = MANIFEST_WARMUP_START,
) -> dict[str, Any]:
    """Build the schema-shaped manifest and derive READY/BLOCKED fail-closed."""

    resolved_dataset_id = _exact_text(dataset_id, "dataset_id")
    generated = _exact_datetime(generated_at, "generated_at")
    start = _exact_date(coverage_start, "coverage_start")
    end = _exact_date(coverage_end, "coverage_end")
    warmup = _exact_date(warmup_start, "warmup_start")
    if (start, end, warmup) != (
        MANIFEST_COVERAGE_START,
        MANIFEST_COVERAGE_END,
        MANIFEST_WARMUP_START,
    ):
        raise DatasetManifestError("manifest coverage and warmup dates are frozen")
    resolved_entries = tuple(entries)
    if any(type(item) is not DatasetInventoryEntry for item in resolved_entries):
        raise DatasetManifestError("entries must contain exact DatasetInventoryEntry values")
    by_id = {item.dataset_id: item for item in resolved_entries}
    if len(resolved_entries) != len(TECHNICAL_FORMAL_DATASET_IDS) or set(by_id) != set(
        TECHNICAL_FORMAL_DATASET_IDS
    ):
        raise DatasetManifestError("manifest must contain each of the nine datasets exactly once")
    checks = _critical_checks(critical_checks)
    if isinstance(remaining_blockers, (str, bytes)) or not isinstance(
        remaining_blockers, Sequence
    ):
        raise DatasetManifestError("remaining_blockers must be an array")
    blockers = tuple(remaining_blockers)
    if any(type(item) is not str or not item or item != item.strip() for item in blockers):
        raise DatasetManifestError("remaining_blockers must contain non-empty strings")
    if len(set(blockers)) != len(blockers):
        raise DatasetManifestError("remaining_blockers must be unique")

    # This pure domain builder deliberately has no authority to certify raw
    # files.  Until the standard CLI emits an independently verified receipt,
    # caller-supplied source labels, hashes and booleans must never unlock READY.
    derived_blockers: list[str] = [
        *blockers,
        STANDARD_CLI_VERIFICATION_BLOCKER,
    ]
    for dataset_key in TECHNICAL_FORMAL_DATASET_IDS:
        entry = by_id[dataset_key]
        derived_blockers.extend(
            _dataset_entry_blockers(
                entry,
                required_start=required_dataset_coverage_start(dataset_key),
                required_end=end,
            )
        )
        derived_blockers.extend(_dataset_source_blockers(entry))
    for check_id in CRITICAL_CHECK_IDS:
        if not checks[check_id]:
            derived_blockers.append(f"critical_check_failed:{check_id}")
    derived_blockers = sorted(set(derived_blockers))
    ready = not derived_blockers
    payload: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "dataset_id": resolved_dataset_id,
        "strategy_id": STRATEGY_ID,
        "generated_at": generated.isoformat(),
        "coverage_start": start.isoformat(),
        "coverage_end": end.isoformat(),
        "warmup_start": warmup.isoformat(),
        "datasets": {
            dataset_key: by_id[dataset_key].to_manifest_value()
            for dataset_key in TECHNICAL_FORMAL_DATASET_IDS
        },
        "critical_checks": checks,
        "data_status": "READY" if ready else "BLOCKED",
        "remaining_blockers": derived_blockers,
        "locked_test_status": "NOT_RUN",
        "locked_test_consumed": False,
        "safety": {
            "paper_eligibility": False,
            "trade_eligibility": False,
            "real_money_list_allowed": False,
            "automatic_order_submission": False,
            "live_supported": False,
        },
    }
    payload["manifest_sha256"] = canonical_sha256(payload)
    validate_technical_formal_dataset_manifest(payload)
    return payload


def _parse_optional_manifest_date(value: Any, field_name: str) -> date | None:
    if value is None:
        return None
    if type(value) is not str:
        raise DatasetManifestError(f"{field_name} must be an ISO date or null")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise DatasetManifestError(f"{field_name} must be an ISO date") from exc


def validate_technical_formal_dataset_manifest(manifest: Mapping[str, Any]) -> None:
    """Validate structural invariants and recompute the manifest hash."""

    if not isinstance(manifest, Mapping):
        raise DatasetManifestError("manifest must be an object")
    expected_fields = {
        "schema_version",
        "dataset_id",
        "strategy_id",
        "generated_at",
        "coverage_start",
        "coverage_end",
        "warmup_start",
        "datasets",
        "critical_checks",
        "data_status",
        "remaining_blockers",
        "locked_test_status",
        "locked_test_consumed",
        "safety",
        "manifest_sha256",
    }
    if set(manifest) != expected_fields:
        raise DatasetManifestError("manifest fields differ from the V1 contract")
    if manifest["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise DatasetManifestError("manifest schema_version is unsupported")
    _exact_text(manifest["dataset_id"], "dataset_id")
    if manifest["strategy_id"] != STRATEGY_ID:
        raise DatasetManifestError("manifest strategy_id is not frozen")
    if type(manifest["generated_at"]) is not str:
        raise DatasetManifestError("generated_at must be an ISO datetime string")
    try:
        generated_at = datetime.fromisoformat(manifest["generated_at"])
    except ValueError as exc:
        raise DatasetManifestError("generated_at must be an ISO datetime") from exc
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise DatasetManifestError("generated_at must include a timezone offset")
    if (
        manifest["coverage_start"] != MANIFEST_COVERAGE_START.isoformat()
        or manifest["coverage_end"] != MANIFEST_COVERAGE_END.isoformat()
        or manifest["warmup_start"] != MANIFEST_WARMUP_START.isoformat()
    ):
        raise DatasetManifestError("manifest dates differ from the frozen contract")
    datasets = manifest["datasets"]
    if not isinstance(datasets, Mapping) or set(datasets) != set(
        TECHNICAL_FORMAL_DATASET_IDS
    ):
        raise DatasetManifestError("manifest datasets are incomplete")
    entries: list[DatasetInventoryEntry] = []
    for dataset_key in TECHNICAL_FORMAL_DATASET_IDS:
        value = datasets[dataset_key]
        if not isinstance(value, Mapping) or set(value) != {
            "status",
            "source",
            "interface",
            "record_count",
            "coverage_start",
            "coverage_end",
            "missing_dates",
            "content_sha256",
            "issues",
        }:
            raise DatasetManifestError(f"dataset manifest is malformed: {dataset_key}")
        missing_dates = value["missing_dates"]
        issues = value["issues"]
        if type(missing_dates) is not list or type(issues) is not list:
            raise DatasetManifestError("dataset missing_dates/issues must be arrays")
        entries.append(
            DatasetInventoryEntry(
                dataset_id=dataset_key,
                status=value["status"],
                source=value["source"],
                interface=value["interface"],
                record_count=value["record_count"],
                coverage_start=_parse_optional_manifest_date(
                    value["coverage_start"], f"{dataset_key}.coverage_start"
                ),
                coverage_end=_parse_optional_manifest_date(
                    value["coverage_end"], f"{dataset_key}.coverage_end"
                ),
                missing_dates=tuple(missing_dates),
                content_sha256=value["content_sha256"],
                issues=tuple(issues),
            )
        )
    checks = _critical_checks(manifest["critical_checks"])
    blockers = manifest["remaining_blockers"]
    if type(blockers) is not list or any(
        type(item) is not str or not item or item != item.strip() for item in blockers
    ):
        raise DatasetManifestError("remaining_blockers must be an array of strings")
    if blockers != sorted(set(blockers)):
        raise DatasetManifestError("remaining_blockers must be unique and sorted")
    required_blockers: set[str] = set()
    required_blockers.add(STANDARD_CLI_VERIFICATION_BLOCKER)
    for entry in entries:
        required_blockers.update(
            _dataset_entry_blockers(
                entry,
                required_start=required_dataset_coverage_start(entry.dataset_id),
                required_end=MANIFEST_COVERAGE_END,
            )
        )
        required_blockers.update(_dataset_source_blockers(entry))
    required_blockers.update(
        f"critical_check_failed:{check_id}"
        for check_id, passed in checks.items()
        if not passed
    )
    if required_blockers - set(blockers):
        raise DatasetManifestError("remaining_blockers omit derived dataset/check failures")
    derived_ready = (
        all(
            item.status == "complete"
            and item.coverage_start is not None
            and item.coverage_start <= required_dataset_coverage_start(item.dataset_id)
            and item.coverage_end is not None
            and item.coverage_end >= MANIFEST_COVERAGE_END
            for item in entries
        )
        and all(checks.values())
        and not blockers
    )
    expected_status = "READY" if derived_ready else "BLOCKED"
    if manifest["data_status"] != expected_status:
        raise DatasetManifestError("data_status disagrees with datasets/checks/blockers")
    if manifest["locked_test_status"] != "NOT_RUN" or manifest["locked_test_consumed"] is not False:
        raise DatasetManifestError("locked test boundary was violated")
    expected_safety = {
        "paper_eligibility": False,
        "trade_eligibility": False,
        "real_money_list_allowed": False,
        "automatic_order_submission": False,
        "live_supported": False,
    }
    if manifest["safety"] != expected_safety:
        raise DatasetManifestError("manifest safety flags must remain false")
    supplied_hash = manifest["manifest_sha256"]
    if type(supplied_hash) is not str or _SHA256.fullmatch(supplied_hash) is None:
        raise DatasetManifestError("manifest_sha256 must be a lowercase SHA-256")
    unhashed = dict(manifest)
    del unhashed["manifest_sha256"]
    expected_hash = canonical_sha256(unhashed)
    if supplied_hash != expected_hash:
        raise DatasetManifestError("manifest_sha256 does not match canonical content")


def blocked_dataset_inventory(
    manifest: Mapping[str, Any],
) -> Mapping[str, tuple[str, ...]]:
    """Return immutable, explicit blocker reasons for every non-ready dataset."""

    validate_technical_formal_dataset_manifest(manifest)
    output: dict[str, tuple[str, ...]] = {}
    for dataset_id in TECHNICAL_FORMAL_DATASET_IDS:
        value = manifest["datasets"][dataset_id]
        if value["status"] != "complete":
            reasons = tuple(value["issues"]) or (
                f"dataset_{dataset_id}_{value['status']}",
            )
            output[dataset_id] = reasons
    return MappingProxyType(output)


__all__ = [
    "AdjustmentFactorPoint",
    "ALLOWED_STANDARD_INTERFACES",
    "CSI800_INDEX_CODE",
    "CRITICAL_CHECK_IDS",
    "DatasetInventoryEntry",
    "DATASET_STANDARD_INTERFACES",
    "DatasetManifestError",
    "DualPriceContractError",
    "DualPriceSeries",
    "ExecutionPricePoint",
    "MANIFEST_COVERAGE_END",
    "MANIFEST_COVERAGE_START",
    "MANIFEST_SCHEMA_VERSION",
    "MANIFEST_WARMUP_START",
    "MIN_INDEX_WEIGHT_DECIMAL_PLACES",
    "PITMembershipRecord",
    "PIT_BOOTSTRAP_LATEST",
    "PITMonthlySnapshot",
    "PITUniverseError",
    "PITUniverseLoader",
    "RawDailyBar",
    "STRATEGY_ID",
    "STANDARD_CLI_VERIFICATION_BLOCKER",
    "SignalPricePoint",
    "TECHNICAL_FORMAL_DATASET_IDS",
    "TechnicalFormalDataError",
    "TechnicalExecutionStatus",
    "blocked_dataset_inventory",
    "build_blocked_dataset_inventory",
    "build_dual_price_series",
    "build_technical_formal_dataset_manifest",
    "required_dataset_coverage_start",
    "validate_execution_status_coverage",
    "validate_technical_formal_dataset_manifest",
]
