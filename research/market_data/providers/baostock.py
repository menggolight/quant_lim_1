"""BaoStock read-only provider for unadjusted daily data and reference data."""

from __future__ import annotations

import importlib
import json
import re
from datetime import datetime, time, timedelta, timezone
from typing import Any, Callable, Mapping

from ..contracts import MarketDataRequest, canonical_json_bytes, sha256_bytes
from .base import (
    DependencyMissingError,
    EmptyDatasetError,
    IncompleteDatasetError,
    NoTradingDaysError,
    ProviderPayload,
    ProviderQueryError,
    UnsupportedDatasetError,
    classify_unexpected_error,
    safe_error_text,
)


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
_NORMALIZED = re.compile(r"^(?P<code>[0-9]{6})\.(?P<market>SH|SZ)$")
_BAOSTOCK = re.compile(r"^(?P<market>sh|sz)\.(?P<code>[0-9]{6})$")
_SH_INDEX_CODES = frozenset(
    {"000001", "000016", "000300", "000688", "000852", "000905"}
)
_SH_A_SHARE_PREFIXES = ("600", "601", "603", "605", "688", "689")
_SZ_A_SHARE_PREFIXES = ("000", "001", "002", "003", "300", "301")


def to_baostock_code(instrument_id: str) -> str:
    text = str(instrument_id).strip().upper()
    match = _NORMALIZED.fullmatch(text)
    if match is None:
        raise ValueError(f"unsupported BaoStock instrument: {instrument_id!r}")
    code = match.group("code")
    market = match.group("market")
    if market == "SH" and code[0] not in {"5", "6", "9"} and code not in _SH_INDEX_CODES:
        raise ValueError(f"instrument code {code} is inconsistent with .SH")
    if market == "SZ" and code[0] not in {"0", "1", "2", "3"}:
        raise ValueError(f"instrument code {code} is inconsistent with .SZ")
    return f"{market.lower()}.{code}"


def normalize_baostock_instrument(instrument_id: str) -> str:
    """Normalize supported SH/SZ aliases and reject ambiguous market mappings."""

    upper = str(instrument_id or "").strip().upper()
    if not upper:
        raise ValueError("BaoStock instrument must not be empty")
    if len(upper) == 9 and upper[6] == "." and upper[:6].isdigit():
        candidate = upper
    elif len(upper) == 8 and upper[:2] in {"SH", "SZ"} and upper[2:].isdigit():
        candidate = f"{upper[2:]}.{upper[:2]}"
    elif len(upper) == 8 and upper[:2] in {"0.", "1."} and upper[2:].isdigit():
        candidate = f"{upper[2:]}.{'SZ' if upper.startswith('0.') else 'SH'}"
    elif len(upper) == 6 and upper.isdigit():
        if upper[0] in {"5", "6", "9"}:
            candidate = f"{upper}.SH"
        elif upper[0] in {"0", "1", "2", "3"}:
            candidate = f"{upper}.SZ"
        else:
            raise ValueError(f"unsupported BaoStock instrument: {instrument_id!r}")
    else:
        raise ValueError(f"unsupported BaoStock instrument: {instrument_id!r}")
    # This is the sole cross-market validator for both Provider and consumers.
    to_baostock_code(candidate)
    return candidate


def normalize_a_share_stock_instrument(instrument_id: str) -> str:
    """Normalize an A-share stock alias, excluding funds, bonds, B shares and indexes."""

    candidate = normalize_baostock_instrument(instrument_id)
    code, exchange = candidate.split(".", 1)
    allowed = (
        code.startswith(_SH_A_SHARE_PREFIXES)
        if exchange == "SH"
        else code.startswith(_SZ_A_SHARE_PREFIXES)
    )
    if not allowed:
        raise ValueError(f"instrument is not an admitted A-share stock: {instrument_id!r}")
    return candidate


def from_baostock_code(provider_code: str) -> str:
    text = str(provider_code).strip().lower()
    match = _BAOSTOCK.fullmatch(text)
    if match is None:
        raise ValueError(f"unsupported BaoStock code: {provider_code!r}")
    normalized = f"{match.group('code')}.{match.group('market').upper()}"
    # Reuse the same cross-market checks in both directions.
    if to_baostock_code(normalized) != text:
        raise ValueError(f"non-canonical BaoStock code: {provider_code!r}")
    return normalized


