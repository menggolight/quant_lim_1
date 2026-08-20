from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
import unittest

from research.strategy_workspace.choice_gate import (
    BenchmarkReturnBasis,
    CapabilityVerification,
    ChoiceCapability,
    ChoiceCapabilityItem,
    ChoiceCapabilityReceipt,
    ChoiceField,
    ChoiceGateContractError,
    ChoiceGateEvaluation,
    ChoiceGateStatus,
    ChoiceProviderId,
    FinancialCurrency,
    FinancialFlowBasis,
    FinancialStatementScope,
    MembershipBackfillPolicy,
    RevisionPolicy,
    SourcePolicy,
    UniverseCompletionPolicy,
    evaluate_choice_quality_growth_gate,
)


ROOT = Path(__file__).resolve().parents[1]
OBSERVED_AT = datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc)
COVERAGE_CUTOFF = date(2026, 8, 18)
STOCK_IDS = tuple(f"{index:06d}.SZ" for index in range(1, 801))


def _item(
    capability: ChoiceCapability,
    *,
    verification: CapabilityVerification = CapabilityVerification.CONTROLLED_EVIDENCE_VERIFIED,
    fields: tuple[ChoiceField, ...] | None = None,
    coverage_start: date = date(2014, 1, 1),
    coverage_end: date = COVERAGE_CUTOFF,
    row_count: int | None = None,
    distinct_subject_count: int | None = None,
    subject_ids: tuple[str, ...] | None = None,
) -> ChoiceCapabilityItem:
    if verification is not CapabilityVerification.CONTROLLED_EVIDENCE_VERIFIED:
        return ChoiceCapabilityItem(
            capability=capability,
            verification=verification,
            provider_id=ChoiceProviderId.CHOICE,
        )
    return ChoiceCapabilityItem(
        capability=capability,
        verification=verification,
        provider_id=ChoiceProviderId.CHOICE,
        dataset_contract=f"choice.{capability.value}.v1",
        subject_ids=(
            subject_ids
            if subject_ids is not None
            else (
                ("H00906.CSI",)
                if capability is ChoiceCapability.TOTAL_RETURN_BENCHMARK
                else (
                    ("XSHG_CALENDAR",)
                    if capability is ChoiceCapability.TRADE_CALENDAR
                    else STOCK_IDS
                )
            )
        ),
        return_basis=(
            BenchmarkReturnBasis.TOTAL_RETURN
            if capability is ChoiceCapability.TOTAL_RETURN_BENCHMARK
            else None
        ),
        financial_flow_basis=(
            FinancialFlowBasis.SINGLE_QUARTER
            if capability is ChoiceCapability.FIRST_DISCLOSURE_FINANCIALS
            else None
        ),
        financial_statement_scope=(
            FinancialStatementScope.CONSOLIDATED
            if capability is ChoiceCapability.FIRST_DISCLOSURE_FINANCIALS
            else None
        ),
        financial_currency=(
            FinancialCurrency.CNY
            if capability is ChoiceCapability.FIRST_DISCLOSURE_FINANCIALS
            else None
        ),
        fields=tuple(ChoiceField) if fields is None else fields,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        row_count=(
            row_count
            if row_count is not None
            else (
                3000
                if capability
                in {
                    ChoiceCapability.TRADE_CALENDAR,
                    ChoiceCapability.TOTAL_RETURN_BENCHMARK,
                }
                else 2_000_000
            )
        ),
        distinct_subject_count=(
            distinct_subject_count
            if distinct_subject_count is not None
            else (
                1
                if capability
                in {
                ChoiceCapability.TRADE_CALENDAR,
                ChoiceCapability.TOTAL_RETURN_BENCHMARK,
                }
                else 800
            )
        ),
        evidence_receipt_sha256="a" * 64,
        normalized_content_sha256="b" * 64,
        observed_at=OBSERVED_AT,
    )


def _receipt(
    *,
    replacements: dict[ChoiceCapability, ChoiceCapabilityItem] | None = None,
    source_policy: SourcePolicy = SourcePolicy.SINGLE_SOURCE_ONLY,
    universe_completion_policy: UniverseCompletionPolicy = (
        UniverseCompletionPolicy.COMPLETE_FROZEN_UNIVERSE_ONLY
    ),
    membership_backfill_policy: MembershipBackfillPolicy = (
        MembershipBackfillPolicy.HISTORICAL_AS_OF_ONLY
    ),
    revision_policy: RevisionPolicy = RevisionPolicy.FIRST_DISCLOSURE_APPEND_ONLY,
) -> ChoiceCapabilityReceipt:
    replacements = replacements or {}
    return ChoiceCapabilityReceipt(
        receipt_id="choice-capability-audit-001",
        generated_at=OBSERVED_AT,
        coverage_cutoff=COVERAGE_CUTOFF,
        capabilities=tuple(
            replacements.get(capability, _item(capability))
            for capability in ChoiceCapability
        ),
        source_policy=source_policy,
        universe_completion_policy=universe_completion_policy,
        membership_backfill_policy=membership_backfill_policy,
        revision_policy=revision_policy,
    )


