"""Point-in-time domain models for broker research report auditing.

The models in this module deliberately reject naive timestamps.  Source
adapters may use :func:`parse_datetime` to attach the source timezone, but a
model can never silently accept an ambiguous datetime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from types import MappingProxyType
from typing import Any, Mapping


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
DIMENSIONS = frozenset({"macro", "industry", "stock"})
CLAIM_EVIDENCE_SOURCE_KINDS = frozenset(
    {"structured/source_record", "textual/pdf"}
)
LEGACY_UNVERIFIED_EVIDENCE_KIND = "legacy/unverified"


class ModelValidationError(ValueError):
    """Raised when an audit model violates its point-in-time contract."""


def ensure_aware(value: datetime, name: str = "timestamp") -> datetime:
    """Return *value* after verifying that it has a usable timezone."""

    if not isinstance(value, datetime):
        raise ModelValidationError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ModelValidationError(f"{name} must be timezone-aware")
    return value


def parse_datetime(value: str | date | datetime, *, default_tz=CHINA_TZ) -> datetime:
    """Parse an external timestamp and attach ``default_tz`` when it is naive.

    Attaching a timezone here is explicit source-normalisation.  Domain models
    themselves still reject naive values through :func:`ensure_aware`.
    """

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    else:
        text = str(value).strip()
        if not text:
            raise ModelValidationError("timestamp must not be empty")
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ModelValidationError(f"invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=default_tz)
    return parsed


def decimal_or_none(value: Any) -> Decimal | None:
    """Convert a source scalar to ``Decimal`` without accepting NaN/Infinity."""

    if value is None or value == "":
        return None
    try:
        converted = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ModelValidationError(f"invalid decimal value: {value!r}") from exc
    if not converted.is_finite():
        raise ModelValidationError(f"decimal value must be finite: {value!r}")
    return converted


def _require_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ModelValidationError(f"{name} must not be empty")


def _normalise_sha256(value: str, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ModelValidationError(f"{name} must be a string")
    normalised = value.strip().lower()
    if allow_empty and not normalised:
        return ""
    if len(normalised) != 64 or any(
        character not in "0123456789abcdef" for character in normalised
    ):
        raise ModelValidationError(f"{name} must be a SHA-256 hex digest")
    return normalised


def _validate_dimension(value: str) -> None:
    if value not in DIMENSIONS:
        raise ModelValidationError(
            f"dimension must be one of {sorted(DIMENSIONS)}, got {value!r}"
        )


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    # A shallow immutable copy prevents accidental mutation of source metadata.
    return MappingProxyType(dict(value))


def stable_identifier(*parts: object) -> str:
    """Return a deterministic SHA-256 identifier for model natural keys."""

    payload = "\x1f".join("" if part is None else str(part) for part in parts)
    return sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ResearchReport:
    """Normalised report metadata exactly as exposed by a source."""

    report_id: str
    dimension: str
    subject_id: str
    title: str
    broker: str
    analyst: str
    published_at: datetime
    available_at: datetime
    fetched_at: datetime
    source: str
    content_hash: str
    subject_name: str = ""
    team: str = ""
    industry_id: str = ""
    rating: str = ""
    rating_change: str = ""
    target_price_min: Decimal | None = None
    target_price_max: Decimal | None = None
    source_url: str = ""
    pdf_url: str = ""
    # SHA-256 of the exact PDF bytes used for textual extraction.  It remains
    # empty until the PDF has actually been fetched and verified; callers must
    # never substitute the listing-record hash for this document hash.
    pdf_sha256: str = ""
    timestamp_quality: str = "date_only"
    broker_code: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name in ("report_id", "title", "broker", "source", "content_hash"):
            _require_text(getattr(self, name), name)
        _validate_dimension(self.dimension)
        ensure_aware(self.published_at, "published_at")
        ensure_aware(self.available_at, "available_at")
        ensure_aware(self.fetched_at, "fetched_at")
        if self.available_at < self.published_at:
            raise ModelValidationError("available_at cannot precede published_at")
        object.__setattr__(
            self,
            "content_hash",
            _normalise_sha256(self.content_hash, "content_hash"),
        )
        object.__setattr__(
            self,
            "pdf_sha256",
            _normalise_sha256(self.pdf_sha256, "pdf_sha256", allow_empty=True),
        )
        low = decimal_or_none(self.target_price_min)
        high = decimal_or_none(self.target_price_max)
        if low is not None and high is not None and low > high:
            raise ModelValidationError("target_price_min cannot exceed target_price_max")
        object.__setattr__(self, "target_price_min", low)
        object.__setattr__(self, "target_price_max", high)
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))


@dataclass(frozen=True)
class ResearchClaim:
    """One explicit and falsifiable forecast extracted from a report."""

    claim_id: str
    report_id: str
    dimension: str
    subject_id: str
    target_type: str
    direction: int
    value_min: Decimal | None
    value_max: Decimal | None
    unit: str
    benchmark: str
    forecast_period: str
    horizon_days: int
    available_at: datetime
    evidence_span: str
    extractor_version: str
    extraction_confidence: float
    # New claims bind their evidence to the exact immutable source bytes.
    # Migrated pre-contract rows are marked legacy/unverified and must not be
    # admitted to formal scoring until they are re-extracted.
    evidence_source_kind: str = LEGACY_UNVERIFIED_EVIDENCE_KIND
    evidence_source_hash: str = ""
    evidence_parser_version: str = ""
    evidence_prompt_version: str = ""
    extractor_bundle_sha256: str = ""

    def __post_init__(self) -> None:
        for name in (
            "claim_id",
            "report_id",
            "target_type",
            "forecast_period",
            "evidence_span",
            "extractor_version",
        ):
            _require_text(getattr(self, name), name)
        _validate_dimension(self.dimension)
        ensure_aware(self.available_at, "available_at")
        if isinstance(self.direction, bool) or self.direction not in (-1, 0, 1):
            raise ModelValidationError("direction must be -1, 0, or 1")
        low = decimal_or_none(self.value_min)
        high = decimal_or_none(self.value_max)
        if low is not None and high is not None and low > high:
            raise ModelValidationError("value_min cannot exceed value_max")
        if low is None and high is None and self.direction == 0:
            raise ModelValidationError("claim needs a numeric value or non-zero direction")
        if self.horizon_days <= 0:
            raise ModelValidationError("horizon_days must be positive")
        if not 0.0 <= float(self.extraction_confidence) <= 1.0:
            raise ModelValidationError("extraction_confidence must be in [0, 1]")
        evidence_kind = str(self.evidence_source_kind or "").strip()
        evidence_hash = _normalise_sha256(
            self.evidence_source_hash,
            "evidence_source_hash",
            allow_empty=True,
        )
        evidence_parser_version = str(self.evidence_parser_version or "").strip()
        evidence_prompt_version = str(self.evidence_prompt_version or "").strip()
        extractor_bundle_sha256 = _normalise_sha256(
            self.extractor_bundle_sha256,
            "extractor_bundle_sha256",
            allow_empty=True,
        )
        if evidence_kind == LEGACY_UNVERIFIED_EVIDENCE_KIND:
            if (
                evidence_hash
                or evidence_parser_version
                or evidence_prompt_version
                or extractor_bundle_sha256
            ):
                raise ModelValidationError(
                    "legacy/unverified evidence cannot carry trusted source versions"
                )
        elif evidence_kind in CLAIM_EVIDENCE_SOURCE_KINDS:
            if not evidence_hash:
                raise ModelValidationError(
                    "verified claim evidence requires evidence_source_hash"
                )
            if not evidence_parser_version or not evidence_prompt_version:
                raise ModelValidationError(
                    "verified claim evidence requires parser and prompt versions"
                )
            if (
                evidence_kind == "textual/pdf"
                and evidence_parser_version in {"none", "source-record-v1"}
            ):
                raise ModelValidationError(
                    "textual PDF evidence requires the actual PDF parser version"
                )
        else:
            raise ModelValidationError(
                "evidence_source_kind must be structured/source_record, "
                "textual/pdf, or legacy/unverified"
            )
        object.__setattr__(self, "value_min", low)
        object.__setattr__(self, "value_max", high)
        object.__setattr__(self, "evidence_source_kind", evidence_kind)
        object.__setattr__(self, "evidence_source_hash", evidence_hash)
        object.__setattr__(
            self, "evidence_parser_version", evidence_parser_version
        )
        object.__setattr__(
            self, "evidence_prompt_version", evidence_prompt_version
        )
        object.__setattr__(
            self, "extractor_bundle_sha256", extractor_bundle_sha256
        )

    @property
    def evidence_is_bound(self) -> bool:
        """Whether formal scoring may rely on this claim's source evidence."""

        return (
            self.evidence_source_kind in CLAIM_EVIDENCE_SOURCE_KINDS
            and bool(self.evidence_source_hash)
            and bool(self.evidence_parser_version)
            and bool(self.evidence_prompt_version)
            and bool(self.extractor_bundle_sha256)
        )


