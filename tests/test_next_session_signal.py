from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

import research.strategy_workspace.next_session_signal as next_session_signal_module

from trading.costs import FeeSchedule
from trading.models import (
    AccountSnapshot,
    InstrumentRule,
    MarketQuote,
    PortfolioIntent,
    PortfolioIntentType,
    Position,
)

from research.strategy_workspace.next_session_signal import (
    CalendarRegistryEntry,
    NextSessionAlreadyConsumed,
    NextSessionChannel,
    NextSessionSignalConflict,
    NextSessionSignalError,
    OfficialCalendarReceipt,
    OfficialCalendarRegistry,
    canonical_next_session_consumption_path,
    consume_next_session_signal,
    create_alpha_next_session_signal,
    create_risk_next_session_signal,
    read_next_session_signal,
    write_new_next_session_signal,
)
from research.strategy_workspace.contracts import canonical_json_bytes
from research.strategy_workspace.portfolio_constructor_v2 import (
    ConstructorCostPolicy,
    CurrentPosition,
    PortfolioConstructorPolicy,
    PortfolioInstrument,
    construct_portfolio,
)


D = Decimal
TZ = timezone(timedelta(hours=8))
STRATEGY_ID = "a-share-small-account-adaptive-exposure-v2"
DECISION = datetime(2026, 8, 21, 15, 5, tzinfo=TZ)
EXECUTION_DATE = date(2026, 8, 24)
CHECKED = datetime(2026, 8, 24, 9, 31, tzinfo=TZ)


def policy() -> PortfolioConstructorPolicy:
    return PortfolioConstructorPolicy(
        policy_id="next-session-policy-v1",
        frozen_at=DECISION - timedelta(days=1),
        max_positions=3,
        max_position_weight=D("0.40"),
        entry_percentile_min=D("0.80"),
        hold_percentile_min=D("0.60"),
        no_trade_threshold=D("0"),
        maximum_execution_price_deviation=D("0.02"),
        maximum_quote_age_seconds=300,
        maximum_account_age_seconds=600,
        costs=ConstructorCostPolicy(
            commission_rate=D("0.00018"),
            minimum_commission=D("5"),
            sell_tax_rate=D("0.0005"),
            transfer_fee_rate=D("0.00001"),
            slippage_bps_one_way=D("10"),
        ),
    )


def row(instrument_id: str = "000001.SZ", *, predicted="0.10", percentile="0.99") -> PortfolioInstrument:
    return PortfolioInstrument(
        instrument_id=instrument_id,
        predicted_return=D(predicted),
        percentile=D(percentile),
        eligibility=True,
        exclusion_codes=(),
        reference_price=D("10"),
        lot_size=100,
    )


def canonical_fees() -> FeeSchedule:
    return FeeSchedule(
        commission_rate=D("0.00018"),
        minimum_commission=D("5"),
        exchange_fee_rate=D("0.00001"),
    )


def canonical_rules(*, lot_size: int = 100) -> dict[str, InstrumentRule]:
    return {
        "000001.SZ": InstrumentRule(
            instrument_id="000001.SZ",
            name="controlled-test-instrument",
            instrument_type="stock",
            lot_size=lot_size,
            tick_size=D("0.01"),
            sell_stamp_duty_rate=D("0.0005"),
            t_plus_one=True,
        )
    }


def receipt() -> OfficialCalendarReceipt:
    return OfficialCalendarReceipt(
        receipt_id="sse-szse-calendar-202608",
        adapter_id="official-calendar-adapter",
        adapter_version="v1",
        source_id="exchange-calendar-source",
        source_document_sha256="9" * 64,
        issued_at=DECISION - timedelta(days=2),
        available_at=DECISION - timedelta(days=2, minutes=1),
        trading_sessions=(date(2026, 8, 20), DECISION.date(), EXECUTION_DATE, date(2026, 8, 25)),
    )


def registry(calendar_receipt: OfficialCalendarReceipt) -> OfficialCalendarRegistry:
    return OfficialCalendarRegistry(
        registry_id="controlled-calendar-registry-v1",
        frozen_at=DECISION - timedelta(days=1),
        entries=(CalendarRegistryEntry.from_receipt(calendar_receipt),),
    )


