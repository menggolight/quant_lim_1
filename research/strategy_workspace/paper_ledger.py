"""Forward-only, append-only Paper ledger for the frozen A-share strategy.

The ledger is deliberately a newline-delimited hash chain instead of a mutable
JSON snapshot.  A header binds the historical Paper admission certificate and
the frozen research hashes, each decision binds its predecessor, and sealing
adds a final immutable record.  This module never submits, cancels, or
authorises an order: every fill must be manually confirmed and reconciled to a
broker-statement evidence hash.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence, TYPE_CHECKING

from .contracts import canonical_json_bytes, canonical_sha256

if TYPE_CHECKING:  # Avoid an admission -> ledger import cycle.
    from .admission import PaperAdmissionCertificate, PaperTrackRecord


PAPER_LEDGER_VERSION = "strategy-workspace-paper-ledger.v1"
PAPER_LEDGER_STATUS = "forward_paper_append_only_not_live"
PAPER_LEDGER_PRODUCER = "controlled-paper-ledger.v1"
MIDEA_INSTRUMENT_ID = "000333.SZ"
MIDEA_QUANTITY = 100
CHINA_STANDARD_TIME = timezone(timedelta(hours=8))
ZERO = Decimal("0")
CENT = Decimal("0.01")
PCT = Decimal("0.00000001")
COMMISSION_RATE = Decimal("0.00018")
MINIMUM_COMMISSION = Decimal("5")
SELL_TAX_RATE = Decimal("0.0005")
TRANSFER_FEE_RATE = Decimal("0.00001")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_INSTRUMENT_RE = re.compile(r"^[0-9A-Z][0-9A-Z.]{2,31}$")


class PaperLedgerError(ValueError):
    """Raised when forward Paper evidence is incomplete or inconsistent."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PaperLedgerError(f"{field_name} must be a timezone-aware datetime")
    return value


def _hash(value: Any, field_name: str) -> str:
    result = str(value).strip()
    if _SHA256_RE.fullmatch(result) is None:
        raise PaperLedgerError(f"{field_name} must be a lowercase SHA-256")
    return result


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PaperLedgerError(f"{field_name} must be non-empty text")
    return value.strip()


def _instrument(value: Any) -> str:
    result = _text(value, "instrument_id").upper()
    if _INSTRUMENT_RE.fullmatch(result) is None:
        raise PaperLedgerError("instrument_id is invalid")
    return result


