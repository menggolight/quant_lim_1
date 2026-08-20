"""Schema-adjacent domain validation for normalized market-data records."""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

from .contracts import (
    MarketDataContractError,
    MarketDataRequest,
    aware_datetime,
    decimal_text,
    iso_date,
)


class DomainValidationError(MarketDataContractError):
    """Raised when records have the right container but unsafe semantics."""


class SchemaValidationError(MarketDataContractError):
    """Raised when normalized evidence violates a versioned JSON Schema."""


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_SCHEMA_PATHS = {
    "daily_bar": REPO_ROOT / "schemas" / "daily_bar.v1.json",
    "trade_calendar": REPO_ROOT / "schemas" / "trade_calendar.v1.json",
    "security_master": REPO_ROOT / "schemas" / "security_master.v1.json",
}
MARKET_DATA_BATCH_SCHEMA_PATH = REPO_ROOT / "schemas" / "market_data_batch.v1.json"


_INSTRUMENT = re.compile(r"^[0-9]{6}\.(?:SH|SZ)$")
_DAILY_FIELDS = frozenset(
    {
        "instrument_id",
        "trading_date",
        "open",
        "high",
        "low",
        "close",
        "preclose",
        "volume",
        "amount",
        "currency",
        "adjustment",
        "trading_status",
        "available_at",
        "availability_status",
        "source_record_id",
    }
)
_CALENDAR_FIELDS = frozenset(
    {
        "calendar_date",
        "is_trading_day",
        "available_at",
        "availability_status",
        "source_record_id",
    }
)
_SECURITY_FIELDS = frozenset(
    {
        "instrument_id",
        "provider_instrument_id",
        "security_name",
        "exchange",
        "security_type",
        "listing_status",
        "list_date",
        "delist_date",
        "available_at",
        "availability_status",
        "source_record_id",
    }
)


@lru_cache(maxsize=None)
def _load_schema(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SchemaValidationError(f"cannot load JSON Schema {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SchemaValidationError(f"JSON Schema root must be an object: {path}")
    return payload


def _schema_type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, (list, tuple))
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    if expected == "number":
        return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)
    raise SchemaValidationError(f"unsupported JSON Schema type: {expected}")


def _resolve_schema_ref(root: Mapping[str, Any], reference: str) -> Mapping[str, Any]:
    if not reference.startswith("#/"):
        raise SchemaValidationError(
            f"external JSON Schema references are unsupported: {reference}"
        )
    current: Any = root
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, Mapping) or part not in current:
            raise SchemaValidationError(f"unresolvable JSON Schema reference: {reference}")
        current = current[part]
    if not isinstance(current, Mapping):
        raise SchemaValidationError(f"JSON Schema reference is not an object: {reference}")
    return current


