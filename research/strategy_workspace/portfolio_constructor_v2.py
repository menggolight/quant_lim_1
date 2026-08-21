"""Deterministic portfolio construction for Adaptive Exposure V2.

The constructor consumes an already-produced Alpha cross-section and an
explicit gross-exposure target.  It never submits orders and it never chooses
production thresholds by itself: every band, cost assumption and execution
tolerance is carried by a frozen, hash-bound policy object supplied by the
caller.

Target, feasible and current exposure are deliberately separate.  Target is
the exposure-engine budget, feasible is the whole-lot/cost-aware portfolio the
constructor can actually request, and current is the pre-decision account
state.  A normal Alpha rebalance is accepted only when the expected portfolio
improvement is *strictly* greater than complete estimated costs plus the
pre-registered no-trade threshold.  Explicit cash/risk reductions bypass that
Alpha comparison but may never introduce a buy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from trading.models import PortfolioIntentType

from .contracts import canonical_sha256


STRATEGY_ID = "a-share-small-account-adaptive-exposure-v2"
POLICY_SCHEMA_VERSION = "portfolio-constructor-policy.v1"
RESULT_SCHEMA_VERSION = "portfolio-construction-result.v2"
POLICY_STATUS = "frozen_pre_registered"
ZERO = Decimal("0")
ONE = Decimal("1")
PCT = Decimal("0.00000001")
MONEY = Decimal("0.0001")
BPS = Decimal("10000")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_INSTRUMENT_RE = re.compile(r"^[0-9A-Z][0-9A-Z.]{2,31}$")
_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class PortfolioConstructionError(ValueError):
    """Raised when construction would guess, widen or corrupt the contract."""


class ConstructionActionType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    CASH = "CASH"


_REDUCTION_TYPES = frozenset(
    {
        PortfolioIntentType.NO_ALPHA_CASH,
        PortfolioIntentType.DEFENSIVE_REDUCTION,
        PortfolioIntentType.RISK_OFF,
        PortfolioIntentType.ACCOUNT_DRAWDOWN_EXIT,
    }
)
_ZERO_TARGET_TYPES = frozenset(
    {
        PortfolioIntentType.NO_ALPHA_CASH,
        PortfolioIntentType.RISK_OFF,
        PortfolioIntentType.ACCOUNT_DRAWDOWN_EXIT,
    }
)


def _decimal(value: Any, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PortfolioConstructionError(f"{field_name} must be decimal") from exc
    if not result.is_finite():
        raise PortfolioConstructionError(f"{field_name} must be finite")
    return result


def _aware(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PortfolioConstructionError(f"{field_name} must be timezone-aware")
    return value


def _sha256(value: str, field_name: str) -> str:
    normalized = str(value)
    if _SHA256_RE.fullmatch(normalized) is None:
        raise PortfolioConstructionError(f"{field_name} must be a lowercase SHA-256")
    return normalized


def _identifier(value: str, field_name: str) -> str:
    normalized = str(value).strip()
    if _IDENTIFIER_RE.fullmatch(normalized) is None:
        raise PortfolioConstructionError(f"{field_name} is not a valid identifier")
    return normalized


def _instrument_id(value: str) -> str:
    normalized = str(value).strip().upper()
    if _INSTRUMENT_RE.fullmatch(normalized) is None:
        raise PortfolioConstructionError("instrument_id is invalid")
    return normalized


def _reason_codes(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise PortfolioConstructionError(f"{field_name} must be an ordered array")
    normalized = tuple(sorted({str(item).strip() for item in values}))
    if any(_REASON_RE.fullmatch(item) is None for item in normalized):
        raise PortfolioConstructionError(f"{field_name} contains an invalid reason code")
    return normalized


def _weight(value: Decimal) -> Decimal:
    return value.quantize(PCT, rounding=ROUND_DOWN)


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY)


@dataclass(frozen=True, slots=True)
class ConstructorCostPolicy:
    commission_rate: Decimal
    minimum_commission: Decimal
    sell_tax_rate: Decimal
    transfer_fee_rate: Decimal
    slippage_bps_one_way: Decimal

    def __post_init__(self) -> None:
        for field_name in (
            "commission_rate",
            "minimum_commission",
            "sell_tax_rate",
            "transfer_fee_rate",
            "slippage_bps_one_way",
        ):
            value = _decimal(getattr(self, field_name), field_name)
            if value < ZERO:
                raise PortfolioConstructionError(f"{field_name} must not be negative")
            object.__setattr__(self, field_name, value)

    def to_dict(self) -> dict[str, str]:
        return {
            "commission_rate": str(self.commission_rate),
            "minimum_commission": str(self.minimum_commission),
            "sell_tax_rate": str(self.sell_tax_rate),
            "transfer_fee_rate": str(self.transfer_fee_rate),
            "slippage_bps_one_way": str(self.slippage_bps_one_way),
        }


@dataclass(frozen=True, slots=True)
class PortfolioConstructorPolicy:
    """All adjustable constructor thresholds, frozen before a decision."""

    policy_id: str
    frozen_at: datetime
    max_positions: int
    max_position_weight: Decimal
    entry_percentile_min: Decimal
    hold_percentile_min: Decimal
    no_trade_threshold: Decimal
    maximum_execution_price_deviation: Decimal
    maximum_quote_age_seconds: int
    maximum_account_age_seconds: int
    costs: ConstructorCostPolicy
    policy_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        policy_id = _identifier(self.policy_id, "policy_id")
        frozen_at = _aware(self.frozen_at, "frozen_at")
        if type(self.max_positions) is not int or not 1 <= self.max_positions <= 3:
            raise PortfolioConstructionError("max_positions must be between 1 and 3")
        max_weight = _decimal(self.max_position_weight, "max_position_weight")
        if not ZERO < max_weight <= Decimal("0.40"):
            raise PortfolioConstructionError(
                "max_position_weight must be positive and not exceed 0.40"
            )
        entry = _decimal(self.entry_percentile_min, "entry_percentile_min")
        hold = _decimal(self.hold_percentile_min, "hold_percentile_min")
        if not ZERO <= hold <= entry <= ONE:
            raise PortfolioConstructionError(
                "hold_percentile_min must be <= entry_percentile_min within [0,1]"
            )
        no_trade = _decimal(self.no_trade_threshold, "no_trade_threshold")
        deviation = _decimal(
            self.maximum_execution_price_deviation,
            "maximum_execution_price_deviation",
        )
        if no_trade < ZERO or deviation < ZERO:
            raise PortfolioConstructionError(
                "no-trade and execution-deviation thresholds must not be negative"
            )
        for field_name in (
            "maximum_quote_age_seconds",
            "maximum_account_age_seconds",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value <= 0:
                raise PortfolioConstructionError(f"{field_name} must be a positive integer")
        if not isinstance(self.costs, ConstructorCostPolicy):
            raise PortfolioConstructionError("costs must be ConstructorCostPolicy")
        object.__setattr__(self, "policy_id", policy_id)
        object.__setattr__(self, "frozen_at", frozen_at)
        object.__setattr__(self, "max_position_weight", max_weight)
        object.__setattr__(self, "entry_percentile_min", entry)
        object.__setattr__(self, "hold_percentile_min", hold)
        object.__setattr__(self, "no_trade_threshold", no_trade)
        object.__setattr__(self, "maximum_execution_price_deviation", deviation)
        object.__setattr__(self, "policy_sha256", canonical_sha256(self.to_content_dict()))

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": POLICY_SCHEMA_VERSION,
            "status": POLICY_STATUS,
            "policy_id": self.policy_id,
            "frozen_at": self.frozen_at,
            "selection_method": "incumbent_hold_band_then_ranked_entry_band",
            "weighting_method": "equal_weight_capped_then_whole_lot_floor",
            "improvement_rule": "strictly_greater_than_complete_cost_plus_threshold",
            "max_positions": self.max_positions,
            "max_position_weight": self.max_position_weight,
            "entry_percentile_min": self.entry_percentile_min,
            "hold_percentile_min": self.hold_percentile_min,
            "no_trade_threshold": self.no_trade_threshold,
            "maximum_execution_price_deviation": (
                self.maximum_execution_price_deviation
            ),
            "maximum_quote_age_seconds": self.maximum_quote_age_seconds,
            "maximum_account_age_seconds": self.maximum_account_age_seconds,
            "costs": self.costs.to_dict(),
            "manual_execution_required": True,
            "automatic_submission": False,
            "live_supported": False,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.to_content_dict()
        payload["policy_sha256"] = self.policy_sha256
        return payload


@dataclass(frozen=True, slots=True)
class PortfolioInstrument:
    instrument_id: str
    predicted_return: Decimal | None
    percentile: Decimal | None
    eligibility: bool
    exclusion_codes: tuple[str, ...]
    reference_price: Decimal
    lot_size: int

    def __post_init__(self) -> None:
        instrument_id = _instrument_id(self.instrument_id)
        predicted = (
            None
            if self.predicted_return is None
            else _decimal(self.predicted_return, "predicted_return")
        )
        percentile = (
            None
            if self.percentile is None
            else _decimal(self.percentile, "percentile")
        )
        price = _decimal(self.reference_price, "reference_price")
        if percentile is not None and not ZERO <= percentile <= ONE:
            raise PortfolioConstructionError("percentile must be within [0,1]")
        if type(self.eligibility) is not bool:
            raise PortfolioConstructionError("eligibility must be boolean")
        exclusions = _reason_codes(self.exclusion_codes, "exclusion_codes")
        if self.eligibility and (predicted is None or percentile is None):
            raise PortfolioConstructionError(
                "eligible instruments require predicted_return and percentile"
            )
        if self.eligibility and exclusions:
            raise PortfolioConstructionError(
                "eligible instruments cannot carry exclusion_codes"
            )
        if not self.eligibility and not exclusions:
            raise PortfolioConstructionError(
                "ineligible instruments require complete exclusion_codes"
            )
        if price <= ZERO:
            raise PortfolioConstructionError("reference_price must be positive")
        if type(self.lot_size) is not int or self.lot_size <= 0:
            raise PortfolioConstructionError("lot_size must be a positive integer")
        object.__setattr__(self, "instrument_id", instrument_id)
        object.__setattr__(self, "predicted_return", predicted)
        object.__setattr__(self, "percentile", percentile)
        object.__setattr__(self, "exclusion_codes", exclusions)
        object.__setattr__(self, "reference_price", price)

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "predicted_return": (
                str(self.predicted_return)
                if self.predicted_return is not None
                else None
            ),
            "percentile": str(self.percentile) if self.percentile is not None else None,
            "eligibility": self.eligibility,
            "exclusion_codes": list(self.exclusion_codes),
            "reference_price": str(self.reference_price),
            "lot_size": self.lot_size,
        }


@dataclass(frozen=True, slots=True)
class CurrentPosition:
    instrument_id: str
    quantity: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument_id", _instrument_id(self.instrument_id))
        if type(self.quantity) is not int or self.quantity <= 0:
            raise PortfolioConstructionError("current position quantity must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {"instrument_id": self.instrument_id, "quantity": self.quantity}


@dataclass(frozen=True, slots=True)
class InstrumentExclusion:
    instrument_id: str
    codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"instrument_id": self.instrument_id, "codes": list(self.codes)}


@dataclass(frozen=True, slots=True)
class ConstructionAction:
    action: ConstructionActionType
    instrument_id: str | None
    order_quantity: int
    whole_lots: int
    odd_lot_quantity: int
    current_quantity: int
    target_quantity: int
    lot_size: int | None
    reference_price: Decimal
    target_weight: Decimal
    feasible_weight: Decimal
    estimated_cost: Decimal
    reason_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "instrument_id": self.instrument_id,
            "order_quantity": self.order_quantity,
            "whole_lots": self.whole_lots,
            "odd_lot_quantity": self.odd_lot_quantity,
            "current_quantity": self.current_quantity,
            "target_quantity": self.target_quantity,
            "lot_size": self.lot_size,
            "reference_price": str(self.reference_price),
            "target_weight": str(self.target_weight),
            "feasible_weight": str(self.feasible_weight),
            "estimated_cost": str(self.estimated_cost),
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True, slots=True)
class PortfolioConstructionResult:
    decision_at: datetime
    requested_intent_type: PortfolioIntentType
    intent_type: PortfolioIntentType
    input_snapshot_sha256: str
    model_sha256: str
    constructor_policy_sha256: str
    constructor_input_sha256: str
    account_state_sha256: str
    current_cash: Decimal
    current_quantities: Mapping[str, int]
    current_nav: Decimal
    current_gross_exposure: Decimal
    current_stock_weights: Mapping[str, Decimal]
    target_gross_exposure: Decimal
    target_stock_weights: Mapping[str, Decimal]
    feasible_gross_exposure: Decimal
    feasible_stock_weights: Mapping[str, Decimal]
    feasible_quantities: Mapping[str, int]
    projected_cash: Decimal
    expected_improvement: Decimal | None
    proposed_expected_cost: Decimal
    expected_cost: Decimal
    expected_cost_ratio: Decimal
    required_improvement: Decimal
    alpha_trade_allowed: bool
    actions: tuple[ConstructionAction, ...]
    exclusions: tuple[InstrumentExclusion, ...]
    reason_codes: tuple[str, ...]
    construction_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        for field_name in (
            "current_quantities",
            "current_stock_weights",
            "target_stock_weights",
            "feasible_stock_weights",
            "feasible_quantities",
        ):
            object.__setattr__(self, field_name, MappingProxyType(dict(getattr(self, field_name))))
        object.__setattr__(self, "construction_sha256", canonical_sha256(self.to_content_dict()))

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "strategy_id": STRATEGY_ID,
            "decision_at": self.decision_at,
            "requested_intent_type": self.requested_intent_type.value,
            "intent_type": self.intent_type.value,
            "input_snapshot_sha256": self.input_snapshot_sha256,
            "model_sha256": self.model_sha256,
            "constructor_policy_sha256": self.constructor_policy_sha256,
            "constructor_input_sha256": self.constructor_input_sha256,
            "account_state_sha256": self.account_state_sha256,
            "current_cash": self.current_cash,
            "current_quantities": dict(sorted(self.current_quantities.items())),
            "current_nav": self.current_nav,
            "current_gross_exposure": self.current_gross_exposure,
            "current_stock_weights": dict(sorted(self.current_stock_weights.items())),
            "target_gross_exposure": self.target_gross_exposure,
            "target_stock_weights": dict(sorted(self.target_stock_weights.items())),
            "feasible_gross_exposure": self.feasible_gross_exposure,
            "feasible_stock_weights": dict(sorted(self.feasible_stock_weights.items())),
            "feasible_quantities": dict(sorted(self.feasible_quantities.items())),
            "projected_cash": self.projected_cash,
            "expected_improvement": self.expected_improvement,
            "proposed_expected_cost": self.proposed_expected_cost,
            "expected_cost": self.expected_cost,
            "expected_cost_ratio": self.expected_cost_ratio,
            "required_improvement": self.required_improvement,
            "alpha_trade_allowed": self.alpha_trade_allowed,
            "actions": [item.to_dict() for item in self.actions],
            "exclusions": [item.to_dict() for item in self.exclusions],
            "reason_codes": list(self.reason_codes),
            "manual_execution_required": True,
            "automatic_submission": False,
            "paper_eligibility": False,
            "trade_eligibility": False,
            "real_money_list_allowed": False,
            "live_supported": False,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.to_content_dict()
        payload["construction_sha256"] = self.construction_sha256
        return payload


def _estimate_trade_cost(
    notional: Decimal,
    *,
    is_sell: bool,
    policy: ConstructorCostPolicy,
) -> Decimal:
    if notional <= ZERO:
        return ZERO
    commission = max(notional * policy.commission_rate, policy.minimum_commission)
    transfer = notional * policy.transfer_fee_rate
    sell_tax = notional * policy.sell_tax_rate if is_sell else ZERO
    slippage = notional * policy.slippage_bps_one_way / BPS
    return _money(commission + transfer + sell_tax + slippage)


def _portfolio_costs(
    quantities: Mapping[str, int],
    current: Mapping[str, int],
    instruments: Mapping[str, PortfolioInstrument],
    policy: ConstructorCostPolicy,
) -> tuple[dict[str, Decimal], Decimal, Decimal]:
    by_instrument: dict[str, Decimal] = {}
    cash_change = ZERO
    total_cost = ZERO
    for instrument_id in sorted(set(quantities) | set(current)):
        target_quantity = int(quantities.get(instrument_id, 0))
        current_quantity = int(current.get(instrument_id, 0))
        delta = target_quantity - current_quantity
        if delta == 0:
            continue
        notional = instruments[instrument_id].reference_price * abs(delta)
        cost = _estimate_trade_cost(
            notional,
            is_sell=delta < 0,
            policy=policy,
        )
        by_instrument[instrument_id] = cost
        total_cost += cost
        cash_change += (-notional if delta > 0 else notional) - cost
    return by_instrument, _money(total_cost), _money(cash_change)


def _metrics(
    quantities: Mapping[str, int],
    current: Mapping[str, int],
    instruments: Mapping[str, PortfolioInstrument],
    cash: Decimal,
    policy: PortfolioConstructorPolicy,
) -> tuple[dict[str, Decimal], Decimal, Decimal, Decimal, dict[str, Decimal]]:
    costs, total_cost, cash_change = _portfolio_costs(
        quantities, current, instruments, policy.costs
    )
    projected_cash = _money(cash + cash_change)
    values = {
        instrument_id: instruments[instrument_id].reference_price * quantity
        for instrument_id, quantity in quantities.items()
        if quantity > 0
    }
    projected_nav = projected_cash + sum(values.values(), ZERO)
    if projected_nav <= ZERO:
        raise PortfolioConstructionError("projected portfolio NAV must be positive")
    weights = {
        instrument_id: _weight(value / projected_nav)
        for instrument_id, value in sorted(values.items())
    }
    gross = _weight(sum(values.values(), ZERO) / projected_nav)
    return weights, gross, projected_cash, total_cost, costs


def _floor_lot(notional: Decimal, price: Decimal, lot_size: int) -> int:
    raw_quantity = int((notional / price).to_integral_value(rounding=ROUND_DOWN))
    return (raw_quantity // lot_size) * lot_size


def _theoretical_weights(
    selected: Sequence[str],
    target_gross_exposure: Decimal,
    policy: PortfolioConstructorPolicy,
) -> dict[str, Decimal]:
    if not selected or target_gross_exposure == ZERO:
        return {}
    per_name = min(
        policy.max_position_weight,
        target_gross_exposure / Decimal(len(selected)),
    )
    return {instrument_id: _weight(per_name) for instrument_id in selected}


def _adjust_quantities(
    quantities: dict[str, int],
    *,
    selected_order: Sequence[str],
    current: Mapping[str, int],
    instruments: Mapping[str, PortfolioInstrument],
    cash: Decimal,
    target_gross_exposure: Decimal,
    policy: PortfolioConstructorPolicy,
) -> tuple[dict[str, int], dict[str, Decimal], Decimal, Decimal, Decimal, dict[str, Decimal]]:
    max_iterations = 1 + sum(
        quantity // instruments[instrument_id].lot_size
        for instrument_id, quantity in quantities.items()
        if quantity > 0
    )
    preference = {instrument_id: index for index, instrument_id in enumerate(selected_order)}
    for _ in range(max_iterations):
        weights, gross, projected_cash, total_cost, costs = _metrics(
            quantities, current, instruments, cash, policy
        )
        overweight = [
            instrument_id
            for instrument_id, weight in weights.items()
            if weight > policy.max_position_weight
        ]
        if projected_cash >= ZERO and gross <= target_gross_exposure and not overweight:
            return quantities, weights, gross, projected_cash, total_cost, costs
        if overweight:
            instrument_id = sorted(overweight)[0]
        else:
            reducible = [item for item, quantity in quantities.items() if quantity > 0]
            if not reducible:
                break
            instrument_id = max(
                reducible,
                key=lambda item: (preference.get(item, len(preference)), item),
            )
        decrement = instruments[instrument_id].lot_size
        quantities[instrument_id] = max(0, quantities[instrument_id] - decrement)
    raise PortfolioConstructionError(
        "whole-lot portfolio cannot satisfy cash and exposure constraints"
    )


def _account_state_sha256(cash: Decimal, quantities: Mapping[str, int]) -> str:
    return canonical_sha256(
        {
            "scope": "portfolio-constructor-account-state.v1",
            "strategy_id": STRATEGY_ID,
            "cash": cash,
            "positions": dict(sorted(quantities.items())),
        }
    )


def construct_portfolio(
    *,
    decision_at: datetime,
    requested_intent_type: PortfolioIntentType,
    target_gross_exposure: Decimal,
    current_cash: Decimal,
    current_positions: Sequence[CurrentPosition],
    instruments: Sequence[PortfolioInstrument],
    policy: PortfolioConstructorPolicy,
    input_snapshot_sha256: str,
    model_sha256: str,
) -> PortfolioConstructionResult:
    """Build one deterministic, research-only portfolio decision."""

    decision_at = _aware(decision_at, "decision_at")
    if not isinstance(policy, PortfolioConstructorPolicy):
        raise PortfolioConstructionError("policy must be PortfolioConstructorPolicy")
    if policy.frozen_at > decision_at:
        raise PortfolioConstructionError("constructor policy must be frozen before decision_at")
    if not isinstance(requested_intent_type, PortfolioIntentType):
        raise PortfolioConstructionError("requested_intent_type must be PortfolioIntentType")
    if requested_intent_type not in {PortfolioIntentType.ALPHA_REBALANCE, *_REDUCTION_TYPES}:
        raise PortfolioConstructionError("unsupported constructor intent type")
    requested_target = _decimal(target_gross_exposure, "target_gross_exposure")
    if not ZERO <= requested_target <= ONE:
        raise PortfolioConstructionError("target_gross_exposure must be within [0,1]")
    if requested_intent_type in _ZERO_TARGET_TYPES and requested_target != ZERO:
        raise PortfolioConstructionError("cash/exit intent requires zero target exposure")
    cash = _decimal(current_cash, "current_cash")
    if cash < ZERO:
        raise PortfolioConstructionError("current_cash must not be negative")
    input_hash = _sha256(input_snapshot_sha256, "input_snapshot_sha256")
    model_hash = _sha256(model_sha256, "model_sha256")

    instrument_rows = tuple(instruments)
    if not instrument_rows:
        raise PortfolioConstructionError("instruments must cover the controlled Alpha pool")
    by_instrument = {item.instrument_id: item for item in instrument_rows}
    if len(by_instrument) != len(instrument_rows):
        raise PortfolioConstructionError("instruments must be unique")
    positions = tuple(current_positions)
    current = {item.instrument_id: item.quantity for item in positions}
    if len(current) != len(positions):
        raise PortfolioConstructionError("current_positions must be unique")
    missing = sorted(set(current) - set(by_instrument))
    if missing:
        raise PortfolioConstructionError(
            "all current positions require complete Alpha/rule/price rows"
        )

    current_values = {
        instrument_id: by_instrument[instrument_id].reference_price * quantity
        for instrument_id, quantity in current.items()
    }
    current_nav = _money(cash + sum(current_values.values(), ZERO))
    if current_nav <= ZERO:
        raise PortfolioConstructionError("current NAV must be positive")
    current_weights = {
        instrument_id: _weight(value / current_nav)
        for instrument_id, value in sorted(current_values.items())
    }
    current_gross = _weight(sum(current_values.values(), ZERO) / current_nav)

    exclusions: dict[str, set[str]] = {
        item.instrument_id: set(item.exclusion_codes) for item in instrument_rows
    }
    # Eligible rows are guaranteed to carry both values.  Excluded rows may
    # retain genuine missing values; they sort after scored rows without
    # manufacturing a numeric zero that could leak into selection or PnL.
    ranked = sorted(
        instrument_rows,
        key=lambda item: (
            item.predicted_return is None,
            -item.predicted_return if item.predicted_return is not None else ZERO,
            item.percentile is None,
            -item.percentile if item.percentile is not None else ZERO,
            item.instrument_id,
        ),
    )
    effective_intent = requested_intent_type
    selected: list[str] = []

    if requested_intent_type is PortfolioIntentType.ALPHA_REBALANCE:
        incumbents = [
            item
            for item in ranked
            if item.instrument_id in current
            and item.eligibility
            and item.percentile >= policy.hold_percentile_min
        ]
        for item in ranked:
            if item.instrument_id in current and item not in incumbents:
                exclusions[item.instrument_id].add("incumbent_below_hold_band")
        selected.extend(item.instrument_id for item in incumbents[: policy.max_positions])
        entrants = [
            item
            for item in ranked
            if item.instrument_id not in current
            and item.eligibility
            and item.percentile >= policy.entry_percentile_min
        ]
        for item in ranked:
            if (
                item.instrument_id not in current
                and item.eligibility
                and item.percentile < policy.entry_percentile_min
            ):
                exclusions[item.instrument_id].add("below_entry_band")
        for item in entrants:
            if len(selected) >= policy.max_positions:
                exclusions[item.instrument_id].add("max_positions_reached")
                continue
            selected.append(item.instrument_id)
        if not selected:
            effective_intent = PortfolioIntentType.NO_ALPHA_CASH
            requested_target = ZERO
    else:
        # Risk/cash reductions never rotate into a new name.  Ranked current
        # positions are retained only to the extent permitted by the lower
        # gross-exposure target; target quantities are capped at current size.
        if requested_intent_type is PortfolioIntentType.DEFENSIVE_REDUCTION:
            if requested_target > current_gross:
                raise PortfolioConstructionError(
                    "DEFENSIVE_REDUCTION cannot increase current gross exposure"
                )
            selected = [
                item.instrument_id
                for item in ranked
                if item.instrument_id in current
            ][: policy.max_positions]

    target_weights = _theoretical_weights(selected, requested_target, policy)
    target_quantities: dict[str, int] = {}
    for instrument_id, target_weight in target_weights.items():
        instrument = by_instrument[instrument_id]
        quantity = _floor_lot(
            target_weight * current_nav,
            instrument.reference_price,
            instrument.lot_size,
        )
        if effective_intent in _REDUCTION_TYPES:
            quantity = min(quantity, current.get(instrument_id, 0))
        target_quantities[instrument_id] = quantity
        if quantity == 0:
            exclusions[instrument_id].add("minimum_lot_unaffordable")
    for instrument_id in current:
        target_quantities.setdefault(instrument_id, 0)

    (
        proposed_quantities,
        proposed_weights,
        proposed_gross,
        proposed_cash,
        proposed_cost,
        proposed_costs,
    ) = _adjust_quantities(
        dict(target_quantities),
        selected_order=selected,
        current=current,
        instruments=by_instrument,
        cash=cash,
        target_gross_exposure=requested_target,
        policy=policy,
    )

    predicted = {item.instrument_id: item.predicted_return for item in instrument_rows}
    changed_instruments = {
        instrument_id
        for instrument_id in set(proposed_quantities) | set(current)
        if proposed_quantities.get(instrument_id, 0) != current.get(instrument_id, 0)
    }
    improvement_is_available = all(
        predicted[instrument_id] is not None for instrument_id in changed_instruments
    )
    expected_improvement: Decimal | None
    if improvement_is_available:
        raw_current_weights = {
            instrument_id: value / current_nav
            for instrument_id, value in current_values.items()
        }
        proposed_values = {
            instrument_id: by_instrument[instrument_id].reference_price * quantity
            for instrument_id, quantity in proposed_quantities.items()
            if quantity > 0
        }
        proposed_nav = proposed_cash + sum(proposed_values.values(), ZERO)
        if proposed_nav <= ZERO:
            raise PortfolioConstructionError("proposed portfolio NAV must be positive")
        raw_proposed_weights = {
            instrument_id: value / proposed_nav
            for instrument_id, value in proposed_values.items()
        }
        expected_improvement = sum(
            (
                (
                    raw_proposed_weights.get(instrument_id, ZERO)
                    - raw_current_weights.get(instrument_id, ZERO)
                )
                * predicted_return
                for instrument_id in sorted(
                    set(raw_current_weights) | set(raw_proposed_weights)
                )
                if (predicted_return := predicted[instrument_id]) is not None
            ),
            ZERO,
        )
    else:
        expected_improvement = None
    raw_cost_ratio = proposed_cost / current_nav
    raw_required_improvement = raw_cost_ratio + policy.no_trade_threshold
    cost_ratio = _weight(raw_cost_ratio)
    required_improvement = raw_required_improvement.quantize(PCT)
    proposed_has_trade = any(
        proposed_quantities.get(item, 0) != current.get(item, 0)
        for item in set(proposed_quantities) | set(current)
    )
    reasons: set[str] = set()

    if effective_intent is PortfolioIntentType.ALPHA_REBALANCE:
        alpha_trade_allowed = bool(
            proposed_has_trade
            and expected_improvement is not None
            and expected_improvement > raw_required_improvement
        )
        if not proposed_has_trade:
            reasons.add("already_at_feasible_target")
        elif expected_improvement is None:
            reasons.add("expected_improvement_unavailable_missing_prediction")
        elif not alpha_trade_allowed:
            reasons.add("expected_improvement_not_above_cost_and_threshold")
    else:
        alpha_trade_allowed = False
        if proposed_has_trade:
            reasons.add("reduction_bypasses_alpha_no_trade_threshold")
        else:
            reasons.add("already_at_reduction_target")
        if effective_intent is PortfolioIntentType.NO_ALPHA_CASH:
            reasons.add("no_eligible_alpha_cash")

    if effective_intent is PortfolioIntentType.ALPHA_REBALANCE and not alpha_trade_allowed:
        final_quantities = dict(current)
        final_weights = dict(current_weights)
        final_gross = current_gross
        final_cash = cash
        final_cost = ZERO
        final_costs: dict[str, Decimal] = {}
    else:
        final_quantities = {
            item: quantity for item, quantity in proposed_quantities.items() if quantity > 0
        }
        final_weights = proposed_weights
        final_gross = proposed_gross
        final_cash = proposed_cash
        final_cost = proposed_cost
        final_costs = proposed_costs

    actions: list[ConstructionAction] = []
    for instrument_id in sorted(set(current) | set(final_quantities)):
        current_quantity = current.get(instrument_id, 0)
        target_quantity = final_quantities.get(instrument_id, 0)
        delta = target_quantity - current_quantity
        if delta > 0:
            action_type = ConstructionActionType.BUY
            action_reasons = ("ranked_entry_whole_lot",)
        elif delta < 0:
            action_type = ConstructionActionType.SELL
            action_reasons = (
                "explicit_reduction" if effective_intent in _REDUCTION_TYPES else "alpha_rebalance",
            )
        elif target_quantity > 0:
            action_type = ConstructionActionType.HOLD
            action_reasons = (
                "incumbent_within_hold_band"
                if instrument_id in selected
                else "no_trade_threshold_hold"
            ,)
        else:
            continue
        instrument = by_instrument[instrument_id]
        order_quantity = abs(delta)
        actions.append(
            ConstructionAction(
                action=action_type,
                instrument_id=instrument_id,
                order_quantity=order_quantity,
                whole_lots=order_quantity // instrument.lot_size,
                odd_lot_quantity=order_quantity % instrument.lot_size,
                current_quantity=current_quantity,
                target_quantity=target_quantity,
                lot_size=instrument.lot_size,
                reference_price=instrument.reference_price,
                target_weight=target_weights.get(instrument_id, ZERO),
                feasible_weight=final_weights.get(instrument_id, ZERO),
                estimated_cost=final_costs.get(instrument_id, ZERO),
                reason_codes=tuple(action_reasons),
            )
        )

    target_cash_weight = _weight(ONE - sum(target_weights.values(), ZERO))
    projected_nav = final_cash + sum(
        by_instrument[item].reference_price * quantity
        for item, quantity in final_quantities.items()
    )
    feasible_cash_weight = _weight(final_cash / projected_nav)
    actions.append(
        ConstructionAction(
            action=ConstructionActionType.CASH,
            instrument_id=None,
            order_quantity=0,
            whole_lots=0,
            odd_lot_quantity=0,
            current_quantity=0,
            target_quantity=0,
            lot_size=None,
            reference_price=ZERO,
            target_weight=target_cash_weight,
            feasible_weight=feasible_cash_weight,
            estimated_cost=ZERO,
            reason_codes=("residual_cash_preserved",),
        )
    )

    normalized_exclusions = tuple(
        InstrumentExclusion(instrument_id, tuple(sorted(codes)))
        for instrument_id, codes in sorted(exclusions.items())
        if codes
    )
    constructor_input_sha256 = canonical_sha256(
        {
            "scope": "portfolio-constructor-input.v2",
            "decision_at": decision_at,
            "requested_intent_type": requested_intent_type.value,
            "target_gross_exposure": target_gross_exposure,
            "current_cash": cash,
            "current_positions": [item.to_dict() for item in sorted(positions, key=lambda x: x.instrument_id)],
            "instruments": [item.to_dict() for item in sorted(instrument_rows, key=lambda x: x.instrument_id)],
            "input_snapshot_sha256": input_hash,
            "model_sha256": model_hash,
            "constructor_policy_sha256": policy.policy_sha256,
        }
    )
    return PortfolioConstructionResult(
        decision_at=decision_at,
        requested_intent_type=requested_intent_type,
        intent_type=effective_intent,
        input_snapshot_sha256=input_hash,
        model_sha256=model_hash,
        constructor_policy_sha256=policy.policy_sha256,
        constructor_input_sha256=constructor_input_sha256,
        account_state_sha256=_account_state_sha256(cash, current),
        current_cash=_money(cash),
        current_quantities=dict(sorted(current.items())),
        current_nav=current_nav,
        current_gross_exposure=current_gross,
        current_stock_weights=current_weights,
        target_gross_exposure=requested_target,
        target_stock_weights=target_weights,
        feasible_gross_exposure=final_gross,
        feasible_stock_weights=final_weights,
        feasible_quantities=dict(sorted(final_quantities.items())),
        projected_cash=_money(final_cash),
        expected_improvement=(
            expected_improvement.quantize(PCT)
            if expected_improvement is not None
            else None
        ),
        proposed_expected_cost=_money(proposed_cost),
        expected_cost=_money(final_cost),
        expected_cost_ratio=cost_ratio,
        required_improvement=required_improvement,
        alpha_trade_allowed=alpha_trade_allowed,
        actions=tuple(actions),
        exclusions=normalized_exclusions,
        reason_codes=tuple(sorted(reasons)),
    )


__all__ = [
    "ConstructionAction",
    "ConstructionActionType",
    "ConstructorCostPolicy",
    "CurrentPosition",
    "InstrumentExclusion",
    "POLICY_SCHEMA_VERSION",
    "PortfolioConstructionError",
    "PortfolioConstructionResult",
    "PortfolioConstructorPolicy",
    "PortfolioInstrument",
    "RESULT_SCHEMA_VERSION",
    "STRATEGY_ID",
    "construct_portfolio",
]
