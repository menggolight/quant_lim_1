"""Schema-adjacent domain validation for normalized market-data records."""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import unquote, urldefrag, urlsplit

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


def _is_json_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Decimal):
        return value.is_finite()
    return False


def _schema_type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, (list, tuple))
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        if not _is_json_number(value):
            return False
        if isinstance(value, int):
            return True
        if isinstance(value, float):
            return value.is_integer()
        return value == value.to_integral_value()
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    if expected == "number":
        return _is_json_number(value)
    raise SchemaValidationError(f"unsupported JSON Schema type: {expected}")


def _json_equal(left: Any, right: Any) -> bool:
    """JSON equality without Python's surprising ``False == 0`` behavior."""

    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if _is_json_number(left) and _is_json_number(right):
        return Decimal(str(left)) == Decimal(str(right))
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, str) or isinstance(right, str):
        return isinstance(left, str) and isinstance(right, str) and left == right
    if isinstance(left, Mapping) or isinstance(right, Mapping):
        if not isinstance(left, Mapping) or not isinstance(right, Mapping):
            return False
        if set(left) != set(right):
            return False
        return all(_json_equal(left[key], right[key]) for key in left)
    if isinstance(left, (list, tuple)) or isinstance(right, (list, tuple)):
        if not isinstance(left, (list, tuple)) or not isinstance(right, (list, tuple)):
            return False
        return len(left) == len(right) and all(
            _json_equal(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    return type(left) is type(right) and left == right


def _resolve_json_pointer(root: Any, fragment: str, reference: str) -> Any:
    if not fragment:
        return root
    if not fragment.startswith("/"):
        raise SchemaValidationError(
            f"unsupported JSON Schema fragment in reference: {reference}"
        )
    current: Any = root
    for raw_part in fragment[1:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, Mapping) or part not in current:
            raise SchemaValidationError(f"unresolvable JSON Schema reference: {reference}")
        current = current[part]
    return current


def _resolve_schema_ref(
    root: Mapping[str, Any],
    reference: str,
    document_path: Path,
) -> tuple[Any, Mapping[str, Any], Path]:
    resource, fragment = urldefrag(reference)
    target_root = root
    target_path = document_path
    if resource:
        parsed = urlsplit(resource)
        if parsed.scheme or parsed.netloc or parsed.query:
            raise SchemaValidationError(
                f"remote JSON Schema references are unsupported: {reference}"
            )
        target_path = (document_path.parent / unquote(parsed.path)).resolve()
        try:
            target_path.relative_to(document_path.parent.resolve())
        except ValueError as exc:
            raise SchemaValidationError(
                f"JSON Schema reference leaves its schema directory: {reference}"
            ) from exc
        target_root = _load_schema(target_path)
    target = _resolve_json_pointer(target_root, fragment, reference)
    if not isinstance(target, (Mapping, bool)):
        raise SchemaValidationError(
            f"JSON Schema reference is not a schema: {reference}"
        )
    return target, target_root, target_path


def _schema_matches(
    value: Any,
    schema: Any,
    root: Mapping[str, Any],
    path: str,
    document_path: Path,
) -> tuple[bool, set[str]]:
    try:
        evaluated = _validate_schema_node(value, schema, root, path, document_path)
    except SchemaValidationError:
        return False, set()
    return True, evaluated


def _validate_format(value: str, format_name: str, path: str) -> None:
    if format_name == "date":
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
            raise SchemaValidationError(f"{path}: string is not an RFC 3339 date")
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise SchemaValidationError(
                f"{path}: string is not an RFC 3339 date"
            ) from exc
        return
    if format_name == "date-time":
        if re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
            r"(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})",
            value,
        ) is None:
            raise SchemaValidationError(f"{path}: string is not an RFC 3339 date-time")
        try:
            parsed = datetime.fromisoformat(value.replace("z", "+00:00").replace("Z", "+00:00"))
        except ValueError as exc:
            raise SchemaValidationError(
                f"{path}: string is not an RFC 3339 date-time"
            ) from exc
        if parsed.utcoffset() is None:
            raise SchemaValidationError(f"{path}: date-time must include a UTC offset")


def _numeric(value: Any) -> Decimal:
    return Decimal(str(value))


def _validate_schema_node(
    value: Any,
    schema: Any,
    root: Mapping[str, Any],
    path: str,
    document_path: Path,
) -> set[str]:
    if schema is True:
        return set()
    if schema is False:
        raise SchemaValidationError(f"{path}: value is rejected by false schema")
    if not isinstance(schema, Mapping):
        raise SchemaValidationError(f"{path}: JSON Schema node must be an object or boolean")

    evaluated_properties: set[str] = set()
    reference = schema.get("$ref")
    if reference is not None:
        if not isinstance(reference, str):
            raise SchemaValidationError(f"{path}: $ref must be a string")
        target, target_root, target_path = _resolve_schema_ref(
            root, reference, document_path
        )
        evaluated_properties.update(
            _validate_schema_node(value, target, target_root, path, target_path)
        )

    if "const" in schema and not _json_equal(value, schema["const"]):
        raise SchemaValidationError(f"{path}: value differs from const")
    if "enum" in schema:
        choices = schema["enum"]
        if not isinstance(choices, list):
            raise SchemaValidationError(f"{path}: enum must be an array")
        if not any(_json_equal(value, choice) for choice in choices):
            raise SchemaValidationError(f"{path}: value is outside enum")

    expected = schema.get("type")
    if expected is not None:
        choices = expected if isinstance(expected, list) else [expected]
        if not choices or not all(isinstance(choice, str) for choice in choices):
            raise SchemaValidationError(f"{path}: type must be a string or string array")
        if not any(
            _schema_type_matches(value, choice) for choice in choices
        ):
            raise SchemaValidationError(f"{path}: value has the wrong JSON type")

    any_of = schema.get("anyOf")
    if any_of is not None:
        if not isinstance(any_of, list) or not any_of:
            raise SchemaValidationError(f"{path}: anyOf must be a non-empty array")
        matches = [
            annotations
            for candidate in any_of
            for matched, annotations in [
                _schema_matches(value, candidate, root, path, document_path)
            ]
            if matched
        ]
        if not matches:
            raise SchemaValidationError(f"{path}: anyOf matched 0 branches")
        for annotations in matches:
            evaluated_properties.update(annotations)

    one_of = schema.get("oneOf")
    if one_of is not None:
        if not isinstance(one_of, list) or not one_of:
            raise SchemaValidationError(f"{path}: oneOf must be a non-empty array")
        matches = [
            annotations
            for candidate in one_of
            for matched, annotations in [
                _schema_matches(value, candidate, root, path, document_path)
            ]
            if matched
        ]
        if len(matches) != 1:
            raise SchemaValidationError(f"{path}: oneOf matched {len(matches)} branches")
        evaluated_properties.update(matches[0])

    all_of = schema.get("allOf")
    if all_of is not None:
        if not isinstance(all_of, list) or not all_of:
            raise SchemaValidationError(f"{path}: allOf must be a non-empty array")
        for candidate in all_of:
            evaluated_properties.update(
                _validate_schema_node(value, candidate, root, path, document_path)
            )

    condition = schema.get("if")
    if condition is not None:
        applies, _ = _schema_matches(value, condition, root, path, document_path)
        selected = schema.get("then") if applies else schema.get("else")
        if selected is not None:
            evaluated_properties.update(
                _validate_schema_node(value, selected, root, path, document_path)
            )

    if "not" in schema:
        matched, _ = _schema_matches(
            value, schema["not"], root, path, document_path
        )
        if matched:
            raise SchemaValidationError(f"{path}: value matches forbidden not schema")

    if isinstance(value, Mapping):
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(
            isinstance(field, str) for field in required
        ):
            raise SchemaValidationError(f"{path}: required must be a string array")
        missing = [field for field in required if field not in value]
        if missing:
            raise SchemaValidationError(f"{path}: missing required fields {missing}")

        if "minProperties" in schema and len(value) < int(schema["minProperties"]):
            raise SchemaValidationError(f"{path}: object has fewer than minProperties")
        if "maxProperties" in schema and len(value) > int(schema["maxProperties"]):
            raise SchemaValidationError(f"{path}: object exceeds maxProperties")

        property_names = schema.get("propertyNames")
        if property_names is not None:
            for field in value:
                if not isinstance(field, str):
                    raise SchemaValidationError(f"{path}: JSON object keys must be strings")
                _validate_schema_node(
                    field,
                    property_names,
                    root,
                    f"{path}.<property:{field}>",
                    document_path,
                )

        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            raise SchemaValidationError(f"{path}: properties must be an object")
        locally_evaluated: set[str] = set()
        for field, child in properties.items():
            if field in value:
                _validate_schema_node(
                    value[field], child, root, f"{path}.{field}", document_path
                )
                locally_evaluated.add(field)

        pattern_properties = schema.get("patternProperties", {})
        if not isinstance(pattern_properties, Mapping):
            raise SchemaValidationError(f"{path}: patternProperties must be an object")
        for pattern, child in pattern_properties.items():
            if not isinstance(pattern, str):
                raise SchemaValidationError(
                    f"{path}: patternProperties keys must be strings"
                )
            try:
                matching_fields = [field for field in value if re.search(pattern, field)]
            except re.error as exc:
                raise SchemaValidationError(
                    f"{path}: invalid patternProperties expression"
                ) from exc
            for field in matching_fields:
                _validate_schema_node(
                    value[field], child, root, f"{path}.{field}", document_path
                )
                locally_evaluated.add(field)

        unmatched = set(value) - locally_evaluated
        if "additionalProperties" in schema:
            additional = schema["additionalProperties"]
            if additional is False and unmatched:
                unknown = sorted(str(field) for field in unmatched)
                raise SchemaValidationError(f"{path}: unknown fields {unknown}")
            if additional is not False:
                for field in unmatched:
                    _validate_schema_node(
                        value[field],
                        additional,
                        root,
                        f"{path}.{field}",
                        document_path,
                    )
                locally_evaluated.update(unmatched)
        evaluated_properties.update(locally_evaluated)

        dependent_required = schema.get("dependentRequired", {})
        if not isinstance(dependent_required, Mapping):
            raise SchemaValidationError(f"{path}: dependentRequired must be an object")
        for trigger, dependencies in dependent_required.items():
            if trigger not in value:
                continue
            if not isinstance(dependencies, list) or not all(
                isinstance(field, str) for field in dependencies
            ):
                raise SchemaValidationError(
                    f"{path}: dependentRequired values must be string arrays"
                )
            missing_dependencies = [
                field for field in dependencies if field not in value
            ]
            if missing_dependencies:
                raise SchemaValidationError(
                    f"{path}: missing dependent fields {missing_dependencies}"
                )

        if "unevaluatedProperties" in schema:
            unevaluated = set(value) - evaluated_properties
            unevaluated_schema = schema["unevaluatedProperties"]
            if unevaluated_schema is False and unevaluated:
                unknown = sorted(str(field) for field in unevaluated)
                raise SchemaValidationError(
                    f"{path}: unevaluated fields {unknown}"
                )
            if unevaluated_schema is not False:
                for field in unevaluated:
                    _validate_schema_node(
                        value[field],
                        unevaluated_schema,
                        root,
                        f"{path}.{field}",
                        document_path,
                    )
                evaluated_properties.update(unevaluated)
    elif isinstance(value, (list, tuple)):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            raise SchemaValidationError(f"{path}: array has fewer than minItems")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            raise SchemaValidationError(f"{path}: array exceeds maxItems")
        if schema.get("uniqueItems") is True:
            for index, item in enumerate(value):
                if any(_json_equal(item, prior) for prior in value[:index]):
                    raise SchemaValidationError(
                        f"{path}: array items are not unique"
                    )
        items = schema.get("items")
        if items is not None:
            for index, item in enumerate(value):
                _validate_schema_node(
                    item, items, root, f"{path}[{index}]", document_path
                )
    elif isinstance(value, str):
        if int(schema.get("minLength", 0)) > len(value):
            raise SchemaValidationError(f"{path}: string is shorter than minLength")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            raise SchemaValidationError(f"{path}: string exceeds maxLength")
        pattern = schema.get("pattern")
        if pattern is not None:
            if not isinstance(pattern, str):
                raise SchemaValidationError(f"{path}: pattern must be a string")
            try:
                matches = re.search(pattern, value)
            except re.error as exc:
                raise SchemaValidationError(f"{path}: invalid regex pattern") from exc
            if matches is None:
                raise SchemaValidationError(f"{path}: string does not match pattern")
        format_name = schema.get("format")
        if format_name is not None:
            if not isinstance(format_name, str):
                raise SchemaValidationError(f"{path}: format must be a string")
            _validate_format(value, format_name, path)
    elif _is_json_number(value):
        number = _numeric(value)
        if "minimum" in schema and number < _numeric(schema["minimum"]):
            raise SchemaValidationError(f"{path}: number is below minimum")
        if "maximum" in schema and number > _numeric(schema["maximum"]):
            raise SchemaValidationError(f"{path}: number exceeds maximum")
        if "exclusiveMinimum" in schema and number <= _numeric(
            schema["exclusiveMinimum"]
        ):
            raise SchemaValidationError(
                f"{path}: number is not above exclusiveMinimum"
            )
        if "exclusiveMaximum" in schema and number >= _numeric(
            schema["exclusiveMaximum"]
        ):
            raise SchemaValidationError(
                f"{path}: number is not below exclusiveMaximum"
            )
        if "multipleOf" in schema:
            divisor = _numeric(schema["multipleOf"])
            if divisor <= 0:
                raise SchemaValidationError(f"{path}: multipleOf must be positive")
            if number % divisor != 0:
                raise SchemaValidationError(f"{path}: number is not a multipleOf")

    return evaluated_properties


def validate_json_schema(value: Any, schema_path: Path) -> None:
    resolved_path = schema_path.resolve()
    schema = _load_schema(resolved_path)
    _validate_schema_node(value, schema, schema, "$", resolved_path)


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