def build_alpha():
    selected_policy = policy()
    construction = construct_portfolio(
        decision_at=DECISION,
        requested_intent_type=PortfolioIntentType.ALPHA_REBALANCE,
        target_gross_exposure=D("0.30"),
        current_cash=D("10000"),
        current_positions=(),
        instruments=(row(),),
        policy=selected_policy,
        input_snapshot_sha256="1" * 64,
        model_sha256="2" * 64,
    )
    intent = PortfolioIntent(
        intent_id="alpha-close-20260821",
        strategy_id=STRATEGY_ID,
        intent_type=construction.intent_type,
        decision_at=DECISION,
        available_at=DECISION - timedelta(minutes=10),
        frozen_at=DECISION - timedelta(minutes=1),
        target_gross_exposure=construction.target_gross_exposure,
        target_weights=construction.feasible_stock_weights,
        reason_codes=("alpha_next_session",),
        signal_sha256=construction.construction_sha256,
        market_data_sha256=construction.input_snapshot_sha256,
        model_sha256=construction.model_sha256,
        risk_state_sha256="4" * 64,
    )
    return selected_policy, construction, intent


def alpha_account(*, cash="10000") -> AccountSnapshot:
    return AccountSnapshot(
        STRATEGY_ID,
        D(cash),
        {},
        snapshot_id="reconciled-d-plus-one",
        as_of=CHECKED - timedelta(minutes=1),
    )


def quote(*, ask="10.10", buy_blocked=False) -> MarketQuote:
    return MarketQuote(
        instrument_id="000001.SZ",
        bid=D("10.00"),
        ask=D(ask),
        last=D("10.05"),
        as_of=CHECKED - timedelta(minutes=1),
        buy_blocked=buy_blocked,
    )


class NextSessionSignalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._registry_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._registry_directory.cleanup)
        self._registry_patch = patch.object(
            next_session_signal_module,
            "NEXT_SESSION_REGISTRY_ROOT",
            Path(self._registry_directory.name) / "fixed-strategy-registry",
        )
        self._registry_patch.start()
        self.addCleanup(self._registry_patch.stop)

    def create_alpha(self):
        selected_policy, construction, intent = build_alpha()
        calendar_receipt = receipt()
        controlled_registry = registry(calendar_receipt)
        signal = create_alpha_next_session_signal(
            intent=intent,
            construction=construction,
            policy=selected_policy,
            receipt=calendar_receipt,
            registry=controlled_registry,
            fees=canonical_fees(),
            instrument_rules=canonical_rules(),
        )
        return selected_policy, construction, intent, calendar_receipt, controlled_registry, signal

    def test_cross_process_json_schemas_parse_and_are_closed(self) -> None:
        schema_dir = Path(__file__).resolve().parents[1] / "schemas"
        expected_versions = {
            "portfolio_constructor_policy.v1.json": "portfolio-constructor-policy.v1",
            "portfolio_construction_result.v2.json": "portfolio-construction-result.v2",
            "official_calendar_receipt.v1.json": "official-calendar-receipt.v1",
            "official_calendar_registry.v1.json": "official-calendar-registry.v1",
            "next_session_signal.v1.json": "next-session-signal.v1",
            "next_session_consumption.v1.json": "next-session-consumption.v1",
        }
        for filename, version in expected_versions.items():
            with self.subTest(filename=filename):
                payload = json.loads((schema_dir / filename).read_text(encoding="utf-8"))
                self.assertEqual(payload["$schema"], "https://json-schema.org/draft/2020-12/schema")
                self.assertFalse(payload["additionalProperties"])
                self.assertEqual(
                    payload["properties"]["schema_version"]["const"],
                    version,
                )

    def test_structured_receipt_requires_exact_registry_allowlist(self) -> None:
        selected_policy, construction, intent = build_alpha()
        calendar_receipt = receipt()
        other_receipt = replace(calendar_receipt, receipt_id="other-calendar-receipt")
        wrong_registry = registry(other_receipt)

        with self.assertRaisesRegex(NextSessionSignalError, "allowlist"):
            create_alpha_next_session_signal(
                intent=intent,
                construction=construction,
                policy=selected_policy,
                receipt=calendar_receipt,
                registry=wrong_registry,
                fees=canonical_fees(),
                instrument_rules=canonical_rules(),
            )
        with self.assertRaisesRegex(NextSessionSignalError, "OfficialCalendarRegistry"):
            create_alpha_next_session_signal(
                intent=intent,
                construction=construction,
                policy=selected_policy,
                receipt=calendar_receipt,
                registry=True,  # type: ignore[arg-type]
                fees=canonical_fees(),
                instrument_rules=canonical_rules(),
            )

    def test_signal_is_byte_idempotent_and_consumed_once_on_adjacent_d_plus_one(self) -> None:
        _, _, _, _, controlled_registry, signal = self.create_alpha()
        self.assertEqual(signal.strategy_date, DECISION.date())
        self.assertEqual(signal.execution_date, EXECUTION_DATE)
        self.assertIs(signal.channel, NextSessionChannel.ALPHA)
        self.assertFalse(signal.to_dict()["automatic_submission"])

        with tempfile.TemporaryDirectory() as folder:
            signal_path = Path(folder) / "signal.json"
            consumed_path = canonical_next_session_consumption_path(
                signal_path, signal.signal_sha256
            )
            write_new_next_session_signal(signal_path, signal)
            original = signal_path.read_bytes()
            self.assertEqual(write_new_next_session_signal(signal_path, signal), signal_path)
            self.assertEqual(signal_path.read_bytes(), original)
            conflict_path = Path(folder) / "conflict-signal.json"
            conflict_path.write_bytes(b"{}\n")
            with self.assertRaisesRegex(NextSessionSignalConflict, "different bytes"):
                write_new_next_session_signal(conflict_path, signal)
            self.assertEqual(signal_path.read_bytes(), original)
            loaded = read_next_session_signal(signal_path, registry=controlled_registry)
            self.assertEqual(loaded.signal_sha256, signal.signal_sha256)

            consumed = consume_next_session_signal(
                signal_path,
                consumed_path,
                registry=controlled_registry,
                account=alpha_account(),
                quotes={"000001.SZ": quote()},
                fees=canonical_fees(),
                instrument_rules=canonical_rules(),
                checked_at=CHECKED,
            )
            self.assertEqual(consumed.status, "READY_FOR_MANUAL_EXECUTION")
            self.assertTrue(consumed.to_dict()["manual_execution_required"])
            self.assertFalse(consumed.to_dict()["automatic_submission"])
            with self.assertRaisesRegex(NextSessionAlreadyConsumed, "already exists"):
                consume_next_session_signal(
                    signal_path,
                    consumed_path,
                    registry=controlled_registry,
                    account=alpha_account(),
                    quotes={"000001.SZ": quote()},
                    fees=canonical_fees(),
                    instrument_rules=canonical_rules(),
                    checked_at=CHECKED,
                )

    def test_one_shot_consumption_path_is_signal_hash_derived(self) -> None:
        _, _, _, _, controlled_registry, signal = self.create_alpha()
        with tempfile.TemporaryDirectory() as folder:
            signal_path = Path(folder) / "signal.json"
            canonical_path = canonical_next_session_consumption_path(
                signal_path, signal.signal_sha256
            )
            alternate_path = Path(folder) / "alternate-consumption.json"
            write_new_next_session_signal(signal_path, signal)
            with self.assertRaisesRegex(NextSessionSignalError, "canonical"):
                consume_next_session_signal(
                    signal_path,
                    alternate_path,
                    registry=controlled_registry,
                    account=alpha_account(),
                    quotes={"000001.SZ": quote()},
                    fees=canonical_fees(),
                    instrument_rules=canonical_rules(),
                    checked_at=CHECKED,
                )
            self.assertFalse(alternate_path.exists())
            consumed = consume_next_session_signal(
                signal_path,
                canonical_path,
                registry=controlled_registry,
                account=alpha_account(),
                quotes={"000001.SZ": quote()},
                fees=canonical_fees(),
                instrument_rules=canonical_rules(),
                checked_at=CHECKED,
            )
            self.assertEqual(consumed.status, "READY_FOR_MANUAL_EXECUTION")
            with self.assertRaises(NextSessionAlreadyConsumed):
                consume_next_session_signal(
                    signal_path,
                    canonical_path,
                    registry=controlled_registry,
                    account=alpha_account(),
                    quotes={"000001.SZ": quote()},
                    fees=canonical_fees(),
                    instrument_rules=canonical_rules(),
                    checked_at=CHECKED,
                )

    def test_cross_directory_publication_shares_one_global_consumption_slot(self) -> None:
        _, _, _, _, controlled_registry, signal = self.create_alpha()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            signal_path = root / "signal.json"
            write_new_next_session_signal(signal_path, signal)
            alias_path = root / "aliases" / "renamed-signal.json"
            alias_path.parent.mkdir()
            alias_path.write_bytes(signal_path.read_bytes())

            canonical_path = canonical_next_session_consumption_path(
                signal_path,
                signal.signal_sha256,
            )
            self.assertEqual(
                canonical_next_session_consumption_path(
                    alias_path,
                    signal.signal_sha256,
                ),
                canonical_path,
            )
            first = consume_next_session_signal(
                signal_path,
                canonical_path,
                registry=controlled_registry,
                account=alpha_account(cash="9999"),
                quotes={"000001.SZ": quote()},
                fees=canonical_fees(),
                instrument_rules=canonical_rules(),
                checked_at=CHECKED,
            )
            self.assertEqual(first.status, "CANCELED")
            with self.assertRaises(NextSessionAlreadyConsumed):
                consume_next_session_signal(
                    alias_path,
                    canonical_path,
                    registry=controlled_registry,
                    account=alpha_account(),
                    quotes={"000001.SZ": quote()},
                    fees=canonical_fees(),
                    instrument_rules=canonical_rules(),
                    checked_at=CHECKED,
                )
            with tempfile.TemporaryDirectory() as outside_folder:
                outside_alias = Path(outside_folder) / "copied-signal.json"
                self.assertEqual(
                    write_new_next_session_signal(outside_alias, signal),
                    outside_alias,
                )
                outside_consumption = canonical_next_session_consumption_path(
                    outside_alias,
                    signal.signal_sha256,
                )
                self.assertEqual(outside_consumption, canonical_path)
                with self.assertRaises(NextSessionAlreadyConsumed):
                    consume_next_session_signal(
                        outside_alias,
                        outside_consumption,
                        registry=controlled_registry,
                        account=alpha_account(),
                        quotes={"000001.SZ": quote()},
                        fees=canonical_fees(),
                        instrument_rules=canonical_rules(),
                        checked_at=CHECKED,
                    )

    def test_concurrent_alias_consumers_have_exactly_one_cas_winner(self) -> None:
        _, _, _, _, controlled_registry, signal = self.create_alpha()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            signal_path = root / "signal.json"
            write_new_next_session_signal(signal_path, signal)
            alias_path = root / "renamed-signal.json"
            alias_path.write_bytes(signal_path.read_bytes())
            canonical_path = canonical_next_session_consumption_path(
                signal_path,
                signal.signal_sha256,
            )
            barrier = Barrier(2)

            def attempt(source: Path) -> str:
                barrier.wait(timeout=5)
                try:
                    consume_next_session_signal(
                        source,
                        canonical_path,
                        registry=controlled_registry,
                        account=alpha_account(),
                        quotes={"000001.SZ": quote()},
                        fees=canonical_fees(),
                        instrument_rules=canonical_rules(),
                        checked_at=CHECKED,
                    )
                except NextSessionAlreadyConsumed:
                    return "lost"
                return "won"

            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(attempt, (signal_path, alias_path)))
            self.assertCountEqual(outcomes, ("won", "lost"))
            persisted = json.loads(canonical_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["signal_id"], signal.signal_id)
            self.assertEqual(persisted["signal_sha256"], signal.signal_sha256)

    def test_wrong_session_is_rejected_without_consuming(self) -> None:
        _, _, _, _, controlled_registry, signal = self.create_alpha()
        with tempfile.TemporaryDirectory() as folder:
            signal_path = Path(folder) / "signal.json"
            consumed_path = canonical_next_session_consumption_path(
                signal_path, signal.signal_sha256
            )
            write_new_next_session_signal(signal_path, signal)
            with self.assertRaisesRegex(NextSessionSignalError, r"bound D\+1"):
                consume_next_session_signal(
                    signal_path,
                    consumed_path,
                    registry=controlled_registry,
                    account=alpha_account(),
                    quotes={"000001.SZ": quote()},
                    fees=canonical_fees(),
                    instrument_rules=canonical_rules(),
                    checked_at=CHECKED + timedelta(days=1),
                )
            self.assertFalse(consumed_path.exists())

    def test_outside_opening_review_window_is_rejected_without_consuming(self) -> None:
        _, _, _, _, controlled_registry, signal = self.create_alpha()
        early_check = CHECKED.replace(hour=9, minute=24)
        early_account = replace(
            alpha_account(),
            as_of=early_check - timedelta(minutes=1),
        )
        early_quote = replace(
            quote(),
            as_of=early_check - timedelta(minutes=1),
        )
        with tempfile.TemporaryDirectory() as folder:
            signal_path = Path(folder) / "signal.json"
            consumed_path = canonical_next_session_consumption_path(
                signal_path, signal.signal_sha256
            )
            write_new_next_session_signal(signal_path, signal)
            with self.assertRaisesRegex(NextSessionSignalError, "09:25-09:35"):
                consume_next_session_signal(
                    signal_path,
                    consumed_path,
                    registry=controlled_registry,
                    account=early_account,
                    quotes={"000001.SZ": early_quote},
                    fees=canonical_fees(),
                    instrument_rules=canonical_rules(),
                    checked_at=early_check,
                )
            self.assertFalse(consumed_path.exists())

    def test_buy_above_frozen_deviation_is_canceled_and_never_submitted(self) -> None:
        _, _, _, _, controlled_registry, signal = self.create_alpha()
        with tempfile.TemporaryDirectory() as folder:
            signal_path = Path(folder) / "signal.json"
            consumed_path = canonical_next_session_consumption_path(
                signal_path, signal.signal_sha256
            )
            write_new_next_session_signal(signal_path, signal)
            consumed = consume_next_session_signal(
                signal_path,
                consumed_path,
                registry=controlled_registry,
                account=alpha_account(),
                quotes={"000001.SZ": quote(ask="10.21")},
                fees=canonical_fees(),
                instrument_rules=canonical_rules(),
                checked_at=CHECKED,
            )
            self.assertEqual(consumed.status, "CANCELED")
            buy = next(item for item in consumed.instructions if item.action == "BUY")
            self.assertEqual(buy.status.value, "CANCELED")
            self.assertIn("buy_price_above_frozen_deviation_limit", buy.cancel_conditions)
            self.assertEqual(consumed.to_dict()["execution_authority"], "none")

    def test_account_state_mismatch_is_persisted_as_canceled_consumption(self) -> None:
        _, _, _, _, controlled_registry, signal = self.create_alpha()
        with tempfile.TemporaryDirectory() as folder:
            signal_path = Path(folder) / "signal.json"
            consumed_path = canonical_next_session_consumption_path(
                signal_path, signal.signal_sha256
            )
            write_new_next_session_signal(signal_path, signal)
            consumed = consume_next_session_signal(
                signal_path,
                consumed_path,
                registry=controlled_registry,
                account=alpha_account(cash="9999"),
                quotes={"000001.SZ": quote()},
                fees=canonical_fees(),
                instrument_rules=canonical_rules(),
                checked_at=CHECKED,
            )
            self.assertEqual(consumed.status, "CANCELED")
            self.assertIn("account_state_mismatch", consumed.cancel_reasons)
            self.assertTrue(consumed_path.exists())

    def test_d_plus_one_rule_bundle_is_rehashed_and_buy_lot_is_rechecked(self) -> None:
        _, _, _, _, controlled_registry, signal = self.create_alpha()
        with tempfile.TemporaryDirectory() as folder:
            signal_path = Path(folder) / "signal.json"
            consumed_path = canonical_next_session_consumption_path(
                signal_path, signal.signal_sha256
            )
            write_new_next_session_signal(signal_path, signal)
            consumed = consume_next_session_signal(
                signal_path,
                consumed_path,
                registry=controlled_registry,
                account=alpha_account(),
                quotes={"000001.SZ": quote()},
                fees=canonical_fees(),
                instrument_rules=canonical_rules(lot_size=300),
                checked_at=CHECKED,
            )
            buy = next(item for item in consumed.instructions if item.action == "BUY")
            self.assertEqual(consumed.status, "CANCELED")
            self.assertNotEqual(
                consumed.execution_rule_bundle_sha256,
                signal.execution_rule_bundle_sha256,
            )
            self.assertIn("execution_rule_bundle_mismatch", buy.cancel_conditions)
            self.assertIn(
                "buy_quantity_not_whole_lot_under_d_plus_one_rule",
                buy.cancel_conditions,
            )

    def test_signal_creation_rejects_fee_bundle_drift(self) -> None:
        selected_policy, construction, intent = build_alpha()
        calendar_receipt = receipt()
        controlled_registry = registry(calendar_receipt)
        drifted_fees = FeeSchedule(D("0.00020"), D("5"), D("0.00001"))
        with self.assertRaisesRegex(NextSessionSignalError, "commission_rate"):
            create_alpha_next_session_signal(
                intent=intent,
                construction=construction,
                policy=selected_policy,
                receipt=calendar_receipt,
                registry=controlled_registry,
                fees=drifted_fees,
                instrument_rules=canonical_rules(),
            )

    def test_risk_exit_cannot_use_alpha_adapter_and_contains_no_buy(self) -> None:
        selected_policy = policy()
        construction = construct_portfolio(
            decision_at=DECISION,
            requested_intent_type=PortfolioIntentType.DEFENSIVE_REDUCTION,
            target_gross_exposure=D("0.30"),
            current_cash=D("5000"),
            current_positions=(CurrentPosition("000001.SZ", 500),),
            instruments=(row(predicted="-0.10", percentile="0.10"),),
            policy=selected_policy,
            input_snapshot_sha256="1" * 64,
            model_sha256="2" * 64,
        )
        intent = PortfolioIntent(
            intent_id="defensive-close-20260821",
            strategy_id=STRATEGY_ID,
            intent_type=construction.intent_type,
            decision_at=DECISION,
            available_at=DECISION - timedelta(minutes=10),
            frozen_at=DECISION - timedelta(minutes=1),
            target_gross_exposure=construction.target_gross_exposure,
            target_weights=construction.feasible_stock_weights,
            reason_codes=("defensive_reduction",),
            signal_sha256=construction.construction_sha256,
            market_data_sha256=construction.input_snapshot_sha256,
            model_sha256=construction.model_sha256,
            risk_state_sha256="4" * 64,
        )
        calendar_receipt = receipt()
        controlled_registry = registry(calendar_receipt)
        with self.assertRaisesRegex(NextSessionSignalError, "not permitted"):
            create_alpha_next_session_signal(
                intent=intent,
                construction=construction,
                policy=selected_policy,
                receipt=calendar_receipt,
                registry=controlled_registry,
                fees=canonical_fees(),
                instrument_rules=canonical_rules(),
            )
        signal = create_risk_next_session_signal(
            intent=intent,
            construction=construction,
            policy=selected_policy,
            receipt=calendar_receipt,
            registry=controlled_registry,
            fees=canonical_fees(),
            instrument_rules=canonical_rules(),
        )
        self.assertIs(signal.channel, NextSessionChannel.RISK_REDUCTION)
        self.assertFalse(any(item["action"] == "BUY" for item in signal.to_dict()["construction"]["actions"]))

    def test_tampered_signal_bytes_fail_hash_or_canonical_verification(self) -> None:
        _, _, _, _, controlled_registry, signal = self.create_alpha()
        with tempfile.TemporaryDirectory() as folder:
            signal_path = Path(folder) / "signal.json"
            write_new_next_session_signal(signal_path, signal)
            payload = json.loads(signal_path.read_text(encoding="utf-8"))
            payload["execution_date"] = "2026-08-25"
            signal_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(NextSessionSignalError):
                read_next_session_signal(signal_path, registry=controlled_registry)

    def test_self_rehashed_alpha_buy_cannot_masquerade_as_risk_signal(self) -> None:
        _, _, _, _, controlled_registry, signal = self.create_alpha()
        attacked = replace(signal, channel=NextSessionChannel.RISK_REDUCTION)
        with tempfile.TemporaryDirectory() as folder:
            signal_path = Path(folder) / "attacked-signal.json"
            with self.assertRaisesRegex(
                NextSessionSignalError, "channel|risk signal"
            ):
                write_new_next_session_signal(signal_path, attacked)
            self.assertFalse(signal_path.exists())

            signal_path.write_bytes(canonical_json_bytes(attacked.to_dict()) + b"\n")
            with self.assertRaisesRegex(
                NextSessionSignalError, "channel|risk signal"
            ):
                read_next_session_signal(signal_path, registry=controlled_registry)


if __name__ == "__main__":
    unittest.main()