def _money(value: Any, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise PaperLedgerError(f"{field_name} must be decimal") from exc
    if not result.is_finite():
        raise PaperLedgerError(f"{field_name} must be finite")
    return result.quantize(CENT, rounding=ROUND_HALF_UP)


def _decimal(value: Any, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise PaperLedgerError(f"{field_name} must be decimal") from exc
    if not result.is_finite():
        raise PaperLedgerError(f"{field_name} must be finite")
    return result


def _sha_line(value: Mapping[str, Any]) -> str:
    return canonical_sha256(value)


def _completed_months(start: date, end: date) -> int:
    months = (end.year - start.year) * 12 + end.month - start.month
    if end.day < start.day:
        months -= 1
    return months


def _month_range(start: date, end: date) -> tuple[tuple[int, int], ...]:
    current_year, current_month = start.year, start.month
    result: list[tuple[int, int]] = []
    while (current_year, current_month) <= (end.year, end.month):
        result.append((current_year, current_month))
        if current_month == 12:
            current_year, current_month = current_year + 1, 1
        else:
            current_month += 1
    return tuple(result)


def _strict_object(raw: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise PaperLedgerError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=pairs)
    except PaperLedgerError:
        raise
    except Exception as exc:
        raise PaperLedgerError("ledger contains invalid JSON") from exc
    if not isinstance(value, dict):
        raise PaperLedgerError("each ledger line must be a JSON object")
    return value


def _keys(value: Mapping[str, Any], expected: set[str], context: str) -> None:
    actual = set(value)
    if actual != expected:
        raise PaperLedgerError(
            f"{context} keys differ: missing={sorted(expected-actual)}, "
            f"unexpected={sorted(actual-expected)}"
        )


def _parse_date(value: Any, field_name: str) -> date:
    if not isinstance(value, str):
        raise PaperLedgerError(f"{field_name} must be an ISO date")
    try:
        result = date.fromisoformat(value)
    except ValueError as exc:
        raise PaperLedgerError(f"{field_name} must be an ISO date") from exc
    if result.isoformat() != value:
        raise PaperLedgerError(f"{field_name} must use canonical ISO format")
    return result


def _parse_datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise PaperLedgerError(f"{field_name} must be an ISO datetime")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PaperLedgerError(f"{field_name} must be an ISO datetime") from exc
    return _aware(result, field_name)


def _verify_certificate(certificate: Any) -> Any:
    from .admission import PAPER_ADMITTED_STATUS, PaperAdmissionCertificate

    if not isinstance(certificate, PaperAdmissionCertificate):
        raise PaperLedgerError("certificate must be a PaperAdmissionCertificate")
    if canonical_sha256(certificate.to_content_dict()) != certificate.certificate_sha256:
        raise PaperLedgerError("Paper admission certificate SHA-256 mismatch")
    if (
        certificate.status != PAPER_ADMITTED_STATUS
        or certificate.manual_execution_required is not True
        or certificate.live_supported is not False
        or certificate.execution_authority != "none"
    ):
        raise PaperLedgerError("certificate cannot authorize this Paper ledger")
    return certificate


@dataclass(frozen=True)
class PaperTarget:
    """One frozen model slot, including a forward-only manual veto."""

    slot: int
    instrument_id: str
    csi_level1_industry: str
    action: str
    model_target_quantity: int
    final_target_quantity: int
    lot_size: int
    predicted_return: Decimal
    percentile: Decimal
    target_weight: Decimal
    manual_veto: bool = False
    manual_veto_reason: str | None = None
    reserved_cash: Decimal = ZERO

    def __post_init__(self) -> None:
        if type(self.slot) is not int or self.slot not in (1, 2):
            raise PaperLedgerError("target slot must be 1 or 2")
        object.__setattr__(self, "instrument_id", _instrument(self.instrument_id))
        object.__setattr__(
            self, "csi_level1_industry", _text(self.csi_level1_industry, "industry")
        )
        action = str(self.action).strip().upper()
        if action not in {"ENTER", "HOLD", "EXIT"}:
            raise PaperLedgerError("target action must be ENTER, HOLD, or EXIT")
        object.__setattr__(self, "action", action)
        if type(self.lot_size) is not int or self.lot_size <= 0:
            raise PaperLedgerError("lot_size must be positive")
        for name in ("model_target_quantity", "final_target_quantity"):
            quantity = getattr(self, name)
            if type(quantity) is not int or quantity < 0 or quantity % self.lot_size:
                raise PaperLedgerError(f"{name} must be a non-negative whole lot")
        prediction = _decimal(self.predicted_return, "predicted_return")
        percentile = _decimal(self.percentile, "percentile")
        weight = _decimal(self.target_weight, "target_weight")
        reserved = _money(self.reserved_cash, "reserved_cash")
        if not ZERO <= percentile <= Decimal("1"):
            raise PaperLedgerError("percentile must be between zero and one")
        if not ZERO <= weight <= Decimal("0.4"):
            raise PaperLedgerError("target_weight must be between zero and 0.4")
        if action == "ENTER" and not (
            prediction > ZERO and percentile >= Decimal("0.95")
        ):
            raise PaperLedgerError("ENTER requires positive prediction and top 5%")
        if action == "HOLD" and not (
            prediction > ZERO and percentile >= Decimal("0.80")
        ):
            raise PaperLedgerError("HOLD requires positive prediction and top 20%")
        if action == "EXIT" and self.final_target_quantity != 0:
            raise PaperLedgerError("EXIT final target must be zero")
        if self.manual_veto:
            if self.final_target_quantity != 0 or self.model_target_quantity <= 0:
                raise PaperLedgerError("manual veto must turn a positive model target to cash")
            if not self.manual_veto_reason or reserved <= ZERO:
                raise PaperLedgerError("manual veto requires a reason and reserved cash")
            if weight != ZERO:
                raise PaperLedgerError("manual-vetoed target weight must be zero")
        else:
            if self.manual_veto_reason is not None or reserved != ZERO:
                raise PaperLedgerError("non-veto targets cannot carry veto fields")
            if action != "EXIT" and self.final_target_quantity != self.model_target_quantity:
                raise PaperLedgerError("manual quantity overrides are forbidden")
        object.__setattr__(self, "predicted_return", prediction)
        object.__setattr__(self, "percentile", percentile)
        object.__setattr__(self, "target_weight", weight)
        object.__setattr__(self, "reserved_cash", reserved)

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True)
class PaperExecution:
    """One manually confirmed fill, partial fill, or explicit non-fill."""

    execution_id: str
    instrument_id: str
    side: str
    status: str
    requested_quantity: int
    filled_quantity: int
    execution_session: date
    executed_at: datetime | None = None
    reference_open: Decimal | None = None
    fill_price: Decimal | None = None
    commission: Decimal = ZERO
    sell_tax: Decimal = ZERO
    transfer_fee: Decimal = ZERO
    slippage_cost: Decimal = ZERO
    unfilled_reason: str | None = None
    broker_statement_sha256: str | None = None
    reconciliation_sha256: str | None = None
    execution_basis: str = "next_session_open_manual"
    manual_confirmed: bool = True
    auto_submitted: bool = False
    live_order_id: None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "execution_id", _text(self.execution_id, "execution_id"))
        object.__setattr__(self, "instrument_id", _instrument(self.instrument_id))
        side = str(self.side).strip().upper()
        status = str(self.status).strip().upper()
        if side not in {"BUY", "SELL"}:
            raise PaperLedgerError("execution side must be BUY or SELL")
        if status not in {"FILLED", "PARTIAL", "UNFILLED"}:
            raise PaperLedgerError("execution status is invalid")
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "status", status)
        if type(self.requested_quantity) is not int or self.requested_quantity <= 0:
            raise PaperLedgerError("requested_quantity must be positive")
        if type(self.filled_quantity) is not int or not 0 <= self.filled_quantity <= self.requested_quantity:
            raise PaperLedgerError("filled_quantity is invalid")
        if self.execution_basis != "next_session_open_manual":
            raise PaperLedgerError("execution basis is frozen to next-session manual open")
        if self.manual_confirmed is not True or self.auto_submitted is not False:
            raise PaperLedgerError("Paper executions must be manual and never auto-submitted")
        if self.live_order_id is not None:
            raise PaperLedgerError("LIVE order identifiers are forbidden")
        costs = {
            name: _money(getattr(self, name), name)
            for name in ("commission", "sell_tax", "transfer_fee", "slippage_cost")
        }
        if any(value < ZERO for value in costs.values()):
            raise PaperLedgerError("execution costs cannot be negative")
        for name, value in costs.items():
            object.__setattr__(self, name, value)
        if status == "UNFILLED":
            if self.filled_quantity != 0 or any(value != ZERO for value in costs.values()):
                raise PaperLedgerError("UNFILLED executions cannot contain fills or costs")
            if any(value is not None for value in (
                self.executed_at, self.reference_open, self.fill_price,
                self.broker_statement_sha256, self.reconciliation_sha256,
            )):
                raise PaperLedgerError("UNFILLED executions cannot contain fill evidence")
            if not self.unfilled_reason:
                raise PaperLedgerError("UNFILLED execution requires a reason")
            return
        if self.filled_quantity <= 0 or (status == "FILLED" and self.filled_quantity != self.requested_quantity):
            raise PaperLedgerError("filled status and quantity are inconsistent")
        if status == "PARTIAL" and (
            self.filled_quantity >= self.requested_quantity or not self.unfilled_reason
        ):
            raise PaperLedgerError("PARTIAL execution requires a residual non-fill reason")
        if status == "FILLED" and self.unfilled_reason is not None:
            raise PaperLedgerError("FILLED execution cannot have an unfilled reason")
        executed_at = _aware(self.executed_at, "executed_at")  # type: ignore[arg-type]
        if executed_at.astimezone(CHINA_STANDARD_TIME).date() != self.execution_session:
            raise PaperLedgerError("fill must occur on the declared execution session")
        local_time = executed_at.astimezone(CHINA_STANDARD_TIME).time().replace(tzinfo=None)
        if not time(9, 25) <= local_time <= time(9, 35):
            raise PaperLedgerError("manual Paper fill must reconcile to the opening window")
        reference = _decimal(self.reference_open, "reference_open")
        fill = _decimal(self.fill_price, "fill_price")
        if reference <= ZERO or fill <= ZERO:
            raise PaperLedgerError("fill and reference-open prices must be positive")
        statement = _hash(self.broker_statement_sha256, "broker_statement_sha256")
        reconciliation = _hash(self.reconciliation_sha256, "reconciliation_sha256")
        notional = _money(fill * self.filled_quantity, "notional")
        expected_commission = max(
            MINIMUM_COMMISSION,
            _money(notional * COMMISSION_RATE, "expected commission"),
        )
        expected_tax = (
            _money(notional * SELL_TAX_RATE, "expected sell tax")
            if side == "SELL" else ZERO
        )
        expected_transfer = _money(notional * TRANSFER_FEE_RATE, "expected transfer")
        expected_slippage = _money(
            abs(fill - reference) * self.filled_quantity, "expected slippage"
        )
        if costs["commission"] != expected_commission:
            raise PaperLedgerError("commission does not match the frozen fee schedule")
        if costs["sell_tax"] != expected_tax:
            raise PaperLedgerError("sell tax does not match the frozen fee schedule")
        if costs["transfer_fee"] != expected_transfer:
            raise PaperLedgerError("transfer fee does not match the frozen fee schedule")
        if costs["slippage_cost"] != expected_slippage:
            raise PaperLedgerError("slippage cost does not reconcile to fill versus open")
        object.__setattr__(self, "executed_at", executed_at)
        object.__setattr__(self, "reference_open", reference)
        object.__setattr__(self, "fill_price", fill)
        object.__setattr__(self, "broker_statement_sha256", statement)
        object.__setattr__(self, "reconciliation_sha256", reconciliation)

    @property
    def notional(self) -> Decimal:
        if self.filled_quantity == 0 or self.fill_price is None:
            return ZERO
        return _money(self.fill_price * self.filled_quantity, "notional")

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True)
class PaperPosition:
    instrument_id: str
    csi_level1_industry: str
    quantity: int
    close_price: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument_id", _instrument(self.instrument_id))
        object.__setattr__(self, "csi_level1_industry", _text(self.csi_level1_industry, "industry"))
        if type(self.quantity) is not int or self.quantity <= 0:
            raise PaperLedgerError("position quantity must be positive")
        price = _decimal(self.close_price, "close_price")
        if price <= ZERO:
            raise PaperLedgerError("close_price must be positive")
        object.__setattr__(self, "close_price", price)

    @property
    def market_value(self) -> Decimal:
        return _money(self.close_price * self.quantity, "market_value")

    def to_dict(self) -> dict[str, Any]:
        result = {name: getattr(self, name) for name in self.__dataclass_fields__}
        result["market_value"] = self.market_value
        return result


@dataclass(frozen=True)
class UnmanagedExternalMark:
    instrument_id: str
    quantity: int
    csi_level1_industry: str
    close_price: Decimal
    ownership: str = "unmanaged_external"

    def __post_init__(self) -> None:
        if _instrument(self.instrument_id) != MIDEA_INSTRUMENT_ID or self.quantity != MIDEA_QUANTITY:
            raise PaperLedgerError("Paper risk mark must contain exactly Midea 000333.SZ 100 shares")
        if self.ownership != "unmanaged_external":
            raise PaperLedgerError("Midea must remain unmanaged_external")
        object.__setattr__(self, "instrument_id", MIDEA_INSTRUMENT_ID)
        object.__setattr__(self, "csi_level1_industry", _text(self.csi_level1_industry, "industry"))
        price = _decimal(self.close_price, "external close_price")
        if price <= ZERO:
            raise PaperLedgerError("external close price must be positive")
        object.__setattr__(self, "close_price", price)

    @property
    def market_value(self) -> Decimal:
        return _money(self.close_price * self.quantity, "external market_value")

    def to_dict(self) -> dict[str, Any]:
        result = {name: getattr(self, name) for name in self.__dataclass_fields__}
        result["market_value"] = self.market_value
        return result


