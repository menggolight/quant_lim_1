# -*- coding: utf-8 -*-
"""HTSC MQuant read-only shadow snapshot exporter.

This file is intentionally self-contained and compatible with Python 3.6.
MQuant injects the query and scheduling functions into the strategy runtime.
The exporter never submits a trading instruction; it only queries account state
and atomically replaces one local JSON snapshot.
"""

from __future__ import print_function

import datetime
import decimal
import hashlib
import hmac
import json
import os
import re
import uuid


SCHEMA_VERSION = "htsc-mquant-shadow/1"
API_SHAPE_ID = "public-manual-v3.1-2021-01-27-unchecked-current"

# Prefer MQuant run_params. These constants are a local fallback for clients
# whose strategy launcher cannot pass custom parameters. Never put an account
# number or shareholder number in ACCOUNT_BINDING_ID.
SNAPSHOT_PATH = ""
ACCOUNT_BINDING_ID = ""
ACCOUNT_BINDING_SECRET = ""
ACCOUNT_TYPE = "stock"
INTERVAL_SECONDS = 5
PAGE_SIZE = 500
MAX_PAGES = 10000

_BINDING_ID_PATTERN = re.compile(r"^htsc-local-[0-9a-f]{32}$")
_LONG_DIGIT_TOKEN = re.compile(r"\d{8,}")
_BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8))

_STATE = {
    "configured": False,
    "snapshot_path": "",
    "account_binding_id": "",
    "account_binding_secret": "",
    "account_type": ACCOUNT_TYPE,
    "interval_seconds": INTERVAL_SECONDS,
    "page_size": PAGE_SIZE,
    "session_id": "",
    "sequence": 0,
}


class _SectionError(Exception):
    """Internal marker for a failed or inconsistent snapshot section."""


def _run_param(context, name, fallback):
    params = getattr(context, "run_params", None)
    if isinstance(params, dict) and name in params:
        value = params.get(name)
        if value is not None and value != "":
            return value
    return fallback


def _require_configuration(context):
    snapshot_path = str(_run_param(context, "snapshot_path", SNAPSHOT_PATH)).strip()
    binding_id = str(
        _run_param(context, "account_binding_id", ACCOUNT_BINDING_ID)
    ).strip().lower()
    binding_secret = str(
        _run_param(context, "account_binding_secret", ACCOUNT_BINDING_SECRET)
    )
    account_type = str(_run_param(context, "account_type", ACCOUNT_TYPE)).strip()

    try:
        interval_seconds = int(
            _run_param(context, "interval_seconds", INTERVAL_SECONDS)
        )
        page_size = int(_run_param(context, "page_size", PAGE_SIZE))
    except (TypeError, ValueError):
        raise ValueError("interval_seconds and page_size must be integers")

    if not snapshot_path:
        raise ValueError("snapshot_path is required")
    if not os.path.isabs(snapshot_path):
        raise ValueError("snapshot_path must be an absolute local path")
    if not snapshot_path.lower().endswith(".json"):
        raise ValueError("snapshot_path must end with .json")
    if not _BINDING_ID_PATTERN.match(binding_id):
        raise ValueError(
            "account_binding_id must be htsc-local- followed by 32 random hex chars"
        )
    if len(binding_secret) < 32:
        raise ValueError("account_binding_secret must contain at least 32 characters")
    if not account_type:
        raise ValueError("account_type is required")
    if interval_seconds < 2:
        raise ValueError("interval_seconds must be at least 2")
    if page_size < 1 or page_size > 1000:
        raise ValueError("page_size must be between 1 and 1000")

    # MQuant's public V3.1 manual restricts file reads after initialize. Perform
    # the only directory existence check/creation during configuration; timed
    # callbacks only write a temporary file and replace the destination.
    output_directory = os.path.dirname(os.path.abspath(snapshot_path))
    if not os.path.isdir(output_directory):
        os.makedirs(output_directory)

    _STATE.update(
        {
            "configured": True,
            "snapshot_path": snapshot_path,
            "account_binding_id": binding_id,
            "account_binding_secret": binding_secret,
            "account_type": account_type,
            "interval_seconds": interval_seconds,
            "page_size": page_size,
            "session_id": uuid.uuid4().hex,
            "sequence": 0,
        }
    )


