from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from research.market_data.validation import validate_json_schema


ROOT = Path(__file__).resolve().parents[1]
FORMAL_CONFIG = ROOT / "configs" / "a_share_technical_momentum_adaptive.v1.json"
SHADOW_CONFIG = ROOT / "configs" / "a_share_technical_shadow_mvp.v1.json"
SCHEMA = ROOT / "schemas" / "technical_momentum_experiment.v1.json"


class TechnicalMomentumExperimentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.formal = json.loads(FORMAL_CONFIG.read_text(encoding="utf-8"))
        cls.shadow = json.loads(SHADOW_CONFIG.read_text(encoding="utf-8"))

    def test_experiment_schema_and_locked_boundary_are_frozen(self) -> None:
        validate_json_schema(self.formal, SCHEMA)
        self.assertEqual(
            self.formal["strategy_id"],
            "a-share-technical-momentum-adaptive-v1",
        )
        self.assertEqual(self.formal["locked_test_status"], "NOT_RUN")
        self.assertIs(self.formal["locked_test_consumed"], False)
        self.assertIs(
            self.formal["splits"]["locked_test"]["allowed_to_run"], False
        )
        self.assertEqual(
            self.formal["splits"]["development"],
            {"start": "2018-01-01", "end": "2022-12-31", "allowed_to_run": True},
        )
        self.assertEqual(
            self.formal["splits"]["validation"],
            {"start": "2023-01-01", "end": "2023-12-31", "allowed_to_run": True},
        )

    def test_alpha_and_exposure_parameters_equal_shadow_truth(self) -> None:
        alpha = self.formal["alpha"]
        shadow_alpha = self.shadow["alpha"]
        self.assertEqual(alpha["factor_ids"], shadow_alpha["factor_ids"])
        self.assertEqual(float(alpha["winsor_lower_quantile"]), shadow_alpha["winsor_lower_quantile"])
        self.assertEqual(float(alpha["winsor_upper_quantile"]), shadow_alpha["winsor_upper_quantile"])
        self.assertEqual(alpha["zscore_ddof"], shadow_alpha["zscore_ddof"])
        self.assertEqual(alpha["directions"], shadow_alpha["directions"])
        self.assertEqual(float(alpha["entry_score_min_exclusive"]), shadow_alpha["entry_score_min_exclusive"])
        self.assertEqual(float(alpha["entry_percentile_min"]), shadow_alpha["entry_percentile_min"])
        self.assertEqual(float(alpha["hold_score_min_exclusive"]), shadow_alpha["hold_score_min_exclusive"])
        self.assertEqual(float(alpha["hold_percentile_min"]), shadow_alpha["hold_percentile_min"])

        exposure = self.formal["exposure"]
        shadow_exposure = self.shadow["exposure"]
        for field in (
            "benchmark_trend_sessions",
            "breadth_trend_sessions",
            "realized_vol_sessions",
            "annualization_sessions",
            "failure_state",
        ):
            self.assertEqual(exposure[field], shadow_exposure[field])
        for group in ("risk_off", "defensive", "risk_on", "gross_exposure"):
            self.assertEqual(
                {key: float(value) for key, value in exposure[group].items()},
                {key: float(value) for key, value in shadow_exposure[group].items()},
            )

    def test_portfolio_and_costs_preserve_existing_parameters(self) -> None:
        formal_portfolio = self.formal["portfolio"]
        shadow_portfolio = self.shadow["portfolio"]
        for field in (
            "max_positions",
            "lot_size",
            "leverage_allowed",
            "short_selling_allowed",
            "candidate_shortage_policy",
        ):
            self.assertEqual(formal_portfolio[field], shadow_portfolio[field])
        self.assertEqual(float(formal_portfolio["initial_cash"]), shadow_portfolio["initial_cash"])
        self.assertEqual(
            float(formal_portfolio["max_position_weight"]),
            shadow_portfolio["max_position_weight"],
        )
        for field in (
            "commission_rate",
            "minimum_commission",
            "sell_tax_rate",
            "transfer_fee_rate_both_sides",
        ):
            self.assertEqual(
                float(self.formal["costs"]["base"][field]),
                shadow_portfolio.get(field, self.shadow["costs"][field]),
            )
        self.assertEqual(
            float(self.formal["costs"]["base"]["slippage_bps_one_way"]),
            self.shadow["costs"]["slippage_bps_one_way"],
        )

    def test_source_hashes_bind_unmodified_shadow_implementation(self) -> None:
        frozen = self.formal["frozen_implementation"]
        self.assertEqual(
            frozen,
            {
                "alpha_policy_path": "configs/a_share_technical_shadow_mvp.v1.json",
                "alpha_policy_sha256": "53b7f2b3da72a2d393c18b4fc61afac9e1a3f63c2cd86756cbd1bd0d47eb77ea",
                "alpha_source_path": "research/strategy_workspace/technical_alpha_shadow_v1.py",
                "alpha_source_sha256": "3cd734c8770e5647754fa21d65e8d6a789da3c17958fb2b2b15352268af3d922",
                "exposure_source_path": "research/strategy_workspace/technical_exposure_shadow_v1.py",
                "exposure_source_sha256": "4a204237752dec4797c2f80cf5950d638aa4d638f2ece615a29ace62f14d0ca7",
            },
        )
        for path_field, hash_field in (
            ("alpha_policy_path", "alpha_policy_sha256"),
            ("alpha_source_path", "alpha_source_sha256"),
            ("exposure_source_path", "exposure_source_sha256"),
        ):
            content = (ROOT / frozen[path_field]).read_bytes()
            self.assertEqual(hashlib.sha256(content).hexdigest(), frozen[hash_field])

    def test_dataset_scope_excludes_forbidden_research_inputs_and_vip(self) -> None:
        allowed = {
            endpoint
            for endpoints in self.formal["data"]["allowed_sources"].values()
            for endpoint in endpoints
        }
        forbidden = set(self.formal["data"]["forbidden_sources"])
        self.assertFalse(allowed & forbidden)
        self.assertNotIn("income_vip", allowed)
        self.assertNotIn("balancesheet_vip", allowed)
        self.assertNotIn("cashflow_vip", allowed)
        self.assertEqual(len(self.formal["data"]["required_datasets"]), 9)

    def test_all_safety_authorities_remain_closed(self) -> None:
        self.assertEqual(
            self.formal["safety"],
            {
                "paper_eligibility": False,
                "trade_eligibility": False,
                "real_money_list_allowed": False,
                "automatic_order_submission": False,
                "live_supported": False,
            },
        )


if __name__ == "__main__":
    unittest.main()
