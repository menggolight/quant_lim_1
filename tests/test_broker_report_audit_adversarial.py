from __future__ import annotations

import hashlib
import json
import unittest
from datetime import date, datetime, timedelta

from research.broker_report_audit.cli import V1_CONFIG_PATH, _run_walk_forward, load_config
from research.broker_report_audit.factors import (
    FactorError,
    _portfolio_metrics,
    build_factor_components,
    build_factor_observations,
    build_internal_factor_research_rows,
    rank_deep_reads,
    validate_walk_forward_input_rows,
    walk_forward_evaluate,
    walk_forward_splits,
)
from research.broker_report_audit.models import CHINA_TZ
from research.broker_report_audit.skills import estimate_skill


def at(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 18, tzinfo=CHINA_TZ)


class PointInTimeFactorAdversarialTests(unittest.TestCase):
    @staticmethod
    def _auditable_factor_row() -> dict[str, object]:
        features = {
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
        snapshot = {
            "as_of": "2024-01-05T23:59:59.999999+08:00",
            "stock_id": "000333.SZ",
            "objective": {"macro": 0.1, "industry": 0.2, "stock": 0.3},
            "objective_provenance": {
                "macro": {"available_at": "2024-01-05T15:00:00+08:00", "source": "macro_fixture"},
                "industry": {"available_at": "2024-01-05T15:00:00+08:00", "source": "industry_fixture"},
                "stock": {"available_at": "2024-01-05T15:00:00+08:00", "source": "stock_fixture"},
            },
            "claims": [],
            "reports": [],
            "snapshots": [],
            "features": features,
        }
        canonical = json.dumps(
            snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return {
            "contract_version": "broker-report-factor-row.v1",
            "as_of": "2024-01-05",
            "stock_id": "000333.SZ",
            "industry_id": "SW801110",
            **features,
            "source_snapshot_payload": snapshot,
            "source_snapshot_hash": hashlib.sha256(canonical).hexdigest(),
            "stock_excess_vs_industry_20d": 0.04,
            "label_definition": "stock_excess_vs_industry_geometric",
            "label_horizon_days": 20,
            "label_end": "2024-02-02T15:00:00+08:00",
            "label_available_at": "2024-02-02T15:10:00+08:00",
            "label_source": "market_bars_fixture",
            "benchmark_id": "SW801110",
        }

    def test_active_claim_cannot_weight_itself_with_its_own_skill_snapshot(self) -> None:
        report = {
            "report_id": "current-report",
            "broker": "测试券商",
            "analyst": "甲",
            "dimension": "stock",
            "subject_id": "000333.SZ",
            "available_at": at(date(2024, 1, 5)),
            "content_hash": "a" * 64,
        }
        claim = {
            "claim_id": "current-claim",
            "report_id": "current-report",
            "dimension": "stock",
            "subject_id": "000333.SZ",
            "target_type": "rating_change",
            "direction": 1,
            "horizon_days": 120,
            "available_at": at(date(2024, 1, 5)),
        }
        contaminated_snapshot = {
            "snapshot_id": "s1",
            "as_of": at(date(2024, 1, 4)),
            "broker": "测试券商",
            "analyst": "甲",
            "team": "",
            "dimension": "stock",
            "target_type": "rating_change",
            "horizon_days": 120,
            "conservative_lower_bound": 0.9,
            "posterior_skill": 0.95,
            "effective_sample_size": 20,
            "source_report_ids": ("current-report",),
        }
        row = build_factor_components(
            as_of=at(date(2024, 6, 20)),
            stock_id="000333.SZ",
            stock_claims=[claim],
            reports=[report],
            skill_snapshots=[contaminated_snapshot],
            macro_objective_factor=0.1,
            industry_objective_factor=0.2,
            stock_objective_factor=0.3,
        )
        self.assertIsNone(row["stock_report_factor"])
        self.assertTrue(
            any("missing_strictly_prior_skill" in reason for reason in row["exclusions"])
        )

    def test_walk_forward_uses_fixed_36_month_windows_and_frequency_safe_purge(self) -> None:
        dates = []
        current = date(2019, 1, 4)
        end = date(2025, 6, 27)
        while current <= end:
            dates.append(current)
            current += timedelta(days=7)
        trading_calendar = []
        current = date(2019, 1, 1)
        while current <= end:
            if current.weekday() < 5:
                trading_calendar.append(current)
            current += timedelta(days=1)
        splits = walk_forward_splits(dates, trading_calendar=trading_calendar)
        self.assertGreaterEqual(len(splits) + 1, 4)  # development windows + frozen holdout
        first, second = splits[0], splits[1]
        self.assertEqual(first["train_period"][0], date(2019, 1, 4))
        self.assertEqual(first["train_period"][1], date(2022, 1, 4))
        self.assertEqual(second["train_period"][0], date(2019, 7, 4))
        self.assertEqual(second["train_period"][1], date(2022, 7, 4))
        self.assertTrue(first["train_dates"])
        self.assertLess(first["train_dates"][-1], date(2021, 8, 1))

    def test_sparse_factor_dates_cannot_impersonate_a_trading_calendar(self) -> None:
        dates = [date(2019, 1, 4) + timedelta(days=7 * index) for index in range(330)]
        with self.assertRaises(FactorError):
            walk_forward_splits(dates)

    def test_all_models_use_the_same_complete_case_oos_universe(self) -> None:
        rows = []
        trading_calendar = []
        calendar_day = date(2019, 1, 1)
        while calendar_day <= date(2025, 6, 27):
            if calendar_day.weekday() < 5:
                trading_calendar.append(calendar_day)
            calendar_day += timedelta(days=1)
        current = date(2019, 1, 4)
        index = 0
        while current <= date(2025, 6, 27):
            for stock in range(5):
                signal = (stock - 2) / 2.0 + ((index % 13) - 6) / 100.0
                row = {
                    "as_of": current.isoformat(),
                    "stock_id": f"S{stock}",
                    "industry_id": f"I{stock % 3}",
                    "target_return_20d": signal * 0.03,
                    "macro_objective_factor": signal * 0.1,
                    "industry_objective_factor": signal * 0.2,
                    "stock_objective_factor": signal * 0.3,
                    "macro_report_raw": signal * 0.1,
                    "industry_report_raw": signal * 0.2,
                    "stock_report_raw": signal * 0.3,
                    "macro_report_factor": signal * 0.1,
                    "industry_report_factor": signal * 0.2,
                    "stock_report_factor": signal * 0.3,
                    "macro_industry_interaction": signal * signal * 0.02,
                    "industry_stock_interaction": signal * signal * 0.06,
                }
                if index % 17 == 0 and stock == 0:
                    row["stock_report_factor"] = None
                rows.append(row)
            index += 1
            current += timedelta(days=7)
        result = walk_forward_evaluate(rows, trading_calendar=trading_calendar)
        self.assertEqual(result["status"], "not_admitted")
        self.assertIn(
            "external_or_unrecomputed_labels_are_diagnostic_only",
            result["admission"]["reasons"],
        )
        evaluated = next(
            window
            for window in result["windows"]
            if all(window[name]["status"] == "evaluated" for name in ("B0", "B1", "B2", "M1"))
        )
        test_counts = {evaluated[name]["test_count"] for name in ("B0", "B1", "B2", "M1")}
        self.assertEqual(len(test_counts), 1)

    def test_portfolio_is_formed_cross_sectionally_on_each_rebalance_date(self) -> None:
        metrics = _portfolio_metrics(
            predictions=[1.0, -1.0, -1.0, 1.0],
            realized=[0.10, -0.10, 0.20, -0.20],
            industries=["A", "B", "A", "B"],
            dates=["2024-01-05", "2024-01-05", "2024-01-12", "2024-01-12"],
            cost_bps=10.0,
        )
        self.assertEqual(metrics["portfolio_rebalance_count"], 2)
        self.assertAlmostEqual(metrics["gross_group_return"], -0.10)
        self.assertAlmostEqual(metrics["cost_after_group_return"], -0.102)

    def test_external_walk_forward_rows_require_hashed_pit_provenance(self) -> None:
        row = self._auditable_factor_row()
        validated = validate_walk_forward_input_rows(
            [row],
            sample_start="2019-01-01",
            sample_end="2025-06-30",
            evaluation_as_of="2026-08-04",
        )
        self.assertEqual(len(validated), 1)

        future = dict(row)
        payload = dict(row["source_snapshot_payload"])  # type: ignore[arg-type]
        provenance = dict(payload["objective_provenance"])  # type: ignore[arg-type]
        provenance["macro"] = {
            "available_at": "2024-01-06T15:00:00+08:00",
            "source": "future_macro",
        }
        payload["objective_provenance"] = provenance
        future["source_snapshot_payload"] = payload
        future["source_snapshot_hash"] = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        with self.assertRaises(FactorError):
            validate_walk_forward_input_rows(
                [future],
                sample_start="2019-01-01",
                sample_end="2025-06-30",
                evaluation_as_of="2026-08-04",
            )

    def test_historical_factor_builds_skill_strictly_before_current_claim(self) -> None:
        reports: list[dict[str, object]] = []
        claims: list[dict[str, object]] = []
        outcomes: list[dict[str, object]] = []
        for index in range(30):
            claim_day = date(2022, 1, 3) + timedelta(days=index * 5)
            truth_day = claim_day + timedelta(days=30)
            report_id = f"past-r{index}"
            claim_id = f"past-c{index}"
            reports.append(
                {
                    "report_id": report_id,
                    "broker": "测试券商",
                    "analyst": "甲",
                    "team": "",
                    "dimension": "stock",
                    "subject_id": f"S{index}",
                    "available_at": at(claim_day),
                    "content_hash": f"{index + 1:064x}",
                }
            )
            claims.append(
                {
                    "claim_id": claim_id,
                    "report_id": report_id,
                    "dimension": "stock",
                    "subject_id": f"S{index}",
                    "target_type": "rating_change",
                    "direction": 1,
                    "horizon_days": 120,
                    "available_at": at(claim_day),
                }
            )
            outcomes.append(
                {
                    "claim_id": claim_id,
                    "hit": True,
                    "mature": True,
                    "truth_available_at": at(truth_day),
                }
            )
        reports.append(
            {
                "report_id": "current-r",
                "broker": "测试券商",
                "analyst": "甲",
                "team": "",
                "dimension": "stock",
                "subject_id": "000333.SZ",
                "available_at": at(date(2024, 1, 5)),
                "content_hash": "f" * 64,
            }
        )
        claims.append(
            {
                "claim_id": "current-c",
                "report_id": "current-r",
                "dimension": "stock",
                "subject_id": "000333.SZ",
                "target_type": "rating_change",
                "direction": 1,
                "horizon_days": 120,
                "available_at": at(date(2024, 1, 5)),
            }
        )
        rows = build_factor_observations(
            [
                {
                    "as_of": "2024-01-10",
                    "stock_id": "000333.SZ",
                    "industry_id": "SW801110",
                    "macro_objective_factor": {
                        "value": 0.1,
                        "available_at": "2024-01-10",
                        "source": "macro_fixture",
                    },
                    "industry_objective_factor": {
                        "value": 0.2,
                        "available_at": "2024-01-10",
                        "source": "industry_fixture",
                    },
                    "stock_objective_factor": {
                        "value": 0.3,
                        "available_at": "2024-01-10",
                        "source": "stock_fixture",
                    },
                }
            ],
            as_of="2026-08-04",
            reports=reports,
            claims=claims,
            outcomes=outcomes,
            outcomes_are_trusted=True,
            config={"skill": {"half_life_days": 730, "sensitivity_half_life_days": 365}},
        )
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0].stock_report_factor)
        self.assertGreater(rows[0].stock_report_factor, 0.0)

    def test_deep_read_excludes_expired_claims_and_self_skill(self) -> None:
        old_report = {
            "report_id": "old",
            "broker": "测试券商",
            "analyst": "甲",
            "dimension": "stock",
            "subject_id": "000333.SZ",
            "available_at": at(date(2023, 1, 5)),
        }
        old_claim = {
            "claim_id": "old-c",
            "report_id": "old",
            "dimension": "stock",
            "subject_id": "000333.SZ",
            "target_type": "stock_rating",
            "direction": 1,
            "forecast_period": "20TD",
            "horizon_days": 20,
            "available_at": at(date(2023, 1, 5)),
            "evidence_span": "买入",
            "extraction_confidence": 0.99,
        }
        self.assertEqual(
            rank_deep_reads(
                [old_report], [old_claim], as_of=at(date(2024, 1, 5))
            ),
            [],
        )

        current_report = dict(old_report, report_id="current", available_at=at(date(2024, 1, 5)))
        current_claim = dict(
            old_claim,
            claim_id="current-c",
            report_id="current",
            horizon_days=120,
            forecast_period="120TD",
            available_at=at(date(2024, 1, 5)),
        )
        self_snapshot = {
            "snapshot_id": "self",
            "as_of": at(date(2024, 1, 4)),
            "broker": "测试券商",
            "analyst": "甲",
            "team": "",
            "dimension": "stock",
            "target_type": "stock_rating",
            "horizon_days": 120,
            "conservative_lower_bound": 0.9,
            "posterior_skill": 0.95,
            "effective_sample_size": 20,
            "source_report_ids": ("current",),
        }
        queue = rank_deep_reads(
            [current_report],
            [current_claim],
            [self_snapshot],
            as_of=at(date(2024, 1, 10)),
        )
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["skill_lower_bound"], 0.0)

    def test_consensus_discount_clusters_publication_not_truth_date(self) -> None:
        rows = []
        for index in range(10):
            rows.append(
                {
                    "hit": True,
                    "mature": True,
                    "available_at": at(date(2023, 1, 2) + timedelta(days=index)),
                    "truth_available_at": at(date(2024, 3, 30)),
                    "report_id": f"r{index}",
                    "subject_id": "000333.SZ",
                    "dimension": "stock",
                    "target_type": "EPS",
                    "horizon_days": 120,
                    "direction": 0,
                }
            )
        result = estimate_skill(
            rows,
            as_of=at(date(2024, 4, 1)),
            prior_strength=0,
            consensus_power=1,
        )
        self.assertGreater(result["effective_sample_size"], 9.0)

    def test_target_price_factor_requires_hashed_point_in_time_reference(self) -> None:
        report = {
            "report_id": "target-r",
            "broker": "测试券商",
            "analyst": "甲",
            "dimension": "stock",
            "subject_id": "000333.SZ",
            "available_at": at(date(2024, 1, 5)),
            "content_hash": "a" * 64,
        }
        claim = {
            "claim_id": "target-c",
            "report_id": "target-r",
            "dimension": "stock",
            "subject_id": "000333.SZ",
            "target_type": "target_price",
            "direction": 0,
            "value_min": 120,
            "value_max": 120,
            "horizon_days": 120,
            "available_at": at(date(2024, 1, 5)),
        }
        snapshot = {
            "snapshot_id": "prior-target-skill",
            "as_of": at(date(2024, 1, 4)),
            "broker": "测试券商",
            "analyst": "甲",
            "team": "",
            "dimension": "stock",
            "target_type": "target_price",
            "horizon_days": 120,
            "conservative_lower_bound": 0.8,
            "posterior_skill": 0.85,
            "effective_sample_size": 20,
            "source_report_ids": ("past-target",),
        }
        common = {
            "as_of": at(date(2024, 1, 10)),
            "stock_id": "000333.SZ",
            "stock_claims": [claim],
            "reports": [report],
            "skill_snapshots": [snapshot],
            "macro_objective_factor": 0.1,
            "industry_objective_factor": 0.2,
            "stock_objective_factor": 0.3,
        }
        unaudited = build_factor_components(
            **common,
            reference_values={"000333.SZ": 100},
        )
        self.assertIsNone(unaudited["stock_report_raw"])
        self.assertTrue(
            any("missing_auditable_reference_price" in reason for reason in unaudited["exclusions"])
        )
        audited = build_factor_components(
            **common,
            reference_values={
                "000333.SZ": {
                    "value": 100,
                    "available_at": "2024-01-10T15:00:00+08:00",
                    "source": "market_bars_fixture",
                    "content_hash": "b" * 64,
                }
            },
        )
        self.assertAlmostEqual(audited["stock_report_raw"], 0.2)
        self.assertEqual(
            audited["source_snapshot_payload"]["reference_values"][0]["content_hash"],
            "b" * 64,
        )


