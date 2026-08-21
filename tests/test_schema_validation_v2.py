from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import unittest

from research.market_data.validation import (
    SchemaValidationError,
    validate_json_schema,
)
from research.strategy_workspace.alpha_engine_v2 import run_alpha_engine
from research.strategy_workspace.contracts import canonical_json_bytes
from research.strategy_workspace.exposure_engine_v2 import decide_exposure
from research.strategy_workspace.next_session_signal import (
    create_alpha_next_session_signal,
)
from tests.test_alpha_engine_v2 import _model, _snapshot
from tests.test_daily_pipeline import decision as daily_decision
from tests.test_exposure_engine_v2 import TZ, _inputs, _memory, _policy
from tests.test_next_session_signal import (
    build_alpha,
    canonical_fees,
    canonical_rules,
    receipt,
    registry,
)


SCHEMA_ROOT = Path(__file__).resolve().parents[1] / "schemas"
SHA = "a" * 64


def _json_payload(value: object) -> object:
    """Exercise the actual cross-process JSON representation, not Python types."""

    return json.loads(canonical_json_bytes(value))


def _frozen_daily_payload() -> dict[str, object]:
    categories = (
        "CSI800_TOTAL_RETURN_TREND",
        "MARKET_BREADTH",
        "REALIZED_VOLATILITY",
        "MARKET_DRAWDOWN",
    )
    return {
        "schema_version": "frozen-daily-data.v2",
        "update_id": "controlled-update-20260818",
        "alpha_snapshot_sha256": SHA,
        "non_alpha_exposure_metrics": [
            {
                "category": category,
                "status": "OK",
                "value": 0.1,
                "observation_session": "2026-08-18",
                "available_at": "2026-08-18T15:59:00+08:00",
                "source_snapshot_sha256": f"{index:x}" * 64,
                "failure_codes": [],
            }
            for index, category in enumerate(categories, start=1)
        ],
        "instrument_rules": [
            {
                "instrument_id": "000001.SZ",
                "name": "controlled-instrument",
                "instrument_type": "A_SHARE",
                "lot_size": 100,
                "tick_size": "0.01",
                "sell_stamp_duty_rate": "0.0005",
                "t_plus_one": True,
            }
        ],
        "held_position_references": [],
        "source_authentication": (
            "external_controlled_adapters_required_hash_is_not_source_proof"
        ),
        "data_update_sha256": "f" * 64,
    }


