"""Typed Choice capability receipt and quality/growth data gate.

This module does not call Choice and does not authenticate a live connection.
It only evaluates a content-addressed capability receipt produced by a future
controlled evidence adapter.  Raw mappings, truthy strings, and booleans are
not accepted as capability verification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
import re
from typing import Any, Mapping, Sequence

from .contracts import canonical_sha256


SCHEMA_VERSION = "choice-quality-growth-gate-v1"
PRODUCER = "choice-controlled-capability-audit-v1"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ChoiceGateContractError(ValueError):
    """Raised when a receipt tries to weaken or bypass the data gate."""


class ChoiceCapability(str, Enum):
    HISTORICAL_CONSTITUENTS = "historical_constituents"
    QFQ_DAILY_BARS = "qfq_daily_bars"
    DAILY_AMOUNT = "daily_amount"
    TRADE_CALENDAR = "trade_calendar"
    TOTAL_RETURN_BENCHMARK = "total_return_benchmark"
    PIT_INDUSTRY = "pit_industry"
    PIT_FLOAT_MARKET_CAP = "pit_float_market_cap"
    HISTORICAL_ST_STATUS = "historical_st_status"
    HISTORICAL_SUSPENSION_STATUS = "historical_suspension_status"
    HISTORICAL_LIMIT_STATUS = "historical_limit_status"
    FIRST_DISCLOSURE_FINANCIALS = "first_disclosure_financials"


class CapabilityVerification(str, Enum):
    CONTROLLED_EVIDENCE_VERIFIED = "controlled_evidence_verified"
    MISSING = "missing"
    UNVERIFIED = "unverified"
    UNSUPPORTED = "unsupported"


class ChoiceProviderId(str, Enum):
    CHOICE = "choice"


class BenchmarkReturnBasis(str, Enum):
    TOTAL_RETURN = "total_return"


class FinancialFlowBasis(str, Enum):
    SINGLE_QUARTER = "single_quarter"


class FinancialStatementScope(str, Enum):
    CONSOLIDATED = "consolidated"


class FinancialCurrency(str, Enum):
    CNY = "CNY"


class ChoiceField(str, Enum):
    INSTRUMENT_ID = "instrument_id"
    MEMBERSHIP_DATE = "membership_date"
    EFFECTIVE_FROM = "effective_from"
    EFFECTIVE_TO = "effective_to"
    MEMBERSHIP_STATUS = "membership_status"
    TRADING_DATE = "trading_date"
    OPEN = "open"
    HIGH = "high"
    LOW = "low"
    CLOSE = "close"
    PRECLOSE = "preclose"
    VOLUME = "volume"
    AMOUNT = "amount"
    ADJUSTMENT = "adjustment"
    IS_TRADING_DAY = "is_trading_day"
    TOTAL_RETURN_LEVEL = "total_return_level"
    TOTAL_RETURN_OPEN = "total_return_open"
    TOTAL_RETURN_CLOSE = "total_return_close"
    INDUSTRY_CODE = "industry_code"
    FLOAT_MARKET_CAP = "float_market_cap"
    IS_ST = "is_st"
    IS_SUSPENDED = "is_suspended"
    IS_LIMIT_UP = "is_limit_up"
    IS_LIMIT_DOWN = "is_limit_down"
    REPORT_PERIOD = "report_period"
    FIRST_DISCLOSED_AT = "first_disclosed_at"
    REVISION_ID = "revision_id"
    FLOW_BASIS = "flow_basis"
    STATEMENT_SCOPE = "statement_scope"
    CURRENCY = "currency"
    REVENUE = "revenue"
    OPERATING_COST = "operating_cost"
    OPERATING_PROFIT = "operating_profit"
    GROSS_PROFIT = "gross_profit"
    NET_PROFIT_ATTRIBUTABLE = "net_profit_attributable"
    OPERATING_CASH_FLOW = "operating_cash_flow"
    RETURN_ON_EQUITY = "return_on_equity"
    TOTAL_ASSETS = "total_assets"
    TOTAL_EQUITY = "total_equity"
    TOTAL_LIABILITIES = "total_liabilities"


class SourcePolicy(str, Enum):
    SINGLE_SOURCE_ONLY = "single_source_only"
    MIXED_SOURCE_ALLOWED = "mixed_source_allowed"


class UniverseCompletionPolicy(str, Enum):
    COMPLETE_FROZEN_UNIVERSE_ONLY = "complete_frozen_universe_only"
    SUCCESSFUL_SUBSAMPLE_ALLOWED = "successful_subsample_allowed"


class MembershipBackfillPolicy(str, Enum):
    HISTORICAL_AS_OF_ONLY = "historical_as_of_only"
    CURRENT_CONSTITUENTS_BACKFILL = "current_constituents_backfill"


class RevisionPolicy(str, Enum):
    FIRST_DISCLOSURE_APPEND_ONLY = "first_disclosure_append_only"
    LATEST_REVISION_OVERWRITE = "latest_revision_overwrite"


class ChoiceGateStatus(str, Enum):
    CONTRACT_SATISFIED = "contract_satisfied_not_connectivity_proof"
    BLOCKED_MISSING_PIT_DATA = "blocked_missing_pit_data"
    BLOCKED_UNSAFE_DATA_POLICY = "blocked_unsafe_data_policy"


DEFAULT_QUALITY_GROWTH_FINANCIAL_FIELDS: tuple[ChoiceField, ...] = (
    ChoiceField.NET_PROFIT_ATTRIBUTABLE,
    ChoiceField.OPERATING_CASH_FLOW,
    ChoiceField.OPERATING_PROFIT,
    ChoiceField.REVENUE,
    ChoiceField.RETURN_ON_EQUITY,
    ChoiceField.TOTAL_ASSETS,
    ChoiceField.TOTAL_EQUITY,
    ChoiceField.TOTAL_LIABILITIES,
)

_BASE_REQUIRED_FIELDS: Mapping[ChoiceCapability, frozenset[ChoiceField]] = {
    ChoiceCapability.HISTORICAL_CONSTITUENTS: frozenset(
        {
            ChoiceField.INSTRUMENT_ID,
            ChoiceField.EFFECTIVE_FROM,
            ChoiceField.EFFECTIVE_TO,
            ChoiceField.MEMBERSHIP_STATUS,
        }
    ),
    ChoiceCapability.QFQ_DAILY_BARS: frozenset(
        {
            ChoiceField.INSTRUMENT_ID,
            ChoiceField.TRADING_DATE,
            ChoiceField.OPEN,
            ChoiceField.HIGH,
            ChoiceField.LOW,
            ChoiceField.CLOSE,
            ChoiceField.PRECLOSE,
            ChoiceField.VOLUME,
            ChoiceField.ADJUSTMENT,
        }
    ),
    ChoiceCapability.DAILY_AMOUNT: frozenset(
        {ChoiceField.INSTRUMENT_ID, ChoiceField.TRADING_DATE, ChoiceField.AMOUNT}
    ),
    ChoiceCapability.TRADE_CALENDAR: frozenset(
        {ChoiceField.TRADING_DATE, ChoiceField.IS_TRADING_DAY}
    ),
    ChoiceCapability.TOTAL_RETURN_BENCHMARK: frozenset(
        {
            ChoiceField.TRADING_DATE,
            ChoiceField.TOTAL_RETURN_OPEN,
            ChoiceField.TOTAL_RETURN_CLOSE,
        }
    ),
    ChoiceCapability.PIT_INDUSTRY: frozenset(
        {
            ChoiceField.INSTRUMENT_ID,
            ChoiceField.EFFECTIVE_FROM,
            ChoiceField.EFFECTIVE_TO,
            ChoiceField.INDUSTRY_CODE,
        }
    ),
    ChoiceCapability.PIT_FLOAT_MARKET_CAP: frozenset(
        {
            ChoiceField.INSTRUMENT_ID,
            ChoiceField.TRADING_DATE,
            ChoiceField.FLOAT_MARKET_CAP,
        }
    ),
    ChoiceCapability.HISTORICAL_ST_STATUS: frozenset(
        {ChoiceField.INSTRUMENT_ID, ChoiceField.TRADING_DATE, ChoiceField.IS_ST}
    ),
    ChoiceCapability.HISTORICAL_SUSPENSION_STATUS: frozenset(
        {ChoiceField.INSTRUMENT_ID, ChoiceField.TRADING_DATE, ChoiceField.IS_SUSPENDED}
    ),
    ChoiceCapability.HISTORICAL_LIMIT_STATUS: frozenset(
        {
            ChoiceField.INSTRUMENT_ID,
            ChoiceField.TRADING_DATE,
            ChoiceField.IS_LIMIT_UP,
            ChoiceField.IS_LIMIT_DOWN,
        }
    ),
    ChoiceCapability.FIRST_DISCLOSURE_FINANCIALS: frozenset(
        {
            ChoiceField.INSTRUMENT_ID,
            ChoiceField.REPORT_PERIOD,
            ChoiceField.FIRST_DISCLOSED_AT,
            ChoiceField.REVISION_ID,
            ChoiceField.FLOW_BASIS,
            ChoiceField.STATEMENT_SCOPE,
            ChoiceField.CURRENCY,
        }
    ),
}

_MINIMUM_COVERAGE_START: Mapping[ChoiceCapability, date] = {
    ChoiceCapability.HISTORICAL_CONSTITUENTS: date(2018, 1, 1),
    ChoiceCapability.QFQ_DAILY_BARS: date(2017, 1, 1),
    ChoiceCapability.DAILY_AMOUNT: date(2017, 1, 1),
    ChoiceCapability.TRADE_CALENDAR: date(2017, 1, 1),
    ChoiceCapability.TOTAL_RETURN_BENCHMARK: date(2018, 1, 1),
    ChoiceCapability.PIT_INDUSTRY: date(2018, 1, 1),
    ChoiceCapability.PIT_FLOAT_MARKET_CAP: date(2017, 1, 1),
    ChoiceCapability.HISTORICAL_ST_STATUS: date(2017, 1, 1),
    ChoiceCapability.HISTORICAL_SUSPENSION_STATUS: date(2017, 1, 1),
    ChoiceCapability.HISTORICAL_LIMIT_STATUS: date(2017, 1, 1),
    ChoiceCapability.FIRST_DISCLOSURE_FINANCIALS: date(2014, 1, 1),
}

_MINIMUM_DISTINCT_SUBJECTS: Mapping[ChoiceCapability, int] = {
    capability: (
        1
        if capability
        in {ChoiceCapability.TRADE_CALENDAR, ChoiceCapability.TOTAL_RETURN_BENCHMARK}
        else 800
    )
    for capability in ChoiceCapability
}

_MINIMUM_ROWS_PER_SUBJECT: Mapping[ChoiceCapability, int] = {
    ChoiceCapability.HISTORICAL_CONSTITUENTS: 1,
    ChoiceCapability.QFQ_DAILY_BARS: 1000,
    ChoiceCapability.DAILY_AMOUNT: 1000,
    ChoiceCapability.TRADE_CALENDAR: 1000,
    ChoiceCapability.TOTAL_RETURN_BENCHMARK: 1000,
    ChoiceCapability.PIT_INDUSTRY: 1,
    ChoiceCapability.PIT_FLOAT_MARKET_CAP: 1000,
    ChoiceCapability.HISTORICAL_ST_STATUS: 1000,
    ChoiceCapability.HISTORICAL_SUSPENSION_STATUS: 1000,
    ChoiceCapability.HISTORICAL_LIMIT_STATUS: 1000,
    ChoiceCapability.FIRST_DISCLOSURE_FINANCIALS: 12,
}


def _require_enum(value: Any, enum_type: type[Enum], label: str) -> Enum:
    if not isinstance(value, enum_type):
        raise ChoiceGateContractError(
            f"{label} must be an explicit {enum_type.__name__} enum, not a string or boolean"
        )
    return value


def _text(value: Any, label: str, *, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChoiceGateContractError(f"{label} must be a non-empty string")
    result = value.strip()
    if pattern is not None and pattern.fullmatch(result) is None:
        raise ChoiceGateContractError(f"{label} has an invalid format")
    return result


def _aware_datetime(value: Any, label: str) -> datetime:
    if not isinstance(value, datetime):
        raise ChoiceGateContractError(f"{label} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ChoiceGateContractError(f"{label} must include a timezone offset")
    return value


def _date(value: Any, label: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ChoiceGateContractError(f"{label} must be a date")
    return value


def _positive_integer(value: Any, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ChoiceGateContractError(f"{label} must be a positive integer")
    return value


@dataclass(frozen=True)
class ChoiceCapabilityItem:
    """One typed capability statement backed by controlled evidence hashes."""

    capability: ChoiceCapability
    verification: CapabilityVerification
    provider_id: ChoiceProviderId
    dataset_contract: str | None = None
    subject_ids: tuple[str, ...] = ()
    return_basis: BenchmarkReturnBasis | None = None
    financial_flow_basis: FinancialFlowBasis | None = None
    financial_statement_scope: FinancialStatementScope | None = None
    financial_currency: FinancialCurrency | None = None
    fields: tuple[ChoiceField, ...] = ()
    coverage_start: date | None = None
    coverage_end: date | None = None
    row_count: int | None = None
    distinct_subject_count: int | None = None
    evidence_receipt_sha256: str | None = None
    normalized_content_sha256: str | None = None
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        capability = _require_enum(self.capability, ChoiceCapability, "capability")
        verification = _require_enum(
            self.verification, CapabilityVerification, "verification"
        )
        provider_id = _require_enum(self.provider_id, ChoiceProviderId, "provider_id")
        subject_ids = tuple(self.subject_ids)
        if any(
            not isinstance(item, str) or _IDENTIFIER.fullmatch(item.strip()) is None
            for item in subject_ids
        ):
            raise ChoiceGateContractError(
                "subject_ids must contain canonical provider subject identifiers"
            )
        subject_ids = tuple(sorted(item.strip() for item in subject_ids))
        if len(set(subject_ids)) != len(subject_ids):
            raise ChoiceGateContractError("subject_ids must be unique")
        return_basis = self.return_basis
        if return_basis is not None and not isinstance(return_basis, BenchmarkReturnBasis):
            raise ChoiceGateContractError(
                "return_basis must be a BenchmarkReturnBasis enum, not a caller string"
            )
        financial_flow_basis = self.financial_flow_basis
        financial_statement_scope = self.financial_statement_scope
        financial_currency = self.financial_currency
        if financial_flow_basis is not None and not isinstance(
            financial_flow_basis, FinancialFlowBasis
        ):
            raise ChoiceGateContractError("financial_flow_basis must be a typed enum")
        if financial_statement_scope is not None and not isinstance(
            financial_statement_scope, FinancialStatementScope
        ):
            raise ChoiceGateContractError("financial_statement_scope must be a typed enum")
        if financial_currency is not None and not isinstance(
            financial_currency, FinancialCurrency
        ):
            raise ChoiceGateContractError("financial_currency must be a typed enum")
        fields = tuple(self.fields)
        if any(not isinstance(item, ChoiceField) for item in fields):
            raise ChoiceGateContractError(
                "fields must contain ChoiceField enums; caller strings cannot verify fields"
            )
        if len(set(fields)) != len(fields):
            raise ChoiceGateContractError("capability fields must be unique")
        fields = tuple(sorted(fields, key=lambda item: item.value))
        verified = verification is CapabilityVerification.CONTROLLED_EVIDENCE_VERIFIED
        if verified:
            dataset_contract = _text(
                self.dataset_contract, "dataset_contract", pattern=_IDENTIFIER
            )
            receipt_hash = _text(
                self.evidence_receipt_sha256, "evidence_receipt_sha256", pattern=_SHA256
            )
            content_hash = _text(
                self.normalized_content_sha256,
                "normalized_content_sha256",
                pattern=_SHA256,
            )
            observed_at = _aware_datetime(self.observed_at, "observed_at")
            coverage_start = _date(self.coverage_start, "coverage_start")
            coverage_end = _date(self.coverage_end, "coverage_end")
            if coverage_start > coverage_end:
                raise ChoiceGateContractError(
                    "coverage_start must not be after coverage_end"
                )
            row_count = _positive_integer(self.row_count, "row_count")
            distinct_subject_count = _positive_integer(
                self.distinct_subject_count, "distinct_subject_count"
            )
            if row_count < distinct_subject_count:
                raise ChoiceGateContractError(
                    "row_count cannot be smaller than distinct_subject_count"
                )
            if not fields:
                raise ChoiceGateContractError("verified capability fields must not be empty")
            if not subject_ids:
                raise ChoiceGateContractError(
                    "verified capability subject_ids must not be empty"
                )
            if capability is ChoiceCapability.TOTAL_RETURN_BENCHMARK:
                if len(subject_ids) != 1:
                    raise ChoiceGateContractError(
                        "verified total-return benchmark must bind exactly one provider subject"
                    )
                if return_basis is not BenchmarkReturnBasis.TOTAL_RETURN:
                    raise ChoiceGateContractError(
                        "verified benchmark must carry typed total_return metadata"
                    )
            elif return_basis is not None:
                raise ChoiceGateContractError(
                    "return_basis is only valid for total_return_benchmark"
                )
            if capability is ChoiceCapability.FIRST_DISCLOSURE_FINANCIALS:
                if (
                    financial_flow_basis is not FinancialFlowBasis.SINGLE_QUARTER
                    or financial_statement_scope
                    is not FinancialStatementScope.CONSOLIDATED
                    or financial_currency is not FinancialCurrency.CNY
                ):
                    raise ChoiceGateContractError(
                        "verified financials must bind single-quarter consolidated CNY values"
                    )
            elif any(
                value is not None
                for value in (
                    financial_flow_basis,
                    financial_statement_scope,
                    financial_currency,
                )
            ):
                raise ChoiceGateContractError(
                    "financial basis metadata is only valid for first_disclosure_financials"
                )
            if len(subject_ids) != distinct_subject_count:
                raise ChoiceGateContractError(
                    "subject_ids must enumerate every distinct subject; aggregate self-reported counts are forbidden"
                )
        else:
            if any(
                value is not None
                for value in (
                    self.dataset_contract,
                    self.evidence_receipt_sha256,
                    self.normalized_content_sha256,
                    self.observed_at,
                    self.coverage_start,
                    self.coverage_end,
                    self.row_count,
                    self.distinct_subject_count,
                )
            ) or fields or subject_ids or return_basis is not None or any(
                value is not None
                for value in (
                    financial_flow_basis,
                    financial_statement_scope,
                    financial_currency,
                )
            ):
                raise ChoiceGateContractError(
                    "unverified capability cannot carry evidence, subjects, return basis, or fields"
                )
            dataset_contract = None
            receipt_hash = None
            content_hash = None
            observed_at = None
            subject_ids = ()
            return_basis = None
            coverage_start = None
            coverage_end = None
            row_count = None
            distinct_subject_count = None
        object.__setattr__(self, "capability", capability)
        object.__setattr__(self, "verification", verification)
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "dataset_contract", dataset_contract)
        object.__setattr__(self, "subject_ids", subject_ids)
        object.__setattr__(self, "return_basis", return_basis)
        object.__setattr__(self, "financial_flow_basis", financial_flow_basis)
        object.__setattr__(self, "financial_statement_scope", financial_statement_scope)
        object.__setattr__(self, "financial_currency", financial_currency)
        object.__setattr__(self, "fields", fields)
        object.__setattr__(self, "coverage_start", coverage_start)
        object.__setattr__(self, "coverage_end", coverage_end)
        object.__setattr__(self, "row_count", row_count)
        object.__setattr__(self, "distinct_subject_count", distinct_subject_count)
        object.__setattr__(self, "evidence_receipt_sha256", receipt_hash)
        object.__setattr__(self, "normalized_content_sha256", content_hash)
        object.__setattr__(self, "observed_at", observed_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability.value,
            "verification": self.verification.value,
            "provider_id": self.provider_id.value,
            "dataset_contract": self.dataset_contract,
            "subject_ids": list(self.subject_ids),
            "return_basis": self.return_basis.value if self.return_basis else None,
            "financial_flow_basis": (
                self.financial_flow_basis.value if self.financial_flow_basis else None
            ),
            "financial_statement_scope": (
                self.financial_statement_scope.value
                if self.financial_statement_scope
                else None
            ),
            "financial_currency": (
                self.financial_currency.value if self.financial_currency else None
            ),
            "fields": [item.value for item in self.fields],
            "coverage_start": self.coverage_start.isoformat() if self.coverage_start else None,
            "coverage_end": self.coverage_end.isoformat() if self.coverage_end else None,
            "row_count": self.row_count,
            "distinct_subject_count": self.distinct_subject_count,
            "evidence_receipt_sha256": self.evidence_receipt_sha256,
            "normalized_content_sha256": self.normalized_content_sha256,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ChoiceCapabilityItem":
        if not isinstance(payload, Mapping) or set(payload) != {
            "capability",
            "verification",
            "provider_id",
            "dataset_contract",
            "subject_ids",
            "return_basis",
            "financial_flow_basis",
            "financial_statement_scope",
            "financial_currency",
            "fields",
            "coverage_start",
            "coverage_end",
            "row_count",
            "distinct_subject_count",
            "evidence_receipt_sha256",
            "normalized_content_sha256",
            "observed_at",
        }:
            raise ChoiceGateContractError("capability item fields differ from the contract")
        try:
            capability = ChoiceCapability(payload["capability"])
            verification = CapabilityVerification(payload["verification"])
            provider_id = ChoiceProviderId(payload["provider_id"])
        except (TypeError, ValueError) as exc:
            raise ChoiceGateContractError("capability item contains an unknown enum") from exc
        raw_fields = payload["fields"]
        if not isinstance(raw_fields, list):
            raise ChoiceGateContractError("capability fields must be an array")
        try:
            fields = tuple(ChoiceField(item) for item in raw_fields)
        except (TypeError, ValueError) as exc:
            raise ChoiceGateContractError("capability contains an unknown field enum") from exc
        raw_subject_ids = payload["subject_ids"]
        if not isinstance(raw_subject_ids, list):
            raise ChoiceGateContractError("capability subject_ids must be an array")
        raw_return_basis = payload["return_basis"]
        try:
            return_basis = (
                None
                if raw_return_basis is None
                else BenchmarkReturnBasis(raw_return_basis)
            )
        except (TypeError, ValueError) as exc:
            raise ChoiceGateContractError("capability return_basis is unknown") from exc
        try:
            financial_flow_basis = (
                None
                if payload["financial_flow_basis"] is None
                else FinancialFlowBasis(payload["financial_flow_basis"])
            )
            financial_statement_scope = (
                None
                if payload["financial_statement_scope"] is None
                else FinancialStatementScope(payload["financial_statement_scope"])
            )
            financial_currency = (
                None
                if payload["financial_currency"] is None
                else FinancialCurrency(payload["financial_currency"])
            )
        except (TypeError, ValueError) as exc:
            raise ChoiceGateContractError("financial basis metadata is unknown") from exc
        raw_observed = payload["observed_at"]
        observed = None
        if raw_observed is not None:
            if not isinstance(raw_observed, str):
                raise ChoiceGateContractError("observed_at must be an ISO datetime")
            try:
                observed = datetime.fromisoformat(raw_observed.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ChoiceGateContractError("observed_at must be an ISO datetime") from exc
        coverage_start = None
        coverage_end = None
        if (payload["coverage_start"] is None) != (payload["coverage_end"] is None):
            raise ChoiceGateContractError(
                "coverage_start and coverage_end must both be null or both be dates"
            )
        if payload["coverage_start"] is not None:
            try:
                coverage_start = date.fromisoformat(payload["coverage_start"])
                coverage_end = date.fromisoformat(payload["coverage_end"])
            except (TypeError, ValueError) as exc:
                raise ChoiceGateContractError(
                    "coverage_start and coverage_end must be ISO dates"
                ) from exc
        return cls(
            capability=capability,
            verification=verification,
            provider_id=provider_id,
            dataset_contract=payload["dataset_contract"],
            subject_ids=tuple(raw_subject_ids),
            return_basis=return_basis,
            financial_flow_basis=financial_flow_basis,
            financial_statement_scope=financial_statement_scope,
            financial_currency=financial_currency,
            fields=fields,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            row_count=payload["row_count"],
            distinct_subject_count=payload["distinct_subject_count"],
            evidence_receipt_sha256=payload["evidence_receipt_sha256"],
            normalized_content_sha256=payload["normalized_content_sha256"],
            observed_at=observed,
        )


@dataclass(frozen=True)
class ChoiceCapabilityReceipt:
    receipt_id: str
    generated_at: datetime
    coverage_cutoff: date
    capabilities: tuple[ChoiceCapabilityItem, ...]
    source_policy: SourcePolicy
    universe_completion_policy: UniverseCompletionPolicy
    membership_backfill_policy: MembershipBackfillPolicy
    revision_policy: RevisionPolicy
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        receipt_id = _text(self.receipt_id, "receipt_id", pattern=_IDENTIFIER)
        generated_at = _aware_datetime(self.generated_at, "generated_at")
        coverage_cutoff = _date(self.coverage_cutoff, "coverage_cutoff")
        if coverage_cutoff > generated_at.date():
            raise ChoiceGateContractError(
                "coverage_cutoff cannot be after receipt generated_at"
            )
        source_policy = _require_enum(self.source_policy, SourcePolicy, "source_policy")
        completion = _require_enum(
            self.universe_completion_policy,
            UniverseCompletionPolicy,
            "universe_completion_policy",
        )
        backfill = _require_enum(
            self.membership_backfill_policy,
            MembershipBackfillPolicy,
            "membership_backfill_policy",
        )
        revision = _require_enum(self.revision_policy, RevisionPolicy, "revision_policy")
        capabilities = tuple(self.capabilities)
        if any(not isinstance(item, ChoiceCapabilityItem) for item in capabilities):
            raise ChoiceGateContractError(
                "capabilities must contain ChoiceCapabilityItem objects"
            )
        ids = [item.capability for item in capabilities]
        expected = set(ChoiceCapability)
        if set(ids) != expected or len(ids) != len(expected):
            raise ChoiceGateContractError(
                "receipt must contain each required Choice capability exactly once"
            )
        capabilities = tuple(sorted(capabilities, key=lambda item: item.capability.value))
        object.__setattr__(self, "receipt_id", receipt_id)
        object.__setattr__(self, "generated_at", generated_at)
        object.__setattr__(self, "coverage_cutoff", coverage_cutoff)
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "source_policy", source_policy)
        object.__setattr__(self, "universe_completion_policy", completion)
        object.__setattr__(self, "membership_backfill_policy", backfill)
        object.__setattr__(self, "revision_policy", revision)
        object.__setattr__(self, "receipt_sha256", canonical_sha256(self.to_content_dict()))

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "receipt_id": self.receipt_id,
            "generated_at": self.generated_at.isoformat(),
            "coverage_cutoff": self.coverage_cutoff.isoformat(),
            "producer": PRODUCER,
            "claim_scope": "capability_contract_only_not_live_connectivity",
            "policies": {
                "source_policy": self.source_policy.value,
                "universe_completion_policy": self.universe_completion_policy.value,
                "membership_backfill_policy": self.membership_backfill_policy.value,
                "revision_policy": self.revision_policy.value,
            },
            "capabilities": [item.to_dict() for item in self.capabilities],
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.to_content_dict()
        payload["receipt_sha256"] = self.receipt_sha256
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ChoiceCapabilityReceipt":
        if not isinstance(payload, Mapping) or set(payload) != {
            "schema_version",
            "receipt_id",
            "generated_at",
            "coverage_cutoff",
            "producer",
            "claim_scope",
            "policies",
            "capabilities",
            "receipt_sha256",
        }:
            raise ChoiceGateContractError("receipt fields differ from the contract")
        if payload["schema_version"] != SCHEMA_VERSION or payload["producer"] != PRODUCER:
            raise ChoiceGateContractError("receipt schema or producer mismatch")
        if payload["claim_scope"] != "capability_contract_only_not_live_connectivity":
            raise ChoiceGateContractError("receipt claim_scope mismatch")
        policies = payload["policies"]
        if not isinstance(policies, Mapping) or set(policies) != {
            "source_policy",
            "universe_completion_policy",
            "membership_backfill_policy",
            "revision_policy",
        }:
            raise ChoiceGateContractError("receipt policies differ from the contract")
        raw_capabilities = payload["capabilities"]
        if not isinstance(raw_capabilities, list):
            raise ChoiceGateContractError("receipt capabilities must be an array")
        try:
            generated_at = datetime.fromisoformat(str(payload["generated_at"]).replace("Z", "+00:00"))
            coverage_cutoff = date.fromisoformat(payload["coverage_cutoff"])
            receipt = cls(
                receipt_id=str(payload["receipt_id"]),
                generated_at=generated_at,
                coverage_cutoff=coverage_cutoff,
                capabilities=tuple(
                    ChoiceCapabilityItem.from_dict(item) for item in raw_capabilities
                ),
                source_policy=SourcePolicy(policies["source_policy"]),
                universe_completion_policy=UniverseCompletionPolicy(
                    policies["universe_completion_policy"]
                ),
                membership_backfill_policy=MembershipBackfillPolicy(
                    policies["membership_backfill_policy"]
                ),
                revision_policy=RevisionPolicy(policies["revision_policy"]),
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ChoiceGateContractError):
                raise
            raise ChoiceGateContractError("receipt contains an unknown enum or datetime") from exc
        declared = _text(payload["receipt_sha256"], "receipt_sha256", pattern=_SHA256)
        if receipt.receipt_sha256 != declared:
            raise ChoiceGateContractError("receipt_sha256 mismatch")
        if receipt.to_dict() != dict(payload):
            raise ChoiceGateContractError("receipt payload is not canonical")
        return receipt


@dataclass(frozen=True)
class ChoiceGateEvaluation:
    status: ChoiceGateStatus
    receipt_sha256: str
    missing_capabilities: tuple[str, ...]
    missing_fields: tuple[str, ...]
    policy_violations: tuple[str, ...]
    live_connectivity_status: str = "not_assessed"
    formal_truth_eligibility: bool = field(init=False, default=False)

    @property
    def contract_satisfied(self) -> bool:
        return self.status is ChoiceGateStatus.CONTRACT_SATISFIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "receipt_sha256": self.receipt_sha256,
            "missing_capabilities": list(self.missing_capabilities),
            "missing_fields": list(self.missing_fields),
            "policy_violations": list(self.policy_violations),
            "live_connectivity_status": self.live_connectivity_status,
            "formal_truth_eligibility": self.formal_truth_eligibility,
            "contract_satisfied": self.contract_satisfied,
        }


def evaluate_choice_quality_growth_gate(
    receipt: ChoiceCapabilityReceipt,
    *,
    required_financial_fields: Sequence[ChoiceField] = DEFAULT_QUALITY_GROWTH_FINANCIAL_FIELDS,
) -> ChoiceGateEvaluation:
    """Evaluate an already controlled receipt without probing or upgrading it."""

    if not isinstance(receipt, ChoiceCapabilityReceipt):
        raise ChoiceGateContractError(
            "gate requires ChoiceCapabilityReceipt; mappings, strings, and booleans cannot verify data"
        )
    financial_fields = tuple(required_financial_fields)
    if not financial_fields or any(not isinstance(item, ChoiceField) for item in financial_fields):
        raise ChoiceGateContractError(
            "required_financial_fields must contain ChoiceField enums"
        )
    if len(set(financial_fields)) != len(financial_fields):
        raise ChoiceGateContractError("required_financial_fields must be unique")

    policy_violations: list[str] = []
    if receipt.source_policy is not SourcePolicy.SINGLE_SOURCE_ONLY:
        policy_violations.append("mixed_source_forbidden")
    if (
        receipt.universe_completion_policy
        is not UniverseCompletionPolicy.COMPLETE_FROZEN_UNIVERSE_ONLY
    ):
        policy_violations.append("successful_subsample_forbidden")
    if receipt.membership_backfill_policy is not MembershipBackfillPolicy.HISTORICAL_AS_OF_ONLY:
        policy_violations.append("current_constituents_backfill_forbidden")
    if receipt.revision_policy is not RevisionPolicy.FIRST_DISCLOSURE_APPEND_ONLY:
        policy_violations.append("revision_overwrite_forbidden")

    by_capability = {item.capability: item for item in receipt.capabilities}
    missing_capabilities: list[str] = []
    missing_fields: list[str] = []
    for capability in ChoiceCapability:
        item = by_capability[capability]
        if item.verification is not CapabilityVerification.CONTROLLED_EVIDENCE_VERIFIED:
            missing_capabilities.append(capability.value)
            continue
        if item.provider_id is not ChoiceProviderId.CHOICE:
            missing_capabilities.append(capability.value)
            continue
        required = set(_BASE_REQUIRED_FIELDS[capability])
        if capability is ChoiceCapability.FIRST_DISCLOSURE_FINANCIALS:
            required.update(financial_fields)
        absent = sorted(required - set(item.fields), key=lambda field: field.value)
        missing_fields.extend(
            f"{capability.value}:{field.value}" for field in absent
        )
        if capability is ChoiceCapability.FIRST_DISCLOSURE_FINANCIALS:
            has_gross_profit = ChoiceField.GROSS_PROFIT in item.fields
            can_recompute_gross_profit = {
                ChoiceField.REVENUE,
                ChoiceField.OPERATING_COST,
            }.issubset(item.fields)
            if not has_gross_profit and not can_recompute_gross_profit:
                missing_fields.append(
                    "first_disclosure_financials:gross_profit_or_revenue_and_operating_cost"
                )
        if item.coverage_start > _MINIMUM_COVERAGE_START[capability]:
            missing_fields.append(f"{capability.value}:coverage_start")
        if item.coverage_end < receipt.coverage_cutoff:
            missing_fields.append(f"{capability.value}:coverage_end")
        minimum_subjects = _MINIMUM_DISTINCT_SUBJECTS[capability]
        if item.distinct_subject_count < minimum_subjects:
            missing_fields.append(f"{capability.value}:distinct_subject_count")
        minimum_rows = (
            item.distinct_subject_count * _MINIMUM_ROWS_PER_SUBJECT[capability]
        )
        if item.row_count < minimum_rows:
            missing_fields.append(f"{capability.value}:row_count")

    stock_scope = set(by_capability[ChoiceCapability.HISTORICAL_CONSTITUENTS].subject_ids)
    for capability in (
        ChoiceCapability.QFQ_DAILY_BARS,
        ChoiceCapability.DAILY_AMOUNT,
        ChoiceCapability.PIT_INDUSTRY,
        ChoiceCapability.PIT_FLOAT_MARKET_CAP,
        ChoiceCapability.HISTORICAL_ST_STATUS,
        ChoiceCapability.HISTORICAL_SUSPENSION_STATUS,
        ChoiceCapability.HISTORICAL_LIMIT_STATUS,
        ChoiceCapability.FIRST_DISCLOSURE_FINANCIALS,
    ):
        item = by_capability[capability]
        if (
            item.verification is CapabilityVerification.CONTROLLED_EVIDENCE_VERIFIED
            and not stock_scope.issubset(item.subject_ids)
        ):
            missing_fields.append(f"{capability.value}:historical_constituent_union")

    if missing_capabilities or missing_fields:
        status = ChoiceGateStatus.BLOCKED_MISSING_PIT_DATA
    elif policy_violations:
        status = ChoiceGateStatus.BLOCKED_UNSAFE_DATA_POLICY
    else:
        status = ChoiceGateStatus.CONTRACT_SATISFIED
    return ChoiceGateEvaluation(
        status=status,
        receipt_sha256=receipt.receipt_sha256,
        missing_capabilities=tuple(sorted(missing_capabilities)),
        missing_fields=tuple(sorted(missing_fields)),
        policy_violations=tuple(sorted(policy_violations)),
    )


__all__ = [
    "BenchmarkReturnBasis",
    "CapabilityVerification",
    "ChoiceCapability",
    "ChoiceCapabilityItem",
    "ChoiceCapabilityReceipt",
    "ChoiceField",
    "ChoiceGateContractError",
    "ChoiceGateEvaluation",
    "ChoiceGateStatus",
    "ChoiceProviderId",
    "DEFAULT_QUALITY_GROWTH_FINANCIAL_FIELDS",
    "FinancialCurrency",
    "FinancialFlowBasis",
    "FinancialStatementScope",
    "MembershipBackfillPolicy",
    "PRODUCER",
    "RevisionPolicy",
    "SCHEMA_VERSION",
    "SourcePolicy",
    "UniverseCompletionPolicy",
    "evaluate_choice_quality_growth_gate",
]
