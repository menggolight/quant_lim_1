from __future__ import annotations

import json
import unittest
from decimal import Decimal
from pathlib import Path

from operations.run_technical_shadow_mvp import (
    _execute_targets,
    _execution_cost,
    _ledger_transaction_accounting,
    _load_config,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "configs" / "a_share_technical_shadow_mvp.v1.json"


def _persisted(fill: dict) -> dict:
    """Exercise the same JSON scalar boundary used by daily decision artifacts."""

    return json.loads(json.dumps(fill, ensure_ascii=False, sort_keys=True))


class TechnicalShadowAccountingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = _load_config(CONFIG_PATH)

    def assert_fill_is_recomputable(self, fill: dict, *, opening_cash: Decimal) -> Decimal:
        persisted = _persisted(fill)
        quantity = Decimal(persisted["simulated_quantity"])
        reference_price = Decimal(persisted["reference_price"])
        execution_price = Decimal(persisted["execution_price"])
        reference_notional = Decimal(persisted["notional_at_reference_price"])
        execution_notional = Decimal(persisted["notional_at_execution_price"])
        commission = Decimal(persisted["commission"])
        transfer_fee = Decimal(persisted["transfer_fee"])
        stamp_duty = Decimal(persisted["stamp_duty"])
        explicit_fee = Decimal(persisted["explicit_fee"])
        slippage = Decimal(persisted["slippage_cost"])
        total_cost = Decimal(persisted["total_transaction_cost"])
        cash_delta = Decimal(persisted["cash_delta"])

        self.assertEqual(reference_notional, reference_price * quantity)
        self.assertEqual(execution_notional, execution_price * quantity)
        self.assertEqual(explicit_fee, commission + transfer_fee + stamp_duty)
        self.assertEqual(total_cost, explicit_fee + slippage)
        expected_delta = (
            -(execution_notional + explicit_fee)
            if persisted["action"] == "BUY"
            else execution_notional - explicit_fee
        )
        self.assertEqual(cash_delta, expected_delta)
        # Slippage is already embedded in execution_notional and must not be
        # deducted from cash for a second time.
        self.assertNotEqual(cash_delta, expected_delta - slippage)
        return opening_cash + cash_delta

    def test_buy_and_sell_fields_recompute_continuous_cash(self):
        opening_cash = Decimal("10000.00")
        positions, cash_after_buy, buy_fills, buy_cost = _execute_targets(
            targets={"000001.SZ": 100},
            positions={},
            cash=opening_cash,
            execution_rows={
                "000001.SZ": {"open": 16.83, "trading_status": "traded", "is_st": False}
            },
            config=self.config,
        )
        buy = buy_fills[0]
        self.assertEqual(buy["action"], "BUY")
        self.assertEqual(buy["reference_price"], "16.83")
        self.assertEqual(buy["execution_price"], "16.85")
        self.assertEqual(buy["notional_at_reference_price"], "1683.00")
        self.assertEqual(buy["notional_at_execution_price"], "1685.00")
        self.assertEqual(buy["commission"], "5.00")
        self.assertEqual(buy["transfer_fee"], "0.02")
        self.assertEqual(buy["stamp_duty"], "0.00")
        self.assertEqual(buy["explicit_fee"], "5.02")
        self.assertEqual(buy["slippage_cost"], "2.00")
        self.assertEqual(buy["total_transaction_cost"], "7.02")
        self.assertEqual(buy["cash_delta"], "-1690.02")
        self.assertEqual(buy_cost, Decimal("7.02"))
        self.assertEqual(
            cash_after_buy,
            self.assert_fill_is_recomputable(buy, opening_cash=opening_cash),
        )

        positions, final_cash, sell_fills, sell_cost = _execute_targets(
            targets={"000001.SZ": 0},
            positions=positions,
            cash=cash_after_buy,
            execution_rows={
                "000001.SZ": {"open": 17.11, "trading_status": "traded", "is_st": False}
            },
            config=self.config,
        )
        sell = sell_fills[0]
        self.assertEqual(sell["action"], "SELL")
        self.assertEqual(sell["reference_price"], "17.11")
        self.assertEqual(sell["execution_price"], "17.09")
        self.assertEqual(sell["notional_at_reference_price"], "1711.00")
        self.assertEqual(sell["notional_at_execution_price"], "1709.00")
        self.assertEqual(sell["commission"], "5.00")
        self.assertEqual(sell["transfer_fee"], "0.02")
        self.assertEqual(sell["stamp_duty"], "0.85")
        self.assertEqual(sell["explicit_fee"], "5.87")
        self.assertEqual(sell["slippage_cost"], "2.00")
        self.assertEqual(sell["total_transaction_cost"], "7.87")
        self.assertEqual(sell["cash_delta"], "1703.13")
        self.assertEqual(sell_cost, Decimal("7.87"))
        self.assertEqual(
            final_cash,
            self.assert_fill_is_recomputable(sell, opening_cash=cash_after_buy),
        )
        self.assertEqual(positions, {})
        self.assertEqual(final_cash, Decimal("10013.11"))

    def test_execution_cost_rounds_price_and_explicit_fees_before_booking(self):
        buy = _execution_cost(
            side="BUY", quantity=100, open_price=Decimal("16.83"), config=self.config
        )
        sell = _execution_cost(
            side="SELL", quantity=100, open_price=Decimal("17.11"), config=self.config
        )
        self.assertEqual(buy["execution_price"], Decimal("16.85"))
        self.assertEqual(buy["commission"], Decimal("5.00"))
        self.assertEqual(buy["transfer_fee"], Decimal("0.02"))
        self.assertEqual(buy["stamp_duty"], Decimal("0.00"))
        self.assertEqual(sell["execution_price"], Decimal("17.09"))
        self.assertEqual(sell["commission"], Decimal("5.00"))
        self.assertEqual(sell["transfer_fee"], Decimal("0.02"))
        self.assertEqual(sell["stamp_duty"], Decimal("0.85"))

    def test_whole_lot_reduction_preserves_nonnegative_cash(self):
        positions, cash, fills, _ = _execute_targets(
            targets={"000001.SZ": 1000},
            positions={},
            cash=Decimal("1200.00"),
            execution_rows={
                "000001.SZ": {"open": 9.99, "trading_status": "traded", "is_st": False}
            },
            config=self.config,
        )
        fill = fills[0]
        self.assertEqual(fill["action"], "BUY")
        self.assertEqual(fill["simulated_quantity"], 100)
        self.assertEqual(positions, {"000001.SZ": 100})
        self.assertGreaterEqual(cash, Decimal("0.00"))
        self.assertEqual(
            cash,
            self.assert_fill_is_recomputable(fill, opening_cash=Decimal("1200.00")),
        )

    def test_natural_path_totals_and_ledger_breakdown_are_replayable(self):
        cash = Decimal("10000.00")
        positions: dict[str, int] = {}
        executed_fills: list[dict] = []

        positions, cash, fills, _ = _execute_targets(
            targets={"600583.SH": 200}, positions=positions, cash=cash,
            execution_rows={
                "600583.SH": {"open": 7.34, "trading_status": "traded", "is_st": False}
            },
            config=self.config,
        )
        executed_fills.extend(fills)
        positions, cash, fills, _ = _execute_targets(
            targets={"600583.SH": 100}, positions=positions, cash=cash,
            execution_rows={
                "600583.SH": {"open": 8.35, "trading_status": "traded", "is_st": False}
            },
            config=self.config,
        )
        executed_fills.extend(fills)
        positions, cash, fills, _ = _execute_targets(
            targets={"600583.SH": 0}, positions=positions, cash=cash,
            execution_rows={
                "600583.SH": {"open": 7.10, "trading_status": "traded", "is_st": False}
            },
            config=self.config,
        )
        executed_fills.extend(fills)

        transaction_summary, ledger_fills = _ledger_transaction_accounting(executed_fills)
        gross_cash_flow = sum(
            (
                Decimal(fill["notional_at_execution_price"])
                if fill["action"] == "SELL"
                else -Decimal(fill["notional_at_execution_price"])
            )
            for fill in ledger_fills
        )
        self.assertEqual(gross_cash_flow, Decimal("73.00"))
        self.assertEqual(transaction_summary["commission"], "15.00")
        self.assertEqual(transaction_summary["stamp_duty"], "0.77")
        self.assertEqual(transaction_summary["transfer_fee"], "0.03")
        self.assertEqual(transaction_summary["explicit_fee"], "15.80")
        self.assertEqual(transaction_summary["slippage_cost"], "4.00")
        self.assertEqual(transaction_summary["total_transaction_cost"], "19.80")
        self.assertEqual(transaction_summary["cash_delta"], "57.20")
        self.assertEqual(cash, Decimal("10057.20"))
        self.assertEqual(positions, {})
        self.assertEqual(transaction_summary["fill_count"], 3)
        self.assertEqual(transaction_summary["buy_fill_count"], 1)
        self.assertEqual(transaction_summary["sell_fill_count"], 2)
        required_fields = {
            "reference_price", "execution_price", "notional_at_reference_price",
            "notional_at_execution_price", "commission", "stamp_duty",
            "transfer_fee", "explicit_fee", "slippage_cost",
            "total_transaction_cost", "cash_delta",
        }
        self.assertTrue(all(required_fields <= set(fill) for fill in ledger_fills))


if __name__ == "__main__":
    unittest.main()
