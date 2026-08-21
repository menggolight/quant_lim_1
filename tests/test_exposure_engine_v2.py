from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import unittest

from research.strategy_workspace.exposure_engine_v2 import (
    EXPOSURE_BY_STATE,
    ComparisonOperator,
    ExposureConditionV2,
    ExposureEngineError,
    ExposureHysteresisPolicyV2,
    ExposureInputCategory,
    ExposureInputSnapshotV2,
    ExposureMetricStatus,
    ExposureMetricV2,
    ExposureState,
    ExposureStateMemoryV2,
    ExposureStateRuleV2,
    ExposureTransitionStatus,
    decide_exposure,
)


TZ = timezone(timedelta(hours=8))


def _policy(*, ambiguous: bool = False) -> ExposureHysteresisPolicyV2:
    rules = [
        ExposureStateRuleV2(
            target_state=ExposureState.RISK_ON,
            conditions=(
                ExposureConditionV2(
                    category=ExposureInputCategory.CSI800_TOTAL_RETURN_TREND,
                    operator=ComparisonOperator.GTE,
                    threshold=0.10,
                ),
            ),
            required_consecutive_sessions=2,
            reason_code="TREND_RISK_ON",
        )
    ]
    if ambiguous:
        rules.append(
            ExposureStateRuleV2(
                target_state=ExposureState.DEFENSIVE,
                conditions=(
                    ExposureConditionV2(
                        category=ExposureInputCategory.CSI800_TOTAL_RETURN_TREND,
                        operator=ComparisonOperator.GTE,
                        threshold=0.05,
                    ),
                ),
                required_consecutive_sessions=2,
                reason_code="OVERLAPPING_DEFENSIVE",
            )
        )
    return ExposureHysteresisPolicyV2(
        policy_id="adaptive-exposure-v2",
        policy_version="test-v1",
        preregistered_at=datetime(2026, 1, 1, tzinfo=TZ),
        rules=tuple(rules),
        policy_source_sha256="a" * 64,
    )


def _inputs(
    decision_at: datetime,
    *,
    trend: float = 0.2,
    account_drawdown: float = 0.01,
    failed_category: ExposureInputCategory | None = None,
    future_category: ExposureInputCategory | None = None,
) -> ExposureInputSnapshotV2:
    values = {
        ExposureInputCategory.CSI800_TOTAL_RETURN_TREND: trend,
        ExposureInputCategory.MARKET_BREADTH: 0.6,
        ExposureInputCategory.REALIZED_VOLATILITY: 0.15,
        ExposureInputCategory.MARKET_DRAWDOWN: 0.04,
        ExposureInputCategory.ALPHA_PREDICTION_DISTRIBUTION: 0.02,
        ExposureInputCategory.ACCOUNT_DRAWDOWN: account_drawdown,
    }
    metrics = []
    for index, category in enumerate(ExposureInputCategory):
        if category is failed_category:
            metrics.append(
                ExposureMetricV2(
                    category=category,
                    status=ExposureMetricStatus.DATA_FAILED,
                    value=None,
                    observation_session=None,
                    available_at=None,
                    source_snapshot_sha256=None,
                    failure_codes=("MISSING_CONTROLLED_INPUT",),
                )
            )
            continue
        available_at = decision_at - timedelta(minutes=1)
        session = decision_at.astimezone(TZ).date()
        if category is future_category:
            available_at = decision_at + timedelta(seconds=1)
        metrics.append(
            ExposureMetricV2(
                category=category,
                status=ExposureMetricStatus.OK,
                value=values[category],
                observation_session=session,
                available_at=available_at,
                source_snapshot_sha256=f"{index + 1:x}" * 64,
            )
        )
    return ExposureInputSnapshotV2(decision_at=decision_at, metrics=tuple(metrics))


def _memory(policy: ExposureHysteresisPolicyV2) -> ExposureStateMemoryV2:
    return ExposureStateMemoryV2(
        policy_sha256=policy.policy_sha256,
        current_state=ExposureState.NEUTRAL,
    )


