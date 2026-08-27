from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from operations.run_technical_shadow_daily import (
    DAILY_SAFETY,
    NextSessionEvidence,
    ReadinessResult,
    TechnicalShadowDailyError,
    _latest_data_complete_capture,
    check_baostock_readiness,
    initialize_persistent_state,
    run_daily,
    wait_until_ready,
)
from operations.run_technical_shadow_mvp import (
    CHINA_TZ,
    CapturedData,
    _canonical_bytes,
    _digest,
    _load_config,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "configs" / "a_share_technical_shadow_mvp.v1.json"
FIRST_STRATEGY_DATE = date(2026, 8, 25)
WORKSPACE_TMP = ROOT / ".tmp"


class _FakeResult:
    def __init__(
        self,
        fields: tuple[str, ...] = (),
        rows: tuple[tuple[str, ...], ...] = (),
        *,
        error_code: str = "0",
        error_msg: str = "",
    ) -> None:
        self.fields = list(fields)
        self.rows = [list(row) for row in rows]
        self.error_code = error_code
        self.error_msg = error_msg
        self._index = -1

    def next(self) -> bool:
        self._index += 1
        return self._index < len(self.rows)

    def get_row_data(self) -> list[str]:
        return self.rows[self._index]


class _ReadinessBaoStock:
    def __init__(
        self,
        *,
        calendar_rows: tuple[tuple[str, str], ...],
        benchmark_rows: tuple[tuple[str, str, str, str], ...],
    ) -> None:
        self.calendar_rows = calendar_rows
        self.benchmark_rows = benchmark_rows
        self.login_calls = 0
        self.logout_calls = 0
        self.calendar_calls: list[dict[str, object]] = []
        self.history_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def login(self) -> _FakeResult:
        self.login_calls += 1
        return _FakeResult()

    def logout(self) -> _FakeResult:
        self.logout_calls += 1
        return _FakeResult()

    def query_trade_dates(self, **kwargs: object) -> _FakeResult:
        self.calendar_calls.append(dict(kwargs))
        return _FakeResult(
            ("calendar_date", "is_trading_day"), self.calendar_rows
        )

    def query_history_k_data_plus(
        self, *args: object, **kwargs: object
    ) -> _FakeResult:
        self.history_calls.append((args, dict(kwargs)))
        return _FakeResult(
            ("date", "code", "close", "tradestatus"), self.benchmark_rows
        )


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
                8.0 + offset * 0.08,
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


def _write_legacy_slot(
    temporary_root: Path,
    config: dict,
    *,
    cash: str = "4321.09",
    positions: dict[str, int] | None = None,
    position_lots: list[dict[str, object]] | None = None,
    peak_nav: str = "5000.00",
    plan_status: str = "NO_ACTION_CASH",
    target_positions: dict[str, int] | None = None,
) -> Path:
    source_slot = temporary_root / "legacy" / FIRST_STRATEGY_DATE.isoformat()
    source_slot.mkdir(parents=True)
    seed = _write_seed(
        temporary_root / "seed.json",
        config,
        cash=cash,
        positions=positions,
        position_lots=position_lots,
        peak_nav=peak_nav,
    )
    application = {
        "status": "BOOTSTRAP_ALREADY_VALUED_CLOSE",
        "decision_date": (FIRST_STRATEGY_DATE - timedelta(days=1)).isoformat(),
        "execution_date": FIRST_STRATEGY_DATE.isoformat(),
        "fills": [],
        "reason_codes": ["controlled_fixture_handoff"],
    }
    state_base = {
        **seed["state"],
        "prior_plan_application_sha256": _digest(application),
        "safety": DAILY_SAFETY,
    }
    state = dict(state_base)
    state["record_sha256"] = _digest(state_base)
    targets = dict(target_positions or {})
    cancelled = plan_status.startswith("CANCELLED_")
    actions = (
        [
            {
                "action": "BUY_CANCELLED",
                "instrument_id": instrument_id,
                "quantity": 0,
                "target_quantity": quantity,
                "reason_codes": ["missed_d_plus_1_open_cutoff_no_retrospective_plan"],
            }
            for instrument_id, quantity in sorted(targets.items())
        ]
        if cancelled
        else [
            {
                "action": "CASH",
                "instrument_id": None,
                "quantity": 0,
                "reason_codes": ["residual_cash_preserved"],
            }
        ]
    )
    zero_cost = {
        "commission": "0.00",
        "stamp_duty": "0.00",
        "transfer_fee": "0.00",
        "explicit_fee": "0.00",
        "slippage_cost": "0.00",
        "total_transaction_cost": "0.00",
    }
    plan_base = {
        "schema_version": "technical-shadow-next-session-plan.v1",
        "strategy_id": "a-share-technical-shadow-mvp-v1",
        "shadow_account_id": "technical-shadow-account-v1",
        "mode": "stateful_daily",
        "plan_type": "manual_shadow_plan_not_order",
        "plan_status": plan_status,
        "execution_window_status": "MISSED" if cancelled else "OPEN",
        "decision_date": FIRST_STRATEGY_DATE.isoformat(),
        "execution_date": (FIRST_STRATEGY_DATE + timedelta(days=1)).isoformat(),
        "valid_only_for_execution_date": (
            FIRST_STRATEGY_DATE + timedelta(days=1)
        ).isoformat(),
        "based_on_account_record_sha256": state["record_sha256"],
        "target_gross_exposure": 0.0,
        "selected_instruments": sorted(targets),
        "target_positions": targets,
        "actions": actions,
        "cost_summary": zero_cost,
        "no_trade_reason_codes": ["RISK_OFF_CASH"],
        "automatic_order_submission": False,
        "safety": DAILY_SAFETY,
    }
    plan = dict(plan_base)
    plan["plan_payload_sha256"] = _digest(plan_base)
    payloads = {
        "state.json": state,
        "next_session_plan.json": plan,
        "prior_plan_application.json": application,
        "previous_state.json": seed["state"],
        "data_receipt.json": {"kind": "controlled_test_fixture"},
        "ranking.json": {"rows": []},
        "exposure.json": {"final_state": "RISK_OFF"},
        "portfolio_decision.json": {"actions": actions},
        "daily_report.md": "# controlled test fixture\n",
    }
    artifacts = {
        name: sha256(_canonical_bytes(payload)).hexdigest()
        for name, payload in sorted(payloads.items())
    }
    manifest_base = {
        "schema_version": "technical-shadow-daily-manifest.v1",
        "strategy_id": "a-share-technical-shadow-mvp-v1",
        "shadow_account_id": "technical-shadow-account-v1",
        "mode": "stateful_daily",
        "strategy_date": FIRST_STRATEGY_DATE.isoformat(),
        "execution_date": (FIRST_STRATEGY_DATE + timedelta(days=1)).isoformat(),
        "config_sha256": _digest(config),
        "predecessor": {"kind": "controlled_test_fixture"},
        "account_record_sha256": state["record_sha256"],
        "artifacts": artifacts,
        "historical_pit_csi800": False,
        "automatic_order_submission": False,
        "safety": DAILY_SAFETY,
    }
    manifest = dict(manifest_base)
    manifest["manifest_payload_sha256"] = _digest(manifest_base)
    for name, payload in payloads.items():
        (source_slot / name).write_bytes(_canonical_bytes(payload))
    (source_slot / "manifest.json").write_bytes(_canonical_bytes(manifest))
    return source_slot


def _initialize_roots(
    temporary_root: Path, config: dict, **legacy_kwargs: object
) -> tuple[Path, Path, Path]:
    source_slot = _write_legacy_slot(temporary_root, config, **legacy_kwargs)
    state_root = temporary_root / "persistent"
    report_root = temporary_root / "reports"
    initialize_persistent_state(
        source_slot=source_slot, state_root=state_root, config=config
    )
    return source_slot, state_root, report_root


def _generated_at(strategy_date: date) -> datetime:
    return datetime(
        strategy_date.year,
        strategy_date.month,
        strategy_date.day,
        16,
        0,
        tzinfo=CHINA_TZ,
    )


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
        # Exercise the first forward day after the explicit controlled-slot
        # migration.  Re-running it must reuse the original generated_at.
        strategy_date = FIRST_STRATEGY_DATE + timedelta(days=1)
        captured = _captured(self.config, strategy_date)
        with tempfile.TemporaryDirectory(dir=WORKSPACE_TMP) as temporary:
            temporary_root = Path(temporary)
            _, state_root, report_root = _initialize_roots(
                temporary_root, self.config
            )

            slot, first = run_daily(
                config=self.config,
                captured=captured,
                execution_evidence=_evidence(strategy_date),
                state_root=state_root,
                report_root=report_root,
                generated_at=_generated_at(strategy_date),
                allow_test_provider=True,
            )
            before = _snapshot(slot)
            report_before = _snapshot(report_root / strategy_date.isoformat())
            slot_again, second = run_daily(
                config=self.config,
                captured=captured,
                execution_evidence=_evidence(strategy_date),
                state_root=state_root,
                report_root=report_root,
                generated_at=_generated_at(strategy_date) + timedelta(hours=1),
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
            self.assertEqual(
                _snapshot(report_root / strategy_date.isoformat()), report_before
            )
            self.assertEqual(first["generated_at"], second["generated_at"])

            state = _json(slot / "state.json")
            decision = _json(
                report_root / strategy_date.isoformat() / "portfolio_decision.json"
            )
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
            self.assertEqual(application["status"], "APPLIED")
            self.assertEqual(application["fills"], [])
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
        strategy_date = FIRST_STRATEGY_DATE + timedelta(days=1)
        captured = _captured(self.config, strategy_date)
        with tempfile.TemporaryDirectory(dir=WORKSPACE_TMP) as temporary:
            temporary_root = Path(temporary)
            _, state_root, report_root = _initialize_roots(
                temporary_root, self.config
            )
            slot, _ = run_daily(
                config=self.config,
                captured=captured,
                execution_evidence=_evidence(strategy_date),
                state_root=state_root,
                report_root=report_root,
                generated_at=_generated_at(strategy_date),
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
                    execution_evidence=_evidence(strategy_date),
                    state_root=state_root,
                    report_root=report_root,
                    generated_at=_generated_at(strategy_date),
                    allow_test_provider=True,
                )
            self.assertEqual(_snapshot(slot), before)

    def test_future_available_at_is_rejected_before_slot_creation(self):
        strategy_date = FIRST_STRATEGY_DATE + timedelta(days=1)
        captured = _captured(self.config, strategy_date)
        instrument_id = self.config["universe"]["instrument_ids"][0]
        stock_rows = dict(captured.stock_rows)
        rows = list(stock_rows[instrument_id])
        future = dict(rows[-1])
        future["available_at"] = (
            f"{strategy_date.isoformat()}T15:30:01+08:00"
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

        with tempfile.TemporaryDirectory(dir=WORKSPACE_TMP) as temporary:
            temporary_root = Path(temporary)
            _, state_root, report_root = _initialize_roots(
                temporary_root, self.config
            )
            with self.assertRaisesRegex(
                TechnicalShadowDailyError, "future_available_at_rejected"
            ):
                run_daily(
                    config=self.config,
                    captured=future_capture,
                    execution_evidence=_evidence(strategy_date),
                    state_root=state_root,
                    report_root=report_root,
                    generated_at=_generated_at(strategy_date),
                    allow_test_provider=True,
                )
            self.assertFalse((state_root / strategy_date.isoformat()).exists())

    def test_tampered_predecessor_state_and_manifest_fail_closed(self):
        with tempfile.TemporaryDirectory(dir=WORKSPACE_TMP) as temporary:
            temporary_root = Path(temporary)
            _, state_root, report_root = _initialize_roots(
                temporary_root, self.config
            )
            first_day = FIRST_STRATEGY_DATE + timedelta(days=1)
            slot, _ = run_daily(
                config=self.config,
                captured=_captured(self.config, first_day),
                execution_evidence=_evidence(first_day),
                state_root=state_root,
                report_root=report_root,
                generated_at=_generated_at(first_day),
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

            next_day = first_day + timedelta(days=1)
            with self.assertRaisesRegex(
                TechnicalShadowDailyError,
                r"manifest|state|binding|integrity",
            ):
                run_daily(
                    config=self.config,
                    captured=_captured(self.config, next_day),
                    execution_evidence=_evidence(next_day),
                    state_root=state_root,
                    report_root=report_root,
                    generated_at=_generated_at(next_day),
                    allow_test_provider=True,
                )
            self.assertFalse((state_root / next_day.isoformat()).exists())

    def test_predecessor_must_be_current_calendars_previous_session(self):
        with tempfile.TemporaryDirectory(dir=WORKSPACE_TMP) as temporary:
            temporary_root = Path(temporary)
            _, state_root, report_root = _initialize_roots(
                temporary_root, self.config
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
                    state_root=state_root,
                    report_root=report_root,
                    generated_at=_generated_at(next_day),
                    allow_test_provider=True,
                )
            self.assertFalse((state_root / next_day.isoformat()).exists())

    def test_cross_day_hash_chain_and_buy_sell_accounting_are_replayable(self):
        first_instrument = self.config["universe"]["instrument_ids"][0]
        with tempfile.TemporaryDirectory(dir=WORKSPACE_TMP) as temporary:
            temporary_root = Path(temporary)
            _, state_root, report_root = _initialize_roots(
                temporary_root,
                self.config,
                cash="10000.00",
                peak_nav="10000.00",
            )

            day0 = FIRST_STRATEGY_DATE + timedelta(days=1)
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
                    state_root=state_root,
                    report_root=report_root,
                    generated_at=_generated_at(day0),
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
                    state_root=state_root,
                    report_root=report_root,
                    generated_at=_generated_at(day1),
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
                    state_root=state_root,
                    report_root=report_root,
                    generated_at=_generated_at(day2),
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

    def test_explicit_migration_copies_hashes_and_is_idempotent(self):
        with tempfile.TemporaryDirectory(dir=WORKSPACE_TMP) as temporary:
            temporary_root = Path(temporary)
            source = _write_legacy_slot(temporary_root, self.config)
            source_before = _snapshot(source)
            state_root = temporary_root / "persistent"
            target, first = initialize_persistent_state(
                source_slot=source, state_root=state_root, config=self.config
            )
            target_before = _snapshot(target)
            target_again, second = initialize_persistent_state(
                source_slot=source, state_root=state_root, config=self.config
            )
            self.assertEqual(first["status"], "initialized")
            self.assertEqual(second["status"], "already_initialized")
            self.assertEqual(target_again, target)
            self.assertEqual(_snapshot(source), source_before)
            self.assertEqual(_snapshot(target), target_before)
            for name in (
                "state.json", "next_session_plan.json",
                "prior_plan_application.json",
            ):
                self.assertEqual((target / name).read_bytes(), (source / name).read_bytes())
            self.assertEqual(
                _json(target / "lineage.json")["source_manifest_sha256"],
                sha256((source / "manifest.json").read_bytes()).hexdigest(),
            )

    def test_cancelled_legacy_plan_is_never_executed(self):
        instrument_id = self.config["universe"]["instrument_ids"][0]
        strategy_date = FIRST_STRATEGY_DATE + timedelta(days=1)
        with tempfile.TemporaryDirectory(dir=WORKSPACE_TMP) as temporary:
            temporary_root = Path(temporary)
            _, state_root, report_root = _initialize_roots(
                temporary_root,
                self.config,
                plan_status="CANCELLED_MISSED_D_PLUS_1_OPEN_CUTOFF",
                target_positions={instrument_id: 100},
            )
            slot, _ = run_daily(
                config=self.config,
                captured=_captured(self.config, strategy_date),
                execution_evidence=_evidence(strategy_date),
                state_root=state_root,
                report_root=report_root,
                generated_at=_generated_at(strategy_date),
                allow_test_provider=True,
            )
            state = _json(slot / "state.json")
            application = _json(slot / "prior_plan_application.json")
            self.assertEqual(application["status"], "NOT_APPLIED_CANCELLED_PLAN")
            self.assertEqual(application["fills"], [])
            self.assertEqual(state["positions"], {})
            self.assertEqual(state["cash"], "4321.09")

    def test_missed_session_carry_forward_preserves_account_and_allows_current_plan(self):
        strategy_date = FIRST_STRATEGY_DATE + timedelta(days=2)
        execution_date = strategy_date + timedelta(days=1)
        missed_date = FIRST_STRATEGY_DATE + timedelta(days=1)
        captured = _captured(self.config, strategy_date)
        ready_close = next(
            row["close"] for row in captured.benchmark_rows
            if row["trading_date"] == strategy_date.isoformat()
        )
        readiness = {
            "strategy_date": strategy_date.isoformat(),
            "execution_date": execution_date.isoformat(),
            "benchmark_candidate_close": str(ready_close),
            "skipped_completed_sessions": [missed_date.isoformat()],
        }
        with tempfile.TemporaryDirectory(dir=WORKSPACE_TMP) as temporary:
            temporary_root = Path(temporary)
            _, state_root, report_root = _initialize_roots(
                temporary_root, self.config,
                cash="10000.00", peak_nav="10000.00",
            )
            source_state = _json(
                state_root / FIRST_STRATEGY_DATE.isoformat() / "state.json"
            )
            source_snapshot = _snapshot(
                state_root / FIRST_STRATEGY_DATE.isoformat()
            )
            slot, _ = run_daily(
                config=self.config, captured=captured,
                execution_evidence=NextSessionEvidence(
                    execution_date=execution_date, receipt={},
                    execution_window_status="OPEN",
                ),
                state_root=state_root, report_root=report_root,
                generated_at=_generated_at(strategy_date),
                readiness_receipt=readiness,
                allow_test_provider=True,
            )
            self.assertFalse((state_root / missed_date.isoformat()).exists())
            self.assertFalse((report_root / missed_date.isoformat()).exists())
            self.assertEqual(
                _snapshot(state_root / FIRST_STRATEGY_DATE.isoformat()),
                source_snapshot,
            )
            application = _json(slot / "prior_plan_application.json")
            lineage = _json(slot / "lineage.json")
            state = _json(slot / "state.json")
            plan = _json(slot / "next_session_plan.json")
            self.assertEqual(
                application["status"],
                "MISSED_SESSION_CARRY_FORWARD",
            )
            self.assertEqual(application["missed_session_date"], "2026-08-26")
            self.assertEqual(application["orders"], [])
            self.assertEqual(application["fills"], [])
            self.assertEqual(application["ledger_fills"], [])
            self.assertFalse(application["forward_evidence"])
            self.assertTrue(application["state_carry_forward"])
            self.assertTrue(application["generated_late"])
            self.assertEqual(application["opening_cash"], "10000.00")
            self.assertEqual(
                application["closing_cash_after_open_execution"], "10000.00"
            )
            self.assertEqual(application["opening_positions"], {})
            self.assertEqual(application["closing_positions_after_open_execution"], {})
            self.assertEqual(application["opening_nav"], "10000.00")
            self.assertEqual(
                application["closing_nav_before_current_close"], "10000.00"
            )
            self.assertEqual(
                application["transaction_summary"]["cash_delta"], "0.00"
            )
            self.assertEqual(
                lineage["skipped_trading_dates"],
                [missed_date.isoformat()],
            )
            self.assertEqual(lineage["previous_state_date"], "2026-08-25")
            self.assertEqual(
                lineage["previous_record_sha256"], source_state["record_sha256"]
            )
            self.assertEqual(state["previous_trading_date"], "2026-08-25")
            self.assertEqual(
                state["previous_record_sha256"], source_state["record_sha256"]
            )
            self.assertEqual(state["cash"], "10000.00")
            self.assertEqual(state["positions"], {})
            self.assertEqual(state["nav"], "10000.00")
            self.assertEqual(plan["decision_date"], "2026-08-27")
            self.assertEqual(plan["execution_date"], "2026-08-28")
            self.assertEqual(plan["execution_window_status"], "OPEN")
            self.assertEqual(
                plan["based_on_account_record_sha256"], state["record_sha256"]
            )
            self.assertLess(
                datetime.fromisoformat(plan["generated_at"]),
                datetime.fromisoformat(plan["execution_open_at"]),
            )
            self.assertFalse(plan["automatic_order_submission"])

    def test_lightweight_readiness_has_three_states_and_no_stock_queries(self):
        calendar = (
            ("2026-08-25", "1"), ("2026-08-26", "1"),
            ("2026-08-27", "1"),
        )
        checked_at = datetime(2026, 8, 26, 16, 0, tzinfo=CHINA_TZ)

        def check(rows: tuple[tuple[str, str, str, str], ...]) -> tuple[ReadinessResult, _ReadinessBaoStock]:
            sdk = _ReadinessBaoStock(
                calendar_rows=calendar, benchmark_rows=rows
            )
            result = check_baostock_readiness(
                state_date=FIRST_STRATEGY_DATE,
                state_record_sha256="a" * 64,
                benchmark_id="000906.SH",
                now=checked_at,
                sdk_loader=lambda _name: sdk,
            )
            return result, sdk

        not_ready, sdk0 = check((("2026-08-25", "sh.000906", "4000", "1"),))
        ready, sdk1 = check((
            ("2026-08-25", "sh.000906", "4000", "1"),
            ("2026-08-26", "sh.000906", "4010", "1"),
        ))
        no_new_sdk = _ReadinessBaoStock(
            calendar_rows=(("2026-08-25", "1"), ("2026-08-26", "1")),
            benchmark_rows=(("2026-08-25", "sh.000906", "4000", "1"),),
        )
        already = check_baostock_readiness(
            state_date=FIRST_STRATEGY_DATE,
            state_record_sha256="a" * 64,
            benchmark_id="000906.SH",
            now=datetime(2026, 8, 26, 10, 0, tzinfo=CHINA_TZ),
            sdk_loader=lambda _name: no_new_sdk,
        )
        gap_sdk = _ReadinessBaoStock(
            calendar_rows=calendar + (("2026-08-28", "1"),),
            benchmark_rows=(
                ("2026-08-25", "sh.000906", "4000", "1"),
                ("2026-08-26", "sh.000906", "4010", "1"),
                ("2026-08-27", "sh.000906", "4020", "1"),
            ),
        )
        flat_gap_ready = check_baostock_readiness(
            state_date=FIRST_STRATEGY_DATE,
            state_record_sha256="a" * 64,
            benchmark_id="000906.SH",
            now=datetime(2026, 8, 27, 16, 0, tzinfo=CHINA_TZ),
            allow_flat_cash_gap=True,
            sdk_loader=lambda _name: gap_sdk,
        )
        self.assertEqual(not_ready.status, "DATA_NOT_READY")
        self.assertEqual(ready.status, "DATA_READY")
        self.assertEqual(ready.strategy_date, date(2026, 8, 26))
        self.assertEqual(ready.execution_date, date(2026, 8, 27))
        self.assertEqual(already.status, "ALREADY_PROCESSED")
        self.assertEqual(flat_gap_ready.status, "DATA_READY")
        self.assertEqual(flat_gap_ready.strategy_date, date(2026, 8, 27))
        self.assertEqual(flat_gap_ready.execution_date, date(2026, 8, 28))
        for sdk in (sdk0, sdk1, no_new_sdk, gap_sdk):
            self.assertEqual(len(sdk.calendar_calls), 1)
            self.assertEqual(len(sdk.history_calls), 1)
            self.assertEqual(sdk.history_calls[0][0][0], "sh.000906")
            self.assertEqual(
                sdk.history_calls[0][0][1], "date,code,close,tradestatus"
            )

    def test_when_ready_is_bounded_and_deadline_wins_over_late_ready(self):
        current = [datetime(2026, 8, 26, 15, 0, tzinfo=CHINA_TZ)]
        sleeps: list[float] = []

        def clock() -> datetime:
            return current[0]

        def sleeper(seconds: float) -> None:
            sleeps.append(seconds)
            current[0] += timedelta(seconds=seconds)

        def result(status: str) -> ReadinessResult:
            return ReadinessResult(
                status=status, state_date=FIRST_STRATEGY_DATE,
                latest_completed_trading_date=FIRST_STRATEGY_DATE,
                latest_benchmark_date=FIRST_STRATEGY_DATE,
                strategy_date=None, execution_date=None,
                checked_at=current[0], deadline_at=None,
                reason_codes=("test",), receipt={},
            )

        bounded = wait_until_ready(
            check=lambda: result("ALREADY_PROCESSED"),
            deadline=current[0] + timedelta(minutes=10),
            poll_interval_seconds=60, max_polls=3,
            clock=clock, sleeper=sleeper,
        )
        self.assertEqual(bounded.status, "DATA_NOT_READY")
        self.assertEqual(sleeps, [60.0, 60.0])
        self.assertIn("max_polls_reached", bounded.reason_codes)

        current[0] = datetime(2026, 8, 26, 16, 0, tzinfo=CHINA_TZ)
        deadline = current[0] + timedelta(seconds=1)

        def late_ready() -> ReadinessResult:
            current[0] += timedelta(seconds=2)
            return result("DATA_READY")

        late = wait_until_ready(
            check=late_ready, deadline=deadline,
            poll_interval_seconds=1, max_polls=2,
            clock=clock, sleeper=sleeper,
        )
        self.assertEqual(late.status, "DATA_NOT_READY")
        self.assertIn("deadline_reached", late.reason_codes)

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
