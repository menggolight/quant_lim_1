from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

import research.strategy_workspace.next_session_signal as next_session_signal_module
import research.strategy_workspace.daily_signal_publication as daily_signal_publication_module

from operations.daily_pipeline import DailyOrderV1, DailyStrategyDecisionV2
from trading.costs import FeeSchedule
from trading.integrity import account_fingerprint, execution_rule_bundle_sha256
from trading.models import (
    AccountSnapshot,
    InstrumentRule,
    MarketQuote,
    PortfolioIntent,
    PortfolioIntentType,
    Position,
)

from research.strategy_workspace.next_session_signal import (
    CalendarRegistryEntry,
    NextSessionAlreadyConsumed,
    NextSessionChannel,
    NextSessionSignal,
    NextSessionSignalConflict,
    NextSessionSignalError,
    OfficialCalendarReceipt,
    OfficialCalendarRegistry,
    canonical_next_session_consumption_path,
    consume_next_session_signal as _consume_next_session_signal,
    create_alpha_next_session_signal,
    create_risk_next_session_signal,
    read_next_session_signal as _read_next_session_signal,
    write_new_next_session_signal as _write_new_next_session_signal,
)
from research.strategy_workspace.contracts import canonical_json_bytes, canonical_sha256
from research.strategy_workspace.daily_signal_publication import (
    DailySignalAdmissionReceiptV1,
    DailySignalAuthority,
    DailySignalPublicationError,
    DailySignalPublicationReceiptV1,
    _publish_daily_signal_bundle_from_daily_pipeline,
    validate_daily_signal_publication_contract,
)
from research.market_data.validation import validate_json_schema
from research.strategy_workspace.experiment_v3_admission import (
    ExperimentV3AdmissionReceiptV1,
)
from research.strategy_workspace.exposure_engine_v2 import (
    ExposureDecisionV2,
    ExposureState,
    ExposureStateMemoryV2,
    ExposureTransitionStatus,
)
from research.strategy_workspace.portfolio_constructor_v2 import (
    ConstructorCostPolicy,
    CurrentPosition,
    PortfolioConstructorPolicy,
    PortfolioInstrument,
    construct_portfolio,
)


D = Decimal
TZ = timezone(timedelta(hours=8))
STRATEGY_ID = "a-share-small-account-adaptive-exposure-v2"
DECISION = datetime(2026, 8, 21, 15, 5, tzinfo=TZ)
EXECUTION_DATE = date(2026, 8, 24)
CHECKED = datetime(2026, 8, 24, 9, 31, tzinfo=TZ)
EXPERIMENT_SPEC_SHA256 = "b" * 64
EXPOSURE_POLICY_SOURCE_SHA256 = "a" * 64
CONSTRUCTOR_POLICY_SOURCE_SHA256 = "c" * 64
FACTOR_REGISTRY_CONTENT = {
    "schema_version": "test-approved-factor-registry.v1",
    "approved_factor_ids": [],
}
FACTOR_REGISTRY_SHA256 = canonical_sha256(FACTOR_REGISTRY_CONTENT)
MODEL_CONTENT = {
    "schema_version": "test-frozen-alpha-model.v2",
    "approved_factor_registry_sha256": FACTOR_REGISTRY_SHA256,
    "model_admission_receipt_sha256": "f" * 64,
}
MODEL_SHA256 = canonical_sha256(MODEL_CONTENT)
EXPOSURE_POLICY_CONTENT = {
    "schema_version": "test-exposure-hysteresis-policy.v2",
    "policy_id": "test-risk-off-policy",
}
EXPOSURE_POLICY_SHA256 = canonical_sha256(EXPOSURE_POLICY_CONTENT)


def frozen_exposure_decision(
    state: ExposureState = ExposureState.RISK_OFF,
) -> ExposureDecisionV2:
    next_memory = ExposureStateMemoryV2(
        policy_sha256=EXPOSURE_POLICY_SHA256,
        current_state=state,
        last_decision_at=DECISION,
        last_input_snapshot_sha256="2" * 64,
    )
    target = {
        ExposureState.RISK_OFF: 0.0,
        ExposureState.DEFENSIVE: 0.30,
        ExposureState.NEUTRAL: 0.60,
        ExposureState.RISK_ON: 1.0,
    }[state]
    return ExposureDecisionV2(
        decision_at=DECISION,
        previous_state=ExposureState.RISK_ON,
        candidate_state=state,
        state=state,
        target_gross_exposure=target,
        transition_status=(
            ExposureTransitionStatus.IMMEDIATE_RISK_OFF
            if state is ExposureState.RISK_OFF
            else ExposureTransitionStatus.STATE_CHANGED
        ),
        pending_state=None,
        pending_consecutive_sessions=0,
        reason_codes=("CONTROLLED_TEST_EXPOSURE",),
        input_snapshot_sha256="2" * 64,
        policy_sha256=EXPOSURE_POLICY_SHA256,
        previous_state_sha256="3" * 64,
        next_state_memory=next_memory,
    )


RISK_EXPOSURE_DECISION = frozen_exposure_decision()
EXPOSURE_STATE_CONTENT = RISK_EXPOSURE_DECISION.next_state_memory.to_content_dict()
EXPOSURE_STATE_SHA256 = RISK_EXPOSURE_DECISION.state_sha256


def admission_receipt(*, approved_factor_registry_sha256: str = FACTOR_REGISTRY_SHA256):
    issued_at = DECISION - timedelta(hours=1)
    return ExperimentV3AdmissionReceiptV1(
        receipt_id="next-session-policy-test",
        issued_at=issued_at,
        experiment_spec_sha256=EXPERIMENT_SPEC_SHA256,
        approved_factor_registry_sha256=approved_factor_registry_sha256,
        approved_factor_registry_frozen_at=issued_at - timedelta(days=1),
        model_training_receipt_sha256="e" * 64,
        model_admission_receipt_sha256="f" * 64,
        model_sha256=MODEL_SHA256,
        model_frozen_at=issued_at - timedelta(days=1),
        calibration_receipt_sha256="1" * 64,
        calibration_horizon_sessions=20,
        exposure_policy_source_sha256=EXPOSURE_POLICY_SOURCE_SHA256,
        exposure_policy_frozen_at=issued_at - timedelta(days=1),
        constructor_policy_source_sha256=CONSTRUCTOR_POLICY_SOURCE_SHA256,
        constructor_policy_frozen_at=DECISION - timedelta(days=1),
    )


def write_new_next_session_signal(path, signal):
    return _write_new_next_session_signal(
        path,
        signal,
        registry=registry(receipt()),
        experiment_v3_admission_receipt=admission_receipt(),
    )


def read_next_session_signal(path, *, registry):
    return _read_next_session_signal(
        path,
        registry=registry,
        experiment_v3_admission_receipt=admission_receipt(),
    )


def consume_next_session_signal(signal_path, consumption_path, **kwargs):
    return _consume_next_session_signal(
        signal_path,
        consumption_path,
        experiment_v3_admission_receipt=admission_receipt(),
        **kwargs,
    )


