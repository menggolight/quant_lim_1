"""Run one immutable, stateful Technical Shadow day with real BaoStock data.

This entry point publishes a manual D+1 plan only.  It cannot submit orders and
does not alter Paper, trade, real-money, or LIVE admission.
"""

from __future__ import annotations

import argparse
import importlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

from operations.run_technical_shadow_mvp import (
    ALPHA_LOOKBACK_SESSIONS,
    CHINA_TZ,
    STRATEGY_ID,
    BaoStockTechnicalShadowSource,
    CapturedData,
    TechnicalShadowRunError,
    _canonical_bytes,
    _cash_reason_codes,
    _decimal,
    _digest,
    _execute_targets,
    _execution_cost,
    _ledger_transaction_accounting,
    _load_config,
    _money,
    _money_text,
    _plan_targets,
    validate_source_provenance,
)
from research.market_data.providers.baostock import BaoStockProvider
from research.strategy_workspace.technical_alpha_shadow_v1 import (
    rank_technical_alpha_shadow,
)
from research.strategy_workspace.technical_exposure_shadow_v1 import (
    compute_technical_shadow_exposure,
)


DEFAULT_CONFIG = Path("configs/a_share_technical_shadow_mvp.v1.json")
DEFAULT_SEED = Path("configs/technical_shadow_daily_seed.v1.json")
DEFAULT_OUTPUT_ROOT = Path("data/tmp/technical-shadow-daily")
SHADOW_ACCOUNT_ID = "technical-shadow-account-v1"
MODE = "stateful_daily"
DECISION_CUTOFF = time(15, 30)
EXECUTION_OPEN = time(9, 30)
DAILY_SAFETY = {
    "strategy_signal": False,
    "alpha_evidence": False,
    "trade_recommendation": False,
    "paper_eligibility": False,
    "trade_eligibility": False,
    "real_money_list_allowed": False,
    "automatic_order_submission": False,
    "live_supported": False,
}
CANCELLATION_CONDITIONS = (
    "provider_or_data_receipt_validation_failed",
    "previous_state_or_hash_chain_mismatch",
    "execution_date_or_trade_calendar_changed",
    "execution_open_missing_or_instrument_not_tradable",
    "st_stock_buy_forbidden",
    "buy_price_above_maximum_buy_price",
    "cash_or_whole_lot_unavailable",
    "t_plus_one_sell_quantity_unavailable",
    "automatic_order_submission_forbidden",
)


class TechnicalShadowDailyError(TechnicalShadowRunError):
    """Fail-closed daily state/immutability error."""


@dataclass(frozen=True)
class NextSessionEvidence:
    execution_date: date
    receipt: Mapping[str, Any]
    execution_window_status: str = "OPEN"


def _file_sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TechnicalShadowDailyError(f"json_object_required:{path}")
    return value


def _strict_row_map(
    rows: Sequence[Mapping[str, Any]], *, instrument_id: str
) -> dict[date, Mapping[str, Any]]:
    mapped: dict[date, Mapping[str, Any]] = {}
    for row in rows:
        day = date.fromisoformat(str(row["trading_date"]))
        if day in mapped:
            raise TechnicalShadowDailyError(
                f"duplicate_trading_date:{instrument_id}:{day.isoformat()}"
            )
        mapped[day] = row
    return mapped


def _validate_row_cutoff(row: Mapping[str, Any], decision_date: date) -> None:
    trading_date = date.fromisoformat(str(row["trading_date"]))
    if trading_date > decision_date:
        raise TechnicalShadowDailyError("future_trading_date_rejected")
    available_at = datetime.fromisoformat(str(row["available_at"]))
    if available_at.tzinfo is None:
        raise TechnicalShadowDailyError("available_at_timezone_required")
    cutoff = datetime.combine(decision_date, DECISION_CUTOFF, CHINA_TZ)
    if available_at.astimezone(CHINA_TZ) > cutoff:
        raise TechnicalShadowDailyError("future_available_at_rejected")


