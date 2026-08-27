"""Run one isolated real-data Technical Shadow retrospective replay.

This module never writes the forward daily state root and never submits orders.
Decision inputs end at 2026-08-26; any 2026-08-27 observation is isolated in
the retrospective execution artifact.
"""

from __future__ import annotations

import argparse
import importlib
import json
import uuid
from datetime import date, datetime, time
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

from operations.run_technical_shadow_daily import (
    DEFAULT_STATE_ROOT,
    _apply_previous_plan,
    _exposure_conditions,
    _market_drawdown,
    _planned_actions,
    _strict_row_map,
    _validate_row_cutoff,
    _verified_state_chain,
)
from operations.run_technical_shadow_mvp import (
    ALPHA_LOOKBACK_SESSIONS,
    CHINA_TZ,
    DAILY_FIELDS,
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
    _optional_number,
    _plan_targets,
    validate_source_provenance,
)
from research.market_data.providers.baostock import BaoStockProvider, to_baostock_code
from research.strategy_workspace.technical_alpha_shadow_v1 import (
    rank_technical_alpha_shadow,
)
from research.strategy_workspace.technical_exposure_shadow_v1 import (
    compute_technical_shadow_exposure,
)


DEFAULT_CONFIG = Path("configs/a_share_technical_shadow_mvp.v1.json")
DEFAULT_OUTPUT_ROOT = Path("data/tmp/technical-shadow-retrospective")
STRATEGY_DATE = date(2026, 8, 26)
EXECUTION_DATE = date(2026, 8, 27)
RUN_MODE = "retrospective_replay"
RETROSPECTIVE_SAFETY = {
    "strategy_signal": False,
    "alpha_evidence": False,
    "forward_evidence": False,
    "trade_recommendation": False,
    "paper_eligibility": False,
    "trade_eligibility": False,
    "real_money_list_allowed": False,
    "automatic_order_submission": False,
    "state_mutation_allowed": False,
    "live_supported": False,
}
EXPECTED_ARTIFACTS = {
    "data_receipt.json",
    "ranking.json",
    "exposure.json",
    "portfolio_decision.json",
    "retrospective_execution.json",
    "retrospective_report.md",
}


class TechnicalShadowRetrospectiveError(TechnicalShadowRunError):
    """Fail-closed retrospective replay error."""


