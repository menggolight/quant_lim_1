#!/usr/bin/env python3
"""Load point-in-time portfolio snapshots for research decisions.

The human-readable Obsidian page is deliberately not used as model input. This
module reads immutable JSON snapshots, checks their accounting, and converts
execution buckets into the four buckets understood by the B3 action scorer.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any


SNAPSHOT_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:\.v(\d+))?\.json$")
MODEL_BUCKETS = ("qd", "quant", "defense", "a_share_alpha")
CHINA_TZ = timezone(timedelta(hours=8))
RECONCILIATION_TOLERANCE = 0.02


@dataclass(frozen=True)
class PortfolioContext:
    path: Path
    as_of: str
    weights: dict[str, float]
    validation_issues: tuple[str, ...]
    risk_flags: tuple[str, ...]


def load_snapshot(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Portfolio snapshot must be a JSON object: {path}")
    return payload


def _parse_timestamp(value: str | date | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.max, tzinfo=CHINA_TZ)
    else:
        text_value = str(value).strip()
        if not text_value:
            raise ValueError("Timestamp must not be empty")
        if "T" not in text_value:
            parsed = datetime.combine(date.fromisoformat(text_value), time.max, tzinfo=CHINA_TZ)
        else:
            parsed = datetime.fromisoformat(text_value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CHINA_TZ)
    return parsed


def _snapshot_timestamp(path: Path, payload: dict[str, Any]) -> datetime:
    as_of = str(payload.get("as_of") or "").strip()
    if as_of:
        return _parse_timestamp(as_of)
    match = SNAPSHOT_NAME.match(path.name)
    if not match:
        raise ValueError(f"Unsupported portfolio snapshot name: {path.name}")
    return _parse_timestamp(match.group(1))


def _snapshot_version(path: Path) -> int:
    match = SNAPSHOT_NAME.match(path.name)
    if not match:
        return 0
    return int(match.group(2) or 1)


def find_latest_snapshot(
    workspace_root: Path,
    decision_time: str | date | datetime | None = None,
) -> Path | None:
    """Return the newest immutable snapshot legally visible at decision_time."""

    portfolio_dir = workspace_root / "data" / "portfolio"
    if not portfolio_dir.exists():
        return None
    cutoff = _parse_timestamp(decision_time) if decision_time is not None else None
    eligible: list[tuple[datetime, int, Path]] = []
    for path in portfolio_dir.glob("*.json"):
        if not SNAPSHOT_NAME.match(path.name):
            continue
        payload = load_snapshot(path)
        snapshot_time = _snapshot_timestamp(path, payload)
        if cutoff is None or snapshot_time <= cutoff:
            eligible.append((snapshot_time, _snapshot_version(path), path))
    if not eligible:
        return None
    eligible.sort(key=lambda item: (item[0], item[1], item[2].name))
    return eligible[-1][2]


def _bucket_values(snapshot: dict[str, Any]) -> dict[str, float]:
    summary = snapshot.get("portfolio_summary") or {}
    raw_values = summary.get("bucket_market_values") or {}
    if raw_values:
        return {str(key): float(value or 0.0) for key, value in raw_values.items()}

    values = {bucket: 0.0 for bucket in MODEL_BUCKETS}
    for position in snapshot.get("positions") or []:
        if not isinstance(position, dict):
            continue
        bucket = str(position.get("asset_bucket") or "")
        if bucket in values:
            values[bucket] += float(position.get("market_value") or 0.0)
    values["cash"] = float(snapshot.get("cash") or 0.0)
    return values


def weights_from_snapshot(snapshot: dict[str, Any]) -> dict[str, float]:
    values = _bucket_values(snapshot)
    modeled_values = {
        "qd": float(values.get("qd", 0.0)),
        "quant": float(values.get("quant", 0.0)),
        "defense": float(values.get("defense", 0.0)) + float(values.get("cash", 0.0)),
        "a_share_alpha": float(values.get("a_share_alpha", 0.0)),
    }
    total = sum(modeled_values.values())
    if total <= 0:
        raise ValueError("Portfolio snapshot has no positive modeled assets")
    return {bucket: value / total * 100.0 for bucket, value in modeled_values.items()}


def validate_snapshot(snapshot: dict[str, Any]) -> list[str]:
    """Return accounting issues instead of silently repairing source data."""

    issues: list[str] = []
    summary = snapshot.get("portfolio_summary") or {}
    declared_total = summary.get("known_total_assets")
    bucket_values = _bucket_values(snapshot)
    bucket_total = sum(bucket_values.values())

    if declared_total is not None:
        declared = float(declared_total)
        if abs(bucket_total - declared) > RECONCILIATION_TOLERANCE:
            issues.append(f"模块金额 {bucket_total:.2f} 与总资产 {declared:.2f} 不一致")

        positions_total = sum(
            float(position.get("market_value") or 0.0)
            for position in snapshot.get("positions") or []
            if isinstance(position, dict)
        )
        positions_plus_cash = positions_total + float(snapshot.get("cash") or 0.0)
        if snapshot.get("positions") and abs(positions_plus_cash - declared) > RECONCILIATION_TOLERANCE:
            issues.append(f"持仓加现金 {positions_plus_cash:.2f} 与总资产 {declared:.2f} 不一致")
    return issues


def risk_flags_from_snapshot(snapshot: dict[str, Any], weights: dict[str, float]) -> list[str]:
    flags: list[str] = []
    summary = snapshot.get("portfolio_summary") or {}
    total = float(summary.get("known_total_assets") or sum(_bucket_values(snapshot).values()))
    if total > 0:
        for position in snapshot.get("positions") or []:
            if not isinstance(position, dict) or position.get("instrument_type") != "equity":
                continue
            market_value = float(position.get("market_value") or 0.0)
            weight = market_value / total * 100.0
            if weight > 15.0:
                name = str(position.get("name") or position.get("instrument_id") or "未命名股票")
                flags.append(f"单只股票 {name} 占 {weight:.2f}%，超过 15% 研究上限")
    if weights["defense"] < 5.0:
        flags.append(f"防守与现金合计 {weights['defense']:.2f}%，低于 5% 下限")
    return flags


def load_portfolio_context(path: Path) -> PortfolioContext:
    snapshot = load_snapshot(path)
    weights = weights_from_snapshot(snapshot)
    return PortfolioContext(
        path=path,
        as_of=str(snapshot.get("as_of") or ""),
        weights=weights,
        validation_issues=tuple(validate_snapshot(snapshot)),
        risk_flags=tuple(risk_flags_from_snapshot(snapshot, weights)),
    )
