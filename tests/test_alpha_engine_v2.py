from __future__ import annotations

from dataclasses import fields, replace
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
import unittest

from research.factor_discovery.governance import (
    ApprovedFactorRegistryV1,
    ApprovedFactorV1,
    FactorHypothesisV2,
    FactorValidationReceiptV1,
)
from research.strategy_workspace.alpha_engine_v2 import (
    FAST_FACTOR_IDS,
    FINANCIAL_FEATURE_IDS,
    NON_FINANCIAL_FEATURE_IDS,
    AlphaEngineError,
    AlphaFactorRuntimeBindingV1,
    AlphaModelAdmissionReceiptV1,
    AlphaModelTrainingReceiptV1,
    AlphaRuntimeBuildManifestV1,
    AlphaRankingV2,
    AlphaRunStatus,
    ControlledPitInstrumentV2,
    ControlledPitSnapshotV2,
    ControlledPriceBarV2,
    FrozenAlphaCalibrationV1,
    FrozenAlphaModelV2,
    FrozenLinearSubmodelV2,
    compute_alpha_model_candidate_sha256,
    compute_alpha_runtime_code_sha256,
    compute_alpha_submodel_bundle_sha256,
    run_alpha_engine,
    run_alpha_engine_diagnostic,
)
from research.strategy_workspace.experiment_v3_admission import (
    ExperimentV3AdmissionReceiptV1,
)
from research.market_data.validation import SchemaValidationError, validate_json_schema
from research.strategy_workspace.quality_growth import QuarterlyFundamental


TZ = timezone(timedelta(hours=8))
SHA = "a" * 64
EXPERIMENT_SPEC_SHA256 = "b" * 64
PREDICTION_HORIZON_SESSIONS = 20


def _sessions(end: date, count: int = 121) -> tuple[date, ...]:
    values: list[date] = []
    cursor = end
    while len(values) < count:
        if cursor.weekday() < 5:
            values.append(cursor)
        cursor -= timedelta(days=1)
    return tuple(reversed(values))


def _bars(
    instrument_id: str,
    sessions: tuple[date, ...],
    *,
    base: float,
    slope: float,
) -> tuple[ControlledPriceBarV2, ...]:
    result = []
    for index, session in enumerate(sessions):
        close = base + slope * index + 0.03 * (index % 5)
        result.append(
            ControlledPriceBarV2(
                instrument_id=instrument_id,
                session_date=session,
                close=close,
                high=close * 1.01,
                available_at=datetime.combine(session, time(15, 5), tzinfo=TZ),
                source_record_id=f"{instrument_id}:{session.isoformat()}",
                source_record_sha256=SHA,
            )
        )
    return tuple(result)


def _quarter_ends() -> tuple[date, ...]:
    return (
        date(2023, 9, 30),
        date(2023, 12, 31),
        date(2024, 3, 31),
        date(2024, 6, 30),
        date(2024, 9, 30),
        date(2024, 12, 31),
        date(2025, 3, 31),
        date(2025, 6, 30),
        date(2025, 9, 30),
        date(2025, 12, 31),
        date(2026, 3, 31),
        date(2026, 6, 30),
    )


def _fundamentals(
    instrument_id: str,
    *,
    scale: float = 1.0,
    missing_revenue: bool = False,
) -> tuple[QuarterlyFundamental, ...]:
    rows = []
    for index, period_end in enumerate(_quarter_ends()):
        revenue = None if missing_revenue and index == 4 else scale * (1000.0 + 55.0 * index)
        rows.append(
            QuarterlyFundamental(
                instrument_id=instrument_id,
                period_end=period_end,
                first_disclosed_at=datetime.combine(
                    period_end + timedelta(days=30), time(9), tzinfo=TZ
                ),
                source_record_id=f"{instrument_id}:Q:{period_end.isoformat()}",
                source_record_sha256=SHA,
                revision_sequence=1,
                roe=0.10 + 0.002 * index + 0.001 * (index % 2),
                net_profit=scale * (100.0 + 8.0 * index + 3.0 * (index % 3)),
                operating_cash_flow=scale * (130.0 + index),
                operating_profit=scale * (100.0 + index),
                gross_profit=scale * (200.0 + 2.0 * index),
                total_assets=scale * (2000.0 + 20.0 * index),
                total_liabilities=scale * (800.0 + 10.0 * index),
                revenue=revenue,
            )
        )
    return tuple(rows)


