from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from research.strategy_workspace.experiment import (
    ExperimentContractError,
    ExperimentSpecV2,
    HISTORICAL_GATE_CONTRACTS,
    QUALITY_GROWTH_FACTOR_CONTRACTS,
    RESIDUALIZATION_CONTROL_CONTRACTS,
    read_experiment_spec,
    write_new_experiment_spec,
)
from research.strategy_workspace.admission import REQUIRED_HISTORICAL_GATE_IDS


ROOT = Path(__file__).resolve().parents[1]


def _valid_content() -> dict[str, object]:
    factors = [
        {
            **dict(item),
            "required_fields": list(item["required_fields"]),
        }
        for item in QUALITY_GROWTH_FACTOR_CONTRACTS
    ]
    return {
        "schema_version": "strategy-experiment-v2",
        "experiment_id": "quality-growth-ridge-001",
        "created_at": "2026-08-18T09:00:00+08:00",
        "status": "preregistered_frozen",
        "universe": {
            "universe_id": "csi800-pit-panel",
            "membership_dataset_id": "CSI800_PIT",
            "effective_interval": {
                "start_date": "2018-01-01",
                "end_date": "2026-08-18",
            },
            "selection_rule": "membership_effective_at_decision",
            "backfill_policy": "forbid_current_constituent_backfill",
            "membership_panel_receipt_sha256": "1" * 64,
            "membership_panel_content_sha256": "2" * 64,
        },
        "benchmark": {
            "instrument_id": "H00906.CSI",
            "provider_id": "choice",
            "return_basis": "total_return",
            "instrument_id_source_receipt_sha256": "3" * 64,
            "total_return_series_content_sha256": "4" * 64,
        },
        "target": {
            "target_id": "excess-return-20-session",
            "horizon_trading_sessions": 20,
            "definition": "future_20_session_open_to_open_excess_total_return",
            "signal_cutoff": "decision_session_close",
            "entry_policy": "next_trading_session_open",
            "exit_policy": "rebalance_open_after_20_trading_sessions",
            "benchmark_alignment": "same_sessions_same_return_basis",
            "rebalance_anchor_date": "2018-01-02",
            "rebalance_anchor_rule": "first_controlled_session_on_or_after_2018-01-01",
            "trading_calendar_content_sha256": "7" * 64,
        },
        "factors": factors,
        "controls": [dict(item) for item in RESIDUALIZATION_CONTROL_CONTRACTS],
        "splits": {
            "train": {"start_date": "2018-01-01", "end_date": "2022-12-31"},
            "validation": {"start_date": "2023-01-01", "end_date": "2023-12-31"},
            "locked_test": {"start_date": "2024-01-01", "end_date": "2025-12-31"},
            "second_audit": {"start_date": "2026-01-01", "end_date": "2026-08-18"},
            "preregistration_cutoff": "2026-08-18",
            "purge_sessions": 20,
            "locked_test_freshness": "fresh_unconsumed",
            "second_audit_freshness": "fresh_unconsumed",
        },
        "ridge": {
            "model": "ridge",
            "alpha": "1.00",
            "fit_intercept": True,
            "standardization": "cross_sectional_train_parameters_only",
            "fit_scope": "train_only",
        },
        "statistics": {
            "fama_macbeth_hac_lag_rule": "andrews_automatic_floor_4_t_over_100_pow_2_over_9",
            "fama_macbeth_min_periods": 2,
            "multiple_testing": "holm",
            "familywise_alpha": "0.05",
            "rank_ic_evaluation_splits": ["validation", "locked_test", "audit"],
            "rank_ic_mean_threshold": "0",
            "rank_ic_positive_fraction_threshold": "0.5",
            "factor_significance_splits": ["locked_test", "audit"],
            "ridge_submodel_policy": "financial_2_factor_nonfinancial_6_factor",
        },
        "cost": {
            "currency": "CNY",
            "commission_rate": "0.0001800",
            "minimum_commission": "5.00",
            "sell_stamp_tax_rate": "0.0005",
            "transfer_fee_rate_both_sides": "0.00001",
            "base_slippage_bps_one_way": "10.0",
            "stress_slippage_bps_one_way": "20",
            "stress_commission_multiplier": "2.0",
            "historical_rate_replay": False,
        },
        "portfolio": {
            "initial_capital": "10000.00",
            "top_decile_research_capital": "1000000",
            "lot_size_policy": "per_instrument_metadata",
            "max_positions": 2,
            "max_weight_per_position": "0.40",
            "cash_reserve_weight": "0.20",
            "rebalance_sessions": 20,
            "max_drawdown": "0.12",
            "entry_top_fraction": "0.05",
            "hold_top_fraction": "0.20",
            "positive_prediction_required": True,
            "manual_veto_policy": "leave_cash_no_substitute",
            "selected_positions_industry_policy": "distinct_level1_industry",
            "combined_account_level1_industry_cap": "0.45",
            "annual_one_way_turnover_cap": "4.0",
            "trial_duration_months": 12,
            "paper_decision_points": 12,
            "execution_mode": "paper_only",
            "long_only": True,
            "unmanaged_external_assets": [
                {
                    "instrument_id": "000333.SZ",
                    "quantity": 100,
                    "status": "unmanaged_external",
                    "level1_industry_code": "choice-level1-home-appliances",
                    "industry_source_receipt_sha256": "9" * 64,
                }
            ],
        },
        "gates": [
            {
                **dict(gate),
                "scope": "validation_and_all_audits",
                "failure_action": "reject",
            }
            for gate in HISTORICAL_GATE_CONTRACTS
        ],
        "hashes": {
            "data_receipt_sha256": ["4" * 64, "3" * 64],
            "code_sha256": "5" * 64,
            "config_sha256": "6" * 64,
        },
        "consumed_test_intervals": [],
    }


