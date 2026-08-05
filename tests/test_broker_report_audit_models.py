from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from research.broker_report_audit.models import (
    CHINA_TZ,
    ClaimOutcome,
    DailyBar,
    FactorObservation,
    ModelValidationError,
    ResearchClaim,
    ResearchReport,
    SkillSnapshot,
    parse_datetime,
)


NOW = datetime(2026, 8, 4, 12, 0, tzinfo=CHINA_TZ)


def report(**overrides):
    values = {
        "report_id": "AP-test",
        "dimension": "stock",
        "subject_id": "000001",
        "title": "测试研报",
        "broker": "测试证券",
        "analyst": "研究员",
        "published_at": NOW - timedelta(days=1),
        "available_at": NOW - timedelta(hours=1),
        "fetched_at": NOW,
        "source": "unit-test",
        "content_hash": "a" * 64,
    }
    values.update(overrides)
    return ResearchReport(**values)


class BrokerReportAuditModelTest(unittest.TestCase):
    def test_source_parser_makes_date_only_values_explicitly_china_aware(self):
        parsed = parse_datetime("2026-08-04")

        self.assertEqual(parsed.tzinfo, CHINA_TZ)
        self.assertEqual(parsed.date(), date(2026, 8, 4))

    def test_domain_models_reject_naive_or_future_inconsistent_timestamps(self):
        with self.assertRaises(ModelValidationError):
            report(published_at=datetime(2026, 8, 3, 12, 0))

        with self.assertRaises(ModelValidationError):
            report(available_at=NOW - timedelta(days=2))

    def test_claim_must_be_falsifiable_and_have_a_positive_horizon(self):
        common = {
            "claim_id": "claim-1",
            "report_id": "AP-test",
            "dimension": "macro",
            "subject_id": "CN-CPI",
            "target_type": "CPI",
            "unit": "%",
            "benchmark": "first_release",
            "forecast_period": "2026-08",
            "available_at": NOW,
            "evidence_span": "预计CPI同比为1.2%",
            "extractor_version": "rules-v1",
            "extraction_confidence": 0.98,
        }

        with self.assertRaises(ModelValidationError):
            ResearchClaim(
                **common,
                direction=0,
                value_min=None,
                value_max=None,
                horizon_days=30,
            )

        with self.assertRaises(ModelValidationError):
            ResearchClaim(
                **common,
                direction=1,
                value_min=Decimal("1.2"),
                value_max=Decimal("1.2"),
                horizon_days=0,
            )

    def test_daily_bar_requires_complete_adjusted_ohlc(self):
        values = {
            "instrument_id": "000001.SZ",
            "trade_date": date(2026, 8, 4),
            "open": Decimal("10"),
            "high": Decimal("11"),
            "low": Decimal("9.5"),
            "close": Decimal("10.5"),
            "volume": Decimal("1000"),
            "amount": Decimal("10000"),
            "available_at": NOW,
            "source": "unit-test",
            "fetched_at": NOW,
            "content_hash": "b" * 64,
            "adjusted_open": Decimal("9"),
        }

        with self.assertRaises(ModelValidationError):
            DailyBar(**values)

        values.update(
            adjusted_open=Decimal("9"),
            adjusted_high=Decimal("10"),
            adjusted_low=Decimal("0"),
            adjusted_close=Decimal("9.5"),
        )
        with self.assertRaises(ModelValidationError):
            DailyBar(**values)

    def test_skill_probabilities_and_lower_bounds_are_consistent(self):
        values = {
            "as_of": NOW,
            "broker": "测试证券",
            "analyst": "研究员",
            "team": "",
            "dimension": "stock",
            "target_type": "EPS",
            "horizon_days": 120,
            "posterior_skill": 0.6,
            "conservative_lower_bound": 0.5,
            "effective_sample_size": 10,
            "source_report_ids": ("r1",),
        }
        invalid = (
            {"posterior_skill": 1.01},
            {"conservative_lower_bound": 0.61},
            {"sensitivity_365": -0.01},
            {"sensitivity_365": 0.55, "sensitivity_365_lower_bound": 0.56},
        )
        for override in invalid:
            with self.subTest(override=override), self.assertRaises(ModelValidationError):
                SkillSnapshot(**(values | override))

    def test_mature_outcome_cannot_precede_truth_availability(self):
        with self.assertRaises(ModelValidationError):
            ClaimOutcome(
                claim_id="claim-1",
                truth_source="official-first-release",
                truth_available_at=NOW,
                realized_value=Decimal("1.2"),
                market_return=None,
                benchmark_return=None,
                error=0.0,
                hit=True,
                mature=True,
                evaluated_at=NOW - timedelta(minutes=1),
            )

    def test_factor_observation_needs_a_frozen_source_snapshot(self):
        with self.assertRaises(ModelValidationError):
            FactorObservation(as_of=NOW, stock_id="000001.SZ", source_snapshot_hash="")

        item = FactorObservation(
            as_of=NOW,
            stock_id="000001.SZ",
            macro_report_raw=0.4,
            macro_report_factor=0.1,
            industry_report_raw=0.5,
            industry_report_factor=0.2,
            stock_report_raw=0.6,
            stock_report_factor=0.3,
            macro_industry_interaction=0.02,
            industry_stock_interaction=0.06,
            source_snapshot_hash="sha256:test",
        )
        self.assertIsNone(item.macro_objective_factor)
        self.assertEqual(item.stock_report_raw, 0.6)

        with self.assertRaises(ModelValidationError):
            FactorObservation(
                as_of=NOW,
                stock_id="000001.SZ",
                macro_report_raw=float("inf"),
                source_snapshot_hash="sha256:test",
            )


if __name__ == "__main__":
    unittest.main()
