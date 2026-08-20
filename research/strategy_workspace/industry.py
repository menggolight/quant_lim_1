"""Read-only CSI industry evidence adapter for the strategy workspace.

The adapter intentionally produces a *diagnostic* universe.  CSI industry
indices are not tradable instruments, so callers must not promote results from
this module to Paper or trade eligibility without a separately frozen ETF or
stock mapping.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..market_data.index_evidence import (
    IndexEvidenceBundle,
    IndexEvidenceError,
    IndexEvidenceStorage,
)
from .backtest import BenchmarkClose, DailyClose, FrozenSignal


INDUSTRY_ADAPTER_VERSION = "strategy-workspace-csi-industry.v1"
_CHINA_TIMEZONE = timezone(timedelta(hours=8))
_NEXT_CLOSE_EXECUTION_TIME = time(15, 30)


class IndustryEvidenceError(ValueError):
    """Raised when the evidence bundle cannot support a closed diagnostic."""


@dataclass(frozen=True)
class IndustryEvidence:
    bars: tuple[DailyClose, ...]
    benchmark: tuple[BenchmarkClose, ...]
    industry_ids: tuple[str, ...]
    benchmark_id: str
    evidence_sha256: str
    hypothesis_sha256: str
    controlled_storage_verified: bool
    receipt_verified: bool
    receipt_sha256: str | None
    availability_by_date: tuple[tuple[date, datetime], ...]
    admission_status: str
    point_in_time_status: str


@dataclass(frozen=True)
class RelativeMomentumScore:
    signal_id: str
    signal_date: date
    factor_id: str
    instrument_id: str
    score: float
    rank: int
    selected: bool
    input_available_at_max: datetime


def _read_json(path: Path) -> tuple[Mapping[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IndustryEvidenceError(f"cannot read JSON evidence: {path}") from exc
    if not isinstance(payload, Mapping):
        raise IndustryEvidenceError(f"JSON root must be an object: {path}")
    return payload, raw


def _full_index_id(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        raise IndustryEvidenceError("index id is required")
    return text if text.endswith(".CSI") else f"{text}.CSI"


def load_csi_industry_evidence(
    evidence_path: str | Path,
    hypothesis_path: str | Path,
    *,
    mapping_key: str = "choice_screen",
    evidence_root: str | Path | None = None,
) -> IndustryEvidence:
    """Load CSI evidence, admitting it only after controlled-storage replay."""

    evidence_file = Path(evidence_path)
    hypothesis_file = Path(hypothesis_path)
    evidence, evidence_raw = _read_json(evidence_file)
    hypothesis, hypothesis_raw = _read_json(hypothesis_file)

    controlled_storage_verified = False
    receipt_verified = False
    receipt_sha256: str | None = None
    admission_status = "not_admitted_unverified_evidence"
    if evidence_root is not None:
        controlled_root = Path(evidence_root).resolve()
        resolved_evidence = evidence_file.resolve()
        try:
            relative_evidence = resolved_evidence.relative_to(controlled_root)
        except ValueError as exc:
            raise IndustryEvidenceError(
                "evidence is outside the controlled evidence root"
            ) from exc
        try:
            evidence_segment = relative_evidence.parts.index("evidence")
        except ValueError as exc:
            raise IndustryEvidenceError(
                "controlled evidence path is not inside an evidence bucket"
            ) from exc
        if evidence_segment == 0 or len(relative_evidence.parts) != evidence_segment + 5:
            raise IndustryEvidenceError("controlled evidence path has an invalid layout")
        storage_root = controlled_root.joinpath(
            *relative_evidence.parts[:evidence_segment]
        )
        try:
            candidate = IndexEvidenceBundle.from_dict(evidence)
            loaded, _, loaded_path = IndexEvidenceStorage(storage_root).load(
                candidate.provider_id,
                candidate.request_fingerprint,
                candidate.dataset_type,
                as_of=candidate.fetched_at,
            )
        except (IndexEvidenceError, OSError, ValueError) as exc:
            raise IndustryEvidenceError(
                "controlled evidence replay verification failed"
            ) from exc
        if loaded_path.resolve() != resolved_evidence:
            raise IndustryEvidenceError(
                "controlled replay selected a different evidence bundle"
            )
        if loaded.evidence_id != candidate.evidence_id:
            raise IndustryEvidenceError("controlled replay evidence_id mismatch")
        if loaded.provider_id != "csi_official" or loaded.source_id != "csi_official":
            raise IndustryEvidenceError("controlled evidence source/provider is not CSI")
        receipt_path = loaded_path.with_suffix(".receipt")
        try:
            receipt_raw = receipt_path.read_bytes()
        except OSError as exc:
            raise IndustryEvidenceError("controlled evidence receipt is missing") from exc
        evidence = loaded.to_dict()
        evidence_raw = loaded_path.read_bytes()
        controlled_storage_verified = True
        receipt_verified = True
        receipt_sha256 = hashlib.sha256(receipt_raw).hexdigest()
        admission_status = loaded.admission_status

    if evidence.get("schema_version") not in (None, "index-evidence-bundle-v1"):
        raise IndustryEvidenceError("unsupported evidence schema_version")
    if evidence.get("dataset_type") != "index_level":
        raise IndustryEvidenceError("evidence dataset_type must be index_level")
    records = evidence.get("records")
    if not isinstance(records, list) or not records:
        raise IndustryEvidenceError("evidence records must be a non-empty array")

    universe = hypothesis.get("universe")
    if not isinstance(universe, Mapping):
        raise IndustryEvidenceError("hypothesis universe is missing")
    source_maps = universe.get("source_index_ids")
    if not isinstance(source_maps, Mapping):
        raise IndustryEvidenceError("hypothesis source_index_ids is missing")
    selected_mapping = source_maps.get(mapping_key)
    if not isinstance(selected_mapping, Mapping) or not selected_mapping:
        raise IndustryEvidenceError(f"unknown or empty mapping_key: {mapping_key}")

    industry_ids = tuple(_full_index_id(value) for value in selected_mapping.values())
    if len(industry_ids) != len(set(industry_ids)):
        raise IndustryEvidenceError("industry mapping contains duplicate index ids")
    benchmark_id = _full_index_id(source_maps.get("benchmark"))
    expected_ids = set(industry_ids) | {benchmark_id}

    evidence_sha256 = hashlib.sha256(evidence_raw).hexdigest()

    bars: list[DailyClose] = []
    benchmark: list[BenchmarkClose] = []
    seen: set[tuple[str, date]] = set()
    seen_source_record_ids: set[str] = set()
    availability_by_date: dict[date, datetime] = {}
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise IndustryEvidenceError(f"records[{index}] must be an object")
        index_id = _full_index_id(record.get("index_id"))
        if index_id not in expected_ids:
            continue
        if record.get("schema_version") not in (None, "index-level-v1"):
            raise IndustryEvidenceError(
                f"records[{index}] has unsupported schema_version"
            )
        try:
            trading_date = date.fromisoformat(str(record["trading_date"]))
            available_at = datetime.fromisoformat(str(record["available_at"]))
            close = float(record["close"])
        except (KeyError, TypeError, ValueError) as exc:
            raise IndustryEvidenceError(f"records[{index}] has invalid date/close") from exc
        if available_at.tzinfo is None or available_at.utcoffset() is None:
            raise IndustryEvidenceError("available_at must include a timezone offset")
        if available_at.utcoffset() != timedelta(hours=8):
            raise IndustryEvidenceError("available_at must use the +08:00 offset")
        if available_at.date() != trading_date:
            raise IndustryEvidenceError(
                "available_at must be on the signal trading_date"
            )
        source_record_id = str(record.get("source_record_id") or "").strip()
        if controlled_storage_verified and not source_record_id:
            raise IndustryEvidenceError("controlled records require source_record_id")
        if source_record_id:
            if source_record_id in seen_source_record_ids:
                raise IndustryEvidenceError(
                    f"duplicate source_record_id: {source_record_id}"
                )
            seen_source_record_ids.add(source_record_id)
        current_max = availability_by_date.get(trading_date)
        if current_max is None or available_at > current_max:
            availability_by_date[trading_date] = available_at
        if not math.isfinite(close) or close <= 0:
            raise IndustryEvidenceError("close must be finite and positive")
        key = (index_id, trading_date)
        if key in seen:
            raise IndustryEvidenceError(f"duplicate index/date record: {key}")
        seen.add(key)
        if index_id == benchmark_id:
            benchmark.append(BenchmarkClose(trading_date=trading_date, close=close))
        else:
            bars.append(
                DailyClose(
                    instrument_id=index_id,
                    trading_date=trading_date,
                    close=close,
                    lot_size=1,
                )
            )

    observed_ids = {item.instrument_id for item in bars}
    if observed_ids != set(industry_ids):
        missing = sorted(set(industry_ids) - observed_ids)
        raise IndustryEvidenceError(f"evidence is missing mapped industries: {missing}")
    if not benchmark:
        raise IndustryEvidenceError("evidence is missing the benchmark")

    return IndustryEvidence(
        bars=tuple(sorted(bars, key=lambda item: (item.trading_date, item.instrument_id))),
        benchmark=tuple(sorted(benchmark, key=lambda item: item.trading_date)),
        industry_ids=industry_ids,
        benchmark_id=benchmark_id,
        evidence_sha256=evidence_sha256,
        hypothesis_sha256=hashlib.sha256(hypothesis_raw).hexdigest(),
        controlled_storage_verified=controlled_storage_verified,
        receipt_verified=receipt_verified,
        receipt_sha256=receipt_sha256,
        availability_by_date=tuple(sorted(availability_by_date.items())),
        admission_status=admission_status,
        point_in_time_status=str(evidence.get("point_in_time_status") or "unknown"),
    )


def _build_relative_momentum_diagnostic(
    evidence: IndustryEvidence,
    *,
    lookback_sessions: int = 20,
    rebalance_sessions: int = 20,
    top_n: int = 3,
) -> tuple[tuple[FrozenSignal, ...], tuple[RelativeMomentumScore, ...]]:
    """Create non-overlapping, close-known relative-momentum signals."""

    if lookback_sessions not in {20, 60, 120}:
        raise IndustryEvidenceError("lookback_sessions must be one of 20, 60, 120")
    if rebalance_sessions < 1:
        raise IndustryEvidenceError("rebalance_sessions must be positive")
    if top_n < 1 or top_n > min(3, len(evidence.industry_ids)):
        raise IndustryEvidenceError("top_n must be between 1 and 3")

    by_instrument: dict[str, dict[date, float]] = {
        instrument_id: {} for instrument_id in evidence.industry_ids
    }
    for item in evidence.bars:
        by_instrument[item.instrument_id][item.trading_date] = float(item.close)
    benchmark = {item.trading_date: float(item.close) for item in evidence.benchmark}
    common_dates = sorted(
        set(benchmark).intersection(
            *(set(by_instrument[instrument_id]) for instrument_id in evidence.industry_ids)
        )
    )
    if len(common_dates) <= lookback_sessions + 1:
        raise IndustryEvidenceError("insufficient complete dates for momentum signals")

    signals: list[FrozenSignal] = []
    score_ledger: list[RelativeMomentumScore] = []
    availability = dict(evidence.availability_by_date)
    final_signal_index = len(common_dates) - 2  # execution requires a later session
    for current_index in range(
        lookback_sessions,
        final_signal_index + 1,
        rebalance_sessions,
    ):
        current_date = common_dates[current_index]
        then_date = common_dates[current_index - lookback_sessions]
        benchmark_return = math.log(benchmark[current_date] / benchmark[then_date])
        scores = []
        for instrument_id in evidence.industry_ids:
            values = by_instrument[instrument_id]
            relative = math.log(values[current_date] / values[then_date]) - benchmark_return
            scores.append((relative, instrument_id))
        ranked = sorted(
            scores,
            key=lambda item: (item[0], item[1]),
            reverse=True,
        )
        selected = tuple(instrument_id for _, instrument_id in ranked[:top_n])
        signal_id = f"RM{lookback_sessions}-{current_date.isoformat()}"
        input_available_at_max = max(
            availability[current_date],
            availability[then_date],
        )
        execution_date = common_dates[current_index + 1]
        execution_cutoff = datetime.combine(
            execution_date,
            _NEXT_CLOSE_EXECUTION_TIME,
            tzinfo=_CHINA_TIMEZONE,
        )
        if input_available_at_max.astimezone(timezone.utc) > execution_cutoff.astimezone(
            timezone.utc
        ):
            raise IndustryEvidenceError(
                "factor input was unavailable before the next execution close"
            )
        for rank, (score, instrument_id) in enumerate(ranked, start=1):
            score_ledger.append(
                RelativeMomentumScore(
                    signal_id=signal_id,
                    signal_date=current_date,
                    factor_id=f"RM{lookback_sessions}",
                    instrument_id=instrument_id,
                    score=score,
                    rank=rank,
                    selected=instrument_id in selected,
                    input_available_at_max=input_available_at_max,
                )
            )
        signals.append(
            FrozenSignal(
                signal_id=signal_id,
                signal_date=current_date,
                instrument_ids=selected,
            )
        )
    return tuple(signals), tuple(score_ledger)


def build_relative_momentum_signals(
    evidence: IndustryEvidence,
    *,
    lookback_sessions: int = 20,
    rebalance_sessions: int = 20,
    top_n: int = 3,
) -> tuple[FrozenSignal, ...]:
    """Create non-overlapping, close-known relative-momentum signals."""

    signals, _ = _build_relative_momentum_diagnostic(
        evidence,
        lookback_sessions=lookback_sessions,
        rebalance_sessions=rebalance_sessions,
        top_n=top_n,
    )
    return signals


def build_relative_momentum_scores(
    evidence: IndustryEvidence,
    *,
    lookback_sessions: int = 20,
    rebalance_sessions: int = 20,
    top_n: int = 3,
) -> tuple[RelativeMomentumScore, ...]:
    """Return the complete per-signal ranking ledger used by the adapter."""

    _, scores = _build_relative_momentum_diagnostic(
        evidence,
        lookback_sessions=lookback_sessions,
        rebalance_sessions=rebalance_sessions,
        top_n=top_n,
    )
    return scores


__all__ = [
    "INDUSTRY_ADAPTER_VERSION",
    "IndustryEvidence",
    "IndustryEvidenceError",
    "RelativeMomentumScore",
    "build_relative_momentum_scores",
    "build_relative_momentum_signals",
    "load_csi_industry_evidence",
]
