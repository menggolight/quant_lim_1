"""Deterministic, manual-only daily decision workflow for Adaptive Exposure V2.

The module deliberately separates the D-close decision, the next-session
review, manual fill evidence, and the close ledger append.  It never submits an
order and never grants Paper, trading, real-money, or LIVE eligibility.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from statistics import median
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from research.factor_discovery.governance import (
    ApprovedFactorRegistryV1,
    FactorGovernanceError,
)
from research.strategy_workspace.adaptive_exposure import (
    DEFAULT_ADAPTIVE_EXPOSURE_POLICY_PATH,
    FROZEN_ADAPTIVE_EXPOSURE_POLICY_SHA256,
    load_adaptive_exposure_policy,
)
from research.strategy_workspace.alpha_engine_v2 import (
    AlphaEngineError,
    AlphaRankingV2,
    AlphaRunStatus,
    ControlledPitSnapshotV2,
    FrozenAlphaModelV2,
    run_alpha_engine,
)
from research.strategy_workspace.contracts import canonical_json_bytes, canonical_sha256
from research.strategy_workspace.daily_signal_publication import (
    DailySignalAdmissionReceiptV1,
    DailySignalAuthority,
    DailySignalPublicationReceiptV1,
    _publish_daily_signal_bundle_from_daily_pipeline,
)
from research.strategy_workspace.experiment_v3_admission import (
    ExperimentV3AdmissionError,
    ExperimentV3AdmissionReceiptV1,
    verify_experiment_v3_admission_receipt,
    verify_experiment_v3_diagnostic_binding,
)
from research.strategy_workspace.exposure_engine_v2 import (
    ACCOUNT_DRAWDOWN_RISK_OFF_TRIGGER,
    EXPOSURE_DECISION_SCHEMA_VERSION,
    EXPOSURE_INPUT_SCHEMA_VERSION,
    ExposureDecisionV2,
    ExposureEngineError,
    ExposureHysteresisPolicyV2,
    ExposureInputCategory,
    ExposureInputSnapshotV2,
    ExposureMetricStatus,
    ExposureMetricV2,
    ExposureState,
    ExposureStateMemoryV2,
    decide_exposure,
)
from research.strategy_workspace.next_session_signal import (
    NextSessionConsumption,
    NextSessionChannel,
    NextSessionSignalConflict,
    NextSessionSignalError,
    NextSessionSignal,
    OfficialCalendarReceipt,
    OfficialCalendarRegistry,
    canonical_manual_fill_bundle_path,
    consume_next_session_signal,
    create_alpha_next_session_signal,
    create_risk_next_session_signal,
    read_next_session_consumption,
    write_new_next_session_signal,
)
from research.strategy_workspace.paper_ledger_v2 import (
    CanonicalExecutionCostBundleV1,
    ControlledCloseMarkBundleV1,
    PaperDailySessionDraftV2,
    PaperCloseExecutionEvidenceV1,
    PaperExecutionAttemptV2,
    VerifiedPaperLedgerV2,
    append_paper_daily_session_v2,
    verify_paper_ledger_v2,
)
from research.strategy_workspace.portfolio_constructor_v2 import (
    ConstructionActionType,
    CurrentPosition,
    PortfolioConstructionError,
    PortfolioConstructionResult,
    PortfolioConstructorPolicy,
    PortfolioInstrument,
    construct_portfolio,
)
from trading.costs import FeeSchedule
from trading.integrity import account_fingerprint, execution_rule_bundle_sha256
from trading.models import (
    ADAPTIVE_EXPOSURE_V2_STRATEGY_ID,
    AccountSnapshot,
    InstrumentRule,
    MarketQuote,
    PortfolioIntent,
    PortfolioIntentType,
)


DAILY_DECISION_SCHEMA_VERSION = "daily-strategy-decision.v2"
FROZEN_DAILY_DATA_SCHEMA_VERSION = "frozen-daily-data.v2"
EXPOSURE_STATE_REGISTRY_ROOT = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "portfolio"
    / ADAPTIVE_EXPOSURE_V2_STRATEGY_ID
    / ".exposure-state-registry.v1"
)
LOCKED_TEST_START = date(2024, 1, 1)
LOCKED_TEST_END = date(2025, 12, 31)
ZERO = Decimal("0")
ONE = Decimal("1")
PCT = Decimal("0.00000001")
CHINA_STANDARD_TIME = timezone(timedelta(hours=8))
OFFICIAL_CLOSE_TIME = time(15, 0)


class DailyPipelineError(ValueError):
    """Raised when a daily decision cannot be frozen or replayed safely."""


class DailyPipelineIntegrityError(DailyPipelineError):
    """Raised for immutable-artifact collisions or short writes.

    These failures are deliberately never converted into a second decision;
    doing so could hide tampering or a concurrent writer.
    """


def _decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise DailyPipelineError(f"{field_name} must be decimal-like")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DailyPipelineError(f"{field_name} must be decimal-like") from exc
    if not parsed.is_finite():
        raise DailyPipelineError(f"{field_name} must be finite")
    return parsed


def _exposure(value: Any, field_name: str) -> Decimal:
    parsed = _decimal(value, field_name)
    if not ZERO <= parsed <= ONE:
        raise DailyPipelineError(f"{field_name} must be between zero and one")
    return parsed


def _sha256(value: Any, field_name: str) -> str:
    text = str(value)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise DailyPipelineError(f"{field_name} must be a lowercase SHA-256")
    return text


def _texts(values: Sequence[Any], field_name: str, *, required: bool = False) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise DailyPipelineError(f"{field_name} must be a sequence")
    normalized = tuple(str(value).strip() for value in values)
    if (required and not normalized) or any(not value for value in normalized):
        raise DailyPipelineError(f"{field_name} contains an empty value")
    if len(set(normalized)) != len(normalized):
        raise DailyPipelineError(f"{field_name} must not contain duplicates")
    return normalized


def _instrument(value: Any, field_name: str = "instrument_id") -> str:
    text = str(value).strip().upper()
    if not text or any(character.isspace() for character in text):
        raise DailyPipelineError(f"{field_name} must be a non-empty identifier")
    return text


def _strict_json_object(raw: bytes) -> dict[str, Any]:
    def hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise DailyPipelineError(f"duplicate JSON key: {key}")
            value[key] = item
        return value

    try:
        decoded = raw.decode("utf-8")
        value = json.loads(decoded, object_pairs_hook=hook)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DailyPipelineError("daily decision must be strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise DailyPipelineError("daily decision root must be an object")
    return value


@dataclass(frozen=True, slots=True)
class DailyOrderV1:
    instrument_id: str
    side: str
    quantity: int
    reference_price: Decimal
    target_weight: Decimal
    maximum_execution_price_deviation: Decimal
    cancel_conditions: tuple[str, ...]

    def __post_init__(self) -> None:
        instrument_id = _instrument(self.instrument_id)
        side = str(self.side).strip().upper()
        if side not in {"BUY", "SELL"}:
            raise DailyPipelineError("daily order side must be BUY or SELL")
        if type(self.quantity) is not int or self.quantity <= 0:
            raise DailyPipelineError("daily order quantity must be a positive integer")
        reference_price = _decimal(self.reference_price, "reference_price")
        if reference_price <= ZERO:
            raise DailyPipelineError("reference_price must be positive")
        target_weight = _exposure(self.target_weight, "target_weight")
        deviation = _exposure(
            self.maximum_execution_price_deviation,
            "maximum_execution_price_deviation",
        )
        object.__setattr__(self, "instrument_id", instrument_id)
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "reference_price", reference_price)
        object.__setattr__(self, "target_weight", target_weight)
        object.__setattr__(self, "maximum_execution_price_deviation", deviation)
        object.__setattr__(
            self,
            "cancel_conditions",
            _texts(self.cancel_conditions, "cancel_conditions", required=True),
        )

    @property
    def frozen_price_boundary(self) -> Decimal | None:
        """Return the enforced BUY ceiling.

        Risk-reducing SELL instructions deliberately have no frozen price
        floor: an adverse opening price must not turn a safe reduction into a
        hidden risk-increase veto.  Quote freshness, suspension, sellability,
        account state, rule-bundle drift and manual confirmation still apply.
        """

        if self.side == "SELL":
            return None
        return self.reference_price * (ONE + self.maximum_execution_price_deviation)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "instrument_id": self.instrument_id,
            "side": self.side,
            "quantity": self.quantity,
            "reference_price": str(self.reference_price),
            "target_weight": str(self.target_weight),
            "maximum_execution_price_deviation": str(
                self.maximum_execution_price_deviation
            ),
            "price_deviation_enforced": self.side == "BUY",
            "cancel_conditions": list(self.cancel_conditions),
        }
        if self.side == "BUY":
            payload["maximum_buy_price"] = str(self.frozen_price_boundary)
        return payload


@dataclass(frozen=True, slots=True)
class DailyHoldV1:
    instrument_id: str
    quantity: int
    target_quantity: int
    target_weight: Decimal
    reason: str

    def __post_init__(self) -> None:
        if type(self.quantity) is not int or self.quantity <= 0:
            raise DailyPipelineError("hold quantity must be positive")
        if type(self.target_quantity) is not int or self.target_quantity < 0:
            raise DailyPipelineError("hold target_quantity must be non-negative")
        reason = str(self.reason).strip()
        if not reason:
            raise DailyPipelineError("hold reason is required")
        object.__setattr__(self, "instrument_id", _instrument(self.instrument_id))
        object.__setattr__(self, "target_weight", _exposure(self.target_weight, "target_weight"))
        object.__setattr__(self, "reason", reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "quantity": self.quantity,
            "target_quantity": self.target_quantity,
            "target_weight": str(self.target_weight),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class DailyStrategyDecisionV2:
    strategy_date: date
    execution_date: date | None
    decision_status: str
    data_status: str
    market_regime: str
    portfolio_intent_type: str
    target_gross_exposure: Decimal
    feasible_gross_exposure: Decimal
    current_gross_exposure: Decimal | None
    realized_gross_exposure: Decimal | None
    target_stock_weights: Mapping[str, Decimal]
    feasible_stock_weights: Mapping[str, Decimal]
    current_stock_weights: Mapping[str, Decimal] | None
    realized_stock_weights: Mapping[str, Decimal] | None
    target_lot_quantities: Mapping[str, int]
    feasible_lot_quantities: Mapping[str, int]
    current_lot_quantities: Mapping[str, int] | None
    realized_lot_quantities: Mapping[str, int] | None
    buy_orders: tuple[DailyOrderV1, ...] = ()
    sell_orders: tuple[DailyOrderV1, ...] = ()
    hold_positions: tuple[DailyHoldV1, ...] = ()
    cash_weight: Decimal | None = ONE
    maximum_execution_price_deviation: Decimal = ZERO
    cancel_conditions: tuple[str, ...] = ()
    expected_cost: Decimal = ZERO
    model_reasons: tuple[str, ...] = ()
    risk_reasons: tuple[str, ...] = ()
    no_trade_reasons: tuple[str, ...] = ()
    data_sha256: str = ""
    model_sha256: str = ""
    policy_sha256: str = ""
    intent_sha256: str = ""
    failed_stage: str | None = None
    failure_codes: tuple[str, ...] = ()
    failure_receipt_sha256: str | None = None
    strategy_id: str = ADAPTIVE_EXPOSURE_V2_STRATEGY_ID
    schema_version: str = DAILY_DECISION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != DAILY_DECISION_SCHEMA_VERSION:
            raise DailyPipelineError("daily decision schema_version mismatch")
        if self.strategy_id != ADAPTIVE_EXPOSURE_V2_STRATEGY_ID:
            raise DailyPipelineError("daily decision strategy_id mismatch")
        if type(self.strategy_date) is not date or (
            self.execution_date is not None and type(self.execution_date) is not date
        ):
            raise DailyPipelineError(
                "strategy_date must be a date and execution_date a date or null"
            )
        if self.execution_date is not None and self.execution_date <= self.strategy_date:
            raise DailyPipelineError("execution_date must follow strategy_date")
        if LOCKED_TEST_START <= self.strategy_date <= LOCKED_TEST_END:
            raise DailyPipelineError(
                "experiment_v3_not_frozen_locked_test_execution_forbidden"
            )
        status = str(self.decision_status).strip().upper()
        if status not in {
            "READY_FOR_NEXT_SESSION_REVIEW",
            "NO_TRADE",
            "DATA_FAIL_CLOSED",
            "MANUAL_PAUSE",
            "BLOCKED",
        }:
            raise DailyPipelineError("daily decision_status is invalid")
        data_status = str(self.data_status).strip()
        if not data_status:
            raise DailyPipelineError("data_status is required")
        regime = str(self.market_regime).strip().upper()
        if regime not in {"RISK_OFF", "DEFENSIVE", "NEUTRAL", "RISK_ON"}:
            raise DailyPipelineError("market_regime is invalid")
        intent_type = str(self.portfolio_intent_type).strip().upper()
        try:
            PortfolioIntentType(intent_type)
        except ValueError as exc:
            raise DailyPipelineError("portfolio_intent_type is invalid") from exc

        target = _exposure(self.target_gross_exposure, "target_gross_exposure")
        feasible = _exposure(self.feasible_gross_exposure, "feasible_gross_exposure")
        current = (
            None
            if self.current_gross_exposure is None
            else _exposure(self.current_gross_exposure, "current_gross_exposure")
        )
        realized = (
            None
            if self.realized_gross_exposure is None
            else _exposure(self.realized_gross_exposure, "realized_gross_exposure")
        )
        cash = (
            None if self.cash_weight is None else _exposure(self.cash_weight, "cash_weight")
        )
        deviation = _exposure(
            self.maximum_execution_price_deviation,
            "maximum_execution_price_deviation",
        )
        cost = _decimal(self.expected_cost, "expected_cost")
        if cost < ZERO:
            raise DailyPipelineError("expected_cost must be non-negative")

        def normalize_weights(
            values: Mapping[str, Decimal] | None,
            field_name: str,
            gross: Decimal | None,
            *,
            enforce_target_limits: bool,
        ) -> dict[str, Decimal] | None:
            if values is None:
                if gross is not None:
                    raise DailyPipelineError(
                        f"{field_name} cannot be null when gross exposure is known"
                    )
                return None
            if gross is None:
                raise DailyPipelineError(
                    f"{field_name} must be null when gross exposure is unknown"
                )
            normalized = {
                _instrument(instrument_id): _exposure(weight, field_name)
                for instrument_id, weight in values.items()
            }
            if enforce_target_limits and (
                len(normalized) > 3
                or any(weight > Decimal("0.40") for weight in normalized.values())
            ):
                raise DailyPipelineError(
                    f"{field_name} violates the 3-name/40% hard limits"
                )
            if any(weight == ZERO for weight in normalized.values()):
                raise DailyPipelineError(f"zero {field_name} values must be omitted")
            rounding_tolerance = PCT * max(1, len(normalized))
            if sum(normalized.values(), ZERO) > gross + rounding_tolerance:
                raise DailyPipelineError(f"{field_name} exceeds its gross exposure")
            return normalized

        target_weights = normalize_weights(
            self.target_stock_weights,
            "target stock weight",
            target,
            enforce_target_limits=True,
        )
        feasible_weights = normalize_weights(
            self.feasible_stock_weights,
            "feasible stock weight",
            feasible,
            enforce_target_limits=False,
        )
        current_weights = normalize_weights(
            self.current_stock_weights,
            "current stock weight",
            current,
            enforce_target_limits=False,
        )
        realized_weights = normalize_weights(
            self.realized_stock_weights,
            "realized stock weight",
            realized,
            enforce_target_limits=False,
        )

        def normalize_quantities(
            values: Mapping[str, int] | None,
            field_name: str,
            weight_keys: set[str] | None,
            *,
            allow_zero: bool = False,
        ) -> dict[str, int] | None:
            if values is None:
                if weight_keys is not None:
                    raise DailyPipelineError(
                        f"{field_name} cannot be null when weights are known"
                    )
                return None
            if weight_keys is None and status != "BLOCKED":
                raise DailyPipelineError(
                    f"{field_name} must be null when weights are unknown"
                )
            normalized: dict[str, int] = {}
            for instrument_id, quantity in values.items():
                normalized_id = _instrument(instrument_id)
                if weight_keys is not None and normalized_id not in weight_keys:
                    raise DailyPipelineError(
                        f"{field_name} has no corresponding positive stock weight"
                    )
                minimum = 0 if allow_zero else 1
                if type(quantity) is not int or quantity < minimum:
                    raise DailyPipelineError(
                        f"{field_name} contains an invalid integer quantity"
                    )
                normalized[normalized_id] = quantity
            if weight_keys is not None and set(normalized) != weight_keys:
                raise DailyPipelineError(
                    f"{field_name} and its stock weights must share keys"
                )
            return normalized

        target_quantities = normalize_quantities(
            self.target_lot_quantities,
            "target lot quantities",
            set(target_weights),
            allow_zero=True,
        )
        feasible_quantities = normalize_quantities(
            self.feasible_lot_quantities,
            "feasible lot quantities",
            set(feasible_weights),
        )
        current_quantities = normalize_quantities(
            self.current_lot_quantities,
            "current lot quantities",
            None if current_weights is None else set(current_weights),
        )
        realized_quantities = normalize_quantities(
            self.realized_lot_quantities,
            "realized lot quantities",
            None if realized_weights is None else set(realized_weights),
        )
        if (
            realized is not None
            or realized_weights is not None
            or realized_quantities is not None
        ):
            raise DailyPipelineError(
                "daily decision realized fields must remain null until D+1 close"
            )

        buys = tuple(self.buy_orders)
        sells = tuple(self.sell_orders)
        holds = tuple(self.hold_positions)
        if any(not isinstance(item, DailyOrderV1) or item.side != "BUY" for item in buys):
            raise DailyPipelineError("buy_orders must contain BUY DailyOrderV1 values")
        if any(not isinstance(item, DailyOrderV1) or item.side != "SELL" for item in sells):
            raise DailyPipelineError("sell_orders must contain SELL DailyOrderV1 values")
        if any(not isinstance(item, DailyHoldV1) for item in holds):
            raise DailyPipelineError("hold_positions must contain DailyHoldV1 values")
        action_ids = [item.instrument_id for item in (*buys, *sells, *holds)]
        if len(action_ids) != len(set(action_ids)):
            raise DailyPipelineError("one instrument cannot have multiple daily actions")
        if status in {"DATA_FAIL_CLOSED", "MANUAL_PAUSE"} and buys:
            raise DailyPipelineError("DATA_FAIL_CLOSED and MANUAL_PAUSE cannot contain BUY")
        failed_stage = (
            None if self.failed_stage is None else str(self.failed_stage).strip().upper()
        )
        failure_codes = _texts(self.failure_codes, "failure_codes")
        failure_receipt = (
            None
            if self.failure_receipt_sha256 is None
            else _sha256(
                self.failure_receipt_sha256,
                "failure_receipt_sha256",
            )
        )
        if status == "BLOCKED":
            if (
                not failed_stage
                or not failure_codes
                or failure_receipt is None
                or buys
                or sells
                or holds
                or target != ZERO
                or feasible != ZERO
            ):
                raise DailyPipelineError(
                    "BLOCKED decision requires failure evidence and zero orders/targets"
                )
        elif (
            failed_stage is not None
            or failure_codes
            or failure_receipt is not None
            or self.execution_date is None
            or current is None
            or cash is None
            or current_weights is None
            or current_quantities is None
        ):
            raise DailyPipelineError(
                "non-BLOCKED decisions require complete current/target facts and no failure receipt"
            )

        object.__setattr__(self, "decision_status", status)
        object.__setattr__(self, "data_status", data_status)
        object.__setattr__(self, "market_regime", regime)
        object.__setattr__(self, "portfolio_intent_type", intent_type)
        object.__setattr__(self, "target_gross_exposure", target)
        object.__setattr__(self, "feasible_gross_exposure", feasible)
        object.__setattr__(self, "current_gross_exposure", current)
        object.__setattr__(self, "realized_gross_exposure", realized)
        object.__setattr__(self, "cash_weight", cash)
        object.__setattr__(self, "maximum_execution_price_deviation", deviation)
        object.__setattr__(self, "expected_cost", cost)
        for field_name, value in (
            ("target_stock_weights", target_weights),
            ("feasible_stock_weights", feasible_weights),
            ("current_stock_weights", current_weights),
            ("realized_stock_weights", realized_weights),
            ("target_lot_quantities", target_quantities),
            ("feasible_lot_quantities", feasible_quantities),
            ("current_lot_quantities", current_quantities),
            ("realized_lot_quantities", realized_quantities),
        ):
            object.__setattr__(
                self,
                field_name,
                (
                    None
                    if value is None
                    else MappingProxyType(dict(sorted(value.items())))
                ),
            )
        object.__setattr__(self, "buy_orders", tuple(sorted(buys, key=lambda item: item.instrument_id)))
        object.__setattr__(self, "sell_orders", tuple(sorted(sells, key=lambda item: item.instrument_id)))
        object.__setattr__(self, "hold_positions", tuple(sorted(holds, key=lambda item: item.instrument_id)))
        object.__setattr__(
            self,
            "cancel_conditions",
            _texts(self.cancel_conditions, "cancel_conditions", required=True),
        )
        object.__setattr__(self, "model_reasons", _texts(self.model_reasons, "model_reasons"))
        object.__setattr__(self, "risk_reasons", _texts(self.risk_reasons, "risk_reasons"))
        object.__setattr__(
            self,
            "no_trade_reasons",
            _texts(self.no_trade_reasons, "no_trade_reasons"),
        )
        object.__setattr__(self, "data_sha256", _sha256(self.data_sha256, "data_sha256"))
        object.__setattr__(self, "model_sha256", _sha256(self.model_sha256, "model_sha256"))
        object.__setattr__(self, "policy_sha256", _sha256(self.policy_sha256, "policy_sha256"))
        object.__setattr__(self, "intent_sha256", _sha256(self.intent_sha256, "intent_sha256"))
        object.__setattr__(self, "failed_stage", failed_stage)
        object.__setattr__(self, "failure_codes", failure_codes)
        object.__setattr__(self, "failure_receipt_sha256", failure_receipt)

    def content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "strategy_id": self.strategy_id,
            "strategy_date": self.strategy_date,
            "execution_date": self.execution_date,
            "decision_status": self.decision_status,
            "data_status": self.data_status,
            "market_regime": self.market_regime,
            "portfolio_intent_type": self.portfolio_intent_type,
            "target_gross_exposure": self.target_gross_exposure,
            "feasible_gross_exposure": self.feasible_gross_exposure,
            "current_gross_exposure": self.current_gross_exposure,
            "realized_gross_exposure": self.realized_gross_exposure,
            "target_stock_weights": dict(self.target_stock_weights),
            "feasible_stock_weights": dict(self.feasible_stock_weights),
            "current_stock_weights": (
                None
                if self.current_stock_weights is None
                else dict(self.current_stock_weights)
            ),
            "realized_stock_weights": (
                None
                if self.realized_stock_weights is None
                else dict(self.realized_stock_weights)
            ),
            "target_lot_quantities": dict(self.target_lot_quantities),
            "feasible_lot_quantities": dict(self.feasible_lot_quantities),
            "current_lot_quantities": (
                None
                if self.current_lot_quantities is None
                else dict(self.current_lot_quantities)
            ),
            "realized_lot_quantities": (
                None
                if self.realized_lot_quantities is None
                else dict(self.realized_lot_quantities)
            ),
            "buy_orders": [item.to_dict() for item in self.buy_orders],
            "sell_orders": [item.to_dict() for item in self.sell_orders],
            "hold_positions": [item.to_dict() for item in self.hold_positions],
            "cash_weight": self.cash_weight,
            "maximum_execution_price_deviation": self.maximum_execution_price_deviation,
            "cancel_conditions": list(self.cancel_conditions),
            "expected_cost": self.expected_cost,
            "model_reasons": list(self.model_reasons),
            "risk_reasons": list(self.risk_reasons),
            "no_trade_reasons": list(self.no_trade_reasons),
            "data_sha256": self.data_sha256,
            "model_sha256": self.model_sha256,
            "policy_sha256": self.policy_sha256,
            "intent_sha256": self.intent_sha256,
            "failed_stage": self.failed_stage,
            "failure_codes": list(self.failure_codes),
            "failure_receipt_sha256": self.failure_receipt_sha256,
            "returns_net_of_full_costs": True,
            "automatic_order_submission": False,
            "paper_eligibility": False,
            "trade_eligibility": False,
            "real_money_list_allowed": False,
            "live_supported": False,
        }

    @property
    def decision_sha256(self) -> str:
        return canonical_sha256(self.content_dict())

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_dict(), "decision_sha256": self.decision_sha256}


def _instrument_rule_dict(rule: InstrumentRule) -> dict[str, Any]:
    return {
        "instrument_id": rule.instrument_id,
        "name": rule.name,
        "instrument_type": rule.instrument_type,
        "lot_size": rule.lot_size,
        "tick_size": rule.tick_size,
        "sell_stamp_duty_rate": rule.sell_stamp_duty_rate,
        "t_plus_one": rule.t_plus_one,
    }


@dataclass(frozen=True, slots=True)
class ControlledHeldPositionReferenceV1:
    """Controlled D-close reference for a strategy holding outside Alpha's pool."""

    instrument_id: str
    session_date: date
    available_at: datetime
    close: Decimal
    source_record_sha256: str

    def __post_init__(self) -> None:
        instrument_id = _instrument(self.instrument_id)
        if type(self.session_date) is not date:
            raise DailyPipelineError("held-position session_date must be a date")
        if not isinstance(self.available_at, datetime) or self.available_at.tzinfo is None:
            raise DailyPipelineError("held-position available_at must be timezone-aware")
        close = _decimal(self.close, "held-position close")
        if close <= ZERO:
            raise DailyPipelineError("held-position close must be positive")
        object.__setattr__(self, "instrument_id", instrument_id)
        object.__setattr__(self, "close", close)
        object.__setattr__(
            self,
            "source_record_sha256",
            _sha256(self.source_record_sha256, "held-position source_record_sha256"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "session_date": self.session_date,
            "available_at": self.available_at,
            "close": self.close,
            "source_record_sha256": self.source_record_sha256,
        }


@dataclass(frozen=True, slots=True)
class FrozenDailyDataV2:
    """Create-only updater output consumed by the after-close pipeline.

    The Alpha snapshot remains the PIT truth source.  Four market observations
    are supplied by controlled adapters.  Alpha distribution and account
    drawdown are derived inside the pipeline, so callers cannot inject either
    risk input.  Held-position references cover strategy-owned names that have
    left the current Alpha universe; they are never buy-eligible.
    """

    update_id: str
    alpha_snapshot: ControlledPitSnapshotV2
    non_alpha_exposure_metrics: tuple[ExposureMetricV2, ...]
    instrument_rules: Mapping[str, InstrumentRule]
    held_position_references: tuple[ControlledHeldPositionReferenceV1, ...] = ()
    data_update_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        update_id = str(self.update_id).strip()
        if not update_id or any(character.isspace() for character in update_id):
            raise DailyPipelineError("update_id must be a non-empty identifier")
        if not isinstance(self.alpha_snapshot, ControlledPitSnapshotV2):
            raise DailyPipelineError("alpha_snapshot must be ControlledPitSnapshotV2")
        metrics = tuple(self.non_alpha_exposure_metrics)
        if any(not isinstance(item, ExposureMetricV2) for item in metrics):
            raise DailyPipelineError(
                "non_alpha_exposure_metrics must contain ExposureMetricV2"
            )
        expected = set(ExposureInputCategory) - {
            ExposureInputCategory.ALPHA_PREDICTION_DISTRIBUTION,
            ExposureInputCategory.ACCOUNT_DRAWDOWN,
        }
        categories = {item.category for item in metrics}
        if len(metrics) != len(expected) or categories != expected:
            raise DailyPipelineError(
                "daily updater must supply exactly the four controlled market exposure metrics"
            )
        ordered_metrics = tuple(sorted(metrics, key=lambda item: item.category.value))
        held_references = tuple(
            sorted(self.held_position_references, key=lambda item: item.instrument_id)
        )
        if any(
            not isinstance(item, ControlledHeldPositionReferenceV1)
            for item in held_references
        ):
            raise DailyPipelineError(
                "held_position_references must contain controlled references"
            )
        held_ids = [item.instrument_id for item in held_references]
        if len(held_ids) != len(set(held_ids)):
            raise DailyPipelineError("held-position references must be unique")
        member_ids = set(self.alpha_snapshot.member_ids)
        if member_ids.intersection(held_ids):
            raise DailyPipelineError(
                "held-position references are only for names outside the Alpha universe"
            )
        strategy_session = self.alpha_snapshot.decision_at.astimezone(
            CHINA_STANDARD_TIME
        ).date()
        for item in held_references:
            if item.session_date != strategy_session:
                raise DailyPipelineError(
                    "held-position references must be from the D strategy session"
                )
            if item.available_at > self.alpha_snapshot.decision_at:
                raise DailyPipelineError(
                    "future held-position references are forbidden"
                )
        rules = dict(self.instrument_rules)
        if set(rules) != member_ids.union(held_ids):
            raise DailyPipelineError(
                "canonical InstrumentRule mapping must exactly cover Alpha plus held-position references"
            )
        for instrument_id, rule in rules.items():
            if not isinstance(rule, InstrumentRule) or rule.instrument_id != instrument_id:
                raise DailyPipelineError("canonical InstrumentRule mapping is malformed")
        object.__setattr__(self, "update_id", update_id)
        object.__setattr__(self, "non_alpha_exposure_metrics", ordered_metrics)
        object.__setattr__(self, "held_position_references", held_references)
        object.__setattr__(
            self, "instrument_rules", MappingProxyType(dict(sorted(rules.items())))
        )
        object.__setattr__(
            self,
            "data_update_sha256",
            canonical_sha256(self.to_content_dict()),
        )

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": FROZEN_DAILY_DATA_SCHEMA_VERSION,
            "update_id": self.update_id,
            "alpha_snapshot_sha256": self.alpha_snapshot.input_snapshot_sha256,
            "non_alpha_exposure_metrics": [
                item.to_dict() for item in self.non_alpha_exposure_metrics
            ],
            "instrument_rules": [
                _instrument_rule_dict(item)
                for item in self.instrument_rules.values()
            ],
            "held_position_references": [
                item.to_dict() for item in self.held_position_references
            ],
            "source_authentication": (
                "external_controlled_adapters_required_hash_is_not_source_proof"
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.to_content_dict(),
            "data_update_sha256": self.data_update_sha256,
        }


@runtime_checkable
class DailyDataUpdaterV2(Protocol):
    def update_and_freeze(self, strategy_date: date) -> FrozenDailyDataV2:
        """Update controlled sources and return one immutable D-close envelope."""


@dataclass(frozen=True, slots=True)
class DailyPipelineRunV1:
    frozen_data: FrozenDailyDataV2
    alpha_ranking: AlphaRankingV2
    exposure_inputs: ExposureInputSnapshotV2
    exposure_decision: ExposureDecisionV2
    construction: PortfolioConstructionResult | None
    portfolio_intent: PortfolioIntent
    next_session_signal: NextSessionSignal | None
    daily_decision: DailyStrategyDecisionV2
    artifacts: "DailyDecisionArtifacts"
    evidence_artifacts: "DailyEvidenceArtifacts"
    daily_signal_admission_receipt: DailySignalAdmissionReceiptV1 | None
    daily_signal_publication_receipt: DailySignalPublicationReceiptV1 | None
    signal_path: Path | None


@dataclass(frozen=True, slots=True)
class DailyPipelineBlockedRunV1:
    """Auditable zero-order result when stage 1 cannot produce typed data."""

    portfolio_intent: PortfolioIntent
    daily_decision: DailyStrategyDecisionV2
    artifacts: "DailyDecisionArtifacts"
    failure_receipt_path: Path
    exposure_inputs: ExposureInputSnapshotV2 | None = None
    exposure_decision: ExposureDecisionV2 | None = None
    daily_signal_admission_receipt: DailySignalAdmissionReceiptV1 | None = None
    daily_signal_publication_receipt: DailySignalPublicationReceiptV1 | None = None
    signal_path: None = None


@dataclass(frozen=True, slots=True)
class DailyDecisionArtifacts:
    json_path: Path
    markdown_path: Path
    notification_path: Path
    decision_sha256: str


@dataclass(frozen=True, slots=True)
class DailyEvidenceArtifacts:
    frozen_data_path: Path
    pit_snapshot_path: Path
    alpha_ranking_path: Path
    exposure_inputs_path: Path
    exposure_decision_path: Path
    exposure_state_path: Path
    portfolio_construction_path: Path | None
    portfolio_intent_path: Path
    approved_factor_registry_path: Path
    experiment_v3_admission_receipt_path: Path
    experiment_v3_evidence_path: Path
    experiment_v3_evidence_sha256: str


def render_daily_decision_markdown(decision: DailyStrategyDecisionV2) -> str:
    if not isinstance(decision, DailyStrategyDecisionV2):
        raise DailyPipelineError("decision must be DailyStrategyDecisionV2")

    def order_lines(items: Sequence[DailyOrderV1]) -> list[str]:
        if not items:
            return ["- 无"]
        lines: list[str] = []
        for item in items:
            boundary = (
                f"BUY 冻结最高价 {item.frozen_price_boundary}"
                if item.side == "BUY"
                else "SELL 不以价格偏离取消风险减仓"
            )
            lines.append(
                f"- {item.side} `{item.instrument_id}` {item.quantity} 股；"
                f"参考价 {item.reference_price}；{boundary}；"
                f"可实现权重 {item.target_weight}"
            )
        return lines

    hold_lines = (
        [
            f"- HOLD `{item.instrument_id}` {item.quantity} 股；目标 {item.target_quantity} 股；{item.reason}"
            for item in decision.hold_positions
        ]
        or ["- 无"]
    )
    text = [
        "# Adaptive Exposure V2 日终决策",
        "",
        f"- strategy_date: `{decision.strategy_date.isoformat()}`",
        f"- execution_date: `{decision.execution_date.isoformat() if decision.execution_date is not None else 'UNKNOWN'}`",
        f"- decision_status: `{decision.decision_status}`",
        f"- data_status: `{decision.data_status}`",
        f"- market_regime: `{decision.market_regime}`",
        f"- portfolio_intent_type: `{decision.portfolio_intent_type}`",
        f"- target_gross_exposure: `{decision.target_gross_exposure}`",
        f"- feasible_gross_exposure: `{decision.feasible_gross_exposure}`",
        f"- current_gross_exposure: `{decision.current_gross_exposure if decision.current_gross_exposure is not None else 'UNKNOWN'}`",
        f"- realized_gross_exposure: `{decision.realized_gross_exposure if decision.realized_gross_exposure is not None else 'PENDING_D_PLUS_ONE_CLOSE'}`",
        f"- CASH: `{decision.cash_weight if decision.cash_weight is not None else 'UNKNOWN'}`",
        f"- expected_cost: `{decision.expected_cost}`（完整成本口径）",
        "",
        "## BUY",
        "",
        *order_lines(decision.buy_orders),
        "",
        "## SELL",
        "",
        *order_lines(decision.sell_orders),
        "",
        "## HOLD",
        "",
        *hold_lines,
        "",
        "## 取消条件",
        "",
        *[f"- {item}" for item in decision.cancel_conditions],
        "",
        "## 原因",
        "",
        f"- model_reasons: {', '.join(decision.model_reasons) or '无'}",
        f"- risk_reasons: {', '.join(decision.risk_reasons) or '无'}",
        f"- no_trade_reasons: {', '.join(decision.no_trade_reasons) or '无'}",
        f"- failed_stage: {decision.failed_stage or '无'}",
        f"- failure_codes: {', '.join(decision.failure_codes) or '无'}",
        "",
        "## 哈希与安全边界",
        "",
        f"- data_sha256: `{decision.data_sha256}`",
        f"- model_sha256: `{decision.model_sha256}`",
        f"- policy_sha256: `{decision.policy_sha256}`",
        f"- intent_sha256: `{decision.intent_sha256}`",
        f"- failure_receipt_sha256: `{decision.failure_receipt_sha256 or '无'}`",
        f"- decision_sha256: `{decision.decision_sha256}`",
        "- Paper eligibility: `false`",
        "- Trade eligibility: `false`",
        "- Real-money list allowed: `false`",
        "- LIVE: `not_supported`",
        "- 本计划仅供 D+1 人工复核，不自动提交订单。",
        "",
    ]
    return "\n".join(text)


def _write_create_only(path: Path, payload: bytes) -> None:
    if path.exists():
        if not path.is_file() or path.is_symlink() or path.read_bytes() != payload:
            raise DailyPipelineIntegrityError(
                f"immutable artifact collision: {path.name}"
            )
        return
    try:
        with path.open("xb") as handle:
            written = handle.write(payload)
            handle.flush()
    except FileExistsError:
        if not path.is_file() or path.is_symlink() or path.read_bytes() != payload:
            raise DailyPipelineIntegrityError(
                f"immutable artifact changed concurrently: {path.name}"
            )
        return
    if written != len(payload):
        raise DailyPipelineIntegrityError(
            f"short write for immutable artifact: {path.name}"
        )


def _exposure_state_registry_directory(*, create: bool) -> Path:
    """Return the single strategy-level state root, independent of report paths."""

    root = Path(EXPOSURE_STATE_REGISTRY_ROOT)
    if create:
        root.mkdir(parents=True, exist_ok=True)
    if root.exists() and (not root.is_dir() or root.is_symlink()):
        raise DailyPipelineIntegrityError(
            "exposure state registry root must be a regular directory"
        )
    return root


def _register_exposure_state_artifacts(
    *,
    strategy_date: date,
    daily_decision: DailyStrategyDecisionV2,
    exposure_inputs: ExposureInputSnapshotV2 | None = None,
    exposure_decision: ExposureDecisionV2 | None = None,
    failure_receipt: Mapping[str, Any] | None = None,
) -> None:
    """Copy canonical continuity evidence into the fixed strategy registry."""

    root = _exposure_state_registry_directory(create=True)
    stem = strategy_date.isoformat()
    _write_create_only(
        root / f"{stem}.daily-decision.json",
        canonical_json_bytes(daily_decision.to_dict()) + b"\n",
    )
    if exposure_inputs is not None:
        _write_create_only(
            root / f"{stem}.exposure-inputs.json",
            canonical_json_bytes(exposure_inputs.to_dict()) + b"\n",
        )
    if exposure_decision is not None:
        _write_create_only(
            root / f"{stem}.exposure-decision.json",
            canonical_json_bytes(exposure_decision.to_dict()) + b"\n",
        )
        _write_create_only(
            root / f"{stem}.exposure-state.json",
            canonical_json_bytes(exposure_decision.next_state_memory.to_dict())
            + b"\n",
        )
    if failure_receipt is not None:
        _write_create_only(
            root / f"{stem}.pipeline-failure.json",
            canonical_json_bytes(failure_receipt) + b"\n",
        )


def write_daily_decision(
    output_directory: str | Path,
    decision: DailyStrategyDecisionV2,
) -> DailyDecisionArtifacts:
    """Write deterministic JSON, Markdown, and a local notification outbox item.

    The outbox is delivery evidence only.  It does not claim that an external
    channel was configured or that a recipient saw the message.
    """

    if not isinstance(decision, DailyStrategyDecisionV2):
        raise DailyPipelineError("decision must be DailyStrategyDecisionV2")
    output = Path(output_directory)
    if not output.exists() or not output.is_dir() or output.is_symlink():
        raise DailyPipelineError("daily decision output directory must already exist")
    stem = decision.strategy_date.isoformat()
    json_path = output / f"{stem}.daily-decision.json"
    markdown_path = output / f"{stem}.daily-decision.md"
    notification_path = output / f"{stem}.notification-outbox.json"
    json_payload = canonical_json_bytes(decision.to_dict()) + b"\n"
    markdown_payload = render_daily_decision_markdown(decision).encode("utf-8")
    notification_payload = canonical_json_bytes(
        {
            "schema_version": "local-notification-outbox.v1",
            "strategy_date": decision.strategy_date,
            "decision_sha256": decision.decision_sha256,
            "status": "PENDING_EXTERNAL_DELIVERY_NOT_CONFIGURED",
            "automatic_order_submission": False,
        }
    ) + b"\n"
    _write_create_only(json_path, json_payload)
    _write_create_only(markdown_path, markdown_payload)
    _write_create_only(notification_path, notification_payload)
    return DailyDecisionArtifacts(
        json_path=json_path,
        markdown_path=markdown_path,
        notification_path=notification_path,
        decision_sha256=decision.decision_sha256,
    )


def _reason_code(value: Any) -> str:
    raw = re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")
    if not raw:
        raw = "unspecified_reason"
    if not raw[0].isalpha():
        raw = f"reason_{raw}"
    if len(raw) > 64:
        suffix = canonical_sha256({"reason": str(value)})[:8]
        raw = f"{raw[:55].rstrip('_')}_{suffix}"
    return raw


def _unique_texts(values: Sequence[Any]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return tuple(result)


def _alpha_distribution_metric(ranking: AlphaRankingV2) -> ExposureMetricV2:
    predictions = [
        item.predicted_return
        for item in ranking.rows
        if item.eligibility and item.predicted_return is not None
    ]
    if ranking.status is AlphaRunStatus.OK and predictions:
        return ExposureMetricV2(
            category=ExposureInputCategory.ALPHA_PREDICTION_DISTRIBUTION,
            status=ExposureMetricStatus.OK,
            value=float(median(predictions)),
            observation_session=ranking.decision_at.astimezone(
                CHINA_STANDARD_TIME
            ).date(),
            available_at=ranking.decision_at,
            source_snapshot_sha256=ranking.ranking_sha256,
        )
    failure = (
        "NO_ELIGIBLE_ALPHA"
        if ranking.status is AlphaRunStatus.NO_ALPHA_CASH
        else "ALPHA_DATA_FAIL_CLOSED"
    )
    return ExposureMetricV2(
        category=ExposureInputCategory.ALPHA_PREDICTION_DISTRIBUTION,
        status=ExposureMetricStatus.DATA_FAILED,
        value=None,
        observation_session=None,
        available_at=None,
        source_snapshot_sha256=None,
        failure_codes=(failure,),
    )


def _latest_controlled_references(
    snapshot: ControlledPitSnapshotV2,
    *,
    strategy_date: date,
) -> tuple[dict[str, Decimal], dict[str, date]]:
    prices: dict[str, Decimal] = {}
    sessions: dict[str, date] = {}
    for instrument in snapshot.instruments:
        eligible_bars = [
            item
            for item in instrument.price_bars
            if item.session_date <= strategy_date
            and item.available_at <= snapshot.decision_at
        ]
        if not eligible_bars:
            continue
        latest = max(
            eligible_bars,
            key=lambda item: (item.session_date, item.available_at, item.source_record_id),
        )
        price = _decimal(latest.close, f"reference close {instrument.instrument_id}")
        if price <= ZERO:
            raise DailyPipelineError("controlled reference close must be positive")
        prices[instrument.instrument_id] = price
        sessions[instrument.instrument_id] = latest.session_date
    return prices, sessions


def _portfolio_instruments(
    *,
    ranking: AlphaRankingV2,
    frozen_data: FrozenDailyDataV2,
    strategy_date: date,
) -> tuple[PortfolioInstrument, ...]:
    prices, sessions = _latest_controlled_references(
        frozen_data.alpha_snapshot,
        strategy_date=strategy_date,
    )
    rows: list[PortfolioInstrument] = []
    for item in ranking.rows:
        if item.instrument_id not in prices:
            continue
        exclusions = {_reason_code(code) for code in item.exclusion_codes}
        eligibility = item.eligibility
        if sessions[item.instrument_id] != strategy_date:
            # A stale close may explain Alpha ineligibility, but it is not a D
            # mark and therefore cannot value a holding or size an order.
            continue
        if not eligibility and not exclusions:
            exclusions.add("alpha_ineligible")
        rows.append(
            PortfolioInstrument(
                instrument_id=item.instrument_id,
                predicted_return=(
                    None
                    if item.predicted_return is None
                    else Decimal(str(item.predicted_return))
                ),
                percentile=(
                    None if item.percentile is None else Decimal(str(item.percentile))
                ),
                eligibility=eligibility,
                exclusion_codes=tuple(sorted(exclusions)),
                reference_price=prices[item.instrument_id],
                lot_size=frozen_data.instrument_rules[item.instrument_id].lot_size,
            )
        )
    for reference in frozen_data.held_position_references:
        rows.append(
            PortfolioInstrument(
                instrument_id=reference.instrument_id,
                predicted_return=None,
                percentile=None,
                eligibility=False,
                exclusion_codes=("not_in_current_alpha_universe",),
                reference_price=reference.close,
                lot_size=frozen_data.instrument_rules[
                    reference.instrument_id
                ].lot_size,
            )
        )
    if not rows:
        raise DailyPipelineError(
            "no controlled reference price is available for portfolio construction"
        )
    return tuple(rows)


def _current_portfolio_metrics(
    account: AccountSnapshot,
    instruments: Sequence[PortfolioInstrument],
) -> tuple[Decimal, dict[str, Decimal], Decimal]:
    by_id = {item.instrument_id: item for item in instruments}
    missing = sorted(set(account.positions) - set(by_id))
    if missing:
        raise DailyPipelineError(
            "strategy positions lack controlled PIT/rule/reference rows: "
            + ",".join(missing)
        )
    values = {
        instrument_id: by_id[instrument_id].reference_price * position.quantity
        for instrument_id, position in account.positions.items()
        if position.quantity > 0
    }
    nav = account.cash + sum(values.values(), ZERO)
    if nav <= ZERO:
        raise DailyPipelineError("strategy account NAV must be positive")
    weights = {
        instrument_id: (value / nav).quantize(PCT)
        for instrument_id, value in sorted(values.items())
    }
    gross = (sum(values.values(), ZERO) / nav).quantize(PCT)
    return gross, weights, nav


def _validate_exposure_memory_provenance(
    *,
    output_directory: Path,
    strategy_date: date,
    previous_session: date | None,
    memory: ExposureStateMemoryV2,
) -> None:
    """Require memory from the fixed strategy registry, never a caller-chosen path."""

    # Kept in the private signature for compatibility with existing callers;
    # report directories are deliberately not continuity authorities.
    _ = output_directory

    controlled_history_suffixes = (
        ".daily-decision.json",
        ".pipeline-failure.json",
        ".exposure-inputs.json",
        ".exposure-decision.json",
        ".exposure-state.json",
    )

    def controlled_artifact_date(path: Path) -> date | None:
        for suffix in controlled_history_suffixes:
            if not path.name.endswith(suffix):
                continue
            raw_date = path.name[: -len(suffix)]
            try:
                parsed = date.fromisoformat(raw_date)
            except ValueError:
                return None
            return parsed if parsed.isoformat() == raw_date else None
        return None

    registry_directory = _exposure_state_registry_directory(create=False)
    registry_entries = (
        tuple(registry_directory.iterdir())
        if registry_directory.exists()
        else ()
    )
    other_day_controlled_artifacts = tuple(
        sorted(
            (
                path
                for path in registry_entries
                if (artifact_date := controlled_artifact_date(path)) is not None
                and artifact_date != strategy_date
            ),
            key=lambda path: path.name,
        )
    )

    if memory.last_decision_at is None:
        if (
            memory.current_state is not ExposureState.NEUTRAL
            or memory.pending_state is not None
            or memory.pending_consecutive_sessions != 0
            or memory.last_input_snapshot_sha256 is not None
        ):
            raise DailyPipelineError(
                "bootstrap exposure memory must be pristine NEUTRAL state"
            )
        if other_day_controlled_artifacts:
            raise DailyPipelineError(
                "bootstrap exposure memory is forbidden after prior controlled "
                "artifacts; recover the canonical preceding state or remain blocked"
            )
        return
    if previous_session is None:
        raise DailyPipelineError(
            "non-bootstrap exposure memory has no preceding official session"
        )
    if previous_session >= strategy_date:
        raise DailyPipelineError(
            "preceding exposure session must be earlier than strategy_date"
        )
    if (
        memory.last_decision_at.astimezone(CHINA_STANDARD_TIME).date()
        != previous_session
    ):
        raise DailyPipelineError(
            "exposure memory is not from the preceding CST strategy session"
        )
    state_path = registry_directory / f"{previous_session.isoformat()}.exposure-state.json"
    inputs_path = registry_directory / f"{previous_session.isoformat()}.exposure-inputs.json"
    decision_path = (
        registry_directory / f"{previous_session.isoformat()}.exposure-decision.json"
    )
    daily_path = registry_directory / f"{previous_session.isoformat()}.daily-decision.json"
    if not daily_path.is_file() or daily_path.is_symlink():
        raise DailyPipelineError(
            "exposure hysteresis memory lacks the preceding immutable daily decision"
        )
    daily_raw = daily_path.read_bytes()
    daily_payload = _strict_json_object(daily_raw)
    if daily_raw != canonical_json_bytes(daily_payload) + b"\n":
        raise DailyPipelineError("preceding daily decision is not canonical JSON")
    persisted_daily_sha = daily_payload.pop("decision_sha256", None)
    if (
        daily_payload.get("schema_version") != DAILY_DECISION_SCHEMA_VERSION
        or daily_payload.get("strategy_id") != ADAPTIVE_EXPOSURE_V2_STRATEGY_ID
        or daily_payload.get("strategy_date") != previous_session.isoformat()
        or persisted_daily_sha is None
        or canonical_sha256(daily_payload) != persisted_daily_sha
    ):
        raise DailyPipelineError(
            "preceding daily decision cannot prove its canonical identity"
        )
    previous_was_blocked = daily_payload.get("decision_status") == "BLOCKED"
    if (
        not state_path.is_file()
        or state_path.is_symlink()
        or not inputs_path.is_file()
        or inputs_path.is_symlink()
        or not decision_path.is_file()
        or decision_path.is_symlink()
    ):
        raise DailyPipelineError(
            "exposure hysteresis memory lacks preceding immutable artifacts"
        )
    expected_state_bytes = canonical_json_bytes(memory.to_dict()) + b"\n"
    if state_path.read_bytes() != expected_state_bytes:
        raise DailyPipelineError(
            "exposure hysteresis memory differs from preceding state artifact"
        )
    if previous_was_blocked and (
        memory.current_state is not ExposureState.RISK_OFF
        or memory.pending_state is not None
        or memory.pending_consecutive_sessions != 0
    ):
        raise DailyPipelineError(
            "a BLOCKED prior day may continue only from its canonical RISK_OFF state"
        )
    if daily_payload.get("market_regime") != memory.current_state.value:
        raise DailyPipelineError(
            "preceding daily decision does not bind the supplied exposure state"
        )
    inputs_raw = inputs_path.read_bytes()
    inputs_payload = _strict_json_object(inputs_raw)
    if inputs_raw != canonical_json_bytes(inputs_payload) + b"\n":
        raise DailyPipelineError("preceding exposure inputs are not canonical JSON")
    persisted_input_sha = inputs_payload.pop("input_snapshot_sha256", None)
    if (
        inputs_payload.get("schema_version") != EXPOSURE_INPUT_SCHEMA_VERSION
        or persisted_input_sha is None
        or canonical_sha256(inputs_payload) != persisted_input_sha
        or persisted_input_sha != memory.last_input_snapshot_sha256
    ):
        raise DailyPipelineError(
            "preceding exposure inputs do not bind the supplied state memory"
        )
    decision_raw = decision_path.read_bytes()
    decision_payload = _strict_json_object(decision_raw)
    if decision_raw != canonical_json_bytes(decision_payload) + b"\n":
        raise DailyPipelineError(
            "preceding exposure decision is not canonical JSON"
        )
    persisted_decision_sha = decision_payload.pop("decision_sha256", None)
    if (
        decision_payload.get("schema_version") != EXPOSURE_DECISION_SCHEMA_VERSION
        or persisted_decision_sha is None
        or canonical_sha256(decision_payload) != persisted_decision_sha
        or decision_payload.get("state_sha256") != memory.state_sha256
        or decision_payload.get("policy_sha256") != memory.policy_sha256
        or decision_payload.get("state") != memory.current_state.value
        or decision_payload.get("pending_state")
        != (
            None
            if memory.pending_state is None
            else memory.pending_state.value
        )
        or decision_payload.get("pending_consecutive_sessions")
        != memory.pending_consecutive_sessions
        or decision_payload.get("decision_at")
        != memory.last_decision_at.isoformat()
        or decision_payload.get("input_snapshot_sha256")
        != memory.last_input_snapshot_sha256
    ):
        raise DailyPipelineError(
            "preceding exposure decision does not bind supplied state memory"
        )


def _derive_account_drawdown_metric(
    *,
    strategy_date: date,
    decision_at: datetime,
    previous_session: date | None,
    account: AccountSnapshot,
    current_nav: Decimal,
    instruments: Sequence[PortfolioInstrument],
    memory: ExposureStateMemoryV2,
    paper_ledger_path: str | Path | None,
    adaptive_policy_sha256: str,
) -> ExposureMetricV2:
    """Derive account drawdown from the strategy ledger peak and D-close NAV."""

    ledger_receipt: Mapping[str, Any]
    if paper_ledger_path is None:
        if memory.last_decision_at is not None or account.positions:
            raise DailyPipelineError(
                "non-bootstrap account drawdown requires a verified Paper Ledger V2"
            )
        supplied_policy_sha256 = _sha256(
            adaptive_policy_sha256,
            "adaptive_policy_sha256",
        )
        if supplied_policy_sha256 != FROZEN_ADAPTIVE_EXPOSURE_POLICY_SHA256:
            raise DailyPipelineError(
                "bootstrap account drawdown requires the canonical frozen strategy policy"
            )
        policy_path = (
            Path(__file__).resolve().parents[1]
            / DEFAULT_ADAPTIVE_EXPOSURE_POLICY_PATH
        )
        try:
            frozen_policy = load_adaptive_exposure_policy(policy_path)
            initial_cash = _decimal(
                frozen_policy.raw["portfolio"]["initial_cash"],
                "frozen strategy initial_cash",
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DailyPipelineError(
                "canonical frozen strategy initial_cash is unavailable"
            ) from exc
        if (
            initial_cash <= ZERO
            or account.cash != initial_cash
            or current_nav != initial_cash
        ):
            raise DailyPipelineError(
                "flat bootstrap account must equal the canonical frozen initial_cash"
            )
        previous_peak = initial_cash
        ledger_receipt = {
            "mode": "flat_bootstrap_without_prior_session",
            "adaptive_policy_sha256": supplied_policy_sha256,
            "frozen_initial_cash": initial_cash,
            "previous_record_sha256": None,
            "paper_ledger_file_sha256": None,
        }
    else:
        try:
            verified = verify_paper_ledger_v2(
                paper_ledger_path,
                as_of=decision_at,
            )
        except Exception as exc:
            raise DailyPipelineError(
                f"Paper Ledger V2 cannot prove account drawdown: {type(exc).__name__}"
            ) from exc
        if verified.header.get("strategy_id") != ADAPTIVE_EXPOSURE_V2_STRATEGY_ID:
            raise DailyPipelineError("Paper Ledger V2 strategy_id mismatch")
        if verified.header.get("policy_sha256") != adaptive_policy_sha256:
            raise DailyPipelineError("Paper Ledger V2 policy hash mismatch")
        if not verified.daily_sessions:
            if memory.last_decision_at is not None or account.positions:
                raise DailyPipelineError(
                    "an empty Paper Ledger V2 cannot support non-bootstrap drawdown"
                )
            previous_peak = _decimal(
                verified.header["initial_cash"],
                "Paper Ledger V2 initial_cash",
            )
        else:
            latest = verified.daily_sessions[-1]
            try:
                latest_date = date.fromisoformat(str(latest["trading_date"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise DailyPipelineError(
                    "Paper Ledger V2 latest session date is malformed"
                ) from exc
            if previous_session is None or latest_date != previous_session:
                raise DailyPipelineError(
                    "Paper Ledger V2 must end on the preceding official session"
                )
            previous_peak = _decimal(
                latest["peak_nav"],
                "Paper Ledger V2 peak_nav",
            )
        if previous_peak <= ZERO:
            raise DailyPipelineError("Paper Ledger V2 peak_nav must be positive")
        ledger_receipt = {
            "mode": "verified_paper_ledger_v2",
            "previous_record_sha256": verified.last_record_sha256,
            "paper_ledger_file_sha256": verified.file_sha256,
        }

    peak = max(previous_peak, current_nav)
    drawdown = ((peak - current_nav) / peak).quantize(PCT)
    controlled_marks = {
        item.instrument_id: item.reference_price
        for item in instruments
        if item.instrument_id in account.positions
    }
    source_snapshot_sha256 = canonical_sha256(
        {
            "schema_version": "account-drawdown-source.v1",
            "strategy_date": strategy_date,
            "account_fingerprint": account_fingerprint(account),
            "current_nav": current_nav,
            "previous_peak_nav": previous_peak,
            "drawdown": drawdown,
            "controlled_close_marks": controlled_marks,
            **ledger_receipt,
        }
    )
    return ExposureMetricV2(
        category=ExposureInputCategory.ACCOUNT_DRAWDOWN,
        status=ExposureMetricStatus.OK,
        value=float(drawdown),
        observation_session=strategy_date,
        available_at=decision_at,
        source_snapshot_sha256=source_snapshot_sha256,
    )


def _reliable_account_drawdown(
    inputs: ExposureInputSnapshotV2,
) -> float | None:
    metric = inputs.by_category[ExposureInputCategory.ACCOUNT_DRAWDOWN]
    if (
        metric.status is not ExposureMetricStatus.OK
        or metric.value is None
        or metric.available_at is None
        or metric.observation_session is None
        or metric.available_at > inputs.decision_at
        or metric.observation_session
        != inputs.decision_at.astimezone(CHINA_STANDARD_TIME).date()
    ):
        return None
    return metric.value


def _has_non_alpha_data_failure(
    inputs: ExposureInputSnapshotV2,
) -> bool:
    for metric in inputs.metrics:
        if metric.category is ExposureInputCategory.ALPHA_PREDICTION_DISTRIBUTION:
            continue
        if metric.status is ExposureMetricStatus.DATA_FAILED:
            return True
        if (
            metric.available_at is None
            or metric.observation_session is None
            or metric.available_at > inputs.decision_at
            or metric.observation_session
            != inputs.decision_at.astimezone(CHINA_STANDARD_TIME).date()
        ):
            return True
    return False


def _validate_execution_policy_binding(
    *,
    constructor_policy: PortfolioConstructorPolicy,
    fees: FeeSchedule,
    instrument_rules: Mapping[str, InstrumentRule],
) -> str:
    if constructor_policy.costs.commission_rate != fees.commission_rate:
        raise DailyPipelineError(
            "constructor commission_rate differs from canonical FeeSchedule"
        )
    if constructor_policy.costs.minimum_commission != fees.minimum_commission:
        raise DailyPipelineError(
            "constructor minimum_commission differs from canonical FeeSchedule"
        )
    if constructor_policy.costs.transfer_fee_rate != fees.exchange_fee_rate:
        raise DailyPipelineError(
            "constructor transfer fee differs from canonical FeeSchedule"
        )
    if any(
        constructor_policy.costs.sell_tax_rate != item.sell_stamp_duty_rate
        for item in instrument_rules.values()
    ):
        raise DailyPipelineError(
            "constructor sell tax differs from canonical InstrumentRule bundle"
        )
    try:
        return execution_rule_bundle_sha256(fees, instrument_rules)
    except (TypeError, ValueError) as exc:
        raise DailyPipelineError("canonical execution rule bundle is invalid") from exc


def _requested_intent(
    *,
    ranking: AlphaRankingV2,
    exposure_inputs: ExposureInputSnapshotV2,
    exposure: ExposureDecisionV2,
    current_gross_exposure: Decimal,
) -> tuple[PortfolioIntentType, Decimal, bool]:
    data_failure = (
        ranking.status is AlphaRunStatus.DATA_FAIL_CLOSED
        or _has_non_alpha_data_failure(exposure_inputs)
    )
    drawdown = _reliable_account_drawdown(exposure_inputs)
    if drawdown is not None and drawdown >= ACCOUNT_DRAWDOWN_RISK_OFF_TRIGGER:
        return PortfolioIntentType.ACCOUNT_DRAWDOWN_EXIT, ZERO, data_failure
    if data_failure:
        return PortfolioIntentType.RISK_OFF, ZERO, True
    if ranking.status is AlphaRunStatus.NO_ALPHA_CASH:
        return PortfolioIntentType.NO_ALPHA_CASH, ZERO, False
    target = Decimal(str(exposure.target_gross_exposure))
    if exposure.state.value == "RISK_OFF":
        return PortfolioIntentType.RISK_OFF, ZERO, False
    if target < current_gross_exposure:
        return PortfolioIntentType.DEFENSIVE_REDUCTION, target, False
    return PortfolioIntentType.ALPHA_REBALANCE, target, False


def _validate_experiment_v3_governance(
    *,
    decision_at: datetime,
    approved_factor_registry: ApprovedFactorRegistryV1,
    experiment_v3_admission_receipt: ExperimentV3AdmissionReceiptV1,
    alpha_model: FrozenAlphaModelV2,
    exposure_policy: ExposureHysteresisPolicyV2,
    constructor_policy: PortfolioConstructorPolicy,
) -> dict[str, Any]:
    """Validate one shared Experiment V3 evidence graph before a decision.

    Each artifact validates some local invariants on construction.  This gate
    additionally proves that the exact registry, model, two policies and the
    caller-supplied receipt belong to the same experiment evidence graph.
    Hash equality is treated only as content binding, never source proof.
    """

    if not isinstance(approved_factor_registry, ApprovedFactorRegistryV1):
        raise DailyPipelineError(
            "approved_factor_registry must be ApprovedFactorRegistryV1"
        )
    if type(experiment_v3_admission_receipt) is not ExperimentV3AdmissionReceiptV1:
        raise DailyPipelineError(
            "experiment_v3_admission_receipt must use the exact controlled V3 type"
        )
    if not isinstance(alpha_model, FrozenAlphaModelV2):
        raise DailyPipelineError("alpha_model must be FrozenAlphaModelV2")
    if not isinstance(exposure_policy, ExposureHysteresisPolicyV2):
        raise DailyPipelineError("exposure_policy must be frozen and typed")
    if not isinstance(constructor_policy, PortfolioConstructorPolicy):
        raise DailyPipelineError("constructor_policy must be frozen and typed")

    try:
        approved_factor_registry.require_valid(as_of=decision_at)
        alpha_model.calibration_artifact.require_valid()
        alpha_model.model_training_receipt.require_valid(as_of=decision_at)
        alpha_model.model_admission_receipt.require_valid(as_of=decision_at)
        verify_experiment_v3_diagnostic_binding(
            experiment_v3_admission_receipt,
            as_of=decision_at
        )
        for policy_receipt in (
            exposure_policy.policy_admission_receipt,
            constructor_policy.policy_admission_receipt,
        ):
            verify_experiment_v3_diagnostic_binding(
                policy_receipt,
                as_of=decision_at,
            )
    except (
        AlphaEngineError,
        ExperimentV3AdmissionError,
        FactorGovernanceError,
        TypeError,
        ValueError,
    ) as exc:
        raise DailyPipelineError(
            f"Experiment V3 governance artifact rejected: {type(exc).__name__}"
        ) from exc

    expected_feature_ids = tuple(
        sorted(
            set(alpha_model.financial_submodel.feature_ids)
            | set(alpha_model.non_financial_submodel.feature_ids)
        )
    )
    bindings = {
        "experiment_spec_sha256": alpha_model.experiment_spec_sha256,
        "approved_factor_registry_sha256": (
            approved_factor_registry.registry_sha256
        ),
        "experiment_v3_admission_receipt_sha256": (
            experiment_v3_admission_receipt.receipt_sha256
        ),
        "model_training_receipt_sha256": (
            alpha_model.model_training_receipt_sha256
        ),
        "model_admission_receipt_sha256": (
            alpha_model.model_admission_receipt_sha256
        ),
        "model_sha256": alpha_model.model_sha256,
        "calibration_receipt_sha256": alpha_model.calibration_receipt_sha256,
        "calibration_horizon_sessions": alpha_model.prediction_horizon_sessions,
        "exposure_policy_source_sha256": exposure_policy.policy_source_sha256,
        "constructor_policy_source_sha256": (
            constructor_policy.policy_source_sha256
        ),
    }
    mismatches = []
    if canonical_sha256(alpha_model.to_content_dict()) != alpha_model.model_sha256:
        mismatches.append("alpha_model_sha256")
    if (
        canonical_sha256(exposure_policy.to_content_dict())
        != exposure_policy.policy_sha256
    ):
        mismatches.append("exposure_policy_sha256")
    if (
        canonical_sha256(constructor_policy.to_content_dict())
        != constructor_policy.policy_sha256
    ):
        mismatches.append("constructor_policy_sha256")
    if alpha_model.approved_factor_registry_sha256 != approved_factor_registry.registry_sha256:
        mismatches.append("model_factor_registry")
    if experiment_v3_admission_receipt.model_sha256 != alpha_model.model_sha256:
        mismatches.append("receipt_model_sha256")
    if approved_factor_registry.approved_factor_ids != expected_feature_ids:
        mismatches.append("registry_feature_set")
    if exposure_policy.experiment_spec_sha256 != alpha_model.experiment_spec_sha256:
        mismatches.append("exposure_experiment_spec")
    if constructor_policy.experiment_spec_sha256 != alpha_model.experiment_spec_sha256:
        mismatches.append("constructor_experiment_spec")
    for name, policy_receipt in (
        ("exposure", exposure_policy.policy_admission_receipt),
        ("constructor", constructor_policy.policy_admission_receipt),
    ):
        if policy_receipt.to_dict() != experiment_v3_admission_receipt.to_dict():
            mismatches.append(f"{name}_experiment_receipt")
    if mismatches:
        raise DailyPipelineError(
            "Experiment V3 governance binding mismatch: "
            + ",".join(sorted(mismatches))
        )
    return bindings


def _unverified_experiment_v3_commitment(
    *,
    approved_factor_registry: Any,
    experiment_v3_admission_receipt: Any,
    alpha_model: Any,
    exposure_policy: Any,
    constructor_policy: Any,
) -> dict[str, Any]:
    """Deterministically commit supplied governance objects on blocked runs."""

    def artifact_hash(
        value: Any,
        *,
        expected_type: type[Any],
        claimed_name: str,
        content_method: str,
        scope: str,
    ) -> dict[str, str]:
        if not isinstance(value, expected_type):
            unavailable = canonical_sha256(
                {"scope": scope, "python_type": type(value).__name__}
            )
            return {"claimed_sha256": unavailable, "content_sha256": unavailable}
        claimed = getattr(value, claimed_name, None)
        if not isinstance(claimed, str) or re.fullmatch(r"[0-9a-f]{64}", claimed) is None:
            claimed = canonical_sha256(
                {"scope": f"{scope}-invalid-claim", "python_type": type(claimed).__name__}
            )
        try:
            content = getattr(value, content_method)()
            content_hash = canonical_sha256(content)
        except (AttributeError, TypeError, ValueError):
            content_hash = canonical_sha256(
                {"scope": f"{scope}-unreadable-content", "python_type": type(value).__name__}
            )
        return {"claimed_sha256": claimed, "content_sha256": content_hash}

    return {
        "scope": "unverified-experiment-v3-governance-commitment.v1",
        "approved_factor_registry": artifact_hash(
            approved_factor_registry,
            expected_type=ApprovedFactorRegistryV1,
            claimed_name="registry_sha256",
            content_method="to_content_dict",
            scope="unavailable-approved-factor-registry.v1",
        ),
        "experiment_v3_admission_receipt": artifact_hash(
            experiment_v3_admission_receipt,
            expected_type=ExperimentV3AdmissionReceiptV1,
            claimed_name="receipt_sha256",
            content_method="to_content_dict",
            scope="unavailable-experiment-v3-admission-receipt.v1",
        ),
        "alpha_model": artifact_hash(
            alpha_model,
            expected_type=FrozenAlphaModelV2,
            claimed_name="model_sha256",
            content_method="to_content_dict",
            scope="unavailable-alpha-model.v1",
        ),
        "exposure_policy": artifact_hash(
            exposure_policy,
            expected_type=ExposureHysteresisPolicyV2,
            claimed_name="policy_sha256",
            content_method="to_content_dict",
            scope="unavailable-exposure-policy.v1",
        ),
        "constructor_policy": artifact_hash(
            constructor_policy,
            expected_type=PortfolioConstructorPolicy,
            claimed_name="policy_sha256",
            content_method="to_content_dict",
            scope="unavailable-constructor-policy.v1",
        ),
    }


def _combined_policy_sha256(
    *,
    adaptive_policy_sha256: str,
    exposure_policy: ExposureHysteresisPolicyV2,
    constructor_policy: PortfolioConstructorPolicy,
    execution_rule_bundle_hash: str,
    experiment_v3_governance: Mapping[str, Any],
) -> str:
    return canonical_sha256(
        {
            "scope": "daily-signal-policy-bundle.v2",
            "adaptive_exposure_policy_sha256": _sha256(
                adaptive_policy_sha256, "adaptive_policy_sha256"
            ),
            "exposure_policy_sha256": exposure_policy.policy_sha256,
            "constructor_policy_sha256": constructor_policy.policy_sha256,
            "execution_rule_bundle_sha256": execution_rule_bundle_hash,
            "experiment_v3_governance": dict(experiment_v3_governance),
            "locked_test_guard": {
                "status": "experiment_v3_not_frozen",
                "forbidden_start": LOCKED_TEST_START,
                "forbidden_end": LOCKED_TEST_END,
            },
            "automatic_submission": False,
            "live_supported": False,
        }
    )


def _make_portfolio_intent(
    *,
    intent_type: PortfolioIntentType,
    decision_at: datetime,
    target_gross_exposure: Decimal,
    target_weights: Mapping[str, Decimal],
    construction_sha256: str,
    data_sha256: str,
    model_sha256: str,
    risk_state_sha256: str,
) -> PortfolioIntent:
    identity = canonical_sha256(
        {
            "scope": "daily-portfolio-intent-id.v1",
            "strategy_id": ADAPTIVE_EXPOSURE_V2_STRATEGY_ID,
            "decision_at": decision_at,
            "intent_type": intent_type.value,
            "construction_sha256": construction_sha256,
            "data_sha256": data_sha256,
            "model_sha256": model_sha256,
            "risk_state_sha256": risk_state_sha256,
        }
    )
    return PortfolioIntent(
        intent_id=(
            "daily-"
            f"{decision_at.astimezone(CHINA_STANDARD_TIME).date().isoformat()}-"
            f"{identity[:16]}"
        ),
        strategy_id=ADAPTIVE_EXPOSURE_V2_STRATEGY_ID,
        intent_type=intent_type,
        decision_at=decision_at,
        available_at=decision_at,
        frozen_at=decision_at,
        target_gross_exposure=target_gross_exposure,
        target_weights=target_weights,
        reason_codes=(_reason_code(intent_type.value),),
        signal_sha256=construction_sha256,
        market_data_sha256=data_sha256,
        model_sha256=model_sha256,
        risk_state_sha256=risk_state_sha256,
    )


_COMMON_CANCEL_CONDITIONS = (
    "仅限绑定的下一官方交易日一次性人工复核",
    "官方日历 receipt 或受控 registry 不匹配时取消",
    "D+1 账户快照不一致、缺失或过期时取消",
    "执行报价缺失、过期、停牌或交易受限时取消相应指令",
    "FeeSchedule 或 InstrumentRule canonical bundle 漂移时取消",
    "现金、可卖数量或整手规则不满足时取消",
    "BUY 开盘卖一价超过冻结最大偏离上限时取消",
    "风险减仓 SELL 不因价格偏离而取消",
    "未完成人工确认时不得记录成交",
)


def _account_publication_payload(
    account: AccountSnapshot,
    *,
    account_state_sha256: str,
) -> dict[str, Any]:
    return {
        "scope": "daily-strategy-account-snapshot.v1",
        "strategy_id": account.strategy_id,
        "snapshot_id": account.snapshot_id,
        "as_of": account.as_of,
        "cash": account.cash,
        "positions": {
            key: position.quantity
            for key, position in sorted(account.positions.items())
        },
        "sellable_positions": {
            key: position.sellable_quantity
            for key, position in sorted(account.positions.items())
        },
        "account_state_sha256": account_state_sha256,
        "account_fingerprint": account_fingerprint(account),
        "strategy_only": True,
    }


def _execution_rule_publication_payload(
    fees: FeeSchedule,
    instrument_rules: Mapping[str, InstrumentRule],
    *,
    execution_rule_bundle_hash: str,
) -> dict[str, Any]:
    return {
        "scope": "daily-canonical-execution-rule-bundle.v1",
        "fee_schedule": {
            "commission_rate": fees.commission_rate,
            "minimum_commission": fees.minimum_commission,
            "exchange_fee_rate": fees.exchange_fee_rate,
        },
        "instrument_rules": [
            _instrument_rule_dict(rule)
            for _, rule in sorted(instrument_rules.items())
        ],
        "whole_lot_policy": "floor_to_instrument_lot.v1",
        "execution_rule_bundle_sha256": execution_rule_bundle_hash,
        "source_authentication": "hash_consistency_only_registry_acl_is_writer_boundary",
    }


def _publish_daily_signal_evidence(
    *,
    output_directory: Path,
    ranking: AlphaRankingV2,
    alpha_model: FrozenAlphaModelV2,
    approved_factor_registry: ApprovedFactorRegistryV1,
    exposure: ExposureDecisionV2,
    exposure_policy: ExposureHysteresisPolicyV2,
    construction: PortfolioConstructionResult,
    intent: PortfolioIntent,
    constructor_policy: PortfolioConstructorPolicy,
    experiment_v3_admission_receipt: ExperimentV3AdmissionReceiptV1,
    account: AccountSnapshot,
    calendar_receipt: OfficialCalendarReceipt,
    calendar_registry: OfficialCalendarRegistry,
    fees: FeeSchedule,
    instrument_rules: Mapping[str, InstrumentRule],
    execution_rule_bundle_hash: str,
    daily_decision: DailyStrategyDecisionV2,
) -> tuple[DailySignalAdmissionReceiptV1, DailySignalPublicationReceiptV1]:
    has_buy = any(
        item.action is ConstructionActionType.BUY for item in construction.actions
    )
    risk_type = intent.intent_type in {
        PortfolioIntentType.RISK_OFF,
        PortfolioIntentType.DEFENSIVE_REDUCTION,
        PortfolioIntentType.NO_ALPHA_CASH,
        PortfolioIntentType.ACCOUNT_DRAWDOWN_EXIT,
    }
    authority = (
        DailySignalAuthority.RISK_REDUCTION_ONLY
        if risk_type and not has_buy
        else DailySignalAuthority.BLOCKED
    )
    authority_content = {
        "schema_version": "daily-signal-authority-receipt.v1",
        "strategy_id": ADAPTIVE_EXPOSURE_V2_STRATEGY_ID,
        "strategy_date": daily_decision.strategy_date,
        "execution_date": daily_decision.execution_date,
        "frozen_at": intent.frozen_at,
        "authority": authority.value,
        "intent_type": intent.intent_type.value,
        "construction_sha256": construction.construction_sha256,
        "formal_v3_loader_status": "blocked_not_implemented",
        "buy_allowed": False,
        "automatic_submission": False,
        "paper_eligibility": False,
        "trade_eligibility": False,
        "live_supported": False,
    }
    authority_hash = canonical_sha256(authority_content)
    authority_payload = {
        **authority_content,
        "authority_receipt_sha256": authority_hash,
    }
    authority_path = (
        output_directory
        / f"{daily_decision.strategy_date.isoformat()}.daily-signal-authority.json"
    )
    _write_create_only(
        authority_path,
        canonical_json_bytes(authority_payload) + b"\n",
    )
    failure_content = (
        {
            "scope": "daily-alpha-publication-blocked.v1",
            "strategy_date": daily_decision.strategy_date,
            "construction_sha256": construction.construction_sha256,
            "failed_stage": "DAILY_SIGNAL_ADMISSION",
            "failure_codes": ["formal_experiment_v3_loader_blocked"],
            "buy_allowed": False,
        }
        if authority is DailySignalAuthority.BLOCKED
        else {
            "schema_version": "daily-signal-publication-failure-receipt.v1",
            "strategy_id": ADAPTIVE_EXPOSURE_V2_STRATEGY_ID,
            "strategy_date": daily_decision.strategy_date,
            "failed_stage": None,
            "failure_codes": [],
            "authority_receipt_sha256": authority_hash,
            "orders_allowed": True,
            "buy_allowed": False,
        }
    )
    failure_hash = canonical_sha256(failure_content)
    if (
        authority is DailySignalAuthority.BLOCKED
        and daily_decision.failure_receipt_sha256 != failure_hash
    ):
        raise DailyPipelineIntegrityError(
            "blocked Daily decision and persisted failure receipt differ"
        )
    failure_payload = {
        **failure_content,
        "failure_receipt_sha256": failure_hash,
    }
    _write_create_only(
        output_directory
        / f"{daily_decision.strategy_date.isoformat()}.daily-signal-publication-failure.json",
        canonical_json_bytes(failure_payload) + b"\n",
    )
    admission = DailySignalAdmissionReceiptV1(
        strategy_date=daily_decision.strategy_date,
        execution_date=daily_decision.execution_date,
        frozen_at=intent.frozen_at,
        authority=authority,
        intent_type=intent.intent_type.value,
        alpha_ranking_sha256=ranking.ranking_sha256,
        model_sha256=ranking.model_sha256,
        approved_factor_registry_sha256=approved_factor_registry.registry_sha256,
        model_admission_receipt_sha256=(
            experiment_v3_admission_receipt.model_admission_receipt_sha256
        ),
        exposure_decision_sha256=exposure.decision_sha256,
        exposure_state_sha256=exposure.state_sha256,
        exposure_state=exposure.state.value,
        exposure_target_gross=Decimal(str(exposure.target_gross_exposure)),
        construction_sha256=construction.construction_sha256,
        intent_sha256=intent.intent_sha256,
        exposure_policy_sha256=exposure_policy.policy_sha256,
        constructor_policy_sha256=constructor_policy.policy_sha256,
        combined_policy_sha256=daily_decision.policy_sha256,
        account_state_sha256=construction.account_state_sha256,
        account_fingerprint=account_fingerprint(account),
        calendar_receipt_sha256=calendar_receipt.receipt_sha256,
        calendar_registry_sha256=calendar_registry.registry_sha256,
        execution_rule_bundle_sha256=execution_rule_bundle_hash,
        daily_decision_sha256=daily_decision.decision_sha256,
        experiment_admission_receipt_sha256=(
            experiment_v3_admission_receipt.receipt_sha256
        ),
        authority_receipt_sha256=authority_hash,
        failure_receipt_sha256=(
            failure_hash if authority is DailySignalAuthority.BLOCKED else None
        ),
    )
    full_artifacts = {
            "alpha-ranking": ranking.to_dict(),
            "alpha-model": alpha_model.to_dict(),
            "approved-factor-registry": approved_factor_registry.to_dict(),
            "exposure-decision": exposure.to_dict(),
            "exposure-state": exposure.next_state_memory.to_dict(),
            "portfolio-construction": construction.to_dict(),
            "portfolio-intent": intent.to_dict(),
            "exposure-policy": exposure_policy.to_dict(),
            "constructor-policy": constructor_policy.to_dict(),
            "experiment-v3-admission": experiment_v3_admission_receipt.to_dict(),
            "account-snapshot": _account_publication_payload(
                account,
                account_state_sha256=construction.account_state_sha256,
            ),
            "calendar-receipt": calendar_receipt.to_dict(),
            "calendar-registry": calendar_registry.to_dict(),
            "execution-rule-bundle": _execution_rule_publication_payload(
                fees,
                instrument_rules,
                execution_rule_bundle_hash=execution_rule_bundle_hash,
            ),
            "daily-decision": daily_decision.to_dict(),
            "authority-receipt": authority_payload,
            "failure-receipt": failure_payload,
        }
    artifacts_for_publication = (
        full_artifacts
        if authority is DailySignalAuthority.RISK_REDUCTION_ONLY
        else {
            "daily-decision": daily_decision.to_dict(),
            "authority-receipt": authority_payload,
            "failure-receipt": failure_payload,
            "received-input-commitments": {
                "scope": "blocked-daily-received-input-commitments.v1",
                "strategy_date": daily_decision.strategy_date,
                "alpha_ranking_sha256": ranking.ranking_sha256,
                "model_sha256": ranking.model_sha256,
                "approved_factor_registry_sha256": (
                    approved_factor_registry.registry_sha256
                ),
                "exposure_decision_sha256": exposure.decision_sha256,
                "exposure_state_sha256": exposure.state_sha256,
                "construction_sha256": construction.construction_sha256,
                "intent_sha256": intent.intent_sha256,
                "combined_policy_sha256": daily_decision.policy_sha256,
                "account_fingerprint": account_fingerprint(account),
                "calendar_receipt_sha256": calendar_receipt.receipt_sha256,
                "calendar_registry_sha256": calendar_registry.registry_sha256,
                "execution_rule_bundle_sha256": execution_rule_bundle_hash,
                "next_session_allowed": False,
            },
        }
    )
    publication = _publish_daily_signal_bundle_from_daily_pipeline(
        admission=admission,
        artifacts=artifacts_for_publication,
    )
    return admission, publication


def _publish_blocked_daily_signal_evidence(
    *,
    output_directory: Path,
    decision: DailyStrategyDecisionV2,
    intent: PortfolioIntent,
    failure_payload: Mapping[str, Any],
    experiment_v3_governance: Mapping[str, Any],
    account: Any,
    calendar_receipt: Any,
    calendar_registry: Any,
    exposure_policy: Any,
    constructor_policy: Any,
    exposure_decision: ExposureDecisionV2 | None,
) -> tuple[DailySignalAdmissionReceiptV1, DailySignalPublicationReceiptV1]:
    def commitment(value: Any, field_name: str) -> str:
        text = str(value)
        if len(text) == 64 and all(character in "0123456789abcdef" for character in text):
            return text
        return canonical_sha256(
            {"scope": f"blocked-unavailable-{field_name}.v1", "python_type": type(value).__name__}
        )

    authority_content = {
        "schema_version": "daily-signal-authority-receipt.v1",
        "strategy_id": ADAPTIVE_EXPOSURE_V2_STRATEGY_ID,
        "strategy_date": decision.strategy_date,
        "execution_date": decision.execution_date,
        "frozen_at": intent.frozen_at,
        "authority": DailySignalAuthority.BLOCKED.value,
        "intent_type": intent.intent_type.value,
        "failure_receipt_sha256": decision.failure_receipt_sha256,
        "formal_v3_loader_status": "blocked_not_implemented",
        "buy_allowed": False,
        "automatic_submission": False,
        "paper_eligibility": False,
        "trade_eligibility": False,
        "live_supported": False,
    }
    authority_hash = canonical_sha256(authority_content)
    authority_payload = {
        **authority_content,
        "authority_receipt_sha256": authority_hash,
    }
    _write_create_only(
        output_directory / f"{decision.strategy_date.isoformat()}.daily-signal-authority.json",
        canonical_json_bytes(authority_payload) + b"\n",
    )
    account_state_hash = canonical_sha256(
        {
            "scope": "blocked-account-commitment.v1",
            "account_fingerprint": (
                account_fingerprint(account)
                if isinstance(account, AccountSnapshot)
                else commitment(account, "account")
            ),
        }
    )
    account_hash = (
        account_fingerprint(account)
        if isinstance(account, AccountSnapshot)
        else commitment(account, "account-fingerprint")
    )
    exposure_decision_hash = (
        exposure_decision.decision_sha256
        if exposure_decision is not None
        else commitment(None, "exposure-decision")
    )
    exposure_state_hash = (
        exposure_decision.state_sha256
        if exposure_decision is not None
        else commitment(None, "exposure-state")
    )
    commitments = {
        "scope": "blocked-daily-received-input-commitments.v1",
        "strategy_date": decision.strategy_date,
        "experiment_v3_governance": dict(experiment_v3_governance),
        "exposure_decision_sha256": exposure_decision_hash,
        "exposure_state_sha256": exposure_state_hash,
        "account_fingerprint": account_hash,
        "calendar_receipt_sha256": (
            calendar_receipt.receipt_sha256
            if isinstance(calendar_receipt, OfficialCalendarReceipt)
            else commitment(calendar_receipt, "calendar-receipt")
        ),
        "calendar_registry_sha256": (
            calendar_registry.registry_sha256
            if isinstance(calendar_registry, OfficialCalendarRegistry)
            else commitment(calendar_registry, "calendar-registry")
        ),
        "next_session_allowed": False,
    }
    admission = DailySignalAdmissionReceiptV1(
        strategy_date=decision.strategy_date,
        execution_date=decision.execution_date,
        frozen_at=intent.frozen_at,
        authority=DailySignalAuthority.BLOCKED,
        intent_type=intent.intent_type.value,
        alpha_ranking_sha256=commitment(None, "alpha-ranking"),
        model_sha256=decision.model_sha256,
        approved_factor_registry_sha256=commitment(
            experiment_v3_governance.get("approved_factor_registry"),
            "approved-factor-registry",
        ),
        model_admission_receipt_sha256=commitment(
            experiment_v3_governance.get("alpha_model"),
            "model-admission-receipt",
        ),
        exposure_decision_sha256=exposure_decision_hash,
        exposure_state_sha256=exposure_state_hash,
        exposure_state=decision.market_regime,
        exposure_target_gross=ZERO,
        construction_sha256=intent.signal_sha256,
        intent_sha256=intent.intent_sha256,
        exposure_policy_sha256=commitment(
            getattr(exposure_policy, "policy_sha256", None), "exposure-policy"
        ),
        constructor_policy_sha256=commitment(
            getattr(constructor_policy, "policy_sha256", None), "constructor-policy"
        ),
        combined_policy_sha256=decision.policy_sha256,
        account_state_sha256=account_state_hash,
        account_fingerprint=account_hash,
        calendar_receipt_sha256=commitments["calendar_receipt_sha256"],
        calendar_registry_sha256=commitments["calendar_registry_sha256"],
        execution_rule_bundle_sha256=commitment(None, "execution-rule-bundle"),
        daily_decision_sha256=decision.decision_sha256,
        experiment_admission_receipt_sha256=commitment(
            experiment_v3_governance.get("experiment_v3_admission_receipt"),
            "experiment-v3-admission-receipt",
        ),
        authority_receipt_sha256=authority_hash,
        failure_receipt_sha256=decision.failure_receipt_sha256,
    )
    publication = _publish_daily_signal_bundle_from_daily_pipeline(
        admission=admission,
        artifacts={
            "daily-decision": decision.to_dict(),
            "authority-receipt": authority_payload,
            "failure-receipt": dict(failure_payload),
            "received-input-commitments": commitments,
        },
    )
    return admission, publication


def _theoretical_target_quantities(
    construction: PortfolioConstructionResult,
    instruments: Sequence[PortfolioInstrument],
) -> dict[str, int]:
    by_id = {item.instrument_id: item for item in instruments}
    result: dict[str, int] = {}
    for instrument_id, weight in construction.target_stock_weights.items():
        instrument = by_id[instrument_id]
        raw = int(
            (
                weight * construction.current_nav / instrument.reference_price
            ).to_integral_value(rounding=ROUND_DOWN)
        )
        result[instrument_id] = (raw // instrument.lot_size) * instrument.lot_size
    return result


def _daily_from_construction(
    *,
    construction: PortfolioConstructionResult,
    instruments: Sequence[PortfolioInstrument],
    intent: PortfolioIntent,
    execution_date: date,
    ranking: AlphaRankingV2,
    exposure: ExposureDecisionV2,
    data_failure: bool,
    data_sha256: str,
    policy_sha256: str,
    constructor_policy: PortfolioConstructorPolicy,
) -> DailyStrategyDecisionV2:
    buys: list[DailyOrderV1] = []
    sells: list[DailyOrderV1] = []
    holds: list[DailyHoldV1] = []
    cash_weight = ONE
    for action in construction.actions:
        if action.action is ConstructionActionType.CASH:
            cash_weight = action.feasible_weight
            continue
        if action.instrument_id is None:
            continue
        if action.action in {ConstructionActionType.BUY, ConstructionActionType.SELL}:
            cancel = list(_COMMON_CANCEL_CONDITIONS)
            order = DailyOrderV1(
                instrument_id=action.instrument_id,
                side=action.action.value,
                quantity=action.order_quantity,
                reference_price=action.reference_price,
                target_weight=action.feasible_weight,
                maximum_execution_price_deviation=(
                    constructor_policy.maximum_execution_price_deviation
                ),
                cancel_conditions=tuple(cancel),
            )
            (buys if action.action is ConstructionActionType.BUY else sells).append(order)
        elif action.action is ConstructionActionType.HOLD:
            holds.append(
                DailyHoldV1(
                    instrument_id=action.instrument_id,
                    quantity=action.current_quantity,
                    target_quantity=action.target_quantity,
                    target_weight=action.feasible_weight,
                    reason=",".join(action.reason_codes),
                )
            )
    if data_failure:
        status = "DATA_FAIL_CLOSED"
        data_status = "DATA_FAIL_CLOSED"
    elif buys or sells:
        status = "READY_FOR_NEXT_SESSION_REVIEW"
        data_status = "CONTROLLED_PIT_OK"
    else:
        status = "NO_TRADE"
        data_status = (
            "NO_ELIGIBLE_ALPHA"
            if ranking.status is AlphaRunStatus.NO_ALPHA_CASH
            else "CONTROLLED_PIT_OK"
        )
    model_reasons = (
        "train_only_frozen_model",
        f"alpha_status={ranking.status.value}",
        f"eligible_count={ranking.eligible_count}",
    )
    risk_reasons = (
        f"exposure_state={exposure.state.value}",
        f"transition_status={exposure.transition_status.value}",
        *exposure.reason_codes,
    )
    no_trade = list(construction.reason_codes)
    if not buys and not sells:
        no_trade.append("zero_orders_is_a_valid_daily_decision")
    current_quantities = dict(construction.current_quantities)
    return DailyStrategyDecisionV2(
        strategy_date=construction.decision_at.astimezone(
            CHINA_STANDARD_TIME
        ).date(),
        execution_date=execution_date,
        decision_status=status,
        data_status=data_status,
        market_regime=exposure.state.value,
        portfolio_intent_type=intent.intent_type.value,
        target_gross_exposure=construction.target_gross_exposure,
        feasible_gross_exposure=construction.feasible_gross_exposure,
        current_gross_exposure=construction.current_gross_exposure,
        realized_gross_exposure=None,
        target_stock_weights=construction.target_stock_weights,
        feasible_stock_weights=construction.feasible_stock_weights,
        current_stock_weights=construction.current_stock_weights,
        realized_stock_weights=None,
        target_lot_quantities=_theoretical_target_quantities(
            construction, instruments
        ),
        feasible_lot_quantities=construction.feasible_quantities,
        current_lot_quantities=current_quantities,
        realized_lot_quantities=None,
        buy_orders=tuple(buys),
        sell_orders=tuple(sells),
        hold_positions=tuple(holds),
        cash_weight=cash_weight,
        maximum_execution_price_deviation=(
            constructor_policy.maximum_execution_price_deviation
        ),
        cancel_conditions=_COMMON_CANCEL_CONDITIONS,
        expected_cost=construction.expected_cost,
        model_reasons=model_reasons,
        risk_reasons=_unique_texts(risk_reasons),
        no_trade_reasons=_unique_texts(no_trade),
        data_sha256=data_sha256,
        model_sha256=ranking.model_sha256,
        policy_sha256=policy_sha256,
        intent_sha256=intent.intent_sha256,
    )


def _manual_pause_decision(
    *,
    strategy_date: date,
    execution_date: date,
    account: AccountSnapshot,
    instruments: Sequence[PortfolioInstrument],
    ranking: AlphaRankingV2,
    exposure: ExposureDecisionV2,
    data_sha256: str,
    policy_sha256: str,
    constructor_policy: PortfolioConstructorPolicy,
) -> tuple[PortfolioIntent, DailyStrategyDecisionV2]:
    gross, weights, nav = _current_portfolio_metrics(account, instruments)
    quantities = {
        instrument_id: position.quantity
        for instrument_id, position in sorted(account.positions.items())
        if position.quantity > 0
    }
    pause_type = (
        PortfolioIntentType.MANUAL_PAUSE
        if quantities
        else PortfolioIntentType.NO_ALPHA_CASH
    )
    pause_hash = canonical_sha256(
        {
            "scope": "manual-pause-construction.v1",
            "strategy_date": strategy_date,
            "account_snapshot_id": account.snapshot_id,
            "account_as_of": account.as_of,
            "current_gross_exposure": gross,
            "current_weights": weights,
            "current_quantities": quantities,
            "no_orders": True,
        }
    )
    intent = _make_portfolio_intent(
        intent_type=pause_type,
        decision_at=ranking.decision_at,
        target_gross_exposure=gross if quantities else ZERO,
        target_weights=weights,
        construction_sha256=pause_hash,
        data_sha256=data_sha256,
        model_sha256=ranking.model_sha256,
        risk_state_sha256=exposure.state_sha256,
    )
    holds = tuple(
        DailyHoldV1(
            instrument_id=instrument_id,
            quantity=quantity,
            target_quantity=quantity,
            target_weight=weights[instrument_id],
            reason="manual_pause_no_risk_increase",
        )
        for instrument_id, quantity in quantities.items()
    )
    decision = DailyStrategyDecisionV2(
        strategy_date=strategy_date,
        execution_date=execution_date,
        decision_status="MANUAL_PAUSE",
        data_status=(
            "DATA_FAIL_CLOSED"
            if ranking.status is AlphaRunStatus.DATA_FAIL_CLOSED
            else "CONTROLLED_PIT_OK"
        ),
        market_regime=exposure.state.value,
        portfolio_intent_type=pause_type.value,
        target_gross_exposure=gross,
        feasible_gross_exposure=gross,
        current_gross_exposure=gross,
        realized_gross_exposure=None,
        target_stock_weights=weights,
        feasible_stock_weights=weights,
        current_stock_weights=weights,
        realized_stock_weights=None,
        target_lot_quantities=quantities,
        feasible_lot_quantities=quantities,
        current_lot_quantities=quantities,
        realized_lot_quantities=None,
        hold_positions=holds,
        cash_weight=(account.cash / nav).quantize(PCT),
        maximum_execution_price_deviation=(
            constructor_policy.maximum_execution_price_deviation
        ),
        cancel_conditions=(
            *_COMMON_CANCEL_CONDITIONS,
            "MANUAL_PAUSE 生效期间全部新增 BUY 取消",
        ),
        expected_cost=ZERO,
        model_reasons=(
            "train_only_frozen_model",
            f"alpha_status={ranking.status.value}",
        ),
        risk_reasons=("manual_pause", *exposure.reason_codes),
        no_trade_reasons=(
            "manual_pause_blocks_all_risk_increase",
            "zero_orders_is_a_valid_daily_decision",
        ),
        data_sha256=data_sha256,
        model_sha256=ranking.model_sha256,
        policy_sha256=policy_sha256,
        intent_sha256=intent.intent_sha256,
    )
    return intent, decision


def _write_daily_evidence(
    output_directory: str | Path,
    *,
    strategy_date: date,
    frozen_data: FrozenDailyDataV2,
    ranking: AlphaRankingV2,
    exposure_inputs: ExposureInputSnapshotV2,
    exposure: ExposureDecisionV2,
    construction: PortfolioConstructionResult | None,
    intent: PortfolioIntent,
    approved_factor_registry: ApprovedFactorRegistryV1,
    experiment_v3_admission_receipt: ExperimentV3AdmissionReceiptV1,
    experiment_v3_governance: Mapping[str, Any],
) -> DailyEvidenceArtifacts:
    output = Path(output_directory)
    stem = strategy_date.isoformat()
    paths_payloads: list[tuple[Path, Mapping[str, Any]]] = [
        (output / f"{stem}.frozen-data.json", frozen_data.to_dict()),
        (
            output / f"{stem}.controlled-pit-snapshot.json",
            frozen_data.alpha_snapshot.to_dict(),
        ),
        (output / f"{stem}.alpha-ranking.json", ranking.to_dict()),
        (output / f"{stem}.exposure-inputs.json", exposure_inputs.to_dict()),
        (output / f"{stem}.exposure-decision.json", exposure.to_dict()),
        (
            output / f"{stem}.exposure-state.json",
            exposure.next_state_memory.to_dict(),
        ),
        (output / f"{stem}.portfolio-intent.json", intent.to_dict()),
    ]
    construction_path: Path | None = None
    if construction is not None:
        construction_path = output / f"{stem}.portfolio-construction.json"
        paths_payloads.append((construction_path, construction.to_dict()))
    approved_factor_registry_path = (
        output / f"{stem}.approved-factor-registry.json"
    )
    experiment_v3_admission_receipt_path = (
        output / f"{stem}.experiment-v3-admission-receipt.json"
    )
    experiment_v3_evidence_path = (
        output / f"{stem}.experiment-v3-governance-evidence.json"
    )
    experiment_v3_evidence_content = {
        "scope": "daily-experiment-v3-governance-evidence.v1",
        "strategy_date": strategy_date,
        "experiment_v3_governance": dict(experiment_v3_governance),
        "approved_factor_registry_sha256": (
            approved_factor_registry.registry_sha256
        ),
        "experiment_v3_admission_receipt_sha256": (
            experiment_v3_admission_receipt.receipt_sha256
        ),
        "source_authentication": (
            "external_controlled_loader_required_hash_is_not_source_proof"
        ),
        "paper_eligibility": False,
        "trade_eligibility": False,
        "real_money_list_allowed": False,
        "live_supported": False,
    }
    experiment_v3_evidence_sha256 = canonical_sha256(
        experiment_v3_evidence_content
    )
    paths_payloads.extend(
        (
            (approved_factor_registry_path, approved_factor_registry.to_dict()),
            (
                experiment_v3_admission_receipt_path,
                experiment_v3_admission_receipt.to_dict(),
            ),
            (
                experiment_v3_evidence_path,
                {
                    **experiment_v3_evidence_content,
                    "evidence_sha256": experiment_v3_evidence_sha256,
                },
            ),
        )
    )
    for path, payload in paths_payloads:
        _write_create_only(path, canonical_json_bytes(payload) + b"\n")
    return DailyEvidenceArtifacts(
        frozen_data_path=paths_payloads[0][0],
        pit_snapshot_path=paths_payloads[1][0],
        alpha_ranking_path=paths_payloads[2][0],
        exposure_inputs_path=paths_payloads[3][0],
        exposure_decision_path=paths_payloads[4][0],
        portfolio_construction_path=construction_path,
        exposure_state_path=paths_payloads[5][0],
        portfolio_intent_path=paths_payloads[6][0],
        approved_factor_registry_path=approved_factor_registry_path,
        experiment_v3_admission_receipt_path=(
            experiment_v3_admission_receipt_path
        ),
        experiment_v3_evidence_path=experiment_v3_evidence_path,
        experiment_v3_evidence_sha256=experiment_v3_evidence_sha256,
    )


def _synthetic_failure_exposure(
    *,
    strategy_date: date,
    decision_at: datetime,
    failure_code: str,
    failure_receipt_sha256: str,
    exposure_policy: Any,
    exposure_memory: Any,
) -> tuple[ExposureInputSnapshotV2, ExposureDecisionV2] | None:
    """Create a canonical RISK_OFF continuation when the policy is usable.

    A malformed or unavailable policy cannot become authoritative merely
    because the pipeline failed.  A malformed caller memory is replaceable by
    a neutral recovery anchor because the resulting state is strictly
    RISK_OFF and the failure receipt records the broken continuity boundary.
    """

    if not isinstance(exposure_policy, ExposureHysteresisPolicyV2):
        return None
    if canonical_sha256(exposure_policy.to_content_dict()) != exposure_policy.policy_sha256:
        return None
    failure_inputs = ExposureInputSnapshotV2(
        decision_at=decision_at,
        metrics=tuple(
            ExposureMetricV2(
                category=category,
                status=ExposureMetricStatus.DATA_FAILED,
                value=None,
                observation_session=strategy_date,
                available_at=decision_at,
                source_snapshot_sha256=failure_receipt_sha256,
                failure_codes=(
                    f"PIPELINE_FAILURE:{failure_code}:{failure_receipt_sha256}",
                ),
            )
            for category in ExposureInputCategory
        ),
    )
    seed_memory: ExposureStateMemoryV2
    if (
        isinstance(exposure_memory, ExposureStateMemoryV2)
        and exposure_memory.policy_sha256 == exposure_policy.policy_sha256
        and canonical_sha256(exposure_memory.to_content_dict())
        == exposure_memory.state_sha256
        and (
            exposure_memory.last_decision_at is None
            or exposure_memory.last_decision_at < decision_at
        )
    ):
        seed_memory = exposure_memory
    else:
        seed_memory = ExposureStateMemoryV2(
            policy_sha256=exposure_policy.policy_sha256,
            current_state=ExposureState.NEUTRAL,
        )
    try:
        failure_decision = decide_exposure(
            failure_inputs,
            exposure_policy,
            seed_memory,
        )
    except ExposureEngineError:
        return None
    if (
        failure_decision.state is not ExposureState.RISK_OFF
        or failure_decision.target_gross_exposure != 0.0
    ):
        raise DailyPipelineIntegrityError(
            "synthetic pipeline-failure exposure did not fail closed"
        )
    return failure_inputs, failure_decision


def _write_pipeline_failure_decision(
    *,
    strategy_date: date,
    failed_stage: str,
    data_status: str,
    failure_code: str,
    failure_exception_type: str,
    approved_factor_registry: ApprovedFactorRegistryV1,
    experiment_v3_admission_receipt: ExperimentV3AdmissionReceiptV1,
    alpha_model: Any,
    exposure_policy: Any,
    exposure_memory: Any,
    constructor_policy: Any,
    account: Any,
    calendar_receipt: Any,
    calendar_registry: Any,
    output_directory: str | Path,
    adaptive_policy_sha256: str,
) -> DailyPipelineBlockedRunV1:
    """Write a deterministic zero-order decision for an expected pipeline failure.

    Unknown market/account values remain null.  Exception messages are omitted
    so secrets and unstable provider text cannot enter the immutable receipt.
    """

    effective_decision_at = datetime.combine(
        strategy_date,
        OFFICIAL_CLOSE_TIME,
        CHINA_STANDARD_TIME,
    )
    normalized_stage = str(failed_stage).strip().upper()
    if not normalized_stage:
        raise DailyPipelineError("failed_stage is required")
    normalized_data_status = str(data_status).strip().upper()
    if not normalized_data_status:
        raise DailyPipelineError("failure data_status is required")
    normalized_failure_code = _reason_code(failure_code)
    failure_content = {
        "schema_version": "daily-pipeline-failure-receipt.v1",
        "strategy_id": ADAPTIVE_EXPOSURE_V2_STRATEGY_ID,
        "strategy_date": strategy_date,
        "effective_decision_at": effective_decision_at,
        "failed_stage": normalized_stage,
        "failure_codes": [normalized_failure_code],
        "exception_type": str(failure_exception_type).strip() or "UnknownError",
        "message_persisted": False,
        "orders_allowed": False,
        "automatic_submission": False,
        "paper_eligibility": False,
        "trade_eligibility": False,
        "live_supported": False,
    }
    failure_receipt_sha256 = canonical_sha256(failure_content)
    failure_payload = {
        **failure_content,
        "failure_receipt_sha256": failure_receipt_sha256,
    }
    failure_path = (
        Path(output_directory)
        / f"{strategy_date.isoformat()}.pipeline-failure.json"
    )
    _write_create_only(
        failure_path,
        canonical_json_bytes(failure_payload) + b"\n",
    )

    model_sha256 = (
        alpha_model.model_sha256
        if isinstance(alpha_model, FrozenAlphaModelV2)
        else canonical_sha256(
            {
                "scope": "unavailable-alpha-model.v1",
                "python_type": type(alpha_model).__name__,
            }
        )
    )
    adaptive_policy_text = str(adaptive_policy_sha256)
    safe_adaptive_policy_sha256 = (
        adaptive_policy_text
        if len(adaptive_policy_text) == 64
        and all(character in "0123456789abcdef" for character in adaptive_policy_text)
        else canonical_sha256(
            {
                "scope": "unavailable-adaptive-policy.v1",
                "python_type": type(adaptive_policy_sha256).__name__,
            }
        )
    )
    experiment_v3_governance = _unverified_experiment_v3_commitment(
        approved_factor_registry=approved_factor_registry,
        experiment_v3_admission_receipt=experiment_v3_admission_receipt,
        alpha_model=alpha_model,
        exposure_policy=exposure_policy,
        constructor_policy=constructor_policy,
    )
    policy_sha256 = canonical_sha256(
        {
            "scope": "blocked-daily-policy-bundle.v2",
            "adaptive_policy_sha256": safe_adaptive_policy_sha256,
            "exposure_policy_sha256": (
                exposure_policy.policy_sha256
                if isinstance(exposure_policy, ExposureHysteresisPolicyV2)
                else canonical_sha256(
                    {
                        "scope": "unavailable-exposure-policy.v1",
                        "python_type": type(exposure_policy).__name__,
                    }
                )
            ),
            "constructor_policy_sha256": (
                constructor_policy.policy_sha256
                if isinstance(constructor_policy, PortfolioConstructorPolicy)
                else canonical_sha256(
                    {
                        "scope": "unavailable-constructor-policy.v1",
                        "python_type": type(constructor_policy).__name__,
                    }
                )
            ),
            "experiment_v3_governance": experiment_v3_governance,
            "execution_rule_bundle_status": "unavailable_due_to_pipeline_failure",
            "live_supported": False,
        }
    )
    data_sha256 = canonical_sha256(
        {
            "scope": "blocked-daily-data-bundle.v2",
            "failure_receipt_sha256": failure_receipt_sha256,
            "experiment_v3_governance": experiment_v3_governance,
        }
    )
    failure_exposure = _synthetic_failure_exposure(
        strategy_date=strategy_date,
        decision_at=effective_decision_at,
        failure_code=normalized_failure_code,
        failure_receipt_sha256=failure_receipt_sha256,
        exposure_policy=exposure_policy,
        exposure_memory=exposure_memory,
    )
    failure_exposure_inputs = (
        None if failure_exposure is None else failure_exposure[0]
    )
    failure_exposure_decision = (
        None if failure_exposure is None else failure_exposure[1]
    )
    risk_state_sha256 = (
        failure_exposure_decision.state_sha256
        if failure_exposure_decision is not None
        else canonical_sha256(
            {
                "scope": "blocked-daily-risk-state.v1",
                "state": "RISK_OFF",
                "failure_receipt_sha256": failure_receipt_sha256,
                "continuation_status": "manual_recovery_required",
            }
        )
    )
    intent = _make_portfolio_intent(
        intent_type=PortfolioIntentType.RISK_OFF,
        decision_at=effective_decision_at,
        target_gross_exposure=ZERO,
        target_weights={},
        construction_sha256=canonical_sha256(
            {
                "scope": "blocked-pipeline-construction.v1",
                "failure_receipt_sha256": failure_receipt_sha256,
                "zero_orders": True,
            }
        ),
        data_sha256=data_sha256,
        model_sha256=model_sha256,
        risk_state_sha256=risk_state_sha256,
    )

    execution_date: date | None = None
    try:
        if (
            isinstance(calendar_receipt, OfficialCalendarReceipt)
            and isinstance(calendar_registry, OfficialCalendarRegistry)
            and calendar_registry.frozen_at <= effective_decision_at
            and calendar_receipt.available_at <= effective_decision_at
        ):
            calendar_registry.verify(calendar_receipt)
            execution_date = calendar_receipt.next_session_after(strategy_date)
    except (KeyError, TypeError, ValueError, NextSessionSignalError):
        execution_date = None

    account_is_known_flat = (
        isinstance(account, AccountSnapshot)
        and account.strategy_id == ADAPTIVE_EXPOSURE_V2_STRATEGY_ID
        and account.as_of is not None
        and account.as_of <= effective_decision_at
        and account.as_of.astimezone(CHINA_STANDARD_TIME).date() == strategy_date
        and not account.positions
    )
    decision = DailyStrategyDecisionV2(
        strategy_date=strategy_date,
        execution_date=execution_date,
        decision_status="BLOCKED",
        data_status=normalized_data_status,
        market_regime="RISK_OFF",
        portfolio_intent_type=PortfolioIntentType.RISK_OFF.value,
        target_gross_exposure=ZERO,
        feasible_gross_exposure=ZERO,
        current_gross_exposure=ZERO if account_is_known_flat else None,
        realized_gross_exposure=None,
        target_stock_weights={},
        feasible_stock_weights={},
        current_stock_weights={} if account_is_known_flat else None,
        realized_stock_weights=None,
        target_lot_quantities={},
        feasible_lot_quantities={},
        current_lot_quantities={} if account_is_known_flat else None,
        realized_lot_quantities=None,
        cash_weight=ONE if account_is_known_flat else None,
        maximum_execution_price_deviation=(
            constructor_policy.maximum_execution_price_deviation
            if isinstance(constructor_policy, PortfolioConstructorPolicy)
            else ZERO
        ),
        cancel_conditions=(
            *_COMMON_CANCEL_CONDITIONS,
            "流水线失败关闭：全部交易取消，等待人工修复后仅可生成新的受控决策",
        ),
        expected_cost=ZERO,
        model_reasons=("model_not_run_or_not_usable_due_to_pipeline_failure",),
        risk_reasons=("pipeline_failure_immediate_risk_off",),
        no_trade_reasons=(
            normalized_failure_code,
            "zero_orders_is_a_valid_daily_decision",
        ),
        data_sha256=data_sha256,
        model_sha256=model_sha256,
        policy_sha256=policy_sha256,
        intent_sha256=intent.intent_sha256,
        failed_stage=normalized_stage,
        failure_codes=(normalized_failure_code,),
        failure_receipt_sha256=failure_receipt_sha256,
    )
    artifacts = write_daily_decision(output_directory, decision)
    if failure_exposure_inputs is not None and failure_exposure_decision is not None:
        stem = strategy_date.isoformat()
        _write_create_only(
            Path(output_directory) / f"{stem}.exposure-inputs.json",
            canonical_json_bytes(failure_exposure_inputs.to_dict()) + b"\n",
        )
        _write_create_only(
            Path(output_directory) / f"{stem}.exposure-decision.json",
            canonical_json_bytes(failure_exposure_decision.to_dict()) + b"\n",
        )
        _write_create_only(
            Path(output_directory) / f"{stem}.exposure-state.json",
            canonical_json_bytes(
                failure_exposure_decision.next_state_memory.to_dict()
            )
            + b"\n",
        )
    _register_exposure_state_artifacts(
        strategy_date=strategy_date,
        daily_decision=decision,
        exposure_inputs=failure_exposure_inputs,
        exposure_decision=failure_exposure_decision,
        failure_receipt=failure_payload,
    )
    daily_admission, daily_publication = _publish_blocked_daily_signal_evidence(
        output_directory=Path(output_directory),
        decision=decision,
        intent=intent,
        failure_payload=failure_payload,
        experiment_v3_governance=experiment_v3_governance,
        account=account,
        calendar_receipt=calendar_receipt,
        calendar_registry=calendar_registry,
        exposure_policy=exposure_policy,
        constructor_policy=constructor_policy,
        exposure_decision=failure_exposure_decision,
    )
    return DailyPipelineBlockedRunV1(
        portfolio_intent=intent,
        daily_decision=decision,
        artifacts=artifacts,
        failure_receipt_path=failure_path,
        exposure_inputs=failure_exposure_inputs,
        exposure_decision=failure_exposure_decision,
        daily_signal_admission_receipt=daily_admission,
        daily_signal_publication_receipt=daily_publication,
    )


def _run_after_close_daily_pipeline_impl(
    *,
    strategy_date: date,
    data_updater: DailyDataUpdaterV2,
    approved_factor_registry: ApprovedFactorRegistryV1,
    experiment_v3_admission_receipt: ExperimentV3AdmissionReceiptV1,
    alpha_model: FrozenAlphaModelV2,
    exposure_policy: ExposureHysteresisPolicyV2,
    exposure_memory: ExposureStateMemoryV2,
    constructor_policy: PortfolioConstructorPolicy,
    account: AccountSnapshot,
    fees: FeeSchedule,
    calendar_receipt: OfficialCalendarReceipt,
    calendar_registry: OfficialCalendarRegistry,
    output_directory: str | Path,
    signal_path: str | Path,
    paper_ledger_path: str | Path | None = None,
    manual_pause: bool = False,
    adaptive_policy_sha256: str = FROZEN_ADAPTIVE_EXPOSURE_POLICY_SHA256,
) -> DailyPipelineRunV1 | DailyPipelineBlockedRunV1:
    """Execute stages 1-9 and freeze the one-shot D+1 signal.

    Thresholds and models are mandatory caller-supplied frozen artifacts.  The
    function never selects production parameters and never submits an order.
    """

    if type(strategy_date) is not date:
        raise DailyPipelineError("strategy_date must be a date")
    if LOCKED_TEST_START <= strategy_date <= LOCKED_TEST_END:
        raise DailyPipelineError(
            "experiment_v3_not_frozen_locked_test_execution_forbidden"
        )
    if type(manual_pause) is not bool:
        raise DailyPipelineError("manual_pause must be boolean")
    output = Path(output_directory)
    if not output.exists() or not output.is_dir() or output.is_symlink():
        raise DailyPipelineError(
            "daily decision output directory must already exist and be regular"
        )
    update_method = getattr(data_updater, "update_and_freeze", None)
    if not callable(update_method):
        return _write_pipeline_failure_decision(
            strategy_date=strategy_date,
            failed_stage="DATA_UPDATE",
            data_status="DATA_UPDATE_FAILED",
            failure_code="data_updater_missing",
            failure_exception_type="MissingUpdateMethod",
            approved_factor_registry=approved_factor_registry,
            experiment_v3_admission_receipt=experiment_v3_admission_receipt,
            alpha_model=alpha_model,
            exposure_policy=exposure_policy,
            exposure_memory=exposure_memory,
            constructor_policy=constructor_policy,
            account=account,
            calendar_receipt=calendar_receipt,
            calendar_registry=calendar_registry,
            output_directory=output_directory,
            adaptive_policy_sha256=adaptive_policy_sha256,
        )
    try:
        frozen_data = update_method(strategy_date)
    except Exception as exc:
        return _write_pipeline_failure_decision(
            strategy_date=strategy_date,
            failed_stage="DATA_UPDATE",
            data_status="DATA_UPDATE_FAILED",
            failure_code="data_update_failed",
            failure_exception_type=type(exc).__name__,
            approved_factor_registry=approved_factor_registry,
            experiment_v3_admission_receipt=experiment_v3_admission_receipt,
            alpha_model=alpha_model,
            exposure_policy=exposure_policy,
            exposure_memory=exposure_memory,
            constructor_policy=constructor_policy,
            account=account,
            calendar_receipt=calendar_receipt,
            calendar_registry=calendar_registry,
            output_directory=output_directory,
            adaptive_policy_sha256=adaptive_policy_sha256,
        )
    if not isinstance(frozen_data, FrozenDailyDataV2):
        return _write_pipeline_failure_decision(
            strategy_date=strategy_date,
            failed_stage="DATA_UPDATE",
            data_status="DATA_UPDATE_FAILED",
            failure_code="unsupported_data_update_envelope",
            failure_exception_type=type(frozen_data).__name__,
            approved_factor_registry=approved_factor_registry,
            experiment_v3_admission_receipt=experiment_v3_admission_receipt,
            alpha_model=alpha_model,
            exposure_policy=exposure_policy,
            exposure_memory=exposure_memory,
            constructor_policy=constructor_policy,
            account=account,
            calendar_receipt=calendar_receipt,
            calendar_registry=calendar_registry,
            output_directory=output_directory,
            adaptive_policy_sha256=adaptive_policy_sha256,
        )
    if canonical_sha256(frozen_data.to_content_dict()) != frozen_data.data_update_sha256:
        raise DailyPipelineError("frozen daily data envelope hash mismatch")
    snapshot = frozen_data.alpha_snapshot
    local_decision_at = snapshot.decision_at.astimezone(CHINA_STANDARD_TIME)
    if local_decision_at.date() != strategy_date:
        raise DailyPipelineError(
            "PIT snapshot decision date differs from strategy_date in CST"
        )
    if local_decision_at.time().replace(tzinfo=None) < OFFICIAL_CLOSE_TIME:
        raise DailyPipelineError(
            "daily pipeline may only freeze a decision at or after the D-session close"
        )
    if not isinstance(alpha_model, FrozenAlphaModelV2):
        raise DailyPipelineError("alpha_model must be FrozenAlphaModelV2")
    if not isinstance(exposure_policy, ExposureHysteresisPolicyV2):
        raise DailyPipelineError("exposure_policy must be frozen and typed")
    if not isinstance(exposure_memory, ExposureStateMemoryV2):
        raise DailyPipelineError("exposure_memory must be typed")
    if not isinstance(constructor_policy, PortfolioConstructorPolicy):
        raise DailyPipelineError("constructor_policy must be frozen and typed")
    experiment_v3_governance = _validate_experiment_v3_governance(
        decision_at=snapshot.decision_at,
        approved_factor_registry=approved_factor_registry,
        experiment_v3_admission_receipt=experiment_v3_admission_receipt,
        alpha_model=alpha_model,
        exposure_policy=exposure_policy,
        constructor_policy=constructor_policy,
    )
    if not isinstance(calendar_receipt, OfficialCalendarReceipt):
        raise DailyPipelineError("calendar_receipt must be structured and typed")
    if not isinstance(calendar_registry, OfficialCalendarRegistry):
        raise DailyPipelineError("calendar_registry must be controlled and typed")
    if (
        not isinstance(account, AccountSnapshot)
        or account.strategy_id != ADAPTIVE_EXPOSURE_V2_STRATEGY_ID
        or account.as_of is None
        or account.as_of > snapshot.decision_at
        or account.as_of.astimezone(CHINA_STANDARD_TIME).date() != strategy_date
    ):
        raise DailyPipelineError(
            "D-close strategy-only account snapshot is absent, future, or mismatched"
        )
    if (
        snapshot.decision_at - account.as_of
    ).total_seconds() > constructor_policy.maximum_account_age_seconds:
        raise DailyPipelineError("D-close strategy account snapshot is stale")
    if calendar_registry.frozen_at > snapshot.decision_at:
        raise DailyPipelineError("official calendar registry was not frozen at decision")
    calendar_registry.verify(calendar_receipt)
    if calendar_receipt.available_at > snapshot.decision_at:
        raise DailyPipelineError("official calendar receipt was unavailable at decision")
    if snapshot.trading_calendar_receipt_sha256 != calendar_receipt.receipt_sha256:
        raise DailyPipelineError(
            "PIT snapshot and next-session adapter bind different calendar receipts"
        )
    if not snapshot.trading_sessions or snapshot.trading_sessions[-1] != strategy_date:
        raise DailyPipelineError(
            "PIT snapshot trading sessions must end on strategy_date"
        )
    try:
        receipt_indices = tuple(
            calendar_receipt.trading_sessions.index(item)
            for item in snapshot.trading_sessions
        )
    except ValueError as exc:
        raise DailyPipelineError(
            "official calendar receipt does not cover the PIT session history"
        ) from exc
    if any(
        right != left + 1
        for left, right in zip(receipt_indices, receipt_indices[1:])
    ):
        raise DailyPipelineError(
            "PIT session history is not contiguous in the official calendar receipt"
        )
    strategy_index = calendar_receipt.trading_sessions.index(strategy_date)
    previous_session = (
        None
        if strategy_index == 0
        else calendar_receipt.trading_sessions[strategy_index - 1]
    )
    if exposure_memory.last_decision_at is not None and (
        previous_session is None
        or exposure_memory.last_decision_at.astimezone(
            CHINA_STANDARD_TIME
        ).date()
        != previous_session
    ):
        raise DailyPipelineError(
            "exposure hysteresis memory is not from the immediately preceding official session"
        )
    _validate_exposure_memory_provenance(
        output_directory=output,
        strategy_date=strategy_date,
        previous_session=previous_session,
        memory=exposure_memory,
    )
    execution_date = calendar_receipt.next_session_after(strategy_date)
    rule_bundle_hash = _validate_execution_policy_binding(
        constructor_policy=constructor_policy,
        fees=fees,
        instrument_rules=frozen_data.instrument_rules,
    )

    ranking = run_alpha_engine(
        snapshot,
        alpha_model,
        approved_factor_registry=approved_factor_registry,
        experiment_v3_admission_receipt=experiment_v3_admission_receipt,
    )
    alpha_metric = _alpha_distribution_metric(ranking)
    instruments = _portfolio_instruments(
        ranking=ranking,
        frozen_data=frozen_data,
        strategy_date=strategy_date,
    )
    current_gross, _, current_nav = _current_portfolio_metrics(account, instruments)
    account_drawdown_metric = _derive_account_drawdown_metric(
        strategy_date=strategy_date,
        decision_at=snapshot.decision_at,
        previous_session=previous_session,
        account=account,
        current_nav=current_nav,
        instruments=instruments,
        memory=exposure_memory,
        paper_ledger_path=paper_ledger_path,
        adaptive_policy_sha256=adaptive_policy_sha256,
    )
    exposure_inputs = ExposureInputSnapshotV2(
        decision_at=snapshot.decision_at,
        metrics=(
            frozen_data.non_alpha_exposure_metrics
            + (alpha_metric, account_drawdown_metric)
        ),
    )
    exposure = decide_exposure(exposure_inputs, exposure_policy, exposure_memory)
    data_sha256 = canonical_sha256(
        {
            "scope": "daily-controlled-data-bundle.v1",
            "strategy_date": strategy_date,
            "data_update_sha256": frozen_data.data_update_sha256,
            "alpha_input_snapshot_sha256": snapshot.input_snapshot_sha256,
            "exposure_input_snapshot_sha256": exposure_inputs.input_snapshot_sha256,
            "experiment_v3_governance": experiment_v3_governance,
        }
    )
    policy_sha256 = _combined_policy_sha256(
        adaptive_policy_sha256=adaptive_policy_sha256,
        exposure_policy=exposure_policy,
        constructor_policy=constructor_policy,
        execution_rule_bundle_hash=rule_bundle_hash,
        experiment_v3_governance=experiment_v3_governance,
    )

    requested_type, target_exposure, data_failure = _requested_intent(
        ranking=ranking,
        exposure_inputs=exposure_inputs,
        exposure=exposure,
        current_gross_exposure=current_gross,
    )
    construction: PortfolioConstructionResult | None
    signal: NextSessionSignal | None
    written_signal_path: Path | None
    if manual_pause and requested_type is PortfolioIntentType.ALPHA_REBALANCE:
        construction = None
        intent, decision = _manual_pause_decision(
            strategy_date=strategy_date,
            execution_date=execution_date,
            account=account,
            instruments=instruments,
            ranking=ranking,
            exposure=exposure,
            data_sha256=data_sha256,
            policy_sha256=policy_sha256,
            constructor_policy=constructor_policy,
        )
        signal = None
        written_signal_path = None
    else:
        positions = tuple(
            CurrentPosition(instrument_id, position.quantity)
            for instrument_id, position in sorted(account.positions.items())
            if position.quantity > 0
        )
        try:
            construction = construct_portfolio(
                decision_at=snapshot.decision_at,
                requested_intent_type=requested_type,
                target_gross_exposure=target_exposure,
                current_cash=account.cash,
                current_positions=positions,
                instruments=instruments,
                policy=constructor_policy,
                input_snapshot_sha256=data_sha256,
                model_sha256=ranking.model_sha256,
            )
        except PortfolioConstructionError as exc:
            raise DailyPipelineError(
                f"portfolio construction failed closed: {exc}"
            ) from exc
        intent = _make_portfolio_intent(
            intent_type=construction.intent_type,
            decision_at=snapshot.decision_at,
            target_gross_exposure=construction.target_gross_exposure,
            target_weights=construction.feasible_stock_weights,
            construction_sha256=construction.construction_sha256,
            data_sha256=data_sha256,
            model_sha256=ranking.model_sha256,
            risk_state_sha256=exposure.state_sha256,
        )
        signal = None
        written_signal_path = None
        decision = _daily_from_construction(
            construction=construction,
            instruments=instruments,
            intent=intent,
            execution_date=execution_date,
            ranking=ranking,
            exposure=exposure,
            data_failure=data_failure,
            data_sha256=data_sha256,
            policy_sha256=policy_sha256,
            constructor_policy=constructor_policy,
        )
        if intent.intent_type is PortfolioIntentType.ALPHA_REBALANCE:
            blocker_content = {
                "scope": "daily-alpha-publication-blocked.v1",
                "strategy_date": strategy_date,
                "construction_sha256": construction.construction_sha256,
                "failed_stage": "DAILY_SIGNAL_ADMISSION",
                "failure_codes": ["formal_experiment_v3_loader_blocked"],
                "buy_allowed": False,
            }
            blocker_hash = canonical_sha256(blocker_content)
            decision = replace(
                decision,
                decision_status="BLOCKED",
                data_status="MODEL_ADMISSION_BLOCKED",
                target_gross_exposure=ZERO,
                feasible_gross_exposure=ZERO,
                target_stock_weights={},
                feasible_stock_weights={},
                target_lot_quantities={},
                feasible_lot_quantities={},
                buy_orders=(),
                sell_orders=(),
                hold_positions=(),
                cash_weight=(account.cash / current_nav).quantize(PCT),
                expected_cost=ZERO,
                no_trade_reasons=(
                    "formal_experiment_v3_loader_blocked",
                    "zero_orders_is_a_valid_daily_decision",
                ),
                failed_stage="DAILY_SIGNAL_ADMISSION",
                failure_codes=("formal_experiment_v3_loader_blocked",),
                failure_receipt_sha256=blocker_hash,
            )

    artifacts = write_daily_decision(output_directory, decision)
    evidence = _write_daily_evidence(
        output_directory,
        strategy_date=strategy_date,
        frozen_data=frozen_data,
        ranking=ranking,
        exposure_inputs=exposure_inputs,
        exposure=exposure,
        construction=construction,
        intent=intent,
        approved_factor_registry=approved_factor_registry,
        experiment_v3_admission_receipt=experiment_v3_admission_receipt,
        experiment_v3_governance=experiment_v3_governance,
    )
    _register_exposure_state_artifacts(
        strategy_date=strategy_date,
        daily_decision=decision,
        exposure_inputs=exposure_inputs,
        exposure_decision=exposure,
    )
    daily_admission: DailySignalAdmissionReceiptV1 | None = None
    daily_publication: DailySignalPublicationReceiptV1 | None = None
    if construction is not None:
        daily_admission, daily_publication = _publish_daily_signal_evidence(
            output_directory=output,
            ranking=ranking,
            alpha_model=alpha_model,
            approved_factor_registry=approved_factor_registry,
            exposure=exposure,
            exposure_policy=exposure_policy,
            construction=construction,
            intent=intent,
            constructor_policy=constructor_policy,
            experiment_v3_admission_receipt=experiment_v3_admission_receipt,
            account=account,
            calendar_receipt=calendar_receipt,
            calendar_registry=calendar_registry,
            fees=fees,
            instrument_rules=frozen_data.instrument_rules,
            execution_rule_bundle_hash=rule_bundle_hash,
            daily_decision=decision,
        )
    if (
        construction is not None
        and daily_publication is not None
        and daily_publication.next_session_allowed
    ):
        signal = create_risk_next_session_signal(
            intent=intent,
            construction=construction,
            policy=constructor_policy,
            experiment_v3_admission_receipt=experiment_v3_admission_receipt,
            daily_signal_publication_receipt=daily_publication,
            receipt=calendar_receipt,
            registry=calendar_registry,
            fees=fees,
            instrument_rules=frozen_data.instrument_rules,
        )
        written_signal_path = write_new_next_session_signal(
            signal_path,
            signal,
            registry=calendar_registry,
            experiment_v3_admission_receipt=(
                experiment_v3_admission_receipt
            ),
            daily_signal_publication_receipt=daily_publication,
        )
    return DailyPipelineRunV1(
        frozen_data=frozen_data,
        alpha_ranking=ranking,
        exposure_inputs=exposure_inputs,
        exposure_decision=exposure,
        construction=construction,
        portfolio_intent=intent,
        next_session_signal=signal,
        daily_decision=decision,
        artifacts=artifacts,
        evidence_artifacts=evidence,
        daily_signal_admission_receipt=daily_admission,
        daily_signal_publication_receipt=daily_publication,
        signal_path=written_signal_path,
    )


def run_after_close_daily_pipeline(
    *,
    strategy_date: date,
    data_updater: DailyDataUpdaterV2,
    approved_factor_registry: ApprovedFactorRegistryV1,
    experiment_v3_admission_receipt: ExperimentV3AdmissionReceiptV1,
    alpha_model: FrozenAlphaModelV2,
    exposure_policy: ExposureHysteresisPolicyV2,
    exposure_memory: ExposureStateMemoryV2,
    constructor_policy: PortfolioConstructorPolicy,
    account: AccountSnapshot,
    fees: FeeSchedule,
    calendar_receipt: OfficialCalendarReceipt,
    calendar_registry: OfficialCalendarRegistry,
    output_directory: str | Path,
    signal_path: str | Path,
    paper_ledger_path: str | Path | None = None,
    manual_pause: bool = False,
    adaptive_policy_sha256: str = FROZEN_ADAPTIVE_EXPOSURE_POLICY_SHA256,
) -> DailyPipelineRunV1 | DailyPipelineBlockedRunV1:
    """Run stages 1-9 with an auditable fail-closed daily boundary.

    Expected data/model/configuration validation failures become a create-only
    BLOCKED decision with zero orders.  Immutable collisions and concurrent
    writes remain loud integrity errors and are never converted.
    """

    if type(strategy_date) is not date:
        raise DailyPipelineError("strategy_date must be a date")
    if LOCKED_TEST_START <= strategy_date <= LOCKED_TEST_END:
        raise DailyPipelineError(
            "experiment_v3_not_frozen_locked_test_execution_forbidden"
        )
    output = Path(output_directory)
    if not output.exists() or not output.is_dir() or output.is_symlink():
        raise DailyPipelineError(
            "daily decision output directory must already exist and be regular"
        )
    try:
        return _run_after_close_daily_pipeline_impl(
            strategy_date=strategy_date,
            data_updater=data_updater,
            approved_factor_registry=approved_factor_registry,
            experiment_v3_admission_receipt=experiment_v3_admission_receipt,
            alpha_model=alpha_model,
            exposure_policy=exposure_policy,
            exposure_memory=exposure_memory,
            constructor_policy=constructor_policy,
            account=account,
            fees=fees,
            calendar_receipt=calendar_receipt,
            calendar_registry=calendar_registry,
            output_directory=output_directory,
            signal_path=signal_path,
            paper_ledger_path=paper_ledger_path,
            manual_pause=manual_pause,
            adaptive_policy_sha256=adaptive_policy_sha256,
        )
    except (DailyPipelineIntegrityError, NextSessionSignalConflict):
        raise
    except (
        AlphaEngineError,
        DailyPipelineError,
        ExposureEngineError,
        NextSessionSignalError,
        PortfolioConstructionError,
    ) as exc:
        return _write_pipeline_failure_decision(
            strategy_date=strategy_date,
            failed_stage="PIPELINE_VALIDATION",
            data_status="DATA_FAIL_CLOSED",
            failure_code=f"pipeline_validation_{type(exc).__name__}",
            failure_exception_type=type(exc).__name__,
            approved_factor_registry=approved_factor_registry,
            experiment_v3_admission_receipt=experiment_v3_admission_receipt,
            alpha_model=alpha_model,
            exposure_policy=exposure_policy,
            exposure_memory=exposure_memory,
            constructor_policy=constructor_policy,
            account=account,
            calendar_receipt=calendar_receipt,
            calendar_registry=calendar_registry,
            output_directory=output_directory,
            adaptive_policy_sha256=adaptive_policy_sha256,
        )


def run_pre_open_review(
    signal_path: str | Path,
    consumption_path: str | Path,
    *,
    calendar_registry: OfficialCalendarRegistry,
    experiment_v3_admission_receipt: ExperimentV3AdmissionReceiptV1,
    daily_signal_publication_receipt: DailySignalPublicationReceiptV1 | None = None,
    account: AccountSnapshot,
    quotes: Mapping[str, MarketQuote],
    fees: FeeSchedule,
    instrument_rules: Mapping[str, InstrumentRule],
    checked_at: datetime,
) -> NextSessionConsumption:
    """Execute stage 10 once; returns instructions, never broker authority."""

    return consume_next_session_signal(
        signal_path,
        consumption_path,
        registry=calendar_registry,
        experiment_v3_admission_receipt=experiment_v3_admission_receipt,
        daily_signal_publication_receipt=daily_signal_publication_receipt,
        account=account,
        quotes=quotes,
        fees=fees,
        instrument_rules=instrument_rules,
        checked_at=checked_at,
    )


@dataclass(frozen=True, slots=True)
class ManualFillConfirmationV1:
    instrument_id: str
    side: str
    status: str
    filled_quantity: int
    attempted_at: datetime
    reference_open: Decimal
    fill_price: Decimal | None
    evidence_sha256: str
    blocked_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument_id", _instrument(self.instrument_id))
        side = str(self.side).strip().upper()
        status = str(self.status).strip().upper()
        if side not in {"BUY", "SELL"} or status not in {
            "FILLED",
            "PARTIAL",
            "UNFILLED",
        }:
            raise DailyPipelineError("manual fill side/status is invalid")
        if type(self.filled_quantity) is not int or self.filled_quantity < 0:
            raise DailyPipelineError("filled_quantity must be non-negative")
        if not isinstance(self.attempted_at, datetime) or self.attempted_at.tzinfo is None:
            raise DailyPipelineError("attempted_at must be timezone-aware")
        reference = _decimal(self.reference_open, "reference_open")
        fill = None if self.fill_price is None else _decimal(self.fill_price, "fill_price")
        if reference <= ZERO or (fill is not None and fill <= ZERO):
            raise DailyPipelineError("manual fill prices must be positive")
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "reference_open", reference)
        object.__setattr__(self, "fill_price", fill)
        object.__setattr__(
            self, "evidence_sha256", _sha256(self.evidence_sha256, "evidence_sha256")
        )


@dataclass(frozen=True, slots=True)
class ManualFillBundleV1:
    batch_id: str
    signal_id: str
    signal_sha256: str
    consumption_sha256: str
    frozen_execution_rule_bundle_sha256: str
    review_execution_rule_bundle_sha256: str
    execution_cost_bundle_sha256: str
    intent_id: str
    intent_sha256: str
    execution_date: date
    attempts: tuple[PaperExecutionAttemptV2, ...]
    fill_bundle_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        batch_id = str(self.batch_id).strip()
        if not batch_id or any(character.isspace() for character in batch_id):
            raise DailyPipelineError("manual fill batch_id is invalid")
        attempts = tuple(self.attempts)
        if any(not isinstance(item, PaperExecutionAttemptV2) for item in attempts):
            raise DailyPipelineError(
                "manual fill bundle requires PaperExecutionAttemptV2 attempts"
            )
        if any(
            item.attempt_id != batch_id
            or item.intent_id != self.intent_id
            or item.intent_sha256 != self.intent_sha256
            or item.execution_session != self.execution_date
            for item in attempts
        ):
            raise DailyPipelineError("manual fill attempts do not bind the batch/intent/date")
        if any(
            item.execution_cost_bundle_sha256
            != self.execution_cost_bundle_sha256
            for item in attempts
        ):
            raise DailyPipelineError(
                "manual fill attempts do not bind one execution cost bundle"
            )
        object.__setattr__(self, "batch_id", batch_id)
        object.__setattr__(self, "attempts", attempts)
        object.__setattr__(
            self, "signal_sha256", _sha256(self.signal_sha256, "signal_sha256")
        )
        object.__setattr__(
            self,
            "consumption_sha256",
            _sha256(self.consumption_sha256, "consumption_sha256"),
        )
        object.__setattr__(
            self,
            "frozen_execution_rule_bundle_sha256",
            _sha256(
                self.frozen_execution_rule_bundle_sha256,
                "frozen_execution_rule_bundle_sha256",
            ),
        )
        object.__setattr__(
            self,
            "review_execution_rule_bundle_sha256",
            _sha256(
                self.review_execution_rule_bundle_sha256,
                "review_execution_rule_bundle_sha256",
            ),
        )
        object.__setattr__(
            self,
            "execution_cost_bundle_sha256",
            _sha256(
                self.execution_cost_bundle_sha256,
                "execution_cost_bundle_sha256",
            ),
        )
        object.__setattr__(
            self, "intent_sha256", _sha256(self.intent_sha256, "intent_sha256")
        )
        object.__setattr__(
            self,
            "fill_bundle_sha256",
            canonical_sha256(self.to_content_dict()),
        )

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "manual-fill-bundle.v1",
            "batch_id": self.batch_id,
            "signal_id": self.signal_id,
            "signal_sha256": self.signal_sha256,
            "consumption_sha256": self.consumption_sha256,
            "frozen_execution_rule_bundle_sha256": (
                self.frozen_execution_rule_bundle_sha256
            ),
            "review_execution_rule_bundle_sha256": (
                self.review_execution_rule_bundle_sha256
            ),
            "execution_cost_bundle_sha256": self.execution_cost_bundle_sha256,
            "intent_id": self.intent_id,
            "intent_sha256": self.intent_sha256,
            "execution_date": self.execution_date,
            "attempts": [item.to_dict() for item in self.attempts],
            "manual_confirmed": True,
            "automatic_submission": False,
            "live_supported": False,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.to_content_dict(),
            "fill_bundle_sha256": self.fill_bundle_sha256,
        }


def record_manual_fills(
    output_path: str | Path,
    *,
    batch_id: str,
    consumption_path: str | Path,
    signal: NextSessionSignal,
    consumption: NextSessionConsumption,
    intent: PortfolioIntent,
    experiment_v3_admission_receipt: ExperimentV3AdmissionReceiptV1,
    daily_signal_publication_receipt: DailySignalPublicationReceiptV1 | None = None,
    calendar_registry: OfficialCalendarRegistry,
    fees: FeeSchedule,
    instrument_rules: Mapping[str, InstrumentRule],
    confirmations: Sequence[ManualFillConfirmationV1],
) -> ManualFillBundleV1:
    """Persist stage-11 operator evidence for every READY instruction."""

    try:
        reloaded_consumption = read_next_session_consumption(
            consumption_path,
            signal=signal,
            experiment_v3_admission_receipt=(
                experiment_v3_admission_receipt
            ),
            daily_signal_publication_receipt=(
                daily_signal_publication_receipt
            ),
            registry=calendar_registry,
        )
    except NextSessionSignalError as exc:
        raise DailyPipelineError(
            "manual fills require the exact immutable one-shot consumption artifact"
        ) from exc
    if reloaded_consumption.to_dict() != consumption.to_dict():
        raise DailyPipelineError(
            "caller consumption differs from the canonical reloaded artifact"
        )
    consumption = reloaded_consumption
    expected_fill_path = canonical_manual_fill_bundle_path(
        consumption.consumption_sha256
    )
    requested_fill_path = Path(output_path)
    if (
        requested_fill_path.resolve(strict=False)
        != expected_fill_path.resolve(strict=False)
    ):
        raise DailyPipelineError(
            "manual fill output_path must be the canonical consumption-hash-derived CAS slot"
        )
    if (
        not expected_fill_path.parent.is_dir()
        or expected_fill_path.parent.is_symlink()
    ):
        raise DailyPipelineError(
            "manual fill registry is absent or unsafe"
        )
    if (
        consumption.signal_id != signal.signal_id
        or consumption.signal_sha256 != signal.signal_sha256
        or signal.intent_sha256 != intent.intent_sha256
        or consumption.execution_date != signal.execution_date
    ):
        raise DailyPipelineError("manual fill inputs do not share one signal/intent/date")
    cost_bundle = CanonicalExecutionCostBundleV1(
        fee_schedule=fees,
        instrument_rules=instrument_rules,
    )
    if (
        cost_bundle.execution_rule_bundle_sha256
        != consumption.execution_rule_bundle_sha256
    ):
        raise DailyPipelineError(
            "manual fill cost bundle differs from the reviewed execution rules"
        )
    ready = {
        item.instrument_id: item
        for item in consumption.instructions
        if item.status.value == "READY_FOR_MANUAL_EXECUTION"
        and item.action in {"BUY", "SELL"}
        and item.instrument_id is not None
    }
    if set(ready) - set(instrument_rules):
        raise DailyPipelineError(
            "manual fill InstrumentRule mapping misses a READY instrument"
        )
    rule_bundle_drifted = (
        consumption.execution_rule_bundle_sha256
        != signal.execution_rule_bundle_sha256
    )
    if ready and rule_bundle_drifted:
        raise DailyPipelineError(
            "READY instructions cannot survive execution-rule bundle drift"
        )
    if rule_bundle_drifted and (
        "execution_rule_bundle_mismatch" not in consumption.cancel_reasons
    ):
        raise DailyPipelineError(
            "execution-rule bundle drift requires an evidenced cancellation"
        )
    confirmation_rows = tuple(confirmations)
    if any(not isinstance(item, ManualFillConfirmationV1) for item in confirmation_rows):
        raise DailyPipelineError("confirmations must be ManualFillConfirmationV1")
    by_id = {item.instrument_id: item for item in confirmation_rows}
    if len(by_id) != len(confirmation_rows) or set(by_id) != set(ready):
        raise DailyPipelineError(
            "manual confirmations must exactly cover READY trade instructions"
        )
    attempts: list[PaperExecutionAttemptV2] = []
    for instrument_id in sorted(ready):
        instruction = ready[instrument_id]
        confirmation = by_id[instrument_id]
        if confirmation.side != instruction.action:
            raise DailyPipelineError("manual confirmation side differs from instruction")
        if confirmation.attempted_at < consumption.checked_at:
            raise DailyPipelineError(
                "manual fill attempt predates the frozen D+1 review"
            )
        if confirmation.filled_quantity > instruction.quantity:
            raise DailyPipelineError("manual fill exceeds the reviewed quantity")
        if confirmation.side == "BUY":
            frozen_reference = signal.frozen_reference_prices[instrument_id]
            ceiling = frozen_reference * (
                ONE + signal.maximum_execution_price_deviation
            )
            if confirmation.reference_open > ceiling or (
                confirmation.fill_price is not None
                and confirmation.fill_price > ceiling
            ):
                raise DailyPipelineError(
                    "manual BUY opening/fill price exceeds the frozen deviation ceiling"
                )
        if (
            instruction.observed_execution_price is None
            or confirmation.reference_open
            != instruction.observed_execution_price
        ):
            raise DailyPipelineError(
                "manual fill reference_open differs from the frozen reviewed quote"
            )
        attempts.append(
            PaperExecutionAttemptV2(
                attempt_id=batch_id,
                intent_id=intent.intent_id,
                intent_sha256=intent.intent_sha256,
                instrument_id=instrument_id,
                side=confirmation.side,
                status=confirmation.status,
                requested_quantity=instruction.quantity,
                filled_quantity=confirmation.filled_quantity,
                execution_session=consumption.execution_date,
                attempted_at=confirmation.attempted_at,
                reference_open=confirmation.reference_open,
                fill_price=confirmation.fill_price,
                evidence_sha256=confirmation.evidence_sha256,
                execution_cost_bundle_sha256=cost_bundle.cost_bundle_sha256,
                commission_rate=fees.commission_rate,
                minimum_commission=fees.minimum_commission,
                sell_tax_rate=instrument_rules[
                    instrument_id
                ].sell_stamp_duty_rate,
                transfer_fee_rate=fees.exchange_fee_rate,
                blocked_reason=confirmation.blocked_reason,
                manual_confirmed=True,
                auto_submitted=False,
                live_order_id=None,
            )
        )
    bundle = ManualFillBundleV1(
        batch_id=batch_id,
        signal_id=signal.signal_id,
        signal_sha256=signal.signal_sha256,
        consumption_sha256=consumption.consumption_sha256,
        frozen_execution_rule_bundle_sha256=(
            signal.execution_rule_bundle_sha256
        ),
        review_execution_rule_bundle_sha256=(
            consumption.execution_rule_bundle_sha256
        ),
        execution_cost_bundle_sha256=cost_bundle.cost_bundle_sha256,
        intent_id=intent.intent_id,
        intent_sha256=intent.intent_sha256,
        execution_date=consumption.execution_date,
        attempts=tuple(attempts),
    )
    encoded = canonical_json_bytes(bundle.to_dict()) + b"\n"
    try:
        descriptor = os.open(
            expected_fill_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as exc:
        raise DailyPipelineIntegrityError(
            "manual fill bundle was already recorded for this consumption"
        ) from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        # Keep the exclusive path after a partial write.  Manual recovery is
        # safer than permitting another Stage-11 winner for the same review.
        raise
    return bundle


def append_close_paper_ledger_v2(
    ledger_path: str | Path,
    *,
    trading_date: date,
    execution_intent: PortfolioIntent,
    closing_intent: PortfolioIntent,
    next_session_signal: NextSessionSignal,
    experiment_v3_admission_receipt: ExperimentV3AdmissionReceiptV1,
    fill_bundle: ManualFillBundleV1,
    execution_cost_bundle: CanonicalExecutionCostBundleV1,
    close_mark_bundle: ControlledCloseMarkBundleV1,
    expected_previous_sha256: str | None = None,
) -> VerifiedPaperLedgerV2:
    """Append stage 12 to Paper Ledger V2 from manual evidence only."""

    if not isinstance(
        experiment_v3_admission_receipt,
        ExperimentV3AdmissionReceiptV1,
    ):
        raise DailyPipelineError(
            "ledger close requires a typed Experiment V3 admission receipt"
        )
    if (
        not isinstance(next_session_signal, NextSessionSignal)
        or canonical_sha256(next_session_signal.to_content_dict())
        != next_session_signal.signal_sha256
        or next_session_signal.experiment_admission_receipt_sha256
        != experiment_v3_admission_receipt.receipt_sha256
    ):
        raise DailyPipelineError(
            "ledger close signal does not bind the supplied Experiment V3 receipt"
        )
    try:
        close_as_of = datetime.combine(
            trading_date,
            OFFICIAL_CLOSE_TIME,
            CHINA_STANDARD_TIME,
        )
        if next_session_signal.channel is NextSessionChannel.RISK_REDUCTION:
            verify_experiment_v3_diagnostic_binding(
                experiment_v3_admission_receipt,
                as_of=close_as_of
            )
            if (
                experiment_v3_admission_receipt.experiment_spec_sha256
                != next_session_signal.experiment_spec_sha256
                or experiment_v3_admission_receipt.approved_factor_registry_sha256
                != next_session_signal.approved_factor_registry_sha256
                or experiment_v3_admission_receipt.model_sha256
                != next_session_signal.model_sha256
            ):
                raise ExperimentV3AdmissionError(
                    "risk close receipt differs from the frozen signal"
                )
        else:
            verify_experiment_v3_admission_receipt(
                experiment_v3_admission_receipt,
                as_of=close_as_of,
            )
    except ExperimentV3AdmissionError as exc:
        raise DailyPipelineError(
            "ledger close rejected the Experiment V3 admission receipt"
        ) from exc
    if not isinstance(fill_bundle, ManualFillBundleV1):
        raise DailyPipelineError("fill_bundle must be ManualFillBundleV1")
    persisted_fill_path = canonical_manual_fill_bundle_path(
        fill_bundle.consumption_sha256
    )
    expected_fill_bytes = canonical_json_bytes(fill_bundle.to_dict()) + b"\n"
    if (
        not persisted_fill_path.is_file()
        or persisted_fill_path.is_symlink()
        or persisted_fill_path.read_bytes() != expected_fill_bytes
    ):
        raise DailyPipelineError(
            "ledger close requires the exact immutable canonical manual fill bundle"
        )
    if fill_bundle.execution_date != trading_date:
        raise DailyPipelineError("manual fill bundle date differs from ledger close date")
    if fill_bundle.signal_sha256 != next_session_signal.signal_sha256:
        raise DailyPipelineError(
            "manual fill bundle does not bind the next-session signal"
        )
    if (
        fill_bundle.intent_id != execution_intent.intent_id
        or fill_bundle.intent_sha256 != execution_intent.intent_sha256
    ):
        raise DailyPipelineError("manual fill bundle does not bind execution_intent")
    if not isinstance(execution_cost_bundle, CanonicalExecutionCostBundleV1):
        raise DailyPipelineError("execution_cost_bundle must be canonical and typed")
    if not isinstance(close_mark_bundle, ControlledCloseMarkBundleV1):
        raise DailyPipelineError("close_mark_bundle must be controlled and typed")
    if (
        fill_bundle.execution_cost_bundle_sha256
        != execution_cost_bundle.cost_bundle_sha256
        or fill_bundle.review_execution_rule_bundle_sha256
        != execution_cost_bundle.execution_rule_bundle_sha256
    ):
        raise DailyPipelineError(
            "ledger cost bundle differs from the D+1 reviewed execution bundle"
        )
    execution_evidence = PaperCloseExecutionEvidenceV1(
        signal_id=fill_bundle.signal_id,
        signal_sha256=fill_bundle.signal_sha256,
        consumption_sha256=fill_bundle.consumption_sha256,
        fill_bundle_sha256=fill_bundle.fill_bundle_sha256,
        frozen_execution_rule_bundle_sha256=(
            fill_bundle.frozen_execution_rule_bundle_sha256
        ),
        review_execution_rule_bundle_sha256=(
            fill_bundle.review_execution_rule_bundle_sha256
        ),
        execution_cost_bundle_sha256=(
            execution_cost_bundle.cost_bundle_sha256
        ),
        execution_intent_sha256=execution_intent.intent_sha256,
    )
    draft = PaperDailySessionDraftV2(
        trading_date=trading_date,
        execution_intent=execution_intent,
        closing_intent=closing_intent,
        attempts=fill_bundle.attempts,
        execution_cost_bundle=execution_cost_bundle,
        close_mark_bundle=close_mark_bundle,
        execution_evidence=execution_evidence,
    )
    return append_paper_daily_session_v2(
        ledger_path,
        draft,
        expected_previous_sha256=expected_previous_sha256,
    )


__all__ = [
    "DAILY_DECISION_SCHEMA_VERSION",
    "FROZEN_DAILY_DATA_SCHEMA_VERSION",
    "ControlledHeldPositionReferenceV1",
    "DailyDataUpdaterV2",
    "DailyDecisionArtifacts",
    "DailyEvidenceArtifacts",
    "DailyHoldV1",
    "DailyOrderV1",
    "DailyPipelineError",
    "DailyPipelineIntegrityError",
    "DailyPipelineBlockedRunV1",
    "DailyPipelineRunV1",
    "DailyStrategyDecisionV2",
    "FrozenDailyDataV2",
    "ManualFillBundleV1",
    "ManualFillConfirmationV1",
    "append_close_paper_ledger_v2",
    "record_manual_fills",
    "render_daily_decision_markdown",
    "run_after_close_daily_pipeline",
    "run_pre_open_review",
    "write_daily_decision",
]
