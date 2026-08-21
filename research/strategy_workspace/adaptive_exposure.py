"""Immutable P0 policy loader for adaptive-exposure strategy V2.

This module only validates and freezes the versioned policy.  It does not
implement an exposure model, produce a PortfolioIntent, grant Paper admission,
or authorize any execution mode.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .contracts import canonical_sha256


DEFAULT_ADAPTIVE_EXPOSURE_POLICY_PATH = Path(
    "configs/strategy_adaptive_exposure.v2.json"
)
ADAPTIVE_EXPOSURE_SCHEMA_VERSION = "strategy-adaptive-exposure-policy.v2"
ADAPTIVE_EXPOSURE_STRATEGY_ID = "a-share-small-account-adaptive-exposure-v2"
FROZEN_ADAPTIVE_EXPOSURE_POLICY_SHA256 = (
    "4e66ae5c769c5151c7592ad9accf637f84132bffdba5cc85d0fef87ee351f9a4"
)

EXPECTED_EXPOSURE_STATES = {
    "RISK_OFF": "0.00",
    "DEFENSIVE": "0.30",
    "NEUTRAL": "0.60",
    "RISK_ON": "1.00",
}
EXPECTED_REPORTING_FLAGS = (
    "is_guarantee",
    "is_model_loss",
    "is_admission_gate",
    "is_parameter_optimization_target",
)
EXPECTED_SAFETY_FALSE_FIELDS = (
    "paper_eligibility",
    "trade_eligibility",
    "real_money_list_allowed",
    "automatic_order_submission",
)


class AdaptiveExposurePolicyError(ValueError):
    """Raised whenever the frozen adaptive-exposure P0 contract drifts."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AdaptiveExposurePolicyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AdaptiveExposurePolicyError(f"{field_name} must be an object")
    return value


