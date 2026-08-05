from __future__ import annotations

import json
import math
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from research.broker_report_audit.evaluation import (
    FutureDataError,
    compute_forward_returns,
    deduplicate_reports,
    evaluate_claim,
    rating_economic_threshold,
    resolve_report_t0,
)
from research.broker_report_audit.models import (
    CHINA_TZ,
    ClaimOutcome,
    DailyBar,
    FactorObservation,
    ResearchClaim,
    ResearchReport,
)
from research.broker_report_audit.reporting import (
    ARTIFACT_FILENAMES,
    build_accuracy_rows,
    build_factor_rows,
    write_report_bundle,
)
from research.broker_report_audit.skills import estimate_skill
from research.broker_report_audit.storage import AuditStore, ContentAddressedHttpCache


def ts(day: int, hour: int = 8) -> datetime:
    return datetime(2024, 1, day, hour, tzinfo=CHINA_TZ)


def report(report_id: str, day: int, *, rating: str = "买入") -> ResearchReport:
    published = ts(day)
    return ResearchReport(
        report_id=report_id,
        dimension="stock",
        subject_id="000333.SZ",
        title="美的集团跟踪",
        broker="测试券商",
        analyst="分析师甲",
        published_at=published,
        available_at=published,
        fetched_at=ts(31),
        source="eastmoney_public_sample",
        content_hash=(report_id * 64)[:64],
        rating=rating,
        timestamp_quality="date_only",
    )


