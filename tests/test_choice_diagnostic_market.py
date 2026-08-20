from __future__ import annotations

import csv
import json
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from research.broker_report_audit.choice_diagnostic import (
    CHOICE_ARTIFACT_FILENAMES,
    DIAGNOSTIC_STATUS,
    ChoiceCollection,
    ChoiceDiagnosticError,
    SlidingWindowRateLimiter,
    build_choice_skill_tables,
    build_reading_queue,
    collect_choice_market_data,
    compute_choice_market_outcomes,
    _reading_candidate_window,
    select_choice_candidate_claims,
    select_pdf_candidates,
    select_recent_pdf_candidates,
    write_choice_diagnostic_bundle,
)
from research.broker_report_audit.cli import _choice_pdf_evidence_span, build_parser
from research.broker_report_audit.extractors import (
    EXTRACTOR_VERSION,
    extractor_bundle_sha256,
)


UTC = timezone.utc
AS_OF = datetime(2026, 8, 11, 15, 59, tzinfo=UTC)


def business_days(start: date, count: int) -> list[date]:
    days = []
    cursor = start
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def claim(
    claim_id: str,
    *,
    report_id: str = "r1",
    subject_id: str = "600000",
    target_type: str = "stock_rating",
    direction: int = 1,
    available_at: str = "2025-01-01T09:30:00+08:00",
) -> dict:
    return {
        "claim_id": claim_id,
        "report_id": report_id,
        "dimension": "stock",
        "subject_id": subject_id,
        "target_type": target_type,
        "direction": direction,
        "value_min": "10" if target_type == "target_price" else None,
        "value_max": "12" if target_type == "target_price" else None,
        "horizon_days": 120,
        "available_at": available_at,
        "evidence_span": "明确预测证据",
    }


def report(
    report_id: str = "r1",
    *,
    broker_code: str = "B1",
    broker: str = "券商甲",
    analyst: str = "分析师甲",
    team: str = "团队甲",
    pdf_sha256: str = "",
    timestamp_quality: str = "source_timestamp",
) -> dict:
    return {
        "report_id": report_id,
        "broker_code": broker_code,
        "broker": broker,
        "analyst": analyst,
        "team": team,
        "title": f"报告 {report_id}",
        "pdf_url": f"https://example.test/{report_id}.pdf",
        "pdf_sha256": pdf_sha256,
        "content_hash": "f" * 64,
        "timestamp_quality": timestamp_quality,
        "metadata": {},
    }


def fake_batch(
    batch_id: str,
    dataset_type: str,
    records: list[dict],
    *,
    instrument_id: str = "",
    adjustment: str = "none",
) -> SimpleNamespace:
    return SimpleNamespace(
        batch_id=batch_id,
        provider_id="choice",
        upstream_source="choice.test",
        dataset_type=dataset_type,
        schema_version=f"{dataset_type}-v1",
        adapter_version="choice-emquantapi-adapter-v2",
        request_fingerprint="1" * 64,
        request_payload={
            "provider_id": "choice",
            "adapter_version": "choice-emquantapi-adapter-v2",
            "dataset_type": dataset_type,
            "schema_version": f"{dataset_type}-v1",
            "instrument_id": instrument_id or None,
            "start_date": "2025-01-01",
            "end_date": "2026-08-11",
            "adjustment": adjustment,
            "parameters": {},
        },
        retrieval_mode="historical_backfill",
        requested_at=AS_OF,
        fetched_at=AS_OF,
        raw_content_sha256="2" * 64,
        normalized_content_sha256="3" * 64,
        record_count=len(records),
        completeness_status="complete",
        freshness_status="historical_backfill",
        admission_status="validated_secondary_not_primary",
        point_in_time_status="historical_backfill_not_original_capture",
        synthetic=False,
        issues=(),
        records=tuple(records),
    )


def bar(day: date, *, open_price: float, close_price: float, status: str = "traded") -> dict:
    return {
        "trading_date": day.isoformat(),
        "open": str(open_price),
        "close": str(close_price),
        "volume": "100" if status != "suspended" else "0",
        "trading_status": status,
        "available_at": f"{day.isoformat()}T15:30:00+08:00",
    }