def _decimal(value: Any, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise AdaptiveExposurePolicyError(f"{field_name} must be decimal") from exc
    if not result.is_finite():
        raise AdaptiveExposurePolicyError(f"{field_name} must be finite")
    return result


def _require_equal(actual: Any, expected: Any, field_name: str) -> None:
    if actual != expected:
        raise AdaptiveExposurePolicyError(
            f"{field_name} must remain {expected!r}"
        )


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _validate_payload(payload: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    try:
        normalized = _thaw(payload)
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
        )
        normalized = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise AdaptiveExposurePolicyError(
            "adaptive-exposure policy must contain only canonical JSON values"
        ) from exc
    if not isinstance(normalized, dict):
        raise AdaptiveExposurePolicyError("adaptive-exposure policy root must be an object")

    _require_equal(
        normalized.get("schema_version"),
        ADAPTIVE_EXPOSURE_SCHEMA_VERSION,
        "schema_version",
    )
    _require_equal(
        normalized.get("strategy_id"),
        ADAPTIVE_EXPOSURE_STRATEGY_ID,
        "strategy_id",
    )
    _require_equal(
        normalized.get("contract_status"),
        "p0_runtime_implemented_not_admitted",
        "contract_status",
    )
    _require_equal(
        normalized.get("research_status"),
        "blocked_missing_pit_data",
        "research_status",
    )
    _require_equal(
        normalized.get("execution_status"),
        "research_only",
        "execution_status",
    )

    compatibility = _mapping(normalized.get("v1_compatibility"), "v1_compatibility")
    _require_equal(
        compatibility.get("strategy_id"),
        "a-share-small-account-quality-growth-v1",
        "v1_compatibility.strategy_id",
    )
    _require_equal(
        compatibility.get("policy"),
        "preserve_unchanged_read_only_compatibility",
        "v1_compatibility.policy",
    )
    _require_equal(
        compatibility.get("historical_results_transferable"),
        False,
        "v1_compatibility.historical_results_transferable",
    )

    challenge = _mapping(normalized.get("challenge"), "challenge")
    _require_equal(challenge.get("metric"), "monthly_net_return", "challenge.metric")
    _require_equal(
        _decimal(challenge.get("target"), "challenge.target"),
        Decimal("0.10"),
        "challenge.target",
    )
    _require_equal(challenge.get("role"), "reporting_only", "challenge.role")
    for field_name in EXPECTED_REPORTING_FLAGS:
        _require_equal(
            challenge.get(field_name),
            False,
            f"challenge.{field_name}",
        )

    architecture = _mapping(normalized.get("architecture"), "architecture")
    _require_equal(
        architecture.get("layers"),
        [
            "alpha_engine",
            "exposure_engine",
            "portfolio_constructor",
            "execution_and_risk",
        ],
        "architecture.layers",
    )
    _require_equal(
        architecture.get("alpha_exposure_models_separate"),
        True,
        "architecture.alpha_exposure_models_separate",
    )
    _require_equal(
        architecture.get("alpha_refresh_sessions"),
        20,
        "architecture.alpha_refresh_sessions",
    )
    _require_equal(
        architecture.get("risk_evaluation_frequency"),
        "each_controlled_session_close",
        "architecture.risk_evaluation_frequency",
    )

    portfolio = _mapping(normalized.get("portfolio"), "portfolio")
    _require_equal(
        _decimal(portfolio.get("target_gross_exposure_min"), "portfolio.target_gross_exposure_min"),
        Decimal("0"),
        "portfolio.target_gross_exposure_min",
    )
    maximum_exposure = _decimal(
        portfolio.get("target_gross_exposure_max"),
        "portfolio.target_gross_exposure_max",
    )
    _require_equal(maximum_exposure, Decimal("1"), "portfolio.target_gross_exposure_max")
    states = _mapping(portfolio.get("exposure_states"), "portfolio.exposure_states")
    _require_equal(dict(states), EXPECTED_EXPOSURE_STATES, "portfolio.exposure_states")
    _require_equal(portfolio.get("max_positions"), 3, "portfolio.max_positions")
    _require_equal(
        _decimal(portfolio.get("max_position_weight"), "portfolio.max_position_weight"),
        Decimal("0.40"),
        "portfolio.max_position_weight",
    )
    maximum_total = _decimal(
        portfolio.get("maximum_total_weight"),
        "portfolio.maximum_total_weight",
    )
    if maximum_total > Decimal("1"):
        raise AdaptiveExposurePolicyError(
            "portfolio.maximum_total_weight must not exceed 1"
        )
    _require_equal(maximum_total, Decimal("1"), "portfolio.maximum_total_weight")
    _require_equal(
        _decimal(portfolio.get("minimum_cash_weight"), "portfolio.minimum_cash_weight"),
        Decimal("0"),
        "portfolio.minimum_cash_weight",
    )
    _require_equal(
        portfolio.get("leverage_allowed"), False, "portfolio.leverage_allowed"
    )
    _require_equal(
        portfolio.get("short_selling_allowed"),
        False,
        "portfolio.short_selling_allowed",
    )

    risk = _mapping(normalized.get("risk"), "risk")
    _require_equal(
        _decimal(risk.get("account_drawdown_trigger"), "risk.account_drawdown_trigger"),
        Decimal("0.12"),
        "risk.account_drawdown_trigger",
    )
    expected_risk = {
        "trigger_meaning": "risk_trigger_not_maximum_loss_guarantee",
        "trigger_observation": "controlled_session_close",
        "trigger_intent_type": "ACCOUNT_DRAWDOWN_EXIT",
        "first_exit_attempt": "next_controlled_session_open",
        "residual_exit_retry": "each_following_controlled_session",
        "ordinary_state_change_requires_hysteresis": True,
        "hard_risk_exit_bypasses_hysteresis": True,
        "hysteresis_parameters_status": "blocked_pending_preregistration",
    }
    for field_name, expected in expected_risk.items():
        _require_equal(risk.get(field_name), expected, f"risk.{field_name}")

    experiment = _mapping(normalized.get("experiment_policy"), "experiment_policy")
    expected_experiment = {
        "train": ["2018-01-01", "2022-12-31"],
        "validation": ["2023-01-01", "2023-12-31"],
        "locked_test": ["2024-01-01", "2025-12-31"],
        "locked_test_run_policy": "single_controlled_run_then_consumed",
        "pre_freeze_2026_status": "retrospective_consumed",
        "forward_start_rule": "next_controlled_trading_session_after_spec_freeze",
        "core_parameter_change_policy": "new_strategy_version_required",
    }
    for field_name, expected in expected_experiment.items():
        _require_equal(
            experiment.get(field_name),
            expected,
            f"experiment_policy.{field_name}",
        )

    paper_ledger = _mapping(
        normalized.get("paper_ledger_contract"), "paper_ledger_contract"
    )
    expected_paper_ledger = {
        "schema_version": "strategy-paper-ledger-record.v2",
        "record_types": ["header", "daily_session"],
        "frequency": "each_controlled_session",
        "append_policy": "same_session_close_append_only_no_backfill",
        "state_derivation": (
            "previous_state_plus_evidence_bound_fills_and_close_marks"
        ),
        "drawdown_latch": "sticky_for_ledger_lifetime_no_reentry",
        "reset_or_new_ledger_orchestration": "not_implemented",
        "live_supported": False,
    }
    for field_name, expected in expected_paper_ledger.items():
        _require_equal(
            paper_ledger.get(field_name),
            expected,
            f"paper_ledger_contract.{field_name}",
        )

    safety = _mapping(normalized.get("safety"), "safety")
    for field_name in EXPECTED_SAFETY_FALSE_FIELDS:
        _require_equal(safety.get(field_name), False, f"safety.{field_name}")
    _require_equal(safety.get("live"), "not_supported", "safety.live")

    try:
        digest = canonical_sha256(normalized)
    except (TypeError, ValueError) as exc:
        raise AdaptiveExposurePolicyError(
            "adaptive-exposure policy cannot be canonically hashed"
        ) from exc
    if digest != FROZEN_ADAPTIVE_EXPOSURE_POLICY_SHA256:
        raise AdaptiveExposurePolicyError(
            "adaptive-exposure policy drifted from the frozen P0 content hash"
        )
    return normalized, digest


@dataclass(frozen=True, slots=True)
class AdaptiveExposurePolicy:
    """Recursively immutable, hash-bound representation of the P0 policy."""

    raw: Mapping[str, Any] = field(repr=False)
    policy_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        normalized, digest = _validate_payload(self.raw)
        object.__setattr__(self, "raw", _freeze(normalized))
        object.__setattr__(self, "policy_sha256", digest)

    @property
    def strategy_id(self) -> str:
        return str(self.raw["strategy_id"])

    @property
    def research_status(self) -> str:
        return str(self.raw["research_status"])

    @property
    def challenge_target(self) -> Decimal:
        return Decimal(str(self.raw["challenge"]["target"]))

    @property
    def exposure_states(self) -> tuple[Decimal, ...]:
        states = self.raw["portfolio"]["exposure_states"]
        return tuple(Decimal(str(states[name])) for name in EXPECTED_EXPOSURE_STATES)

    @property
    def paper_eligible(self) -> bool:
        return bool(self.raw["safety"]["paper_eligibility"])

    @property
    def trade_eligible(self) -> bool:
        return bool(self.raw["safety"]["trade_eligibility"])

    @property
    def live_supported(self) -> bool:
        return False

    def to_dict(self) -> dict[str, Any]:
        return _thaw(self.raw)


def validate_adaptive_exposure_policy(
    payload: Mapping[str, Any],
) -> AdaptiveExposurePolicy:
    if not isinstance(payload, Mapping):
        raise AdaptiveExposurePolicyError(
            "adaptive-exposure policy root must be an object"
        )
    return AdaptiveExposurePolicy(payload)


def load_adaptive_exposure_policy(
    path: Path | str = DEFAULT_ADAPTIVE_EXPOSURE_POLICY_PATH,
) -> AdaptiveExposurePolicy:
    try:
        payload = json.loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object,
        )
    except AdaptiveExposurePolicyError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdaptiveExposurePolicyError(
            f"cannot read adaptive-exposure policy: {path}"
        ) from exc
    return validate_adaptive_exposure_policy(payload)


__all__ = [
    "ADAPTIVE_EXPOSURE_SCHEMA_VERSION",
    "ADAPTIVE_EXPOSURE_STRATEGY_ID",
    "DEFAULT_ADAPTIVE_EXPOSURE_POLICY_PATH",
    "FROZEN_ADAPTIVE_EXPOSURE_POLICY_SHA256",
    "AdaptiveExposurePolicy",
    "AdaptiveExposurePolicyError",
    "load_adaptive_exposure_policy",
    "validate_adaptive_exposure_policy",
]