@dataclass(frozen=True)
class PaperDecisionDraft:
    decision_id: str
    decision_at: datetime
    data_available_at: datetime
    signal_generated_at: datetime
    signal_sha256: str
    model_result_sha256: str
    source_bundle_sha256: str
    targets: tuple[PaperTarget, ...]
    executions: tuple[PaperExecution, ...]
    positions: tuple[PaperPosition, ...]
    cash: Decimal
    external_midea: UnmanagedExternalMark
    risk_frozen: bool = False
    auto_submit: bool = False
    live_order_payload: None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_id", _text(self.decision_id, "decision_id"))
        decision_at = _aware(self.decision_at, "decision_at")
        data_at = _aware(self.data_available_at, "data_available_at")
        signal_at = _aware(self.signal_generated_at, "signal_generated_at")
        if not data_at <= signal_at <= decision_at:
            raise PaperLedgerError("PIT data and signal times must not exceed decision_at")
        local_decision = decision_at.astimezone(CHINA_STANDARD_TIME)
        if local_decision.time().replace(tzinfo=None) < time(15, 0):
            raise PaperLedgerError("Paper signal must be formed after the close")
        if self.auto_submit is not False or self.live_order_payload is not None:
            raise PaperLedgerError("LIVE and automatic submission are permanently unsupported")
        targets, executions, positions = tuple(self.targets), tuple(self.executions), tuple(self.positions)
        if any(not isinstance(item, PaperTarget) for item in targets):
            raise PaperLedgerError("targets must contain PaperTarget values")
        if any(not isinstance(item, PaperExecution) for item in executions):
            raise PaperLedgerError("executions must contain PaperExecution values")
        if any(not isinstance(item, PaperPosition) for item in positions):
            raise PaperLedgerError("positions must contain PaperPosition values")
        if len({item.slot for item in targets}) != len(targets) or len(targets) > 2:
            raise PaperLedgerError("target slots must be unique and capped at two")
        if len({item.instrument_id for item in targets}) != len(targets):
            raise PaperLedgerError("target instruments must be unique")
        if len({item.execution_id for item in executions}) != len(executions):
            raise PaperLedgerError("execution IDs must be unique")
        if len({item.instrument_id for item in positions}) != len(positions):
            raise PaperLedgerError("position instruments must be unique")
        approved = [item for item in targets if item.final_target_quantity > 0]
        if len({item.csi_level1_industry for item in approved}) != len(approved):
            raise PaperLedgerError("approved targets must use different CSI level-1 industries")
        cash = _money(self.cash, "cash")
        if cash < ZERO:
            raise PaperLedgerError("leverage is forbidden")
        if not isinstance(self.external_midea, UnmanagedExternalMark):
            raise PaperLedgerError("external_midea mark is required")
        object.__setattr__(self, "decision_at", decision_at)
        object.__setattr__(self, "data_available_at", data_at)
        object.__setattr__(self, "signal_generated_at", signal_at)
        object.__setattr__(self, "signal_sha256", _hash(self.signal_sha256, "signal_sha256"))
        object.__setattr__(self, "model_result_sha256", _hash(self.model_result_sha256, "model_result_sha256"))
        object.__setattr__(self, "source_bundle_sha256", _hash(self.source_bundle_sha256, "source_bundle_sha256"))
        object.__setattr__(self, "targets", targets)
        object.__setattr__(self, "executions", executions)
        object.__setattr__(self, "positions", positions)
        object.__setattr__(self, "cash", cash)


@dataclass(frozen=True)
class VerifiedPaperLedger:
    path: Path
    header: Mapping[str, Any]
    decisions: tuple[Mapping[str, Any], ...]
    seal: Mapping[str, Any] | None
    last_record_sha256: str
    file_sha256: str
    byte_length: int


@dataclass(frozen=True)
class PaperLedgerSummary:
    track_record: Any
    complete: bool
    reasons: tuple[str, ...]
    seal_sha256: str
    ledger_file_sha256: str
    decision_count: int
    completed_months: int
    distinct_decision_months: int
    missing_decision_months: tuple[str, ...]
    live_supported: bool = False
    execution_authority: str = "none"

    def __post_init__(self) -> None:
        if self.live_supported is not False or self.execution_authority != "none":
            raise PaperLedgerError("Paper summary can never authorize LIVE")


def _header_content(certificate: Any, calendar: Sequence[date], initial_cash: Decimal, created_at: datetime) -> dict[str, Any]:
    return {
        "schema_version": PAPER_LEDGER_VERSION,
        "producer": PAPER_LEDGER_PRODUCER,
        "status": PAPER_LEDGER_STATUS,
        "created_at": created_at,
        "paper_certificate_sha256": certificate.certificate_sha256,
        "experiment_sha256": certificate.experiment_sha256,
        "configuration_sha256": certificate.configuration_sha256,
        "data_sha256": certificate.data_sha256,
        "code_sha256": certificate.code_sha256,
        "choice_receipt_sha256": certificate.choice_receipt_sha256,
        "evaluation_sha256": certificate.evaluation_sha256,
        "backtest_sha256": certificate.backtest_sha256,
        "top_decile_result_sha256": certificate.top_decile_result_sha256,
        "historical_gate_sha256": certificate.historical_gate_sha256,
        "controlled_trading_dates": tuple(calendar),
        "controlled_calendar_sha256": canonical_sha256(tuple(calendar)),
        "initial_cash": initial_cash,
        "manual_execution_required": True,
        "auto_submit": False,
        "live_supported": False,
        "execution_authority": "none",
    }


def _write_new_line(path: Path, record: Mapping[str, Any], *, exclusive: bool = False, expected_size: int | None = None) -> None:
    data = canonical_json_bytes(record) + b"\n"
    flags = os.O_WRONLY | os.O_BINARY
    flags |= os.O_CREAT | os.O_EXCL if exclusive else os.O_APPEND
    if not exclusive and expected_size is not None and path.stat().st_size != expected_size:
        raise PaperLedgerError("ledger changed concurrently before append")
    try:
        descriptor = os.open(str(path), flags, 0o600)
    except FileExistsError as exc:
        raise PaperLedgerError("Paper ledger already exists") from exc
    try:
        written = os.write(descriptor, data)
        if written != len(data):
            raise PaperLedgerError("short write while appending Paper ledger")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def create_or_verify_paper_ledger(
    path: str | Path,
    certificate: "PaperAdmissionCertificate",
    *,
    controlled_trading_dates: Sequence[date],
    initial_cash: Decimal = Decimal("10000"),
) -> VerifiedPaperLedger:
    """Create a ledger once, or verify that an existing header matches exactly."""

    certificate = _verify_certificate(certificate)
    target = Path(path)
    calendar = tuple(controlled_trading_dates)
    if (
        not calendar
        or any(not isinstance(item, date) or isinstance(item, datetime) for item in calendar)
        or tuple(sorted(calendar)) != calendar
        or len(set(calendar)) != len(calendar)
    ):
        raise PaperLedgerError("controlled trading calendar must be chronological and unique")
    cash = _money(initial_cash, "initial_cash")
    if cash != Decimal("10000.00"):
        raise PaperLedgerError("V1 Paper initial cash is frozen to CNY 10,000")
    if target.exists():
        verified = verify_paper_ledger(target, certificate=certificate)
        expected = _header_content(
            certificate,
            calendar,
            cash,
            _parse_datetime(verified.header["created_at"], "created_at"),
        )
        if dict(verified.header) != json.loads(canonical_json_bytes(expected)):
            raise PaperLedgerError("existing Paper ledger header does not match requested contract")
        return verified
    if not target.parent.exists() or not target.parent.is_dir():
        raise PaperLedgerError("Paper ledger parent directory must already exist")
    created_at = _now()
    if created_at < certificate.issued_at:
        raise PaperLedgerError("ledger cannot be created before its Paper certificate")
    content = _header_content(certificate, calendar, cash, created_at)
    record = {
        "record_type": "header",
        "content": content,
        "record_sha256": _sha_line({"record_type": "header", "content": content}),
    }
    _write_new_line(target, record, exclusive=True)
    return verify_paper_ledger(target, certificate=certificate)


