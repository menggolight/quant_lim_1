"""Diagnose the frozen Technical Shadow exposure policy on 120 real sessions."""

from __future__ import annotations

import argparse
import json
import math
import uuid
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

from operations.run_technical_shadow_mvp import (
    BaoStockTechnicalShadowSource,
    CapturedData,
    CHINA_TZ,
    DEFAULT_CONFIG,
    TechnicalShadowRunError,
    _load_config,
    validate_source_provenance,
)
from research.strategy_workspace.technical_alpha_shadow_v1 import (
    TechnicalAlphaShadowError,
    rank_technical_alpha_shadow,
)
from research.strategy_workspace.technical_exposure_shadow_v1 import (
    EXPOSURES,
    compute_technical_shadow_exposure,
)


DIAGNOSTIC_DAYS = 120
ALPHA_LOOKBACK_SESSIONS = 120
BASELINE_REPLAY_DAYS = 10
STATE_ORDER = ("RISK_OFF", "DEFENSIVE", "NEUTRAL", "RISK_ON")
DEFAULT_OUTPUT_ROOT = Path("data/tmp/technical-shadow-exposure-diagnostic")
PURPOSE = "exposure_policy_diagnostic_only"
SAFETY = {
    "strategy_signal": False,
    "alpha_evidence": False,
    "trade_recommendation": False,
    "paper_eligibility": False,
    "trade_eligibility": False,
    "real_money_list_allowed": False,
    "automatic_order_submission": False,
    "live_supported": False,
}
FIXED_IMPLEMENTATION_BUGS = (
    "configured_exposure_windows_were_ignored_before_this_diagnostic",
)


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return sha256(_canonical_bytes(value)).hexdigest()