def _load_seed(path: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    seed = _json(path)
    if (
        seed.get("schema_version") != "technical-shadow-daily-state-seed.v1"
        or seed.get("strategy_id") != STRATEGY_ID
        or seed.get("mode") != MODE
        or seed.get("shadow_account_id") != SHADOW_ACCOUNT_ID
    ):
        raise TechnicalShadowDailyError("daily_seed_identity_mismatch")
    if seed.get("config_sha256") != _digest(config):
        raise TechnicalShadowDailyError("daily_seed_config_sha256_mismatch")
    if seed.get("safety") != DAILY_SAFETY:
        raise TechnicalShadowDailyError("daily_seed_safety_mismatch")
    state = seed.get("state")
    if not isinstance(state, dict):
        raise TechnicalShadowDailyError("daily_seed_state_missing")
    required = {
        "state_date", "previous_trading_date", "previous_record_sha256",
        "cash", "positions", "sellable_quantities", "peak_nav", "drawdown",
        "exposure_state", "pending_state", "hysteresis_count",
    }
    if not required <= set(state):
        raise TechnicalShadowDailyError("daily_seed_state_incomplete")
    if _decimal(state["cash"]) < 0 or _decimal(state["peak_nav"]) <= 0:
        raise TechnicalShadowDailyError("daily_seed_account_invalid")
    return seed


def _verified_slot(path: Path) -> tuple[dict[str, Any], str]:
    manifest_path = path / "manifest.json"
    if not manifest_path.is_file():
        raise TechnicalShadowDailyError(f"partial_daily_slot_requires_manual_recovery:{path}")
    manifest = _json(manifest_path)
    if manifest.get("schema_version") != "technical-shadow-daily-manifest.v1":
        raise TechnicalShadowDailyError("daily_manifest_schema_mismatch")
    manifest_base = {
        key: value for key, value in manifest.items()
        if key != "manifest_payload_sha256"
    }
    if manifest.get("manifest_payload_sha256") != _digest(manifest_base):
        raise TechnicalShadowDailyError("daily_manifest_payload_sha256_mismatch")
    if (
        manifest.get("strategy_id") != STRATEGY_ID
        or manifest.get("shadow_account_id") != SHADOW_ACCOUNT_ID
        or manifest.get("mode") != MODE
        or manifest.get("safety") != DAILY_SAFETY
        or manifest.get("automatic_order_submission") is not False
    ):
        raise TechnicalShadowDailyError("daily_manifest_identity_or_safety_mismatch")
    expected_files = set(manifest.get("artifacts", {})) | {"manifest.json"}
    actual_files = {
        artifact.relative_to(path).as_posix()
        for artifact in path.rglob("*") if artifact.is_file()
    }
    if actual_files != expected_files:
        raise TechnicalShadowDailyError("daily_manifest_artifact_set_mismatch")
    for relative, expected in manifest.get("artifacts", {}).items():
        artifact = path / relative
        if not artifact.is_file() or _file_sha256(artifact) != expected:
            raise TechnicalShadowDailyError(f"daily_artifact_integrity_failed:{relative}")
    state = _json(path / "state.json")
    state_base = {key: value for key, value in state.items() if key != "record_sha256"}
    if state.get("record_sha256") != _digest(state_base):
        raise TechnicalShadowDailyError("daily_state_record_sha256_mismatch")
    plan = _json(path / "next_session_plan.json")
    plan_base = {key: value for key, value in plan.items() if key != "plan_payload_sha256"}
    if plan.get("plan_payload_sha256") != _digest(plan_base):
        raise TechnicalShadowDailyError("daily_plan_payload_sha256_mismatch")
    if (
        manifest.get("account_record_sha256") != state.get("record_sha256")
        or plan.get("based_on_account_record_sha256") != state.get("record_sha256")
        or state.get("safety") != DAILY_SAFETY
        or plan.get("safety") != DAILY_SAFETY
        or plan.get("automatic_order_submission") is not False
    ):
        raise TechnicalShadowDailyError("daily_state_plan_manifest_binding_mismatch")
    return manifest, _file_sha256(manifest_path)


def _latest_data_complete_capture(
    captured: CapturedData,
) -> CapturedData:
    """Trim a calendar-leading capture to the latest benchmark-complete close."""

    benchmark_dates = {
        date.fromisoformat(str(row["trading_date"]))
        for row in captured.benchmark_rows
        if row.get("close") is not None
    }
    completed = [day for day in captured.sessions if day in benchmark_dates]
    if not completed:
        raise TechnicalShadowDailyError("no_baostock_data_complete_strategy_date")
    latest = completed[-1]
    sessions = tuple(day for day in captured.sessions if day <= latest)
    if len(sessions) < 121:
        raise TechnicalShadowDailyError("latest_complete_capture_has_insufficient_history")
    return CapturedData(
        provider_id=captured.provider_id,
        provider_kind=captured.provider_kind,
        adapter_version=captured.adapter_version,
        synthetic=captured.synthetic,
        captured_at=captured.captured_at,
        sessions=sessions,
        stock_rows=captured.stock_rows,
        benchmark_rows=captured.benchmark_rows,
        receipts=captured.receipts,
    )


def _load_previous_context(
    *, strategy_date: date, output_root: Path, seed_path: Path,
    config: Mapping[str, Any], previous_session: date,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any]]:
    seed = _load_seed(seed_path, config)
    seed_state = dict(seed["state"])
    seed_date = date.fromisoformat(str(seed_state["state_date"]))
    if strategy_date < seed_date:
        raise TechnicalShadowDailyError("strategy_date_precedes_controlled_seed")
    if strategy_date == seed_date:
        return seed_state, None, {
            "kind": "controlled_seed_same_close",
            "seed_sha256": _file_sha256(seed_path),
            "bootstrap": seed["bootstrap"],
        }

    committed: list[tuple[date, Path]] = []
    if output_root.exists():
        for child in output_root.iterdir():
            if not child.is_dir():
                continue
            try:
                child_date = date.fromisoformat(child.name)
            except ValueError:
                continue
            if child_date < strategy_date:
                committed.append((child_date, child))
    if not committed:
        if previous_session != seed_date:
            raise TechnicalShadowDailyError("daily_state_gap_after_seed")
        return seed_state, None, {
            "kind": "controlled_seed_cash_carry_forward_without_prior_plan",
            "seed_sha256": _file_sha256(seed_path),
            "bootstrap": seed["bootstrap"],
            "reason_code": "NO_PRIOR_DAILY_PLAN_NO_RETROSPECTIVE_EXECUTION",
        }
    previous_date, previous_root = max(committed)
    manifest, manifest_sha = _verified_slot(previous_root)
    state = _json(previous_root / "state.json")
    plan = _json(previous_root / "next_session_plan.json")
    if date.fromisoformat(str(state["state_date"])) != previous_date:
        raise TechnicalShadowDailyError("previous_state_date_mismatch")
    if plan.get("decision_date") != previous_date.isoformat():
        raise TechnicalShadowDailyError("previous_plan_decision_date_mismatch")
    if plan.get("execution_date") != strategy_date.isoformat():
        raise TechnicalShadowDailyError("daily_state_gap_or_plan_execution_mismatch")
    if manifest.get("strategy_date") != previous_date.isoformat():
        raise TechnicalShadowDailyError("previous_manifest_date_mismatch")
    return state, plan, {
        "kind": "previous_daily_commit",
        "previous_manifest_sha256": manifest_sha,
        "previous_state_sha256": _file_sha256(previous_root / "state.json"),
        "previous_plan_sha256": _file_sha256(previous_root / "next_session_plan.json"),
    }


def query_next_baostock_session(*, after_date: date) -> NextSessionEvidence:
    sdk = importlib.import_module("baostock")
    login = sdk.login()
    BaoStockProvider._check_result(login, "login")
    try:
        start = after_date + timedelta(days=1)
        end = after_date + timedelta(days=14)
        result = sdk.query_trade_dates(
            start_date=start.isoformat(), end_date=end.isoformat()
        )
        fields, raw_rows = BaoStockProvider._query_rows(
            result, "query_trade_dates:next_session"
        )
        if set(fields) != {"calendar_date", "is_trading_day"}:
            raise TechnicalShadowDailyError("baostock_next_calendar_contract_changed")
        rows = [dict(zip(fields, row, strict=True)) for row in raw_rows]
        sessions = [
            date.fromisoformat(row["calendar_date"])
            for row in rows if row["is_trading_day"] == "1"
        ]
        if not sessions:
            raise TechnicalShadowDailyError("next_trading_session_unavailable")
        receipt = {
            "provider_id": "baostock",
            "provider_kind": "real_provider",
            "adapter_version": BaoStockTechnicalShadowSource.adapter_version,
            "request": {"start_date": start.isoformat(), "end_date": end.isoformat()},
            "fields": fields,
            "rows": raw_rows,
            "raw_content_sha256": _digest({"fields": fields, "rows": raw_rows}),
        }
        return NextSessionEvidence(execution_date=sessions[0], receipt=receipt)
    finally:
        try:
            sdk.logout()
        except Exception:
            pass


