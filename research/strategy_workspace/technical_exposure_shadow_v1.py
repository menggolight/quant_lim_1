"""Frozen, fail-closed market exposure heuristic for the Shadow MVP."""

from __future__ import annotations

import math
from statistics import stdev
from typing import Any, Mapping, Sequence


EXPOSURES = {"RISK_OFF": 0.0, "DEFENSIVE": 0.30, "NEUTRAL": 0.60, "RISK_ON": 1.0}
DEFAULT_POLICY = {
    "benchmark_trend_sessions": 60,
    "breadth_trend_sessions": 60,
    "realized_vol_sessions": 20,
    "annualization_sessions": 252,
    "risk_off": {
        "benchmark_trend_max": 0,
        "breadth_max": 0.40,
        "account_drawdown_max_loss": -0.10,
    },
    "defensive": {
        "breadth_trigger_below": 0.50,
        "realized_vol_max": 0.30,
        "account_drawdown_max_loss": -0.07,
    },
    "risk_on": {
        "benchmark_trend_min": 0,
        "breadth_min": 0.60,
        "realized_vol_max": 0.20,
        "account_drawdown_min": -0.03,
    },
    "gross_exposure": EXPOSURES,
}


def _configured_session_count(value: Any, field: str, *, minimum: int = 2) -> int:
    if isinstance(value, bool):
        raise ValueError(field)
    parsed = int(value)
    if parsed != value or parsed < minimum:
        raise ValueError(field)
    return parsed


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
        frozen = policy or DEFAULT_POLICY
        benchmark_trend_sessions = _configured_session_count(
            frozen["benchmark_trend_sessions"], "benchmark_trend_sessions"
        )
        breadth_trend_sessions = _configured_session_count(
            frozen["breadth_trend_sessions"], "breadth_trend_sessions"
        )
        realized_vol_sessions = _configured_session_count(
            frozen["realized_vol_sessions"], "realized_vol_sessions"
        )
        annualization_sessions = _configured_session_count(
            frozen["annualization_sessions"], "annualization_sessions", minimum=1
        )
        required_benchmark_rows = max(
            benchmark_trend_sessions + 1, realized_vol_sessions + 1
        )
        if (
            len(benchmark_rows) < required_benchmark_rows
            or not eligible_stock_rows
            or current_nav <= 0
            or peak_nav <= 0
        ):
            raise ValueError("incomplete_exposure_input")
        closes = [
            float(row["close"])
            for row in benchmark_rows[-required_benchmark_rows:]
        ]
        if any(not math.isfinite(value) or value <= 0 for value in closes):
            raise ValueError("invalid_benchmark_close")
        trend_closes = closes[-benchmark_trend_sessions:]
        benchmark_trend = closes[-1] / (
            sum(trend_closes) / benchmark_trend_sessions
        ) - 1
        volatility_closes = closes[-(realized_vol_sessions + 1):]
        log_returns = [
            math.log(volatility_closes[index] / volatility_closes[index - 1])
            for index in range(1, len(volatility_closes))
        ]
        realized_vol = stdev(log_returns) * math.sqrt(annualization_sessions)
        breadth_flags: list[bool] = []
        for rows in eligible_stock_rows:
            series = [float(row["close"]) for row in rows[-breadth_trend_sessions:]]
            if len(series) != breadth_trend_sessions or any(
                not math.isfinite(value) or value <= 0 for value in series
            ):
                continue
            breadth_flags.append(
                series[-1] > sum(series) / breadth_trend_sessions
            )
        if not breadth_flags:
            raise ValueError("breadth_unavailable")
        breadth = sum(breadth_flags) / len(breadth_flags)
        drawdown = current_nav / peak_nav - 1
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