class Draft202012SchemaValidationTests(unittest.TestCase):
    def assert_schema_rejects(self, payload: object, schema_name: str) -> None:
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(payload, SCHEMA_ROOT / schema_name)

    def test_daily_decision_schema_enforces_closed_shape_and_conditional_branch(self) -> None:
        schema_name = "daily_strategy_decision.v2.json"
        payload = _json_payload(daily_decision().to_dict())
        self.assertIsInstance(payload, dict)
        validate_json_schema(payload, SCHEMA_ROOT / schema_name)

        missing = deepcopy(payload)
        missing.pop("data_sha256")
        self.assert_schema_rejects(missing, schema_name)

        extra = deepcopy(payload)
        extra["unregistered_field"] = True
        self.assert_schema_rejects(extra, schema_name)

        blocked_with_buy_and_positive_target = deepcopy(payload)
        blocked_with_buy_and_positive_target["decision_status"] = "BLOCKED"
        self.assert_schema_rejects(blocked_with_buy_and_positive_target, schema_name)

        for fail_closed_status in ("DATA_FAIL_CLOSED", "MANUAL_PAUSE"):
            fail_closed_with_buy = deepcopy(payload)
            fail_closed_with_buy["decision_status"] = fail_closed_status
            self.assert_schema_rejects(fail_closed_with_buy, schema_name)

        non_blocked_with_failure = deepcopy(payload)
        non_blocked_with_failure["failure_codes"] = ["FORGED_FAILURE"]
        self.assert_schema_rejects(non_blocked_with_failure, schema_name)

        unexpected_buy_field = deepcopy(payload)
        unexpected_buy_field["buy_orders"][0]["bypass"] = True
        self.assert_schema_rejects(unexpected_buy_field, schema_name)

        forbidden_zero_position_weight = deepcopy(payload)
        forbidden_zero_position_weight["target_stock_weights"]["000001.SZ"] = "0.00"
        self.assert_schema_rejects(forbidden_zero_position_weight, schema_name)

        invalid_weight_property_name = deepcopy(payload)
        invalid_weight_property_name["target_stock_weights"]["not an instrument"] = (
            "0.10"
        )
        self.assert_schema_rejects(invalid_weight_property_name, schema_name)

        too_many_target_properties = deepcopy(payload)
        too_many_target_properties["target_stock_weights"].update(
            {"000002.SZ": "0.10", "000003.SZ": "0.10"}
        )
        self.assert_schema_rejects(too_many_target_properties, schema_name)

        duplicate_cancel_condition = deepcopy(payload)
        duplicate_cancel_condition["cancel_conditions"] = ["same", "same"]
        self.assert_schema_rejects(duplicate_cancel_condition, schema_name)

        impossible_date = deepcopy(payload)
        impossible_date["strategy_date"] = "2026-02-30"
        self.assert_schema_rejects(impossible_date, schema_name)

    def test_frozen_daily_schema_enforces_arrays_formats_and_nested_closure(self) -> None:
        schema_name = "frozen_daily_data.v2.json"
        payload = _frozen_daily_payload()
        validate_json_schema(payload, SCHEMA_ROOT / schema_name)

        missing = deepcopy(payload)
        missing.pop("alpha_snapshot_sha256")
        self.assert_schema_rejects(missing, schema_name)

        extra = deepcopy(payload)
        extra["caller_asserted_official"] = True
        self.assert_schema_rejects(extra, schema_name)

        too_many_metrics = deepcopy(payload)
        too_many_metrics["non_alpha_exposure_metrics"].append(
            deepcopy(too_many_metrics["non_alpha_exposure_metrics"][0])
        )
        self.assert_schema_rejects(too_many_metrics, schema_name)

        impossible_observation_date = deepcopy(payload)
        impossible_observation_date["non_alpha_exposure_metrics"][0][
            "observation_session"
        ] = "2026-13-01"
        self.assert_schema_rejects(impossible_observation_date, schema_name)

        duplicate_failure_code = deepcopy(payload)
        duplicate_failure_code["non_alpha_exposure_metrics"][0]["failure_codes"] = [
            "DUPLICATE",
            "DUPLICATE",
        ]
        self.assert_schema_rejects(duplicate_failure_code, schema_name)

        nested_extra = deepcopy(payload)
        nested_extra["instrument_rules"][0]["trust_me"] = True
        self.assert_schema_rejects(nested_extra, schema_name)

    def test_alpha_schema_accepts_real_output_and_rejects_invalid_rows(self) -> None:
        schema_name = "alpha_ranking.v2.json"
        payload = _json_payload(run_alpha_engine(_snapshot(), _model()).to_dict())
        self.assertIsInstance(payload, dict)
        validate_json_schema(payload, SCHEMA_ROOT / schema_name)

        missing = deepcopy(payload)
        missing["rows"][0].pop("instrument_id")
        self.assert_schema_rejects(missing, schema_name)

        nested_extra = deepcopy(payload)
        nested_extra["rows"][0]["future_label"] = 1.0
        self.assert_schema_rejects(nested_extra, schema_name)

        percentile_above_one = deepcopy(payload)
        percentile_above_one["rows"][0]["percentile"] = 1.01
        self.assert_schema_rejects(percentile_above_one, schema_name)

        no_rows = deepcopy(payload)
        no_rows["rows"] = []
        self.assert_schema_rejects(no_rows, schema_name)

        duplicate_exclusion = deepcopy(payload)
        duplicate_exclusion["rows"][0]["exclusion_codes"] = ["X", "X"]
        self.assert_schema_rejects(duplicate_exclusion, schema_name)

        offset_free_timestamp = deepcopy(payload)
        offset_free_timestamp["decision_at"] = "2026-08-18T16:00:00"
        self.assert_schema_rejects(offset_free_timestamp, schema_name)

    def test_exposure_schemas_enforce_anyof_bounds_and_closed_objects(self) -> None:
        input_schema = "exposure_input_snapshot.v1.json"
        decision_schema = "exposure_decision.v2.json"
        policy = _policy()
        inputs = _inputs(datetime(2026, 8, 18, 16, tzinfo=TZ))
        exposure = decide_exposure(inputs, policy, _memory(policy))
        input_payload = _json_payload(inputs.to_dict())
        decision_payload = _json_payload(exposure.to_dict())
        self.assertIsInstance(input_payload, dict)
        self.assertIsInstance(decision_payload, dict)
        validate_json_schema(input_payload, SCHEMA_ROOT / input_schema)
        validate_json_schema(decision_payload, SCHEMA_ROOT / decision_schema)

        missing = deepcopy(input_payload)
        missing["metrics"][0].pop("category")
        self.assert_schema_rejects(missing, input_schema)

        nested_extra = deepcopy(input_payload)
        nested_extra["metrics"][0]["uncontrolled_metric"] = 0.5
        self.assert_schema_rejects(nested_extra, input_schema)

        only_five_metrics = deepcopy(input_payload)
        only_five_metrics["metrics"] = only_five_metrics["metrics"][:-1]
        self.assert_schema_rejects(only_five_metrics, input_schema)

        no_anyof_branch = deepcopy(input_payload)
        no_anyof_branch["metrics"][0]["source_snapshot_sha256"] = "not-a-hash"
        self.assert_schema_rejects(no_anyof_branch, input_schema)

        negative_pending_count = deepcopy(decision_payload)
        negative_pending_count["pending_consecutive_sessions"] = -1
        self.assert_schema_rejects(negative_pending_count, decision_schema)

        duplicate_reason = deepcopy(decision_payload)
        duplicate_reason["reason_codes"] = ["SAME", "SAME"]
        self.assert_schema_rejects(duplicate_reason, decision_schema)

        unregistered_exposure = deepcopy(decision_payload)
        unregistered_exposure["target_gross_exposure"] = 0.7
        self.assert_schema_rejects(unregistered_exposure, decision_schema)

        decision_extra = deepcopy(decision_payload)
        decision_extra["paper_eligibility"] = True
        self.assert_schema_rejects(decision_extra, decision_schema)

    def test_next_session_schema_resolves_external_refs_and_closes_embedded_intent(self) -> None:
        selected_policy, construction, intent = build_alpha()
        calendar_receipt = receipt()
        signal = create_alpha_next_session_signal(
            intent=intent,
            construction=construction,
            policy=selected_policy,
            receipt=calendar_receipt,
            registry=registry(calendar_receipt),
            fees=canonical_fees(),
            instrument_rules=canonical_rules(),
        )
        payload = _json_payload(signal.to_dict())
        schema_name = "next_session_signal.v1.json"
        validate_json_schema(payload, SCHEMA_ROOT / schema_name)

        construction_payload = _json_payload(construction.to_dict())
        construction_schema = "portfolio_construction_result.v2.json"
        validate_json_schema(
            construction_payload,
            SCHEMA_ROOT / construction_schema,
        )
        risk_construction_with_buy = deepcopy(construction_payload)
        risk_construction_with_buy["intent_type"] = "RISK_OFF"
        self.assert_schema_rejects(
            risk_construction_with_buy,
            construction_schema,
        )

        alpha_buy_masquerading_as_risk = deepcopy(payload)
        alpha_buy_masquerading_as_risk["channel"] = (
            "RISK_REDUCTION_NEXT_SESSION"
        )
        self.assert_schema_rejects(
            alpha_buy_masquerading_as_risk,
            schema_name,
        )

        forged_embedded_intent = deepcopy(payload)
        forged_embedded_intent["portfolio_intent"]["bypass"] = True
        self.assert_schema_rejects(forged_embedded_intent, schema_name)

        positive_intent_without_weight = deepcopy(payload)
        positive_intent_without_weight["portfolio_intent"]["target_weights"] = {}
        self.assert_schema_rejects(positive_intent_without_weight, schema_name)


if __name__ == "__main__":
    unittest.main()
