import json
import tempfile
import unittest
from pathlib import Path

from research.industry_radar import (
    FutureDataError,
    classify_state,
    render_markdown,
    score_payload,
)


def feature(value: float, available_at: str = "2026-07-13T15:30:00+08:00") -> dict:
    return {
        "value": value,
        "observation_period_end": "2026-07-13",
        "available_at": available_at,
        "source": "unit-test",
        "version": "v1",
    }


def industry(industry_id: str, name: str, offset: float) -> dict:
    return {
        "industry_id": industry_id,
        "industry_name": name,
        "market": "CN",
        "classification": "CSI_LEVEL_1",
        "features": {
            "excess_return_20d": feature(-0.03 + offset),
            "excess_return_60d": feature(-0.06 + offset * 2),
            "excess_return_120d": feature(-0.08 + offset * 3),
            "breadth_above_ma20": feature(0.30 + offset),
            "breadth_above_ma60": feature(0.25 + offset),
            "breadth_change_20d": feature(-0.10 + offset),
            "turnover_z20": feature(-0.50 + offset * 3),
            "volume_share_change_20d": feature(-0.05 + offset),
            "earnings_revision_3m": feature(-0.10 + offset),
            "revenue_growth_yoy": feature(-0.05 + offset),
            "capex_growth_yoy": feature(-0.08 + offset),
            "valuation_percentile": feature(0.20 + offset),
            "realized_vol_20d": feature(0.15 + offset / 2),
        },
        "previous": {"state": "mixed", "direction_score": 50.0},
    }


class IndustryRadarTest(unittest.TestCase):
    def test_classify_state_separates_price_and_fundamental_evidence(self):
        thresholds = {
            "strong": 65.0,
            "weak": 40.0,
            "breadth_confirmed": 60.0,
            "fundamental_confirmed": 55.0,
            "crowded": 70.0,
            "bottoming_trend_ceiling": 45.0,
            "material_score_change": 8.0,
            "breadth_divergence": 15.0,
        }

        self.assertEqual(
            classify_state(
                {"trend": 78, "breadth": 48, "participation": 90, "fundamental": 60, "crowding": 88},
                70,
                {"state": "strengthening", "direction_score": 72},
                thresholds,
            ),
            "crowded",
        )
        self.assertEqual(
            classify_state(
                {"trend": 72, "breadth": 68, "participation": 65, "fundamental": None, "crowding": 55},
                69,
                None,
                thresholds,
            ),
            "price_only",
        )
        self.assertEqual(
            classify_state(
                {"trend": 52, "breadth": 75, "participation": 62, "fundamental": 70, "crowding": 35},
                63,
                {"state": "mixed", "direction_score": 50},
                thresholds,
            ),
            "emerging",
        )
        self.assertEqual(
            classify_state(
                {"trend": 35, "breadth": 30, "participation": 25, "fundamental": 32, "crowding": 20},
                34,
                None,
                thresholds,
            ),
            "weakening",
        )
        self.assertEqual(
            classify_state(
                {"trend": 40, "breadth": 65, "participation": 48, "fundamental": 68, "crowding": 25},
                54,
                None,
                thresholds,
            ),
            "bottoming",
        )

    def test_score_payload_ranks_industries_and_renders_attention_report(self):
        payload = {
            "as_of": "2026-07-13",
            "decision_time": "2026-07-13T20:00:00+08:00",
            "industries": [
                industry("CN10", "能源", 0.00),
                industry("CN20", "原材料", 0.15),
                industry("CN30", "工业", 0.30),
                industry("CN45", "信息技术", 0.45),
            ],
        }

        result = score_payload(payload)

        self.assertEqual(result["model_id"], "industry-radar-r0")
        self.assertEqual(len(result["industries"]), 4)
        self.assertEqual(result["industries"][0]["industry_name"], "信息技术")
        self.assertGreaterEqual(result["industries"][0]["direction_score"], result["industries"][-1]["direction_score"])
        self.assertIn("attention_score", result["industries"][0])
        self.assertIn("confidence_score", result["industries"][0])
        self.assertTrue(result["industries"][0]["evidence_for"])
        self.assertTrue(result["industries"][0]["invalidation"])

        markdown = render_markdown(result)
        self.assertIn("行业变化雷达", markdown)
        self.assertIn("高关注不等于看多", markdown)
        self.assertIn("信息技术", markdown)
        self.assertIn("基本面", markdown)

    def test_future_available_feature_is_rejected(self):
        records = [
            industry("CN10", "能源", 0.00),
            industry("CN20", "原材料", 0.15),
            industry("CN30", "工业", 0.30),
            industry("CN45", "信息技术", 0.45),
        ]
        records[0]["features"]["earnings_revision_3m"] = feature(
            0.2,
            available_at="2026-07-14T09:00:00+08:00",
        )
        payload = {
            "as_of": "2026-07-13",
            "decision_time": "2026-07-13T20:00:00+08:00",
            "industries": records,
        }

        with self.assertRaises(FutureDataError):
            score_payload(payload)

    def test_missing_fundamentals_cannot_be_called_strengthening(self):
        records = [
            industry("CN10", "能源", 0.00),
            industry("CN20", "原材料", 0.15),
            industry("CN30", "工业", 0.30),
            industry("CN45", "信息技术", 0.45),
        ]
        for name in ("earnings_revision_3m", "revenue_growth_yoy", "capex_growth_yoy"):
            records[-1]["features"].pop(name)
        payload = {
            "as_of": "2026-07-13",
            "decision_time": "2026-07-13T20:00:00+08:00",
            "industries": records,
        }

        result = score_payload(payload)
        technology = next(item for item in result["industries"] if item["industry_id"] == "CN45")

        self.assertEqual(technology["state"], "price_only")
        self.assertLess(technology["confidence_score"], 100.0)


if __name__ == "__main__":
    unittest.main()