def _stable_data_receipt(
    *, captured: CapturedData, strategy_date: date,
    execution_evidence: NextSessionEvidence, config: Mapping[str, Any],
) -> dict[str, Any]:
    sessions = tuple(day for day in captured.sessions if day <= strategy_date)[-121:]
    if len(sessions) != 121 or sessions[-1] != strategy_date:
        raise TechnicalShadowDailyError("daily_requires_121_sessions_ending_strategy_date")
    instrument_ids = list(config["universe"]["instrument_ids"])
    benchmark = [
        dict(row) for row in captured.benchmark_rows
        if date.fromisoformat(str(row["trading_date"])) in set(sessions)
    ]
    stocks: dict[str, list[dict[str, Any]]] = {}
    for instrument_id in instrument_ids:
        stocks[instrument_id] = [
            dict(row) for row in captured.stock_rows.get(instrument_id, ())
            if date.fromisoformat(str(row["trading_date"])) in set(sessions)
        ]
    receipt_summaries = {}
    for key, receipt in sorted(captured.receipts.items()):
        receipt_summaries[key] = {
            field: receipt.get(field)
            for field in (
                "receipt_type", "provider_id", "provider_kind", "adapter_version",
                "synthetic", "instrument_id", "is_benchmark", "request",
                "record_count", "raw_content_sha256", "normalized_content_sha256",
            )
            if field in receipt
        }
    payload = {
        "schema_version": "technical-shadow-daily-data-receipt.v1",
        "strategy_id": STRATEGY_ID,
        "mode": MODE,
        "strategy_date": strategy_date.isoformat(),
        "execution_date": execution_evidence.execution_date.isoformat(),
        "execution_window_status": execution_evidence.execution_window_status,
        "decision_cutoff_at": datetime.combine(
            strategy_date, DECISION_CUTOFF, CHINA_TZ
        ).isoformat(),
        "provider": {
            "provider_id": captured.provider_id,
            "provider_kind": captured.provider_kind,
            "adapter_version": captured.adapter_version,
            "synthetic": captured.synthetic,
        },
        "sessions": [day.isoformat() for day in sessions],
        "benchmark_records": benchmark,
        "stock_records": stocks,
        "source_receipt_summaries": receipt_summaries,
        "next_session_calendar_receipt": execution_evidence.receipt,
        "universe_basis": config["universe"]["basis"],
        "historical_pit_csi800": False,
    }
    payload["data_content_sha256"] = _digest(payload)
    return payload


def _market_drawdown(rows: Sequence[Mapping[str, Any]]) -> float:
    closes = [float(row["close"]) for row in rows if row.get("close") is not None]
    if not closes or max(closes) <= 0:
        raise TechnicalShadowDailyError("benchmark_drawdown_unavailable")
    return closes[-1] / max(closes) - 1.0


def _exposure_conditions(
    exposure: Mapping[str, Any], policy: Mapping[str, Any]
) -> dict[str, Any]:
    trend = exposure.get("benchmark_trend")
    breadth = exposure.get("market_breadth")
    volatility = exposure.get("realized_volatility")
    drawdown = exposure.get("account_drawdown")
    if any(value is None for value in (trend, breadth, volatility, drawdown)):
        return {
            "risk_off": {"data_fail_closed": True},
            "defensive": {"data_fail_closed": True},
            "risk_on": {"data_fail_closed": True},
        }
    return {
        "risk_off": {
            "benchmark_trend_lte_max": trend <= float(policy["risk_off"]["benchmark_trend_max"]),
            "market_breadth_lt_max": breadth < float(policy["risk_off"]["breadth_max"]),
            "account_drawdown_lte_max_loss": drawdown <= float(policy["risk_off"]["account_drawdown_max_loss"]),
        },
        "defensive": {
            "market_breadth_lt_trigger": breadth < float(policy["defensive"]["breadth_trigger_below"]),
            "realized_volatility_gt_max": volatility > float(policy["defensive"]["realized_vol_max"]),
            "account_drawdown_lte_max_loss": drawdown <= float(policy["defensive"]["account_drawdown_max_loss"]),
        },
        "risk_on": {
            "benchmark_trend_gt_min": trend > float(policy["risk_on"]["benchmark_trend_min"]),
            "market_breadth_gte_min": breadth >= float(policy["risk_on"]["breadth_min"]),
            "realized_volatility_lte_max": volatility <= float(policy["risk_on"]["realized_vol_max"]),
            "account_drawdown_gt_min": drawdown > float(policy["risk_on"]["account_drawdown_min"]),
        },
    }


def _consume_lots(
    lots: list[dict[str, Any]], instrument_id: str, quantity: int, day: date
) -> None:
    remaining = quantity
    for lot in sorted(
        (item for item in lots if item["instrument_id"] == instrument_id),
        key=lambda item: (item["acquired_session"], item["lot_id"]),
    ):
        if date.fromisoformat(str(lot["sellable_from_session"])) > day:
            continue
        consumed = min(int(lot["quantity"]), remaining)
        lot["quantity"] = int(lot["quantity"]) - consumed
        remaining -= consumed
        if remaining == 0:
            break
    if remaining:
        raise TechnicalShadowDailyError("t_plus_one_lot_accounting_mismatch")
    lots[:] = [item for item in lots if int(item["quantity"]) > 0]


