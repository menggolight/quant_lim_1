from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import unittest

import numpy as np

from research.strategy_workspace.quality_growth import (
    FINANCIAL_FACTOR_IDS,
    QUALITY_GROWTH_FACTOR_IDS,
    QUALITY_GROWTH_FACTOR_SPECS,
    FactorAvailability,
    QualityGrowthError,
    QuarterlyFundamental,
    compute_quality_growth_snapshot,
)


CN_TZ = timezone(timedelta(hours=8))


def quarter_ends(start_year: int, count: int) -> list[date]:
    result = []
    year = start_year
    for _ in range(count):
        for month, day in ((3, 31), (6, 30), (9, 30), (12, 31)):
            result.append(date(year, month, day))
            if len(result) == count:
                return result
        year += 1
    return result


def records(count: int = 12) -> list[QuarterlyFundamental]:
    result = []
    for index, period_end in enumerate(quarter_ends(2020, count), start=1):
        result.append(
            QuarterlyFundamental(
                instrument_id="000001.SZ",
                period_end=period_end,
                first_disclosed_at=datetime.combine(
                    period_end + timedelta(days=30),
                    datetime.min.time(),
                    tzinfo=CN_TZ,
                ),
                source_record_id=f"first-{index}",
                source_record_sha256=f"{index:064x}",
                revision_sequence=1,
                roe=float(index),
                net_profit=float(index * index + (index % 3)),
                operating_cash_flow=float(index * 3),
                operating_profit=float(index * 2),
                gross_profit=float(index * 4),
                total_assets=float(100 + index),
                total_liabilities=float(50 + index),
                revenue=float(100 + index * index),
            )
        )
    return result