class ChoiceQualityGrowthGateTests(unittest.TestCase):
    def test_complete_controlled_receipt_only_satisfies_contract(self) -> None:
        receipt = _receipt()
        result = evaluate_choice_quality_growth_gate(receipt)

        self.assertEqual(result.status, ChoiceGateStatus.CONTRACT_SATISFIED)
        self.assertTrue(result.contract_satisfied)
        self.assertEqual(result.live_connectivity_status, "not_assessed")
        self.assertFalse(result.formal_truth_eligibility)
        benchmark = next(
            item
            for item in receipt.capabilities
            if item.capability is ChoiceCapability.TOTAL_RETURN_BENCHMARK
        )
        self.assertEqual(benchmark.subject_ids, ("H00906.CSI",))
        self.assertIs(benchmark.return_basis, BenchmarkReturnBasis.TOTAL_RETURN)

    def test_aggregate_subject_count_cannot_impersonate_complete_universe(self) -> None:
        with self.assertRaisesRegex(ChoiceGateContractError, "enumerate every distinct subject"):
            ChoiceCapabilityItem(
                capability=ChoiceCapability.QFQ_DAILY_BARS,
                verification=CapabilityVerification.CONTROLLED_EVIDENCE_VERIFIED,
                provider_id=ChoiceProviderId.CHOICE,
                dataset_contract="choice.qfq_daily_bars.v1",
                subject_ids=("CSI800_PIT",),
                fields=tuple(ChoiceField),
                coverage_start=date(2017, 1, 1),
                coverage_end=COVERAGE_CUTOFF,
                row_count=2_000_000,
                distinct_subject_count=800,
                evidence_receipt_sha256="a" * 64,
                normalized_content_sha256="b" * 64,
                observed_at=OBSERVED_AT,
            )

    def test_caller_cannot_construct_formal_truth_eligibility(self) -> None:
        with self.assertRaises(TypeError):
            ChoiceGateEvaluation(
                status=ChoiceGateStatus.CONTRACT_SATISFIED,
                receipt_sha256="a" * 64,
                missing_capabilities=(),
                missing_fields=(),
                policy_violations=(),
                formal_truth_eligibility=True,  # type: ignore[call-arg]
            )

    def test_missing_pit_capability_fails_closed_even_with_unsafe_policy(self) -> None:
        missing = _item(
            ChoiceCapability.PIT_INDUSTRY,
            verification=CapabilityVerification.MISSING,
        )
        receipt = _receipt(
            replacements={ChoiceCapability.PIT_INDUSTRY: missing},
            source_policy=SourcePolicy.MIXED_SOURCE_ALLOWED,
        )

        result = evaluate_choice_quality_growth_gate(receipt)

        self.assertEqual(result.status.value, "blocked_missing_pit_data")
        self.assertIn("pit_industry", result.missing_capabilities)
        self.assertIn("mixed_source_forbidden", result.policy_violations)

    def test_short_or_single_subject_coverage_cannot_claim_complete(self) -> None:
        incomplete = _item(
            ChoiceCapability.QFQ_DAILY_BARS,
            coverage_start=date(2026, 1, 1),
            row_count=1,
            distinct_subject_count=1,
            subject_ids=("000001.SZ",),
        )
        result = evaluate_choice_quality_growth_gate(
            _receipt(
                replacements={ChoiceCapability.QFQ_DAILY_BARS: incomplete}
            )
        )

        self.assertEqual(result.status.value, "blocked_missing_pit_data")
        self.assertIn("qfq_daily_bars:coverage_start", result.missing_fields)
        self.assertIn("qfq_daily_bars:distinct_subject_count", result.missing_fields)
        self.assertIn("qfq_daily_bars:row_count", result.missing_fields)

    def test_financial_gate_requires_operating_profit_roe_and_gross_profit_basis(self) -> None:
        no_gross_basis = tuple(
            field
            for field in ChoiceField
            if field not in {ChoiceField.GROSS_PROFIT, ChoiceField.OPERATING_COST}
        )
        receipt = _receipt(
            replacements={
                ChoiceCapability.FIRST_DISCLOSURE_FINANCIALS: _item(
                    ChoiceCapability.FIRST_DISCLOSURE_FINANCIALS,
                    fields=no_gross_basis,
                )
            }
        )
        result = evaluate_choice_quality_growth_gate(receipt)
        self.assertEqual(result.status, ChoiceGateStatus.BLOCKED_MISSING_PIT_DATA)
        self.assertIn(
            "first_disclosure_financials:gross_profit_or_revenue_and_operating_cost",
            result.missing_fields,
        )

        reconstructable = no_gross_basis + (ChoiceField.OPERATING_COST,)
        accepted = evaluate_choice_quality_growth_gate(
            _receipt(
                replacements={
                    ChoiceCapability.FIRST_DISCLOSURE_FINANCIALS: _item(
                        ChoiceCapability.FIRST_DISCLOSURE_FINANCIALS,
                        fields=reconstructable,
                    )
                }
            )
        )
        self.assertEqual(accepted.status, ChoiceGateStatus.CONTRACT_SATISFIED)

        missing_roe = tuple(
            field for field in ChoiceField if field is not ChoiceField.RETURN_ON_EQUITY
        )
        result = evaluate_choice_quality_growth_gate(
            _receipt(
                replacements={
                    ChoiceCapability.FIRST_DISCLOSURE_FINANCIALS: _item(
                        ChoiceCapability.FIRST_DISCLOSURE_FINANCIALS,
                        fields=missing_roe,
                    )
                }
            )
        )
        self.assertIn(
            "first_disclosure_financials:return_on_equity", result.missing_fields
        )

    def test_forbidden_data_policies_are_individually_audited(self) -> None:
        cases = (
            (
                {"source_policy": SourcePolicy.MIXED_SOURCE_ALLOWED},
                "mixed_source_forbidden",
            ),
            (
                {
                    "universe_completion_policy": (
                        UniverseCompletionPolicy.SUCCESSFUL_SUBSAMPLE_ALLOWED
                    )
                },
                "successful_subsample_forbidden",
            ),
            (
                {
                    "membership_backfill_policy": (
                        MembershipBackfillPolicy.CURRENT_CONSTITUENTS_BACKFILL
                    )
                },
                "current_constituents_backfill_forbidden",
            ),
            (
                {"revision_policy": RevisionPolicy.LATEST_REVISION_OVERWRITE},
                "revision_overwrite_forbidden",
            ),
        )
        for overrides, expected_violation in cases:
            with self.subTest(expected_violation=expected_violation):
                result = evaluate_choice_quality_growth_gate(_receipt(**overrides))
                self.assertEqual(
                    result.status, ChoiceGateStatus.BLOCKED_UNSAFE_DATA_POLICY
                )
                self.assertIn(expected_violation, result.policy_violations)

    def test_strings_booleans_and_raw_mappings_cannot_verify_capabilities(self) -> None:
        with self.assertRaisesRegex(ChoiceGateContractError, "explicit ChoiceCapability"):
            ChoiceCapabilityItem(  # type: ignore[arg-type]
                capability="historical_constituents",
                verification=CapabilityVerification.MISSING,
                provider_id=ChoiceProviderId.CHOICE,
            )
        with self.assertRaisesRegex(ChoiceGateContractError, "explicit CapabilityVerification"):
            ChoiceCapabilityItem(  # type: ignore[arg-type]
                capability=ChoiceCapability.HISTORICAL_CONSTITUENTS,
                verification=True,
                provider_id=ChoiceProviderId.CHOICE,
            )
        with self.assertRaisesRegex(ChoiceGateContractError, "ChoiceField enums"):
            ChoiceCapabilityItem(  # type: ignore[arg-type]
                capability=ChoiceCapability.HISTORICAL_CONSTITUENTS,
                verification=CapabilityVerification.CONTROLLED_EVIDENCE_VERIFIED,
                provider_id=ChoiceProviderId.CHOICE,
                dataset_contract="choice.constituents.v1",
                subject_ids=("CSI800_PIT",),
                fields=("instrument_id",),
                evidence_receipt_sha256="a" * 64,
                normalized_content_sha256="b" * 64,
                observed_at=OBSERVED_AT,
            )
        with self.assertRaisesRegex(ChoiceGateContractError, "caller string"):
            ChoiceCapabilityItem(  # type: ignore[arg-type]
                capability=ChoiceCapability.TOTAL_RETURN_BENCHMARK,
                verification=CapabilityVerification.CONTROLLED_EVIDENCE_VERIFIED,
                provider_id=ChoiceProviderId.CHOICE,
                dataset_contract="choice.benchmark.v1",
                subject_ids=("H00906.CSI",),
                return_basis="total_return",
                fields=(ChoiceField.TOTAL_RETURN_LEVEL,),
                evidence_receipt_sha256="a" * 64,
                normalized_content_sha256="b" * 64,
                observed_at=OBSERVED_AT,
            )
        for raw in ({}, True, "verified"):
            with self.subTest(raw=raw):
                with self.assertRaises(ChoiceGateContractError):
                    evaluate_choice_quality_growth_gate(raw)  # type: ignore[arg-type]

    def test_total_return_benchmark_must_bind_one_subject_and_typed_basis(self) -> None:
        with self.assertRaisesRegex(ChoiceGateContractError, "exactly one"):
            ChoiceCapabilityItem(
                capability=ChoiceCapability.TOTAL_RETURN_BENCHMARK,
                verification=CapabilityVerification.CONTROLLED_EVIDENCE_VERIFIED,
                provider_id=ChoiceProviderId.CHOICE,
                dataset_contract="choice.benchmark.v1",
                subject_ids=("H00906.CSI", "000906.SH"),
                return_basis=BenchmarkReturnBasis.TOTAL_RETURN,
                fields=(ChoiceField.TRADING_DATE, ChoiceField.TOTAL_RETURN_LEVEL),
                coverage_start=date(2018, 1, 1),
                coverage_end=COVERAGE_CUTOFF,
                row_count=3000,
                distinct_subject_count=1,
                evidence_receipt_sha256="a" * 64,
                normalized_content_sha256="b" * 64,
                observed_at=OBSERVED_AT,
            )
        with self.assertRaisesRegex(ChoiceGateContractError, "typed total_return"):
            ChoiceCapabilityItem(
                capability=ChoiceCapability.TOTAL_RETURN_BENCHMARK,
                verification=CapabilityVerification.CONTROLLED_EVIDENCE_VERIFIED,
                provider_id=ChoiceProviderId.CHOICE,
                dataset_contract="choice.benchmark.v1",
                subject_ids=("H00906.CSI",),
                fields=(ChoiceField.TRADING_DATE, ChoiceField.TOTAL_RETURN_LEVEL),
                coverage_start=date(2018, 1, 1),
                coverage_end=COVERAGE_CUTOFF,
                row_count=3000,
                distinct_subject_count=1,
                evidence_receipt_sha256="a" * 64,
                normalized_content_sha256="b" * 64,
                observed_at=OBSERVED_AT,
            )

    def test_receipt_hash_round_trip_and_exact_capability_set(self) -> None:
        receipt = _receipt()
        self.assertEqual(
            ChoiceCapabilityReceipt.from_dict(receipt.to_dict()).to_dict(),
            receipt.to_dict(),
        )
        tampered = receipt.to_dict()
        tampered["receipt_id"] = "different"
        with self.assertRaisesRegex(ChoiceGateContractError, "receipt_sha256 mismatch"):
            ChoiceCapabilityReceipt.from_dict(tampered)

        duplicate = list(receipt.capabilities)
        duplicate[-1] = duplicate[0]
        with self.assertRaisesRegex(ChoiceGateContractError, "exactly once"):
            ChoiceCapabilityReceipt(
                receipt_id="duplicate",
                generated_at=OBSERVED_AT,
                coverage_cutoff=COVERAGE_CUTOFF,
                capabilities=tuple(duplicate),
                source_policy=SourcePolicy.SINGLE_SOURCE_ONLY,
                universe_completion_policy=(
                    UniverseCompletionPolicy.COMPLETE_FROZEN_UNIVERSE_ONLY
                ),
                membership_backfill_policy=MembershipBackfillPolicy.HISTORICAL_AS_OF_ONLY,
                revision_policy=RevisionPolicy.FIRST_DISCLOSURE_APPEND_ONLY,
            )

    def test_schema_exposes_subject_binding_and_financial_fields(self) -> None:
        schema = json.loads(
            (ROOT / "schemas" / "choice_quality_growth_gate.v1.json").read_text(
                encoding="utf-8"
            )
        )
        fields = schema["$defs"]["choiceField"]["enum"]
        self.assertIn("operating_profit", fields)
        self.assertIn("gross_profit", fields)
        self.assertIn("return_on_equity", fields)
        required = schema["$defs"]["capabilityItem"]["required"]
        self.assertIn("subject_ids", required)
        self.assertIn("return_basis", required)
        self.assertIn("coverage_cutoff", schema["required"])
        for field in (
            "coverage_start",
            "coverage_end",
            "row_count",
            "distinct_subject_count",
        ):
            self.assertIn(field, required)

        emitted = _receipt().to_dict()
        self.assertEqual(set(emitted), set(schema["properties"]))
        capability_schema = schema["$defs"]["capabilityItem"]
        for item in emitted["capabilities"]:
            self.assertEqual(set(item), set(capability_schema["properties"]))
            self.assertTrue(set(capability_schema["required"]).issubset(item))


if __name__ == "__main__":
    unittest.main()
