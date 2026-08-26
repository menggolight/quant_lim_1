from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from operations.run_technical_shadow_daily import (
    DAILY_SAFETY,
    NextSessionEvidence,
    TechnicalShadowDailyError,
    _latest_data_complete_capture,
    run_daily,
)
from operations.run_technical_shadow_mvp import (
    CapturedData,
    _canonical_bytes,
    _digest,
    _load_config,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "configs" / "a_share_technical_shadow_mvp.v1.json"
FIRST_STRATEGY_DATE = date(2026, 8, 25)


def _row(instrument_id: str, day: date, close: float) -> dict[str, object]:
    return {
        "instrument_id": instrument_id,
        "trading_date": day.isoformat(),
        "open": close,
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
        "preclose": close,
        "volume": 1_000_000.0,
        "amount": close * 1_000_000,
        "adjustment": "none",
        "trading_status": "traded",
        "is_st": False,
        "available_at": f"{day.isoformat()}T15:30:00+08:00",
    }


def _captured(
    config: dict,
    strategy_date: date,
    *,
    omit_immediately_previous_session: bool = False,
) -> CapturedData:
    if omit_immediately_previous_session:
        sessions = tuple(
            strategy_date - timedelta(days=121 - index) for index in range(120)
        ) + (strategy_date,)
    else:
        sessions = tuple(
            strategy_date - timedelta(days=120 - index) for index in range(121)
        )
    origin = date(2026, 1, 1)
    benchmark_rows = tuple(
        _row(
            str(config["data"]["benchmark_id"]),
            day,
            300.0 - (day - origin).days * 0.20,
        )
        for day in sessions
    )
    stock_rows: dict[str, tuple[dict[str, object], ...]] = {}
    for offset, instrument_id in enumerate(config["universe"]["instrument_ids"]):
        stock_rows[instrument_id] = tuple(
            _row(
                instrument_id,
                day,
                8.0
                + offset * 0.08
                + (day - origin).days * (0.004 + offset * 0.00003)
                + ((day.toordinal() + offset) % 7) * 0.002,
            )
            for day in sessions
        )
    return CapturedData(
        provider_id="fixture",
        provider_kind="test_fixture",
        adapter_version="offline-daily-fixture-v1",
        synthetic=True,
        captured_at="2099-01-01T00:00:00+08:00",
        sessions=sessions,
        stock_rows=stock_rows,
        benchmark_rows=benchmark_rows,
        receipts={},
    )


def _evidence(strategy_date: date) -> NextSessionEvidence:
    execution_date = strategy_date + timedelta(days=1)
    rows = [[execution_date.isoformat(), "1"]]
    return NextSessionEvidence(
        execution_date=execution_date,
        receipt={
            "provider_id": "baostock",
            "provider_kind": "real_provider",
            "adapter_version": "offline-calendar-fixture-v1",
            "request": {
                "start_date": execution_date.isoformat(),
                "end_date": execution_date.isoformat(),
            },
            "fields": ["calendar_date", "is_trading_day"],
            "rows": rows,
            "raw_content_sha256": _digest(
                {
                    "fields": ["calendar_date", "is_trading_day"],
                    "rows": rows,
                }
            ),
        },
    )


def _write_seed(
    path: Path,
    config: dict,
    *,
    state_date: date = FIRST_STRATEGY_DATE,
    cash: str = "4321.09",
    positions: dict[str, int] | None = None,
    position_lots: list[dict[str, object]] | None = None,
    peak_nav: str = "5000.00",
) -> dict:
    positions = dict(positions or {})
    position_lots = deepcopy(position_lots or [])
    state = {
        "state_date": state_date.isoformat(),
        "previous_trading_date": (state_date - timedelta(days=1)).isoformat(),
        "previous_record_sha256": "a" * 64,
        "cash": cash,
        "positions": positions,
        "position_lots": position_lots,
        "sellable_quantities": {
            item: quantity for item, quantity in positions.items() if quantity
        },
        "nav": cash,
        "peak_nav": peak_nav,
        "drawdown": 0.0,
        "exposure_state": "RISK_OFF",
        "pending_state": None,
        "hysteresis_count": 0,
        "cumulative_explicit_fee": "0.00",
        "cumulative_slippage_cost": "0.00",
        "cumulative_transaction_cost": "0.00",
    }
    seed = {
        "schema_version": "technical-shadow-daily-state-seed.v1",
        "strategy_id": "a-share-technical-shadow-mvp-v1",
        "mode": "stateful_daily",
        "shadow_account_id": "technical-shadow-account-v1",
        "config_sha256": _digest(config),
        "state": state,
        "bootstrap": {
            "kind": "offline_controlled_seed",
            "source_record_sha256": state["previous_record_sha256"],
        },
        "safety": DAILY_SAFETY,
    }
    path.write_bytes(_canonical_bytes(seed))
    return seed


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _snapshot(root: Path) -> dict[str, tuple[str, int, int]]:
    return {
        path.relative_to(root).as_posix(): (
            sha256(path.read_bytes()).hexdigest(),
            path.stat().st_mtime_ns,
            path.stat().st_size,
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _ranking(config: dict, *, entry_instrument: str | None) -> list[dict]:
    rows = []
    for index, instrument_id in enumerate(config["universe"]["instrument_ids"]):
        selected = instrument_id == entry_instrument
        rows.append(
            {
                "instrument_id": instrument_id,
                "factors": {},
                "z_scores": {},
                "composite_score": 1.0 if selected else -1.0,
                "rank": index + 1,
                "percentile": 1.0 if selected else 0.0,
                "eligibility": True,
                "entry_eligible": selected,
                "hold_eligible": selected,
                "exclusion_codes": [] if selected else ["below_entry_threshold"],
            }
        )
    return rows


def _exposure(state: str) -> dict[str, object]:
    if state == "RISK_ON":
        trend, breadth, volatility, target = 0.05, 0.75, 0.10, 1.0
    else:
        trend, breadth, volatility, target = -0.05, 0.30, 0.20, 0.0
    return {
        "market_state": state,
        "target_gross_exposure": target,
        "benchmark_trend": trend,
        "market_breadth": breadth,
        "realized_volatility": volatility,
        "account_drawdown": 0.0,
        "data_fail_closed": False,
        "reason_codes": [f"exposure_{state.lower()}"],
    }


class TechnicalShadowDailyAdversarialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = _load_config(CONFIG_PATH)

    def test_same_input_is_byte_idempotent_without_state_or_mtime_change(self):
        # Exercise the first real daily handoff: the controlled replay seed is
        # yesterday's close and no immutable daily plan exists yet.  The runner
        # must carry that state forward without inventing a retrospective fill.
        strategy_date = FIRST_STRATEGY_DATE + timedelta(days=1)
        captured = _captured(self.config, strategy_date)
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            seed_path = temporary_root / "seed.json"
            output_root = temporary_root / "daily"
            _write_seed(seed_path, self.config)

            slot, first = run_daily(
                config=self.config,
                captured=captured,
                execution_evidence=_evidence(strategy_date),
                output_root=output_root,
                seed_path=seed_path,
                allow_test_provider=True,
            )
            before = _snapshot(slot)
            slot_again, second = run_daily(
                config=self.config,
                captured=captured,
                execution_evidence=_evidence(strategy_date),
                output_root=output_root,
                seed_path=seed_path,
                allow_test_provider=True,
            )
            after = _snapshot(slot)

            self.assertEqual(slot_again, slot)
            self.assertEqual(first["status"], "created")
            self.assertEqual(second["status"], "idempotent_existing")
            self.assertFalse(first["idempotent"])
            self.assertTrue(second["idempotent"])
            self.assertEqual(first["manifest_sha256"], second["manifest_sha256"])
            self.assertEqual(after, before)

            state = _json(slot / "state.json")
            decision = _json(slot / "portfolio_decision.json")
            plan = _json(slot / "next_session_plan.json")
            application = _json(slot / "prior_plan_application.json")
            self.assertEqual(self.config["portfolio"]["initial_cash"], 10000)
            self.assertEqual(state["cash"], "4321.09")
            self.assertEqual(decision["current_cash"], "4321.09")
            self.assertEqual(second["current_cash"], "4321.09")
            self.assertEqual(plan["plan_type"], "manual_shadow_plan_not_order")
            self.assertFalse(plan["automatic_order_submission"])
            self.assertFalse(decision["automatic_order_submission"])
            self.assertFalse(_json(slot / "manifest.json")["automatic_order_submission"])
            self.assertEqual(
                application["status"], "NO_PRIOR_PLAN_CASH_CARRY_FORWARD"
            )
            self.assertEqual(state["previous_trading_date"], FIRST_STRATEGY_DATE.isoformat())
            self.assertFalse(
                any("order" in path.name.lower() for path in slot.iterdir())
            )

    def test_calendar_leading_day_is_trimmed_to_latest_benchmark_complete_close(self):
        complete = _captured(self.config, FIRST_STRATEGY_DATE)
        calendar_only_day = FIRST_STRATEGY_DATE + timedelta(days=1)
        calendar_leading = CapturedData(
            provider_id=complete.provider_id,
            provider_kind=complete.provider_kind,
            adapter_version=complete.adapter_version,
            synthetic=complete.synthetic,
            captured_at=complete.captured_at,
            sessions=complete.sessions + (calendar_only_day,),
            stock_rows=complete.stock_rows,
            benchmark_rows=complete.benchmark_rows,
            receipts=complete.receipts,
        )

        trimmed = _latest_data_complete_capture(calendar_leading)

        self.assertEqual(trimmed.sessions, complete.sessions)
        self.assertEqual(trimmed.sessions[-1], FIRST_STRATEGY_DATE)
        self.assertEqual(len(trimmed.sessions), 121)
        self.assertNotIn(calendar_only_day, trimmed.sessions)
        self.assertIs(trimmed.stock_rows, complete.stock_rows)
        self.assertIs(trimmed.benchmark_rows, complete.benchmark_rows)

    def test_same_date_different_market_input_is_immutable_conflict(self):
        captured = _captured(self.config, FIRST_STRATEGY_DATE)
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            seed_path = temporary_root / "seed.json"
            output_root = temporary_root / "daily"
            _write_seed(seed_path, self.config)
            slot, _ = run_daily(
                config=self.config,
                captured=captured,
                execution_evidence=_evidence(FIRST_STRATEGY_DATE),
                output_root=output_root,
                seed_path=seed_path,
                allow_test_provider=True,
            )
            before = _snapshot(slot)

            changed_rows = dict(captured.stock_rows)
            instrument_id = self.config["universe"]["instrument_ids"][0]
            rows = list(changed_rows[instrument_id])
            changed = dict(rows[-1])
            changed["close"] = float(changed["close"]) + 0.25
            rows[-1] = changed
            changed_rows[instrument_id] = tuple(rows)
            changed_capture = CapturedData(
                provider_id=captured.provider_id,
                provider_kind=captured.provider_kind,
                adapter_version=captured.adapter_version,
                synthetic=captured.synthetic,
                captured_at=captured.captured_at,
                sessions=captured.sessions,
                stock_rows=changed_rows,
                benchmark_rows=captured.benchmark_rows,
                receipts=captured.receipts,
            )
            with self.assertRaisesRegex(
                TechnicalShadowDailyError, r"immutable_conflict:"
            ):
                run_daily(
                    config=self.config,
                    captured=changed_capture,
                    execution_evidence=_evidence(FIRST_STRATEGY_DATE),
                    output_root=output_root,
                    seed_path=seed_path,
                    allow_test_provider=True,
                )
            self.assertEqual(_snapshot(slot), before)

    def test_future_available_at_is_rejected_before_slot_creation(self):
        captured = _captured(self.config, FIRST_STRATEGY_DATE)
        instrument_id = self.config["universe"]["instrument_ids"][0]
        stock_rows = dict(captured.stock_rows)
        rows = list(stock_rows[instrument_id])
        future = dict(rows[-1])
        future["available_at"] = (
            f"{FIRST_STRATEGY_DATE.isoformat()}T15:30:01+08:00"
        )
        rows[-1] = future
        stock_rows[instrument_id] = tuple(rows)
        future_capture = CapturedData(
            provider_id=captured.provider_id,
            provider_kind=captured.provider_kind,
            adapter_version=captured.adapter_version,
            synthetic=captured.synthetic,
            captured_at=captured.captured_at,
            sessions=captured.sessions,
            stock_rows=stock_rows,
            benchmark_rows=captured.benchmark_rows,
            receipts=captured.receipts,
        )

        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            seed_path = temporary_root / "seed.json"
            output_root = temporary_root / "daily"
            _write_seed(seed_path, self.config)
            with self.assertRaisesRegex(
                TechnicalShadowDailyError, "future_available_at_rejected"
            ):
                run_daily(
                    config=self.config,
                    captured=future_capture,
                    execution_evidence=_evidence(FIRST_STRATEGY_DATE),
                    output_root=output_root,
                    seed_path=seed_path,
                    allow_test_provider=True,
                )
            self.assertFalse((output_root / FIRST_STRATEGY_DATE.isoformat()).exists())

    def test_tampered_predecessor_state_and_manifest_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            seed_path = temporary_root / "seed.json"
            output_root = temporary_root / "daily"
            _write_seed(seed_path, self.config)
            slot, _ = run_daily(
                config=self.config,
                captured=_captured(self.config, FIRST_STRATEGY_DATE),
                execution_evidence=_evidence(FIRST_STRATEGY_DATE),
                output_root=output_root,
                seed_path=seed_path,
                allow_test_provider=True,
            )

            # Model an adversary/manual edit that rewrites both the state and
            # its entry in the manifest but cannot preserve the immutable
            # manifest's own payload hash.  Merely trusting the rewritten
            # artifact table would turn a checksum into a mutable allow-list.
            state_path = slot / "state.json"
            state = _json(state_path)
            state["cash"] = "9999.99"
            state["nav"] = "9999.99"
            state["record_sha256"] = _digest(
                {key: value for key, value in state.items() if key != "record_sha256"}
            )
            state_raw = _canonical_bytes(state)
            state_path.write_bytes(state_raw)

            manifest_path = slot / "manifest.json"
            manifest = _json(manifest_path)
            manifest["artifacts"]["state.json"] = sha256(state_raw).hexdigest()
            manifest["account_record_sha256"] = state["record_sha256"]
            # Deliberately leave manifest_payload_sha256 unchanged.
            manifest_path.write_bytes(_canonical_bytes(manifest))

            next_day = FIRST_STRATEGY_DATE + timedelta(days=1)
            with self.assertRaisesRegex(
                TechnicalShadowDailyError,
                r"manifest|state|binding|integrity",
            ):
                run_daily(
                    config=self.config,
                    captured=_captured(self.config, next_day),
                    execution_evidence=_evidence(next_day),
                    output_root=output_root,
                    seed_path=seed_path,
                    allow_test_provider=True,
                )
            self.assertFalse((output_root / next_day.isoformat()).exists())

    def test_predecessor_must_be_current_calendars_previous_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            seed_path = temporary_root / "seed.json"
            output_root = temporary_root / "daily"
            _write_seed(seed_path, self.config)
            run_daily(
                config=self.config,
                captured=_captured(self.config, FIRST_STRATEGY_DATE),
                execution_evidence=_evidence(FIRST_STRATEGY_DATE),
                output_root=output_root,
                seed_path=seed_path,
                allow_test_provider=True,
            )

            next_day = FIRST_STRATEGY_DATE + timedelta(days=1)
            changed_calendar = _captured(
                self.config,
                next_day,
                omit_immediately_previous_session=True,
            )
            self.assertNotEqual(
                changed_calendar.sessions[-2], FIRST_STRATEGY_DATE
            )
            with self.assertRaisesRegex(
                TechnicalShadowDailyError,
                r"previous.*session|calendar.*continuity|state.*gap",
            ):
                run_daily(
                    config=self.config,
                    captured=changed_calendar,
                    execution_evidence=_evidence(next_day),
                    output_root=output_root,
                    seed_path=seed_path,
                    allow_test_provider=True,
                )
            self.assertFalse((output_root / next_day.isoformat()).exists())

    def test_cross_day_hash_chain_and_buy_sell_accounting_are_replayable(self):
        first_instrument = self.config["universe"]["instrument_ids"][0]
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            seed_path = temporary_root / "seed.json"
            output_root = temporary_root / "daily"
            _write_seed(
                seed_path,
                self.config,
                cash="10000.00",
                peak_nav="10000.00",
            )

            day0 = FIRST_STRATEGY_DATE
            with patch(
                "operations.run_technical_shadow_daily.rank_technical_alpha_shadow",
                return_value=_ranking(self.config, entry_instrument=first_instrument),
            ), patch(
                "operations.run_technical_shadow_daily.compute_technical_shadow_exposure",
                return_value=_exposure("RISK_ON"),
            ):
                slot0, _ = run_daily(
                    config=self.config,
                    captured=_captured(self.config, day0),
                    execution_evidence=_evidence(day0),
                    output_root=output_root,
                    seed_path=seed_path,
                    allow_test_provider=True,
                )
            state0 = _json(slot0 / "state.json")
            plan0 = _json(slot0 / "next_session_plan.json")
            buy_plan = next(row for row in plan0["actions"] if row["action"] == "BUY")
            self._assert_planned_cost_breakdown(buy_plan, side="BUY")

            day1 = day0 + timedelta(days=1)
            with patch(
                "operations.run_technical_shadow_daily.rank_technical_alpha_shadow",
                return_value=_ranking(self.config, entry_instrument=None),
            ), patch(
                "operations.run_technical_shadow_daily.compute_technical_shadow_exposure",
                return_value=_exposure("RISK_OFF"),
            ):
                slot1, _ = run_daily(
                    config=self.config,
                    captured=_captured(self.config, day1),
                    execution_evidence=_evidence(day1),
                    output_root=output_root,
                    seed_path=seed_path,
                    allow_test_provider=True,
                )
            state1 = _json(slot1 / "state.json")
            manifest1 = _json(slot1 / "manifest.json")
            application1 = _json(slot1 / "prior_plan_application.json")
            plan1 = _json(slot1 / "next_session_plan.json")
            buy_fill = application1["ledger_fills"][0]

            self.assertEqual(state1["previous_trading_date"], day0.isoformat())
            self.assertEqual(state1["previous_record_sha256"], state0["record_sha256"])
            self.assertEqual(plan0["based_on_account_record_sha256"], state0["record_sha256"])
            self.assertEqual(manifest1["account_record_sha256"], state1["record_sha256"])
            self.assertEqual(
                state1["record_sha256"],
                _digest({key: value for key, value in state1.items() if key != "record_sha256"}),
            )
            self._assert_executed_fill_accounting(buy_fill, side="BUY")
            sell_plan = next(row for row in plan1["actions"] if row["action"] == "SELL")
            self._assert_planned_cost_breakdown(sell_plan, side="SELL")
            self.assertEqual(state1["sellable_quantities"], {})

            day2 = day1 + timedelta(days=1)
            with patch(
                "operations.run_technical_shadow_daily.rank_technical_alpha_shadow",
                return_value=_ranking(self.config, entry_instrument=None),
            ), patch(
                "operations.run_technical_shadow_daily.compute_technical_shadow_exposure",
                return_value=_exposure("RISK_OFF"),
            ):
                slot2, _ = run_daily(
                    config=self.config,
                    captured=_captured(self.config, day2),
                    execution_evidence=_evidence(day2),
                    output_root=output_root,
                    seed_path=seed_path,
                    allow_test_provider=True,
                )
            state2 = _json(slot2 / "state.json")
            application2 = _json(slot2 / "prior_plan_application.json")
            sell_fill = application2["ledger_fills"][0]
            self._assert_executed_fill_accounting(sell_fill, side="SELL")
            self.assertEqual(state2["previous_trading_date"], day1.isoformat())
            self.assertEqual(state2["previous_record_sha256"], state1["record_sha256"])
            self.assertEqual(state2["positions"], {})
            self.assertEqual(state2["position_lots"], [])
            expected_cash = (
                Decimal("10000.00")
                + Decimal(buy_fill["cash_delta"])
                + Decimal(sell_fill["cash_delta"])
            )
            self.assertEqual(Decimal(state2["cash"]), expected_cash)

    def _assert_planned_cost_breakdown(self, action: dict, *, side: str) -> None:
        quantity = Decimal(action["quantity"])
        reference_price = Decimal(action["reference_price"])
        reference_notional = Decimal(action["notional_at_reference_price"])
        execution_notional = Decimal(action["notional_at_execution_price"])
        commission = Decimal(action["commission"])
        stamp_duty = Decimal(action["stamp_duty"])
        transfer_fee = Decimal(action["transfer_fee"])
        explicit_fee = Decimal(action["explicit_fee"])
        slippage_cost = Decimal(action["slippage_cost"])
        total_cost = Decimal(action["total_transaction_cost"])
        self.assertEqual(action["action"], side)
        self.assertEqual(reference_notional, reference_price * quantity)
        self.assertEqual(explicit_fee, commission + stamp_duty + transfer_fee)
        self.assertEqual(total_cost, explicit_fee + slippage_cost)
        self.assertEqual(
            slippage_cost, abs(execution_notional - reference_notional)
        )
        if side == "BUY":
            self.assertEqual(
                execution_notional,
                Decimal(action["maximum_buy_price"]) * quantity,
            )
        else:
            self.assertIsNone(action["maximum_buy_price"])

    def _assert_executed_fill_accounting(self, fill: dict, *, side: str) -> None:
        required = {
            "reference_price",
            "execution_price",
            "notional_at_reference_price",
            "notional_at_execution_price",
            "commission",
            "stamp_duty",
            "transfer_fee",
            "explicit_fee",
            "slippage_cost",
            "total_transaction_cost",
            "cash_delta",
        }
        self.assertTrue(required <= set(fill))
        quantity = Decimal(fill["simulated_quantity"])
        reference_notional = Decimal(fill["notional_at_reference_price"])
        execution_notional = Decimal(fill["notional_at_execution_price"])
        explicit_fee = Decimal(fill["explicit_fee"])
        slippage_cost = Decimal(fill["slippage_cost"])
        self.assertEqual(
            reference_notional, Decimal(fill["reference_price"]) * quantity
        )
        self.assertEqual(
            execution_notional, Decimal(fill["execution_price"]) * quantity
        )
        self.assertEqual(
            explicit_fee,
            Decimal(fill["commission"])
            + Decimal(fill["stamp_duty"])
            + Decimal(fill["transfer_fee"]),
        )
        self.assertEqual(
            Decimal(fill["total_transaction_cost"]),
            explicit_fee + slippage_cost,
        )
        expected_cash_delta = (
            -(execution_notional + explicit_fee)
            if side == "BUY"
            else execution_notional - explicit_fee
        )
        self.assertEqual(Decimal(fill["cash_delta"]), expected_cash_delta)
        self.assertNotEqual(
            Decimal(fill["cash_delta"]),
            expected_cash_delta - slippage_cost,
        )


if __name__ == "__main__":
    unittest.main()
