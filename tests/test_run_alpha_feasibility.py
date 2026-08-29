from __future__ import annotations

import copy
import io
import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from operations import run_alpha_feasibility as cli
from research.strategy_workspace import alpha_feasibility as engine


COMMIT_SHA = "33ac5f0e3c484a514288136ac5317830902e2105"
GENERATED_AT = "2026-08-28T12:00:00+08:00"


def _counts() -> dict[str, int]:
    return {
        "trade_cal": 1,
        "index_weight": 73,
        "daily": 1,
        "adj_factor": 1,
        "index_daily": 1,
        "suspend_d": 1,
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


def _backfill(*, stage: str, blocker: str | None = None) -> dict[str, object]:
    blocked = stage != cli.READY_STAGE
    adapter_blocked = stage == "BLOCKED_ADAPTER_PROTOCOL"
    return {
        "schema_version": "tushare-alpha-feasibility-backfill-result.v1",
        "experiment_id": "a-share-technical-alpha-feasibility-tushare-p1-v1",
        "stage_status": stage,
        "terminal_status": (
            "BLOCKED_ADAPTER_PROTOCOL"
            if adapter_blocked
            else "BLOCKED_DATA"
            if blocked
            else None
        ),
        "generated_at": GENERATED_AT,
        "actual_tushare_request_count_by_endpoint": _counts(),
        "first_index_weight": None if blocked else _first_index_weight(),
        "stock_basic_status": "DEFERRED_NOT_REQUIRED_FOR_ALPHA_FEASIBILITY",
        "stock_basic_request_count": 0,
        "security_master_pit_status": "NOT_IMPLEMENTED_NOT_REQUIRED_IN_P1",
        "coverage_start": "2017-07-01",
        "coverage_end": "2023-12-31",
        "pit_months_expected": 73,
        "pit_months_observed": 72 if blocked else 73,
        "union_instrument_count": 0 if blocked else 1,
        "valid_candidate_count_by_decision": (
            {} if blocked else {"2023-01-03": 1}
        ),
        "insufficient_history_count_by_decision": (
            {} if blocked else {"2023-01-03": 0}
        ),
        "ineligible_no_initial_price_count": 0,
        "unexplained_market_data_gap_count": 0,
        "collection_plan_sha256": "1" * 64,
        "pit_membership_manifest_sha256": "2" * 64,
        "history_manifest_sha256": None if blocked else "3" * 64,
        "daily_coverage_status": "BLOCKED_DATA" if blocked else "COMPLETE",
        "adj_factor_coverage_status": "BLOCKED_DATA" if blocked else "COMPLETE",
        "suspension_coverage_status": "BLOCKED_DATA" if blocked else "COMPLETE",
        "benchmark_coverage_status": "BLOCKED_DATA" if blocked else "COMPLETE",
        "remaining_blockers": (
            [blocker or "semantic_core_missing"]
            if adapter_blocked
            else [blocker or "pit_membership_incomplete"]
            if blocked
            else []
        ),
        "locked_test_status": dict(cli.LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
        "execution_realism": "INCOMPLETE",
        "trade_eligibility": False,
    }


def _loaded_inputs() -> dict[str, object]:
    return {
        "coverage_start": "2017-07-01",
        "coverage_end": "2023-12-31",
        "trading_dates": ["2017-07-03"],
        "pit_snapshots": [
            {"snapshot_date": "2017-12-29", "members": ["000001.SZ"]}
        ],
        "pit_coverage_report": {"evidence": "coverage"},
        "pit_manifest": {"evidence": "manifest"},
        "signal_bars": [
            {
                "trading_date": "2017-07-03",
                "instrument_id": "000001.SZ",
                "raw_open": "10",
                "adj_factor": "10.8",
                "open": "108",
                "close": "110",
                "high": "111",
            }
        ],
        "benchmark_bars": [
            {"trading_date": "2017-07-03", "close": "100", "high": "101"}
        ],
        "suspensions": [],
        "locked_test_status": dict(cli.LOCKED_TEST_STATUS),
        "locked_test_consumed": False,
        "execution_realism": "INCOMPLETE",
        "trade_eligibility": False,
    }


def _metrics(period: str, *, active: str) -> engine.AlphaFeasibilityMetrics:
    return engine.AlphaFeasibilityMetrics(
        net_return=Decimal("0.08"),
        benchmark_return=Decimal("0.05"),
        net_active_return=Decimal(active),
        max_drawdown=Decimal("0.10"),
        annualized_turnover=Decimal("1.2"),
        total_cost=Decimal("0.006"),
        average_gross_exposure=Decimal("0.60"),
        cash_day_fraction=Decimal("0.25"),
        exposure_state_distribution={
            "RISK_OFF": Decimal("0.10"),
            "DEFENSIVE": Decimal("0.20"),
            "NEUTRAL": Decimal("0.30"),
            "RISK_ON": Decimal("0.40"),
        },
        trade_or_rebalance_count=24,
        positive_month_rate=Decimal("0.58"),
        positive_half_year_count=2,
        worst_month=engine.PeriodActiveReturn(
            period=period,
            net_return=Decimal("-0.04"),
            benchmark_return=Decimal("-0.02"),
            net_active_return=Decimal("-0.02"),
        ),
        per_stock_pnl_contribution={"000001.SZ": Decimal("0.02")},
        largest_stock_pnl_share=Decimal("0.40"),
        largest_10_days_pnl_share=Decimal("0.35"),
    )


def _study() -> SimpleNamespace:
    development = SimpleNamespace(
        base=SimpleNamespace(metrics=_metrics("2020-03", active="0.04")),
        stress=SimpleNamespace(metrics=_metrics("2020-03", active="0.02")),
    )
    validation = SimpleNamespace(
        base=SimpleNamespace(metrics=_metrics("2023-08", active="0.03")),
        stress=SimpleNamespace(metrics=_metrics("2023-08", active="0.01")),
    )
    return SimpleNamespace(development=development, validation=validation)


class RunAlphaFeasibilityCliTests(unittest.TestCase):
    def test_locked_test_modules_are_never_imported(self) -> None:
        forbidden = (
            "tests.test_strategy_workspace_admission",
            "tests.test_strategy_workspace_evaluation",
            "tests.test_strategy_workspace_experiment",
            "tests.test_strategy_workspace_top_decile_backtest",
        )
        self.assertTrue(all(name not in sys.modules for name in forbidden))

    def test_adapter_protocol_failure_sets_remain_exactly_aligned(self) -> None:
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
        self.assertEqual(set(cli.data_lane.ADAPTER_PROTOCOL_FAILURES), expected)
        self.assertEqual(
            set(cli.reporting.ADAPTER_PROTOCOL_BLOCKERS),
            expected,
        )

    def test_date_and_endpoint_preflight_precede_token_and_output_access(self) -> None:
        source_config = json.loads(cli.reporting.DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
        mutations = {
            "post_cutoff": lambda value: value["dates"].__setitem__(
                "validation_end", "2024-01-01"
            ),
            "forbidden_endpoint": lambda value: value["source"][
                "allowed_endpoints"
            ].append("daily_basic"),
            "stock_basic_endpoint": lambda value: value["source"][
                "allowed_endpoints"
            ].append("stock_basic"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                config = copy.deepcopy(source_config)
                mutate(config)
                config_path = root / "unsafe.json"
                config_path.write_text(json.dumps(config), encoding="utf-8")
                output_root = root / "must-not-exist"
                with mock.patch.object(
                    cli.data_lane, "run_backfill_from_environment"
                ) as backfill, mock.patch("sys.stderr", new_callable=io.StringIO):
                    code = cli.main(
                        [
                            "all",
                            "--config",
                            str(config_path),
                            "--output-root",
                            str(output_root),
                        ]
                    )
                self.assertEqual(code, 2)
                backfill.assert_not_called()
                self.assertFalse(output_root.exists())

    def test_reporting_boundary_enforces_v3_fixed_fields_maps_and_gap_types(self) -> None:
        invalid_mutations = {
            "request_count_missing": lambda result: result[
                "actual_tushare_request_count_by_endpoint"
            ].pop("daily"),
            "request_count_stock_extra": lambda result: result[
                "actual_tushare_request_count_by_endpoint"
            ].__setitem__("stock_basic", 0),
            "request_count_bool": lambda result: result[
                "actual_tushare_request_count_by_endpoint"
            ].__setitem__("daily", True),
            "stock_status_missing": lambda result: result.pop(
                "stock_basic_status"
            ),
            "stock_status_drift": lambda result: result.__setitem__(
                "stock_basic_status", "READY"
            ),
            "stock_count_missing": lambda result: result.pop(
                "stock_basic_request_count"
            ),
            "stock_count_nonzero": lambda result: result.__setitem__(
                "stock_basic_request_count", 1
            ),
            "stock_count_bool": lambda result: result.__setitem__(
                "stock_basic_request_count", False
            ),
            "stock_count_float": lambda result: result.__setitem__(
                "stock_basic_request_count", 0.0
            ),
            "security_master_status_missing": lambda result: result.pop(
                "security_master_pit_status"
            ),
            "security_master_status_drift": lambda result: result.__setitem__(
                "security_master_pit_status", "READY"
            ),
            "valid_map_missing": lambda result: result.pop(
                "valid_candidate_count_by_decision"
            ),
            "valid_map_not_object": lambda result: result.__setitem__(
                "valid_candidate_count_by_decision", []
            ),
            "valid_map_bool": lambda result: result.__setitem__(
                "valid_candidate_count_by_decision", {"2023-01-03": True}
            ),
            "valid_map_float": lambda result: result.__setitem__(
                "valid_candidate_count_by_decision", {"2023-01-03": 1.0}
            ),
            "insufficient_map_missing": lambda result: result.pop(
                "insufficient_history_count_by_decision"
            ),
            "insufficient_map_negative": lambda result: result.__setitem__(
                "insufficient_history_count_by_decision", {"2023-01-03": -1}
            ),
            "future_decision": lambda result: result.__setitem__(
                "insufficient_history_count_by_decision", {"2024-01-02": 1}
            ),
            "invalid_decision": lambda result: result.__setitem__(
                "insufficient_history_count_by_decision", {"2023-02-30": 1}
            ),
            "decision_key_mismatch": lambda result: result.__setitem__(
                "insufficient_history_count_by_decision", {"2023-01-04": 0}
            ),
            "gap_missing": lambda result: result.pop(
                "unexplained_market_data_gap_count"
            ),
            "gap_bool": lambda result: result.__setitem__(
                "unexplained_market_data_gap_count", False
            ),
            "gap_float": lambda result: result.__setitem__(
                "unexplained_market_data_gap_count", 0.0
            ),
            "gap_negative": lambda result: result.__setitem__(
                "unexplained_market_data_gap_count", -1
            ),
            "ready_gap": lambda result: result.__setitem__(
                "unexplained_market_data_gap_count", 1
            ),
            "no_initial_missing": lambda result: result.pop(
                "ineligible_no_initial_price_count"
            ),
            "no_initial_bool": lambda result: result.__setitem__(
                "ineligible_no_initial_price_count", False
            ),
            "no_initial_negative": lambda result: result.__setitem__(
                "ineligible_no_initial_price_count", -1
            ),
            "first_missing": lambda result: result.pop("first_index_weight"),
            "first_order_drift": lambda result: result[
                "first_index_weight"
            ].__setitem__("field_order_matches_canonical", False),
            "first_incomplete_ready": lambda result: result[
                "first_index_weight"
            ].__setitem__("normalized_content_sha256", None),
        }
        for name, mutate in invalid_mutations.items():
            with self.subTest(name=name):
                result = _backfill(stage=cli.READY_STAGE)
                mutate(result)
                with self.assertRaises(cli.AlphaFeasibilityWorkflowError):
                    cli._reporting_data_summary(result)

        summary = cli._reporting_data_summary(_backfill(stage=cli.READY_STAGE))
        self.assertEqual(
            set(summary["actual_tushare_request_count_by_endpoint"]),
            set(cli.reporting.ALLOWED_ENDPOINTS),
        )
        self.assertNotIn(
            "stock_basic", summary["actual_tushare_request_count_by_endpoint"]
        )
        self.assertEqual(
            summary["stock_basic_status"],
            "DEFERRED_NOT_REQUIRED_FOR_ALPHA_FEASIBILITY",
        )
        self.assertEqual(summary["stock_basic_request_count"], 0)
        self.assertIs(type(summary["stock_basic_request_count"]), int)
        self.assertEqual(
            summary["security_master_pit_status"],
            "NOT_IMPLEMENTED_NOT_REQUIRED_IN_P1",
        )
        self.assertEqual(
            summary["valid_candidate_count_by_decision"], {"2023-01-03": 1}
        )
        self.assertEqual(
            summary["insufficient_history_count_by_decision"],
            {"2023-01-03": 0},
        )
        self.assertEqual(summary["ineligible_no_initial_price_count"], 0)
        self.assertEqual(summary["first_index_weight"], _first_index_weight())
        self.assertEqual(summary["unexplained_market_data_gap_count"], 0)

    def test_reporting_boundary_accepts_extra_or_reordered_provider_fields(self) -> None:
        for observed, order_matches in (
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
        ):
            with self.subTest(observed=observed):
                result = _backfill(stage=cli.READY_STAGE)
                result["first_index_weight"].update(
                    {
                        "observed_data_fields": observed,
                        "extra_data_fields": ["index_name"],
                        "field_order_matches_canonical": order_matches,
                    }
                )
                summary = cli._reporting_data_summary(result)
                self.assertEqual(
                    summary["first_index_weight"]["extra_data_fields"],
                    ["index_name"],
                )

    def test_pit_block_publishes_blocked_report_and_never_enters_alpha(self) -> None:
        blocked = _backfill(stage="BLOCKED_PIT_MEMBERSHIP")
        blocked["untrusted_provider_text"] = "super-secret-token-value 2024-01-01"
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            cli.data_lane,
            "run_backfill_from_environment",
            return_value=blocked,
        ), mock.patch.object(
            cli.data_lane, "load_feasibility_inputs"
        ) as load_inputs, mock.patch.object(
            cli.engine, "run_alpha_feasibility_study"
        ) as run_alpha, mock.patch.object(
            cli, "_current_commit_sha", return_value=COMMIT_SHA
        ), mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            code = cli.main(
                [
                    "all",
                    "--output-root",
                    temp_dir,
                    "--generated-at",
                    GENERATED_AT,
                ]
            )
            serialized = stdout.getvalue()
            report = json.loads(
                (Path(temp_dir) / "alpha_feasibility_report.json").read_text(
                    encoding="utf-8"
                )
            )
        self.assertEqual(code, 1)
        load_inputs.assert_not_called()
        run_alpha.assert_not_called()
        self.assertEqual(report["terminal_status"], "BLOCKED_DATA")
        self.assertIsNone(report["development_metrics"])
        self.assertIsNone(report["validation_metrics"])
        self.assertEqual(report["locked_test_status"], cli.LOCKED_TEST_STATUS)
        self.assertIs(report["locked_test_consumed"], False)
        self.assertEqual(report["stock_basic_request_count"], 0)
        self.assertEqual(report["valid_candidate_count_by_decision"], {})
        self.assertEqual(report["insufficient_history_count_by_decision"], {})
        self.assertEqual(report["ineligible_no_initial_price_count"], 0)
        self.assertIsNone(report["first_index_weight"])
        self.assertEqual(report["unexplained_market_data_gap_count"], 0)
        self.assertNotIn("super-secret-token-value", serialized)
        self.assertNotIn("2024-01-01", serialized)

    def test_unexplained_gap_block_preserves_count_and_never_enters_alpha(self) -> None:
        blocked = _backfill(
            stage="BLOCKED_DATA", blocker="unexplained_market_data_gap"
        )
        blocked.update(
            {
                "pit_months_observed": 73,
                "union_instrument_count": 1,
                "valid_candidate_count_by_decision": {"2023-01-03": 0},
                "insufficient_history_count_by_decision": {"2023-01-03": 1},
                "unexplained_market_data_gap_count": 2,
                "adj_factor_coverage_status": "COMPLETE",
                "benchmark_coverage_status": "COMPLETE",
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            cli.data_lane,
            "run_backfill_from_environment",
            return_value=blocked,
        ), mock.patch.object(
            cli.data_lane, "load_feasibility_inputs"
        ) as load_inputs, mock.patch.object(
            cli.engine, "run_alpha_feasibility_study"
        ) as run_alpha, mock.patch.object(
            cli, "_current_commit_sha", return_value=COMMIT_SHA
        ), mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            code = cli.main(
                [
                    "all",
                    "--output-root",
                    temp_dir,
                    "--generated-at",
                    GENERATED_AT,
                ]
            )
            summary = json.loads(stdout.getvalue())
            report = json.loads(
                (Path(temp_dir) / "alpha_feasibility_report.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(code, 1)
        self.assertEqual(summary["terminal_status"], "BLOCKED_DATA")
        self.assertEqual(report["terminal_status"], "BLOCKED_DATA")
        self.assertEqual(report["remaining_blockers"], ["unexplained_market_data_gap"])
        self.assertEqual(summary["unexplained_market_data_gap_count"], 2)
        self.assertEqual(report["unexplained_market_data_gap_count"], 2)
        self.assertEqual(
            report["insufficient_history_count_by_decision"],
            {"2023-01-03": 1},
        )
        self.assertIsNone(report["development_metrics"])
        self.assertIsNone(report["validation_metrics"])
        load_inputs.assert_not_called()
        run_alpha.assert_not_called()

    def test_adapter_protocol_block_retains_distinct_terminal_and_skips_alpha(self) -> None:
        blocked = _backfill(stage="BLOCKED_ADAPTER_PROTOCOL")
        blocked["pit_months_observed"] = 0
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            cli.data_lane,
            "run_backfill_from_environment",
            return_value=blocked,
        ), mock.patch.object(
            cli.data_lane, "load_feasibility_inputs"
        ) as load_inputs, mock.patch.object(
            cli.engine, "run_alpha_feasibility_study"
        ) as run_alpha, mock.patch.object(
            cli, "_current_commit_sha", return_value=COMMIT_SHA
        ), mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            code = cli.main(
                [
                    "all",
                    "--output-root",
                    temp_dir,
                    "--generated-at",
                    GENERATED_AT,
                ]
            )
            summary = json.loads(stdout.getvalue())
            report = json.loads(
                (Path(temp_dir) / "alpha_feasibility_report.json").read_text(
                    encoding="utf-8"
                )
            )
        self.assertEqual(code, 1)
        self.assertEqual(summary["terminal_status"], "BLOCKED_ADAPTER_PROTOCOL")
        self.assertEqual(report["terminal_status"], "BLOCKED_ADAPTER_PROTOCOL")
        load_inputs.assert_not_called()
        run_alpha.assert_not_called()

    def test_nonzero_upstream_code_with_controlled_extensions_is_blocked_data(self) -> None:
        task = cli.data_lane.CollectionTask(
            endpoint="trade_cal",
            params={
                "exchange": "SSE",
                "start_date": "20170701",
                "end_date": "20231231",
            },
            fields=cli.data_lane.EXPECTED_FIELDS["trade_cal"],
            plan_sha256="1" * 64,
        )
        raw = json.dumps(
            {
                "code": 2002,
                "msg": None,
                "data": {"provider_status": "denied"},
                "detail": {"safe_hint": "do-not-persist-this-value"},
                "request_id": "request-opaque-1",
                "trace_id": ["trace-opaque-1"],
            },
            separators=(",", ":"),
        ).encode("utf-8")
        with self.assertRaises(cli.data_lane.AlphaFeasibilityDataError) as raised:
            cli.data_lane.validate_response_bytes(
                task,
                raw,
                token="UnitTestCredentialNeverPersist123456",
            )
        upstream_error = raised.exception
        self.assertEqual(upstream_error.code, "upstream_permission_error")
        self.assertNotIn(upstream_error.code, cli.data_lane.ADAPTER_PROTOCOL_FAILURES)
        self.assertEqual(upstream_error.diagnostic["upstream_code"], 2002)
        self.assertEqual(upstream_error.diagnostic["upstream_error_category"], "permission")
        self.assertEqual(
            upstream_error.diagnostic["transport_extension_field_names"],
            ["detail", "request_id", "trace_id"],
        )
        self.assertEqual(
            upstream_error.diagnostic["transport_extension_type_by_field"],
            {"detail": "object", "request_id": "string", "trace_id": "array"},
        )
        self.assertNotIn(
            "do-not-persist-this-value",
            repr(upstream_error.diagnostic),
        )

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            cli.data_lane,
            "run_backfill_from_environment",
            side_effect=upstream_error,
        ), mock.patch.object(
            cli, "_current_commit_sha", return_value=COMMIT_SHA
        ), mock.patch.object(
            cli.engine, "run_alpha_feasibility_study"
        ) as run_alpha, mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            code = cli.main(
                [
                    "all",
                    "--output-root",
                    temp_dir,
                    "--generated-at",
                    GENERATED_AT,
                ]
            )
            serialized = stdout.getvalue()
            summary = json.loads(serialized)
            report = json.loads(
                (Path(temp_dir) / "alpha_feasibility_report.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(code, 1)
        self.assertEqual(summary["terminal_status"], "BLOCKED_DATA")
        self.assertEqual(report["terminal_status"], "BLOCKED_DATA")
        self.assertEqual(report["remaining_blockers"], ["upstream_permission_error"])
        persisted_text = serialized + json.dumps(report, sort_keys=True)
        self.assertNotIn("do-not-persist-this-value", persisted_text)
        self.assertNotIn("request-opaque-1", persisted_text)
        self.assertNotIn("trace-opaque-1", persisted_text)
        run_alpha.assert_not_called()

    def test_missing_token_after_valid_preflight_is_blocked_data_not_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            cli.data_lane,
            "run_backfill_from_environment",
            side_effect=cli.data_lane.AlphaFeasibilityDataError(
                "missing_tushare_token"
            ),
        ), mock.patch.object(
            cli, "_current_commit_sha", return_value=COMMIT_SHA
        ), mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            code = cli.main(
                [
                    "all",
                    "--output-root",
                    temp_dir,
                    "--generated-at",
                    GENERATED_AT,
                ]
            )
            summary = json.loads(stdout.getvalue())
            report = json.loads(
                (Path(temp_dir) / "alpha_feasibility_report.json").read_text(
                    encoding="utf-8"
                )
            )
        self.assertEqual(code, 1)
        self.assertEqual(summary["terminal_status"], "BLOCKED_DATA")
        self.assertEqual(report["terminal_status"], "BLOCKED_DATA")
        self.assertIn("missing_tushare_token", report["remaining_blockers"])
        self.assertEqual(
            report["actual_tushare_request_count_by_endpoint"],
            {endpoint: 0 for endpoint in cli.reporting.ALLOWED_ENDPOINTS},
        )
        self.assertEqual(report["stock_basic_request_count"], 0)
        self.assertEqual(
            report["stock_basic_status"],
            "DEFERRED_NOT_REQUIRED_FOR_ALPHA_FEASIBILITY",
        )
        self.assertEqual(
            report["security_master_pit_status"],
            "NOT_IMPLEMENTED_NOT_REQUIRED_IN_P1",
        )
        self.assertEqual(report["valid_candidate_count_by_decision"], {})
        self.assertEqual(report["insufficient_history_count_by_decision"], {})
        self.assertEqual(report["ineligible_no_initial_price_count"], 0)
        self.assertIsNone(report["first_index_weight"])
        self.assertEqual(report["unexplained_market_data_gap_count"], 0)

    def test_precollection_block_replays_byte_identically_without_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            cli.data_lane,
            "run_backfill_from_environment",
            side_effect=cli.data_lane.AlphaFeasibilityDataError(
                "missing_tushare_token"
            ),
        ), mock.patch.object(
            cli, "_current_commit_sha", return_value=COMMIT_SHA
        ):
            outputs: list[dict[str, object]] = []
            original: bytes | None = None
            for _ in range(2):
                with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    code = cli.main(["all", "--output-root", temp_dir])
                self.assertEqual(code, 1)
                outputs.append(json.loads(stdout.getvalue()))
                current = (
                    Path(temp_dir) / "alpha_feasibility_report.json"
                ).read_bytes()
                if original is None:
                    original = current
                else:
                    self.assertEqual(current, original)
            self.assertEqual(outputs[0], outputs[1])

    def test_completed_wiring_requires_adjusted_open_and_preserves_safety(self) -> None:
        captured: dict[str, engine.AlphaFeasibilityInput] = {}
        loaded = _loaded_inputs()
        loaded["signal_bars"] = iter(loaded["signal_bars"])
        loaded["suspensions"] = iter(loaded["suspensions"])

        def fake_run(*, inputs: engine.AlphaFeasibilityInput) -> SimpleNamespace:
            captured["inputs"] = inputs
            return _study()

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            cli.data_lane,
            "run_backfill_from_environment",
            return_value=_backfill(stage=cli.READY_STAGE),
        ), mock.patch.object(
            cli.data_lane,
            "load_feasibility_inputs",
            return_value=loaded,
        ), mock.patch.object(
            cli.engine, "run_alpha_feasibility_study", side_effect=fake_run
        ), mock.patch.object(
            cli, "_current_commit_sha", return_value=COMMIT_SHA
        ), mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            code = cli.main(
                [
                    "all",
                    "--output-root",
                    temp_dir,
                    "--generated-at",
                    GENERATED_AT,
                ]
            )
            summary = json.loads(stdout.getvalue())
            report = json.loads(
                (Path(temp_dir) / "alpha_feasibility_report.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(code, 0)
        signal_bars = tuple(captured["inputs"].stock_signal_bars)
        self.assertEqual(signal_bars[0].open, Decimal("108"))
        self.assertNotEqual(signal_bars[0].open, signal_bars[0].close)
        self.assertIsInstance(captured["inputs"].pit_admission, engine.PITAdmissionArtifacts)
        self.assertEqual(report["terminal_status"], "ALPHA_FEASIBILITY_GO_CANDIDATE")
        self.assertEqual(summary["terminal_status"], report["terminal_status"])
        self.assertEqual(report["safety"]["execution_realism"], "INCOMPLETE")
        self.assertIs(report["safety"]["paper_eligibility"], False)
        self.assertIs(report["safety"]["trade_eligibility"], False)
        self.assertIs(report["safety"]["automatic_order_submission"], False)
        self.assertEqual(report["locked_test_status"], cli.LOCKED_TEST_STATUS)
        self.assertIs(report["locked_test_consumed"], False)
        self.assertEqual(
            report["valid_candidate_count_by_decision"], {"2023-01-03": 1}
        )
        self.assertEqual(
            report["insufficient_history_count_by_decision"],
            {"2023-01-03": 0},
        )
        self.assertEqual(report["ineligible_no_initial_price_count"], 0)
        self.assertEqual(report["first_index_weight"], _first_index_weight())
        self.assertEqual(report["unexplained_market_data_gap_count"], 0)
        self.assertEqual(report["stock_basic_request_count"], 0)

    def test_missing_adjusted_open_fails_before_alpha_engine(self) -> None:
        loaded = _loaded_inputs()
        loaded["signal_bars"][0].pop("open")
        with self.assertRaisesRegex(cli.AlphaFeasibilityWorkflowError, "adjusted_open_required"):
            inputs = cli.build_alpha_input(loaded)
            tuple(inputs.stock_signal_bars)

    def test_data_command_stops_at_ready_data_without_alpha(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            cli.data_lane,
            "run_backfill_from_environment",
            return_value=_backfill(stage=cli.READY_STAGE),
        ), mock.patch.object(
            cli.data_lane, "load_feasibility_inputs"
        ) as load_inputs, mock.patch.object(
            cli.engine, "run_alpha_feasibility_study"
        ) as run_alpha, mock.patch.object(
            cli, "_current_commit_sha", return_value=COMMIT_SHA
        ), mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            code = cli.main(["data", "--output-root", temp_dir])
            summary = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(summary["stage_status"], cli.READY_STAGE)
        self.assertEqual(summary["stock_basic_request_count"], 0)
        self.assertEqual(
            summary["valid_candidate_count_by_decision"], {"2023-01-03": 1}
        )
        self.assertEqual(
            summary["insufficient_history_count_by_decision"],
            {"2023-01-03": 0},
        )
        self.assertEqual(summary["ineligible_no_initial_price_count"], 0)
        self.assertEqual(summary["first_index_weight"], _first_index_weight())
        self.assertEqual(summary["unexplained_market_data_gap_count"], 0)
        self.assertEqual(summary["locked_test_status"], cli.LOCKED_TEST_STATUS)
        self.assertIs(summary["locked_test_consumed"], False)
        load_inputs.assert_not_called()
        run_alpha.assert_not_called()


if __name__ == "__main__":
    unittest.main()
