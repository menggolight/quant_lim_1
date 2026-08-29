from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import research.strategy_workspace.daily_signal_publication as daily_signal_publication_module
from research.market_data.validation import (
    SchemaValidationError,
    validate_json_schema,
)
from research.strategy_workspace.alpha_engine_v2 import run_alpha_engine
from research.strategy_workspace.contracts import canonical_json_bytes
from research.strategy_workspace.exposure_engine_v2 import decide_exposure
from research.strategy_workspace.next_session_signal import (
    create_alpha_next_session_signal,
    create_risk_next_session_signal,
    NextSessionSignalError,
)
from tests.test_alpha_engine_v2 import _model, _snapshot
from tests.test_daily_pipeline import decision as daily_decision
from tests.test_exposure_engine_v2 import TZ, _inputs, _memory, _policy
from tests.test_next_session_signal import (
    admission_receipt,
    build_alpha,
    build_risk,
    canonical_fees,
    canonical_rules,
    publish_risk_bundle,
    receipt,
    registry,
)
from tests.test_portfolio_constructor_v2 import policy as constructor_policy


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

    def test_experiment_v3_receipt_and_policy_v2_schemas_reject_legacy_or_drift(self) -> None:
        exposure_policy = _policy()
        constructor = constructor_policy(no_trade="0")
        receipt = exposure_policy.policy_admission_receipt

        validate_json_schema(
            _json_payload(receipt.to_dict()),
            SCHEMA_ROOT / "experiment_v3_admission_receipt.v1.json",
        )
        validate_json_schema(
            _json_payload(exposure_policy.to_dict()),
            SCHEMA_ROOT / "exposure_hysteresis_policy.v2.json",
        )
        constructor_payload = _json_payload(constructor.to_dict())
        validate_json_schema(
            constructor_payload,
            SCHEMA_ROOT / "portfolio_constructor_policy.v2.json",
        )

        legacy = deepcopy(constructor_payload)
        legacy["schema_version"] = "portfolio-constructor-policy.v1"
        self.assert_schema_rejects(
            legacy, "portfolio_constructor_policy.v2.json"
        )
        missing_receipt = deepcopy(constructor_payload)
        missing_receipt.pop("policy_admission_receipt_sha256")
        self.assert_schema_rejects(
            missing_receipt, "portfolio_constructor_policy.v2.json"
        )
        non_positive_entry = deepcopy(constructor_payload)
        non_positive_entry["entry_predicted_return_min"] = "0"
        self.assert_schema_rejects(
            non_positive_entry, "portfolio_constructor_policy.v2.json"
        )

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
        selected_policy, construction, intent = build_risk()
        calendar_receipt = receipt()
        controlled_registry = registry(calendar_receipt)
        with tempfile.TemporaryDirectory() as directory, patch.object(
            daily_signal_publication_module,
            "DAILY_SIGNAL_PUBLICATION_REGISTRY_ROOT",
            Path(directory) / "fixed-daily-publication-registry",
        ):
            publish_risk_bundle(
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
        payload = _json_payload(signal.to_dict())
        schema_name = "next_session_signal.v2.json"
        validate_json_schema(payload, SCHEMA_ROOT / schema_name)

        alpha_policy, alpha_construction, alpha_intent = build_alpha()
        with self.assertRaisesRegex(
            NextSessionSignalError,
            "formal Experiment V3 loader is blocked_not_implemented",
        ):
            create_alpha_next_session_signal(
                intent=alpha_intent,
                construction=alpha_construction,
                policy=alpha_policy,
                experiment_v3_admission_receipt=admission_receipt(),
                receipt=calendar_receipt,
                registry=controlled_registry,
                fees=canonical_fees(),
                instrument_rules=canonical_rules(),
            )

        construction_payload = _json_payload(construction.to_dict())
        construction_schema = "portfolio_construction_result.v2.json"
        validate_json_schema(
            construction_payload,
            SCHEMA_ROOT / construction_schema,
        )
        risk_construction_with_buy = deepcopy(construction_payload)
        risk_construction_with_buy["actions"][0]["action"] = "BUY"
        self.assert_schema_rejects(
            risk_construction_with_buy,
            construction_schema,
        )

        risk_sell_masquerading_as_alpha = deepcopy(payload)
        risk_sell_masquerading_as_alpha["channel"] = "ALPHA_NEXT_SESSION"
        self.assert_schema_rejects(
            risk_sell_masquerading_as_alpha,
            schema_name,
        )

        forged_embedded_intent = deepcopy(payload)
        forged_embedded_intent["portfolio_intent"]["bypass"] = True
        self.assert_schema_rejects(forged_embedded_intent, schema_name)

        defensive_intent_without_positive_weight = deepcopy(payload)
        defensive_intent_without_positive_weight["portfolio_intent"][
            "intent_type"
        ] = "DEFENSIVE_REDUCTION"
        self.assert_schema_rejects(
            defensive_intent_without_positive_weight,
            schema_name,
        )

    def test_tushare_v3_response_and_quarantine_schemas_close_transport_metadata(self) -> None:
        locked = {
            "access": "NOT_ACCESSED",
            "download": "NOT_DOWNLOADED",
            "run": "NOT_RUN",
        }
        receipt = {
            "observed_root_fields": ["code", "data", "detail", "msg"],
            "semantic_core_fields": ["code", "data", "msg"],
            "transport_extension_field_names": ["detail"],
            "transport_extension_type_by_field": {"detail": "object"},
            "transport_extension_value_sha256_by_field": {"detail": SHA},
            "transport_extensions_sha256": SHA,
            "transport_extensions_byte_count": 24,
            "raw_transport_sha256": SHA,
            "token_leak_check": "PASSED",
        }
        response = {
            "schema_version": "tushare-alpha-feasibility-task-response.v3",
            "state": "RESPONSE_VALIDATED",
            "task_id": f"index_weight-{SHA}",
            "endpoint": "index_weight",
            "plan_sha256": SHA,
            "raw_response_sha256": SHA,
            "wire_response_sha256": SHA,
            "transport_receipt": receipt,
            "raw_response_persisted": False,
            "normalized_rows_sha256": SHA,
            "normalized_content_sha256": SHA,
            "row_count": 0,
            "isolated_future_delist_date_count": 0,
            "isolated_non_union_row_count": 0,
            "rows": [],
            "locked_test_status": locked,
            "locked_test_consumed": False,
            "response_artifact_sha256": SHA,
        }
        response_schema = "tushare_alpha_feasibility_task_response.v3.json"
        validate_json_schema(response, SCHEMA_ROOT / response_schema)

        response_extra = deepcopy(response)
        response_extra["transport_receipt"]["detail"] = {"raw": "forbidden"}
        self.assert_schema_rejects(response_extra, response_schema)

        response_unsafe_key = deepcopy(response)
        response_unsafe_key["transport_receipt"][
            "transport_extension_type_by_field"
        ] = {"bad key": "string"}
        self.assert_schema_rejects(response_unsafe_key, response_schema)

        quarantine = {
            "schema_version": "tushare-alpha-feasibility-quarantine.v3",
            "state": "RESPONSE_QUARANTINED",
            "task_id": f"index_weight-{SHA}",
            "endpoint": "index_weight",
            "plan_sha256": SHA,
            "reason": "transport_extension_secret_detected",
            "failure_code": "transport_extension_secret_detected",
            "raw_transport_sha256": SHA,
            "http_status": 200,
            "response_byte_count": 128,
            "observed_root_fields": ["code", "data", "msg", "trace_id"],
            "semantic_core_fields": ["code", "data", "msg"],
            "missing_semantic_core_fields": [],
            "transport_extension_field_names": ["trace_id"],
            "transport_extension_type_by_field": {"trace_id": "string"},
            "transport_extension_value_sha256_by_field": {"trace_id": SHA},
            "transport_extensions_sha256": SHA,
            "transport_extensions_byte_count": 18,
            "upstream_code": None,
            "upstream_error_category": None,
            "data_failure_category": None,
            "token_leak_check": "PASSED",
            "raw_response_persisted": False,
            "locked_test_status": locked,
            "locked_test_consumed": False,
        }
        quarantine_schema = "tushare_alpha_feasibility_quarantine.v3.json"
        validate_json_schema(quarantine, SCHEMA_ROOT / quarantine_schema)

        quarantine_extra = deepcopy(quarantine)
        quarantine_extra["detail_value"] = "must never persist"
        self.assert_schema_rejects(quarantine_extra, quarantine_schema)

    def test_tushare_v2_experiment_defers_stock_basic_and_caps_response(self) -> None:
        config = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "configs"
                / "a_share_technical_alpha_feasibility.v2.json"
            ).read_text(encoding="utf-8")
        )
        schema_name = "technical_alpha_feasibility_experiment.v2.json"
        validate_json_schema(config, SCHEMA_ROOT / schema_name)
        self.assertNotIn("stock_basic", config["source"]["allowed_endpoints"])
        self.assertNotIn("stock_basic", config["requests"])
        self.assertEqual(config["stock_basic_request_count"], 0)
        oversized = deepcopy(config)
        oversized["source"]["maximum_response_bytes"] = 2 * 1024 * 1024 + 1
        self.assert_schema_rejects(oversized, schema_name)


if __name__ == "__main__":
    unittest.main()