class PointInTimeEvaluationTests(unittest.TestCase):
    def test_fundamental_truth_requires_exact_identity_unit_and_basis(self) -> None:
        claim = {
            "claim_id": "eps-contract",
            "dimension": "stock",
            "subject_id": "000333.SZ",
            "target_type": "EPS",
            "direction": 0,
            "value_min": 1.2,
            "value_max": 1.2,
            "unit": "CNY/share",
            "benchmark": "annual_report_basic_eps",
            "forecast_period": "2024FY",
            "horizon_days": 120,
            "available_at": ts(1),
        }
        truth = {
            "claim_id": "eps-contract",
            "dimension": "stock",
            "subject_id": "000333.SZ",
            "target_type": "EPS",
            "forecast_period": "2024FY",
            "unit": "CNY/share",
            "basis": "annual_report_basic_eps",
            "realized_value": 1.2,
            "truth_source": "cninfo_first_release",
            "available_at": ts(3),
            "first_release": True,
            "revision": False,
            "evidence_verified": True,
            "content_hash": "f" * 64,
            "evidence_url": "https://www.cninfo.com.cn/evidence",
        }
        cases = (
            ({**truth, "subject_id": "000001.SZ"}, "truth_identity_mismatch"),
            ({**truth, "unit": "%"}, "truth_unit_mismatch"),
            ({**truth, "basis": "diluted_eps"}, "truth_basis_mismatch"),
        )
        for bad_truth, reason in cases:
            with self.subTest(reason=reason):
                outcome = evaluate_claim(claim, truth=bad_truth, as_of=ts(20, 23))
                self.assertFalse(outcome.mature)
                self.assertEqual(outcome.exclusion_reason, reason)

    def test_directional_fundamental_never_uses_level_as_change(self) -> None:
        claim = {
            "claim_id": "demand-direction",
            "dimension": "industry",
            "subject_id": "BK_TEST",
            "target_type": "industry_demand",
            "direction": 1,
            "value_min": None,
            "value_max": None,
            "unit": "%",
            "benchmark": "同比变化",
            "forecast_period": "2024Q1",
            "horizon_days": 120,
            "available_at": ts(1),
        }
        truth = {
            "claim_id": "demand-direction",
            "dimension": "industry",
            "subject_id": "BK_TEST",
            "target_type": "industry_demand",
            "forecast_period": "2024Q1",
            "unit": "%",
            "basis": "同比变化",
            "realized_value": 20.0,
            "truth_source": "industry_official_first_release",
            "available_at": ts(3),
            "first_release": True,
            "revision": False,
            "evidence_verified": True,
            "content_hash": "e" * 64,
            "evidence_url": "https://industry.example/evidence",
        }
        outcome = evaluate_claim(claim, truth=truth, as_of=ts(20, 23))
        self.assertFalse(outcome.mature)
        self.assertEqual(outcome.exclusion_reason, "missing_directional_change_truth")
    def test_date_only_report_executes_at_next_trading_day_open(self) -> None:
        t0 = resolve_report_t0(
            {"published_at": "2024-01-05", "timestamp_quality": "date_only"},
            ["2024-01-05", "2024-01-08", "2024-01-09"],
        )
        self.assertEqual(t0, datetime(2024, 1, 8, 9, 30, tzinfo=CHINA_TZ))

    def test_forward_excess_return_is_geometric_and_uses_adjusted_prices(self) -> None:
        bars = [
            {
                "trade_date": "2024-01-08",
                "open": 100,
                "close": 50,
                "adjusted_open": 50,
                "adjusted_close": 50,
                "available_at": ts(8, 16),
                "fetched_at": ts(8, 16),
            },
            {
                "trade_date": "2024-01-09",
                "open": 51,
                "close": 55,
                "adjusted_open": 51,
                "adjusted_close": 55,
                "available_at": ts(9, 16),
                "fetched_at": ts(9, 16),
            },
        ]
        benchmark = [
            {"trade_date": "2024-01-08", "open": 100, "close": 100, "available_at": ts(8, 16), "fetched_at": ts(8, 16)},
            {"trade_date": "2024-01-09", "open": 100, "close": 105, "available_at": ts(9, 16), "fetched_at": ts(9, 16)},
        ]
        result = compute_forward_returns(
            {"published_at": "2024-01-05", "timestamp_quality": "date_only"},
            bars,
            benchmark,
            horizons=(2,),
            as_of=ts(20),
        )[2]
        self.assertTrue(result["mature"])
        self.assertAlmostEqual(result["market_return"], 0.10)
        self.assertAlmostEqual(result["benchmark_return"], 0.05)
        self.assertAlmostEqual(result["excess_return"], 1.10 / 1.05 - 1.0)

    def test_future_fetched_bar_cannot_be_backfilled_into_history(self) -> None:
        with self.assertRaises(FutureDataError):
            compute_forward_returns(
                {"published_at": "2024-01-05", "timestamp_quality": "date_only"},
                [
                    {
                        "trade_date": "2024-01-08",
                        "open": 100,
                        "close": 100,
                        "available_at": ts(8, 16),
                        "fetched_at": ts(31, 16),
                    }
                ],
                horizons=(1,),
                as_of=ts(20, 23),
            )

    def test_unverified_truth_cannot_mint_a_fundamental_hit(self) -> None:
        claim = {
            "claim_id": "unverified-truth",
            "dimension": "stock",
            "subject_id": "000333.SZ",
            "target_type": "EPS",
            "direction": 0,
            "value_min": 1.2,
            "value_max": 1.2,
            "unit": "CNY/share",
            "benchmark": "annual_report_basic_eps",
            "forecast_period": "2024FY",
            "horizon_days": 120,
            "available_at": ts(1),
        }
        truth = {
            "claim_id": claim["claim_id"],
            "dimension": claim["dimension"],
            "subject_id": claim["subject_id"],
            "target_type": claim["target_type"],
            "forecast_period": claim["forecast_period"],
            "unit": claim["unit"],
            "basis": claim["benchmark"],
            "realized_value": 1.2,
            "truth_source": "cninfo_first_release",
            "available_at": ts(3),
            "first_release": True,
            "revision": False,
            "evidence_verified": False,
            "content_hash": "f" * 64,
            "evidence_url": "https://www.cninfo.com.cn/evidence",
        }
        outcome = evaluate_claim(claim, truth=truth, as_of=ts(20, 23))
        self.assertFalse(outcome.mature)
        self.assertEqual(outcome.exclusion_reason, "truth_evidence_not_verified")

    def test_default_rating_threshold_has_preregistered_sqrt_time_scaling(self) -> None:
        for horizon in (20, 60, 120, 250):
            self.assertAlmostEqual(
                rating_economic_threshold(horizon),
                0.05 * math.sqrt(horizon / 250.0),
                places=12,
            )

    def test_target_price_already_inside_range_at_t0_cannot_score_hit(self) -> None:
        claim = {
            "claim_id": "target-at-t0",
            "dimension": "stock",
            "subject_id": "000333.SZ",
            "target_type": "target_price",
            "direction": 0,
            "value_min": 100,
            "value_max": 110,
            "horizon_days": 2,
            "available_at": ts(5),
        }
        source_report = {
            "published_at": "2024-01-05",
            "available_at": "2024-01-05",
            "timestamp_quality": "date_only",
        }
        bars = [
            {
                "trade_date": "2024-01-08",
                "open": 105,
                "high": 108,
                "low": 103,
                "close": 106,
                "available_at": ts(8, 16),
                "fetched_at": ts(8, 16),
                "source": "eastmoney_public.push2his",
            },
            {
                "trade_date": "2024-01-09",
                "open": 106,
                "high": 120,
                "low": 105,
                "close": 118,
                "available_at": ts(9, 16),
                "fetched_at": ts(9, 16),
                "source": "eastmoney_public.push2his",
            },
        ]
        result = evaluate_claim(
            claim,
            report=source_report,
            bars=bars,
            benchmark_bars=[
                {
                    "trade_date": "2024-01-08",
                    "open": 100,
                    "close": 100,
                    "available_at": ts(8, 16),
                    "fetched_at": ts(8, 16),
                    "source": "eastmoney_public.push2his",
                },
                {
                    "trade_date": "2024-01-09",
                    "open": 100,
                    "close": 101,
                    "available_at": ts(9, 16),
                    "fetched_at": ts(9, 16),
                    "source": "eastmoney_public.push2his",
                },
            ],
            as_of=ts(20, 23),
            market_benchmark_id="BK_TEST_INDUSTRY",
            market_benchmark_kind="industry",
        )
        self.assertFalse(result.mature)
        self.assertIsNone(result.hit)
        self.assertEqual(result.exclusion_reason, "target_already_reached_at_t0")
        self.assertEqual(
            result.market_exclusion_reason, "target_already_reached_at_t0"
        )

    def test_unchanged_report_episode_defaults_to_sixty_days(self) -> None:
        reports = [
            {
                "report_id": "r1",
                "available_at": "2024-01-01T08:00:00+08:00",
                "broker": "甲",
                "analyst": "乙",
                "dimension": "stock",
                "subject_id": "000001.SZ",
                "rating": "buy",
                "title": "公司周报",
            },
            {
                "report_id": "r2",
                "available_at": "2024-02-15T08:00:00+08:00",
                "broker": "甲",
                "analyst": "乙",
                "dimension": "stock",
                "subject_id": "000001.SZ",
                "rating": "buy",
                "title": "公司周报",
            },
        ]
        self.assertEqual([row["report_id"] for row in deduplicate_reports(reports)], ["r1"])


