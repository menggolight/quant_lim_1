import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from trading.models import ExecutionMode
from trading.strategy_bridge import SignalEnvelope, SignalRejected, targets_from_signal


TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 7, 14, 18, 0, tzinfo=TZ)


def signal(**overrides) -> SignalEnvelope:
    values = {
        "signal_id": "signal-001",
        "model_id": "validated-etf-model-v1",
        "model_admission": "approved_for_paper",
        "source_kind": "point_in_time_market_data",
        "available_at": NOW - timedelta(minutes=5),
        "frozen_at": NOW - timedelta(minutes=1),
        "data_snapshot_hash": "sha256:abc123",
        "synthetic": False,
        "trade_eligible": True,
        "target_weights": {"ETF_A": Decimal("0.30")},
    }
    values.update(overrides)
    return SignalEnvelope(**values)


class StrategyBridgeTest(unittest.TestCase):
    def test_current_industry_radar_is_never_a_direct_order_source(self):
        with self.assertRaises(SignalRejected) as caught:
            targets_from_signal(
                signal(model_id="industry-radar-r0", source_kind="industry_radar"),
                decision_time=NOW,
                mode=ExecutionMode.PAPER,
            )

        self.assertEqual(caught.exception.code, "research_radar_not_trade_signal")

    def test_synthetic_or_unfrozen_inputs_fail_closed(self):
        cases = (
            (signal(synthetic=True), "synthetic_signal"),
            (signal(frozen_at=None), "signal_not_frozen"),
            (signal(data_snapshot_hash=""), "data_snapshot_untraceable"),
        )
        for envelope, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                with self.assertRaises(SignalRejected) as caught:
                    targets_from_signal(envelope, decision_time=NOW, mode=ExecutionMode.PAPER)
                self.assertEqual(caught.exception.code, expected_code)

    def test_approved_frozen_paper_signal_can_only_emit_target_weights(self):
        targets = targets_from_signal(signal(), decision_time=NOW, mode=ExecutionMode.PAPER)

        self.assertEqual(targets, {"ETF_A": Decimal("0.30")})

    def test_live_requires_separate_model_admission(self):
        with self.assertRaises(SignalRejected) as caught:
            targets_from_signal(signal(), decision_time=NOW, mode=ExecutionMode.LIVE)

        self.assertEqual(caught.exception.code, "model_not_approved_for_live")


if __name__ == "__main__":
    unittest.main()
