"""Frozen, fail-closed market exposure heuristic for the Shadow MVP."""

from __future__ import annotations

import math
from statistics import stdev
from typing import Any, Mapping, Sequence


EXPOSURES = {"RISK_OFF": 0.0, "DEFENSIVE": 0.30, "NEUTRAL": 0.60, "RISK_ON": 1.0}


def compute_technical_shadow_exposure(
    *,
    benchmark_rows: Sequence[Mapping[str, Any]],
    eligible_stock_rows: Sequence[Sequence[Mapping[str, Any]]],
    current_nav: float,
    peak_nav: float,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Use only benchmark trend, breadth, realized vol and account drawdown."""

    try:
        if len(benchmark_rows) < 61 or not eligible_stock_rows or current_nav <= 0 or peak_nav <= 0:
            raise ValueError("incomplete_exposure_input")
        closes = [float(row["close"]) for row in benchmark_rows[-61:]]
        if any(not math.isfinite(value) or value <= 0 for value in closes):
            raise ValueError("invalid_benchmark_close")
        benchmark_trend = closes[-1] / (sum(closes[-60:]) / 60) - 1
        log_returns = [math.log(closes[index] / closes[index - 1]) for index in range(len(closes) - 20, len(closes))]
        realized_vol = stdev(log_returns) * math.sqrt(252)
        breadth_flags: list[bool] = []
        for rows in eligible_stock_rows:
            series = [float(row["close"]) for row in rows[-60:]]
            if len(series) != 60 or any(not math.isfinite(value) or value <= 0 for value in series):
                continue
            breadth_flags.append(series[-1] > sum(series) / 60)
        if not breadth_flags:
            raise ValueError("breadth_unavailable")
        breadth = sum(breadth_flags) / len(breadth_flags)
        drawdown = current_nav / peak_nav - 1
        frozen = policy or {
            "risk_off": {"benchmark_trend_max": 0, "breadth_max": 0.40, "account_drawdown_max_loss": -0.10},
            "defensive": {"breadth_trigger_below": 0.50, "realized_vol_max": 0.30, "account_drawdown_max_loss": -0.07},
            "risk_on": {"benchmark_trend_min": 0, "breadth_min": 0.60, "realized_vol_max": 0.20, "account_drawdown_min": -0.03},
            "gross_exposure": EXPOSURES,
        }
        if {str(key): float(value) for key, value in frozen["gross_exposure"].items()} != EXPOSURES:
            raise ValueError("exposure_labels_or_weights_drifted")
        risk_off = frozen["risk_off"]
        defensive = frozen["defensive"]
        risk_on = frozen["risk_on"]
        if (
            benchmark_trend <= float(risk_off["benchmark_trend_max"])
            or breadth < float(risk_off["breadth_max"])
            or drawdown <= float(risk_off["account_drawdown_max_loss"])
        ):
            state = "RISK_OFF"
        elif (
            breadth < float(defensive["breadth_trigger_below"])
            or realized_vol > float(defensive["realized_vol_max"])
            or drawdown <= float(defensive["account_drawdown_max_loss"])
        ):
            state = "DEFENSIVE"
        elif (
            benchmark_trend > float(risk_on["benchmark_trend_min"])
            and breadth >= float(risk_on["breadth_min"])
            and realized_vol <= float(risk_on["realized_vol_max"])
            and drawdown > float(risk_on["account_drawdown_min"])
        ):
            state = "RISK_ON"
        else:
            state = "NEUTRAL"
        return {
            "market_state": state,
            "target_gross_exposure": float(frozen["gross_exposure"][state]),
            "benchmark_trend": benchmark_trend,
            "market_breadth": breadth,
            "realized_volatility": realized_vol,
            "account_drawdown": drawdown,
            "data_fail_closed": False,
            "reason_codes": [f"exposure_{state.lower()}"],
        }
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return {
            "market_state": "RISK_OFF",
            "target_gross_exposure": 0.0,
            "benchmark_trend": None,
            "market_breadth": None,
            "realized_volatility": None,
            "account_drawdown": None,
            "data_fail_closed": True,
            "reason_codes": ["exposure_data_fail_closed"],
        }


__all__ = ["EXPOSURES", "compute_technical_shadow_exposure"]