def _validate_schema_node(
    value: Any,
    schema: Mapping[str, Any],
    root: Mapping[str, Any],
    path: str,
) -> None:
    reference = schema.get("$ref")
    if reference is not None:
        if not isinstance(reference, str):
            raise SchemaValidationError(f"{path}: $ref must be a string")
        _validate_schema_node(value, _resolve_schema_ref(root, reference), root, path)
        return
    if "const" in schema and value != schema["const"]:
        raise SchemaValidationError(f"{path}: value differs from const")
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaValidationError(f"{path}: value is outside enum")

    expected = schema.get("type")
    if expected is not None:
        choices = expected if isinstance(expected, list) else [expected]
        if not any(
            isinstance(choice, str) and _schema_type_matches(value, choice)
            for choice in choices
        ):
            raise SchemaValidationError(f"{path}: value has the wrong JSON type")

    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        matches = 0
        for candidate in one_of:
            if not isinstance(candidate, Mapping):
                continue
            try:
                _validate_schema_node(value, candidate, root, path)
            except SchemaValidationError:
                continue
            matches += 1
        if matches != 1:
            raise SchemaValidationError(f"{path}: oneOf matched {matches} branches")

    if isinstance(value, Mapping):
        required = schema.get("required", [])
        if isinstance(required, list):
            missing = [field for field in required if field not in value]
            if missing:
                raise SchemaValidationError(f"{path}: missing required fields {missing}")
        properties = schema.get("properties", {})
        if isinstance(properties, Mapping):
            if schema.get("additionalProperties") is False:
                unknown = sorted(str(field) for field in set(value) - set(properties))
                if unknown:
                    raise SchemaValidationError(f"{path}: unknown fields {unknown}")
            for field, child in properties.items():
                if field in value and isinstance(child, Mapping):
                    _validate_schema_node(value[field], child, root, f"{path}.{field}")
    elif isinstance(value, (list, tuple)):
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, item in enumerate(value):
                _validate_schema_node(item, items, root, f"{path}[{index}]")
    elif isinstance(value, str):
        if int(schema.get("minLength", 0)) > len(value):
            raise SchemaValidationError(f"{path}: string is shorter than minLength")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            raise SchemaValidationError(f"{path}: string exceeds maxLength")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            raise SchemaValidationError(f"{path}: string does not match pattern")
    elif isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise SchemaValidationError(f"{path}: number is below minimum")

    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for candidate in all_of:
            if not isinstance(candidate, Mapping):
                continue
            condition = candidate.get("if")
            applies = True
            if isinstance(condition, Mapping):
                try:
                    _validate_schema_node(value, condition, root, path)
                except SchemaValidationError:
                    applies = False
            then = candidate.get("then")
            if applies and isinstance(then, Mapping):
                _validate_schema_node(value, then, root, path)


def validate_json_schema(value: Any, schema_path: Path) -> None:
    schema = _load_schema(schema_path.resolve())
    _validate_schema_node(value, schema, schema, "$")


def validate_normalized_record_schemas(
    dataset_type: str,
    records: Iterable[Mapping[str, Any]],
) -> None:
    schema_path = DATASET_SCHEMA_PATHS.get(dataset_type)
    if schema_path is None:
        raise SchemaValidationError(
            f"dataset {dataset_type!r} has no executable record Schema"
        )
    for index, record in enumerate(records):
        try:
            validate_json_schema(record, schema_path)
        except SchemaValidationError as exc:
            raise SchemaValidationError(f"{dataset_type}[{index}]: {exc}") from exc


def validate_market_data_batch_schema(payload: Mapping[str, Any]) -> None:
    validate_json_schema(payload, MARKET_DATA_BATCH_SCHEMA_PATH)


def _strict_record(record: Mapping[str, Any], expected: frozenset[str], label: str) -> dict[str, Any]:
    missing = sorted(expected - set(record))
    unknown = sorted(set(record) - expected)
    if missing:
        raise DomainValidationError(f"{label} missing fields: {', '.join(missing)}")
    if unknown:
        raise DomainValidationError(f"{label} has unknown fields: {', '.join(unknown)}")
    return dict(record)