class PointInTimeStorageTests(unittest.TestCase):
    def test_decision_time_is_an_automatic_read_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "audit.sqlite3"
            with AuditStore(database) as store:
                store.upsert_reports([report("a", 1), report("b", 20)])
                store.upsert_factor_observation(
                    FactorObservation(
                        as_of=ts(20),
                        stock_id="000333.SZ",
                        macro_objective_factor=0.1,
                        macro_report_factor=0.1,
                        industry_objective_factor=0.1,
                        industry_report_factor=0.1,
                        stock_report_factor=0.1,
                        macro_industry_interaction=0.01,
                        industry_stock_interaction=0.01,
                        source_snapshot_hash="f" * 64,
                    )
                )
            with AuditStore(database, decision_time=ts(10, 23)) as historical:
                self.assertEqual(list(historical.iter_reports()), [])
                self.assertEqual(list(historical.iter_factor_observations()), [])

    def test_content_cache_detects_blob_corruption(self) -> None:
        from research.broker_report_audit.storage import CacheCorruptionError

        with tempfile.TemporaryDirectory() as directory:
            with ContentAddressedHttpCache(directory) as cache:
                entry = cache.put("key", "https://example.test/a", 200, {}, b"abc", ts(1))
                path = cache._blob_path(entry.content_hash)
                path.write_bytes(b"tampered")
                with self.assertRaises(CacheCorruptionError):
                    cache.get("key")

    def test_v6_factor_table_migrates_and_round_trips_raw_report_factors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "audit.sqlite3"
            connection = sqlite3.connect(database)
            connection.execute(
                """
                CREATE TABLE factor_observations (
                    as_of TEXT NOT NULL,
                    stock_id TEXT NOT NULL,
                    macro_objective_factor REAL,
                    macro_report_factor REAL,
                    industry_objective_factor REAL,
                    industry_report_factor REAL,
                    stock_objective_factor REAL,
                    stock_report_factor REAL,
                    macro_industry_interaction REAL,
                    industry_stock_interaction REAL,
                    source_snapshot_hash TEXT NOT NULL,
                    PRIMARY KEY(as_of, stock_id)
                )
                """
            )
            connection.commit()
            connection.close()

            observation = FactorObservation(
                as_of=ts(3),
                stock_id="000333.SZ",
                macro_objective_factor=0.1,
                macro_report_raw=0.11,
                macro_report_factor=0.12,
                industry_objective_factor=0.2,
                industry_report_raw=0.21,
                industry_report_factor=0.22,
                stock_objective_factor=0.3,
                stock_report_raw=0.31,
                stock_report_factor=0.32,
                macro_industry_interaction=0.0264,
                industry_stock_interaction=0.0704,
                source_snapshot_hash="f" * 64,
            )
            with AuditStore(database) as store:
                store.upsert_factor_observation(observation)
                loaded = list(store.iter_factor_observations())
                columns = {
                    row["name"]
                    for row in store._connection.execute(
                        "PRAGMA table_info(factor_observations)"
                    ).fetchall()
                }
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].macro_report_raw, 0.11)
            self.assertEqual(loaded[0].industry_report_raw, 0.21)
            self.assertEqual(loaded[0].stock_report_raw, 0.31)
            self.assertTrue(
                {"macro_report_raw", "industry_report_raw", "stock_report_raw"}
                <= columns
            )

    def test_outcome_preserves_fundamental_and_market_truth_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "audit.sqlite3"
            with AuditStore(database) as store:
                store.upsert_report(report("a", 1))
                claim = ResearchClaim(
                    claim_id="claim-a",
                    report_id="a",
                    dimension="stock",
                    subject_id="000333.SZ",
                    target_type="EPS",
                    direction=0,
                    value_min=Decimal("1.2"),
                    value_max=Decimal("1.2"),
                    unit="CNY/share",
                    benchmark="annual_report_basic_eps",
                    forecast_period="2024FY",
                    horizon_days=120,
                    available_at=ts(1),
                    evidence_span="预计EPS为1.2元",
                    extractor_version="rules-v1",
                    extraction_confidence=0.99,
                )
                store.upsert_claim(claim)
                store.upsert_outcome(
                    ClaimOutcome(
                        claim_id="claim-a",
                        truth_source="cninfo_first_release",
                        market_truth_source="eastmoney_qfq_daily",
                        market_benchmark_id="BK_TEST_INDUSTRY",
                        market_benchmark_kind="industry",
                        truth_available_at=ts(3),
                        realized_value=Decimal("1.2"),
                        market_return=0.1,
                        benchmark_return=0.05,
                        error=0.0,
                        hit=True,
                        mature=True,
                        evaluated_at=ts(4),
                    )
                )
                loaded = tuple(store.iter_outcomes())[0]
                self.assertEqual(loaded.truth_source, "cninfo_first_release")
                self.assertEqual(loaded.market_truth_source, "eastmoney_qfq_daily")
                self.assertEqual(loaded.market_benchmark_id, "BK_TEST_INDUSTRY")
                self.assertEqual(loaded.market_benchmark_kind, "industry")


