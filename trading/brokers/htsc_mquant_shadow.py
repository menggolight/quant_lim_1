"""Validate snapshots written by the official Huatai MQuant runtime.

This adapter has no submission or cancellation capability.  The public MQuant
manual describes an in-client API and forbids network/IPC access, so the safe
integration boundary is an atomically written local JSON snapshot.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from trading.brokers.models import (
    BrokerFunds,
    BrokerOrder,
    BrokerPosition,
    BrokerTrade,
    RawBrokerSnapshot,
)


SCHEMA_VERSION = "htsc-mquant-shadow/1"
MAX_SNAPSHOT_BYTES = 20 * 1024 * 1024
_BINDING_PATTERN = re.compile(r"^htsc-local-[0-9a-f]{32}$")
_ACCOUNT_FINGERPRINT_PATTERN = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")
_LOCAL_SHAPE_PATTERN = re.compile(r"^local-shape-sha256:[0-9a-f]{64}$")
_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_REQUIRED_SECTIONS = ("funds", "positions", "open_orders", "today_orders", "trades")
_ROOT_KEYS = {
    "schema_version",
    "capabilities",
    "source",
    "capture",
    "funds",
    "positions",
    "open_orders",
    "today_orders",
    "trades",
    "payload_sha256",
}


class SnapshotValidationError(ValueError):
    """The spool file is not safe to use as a broker fact source."""


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SnapshotValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SnapshotValidationError(f"{field} must be an object")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], field: str) -> None:
    actual = set(value)
    missing = expected - actual
    unknown = actual - expected
    if missing:
        raise SnapshotValidationError(
            f"{field} is missing fields: {','.join(sorted(missing))}"
        )
    if unknown:
        raise SnapshotValidationError(
            f"{field} contains unknown fields: {','.join(sorted(unknown))}"
        )


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise SnapshotValidationError(f"{field} must be an array")
    return value


def _text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str or (not allow_empty and not value):
        raise SnapshotValidationError(f"{field} must be a string")
    return value


def _integer(value: Any, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise SnapshotValidationError(f"{field} must be an integer >= {minimum}")
    return value


def _side(value: Any, field: str) -> str:
    parsed = _text(value, field)
    if parsed not in {"BUY", "SELL", "UNKNOWN"}:
        raise SnapshotValidationError(f"{field} has an unknown value")
    return parsed


def _decimal(value: Any, field: str) -> Decimal:
    if type(value) is not str:
        raise SnapshotValidationError(f"{field} must be a decimal string")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise SnapshotValidationError(f"{field} is not a decimal") from exc
    if not parsed.is_finite() or parsed < 0:
        raise SnapshotValidationError(f"{field} must be finite and non-negative")
    return parsed


def _timestamp(value: Any, field: str, *, allow_empty: bool = False) -> datetime | None:
    if value is None and allow_empty:
        return None
    text = _text(value, field, allow_empty=allow_empty)
    if not text and allow_empty:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SnapshotValidationError(f"{field} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise SnapshotValidationError(f"{field} must include a timezone")
    return parsed


def _canonical_hash(payload: dict[str, Any]) -> str:
    body = dict(payload)
    body.pop("payload_sha256", None)
    encoded = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _unique(items: Iterable[Any], key_name: str, field: str) -> None:
    seen: set[str] = set()
    for item in items:
        key = getattr(item, key_name)
        if key in seen:
            raise SnapshotValidationError(f"duplicate {field}: {key}")
        seen.add(key)


def _parse_order(value: Any, field: str) -> BrokerOrder:
    item = _mapping(value, field)
    _exact_keys(
        item,
        {
            "order_id",
            "entrust_no",
            "symbol",
            "side",
            "status",
            "quantity",
            "filled_quantity",
            "withdrawn_quantity",
            "entrust_price",
            "average_price",
            "created_at",
            "cancel_info",
        },
        field,
    )
    symbol = _text(item.get("symbol"), f"{field}.symbol")
    if not _SYMBOL_PATTERN.fullmatch(symbol):
        raise SnapshotValidationError(f"{field}.symbol has an unsafe format")
    return BrokerOrder(
        broker_order_id=_text(item.get("order_id"), f"{field}.order_id"),
        entrust_no=_text(item.get("entrust_no", ""), f"{field}.entrust_no", allow_empty=True),
        instrument_id=symbol,
        side=_side(item.get("side"), f"{field}.side"),
        status=_text(item.get("status"), f"{field}.status"),
        quantity=_integer(item.get("quantity"), f"{field}.quantity"),
        filled_quantity=_integer(item.get("filled_quantity"), f"{field}.filled_quantity"),
        withdrawn_quantity=_integer(
            item.get("withdrawn_quantity"), f"{field}.withdrawn_quantity"
        ),
        entrust_price=_decimal(item.get("entrust_price"), f"{field}.entrust_price"),
        average_price=_decimal(item.get("average_price"), f"{field}.average_price"),
        created_at=_timestamp(item.get("created_at", ""), f"{field}.created_at", allow_empty=True),
        cancel_info=_text(item.get("cancel_info", ""), f"{field}.cancel_info", allow_empty=True),
    )


def _parse_trade(value: Any, field: str) -> BrokerTrade:
    item = _mapping(value, field)
    _exact_keys(
        item,
        {
            "trade_id",
            "order_id",
            "entrust_no",
            "symbol",
            "side",
            "quantity",
            "price",
            "business_balance",
            "real_type",
            "traded_at",
        },
        field,
    )
    symbol = _text(item.get("symbol"), f"{field}.symbol")
    if not _SYMBOL_PATTERN.fullmatch(symbol):
        raise SnapshotValidationError(f"{field}.symbol has an unsafe format")
    return BrokerTrade(
        broker_trade_id=_text(item.get("trade_id"), f"{field}.trade_id"),
        broker_order_id=_text(
            item.get("order_id", ""), f"{field}.order_id", allow_empty=True
        ),
        entrust_no=_text(item.get("entrust_no", ""), f"{field}.entrust_no", allow_empty=True),
        instrument_id=symbol,
        side=_side(item.get("side"), f"{field}.side"),
        quantity=_integer(item.get("quantity"), f"{field}.quantity"),
        price=_decimal(item.get("price"), f"{field}.price"),
        business_balance=_decimal(
            item.get("business_balance"), f"{field}.business_balance"
        ),
        real_type=_text(item.get("real_type", ""), f"{field}.real_type", allow_empty=True),
        traded_at=_timestamp(item.get("traded_at", ""), f"{field}.traded_at", allow_empty=True),
    )


class HtscMQuantShadowAdapter:
    """Read and validate one MQuant-generated snapshot.

    Passing ``expected_account_binding_id=None`` is useful only for a first,
    manually inspected enrollment snapshot.  Such a snapshot is explicitly
    marked unverified and cannot be reconciled into the strategy ledger.
    """

    def __init__(
        self,
        snapshot_path: Path | str,
        *,
        expected_account_binding_id: str | None,
        expected_account_fingerprint: str | None = None,
        expected_api_shape_id: str | None = None,
        max_age_seconds: int = 15,
        future_tolerance_seconds: int = 5,
    ) -> None:
        if type(max_age_seconds) is not int or max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be a positive integer")
        if type(future_tolerance_seconds) is not int or future_tolerance_seconds < 0:
            raise ValueError("future_tolerance_seconds must be a non-negative integer")
        if expected_account_binding_id is not None and not _BINDING_PATTERN.fullmatch(
            expected_account_binding_id
        ):
            raise ValueError("expected_account_binding_id has an unsafe format")
        if expected_account_fingerprint is not None and not _ACCOUNT_FINGERPRINT_PATTERN.fullmatch(
            expected_account_fingerprint
        ):
            raise ValueError("expected_account_fingerprint has an unsafe format")
        if expected_api_shape_id is not None and not _LOCAL_SHAPE_PATTERN.fullmatch(
            expected_api_shape_id
        ):
            raise ValueError("expected_api_shape_id must be a local static shape hash")
        self.snapshot_path = Path(snapshot_path)
        self.expected_account_binding_id = expected_account_binding_id
        self.expected_account_fingerprint = expected_account_fingerprint
        self.expected_api_shape_id = expected_api_shape_id
        self.max_age_seconds = max_age_seconds
        self.future_tolerance_seconds = future_tolerance_seconds

    def read_snapshot(self, now: datetime) -> RawBrokerSnapshot:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        try:
            size = self.snapshot_path.stat().st_size
        except FileNotFoundError as exc:
            raise SnapshotValidationError("MQuant snapshot file does not exist") from exc
        if size <= 0 or size > MAX_SNAPSHOT_BYTES:
            raise SnapshotValidationError("MQuant snapshot file size is unsafe")
        try:
            payload = json.loads(
                self.snapshot_path.read_text(encoding="utf-8"),
                object_pairs_hook=_object_without_duplicate_keys,
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SnapshotValidationError("MQuant snapshot is not valid UTF-8 JSON") from exc
        payload = _mapping(payload, "root")
        _exact_keys(payload, _ROOT_KEYS, "root")

        if payload.get("schema_version") != SCHEMA_VERSION:
            raise SnapshotValidationError("unsupported MQuant snapshot schema")
        capabilities = _mapping(payload.get("capabilities"), "capabilities")
        _exact_keys(capabilities, {"read_only", "orders_enabled"}, "capabilities")
        if capabilities.get("read_only") is not True:
            raise SnapshotValidationError("snapshot is not explicitly read-only")
        if capabilities.get("orders_enabled") is not False:
            raise SnapshotValidationError("snapshot unexpectedly enables orders")

        supplied_hash = _text(payload.get("payload_sha256"), "payload_sha256")
        if not _HASH_PATTERN.fullmatch(supplied_hash):
            raise SnapshotValidationError("payload_sha256 has an invalid format")
        if not hmac.compare_digest(supplied_hash, _canonical_hash(payload)):
            raise SnapshotValidationError("MQuant snapshot hash mismatch")

        source = _mapping(payload.get("source"), "source")
        _exact_keys(
            source,
            {
                "broker",
                "adapter",
                "environment",
                "api_shape_id",
                "account_binding_id",
                "account_fingerprint",
                "session_id",
            },
            "source",
        )
        if source.get("broker") != "HTSC" or source.get("adapter") != "mquant":
            raise SnapshotValidationError("unexpected broker source")
        if source.get("environment") != "real_account_read_only":
            raise SnapshotValidationError("unexpected MQuant environment")
        binding = _text(source.get("account_binding_id"), "source.account_binding_id")
        if not _BINDING_PATTERN.fullmatch(binding):
            raise SnapshotValidationError("account_binding_id has an unsafe format")
        account_fingerprint = _text(
            source.get("account_fingerprint"), "source.account_fingerprint"
        )
        if not _ACCOUNT_FINGERPRINT_PATTERN.fullmatch(account_fingerprint):
            raise SnapshotValidationError("account_fingerprint has an unsafe format")
        if self.expected_account_binding_id is not None and not hmac.compare_digest(
            binding, self.expected_account_binding_id
        ):
            raise SnapshotValidationError("account binding mismatch")
        if self.expected_account_fingerprint is not None and not hmac.compare_digest(
            account_fingerprint, self.expected_account_fingerprint
        ):
            raise SnapshotValidationError("account fingerprint mismatch")
        account_binding_matched = (
            self.expected_account_binding_id is not None
            and self.expected_account_fingerprint is not None
        )
        api_shape_id = _text(
            source.get("api_shape_id"), "source.api_shape_id"
        )
        if self.expected_api_shape_id is not None and not hmac.compare_digest(
            api_shape_id, self.expected_api_shape_id
        ):
            raise SnapshotValidationError("local API shape id mismatch")
        shape_checked = (
            self.expected_api_shape_id is not None
            and _LOCAL_SHAPE_PATTERN.fullmatch(api_shape_id) is not None
        )

        capture = _mapping(payload.get("capture"), "capture")
        _exact_keys(
            capture,
            {
                "sequence",
                "started_at",
                "completed_at",
                "complete",
                "consistency",
                "sections",
                "pagination",
                "errors",
                "warnings",
            },
            "capture",
        )
        if capture.get("complete") is not True:
            raise SnapshotValidationError("MQuant capture is incomplete")
        if capture.get("consistency") != "sequential_non_atomic":
            raise SnapshotValidationError("MQuant capture consistency is unsupported")
        sections = _mapping(capture.get("sections"), "capture.sections")
        _exact_keys(sections, set(_REQUIRED_SECTIONS), "capture.sections")
        for section in _REQUIRED_SECTIONS:
            if sections.get(section) is not True:
                raise SnapshotValidationError(f"MQuant section is incomplete: {section}")
        errors = _list(capture.get("errors"), "capture.errors")
        if errors:
            raise SnapshotValidationError("MQuant capture contains errors")
        warnings = tuple(
            _text(value, f"capture.warnings[{index}]")
            for index, value in enumerate(_list(capture.get("warnings", []), "capture.warnings"))
        )
        started_at = _timestamp(capture.get("started_at"), "capture.started_at")
        completed_at = _timestamp(capture.get("completed_at"), "capture.completed_at")
        assert started_at is not None and completed_at is not None
        if completed_at < started_at:
            raise SnapshotValidationError("capture completed before it started")
        if completed_at - started_at > timedelta(seconds=self.max_age_seconds):
            raise SnapshotValidationError("MQuant sequential capture window is too long")
        age = (now - completed_at).total_seconds()
        if age > self.max_age_seconds:
            raise SnapshotValidationError("MQuant snapshot is stale")
        if age < -self.future_tolerance_seconds:
            raise SnapshotValidationError("MQuant snapshot is from the future")

        funds_payload = _mapping(payload.get("funds"), "funds")
        _exact_keys(
            funds_payload,
            {
                "available_cash",
                "frozen_cash",
                "hold_cash",
                "total_value",
                "market_value",
                "transferable_cash",
            },
            "funds",
        )
        funds = BrokerFunds(
            available_cash=_decimal(funds_payload.get("available_cash"), "funds.available_cash"),
            frozen_cash=_decimal(funds_payload.get("frozen_cash"), "funds.frozen_cash"),
            hold_cash=_decimal(funds_payload.get("hold_cash"), "funds.hold_cash"),
            total_value=_decimal(funds_payload.get("total_value"), "funds.total_value"),
            market_value=_decimal(funds_payload.get("market_value"), "funds.market_value"),
            transferable_cash=_decimal(
                funds_payload.get("transferable_cash"), "funds.transferable_cash"
            ),
        )

        positions: dict[str, BrokerPosition] = {}
        for index, raw_position in enumerate(_list(payload.get("positions"), "positions")):
            field = f"positions[{index}]"
            item = _mapping(raw_position, field)
            _exact_keys(
                item,
                {
                    "symbol",
                    "total_quantity",
                    "sellable_quantity",
                    "today_quantity",
                    "frozen_quantity",
                    "price",
                    "market_value",
                    "hold_cost",
                },
                field,
            )
            symbol = _text(item.get("symbol"), f"{field}.symbol")
            if not _SYMBOL_PATTERN.fullmatch(symbol):
                raise SnapshotValidationError(f"{field}.symbol has an unsafe format")
            if symbol in positions:
                raise SnapshotValidationError(f"duplicate broker position: {symbol}")
            try:
                positions[symbol] = BrokerPosition(
                    instrument_id=symbol,
                    quantity=_integer(item.get("total_quantity"), f"{field}.total_quantity"),
                    sellable_quantity=_integer(
                        item.get("sellable_quantity"), f"{field}.sellable_quantity"
                    ),
                    today_quantity=_integer(
                        item.get("today_quantity"), f"{field}.today_quantity"
                    ),
                    frozen_quantity=_integer(
                        item.get("frozen_quantity"), f"{field}.frozen_quantity"
                    ),
                    price=_decimal(item.get("price"), f"{field}.price"),
                    market_value=_decimal(
                        item.get("market_value"), f"{field}.market_value"
                    ),
                    hold_cost=_decimal(item.get("hold_cost"), f"{field}.hold_cost"),
                )
            except ValueError as exc:
                raise SnapshotValidationError(str(exc)) from exc

        try:
            open_orders = tuple(
                _parse_order(value, f"open_orders[{index}]")
                for index, value in enumerate(_list(payload.get("open_orders"), "open_orders"))
            )
            today_orders = tuple(
                _parse_order(value, f"today_orders[{index}]")
                for index, value in enumerate(_list(payload.get("today_orders"), "today_orders"))
            )
            trades = tuple(
                _parse_trade(value, f"trades[{index}]")
                for index, value in enumerate(_list(payload.get("trades"), "trades"))
            )
        except ValueError as exc:
            if isinstance(exc, SnapshotValidationError):
                raise
            raise SnapshotValidationError(str(exc)) from exc
        _unique(open_orders, "broker_order_id", "open order id")
        _unique(today_orders, "broker_order_id", "today order id")
        _unique(trades, "broker_trade_id", "trade id")

        pagination = _mapping(capture.get("pagination"), "capture.pagination")
        _exact_keys(
            pagination,
            {"open_orders", "today_orders", "trades"},
            "capture.pagination",
        )
        section_lengths = {
            "open_orders": len(open_orders),
            "today_orders": len(today_orders),
            "trades": len(trades),
        }
        for section, returned_length in section_lengths.items():
            page = _mapping(pagination.get(section), f"capture.pagination.{section}")
            _exact_keys(
                page,
                {"page_count", "reported_total_count", "returned_count", "is_last"},
                f"capture.pagination.{section}",
            )
            _integer(
                page.get("page_count"),
                f"capture.pagination.{section}.page_count",
                minimum=1,
            )
            _integer(
                page.get("reported_total_count"),
                f"capture.pagination.{section}.reported_total_count",
            )
            returned_count = _integer(
                page.get("returned_count"),
                f"capture.pagination.{section}.returned_count",
            )
            if page.get("is_last") is not True:
                raise SnapshotValidationError(f"pagination did not finish: {section}")
            if returned_count != returned_length:
                raise SnapshotValidationError(f"pagination count mismatch: {section}")
            if page.get("reported_total_count") != returned_count:
                raise SnapshotValidationError(f"pagination reported count mismatch: {section}")

        try:
            return RawBrokerSnapshot(
                broker="HTSC",
                adapter="mquant",
                environment="real_account_read_only",
                api_shape_id=api_shape_id,
                shape_checked=shape_checked,
                account_binding_id=binding,
                account_fingerprint=account_fingerprint,
                account_binding_matched=account_binding_matched,
                source_authenticated=False,
                session_id=_text(source.get("session_id"), "source.session_id"),
                sequence=_integer(capture.get("sequence"), "capture.sequence", minimum=1),
                started_at=started_at,
                completed_at=completed_at,
                capture_consistency="sequential_non_atomic",
                funds=funds,
                positions=positions,
                open_orders=open_orders,
                today_orders=today_orders,
                trades=trades,
                warnings=warnings,
                payload_sha256=supplied_hash,
            )
        except ValueError as exc:
            raise SnapshotValidationError(str(exc)) from exc