@dataclass(frozen=True)
class TruthObservation:
    """One immutable, point-in-time version of an official realised value.

    An observation can bind directly to ``claim_id`` or use the complete
    dimension/subject/target/period locator.  Release versions are explicit so
    revisions can be retained without silently replacing the first release.
    """

    realized_value: Decimal
    truth_source: str
    available_at: datetime
    fetched_at: datetime
    first_release: bool
    revision: bool
    content_hash: str
    evidence_url: str
    claim_id: str = ""
    dimension: str = ""
    subject_id: str = ""
    target_type: str = ""
    forecast_period: str = ""
    observation_id: str = ""
    unit: str = ""
    basis: str = ""
    change_value: Decimal | None = None
    change_basis: str = ""
    evidence_verified: bool = False

    def __post_init__(self) -> None:
        for name in (
            "claim_id",
            "dimension",
            "subject_id",
            "target_type",
            "forecast_period",
            "unit",
            "basis",
            "change_basis",
        ):
            if not isinstance(getattr(self, name), str):
                raise ModelValidationError(f"{name} must be a string")
        _require_text(self.truth_source, "truth_source")
        _require_text(self.evidence_url, "evidence_url")
        ensure_aware(self.available_at, "available_at")
        ensure_aware(self.fetched_at, "fetched_at")
        if self.fetched_at < self.available_at:
            raise ModelValidationError("fetched_at cannot precede available_at")
        if type(self.first_release) is not bool or type(self.revision) is not bool:
            raise ModelValidationError("first_release and revision must be booleans")
        if type(self.evidence_verified) is not bool:
            raise ModelValidationError("evidence_verified must be a boolean")
        if self.first_release == self.revision:
            raise ModelValidationError(
                "exactly one of first_release and revision must be true"
            )

        claim_id = self.claim_id.strip()
        locator = tuple(
            getattr(self, name).strip()
            for name in ("dimension", "subject_id", "target_type", "forecast_period")
        )
        if any(locator) and not all(locator):
            raise ModelValidationError(
                "truth locator must include dimension, subject_id, target_type and forecast_period"
            )
        if not claim_id and not all(locator):
            raise ModelValidationError(
                "truth observation needs claim_id or a complete field locator"
            )
        if all(locator):
            _validate_dimension(locator[0])

        realized = decimal_or_none(self.realized_value)
        if realized is None:
            raise ModelValidationError("realized_value must not be empty")
        change_value = decimal_or_none(self.change_value)
        change_basis = self.change_basis.strip()
        if (change_value is None) != (not change_basis):
            raise ModelValidationError(
                "change_value and change_basis must be supplied together"
            )
        content_hash = self.content_hash.strip().lower()
        if len(content_hash) != 64 or any(
            character not in "0123456789abcdef" for character in content_hash
        ):
            raise ModelValidationError("content_hash must be a SHA-256 hex digest")

        truth_source = self.truth_source.strip()
        evidence_url = self.evidence_url.strip()
        for name, value in zip(
            ("dimension", "subject_id", "target_type", "forecast_period"), locator
        ):
            object.__setattr__(self, name, value)
        object.__setattr__(self, "claim_id", claim_id)
        object.__setattr__(self, "truth_source", truth_source)
        object.__setattr__(self, "evidence_url", evidence_url)
        object.__setattr__(self, "realized_value", realized)
        object.__setattr__(self, "unit", self.unit.strip())
        object.__setattr__(self, "basis", self.basis.strip())
        object.__setattr__(self, "change_value", change_value)
        object.__setattr__(self, "change_basis", change_basis)
        object.__setattr__(self, "content_hash", content_hash)

        identity_parts: list[object] = [
            "truth-observation-v1",
            claim_id,
            *locator,
            truth_source,
            self.available_at.astimezone(timezone.utc).isoformat(),
            self.first_release,
            self.revision,
            "0" if realized == 0 else format(realized.normalize(), "f"),
            content_hash,
        ]
        # Preserve IDs for pre-contract rows while making new unit/basis/change
        # evidence part of the immutable identity.
        if self.unit or self.basis or change_value is not None or change_basis:
            identity_parts.extend(
                (
                    self.unit,
                    self.basis,
                    ""
                    if change_value is None
                    else (
                        "0"
                        if change_value == 0
                        else format(change_value.normalize(), "f")
                    ),
                    change_basis,
                )
            )
        if self.evidence_verified:
            identity_parts.extend(("truth-evidence-v1", True))
        expected_id = stable_identifier(*identity_parts)
        if self.observation_id and self.observation_id != expected_id:
            raise ModelValidationError(
                "observation_id does not match the immutable observation payload"
            )
        object.__setattr__(self, "observation_id", expected_id)


