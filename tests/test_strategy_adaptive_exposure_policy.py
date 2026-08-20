from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from research.strategy_workspace.adaptive_exposure import (
    ADAPTIVE_EXPOSURE_SCHEMA_VERSION,
    ADAPTIVE_EXPOSURE_STRATEGY_ID,
    DEFAULT_ADAPTIVE_EXPOSURE_POLICY_PATH,
    FROZEN_ADAPTIVE_EXPOSURE_POLICY_SHA256,
    AdaptiveExposurePolicyError,
    load_adaptive_exposure_policy,
    validate_adaptive_exposure_policy,
)
from research.strategy_workspace.contracts import canonical_sha256


class AdaptiveExposurePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = load_adaptive_exposure_policy()
        self.payload = self.policy.to_dict()

    def assert_drift_rejected(self, path: tuple[object, ...], value: object) -> None:
        changed = deepcopy(self.payload)
        cursor = changed
        for key in path[:-1]:
            cursor = cursor[key]  # type: ignore[index]
        cursor[path[-1]] = value  # type: ignore[index]
        with self.assertRaises(AdaptiveExposurePolicyError):
            validate_adaptive_exposure_policy(changed)

    def test_repository_policy_loads_as_immutable_hash_bound_p0_contract(self) -> None:
        self.assertEqual(self.policy.strategy_id, ADAPTIVE_EXPOSURE_STRATEGY_ID)
        self.assertEqual(
            self.payload["schema_version"], ADAPTIVE_EXPOSURE_SCHEMA_VERSION
        )
        self.assertEqual(
            self.payload["contract_status"],
            "p0_runtime_implemented_not_admitted",
        )
        self.assertEqual(self.policy.research_status, "blocked_missing_pit_data")
        self.assertEqual(self.policy.challenge_target, Decimal("0.10"))
        self.assertEqual(
            self.policy.exposure_states,
            (Decimal("0.00"), Decimal("0.30"), Decimal("0.60"), Decimal("1.00")),
        )
        self.assertEqual(
            self.policy.policy_sha256,
            FROZEN_ADAPTIVE_EXPOSURE_POLICY_SHA256,
        )
        self.assertEqual(
            self.policy.policy_sha256,
            canonical_sha256(self.policy.to_dict()),
        )
        self.assertFalse(self.policy.paper_eligible)
        self.assertFalse(self.policy.trade_eligible)
        self.assertFalse(self.policy.live_supported)
        with self.assertRaises(TypeError):
            self.policy.raw["strategy_id"] = "changed"  # type: ignore[index]
        with self.assertRaises(TypeError):
            self.policy.raw["portfolio"]["max_positions"] = 4  # type: ignore[index]

    def test_status_identity_and_any_unlisted_content_drift_fail_closed(self) -> None:
        mutations = (
            (("schema_version",), "strategy-adaptive-exposure-policy.v2"),
            (("strategy_id",), "other-strategy"),
            (("contract_status",), "implemented"),
            (("research_status",), "admitted"),
            (("execution_status",), "paper_only"),
            (("description",), "changed but semantically harmless"),
            (("v1_compatibility", "historical_results_transferable"), True),
        )
        for path, value in mutations:
            with self.subTest(path=path):
                self.assert_drift_rejected(path, value)

        changed = deepcopy(self.payload)
        changed["unexpected"] = True
        with self.assertRaisesRegex(AdaptiveExposurePolicyError, "content hash"):
            validate_adaptive_exposure_policy(changed)

    def test_daily_paper_ledger_contract_is_bound_without_granting_admission(self) -> None:
        ledger = self.payload["paper_ledger_contract"]
        self.assertEqual(ledger["schema_version"], "strategy-paper-ledger-record.v2")
        self.assertEqual(ledger["record_types"], ["header", "daily_session"])
        self.assertEqual(ledger["frequency"], "each_controlled_session")
        self.assertEqual(
            ledger["append_policy"],
            "same_session_close_append_only_no_backfill",
        )
        self.assertEqual(
            ledger["drawdown_latch"],
            "sticky_for_ledger_lifetime_no_reentry",
        )
        self.assertEqual(
            ledger["reset_or_new_ledger_orchestration"],
            "not_implemented",
        )
        self.assertIs(ledger["live_supported"], False)
        mutations = (
            (("paper_ledger_contract", "schema_version"), "v3"),
            (("paper_ledger_contract", "record_types"), ["header"]),
            (("paper_ledger_contract", "frequency"), "alpha_only"),
            (("paper_ledger_contract", "drawdown_latch"), "sticky_until_flat"),
            (
                ("paper_ledger_contract", "reset_or_new_ledger_orchestration"),
                "automatic_reset",
            ),
            (("paper_ledger_contract", "live_supported"), True),
        )
        for path, value in mutations:
            with self.subTest(path=path):
                self.assert_drift_rejected(path, value)

    def test_monthly_ten_percent_is_reporting_only_never_a_gate_or_objective(self) -> None:
        challenge = self.payload["challenge"]
        self.assertEqual(challenge["metric"], "monthly_net_return")
        self.assertEqual(challenge["target"], "0.10")
        self.assertEqual(challenge["role"], "reporting_only")
        for field_name in (
            "is_guarantee",
            "is_model_loss",
            "is_admission_gate",
            "is_parameter_optimization_target",
        ):
            self.assertIs(challenge[field_name], False)
            with self.subTest(field=field_name):
                self.assert_drift_rejected(("challenge", field_name), True)
        self.assert_drift_rejected(("challenge", "target"), "0.11")
        self.assert_drift_rejected(("challenge", "role"), "admission_gate")

    def test_exposure_portfolio_and_long_only_boundaries_are_frozen(self) -> None:
        expected_states = {
            "RISK_OFF": "0.00",
            "DEFENSIVE": "0.30",
            "NEUTRAL": "0.60",
            "RISK_ON": "1.00",
        }
        portfolio = self.payload["portfolio"]
        self.assertEqual(portfolio["exposure_states"], expected_states)
        self.assertEqual(portfolio["max_positions"], 3)
        self.assertEqual(portfolio["max_position_weight"], "0.40")
        self.assertEqual(portfolio["minimum_cash_weight"], "0.00")
        self.assertEqual(portfolio["maximum_total_weight"], "1.00")
        self.assertIs(portfolio["leverage_allowed"], False)
        self.assertIs(portfolio["short_selling_allowed"], False)
        mutations = (
            (("portfolio", "exposure_states", "DEFENSIVE"), "0.40"),
            (("portfolio", "max_positions"), 4),
            (("portfolio", "max_position_weight"), "0.50"),
            (("portfolio", "minimum_cash_weight"), "-0.01"),
            (("portfolio", "maximum_total_weight"), "1.01"),
            (("portfolio", "leverage_allowed"), True),
            (("portfolio", "short_selling_allowed"), True),
        )
        for path, value in mutations:
            with self.subTest(path=path):
                self.assert_drift_rejected(path, value)

    def test_drawdown_exit_timing_retry_and_oos_intervals_are_frozen(self) -> None:
        risk = self.payload["risk"]
        self.assertEqual(risk["account_drawdown_trigger"], "0.12")
        self.assertEqual(risk["trigger_observation"], "controlled_session_close")
        self.assertEqual(risk["trigger_intent_type"], "ACCOUNT_DRAWDOWN_EXIT")
        self.assertEqual(risk["first_exit_attempt"], "next_controlled_session_open")
        self.assertEqual(
            risk["residual_exit_retry"], "each_following_controlled_session"
        )
        experiment = self.payload["experiment_policy"]
        self.assertEqual(experiment["train"], ["2018-01-01", "2022-12-31"])
        self.assertEqual(experiment["validation"], ["2023-01-01", "2023-12-31"])
        self.assertEqual(
            experiment["locked_test"], ["2024-01-01", "2025-12-31"]
        )
        self.assertEqual(experiment["pre_freeze_2026_status"], "retrospective_consumed")
        self.assertEqual(
            experiment["forward_start_rule"],
            "next_controlled_trading_session_after_spec_freeze",
        )
        mutations = (
            (("risk", "account_drawdown_trigger"), "0.15"),
            (("risk", "trigger_observation"), "next_alpha_decision"),
            (("risk", "first_exit_attempt"), "next_alpha_decision"),
            (("risk", "residual_exit_retry"), "never"),
            (("experiment_policy", "train", 1), "2023-01-31"),
            (("experiment_policy", "validation", 0), "2022-01-01"),
            (("experiment_policy", "locked_test", 0), "2025-01-01"),
            (("experiment_policy", "pre_freeze_2026_status"), "fresh_unconsumed"),
            (("experiment_policy", "forward_start_rule"), "backfill_allowed"),
        )
        for path, value in mutations:
            with self.subTest(path=path):
                self.assert_drift_rejected(path, value)

    def test_all_admission_flags_and_live_remain_closed(self) -> None:
        safety = self.payload["safety"]
        for field_name in (
            "paper_eligibility",
            "trade_eligibility",
            "real_money_list_allowed",
            "automatic_order_submission",
        ):
            self.assertIs(safety[field_name], False)
            with self.subTest(field=field_name):
                self.assert_drift_rejected(("safety", field_name), True)
        self.assertEqual(safety["live"], "not_supported")
        self.assert_drift_rejected(("safety", "live"), "enabled")

    def test_loader_rejects_duplicate_keys_and_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            duplicate = Path(directory) / "duplicate.json"
            duplicate.write_text(
                '{"schema_version":"one","schema_version":"two"}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(AdaptiveExposurePolicyError, "duplicate JSON key"):
                load_adaptive_exposure_policy(duplicate)

            invalid = Path(directory) / "invalid.json"
            invalid.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(AdaptiveExposurePolicyError, "cannot read"):
                load_adaptive_exposure_policy(invalid)

    def test_default_path_names_the_versioned_repository_config(self) -> None:
        self.assertEqual(
            DEFAULT_ADAPTIVE_EXPOSURE_POLICY_PATH.as_posix(),
            "configs/strategy_adaptive_exposure.v2.json",
        )
        self.assertTrue(DEFAULT_ADAPTIVE_EXPOSURE_POLICY_PATH.is_file())


if __name__ == "__main__":
    unittest.main()
