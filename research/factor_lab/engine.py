"""Frozen CSI-11 relative-momentum factor research engine.

Evidence bundle contract
========================

The engine consumes one JSON object and never imports a provider SDK.  Unknown
fields are rejected.  Decimal prices are strings so transport parsing cannot
silently change them::

    {
      "schema_version": "factor-lab-evidence-bundle.v1",
      "bundle_id": "stable-source-batch-id",
      "stage": "screen | confirm | weekly",
      "source": {
        "source_id": "choice | csi",
        "source_authority": "licensed_secondary | official",
        "source_uri": "https://...",
        "adapter_version": "adapter-v1",
        "retrieved_at": "2026-08-13T12:00:00+08:00"
      },
      "receipt": {
        "transport": "factor_evidence_probe",
        "request_sha256": "64 lowercase hex characters",
        "response_sha256": "64 lowercase hex characters",
        "evidence_verified": false
      },
      "instruments": [
        {
          "instrument_id": "source-specific-id",
          "canonical_id": "CSI_ENERGY",
          "role": "industry | benchmark",
          "name": "能源"
        }
      ],
      "calendar": [
        {
          "trading_date": "2026-08-13",
          "is_trading_day": true,
          "available_at": "2026-08-13T09:00:00+08:00",
          "source_record_id": "calendar-row-id"
        }
      ],
      "bars": [
        {
          "instrument_id": "source-specific-id",
          "trading_date": "2026-08-13",
          "close": "123.45",
          "available_at": "2026-08-13T15:01:00+08:00",
          "source_record_id": "bar-row-id"
        }
      ]
    }

V1 ``screen`` accepts only the frozen Choice provider identity.  The separate
V2 experiment uses CSI's legacy series for Screen and CSI's current series for
Confirm; it is explicitly a same-source temporal/cross-generation holdout, not
an independent-source confirmation.  Both versions bind exact adapter,
source-record and code identities.  No method in this module creates orders or
imports an execution bridge.
"""

from __future__ import annotations

import csv
import json
import math
import random
import re
import statistics
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from typing import Any

from research.reproducibility import git_worktree_state


ARTIFACT_FILENAMES = (
    "hypothesis_card.json",
    "universe_manifest.json",
    "source_reconciliation.csv",
    "factor_observations.csv",
    "weekly_metrics.csv",
    "window_metrics.csv",
    "exceptions.csv",
    "factor_report.md",
    "run_manifest.json",
)

JSON_ARTIFACTS = frozenset({"hypothesis_card.json", "universe_manifest.json"})
CSV_ARTIFACTS = frozenset(
    {
        "source_reconciliation.csv",
        "factor_observations.csv",
        "weekly_metrics.csv",
        "window_metrics.csv",
        "exceptions.csv",
    }
)

FACTOR_OBSERVATION_FIELDS = (
    "stage",
    "window_id",
    "week_end",
    "label_start",
    "label_end",
    "candidate_id",
    "lookback_sessions",
    "industry_id",
    "industry_name",
    "signal",
    "signal_rank",
    "forward_excess_return_20d",
    "source_bundle_id",
)
WEEKLY_METRIC_FIELDS = (
    "stage",
    "window_id",
    "week_end",
    "candidate_id",
    "industry_count",
    "rank_ic",
    "gross_top3_bottom3_spread",
    "one_way_turnover",
)
WINDOW_METRIC_FIELDS = (
    "row_type",
    "stage",
    "candidate_id",
    "window_id",
    "industry_id",
    "week_count",
    "mean_ic",
    "median_ic",
    "ic_std",
    "overlap_adjusted_icir",
    "mean_gross_spread",
    "positive_ic_weeks",
    "positive_window_count",
    "bootstrap_lower_95",
    "bootstrap_p_value",
    "permutation_p_value",
    "holm_adjusted_p_value",
    "max_industry_contribution_share",
    "industry_contribution",
    "leave_one_industry_out_min_mean_ic",
    "passed",
    "gate_reasons",
    "selected_winner",
)
RECONCILIATION_FIELDS = (
    "canonical_id",
    "matched_return_dates",
    "screen_return_dates",
    "official_return_dates",
    "date_coverage",
    "median_abs_return_diff_bps",
    "p99_abs_return_diff_bps",
    "passed",
    "reason",
)
EXCEPTION_FIELDS = (
    "stage",
    "code",
    "severity",
    "week_end",
    "candidate_id",
    "industry_id",
    "message",
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_AWARE_TIME_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)

V1_HYPOTHESIS_ID = "csi11-relative-momentum-v1"
V2_HYPOTHESIS_ID = "csi11-relative-momentum-csi-same-source-holdout-v2"