def _text(value: Any, field_name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise DomainValidationError(f"{field_name} must be a string")
    text = value.strip()
    if not text and not allow_empty:
        raise DomainValidationError(f"{field_name} must not be empty")
    return text


def _instrument(value: Any, field_name: str) -> str:
    text = _text(value, field_name).upper()
    if _INSTRUMENT.fullmatch(text) is None:
        raise DomainValidationError(f"{field_name} must be a supported .SH or .SZ instrument")
    return text


def _availability(value: Any, field_name: str) -> str:
    status = _text(value, field_name)
    if status not in {"known", "policy_estimated", "unknown", "current_snapshot_not_pit"}:
        raise DomainValidationError(f"unsupported {field_name}: {status!r}")
    return status


def validate_daily_bars(
    request: MarketDataRequest,
    records: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    normalized: list[dict[str, Any]] = []
    seen_dates: set[date] = set()
    seen_source_ids: set[str] = set()
    prior_date: date | None = None
    for index, supplied in enumerate(records):
        row = _strict_record(supplied, _DAILY_FIELDS, f"daily_bar[{index}]")
        instrument = _instrument(row["instrument_id"], f"daily_bar[{index}].instrument_id")
        if instrument != request.instrument_id:
            raise DomainValidationError(
                f"daily_bar[{index}] returned {instrument}, requested {request.instrument_id}"
            )
        trading_date = iso_date(row["trading_date"], f"daily_bar[{index}].trading_date", required=True)
        assert trading_date is not None
        if request.start_date and trading_date < request.start_date or request.end_date and trading_date > request.end_date:
            raise DomainValidationError(f"daily_bar[{index}] is outside the requested window")
        if trading_date in seen_dates:
            raise DomainValidationError(f"duplicate daily_bar date: {trading_date.isoformat()}")
        if prior_date is not None and trading_date <= prior_date:
            raise DomainValidationError("daily_bar dates must be strictly ascending")
        seen_dates.add(trading_date)
        prior_date = trading_date

        decimal_fields = {
            name: decimal_text(row[name], f"daily_bar[{index}].{name}")
            for name in ("open", "high", "low", "close", "preclose", "volume", "amount")
        }
        prices = {name: Decimal(decimal_fields[name]) for name in ("open", "high", "low", "close", "preclose")}
        if min(prices.values()) <= 0:
            raise DomainValidationError(f"daily_bar[{index}] prices must be positive")
        if prices["high"] < max(prices["open"], prices["low"], prices["close"]):
            raise DomainValidationError(f"daily_bar[{index}].high does not cover OHLC")
        if prices["low"] > min(prices["open"], prices["high"], prices["close"]):
            raise DomainValidationError(f"daily_bar[{index}].low does not cover OHLC")
        if Decimal(decimal_fields["volume"]) < 0 or Decimal(decimal_fields["amount"]) < 0:
            raise DomainValidationError(f"daily_bar[{index}] volume and amount must be non-negative")
        adjustment = _text(row["adjustment"], f"daily_bar[{index}].adjustment").lower()
        if adjustment not in {"none", "qfq", "hfq"} or adjustment != request.adjustment:
            raise DomainValidationError(f"daily_bar[{index}] adjustment does not match the request")
        currency = _text(row["currency"], f"daily_bar[{index}].currency").upper()
        if currency != "CNY":
            raise DomainValidationError("daily_bar currency must be CNY")
        trading_status = _text(
            row["trading_status"], f"daily_bar[{index}].trading_status"
        ).lower()
        if trading_status not in {"traded", "suspended", "unknown"}:
            raise DomainValidationError("daily_bar trading_status is unsupported")
        source_record_id = _text(row["source_record_id"], f"daily_bar[{index}].source_record_id")
        if source_record_id in seen_source_ids:
            raise DomainValidationError("daily_bar source_record_id values must be unique")
        seen_source_ids.add(source_record_id)
        normalized.append(
            {
                "instrument_id": instrument,
                "trading_date": trading_date.isoformat(),
                **decimal_fields,
                "currency": currency,
                "adjustment": adjustment,
                "trading_status": trading_status,
                "available_at": aware_datetime(row["available_at"], f"daily_bar[{index}].available_at").isoformat(),
                "availability_status": _availability(row["availability_status"], f"daily_bar[{index}].availability_status"),
                "source_record_id": source_record_id,
            }
        )
    if not normalized:
        raise DomainValidationError("daily_bar result is empty")
    return tuple(normalized)


def validate_trade_calendar(
    request: MarketDataRequest,
    records: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    normalized: list[dict[str, Any]] = []
    prior_date: date | None = None
    seen: set[date] = set()
    for index, supplied in enumerate(records):
        row = _strict_record(supplied, _CALENDAR_FIELDS, f"trade_calendar[{index}]")
        day = iso_date(row["calendar_date"], f"trade_calendar[{index}].calendar_date", required=True)
        assert day is not None
        if request.start_date and day < request.start_date or request.end_date and day > request.end_date:
            raise DomainValidationError(f"trade_calendar[{index}] is outside the requested window")
        if day in seen or prior_date is not None and day <= prior_date:
            raise DomainValidationError("trade_calendar dates must be unique and strictly ascending")
        if type(row["is_trading_day"]) is not bool:
            raise DomainValidationError("trade_calendar.is_trading_day must be a boolean")
        seen.add(day)
        prior_date = day
        normalized.append(
            {
                "calendar_date": day.isoformat(),
                "is_trading_day": row["is_trading_day"],
                "available_at": aware_datetime(row["available_at"], f"trade_calendar[{index}].available_at").isoformat(),
                "availability_status": _availability(row["availability_status"], f"trade_calendar[{index}].availability_status"),
                "source_record_id": _text(row["source_record_id"], f"trade_calendar[{index}].source_record_id"),
            }
        )
    if not normalized:
        raise DomainValidationError("trade_calendar result is empty")
    return tuple(normalized)


def validate_security_master(
    request: MarketDataRequest,
    records: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    normalized: list[dict[str, Any]] = []
    for index, supplied in enumerate(records):
        row = _strict_record(supplied, _SECURITY_FIELDS, f"security_master[{index}]")
        instrument = _instrument(row["instrument_id"], f"security_master[{index}].instrument_id")
        if instrument != request.instrument_id:
            raise DomainValidationError("security_master returned a different instrument")
        exchange = _text(row["exchange"], f"security_master[{index}].exchange").upper()
        if exchange != instrument[-2:]:
            raise DomainValidationError("security_master exchange does not match instrument_id")
        security_type = _text(
            row["security_type"], f"security_master[{index}].security_type"
        ).lower()
        if security_type not in {
            "stock",
            "index",
            "fund",
            "bond",
            "convertible_bond",
            "other",
            "unknown",
        }:
            raise DomainValidationError("security_master security_type is unsupported")
        listing_status = _text(
            row["listing_status"], f"security_master[{index}].listing_status"
        ).lower()
        if listing_status not in {"listed", "delisted", "suspended", "unknown"}:
            raise DomainValidationError("security_master listing_status is unsupported")
        list_date = iso_date(row["list_date"], f"security_master[{index}].list_date")
        delist_date = iso_date(row["delist_date"], f"security_master[{index}].delist_date")
        if list_date and delist_date and list_date > delist_date:
            raise DomainValidationError("security_master list_date cannot follow delist_date")
        normalized.append(
            {
                "instrument_id": instrument,
                "provider_instrument_id": _text(row["provider_instrument_id"], f"security_master[{index}].provider_instrument_id"),
                "security_name": _text(row["security_name"], f"security_master[{index}].security_name"),
                "exchange": exchange,
                "security_type": security_type,
                "listing_status": listing_status,
                "list_date": list_date.isoformat() if list_date else None,
                "delist_date": delist_date.isoformat() if delist_date else None,
                "available_at": aware_datetime(row["available_at"], f"security_master[{index}].available_at").isoformat(),
                "availability_status": _availability(row["availability_status"], f"security_master[{index}].availability_status"),
                "source_record_id": _text(row["source_record_id"], f"security_master[{index}].source_record_id"),
            }
        )
    if len(normalized) != 1:
        raise DomainValidationError("security_master requires exactly one matching record")
    return tuple(normalized)


def validate_and_normalize(
    request: MarketDataRequest,
    records: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    if request.dataset_type == "daily_bar":
        return validate_daily_bars(request, records)
    if request.dataset_type == "trade_calendar":
        return validate_trade_calendar(request, records)
    if request.dataset_type == "security_master":
        return validate_security_master(request, records)
    raise DomainValidationError(
        f"dataset {request.dataset_type!r} has no admitted V1 domain validator"
    )
