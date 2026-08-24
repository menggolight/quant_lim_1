"""Bounded Choice CSI-index capture for secondary screening evidence only."""

from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Callable

from .. import provider_access
from ..contracts import canonical_json_bytes
from ..index_evidence import (
    ALL_INDEX_IDS,
    INDEX_LEVEL,
    IndexEvidenceRequest,
    IndexSourcePayload,
)
from .base import (
    DependencyMissingError,
    NetworkBlockedError,
    ProviderNotConfiguredError,
    ProviderQueryError,
    ProviderQuotaExceededError,
    UnsupportedDatasetError,
    classify_unexpected_error,
)
from .choice import ChoiceProvider


# Canonical research IDs and vendor aliases are deliberately distinct maps.
# Only this one alias is attempted for each code.  Until one bounded live probe
# succeeds the mapping is not represented as vendor-verified.
CHOICE_INDEX_ALIASES = MappingProxyType(
    {index_id: f"{index_id[:6]}.CSI" for index_id in ALL_INDEX_IDS}
)
CHOICE_ALIAS_STATUS = "unverified_until_probe"


class ChoiceIndexProvider:
    provider_id = "choice_index"
    source_id = "choice_licensed_secondary"
    adapter_version = "choice-index-adapter-v1"
    upstream_source = "choice.eastmoney_emquantapi.csd_index_with_single_tradedates"
    supported_datasets = frozenset({INDEX_LEVEL})

    _ALLOWED_SDK_METHODS = frozenset({"start", "stop", "csd", "tradedates"})
    _INDICATORS = ("OPEN", "HIGH", "LOW", "CLOSE", "PRECLOSE", "VOLUME", "AMOUNT")
    _OPTIONS = (
        "Period=1,AdjustFlag=1,CurType=1,Order=1,filldata=0,"
        "Ispandas=0,RowIndex=1,RECVtimeout=30"
    )
    _CALENDAR_OPTIONS = "Period=1,Market=CNSESH,Order=1,RECVtimeout=30"
    _LOGIN_OPTIONS = "RecordLoginInfo=0,HTTPTimeout=15"

    def __init__(
        self,
        *,
        sdk_loader: Callable[[], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._core = ChoiceProvider(sdk_loader=sdk_loader, clock=clock)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @classmethod
    def _sdk_call(cls, client: Any, operation: str, *args: Any) -> Any:
        if operation not in cls._ALLOWED_SDK_METHODS:
            raise ProviderQueryError(
                f"Choice index operation is outside the read-only allowlist: {operation}"
            )
        if operation != "stop":
            provider_access.require_choice_network_access(f"sdk_{operation}")
        function = getattr(client, operation, None)
        if not callable(function):
            raise ProviderQueryError(f"Choice SDK does not expose {operation}")
        return function(*args)

    @staticmethod
    def _quiet_log(_: Any) -> int:
        return 1

    def _aware_clock(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ProviderQueryError("Choice index provider clock must include a timezone offset")
        return value

    def fetch(self, request: IndexEvidenceRequest) -> IndexSourcePayload:
        provider_access.require_choice_network_access(
            f"index_fetch_{request.retrieval_mode}"
        )
        if request.dataset_type not in self.supported_datasets:
            raise UnsupportedDatasetError(
                f"Choice index provider does not support {request.dataset_type!r}"
            )
        if request.retrieval_mode == "offline_replay":
            raise UnsupportedDatasetError("Choice online provider cannot perform offline replay")
        if not request.index_ids:
            raise ProviderQueryError("Choice index request has no whitelisted IDs")
        sdk = self._core._load_sdk()
        client = getattr(sdk, "c", None)
        if client is None:
            raise ProviderQueryError("Choice SDK does not expose its client contract")
        started = False
        calendar_dates: list[str] = []
        captures: list[dict[str, Any]] = []
        try:
            login = self._sdk_call(client, "start", self._LOGIN_OPTIONS, self._quiet_log)
            started = True
            ChoiceProvider._raise_sdk_error(login, "start")
            calendar = self._sdk_call(
                client,
                "tradedates",
                request.start_date.isoformat(),  # type: ignore[union-attr]
                request.end_date.isoformat(),  # type: ignore[union-attr]
                self._CALENDAR_OPTIONS,
            )
            ChoiceProvider._raise_sdk_error(calendar, "tradedates")
            calendar_dates = ChoiceProvider._calendar_dates(calendar)
            if not calendar_dates:
                raise ProviderQueryError("Choice calendar returned no trading sessions")

            for index_id in request.index_ids:
                # There is deliberately no suffix fallback.  The first error,
                # including 10001029, exits this loop and closes the session.
                alias = CHOICE_INDEX_ALIASES[index_id]
                response = self._sdk_call(
                    client,
                    "csd",
                    alias,
                    ",".join(self._INDICATORS),
                    request.start_date.isoformat(),  # type: ignore[union-attr]
                    request.end_date.isoformat(),  # type: ignore[union-attr]
                    self._OPTIONS,
                )
                ChoiceProvider._raise_sdk_error(response, "csd")
                rows = ChoiceProvider._response_rows(response, alias)
                if not rows:
                    raise ProviderQueryError(f"Choice returned no index levels for {alias}")
                returned_dates = [str(row["date"]) for row in rows]
                if returned_dates != calendar_dates:
                    raise ProviderQueryError(
                        "Choice index dates must exactly equal the single tradedates response"
                    )
                captures.append(
                    {
                        "index_id": index_id,
                        "sdk_alias": alias,
                        "alias_status": CHOICE_ALIAS_STATUS,
                        "rows": rows,
                    }
                )
            fetched_at = self._aware_clock()
            records: list[dict[str, Any]] = []
            for capture in captures:
                index_id = str(capture["index_id"])
                alias = str(capture["sdk_alias"])
                for row in capture["rows"]:
                    day = str(row["date"])
                    records.append(
                        {
                            "schema_version": "index-level-v1",
                            "index_id": index_id,
                            "trading_date": day,
                            "open": row["open"],
                            "high": row["high"],
                            "low": row["low"],
                            "close": row["close"],
                            "currency": "CNY",
                            "basis": "index_points_unadjusted",
                            "available_at": f"{day}T15:30:00+08:00",
                            "availability_status": "policy_estimated",
                            "source_record_id": f"choice-csd:{alias}:{day}",
                        }
                    )
            raw = canonical_json_bytes(
                {
                    "contract_version": "choice-index-raw-v1",
                    "request": request.fingerprint_payload(self.provider_id, self.adapter_version),
                    "alias_mapping_status": CHOICE_ALIAS_STATUS,
                    "calendar": {
                        "method": "tradedates",
                        "options": self._CALENDAR_OPTIONS,
                        "dates": calendar_dates,
                    },
                    "captures": captures,
                }
            )
            stopped = self._sdk_call(client, "stop")
            started = False
            ChoiceProvider._raise_sdk_error(stopped, "stop")
            return IndexSourcePayload(
                raw_content=raw,
                records=tuple(records),
                fetched_at=fetched_at,
                upstream_source=self.upstream_source,
                point_in_time_status="historical_backfill_not_original_capture",
                capture_mode="licensed_read_only_secondary",
            )
        except (
            DependencyMissingError,
            NetworkBlockedError,
            ProviderNotConfiguredError,
            ProviderQuotaExceededError,
            ProviderQueryError,
            UnsupportedDatasetError,
        ):
            raise
        except Exception as exc:
            raise classify_unexpected_error(exc) from exc
        finally:
            if started:
                try:
                    self._sdk_call(client, "stop")
                except Exception:
                    pass


__all__ = [
    "CHOICE_ALIAS_STATUS",
    "CHOICE_INDEX_ALIASES",
    "ChoiceIndexProvider",
]