class FakeRegistry:
    def __init__(self, sessions: list[date]) -> None:
        self.sessions = sessions
        self.requests = []

    def fetch_diagnostic(self, request, *, provider_id):
        self.requests.append(request)
        if provider_id != "choice":
            raise AssertionError("Choice must be explicit")
        if request.dataset_type == "trade_calendar":
            records = [
                {"calendar_date": day.isoformat(), "is_trading_day": True}
                for day in self.sessions
                if request.start_date <= day <= request.end_date
            ]
            result = fake_batch("calendar", "trade_calendar", records)
            result.request_payload["start_date"] = request.start_date.isoformat()
            result.request_payload["end_date"] = request.end_date.isoformat()
            return result
        records = [
            bar(day, open_price=100, close_price=110 if request.instrument_id == "600000.SH" else 105)
            for day in self.sessions
            if request.start_date <= day <= request.end_date
        ]
        result = fake_batch(
            f"bar-{request.instrument_id}-{request.adjustment}",
            "daily_bar",
            records,
            instrument_id=request.instrument_id,
            adjustment=request.adjustment,
        )
        result.request_payload["start_date"] = request.start_date.isoformat()
        result.request_payload["end_date"] = request.end_date.isoformat()
        return result


class ChoiceDiagnosticMarketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sessions = business_days(date(2025, 1, 2), 150)
        self.calendar = fake_batch(
            "calendar",
            "trade_calendar",
            [{"calendar_date": day.isoformat(), "is_trading_day": True} for day in self.sessions],
        )
        self.stock = fake_batch(
            "stock",
            "daily_bar",
            [bar(day, open_price=100, close_price=120) for day in self.sessions],
            instrument_id="600000.SH",
            adjustment="qfq",
        )
        self.benchmark = fake_batch(
            "benchmark",
            "daily_bar",
            [bar(day, open_price=100, close_price=110) for day in self.sessions],
            instrument_id="000300.SH",
            adjustment="none",
        )

    def collection(self, *, stock=None, failures=None) -> ChoiceCollection:
        return ChoiceCollection(
            self.calendar,
            self.benchmark,
            {"600000.SH": stock or self.stock} if stock is not False else {},
            failures or {},
            Path("checkpoint.json"),
        )

    def test_candidate_filter_keeps_only_visible_stock_rating_and_target_price(self):
        values = [
            claim("rating"),
            claim("target", target_type="target_price", direction=0),
            {**claim("industry"), "dimension": "industry"},
            {**claim("eps"), "target_type": "EPS"},
            claim("future", available_at="2027-01-01T00:00:00+08:00"),
        ]
        selected = select_choice_candidate_claims(values, as_of=AS_OF)
        self.assertEqual([item["claim_id"] for item in selected], ["rating", "target"])

    def test_pdf_validation_claim_cannot_duplicate_frozen_diagnostic_population(self):
        structured = {
            **claim("structured"),
            "extractor_version": EXTRACTOR_VERSION,
            "extractor_bundle_sha256": extractor_bundle_sha256(),
            "evidence_source_kind": "structured/source_record",
            "evidence_source_hash": "f" * 64,
            "evidence_parser_version": "source-record-v1",
            "evidence_prompt_version": "none",
        }
        textual = {
            **structured,
            "claim_id": "textual",
            "evidence_source_kind": "textual/pdf",
            "evidence_source_hash": "a" * 64,
            "evidence_parser_version": "pypdf-test",
        }
        selected = select_choice_candidate_claims(
            [structured, textual], as_of=AS_OF, reports=[report()]
        )
        self.assertEqual([item["claim_id"] for item in selected], ["structured"])

    def test_outcomes_use_next_session_qfq_and_geometric_csi300_excess(self):
        values = [
            claim("rating"),
            claim("target", target_type="target_price", direction=0),
        ]
        rows = compute_choice_market_outcomes(
            values,
            [report()],
            self.collection(),
            as_of=AS_OF,
        )
        by_id = {row["claim_id"]: row for row in rows}
        rating = by_id["rating"]
        self.assertEqual(rating["t0"], self.sessions[0].isoformat())
        self.assertEqual(rating["end_date"], self.sessions[119].isoformat())
        self.assertEqual(rating["outcome_status"], "success")
        self.assertAlmostEqual(
            rating["geometric_excess_return"],
            (1.2 / 1.1) - 1.0,
        )
        self.assertTrue(rating["market_hit"])
        target = by_id["target"]
        self.assertEqual(target["outcome_status"], "excluded")
        self.assertEqual(target["exclusion_reason"], "target_price_direction_missing")
        self.assertEqual(
            target["absolute_target_achievement"],
            "absolute_target_achievement_not_evaluated",
        )
        self.assertIsNotNone(target["geometric_excess_return"])
        self.assertFalse(target["truth_eligible"])

    def test_date_only_candidate_open_is_calendar_confirmed_not_advanced_twice(self):
        date_only_claim = claim(
            "date-only", available_at="2025-01-02T09:30:00+08:00"
        )
        date_only_report = report(
            timestamp_quality="date_only_calendar_unverified"
        )
        date_only = compute_choice_market_outcomes(
            [date_only_claim],
            [date_only_report],
            self.collection(),
            as_of=AS_OF,
        )[0]
        self.assertEqual(date_only["t0"], self.sessions[0].isoformat())

        precise = compute_choice_market_outcomes(
            [date_only_claim],
            [report(timestamp_quality="source_timestamp")],
            self.collection(),
            as_of=AS_OF,
        )[0]
        self.assertEqual(precise["t0"], self.sessions[1].isoformat())

    def test_suspension_and_network_failure_are_explicit_not_silent(self):
        suspended_records = list(self.stock.records)
        suspended_records[0] = bar(
            self.sessions[0], open_price=100, close_price=100, status="suspended"
        )
        suspended = fake_batch(
            "suspended",
            "daily_bar",
            suspended_records,
            instrument_id="600000.SH",
            adjustment="qfq",
        )
        row = compute_choice_market_outcomes(
            [claim("s")], [report()], self.collection(stock=suspended), as_of=AS_OF
        )[0]
        self.assertEqual(row["exclusion_reason"], "entry_session_suspended_or_missing")

        failed = self.collection(
            stock=False,
            failures={
                "600000.SH": {
                    "status": "network_blocked",
                    "code": "10002003",
                    "message": "timeout",
                }
            },
        )
        row = compute_choice_market_outcomes(
            [claim("f")], [report()], failed, as_of=AS_OF
        )[0]
        self.assertEqual(row["exclusion_reason"], "stock_batch_unavailable:network_blocked")

    def test_consensus_discount_and_ess_gate_are_visible(self):
        reports = []
        independent = []
        for index in range(6):
            reports.append(report(f"r{index}"))
            independent.append(
                claim(
                    f"c{index}",
                    report_id=f"r{index}",
                    subject_id=f"6000{index:02d}",
                    available_at=f"2025-01-{index + 1:02d}T09:30:00+08:00",
                )
            )
        outcomes = []
        for index, item in enumerate(independent):
            outcomes.append(
                {
                    **compute_choice_market_outcomes(
                        [item],
                        [reports[index]],
                        self.collection(),
                        as_of=AS_OF,
                    )[0],
                    "subject_id": item["subject_id"],
                    "truth_available_at": "2026-07-01T15:30:00+08:00",
                    "outcome_status": "success",
                    "market_hit": True,
                    "broker_id": "B1",
                    "broker": "券商甲",
                }
            )
        broker, _analyst = build_choice_skill_tables(outcomes, as_of=AS_OF)
        self.assertEqual(len(broker), 1)
        self.assertGreaterEqual(broker[0]["effective_sample_size"], 5.0)
        self.assertTrue(broker[0]["rank_eligible"])

        duplicated = [
            {
                **row,
                "subject_id": "600000",
                "claim_available_at": "2025-01-01T09:30:00+08:00",
                "consensus_weight": 1.0 / len(outcomes),
            }
            for row in outcomes
        ]
        broker, _analyst = build_choice_skill_tables(duplicated, as_of=AS_OF)
        self.assertLess(broker[0]["effective_sample_size"], 5.0)
        self.assertFalse(broker[0]["rank_eligible"])
        self.assertIsNone(broker[0]["posterior_skill"])
        self.assertIsNone(broker[0]["conservative_lower_bound"])

    def test_cross_broker_consensus_discount_survives_entity_grouping(self):
        outcomes = []
        for day_index in range(6):
            for broker_id in ("B1", "B2"):
                base = compute_choice_market_outcomes(
                    [
                        claim(
                            f"{broker_id}-{day_index}",
                            report_id=f"{broker_id}-r{day_index}",
                            available_at=f"2025-01-{day_index + 1:02d}T09:30:00+08:00",
                        )
                    ],
                    [
                        report(
                            f"{broker_id}-r{day_index}",
                            broker_code=broker_id,
                            broker=f"券商{broker_id}",
                        )
                    ],
                    self.collection(),
                    as_of=AS_OF,
                )[0]
                outcomes.append(
                    {
                        **base,
                        "outcome_status": "success",
                        "market_hit": True,
                        "truth_available_at": "2026-07-01T15:30:00+08:00",
                        "consensus_weight": 0.5,
                    }
                )
        brokers, _people = build_choice_skill_tables(outcomes, as_of=AS_OF)
        self.assertEqual(len(brokers), 2)
        self.assertTrue(
            all(row["effective_sample_size"] < 5.0 for row in brokers)
        )
        self.assertTrue(all(row["rank_eligible"] is False for row in brokers))

    def test_same_analyst_name_at_two_brokers_is_not_merged(self):
        outcomes = []
        for broker_id in ("B1", "B2"):
            for index in range(3):
                outcomes.append(
                    {
                        **compute_choice_market_outcomes(
                            [
                                claim(
                                    f"{broker_id}-{index}",
                                    report_id=f"{broker_id}-r{index}",
                                )
                            ],
                            [
                                report(
                                    f"{broker_id}-r{index}",
                                    broker_code=broker_id,
                                    broker=f"券商{broker_id}",
                                    analyst="同名分析师",
                                    team="同名团队",
                                )
                            ],
                            self.collection(),
                            as_of=AS_OF,
                        )[0],
                        "outcome_status": "success",
                        "market_hit": True,
                        "truth_available_at": "2026-07-01T15:30:00+08:00",
                    }
                )
        _broker, people = build_choice_skill_tables(outcomes, as_of=AS_OF)
        analyst_rows = [row for row in people if row["entity_type"] == "analyst"]
        self.assertEqual(len(analyst_rows), 2)
        self.assertEqual(
            {row["entity_id"] for row in analyst_rows},
            {"B1|同名分析师", "B2|同名分析师"},
        )

    def test_rate_limiter_waits_at_the_window_boundary(self):
        now = [0.0]
        sleeps = []

        def sleeper(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        limiter = SlidingWindowRateLimiter(
            2,
            clock=lambda: now[0],
            sleeper=sleeper,
        )
        limiter.acquire()
        limiter.acquire()
        limiter.acquire()
        self.assertEqual(sleeps, [60.0])

    def test_collection_uses_explicit_choice_qfq_none_and_checkpoint(self):
        registry = FakeRegistry(self.sessions)
        with tempfile.TemporaryDirectory() as temporary:
            checkpoint = Path(temporary) / "checkpoint.json"
            collection = collect_choice_market_data(
                registry,
                [claim("c")],
                as_of="2025-07-31",
                checkpoint_path=checkpoint,
                offline=False,
                resume=True,
                requested_at=AS_OF,
            )
            self.assertIsNotNone(collection.calendar_batch)
            self.assertIsNotNone(collection.benchmark_batch)
            self.assertIn("600000.SH", collection.stock_batches)
            daily = [request for request in registry.requests if request.dataset_type == "daily_bar"]
            self.assertEqual(
                {(request.instrument_id, request.adjustment) for request in daily},
                {("000300.SH", "none"), ("600000.SH", "qfq")},
            )
            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
            self.assertTrue(payload["entries"])
            self.assertTrue(all(item["status"] == "success" for item in payload["entries"].values()))

    def test_collection_opens_circuit_after_three_global_failures(self):
        class NetworkBlocked(RuntimeError):
            status = "network_blocked"
            code = "10002003"

        class FailingRegistry(FakeRegistry):
            def fetch_diagnostic(self, request, *, provider_id):
                if request.dataset_type == "daily_bar" and request.instrument_id != "000300.SH":
                    self.requests.append(request)
                    raise NetworkBlocked("network timeout")
                return super().fetch_diagnostic(request, provider_id=provider_id)

        registry = FailingRegistry(self.sessions)
        claims = [
            claim(f"c{index}", subject_id=f"60000{index}")
            for index in range(6)
        ]
        with tempfile.TemporaryDirectory() as temporary:
            collection = collect_choice_market_data(
                registry,
                claims,
                as_of="2025-07-31",
                checkpoint_path=Path(temporary) / "checkpoint.json",
                offline=False,
                resume=True,
                requested_at=AS_OF,
            )
        stock_requests = [
            request
            for request in registry.requests
            if request.dataset_type == "daily_bar"
            and request.instrument_id != "000300.SH"
        ]
        self.assertEqual(len(stock_requests), 3)
        self.assertEqual(len(collection.failures), 6)
        self.assertEqual(
            collection.failures["600003.SH"]["code"],
            "choice_collection_circuit_open",
        )

    def test_collection_opens_quota_circuit_and_preserves_distinct_reason(self):
        class QuotaExceeded(RuntimeError):
            status = "failed"
            code = "quota_exhausted"

        class QuotaRegistry(FakeRegistry):
            def fetch_diagnostic(self, request, *, provider_id):
                if request.dataset_type == "daily_bar" and request.instrument_id != "000300.SH":
                    self.requests.append(request)
                    raise QuotaExceeded("Choice data limit exceeded")
                return super().fetch_diagnostic(request, provider_id=provider_id)

        registry = QuotaRegistry(self.sessions)
        claims = [
            claim(f"c{index}", subject_id=f"60000{index}")
            for index in range(6)
        ]
        with tempfile.TemporaryDirectory() as temporary:
            checkpoint = Path(temporary) / "checkpoint.json"
            collection = collect_choice_market_data(
                registry,
                claims,
                as_of="2025-07-31",
                checkpoint_path=checkpoint,
                offline=False,
                resume=True,
                requested_at=AS_OF,
            )
            checkpoint_bytes = checkpoint.read_bytes()
        stock_requests = [
            request
            for request in registry.requests
            if request.dataset_type == "daily_bar"
            and request.instrument_id != "000300.SH"
        ]
        self.assertEqual(len(stock_requests), 3)
        self.assertEqual(collection.failures["600000.SH"]["code"], "quota_exhausted")
        self.assertEqual(
            collection.failures["600003.SH"]["code"],
            "choice_quota_exhausted_circuit_open",
        )
        replay_registry = FakeRegistry(self.sessions)
        with tempfile.TemporaryDirectory() as temporary:
            replay_checkpoint = Path(temporary) / "checkpoint.json"
            replay_checkpoint.write_bytes(checkpoint_bytes)
            replay = collect_choice_market_data(
                replay_registry,
                claims,
                as_of="2025-07-31",
                checkpoint_path=replay_checkpoint,
                offline=True,
                resume=True,
                requested_at=AS_OF,
            )
        replay_stock_requests = [
            request
            for request in replay_registry.requests
            if request.dataset_type == "daily_bar"
            and request.instrument_id != "000300.SH"
        ]
        self.assertEqual(replay_stock_requests, [])
        self.assertEqual(replay.failures["600000.SH"]["code"], "quota_exhausted")
        self.assertEqual(
            replay.failures["600003.SH"]["code"],
            "choice_quota_exhausted_circuit_open",
        )

    def test_session_start_failure_becomes_explicit_calendar_failure(self):
        class NotConfigured(RuntimeError):
            status = "not_configured"
            code = "10001012"

        class StartFailingRegistry(FakeRegistry):
            @contextmanager
            def diagnostic_session(self, *, provider_id):
                self.assert_choice(provider_id)
                raise NotConfigured("insufficient user access")
                yield  # pragma: no cover

            @staticmethod
            def assert_choice(provider_id):
                if provider_id != "choice":
                    raise AssertionError("Choice must be explicit")

        with tempfile.TemporaryDirectory() as temporary:
            collection = collect_choice_market_data(
                StartFailingRegistry(self.sessions),
                [claim("c")],
                as_of="2025-07-31",
                checkpoint_path=Path(temporary) / "checkpoint.json",
                offline=False,
                resume=True,
                requested_at=AS_OF,
            )
        self.assertIsNone(collection.calendar_batch)
        self.assertEqual(
            collection.failures["trade_calendar"]["status"], "not_configured"
        )

    def test_session_stop_failure_preserves_completed_batches(self):
        class StopFailed(RuntimeError):
            status = "failed"
            code = "choice_stop_failed"

        class StopFailingRegistry(FakeRegistry):
            @contextmanager
            def diagnostic_session(self, *, provider_id):
                if provider_id != "choice":
                    raise AssertionError("Choice must be explicit")
                yield self
                raise StopFailed("Choice stop failed")

        with tempfile.TemporaryDirectory() as temporary:
            collection = collect_choice_market_data(
                StopFailingRegistry(self.sessions),
                [claim("c")],
                as_of="2025-07-31",
                checkpoint_path=Path(temporary) / "checkpoint.json",
                offline=False,
                resume=True,
                requested_at=AS_OF,
            )
        self.assertIsNotNone(collection.calendar_batch)
        self.assertIsNotNone(collection.benchmark_batch)
        self.assertIn("600000.SH", collection.stock_batches)
        self.assertEqual(collection.failures["session_stop"]["status"], "failed")

    def test_cli_exposes_bounded_diagnostic_and_validation_commands(self):
        parser = build_parser()
        diagnostic = parser.parse_args(
            [
                "diagnostic-market",
                "--offline",
                "--max-requests-per-minute",
                "300",
                "--max-pdf-candidates",
                "20",
                "--max-recommendations",
                "5",
            ]
        )
        self.assertEqual(diagnostic.command, "diagnostic-market")
        self.assertTrue(diagnostic.offline)
        prepared = parser.parse_args(["prepare-validation", "--offline"])
        self.assertEqual(prepared.command, "prepare-validation")
        finalized = parser.parse_args(
            ["finalize-validation", "--review", "review.json"]
        )
        self.assertEqual(finalized.command, "finalize-validation")

    def test_pdf_bounds_and_evidence_gate(self):
        outcome = {
            "claim_id": "c1",
            "report_id": "r1",
            "broker_id": "B1",
            "broker": "券商甲",
            "analyst": "分析师甲",
            "report_title": "报告",
            "report_pdf_url": "https://example.test/r1.pdf",
            "report_pdf_sha256": "a" * 64,
            "evidence_span": "证据",
            "pdf_evidence_verified": True,
            "target_type": "stock_rating",
            "claim_available_at": "2025-01-01T09:30:00+08:00",
            "outcome_status": "success",
        }
        skill = {
            "entity_id": "B1",
            "target_type": "stock_rating",
            "conservative_lower_bound": 0.6,
            "rank_eligible": True,
        }
        candidates = select_pdf_candidates([outcome] * 25, [skill], limit=100)
        self.assertLessEqual(len(candidates), 20)
        queue = build_reading_queue(candidates, [outcome], limit=99)
        self.assertEqual(len(queue), 1)
        self.assertTrue(queue[0]["why_read"])
        self.assertTrue(queue[0]["might_change"])
        invalid = [{**outcome, "report_pdf_sha256": ""}]
        self.assertEqual(build_reading_queue(candidates, invalid, limit=5), [])
        unbound = [{**outcome, "pdf_evidence_verified": False}]
        self.assertEqual(build_reading_queue(candidates, unbound, limit=5), [])

    def test_pdf_evidence_must_match_claim_direction_and_target_values(self):
        negative_rating = claim("rating-negative", direction=-1)
        positive_rating = claim("rating-positive", direction=1)
        rating_report = {"rating": "买入"}
        self.assertEqual(
            _choice_pdf_evidence_span(
                rating_report, negative_rating, "投资评级：买入。"
            ),
            "",
        )
        self.assertTrue(
            _choice_pdf_evidence_span(
                rating_report, positive_rating, "投资评级：买入。"
            )
        )

        target = claim("target", target_type="target_price", direction=0)
        self.assertEqual(
            _choice_pdf_evidence_span({}, target, "未来120个交易日目标价50元。"),
            "",
        )
        self.assertTrue(
            _choice_pdf_evidence_span(
                {}, target, "未来120个交易日目标价区间10-12元。"
            )
        )

    def test_recent_reading_candidates_are_separate_from_historical_sample(self):
        skills = [
            {
                "entity_id": "B1",
                "target_type": "stock_rating",
                "conservative_lower_bound": 0.6,
                "rank_eligible": True,
            }
        ]
        historical = claim(
            "historical",
            report_id="historical-report",
            available_at="2025-06-30T09:30:00+08:00",
        )
        recent = claim(
            "recent",
            report_id="recent-report",
            available_at="2026-08-01T09:30:00+08:00",
        )
        candidates, rows = select_recent_pdf_candidates(
            [historical, recent],
            [
                report("historical-report"),
                report("recent-report"),
            ],
            skills,
            limit=1,
        )
        self.assertEqual([row["claim_id"] for row in candidates], ["recent"])
        self.assertEqual([row["claim_id"] for row in rows], ["recent"])

    def test_reading_window_never_overlaps_skill_sample(self):
        start, end = _reading_candidate_window(
            sample_end=date(2025, 6, 30),
            as_of_day=date(2025, 7, 5),
            recent_candidate_days=210,
        )
        self.assertEqual(start, date(2025, 7, 1))
        self.assertEqual(end, date(2025, 7, 5))
        blocked_start, blocked_end = _reading_candidate_window(
            sample_end=date(2025, 6, 30),
            as_of_day=date(2025, 6, 15),
            recent_candidate_days=210,
        )
        self.assertGreater(blocked_start, blocked_end)

    def test_bundle_enforces_configured_recommendation_limit(self):
        outcome = {
            "claim_id": "c1",
            "report_id": "r1",
            "broker": "券商甲",
            "analyst": "分析师甲",
            "report_title": "报告",
            "report_pdf_url": "https://example.test/r1.pdf",
            "report_pdf_sha256": "a" * 64,
            "evidence_span": "投资评级：买入",
            "pdf_evidence_verified": True,
            "target_type": "stock_rating",
        }
        candidates = [{"report_id": "r1", "claim_id": "c1"}]
        queue = build_reading_queue(candidates, [outcome], limit=1)
        self.assertEqual(len(queue), 1)
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(
                ChoiceDiagnosticError, "configured recommendation limit"
            ):
                write_choice_diagnostic_bundle(
                    Path(temporary) / "output",
                    as_of=AS_OF,
                    outcomes=[],
                    broker_skills=[],
                    analyst_skills=[],
                    collection=self.collection(),
                    pdf_candidates=candidates,
                    reading_queue=queue,
                    parameters={"max_recommendations": 0},
                )

    def test_seven_file_bundle_is_byte_deterministic(self):
        outcomes = compute_choice_market_outcomes(
            [claim("c")], [report()], self.collection(), as_of=AS_OF
        )
        broker, analysts = build_choice_skill_tables(outcomes, as_of=AS_OF)
        with tempfile.TemporaryDirectory() as temporary:
            first = write_choice_diagnostic_bundle(
                Path(temporary) / "one",
                as_of=AS_OF,
                outcomes=outcomes,
                broker_skills=broker,
                analyst_skills=analysts,
                collection=self.collection(),
                pdf_candidates=[],
                reading_queue=[],
            )
            second = write_choice_diagnostic_bundle(
                Path(temporary) / "two",
                as_of=AS_OF,
                outcomes=outcomes,
                broker_skills=broker,
                analyst_skills=analysts,
                collection=self.collection(),
                pdf_candidates=[],
                reading_queue=[],
            )
            self.assertEqual(first.run_id, second.run_id)
            self.assertEqual(set(first.paths), set(CHOICE_ARTIFACT_FILENAMES))
            for name in CHOICE_ARTIFACT_FILENAMES:
                self.assertEqual(first.paths[name].read_bytes(), second.paths[name].read_bytes())
            manifest = json.loads(first.paths["run_manifest.json"].read_text(encoding="utf-8"))
            self.assertEqual(manifest["diagnostic_status"], DIAGNOSTIC_STATUS)
            self.assertFalse(manifest["formal_truth_eligible"])
            self.assertEqual(
                tuple(manifest["artifact_names"]), CHOICE_ARTIFACT_FILENAMES
            )
            with first.paths["choice_claim_market_outcomes.csv"].open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)

    def test_quota_truncation_suppresses_rankings_and_reading_queue(self):
        outcomes = []
        for index in range(6):
            base = compute_choice_market_outcomes(
                [
                    claim(
                        f"c{index}",
                        report_id=f"r{index}",
                        available_at=f"2025-01-{index + 1:02d}T09:30:00+08:00",
                    )
                ],
                [report(f"r{index}")],
                self.collection(),
                as_of=AS_OF,
            )[0]
            outcomes.append(
                {
                    **base,
                    "outcome_status": "success",
                    "market_hit": True,
                    "truth_available_at": "2026-07-01T15:30:00+08:00",
                }
            )
        broker, analysts = build_choice_skill_tables(outcomes, as_of=AS_OF)
        self.assertTrue(any(row["rank_eligible"] for row in broker))
        quota_collection = self.collection(
            failures={
                "600999.SH": {
                    "status": "failed",
                    "code": "quota_exhausted",
                    "message": "Choice data limit exceeded",
                }
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            bundle = write_choice_diagnostic_bundle(
                Path(temporary) / "quota",
                as_of=AS_OF,
                outcomes=outcomes,
                broker_skills=broker,
                analyst_skills=analysts,
                collection=quota_collection,
                pdf_candidates=[{"report_id": "r0", "claim_id": "c0"}],
                reading_queue=[],
            )
            with bundle.paths["choice_broker_accuracy.csv"].open(
                encoding="utf-8", newline=""
            ) as handle:
                broker_rows = list(csv.DictReader(handle))
            self.assertTrue(broker_rows)
            self.assertTrue(all(row["rank_eligible"] == "false" for row in broker_rows))
            self.assertTrue(
                all(
                    row["skill_status"] == "partial_quota_truncated_not_rankable"
                    for row in broker_rows
                )
            )
            self.assertTrue(
                all(
                    row["raw_hit_rate"] == ""
                    and row["posterior_skill"] == ""
                    and row["conservative_lower_bound"] == ""
                    for row in broker_rows
                )
            )
            with bundle.paths["choice_source_coverage.csv"].open(
                encoding="utf-8", newline=""
            ) as handle:
                coverage = list(csv.DictReader(handle))
            self.assertTrue(
                all(
                    row["coverage_status"] == "partial_quota_truncated_not_rankable"
                    for row in coverage
                )
            )
            self.assertIn(
                "## 暂无合格推荐",
                bundle.paths["choice_reading_queue.md"].read_text(encoding="utf-8"),
            )
            manifest = json.loads(
                bundle.paths["run_manifest.json"].read_text(encoding="utf-8")
            )
            self.assertTrue(manifest["quota_truncated"])
            self.assertTrue(manifest["collection_incomplete"])
            self.assertEqual(manifest["counts"]["bounded_pdf_candidates"], 0)
            self.assertEqual(manifest["counts"]["reading_recommendations"], 0)

    def test_nonquota_or_renamed_failure_cannot_publish_accuracy(self):
        outcomes = []
        for index in range(6):
            base = compute_choice_market_outcomes(
                [
                    claim(
                        f"network-{index}",
                        report_id=f"network-r{index}",
                        available_at=f"2025-01-{index + 1:02d}T09:30:00+08:00",
                    )
                ],
                [report(f"network-r{index}")],
                self.collection(),
                as_of=AS_OF,
            )[0]
            outcomes.append(
                {
                    **base,
                    "outcome_status": "success",
                    "market_hit": True,
                    "truth_available_at": "2026-07-01T15:30:00+08:00",
                }
            )
        broker, analysts = build_choice_skill_tables(outcomes, as_of=AS_OF)
        self.assertTrue(any(row["rank_eligible"] for row in broker))
        for failure in (
            {
                "status": "network_blocked",
                "code": "choice_collection_circuit_open",
                "message": "network stopped ordered collection",
            },
            {
                "status": "failed",
                "code": "renamed_cache_miss",
                "message": "locally renamed checkpoint failure",
            },
        ):
            with self.subTest(code=failure["code"]), tempfile.TemporaryDirectory() as temporary:
                bundle = write_choice_diagnostic_bundle(
                    Path(temporary) / "incomplete",
                    as_of=AS_OF,
                    outcomes=outcomes,
                    broker_skills=broker,
                    analyst_skills=analysts,
                    collection=self.collection(failures={"600999.SH": failure}),
                    pdf_candidates=[],
                    reading_queue=[],
                )
                with bundle.paths["choice_broker_accuracy.csv"].open(
                    encoding="utf-8", newline=""
                ) as handle:
                    rows = list(csv.DictReader(handle))
                self.assertTrue(rows)
                self.assertTrue(all(row["raw_hit_rate"] == "" for row in rows))
                self.assertTrue(
                    all(row["skill_status"] == "partial_collection_not_rankable" for row in rows)
                )
                manifest = json.loads(
                    bundle.paths["run_manifest.json"].read_text(encoding="utf-8")
                )
                self.assertTrue(manifest["collection_incomplete"])
                self.assertFalse(manifest["quota_truncated"])


if __name__ == "__main__":
    unittest.main()
