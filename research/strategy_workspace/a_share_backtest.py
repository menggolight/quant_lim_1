"""Fail-closed A-share Top-2 paper backtest.

The module is deliberately separate from the generic strategy-workspace
backtest.  It models the small-account policy agreed for this experiment and
has no broker, order-routing, or LIVE execution capability.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
import re
from typing import Any, ClassVar, Mapping, Sequence

from .contracts import canonical_sha256
from .evaluation import (
    BenchmarkTotalReturnPoint,
    EvaluationResult,
    OOSPrediction,
    PreparedCrossSection,
    benchmark_total_return_series_content_sha256,
    evaluation_result_content_sha256,
    trading_calendar_content_sha256,
)
from .experiment import ExperimentSpecV2
from .top_decile_backtest import BenchmarkTotalReturnBar


ZERO = Decimal("0")
ONE = Decimal("1")
MONEY = Decimal("0.0001")
PCT = Decimal("0.00000001")
AS_SHARE_BACKTEST_VERSION = "strategy-workspace-a-share-top2.v3"
UNMANAGED_MIDEA_INSTRUMENT_ID = "000333.SZ"
DIAGNOSTIC_SIGNAL_SCOPE = "diagnostic_raw_signal_not_admission"
FORMAL_SIGNAL_SCOPE = "formal_evaluation_bound_locked_test"
FORMAL_BACKTEST_SCOPE = "formal_evaluation_bound_historical"
FORMAL_SPLIT = "locked_test"
PRIMARY_MODEL = "ridge_alpha_1"
FORMAL_PREDICTION_MODELS = (PRIMARY_MODEL, "direction_equal_weight")
FORMAL_HORIZON_SESSIONS = 20
CONTROLLED_EXECUTION_BAR_ADAPTER_VERIFIED = False
CHINA_TZ = timezone(timedelta(hours=8))
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FORMAL_CLOSE_SIGNAL_TOKEN = object()


class AShareBacktestError(ValueError):
    """Raised when an input or transition would weaken the frozen policy."""


def _decimal(value: Any, field_name: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:  # pragma: no cover - defensive boundary
        raise AShareBacktestError(f"{field_name} must be decimal-compatible") from exc
    if not result.is_finite():
        raise AShareBacktestError(f"{field_name} must be finite")
    return result


def _date(value: Any, field_name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except Exception as exc:  # pragma: no cover - defensive boundary
        raise AShareBacktestError(f"{field_name} must be an ISO date") from exc


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY)


def _content_hash(value: Any) -> str:
    return canonical_sha256(value)


@dataclass(frozen=True)
class RankedStockCandidate:
    """One score frozen at a decision-date close; percentile 1.0 is best."""

    instrument_id: str
    csi_level1_industry: str
    score: Decimal
    percentile: Decimal
    manual_veto: bool = False

    def __post_init__(self) -> None:
        instrument_id = str(self.instrument_id).strip().upper()
        industry = str(self.csi_level1_industry).strip()
        score = _decimal(self.score, "score")
        percentile = _decimal(self.percentile, "percentile")
        if not instrument_id or not industry:
            raise AShareBacktestError("candidate identity and industry are required")
        if percentile < ZERO or percentile > ONE:
            raise AShareBacktestError("percentile must be in [0, 1]")
        if type(self.manual_veto) is not bool:
            raise AShareBacktestError("manual_veto must be boolean")
        object.__setattr__(self, "instrument_id", instrument_id)
        object.__setattr__(self, "csi_level1_industry", industry)
        object.__setattr__(self, "score", score)
        object.__setattr__(self, "percentile", percentile)


@dataclass(frozen=True)
class CloseSignal:
    """A close-known ranking that may execute only at the next session open."""

    decision_id: str
    signal_date: date
    candidates: tuple[RankedStockCandidate, ...]
    signal_scope: str = DIAGNOSTIC_SIGNAL_SCOPE
    evaluation_sha256: str | None = None
    experiment_spec_sha256: str | None = None
    member_ids_sha256: str | None = None
    ranking_sha256: str | None = None
    _formal_signal_token: InitVar[object] = None

    def __post_init__(self, _formal_signal_token: object) -> None:
        decision_id = str(self.decision_id).strip()
        signal_date = _date(self.signal_date, "signal_date")
        candidates = tuple(self.candidates)
        if not decision_id:
            raise AShareBacktestError("signal requires an id")
        if any(not isinstance(item, RankedStockCandidate) for item in candidates):
            raise AShareBacktestError("candidates must be RankedStockCandidate values")
        ids = [item.instrument_id for item in candidates]
        if len(ids) != len(set(ids)):
            raise AShareBacktestError("candidate ids must be unique per signal")
        candidates = tuple(
            sorted(
                candidates,
                key=lambda item: (-item.score, -item.percentile, item.instrument_id),
            )
        )
        scope = str(self.signal_scope).strip()
        provenance = (
            self.evaluation_sha256,
            self.experiment_spec_sha256,
            self.member_ids_sha256,
            self.ranking_sha256,
        )
        if scope == FORMAL_SIGNAL_SCOPE:
            if _formal_signal_token is not _FORMAL_CLOSE_SIGNAL_TOKEN:
                raise AShareBacktestError(
                    "formal CloseSignal can only be derived from EvaluationResult"
                )
            for value, field_name in zip(
                provenance,
                (
                    "evaluation_sha256",
                    "experiment_spec_sha256",
                    "member_ids_sha256",
                    "ranking_sha256",
                ),
                strict=True,
            ):
                if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
                    raise AShareBacktestError(
                        f"formal CloseSignal {field_name} must be a SHA-256"
                    )
        elif scope == DIAGNOSTIC_SIGNAL_SCOPE:
            if any(value is not None for value in provenance):
                raise AShareBacktestError(
                    "diagnostic CloseSignal cannot claim formal provenance"
                )
        else:
            raise AShareBacktestError("unsupported CloseSignal scope")
        object.__setattr__(self, "decision_id", decision_id)
        object.__setattr__(self, "signal_date", signal_date)
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "signal_scope", scope)


@dataclass(frozen=True)
class FormalSignalBinding:
    """Per-decision membership and full Ridge-ranking content receipts."""

    decision_date: date
    member_count: int
    member_ids_sha256: str
    ranking_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "decision_date", _date(self.decision_date, "decision_date")
        )
        if type(self.member_count) is not int or self.member_count <= 0:
            raise AShareBacktestError("formal member_count must be positive")
        for field_name in ("member_ids_sha256", "ranking_sha256"):
            value = str(getattr(self, field_name)).strip()
            if _SHA256_RE.fullmatch(value) is None:
                raise AShareBacktestError(f"{field_name} must be a SHA-256")
            object.__setattr__(self, field_name, value)


@dataclass(frozen=True)
class AShareDailyBar:
    """Decision-safe daily bar and the execution gates known for that session."""

    instrument_id: str
    trading_date: date
    open_price: Decimal
    close_price: Decimal
    csi_level1_industry: str
    lot_size: int | None = None
    suspended: bool | None = None
    is_st: bool | None = None
    limit_up_locked: bool | None = None
    limit_down_locked: bool | None = None
    listing_days: int | None = None
    average_turnover_20d: Decimal | None = None
    eligibility_available_at: datetime | None = None
    eligibility_source_sha256: str | None = None

    def __post_init__(self) -> None:
        instrument_id = str(self.instrument_id).strip().upper()
        trading_date = _date(self.trading_date, "trading_date")
        open_price = _decimal(self.open_price, "open_price")
        close_price = _decimal(self.close_price, "close_price")
        industry = str(self.csi_level1_industry).strip()
        turnover = _decimal(self.average_turnover_20d, "average_turnover_20d")
        if not instrument_id or not industry:
            raise AShareBacktestError("bar identity and industry are required")
        if open_price <= ZERO or close_price <= ZERO:
            raise AShareBacktestError("open and close prices must be positive")
        if type(self.lot_size) is not int or self.lot_size <= 0:
            raise AShareBacktestError("lot_size must be a positive integer")
        if type(self.listing_days) is not int or self.listing_days < 0:
            raise AShareBacktestError("listing_days must be a non-negative integer")
        if turnover < ZERO:
            raise AShareBacktestError("average_turnover_20d must not be negative")
        for name in (
            "suspended",
            "is_st",
            "limit_up_locked",
            "limit_down_locked",
        ):
            if type(getattr(self, name)) is not bool:
                raise AShareBacktestError(f"{name} must be boolean")
        available_at = self.eligibility_available_at
        if (
            not isinstance(available_at, datetime)
            or available_at.tzinfo is None
            or available_at.utcoffset() is None
        ):
            raise AShareBacktestError(
                "eligibility_available_at must be a timezone-aware datetime"
            )
        if available_at.utcoffset() != timedelta(hours=8):
            raise AShareBacktestError(
                "eligibility_available_at must use the China +08:00 offset"
            )
        execution_cutoff = datetime.combine(trading_date, time(9, 30), CHINA_TZ)
        if available_at > execution_cutoff:
            raise AShareBacktestError(
                "eligibility fields were not available by the execution open"
            )
        source_hash = str(self.eligibility_source_sha256 or "").strip()
        if _SHA256_RE.fullmatch(source_hash) is None:
            raise AShareBacktestError(
                "eligibility_source_sha256 must bind controlled source content"
            )
        object.__setattr__(self, "instrument_id", instrument_id)
        object.__setattr__(self, "trading_date", trading_date)
        object.__setattr__(self, "open_price", open_price)
        object.__setattr__(self, "close_price", close_price)
        object.__setattr__(self, "csi_level1_industry", industry)
        object.__setattr__(self, "average_turnover_20d", turnover)
        object.__setattr__(self, "eligibility_available_at", available_at)
        object.__setattr__(self, "eligibility_source_sha256", source_hash)


@dataclass(frozen=True)
class UnmanagedExternalPosition:
    """Account risk that is observed but can never be traded or strategy-owned."""

    instrument_id: str
    quantity: int
    ownership: str = "unmanaged_external"

    def __post_init__(self) -> None:
        instrument_id = str(self.instrument_id).strip().upper()
        if not instrument_id:
            raise AShareBacktestError("external identity is required")
        if instrument_id != UNMANAGED_MIDEA_INSTRUMENT_ID:
            raise AShareBacktestError("only the frozen unmanaged Midea position is supported")
        if type(self.quantity) is not int or self.quantity != 100:
            raise AShareBacktestError("unmanaged Midea quantity is frozen to 100 shares")
        if self.ownership != "unmanaged_external":
            raise AShareBacktestError("external positions must remain unmanaged_external")
        object.__setattr__(self, "instrument_id", instrument_id)


@dataclass(frozen=True)
class AShareCostSchedule:
    commission_rate: Decimal = Decimal("0.00018")
    minimum_commission: Decimal = Decimal("5")
    sell_tax_rate: Decimal = Decimal("0.0005")
    transfer_fee_rate: Decimal = Decimal("0.00001")
    slippage_bps: Decimal = Decimal("10")
    commission_multiplier: Decimal = ONE

    def __post_init__(self) -> None:
        for name in (
            "commission_rate",
            "minimum_commission",
            "sell_tax_rate",
            "transfer_fee_rate",
            "slippage_bps",
            "commission_multiplier",
        ):
            value = _decimal(getattr(self, name), name)
            if value < ZERO:
                raise AShareBacktestError(f"{name} must not be negative")
            object.__setattr__(self, name, value)
        if self.commission_multiplier < ONE:
            raise AShareBacktestError("commission_multiplier must be at least one")

    @property
    def slippage_rate(self) -> Decimal:
        return self.slippage_bps / Decimal("10000")

    def fill_price(self, reference_open: Decimal, side: str) -> Decimal:
        if side == "BUY":
            return reference_open * (ONE + self.slippage_rate)
        if side == "SELL":
            return reference_open * (ONE - self.slippage_rate)
        raise AShareBacktestError(f"unsupported side: {side}")

    def fees(self, notional: Decimal, side: str) -> tuple[Decimal, Decimal, Decimal]:
        base_commission = max(
            notional * self.commission_rate,
            self.minimum_commission,
        )
        commission = base_commission * self.commission_multiplier
        sell_tax = notional * self.sell_tax_rate if side == "SELL" else ZERO
        transfer = notional * self.transfer_fee_rate
        return _money(commission), _money(sell_tax), _money(transfer)


BASE_COSTS = AShareCostSchedule()
STRESS_COSTS = AShareCostSchedule(
    slippage_bps=Decimal("20"),
    commission_multiplier=Decimal("2"),
)


@dataclass(frozen=True)
class AShareTop2Config:
    """Frozen 10,000 CNY experiment policy."""

    initial_cash: Decimal = Decimal("10000")
    max_positions: int = 2
    max_stock_weight: Decimal = Decimal("0.40")
    minimum_cash_weight: Decimal = Decimal("0.20")
    combined_industry_cap: Decimal = Decimal("0.45")
    entry_percentile: Decimal = Decimal("0.95")
    hold_percentile: Decimal = Decimal("0.80")
    minimum_listing_days: int = 250
    minimum_average_turnover_20d: Decimal = Decimal("100000000")
    drawdown_stop: Decimal = Decimal("0.12")
    annualized_one_way_turnover_cap: Decimal = Decimal("4")
    leverage_allowed: bool = False
    short_selling_allowed: bool = False

    _FROZEN_VALUES: ClassVar[Mapping[str, Any]] = {
        "initial_cash": Decimal("10000"),
        "max_positions": 2,
        "max_stock_weight": Decimal("0.40"),
        "minimum_cash_weight": Decimal("0.20"),
        "combined_industry_cap": Decimal("0.45"),
        "entry_percentile": Decimal("0.95"),
        "hold_percentile": Decimal("0.80"),
        "minimum_listing_days": 250,
        "minimum_average_turnover_20d": Decimal("100000000"),
        "drawdown_stop": Decimal("0.12"),
        "annualized_one_way_turnover_cap": Decimal("4"),
        "leverage_allowed": False,
        "short_selling_allowed": False,
    }

    def __post_init__(self) -> None:
        decimal_names = {
            "initial_cash",
            "max_stock_weight",
            "minimum_cash_weight",
            "combined_industry_cap",
            "entry_percentile",
            "hold_percentile",
            "minimum_average_turnover_20d",
            "drawdown_stop",
            "annualized_one_way_turnover_cap",
        }
        for name, expected in self._FROZEN_VALUES.items():
            raw = getattr(self, name)
            if name in {"max_positions", "minimum_listing_days"} and type(raw) is not int:
                raise AShareBacktestError(f"{name} must be an integer")
            if name in {"leverage_allowed", "short_selling_allowed"} and type(raw) is not bool:
                raise AShareBacktestError(f"{name} must be boolean")
            actual = _decimal(raw, name) if name in decimal_names else raw
            if actual != expected:
                raise AShareBacktestError(f"{name} is frozen to {expected}")
            if name in decimal_names:
                object.__setattr__(self, name, actual)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": AS_SHARE_BACKTEST_VERSION,
            **{name: getattr(self, name) for name in self._FROZEN_VALUES},
            "base_costs": _cost_dict(BASE_COSTS),
            "stress_costs": _cost_dict(STRESS_COSTS),
        }

    @property
    def configuration_sha256(self) -> str:
        return _content_hash(self.to_dict())


@dataclass(frozen=True)
class AShareTrade:
    trading_date: date
    decision_id: str
    instrument_id: str
    side: str
    quantity: int
    reference_open: Decimal
    fill_price: Decimal
    notional: Decimal
    commission: Decimal
    sell_tax: Decimal
    transfer_fee: Decimal
    slippage_cost: Decimal
    scenario: str


@dataclass(frozen=True)
class AShareEvent:
    trading_date: date
    code: str
    instrument_id: str = ""
    detail: str = ""


@dataclass(frozen=True)
class AShareNavPoint:
    trading_date: date
    cash: Decimal
    strategy_positions_value: Decimal
    strategy_nav: Decimal
    external_value: Decimal
    combined_account_value: Decimal
    drawdown: Decimal
    risk_off: bool


@dataclass(frozen=True)
class PolicyGateResult:
    gate_id: str
    passed: bool
    observed: str
    limit: str

    def __post_init__(self) -> None:
        if not str(self.gate_id).strip():
            raise AShareBacktestError("gate_id is required")
        if type(self.passed) is not bool:
            raise AShareBacktestError("gate passed must be boolean")


@dataclass(frozen=True)
class AShareScenarioResult:
    scenario: str
    start_date: date
    end_date: date
    decision_dates: tuple[date, ...]
    configuration_sha256: str
    nav: tuple[AShareNavPoint, ...]
    trades: tuple[AShareTrade, ...]
    events: tuple[AShareEvent, ...]
    final_positions: Mapping[str, int]
    net_return: Decimal
    benchmark_id: str
    benchmark_total_return: Decimal
    net_active_return: Decimal
    benchmark_data_sha256: str
    max_drawdown: Decimal
    annualized_one_way_turnover: Decimal
    total_cost: Decimal
    gate_results: tuple[PolicyGateResult, ...]
    result_sha256: str


@dataclass(frozen=True)
class HistoricalGateResult:
    """Structured historical gates consumed by ``admission``."""

    start_date: date
    end_date: date
    decision_dates: tuple[date, ...]
    configuration_hashes: tuple[str, ...]
    max_drawdown: Decimal
    annualized_one_way_turnover: Decimal
    gate_results: tuple[PolicyGateResult, ...]
    base_result_sha256: str
    stress_result_sha256: str
    backtest_sha256: str
    top_decile_result_sha256: str
    evaluation_sha256: str
    choice_receipt_sha256: str
    aggregate_sha256: str


@dataclass(frozen=True)
class AShareBacktestComparison:
    base: AShareScenarioResult
    stress: AShareScenarioResult
    input_sha256: str
    backtest_sha256: str
    research_scope: str = DIAGNOSTIC_SIGNAL_SCOPE
    formal_signal_binding: bool = False
    formal_signal_bindings: tuple[FormalSignalBinding, ...] = ()
    evaluation_sha256: str | None = None
    experiment_spec_sha256: str | None = None
    evaluation_source_bundle_sha256: str | None = None
    trading_calendar_sha256: str | None = None
    benchmark_series_sha256: str | None = None
    execution_calendar_sha256: str | None = None
    formal_window_start: date | None = None
    formal_window_end: date | None = None
    unmanaged_external_sha256: str | None = None
    controlled_execution_bar_adapter_verified: bool = field(
        default=CONTROLLED_EXECUTION_BAR_ADAPTER_VERIFIED,
        init=False,
    )


@dataclass
class _Position:
    instrument_id: str
    industry: str
    quantity: int
    acquired_on: date


def _cost_dict(costs: AShareCostSchedule) -> dict[str, Any]:
    return {
        "commission_rate": costs.commission_rate,
        "minimum_commission": costs.minimum_commission,
        "sell_tax_rate": costs.sell_tax_rate,
        "transfer_fee_rate": costs.transfer_fee_rate,
        "slippage_bps": costs.slippage_bps,
        "commission_multiplier": costs.commission_multiplier,
    }


def _bar_payload(bar: AShareDailyBar) -> dict[str, Any]:
    return {
        "instrument_id": bar.instrument_id,
        "trading_date": bar.trading_date,
        "open_price": bar.open_price,
        "close_price": bar.close_price,
        "industry": bar.csi_level1_industry,
        "lot_size": bar.lot_size,
        "suspended": bar.suspended,
        "is_st": bar.is_st,
        "limit_up_locked": bar.limit_up_locked,
        "limit_down_locked": bar.limit_down_locked,
        "listing_days": bar.listing_days,
        "average_turnover_20d": bar.average_turnover_20d,
        "eligibility_available_at": bar.eligibility_available_at,
        "eligibility_source_sha256": bar.eligibility_source_sha256,
    }


def _formal_ranking_rows(
    predictions: Sequence[OOSPrediction],
) -> tuple[dict[str, Any], ...]:
    ordered = tuple(
        sorted(
            predictions,
            key=lambda item: (-item.prediction, item.instrument_id),
        )
    )
    count = len(ordered)
    if count <= 0:
        raise AShareBacktestError("formal Ridge ranking cannot be empty")
    denominator = Decimal(max(1, count - 1))
    rows: list[dict[str, Any]] = []
    for rank_index, item in enumerate(ordered):
        # With N names, this endpoint-inclusive percentile makes exactly the
        # first ceil(5% * N) names exceed the 0.95 entry boundary for the
        # frozen 20-name fixture, and the first 20% exceed the 0.80 hold band.
        percentile = (
            ONE
            if count == 1
            else Decimal(count - 1 - rank_index) / denominator
        )
        rows.append(
            {
                "instrument_id": item.instrument_id,
                "prediction": Decimal(str(item.prediction)),
                "rank": rank_index + 1,
                "percentile": percentile,
            }
        )
    return tuple(rows)


def formal_signal_bindings_from_evaluation(
    evaluation_result: EvaluationResult,
) -> tuple[FormalSignalBinding, ...]:
    """Recompute complete locked-test membership/ranking receipts.

    Both formally produced OOS models must cover every member of every
    prepared locked-test cross-section.  A successful financial or
    non-financial submodel subset is never accepted as a complete ranking.
    """

    if not isinstance(evaluation_result, EvaluationResult):
        raise AShareBacktestError(
            "formal Top2 signals require an EvaluationResult"
        )
    prepared = tuple(
        item
        for item in evaluation_result.prepared_panel.cross_sections
        if item.split == FORMAL_SPLIT
    )
    if not prepared:
        raise AShareBacktestError("locked-test prepared cross-sections are missing")
    decision_dates = tuple(item.decision_at.date() for item in prepared)
    if tuple(sorted(decision_dates)) != decision_dates or len(set(decision_dates)) != len(
        decision_dates
    ):
        raise AShareBacktestError(
            "locked-test prepared decisions must be unique and chronological"
        )

    expected_dates = set(decision_dates)
    grouped: dict[tuple[date, str], list[OOSPrediction]] = {}
    for item in evaluation_result.predictions:
        if item.split != FORMAL_SPLIT or item.model not in FORMAL_PREDICTION_MODELS:
            continue
        grouped.setdefault((item.decision_date, item.model), []).append(item)
    for model in FORMAL_PREDICTION_MODELS:
        model_dates = {
            decision_date
            for decision_date, observed_model in grouped
            if observed_model == model
        }
        if model_dates != expected_dates:
            raise AShareBacktestError(
                f"{model} predictions must cover every locked-test decision"
            )

    bindings: list[FormalSignalBinding] = []
    for section in prepared:
        if not isinstance(section, PreparedCrossSection):
            raise AShareBacktestError("prepared panel contains an invalid row")
        decision_date = section.decision_at.date()
        members = tuple(item.instrument_id for item in section.observations)
        if not members or len(members) != len(set(members)):
            raise AShareBacktestError("prepared member ids must be non-empty and unique")
        expected_members = set(members)
        for model in FORMAL_PREDICTION_MODELS:
            rows = tuple(grouped[(decision_date, model)])
            observed_ids = tuple(item.instrument_id for item in rows)
            if len(observed_ids) != len(set(observed_ids)) or set(observed_ids) != (
                expected_members
            ):
                raise AShareBacktestError(
                    f"{model} prediction ids must exactly match prepared members "
                    f"for {decision_date}"
                )
            for item in rows:
                if (
                    item.label_start_date != section.label_start_date
                    or item.label_end_date != section.label_end_date
                    or item.return_basis != section.return_basis
                ):
                    raise AShareBacktestError(
                        "formal prediction label contract differs from its prepared row"
                    )
        ridge_rows = tuple(grouped[(decision_date, PRIMARY_MODEL)])
        ranking_rows = _formal_ranking_rows(ridge_rows)
        bindings.append(
            FormalSignalBinding(
                decision_date=decision_date,
                member_count=len(members),
                member_ids_sha256=_content_hash(tuple(sorted(members))),
                ranking_sha256=_content_hash(ranking_rows),
            )
        )
    return tuple(bindings)


def derive_formal_a_share_top2_close_signals(
    evaluation_result: EvaluationResult,
    bars: Sequence[AShareDailyBar],
) -> tuple[CloseSignal, ...]:
    """Derive full locked-test Ridge rankings; callers cannot mark raw signals formal."""

    bindings = formal_signal_bindings_from_evaluation(evaluation_result)
    evaluation_hash = evaluation_result_content_sha256(evaluation_result)
    bar_index: dict[tuple[date, str], AShareDailyBar] = {}
    for bar in tuple(bars):
        if not isinstance(bar, AShareDailyBar):
            raise AShareBacktestError("bars must contain AShareDailyBar values")
        key = (bar.trading_date, bar.instrument_id)
        if key in bar_index:
            raise AShareBacktestError("duplicate instrument/date bar")
        bar_index[key] = bar

    ridge_by_date: dict[date, list[OOSPrediction]] = {}
    for item in evaluation_result.predictions:
        if item.split == FORMAL_SPLIT and item.model == PRIMARY_MODEL:
            ridge_by_date.setdefault(item.decision_date, []).append(item)
    signals: list[CloseSignal] = []
    for binding in bindings:
        ranking_rows = _formal_ranking_rows(
            ridge_by_date[binding.decision_date]
        )
        if _content_hash(ranking_rows) != binding.ranking_sha256:
            raise AssertionError("formal ranking receipt drifted during signal derivation")
        candidates: list[RankedStockCandidate] = []
        for row in ranking_rows:
            key = (binding.decision_date, str(row["instrument_id"]))
            bar = bar_index.get(key)
            if bar is None:
                raise AShareBacktestError(
                    "every formal member requires a decision-date PIT industry bar"
                )
            candidates.append(
                RankedStockCandidate(
                    instrument_id=bar.instrument_id,
                    csi_level1_industry=bar.csi_level1_industry,
                    score=Decimal(str(row["prediction"])),
                    percentile=Decimal(str(row["percentile"])),
                    manual_veto=False,
                )
            )
        signals.append(
            CloseSignal(
                decision_id=f"formal-ridge-locked-test:{binding.decision_date.isoformat()}",
                signal_date=binding.decision_date,
                candidates=tuple(candidates),
                signal_scope=FORMAL_SIGNAL_SCOPE,
                evaluation_sha256=evaluation_hash,
                experiment_spec_sha256=evaluation_result.experiment_spec_sha256,
                member_ids_sha256=binding.member_ids_sha256,
                ranking_sha256=binding.ranking_sha256,
                _formal_signal_token=_FORMAL_CLOSE_SIGNAL_TOKEN,
            )
        )
    return tuple(signals)


def _benchmark_payload(bar: BenchmarkTotalReturnBar) -> dict[str, Any]:
    return {
        "benchmark_id": bar.benchmark_id,
        "trading_date": bar.trading_date,
        "open_level": bar.open_level,
        "close_level": bar.close_level,
        "available_at": bar.available_at,
        "source_sha256": bar.source_sha256,
    }


def _validate_benchmark(
    benchmark_bars: Sequence[BenchmarkTotalReturnBar],
    controlled_trading_dates: Sequence[date],
    *,
    return_start_date: date | None = None,
    return_end_date: date | None = None,
    return_basis: str = "close_to_close",
) -> tuple[str, Decimal, str, tuple[BenchmarkTotalReturnBar, ...]]:
    bars = tuple(benchmark_bars)
    if not bars or any(not isinstance(item, BenchmarkTotalReturnBar) for item in bars):
        raise AShareBacktestError(
            "benchmark_bars must contain controlled BenchmarkTotalReturnBar values"
        )
    dates = tuple(controlled_trading_dates)
    by_date: dict[date, BenchmarkTotalReturnBar] = {}
    for item in bars:
        if item.trading_date in by_date:
            raise AShareBacktestError("duplicate CSI800 total-return benchmark date")
        by_date[item.trading_date] = item
    if set(by_date) != set(dates) or len(bars) != len(dates):
        raise AShareBacktestError(
            "CSI800 total-return benchmark must match the exact controlled calendar"
        )
    ids = {item.benchmark_id for item in bars}
    if len(ids) != 1:
        raise AShareBacktestError("benchmark subject must remain constant")
    normalized = tuple(by_date[item] for item in dates)
    start_date = return_start_date or dates[0]
    end_date = return_end_date or dates[-1]
    if start_date not in by_date or end_date not in by_date or start_date > end_date:
        raise AShareBacktestError("benchmark return window is outside the controlled calendar")
    if return_basis == "close_to_close":
        start_level = by_date[start_date].close_level
        end_level = by_date[end_date].close_level
    elif return_basis == "open_to_open":
        start_level = by_date[start_date].open_level
        end_level = by_date[end_date].open_level
    else:
        raise AShareBacktestError("unsupported benchmark return basis")
    benchmark_return = end_level / start_level - ONE
    benchmark_hash = _content_hash(
        [_benchmark_payload(item) for item in normalized]
    )
    return next(iter(ids)), benchmark_return, benchmark_hash, normalized


def _signal_payload(signal: CloseSignal) -> dict[str, Any]:
    return {
        "decision_id": signal.decision_id,
        "signal_date": signal.signal_date,
        "signal_scope": signal.signal_scope,
        "evaluation_sha256": signal.evaluation_sha256,
        "experiment_spec_sha256": signal.experiment_spec_sha256,
        "member_ids_sha256": signal.member_ids_sha256,
        "ranking_sha256": signal.ranking_sha256,
        "candidates": [
            {
                "instrument_id": item.instrument_id,
                "industry": item.csi_level1_industry,
                "score": item.score,
                "percentile": item.percentile,
                "manual_veto": item.manual_veto,
            }
            for item in signal.candidates
        ],
    }


def _trade_payload(trade: AShareTrade) -> dict[str, Any]:
    return {
        name: getattr(trade, name)
        for name in trade.__dataclass_fields__
    }


def _new_buy_block(bar: AShareDailyBar, config: AShareTop2Config) -> str | None:
    if bar.suspended:
        return "new_buy_blocked_suspended"
    if bar.is_st:
        return "new_buy_blocked_st"
    if bar.limit_up_locked or bar.limit_down_locked:
        return "new_buy_blocked_limit_locked"
    if bar.listing_days < config.minimum_listing_days:
        return "new_buy_blocked_listing_age"
    if bar.average_turnover_20d < config.minimum_average_turnover_20d:
        return "new_buy_blocked_liquidity"
    return None


def _combined_industry_weights(
    *,
    cash: Decimal,
    positions: Mapping[str, _Position],
    prices: Mapping[str, Decimal],
    external: Sequence[UnmanagedExternalPosition],
    external_prices: Mapping[str, Decimal],
    external_industries: Mapping[str, str],
) -> Mapping[str, Decimal]:
    values: dict[str, Decimal] = {}
    strategy_positions = ZERO
    for instrument_id, position in positions.items():
        value = prices[instrument_id] * position.quantity
        strategy_positions += value
        values[position.industry] = values.get(position.industry, ZERO) + value
    external_total = ZERO
    for item in external:
        if item.instrument_id not in external_prices:
            raise AShareBacktestError("unmanaged Midea requires a daily mark")
        if item.instrument_id not in external_industries:
            raise AShareBacktestError("unmanaged Midea requires a daily PIT industry")
        market_value = external_prices[item.instrument_id] * item.quantity
        external_total += market_value
        industry = external_industries[item.instrument_id]
        values[industry] = (
            values.get(industry, ZERO) + market_value
        )
    total = cash + strategy_positions + external_total
    if total <= ZERO:
        raise AShareBacktestError("combined account value must remain positive")
    return {industry: value / total for industry, value in values.items()}


def _sell(
    *,
    trading_date: date,
    decision_id: str,
    position: _Position,
    quantity: int,
    bar: AShareDailyBar,
    cash: Decimal,
    costs: AShareCostSchedule,
    scenario: str,
) -> tuple[Decimal, AShareTrade | None, str | None]:
    if quantity <= 0:
        return cash, None, None
    if position.acquired_on >= trading_date:
        return cash, None, "sell_blocked_t_plus_one"
    if bar.suspended:
        return cash, None, "sell_blocked_suspended"
    if bar.limit_down_locked:
        return cash, None, "sell_blocked_limit_down"
    quantity = min(quantity, position.quantity)
    fill = costs.fill_price(bar.open_price, "SELL")
    notional = _money(fill * quantity)
    commission, sell_tax, transfer = costs.fees(notional, "SELL")
    slippage = _money((bar.open_price - fill) * quantity)
    cash = _money(cash + notional - commission - sell_tax - transfer)
    return cash, AShareTrade(
        trading_date=trading_date,
        decision_id=decision_id,
        instrument_id=position.instrument_id,
        side="SELL",
        quantity=quantity,
        reference_open=bar.open_price,
        fill_price=fill,
        notional=notional,
        commission=commission,
        sell_tax=sell_tax,
        transfer_fee=transfer,
        slippage_cost=slippage,
        scenario=scenario,
    ), None


def _affordable_buy_quantity(
    *,
    bar: AShareDailyBar,
    strategy_nav: Decimal,
    cash: Decimal,
    current_quantity: int,
    costs: AShareCostSchedule,
    config: AShareTop2Config,
) -> int:
    target_value = strategy_nav * config.max_stock_weight
    current_value = bar.open_price * current_quantity
    remaining_target = max(ZERO, target_value - current_value)
    quantity = int(
        (remaining_target / (bar.open_price * bar.lot_size)).to_integral_value(
            rounding=ROUND_DOWN
        )
    ) * bar.lot_size
    minimum_cash = strategy_nav * config.minimum_cash_weight
    fill = costs.fill_price(bar.open_price, "BUY")
    while quantity > 0:
        notional = _money(fill * quantity)
        commission, _, transfer = costs.fees(notional, "BUY")
        slippage = (fill - bar.open_price) * quantity
        nav_after_cost = strategy_nav - commission - transfer - slippage
        if (
            cash - notional - commission - transfer
            >= nav_after_cost * config.minimum_cash_weight
            and bar.open_price * (current_quantity + quantity)
            <= nav_after_cost * config.max_stock_weight
        ):
            return quantity
        quantity -= bar.lot_size
    return 0


def _scenario_result(
    signals: Sequence[CloseSignal],
    bars: Sequence[AShareDailyBar],
    controlled_trading_dates: Sequence[date],
    external: Sequence[UnmanagedExternalPosition],
    config: AShareTop2Config,
    costs: AShareCostSchedule,
    scenario: str,
    benchmark_id: str,
    benchmark_total_return: Decimal,
    benchmark_data_sha256: str,
    reported_start_date: date | None = None,
    reported_end_date: date | None = None,
) -> AShareScenarioResult:
    bar_index: dict[tuple[date, str], AShareDailyBar] = {}
    bar_dates: set[date] = set()
    for bar in bars:
        if not isinstance(bar, AShareDailyBar):
            raise AShareBacktestError("bars must contain AShareDailyBar values")
        key = (bar.trading_date, bar.instrument_id)
        if key in bar_index:
            raise AShareBacktestError("duplicate instrument/date bar")
        bar_index[key] = bar
        bar_dates.add(bar.trading_date)
    dates = tuple(_date(item, "controlled trading date") for item in controlled_trading_dates)
    if tuple(sorted(dates)) != dates or len(set(dates)) != len(dates):
        raise AShareBacktestError(
            "controlled trading dates must be unique and strictly chronological"
        )
    if len(dates) < 2:
        raise AShareBacktestError("at least two trading sessions are required")
    if bar_dates != set(dates):
        raise AShareBacktestError(
            "daily bars must match the exact controlled trading calendar"
        )
    next_date = {dates[index]: dates[index + 1] for index in range(len(dates) - 1)}
    signals_by_execution: dict[date, CloseSignal] = {}
    decision_dates: list[date] = []
    seen_decisions: set[str] = set()
    for signal in signals:
        if not isinstance(signal, CloseSignal):
            raise AShareBacktestError("signals must contain CloseSignal values")
        if signal.decision_id in seen_decisions:
            raise AShareBacktestError("decision ids must be unique")
        seen_decisions.add(signal.decision_id)
        if signal.signal_date not in next_date:
            raise AShareBacktestError("every signal needs a controlled next session")
        execution_date = next_date[signal.signal_date]
        if execution_date in signals_by_execution:
            raise AShareBacktestError("only one signal may execute per session")
        signals_by_execution[execution_date] = signal
        decision_dates.append(signal.signal_date)
    if decision_dates != sorted(decision_dates):
        raise AShareBacktestError("signals must be strictly chronological")
    calendar_index = {trading_date: index for index, trading_date in enumerate(dates)}
    for previous, current in zip(decision_dates, decision_dates[1:]):
        if calendar_index[current] - calendar_index[previous] != 20:
            raise AShareBacktestError(
                "adjacent decision points must be exactly 20 controlled sessions apart"
            )

    external_ids = {item.instrument_id for item in external}
    if len(external_ids) != len(tuple(external)):
        raise AShareBacktestError("external position ids must be unique")
    if external_ids != {UNMANAGED_MIDEA_INSTRUMENT_ID}:
        raise AShareBacktestError(
            "unmanaged_external must contain the frozen 100-share Midea position"
        )
    cash = config.initial_cash
    positions: dict[str, _Position] = {}
    last_close: dict[str, Decimal] = {}
    nav_points: list[AShareNavPoint] = []
    trades: list[AShareTrade] = []
    events: list[AShareEvent] = []
    total_cost = ZERO
    peak_nav = config.initial_cash
    max_drawdown = ZERO
    risk_off = False
    liquidation_pending = False
    invariant_breached = False
    combined_industry_breached = False

    for trading_date in dates:
        day_bars = {
            instrument_id: bar
            for (bar_date, instrument_id), bar in bar_index.items()
            if bar_date == trading_date
        }
        # Refresh held-name industry from the current PIT bar.  Purchase-date
        # classifications cannot drive today's distinct-industry/cap checks.
        for instrument_id, position in positions.items():
            bar = day_bars.get(instrument_id)
            if bar is None:
                raise AShareBacktestError("held instruments require a daily bar")
            position.industry = bar.csi_level1_industry
        for instrument_id, bar in day_bars.items():
            last_close[instrument_id] = bar.close_price
        external_open_prices: dict[str, Decimal] = {}
        external_close_prices: dict[str, Decimal] = {}
        external_industries: dict[str, str] = {}
        for item in external:
            bar = day_bars.get(item.instrument_id)
            if bar is None:
                raise AShareBacktestError("unmanaged Midea requires a daily bar")
            external_open_prices[item.instrument_id] = bar.open_price
            external_close_prices[item.instrument_id] = bar.close_price
            external_industries[item.instrument_id] = bar.csi_level1_industry

        signal = signals_by_execution.get(trading_date)
        if signal is not None and risk_off and not liquidation_pending:
            liquidation_pending = bool(positions)
            events.append(
                AShareEvent(
                    trading_date,
                    "drawdown_cash_target_activated_at_decision",
                    detail=signal.decision_id,
                )
            )
        if liquidation_pending:
            for instrument_id in sorted(tuple(positions)):
                position = positions[instrument_id]
                bar = day_bars.get(instrument_id)
                if bar is None:
                    raise AShareBacktestError("held instruments require a daily bar")
                cash, trade, blocked = _sell(
                    trading_date=trading_date,
                    decision_id="drawdown_cash_target",
                    position=position,
                    quantity=position.quantity,
                    bar=bar,
                    cash=cash,
                    costs=costs,
                    scenario=scenario,
                )
                if blocked:
                    events.append(AShareEvent(trading_date, blocked, instrument_id))
                    continue
                assert trade is not None
                trades.append(trade)
                total_cost += trade.commission + trade.sell_tax + trade.transfer_fee + trade.slippage_cost
                del positions[instrument_id]
            liquidation_pending = bool(positions)

        if signal is not None:
            if risk_off:
                events.append(
                    AShareEvent(trading_date, "new_buys_stopped_drawdown", detail=signal.decision_id)
                )
            else:
                candidate_by_id = {item.instrument_id: item for item in signal.candidates}
                retained: list[str] = []
                vetoed_ids: set[str] = set()
                used_industries: set[str] = set()
                # Incumbents get the wider hold band, but a veto consumes the slot.
                for instrument_id in sorted(
                    positions,
                    key=lambda value: (
                        -candidate_by_id.get(value, RankedStockCandidate(value, positions[value].industry, ZERO, ZERO)).score,
                        value,
                    ),
                ):
                    candidate = candidate_by_id.get(instrument_id)
                    if candidate is None or candidate.score <= ZERO or candidate.percentile < config.hold_percentile:
                        continue
                    bar = day_bars.get(instrument_id)
                    if bar is None:
                        raise AShareBacktestError(
                            "held instruments require an execution-day bar"
                        )
                    if bar.csi_level1_industry != candidate.csi_level1_industry:
                        raise AShareBacktestError("signal/bar industry mismatch")
                    eligibility_block = _new_buy_block(bar, config)
                    if eligibility_block is not None:
                        events.append(
                            AShareEvent(
                                trading_date,
                                "hold_eligibility_failed",
                                instrument_id,
                                eligibility_block,
                            )
                        )
                        continue
                    if candidate.manual_veto:
                        vetoed_ids.add(instrument_id)
                        events.append(AShareEvent(trading_date, "manual_veto_cash_not_replaced", instrument_id))
                        continue
                    if candidate.csi_level1_industry in used_industries:
                        continue
                    retained.append(instrument_id)
                    used_industries.add(candidate.csi_level1_industry)

                # Unwanted/excess holdings are offered for sale first.  A blocked
                # exit remains real exposure and constrains every later buy.
                for instrument_id in sorted(tuple(positions)):
                    position = positions[instrument_id]
                    bar = day_bars.get(instrument_id)
                    if bar is None:
                        raise AShareBacktestError("held instruments require a daily bar")
                    open_nav = cash + sum(
                        day_bars[item].open_price * held.quantity
                        for item, held in positions.items()
                        if item in day_bars
                    )
                    desired_quantity = 0
                    if instrument_id in retained:
                        cap_value = open_nav * config.max_stock_weight
                        desired_quantity = int(
                            (cap_value / (bar.open_price * bar.lot_size)).to_integral_value(
                                rounding=ROUND_DOWN
                            )
                        ) * bar.lot_size
                    sell_quantity = max(0, position.quantity - desired_quantity)
                    cash, trade, blocked = _sell(
                        trading_date=trading_date,
                        decision_id=signal.decision_id,
                        position=position,
                        quantity=sell_quantity,
                        bar=bar,
                        cash=cash,
                        costs=costs,
                        scenario=scenario,
                    )
                    if blocked:
                        events.append(AShareEvent(trading_date, blocked, instrument_id))
                        continue
                    if trade is not None:
                        trades.append(trade)
                        total_cost += trade.commission + trade.sell_tax + trade.transfer_fee + trade.slippage_cost
                        position.quantity -= trade.quantity
                        if position.quantity == 0:
                            del positions[instrument_id]

                # A sell blocked by T+1, suspension, or a locked limit-down is
                # still a real position and therefore consumes a slot.
                occupied_slots = len(positions) + len(vetoed_ids - set(positions))
                for candidate in signal.candidates:
                    if occupied_slots >= config.max_positions:
                        break
                    if candidate.instrument_id in retained or candidate.instrument_id in positions:
                        continue
                    if candidate.instrument_id in vetoed_ids:
                        continue
                    if candidate.score <= ZERO or candidate.percentile < config.entry_percentile:
                        continue
                    if candidate.csi_level1_industry in {item.industry for item in positions.values()}:
                        events.append(AShareEvent(trading_date, "candidate_skipped_same_industry", candidate.instrument_id))
                        continue
                    if candidate.manual_veto:
                        occupied_slots += 1
                        events.append(AShareEvent(trading_date, "manual_veto_cash_not_replaced", candidate.instrument_id))
                        continue
                    if (
                        candidate.instrument_id == UNMANAGED_MIDEA_INSTRUMENT_ID
                        or candidate.instrument_id in external_ids
                    ):
                        events.append(AShareEvent(trading_date, "unmanaged_external_not_tradeable", candidate.instrument_id))
                        continue
                    bar = day_bars.get(candidate.instrument_id)
                    if bar is None:
                        events.append(AShareEvent(trading_date, "new_buy_blocked_missing_bar", candidate.instrument_id))
                        continue
                    if bar.csi_level1_industry != candidate.csi_level1_industry:
                        raise AShareBacktestError("signal/bar industry mismatch")
                    blocked = _new_buy_block(bar, config)
                    if blocked:
                        events.append(AShareEvent(trading_date, blocked, candidate.instrument_id))
                        continue
                    open_prices = {item: day_bars[item].open_price for item in positions}
                    open_nav = cash + sum(
                        open_prices[item] * position.quantity
                        for item, position in positions.items()
                    )
                    quantity = _affordable_buy_quantity(
                        bar=bar,
                        strategy_nav=open_nav,
                        cash=cash,
                        current_quantity=0,
                        costs=costs,
                        config=config,
                    )
                    # Reduce by lots until the combined account industry cap is met.
                    while quantity > 0:
                        fill = costs.fill_price(bar.open_price, "BUY")
                        notional = _money(fill * quantity)
                        commission, _, transfer = costs.fees(notional, "BUY")
                        trial_cash = cash - notional - commission - transfer
                        trial_positions = dict(positions)
                        trial_positions[candidate.instrument_id] = _Position(
                            candidate.instrument_id,
                            candidate.csi_level1_industry,
                            quantity,
                            trading_date,
                        )
                        trial_prices = dict(open_prices)
                        trial_prices[candidate.instrument_id] = bar.open_price
                        weights = _combined_industry_weights(
                            cash=trial_cash,
                            positions=trial_positions,
                            prices=trial_prices,
                            external=external,
                            external_prices=external_open_prices,
                            external_industries=external_industries,
                        )
                        if (
                            weights.get(candidate.csi_level1_industry, ZERO)
                            <= config.combined_industry_cap
                        ):
                            break
                        quantity -= bar.lot_size
                    if quantity <= 0:
                        events.append(AShareEvent(trading_date, "budget_or_industry_skip_next", candidate.instrument_id))
                        continue
                    fill = costs.fill_price(bar.open_price, "BUY")
                    notional = _money(fill * quantity)
                    commission, _, transfer = costs.fees(notional, "BUY")
                    slippage = _money((fill - bar.open_price) * quantity)
                    cash = _money(cash - notional - commission - transfer)
                    trade = AShareTrade(
                        trading_date=trading_date,
                        decision_id=signal.decision_id,
                        instrument_id=candidate.instrument_id,
                        side="BUY",
                        quantity=quantity,
                        reference_open=bar.open_price,
                        fill_price=fill,
                        notional=notional,
                        commission=commission,
                        sell_tax=ZERO,
                        transfer_fee=transfer,
                        slippage_cost=slippage,
                        scenario=scenario,
                    )
                    trades.append(trade)
                    total_cost += commission + transfer + slippage
                    positions[candidate.instrument_id] = _Position(
                        candidate.instrument_id,
                        candidate.csi_level1_industry,
                        quantity,
                        trading_date,
                    )
                    retained.append(candidate.instrument_id)
                    occupied_slots += 1

                # Top up retained names only after exits; they still face every
                # new-buy market and account-risk gate.
                for instrument_id in retained:
                    position = positions.get(instrument_id)
                    if position is None:
                        continue
                    bar = day_bars[instrument_id]
                    blocked = _new_buy_block(bar, config)
                    if blocked:
                        continue
                    open_nav = cash + sum(
                        day_bars[item].open_price * held.quantity
                        for item, held in positions.items()
                    )
                    quantity = _affordable_buy_quantity(
                        bar=bar,
                        strategy_nav=open_nav,
                        cash=cash,
                        current_quantity=position.quantity,
                        costs=costs,
                        config=config,
                    )
                    if quantity <= 0:
                        continue
                    open_prices = {
                        item: day_bars[item].open_price for item in positions
                    }
                    while quantity > 0:
                        fill = costs.fill_price(bar.open_price, "BUY")
                        notional = _money(fill * quantity)
                        commission, _, transfer = costs.fees(notional, "BUY")
                        trial_positions = dict(positions)
                        trial_positions[instrument_id] = _Position(
                            instrument_id,
                            position.industry,
                            position.quantity + quantity,
                            position.acquired_on,
                        )
                        weights = _combined_industry_weights(
                            cash=cash - notional - commission - transfer,
                            positions=trial_positions,
                            prices=open_prices,
                            external=external,
                            external_prices=external_open_prices,
                            external_industries=external_industries,
                        )
                        if (
                            weights.get(position.industry, ZERO)
                            <= config.combined_industry_cap
                        ):
                            break
                        quantity -= bar.lot_size
                    if quantity <= 0:
                        continue
                    fill = costs.fill_price(bar.open_price, "BUY")
                    notional = _money(fill * quantity)
                    commission, _, transfer = costs.fees(notional, "BUY")
                    cash = _money(cash - notional - commission - transfer)
                    slippage = _money((fill - bar.open_price) * quantity)
                    trades.append(AShareTrade(
                        trading_date, signal.decision_id, instrument_id, "BUY", quantity,
                        bar.open_price, fill, notional, commission, ZERO, transfer,
                        slippage, scenario,
                    ))
                    total_cost += commission + transfer + slippage
                    position.quantity += quantity

        strategy_positions_value = ZERO
        close_prices: dict[str, Decimal] = {}
        for instrument_id, position in positions.items():
            bar = day_bars.get(instrument_id)
            if bar is None:
                raise AShareBacktestError("held instruments require a daily bar")
            close_prices[instrument_id] = bar.close_price
            strategy_positions_value += bar.close_price * position.quantity
        strategy_nav = _money(cash + strategy_positions_value)
        if cash < ZERO or any(item.quantity < 0 for item in positions.values()):
            raise AShareBacktestError("leverage and short positions are forbidden")
        peak_nav = max(peak_nav, strategy_nav)
        drawdown = (peak_nav - strategy_nav) / peak_nav
        max_drawdown = max(max_drawdown, drawdown)
        if drawdown >= config.drawdown_stop and not risk_off:
            risk_off = True
            events.append(AShareEvent(trading_date, "drawdown_stop_latched", detail=str(drawdown)))
        external_value = sum(
            (
                external_close_prices[item.instrument_id] * item.quantity
                for item in external
            ),
            ZERO,
        )
        combined_weights = _combined_industry_weights(
            cash=cash,
            positions=positions,
            prices=close_prices,
            external=external,
            external_prices=external_close_prices,
            external_industries=external_industries,
        )
        strategy_industries = {item.industry for item in positions.values()}
        for industry, weight in combined_weights.items():
            if weight <= config.combined_industry_cap:
                continue
            if industry in strategy_industries:
                combined_industry_breached = True
                events.append(
                    AShareEvent(
                        trading_date,
                        "strategy_added_combined_industry_cap_breach",
                        detail=industry,
                    )
                )
            else:
                events.append(
                    AShareEvent(
                        trading_date,
                        "unmanaged_external_industry_over_cap",
                        detail=industry,
                    )
                )
        position_weights = [
            close_prices[item] * position.quantity / strategy_nav
            for item, position in positions.items()
        ]
        cash_weight = cash / strategy_nav
        industries = [item.industry for item in positions.values()]
        if (
            len(positions) > config.max_positions
            or any(weight > config.max_stock_weight for weight in position_weights)
            or cash_weight < config.minimum_cash_weight
            or len(industries) != len(set(industries))
        ):
            invariant_breached = True
        nav_points.append(AShareNavPoint(
            trading_date=trading_date,
            cash=_money(cash),
            strategy_positions_value=_money(strategy_positions_value),
            strategy_nav=strategy_nav,
            external_value=_money(external_value),
            combined_account_value=_money(strategy_nav + external_value),
            drawdown=drawdown.quantize(PCT),
            risk_off=risk_off,
        ))

    result_start = reported_start_date or dates[0]
    result_end = reported_end_date or dates[-1]
    if (
        result_start not in calendar_index
        or result_end not in calendar_index
        or result_start > result_end
    ):
        raise AShareBacktestError("reported result window is outside the controlled calendar")
    reported_nav = tuple(
        item
        for item in nav_points
        if result_start <= item.trading_date <= result_end
    )
    if not reported_nav:
        raise AShareBacktestError("reported result window cannot be empty")
    average_nav = sum(
        (item.strategy_nav for item in reported_nav), ZERO
    ) / len(reported_nav)
    total_notional = sum((item.notional for item in trades), ZERO)
    one_way = total_notional / (Decimal("2") * average_nav) if average_nav else ZERO
    elapsed_sessions = calendar_index[result_end] - calendar_index[result_start]
    annualized_turnover = one_way * Decimal("252") / Decimal(max(1, elapsed_sessions))
    final_nav = reported_nav[-1].strategy_nav
    net_return = final_nav / config.initial_cash - ONE
    net_active_return = net_return - benchmark_total_return
    gates = (
        PolicyGateResult("no_leverage_or_short", cash >= ZERO and all(item.quantity >= 0 for item in positions.values()), str(cash), ">=0"),
        PolicyGateResult("top2_weight_cash_industry_invariants", not invariant_breached, str(invariant_breached), "false"),
        PolicyGateResult("combined_account_industry_cap", not combined_industry_breached, str(combined_industry_breached), "<=0.45"),
        PolicyGateResult("drawdown_at_or_below_12pct", max_drawdown <= config.drawdown_stop, str(max_drawdown), "<=0.12"),
        PolicyGateResult("annualized_one_way_turnover", annualized_turnover <= config.annualized_one_way_turnover_cap, str(annualized_turnover), "<=4"),
    )
    payload = {
        "scenario": scenario,
        "start_date": result_start,
        "end_date": result_end,
        "decision_dates": decision_dates,
        "configuration_sha256": config.configuration_sha256,
        "net_return": net_return,
        "benchmark_id": benchmark_id,
        "benchmark_total_return": benchmark_total_return,
        "net_active_return": net_active_return,
        "benchmark_data_sha256": benchmark_data_sha256,
        "max_drawdown": max_drawdown,
        "annualized_one_way_turnover": annualized_turnover,
        "total_cost": total_cost,
        "nav": [
            {name: getattr(item, name) for name in item.__dataclass_fields__}
            for item in reported_nav
        ],
        "final_positions": {
            key: item.quantity for key, item in sorted(positions.items())
        },
        "trades": [_trade_payload(item) for item in trades],
        "events": [
            {name: getattr(item, name) for name in item.__dataclass_fields__}
            for item in events
        ],
        "gates": [
            {name: getattr(item, name) for name in item.__dataclass_fields__}
            for item in gates
        ],
    }
    result_hash = _content_hash(payload)
    return AShareScenarioResult(
        scenario=scenario,
        start_date=result_start,
        end_date=result_end,
        decision_dates=tuple(decision_dates),
        configuration_sha256=config.configuration_sha256,
        nav=reported_nav,
        trades=tuple(trades),
        events=tuple(events),
        final_positions={key: item.quantity for key, item in sorted(positions.items())},
        net_return=net_return.quantize(PCT),
        benchmark_id=benchmark_id,
        benchmark_total_return=benchmark_total_return.quantize(PCT),
        net_active_return=net_active_return.quantize(PCT),
        benchmark_data_sha256=benchmark_data_sha256,
        max_drawdown=max_drawdown.quantize(PCT),
        annualized_one_way_turnover=annualized_turnover.quantize(PCT),
        total_cost=_money(total_cost),
        gate_results=gates,
        result_sha256=result_hash,
    )


def _formal_binding_payload(item: FormalSignalBinding) -> dict[str, Any]:
    return {
        "decision_date": item.decision_date,
        "member_count": item.member_count,
        "member_ids_sha256": item.member_ids_sha256,
        "ranking_sha256": item.ranking_sha256,
    }


def _comparison_hash_payload(
    *,
    base: AShareScenarioResult,
    stress: AShareScenarioResult,
    input_sha256: str,
    research_scope: str,
    formal_signal_binding: bool,
    formal_signal_bindings: Sequence[FormalSignalBinding],
    evaluation_sha256: str | None,
    experiment_spec_sha256: str | None,
    evaluation_source_bundle_sha256: str | None,
    trading_calendar_sha256: str | None,
    benchmark_series_sha256: str | None,
    execution_calendar_sha256: str | None,
    formal_window_start: date | None,
    formal_window_end: date | None,
    unmanaged_external_sha256: str | None,
    controlled_execution_bar_adapter_verified: bool,
) -> dict[str, Any]:
    return {
        "engine_version": AS_SHARE_BACKTEST_VERSION,
        "configuration_sha256": base.configuration_sha256,
        "input_sha256": input_sha256,
        "base_result_sha256": base.result_sha256,
        "stress_result_sha256": stress.result_sha256,
        "benchmark_id": base.benchmark_id,
        "benchmark_data_sha256": base.benchmark_data_sha256,
        "research_scope": research_scope,
        "formal_signal_binding": formal_signal_binding,
        "formal_signal_bindings": [
            _formal_binding_payload(item) for item in formal_signal_bindings
        ],
        "evaluation_sha256": evaluation_sha256,
        "experiment_spec_sha256": experiment_spec_sha256,
        "evaluation_source_bundle_sha256": evaluation_source_bundle_sha256,
        "trading_calendar_sha256": trading_calendar_sha256,
        "benchmark_series_sha256": benchmark_series_sha256,
        "execution_calendar_sha256": execution_calendar_sha256,
        "formal_window_start": formal_window_start,
        "formal_window_end": formal_window_end,
        "unmanaged_external_sha256": unmanaged_external_sha256,
        "controlled_execution_bar_adapter_verified": (
            controlled_execution_bar_adapter_verified
        ),
    }


def a_share_backtest_comparison_content_sha256(
    result: AShareBacktestComparison,
) -> str:
    """Recompute the comparison hash, including every formal provenance field."""

    if not isinstance(result, AShareBacktestComparison):
        raise AShareBacktestError("result must be an AShareBacktestComparison")
    return _content_hash(
        _comparison_hash_payload(
            base=result.base,
            stress=result.stress,
            input_sha256=result.input_sha256,
            research_scope=result.research_scope,
            formal_signal_binding=result.formal_signal_binding,
            formal_signal_bindings=result.formal_signal_bindings,
            evaluation_sha256=result.evaluation_sha256,
            experiment_spec_sha256=result.experiment_spec_sha256,
            evaluation_source_bundle_sha256=result.evaluation_source_bundle_sha256,
            trading_calendar_sha256=result.trading_calendar_sha256,
            benchmark_series_sha256=result.benchmark_series_sha256,
            execution_calendar_sha256=result.execution_calendar_sha256,
            formal_window_start=result.formal_window_start,
            formal_window_end=result.formal_window_end,
            unmanaged_external_sha256=result.unmanaged_external_sha256,
            controlled_execution_bar_adapter_verified=(
                result.controlled_execution_bar_adapter_verified
            ),
        )
    )


def _run_a_share_top2_comparison(
    signals: Sequence[CloseSignal],
    bars: Sequence[AShareDailyBar],
    *,
    controlled_trading_dates: Sequence[date],
    benchmark_bars: Sequence[BenchmarkTotalReturnBar],
    unmanaged_external: Sequence[UnmanagedExternalPosition] = (),
    config: AShareTop2Config,
    research_scope: str,
    formal_signal_binding: bool,
    formal_signal_bindings: Sequence[FormalSignalBinding] = (),
    evaluation_sha256: str | None = None,
    experiment_spec_sha256: str | None = None,
    evaluation_source_bundle_sha256: str | None = None,
    trading_calendar_sha256: str | None = None,
    benchmark_series_sha256: str | None = None,
    reported_start_date: date | None = None,
    reported_end_date: date | None = None,
    benchmark_return_basis: str = "close_to_close",
) -> AShareBacktestComparison:
    signals = tuple(signals)
    bars = tuple(bars)
    bindings = tuple(formal_signal_bindings)
    controlled_dates = tuple(controlled_trading_dates)
    if any(
        candidate.manual_veto
        for signal in signals
        for candidate in signal.candidates
    ):
        raise AShareBacktestError(
            "historical backtests require manual_veto=false; veto is forward-Paper evidence only"
        )
    if formal_signal_binding:
        if research_scope != FORMAL_BACKTEST_SCOPE:
            raise AShareBacktestError("formal backtest scope is inconsistent")
        if any(signal.signal_scope != FORMAL_SIGNAL_SCOPE for signal in signals):
            raise AShareBacktestError("formal backtest requires derived formal signals")
        signal_receipts = tuple(
            FormalSignalBinding(
                decision_date=item.signal_date,
                member_count=len(item.candidates),
                member_ids_sha256=str(item.member_ids_sha256),
                ranking_sha256=str(item.ranking_sha256),
            )
            for item in signals
        )
        if signal_receipts != bindings:
            raise AShareBacktestError("formal signal receipts do not match derived rankings")
    elif research_scope != DIAGNOSTIC_SIGNAL_SCOPE or bindings:
        raise AShareBacktestError("diagnostic backtests cannot claim formal bindings")

    (
        benchmark_id,
        benchmark_total_return,
        benchmark_data_sha256,
        normalized_benchmark,
    ) = _validate_benchmark(
        benchmark_bars,
        controlled_dates,
        return_start_date=reported_start_date,
        return_end_date=reported_end_date,
        return_basis=benchmark_return_basis,
    )
    external = tuple(unmanaged_external)
    if any(not isinstance(item, UnmanagedExternalPosition) for item in external):
        raise AShareBacktestError("unmanaged_external values are invalid")
    external_payload = [
        {
            "instrument_id": item.instrument_id,
            "quantity": item.quantity,
            "ownership": item.ownership,
        }
        for item in external
    ]
    unmanaged_external_hash = _content_hash(external_payload)
    execution_calendar_hash = trading_calendar_content_sha256(controlled_dates)
    effective_calendar_hash = trading_calendar_sha256 or execution_calendar_hash
    input_sha256 = _content_hash({
        "signals": [_signal_payload(item) for item in signals],
        "bars": [_bar_payload(item) for item in bars],
        "controlled_trading_dates": controlled_dates,
        "benchmark_bars": [
            _benchmark_payload(item) for item in normalized_benchmark
        ],
        "unmanaged_external": external_payload,
        "research_scope": research_scope,
        "formal_signal_bindings": [
            _formal_binding_payload(item) for item in bindings
        ],
        "evaluation_sha256": evaluation_sha256,
        "experiment_spec_sha256": experiment_spec_sha256,
        "evaluation_source_bundle_sha256": evaluation_source_bundle_sha256,
        "trading_calendar_sha256": effective_calendar_hash,
        "benchmark_series_sha256": benchmark_series_sha256,
        "reported_start_date": reported_start_date,
        "reported_end_date": reported_end_date,
    })
    base = _scenario_result(
        signals,
        bars,
        controlled_dates,
        external,
        config,
        BASE_COSTS,
        "base",
        benchmark_id,
        benchmark_total_return,
        benchmark_data_sha256,
        reported_start_date,
        reported_end_date,
    )
    stress = _scenario_result(
        signals,
        bars,
        controlled_dates,
        external,
        config,
        STRESS_COSTS,
        "stress",
        benchmark_id,
        benchmark_total_return,
        benchmark_data_sha256,
        reported_start_date,
        reported_end_date,
    )
    comparison_payload = _comparison_hash_payload(
        base=base,
        stress=stress,
        input_sha256=input_sha256,
        research_scope=research_scope,
        formal_signal_binding=formal_signal_binding,
        formal_signal_bindings=bindings,
        evaluation_sha256=evaluation_sha256,
        experiment_spec_sha256=experiment_spec_sha256,
        evaluation_source_bundle_sha256=evaluation_source_bundle_sha256,
        trading_calendar_sha256=effective_calendar_hash,
        benchmark_series_sha256=benchmark_series_sha256,
        execution_calendar_sha256=execution_calendar_hash,
        formal_window_start=reported_start_date,
        formal_window_end=reported_end_date,
        unmanaged_external_sha256=unmanaged_external_hash,
        controlled_execution_bar_adapter_verified=(
            CONTROLLED_EXECUTION_BAR_ADAPTER_VERIFIED
        ),
    )
    return AShareBacktestComparison(
        base=base,
        stress=stress,
        input_sha256=input_sha256,
        backtest_sha256=_content_hash(comparison_payload),
        research_scope=research_scope,
        formal_signal_binding=formal_signal_binding,
        formal_signal_bindings=bindings,
        evaluation_sha256=evaluation_sha256,
        experiment_spec_sha256=experiment_spec_sha256,
        evaluation_source_bundle_sha256=evaluation_source_bundle_sha256,
        trading_calendar_sha256=effective_calendar_hash,
        benchmark_series_sha256=benchmark_series_sha256,
        execution_calendar_sha256=execution_calendar_hash,
        formal_window_start=reported_start_date,
        formal_window_end=reported_end_date,
        unmanaged_external_sha256=unmanaged_external_hash,
    )


def run_a_share_top2_backtest(
    signals: Sequence[CloseSignal],
    bars: Sequence[AShareDailyBar],
    *,
    controlled_trading_dates: Sequence[date],
    benchmark_bars: Sequence[BenchmarkTotalReturnBar],
    unmanaged_external: Sequence[UnmanagedExternalPosition] = (),
    config: AShareTop2Config | None = None,
) -> AShareBacktestComparison:
    """Run a diagnostic raw-signal backtest that is never admission eligible."""

    frozen_config = config or AShareTop2Config()
    if not isinstance(frozen_config, AShareTop2Config):
        raise AShareBacktestError("config must be AShareTop2Config")
    return _run_a_share_top2_comparison(
        signals,
        bars,
        controlled_trading_dates=controlled_trading_dates,
        benchmark_bars=benchmark_bars,
        unmanaged_external=unmanaged_external,
        config=frozen_config,
        research_scope=DIAGNOSTIC_SIGNAL_SCOPE,
        formal_signal_binding=False,
    )


def run_formal_a_share_top2_backtest(
    evaluation_result: EvaluationResult,
    experiment: ExperimentSpecV2,
    bars: Sequence[AShareDailyBar],
    *,
    trading_calendar: Sequence[date],
    benchmark_bars: Sequence[BenchmarkTotalReturnBar],
    unmanaged_external: Sequence[UnmanagedExternalPosition],
    config: AShareTop2Config | None = None,
) -> AShareBacktestComparison:
    """Run the model-bound Top2 path used by historical-gate diagnostics.

    The full experiment calendar and total-return open-level series are
    re-hashed before the locked-test execution slice is formed.  Signals are
    always derived internally from the complete formal Ridge predictions.
    The supplied stock bars are not yet source-authenticated by a controlled
    Choice adapter, so the returned comparison remains explicitly unable to
    pass the Stage-A data gate.
    """

    if not isinstance(evaluation_result, EvaluationResult):
        raise AShareBacktestError("formal Top2 requires an EvaluationResult")
    if not isinstance(experiment, ExperimentSpecV2):
        raise AShareBacktestError("formal Top2 requires ExperimentSpecV2")
    if (
        evaluation_result.experiment_id != experiment.experiment_id
        or evaluation_result.experiment_spec_sha256 != experiment.spec_sha256
    ):
        raise AShareBacktestError("EvaluationResult is not bound to ExperimentSpecV2")
    content = experiment.to_content_dict()
    target = content["target"]
    benchmark_contract = content["benchmark"]
    if not isinstance(target, Mapping) or not isinstance(benchmark_contract, Mapping):
        raise AShareBacktestError("experiment target/benchmark contract is malformed")

    calendar = tuple(trading_calendar)
    calendar_hash = trading_calendar_content_sha256(calendar)
    if calendar_hash != target["trading_calendar_content_sha256"]:
        raise AShareBacktestError(
            "formal Top2 calendar hash does not match ExperimentSpecV2"
        )
    if int(target["horizon_trading_sessions"]) != FORMAL_HORIZON_SESSIONS:
        raise AShareBacktestError("formal Top2 horizon must remain 20 sessions")
    calendar_index = {item: index for index, item in enumerate(calendar)}

    full_benchmark = tuple(benchmark_bars)
    benchmark_id, _, _, normalized_full_benchmark = _validate_benchmark(
        full_benchmark, calendar
    )
    if benchmark_id != benchmark_contract["instrument_id"]:
        raise AShareBacktestError(
            "formal Top2 benchmark id does not match ExperimentSpecV2"
        )
    benchmark_points = tuple(
        BenchmarkTotalReturnPoint(item.trading_date, float(item.open_level))
        for item in normalized_full_benchmark
    )
    benchmark_series_hash = benchmark_total_return_series_content_sha256(
        benchmark_points
    )
    if benchmark_series_hash != benchmark_contract[
        "total_return_series_content_sha256"
    ]:
        raise AShareBacktestError(
            "formal Top2 benchmark series hash does not match ExperimentSpecV2"
        )

    bindings = formal_signal_bindings_from_evaluation(evaluation_result)
    decision_dates = tuple(item.decision_date for item in bindings)
    try:
        decision_indices = tuple(calendar_index[item] for item in decision_dates)
    except KeyError as exc:
        raise AShareBacktestError(
            "formal Top2 decision is absent from the experiment calendar"
        ) from exc
    if any(
        current - previous != FORMAL_HORIZON_SESSIONS
        for previous, current in zip(decision_indices, decision_indices[1:])
    ):
        raise AShareBacktestError(
            "formal Top2 decisions must remain exactly 20 sessions apart"
        )
    final_index = decision_indices[-1] + FORMAL_HORIZON_SESSIONS + 1
    if final_index >= len(calendar):
        raise AShareBacktestError("formal Top2 calendar is missing the final exit")
    first_index = decision_indices[0]
    execution_calendar = calendar[first_index : final_index + 1]
    reported_start = calendar[first_index + 1]
    reported_end = calendar[final_index]

    prepared_by_date = {
        item.decision_at.date(): item
        for item in evaluation_result.prepared_panel.cross_sections
        if item.split == FORMAL_SPLIT
    }
    for decision_date, decision_index in zip(
        decision_dates, decision_indices, strict=True
    ):
        prepared = prepared_by_date[decision_date]
        if (
            prepared.label_start_date != calendar[decision_index + 1]
            or prepared.label_end_date
            != calendar[decision_index + FORMAL_HORIZON_SESSIONS + 1]
        ):
            raise AShareBacktestError(
                "formal Top2 prepared window is not next-open to 20-session-open"
            )

    materialized_bars = tuple(bars)
    observed_bar_dates = {item.trading_date for item in materialized_bars}
    if observed_bar_dates != set(execution_calendar):
        raise AShareBacktestError(
            "formal Top2 stock bars must match the exact locked-test execution calendar"
        )
    signals = derive_formal_a_share_top2_close_signals(
        evaluation_result, materialized_bars
    )
    bar_keys = {(item.trading_date, item.instrument_id) for item in materialized_bars}
    for signal, decision_index in zip(signals, decision_indices, strict=True):
        execution_date = calendar[decision_index + 1]
        if any(
            (execution_date, candidate.instrument_id) not in bar_keys
            for candidate in signal.candidates
        ):
            raise AShareBacktestError(
                "every formal member requires an execution-date eligibility bar"
            )

    execution_benchmark = tuple(
        item
        for item in normalized_full_benchmark
        if item.trading_date in set(execution_calendar)
    )
    frozen_config = config or AShareTop2Config()
    if not isinstance(frozen_config, AShareTop2Config):
        raise AShareBacktestError("config must be AShareTop2Config")
    return _run_a_share_top2_comparison(
        signals,
        materialized_bars,
        controlled_trading_dates=execution_calendar,
        benchmark_bars=execution_benchmark,
        unmanaged_external=unmanaged_external,
        config=frozen_config,
        research_scope=FORMAL_BACKTEST_SCOPE,
        formal_signal_binding=True,
        formal_signal_bindings=bindings,
        evaluation_sha256=evaluation_result_content_sha256(evaluation_result),
        experiment_spec_sha256=experiment.spec_sha256,
        evaluation_source_bundle_sha256=evaluation_result.source_bundle_sha256,
        trading_calendar_sha256=calendar_hash,
        benchmark_series_sha256=benchmark_series_hash,
        reported_start_date=reported_start,
        reported_end_date=reported_end,
        benchmark_return_basis="open_to_open",
    )


__all__ = [
    "AS_SHARE_BACKTEST_VERSION",
    "CONTROLLED_EXECUTION_BAR_ADAPTER_VERIFIED",
    "DIAGNOSTIC_SIGNAL_SCOPE",
    "FORMAL_BACKTEST_SCOPE",
    "FORMAL_SIGNAL_SCOPE",
    "UNMANAGED_MIDEA_INSTRUMENT_ID",
    "AShareBacktestComparison",
    "AShareBacktestError",
    "AShareCostSchedule",
    "AShareDailyBar",
    "AShareEvent",
    "AShareNavPoint",
    "AShareScenarioResult",
    "AShareTop2Config",
    "AShareTrade",
    "BenchmarkTotalReturnBar",
    "CloseSignal",
    "FormalSignalBinding",
    "HistoricalGateResult",
    "PolicyGateResult",
    "RankedStockCandidate",
    "UnmanagedExternalPosition",
    "a_share_backtest_comparison_content_sha256",
    "derive_formal_a_share_top2_close_signals",
    "formal_signal_bindings_from_evaluation",
    "run_a_share_top2_backtest",
    "run_formal_a_share_top2_backtest",
]
