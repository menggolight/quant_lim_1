from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
import unittest

from research.factor_discovery.governance import (
    ApprovedFactorRegistryV1,
    ApprovedFactorV1,
    FactorGovernanceError,
    FactorHypothesisV2,
    FactorValidationReceiptV1,
    canonical_json_bytes,
    canonical_sha256,
)
from research.market_data.validation import (
    SchemaValidationError,
    validate_json_schema,
)


UTC = timezone.utc
SCHEMA_ROOT = Path(__file__).resolve().parents[1] / "schemas"
EXPERIMENT_SPEC_SHA256 = "e" * 64


def _at(hour: int) -> datetime:
    return datetime(2026, 8, 21, hour, 0, tzinfo=UTC)


def _hash(character: str) -> str:
    return character * 64


def hypothesis(
    *,
    factor_id: str = "QUALITY_CASH_CONVERSION",
    hypothesis_id: str = "hypothesis.quality-cash-conversion.v2",
    formula: str = "operating_cash_flow / operating_profit",
) -> FactorHypothesisV2:
    return FactorHypothesisV2(
        hypothesis_id=hypothesis_id,
        factor_id=factor_id,
        created_at=_at(9),
        information_cutoff_at=_at(8),
        formula=formula,
        input_fields=("operating_profit", "operating_cash_flow"),
        input_schema_sha256=_hash("1"),
        prediction_target="D+1 open to D+21 open excess return",
        horizon_trading_days=20,
        expected_sign="positive",
        universe_policy="controlled PIT CSI800 members",
        benchmark_policy="CSI800 total return same interval",
        economic_rationale="cash-backed earnings should be more persistent",
        falsification_conditions=(
            "validation rank IC is non-positive",
            "net spread does not exceed registered cost",
        ),
    )


def receipt(
    candidate: FactorHypothesisV2 | None = None,
    *,
    suffix: str = "a",
) -> FactorValidationReceiptV1:
    candidate = candidate or hypothesis()
    return FactorValidationReceiptV1.from_hypothesis(
        candidate,
        receipt_id=f"receipt.{candidate.factor_id.lower()}.{suffix}",
        validator_id="controlled-factor-validator.v1",
        experiment_spec_sha256=EXPERIMENT_SPEC_SHA256,
        implementation_code_sha256=_hash("2" if suffix == "a" else "3"),
        validation_spec_sha256=_hash("4"),
        validation_dataset_sha256=_hash("5" if suffix == "a" else "6"),
        validation_code_sha256=_hash("7"),
        validation_started_at=_at(10),
        validation_completed_at=_at(11),
    )


def approved(
    candidate: FactorHypothesisV2 | None = None,
    *,
    suffix: str = "a",
) -> ApprovedFactorV1:
    return ApprovedFactorV1.from_validation_receipt(
        receipt(candidate, suffix=suffix), approved_at=_at(12)
    )


def registry(*factors: ApprovedFactorV1) -> ApprovedFactorRegistryV1:
    return ApprovedFactorRegistryV1(
        registry_id="approved-factor-registry.20260821.v1",
        frozen_at=_at(13),
        experiment_spec_sha256=EXPERIMENT_SPEC_SHA256,
        prediction_target=(factors or (approved(),))[0].prediction_target,
        horizon_trading_days=(factors or (approved(),))[0].horizon_trading_days,
        universe_policy=(factors or (approved(),))[0].universe_policy,
        benchmark_policy=(factors or (approved(),))[0].benchmark_policy,
        factors=tuple(factors or (approved(),)),
    )