@dataclass(frozen=True)
class ClaimOutcome:
    """Point-in-time truth and market outcome for one claim."""

    claim_id: str
    truth_source: str
    truth_available_at: datetime | None
    realized_value: Decimal | None
    market_return: float | None
    benchmark_return: float | None
    error: float | None
    hit: bool | None
    mature: bool
    exclusion_reason: str = ""
    evaluated_at: datetime | None = None
    fundamental_hit: bool | None = None
    market_hit: bool | None = None
    market_excess_return: float | None = None
    market_exclusion_reason: str = ""
    market_truth_source: str = ""
    market_benchmark_id: str = ""
    market_benchmark_kind: str = ""
    truth_unit: str = ""
    truth_basis: str = ""
    truth_change_value: Decimal | None = None
    truth_change_basis: str = ""

    def __post_init__(self) -> None:
        _require_text(self.claim_id, "claim_id")
        if self.truth_available_at is not None:
            ensure_aware(self.truth_available_at, "truth_available_at")
        if self.evaluated_at is not None:
            ensure_aware(self.evaluated_at, "evaluated_at")
        if (
            self.mature
            and self.truth_available_at is not None
            and self.evaluated_at is not None
            and self.truth_available_at > self.evaluated_at
        ):
            raise ModelValidationError(
                "mature outcome truth_available_at cannot follow evaluated_at"
            )
        realized = decimal_or_none(self.realized_value)
        object.__setattr__(self, "realized_value", realized)
        object.__setattr__(
            self, "truth_change_value", decimal_or_none(self.truth_change_value)
        )
        for name in (
            "market_benchmark_id",
            "market_benchmark_kind",
            "truth_unit",
            "truth_basis",
            "truth_change_basis",
        ):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise ModelValidationError(f"{name} must be a string")
            object.__setattr__(self, name, value.strip())
        for name in ("market_return", "benchmark_return", "error", "market_excess_return"):
            value = getattr(self, name)
            if value is not None and not float("-inf") < float(value) < float("inf"):
                raise ModelValidationError(f"{name} must be finite")
        if type(self.mature) is not bool:
            raise ModelValidationError("mature must be a boolean")
        if self.hit is not None and type(self.hit) is not bool:
            raise ModelValidationError("hit must be a boolean or None")
        for name in ("fundamental_hit", "market_hit"):
            value = getattr(self, name)
            if value is not None and type(value) is not bool:
                raise ModelValidationError(f"{name} must be a boolean or None")
        if self.mature and self.hit is None and not self.exclusion_reason:
            if self.fundamental_hit is None and self.market_hit is None:
                raise ModelValidationError(
                    "mature outcome needs a hit field or exclusion_reason"
                )