class InternalLabelClosureAdversarialTests(unittest.TestCase):
    @staticmethod
    def _fixture() -> tuple[dict[str, object], dict[str, object], list[dict[str, object]], list[date]]:
        observation_day = date(2024, 1, 5)
        features = {
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
        snapshot = {
            "as_of": at(observation_day).isoformat(),
            "stock_id": "000333.SZ",
            "objective": {"macro": 0.1, "industry": 0.2, "stock": 0.3},
            "objective_provenance": {
                layer: {
                    "available_at": "2024-01-05T15:00:00+08:00",
                    "source": f"{layer}_fixture",
                }
                for layer in ("macro", "industry", "stock")
            },
            "claims": [],
            "reports": [],
            "snapshots": [],
            "reference_values": [],
            "features": features,
        }
        component: dict[str, object] = {
            "contract_version": "broker-report-factor-row.v1",
            "as_of": at(observation_day),
            "stock_id": "000333.SZ",
            **features,
            "source_snapshot_payload": snapshot,
            "source_snapshot_hash": hashlib.sha256(
                json.dumps(
                    snapshot,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        }
        mapping = {
            "stock_id": "000333.SZ",
            "industry_id": "SW801110",
            "benchmark_id": "BK0476",
            "available_at": "2024-01-01T15:00:00+08:00",
            "effective_from": "2023-01-01",
            "source": "pit_industry_membership_fixture",
        }
        mapping_hash = hashlib.sha256(
            json.dumps(
                mapping,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        specification: dict[str, object] = {
            "as_of": at(observation_day),
            "stock_id": "000333.SZ",
            "industry_id": "SW801110",
            "industry_mapping": {**mapping, "content_hash": mapping_hash},
        }
        calendar: list[date] = []
        day = date(2024, 1, 2)
        while day <= date(2024, 4, 5):
            if day.weekday() < 5:
                calendar.append(day)
            day += timedelta(days=1)
        position = calendar.index(observation_day)
        endpoints = {
            calendar[position + 1]: (100.0, 100.0),
            calendar[position + 20]: (120.0, 110.0),
            calendar[position + 60]: (130.0, 115.0),
        }
        bars: list[dict[str, object]] = []
        for trade_day, (stock_price, benchmark_price) in endpoints.items():
            for instrument_id, price in (
                ("000333.SZ", stock_price),
                ("BK0476", benchmark_price),
            ):
                bars.append(
                    {
                        "instrument_id": instrument_id,
                        "trade_date": trade_day,
                        "open": price,
                        "close": price,
                        "available_at": at(trade_day),
                        "fetched_at": at(date(2024, 4, 8)),
                        "source": "eastmoney_public.push2his",
                        "content_hash": hashlib.sha256(
                            f"{instrument_id}|{trade_day}|{price}".encode()
                        ).hexdigest(),
                    }
                )
        return component, specification, bars, calendar

    def test_internal_builder_recomputes_labels_and_strict_provenance(self) -> None:
        component, specification, bars, calendar = self._fixture()
        batch = build_internal_factor_research_rows(
            [component],
            [specification],
            bars,
            trading_calendar=calendar,
            evaluation_as_of="2024-06-30",
            sample_start="2024-01-01",
            sample_end="2024-06-30",
        )
        self.assertEqual(len(batch.rows), 1)
        row = batch.rows[0]
        self.assertAlmostEqual(
            row["stock_excess_vs_industry_20d"], (120 / 100) / (110 / 100) - 1
        )
        self.assertAlmostEqual(
            row["stock_excess_vs_industry_60d"], (130 / 100) / (115 / 100) - 1
        )
        validated = validate_walk_forward_input_rows(
            batch.rows,
            sample_start="2024-01-01",
            sample_end="2024-06-30",
            evaluation_as_of="2024-06-30",
            require_internal_label_provenance=True,
        )
        self.assertEqual(len(validated), 1)

        tampered = dict(row)
        tampered["stock_excess_vs_industry_20d"] = 999
        with self.assertRaises(FactorError):
            validate_walk_forward_input_rows(
                [tampered],
                sample_start="2024-01-01",
                sample_end="2024-06-30",
                evaluation_as_of="2024-06-30",
                require_internal_label_provenance=True,
            )

    def test_missing_pit_industry_mapping_fails_closed(self) -> None:
        component, specification, bars, calendar = self._fixture()
        specification.pop("industry_mapping")
        batch = build_internal_factor_research_rows(
            [component],
            [specification],
            bars,
            trading_calendar=calendar,
            evaluation_as_of="2024-06-30",
            sample_start="2024-01-01",
            sample_end="2024-06-30",
        )
        self.assertFalse(batch.rows)
        self.assertTrue(any("industry_mapping" in item for item in batch.exclusions))

    def test_cli_only_marks_internal_batch_evidence_verified(self) -> None:
        component, specification, bars, calendar = self._fixture()
        batch = build_internal_factor_research_rows(
            [component],
            [specification],
            bars,
            trading_calendar=calendar,
            evaluation_as_of="2024-06-30",
            sample_start="2024-01-01",
            sample_end="2024-06-30",
        )
        internal_issues: list[dict[str, object]] = []
        internal = _run_walk_forward(
            [],
            config=load_config(V1_CONFIG_PATH),
            issues=internal_issues,
            evaluation_as_of=at(date(2026, 8, 4)),
            trading_calendar=calendar,
            internal_batch=batch,
        )
        self.assertFalse(internal["admission"]["evidence_verified"])  # type: ignore[index]
        self.assertIn(  # type: ignore[index]
            "external_or_unrecomputed_labels_are_diagnostic_only",
            internal["admission"]["reasons"],
        )

        external_issues: list[dict[str, object]] = []
        external = _run_walk_forward(
            batch.rows,
            config=load_config(V1_CONFIG_PATH),
            issues=external_issues,
            evaluation_as_of=at(date(2026, 8, 4)),
            trading_calendar=calendar,
        )
        self.assertFalse(external["admission"]["evidence_verified"])  # type: ignore[index]
        self.assertIn(
            "external_or_unrecomputed_labels_are_diagnostic_only",
            external["admission"]["reasons"],  # type: ignore[index]
        )

    def test_turnover_cost_uses_adjacent_holdings(self) -> None:
        metrics = _portfolio_metrics(
            predictions=[1.0, -1.0, 1.0, -1.0],
            realized=[0.10, -0.10, 0.10, -0.10],
            industries=["A", "B", "A", "B"],
            dates=["2024-01-05", "2024-01-05", "2024-01-12", "2024-01-12"],
            cost_bps=10.0,
            stock_ids=["S1", "S2", "S1", "S2"],
        )
        self.assertAlmostEqual(metrics["average_one_way_turnover"], 1.0)
        self.assertAlmostEqual(metrics["gross_group_return"], 0.20)
        self.assertAlmostEqual(metrics["cost_after_group_return"], 0.199)


if __name__ == "__main__":
    unittest.main()