class FactorDiscoveryGovernanceTests(unittest.TestCase):
    def assert_schema_rejects(self, payload: object, schema_name: str) -> None:
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(payload, SCHEMA_ROOT / schema_name)

    def test_hypothesis_is_candidate_only_and_self_hashed(self) -> None:
        candidate = hypothesis()
        self.assertEqual(candidate.status, "llm_research_candidate_only")
        self.assertEqual(
            candidate.formula_sha256,
            canonical_sha256({"formula": candidate.formula}),
        )
        self.assertEqual(
            candidate.hypothesis_sha256,
            canonical_sha256(candidate.to_content_dict()),
        )
        self.assertIs(candidate.require_candidate(as_of=_at(9)), candidate)
        self.assertFalse(candidate.paper_eligibility)
        self.assertFalse(candidate.trade_eligibility)
        self.assertFalse(candidate.real_money_list_allowed)
        self.assertEqual(candidate.live_execution_status, "live_not_supported")

    def test_hypothesis_normalizes_semantic_arrays_deterministically(self) -> None:
        first = hypothesis()
        second = FactorHypothesisV2(
            hypothesis_id=first.hypothesis_id,
            factor_id=first.factor_id,
            created_at=first.created_at.astimezone(timezone(timedelta(hours=8))),
            information_cutoff_at=first.information_cutoff_at.astimezone(
                timezone(timedelta(hours=8))
            ),
            formula=first.formula,
            input_fields=tuple(reversed(first.input_fields)),
            input_schema_sha256=first.input_schema_sha256,
            prediction_target=first.prediction_target,
            horizon_trading_days=first.horizon_trading_days,
            expected_sign=first.expected_sign,
            universe_policy=first.universe_policy,
            benchmark_policy=first.benchmark_policy,
            economic_rationale=first.economic_rationale,
            falsification_conditions=tuple(
                reversed(first.falsification_conditions)
            ),
        )
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.hypothesis_sha256, second.hypothesis_sha256)

    def test_candidate_cannot_claim_approval_or_enable_trading(self) -> None:
        payload = hypothesis().to_dict()
        payload["status"] = "approved_for_frozen_research_only"
        with self.assertRaisesRegex(FactorGovernanceError, "candidate_only"):
            FactorHypothesisV2.from_dict(payload, as_of=_at(9))

        payload = hypothesis().to_dict()
        payload["trade_eligibility"] = True
        self.assert_schema_rejects(payload, "factor_hypothesis.v2.json")

    def test_candidate_future_and_contradictory_times_fail_closed(self) -> None:
        payload = hypothesis().to_dict()
        with self.assertRaisesRegex(FactorGovernanceError, "future"):
            FactorHypothesisV2.from_dict(payload, as_of=_at(8))
        with self.assertRaisesRegex(FactorGovernanceError, "cutoff"):
            FactorHypothesisV2(
                hypothesis_id="hypothesis.future.v2",
                factor_id="FUTURE_FACTOR",
                created_at=_at(8),
                information_cutoff_at=_at(9),
                formula="future_value / current_value",
                input_fields=("future_value", "current_value"),
                input_schema_sha256=_hash("1"),
                prediction_target="future excess return",
                horizon_trading_days=20,
                expected_sign="positive",
                universe_policy="controlled PIT universe",
                benchmark_policy="same interval benchmark",
                economic_rationale="adversarial fixture",
                falsification_conditions=("non-positive rank IC",),
            )

    def test_candidate_tampering_and_approval_field_injection_are_rejected(self) -> None:
        payload = hypothesis().to_dict()
        payload["formula"] = "future_return"
        with self.assertRaisesRegex(FactorGovernanceError, "formula SHA"):
            FactorHypothesisV2.from_dict(payload, as_of=_at(9))

        injected = hypothesis().to_dict()
        injected["validation_receipt"] = {"validation_passed": True}
        with self.assertRaisesRegex(FactorGovernanceError, "unknown fields"):
            FactorHypothesisV2.from_dict(injected, as_of=_at(9))
        self.assert_schema_rejects(injected, "factor_hypothesis.v2.json")

    def test_validation_receipt_binds_all_required_hashes(self) -> None:
        candidate = hypothesis()
        validation = receipt(candidate)
        self.assertEqual(validation.factor_id, candidate.factor_id)
        self.assertEqual(validation.hypothesis_sha256, candidate.hypothesis_sha256)
        self.assertEqual(validation.formula_sha256, candidate.formula_sha256)
        self.assertEqual(
            validation.input_schema_sha256, candidate.input_schema_sha256
        )
        self.assertEqual(validation.validation_partition, "validation_only_not_locked_test")
        self.assertEqual(
            validation.receipt_sha256,
            canonical_sha256(validation.to_content_dict()),
        )
        self.assertIs(validation.require_valid(as_of=_at(11)), validation)

    def test_validation_receipt_rejects_future_locked_and_bad_sequence(self) -> None:
        payload = receipt().to_dict()
        with self.assertRaisesRegex(FactorGovernanceError, "future"):
            FactorValidationReceiptV1.from_dict(payload, as_of=_at(10))

        locked = deepcopy(payload)
        locked["validation_partition"] = "locked_test"
        with self.assertRaisesRegex(FactorGovernanceError, "Locked Test"):
            FactorValidationReceiptV1.from_dict(locked, as_of=_at(11))
        self.assert_schema_rejects(locked, "factor_validation_receipt.v1.json")

        contradictory = deepcopy(payload)
        contradictory["validation_started_at"] = _at(8).isoformat()
        with self.assertRaisesRegex(FactorGovernanceError, "self-contradictory"):
            FactorValidationReceiptV1.from_dict(contradictory, as_of=_at(11))

    def test_candidate_cannot_be_directly_upgraded(self) -> None:
        candidate = hypothesis()
        with self.assertRaisesRegex(FactorGovernanceError, "direct upgrade"):
            ApprovedFactorV1.from_validation_receipt(  # type: ignore[arg-type]
                candidate, approved_at=_at(12)
            )
        with self.assertRaisesRegex(FactorGovernanceError, "directly upgraded"):
            ApprovedFactorRegistryV1(
                registry_id="forged-registry.v1",
                frozen_at=_at(13),
                experiment_spec_sha256=EXPERIMENT_SPEC_SHA256,
                prediction_target=candidate.prediction_target,
                horizon_trading_days=candidate.horizon_trading_days,
                universe_policy=candidate.universe_policy,
                benchmark_policy=candidate.benchmark_policy,
                factors=(candidate,),  # type: ignore[arg-type]
            )

    def test_approved_factor_rejects_receipt_binding_contradictions(self) -> None:
        validation = receipt()
        with self.assertRaisesRegex(FactorGovernanceError, "formula_sha256"):
            ApprovedFactorV1(
                factor_id=validation.factor_id,
                hypothesis_id=validation.hypothesis_id,
                hypothesis_sha256=validation.hypothesis_sha256,
                experiment_spec_sha256=validation.experiment_spec_sha256,
                prediction_target=validation.prediction_target,
                horizon_trading_days=validation.horizon_trading_days,
                universe_policy=validation.universe_policy,
                benchmark_policy=validation.benchmark_policy,
                formula_sha256=_hash("9"),
                implementation_code_sha256=validation.implementation_code_sha256,
                input_schema_sha256=validation.input_schema_sha256,
                validation_receipt=validation,
                approved_at=_at(12),
            )
        with self.assertRaisesRegex(FactorGovernanceError, "future|precede"):
            ApprovedFactorV1.from_validation_receipt(
                validation, approved_at=_at(10)
            )

    def test_registry_is_sorted_self_hashed_and_lookup_is_exact(self) -> None:
        first = approved()
        candidate_two = hypothesis(
            factor_id="GROWTH_PERSISTENCE",
            hypothesis_id="hypothesis.growth-persistence.v2",
            formula="revenue_ttm / revenue_ttm_lag4 - 1",
        )
        second = approved(candidate_two, suffix="b")
        left = registry(first, second)
        right = registry(second, first)
        self.assertEqual(left.approved_factor_ids, tuple(sorted((first.factor_id, second.factor_id))))
        self.assertEqual(left.to_dict(), right.to_dict())
        self.assertEqual(left.registry_sha256, right.registry_sha256)
        self.assertEqual(
            left.registry_sha256, canonical_sha256(left.to_content_dict())
        )
        self.assertIs(left.get(first.factor_id), first)
        with self.assertRaisesRegex(FactorGovernanceError, "not approved"):
            left.get("UNKNOWN_FACTOR")

    def test_registry_rejects_duplicate_factor_and_formula_payloads(self) -> None:
        first = approved()
        with self.assertRaisesRegex(FactorGovernanceError, "duplicate factor_id"):
            registry(first, first)

        alias_candidate = hypothesis(
            factor_id="CASH_CONVERSION_ALIAS",
            hypothesis_id="hypothesis.cash-conversion-alias.v2",
            formula=hypothesis().formula,
        )
        alias = approved(alias_candidate, suffix="b")
        with self.assertRaisesRegex(FactorGovernanceError, "duplicate formula"):
            registry(first, alias)

    def test_registry_rejects_mixed_experiment_target_horizon_and_universe(self) -> None:
        first = approved()
        incompatible_candidate = FactorHypothesisV2(
            hypothesis_id="hypothesis.incompatible.v2",
            factor_id="INCOMPATIBLE_FACTOR",
            created_at=_at(9),
            information_cutoff_at=_at(8),
            formula="incompatible_value / lagged_value",
            input_fields=("incompatible_value", "lagged_value"),
            input_schema_sha256=_hash("8"),
            prediction_target="different target",
            horizon_trading_days=5,
            expected_sign="positive",
            universe_policy="different universe",
            benchmark_policy="different benchmark",
            economic_rationale="adversarial semantic mismatch",
            falsification_conditions=("reject",),
        )
        incompatible = approved(incompatible_candidate, suffix="b")
        with self.assertRaisesRegex(FactorGovernanceError, "policy binding mismatch"):
            registry(first, incompatible)

    def test_registry_rejects_replayed_receipt_even_if_outer_payload_is_resigned(self) -> None:
        first = approved()
        candidate_two = hypothesis(
            factor_id="GROWTH_PERSISTENCE",
            hypothesis_id="hypothesis.growth-persistence.v2",
            formula="revenue_ttm / revenue_ttm_lag4 - 1",
        )
        second = approved(candidate_two, suffix="b")
        object.__setattr__(
            second.validation_receipt,
            "receipt_sha256",
            first.validation_receipt.receipt_sha256,
        )
        with self.assertRaisesRegex(FactorGovernanceError, "receipt cannot be replayed"):
            registry(first, second)

    def test_registry_rejects_future_and_preapproval_freeze(self) -> None:
        current = registry()
        with self.assertRaisesRegex(FactorGovernanceError, "future"):
            current.require_valid(as_of=_at(12))
        with self.assertRaisesRegex(FactorGovernanceError, "future"):
            ApprovedFactorRegistryV1(
                registry_id="too-early-registry.v1",
                frozen_at=_at(11),
                experiment_spec_sha256=EXPERIMENT_SPEC_SHA256,
                prediction_target=approved().prediction_target,
                horizon_trading_days=approved().horizon_trading_days,
                universe_policy=approved().universe_policy,
                benchmark_policy=approved().benchmark_policy,
                factors=(approved(),),
            )

    def test_registry_parser_recomputes_every_nested_hash(self) -> None:
        original = registry()
        restored = ApprovedFactorRegistryV1.from_dict(
            original.to_dict(), as_of=_at(13)
        )
        self.assertEqual(restored.to_dict(), original.to_dict())

        tampered = deepcopy(original.to_dict())
        tampered["factors"][0]["implementation_code_sha256"] = _hash("9")
        tampered["registry_sha256"] = canonical_sha256(
            {key: value for key, value in tampered.items() if key != "registry_sha256"}
        )
        with self.assertRaisesRegex(FactorGovernanceError, "contradicts"):
            ApprovedFactorRegistryV1.from_dict(tampered, as_of=_at(13))

    def test_resigned_runtime_objects_cannot_bypass_semantic_checks(self) -> None:
        candidate = hypothesis()
        object.__setattr__(candidate, "status", "approved_for_frozen_research_only")
        object.__setattr__(
            candidate,
            "hypothesis_sha256",
            canonical_sha256(candidate.to_content_dict()),
        )
        with self.assertRaisesRegex(FactorGovernanceError, "candidate_only"):
            candidate.require_candidate(as_of=_at(9))

        validation = receipt()
        object.__setattr__(validation, "result", "caller_asserted_pass")
        object.__setattr__(
            validation,
            "receipt_sha256",
            canonical_sha256(validation.to_content_dict()),
        )
        with self.assertRaisesRegex(FactorGovernanceError, "controlled result"):
            validation.require_valid(as_of=_at(11))

        approved_factor = approved()
        approved_registry = registry(approved_factor)
        object.__setattr__(
            approved_registry, "factors", (approved_factor, approved_factor)
        )
        object.__setattr__(
            approved_registry,
            "registry_sha256",
            canonical_sha256(approved_registry.to_content_dict()),
        )
        with self.assertRaisesRegex(FactorGovernanceError, "duplicate factor_id"):
            approved_registry.require_valid(as_of=_at(13))

    def test_schemas_accept_canonical_objects_and_reject_safety_escalation(self) -> None:
        candidate = hypothesis().to_dict()
        validation = receipt().to_dict()
        approved_registry = registry().to_dict()
        validate_json_schema(candidate, SCHEMA_ROOT / "factor_hypothesis.v2.json")
        validate_json_schema(
            validation, SCHEMA_ROOT / "factor_validation_receipt.v1.json"
        )
        validate_json_schema(
            approved_registry, SCHEMA_ROOT / "approved_factor_registry.v1.json"
        )

        forged = deepcopy(approved_registry)
        forged["paper_eligibility"] = True
        self.assert_schema_rejects(forged, "approved_factor_registry.v1.json")
        forged = deepcopy(approved_registry)
        forged["source_authenticated"] = True
        self.assert_schema_rejects(forged, "approved_factor_registry.v1.json")

    def test_canonical_serialization_rejects_unordered_and_nonfinite_values(self) -> None:
        instant = _at(9)
        equivalent = instant.astimezone(timezone(timedelta(hours=8)))
        self.assertEqual(
            canonical_json_bytes({"b": 2, "a": instant}),
            canonical_json_bytes({"a": equivalent, "b": 2}),
        )
        with self.assertRaisesRegex(FactorGovernanceError, "unordered"):
            canonical_json_bytes({"bad": {"x"}})
        with self.assertRaisesRegex(FactorGovernanceError, "finite"):
            canonical_json_bytes({"bad": math.nan})
        with self.assertRaisesRegex(FactorGovernanceError, "timezone"):
            canonical_json_bytes({"bad": datetime(2026, 8, 21, 9, 0)})


if __name__ == "__main__":
    unittest.main()