class SkillTests(unittest.TestCase):
    def test_future_and_unmatured_outcomes_never_update_skill(self) -> None:
        rows = [
            {"hit": True, "mature": True, "truth_available_at": ts(2), "report_id": "known"},
            {"hit": True, "mature": True, "truth_available_at": ts(20), "report_id": "future"},
            {"hit": True, "mature": False, "truth_available_at": ts(2), "report_id": "immature"},
        ]
        result = estimate_skill(rows, as_of=ts(10), prior_strength=0)
        self.assertEqual(result["raw_observation_count"], 1)
        self.assertEqual(result["source_report_ids"], ("known",))

    def test_consensus_cluster_has_one_unit_total_weight(self) -> None:
        rows = [
            {
                "hit": True,
                "mature": True,
                "truth_available_at": ts(2),
                "report_id": f"r{i}",
                "subject_id": "X",
                "dimension": "stock",
                "target_type": "rating",
                "horizon_days": 20,
                "direction": 1,
            }
            for i in range(10)
        ]
        discounted = estimate_skill(rows, as_of=ts(3), prior_strength=0, consensus_power=1)
        independent = estimate_skill(rows, as_of=ts(3), prior_strength=0, consensus_power=0)
        self.assertAlmostEqual(discounted["total_weight"] * 10, independent["total_weight"])
        self.assertLessEqual(discounted["effective_sample_size"], 1.0)
        self.assertGreater(independent["effective_sample_size"], 9.0)