def _header_from_record(record: Mapping[str, Any], certificate: Any | None) -> tuple[dict[str, Any], str]:
    _keys(record, {"record_type", "content", "record_sha256"}, "header record")
    if record["record_type"] != "header" or not isinstance(record["content"], dict):
        raise PaperLedgerError("first ledger record must be a header")
    content = record["content"]
    expected_keys = {
        "schema_version", "producer", "status", "created_at",
        "paper_certificate_sha256", "experiment_sha256", "configuration_sha256",
        "data_sha256", "code_sha256", "choice_receipt_sha256", "evaluation_sha256",
        "backtest_sha256", "top_decile_result_sha256", "historical_gate_sha256",
        "controlled_trading_dates", "controlled_calendar_sha256", "initial_cash",
        "manual_execution_required", "auto_submit", "live_supported", "execution_authority",
    }
    _keys(content, expected_keys, "header content")
    if content["schema_version"] != PAPER_LEDGER_VERSION or content["producer"] != PAPER_LEDGER_PRODUCER or content["status"] != PAPER_LEDGER_STATUS:
        raise PaperLedgerError("Paper ledger header version or status is unsupported")
    if content["manual_execution_required"] is not True or content["auto_submit"] is not False or content["live_supported"] is not False or content["execution_authority"] != "none":
        raise PaperLedgerError("Paper ledger safety boundary was altered")
    for name in (
        "paper_certificate_sha256", "experiment_sha256", "configuration_sha256",
        "data_sha256", "code_sha256", "choice_receipt_sha256", "evaluation_sha256",
        "backtest_sha256", "top_decile_result_sha256", "historical_gate_sha256",
        "controlled_calendar_sha256",
    ):
        _hash(content[name], name)
    calendar_raw = content["controlled_trading_dates"]
    if not isinstance(calendar_raw, list):
        raise PaperLedgerError("controlled_trading_dates must be an array")
    calendar = tuple(_parse_date(item, "controlled trading date") for item in calendar_raw)
    if not calendar or tuple(sorted(calendar)) != calendar or len(set(calendar)) != len(calendar):
        raise PaperLedgerError("controlled trading calendar is invalid")
    if canonical_sha256(calendar) != content["controlled_calendar_sha256"]:
        raise PaperLedgerError("controlled trading calendar SHA-256 mismatch")
    _parse_datetime(content["created_at"], "created_at")
    if _money(content["initial_cash"], "initial_cash") != Decimal("10000.00"):
        raise PaperLedgerError("Paper initial cash drifted from CNY 10,000")
    expected_hash = _sha_line({"record_type": "header", "content": content})
    if record["record_sha256"] != expected_hash:
        raise PaperLedgerError("Paper ledger header SHA-256 mismatch")
    if certificate is not None:
        certificate = _verify_certificate(certificate)
        bindings = {
            "paper_certificate_sha256": certificate.certificate_sha256,
            "experiment_sha256": certificate.experiment_sha256,
            "configuration_sha256": certificate.configuration_sha256,
            "data_sha256": certificate.data_sha256,
            "code_sha256": certificate.code_sha256,
            "choice_receipt_sha256": certificate.choice_receipt_sha256,
            "evaluation_sha256": certificate.evaluation_sha256,
            "backtest_sha256": certificate.backtest_sha256,
            "top_decile_result_sha256": certificate.top_decile_result_sha256,
            "historical_gate_sha256": certificate.historical_gate_sha256,
        }
        if any(content[name] != value for name, value in bindings.items()):
            raise PaperLedgerError("Paper ledger certificate binding mismatch")
    return content, expected_hash


def _validate_decision_content(
    content: Mapping[str, Any], header: Mapping[str, Any], previous: Mapping[str, Any] | None
) -> None:
    expected = {
        "decision_id", "decision_at", "decision_date", "execution_session", "recorded_at",
        "data_available_at", "signal_generated_at", "signal_sha256", "model_result_sha256",
        "source_bundle_sha256", "paper_certificate_sha256", "experiment_sha256",
        "configuration_sha256", "data_sha256", "code_sha256", "controlled_calendar_sha256",
        "decision_calendar_index", "execution_calendar_index", "targets", "executions",
        "positions", "cash", "strategy_positions_value", "strategy_nav", "external_midea",
        "combined_account_value", "peak_nav", "drawdown", "total_transaction_cost",
        "risk_invariants", "risk_frozen", "manual_execution_required", "auto_submit",
        "live_supported", "execution_authority", "live_order_payload",
    }
    _keys(content, expected, "decision content")
    if content["manual_execution_required"] is not True or content["auto_submit"] is not False or content["live_supported"] is not False or content["execution_authority"] != "none" or content["live_order_payload"] is not None:
        raise PaperLedgerError("decision safety boundary was altered")
    for name in (
        "paper_certificate_sha256", "experiment_sha256", "configuration_sha256",
        "data_sha256", "code_sha256", "controlled_calendar_sha256",
    ):
        if content[name] != header[name]:
            raise PaperLedgerError(f"decision {name} drifted from header")
    decision_at = _parse_datetime(content["decision_at"], "decision_at")
    decision_date = _parse_date(content["decision_date"], "decision_date")
    execution_session = _parse_date(content["execution_session"], "execution_session")
    recorded_at = _parse_datetime(content["recorded_at"], "recorded_at")
    data_at = _parse_datetime(content["data_available_at"], "data_available_at")
    signal_at = _parse_datetime(content["signal_generated_at"], "signal_generated_at")
    created_at = _parse_datetime(header["created_at"], "header created_at")
    if not created_at <= data_at <= signal_at <= decision_at < recorded_at:
        raise PaperLedgerError("decision violates forward-only timestamp ordering")
    if decision_at.astimezone(CHINA_STANDARD_TIME).date() != decision_date:
        raise PaperLedgerError("decision_date does not match decision_at")
    if recorded_at.astimezone(CHINA_STANDARD_TIME).date() != execution_session or recorded_at.astimezone(CHINA_STANDARD_TIME).time().replace(tzinfo=None) < time(15, 0):
        raise PaperLedgerError("decision must be appended after the execution-session close")
    calendar = tuple(_parse_date(item, "calendar date") for item in header["controlled_trading_dates"])
    decision_index = content["decision_calendar_index"]
    execution_index = content["execution_calendar_index"]
    if type(decision_index) is not int or type(execution_index) is not int:
        raise PaperLedgerError("calendar indexes must be integers")
    if not 0 <= decision_index < len(calendar) or execution_index != decision_index + 1:
        raise PaperLedgerError("execution must be the next controlled trading session")
    if calendar[decision_index] != decision_date or calendar[execution_index] != execution_session:
        raise PaperLedgerError("decision calendar references are inconsistent")
    if previous is not None:
        previous_index = previous["decision_calendar_index"]
        if decision_index - previous_index != 20:
            raise PaperLedgerError("Paper decisions must be exactly 20 controlled sessions apart")
        if recorded_at <= _parse_datetime(previous["recorded_at"], "previous recorded_at"):
            raise PaperLedgerError("Paper decisions must be appended chronologically")
        if previous["risk_frozen"] is True:
            raise PaperLedgerError("no decisions may follow a frozen Paper record")
    for name in ("signal_sha256", "model_result_sha256", "source_bundle_sha256"):
        _hash(content[name], name)
    if not isinstance(content["targets"], list) or not isinstance(content["executions"], list) or not isinstance(content["positions"], list):
        raise PaperLedgerError("targets, executions, and positions must be arrays")
    if not isinstance(content["external_midea"], dict) or content["external_midea"].get("instrument_id") != MIDEA_INSTRUMENT_ID or content["external_midea"].get("quantity") != MIDEA_QUANTITY or content["external_midea"].get("ownership") != "unmanaged_external":
        raise PaperLedgerError("Midea unmanaged external mark is missing")
    if _decimal(content["drawdown"], "drawdown") < ZERO:
        raise PaperLedgerError("drawdown cannot be negative")
    if not isinstance(content["risk_frozen"], bool):
        raise PaperLedgerError("risk_frozen must be boolean")
    invariants = content["risk_invariants"]
    _keys(invariants, {"max_two_positions", "max_stock_weight", "minimum_cash", "distinct_industries", "combined_industry_addition_cap", "no_leverage_or_short"}, "risk invariants")
    if any(type(value) is not bool for value in invariants.values()):
        raise PaperLedgerError("risk invariants must be booleans")
    if _decimal(content["drawdown"], "drawdown") >= Decimal("0.12") and content["risk_frozen"] is not True:
        raise PaperLedgerError("12% Paper drawdown must freeze the ledger")


