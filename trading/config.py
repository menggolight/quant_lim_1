"""Validated JSON configuration for the small-account execution layer."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading.costs import FeeSchedule
from trading.risk import RiskLimits


@dataclass(frozen=True)
class TradingConfig:
    strategy_id: str
    execution_status: str
    fees: FeeSchedule
    limits: RiskLimits
    fee_verified_for_user_account: bool
    real_trading_whitelist: tuple[str, ...]
    broker_adapter: str | None
    live_order_submission_enabled: bool
    raw: dict[str, Any]


def _decimal(value: Any, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"Invalid decimal for {field}") from exc


def _boolean(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field} must be a JSON boolean")
    return value


def load_trading_config(path: Path | str) -> TradingConfig:
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "1.0":
        raise ValueError("Unsupported trading config schema")
    if payload.get("execution_status") not in {"paper_only", "shadow_only", "live"}:
        raise ValueError("Invalid execution_status")

    capital = payload["capital"]
    universe = payload["universe"]
    fee = payload["fee_assumption"]
    risk = payload["risk"]
    readiness = payload["live_readiness"]
    fees = FeeSchedule(
        commission_rate=_decimal(fee["commission_rate"], "commission_rate"),
        minimum_commission=_decimal(fee["minimum_commission"], "minimum_commission"),
        exchange_fee_rate=_decimal(fee["exchange_fee_rate"], "exchange_fee_rate"),
    )
    limits = RiskLimits(
        strategy_capital_limit=_decimal(capital["strategy_capital_limit"], "strategy_capital_limit"),
        allowed_instrument_types=tuple(universe["allowed_instrument_types"]),
        max_positions=int(universe["max_positions"]),
        max_position_weight=_decimal(universe["max_position_weight"], "max_position_weight"),
        cash_reserve_ratio=_decimal(capital["cash_reserve_ratio"], "cash_reserve_ratio"),
        minimum_trade_notional=_decimal(risk["minimum_trade_notional"], "minimum_trade_notional"),
        max_orders_per_plan=int(risk["max_orders_per_plan"]),
        max_order_notional_ratio=_decimal(
            risk["max_order_notional_ratio"], "max_order_notional_ratio"
        ),
        max_daily_turnover_ratio=_decimal(
            risk["max_daily_turnover_ratio"], "max_daily_turnover_ratio"
        ),
        bootstrap_turnover_ratio=_decimal(risk["bootstrap_turnover_ratio"], "bootstrap_turnover_ratio"),
        maximum_quote_age_seconds=int(risk["maximum_quote_age_seconds"]),
        maximum_daily_loss_ratio=_decimal(risk["maximum_daily_loss_ratio"], "maximum_daily_loss_ratio"),
        allowed_instrument_ids=tuple(universe["real_trading_whitelist"]),
        max_orders_per_day=int(risk["max_orders_per_day"]),
        maximum_spread_ratio=_decimal(risk["maximum_spread_ratio"], "maximum_spread_ratio"),
    )
    config = TradingConfig(
        strategy_id=str(payload["strategy_id"]),
        execution_status=str(payload["execution_status"]),
        fees=fees,
        limits=limits,
        fee_verified_for_user_account=_boolean(
            fee["verified_for_user_account"], "verified_for_user_account"
        ),
        real_trading_whitelist=tuple(universe["real_trading_whitelist"]),
        broker_adapter=readiness["broker_adapter"],
        live_order_submission_enabled=_boolean(
            readiness["live_order_submission_enabled"], "live_order_submission_enabled"
        ),
        raw=payload,
    )
    if config.execution_status == "live":
        if not config.fee_verified_for_user_account:
            raise ValueError("Live config requires verified account fees")
        if not config.real_trading_whitelist:
            raise ValueError("Live config requires a non-empty trading whitelist")
        if not config.broker_adapter or not config.live_order_submission_enabled:
            raise ValueError("Live config requires an enabled official broker adapter")
    return config
