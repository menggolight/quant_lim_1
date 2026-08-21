from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from research.strategy_workspace.contracts import canonical_json_bytes, canonical_sha256
from research.strategy_workspace.adaptive_exposure import (
    FROZEN_ADAPTIVE_EXPOSURE_POLICY_SHA256,
)
from research.strategy_workspace.paper_ledger_v2 import (
    ADAPTIVE_POLICY_SCHEMA_VERSION,
    ADAPTIVE_STRATEGY_ID,
    PAPER_LEDGER_V2_VERSION,
    CanonicalExecutionCostBundleV1,
    ControlledCloseMarkBundleV1,
    PaperCloseExecutionEvidenceV1,
    PaperDailySessionDraftV2,
    PaperExecutionAttemptV2,
    PaperLedgerV2Error,
    PaperPositionMarkV2,
    append_paper_daily_session_v2,
    create_or_verify_paper_ledger_v2,
    verify_paper_ledger_v2,
)
from trading.costs import FeeSchedule
from trading.models import InstrumentRule, PortfolioIntent, PortfolioIntentType


CST = timezone(timedelta(hours=8))
POLICY_SHA256 = FROZEN_ADAPTIVE_EXPOSURE_POLICY_SHA256
FEES = FeeSchedule(
    commission_rate=Decimal("0.00018"),
    minimum_commission=Decimal("5"),
    exchange_fee_rate=Decimal("0.00001"),
)
RULES = {
    instrument_id: InstrumentRule(
        instrument_id=instrument_id,
        name=instrument_id,
        instrument_type="stock",
        lot_size=100,
        tick_size=Decimal("0.01"),
        sell_stamp_duty_rate=Decimal("0.0005"),
        t_plus_one=True,
    )
    for instrument_id in ("600000.SH", "600001.SH")
}
COST_BUNDLE = CanonicalExecutionCostBundleV1(FEES, RULES)


def at(day: date, hour: int, minute: int) -> datetime:
    return datetime.combine(day, time(hour, minute), CST)


def intent(
    intent_id: str,
    decision_day: date,
    intent_type: PortfolioIntentType,
    target: str,
    weights: dict[str, Decimal] | None = None,
) -> PortfolioIntent:
    digest_seed = sha256(intent_id.encode("utf-8")).hexdigest()
    return PortfolioIntent(
        intent_id=intent_id,
        strategy_id=ADAPTIVE_STRATEGY_ID,
        intent_type=intent_type,
        decision_at=at(decision_day, 15, 5),
        available_at=at(decision_day, 14, 50),
        frozen_at=at(decision_day, 15, 1),
        target_gross_exposure=Decimal(target),
        target_weights=weights or {},
        reason_codes=(intent_type.value.lower(),),
        signal_sha256=digest_seed,
        market_data_sha256="b" * 64,
        model_sha256="c" * 64,
        risk_state_sha256="d" * 64,
    )


def attempt(
    attempt_id: str,
    execution_intent: PortfolioIntent,
    day: date,
    *,
    instrument_id: str = "600000.SH",
    side: str,
    requested: int,
    filled: int,
    status: str,
    reference_open: str,
    fill_price: str | None,
    blocked_reason: str | None = None,
) -> PaperExecutionAttemptV2:
    return PaperExecutionAttemptV2(
        attempt_id=attempt_id,
        intent_id=execution_intent.intent_id,
        intent_sha256=execution_intent.intent_sha256,
        instrument_id=instrument_id,
        side=side,
        status=status,
        requested_quantity=requested,
        filled_quantity=filled,
        execution_session=day,
        attempted_at=at(day, 9, 30),
        reference_open=Decimal(reference_open),
        fill_price=None if fill_price is None else Decimal(fill_price),
        evidence_sha256=sha256(
            f"evidence:{attempt_id}:{instrument_id}".encode("utf-8")
        ).hexdigest(),
        execution_cost_bundle_sha256=COST_BUNDLE.cost_bundle_sha256,
        commission_rate=FEES.commission_rate,
        minimum_commission=FEES.minimum_commission,
        sell_tax_rate=RULES[instrument_id].sell_stamp_duty_rate,
        transfer_fee_rate=FEES.exchange_fee_rate,
        blocked_reason=blocked_reason,
    )


