from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from trading.models import PortfolioIntentType

from research.strategy_workspace.experiment_v3_admission import (
    ExperimentV3AdmissionReceiptV1,
)
from research.strategy_workspace.portfolio_constructor_v2 import (
    ConstructionActionType,
    ConstructorCostPolicy,
    CurrentPosition,
    PortfolioConstructionError,
    PortfolioConstructorPolicy,
    PortfolioInstrument,
    construct_portfolio as construct_portfolio_formal,
    construct_portfolio_diagnostic as construct_portfolio,
)


D = Decimal
TZ = timezone(timedelta(hours=8))
DECISION = datetime(2026, 8, 21, 15, 5, tzinfo=TZ)
EXPERIMENT_SHA256 = "b" * 64
EXPOSURE_POLICY_SOURCE_SHA256 = "a" * 64
CONSTRUCTOR_POLICY_SOURCE_SHA256 = "c" * 64


def admission_receipt():
    return ExperimentV3AdmissionReceiptV1(
        receipt_id="constructor-policy-test",
        issued_at=DECISION - timedelta(hours=12),
        experiment_spec_sha256=EXPERIMENT_SHA256,
        approved_factor_registry_sha256="d" * 64,
        approved_factor_registry_frozen_at=DECISION - timedelta(days=10),
        model_training_receipt_sha256="e" * 64,
        model_admission_receipt_sha256="f" * 64,
        model_sha256="2" * 64,
        model_frozen_at=DECISION - timedelta(days=3),
        calibration_receipt_sha256="1" * 64,
        calibration_horizon_sessions=20,
        exposure_policy_source_sha256=EXPOSURE_POLICY_SOURCE_SHA256,
        exposure_policy_frozen_at=DECISION - timedelta(days=2),
        constructor_policy_source_sha256=CONSTRUCTOR_POLICY_SOURCE_SHA256,
        constructor_policy_frozen_at=DECISION - timedelta(days=1),
    )