def _target_from_payload(value: Any) -> PaperTarget:
    if not isinstance(value, dict):
        raise PaperLedgerError("persisted Paper target must be an object")
    _keys(value, set(PaperTarget.__dataclass_fields__), "persisted target")
    return PaperTarget(
        slot=value["slot"],
        instrument_id=value["instrument_id"],
        csi_level1_industry=value["csi_level1_industry"],
        action=value["action"],
        model_target_quantity=value["model_target_quantity"],
        final_target_quantity=value["final_target_quantity"],
        lot_size=value["lot_size"],
        predicted_return=_decimal(value["predicted_return"], "predicted_return"),
        percentile=_decimal(value["percentile"], "percentile"),
        target_weight=_decimal(value["target_weight"], "target_weight"),
        manual_veto=value["manual_veto"],
        manual_veto_reason=value["manual_veto_reason"],
        reserved_cash=_money(value["reserved_cash"], "reserved_cash"),
    )


def _execution_from_payload(value: Any) -> PaperExecution:
    if not isinstance(value, dict):
        raise PaperLedgerError("persisted Paper execution must be an object")
    _keys(
        value,
        set(PaperExecution.__dataclass_fields__) | {"notional"},
        "persisted execution",
    )
    execution = PaperExecution(
        execution_id=value["execution_id"],
        instrument_id=value["instrument_id"],
        side=value["side"],
        status=value["status"],
        requested_quantity=value["requested_quantity"],
        filled_quantity=value["filled_quantity"],
        execution_session=_parse_date(value["execution_session"], "execution_session"),
        executed_at=(
            _parse_datetime(value["executed_at"], "executed_at")
            if value["executed_at"] is not None else None
        ),
        reference_open=(
            _decimal(value["reference_open"], "reference_open")
            if value["reference_open"] is not None else None
        ),
        fill_price=(
            _decimal(value["fill_price"], "fill_price")
            if value["fill_price"] is not None else None
        ),
        commission=_money(value["commission"], "commission"),
        sell_tax=_money(value["sell_tax"], "sell_tax"),
        transfer_fee=_money(value["transfer_fee"], "transfer_fee"),
        slippage_cost=_money(value["slippage_cost"], "slippage_cost"),
        unfilled_reason=value["unfilled_reason"],
        broker_statement_sha256=value["broker_statement_sha256"],
        reconciliation_sha256=value["reconciliation_sha256"],
        execution_basis=value["execution_basis"],
        manual_confirmed=value["manual_confirmed"],
        auto_submitted=value["auto_submitted"],
        live_order_id=value["live_order_id"],
    )
    if _money(value["notional"], "persisted notional") != execution.notional:
        raise PaperLedgerError("persisted execution notional does not reconcile")
    return execution


def _position_from_payload(value: Any) -> PaperPosition:
    if not isinstance(value, dict):
        raise PaperLedgerError("persisted Paper position must be an object")
    _keys(
        value,
        set(PaperPosition.__dataclass_fields__) | {"market_value"},
        "persisted position",
    )
    position = PaperPosition(
        instrument_id=value["instrument_id"],
        csi_level1_industry=value["csi_level1_industry"],
        quantity=value["quantity"],
        close_price=_decimal(value["close_price"], "close_price"),
    )
    if _money(value["market_value"], "persisted market value") != position.market_value:
        raise PaperLedgerError("persisted position market value does not reconcile")
    return position


def _external_from_payload(value: Any) -> UnmanagedExternalMark:
    if not isinstance(value, dict):
        raise PaperLedgerError("persisted external mark must be an object")
    _keys(
        value,
        set(UnmanagedExternalMark.__dataclass_fields__) | {"market_value"},
        "persisted external mark",
    )
    mark = UnmanagedExternalMark(
        instrument_id=value["instrument_id"],
        quantity=value["quantity"],
        csi_level1_industry=value["csi_level1_industry"],
        close_price=_decimal(value["close_price"], "external close_price"),
        ownership=value["ownership"],
    )
    if _money(value["market_value"], "external market value") != mark.market_value:
        raise PaperLedgerError("persisted external market value does not reconcile")
    return mark