@dataclass(frozen=True)
class SkillSnapshot:
    """Historical source skill known at ``as_of`` (never self-weighted)."""

    as_of: datetime
    broker: str
    analyst: str
    team: str
    dimension: str
    target_type: str
    horizon_days: int
    posterior_skill: float
    conservative_lower_bound: float
    effective_sample_size: float
    source_report_ids: tuple[str, ...]
    market_state: str = ""
    industry_id: str = ""
    snapshot_id: str = ""
    broker_display: str = ""
    sensitivity_365: float | None = None
    sensitivity_365_lower_bound: float | None = None
    sensitivity_365_effective_sample_size: float | None = None
    sensitivity_delta: float | None = None

    def __post_init__(self) -> None:
        ensure_aware(self.as_of, "as_of")
        _validate_dimension(self.dimension)
        _require_text(self.broker, "broker")
        _require_text(self.target_type, "target_type")
        if self.horizon_days <= 0:
            raise ModelValidationError("horizon_days must be positive")
        if self.effective_sample_size < 0:
            raise ModelValidationError("effective_sample_size cannot be negative")
        for name in (
            "posterior_skill",
            "conservative_lower_bound",
            "effective_sample_size",
            "sensitivity_365",
            "sensitivity_365_lower_bound",
            "sensitivity_365_effective_sample_size",
            "sensitivity_delta",
        ):
            value = getattr(self, name)
            if value is not None and not float("-inf") < float(value) < float("inf"):
                raise ModelValidationError(f"{name} must be finite")
        for name in (
            "posterior_skill",
            "conservative_lower_bound",
            "sensitivity_365",
            "sensitivity_365_lower_bound",
        ):
            value = getattr(self, name)
            if value is not None and not 0.0 <= float(value) <= 1.0:
                raise ModelValidationError(f"{name} must be in [0, 1]")
        if self.conservative_lower_bound > self.posterior_skill:
            raise ModelValidationError(
                "conservative_lower_bound cannot exceed posterior_skill"
            )
        if (
            self.sensitivity_365_lower_bound is not None
            and self.sensitivity_365 is None
        ):
            raise ModelValidationError(
                "sensitivity_365_lower_bound requires sensitivity_365"
            )
        if (
            self.sensitivity_365 is not None
            and self.sensitivity_365_lower_bound is not None
            and self.sensitivity_365_lower_bound > self.sensitivity_365
        ):
            raise ModelValidationError(
                "sensitivity_365_lower_bound cannot exceed sensitivity_365"
            )
        if (
            self.sensitivity_365_effective_sample_size is not None
            and self.sensitivity_365_effective_sample_size < 0
        ):
            raise ModelValidationError(
                "sensitivity_365_effective_sample_size cannot be negative"
            )
        if len(self.source_report_ids) != len(set(self.source_report_ids)):
            raise ModelValidationError("source_report_ids must be unique")
        if not self.snapshot_id:
            object.__setattr__(
                self,
                "snapshot_id",
                stable_identifier(
                    self.as_of.isoformat(),
                    self.broker,
                    self.analyst,
                    self.team,
                    self.dimension,
                    self.target_type,
                    self.horizon_days,
                    self.market_state,
                    self.industry_id,
                ),
            )


