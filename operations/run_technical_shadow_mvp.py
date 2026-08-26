"""Run the non-admitted ten-session A-share technical Shadow business loop."""

from __future__ import annotations

import argparse
import importlib
import json
import math
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

from research.market_data.providers.baostock import BaoStockProvider, to_baostock_code
from research.strategy_workspace.technical_alpha_shadow_v1 import rank_technical_alpha_shadow
from research.strategy_workspace.technical_exposure_shadow_v1 import compute_technical_shadow_exposure


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
STRATEGY_ID = "a-share-technical-shadow-mvp-v1"
DEFAULT_CONFIG = Path("configs/a_share_technical_shadow_mvp.v1.json")
DEFAULT_OUTPUT_ROOT = Path("data/tmp/technical-shadow-mvp")
DAILY_FIELDS = (
    "date", "code", "open", "high", "low", "close", "preclose", "volume",
    "amount", "adjustflag", "tradestatus", "isST",
)
CENT = Decimal("0.01")


class TechnicalShadowRunError(RuntimeError):
    pass


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _digest(value: Any) -> str:
    return sha256(_canonical_bytes(value)).hexdigest()


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _money_text(value: Decimal) -> str:
    return format(_money(value), ".2f")


def _write_new(path: Path, value: Any, artifacts: dict[str, str], root: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = value if isinstance(value, bytes) else _canonical_bytes(value)
    try:
        with path.open("xb") as stream:
            stream.write(raw)
    except FileExistsError as exc:
        raise TechnicalShadowRunError(f"create_only_path_exists:{path}") from exc
    artifacts[path.relative_to(root).as_posix()] = sha256(raw).hexdigest()


def _write_text_new(path: Path, value: str, artifacts: dict[str, str], root: Path) -> None:
    raw = value.replace("\r\n", "\n").encode("utf-8")
    _write_new(path, raw, artifacts, root)


def validate_source_provenance(*, provider_id: str, provider_kind: str, synthetic: bool) -> None:
    if provider_kind == "real_provider" and (provider_id != "baostock" or synthetic):
        raise TechnicalShadowRunError("mock_or_synthetic_cannot_be_marked_real_provider")


def _load_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("strategy_id") != STRATEGY_ID:
        raise TechnicalShadowRunError("strategy_id_mismatch")
    if payload.get("purpose") != "business_loop_validation" or payload.get("research_status") != "heuristic_shadow_baseline":
        raise TechnicalShadowRunError("shadow_status_mismatch")
    safety = payload.get("safety", {})
    if any(safety.get(key) is not False for key in (
        "paper_eligibility", "trade_eligibility", "real_money_list_allowed",
        "automatic_order_submission", "live_supported",
    )):
        raise TechnicalShadowRunError("shadow_safety_must_remain_false")
    instruments = payload.get("universe", {}).get("instrument_ids", [])
    if len(instruments) != 60 or len(set(instruments)) != 60:
        raise TechnicalShadowRunError("frozen_universe_must_have_60_unique_ids")
    if payload.get("data", {}).get("minimum_common_sessions") < 121:
        raise TechnicalShadowRunError("minimum_common_sessions_below_121")
    validate_source_provenance(
        provider_id=str(payload["data"]["provider_id"]),
        provider_kind=str(payload["data"]["provider_kind"]),
        synthetic=False,
    )
    return payload


def _optional_number(value: str) -> float | None:
    text = str(value).strip()
    if not text:
        return None
    parsed = float(text)
    return parsed if math.isfinite(parsed) else None


@dataclass(frozen=True)
class CapturedData:
    provider_id: str
    provider_kind: str
    adapter_version: str
    synthetic: bool
    captured_at: str
    sessions: tuple[date, ...]
    stock_rows: Mapping[str, tuple[Mapping[str, Any], ...]]
    benchmark_rows: tuple[Mapping[str, Any], ...]
    receipts: Mapping[str, Mapping[str, Any]]


class BaoStockTechnicalShadowSource:
    """Thin read-only SDK client; isolated from the formal MarketData V1 contract."""

    provider_id = "baostock"
    provider_kind = "real_provider"
    adapter_version = "baostock-technical-shadow-adapter-v1"
    synthetic = False

    @staticmethod
    def _rows(result: Any, operation: str) -> tuple[list[str], list[list[str]]]:
        return BaoStockProvider._query_rows(result, operation)

    def capture(
        self,
        *,
        instrument_ids: Sequence[str],
        benchmark_id: str,
        recent_completed_sessions: int,
        lookback_days: int,
        now: datetime,
    ) -> CapturedData:
        validate_source_provenance(
            provider_id=self.provider_id, provider_kind=self.provider_kind, synthetic=self.synthetic
        )
        sdk = importlib.import_module("baostock")
        login = sdk.login()
        BaoStockProvider._check_result(login, "login")
        captured_at = now.astimezone(CHINA_TZ).isoformat()
        try:
            end_day = now.astimezone(CHINA_TZ).date() - timedelta(days=1)
            start_day = end_day - timedelta(days=lookback_days)
            calendar_result = sdk.query_trade_dates(
                start_date=start_day.isoformat(), end_date=end_day.isoformat()
            )
            calendar_fields, calendar_raw = self._rows(calendar_result, "query_trade_dates")
            if set(calendar_fields) != {"calendar_date", "is_trading_day"}:
                raise TechnicalShadowRunError("baostock_calendar_contract_changed")
            calendar_dicts = [dict(zip(calendar_fields, row, strict=True)) for row in calendar_raw]
            open_sessions = tuple(
                date.fromisoformat(row["calendar_date"])
                for row in calendar_dicts if row["is_trading_day"] == "1"
            )
            required_count = 120 + recent_completed_sessions + 1
            if len(open_sessions) < required_count:
                raise TechnicalShadowRunError("fewer_than_required_completed_sessions")
            sessions = open_sessions[-required_count:]
            query_start, query_end = sessions[0], sessions[-1]
            receipts: dict[str, Mapping[str, Any]] = {
                "calendar": {
                    "receipt_type": "baostock_trade_calendar_capture",
                    "provider_id": self.provider_id,
                    "provider_kind": self.provider_kind,
                    "adapter_version": self.adapter_version,
                    "synthetic": False,
                    "captured_at": captured_at,
                    "request": {"start_date": start_day.isoformat(), "end_date": end_day.isoformat()},
                    "fields": calendar_fields,
                    "rows": calendar_raw,
                    "raw_content_sha256": _digest({"fields": calendar_fields, "rows": calendar_raw}),
                }
            }

            def query_daily(instrument_id: str, *, is_benchmark: bool) -> tuple[Mapping[str, Any], ...]:
                result = sdk.query_history_k_data_plus(
                    to_baostock_code(instrument_id),
                    ",".join(DAILY_FIELDS),
                    start_date=query_start.isoformat(),
                    end_date=query_end.isoformat(),
                    frequency="d",
                    adjustflag="3",
                )
                fields, raw_rows = self._rows(result, f"query_history_k_data_plus:{instrument_id}")
                if tuple(fields) != DAILY_FIELDS:
                    raise TechnicalShadowRunError(f"baostock_daily_contract_changed:{instrument_id}")
                normalized: list[Mapping[str, Any]] = []
                for raw_values in raw_rows:
                    raw = dict(zip(fields, raw_values, strict=True))
                    normalized.append({
                        "instrument_id": instrument_id,
                        "trading_date": raw["date"],
                        "open": _optional_number(raw["open"]),
                        "high": _optional_number(raw["high"]),
                        "low": _optional_number(raw["low"]),
                        "close": _optional_number(raw["close"]),
                        "preclose": _optional_number(raw["preclose"]),
                        "volume": _optional_number(raw["volume"]),
                        "amount": _optional_number(raw["amount"]),
                        "adjustment": "none",
                        "trading_status": "traded" if raw["tradestatus"] == "1" else "suspended",
                        "is_st": False if is_benchmark else raw["isST"] == "1",
                        "available_at": datetime.combine(date.fromisoformat(raw["date"]), datetime.min.time().replace(hour=15, minute=30), CHINA_TZ).isoformat(),
                    })
                receipt_key = "benchmark" if is_benchmark else f"stocks/{instrument_id}"
                receipts[receipt_key] = {
                    "receipt_type": "baostock_daily_capture",
                    "provider_id": self.provider_id,
                    "provider_kind": self.provider_kind,
                    "adapter_version": self.adapter_version,
                    "synthetic": False,
                    "captured_at": captured_at,
                    "instrument_id": instrument_id,
                    "is_benchmark": is_benchmark,
                    "request": {
                        "start_date": query_start.isoformat(), "end_date": query_end.isoformat(),
                        "frequency": "d", "adjustflag": "3", "fields": list(DAILY_FIELDS),
                    },
                    "record_count": len(normalized),
                    "raw_content_sha256": _digest({"fields": fields, "rows": raw_rows}),
                    "normalized_content_sha256": _digest(normalized),
                    "records": normalized,
                }
                return tuple(normalized)

            benchmark_rows = query_daily(benchmark_id, is_benchmark=True)
            stock_rows = {item: query_daily(item, is_benchmark=False) for item in instrument_ids}
            return CapturedData(
                provider_id=self.provider_id,
                provider_kind=self.provider_kind,
                adapter_version=self.adapter_version,
                synthetic=False,
                captured_at=captured_at,
                sessions=sessions,
                stock_rows=stock_rows,
                benchmark_rows=benchmark_rows,
                receipts=receipts,
            )
        finally:
            try:
                sdk.logout()
            except Exception:
                pass


def _row_map(rows: Sequence[Mapping[str, Any]]) -> dict[date, Mapping[str, Any]]:
    return {date.fromisoformat(str(row["trading_date"])): row for row in rows}


def _execution_cost(
    *, side: str, quantity: int, open_price: Decimal, config: Mapping[str, Any]
) -> dict[str, Decimal]:
    costs = config["costs"]
    slippage_rate = _decimal(costs["slippage_bps_one_way"]) / Decimal("10000")
    execution_price = open_price * (Decimal("1") + slippage_rate if side == "BUY" else Decimal("1") - slippage_rate)
    base_notional = open_price * quantity
    execution_notional = execution_price * quantity
    commission = max(_decimal(costs["minimum_commission"]), execution_notional * _decimal(costs["commission_rate"]))
    transfer = execution_notional * _decimal(costs["transfer_fee_rate_both_sides"])
    sell_tax = execution_notional * _decimal(costs["sell_tax_rate"]) if side == "SELL" else Decimal("0")
    slippage = abs(execution_price - open_price) * quantity
    explicit = commission + transfer + sell_tax
    return {
        "execution_price": execution_price,
        "base_notional": base_notional,
        "execution_notional": execution_notional,
        "commission": commission,
        "transfer_fee": transfer,
        "sell_tax": sell_tax,
        "slippage": slippage,
        "explicit_fees": explicit,
        "total_cost": explicit + slippage,
    }


def _plan_targets(
    *, ranking: Sequence[Mapping[str, Any]], positions: Mapping[str, int], nav: Decimal,
    target_exposure: float, max_positions: int, max_weight: Decimal, lot_size: int,
    close_by_id: Mapping[str, Decimal],
) -> tuple[dict[str, int], list[str]]:
    by_id = {str(row["instrument_id"]): row for row in ranking}
    incumbents = [
        item for item in positions
        if item in by_id and by_id[item]["hold_eligible"] and item in close_by_id
    ]
    incumbents.sort(key=lambda item: (by_id[item]["rank"], item))
    entries = [
        str(row["instrument_id"]) for row in ranking
        if row["entry_eligible"] and row["instrument_id"] not in incumbents and row["instrument_id"] in close_by_id
    ]
    selected = (incumbents + entries)[:max_positions]
    if not selected or target_exposure <= 0:
        return ({item: 0 for item in positions}, [])
    per_weight = min(max_weight, _decimal(target_exposure) / len(selected))
    targets: dict[str, int] = {item: 0 for item in positions}
    for item in selected:
        raw = nav * per_weight / close_by_id[item]
        targets[item] = int((raw / lot_size).to_integral_value(rounding=ROUND_DOWN)) * lot_size
    return targets, selected


def _cash_reason_codes(
    *, ranking: Sequence[Mapping[str, Any]], positions: Mapping[str, int],
    selected: Sequence[str], target_exposure: float,
) -> list[str]:
    if selected:
        return []
    has_alpha_candidate = any(
        bool(row["entry_eligible"])
        or (
            str(row["instrument_id"]) in positions
            and bool(row["hold_eligible"])
        )
        for row in ranking
    )
    if target_exposure <= 0 and has_alpha_candidate:
        return ["RISK_OFF_CASH"]
    return ["NO_ALPHA_CASH"]


def _execute_targets(
    *, targets: Mapping[str, int], positions: dict[str, int], cash: Decimal,
    execution_rows: Mapping[str, Mapping[str, Any]], config: Mapping[str, Any],
    buy_order: Sequence[str] | None = None,
) -> tuple[dict[str, int], Decimal, list[dict[str, Any]], Decimal]:
    fills: list[dict[str, Any]] = []
    total_cost = Decimal("0")
    lot_size = int(config["portfolio"]["lot_size"])

    def add_fill(side: str, item: str, quantity: int, cost: Mapping[str, Decimal], reason: str) -> None:
        nonlocal total_cost
        total_cost += cost["total_cost"]
        fills.append({
            "action": side, "instrument_id": item, "simulated_quantity": quantity,
            "target_quantity": targets.get(item, positions.get(item, 0)),
            "market_open_price": _money_text(cost["base_notional"] / quantity),
            "simulated_fill_price": _money_text(cost["execution_price"]),
            "commission": _money_text(cost["commission"]),
            "transfer_fee": _money_text(cost["transfer_fee"]),
            "sell_tax": _money_text(cost["sell_tax"]),
            "slippage_cost": _money_text(cost["slippage"]),
            "total_cost": _money_text(cost["total_cost"]), "reason_codes": [reason],
        })

    for item in sorted(set(positions) | set(targets)):
        delta = targets.get(item, 0) - positions.get(item, 0)
        if delta >= 0:
            continue
        row = execution_rows.get(item)
        if row is None or row.get("trading_status") != "traded" or row.get("open") is None:
            fills.append({"action": "SELL_CANCELLED", "instrument_id": item, "target_quantity": targets.get(item, 0), "simulated_quantity": 0, "reason_codes": ["execution_data_unavailable_fail_closed"]})
            continue
        quantity = -delta
        cost = _execution_cost(side="SELL", quantity=quantity, open_price=_decimal(row["open"]), config=config)
        cash += cost["execution_notional"] - cost["explicit_fees"]
        positions[item] -= quantity
        if positions[item] == 0:
            del positions[item]
        add_fill("SELL", item, quantity, cost, "d_signal_d_plus_1_open")

    buy_candidates = {item for item in targets if targets[item] > positions.get(item, 0)}
    ordered_buys = [item for item in (buy_order or ()) if item in buy_candidates]
    ordered_buys.extend(sorted(buy_candidates - set(ordered_buys)))
    for item in ordered_buys:
        row = execution_rows.get(item)
        target_delta = targets[item] - positions.get(item, 0)
        if row is None or row.get("trading_status") != "traded" or bool(row.get("is_st")) or row.get("open") is None:
            fills.append({"action": "BUY_CANCELLED", "instrument_id": item, "target_quantity": targets[item], "simulated_quantity": 0, "reason_codes": ["execution_not_tradable_or_st_fail_closed"]})
            continue
        quantity = target_delta
        cost: dict[str, Decimal] | None = None
        while quantity > 0:
            candidate = _execution_cost(side="BUY", quantity=quantity, open_price=_decimal(row["open"]), config=config)
            if candidate["execution_notional"] + candidate["explicit_fees"] <= cash:
                cost = candidate
                break
            quantity -= lot_size
        if quantity <= 0 or cost is None:
            fills.append({"action": "BUY_CANCELLED", "instrument_id": item, "target_quantity": targets[item], "simulated_quantity": 0, "reason_codes": ["cash_or_whole_lot_unavailable"]})
            continue
        cash -= cost["execution_notional"] + cost["explicit_fees"]
        positions[item] = positions.get(item, 0) + quantity
        add_fill("BUY", item, quantity, cost, "d_signal_d_plus_1_open")
    if cash < Decimal("-0.005"):
        raise TechnicalShadowRunError("cash_overdraft")
    return positions, _money(cash), fills, _money(total_cost)


def _decision_markdown(decision: Mapping[str, Any]) -> str:
    fills = decision["fills"]
    lines = [
        f"# Technical Shadow 决策 {decision['decision_date']}", "",
        f"- 执行日：`{decision['execution_date']}`",
        f"- 市场状态：`{decision['market_state']}`；目标总仓位：`{decision['target_gross_exposure']:.2%}`",
        f"- 当前仓位：`{decision['opening_positions']}`",
        f"- 目标仓位：`{decision['target_positions']}`",
        f"- 收盘 NAV：`{decision['closing_nav']}`；现金：`{decision['closing_cash']}`",
        f"- 数据失败关闭：`{str(decision['data_fail_closed']).lower()}`",
        "", "## BUY / SELL / HOLD / CASH", "",
    ]
    for fill in fills:
        lines.append(
            f"- {fill['action']} {fill.get('instrument_id') or 'CASH'} target={fill.get('target_quantity', '-')}, "
            f"filled={fill.get('simulated_quantity', 0)}, price={fill.get('simulated_fill_price', '-')}, "
            f"cost={fill.get('total_cost', '0.00')}, reason={','.join(fill.get('reason_codes', []))}"
        )
    return "\n".join(lines) + "\n"


def run_replay(
    *, config: Mapping[str, Any], captured: CapturedData, recent_completed_sessions: int,
    initial_cash: Decimal, output_root: Path, run_id: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    validate_source_provenance(
        provider_id=captured.provider_id, provider_kind=captured.provider_kind, synthetic=captured.synthetic
    )
    if recent_completed_sessions <= 0:
        raise TechnicalShadowRunError("recent_completed_sessions_must_be_positive")
    expected_count = 120 + recent_completed_sessions + 1
    if len(captured.sessions) < expected_count:
        raise TechnicalShadowRunError("captured_sessions_insufficient")
    sessions = captured.sessions[-expected_count:]
    decision_dates = sessions[120:-1]
    execution_dates = sessions[121:]
    if len(decision_dates) != recent_completed_sessions or len(execution_dates) != recent_completed_sessions:
        raise TechnicalShadowRunError("decision_execution_calendar_mismatch")
    run_id = run_id or f"{datetime.now(CHINA_TZ).strftime('%Y%m%dT%H%M%S%z')}-{uuid.uuid4().hex[:8]}"
    run_root = output_root / run_id
    try:
        run_root.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise TechnicalShadowRunError(f"create_only_run_directory_exists:{run_root}") from exc
    artifacts: dict[str, str] = {}
    for key, receipt in sorted(captured.receipts.items()):
        _write_new(run_root / "data_receipts" / f"{key}.json", receipt, artifacts, run_root)

    instrument_ids = list(config["universe"]["instrument_ids"])
    benchmark_map = _row_map(captured.benchmark_rows)
    stock_maps = {item: _row_map(captured.stock_rows.get(item, ())) for item in instrument_ids}
    cash = _money(initial_cash)
    positions: dict[str, int] = {}
    peak_nav = cash
    total_cost = Decimal("0")
    ledger: list[dict[str, Any]] = []
    action_counts = {"BUY": 0, "SELL": 0, "HOLD": 0}
    cash_days = 0
    data_fail_closed = False

    for decision_date, execution_date in zip(decision_dates, execution_dates, strict=True):
        history_sessions = tuple(day for day in sessions if day <= decision_date)
        stock_slices = {
            item: tuple(row for row in captured.stock_rows.get(item, ()) if date.fromisoformat(str(row["trading_date"])) <= decision_date)
            for item in instrument_ids
        }
        benchmark_slice = tuple(row for row in captured.benchmark_rows if date.fromisoformat(str(row["trading_date"])) <= decision_date)
        ranking = rank_technical_alpha_shadow(
            decision_date=decision_date, sessions=history_sessions, instrument_ids=instrument_ids,
            stock_rows=stock_slices, benchmark_rows=benchmark_slice,
            winsor_lower_quantile=float(config["alpha"]["winsor_lower_quantile"]),
            winsor_upper_quantile=float(config["alpha"]["winsor_upper_quantile"]),
        )
        ranking_payload = {
            "strategy_id": STRATEGY_ID, "purpose": "business_loop_validation",
            "research_status": "heuristic_shadow_baseline", "decision_date": decision_date.isoformat(),
            "universe_basis": config["universe"]["basis"], "historical_pit_csi800": False,
            "rows": ranking,
            "safety": config["safety"],
        }
        ranking_payload["ranking_sha256"] = _digest(ranking_payload)
        _write_new(run_root / "ranking" / f"{decision_date}.ranking.json", ranking_payload, artifacts, run_root)

        decision_close_by_id = {
            item: _decimal(stock_maps[item][decision_date]["close"])
            for item in instrument_ids
            if decision_date in stock_maps[item] and stock_maps[item][decision_date].get("close") is not None
        }
        opening_nav = cash + sum(decision_close_by_id.get(item, Decimal("0")) * quantity for item, quantity in positions.items())
        peak_nav = max(peak_nav, opening_nav)
        eligible_ids = {row["instrument_id"] for row in ranking if row["eligibility"]}
        exposure = compute_technical_shadow_exposure(
            benchmark_rows=benchmark_slice,
            eligible_stock_rows=[stock_slices[item] for item in instrument_ids if item in eligible_ids],
            current_nav=float(opening_nav), peak_nav=float(peak_nav), policy=config["exposure"],
        )
        data_fail_closed = data_fail_closed or bool(exposure["data_fail_closed"])
        targets, selected = _plan_targets(
            ranking=ranking, positions=positions, nav=opening_nav,
            target_exposure=float(exposure["target_gross_exposure"]),
            max_positions=int(config["portfolio"]["max_positions"]),
            max_weight=_decimal(config["portfolio"]["max_position_weight"]),
            lot_size=int(config["portfolio"]["lot_size"]), close_by_id=decision_close_by_id,
        )
        execution_rows = {
            item: stock_maps[item][execution_date] for item in instrument_ids if execution_date in stock_maps[item]
        }
        opening_positions = dict(sorted(positions.items()))
        opening_cash = cash
        positions, cash, fills, day_cost = _execute_targets(
            targets=targets, positions=dict(positions), cash=cash,
            execution_rows=execution_rows, config=config, buy_order=selected,
        )
        total_cost += day_cost
        for item in sorted(positions):
            if not any(fill.get("instrument_id") == item and fill["action"] in {"BUY", "SELL"} for fill in fills):
                fills.append({
                    "action": "HOLD", "instrument_id": item, "target_quantity": targets.get(item, positions[item]),
                    "simulated_quantity": 0, "simulated_fill_price": None, "total_cost": "0.00",
                    "reason_codes": ["incumbent_within_hold_band" if item in selected else "execution_cancelled_position_retained"],
                })
        fills.append({
            "action": "CASH", "instrument_id": None, "target_quantity": 0,
            "simulated_quantity": 0, "simulated_fill_price": None, "total_cost": "0.00",
            "cash_balance": _money_text(cash), "reason_codes": ["residual_cash_preserved"],
        })
        for action in action_counts:
            action_counts[action] += sum(fill["action"] == action for fill in fills)
        if not positions:
            cash_days += 1
        execution_close = {
            item: _decimal(row["close"]) for item, row in execution_rows.items() if row.get("close") is not None
        }
        missing_marks = sorted(item for item in positions if item not in execution_close)
        if missing_marks:
            data_fail_closed = True
            raise TechnicalShadowRunError(f"missing_execution_close_marks:{missing_marks}")
        closing_nav = cash + sum(execution_close[item] * quantity for item, quantity in positions.items())
        peak_nav = max(peak_nav, closing_nav)
        ranking_data_failure = any(
            any(code in {"duplicate_trading_date", "missing_common_session", "invalid_or_missing_ohlcv"} for code in row["exclusion_codes"])
            for row in ranking
        )
        data_fail_closed = data_fail_closed or ranking_data_failure
        daily_record = {
            "strategy_id": STRATEGY_ID, "decision_date": decision_date.isoformat(),
            "execution_date": execution_date.isoformat(), "market_state": exposure["market_state"],
            "target_gross_exposure": exposure["target_gross_exposure"],
            "exposure_inputs": exposure, "opening_cash": _money_text(opening_cash),
            "opening_positions": opening_positions, "opening_nav_at_decision_close": _money_text(opening_nav),
            "selected_instruments": selected, "target_positions": dict(sorted(targets.items())),
            "fills": fills, "closing_positions": dict(sorted(positions.items())),
            "closing_cash": _money_text(cash), "closing_nav": _money_text(closing_nav),
            "daily_transaction_cost": _money_text(day_cost),
            "data_fail_closed": bool(exposure["data_fail_closed"] or ranking_data_failure),
            "reason_codes": exposure["reason_codes"] + _cash_reason_codes(
                ranking=ranking,
                positions=opening_positions,
                selected=selected,
                target_exposure=float(exposure["target_gross_exposure"]),
            ),
            "automatic_order_submission": False, "live_supported": False,
        }
        daily_record["decision_sha256"] = _digest(daily_record)
        _write_new(run_root / "daily" / f"{decision_date}.decision.json", daily_record, artifacts, run_root)
        _write_text_new(run_root / "daily" / f"{decision_date}.decision.md", _decision_markdown(daily_record), artifacts, run_root)
        ledger.append({
            "event_type": "D_PLUS_1_SIMULATED_ACCOUNT_CLOSE", "sequence": len(ledger) + 1,
            "decision_date": decision_date.isoformat(), "execution_date": execution_date.isoformat(),
            "cash": _money_text(cash), "positions": dict(sorted(positions.items())),
            "nav": _money_text(closing_nav), "transaction_cost": _money_text(day_cost),
            "previous_event_sha256": ledger[-1]["event_sha256"] if ledger else None,
        })
        ledger[-1]["event_sha256"] = _digest(ledger[-1])

    ledger_raw = b"".join(_canonical_bytes(item) for item in ledger)
    _write_new(run_root / "ledger.jsonl", ledger_raw, artifacts, run_root)
    navs = [initial_cash] + [_decimal(item["nav"]) for item in ledger]
    running_peak = navs[0]
    max_drawdown = Decimal("0")
    for nav in navs:
        running_peak = max(running_peak, nav)
        max_drawdown = min(max_drawdown, nav / running_peak - Decimal("1"))
    final_nav = _decimal(ledger[-1]["nav"])
    complete_instruments = sum(
        all(day in stock_maps[item] and stock_maps[item][day].get("close") is not None for day in sessions)
        for item in instrument_ids
    )
    summary = {
        "strategy_id": STRATEGY_ID, "purpose": "business_loop_validation",
        "research_status": "heuristic_shadow_baseline",
        "actual_decision_date_range": [decision_dates[0].isoformat(), decision_dates[-1].isoformat()],
        "actual_execution_date_range": [execution_dates[0].isoformat(), execution_dates[-1].isoformat()],
        "actual_stock_count": len(instrument_ids), "data_complete_stock_count": complete_instruments,
        "daily_decision_count": len(ledger), "buy_count": action_counts["BUY"],
        "sell_count": action_counts["SELL"], "hold_count": action_counts["HOLD"],
        "cash_day_count": cash_days, "final_positions": dict(sorted(positions.items())),
        "final_cash": _money_text(cash), "final_nav": _money_text(final_nav),
        "total_transaction_cost": _money_text(total_cost), "maximum_drawdown": float(max_drawdown),
        "data_fail_closed_occurred": data_fail_closed,
        "provider_id": captured.provider_id, "provider_kind": captured.provider_kind,
        "synthetic": captured.synthetic, "output_directory": str(run_root.resolve()),
        "paper_eligibility": False, "trade_eligibility": False,
        "real_money_list_allowed": False, "automatic_order_submission": False,
        "live_supported": False,
    }
    _write_new(run_root / "run_summary.json", summary, artifacts, run_root)
    summary_md = "\n".join([
        "# A股技术 Shadow MVP 真实业务回放", "",
        f"- 决策日期：`{summary['actual_decision_date_range'][0]}` 至 `{summary['actual_decision_date_range'][1]}`",
        f"- 执行日期：`{summary['actual_execution_date_range'][0]}` 至 `{summary['actual_execution_date_range'][1]}`",
        f"- 股票池 / 完整股票：`{summary['actual_stock_count']} / {summary['data_complete_stock_count']}`",
        f"- 决策 / BUY / SELL / HOLD / CASH日：`{summary['daily_decision_count']} / {summary['buy_count']} / {summary['sell_count']} / {summary['hold_count']} / {summary['cash_day_count']}`",
        f"- 最终持仓：`{summary['final_positions']}`",
        f"- 最终现金 / NAV：`{summary['final_cash']} / {summary['final_nav']}`",
        f"- 总交易成本 / 最大回撤：`{summary['total_transaction_cost']} / {summary['maximum_drawdown']:.6%}`",
        f"- DATA_FAIL_CLOSED：`{str(summary['data_fail_closed_occurred']).lower()}`",
        "- 状态：`heuristic_shadow_baseline`；非历史PIT中证800；Paper、交易、真实资金、自动下单和LIVE全部禁止。",
        "",
    ])
    _write_text_new(run_root / "run_summary.md", summary_md, artifacts, run_root)
    manifest = {
        "schema_version": "technical-shadow-mvp-run-manifest.v1", "strategy_id": STRATEGY_ID,
        "created_at": datetime.now(CHINA_TZ).isoformat(), "config_sha256": _digest(config),
        "provider": {"provider_id": captured.provider_id, "provider_kind": captured.provider_kind, "adapter_version": captured.adapter_version, "synthetic": captured.synthetic},
        "universe_basis": config["universe"]["basis"], "historical_pit_csi800": False,
        "artifacts": dict(sorted(artifacts.items())), "summary_sha256": _digest(summary),
        "safety": config["safety"],
    }
    _write_new(run_root / "run_manifest.json", manifest, artifacts, run_root)
    return run_root, summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recent-completed-sessions", type=int, default=10)
    parser.add_argument("--initial-cash", type=Decimal, default=Decimal("10000"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = _load_config(args.config)
    if args.initial_cash <= 0:
        raise TechnicalShadowRunError("initial_cash_must_be_positive")
    source = BaoStockTechnicalShadowSource()
    now = datetime.now(CHINA_TZ)
    captured = source.capture(
        instrument_ids=config["universe"]["instrument_ids"],
        benchmark_id=config["data"]["benchmark_id"],
        recent_completed_sessions=args.recent_completed_sessions,
        lookback_days=int(config["data"]["calendar_lookback_days"]), now=now,
    )
    run_root, summary = run_replay(
        config=config, captured=captured,
        recent_completed_sessions=args.recent_completed_sessions,
        initial_cash=args.initial_cash, output_root=args.output_root,
    )
    print(json.dumps({"output_directory": str(run_root.resolve()), "summary": summary}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BaoStockTechnicalShadowSource", "CapturedData", "TechnicalShadowRunError",
    "run_replay", "validate_source_provenance",
]
