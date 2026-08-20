"""Controlled AKShare extension skeleton with no arbitrary function execution."""

from __future__ import annotations

from ..contracts import MarketDataRequest
from .base import ProviderNotConfiguredError, ProviderPayload


class AKShareProvider:
    provider_id = "akshare"
    upstream_source = "unconfigured"
    adapter_version = "akshare-adapter-v1"
    supported_datasets = frozenset()

    @staticmethod
    def validate_dataset_declaration(
        *,
        api_name: str,
        upstream_source: str,
        admission_status: str,
    ) -> None:
        api = str(api_name).strip().casefold()
        upstream = str(upstream_source).strip().casefold()
        admission = str(admission_status).strip().casefold()
        if not api or not upstream:
            raise ValueError("AKShare dataset declarations require an API and real upstream")
        if api.endswith("_em") or "eastmoney" in upstream or "东方财富" in upstream:
            if admission not in {"diagnostic_only", "not_admitted"}:
                raise ValueError("Eastmoney-backed AKShare interfaces cannot enter an admitted path")

    def fetch(self, request: MarketDataRequest) -> ProviderPayload:
        del request
        raise ProviderNotConfiguredError(
            "AKShare is an extension skeleton; no dataset-specific adapter is configured"
        )
