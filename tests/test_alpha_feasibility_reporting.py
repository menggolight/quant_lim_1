from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from research.strategy_workspace import alpha_feasibility_reporting as reporting
from research.strategy_workspace.contracts import canonical_sha256


GENERATED_AT = "2026-08-28T12:00:00+08:00"
COMMIT_SHA = "33ac5f0e3c484a514288136ac5317830902e2105"


def _counts() -> dict[str, int]:
    return {
        "trade_cal": 1,
        "index_weight": 73,
        "daily": 401,
        "adj_factor": 401,
        "index_daily": 1,
        "suspend_d": 401,
    }


def _first_index_weight() -> dict[str, object]:
    return {
        "observed_data_fields": [
            "index_code",
            "con_code",
            "trade_date",
            "weight",
        ],
        "required_data_fields": [
            "index_code",
            "con_code",
            "trade_date",
            "weight",
        ],
        "missing_required_data_fields": [],
        "extra_data_fields": [],
        "field_order_matches_canonical": True,
        "data_row_count": 800,
        "provider_payload_sha256": "4" * 64,
        "normalized_content_sha256": "5" * 64,
    }


def _complete_data() -> dict[str, object]:
    return {
        "actual_tushare_request_count_by_endpoint": _counts(),
        "first_index_weight": _first_index_weight(),
        "stock_basic_status": "DEFERRED_NOT_REQUIRED_FOR_ALPHA_FEASIBILITY",
        "stock_basic_request_count": 0,
        "security_master_pit_status": "NOT_IMPLEMENTED_NOT_REQUIRED_IN_P1",
        "coverage_start": "2017-07-01",
        "coverage_end": "2023-12-31",
        "pit_months_expected": 73,
        "pit_months_observed": 73,
        "union_instrument_count": 1200,
        "valid_candidate_count_by_decision": {"2023-01-03": 1199},
        "insufficient_history_count_by_decision": {"2023-01-03": 1},
        "ineligible_no_initial_price_count": 1,
        "unexplained_market_data_gap_count": 0,
        "collection_plan_sha256": "1" * 64,
        "pit_membership_manifest_sha256": "2" * 64,
        "history_manifest_sha256": "3" * 64,
        "daily_coverage_status": "complete",
        "adj_factor_coverage_status": "complete",
        "suspension_coverage_status": "complete",
        "benchmark_coverage_status": "complete",
        "data_status": "READY",
        "remaining_blockers": [],
        "locked_test_status": {
            "access": "NOT_ACCESSED",
            "download": "NOT_DOWNLOADED",
            "run": "NOT_RUN",
        },
        "locked_test_consumed": False,
        "safety": dict(reporting.SAFETY),
    }


def _blocked_data() -> dict[str, object]:
    value = _complete_data()
    value.update(
        {
            "pit_months_observed": 72,
            "union_instrument_count": 0,
            "valid_candidate_count_by_decision": {},
            "insufficient_history_count_by_decision": {},
            "ineligible_no_initial_price_count": 0,
            "unexplained_market_data_gap_count": 0,
            "daily_coverage_status": "not_run",
            "adj_factor_coverage_status": "not_run",
            "suspension_coverage_status": "not_run",
            "benchmark_coverage_status": "not_run",
            "data_status": "BLOCKED_PIT_MEMBERSHIP",
            "remaining_blockers": ["pit_membership_month_missing"],
        }
    )
    return value


def _metrics(
    *,
    month: str,
    active: float = 0.03,
    drawdown: float = 0.10,
    stock_share: float | None = 0.40,
    days_share: float | None = 0.35,
) -> dict[str, object]:
    return {
        "net_return": 0.08,
        "benchmark_return": 0.05,
        "net_active_return": active,
        "max_drawdown": drawdown,
        "annualized_turnover": 1.2,
        "total_cost": 0.02,
        "cost_to_gross_profit": 0.2,
        "average_gross_exposure": 0.60,
        "cash_day_fraction": 0.25,
        "exposure_state_distribution": {
            "RISK_OFF": 0.10,
            "DEFENSIVE": 0.20,
            "NEUTRAL": 0.30,
            "RISK_ON": 0.40,
        },
        "rebalance_count": 24,
        "positive_month_rate": 0.58,
        "positive_half_year_count": 2,
        "worst_month": {
            "month": month,
            "net_return": -0.04,
            "benchmark_return": -0.02,
            "net_active_return": -0.02,
        },
        "per_stock_pnl_contribution": {
            "000001.SZ": 0.02,
            "600000.SH": 0.01,
        },
        "largest_stock_pnl_share": stock_share,
        "largest_10_days_pnl_share": days_share,
    }


def _development() -> dict[str, object]:
    return {
        "base": _metrics(month="2020-03", active=0.04),
        "stress": _metrics(month="2020-03", active=0.02),
    }


def _validation() -> dict[str, object]:
    return {
        "base": _metrics(
            month="2023-08",
            active=0.03,
            drawdown=0.10,
            stock_share=0.40,
            days_share=0.35,
        ),
        "stress": _metrics(
            month="2023-08",
            active=0.01,
            drawdown=0.11,
            stock_share=0.45,
            days_share=0.49,
        ),
    }


def _resign(report: dict[str, object]) -> None:
    report.pop("report_sha256", None)
    report["report_sha256"] = canonical_sha256(report)


class AlphaFeasibilityReportingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.experiment = reporting.load_and_validate_experiment_config()
        cls.p15_experiment = reporting.load_and_validate_experiment_config(
            reporting.P15_CONFIG_PATH
        )

    def _completed(
        self,
        *,
        validation: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return reporting.build_completed_alpha_feasibility_report(
            commit_sha=COMMIT_SHA,
            data_summary=_complete_data(),
            development_metrics=_development(),
            validation_metrics=validation or _validation(),
            experiment=self.experiment,
            generated_at=GENERATED_AT,
        )

    def test_frozen_config_and_three_file_hashes_validate(self) -> None:
        self.assertEqual(self.experiment["frozen_implementation"], reporting.FROZEN_IMPLEMENTATION)
        self.assertEqual(tuple(self.experiment["source"]["allowed_endpoints"]), reporting.ALLOWED_ENDPOINTS)
        self.assertEqual(set(self.experiment["requests"]), set(reporting.ALLOWED_ENDPOINTS))
        self.assertEqual(
            reporting.ALLOWED_ENDPOINTS,
            (
                "trade_cal",
                "index_weight",
                "daily",
                "adj_factor",
                "index_daily",
                "suspend_d",
            ),
        )
        self.assertNotIn("stock_basic", self.experiment["requests"])
        self.assertEqual(
            self.experiment["stock_basic_status"],
            "DEFERRED_NOT_REQUIRED_FOR_ALPHA_FEASIBILITY",
        )
        self.assertEqual(self.experiment["stock_basic_request_count"], 0)
        self.assertIs(type(self.experiment["stock_basic_request_count"]), int)
        self.assertEqual(
            self.experiment["security_master_pit_status"],
            "NOT_IMPLEMENTED_NOT_REQUIRED_IN_P1",
        )
        self.assertEqual(self.experiment["dates"], reporting.DATES)
        self.assertEqual(self.experiment["portfolio"], reporting.PORTFOLIO)
        self.assertEqual(self.experiment["costs"], reporting.COSTS)
        self.assertEqual(self.experiment["gate"], reporting.GATE)
        self.assertEqual(self.experiment["locked_test_status"], reporting.LOCKED_TEST_STATUS)
        self.assertEqual(self.experiment["safety"], reporting.EXPERIMENT_SAFETY)

    def test_p15_v3_config_is_exact_and_builds_a_v4_report(self) -> None:
        self.assertEqual(
            self.p15_experiment["schema_version"],
            "technical-alpha-feasibility-experiment.v3",
        )
        self.assertEqual(self.p15_experiment["source"], reporting.SOURCE_V3)
        self.assertEqual(self.p15_experiment["index"], reporting.INDEX_V3)
        self.assertEqual(self.p15_experiment["safety"], reporting.SAFETY)
        self.assertNotIn("real_money_list_allowed", self.experiment["safety"])
        self.assertIs(
            self.p15_experiment["safety"]["real_money_list_allowed"], False
        )
        report = reporting.build_completed_alpha_feasibility_report(
            commit_sha=COMMIT_SHA,
            data_summary=_complete_data(),
            development_metrics=_development(),
            validation_metrics=_validation(),
            experiment=self.p15_experiment,
            generated_at=GENERATED_AT,
        )
        self.assertEqual(
            report["schema_version"], "technical-alpha-feasibility-report.v4"
        )
        self.assertEqual(
            report["experiment_config_canonical_sha256"],
            reporting.EXPECTED_P15_EXPERIMENT_CANONICAL_SHA256,
        )
        self.assertEqual(
            report["benchmark"],
            {"index_code": "000906.SH", "basis": "unadjusted_price_index"},
        )
        self.assertIs(report["safety"]["real_money_list_allowed"], False)
        reporting.verify_alpha_feasibility_report(
            report,
            experiment=self.p15_experiment,
        )

    def test_p15_v3_source_and_index_extensions_are_exact(self) -> None:
        mutations = (
            ("source_retry_drift", "source", "maximum_attempts_per_fingerprint", 4),
            (
                "source_recovery_drift",
                "source",
                "interrupted_fingerprint_recovery",
                "retry_started_request",
            ),
            ("index_hard_bound_drift", "index", "weight_sum_hard_min", "99.4"),
            (
                "index_warning_bound_drift",
                "index",
                "weight_sum_warning_max",
                "100.06",
            ),
        )
        for name, section, field, replacement in mutations:
            with self.subTest(name=name):
                drifted = copy.deepcopy(self.p15_experiment)
                drifted[section][field] = replacement
                with self.assertRaisesRegex(
                    reporting.AlphaFeasibilityReportingError,
                    rf"(?:{section} drift|schema violation)",
                ):
                    reporting.validate_experiment_config(drifted)

    def test_experiment_safety_contract_is_version_specific(self) -> None:
        v2_with_v3_field = copy.deepcopy(self.experiment)
        v2_with_v3_field["safety"]["real_money_list_allowed"] = False
        v3_without_field = copy.deepcopy(self.p15_experiment)
        v3_without_field["safety"].pop("real_money_list_allowed")
        v3_enabled = copy.deepcopy(self.p15_experiment)
        v3_enabled["safety"]["real_money_list_allowed"] = True

        for name, candidate in (
            ("v2_rewritten", v2_with_v3_field),
            ("v3_missing", v3_without_field),
            ("v3_enabled", v3_enabled),
        ):
            with self.subTest(name=name):
                with self.assertRaisesRegex(
                    reporting.AlphaFeasibilityReportingError,
                    r"(?:schema violation|safety drift|canonical content drift)",
                ):
                    reporting.validate_experiment_config(candidate)

    def test_blocked_report_has_no_metrics_and_fixed_safety(self) -> None:
        report = reporting.build_blocked_alpha_feasibility_report(
            commit_sha=COMMIT_SHA,
            data_summary=_blocked_data(),
            experiment=self.experiment,
            generated_at=GENERATED_AT,
        )
        self.assertEqual(report["terminal_status"], "BLOCKED_DATA")
        self.assertIsNone(report["development_metrics"])
        self.assertIsNone(report["validation_metrics"])
        self.assertIsNone(report["concentration_metrics"])
        self.assertEqual(report["locked_test_status"], reporting.LOCKED_TEST_STATUS)
        self.assertIs(report["locked_test_consumed"], False)
        self.assertEqual(report["safety"], reporting.SAFETY)
        self.assertIs(report["safety"]["real_money_list_allowed"], False)
        self.assertEqual(report["benchmark"], reporting.BENCHMARK)
        self.assertEqual(
            report["stock_basic_status"],
            "DEFERRED_NOT_REQUIRED_FOR_ALPHA_FEASIBILITY",
        )
        self.assertEqual(report["stock_basic_request_count"], 0)
        self.assertIs(type(report["stock_basic_request_count"]), int)
        self.assertEqual(
            report["security_master_pit_status"],
            "NOT_IMPLEMENTED_NOT_REQUIRED_IN_P1",
        )
        self.assertEqual(report["valid_candidate_count_by_decision"], {})
        self.assertEqual(report["insufficient_history_count_by_decision"], {})
        self.assertEqual(report["unexplained_market_data_gap_count"], 0)
        self.assertEqual(
            set(report["actual_tushare_request_count_by_endpoint"]),
            set(reporting.ALLOWED_ENDPOINTS),
        )
        reporting.verify_alpha_feasibility_report(report, experiment=self.experiment)

    def test_adapter_protocol_block_has_distinct_terminal_and_no_metrics(self) -> None:
        data = _blocked_data()
        data["data_status"] = "BLOCKED_ADAPTER_PROTOCOL"
        data["pit_months_observed"] = 0
        data["remaining_blockers"] = ["semantic_core_missing"]
        report = reporting.build_blocked_alpha_feasibility_report(
            commit_sha=COMMIT_SHA,
            data_summary=data,
            experiment=self.experiment,
            generated_at=GENERATED_AT,
        )
        self.assertEqual(report["terminal_status"], "BLOCKED_ADAPTER_PROTOCOL")
        self.assertIsNone(report["development_metrics"])
        self.assertIsNone(report["validation_metrics"])
        self.assertIsNone(report["concentration_metrics"])
        reporting.verify_alpha_feasibility_report(report, experiment=self.experiment)

    def test_protocol_blocker_codes_are_exact_and_upstream_errors_remain_data_blocks(self) -> None:
        expected = {
            "duplicate_json_key",
            "semantic_core_missing",
            "semantic_core_type_invalid",
            "response_body_too_large",
            "transport_extensions_too_large",
            "transport_extensions_too_deep",
            "transport_extension_secret_detected",
            "data_payload_invalid",
            "data_fields_not_array",
            "data_field_name_invalid",
            "data_duplicate_fields",
            "data_required_fields_missing",
            "data_item_width_mismatch",
            "data_required_value_invalid",
            "unknown_non_json_value",
        }
        self.assertEqual(set(reporting.ADAPTER_PROTOCOL_BLOCKERS), expected)

        for blocker in (
            "upstream_permission_error",
            "upstream_rate_limit_error",
            "upstream_authentication_account_error",
            "upstream_invalid_parameter_error",
            "upstream_server_internal_error",
            "upstream_unknown_error",
        ):
            with self.subTest(blocker=blocker):
                data = _blocked_data()
                data["data_status"] = "BLOCKED_DATA"
                data["remaining_blockers"] = [blocker]
                report = reporting.build_blocked_alpha_feasibility_report(
                    commit_sha=COMMIT_SHA,
                    data_summary=data,
                    experiment=self.experiment,
                    generated_at=GENERATED_AT,
                )
                self.assertEqual(report["terminal_status"], "BLOCKED_DATA")
                self.assertEqual(report["remaining_blockers"], [blocker])
                reporting.verify_alpha_feasibility_report(
                    report,
                    experiment=self.experiment,
                )

    def test_blocked_report_requires_a_real_blocker(self) -> None:
        data = _blocked_data()
        data["remaining_blockers"] = []
        with self.assertRaisesRegex(reporting.AlphaFeasibilityReportingError, "requires at least one"):
            reporting.build_blocked_alpha_feasibility_report(
                commit_sha=COMMIT_SHA,
                data_summary=data,
                experiment=self.experiment,
                generated_at=GENERATED_AT,
            )

    def test_complete_report_go_candidate_and_complete_metric_contract(self) -> None:
        report = self._completed()
        self.assertEqual(report["schema_version"], "technical-alpha-feasibility-report.v4")
        self.assertEqual(report["terminal_status"], "ALPHA_FEASIBILITY_GO_CANDIDATE")
        self.assertEqual(
            report["valid_candidate_count_by_decision"], {"2023-01-03": 1199}
        )
        self.assertEqual(
            report["insufficient_history_count_by_decision"], {"2023-01-03": 1}
        )
        self.assertEqual(report["ineligible_no_initial_price_count"], 1)
        self.assertEqual(report["first_index_weight"], _first_index_weight())
        self.assertEqual(report["unexplained_market_data_gap_count"], 0)
        self.assertEqual(report["stock_basic_request_count"], 0)
        for split in ("development_metrics", "validation_metrics"):
            self.assertEqual(set(report[split]), {"base", "stress"})
            for scenario in ("base", "stress"):
                self.assertEqual(set(report[split][scenario]), set(reporting.METRIC_FIELDS))
                self.assertEqual(report[split][scenario]["rebalance_count"], 24)
                self.assertEqual(
                    report[split][scenario]["cost_to_gross_profit"], "0.2"
                )
                self.assertNotIn(
                    "trade_or_rebalance_count", report[split][scenario]
                )
        self.assertIs(report["safety"]["trade_eligibility"], False)
        self.assertIs(report["safety"]["real_money_list_allowed"], False)
        self.assertEqual(report["safety"]["execution_realism"], "INCOMPLETE")
        self.assertEqual(
            report["benchmark"],
            {"index_code": "000906.SH", "basis": "unadjusted_price_index"},
        )
        reporting.verify_alpha_feasibility_report(report, experiment=self.experiment)

    def test_report_v4_schema_terminal_branches_are_fail_closed(self) -> None:
        completed = self._completed()
        completed_mutations = (
            ("development_metrics", None),
            ("validation_metrics", None),
            ("concentration_metrics", None),
            ("pit_months_observed", 72),
            ("union_instrument_count", 0),
            ("collection_plan_sha256", None),
            ("pit_membership_manifest_sha256", None),
            ("history_manifest_sha256", None),
            ("daily_coverage_status", "blocked"),
            ("adj_factor_coverage_status", "blocked"),
            ("suspension_coverage_status", "blocked"),
            ("benchmark_coverage_status", "blocked"),
            ("remaining_blockers", ["unexpected_completed_blocker"]),
            (
                "benchmark",
                {"index_code": "000906.SH", "basis": "total_return_index"},
            ),
            (
                "safety",
                {**completed["safety"], "real_money_list_allowed": True},
            ),
            (
                "locked_test_status",
                {"access": "ACCESSED", "download": "NOT_DOWNLOADED", "run": "NOT_RUN"},
            ),
            ("locked_test_consumed", True),
        )
        for field, replacement in completed_mutations:
            with self.subTest(branch="completed", field=field):
                forged = copy.deepcopy(completed)
                forged[field] = replacement
                _resign(forged)
                with self.assertRaisesRegex(
                    reporting.AlphaFeasibilityReportingError,
                    "report schema violation",
                ):
                    reporting.verify_alpha_feasibility_report(
                        forged, experiment=self.experiment
                    )

        blocked = reporting.build_blocked_alpha_feasibility_report(
            commit_sha=COMMIT_SHA,
            data_summary=_blocked_data(),
            experiment=self.experiment,
            generated_at=GENERATED_AT,
        )
        blocked_mutations = (
            ("development_metrics", completed["development_metrics"]),
            ("validation_metrics", completed["validation_metrics"]),
            ("concentration_metrics", completed["concentration_metrics"]),
            ("remaining_blockers", []),
        )
        for field, replacement in blocked_mutations:
            with self.subTest(branch="blocked", field=field):
                forged = copy.deepcopy(blocked)
                forged[field] = replacement
                _resign(forged)
                with self.assertRaisesRegex(
                    reporting.AlphaFeasibilityReportingError,
                    "report schema violation",
                ):
                    reporting.verify_alpha_feasibility_report(
                        forged, experiment=self.experiment
                    )

    def test_completed_report_accepts_extra_or_reordered_provider_fields(self) -> None:
        cases = (
            (
                [
                    "index_code",
                    "index_name",
                    "con_code",
                    "trade_date",
                    "weight",
                ],
                True,
            ),
            (
                [
                    "trade_date",
                    "index_name",
                    "weight",
                    "con_code",
                    "index_code",
                ],
                False,
            ),
        )
        for observed, order_matches in cases:
            with self.subTest(observed=observed):
                data = _complete_data()
                evidence = copy.deepcopy(data["first_index_weight"])
                evidence.update(
                    {
                        "observed_data_fields": observed,
                        "extra_data_fields": ["index_name"],
                        "field_order_matches_canonical": order_matches,
                    }
                )
                data["first_index_weight"] = evidence
                report = reporting.build_completed_alpha_feasibility_report(
                    commit_sha=COMMIT_SHA,
                    data_summary=data,
                    development_metrics=_development(),
                    validation_metrics=_validation(),
                    experiment=self.experiment,
                    generated_at=GENERATED_AT,
                )
                self.assertEqual(
                    report["first_index_weight"]["extra_data_fields"],
                    ["index_name"],
                )
                reporting.verify_alpha_feasibility_report(
                    report,
                    experiment=self.experiment,
                )

    def test_blocked_report_preserves_safe_invalid_field_placeholders(self) -> None:
        data = _blocked_data()
        data["data_status"] = "BLOCKED_ADAPTER_PROTOCOL"
        data["remaining_blockers"] = ["data_field_name_invalid"]
        data["first_index_weight"] = {
            "observed_data_fields": ["sha256:" + "a" * 64, "type:string"],
            "required_data_fields": [
                "index_code",
                "con_code",
                "trade_date",
                "weight",
            ],
            "missing_required_data_fields": [
                "index_code",
                "con_code",
                "trade_date",
                "weight",
            ],
            "extra_data_fields": [],
            "field_order_matches_canonical": False,
            "data_row_count": 0,
            "provider_payload_sha256": None,
            "normalized_content_sha256": None,
        }
        report = reporting.build_blocked_alpha_feasibility_report(
            commit_sha=COMMIT_SHA,
            data_summary=data,
            experiment=self.experiment,
            generated_at=GENERATED_AT,
        )
        self.assertEqual(
            report["first_index_weight"]["observed_data_fields"],
            ["sha256:" + "a" * 64, "type:string"],
        )
        reporting.verify_alpha_feasibility_report(
            report,
            experiment=self.experiment,
        )

        parser_like = copy.deepcopy(data)
        parser_like["first_index_weight"] = {
            "observed_data_fields": [
                "index_code",
                "con_code",
                "trade_date",
                "weight",
                "sha256:" + "b" * 64,
            ],
            "required_data_fields": [
                "index_code",
                "con_code",
                "trade_date",
                "weight",
            ],
            "missing_required_data_fields": [],
            "extra_data_fields": [],
            "field_order_matches_canonical": True,
            "data_row_count": 1,
            "provider_payload_sha256": "c" * 64,
            "normalized_content_sha256": None,
        }
        parser_like_report = reporting.build_blocked_alpha_feasibility_report(
            commit_sha=COMMIT_SHA,
            data_summary=parser_like,
            experiment=self.experiment,
            generated_at=GENERATED_AT,
        )
        self.assertTrue(
            parser_like_report["first_index_weight"][
                "field_order_matches_canonical"
            ]
        )
        reporting.verify_alpha_feasibility_report(
            parser_like_report,
            experiment=self.experiment,
        )

        unsafe = copy.deepcopy(data)
        unsafe["first_index_weight"]["observed_data_fields"] = [
            "unsafe token-shaped field"
        ]
        with self.assertRaises(reporting.AlphaFeasibilityReportingError):
            reporting.build_blocked_alpha_feasibility_report(
                commit_sha=COMMIT_SHA,
                data_summary=unsafe,
                experiment=self.experiment,
                generated_at=GENERATED_AT,
            )

    def test_concentration_is_worst_validation_base_or_stress(self) -> None:
        report = self._completed()
        concentration = report["concentration_metrics"]
        self.assertEqual(concentration["largest_stock_pnl_share"], "0.45")
        self.assertEqual(concentration["largest_10_days_pnl_share"], "0.49")
        self.assertIs(concentration["gate_passed"], True)
        self.assertEqual(concentration["issues"], [])

    def test_no_go_for_each_preregistered_gate_failure(self) -> None:
        cases: dict[str, tuple[str, str, object]] = {
            "base_active_not_positive": ("base", "net_active_return", 0.0),
            "stress_active_negative": ("stress", "net_active_return", -0.0001),
            "base_drawdown": ("base", "max_drawdown", 0.120001),
            "stress_drawdown": ("stress", "max_drawdown", 0.120001),
            "base_stock_concentration": ("base", "largest_stock_pnl_share", 0.500001),
            "stress_days_concentration": ("stress", "largest_10_days_pnl_share", 0.500001),
        }
        for name, (scenario, field, value) in cases.items():
            with self.subTest(name=name):
                validation = _validation()
                validation[scenario][field] = value
                report = self._completed(validation=validation)
                self.assertEqual(report["terminal_status"], "ALPHA_FEASIBILITY_NO_GO")

    def test_gate_boundaries_are_inclusive_except_base_active(self) -> None:
        validation = _validation()
        validation["base"]["net_active_return"] = 0.000000001
        validation["stress"]["net_active_return"] = 0.0
        for scenario in ("base", "stress"):
            validation[scenario]["max_drawdown"] = 0.12
            validation[scenario]["largest_stock_pnl_share"] = 0.50
            validation[scenario]["largest_10_days_pnl_share"] = 0.50
        report = self._completed(validation=validation)
        self.assertEqual(report["terminal_status"], "ALPHA_FEASIBILITY_GO_CANDIDATE")
        self.assertIs(report["concentration_metrics"]["gate_passed"], True)

        validation["base"]["net_active_return"] = 0.0
        report = self._completed(validation=validation)
        self.assertEqual(report["terminal_status"], "ALPHA_FEASIBILITY_NO_GO")

    def test_sub_float_gate_breaches_remain_exact_and_fail_closed(self) -> None:
        validation = _validation()
        validation["base"]["max_drawdown"] = Decimal(
            "0.1200000000000000000000000001"
        )
        report = self._completed(validation=validation)
        self.assertEqual(report["terminal_status"], "ALPHA_FEASIBILITY_NO_GO")
        self.assertEqual(
            report["validation_metrics"]["base"]["max_drawdown"],
            "0.1200000000000000000000000001",
        )

        validation = _validation()
        validation["stress"]["largest_10_days_pnl_share"] = Decimal(
            "0.5000000000000000000000000001"
        )
        report = self._completed(validation=validation)
        self.assertEqual(report["terminal_status"], "ALPHA_FEASIBILITY_NO_GO")
        self.assertEqual(
            report["validation_metrics"]["stress"]["largest_10_days_pnl_share"],
            "0.5000000000000000000000000001",
        )

    def test_missing_concentration_is_fail_closed_no_go(self) -> None:
        validation = _validation()
        validation["stress"]["largest_stock_pnl_share"] = None
        report = self._completed(validation=validation)
        self.assertEqual(report["terminal_status"], "ALPHA_FEASIBILITY_NO_GO")
        self.assertIsNone(report["concentration_metrics"]["largest_stock_pnl_share"])
        self.assertIn(
            "validation_stress_largest_stock_pnl_share_missing",
            report["concentration_metrics"]["issues"],
        )

    def test_missing_or_extra_metric_is_rejected(self) -> None:
        for mutation in ("missing", "extra", "legacy_rebalance_name"):
            with self.subTest(mutation=mutation):
                validation = _validation()
                if mutation == "missing":
                    validation["base"].pop("annualized_turnover")
                elif mutation == "legacy_rebalance_name":
                    validation["base"]["trade_or_rebalance_count"] = (
                        validation["base"].pop("rebalance_count")
                    )
                else:
                    validation["base"]["parameter_search_result"] = 1
                with self.assertRaisesRegex(reporting.AlphaFeasibilityReportingError, "metrics mismatch"):
                    self._completed(validation=validation)

    def test_cost_to_gross_profit_is_derived_and_nonpositive_is_null(self) -> None:
        validation = _validation()
        validation["base"].update(
            net_return=-0.02,
            total_cost=0.02,
            cost_to_gross_profit=None,
        )
        report = self._completed(validation=validation)
        self.assertIsNone(
            report["validation_metrics"]["base"]["cost_to_gross_profit"]
        )

        for name, net_return, total_cost, supplied in (
            ("positive_denominator_missing", 0.08, 0.02, None),
            ("positive_denominator_mismatch", 0.08, 0.02, 0.21),
            ("zero_denominator_numeric", -0.02, 0.02, 0),
            ("negative_denominator_numeric", -0.03, 0.02, 0),
        ):
            with self.subTest(name=name):
                invalid = _validation()
                invalid["base"].update(
                    net_return=net_return,
                    total_cost=total_cost,
                    cost_to_gross_profit=supplied,
                )
                with self.assertRaises(reporting.AlphaFeasibilityReportingError):
                    self._completed(validation=invalid)

    def test_completed_report_rejects_incomplete_data(self) -> None:
        data = _complete_data()
        data["benchmark_coverage_status"] = "blocked"
        with self.assertRaisesRegex(reporting.AlphaFeasibilityReportingError, "complete data coverage"):
            reporting.build_completed_alpha_feasibility_report(
                commit_sha=COMMIT_SHA,
                data_summary=data,
                development_metrics=_development(),
                validation_metrics=_validation(),
                experiment=self.experiment,
                generated_at=GENERATED_AT,
            )

    def test_request_counts_require_exactly_six_strict_nonnegative_integer_keys(self) -> None:
        def build(data: dict[str, object]) -> None:
            reporting.build_completed_alpha_feasibility_report(
                commit_sha=COMMIT_SHA,
                data_summary=data,
                development_metrics=_development(),
                validation_metrics=_validation(),
                experiment=self.experiment,
                generated_at=GENERATED_AT,
            )

        mutations = {
            "missing": lambda data: data[
                "actual_tushare_request_count_by_endpoint"
            ].pop("daily"),
            "stock_basic_extra": lambda data: data[
                "actual_tushare_request_count_by_endpoint"
            ].__setitem__("stock_basic", 0),
            "bool": lambda data: data[
                "actual_tushare_request_count_by_endpoint"
            ].__setitem__("daily", True),
            "float": lambda data: data[
                "actual_tushare_request_count_by_endpoint"
            ].__setitem__("daily", 1.0),
            "negative": lambda data: data[
                "actual_tushare_request_count_by_endpoint"
            ].__setitem__("daily", -1),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                data = _complete_data()
                mutate(data)
                with self.assertRaises(reporting.AlphaFeasibilityReportingError):
                    build(data)

    def test_v4_fixed_fields_and_decision_maps_have_strict_contracts(self) -> None:
        def build(data: dict[str, object]) -> None:
            reporting.build_completed_alpha_feasibility_report(
                commit_sha=COMMIT_SHA,
                data_summary=data,
                development_metrics=_development(),
                validation_metrics=_validation(),
                experiment=self.experiment,
                generated_at=GENERATED_AT,
            )

        mutations = {
            "stock_status_missing": lambda data: data.pop("stock_basic_status"),
            "stock_status": lambda data: data.__setitem__(
                "stock_basic_status", "READY"
            ),
            "stock_count_missing": lambda data: data.pop(
                "stock_basic_request_count"
            ),
            "stock_count_nonzero": lambda data: data.__setitem__(
                "stock_basic_request_count", 1
            ),
            "stock_count_bool": lambda data: data.__setitem__(
                "stock_basic_request_count", False
            ),
            "stock_count_float": lambda data: data.__setitem__(
                "stock_basic_request_count", 0.0
            ),
            "stock_count_string": lambda data: data.__setitem__(
                "stock_basic_request_count", "0"
            ),
            "security_master_status": lambda data: data.__setitem__(
                "security_master_pit_status", "READY"
            ),
            "security_master_status_missing": lambda data: data.pop(
                "security_master_pit_status"
            ),
            "valid_map_missing": lambda data: data.pop(
                "valid_candidate_count_by_decision"
            ),
            "valid_map_not_object": lambda data: data.__setitem__(
                "valid_candidate_count_by_decision", []
            ),
            "valid_map_bool": lambda data: data.__setitem__(
                "valid_candidate_count_by_decision", {"2023-01-03": True}
            ),
            "valid_map_float": lambda data: data.__setitem__(
                "valid_candidate_count_by_decision", {"2023-01-03": 1.0}
            ),
            "valid_map_negative": lambda data: data.__setitem__(
                "valid_candidate_count_by_decision", {"2023-01-03": -1}
            ),
            "future_decision": lambda data: data.__setitem__(
                "insufficient_history_count_by_decision", {"2024-01-02": 1}
            ),
            "insufficient_map_missing": lambda data: data.pop(
                "insufficient_history_count_by_decision"
            ),
            "invalid_decision": lambda data: data.__setitem__(
                "insufficient_history_count_by_decision", {"2023-02-30": 1}
            ),
            "decision_key_mismatch": lambda data: data.__setitem__(
                "insufficient_history_count_by_decision", {"2023-01-04": 1}
            ),
            "gap_bool": lambda data: data.__setitem__(
                "unexplained_market_data_gap_count", False
            ),
            "gap_missing": lambda data: data.pop(
                "unexplained_market_data_gap_count"
            ),
            "gap_float": lambda data: data.__setitem__(
                "unexplained_market_data_gap_count", 0.0
            ),
            "gap_negative": lambda data: data.__setitem__(
                "unexplained_market_data_gap_count", -1
            ),
            "no_initial_missing": lambda data: data.pop(
                "ineligible_no_initial_price_count"
            ),
            "no_initial_bool": lambda data: data.__setitem__(
                "ineligible_no_initial_price_count", False
            ),
            "no_initial_float": lambda data: data.__setitem__(
                "ineligible_no_initial_price_count", 1.0
            ),
            "no_initial_negative": lambda data: data.__setitem__(
                "ineligible_no_initial_price_count", -1
            ),
            "first_missing": lambda data: data.pop("first_index_weight"),
            "first_order_drift": lambda data: data["first_index_weight"].__setitem__(
                "field_order_matches_canonical", False
            ),
            "first_extra_inconsistent": lambda data: data[
                "first_index_weight"
            ].__setitem__("extra_data_fields", ["index_name"]),
            "first_row_bool": lambda data: data["first_index_weight"].__setitem__(
                "data_row_count", True
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                data = _complete_data()
                mutate(data)
                with self.assertRaises(reporting.AlphaFeasibilityReportingError):
                    build(data)

    def test_ready_data_rejects_unexplained_gap_but_blocked_report_preserves_count(self) -> None:
        ready = _complete_data()
        ready["unexplained_market_data_gap_count"] = 1
        with self.assertRaises(reporting.AlphaFeasibilityReportingError):
            reporting.build_completed_alpha_feasibility_report(
                commit_sha=COMMIT_SHA,
                data_summary=ready,
                development_metrics=_development(),
                validation_metrics=_validation(),
                experiment=self.experiment,
                generated_at=GENERATED_AT,
            )

        blocked = _blocked_data()
        blocked.update(
            {
                "pit_months_observed": 73,
                "union_instrument_count": 1200,
                "daily_coverage_status": "blocked",
                "adj_factor_coverage_status": "complete",
                "suspension_coverage_status": "blocked",
                "benchmark_coverage_status": "complete",
                "data_status": "BLOCKED_DATA",
                "remaining_blockers": ["unexplained_market_data_gap"],
                "valid_candidate_count_by_decision": {"2023-01-03": 1199},
                "insufficient_history_count_by_decision": {"2023-01-03": 1},
                "unexplained_market_data_gap_count": 2,
            }
        )
        report = reporting.build_blocked_alpha_feasibility_report(
            commit_sha=COMMIT_SHA,
            data_summary=blocked,
            experiment=self.experiment,
            generated_at=GENERATED_AT,
        )
        self.assertEqual(report["terminal_status"], "BLOCKED_DATA")
        self.assertEqual(report["unexplained_market_data_gap_count"], 2)
        self.assertEqual(
            report["insufficient_history_count_by_decision"], {"2023-01-03": 1}
        )
        self.assertIsNone(report["development_metrics"])

    def test_rehashed_terminal_and_concentration_drift_are_rejected(self) -> None:
        report = self._completed()
        forged = copy.deepcopy(report)
        forged["terminal_status"] = "ALPHA_FEASIBILITY_NO_GO"
        _resign(forged)
        with self.assertRaisesRegex(reporting.AlphaFeasibilityReportingError, "terminal_status derived result drift"):
            reporting.verify_alpha_feasibility_report(forged, experiment=self.experiment)

        forged = copy.deepcopy(report)
        forged["alpha_feasibility_engine_sha256"] = "0" * 64
        _resign(forged)
        with self.assertRaisesRegex(
            reporting.AlphaFeasibilityReportingError,
            "runtime provenance drift",
        ):
            reporting.verify_alpha_feasibility_report(
                forged, experiment=self.experiment
            )

        forged = copy.deepcopy(report)
        forged["concentration_metrics"]["largest_stock_pnl_share"] = "0.49"
        _resign(forged)
        with self.assertRaisesRegex(reporting.AlphaFeasibilityReportingError, "concentration_metrics derived result drift"):
            reporting.verify_alpha_feasibility_report(forged, experiment=self.experiment)

    def test_plain_hash_tamper_is_rejected(self) -> None:
        report = self._completed()
        report["union_instrument_count"] = 999
        with self.assertRaisesRegex(reporting.AlphaFeasibilityReportingError, "report_sha256 mismatch"):
            reporting.verify_alpha_feasibility_report(report, experiment=self.experiment)

    def test_publish_is_create_only_with_byte_identical_replay(self) -> None:
        report = self._completed()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = reporting.publish_alpha_feasibility_report(
                temp_dir,
                report,
                experiment=self.experiment,
            )
            original = path.read_bytes()
            replay = reporting.publish_alpha_feasibility_report(
                temp_dir,
                report,
                experiment=self.experiment,
            )
            self.assertEqual(replay, path)
            self.assertEqual(replay.read_bytes(), original)
            self.assertEqual(path.name, reporting.REPORT_FILENAME)

            different = copy.deepcopy(report)
            different["commit_sha"] = "0" * 40
            _resign(different)
            with self.assertRaisesRegex(
                reporting.AlphaFeasibilityReportingError,
                "create_only_report_exists_with_different_bytes",
            ):
                reporting.publish_alpha_feasibility_report(
                    temp_dir,
                    different,
                    experiment=self.experiment,
                )

    def test_config_semantic_and_frozen_file_hash_drift_are_rejected(self) -> None:
        drifted = copy.deepcopy(self.experiment)
        drifted["index"]["expected_component_count"] = 801
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "drifted.json"
            path.write_text(json.dumps(drifted), encoding="utf-8")
            with self.assertRaisesRegex(
                reporting.AlphaFeasibilityReportingError,
                r"(?:index drift|canonical content drift)",
            ):
                reporting.load_and_validate_experiment_config(path)

        with mock.patch.object(reporting, "_file_sha256", return_value="0" * 64):
            with self.assertRaisesRegex(reporting.AlphaFeasibilityReportingError, "frozen implementation hash drift"):
                reporting.validate_experiment_config(self.experiment)

    def test_config_gate_and_frozen_declared_hash_drift_are_rejected(self) -> None:
        drifted = copy.deepcopy(self.experiment)
        drifted["gate"]["validation_max_drawdown_max_inclusive"] = "0.13"
        with self.assertRaisesRegex(reporting.AlphaFeasibilityReportingError, "config"):
            reporting.validate_experiment_config(drifted)

        drifted = copy.deepcopy(self.experiment)
        drifted["frozen_implementation"]["alpha_source_sha256"] = "0" * 64
        with self.assertRaisesRegex(reporting.AlphaFeasibilityReportingError, "config"):
            reporting.validate_experiment_config(drifted)

    def test_report_does_not_copy_token_or_unknown_out_of_bound_date(self) -> None:
        data = _blocked_data()
        data["token"] = "super-secret-token-value"
        data["isolated_upstream_date"] = "2024-01-01"
        with mock.patch.dict(os.environ, {"TUSHARE_TOKEN": "super-secret-token-value"}):
            report = reporting.build_blocked_alpha_feasibility_report(
                commit_sha=COMMIT_SHA,
                data_summary=data,
                experiment=self.experiment,
                generated_at=GENERATED_AT,
            )
        serialized = json.dumps(report, sort_keys=True)
        self.assertNotIn("super-secret-token-value", serialized)
        self.assertNotIn("2024-01-01", serialized)

    def test_report_rejects_secret_or_out_of_split_data_date_in_emitted_fields(self) -> None:
        data = _blocked_data()
        data["remaining_blockers"] = ["provider returned 2024-01-01"]
        with self.assertRaisesRegex(reporting.AlphaFeasibilityReportingError, "out-of-bound data date"):
            reporting.build_blocked_alpha_feasibility_report(
                commit_sha=COMMIT_SHA,
                data_summary=data,
                experiment=self.experiment,
                generated_at=GENERATED_AT,
            )

        data = _blocked_data()
        for blocker in ("post_cutoff_20240101", "postcutoff20240101"):
            with self.subTest(blocker=blocker):
                data = _blocked_data()
                data["remaining_blockers"] = [blocker]
                with self.assertRaisesRegex(
                    reporting.AlphaFeasibilityReportingError,
                    "out-of-bound data date",
                ):
                    reporting.build_blocked_alpha_feasibility_report(
                        commit_sha=COMMIT_SHA,
                        data_summary=data,
                        experiment=self.experiment,
                        generated_at=GENERATED_AT,
                    )

        data = _blocked_data()
        data["remaining_blockers"] = ["super-secret-token-value"]
        with mock.patch.dict(os.environ, {"TUSHARE_TOKEN": "super-secret-token-value"}):
            with self.assertRaisesRegex(reporting.AlphaFeasibilityReportingError, "persist TUSHARE_TOKEN"):
                reporting.build_blocked_alpha_feasibility_report(
                    commit_sha=COMMIT_SHA,
                    data_summary=data,
                    experiment=self.experiment,
                    generated_at=GENERATED_AT,
                )

        validation = _validation()
        validation["base"]["worst_month"]["month"] = "2024-01"
        with self.assertRaisesRegex(reporting.AlphaFeasibilityReportingError, "outside the validation split"):
            self._completed(validation=validation)


if __name__ == "__main__":
    unittest.main()