def _write_new(
    path: Path,
    value: Any,
    *,
    root: Path,
    artifacts: dict[str, str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = value if isinstance(value, bytes) else _canonical_bytes(value)
    try:
        with path.open("xb") as stream:
            stream.write(raw)
    except FileExistsError as exc:
        raise TechnicalShadowRunError(f"create_only_path_exists:{path}") from exc
    artifacts[path.relative_to(root).as_posix()] = sha256(raw).hexdigest()


def _write_text_new(
    path: Path,
    value: str,
    *,
    root: Path,
    artifacts: dict[str, str],
) -> None:
    _write_new(
        path,
        value.replace("\r\n", "\n").encode("utf-8"),
        root=root,
        artifacts=artifacts,
    )


def _day(row: Mapping[str, Any]) -> date:
    return date.fromisoformat(str(row["trading_date"]))


def _quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("quantile requires observations")
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (
        ordered[upper] - ordered[lower]
    ) * (position - lower)


def _distribution(values: Sequence[float]) -> dict[str, Any]:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    if not finite:
        return {
            "count": 0,
            "min": None,
            "p10": None,
            "p25": None,
            "median": None,
            "p75": None,
            "p90": None,
            "max": None,
        }
    return {
        "count": len(finite),
        "min": min(finite),
        "p10": _quantile(finite, 0.10),
        "p25": _quantile(finite, 0.25),
        "median": _quantile(finite, 0.50),
        "p75": _quantile(finite, 0.75),
        "p90": _quantile(finite, 0.90),
        "max": max(finite),
    }


def _thresholds(policy: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "risk_off": {
            "benchmark_trend": {
                "operator": "<=",
                "value": float(policy["risk_off"]["benchmark_trend_max"]),
            },
            "market_breadth": {
                "operator": "<",
                "value": float(policy["risk_off"]["breadth_max"]),
            },
            "account_drawdown": {
                "operator": "<=",
                "value": float(policy["risk_off"]["account_drawdown_max_loss"]),
            },
        },
        "defensive": {
            "market_breadth": {
                "operator": "<",
                "value": float(policy["defensive"]["breadth_trigger_below"]),
            },
            "realized_volatility": {
                "operator": ">",
                "value": float(policy["defensive"]["realized_vol_max"]),
            },
            "account_drawdown": {
                "operator": "<=",
                "value": float(policy["defensive"]["account_drawdown_max_loss"]),
            },
        },
        "risk_on": {
            "benchmark_trend": {
                "operator": ">",
                "value": float(policy["risk_on"]["benchmark_trend_min"]),
            },
            "market_breadth": {
                "operator": ">=",
                "value": float(policy["risk_on"]["breadth_min"]),
            },
            "realized_volatility": {
                "operator": "<=",
                "value": float(policy["risk_on"]["realized_vol_max"]),
            },
            "account_drawdown": {
                "operator": ">",
                "value": float(policy["risk_on"]["account_drawdown_min"]),
            },
        },
        "market_drawdown": {
            "operator": None,
            "value": None,
            "used_by_policy": False,
        },
    }


def _evaluate_policy(
    *,
    benchmark_trend: float,
    market_breadth: float,
    realized_volatility: float,
    account_drawdown: float,
    policy: Mapping[str, Any],
) -> tuple[dict[str, dict[str, bool]], str, str]:
    risk_off = policy["risk_off"]
    defensive = policy["defensive"]
    risk_on = policy["risk_on"]
    conditions = {
        "risk_off": {
            "benchmark_trend": benchmark_trend
            <= float(risk_off["benchmark_trend_max"]),
            "market_breadth": market_breadth < float(risk_off["breadth_max"]),
            "account_drawdown": account_drawdown
            <= float(risk_off["account_drawdown_max_loss"]),
        },
        "defensive": {
            "market_breadth": market_breadth
            < float(defensive["breadth_trigger_below"]),
            "realized_volatility": realized_volatility
            > float(defensive["realized_vol_max"]),
            "account_drawdown": account_drawdown
            <= float(defensive["account_drawdown_max_loss"]),
        },
        "risk_on": {
            "benchmark_trend": benchmark_trend
            > float(risk_on["benchmark_trend_min"]),
            "market_breadth": market_breadth >= float(risk_on["breadth_min"]),
            "realized_volatility": realized_volatility
            <= float(risk_on["realized_vol_max"]),
            "account_drawdown": account_drawdown
            > float(risk_on["account_drawdown_min"]),
        },
    }
    if any(conditions["risk_off"].values()):
        return conditions, "risk_off", "RISK_OFF"
    if any(conditions["defensive"].values()):
        return conditions, "defensive", "DEFENSIVE"
    if all(conditions["risk_on"].values()):
        return conditions, "risk_on", "RISK_ON"
    return conditions, "neutral", "NEUTRAL"


def _market_drawdown(benchmark_rows: Sequence[Mapping[str, Any]]) -> float:
    closes = [float(row["close"]) for row in benchmark_rows]
    if not closes or any(not math.isfinite(value) or value <= 0 for value in closes):
        raise ValueError("market_drawdown_unavailable")
    return closes[-1] / max(closes) - 1


def _longest_streak(rows: Sequence[Mapping[str, Any]], state: str) -> int:
    longest = 0
    current = 0
    for row in rows:
        if row["final_state"] == state:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _structural_reachability(policy: Mapping[str, Any]) -> dict[str, Any]:
    trend_floor = float(policy["risk_off"]["benchmark_trend_max"])
    breadth_off = float(policy["risk_off"]["breadth_max"])
    breadth_defensive = float(policy["defensive"]["breadth_trigger_below"])
    breadth_on = float(policy["risk_on"]["breadth_min"])
    vol_on = float(policy["risk_on"]["realized_vol_max"])
    vol_defensive = float(policy["defensive"]["realized_vol_max"])
    witnesses = {
        "RISK_OFF": {
            "benchmark_trend": trend_floor - 0.01,
            "market_breadth": max(breadth_on, 0.80),
            "realized_volatility": max(vol_on / 2, 0.01),
            "account_drawdown": 0.0,
        },
        "DEFENSIVE": {
            "benchmark_trend": trend_floor + 0.01,
            "market_breadth": (breadth_off + breadth_defensive) / 2,
            "realized_volatility": max(vol_on / 2, 0.01),
            "account_drawdown": 0.0,
        },
        "NEUTRAL": {
            "benchmark_trend": trend_floor + 0.01,
            "market_breadth": (breadth_defensive + breadth_on) / 2,
            "realized_volatility": (vol_on + vol_defensive) / 2,
            "account_drawdown": 0.0,
        },
        "RISK_ON": {
            "benchmark_trend": trend_floor + 0.01,
            "market_breadth": min(max(breadth_on + 0.10, 0.70), 1.0),
            "realized_volatility": max(vol_on / 2, 0.01),
            "account_drawdown": 0.0,
        },
    }
    observed: dict[str, Mapping[str, Any]] = {}
    for expected, metrics in witnesses.items():
        _, _, actual = _evaluate_policy(policy=policy, **metrics)
        if actual == expected:
            observed[expected] = metrics
    return {
        "structurally_reachable_states": sorted(observed),
        "structurally_unreachable_states": sorted(set(EXPOSURES) - set(observed)),
        "constructive_witnesses": observed,
    }


def _threshold_positions(
    daily: Sequence[Mapping[str, Any]], thresholds: Mapping[str, Any]
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for rule in ("risk_off", "defensive", "risk_on"):
        for metric, specification in thresholds[rule].items():
            values = [
                float(row[metric])
                for row in daily
                if row.get(metric) is not None
            ]
            threshold = float(specification["value"])
            output[f"{rule}.{metric}"] = {
                "metric": metric,
                "operator": specification["operator"],
                "threshold": threshold,
                "empirical_cdf_less_than_or_equal": (
                    sum(value <= threshold for value in values) / len(values)
                    if values
                    else None
                ),
                "inside_observed_range": (
                    min(values) <= threshold <= max(values) if values else None
                ),
            }
    output["market_drawdown"] = {
        "metric": "market_drawdown",
        "operator": None,
        "threshold": None,
        "used_by_policy": False,
    }
    return output


def _implementation_checks(
    daily: Sequence[Mapping[str, Any]], policy: Mapping[str, Any]
) -> dict[str, Any]:
    def outcome(passed: bool, detail: str) -> dict[str, Any]:
        return {"passed": bool(passed), "detail": detail}

    threshold_values = [
        float(policy["risk_off"]["benchmark_trend_max"]),
        float(policy["risk_off"]["breadth_max"]),
        float(policy["risk_off"]["account_drawdown_max_loss"]),
        float(policy["defensive"]["breadth_trigger_below"]),
        float(policy["defensive"]["realized_vol_max"]),
        float(policy["defensive"]["account_drawdown_max_loss"]),
        float(policy["risk_on"]["benchmark_trend_min"]),
        float(policy["risk_on"]["breadth_min"]),
        float(policy["risk_on"]["realized_vol_max"]),
        float(policy["risk_on"]["account_drawdown_min"]),
    ]
    gross_mapping = {
        str(key): float(value)
        for key, value in policy["gross_exposure"].items()
    }
    breadth_values = [
        float(row["market_breadth"])
        for row in daily
        if row.get("market_breadth") is not None
    ]
    volatility_values = [
        float(row["realized_volatility"])
        for row in daily
        if row.get("realized_volatility") is not None
    ]
    account_drawdowns = [
        float(row["account_drawdown"])
        for row in daily
        if row.get("account_drawdown") is not None
    ]
    trend_values = [
        float(row["benchmark_trend"])
        for row in daily
        if row.get("benchmark_trend") is not None
    ]
    market_drawdowns = [
        float(row["market_drawdown"])
        for row in daily
        if row.get("market_drawdown") is not None
    ]
    reachability = _structural_reachability(policy)
    checks = {
        "thresholds_are_finite": outcome(
            all(math.isfinite(value) for value in threshold_values),
            "all configured comparison thresholds must be finite",
        ),
        "gross_exposure_mapping_is_frozen": outcome(
            gross_mapping == EXPOSURES,
            "RISK_OFF/DEFENSIVE/NEUTRAL/RISK_ON must map to 0/0.30/0.60/1.00",
        ),
        "breadth_threshold_direction_is_ordered": outcome(
            0
            <= float(policy["risk_off"]["breadth_max"])
            <= float(policy["defensive"]["breadth_trigger_below"])
            <= float(policy["risk_on"]["breadth_min"])
            <= 1,
            "risk-off breadth <= defensive breadth <= risk-on breadth in fraction units",
        ),
        "volatility_threshold_direction_is_ordered": outcome(
            0
            <= float(policy["risk_on"]["realized_vol_max"])
            <= float(policy["defensive"]["realized_vol_max"]),
            "annualized volatility is a non-negative fraction and risk-on cap <= defensive trigger",
        ),
        "drawdown_threshold_direction_is_ordered": outcome(
            -1
            <= float(policy["risk_off"]["account_drawdown_max_loss"])
            <= float(policy["defensive"]["account_drawdown_max_loss"])
            <= float(policy["risk_on"]["account_drawdown_min"])
            <= 0,
            "more negative account drawdown must trigger stricter states",
        ),
        "observed_breadth_units_are_fractional": outcome(
            all(0 <= value <= 1 for value in breadth_values),
            "observed breadth must be in [0,1]",
        ),
        "observed_volatility_units_are_non_negative": outcome(
            all(math.isfinite(value) and value >= 0 for value in volatility_values),
            "observed annualized volatility must be finite and non-negative",
        ),
        "observed_trend_units_are_finite_returns": outcome(
            all(math.isfinite(value) for value in trend_values),
            "benchmark trend must be a finite fractional return",
        ),
        "observed_drawdowns_are_fractional": outcome(
            all(-1 <= value <= 0 for value in account_drawdowns + market_drawdowns),
            "account and descriptive market drawdowns must be in [-1,0]",
        ),
        "rule_mapping_matches_production_implementation": outcome(
            all(bool(row["implementation_crosscheck_passed"]) for row in daily),
            "independent rule classification must match compute_technical_shadow_exposure",
        ),
        "all_four_states_are_structurally_reachable": outcome(
            set(reachability["structurally_reachable_states"]) == set(STATE_ORDER),
            "constructive witnesses must reach every frozen state",
        ),
        "stateless_hysteresis_contract_is_consistent": outcome(
            "hysteresis" not in policy
            and all(
                row.get("pending_state") is None
                and row.get("hysteresis_count") == 0
                and row.get("candidate_state") == row.get("final_state")
                for row in daily
            ),
            "frozen policy has no hysteresis and final state must equal same-day candidate",
        ),
    }
    errors = sorted(name for name, value in checks.items() if not value["passed"])
    return {"checks": checks, "failed_checks": errors, "all_passed": not errors}


def _window_cause(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    condition_counts: Counter[str] = Counter()
    for row in rows:
        for rule, values in row["condition_results"].items():
            for metric, matched in values.items():
                if matched:
                    condition_counts[f"{rule}.{metric}"] += 1
    return {
        "decision_date_range": (
            [rows[0]["decision_date"], rows[-1]["decision_date"]]
            if rows
            else []
        ),
        "day_count": len(rows),
        "all_risk_off": bool(rows)
        and all(row["final_state"] == "RISK_OFF" for row in rows),
        "matched_rule_counts": dict(
            sorted(Counter(row["matched_rule"] for row in rows).items())
        ),
        "condition_true_counts": dict(sorted(condition_counts.items())),
        "root_cause": (
            "benchmark_trend_non_positive_on_all_10_days"
            if len(rows) == BASELINE_REPLAY_DAYS
            and condition_counts["risk_off.benchmark_trend"]
            == BASELINE_REPLAY_DAYS
            else "see_daily_condition_results"
        ),
    }


def _summarize(
    daily: Sequence[Mapping[str, Any]],
    *,
    policy: Mapping[str, Any],
    thresholds: Mapping[str, Any],
) -> dict[str, Any]:
    if not daily:
        raise TechnicalShadowRunError("exposure_diagnostic_has_no_days")
    state_counts = Counter(str(row["final_state"]) for row in daily)
    matched_counts = Counter(str(row["matched_rule"]) for row in daily)
    condition_counts: Counter[str] = Counter()
    for row in daily:
        for rule, values in row["condition_results"].items():
            for metric, matched in values.items():
                if matched:
                    condition_counts[f"{rule}.{metric}"] += 1
    switch_count = sum(
        daily[index]["final_state"] != daily[index - 1]["final_state"]
        for index in range(1, len(daily))
    )
    distributions = {
        metric: _distribution(
            [float(row[metric]) for row in daily if row.get(metric) is not None]
        )
        for metric in (
            "benchmark_trend",
            "market_breadth",
            "realized_volatility",
            "market_drawdown",
            "account_drawdown",
        )
    }
    reachability = _structural_reachability(policy)
    unobserved = sorted(state for state in EXPOSURES if state_counts[state] == 0)
    nonzero_dates = [
        {
            "decision_date": row["decision_date"],
            "final_state": row["final_state"],
            "target_gross_exposure": row["target_gross_exposure"],
        }
        for row in daily
        if float(row["target_gross_exposure"]) > 0
    ]
    latest_ten = list(daily[-BASELINE_REPLAY_DAYS:])
    prior_replay_ten = list(
        daily[-(BASELINE_REPLAY_DAYS + 1):-1]
        if len(daily) >= BASELINE_REPLAY_DAYS + 1
        else daily[-BASELINE_REPLAY_DAYS:]
    )
    mismatches = [
        row["decision_date"]
        for row in daily
        if not row["implementation_crosscheck_passed"]
    ]
    implementation_checks = _implementation_checks(daily, policy)
    all_risk_off = state_counts["RISK_OFF"] == len(daily)
    return {
        "purpose": PURPOSE,
        "decision_date_range": [daily[0]["decision_date"], daily[-1]["decision_date"]],
        "decision_day_count": len(daily),
        "account_path": "flat_cash_counterfactual_for_market_policy_diagnosis",
        "account_drawdown_is_strategy_replay": False,
        "state_distribution_semantics": (
            "market_policy_with_account_drawdown_held_at_zero_not_natural_account_replay"
        ),
        "state_distribution": {
            state: {
                "days": state_counts[state],
                "proportion": state_counts[state] / len(daily),
            }
            for state in STATE_ORDER
        },
        "state_switch_count": switch_count,
        "longest_consecutive_risk_off": _longest_streak(daily, "RISK_OFF"),
        "longest_consecutive_risk_on": _longest_streak(daily, "RISK_ON"),
        "matched_rule_counts": dict(sorted(matched_counts.items())),
        "condition_true_counts": dict(sorted(condition_counts.items())),
        "input_distributions": distributions,
        "quantile_method": "linear_interpolation_at_(n_minus_1)_times_p",
        "threshold_positions": _threshold_positions(daily, thresholds),
        "nonzero_position_dates": nonzero_dates,
        "nonzero_position_date_semantics": (
            "flat_cash_counterfactual_policy_target_not_realized_account_position"
        ),
        "nonzero_target_dates_flat_cash_counterfactual": nonzero_dates,
        "natural_position_dates_not_inferred_by_this_diagnostic": True,
        "unobserved_states_in_window": unobserved,
        **reachability,
        "unreachable_state_found": bool(
            reachability["structurally_unreachable_states"]
        ),
        "hysteresis_enabled": False,
        "hysteresis_policy": "none",
        "implementation_bug_found": True,
        "implementation_bug_fixed": True,
        "fixed_implementation_bugs": list(FIXED_IMPLEMENTATION_BUGS),
        "fixed_bug_behavioral_impact_on_frozen_policy": False,
        "fixed_bug_is_risk_off_root_cause": False,
        "fixed_bug_note": (
            "old hard-coded windows equaled the current frozen 60/60/20/252 values; "
            "the repair restores configuration mapping without changing frozen outputs"
        ),
        "implementation_crosscheck_mismatch_dates": mismatches,
        "implementation_checks": implementation_checks["checks"],
        "implementation_checks_all_passed": implementation_checks["all_passed"],
        "unit_direction_or_mapping_errors": implementation_checks["failed_checks"],
        "market_drawdown_used_by_policy": False,
        "market_drawdown_basis": "diagnostic_capture_window_expanding_peak_to_decision_date",
        "all_120_days_risk_off": all_risk_off,
        "policy_usability_status": (
            "exposure_policy_unusable_for_business_mvp"
            if all_risk_off
            else "nonzero_exposure_observed_without_parameter_change"
        ),
        "latest_10_completed_sessions": _window_cause(latest_ten),
        "prior_10_day_d_plus_1_replay_decisions": _window_cause(
            prior_replay_ten
        ),
        "safety": SAFETY,
    }


def build_exposure_diagnostic(
    *,
    config: Mapping[str, Any],
    captured: CapturedData,
    decision_count: int = DIAGNOSTIC_DAYS,
    initial_cash: Decimal = Decimal("10000"),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    validate_source_provenance(
        provider_id=captured.provider_id,
        provider_kind=captured.provider_kind,
        synthetic=captured.synthetic,
    )
    required_count = ALPHA_LOOKBACK_SESSIONS + decision_count
    if len(captured.sessions) < required_count:
        raise TechnicalShadowRunError("exposure_diagnostic_sessions_insufficient")
    sessions = captured.sessions[-required_count:]
    if tuple(sorted(set(sessions))) != tuple(sessions):
        raise TechnicalShadowRunError(
            "exposure_diagnostic_sessions_not_strictly_increasing_unique"
        )
    decision_dates = sessions[ALPHA_LOOKBACK_SESSIONS:]
    if len(decision_dates) != decision_count:
        raise TechnicalShadowRunError("exposure_diagnostic_date_count_mismatch")
    policy = config["exposure"]
    thresholds = _thresholds(policy)
    instrument_ids = tuple(config["universe"]["instrument_ids"])
    previous_state: str | None = None
    daily: list[dict[str, Any]] = []
    flat_nav = float(initial_cash)
    session_set = set(sessions)

    for decision_date in decision_dates:
        history_sessions = tuple(day for day in sessions if day <= decision_date)
        stock_slices = {
            item: tuple(
                row
                for row in captured.stock_rows.get(item, ())
                if _day(row) in session_set and _day(row) <= decision_date
            )
            for item in instrument_ids
        }
        benchmark_slice = tuple(
            row
            for row in captured.benchmark_rows
            if _day(row) in session_set and _day(row) <= decision_date
        )
        ranking_reason_codes: list[str] = []
        try:
            ranking = rank_technical_alpha_shadow(
                decision_date=decision_date,
                sessions=history_sessions,
                instrument_ids=instrument_ids,
                stock_rows=stock_slices,
                benchmark_rows=benchmark_slice,
                winsor_lower_quantile=float(
                    config["alpha"]["winsor_lower_quantile"]
                ),
                winsor_upper_quantile=float(
                    config["alpha"]["winsor_upper_quantile"]
                ),
            )
        except (
            TechnicalAlphaShadowError,
            IndexError,
            KeyError,
            TypeError,
            ValueError,
            ZeroDivisionError,
        ):
            ranking = []
            ranking_reason_codes.append("ranking_data_fail_closed")
        eligible_ids = {
            str(row["instrument_id"])
            for row in ranking
            if bool(row["eligibility"])
        }
        exposure = compute_technical_shadow_exposure(
            benchmark_rows=benchmark_slice,
            eligible_stock_rows=[
                stock_slices[item] for item in instrument_ids if item in eligible_ids
            ],
            current_nav=flat_nav,
            peak_nav=flat_nav,
            policy=policy,
        )
        metrics = {
            "benchmark_trend": exposure["benchmark_trend"],
            "market_breadth": exposure["market_breadth"],
            "realized_volatility": exposure["realized_volatility"],
            "account_drawdown": exposure["account_drawdown"],
        }
        if exposure["data_fail_closed"]:
            condition_results = {
                rule: {metric: None for metric in thresholds[rule]}
                for rule in ("risk_off", "defensive", "risk_on")
            }
            matched_rule = "data_fail_closed"
            candidate_state = "RISK_OFF"
        else:
            condition_results, matched_rule, candidate_state = _evaluate_policy(
                policy=policy,
                **{key: float(value) for key, value in metrics.items()},
            )
        target = float(policy["gross_exposure"][candidate_state])
        crosscheck = (
            candidate_state == exposure["market_state"]
            and math.isclose(target, float(exposure["target_gross_exposure"]))
        )
        descriptive_reason_codes: list[str] = []
        try:
            market_drawdown = _market_drawdown(benchmark_slice)
        except (TypeError, ValueError):
            # This metric is descriptive only.  Its failure must not turn an
            # already fail-closed policy result into a missing terminal day.
            market_drawdown = None
            descriptive_reason_codes.append(
                "market_drawdown_unavailable_descriptive_only"
            )
        daily.append({
            "decision_date": decision_date.isoformat(),
            **metrics,
            "market_drawdown": market_drawdown,
            "market_drawdown_used_by_policy": False,
            "market_drawdown_basis": (
                "diagnostic_capture_window_expanding_peak_to_decision_date"
            ),
            "market_drawdown_window_start": (
                _day(benchmark_slice[0]).isoformat() if benchmark_slice else None
            ),
            "market_drawdown_observation_count": len(benchmark_slice),
            "thresholds": thresholds,
            "condition_results": condition_results,
            "matched_rule": matched_rule,
            "previous_state": previous_state,
            "candidate_state": candidate_state,
            "pending_state": None,
            "hysteresis_count": 0,
            "final_state": candidate_state,
            "target_gross_exposure": target,
            "reason_codes": (
                list(exposure["reason_codes"])
                + ranking_reason_codes
                + descriptive_reason_codes
            ),
            "data_fail_closed": bool(exposure["data_fail_closed"]),
            "eligible_stock_count": len(eligible_ids),
            "ranking_data_fail_closed": bool(ranking_reason_codes),
            "implementation_crosscheck_passed": crosscheck,
        })
        previous_state = candidate_state
    return daily, _summarize(daily, policy=policy, thresholds=thresholds)


def write_exposure_diagnostic(
    *,
    config: Mapping[str, Any],
    captured: CapturedData,
    daily: Sequence[Mapping[str, Any]],
    summary: Mapping[str, Any],
    output_root: Path,
    run_id: str | None = None,
) -> Path:
    run_id = run_id or (
        f"{datetime.now(CHINA_TZ).strftime('%Y%m%dT%H%M%S%z')}-"
        f"{uuid.uuid4().hex[:8]}"
    )
    run_root = output_root / run_id
    try:
        run_root.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise TechnicalShadowRunError(
            f"create_only_run_directory_exists:{run_root}"
        ) from exc
    artifacts: dict[str, str] = {}
    for key, receipt in sorted(captured.receipts.items()):
        _write_new(
            run_root / "data_receipts" / f"{key}.json",
            receipt,
            root=run_root,
            artifacts=artifacts,
        )
    for row in daily:
        _write_new(
            run_root / "daily" / f"{row['decision_date']}.exposure.json",
            row,
            root=run_root,
            artifacts=artifacts,
        )
    daily_jsonl = b"".join(_canonical_bytes(row) for row in daily)
    _write_new(
        run_root / "exposure_daily.jsonl",
        daily_jsonl,
        root=run_root,
        artifacts=artifacts,
    )
    _write_new(
        run_root / "exposure_summary.json",
        summary,
        root=run_root,
        artifacts=artifacts,
    )
    distribution = summary["state_distribution"]
    markdown = "\n".join([
        "# Technical Shadow Exposure 120日诊断",
        "",
        f"- 日期：`{summary['decision_date_range'][0]}` 至 `{summary['decision_date_range'][1]}`",
        f"- 状态分布：`{distribution}`",
        f"- 状态切换：`{summary['state_switch_count']}`",
        f"- 最长RISK_OFF / RISK_ON：`{summary['longest_consecutive_risk_off']} / {summary['longest_consecutive_risk_on']}`",
        f"- 自然非零目标日期数：`{len(summary['nonzero_position_dates'])}`",
        f"- 结构性不可达状态：`{summary['structurally_unreachable_states']}`",
        f"- 策略可用性：`{summary['policy_usability_status']}`",
        "- 迟滞：`disabled`；market_drawdown仅作描述，不参与冻结规则。",
        "- 本诊断不构成策略信号、Alpha证据、交易建议、Paper或交易准入。",
        "",
    ])
    _write_text_new(
        run_root / "exposure_summary.md",
        markdown,
        root=run_root,
        artifacts=artifacts,
    )
    manifest = {
        "schema_version": "technical-shadow-exposure-diagnostic-manifest.v1",
        "purpose": PURPOSE,
        "strategy_id": config["strategy_id"],
        "created_at": datetime.now(CHINA_TZ).isoformat(),
        "config_sha256": _digest(config),
        "provider": {
            "provider_id": captured.provider_id,
            "provider_kind": captured.provider_kind,
            "adapter_version": captured.adapter_version,
            "synthetic": captured.synthetic,
        },
        "decision_day_count": len(daily),
        "decision_date_range": summary["decision_date_range"],
        "source_session_count": len(captured.sessions),
        "source_session_range": [
            captured.sessions[0].isoformat(),
            captured.sessions[-1].isoformat(),
        ],
        "source_cutoff_date": captured.sessions[-1].isoformat(),
        "hysteresis_enabled": False,
        "condition_results_nullable_only_on_data_fail_closed": True,
        "market_drawdown_used_by_policy": False,
        "market_drawdown_basis": (
            "diagnostic_capture_window_expanding_peak_to_decision_date"
        ),
        "account_path": "flat_cash_counterfactual_for_market_policy_diagnosis",
        "artifacts": dict(sorted(artifacts.items())),
        "summary_sha256": _digest(summary),
        "safety": SAFETY,
    }
    _write_new(
        run_root / "run_manifest.json",
        manifest,
        root=run_root,
        artifacts=artifacts,
    )
    return run_root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = _load_config(args.config)
    source = BaoStockTechnicalShadowSource()
    captured = source.capture(
        instrument_ids=config["universe"]["instrument_ids"],
        benchmark_id=config["data"]["benchmark_id"],
        recent_completed_sessions=DIAGNOSTIC_DAYS,
        lookback_days=int(config["data"]["calendar_lookback_days"]),
        now=datetime.now(CHINA_TZ),
    )
    daily, summary = build_exposure_diagnostic(config=config, captured=captured)
    run_root = write_exposure_diagnostic(
        config=config,
        captured=captured,
        daily=daily,
        summary=summary,
        output_root=args.output_root,
    )
    print(json.dumps(
        {
            "output_directory": str(run_root.resolve()),
            "summary": summary,
        },
        ensure_ascii=False,
        sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DIAGNOSTIC_DAYS",
    "PURPOSE",
    "SAFETY",
    "build_exposure_diagnostic",
    "write_exposure_diagnostic",
]