def policy() -> PortfolioConstructorPolicy:
    return PortfolioConstructorPolicy(
        policy_id="next-session-policy-v2",
        frozen_at=DECISION - timedelta(days=1),
        policy_source_sha256=CONSTRUCTOR_POLICY_SOURCE_SHA256,
        experiment_spec_sha256=EXPERIMENT_SPEC_SHA256,
        policy_admission_receipt=admission_receipt(),
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


def row(instrument_id: str = "000001.SZ", *, predicted="0.10", percentile="0.99") -> PortfolioInstrument:
    return PortfolioInstrument(
        instrument_id=instrument_id,
        predicted_return=D(predicted),
        percentile=D(percentile),
        eligibility=True,
        exclusion_codes=(),
        reference_price=D("10"),
        lot_size=100,
    )


def canonical_fees() -> FeeSchedule:
    return FeeSchedule(
        commission_rate=D("0.00018"),
        minimum_commission=D("5"),
        exchange_fee_rate=D("0.00001"),
    )


def canonical_rules(*, lot_size: int = 100) -> dict[str, InstrumentRule]:
    return {
        "000001.SZ": InstrumentRule(
            instrument_id="000001.SZ",
            name="controlled-test-instrument",
            instrument_type="stock",
            lot_size=lot_size,
            tick_size=D("0.01"),
            sell_stamp_duty_rate=D("0.0005"),
            t_plus_one=True,
        )
    }


def receipt() -> OfficialCalendarReceipt:
    return OfficialCalendarReceipt(
        receipt_id="sse-szse-calendar-202608",
        adapter_id="official-calendar-adapter",
        adapter_version="v1",
        source_id="exchange-calendar-source",
        source_document_sha256="9" * 64,
        issued_at=DECISION - timedelta(days=2),
        available_at=DECISION - timedelta(days=2, minutes=1),
        trading_sessions=(date(2026, 8, 20), DECISION.date(), EXECUTION_DATE, date(2026, 8, 25)),
    )


def registry(calendar_receipt: OfficialCalendarReceipt) -> OfficialCalendarRegistry:
    return OfficialCalendarRegistry(
        registry_id="controlled-calendar-registry-v1",
        frozen_at=DECISION - timedelta(days=1),
        entries=(CalendarRegistryEntry.from_receipt(calendar_receipt),),
    )


def build_alpha():
    selected_policy = policy()
    construction = construct_portfolio(
        decision_at=DECISION,
        requested_intent_type=PortfolioIntentType.ALPHA_REBALANCE,
        target_gross_exposure=D("0.30"),
        current_cash=D("10000"),
        current_positions=(),
        instruments=(row(),),
        policy=selected_policy,
        input_snapshot_sha256="1" * 64,
        model_sha256=MODEL_SHA256,
    )
    intent = PortfolioIntent(
        intent_id="alpha-close-20260821",
        strategy_id=STRATEGY_ID,
        intent_type=construction.intent_type,
        decision_at=DECISION,
        available_at=DECISION - timedelta(minutes=10),
        frozen_at=DECISION - timedelta(minutes=1),
        target_gross_exposure=construction.target_gross_exposure,
        target_weights=construction.feasible_stock_weights,
        reason_codes=("alpha_next_session",),
        signal_sha256=construction.construction_sha256,
        market_data_sha256=construction.input_snapshot_sha256,
        model_sha256=construction.model_sha256,
        risk_state_sha256="4" * 64,
    )
    return selected_policy, construction, intent


def build_risk():
    selected_policy = policy()
    construction = construct_portfolio(
        decision_at=DECISION,
        requested_intent_type=PortfolioIntentType.RISK_OFF,
        target_gross_exposure=D("0"),
        current_cash=D("9000"),
        current_positions=(CurrentPosition("000001.SZ", 100),),
        instruments=(row(),),
        policy=selected_policy,
        input_snapshot_sha256="1" * 64,
        model_sha256=MODEL_SHA256,
    )
    intent = PortfolioIntent(
        intent_id="risk-close-20260821",
        strategy_id=STRATEGY_ID,
        intent_type=construction.intent_type,
        decision_at=DECISION,
        available_at=DECISION - timedelta(minutes=10),
        frozen_at=DECISION - timedelta(minutes=1),
        target_gross_exposure=construction.target_gross_exposure,
        target_weights=construction.feasible_stock_weights,
        reason_codes=("risk_next_session",),
        signal_sha256=construction.construction_sha256,
        market_data_sha256=construction.input_snapshot_sha256,
        model_sha256=construction.model_sha256,
        risk_state_sha256=EXPOSURE_STATE_SHA256,
    )
    return selected_policy, construction, intent


def risk_account(*, cash="9000", quantity=100, sellable_quantity=100) -> AccountSnapshot:
    return AccountSnapshot(
        STRATEGY_ID,
        D(cash),
        {
            "000001.SZ": Position(
                "000001.SZ",
                quantity,
                sellable_quantity,
            )
        },
        snapshot_id="reconciled-d-plus-one",
        as_of=CHECKED - timedelta(minutes=1),
    )


def close_account() -> AccountSnapshot:
    return AccountSnapshot(
        STRATEGY_ID,
        D("9000"),
        {"000001.SZ": Position("000001.SZ", 100, 100)},
        snapshot_id="strategy-close-20260821",
        as_of=DECISION - timedelta(minutes=1),
    )


def _with_self_hash(content, field_name):
    return {**content, field_name: canonical_sha256(content)}


def _dataclass_init_kwargs(value):
    return {
        item.name: getattr(value, item.name)
        for item in fields(value)
        if item.init
    }


def publish_risk_bundle(
    selected_policy,
    construction,
    intent,
    calendar_receipt,
    controlled_registry,
    *,
    return_bundle=False,
):
    admitted = admission_receipt()
    account = close_account()
    fees = canonical_fees()
    instrument_rules = canonical_rules()
    rule_hash = execution_rule_bundle_sha256(fees, instrument_rules)
    ranking_content = {
        "schema_version": "alpha-ranking.v2",
        "status": "DATA_FAIL_CLOSED",
        "decision_at": DECISION.isoformat(),
        "eligible_count": 0,
        "rows": [
            {
                "instrument_id": "000001.SZ",
                "decision_at": DECISION.isoformat(),
                "predicted_return": None,
                "quality_score": None,
                "timing_score": None,
                "percentile": None,
                "rank": None,
                "industry": "controlled-test-industry",
                "eligibility": False,
                "exclusion_codes": ["FORMAL_EXPERIMENT_V3_ADMISSION_BLOCKED"],
            }
        ],
        "model_sha256": MODEL_SHA256,
        "input_snapshot_sha256": construction.input_snapshot_sha256,
    }
    exposure_decision = RISK_EXPOSURE_DECISION.to_dict()
    combined_policy_sha256 = canonical_sha256(
        {
            "exposure_policy_sha256": EXPOSURE_POLICY_SHA256,
            "constructor_policy_sha256": selected_policy.policy_sha256,
            "execution_rule_bundle_sha256": rule_hash,
        }
    )
    frozen_cancel_conditions = (
        "official_calendar_or_snapshot_binding_changed",
        "D+1_account_or_quote_preflight_failed",
    )
    sell_orders = tuple(
        DailyOrderV1(
            instrument_id=str(item.instrument_id),
            side="SELL",
            quantity=item.order_quantity,
            reference_price=item.reference_price,
            target_weight=item.feasible_weight,
            maximum_execution_price_deviation=selected_policy.maximum_execution_price_deviation,
            cancel_conditions=frozen_cancel_conditions,
        )
        for item in construction.actions
        if item.action.value == "SELL"
    )
    daily_payload = DailyStrategyDecisionV2(
        strategy_date=DECISION.date(),
        execution_date=EXECUTION_DATE,
        decision_status="DATA_FAIL_CLOSED",
        data_status="DATA_FAIL_CLOSED",
        market_regime="RISK_OFF",
        portfolio_intent_type=intent.intent_type.value,
        target_gross_exposure=construction.target_gross_exposure,
        feasible_gross_exposure=construction.feasible_gross_exposure,
        current_gross_exposure=construction.current_gross_exposure,
        realized_gross_exposure=None,
        target_stock_weights=construction.target_stock_weights,
        feasible_stock_weights=construction.feasible_stock_weights,
        current_stock_weights=construction.current_stock_weights,
        realized_stock_weights=None,
        target_lot_quantities={},
        feasible_lot_quantities=construction.feasible_quantities,
        current_lot_quantities=construction.current_quantities,
        realized_lot_quantities=None,
        buy_orders=(),
        sell_orders=sell_orders,
        hold_positions=(),
        cash_weight=D("1"),
        maximum_execution_price_deviation=selected_policy.maximum_execution_price_deviation,
        cancel_conditions=frozen_cancel_conditions,
        expected_cost=construction.expected_cost,
        model_reasons=("formal_alpha_data_fail_closed",),
        risk_reasons=("risk_off_exit_remains_available",),
        no_trade_reasons=(),
        data_sha256=construction.input_snapshot_sha256,
        model_sha256=MODEL_SHA256,
        policy_sha256=combined_policy_sha256,
        intent_sha256=intent.intent_sha256,
    ).to_dict()
    authority_content = {
        "schema_version": "daily-signal-authority-receipt.v1",
        "strategy_id": STRATEGY_ID,
        "strategy_date": DECISION.date(),
        "execution_date": EXECUTION_DATE,
        "frozen_at": intent.frozen_at,
        "authority": DailySignalAuthority.RISK_REDUCTION_ONLY.value,
        "intent_type": intent.intent_type.value,
        "construction_sha256": construction.construction_sha256,
        "formal_v3_loader_status": "blocked_not_implemented",
        "buy_allowed": False,
        "automatic_submission": False,
        "paper_eligibility": False,
        "trade_eligibility": False,
        "live_supported": False,
    }
    authority_payload = _with_self_hash(
        authority_content, "authority_receipt_sha256"
    )
    failure_content = {
        "schema_version": "daily-signal-publication-failure-receipt.v1",
        "strategy_id": STRATEGY_ID,
        "strategy_date": DECISION.date(),
        "failed_stage": None,
        "failure_codes": [],
        "authority_receipt_sha256": authority_payload["authority_receipt_sha256"],
        "orders_allowed": True,
        "buy_allowed": False,
    }
    failure_payload = _with_self_hash(
        failure_content, "failure_receipt_sha256"
    )
    account_state_sha256 = construction.account_state_sha256
    account_payload = {
        "scope": "daily-strategy-account-snapshot.v1",
        "strategy_id": account.strategy_id,
        "snapshot_id": account.snapshot_id,
        "as_of": account.as_of,
        "cash": account.cash,
        "positions": {key: value.quantity for key, value in account.positions.items()},
        "sellable_positions": {
            key: value.sellable_quantity for key, value in account.positions.items()
        },
        "account_state_sha256": account_state_sha256,
        "account_fingerprint": account_fingerprint(account),
        "strategy_only": True,
    }
    rule_payload = {
        "scope": "daily-canonical-execution-rule-bundle.v1",
        "fee_schedule": {
            "commission_rate": fees.commission_rate,
            "minimum_commission": fees.minimum_commission,
            "exchange_fee_rate": fees.exchange_fee_rate,
        },
        "instrument_rules": [
            {
                "instrument_id": item.instrument_id,
                "name": item.name,
                "instrument_type": item.instrument_type,
                "lot_size": item.lot_size,
                "tick_size": item.tick_size,
                "sell_stamp_duty_rate": item.sell_stamp_duty_rate,
                "t_plus_one": item.t_plus_one,
            }
            for _, item in sorted(instrument_rules.items())
        ],
        "whole_lot_policy": "floor_to_instrument_lot.v1",
        "execution_rule_bundle_sha256": rule_hash,
        "source_authentication": "hash_consistency_only_registry_acl_is_writer_boundary",
    }
    ranking_payload = _with_self_hash(ranking_content, "ranking_sha256")
    exposure_policy_payload = _with_self_hash(
        EXPOSURE_POLICY_CONTENT, "policy_sha256"
    )
    exposure_state_payload = RISK_EXPOSURE_DECISION.next_state_memory.to_dict()
    admission = DailySignalAdmissionReceiptV1(
        strategy_date=DECISION.date(),
        execution_date=EXECUTION_DATE,
        frozen_at=intent.frozen_at,
        authority=DailySignalAuthority.RISK_REDUCTION_ONLY,
        intent_type=intent.intent_type.value,
        alpha_ranking_sha256=ranking_payload["ranking_sha256"],
        model_sha256=MODEL_SHA256,
        approved_factor_registry_sha256=FACTOR_REGISTRY_SHA256,
        model_admission_receipt_sha256=admitted.model_admission_receipt_sha256,
        exposure_decision_sha256=exposure_decision["decision_sha256"],
        exposure_state_sha256=EXPOSURE_STATE_SHA256,
        exposure_state="RISK_OFF",
        exposure_target_gross=D("0"),
        construction_sha256=construction.construction_sha256,
        intent_sha256=intent.intent_sha256,
        exposure_policy_sha256=EXPOSURE_POLICY_SHA256,
        constructor_policy_sha256=selected_policy.policy_sha256,
        combined_policy_sha256=combined_policy_sha256,
        account_state_sha256=account_state_sha256,
        account_fingerprint=account_payload["account_fingerprint"],
        calendar_receipt_sha256=calendar_receipt.receipt_sha256,
        calendar_registry_sha256=controlled_registry.registry_sha256,
        execution_rule_bundle_sha256=rule_hash,
        daily_decision_sha256=daily_payload["decision_sha256"],
        experiment_admission_receipt_sha256=admitted.receipt_sha256,
        authority_receipt_sha256=authority_payload["authority_receipt_sha256"],
    )
    artifacts = {
        "alpha-ranking": ranking_payload,
        "alpha-model": {**MODEL_CONTENT, "model_sha256": MODEL_SHA256},
        "approved-factor-registry": {
            **FACTOR_REGISTRY_CONTENT,
            "registry_sha256": FACTOR_REGISTRY_SHA256,
        },
        "exposure-decision": exposure_decision,
        "exposure-state": exposure_state_payload,
        "portfolio-construction": construction.to_dict(),
        "portfolio-intent": intent.to_dict(),
        "exposure-policy": exposure_policy_payload,
        "constructor-policy": selected_policy.to_dict(),
        "experiment-v3-admission": admitted.to_dict(),
        "account-snapshot": account_payload,
        "calendar-receipt": calendar_receipt.to_dict(),
        "calendar-registry": controlled_registry.to_dict(),
        "execution-rule-bundle": rule_payload,
        "daily-decision": daily_payload,
        "authority-receipt": authority_payload,
        "failure-receipt": failure_payload,
    }
    if return_bundle:
        return admission, json.loads(canonical_json_bytes(artifacts))
    return _publish_daily_signal_bundle_from_daily_pipeline(
        admission=admission,
        artifacts=artifacts,
    )


def quote(*, ask="10.10", buy_blocked=False) -> MarketQuote:
    return MarketQuote(
        instrument_id="000001.SZ",
        bid=D("10.00"),
        ask=D(ask),
        last=D("10.05"),
        as_of=CHECKED - timedelta(minutes=1),
        buy_blocked=buy_blocked,
    )


class NextSessionSignalTests(unittest.TestCase):
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

    def create_risk(self):
        selected_policy, construction, intent = build_risk()
        calendar_receipt = receipt()
        controlled_registry = registry(calendar_receipt)
        publication = publish_risk_bundle(
            selected_policy,
            construction,
            intent,
            calendar_receipt,
            controlled_registry,
        )
        signal = create_risk_next_session_signal(
            intent=intent,
            construction=construction,
            policy=selected_policy,
            experiment_v3_admission_receipt=admission_receipt(),
            receipt=calendar_receipt,
            registry=controlled_registry,
            fees=canonical_fees(),
            instrument_rules=canonical_rules(),
        )
        self.assertEqual(
            signal.daily_signal_publication_receipt_sha256,
            publication.publication_receipt_sha256,
        )
        return selected_policy, construction, intent, calendar_receipt, controlled_registry, signal

    def test_cross_process_json_schemas_parse_and_are_closed(self) -> None:
        schema_dir = Path(__file__).resolve().parents[1] / "schemas"
        expected_versions = {
            "portfolio_constructor_policy.v2.json": "portfolio-constructor-policy.v2",
            "portfolio_construction_result.v2.json": "portfolio-construction-result.v2",
            "official_calendar_receipt.v1.json": "official-calendar-receipt.v1",
            "official_calendar_registry.v1.json": "official-calendar-registry.v1",
            "next_session_signal.v1.json": "next-session-signal.v1",
            "next_session_signal.v2.json": "next-session-signal.v2",
            "next_session_consumption.v1.json": "next-session-consumption.v1",
        }
        for filename, version in expected_versions.items():
            with self.subTest(filename=filename):
                payload = json.loads((schema_dir / filename).read_text(encoding="utf-8"))
                self.assertEqual(payload["$schema"], "https://json-schema.org/draft/2020-12/schema")
                self.assertFalse(payload["additionalProperties"])
                self.assertEqual(
                    payload["properties"]["schema_version"]["const"],
                    version,
                )

    def test_structured_receipt_requires_exact_registry_allowlist(self) -> None:
        selected_policy, construction, intent = build_risk()
        calendar_receipt = receipt()
        other_receipt = replace(calendar_receipt, receipt_id="other-calendar-receipt")
        wrong_registry = registry(other_receipt)

        with self.assertRaisesRegex(NextSessionSignalError, "allowlist"):
            create_risk_next_session_signal(
                intent=intent,
                construction=construction,
                policy=selected_policy,
                experiment_v3_admission_receipt=admission_receipt(),
                receipt=calendar_receipt,
                registry=wrong_registry,
                fees=canonical_fees(),
                instrument_rules=canonical_rules(),
            )
        with self.assertRaisesRegex(NextSessionSignalError, "OfficialCalendarRegistry"):
            create_risk_next_session_signal(
                intent=intent,
                construction=construction,
                policy=selected_policy,
                experiment_v3_admission_receipt=admission_receipt(),
                receipt=calendar_receipt,
                registry=True,  # type: ignore[arg-type]
                fees=canonical_fees(),
                instrument_rules=canonical_rules(),
            )

    def test_risk_creation_without_fixed_daily_publication_fails_closed(self) -> None:
        selected_policy, construction, intent = build_risk()
        calendar_receipt = receipt()
        with self.assertRaisesRegex(
            NextSessionSignalError, "fixed Daily publication"
        ):
            create_risk_next_session_signal(
                intent=intent,
                construction=construction,
                policy=selected_policy,
                experiment_v3_admission_receipt=admission_receipt(),
                receipt=calendar_receipt,
                registry=registry(calendar_receipt),
                fees=canonical_fees(),
                instrument_rules=canonical_rules(),
            )

    def test_blocked_daily_cannot_masquerade_as_risk_publication(self) -> None:
        selected_policy, construction, intent = build_risk()
        calendar_receipt = receipt()
        controlled_registry = registry(calendar_receipt)
        admission, artifacts = publish_risk_bundle(
            selected_policy,
            construction,
            intent,
            calendar_receipt,
            controlled_registry,
            return_bundle=True,
        )
        attacked_artifacts = dict(artifacts)
        daily = dict(attacked_artifacts["daily-decision"])
        daily.update(
            {
                "decision_status": "BLOCKED",
                "data_status": "MODEL_ADMISSION_BLOCKED",
                "target_gross_exposure": "0",
                "feasible_gross_exposure": "0",
                "target_stock_weights": {},
                "feasible_stock_weights": {},
                "target_lot_quantities": {},
                "feasible_lot_quantities": {},
                "buy_orders": [],
                "sell_orders": [],
                "hold_positions": [],
                "expected_cost": "0",
                "failed_stage": "DAILY_SIGNAL_ADMISSION",
                "failure_codes": ["FORGED_BLOCKED_AS_RISK"],
                "failure_receipt_sha256": "8" * 64,
            }
        )
        daily.pop("decision_sha256")
        attacked_daily = _with_self_hash(daily, "decision_sha256")
        attacked_artifacts["daily-decision"] = attacked_daily
        attacked_admission = replace(
            admission,
            daily_decision_sha256=attacked_daily["decision_sha256"],
        )
        with self.assertRaisesRegex(
            DailySignalPublicationError,
            "risk authority|Daily decision status|formal BLOCKED",
        ):
            validate_daily_signal_publication_contract(
                attacked_admission,
                attacked_artifacts,
            )

    def test_defensive_exposure_cannot_publish_a_full_exit_target(self) -> None:
        selected_policy, construction, intent = build_risk()
        calendar_receipt = receipt()
        controlled_registry = registry(calendar_receipt)
        admission, artifacts = publish_risk_bundle(
            selected_policy,
            construction,
            intent,
            calendar_receipt,
            controlled_registry,
            return_bundle=True,
        )
        defensive = frozen_exposure_decision(ExposureState.DEFENSIVE)
        attacked_artifacts = dict(artifacts)
        attacked_artifacts["exposure-decision"] = defensive.to_dict()
        attacked_artifacts["exposure-state"] = defensive.next_state_memory.to_dict()
        ranking = dict(attacked_artifacts["alpha-ranking"])
        ranking.update(
            {
                "status": "OK",
                "eligible_count": 1,
                "rows": [
                    {
                        "instrument_id": "000001.SZ",
                        "decision_at": DECISION.isoformat(),
                        "predicted_return": 0.10,
                        "quality_score": 0.50,
                        "timing_score": 0.50,
                        "percentile": 1.0,
                        "rank": 1,
                        "industry": "controlled-test-industry",
                        "eligibility": True,
                        "exclusion_codes": [],
                    }
                ],
            }
        )
        ranking.pop("ranking_sha256")
        attacked_ranking = _with_self_hash(ranking, "ranking_sha256")
        attacked_artifacts["alpha-ranking"] = attacked_ranking
        intent_payload = dict(attacked_artifacts["portfolio-intent"])
        intent_payload["risk_state_sha256"] = defensive.state_sha256
        intent_payload.pop("intent_sha256")
        attacked_intent = _with_self_hash(intent_payload, "intent_sha256")
        attacked_artifacts["portfolio-intent"] = attacked_intent
        daily = dict(attacked_artifacts["daily-decision"])
        daily["market_regime"] = "DEFENSIVE"
        daily["decision_status"] = "READY_FOR_NEXT_SESSION_REVIEW"
        daily["data_status"] = "CONTROLLED_PIT_OK"
        daily["intent_sha256"] = attacked_intent["intent_sha256"]
        daily.pop("decision_sha256")
        attacked_daily = _with_self_hash(daily, "decision_sha256")
        attacked_artifacts["daily-decision"] = attacked_daily
        attacked_admission = replace(
            admission,
            alpha_ranking_sha256=attacked_ranking["ranking_sha256"],
            exposure_decision_sha256=defensive.decision_sha256,
            exposure_state_sha256=defensive.state_sha256,
            exposure_state="DEFENSIVE",
            exposure_target_gross=D("0.30"),
            intent_sha256=attacked_intent["intent_sha256"],
            daily_decision_sha256=attacked_daily["decision_sha256"],
        )
        with self.assertRaisesRegex(
            DailySignalPublicationError,
            "graph drifted|unreachable",
        ):
            validate_daily_signal_publication_contract(
                attacked_admission,
                attacked_artifacts,
            )

    def test_risk_off_exposure_cannot_publish_a_defensive_target(self) -> None:
        selected_policy, construction, intent = build_risk()
        calendar_receipt = receipt()
        controlled_registry = registry(calendar_receipt)
        admission, artifacts = publish_risk_bundle(
            selected_policy,
            construction,
            intent,
            calendar_receipt,
            controlled_registry,
            return_bundle=True,
        )
        attacked_artifacts = dict(artifacts)
        construction_payload = dict(attacked_artifacts["portfolio-construction"])
        construction_payload.update(
            {
                "requested_intent_type": "DEFENSIVE_REDUCTION",
                "intent_type": "DEFENSIVE_REDUCTION",
                "target_gross_exposure": "0.30",
            }
        )
        construction_payload.pop("construction_sha256")
        attacked_construction = _with_self_hash(
            construction_payload,
            "construction_sha256",
        )
        attacked_artifacts["portfolio-construction"] = attacked_construction
        intent_payload = dict(attacked_artifacts["portfolio-intent"])
        intent_payload.update(
            {
                "intent_type": "DEFENSIVE_REDUCTION",
                "target_gross_exposure": "0.30",
                "signal_sha256": attacked_construction["construction_sha256"],
            }
        )
        intent_payload.pop("intent_sha256")
        attacked_intent = _with_self_hash(intent_payload, "intent_sha256")
        attacked_artifacts["portfolio-intent"] = attacked_intent
        authority = dict(attacked_artifacts["authority-receipt"])
        authority.update(
            {
                "intent_type": "DEFENSIVE_REDUCTION",
                "construction_sha256": attacked_construction["construction_sha256"],
            }
        )
        authority.pop("authority_receipt_sha256")
        attacked_authority = _with_self_hash(
            authority,
            "authority_receipt_sha256",
        )
        attacked_artifacts["authority-receipt"] = attacked_authority
        failure = dict(attacked_artifacts["failure-receipt"])
        failure["authority_receipt_sha256"] = attacked_authority[
            "authority_receipt_sha256"
        ]
        failure.pop("failure_receipt_sha256")
        attacked_artifacts["failure-receipt"] = _with_self_hash(
            failure,
            "failure_receipt_sha256",
        )
        daily = dict(attacked_artifacts["daily-decision"])
        daily.update(
            {
                "portfolio_intent_type": "DEFENSIVE_REDUCTION",
                "target_gross_exposure": "0.30",
                "intent_sha256": attacked_intent["intent_sha256"],
            }
        )
        daily.pop("decision_sha256")
        attacked_daily = _with_self_hash(daily, "decision_sha256")
        attacked_artifacts["daily-decision"] = attacked_daily
        attacked_admission = replace(
            admission,
            intent_type="DEFENSIVE_REDUCTION",
            construction_sha256=attacked_construction["construction_sha256"],
            intent_sha256=attacked_intent["intent_sha256"],
            authority_receipt_sha256=attacked_authority[
                "authority_receipt_sha256"
            ],
            daily_decision_sha256=attacked_daily["decision_sha256"],
        )
        with self.assertRaisesRegex(
            DailySignalPublicationError,
            "graph drifted|unreachable",
        ):
            validate_daily_signal_publication_contract(
                attacked_admission,
                attacked_artifacts,
            )

    def test_trust_boundaries_reject_receipt_and_signal_subclasses(self) -> None:
        selected_policy, construction, intent = build_risk()
        calendar_receipt = receipt()
        controlled_registry = registry(calendar_receipt)
        publication = publish_risk_bundle(
            selected_policy,
            construction,
            intent,
            calendar_receipt,
            controlled_registry,
        )
        daily_admission, daily_artifacts = publish_risk_bundle(
            selected_policy,
            construction,
            intent,
            calendar_receipt,
            controlled_registry,
            return_bundle=True,
        )

        class ForgedDailyAdmission(DailySignalAdmissionReceiptV1):
            def to_dict(self):  # pragma: no cover - must not run
                return {"authority": "RISK_REDUCTION_ONLY"}

        forged_daily_admission = ForgedDailyAdmission(
            **_dataclass_init_kwargs(daily_admission)
        )
        with self.assertRaisesRegex(DailySignalPublicationError, "exact V1 type"):
            validate_daily_signal_publication_contract(
                forged_daily_admission,
                daily_artifacts,
            )

        class ForgedExperimentReceipt(ExperimentV3AdmissionReceiptV1):
            def require_structural_valid(self, *, as_of):  # pragma: no cover - must not run
                return None

        forged_experiment = ForgedExperimentReceipt(
            **_dataclass_init_kwargs(admission_receipt())
        )
        with self.assertRaisesRegex(NextSessionSignalError, "exact external receipt"):
            create_risk_next_session_signal(
                intent=intent,
                construction=construction,
                policy=selected_policy,
                experiment_v3_admission_receipt=forged_experiment,
                receipt=calendar_receipt,
                registry=controlled_registry,
                fees=canonical_fees(),
                instrument_rules=canonical_rules(),
            )

        class ForgedPublicationReceipt(DailySignalPublicationReceiptV1):
            def to_dict(self):  # pragma: no cover - must not run
                return {"authority": "FORGED"}

        forged_publication = ForgedPublicationReceipt(
            **_dataclass_init_kwargs(publication)
        )
        with self.assertRaisesRegex(NextSessionSignalError, "exact receipt type"):
            create_risk_next_session_signal(
                intent=intent,
                construction=construction,
                policy=selected_policy,
                experiment_v3_admission_receipt=admission_receipt(),
                daily_signal_publication_receipt=forged_publication,
                receipt=calendar_receipt,
                registry=controlled_registry,
                fees=canonical_fees(),
                instrument_rules=canonical_rules(),
            )

        signal = create_risk_next_session_signal(
            intent=intent,
            construction=construction,
            policy=selected_policy,
            experiment_v3_admission_receipt=admission_receipt(),
            daily_signal_publication_receipt=publication,
            receipt=calendar_receipt,
            registry=controlled_registry,
            fees=canonical_fees(),
            instrument_rules=canonical_rules(),
        )

        class ForgedNextSessionSignal(NextSessionSignal):
            def to_dict(self):  # pragma: no cover - must not run
                return {"channel": "ALPHA"}

        forged_signal = ForgedNextSessionSignal(**_dataclass_init_kwargs(signal))
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(NextSessionSignalError, "exact NextSessionSignal"):
                write_new_next_session_signal(
                    Path(folder) / "forged-signal.json",
                    forged_signal,
                )

    def test_self_hashed_construction_model_drift_cannot_replace_publication(self) -> None:
        selected_policy, construction, intent = build_risk()
        calendar_receipt = receipt()
        controlled_registry = registry(calendar_receipt)
        publish_risk_bundle(
            selected_policy,
            construction,
            intent,
            calendar_receipt,
            controlled_registry,
        )
        forged_construction = replace(construction, model_sha256="a" * 64)
        forged_intent = replace(
            intent,
            signal_sha256=forged_construction.construction_sha256,
            model_sha256=forged_construction.model_sha256,
        )
        with self.assertRaisesRegex(
            NextSessionSignalError,
            "model|fixed Daily publication|receipt differs from fixed risk construction",
        ):
            create_risk_next_session_signal(
                intent=forged_intent,
                construction=forged_construction,
                policy=selected_policy,
                experiment_v3_admission_receipt=admission_receipt(),
                receipt=calendar_receipt,
                registry=controlled_registry,
                fees=canonical_fees(),
                instrument_rules=canonical_rules(),
            )

    def test_signal_v2_binds_complete_experiment_v3_admission_evidence(self) -> None:
        _, _, _, _, _, signal = self.create_risk()
        admitted = admission_receipt()
        payload = signal.to_dict()
        self.assertEqual(payload["schema_version"], "next-session-signal.v2")
        self.assertEqual(
            payload["experiment_spec_sha256"], admitted.experiment_spec_sha256
        )
        self.assertEqual(
            payload["experiment_admission_receipt_sha256"],
            admitted.receipt_sha256,
        )
        self.assertEqual(
            payload["approved_factor_registry_sha256"],
            admitted.approved_factor_registry_sha256,
        )
        self.assertEqual(
            payload["model_training_receipt_sha256"],
            admitted.model_training_receipt_sha256,
        )
        self.assertEqual(
            payload["model_admission_receipt_sha256"],
            admitted.model_admission_receipt_sha256,
        )
        self.assertEqual(
            payload["calibration_receipt_sha256"],
            admitted.calibration_receipt_sha256,
        )
        self.assertEqual(
            payload["calibration_horizon_sessions"],
            admitted.calibration_horizon_sessions,
        )
        self.assertNotIn("experiment_admission_receipt", payload)
        validate_json_schema(
            json.loads(canonical_json_bytes(payload)),
            Path(__file__).resolve().parents[1]
            / "schemas"
            / "next_session_signal.v2.json",
        )

    def test_self_hashed_signal_cannot_replace_external_typed_admission(self) -> None:
        _, _, _, _, _, signal = self.create_risk()
        with tempfile.TemporaryDirectory() as folder:
            signal_path = Path(folder) / "signal.json"
            with self.assertRaises(TypeError):
                _write_new_next_session_signal(signal_path, signal)
            with self.assertRaisesRegex(
                NextSessionSignalError,
                "admission|constructor policy is malformed",
            ):
                _write_new_next_session_signal(
                    signal_path,
                    signal,
                    registry=registry(receipt()),
                    experiment_v3_admission_receipt=signal.to_dict(),  # type: ignore[arg-type]
                )
            self.assertFalse(signal_path.exists())

    def test_external_admission_mismatch_rejects_reload_and_consume_before_cas(self) -> None:
        _, _, _, _, controlled_registry, signal = self.create_risk()
        wrong_receipt = admission_receipt(
            approved_factor_registry_sha256="2" * 64
        )
        with tempfile.TemporaryDirectory() as folder:
            signal_path = Path(folder) / "signal.json"
            write_new_next_session_signal(signal_path, signal)
            consumed_path = canonical_next_session_consumption_path(
                signal_path, signal.signal_sha256
            )
            with self.assertRaisesRegex(
                NextSessionSignalError,
                "receipt|policy hash|admission",
            ):
                _read_next_session_signal(
                    signal_path,
                    registry=controlled_registry,
                    experiment_v3_admission_receipt=wrong_receipt,
                )
            with self.assertRaisesRegex(
                NextSessionSignalError,
                "receipt|policy hash|admission",
            ):
                _consume_next_session_signal(
                    signal_path,
                    consumed_path,
                    registry=controlled_registry,
                    experiment_v3_admission_receipt=wrong_receipt,
                    account=risk_account(),
                    quotes={"000001.SZ": quote()},
                    fees=canonical_fees(),
                    instrument_rules=canonical_rules(),
                    checked_at=CHECKED,
                )
            self.assertFalse(consumed_path.exists())

    def test_runtime_explicitly_rejects_historical_v1_signal(self) -> None:
        _, _, _, _, controlled_registry, signal = self.create_risk()
        payload = signal.to_dict()
        payload["schema_version"] = "next-session-signal.v1"
        with tempfile.TemporaryDirectory() as folder:
            signal_path = Path(folder) / "historical-v1.json"
            signal_path.write_bytes(canonical_json_bytes(payload) + b"\n")
            with self.assertRaisesRegex(NextSessionSignalError, "v2 only"):
                read_next_session_signal(
                    signal_path,
                    registry=controlled_registry,
                )

    def test_signal_is_byte_idempotent_and_consumed_once_on_adjacent_d_plus_one(self) -> None:
        _, _, _, _, controlled_registry, signal = self.create_risk()
        self.assertEqual(signal.strategy_date, DECISION.date())
        self.assertEqual(signal.execution_date, EXECUTION_DATE)
        self.assertIs(signal.channel, NextSessionChannel.RISK_REDUCTION)
        self.assertFalse(signal.to_dict()["automatic_submission"])

        with tempfile.TemporaryDirectory() as folder:
            signal_path = Path(folder) / "signal.json"
            consumed_path = canonical_next_session_consumption_path(
                signal_path, signal.signal_sha256
            )
            write_new_next_session_signal(signal_path, signal)
            original = signal_path.read_bytes()
            self.assertEqual(write_new_next_session_signal(signal_path, signal), signal_path)
            self.assertEqual(signal_path.read_bytes(), original)
            conflict_path = Path(folder) / "conflict-signal.json"
            conflict_path.write_bytes(b"{}\n")
            with self.assertRaisesRegex(NextSessionSignalConflict, "different bytes"):
                write_new_next_session_signal(conflict_path, signal)
            self.assertEqual(signal_path.read_bytes(), original)
            loaded = read_next_session_signal(signal_path, registry=controlled_registry)
            self.assertEqual(loaded.signal_sha256, signal.signal_sha256)

            consumed = consume_next_session_signal(
                signal_path,
                consumed_path,
                registry=controlled_registry,
                account=risk_account(),
                quotes={"000001.SZ": quote()},
                fees=canonical_fees(),
                instrument_rules=canonical_rules(),
                checked_at=CHECKED,
            )
            self.assertEqual(consumed.status, "READY_FOR_MANUAL_EXECUTION")
            self.assertTrue(consumed.to_dict()["manual_execution_required"])
            self.assertFalse(consumed.to_dict()["automatic_submission"])
            with self.assertRaisesRegex(NextSessionAlreadyConsumed, "already exists"):
                consume_next_session_signal(
                    signal_path,
                    consumed_path,
                    registry=controlled_registry,
                    account=risk_account(),
                    quotes={"000001.SZ": quote()},
                    fees=canonical_fees(),
                    instrument_rules=canonical_rules(),
                    checked_at=CHECKED,
                )

    def test_one_shot_consumption_path_is_signal_hash_derived(self) -> None:
        _, _, _, _, controlled_registry, signal = self.create_risk()
        with tempfile.TemporaryDirectory() as folder:
            signal_path = Path(folder) / "signal.json"
            canonical_path = canonical_next_session_consumption_path(
                signal_path, signal.signal_sha256
            )
            alternate_path = Path(folder) / "alternate-consumption.json"
            write_new_next_session_signal(signal_path, signal)
            with self.assertRaisesRegex(NextSessionSignalError, "canonical"):
                consume_next_session_signal(
                    signal_path,
                    alternate_path,
                    registry=controlled_registry,
                    account=risk_account(),
                    quotes={"000001.SZ": quote()},
                    fees=canonical_fees(),
                    instrument_rules=canonical_rules(),
                    checked_at=CHECKED,
                )
            self.assertFalse(alternate_path.exists())
            consumed = consume_next_session_signal(
                signal_path,
                canonical_path,
                registry=controlled_registry,
                account=risk_account(),
                quotes={"000001.SZ": quote()},
                fees=canonical_fees(),
                instrument_rules=canonical_rules(),
                checked_at=CHECKED,
            )
            self.assertEqual(consumed.status, "READY_FOR_MANUAL_EXECUTION")
            with self.assertRaises(NextSessionAlreadyConsumed):
                consume_next_session_signal(
                    signal_path,
                    canonical_path,
                    registry=controlled_registry,
                    account=risk_account(),
                    quotes={"000001.SZ": quote()},
                    fees=canonical_fees(),
                    instrument_rules=canonical_rules(),
                    checked_at=CHECKED,
                )

    def test_cross_directory_publication_shares_one_global_consumption_slot(self) -> None:
        _, _, _, _, controlled_registry, signal = self.create_risk()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            signal_path = root / "signal.json"
            write_new_next_session_signal(signal_path, signal)
            alias_path = root / "aliases" / "renamed-signal.json"
            alias_path.parent.mkdir()
            alias_path.write_bytes(signal_path.read_bytes())

            canonical_path = canonical_next_session_consumption_path(
                signal_path,
                signal.signal_sha256,
            )
            self.assertEqual(
                canonical_next_session_consumption_path(
                    alias_path,
                    signal.signal_sha256,
                ),
                canonical_path,
            )
            first = consume_next_session_signal(
                signal_path,
                canonical_path,
                registry=controlled_registry,
                account=risk_account(cash="8999"),
                quotes={"000001.SZ": quote()},
                fees=canonical_fees(),
                instrument_rules=canonical_rules(),
                checked_at=CHECKED,
            )
            self.assertEqual(first.status, "CANCELED")
            with self.assertRaises(NextSessionAlreadyConsumed):
                consume_next_session_signal(
                    alias_path,
                    canonical_path,
                    registry=controlled_registry,
                    account=risk_account(),
                    quotes={"000001.SZ": quote()},
                    fees=canonical_fees(),
                    instrument_rules=canonical_rules(),
                    checked_at=CHECKED,
                )
            with tempfile.TemporaryDirectory() as outside_folder:
                outside_alias = Path(outside_folder) / "copied-signal.json"
                self.assertEqual(
                    write_new_next_session_signal(outside_alias, signal),
                    outside_alias,
                )
                outside_consumption = canonical_next_session_consumption_path(
                    outside_alias,
                    signal.signal_sha256,
                )
                self.assertEqual(outside_consumption, canonical_path)
                with self.assertRaises(NextSessionAlreadyConsumed):
                    consume_next_session_signal(
                        outside_alias,
                        outside_consumption,
                        registry=controlled_registry,
                        account=risk_account(),
                        quotes={"000001.SZ": quote()},
                        fees=canonical_fees(),
                        instrument_rules=canonical_rules(),
                        checked_at=CHECKED,
                    )

    def test_concurrent_alias_consumers_have_exactly_one_cas_winner(self) -> None:
        _, _, _, _, controlled_registry, signal = self.create_risk()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            signal_path = root / "signal.json"
            write_new_next_session_signal(signal_path, signal)
            alias_path = root / "renamed-signal.json"
            alias_path.write_bytes(signal_path.read_bytes())
            canonical_path = canonical_next_session_consumption_path(
                signal_path,
                signal.signal_sha256,
            )
            barrier = Barrier(2)

            def attempt(source: Path) -> str:
                barrier.wait(timeout=5)
                try:
                    consume_next_session_signal(
                        source,
                        canonical_path,
                        registry=controlled_registry,
                        account=risk_account(),
                        quotes={"000001.SZ": quote()},
                        fees=canonical_fees(),
                        instrument_rules=canonical_rules(),
                        checked_at=CHECKED,
                    )
                except NextSessionAlreadyConsumed:
                    return "lost"
                return "won"

            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(attempt, (signal_path, alias_path)))
            self.assertCountEqual(outcomes, ("won", "lost"))
            persisted = json.loads(canonical_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["signal_id"], signal.signal_id)
            self.assertEqual(persisted["signal_sha256"], signal.signal_sha256)

    def test_wrong_session_is_rejected_without_consuming(self) -> None:
        _, _, _, _, controlled_registry, signal = self.create_risk()
        with tempfile.TemporaryDirectory() as folder:
            signal_path = Path(folder) / "signal.json"
            consumed_path = canonical_next_session_consumption_path(
                signal_path, signal.signal_sha256
            )
            write_new_next_session_signal(signal_path, signal)
            with self.assertRaisesRegex(NextSessionSignalError, r"bound D\+1"):
                consume_next_session_signal(
                    signal_path,
                    consumed_path,
                    registry=controlled_registry,
                    account=risk_account(),
                    quotes={"000001.SZ": quote()},
                    fees=canonical_fees(),
                    instrument_rules=canonical_rules(),
                    checked_at=CHECKED + timedelta(days=1),
                )
            self.assertFalse(consumed_path.exists())

    def test_outside_opening_review_window_is_rejected_without_consuming(self) -> None:
        _, _, _, _, controlled_registry, signal = self.create_risk()
        early_check = CHECKED.replace(hour=9, minute=24)
        early_account = replace(
            risk_account(),
            as_of=early_check - timedelta(minutes=1),
        )
        early_quote = replace(
            quote(),
            as_of=early_check - timedelta(minutes=1),
        )
        with tempfile.TemporaryDirectory() as folder:
            signal_path = Path(folder) / "signal.json"
            consumed_path = canonical_next_session_consumption_path(
                signal_path, signal.signal_sha256
            )
            write_new_next_session_signal(signal_path, signal)
            with self.assertRaisesRegex(NextSessionSignalError, "09:25-09:35"):
                consume_next_session_signal(
                    signal_path,
                    consumed_path,
                    registry=controlled_registry,
                    account=early_account,
                    quotes={"000001.SZ": early_quote},
                    fees=canonical_fees(),
                    instrument_rules=canonical_rules(),
                    checked_at=early_check,
                )
            self.assertFalse(consumed_path.exists())

    def test_risk_sell_is_not_canceled_by_buy_price_deviation_rule(self) -> None:
        _, _, _, _, controlled_registry, signal = self.create_risk()
        with tempfile.TemporaryDirectory() as folder:
            signal_path = Path(folder) / "signal.json"
            consumed_path = canonical_next_session_consumption_path(
                signal_path, signal.signal_sha256
            )
            write_new_next_session_signal(signal_path, signal)
            consumed = consume_next_session_signal(
                signal_path,
                consumed_path,
                registry=controlled_registry,
                account=risk_account(),
                quotes={"000001.SZ": quote(ask="10.21")},
                fees=canonical_fees(),
                instrument_rules=canonical_rules(),
                checked_at=CHECKED,
            )
            self.assertEqual(consumed.status, "READY_FOR_MANUAL_EXECUTION")
            self.assertFalse(any(item.action == "BUY" for item in consumed.instructions))
            sell = next(item for item in consumed.instructions if item.action == "SELL")
            self.assertEqual(sell.status.value, "READY_FOR_MANUAL_EXECUTION")
            self.assertNotIn(
                "buy_price_above_frozen_deviation_limit", sell.cancel_conditions
            )

    def test_account_state_mismatch_is_persisted_as_canceled_consumption(self) -> None:
        _, _, _, _, controlled_registry, signal = self.create_risk()
        with tempfile.TemporaryDirectory() as folder:
            signal_path = Path(folder) / "signal.json"
            consumed_path = canonical_next_session_consumption_path(
                signal_path, signal.signal_sha256
            )
            write_new_next_session_signal(signal_path, signal)
            consumed = consume_next_session_signal(
                signal_path,
                consumed_path,
                registry=controlled_registry,
                account=risk_account(cash="8999"),
                quotes={"000001.SZ": quote()},
                fees=canonical_fees(),
                instrument_rules=canonical_rules(),
                checked_at=CHECKED,
            )
            self.assertEqual(consumed.status, "CANCELED")
            self.assertIn("account_state_mismatch", consumed.cancel_reasons)
            self.assertTrue(consumed_path.exists())

    def test_d_plus_one_rule_bundle_is_rehashed_for_risk_sell(self) -> None:
        _, _, _, _, controlled_registry, signal = self.create_risk()
        with tempfile.TemporaryDirectory() as folder:
            signal_path = Path(folder) / "signal.json"
            consumed_path = canonical_next_session_consumption_path(
                signal_path, signal.signal_sha256
            )
            write_new_next_session_signal(signal_path, signal)
            consumed = consume_next_session_signal(
                signal_path,
                consumed_path,
                registry=controlled_registry,
                account=risk_account(),
                quotes={"000001.SZ": quote()},
                fees=canonical_fees(),
                instrument_rules=canonical_rules(lot_size=300),
                checked_at=CHECKED,
            )
            sell = next(item for item in consumed.instructions if item.action == "SELL")
            self.assertEqual(consumed.status, "CANCELED")
            self.assertNotEqual(
                consumed.execution_rule_bundle_sha256,
                signal.execution_rule_bundle_sha256,
            )
            self.assertIn("execution_rule_bundle_mismatch", sell.cancel_conditions)
            self.assertNotIn(
                "buy_quantity_not_whole_lot_under_d_plus_one_rule",
                sell.cancel_conditions,
            )

    def test_duplicate_sell_rows_are_rejected_before_execution(self) -> None:
        _, _, _, _, _, signal = self.create_risk()
        payload = json.loads(canonical_json_bytes(signal.construction))
        sell = next(item for item in payload["actions"] if item["action"] == "SELL")
        payload["actions"].append(dict(sell))
        content = dict(payload)
        content.pop("construction_sha256")
        payload["construction_sha256"] = canonical_sha256(content)
        with self.assertRaisesRegex(
            NextSessionSignalError, "repeat an instrument action"
        ):
            next_session_signal_module._validate_construction_payload(payload)

    def test_aggregate_sell_cannot_exceed_d_plus_one_sellable_quantity(self) -> None:
        _, _, _, _, controlled_registry, signal = self.create_risk()
        with tempfile.TemporaryDirectory() as folder:
            signal_path = Path(folder) / "signal.json"
            consumed_path = canonical_next_session_consumption_path(
                signal_path, signal.signal_sha256
            )
            write_new_next_session_signal(signal_path, signal)
            consumed = consume_next_session_signal(
                signal_path,
                consumed_path,
                registry=controlled_registry,
                account=risk_account(sellable_quantity=0),
                quotes={"000001.SZ": quote()},
                fees=canonical_fees(),
                instrument_rules=canonical_rules(),
                checked_at=CHECKED,
            )
            self.assertEqual(consumed.status, "CANCELED")
            self.assertIn(
                "aggregate_sell_quantity_exceeds_sellable",
                consumed.cancel_reasons,
            )

    def test_signal_creation_rejects_fee_bundle_drift(self) -> None:
        selected_policy, construction, intent = build_risk()
        calendar_receipt = receipt()
        controlled_registry = registry(calendar_receipt)
        publish_risk_bundle(
            selected_policy,
            construction,
            intent,
            calendar_receipt,
            controlled_registry,
        )
        drifted_fees = FeeSchedule(D("0.00020"), D("5"), D("0.00001"))
        with self.assertRaisesRegex(NextSessionSignalError, "commission_rate"):
            create_risk_next_session_signal(
                intent=intent,
                construction=construction,
                policy=selected_policy,
                experiment_v3_admission_receipt=admission_receipt(),
                receipt=calendar_receipt,
                registry=controlled_registry,
                fees=drifted_fees,
                instrument_rules=canonical_rules(),
            )

    def test_risk_exit_cannot_use_alpha_adapter_and_contains_no_buy(self) -> None:
        selected_policy, construction, intent = build_risk()
        calendar_receipt = receipt()
        controlled_registry = registry(calendar_receipt)
        with self.assertRaisesRegex(NextSessionSignalError, "blocked_not_implemented"):
            create_alpha_next_session_signal(
                intent=intent,
                construction=construction,
                policy=selected_policy,
                experiment_v3_admission_receipt=admission_receipt(),
                receipt=calendar_receipt,
                registry=controlled_registry,
                fees=canonical_fees(),
                instrument_rules=canonical_rules(),
            )
        _, _, _, _, _, signal = self.create_risk()
        self.assertIs(signal.channel, NextSessionChannel.RISK_REDUCTION)
        self.assertFalse(any(item["action"] == "BUY" for item in signal.to_dict()["construction"]["actions"]))

    def test_tampered_signal_bytes_fail_hash_or_canonical_verification(self) -> None:
        _, _, _, _, controlled_registry, signal = self.create_risk()
        with tempfile.TemporaryDirectory() as folder:
            signal_path = Path(folder) / "signal.json"
            write_new_next_session_signal(signal_path, signal)
            payload = json.loads(signal_path.read_text(encoding="utf-8"))
            payload["execution_date"] = "2026-08-25"
            signal_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(NextSessionSignalError):
                read_next_session_signal(signal_path, registry=controlled_registry)

    def test_self_rehashed_risk_signal_cannot_masquerade_as_alpha(self) -> None:
        _, _, _, _, controlled_registry, signal = self.create_risk()
        attacked = replace(signal, channel=NextSessionChannel.ALPHA)
        with tempfile.TemporaryDirectory() as folder:
            signal_path = Path(folder) / "attacked-signal.json"
            with self.assertRaisesRegex(
                NextSessionSignalError, "channel|authority|Alpha|admission"
            ):
                write_new_next_session_signal(signal_path, attacked)
            self.assertFalse(signal_path.exists())

            signal_path.write_bytes(canonical_json_bytes(attacked.to_dict()) + b"\n")
            with self.assertRaisesRegex(
                NextSessionSignalError, "channel|authority|Alpha|admission"
            ):
                read_next_session_signal(signal_path, registry=controlled_registry)


if __name__ == "__main__":
    unittest.main()
