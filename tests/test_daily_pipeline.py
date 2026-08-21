from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from operations.daily_pipeline import (
    DailyHoldV1,
    DailyOrderV1,
    DailyPipelineError,
    DailyStrategyDecisionV2,
    render_daily_decision_markdown,
    write_daily_decision,
)


SHA = "a" * 64


def decision(*, strategy_date: date = date(2026, 8, 21)) -> DailyStrategyDecisionV2:
    buy = DailyOrderV1(
        instrument_id="000001.SZ",
        side="BUY",
        quantity=100,
        reference_price=Decimal("10.00"),
        target_weight=Decimal("0.30"),
        maximum_execution_price_deviation=Decimal("0.02"),
        cancel_conditions=("buy_price_above_frozen_limit", "account_fingerprint_changed"),
    )
    hold = DailyHoldV1(
        instrument_id="600000.SH",
        quantity=100,
        target_quantity=100,
        target_weight=Decimal("0.30"),
        reason="incumbent_within_hold_band",
    )
    return DailyStrategyDecisionV2(
        strategy_date=strategy_date,
        execution_date=date(2026, 8, 24),
        decision_status="READY_FOR_NEXT_SESSION_REVIEW",
        data_status="controlled_pit_admitted",
        market_regime="NEUTRAL",
        portfolio_intent_type="ALPHA_REBALANCE",
        target_gross_exposure=Decimal("0.60"),
        feasible_gross_exposure=Decimal("0.60"),
        current_gross_exposure=Decimal("0.30"),
        realized_gross_exposure=None,
        target_stock_weights={"000001.SZ": Decimal("0.30"), "600000.SH": Decimal("0.30")},
        feasible_stock_weights={"000001.SZ": Decimal("0.30"), "600000.SH": Decimal("0.30")},
        current_stock_weights={"600000.SH": Decimal("0.30")},
        realized_stock_weights=None,
        target_lot_quantities={"000001.SZ": 100, "600000.SH": 100},
        feasible_lot_quantities={"000001.SZ": 100, "600000.SH": 100},
        current_lot_quantities={"600000.SH": 100},
        realized_lot_quantities=None,
        buy_orders=(buy,),
        hold_positions=(hold,),
        cash_weight=Decimal("0.40"),
        maximum_execution_price_deviation=Decimal("0.02"),
        cancel_conditions=(
            "buy_price_above_frozen_limit",
            "account_fingerprint_changed",
        ),
        expected_cost=Decimal("6.01"),
        model_reasons=("positive_train_only_prediction",),
        risk_reasons=("neutral_exposure_after_hysteresis",),
        no_trade_reasons=(),
        data_sha256=SHA,
        model_sha256="b" * 64,
        policy_sha256="c" * 64,
        intent_sha256="d" * 64,
    )


class DailyDecisionTests(unittest.TestCase):
    def test_replay_is_byte_identical_and_create_only(self) -> None:
        first = decision()
        second = decision()
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.decision_sha256, second.decision_sha256)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            artifacts_a = write_daily_decision(output, first)
            original = artifacts_a.json_path.read_bytes()
            artifacts_b = write_daily_decision(output, second)
            self.assertEqual(artifacts_a, artifacts_b)
            self.assertEqual(original, artifacts_b.json_path.read_bytes())
            payload = json.loads(original)
            self.assertEqual(payload["decision_sha256"], first.decision_sha256)
            self.assertFalse(payload["paper_eligibility"])
            self.assertFalse(payload["trade_eligibility"])
            self.assertFalse(payload["real_money_list_allowed"])
            self.assertFalse(payload["live_supported"])

    def test_same_date_different_content_is_an_immutable_collision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            write_daily_decision(output, decision())
            base = decision()
            changed = DailyStrategyDecisionV2(
                **{
                    field: getattr(base, field)
                    for field in base.__dataclass_fields__
                    if field not in {"data_status"}
                },
                data_status="changed",
            )
            with self.assertRaisesRegex(DailyPipelineError, "immutable artifact collision"):
                write_daily_decision(output, changed)

    def test_fail_closed_and_pause_decisions_cannot_contain_buy(self) -> None:
        base = decision()
        for status in ("DATA_FAIL_CLOSED", "MANUAL_PAUSE"):
            with self.subTest(status=status):
                values = {
                    field: getattr(base, field)
                    for field in base.__dataclass_fields__
                    if field not in {"decision_status"}
                }
                with self.assertRaisesRegex(DailyPipelineError, "cannot contain BUY"):
                    DailyStrategyDecisionV2(**values, decision_status=status)

    def test_locked_test_dates_are_unconditionally_forbidden(self) -> None:
        base = decision()
        values = {
            field: getattr(base, field)
            for field in base.__dataclass_fields__
            if field not in {"strategy_date", "execution_date"}
        }
        with self.assertRaisesRegex(DailyPipelineError, "locked_test"):
            DailyStrategyDecisionV2(
                **values,
                strategy_date=date(2025, 12, 31),
                execution_date=date(2026, 1, 5),
            )

    def test_markdown_contains_actions_cost_hashes_and_safety_boundary(self) -> None:
        report = render_daily_decision_markdown(decision())
        self.assertIn("## BUY", report)
        self.assertIn("## SELL", report)
        self.assertIn("## HOLD", report)
        self.assertIn("CASH", report)
        self.assertIn("完整成本口径", report)
        self.assertIn("decision_sha256", report)
        self.assertIn("LIVE: `not_supported`", report)

    def test_sell_price_deviation_is_disclosed_but_not_an_exit_veto(self) -> None:
        sell = DailyOrderV1(
            instrument_id="000001.SZ",
            side="SELL",
            quantity=100,
            reference_price=Decimal("10"),
            target_weight=Decimal("0"),
            maximum_execution_price_deviation=Decimal("0.02"),
            cancel_conditions=("risk_reduction_sell_has_no_price_floor",),
        )
        payload = sell.to_dict()
        self.assertFalse(payload["price_deviation_enforced"])
        self.assertNotIn("minimum_sell_price", payload)
        self.assertIsNone(sell.frozen_price_boundary)

    def test_plan_stage_cannot_claim_realized_positions(self) -> None:
        base = decision()
        values = {
            field: getattr(base, field)
            for field in base.__dataclass_fields__
            if field
            not in {
                "realized_gross_exposure",
                "realized_stock_weights",
                "realized_lot_quantities",
            }
        }
        with self.assertRaisesRegex(
            DailyPipelineError,
            "realized fields must remain null",
        ):
            DailyStrategyDecisionV2(
                **values,
                realized_gross_exposure=Decimal("0.30"),
                realized_stock_weights={"600000.SH": Decimal("0.30")},
                realized_lot_quantities={"600000.SH": 100},
            )

    def test_pipeline_schemas_are_strict_json_contracts(self) -> None:
        root = Path(__file__).resolve().parents[1] / "schemas"
        for name in (
            "daily_strategy_decision.v2.json",
            "daily_pipeline_failure_receipt.v1.json",
            "frozen_daily_data.v2.json",
            "manual_fill_bundle.v1.json",
        ):
            payload = json.loads((root / name).read_text(encoding="utf-8"))
            self.assertEqual(payload["type"], "object")
            self.assertFalse(payload["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
