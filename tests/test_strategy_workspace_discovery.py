from __future__ import annotations

import inspect
import math
import unittest
from dataclasses import FrozenInstanceError

from research.strategy_workspace.catalog import (
    DEFAULT_CATALOG_SHA256,
    DEFAULT_FACTOR_CATALOG,
    factors_for_mechanism,
    get_factor,
)
from research.strategy_workspace.contracts import (
    ALLOWED_LOOKBACK_DAYS,
    DiscoveryContractError,
    DiscoveryPlan,
    DiscoveryStatus,
    ExpectedSign,
    FactorDefinition,
    ThesisSpec,
    canonical_json_bytes,
    canonical_sha256,
)
from research.strategy_workspace.discovery import freeze_plan, generate_candidates


class StrategyWorkspaceDiscoveryTests(unittest.TestCase):
    @staticmethod
    def _thesis(
        mechanisms: tuple[str, ...] = ("price.momentum",),
        *,
        horizon_days: int = 20,
    ) -> ThesisSpec:
        return ThesisSpec(
            thesis_id="export-demand-v1",
            viewpoint="External demand may persist into the next holding period.",
            mechanisms=mechanisms,
            horizon_days=horizon_days,
        )

    @staticmethod
    def _factor(
        factor_id: str,
        mechanism_path: str = "price.momentum",
        *,
        lookback_days: int = 20,
    ) -> FactorDefinition:
        return FactorDefinition(
            factor_id=factor_id,
            name=f"Factor {factor_id}",
            mechanism_path=mechanism_path,
            lookback_days=lookback_days,
            required_fields=("close",),
            expected_sign=ExpectedSign.POSITIVE,
            formula=f"close / lag(close,{lookback_days}) - 1",
        )

    def test_default_catalog_is_larger_than_one_plan_but_low_freedom(self) -> None:
        self.assertEqual(
            [item.factor_id for item in DEFAULT_FACTOR_CATALOG],
            [
                "RM20",
                "RM60",
                "RM120",
                "REV20",
                "TREND_EFF60",
                "DOWNSIDE_VOL60",
                "BREAKOUT60",
            ],
        )
        self.assertEqual(len(DEFAULT_FACTOR_CATALOG), 7)
        self.assertEqual(
            {item.lookback_days for item in DEFAULT_FACTOR_CATALOG},
            ALLOWED_LOOKBACK_DAYS,
        )
        self.assertTrue(all(item.required_fields for item in DEFAULT_FACTOR_CATALOG))
        self.assertEqual(
            DEFAULT_CATALOG_SHA256,
            canonical_sha256([item.to_dict() for item in DEFAULT_FACTOR_CATALOG]),
        )
        self.assertEqual(get_factor("rm20").factor_id, "RM20")
        self.assertEqual(
            {item.factor_id for item in factors_for_mechanism("price.trend")},
            {"TREND_EFF60", "BREAKOUT60"},
        )

    def test_thesis_allows_one_horizon_and_at_most_two_mechanisms(self) -> None:
        thesis = self._thesis(("PRICE.MOMENTUM", "price.trend"), horizon_days=60)
        self.assertEqual(thesis.mechanisms, ("price.momentum", "price.trend"))
        self.assertEqual(thesis.horizon_days, 60)
        self.assertEqual(thesis.mechanism_paths, thesis.mechanisms)
        with self.assertRaisesRegex(DiscoveryContractError, "1-2 mechanisms"):
            self._thesis(("price.momentum", "price.trend", "risk.downside"))
        with self.assertRaisesRegex(DiscoveryContractError, "one positive integer"):
            self._thesis(horizon_days=(20, 60))  # type: ignore[arg-type]
        with self.assertRaisesRegex(DiscoveryContractError, "unique"):
            self._thesis(("price.momentum", "price.momentum"))

    def test_factor_definition_rejects_unapproved_lookback(self) -> None:
        with self.assertRaisesRegex(DiscoveryContractError, "20, 60, or 120"):
            self._factor("BAD10", lookback_days=10)
        with self.assertRaisesRegex(DiscoveryContractError, "positive or negative"):
            FactorDefinition(
                factor_id="BAD_SIGN",
                name="Bad sign",
                mechanism_path="price.momentum",
                lookback_days=20,
                required_fields=("close",),
                expected_sign="unknown",  # type: ignore[arg-type]
                formula="close / lag(close,20) - 1",
            )

    def test_candidate_generation_is_bounded_and_deterministic(self) -> None:
        thesis = self._thesis(("price.momentum", "price.trend"))
        first = generate_candidates(thesis)
        second = generate_candidates(thesis, tuple(reversed(DEFAULT_FACTOR_CATALOG)))
        self.assertEqual(first.status, DiscoveryStatus.CANDIDATES_GENERATED)
        self.assertEqual(first.factor_ids, second.factor_ids)
        self.assertEqual(first.plan_sha256, second.plan_sha256)
        self.assertEqual(len(first.factors), 5)
        self.assertLessEqual(len(first.factors), 6)
        self.assertEqual(first.horizon_days, thesis.horizon_days)
        for mechanism_path in thesis.mechanisms:
            self.assertLessEqual(
                sum(
                    factor.mechanism_path == mechanism_path
                    for factor in first.factors
                ),
                3,
            )

    def test_missing_or_oversized_mechanism_blocks_instead_of_guessing(self) -> None:
        missing = generate_candidates(self._thesis(("fundamental.quality",)))
        self.assertEqual(missing.status, DiscoveryStatus.BLOCKED)
        self.assertFalse(missing.is_frozen)
        self.assertEqual(missing.factors, ())
        self.assertIn("catalog_missing_mechanism", missing.blocked_reasons[0])
        oversized_catalog = tuple(
            self._factor(f"X{index}") for index in range(1, 5)
        )
        oversized = generate_candidates(self._thesis(), oversized_catalog)
        self.assertEqual(oversized.status, DiscoveryStatus.BLOCKED)
        self.assertEqual(oversized.factors, ())
        self.assertIn("mechanism_factor_cap_exceeded", oversized.blocked_reasons[0])
        with self.assertRaisesRegex(DiscoveryContractError, "cannot be frozen"):
            freeze_plan(oversized)

    def test_plan_contract_independently_enforces_factor_caps(self) -> None:
        thesis = self._thesis(("price.momentum", "price.trend"))
        seven = tuple(
            self._factor(
                f"F{index}",
                "price.momentum" if index <= 3 else "price.trend",
            )
            for index in range(1, 8)
        )
        with self.assertRaisesRegex(DiscoveryContractError, "at most 6"):
            DiscoveryPlan(thesis=thesis, factors=seven)
        four_same_mechanism = tuple(
            self._factor(f"M{index}") for index in range(1, 5)
        )
        with self.assertRaisesRegex(DiscoveryContractError, "3-factor cap"):
            DiscoveryPlan(
                thesis=self._thesis(),
                factors=four_same_mechanism,
            )

    def test_freeze_is_pre_outcome_state_transition_and_hash_is_canonical(self) -> None:
        generated = generate_candidates(self._thesis())
        with self.assertRaisesRegex(DiscoveryContractError, "not frozen"):
            generated.require_frozen()
        frozen = freeze_plan(generated)
        self.assertEqual(frozen.status, DiscoveryStatus.FROZEN)
        self.assertTrue(frozen.is_frozen)
        self.assertIs(frozen.require_frozen(), frozen)
        self.assertNotEqual(generated.plan_sha256, frozen.plan_sha256)
        self.assertEqual(
            frozen.plan_sha256,
            canonical_sha256(frozen.to_content_dict()),
        )
        serialized = frozen.to_dict()
        self.assertEqual(serialized["plan_sha256"], frozen.plan_sha256)
        self.assertEqual(serialized["horizon_days"], 20)
        self.assertNotIn("label", canonical_json_bytes(serialized).decode("utf-8"))
        self.assertNotIn("backtest", canonical_json_bytes(serialized).decode("utf-8"))
        with self.assertRaises(FrozenInstanceError):
            frozen.status = DiscoveryStatus.BLOCKED  # type: ignore[misc]

    def test_generation_and_freezing_apis_do_not_accept_results(self) -> None:
        forbidden_tokens = ("label", "return", "backtest", "score", "metric", "result")
        for function in (generate_candidates, freeze_plan):
            parameter_names = tuple(inspect.signature(function).parameters)
            self.assertFalse(
                any(
                    token in parameter_name.lower()
                    for parameter_name in parameter_names
                    for token in forbidden_tokens
                )
            )
        generated = generate_candidates(self._thesis())
        with self.assertRaises(TypeError):
            generate_candidates(self._thesis(), labels=[1, 0])  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            freeze_plan(generated, backtest_results={"RM20": 1.0})  # type: ignore[call-arg]

    def test_malformed_catalog_and_unknown_status_fail_closed(self) -> None:
        duplicate = self._factor("DUP")
        with self.assertRaisesRegex(DiscoveryContractError, "unique"):
            generate_candidates(self._thesis(), (duplicate, duplicate))
        with self.assertRaisesRegex(DiscoveryContractError, "unknown discovery status"):
            DiscoveryPlan(
                thesis=self._thesis(),
                factors=(self._factor("ONE"),),
                status="ready",  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(DiscoveryContractError, "blocked_reasons"):
            DiscoveryPlan(
                thesis=self._thesis(),
                factors=(),
                status=DiscoveryStatus.BLOCKED,
            )

    def test_canonical_json_rejects_unordered_and_non_finite_values(self) -> None:
        self.assertEqual(
            canonical_json_bytes({"b": 2, "a": 1}),
            b'{"a":1,"b":2}',
        )
        with self.assertRaisesRegex(TypeError, "unordered"):
            canonical_json_bytes({"values": {1, 2}})
        with self.assertRaisesRegex(DiscoveryContractError, "finite"):
            canonical_json_bytes({"value": math.nan})


if __name__ == "__main__":
    unittest.main()
