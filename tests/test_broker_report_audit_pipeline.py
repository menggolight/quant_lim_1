from __future__ import annotations

import csv
from hashlib import sha256
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from research.broker_report_audit.cli import (
    ConfigurationError,
    DEFAULT_CONFIG_PATH,
    PipelineState,
    V1_CONFIG_PATH,
    _apply_validation_manifest_override,
    _build_factor_observations,
    _claims_with_bound_evidence,
    _claims_with_current_evidence,
    _active_extraction_versions,
    _active_extractor_bundle_sha256,
    _evaluate_missing_outcomes,
    _eligible_claims,
    _import_truth_inputs,
    _ingest_market_bars,
    _is_market_truth_source,
    _match_first_release_truths,
    _trusted_outcomes,
    build_factor,
    build_parser,
    deep_read,
    load_config,
    load_trading_calendar,
    run_audit,
)
from research.broker_report_audit.extractors import RuleBasedExtractor
from research.broker_report_audit.factors import (
    MODEL_FEATURES,
    build_factor_components,
    build_model_feature_sets,
    rank_deep_reads,
    walk_forward_evaluate,
)
from research.broker_report_audit.skills import build_skill_snapshots
from research.broker_report_audit.models import (
    CHINA_TZ,
    ClaimOutcome,
    DailyBar,
    ResearchClaim,
    ResearchReport,
    SkillSnapshot,
    TruthObservation,
)
from research.broker_report_audit.reporting import ARTIFACT_FILENAMES, write_report_bundle
from research.market_data import MarketDataBatch, MarketDataRegistry
from research.broker_report_audit.sources import EastmoneySource, HttpResponse
from research.broker_report_audit.storage import (
    AuditStore,
    CacheCollisionError,
    ExtractionCache,
)


def at(day: date, hour: int = 8) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=CHINA_TZ).replace(hour=hour)


def make_report(
    report_id: str,
    dimension: str = "stock",
    subject_id: str = "000333.SZ",
    day: date = date(2024, 1, 5),
    *,
    industry_id: str = "",
    pdf_url: str = "",
) -> ResearchReport:
    published = at(day)
    return ResearchReport(
        report_id=report_id,
        dimension=dimension,
        subject_id=subject_id,
        title="明确预测",
        broker="测试券商",
        analyst="甲",
        published_at=published,
        available_at=published,
        fetched_at=at(date(2024, 2, 1)),
        source="fixture",
        content_hash="a" * 64,
        pdf_sha256="b" * 64,
        industry_id=industry_id,
        rating="买入" if dimension == "stock" else "",
        pdf_url=pdf_url,
        timestamp_quality="date_only_exchange_calendar",
    )


def make_claim(
    report_id: str,
    claim_id: str = "c1",
    subject_id: str = "000333.SZ",
    *,
    benchmark: str = "000300.SH",
    horizon_days: int = 120,
) -> ResearchClaim:
    return ResearchClaim(
        claim_id=claim_id,
        report_id=report_id,
        dimension="stock",
        subject_id=subject_id,
        target_type="stock_rating",
        direction=1,
        value_min=None,
        value_max=None,
        unit="rating",
        benchmark=benchmark,
        forecast_period=f"{horizon_days}TD",
        horizon_days=horizon_days,
        available_at=at(date(2024, 1, 5)),
        evidence_span="结构化评级：买入",
        extractor_version=_active_extraction_versions()[0],
        extraction_confidence=0.995,
        evidence_source_kind="structured/source_record",
        evidence_source_hash="a" * 64,
        evidence_parser_version="source-record-v1",
        evidence_prompt_version="none",
        extractor_bundle_sha256=_active_extractor_bundle_sha256(),
    )


def admitted_config(config_path: Path | str = V1_CONFIG_PATH) -> dict[str, object]:
    # Existing outcome/factor regression fixtures exercise the explicit V1
    # Eastmoney compatibility contract, not the V2 BaoStock default.
    config = load_config(config_path)
    manifest_hash = "f" * 64
    config["acceptance"]["validation_manifest_sha256"] = manifest_hash
    validation = config["acceptance"]["extractor_validation"]
    extractor_version, parser_version, prompt_version = _active_extraction_versions()
    for dimension in ("macro", "industry", "stock"):
        validation[dimension] = {
            "sample_count": 30,
            "metadata_match_rate": 1.0,
            "field_precision": 0.95,
            "passed": True,
            "manifest_sha256": manifest_hash,
            "validation_contract_version": "broker-report-extractor-validation.v3",
            "extractor_version": extractor_version,
            "extractor_bundle_sha256": _active_extractor_bundle_sha256(),
            "parser_version": parser_version,
            "prompt_version": prompt_version,
        }
    return config


def make_bar(
    instrument_id: str,
    day: date,
    open_price: int,
    close_price: int,
    *,
    source: str = "eastmoney_public.push2his",
) -> DailyBar:
    available = at(day, 16)
    return DailyBar(
        instrument_id=instrument_id,
        trade_date=day,
        open=open_price,
        high=max(open_price, close_price),
        low=min(open_price, close_price),
        close=close_price,
        volume=100,
        amount=1000,
        adjusted_open=open_price,
        adjusted_high=max(open_price, close_price),
        adjusted_low=min(open_price, close_price),
        adjusted_close=close_price,
        available_at=available,
        source=source,
        fetched_at=available,
        content_hash=(instrument_id.encode("utf-8").hex() + "0" * 64)[:64],
    )


class _FakeClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = 0

    def get(self, *_args, **_kwargs) -> HttpResponse:
        self.calls += 1
        body = json.dumps(self.payload, ensure_ascii=False).encode("utf-8")
        return HttpResponse(
            url="https://reportapi.eastmoney.com/report/list",
            status=200,
            headers={"content-type": "application/json; charset=utf-8"},
            body=body,
            fetched_at=at(date(2024, 2, 1)),
            content_hash="b" * 64,
            from_cache=False,
        )


