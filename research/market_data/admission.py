"""Dataset-specific local admission; provider names never self-certify truth."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .contracts import MarketDataRequest


@dataclass(frozen=True)
class AdmissionDecision:
    admission_status: str
    point_in_time_status: str
    freshness_status: str
    issues: tuple[Mapping[str, Any], ...] = ()


def evaluate_admission(
    request: MarketDataRequest,
    *,
    provider_id: str,
    upstream_source: str,
    synthetic: bool,
    config: Mapping[str, Any],
) -> AdmissionDecision:
    """Recompute policy from dataset facts instead of trusting adapter flags."""

    issues: list[Mapping[str, Any]] = []
    providers = config.get("providers", {})
    provider_policy = providers.get(provider_id, {}) if isinstance(providers, Mapping) else {}
    if synthetic:
        return AdmissionDecision(
            "rejected_synthetic",
            "not_applicable",
            "not_assessed",
            ({"code": "synthetic_not_allowed", "severity": "error"},),
        )
    allowlist = config.get("provider_allowlist")
    if not isinstance(allowlist, list) or provider_id not in allowlist:
        return AdmissionDecision(
            "rejected_provider_not_allowlisted",
            "unknown",
            "not_assessed",
            ({"code": "provider_not_allowlisted", "severity": "error"},),
        )
    if not isinstance(provider_policy, Mapping) or provider_policy.get("enabled") is not True:
        return AdmissionDecision(
            "rejected_provider_disabled",
            "unknown",
            "not_assessed",
            ({"code": "provider_disabled", "severity": "error"},),
        )
    declared_datasets = provider_policy.get("datasets")
    if (
        not isinstance(declared_datasets, list)
        or request.dataset_type not in declared_datasets
    ):
        return AdmissionDecision(
            "rejected_provider_dataset_undeclared",
            "unknown",
            "not_assessed",
            (
                {
                    "code": "provider_dataset_undeclared",
                    "severity": "error",
                    "message": (
                        f"provider {provider_id} is not configured for "
                        f"dataset {request.dataset_type}"
                    ),
                },
            ),
        )
    allowed_by_dataset = provider_policy.get("allowed_upstream_sources")
    allowed_sources = (
        allowed_by_dataset.get(request.dataset_type)
        if isinstance(allowed_by_dataset, Mapping)
        else None
    )
    if (
        not isinstance(allowed_sources, list)
        or not allowed_sources
        or upstream_source not in allowed_sources
    ):
        return AdmissionDecision(
            "rejected_unexpected_upstream",
            "unknown",
            "not_assessed",
            (
                {
                    "code": "unexpected_upstream_source",
                    "severity": "error",
                    "message": (
                        f"upstream {upstream_source!r} is not allowed for "
                        f"{provider_id}/{request.dataset_type}"
                    ),
                },
            ),
        )
    licensed_choice_source = (
        provider_id == "choice"
        and provider_policy.get("source_access") == "licensed_read_only_sdk"
        and provider_policy.get("admission_role")
        == "licensed_optional_read_only_secondary"
    )
    if provider_id == "eastmoney_legacy" or (
        "eastmoney" in upstream_source.casefold() and not licensed_choice_source
    ):
        return AdmissionDecision(
            "diagnostic_only",
            "not_admitted",
            "not_assessed",
            ({"code": "legacy_upstream_not_admitted", "severity": "warning"},),
        )
    if licensed_choice_source:
        issues.append(
            {
                "code": "licensed_choice_source_not_official_truth",
                "severity": "info",
                "message": (
                    "licensed Choice SDK access is separate from Eastmoney public "
                    "legacy endpoints and does not authenticate official truth"
                ),
            }
        )
    if request.dataset_type == "industry_classification":
        return AdmissionDecision(
            "diagnostic_current_only",
            "diagnostic_current_only",
            "current_snapshot",
            ({"code": "industry_classification_not_point_in_time", "severity": "warning"},),
        )
    if request.dataset_type == "financial_indicator":
        return AdmissionDecision(
            "research_only_unless_disclosure_time_present",
            "research_only_not_pit",
            "not_assessed",
            ({"code": "disclosure_time_not_authenticated", "severity": "warning"},),
        )

    if request.retrieval_mode == "historical_backfill":
        point_in_time = "historical_backfill_not_original_capture"
        freshness = "historical_backfill"
        issues.append(
            {
                "code": "historical_backfill_not_original_capture",
                "severity": "info",
                "message": "current retrieval of history is not evidence of an original point-in-time capture",
            }
        )
    elif request.retrieval_mode == "offline_replay":
        point_in_time = "offline_replay_of_validated_capture"
        freshness = "replayed"
    else:
        point_in_time = (
            "current_snapshot_not_pit"
            if request.dataset_type == "security_master"
            else "policy_estimated_availability"
        )
        freshness = "live_capture"

    datasets = config.get("datasets", {})
    dataset_policy = datasets.get(request.dataset_type, {}) if isinstance(datasets, Mapping) else {}
    configured_primary = (
        str(dataset_policy.get("primary") or "")
        if isinstance(dataset_policy, Mapping)
        else ""
    )
    if configured_primary and configured_primary != provider_id:
        admission = "validated_secondary_not_primary"
    elif provider_id == "baostock":
        admission = "validated_research_only"
    else:
        admission = "validated_optional_source"
    issues.append(
        {
            "code": "provider_name_not_truth_authentication",
            "severity": "info",
            "message": "provider identity and content hashes do not authenticate official truth",
        }
    )
    return AdmissionDecision(admission, point_in_time, freshness, tuple(issues))
