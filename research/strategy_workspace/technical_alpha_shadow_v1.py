"""Frozen six-factor technical ranking for the non-admitted Shadow MVP."""

from __future__ import annotations

import math
from datetime import date, datetime
from statistics import pstdev, stdev
from typing import Any, Mapping, Sequence


FACTOR_IDS = (
    "RM20", "RM60", "RM120", "TREND_EFF60", "DOWNSIDE_VOL60", "BREAKOUT60"
)
DIRECTIONS = {
    "RM20": 1.0, "RM60": 1.0, "RM120": 1.0, "TREND_EFF60": 1.0,
    "DOWNSIDE_VOL60": -1.0, "BREAKOUT60": 1.0,
}


class TechnicalAlphaShadowError(ValueError):
    pass


def _day(row: Mapping[str, Any]) -> date:
    value = row.get("trading_date")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _finite_positive(row: Mapping[str, Any], field: str) -> float:
    value = float(row[field])
    if not math.isfinite(value) or value <= 0:
        raise ValueError(field)
    return value


def _quantile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _factor_values(
    rows: Sequence[Mapping[str, Any]],
    benchmark: Sequence[Mapping[str, Any]],
) -> dict[str, float]:
    closes = [_finite_positive(row, "close") for row in rows]
    highs = [_finite_positive(row, "high") for row in rows]
    benchmark_closes = [_finite_positive(row, "close") for row in benchmark]
    values: dict[str, float] = {}
    for lookback in (20, 60, 120):
        values[f"RM{lookback}"] = math.log(closes[-1] / closes[-1 - lookback]) - math.log(
            benchmark_closes[-1] / benchmark_closes[-1 - lookback]
        )
    path = closes[-61:]
    path_length = sum(abs(path[index] - path[index - 1]) for index in range(1, 61))
    values["TREND_EFF60"] = 0.0 if path_length == 0 else (path[-1] - path[0]) / path_length
    downside = [min(math.log(path[index] / path[index - 1]), 0.0) for index in range(1, 61)]
    values["DOWNSIDE_VOL60"] = stdev(downside)
    values["BREAKOUT60"] = closes[-1] / max(highs[-61:-1]) - 1.0
    if any(not math.isfinite(value) for value in values.values()):
        raise ValueError("non_finite_factor")
    return {factor: values[factor] for factor in FACTOR_IDS}


def rank_technical_alpha_shadow(
    *,
    decision_date: date,
    sessions: Sequence[date],
    instrument_ids: Sequence[str],
    stock_rows: Mapping[str, Sequence[Mapping[str, Any]]],
    benchmark_rows: Sequence[Mapping[str, Any]],
    winsor_lower_quantile: float = 0.01,
    winsor_upper_quantile: float = 0.99,
) -> list[dict[str, Any]]:
    """Return all frozen names; any future observation rejects the entire decision."""

    if len(instrument_ids) != 60 or len(set(instrument_ids)) != 60:
        raise TechnicalAlphaShadowError("frozen universe must contain exactly 60 unique instruments")
    if not 0 <= winsor_lower_quantile < winsor_upper_quantile <= 1:
        raise TechnicalAlphaShadowError("invalid winsor quantiles")
    required = tuple(sessions[-121:])
    if len(required) != 121 or required[-1] != decision_date:
        raise TechnicalAlphaShadowError("decision requires exactly 121 ending common sessions")
    for rows in list(stock_rows.values()) + [benchmark_rows]:
        if any(_day(row) > decision_date for row in rows):
            raise TechnicalAlphaShadowError("future_data_rejected")
    benchmark_by_day = {_day(row): row for row in benchmark_rows}
    if len(benchmark_by_day) != len(benchmark_rows) or any(day not in benchmark_by_day for day in required):
        raise TechnicalAlphaShadowError("benchmark_missing_required_session")
    benchmark = [benchmark_by_day[day] for day in required]

    output: list[dict[str, Any]] = []
    factor_rows: dict[str, dict[str, float]] = {}
    for instrument_id in instrument_ids:
        exclusions: list[str] = []
        rows = stock_rows.get(instrument_id, ())
        by_day = {_day(row): row for row in rows}
        if len(by_day) != len(rows):
            exclusions.append("duplicate_trading_date")
        missing = [day for day in required if day not in by_day]
        if missing:
            exclusions.append("missing_common_session")
        decision_row = by_day.get(decision_date)
        if decision_row is not None:
            if str(decision_row.get("trading_status")) != "traded":
                exclusions.append("suspended_on_decision_date")
            if bool(decision_row.get("is_st")):
                exclusions.append("st_on_decision_date")
        if not exclusions:
            try:
                factor_rows[instrument_id] = _factor_values(
                    [by_day[day] for day in required], benchmark
                )
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                exclusions.append("invalid_or_missing_ohlcv")
        output.append({
            "instrument_id": instrument_id,
            "factors": factor_rows.get(instrument_id),
            "z_scores": None,
            "composite_score": None,
            "rank": None,
            "percentile": None,
            "eligibility": False,
            "entry_eligible": False,
            "hold_eligible": False,
            "exclusion_codes": sorted(set(exclusions)),
        })

    z_by_instrument = {instrument_id: {} for instrument_id in factor_rows}
    for factor in FACTOR_IDS:
        values = [factor_rows[item][factor] for item in factor_rows]
        lower = _quantile(values, winsor_lower_quantile)
        upper = _quantile(values, winsor_upper_quantile)
        clipped = {item: min(max(factor_rows[item][factor], lower), upper) for item in factor_rows}
        mean = sum(clipped.values()) / len(clipped)
        sigma = pstdev(clipped.values())
        for item, value in clipped.items():
            z_by_instrument[item][factor] = 0.0 if sigma == 0 else (value - mean) / sigma
    scores = {
        item: sum(DIRECTIONS[factor] * z[factor] for factor in FACTOR_IDS)
        for item, z in z_by_instrument.items()
    }
    ranked_ids = sorted(scores, key=lambda item: (-scores[item], item))
    rank_by_id = {item: index + 1 for index, item in enumerate(ranked_ids)}
    denominator = max(len(ranked_ids) - 1, 1)
    by_id = {row["instrument_id"]: row for row in output}
    for item in ranked_ids:
        row = by_id[item]
        percentile = (len(ranked_ids) - rank_by_id[item]) / denominator
        score = scores[item]
        row.update({
            "z_scores": z_by_instrument[item],
            "composite_score": score,
            "rank": rank_by_id[item],
            "percentile": percentile,
            "eligibility": True,
            "entry_eligible": score > 0 and percentile >= 0.90,
            "hold_eligible": score > 0 and percentile >= 0.70,
        })
        if not row["entry_eligible"]:
            row["exclusion_codes"].append("below_entry_threshold")
        if not row["hold_eligible"]:
            row["exclusion_codes"].append("below_hold_threshold")
    return sorted(output, key=lambda row: (row["rank"] is None, row["rank"] or 10**9, row["instrument_id"]))


__all__ = ["FACTOR_IDS", "TechnicalAlphaShadowError", "rank_technical_alpha_shadow"]