class SourceAndExtractionTests(unittest.TestCase):
    def test_date_only_report_requires_exchange_calendar_for_formal_scoring(self) -> None:
        payload = {
            "data": [
                {
                    "title": "国庆节前报告",
                    "orgSName": "华泰证券",
                    "publishDate": "2024-09-30 00:00:00",
                    "infoCode": "HOLIDAY",
                    "stockCode": "000333",
                    "stockName": "美的集团",
                    "researcher": "分析师甲",
                    "sRatingName": "买入",
                }
            ],
            "TotalPage": 1,
            "currentYear": 2024,
        }
        decision = at(date(2024, 10, 8), 10)
        verified = EastmoneySource(
            _FakeClient(payload),
            trading_calendar=[date(2024, 10, 8)],
            trading_calendar_verified=True,
        ).fetch_reports(
            "stock", "2024-09-30", "2024-10-08", as_of=decision
        )[0]
        self.assertEqual(
            verified.available_at,
            datetime(2024, 10, 8, 9, 30, tzinfo=CHINA_TZ),
        )
        self.assertEqual(verified.timestamp_quality, "date_only_exchange_calendar")

        unverified = EastmoneySource(_FakeClient(payload)).fetch_reports(
            "stock", "2024-09-30", "2024-10-08", as_of=decision
        )[0]
        self.assertEqual(
            unverified.available_at,
            datetime(2024, 10, 1, 9, 30, tzinfo=CHINA_TZ),
        )
        self.assertEqual(unverified.timestamp_quality, "date_only_calendar_unverified")
        claim = replace(
            make_claim(unverified.report_id),
            subject_id=unverified.subject_id,
            available_at=unverified.available_at,
            evidence_source_hash=unverified.content_hash,
        )
        self.assertEqual(
            _eligible_claims([claim], admitted_config(), reports=[unverified]), []
        )
        self.assertEqual(
            _claims_with_current_evidence([claim], reports=[unverified]), []
        )
        self.assertEqual(
            _claims_with_bound_evidence([claim], reports=[unverified]), [claim]
        )

    def test_old_extractor_claim_cannot_borrow_current_validation_gate(self) -> None:
        config = admitted_config()
        report = make_report("version-gated-report")
        current = make_claim(report.report_id)
        obsolete = replace(current, extractor_version="obsolete-buggy-v0")

        self.assertEqual(
            _eligible_claims([obsolete], config, reports=[report]), []
        )
        self.assertEqual(
            _eligible_claims([current], config, reports=[report]), [current]
        )

    def test_future_date_only_row_does_not_discard_prior_valid_reports(self) -> None:
        payload = {
            "data": [
                {
                    "title": "已可执行报告",
                    "orgSName": "华泰证券",
                    "publishDate": "2024-01-04 00:00:00",
                    "infoCode": "VALID",
                    "stockCode": "000333",
                    "stockName": "美的集团",
                    "researcher": "分析师甲",
                },
                {
                    "title": "尚未可执行报告",
                    "orgSName": "华泰证券",
                    "publishDate": "2024-01-05 00:00:00",
                    "infoCode": "FUTURE",
                    "stockCode": "000333",
                    "stockName": "美的集团",
                    "researcher": "分析师甲",
                },
            ],
            "TotalPage": 1,
            "currentYear": 2024,
        }
        reports = EastmoneySource(_FakeClient(payload)).fetch_reports(
            "stock",
            "2024-01-01",
            "2024-01-08",
            as_of=at(date(2024, 1, 8), 8),
        )
        self.assertEqual([report.report_id for report in reports], ["eastmoney:stock:VALID"])

    def test_eastmoney_fixture_normalises_structured_stock_report(self) -> None:
        payload = {
            "data": [
                {
                    "title": "公司盈利预测",
                    "orgSName": "华泰证券",
                    "publishDate": "2024-01-05 00:00:00",
                    "infoCode": "INFO1",
                    "stockCode": "000333",
                    "stockName": "美的集团",
                    "researcher": "分析师甲",
                    "sRatingName": "买入",
                    "ratingChange": "上调",
                    "indvAimPriceL": "60",
                    "indvAimPriceT": "65",
                    "predictThisYearEps": "4.50",
                }
            ],
            "TotalPage": 1,
            "currentYear": 2024,
        }
        source = EastmoneySource(_FakeClient(payload))
        reports = source.fetch_reports("stock", "2024-01-01", "2024-01-31")
        self.assertEqual(len(reports), 1)
        item = reports[0]
        self.assertEqual(item.subject_id, "000333")
        self.assertEqual(item.broker, "华泰证券")
        self.assertEqual(item.available_at, datetime(2024, 1, 8, 9, 30, tzinfo=CHINA_TZ))
        claims = RuleBasedExtractor().extract(item)
        self.assertEqual(
            {claim.target_type for claim in claims},
            {"stock_rating", "rating_change", "target_price", "EPS"},
        )

    def test_structured_rating_change_uses_only_explicit_enum_values(self) -> None:
        expected = {"上调": 1, "维持": 0, "下调": -1}
        for index, (raw_change, direction) in enumerate(expected.items()):
            with self.subTest(rating_change=raw_change):
                report = replace(
                    make_report(f"rating-change-{index}"),
                    rating_change=raw_change,
                )
                claims = RuleBasedExtractor().extract(report)
                change_claims = [
                    claim for claim in claims if claim.target_type == "rating_change"
                ]
                self.assertEqual(len(change_claims), 1)
                self.assertEqual(change_claims[0].direction, direction)
                if direction == 0:
                    self.assertEqual(str(change_claims[0].value_min), "0")
                    self.assertEqual(str(change_claims[0].value_max), "0")
                self.assertIn("stock_rating", {claim.target_type for claim in claims})

        for raw_change in ("", "首次覆盖", "未知", "watch"):
            with self.subTest(rating_change=raw_change):
                report = replace(
                    make_report(f"rating-change-unknown-{raw_change or 'empty'}"),
                    rating_change=raw_change,
                )
                claims = RuleBasedExtractor().extract(report)
                self.assertNotIn("rating_change", {claim.target_type for claim in claims})
                self.assertIn("stock_rating", {claim.target_type for claim in claims})

    def test_claim_id_binds_full_extracted_semantics(self) -> None:
        report = replace(make_report("semantic-id"), rating_change="上调")
        positive = next(
            claim
            for claim in RuleBasedExtractor().extract(report)
            if claim.target_type == "rating_change"
        )
        # Hold report bytes, evidence text, target and extractor version fixed;
        # a semantic direction change must still produce a different identity.
        with patch(
            "research.broker_report_audit.extractors._rating_change_direction",
            return_value=-1,
        ):
            negative = next(
                claim
                for claim in RuleBasedExtractor().extract(report)
                if claim.target_type == "rating_change"
            )
        self.assertEqual(positive.evidence_span, negative.evidence_span)
        self.assertNotEqual(positive.direction, negative.direction)
        self.assertNotEqual(positive.claim_id, negative.claim_id)

    def test_rule_extractor_rejects_vague_and_conditional_prose(self) -> None:
        report = make_report("macro-1", dimension="macro", subject_id="macro")
        extractor = RuleBasedExtractor(
            parser_version=(
                f"pypdf-test+{_active_extraction_versions()[0]}"
            ),
            prompt_version="none",
        )
        claims = extractor.extract(
            report,
            "长期向好，政策支持。若外部冲击消失，预计2024年CPI同比增长2.5%。",
        )
        self.assertEqual(claims, ())
        explicit = extractor.extract(report, "预计2024年CPI同比增长2.5%。")
        self.assertEqual(len(explicit), 1)
        self.assertEqual(explicit[0].target_type, "CPI")
        self.assertEqual(str(explicit[0].value_min), "2.5")

    def test_extraction_cache_is_keyed_by_pdf_parser_and_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with ExtractionCache(directory) as cache:
                first = cache.put("a" * 64, "pypdf-1", "none", b"payload")
                replay = cache.get("a" * 64, "pypdf-1", "none")
                self.assertEqual(replay.payload, b"payload")  # type: ignore[union-attr]
                self.assertEqual(first.cache_key, replay.cache_key)  # type: ignore[union-attr]
                with self.assertRaises(CacheCollisionError):
                    cache.put("a" * 64, "pypdf-1", "none", b"different")