def policy(
    *,
    no_trade: str = "0.001",
    entry_return: str = "0.000001",
    hold_return: str = "0",
) -> PortfolioConstructorPolicy:
    return PortfolioConstructorPolicy(
        policy_id=f"constructor-test-{no_trade.replace('.', '-')}",
        frozen_at=DECISION - timedelta(days=1),
        policy_source_sha256=CONSTRUCTOR_POLICY_SOURCE_SHA256,
        experiment_spec_sha256=EXPERIMENT_SHA256,
        policy_admission_receipt=admission_receipt(),
        max_positions=3,
        max_position_weight=D("0.40"),
        entry_percentile_min=D("0.80"),
        hold_percentile_min=D("0.60"),
        entry_predicted_return_min=D(entry_return),
        hold_predicted_return_min=D(hold_return),
        no_trade_threshold=D(no_trade),
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


def instrument(
    instrument_id: str,
    predicted: str | None,
    percentile: str | None,
    *,
    price: str = "10",
    eligible: bool = True,
) -> PortfolioInstrument:
    return PortfolioInstrument(
        instrument_id=instrument_id,
        predicted_return=D(predicted) if predicted is not None else None,
        percentile=D(percentile) if percentile is not None else None,
        eligibility=eligible,
        exclusion_codes=() if eligible else ("missing_pit_field",),
        reference_price=D(price),
        lot_size=100,
    )


class PortfolioConstructorV2Tests(unittest.TestCase):
    def construct(self, *, instruments, positions=(), cash="10000", target="1", intent=PortfolioIntentType.ALPHA_REBALANCE, selected_policy=None):
        return construct_portfolio(
            decision_at=DECISION,
            requested_intent_type=intent,
            target_gross_exposure=D(target),
            current_cash=D(cash),
            current_positions=positions,
            instruments=instruments,
            policy=selected_policy or policy(no_trade="0"),
            input_snapshot_sha256="1" * 64,
            model_sha256="2" * 64,
        )

    def test_formal_blocked_alpha_never_buys_but_pure_reduction_still_sells(self) -> None:
        rows = (instrument("000001.SZ", "0.10", "0.99"),)
        blocked_alpha = construct_portfolio_formal(
            decision_at=DECISION,
            requested_intent_type=PortfolioIntentType.ALPHA_REBALANCE,
            target_gross_exposure=D("1"),
            current_cash=D("10000"),
            current_positions=(),
            instruments=rows,
            policy=policy(no_trade="0"),
            input_snapshot_sha256="1" * 64,
            model_sha256="2" * 64,
        )
        self.assertIs(blocked_alpha.intent_type, PortfolioIntentType.NO_ALPHA_CASH)
        self.assertFalse(
            any(item.action is ConstructionActionType.BUY for item in blocked_alpha.actions)
        )
        self.assertIn(
            "formal_experiment_v3_admission_blocked", blocked_alpha.reason_codes
        )

        reduction = construct_portfolio_formal(
            decision_at=DECISION,
            requested_intent_type=PortfolioIntentType.DEFENSIVE_REDUCTION,
            target_gross_exposure=D("0.30"),
            current_cash=D("5000"),
            current_positions=(CurrentPosition("000001.SZ", 500),),
            instruments=rows,
            policy=policy(no_trade="0"),
            input_snapshot_sha256="1" * 64,
            model_sha256="2" * 64,
        )
        self.assertTrue(
            any(item.action is ConstructionActionType.SELL for item in reduction.actions)
        )
        self.assertFalse(
            any(item.action is ConstructionActionType.BUY for item in reduction.actions)
        )

    def test_max_three_cap_whole_lots_and_deterministic_cash_residual(self) -> None:
        rows = (
            instrument("000001.SZ", "0.09", "0.99"),
            instrument("000002.SZ", "0.08", "0.98"),
            instrument("600000.SH", "0.07", "0.97"),
            instrument("600001.SH", "0.06", "0.96"),
        )
        first = self.construct(instruments=rows)
        second = self.construct(instruments=tuple(reversed(rows)))

        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.construction_sha256, second.construction_sha256)
        self.assertEqual(len(first.feasible_stock_weights), 3)
        self.assertTrue(all(weight <= D("0.40") for weight in first.feasible_stock_weights.values()))
        buys = [item for item in first.actions if item.action is ConstructionActionType.BUY]
        self.assertEqual(len(buys), 3)
        self.assertTrue(all(item.order_quantity % 100 == 0 for item in buys))
        self.assertGreater(first.projected_cash, 0)
        self.assertIn(
            "max_positions_reached",
            next(item.codes for item in first.exclusions if item.instrument_id == "600001.SH"),
        )
        self.assertFalse(first.to_dict()["live_supported"])

    def test_hold_band_prefers_incumbents_over_a_small_ranking_change(self) -> None:
        rows = (
            instrument("000001.SZ", "0.050", "0.70"),
            instrument("000002.SZ", "0.049", "0.69"),
            instrument("600000.SH", "0.048", "0.68"),
            instrument("600001.SH", "0.051", "0.99"),
        )
        positions = (
            CurrentPosition("000001.SZ", 100),
            CurrentPosition("000002.SZ", 100),
            CurrentPosition("600000.SH", 100),
        )
        result = self.construct(instruments=rows, positions=positions, cash="7000")

        self.assertEqual(
            set(result.target_stock_weights),
            {"000001.SZ", "000002.SZ", "600000.SH"},
        )
        self.assertNotIn("600001.SH", result.feasible_quantities)
        self.assertIn(
            "max_positions_reached",
            next(item.codes for item in result.exclusions if item.instrument_id == "600001.SH"),
        )

    def test_expected_improvement_must_strictly_exceed_full_cost_and_threshold(self) -> None:
        rows = (
            instrument("000001.SZ", "0.030", "0.70"),
            instrument("000002.SZ", "0.031", "0.99"),
        )
        result = self.construct(
            instruments=rows,
            positions=(CurrentPosition("000001.SZ", 100),),
            cash="9000",
            target="0.60",
            selected_policy=policy(no_trade="0.02"),
        )

        self.assertFalse(result.alpha_trade_allowed)
        self.assertGreater(result.proposed_expected_cost, 0)
        self.assertEqual(result.expected_cost, 0)
        self.assertEqual(result.feasible_quantities, {"000001.SZ": 100})
        self.assertFalse(any(item.action in {ConstructionActionType.BUY, ConstructionActionType.SELL} for item in result.actions))
        self.assertIn(
            "expected_improvement_not_above_cost_and_threshold",
            result.reason_codes,
        )

    def test_unrounded_cost_ratio_closes_the_display_rounding_gap(self) -> None:
        probe = self.construct(
            instruments=(instrument("000001.SZ", "0.10", "0.99"),),
            cash="10001",
            target="0.30",
        )
        actual_cost_ratio = probe.proposed_expected_cost / probe.current_nav
        self.assertLess(probe.expected_cost_ratio, actual_cost_ratio)

        quantity = probe.feasible_quantities["000001.SZ"]
        proposed_value = D(quantity) * D("10")
        raw_proposed_weight = proposed_value / (
            probe.projected_cash + proposed_value
        )
        improvement_in_rounding_gap = (
            probe.expected_cost_ratio
            + (actual_cost_ratio - probe.expected_cost_ratio) / D("2")
        )
        predicted_return = improvement_in_rounding_gap / raw_proposed_weight

        result = self.construct(
            instruments=(
                instrument(
                    "000001.SZ",
                    str(predicted_return),
                    "0.99",
                ),
            ),
            cash="10001",
            target="0.30",
        )

        self.assertFalse(result.alpha_trade_allowed)
        self.assertEqual(result.expected_cost, 0)
        self.assertFalse(
            any(
                item.action in {
                    ConstructionActionType.BUY,
                    ConstructionActionType.SELL,
                }
                for item in result.actions
            )
        )
        self.assertIn(
            "expected_improvement_not_above_cost_and_threshold",
            result.reason_codes,
        )

    def test_candidate_shortage_and_unaffordable_lot_preserve_cash(self) -> None:
        affordable = self.construct(
            instruments=(instrument("000001.SZ", "0.10", "0.99", price="20"),),
            target="1",
        )
        self.assertEqual(len(affordable.feasible_quantities), 1)
        self.assertLessEqual(next(iter(affordable.feasible_stock_weights.values())), D("0.40"))
        self.assertGreater(affordable.projected_cash, D("5000"))

        unaffordable = self.construct(
            instruments=(instrument("000001.SZ", "0.10", "0.99", price="1000"),),
            target="1",
        )
        self.assertEqual(unaffordable.feasible_quantities, {})
        self.assertEqual(unaffordable.feasible_gross_exposure, 0)
        self.assertEqual(unaffordable.projected_cash, D("10000.0000"))
        self.assertIn(
            "minimum_lot_unaffordable",
            next(item.codes for item in unaffordable.exclusions if item.instrument_id == "000001.SZ"),
        )

    def test_no_eligible_stock_becomes_no_alpha_cash(self) -> None:
        result = self.construct(
            instruments=(instrument("000001.SZ", "0.10", "0.99", eligible=False),),
            positions=(CurrentPosition("000001.SZ", 100),),
            cash="9000",
            target="0.60",
        )
        self.assertIs(result.intent_type, PortfolioIntentType.NO_ALPHA_CASH)
        self.assertEqual(result.target_gross_exposure, 0)
        self.assertEqual(result.feasible_quantities, {})
        self.assertTrue(any(item.action is ConstructionActionType.SELL for item in result.actions))
        self.assertIn("no_eligible_alpha_cash", result.reason_codes)

    def test_non_positive_or_below_entry_predictions_become_cash_and_never_buy(self) -> None:
        for predicted in ("-0.01", "0", "0.004"):
            with self.subTest(predicted=predicted):
                result = self.construct(
                    instruments=(
                        instrument("000001.SZ", predicted, "0.99"),
                        instrument("000002.SZ", "-0.001", "1.00"),
                    ),
                    target="0.60",
                    selected_policy=policy(
                        no_trade="0",
                        entry_return="0.005",
                        hold_return="0",
                    ),
                )
                self.assertIs(
                    result.intent_type, PortfolioIntentType.NO_ALPHA_CASH
                )
                self.assertEqual(result.target_gross_exposure, 0)
                self.assertFalse(
                    any(
                        item.action is ConstructionActionType.BUY
                        for item in result.actions
                    )
                )
                self.assertIn(
                    "no_entry_qualified_alpha_cash", result.reason_codes
                )

    def test_below_hold_incumbent_must_exit_even_when_rotation_fails_no_trade_gate(self) -> None:
        result = self.construct(
            instruments=(
                instrument("000001.SZ", "0.005", "0.70"),
                instrument("000002.SZ", "0.03", "0.99"),
            ),
            positions=(CurrentPosition("000001.SZ", 100),),
            cash="9000",
            target="0.60",
            selected_policy=policy(
                no_trade="0.50",
                entry_return="0.01",
                hold_return="0.01",
            ),
        )

        sells = [
            item for item in result.actions if item.action is ConstructionActionType.SELL
        ]
        self.assertEqual([item.instrument_id for item in sells], ["000001.SZ"])
        self.assertIn("mandatory_alpha_exit", sells[0].reason_codes)
        self.assertFalse(
            any(item.action is ConstructionActionType.BUY for item in result.actions)
        )
        self.assertIn(
            "mandatory_exit_bypasses_alpha_no_trade_threshold",
            result.reason_codes,
        )

    def test_out_of_pool_incumbent_is_mandatory_exit_not_unbounded_hold(self) -> None:
        outside = PortfolioInstrument(
            instrument_id="000001.SZ",
            predicted_return=None,
            percentile=None,
            eligibility=False,
            exclusion_codes=("not_in_current_alpha_universe",),
            reference_price=D("10"),
            lot_size=100,
        )
        result = self.construct(
            instruments=(outside, instrument("000002.SZ", "0.03", "0.99")),
            positions=(CurrentPosition("000001.SZ", 100),),
            cash="9000",
            target="0.60",
            selected_policy=policy(no_trade="0.50", entry_return="0.01"),
        )

        sell = next(
            item for item in result.actions if item.action is ConstructionActionType.SELL
        )
        self.assertEqual(sell.instrument_id, "000001.SZ")
        self.assertIn("mandatory_alpha_exit", sell.reason_codes)
        self.assertNotIn("000001.SZ", result.feasible_quantities)
        outside_codes = next(
            item.codes
            for item in result.exclusions
            if item.instrument_id == "000001.SZ"
        )
        self.assertIn("mandatory_exit_out_of_pool", outside_codes)

    def test_constructor_policy_rejects_receipt_or_source_drift(self) -> None:
        receipt = admission_receipt()
        with self.assertRaisesRegex(
            PortfolioConstructionError, "diagnostic binding mismatch"
        ):
            PortfolioConstructorPolicy(
                policy_id="source-drift",
                frozen_at=DECISION - timedelta(days=1),
                policy_source_sha256="9" * 64,
                experiment_spec_sha256=EXPERIMENT_SHA256,
                policy_admission_receipt=receipt,
                max_positions=3,
                max_position_weight=D("0.40"),
                entry_percentile_min=D("0.80"),
                hold_percentile_min=D("0.60"),
                entry_predicted_return_min=D("0.01"),
                hold_predicted_return_min=D("0"),
                no_trade_threshold=D("0"),
                maximum_execution_price_deviation=D("0.02"),
                maximum_quote_age_seconds=300,
                maximum_account_age_seconds=600,
                costs=policy(no_trade="0").costs,
            )

    def test_ineligible_missing_scores_remain_null_and_never_enter_alpha(self) -> None:
        excluded = instrument("000001.SZ", None, None, eligible=False)
        self.assertIsNone(excluded.to_dict()["predicted_return"])
        self.assertIsNone(excluded.to_dict()["percentile"])
        self.assertEqual(excluded.exclusion_codes, ("missing_pit_field",))

        result = self.construct(
            instruments=(excluded,),
            positions=(CurrentPosition("000001.SZ", 100),),
            cash="9000",
            target="0.60",
        )
        self.assertIs(result.intent_type, PortfolioIntentType.NO_ALPHA_CASH)
        self.assertIsNone(result.expected_improvement)
        self.assertTrue(any(item.action is ConstructionActionType.SELL for item in result.actions))

    def test_explicit_risk_reduction_bypasses_alpha_no_trade_and_never_buys(self) -> None:
        result = self.construct(
            instruments=(instrument("000001.SZ", "-0.10", "0.10"),),
            positions=(CurrentPosition("000001.SZ", 500),),
            cash="5000",
            target="0.30",
            intent=PortfolioIntentType.DEFENSIVE_REDUCTION,
            selected_policy=policy(no_trade="0.50"),
        )

        self.assertIs(result.intent_type, PortfolioIntentType.DEFENSIVE_REDUCTION)
        self.assertTrue(any(item.action is ConstructionActionType.SELL for item in result.actions))
        self.assertFalse(any(item.action is ConstructionActionType.BUY for item in result.actions))
        self.assertGreater(result.expected_cost, 0)
        self.assertIn("reduction_bypasses_alpha_no_trade_threshold", result.reason_codes)
        self.assertLess(result.feasible_gross_exposure, result.current_gross_exposure)
        self.assertEqual(result.target_gross_exposure, D("0.30"))


if __name__ == "__main__":
    unittest.main()
