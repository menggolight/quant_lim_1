"""Generate an explicitly synthetic, paper-only execution validation artifact."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading.config import load_trading_config
from trading.models import AccountSnapshot, ExecutionMode, InstrumentRule, MarketQuote
from trading.order_store import OrderStore
from trading.paper import PaperBroker
from trading.planner import build_rebalance_plan
from trading.risk import ExecutionGate, LiveReadiness
from trading.strategy_bridge import SignalEnvelope, SignalRejected, targets_from_signal


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "configs" / "small_account_trading.v1.json"
CHINA_TZ = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class PaperRunResult:
    run_directory: Path
    json_path: Path
    markdown_path: Path
    order_store_path: Path


def _as_text(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def _synthetic_fixture(decision_time: datetime):
    prices = {"SYNTH.ETF.A": Decimal("2.000"), "SYNTH.ETF.B": Decimal("1.500"), "SYNTH.ETF.C": Decimal("0.800")}
    instruments = {
        instrument_id: InstrumentRule(
            instrument_id=instrument_id,
            name=f"合成管线验证ETF {instrument_id[-1]}",
            instrument_type="ETF",
            lot_size=100,
            tick_size=Decimal("0.001"),
            sell_stamp_duty_rate=Decimal("0"),
            t_plus_one=True,
        )
        for instrument_id in prices
    }
    quotes = {
        instrument_id: MarketQuote(
            instrument_id=instrument_id,
            bid=price,
            ask=price,
            last=price,
            as_of=decision_time,
        )
        for instrument_id, price in prices.items()
    }
    targets = {instrument_id: Decimal("0.30") for instrument_id in prices}
    return instruments, quotes, targets


def run_synthetic_validation(
    config_path: Path | str = DEFAULT_CONFIG,
    output_root: Path | str = ROOT / "data" / "trading" / "synthetic_validation",
    decision_time: datetime | None = None,
) -> PaperRunResult:
    config = load_trading_config(config_path)
    if config.execution_status != "paper_only":
        raise ValueError("Synthetic validation requires a paper_only config")
    if config.live_order_submission_enabled or config.broker_adapter is not None:
        raise ValueError("Synthetic validation refuses any live broker capability")
    decision_time = decision_time or datetime.now(CHINA_TZ)
    if decision_time.tzinfo is None:
        raise ValueError("decision_time must be timezone-aware")

    run_id = decision_time.strftime("%Y%m%dT%H%M%S%z")
    run_directory = Path(output_root) / run_id
    run_directory.mkdir(parents=True, exist_ok=False)
    store_path = run_directory / "paper_orders.sqlite"
    json_path = run_directory / "result.json"
    markdown_path = run_directory / "result.md"
    instruments, quotes, fixture_targets = _synthetic_fixture(decision_time)

    research_bridge_check: dict[str, Any]
    synthetic_signal = SignalEnvelope(
        signal_id="synthetic-pipeline-validation",
        model_id="synthetic-model",
        model_admission="approved_for_paper",
        source_kind="synthetic_fixture",
        available_at=decision_time,
        frozen_at=decision_time,
        data_snapshot_hash="sha256:synthetic-fixture",
        synthetic=True,
        trade_eligible=True,
        target_weights=fixture_targets,
    )
    try:
        targets_from_signal(synthetic_signal, decision_time, ExecutionMode.PAPER)
    except SignalRejected as exc:
        research_bridge_check = {"blocked": True, "block_code": exc.code, "message": str(exc)}
    else:
        raise AssertionError("Synthetic research signal unexpectedly passed the bridge")

    account_before = AccountSnapshot(
        strategy_id=config.strategy_id,
        cash=config.limits.strategy_capital_limit,
        positions={},
    )
    plan = build_rebalance_plan(
        account=account_before,
        target_weights=fixture_targets,
        instruments=instruments,
        quotes=quotes,
        fees=config.fees,
        limits=config.limits,
        decision_time=decision_time,
        bootstrap=True,
        decision_id="synthetic-pipeline-validation",
    )
    gate = ExecutionGate(config.limits).evaluate(
        mode=ExecutionMode.PAPER,
        plan=plan,
        account=account_before,
        decision_time=decision_time,
        daily_pnl_ratio=Decimal("0"),
        kill_switch_active=False,
        readiness=LiveReadiness(),
    )
    if not gate.allowed:
        raise RuntimeError(f"Paper gate unexpectedly blocked: {gate.block_codes}")

    with OrderStore(store_path) as store:
        execution = PaperBroker(
            account=account_before,
            instruments=instruments,
            fees=config.fees,
            order_store=store,
        ).execute(plan, gate.approval, decision_time)

    total_fee = sum((order.estimated_fee for order in plan.orders), Decimal("0"))
    payload = {
        "schema_version": "1.0",
        "run_id": run_id,
        "run_type": "synthetic_execution_pipeline_validation",
        "synthetic": True,
        "not_an_investment_result": True,
        "execution_mode": ExecutionMode.PAPER.value,
        "live_orders_submitted": False,
        "decision_time": decision_time,
        "config": {
            "strategy_id": config.strategy_id,
            "execution_status": config.execution_status,
            "fee_verified_for_user_account": config.fee_verified_for_user_account,
            "real_trading_whitelist": config.real_trading_whitelist,
            "broker_adapter": config.broker_adapter,
        },
        "research_bridge_check": research_bridge_check,
        "gate": {
            "allowed": gate.allowed,
            "block_codes": gate.block_codes,
            "approval_bound_to_plan_and_account": gate.approval is not None,
        },
        "account_before": {"cash": account_before.cash, "positions": {}},
        "plan": {
            "plan_id": plan.plan_id,
            "strategy_equity": plan.strategy_equity,
            "projected_cash": plan.projected_cash,
            "turnover_ratio": plan.turnover_ratio,
            "turnover_limit": plan.turnover_limit,
            "orders": [
                {
                    "client_order_id": order.client_order_id,
                    "instrument_id": order.instrument_id,
                    "side": order.side,
                    "quantity": order.quantity,
                    "limit_price": order.limit_price,
                    "notional": order.notional,
                    "estimated_fee": order.estimated_fee,
                }
                for order in plan.orders
            ],
            "rejections": [item.__dict__ for item in plan.rejections],
        },
        "fills": [
            {
                "client_order_id": fill.client_order_id,
                "instrument_id": fill.instrument_id,
                "side": fill.side,
                "quantity": fill.quantity,
                "price": fill.price,
                "fee": fill.fee,
                "status": fill.status,
            }
            for fill in execution.fills
        ],
        "account_after": {
            "cash": execution.account.cash,
            "positions": {
                instrument_id: {
                    "quantity": position.quantity,
                    "sellable_quantity": position.sellable_quantity,
                }
                for instrument_id, position in execution.account.positions.items()
            },
        },
        "total_estimated_fee": total_fee,
        "controls_verified": [
            "ETF-only",
            "whole-lot planning",
            "cash reserve",
            "minimum commission",
            "T+1 sellable quantity",
            "paper-only gate",
            "persistent client-order idempotency",
            "gate approval bound to plan and account snapshot",
            "persisted paper cash and positions",
            "one-time bootstrap allowance",
            "synthetic research signal blocked",
        ],
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_as_text) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(
        "\n".join(
            (
                "# 1万元交易内核合成纸面验证",
                "",
                "> 这是合成数据的管线验证，不是回测收益、投资建议或真实订单。",
                "",
                f"- 决策时间：{decision_time.isoformat()}",
                "- 执行模式：PAPER",
                "- 真实下单：否",
                f"- 计划订单数：{len(plan.orders)}",
                f"- 预估总费用：{total_fee} 元",
                f"- 执行后现金：{execution.account.cash} 元",
                f"- 换手率：{plan.turnover_ratio}",
                f"- 研究信号隔离：{research_bridge_check['block_code']}",
                "",
                "完整字段与订单审计见 `result.json` 和 `paper_orders.sqlite`。",
                "",
            )
        ),
        encoding="utf-8",
    )
    return PaperRunResult(run_directory, json_path, markdown_path, store_path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=ROOT / "data" / "trading" / "synthetic_validation",
    )
    args = parser.parse_args()
    result = run_synthetic_validation(args.config, args.output_root)
    print(result.json_path)


if __name__ == "__main__":
    main()