def mark(
    quantity: int,
    close_price: str,
    *,
    instrument_id: str = "600000.SH",
) -> PaperPositionMarkV2:
    return PaperPositionMarkV2(
        instrument_id=instrument_id,
        quantity=quantity,
        close_price=Decimal(close_price),
        price_source_sha256=sha256(
            f"mark:{instrument_id}:{quantity}:{close_price}".encode("utf-8")
        ).hexdigest(),
    )


def draft(
    day: date,
    execution_intent: PortfolioIntent,
    *,
    closing_intent: PortfolioIntent | None = None,
    attempts: tuple[PaperExecutionAttemptV2, ...] = (),
    positions: tuple[PaperPositionMarkV2, ...] = (),
    cost_bundle: CanonicalExecutionCostBundleV1 = COST_BUNDLE,
) -> PaperDailySessionDraftV2:
    receipt = sha256(f"close-receipt:{day}".encode("utf-8")).hexdigest()
    close_bundle = ControlledCloseMarkBundleV1.from_close_prices(
        session_date=day,
        observed_at=at(day, 15, 0),
        available_at=at(day, 15, 10),
        source="controlled-test-close",
        source_receipt_sha256=receipt,
        position_closes={
            item.instrument_id: (item.quantity, item.close_price)
            for item in positions
        },
    )
    execution_evidence = PaperCloseExecutionEvidenceV1(
        signal_id=f"signal:{day}",
        signal_sha256=sha256(f"signal:{day}".encode("utf-8")).hexdigest(),
        consumption_sha256=sha256(
            f"consumption:{day}".encode("utf-8")
        ).hexdigest(),
        fill_bundle_sha256=sha256(
            f"fill-bundle:{day}".encode("utf-8")
        ).hexdigest(),
        frozen_execution_rule_bundle_sha256=(
            cost_bundle.execution_rule_bundle_sha256
        ),
        review_execution_rule_bundle_sha256=(
            cost_bundle.execution_rule_bundle_sha256
        ),
        execution_cost_bundle_sha256=cost_bundle.cost_bundle_sha256,
        execution_intent_sha256=execution_intent.intent_sha256,
    )
    return PaperDailySessionDraftV2(
        trading_date=day,
        execution_intent=execution_intent,
        closing_intent=closing_intent or execution_intent,
        attempts=attempts,
        execution_cost_bundle=cost_bundle,
        close_mark_bundle=close_bundle,
        execution_evidence=execution_evidence,
    )