@dataclass(frozen=True)
class FactorObservation:
    """Three-layer factors visible for one stock at one decision time."""

    as_of: datetime
    stock_id: str
    macro_objective_factor: float | None = None
    macro_report_factor: float | None = None
    industry_objective_factor: float | None = None
    industry_report_factor: float | None = None
    stock_objective_factor: float | None = None
    stock_report_factor: float | None = None
    macro_industry_interaction: float | None = None
    industry_stock_interaction: float | None = None
    source_snapshot_hash: str = ""
    # Appended for constructor compatibility with V1 callers that may still
    # instantiate the original fields positionally.
    macro_report_raw: float | None = None
    industry_report_raw: float | None = None
    stock_report_raw: float | None = None

    def __post_init__(self) -> None:
        ensure_aware(self.as_of, "as_of")
        _require_text(self.stock_id, "stock_id")
        _require_text(self.source_snapshot_hash, "source_snapshot_hash")
        for name in (
            "macro_objective_factor",
            "macro_report_raw",
            "macro_report_factor",
            "industry_objective_factor",
            "industry_report_raw",
            "industry_report_factor",
            "stock_objective_factor",
            "stock_report_raw",
            "stock_report_factor",
            "macro_industry_interaction",
            "industry_stock_interaction",
        ):
            value = getattr(self, name)
            if value is not None and not float("-inf") < float(value) < float("inf"):
                raise ModelValidationError(f"{name} must be finite")