def _replay_persisted_decision(
    content: Mapping[str, Any],
    header: Mapping[str, Any],
    prior: Sequence[Mapping[str, Any]],
) -> None:
    """Recompute a persisted decision; hashes alone are never treated as proof."""

    if any(item["decision_id"] == content["decision_id"] for item in prior):
        raise PaperLedgerError("Paper decision_id replay is forbidden")
    targets = tuple(_target_from_payload(item) for item in content["targets"])
    executions = tuple(_execution_from_payload(item) for item in content["executions"])
    positions = tuple(_position_from_payload(item) for item in content["positions"])
    external = _external_from_payload(content["external_midea"])
    if len({item.slot for item in targets}) != len(targets) or len(targets) > 2:
        raise PaperLedgerError("persisted target slots are invalid")
    if len({item.instrument_id for item in targets}) != len(targets):
        raise PaperLedgerError("persisted target instruments are duplicated")
    if len({item.execution_id for item in executions}) != len(executions):
        raise PaperLedgerError("persisted execution IDs are duplicated")
    if len({item.instrument_id for item in positions}) != len(positions):
        raise PaperLedgerError("persisted position instruments are duplicated")
    approved = [item for item in targets if item.final_target_quantity > 0]
    if len({item.csi_level1_industry for item in approved}) != len(approved):
        raise PaperLedgerError("persisted approved targets repeat an industry")
    if any(item.instrument_id == MIDEA_INSTRUMENT_ID for item in targets + executions + positions):
        raise PaperLedgerError("Midea cannot be a strategy target, fill, or position")

    previous_positions_raw = prior[-1]["positions"] if prior else []
    previous_positions = {
        item["instrument_id"]: int(item["quantity"])
        for item in previous_positions_raw
    }
    previous_industries = {
        item["instrument_id"]: str(item["csi_level1_industry"])
        for item in previous_positions_raw
    }
    quantities = dict(previous_positions)
    cash = (
        _money(prior[-1]["cash"], "previous cash")
        if prior else _money(header["initial_cash"], "initial cash")
    )
    total_cost = ZERO
    target_map = {item.instrument_id: item for item in targets}
    for execution in executions:
        target = target_map.get(execution.instrument_id)
        previous_quantity = previous_positions.get(execution.instrument_id, 0)
        desired = target.final_target_quantity if target is not None else 0
        if execution.side == "BUY":
            if target is None or target.manual_veto or desired <= previous_quantity or execution.filled_quantity > desired - previous_quantity:
                raise PaperLedgerError("persisted BUY is unsupported by its target")
        elif execution.instrument_id not in previous_positions or desired >= previous_quantity or execution.filled_quantity > previous_quantity - desired:
            raise PaperLedgerError("persisted SELL is unsupported by its target")
        if execution.filled_quantity:
            signed = execution.filled_quantity if execution.side == "BUY" else -execution.filled_quantity
            quantities[execution.instrument_id] = quantities.get(execution.instrument_id, 0) + signed
            if quantities[execution.instrument_id] < 0:
                raise PaperLedgerError("persisted execution creates a short position")
            if quantities[execution.instrument_id] == 0:
                del quantities[execution.instrument_id]
            fees = execution.commission + execution.sell_tax + execution.transfer_fee
            cash += (
                -execution.notional - fees
                if execution.side == "BUY"
                else execution.notional - fees
            )
            total_cost += fees + execution.slippage_cost
    snapshot = {item.instrument_id: item.quantity for item in positions}
    if snapshot != quantities:
        raise PaperLedgerError("persisted position snapshot does not reconcile")
    if _money(cash, "replayed cash") != _money(content["cash"], "persisted cash"):
        raise PaperLedgerError("persisted cash does not reconcile")
    for position in positions:
        expected_industry = (
            target_map[position.instrument_id].csi_level1_industry
            if position.instrument_id in target_map
            else previous_industries.get(position.instrument_id)
        )
        if expected_industry is None or position.csi_level1_industry != expected_industry:
            raise PaperLedgerError("persisted position industry does not reconcile")
    positions_value = sum((item.market_value for item in positions), ZERO)
    strategy_nav = _money(cash + positions_value, "replayed strategy NAV")
    peak_nav = max(
        [Decimal("10000.00")]
        + [_money(item["strategy_nav"], "prior NAV") for item in prior]
        + [strategy_nav]
    )
    drawdown = ((peak_nav - strategy_nav) / peak_nav).quantize(PCT) if peak_nav else ZERO
    combined_value = strategy_nav + external.market_value
    industry_values: dict[str, Decimal] = {}
    for position in positions:
        industry_values[position.csi_level1_industry] = industry_values.get(position.csi_level1_industry, ZERO) + position.market_value
    industry_values[external.csi_level1_industry] = industry_values.get(external.csi_level1_industry, ZERO) + external.market_value
    strategy_industries = {item.csi_level1_industry for item in positions}
    calculated_invariants = {
        "max_two_positions": len(positions) <= 2,
        "max_stock_weight": all(item.market_value / strategy_nav <= Decimal("0.4") for item in positions) if strategy_nav else False,
        "minimum_cash": strategy_nav > ZERO and cash / strategy_nav >= Decimal("0.2"),
        "distinct_industries": len(strategy_industries) == len(positions),
        "combined_industry_addition_cap": all(
            industry not in strategy_industries or value / combined_value <= Decimal("0.45")
            for industry, value in industry_values.items()
        ),
        "no_leverage_or_short": cash >= ZERO and all(item.quantity > 0 for item in positions),
    }
    expected_values = {
        "strategy_positions_value": _money(positions_value, "positions value"),
        "strategy_nav": strategy_nav,
        "combined_account_value": _money(combined_value, "combined value"),
        "peak_nav": _money(peak_nav, "peak NAV"),
        "total_transaction_cost": _money(total_cost, "transaction cost"),
    }
    for name, expected in expected_values.items():
        if _money(content[name], name) != expected:
            raise PaperLedgerError(f"persisted {name} does not reconcile")
    if _decimal(content["drawdown"], "drawdown") != drawdown:
        raise PaperLedgerError("persisted drawdown does not reconcile")
    if dict(content["risk_invariants"]) != calculated_invariants:
        raise PaperLedgerError("persisted risk invariants do not reconcile")


def verify_paper_ledger(
    path: str | Path,
    *,
    certificate: "PaperAdmissionCertificate | None" = None,
    as_of: datetime | None = None,
) -> VerifiedPaperLedger:
    """Verify JSON syntax, strict schemas, the hash chain, and forward ordering."""

    target = Path(path)
    if not target.is_file() or target.is_symlink():
        raise PaperLedgerError("Paper ledger must be a regular non-symlink file")
    raw = target.read_bytes()
    if not raw or not raw.endswith(b"\n"):
        raise PaperLedgerError("Paper ledger must end with one complete newline record")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise PaperLedgerError("Paper ledger must be UTF-8") from exc
    if not lines or any(not line for line in lines):
        raise PaperLedgerError("Paper ledger cannot contain blank records")
    records = tuple(_strict_object(line) for line in lines)
    header, previous_hash = _header_from_record(records[0], certificate)
    effective_as_of = _aware(as_of, "as_of") if as_of is not None else _now()
    if _parse_datetime(header["created_at"], "created_at") > effective_as_of:
        raise PaperLedgerError("Paper ledger header is from the future")
    decisions: list[Mapping[str, Any]] = []
    seal: Mapping[str, Any] | None = None
    previous_content: Mapping[str, Any] | None = None
    for index, record in enumerate(records[1:], 1):
        _keys(record, {"record_type", "previous_record_sha256", "content", "record_sha256"}, f"record {index}")
        if record["previous_record_sha256"] != previous_hash:
            raise PaperLedgerError("Paper ledger hash chain is broken")
        if not isinstance(record["content"], dict):
            raise PaperLedgerError("record content must be an object")
        expected_hash = _sha_line({
            "record_type": record["record_type"],
            "previous_record_sha256": previous_hash,
            "content": record["content"],
        })
        if record["record_sha256"] != expected_hash:
            raise PaperLedgerError("Paper ledger record SHA-256 mismatch")
        if record["record_type"] == "decision":
            if seal is not None:
                raise PaperLedgerError("no decision may follow a seal")
            _validate_decision_content(record["content"], header, previous_content)
            _replay_persisted_decision(record["content"], header, decisions)
            if _parse_datetime(record["content"]["recorded_at"], "recorded_at") > effective_as_of:
                raise PaperLedgerError("Paper ledger contains a future event")
            decisions.append(record["content"])
            previous_content = record["content"]
        elif record["record_type"] == "seal":
            if seal is not None or index != len(records) - 1:
                raise PaperLedgerError("Paper ledger may contain only one final seal")
            content = record["content"]
            _keys(content, {"sealed_at", "reason", "decision_count", "last_decision_sha256", "manual_execution_required", "auto_submit", "live_supported", "execution_authority"}, "seal content")
            sealed_at = _parse_datetime(content["sealed_at"], "sealed_at")
            if sealed_at > effective_as_of:
                raise PaperLedgerError("Paper ledger seal is from the future")
            if not decisions or content["decision_count"] != len(decisions) or content["last_decision_sha256"] != previous_hash:
                raise PaperLedgerError("Paper ledger seal does not bind all decisions")
            if content["reason"] not in {"completed_forward_paper", "risk_freeze", "terminated"}:
                raise PaperLedgerError("Paper ledger seal reason is invalid")
            if content["manual_execution_required"] is not True or content["auto_submit"] is not False or content["live_supported"] is not False or content["execution_authority"] != "none":
                raise PaperLedgerError("Paper ledger seal safety boundary was altered")
            seal = content
        else:
            raise PaperLedgerError("unknown Paper ledger record type")
        previous_hash = expected_hash
    return VerifiedPaperLedger(
        path=target,
        header=header,
        decisions=tuple(decisions),
        seal=seal,
        last_record_sha256=previous_hash,
        file_sha256=sha256(raw).hexdigest(),
        byte_length=len(raw),
    )


def _execution_payload(execution: PaperExecution) -> dict[str, Any]:
    result = execution.to_dict()
    result["notional"] = execution.notional
    return result