def _now_iso():
    return datetime.datetime.now(_BEIJING_TZ).isoformat()


def _number_text(value):
    if value is None:
        raise _SectionError("numeric value is missing")
    try:
        parsed = decimal.Decimal(str(value))
    except (decimal.InvalidOperation, TypeError, ValueError):
        raise _SectionError("numeric value is invalid")
    if not parsed.is_finite() or parsed < 0:
        raise _SectionError("numeric value must be finite and non-negative")
    if parsed == 0:
        return "0"
    return format(parsed, "f")


def _integer_value(value):
    if value is None or isinstance(value, bool):
        raise _SectionError("integer value is missing or invalid")
    try:
        parsed = decimal.Decimal(str(value))
    except (decimal.InvalidOperation, TypeError, ValueError):
        raise _SectionError("integer value is invalid")
    if not parsed.is_finite() or parsed != parsed.to_integral_value():
        raise _SectionError("integer value is not a finite integer")
    if parsed < 0:
        raise _SectionError("integer value is negative")
    return int(parsed)


def _text(value):
    if value is None:
        return ""
    return str(value)


def _redacted_text(value):
    return _LONG_DIGIT_TOKEN.sub("[redacted]", _text(value))


def _enum_text(value):
    enum_value = getattr(value, "value", value)
    return _text(enum_value)


def _side_text(value):
    raw = _enum_text(value).strip().lower()
    if raw in ("long", "buy"):
        return "BUY"
    if raw in ("short", "sell"):
        return "SELL"
    if raw in ("", "unknown"):
        return "UNKNOWN"
    raise _SectionError("unknown order side")


def _time_text(value):
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_BEIJING_TZ)
        return value.isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    return _text(value)


def _required_attr(obj, name):
    if obj is None or not hasattr(obj, name):
        raise _SectionError("required field is missing")
    return getattr(obj, name)


def _first_attr(obj, names):
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None and value != "":
                return value
    raise _SectionError("required field is missing")


def _funds_record(fund_info):
    if fund_info is None:
        raise _SectionError("fund query returned no data")
    return {
        "available_cash": _number_text(_required_attr(fund_info, "available_cash")),
        "frozen_cash": _number_text(_required_attr(fund_info, "frozen_cash")),
        "hold_cash": _number_text(_required_attr(fund_info, "hold_cash")),
        "total_value": _number_text(_required_attr(fund_info, "total_value")),
        "market_value": _number_text(_required_attr(fund_info, "market_value")),
        "transferable_cash": _number_text(
            _required_attr(fund_info, "transferable_cash")
        ),
    }


