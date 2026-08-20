"""Current, evidence-bound status for the quality-growth implementation.

This is intentionally separate from the future Choice capability receipt.  It
summarises real probe artifacts without promoting a failed login, a supported
SDK method, or a caller-provided flag into a data-admission claim.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

from .contracts import canonical_sha256
from .policy import QualityGrowthPolicy


RUNTIME_STATUSES = frozenset(
    {"passed", "dependency_missing", "network_blocked", "not_configured", "failed"}
)
FORMAL_BLOCKED_STATUS = "blocked_missing_pit_data"
FALLBACK_BLOCKED_STATUS = "blocked_missing_current_universe_or_complete_price_panel"

UNIMPLEMENTED_CONTROLLED_CAPABILITIES = (
    "csi800_total_return_benchmark_contract",
    "pit_csi_level1_industry",
    "pit_float_market_cap",
    "historical_st_status",
    "historical_suspension_status",
    "historical_limit_up_down_status",
    "first_disclosure_financials",
    "first_disclosure_operating_profit",
    "first_disclosure_gross_profit",
    "controlled_top_decile_price_bar_bundle",
    "controlled_top2_execution_bar_bundle",
)

UNIMPLEMENTED_FORWARD_PAPER_CAPABILITIES = (
    "source_authenticated_forward_signal_adapter",
    "official_controlled_paper_calendar_adapter",
    "daily_pit_nav_and_drawdown_marks",
    "sticky_drawdown_freeze_with_exit_retry",
    "standard_stage_a_certificate_artifact_verification",
)


class StatusArtifactError(ValueError):
    """Raised when a probe artifact does not match the controlled probe shape."""


def _sha256(raw: bytes) -> str:
    return sha256(raw).hexdigest()


def _load_object(path: Path | str) -> tuple[dict[str, Any], str]:
    source = Path(path)
    try:
        raw = source.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StatusArtifactError(f"cannot read probe artifact: {source}") from exc
    if not isinstance(payload, dict):
        raise StatusArtifactError("probe artifact root must be an object")
    return payload, _sha256(raw)


def _status(value: Any) -> str:
    result = str(value or "").strip()
    if result not in RUNTIME_STATUSES:
        raise StatusArtifactError("probe status is unsupported")
    return result


def _market_probe(path: Path | str, *, dataset: str) -> dict[str, Any]:
    payload, artifact_sha = _load_object(path)
    if payload.get("probe_version") != "market-data-probe-v1":
        raise StatusArtifactError("market probe_version mismatch")
    if payload.get("provider_id") != "choice" or payload.get("dataset_type") != dataset:
        raise StatusArtifactError("market probe provider or dataset mismatch")
    if payload.get("evidence_mode") != "real_provider":
        raise StatusArtifactError("market probe must be real_provider evidence")
    if dataset == "daily_bar" and payload.get("adjustment") != "qfq":
        raise StatusArtifactError("Choice stock daily probe must use qfq")
    return {
        "artifact_sha256": artifact_sha,
        "probe_version": payload["probe_version"],
        "provider_id": "choice",
        "adapter_version": payload.get("adapter_version"),
        "dataset_type": dataset,
        "status": _status(payload.get("status")),
        "error_code": payload.get("error_code"),
        "record_count": payload.get("record_count"),
        "request_fingerprint": payload.get("request_fingerprint"),
        "admission_status": payload.get("admission_status"),
        "point_in_time_status": payload.get("point_in_time_status"),
    }


def _sector_probe(path: Path | str) -> dict[str, Any]:
    payload, artifact_sha = _load_object(path)
    if payload.get("probe_version") != "choice-candidate-probe-v1":
        raise StatusArtifactError("sector probe_version mismatch")
    if (
        payload.get("provider_id") != "choice"
        or payload.get("query_type") != "historical_sector_membership"
        or payload.get("mode") != "online"
    ):
        raise StatusArtifactError("sector probe contract mismatch")
    return {
        "artifact_sha256": artifact_sha,
        "probe_version": payload["probe_version"],
        "provider_id": "choice",
        "adapter_version": payload.get("adapter_version"),
        "query_type": payload["query_type"],
        "status": _status(payload.get("status")),
        "record_count": payload.get("record_count"),
        "request_fingerprint": payload.get("request_fingerprint"),
        "admission_status": payload.get("admission_status"),
        "point_in_time_status": payload.get("point_in_time_status"),
        "formal_truth_eligible": payload.get("formal_truth_eligible"),
        "failure_codes": sorted(
            {
                str(item.get("code"))
                for item in payload.get("issues", [])
                if isinstance(item, Mapping) and item.get("code")
            }
        ),
    }


@dataclass(frozen=True)
class QualityGrowthCurrentStatus:
    policy_sha256: str
    runtime_probes: Mapping[str, Mapping[str, Any]]
    formal_status: str = FORMAL_BLOCKED_STATUS
    fallback_status: str = FALLBACK_BLOCKED_STATUS

    def __post_init__(self) -> None:
        if self.formal_status != FORMAL_BLOCKED_STATUS:
            raise StatusArtifactError("current status cannot promote the formal strategy")
        if self.fallback_status != FALLBACK_BLOCKED_STATUS:
            raise StatusArtifactError("current incomplete probes cannot promote the fallback")

    @property
    def status_sha256(self) -> str:
        return canonical_sha256(self.to_dict(include_hash=False))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload = {
            "schema_version": "strategy-workspace-quality-growth-current-status.v1",
            "strategy_id": "a-share-small-account-quality-growth-v1",
            "formal_status": self.formal_status,
            "fallback_status": self.fallback_status,
            "policy_sha256": self.policy_sha256,
            "runtime_probes": {
                key: dict(self.runtime_probes[key]) for key in sorted(self.runtime_probes)
            },
            "controlled_adapter_missing_capabilities": list(
                UNIMPLEMENTED_CONTROLLED_CAPABILITIES
            ),
            "forward_paper_missing_capabilities": list(
                UNIMPLEMENTED_FORWARD_PAPER_CAPABILITIES
            ),
            "interpretation": {
                "sdk_or_method_presence_is_not_admission": True,
                "old_cache_cannot_replace_current_probe": True,
                "formal_backtest_run": False,
                "fallback_diagnostic_run": False,
                "append_only_paper_accounting_ledger_implemented": True,
                "manual_real_money_candidate_reachable": False,
            },
            "safety": {
                "paper_eligibility": False,
                "trade_eligibility": False,
                "real_money_list_allowed": False,
                "live": "not_supported",
            },
        }
        if include_hash:
            payload["status_sha256"] = self.status_sha256
        return payload


def build_current_status(
    policy: QualityGrowthPolicy,
    *,
    daily_bar_probe: Path | str,
    trade_calendar_probe: Path | str,
    historical_sector_probe: Path | str,
) -> QualityGrowthCurrentStatus:
    """Bind the three executed probes; missing formal capabilities remain blocked."""

    if not isinstance(policy, QualityGrowthPolicy):
        raise StatusArtifactError("policy must be a validated QualityGrowthPolicy")
    probes = {
        "qfq_daily_bar": _market_probe(daily_bar_probe, dataset="daily_bar"),
        "trade_calendar": _market_probe(trade_calendar_probe, dataset="trade_calendar"),
        "historical_sector_membership": _sector_probe(historical_sector_probe),
    }
    return QualityGrowthCurrentStatus(
        policy_sha256=policy.policy_sha256,
        runtime_probes=probes,
    )


__all__ = [
    "FALLBACK_BLOCKED_STATUS",
    "FORMAL_BLOCKED_STATUS",
    "RUNTIME_STATUSES",
    "UNIMPLEMENTED_CONTROLLED_CAPABILITIES",
    "UNIMPLEMENTED_FORWARD_PAPER_CAPABILITIES",
    "QualityGrowthCurrentStatus",
    "StatusArtifactError",
    "build_current_status",
]