def append_paper_decision(
    path: str | Path,
    certificate: "PaperAdmissionCertificate",
    draft: PaperDecisionDraft,
    *,
    expected_previous_sha256: str | None = None,
) -> VerifiedPaperLedger:
    """Append exactly one reconciled, next-session Paper decision."""

    certificate = _verify_certificate(certificate)
    if not isinstance(draft, PaperDecisionDraft):
        raise PaperLedgerError("draft must be a PaperDecisionDraft")
    recorded_at = _now()
    current = verify_paper_ledger(path, certificate=certificate, as_of=recorded_at)
    if current.seal is not None:
        raise PaperLedgerError("sealed Paper ledger is immutable")
    if expected_previous_sha256 is not None and _hash(expected_previous_sha256, "expected_previous_sha256") != current.last_record_sha256:
        raise PaperLedgerError("stale Paper ledger append cursor")
    header = current.header
    calendar = tuple(_parse_date(item, "calendar date") for item in header["controlled_trading_dates"])
    decision_date = draft.decision_at.astimezone(CHINA_STANDARD_TIME).date()
    try:
        decision_index = calendar.index(decision_date)
    except ValueError as exc:
        raise PaperLedgerError("decision is not in the controlled trading calendar") from exc
    if decision_index + 1 >= len(calendar):
        raise PaperLedgerError("controlled calendar is missing the next execution session")
    execution_session = calendar[decision_index + 1]
    if recorded_at.astimezone(CHINA_STANDARD_TIME).date() != execution_session or recorded_at.astimezone(CHINA_STANDARD_TIME).time().replace(tzinfo=None) < time(15, 0):
        raise PaperLedgerError("decision must be appended after the next-session close, without backfill")
    if any(item.execution_session != execution_session for item in draft.executions):
        raise PaperLedgerError("all execution attempts must use the next controlled session")
    if any(item.executed_at is not None and item.executed_at > recorded_at for item in draft.executions):
        raise PaperLedgerError("execution evidence cannot be from the future")
    if any(item.instrument_id == MIDEA_INSTRUMENT_ID for item in draft.targets) or any(item.instrument_id == MIDEA_INSTRUMENT_ID for item in draft.executions) or any(item.instrument_id == MIDEA_INSTRUMENT_ID for item in draft.positions):
        raise PaperLedgerError("Midea is unmanaged_external and cannot enter strategy targets or fills")
    if any(item.manual_veto and any(ex.instrument_id == item.instrument_id and ex.filled_quantity > 0 for ex in draft.executions) for item in draft.targets):
        raise PaperLedgerError("manual-vetoed target cannot have a fill")
    prior_positions = {
        item["instrument_id"]: int(item["quantity"])
        for item in (current.decisions[-1]["positions"] if current.decisions else [])
    }
    prior_cash = (
        _money(current.decisions[-1]["cash"], "prior cash")
        if current.decisions else _money(header["initial_cash"], "initial cash")
    )
    target_map = {item.instrument_id: item for item in draft.targets}
    quantities = dict(prior_positions)
    cash = prior_cash
    total_cost = ZERO
    for execution in draft.executions:
        target = target_map.get(execution.instrument_id)
        previous_quantity = prior_positions.get(execution.instrument_id, 0)
        desired = target.final_target_quantity if target is not None else 0
        if execution.side == "BUY":
            if target is None or target.manual_veto or desired <= previous_quantity or execution.filled_quantity > desired - previous_quantity:
                raise PaperLedgerError("BUY execution is not supported by the frozen target")
        elif execution.instrument_id not in prior_positions or desired >= previous_quantity or execution.filled_quantity > previous_quantity - desired:
            raise PaperLedgerError("SELL execution is not supported by the frozen target")
        if execution.filled_quantity:
            signed = execution.filled_quantity if execution.side == "BUY" else -execution.filled_quantity
            quantities[execution.instrument_id] = quantities.get(execution.instrument_id, 0) + signed
            if quantities[execution.instrument_id] < 0:
                raise PaperLedgerError("short positions are forbidden")
            if quantities[execution.instrument_id] == 0:
                del quantities[execution.instrument_id]
            fees = execution.commission + execution.sell_tax + execution.transfer_fee
            if execution.side == "BUY":
                cash -= execution.notional + fees
            else:
                cash += execution.notional - fees
            total_cost += fees + execution.slippage_cost
    snapshot = {item.instrument_id: item.quantity for item in draft.positions}
    if snapshot != quantities:
        raise PaperLedgerError("position snapshot does not reconcile to prior positions and fills")
    if _money(cash, "reconciled cash") != draft.cash:
        raise PaperLedgerError("cash does not reconcile to fills and fees")
    positions_value = sum((item.market_value for item in draft.positions), ZERO)
    strategy_nav = _money(draft.cash + positions_value, "strategy_nav")
    peak_nav = max(
        [Decimal("10000.00")]
        + [_money(item["strategy_nav"], "prior nav") for item in current.decisions]
        + [strategy_nav]
    )
    drawdown = ((peak_nav - strategy_nav) / peak_nav).quantize(PCT) if peak_nav else ZERO
    if drawdown >= Decimal("0.12") and draft.risk_frozen is not True:
        raise PaperLedgerError("12% Paper drawdown must immediately freeze the record")
    position_count_ok = len(draft.positions) <= 2
    weights = [item.market_value / strategy_nav for item in draft.positions] if strategy_nav else []
    stock_weight_ok = all(item <= Decimal("0.4") for item in weights)
    cash_ok = strategy_nav > ZERO and draft.cash / strategy_nav >= Decimal("0.2")
    distinct_industries = len({item.csi_level1_industry for item in draft.positions}) == len(draft.positions)
    no_leverage = draft.cash >= ZERO and all(item.quantity > 0 for item in draft.positions)
    combined_value = strategy_nav + draft.external_midea.market_value
    industry_values: dict[str, Decimal] = {}
    for item in draft.positions:
        industry_values[item.csi_level1_industry] = industry_values.get(item.csi_level1_industry, ZERO) + item.market_value
    industry_values[draft.external_midea.csi_level1_industry] = industry_values.get(draft.external_midea.csi_level1_industry, ZERO) + draft.external_midea.market_value
    strategy_industries = {item.csi_level1_industry for item in draft.positions}
    combined_cap_ok = all(
        industry not in strategy_industries or value / combined_value <= Decimal("0.45")
        for industry, value in industry_values.items()
    )
    content = {
        "decision_id": draft.decision_id,
        "decision_at": draft.decision_at,
        "decision_date": decision_date,
        "execution_session": execution_session,
        "recorded_at": recorded_at,
        "data_available_at": draft.data_available_at,
        "signal_generated_at": draft.signal_generated_at,
        "signal_sha256": draft.signal_sha256,
        "model_result_sha256": draft.model_result_sha256,
        "source_bundle_sha256": draft.source_bundle_sha256,
        "paper_certificate_sha256": header["paper_certificate_sha256"],
        "experiment_sha256": header["experiment_sha256"],
        "configuration_sha256": header["configuration_sha256"],
        "data_sha256": header["data_sha256"],
        "code_sha256": header["code_sha256"],
        "controlled_calendar_sha256": header["controlled_calendar_sha256"],
        "decision_calendar_index": decision_index,
        "execution_calendar_index": decision_index + 1,
        "targets": [item.to_dict() for item in draft.targets],
        "executions": [_execution_payload(item) for item in draft.executions],
        "positions": [item.to_dict() for item in draft.positions],
        "cash": draft.cash,
        "strategy_positions_value": _money(positions_value, "positions value"),
        "strategy_nav": strategy_nav,
        "external_midea": draft.external_midea.to_dict(),
        "combined_account_value": _money(combined_value, "combined account value"),
        "peak_nav": _money(peak_nav, "peak_nav"),
        "drawdown": drawdown,
        "total_transaction_cost": _money(total_cost, "total_transaction_cost"),
        "risk_invariants": {
            "max_two_positions": position_count_ok,
            "max_stock_weight": stock_weight_ok,
            "minimum_cash": cash_ok,
            "distinct_industries": distinct_industries,
            "combined_industry_addition_cap": combined_cap_ok,
            "no_leverage_or_short": no_leverage,
        },
        "risk_frozen": draft.risk_frozen,
        "manual_execution_required": True,
        "auto_submit": False,
        "live_supported": False,
        "execution_authority": "none",
        "live_order_payload": None,
    }
    # Validate and persist the exact canonical JSON representation, not a
    # Python-only mixture of datetime/date/Decimal objects.
    content = json.loads(canonical_json_bytes(content))
    previous_content = current.decisions[-1] if current.decisions else None
    _validate_decision_content(content, header, previous_content)
    record = {
        "record_type": "decision",
        "previous_record_sha256": current.last_record_sha256,
        "content": content,
    }
    record["record_sha256"] = _sha_line(record)
    _write_new_line(Path(path), record, expected_size=current.byte_length)
    return verify_paper_ledger(path, certificate=certificate, as_of=recorded_at)