class QualityGrowthFactorTests(unittest.TestCase):
    def test_family_is_exactly_six_positive_preregistered_formulas(self) -> None:
        self.assertEqual(len(QUALITY_GROWTH_FACTOR_SPECS), 6)
        self.assertEqual(
            tuple(item.factor_id for item in QUALITY_GROWTH_FACTOR_SPECS),
            QUALITY_GROWTH_FACTOR_IDS,
        )
        self.assertTrue(all(item.expected_sign == "positive" for item in QUALITY_GROWTH_FACTOR_SPECS))
        self.assertEqual(
            FINANCIAL_FACTOR_IDS,
            (
                "QG_ROE_STABILITY",
                "QG_EARNINGS_TREND_DEVIATION",
            ),
        )

    def test_nonfinancial_formulas_match_the_frozen_definitions(self) -> None:
        source = records()
        decision_at = datetime(2023, 3, 1, 16, tzinfo=CN_TZ)
        snapshot = compute_quality_growth_snapshot(
            source,
            decision_at=decision_at,
            industry_is_financial=False,
        )
        values = snapshot.values

        roe = np.asarray([item.roe for item in source], dtype=float)
        self.assertAlmostEqual(
            values["QG_ROE_STABILITY"],
            roe[-1] - np.std(roe[-12:], ddof=1),
        )
        preceding = np.asarray([item.net_profit for item in source[-9:-1]], dtype=float)
        design = np.column_stack((np.ones(8), np.arange(8, dtype=float)))
        parameters = np.linalg.lstsq(design, preceding, rcond=None)[0]
        residuals = preceding - design @ parameters
        expected_deviation = (
            float(source[-1].net_profit) - float(np.asarray([1.0, 8.0]) @ parameters)
        ) / float(np.std(residuals, ddof=1))
        self.assertAlmostEqual(values["QG_EARNINGS_TREND_DEVIATION"], expected_deviation)

        last_four = source[-4:]
        ttm_cash = sum(float(item.operating_cash_flow) for item in last_four)
        ttm_operating_profit = sum(float(item.operating_profit) for item in last_four)
        ttm_gross = sum(float(item.gross_profit) for item in last_four)
        self.assertAlmostEqual(
            values["QG_CASH_EARNINGS_QUALITY"],
            (ttm_cash - ttm_operating_profit) / float(source[-1].total_assets),
        )
        self.assertAlmostEqual(
            values["QG_CASH_DEBT_COVERAGE"],
            ttm_cash / float(source[-1].total_liabilities),
        )
        average_assets = (float(source[-1].total_assets) + float(source[-5].total_assets)) / 2.0
        self.assertAlmostEqual(values["QG_GROSS_PROFITABILITY"], ttm_gross / average_assets)
        revenue = np.asarray([item.revenue for item in source[-12:]], dtype=float)
        growth = revenue[4:] / revenue[:8] - 1.0
        self.assertAlmostEqual(
            values["QG_REVENUE_GROWTH_STABILITY"],
            np.mean(growth) - np.std(growth, ddof=1),
        )
        self.assertTrue(
            all(item.availability is FactorAvailability.AVAILABLE for item in snapshot.factors)
        )

    def test_future_and_revision_records_cannot_change_the_snapshot(self) -> None:
        source = records()
        decision_at = datetime(2023, 3, 1, 16, tzinfo=CN_TZ)
        baseline = compute_quality_growth_snapshot(
            source, decision_at=decision_at, industry_is_financial=False
        )
        revised = replace(
            source[-1],
            source_record_id="later-revision",
            source_record_sha256="f" * 64,
            revision_sequence=2,
            net_profit=999999.0,
        )
        future = QuarterlyFundamental(
            instrument_id="000001.SZ",
            period_end=date(2023, 3, 31),
            first_disclosed_at=datetime(2023, 5, 1, tzinfo=CN_TZ),
            source_record_id="future-first",
            source_record_sha256="e" * 64,
            revision_sequence=1,
            roe=999.0,
        )
        observed = compute_quality_growth_snapshot(
            [*source, revised, future],
            decision_at=decision_at,
            industry_is_financial=False,
        )
        self.assertEqual(observed.latest_period_end, baseline.latest_period_end)
        self.assertEqual(observed.values, baseline.values)
        self.assertNotIn("later-revision", observed.source_record_ids)
        self.assertNotIn("future-first", observed.source_record_ids)

    def test_financial_subset_is_explicitly_not_applicable_not_zero(self) -> None:
        snapshot = compute_quality_growth_snapshot(
            records(),
            decision_at=datetime(2023, 3, 1, 16, tzinfo=CN_TZ),
            industry_is_financial=True,
        )
        by_id = {item.factor_id: item for item in snapshot.factors}
        for factor_id in (
            "QG_CASH_EARNINGS_QUALITY",
            "QG_CASH_DEBT_COVERAGE",
            "QG_GROSS_PROFITABILITY",
            "QG_REVENUE_GROWTH_STABILITY",
        ):
            self.assertIsNone(by_id[factor_id].value)
            self.assertIs(by_id[factor_id].availability, FactorAvailability.NOT_APPLICABLE)
        for factor_id in FINANCIAL_FACTOR_IDS:
            self.assertIs(by_id[factor_id].availability, FactorAvailability.AVAILABLE)

    def test_invalid_denominator_and_zero_trend_residual_remain_missing(self) -> None:
        source = records()
        source[-1] = replace(source[-1], total_liabilities=0.0)
        for index, item in enumerate(source):
            source[index] = replace(item, net_profit=float(index))
        snapshot = compute_quality_growth_snapshot(
            source,
            decision_at=datetime(2023, 3, 1, 16, tzinfo=CN_TZ),
            industry_is_financial=False,
        )
        by_id = {item.factor_id: item for item in snapshot.factors}
        self.assertIsNone(by_id["QG_CASH_DEBT_COVERAGE"].value)
        self.assertIsNone(by_id["QG_EARNINGS_TREND_DEVIATION"].value)
        self.assertIs(by_id["QG_CASH_DEBT_COVERAGE"].availability, FactorAvailability.MISSING)

    def test_duplicate_first_disclosure_is_rejected(self) -> None:
        source = records()
        duplicate = replace(source[-1], source_record_id="duplicate")
        with self.assertRaisesRegex(QualityGrowthError, "duplicate first-disclosure"):
            compute_quality_growth_snapshot(
                [*source, duplicate],
                decision_at=datetime(2023, 3, 1, 16, tzinfo=CN_TZ),
                industry_is_financial=False,
            )

    def test_flow_basis_and_revision_chain_are_explicit_not_caller_booleans(self) -> None:
        with self.assertRaisesRegex(QualityGrowthError, "flow_basis"):
            replace(records()[0], flow_basis="ytd_cumulative")
        with self.assertRaisesRegex(QualityGrowthError, "revision_sequence"):
            replace(records()[0], revision_sequence=0)
        with self.assertRaisesRegex(QualityGrowthError, "source_record_sha256"):
            replace(records()[0], source_record_sha256="caller-says-official")


if __name__ == "__main__":
    unittest.main()
