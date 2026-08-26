from __future__ import annotations

import json
import unittest
from decimal import Decimal
from pathlib import Path

from operations.run_technical_shadow_mvp import (
    _execute_targets,
    _execution_cost,
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
        execution_price = Decimal(persisted["simulated_fill_price"])
        execution_notional = Decimal(persisted["execution_notional"])
        commission = Decimal(persisted["commission"])
        transfer_fee = Decimal(persisted["transfer_fee"])
        sell_tax = Decimal(persisted["sell_tax"])
        explicit_fees = Decimal(persisted["explicit_fees"])
        slippage = Decimal(persisted["slippage_cost"])
        total_cost = Decimal(persisted["total_cost"])
        cash_delta = Decimal(persisted["cash_delta"])

        self.assertEqual(execution_notional, execution_price * quantity)
        self.assertEqual(explicit_fees, commission + transfer_fee + sell_tax)
        self.assertEqual(total_cost, explicit_fees + slippage)
        expected_delta = (
            -(execution_notional + explicit_fees)
            if persisted["action"] == "BUY"
            else execution_notional - explicit_fees
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
        self.assertEqual(buy["simulated_fill_price"], "16.85")
        self.assertEqual(buy["execution_notional"], "1685.00")
        self.assertEqual(buy["commission"], "5.00")
        self.assertEqual(buy["transfer_fee"], "0.02")
        self.assertEqual(buy["sell_tax"], "0.00")
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
        self.assertEqual(sell["simulated_fill_price"], "17.09")
        self.assertEqual(sell["execution_notional"], "1709.00")
        self.assertEqual(sell["commission"], "5.00")
        self.assertEqual(sell["transfer_fee"], "0.02")
        self.assertEqual(sell["sell_tax"], "0.85")
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
        self.assertEqual(buy["sell_tax"], Decimal("0.00"))
        self.assertEqual(sell["execution_price"], Decimal("17.09"))
        self.assertEqual(sell["commission"], Decimal("5.00"))
        self.assertEqual(sell["transfer_fee"], Decimal("0.02"))
        self.assertEqual(sell["sell_tax"], Decimal("0.85"))

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


if __name__ == "__main__":
    unittest.main()