class FactorPipelineTests(unittest.TestCase):
    def test_market_truth_time_not_late_evaluation_controls_skill_visibility(self) -> None:
        report = ResearchReport(
            report_id="historical-skill-report",
            dimension="stock",
            subject_id="000333.SZ",
            title="历史评级",
            broker="测试券商",
            analyst="甲",
            published_at=at(date(2023, 1, 5)),
            available_at=at(date(2023, 1, 5)),
            fetched_at=at(date(2024, 1, 5)),
            source="fixture",
            content_hash="b" * 64,
            industry_id="BK_TEST_INDUSTRY",
        )
        claim = ResearchClaim(
            claim_id="historical-skill-claim",
            report_id=report.report_id,
            dimension="stock",
            subject_id="000333.SZ",
            target_type="stock_rating",
            direction=1,
            value_min=None,
            value_max=None,
            unit="rating",
            benchmark="BK_TEST_INDUSTRY",
            forecast_period="120TD",
            horizon_days=120,
            available_at=at(date(2023, 1, 5)),
            evidence_span="结构化评级：买入",
            extractor_version=_active_extraction_versions()[0],
            extraction_confidence=0.995,
            evidence_source_kind="structured/source_record",
            evidence_source_hash=report.content_hash,
            evidence_parser_version="source-record-v1",
            evidence_prompt_version="none",
            extractor_bundle_sha256=_active_extractor_bundle_sha256(),
        )
        outcome = ClaimOutcome(
            claim_id=claim.claim_id,
            truth_source="market_bars",
            truth_available_at=at(date(2023, 6, 30), 16),
            realized_value=0.08,
            market_return=0.10,
            benchmark_return=0.02,
            error=0.0,
            hit=True,
            mature=True,
            evaluated_at=at(date(2026, 8, 4), 23),
            market_hit=True,
            market_excess_return=0.08,
            market_truth_source="eastmoney_public.push2his",
            market_benchmark_id="BK_TEST_INDUSTRY",
            market_benchmark_kind="industry",
        )
        config = admitted_config()
        with patch(
            "research.broker_report_audit.cli._market_outcome_matches_current_evidence",
            return_value=True,
        ):
            trusted = _trusted_outcomes([outcome], [claim], config)
        self.assertEqual(trusted[0]["truth_available_at"], outcome.truth_available_at)
        snapshots = build_skill_snapshots(
            trusted,
            [claim],
            [report],
            as_of=at(date(2025, 1, 1), 23),
        )
        self.assertEqual(len(snapshots), 1)

    def test_cli_passes_only_trusted_nonempty_outcomes_to_factor_builder(self) -> None:
        config = admitted_config()
        source_report = make_report("trusted-factor", industry_id="BK_TEST_INDUSTRY")
        claim = make_claim(source_report.report_id, claim_id="trusted-factor-claim")
        outcome = ClaimOutcome(
            claim_id=claim.claim_id,
            truth_source="market_bars",
            truth_available_at=at(date(2024, 1, 9), 16),
            realized_value=0.08,
            market_return=0.10,
            benchmark_return=0.02,
            error=0.0,
            hit=True,
            mature=True,
            evaluated_at=at(date(2024, 2, 1), 23),
            market_hit=True,
            market_excess_return=0.08,
            market_truth_source="eastmoney_public.push2his",
            market_benchmark_id="BK_TEST_INDUSTRY",
            market_benchmark_kind="industry",
        )
        state = PipelineState(
            reports=[source_report],
            claims=[claim],
            outcomes=[outcome],
            skill_snapshots=[],
            factor_observations=[],
        )

        class Sink:
            def upsert_factor_observations(self, rows: object) -> None:
                self.rows = rows

        sentinel = object()
        issues: list[dict[str, object]] = []
        with patch(
            "research.broker_report_audit.factors.build_factor_observations",
            return_value=[sentinel],
        ) as builder, patch(
            "research.broker_report_audit.cli._market_outcome_matches_current_evidence",
            return_value=True,
        ):
            result = _build_factor_observations(
                Sink(),
                state=state,
                config=config,
                decision=at(date(2024, 2, 1), 23),
                issues=issues,
                specifications=[{"stock_id": "000333.SZ"}],
            )
        self.assertEqual(result, [sentinel])
        self.assertTrue(builder.call_args.kwargs["outcomes_are_trusted"])
        self.assertEqual(len(builder.call_args.kwargs["outcomes"]), 1)

    def test_skill_factor_is_zero_until_lower_bound_beats_chance(self) -> None:
        report = make_report("r1")
        claim = replace(
            make_claim("r1"),
            target_type="rating_change",
            unit="rating_change",
            evidence_span="结构化评级变化：上调",
        )
        weak = SkillSnapshot(
            as_of=at(date(2024, 1, 4)),
            broker="测试券商",
            analyst="甲",
            team="",
            dimension="stock",
            target_type="rating_change",
            horizon_days=120,
            posterior_skill=0.60,
            conservative_lower_bound=0.50,
            effective_sample_size=10,
            source_report_ids=("old",),
        )
        strong = SkillSnapshot(
            as_of=at(date(2024, 1, 4)),
            broker="测试券商",
            analyst="甲",
            team="",
            dimension="stock",
            target_type="rating_change",
            horizon_days=120,
            posterior_skill=0.75,
            conservative_lower_bound=0.60,
            effective_sample_size=20,
            source_report_ids=("old2",),
        )
        common = dict(
            as_of=at(date(2024, 1, 10), 18),
            stock_id="000333.SZ",
            stock_claims=[claim],
            reports=[report],
            macro_objective_factor=0.1,
            industry_objective_factor=0.2,
            stock_objective_factor=0.3,
        )
        weak_row = build_factor_components(**common, skill_snapshots=[weak])
        strong_row = build_factor_components(**common, skill_snapshots=[strong])
        self.assertEqual(weak_row["stock_report_factor"], 0.0)
        self.assertAlmostEqual(strong_row["stock_report_factor"], 0.20)
        self.assertNotEqual(weak_row["source_snapshot_hash"], strong_row["source_snapshot_hash"])

    def test_absolute_rating_and_eps_remain_audit_only_not_factor_inputs(self) -> None:
        report = make_report("absolute-only")
        rating_claim = make_claim(report.report_id, claim_id="absolute-rating")
        eps_claim = replace(
            rating_claim,
            claim_id="absolute-eps",
            target_type="EPS",
            direction=0,
            value_min=Decimal("1.2"),
            value_max=Decimal("1.2"),
            unit="CNY/share",
            forecast_period="2024FY",
            evidence_span="结构化2024年EPS预测：1.2",
        )
        row = build_factor_components(
            as_of=at(date(2024, 1, 10), 18),
            stock_id=report.subject_id,
            stock_claims=[rating_claim, eps_claim],
            reports=[report],
            macro_objective_factor=0.1,
            industry_objective_factor=0.2,
            stock_objective_factor=0.3,
        )
        self.assertIsNone(row["stock_report_raw"])
        self.assertIsNone(row["stock_report_factor"])
        exclusions = "|".join(row["exclusions"])
        self.assertIn("not_admissible_stock_change_signal:stock_rating", exclusions)
        self.assertIn("not_admissible_stock_change_signal:eps", exclusions)

    def test_explicit_maintain_rating_change_is_a_neutral_not_missing_signal(self) -> None:
        report = make_report("maintain-rating")
        claim = ResearchClaim(
            claim_id="maintain-rating-claim",
            report_id=report.report_id,
            dimension="stock",
            subject_id=report.subject_id,
            target_type="rating_change",
            direction=0,
            value_min="0",
            value_max="0",
            unit="rating_change",
            benchmark="",
            forecast_period="120TD",
            horizon_days=120,
            available_at=report.available_at,
            evidence_span="结构化评级变化：维持",
            extractor_version=_active_extraction_versions()[0],
            extraction_confidence=0.995,
            evidence_source_kind="structured/source_record",
            evidence_source_hash=report.content_hash,
            evidence_parser_version="source-record-v1",
            evidence_prompt_version="none",
            extractor_bundle_sha256=_active_extractor_bundle_sha256(),
        )
        snapshot = SkillSnapshot(
            as_of=at(date(2024, 1, 4)),
            broker="测试券商",
            analyst="甲",
            team="",
            dimension="stock",
            target_type="rating_change",
            horizon_days=120,
            posterior_skill=0.85,
            conservative_lower_bound=0.80,
            effective_sample_size=20,
            source_report_ids=("prior-rating-change",),
        )
        row = build_factor_components(
            as_of=at(date(2024, 1, 10), 18),
            stock_id=report.subject_id,
            stock_claims=[claim],
            reports=[report],
            skill_snapshots=[snapshot],
            macro_objective_factor=0.1,
            industry_objective_factor=0.2,
            stock_objective_factor=0.3,
        )
        self.assertEqual(row["stock_report_raw"], 0.0)
        self.assertEqual(row["stock_report_factor"], 0.0)

    def test_all_models_keep_objective_stock_factor_and_no_three_way_interaction(self) -> None:
        observation = {
            "macro_objective_factor": 0.1,
            "industry_objective_factor": 0.2,
            "stock_objective_factor": 0.3,
            "macro_report_raw": 0.1,
            "industry_report_raw": 0.2,
            "stock_report_raw": 0.3,
            "macro_report_factor": 0.1,
            "industry_report_factor": 0.2,
            "stock_report_factor": 0.3,
            "macro_industry_interaction": 0.02,
            "industry_stock_interaction": 0.06,
        }
        feature_sets = build_model_feature_sets(observation)
        for model_name, features in feature_sets.items():
            self.assertIsNotNone(features, model_name)
            self.assertIn("stock_objective_factor", features)  # type: ignore[operator]
            self.assertNotIn("macro_industry_stock_interaction", MODEL_FEATURES[model_name])

    def test_deep_read_ranking_emits_reporting_contract(self) -> None:
        first = make_report("r1", day=date(2024, 1, 5))
        second = make_report("r2", subject_id="000001.SZ", day=date(2024, 1, 6))
        rows = rank_deep_reads(
            [first, second],
            [make_claim("r1", "c1"), make_claim("r2", "c2", "000001.SZ")],
            [],
            as_of=at(date(2024, 2, 1), 18),
            decision_sensitivity={"r1": 1.0, "r2": 0.5},
        )
        self.assertEqual(rows[0]["report_id"], "r1")
        self.assertTrue(rows[0]["why_read"])
        self.assertTrue(rows[0]["might_change"])

    def test_walk_forward_keeps_final_twelve_months_frozen(self) -> None:
        rows = []
        current = date(2019, 1, 1)
        end = date(2025, 6, 30)
        index = 0
        while current <= end:
            if current.weekday() < 5:
                time_component = ((index % 41) - 20) / 100.0
                for stock in range(5):
                    x = (stock - 2) / 2.0 + time_component
                    rows.append(
                        {
                            "as_of": current.isoformat(),
                            "stock_id": f"S{stock}",
                            "industry_id": f"I{stock % 4}",
                            "target_return_20d": x * 0.03,
                            "macro_objective_factor": time_component,
                            "industry_objective_factor": x * 0.8,
                            "stock_objective_factor": x * 0.6,
                            "macro_report_raw": x * 0.4,
                            "industry_report_raw": x * 0.3,
                            "stock_report_raw": x * 0.2,
                            "macro_report_factor": x * 0.4,
                            "industry_report_factor": x * 0.3,
                            "stock_report_factor": x * 0.2,
                            "macro_industry_interaction": time_component * x * 0.3,
                            "industry_stock_interaction": x * x * 0.06,
                        }
                    )
                index += 1
            current += timedelta(days=1)
        result = walk_forward_evaluate(rows, frozen_months=12, purge_embargo_days=120)
        frozen = result["frozen_test"]
        self.assertTrue(frozen["frozen"])
        self.assertEqual(frozen["M1"]["status"], "evaluated")
        self.assertEqual(frozen["M1"]["alpha_source"], "development_validation_windows")
        self.assertGreaterEqual(len(result["windows"]) + 1, 4)