@dataclass(frozen=True)
class DailyBar:
    """Daily market bar with raw and optional adjusted OHLC values."""

    instrument_id: str
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    amount: Decimal | None
    available_at: datetime
    source: str
    fetched_at: datetime
    content_hash: str
    adjusted_open: Decimal | None = None
    adjusted_high: Decimal | None = None
    adjusted_low: Decimal | None = None
    adjusted_close: Decimal | None = None
    suspended: bool = False

    def __post_init__(self) -> None:
        _require_text(self.instrument_id, "instrument_id")
        _require_text(self.source, "source")
        object.__setattr__(
            self,
            "content_hash",
            _normalise_sha256(self.content_hash, "content_hash"),
        )
        if isinstance(self.trade_date, datetime) or not isinstance(self.trade_date, date):
            raise ModelValidationError("trade_date must be a date")
        ensure_aware(self.available_at, "available_at")
        ensure_aware(self.fetched_at, "fetched_at")
        if self.fetched_at < self.available_at:
            raise ModelValidationError("fetched_at cannot precede available_at")
        converted: dict[str, Decimal | None] = {}
        for name in (
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            "adjusted_open",
            "adjusted_high",
            "adjusted_low",
            "adjusted_close",
        ):
            converted[name] = decimal_or_none(getattr(self, name))
            object.__setattr__(self, name, converted[name])
        if any(converted[name] is None for name in ("open", "high", "low", "close", "volume")):
            raise ModelValidationError("raw OHLCV values are required")
        if min(converted[name] for name in ("open", "high", "low", "close")) <= 0:  # type: ignore[type-var]
            raise ModelValidationError("OHLC prices must be positive")
        if converted["high"] < max(converted["open"], converted["close"]):  # type: ignore[type-var]
            raise ModelValidationError("high must cover open and close")
        if converted["low"] > min(converted["open"], converted["close"]):  # type: ignore[type-var]
            raise ModelValidationError("low must cover open and close")
        if converted["volume"] < 0:  # type: ignore[operator]
            raise ModelValidationError("volume cannot be negative")
        adjusted = tuple(converted[name] for name in (
            "adjusted_open",
            "adjusted_high",
            "adjusted_low",
            "adjusted_close",
        ))
        if any(value is not None for value in adjusted) and not all(
            value is not None for value in adjusted
        ):
            raise ModelValidationError("adjusted OHLC must be all present or all absent")
        if all(value is not None for value in adjusted) and min(adjusted) <= 0:  # type: ignore[type-var]
            raise ModelValidationError("adjusted OHLC prices must be positive")
        if type(self.suspended) is not bool:
            raise ModelValidationError("suspended must be a boolean")

    @property
    def evaluation_open(self) -> Decimal:
        """Prefer adjusted open; explicitly fall back to the raw open."""

        return self.adjusted_open if self.adjusted_open is not None else self.open

    @property
    def evaluation_close(self) -> Decimal:
        """Prefer adjusted close; explicitly fall back to the raw close."""

        return self.adjusted_close if self.adjusted_close is not None else self.close

    @property
    def evaluation_price_basis(self) -> str:
        """Make adjusted-price degradation visible to evaluators and reports."""

        return "adjusted" if self.adjusted_close is not None else "raw_unadjusted_fallback"