def _instrument_input(
    instrument_id: str,
    sessions: tuple[date, ...],
    *,
    scale: float = 1.0,
    missing_revenue: bool = False,
    is_financial: bool = False,
) -> ControlledPitInstrumentV2:
    decision_date = sessions[-1]
    return ControlledPitInstrumentV2(
        instrument_id=instrument_id,
        industry="CSI2021_L1/金融" if is_financial else "CSI2021_L1/工业",
        industry_is_financial=is_financial,
        constituent_available_at=datetime.combine(decision_date, time(9), tzinfo=TZ),
        industry_available_at=datetime.combine(decision_date, time(9), tzinfo=TZ),
        fundamentals=_fundamentals(
            instrument_id, scale=scale, missing_revenue=missing_revenue
        ),
        price_bars=_bars(instrument_id, sessions, base=10.0 * scale, slope=0.03 * scale),
    )


def _submodel(submodel_id: str, *, multiplier: float = 1.0) -> FrozenLinearSubmodelV2:
    feature_ids = FINANCIAL_FEATURE_IDS if submodel_id == "financial" else NON_FINANCIAL_FEATURE_IDS
    return FrozenLinearSubmodelV2(
        submodel_id=submodel_id,
        feature_ids=feature_ids,
        intercept=0.0,
        coefficients=tuple(multiplier * (index + 1) / 1000.0 for index in range(len(feature_ids))),
        centers=tuple(0.0 for _ in feature_ids),
        scales=tuple(1.0 for _ in feature_ids),
    )


def _approved_factor_registry(
    *,
    factor_ids: tuple[str, ...] | None = None,
) -> ApprovedFactorRegistryV1:
    selected = factor_ids or tuple(sorted(set(FINANCIAL_FEATURE_IDS) | set(NON_FINANCIAL_FEATURE_IDS)))
    information_cutoff_at = datetime(2022, 1, 1, tzinfo=TZ)
    created_at = datetime(2022, 1, 2, tzinfo=TZ)
    validation_started_at = datetime(2022, 1, 3, tzinfo=TZ)
    validation_completed_at = datetime(2022, 1, 4, tzinfo=TZ)
    approved_at = datetime(2022, 1, 5, tzinfo=TZ)
    approved = []
    for index, factor_id in enumerate(selected, start=1):
        hypothesis = FactorHypothesisV2(
            hypothesis_id=f"alpha-v3-{factor_id.lower()}",
            factor_id=factor_id,
            created_at=created_at,
            information_cutoff_at=information_cutoff_at,
            formula=f"formula_{index}_{factor_id.lower()}(pit_value)",
            input_fields=("pit_value",),
            input_schema_sha256=f"{index % 15 + 1:x}" * 64,
            prediction_target="forward_total_return",
            horizon_trading_days=PREDICTION_HORIZON_SESSIONS,
            expected_sign="positive",
            universe_policy="controlled_csi800_pit",
            benchmark_policy="csi800_total_return",
            economic_rationale=f"pre_registered_reason_{factor_id.lower()}",
            falsification_conditions=(f"reject_if_{factor_id.lower()}_fails",),
        )
        validation = FactorValidationReceiptV1.from_hypothesis(
            hypothesis,
            receipt_id=f"validation-{factor_id.lower()}",
            validator_id="independent-validator",
            experiment_spec_sha256=EXPERIMENT_SPEC_SHA256,
            implementation_code_sha256=f"{(index + 1) % 15 + 1:x}" * 64,
            validation_spec_sha256="c" * 64,
            validation_dataset_sha256=f"{(index + 2) % 15 + 1:x}" * 64,
            validation_code_sha256="d" * 64,
            validation_started_at=validation_started_at,
            validation_completed_at=validation_completed_at,
        )
        approved.append(
            ApprovedFactorV1.from_validation_receipt(
                validation,
                approved_at=approved_at,
            )
        )
    return ApprovedFactorRegistryV1(
        registry_id="alpha-v3-approved-factors",
        frozen_at=datetime(2022, 1, 6, tzinfo=TZ),
        experiment_spec_sha256=EXPERIMENT_SPEC_SHA256,
        prediction_target="forward_total_return",
        horizon_trading_days=PREDICTION_HORIZON_SESSIONS,
        universe_policy="controlled_csi800_pit",
        benchmark_policy="csi800_total_return",
        factors=tuple(approved),
    )


