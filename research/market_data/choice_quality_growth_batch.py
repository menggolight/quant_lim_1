"""Fixed Choice quality-growth historical batch capture.

This module deliberately records a bounded historical evidence surface.  It
does not certify original point-in-time availability, official truth, Paper
eligibility, trading eligibility, or LIVE capability.  Those denials are part
of every manifest and cannot be overridden by a caller.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .contracts import MarketDataRequest, canonical_json_bytes, sha256_bytes
from .providers.base import ProviderPayload, safe_error_text
from .providers.choice import ChoiceProvider


SCHEMA_VERSION = "choice-quality-growth-batch.v1"
PLAN_SCHEMA_VERSION = "choice-quality-growth-plan.v1"
CHECKPOINT_SCHEMA_VERSION = "choice-quality-growth-checkpoint.v1"
CHECKPOINT_EVENT_SCHEMA_VERSION = "choice-quality-growth-checkpoint-event.v1"
FAILURE_EVENT_SCHEMA_VERSION = "choice-quality-growth-failure-event.v1"
ARTIFACT_SCHEMA_VERSION = "choice-quality-growth-artifact.v1"
ADAPTER_VERSION = "choice-quality-growth-batch-adapter.v1"
PROVIDER_ID = "choice"
SECTOR_CODE = "009006039"
EXPECTED_MEMBERS_PER_DECISION = 800
PRICE_START_DATE = date(2017, 1, 1)
TRAIN_START_DATE = date(2018, 1, 1)
REBALANCE_ANCHOR_DATE = date(2018, 1, 2)
REBALANCE_SESSIONS = 20
OUTCOME_SESSIONS = 20
PRICE_BASES = ("qfq", "none")
CSD_FIELDS = (
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
CSS_FIELDS = (
    "LIMITUPPRICE",
    "LIMITDOWNPRICE",
    "TRADESTATUS",
    "ISSTSTOCK",
    "LISTDATE",
)
CSS_STATE_FIELDS = CSS_FIELDS[:-1]
CSS_LIST_DATE_FIELDS = ("LISTDATE",)
CSS_BATCH_SIZE = 50
INDUSTRY_BLOCKER = "blocked_missing_controlled_industry_adapter"
PIT_BLOCKER = "blocked_historical_backfill_not_original_pit"
CALENDAR_BLOCKER = "blocked_choice_calendar_not_reconciled_to_exchange_truth"
LIVE_STATUS = "live_not_supported"
CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
RAW_SEMANTICS = "canonicalized_sdk_response_evidence_not_wire_bytes"
INTEGRITY_SEMANTICS = "content_integrity_not_source_authentication"
CHECKPOINT_COMPACTION_INTERVAL = 100

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ERROR_CODE = re.compile(r"^[a-z0-9_]{1,64}$")
_SAFE_ERROR_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_INSTRUMENT = re.compile(r"^[0-9]{6}\.(?:SH|SZ)$")
_TRUE = {"1", "true", "yes", "y", "是"}
_FALSE = {"0", "false", "no", "n", "否"}
_TRADING_STATUSES = {
    "1",
    "trade",
    "trading",
    "交易",
    "交易中",
    "正常",
    "正常交易",
}
_SUSPENDED_STATUSES = {
    "-1",
    "0",
    "suspend",
    "suspended",
    "停牌",
    "停牌一天",
    "全天停牌",
    "临时停牌",
    "盘中停牌",
    "暂停交易",
}
_DECIMAL_MAXIMUMS = {
    "open": Decimal("100000000"),
    "high": Decimal("100000000"),
    "low": Decimal("100000000"),
    "close": Decimal("100000000"),
    "preclose": Decimal("100000000"),
    "limitupprice": Decimal("100000000"),
    "limitdownprice": Decimal("100000000"),
    "limit_up_price": Decimal("100000000"),
    "limit_down_price": Decimal("100000000"),
    "volume": Decimal("10000000000000000"),
    "amount": Decimal("100000000000000000000"),
}


class ChoiceQualityGrowthBatchError(ValueError):
    """The fixed batch contract or its stored evidence is invalid."""


@dataclass(frozen=True)
class ChoiceQualityGrowthBatchRun:
    manifest_path: Path
    manifest_sha256: str
    status: str
    collection_status: str
    blocking_reasons: tuple[str, ...]


@dataclass(frozen=True)
class ChoiceQualityGrowthBatchVerification:
    manifest_path: Path
    integrity_verified: bool
    status: str
    collection_status: str
    reasons: tuple[str, ...]
    formal_truth_eligible: bool = False
    paper_eligible: bool = False
    trade_eligible: bool = False
    real_money_candidate: bool = False
    live_execution_status: str = LIVE_STATUS


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ChoiceQualityGrowthBatchError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ChoiceQualityGrowthBatchError(
                    f"non-finite JSON constant is forbidden: {item}"
                )
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ChoiceQualityGrowthBatchError(
            f"cannot read strict JSON {path.name}: {type(exc).__name__}"
        ) from exc
    if not isinstance(value, dict):
        raise ChoiceQualityGrowthBatchError(f"{path.name} must contain one object")
    return value


def _atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_bytes(raw)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _absolute_lexical(path: Path) -> Path:
    return Path(os.path.abspath(str(path)))


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(
        int(getattr(metadata, "st_file_attributes", 0)) & 0x400
    )


def _guard_batch_path(root: Path, target: Path) -> tuple[Path, Path]:
    """Lexically contain a path and reject links/reparse points before I/O."""

    absolute_root = _absolute_lexical(root)
    absolute_target = _absolute_lexical(target)
    try:
        relative = absolute_target.relative_to(absolute_root)
    except ValueError as exc:
        raise ChoiceQualityGrowthBatchError(
            "batch path escapes the output root"
        ) from exc
    for ancestor in (*reversed(absolute_root.parents), absolute_root):
        if _is_link_or_reparse(ancestor):
            raise ChoiceQualityGrowthBatchError(
                "batch output ancestry contains a link or reparse point"
            )
    current = absolute_root
    for part in relative.parts:
        current = current / part
        if _is_link_or_reparse(current):
            raise ChoiceQualityGrowthBatchError(
                "batch path contains a link or reparse point"
            )
    return absolute_root, absolute_target


@contextmanager
def _exclusive_batch_lock(root: Path):
    """Reject concurrent writers instead of merging two mutable checkpoints."""

    resolved = _absolute_lexical(root)
    _guard_batch_path(resolved, resolved)
    resolved.mkdir(parents=True, exist_ok=True)
    _guard_batch_path(resolved, resolved)
    lock_path = resolved / ".choice_quality_growth_batch.lock"
    try:
        descriptor = os.open(
            lock_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        )
    except FileExistsError as exc:
        raise ChoiceQualityGrowthBatchError(
            "another fixed Choice batch writer already owns this output root"
        ) from exc
    try:
        os.write(descriptor, b"choice-quality-growth-batch-writer-v1\n")
        yield
    finally:
        os.close(descriptor)
        lock_path.unlink(missing_ok=True)


def _write_immutable(path: Path, raw: bytes, *, root: Path | None = None) -> None:
    if root is not None:
        _guard_batch_path(root, path)
    if path.exists():
        if path.read_bytes() != raw:
            raise ChoiceQualityGrowthBatchError(
                f"immutable artifact collision at {path.name}"
            )
        return
    _atomic_write(path, raw)


def _relative(root: Path, path: Path) -> str:
    absolute_root, absolute_path = _guard_batch_path(root, path)
    return absolute_path.relative_to(absolute_root).as_posix()


def _resolve_ref(root: Path, value: Any) -> Path:
    text = str(value)
    candidate = Path(text)
    if not text or candidate.is_absolute() or ".." in candidate.parts:
        raise ChoiceQualityGrowthBatchError("artifact reference must be relative")
    _, resolved = _guard_batch_path(root, root / candidate)
    return resolved


def _aware(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ChoiceQualityGrowthBatchError(f"{field_name} must include a timezone")
    return value


def _validate_failure_payload(task_id: str, failure: Mapping[str, Any]) -> None:
    if (
        _SHA256.fullmatch(task_id) is None
        or set(failure)
        != {"task", "error_type", "error_code", "error_message"}
        or not isinstance(failure.get("task"), Mapping)
        or _task_id(failure["task"]) != task_id
        or _SAFE_ERROR_TYPE.fullmatch(str(failure.get("error_type", ""))) is None
        or _SAFE_ERROR_CODE.fullmatch(str(failure.get("error_code", ""))) is None
        or not isinstance(failure.get("error_message"), str)
        or len(failure["error_message"]) > 1000
    ):
        raise ChoiceQualityGrowthBatchError("stored failure payload drifted")


def fixed_choice_quality_growth_plan(cutoff_date: date) -> Mapping[str, Any]:
    """Return the caller-invariant v1 plan for one frozen cutoff."""

    if not isinstance(cutoff_date, date) or isinstance(cutoff_date, datetime):
        raise ChoiceQualityGrowthBatchError("cutoff_date must be a date")
    if cutoff_date < REBALANCE_ANCHOR_DATE:
        raise ChoiceQualityGrowthBatchError("cutoff_date precedes the fixed anchor")
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "adapter_version": ADAPTER_VERSION,
        "provider_id": PROVIDER_ID,
        "sector_code": SECTOR_CODE,
        "expected_members_per_decision": EXPECTED_MEMBERS_PER_DECISION,
        "price_start_date": PRICE_START_DATE.isoformat(),
        "train_start_date": TRAIN_START_DATE.isoformat(),
        "rebalance_anchor_date": REBALANCE_ANCHOR_DATE.isoformat(),
        "rebalance_sessions": REBALANCE_SESSIONS,
        "outcome_sessions": OUTCOME_SESSIONS,
        "cutoff_date": cutoff_date.isoformat(),
        "price_bases": list(PRICE_BASES),
        "csd_fields": list(CSD_FIELDS),
        "css_state_fields": list(CSS_STATE_FIELDS),
        "css_list_date_fields": list(CSS_LIST_DATE_FIELDS),
        "css_batch_size": CSS_BATCH_SIZE,
        "css_historical_date_parameter": "EndDate",
        "css_historical_response_date_required": True,
        "highlimit_lowlimit_semantics": "touch_flags_not_prices",
        "limit_price_source": "CSS_LIMITUPPRICE_LIMITDOWNPRICE",
        "eligibility_snapshot_date": "next_trading_session_execution",
        "industry_contract_status": INDUSTRY_BLOCKER,
        "calendar_truth_status": CALENDAR_BLOCKER,
        "source_authenticated": False,
        "raw_semantics": RAW_SEMANTICS,
        "integrity_semantics": INTEGRITY_SEMANTICS,
        "retrieval_mode": "historical_backfill",
    }


def choice_quality_growth_plan_sha256(cutoff_date: date) -> str:
    return sha256_bytes(canonical_json_bytes(fixed_choice_quality_growth_plan(cutoff_date)))


def derive_choice_quality_growth_decision_grid(
    trading_sessions: Sequence[date], cutoff_date: date
) -> tuple[date, ...]:
    """Derive the sole allowed 20-session grid, including only observable labels."""

    sessions = tuple(trading_sessions)
    if not sessions or sessions != tuple(sorted(set(sessions))):
        raise ChoiceQualityGrowthBatchError(
            "trading sessions must be non-empty, unique, and ascending"
        )
    try:
        canonical_anchor = next(day for day in sessions if day >= TRAIN_START_DATE)
    except StopIteration as exc:
        raise ChoiceQualityGrowthBatchError("calendar does not cover train start") from exc
    if canonical_anchor != REBALANCE_ANCHOR_DATE:
        raise ChoiceQualityGrowthBatchError(
            "calendar does not prove the fixed 2018-01-02 rebalance anchor"
        )
    index = {day: position for position, day in enumerate(sessions)}
    try:
        anchor_index = index[REBALANCE_ANCHOR_DATE]
        cutoff_index = index[cutoff_date]
    except KeyError as exc:
        raise ChoiceQualityGrowthBatchError(
            "anchor and cutoff must both be controlled trading sessions"
        ) from exc
    return tuple(
        sessions[position]
        for position in range(
            anchor_index, cutoff_index + 1, REBALANCE_SESSIONS
        )
        if position + 1 + OUTCOME_SESSIONS <= cutoff_index
    )


def _task(kind: str, **fields: Any) -> dict[str, Any]:
    return {
        "task_contract": "choice-quality-growth-fixed-task.v1",
        "kind": kind,
        **fields,
    }


def _task_id(task: Mapping[str, Any]) -> str:
    return sha256_bytes(canonical_json_bytes(dict(task)))


def _instrument_batches(
    instrument_ids: Sequence[str], batch_size: int = CSS_BATCH_SIZE
) -> tuple[tuple[str, ...], ...]:
    values = tuple(instrument_ids)
    if values != tuple(sorted(set(values))):
        raise ChoiceQualityGrowthBatchError(
            "instrument batch source must be ascending and unique"
        )
    return tuple(
        values[index : index + batch_size]
        for index in range(0, len(values), batch_size)
    )


def _validated_task_instruments(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ChoiceQualityGrowthBatchError(
            "stored CSS batch instruments must be an array"
        )
    instruments = tuple(str(item) for item in value)
    if (
        not 1 <= len(instruments) <= CSS_BATCH_SIZE
        or instruments != tuple(sorted(set(instruments)))
        or any(_INSTRUMENT.fullmatch(item) is None for item in instruments)
    ):
        raise ChoiceQualityGrowthBatchError(
            "stored CSS batch instruments drifted"
        )
    return instruments


def _validate_task_contract(task: Mapping[str, Any]) -> None:
    common = {"task_contract", "kind"}
    if task.get("task_contract") != "choice-quality-growth-fixed-task.v1":
        raise ChoiceQualityGrowthBatchError("stored task contract version drifted")
    kind = task.get("kind")
    expected = {
        "calendar": common | {"start_date", "end_date"},
        "membership": common | {"membership_date"},
        "csd": common
        | {"instrument_id", "start_date", "end_date", "price_basis"},
        "css_state_batch": common
        | {"decision_date", "instrument_ids", "trading_date"},
        "css_list_date_batch": common | {"instrument_ids"},
    }.get(str(kind))
    if expected is None or set(task) != expected:
        raise ChoiceQualityGrowthBatchError("stored task has an unapproved shape")
    if kind == "calendar":
        if task["start_date"] != PRICE_START_DATE.isoformat():
            raise ChoiceQualityGrowthBatchError("calendar task start date drifted")
        _iso_date(task["end_date"], "calendar.end_date")
    elif kind == "membership":
        _iso_date(task["membership_date"], "membership_date")
    elif kind == "csd":
        if (
            _INSTRUMENT.fullmatch(str(task["instrument_id"])) is None
            or task["start_date"] != PRICE_START_DATE.isoformat()
            or task["price_basis"] not in PRICE_BASES
        ):
            raise ChoiceQualityGrowthBatchError("CSD fixed task identity drifted")
        _iso_date(task["end_date"], "csd.end_date")
    elif kind == "css_state_batch":
        _validated_task_instruments(task["instrument_ids"])
        decision = _iso_date(task["decision_date"], "css.decision_date")
        execution = _iso_date(task["trading_date"], "css.trading_date")
        if execution <= decision:
            raise ChoiceQualityGrowthBatchError(
                "CSS eligibility date must follow the close-signal date"
            )
    else:
        _validated_task_instruments(task["instrument_ids"])


def _decimal_text(value: Any, field_name: str, *, positive: bool = False) -> str:
    text = str(value).strip()
    if not text:
        raise ChoiceQualityGrowthBatchError(f"{field_name} is missing")
    if len(text) > 64:
        raise ChoiceQualityGrowthBatchError(f"{field_name} is too wide")
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise ChoiceQualityGrowthBatchError(f"{field_name} is not decimal") from exc
    digits = parsed.as_tuple().digits
    exponent = parsed.as_tuple().exponent
    if (
        not parsed.is_finite()
        or len(digits) > 28
        or exponent < -12
        or exponent > 18
        or (positive and parsed <= 0)
        or (not positive and parsed < 0)
        or parsed > _DECIMAL_MAXIMUMS.get(field_name, Decimal("1e20"))
    ):
        raise ChoiceQualityGrowthBatchError(f"{field_name} is outside its domain")
    rendered = format(parsed, "f")
    if len(rendered) > 64:
        raise ChoiceQualityGrowthBatchError(f"{field_name} normalization is too wide")
    return rendered


def _validate_ohlc(open_value: str, high_value: str, low_value: str, close_value: str) -> None:
    opened = Decimal(open_value)
    high = Decimal(high_value)
    low = Decimal(low_value)
    closed = Decimal(close_value)
    if high < max(opened, low, closed) or low > min(opened, high, closed):
        raise ChoiceQualityGrowthBatchError("OHLC ordering is invalid")


def _validate_limit_prices(limit_up: str, limit_down: str) -> None:
    if Decimal(limit_up) <= Decimal(limit_down):
        raise ChoiceQualityGrowthBatchError(
            "limit-up price must exceed limit-down price"
        )


def _validate_price_basis_pair(
    instrument_id: str,
    qfq_records: Sequence[Mapping[str, Any]],
    none_records: Sequence[Mapping[str, Any]],
    required_session_dates: Sequence[str],
    controlled_session_dates: Sequence[str] | None = None,
) -> None:
    """Require complete same-date observations and one OHLC adjustment ratio."""

    required = tuple(required_session_dates)
    controlled = tuple(controlled_session_dates or required)
    qfq_dates = tuple(str(row.get("trading_date", "")) for row in qfq_records)
    none_dates = tuple(str(row.get("trading_date", "")) for row in none_records)
    if (
        qfq_dates != none_dates
        or not set(required).issubset(qfq_dates)
        or not set(qfq_dates).issubset(controlled)
    ):
        raise ChoiceQualityGrowthBatchError(
            f"CSD controlled-session coverage drifted for {instrument_id}"
        )
    if qfq_dates:
        first = controlled.index(qfq_dates[0])
        last = controlled.index(qfq_dates[-1])
        if qfq_dates != controlled[first : last + 1]:
            raise ChoiceQualityGrowthBatchError(
                f"CSD contains an internal controlled-session gap for {instrument_id}"
            )
    comparable = (
        "trading_status",
        "is_st",
        "high_limit_hit",
        "low_limit_hit",
    )
    for qfq, unadjusted in zip(qfq_records, none_records):
        if any(qfq.get(name) != unadjusted.get(name) for name in comparable):
            raise ChoiceQualityGrowthBatchError(
                f"CSD basis-invariant field drifted for {instrument_id}"
            )
        if any(
            Decimal(str(qfq[name])) != Decimal(str(unadjusted[name]))
            for name in ("volume", "amount")
        ):
            raise ChoiceQualityGrowthBatchError(
                f"CSD basis-invariant volume/amount drifted for {instrument_id}"
            )
        ratios: list[Decimal] = []
        for name in ("open", "high", "low", "close", "preclose"):
            denominator = Decimal(str(unadjusted[name]))
            numerator = Decimal(str(qfq[name]))
            if denominator <= 0 or numerator <= 0:
                raise ChoiceQualityGrowthBatchError(
                    f"CSD basis price is non-positive for {instrument_id}"
                )
            ratios.append(numerator / denominator)
        anchor = ratios[0]
        tolerance = max(Decimal("0.000001"), abs(anchor) * Decimal("0.000001"))
        if any(abs(item - anchor) > tolerance for item in ratios[1:]):
            raise ChoiceQualityGrowthBatchError(
                f"CSD qfq/none OHLC adjustment ratio drifted for {instrument_id}"
            )
    # In addition to the same-date adjustment ratio, each basis must carry the
    # prior controlled session's close forward through PRECLOSE.
    for series in (qfq_records, none_records):
        for previous, current in zip(series, series[1:]):
            prior_close = Decimal(str(previous["close"]))
            current_preclose = Decimal(str(current["preclose"]))
            tolerance = max(
                Decimal("0.000001"), abs(prior_close) * Decimal("0.000001")
            )
            if abs(current_preclose - prior_close) > tolerance:
                raise ChoiceQualityGrowthBatchError(
                    f"CSD PRECLOSE continuity drifted for {instrument_id}"
                )


def _strict_bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ChoiceQualityGrowthBatchError(f"{field_name} is not an explicit boolean")


def _trading_status(value: Any) -> str:
    text = str(value).strip().casefold()
    if text in _TRADING_STATUSES:
        return "trading"
    if text in _SUSPENDED_STATUSES:
        return "suspended"
    raise ChoiceQualityGrowthBatchError("tradestatus is unknown")


def _iso_date(value: Any, field_name: str) -> str:
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except ValueError as exc:
        raise ChoiceQualityGrowthBatchError(f"{field_name} is not an ISO date") from exc


def _normalize_records(
    task: Mapping[str, Any], payload: ProviderPayload
) -> tuple[Mapping[str, Any], ...]:
    kind = task["kind"]
    records = tuple(dict(item) for item in payload.records)
    if kind == "calendar":
        expected = {"calendar_date", "is_trading_day", "available_at", "availability_status", "source_record_id"}
        normalized: list[Mapping[str, Any]] = []
        for row in records:
            if set(row) != expected or not isinstance(row["is_trading_day"], bool):
                raise ChoiceQualityGrowthBatchError("calendar row contract drifted")
            normalized.append(
                {
                    "calendar_date": _iso_date(row["calendar_date"], "calendar_date"),
                    "is_trading_day": row["is_trading_day"],
                }
            )
        expected_days: list[str] = []
        cursor = date.fromisoformat(str(task["start_date"]))
        end = date.fromisoformat(str(task["end_date"]))
        while cursor <= end:
            expected_days.append(cursor.isoformat())
            cursor += timedelta(days=1)
        if [item["calendar_date"] for item in normalized] != expected_days:
            raise ChoiceQualityGrowthBatchError("calendar has a date gap or reordering")
        return tuple(normalized)

    if kind == "membership":
        expected = {
            "sector_code",
            "membership_date",
            "instrument_id",
            "security_short_name",
        }
        normalized = []
        for row in records:
            if set(row) != expected:
                raise ChoiceQualityGrowthBatchError("membership row contract drifted")
            instrument = str(row["instrument_id"])
            if _INSTRUMENT.fullmatch(instrument) is None:
                raise ChoiceQualityGrowthBatchError("membership instrument is not canonical")
            short_name = str(row["security_short_name"]).strip()
            if (
                row["sector_code"] != SECTOR_CODE
                or row["membership_date"] != task["membership_date"]
                or not short_name
            ):
                raise ChoiceQualityGrowthBatchError("membership identity drifted")
            normalized.append(
                {
                    "membership_date": task["membership_date"],
                    "instrument_id": instrument,
                    "security_short_name": short_name,
                }
            )
        ids = [item["instrument_id"] for item in normalized]
        if len(ids) != EXPECTED_MEMBERS_PER_DECISION or ids != sorted(set(ids)):
            raise ChoiceQualityGrowthBatchError(
                "CSI 800 membership must contain exactly 800 ascending unique members"
            )
        return tuple(normalized)

    if kind == "csd":
        expected = {
            "instrument_id",
            "trading_date",
            "adjustment",
            *(item.lower() for item in CSD_FIELDS),
        }
        normalized = []
        for row in records:
            if set(row) != expected:
                raise ChoiceQualityGrowthBatchError("CSD row contract drifted")
            if row["instrument_id"] != task["instrument_id"] or row["adjustment"] != task["price_basis"]:
                raise ChoiceQualityGrowthBatchError("CSD identity or basis drifted")
            trading_date = _iso_date(row["trading_date"], "trading_date")
            status = _trading_status(row["tradestatus"])
            open_value = _decimal_text(row["open"], "open", positive=True)
            high_value = _decimal_text(row["high"], "high", positive=True)
            low_value = _decimal_text(row["low"], "low", positive=True)
            close_value = _decimal_text(row["close"], "close", positive=True)
            _validate_ohlc(open_value, high_value, low_value, close_value)
            normalized.append(
                {
                    "instrument_id": task["instrument_id"],
                    "trading_date": trading_date,
                    "price_basis": task["price_basis"],
                    "open": open_value,
                    "high": high_value,
                    "low": low_value,
                    "close": close_value,
                    "preclose": _decimal_text(row["preclose"], "preclose", positive=True),
                    "volume": _decimal_text(row["volume"], "volume"),
                    "amount": _decimal_text(row["amount"], "amount"),
                    "trading_status": status,
                    "is_st": _strict_bool(row["isststock"], "isststock"),
                    # Choice HIGHLIMIT/LOWLIMIT are touch flags, never prices.
                    "high_limit_hit": _strict_bool(row["highlimit"], "highlimit"),
                    "low_limit_hit": _strict_bool(row["lowlimit"], "lowlimit"),
                }
            )
        dates = [item["trading_date"] for item in normalized]
        if dates != sorted(set(dates)):
            raise ChoiceQualityGrowthBatchError("CSD dates are duplicated or reordered")
        return tuple(normalized)

    if kind == "css_state_batch":
        expected = {
            "instrument_id",
            "trading_date",
            *(item.lower() for item in CSS_STATE_FIELDS),
        }
        instrument_ids = _validated_task_instruments(task["instrument_ids"])
        if len(records) != len(instrument_ids):
            raise ChoiceQualityGrowthBatchError("CSS state batch width drifted")
        normalized = []
        for instrument_id, row in zip(instrument_ids, records):
            if (
                set(row) != expected
                or row["instrument_id"] != instrument_id
                or row["trading_date"] != task["trading_date"]
            ):
                raise ChoiceQualityGrowthBatchError("CSS state row contract drifted")
            limit_up = _decimal_text(
                row["limitupprice"], "limitupprice", positive=True
            )
            limit_down = _decimal_text(
                row["limitdownprice"], "limitdownprice", positive=True
            )
            _validate_limit_prices(limit_up, limit_down)
            normalized.append(
                {
                    "instrument_id": instrument_id,
                    "trading_date": task["trading_date"],
                    "limit_up_price": limit_up,
                    "limit_down_price": limit_down,
                    "trading_status": _trading_status(row["tradestatus"]),
                    "is_st": _strict_bool(row["isststock"], "isststock"),
                    "historical_date_proven": True,
                }
            )
        return tuple(normalized)
    if kind == "css_list_date_batch":
        expected = {"instrument_id", "listdate"}
        instrument_ids = _validated_task_instruments(task["instrument_ids"])
        if len(records) != len(instrument_ids):
            raise ChoiceQualityGrowthBatchError("CSS LISTDATE batch width drifted")
        normalized = []
        for instrument_id, row in zip(instrument_ids, records):
            if set(row) != expected or row["instrument_id"] != instrument_id:
                raise ChoiceQualityGrowthBatchError("CSS LISTDATE row contract drifted")
            normalized.append(
                {
                    "instrument_id": instrument_id,
                    "list_date": _iso_date(row["listdate"], "listdate"),
                    "list_date_semantics": "static_reference_field_not_historical_state",
                }
            )
        return tuple(normalized)
    raise ChoiceQualityGrowthBatchError(f"unsupported fixed task kind: {kind}")


def _validate_stored_records(
    task: Mapping[str, Any], records: Sequence[Mapping[str, Any]]
) -> None:
    """Revalidate normalized artifacts during resume and offline verification."""

    _validate_task_contract(task)
    kind = task["kind"]
    if kind == "calendar":
        expected_keys = {"calendar_date", "is_trading_day"}
        dates: list[str] = []
        for row in records:
            if set(row) != expected_keys or not isinstance(row["is_trading_day"], bool):
                raise ChoiceQualityGrowthBatchError("stored calendar row drifted")
            dates.append(_iso_date(row["calendar_date"], "calendar_date"))
        expected_dates: list[str] = []
        cursor = date.fromisoformat(str(task["start_date"]))
        end = date.fromisoformat(str(task["end_date"]))
        while cursor <= end:
            expected_dates.append(cursor.isoformat())
            cursor += timedelta(days=1)
        if dates != expected_dates:
            raise ChoiceQualityGrowthBatchError("stored calendar coverage drifted")
        return
    if kind == "membership":
        expected_keys = {
            "membership_date",
            "instrument_id",
            "security_short_name",
        }
        ids: list[str] = []
        for row in records:
            instrument = str(row.get("instrument_id", ""))
            if (
                set(row) != expected_keys
                or row["membership_date"] != task["membership_date"]
                or _INSTRUMENT.fullmatch(instrument) is None
                or not str(row["security_short_name"]).strip()
            ):
                raise ChoiceQualityGrowthBatchError("stored membership row drifted")
            ids.append(instrument)
        if len(ids) != EXPECTED_MEMBERS_PER_DECISION or ids != sorted(set(ids)):
            raise ChoiceQualityGrowthBatchError("stored membership is not exact 800")
        return
    if kind == "csd":
        expected_keys = {
            "instrument_id",
            "trading_date",
            "price_basis",
            "open",
            "high",
            "low",
            "close",
            "preclose",
            "volume",
            "amount",
            "trading_status",
            "is_st",
            "high_limit_hit",
            "low_limit_hit",
        }
        dates: list[str] = []
        task_start = date.fromisoformat(str(task["start_date"]))
        task_end = date.fromisoformat(str(task["end_date"]))
        for row in records:
            if (
                set(row) != expected_keys
                or row["instrument_id"] != task["instrument_id"]
                or row["price_basis"] != task["price_basis"]
                or row["trading_status"] not in {"trading", "suspended"}
                or any(
                    not isinstance(row[name], bool)
                    for name in ("is_st", "high_limit_hit", "low_limit_hit")
                )
            ):
                raise ChoiceQualityGrowthBatchError("stored CSD row drifted")
            trading_date = _iso_date(row["trading_date"], "trading_date")
            if not task_start <= date.fromisoformat(trading_date) <= task_end:
                raise ChoiceQualityGrowthBatchError(
                    "stored CSD date falls outside the fixed task window"
                )
            dates.append(trading_date)
            for name in ("open", "high", "low", "close", "preclose"):
                _decimal_text(row[name], name, positive=True)
            _validate_ohlc(
                str(row["open"]),
                str(row["high"]),
                str(row["low"]),
                str(row["close"]),
            )
            for name in ("volume", "amount"):
                _decimal_text(row[name], name)
        if dates != sorted(set(dates)):
            raise ChoiceQualityGrowthBatchError("stored CSD dates drifted")
        return
    instruments = _validated_task_instruments(task["instrument_ids"])
    if len(records) != len(instruments):
        raise ChoiceQualityGrowthBatchError("stored CSS batch width drifted")
    if kind == "css_state_batch":
        expected_keys = {
            "instrument_id",
            "trading_date",
            "limit_up_price",
            "limit_down_price",
            "trading_status",
            "is_st",
            "historical_date_proven",
        }
        for instrument_id, row in zip(instruments, records):
            if (
                set(row) != expected_keys
                or row["instrument_id"] != instrument_id
                or row["trading_date"] != task["trading_date"]
                or row["trading_status"] not in {"trading", "suspended"}
                or not isinstance(row["is_st"], bool)
                or row["historical_date_proven"] is not True
            ):
                raise ChoiceQualityGrowthBatchError("stored CSS state row drifted")
            limit_up = _decimal_text(
                row["limit_up_price"], "limit_up_price", positive=True
            )
            limit_down = _decimal_text(
                row["limit_down_price"], "limit_down_price", positive=True
            )
            _validate_limit_prices(limit_up, limit_down)
        return
    expected_keys = {
        "instrument_id",
        "list_date",
        "list_date_semantics",
    }
    for instrument_id, row in zip(instruments, records):
        if (
            set(row) != expected_keys
            or row["instrument_id"] != instrument_id
            or row["list_date_semantics"]
            != "static_reference_field_not_historical_state"
        ):
            raise ChoiceQualityGrowthBatchError("stored CSS LISTDATE row drifted")
        _iso_date(row["list_date"], "list_date")


def _strict_raw_object(raw_content: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(
            raw_content.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ChoiceQualityGrowthBatchError(
                    f"non-finite raw JSON constant is forbidden: {item}"
                )
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ChoiceQualityGrowthBatchError("raw evidence is not strict JSON") from exc
    if not isinstance(payload, dict) or canonical_json_bytes(payload) != raw_content:
        raise ChoiceQualityGrowthBatchError(
            "raw evidence is not canonical deterministic JSON"
        )
    return payload


def _replay_raw_records(
    task: Mapping[str, Any], raw_content: bytes
) -> tuple[Mapping[str, Any], ...]:
    """Replay the fixed canonical SDK envelope into normalized task records."""

    _validate_task_contract(task)
    payload = _strict_raw_object(raw_content)
    kind = str(task["kind"])
    if kind == "calendar":
        if set(payload) != {"operation", "request", "trade_calendar"} or payload.get(
            "operation"
        ) != "tradedates":
            raise ChoiceQualityGrowthBatchError("calendar raw envelope drifted")
        expected_request = MarketDataRequest(
            dataset_type="trade_calendar",
            start_date=date.fromisoformat(str(task["start_date"])),
            end_date=date.fromisoformat(str(task["end_date"])),
            retrieval_mode="historical_backfill",
            requested_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
        ).fingerprint_payload(ChoiceProvider.provider_id, ChoiceProvider.adapter_version)
        calendar = payload.get("trade_calendar")
        if (
            payload.get("request") != expected_request
            or not isinstance(calendar, Mapping)
            or set(calendar) != {"options", "dates"}
            or calendar.get("options") != ChoiceProvider._calendar_options("CNSESH")
            or not isinstance(calendar.get("dates"), list)
        ):
            raise ChoiceQualityGrowthBatchError("calendar raw request drifted")
        open_dates = [
            _iso_date(item, "calendar.raw_date") for item in calendar["dates"]
        ]
        if open_dates != sorted(set(open_dates)):
            raise ChoiceQualityGrowthBatchError("calendar raw dates drifted")
        cursor = date.fromisoformat(str(task["start_date"]))
        end = date.fromisoformat(str(task["end_date"]))
        normalized: list[Mapping[str, Any]] = []
        open_set = set(open_dates)
        while cursor <= end:
            normalized.append(
                {
                    "calendar_date": cursor.isoformat(),
                    "is_trading_day": cursor.isoformat() in open_set,
                }
            )
            cursor += timedelta(days=1)
        return tuple(normalized)

    expected_envelopes = {
        "membership": (
            "quality_growth_fixed_csi800_sector",
            {"operation", "request", "records"},
        ),
        "csd": (
            "quality_growth_fixed_csd",
            {"operation", "request", "records"},
        ),
        "css_state_batch": (
            "quality_growth_fixed_css_state_batch",
            {"operation", "request", "response_dates", "records"},
        ),
        "css_list_date_batch": (
            "quality_growth_fixed_css_list_date_batch",
            {"operation", "request", "response_dates", "records"},
        ),
    }
    operation, fields = expected_envelopes[kind]
    if set(payload) != fields or payload.get("operation") != operation:
        raise ChoiceQualityGrowthBatchError("fixed Choice raw envelope drifted")
    request = payload.get("request")
    raw_records = payload.get("records")
    if not isinstance(request, Mapping) or not isinstance(raw_records, list):
        raise ChoiceQualityGrowthBatchError("fixed Choice raw payload drifted")
    if kind == "membership":
        expected_request = {
            "sector_code": SECTOR_CODE,
            "membership_date": task["membership_date"],
            "options": ChoiceProvider._QUALITY_GROWTH_SECTOR_OPTIONS,
        }
    elif kind == "csd":
        expected_request = {
            "instrument_id": task["instrument_id"],
            "start_date": task["start_date"],
            "end_date": task["end_date"],
            "adjustment": task["price_basis"],
            "indicators": list(CSD_FIELDS),
            "options": ChoiceProvider._quality_growth_csd_options(
                str(task["price_basis"])
            ),
        }
    elif kind == "css_state_batch":
        expected_request = {
            "instrument_ids": task["instrument_ids"],
            "trading_date": task["trading_date"],
            "indicators": list(CSS_STATE_FIELDS),
            "options": ChoiceProvider._quality_growth_css_options(
                date.fromisoformat(str(task["trading_date"]))
            ),
        }
        if payload.get("response_dates") != [task["trading_date"]]:
            raise ChoiceQualityGrowthBatchError(
                "CSS state raw evidence lacks its historical response date"
            )
    else:
        expected_request = {
            "instrument_ids": task["instrument_ids"],
            "indicators": list(CSS_LIST_DATE_FIELDS),
            "options": ChoiceProvider._quality_growth_list_date_options(),
        }
        if payload.get("response_dates") != []:
            raise ChoiceQualityGrowthBatchError(
                "CSS LISTDATE raw response must remain static and undated"
            )
    if dict(request) != expected_request:
        raise ChoiceQualityGrowthBatchError("fixed Choice raw request drifted")
    replay_payload = ProviderPayload(
        raw_content=raw_content,
        records=tuple(raw_records),
        fetched_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
        upstream_source="choice.raw_replay",
    )
    return _normalize_records(task, replay_payload)


class _BatchStore:
    def __init__(
        self, root: Path, plan: Mapping[str, Any], *, read_only: bool = False
    ) -> None:
        self.root = _absolute_lexical(root)
        _guard_batch_path(self.root, self.root)
        self.plan = dict(plan)
        self.plan_sha256 = sha256_bytes(canonical_json_bytes(self.plan))
        self.plan_path = self.root / "plan.json"
        self.checkpoint_path = self.root / "checkpoint.json"
        self.read_only = read_only
        plan_envelope = {"plan": self.plan, "plan_sha256": self.plan_sha256}
        if read_only:
            if not self.root.is_dir() or not self.plan_path.is_file():
                raise ChoiceQualityGrowthBatchError(
                    "read-only verification requires an existing batch root"
                )
            _guard_batch_path(self.root, self.plan_path)
            if _load_json(self.plan_path) != plan_envelope:
                raise ChoiceQualityGrowthBatchError("stored fixed plan drifted")
        else:
            self.root.mkdir(parents=True, exist_ok=True)
            _guard_batch_path(self.root, self.root)
            _write_immutable(
                self.plan_path,
                canonical_json_bytes(plan_envelope),
                root=self.root,
            )

    @staticmethod
    def _empty_checkpoint(plan_sha256: str) -> dict[str, Any]:
        return {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "plan_sha256": plan_sha256,
            "completed": {},
            "failures": {},
        }

    def _validate_checkpoint(self, checkpoint: Mapping[str, Any]) -> None:
        if (
            set(checkpoint)
            != {"schema_version", "plan_sha256", "completed", "failures"}
            or checkpoint.get("schema_version") != CHECKPOINT_SCHEMA_VERSION
            or checkpoint.get("plan_sha256") != self.plan_sha256
            or not isinstance(checkpoint.get("completed"), dict)
            or not isinstance(checkpoint.get("failures"), dict)
        ):
            raise ChoiceQualityGrowthBatchError("checkpoint contract or plan drifted")
        for task_id, failure in checkpoint["failures"].items():
            if not isinstance(failure, Mapping):
                raise ChoiceQualityGrowthBatchError(
                    "checkpoint failure reference is malformed"
                )
            _validate_failure_payload(str(task_id), failure)

    def _has_recoverable_work(self) -> bool:
        for name in ("artifacts", "checkpoint_events", "failure_events"):
            directory = self.root / name
            _guard_batch_path(self.root, directory)
            if directory.is_dir() and any(directory.iterdir()):
                return True
        return False

    def _merge_recoverable_artifacts(self, checkpoint: dict[str, Any]) -> None:
        event_directory = self.root / "checkpoint_events"
        _guard_batch_path(self.root, event_directory)
        if event_directory.is_dir():
            for event_path in sorted(event_directory.glob("*.json")):
                _guard_batch_path(self.root, event_path)
                event = _load_json(event_path)
                task_id = str(event.get("task_id", ""))
                reference = event.get("artifact")
                if (
                    set(event)
                    != {"schema_version", "plan_sha256", "task_id", "artifact"}
                    or event.get("schema_version")
                    != CHECKPOINT_EVENT_SCHEMA_VERSION
                    or event.get("plan_sha256") != self.plan_sha256
                    or event_path.name != f"{task_id}.json"
                    or not isinstance(reference, Mapping)
                ):
                    raise ChoiceQualityGrowthBatchError(
                        "checkpoint completion event drifted"
                    )
                self.load_artifact(reference, expected_task_id=task_id)
                existing = checkpoint["completed"].get(task_id)
                if existing is not None and dict(existing) != dict(reference):
                    raise ChoiceQualityGrowthBatchError(
                        "checkpoint completion reference collision"
                    )
                checkpoint["completed"][task_id] = dict(reference)

        artifact_directory = self.root / "artifacts"
        _guard_batch_path(self.root, artifact_directory)
        if artifact_directory.is_dir():
            for artifact_path in sorted(artifact_directory.glob("*.json")):
                _guard_batch_path(self.root, artifact_path)
                task_id = artifact_path.stem
                if _SHA256.fullmatch(task_id) is None:
                    raise ChoiceQualityGrowthBatchError(
                        "orphan artifact filename is not task-bound"
                    )
                raw = artifact_path.read_bytes()
                reference = {
                    "path": _relative(self.root, artifact_path),
                    "sha256": sha256_bytes(raw),
                }
                self.load_artifact(reference, expected_task_id=task_id)
                existing = checkpoint["completed"].get(task_id)
                if existing is not None and dict(existing) != reference:
                    raise ChoiceQualityGrowthBatchError(
                        "orphan artifact reference collision"
                    )
                checkpoint["completed"][task_id] = reference

        failure_directory = self.root / "failure_events"
        _guard_batch_path(self.root, failure_directory)
        if failure_directory.is_dir():
            for failure_path in sorted(failure_directory.glob("*.json")):
                _guard_batch_path(self.root, failure_path)
                event = _load_json(failure_path)
                task_id = str(event.get("task_id", ""))
                failure = event.get("failure")
                if (
                    set(event)
                    != {"schema_version", "plan_sha256", "task_id", "failure"}
                    or event.get("schema_version") != FAILURE_EVENT_SCHEMA_VERSION
                    or event.get("plan_sha256") != self.plan_sha256
                    or failure_path.name != f"{task_id}.json"
                    or _SHA256.fullmatch(task_id) is None
                    or not isinstance(failure, Mapping)
                ):
                    raise ChoiceQualityGrowthBatchError("failure event drifted")
                _validate_failure_payload(task_id, failure)
                if task_id not in checkpoint["completed"]:
                    checkpoint["failures"][task_id] = dict(failure)
        for task_id in checkpoint["completed"]:
            checkpoint["failures"].pop(task_id, None)

    def load_checkpoint(self, *, resume: bool) -> dict[str, Any]:
        _guard_batch_path(self.root, self.checkpoint_path)
        if self.checkpoint_path.exists():
            if not resume:
                raise ChoiceQualityGrowthBatchError(
                    "checkpoint exists; use resume or a new output directory"
                )
            checkpoint = _load_json(self.checkpoint_path)
            self._validate_checkpoint(checkpoint)
            for task_id, reference in checkpoint["completed"].items():
                if not isinstance(reference, Mapping):
                    raise ChoiceQualityGrowthBatchError(
                        "checkpoint reference is malformed"
                    )
                self.load_artifact(reference, expected_task_id=task_id)
        else:
            if not resume and self._has_recoverable_work():
                raise ChoiceQualityGrowthBatchError(
                    "recoverable task artifacts exist; use resume or a new output directory"
                )
            checkpoint = self._empty_checkpoint(self.plan_sha256)
        if resume:
            self._merge_recoverable_artifacts(checkpoint)
        return checkpoint

    def save_checkpoint(self, checkpoint: Mapping[str, Any]) -> None:
        if self.read_only:
            raise ChoiceQualityGrowthBatchError("read-only store cannot write checkpoint")
        self._validate_checkpoint(checkpoint)
        _guard_batch_path(self.root, self.checkpoint_path)
        _atomic_write(self.checkpoint_path, canonical_json_bytes(dict(checkpoint)))

    def load_artifact(
        self, reference: Mapping[str, Any], *, expected_task_id: str
    ) -> dict[str, Any]:
        if (
            set(reference) != {"path", "sha256"}
            or _SHA256.fullmatch(str(reference.get("sha256", ""))) is None
            or _SHA256.fullmatch(expected_task_id) is None
        ):
            raise ChoiceQualityGrowthBatchError(
                "checkpoint artifact reference contract drifted"
            )
        artifact_path = _resolve_ref(self.root, reference.get("path"))
        raw = artifact_path.read_bytes()
        if sha256_bytes(raw) != reference.get("sha256"):
            raise ChoiceQualityGrowthBatchError("checkpoint artifact hash mismatch")
        artifact = _load_json(artifact_path)
        task = artifact.get("task")
        if (
            set(artifact)
            != {
                "schema_version",
                "task_id",
                "task",
                "plan_sha256",
                "provider_id",
                "provider_adapter_version",
                "batch_adapter_version",
                "fetched_at",
                "upstream_source",
                "source_authenticated",
                "raw_semantics",
                "integrity_semantics",
                "issues",
                "raw_path",
                "raw_content_sha256",
                "records_content_sha256",
                "records",
            }
            or artifact.get("schema_version") != ARTIFACT_SCHEMA_VERSION
            or artifact.get("task_id") != expected_task_id
            or not isinstance(task, Mapping)
            or _task_id(task) != expected_task_id
            or artifact.get("plan_sha256") != self.plan_sha256
            or artifact.get("provider_id") != PROVIDER_ID
            or artifact.get("provider_adapter_version")
            != ChoiceProvider.adapter_version
            or artifact.get("batch_adapter_version") != ADAPTER_VERSION
            or artifact.get("source_authenticated") is not False
            or artifact.get("raw_semantics") != RAW_SEMANTICS
            or artifact.get("integrity_semantics") != INTEGRITY_SEMANTICS
            or not str(artifact.get("upstream_source", "")).strip()
            or not isinstance(artifact.get("issues"), list)
        ):
            raise ChoiceQualityGrowthBatchError("stored task artifact contract drifted")
        _aware(
            datetime.fromisoformat(str(artifact.get("fetched_at", ""))),
            "artifact.fetched_at",
        )
        records = artifact.get("records")
        if not isinstance(records, list) or sha256_bytes(
            canonical_json_bytes(records)
        ) != artifact.get("records_content_sha256"):
            raise ChoiceQualityGrowthBatchError("stored normalized records hash mismatch")
        _validate_stored_records(task, records)
        raw_path = _resolve_ref(self.root, artifact.get("raw_path"))
        raw_evidence = raw_path.read_bytes()
        if sha256_bytes(raw_evidence) != artifact.get("raw_content_sha256"):
            raise ChoiceQualityGrowthBatchError("stored raw evidence hash mismatch")
        replayed = [dict(item) for item in _replay_raw_records(task, raw_evidence)]
        if replayed != records:
            raise ChoiceQualityGrowthBatchError(
                "normalized records do not replay from fixed raw evidence"
            )
        return artifact

    def capture(
        self,
        checkpoint: dict[str, Any],
        task: Mapping[str, Any],
        fetcher: Callable[[], ProviderPayload],
    ) -> dict[str, Any]:
        if self.read_only:
            raise ChoiceQualityGrowthBatchError("read-only store cannot capture")
        _validate_task_contract(task)
        task_id = _task_id(task)
        completed = checkpoint["completed"]
        if task_id in completed:
            return self.load_artifact(completed[task_id], expected_task_id=task_id)
        payload = fetcher()
        if not isinstance(payload, ProviderPayload):
            raise ChoiceQualityGrowthBatchError("fixed Choice fetch returned no payload")
        _aware(payload.fetched_at, "payload.fetched_at")
        records = [dict(item) for item in _normalize_records(task, payload)]
        _validate_stored_records(task, records)
        replayed = [dict(item) for item in _replay_raw_records(task, payload.raw_content)]
        if replayed != records:
            raise ChoiceQualityGrowthBatchError(
                "fixed raw response does not replay to normalized records"
            )
        raw_sha256 = sha256_bytes(payload.raw_content)
        raw_path = self.root / "raw" / f"{raw_sha256}.raw"
        _write_immutable(raw_path, payload.raw_content, root=self.root)
        artifact = {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "task_id": task_id,
            "task": dict(task),
            "plan_sha256": self.plan_sha256,
            "provider_id": PROVIDER_ID,
            "provider_adapter_version": ChoiceProvider.adapter_version,
            "batch_adapter_version": ADAPTER_VERSION,
            "fetched_at": payload.fetched_at.isoformat(),
            "upstream_source": payload.upstream_source,
            "source_authenticated": False,
            "raw_semantics": RAW_SEMANTICS,
            "integrity_semantics": INTEGRITY_SEMANTICS,
            "issues": [dict(item) for item in payload.issues],
            "raw_path": _relative(self.root, raw_path),
            "raw_content_sha256": raw_sha256,
            "records_content_sha256": sha256_bytes(canonical_json_bytes(records)),
            "records": records,
        }
        artifact_raw = canonical_json_bytes(artifact)
        artifact_path = self.root / "artifacts" / f"{task_id}.json"
        _write_immutable(artifact_path, artifact_raw, root=self.root)
        reference = {
            "path": _relative(self.root, artifact_path),
            "sha256": sha256_bytes(artifact_raw),
        }
        event = {
            "schema_version": CHECKPOINT_EVENT_SCHEMA_VERSION,
            "plan_sha256": self.plan_sha256,
            "task_id": task_id,
            "artifact": reference,
        }
        event_path = self.root / "checkpoint_events" / f"{task_id}.json"
        _write_immutable(
            event_path, canonical_json_bytes(event), root=self.root
        )
        completed[task_id] = reference
        checkpoint["failures"].pop(task_id, None)
        if len(completed) % CHECKPOINT_COMPACTION_INTERVAL == 0:
            self.save_checkpoint(checkpoint)
        return artifact

    def record_failure(
        self,
        checkpoint: dict[str, Any],
        task: Mapping[str, Any],
        exc: Exception,
    ) -> None:
        if self.read_only:
            raise ChoiceQualityGrowthBatchError("read-only store cannot record failure")
        task_id = _task_id(task)
        candidate_code = str(getattr(exc, "code", "batch_capture_failed")).lower()
        error_code = (
            candidate_code
            if _SAFE_ERROR_CODE.fullmatch(candidate_code)
            else "batch_capture_failed"
        )
        candidate_type = type(exc).__name__
        failure = {
            "task": dict(task),
            "error_type": (
                candidate_type
                if _SAFE_ERROR_TYPE.fullmatch(candidate_type)
                else "RuntimeError"
            ),
            "error_code": error_code,
            "error_message": safe_error_text(exc)[:1000],
        }
        _validate_failure_payload(task_id, failure)
        checkpoint["failures"][task_id] = failure
        event = {
            "schema_version": FAILURE_EVENT_SCHEMA_VERSION,
            "plan_sha256": self.plan_sha256,
            "task_id": task_id,
            "failure": failure,
        }
        failure_path = self.root / "failure_events" / f"{task_id}.json"
        _guard_batch_path(self.root, failure_path)
        _atomic_write(failure_path, canonical_json_bytes(event))


def _record_failure(
    store: _BatchStore,
    checkpoint: dict[str, Any],
    task: Mapping[str, Any],
    exc: Exception,
) -> None:
    store.record_failure(checkpoint, task, exc)


def _calendar_sessions(artifact: Mapping[str, Any]) -> tuple[date, ...]:
    return tuple(
        date.fromisoformat(str(row["calendar_date"]))
        for row in artifact["records"]
        if row["is_trading_day"] is True
    )


def _artifact_for(
    store: _BatchStore, checkpoint: Mapping[str, Any], task: Mapping[str, Any]
) -> dict[str, Any] | None:
    task_id = _task_id(task)
    reference = checkpoint["completed"].get(task_id)
    if reference is None:
        return None
    return store.load_artifact(reference, expected_task_id=task_id)


def _assess(
    store: _BatchStore, checkpoint: Mapping[str, Any]
) -> tuple[bool, dict[str, Any], tuple[str, ...]]:
    cutoff = date.fromisoformat(str(store.plan["cutoff_date"]))
    calendar_task = _task(
        "calendar",
        start_date=PRICE_START_DATE.isoformat(),
        end_date=cutoff.isoformat(),
    )
    expected_completed_task_ids = {_task_id(calendar_task)}
    missing: list[str] = []
    calendar_artifact = _artifact_for(store, checkpoint, calendar_task)
    if calendar_artifact is None:
        missing.append("calendar")
        sessions: tuple[date, ...] = ()
        grid: tuple[date, ...] = ()
    else:
        sessions = _calendar_sessions(calendar_artifact)
        try:
            grid = derive_choice_quality_growth_decision_grid(sessions, cutoff)
        except ChoiceQualityGrowthBatchError as exc:
            missing.append(f"decision_grid:{safe_error_text(exc)}")
            grid = ()

    memberships: dict[date, tuple[str, ...]] = {}
    if grid:
        for decision_date in grid:
            task = _task("membership", membership_date=decision_date.isoformat())
            expected_completed_task_ids.add(_task_id(task))
            artifact = _artifact_for(store, checkpoint, task)
            if artifact is None:
                missing.append(f"membership:{decision_date.isoformat()}")
                continue
            memberships[decision_date] = tuple(
                str(row["instrument_id"]) for row in artifact["records"]
            )

    union_ids = tuple(sorted({item for ids in memberships.values() for item in ids}))
    membership_sets = {
        decision_date: frozenset(ids)
        for decision_date, ids in memberships.items()
    }
    listing_dates: dict[str, date] = {}
    state_rows: dict[tuple[str, str], Mapping[str, Any]] = {}
    price_rows: dict[tuple[str, str], tuple[Mapping[str, Any], ...]] = {}
    required_price_end: dict[str, date] = {}
    if grid and len(memberships) == len(grid):
        session_index = {day: index for index, day in enumerate(sessions)}
        for instruments in _instrument_batches(union_ids):
            task = _task(
                "css_list_date_batch", instrument_ids=list(instruments)
            )
            expected_completed_task_ids.add(_task_id(task))
            artifact = _artifact_for(store, checkpoint, task)
            if artifact is None:
                missing.append(f"css:list_date_batch:{instruments[0]}")
                continue
            for row in artifact["records"]:
                instrument_id = str(row["instrument_id"])
                if instrument_id in listing_dates:
                    missing.append(f"css:list_date_duplicate:{instrument_id}")
                    continue
                listing_dates[instrument_id] = date.fromisoformat(
                    str(row["list_date"])
                )

        for decision_date, members in memberships.items():
            execution_date = sessions[session_index[decision_date] + 1]
            outcome_end = sessions[
                session_index[decision_date] + 1 + OUTCOME_SESSIONS
            ]
            for instrument_id in members:
                required_price_end[instrument_id] = max(
                    required_price_end.get(instrument_id, outcome_end),
                    outcome_end,
                )
            for instruments in _instrument_batches(members):
                task = _task(
                    "css_state_batch",
                    decision_date=decision_date.isoformat(),
                    instrument_ids=list(instruments),
                    trading_date=execution_date.isoformat(),
                )
                expected_completed_task_ids.add(_task_id(task))
                artifact = _artifact_for(store, checkpoint, task)
                if artifact is None:
                    missing.append(
                        f"css:state_batch:{execution_date.isoformat()}:{instruments[0]}"
                    )
                    continue
                for row in artifact["records"]:
                    key = (execution_date.isoformat(), str(row["instrument_id"]))
                    if key in state_rows:
                        missing.append(f"css:state_duplicate:{key[0]}:{key[1]}")
                    state_rows[key] = row

        for instrument_id in union_ids:
            for price_basis in PRICE_BASES:
                task = _task(
                    "csd",
                    instrument_id=instrument_id,
                    start_date=PRICE_START_DATE.isoformat(),
                    end_date=cutoff.isoformat(),
                    price_basis=price_basis,
                )
                expected_completed_task_ids.add(_task_id(task))
                artifact = _artifact_for(store, checkpoint, task)
                if artifact is None:
                    missing.append(f"csd:{price_basis}:{instrument_id}")
                    continue
                price_rows[(instrument_id, price_basis)] = tuple(
                    artifact["records"]
                )

        controlled_dates = tuple(day.isoformat() for day in sessions)
        for instrument_id in union_ids:
            listing_date = listing_dates.get(instrument_id)
            if listing_date is None:
                missing.append(f"css:list_date_missing:{instrument_id}")
                continue
            if any(
                listing_date > decision_date
                for decision_date, members in membership_sets.items()
                if instrument_id in members
            ):
                missing.append(f"membership:before_list_date:{instrument_id}")
            required_dates = tuple(
                item
                for item in controlled_dates
                if max(listing_date, PRICE_START_DATE).isoformat()
                <= item
                <= required_price_end[instrument_id].isoformat()
            )
            qfq = price_rows.get((instrument_id, "qfq"))
            unadjusted = price_rows.get((instrument_id, "none"))
            if qfq is None or unadjusted is None:
                continue
            try:
                _validate_price_basis_pair(
                    instrument_id,
                    qfq,
                    unadjusted,
                    required_dates,
                    controlled_dates,
                )
            except ChoiceQualityGrowthBatchError as exc:
                missing.append(
                    f"csd:basis_invariant:{instrument_id}:{safe_error_text(exc)}"
                )
                continue
            unadjusted_by_date = {
                str(row["trading_date"]): row for row in unadjusted
            }
            for decision_date, members in membership_sets.items():
                if instrument_id not in members:
                    continue
                execution_date = sessions[
                    session_index[decision_date] + 1
                ].isoformat()
                state = state_rows.get((execution_date, instrument_id))
                price = unadjusted_by_date.get(execution_date)
                if state is None:
                    missing.append(
                        f"css:state_missing:{execution_date}:{instrument_id}"
                    )
                elif price is None:
                    missing.append(
                        f"csd:execution_date_missing:{execution_date}:{instrument_id}"
                    )
                elif (
                    state["trading_status"] != price["trading_status"]
                    or state["is_st"] != price["is_st"]
                ):
                    missing.append(
                        f"css:csd_state_mismatch:{execution_date}:{instrument_id}"
                    )

    completed = checkpoint["completed"]
    unexpected_completed = sorted(
        set(completed) - expected_completed_task_ids
    )
    if unexpected_completed:
        missing.append(
            f"unexpected_completed_tasks:{len(unexpected_completed)}"
        )
    completed_by_kind = {
        kind: 0
        for kind in (
            "calendar",
            "membership",
            "csd",
            "css_state_batch",
            "css_list_date_batch",
        )
    }
    refs: list[dict[str, Any]] = []
    for task_id, reference in sorted(completed.items()):
        artifact = store.load_artifact(reference, expected_task_id=task_id)
        kind = str(artifact["task"]["kind"])
        if kind not in completed_by_kind:
            raise ChoiceQualityGrowthBatchError("checkpoint contains an unknown task")
        completed_by_kind[kind] += 1
        refs.append(
            {
                "task_id": task_id,
                "artifact_sha256": reference["sha256"],
            }
        )
    summary = {
        "decision_dates": [item.isoformat() for item in grid],
        "decision_dates_content_sha256": sha256_bytes(
            canonical_json_bytes([item.isoformat() for item in grid])
        ),
        "membership_union_count": len(union_ids),
        "membership_union_content_sha256": sha256_bytes(
            canonical_json_bytes(list(union_ids))
        ),
        "completed_task_counts": completed_by_kind,
        "completed_task_refs_content_sha256": sha256_bytes(
            canonical_json_bytes(refs)
        ),
        "failed_task_count": len(checkpoint["failures"]),
        "missing_contract_item_count": len(missing),
        "missing_contract_item_sample": missing[:20],
        "highlimit_lowlimit_semantics": "touch_flags_not_prices",
        "price_basis_contract": "qfq_and_none_stored_separately",
        "industry_status": INDUSTRY_BLOCKER,
        "calendar_truth_status": CALENDAR_BLOCKER,
        "source_authenticated": False,
        "raw_semantics": RAW_SEMANTICS,
        "integrity_semantics": INTEGRITY_SEMANTICS,
        "suspended_execution_semantics": "mark_only_not_executable",
    }
    complete = bool(grid) and not missing and not checkpoint["failures"]
    return complete, summary, tuple(missing)


def _finalize_manifest(
    store: _BatchStore,
    checkpoint: Mapping[str, Any],
    *,
    as_of: datetime,
    generated_at: datetime,
) -> ChoiceQualityGrowthBatchRun:
    as_of = _aware(as_of, "as_of")
    generated_at = _aware(generated_at, "generated_at")
    if generated_at < as_of:
        raise ChoiceQualityGrowthBatchError("generated_at precedes as_of")
    complete, summary, missing = _assess(store, checkpoint)
    store.save_checkpoint(checkpoint)
    checkpoint_raw = canonical_json_bytes(dict(checkpoint))
    checkpoint_sha256 = sha256_bytes(checkpoint_raw)
    checkpoint_snapshot = store.root / "checkpoints" / f"{checkpoint_sha256}.json"
    _write_immutable(checkpoint_snapshot, checkpoint_raw, root=store.root)
    blocking_reasons = [PIT_BLOCKER, INDUSTRY_BLOCKER, CALENDAR_BLOCKER]
    if not complete:
        blocking_reasons.insert(0, "incomplete_fixed_batch_contract")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "adapter_version": ADAPTER_VERSION,
        "provider_id": PROVIDER_ID,
        "status": "blocked" if complete else "incomplete",
        "collection_status": "complete" if complete else "incomplete",
        "admission_status": INDUSTRY_BLOCKER,
        "blocking_reasons": blocking_reasons,
        "as_of": as_of.isoformat(),
        "generated_at": generated_at.isoformat(),
        "point_in_time_status": "historical_backfill_not_original_capture",
        "source_authenticated": False,
        "raw_semantics": RAW_SEMANTICS,
        "integrity_semantics": INTEGRITY_SEMANTICS,
        "plan_path": _relative(store.root, store.plan_path),
        "plan_sha256": store.plan_sha256,
        "checkpoint_path": _relative(store.root, checkpoint_snapshot),
        "checkpoint_sha256": checkpoint_sha256,
        "completeness": summary,
        "missing_contract_items": list(missing[:100]),
        "formal_truth_eligible": False,
        "paper_eligible": False,
        "trade_eligible": False,
        "real_money_candidate": False,
        "live_execution_status": LIVE_STATUS,
    }
    manifest_sha256 = sha256_bytes(canonical_json_bytes(manifest))
    envelope = {**manifest, "manifest_sha256": manifest_sha256}
    manifest_path = store.root / "manifests" / f"{manifest_sha256}.json"
    _write_immutable(
        manifest_path, canonical_json_bytes(envelope), root=store.root
    )
    return ChoiceQualityGrowthBatchRun(
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        status=str(manifest["status"]),
        collection_status=str(manifest["collection_status"]),
        blocking_reasons=tuple(blocking_reasons),
    )


def _collect_choice_quality_growth_batch_locked(
    *,
    provider: ChoiceProvider,
    cutoff_date: date,
    as_of: datetime,
    output_root: Path,
    resume: bool = False,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> ChoiceQualityGrowthBatchRun:
    """Capture the fixed v1 plan with resumable, hash-bound checkpoints."""

    if not isinstance(provider, ChoiceProvider):
        raise ChoiceQualityGrowthBatchError("collector requires ChoiceProvider")
    as_of = _aware(as_of, "as_of")
    if cutoff_date > as_of.astimezone(CHINA_TZ).date():
        raise ChoiceQualityGrowthBatchError("cutoff_date is after as_of")
    plan = fixed_choice_quality_growth_plan(cutoff_date)
    store = _BatchStore(Path(output_root), plan)
    checkpoint = store.load_checkpoint(resume=resume)

    def capture(task: Mapping[str, Any], fetcher: Callable[[], ProviderPayload]) -> dict[str, Any] | None:
        try:
            return store.capture(checkpoint, task, fetcher)
        except Exception as exc:
            _record_failure(store, checkpoint, task, exc)
            return None

    calendar_task = _task(
        "calendar",
        start_date=PRICE_START_DATE.isoformat(),
        end_date=cutoff_date.isoformat(),
    )
    try:
        with provider.diagnostic_session():
            calendar_artifact = capture(
                calendar_task,
                lambda: provider.fetch_quality_growth_calendar(
                    PRICE_START_DATE, cutoff_date
                ),
            )
            if calendar_artifact is None:
                return _finalize_manifest(
                    store, checkpoint, as_of=as_of, generated_at=clock()
                )
            sessions = _calendar_sessions(calendar_artifact)
            try:
                grid = derive_choice_quality_growth_decision_grid(
                    sessions, cutoff_date
                )
            except ChoiceQualityGrowthBatchError as exc:
                _record_failure(store, checkpoint, calendar_task, exc)
                return _finalize_manifest(
                    store, checkpoint, as_of=as_of, generated_at=clock()
                )

            memberships: dict[date, tuple[str, ...]] = {}
            for decision_date in grid:
                task = _task(
                    "membership", membership_date=decision_date.isoformat()
                )
                artifact = capture(
                    task,
                    lambda day=decision_date: provider.fetch_quality_growth_membership(
                        day
                    ),
                )
                if artifact is None:
                    return _finalize_manifest(
                        store, checkpoint, as_of=as_of, generated_at=clock()
                    )
                memberships[decision_date] = tuple(
                    str(row["instrument_id"]) for row in artifact["records"]
                )

            union_ids = tuple(
                sorted({item for members in memberships.values() for item in members})
            )
            for instruments in _instrument_batches(union_ids):
                task = _task(
                    "css_list_date_batch", instrument_ids=list(instruments)
                )
                artifact = capture(
                    task,
                    lambda codes=instruments: provider.fetch_quality_growth_css_list_date_batch(
                        codes
                    ),
                )
                if artifact is None:
                    return _finalize_manifest(
                        store, checkpoint, as_of=as_of, generated_at=clock()
                    )
            session_index = {day: index for index, day in enumerate(sessions)}
            # Eligibility is evaluated for the executable next-session open,
            # never on the close-signal date itself.
            for decision_date, members in memberships.items():
                execution_date = sessions[session_index[decision_date] + 1]
                for instruments in _instrument_batches(members):
                    task = _task(
                        "css_state_batch",
                        decision_date=decision_date.isoformat(),
                        instrument_ids=list(instruments),
                        trading_date=execution_date.isoformat(),
                    )
                    artifact = capture(
                        task,
                        lambda codes=instruments, day=execution_date: provider.fetch_quality_growth_css_state_batch(
                            codes, day
                        ),
                    )
                    if artifact is None:
                        return _finalize_manifest(
                            store, checkpoint, as_of=as_of, generated_at=clock()
                        )
            for instrument_id in union_ids:
                for price_basis in PRICE_BASES:
                    task = _task(
                        "csd",
                        instrument_id=instrument_id,
                        start_date=PRICE_START_DATE.isoformat(),
                        end_date=cutoff_date.isoformat(),
                        price_basis=price_basis,
                    )
                    artifact = capture(
                        task,
                        lambda code=instrument_id, basis=price_basis: provider.fetch_quality_growth_csd(
                            code,
                            PRICE_START_DATE,
                            cutoff_date,
                            adjustment=basis,
                        ),
                    )
                    if artifact is None:
                        return _finalize_manifest(
                            store, checkpoint, as_of=as_of, generated_at=clock()
                        )

    except Exception as exc:
        session_task = _task("session", provider_id=PROVIDER_ID)
        _record_failure(store, checkpoint, session_task, exc)
    return _finalize_manifest(store, checkpoint, as_of=as_of, generated_at=clock())


def collect_choice_quality_growth_batch(
    *,
    provider: ChoiceProvider,
    cutoff_date: date,
    as_of: datetime,
    output_root: Path,
    resume: bool = False,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> ChoiceQualityGrowthBatchRun:
    """Capture one fixed batch while holding an exclusive checkpoint writer lock."""

    root = Path(output_root)
    with _exclusive_batch_lock(root):
        return _collect_choice_quality_growth_batch_locked(
            provider=provider,
            cutoff_date=cutoff_date,
            as_of=as_of,
            output_root=root,
            resume=resume,
            clock=clock,
        )


def verify_choice_quality_growth_batch(
    manifest_path: Path,
) -> ChoiceQualityGrowthBatchVerification:
    """Offline integrity check; success never changes the manifest's safety status."""

    path = _absolute_lexical(Path(manifest_path))
    try:
        root = path.parent.parent
        _guard_batch_path(root, path)
        manifest = _load_json(path)
        declared_hash = str(manifest.get("manifest_sha256", ""))
        core = dict(manifest)
        core.pop("manifest_sha256", None)
        if _SHA256.fullmatch(declared_hash) is None or sha256_bytes(
            canonical_json_bytes(core)
        ) != declared_hash:
            raise ChoiceQualityGrowthBatchError("manifest self-hash mismatch")
        if path.name != f"{declared_hash}.json":
            raise ChoiceQualityGrowthBatchError("manifest filename is not hash-bound")
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise ChoiceQualityGrowthBatchError("manifest schema version drifted")
        expected_manifest_fields = {
            "schema_version",
            "adapter_version",
            "provider_id",
            "status",
            "collection_status",
            "admission_status",
            "blocking_reasons",
            "as_of",
            "generated_at",
            "point_in_time_status",
            "source_authenticated",
            "raw_semantics",
            "integrity_semantics",
            "plan_path",
            "plan_sha256",
            "checkpoint_path",
            "checkpoint_sha256",
            "completeness",
            "missing_contract_items",
            "formal_truth_eligible",
            "paper_eligible",
            "trade_eligible",
            "real_money_candidate",
            "live_execution_status",
            "manifest_sha256",
        }
        if (
            set(manifest) != expected_manifest_fields
            or manifest.get("adapter_version") != ADAPTER_VERSION
            or manifest.get("provider_id") != PROVIDER_ID
        ):
            raise ChoiceQualityGrowthBatchError("manifest field contract drifted")
        as_of = _aware(
            datetime.fromisoformat(str(manifest.get("as_of", ""))), "manifest.as_of"
        )
        generated_at = _aware(
            datetime.fromisoformat(str(manifest.get("generated_at", ""))),
            "manifest.generated_at",
        )
        cutoff = date.fromisoformat(
            str(_load_json(_resolve_ref(root, manifest.get("plan_path")))["plan"]["cutoff_date"])
        )
        expected_plan = fixed_choice_quality_growth_plan(cutoff)
        if cutoff > as_of.astimezone(CHINA_TZ).date():
            raise ChoiceQualityGrowthBatchError("manifest cutoff is after as_of")
        if generated_at < as_of:
            raise ChoiceQualityGrowthBatchError("manifest generated_at precedes as_of")
        plan_envelope = _load_json(_resolve_ref(root, manifest.get("plan_path")))
        expected_plan_sha = sha256_bytes(canonical_json_bytes(expected_plan))
        if (
            plan_envelope != {"plan": expected_plan, "plan_sha256": expected_plan_sha}
            or manifest.get("plan_sha256") != expected_plan_sha
        ):
            raise ChoiceQualityGrowthBatchError("fixed plan binding mismatch")
        checkpoint_path = _resolve_ref(root, manifest.get("checkpoint_path"))
        checkpoint_raw = checkpoint_path.read_bytes()
        if (
            sha256_bytes(checkpoint_raw) != manifest.get("checkpoint_sha256")
            or checkpoint_path.name != f"{manifest.get('checkpoint_sha256')}.json"
        ):
            raise ChoiceQualityGrowthBatchError("checkpoint snapshot hash mismatch")
        checkpoint = _load_json(checkpoint_path)
        if (
            set(checkpoint)
            != {"schema_version", "plan_sha256", "completed", "failures"}
            or checkpoint.get("schema_version") != CHECKPOINT_SCHEMA_VERSION
            or checkpoint.get("plan_sha256") != expected_plan_sha
            or not isinstance(checkpoint.get("completed"), dict)
            or not isinstance(checkpoint.get("failures"), dict)
        ):
            raise ChoiceQualityGrowthBatchError("checkpoint plan binding mismatch")
        store = _BatchStore(root, expected_plan, read_only=True)
        store._validate_checkpoint(checkpoint)
        for task_id, reference in checkpoint.get("completed", {}).items():
            store.load_artifact(reference, expected_task_id=task_id)
        complete, summary, missing = _assess(store, checkpoint)
        expected_status = "blocked" if complete else "incomplete"
        expected_collection = "complete" if complete else "incomplete"
        expected_blockers = [PIT_BLOCKER, INDUSTRY_BLOCKER, CALENDAR_BLOCKER]
        if not complete:
            expected_blockers.insert(0, "incomplete_fixed_batch_contract")
        if (
            manifest.get("status") != expected_status
            or manifest.get("collection_status") != expected_collection
            or manifest.get("admission_status") != INDUSTRY_BLOCKER
            or manifest.get("blocking_reasons") != expected_blockers
            or manifest.get("completeness") != summary
            or manifest.get("missing_contract_items") != list(missing[:100])
        ):
            raise ChoiceQualityGrowthBatchError(
                "manifest completeness/status was not derived from stored artifacts"
            )
        if (
            manifest.get("formal_truth_eligible") is not False
            or manifest.get("paper_eligible") is not False
            or manifest.get("trade_eligible") is not False
            or manifest.get("real_money_candidate") is not False
            or manifest.get("live_execution_status") != LIVE_STATUS
            or manifest.get("point_in_time_status")
            != "historical_backfill_not_original_capture"
            or manifest.get("source_authenticated") is not False
            or manifest.get("raw_semantics") != RAW_SEMANTICS
            or manifest.get("integrity_semantics") != INTEGRITY_SEMANTICS
        ):
            raise ChoiceQualityGrowthBatchError("manifest safety boundary drifted")
        return ChoiceQualityGrowthBatchVerification(
            manifest_path=path,
            integrity_verified=True,
            status=expected_status,
            collection_status=expected_collection,
            reasons=tuple(expected_blockers),
        )
    except Exception as exc:
        return ChoiceQualityGrowthBatchVerification(
            manifest_path=path,
            integrity_verified=False,
            status="invalid",
            collection_status="invalid",
            reasons=(safe_error_text(exc),),
        )


__all__ = [
    "ADAPTER_VERSION",
    "CALENDAR_BLOCKER",
    "CSD_FIELDS",
    "CSS_FIELDS",
    "CSS_LIST_DATE_FIELDS",
    "CSS_STATE_FIELDS",
    "EXPECTED_MEMBERS_PER_DECISION",
    "INDUSTRY_BLOCKER",
    "LIVE_STATUS",
    "PRICE_BASES",
    "REBALANCE_ANCHOR_DATE",
    "SCHEMA_VERSION",
    "SECTOR_CODE",
    "ChoiceQualityGrowthBatchError",
    "ChoiceQualityGrowthBatchRun",
    "ChoiceQualityGrowthBatchVerification",
    "choice_quality_growth_plan_sha256",
    "collect_choice_quality_growth_batch",
    "derive_choice_quality_growth_decision_grid",
    "fixed_choice_quality_growth_plan",
    "verify_choice_quality_growth_batch",
]
