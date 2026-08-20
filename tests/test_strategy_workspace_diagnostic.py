from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from dataclasses import replace
import unittest

from research.strategy_workspace.diagnostic import (
    DIAGNOSTIC_FACTOR_IDS,
    CurrentUniverseMember,
    DiagnosticContractError,
    DiagnosticPriceBar,
    compute_price_diagnostics,
    freeze_current_universe_sample,
)
from research.strategy_workspace.contracts import canonical_sha256


TZ = timezone(timedelta(hours=8))
INDUSTRIES = (
    "CSI2021_L1/能源",
    "CSI2021_L1/原材料",
    "CSI2021_L1/工业",
    "CSI2021_L1/可选消费",
    "CSI2021_L1/主要消费",
    "CSI2021_L1/医药卫生",
    "CSI2021_L1/金融",
    "CSI2021_L1/信息技术",
    "CSI2021_L1/通信服务",
    "CSI2021_L1/公用事业",
    "CSI2021_L1/房地产",
)
BUNDLE_FILES = {
    "agent/current_industry_import.py": "1" * 64,
    "agent/current_universe_import.py": "2" * 64,
    "research/market_data/contracts.py": "3" * 64,
    "research/strategy_workspace/contracts.py": "4" * 64,
    "research/strategy_workspace/diagnostic.py": "5" * 64,
}
BUNDLE_RUNTIME = "cpython-3.12.13"
SOURCE_KW = {
    "source_membership_artifact_sha256": "a" * 64,
    "source_membership_payload_sha256": "f" * 64,
    "source_membership_content_sha256": "b" * 64,
    "source_industry_artifact_sha256": "c" * 64,
    "source_industry_payload_sha256": "e" * 64,
    "source_industry_content_sha256": "d" * 64,
    "generator_code_bundle_files": BUNDLE_FILES,
    "generator_code_bundle_runtime": BUNDLE_RUNTIME,
    "generator_code_bundle_sha256": canonical_sha256(
        {"files": BUNDLE_FILES, "runtime": BUNDLE_RUNTIME}
    ),
}


class StrategyWorkspaceDiagnosticTests(unittest.TestCase):
    def test_current_universe_sample_is_deterministic_stratified_and_never_paper(self) -> None:
        members = [
            CurrentUniverseMember(f"{index:06d}.SZ", INDUSTRIES[index % len(INDUSTRIES)])
            for index in range(800)
        ]
        first = freeze_current_universe_sample(
            members,
            information_cutoff_date=date(2026, 8, 19),
            market_snapshot_date=date(2026, 8, 18),
            source_universe_id="CSI800_CURRENT_CHOICE",
            **SOURCE_KW,
        )
        second = freeze_current_universe_sample(
            list(reversed(members)),
            information_cutoff_date=date(2026, 8, 19),
            market_snapshot_date=date(2026, 8, 18),
            source_universe_id="CSI800_CURRENT_CHOICE",
            **SOURCE_KW,
        )

        self.assertEqual(first.instrument_ids, second.instrument_ids)
        self.assertEqual(first.sample_payload_sha256, second.sample_payload_sha256)
        self.assertEqual(len(first.instrument_ids), 60)
        self.assertEqual(set(first.industry_by_instrument.values()), set(INDUSTRIES))
        self.assertEqual(
            first.to_dict()["representation"],
            "diagnostic_equal_industry_coverage_not_csi800_representative",
        )
        self.assertFalse(first.to_dict()["safety"]["paper_eligibility"])
        self.assertFalse(first.to_dict()["safety"]["real_money_list_allowed"])

    def test_fallback_refuses_pit_or_incomplete_universe(self) -> None:
        members = [CurrentUniverseMember(f"{index:06d}.SZ", INDUSTRIES[0]) for index in range(5)]
        with self.assertRaisesRegex(DiagnosticContractError, "formal path"):
            freeze_current_universe_sample(
                members,
                information_cutoff_date=date(2026, 8, 19),
                market_snapshot_date=date(2026, 8, 18),
                source_universe_id="CSI800_CURRENT_CHOICE",
                historical_pit_proven=True,
                **SOURCE_KW,
            )
        with self.assertRaisesRegex(DiagnosticContractError, "complete 800-member"):
            freeze_current_universe_sample(
                members,
                information_cutoff_date=date(2026, 8, 19),
                market_snapshot_date=date(2026, 8, 18),
                source_universe_id="CSI800_CURRENT_CHOICE",
                **SOURCE_KW,
            )

    def test_sample_runtime_enforces_exact_size_keyset_and_cutoff(self) -> None:
        members = [
            CurrentUniverseMember(f"{index:06d}.SZ", INDUSTRIES[index % len(INDUSTRIES)])
            for index in range(800)
        ]
        with self.assertRaisesRegex(DiagnosticContractError, "exactly 60"):
            freeze_current_universe_sample(
                members,
                information_cutoff_date=date(2026, 8, 19),
                market_snapshot_date=date(2026, 8, 18),
                source_universe_id="CSI800_CURRENT_CHOICE",
                sample_size=59,
                **SOURCE_KW,
            )
        with self.assertRaisesRegex(DiagnosticContractError, "snapshot cannot postdate"):
            freeze_current_universe_sample(
                members,
                information_cutoff_date=date(2026, 8, 18),
                market_snapshot_date=date(2026, 8, 19),
                source_universe_id="CSI800_CURRENT_CHOICE",
                **SOURCE_KW,
            )
        sample = freeze_current_universe_sample(
            members,
            information_cutoff_date=date(2026, 8, 19),
            market_snapshot_date=date(2026, 8, 18),
            source_universe_id="CSI800_CURRENT_CHOICE",
            **SOURCE_KW,
        )
        mismatched = dict(sample.industry_by_instrument)
        mismatched.pop(sample.instrument_ids[0])
        mismatched["999999.SH"] = INDUSTRIES[0]
        with self.assertRaisesRegex(DiagnosticContractError, "exactly cover"):
            replace(sample, industry_by_instrument=mismatched)

    def test_price_diagnostics_use_relative_momentum_and_frozen_six(self) -> None:
        sessions = [date(2025, 1, 1) + timedelta(days=index) for index in range(121)]
        stock = []
        benchmark = []
        for index, day in enumerate(sessions):
            available = datetime.combine(day, datetime.min.time(), tzinfo=TZ).replace(hour=16)
            stock.append(
                DiagnosticPriceBar("000001.SZ", day, 100 + index, 101 + index, available)
            )
            benchmark.append(
                DiagnosticPriceBar("000906.CSI", day, 100 + index / 2, 101 + index / 2, available)
            )
        rows = compute_price_diagnostics(
            stock,
            benchmark,
            allowed_instrument_ids=("000001.SZ",),
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(tuple(rows[0].values), DIAGNOSTIC_FACTOR_IDS)
        self.assertGreater(rows[0].values["RM20"], 0)
        self.assertAlmostEqual(rows[0].values["TREND_EFF60"], 1.0)

    def test_available_at_cannot_move_to_a_future_china_date(self) -> None:
        with self.assertRaisesRegex(DiagnosticContractError, "available_at"):
            DiagnosticPriceBar(
                "000001.SZ",
                date(2026, 8, 18),
                10,
                10,
                datetime(2026, 8, 19, 0, 1, tzinfo=TZ),
            )
        with self.assertRaisesRegex(DiagnosticContractError, "controlled close"):
            DiagnosticPriceBar(
                "000001.SZ",
                date(2026, 8, 18),
                10,
                10,
                datetime(2026, 8, 18, 14, 59, tzinfo=TZ),
            )


if __name__ == "__main__":
    unittest.main()
