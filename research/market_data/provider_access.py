"""Versioned, fail-closed access policy for external market-data providers.

This boundary answers whether a provider may be contacted or newly consumed;
it does not change the evidentiary or research-admission status of any data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .providers.base import (
    ProviderAccessExpiredError,
    ProviderAccessPolicyInvalidError,
)
from .validation import SchemaValidationError, validate_json_schema


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROVIDER_ACCESS_POLICY_PATH = REPO_ROOT / "configs" / "provider_access.v1.json"
PROVIDER_ACCESS_POLICY_SCHEMA_PATH = (
    REPO_ROOT / "schemas" / "provider_access_policy.v1.json"
)
SCHEMA_VERSION = "provider-access-policy-v1"


class ProviderAccessPolicyError(ValueError):
    """The versioned access-policy file is missing, malformed, or unsupported."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ProviderAccessPolicyError(
                f"duplicate key in provider access policy: {key!r}"
            )
        value[key] = item
    return value


def _reject_nonfinite(token: str) -> None:
    raise ProviderAccessPolicyError(
        f"non-finite number in provider access policy: {token}"
    )


@dataclass(frozen=True)
class ChoiceAccessPolicy:
    access_status: str
    network_fetch_allowed: bool
    diagnostic_session_allowed: bool
    offline_research_consumption_allowed: bool
    historical_evidence_preserved: bool
    automatic_fallback_allowed: bool
    partial_fallback_allowed: bool


@dataclass(frozen=True)
class TushareAccessPolicy:
    access_status: str
    capability_probe_allowed: bool
    formal_provider_allowed: bool
    automatic_fallback_allowed: bool
    partial_fallback_allowed: bool


@dataclass(frozen=True)
class ProviderAccessPolicy:
    schema_version: str
    choice: ChoiceAccessPolicy
    tushare: TushareAccessPolicy


def load_provider_access_policy(
    path: Path | str = DEFAULT_PROVIDER_ACCESS_POLICY_PATH,
) -> ProviderAccessPolicy:
    """Load and validate the strict V1 policy without any environment override."""

    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = REPO_ROOT / resolved
    try:
        payload = json.loads(
            resolved.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_nonfinite,
        )
    except ProviderAccessPolicyError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderAccessPolicyError(
            f"cannot load provider access policy: {type(exc).__name__}"
        ) from exc
    if not isinstance(payload, dict):
        raise ProviderAccessPolicyError("provider access policy must be an object")
    try:
        validate_json_schema(payload, PROVIDER_ACCESS_POLICY_SCHEMA_PATH)
    except (OSError, SchemaValidationError) as exc:
        raise ProviderAccessPolicyError(
            f"provider access policy failed V1 schema validation: {exc}"
        ) from exc

    choice = payload["choice"]
    tushare = payload["tushare"]
    # These checks deliberately duplicate the security-critical schema consts.
    # A future access change must introduce and consume a new policy version.
    if (
        payload.get("schema_version") != SCHEMA_VERSION
        or choice.get("access_status") != "expired"
        or choice.get("network_fetch_allowed") is not False
        or choice.get("diagnostic_session_allowed") is not False
        or choice.get("offline_research_consumption_allowed") is not False
        or choice.get("historical_evidence_preserved") is not True
        or choice.get("automatic_fallback_allowed") is not False
        or choice.get("partial_fallback_allowed") is not False
    ):
        raise ProviderAccessPolicyError(
            "Choice V1 access policy must preserve evidence and deny all new access"
        )
    if (
        tushare.get("access_status") != "capability_probe_only"
        or tushare.get("capability_probe_allowed") is not True
        or tushare.get("formal_provider_allowed") is not False
        or tushare.get("automatic_fallback_allowed") is not False
        or tushare.get("partial_fallback_allowed") is not False
    ):
        raise ProviderAccessPolicyError(
            "Tushare V1 access policy must remain capability-probe-only"
        )
    return ProviderAccessPolicy(
        schema_version=SCHEMA_VERSION,
        choice=ChoiceAccessPolicy(**choice),
        tushare=TushareAccessPolicy(**tushare),
    )


def _load_choice_policy_or_fail_closed() -> ChoiceAccessPolicy:
    try:
        return load_provider_access_policy().choice
    except ProviderAccessPolicyError as exc:
        raise ProviderAccessPolicyInvalidError(
            "provider access policy is unavailable or invalid"
        ) from exc


def require_choice_network_access(operation: str) -> None:
    """Reject every new Choice SDK import, start, or network operation."""

    policy = _load_choice_policy_or_fail_closed()
    if policy.access_status == "expired" or not policy.network_fetch_allowed:
        raise ProviderAccessExpiredError(provider_id="choice", operation=operation)


def require_choice_diagnostic_session(operation: str = "diagnostic_session") -> None:
    """Reject opening a new Choice diagnostic session before SDK loading."""

    policy = _load_choice_policy_or_fail_closed()
    if policy.access_status == "expired" or not policy.diagnostic_session_allowed:
        raise ProviderAccessExpiredError(provider_id="choice", operation=operation)


def require_choice_offline_research_consumption(
    operation: str = "offline_research_consumption",
) -> None:
    """Keep preserved Choice evidence out of new formal research consumption."""

    policy = _load_choice_policy_or_fail_closed()
    if (
        policy.access_status == "expired"
        or not policy.offline_research_consumption_allowed
    ):
        raise ProviderAccessExpiredError(provider_id="choice", operation=operation)


__all__ = [
    "DEFAULT_PROVIDER_ACCESS_POLICY_PATH",
    "PROVIDER_ACCESS_POLICY_SCHEMA_PATH",
    "ChoiceAccessPolicy",
    "ProviderAccessPolicy",
    "ProviderAccessPolicyError",
    "TushareAccessPolicy",
    "load_provider_access_policy",
    "require_choice_diagnostic_session",
    "require_choice_network_access",
    "require_choice_offline_research_consumption",
]