class ExperimentSpecV2Tests(unittest.TestCase):
    def test_hash_is_deterministic_after_order_and_decimal_normalisation(self) -> None:
        first = _valid_content()
        second = deepcopy(first)
        second["factors"].reverse()  # type: ignore[index, union-attr]
        second["controls"].reverse()  # type: ignore[index, union-attr]
        second["gates"].reverse()  # type: ignore[index, union-attr]
        second["hashes"]["data_receipt_sha256"].reverse()  # type: ignore[index, union-attr]
        second["cost"]["commission_rate"] = "0.00018"  # type: ignore[index]
        second["portfolio"]["initial_capital"] = "10000"  # type: ignore[index]

        spec_one = ExperimentSpecV2.create(first)
        spec_two = ExperimentSpecV2.create(second)

        self.assertEqual(spec_one.spec_sha256, spec_two.spec_sha256)
        self.assertEqual(spec_one.to_dict(), spec_two.to_dict())
        self.assertEqual(
            [factor["factor_id"] for factor in spec_one.to_dict()["factors"]],
            sorted(factor["factor_id"] for factor in first["factors"]),
        )

    def test_contract_freezes_six_factors_and_exact_twenty_session_target(self) -> None:
        too_few = _valid_content()
        too_few["factors"] = too_few["factors"][:-1]  # type: ignore[index]
        with self.assertRaisesRegex(ExperimentContractError, "exactly six"):
            ExperimentSpecV2.create(too_few)

        wrong_horizon = _valid_content()
        wrong_horizon["target"]["horizon_trading_sessions"] = 21  # type: ignore[index]
        with self.assertRaisesRegex(ExperimentContractError, "exactly 20"):
            ExperimentSpecV2.create(wrong_horizon)

        tuned_alpha = _valid_content()
        tuned_alpha["ridge"]["alpha"] = "2"  # type: ignore[index]
        with self.assertRaisesRegex(ExperimentContractError, "fixed at 1"):
            ExperimentSpecV2.create(tuned_alpha)

        shifted_anchor = _valid_content()
        shifted_anchor["target"]["rebalance_anchor_rule"] = "best_historical_phase"  # type: ignore[index]
        with self.assertRaisesRegex(ExperimentContractError, "rebalance_anchor_rule"):
            ExperimentSpecV2.create(shifted_anchor)

        late_anchor = _valid_content()
        late_anchor["target"]["rebalance_anchor_date"] = "2019-01-02"  # type: ignore[index]
        with self.assertRaisesRegex(ExperimentContractError, "frozen 2018-01-02"):
            ExperimentSpecV2.create(late_anchor)

    def test_v1_factors_controls_costs_external_asset_and_all_gates_are_exact(self) -> None:
        wrong_factor = _valid_content()
        wrong_factor["factors"][0]["formula"] = "future_aware_formula"  # type: ignore[index]
        with self.assertRaisesRegex(ExperimentContractError, "frozen V1 quality-growth"):
            ExperimentSpecV2.create(wrong_factor)

        missing_control = _valid_content()
        missing_control["controls"] = missing_control["controls"][:-1]  # type: ignore[index]
        with self.assertRaisesRegex(ExperimentContractError, "controls must exactly"):
            ExperimentSpecV2.create(missing_control)

        cheap_slippage = _valid_content()
        cheap_slippage["cost"]["base_slippage_bps_one_way"] = "5"  # type: ignore[index]
        with self.assertRaisesRegex(ExperimentContractError, "must be 10"):
            ExperimentSpecV2.create(cheap_slippage)

        wrong_external_quantity = _valid_content()
        wrong_external_quantity["portfolio"]["unmanaged_external_assets"][0]["quantity"] = 0  # type: ignore[index]
        with self.assertRaises(ExperimentContractError):
            ExperimentSpecV2.create(wrong_external_quantity)

        missing_gate = _valid_content()
        missing_gate["gates"] = missing_gate["gates"][:-1]  # type: ignore[index]
        with self.assertRaisesRegex(ExperimentContractError, "all frozen V1"):
            ExperimentSpecV2.create(missing_gate)

    def test_consumed_test_intervals_cannot_be_called_fresh(self) -> None:
        content = _valid_content()
        content["consumed_test_intervals"] = [
            {
                "start_date": "2025-06-01",
                "end_date": "2025-12-31",
                "source_experiment_id": "earlier-test",
                "source_run_sha256": "7" * 64,
            }
        ]
        with self.assertRaisesRegex(ExperimentContractError, "overlaps a consumed"):
            ExperimentSpecV2.create(content)

        content["splits"]["locked_test_freshness"] = "retrospective_consumed"  # type: ignore[index]
        spec = ExperimentSpecV2.create(content)
        self.assertEqual(
            spec.to_dict()["splits"]["locked_test_freshness"],
            "retrospective_consumed",
        )

    def test_write_is_create_only_and_read_verifies_hash(self) -> None:
        spec = ExperimentSpecV2.create(_valid_content())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "experiment.json"
            write_new_experiment_spec(path, spec)
            self.assertEqual(read_experiment_spec(path).to_dict(), spec.to_dict())
            with self.assertRaisesRegex(ExperimentContractError, "refusing to overwrite"):
                write_new_experiment_spec(path, spec)

        tampered = spec.to_dict()
        tampered["hashes"]["code_sha256"] = "f" * 64
        with self.assertRaisesRegex(ExperimentContractError, "spec_sha256 mismatch"):
            ExperimentSpecV2.from_dict(tampered)

    def test_schema_declares_v2_and_exact_target(self) -> None:
        schema = json.loads(
            (ROOT / "schemas" / "strategy_experiment.v2.json").read_text(encoding="utf-8")
        )
        self.assertEqual(schema["properties"]["schema_version"]["const"], "strategy-experiment-v2")
        self.assertEqual(
            schema["properties"]["target"]["properties"]["horizon_trading_sessions"]["const"],
            20,
        )
        self.assertEqual(schema["properties"]["factors"]["minItems"], 6)
        self.assertEqual(schema["properties"]["factors"]["maxItems"], 6)
        self.assertEqual(
            schema["properties"]["ridge"]["properties"]["alpha"]["const"], "1"
        )
        self.assertEqual(
            schema["properties"]["portfolio"]["properties"]["lot_size_policy"]["const"],
            "per_instrument_metadata",
        )

    def test_frozen_constants_match_policy_and_admission_contracts(self) -> None:
        policy = json.loads(
            (ROOT / "configs" / "strategy_quality_growth.v1.json").read_text(
                encoding="utf-8"
            )
        )
        expected_factor_core = [
            {
                key: factor[key]
                for key in (
                    "factor_id",
                    "formula",
                    "expected_sign",
                    "financial_applicability",
                )
            }
            for factor in policy["factors"]
        ]
        actual_factor_core = [
            {
                key: factor[key]
                for key in (
                    "factor_id",
                    "formula",
                    "expected_sign",
                    "financial_applicability",
                )
            }
            for factor in QUALITY_GROWTH_FACTOR_CONTRACTS
        ]
        self.assertEqual(actual_factor_core, expected_factor_core)
        self.assertEqual(
            [item["control_id"] for item in RESIDUALIZATION_CONTROL_CONTRACTS],
            policy["preprocessing"]["residualize_controls"],
        )
        self.assertEqual(
            {item["gate_id"] for item in HISTORICAL_GATE_CONTRACTS},
            set(REQUIRED_HISTORICAL_GATE_IDS),
        )


if __name__ == "__main__":
    unittest.main()
