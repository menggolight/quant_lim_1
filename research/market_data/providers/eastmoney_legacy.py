"""Legacy Eastmoney market adapter metadata; never a default or admitted source."""

from __future__ import annotations

from typing import Any, Callable

from ..contracts import MarketDataRequest
from .base import ProviderNotConfiguredError, ProviderPayload, UnsupportedDatasetError


class EastmoneyLegacyProvider:
    """Reuse the old implementation only when an explicit diagnostic factory is supplied."""

    provider_id = "eastmoney_legacy"
    upstream_source = "eastmoney_public.push2his"
    adapter_version = "eastmoney-legacy-adapter-v1"
    supported_datasets = frozenset({"daily_bar"})

    def __init__(self, *, diagnostic_factory: Callable[[], Any] | None = None) -> None:
        self._diagnostic_factory = diagnostic_factory

    def fetch(self, request: MarketDataRequest) -> ProviderPayload:
        if request.dataset_type != "daily_bar":
            raise UnsupportedDatasetError("Eastmoney legacy only exposes diagnostic daily bars")
        if self._diagnostic_factory is None:
            raise ProviderNotConfiguredError(
                "Eastmoney legacy requires an explicit diagnostic client and is never a fallback"
            )
        # The existing EastmoneyMarketSource remains the single implementation.
        # It is intentionally not adapted into a validated batch here because
        # its HTTP cache predates V2 raw/quarantine/validated admission.
        raise ProviderNotConfiguredError(
            "Eastmoney legacy is diagnostic_only; use agent.eastmoney_source_probe"
        )