def _calibration(
    *,
    approved_factor_registry_sha256: str,
    financial_intercept: float = 0.0,
    financial_slope: float = 1.0,
    non_financial_intercept: float = 0.0,
    non_financial_slope: float = 1.0,
) -> FrozenAlphaCalibrationV1:
    return FrozenAlphaCalibrationV1(
        calibration_id="alpha-v3-common-return-calibration",
        target_id="forward_total_return",
        prediction_horizon_sessions=PREDICTION_HORIZON_SESSIONS,
        experiment_spec_sha256=EXPERIMENT_SPEC_SHA256,
        approved_factor_registry_sha256=approved_factor_registry_sha256,
        universe_policy="controlled_csi800_pit",
        benchmark_policy="csi800_total_return",
        fitting_window_start=date(2018, 1, 1),
        fitting_window_end=date(2022, 12, 31),
        fitting_data_cutoff_at=datetime(2023, 2, 1, tzinfo=TZ),
        fitted_at=datetime(2023, 2, 3, tzinfo=TZ),
        financial_intercept=financial_intercept,
        financial_slope=financial_slope,
        non_financial_intercept=non_financial_intercept,
        non_financial_slope=non_financial_slope,
        calibration_dataset_sha256="e" * 64,
        calibration_code_sha256="f" * 64,
        calibration_config_sha256="0" * 64,
    )