class CliMarketOutcomeRegressionTests(unittest.TestCase):
    def test_admitted_status_label_cannot_unlock_unbound_local_truth(self) -> None:
        source_report = make_report("receipt-binding")
        claim = make_claim(source_report.report_id)
        observation = TruthObservation(
            claim_id=claim.claim_id,
            dimension=claim.dimension,
            subject_id=claim.subject_id,
            target_type=claim.target_type,
            forecast_period=claim.forecast_period,
            unit=claim.unit,
            basis=claim.benchmark,
            realized_value=Decimal("1.2"),
            truth_source="cninfo_first_disclosure",
            available_at=at(date(2024, 1, 15), 10),
            fetched_at=at(date(2024, 1, 15), 11),
            first_release=True,
            revision=False,
            content_hash="d" * 64,
            evidence_url="https://www.cninfo.com.cn/example.pdf",
            evidence_verified=True,
        )
        issues: list[dict[str, object]] = []
        with patch(
            "research.broker_report_audit.official_truth.OFFICIAL_ADMISSION_STATUS",
            "admitted",
        ):
            matched = _match_first_release_truths(
                [claim], [observation], admitted_config(), issues
            )
        self.assertEqual(matched, {})
        self.assertIn(
            "OFFICIAL_TRUTH_RECEIPT_BINDING_NOT_IMPLEMENTED",
            {item["code"] for item in issues},
        )

    def test_truth_inputs_are_append_only_but_local_rows_cannot_score(self) -> None:
        parsed = build_parser().parse_args(
            ["audit", "--truth-input", "a.json", "--truth-input", "b.csv"]
        )
        self.assertEqual(parsed.truth_input, ["a.json", "b.csv"])

        config = admitted_config()
        decision = at(date(2024, 2, 1), 23)
        source_report = make_report("truth-report", industry_id="BK_TEST_INDUSTRY")
        claim = ResearchClaim(
            claim_id="truth-claim",
            report_id=source_report.report_id,
            dimension="stock",
            subject_id="000333.SZ",
            target_type="EPS",
            direction=0,
            value_min="1.2",
            value_max="1.2",
            unit="CNY/share",
            benchmark="annual_report_basic_eps",
            forecast_period="2024FY",
            horizon_days=2,
            available_at=at(date(2024, 1, 5)),
            evidence_span="预计2024FY EPS为1.2元",
            extractor_version=_active_extraction_versions()[0],
            extraction_confidence=0.995,
            evidence_source_kind="structured/source_record",
            evidence_source_hash=source_report.content_hash,
            evidence_parser_version="source-record-v1",
            evidence_prompt_version="none",
            extractor_bundle_sha256=_active_extractor_bundle_sha256(),
        )
        base = {
            "claim_id": claim.claim_id,
            "dimension": "stock",
            "subject_id": claim.subject_id,
            "target_type": claim.target_type,
            "forecast_period": claim.forecast_period,
            "unit": claim.unit,
            "basis": claim.benchmark,
            "realized_value": 1.2,
            "truth_source": "cninfo_first_release",
            "available_at": "2024-01-10T10:00:00+08:00",
            "fetched_at": "2024-02-01T12:00:00+08:00",
            "first_release": True,
            "revision": False,
            "content_hash": sha256(b"first-release-evidence").hexdigest(),
            "evidence_url": "https://www.cninfo.com.cn/first",
            "evidence_path": "first-release.bin",
        }
        revision = {
            **base,
            "realized_value": 9.9,
            "available_at": "2024-01-15T10:00:00+08:00",
            "first_release": False,
            "revision": True,
            "content_hash": sha256(b"revision-evidence").hexdigest(),
            "evidence_url": "https://www.cninfo.com.cn/revision",
            "evidence_path": "revision.bin",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "first-release.bin").write_bytes(b"first-release-evidence")
            (root / "revision.bin").write_bytes(b"revision-evidence")
            truth_path = root / "truth.json"
            truth_path.write_text(json.dumps([base, revision]), encoding="utf-8")
            with AuditStore(root / "audit.sqlite3", decision_time=decision) as store:
                store.upsert_report(source_report)
                store.upsert_claim(claim)
                issues: list[dict[str, object]] = []
                _import_truth_inputs(
                    store,
                    config=config,
                    paths=[truth_path],
                    issues=issues,
                )
                _import_truth_inputs(
                    store,
                    config=config,
                    paths=[truth_path],
                    issues=issues,
                )
                self.assertEqual(
                    len(tuple(store.iter_truth_observations(first_release=None))),
                    2,
                )
                # Even direct immutable rows with official-looking names and
                # evidence_verified=True remain caller assertions.  They must
                # not unlock scoring without a source-owned receipt transport.
                store.insert_truth_observations(
                    (
                        TruthObservation(
                            claim_id=claim.claim_id,
                            dimension=claim.dimension,
                            subject_id=claim.subject_id,
                            target_type=claim.target_type,
                            forecast_period=claim.forecast_period,
                            unit=claim.unit,
                            basis=claim.benchmark,
                            realized_value=base["realized_value"],
                            truth_source=base["truth_source"],
                            available_at=datetime.fromisoformat(base["available_at"]),
                            fetched_at=datetime.fromisoformat(base["fetched_at"]),
                            first_release=True,
                            revision=False,
                            content_hash=base["content_hash"],
                            evidence_url=base["evidence_url"],
                            evidence_verified=True,
                        ),
                        TruthObservation(
                            claim_id=claim.claim_id,
                            dimension=claim.dimension,
                            subject_id=claim.subject_id,
                            target_type=claim.target_type,
                            forecast_period=claim.forecast_period,
                            unit=claim.unit,
                            basis=claim.benchmark,
                            realized_value=revision["realized_value"],
                            truth_source=revision["truth_source"],
                            available_at=datetime.fromisoformat(revision["available_at"]),
                            fetched_at=datetime.fromisoformat(revision["fetched_at"]),
                            first_release=False,
                            revision=True,
                            content_hash=revision["content_hash"],
                            evidence_url=revision["evidence_url"],
                            evidence_verified=True,
                        ),
                    )
                )
                _evaluate_missing_outcomes(
                    store,
                    reports=[source_report],
                    claims=[claim],
                    outcomes=[],
                    config=config,
                    decision=decision,
                    issues=issues,
                )
                outcome = tuple(store.iter_outcomes())[0]
        self.assertFalse(outcome.mature)
        self.assertIsNone(outcome.realized_value)
        self.assertIsNone(outcome.fundamental_hit)
        self.assertIn(
            "OFFICIAL_TRUTH_RECEIPT_REQUIRED",
            {item["code"] for item in issues},
        )
        self.assertIn(
            "TRUTH_REVISIONS_STORED_BUT_EXCLUDED",
            {item["code"] for item in issues},
        )

    def test_complete_market_outcome_is_recomputed_from_current_bars(self) -> None:
        config = admitted_config()
        source_report = make_report("complete-report")
        claim = make_claim(source_report.report_id, horizon_days=2)
        complete = {
            "claim_id": claim.claim_id,
            "truth_source": "market_bars",
            "truth_available_at": at(date(2024, 1, 9), 16),
            "realized_value": 0.05,
            "market_return": 0.05,
            "benchmark_return": 0.01,
            "hit": True,
            "market_hit": True,
            "mature": True,
            "exclusion_reason": "",
            "market_truth_source": "eastmoney_public.push2his",
            "market_benchmark_id": "BK_TEST_INDUSTRY",
            "market_benchmark_kind": "industry",
        }
        with patch(
            "research.broker_report_audit.evaluation.evaluate_claims"
        ) as evaluate:
            _evaluate_missing_outcomes(
                object(),
                reports=[source_report],
                claims=[claim],
                outcomes=[complete],
                config=config,
                decision=at(date(2024, 2, 1), 23),
                issues=[],
            )
        evaluate.assert_called_once()

    def test_immature_existing_outcome_is_reevaluated_when_bars_arrive(self) -> None:
        config = admitted_config()
        decision = at(date(2024, 2, 1), 23)
        source_report = make_report("retry-report", industry_id="BK_TEST_INDUSTRY")
        claim = make_claim(
            source_report.report_id,
            claim_id="retry-claim",
            horizon_days=2,
        )
        immature = ClaimOutcome(
            claim_id=claim.claim_id,
            truth_source="",
            truth_available_at=None,
            realized_value=None,
            market_return=None,
            benchmark_return=None,
            error=None,
            hit=None,
            mature=False,
            exclusion_reason="missing_truth",
            evaluated_at=at(date(2024, 1, 10), 23),
        )
        with tempfile.TemporaryDirectory() as directory:
            with AuditStore(Path(directory) / "audit.sqlite3", decision_time=decision) as store:
                store.upsert_report(source_report)
                store.upsert_claim(claim)
                store.upsert_outcome(immature)
                store.upsert_daily_bars(
                    [
                        make_bar("000333.SZ", date(2024, 1, 8), 100, 100),
                        make_bar("000333.SZ", date(2024, 1, 9), 100, 110),
                        make_bar("BK_TEST_INDUSTRY", date(2024, 1, 8), 100, 100),
                        make_bar("BK_TEST_INDUSTRY", date(2024, 1, 9), 100, 102),
                    ]
                )
                issues: list[dict[str, object]] = []
                _evaluate_missing_outcomes(
                    store,
                    reports=[source_report],
                    claims=[claim],
                    outcomes=[immature],
                    config=config,
                    decision=decision,
                    issues=issues,
                )
                result = tuple(store.iter_outcomes())[0]
        self.assertTrue(result.mature)
        self.assertIsNotNone(result.market_hit)
        self.assertEqual(result.market_truth_source, "eastmoney_public.push2his")
        self.assertEqual(result.market_benchmark_kind, "industry")
        self.assertGreater(result.evaluated_at, immature.evaluated_at)

    def test_market_ingestion_fetches_subject_and_csi300_with_explicit_fallback(self) -> None:
        config = admitted_config()
        source_report = make_report("market-report", industry_id="NO_INDEX_MAPPING")
        claim = make_claim(source_report.report_id, benchmark="", horizon_days=2)
        decision = at(date(2024, 2, 1), 23)
        calls: list[str] = []

        class FakeMarketSource:
            def __init__(self, client: object, *, source_name: str) -> None:
                self.client = client
                self.source_name = source_name
                self.last_issues: list[dict[str, object]] = []

            def daily_bars(
                self,
                instrument_id: str,
                start_date: date,
                end_date: date,
                *,
                as_of: datetime,
                **_kwargs: object,
            ) -> tuple[DailyBar, ...]:
                self.end_date = end_date
                self.as_of = as_of
                calls.append(instrument_id)
                return (
                    make_bar(
                        instrument_id,
                        start_date,
                        100,
                        101,
                        source=f"{self.source_name}.push2his",
                    ),
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with AuditStore(root / "audit.sqlite3", decision_time=decision) as store:
                issues: list[dict[str, object]] = []
                with patch(
                    "research.broker_report_audit.sources.EastmoneyMarketSource",
                    FakeMarketSource,
                ):
                    _ingest_market_bars(
                        store,
                        reports=[source_report],
                        claims=[claim],
                        config=config,
                        decision=decision,
                        cache_directory=root / "cache",
                        offline=False,
                        issues=issues,
                    )
                stored = {bar.instrument_id for bar in store.iter_daily_bars()}
        self.assertEqual(set(calls), {"000333.SZ", "000300.SH"})
        self.assertEqual(stored, {"000333.SZ", "000300.SH"})
        self.assertIn(
            "STOCK_INDUSTRY_BENCHMARK_MISSING",
            {item["code"] for item in issues},
        )

    def test_eastmoney_push2his_is_a_configured_market_truth_source(self) -> None:
        config = admitted_config()
        self.assertTrue(
            _is_market_truth_source("eastmoney_public.push2his", config)
        )
        self.assertFalse(_is_market_truth_source("unapproved.vendor", config))

    def test_audit_defers_pdf_downloads_to_bounded_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_report = make_report(
                "pdf-with-structured",
                day=date(2024, 7, 5),
                pdf_url="https://example.test/report.pdf",
            )
            claim = make_claim(source_report.report_id)
            database = root / "audit.sqlite3"
            with AuditStore(database) as store:
                store.upsert_report(source_report)
                store.upsert_claim(claim)
            with patch(
                "research.broker_report_audit.cli._extract_pdf_texts",
                side_effect=AssertionError("audit must not fetch the full PDF population"),
            ) as extract_pdf, patch(
                "research.broker_report_audit.cli._write_state_bundle",
                return_value=None,
            ) as write_bundle:
                run_audit(
                    offline=True,
                    dimensions="stock",
                    as_of="2026-08-04",
                    db_path=database,
                    cache_directory=root / "cache",
                    output_directory=root / "out",
                )
            self.assertFalse(extract_pdf.called)
            issue_codes = {
                item["code"] for item in write_bundle.call_args.kwargs["issues"]
            }
        self.assertIn("PDF_EXTRACTION_DEFERRED_TO_BOUNDED_COMMAND", issue_codes)

    def test_deep_read_pdf_preselection_is_deterministic_and_hard_capped(self) -> None:
        from research.broker_report_audit.cli import _bounded_pdf_candidates

        rows = [
            make_report(
                f"pdf-{index:02d}",
                day=date(2026, 7, 1) + timedelta(days=index),
                pdf_url=f"https://example.test/{index}.pdf",
            )
            for index in range(25)
        ]
        selected = _bounded_pdf_candidates(reversed(rows), limit=100)
        self.assertEqual(len(selected), 20)
        self.assertEqual(selected[0].report_id, "pdf-24")
        self.assertEqual(selected[-1].report_id, "pdf-05")


class MarketDataV2IntegrationTests(unittest.TestCase):
    @staticmethod
    def _batch(request: object, *, batch_id: str) -> MarketDataBatch:
        requested_at = request.requested_at
        available_at = datetime.combine(
            request.start_date,
            datetime.min.time(),
            tzinfo=CHINA_TZ,
        ).replace(hour=15, minute=30)
        return MarketDataBatch(
            batch_id=batch_id,
            provider_id="baostock",
            upstream_source="baostock.query_history_k_data_plus",
            dataset_type="daily_bar",
            schema_version="daily-bar-v1",
            adapter_version="baostock-adapter-v1",
            request_fingerprint=request.fingerprint(
                "baostock", "baostock-adapter-v1"
            ),
            request_payload=request.fingerprint_payload(
                "baostock", "baostock-adapter-v1"
            ),
            retrieval_mode=request.retrieval_mode,
            requested_at=requested_at,
            fetched_at=requested_at,
            available_at_min=available_at,
            available_at_max=available_at,
            raw_content_sha256="a" * 64,
            normalized_content_sha256="b" * 64,
            record_count=1,
            completeness_status="complete",
            freshness_status="historical_backfill",
            admission_status="validated_research_only",
            point_in_time_status="historical_backfill_not_original_capture",
            synthetic=False,
            issues=(),
            records=(
                {
                    "instrument_id": request.instrument_id,
                    "trading_date": request.start_date.isoformat(),
                    "open": "10",
                    "high": "11",
                    "low": "9",
                    "close": "10.5",
                    "preclose": "10",
                    "volume": "100",
                    "amount": "1000",
                    "currency": "CNY",
                    "adjustment": "none",
                    "trading_status": "traded",
                    "available_at": available_at.isoformat(),
                    "availability_status": "policy_estimated",
                    "source_record_id": "d" * 64,
                },
            ),
        )

    def test_default_is_v2_and_v1_requires_explicit_selection(self) -> None:
        default = load_config()
        legacy = load_config(V1_CONFIG_PATH)
        self.assertEqual(DEFAULT_CONFIG_PATH.name, "broker_report_audit.v2.json")
        self.assertEqual(default["model_id"], "broker-report-audit-v2")
        self.assertEqual(default["sources"]["market"]["provider"], "baostock")
        self.assertEqual(default["sources"]["market"]["truth_source_allowlist"], [])
        self.assertEqual(legacy["model_id"], "broker-report-audit-v1")
        self.assertEqual(
            legacy["sources"]["market"]["provider"],
            "eastmoney_public.push2his",
        )

    def test_v2_uses_whole_baostock_batches_and_records_manifest_evidence(self) -> None:
        config = admitted_config(DEFAULT_CONFIG_PATH)
        report = make_report(
            "v2-market",
            subject_id="000333",
            industry_id="BK_FORBIDDEN",
        )
        claim = make_claim(
            report.report_id,
            subject_id="000333",
            benchmark="",
            horizon_days=2,
        )
        decision = at(date(2024, 2, 1), 23)
        captured_at = decision - timedelta(minutes=1)
        requests: list[object] = []

        class FakeRegistry:
            def fetch(self, request: object, *, provider_id: str) -> MarketDataBatch:
                self.provider_id = provider_id
                requests.append(request)
                return MarketDataV2IntegrationTests._batch(
                    request,
                    batch_id=f"batch-{request.instrument_id}",
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with AuditStore(root / "audit.sqlite3", decision_time=decision) as store:
                issues: list[dict[str, object]] = []
                with patch.object(
                    MarketDataRegistry,
                    "configured",
                    return_value=FakeRegistry(),
                ), patch(
                    "research.broker_report_audit.cli._market_data_requested_at",
                    return_value=captured_at,
                ), patch(
                    "research.broker_report_audit.sources.EastmoneyMarketSource",
                    side_effect=AssertionError("V2 must not call Eastmoney market data"),
                ):
                    evidence = _ingest_market_bars(
                        store,
                        reports=[report],
                        claims=[claim],
                        config=config,
                        decision=decision,
                        cache_directory=root / "cache",
                        offline=False,
                        issues=issues,
                    )
                bars = list(store.iter_daily_bars())
            bundle = write_report_bundle(
                root / "out",
                as_of="2024-02-01",
                command="audit",
                config=config,
                report_source={"provider_id": "eastmoney_public_report_sample"},
                market_data_batches=[{**evidence[0], "records": [{"forbidden": True}]}],
            )
            replay_bundle = write_report_bundle(
                root / "other-temp-output",
                as_of="2024-02-01",
                command="audit",
                config=config,
                report_source={"provider_id": "eastmoney_public_report_sample"},
                market_data_batches=[{**evidence[0], "records": [{"different": "ignored"}]}],
            )
            manifest = json.loads(
                bundle.paths["run_manifest.json"].read_text(encoding="utf-8")
            )

        self.assertEqual({request.instrument_id for request in requests}, {"000333.SZ", "000300.SH"})
        self.assertTrue(all(request.retrieval_mode == "historical_backfill" for request in requests))
        self.assertTrue(all(request.requested_at == captured_at for request in requests))
        self.assertTrue(all(request.requested_at != decision for request in requests))
        self.assertEqual(len(evidence), 2)
        self.assertEqual(len(bars), 2)
        self.assertTrue(all("batch=batch-" in bar.source for bar in bars))
        self.assertTrue(all(bar.content_hash == "b" * 64 for bar in bars))
        self.assertNotIn("records", manifest["market_data_batches"][0])
        self.assertEqual(manifest["source_snapshot"]["market_data_batches"], manifest["market_data_batches"])
        self.assertEqual(bundle.run_id, replay_bundle.run_id)
        self.assertEqual(bundle.hashes, replay_bundle.hashes)
        self.assertFalse(_is_market_truth_source(bars[0].source, config))

    def test_v2_provider_failure_is_truthful_and_never_falls_back(self) -> None:
        config = admitted_config(DEFAULT_CONFIG_PATH)
        report = make_report("v2-failure", industry_id="BK_FORBIDDEN")
        claim = make_claim(report.report_id, benchmark="", horizon_days=2)
        decision = at(date(2024, 2, 1), 23)

        class MissingDependency(RuntimeError):
            status = "dependency_missing"
            code = "dependency_missing"

        class FailingRegistry:
            def fetch(self, request: object, *, provider_id: str) -> MarketDataBatch:
                raise MissingDependency("baostock is unavailable")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with AuditStore(root / "audit.sqlite3", decision_time=decision) as store:
                issues: list[dict[str, object]] = []
                with patch.object(
                    MarketDataRegistry,
                    "configured",
                    return_value=FailingRegistry(),
                ), patch(
                    "research.broker_report_audit.sources.EastmoneyMarketSource",
                    side_effect=AssertionError("fallback is forbidden"),
                ):
                    evidence = _ingest_market_bars(
                        store,
                        reports=[report],
                        claims=[claim],
                        config=config,
                        decision=decision,
                        cache_directory=root / "cache",
                        offline=True,
                        issues=issues,
                    )
                self.assertEqual(list(store.iter_daily_bars()), [])
        failures = [item for item in issues if item["code"] == "MARKET_DATA_BATCH_FAILED"]
        self.assertEqual(evidence, [])
        self.assertTrue(failures)
        self.assertTrue(all(item["details"]["status"] == "dependency_missing" for item in failures))
        self.assertTrue(all(item["details"]["retrieval_mode"] == "offline_replay" for item in failures))


class CliContractTests(unittest.TestCase):
    def test_validation_manifest_runtime_anchor_hashes_and_rejects_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "extractor_validation.v3.json"
            path.write_text('{"contract_version":"test"}\n', encoding="utf-8")
            config = load_config()
            digest = sha256(path.read_bytes()).hexdigest()
            with self.assertRaises(ConfigurationError):
                _apply_validation_manifest_override(config, path)
            resolved, anchored_digest = _apply_validation_manifest_override(
                config, path, digest
            )
            self.assertEqual(Path(resolved), path)
            self.assertEqual(anchored_digest, digest)
            self.assertEqual(
                config["acceptance"]["validation_manifest_sha256"], digest
            )
            with self.assertRaises(ConfigurationError):
                _apply_validation_manifest_override(config, path, "0" * 64)

    def test_trading_calendar_loader_is_strict_and_cli_option_is_wired(self) -> None:
        parsed = build_parser().parse_args(
            ["build-factor", "--trading-calendar", "calendar.csv"]
        )
        self.assertEqual(parsed.trading_calendar, "calendar.csv")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {
                "json": root / "calendar.json",
                "jsonl": root / "calendar.jsonl",
                "csv": root / "calendar.csv",
            }
            paths["json"].write_text(
                json.dumps(["2024-01-02", "2024-01-03"]), encoding="utf-8"
            )
            paths["jsonl"].write_text(
                '{"trade_date":"2024-01-02"}\n{"trade_date":"2024-01-03"}\n',
                encoding="utf-8",
            )
            paths["csv"].write_text(
                "trade_date\n2024-01-02\n2024-01-03\n", encoding="utf-8"
            )
            for path in paths.values():
                self.assertEqual(
                    load_trading_calendar(path),
                    (date(2024, 1, 2), date(2024, 1, 3)),
                )
            invalid = root / "invalid.json"
            invalid.write_text(
                json.dumps(["2024-01-02", "2024-01-02"]), encoding="utf-8"
            )
            with self.assertRaises(ConfigurationError):
                load_trading_calendar(invalid)

    def test_v1_skill_and_admission_constants_are_locked(self) -> None:
        config = load_config(V1_CONFIG_PATH)
        config["skill"]["half_life_days"] = 999
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_config(path)

    def test_all_three_commands_write_the_fixed_bundle_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            common = {
                "as_of": "2026-08-04",
                "db_path": root / "audit.sqlite3",
                "cache_directory": root / "cache",
            }
            audit = run_audit(
                offline=True,
                dimensions="macro,industry,stock",
                output_directory=root / "audit",
                **common,
            )
            factor = build_factor(output_directory=root / "factor", **common)
            queue = deep_read(output_directory=root / "deep", limit=20, **common)
            for bundle in (audit, factor, queue):
                self.assertEqual(set(bundle.paths), set(ARTIFACT_FILENAMES))
                self.assertTrue(all(path.exists() for path in bundle.paths.values()))
            with audit.paths["exceptions.csv"].open(encoding="utf-8-sig", newline="") as handle:
                codes = {row["code"] for row in csv.DictReader(handle)}
            self.assertIn("OFFICIAL_TRUTH_SOURCE_NOT_CONFIGURED", codes)
            self.assertIn("NO_SKILL_SNAPSHOTS", codes)
            self.assertIn("NO_FACTOR_OBSERVATIONS", codes)


if __name__ == "__main__":
    unittest.main()
