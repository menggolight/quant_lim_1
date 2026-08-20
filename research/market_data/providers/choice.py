"""Licensed Choice EmQuantAPI read-only market-data provider.

Choice is an explicitly selected secondary source.  Its adapter never exposes
portfolio/account calls and its evidence is diagnostic, not official truth.
"""

from __future__ import annotations

import importlib
import json
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from typing import Any, Callable, Mapping

from ..contracts import MarketDataRequest, canonical_json_bytes, sha256_bytes
from .baostock import normalize_a_share_stock_instrument
from .base import (
    DependencyMissingError,
    EmptyDatasetError,
    IncompleteDatasetError,
    NetworkBlockedError,
    NoTradingDaysError,
    ProviderNotConfiguredError,
    ProviderPayload,
    ProviderQuotaExceededError,
    ProviderQueryError,
    UnsupportedDatasetError,
    classify_unexpected_error,
    safe_error_text,
)


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def _classified_quality_growth_call(function):
    """Keep the batch-only surface inside the existing Provider taxonomy."""

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> ProviderPayload:
        try:
            return function(*args, **kwargs)
        except Exception as exc:
            classified = classify_unexpected_error(exc)
            if classified is exc:
                raise
            raise classified from exc

    return wrapped


class ChoiceProvider:
    """Read a bounded Choice contract without arbitrary SDK dispatch."""

    provider_id = "choice"
    adapter_version = "choice-emquantapi-adapter-v2"
    supported_datasets = frozenset({"daily_bar", "trade_calendar"})

    _DAILY_UPSTREAM = "choice.eastmoney_emquantapi.csd_with_tradedates"
    _CALENDAR_UPSTREAM = "choice.eastmoney_emquantapi.tradedates"
    upstream_source = _DAILY_UPSTREAM
    _INDEX_ADJUSTMENT_WHITELIST = {"000300.SH": "none"}
    _ALLOWED_SDK_METHODS = frozenset(
        {"start", "stop", "csd", "tradedates", "css", "sector", "edb", "edbquery"}
    )
    _DAILY_INDICATORS = (
        "OPEN",
        "HIGH",
        "LOW",
        "CLOSE",
        "PRECLOSE",
        "VOLUME",
        "AMOUNT",
    )
    # Strategy-workspace batch capture has a deliberately separate, fixed
    # surface.  Do not add caller-selected indicators or options here: the
    # generic Provider contract above must continue to reject unadjusted stock
    # bars, while this bounded evidence collector records both research and
    # executable price bases plus the exact eligibility fields observed in the
    # licensed Choice probe.
    _QUALITY_GROWTH_SECTOR_CODE = "009006039"
    _QUALITY_GROWTH_CSD_INDICATORS = (
        "OPEN",
        "HIGH",
        "LOW",
        "CLOSE",
        "PRECLOSE",
        "VOLUME",
        "AMOUNT",
        "TRADESTATUS",
        "ISSTSTOCK",
        "HIGHLIMIT",
        "LOWLIMIT",
    )
    _QUALITY_GROWTH_CSS_STATE_INDICATORS = (
        "LIMITUPPRICE",
        "LIMITDOWNPRICE",
        "TRADESTATUS",
        "ISSTSTOCK",
    )
    _QUALITY_GROWTH_CSS_LIST_DATE_INDICATORS = ("LISTDATE",)
    _QUALITY_GROWTH_CSS_BATCH_SIZE = 50
    _QUALITY_GROWTH_SECTOR_OPTIONS = "Ispandas=0,RowIndex=1,RECVtimeout=30"
    # This is a deliberately tiny, frozen capability probe.  It exists only
    # to learn whether Choice's HISCSIND response echoes the requested
    # historical date.  Keep the request surface closed: callers cannot
    # select another indicator, classification level, instrument, or date.
    _HISTORICAL_CSI_INDUSTRY_PROBE_INDICATOR = "HISCSIND"
    _HISTORICAL_CSI_INDUSTRY_PROBE_INSTRUMENTS = (
        "000001.SZ",
        "000333.SZ",
        "600519.SH",
    )
    _HISTORICAL_CSI_INDUSTRY_PROBE_DATES = (
        "2024-06-28",
        "2026-08-18",
    )
    _HISTORICAL_CSI_INDUSTRY_PROBE_MAX_RESPONSE_DATES = 4
    _CSI800_BENCHMARK_PROBE_DATE = "2026-08-18"
    _CSI800_BENCHMARK_PROBE_SERIES = (
        ("price", "000906.SH"),
        ("total_return", "H00906.CSI"),
    )
    _CSI800_BENCHMARK_PROBE_EXCLUDED_SERIES = (
        ("net_return", "N00906.CSI"),
    )
    _CSI800_BENCHMARK_PROBE_INDICATORS = (
        "OPEN",
        "HIGH",
        "LOW",
        "CLOSE",
        "PRECLOSE",
        "VOLUME",
        "AMOUNT",
    )
    _CSI800_BENCHMARK_MAX_WINDOW_DAYS = 4000
    _CSI800_BENCHMARK_DECIMAL_MAXIMUMS = {
        "open": Decimal("1000000000"),
        "high": Decimal("1000000000"),
        "low": Decimal("1000000000"),
        "close": Decimal("1000000000"),
        "preclose": Decimal("1000000000"),
        "volume": Decimal("100000000000000000000"),
        "amount": Decimal("1000000000000000000000000"),
    }
    _LOGIN_OPTIONS = "RecordLoginInfo=0,HTTPTimeout=15"
    _NOT_CONFIGURED_MARKERS = (
        "loginactivator",
        "activate",
        "activation",
        "not login",
        "not authorized",
        "no permission",
        "permission denied",
        "permission expired",
        "unauthorized",
        "无权限",
        "未登录",
        "未激活",
        "需激活",
        "授权",
        "已过期",
    )
    _NOT_CONFIGURED_CODES = frozenset(
        {
            "10001001",
            "10001002",
            "10001003",
            "10001004",
            "10001005",
            "10001007",
            "10001008",
            "10001009",
            "10001010",
            "10001012",
            "10001013",
            "10001014",
            "10001016",
            "10001018",
            "10001019",
            "10001020",
            "10001023",
            "10001024",
            "10001025",
            "10001026",
            "10001027",
            "10001028",
        }
    )
    _DEPENDENCY_CODES = frozenset({"10001006"})
    _QUOTA_CODES = frozenset({"10001029"})
    _NETWORK_CODES = frozenset(
        {
            "10000011",
            "10000014",
            "10000015",
            "10000017",
            "10001011",
            "10002001",
            "10002002",
            "10002003",
            "10002004",
            "10002005",
            "10002006",
            "10002007",
            "10002008",
            "10002010",
            "10002011",
            "10002013",
            "10002014",
            "10002015",
            "10002016",
        }
    )
    _NETWORK_MARKERS = (
        "network",
        "connection",
        "connect failed",
        "timed out",
        "timeout",
        "socket",
        "dns",
        "网络",
        "连接失败",
        "无法连接",
        "超时",
    )

    def __init__(
        self,
        *,
        sdk_loader: Callable[[], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sdk_loader = sdk_loader
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._diagnostic_client: Any | None = None

    def _load_sdk(self) -> Any:
        if self._sdk_loader is not None:
            return self._sdk_loader()
        try:
            return importlib.import_module("EmQuantAPI")
        except ModuleNotFoundError as exc:
            if exc.name not in {None, "EmQuantAPI"}:
                raise ProviderQueryError(
                    f"Choice SDK dependency import failed: {type(exc).__name__}"
                ) from exc
            raise DependencyMissingError(
                "Choice EmQuantAPI SDK is not registered in this Python environment"
            ) from exc
        except ImportError as exc:
            raise ProviderQueryError(
                f"Choice SDK import failed: {type(exc).__name__}"
            ) from exc

    @classmethod
    def _sdk_call(cls, client: Any, operation: str, *args: Any) -> Any:
        if operation not in cls._ALLOWED_SDK_METHODS:
            raise ProviderQueryError(
                f"Choice SDK operation is outside the read-only allowlist: {operation}"
            )
        function = getattr(client, operation, None)
        if not callable(function):
            raise ProviderQueryError(
                f"Choice SDK does not expose required read-only method {operation}"
            )
        return function(*args)

    @classmethod
    def _raise_sdk_error(cls, response: Any, operation: str) -> None:
        code = str(getattr(response, "ErrorCode", "")).strip()
        message = safe_error_text(getattr(response, "ErrorMsg", ""))
        if code == "0":
            return
        raw_content = canonical_json_bytes(
            {
                "operation": operation,
                "error_code": code or "<missing>",
                "error_message": message or "<missing>",
            }
        )
        rendered = (
            f"Choice {operation} failed: error_code={code or '<missing>'}, "
            f"error_msg={message or '<missing>'}"
        )
        lowered = message.casefold()
        if code in cls._DEPENDENCY_CODES:
            raise DependencyMissingError(rendered, raw_content=raw_content)
        if code in cls._QUOTA_CODES:
            raise ProviderQuotaExceededError(rendered, raw_content=raw_content)
        if code in cls._NOT_CONFIGURED_CODES or any(
            marker in lowered for marker in cls._NOT_CONFIGURED_MARKERS
        ):
            raise ProviderNotConfiguredError(rendered, raw_content=raw_content)
        if code in cls._NETWORK_CODES or any(
            marker in lowered for marker in cls._NETWORK_MARKERS
        ):
            raise NetworkBlockedError(rendered, raw_content=raw_content)
        raise ProviderQueryError(rendered, raw_content=raw_content)

    @staticmethod
    def _quiet_log(_: Any) -> int:
        return 1

    @classmethod
    def _daily_options(cls, adjustment: str) -> str:
        adjust_flag = {"none": "1", "qfq": "3"}.get(adjustment)
        if adjust_flag is None:
            raise UnsupportedDatasetError(
                "Choice diagnostic daily bars only support stock qfq or index none"
            )
        return (
            f"Period=1,AdjustFlag={adjust_flag},CurType=1,Order=1,filldata=0,"
            "Ispandas=0,RowIndex=1,RECVtimeout=30"
        )

    @classmethod
    def _quality_growth_css_options(cls, trading_date: date) -> str:
        return (
            f"EndDate={trading_date.isoformat()},"
            "Ispandas=0,RowIndex=1,RECVtimeout=30"
        )

    @staticmethod
    def _quality_growth_list_date_options() -> str:
        return "Ispandas=0,RowIndex=1,RECVtimeout=30"

    @classmethod
    def _historical_csi_industry_probe_options(cls, historical_date: date) -> str:
        if (
            not isinstance(historical_date, date)
            or isinstance(historical_date, datetime)
            or historical_date.isoformat()
            not in cls._HISTORICAL_CSI_INDUSTRY_PROBE_DATES
        ):
            raise ProviderQueryError(
                "historical CSI industry probe date is outside the frozen allowlist"
            )
        return (
            f"EndDate={historical_date.isoformat()},ClassiFication=1,"
            "Ispandas=0,RowIndex=1,RECVtimeout=30"
        )

    @classmethod
    def _csi800_benchmark_probe_options(cls) -> str:
        # AdjustFlag=1 is the Choice unadjusted/none basis.  The fixed probe
        # never retries another code, price basis, or fill policy.
        return (
            "Period=1,AdjustFlag=1,CurType=1,Order=1,filldata=0,"
            "Ispandas=0,RowIndex=1,RECVtimeout=30"
        )

    @classmethod
    def _csi800_benchmark_window(
        cls, start_date: date, end_date: date
    ) -> tuple[date, date]:
        if (
            not isinstance(start_date, date)
            or isinstance(start_date, datetime)
            or not isinstance(end_date, date)
            or isinstance(end_date, datetime)
        ):
            raise ProviderQueryError("CSI 800 benchmark window requires date values")
        if start_date > end_date:
            raise ProviderQueryError("CSI 800 benchmark start_date exceeds end_date")
        if (end_date - start_date).days > cls._CSI800_BENCHMARK_MAX_WINDOW_DAYS:
            raise ProviderQueryError("CSI 800 benchmark window exceeds fixed safety cap")
        return start_date, end_date

    @classmethod
    def _csi800_benchmark_decimal(
        cls, value: Any, field_name: str, *, allow_missing: bool = False
    ) -> str | None:
        text = cls._scalar_text(value)
        if not text and allow_missing:
            return None
        if not text or len(text) > 64:
            raise ProviderQueryError(
                f"Choice CSI 800 benchmark {field_name} is not a bounded decimal"
            )
        try:
            parsed = Decimal(text)
        except (InvalidOperation, ValueError) as exc:
            raise ProviderQueryError(
                f"Choice CSI 800 benchmark {field_name} is not a bounded decimal"
            ) from exc
        if (
            not parsed.is_finite()
            or len(parsed.as_tuple().digits) > 32
            or parsed.adjusted() > 24
            or parsed.as_tuple().exponent < -16
        ):
            raise ProviderQueryError(
                f"Choice CSI 800 benchmark {field_name} is not a bounded decimal"
            )
        if field_name in {"open", "high", "low", "close", "preclose"}:
            if parsed <= 0:
                raise ProviderQueryError(
                    f"Choice CSI 800 benchmark {field_name} must be positive"
                )
        elif parsed < 0:
            raise ProviderQueryError(
                f"Choice CSI 800 benchmark {field_name} must be non-negative"
            )
        if parsed > cls._CSI800_BENCHMARK_DECIMAL_MAXIMUMS[field_name]:
            raise ProviderQueryError(
                f"Choice CSI 800 benchmark {field_name} exceeds its fixed bound"
            )
        normalized = format(parsed, "f")
        if "." in normalized:
            normalized = normalized.rstrip("0").rstrip(".")
        return "0" if normalized in {"", "-0"} else normalized

    @classmethod
    def _quality_growth_csd_options(cls, adjustment: str) -> str:
        adjust_flag = {"none": "1", "qfq": "3"}.get(adjustment)
        if adjust_flag is None:
            raise UnsupportedDatasetError(
                "quality-growth CSD supports only fixed qfq and none bases"
            )
        # The fixed batch requires one row for suspended sessions.  Filled
        # prices are marks only; TRADESTATUS remains authoritative and a
        # suspended row is never interpreted as executable.
        return (
            f"Period=1,AdjustFlag={adjust_flag},CurType=1,Order=1,filldata=1,"
            "Ispandas=0,RowIndex=1,RECVtimeout=30"
        )

    @classmethod
    def _quality_growth_instruments(
        cls, instrument_ids: tuple[str, ...]
    ) -> tuple[str, ...]:
        if (
            not isinstance(instrument_ids, tuple)
            or not 1 <= len(instrument_ids) <= cls._QUALITY_GROWTH_CSS_BATCH_SIZE
        ):
            raise ProviderQueryError(
                "quality-growth CSS requires a fixed tuple of 1..50 instruments"
            )
        normalized: list[str] = []
        for instrument_id in instrument_ids:
            candidate = normalize_a_share_stock_instrument(instrument_id)
            if candidate != instrument_id:
                raise ProviderQueryError(
                    "quality-growth CSS requires canonical SH/SZ instrument ids"
                )
            normalized.append(candidate)
        if tuple(normalized) != tuple(sorted(set(normalized))):
            raise ProviderQueryError(
                "quality-growth CSS instruments must be ascending and unique"
            )
        return tuple(normalized)

    @classmethod
    def _quality_growth_css_records(
        cls,
        response: Any,
        *,
        instrument_ids: tuple[str, ...],
        indicators: tuple[str, ...],
        expected_dates: list[str],
        trading_date: date | None,
    ) -> tuple[Mapping[str, Any], ...]:
        codes = [str(item).strip().upper() for item in getattr(response, "Codes", ())]
        returned_indicators = [
            str(item).strip().upper()
            for item in getattr(response, "Indicators", ())
        ]
        returned_dates = [
            cls._normalize_date(item) for item in getattr(response, "Dates", ())
        ]
        if codes != list(instrument_ids) or returned_indicators != list(indicators):
            raise ProviderQueryError("Choice quality-growth CSS response contract drifted")
        if returned_dates != expected_dates:
            raise ProviderQueryError(
                "Choice quality-growth CSS did not prove the requested historical date"
            )
        data = getattr(response, "Data", None)
        if not isinstance(data, Mapping):
            raise ProviderQueryError("Choice quality-growth CSS Data must be an object")
        keyed = {str(key).strip().upper(): value for key, value in data.items()}
        if set(keyed) != set(instrument_ids):
            raise ProviderQueryError("Choice quality-growth CSS Data code drifted")
        records: list[Mapping[str, Any]] = []
        for instrument_id in instrument_ids:
            values = keyed[instrument_id]
            if not isinstance(values, (list, tuple)) or len(values) != len(indicators):
                raise ProviderQueryError("Choice quality-growth CSS Data width drifted")
            record: dict[str, Any] = {"instrument_id": instrument_id}
            if trading_date is not None:
                record["trading_date"] = trading_date.isoformat()
            record.update(
                {
                    indicator.lower(): cls._scalar_text(values[index])
                    for index, indicator in enumerate(indicators)
                }
            )
            records.append(record)
        return tuple(records)

    def _quality_growth_client(self) -> Any:
        client = self._diagnostic_client
        if client is None:
            raise ProviderQueryError(
                "Choice quality-growth batch calls require one open diagnostic session"
            )
        return client

    @staticmethod
    def _quality_growth_issue() -> tuple[Mapping[str, Any], ...]:
        return (
            {
                "code": "choice_quality_growth_historical_capture_not_formal_truth",
                "severity": "warning",
                "message": (
                    "The fixed Choice batch surface records licensed historical "
                    "responses but does not prove original point-in-time availability, "
                    "Paper eligibility, or trade eligibility"
                ),
            },
        )

    @_classified_quality_growth_call
    def fetch_quality_growth_calendar(
        self, start_date: date, end_date: date
    ) -> ProviderPayload:
        """Fetch the one fixed China-equity calendar used by the batch plan."""

        if not isinstance(start_date, date) or isinstance(start_date, datetime):
            raise ProviderQueryError("quality-growth calendar start_date must be a date")
        if not isinstance(end_date, date) or isinstance(end_date, datetime):
            raise ProviderQueryError("quality-growth calendar end_date must be a date")
        if start_date > end_date:
            raise ProviderQueryError("quality-growth calendar start_date exceeds end_date")
        request = MarketDataRequest(
            dataset_type="trade_calendar",
            start_date=start_date,
            end_date=end_date,
            retrieval_mode="historical_backfill",
            requested_at=self._aware_clock(),
        )
        payload = self._trade_calendar(
            self._quality_growth_client(), request, "CNSESH"
        )
        return ProviderPayload(
            raw_content=payload.raw_content,
            records=payload.records,
            fetched_at=payload.fetched_at,
            upstream_source=payload.upstream_source,
            issues=(*payload.issues, *self._quality_growth_issue()),
        )

    @_classified_quality_growth_call
    def fetch_quality_growth_membership(
        self, membership_date: date
    ) -> ProviderPayload:
        """Fetch fixed CSI 800 membership for one internally derived grid date."""

        if not isinstance(membership_date, date) or isinstance(
            membership_date, datetime
        ):
            raise ProviderQueryError("quality-growth membership_date must be a date")
        client = self._quality_growth_client()
        response = self._sdk_call(
            client,
            "sector",
            self._QUALITY_GROWTH_SECTOR_CODE,
            membership_date.isoformat(),
            self._QUALITY_GROWTH_SECTOR_OPTIONS,
        )
        self._raise_sdk_error(response, "sector")
        codes = [
            normalize_a_share_stock_instrument(str(item).strip().upper())
            for item in getattr(response, "Codes", ())
        ]
        if not codes or codes != sorted(set(codes)):
            raise ProviderQueryError(
                "Choice CSI 800 sector members must be non-empty, unique, and ascending"
            )
        indicators = [
            str(item).strip().upper()
            for item in getattr(response, "Indicators", ())
        ]
        if indicators != ["SECUCODE", "SECURITYSHORTNAME"]:
            raise ProviderQueryError(
                "Choice CSI 800 sector indicator contract drifted"
            )
        returned_dates = [
            self._normalize_date(item) for item in getattr(response, "Dates", ())
        ]
        if returned_dates != [membership_date.isoformat()]:
            raise ProviderQueryError("Choice CSI 800 sector returned an unexpected date")
        values = getattr(response, "Data", None)
        if not isinstance(values, (list, tuple)) or len(values) != len(codes) * 2:
            raise ProviderQueryError("Choice CSI 800 sector Data shape drifted")
        records: list[dict[str, Any]] = []
        for index, instrument_id in enumerate(codes):
            returned_code = str(values[index * 2]).strip().upper()
            short_name = str(values[index * 2 + 1]).strip()
            if returned_code != instrument_id or not short_name:
                raise ProviderQueryError(
                    "Choice CSI 800 sector Data rows disagree with Codes"
                )
            records.append(
                {
                    "sector_code": self._QUALITY_GROWTH_SECTOR_CODE,
                    "membership_date": membership_date.isoformat(),
                    "instrument_id": instrument_id,
                    "security_short_name": short_name,
                }
            )
        fetched_at = self._aware_clock()
        raw_content = canonical_json_bytes(
            {
                "operation": "quality_growth_fixed_csi800_sector",
                "request": {
                    "sector_code": self._QUALITY_GROWTH_SECTOR_CODE,
                    "membership_date": membership_date.isoformat(),
                    "options": self._QUALITY_GROWTH_SECTOR_OPTIONS,
                },
                "records": records,
            }
        )
        return ProviderPayload(
            raw_content=raw_content,
            records=tuple(records),
            fetched_at=fetched_at,
            upstream_source="choice.eastmoney_emquantapi.sector.csi800_fixed",
            issues=self._quality_growth_issue(),
        )

    @_classified_quality_growth_call
    def fetch_quality_growth_csd(
        self,
        instrument_id: str,
        start_date: date,
        end_date: date,
        *,
        adjustment: str,
    ) -> ProviderPayload:
        """Fetch one fixed CSD field set in qfq or unadjusted price basis."""

        normalized = normalize_a_share_stock_instrument(instrument_id)
        if normalized != instrument_id:
            raise ProviderQueryError(
                "quality-growth CSD requires canonical SH/SZ instrument ids"
            )
        if adjustment not in {"qfq", "none"}:
            raise UnsupportedDatasetError(
                "quality-growth CSD supports only fixed qfq and none bases"
            )
        if (
            not isinstance(start_date, date)
            or isinstance(start_date, datetime)
            or not isinstance(end_date, date)
            or isinstance(end_date, datetime)
            or start_date > end_date
        ):
            raise ProviderQueryError("quality-growth CSD date window is invalid")
        options = self._quality_growth_csd_options(adjustment)
        response = self._sdk_call(
            self._quality_growth_client(),
            "csd",
            normalized,
            ",".join(self._QUALITY_GROWTH_CSD_INDICATORS),
            start_date.isoformat(),
            end_date.isoformat(),
            options,
        )
        self._raise_sdk_error(response, "csd")
        rows = self._response_rows_for_indicators(
            response, normalized, self._QUALITY_GROWTH_CSD_INDICATORS
        )
        fetched_at = self._aware_clock()
        records = tuple(
            {
                "instrument_id": normalized,
                "trading_date": row["date"],
                "adjustment": adjustment,
                **{
                    indicator.lower(): row[indicator.lower()]
                    for indicator in self._QUALITY_GROWTH_CSD_INDICATORS
                },
            }
            for row in rows
        )
        raw_content = canonical_json_bytes(
            {
                "operation": "quality_growth_fixed_csd",
                "request": {
                    "instrument_id": normalized,
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    "adjustment": adjustment,
                    "indicators": list(self._QUALITY_GROWTH_CSD_INDICATORS),
                    "options": options,
                },
                "records": records,
            }
        )
        return ProviderPayload(
            raw_content=raw_content,
            records=records,
            fetched_at=fetched_at,
            upstream_source="choice.eastmoney_emquantapi.csd.quality_growth_fixed",
            issues=self._quality_growth_issue(),
        )

    @_classified_quality_growth_call
    def fetch_quality_growth_css_state_batch(
        self, instrument_ids: tuple[str, ...], trading_date: date
    ) -> ProviderPayload:
        """Fetch one response-dated historical eligibility batch."""

        normalized = self._quality_growth_instruments(instrument_ids)
        if not isinstance(trading_date, date) or isinstance(trading_date, datetime):
            raise ProviderQueryError("quality-growth CSS trading_date must be a date")
        options = self._quality_growth_css_options(trading_date)
        response = self._sdk_call(
            self._quality_growth_client(),
            "css",
            ",".join(normalized),
            ",".join(self._QUALITY_GROWTH_CSS_STATE_INDICATORS),
            options,
        )
        self._raise_sdk_error(response, "css")
        records = self._quality_growth_css_records(
            response,
            instrument_ids=normalized,
            indicators=self._QUALITY_GROWTH_CSS_STATE_INDICATORS,
            expected_dates=[trading_date.isoformat()],
            trading_date=trading_date,
        )
        fetched_at = self._aware_clock()
        raw_content = canonical_json_bytes(
            {
                "operation": "quality_growth_fixed_css_state_batch",
                "request": {
                    "instrument_ids": list(normalized),
                    "trading_date": trading_date.isoformat(),
                    "indicators": list(self._QUALITY_GROWTH_CSS_STATE_INDICATORS),
                    "options": options,
                },
                "response_dates": [trading_date.isoformat()],
                "records": records,
            }
        )
        return ProviderPayload(
            raw_content=raw_content,
            records=records,
            fetched_at=fetched_at,
            upstream_source="choice.eastmoney_emquantapi.css.quality_growth_state_fixed",
            issues=self._quality_growth_issue(),
        )

    @_classified_quality_growth_call
    def fetch_quality_growth_css_list_date_batch(
        self, instrument_ids: tuple[str, ...]
    ) -> ProviderPayload:
        """Fetch the static LISTDATE field separately from historical state."""

        normalized = self._quality_growth_instruments(instrument_ids)
        options = self._quality_growth_list_date_options()
        response = self._sdk_call(
            self._quality_growth_client(),
            "css",
            ",".join(normalized),
            ",".join(self._QUALITY_GROWTH_CSS_LIST_DATE_INDICATORS),
            options,
        )
        self._raise_sdk_error(response, "css")
        records = self._quality_growth_css_records(
            response,
            instrument_ids=normalized,
            indicators=self._QUALITY_GROWTH_CSS_LIST_DATE_INDICATORS,
            expected_dates=[],
            trading_date=None,
        )
        fetched_at = self._aware_clock()
        raw_content = canonical_json_bytes(
            {
                "operation": "quality_growth_fixed_css_list_date_batch",
                "request": {
                    "instrument_ids": list(normalized),
                    "indicators": list(
                        self._QUALITY_GROWTH_CSS_LIST_DATE_INDICATORS
                    ),
                    "options": options,
                },
                "response_dates": [],
                "records": records,
            }
        )
        return ProviderPayload(
            raw_content=raw_content,
            records=records,
            fetched_at=fetched_at,
            upstream_source="choice.eastmoney_emquantapi.css.quality_growth_list_date_fixed",
            issues=self._quality_growth_issue(),
        )

    @_classified_quality_growth_call
    def fetch_historical_csi_industry_probe(
        self, historical_date: date
    ) -> ProviderPayload:
        """Capture one frozen HISCSIND response without promoting it to PIT.

        A matching ``Dates`` echo proves only what this SDK response reported.
        It does not prove original point-in-time availability, classification
        version provenance, or formal-source authenticity.  Empty or mismatched
        response dates are retained as diagnostic evidence instead of being
        silently treated as the requested historical date.
        """

        options = self._historical_csi_industry_probe_options(historical_date)
        instrument_ids = self._HISTORICAL_CSI_INDUSTRY_PROBE_INSTRUMENTS
        indicator = self._HISTORICAL_CSI_INDUSTRY_PROBE_INDICATOR
        response = self._sdk_call(
            self._quality_growth_client(),
            "css",
            ",".join(instrument_ids),
            indicator,
            options,
        )
        self._raise_sdk_error(response, "css")

        returned_codes = [
            str(item).strip().upper() for item in getattr(response, "Codes", ())
        ]
        returned_indicators = [
            str(item).strip().upper()
            for item in getattr(response, "Indicators", ())
        ]
        if returned_codes != list(instrument_ids):
            raise ProviderQueryError(
                "Choice historical CSI industry probe code contract drifted"
            )
        if returned_indicators != [indicator]:
            raise ProviderQueryError(
                "Choice historical CSI industry probe indicator contract drifted"
            )

        raw_dates = list(getattr(response, "Dates", ()))
        if len(raw_dates) > self._HISTORICAL_CSI_INDUSTRY_PROBE_MAX_RESPONSE_DATES:
            raise ProviderQueryError(
                "Choice historical CSI industry probe returned too many dates"
            )
        response_dates = [self._normalize_date(item) for item in raw_dates]
        historical_date_proven = response_dates == [historical_date.isoformat()]

        data = getattr(response, "Data", None)
        if not isinstance(data, Mapping):
            raise ProviderQueryError(
                "Choice historical CSI industry probe Data must be an object"
            )
        keyed = {str(key).strip().upper(): value for key, value in data.items()}
        if len(keyed) != len(data) or set(keyed) != set(instrument_ids):
            raise ProviderQueryError(
                "Choice historical CSI industry probe Data code contract drifted"
            )

        projected_data: dict[str, list[str]] = {}
        records: list[dict[str, Any]] = []
        for instrument_id in instrument_ids:
            values = keyed[instrument_id]
            if not isinstance(values, (list, tuple)) or len(values) != 1:
                raise ProviderQueryError(
                    "Choice historical CSI industry probe Data width drifted"
                )
            industry_name = self._scalar_text(values[0])
            projected_data[instrument_id] = [industry_name]
            records.append(
                {
                    "instrument_id": instrument_id,
                    "requested_date": historical_date.isoformat(),
                    "response_dates": list(response_dates),
                    "historical_date_proven": historical_date_proven,
                    "industry_name": industry_name,
                    "classification_level": 1,
                    "classification_name": "CSI2021",
                    "point_in_time_eligible": False,
                    "formal_truth_eligible": False,
                }
            )

        raw_content = canonical_json_bytes(
            {
                "operation": "choice_fixed_historical_csi_industry_probe",
                "raw_semantics": "canonicalized_sdk_projection",
                "request": {
                    "instrument_ids": list(instrument_ids),
                    "requested_date": historical_date.isoformat(),
                    "indicators": [indicator],
                    "classification_level": 1,
                    "options": options,
                },
                "response": {
                    "codes": returned_codes,
                    "indicators": returned_indicators,
                    "dates": response_dates,
                    "data": projected_data,
                },
                "date_evidence": {
                    "requested_date": historical_date.isoformat(),
                    "response_dates": response_dates,
                    "historical_date_proven": historical_date_proven,
                    "proof_rule": "response_dates_exactly_equal_requested_date",
                },
                "records": records,
            }
        )
        issue_code = (
            "choice_response_date_echo_only_not_pit_availability"
            if historical_date_proven
            else "choice_historical_industry_response_date_not_proven"
        )
        issue_message = (
            "The Choice response echoed the requested date, but this does not "
            "prove original point-in-time availability or formal-source authenticity"
            if historical_date_proven
            else "The Choice response did not exactly echo the requested date; the "
            "industry values remain diagnostic and must not be treated as historical PIT"
        )
        return ProviderPayload(
            raw_content=raw_content,
            records=tuple(records),
            fetched_at=self._aware_clock(),
            upstream_source=(
                "choice.eastmoney_emquantapi.css.hiscsind.fixed_diagnostic"
            ),
            issues=(
                {
                    "code": issue_code,
                    "severity": "warning",
                    "message": issue_message,
                },
            ),
        )

    @_classified_quality_growth_call
    def fetch_csi800_benchmark_probe(self) -> ProviderPayload:
        """Capture the two frozen CSI 800 benchmark aliases for one day.

        This method is an identity/capability probe only.  It deliberately has
        no date, instrument, series, or adjustment arguments and never falls
        back to the net-return alias or another price basis.
        """

        client = self._quality_growth_client()
        expected_date = self._CSI800_BENCHMARK_PROBE_DATE
        indicators = self._CSI800_BENCHMARK_PROBE_INDICATORS
        options = self._csi800_benchmark_probe_options()
        series_results: list[dict[str, Any]] = []
        records: list[dict[str, Any]] = []

        for series, instrument_id in self._CSI800_BENCHMARK_PROBE_SERIES:
            response = self._sdk_call(
                client,
                "csd",
                instrument_id,
                ",".join(indicators),
                expected_date,
                expected_date,
                options,
            )
            self._raise_sdk_error(response, "csd")
            returned_codes = [
                str(item).strip().upper()
                for item in getattr(response, "Codes", ())
            ]
            returned_indicators = [
                str(item).strip().upper()
                for item in getattr(response, "Indicators", ())
            ]
            if returned_codes != [instrument_id]:
                raise ProviderQueryError(
                    "Choice CSI 800 benchmark probe code contract drifted"
                )
            if returned_indicators != list(indicators):
                raise ProviderQueryError(
                    "Choice CSI 800 benchmark probe indicator contract drifted"
                )

            raw_dates = list(getattr(response, "Dates", ()))
            if len(raw_dates) > 4:
                raise ProviderQueryError(
                    "Choice CSI 800 benchmark probe returned too many dates"
                )
            response_dates = [self._normalize_date(item) for item in raw_dates]
            historical_date_proven = response_dates == [expected_date]

            data = getattr(response, "Data", None)
            if not isinstance(data, Mapping):
                raise ProviderQueryError(
                    "Choice CSI 800 benchmark probe Data must be an object"
                )
            keyed = {
                str(key).strip().upper(): value for key, value in data.items()
            }
            if len(keyed) != len(data) or set(keyed) != {instrument_id}:
                raise ProviderQueryError(
                    "Choice CSI 800 benchmark probe Data code contract drifted"
                )
            columns = keyed[instrument_id]
            if not isinstance(columns, (list, tuple)) or len(columns) != len(
                indicators
            ):
                raise ProviderQueryError(
                    "Choice CSI 800 benchmark probe Data column width drifted"
                )
            projected_columns: list[list[str]] = []
            for column in columns:
                if not isinstance(column, (list, tuple)) or len(column) != len(
                    response_dates
                ):
                    raise ProviderQueryError(
                        "Choice CSI 800 benchmark probe Data date width drifted"
                    )
                projected_columns.append(
                    [self._scalar_text(value) for value in column]
                )

            series_records: list[dict[str, Any]] = []
            for date_index, returned_date in enumerate(response_dates):
                record = {
                    "series": series,
                    "instrument_id": instrument_id,
                    "requested_date": expected_date,
                    "trading_date": returned_date,
                    "adjustment": "none",
                    "historical_date_proven": historical_date_proven,
                    "point_in_time_eligible": False,
                    "formal_truth_eligible": False,
                }
                record.update(
                    {
                        indicator.lower(): projected_columns[indicator_index][
                            date_index
                        ]
                        for indicator_index, indicator in enumerate(indicators)
                    }
                )
                series_records.append(record)
                records.append(record)
            series_results.append(
                {
                    "series": series,
                    "instrument_id": instrument_id,
                    "response": {
                        "codes": returned_codes,
                        "indicators": returned_indicators,
                        "dates": response_dates,
                        "data": {instrument_id: projected_columns},
                    },
                    "date_evidence": {
                        "requested_date": expected_date,
                        "response_dates": response_dates,
                        "historical_date_proven": historical_date_proven,
                        "proof_rule": (
                            "response_dates_exactly_equal_requested_date"
                        ),
                    },
                    "records": series_records,
                }
            )

        all_dates_proven = all(
            item["date_evidence"]["historical_date_proven"]
            for item in series_results
        )
        raw_content = canonical_json_bytes(
            {
                "operation": "choice_fixed_csi800_benchmark_probe",
                "raw_semantics": "canonicalized_sdk_projection",
                "request": {
                    "series": [
                        {"series": series, "instrument_id": instrument_id}
                        for series, instrument_id in self._CSI800_BENCHMARK_PROBE_SERIES
                    ],
                    "excluded_distinct_series": [
                        {"series": series, "instrument_id": instrument_id}
                        for series, instrument_id in self._CSI800_BENCHMARK_PROBE_EXCLUDED_SERIES
                    ],
                    "start_date": expected_date,
                    "end_date": expected_date,
                    "adjustment": "none",
                    "indicators": list(indicators),
                    "options": options,
                    "fallback_allowed": False,
                },
                "series_results": series_results,
                "historical_date_proven_for_all_series": all_dates_proven,
            }
        )
        issue_code = (
            "choice_benchmark_response_date_echo_only_not_formal_truth"
            if all_dates_proven
            else "choice_benchmark_response_date_not_proven"
        )
        return ProviderPayload(
            raw_content=raw_content,
            records=tuple(records),
            fetched_at=self._aware_clock(),
            upstream_source=(
                "choice.eastmoney_emquantapi.csd.csi800_benchmark_fixed_diagnostic"
            ),
            issues=(
                {
                    "code": issue_code,
                    "severity": "warning",
                    "message": (
                        "The fixed Choice benchmark response is diagnostic only; "
                        "metadata integrity and a date echo do not authenticate an "
                        "official CSI benchmark or establish historical PIT availability"
                    ),
                },
            ),
        )

    def _fetch_csi800_benchmark_csd(
        self,
        start_date: date,
        end_date: date,
        *,
        series: str,
        instrument_id: str,
    ) -> ProviderPayload:
        start_date, end_date = self._csi800_benchmark_window(
            start_date, end_date
        )
        fixed_aliases = dict(self._CSI800_BENCHMARK_PROBE_SERIES)
        if fixed_aliases.get(series) != instrument_id:
            raise ProviderQueryError(
                "CSI 800 benchmark series and alias are outside the fixed contract"
            )
        indicators = self._CSI800_BENCHMARK_PROBE_INDICATORS
        options = self._csi800_benchmark_probe_options()
        response = self._sdk_call(
            self._quality_growth_client(),
            "csd",
            instrument_id,
            ",".join(indicators),
            start_date.isoformat(),
            end_date.isoformat(),
            options,
        )
        self._raise_sdk_error(response, "csd")

        returned_codes = [
            str(item).strip().upper() for item in getattr(response, "Codes", ())
        ]
        returned_indicators = [
            str(item).strip().upper()
            for item in getattr(response, "Indicators", ())
        ]
        if returned_codes != [instrument_id]:
            raise ProviderQueryError(
                "Choice CSI 800 benchmark CSD code contract drifted"
            )
        if returned_indicators != list(indicators):
            raise ProviderQueryError(
                "Choice CSI 800 benchmark CSD indicator contract drifted"
            )

        raw_dates = list(getattr(response, "Dates", ()))
        if not raw_dates:
            raise EmptyDatasetError("Choice CSI 800 benchmark CSD returned no dates")
        response_dates = [self._normalize_date(item) for item in raw_dates]
        if response_dates != sorted(set(response_dates)):
            raise ProviderQueryError(
                "Choice CSI 800 benchmark CSD dates must be unique and ascending"
            )
        if any(
            day < start_date.isoformat() or day > end_date.isoformat()
            for day in response_dates
        ):
            raise ProviderQueryError(
                "Choice CSI 800 benchmark CSD returned a date outside the request"
            )

        data = getattr(response, "Data", None)
        if not isinstance(data, Mapping):
            raise ProviderQueryError(
                "Choice CSI 800 benchmark CSD Data must be an object"
            )
        keyed = {str(key).strip().upper(): value for key, value in data.items()}
        if len(keyed) != len(data) or set(keyed) != {instrument_id}:
            raise ProviderQueryError(
                "Choice CSI 800 benchmark CSD Data code contract drifted"
            )
        columns = keyed[instrument_id]
        if not isinstance(columns, (list, tuple)) or len(columns) != len(
            indicators
        ):
            raise ProviderQueryError(
                "Choice CSI 800 benchmark CSD Data column width drifted"
            )
        normalized_columns: list[list[str | None]] = []
        for indicator, column in zip(indicators, columns):
            if not isinstance(column, (list, tuple)) or len(column) != len(
                response_dates
            ):
                raise ProviderQueryError(
                    "Choice CSI 800 benchmark CSD Data date width drifted"
                )
            normalized_columns.append(
                [
                    self._csi800_benchmark_decimal(
                        value,
                        indicator.lower(),
                        allow_missing=(
                            series == "total_return"
                            and indicator in {"VOLUME", "AMOUNT"}
                        ),
                    )
                    for value in column
                ]
            )

        records: list[dict[str, Any]] = []
        for date_index, trading_date in enumerate(response_dates):
            values = {
                indicator.lower(): normalized_columns[indicator_index][date_index]
                for indicator_index, indicator in enumerate(indicators)
            }
            opened = Decimal(str(values["open"]))
            high = Decimal(str(values["high"]))
            low = Decimal(str(values["low"]))
            closed = Decimal(str(values["close"]))
            if low > min(opened, closed) or high < max(opened, closed) or low > high:
                raise ProviderQueryError(
                    "Choice CSI 800 benchmark CSD OHLC ordering is invalid"
                )
            records.append(
                {
                    "series": series,
                    "instrument_id": instrument_id,
                    "trading_date": trading_date,
                    "adjustment": "none",
                    "fill_policy": "no_fill_returned_dates_only",
                    "calendar_completeness_status": (
                        "requires_external_exchange_calendar_reconciliation"
                    ),
                    "point_in_time_eligible": False,
                    "formal_truth_eligible": False,
                    **values,
                }
            )

        raw_content = canonical_json_bytes(
            {
                "operation": "choice_fixed_csi800_benchmark_csd",
                "raw_semantics": "canonicalized_sdk_projection",
                "request": {
                    "series": series,
                    "instrument_id": instrument_id,
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    "adjustment": "none",
                    "indicators": list(indicators),
                    "options": options,
                    "fill_policy": "no_fill_returned_dates_only",
                    "fallback_allowed": False,
                },
                "response_dates": response_dates,
                "calendar_completeness_status": (
                    "requires_external_exchange_calendar_reconciliation"
                ),
                "records": records,
            }
        )
        return ProviderPayload(
            raw_content=raw_content,
            records=tuple(records),
            fetched_at=self._aware_clock(),
            upstream_source=(
                "choice.eastmoney_emquantapi.csd.csi800_benchmark_fixed_range"
            ),
            issues=(
                {
                    "code": "choice_benchmark_calendar_reconciliation_required",
                    "severity": "warning",
                    "message": (
                        "Choice CSD returned strict unfilled dates within the requested "
                        "window; completeness still requires an independently admitted "
                        "exchange calendar"
                    ),
                },
                {
                    "code": "choice_benchmark_not_officially_authenticated",
                    "severity": "warning",
                    "message": (
                        "The fixed Choice alias remains secondary diagnostic evidence "
                        "and is not an authenticated official CSI benchmark feed"
                    ),
                },
            ),
        )

    @_classified_quality_growth_call
    def fetch_csi800_price_index_csd(
        self, start_date: date, end_date: date
    ) -> ProviderPayload:
        """Fetch fixed 000906.SH, unadjusted, with no alias fallback."""

        return self._fetch_csi800_benchmark_csd(
            start_date,
            end_date,
            series="price",
            instrument_id="000906.SH",
        )

    @_classified_quality_growth_call
    def fetch_csi800_total_return_csd(
        self, start_date: date, end_date: date
    ) -> ProviderPayload:
        """Fetch fixed H00906.CSI, unadjusted, with no alias fallback."""

        return self._fetch_csi800_benchmark_csd(
            start_date,
            end_date,
            series="total_return",
            instrument_id="H00906.CSI",
        )

    @staticmethod
    def _calendar_options(market: str = "CNSESH") -> str:
        return f"Period=1,Market={market},Order=1,RECVtimeout=30"

    @classmethod
    def _daily_instrument(cls, request: MarketDataRequest) -> tuple[str, str]:
        instrument = request.instrument_id
        if instrument in cls._INDEX_ADJUSTMENT_WHITELIST:
            required = cls._INDEX_ADJUSTMENT_WHITELIST[instrument]
            if request.adjustment != required:
                raise UnsupportedDatasetError(
                    f"Choice index {instrument} requires adjustment={required}"
                )
            return instrument, "CNSESH"
        try:
            normalized = normalize_a_share_stock_instrument(instrument)
        except ValueError as exc:
            raise ProviderQueryError(
                "Choice only accepts canonical SH/SZ A-share stocks or the "
                "whitelisted index 000300.SH"
            ) from exc
        if normalized != instrument:
            raise ProviderQueryError(
                "Choice requires canonical instrument codes such as 000333.SZ"
            )
        if request.adjustment != "qfq":
            raise UnsupportedDatasetError(
                "Choice stock diagnostic outcomes require qfq; unadjusted fallback is forbidden"
            )
        return normalized, "CNSESH" if normalized.endswith(".SH") else "CNSESZ"

    @contextmanager
    def diagnostic_session(self):
        """Reuse one authenticated read-only session for a bounded collector."""

        if self._diagnostic_client is not None:
            raise ProviderQueryError("nested Choice diagnostic sessions are not supported")
        sdk = self._load_sdk()
        client = getattr(sdk, "c", None)
        if client is None:
            raise ProviderQueryError("Choice SDK does not expose its client contract")
        try:
            login = self._sdk_call(
                client, "start", self._LOGIN_OPTIONS, self._quiet_log
            )
            self._raise_sdk_error(login, "start")
        except (
            DependencyMissingError,
            NetworkBlockedError,
            ProviderNotConfiguredError,
            ProviderQueryError,
        ):
            try:
                self._sdk_call(client, "stop")
            except Exception:
                pass
            raise
        except Exception as exc:
            try:
                self._sdk_call(client, "stop")
            except Exception:
                pass
            raise classify_unexpected_error(exc) from exc

        self._diagnostic_client = client
        body_failed = False
        try:
            yield self
        except BaseException:
            body_failed = True
            raise
        finally:
            self._diagnostic_client = None
            try:
                stopped = self._sdk_call(client, "stop")
                self._raise_sdk_error(stopped, "stop")
            except (
                NetworkBlockedError,
                ProviderNotConfiguredError,
                ProviderQueryError,
            ):
                if not body_failed:
                    raise
            except Exception as exc:
                if not body_failed:
                    raise classify_unexpected_error(exc) from exc

    def _fetch_in_open_session(
        self,
        request: MarketDataRequest,
        instrument_id: str,
        market: str,
    ) -> ProviderPayload:
        client = self._diagnostic_client
        if client is None:
            raise ProviderQueryError("Choice diagnostic session is not open")
        try:
            if request.dataset_type == "daily_bar":
                return self._daily_bar(client, request, instrument_id, market)
            return self._trade_calendar(client, request, market)
        except (
            DependencyMissingError,
            EmptyDatasetError,
            IncompleteDatasetError,
            NetworkBlockedError,
            NoTradingDaysError,
            ProviderNotConfiguredError,
            ProviderQueryError,
            UnsupportedDatasetError,
        ):
            raise
        except Exception as exc:
            raise classify_unexpected_error(exc) from exc

    def fetch(self, request: MarketDataRequest) -> ProviderPayload:
        if request.dataset_type not in self.supported_datasets:
            raise UnsupportedDatasetError(
                f"Choice does not implement dataset {request.dataset_type!r}"
            )
        if request.dataset_type == "daily_bar":
            instrument_id, market = self._daily_instrument(request)
        else:
            instrument_id, market = "", "CNSESH"
        if self._diagnostic_client is not None:
            return self._fetch_in_open_session(request, instrument_id, market)
        with self.diagnostic_session():
            return self._fetch_in_open_session(request, instrument_id, market)

    def _daily_bar(
        self,
        client: Any,
        request: MarketDataRequest,
        instrument_id: str,
        market: str,
    ) -> ProviderPayload:
        daily_options = self._daily_options(request.adjustment)
        response = self._sdk_call(
            client,
            "csd",
            instrument_id,
            ",".join(self._DAILY_INDICATORS),
            request.start_date.isoformat(),  # type: ignore[union-attr]
            request.end_date.isoformat(),  # type: ignore[union-attr]
            daily_options,
        )
        self._raise_sdk_error(response, "csd")
        raw_rows = self._response_rows(response, instrument_id)
        calendar_options = self._calendar_options(market)
        calendar_response = self._sdk_call(
            client,
            "tradedates",
            request.start_date.isoformat(),  # type: ignore[union-attr]
            request.end_date.isoformat(),  # type: ignore[union-attr]
            calendar_options,
        )
        self._raise_sdk_error(calendar_response, "tradedates")
        calendar_dates = self._calendar_dates(calendar_response)
        raw_content = canonical_json_bytes(
            {
                "operation": "csd_with_tradedates_completeness",
                "request": request.fingerprint_payload(
                    self.provider_id, self.adapter_version
                ),
                "daily": {
                    "options": daily_options,
                    "indicators": list(self._DAILY_INDICATORS),
                    "rows": raw_rows,
                },
                "trade_calendar": {
                    "options": calendar_options,
                    "dates": calendar_dates,
                },
            }
        )
        fetched_at = self._aware_clock()
        records = replay_choice_raw(request, raw_content, fetched_at)
        return ProviderPayload(
            raw_content=raw_content,
            records=records,
            fetched_at=fetched_at,
            upstream_source=self._DAILY_UPSTREAM,
            issues=(
                {
                    "code": "licensed_secondary_not_primary",
                    "severity": "info",
                    "message": (
                        "Choice is an explicitly selected licensed read-only secondary "
                        "and does not replace the BaoStock primary chain"
                    ),
                },
            ),
        )

    def _trade_calendar(
        self,
        client: Any,
        request: MarketDataRequest,
        market: str,
    ) -> ProviderPayload:
        options = self._calendar_options(market)
        response = self._sdk_call(
            client,
            "tradedates",
            request.start_date.isoformat(),  # type: ignore[union-attr]
            request.end_date.isoformat(),  # type: ignore[union-attr]
            options,
        )
        self._raise_sdk_error(response, "tradedates")
        dates = self._calendar_dates(response)
        raw_content = canonical_json_bytes(
            {
                "operation": "tradedates",
                "request": request.fingerprint_payload(
                    self.provider_id, self.adapter_version
                ),
                "trade_calendar": {"options": options, "dates": dates},
            }
        )
        fetched_at = self._aware_clock()
        records = replay_choice_raw(request, raw_content, fetched_at)
        return ProviderPayload(
            raw_content=raw_content,
            records=records,
            fetched_at=fetched_at,
            upstream_source=self._CALENDAR_UPSTREAM,
            issues=(
                {
                    "code": "choice_calendar_secondary_not_official",
                    "severity": "info",
                    "message": (
                        "Choice trade dates are diagnostic secondary evidence, not "
                        "an exchange-signed official calendar"
                    ),
                },
            ),
        )

    @classmethod
    def _calendar_dates(cls, response: Any) -> list[str]:
        codes = [str(item).strip() for item in getattr(response, "Codes", ())]
        indicators = [
            str(item).strip().upper() for item in getattr(response, "Indicators", ())
        ]
        if codes != [""] or indicators != ["TRADEDATE"]:
            raise ProviderQueryError("Choice tradedates field contract drifted")
        raw_dates = list(getattr(response, "Dates", ()))
        data = getattr(response, "Data", None)
        if not isinstance(data, (list, tuple)):
            raise ProviderQueryError("Choice tradedates Data must be a sequence")
        dates = [cls._normalize_date(value) for value in raw_dates]
        data_dates = [cls._normalize_date(value) for value in data]
        if dates != data_dates:
            raise ProviderQueryError("Choice tradedates Dates and Data disagree")
        if dates != sorted(set(dates)):
            raise ProviderQueryError(
                "Choice tradedates dates must be unique and strictly ascending"
            )
        return dates

    @classmethod
    def _response_rows(
        cls, response: Any, instrument_id: str
    ) -> list[dict[str, str]]:
        return cls._response_rows_for_indicators(
            response, instrument_id, cls._DAILY_INDICATORS
        )

    @classmethod
    def _response_rows_for_indicators(
        cls,
        response: Any,
        instrument_id: str,
        expected_indicators: tuple[str, ...],
    ) -> list[dict[str, str]]:
        codes = [str(item).strip().upper() for item in getattr(response, "Codes", ())]
        if codes != [instrument_id]:
            raise ProviderQueryError(
                f"Choice csd returned codes {codes!r}, requested {instrument_id}"
            )
        indicators = [
            str(item).strip().upper() for item in getattr(response, "Indicators", ())
        ]
        if indicators != list(expected_indicators):
            raise ProviderQueryError(
                "Choice csd indicator contract differs from the requested fields"
            )
        raw_dates = list(getattr(response, "Dates", ()))
        if not raw_dates:
            return []
        dates = [cls._normalize_date(value) for value in raw_dates]
        if dates != sorted(set(dates)):
            raise ProviderQueryError(
                "Choice csd dates must be unique and strictly ascending"
            )
        data = getattr(response, "Data", None)
        if not isinstance(data, Mapping):
            raise ProviderQueryError("Choice csd Data must be an object")
        keyed = {str(key).strip().upper(): value for key, value in data.items()}
        if len(keyed) != len(data):
            raise ProviderQueryError("Choice csd Data contains duplicate code aliases")
        if set(keyed) != {instrument_id}:
            raise ProviderQueryError(
                "Choice csd Data codes differ from the response code contract"
            )
        columns = keyed[instrument_id]
        if not isinstance(columns, (list, tuple)) or len(columns) != len(indicators):
            raise ProviderQueryError("Choice csd Data column width drifted")
        normalized_columns: list[list[str]] = []
        for column in columns:
            if not isinstance(column, (list, tuple)) or len(column) != len(dates):
                raise ProviderQueryError("Choice csd Data date width drifted")
            normalized_columns.append([cls._scalar_text(value) for value in column])
        rows: list[dict[str, str]] = []
        for date_index, day in enumerate(dates):
            row = {"code": instrument_id, "date": day}
            for indicator_index, indicator in enumerate(indicators):
                row[indicator.lower()] = normalized_columns[indicator_index][date_index]
            rows.append(row)
        return rows

    @staticmethod
    def _normalize_date(value: Any) -> str:
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        text = str(value).strip()
        for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
            try:
                return datetime.strptime(text, pattern).date().isoformat()
            except ValueError:
                continue
        raise ProviderQueryError("Choice returned an invalid date")

    @staticmethod
    def _scalar_text(value: Any) -> str:
        if value is None or isinstance(value, bool):
            return ""
        if isinstance(value, Decimal):
            return format(value, "f")
        if isinstance(value, float):
            if value != value or value in {float("inf"), float("-inf")}:
                return ""
            return format(value, ".15g")
        return str(value).strip()

    def _aware_clock(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ProviderQueryError(
                "Choice provider clock must include a timezone offset"
            )
        return value


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_choice_raw(raw_content: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(
            raw_content.decode("utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ProviderQueryError(
            f"Choice raw evidence is not strict JSON: {exc}",
            raw_content=raw_content,
        ) from exc
    if not isinstance(payload, dict) or canonical_json_bytes(payload) != raw_content:
        raise ProviderQueryError(
            "Choice raw evidence is not in deterministic canonical form",
            raw_content=raw_content,
        )
    return payload


def _raw_calendar_dates(
    value: Any,
    *,
    request: MarketDataRequest,
    expected_options: str,
    raw_content: bytes,
) -> list[str]:
    if not isinstance(value, Mapping) or set(value) != {"options", "dates"}:
        raise ProviderQueryError(
            "Choice raw trade-calendar envelope is malformed",
            raw_content=raw_content,
        )
    if value.get("options") != expected_options:
        raise ProviderQueryError(
            "Choice raw trade-calendar options differ from the request",
            raw_content=raw_content,
        )
    dates_value = value.get("dates")
    if not isinstance(dates_value, list) or any(
        not isinstance(item, str) for item in dates_value
    ):
        raise ProviderQueryError(
            "Choice raw trade-calendar dates are malformed",
            raw_content=raw_content,
        )
    try:
        dates = [ChoiceProvider._normalize_date(item) for item in dates_value]
    except ProviderQueryError as exc:
        raise ProviderQueryError(str(exc), raw_content=raw_content) from exc
    if dates != sorted(set(dates)):
        raise ProviderQueryError(
            "Choice raw trade-calendar dates must be unique and ascending",
            raw_content=raw_content,
        )
    start = request.start_date
    end = request.end_date
    if start is None or end is None:
        raise ProviderQueryError(
            "Choice trade calendar requires a date window", raw_content=raw_content
        )
    if any(date.fromisoformat(item) < start or date.fromisoformat(item) > end for item in dates):
        raise ProviderQueryError(
            "Choice raw trade-calendar date is outside the requested window",
            raw_content=raw_content,
        )
    return dates


def replay_choice_raw(
    request: MarketDataRequest,
    raw_content: bytes,
    fetched_at: datetime,
) -> tuple[Mapping[str, Any], ...]:
    """Deterministically derive normalized Choice records from raw evidence."""

    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise ProviderQueryError(
            "Choice replay fetched_at must include a timezone offset",
            raw_content=raw_content,
        )
    payload = _strict_choice_raw(raw_content)
    expected_request = request.fingerprint_payload(
        ChoiceProvider.provider_id, ChoiceProvider.adapter_version
    )
    if payload.get("request") != expected_request:
        raise ProviderQueryError(
            "Choice raw evidence request differs from the batch request",
            raw_content=raw_content,
        )

    if request.dataset_type == "trade_calendar":
        if set(payload) != {"operation", "request", "trade_calendar"} or payload.get(
            "operation"
        ) != "tradedates":
            raise ProviderQueryError(
                "Choice trade-calendar raw envelope is malformed",
                raw_content=raw_content,
            )
        open_dates = set(
            _raw_calendar_dates(
                payload["trade_calendar"],
                request=request,
                expected_options=ChoiceProvider._calendar_options("CNSESH"),
                raw_content=raw_content,
            )
        )
        start = request.start_date
        end = request.end_date
        assert start is not None and end is not None
        records: list[Mapping[str, Any]] = []
        day = start
        while day <= end:
            raw = {
                "calendar_date": day.isoformat(),
                "is_trading_day": day.isoformat() in open_dates,
            }
            records.append(
                {
                    **raw,
                    "available_at": fetched_at.isoformat(),
                    "availability_status": "unknown",
                    "source_record_id": sha256_bytes(canonical_json_bytes(raw)),
                }
            )
            day += timedelta(days=1)
        if not records:
            raise EmptyDatasetError(
                "Choice returned no calendar window", raw_content=raw_content
            )
        return tuple(records)

    if request.dataset_type != "daily_bar":
        raise UnsupportedDatasetError(
            f"Choice raw replay does not support {request.dataset_type!r}"
        )
    instrument_id, market = ChoiceProvider._daily_instrument(request)
    if set(payload) != {"operation", "request", "daily", "trade_calendar"} or payload.get(
        "operation"
    ) != "csd_with_tradedates_completeness":
        raise ProviderQueryError(
            "Choice daily raw envelope is malformed", raw_content=raw_content
        )
    daily = payload.get("daily")
    expected_row_fields = {"code", "date"} | {
        item.casefold() for item in ChoiceProvider._DAILY_INDICATORS
    }
    if not isinstance(daily, Mapping) or set(daily) != {
        "options",
        "indicators",
        "rows",
    }:
        raise ProviderQueryError(
            "Choice raw daily table is malformed", raw_content=raw_content
        )
    if daily.get("options") != ChoiceProvider._daily_options(request.adjustment) or daily.get(
        "indicators"
    ) != list(ChoiceProvider._DAILY_INDICATORS):
        raise ProviderQueryError(
            "Choice raw daily options or indicators differ from the request",
            raw_content=raw_content,
        )
    rows = daily.get("rows")
    if not isinstance(rows, list):
        raise ProviderQueryError(
            "Choice raw daily rows are malformed", raw_content=raw_content
        )
    calendar_dates = _raw_calendar_dates(
        payload["trade_calendar"],
        request=request,
        expected_options=ChoiceProvider._calendar_options(market),
        raw_content=raw_content,
    )
    if not calendar_dates:
        if rows:
            raise IncompleteDatasetError(
                "Choice csd returned rows while its trade calendar was empty",
                raw_content=raw_content,
            )
        raise NoTradingDaysError(
            "Choice calendar confirms no exchange trading days in the requested window",
            raw_content=raw_content,
        )
    if not rows:
        raise IncompleteDatasetError(
            "Choice csd returned no rows although its calendar contains trading days",
            raw_content=raw_content,
        )
    records = []
    prior_day: date | None = None
    for row in rows:
        if (
            not isinstance(row, Mapping)
            or set(row) != expected_row_fields
            or any(not isinstance(value, str) for value in row.values())
        ):
            raise ProviderQueryError(
                "Choice raw daily row contract drifted", raw_content=raw_content
            )
        if row.get("code") != instrument_id:
            raise ProviderQueryError(
                "Choice raw daily row returned a different instrument",
                raw_content=raw_content,
            )
        try:
            trading_day = date.fromisoformat(str(row.get("date")))
        except ValueError as exc:
            raise ProviderQueryError(
                "Choice raw daily row has an invalid date", raw_content=raw_content
            ) from exc
        if prior_day is not None and trading_day <= prior_day:
            raise ProviderQueryError(
                "Choice raw daily dates must be unique and ascending",
                raw_content=raw_content,
            )
        prior_day = trading_day
        available_at = datetime.combine(trading_day, time(15, 30), tzinfo=CHINA_TZ)
        records.append(
            {
                "instrument_id": instrument_id,
                "trading_date": trading_day.isoformat(),
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "preclose": row["preclose"],
                "volume": row["volume"],
                "amount": row["amount"],
                "currency": "CNY",
                "adjustment": request.adjustment,
                "trading_status": "unknown",
                "available_at": available_at.isoformat(),
                "availability_status": "policy_estimated",
                "source_record_id": sha256_bytes(canonical_json_bytes(dict(row))),
            }
        )
    returned_dates = {str(record["trading_date"]) for record in records}
    expected_dates = set(calendar_dates)
    if returned_dates != expected_dates or len(returned_dates) != len(records):
        raise IncompleteDatasetError(
            "Choice csd response is incomplete against its trade calendar: "
            f"missing={sorted(expected_dates - returned_dates)}, "
            f"unexpected={sorted(returned_dates - expected_dates)}; "
            "a suspension cannot be distinguished from a missing response without "
            "an authenticated per-security status field",
            raw_content=raw_content,
        )
    return tuple(records)


__all__ = ["ChoiceProvider", "replay_choice_raw"]