def _model_bundle(
    *,
    multiplier: float = 1.0,
    registry: ApprovedFactorRegistryV1 | None = None,
    financial_calibration_intercept: float = 0.0,
    financial_calibration_slope: float = 1.0,
    non_financial_calibration_intercept: float = 0.0,
    non_financial_calibration_slope: float = 1.0,
) -> tuple[FrozenAlphaModelV2, ApprovedFactorRegistryV1, ExperimentV3AdmissionReceiptV1]:
    registry = registry or _approved_factor_registry()
    financial_submodel = _submodel("financial", multiplier=multiplier)
    non_financial_submodel = _submodel("non_financial", multiplier=multiplier)
    calibration = _calibration(
        approved_factor_registry_sha256=registry.registry_sha256,
        financial_intercept=financial_calibration_intercept,
        financial_slope=financial_calibration_slope,
        non_financial_intercept=non_financial_calibration_intercept,
        non_financial_slope=non_financial_calibration_slope,
    )
    runtime_manifest = AlphaRuntimeBuildManifestV1(
        manifest_id="alpha-v3-runtime-build-test",
        built_at=datetime(2022, 1, 7, tzinfo=TZ),
        experiment_spec_sha256=EXPERIMENT_SPEC_SHA256,
        approved_factor_registry_sha256=registry.registry_sha256,
        prediction_target="forward_total_return",
        prediction_horizon_sessions=PREDICTION_HORIZON_SESSIONS,
        universe_policy="controlled_csi800_pit",
        benchmark_policy="csi800_total_return",
        runtime_code_sha256=compute_alpha_runtime_code_sha256(),
        factor_bindings=tuple(
            AlphaFactorRuntimeBindingV1(
                factor_id=factor.factor_id,
                formula_sha256=factor.formula_sha256,
                implementation_code_sha256=factor.implementation_code_sha256,
                input_schema_sha256=factor.input_schema_sha256,
            )
            for factor in registry.factors
        ),
    )
    common = {
        "model_id": "adaptive-alpha-v2",
        "model_version": "experiment-v3-test-v1",
        "training_window_start": date(2018, 1, 1),
        "training_window_end": date(2022, 12, 31),
        "training_data_cutoff_at": datetime(2023, 2, 1, tzinfo=TZ),
        "trained_at": datetime(2023, 2, 2, tzinfo=TZ),
        "frozen_at": datetime(2023, 2, 10, tzinfo=TZ),
        "training_dataset_sha256": "1" * 64,
        "training_code_sha256": "2" * 64,
        "preprocessing_policy_sha256": "3" * 64,
        "model_config_sha256": "4" * 64,
        "experiment_spec_sha256": EXPERIMENT_SPEC_SHA256,
        "approved_factor_registry_sha256": registry.registry_sha256,
        "prediction_target": "forward_total_return",
        "prediction_horizon_sessions": PREDICTION_HORIZON_SESSIONS,
        "universe_policy": "controlled_csi800_pit",
        "benchmark_policy": "csi800_total_return",
        "runtime_build_manifest": runtime_manifest,
    }
    training_receipt = AlphaModelTrainingReceiptV1(
        receipt_id="alpha-v3-training-receipt",
        issued_at=datetime(2023, 2, 3, tzinfo=TZ),
        runtime_build_manifest_sha256=runtime_manifest.manifest_sha256,
        submodel_bundle_sha256=compute_alpha_submodel_bundle_sha256(
            financial_submodel,
            non_financial_submodel,
        ),
        **{key: common[key] for key in (
            "model_id", "model_version", "experiment_spec_sha256",
            "approved_factor_registry_sha256", "prediction_target",
            "prediction_horizon_sessions", "universe_policy", "benchmark_policy",
            "training_window_start", "training_window_end", "training_data_cutoff_at",
            "trained_at", "training_dataset_sha256", "training_code_sha256",
            "preprocessing_policy_sha256", "model_config_sha256",
        )},
    )
    candidate_sha256 = compute_alpha_model_candidate_sha256(
        **common,
        artifact_status="frozen_train_only_calibrated_research_candidate",
        training_partition="train_only",
        calibration_artifact=calibration,
        model_training_receipt=training_receipt,
        financial_submodel=financial_submodel,
        non_financial_submodel=non_financial_submodel,
    )
    model_admission_receipt = AlphaModelAdmissionReceiptV1(
        receipt_id="alpha-v3-model-admission",
        issued_at=datetime(2023, 2, 5, tzinfo=TZ),
        model_id=common["model_id"],
        model_version=common["model_version"],
        model_candidate_sha256=candidate_sha256,
        experiment_spec_sha256=common["experiment_spec_sha256"],
        approved_factor_registry_sha256=common["approved_factor_registry_sha256"],
        prediction_target=common["prediction_target"],
        model_training_receipt_sha256=training_receipt.receipt_sha256,
        calibration_receipt_sha256=calibration.calibration_receipt_sha256,
        prediction_horizon_sessions=PREDICTION_HORIZON_SESSIONS,
        universe_policy=common["universe_policy"],
        benchmark_policy=common["benchmark_policy"],
        runtime_build_manifest_sha256=runtime_manifest.manifest_sha256,
    )
    model = FrozenAlphaModelV2(
        **common,
        calibration_artifact=calibration,
        model_training_receipt=training_receipt,
        model_admission_receipt=model_admission_receipt,
        financial_submodel=financial_submodel,
        non_financial_submodel=non_financial_submodel,
    )
    experiment_receipt = ExperimentV3AdmissionReceiptV1(
        receipt_id="experiment-v3-alpha-admission",
        issued_at=datetime(2023, 2, 12, tzinfo=TZ),
        experiment_spec_sha256=common["experiment_spec_sha256"],
        approved_factor_registry_sha256=common["approved_factor_registry_sha256"],
        approved_factor_registry_frozen_at=registry.frozen_at,
        model_training_receipt_sha256=training_receipt.receipt_sha256,
        model_admission_receipt_sha256=model_admission_receipt.receipt_sha256,
        model_sha256=model.model_sha256,
        model_frozen_at=model.frozen_at,
        calibration_receipt_sha256=calibration.calibration_receipt_sha256,
        calibration_horizon_sessions=PREDICTION_HORIZON_SESSIONS,
        exposure_policy_source_sha256="5" * 64,
        exposure_policy_frozen_at=datetime(2023, 2, 8, tzinfo=TZ),
        constructor_policy_source_sha256="6" * 64,
        constructor_policy_frozen_at=datetime(2023, 2, 9, tzinfo=TZ),
    )
    return model, registry, experiment_receipt


def _model(*, multiplier: float = 1.0) -> FrozenAlphaModelV2:
    return _model_bundle(multiplier=multiplier)[0]


def _run(
    snapshot: ControlledPitSnapshotV2,
    model: FrozenAlphaModelV2 | None = None,
) -> AlphaRankingV2:
    admitted_model, registry, receipt = _model_bundle()
    return run_alpha_engine_diagnostic(
        snapshot,
        model or admitted_model,
        approved_factor_registry=registry,
        experiment_v3_admission_receipt=receipt,
    )


