from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import research.strategy_workspace.next_session_signal as next_session_signal_module
import research.strategy_workspace.daily_signal_publication as daily_signal_publication_module

from operations.daily_pipeline import (
    ControlledHeldPositionReferenceV1,
    DailyPipelineBlockedRunV1,
    DailyPipelineError,
    FrozenDailyDataV2,
    ManualFillConfirmationV1,
    append_close_paper_ledger_v2,
    record_manual_fills,
    run_after_close_daily_pipeline,
    run_pre_open_review,
    _validate_exposure_memory_provenance,
)
from research.strategy_workspace.adaptive_exposure import (
    FROZEN_ADAPTIVE_EXPOSURE_POLICY_SHA256,
)
from research.strategy_workspace.contracts import canonical_json_bytes, canonical_sha256
from research.strategy_workspace.exposure_engine_v2 import (
    ExposureInputCategory,
    ExposureState,
    ExposureStateMemoryV2,
)
from research.strategy_workspace.experiment_v3_admission import (
    ExperimentV3AdmissionReceiptV1,
)
from research.strategy_workspace.next_session_signal import (
    CalendarRegistryEntry,
    NextSessionAlreadyConsumed,
    OfficialCalendarReceipt,
    OfficialCalendarRegistry,
    canonical_manual_fill_bundle_path,
    canonical_next_session_consumption_path,
)
from research.strategy_workspace.paper_ledger_v2 import (
    ADAPTIVE_POLICY_SCHEMA_VERSION,
    ADAPTIVE_STRATEGY_ID,
    CanonicalExecutionCostBundleV1,
    ControlledCloseMarkBundleV1,
    PaperCloseExecutionEvidenceV1,
    PaperDailySessionDraftV2,
    PaperExecutionAttemptV2,
    PaperLedgerV2Error,
    VerifiedPaperLedgerV2,
    append_paper_daily_session_v2,
    create_or_verify_paper_ledger_v2,
)
from research.strategy_workspace.portfolio_constructor_v2 import (
    ConstructorCostPolicy,
    PortfolioConstructorPolicy,
)
from research.market_data.validation import validate_json_schema
from tests.test_alpha_engine_v2 import (
    _bars,
    _instrument_input,
    _model_bundle,
    _sessions,
    _snapshot,
)
from tests.test_exposure_engine_v2 import _inputs, _memory, _policy
from trading.costs import FeeSchedule
from trading.models import (
    AccountSnapshot,
    InstrumentRule,
    MarketQuote,
    PortfolioIntent,
    PortfolioIntentType,
    Position,
)


TZ = timezone(timedelta(hours=8))
D = Decimal
STRATEGY_DATE = date(2026, 8, 18)
EXECUTION_DATE = date(2026, 8, 19)


class StaticUpdater:
    def __init__(self, payload: FrozenDailyDataV2) -> None:
        self.payload = payload
        self.calls: list[date] = []

    def update_and_freeze(self, strategy_date: date) -> FrozenDailyDataV2:
        self.calls.append(strategy_date)
        return self.payload


class RaisingUpdater:
    def __init__(self) -> None:
        self.calls: list[date] = []

    def update_and_freeze(self, strategy_date: date) -> FrozenDailyDataV2:
        self.calls.append(strategy_date)
        raise OSError("provider detail must not enter the immutable receipt")


def admitted_exposure_policy(
    admission_receipt: ExperimentV3AdmissionReceiptV1,
):
    return replace(
        _policy(),
        preregistered_at=admission_receipt.exposure_policy_frozen_at,
        policy_source_sha256=admission_receipt.exposure_policy_source_sha256,
        experiment_spec_sha256=admission_receipt.experiment_spec_sha256,
        policy_admission_receipt=admission_receipt,
    )


def constructor_policy(
    decision_at: datetime,
    admission_receipt: ExperimentV3AdmissionReceiptV1,
) -> PortfolioConstructorPolicy:
    return PortfolioConstructorPolicy(
        policy_id="daily-pipeline-integration-v1",
        frozen_at=admission_receipt.constructor_policy_frozen_at,
        policy_source_sha256=(
            admission_receipt.constructor_policy_source_sha256
        ),
        experiment_spec_sha256=admission_receipt.experiment_spec_sha256,
        policy_admission_receipt=admission_receipt,
        max_positions=3,
        max_position_weight=D("0.40"),
        entry_percentile_min=D("0.80"),
        hold_percentile_min=D("0.60"),
        entry_predicted_return_min=D("0.000001"),
        hold_predicted_return_min=D("0"),
        no_trade_threshold=D("0"),
        maximum_execution_price_deviation=D("0.02"),
        maximum_quote_age_seconds=300,
        maximum_account_age_seconds=600,
        costs=ConstructorCostPolicy(
            commission_rate=D("0.00018"),
            minimum_commission=D("5"),
            sell_tax_rate=D("0.0005"),
            transfer_fee_rate=D("0.00001"),
            slippage_bps_one_way=D("10"),
        ),
    )


def fees() -> FeeSchedule:
    return FeeSchedule(D("0.00018"), D("5"), D("0.00001"))


def rules(member_ids: tuple[str, ...]) -> dict[str, InstrumentRule]:
    return {
        instrument_id: InstrumentRule(
            instrument_id=instrument_id,
            name=f"controlled-{instrument_id}",
            instrument_type="A_SHARE",
            lot_size=100,
            tick_size=D("0.01"),
            sell_stamp_duty_rate=D("0.0005"),
            t_plus_one=True,
        )
        for instrument_id in member_ids
    }


def receipt(snapshot) -> OfficialCalendarReceipt:
    strategy_session = snapshot.decision_at.astimezone(TZ).date()
    following_sessions: list[date] = []
    cursor = strategy_session + timedelta(days=1)
    while len(following_sessions) < 2:
        if cursor.weekday() < 5:
            following_sessions.append(cursor)
        cursor += timedelta(days=1)
    return OfficialCalendarReceipt(
        receipt_id=f"official-calendar-{strategy_session.isoformat()}",
        adapter_id="controlled-official-calendar-adapter",
        adapter_version="v1",
        source_id="exchange-official-calendar-source",
        source_document_sha256="9" * 64,
        issued_at=snapshot.decision_at - timedelta(days=1),
        available_at=snapshot.decision_at - timedelta(days=1, minutes=1),
        trading_sessions=(
            *snapshot.trading_sessions,
            *following_sessions,
        ),
    )


def registry(
    decision_at: datetime, calendar_receipt: OfficialCalendarReceipt
) -> OfficialCalendarRegistry:
    return OfficialCalendarRegistry(
        registry_id="controlled-official-calendar-registry-v1",
        frozen_at=decision_at - timedelta(hours=1),
        entries=(CalendarRegistryEntry.from_receipt(calendar_receipt),),
    )


def frozen_data(snapshot) -> FrozenDailyDataV2:
    selected = snapshot
    complete = _inputs(selected.decision_at)
    metrics = tuple(
        item
        for item in complete.metrics
        if item.category
        not in {
            ExposureInputCategory.ALPHA_PREDICTION_DISTRIBUTION,
            ExposureInputCategory.ACCOUNT_DRAWDOWN,
        }
    )
    return FrozenDailyDataV2(
        update_id="controlled-update-20260818",
        alpha_snapshot=selected,
        non_alpha_exposure_metrics=metrics,
        instrument_rules=rules(selected.member_ids),
    )


class DailyPipelineIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._registry_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._registry_directory.cleanup)
        self._registry_patch = patch.object(
            next_session_signal_module,
            "NEXT_SESSION_REGISTRY_ROOT",
            Path(self._registry_directory.name) / "fixed-strategy-registry",
        )
        self._registry_patch.start()
        self.addCleanup(self._registry_patch.stop)
        self._publication_registry_patch = patch.object(
            daily_signal_publication_module,
            "DAILY_SIGNAL_PUBLICATION_REGISTRY_ROOT",
            Path(self._registry_directory.name) / "fixed-daily-publication-registry",
        )
        self._publication_registry_patch.start()
        self.addCleanup(self._publication_registry_patch.stop)

    def run_pipeline(
        self,
        folder: Path,
        *,
        snapshot=None,
        manual_pause=False,
        data_updater=None,
        account_override=None,
        paper_ledger_path=None,
        held_position_references=(),
        exposure_registry_root=None,
        publication_registry_root=None,
        strategy_date=STRATEGY_DATE,
        exposure_memory_override=None,
        governance_bundle_override=None,
    ):
        selected = snapshot or _snapshot()
        calendar_receipt = receipt(selected)
        selected = replace(
            selected,
            trading_calendar_receipt_sha256=calendar_receipt.receipt_sha256,
        )
        data = frozen_data(selected)
        held_position_references = tuple(held_position_references)
        if held_position_references:
            held_rules = rules(
                tuple(item.instrument_id for item in held_position_references)
            )
            data = FrozenDailyDataV2(
                update_id=data.update_id,
                alpha_snapshot=data.alpha_snapshot,
                non_alpha_exposure_metrics=data.non_alpha_exposure_metrics,
                instrument_rules={**data.instrument_rules, **held_rules},
                held_position_references=held_position_references,
            )
        updater = data_updater or StaticUpdater(data)
        (
            alpha_model,
            approved_factor_registry,
            experiment_v3_admission_receipt,
        ) = governance_bundle_override or _model_bundle()
        exposure_policy = admitted_exposure_policy(
            experiment_v3_admission_receipt
        )
        self.experiment_v3_admission_receipt = (
            experiment_v3_admission_receipt
        )
        controlled_registry = registry(
            data.alpha_snapshot.decision_at, calendar_receipt
        )
        account = account_override or AccountSnapshot(
            strategy_id=ADAPTIVE_STRATEGY_ID,
            cash=D("10000"),
            positions={},
            snapshot_id="strategy-close-20260818",
            as_of=data.alpha_snapshot.decision_at - timedelta(minutes=1),
        )
        selected_registry_root = (
            Path(exposure_registry_root)
            if exposure_registry_root is not None
            else folder / ".test-exposure-state-registry.v1"
        )
        selected_publication_root = (
            Path(publication_registry_root)
            if publication_registry_root is not None
            else folder / ".test-daily-publication-registry.v1"
        )
        publication_patch = patch.object(
            daily_signal_publication_module,
            "DAILY_SIGNAL_PUBLICATION_REGISTRY_ROOT",
            selected_publication_root,
        )
        publication_patch.start()
        self.addCleanup(publication_patch.stop)
        with patch(
            "operations.daily_pipeline.EXPOSURE_STATE_REGISTRY_ROOT",
            selected_registry_root,
        ):
            result = run_after_close_daily_pipeline(
                strategy_date=strategy_date,
                data_updater=updater,
                approved_factor_registry=approved_factor_registry,
                experiment_v3_admission_receipt=(
                    experiment_v3_admission_receipt
                ),
                alpha_model=alpha_model,
                exposure_policy=exposure_policy,
                exposure_memory=(
                    exposure_memory_override
                    if exposure_memory_override is not None
                    else _memory(exposure_policy)
                ),
                constructor_policy=constructor_policy(
                    data.alpha_snapshot.decision_at,
                    experiment_v3_admission_receipt,
                ),
                account=account,
                fees=fees(),
                calendar_receipt=calendar_receipt,
                calendar_registry=controlled_registry,
                output_directory=folder,
                signal_path=folder / "next-session-signal.json",
                paper_ledger_path=paper_ledger_path,
                manual_pause=manual_pause,
            )
        return result, updater, controlled_registry, account

    def run_risk_exit_pipeline(self, folder: Path):
        selected = _snapshot()
        previous_session = selected.trading_sessions[-2]
        seed_decision_date = selected.trading_sessions[-3]
        seed_intent = PortfolioIntent(
            intent_id="seed-position-before-risk-exit",
            strategy_id=ADAPTIVE_STRATEGY_ID,
            intent_type=PortfolioIntentType.ALPHA_REBALANCE,
            decision_at=datetime.combine(seed_decision_date, time(15, 5), TZ),
            available_at=datetime.combine(seed_decision_date, time(15, 0), TZ),
            frozen_at=datetime.combine(seed_decision_date, time(15, 4), TZ),
            target_gross_exposure=D("0.10"),
            target_weights={"000001.SZ": D("0.10")},
            reason_codes=("controlled_seed_position",),
            signal_sha256="1" * 64,
            market_data_sha256="2" * 64,
            model_sha256="3" * 64,
            risk_state_sha256="4" * 64,
        )
        self.seed_intent = seed_intent
        seed_cost_bundle = CanonicalExecutionCostBundleV1(
            fee_schedule=fees(),
            instrument_rules=rules(("000001.SZ",)),
        )
        seed_rule = seed_cost_bundle.instrument_rules["000001.SZ"]
        seed_attempt = PaperExecutionAttemptV2(
            attempt_id="controlled-seed-buy",
            intent_id=seed_intent.intent_id,
            intent_sha256=seed_intent.intent_sha256,
            instrument_id="000001.SZ",
            side="BUY",
            status="FILLED",
            requested_quantity=100,
            filled_quantity=100,
            execution_session=previous_session,
            attempted_at=datetime.combine(previous_session, time(9, 31), TZ),
            reference_open=D("10"),
            fill_price=D("10"),
            evidence_sha256="5" * 64,
            execution_cost_bundle_sha256=seed_cost_bundle.cost_bundle_sha256,
            commission_rate=fees().commission_rate,
            minimum_commission=fees().minimum_commission,
            sell_tax_rate=seed_rule.sell_stamp_duty_rate,
            transfer_fee_rate=fees().exchange_fee_rate,
        )
        seed_close_bundle = ControlledCloseMarkBundleV1.from_close_prices(
            session_date=previous_session,
            observed_at=datetime.combine(previous_session, time(15, 0), TZ),
            available_at=datetime.combine(previous_session, time(15, 1), TZ),
            source="controlled-seed-close",
            source_receipt_sha256="6" * 64,
            position_closes={"000001.SZ": (100, D("30"))},
        )
        seed_evidence = PaperCloseExecutionEvidenceV1(
            signal_id="controlled-seed-signal",
            signal_sha256="7" * 64,
            consumption_sha256="8" * 64,
            fill_bundle_sha256="9" * 64,
            frozen_execution_rule_bundle_sha256=(
                seed_cost_bundle.execution_rule_bundle_sha256
            ),
            review_execution_rule_bundle_sha256=(
                seed_cost_bundle.execution_rule_bundle_sha256
            ),
            execution_cost_bundle_sha256=seed_cost_bundle.cost_bundle_sha256,
            execution_intent_sha256=seed_intent.intent_sha256,
        )
        ledger_path = folder / "controlled-paper-ledger-v2.jsonl"
        with patch(
            "research.strategy_workspace.paper_ledger_v2._now",
            return_value=datetime.combine(previous_session, time(8), TZ),
        ):
            create_or_verify_paper_ledger_v2(
                ledger_path,
                strategy_id=ADAPTIVE_STRATEGY_ID,
                policy_schema_version=ADAPTIVE_POLICY_SCHEMA_VERSION,
                policy_sha256=FROZEN_ADAPTIVE_EXPOSURE_POLICY_SHA256,
                controlled_trading_dates=(
                    previous_session,
                    STRATEGY_DATE,
                    EXECUTION_DATE,
                    date(2026, 8, 20),
                ),
            )
        with patch(
            "research.strategy_workspace.paper_ledger_v2._now",
            return_value=datetime.combine(previous_session, time(15, 30), TZ),
        ):
            seeded = append_paper_daily_session_v2(
                ledger_path,
                PaperDailySessionDraftV2(
                    trading_date=previous_session,
                    execution_intent=seed_intent,
                    closing_intent=seed_intent,
                    attempts=(seed_attempt,),
                    execution_cost_bundle=seed_cost_bundle,
                    close_mark_bundle=seed_close_bundle,
                    execution_evidence=seed_evidence,
                ),
            )
        seed_cash = D(seeded.daily_sessions[-1]["cash"])
        account = AccountSnapshot(
            strategy_id=ADAPTIVE_STRATEGY_ID,
            cash=seed_cash,
            positions={
                "000001.SZ": Position(
                    instrument_id="000001.SZ",
                    quantity=100,
                    sellable_quantity=100,
                )
            },
            snapshot_id="strategy-close-risk-exit-20260818",
            as_of=selected.decision_at - timedelta(minutes=1),
        )
        return self.run_pipeline(
            folder,
            account_override=account,
            paper_ledger_path=ledger_path,
        )

    def append_strategy_date_bridge(self, folder: Path, run) -> None:
        cost_bundle = CanonicalExecutionCostBundleV1(
            fee_schedule=fees(),
            instrument_rules=run.frozen_data.instrument_rules,
        )
        reference_price = next(
            item.reference_price
            for item in run.construction.actions
            if item.instrument_id == "000001.SZ"
        )
        close_bundle = ControlledCloseMarkBundleV1.from_close_prices(
            session_date=STRATEGY_DATE,
            observed_at=datetime.combine(STRATEGY_DATE, time(15, 0), TZ),
            available_at=datetime.combine(STRATEGY_DATE, time(15, 1), TZ),
            source="controlled-strategy-date-close",
            source_receipt_sha256="a" * 64,
            position_closes={"000001.SZ": (100, reference_price)},
        )
        evidence = PaperCloseExecutionEvidenceV1(
            signal_id="controlled-strategy-date-bridge",
            signal_sha256="b" * 64,
            consumption_sha256="c" * 64,
            fill_bundle_sha256="d" * 64,
            frozen_execution_rule_bundle_sha256=(
                cost_bundle.execution_rule_bundle_sha256
            ),
            review_execution_rule_bundle_sha256=(
                cost_bundle.execution_rule_bundle_sha256
            ),
            execution_cost_bundle_sha256=cost_bundle.cost_bundle_sha256,
            execution_intent_sha256=self.seed_intent.intent_sha256,
        )
        with patch(
            "research.strategy_workspace.paper_ledger_v2._now",
            return_value=datetime.combine(STRATEGY_DATE, time(17, 0), TZ),
        ):
            append_paper_daily_session_v2(
                folder / "controlled-paper-ledger-v2.jsonl",
                PaperDailySessionDraftV2(
                    trading_date=STRATEGY_DATE,
                    execution_intent=self.seed_intent,
                    closing_intent=run.portfolio_intent,
                    attempts=(),
                    execution_cost_bundle=cost_bundle,
                    close_mark_bundle=close_bundle,
                    execution_evidence=evidence,
                ),
            )

    def test_data_updater_exception_still_writes_immutable_blocked_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            updater = RaisingUpdater()
            first, _, _, _ = self.run_pipeline(folder, data_updater=updater)
            self.assertIsInstance(first, DailyPipelineBlockedRunV1)
            self.assertEqual(first.daily_decision.decision_status, "BLOCKED")
            self.assertEqual(first.daily_decision.data_status, "DATA_UPDATE_FAILED")
            self.assertEqual(
                first.daily_decision.portfolio_intent_type,
                "RISK_OFF",
            )
            self.assertFalse(first.daily_decision.buy_orders)
            self.assertFalse(first.daily_decision.sell_orders)
            self.assertTrue(first.failure_receipt_path.is_file())
            self.assertFalse((folder / "next-session-signal.json").exists())
            original = first.artifacts.json_path.read_bytes()

            second, _, _, _ = self.run_pipeline(
                folder,
                data_updater=RaisingUpdater(),
            )
            self.assertIsInstance(second, DailyPipelineBlockedRunV1)
            self.assertEqual(original, second.artifacts.json_path.read_bytes())
            failure_text = second.failure_receipt_path.read_text(encoding="utf-8")
            self.assertNotIn("provider detail", failure_text)
            validate_json_schema(
                json.loads(failure_text),
                Path(__file__).resolve().parents[1]
                / "schemas"
                / "daily_pipeline_failure_receipt.v1.json",
            )

    def test_experiment_v3_cross_artifact_mismatch_is_blocked_without_signal(self) -> None:
        model, factor_registry, admitted = _model_bundle()
        mismatched = replace(
            admitted,
            receipt_id="daily-pipeline-mismatched-experiment",
            experiment_spec_sha256="7" * 64,
        )
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            blocked, _, _, _ = self.run_pipeline(
                folder,
                governance_bundle_override=(model, factor_registry, mismatched),
            )
            self.assertIsInstance(blocked, DailyPipelineBlockedRunV1)
            self.assertFalse(blocked.daily_decision.buy_orders)
            self.assertFalse(blocked.daily_decision.sell_orders)
            self.assertIsNone(blocked.signal_path)
            self.assertFalse((folder / "next-session-signal.json").exists())
            self.assertIn(
                "pipeline_validation_dailypipelineerror",
                blocked.daily_decision.failure_codes,
            )

    def test_self_hash_drifted_factor_registry_is_blocked_without_signal(self) -> None:
        model, factor_registry, admitted = _model_bundle()
        object.__setattr__(factor_registry, "registry_sha256", "0" * 64)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            blocked, _, _, _ = self.run_pipeline(
                folder,
                governance_bundle_override=(model, factor_registry, admitted),
            )
            self.assertIsInstance(blocked, DailyPipelineBlockedRunV1)
            self.assertFalse(blocked.daily_decision.buy_orders)
            self.assertFalse(blocked.daily_decision.sell_orders)
            self.assertIsNone(blocked.signal_path)
            self.assertFalse((folder / "next-session-signal.json").exists())
            self.assertNotEqual(
                blocked.daily_decision.data_sha256,
                blocked.daily_decision.failure_receipt_sha256,
            )

    def test_exposure_memory_must_match_the_preceding_immutable_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            registry_root = folder / ".test-exposure-state-registry.v1"
            first, _, _, _ = self.run_pipeline(folder)
            memory = first.exposure_decision.next_state_memory
            with patch(
                "operations.daily_pipeline.EXPOSURE_STATE_REGISTRY_ROOT",
                registry_root,
            ):
                _validate_exposure_memory_provenance(
                    output_directory=folder,
                    strategy_date=EXECUTION_DATE,
                    previous_session=STRATEGY_DATE,
                    memory=memory,
                )
            forged = ExposureStateMemoryV2(
                policy_sha256=memory.policy_sha256,
                current_state=memory.current_state,
                last_decision_at=memory.last_decision_at,
                last_input_snapshot_sha256="f" * 64,
            )
            with patch(
                "operations.daily_pipeline.EXPOSURE_STATE_REGISTRY_ROOT",
                registry_root,
            ), self.assertRaisesRegex(
                DailyPipelineError,
                "differs from preceding state artifact",
            ):
                _validate_exposure_memory_provenance(
                    output_directory=folder,
                    strategy_date=EXECUTION_DATE,
                    previous_session=STRATEGY_DATE,
                    memory=forged,
                )

    def test_exposure_bootstrap_cannot_replace_preceding_immutable_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            registry_root = folder / ".test-exposure-state-registry.v1"
            self.run_pipeline(folder)
            policy = admitted_exposure_policy(_model_bundle()[2])
            with patch(
                "operations.daily_pipeline.EXPOSURE_STATE_REGISTRY_ROOT",
                registry_root,
            ), self.assertRaisesRegex(
                DailyPipelineError,
                "bootstrap exposure memory is forbidden after prior controlled artifacts",
            ):
                _validate_exposure_memory_provenance(
                    output_directory=folder,
                    strategy_date=EXECUTION_DATE,
                    previous_session=STRATEGY_DATE,
                    memory=_memory(policy),
                )

    def test_switching_report_directory_cannot_reset_exposure_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_output = root / "first-output"
            second_output = root / "second-output"
            registry_root = root / "strategy-level-exposure-registry.v1"
            first_output.mkdir()
            second_output.mkdir()
            self.run_pipeline(
                first_output,
                exposure_registry_root=registry_root,
            )
            policy = admitted_exposure_policy(_model_bundle()[2])
            with patch(
                "operations.daily_pipeline.EXPOSURE_STATE_REGISTRY_ROOT",
                registry_root,
            ), self.assertRaisesRegex(
                DailyPipelineError,
                "bootstrap exposure memory is forbidden after prior controlled artifacts",
            ):
                _validate_exposure_memory_provenance(
                    output_directory=second_output,
                    strategy_date=EXECUTION_DATE,
                    previous_session=STRATEGY_DATE,
                    memory=_memory(policy),
                )

    def test_blocked_day_cannot_be_followed_by_a_fresh_exposure_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            registry_root = folder / ".test-exposure-state-registry.v1"
            blocked, _, _, _ = self.run_pipeline(
                folder,
                data_updater=RaisingUpdater(),
            )
            self.assertIsInstance(blocked, DailyPipelineBlockedRunV1)
            policy = admitted_exposure_policy(_model_bundle()[2])
            with patch(
                "operations.daily_pipeline.EXPOSURE_STATE_REGISTRY_ROOT",
                registry_root,
            ), self.assertRaisesRegex(
                DailyPipelineError,
                "bootstrap exposure memory is forbidden after prior controlled artifacts",
            ):
                _validate_exposure_memory_provenance(
                    output_directory=folder,
                    strategy_date=EXECUTION_DATE,
                    previous_session=STRATEGY_DATE,
                    memory=_memory(policy),
                )

    def test_blocked_day_persists_risk_off_memory_for_safe_next_day_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            registry_root = folder / ".test-exposure-state-registry.v1"
            blocked, _, _, _ = self.run_pipeline(
                folder,
                data_updater=RaisingUpdater(),
            )
            self.assertIsNotNone(blocked.exposure_inputs)
            self.assertIsNotNone(blocked.exposure_decision)
            failure_exposure = blocked.exposure_decision
            self.assertEqual(failure_exposure.state, ExposureState.RISK_OFF)
            self.assertEqual(failure_exposure.target_gross_exposure, 0.0)
            self.assertTrue(
                all(
                    item.source_snapshot_sha256
                    == blocked.daily_decision.failure_receipt_sha256
                    for item in blocked.exposure_inputs.metrics
                )
            )
            persisted_memory = failure_exposure.next_state_memory
            with patch(
                "operations.daily_pipeline.EXPOSURE_STATE_REGISTRY_ROOT",
                registry_root,
            ):
                _validate_exposure_memory_provenance(
                    output_directory=folder / "different-report-directory",
                    strategy_date=EXECUTION_DATE,
                    previous_session=STRATEGY_DATE,
                    memory=persisted_memory,
                )
            next_sessions = _sessions(EXECUTION_DATE)
            next_snapshot = replace(
                _snapshot(
                    instruments=(
                        _instrument_input("000001.SZ", next_sessions, scale=1.0),
                        _instrument_input("600000.SH", next_sessions, scale=1.3),
                    )
                ),
                decision_at=datetime(2026, 8, 19, 16, tzinfo=TZ),
                universe_as_of=EXECUTION_DATE,
                universe_available_at=datetime(2026, 8, 19, 9, tzinfo=TZ),
                universe_version="CSI800-PIT-20260819",
                trading_sessions=next_sessions,
                benchmark_price_bars=_bars(
                    "H00906.CSI",
                    next_sessions,
                    base=5000.0,
                    slope=2.0,
                ),
            )
            prior_ledger = VerifiedPaperLedgerV2(
                path=folder / "controlled-paper-ledger-v2.jsonl",
                header={
                    "strategy_id": ADAPTIVE_STRATEGY_ID,
                    "policy_sha256": FROZEN_ADAPTIVE_EXPOSURE_POLICY_SHA256,
                },
                daily_sessions=(
                    {
                        "trading_date": STRATEGY_DATE.isoformat(),
                        "peak_nav": "10000",
                    },
                ),
                last_record_sha256="a" * 64,
                file_sha256="b" * 64,
                byte_length=1,
            )
            with patch(
                "operations.daily_pipeline.verify_paper_ledger_v2",
                return_value=prior_ledger,
            ):
                continued, _, _, _ = self.run_pipeline(
                    folder,
                    snapshot=next_snapshot,
                    paper_ledger_path=prior_ledger.path,
                    exposure_registry_root=registry_root,
                    strategy_date=EXECUTION_DATE,
                    exposure_memory_override=persisted_memory,
                )
            self.assertNotIsInstance(continued, DailyPipelineBlockedRunV1)
            self.assertEqual(continued.exposure_decision.state, ExposureState.RISK_OFF)
            self.assertEqual(continued.exposure_decision.target_gross_exposure, 0.0)
            self.assertFalse(continued.daily_decision.buy_orders)
            self.assertFalse(continued.daily_decision.sell_orders)

    def test_missing_calendar_registry_writes_a_blocked_daily_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            selected = _snapshot()
            calendar_receipt = receipt(selected)
            selected = replace(
                selected,
                trading_calendar_receipt_sha256=calendar_receipt.receipt_sha256,
            )
            data = frozen_data(selected)
            (
                alpha_model,
                approved_factor_registry,
                experiment_v3_admission_receipt,
            ) = _model_bundle()
            policy = admitted_exposure_policy(
                experiment_v3_admission_receipt
            )
            account = AccountSnapshot(
                strategy_id=ADAPTIVE_STRATEGY_ID,
                cash=D("10000"),
                positions={},
                snapshot_id="strategy-close-missing-calendar-registry",
                as_of=selected.decision_at - timedelta(minutes=1),
            )
            with patch(
                "operations.daily_pipeline.EXPOSURE_STATE_REGISTRY_ROOT",
                folder / ".test-exposure-state-registry.v1",
            ):
                result = run_after_close_daily_pipeline(
                    strategy_date=STRATEGY_DATE,
                    data_updater=StaticUpdater(data),
                    approved_factor_registry=approved_factor_registry,
                    experiment_v3_admission_receipt=(
                        experiment_v3_admission_receipt
                    ),
                    alpha_model=alpha_model,
                    exposure_policy=policy,
                    exposure_memory=_memory(policy),
                    constructor_policy=constructor_policy(
                        selected.decision_at,
                        experiment_v3_admission_receipt,
                    ),
                    account=account,
                    fees=fees(),
                    calendar_receipt=calendar_receipt,
                    calendar_registry=None,  # type: ignore[arg-type]
                    output_directory=folder,
                    signal_path=folder / "next-session-signal.json",
                )
            self.assertIsInstance(result, DailyPipelineBlockedRunV1)
            self.assertEqual(result.daily_decision.decision_status, "BLOCKED")
            self.assertEqual(
                result.daily_decision.failed_stage,
                "PIPELINE_VALIDATION",
            )
            self.assertFalse(result.daily_decision.buy_orders)
            self.assertFalse((folder / "next-session-signal.json").exists())

    def test_account_drawdown_is_derived_from_verified_ledger_not_updater(self) -> None:
        selected = _snapshot()
        close = D(str(selected.instruments[0].price_bars[-1].close))
        account = AccountSnapshot(
            strategy_id=ADAPTIVE_STRATEGY_ID,
            cash=D("0"),
            positions={
                "000001.SZ": Position(
                    instrument_id="000001.SZ",
                    quantity=100,
                    sellable_quantity=100,
                )
            },
            snapshot_id="strategy-close-ledger-drawdown",
            as_of=selected.decision_at - timedelta(minutes=1),
        )
        with tempfile.TemporaryDirectory() as directory:
            blocked, _, _, _ = self.run_pipeline(
                Path(directory),
                account_override=account,
            )
        self.assertIsInstance(blocked, DailyPipelineBlockedRunV1)
        self.assertFalse(blocked.daily_decision.buy_orders)
        prior = VerifiedPaperLedgerV2(
            path=Path("controlled-paper-ledger-v2.jsonl"),
            header={
                "strategy_id": ADAPTIVE_STRATEGY_ID,
                "policy_sha256": FROZEN_ADAPTIVE_EXPOSURE_POLICY_SHA256,
            },
            daily_sessions=(
                {
                    "trading_date": selected.trading_sessions[-2].isoformat(),
                    "peak_nav": str(close * 200),
                },
            ),
            last_record_sha256="a" * 64,
            file_sha256="b" * 64,
            byte_length=1,
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "operations.daily_pipeline.verify_paper_ledger_v2",
            return_value=prior,
        ):
            result, _, _, _ = self.run_pipeline(
                Path(directory),
                account_override=account,
                paper_ledger_path=Path(directory) / "controlled-paper-ledger-v2.jsonl",
            )
        self.assertEqual(
            result.daily_decision.portfolio_intent_type,
            "ACCOUNT_DRAWDOWN_EXIT",
        )
        self.assertFalse(result.daily_decision.buy_orders)
        self.assertTrue(result.daily_decision.sell_orders)
        account_metric = result.exposure_inputs.by_category[
            ExposureInputCategory.ACCOUNT_DRAWDOWN
        ]
        self.assertGreaterEqual(account_metric.value, 0.12)
        self.assertNotIn(
            ExposureInputCategory.ACCOUNT_DRAWDOWN,
            {item.category for item in result.frozen_data.non_alpha_exposure_metrics},
        )

    def test_underfunded_flat_bootstrap_is_blocked_instead_of_resetting_peak_nav(self) -> None:
        selected = _snapshot()
        underfunded = AccountSnapshot(
            strategy_id=ADAPTIVE_STRATEGY_ID,
            cash=D("8000"),
            positions={},
            snapshot_id="strategy-close-underfunded-bootstrap",
            as_of=selected.decision_at - timedelta(minutes=1),
        )
        with tempfile.TemporaryDirectory() as directory:
            blocked, _, _, _ = self.run_pipeline(
                Path(directory),
                account_override=underfunded,
            )
        self.assertIsInstance(blocked, DailyPipelineBlockedRunV1)
        self.assertEqual(blocked.daily_decision.decision_status, "BLOCKED")
        self.assertEqual(
            blocked.daily_decision.portfolio_intent_type,
            "RISK_OFF",
        )
        self.assertFalse(blocked.daily_decision.buy_orders)
        self.assertFalse(blocked.daily_decision.sell_orders)

    def test_out_of_universe_strategy_holding_remains_exit_only(self) -> None:
        selected = _snapshot()
        held = ControlledHeldPositionReferenceV1(
            instrument_id="000002.SZ",
            session_date=STRATEGY_DATE,
            available_at=selected.decision_at,
            close=D("10"),
            source_record_sha256="c" * 64,
        )
        account = AccountSnapshot(
            strategy_id=ADAPTIVE_STRATEGY_ID,
            cash=D("9000"),
            positions={
                "000002.SZ": Position(
                    instrument_id="000002.SZ",
                    quantity=100,
                    sellable_quantity=100,
                )
            },
            snapshot_id="strategy-close-universe-exit",
            as_of=selected.decision_at - timedelta(minutes=1),
        )
        prior = VerifiedPaperLedgerV2(
            path=Path("controlled-paper-ledger-v2.jsonl"),
            header={
                "strategy_id": ADAPTIVE_STRATEGY_ID,
                "policy_sha256": FROZEN_ADAPTIVE_EXPOSURE_POLICY_SHA256,
            },
            daily_sessions=(
                {
                    "trading_date": selected.trading_sessions[-2].isoformat(),
                    "peak_nav": "12000",
                },
            ),
            last_record_sha256="d" * 64,
            file_sha256="e" * 64,
            byte_length=1,
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "operations.daily_pipeline.verify_paper_ledger_v2",
            return_value=prior,
        ):
            result, _, _, _ = self.run_pipeline(
                Path(directory),
                account_override=account,
                paper_ledger_path=Path(directory) / "controlled-paper-ledger-v2.jsonl",
                held_position_references=(held,),
            )
        held_actions = [
            item
            for item in result.construction.actions
            if item.instrument_id == "000002.SZ"
        ]
        self.assertEqual(len(held_actions), 1)
        self.assertNotEqual(held_actions[0].action.value, "BUY")
        self.assertIn(
            held_actions[0].action.value,
            {"SELL", "HOLD"},
        )

    def test_end_to_end_replay_preopen_manual_fill_and_paper_close(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            first, updater, controlled_registry, close_account = (
                self.run_risk_exit_pipeline(folder)
            )
            original_decision = first.artifacts.json_path.read_bytes()
            second, _, _, _ = self.run_pipeline(
                folder,
                account_override=close_account,
                paper_ledger_path=folder / "controlled-paper-ledger-v2.jsonl",
            )

            self.assertEqual(updater.calls, [STRATEGY_DATE])
            self.assertEqual(first.daily_decision.to_dict(), second.daily_decision.to_dict())
            self.assertEqual(original_decision, second.artifacts.json_path.read_bytes())
            self.assertEqual(first.alpha_ranking.ranking_sha256, second.alpha_ranking.ranking_sha256)
            self.assertEqual(first.next_session_signal.signal_sha256, second.next_session_signal.signal_sha256)
            self.append_strategy_date_bridge(folder, first)
            self.assertTrue(first.evidence_artifacts.alpha_ranking_path.is_file())
            self.assertTrue(
                first.evidence_artifacts.approved_factor_registry_path.is_file()
            )
            self.assertTrue(
                first.evidence_artifacts.experiment_v3_admission_receipt_path.is_file()
            )
            governance_evidence = json.loads(
                first.evidence_artifacts.experiment_v3_evidence_path.read_text(
                    encoding="utf-8"
                )
            )
            evidence_sha256 = governance_evidence.pop("evidence_sha256")
            self.assertEqual(
                evidence_sha256,
                first.evidence_artifacts.experiment_v3_evidence_sha256,
            )
            self.assertEqual(
                evidence_sha256,
                canonical_sha256(governance_evidence),
            )
            self.assertEqual(
                first.next_session_signal.experiment_admission_receipt_sha256,
                self.experiment_v3_admission_receipt.receipt_sha256,
            )
            self.assertFalse(first.daily_decision.to_dict()["automatic_order_submission"])

            trades = [
                item
                for item in first.construction.actions
                if item.action.value in {"BUY", "SELL"}
            ]
            self.assertTrue(trades)
            checked_at = datetime.combine(EXECUTION_DATE, time(9, 31), TZ)
            quotes = {
                item.instrument_id: MarketQuote(
                    instrument_id=item.instrument_id,
                    bid=item.reference_price,
                    ask=(item.reference_price * D("1.01")).quantize(D("0.01")),
                    last=item.reference_price,
                    as_of=checked_at - timedelta(minutes=1),
                )
                for item in trades
            }
            execution_account = replace(
                close_account,
                snapshot_id="strategy-open-20260819",
                as_of=checked_at - timedelta(minutes=1),
            )
            consumption_path = canonical_next_session_consumption_path(
                first.signal_path,
                first.next_session_signal.signal_sha256,
            )
            consumed = run_pre_open_review(
                first.signal_path,
                consumption_path,
                calendar_registry=controlled_registry,
                experiment_v3_admission_receipt=(
                    self.experiment_v3_admission_receipt
                ),
                daily_signal_publication_receipt=(
                    first.daily_signal_publication_receipt
                ),
                account=execution_account,
                quotes=quotes,
                fees=fees(),
                instrument_rules=first.frozen_data.instrument_rules,
                checked_at=checked_at,
            )
            self.assertEqual(consumed.status, "READY_FOR_MANUAL_EXECUTION")
            fill_bundle_path = canonical_manual_fill_bundle_path(
                consumed.consumption_sha256
            )
            with self.assertRaises(NextSessionAlreadyConsumed):
                run_pre_open_review(
                    first.signal_path,
                    consumption_path,
                    calendar_registry=controlled_registry,
                    experiment_v3_admission_receipt=(
                        self.experiment_v3_admission_receipt
                    ),
                    daily_signal_publication_receipt=(
                        first.daily_signal_publication_receipt
                    ),
                    account=execution_account,
                    quotes=quotes,
                    fees=fees(),
                    instrument_rules=first.frozen_data.instrument_rules,
                    checked_at=checked_at,
                )

            confirmations = tuple(
                ManualFillConfirmationV1(
                    instrument_id=item.instrument_id,
                    side=item.action,
                    status="FILLED",
                    filled_quantity=item.quantity,
                    attempted_at=checked_at,
                    reference_open=item.observed_execution_price,
                    fill_price=item.observed_execution_price,
                    evidence_sha256="e" * 64,
                )
                for item in consumed.instructions
                if item.status.value == "READY_FOR_MANUAL_EXECUTION"
            )
            forged_consumption = replace(
                consumed,
                cancel_reasons=("caller_forged_consumption",),
            )
            with self.assertRaisesRegex(DailyPipelineError, "caller consumption"):
                record_manual_fills(
                    fill_bundle_path,
                    batch_id="manual-open-forged-consumption-20260819",
                    consumption_path=consumption_path,
                    signal=first.next_session_signal,
                    consumption=forged_consumption,
                    intent=first.portfolio_intent,
                    experiment_v3_admission_receipt=(
                        self.experiment_v3_admission_receipt
                    ),
                    daily_signal_publication_receipt=(
                        first.daily_signal_publication_receipt
                    ),
                    calendar_registry=controlled_registry,
                    fees=fees(),
                    instrument_rules=first.frozen_data.instrument_rules,
                    confirmations=confirmations,
                )
            attacked_confirmation = replace(
                confirmations[0],
                reference_open=(
                    first.next_session_signal.frozen_reference_prices[
                        confirmations[0].instrument_id
                    ]
                    * D("1.03")
                ),
                fill_price=(
                    first.next_session_signal.frozen_reference_prices[
                        confirmations[0].instrument_id
                    ]
                    * D("1.03")
                ),
            )
            with self.assertRaisesRegex(DailyPipelineError, "reviewed quote"):
                record_manual_fills(
                    fill_bundle_path,
                    batch_id="manual-open-attacked-20260819",
                    consumption_path=consumption_path,
                    signal=first.next_session_signal,
                    consumption=consumed,
                    intent=first.portfolio_intent,
                    experiment_v3_admission_receipt=(
                        self.experiment_v3_admission_receipt
                    ),
                    daily_signal_publication_receipt=(
                        first.daily_signal_publication_receipt
                    ),
                    calendar_registry=controlled_registry,
                    fees=fees(),
                    instrument_rules=first.frozen_data.instrument_rules,
                    confirmations=(attacked_confirmation, *confirmations[1:]),
                )
            fill_bundle = record_manual_fills(
                fill_bundle_path,
                batch_id="manual-open-20260819",
                consumption_path=consumption_path,
                signal=first.next_session_signal,
                consumption=consumed,
                intent=first.portfolio_intent,
                experiment_v3_admission_receipt=(
                    self.experiment_v3_admission_receipt
                ),
                daily_signal_publication_receipt=(
                    first.daily_signal_publication_receipt
                ),
                calendar_registry=controlled_registry,
                fees=fees(),
                instrument_rules=first.frozen_data.instrument_rules,
                confirmations=confirmations,
            )
            self.assertTrue(fill_bundle.attempts)
            self.assertEqual(
                fill_bundle.review_execution_rule_bundle_sha256,
                consumed.execution_rule_bundle_sha256,
            )
            self.assertEqual(
                fill_bundle.frozen_execution_rule_bundle_sha256,
                first.next_session_signal.execution_rule_bundle_sha256,
            )

            mismatched_reference = replace(
                confirmations[0],
                reference_open=confirmations[0].reference_open - D("0.01"),
            )
            with self.assertRaisesRegex(DailyPipelineError, "reviewed quote"):
                record_manual_fills(
                    fill_bundle_path,
                    batch_id="manual-open-reference-attack-20260819",
                    consumption_path=consumption_path,
                    signal=first.next_session_signal,
                    consumption=consumed,
                    intent=first.portfolio_intent,
                    experiment_v3_admission_receipt=(
                        self.experiment_v3_admission_receipt
                    ),
                    daily_signal_publication_receipt=(
                        first.daily_signal_publication_receipt
                    ),
                    calendar_registry=controlled_registry,
                    fees=fees(),
                    instrument_rules=first.frozen_data.instrument_rules,
                    confirmations=(mismatched_reference, *confirmations[1:]),
                )

            pre_review_attempt = replace(
                confirmations[0],
                attempted_at=checked_at - timedelta(seconds=1),
            )
            with self.assertRaisesRegex(DailyPipelineError, "predates"):
                record_manual_fills(
                    fill_bundle_path,
                    batch_id="manual-open-time-attack-20260819",
                    consumption_path=consumption_path,
                    signal=first.next_session_signal,
                    consumption=consumed,
                    intent=first.portfolio_intent,
                    experiment_v3_admission_receipt=(
                        self.experiment_v3_admission_receipt
                    ),
                    daily_signal_publication_receipt=(
                        first.daily_signal_publication_receipt
                    ),
                    calendar_registry=controlled_registry,
                    fees=fees(),
                    instrument_rules=first.frozen_data.instrument_rules,
                    confirmations=(pre_review_attempt, *confirmations[1:]),
                )

            with self.assertRaisesRegex(DailyPipelineError, "canonical"):
                record_manual_fills(
                    folder / "second-fill-bundle.json",
                    batch_id="manual-open-second-path-20260819",
                    consumption_path=consumption_path,
                    signal=first.next_session_signal,
                    consumption=consumed,
                    intent=first.portfolio_intent,
                    experiment_v3_admission_receipt=(
                        self.experiment_v3_admission_receipt
                    ),
                    daily_signal_publication_receipt=(
                        first.daily_signal_publication_receipt
                    ),
                    calendar_registry=controlled_registry,
                    fees=fees(),
                    instrument_rules=first.frozen_data.instrument_rules,
                    confirmations=confirmations,
                )
            with self.assertRaisesRegex(DailyPipelineError, "already recorded"):
                record_manual_fills(
                    fill_bundle_path,
                    batch_id="manual-open-second-cas-20260819",
                    consumption_path=consumption_path,
                    signal=first.next_session_signal,
                    consumption=consumed,
                    intent=first.portfolio_intent,
                    experiment_v3_admission_receipt=(
                        self.experiment_v3_admission_receipt
                    ),
                    daily_signal_publication_receipt=(
                        first.daily_signal_publication_receipt
                    ),
                    calendar_registry=controlled_registry,
                    fees=fees(),
                    instrument_rules=first.frozen_data.instrument_rules,
                    confirmations=confirmations,
                )

            ledger_path = folder / "controlled-paper-ledger-v2.jsonl"
            self.assertTrue(fill_bundle.attempts)
            self.assertTrue(
                all(item.side == "SELL" for item in fill_bundle.attempts)
            )
            cost_bundle = CanonicalExecutionCostBundleV1(
                fee_schedule=fees(),
                instrument_rules=first.frozen_data.instrument_rules,
            )
            close_marks = ControlledCloseMarkBundleV1.from_close_prices(
                session_date=EXECUTION_DATE,
                observed_at=datetime.combine(EXECUTION_DATE, time(15, 0), TZ),
                available_at=datetime.combine(EXECUTION_DATE, time(15, 1), TZ),
                source="controlled-close-test-adapter",
                source_receipt_sha256="f" * 64,
                position_closes={},
            )
            forged_fill_bundle = replace(
                fill_bundle,
                signal_id="forged-signal-id",
            )
            with self.assertRaisesRegex(
                DailyPipelineError,
                "exact immutable canonical manual fill bundle",
            ):
                append_close_paper_ledger_v2(
                    ledger_path,
                    trading_date=EXECUTION_DATE,
                    execution_intent=first.portfolio_intent,
                    closing_intent=first.portfolio_intent,
                    next_session_signal=first.next_session_signal,
                    experiment_v3_admission_receipt=(
                        self.experiment_v3_admission_receipt
                    ),
                    fill_bundle=forged_fill_bundle,
                    execution_cost_bundle=cost_bundle,
                    close_mark_bundle=close_marks,
                )
            with patch(
                "research.strategy_workspace.paper_ledger_v2._now",
                return_value=datetime.combine(EXECUTION_DATE, time(15, 5), TZ),
            ):
                verified = append_close_paper_ledger_v2(
                    ledger_path,
                    trading_date=EXECUTION_DATE,
                    execution_intent=first.portfolio_intent,
                    closing_intent=first.portfolio_intent,
                    next_session_signal=first.next_session_signal,
                    experiment_v3_admission_receipt=(
                        self.experiment_v3_admission_receipt
                    ),
                    fill_bundle=fill_bundle,
                    execution_cost_bundle=cost_bundle,
                    close_mark_bundle=close_marks,
                )
            self.assertEqual(len(verified.daily_sessions), 3)
            self.assertGreater(
                D(verified.daily_sessions[-1]["session_transaction_cost"]), D("0")
            )
            self.assertEqual(
                verified.daily_sessions[-1]["feasible_gross_exposure"],
                "0.00000000",
            )
            self.assertNotEqual(
                verified.daily_sessions[-1]["execution_evidence_bundle_sha256"],
                "1" * 64,
            )
            self.assertEqual(
                verified.daily_sessions[-1]["execution_evidence_bundle_sha256"],
                canonical_sha256(
                    {
                        "schema_version": "paper-close-execution-evidence.v1",
                        "signal_id": fill_bundle.signal_id,
                        "signal_sha256": fill_bundle.signal_sha256,
                        "consumption_sha256": fill_bundle.consumption_sha256,
                        "fill_bundle_sha256": fill_bundle.fill_bundle_sha256,
                        "frozen_execution_rule_bundle_sha256": (
                            fill_bundle.frozen_execution_rule_bundle_sha256
                        ),
                        "review_execution_rule_bundle_sha256": (
                            fill_bundle.review_execution_rule_bundle_sha256
                        ),
                        "execution_cost_bundle_sha256": (
                            cost_bundle.cost_bundle_sha256
                        ),
                        "execution_intent_sha256": (
                            first.portfolio_intent.intent_sha256
                        ),
                    }
                ),
            )

    def test_self_hashed_expanded_consumption_in_canonical_slot_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            run, _, controlled_registry, close_account = (
                self.run_risk_exit_pipeline(folder)
            )
            self.append_strategy_date_bridge(folder, run)
            checked_at = datetime.combine(EXECUTION_DATE, time(9, 31), TZ)
            trades = [
                item
                for item in run.construction.actions
                if item.action.value in {"BUY", "SELL"}
            ]
            quotes = {
                item.instrument_id: MarketQuote(
                    instrument_id=item.instrument_id,
                    bid=item.reference_price,
                    ask=(item.reference_price * D("1.01")).quantize(D("0.01")),
                    last=item.reference_price,
                    as_of=checked_at - timedelta(minutes=1),
                )
                for item in trades
            }
            execution_account = replace(
                close_account,
                snapshot_id="strategy-open-forged-consumption-20260819",
                as_of=checked_at - timedelta(minutes=1),
            )
            legitimate = next_session_signal_module._preflight(
                run.next_session_signal,
                experiment_v3_admission_receipt=(
                    self.experiment_v3_admission_receipt
                ),
                daily_signal_publication_receipt=(
                    run.daily_signal_publication_receipt
                ),
                registry=controlled_registry,
                account=execution_account,
                quotes=quotes,
                fees=fees(),
                instrument_rules=run.frozen_data.instrument_rules,
                checked_at=checked_at,
            )
            trade_index = next(
                index
                for index, item in enumerate(legitimate.instructions)
                if item.action in {"BUY", "SELL"}
            )
            forged_instructions = list(legitimate.instructions)
            self.assertLess(forged_instructions[trade_index].quantity, 10000)
            forged_instructions[trade_index] = replace(
                forged_instructions[trade_index],
                quantity=10000,
            )
            forged = replace(
                legitimate,
                instructions=tuple(forged_instructions),
            )
            consumption_path = canonical_next_session_consumption_path(
                run.signal_path,
                run.next_session_signal.signal_sha256,
            )
            consumption_path.write_bytes(
                canonical_json_bytes(forged.to_dict()) + b"\n"
            )

            with self.assertRaisesRegex(
                DailyPipelineError,
                "exact immutable one-shot consumption",
            ):
                record_manual_fills(
                    canonical_manual_fill_bundle_path(
                        forged.consumption_sha256
                    ),
                    batch_id="forged-expanded-quantity-20260819",
                    consumption_path=consumption_path,
                    signal=run.next_session_signal,
                    consumption=forged,
                    intent=run.portfolio_intent,
                    experiment_v3_admission_receipt=(
                        self.experiment_v3_admission_receipt
                    ),
                    daily_signal_publication_receipt=(
                        run.daily_signal_publication_receipt
                    ),
                    calendar_registry=controlled_registry,
                    fees=fees(),
                    instrument_rules=run.frozen_data.instrument_rules,
                    confirmations=(),
                )

    def test_rule_drift_cancellation_cannot_silently_close_without_exit_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            run, _, controlled_registry, close_account = (
                self.run_risk_exit_pipeline(folder)
            )
            self.append_strategy_date_bridge(folder, run)
            checked_at = datetime.combine(EXECUTION_DATE, time(9, 31), TZ)
            trades = [
                item
                for item in run.construction.actions
                if item.action.value in {"BUY", "SELL"}
            ]
            quotes = {
                item.instrument_id: MarketQuote(
                    instrument_id=item.instrument_id,
                    bid=item.reference_price,
                    ask=item.reference_price,
                    last=item.reference_price,
                    as_of=checked_at - timedelta(minutes=1),
                )
                for item in trades
            }
            execution_account = replace(
                close_account,
                snapshot_id="strategy-open-rule-drift-20260819",
                as_of=checked_at - timedelta(minutes=1),
            )
            drifted_rules = {
                instrument_id: replace(rule, lot_size=300)
                for instrument_id, rule in run.frozen_data.instrument_rules.items()
            }
            consumption_path = canonical_next_session_consumption_path(
                run.signal_path,
                run.next_session_signal.signal_sha256,
            )
            consumed = run_pre_open_review(
                run.signal_path,
                consumption_path,
                calendar_registry=controlled_registry,
                experiment_v3_admission_receipt=(
                    self.experiment_v3_admission_receipt
                ),
                daily_signal_publication_receipt=(
                    run.daily_signal_publication_receipt
                ),
                account=execution_account,
                quotes=quotes,
                fees=fees(),
                instrument_rules=drifted_rules,
                checked_at=checked_at,
            )
            self.assertEqual(consumed.status, "CANCELED")
            self.assertIn(
                "execution_rule_bundle_mismatch",
                consumed.cancel_reasons,
            )
            fill_bundle = record_manual_fills(
                canonical_manual_fill_bundle_path(
                    consumed.consumption_sha256
                ),
                batch_id="manual-open-rule-drift-20260819",
                consumption_path=consumption_path,
                signal=run.next_session_signal,
                consumption=consumed,
                intent=run.portfolio_intent,
                experiment_v3_admission_receipt=(
                    self.experiment_v3_admission_receipt
                ),
                daily_signal_publication_receipt=(
                    run.daily_signal_publication_receipt
                ),
                calendar_registry=controlled_registry,
                fees=fees(),
                instrument_rules=drifted_rules,
                confirmations=(),
            )
            self.assertEqual(fill_bundle.attempts, ())
            self.assertNotEqual(
                fill_bundle.frozen_execution_rule_bundle_sha256,
                fill_bundle.review_execution_rule_bundle_sha256,
            )

            ledger_path = folder / "controlled-paper-ledger-v2.jsonl"
            with patch(
                "research.strategy_workspace.paper_ledger_v2._now",
                return_value=datetime.combine(EXECUTION_DATE, time(15, 5), TZ),
            ):
                cost_bundle = CanonicalExecutionCostBundleV1(
                    fee_schedule=fees(),
                    instrument_rules=drifted_rules,
                )
                close_marks = ControlledCloseMarkBundleV1.from_close_prices(
                    session_date=EXECUTION_DATE,
                    observed_at=datetime.combine(EXECUTION_DATE, time(15, 0), TZ),
                    available_at=datetime.combine(EXECUTION_DATE, time(15, 1), TZ),
                    source="controlled-close-test-adapter",
                    source_receipt_sha256="f" * 64,
                    position_closes={
                        "000001.SZ": (100, quotes["000001.SZ"].last),
                    },
                )
                with self.assertRaisesRegex(
                    PaperLedgerV2Error,
                    "every residual position requires exactly one daily exit retry",
                ):
                    append_close_paper_ledger_v2(
                        ledger_path,
                        trading_date=EXECUTION_DATE,
                        execution_intent=run.portfolio_intent,
                        closing_intent=run.portfolio_intent,
                        next_session_signal=run.next_session_signal,
                        experiment_v3_admission_receipt=(
                            self.experiment_v3_admission_receipt
                        ),
                        fill_bundle=fill_bundle,
                        execution_cost_bundle=cost_bundle,
                        close_mark_bundle=close_marks,
                    )

    def test_future_input_fails_closed_and_creates_no_buy(self) -> None:
        base = _snapshot()
        attacked_input = base.instruments[0]
        bars = list(attacked_input.price_bars)
        bars[-1] = replace(
            bars[-1], available_at=base.decision_at + timedelta(seconds=1)
        )
        attacked = replace(
            base,
            instruments=(replace(attacked_input, price_bars=tuple(bars)), base.instruments[1]),
        )
        held_account = AccountSnapshot(
            strategy_id=ADAPTIVE_STRATEGY_ID,
            cash=D("1000"),
            positions={
                attacked_input.instrument_id: Position(
                    instrument_id=attacked_input.instrument_id,
                    quantity=100,
                    sellable_quantity=100,
                )
            },
            snapshot_id="strategy-close-future-mark",
            as_of=base.decision_at - timedelta(minutes=1),
        )
        with tempfile.TemporaryDirectory() as directory:
            blocked, _, _, _ = self.run_pipeline(
                Path(directory),
                snapshot=attacked,
                account_override=held_account,
            )
        self.assertIsInstance(blocked, DailyPipelineBlockedRunV1)
        self.assertFalse(blocked.daily_decision.buy_orders)
        with tempfile.TemporaryDirectory() as directory:
            result, _, _, _ = self.run_pipeline(Path(directory), snapshot=attacked)
        self.assertEqual(result.alpha_ranking.status.value, "DATA_FAIL_CLOSED")
        self.assertEqual(result.daily_decision.decision_status, "DATA_FAIL_CLOSED")
        self.assertEqual(result.daily_decision.portfolio_intent_type, "RISK_OFF")
        self.assertFalse(result.daily_decision.buy_orders)

        with tempfile.TemporaryDirectory() as directory:
            paused, _, _, _ = self.run_pipeline(
                Path(directory), snapshot=attacked, manual_pause=True
            )
        self.assertEqual(paused.daily_decision.portfolio_intent_type, "RISK_OFF")
        self.assertNotEqual(paused.daily_decision.decision_status, "MANUAL_PAUSE")
        self.assertFalse(paused.daily_decision.buy_orders)

    def test_pipeline_rejects_a_pre_close_or_non_cst_decision_boundary(self) -> None:
        pre_close = replace(
            _snapshot(),
            decision_at=datetime.combine(
                STRATEGY_DATE,
                time(14, 59),
                TZ,
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            blocked, _, _, _ = self.run_pipeline(
                Path(directory), snapshot=pre_close
            )
        self.assertIsInstance(blocked, DailyPipelineBlockedRunV1)
        self.assertEqual(blocked.daily_decision.failed_stage, "PIPELINE_VALIDATION")
        self.assertFalse(blocked.daily_decision.buy_orders)

    def test_missing_pit_row_is_excluded_and_all_missing_becomes_no_alpha_cash(self) -> None:
        sessions = _sessions(STRATEGY_DATE)
        missing = _instrument_input(
            "000001.SZ", sessions, scale=1.0, missing_revenue=True
        )
        valid = _instrument_input("600000.SH", sessions, scale=1.2)
        one_missing = _snapshot(instruments=(missing, valid))
        with tempfile.TemporaryDirectory() as directory:
            admitted, _, _, _ = self.run_pipeline(
                Path(directory), snapshot=one_missing
            )
        excluded = next(
            item
            for item in admitted.alpha_ranking.rows
            if item.instrument_id == "000001.SZ"
        )
        self.assertFalse(excluded.eligibility)
        self.assertIsNone(excluded.predicted_return)
        self.assertNotIn("000001.SZ", admitted.daily_decision.target_stock_weights)

        both_missing = _snapshot(
            instruments=(
                missing,
                _instrument_input(
                    "600000.SH", sessions, scale=1.2, missing_revenue=True
                ),
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            cash, _, _, _ = self.run_pipeline(Path(directory), snapshot=both_missing)
        self.assertEqual(cash.alpha_ranking.status.value, "DATA_FAIL_CLOSED")
        self.assertEqual(cash.daily_decision.portfolio_intent_type, "RISK_OFF")
        self.assertEqual(
            cash.next_session_signal.channel.value,
            "RISK_REDUCTION_NEXT_SESSION",
        )
        self.assertEqual(cash.daily_decision.cash_weight, D("1.00000000"))
        self.assertFalse(cash.daily_decision.buy_orders)
        self.assertFalse(cash.daily_decision.sell_orders)


if __name__ == "__main__":
    unittest.main()