class PaperLedgerV2Tests(unittest.TestCase):
    def create(self, path: Path, calendar: tuple[date, ...], created_at: datetime) -> None:
        with patch(
            "research.strategy_workspace.paper_ledger_v2._now",
            return_value=created_at,
        ):
            created = create_or_verify_paper_ledger_v2(
                path,
                strategy_id=ADAPTIVE_STRATEGY_ID,
                policy_schema_version=ADAPTIVE_POLICY_SCHEMA_VERSION,
                policy_sha256=POLICY_SHA256,
                controlled_trading_dates=calendar,
            )
        self.assertEqual(created.daily_sessions, ())

    def append(
        self,
        path: Path,
        session: PaperDailySessionDraftV2,
        recorded_at: datetime,
    ):
        with patch(
            "research.strategy_workspace.paper_ledger_v2._now",
            return_value=recorded_at,
        ):
            return append_paper_daily_session_v2(path, session)

    def seed_position_and_trigger(
        self,
        path: Path,
        calendar: tuple[date, ...],
    ) -> tuple[PortfolioIntent, PortfolioIntent]:
        alpha = intent(
            "alpha-before-start",
            calendar[0] - timedelta(days=1),
            PortfolioIntentType.ALPHA_REBALANCE,
            "0.30",
            {"600000.SH": Decimal("0.30")},
        )
        buy = attempt(
            "buy-1",
            alpha,
            calendar[0],
            side="BUY",
            requested=300,
            filled=300,
            status="FILLED",
            reference_open="10",
            fill_price="10.01",
        )
        self.append(
            path,
            draft(calendar[0], alpha, attempts=(buy,), positions=(mark(300, "10"),)),
            at(calendar[0], 15, 30),
        )
        exit_intent = intent(
            "drawdown-exit",
            calendar[1],
            PortfolioIntentType.ACCOUNT_DRAWDOWN_EXIT,
            "0",
        )
        triggered = self.append(
            path,
            draft(
                calendar[1],
                alpha,
                closing_intent=exit_intent,
                positions=(mark(300, "5"),),
            ),
            at(calendar[1], 15, 30),
        )
        self.assertTrue(triggered.daily_sessions[-1]["risk_latched"])
        return alpha, exit_intent

    def test_header_binds_strategy_policy_calendar_and_external_schema(self) -> None:
        calendar = (date(2026, 8, 24), date(2026, 8, 25))
        self.assertEqual(
            ADAPTIVE_POLICY_SCHEMA_VERSION,
            "strategy-adaptive-exposure-policy.v2",
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "paper-v2.jsonl"
            self.create(path, calendar, at(date(2026, 8, 21), 16, 0))
            verified = verify_paper_ledger_v2(
                path, as_of=at(date(2026, 8, 21), 16, 1)
            )
            self.assertEqual(verified.header["strategy_id"], ADAPTIVE_STRATEGY_ID)
            self.assertEqual(verified.header["policy_sha256"], POLICY_SHA256)
            self.assertEqual(
                verified.header["controlled_calendar_sha256"],
                canonical_sha256(calendar),
            )
            with self.assertRaisesRegex(PaperLedgerV2Error, "frozen repository policy"):
                with patch(
                    "research.strategy_workspace.paper_ledger_v2._now",
                    return_value=at(date(2026, 8, 21), 16, 1),
                ):
                    create_or_verify_paper_ledger_v2(
                        path,
                        strategy_id=ADAPTIVE_STRATEGY_ID,
                        policy_schema_version=ADAPTIVE_POLICY_SCHEMA_VERSION,
                        policy_sha256="e" * 64,
                        controlled_trading_dates=calendar,
                    )
            with self.assertRaisesRegex(
                PaperLedgerV2Error,
                "policy_schema_version must remain",
            ):
                create_or_verify_paper_ledger_v2(
                    Path(folder) / "old-policy-paper-v2.jsonl",
                    strategy_id=ADAPTIVE_STRATEGY_ID,
                    policy_schema_version="strategy-adaptive-exposure-policy.v1",
                    policy_sha256=POLICY_SHA256,
                    controlled_trading_dates=calendar,
                )

        schema_path = (
            Path(__file__).resolve().parents[1]
            / "schemas"
            / "strategy_paper_ledger_record.v2.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(schema["$id"], "strategy_paper_ledger_record.v2.json")
        self.assertEqual(len(schema["oneOf"]), 2)
        self.assertEqual(
            schema["$defs"]["headerContent"]["properties"]["schema_version"]["const"],
            PAPER_LEDGER_V2_VERSION,
        )
        self.assertEqual(
            schema["$defs"]["headerContent"]["properties"][
                "policy_schema_version"
            ]["const"],
            ADAPTIVE_POLICY_SCHEMA_VERSION,
        )
        daily_contract = schema["$defs"]["dailySessionContent"]
        self.assertIn("execution_intent", daily_contract["required"])
        self.assertIn("closing_intent", daily_contract["required"])
        self.assertIn("execution_cost_bundle", daily_contract["required"])
        self.assertIn("close_mark_bundle", daily_contract["required"])
        self.assertIn("execution_evidence", daily_contract["required"])
        self.assertNotIn("active_intent", daily_contract["properties"])

    def test_real_fills_recompute_cash_cost_nav_and_three_exposures(self) -> None:
        day = date(2026, 8, 24)
        calendar = (day,)
        alpha = intent(
            "alpha-1",
            day - timedelta(days=1),
            PortfolioIntentType.ALPHA_REBALANCE,
            "0.30",
            {"600000.SH": Decimal("0.30")},
        )
        buy = attempt(
            "buy-accounting",
            alpha,
            day,
            side="BUY",
            requested=300,
            filled=300,
            status="FILLED",
            reference_open="10",
            fill_price="10.01",
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "paper-v2.jsonl"
            self.create(path, calendar, at(day - timedelta(days=1), 16, 0))
            verified = self.append(
                path,
                draft(day, alpha, attempts=(buy,), positions=(mark(300, "10"),)),
                at(day, 15, 30),
            )
            session = verified.daily_sessions[-1]
            self.assertEqual(session["cash"], "6991.97")
            self.assertEqual(session["strategy_positions_value"], "3000.00")
            self.assertEqual(session["strategy_nav"], "9991.97")
            self.assertEqual(session["session_transaction_cost"], "8.03")
            self.assertEqual(session["target_gross_exposure"], "0.30000000")
            expected_realized = (
                Decimal("3000") / Decimal("9991.97")
            ).quantize(Decimal("0.00000001"))
            self.assertEqual(
                Decimal(session["realized_gross_exposure"]), expected_realized
            )
            self.assertEqual(
                session["feasible_gross_exposure"],
                "0.30000000",
            )
            self.assertNotEqual(
                session["feasible_gross_exposure"],
                session["realized_gross_exposure"],
            )
            self.assertFalse(session["risk_latched"])

    def test_alpha_open_can_execute_before_close_drawdown_creates_d_plus_one_exit(self) -> None:
        d1, d2, d3 = date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26)
        calendar = (d1, d2, d3)
        first_alpha = intent(
            "alpha-seed",
            d1 - timedelta(days=1),
            PortfolioIntentType.ALPHA_REBALANCE,
            "0.30",
            {"600000.SH": Decimal("0.30")},
        )
        second_alpha = intent(
            "alpha-executed-on-trigger-day",
            d1,
            PortfolioIntentType.ALPHA_REBALANCE,
            "0.40",
            {
                "600000.SH": Decimal("0.30"),
                "600001.SH": Decimal("0.10"),
            },
        )
        closing_exit = intent(
            "close-created-drawdown-exit",
            d2,
            PortfolioIntentType.ACCOUNT_DRAWDOWN_EXIT,
            "0",
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "paper-v2.jsonl"
            self.create(path, calendar, at(d1 - timedelta(days=1), 16, 0))
            seed_buy = attempt(
                "seed-buy",
                first_alpha,
                d1,
                side="BUY",
                requested=300,
                filled=300,
                status="FILLED",
                reference_open="10",
                fill_price="10.01",
            )
            self.append(
                path,
                draft(
                    d1,
                    first_alpha,
                    closing_intent=second_alpha,
                    attempts=(seed_buy,),
                    positions=(mark(300, "10"),),
                ),
                at(d1, 15, 30),
            )

            wrongly_bound = attempt(
                "wrongly-bound-trigger-day-buy",
                closing_exit,
                d2,
                instrument_id="600001.SH",
                side="BUY",
                requested=100,
                filled=100,
                status="FILLED",
                reference_open="10",
                fill_price="10",
            )
            with self.assertRaisesRegex(PaperLedgerV2Error, "differs from execution_intent"):
                self.append(
                    path,
                    draft(
                        d2,
                        second_alpha,
                        closing_intent=closing_exit,
                        attempts=(wrongly_bound,),
                        positions=(
                            mark(300, "5"),
                            mark(100, "5", instrument_id="600001.SH"),
                        ),
                    ),
                    at(d2, 15, 30),
                )

            trigger_day_buy = attempt(
                "alpha-trigger-day-buy",
                second_alpha,
                d2,
                instrument_id="600001.SH",
                side="BUY",
                requested=100,
                filled=100,
                status="FILLED",
                reference_open="10",
                fill_price="10",
            )
            triggered = self.append(
                path,
                draft(
                    d2,
                    second_alpha,
                    closing_intent=closing_exit,
                    attempts=(trigger_day_buy,),
                    positions=(
                        mark(300, "5"),
                        mark(100, "5", instrument_id="600001.SH"),
                    ),
                ),
                at(d2, 15, 30),
            ).daily_sessions[-1]
            self.assertEqual(triggered["execution_intent"]["intent_id"], second_alpha.intent_id)
            self.assertEqual(triggered["closing_intent"]["intent_id"], closing_exit.intent_id)
            self.assertEqual(triggered["attempts"][0]["intent_id"], second_alpha.intent_id)
            self.assertEqual(triggered["target_gross_exposure"], "0.00000000")
            self.assertTrue(triggered["risk_latched"])
            self.assertTrue(triggered["exit_pending"])

            first_exit = attempt(
                "forced-exit-d-plus-one",
                closing_exit,
                d3,
                side="SELL",
                requested=300,
                filled=300,
                status="FILLED",
                reference_open="5",
                fill_price="4.99",
            )
            second_exit = attempt(
                "forced-exit-d-plus-one",
                closing_exit,
                d3,
                instrument_id="600001.SH",
                side="SELL",
                requested=100,
                filled=100,
                status="FILLED",
                reference_open="5",
                fill_price="4.99",
            )
            flat = self.append(
                path,
                draft(
                    d3,
                    closing_exit,
                    attempts=(first_exit, second_exit),
                ),
                at(d3, 15, 30),
            ).daily_sessions[-1]
            self.assertTrue(flat["risk_latched"])
            self.assertFalse(flat["exit_pending"])
            self.assertEqual(flat["positions"], [])

    def test_daily_sessions_are_contiguous_same_day_and_never_backfilled(self) -> None:
        d1, d2 = date(2026, 8, 24), date(2026, 8, 25)
        calendar = (d1, d2)
        cash_intent = intent(
            "cash",
            d1 - timedelta(days=1),
            PortfolioIntentType.NO_ALPHA_CASH,
            "0",
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "paper-v2.jsonl"
            self.create(path, calendar, at(d1 - timedelta(days=1), 16, 0))
            with self.assertRaisesRegex(PaperLedgerV2Error, "next controlled"):
                self.append(path, draft(d2, cash_intent), at(d2, 15, 30))
            with self.assertRaisesRegex(PaperLedgerV2Error, "without backfill"):
                self.append(path, draft(d1, cash_intent), at(d2, 15, 30))
            self.append(path, draft(d1, cash_intent), at(d1, 15, 30))
            with self.assertRaisesRegex(PaperLedgerV2Error, "without backfill"):
                self.append(path, draft(d2, cash_intent), at(d2, 14, 59))

    def test_drawdown_latch_preserves_real_exposure_and_retries_until_flat(self) -> None:
        calendar = tuple(date(2026, 8, 24) + timedelta(days=index) for index in range(5))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "paper-v2.jsonl"
            self.create(path, calendar, at(calendar[0] - timedelta(days=1), 16, 0))
            _, exit_intent = self.seed_position_and_trigger(path, calendar)
            triggered = verify_paper_ledger_v2(
                path, as_of=at(calendar[1], 15, 31)
            ).daily_sessions[-1]
            self.assertEqual(triggered["target_gross_exposure"], "0.00000000")
            self.assertGreater(Decimal(triggered["realized_gross_exposure"]), 0)
            self.assertTrue(triggered["exit_pending"])

            blocked = attempt(
                "exit-day-1",
                exit_intent,
                calendar[2],
                side="SELL",
                requested=300,
                filled=0,
                status="UNFILLED",
                reference_open="5",
                fill_price=None,
                blocked_reason="limit_down_locked",
            )
            after_block = self.append(
                path,
                draft(
                    calendar[2],
                    exit_intent,
                    attempts=(blocked,),
                    positions=(mark(300, "4.5"),),
                ),
                at(calendar[2], 15, 30),
            ).daily_sessions[-1]
            self.assertTrue(after_block["risk_latched"])
            self.assertGreater(Decimal(after_block["realized_gross_exposure"]), 0)
            self.assertEqual(
                after_block["blocked_exit_reasons"],
                [
                    {
                        "attempt_id": "exit-day-1",
                        "instrument_id": "600000.SH",
                        "reason": "limit_down_locked",
                        "residual_quantity": 300,
                    }
                ],
            )

            filled = attempt(
                "exit-day-2",
                exit_intent,
                calendar[3],
                side="SELL",
                requested=300,
                filled=300,
                status="FILLED",
                reference_open="4.5",
                fill_price="4.49",
            )
            flat = self.append(
                path,
                draft(calendar[3], exit_intent, attempts=(filled,)),
                at(calendar[3], 15, 30),
            ).daily_sessions[-1]
            self.assertTrue(flat["risk_latched"])
            self.assertEqual(flat["risk_trigger_date"], calendar[1].isoformat())
            self.assertEqual(flat["realized_gross_exposure"], "0.00000000")
            self.assertEqual(flat["positions"], [])
            self.assertFalse(flat["exit_pending"])

            forbidden_reentry = attempt(
                "post-flat-buy",
                exit_intent,
                calendar[4],
                instrument_id="600001.SH",
                side="BUY",
                requested=100,
                filled=100,
                status="FILLED",
                reference_open="10",
                fill_price="10",
            )
            with self.assertRaisesRegex(PaperLedgerV2Error, "BUY attempts are forbidden"):
                self.append(
                    path,
                    draft(
                        calendar[4],
                        exit_intent,
                        attempts=(forbidden_reentry,),
                        positions=(
                            mark(100, "10", instrument_id="600001.SH"),
                        ),
                    ),
                    at(calendar[4], 15, 30),
                )
            still_frozen = self.append(
                path,
                draft(calendar[4], exit_intent),
                at(calendar[4], 15, 30),
            ).daily_sessions[-1]
            self.assertTrue(still_frozen["risk_latched"])
            self.assertFalse(still_frozen["exit_pending"])

    def test_missing_daily_retry_is_rejected_while_residual_position_exists(self) -> None:
        calendar = tuple(date(2026, 9, 1) + timedelta(days=index) for index in range(3))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "paper-v2.jsonl"
            self.create(path, calendar, at(calendar[0] - timedelta(days=1), 16, 0))
            _, exit_intent = self.seed_position_and_trigger(path, calendar)
            with self.assertRaisesRegex(PaperLedgerV2Error, "every residual position"):
                self.append(
                    path,
                    draft(calendar[2], exit_intent, positions=(mark(300, "4"),)),
                    at(calendar[2], 15, 30),
                )

    def test_buy_is_forbidden_after_the_drawdown_latch(self) -> None:
        calendar = tuple(date(2026, 9, 7) + timedelta(days=index) for index in range(3))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "paper-v2.jsonl"
            self.create(path, calendar, at(calendar[0] - timedelta(days=1), 16, 0))
            _, exit_intent = self.seed_position_and_trigger(path, calendar)
            illegal_buy = attempt(
                "illegal-buy",
                exit_intent,
                calendar[2],
                instrument_id="600001.SH",
                side="BUY",
                requested=100,
                filled=100,
                status="FILLED",
                reference_open="10",
                fill_price="10",
            )
            with self.assertRaisesRegex(PaperLedgerV2Error, "BUY attempts are forbidden"):
                self.append(
                    path,
                    draft(
                        calendar[2],
                        exit_intent,
                        attempts=(illegal_buy,),
                        positions=(mark(300, "5"), mark(100, "10", instrument_id="600001.SH")),
                    ),
                    at(calendar[2], 15, 30),
                )

    def test_attempt_id_replay_is_rejected_instead_of_duplicating_accounting(self) -> None:
        d1, d2 = date(2026, 9, 14), date(2026, 9, 15)
        calendar = (d1, d2)
        alpha = intent(
            "alpha-replay",
            d1 - timedelta(days=1),
            PortfolioIntentType.ALPHA_REBALANCE,
            "0.30",
            {"600000.SH": Decimal("0.30")},
        )
        first = attempt(
            "same-attempt",
            alpha,
            d1,
            side="BUY",
            requested=300,
            filled=300,
            status="FILLED",
            reference_open="10",
            fill_price="10.01",
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "paper-v2.jsonl"
            self.create(path, calendar, at(d1 - timedelta(days=1), 16, 0))
            self.append(
                path,
                draft(d1, alpha, attempts=(first,), positions=(mark(300, "10"),)),
                at(d1, 15, 30),
            )
            replay = attempt(
                "same-attempt",
                alpha,
                d2,
                side="SELL",
                requested=300,
                filled=0,
                status="UNFILLED",
                reference_open="10",
                fill_price=None,
                blocked_reason="suspended",
            )
            with self.assertRaisesRegex(PaperLedgerV2Error, "attempt replay"):
                self.append(
                    path,
                    draft(d2, alpha, attempts=(replay,), positions=(mark(300, "10"),)),
                    at(d2, 15, 30),
                )

    def test_one_portfolio_attempt_can_bind_multiple_instrument_orders(self) -> None:
        day = date(2026, 9, 18)
        alpha = intent(
            "alpha-two-names",
            day - timedelta(days=1),
            PortfolioIntentType.ALPHA_REBALANCE,
            "0.60",
            {"600000.SH": Decimal("0.30"), "600001.SH": Decimal("0.30")},
        )
        first = attempt(
            "portfolio-attempt-1",
            alpha,
            day,
            instrument_id="600000.SH",
            side="BUY",
            requested=200,
            filled=200,
            status="FILLED",
            reference_open="10",
            fill_price="10.01",
        )
        second = attempt(
            "portfolio-attempt-1",
            alpha,
            day,
            instrument_id="600001.SH",
            side="BUY",
            requested=200,
            filled=200,
            status="FILLED",
            reference_open="10",
            fill_price="10.01",
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "paper-v2.jsonl"
            self.create(path, (day,), at(day - timedelta(days=1), 16, 0))
            verified = self.append(
                path,
                draft(
                    day,
                    alpha,
                    attempts=(first, second),
                    positions=(
                        mark(200, "10", instrument_id="600000.SH"),
                        mark(200, "10", instrument_id="600001.SH"),
                    ),
                ),
                at(day, 15, 30),
            )
            self.assertEqual(len(verified.daily_sessions[-1]["attempts"]), 2)
            self.assertEqual(
                {item["attempt_id"] for item in verified.daily_sessions[-1]["attempts"]},
                {"portfolio-attempt-1"},
            )

    def test_attempt_with_a_different_intent_id_is_rejected(self) -> None:
        day = date(2026, 9, 19)
        alpha = intent(
            "bound-alpha",
            day - timedelta(days=1),
            PortfolioIntentType.ALPHA_REBALANCE,
            "0.30",
            {"600000.SH": Decimal("0.30")},
        )
        unbound = replace(
            attempt(
                "unbound-attempt",
                alpha,
                day,
                side="BUY",
                requested=300,
                filled=300,
                status="FILLED",
                reference_open="10",
                fill_price="10.01",
            ),
            intent_id="different-intent",
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "paper-v2.jsonl"
            self.create(path, (day,), at(day - timedelta(days=1), 16, 0))
            with self.assertRaisesRegex(PaperLedgerV2Error, "intent_id differs"):
                self.append(
                    path,
                    draft(
                        day,
                        alpha,
                        attempts=(unbound,),
                        positions=(mark(300, "10"),),
                    ),
                    at(day, 15, 30),
                )

    def test_costs_are_recomputed_from_bound_bundle_not_module_constants(self) -> None:
        day = date(2026, 9, 20)
        alpha = intent(
            "bound-cost-alpha",
            day - timedelta(days=1),
            PortfolioIntentType.ALPHA_REBALANCE,
            "0.30",
            {"600000.SH": Decimal("0.30")},
        )
        alternate_fees = FeeSchedule(
            commission_rate=Decimal("0.01"),
            minimum_commission=Decimal("0"),
            exchange_fee_rate=Decimal("0.00001"),
        )
        alternate_bundle = CanonicalExecutionCostBundleV1(
            alternate_fees,
            RULES,
        )
        buy = replace(
            attempt(
                "alternate-cost-buy",
                alpha,
                day,
                side="BUY",
                requested=300,
                filled=300,
                status="FILLED",
                reference_open="10",
                fill_price="10.01",
            ),
            execution_cost_bundle_sha256=alternate_bundle.cost_bundle_sha256,
            commission_rate=alternate_fees.commission_rate,
            minimum_commission=alternate_fees.minimum_commission,
            transfer_fee_rate=alternate_fees.exchange_fee_rate,
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "paper-v2.jsonl"
            self.create(path, (day,), at(day - timedelta(days=1), 16, 0))
            session = self.append(
                path,
                draft(
                    day,
                    alpha,
                    attempts=(buy,),
                    positions=(mark(300, "10"),),
                    cost_bundle=alternate_bundle,
                ),
                at(day, 15, 30),
            ).daily_sessions[-1]
        self.assertEqual(session["attempts"][0]["commission"], "30.03")
        self.assertEqual(session["session_transaction_cost"], "33.06")
        self.assertEqual(
            session["execution_cost_bundle"]["fee_schedule"]["commission_rate"],
            "0.01",
        )

    def test_attempt_rate_drift_from_bound_cost_bundle_is_rejected(self) -> None:
        day = date(2026, 9, 20)
        alpha = intent(
            "rate-drift-alpha",
            day - timedelta(days=1),
            PortfolioIntentType.ALPHA_REBALANCE,
            "0.30",
            {"600000.SH": Decimal("0.30")},
        )
        buy = replace(
            attempt(
                "rate-drift-buy",
                alpha,
                day,
                side="BUY",
                requested=300,
                filled=300,
                status="FILLED",
                reference_open="10",
                fill_price="10.01",
            ),
            commission_rate=Decimal("0"),
        )
        with self.assertRaisesRegex(PaperLedgerV2Error, "fee rates differ"):
            draft(
                day,
                alpha,
                attempts=(buy,),
                positions=(mark(300, "10"),),
            )

    def test_sell_cost_includes_commission_tax_transfer_and_slippage(self) -> None:
        day = date(2026, 9, 20)
        exit_intent = intent(
            "complete-sell-cost",
            day - timedelta(days=1),
            PortfolioIntentType.RISK_OFF,
            "0",
        )
        sell = attempt(
            "complete-sell-cost-attempt",
            exit_intent,
            day,
            side="SELL",
            requested=300,
            filled=300,
            status="FILLED",
            reference_open="10",
            fill_price="9.99",
        )
        COST_BUNDLE.validate_attempt(sell)
        self.assertEqual(sell.commission, Decimal("5.00"))
        self.assertEqual(sell.sell_tax, Decimal("1.50"))
        self.assertEqual(sell.transfer_fee, Decimal("0.03"))
        self.assertEqual(sell.slippage_cost, Decimal("3.00"))
        self.assertEqual(sell.total_cost, Decimal("9.53"))

    def test_close_marks_require_receipt_bound_prices_and_timely_availability(self) -> None:
        day = date(2026, 9, 20)
        receipt = sha256(b"controlled-close-receipt").hexdigest()
        with self.assertRaisesRegex(PaperLedgerV2Error, "does not bind the close receipt"):
            ControlledCloseMarkBundleV1(
                session_date=day,
                observed_at=at(day, 15, 0),
                available_at=at(day, 15, 10),
                source="controlled-test-close",
                source_receipt_sha256=receipt,
                positions=(mark(100, "10"),),
            )

        alpha = intent(
            "late-close-alpha",
            day - timedelta(days=1),
            PortfolioIntentType.NO_ALPHA_CASH,
            "0",
        )
        late_bundle = ControlledCloseMarkBundleV1.from_close_prices(
            session_date=day,
            observed_at=at(day, 15, 0),
            available_at=at(day, 15, 40),
            source="controlled-test-close",
            source_receipt_sha256=receipt,
            position_closes={},
        )
        normal = draft(day, alpha)
        late_draft = replace(normal, close_mark_bundle=late_bundle)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "paper-v2.jsonl"
            self.create(path, (day,), at(day - timedelta(days=1), 16, 0))
            with self.assertRaisesRegex(PaperLedgerV2Error, "unavailable"):
                self.append(path, late_draft, at(day, 15, 30))

    def test_rehashed_zero_exposure_claim_is_rejected_by_semantic_replay(self) -> None:
        day = date(2026, 9, 21)
        alpha = intent(
            "alpha-tamper",
            day - timedelta(days=1),
            PortfolioIntentType.ALPHA_REBALANCE,
            "0.30",
            {"600000.SH": Decimal("0.30")},
        )
        buy = attempt(
            "tamper-buy",
            alpha,
            day,
            side="BUY",
            requested=300,
            filled=300,
            status="FILLED",
            reference_open="10",
            fill_price="10.01",
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "paper-v2.jsonl"
            self.create(path, (day,), at(day - timedelta(days=1), 16, 0))
            self.append(
                path,
                draft(day, alpha, attempts=(buy,), positions=(mark(300, "10"),)),
                at(day, 15, 30),
            )
            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            records[1]["content"]["realized_gross_exposure"] = "0.00000000"
            records[1]["record_sha256"] = canonical_sha256(
                {
                    "record_type": records[1]["record_type"],
                    "previous_record_sha256": records[1]["previous_record_sha256"],
                    "content": records[1]["content"],
                }
            )
            path.write_bytes(
                b"".join(canonical_json_bytes(record) + b"\n" for record in records)
            )
            with self.assertRaisesRegex(PaperLedgerV2Error, "semantic replay"):
                verify_paper_ledger_v2(path, as_of=at(day, 15, 31))


if __name__ == "__main__":
    unittest.main()