def _snapshot(
    *,
    instruments: tuple[ControlledPitInstrumentV2, ...] | None = None,
    member_ids: tuple[str, ...] = ("000001.SZ", "600000.SH"),
) -> ControlledPitSnapshotV2:
    sessions = _sessions(date(2026, 8, 18))
    if instruments is None:
        instruments = (
            _instrument_input("000001.SZ", sessions, scale=1.0),
            _instrument_input("600000.SH", sessions, scale=1.3),
        )
    return ControlledPitSnapshotV2(
        decision_at=datetime(2026, 8, 18, 16, 0, tzinfo=TZ),
        universe_as_of=date(2026, 8, 18),
        universe_available_at=datetime(2026, 8, 18, 9, 0, tzinfo=TZ),
        universe_version="CSI800-PIT-20260818",
        member_ids=member_ids,
        instruments=instruments,
        trading_sessions=sessions,
        benchmark_instrument_id="H00906.CSI",
        benchmark_price_bars=_bars("H00906.CSI", sessions, base=5000.0, slope=2.0),
        trading_calendar_receipt_sha256="5" * 64,
        universe_receipt_sha256="6" * 64,
        financial_data_receipt_sha256="7" * 64,
        industry_data_receipt_sha256="8" * 64,
        price_data_receipt_sha256="9" * 64,
    )