class BaoStockProvider:
    provider_id = "baostock"
    upstream_source = "baostock"
    adapter_version = "baostock-adapter-v1"
    supported_datasets = frozenset({"daily_bar", "trade_calendar", "security_master"})

    _DAILY_FIELDS = (
        "date",
        "code",
        "open",
        "high",
        "low",
        "close",
        "preclose",
        "volume",
        "amount",
        "adjustflag",
        "tradestatus",
    )

    def __init__(
        self,
        *,
        sdk_loader: Callable[[], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sdk_loader = sdk_loader
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _load_sdk(self) -> Any:
        if self._sdk_loader is not None:
            return self._sdk_loader()
        try:
            return importlib.import_module("baostock")
        except ModuleNotFoundError as exc:
            if exc.name not in {None, "baostock"}:
                raise ProviderQueryError(
                    f"BaoStock SDK dependency import failed: {type(exc).__name__}"
                ) from exc
            raise DependencyMissingError(
                "BaoStock SDK is not installed; install the market-baostock extra"
            ) from exc
        except ImportError as exc:
            raise ProviderQueryError(
                f"BaoStock SDK import failed: {type(exc).__name__}"
            ) from exc

    @staticmethod
    def _check_result(result: Any, operation: str) -> None:
        code = str(getattr(result, "error_code", ""))
        message = str(getattr(result, "error_msg", ""))
        if code != "0":
            safe_message = safe_error_text(message)
            raise ProviderQueryError(
                f"BaoStock {operation} failed: error_code={code or '<missing>'}, "
                f"error_msg={safe_message}",
                raw_content=canonical_json_bytes(
                    {
                        "operation": operation,
                        "error_code": code or "<missing>",
                        "error_message": safe_message,
                    }
                ),
            )

    @classmethod
    def _query_rows(cls, result: Any, operation: str) -> tuple[list[str], list[list[str]]]:
        cls._check_result(result, operation)
        raw_fields = getattr(result, "fields", ())
        fields = (
            [item.strip() for item in raw_fields.split(",")]
            if isinstance(raw_fields, str)
            else [str(item).strip() for item in raw_fields]
        )
        if not fields or any(not item for item in fields):
            raise ProviderQueryError(f"BaoStock {operation} returned no field contract")
        rows: list[list[str]] = []
        try:
            while result.next():
                row = [str(item).strip() for item in result.get_row_data()]
                if len(row) != len(fields):
                    raise ProviderQueryError(
                        f"BaoStock {operation} row width differs from fields"
                    )
                rows.append(row)
        except ProviderQueryError:
            raise
        except Exception as exc:
            raise classify_unexpected_error(exc) from exc
        cls._check_result(result, operation)
        return fields, rows

    def fetch(self, request: MarketDataRequest) -> ProviderPayload:
        if request.dataset_type not in self.supported_datasets:
            raise UnsupportedDatasetError(
                f"BaoStock does not implement dataset {request.dataset_type!r}"
            )
        if request.dataset_type == "daily_bar" and request.adjustment != "none":
            raise UnsupportedDatasetError(
                "BaoStock V1 primary path only supports unadjusted daily bars"
            )
        sdk = self._load_sdk()
        try:
            login = sdk.login()
            self._check_result(login, "login")
            if request.dataset_type == "daily_bar":
                return self._daily_bar(sdk, request)
            if request.dataset_type == "trade_calendar":
                return self._trade_calendar(sdk, request)
            return self._security_master(sdk, request)
        except (DependencyMissingError, EmptyDatasetError, ProviderQueryError, UnsupportedDatasetError):
            raise
        except Exception as exc:
            raise classify_unexpected_error(exc) from exc
        finally:
            try:
                sdk.logout()
            except Exception:
                # Logout failure cannot turn a failed query into success, and no
                # mutable broker state exists in this read-only provider.
                pass

    def _daily_bar(self, sdk: Any, request: MarketDataRequest) -> ProviderPayload:
        provider_code = to_baostock_code(request.instrument_id)
        result = sdk.query_history_k_data_plus(
            provider_code,
            ",".join(self._DAILY_FIELDS),
            start_date=request.start_date.isoformat(),  # type: ignore[union-attr]
            end_date=request.end_date.isoformat(),  # type: ignore[union-attr]
            frequency="d",
            adjustflag="3",
        )
        fields, rows = self._query_rows(result, "query_history_k_data_plus")
        calendar_result = sdk.query_trade_dates(
            start_date=request.start_date.isoformat(),  # type: ignore[union-attr]
            end_date=request.end_date.isoformat(),  # type: ignore[union-attr]
        )
        calendar_fields, calendar_rows = self._query_rows(
            calendar_result, "query_trade_dates_for_daily_completeness"
        )
        raw_content = canonical_json_bytes(
            {
                "operation": "query_history_k_data_plus_with_trade_calendar_completeness",
                "request": request.fingerprint_payload(self.provider_id, self.adapter_version),
                "daily": {"fields": fields, "rows": rows},
                "trade_calendar": {
                    "fields": calendar_fields,
                    "rows": calendar_rows,
                },
            }
        )
        fetched_at = self._aware_clock()
        records = replay_baostock_raw(request, raw_content, fetched_at)
        return ProviderPayload(
            raw_content=raw_content,
            records=records,
            fetched_at=fetched_at,
            upstream_source="baostock.query_history_k_data_plus",
        )

    def _trade_calendar(self, sdk: Any, request: MarketDataRequest) -> ProviderPayload:
        result = sdk.query_trade_dates(
            start_date=request.start_date.isoformat(),  # type: ignore[union-attr]
            end_date=request.end_date.isoformat(),  # type: ignore[union-attr]
        )
        fields, rows = self._query_rows(result, "query_trade_dates")
        raw_content = canonical_json_bytes(
            {
                "operation": "query_trade_dates",
                "request": request.fingerprint_payload(self.provider_id, self.adapter_version),
                "fields": fields,
                "rows": rows,
            }
        )
        fetched_at = self._aware_clock()
        records = replay_baostock_raw(request, raw_content, fetched_at)
        return ProviderPayload(
            raw_content=raw_content,
            records=records,
            fetched_at=fetched_at,
            upstream_source="baostock.query_trade_dates",
        )

    @staticmethod
    def _validate_calendar_coverage(
        fields: list[str],
        rows: list[list[str]],
        request: MarketDataRequest,
        raw_content: bytes,
    ) -> set[str]:
        start_date = request.start_date
        end_date = request.end_date
        if start_date is None or end_date is None:
            raise ProviderQueryError(
                "BaoStock trade calendar requires an explicit date range",
                raw_content=raw_content,
            )
        date_counts: dict[str, int] = {}
        open_dates: set[str] = set()
        for values in rows:
            raw = dict(zip(fields, values, strict=True))
            day_text = raw.get("calendar_date", "")
            try:
                day = datetime.strptime(day_text, "%Y-%m-%d").date()
            except ValueError as exc:
                raise ProviderQueryError(
                    "BaoStock trade calendar returned an invalid calendar date",
                    raw_content=raw_content,
                ) from exc
            day_iso = day.isoformat()
            date_counts[day_iso] = date_counts.get(day_iso, 0) + 1
            flag = raw.get("is_trading_day")
            if flag not in {"0", "1"}:
                raise ProviderQueryError(
                    "BaoStock trade calendar returned an invalid trading-day flag",
                    raw_content=raw_content,
                )
            if flag == "1":
                open_dates.add(day_iso)
        expected_dates: set[str] = set()
        current = start_date
        while current <= end_date:
            expected_dates.add(current.isoformat())
            current += timedelta(days=1)
        actual_dates = set(date_counts)
        duplicate_dates = sorted(day for day, count in date_counts.items() if count != 1)
        if actual_dates != expected_dates or duplicate_dates:
            raise ProviderQueryError(
                "BaoStock trade calendar is incomplete for the requested natural-date range: "
                f"missing={sorted(expected_dates - actual_dates)}, "
                f"unexpected={sorted(actual_dates - expected_dates)}, "
                f"duplicates={duplicate_dates}",
                raw_content=raw_content,
            )
        return open_dates

    def _security_master(self, sdk: Any, request: MarketDataRequest) -> ProviderPayload:
        provider_code = to_baostock_code(request.instrument_id)
        result = sdk.query_stock_basic(code=provider_code)
        fields, rows = self._query_rows(result, "query_stock_basic")
        raw_content = canonical_json_bytes(
            {
                "operation": "query_stock_basic",
                "request": request.fingerprint_payload(self.provider_id, self.adapter_version),
                "fields": fields,
                "rows": rows,
            }
        )
        fetched_at = self._aware_clock()
        records = replay_baostock_raw(request, raw_content, fetched_at)
        return ProviderPayload(
            raw_content=raw_content,
            records=records,
            fetched_at=fetched_at,
            upstream_source="baostock.query_stock_basic",
        )

    def _aware_clock(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ProviderQueryError("BaoStock provider clock must include a timezone offset")
        return value


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _raw_table(
    value: Any,
    *,
    label: str,
    expected_fields: tuple[str, ...] | None = None,
) -> tuple[list[str], list[list[str]]]:
    if not isinstance(value, Mapping) or set(value) != {"fields", "rows"}:
        raise ProviderQueryError(f"BaoStock raw {label} table is malformed")
    fields = value.get("fields")
    rows = value.get("rows")
    if not isinstance(fields, list) or any(not isinstance(item, str) for item in fields):
        raise ProviderQueryError(f"BaoStock raw {label} fields are malformed")
    if len(fields) != len(set(fields)) or not fields:
        raise ProviderQueryError(f"BaoStock raw {label} fields are empty or duplicated")
    if expected_fields is not None and tuple(fields) != expected_fields:
        raise ProviderQueryError(f"BaoStock raw {label} field contract drifted")
    if not isinstance(rows, list):
        raise ProviderQueryError(f"BaoStock raw {label} rows are malformed")
    normalized_rows: list[list[str]] = []
    for row in rows:
        if (
            not isinstance(row, list)
            or len(row) != len(fields)
            or any(not isinstance(item, str) for item in row)
        ):
            raise ProviderQueryError(f"BaoStock raw {label} row contract drifted")
        normalized_rows.append(row)
    return fields, normalized_rows


def replay_baostock_raw(
    request: MarketDataRequest,
    raw_content: bytes,
    fetched_at: datetime,
) -> tuple[Mapping[str, Any], ...]:
    """Deterministically derive normalized records from stored BaoStock evidence."""

    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise ProviderQueryError("BaoStock replay fetched_at must include a timezone offset")
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
            f"BaoStock raw evidence is not strict JSON: {exc}",
            raw_content=raw_content,
        ) from exc
    if not isinstance(payload, dict) or canonical_json_bytes(payload) != raw_content:
        raise ProviderQueryError(
            "BaoStock raw evidence is not in deterministic canonical form",
            raw_content=raw_content,
        )
    expected_request = request.fingerprint_payload(
        BaoStockProvider.provider_id, BaoStockProvider.adapter_version
    )
    if payload.get("request") != expected_request:
        raise ProviderQueryError(
            "BaoStock raw evidence request differs from the batch request",
            raw_content=raw_content,
        )

    operation = payload.get("operation")
    if request.dataset_type == "daily_bar":
        expected_root = {"operation", "request", "daily", "trade_calendar"}
        if set(payload) != expected_root or operation != (
            "query_history_k_data_plus_with_trade_calendar_completeness"
        ):
            raise ProviderQueryError(
                "BaoStock daily raw envelope is malformed",
                raw_content=raw_content,
            )
        fields, rows = _raw_table(
            payload["daily"],
            label="daily",
            expected_fields=BaoStockProvider._DAILY_FIELDS,
        )
        calendar_fields, calendar_rows = _raw_table(
            payload["trade_calendar"],
            label="trade_calendar",
            expected_fields=("calendar_date", "is_trading_day"),
        )
        open_dates = BaoStockProvider._validate_calendar_coverage(
            calendar_fields,
            calendar_rows,
            request,
            raw_content,
        )
        if not open_dates:
            raise NoTradingDaysError(
                "BaoStock calendar confirms no exchange trading days in the requested window",
                raw_content=raw_content,
            )
        if not rows:
            raise IncompleteDatasetError(
                "BaoStock daily response is empty although the calendar contains trading days",
                raw_content=raw_content,
            )
        records: list[Mapping[str, Any]] = []
        for values in rows:
            raw = dict(zip(fields, values, strict=True))
            if raw.get("adjustflag") != "3":
                raise ProviderQueryError(
                    "BaoStock daily row is not the requested unadjusted adjustflag=3",
                    raw_content=raw_content,
                )
            trading_status = raw.get("tradestatus")
            if trading_status not in {"0", "1"}:
                raise ProviderQueryError(
                    "BaoStock daily row has invalid tradestatus; expected 0 or 1",
                    raw_content=raw_content,
                )
            try:
                returned_code = from_baostock_code(raw.get("code", ""))
                day = str(raw.get("date", ""))
                trading_day = datetime.strptime(day, "%Y-%m-%d").date()
            except ValueError as exc:
                raise ProviderQueryError(
                    f"BaoStock daily row has an invalid code or date: {exc}",
                    raw_content=raw_content,
                ) from exc
            if returned_code != request.instrument_id:
                raise ProviderQueryError(
                    f"BaoStock returned {returned_code}, requested {request.instrument_id}",
                    raw_content=raw_content,
                )
            available_at = datetime.combine(
                trading_day,
                time(15, 30),
                tzinfo=CHINA_TZ,
            )
            records.append(
                {
                    "instrument_id": returned_code,
                    "trading_date": day,
                    "open": raw.get("open", ""),
                    "high": raw.get("high", ""),
                    "low": raw.get("low", ""),
                    "close": raw.get("close", ""),
                    "preclose": raw.get("preclose", ""),
                    "volume": raw.get("volume", ""),
                    "amount": raw.get("amount", ""),
                    "currency": "CNY",
                    "adjustment": "none",
                    "trading_status": (
                        "traded" if trading_status == "1" else "suspended"
                    ),
                    "available_at": available_at.isoformat(),
                    "availability_status": "policy_estimated",
                    "source_record_id": sha256_bytes(canonical_json_bytes(raw)),
                }
            )
        returned_dates = {str(record["trading_date"]) for record in records}
        if returned_dates != open_dates:
            raise IncompleteDatasetError(
                "BaoStock daily response is incomplete against its trade calendar: "
                f"missing={sorted(open_dates - returned_dates)}, "
                f"unexpected={sorted(returned_dates - open_dates)}",
                raw_content=raw_content,
            )
        return tuple(records)

    if request.dataset_type == "trade_calendar":
        if set(payload) != {"operation", "request", "fields", "rows"} or operation != (
            "query_trade_dates"
        ):
            raise ProviderQueryError(
                "BaoStock trade-calendar raw envelope is malformed",
                raw_content=raw_content,
            )
        fields, rows = _raw_table(
            {"fields": payload["fields"], "rows": payload["rows"]},
            label="trade_calendar",
            expected_fields=("calendar_date", "is_trading_day"),
        )
        if not rows:
            raise EmptyDatasetError(
                "BaoStock returned no calendar rows", raw_content=raw_content
            )
        BaoStockProvider._validate_calendar_coverage(
            fields, rows, request, raw_content
        )
        return tuple(
            {
                "calendar_date": raw["calendar_date"],
                "is_trading_day": raw["is_trading_day"] == "1",
                "available_at": fetched_at.isoformat(),
                "availability_status": "unknown",
                "source_record_id": sha256_bytes(canonical_json_bytes(raw)),
            }
            for raw in (dict(zip(fields, values, strict=True)) for values in rows)
        )

    if request.dataset_type == "security_master":
        if set(payload) != {"operation", "request", "fields", "rows"} or operation != (
            "query_stock_basic"
        ):
            raise ProviderQueryError(
                "BaoStock security-master raw envelope is malformed",
                raw_content=raw_content,
            )
        fields, rows = _raw_table(
            {"fields": payload["fields"], "rows": payload["rows"]},
            label="security_master",
        )
        if set(fields) != {"code", "code_name", "ipoDate", "outDate", "type", "status"}:
            raise ProviderQueryError(
                "BaoStock security-master field contract drifted",
                raw_content=raw_content,
            )
        if not rows:
            raise EmptyDatasetError(
                "BaoStock security master returned no matching security",
                raw_content=raw_content,
            )
        type_names = {
            "1": "stock",
            "2": "index",
            "3": "other",
            "4": "convertible_bond",
            "5": "fund",
        }
        status_names = {"1": "listed", "0": "delisted"}
        records = []
        for values in rows:
            raw = dict(zip(fields, values, strict=True))
            if raw.get("type") not in type_names or raw.get("status") not in status_names:
                raise ProviderQueryError(
                    "BaoStock security master returned an unknown type or status code",
                    raw_content=raw_content,
                )
            try:
                returned_code = from_baostock_code(raw.get("code", ""))
            except ValueError as exc:
                raise ProviderQueryError(
                    f"BaoStock security master returned an invalid code: {exc}",
                    raw_content=raw_content,
                ) from exc
            records.append(
                {
                    "instrument_id": returned_code,
                    "provider_instrument_id": raw.get("code", ""),
                    "security_name": raw.get("code_name", ""),
                    "exchange": returned_code[-2:],
                    "security_type": type_names.get(raw.get("type", ""), "unknown"),
                    "listing_status": status_names.get(raw.get("status", ""), "unknown"),
                    "list_date": raw.get("ipoDate") or None,
                    "delist_date": raw.get("outDate") or None,
                    "available_at": fetched_at.isoformat(),
                    "availability_status": "current_snapshot_not_pit",
                    "source_record_id": sha256_bytes(canonical_json_bytes(raw)),
                }
            )
        return tuple(records)

    raise UnsupportedDatasetError(
        f"BaoStock raw replay does not support {request.dataset_type!r}"
    )


__all__ = [
    "BaoStockProvider",
    "from_baostock_code",
    "normalize_a_share_stock_instrument",
    "normalize_baostock_instrument",
    "replay_baostock_raw",
    "to_baostock_code",
]
