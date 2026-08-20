"""Deterministic non-PIT fallback for the quality-growth workspace.

This module is deliberately unable to produce a Paper or real-money signal.
It only freezes a result-blind, industry-stratified current-universe sample and
computes the six predeclared price diagnostics needed to exercise the panel.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from statistics import stdev
from typing import Any, Iterable, Mapping, Sequence

from .contracts import canonical_sha256


DIAGNOSTIC_STATUS = "diagnostic_current_universe_not_pit"
DIAGNOSTIC_SCHEMA_VERSION = "strategy-workspace-current-universe-diagnostic.v2"
DIAGNOSTIC_SOURCE_UNIVERSE_ID = "CSI800_CURRENT_CHOICE"
SELECTION_POLICY = (
    "sha256_instrument_id_equal_industry_coverage_round_robin_not_index_representative"
)
REPRESENTATION = "diagnostic_equal_industry_coverage_not_csi800_representative"
DIAGNOSTIC_FACTOR_IDS = (
    "RM20",
    "RM60",
    "RM120",
    "TREND_EFF60",
    "DOWNSIDE_VOL60",
    "BREAKOUT60",
)
CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
_INSTRUMENT = re.compile(r"^\d{6}\.(?:SH|SZ)$")
CSI2021_LEVEL1_INDUSTRY_IDS = frozenset(
    {
        "CSI2021_L1/能源",
        "CSI2021_L1/原材料",
        "CSI2021_L1/工业",
        "CSI2021_L1/可选消费",
        "CSI2021_L1/主要消费",
        "CSI2021_L1/医药卫生",
        "CSI2021_L1/金融",
        "CSI2021_L1/信息技术",
        "CSI2021_L1/通信服务",
        "CSI2021_L1/公用事业",
        "CSI2021_L1/房地产",
    }
)
GENERATOR_CODE_BUNDLE_PATHS = frozenset(
    {
        "agent/current_industry_import.py",
        "agent/current_universe_import.py",
        "research/market_data/contracts.py",
        "research/strategy_workspace/contracts.py",
        "research/strategy_workspace/diagnostic.py",
    }
)


class DiagnosticContractError(ValueError):
    """Raised when a fallback input could masquerade as PIT evidence."""


def _identifier(value: Any, field: str) -> str:
    text = str(value or "").strip().upper()
    if not text or any(character.isspace() for character in text):
        raise DiagnosticContractError(f"{field} must be a non-empty identifier")
    return text


@dataclass(frozen=True)
class CurrentUniverseMember:
    instrument_id: str
    industry_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument_id", _identifier(self.instrument_id, "instrument_id"))
        object.__setattr__(self, "industry_id", _identifier(self.industry_id, "industry_id"))
        if _INSTRUMENT.fullmatch(self.instrument_id) is None:
            raise DiagnosticContractError("instrument_id must be an explicit SH/SZ A-share code")
        if self.industry_id not in CSI2021_LEVEL1_INDUSTRY_IDS:
            raise DiagnosticContractError("industry_id must be a CSI 2021 level-1 industry")


@dataclass(frozen=True)
class DiagnosticPriceBar:
    instrument_id: str
    trading_date: date
    close: float
    high: float
    available_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument_id", _identifier(self.instrument_id, "instrument_id"))
        if not isinstance(self.trading_date, date):
            raise DiagnosticContractError("trading_date must be a date")
        for field_name in ("close", "high"):
            value = float(getattr(self, field_name))
            if not math.isfinite(value) or value <= 0:
                raise DiagnosticContractError(f"{field_name} must be finite and positive")
            object.__setattr__(self, field_name, value)
        if self.high < self.close:
            raise DiagnosticContractError("high must be at least close")
        if not isinstance(self.available_at, datetime) or self.available_at.tzinfo is None:
            raise DiagnosticContractError("available_at must be timezone-aware")
        if self.available_at.astimezone(CHINA_TZ).date() != self.trading_date:
            raise DiagnosticContractError("available_at must be on trading_date in Asia/Shanghai")
        local_available = self.available_at.astimezone(CHINA_TZ)
        if (local_available.hour, local_available.minute) < (15, 0):
            raise DiagnosticContractError(
                "close-based diagnostic bar must not be available before the controlled close"
            )


@dataclass(frozen=True)
class DiagnosticFactorRow:
    instrument_id: str
    trading_date: date
    values: Mapping[str, float]

    def __post_init__(self) -> None:
        if tuple(self.values) != DIAGNOSTIC_FACTOR_IDS:
            raise DiagnosticContractError("diagnostic factor fields are not the frozen six")
        if any(not math.isfinite(float(value)) for value in self.values.values()):
            raise DiagnosticContractError("diagnostic factors must be finite")


@dataclass(frozen=True)
class DiagnosticSample:
    information_cutoff_date: date
    market_snapshot_date: date
    source_universe_id: str
    source_member_count: int
    source_membership_artifact_sha256: str
    source_membership_payload_sha256: str
    source_membership_content_sha256: str
    source_industry_artifact_sha256: str
    source_industry_payload_sha256: str
    source_industry_content_sha256: str
    generator_code_bundle_files: Mapping[str, str]
    generator_code_bundle_runtime: str
    generator_code_bundle_sha256: str
    instrument_ids: tuple[str, ...]
    industry_by_instrument: Mapping[str, str]
    status: str = DIAGNOSTIC_STATUS
    paper_eligibility: bool = False
    trade_eligibility: bool = False
    live: str = "not_supported"

    def __post_init__(self) -> None:
        if self.status != DIAGNOSTIC_STATUS:
            raise DiagnosticContractError("fallback status cannot be upgraded")
        if self.paper_eligibility or self.trade_eligibility or self.live != "not_supported":
            raise DiagnosticContractError("fallback can never unlock Paper, trade, or LIVE")
        if len(self.instrument_ids) != 60 or len(set(self.instrument_ids)) != 60:
            raise DiagnosticContractError("diagnostic sample must contain exactly 60 unique instruments")
        if any(_INSTRUMENT.fullmatch(item) is None for item in self.instrument_ids):
            raise DiagnosticContractError("diagnostic sample contains a non-A-share instrument")
        if set(self.industry_by_instrument) != set(self.instrument_ids):
            raise DiagnosticContractError("industry map must exactly cover the sample")
        if any(
            value not in CSI2021_LEVEL1_INDUSTRY_IDS
            for value in self.industry_by_instrument.values()
        ):
            raise DiagnosticContractError("diagnostic sample has an invalid industry")
        if self.source_member_count != 800:
            raise DiagnosticContractError("fallback source must be the complete 800-member current universe")
        if self.source_universe_id != DIAGNOSTIC_SOURCE_UNIVERSE_ID:
            raise DiagnosticContractError("fallback source universe differs from the locked Choice CSI800 id")
        if not isinstance(self.information_cutoff_date, date) or not isinstance(
            self.market_snapshot_date, date
        ):
            raise DiagnosticContractError("diagnostic cutoff and snapshot must be dates")
        if self.market_snapshot_date > self.information_cutoff_date:
            raise DiagnosticContractError("market snapshot cannot postdate information cutoff")
        for field in (
            "source_membership_artifact_sha256",
            "source_membership_payload_sha256",
            "source_membership_content_sha256",
            "source_industry_artifact_sha256",
            "source_industry_payload_sha256",
            "source_industry_content_sha256",
            "generator_code_bundle_sha256",
        ):
            value = getattr(self, field)
            if not isinstance(value, str) or len(value) != 64 or any(
                character not in "0123456789abcdef" for character in value
            ):
                raise DiagnosticContractError(f"{field} must be a SHA-256 digest")
        if not self.generator_code_bundle_runtime:
            raise DiagnosticContractError("generator runtime must be recorded")
        if set(self.generator_code_bundle_files) != GENERATOR_CODE_BUNDLE_PATHS:
            raise DiagnosticContractError("generator code bundle file set differs from the contract")
        if any(
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in self.generator_code_bundle_files.values()
        ):
            raise DiagnosticContractError("generator code bundle files must be SHA-256 digests")
        if canonical_sha256(
            {
                "files": dict(self.generator_code_bundle_files),
                "runtime": self.generator_code_bundle_runtime,
            }
        ) != self.generator_code_bundle_sha256:
            raise DiagnosticContractError("generator code bundle hash mismatch")

    @property
    def sample_content_sha256(self) -> str:
        return canonical_sha256(
            {
                "information_cutoff_date": self.information_cutoff_date.isoformat(),
                "market_snapshot_date": self.market_snapshot_date.isoformat(),
                "source_universe_id": self.source_universe_id,
                "selection_policy": SELECTION_POLICY,
                "representation": REPRESENTATION,
                "instrument_ids": list(self.instrument_ids),
                "industry_by_instrument": {
                    key: self.industry_by_instrument[key] for key in self.instrument_ids
                },
                "factor_ids": list(DIAGNOSTIC_FACTOR_IDS),
            }
        )

    @property
    def sample_payload_sha256(self) -> str:
        return canonical_sha256(self.to_dict(include_hash=False))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload = {
            "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
            "status": self.status,
            "information_cutoff_date": self.information_cutoff_date.isoformat(),
            "market_snapshot_date": self.market_snapshot_date.isoformat(),
            "source_universe_id": self.source_universe_id,
            "source_member_count": self.source_member_count,
            "source_membership_artifact_sha256": self.source_membership_artifact_sha256,
            "source_membership_payload_sha256": self.source_membership_payload_sha256,
            "source_membership_content_sha256": self.source_membership_content_sha256,
            "source_industry_artifact_sha256": self.source_industry_artifact_sha256,
            "source_industry_payload_sha256": self.source_industry_payload_sha256,
            "source_industry_content_sha256": self.source_industry_content_sha256,
            "generator_code_bundle_files": dict(self.generator_code_bundle_files),
            "generator_code_bundle_runtime": self.generator_code_bundle_runtime,
            "generator_code_bundle_sha256": self.generator_code_bundle_sha256,
            "selection_policy": SELECTION_POLICY,
            "representation": REPRESENTATION,
            "instrument_ids": list(self.instrument_ids),
            "industry_by_instrument": {
                key: self.industry_by_instrument[key] for key in self.instrument_ids
            },
            "factor_ids": list(DIAGNOSTIC_FACTOR_IDS),
            "sample_content_sha256": self.sample_content_sha256,
            "safety": {
                "paper_eligibility": self.paper_eligibility,
                "trade_eligibility": self.trade_eligibility,
                "real_money_list_allowed": False,
                "live": self.live,
            },
        }
        if include_hash:
            payload["sample_payload_sha256"] = self.sample_payload_sha256
        return payload


def _instrument_hash(instrument_id: str) -> str:
    # Instrument codes are ASCII, but this helper is also used to order
    # controlled industry identifiers.  Industry taxonomies may contain
    # non-ASCII labels, so hash their canonical UTF-8 bytes without changing
    # the result for existing ASCII identifiers.
    return sha256(instrument_id.encode("utf-8", errors="strict")).hexdigest()


def freeze_current_universe_sample(
    members: Sequence[CurrentUniverseMember],
    *,
    information_cutoff_date: date,
    market_snapshot_date: date,
    source_universe_id: str,
    source_membership_artifact_sha256: str,
    source_membership_payload_sha256: str,
    source_membership_content_sha256: str,
    source_industry_artifact_sha256: str,
    source_industry_payload_sha256: str,
    source_industry_content_sha256: str,
    generator_code_bundle_files: Mapping[str, str],
    generator_code_bundle_runtime: str,
    generator_code_bundle_sha256: str,
    sample_size: int = 60,
    historical_pit_proven: bool = False,
) -> DiagnosticSample:
    """Freeze a deterministic current-universe sample without reading outcomes."""

    if historical_pit_proven:
        raise DiagnosticContractError(
            "the fallback accepts current non-PIT membership only; use the formal path for PIT evidence"
        )
    if type(sample_size) is not int or sample_size != 60:
        raise DiagnosticContractError("diagnostic sample size is frozen at exactly 60")
    canonical = tuple(members)
    if any(not isinstance(item, CurrentUniverseMember) for item in canonical):
        raise DiagnosticContractError("members must contain CurrentUniverseMember objects")
    instrument_ids = [item.instrument_id for item in canonical]
    if len(set(instrument_ids)) != len(instrument_ids):
        raise DiagnosticContractError("current universe contains duplicate instruments")
    if len(canonical) != 800:
        raise DiagnosticContractError("fallback requires the complete 800-member current universe")
    if len(canonical) < sample_size:
        raise DiagnosticContractError("current universe is smaller than the frozen sample size")

    grouped: dict[str, list[CurrentUniverseMember]] = {}
    for member in canonical:
        grouped.setdefault(member.industry_id, []).append(member)
    if set(grouped) != CSI2021_LEVEL1_INDUSTRY_IDS:
        raise DiagnosticContractError("current universe must contain all 11 CSI 2021 level-1 industries")
    for group in grouped.values():
        group.sort(key=lambda item: (_instrument_hash(item.instrument_id), item.instrument_id))

    # Round-robin by within-industry hash rank gives every represented industry
    # one name before any industry receives a second, while remaining result blind.
    selected: list[CurrentUniverseMember] = []
    industry_order = sorted(grouped, key=lambda key: (_instrument_hash(key), key))
    rank = 0
    while len(selected) < sample_size:
        added = False
        for industry_id in industry_order:
            group = grouped[industry_id]
            if rank < len(group):
                selected.append(group[rank])
                added = True
                if len(selected) == sample_size:
                    break
        if not added:
            raise DiagnosticContractError("could not fill the frozen sample")
        rank += 1

    selected.sort(key=lambda item: (_instrument_hash(item.instrument_id), item.instrument_id))
    ids = tuple(item.instrument_id for item in selected)
    return DiagnosticSample(
        information_cutoff_date=information_cutoff_date,
        market_snapshot_date=market_snapshot_date,
        source_universe_id=_identifier(source_universe_id, "source_universe_id"),
        source_member_count=len(canonical),
        source_membership_artifact_sha256=source_membership_artifact_sha256,
        source_membership_payload_sha256=source_membership_payload_sha256,
        source_membership_content_sha256=source_membership_content_sha256,
        source_industry_artifact_sha256=source_industry_artifact_sha256,
        source_industry_payload_sha256=source_industry_payload_sha256,
        source_industry_content_sha256=source_industry_content_sha256,
        generator_code_bundle_files=dict(generator_code_bundle_files),
        generator_code_bundle_runtime=generator_code_bundle_runtime,
        generator_code_bundle_sha256=generator_code_bundle_sha256,
        instrument_ids=ids,
        industry_by_instrument={item.instrument_id: item.industry_id for item in selected},
    )


def _log_return(current: float, previous: float) -> float:
    return math.log(current / previous)


def compute_price_diagnostics(
    bars: Iterable[DiagnosticPriceBar],
    benchmark_bars: Iterable[DiagnosticPriceBar],
    *,
    allowed_instrument_ids: Sequence[str],
) -> tuple[DiagnosticFactorRow, ...]:
    """Compute the frozen six diagnostics on complete, aligned date histories."""

    allowed = tuple(_identifier(item, "allowed_instrument_ids item") for item in allowed_instrument_ids)
    if not allowed or len(set(allowed)) != len(allowed):
        raise DiagnosticContractError("allowed_instrument_ids must be unique and non-empty")
    by_instrument: dict[str, list[DiagnosticPriceBar]] = {item: [] for item in allowed}
    for bar in bars:
        if bar.instrument_id not in by_instrument:
            raise DiagnosticContractError("bars contain an instrument outside the frozen sample")
        by_instrument[bar.instrument_id].append(bar)
    benchmark = list(benchmark_bars)
    if not benchmark:
        raise DiagnosticContractError("benchmark bars are required")
    benchmark_ids = {item.instrument_id for item in benchmark}
    if len(benchmark_ids) != 1:
        raise DiagnosticContractError("benchmark bars must contain exactly one instrument")

    def normalized(series: list[DiagnosticPriceBar], label: str) -> list[DiagnosticPriceBar]:
        ordered = sorted(series, key=lambda item: item.trading_date)
        dates = [item.trading_date for item in ordered]
        if not ordered or dates != sorted(set(dates)):
            raise DiagnosticContractError(f"{label} bars must be non-empty with unique dates")
        return ordered

    benchmark = normalized(benchmark, "benchmark")
    benchmark_by_date = {item.trading_date: item for item in benchmark}
    rows: list[DiagnosticFactorRow] = []
    for instrument_id in allowed:
        series = normalized(by_instrument[instrument_id], instrument_id)
        dates = [item.trading_date for item in series]
        if dates != [item.trading_date for item in benchmark]:
            raise DiagnosticContractError(
                "every diagnostic stock must share the complete benchmark session calendar"
            )
        if len(series) < 121:
            raise DiagnosticContractError("each diagnostic instrument requires at least 121 sessions")
        for index in range(120, len(series)):
            current = series[index]
            values: dict[str, float] = {}
            for lookback in (20, 60, 120):
                previous = series[index - lookback]
                benchmark_current = benchmark_by_date[current.trading_date]
                benchmark_previous = benchmark_by_date.get(previous.trading_date)
                if benchmark_previous is None:
                    raise DiagnosticContractError("benchmark sessions are not aligned with stock sessions")
                values[f"RM{lookback}"] = _log_return(current.close, previous.close) - _log_return(
                    benchmark_current.close, benchmark_previous.close
                )
            path = series[index - 60 : index + 1]
            path_length = sum(abs(path[offset].close - path[offset - 1].close) for offset in range(1, 61))
            values["TREND_EFF60"] = 0.0 if path_length == 0 else (current.close - path[0].close) / path_length
            downside = [min(_log_return(path[offset].close, path[offset - 1].close), 0.0) for offset in range(1, 61)]
            values["DOWNSIDE_VOL60"] = stdev(downside)
            prior_high = max(item.high for item in series[index - 60 : index])
            values["BREAKOUT60"] = current.close / prior_high - 1.0
            rows.append(
                DiagnosticFactorRow(
                    instrument_id=instrument_id,
                    trading_date=current.trading_date,
                    values={factor_id: values[factor_id] for factor_id in DIAGNOSTIC_FACTOR_IDS},
                )
            )
    rows.sort(key=lambda item: (item.trading_date, item.instrument_id))
    return tuple(rows)


__all__ = [
    "DIAGNOSTIC_FACTOR_IDS",
    "DIAGNOSTIC_STATUS",
    "CurrentUniverseMember",
    "DiagnosticContractError",
    "DiagnosticFactorRow",
    "DiagnosticPriceBar",
    "DiagnosticSample",
    "compute_price_diagnostics",
    "freeze_current_universe_sample",
]