class AlphaEngineV2Tests(unittest.TestCase):
    def test_repeated_frozen_inputs_are_byte_deterministic(self) -> None:
        snapshot = _snapshot()
        model = _model()
        first = _run(snapshot, model)
        second = _run(snapshot, model)
        self.assertEqual(first.status, AlphaRunStatus.DIAGNOSTIC_ONLY_NOT_ADMITTED)
        self.assertEqual(len(first.rows), 2)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.ranking_sha256, second.ranking_sha256)
        self.assertEqual(first.rows[0].percentile, 1.0)
        self.assertEqual(first.rows[1].percentile, 0.0)
        json.dumps(first.to_dict(), ensure_ascii=False)
        validate_json_schema(
            first.to_dict(),
            Path(__file__).resolve().parents[1] / "schemas" / "alpha_ranking.v2.json",
        )
        json.dumps(snapshot.to_dict(), ensure_ascii=False)
        json.dumps(model.to_dict(), ensure_ascii=False)

    def test_model_artifact_self_hash_changes_with_coefficients(self) -> None:
        self.assertNotEqual(_model(multiplier=1.0).model_sha256, _model(multiplier=2.0).model_sha256)

    def test_model_hash_binds_train_only_calibration_artifact(self) -> None:
        baseline = _model_bundle()[0]
        recalibrated = _model_bundle(
            financial_calibration_intercept=0.01,
            financial_calibration_slope=1.25,
        )[0]
        self.assertNotEqual(
            baseline.calibration_receipt_sha256,
            recalibrated.calibration_receipt_sha256,
        )
        self.assertNotEqual(baseline.model_sha256, recalibrated.model_sha256)

    def test_runtime_code_manifest_and_review_timestamps_are_enforced(self) -> None:
        model = _model()
        tampered_manifest = replace(
            model.runtime_build_manifest,
            runtime_code_sha256="9" * 64,
        )
        with self.assertRaisesRegex(AlphaEngineError, "executing factor code"):
            replace(model, runtime_build_manifest=tampered_manifest)

        early_review = replace(
            model.model_admission_receipt,
            issued_at=model.model_training_receipt.issued_at - timedelta(seconds=1),
        )
        with self.assertRaisesRegex(AlphaEngineError, "timestamps are out of order"):
            replace(model, model_admission_receipt=early_review)

    def test_valid_diagnostic_binding_cannot_make_formal_alpha_ok(self) -> None:
        model, registry, receipt = _model_bundle()
        result = run_alpha_engine(
            _snapshot(),
            model,
            approved_factor_registry=registry,
            experiment_v3_admission_receipt=receipt,
        )
        self.assertEqual(result.status, AlphaRunStatus.DATA_FAIL_CLOSED)
        self.assertTrue(
            all(
                "FORMAL_EXPERIMENT_V3_ADMISSION_BLOCKED" in row.exclusion_codes
                for row in result.rows
            )
        )

    def test_receipt_subclass_cannot_override_formal_admission(self) -> None:
        class CallerOwnedReceipt(ExperimentV3AdmissionReceiptV1):
            def require_structural_valid(self, *, as_of):
                return self

            def require_valid(self, *, as_of):
                return self

        model, registry, receipt = _model_bundle()
        forged = CallerOwnedReceipt(
            **{
                item.name: getattr(receipt, item.name)
                for item in fields(ExperimentV3AdmissionReceiptV1)
                if item.init
            }
        )

        result = run_alpha_engine(
            _snapshot(),
            model,
            approved_factor_registry=registry,
            experiment_v3_admission_receipt=forged,
        )

        self.assertEqual(result.status, AlphaRunStatus.DATA_FAIL_CLOSED)
        self.assertTrue(
            all(
                "MISSING_TYPED_EXPERIMENT_V3_ADMISSION_RECEIPT"
                in row.exclusion_codes
                for row in result.rows
            )
        )

    def test_bare_self_hashed_model_without_typed_admission_evidence_fails_closed(self) -> None:
        model, registry, receipt = _model_bundle()
        missing_all = run_alpha_engine(_snapshot(), model)
        missing_receipt = run_alpha_engine(
            _snapshot(),
            model,
            approved_factor_registry=registry,
        )
        missing_registry = run_alpha_engine(
            _snapshot(),
            model,
            experiment_v3_admission_receipt=receipt,
        )
        for result, code in (
            (missing_all, "MISSING_TYPED_EXPERIMENT_V3_ADMISSION_RECEIPT"),
            (missing_receipt, "MISSING_TYPED_EXPERIMENT_V3_ADMISSION_RECEIPT"),
            (missing_registry, "MISSING_TYPED_APPROVED_FACTOR_REGISTRY"),
        ):
            self.assertEqual(result.status, AlphaRunStatus.DATA_FAIL_CLOSED)
            self.assertTrue(all(code in row.exclusion_codes for row in result.rows))

    def test_hash_matching_registry_still_requires_exact_approved_feature_set(self) -> None:
        required = tuple(sorted(set(FINANCIAL_FEATURE_IDS) | set(NON_FINANCIAL_FEATURE_IDS)))
        incomplete_registry = _approved_factor_registry(factor_ids=required[:-1])
        model, registry, receipt = _model_bundle(registry=incomplete_registry)
        self.assertEqual(registry.registry_sha256, model.approved_factor_registry_sha256)

        result = run_alpha_engine_diagnostic(
            _snapshot(),
            model,
            approved_factor_registry=registry,
            experiment_v3_admission_receipt=receipt,
        )

        self.assertEqual(result.status, AlphaRunStatus.DATA_FAIL_CLOSED)
        self.assertTrue(
            all(
                "APPROVED_FACTOR_REGISTRY_FEATURE_MISMATCH" in row.exclusion_codes
                for row in result.rows
            )
        )

    def test_registry_with_unapproved_extra_factor_is_rejected_even_when_fully_bound(self) -> None:
        required = tuple(sorted(set(FINANCIAL_FEATURE_IDS) | set(NON_FINANCIAL_FEATURE_IDS)))
        registry = _approved_factor_registry(factor_ids=required + ("UNAPPROVED_EXTRA",))
        model, registry, receipt = _model_bundle(registry=registry)
        result = run_alpha_engine(
            _snapshot(),
            model,
            approved_factor_registry=registry,
            experiment_v3_admission_receipt=receipt,
        )
        self.assertEqual(result.status, AlphaRunStatus.DATA_FAIL_CLOSED)
        self.assertIn(
            "APPROVED_FACTOR_REGISTRY_FEATURE_MISMATCH",
            result.rows[0].exclusion_codes,
        )

    def test_financial_and_non_financial_scores_are_calibrated_before_pool_ranking(self) -> None:
        sessions = _sessions(date(2026, 8, 18))
        financial = _instrument_input(
            "000001.SZ",
            sessions,
            scale=1.0,
            is_financial=True,
        )
        non_financial = _instrument_input("600000.SH", sessions, scale=1.3)
        model, registry, receipt = _model_bundle(
            financial_calibration_intercept=10.0,
            financial_calibration_slope=2.0,
            non_financial_calibration_intercept=-10.0,
            non_financial_calibration_slope=0.5,
        )

        result = run_alpha_engine_diagnostic(
            _snapshot(instruments=(financial, non_financial)),
            model,
            approved_factor_registry=registry,
            experiment_v3_admission_receipt=receipt,
        )

        self.assertEqual(result.status, AlphaRunStatus.DIAGNOSTIC_ONLY_NOT_ADMITTED)
        by_id = {row.instrument_id: row for row in result.rows}
        self.assertEqual(by_id["000001.SZ"].rank, 1)
        self.assertGreater(by_id["000001.SZ"].predicted_return, 9.0)
        self.assertLess(by_id["600000.SH"].predicted_return, -9.0)

    def test_tampered_calibration_and_admission_receipts_fail_closed(self) -> None:
        model, registry, receipt = _model_bundle()
        object.__setattr__(model.calibration_artifact, "financial_slope", 9.0)
        calibration_attack = run_alpha_engine(
            _snapshot(),
            model,
            approved_factor_registry=registry,
            experiment_v3_admission_receipt=receipt,
        )
        self.assertEqual(calibration_attack.status, AlphaRunStatus.DATA_FAIL_CLOSED)
        self.assertIn("MODEL_HASH_MISMATCH", calibration_attack.rows[0].exclusion_codes)
        self.assertIn("INVALID_MODEL_INTERNAL_RECEIPT", calibration_attack.rows[0].exclusion_codes)

        model, registry, receipt = _model_bundle()
        object.__setattr__(receipt, "model_training_receipt_sha256", "f" * 64)
        admission_attack = run_alpha_engine(
            _snapshot(),
            model,
            approved_factor_registry=registry,
            experiment_v3_admission_receipt=receipt,
        )
        self.assertEqual(admission_attack.status, AlphaRunStatus.DATA_FAIL_CLOSED)
        self.assertIn(
            "INVALID_EXPERIMENT_V3_ADMISSION_RECEIPT",
            admission_attack.rows[0].exclusion_codes,
        )

    def test_legacy_frozen_model_v1_is_explicitly_rejected(self) -> None:
        with self.assertRaisesRegex(AlphaEngineError, "legacy v1"):
            replace(_model(), schema_version="frozen-alpha-model.v1")

    def test_tampered_model_payload_fails_complete_universe_closed(self) -> None:
        model = _model()
        object.__setattr__(model, "model_version", "tampered")
        result = _run(_snapshot(), model)
        self.assertEqual(result.status, AlphaRunStatus.DATA_FAIL_CLOSED)
        self.assertTrue(all("MODEL_HASH_MISMATCH" in row.exclusion_codes for row in result.rows))

    def test_future_price_available_at_fails_complete_universe_closed(self) -> None:
        snapshot = _snapshot()
        first_input = snapshot.instruments[0]
        bars = list(first_input.price_bars)
        bars[-1] = replace(
            bars[-1], available_at=snapshot.decision_at + timedelta(seconds=1)
        )
        attacked_input = replace(first_input, price_bars=tuple(bars))
        attacked = replace(
            snapshot,
            instruments=(attacked_input, snapshot.instruments[1]),
        )
        result = _run(attacked)
        self.assertEqual(result.status, AlphaRunStatus.DATA_FAIL_CLOSED)
        self.assertTrue(all(not item.eligibility for item in result.rows))
        self.assertTrue(
            all("FUTURE_PRICE_AVAILABLE_AT" in item.exclusion_codes for item in result.rows)
        )

    def test_future_fundamental_record_fails_complete_universe_closed(self) -> None:
        snapshot = _snapshot()
        first_input = snapshot.instruments[0]
        future = replace(
            first_input.fundamentals[-1],
            first_disclosed_at=snapshot.decision_at + timedelta(days=1),
        )
        attacked_input = replace(
            first_input,
            fundamentals=first_input.fundamentals[:-1] + (future,),
        )
        attacked = replace(snapshot, instruments=(attacked_input, snapshot.instruments[1]))
        result = _run(attacked)
        self.assertEqual(result.status, AlphaRunStatus.DATA_FAIL_CLOSED)
        self.assertIn("FUTURE_FUNDAMENTAL_AVAILABLE_AT", result.rows[0].exclusion_codes)

    def test_plus14_decision_uses_the_cst_strategy_date(self) -> None:
        snapshot = _snapshot()
        plus_14 = timezone(timedelta(hours=14))
        decision_at = datetime(2026, 8, 19, 5, 30, tzinfo=plus_14)

        result = _run(replace(snapshot, decision_at=decision_at))

        self.assertEqual(decision_at.astimezone(TZ).date(), date(2026, 8, 18))
        self.assertEqual(result.status, AlphaRunStatus.DIAGNOSTIC_ONLY_NOT_ADMITTED)
        self.assertTrue(all(item.eligibility for item in result.rows))

    def test_plus14_raw_date_cannot_admit_a_future_cst_session(self) -> None:
        plus_14 = timezone(timedelta(hours=14))
        decision_at = datetime(2026, 8, 19, 5, 30, tzinfo=plus_14)
        claimed_available_at = decision_at - timedelta(minutes=1)
        future_sessions = _sessions(date(2026, 8, 19))

        def retimed_input(instrument_id: str, scale: float) -> ControlledPitInstrumentV2:
            value = _instrument_input(instrument_id, future_sessions, scale=scale)
            return replace(
                value,
                constituent_available_at=claimed_available_at,
                industry_available_at=claimed_available_at,
                price_bars=tuple(
                    replace(bar, available_at=claimed_available_at)
                    for bar in value.price_bars
                ),
            )

        base = _snapshot()
        attacked = replace(
            base,
            decision_at=decision_at,
            universe_as_of=date(2026, 8, 19),
            universe_available_at=claimed_available_at,
            trading_sessions=future_sessions,
            instruments=(
                retimed_input("000001.SZ", 1.0),
                retimed_input("600000.SH", 1.3),
            ),
            benchmark_price_bars=tuple(
                replace(bar, available_at=claimed_available_at)
                for bar in _bars(
                    "H00906.CSI", future_sessions, base=5000.0, slope=2.0
                )
            ),
        )

        result = _run(attacked)

        self.assertEqual(decision_at.astimezone(TZ).date(), date(2026, 8, 18))
        self.assertEqual(result.status, AlphaRunStatus.DATA_FAIL_CLOSED)
        self.assertTrue(
            all("FUTURE_TRADING_SESSION" in item.exclusion_codes for item in result.rows)
        )
        self.assertTrue(
            all("FUTURE_PRICE_SESSION" in item.exclusion_codes for item in result.rows)
        )

    def test_missing_factor_excludes_instrument_without_zero_fill(self) -> None:
        sessions = _sessions(date(2026, 8, 18))
        missing = _instrument_input(
            "000001.SZ", sessions, scale=1.0, missing_revenue=True
        )
        valid = _instrument_input("600000.SH", sessions, scale=1.2)
        result = _run(_snapshot(instruments=(missing, valid)))
        self.assertEqual(result.status, AlphaRunStatus.DIAGNOSTIC_ONLY_NOT_ADMITTED)
        by_id = {item.instrument_id: item for item in result.rows}
        excluded = by_id["000001.SZ"]
        self.assertFalse(excluded.eligibility)
        self.assertIsNone(excluded.predicted_return)
        self.assertTrue(
            any(code.startswith("MISSING_FACTOR:QG_REVENUE_GROWTH_STABILITY:") for code in excluded.exclusion_codes)
        )
        self.assertTrue(by_id["600000.SH"].eligibility)

    def test_missing_member_input_is_retained_and_no_eligible_means_cash(self) -> None:
        snapshot = _snapshot(instruments=())
        result = _run(snapshot)
        self.assertEqual(result.status, AlphaRunStatus.DIAGNOSTIC_ONLY_NOT_ADMITTED)
        self.assertEqual(tuple(item.instrument_id for item in result.rows), snapshot.member_ids)
        self.assertTrue(
            all(item.exclusion_codes == ("MISSING_INSTRUMENT_INPUT",) for item in result.rows)
        )

    def test_only_typed_snapshot_and_exact_feature_family_are_accepted(self) -> None:
        with self.assertRaises(AlphaEngineError):
            run_alpha_engine({}, _model())  # type: ignore[arg-type]
        with self.assertRaises(AlphaEngineError):
            FrozenLinearSubmodelV2(
                submodel_id="financial",
                feature_ids=FAST_FACTOR_IDS,
                intercept=0.0,
                coefficients=(0.0,) * 6,
                centers=(0.0,) * 6,
                scales=(1.0,) * 6,
            )

    def test_new_schemas_are_valid_json(self) -> None:
        root = Path(__file__).resolve().parents[1] / "schemas"
        for name in (
            "controlled_pit_decision_snapshot.v1.json",
            "frozen_alpha_model.v1.json",
            "frozen_alpha_model.v2.json",
            "alpha_ranking.v2.json",
        ):
            payload = json.loads((root / name).read_text(encoding="utf-8"))
            self.assertEqual(payload["$schema"], "https://json-schema.org/draft/2020-12/schema")

    def test_frozen_model_v2_payload_validates_and_v1_schema_does_not_accept_it(self) -> None:
        root = Path(__file__).resolve().parents[1] / "schemas"
        payload = _model().to_dict()
        validate_json_schema(payload, root / "frozen_alpha_model.v2.json")
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(payload, root / "frozen_alpha_model.v1.json")


if __name__ == "__main__":
    unittest.main()