def _account_fingerprint(fund_info):
    fund_account = _text(_required_attr(fund_info, "fund_account")).strip()
    if not fund_account:
        raise _SectionError("fund account is unavailable for account binding")
    digest = hmac.new(
        _STATE["account_binding_secret"].encode("utf-8"),
        fund_account.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return "hmac-sha256:" + digest


def _position_record(position):
    symbol = _text(_required_attr(position, "security")).strip()
    if not symbol:
        raise _SectionError("position symbol is empty")
    return {
        "symbol": symbol,
        "total_quantity": _integer_value(_required_attr(position, "total_amount")),
        "sellable_quantity": _integer_value(
            _required_attr(position, "closeable_amount")
        ),
        "today_quantity": _integer_value(_required_attr(position, "today_amount")),
        "frozen_quantity": _integer_value(_required_attr(position, "locked_amount")),
        "price": _number_text(_required_attr(position, "price")),
        "market_value": _number_text(_required_attr(position, "value")),
        "hold_cost": _number_text(_required_attr(position, "hold_cost")),
    }


def _order_record(item):
    order_id = _text(_required_attr(item, "order_id")).strip()
    symbol = _text(_first_attr(item, ("symbol", "security"))).strip()
    if not order_id or not symbol:
        raise _SectionError("order identity is empty")
    return {
        "order_id": order_id,
        "entrust_no": _text(_required_attr(item, "entrust_no")),
        "symbol": symbol,
        "side": _side_text(_required_attr(item, "side")),
        "status": _enum_text(_required_attr(item, "status")),
        "quantity": _integer_value(_required_attr(item, "amount")),
        "filled_quantity": _integer_value(_required_attr(item, "filled")),
        "withdrawn_quantity": _integer_value(
            _required_attr(item, "withdraw_amount")
        ),
        "entrust_price": _number_text(_required_attr(item, "entrust_price")),
        "average_price": _number_text(_required_attr(item, "price")),
        "created_at": _time_text(_first_attr(item, ("add_time", "create_time"))),
        "cancel_info": _redacted_text(_required_attr(item, "cancel_info")),
    }


def _trade_record(item):
    trade_id = _text(_required_attr(item, "trade_id")).strip()
    symbol = _text(_first_attr(item, ("symbol", "security"))).strip()
    if not trade_id or not symbol:
        raise _SectionError("trade identity is empty")
    return {
        "trade_id": trade_id,
        "order_id": _text(_required_attr(item, "order_id")),
        "entrust_no": _text(_required_attr(item, "entrust_no")),
        "symbol": symbol,
        "side": _side_text(_required_attr(item, "side")),
        "quantity": _integer_value(_required_attr(item, "amount")),
        "price": _number_text(_required_attr(item, "price")),
        "business_balance": _number_text(
            _required_attr(item, "business_balance")
        ),
        "real_type": _enum_text(_required_attr(item, "real_type")),
        "traded_at": _time_text(_required_attr(item, "time")),
    }


def _collection_values(collection):
    if isinstance(collection, dict):
        return list(collection.values())
    if isinstance(collection, (list, tuple)):
        return list(collection)
    raise _SectionError("query payload is not a collection")


def _read_paginated(section_name, page_query, serializer, identity_field):
    page_no = 1
    page_count = 0
    first_total_count = None
    records = []
    seen = set()

    while True:
        if page_no > MAX_PAGES:
            raise _SectionError("pagination limit exceeded")

        result = page_query(page_no, _STATE["page_size"])
        if not isinstance(result, (list, tuple)) or len(result) != 3:
            raise _SectionError("extended query returned an unexpected value")

        total_count, is_last, payload = result
        try:
            total_count = int(total_count)
        except (TypeError, ValueError):
            raise _SectionError("total_count is invalid")
        if total_count < 0:
            raise _SectionError("total_count is negative")
        if is_last not in (True, False, 0, 1):
            raise _SectionError("is_last is invalid")
        is_last = bool(is_last)

        if first_total_count is None:
            first_total_count = total_count
        elif total_count != first_total_count:
            raise _SectionError("total_count changed during pagination")

        for item in _collection_values(payload):
            record = serializer(item)
            identity = _text(record.get(identity_field)).strip()
            if not identity or identity in seen:
                raise _SectionError("duplicate or empty identity across pages")
            seen.add(identity)
            records.append(record)

        page_count += 1
        if is_last:
            break
        page_no += 1

    if first_total_count != len(records):
        # All three callers use account-wide, unfiltered queries. A mismatch is
        # therefore an incomplete or contract-drifted capture. Current SDK
        # semantics must be re-verified before relaxing this check.
        raise _SectionError("reported and returned counts differ")

    records.sort(key=lambda record: _text(record.get(identity_field)))
    pagination = {
        "page_count": page_count,
        "reported_total_count": first_total_count,
        "returned_count": len(records),
        "is_last": True,
    }
    return records, pagination


def _query_funds():
    fund_info = get_fund_info(account_type=_STATE["account_type"])
    return _funds_record(fund_info), _account_fingerprint(fund_info)


def _query_positions():
    result = get_positions_ex(account_type=_STATE["account_type"], symbol="")
    if result is None:
        raise _SectionError("position query returned no data")
    records = [_position_record(item) for item in _collection_values(result)]
    identities = [record["symbol"] for record in records]
    if len(identities) != len(set(identities)):
        raise _SectionError("duplicate position symbol")
    records.sort(key=lambda record: record["symbol"])
    return records


def _query_open_orders():
    def page_query(page_no, page_size):
        return get_open_orders_ex(
            page_no=page_no,
            page_size=page_size,
            only_this_inst=False,
            account_type=_STATE["account_type"],
        )

    return _read_paginated("open_orders", page_query, _order_record, "order_id")


def _query_today_orders():
    def page_query(page_no, page_size):
        return get_orders_ex(
            order_id="",
            security="",
            status=None,
            page_no=page_no,
            page_size=page_size,
            only_this_inst=False,
            account_type=_STATE["account_type"],
        )

    return _read_paginated("today_orders", page_query, _order_record, "order_id")


def _query_trades():
    def page_query(page_no, page_size):
        return get_trades_ex(
            order_id="",
            security="",
            page_no=page_no,
            page_size=page_size,
            account_type=_STATE["account_type"],
            include_rejected_orders=True,
            include_withdraw_orders=True,
            only_this_inst=False,
        )

    return _read_paginated("trades", page_query, _trade_record, "trade_id")


def _empty_funds():
    return {
        "available_cash": "0",
        "frozen_cash": "0",
        "hold_cash": "0",
        "total_value": "0",
        "market_value": "0",
        "transferable_cash": "0",
    }


def _error(section, code):
    # Deliberately omit exception messages: broker errors may contain account
    # identifiers. The MQuant client remains the place to inspect full logs.
    return {"section": section, "code": code}


def _snapshot_payload():
    _STATE["sequence"] += 1
    started_at = _now_iso()
    sections = {
        "funds": False,
        "positions": False,
        "open_orders": False,
        "today_orders": False,
        "trades": False,
    }
    errors = []
    pagination = {}
    funds = _empty_funds()
    account_fingerprint = ""
    positions = []
    open_orders = []
    today_orders = []
    trades = []

    try:
        funds, account_fingerprint = _query_funds()
        sections["funds"] = True
    except Exception:
        errors.append(_error("funds", "query_or_mapping_failed"))

    try:
        positions = _query_positions()
        sections["positions"] = True
    except Exception:
        errors.append(_error("positions", "query_or_mapping_failed"))

    try:
        open_orders, pagination["open_orders"] = _query_open_orders()
        sections["open_orders"] = True
    except Exception:
        errors.append(_error("open_orders", "query_mapping_or_pagination_failed"))

    try:
        today_orders, pagination["today_orders"] = _query_today_orders()
        sections["today_orders"] = True
    except Exception:
        errors.append(_error("today_orders", "query_mapping_or_pagination_failed"))

    try:
        trades, pagination["trades"] = _query_trades()
        sections["trades"] = True
    except Exception:
        errors.append(_error("trades", "query_mapping_or_pagination_failed"))

    complete = all(sections.values())
    return {
        "schema_version": SCHEMA_VERSION,
        "capabilities": {"read_only": True, "orders_enabled": False},
        "source": {
            "broker": "HTSC",
            "adapter": "mquant",
            "environment": "real_account_read_only",
            "api_shape_id": API_SHAPE_ID,
            "account_binding_id": _STATE["account_binding_id"],
            "account_fingerprint": account_fingerprint,
            "session_id": _STATE["session_id"],
        },
        "capture": {
            "sequence": _STATE["sequence"],
            "started_at": started_at,
            "completed_at": _now_iso(),
            "complete": complete,
            "consistency": "sequential_non_atomic",
            "sections": sections,
            "pagination": pagination,
            "errors": errors,
            "warnings": [
                "local_api_shape_only_not_official_source_or_runtime_attestation",
                "sequential_queries_not_atomic_broker_snapshot",
            ],
        },
        "funds": funds,
        "positions": positions,
        "open_orders": open_orders,
        "today_orders": today_orders,
        "trades": trades,
    }


def _with_payload_hash(payload):
    unsigned = dict(payload)
    unsigned.pop("payload_sha256", None)
    canonical = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    result = dict(unsigned)
    result["payload_sha256"] = "sha256:" + hashlib.sha256(canonical).hexdigest()
    return result


def _atomic_write_json(path, payload):
    temp_path = "{}.tmp.{}.{}".format(
        path, _STATE["session_id"], _STATE["sequence"]
    )
    try:
        with open(temp_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass


def capture_once(context):
    """Query every read-only section and atomically publish one snapshot."""
    if not _STATE["configured"]:
        _require_configuration(context)
    payload = _with_payload_hash(_snapshot_payload())
    _atomic_write_json(_STATE["snapshot_path"], payload)
    return payload


def _scheduled_capture(context, *args, **kwargs):
    capture_once(context)


def initialize(context):
    """MQuant strategy entry point."""
    _require_configuration(context)
    capture_once(context)
    run_timely(_scheduled_capture, _STATE["interval_seconds"])