def _file_sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _tree_snapshot(root: Path) -> dict[str, str]:
    if not root.is_dir():
        raise TechnicalShadowRetrospectiveError("formal_state_root_missing")
    return {
        path.relative_to(root).as_posix(): _file_sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _base_fields(*, run_id: str, generated_at: datetime) -> dict[str, Any]:
    return {
        "strategy_id": STRATEGY_ID,
        "run_id": run_id,
        "run_mode": RUN_MODE,
        "strategy_date": STRATEGY_DATE.isoformat(),
        "execution_date": EXECUTION_DATE.isoformat(),
        "generated_at": generated_at.isoformat(),
        "generated_late": True,
        "execution_window_status": "MISSED",
        **RETROSPECTIVE_SAFETY,
        "safety": RETROSPECTIVE_SAFETY,
    }


def _load_verified_source_state(
    *, state_root: Path, config: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str, str]:
    slots = _verified_state_chain(
        state_root, expected_config_sha256=_digest(config)
    )
    match = [item for item in slots if item[0] == date(2026, 8, 25)]
    if len(match) != 1:
        raise TechnicalShadowRetrospectiveError("controlled_2026_08_25_state_missing")
    _day, slot, manifest, manifest_sha = match[0]
    state = json.loads((slot / "state.json").read_text(encoding="utf-8"))
    plan = json.loads(
        (slot / "next_session_plan.json").read_text(encoding="utf-8")
    )
    if (
        state.get("state_date") != "2026-08-25"
        or state.get("cash") != "10000.00"
        or state.get("positions") != {}
        or state.get("sellable_quantities") != {}
        or plan.get("plan_status") != "NO_ACTION_CASH"
        or plan.get("target_positions") != {}
        or plan.get("execution_date") != STRATEGY_DATE.isoformat()
    ):
        raise TechnicalShadowRetrospectiveError(
            "controlled_source_state_not_expected_flat_cash"
        )
    return state, plan, manifest, manifest_sha, str(slot.resolve())


def _validate_decision_capture(
    *, captured: CapturedData, config: Mapping[str, Any],
    allow_test_provider: bool = False,
) -> tuple[
    tuple[date, ...],
    dict[str, dict[date, Mapping[str, Any]]],
    dict[date, Mapping[str, Any]],
]:
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
        raise TechnicalShadowRetrospectiveError("real_baostock_provider_required")
    if tuple(sorted(captured.sessions)) != captured.sessions:
        raise TechnicalShadowRetrospectiveError("captured_sessions_unsorted")
    sessions = tuple(day for day in captured.sessions if day <= STRATEGY_DATE)[-121:]
    if len(sessions) != ALPHA_LOOKBACK_SESSIONS + 1 or sessions[-1] != STRATEGY_DATE:
        raise TechnicalShadowRetrospectiveError(
            "decision_requires_121_sessions_ending_2026_08_26"
        )
    instrument_ids = list(config["universe"]["instrument_ids"])
    if set(captured.stock_rows) != set(instrument_ids):
        raise TechnicalShadowRetrospectiveError("frozen_universe_capture_mismatch")
    for instrument_id, rows in captured.stock_rows.items():
        if any(
            date.fromisoformat(str(row["trading_date"])) > STRATEGY_DATE
            for row in rows
        ):
            raise TechnicalShadowRetrospectiveError(
                f"d_plus_one_stock_data_in_decision_capture:{instrument_id}"
            )
    if any(
        date.fromisoformat(str(row["trading_date"])) > STRATEGY_DATE
        for row in captured.benchmark_rows
    ):
        raise TechnicalShadowRetrospectiveError(
            "d_plus_one_benchmark_data_in_decision_capture"
        )
    stock_maps = {
        item: _strict_row_map(captured.stock_rows[item], instrument_id=item)
        for item in instrument_ids
    }
    benchmark_map = _strict_row_map(
        captured.benchmark_rows,
        instrument_id=str(config["data"]["benchmark_id"]),
    )
    for day in sessions:
        if day not in benchmark_map:
            raise TechnicalShadowRetrospectiveError(
                "benchmark_missing_decision_session"
            )
        _validate_row_cutoff(benchmark_map[day], STRATEGY_DATE)
    for item in instrument_ids:
        for day, row in stock_maps[item].items():
            if day in sessions:
                _validate_row_cutoff(row, STRATEGY_DATE)
    return sessions, stock_maps, benchmark_map


def _decision_data_receipt(
    *, run_id: str, generated_at: datetime, captured: CapturedData,
    config: Mapping[str, Any], sessions: Sequence[date],
) -> dict[str, Any]:
    session_set = set(sessions)
    source_receipts = {
        key: {
            field: receipt.get(field)
            for field in (
                "receipt_type", "provider_id", "provider_kind",
                "adapter_version", "synthetic", "instrument_id",
                "is_benchmark", "request", "record_count",
                "raw_content_sha256", "normalized_content_sha256",
            )
            if field in receipt
        }
        for key, receipt in sorted(captured.receipts.items())
    }
    payload = {
        **_base_fields(run_id=run_id, generated_at=generated_at),
        "schema_version": "technical-shadow-retrospective-data-receipt.v1",
        "config_sha256": _digest(config),
        "provider": {
            "provider_id": captured.provider_id,
            "provider_kind": captured.provider_kind,
            "adapter_version": captured.adapter_version,
            "synthetic": captured.synthetic,
        },
        "decision_input_boundary": {
            "maximum_trading_date": STRATEGY_DATE.isoformat(),
            "maximum_available_at": datetime.combine(
                STRATEGY_DATE, time(15, 30), CHINA_TZ
            ).isoformat(),
            "d_plus_one_data_included": False,
        },
        "sessions": [day.isoformat() for day in sessions],
        "benchmark_records": [
            dict(row) for row in captured.benchmark_rows
            if date.fromisoformat(str(row["trading_date"])) in session_set
        ],
        "stock_records": {
            item: [
                dict(row) for row in captured.stock_rows[item]
                if date.fromisoformat(str(row["trading_date"])) in session_set
            ]
            for item in config["universe"]["instrument_ids"]
        },
        "source_receipt_summaries": source_receipts,
        "historical_pit_csi800": False,
    }
    payload["decision_input_sha256"] = _digest(payload)
    return payload


def build_retrospective_decision(
    *, run_id: str, generated_at: datetime, config: Mapping[str, Any],
    captured: CapturedData, source_state: Mapping[str, Any],
    source_plan: Mapping[str, Any], source_manifest_sha256: str,
    allow_test_provider: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Build and hash D-close artifacts before any D+1 observation is loaded."""

    sessions, stock_maps, benchmark_map = _validate_decision_capture(
        captured=captured, config=config,
        allow_test_provider=allow_test_provider,
    )
    data_receipt = _decision_data_receipt(
        run_id=run_id, generated_at=generated_at, captured=captured,
        config=config, sessions=sessions,
    )
    instrument_ids = list(config["universe"]["instrument_ids"])
    account, prior_application = _apply_previous_plan(
        state=source_state,
        plan=source_plan,
        strategy_date=STRATEGY_DATE,
        next_session=EXECUTION_DATE,
        stock_maps=stock_maps,
        config=config,
    )
    if prior_application.get("fills"):
        raise TechnicalShadowRetrospectiveError(
            "unexpected_source_plan_trade_on_2026_08_26"
        )
    positions = dict(account["positions"])
    cash = _money(_decimal(account["cash"]))
    held_closes = {
        item: _decimal(stock_maps[item][STRATEGY_DATE]["close"])
        for item in positions
        if STRATEGY_DATE in stock_maps[item]
        and stock_maps[item][STRATEGY_DATE].get("close") is not None
    }
    if set(held_closes) != set(positions):
        raise TechnicalShadowRetrospectiveError("held_position_close_unavailable")
    nav = _money(
        cash + sum(
            held_closes[item] * quantity
            for item, quantity in positions.items()
        )
    )
    peak_nav = max(_money(_decimal(source_state["peak_nav"])), nav)
    account_drawdown = float(nav / peak_nav - Decimal("1"))
    stock_slices = {
        item: tuple(
            stock_maps[item][day] for day in sessions
            if day in stock_maps[item]
        )
        for item in instrument_ids
    }
    benchmark_slice = tuple(benchmark_map[day] for day in sessions)
    ranking_rows = rank_technical_alpha_shadow(
        decision_date=STRATEGY_DATE,
        sessions=sessions,
        instrument_ids=instrument_ids,
        stock_rows=stock_slices,
        benchmark_rows=benchmark_slice,
        winsor_lower_quantile=float(config["alpha"]["winsor_lower_quantile"]),
        winsor_upper_quantile=float(config["alpha"]["winsor_upper_quantile"]),
    )
    if len(ranking_rows) != 60:
        raise TechnicalShadowRetrospectiveError("ranking_must_contain_60_rows")
    top_10 = [dict(row) for row in ranking_rows[:10]]
    ranking = {
        **_base_fields(run_id=run_id, generated_at=generated_at),
        "schema_version": "technical-shadow-retrospective-ranking.v1",
        "decision_input_sha256": data_receipt["decision_input_sha256"],
        "universe_basis": config["universe"]["basis"],
        "historical_pit_csi800": False,
        "rows": ranking_rows,
        "top_10": top_10,
        "top_10_instrument_ids": [row["instrument_id"] for row in top_10],
    }
    ranking["ranking_payload_sha256"] = _digest(ranking)

    eligible_ids = {
        str(row["instrument_id"])
        for row in ranking_rows
        if row["eligibility"]
    }
    exposure_core = compute_technical_shadow_exposure(
        benchmark_rows=benchmark_slice,
        eligible_stock_rows=[
            stock_slices[item]
            for item in instrument_ids
            if item in eligible_ids
        ],
        current_nav=float(nav),
        peak_nav=float(peak_nav),
        policy=config["exposure"],
    )
    thresholds = {
        "risk_off": config["exposure"]["risk_off"],
        "defensive": config["exposure"]["defensive"],
        "risk_on": config["exposure"]["risk_on"],
    }
    final_state = str(exposure_core["market_state"])
    exposure = {
        **_base_fields(run_id=run_id, generated_at=generated_at),
        "schema_version": "technical-shadow-retrospective-exposure.v1",
        "decision_input_sha256": data_receipt["decision_input_sha256"],
        "ranking_payload_sha256": ranking["ranking_payload_sha256"],
        "inputs": {
            "benchmark_trend": {
                "value": exposure_core["benchmark_trend"],
                "used_by_policy": True,
                "threshold": thresholds,
            },
            "market_breadth": {
                "value": exposure_core["market_breadth"],
                "used_by_policy": True,
                "threshold": thresholds,
            },
            "realized_volatility": {
                "value": exposure_core["realized_volatility"],
                "used_by_policy": True,
                "threshold": thresholds,
            },
            "market_drawdown": {
                "value": _market_drawdown(benchmark_slice),
                "used_by_policy": False,
                "threshold": None,
            },
            "account_drawdown": {
                "value": exposure_core["account_drawdown"],
                "used_by_policy": True,
                "threshold": thresholds,
            },
            "eligible_stock_count": {
                "value": len(eligible_ids),
                "used_by_policy": False,
                "threshold": None,
            },
        },
        "thresholds": thresholds,
        "condition_results": _exposure_conditions(
            exposure_core, config["exposure"]
        ),
        "matched_rule": (
            "data_fail_closed"
            if exposure_core["data_fail_closed"]
            else f"{final_state.lower()}_rule"
        ),
        "previous_state": source_state["exposure_state"],
        "candidate_state": final_state,
        "pending_state": None,
        "hysteresis_count": 0,
        "final_state": final_state,
        "target_gross_exposure": float(
            exposure_core["target_gross_exposure"]
        ),
        "data_fail_closed": bool(exposure_core["data_fail_closed"]),
        "reason_codes": list(exposure_core["reason_codes"]),
    }
    exposure["exposure_payload_sha256"] = _digest(exposure)

    decision_closes = {
        item: _decimal(stock_maps[item][STRATEGY_DATE]["close"])
        for item in instrument_ids
        if STRATEGY_DATE in stock_maps[item]
        and stock_maps[item][STRATEGY_DATE].get("close") is not None
    }
    targets, selected = _plan_targets(
        ranking=ranking_rows,
        positions=positions,
        nav=nav,
        target_exposure=float(exposure["target_gross_exposure"]),
        max_positions=int(config["portfolio"]["max_positions"]),
        max_weight=_decimal(config["portfolio"]["max_position_weight"]),
        lot_size=int(config["portfolio"]["lot_size"]),
        close_by_id=decision_closes,
    )
    actions, cost_summary = _planned_actions(
        targets=targets,
        positions=positions,
        selected=selected,
        close_by_id=decision_closes,
        config=config,
    )
    no_trade_reasons = _cash_reason_codes(
        ranking=ranking_rows,
        positions=positions,
        selected=selected,
        target_exposure=float(exposure["target_gross_exposure"]),
    )
    if (
        selected
        and not any(quantity > 0 for quantity in targets.values())
        and not no_trade_reasons
    ):
        no_trade_reasons = ["WHOLE_LOT_TARGET_ROUNDED_TO_ZERO"]
    action_counts = {
        action: sum(row["action"] == action for row in actions)
        for action in ("BUY", "SELL", "HOLD", "CASH")
    }
    target_weights = {
        item: float(
            _decimal(quantity) * decision_closes[item] / nav
        )
        for item, quantity in targets.items()
        if quantity > 0 and item in decision_closes and nav > 0
    }
    alpha_candidates = [
        str(row["instrument_id"])
        for row in ranking_rows
        if row["entry_eligible"]
    ]
    manual_plan = {
        "plan_type": "retrospective_manual_plan_not_order",
        "plan_status": "RETROSPECTIVE_ONLY_MISSED",
        "decision_date": STRATEGY_DATE.isoformat(),
        "execution_date": EXECUTION_DATE.isoformat(),
        "execution_window_status": "MISSED",
        "target_positions": dict(sorted(targets.items())),
        "selected_instruments": list(selected),
        "actions": actions,
        "cost_summary": cost_summary,
        "no_trade_reason_codes": no_trade_reasons,
        "automatic_order_submission": False,
    }
    manual_plan["plan_payload_sha256"] = _digest(manual_plan)
    decision = {
        **_base_fields(run_id=run_id, generated_at=generated_at),
        "schema_version": "technical-shadow-retrospective-portfolio-decision.v1",
        "decision_input_sha256": data_receipt["decision_input_sha256"],
        "ranking_payload_sha256": ranking["ranking_payload_sha256"],
        "exposure_payload_sha256": exposure["exposure_payload_sha256"],
        "source_state_record_sha256": source_state["record_sha256"],
        "source_previous_record_sha256": source_state["previous_record_sha256"],
        "source_persistent_manifest_sha256": source_manifest_sha256,
        "account_after_prior_plan": {
            "cash": _money_text(cash),
            "positions": dict(sorted(positions.items())),
            "sellable_quantities": source_state["sellable_quantities"],
            "nav": _money_text(nav),
            "peak_nav": _money_text(peak_nav),
            "drawdown": account_drawdown,
            "prior_plan_application": prior_application,
        },
        "top_10_candidates": top_10,
        "alpha_candidate_exists": bool(alpha_candidates),
        "alpha_candidate_instruments": alpha_candidates,
        "exposure_state": final_state,
        "target_gross_exposure": exposure["target_gross_exposure"],
        "target_positions": manual_plan["target_positions"],
        "target_weights": target_weights,
        "action_counts": action_counts,
        "actions": actions,
        "no_trade_reason_codes": no_trade_reasons,
        "d_plus_one_manual_plan": manual_plan,
        "actual_order_submitted": False,
        "strategy_state_mutated": False,
    }
    decision["decision_payload_sha256"] = _digest(decision)
    context = {
        "positions": positions,
        "cash": cash,
        "nav": nav,
        "targets": targets,
        "selected": selected,
        "actions": actions,
        "stock_maps": stock_maps,
        "position_lots": list(account["position_lots"]),
        "sellable_quantities": dict(source_state["sellable_quantities"]),
    }
    return data_receipt, ranking, exposure, decision, context


def _capture_execution_open_rows(
    *, instrument_ids: Sequence[str], observed_at: datetime,
) -> tuple[dict[str, Mapping[str, Any]], dict[str, Any]]:
    """Read only the D+1 open evidence needed by already-frozen actions."""

    sdk = importlib.import_module("baostock")
    login = sdk.login()
    BaoStockProvider._check_result(login, "login")
    rows_by_id: dict[str, Mapping[str, Any]] = {}
    receipts: dict[str, Any] = {}
    try:
        for instrument_id in sorted(set(instrument_ids)):
            result = sdk.query_history_k_data_plus(
                to_baostock_code(instrument_id),
                ",".join(DAILY_FIELDS),
                start_date=EXECUTION_DATE.isoformat(),
                end_date=EXECUTION_DATE.isoformat(),
                frequency="d",
                adjustflag="3",
            )
            fields, raw_rows = BaoStockTechnicalShadowSource._rows(
                result, f"query_history_k_data_plus:{instrument_id}"
            )
            if tuple(fields) != DAILY_FIELDS:
                raise TechnicalShadowRetrospectiveError(
                    f"baostock_execution_open_contract_changed:{instrument_id}"
                )
            matching = [
                dict(zip(fields, values, strict=True))
                for values in raw_rows
                if values and values[0] == EXECUTION_DATE.isoformat()
            ]
            if len(matching) > 1:
                raise TechnicalShadowRetrospectiveError(
                    f"duplicate_execution_open_row:{instrument_id}"
                )
            normalized: Mapping[str, Any] | None = None
            if matching:
                raw = matching[0]
                normalized = {
                    "instrument_id": instrument_id,
                    "trading_date": EXECUTION_DATE.isoformat(),
                    "open": _optional_number(raw["open"]),
                    "trading_status": (
                        "traded" if raw["tradestatus"] == "1" else "suspended"
                    ),
                    "is_st": raw["isST"] == "1",
                    "open_observed_at": observed_at.isoformat(),
                    "source": "baostock",
                }
                rows_by_id[instrument_id] = normalized
            receipts[instrument_id] = {
                "receipt_type": "baostock_retrospective_execution_open_capture",
                "provider_id": "baostock",
                "provider_kind": "real_provider",
                "adapter_version": "baostock-technical-shadow-adapter-v1",
                "synthetic": False,
                "observed_at": observed_at.isoformat(),
                "instrument_id": instrument_id,
                "request": {
                    "start_date": EXECUTION_DATE.isoformat(),
                    "end_date": EXECUTION_DATE.isoformat(),
                    "frequency": "d",
                    "adjustflag": "3",
                    "fields": list(DAILY_FIELDS),
                },
                "record_count": len(matching),
                "raw_content_sha256": _digest(
                    {"fields": fields, "rows": raw_rows}
                ),
                "normalized_open_record": normalized,
            }
    finally:
        try:
            sdk.logout()
        except Exception:
            pass
    return rows_by_id, receipts


def build_retrospective_execution(
    *, run_id: str, generated_at: datetime, config: Mapping[str, Any],
    decision: Mapping[str, Any], context: Mapping[str, Any],
    execution_capture: Any = _capture_execution_open_rows,
) -> dict[str, Any]:
    """Simulate D+1 open only after the D decision payload is immutable."""

    planned_trade_ids = sorted({
        str(row["instrument_id"])
        for row in context["actions"]
        if row.get("action") in {"BUY", "SELL"}
        and row.get("instrument_id")
    })
    zero_summary, _ = _ledger_transaction_accounting([])
    close_cutoff = datetime.combine(EXECUTION_DATE, time(15, 30), CHINA_TZ)
    close_status = (
        "PENDING" if generated_at < close_cutoff else "NOT_REQUESTED_ISOLATED"
    )
    if not planned_trade_ids:
        execution = {
            **_base_fields(run_id=run_id, generated_at=generated_at),
            "schema_version": "technical-shadow-retrospective-execution.v1",
            "decision_payload_sha256": decision["decision_payload_sha256"],
            "retrospective_execution": True,
            "open_observation_status": "NOT_REQUIRED_NO_ACTION",
            "open_evidence": {},
            "simulated_fills": [],
            "execution_result": "NO_ACTION",
            "transaction_summary": zero_summary,
            "ending_cash_after_open": _money_text(context["cash"]),
            "ending_positions_after_open": dict(sorted(context["positions"].items())),
            "close_valuation_status": close_status,
            "close_valuation": None,
            "actual_order_submitted": False,
            "strategy_state_mutated": False,
        }
        execution["execution_payload_sha256"] = _digest(execution)
        return execution

    execution_rows, open_receipts = execution_capture(
        instrument_ids=planned_trade_ids, observed_at=generated_at
    )
    positions = {
        str(key): int(value) for key, value in context["positions"].items()
    }
    opening_positions = dict(positions)
    opening_cash = _money(_decimal(context["cash"]))
    effective_targets = {
        str(key): int(value) for key, value in context["targets"].items()
    }
    cancellations: list[dict[str, Any]] = []

    sellable: dict[str, int] = {}
    for instrument_id in positions:
        lot_quantity = sum(
            int(lot["quantity"])
            for lot in context.get("position_lots", [])
            if lot.get("instrument_id") == instrument_id
            and date.fromisoformat(str(lot["sellable_from_session"]))
            <= EXECUTION_DATE
        )
        sellable[instrument_id] = max(
            lot_quantity,
            int(context.get("sellable_quantities", {}).get(instrument_id, 0)),
        )
    for instrument_id, current in positions.items():
        requested_target = effective_targets.get(instrument_id, 0)
        requested_sell = max(current - requested_target, 0)
        allowed_sell = min(requested_sell, sellable.get(instrument_id, 0))
        if allowed_sell < requested_sell:
            effective_targets[instrument_id] = current - allowed_sell
            cancellations.append({
                "action": "SELL_CANCELLED",
                "instrument_id": instrument_id,
                "target_quantity": requested_target,
                "simulated_quantity": 0,
                "reason_codes": ["t_plus_one_sell_quantity_unavailable"],
            })

    maximum_buy_prices = {
        str(row["instrument_id"]): _decimal(row["maximum_buy_price"])
        for row in context["actions"]
        if row.get("action") == "BUY"
        and row.get("instrument_id")
        and row.get("maximum_buy_price") is not None
    }
    for instrument_id, maximum in maximum_buy_prices.items():
        current = positions.get(instrument_id, 0)
        target = effective_targets.get(instrument_id, current)
        row = execution_rows.get(instrument_id)
        if target <= current or row is None or row.get("open") is None:
            continue
        actual = _execution_cost(
            side="BUY", quantity=target - current,
            open_price=_decimal(row["open"]), config=config,
        )
        if actual["execution_price"] > maximum:
            effective_targets[instrument_id] = current
            cancellations.append({
                "action": "BUY_CANCELLED",
                "instrument_id": instrument_id,
                "target_quantity": target,
                "simulated_quantity": 0,
                "actual_open": _money_text(actual["reference_price"]),
                "maximum_buy_price": _money_text(maximum),
                "reason_codes": ["buy_price_above_maximum_buy_price"],
            })

    positions, cash, fills, _transaction_cost = _execute_targets(
        targets=effective_targets,
        positions=dict(positions),
        cash=opening_cash,
        execution_rows=execution_rows,
        config=config,
        buy_order=[str(item) for item in context["selected"]],
    )
    fills.extend(cancellations)
    for fill in fills:
        if fill.get("action") in {"BUY", "SELL"}:
            fill["actual_open"] = fill["reference_price"]
    transaction_summary, ledger_fills = _ledger_transaction_accounting(fills)
    executed_count = int(transaction_summary["fill_count"])
    if executed_count == len(planned_trade_ids) and not cancellations:
        result = "SIMULATED_FILLED"
    elif executed_count:
        result = "SIMULATED_PARTIAL"
    else:
        result = "NO_FILL"
    execution = {
        **_base_fields(run_id=run_id, generated_at=generated_at),
        "schema_version": "technical-shadow-retrospective-execution.v1",
        "decision_payload_sha256": decision["decision_payload_sha256"],
        "retrospective_execution": True,
        "open_observation_status": (
            "CAPTURED" if execution_rows else "UNAVAILABLE"
        ),
        "open_evidence": open_receipts,
        "opening_cash": _money_text(opening_cash),
        "opening_positions": dict(sorted(opening_positions.items())),
        "simulated_fills": fills,
        "ledger_fills": ledger_fills,
        "execution_result": result,
        "transaction_summary": transaction_summary,
        "ending_cash_after_open": _money_text(cash),
        "ending_positions_after_open": dict(sorted(positions.items())),
        "close_valuation_status": close_status,
        "close_valuation": None,
        "actual_order_submitted": False,
        "strategy_state_mutated": False,
    }
    execution["execution_payload_sha256"] = _digest(execution)
    return execution


def _display_number(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


def _retrospective_report(
    *, ranking: Mapping[str, Any], exposure: Mapping[str, Any],
    decision: Mapping[str, Any], execution: Mapping[str, Any],
) -> str:
    lines = [
        "# Technical Shadow 2026-08-26 独立回溯决策", "",
        "- 运行模式：`retrospective_replay`",
        "- 决策日：`2026-08-26`；执行日：`2026-08-27`",
        "- 迟生成：`true`；执行窗口：`MISSED`；前向证据：`false`",
        "- 正式状态变更：`false`；自动下单：`false`", "",
        "## Top 10 排名", "",
        "|排名|股票|RM20|RM60|RM120|TREND_EFF60|DOWNSIDE_VOL60|BREAKOUT60|score|percentile|eligible|排除码|",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    factor_ids = (
        "RM20", "RM60", "RM120", "TREND_EFF60",
        "DOWNSIDE_VOL60", "BREAKOUT60",
    )
    for row in ranking["top_10"]:
        values = []
        for factor_id in factor_ids:
            raw = _display_number(row.get("factors", {}).get(factor_id))
            z_value = _display_number(row.get("z_scores", {}).get(factor_id))
            values.append(f"{raw} (Z={z_value})")
        lines.append(
            "|{rank}|{instrument}|{factors}|{score}|{percentile}|{eligible}|{codes}|".format(
                rank=row["rank"], instrument=row["instrument_id"],
                factors="|".join(values),
                score=_display_number(row["composite_score"]),
                percentile=_display_number(row["percentile"]),
                eligible=str(bool(row["eligibility"])).lower(),
                codes=",".join(row["exclusion_codes"]) or "-",
            )
        )
    lines.extend(["", "## Exposure", ""])
    for name, details in exposure["inputs"].items():
        lines.append(
            f"- `{name}` = `{_display_number(details['value'])}`; "
            f"used_by_policy=`{str(details['used_by_policy']).lower()}`; "
            f"threshold=`{json.dumps(details['threshold'], ensure_ascii=False, sort_keys=True)}`"
        )
    lines.extend([
        f"- 条件判断：`{json.dumps(exposure['condition_results'], ensure_ascii=False, sort_keys=True)}`",
        f"- 命中规则：`{exposure['matched_rule']}`",
        f"- previous/candidate/final：`{exposure['previous_state']}` / `{exposure['candidate_state']}` / `{exposure['final_state']}`",
        f"- 目标总仓位：`{exposure['target_gross_exposure']}`",
        f"- 原因码：`{','.join(exposure['reason_codes']) or '-'}`",
        "", "## 组合决策与 D+1 人工计划", "",
        f"- Alpha 候选存在：`{str(decision['alpha_candidate_exists']).lower()}`；候选：`{decision['alpha_candidate_instruments']}`",
        f"- 目标股票及数量：`{decision['target_positions']}`",
        f"- 目标权重：`{decision['target_weights']}`",
        f"- BUY / SELL / HOLD / CASH：`{decision['action_counts']}`",
        f"- no-trade 原因：`{decision['no_trade_reason_codes']}`",
    ])
    for action in decision["actions"]:
        lines.append(
            f"- `{action['action']}` `{action.get('instrument_id') or 'CASH'}` "
            f"quantity=`{action.get('quantity', 0)}` reference=`{action.get('reference_price')}` "
            f"maximum_buy=`{action.get('maximum_buy_price')}` reason=`{action.get('reason_codes', [])}`"
        )
    lines.extend(["", "## 2026-08-27 开盘独立模拟", ""])
    lines.append(f"- execution_result：`{execution['execution_result']}`")
    lines.append(f"- simulated_fills：`{json.dumps(execution['simulated_fills'], ensure_ascii=False, sort_keys=True)}`")
    lines.append(f"- transaction_summary：`{json.dumps(execution['transaction_summary'], ensure_ascii=False, sort_keys=True)}`")
    lines.append(f"- close_valuation_status：`{execution['close_valuation_status']}`")
    lines.extend(["", "## 安全边界", ""])
    for key, value in RETROSPECTIVE_SAFETY.items():
        lines.append(f"- `{key}` = `{str(value).lower()}`")
    return "\n".join(lines) + "\n"


def _artifact_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.replace("\r\n", "\n").encode("utf-8")
    return _canonical_bytes(value)


def _verify_retrospective_run(run_root: Path) -> tuple[dict[str, Any], str]:
    actual_names = {
        path.relative_to(run_root).as_posix()
        for path in run_root.rglob("*") if path.is_file()
    }
    if actual_names != EXPECTED_ARTIFACTS | {"manifest.json"}:
        raise TechnicalShadowRetrospectiveError(
            "retrospective_artifact_set_mismatch"
        )
    manifest_path = run_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    base = dict(manifest)
    claimed_payload_sha = base.pop("manifest_payload_sha256", None)
    if claimed_payload_sha != _digest(base):
        raise TechnicalShadowRetrospectiveError(
            "retrospective_manifest_payload_sha256_mismatch"
        )
    for name, expected_sha in manifest["artifacts"].items():
        if name not in EXPECTED_ARTIFACTS:
            raise TechnicalShadowRetrospectiveError(
                "retrospective_manifest_unexpected_artifact"
            )
        if _file_sha256(run_root / name) != expected_sha:
            raise TechnicalShadowRetrospectiveError(
                f"retrospective_artifact_sha256_mismatch:{name}"
            )
    if manifest.get("safety") != RETROSPECTIVE_SAFETY:
        raise TechnicalShadowRetrospectiveError(
            "retrospective_manifest_safety_mismatch"
        )
    return manifest, _file_sha256(manifest_path)


def _publish_retrospective_run(
    *, output_root: Path, run_id: str, generated_at: datetime,
    config: Mapping[str, Any], source_state: Mapping[str, Any],
    formal_snapshot_sha256: str, payloads: Mapping[str, Any],
) -> tuple[Path, str]:
    if set(payloads) != EXPECTED_ARTIFACTS:
        raise TechnicalShadowRetrospectiveError(
            "retrospective_payload_set_mismatch"
        )
    run_root = output_root / STRATEGY_DATE.isoformat() / run_id
    try:
        run_root.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise TechnicalShadowRetrospectiveError(
            f"create_only_run_directory_exists:{run_root}"
        ) from exc
    raw_payloads = {
        name: _artifact_bytes(value) for name, value in payloads.items()
    }
    artifact_hashes = {
        name: sha256(raw).hexdigest()
        for name, raw in sorted(raw_payloads.items())
    }
    manifest_base = {
        **_base_fields(run_id=run_id, generated_at=generated_at),
        "schema_version": "technical-shadow-retrospective-manifest.v1",
        "config_sha256": _digest(config),
        "source_state_date": source_state["state_date"],
        "source_state_record_sha256": source_state["record_sha256"],
        "formal_state_tree_sha256_before": formal_snapshot_sha256,
        "formal_state_tree_sha256_after": formal_snapshot_sha256,
        "formal_state_chain_modified": False,
        "historical_pit_csi800": False,
        "artifacts": artifact_hashes,
    }
    manifest = dict(manifest_base)
    manifest["manifest_payload_sha256"] = _digest(manifest_base)
    for name in sorted(raw_payloads):
        with (run_root / name).open("xb") as stream:
            stream.write(raw_payloads[name])
    with (run_root / "manifest.json").open("xb") as stream:
        stream.write(_canonical_bytes(manifest))
    _verified_manifest, manifest_sha = _verify_retrospective_run(run_root)
    return run_root, manifest_sha


def run_retrospective(
    *, config: Mapping[str, Any], state_root: Path = DEFAULT_STATE_ROOT,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    source: BaoStockTechnicalShadowSource | None = None,
    now: datetime | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Run exactly one isolated replay without writing the formal state tree."""

    request_started_at = (now or datetime.now(CHINA_TZ)).astimezone(CHINA_TZ)
    if request_started_at.tzinfo is None:
        raise TechnicalShadowRetrospectiveError("generated_at_timezone_required")
    before_snapshot = _tree_snapshot(state_root)
    before_snapshot_sha = _digest(before_snapshot)
    source_state, source_plan, _source_manifest, source_manifest_sha, source_slot = (
        _load_verified_source_state(state_root=state_root, config=config)
    )
    captured = (source or BaoStockTechnicalShadowSource()).capture(
        instrument_ids=list(config["universe"]["instrument_ids"]),
        benchmark_id=str(config["data"]["benchmark_id"]),
        recent_completed_sessions=1,
        lookback_days=int(config["data"]["calendar_lookback_days"]),
        now=request_started_at,
        completed_through=STRATEGY_DATE,
    )
    generated_at = datetime.now(CHINA_TZ) if now is None else request_started_at
    run_id = (
        f"{generated_at.strftime('%Y%m%dT%H%M%S%z')}-"
        f"{uuid.uuid4().hex[:8]}"
    )
    data_receipt, ranking, exposure, decision, context = (
        build_retrospective_decision(
            run_id=run_id, generated_at=generated_at, config=config,
            captured=captured, source_state=source_state,
            source_plan=source_plan,
            source_manifest_sha256=source_manifest_sha,
        )
    )
    decision_fingerprint = _digest({
        "data_receipt": data_receipt["decision_input_sha256"],
        "ranking": ranking["ranking_payload_sha256"],
        "exposure": exposure["exposure_payload_sha256"],
        "decision": decision["decision_payload_sha256"],
    })
    execution = build_retrospective_execution(
        run_id=run_id, generated_at=generated_at, config=config,
        decision=decision, context=context,
    )
    if decision_fingerprint != _digest({
        "data_receipt": data_receipt["decision_input_sha256"],
        "ranking": ranking["ranking_payload_sha256"],
        "exposure": exposure["exposure_payload_sha256"],
        "decision": decision["decision_payload_sha256"],
    }):
        raise TechnicalShadowRetrospectiveError(
            "d_plus_one_observation_mutated_decision"
        )
    after_execution_snapshot = _tree_snapshot(state_root)
    if after_execution_snapshot != before_snapshot:
        raise TechnicalShadowRetrospectiveError(
            "formal_state_chain_changed_during_retrospective"
        )
    report = _retrospective_report(
        ranking=ranking, exposure=exposure, decision=decision,
        execution=execution,
    )
    payloads: dict[str, Any] = {
        "data_receipt.json": data_receipt,
        "ranking.json": ranking,
        "exposure.json": exposure,
        "portfolio_decision.json": decision,
        "retrospective_execution.json": execution,
        "retrospective_report.md": report,
    }
    run_root, manifest_sha = _publish_retrospective_run(
        output_root=output_root, run_id=run_id,
        generated_at=generated_at, config=config,
        source_state=source_state,
        formal_snapshot_sha256=before_snapshot_sha,
        payloads=payloads,
    )
    if _tree_snapshot(state_root) != before_snapshot:
        raise TechnicalShadowRetrospectiveError(
            "formal_state_chain_changed_after_retrospective_publish"
        )
    result = {
        "strategy_date": STRATEGY_DATE.isoformat(),
        "execution_date": EXECUTION_DATE.isoformat(),
        "generated_at": generated_at.isoformat(),
        "run_mode": RUN_MODE,
        "exposure_state": exposure["final_state"],
        "target_gross_exposure": exposure["target_gross_exposure"],
        "top_10": ranking["top_10"],
        "action_counts": decision["action_counts"],
        "actions": decision["actions"],
        "no_trade_reason_codes": decision["no_trade_reason_codes"],
        "execution_result": execution["execution_result"],
        "simulated_fills": execution["simulated_fills"],
        "transaction_summary": execution["transaction_summary"],
        "close_valuation_status": execution["close_valuation_status"],
        "output_directory": str(run_root.resolve()),
        "manifest_sha256": manifest_sha,
        "source_state_directory": source_slot,
        "formal_state_chain_modified": False,
        "automatic_order_submission": False,
    }
    return run_root, result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = _load_config(args.config)
    _run_root, result = run_retrospective(
        config=config, state_root=args.state_root,
        output_root=args.output_root,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