def _apply_previous_plan(
    *, state: Mapping[str, Any], plan: Mapping[str, Any] | None,
    strategy_date: date, next_session: date,
    stock_maps: Mapping[str, Mapping[date, Mapping[str, Any]]],
    config: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    positions = {str(key): int(value) for key, value in state.get("positions", {}).items()}
    cash = _money(_decimal(state["cash"]))
    lots = [dict(item) for item in state.get("position_lots", [])]
    if plan is None:
        same_close = str(state["state_date"]) == strategy_date.isoformat()
        return {
            "cash": cash, "positions": positions, "position_lots": lots,
            "fills": [], "transaction_cost": Decimal("0.00"),
        }, {
            "status": (
                "BOOTSTRAP_ALREADY_VALUED_CLOSE"
                if same_close else "NO_PRIOR_PLAN_CASH_CARRY_FORWARD"
            ),
            "decision_date": state["previous_trading_date"] if same_close else state["state_date"],
            "execution_date": strategy_date.isoformat(),
            "fills": [],
            "reason_codes": [
                "controlled_replay_state_already_includes_strategy_date_close"
                if same_close else "no_prior_daily_plan_no_retrospective_execution"
            ],
        }

    targets = {str(key): int(value) for key, value in plan["target_positions"].items()}
    sellable = {
        instrument_id: sum(
            int(lot["quantity"]) for lot in lots
            if lot["instrument_id"] == instrument_id
            and date.fromisoformat(str(lot["sellable_from_session"])) <= strategy_date
        )
        for instrument_id in positions
    }
    effective_targets = dict(targets)
    t1_cancellations: list[dict[str, Any]] = []
    for instrument_id, current in positions.items():
        requested_target = targets.get(instrument_id, 0)
        requested_sell = max(current - requested_target, 0)
        allowed_sell = min(requested_sell, sellable.get(instrument_id, 0))
        if allowed_sell < requested_sell:
            effective_targets[instrument_id] = current - allowed_sell
            t1_cancellations.append({
                "action": "SELL_CANCELLED",
                "instrument_id": instrument_id,
                "target_quantity": requested_target,
                "simulated_quantity": 0,
                "reason_codes": ["t_plus_one_sell_quantity_unavailable"],
            })
    execution_rows = {
        instrument_id: rows[strategy_date]
        for instrument_id, rows in stock_maps.items() if strategy_date in rows
    }
    maximum_by_instrument = {
        str(row["instrument_id"]): _decimal(row["maximum_buy_price"])
        for row in plan.get("actions", [])
        if row.get("action") == "BUY"
        and row.get("instrument_id")
        and row.get("maximum_buy_price") is not None
    }
    price_cancellations: list[dict[str, Any]] = []
    for instrument_id, target in targets.items():
        current = positions.get(instrument_id, 0)
        if target <= current or instrument_id not in maximum_by_instrument:
            continue
        row = execution_rows.get(instrument_id)
        if row is None or row.get("open") is None:
            continue
        actual = _execution_cost(
            side="BUY", quantity=target - current,
            open_price=_decimal(row["open"]), config=config,
        )
        if actual["execution_price"] > maximum_by_instrument[instrument_id]:
            effective_targets[instrument_id] = current
            price_cancellations.append({
                "action": "BUY_CANCELLED", "instrument_id": instrument_id,
                "target_quantity": target, "simulated_quantity": 0,
                "reference_price": _money_text(actual["reference_price"]),
                "execution_price": None,
                "maximum_buy_price": _money_text(maximum_by_instrument[instrument_id]),
                "reason_codes": ["buy_price_above_maximum_buy_price"],
            })
    before = dict(positions)
    positions, cash, fills, transaction_cost = _execute_targets(
        targets=effective_targets,
        positions=dict(positions),
        cash=cash,
        execution_rows=execution_rows,
        config=config,
        buy_order=[str(item) for item in plan.get("selected_instruments", [])],
    )
    fills.extend(t1_cancellations)
    fills.extend(price_cancellations)
    for fill in fills:
        if fill["action"] == "SELL":
            _consume_lots(
                lots, str(fill["instrument_id"]), int(fill["simulated_quantity"]),
                strategy_date,
            )
        elif fill["action"] == "BUY":
            instrument_id = str(fill["instrument_id"])
            lot_id = _digest({
                "instrument_id": instrument_id,
                "acquired_session": strategy_date.isoformat(),
                "quantity": int(fill["simulated_quantity"]),
                "execution_price": fill["execution_price"],
                "predecessor": state["previous_record_sha256"],
            })
            lots.append({
                "lot_id": lot_id,
                "instrument_id": instrument_id,
                "quantity": int(fill["simulated_quantity"]),
                "acquired_session": strategy_date.isoformat(),
                "sellable_from_session": next_session.isoformat(),
                "acquisition_fill_sha256": _digest(fill),
            })
    for instrument_id, quantity in positions.items():
        lot_quantity = sum(
            int(item["quantity"]) for item in lots
            if item["instrument_id"] == instrument_id
        )
        if lot_quantity != quantity:
            raise TechnicalShadowDailyError(
                f"position_lot_reconciliation_failed:{instrument_id}"
            )
    summary, ledger_fills = _ledger_transaction_accounting(fills)
    application = {
        "status": "APPLIED",
        "decision_date": plan["decision_date"],
        "execution_date": strategy_date.isoformat(),
        "opening_cash": _money_text(_decimal(state["cash"])),
        "opening_positions": before,
        "closing_cash_after_open_execution": _money_text(cash),
        "closing_positions_after_open_execution": dict(sorted(positions.items())),
        "fills": fills,
        "transaction_summary": summary,
        "ledger_fills": ledger_fills,
        "reason_codes": ["previous_immutable_plan_applied_at_real_open"],
    }
    return {
        "cash": cash, "positions": positions, "position_lots": lots,
        "fills": fills, "transaction_cost": transaction_cost,
    }, application


def _planned_actions(
    *, targets: Mapping[str, int], positions: Mapping[str, int],
    selected: Sequence[str], close_by_id: Mapping[str, Decimal],
    config: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    costs: list[dict[str, Any]] = []
    for instrument_id in sorted(set(positions) | set(targets)):
        current = int(positions.get(instrument_id, 0))
        target = int(targets.get(instrument_id, 0))
        delta = target - current
        reference = close_by_id.get(instrument_id)
        if delta == 0:
            if current:
                actions.append({
                    "action": "HOLD", "instrument_id": instrument_id,
                    "current_quantity": current, "target_quantity": target,
                    "quantity": 0,
                    "reference_price": _money_text(reference) if reference else None,
                    "maximum_buy_price": None,
                    "commission": "0.00", "stamp_duty": "0.00",
                    "transfer_fee": "0.00", "explicit_fee": "0.00",
                    "slippage_cost": "0.00", "total_transaction_cost": "0.00",
                    "reason_codes": ["incumbent_within_hold_band"],
                })
            continue
        side = "BUY" if delta > 0 else "SELL"
        quantity = abs(delta)
        if reference is None:
            actions.append({
                "action": f"{side}_CANCELLED", "instrument_id": instrument_id,
                "current_quantity": current, "target_quantity": target,
                "quantity": 0, "reference_price": None,
                "maximum_buy_price": None,
                "commission": "0.00", "stamp_duty": "0.00",
                "transfer_fee": "0.00", "explicit_fee": "0.00",
                "slippage_cost": "0.00", "total_transaction_cost": "0.00",
                "reason_codes": ["decision_reference_price_unavailable"],
            })
            continue
        estimate = _execution_cost(
            side=side, quantity=quantity, open_price=reference, config=config
        )
        action = {
            "action": side, "instrument_id": instrument_id,
            "current_quantity": current, "target_quantity": target,
            "quantity": quantity,
            "reference_price": _money_text(estimate["reference_price"]),
            "maximum_buy_price": (
                _money_text(estimate["execution_price"]) if side == "BUY" else None
            ),
            "notional_at_reference_price": _money_text(estimate["notional_at_reference_price"]),
            "notional_at_execution_price": _money_text(estimate["notional_at_execution_price"]),
            "commission": _money_text(estimate["commission"]),
            "stamp_duty": _money_text(estimate["stamp_duty"]),
            "transfer_fee": _money_text(estimate["transfer_fee"]),
            "explicit_fee": _money_text(estimate["explicit_fee"]),
            "slippage_cost": _money_text(estimate["slippage_cost"]),
            "total_transaction_cost": _money_text(estimate["total_transaction_cost"]),
            "reason_codes": ["manual_d_plus_1_open_plan_not_order"],
        }
        actions.append(action)
        costs.append(action)
    if not actions:
        actions.append({
            "action": "CASH", "instrument_id": None,
            "current_quantity": 0, "target_quantity": 0, "quantity": 0,
            "reference_price": None, "maximum_buy_price": None,
            "commission": "0.00", "stamp_duty": "0.00",
            "transfer_fee": "0.00", "explicit_fee": "0.00",
            "slippage_cost": "0.00", "total_transaction_cost": "0.00",
            "reason_codes": ["residual_cash_preserved"],
        })
    summary = {
        "commission": _money_text(sum((_decimal(row["commission"]) for row in costs), Decimal("0"))),
        "stamp_duty": _money_text(sum((_decimal(row["stamp_duty"]) for row in costs), Decimal("0"))),
        "transfer_fee": _money_text(sum((_decimal(row["transfer_fee"]) for row in costs), Decimal("0"))),
        "explicit_fee": _money_text(sum((_decimal(row["explicit_fee"]) for row in costs), Decimal("0"))),
        "slippage_cost": _money_text(sum((_decimal(row["slippage_cost"]) for row in costs), Decimal("0"))),
        "total_transaction_cost": _money_text(sum((_decimal(row["total_transaction_cost"]) for row in costs), Decimal("0"))),
    }
    return actions, summary


def _daily_report(
    *, strategy_date: date, execution_date: date,
    state: Mapping[str, Any], exposure: Mapping[str, Any],
    plan: Mapping[str, Any], application: Mapping[str, Any],
) -> str:
    lines = [
        f"# Technical Shadow 每日报告 {strategy_date.isoformat()}", "",
        f"- 决策日 / 人工计划执行日：`{strategy_date}` / `{execution_date}`",
        f"- 模式：`{MODE}`；市场状态：`{exposure['final_state']}`",
        f"- 当前现金 / NAV：`{state['cash']}` / `{state['nav']}`",
        f"- 当前持仓：`{state['positions']}`",
        f"- 目标总仓位：`{exposure['target_gross_exposure']:.2%}`；目标持仓：`{plan['target_positions']}`",
        f"- 前一计划应用：`{application['status']}`",
        "", "## Exposure 输入、阈值与命中条件", "",
    ]
    for key, item in exposure["inputs"].items():
        lines.append(
            f"- `{key}` = `{item['value']}`；used_by_policy=`{str(item['used_by_policy']).lower()}`；threshold=`{item.get('threshold')}`"
        )
    lines.extend([
        f"- 条件：`{exposure['condition_results']}`",
        f"- 命中规则 / 最终状态：`{exposure['matched_rule']}` / `{exposure['final_state']}`",
        "", "## BUY / SELL / HOLD / CASH 人工计划", "",
        "| 动作 | 标的 | 当前数 | 目标数 | 计划数 | 参考价 | 最大买入价 | 显式费用 | 滑点 | 总成本 | 原因 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ])
    for row in plan["actions"]:
        lines.append(
            "| {action} | {instrument} | {current} | {target} | {quantity} | {reference} | {maximum} | {explicit} | {slippage} | {total} | {reason} |".format(
                action=row["action"], instrument=row.get("instrument_id") or "CASH",
                current=row.get("current_quantity", 0), target=row.get("target_quantity", 0),
                quantity=row.get("quantity", 0), reference=row.get("reference_price") or "-",
                maximum=row.get("maximum_buy_price") or "-", explicit=row.get("explicit_fee", "0.00"),
                slippage=row.get("slippage_cost", "0.00"), total=row.get("total_transaction_cost", "0.00"),
                reason=",".join(row.get("reason_codes", [])),
            )
        )
    lines.extend([
        "", f"- 计划成本拆分：`{plan['cost_summary']}`",
        f"- no-trade 原因：`{plan['no_trade_reason_codes']}`",
        f"- 取消条件：`{plan['cancellation_conditions']}`",
        "- 该文件不是订单；不会连接券商或自动提交。",
        "- 当前股票池不是历史 PIT 中证800；本策略不具备 Paper、交易、真实资金或 LIVE 准入。",
        "",
    ])
    return "\n".join(lines)


def _payload_bytes(value: Any) -> bytes:
    if isinstance(value, str):
        return value.replace("\r\n", "\n").encode("utf-8")
    return _canonical_bytes(value)


def _publish_create_only(
    *, root: Path, payloads: Mapping[str, Any], manifest: Mapping[str, Any]
) -> tuple[str, bool]:
    expected = {name: _payload_bytes(value) for name, value in payloads.items()}
    expected["manifest.json"] = _payload_bytes(manifest)
    if root.exists():
        _verified_slot(root)
        existing_files = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*") if path.is_file()
        }
        if existing_files != set(expected):
            raise TechnicalShadowDailyError("immutable_conflict:file_set_changed")
        for relative, raw in expected.items():
            if (root / relative).read_bytes() != raw:
                raise TechnicalShadowDailyError(f"immutable_conflict:{relative}")
        return _file_sha256(root / "manifest.json"), True
    root.parent.mkdir(parents=True, exist_ok=True)
    try:
        root.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise TechnicalShadowDailyError("daily_slot_reservation_conflict") from exc
    try:
        for relative in sorted(payloads):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                stream.write(expected[relative])
        with (root / "manifest.json").open("xb") as stream:
            stream.write(expected["manifest.json"])
    except Exception:
        # A partial slot is intentionally retained as fail-closed evidence.
        raise
    return _file_sha256(root / "manifest.json"), False


def run_daily(
    *, config: Mapping[str, Any], captured: CapturedData,
    execution_evidence: NextSessionEvidence, output_root: Path,
    seed_path: Path = DEFAULT_SEED, allow_test_provider: bool = False,
) -> tuple[Path, dict[str, Any]]:
    validate_source_provenance(
        provider_id=captured.provider_id,
        provider_kind=captured.provider_kind,
        synthetic=captured.synthetic,
    )
    if not allow_test_provider and (
        captured.provider_id != "baostock"
        or captured.provider_kind != "real_provider"
        or captured.synthetic
    ):
        raise TechnicalShadowDailyError("real_baostock_provider_required")
    if len(captured.sessions) < ALPHA_LOOKBACK_SESSIONS + 1:
        raise TechnicalShadowDailyError("captured_sessions_insufficient")
    if len(set(captured.sessions)) != len(captured.sessions) or tuple(sorted(captured.sessions)) != captured.sessions:
        raise TechnicalShadowDailyError("captured_calendar_duplicate_or_unsorted")
    strategy_date = captured.sessions[-1]
    if execution_evidence.execution_date <= strategy_date:
        raise TechnicalShadowDailyError("execution_date_must_follow_strategy_date")
    prior_state, prior_plan, predecessor = _load_previous_context(
        strategy_date=strategy_date, output_root=output_root,
        seed_path=seed_path, config=config, previous_session=captured.sessions[-2],
    )
    prior_state_date = date.fromisoformat(str(prior_state["state_date"]))
    if (
        prior_state_date != strategy_date
        and prior_state_date != captured.sessions[-2]
    ):
        raise TechnicalShadowDailyError(
            "previous_controlled_date_not_previous_session"
        )
    if prior_plan is not None and predecessor.get("previous_plan_sha256") != _file_sha256(
        output_root / str(prior_plan["decision_date"]) / "next_session_plan.json"
    ):
        raise TechnicalShadowDailyError("previous_plan_hash_mismatch")
    if prior_plan is not None and prior_plan.get("safety") != DAILY_SAFETY:
        raise TechnicalShadowDailyError("previous_plan_safety_mismatch")

    instrument_ids = list(config["universe"]["instrument_ids"])
    stock_maps = {
        item: _strict_row_map(captured.stock_rows.get(item, ()), instrument_id=item)
        for item in instrument_ids
    }
    benchmark_map = _strict_row_map(
        captured.benchmark_rows, instrument_id=str(config["data"]["benchmark_id"])
    )
    sessions = tuple(day for day in captured.sessions if day <= strategy_date)[-121:]
    if len(sessions) != 121 or sessions[-1] != strategy_date:
        raise TechnicalShadowDailyError("daily_121_session_window_unavailable")
    for day in sessions:
        if day not in benchmark_map:
            raise TechnicalShadowDailyError("benchmark_missing_required_session")
        _validate_row_cutoff(benchmark_map[day], strategy_date)
    for item in instrument_ids:
        for row in captured.stock_rows.get(item, ()):
            if date.fromisoformat(str(row["trading_date"])) <= strategy_date:
                _validate_row_cutoff(row, strategy_date)

    account, application = _apply_previous_plan(
        state=prior_state, plan=prior_plan, strategy_date=strategy_date,
        next_session=execution_evidence.execution_date,
        stock_maps=stock_maps, config=config,
    )
    positions = account["positions"]
    cash = account["cash"]
    close_by_id = {
        item: _decimal(stock_maps[item][strategy_date]["close"])
        for item in positions
        if strategy_date in stock_maps[item]
        and stock_maps[item][strategy_date].get("close") is not None
    }
    if set(positions) != set(close_by_id):
        raise TechnicalShadowDailyError("held_position_close_unavailable")
    nav = _money(cash + sum(close_by_id[item] * quantity for item, quantity in positions.items()))
    peak_nav = max(_money(_decimal(prior_state["peak_nav"])), nav)
    drawdown = float(nav / peak_nav - Decimal("1"))

    stock_slices = {
        item: tuple(stock_maps[item][day] for day in sessions if day in stock_maps[item])
        for item in instrument_ids
    }
    benchmark_slice = tuple(benchmark_map[day] for day in sessions)
    ranking_rows = rank_technical_alpha_shadow(
        decision_date=strategy_date, sessions=sessions,
        instrument_ids=instrument_ids, stock_rows=stock_slices,
        benchmark_rows=benchmark_slice,
        winsor_lower_quantile=float(config["alpha"]["winsor_lower_quantile"]),
        winsor_upper_quantile=float(config["alpha"]["winsor_upper_quantile"]),
    )
    ranking = {
        "schema_version": "technical-shadow-daily-ranking.v1",
        "strategy_id": STRATEGY_ID, "mode": MODE,
        "strategy_date": strategy_date.isoformat(),
        "universe_basis": config["universe"]["basis"],
        "historical_pit_csi800": False, "rows": ranking_rows,
        "safety": DAILY_SAFETY,
    }
    ranking["ranking_payload_sha256"] = _digest(ranking)

    eligible_ids = {
        str(row["instrument_id"]) for row in ranking_rows if row["eligibility"]
    }
    exposure_core = compute_technical_shadow_exposure(
        benchmark_rows=benchmark_slice,
        eligible_stock_rows=[stock_slices[item] for item in instrument_ids if item in eligible_ids],
        current_nav=float(nav), peak_nav=float(peak_nav), policy=config["exposure"],
    )
    market_drawdown = _market_drawdown(benchmark_slice)
    condition_results = _exposure_conditions(exposure_core, config["exposure"])
    final_state = str(exposure_core["market_state"])
    thresholds = {
        "risk_off": config["exposure"]["risk_off"],
        "defensive": config["exposure"]["defensive"],
        "risk_on": config["exposure"]["risk_on"],
    }
    exposure = {
        "schema_version": "technical-shadow-daily-exposure.v1",
        "strategy_id": STRATEGY_ID, "mode": MODE,
        "strategy_date": strategy_date.isoformat(),
        "inputs": {
            "benchmark_trend": {"value": exposure_core["benchmark_trend"], "used_by_policy": True, "threshold": thresholds},
            "market_breadth": {"value": exposure_core["market_breadth"], "used_by_policy": True, "threshold": thresholds},
            "realized_volatility": {"value": exposure_core["realized_volatility"], "used_by_policy": True, "threshold": thresholds},
            "market_drawdown": {"value": market_drawdown, "used_by_policy": False, "threshold": None},
            "account_drawdown": {"value": exposure_core["account_drawdown"], "used_by_policy": True, "threshold": thresholds},
            "eligible_stock_count": {"value": len(eligible_ids), "used_by_policy": False, "threshold": None},
        },
        "thresholds": thresholds,
        "condition_results": condition_results,
        "matched_rule": f"{final_state.lower()}_rule" if not exposure_core["data_fail_closed"] else "data_fail_closed",
        "previous_state": prior_state["exposure_state"],
        "candidate_state": final_state,
        "pending_state": None,
        "hysteresis_count": 0,
        "final_state": final_state,
        "target_gross_exposure": float(exposure_core["target_gross_exposure"]),
        "data_fail_closed": bool(exposure_core["data_fail_closed"]),
        "reason_codes": list(exposure_core["reason_codes"]),
        "safety": DAILY_SAFETY,
    }
    exposure["exposure_payload_sha256"] = _digest(exposure)

    decision_close_by_id = {
        item: _decimal(stock_maps[item][strategy_date]["close"])
        for item in instrument_ids
        if strategy_date in stock_maps[item]
        and stock_maps[item][strategy_date].get("close") is not None
    }
    targets, selected = _plan_targets(
        ranking=ranking_rows, positions=positions, nav=nav,
        target_exposure=float(exposure["target_gross_exposure"]),
        max_positions=int(config["portfolio"]["max_positions"]),
        max_weight=_decimal(config["portfolio"]["max_position_weight"]),
        lot_size=int(config["portfolio"]["lot_size"]),
        close_by_id=decision_close_by_id,
    )
    actions, cost_summary = _planned_actions(
        targets=targets, positions=positions, selected=selected,
        close_by_id=decision_close_by_id, config=config,
    )
    no_trade_reasons = _cash_reason_codes(
        ranking=ranking_rows, positions=positions, selected=selected,
        target_exposure=float(exposure["target_gross_exposure"]),
    )
    has_planned_trade = any(row["action"] in {"BUY", "SELL"} for row in actions)
    if execution_evidence.execution_window_status == "MISSED" and has_planned_trade:
        cancelled_actions: list[dict[str, Any]] = []
        for row in actions:
            changed = dict(row)
            if row["action"] in {"BUY", "SELL"}:
                changed["requested_quantity_before_cancellation"] = row["quantity"]
                changed["action"] = f"{row['action']}_CANCELLED"
                changed["quantity"] = 0
                changed["commission"] = "0.00"
                changed["stamp_duty"] = "0.00"
                changed["transfer_fee"] = "0.00"
                changed["explicit_fee"] = "0.00"
                changed["slippage_cost"] = "0.00"
                changed["total_transaction_cost"] = "0.00"
                changed["reason_codes"] = list(row["reason_codes"]) + [
                    "missed_d_plus_1_open_cutoff_no_retrospective_plan"
                ]
            cancelled_actions.append(changed)
        actions = cancelled_actions
        cost_summary = {
            "commission": "0.00", "stamp_duty": "0.00",
            "transfer_fee": "0.00", "explicit_fee": "0.00",
            "slippage_cost": "0.00", "total_transaction_cost": "0.00",
        }
        no_trade_reasons = list(no_trade_reasons) + [
            "MISSED_D_PLUS_1_OPEN_CUTOFF"
        ]

    sellable = {
        item: sum(
            int(lot["quantity"]) for lot in account["position_lots"]
            if lot["instrument_id"] == item
            and date.fromisoformat(str(lot["sellable_from_session"])) <= strategy_date
        )
        for item in positions
    }
    cumulative_explicit = _money(
        _decimal(prior_state.get("cumulative_explicit_fee", "0"))
        + _decimal(application.get("transaction_summary", {}).get("explicit_fee", "0"))
    )
    cumulative_slippage = _money(
        _decimal(prior_state.get("cumulative_slippage_cost", "0"))
        + _decimal(application.get("transaction_summary", {}).get("slippage_cost", "0"))
    )
    state_base = {
        "schema_version": "technical-shadow-daily-account-state.v1",
        "strategy_id": STRATEGY_ID, "shadow_account_id": SHADOW_ACCOUNT_ID,
        "mode": MODE, "state_date": strategy_date.isoformat(),
        "previous_trading_date": (
            prior_state["previous_trading_date"]
            if prior_plan is None and prior_state["state_date"] == strategy_date.isoformat()
            else prior_state["state_date"]
        ),
        "previous_record_sha256": prior_state["previous_record_sha256"] if prior_plan is None else prior_state["record_sha256"],
        "cash": _money_text(cash), "positions": dict(sorted(positions.items())),
        "position_lots": sorted(account["position_lots"], key=lambda item: item["lot_id"]),
        "sellable_quantities": dict(sorted((key, value) for key, value in sellable.items() if value)),
        "nav": _money_text(nav), "peak_nav": _money_text(peak_nav),
        "drawdown": drawdown, "exposure_state": final_state,
        "pending_state": None, "hysteresis_count": 0,
        "cumulative_explicit_fee": _money_text(cumulative_explicit),
        "cumulative_slippage_cost": _money_text(cumulative_slippage),
        "cumulative_transaction_cost": _money_text(cumulative_explicit + cumulative_slippage),
        "prior_plan_application_sha256": _digest(application),
        "safety": DAILY_SAFETY,
    }
    state = dict(state_base)
    state["record_sha256"] = _digest(state_base)

    plan = {
        "schema_version": "technical-shadow-next-session-plan.v1",
        "strategy_id": STRATEGY_ID, "shadow_account_id": SHADOW_ACCOUNT_ID,
        "mode": MODE, "plan_type": "manual_shadow_plan_not_order",
        "plan_status": (
            "CANCELLED_MISSED_D_PLUS_1_OPEN_CUTOFF"
            if execution_evidence.execution_window_status == "MISSED" and has_planned_trade
            else "NO_ACTION_CASH" if not has_planned_trade else "READY"
        ),
        "execution_window_status": execution_evidence.execution_window_status,
        "decision_date": strategy_date.isoformat(),
        "execution_date": execution_evidence.execution_date.isoformat(),
        "valid_only_for_execution_date": execution_evidence.execution_date.isoformat(),
        "based_on_account_record_sha256": state["record_sha256"],
        "ranking_payload_sha256": ranking["ranking_payload_sha256"],
        "exposure_payload_sha256": exposure["exposure_payload_sha256"],
        "target_gross_exposure": exposure["target_gross_exposure"],
        "selected_instruments": selected,
        "target_positions": dict(sorted(targets.items())),
        "actions": actions, "cost_summary": cost_summary,
        "no_trade_reason_codes": no_trade_reasons,
        "cancellation_conditions": list(CANCELLATION_CONDITIONS),
        "lot_size": int(config["portfolio"]["lot_size"]),
        "max_positions": int(config["portfolio"]["max_positions"]),
        "max_position_weight": config["portfolio"]["max_position_weight"],
        "automatic_order_submission": False,
        "safety": DAILY_SAFETY,
    }
    plan["plan_payload_sha256"] = _digest(plan)

    action_counts = {
        action: sum(row["action"] == action for row in actions)
        for action in ("BUY", "SELL", "HOLD", "CASH")
    }
    portfolio_decision = {
        "schema_version": "technical-shadow-daily-portfolio-decision.v1",
        "strategy_id": STRATEGY_ID, "mode": MODE,
        "strategy_date": strategy_date.isoformat(),
        "execution_date": execution_evidence.execution_date.isoformat(),
        "current_cash": state["cash"], "current_positions": state["positions"],
        "current_nav": state["nav"], "target_positions": plan["target_positions"],
        "target_gross_exposure": exposure["target_gross_exposure"],
        "action_counts": action_counts, "actions": actions,
        "prior_plan_application": application,
        "no_trade_reason_codes": no_trade_reasons,
        "automatic_order_submission": False, "safety": DAILY_SAFETY,
    }
    portfolio_decision["decision_payload_sha256"] = _digest(portfolio_decision)

    data_receipt = _stable_data_receipt(
        captured=captured, strategy_date=strategy_date,
        execution_evidence=execution_evidence, config=config,
    )
    report = _daily_report(
        strategy_date=strategy_date, execution_date=execution_evidence.execution_date,
        state=state, exposure=exposure, plan=plan, application=application,
    )
    payloads: dict[str, Any] = {
        "data_receipt.json": data_receipt,
        "ranking.json": ranking,
        "exposure.json": exposure,
        "portfolio_decision.json": portfolio_decision,
        "next_session_plan.json": plan,
        "state.json": state,
        "previous_state.json": prior_state,
        "prior_plan_application.json": application,
        "daily_report.md": report,
    }
    artifact_hashes = {
        name: sha256(_payload_bytes(value)).hexdigest()
        for name, value in sorted(payloads.items())
    }
    manifest_base = {
        "schema_version": "technical-shadow-daily-manifest.v1",
        "strategy_id": STRATEGY_ID, "shadow_account_id": SHADOW_ACCOUNT_ID,
        "mode": MODE, "strategy_date": strategy_date.isoformat(),
        "execution_date": execution_evidence.execution_date.isoformat(),
        "config_sha256": _digest(config),
        "predecessor": predecessor,
        "account_record_sha256": state["record_sha256"],
        "artifacts": artifact_hashes,
        "provider": data_receipt["provider"],
        "historical_pit_csi800": False,
        "automatic_order_submission": False, "safety": DAILY_SAFETY,
    }
    manifest = dict(manifest_base)
    manifest["manifest_payload_sha256"] = _digest(manifest_base)
    root = output_root / strategy_date.isoformat()
    manifest_sha, idempotent = _publish_create_only(
        root=root, payloads=payloads, manifest=manifest,
    )
    result = {
        "status": "idempotent_existing" if idempotent else "created",
        "strategy_date": strategy_date.isoformat(),
        "execution_date": execution_evidence.execution_date.isoformat(),
        "exposure_state": final_state,
        "action_counts": action_counts,
        "current_cash": state["cash"], "current_positions": state["positions"],
        "current_nav": state["nav"], "plan_cost_summary": cost_summary,
        "output_directory": str(root.resolve()),
        "manifest_sha256": manifest_sha,
        "idempotent": idempotent,
        "automatic_order_submission": False,
    }
    return root, result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state-seed", type=Path, default=DEFAULT_SEED)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = _load_config(args.config)
    now = datetime.now(CHINA_TZ)
    source = BaoStockTechnicalShadowSource()
    captured = source.capture(
        instrument_ids=config["universe"]["instrument_ids"],
        benchmark_id=config["data"]["benchmark_id"],
        recent_completed_sessions=1,
        lookback_days=int(config["data"]["calendar_lookback_days"]),
        now=now,
        completed_through=(
            now.astimezone(CHINA_TZ).date()
            if now.astimezone(CHINA_TZ).time() >= DECISION_CUTOFF
            else now.astimezone(CHINA_TZ).date() - timedelta(days=1)
        ),
    )
    captured = _latest_data_complete_capture(captured)
    evidence = query_next_baostock_session(after_date=captured.sessions[-1])
    local_now = now.astimezone(CHINA_TZ)
    window_status = (
        "MISSED"
        if evidence.execution_date < local_now.date()
        or (
            evidence.execution_date == local_now.date()
            and local_now.time() >= EXECUTION_OPEN
        )
        else "OPEN"
    )
    evidence = NextSessionEvidence(
        execution_date=evidence.execution_date,
        receipt=evidence.receipt,
        execution_window_status=window_status,
    )
    _, result = run_daily(
        config=config, captured=captured, execution_evidence=evidence,
        output_root=args.output_root, seed_path=args.state_seed,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "NextSessionEvidence", "TechnicalShadowDailyError", "run_daily",
    "query_next_baostock_session",
]