class ExposureEngineV2Tests(unittest.TestCase):
    def test_fixed_state_mapping(self) -> None:
        self.assertEqual(
            EXPOSURE_BY_STATE,
            {
                ExposureState.RISK_OFF: 0.0,
                ExposureState.DEFENSIVE: 0.3,
                ExposureState.NEUTRAL: 0.6,
                ExposureState.RISK_ON: 1.0,
            },
        )

    def test_ordinary_change_requires_two_consecutive_sessions(self) -> None:
        policy = _policy()
        first_input = _inputs(datetime(2026, 8, 18, 16, tzinfo=TZ))
        first = decide_exposure(first_input, policy, _memory(policy))
        self.assertEqual(first.transition_status, ExposureTransitionStatus.HYSTERESIS_PENDING)
        self.assertEqual(first.state, ExposureState.NEUTRAL)
        self.assertEqual(first.target_gross_exposure, 0.6)
        self.assertEqual(first.pending_state, ExposureState.RISK_ON)
        self.assertEqual(first.pending_consecutive_sessions, 1)

        second_input = _inputs(datetime(2026, 8, 19, 16, tzinfo=TZ))
        second = decide_exposure(second_input, policy, first.next_state_memory)
        self.assertEqual(second.transition_status, ExposureTransitionStatus.STATE_CHANGED)
        self.assertEqual(second.state, ExposureState.RISK_ON)
        self.assertEqual(second.target_gross_exposure, 1.0)
        self.assertIsNone(second.pending_state)

    def test_account_drawdown_at_12_percent_is_immediate_risk_off(self) -> None:
        policy = _policy()
        result = decide_exposure(
            _inputs(datetime(2026, 8, 18, 16, tzinfo=TZ), account_drawdown=0.12),
            policy,
            _memory(policy),
        )
        self.assertEqual(result.transition_status, ExposureTransitionStatus.IMMEDIATE_RISK_OFF)
        self.assertEqual(result.state, ExposureState.RISK_OFF)
        self.assertEqual(result.target_gross_exposure, 0.0)
        self.assertIn("ACCOUNT_DRAWDOWN_GTE_12_PERCENT", result.reason_codes)

    def test_any_data_failure_or_future_input_is_immediate_risk_off(self) -> None:
        policy = _policy()
        failed = decide_exposure(
            _inputs(
                datetime(2026, 8, 18, 16, tzinfo=TZ),
                failed_category=ExposureInputCategory.MARKET_BREADTH,
            ),
            policy,
            _memory(policy),
        )
        self.assertEqual(failed.state, ExposureState.RISK_OFF)
        self.assertTrue(any(code.startswith("DATA_FAILURE:MARKET_BREADTH:") for code in failed.reason_codes))

        future = decide_exposure(
            _inputs(
                datetime(2026, 8, 18, 16, tzinfo=TZ),
                future_category=ExposureInputCategory.REALIZED_VOLATILITY,
            ),
            policy,
            _memory(policy),
        )
        self.assertEqual(future.state, ExposureState.RISK_OFF)
        self.assertIn("FUTURE_AVAILABLE_AT:REALIZED_VOLATILITY", future.reason_codes)

    def test_all_ok_metrics_must_belong_to_the_cst_strategy_date(self) -> None:
        policy = _policy()
        decision_at = datetime(2026, 8, 18, 16, tzinfo=TZ)
        for category in ExposureInputCategory:
            with self.subTest(category=category.value):
                inputs = _inputs(decision_at)
                stale_metrics = tuple(
                    replace(
                        metric,
                        observation_session=date(2010, 1, 1),
                    )
                    if metric.category is category
                    else metric
                    for metric in inputs.metrics
                )
                stale = ExposureInputSnapshotV2(
                    decision_at=decision_at,
                    metrics=stale_metrics,
                )

                result = decide_exposure(stale, policy, _memory(policy))

                self.assertEqual(
                    result.transition_status,
                    ExposureTransitionStatus.IMMEDIATE_RISK_OFF,
                )
                self.assertEqual(result.state, ExposureState.RISK_OFF)
                self.assertIn(f"STALE_SESSION:{category.value}", result.reason_codes)

    def test_plus14_raw_date_is_not_the_strategy_session(self) -> None:
        policy = _policy()
        plus_14 = timezone(timedelta(hours=14))
        decision_at = datetime(2026, 8, 19, 5, 30, tzinfo=plus_14)
        inputs = _inputs(decision_at)

        valid = decide_exposure(inputs, policy, _memory(policy))
        self.assertEqual(valid.transition_status, ExposureTransitionStatus.HYSTERESIS_PENDING)

        future_metrics = tuple(
            replace(metric, observation_session=decision_at.date())
            if metric.category is ExposureInputCategory.MARKET_BREADTH
            else metric
            for metric in inputs.metrics
        )
        attacked = ExposureInputSnapshotV2(
            decision_at=decision_at,
            metrics=future_metrics,
        )
        rejected = decide_exposure(attacked, policy, _memory(policy))

        self.assertEqual(decision_at.astimezone(TZ).date(), date(2026, 8, 18))
        self.assertEqual(rejected.state, ExposureState.RISK_OFF)
        self.assertIn("FUTURE_SESSION:MARKET_BREADTH", rejected.reason_codes)

    def test_same_cst_strategy_date_cannot_advance_hysteresis_twice(self) -> None:
        policy = _policy()
        first = decide_exposure(
            _inputs(datetime(2026, 8, 18, 16, tzinfo=TZ)),
            policy,
            _memory(policy),
        )
        plus_14 = timezone(timedelta(hours=14))
        later_same_cst_date = datetime(2026, 8, 19, 5, 30, tzinfo=plus_14)

        with self.assertRaisesRegex(ExposureEngineError, "later CST strategy date"):
            decide_exposure(
                _inputs(later_same_cst_date),
                policy,
                first.next_state_memory,
            )

    def test_unreachable_pending_count_is_rejected(self) -> None:
        policy = _policy()
        forged = ExposureStateMemoryV2(
            policy_sha256=policy.policy_sha256,
            current_state=ExposureState.NEUTRAL,
            pending_state=ExposureState.RISK_ON,
            pending_consecutive_sessions=99,
            last_decision_at=datetime(2026, 8, 17, 16, tzinfo=TZ),
            last_input_snapshot_sha256="f" * 64,
        )

        with self.assertRaisesRegex(ExposureEngineError, "unreachable"):
            decide_exposure(
                _inputs(datetime(2026, 8, 18, 16, tzinfo=TZ)),
                policy,
                forged,
            )

    def test_overlapping_rules_fail_closed(self) -> None:
        policy = _policy(ambiguous=True)
        result = decide_exposure(
            _inputs(datetime(2026, 8, 18, 16, tzinfo=TZ)),
            policy,
            _memory(policy),
        )
        self.assertEqual(result.state, ExposureState.RISK_OFF)
        self.assertIn("AMBIGUOUS_HYSTERESIS_POLICY_MATCH", result.reason_codes)

    def test_policy_and_decision_hashes_are_deterministic(self) -> None:
        policy = _policy()
        inputs = _inputs(datetime(2026, 8, 18, 16, tzinfo=TZ))
        first = decide_exposure(inputs, policy, _memory(policy))
        second = decide_exposure(inputs, policy, _memory(policy))
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.decision_sha256, second.decision_sha256)
        self.assertEqual(first.state_sha256, second.state_sha256)
        json.dumps(inputs.to_dict(), ensure_ascii=False)
        json.dumps(policy.to_dict(), ensure_ascii=False)
        json.dumps(first.next_state_memory.to_dict(), ensure_ascii=False)
        json.dumps(first.to_dict(), ensure_ascii=False)

    def test_tampered_policy_is_rejected_before_state_change(self) -> None:
        policy = _policy()
        memory = _memory(policy)
        object.__setattr__(policy, "policy_version", "tampered")
        with self.assertRaisesRegex(ExposureEngineError, "policy hash mismatch"):
            decide_exposure(
                _inputs(datetime(2026, 8, 18, 16, tzinfo=TZ)), policy, memory
            )

    def test_exact_six_categories_and_policy_bound_memory_are_required(self) -> None:
        complete = _inputs(datetime(2026, 8, 18, 16, tzinfo=TZ))
        with self.assertRaises(ExposureEngineError):
            ExposureInputSnapshotV2(
                decision_at=complete.decision_at,
                metrics=complete.metrics[:-1],
            )
        with self.assertRaises(ExposureEngineError):
            ExposureStateRuleV2(
                target_state=ExposureState.RISK_ON,
                conditions=(
                    ExposureConditionV2(
                        ExposureInputCategory.MARKET_BREADTH,
                        ComparisonOperator.GTE,
                        0.5,
                    ),
                ),
                required_consecutive_sessions=1,
                reason_code="NO_HYSTERESIS",
            )
        policy = _policy()
        wrong_memory = ExposureStateMemoryV2(
            policy_sha256="f" * 64,
            current_state=ExposureState.NEUTRAL,
        )
        with self.assertRaisesRegex(ExposureEngineError, "policy hash mismatch"):
            decide_exposure(complete, policy, wrong_memory)

    def test_new_schemas_are_valid_json(self) -> None:
        root = Path(__file__).resolve().parents[1] / "schemas"
        for name in (
            "exposure_input_snapshot.v1.json",
            "exposure_hysteresis_policy.v1.json",
            "exposure_state_memory.v1.json",
            "exposure_decision.v2.json",
        ):
            payload = json.loads((root / name).read_text(encoding="utf-8"))
            self.assertEqual(payload["$schema"], "https://json-schema.org/draft/2020-12/schema")


if __name__ == "__main__":
    unittest.main()
