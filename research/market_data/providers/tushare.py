"""Optional Tushare daily-bar validation provider."""

from __future__ import annotations

import importlib
import os
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping

from ..contracts import MarketDataRequest, canonical_json_bytes, sha256_bytes
from .base import (
    DependencyMissingError,
    EmptyDatasetError,
    ProviderNotConfiguredError,
    ProviderPayload,
    ProviderQueryError,
    UnsupportedDatasetError,
    classify_unexpected_error,
)


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


class TushareProvider:
    provider_id = "tushare"
    upstream_source = "tushare.pro"
    adapter_version = "tushare-adapter-v1"
    supported_datasets = frozenset({"daily_bar"})

    def __init__(
        self,
        *,
        sdk_loader: Callable[[], Any] | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sdk_loader = sdk_loader
        self._environ = environ if environ is not None else os.environ
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _load_sdk(self) -> Any:
        if self._sdk_loader is not None:
            return self._sdk_loader()
        try:
            return importlib.import_module("tushare")
        except ModuleNotFoundError as exc:
            if exc.name not in {None, "tushare"}:
                raise ProviderQueryError(
                    f"Tushare SDK dependency import failed: {type(exc).__name__}"
                ) from exc
            raise DependencyMissingError(
                "Tushare SDK is not installed; install the market-tushare extra"
            ) from exc
        except ImportError as exc:
            raise ProviderQueryError(
                f"Tushare SDK import failed: {type(exc).__name__}"
            ) from exc

    def fetch(self, request: MarketDataRequest) -> ProviderPayload:
        if request.dataset_type != "daily_bar":
            raise UnsupportedDatasetError("Tushare V1 only implements daily_bar sampling")
        if request.adjustment != "none":
            raise UnsupportedDatasetError("Tushare validation path only accepts unadjusted daily bars")
        token = str(self._environ.get("TUSHARE_TOKEN") or "").strip()
        if not token:
            raise ProviderNotConfiguredError("TUSHARE_TOKEN is not configured")
        sdk = self._load_sdk()
        try:
            response = sdk.pro_api(token).daily(
                ts_code=request.instrument_id,
                start_date=request.start_date.strftime("%Y%m%d"),  # type: ignore[union-attr]
                end_date=request.end_date.strftime("%Y%m%d"),  # type: ignore[union-attr]
            )
            if response is None:
                raise ProviderQueryError("Tushare daily returned data=null")
            if not hasattr(response, "to_dict"):
                raise ProviderQueryError("Tushare daily returned an unsupported response object")
            raw_rows = response.to_dict(orient="records")
            if not isinstance(raw_rows, list):
                raise ProviderQueryError("Tushare daily response cannot be converted to rows")
        except (ProviderQueryError, ProviderNotConfiguredError):
            raise
        except Exception as exc:
            raise classify_unexpected_error(exc) from exc
        raw_content = canonical_json_bytes(
            {
                "operation": "pro.daily",
                "request": request.fingerprint_payload(self.provider_id, self.adapter_version),
                "rows": raw_rows,
            }
        )
        if not raw_rows:
            raise EmptyDatasetError("Tushare returned an empty daily batch", raw_content=raw_content)
        records: list[Mapping[str, Any]] = []
        for raw in sorted(raw_rows, key=lambda item: str(item.get("trade_date") or "")):
            if not isinstance(raw, Mapping):
                raise ProviderQueryError("Tushare daily row must be an object", raw_content=raw_content)
            day_text = str(raw.get("trade_date") or "")
            try:
                day = datetime.strptime(day_text, "%Y%m%d").date()
                volume = Decimal(str(raw.get("vol"))) * Decimal("100")
                amount = Decimal(str(raw.get("amount"))) * Decimal("1000")
            except (ValueError, InvalidOperation, TypeError) as exc:
                raise ProviderQueryError("Tushare daily row has invalid date or units", raw_content=raw_content) from exc
            records.append(
                {
                    "instrument_id": str(raw.get("ts_code") or "").upper(),
                    "trading_date": day.isoformat(),
                    "open": str(raw.get("open")),
                    "high": str(raw.get("high")),
                    "low": str(raw.get("low")),
                    "close": str(raw.get("close")),
                    "preclose": str(raw.get("pre_close")),
                    "volume": format(volume, "f"),
                    "amount": format(amount, "f"),
                    "currency": "CNY",
                    "adjustment": "none",
                    "trading_status": "traded",
                    "available_at": datetime.combine(day, time(15, 30), tzinfo=CHINA_TZ).isoformat(),
                    "availability_status": "policy_estimated",
                    "source_record_id": sha256_bytes(canonical_json_bytes(dict(raw))),
                }
            )
        fetched_at = self._clock()
        if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
            raise ProviderQueryError("Tushare provider clock must include a timezone offset")
        return ProviderPayload(
            raw_content=raw_content,
            records=tuple(records),
            fetched_at=fetched_at,
            upstream_source="tushare.pro.daily",
            issues=(
                {
                    "code": "optional_validation_source",
                    "severity": "info",
                    "message": "Tushare rows remain a separate batch and do not overwrite BaoStock",
                },
            ),
        )