class FactorLabError(ValueError):
    """Raised when frozen research or evidence contracts are violated."""


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_value(value: Any) -> str:
    return sha256(_canonical_json_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _require_exact_keys(
    value: Mapping[str, Any],
    *,
    required: set[str],
    context: str,
) -> None:
    actual = set(value)
    missing = sorted(required - actual)
    unknown = sorted(actual - required)
    if missing or unknown:
        raise FactorLabError(
            f"{context} fields are not frozen: missing={missing}, unknown={unknown}"
        )


def _parse_date(value: Any, context: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise FactorLabError(f"{context} must be an ISO date") from exc


def _parse_time(value: Any, context: str) -> datetime:
    text = str(value or "")
    if not _AWARE_TIME_RE.fullmatch(text):
        raise FactorLabError(f"{context} must be an offset-aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FactorLabError(f"{context} is not a valid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FactorLabError(f"{context} must include a UTC offset")
    return parsed


def _decimal(value: Any, context: str) -> Decimal:
    if not isinstance(value, str):
        raise FactorLabError(f"{context} must be a decimal string")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise FactorLabError(f"{context} must be a decimal string") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise FactorLabError(f"{context} must be finite and positive")
    return parsed


def _finite(value: Any, context: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise FactorLabError(f"{context} must be finite numeric") from exc
    if not math.isfinite(parsed):
        raise FactorLabError(f"{context} must be finite numeric")
    return parsed


def _quantile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


@dataclass(frozen=True)
class FactorSpec:
    """One immutable factor candidate in the preregistered family."""

    candidate_id: str
    lookback_sessions: int
    expected_sign: str = "positive"
    formula: str = "log_industry_close_return_minus_log_benchmark_close_return"

    def __post_init__(self) -> None:
        expected_id = f"RM{int(self.lookback_sessions)}"
        if self.candidate_id != expected_id:
            raise FactorLabError(
                f"candidate_id must equal {expected_id} for its frozen lookback"
            )
        if self.lookback_sessions not in {20, 60, 120}:
            raise FactorLabError("V1 permits only RM20, RM60 and RM120")
        if self.expected_sign != "positive":
            raise FactorLabError("V1 expected_sign is frozen to positive")
        if self.formula != "log_industry_close_return_minus_log_benchmark_close_return":
            raise FactorLabError("V1 formula is frozen")


@dataclass(frozen=True)
class FactorObservation:
    """One industry/candidate observation at an official week-end session."""

    stage: str
    window_id: str
    week_end: date
    label_start: date | None
    label_end: date | None
    candidate_id: str
    lookback_sessions: int
    industry_id: str
    industry_name: str
    signal: float
    signal_rank: float
    forward_excess_return_20d: float | None
    source_bundle_id: str

    def as_csv_row(self) -> dict[str, Any]:
        row = asdict(self)
        for field in ("week_end", "label_start", "label_end"):
            value = row[field]
            row[field] = value.isoformat() if isinstance(value, date) else ""
        return row


class FactorPlugin(ABC):
    """Narrow interface for a preregistered, non-provider factor family."""

    plugin_id: str

    @property
    @abstractmethod
    def specs(self) -> tuple[FactorSpec, ...]:
        """Return the complete frozen candidate family."""

    @abstractmethod
    def compute(
        self,
        spec: FactorSpec,
        *,
        industry_now: Decimal,
        industry_then: Decimal,
        benchmark_now: Decimal,
        benchmark_then: Decimal,
    ) -> float:
        """Compute one point-in-time signal without inspecting labels."""

    def inventory(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "research_only": True,
            "trading_bridge": "not_supported",
            "candidates": [asdict(spec) for spec in self.specs],
        }


class RelativeMomentumPlugin(FactorPlugin):
    """Frozen RM20/RM60/RM120 industry-relative momentum family."""

    plugin_id = "csi11-relative-momentum-v1"
    _specs = tuple(FactorSpec(f"RM{window}", window) for window in (20, 60, 120))

    @property
    def specs(self) -> tuple[FactorSpec, ...]:
        return self._specs

    def compute(
        self,
        spec: FactorSpec,
        *,
        industry_now: Decimal,
        industry_then: Decimal,
        benchmark_now: Decimal,
        benchmark_then: Decimal,
    ) -> float:
        del spec
        # Frozen formula: log(C_t/C_t-L) - log(B_t/B_t-L).  Convert only the
        # final positive Decimal ratios to float because ``math.log`` is the
        # statistical implementation used throughout V1.
        return math.log(float(industry_now / industry_then)) - math.log(
            float(benchmark_now / benchmark_then)
        )


@dataclass(frozen=True)
class Instrument:
    instrument_id: str
    canonical_id: str
    role: str
    name: str


@dataclass(frozen=True)
class Bar:
    instrument_id: str
    canonical_id: str
    trading_date: date
    close: Decimal
    available_at: datetime
    source_record_id: str


@dataclass(frozen=True)
class EvidenceBundle:
    """Validated provider-neutral evidence; construct with :meth:`from_json`."""

    raw: Mapping[str, Any]
    bundle_id: str
    stage: str
    source: Mapping[str, Any]
    receipt: Mapping[str, Any]
    instruments: tuple[Instrument, ...]
    calendar: tuple[date, ...]
    bars: tuple[Bar, ...]
    evidence_sha256: str
    source_bundle_sha256: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, value: Path | str | Mapping[str, Any]) -> "EvidenceBundle":
        if isinstance(value, Mapping):
            payload = dict(value)
        else:
            path = Path(value)
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise FactorLabError(f"cannot load evidence bundle: {exc}") from exc
        if not isinstance(payload, dict):
            raise FactorLabError("evidence bundle root must be an object")
        _require_exact_keys(
            payload,
            required={
                "schema_version",
                "bundle_id",
                "stage",
                "source",
                "receipt",
                "instruments",
                "calendar",
                "bars",
            },
            context="evidence bundle",
        )
        if payload["schema_version"] != "factor-lab-evidence-bundle.v1":
            raise FactorLabError("unsupported evidence bundle schema_version")
        bundle_id = str(payload["bundle_id"] or "").strip()
        if not bundle_id:
            raise FactorLabError("bundle_id is required")
        stage = str(payload["stage"] or "")
        if stage not in {"screen", "confirm", "weekly"}:
            raise FactorLabError("evidence stage must be screen, confirm or weekly")

        source = payload["source"]
        receipt = payload["receipt"]
        if not isinstance(source, dict) or not isinstance(receipt, dict):
            raise FactorLabError("source and receipt must be objects")
        _require_exact_keys(
            source,
            required={
                "source_id",
                "source_authority",
                "source_uri",
                "adapter_version",
                "retrieved_at",
            },
            context="source",
        )
        _require_exact_keys(
            receipt,
            required={
                "transport",
                "request_sha256",
                "response_sha256",
                "evidence_verified",
            },
            context="receipt",
        )
        for field in ("source_id", "source_authority", "source_uri", "adapter_version"):
            if not str(source[field] or "").strip():
                raise FactorLabError(f"source.{field} is required")
        _parse_time(source["retrieved_at"], "source.retrieved_at")
        for field in ("request_sha256", "response_sha256"):
            if not _SHA256_RE.fullmatch(str(receipt[field] or "")):
                raise FactorLabError(f"receipt.{field} must be lowercase SHA-256")
        if type(receipt["evidence_verified"]) is not bool:
            raise FactorLabError("receipt.evidence_verified must be boolean")

        raw_instruments = payload["instruments"]
        if not isinstance(raw_instruments, list) or not raw_instruments:
            raise FactorLabError("instruments must be a non-empty array")
        instruments: list[Instrument] = []
        instrument_ids: set[str] = set()
        canonical_ids: set[str] = set()
        for index, raw in enumerate(raw_instruments):
            if not isinstance(raw, dict):
                raise FactorLabError(f"instruments[{index}] must be an object")
            _require_exact_keys(
                raw,
                required={"instrument_id", "canonical_id", "role", "name"},
                context=f"instruments[{index}]",
            )
            instrument = Instrument(
                instrument_id=str(raw["instrument_id"] or "").strip(),
                canonical_id=str(raw["canonical_id"] or "").strip(),
                role=str(raw["role"] or "").strip(),
                name=str(raw["name"] or "").strip(),
            )
            if (
                not instrument.instrument_id
                or not instrument.canonical_id
                or not instrument.name
                or instrument.role
                not in {"industry", "benchmark", "reconciliation_industry"}
            ):
                raise FactorLabError(f"instruments[{index}] is incomplete")
            if instrument.instrument_id in instrument_ids:
                raise FactorLabError("duplicate source instrument_id")
            if instrument.canonical_id in canonical_ids:
                raise FactorLabError("duplicate canonical_id")
            instrument_ids.add(instrument.instrument_id)
            canonical_ids.add(instrument.canonical_id)
            instruments.append(instrument)
        by_instrument = {item.instrument_id: item for item in instruments}

        raw_calendar = payload["calendar"]
        if not isinstance(raw_calendar, list) or not raw_calendar:
            raise FactorLabError("calendar must be a non-empty array")
        calendar: list[date] = []
        calendar_seen: set[date] = set()
        for index, raw in enumerate(raw_calendar):
            if not isinstance(raw, dict):
                raise FactorLabError(f"calendar[{index}] must be an object")
            _require_exact_keys(
                raw,
                required={
                    "trading_date",
                    "is_trading_day",
                    "available_at",
                    "source_record_id",
                },
                context=f"calendar[{index}]",
            )
            trading_day = _parse_date(raw["trading_date"], f"calendar[{index}].trading_date")
            if type(raw["is_trading_day"]) is not bool:
                raise FactorLabError(f"calendar[{index}].is_trading_day must be boolean")
            _parse_time(raw["available_at"], f"calendar[{index}].available_at")
            if not str(raw["source_record_id"] or "").strip():
                raise FactorLabError(f"calendar[{index}].source_record_id is required")
            if trading_day in calendar_seen:
                raise FactorLabError("duplicate calendar date")
            calendar_seen.add(trading_day)
            if raw["is_trading_day"]:
                calendar.append(trading_day)
        if calendar != sorted(calendar) or not calendar:
            raise FactorLabError("trading calendar must be non-empty and strictly ascending")

        raw_bars = payload["bars"]
        if not isinstance(raw_bars, list) or not raw_bars:
            raise FactorLabError("bars must be a non-empty array")
        bars: list[Bar] = []
        bar_seen: set[tuple[str, date]] = set()
        for index, raw in enumerate(raw_bars):
            if not isinstance(raw, dict):
                raise FactorLabError(f"bars[{index}] must be an object")
            _require_exact_keys(
                raw,
                required={
                    "instrument_id",
                    "trading_date",
                    "close",
                    "available_at",
                    "source_record_id",
                },
                context=f"bars[{index}]",
            )
            instrument_id = str(raw["instrument_id"] or "").strip()
            instrument = by_instrument.get(instrument_id)
            if instrument is None:
                raise FactorLabError(f"bars[{index}] references an unknown instrument")
            trading_day = _parse_date(raw["trading_date"], f"bars[{index}].trading_date")
            if trading_day not in calendar_seen or trading_day not in set(calendar):
                raise FactorLabError(f"bars[{index}] is not on an official trading session")
            key = (instrument_id, trading_day)
            if key in bar_seen:
                raise FactorLabError("duplicate instrument/date bar")
            bar_seen.add(key)
            bars.append(
                Bar(
                    instrument_id=instrument_id,
                    canonical_id=instrument.canonical_id,
                    trading_date=trading_day,
                    close=_decimal(raw["close"], f"bars[{index}].close"),
                    available_at=_parse_time(
                        raw["available_at"], f"bars[{index}].available_at"
                    ),
                    source_record_id=str(raw["source_record_id"] or "").strip(),
                )
            )
            if not bars[-1].source_record_id:
                raise FactorLabError(f"bars[{index}].source_record_id is required")
        bars.sort(key=lambda item: (item.trading_date, item.canonical_id))
        return cls(
            raw=payload,
            bundle_id=bundle_id,
            stage=stage,
            source=source,
            receipt=receipt,
            instruments=tuple(instruments),
            calendar=tuple(calendar),
            bars=tuple(bars),
            evidence_sha256=_sha256_value(payload),
        )

    @property
    def instrument_by_canonical(self) -> dict[str, Instrument]:
        return {item.canonical_id: item for item in self.instruments}

    @property
    def bar_by_key(self) -> dict[tuple[str, date], Bar]:
        return {(item.canonical_id, item.trading_date): item for item in self.bars}


def _average_ranks(values: Sequence[float]) -> list[float]:
    """Return deterministic 1-based average ranks, including ties."""

    order = sorted(range(len(values)), key=lambda index: (values[index], index))
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(order):
        end = cursor + 1
        while end < len(order) and values[order[end]] == values[order[cursor]]:
            end += 1
        average = ((cursor + 1) + end) / 2.0
        for position in range(cursor, end):
            ranks[order[position]] = average
        cursor = end
    return ranks


def _pearson(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right) or len(left) < 3:
        return None
    left_mean = statistics.fmean(left)
    right_mean = statistics.fmean(right)
    left_centered = [value - left_mean for value in left]
    right_centered = [value - right_mean for value in right]
    left_sum = sum(value * value for value in left_centered)
    right_sum = sum(value * value for value in right_centered)
    if left_sum <= 0.0 or right_sum <= 0.0:
        return None
    return sum(
        left_value * right_value
        for left_value, right_value in zip(left_centered, right_centered)
    ) / math.sqrt(left_sum * right_sum)


def _spearman(left: Sequence[float], right: Sequence[float]) -> float | None:
    return _pearson(_average_ranks(left), _average_ranks(right))


def _week_end_sessions(calendar: Sequence[date]) -> list[date]:
    """Return the last official trading session in each ISO week."""

    result: list[date] = []
    for trading_day in calendar:
        if result and result[-1].isocalendar()[:2] == trading_day.isocalendar()[:2]:
            result[-1] = trading_day
        else:
            result.append(trading_day)
    return result


def _hac_overlap_adjusted_icir(
    values: Sequence[float], *, overlap_weeks: int = 5
) -> float | None:
    """Conservative ICIR using a Bartlett HAC variance for overlapping labels.

    The 20-session label overlaps roughly five weekly observations.  The
    annualisation factor is therefore ``sqrt(52 / 5)`` rather than ``sqrt(52)``.
    Block bootstrap and permutation inference below use the same five-week
    dependence unit.
    """

    if len(values) < overlap_weeks * 2:
        return None
    mean_value = statistics.fmean(values)
    centered = [value - mean_value for value in values]
    count = len(centered)
    long_run_variance = sum(value * value for value in centered) / count
    for lag in range(1, overlap_weeks):
        covariance = sum(
            centered[index] * centered[index - lag]
            for index in range(lag, count)
        ) / count
        long_run_variance += 2.0 * (1.0 - lag / overlap_weeks) * covariance
    if long_run_variance <= 0.0:
        return None
    return mean_value / math.sqrt(long_run_variance) * math.sqrt(52.0 / overlap_weeks)


def _moving_block_sample(
    values: Sequence[float], *, block_length: int, random_source: random.Random
) -> list[float]:
    if not values:
        return []
    sampled: list[float] = []
    while len(sampled) < len(values):
        start = random_source.randrange(len(values))
        sampled.extend(
            values[(start + offset) % len(values)] for offset in range(block_length)
        )
    return sampled[: len(values)]


def _block_bootstrap(
    values: Sequence[float],
    *,
    block_length: int,
    resamples: int,
    seed: int,
) -> tuple[float | None, float | None]:
    if len(values) < block_length * 2:
        return None, None
    random_source = random.Random(seed)
    boot_means = [
        statistics.fmean(
            _moving_block_sample(
                values, block_length=block_length, random_source=random_source
            )
        )
        for _ in range(resamples)
    ]
    lower = _quantile(boot_means, 0.05)
    p_value = (1 + sum(value <= 0.0 for value in boot_means)) / (resamples + 1)
    return lower, p_value


def _cross_sectional_label_permutation_p_value(
    observations: Sequence[FactorObservation],
    *,
    resamples: int,
    seed: int,
) -> float | None:
    """Confirm-only null: shuffle 11 labels within each weekly cross-section."""

    by_week: dict[date, list[FactorObservation]] = defaultdict(list)
    for item in observations:
        by_week[item.week_end].append(item)
    weeks = [sorted(items, key=lambda item: item.industry_id) for _, items in sorted(by_week.items())]
    if not weeks or any(len(items) != 11 for items in weeks):
        return None
    observed_values = [
        _spearman(
            [item.signal for item in items],
            [float(item.forward_excess_return_20d) for item in items],
        )
        for items in weeks
    ]
    if any(value is None for value in observed_values):
        return None
    observed = statistics.fmean(float(value) for value in observed_values)
    random_source = random.Random(seed)
    exceedances = 0
    for _ in range(resamples):
        permuted_ics: list[float] = []
        for items in weeks:
            labels = [float(item.forward_excess_return_20d) for item in items]
            random_source.shuffle(labels)
            value = _spearman([item.signal for item in items], labels)
            if value is None:
                return None
            permuted_ics.append(value)
        if statistics.fmean(permuted_ics) >= observed:
            exceedances += 1
    return (exceedances + 1) / (resamples + 1)


def _holm_adjust(p_values: Mapping[str, float | None]) -> dict[str, float | None]:
    """Holm adjusted p-values over the complete preregistered family."""

    if any(value is None for value in p_values.values()):
        return {key: None for key in p_values}
    ordered = sorted(
        ((key, float(value)) for key, value in p_values.items() if value is not None),
        key=lambda item: (item[1], item[0]),
    )
    total = len(ordered)
    adjusted: dict[str, float | None] = {}
    running = 0.0
    for index, (key, value) in enumerate(ordered):
        running = max(running, min(1.0, (total - index) * value))
        adjusted[key] = running
    return adjusted


def _seed_from(*parts: Any) -> int:
    return int(_sha256_value(list(parts))[:16], 16)


def _format_number(value: float | int | None) -> str:
    if value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        raise FactorLabError("attempted to write a non-finite metric")
    return format(value, ".12g")


def _load_hypothesis(value: Path | str | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(value, Mapping):
        payload = dict(value)
    else:
        path = Path(value)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FactorLabError(f"cannot load hypothesis card: {exc}") from exc
    if not isinstance(payload, dict):
        raise FactorLabError("hypothesis card root must be an object")
    _validate_hypothesis(payload)
    return payload


def _validate_hypothesis(payload: Mapping[str, Any]) -> None:
    _require_exact_keys(
        payload,
        required={
            "schema_version",
            "factor_hypothesis",
            "subjective_thesis_path",
            "universe",
            "candidates",
            "observation",
            "stages",
            "statistics",
            "source_policy",
            "time_policy",
            "winner_rule",
            "artifacts",
            "safety",
        },
        context="hypothesis card",
    )
    if payload["schema_version"] != "factor-lab-config.v1":
        raise FactorLabError("unsupported factor lab config schema_version")
    if payload["subjective_thesis_path"] != "configs/factor_hypotheses/subjective_thesis.csi11.v1.json":
        raise FactorLabError("V1 subjective thesis path is frozen")
    card = payload["factor_hypothesis"]
    if not isinstance(card, dict):
        raise FactorLabError("factor_hypothesis must be an object")
    _require_exact_keys(
        card,
        required={
            "schema_version",
            "hypothesis_id",
            "created_at",
            "decision_time",
            "universe_version",
            "screen_index_ids",
            "confirm_index_ids",
            "benchmark_id",
            "signal_name",
            "signal_definition",
            "direction",
            "horizons_trading_days",
            "entry_price_policy",
            "exit_price_policy",
            "benchmark_policy",
            "required_fields",
            "subjective_thesis_id",
            "subjective_thesis_sha256",
            "confounders",
            "falsification_conditions",
            "status",
        },
        context="factor_hypothesis",
    )
    if card["schema_version"] != "factor-hypothesis-v1":
        raise FactorLabError("factor_hypothesis schema_version is unsupported")
    hypothesis_id = str(card["hypothesis_id"])
    if hypothesis_id not in {V1_HYPOTHESIS_ID, V2_HYPOTHESIS_ID}:
        raise FactorLabError("factor lab hypothesis_id is outside the frozen versions")
    same_source_v2 = hypothesis_id == V2_HYPOTHESIS_ID
    _parse_time(card["created_at"], "factor_hypothesis.created_at")
    _parse_time(card["decision_time"], "factor_hypothesis.decision_time")
    expected_card_values = {
        "universe_version": "csi-level1-screen-confirm-v1",
        "benchmark_id": "000985.CSI",
        "signal_definition": "log(C_t/C_t-L)-log(B_t/B_t-L), L in {20,60,120} trading sessions",
        "direction": "positive",
        "horizons_trading_days": [20, 60, 120],
        "entry_price_policy": "next_trading_session_close",
        "exit_price_policy": "nth_future_trading_session_close",
        "benchmark_policy": "same_sessions_same_price_field",
        "subjective_thesis_id": "csi11-user-view",
        "status": "preregistered",
    }
    for field, expected in expected_card_values.items():
        if card[field] != expected:
            raise FactorLabError(f"factor_hypothesis.{field} is frozen")
    if not _SHA256_RE.fullmatch(str(card["subjective_thesis_sha256"])):
        raise FactorLabError("factor_hypothesis subjective thesis hash is invalid")
    for field in ("signal_name", "required_fields", "confounders", "falsification_conditions"):
        if not card[field]:
            raise FactorLabError(f"factor_hypothesis.{field} is required")

    universe = payload["universe"]
    if not isinstance(universe, dict):
        raise FactorLabError("universe must be an object")
    _require_exact_keys(
        universe,
        required={
            "classification",
            "point_in_time",
            "benchmark",
            "industries",
            "source_index_ids",
        },
        context="universe",
    )
    if universe["classification"] != "CSI_PRIMARY_11" or universe["point_in_time"] is not True:
        raise FactorLabError("V1 requires point-in-time CSI primary 11 classification")
    benchmark = universe["benchmark"]
    if not isinstance(benchmark, dict):
        raise FactorLabError("universe.benchmark must be an object")
    _require_exact_keys(
        benchmark,
        required={"canonical_id", "name"},
        context="universe.benchmark",
    )
    industries = universe["industries"]
    if not isinstance(industries, list) or len(industries) != 11:
        raise FactorLabError("V1 universe must contain exactly 11 industries")
    canonical_ids: set[str] = set()
    for index, item in enumerate(industries):
        if not isinstance(item, dict):
            raise FactorLabError(f"universe.industries[{index}] must be an object")
        _require_exact_keys(
            item,
            required={"canonical_id", "name"},
            context=f"universe.industries[{index}]",
        )
        canonical_id = str(item["canonical_id"] or "")
        if not canonical_id or not str(item["name"] or "") or canonical_id in canonical_ids:
            raise FactorLabError("industry identifiers and names must be unique and non-empty")
        canonical_ids.add(canonical_id)
    if benchmark["canonical_id"] in canonical_ids:
        raise FactorLabError("benchmark cannot also be an industry")
    source_index_ids = universe["source_index_ids"]
    if not isinstance(source_index_ids, dict):
        raise FactorLabError("universe.source_index_ids must be an object")
    _require_exact_keys(
        source_index_ids,
        required=(
            {"csi_legacy_screen", "csi_confirm", "benchmark"}
            if same_source_v2
            else {
                "choice_screen",
                "choice_current_reconciliation",
                "csi_confirm",
                "benchmark",
            }
        ),
        context="universe.source_index_ids",
    )
    expected_old = (
        "000986",
        "000987",
        "000988",
        "000989",
        "000990",
        "000991",
        "932075",
        "000993",
        "000994",
        "000995",
        "932076",
    )
    expected_current = (
        "932077",
        "932078",
        "932079",
        "932080",
        "932081",
        "932082",
        "932083",
        "932084",
        "932085",
        "932086",
        "931775",
    )
    ordered_ids = [str(item["canonical_id"]) for item in industries]
    screen_mapping_key = "csi_legacy_screen" if same_source_v2 else "choice_screen"
    if source_index_ids[screen_mapping_key] != dict(zip(ordered_ids, expected_old)):
        raise FactorLabError("legacy screen index mapping is frozen")
    if not same_source_v2 and source_index_ids["choice_current_reconciliation"] != dict(
        zip(ordered_ids, expected_current)
    ):
        raise FactorLabError("Choice current reconciliation index mapping is frozen")
    if source_index_ids["csi_confirm"] != dict(zip(ordered_ids, expected_current)):
        raise FactorLabError("CSI confirmation index mapping is frozen")
    if source_index_ids["benchmark"] != "000985":
        raise FactorLabError("V1 benchmark source index is frozen")
    if card["screen_index_ids"] != [f"{value}.CSI" for value in expected_old]:
        raise FactorLabError("factor_hypothesis screen_index_ids mismatch runner mapping")
    if card["confirm_index_ids"] != [f"{value}.CSI" for value in expected_current]:
        raise FactorLabError("factor_hypothesis confirm_index_ids mismatch runner mapping")

    candidates = payload["candidates"]
    if not isinstance(candidates, list):
        raise FactorLabError("candidates must be an array")
    frozen_specs = [asdict(spec) for spec in RelativeMomentumPlugin().specs]
    if candidates != frozen_specs:
        raise FactorLabError("candidate family must be exactly RM20, RM60 and RM120")

    observation = payload["observation"]
    if not isinstance(observation, dict):
        raise FactorLabError("observation must be an object")
    _require_exact_keys(
        observation,
        required={
            "frequency",
            "signal_price",
            "label",
            "label_horizon_sessions",
            "complete_case",
        },
        context="observation",
    )
    expected_observation = {
        "frequency": "weekly_last_trading_session",
        "signal_price": "close",
        "label": "next_session_close_to_20th_future_session_close",
        "label_horizon_sessions": 20,
        "complete_case": True,
    }
    if observation != expected_observation:
        raise FactorLabError("V1 observation and label contract is frozen")

    stages = payload["stages"]
    if not isinstance(stages, dict):
        raise FactorLabError("stages must be an object")
    _require_exact_keys(stages, required={"screen", "confirm"}, context="stages")
    for stage, total_weeks, windows in (("screen", 260, 5), ("confirm", 104, 2)):
        policy = stages[stage]
        if not isinstance(policy, dict):
            raise FactorLabError(f"stages.{stage} must be an object")
        _require_exact_keys(
            policy,
            required={
                "total_weeks",
                "window_count",
                "weeks_per_window",
                "minimum_total_weeks",
                "minimum_weeks_per_window",
                "gates",
            },
            context=f"stages.{stage}",
        )
        if (
            policy["total_weeks"] != total_weeks
            or policy["window_count"] != windows
            or policy["weeks_per_window"] != 52
            or total_weeks != windows * 52
        ):
            raise FactorLabError(f"{stage} window geometry is frozen")
        minimum_total_weeks = int(policy["minimum_total_weeks"])
        minimum_per_window = int(policy["minimum_weeks_per_window"])
        expected_minimum = 220 if stage == "screen" else 88
        if minimum_total_weeks != expected_minimum or minimum_per_window != 44:
            raise FactorLabError(f"{stage} coverage gate is invalid")
        gates = policy["gates"]
        if not isinstance(gates, dict):
            raise FactorLabError(f"stages.{stage}.gates must be an object")
        _require_exact_keys(
            gates,
            required=(
                {
                    "minimum_combined_mean_ic",
                    "minimum_positive_ic_windows",
                    "minimum_positive_spread_windows",
                    "maximum_holm_p_value",
                }
                if stage == "screen"
                else {
                    "minimum_combined_mean_ic",
                    "minimum_positive_ic_windows",
                    "bootstrap_lower_95_strictly_positive",
                    "minimum_combined_gross_spread",
                    "minimum_positive_leave_one_industry_out_count",
                    "maximum_industry_contribution_share",
                    "maximum_permutation_p_value",
                }
            ),
            context=f"stages.{stage}.gates",
        )
        for key, value in gates.items():
            if key != "bootstrap_lower_95_strictly_positive":
                _finite(value, f"stages.{stage}.gates.{key}")
        if stage == "confirm" and gates["bootstrap_lower_95_strictly_positive"] is not True:
            raise FactorLabError("confirm bootstrap lower bound gate cannot be disabled")

    statistics_policy = payload["statistics"]
    if not isinstance(statistics_policy, dict):
        raise FactorLabError("statistics must be an object")
    _require_exact_keys(
        statistics_policy,
        required={
            "block_length_weeks",
            "resamples",
            "familywise_alpha",
            "multiple_testing",
            "permutation",
            "portfolio",
        },
        context="statistics",
    )
    if (
        statistics_policy["block_length_weeks"] != 5
        or statistics_policy["resamples"] != 10000
        or statistics_policy["multiple_testing"] != "holm"
        or statistics_policy["permutation"]
        != "confirm_weekly_cross_sectional_label_shuffle"
    ):
        raise FactorLabError("V1 inference settings are frozen")
    alpha = _finite(statistics_policy["familywise_alpha"], "statistics.familywise_alpha")
    if alpha != 0.05:
        raise FactorLabError("V1 familywise alpha is frozen at 0.05")
    portfolio = statistics_policy["portfolio"]
    if not isinstance(portfolio, dict):
        raise FactorLabError("statistics.portfolio must be an object")
    _require_exact_keys(
        portfolio,
        required={"long_count", "short_count"},
        context="statistics.portfolio",
    )
    if portfolio != {"long_count": 3, "short_count": 3}:
        raise FactorLabError("V1 descriptive portfolio is frozen")

    source_policy = payload["source_policy"]
    if not isinstance(source_policy, dict):
        raise FactorLabError("source_policy must be an object")
    _require_exact_keys(
        source_policy,
        required={"screen", "confirm", "weekly", "official_transport_status", "reconciliation"},
        context="source_policy",
    )
    expected_sources = {
        "screen": (
            ("csi", "official", "index_evidence_service", True)
            if same_source_v2
            else ("choice", "licensed_secondary", "factor_evidence_probe", True)
        ),
        "confirm": ("csi", "official", "factor_evidence_probe", True),
        "weekly": ("csi", "official", "factor_evidence_probe", True),
    }
    for stage, expected in expected_sources.items():
        item = source_policy[stage]
        if not isinstance(item, dict):
            raise FactorLabError(f"source_policy.{stage} must be an object")
        _require_exact_keys(
            item,
            required={"source_id", "source_authority", "transport", "integrity_flag_required"},
            context=f"source_policy.{stage}",
        )
        observed = (
            item["source_id"],
            item["source_authority"],
            item["transport"],
            item["integrity_flag_required"],
        )
        if observed != expected:
            raise FactorLabError(f"source_policy.{stage} is frozen")
    if source_policy["official_transport_status"] != "not_configured":
        raise FactorLabError("V1 cannot claim a source-owned official transport")
    reconciliation = source_policy["reconciliation"]
    if not isinstance(reconciliation, dict):
        raise FactorLabError("source_policy.reconciliation must be an object")
    if same_source_v2:
        if reconciliation != {"mode": "not_applicable_same_source"}:
            raise FactorLabError("V2 source reconciliation must be not_applicable_same_source")
    else:
        _require_exact_keys(
            reconciliation,
            required={
                "minimum_matched_return_dates",
                "minimum_date_coverage",
                "maximum_median_abs_return_diff_bps",
                "maximum_p99_abs_return_diff_bps",
            },
            context="source_policy.reconciliation",
        )
        if reconciliation != {
            "minimum_matched_return_dates": 60,
            "minimum_date_coverage": 0.995,
            "maximum_median_abs_return_diff_bps": 1.0,
            "maximum_p99_abs_return_diff_bps": 5.0,
        }:
            raise FactorLabError("V1 source reconciliation thresholds are frozen")

    time_policy = payload["time_policy"]
    if time_policy != {
        "screen_signal_cutoff": "2023-03-10",
        "confirm_series_inception": "2023-03-13",
        "screen_labels_may_mature_after_cutoff": False,
    }:
        raise FactorLabError("V1 time boundary is frozen")

    winner_rule = payload["winner_rule"]
    if winner_rule != {
        "primary": "highest_median_window_ic_among_passers",
        "near_tie_absolute_ic_difference_strictly_below": 0.01,
        "near_tie_tiebreaker": "shorter_lookback",
        "confirm_cannot_replace_screen_winner": True,
    }:
        raise FactorLabError("V1 winner rule is frozen")
    if payload["artifacts"] != list(ARTIFACT_FILENAMES):
        raise FactorLabError("artifact list must match the fixed nine-file contract")
    if payload["safety"] != {
        "mode": "research_only",
        "live": "not_supported",
        "trading_bridge": "forbidden",
    }:
        raise FactorLabError("factor lab safety boundary is frozen")


@dataclass
class _Evaluation:
    stage: str
    expected_week_count: int
    selected_week_ends: list[date]
    observations: list[FactorObservation]
    weekly_metrics: list[dict[str, Any]]
    window_metrics: list[dict[str, Any]]
    exceptions: list[dict[str, Any]]
    summaries: dict[str, dict[str, Any]]
    coverage: float
    valid_weeks_by_window: dict[str, int]
    selected_winner: str | None


class ExperimentRunner:
    """Run a frozen experiment and publish only controlled nine-file runs."""

    DEFAULT_HYPOTHESIS_PATH = (
        Path(__file__).resolve().parents[2]
        / "configs"
        / "factor_hypotheses"
        / "csi11_relative_momentum.v1.json"
    )

    def __init__(
        self,
        hypothesis: Path | str | Mapping[str, Any] | None = None,
        *,
        plugin: FactorPlugin | None = None,
    ) -> None:
        self.hypothesis = _load_hypothesis(
            hypothesis if hypothesis is not None else self.DEFAULT_HYPOTHESIS_PATH
        )
        self.hypothesis_card = dict(self.hypothesis["factor_hypothesis"])
        self.hypothesis_sha256 = _sha256_value(self.hypothesis_card)
        self.config_sha256 = _sha256_value(self.hypothesis)
        thesis_path = (
            Path(__file__).resolve().parents[2]
            / str(self.hypothesis["subjective_thesis_path"])
        )
        try:
            thesis = json.loads(thesis_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FactorLabError(f"cannot load subjective thesis: {exc}") from exc
        if not isinstance(thesis, dict):
            raise FactorLabError("subjective thesis root must be an object")
        _require_exact_keys(
            thesis,
            required={
                "schema_version",
                "thesis_id",
                "version",
                "parent_thesis_sha256",
                "content_sha256",
                "created_at",
                "as_of",
                "author_scope",
                "statement",
                "preferred_index_ids",
                "avoided_index_ids",
                "confidence_0_to_1",
                "evidence_for",
                "evidence_against",
                "invalidation_conditions",
                "expires_at",
                "status",
            },
            context="subjective thesis",
        )
        if (
            thesis["schema_version"] != "subjective-thesis-v1"
            or thesis["thesis_id"] != self.hypothesis_card["subjective_thesis_id"]
            or thesis["author_scope"] != "user"
            or thesis["status"] not in {
                "user_view_not_provided",
                "active",
                "expired",
                "withdrawn",
            }
        ):
            raise FactorLabError("subjective thesis identity or state is invalid")
        content = dict(thesis)
        claimed_content_hash = str(content.pop("content_sha256"))
        if _sha256_value(content) != claimed_content_hash:
            raise FactorLabError("subjective thesis content_sha256 mismatch")
        if _sha256_value(thesis) != self.hypothesis_card["subjective_thesis_sha256"]:
            raise FactorLabError("factor hypothesis subjective thesis binding mismatch")
        if thesis["status"] == "user_view_not_provided" and (
            thesis["statement"]
            or thesis["preferred_index_ids"]
            or thesis["avoided_index_ids"]
            or thesis["confidence_0_to_1"] is not None
            or thesis["evidence_for"]
            or thesis["evidence_against"]
        ):
            raise FactorLabError("unprovided user view must remain a neutral empty card")
        self.subjective_thesis = thesis
        self.plugin = plugin or RelativeMomentumPlugin()
        if self.plugin.inventory() != RelativeMomentumPlugin().inventory():
            raise FactorLabError("V1 plugin implementation and inventory are frozen")

    @property
    def benchmark_id(self) -> str:
        return str(self.hypothesis["universe"]["benchmark"]["canonical_id"])

    @property
    def is_same_source_v2(self) -> bool:
        return self.hypothesis_card["hypothesis_id"] == V2_HYPOTHESIS_ID

    @property
    def industry_items(self) -> tuple[Mapping[str, str], ...]:
        return tuple(self.hypothesis["universe"]["industries"])

    @property
    def industry_ids(self) -> tuple[str, ...]:
        return tuple(str(item["canonical_id"]) for item in self.industry_items)

    @property
    def reconciliation_ids(self) -> tuple[str, ...]:
        return tuple(f"RECON__{item}" for item in self.industry_ids)

    def inventory(self) -> dict[str, Any]:
        """Return the frozen candidate and safety inventory without reading data."""

        return {
            "schema_version": "factor-lab-inventory.v1",
            "hypothesis_id": self.hypothesis_card["hypothesis_id"],
            "hypothesis_sha256": self.hypothesis_sha256,
            "plugin": self.plugin.inventory(),
            "universe": {
                "classification": self.hypothesis["universe"]["classification"],
                "industry_count": len(self.industry_ids),
                "benchmark_id": self.benchmark_id,
            },
            "stages": self.hypothesis["stages"],
            "official_transport_status": self.hypothesis["source_policy"][
                "official_transport_status"
            ],
            "admission_ceiling": "statistical_confirmation_only_source_authentication_blocked",
            "safety": self.hypothesis["safety"],
        }

    @staticmethod
    def _probe_receipt(
        receipt_path: Path | str,
        evidence_root: Path | str,
    ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        """Verify a factor_evidence_probe receipt and its normalized payload.

        Both online receipts and offline replay receipts are accepted.  Offline
        receipts must bind an existing content-addressed online receipt; the
        latter remains the identity source.  This verifies controlled capture
        integrity only and intentionally returns no official-source attestation.
        """

        root = Path(evidence_root).resolve()
        supplied = Path(receipt_path).resolve()
        try:
            supplied.relative_to(root)
        except ValueError as exc:
            raise FactorLabError("probe receipt must remain under evidence_root") from exc
        try:
            raw = supplied.read_bytes()
            receipt = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FactorLabError(f"cannot read probe receipt: {exc}") from exc
        if not isinstance(receipt, dict):
            raise FactorLabError("probe receipt root must be an object")
        if not _SHA256_RE.fullmatch(supplied.stem) or sha256(raw).hexdigest() != supplied.stem:
            raise FactorLabError("probe receipt filename/content hash mismatch")
        if (
            receipt.get("receipt_version") != "factor-evidence-receipt-v1"
            or receipt.get("probe_version") != "factor-evidence-probe-v1"
        ):
            raise FactorLabError("unsupported factor evidence probe receipt")

        identity_receipt = receipt
        identity_path = supplied
        if receipt.get("mode") == "offline":
            online_digest = str(receipt.get("verified_online_receipt_sha256") or "")
            if not _SHA256_RE.fullmatch(online_digest):
                raise FactorLabError("offline receipt lacks a valid online receipt binding")
            matches = sorted((root / "receipts").glob(f"**/{online_digest}.json"))
            if len(matches) != 1:
                raise FactorLabError("offline receipt online binding is missing or ambiguous")
            identity_path = matches[0].resolve()
            try:
                online_raw = identity_path.read_bytes()
                identity_receipt = json.loads(online_raw.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise FactorLabError("cannot read bound online probe receipt") from exc
            if (
                not isinstance(identity_receipt, dict)
                or identity_path.stem != online_digest
                or sha256(online_raw).hexdigest() != online_digest
                or identity_receipt.get("mode") != "online"
            ):
                raise FactorLabError("offline receipt online binding is invalid")
            for field in (
                "source",
                "dataset_type",
                "request_fingerprint",
                "raw_content_sha256",
                "normalized_content_sha256",
                "normalized_path",
                "record_count",
            ):
                if receipt.get(field) != identity_receipt.get(field):
                    raise FactorLabError(f"offline receipt does not bind online {field}")
        elif receipt.get("mode") != "online":
            raise FactorLabError("probe receipt mode must be online or offline")

        required_identity = {
            "source",
            "source_role",
            "provider_id",
            "provider_adapter_identity",
            "adapter_version",
            "upstream_source",
            "dataset_type",
            "request_fingerprint",
            "request",
            "fetched_at",
            "availability_status",
            "point_in_time_status",
            "research_admission_status",
            "formal_truth_eligible",
            "record_count",
            "raw_content_sha256",
            "normalized_content_sha256",
            "normalized_path",
        }
        missing = sorted(required_identity - set(identity_receipt))
        if missing:
            raise FactorLabError(f"online probe receipt is incomplete: {missing}")
        if (
            identity_receipt["research_admission_status"] != "not_admitted_probe_only"
            or identity_receipt["formal_truth_eligible"] is not False
        ):
            raise FactorLabError("probe receipt admission boundary was altered")
        request = identity_receipt["request"]
        if not isinstance(request, dict):
            raise FactorLabError("probe receipt request must be an object")
        probe_request_hash = sha256(_canonical_json_bytes(request) + b"\n").hexdigest()
        if probe_request_hash != identity_receipt["request_fingerprint"]:
            raise FactorLabError("probe receipt request fingerprint mismatch")
        _parse_time(identity_receipt["fetched_at"], "probe receipt fetched_at")
        normalized_digest = str(identity_receipt["normalized_content_sha256"])
        if not _SHA256_RE.fullmatch(normalized_digest):
            raise FactorLabError("probe normalized digest is invalid")
        normalized_path = (root / str(identity_receipt["normalized_path"])).resolve()
        try:
            normalized_path.relative_to(root)
        except ValueError as exc:
            raise FactorLabError("probe normalized path escapes evidence_root") from exc
        try:
            normalized_raw = normalized_path.read_bytes()
            records = json.loads(normalized_raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FactorLabError("cannot read probe normalized evidence") from exc
        if sha256(normalized_raw).hexdigest() != normalized_digest:
            raise FactorLabError("probe normalized evidence hash mismatch")
        if (
            not isinstance(records, list)
            or not records
            or any(not isinstance(item, dict) for item in records)
            or len(records) != identity_receipt["record_count"]
        ):
            raise FactorLabError("probe normalized evidence record_count mismatch")
        return receipt, identity_receipt, records

    @staticmethod
    def _index_service_receipt(
        receipt_path: Path | str,
        evidence_root: Path | str,
    ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        """Verify and adapt an IndexEvidenceService source-owned receipt.

        The storage service, not caller-supplied identity fields, revalidates
        bundle, raw bytes, normalized records, transport receipts and hashes.
        ``evidence_root`` may be the storage root or a common ancestor that
        contains multiple immutable capture roots.
        """

        from research.market_data.index_evidence import (
            IndexEvidenceBundle,
            IndexEvidenceStorage,
        )

        root = Path(evidence_root).resolve()
        supplied = Path(receipt_path).resolve()
        try:
            relative = supplied.relative_to(root)
        except ValueError as exc:
            raise FactorLabError("index evidence receipt must remain under evidence_root") from exc
        parts = relative.parts
        try:
            evidence_part = parts.index("evidence")
        except ValueError as exc:
            raise FactorLabError("index evidence receipt is outside a storage evidence bucket") from exc
        storage_root = root.joinpath(*parts[:evidence_part])
        bundle_path = supplied.with_suffix(".json")
        try:
            receipt = json.loads(supplied.read_text(encoding="utf-8"))
            bundle_value = json.loads(bundle_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FactorLabError(f"cannot read index evidence receipt: {exc}") from exc
        if not isinstance(receipt, dict) or not isinstance(bundle_value, dict):
            raise FactorLabError("index evidence receipt and bundle must be objects")
        if receipt.get("receipt_version") != "index-evidence-storage-receipt-v1":
            raise FactorLabError("unsupported IndexEvidenceService receipt")
        try:
            candidate = IndexEvidenceBundle.from_dict(bundle_value)
            loaded, _, loaded_path = IndexEvidenceStorage(storage_root).load(
                candidate.provider_id,
                candidate.request_fingerprint,
                candidate.dataset_type,
                as_of=candidate.fetched_at,
            )
        except ValueError as exc:
            raise FactorLabError(f"IndexEvidenceService verification failed: {exc}") from exc
        if loaded_path.resolve() != bundle_path or loaded.evidence_id != candidate.evidence_id:
            raise FactorLabError("IndexEvidenceService receipt does not select the supplied bundle")
        if loaded.admission_status != "admitted_for_research":
            raise FactorLabError("source-owned evidence is not admitted for research")
        identities = {
            "csi_official": (
                "csi",
                "research.market_data.providers.csi_official.CSIOfficialProvider",
                "csindex.source_owned_https",
            ),
            "sse_calendar": (
                "sse",
                "research.market_data.providers.sse_calendar.SSECalendarProvider",
                "sse.source_owned_https",
            ),
        }
        identity_contract = identities.get(loaded.provider_id)
        if identity_contract is None:
            raise FactorLabError("IndexEvidenceService provider is outside Factor Lab policy")
        source, adapter_identity, upstream_source = identity_contract
        identity = {
            "source": source,
            "provider_id": loaded.provider_id,
            "provider_adapter_identity": adapter_identity,
            "adapter_version": loaded.adapter_version,
            "upstream_source": upstream_source,
            "dataset_type": loaded.dataset_type,
            "retrieval_mode": loaded.retrieval_mode,
            "request_fingerprint": loaded.request_fingerprint,
            "request": dict(loaded.request_payload),
            "fetched_at": loaded.fetched_at.isoformat(),
            "availability_status": loaded.point_in_time_status,
            "point_in_time_status": loaded.point_in_time_status,
            "research_admission_status": loaded.admission_status,
            "formal_truth_eligible": False,
            "record_count": len(loaded.records),
            "evidence_id": loaded.evidence_id,
            "bundle_sha256": str(receipt["bundle_sha256"]),
            "raw_content_sha256": loaded.raw_content_sha256,
            "normalized_content_sha256": loaded.normalized_content_sha256,
            "normalized_path": str(bundle_path.relative_to(storage_root)),
            "controlled_transport": "index_evidence_service",
        }
        return receipt, identity, [dict(item) for item in loaded.records]

    @classmethod
    def _controlled_receipt(
        cls,
        receipt_path: Path | str,
        evidence_root: Path | str,
    ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        supplied = Path(receipt_path)
        try:
            value = json.loads(supplied.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FactorLabError(f"cannot inspect controlled receipt: {exc}") from exc
        if not isinstance(value, dict):
            raise FactorLabError("controlled receipt root must be an object")
        if value.get("receipt_version") == "index-evidence-storage-receipt-v1":
            return cls._index_service_receipt(receipt_path, evidence_root)
        return cls._probe_receipt(receipt_path, evidence_root)

    @staticmethod
    def _source_index_text(value: Any, context: str) -> str:
        text = str(value or "").strip().upper()
        match = re.fullmatch(r"([0-9]{6})(?:\.CSI)?", text)
        if match is None:
            raise FactorLabError(f"{context} must be a six-digit CSI index id")
        return match.group(1)

    @staticmethod
    def _receipt_paths(
        value: Path | str | Sequence[Path | str],
    ) -> tuple[Path, ...]:
        if isinstance(value, (Path, str)):
            paths = (Path(value),)
        elif isinstance(value, Sequence):
            paths = tuple(Path(item) for item in value)
        else:
            raise FactorLabError("index_receipt must be a path or an ordered path list")
        if not paths:
            raise FactorLabError("at least one index receipt is required")
        resolved = tuple(path.resolve() for path in paths)
        if len(resolved) != len(set(resolved)):
            raise FactorLabError("duplicate index receipt paths are forbidden")
        return paths

    def load_probe_evidence(
        self,
        *,
        stage: str,
        index_receipt: Path | str | Sequence[Path | str],
        calendar_receipt: Path | str,
        evidence_root: Path | str,
    ) -> EvidenceBundle:
        """Convert controlled receipts into one strict internal evidence bundle.

        Only V2 Screen may provide multiple index receipts.  Receipt argument
        order is the frozen chronological shard order; each shard is verified
        independently before any records are combined.
        """

        if stage not in {"screen", "confirm", "weekly"}:
            raise FactorLabError("probe evidence stage is unsupported")
        index_receipt_paths = self._receipt_paths(index_receipt)
        if len(index_receipt_paths) > 1 and not (
            self.is_same_source_v2 and stage == "screen"
        ):
            raise FactorLabError(
                "multiple index receipts are supported only by V2 screen"
            )
        index_components = [
            self._controlled_receipt(path, evidence_root)
            for path in index_receipt_paths
        ]
        index_identities = [item[1] for item in index_components]
        index_record_parts = [item[2] for item in index_components]
        index_identity = index_identities[0]
        _, calendar_identity, calendar_records = self._controlled_receipt(
            calendar_receipt, evidence_root
        )
        expected_source = (
            "csi" if stage == "screen" and self.is_same_source_v2
            else "choice" if stage == "screen"
            else "csi"
        )
        expected_provider_ids = {
            "choice": {"choice", "choice_index"},
            "csi": {"csi", "csi_official"},
            "sse": {"sse", "sse_calendar"},
        }
        expected_adapter_suffix = {
            "choice": "research.market_data.providers.choice_index.ChoiceIndexProvider",
            "csi": "research.market_data.providers.csi_official.CSIOfficialProvider",
            "sse": "research.market_data.providers.sse_calendar.SSECalendarProvider",
        }
        for identity, source in (
            *((item, expected_source) for item in index_identities),
            (calendar_identity, "sse"),
        ):
            if identity["source"] != source:
                raise FactorLabError(f"probe source must be {source}")
            if str(identity["provider_id"]) not in expected_provider_ids[source]:
                raise FactorLabError(f"probe {source} provider_id is outside frozen policy")
            if identity["provider_adapter_identity"] != expected_adapter_suffix[source]:
                raise FactorLabError(f"probe {source} adapter identity is outside frozen policy")
            if not str(identity["adapter_version"] or "").strip():
                raise FactorLabError(f"probe {source} adapter_version is required")
        if any(item["dataset_type"] != "index_level" for item in index_identities):
            raise FactorLabError("index probe dataset_type must be index_level")
        if calendar_identity["dataset_type"] not in {"trade_calendar", "cn_equity_session"}:
            raise FactorLabError("calendar probe dataset_type is unsupported")
        if self.is_same_source_v2 and stage == "screen" and any(
            item.get("controlled_transport") != "index_evidence_service"
            or item.get("retrieval_mode") != "historical_backfill"
            or item.get("point_in_time_status")
            != "historical_backfill_not_original_capture"
            for item in index_identities
        ):
            raise FactorLabError(
                "V2 screen requires the controlled CSI historical-backfill receipt"
            )
        identity_fields = (
            "source",
            "provider_id",
            "provider_adapter_identity",
            "adapter_version",
            "upstream_source",
            "dataset_type",
            "retrieval_mode",
            "point_in_time_status",
            "controlled_transport",
        )
        if any(
            any(item.get(field) != index_identity.get(field) for field in identity_fields)
            for item in index_identities[1:]
        ):
            raise FactorLabError("index receipt shards do not share one frozen provider identity")

        source_maps = self.hypothesis["universe"]["source_index_ids"]
        benchmark_source_id = str(source_maps["benchmark"])
        ordered_industry_ids = list(self.industry_ids)
        if stage == "screen" and self.is_same_source_v2:
            index_map = {
                self._source_index_text(source_id, "CSI legacy screen mapping"): canonical_id
                for canonical_id, source_id in source_maps["csi_legacy_screen"].items()
            }
            index_map[benchmark_source_id] = self.benchmark_id
        elif stage == "screen":
            screen_map = {
                self._source_index_text(source_id, "choice screen mapping"): canonical_id
                for canonical_id, source_id in source_maps["choice_screen"].items()
            }
            current_map = {
                self._source_index_text(source_id, "choice current mapping"): f"RECON__{canonical_id}"
                for canonical_id, source_id in source_maps[
                    "choice_current_reconciliation"
                ].items()
            }
            index_map = {**screen_map, **current_map, benchmark_source_id: self.benchmark_id}
        else:
            index_map = {
                self._source_index_text(source_id, "CSI confirm mapping"): canonical_id
                for canonical_id, source_id in source_maps["csi_confirm"].items()
            }
            index_map[benchmark_source_id] = self.benchmark_id
        component_windows: list[tuple[date, date]] = []
        for component_index, identity in enumerate(index_identities):
            request = identity.get("request")
            if not isinstance(request, dict):
                raise FactorLabError("index receipt request must be an object")
            request_ids = {
                self._source_index_text(item, "probe request index_id")
                for item in request.get("index_ids", [])
            }
            if request_ids != set(index_map):
                raise FactorLabError(
                    f"index receipt shard {component_index + 1} whitelist mismatch: "
                    f"expected={sorted(index_map)}, observed={sorted(request_ids)}"
                )
            start = _parse_date(
                request.get("start_date"),
                f"index receipt shard {component_index + 1} start_date",
            )
            end = _parse_date(
                request.get("end_date"),
                f"index receipt shard {component_index + 1} end_date",
            )
            if start > end:
                raise FactorLabError("index receipt shard start_date follows end_date")
            if component_windows and start <= component_windows[-1][1]:
                raise FactorLabError(
                    "index receipt shards must be chronological and non-overlapping"
                )
            component_windows.append((start, end))

        calendar_payload: list[dict[str, Any]] = []
        calendar_all_dates: set[date] = set()
        calendar_trading_dates: set[date] = set()
        expected_calendar_fields = {
            "schema_version",
            "calendar_date",
            "is_trading_day",
            "session_open_at",
            "session_close_at",
            "available_at",
            "availability_status",
            "source_record_id",
        }
        for index, record in enumerate(calendar_records):
            _require_exact_keys(
                record,
                required=expected_calendar_fields,
                context=f"probe calendar[{index}]",
            )
            calendar_day = _parse_date(
                record["calendar_date"], f"probe calendar[{index}].calendar_date"
            )
            if calendar_day in calendar_all_dates:
                raise FactorLabError("probe calendar contains a duplicate natural date")
            calendar_all_dates.add(calendar_day)
            if record["is_trading_day"] is True:
                calendar_trading_dates.add(calendar_day)
            calendar_payload.append(
                {
                    "trading_date": record["calendar_date"],
                    "is_trading_day": record["is_trading_day"],
                    "available_at": record["available_at"],
                    "source_record_id": record["source_record_id"],
                }
            )
        combined_start = component_windows[0][0]
        combined_end = component_windows[-1][1]
        expected_calendar_dates: set[date] = set()
        current_calendar_day = combined_start
        while current_calendar_day <= combined_end:
            expected_calendar_dates.add(current_calendar_day)
            current_calendar_day += timedelta(days=1)
        if not expected_calendar_dates.issubset(calendar_all_dates):
            missing_dates = sorted(expected_calendar_dates - calendar_all_dates)
            raise FactorLabError(
                "SSE calendar does not completely cover the combined index window: "
                f"first_missing={missing_dates[0].isoformat()}"
            )

        bars: list[dict[str, Any]] = []
        observed_source_ids: set[str] = set()
        expected_index_fields = {
            "schema_version",
            "index_id",
            "trading_date",
            "open",
            "high",
            "low",
            "close",
            "currency",
            "basis",
            "available_at",
            "availability_status",
            "source_record_id",
        }
        observed_bar_keys: set[tuple[str, date]] = set()
        observed_record_ids: set[str] = set()
        for component_index, (records, window) in enumerate(
            zip(index_record_parts, component_windows)
        ):
            part_source_ids: set[str] = set()
            last_date_by_source: dict[str, date] = {}
            for record_index, record in enumerate(records):
                context = (
                    f"probe index shard {component_index + 1}"
                    f"[{record_index}]"
                )
                _require_exact_keys(
                    record,
                    required=expected_index_fields,
                    context=context,
                )
                source_id = self._source_index_text(
                    record["index_id"], f"{context}.index_id"
                )
                canonical_id = index_map.get(source_id)
                if canonical_id is None:
                    raise FactorLabError(
                        "probe normalized evidence contains a non-whitelisted index"
                    )
                trading_day = _parse_date(
                    record["trading_date"], f"{context}.trading_date"
                )
                if not window[0] <= trading_day <= window[1]:
                    raise FactorLabError("index shard contains a date outside its request window")
                previous = last_date_by_source.get(source_id)
                if previous is not None and trading_day <= previous:
                    raise FactorLabError(
                        "each index series inside a shard must be strictly time ordered"
                    )
                last_date_by_source[source_id] = trading_day
                key = (source_id, trading_day)
                if key in observed_bar_keys:
                    raise FactorLabError(
                        "index receipt shards contain an overlapping or duplicate bar"
                    )
                source_record_id = str(record["source_record_id"] or "").strip()
                if not source_record_id or source_record_id in observed_record_ids:
                    raise FactorLabError(
                        "index receipt shards contain a missing or duplicate source_record_id"
                    )
                if trading_day not in calendar_trading_dates:
                    raise FactorLabError(
                        "index receipt contains a bar on an SSE non-trading date"
                    )
                if record["currency"] != "CNY" or record["basis"] != "index_points_unadjusted":
                    raise FactorLabError("probe index units or adjustment basis violate V1 policy")
                optional_prices = (record["open"], record["high"], record["low"])
                if any(value is None for value in optional_prices) and not all(
                    value is None for value in optional_prices
                ):
                    raise FactorLabError("probe optional OHLC fields are partially missing")
                for field, value in zip(("open", "high", "low"), optional_prices):
                    if value is not None:
                        _decimal(value, f"{context}.{field}")
                _decimal(record["close"], f"{context}.close")
                observed_bar_keys.add(key)
                observed_record_ids.add(source_record_id)
                observed_source_ids.add(source_id)
                part_source_ids.add(source_id)
                bars.append(
                    {
                        "instrument_id": source_id,
                        "trading_date": record["trading_date"],
                        "close": record["close"],
                        "available_at": record["available_at"],
                        "source_record_id": source_record_id,
                    }
                )
            if part_source_ids != set(index_map):
                raise FactorLabError(
                    f"index receipt shard {component_index + 1} does not contain all 12 frozen codes"
                )
        if observed_source_ids != set(index_map):
            raise FactorLabError("probe normalized evidence misses a frozen index series")
        expected_bar_keys = {
            (source_id, trading_day)
            for source_id in index_map
            for trading_day in calendar_trading_dates
            if combined_start <= trading_day <= combined_end
        }
        if observed_bar_keys != expected_bar_keys:
            missing = sorted(expected_bar_keys - observed_bar_keys)
            unknown = sorted(observed_bar_keys - expected_bar_keys)
            detail = (
                f"first_missing={missing[0][0]}:{missing[0][1].isoformat()}"
                if missing
                else f"first_unknown={unknown[0][0]}:{unknown[0][1].isoformat()}"
            )
            raise FactorLabError(
                "combined index shards fail SSE complete-panel validation: " + detail
            )

        names = {str(item["canonical_id"]): str(item["name"]) for item in self.industry_items}
        instruments = []
        for source_id, canonical_id in index_map.items():
            if canonical_id == self.benchmark_id:
                role = "benchmark"
                name = str(self.hypothesis["universe"]["benchmark"]["name"])
            elif canonical_id.startswith("RECON__"):
                role = "reconciliation_industry"
                name = names[canonical_id.removeprefix("RECON__")] + "（对账序列）"
            else:
                role = "industry"
                name = names[canonical_id]
            instruments.append(
                {
                    "instrument_id": source_id,
                    "canonical_id": canonical_id,
                    "role": role,
                    "name": name,
                }
            )
        fetched_at = max(
            *(
                _parse_time(item["fetched_at"], "index receipt fetched_at")
                for item in index_identities
            ),
            _parse_time(calendar_identity["fetched_at"], "calendar receipt fetched_at"),
        )
        index_bundle_hashes = tuple(
            str(item["bundle_sha256"])
            for item in index_identities
            if item.get("bundle_sha256") is not None
        )
        if len(index_receipt_paths) > 1 and len(index_bundle_hashes) != len(
            index_receipt_paths
        ):
            raise FactorLabError("every V2 screen shard must bind an IndexEvidenceService bundle hash")
        request_sha256 = (
            str(index_identity["request_fingerprint"])
            if len(index_identities) == 1
            else _sha256_value(
                [str(item["request_fingerprint"]) for item in index_identities]
            )
        )
        response_sha256 = (
            str(index_identity["normalized_content_sha256"])
            if len(index_identities) == 1
            else _sha256_value(
                [str(item["normalized_content_sha256"]) for item in index_identities]
            )
        )
        payload = {
            "schema_version": "factor-lab-evidence-bundle.v1",
            "bundle_id": _sha256_value(
                {
                    "stage": stage,
                    "index_receipts": [path.stem for path in index_receipt_paths],
                    "index_bundle_sha256": list(index_bundle_hashes),
                    "calendar_receipt": Path(calendar_receipt).stem,
                }
            ),
            "stage": stage,
            "source": {
                "source_id": expected_source,
                "source_authority": "official"
                if self.is_same_source_v2 or stage != "screen"
                else "licensed_secondary",
                "source_uri": str(index_identity["upstream_source"]),
                "adapter_version": str(index_identity["adapter_version"]),
                "retrieved_at": fetched_at.isoformat(),
            },
            "receipt": {
                "transport": str(
                    index_identity.get("controlled_transport", "factor_evidence_probe")
                ),
                "request_sha256": request_sha256,
                "response_sha256": response_sha256,
                # This flag describes only converter integrity.  The immutable
                # config keeps official_transport_status=not_configured.
                "evidence_verified": True,
            },
            "instruments": instruments,
            "calendar": calendar_payload,
            "bars": bars,
        }
        bundle = EvidenceBundle.from_json(payload)
        return replace(bundle, source_bundle_sha256=index_bundle_hashes)

    def _validate_source(self, bundle: EvidenceBundle, stage: str) -> None:
        if bundle.stage != stage:
            raise FactorLabError(
                f"evidence stage {bundle.stage!r} cannot be used for {stage!r}"
            )
        policy = self.hypothesis["source_policy"][stage]
        expected = {
            "source_id": policy["source_id"],
            "source_authority": policy["source_authority"],
        }
        for field, value in expected.items():
            if bundle.source.get(field) != value:
                raise FactorLabError(f"{stage} evidence {field} violates source policy")
        screen_source_contract = (
            (
                "csindex.source_owned_https",
                "csi-official-adapter-v1",
                "csi-index-perf:",
            )
            if self.is_same_source_v2
            else (
                "choice.eastmoney_emquantapi.csd_index_with_single_tradedates",
                "choice-index-adapter-v1",
                "choice-csd:",
            )
        )
        expected_source_contract = {
            "screen": screen_source_contract,
            "confirm": (
                "csindex.source_owned_https",
                "csi-official-adapter-v1",
                "csi-index-perf:",
            ),
            "weekly": (
                "csindex.source_owned_https",
                "csi-official-adapter-v1",
                "csi-index-perf:",
            ),
        }
        expected_uri, expected_adapter, expected_record_prefix = expected_source_contract[
            stage
        ]
        if (
            bundle.source.get("source_uri") != expected_uri
            or bundle.source.get("adapter_version") != expected_adapter
        ):
            raise FactorLabError(
                f"{stage} evidence source identity violates the frozen provider contract"
            )
        if any(
            not item.source_record_id.startswith(expected_record_prefix)
            for item in bundle.bars
        ):
            raise FactorLabError(
                f"{stage} evidence source records violate the frozen provider identity"
            )
        if bundle.receipt.get("transport") != policy["transport"]:
            raise FactorLabError(f"{stage} evidence transport violates source policy")
        if policy["integrity_flag_required"] and bundle.receipt.get("evidence_verified") is not True:
            raise FactorLabError(f"{stage} evidence integrity verification is required")
        retrieved_at = _parse_time(bundle.source["retrieved_at"], "source.retrieved_at")
        if any(bar.available_at > retrieved_at for bar in bundle.bars):
            raise FactorLabError("bar available_at cannot follow bundle retrieved_at")

    def _validate_universe(self, bundle: EvidenceBundle) -> None:
        expected = set(self.industry_ids) | {self.benchmark_id}
        observed = set(bundle.instrument_by_canonical)
        allowed_sets = [expected]
        if bundle.stage == "screen" and not self.is_same_source_v2:
            allowed_sets.append(expected | set(self.reconciliation_ids))
        if observed not in allowed_sets:
            raise FactorLabError(
                f"evidence universe mismatch: missing={sorted(expected - observed)}, "
                f"unknown={sorted(observed - expected - set(self.reconciliation_ids))}"
            )
        for item in bundle.instruments:
            if item.canonical_id in self.reconciliation_ids:
                expected_role = "reconciliation_industry"
            else:
                expected_role = (
                    "benchmark"
                    if item.canonical_id == self.benchmark_id
                    else "industry"
                )
            if item.role != expected_role:
                raise FactorLabError(f"evidence role mismatch for {item.canonical_id}")
        source_maps = self.hypothesis["universe"]["source_index_ids"]
        expected_source_by_canonical: dict[str, str]
        if bundle.stage == "screen" and self.is_same_source_v2:
            expected_source_by_canonical = {
                canonical_id: self._source_index_text(
                    source_id, "CSI legacy screen series mapping"
                )
                for canonical_id, source_id in source_maps["csi_legacy_screen"].items()
            }
        elif bundle.stage == "screen":
            expected_source_by_canonical = {
                canonical_id: self._source_index_text(source_id, "screen series mapping")
                for canonical_id, source_id in source_maps["choice_screen"].items()
            }
            expected_source_by_canonical.update(
                {
                    f"RECON__{canonical_id}": self._source_index_text(
                        source_id, "screen reconciliation series mapping"
                    )
                    for canonical_id, source_id in source_maps[
                        "choice_current_reconciliation"
                    ].items()
                }
            )
        else:
            expected_source_by_canonical = {
                canonical_id: self._source_index_text(source_id, "confirm series mapping")
                for canonical_id, source_id in source_maps["csi_confirm"].items()
            }
        expected_source_by_canonical[self.benchmark_id] = self._source_index_text(
            source_maps["benchmark"], "benchmark series mapping"
        )
        for item in bundle.instruments:
            expected_source_id = expected_source_by_canonical.get(item.canonical_id)
            if expected_source_id is None or self._source_index_text(
                item.instrument_id, f"evidence instrument mapping for {item.canonical_id}"
            ) != expected_source_id:
                raise FactorLabError(
                    f"evidence series mapping violates the frozen {bundle.stage} universe"
                )

    def _expected_weeks(
        self,
        bundle: EvidenceBundle,
        stage: str,
        *,
        after_date: date | None = None,
        candidate_ids: set[str] | None = None,
    ) -> tuple[list[date], dict[date, str]]:
        policy = self.hypothesis["stages"][stage]
        total = int(policy["total_weeks"])
        calendar_index = {trading_day: index for index, trading_day in enumerate(bundle.calendar)}
        candidate_lookbacks = [
            spec.lookback_sessions
            for spec in self.plugin.specs
            if candidate_ids is None or spec.candidate_id in candidate_ids
        ]
        if not candidate_lookbacks:
            raise FactorLabError("evaluation candidate set is empty")
        minimum_lookback = max(candidate_lookbacks)
        eligible = [
            trading_day
            for trading_day in _week_end_sessions(bundle.calendar)
            if calendar_index[trading_day] >= minimum_lookback
            and calendar_index[trading_day] + 20 < len(bundle.calendar)
            and (after_date is None or trading_day > after_date)
        ]
        if stage == "screen":
            cutoff = date.fromisoformat(self.hypothesis["time_policy"]["screen_signal_cutoff"])
            # The screen and confirmation series are independent holdouts.  A
            # screen signal is eligible only when its complete 20-session
            # label has matured before the frozen cutoff; otherwise the
            # winner selection would read outcomes from the confirm era.
            eligible = [
                trading_day
                for trading_day in eligible
                if trading_day <= cutoff
                and bundle.calendar[calendar_index[trading_day] + 20] <= cutoff
            ]
            selected = eligible[-total:]
        else:
            inception = date.fromisoformat(
                self.hypothesis["time_policy"]["confirm_series_inception"]
            )
            eligible = [trading_day for trading_day in eligible if trading_day >= inception]
            selected = eligible[-total:]
        weeks_per_window = int(policy["weeks_per_window"])
        assignments = {
            trading_day: f"{stage}-W{(position // weeks_per_window) + 1}"
            for position, trading_day in enumerate(selected)
        }
        return selected, assignments

    @staticmethod
    def _exception(
        stage: str,
        code: str,
        message: str,
        *,
        severity: str = "error",
        week_end: date | None = None,
        candidate_id: str = "",
        industry_id: str = "",
    ) -> dict[str, Any]:
        return {
            "stage": stage,
            "code": code,
            "severity": severity,
            "week_end": week_end.isoformat() if week_end else "",
            "candidate_id": candidate_id,
            "industry_id": industry_id,
            "message": message,
        }

    def _build_observations(
        self,
        bundle: EvidenceBundle,
        stage: str,
        expected_weeks: Sequence[date],
        window_by_week: Mapping[date, str],
        *,
        candidate_ids: set[str] | None = None,
    ) -> tuple[list[FactorObservation], list[dict[str, Any]], dict[str, int]]:
        bars = bundle.bar_by_key
        calendar_index = {trading_day: index for index, trading_day in enumerate(bundle.calendar)}
        retrieved_at = _parse_time(bundle.source["retrieved_at"], "source.retrieved_at")
        names = {str(item["canonical_id"]): str(item["name"]) for item in self.industry_items}
        observations: list[FactorObservation] = []
        exceptions: list[dict[str, Any]] = []
        valid_weeks_by_window = {
            f"{stage}-W{index}": 0
            for index in range(1, int(self.hypothesis["stages"][stage]["window_count"]) + 1)
        }

        for week_end in expected_weeks:
            decision_index = calendar_index[week_end]
            label_start = bundle.calendar[decision_index + 1]
            label_end = bundle.calendar[decision_index + 20]
            needed_dates = {week_end, label_start, label_end}
            active_specs = [
                spec
                for spec in self.plugin.specs
                if candidate_ids is None or spec.candidate_id in candidate_ids
            ]
            needed_dates.update(
                bundle.calendar[decision_index - spec.lookback_sessions]
                for spec in active_specs
            )
            missing: list[str] = []
            for canonical_id in (*self.industry_ids, self.benchmark_id):
                for needed_date in needed_dates:
                    bar = bars.get((canonical_id, needed_date))
                    if bar is None:
                        missing.append(f"{canonical_id}@{needed_date.isoformat()}")
                    elif bar.available_at > retrieved_at:
                        missing.append(
                            f"{canonical_id}@{needed_date.isoformat()}:not_yet_available"
                        )
            if missing:
                exceptions.append(
                    self._exception(
                        stage,
                        "incomplete_week",
                        "complete-case week excluded; missing endpoints: "
                        + ", ".join(missing[:8])
                        + (" ..." if len(missing) > 8 else ""),
                        week_end=week_end,
                    )
                )
                continue

            # Every signal endpoint must have been available by the decision
            # session's local end of day.  Labels may arrive later but never
            # after the captured bundle's retrieval time (checked above).
            signal_tz = retrieved_at.tzinfo
            decision_cutoff = datetime.combine(week_end, time.max, tzinfo=signal_tz)
            leaking = []
            signal_dates = {week_end}
            signal_dates.update(
                bundle.calendar[decision_index - spec.lookback_sessions]
                for spec in active_specs
            )
            for canonical_id in (*self.industry_ids, self.benchmark_id):
                for signal_date in signal_dates:
                    if bars[(canonical_id, signal_date)].available_at > decision_cutoff:
                        leaking.append(f"{canonical_id}@{signal_date.isoformat()}")
            if leaking:
                exceptions.append(
                    self._exception(
                        stage,
                        "future_available_signal",
                        "complete-case week excluded; signal was unavailable at decision: "
                        + ", ".join(leaking[:8])
                        + (" ..." if len(leaking) > 8 else ""),
                        week_end=week_end,
                    )
                )
                continue

            benchmark_label = (
                bars[(self.benchmark_id, label_end)].close
                / bars[(self.benchmark_id, label_start)].close
            )
            for spec in self.plugin.specs:
                if candidate_ids is not None and spec.candidate_id not in candidate_ids:
                    continue
                lookback_date = bundle.calendar[decision_index - spec.lookback_sessions]
                raw_rows: list[tuple[str, float, float]] = []
                for industry_id in self.industry_ids:
                    signal = self.plugin.compute(
                        spec,
                        industry_now=bars[(industry_id, week_end)].close,
                        industry_then=bars[(industry_id, lookback_date)].close,
                        benchmark_now=bars[(self.benchmark_id, week_end)].close,
                        benchmark_then=bars[(self.benchmark_id, lookback_date)].close,
                    )
                    industry_label = (
                        bars[(industry_id, label_end)].close
                        / bars[(industry_id, label_start)].close
                    )
                    forward_excess = float(industry_label / benchmark_label - Decimal("1"))
                    raw_rows.append((industry_id, signal, forward_excess))
                ranks = _average_ranks([item[1] for item in raw_rows])
                for item, rank in zip(raw_rows, ranks):
                    industry_id, signal, forward_excess = item
                    observations.append(
                        FactorObservation(
                            stage=stage,
                            window_id=window_by_week[week_end],
                            week_end=week_end,
                            label_start=label_start,
                            label_end=label_end,
                            candidate_id=spec.candidate_id,
                            lookback_sessions=spec.lookback_sessions,
                            industry_id=industry_id,
                            industry_name=names[industry_id],
                            signal=signal,
                            signal_rank=rank,
                            forward_excess_return_20d=forward_excess,
                            source_bundle_id=bundle.bundle_id,
                        )
                    )
            valid_weeks_by_window[window_by_week[week_end]] += 1
        return observations, exceptions, valid_weeks_by_window

    def _build_weekly_metrics(
        self,
        observations: Sequence[FactorObservation],
        stage: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        grouped: dict[tuple[str, date], list[FactorObservation]] = defaultdict(list)
        for observation in observations:
            grouped[(observation.candidate_id, observation.week_end)].append(observation)
        portfolio = self.hypothesis["statistics"]["portfolio"]
        long_count = int(portfolio["long_count"])
        short_count = int(portfolio["short_count"])
        prior_weights: dict[str, dict[str, float]] = {}
        rows: list[dict[str, Any]] = []
        exceptions: list[dict[str, Any]] = []

        invalid_screen_weeks: set[date] = set()
        if stage == "screen":
            all_weeks = sorted({key[1] for key in grouped})
            for week_end in all_weeks:
                failures = []
                for spec in self.plugin.specs:
                    items = grouped.get((spec.candidate_id, week_end), [])
                    value = (
                        _spearman(
                            [item.signal for item in items],
                            [float(item.forward_excess_return_20d) for item in items],
                        )
                        if len(items) == len(self.industry_ids)
                        else None
                    )
                    if value is None:
                        failures.append(spec.candidate_id)
                if failures:
                    invalid_screen_weeks.add(week_end)
                    exceptions.append(
                        self._exception(
                            stage,
                            "shared_candidate_week_invalid",
                            "screen complete-case week excluded for every candidate; "
                            "undefined or incomplete IC for " + ",".join(failures),
                            severity="warning",
                            week_end=week_end,
                        )
                    )

        for spec in self.plugin.specs:
            candidate_keys = sorted(
                (key for key in grouped if key[0] == spec.candidate_id),
                key=lambda key: key[1],
            )
            for key in candidate_keys:
                if key[1] in invalid_screen_weeks:
                    continue
                items = sorted(grouped[key], key=lambda item: item.industry_id)
                signals = [item.signal for item in items]
                labels = [
                    float(item.forward_excess_return_20d)
                    for item in items
                    if item.forward_excess_return_20d is not None
                ]
                if len(items) != len(self.industry_ids) or len(labels) != len(items):
                    raise FactorLabError("internal complete-case invariant failed")
                rank_ic = _spearman(signals, labels)
                if rank_ic is None:
                    exceptions.append(
                        self._exception(
                            stage,
                            "undefined_rank_ic",
                            "candidate week excluded because cross-sectional ranks are constant",
                            severity="warning",
                            week_end=key[1],
                            candidate_id=spec.candidate_id,
                        )
                    )
                    continue
                ordered = sorted(items, key=lambda item: (item.signal, item.industry_id))
                bottom = ordered[:short_count]
                top = ordered[-long_count:]
                gross_spread = statistics.fmean(
                    float(item.forward_excess_return_20d) for item in top
                ) - statistics.fmean(
                    float(item.forward_excess_return_20d) for item in bottom
                )
                current_weights = {industry_id: 0.0 for industry_id in self.industry_ids}
                for item in top:
                    current_weights[item.industry_id] = 1.0 / long_count
                for item in bottom:
                    current_weights[item.industry_id] = -1.0 / short_count
                previous = prior_weights.get(
                    spec.candidate_id,
                    {industry_id: 0.0 for industry_id in self.industry_ids},
                )
                one_way_turnover = 0.5 * sum(
                    abs(current_weights[industry_id] - previous[industry_id])
                    for industry_id in self.industry_ids
                )
                prior_weights[spec.candidate_id] = current_weights
                rows.append(
                    {
                        "stage": stage,
                        "window_id": items[0].window_id,
                        "week_end": key[1].isoformat(),
                        "candidate_id": spec.candidate_id,
                        "industry_count": len(items),
                        "rank_ic": rank_ic,
                        "gross_top3_bottom3_spread": gross_spread,
                        "one_way_turnover": one_way_turnover,
                    }
                )
        return rows, exceptions

    @staticmethod
    def _window_row(**changes: Any) -> dict[str, Any]:
        row = {field: "" for field in WINDOW_METRIC_FIELDS}
        row.update(changes)
        return row

    def _leave_one_out(
        self,
        observations: Sequence[FactorObservation],
        candidate_id: str,
    ) -> dict[str, float]:
        relevant = [item for item in observations if item.candidate_id == candidate_id]
        by_week: dict[date, list[FactorObservation]] = defaultdict(list)
        for item in relevant:
            by_week[item.week_end].append(item)
        result: dict[str, float] = {}
        for omitted in self.industry_ids:
            week_values: list[float] = []
            for week_end in sorted(by_week):
                retained = [item for item in by_week[week_end] if item.industry_id != omitted]
                value = _spearman(
                    [item.signal for item in retained],
                    [float(item.forward_excess_return_20d) for item in retained],
                )
                if value is not None:
                    week_values.append(value)
            result[omitted] = statistics.fmean(week_values) if week_values else -1.0
        return result

    def _industry_contributions(
        self,
        observations: Sequence[FactorObservation],
        candidate_id: str,
    ) -> tuple[dict[str, float], float]:
        by_week: dict[date, list[FactorObservation]] = defaultdict(list)
        for item in observations:
            if item.candidate_id == candidate_id:
                by_week[item.week_end].append(item)
        raw: dict[str, list[float]] = defaultdict(list)
        for items in by_week.values():
            rank_mean = statistics.fmean(item.signal_rank for item in items)
            label_mean = statistics.fmean(
                float(item.forward_excess_return_20d) for item in items
            )
            for item in items:
                raw[item.industry_id].append(
                    (item.signal_rank - rank_mean)
                    * (float(item.forward_excess_return_20d) - label_mean)
                )
        contributions = {
            industry_id: statistics.fmean(raw.get(industry_id, [0.0]))
            for industry_id in self.industry_ids
        }
        denominator = sum(abs(value) for value in contributions.values())
        concentration = (
            max(abs(value) for value in contributions.values()) / denominator
            if denominator > 0.0
            else 1.0
        )
        return contributions, concentration

    def _summarize_candidates(
        self,
        *,
        stage: str,
        observations: Sequence[FactorObservation],
        weekly_metrics: Sequence[Mapping[str, Any]],
        coverage: float,
        valid_weeks_by_window: Mapping[str, int],
        candidate_ids: set[str] | None = None,
    ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
        policy = self.hypothesis["stages"][stage]
        block_length = int(self.hypothesis["statistics"]["block_length_weeks"])
        resamples = int(self.hypothesis["statistics"]["resamples"])
        rows: list[dict[str, Any]] = []
        summaries: dict[str, dict[str, Any]] = {}

        for spec in self.plugin.specs:
            if candidate_ids is not None and spec.candidate_id not in candidate_ids:
                continue
            candidate_rows = [
                dict(item)
                for item in weekly_metrics
                if item["candidate_id"] == spec.candidate_id
            ]
            window_means: list[float] = []
            window_counts: dict[str, int] = {}
            for window_index in range(1, int(policy["window_count"]) + 1):
                window_id = f"{stage}-W{window_index}"
                current = [item for item in candidate_rows if item["window_id"] == window_id]
                values = [float(item["rank_ic"]) for item in current]
                window_counts[window_id] = len(values)
                mean_ic = statistics.fmean(values) if values else None
                if mean_ic is not None:
                    window_means.append(mean_ic)
                rows.append(
                    self._window_row(
                        row_type="window",
                        stage=stage,
                        candidate_id=spec.candidate_id,
                        window_id=window_id,
                        week_count=len(values),
                        mean_ic=mean_ic,
                        median_ic=statistics.median(values) if values else None,
                        ic_std=statistics.pstdev(values) if len(values) > 1 else None,
                        overlap_adjusted_icir=_hac_overlap_adjusted_icir(values),
                        mean_gross_spread=statistics.fmean(
                            float(item["gross_top3_bottom3_spread"]) for item in current
                        )
                        if current
                        else None,
                        positive_ic_weeks=sum(value > 0.0 for value in values),
                    )
                )

            ic_values = [float(item["rank_ic"]) for item in candidate_rows]
            seed = _seed_from(self.hypothesis_sha256, stage, spec.candidate_id)
            sampled_lower, bootstrap_p = _block_bootstrap(
                ic_values,
                block_length=block_length,
                resamples=resamples,
                seed=seed,
            )
            candidate_observations = [
                item for item in observations if item.candidate_id == spec.candidate_id
            ]
            permutation_p = (
                _cross_sectional_label_permutation_p_value(
                    candidate_observations,
                    resamples=resamples,
                    seed=seed ^ 0x5A5A5A5A,
                )
                if stage == "confirm"
                else None
            )
            bootstrap_lower = sampled_lower if stage == "confirm" else None
            if stage == "confirm":
                leave_one_out = self._leave_one_out(observations, spec.candidate_id)
                contributions, concentration = self._industry_contributions(
                    observations, spec.candidate_id
                )
            else:
                leave_one_out = {}
                contributions = {}
                concentration = None
            leave_one_min = min(leave_one_out.values()) if leave_one_out else None
            summary = {
                "candidate_id": spec.candidate_id,
                "lookback_sessions": spec.lookback_sessions,
                "week_count": len(ic_values),
                "window_counts": window_counts,
                "mean_ic": statistics.fmean(ic_values) if ic_values else None,
                "median_window_ic": statistics.median(window_means)
                if len(window_means) == int(policy["window_count"])
                else None,
                "positive_window_count": sum(value > 0.0 for value in window_means),
                "positive_spread_window_count": sum(
                    statistics.fmean(
                        float(item["gross_top3_bottom3_spread"])
                        for item in candidate_rows
                        if item["window_id"] == f"{stage}-W{window_index}"
                    )
                    > 0.0
                    for window_index in range(1, int(policy["window_count"]) + 1)
                    if any(
                        item["window_id"] == f"{stage}-W{window_index}"
                        for item in candidate_rows
                    )
                ),
                "ic_std": statistics.pstdev(ic_values) if len(ic_values) > 1 else None,
                "overlap_adjusted_icir": _hac_overlap_adjusted_icir(ic_values),
                "mean_gross_spread": statistics.fmean(
                    float(item["gross_top3_bottom3_spread"]) for item in candidate_rows
                )
                if candidate_rows
                else None,
                "bootstrap_lower_95": bootstrap_lower,
                "bootstrap_p_value": bootstrap_p,
                "permutation_p_value": permutation_p,
                "raw_family_p_value": bootstrap_p if stage == "screen" else None,
                "holm_adjusted_p_value": None,
                "leave_one_industry_out": leave_one_out,
                "leave_one_industry_out_min_mean_ic": leave_one_min,
                "industry_contributions": contributions,
                "max_industry_contribution_share": concentration,
                "coverage": coverage,
                "data_valid_weeks_by_window": dict(valid_weeks_by_window),
                "passed": False,
                "gate_reasons": [],
            }
            summaries[spec.candidate_id] = summary
            for industry_id in self.industry_ids if stage == "confirm" else ():
                rows.append(
                    self._window_row(
                        row_type="leave_one_industry_out",
                        stage=stage,
                        candidate_id=spec.candidate_id,
                        industry_id=industry_id,
                        week_count=len(ic_values),
                        mean_ic=leave_one_out[industry_id],
                    )
                )
                rows.append(
                    self._window_row(
                        row_type="industry_contribution",
                        stage=stage,
                        candidate_id=spec.candidate_id,
                        industry_id=industry_id,
                        week_count=len(ic_values),
                        max_industry_contribution_share=concentration,
                        industry_contribution=contributions[industry_id],
                    )
                )

        if stage == "screen":
            adjusted = _holm_adjust(
                {
                    candidate_id: summary["raw_family_p_value"]
                    for candidate_id, summary in summaries.items()
                }
            )
            for candidate_id, value in adjusted.items():
                summaries[candidate_id]["holm_adjusted_p_value"] = value
        return summaries, rows

    def _apply_gates(
        self,
        *,
        stage: str,
        summaries: dict[str, dict[str, Any]],
        coverage: float,
        valid_weeks_by_window: Mapping[str, int],
    ) -> None:
        policy = self.hypothesis["stages"][stage]
        gates = policy["gates"]
        for summary in summaries.values():
            reasons: list[str] = []
            if summary["week_count"] < int(policy["minimum_total_weeks"]):
                reasons.append("total_weeks_below_minimum")
            if any(
                count < int(policy["minimum_weeks_per_window"])
                for count in summary["window_counts"].values()
            ):
                reasons.append("window_coverage_below_minimum")
            if summary["mean_ic"] is None or summary["mean_ic"] < float(
                gates["minimum_combined_mean_ic"]
            ):
                reasons.append("combined_mean_ic_below_minimum")
            if summary["positive_window_count"] < int(gates["minimum_positive_ic_windows"]):
                reasons.append("insufficient_positive_windows")
            if stage == "screen":
                if summary["positive_spread_window_count"] < int(
                    gates["minimum_positive_spread_windows"]
                ):
                    reasons.append("insufficient_positive_spread_windows")
                adjusted = summary["holm_adjusted_p_value"]
                if adjusted is None or adjusted >= float(gates["maximum_holm_p_value"]):
                    reasons.append("holm_adjusted_p_above_maximum")
            else:
                bootstrap_lower = summary["bootstrap_lower_95"]
                if bootstrap_lower is None or bootstrap_lower <= 0.0:
                    reasons.append("bootstrap_lower_95_not_positive")
                if summary["mean_gross_spread"] is None or summary[
                    "mean_gross_spread"
                ] <= float(gates["minimum_combined_gross_spread"]):
                    reasons.append("combined_gross_spread_not_positive")
                permutation = summary["permutation_p_value"]
                if permutation is None or permutation >= float(
                    gates["maximum_permutation_p_value"]
                ):
                    reasons.append("cross_sectional_permutation_p_above_maximum")
                if sum(value > 0.0 for value in summary["leave_one_industry_out"].values()) < int(
                    gates["minimum_positive_leave_one_industry_out_count"]
                ):
                    reasons.append("insufficient_positive_leave_one_industry_out")
                if summary["max_industry_contribution_share"] is None or summary[
                    "max_industry_contribution_share"
                ] > float(gates["maximum_industry_contribution_share"]):
                    reasons.append("industry_contribution_too_concentrated")
            summary["gate_reasons"] = reasons
            summary["passed"] = not reasons

    def _choose_winner(self, summaries: Mapping[str, Mapping[str, Any]]) -> str | None:
        passing = [summary for summary in summaries.values() if summary["passed"]]
        if not passing:
            return None
        top_value = max(float(summary["median_window_ic"]) for summary in passing)
        near_tie = float(
            self.hypothesis["winner_rule"][
                "near_tie_absolute_ic_difference_strictly_below"
            ]
        )
        eligible = [
            summary
            for summary in passing
            if top_value - float(summary["median_window_ic"]) < near_tie
        ]
        selected = min(
            eligible,
            key=lambda summary: (
                int(summary["lookback_sessions"]),
                str(summary["candidate_id"]),
            ),
        )
        return str(selected["candidate_id"])

    def _evaluate(
        self,
        bundle: EvidenceBundle,
        *,
        stage: str,
        after_date: date | None = None,
        candidate_ids: set[str] | None = None,
    ) -> _Evaluation:
        self._validate_source(bundle, stage)
        self._validate_universe(bundle)
        expected_weeks, window_by_week = self._expected_weeks(
            bundle,
            stage,
            after_date=after_date,
            candidate_ids=candidate_ids,
        )
        policy = self.hypothesis["stages"][stage]
        exceptions: list[dict[str, Any]] = []
        if len(expected_weeks) < int(policy["total_weeks"]):
            exceptions.append(
                self._exception(
                    stage,
                    "insufficient_expected_weeks",
                    f"found {len(expected_weeks)} eligible week ends; "
                    f"required {policy['total_weeks']}",
                )
            )
        observations, observation_exceptions, valid_weeks_by_window = (
            self._build_observations(
                bundle,
                stage,
                expected_weeks,
                window_by_week,
                candidate_ids=candidate_ids,
            )
        )
        exceptions.extend(observation_exceptions)
        weekly_metrics, metric_exceptions = self._build_weekly_metrics(
            observations, stage
        )
        exceptions.extend(metric_exceptions)
        valid_week_dates = {
            date.fromisoformat(str(item["week_end"])) for item in weekly_metrics
        }
        observations = [
            item for item in observations if item.week_end in valid_week_dates
        ]
        valid_weeks_by_window = {
            window_id: sum(
                week_end in valid_week_dates
                for week_end, assigned in window_by_week.items()
                if assigned == window_id
            )
            for window_id in valid_weeks_by_window
        }
        total_weeks = int(policy["total_weeks"])
        coverage = len(valid_week_dates) / total_weeks
        summaries, window_metrics = self._summarize_candidates(
            stage=stage,
            observations=observations,
            weekly_metrics=weekly_metrics,
            coverage=coverage,
            valid_weeks_by_window=valid_weeks_by_window,
            candidate_ids=candidate_ids,
        )
        self._apply_gates(
            stage=stage,
            summaries=summaries,
            coverage=coverage,
            valid_weeks_by_window=valid_weeks_by_window,
        )
        winner = self._choose_winner(summaries) if stage == "screen" else None
        for candidate_id, summary in summaries.items():
            window_metrics.append(
                self._window_row(
                    row_type="aggregate",
                    stage=stage,
                    candidate_id=candidate_id,
                    window_id="ALL",
                    week_count=summary["week_count"],
                    mean_ic=summary["mean_ic"],
                    median_ic=summary["median_window_ic"],
                    ic_std=summary["ic_std"],
                    overlap_adjusted_icir=summary["overlap_adjusted_icir"],
                    mean_gross_spread=summary["mean_gross_spread"],
                    positive_window_count=summary["positive_window_count"],
                    bootstrap_lower_95=summary["bootstrap_lower_95"],
                    bootstrap_p_value=summary["bootstrap_p_value"],
                    permutation_p_value=summary["permutation_p_value"],
                    holm_adjusted_p_value=summary["holm_adjusted_p_value"],
                    max_industry_contribution_share=summary[
                        "max_industry_contribution_share"
                    ],
                    leave_one_industry_out_min_mean_ic=summary[
                        "leave_one_industry_out_min_mean_ic"
                    ],
                    passed=str(bool(summary["passed"])).lower(),
                    gate_reasons="|".join(summary["gate_reasons"]),
                    selected_winner=str(candidate_id == winner).lower()
                    if stage == "screen"
                    else "",
                )
            )
        return _Evaluation(
            stage=stage,
            expected_week_count=total_weeks,
            selected_week_ends=list(expected_weeks),
            observations=observations,
            weekly_metrics=weekly_metrics,
            window_metrics=window_metrics,
            exceptions=exceptions,
            summaries=summaries,
            coverage=coverage,
            valid_weeks_by_window=valid_weeks_by_window,
            selected_winner=winner,
        )

    def _reconcile_sources(
        self,
        screen_bundle: EvidenceBundle,
        official_bundle: EvidenceBundle,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return the frozen source comparison evidence for the experiment version."""

        if self.is_same_source_v2:
            return (
                [
                    {
                        "canonical_id": "ALL",
                        "matched_return_dates": None,
                        "screen_return_dates": None,
                        "official_return_dates": None,
                        "date_coverage": None,
                        "median_abs_return_diff_bps": None,
                        "p99_abs_return_diff_bps": None,
                        "passed": "not_applicable",
                        "reason": "not_applicable_same_source",
                    }
                ],
                True,
            )

        policy = self.hypothesis["source_policy"]["reconciliation"]
        screen_bars = screen_bundle.bar_by_key
        official_bars = official_bundle.bar_by_key
        rows: list[dict[str, Any]] = []
        overall = True
        pairs = [
            (f"RECON__{industry_id}", industry_id)
            for industry_id in self.industry_ids
        ] + [(self.benchmark_id, self.benchmark_id)]
        for screen_id, official_id in pairs:
            screen_dates = sorted(
                trading_day
                for canonical_id, trading_day in screen_bars
                if canonical_id == screen_id
            )
            official_dates = sorted(
                trading_day
                for canonical_id, trading_day in official_bars
                if canonical_id == official_id
            )
            screen_returns = {
                current: float(
                    screen_bars[(screen_id, current)].close
                    / screen_bars[(screen_id, previous)].close
                    - Decimal("1")
                )
                for previous, current in zip(screen_dates, screen_dates[1:])
            }
            official_returns = {
                current: float(
                    official_bars[(official_id, current)].close
                    / official_bars[(official_id, previous)].close
                    - Decimal("1")
                )
                for previous, current in zip(official_dates, official_dates[1:])
            }
            matched_dates = sorted(set(screen_returns) & set(official_returns))
            union_dates = set(screen_returns) | set(official_returns)
            coverage = len(matched_dates) / len(union_dates) if union_dates else 0.0
            differences = [
                abs(screen_returns[trading_day] - official_returns[trading_day])
                * 10000.0
                for trading_day in matched_dates
            ]
            median_diff = statistics.median(differences) if differences else None
            p99_diff = _quantile(differences, 0.99)
            reasons: list[str] = []
            if len(matched_dates) < int(policy["minimum_matched_return_dates"]):
                reasons.append("matched_return_dates_below_minimum")
            if coverage < float(policy["minimum_date_coverage"]):
                reasons.append("calendar_coverage_below_minimum")
            if median_diff is None or median_diff > float(
                policy["maximum_median_abs_return_diff_bps"]
            ):
                reasons.append("median_return_difference_above_maximum")
            if p99_diff is None or p99_diff > float(
                policy["maximum_p99_abs_return_diff_bps"]
            ):
                reasons.append("p99_return_difference_above_maximum")
            passed = not reasons
            overall = overall and passed
            rows.append(
                {
                    "canonical_id": official_id,
                    "matched_return_dates": len(matched_dates),
                    "screen_return_dates": len(screen_returns),
                    "official_return_dates": len(official_returns),
                    "date_coverage": coverage,
                    "median_abs_return_diff_bps": median_diff,
                    "p99_abs_return_diff_bps": p99_diff,
                    "passed": str(passed).lower(),
                    "reason": "|".join(reasons),
                }
            )
        return rows, overall

    @staticmethod
    def _prepare_output(output_dir: Path | str) -> Path:
        target = Path(output_dir)
        if target.exists():
            existing = list(target.iterdir())
            if existing:
                raise FactorLabError("output directory must be empty; runs are immutable")
        target.mkdir(parents=True, exist_ok=True)
        return target

    @staticmethod
    def _write_json(path: Path, value: Any) -> None:
        path.write_bytes(_canonical_json_bytes(value) + b"\n")

    @staticmethod
    def _write_csv(path: Path, fields: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="raise")
            writer.writeheader()
            for supplied in rows:
                row = {}
                for field in fields:
                    value = supplied.get(field, "")
                    if isinstance(value, float):
                        value = _format_number(value)
                    elif value is None:
                        value = ""
                    row[field] = value
                writer.writerow(row)

    def _universe_manifest(
        self,
        *,
        stage: str,
        bundle: EvidenceBundle | None,
        selected_weeks: Sequence[date],
    ) -> dict[str, Any]:
        manifest = {
            "schema_version": "factor-lab-universe-manifest.v1",
            "hypothesis_id": self.hypothesis_card["hypothesis_id"],
            "stage": stage,
            "classification": self.hypothesis["universe"]["classification"],
            "point_in_time_required": True,
            "benchmark": self.hypothesis["universe"]["benchmark"],
            "industries": self.hypothesis["universe"]["industries"],
            "source_index_ids": self.hypothesis["universe"]["source_index_ids"],
            "bundle_id": bundle.bundle_id if bundle else None,
            "bundle_sha256": bundle.evidence_sha256 if bundle else None,
            "first_selected_week": selected_weeks[0].isoformat() if selected_weeks else None,
            "last_selected_week": selected_weeks[-1].isoformat() if selected_weeks else None,
            "selected_week_count": len(selected_weeks),
        }
        if self.is_same_source_v2:
            manifest["source_bundle_sha256"] = (
                list(bundle.source_bundle_sha256) if bundle else []
            )
        return manifest

    @staticmethod
    def _coverage_report_lines(evaluation: _Evaluation | None) -> list[str]:
        if evaluation is None:
            return [
                "- 完整样本周：`未运行`",
                "- 各窗口完整样本周：`未运行`",
            ]
        complete_case_weeks = sum(evaluation.valid_weeks_by_window.values())
        window_counts = "；".join(
            f"{window_id}={count}"
            for window_id, count in evaluation.valid_weeks_by_window.items()
        )
        return [
            f"- 完整样本周：`{complete_case_weeks} / {evaluation.expected_week_count}`（complete-case）",
            f"- 各窗口完整样本周：`{window_counts or '无'}`",
        ]

    def _publish(
        self,
        output_dir: Path | str,
        *,
        stage: str,
        bundle: EvidenceBundle | None,
        evaluation: _Evaluation | None,
        reconciliation_rows: Sequence[Mapping[str, Any]] = (),
        status: Mapping[str, Any],
        selected_winner: str | None = None,
        upstream_runs: Mapping[str, str] | None = None,
        extra_exceptions: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        target = self._prepare_output(output_dir)
        observations = evaluation.observations if evaluation else []
        weekly_metrics = evaluation.weekly_metrics if evaluation else []
        window_metrics = evaluation.window_metrics if evaluation else []
        exceptions = [*(evaluation.exceptions if evaluation else []), *extra_exceptions]
        selected_weeks = evaluation.selected_week_ends if evaluation else []
        self._write_json(target / "hypothesis_card.json", self.hypothesis_card)
        self._write_json(
            target / "universe_manifest.json",
            self._universe_manifest(
                stage=stage, bundle=bundle, selected_weeks=selected_weeks
            ),
        )
        reconciliation_output = list(reconciliation_rows)
        if self.is_same_source_v2 and not reconciliation_output:
            reconciliation_output, _ = self._reconcile_sources(None, None)  # type: ignore[arg-type]
        self._write_csv(
            target / "source_reconciliation.csv",
            RECONCILIATION_FIELDS,
            reconciliation_output,
        )
        self._write_csv(
            target / "factor_observations.csv",
            FACTOR_OBSERVATION_FIELDS,
            [item.as_csv_row() for item in observations],
        )
        self._write_csv(
            target / "weekly_metrics.csv", WEEKLY_METRIC_FIELDS, weekly_metrics
        )
        self._write_csv(
            target / "window_metrics.csv", WINDOW_METRIC_FIELDS, window_metrics
        )
        self._write_csv(target / "exceptions.csv", EXCEPTION_FIELDS, exceptions)
        report_lines = [
            f"# Factor Lab {'V2' if self.is_same_source_v2 else 'V1'} 研究报告",
            "",
            f"- 阶段：`{stage}`",
            f"- 假设：`{self.hypothesis_card['hypothesis_id']}`",
            f"- 用户主观卡：`{self.subjective_thesis['status']}`",
            f"- 冻结赢家：`{selected_winner or '无'}`",
            *self._coverage_report_lines(evaluation),
            f"- 统计状态：`{status.get('statistics')}`",
            f"- 来源认证：`{status.get('source_authentication')}`",
            f"- 研究准入：`{status.get('research_admission')}`",
            f"- 产物性质：`{'研究准入' if status.get('research_admission') == 'admitted' else '诊断观察'}`",
            "- 安全边界：`research_only_no_trading_bridge`",
            "",
            "Screen 结果只用于冻结一个诊断赢家；Confirm 不搜索替代候选。",
            "用户尚未登记具体行业观点；主观卡不参与打分。"
            if self.subjective_thesis["status"] == "user_view_not_provided"
            else "用户主观卡只用于预注册研究方向，不参与因子分数计算。",
            (
                "V2 是 CSI 同源跨时期/跨代际 holdout，不是独立来源确认；source_reconciliation 固定为 not_applicable_same_source；任何来源字符串、布尔值或哈希都不能解锁正式准入。"
                if self.is_same_source_v2
                else "当前 V1 的完整官方确认链仍未配置完成；任何来源字符串、布尔值或哈希都不能解锁正式准入。"
            ),
            "前三减后三组合只作描述，不含交易成本，也不是交易信号。",
        ]
        (target / "factor_report.md").write_text(
            "\n".join(report_lines) + "\n", encoding="utf-8"
        )
        artifact_hashes = {
            filename: _sha256_file(target / filename)
            for filename in ARTIFACT_FILENAMES
            if filename != "run_manifest.json"
        }
        repository_root = Path(__file__).resolve().parents[2]
        git_commit, git_dirty, git_diff_sha256 = git_worktree_state(repository_root)
        reproducibility_status = (
            "captured"
            if git_commit is not None
            and git_dirty is not None
            and (git_dirty is False or git_diff_sha256 is not None)
            else "unavailable_fail_closed"
        )
        manifest_core = {
            "schema_version": "factor-lab-run-manifest.v1",
            "stage": stage,
            "hypothesis_id": self.hypothesis_card["hypothesis_id"],
            "hypothesis_sha256": self.hypothesis_sha256,
            "factor_lab_config_sha256": self.config_sha256,
            "subjective_thesis": self.subjective_thesis,
            "git_commit": git_commit,
            "git_dirty": git_dirty,
            "git_diff_sha256": git_diff_sha256,
            "reproducibility_status": reproducibility_status,
            "paper_eligibility": False,
            "trade_eligibility": False,
            "live_execution_status": "live_not_supported",
            "bundle_id": bundle.bundle_id if bundle else None,
            "bundle_sha256": bundle.evidence_sha256 if bundle else None,
            "selected_winner": selected_winner,
            "status": dict(status),
            "upstream_runs": dict(upstream_runs or {}),
            "expected_artifacts": list(ARTIFACT_FILENAMES),
            "artifact_sha256": artifact_hashes,
            "safety": {
                "mode": "research_only",
                "live": "not_supported",
                "trading_bridge": "forbidden",
            },
        }
        if self.is_same_source_v2:
            manifest_core["source_bundle_sha256"] = (
                list(bundle.source_bundle_sha256) if bundle else []
            )
        manifest = {**manifest_core, "run_id": _sha256_value(manifest_core)}
        self._write_json(target / "run_manifest.json", manifest)
        return manifest

    def preregister(self, output_dir: Path | str) -> dict[str, Any]:
        return self._publish(
            output_dir,
            stage="preregister",
            bundle=None,
            evaluation=None,
            status={
                "hypothesis": "frozen",
                "data": "not_supplied",
                "statistics": "not_run",
                "source_authentication": "not_applicable",
                "research_admission": "not_screened",
                "safety": "research_only_no_trading_bridge",
            },
        )

    def screen(
        self, evidence: EvidenceBundle | Path | str | Mapping[str, Any], output_dir: Path | str
    ) -> dict[str, Any]:
        bundle = evidence if isinstance(evidence, EvidenceBundle) else EvidenceBundle.from_json(evidence)
        evaluation = self._evaluate(bundle, stage="screen")
        passed = evaluation.selected_winner is not None
        return self._publish(
            output_dir,
            stage="screen",
            bundle=bundle,
            evaluation=evaluation,
            selected_winner=evaluation.selected_winner,
            status={
                "hypothesis": "frozen",
                "data": "complete_case_passed"
                if all(
                    count >= self.hypothesis["stages"]["screen"]["minimum_weeks_per_window"]
                    for count in evaluation.valid_weeks_by_window.values()
                )
                else "insufficient_coverage",
                "statistics": "screen_passed" if passed else "screen_failed",
                "source_authentication": (
                    "official_historical_backfill_integrity_only"
                    if self.is_same_source_v2
                    else "licensed_secondary_probe_integrity_only"
                ),
                "research_admission": "diagnostic_not_admitted",
                "safety": "research_only_no_trading_bridge",
            },
        )

    def _read_verified_manifest(self, run_dir: Path | str) -> dict[str, Any]:
        target = Path(run_dir)
        expected = set(ARTIFACT_FILENAMES)
        observed = {item.name for item in target.iterdir() if item.is_file()}
        if observed != expected:
            raise FactorLabError(
                f"run artifact set mismatch: missing={sorted(expected-observed)}, "
                f"unknown={sorted(observed-expected)}"
            )
        try:
            manifest = json.loads((target / "run_manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FactorLabError("cannot read run manifest") from exc
        if not isinstance(manifest, dict):
            raise FactorLabError("run manifest must be an object")
        required_manifest_keys = {
            "schema_version",
            "stage",
            "hypothesis_id",
            "hypothesis_sha256",
            "factor_lab_config_sha256",
            "subjective_thesis",
            "git_commit",
            "git_dirty",
            "git_diff_sha256",
            "reproducibility_status",
            "paper_eligibility",
            "trade_eligibility",
            "live_execution_status",
            "bundle_id",
            "bundle_sha256",
            "selected_winner",
            "status",
            "upstream_runs",
            "expected_artifacts",
            "artifact_sha256",
            "safety",
            "run_id",
        }
        if self.is_same_source_v2:
            required_manifest_keys.add("source_bundle_sha256")
        if set(manifest) != required_manifest_keys:
            raise FactorLabError("run manifest field set mismatch")
        if (
            manifest.get("schema_version") != "factor-lab-run-manifest.v1"
            or manifest.get("hypothesis_id") != self.hypothesis_card["hypothesis_id"]
            or manifest.get("hypothesis_sha256") != self.hypothesis_sha256
            or manifest.get("factor_lab_config_sha256") != self.config_sha256
            or manifest.get("subjective_thesis") != self.subjective_thesis
        ):
            raise FactorLabError("run manifest frozen identity mismatch")
        if (
            manifest.get("paper_eligibility") is not False
            or manifest.get("trade_eligibility") is not False
            or manifest.get("live_execution_status") != "live_not_supported"
            or manifest.get("safety")
            != {
                "mode": "research_only",
                "live": "not_supported",
                "trading_bridge": "forbidden",
            }
        ):
            raise FactorLabError("run manifest safety boundary mismatch")
        source_bundle_hashes = manifest.get("source_bundle_sha256", [])
        if self.is_same_source_v2 and (
            not isinstance(source_bundle_hashes, list)
            or len(source_bundle_hashes) != len(set(source_bundle_hashes))
            or any(
                not isinstance(value, str) or not _SHA256_RE.fullmatch(value)
                for value in source_bundle_hashes
            )
        ):
            raise FactorLabError("run manifest source bundle hash binding is invalid")
        stage = manifest.get("stage")
        if stage not in {"preregister", "screen", "confirm", "weekly"}:
            raise FactorLabError("run manifest stage is unsupported")
        if self.is_same_source_v2 and stage == "screen" and not source_bundle_hashes:
            raise FactorLabError("V2 screen manifest must bind source bundle hashes")
        if manifest.get("expected_artifacts") != list(ARTIFACT_FILENAMES):
            raise FactorLabError("run manifest expected artifact contract mismatch")
        status = manifest.get("status")
        if not isinstance(status, dict):
            raise FactorLabError("run manifest status must be an object")
        allowed_status_keys = {
            "hypothesis",
            "data",
            "statistics",
            "source_authentication",
            "research_admission",
            "admission_block_reason",
            "safety",
        }
        required_status_keys = allowed_status_keys - {"admission_block_reason"}
        if not required_status_keys.issubset(status) or not set(status).issubset(
            allowed_status_keys
        ):
            raise FactorLabError("run manifest status field set mismatch")
        if status.get("hypothesis") != "frozen" or status.get("safety") != "research_only_no_trading_bridge":
            raise FactorLabError("run manifest status safety mismatch")
        allowed_stage_states = {
            "preregister": {
                "statistics": {"not_run"},
                "source_authentication": {"not_applicable"},
                "research_admission": {"not_screened"},
            },
            "screen": {
                "statistics": {"screen_passed", "screen_failed"},
                "source_authentication": {
                    "official_historical_backfill_integrity_only"
                    if self.is_same_source_v2
                    else "licensed_secondary_probe_integrity_only"
                },
                "research_admission": {"diagnostic_not_admitted"},
            },
            "confirm": {
                "statistics": {"confirm_passed", "confirm_failed"},
                "source_authentication": {"not_configured"},
                "research_admission": {
                    "blocked_source_disagreement",
                    "blocked_confirm_failed",
                    "confirmed_not_admitted",
                },
            },
            "weekly": {
                "statistics": {"inherited_confirm_passed"},
                "source_authentication": {"not_configured"},
                "research_admission": {"diagnostic_weekly_source_authentication_blocked"},
            },
        }
        for field, allowed in allowed_stage_states[str(stage)].items():
            if status.get(field) not in allowed:
                raise FactorLabError(f"run manifest {stage} {field} is invalid")
        winner = manifest.get("selected_winner")
        frozen_candidates = {spec.candidate_id for spec in self.plugin.specs}
        if winner is not None and winner not in frozen_candidates:
            raise FactorLabError("run manifest winner is outside the frozen family")
        if stage in {"preregister"} and winner is not None:
            raise FactorLabError("preregister run cannot select a winner")
        if stage in {"confirm", "weekly"} and winner is None:
            raise FactorLabError(f"{stage} run must bind the frozen screen winner")
        hashes = manifest.get("artifact_sha256")
        if not isinstance(hashes, dict) or set(hashes) != expected - {"run_manifest.json"}:
            raise FactorLabError("run manifest artifact hash set mismatch")
        for filename, expected_hash in hashes.items():
            if not isinstance(expected_hash, str) or not _SHA256_RE.fullmatch(expected_hash):
                raise FactorLabError(f"artifact hash is invalid: {filename}")
            if _sha256_file(target / filename) != expected_hash:
                raise FactorLabError(f"artifact hash mismatch: {filename}")
        if self.is_same_source_v2:
            try:
                universe_manifest = json.loads(
                    (target / "universe_manifest.json").read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise FactorLabError("cannot read universe manifest") from exc
            if (
                not isinstance(universe_manifest, dict)
                or universe_manifest.get("source_bundle_sha256") != source_bundle_hashes
            ):
                raise FactorLabError("run manifest conflicts with source bundle hash artifact")
        try:
            with (target / "window_metrics.csv").open(
                "r", encoding="utf-8", newline=""
            ) as handle:
                reader = csv.DictReader(handle)
                if tuple(reader.fieldnames or ()) != WINDOW_METRIC_FIELDS:
                    raise FactorLabError("window metrics field contract mismatch")
                metric_rows = list(reader)
        except OSError as exc:
            raise FactorLabError("cannot read window metrics") from exc
        aggregate_rows = [
            row
            for row in metric_rows
            if row.get("row_type") == "aggregate" and row.get("stage") == stage
        ]
        if any(row.get(None) for row in metric_rows):
            raise FactorLabError("window metrics contains overflow fields")
        if stage == "screen":
            marked = [
                str(row.get("candidate_id"))
                for row in aggregate_rows
                if row.get("selected_winner") == "true"
            ]
            if any(
                row.get("selected_winner") not in {"true", "false"}
                for row in aggregate_rows
            ):
                raise FactorLabError("screen winner marker is invalid")
            expected_marked = [str(winner)] if winner is not None else []
            if marked != expected_marked:
                raise FactorLabError("manifest winner conflicts with window metrics")
            if status.get("statistics") == "screen_passed" and len(marked) != 1:
                raise FactorLabError("passing screen must bind exactly one winner")
            if status.get("statistics") == "screen_failed" and marked:
                raise FactorLabError("failed screen cannot bind a winner")
        elif stage == "confirm":
            aggregate_candidates = {str(row.get("candidate_id")) for row in aggregate_rows}
            if aggregate_candidates != {str(winner)}:
                raise FactorLabError("confirm metrics do not bind only the frozen winner")
        elif aggregate_rows:
            raise FactorLabError(f"{stage} run cannot contain aggregate factor metrics")
        core = {key: value for key, value in manifest.items() if key != "run_id"}
        if manifest.get("run_id") != _sha256_value(core):
            raise FactorLabError("run_id mismatch")
        return manifest

    def verify(self, run_dir: Path | str) -> dict[str, Any]:
        manifest = self._read_verified_manifest(run_dir)
        if manifest.get("hypothesis_sha256") != self.hypothesis_sha256:
            raise FactorLabError("run hypothesis differs from current frozen hypothesis")
        return {
            "status": "verified",
            "run_id": manifest["run_id"],
            "stage": manifest["stage"],
            "research_admission": manifest["status"]["research_admission"],
        }

    def confirm(
        self,
        evidence: EvidenceBundle | Path | str | Mapping[str, Any],
        *,
        screen_evidence: EvidenceBundle | Path | str | Mapping[str, Any],
        screen_run: Path | str,
        output_dir: Path | str,
    ) -> dict[str, Any]:
        manifest = self._read_verified_manifest(screen_run)
        if (
            manifest.get("stage") != "screen"
            or manifest.get("hypothesis_sha256") != self.hypothesis_sha256
            or not manifest.get("selected_winner")
            or manifest.get("status", {}).get("statistics") != "screen_passed"
        ):
            raise FactorLabError("confirm requires a verified passing screen run")
        winner = str(manifest["selected_winner"])
        official = evidence if isinstance(evidence, EvidenceBundle) else EvidenceBundle.from_json(evidence)
        screen_bundle = (
            screen_evidence
            if isinstance(screen_evidence, EvidenceBundle)
            else EvidenceBundle.from_json(screen_evidence)
        )
        self._validate_source(screen_bundle, "screen")
        self._validate_universe(screen_bundle)
        evaluation = self._evaluate(
            official, stage="confirm", candidate_ids={winner}
        )
        reconciliation_rows, reconciliation_passed = self._reconcile_sources(
            screen_bundle, official
        )
        winner_summary = evaluation.summaries[winner]
        statistical_passed = bool(winner_summary["passed"])
        exceptions: list[dict[str, Any]] = []
        if not reconciliation_passed:
            exceptions.append(
                self._exception(
                    "confirm",
                    "source_disagreement",
                    "Choice-current and CSI same-code reconciliation failed",
                )
            )
        if not reconciliation_passed:
            admission = "blocked_source_disagreement"
        elif not statistical_passed:
            admission = "blocked_confirm_failed"
        else:
            admission = "confirmed_not_admitted"
        return self._publish(
            output_dir,
            stage="confirm",
            bundle=official,
            evaluation=evaluation,
            reconciliation_rows=reconciliation_rows,
            selected_winner=winner,
            upstream_runs={"screen_run_id": str(manifest["run_id"])},
            extra_exceptions=exceptions,
            status={
                "hypothesis": "frozen",
                "data": (
                    "same_source_temporal_cross_generation_holdout"
                    if self.is_same_source_v2
                    else "reconciled" if reconciliation_passed
                    else "source_disagreement"
                ),
                "statistics": "confirm_passed"
                if statistical_passed
                else "confirm_failed",
                "source_authentication": "not_configured",
                "research_admission": admission,
                "admission_block_reason": (
                    "same_source_holdout_not_independent_replication"
                    if self.is_same_source_v2 and statistical_passed
                    else "source_authentication_not_configured"
                    if statistical_passed and reconciliation_passed
                    else None
                ),
                "safety": "research_only_no_trading_bridge",
            },
        )

    def weekly(
        self,
        evidence: EvidenceBundle | Path | str | Mapping[str, Any],
        *,
        confirmed_run: Path | str,
        output_dir: Path | str,
        as_of: date | None = None,
    ) -> dict[str, Any]:
        """Publish the latest official-series diagnostic signals, never orders."""

        manifest = self._read_verified_manifest(confirmed_run)
        if (
            manifest.get("stage") != "confirm"
            or manifest.get("hypothesis_sha256") != self.hypothesis_sha256
            or manifest.get("status", {}).get("statistics") != "confirm_passed"
            or not manifest.get("selected_winner")
        ):
            raise FactorLabError("weekly requires a verified statistically passing confirm run")
        winner = str(manifest["selected_winner"])
        spec = next(
            (item for item in self.plugin.specs if item.candidate_id == winner), None
        )
        if spec is None:
            raise FactorLabError("confirmed winner is outside the frozen candidate family")
        bundle = evidence if isinstance(evidence, EvidenceBundle) else EvidenceBundle.from_json(evidence)
        self._validate_source(bundle, "weekly")
        self._validate_universe(bundle)
        bars = bundle.bar_by_key
        calendar_index = {trading_day: index for index, trading_day in enumerate(bundle.calendar)}
        retrieved_at = _parse_time(bundle.source["retrieved_at"], "source.retrieved_at")
        candidates = [
            trading_day
            for trading_day in _week_end_sessions(bundle.calendar)
            if calendar_index[trading_day] >= spec.lookback_sessions
            and (as_of is None or trading_day <= as_of)
            and trading_day
            >= date.fromisoformat(
                self.hypothesis["time_policy"]["confirm_series_inception"]
            )
        ]
        selected: date | None = None
        for week_end in reversed(candidates):
            then = bundle.calendar[calendar_index[week_end] - spec.lookback_sessions]
            needed = [(canonical_id, point) for canonical_id in (*self.industry_ids, self.benchmark_id) for point in (then, week_end)]
            if all(key in bars and bars[key].available_at <= retrieved_at for key in needed):
                selected = week_end
                break
        if selected is None:
            raise FactorLabError("weekly evidence has no complete mature signal week")
        lookback_date = bundle.calendar[calendar_index[selected] - spec.lookback_sessions]
        names = {str(item["canonical_id"]): str(item["name"]) for item in self.industry_items}
        raw = []
        for industry_id in self.industry_ids:
            signal = self.plugin.compute(
                spec,
                industry_now=bars[(industry_id, selected)].close,
                industry_then=bars[(industry_id, lookback_date)].close,
                benchmark_now=bars[(self.benchmark_id, selected)].close,
                benchmark_then=bars[(self.benchmark_id, lookback_date)].close,
            )
            raw.append((industry_id, signal))
        ranks = _average_ranks([item[1] for item in raw])
        observations = [
            FactorObservation(
                stage="weekly",
                window_id="weekly-current",
                week_end=selected,
                label_start=None,
                label_end=None,
                candidate_id=winner,
                lookback_sessions=spec.lookback_sessions,
                industry_id=industry_id,
                industry_name=names[industry_id],
                signal=signal,
                signal_rank=rank,
                forward_excess_return_20d=None,
                source_bundle_id=bundle.bundle_id,
            )
            for (industry_id, signal), rank in zip(raw, ranks)
        ]
        evaluation = _Evaluation(
            stage="weekly",
            expected_week_count=1,
            selected_week_ends=[selected],
            observations=observations,
            weekly_metrics=[],
            window_metrics=[],
            exceptions=[],
            summaries={},
            coverage=1.0,
            valid_weeks_by_window={"weekly-current": 1},
            selected_winner=winner,
        )
        return self._publish(
            output_dir,
            stage="weekly",
            bundle=bundle,
            evaluation=evaluation,
            selected_winner=winner,
            upstream_runs={"confirm_run_id": str(manifest["run_id"])},
            status={
                "hypothesis": "frozen",
                "data": "latest_complete_signal",
                "statistics": "inherited_confirm_passed",
                "source_authentication": "not_configured",
                "research_admission": "diagnostic_weekly_source_authentication_blocked",
                "safety": "research_only_no_trading_bridge",
            },
        )