def seal_paper_ledger(
    path: str | Path,
    certificate: "PaperAdmissionCertificate",
    *,
    reason: str = "completed_forward_paper",
) -> VerifiedPaperLedger:
    """Append the one final seal; no subsequent decisions are permitted."""

    certificate = _verify_certificate(certificate)
    sealed_at = _now()
    current = verify_paper_ledger(path, certificate=certificate, as_of=sealed_at)
    if current.seal is not None:
        raise PaperLedgerError("Paper ledger is already sealed")
    if not current.decisions:
        raise PaperLedgerError("an empty Paper ledger cannot be sealed")
    reason = str(reason).strip()
    if reason not in {"completed_forward_paper", "risk_freeze", "terminated"}:
        raise PaperLedgerError("seal reason is invalid")
    if reason == "risk_freeze" and current.decisions[-1]["risk_frozen"] is not True:
        raise PaperLedgerError("risk_freeze seal requires a frozen last decision")
    content = {
        "sealed_at": sealed_at,
        "reason": reason,
        "decision_count": len(current.decisions),
        "last_decision_sha256": current.last_record_sha256,
        "manual_execution_required": True,
        "auto_submit": False,
        "live_supported": False,
        "execution_authority": "none",
    }
    record = {
        "record_type": "seal",
        "previous_record_sha256": current.last_record_sha256,
        "content": content,
    }
    record["record_sha256"] = _sha_line(record)
    _write_new_line(Path(path), record, expected_size=current.byte_length)
    return verify_paper_ledger(path, certificate=certificate, as_of=sealed_at)


def derive_paper_track_record(
    path: str | Path,
    certificate: "PaperAdmissionCertificate",
    *,
    as_of: datetime | None = None,
) -> PaperLedgerSummary:
    """Derive the exact six Stage-B gates from a sealed controlled ledger."""

    from .admission import (
        REQUIRED_FORWARD_PAPER_GATE_IDS,
        PaperTrackRecord,
        PolicyGateResult,
    )

    effective_as_of = _aware(as_of, "as_of") if as_of is not None else _now()
    verified = verify_paper_ledger(path, certificate=certificate, as_of=effective_as_of)
    if verified.seal is None:
        raise PaperLedgerError("Paper track record can only be derived from a sealed ledger")
    decisions = verified.decisions
    first_date = _parse_date(decisions[0]["decision_date"], "first decision date")
    sealed_date = _parse_datetime(verified.seal["sealed_at"], "sealed_at").astimezone(CHINA_STANDARD_TIME).date()
    full_calendar = tuple(_parse_date(item, "calendar date") for item in verified.header["controlled_trading_dates"])
    controlled = tuple(item for item in full_calendar if first_date <= item <= sealed_date)
    if not controlled:
        raise PaperLedgerError("sealed Paper window has no controlled sessions")
    decision_dates = tuple(_parse_date(item["decision_date"], "decision date") for item in decisions)
    configs = tuple(str(item["configuration_sha256"]) for item in decisions)
    max_drawdown = max(_decimal(item["drawdown"], "drawdown") for item in decisions)
    completed = _completed_months(first_date, controlled[-1])
    actual_months = {(item.year, item.month) for item in decision_dates}
    # A partial month between the last scheduled decision and sealing is not a
    # missing decision month; the 20-session due-date check below governs it.
    required_months = _month_range(first_date, decision_dates[-1])
    missing_months = tuple(
        f"{year:04d}-{month:02d}"
        for year, month in required_months
        if (year, month) not in actual_months
    )
    exact_cadence = all(
        int(current["decision_calendar_index"]) - int(previous["decision_calendar_index"]) == 20
        for previous, current in zip(decisions, decisions[1:])
    )
    no_due_decision_omitted = (
        int(decisions[-1]["decision_calendar_index"]) + 20 >= full_calendar.index(controlled[-1])
    )
    pit_complete = all(
        _parse_datetime(item["data_available_at"], "data_available_at")
        <= _parse_datetime(item["signal_generated_at"], "signal_generated_at")
        <= _parse_datetime(item["decision_at"], "decision_at")
        for item in decisions
    ) and exact_cadence and no_due_decision_omitted and not missing_months
    next_open = all(
        int(item["execution_calendar_index"]) == int(item["decision_calendar_index"]) + 1
        and all(execution["execution_basis"] == "next_session_open_manual" for execution in item["executions"])
        for item in decisions
    )
    reconciled = all(
        all(
            execution["status"] == "UNFILLED"
            or (
                _SHA256_RE.fullmatch(str(execution["broker_statement_sha256"])) is not None
                and _SHA256_RE.fullmatch(str(execution["reconciliation_sha256"])) is not None
            )
            for execution in item["executions"]
        )
        for item in decisions
    )
    risk_passed = max_drawdown < Decimal("0.12") and all(
        all(item["risk_invariants"].values()) and item["risk_frozen"] is False
        for item in decisions
    )
    config_unchanged = set(configs) == {certificate.configuration_sha256}
    manual_only = all(
        item["manual_execution_required"] is True
        and item["auto_submit"] is False
        and item["live_supported"] is False
        and item["execution_authority"] == "none"
        and item["live_order_payload"] is None
        and all(execution["manual_confirmed"] is True and execution["auto_submitted"] is False and execution["live_order_id"] is None for execution in item["executions"])
        for item in decisions
    )
    gates = (
        PolicyGateResult("paper_data_and_signal_pit_complete", pit_complete, str(pit_complete), "true; no missing month/decision"),
        PolicyGateResult("paper_next_session_open_execution_reconciled", next_open, str(next_open), "true"),
        PolicyGateResult("paper_costs_and_positions_reconciled", reconciled, str(reconciled), "true"),
        PolicyGateResult("paper_risk_limits_all_passed", risk_passed, f"max_drawdown={max_drawdown}", "all true; max_drawdown<0.12"),
        PolicyGateResult("paper_configuration_unchanged", config_unchanged, str(config_unchanged), "true"),
        PolicyGateResult("paper_manual_only_live_blocked", manual_only, str(manual_only), "manual=true; LIVE=false"),
    )
    if tuple(item.gate_id for item in gates) != REQUIRED_FORWARD_PAPER_GATE_IDS:
        raise AssertionError("Paper ledger gates drifted from Stage-B contract")
    track = PaperTrackRecord(
        paper_certificate_sha256=certificate.certificate_sha256,
        start_date=controlled[0],
        end_date=controlled[-1],
        decision_dates=decision_dates,
        controlled_trading_dates=controlled,
        configuration_hashes=configs,
        max_drawdown=max_drawdown,
        gate_results=gates,
    )
    reasons: list[str] = []
    if verified.seal["reason"] != "completed_forward_paper":
        reasons.append(f"paper_sealed_as:{verified.seal['reason']}")
    if completed < 12:
        reasons.append("forward_paper_shorter_than_12_completed_months")
    if len(decisions) < 12:
        reasons.append("fewer_than_12_forward_paper_decision_points")
    if len(actual_months) < 12:
        reasons.append("fewer_than_12_distinct_forward_paper_months")
    reasons.extend(f"missing_paper_decision_month:{item}" for item in missing_months)
    reasons.extend(f"forward_paper_gate_failed:{item.gate_id}" for item in gates if not item.passed)
    return PaperLedgerSummary(
        track_record=track,
        complete=not reasons,
        reasons=tuple(dict.fromkeys(reasons)),
        seal_sha256=verified.last_record_sha256,
        ledger_file_sha256=verified.file_sha256,
        decision_count=len(decisions),
        completed_months=completed,
        distinct_decision_months=len(actual_months),
        missing_decision_months=missing_months,
    )


__all__ = [
    "PAPER_LEDGER_PRODUCER",
    "PAPER_LEDGER_STATUS",
    "PAPER_LEDGER_VERSION",
    "PaperDecisionDraft",
    "PaperExecution",
    "PaperLedgerError",
    "PaperLedgerSummary",
    "PaperPosition",
    "PaperTarget",
    "UnmanagedExternalMark",
    "VerifiedPaperLedger",
    "append_paper_decision",
    "create_or_verify_paper_ledger",
    "derive_paper_track_record",
    "seal_paper_ledger",
    "verify_paper_ledger",
]
