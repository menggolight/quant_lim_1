from __future__ import annotations

from copy import deepcopy
import unittest

from research.strategy_workspace.policy import (
    DEFAULT_POLICY_PATH,
    QualityGrowthPolicyError,
    load_quality_growth_policy,
    validate_quality_growth_policy,
)


class StrategyWorkspacePolicyTests(unittest.TestCase):
    def test_repository_policy_loads_with_frozen_hash(self) -> None:
        policy = load_quality_growth_policy(DEFAULT_POLICY_PATH)
        self.assertEqual(policy.research_status, "blocked_missing_pit_data")
        self.assertEqual(len(policy.factor_ids), 6)
        self.assertRegex(policy.policy_sha256, r"^[0-9a-f]{64}$")

    def test_policy_rejects_looser_position_or_live_settings(self) -> None:
        original = load_quality_growth_policy(DEFAULT_POLICY_PATH).to_dict()
        for path, value in (
            (("portfolio", "max_positions"), 3),
            (("portfolio", "combined_account_industry_cap"), "0.60"),
            (("safety", "live"), "enabled"),
            (("paper", "minimum_calendar_months"), 1),
        ):
            with self.subTest(path=path):
                changed = deepcopy(original)
                changed[path[0]][path[1]] = value
                with self.assertRaises(QualityGrowthPolicyError):
                    validate_quality_growth_policy(changed)

    def test_total_return_benchmark_cannot_be_guessed_in_policy(self) -> None:
        changed = load_quality_growth_policy(DEFAULT_POLICY_PATH).to_dict()
        changed["data_policy"]["total_return_benchmark_id"] = "000906-TR-GUESSED"
        with self.assertRaisesRegex(QualityGrowthPolicyError, "Choice receipt"):
            validate_quality_growth_policy(changed)

    def test_factor_execution_and_historical_gates_cannot_drift(self) -> None:
        original = load_quality_growth_policy(DEFAULT_POLICY_PATH).to_dict()
        mutations = (
            (("factors", 0, "formula"), "best_formula_after_test"),
            (("preprocessing", "residualize_controls"), ["industry_only"]),
            (("eligibility", "minimum_listing_sessions"), 1),
            (("historical_gates", "minimum_corrected_independent_factors"), 1),
        )
        for path, value in mutations:
            with self.subTest(path=path):
                changed = deepcopy(original)
                cursor = changed
                for key in path[:-1]:
                    cursor = cursor[key]
                cursor[path[-1]] = value
                with self.assertRaises(QualityGrowthPolicyError):
                    validate_quality_growth_policy(changed)


if __name__ == "__main__":
    unittest.main()