class ReportingTests(unittest.TestCase):
    def test_market_and_fundamental_hits_are_separate_and_geometric(self) -> None:
        reports = [{"report_id": "r", "dimension": "stock", "broker": "甲"}]
        claims = [{
            "claim_id": "c",
            "report_id": "r",
            "dimension": "stock",
            "subject_id": "X",
            "target_type": "EPS",
            "direction": 1,
            "forecast_period": "2024",
            "horizon_days": 120,
        }]
        outcomes = [{
            "claim_id": "c",
            "truth_source": "cninfo_first_release",
            "realized_value": 2.0,
            "hit": True,
            "mature": True,
            "market_return": 0.10,
            "benchmark_return": 0.05,
        }]
        row = build_accuracy_rows("stock", reports, claims, outcomes)[0]
        self.assertIs(row["fundamental_hit"], True)
        self.assertIs(row["market_hit"], True)
        self.assertAlmostEqual(row["market_excess_return"], 1.10 / 1.05 - 1.0)

        market_only = [{**outcomes[0], "truth_source": "market_bars", "hit": True}]
        row = build_accuracy_rows("stock", reports, claims, market_only)[0]
        self.assertIsNone(row["fundamental_hit"])
        self.assertIs(row["market_hit"], True)

    def test_accuracy_scoring_requires_the_complete_claim_extraction_contract(self) -> None:
        reports = [{"report_id": "r", "dimension": "stock", "broker": "甲"}]
        outcome = [{
            "claim_id": "c",
            "truth_source": "cninfo_first_release",
            "realized_value": 2.0,
            "hit": True,
            "mature": True,
        }]
        valid_claim = {
            "claim_id": "c",
            "report_id": "r",
            "dimension": "stock",
            "subject_id": "X",
            "target_type": "EPS",
            "direction": 1,
            "forecast_period": "2025FY",
            "horizon_days": 120,
            "extraction_confidence": 0.95,
        }
        valid = build_accuracy_rows("stock", reports, [valid_claim], outcome)[0]
        self.assertTrue(valid["eligible_for_scoring"])
        self.assertEqual(valid["exclusion_reason"], "")

        invalid_claim = {
            **valid_claim,
            "target_type": "",
            "forecast_period": "",
            "horizon_days": 0,
            "extraction_confidence": 0.949,
        }
        invalid_outcome = [{**outcome[0], "exclusion_reason": "source_basis_note"}]
        invalid = build_accuracy_rows(
            "stock", reports, [invalid_claim], invalid_outcome
        )[0]
        self.assertFalse(invalid["eligible_for_scoring"])
        reasons = set(invalid["exclusion_reason"].split("|"))
        self.assertIn("source_basis_note", reasons)
        self.assertIn("extraction_confidence_below_threshold", reasons)
        self.assertIn("target_type_missing", reasons)
        self.assertIn("forecast_period_missing", reasons)
        self.assertIn("horizon_days_missing_or_invalid", reasons)

    def test_factor_status_is_bound_to_primary_walk_forward_admission(self) -> None:
        observation = {
            "as_of": "2026-08-04T00:00:00+08:00",
            "stock_id": "000333.SZ",
            "macro_objective_factor": 0.1,
            "macro_report_raw": 0.11,
            "macro_report_factor": 0.12,
            "industry_objective_factor": 0.2,
            "industry_report_raw": 0.21,
            "industry_report_factor": 0.22,
            "stock_objective_factor": 0.3,
            "stock_report_raw": 0.31,
            "stock_report_factor": 0.32,
            "macro_industry_interaction": 0.0264,
            "industry_stock_interaction": 0.0704,
            "source_snapshot_hash": "f" * 64,
        }
        not_admitted = build_factor_rows([observation])[0]
        self.assertEqual(
            not_admitted["factor_status"],
            "observation_complete_but_not_admitted",
        )
        self.assertIn("walk_forward_not_admitted", not_admitted["exclusion_reason"])

        windows = []
        for index in range(4):
            windows.append({
                "window": index + 1,
                "B0": {"status": "evaluated", "rank_ic": 0.01},
                "B1": {"status": "evaluated", "rank_ic": 0.02},
                "B2": {"status": "evaluated", "rank_ic": 0.03},
                "M1": {
                    "status": "evaluated",
                    "rank_ic": 0.08,
                    "cost_after_group_return": 0.01,
                    "max_industry_contribution_share": 0.25,
                },
            })
        result = {
            "windows": windows,
            "admission": {
                "admitted": True,
                "window_count": 4,
                "mean_m1_rank_ic": 0.08,
                "incremental_window_count": 4,
                "max_industry_contribution_share": 0.25,
            },
        }
        admitted = build_factor_rows(
            [observation], walk_forward_result=result
        )[0]
        self.assertEqual(
            admitted["factor_status"],
            "observation_complete_but_not_admitted",
        )
        self.assertIn("walk_forward_not_admitted", admitted["exclusion_reason"])

    def test_bundle_is_complete_and_generator_inputs_hash_identically(self) -> None:
        config = {
            "model_id": "broker-report-audit-v1",
            "skill": {"minimum_effective_sample_size_for_ranking": 5},
            "acceptance": {"minimum_extraction_precision": 0.95},
            "deep_read": {"maximum_limit": 20},
        }
        reports = [{
            "report_id": "r",
            "dimension": "stock",
            "broker": "甲",
            "analyst": "乙",
            "title": "可证伪报告",
            "source_url": "https://example.test/report",
        }]
        candidates = [{
            "report_id": "r",
            "dimension": "stock",
            "why_read": "与当前客观信号冲突",
            "might_change": "行业内个股相对排名",
            "decision_sensitivity": 1,
            "conflict_degree": 1,
            "change_degree": 1,
            "source_skill_lower_bound": 0.6,
            "falsifiability": 1,
            "evidence_completeness": 1,
        }]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = write_report_bundle(
                root / "a",
                as_of="2026-08-04",
                command="deep-read",
                config=config,
                reports=reports,
                deep_read_candidates=candidates,
            )
            second = write_report_bundle(
                root / "b",
                as_of="2026-08-04",
                command="deep-read",
                config=config,
                reports=reports,
                deep_read_candidates=(item for item in candidates),
            )
            self.assertEqual(set(first.paths), set(ARTIFACT_FILENAMES))
            self.assertTrue(all(path.is_file() for path in first.paths.values()))
            self.assertEqual(first.run_id, second.run_id)
            self.assertEqual(first.hashes, second.hashes)
            manifest = json.loads(first.paths["run_manifest.json"].read_text(encoding="utf-8"))
            self.assertFalse(manifest["automatic_trading_enabled"])


if __name__ == "__main__":
    unittest.main()
