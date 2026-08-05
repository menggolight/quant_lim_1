"""Point-in-time evaluation primitives for broker research claims.

The functions in this module are deliberately deterministic and side-effect
free.  They accept either dataclass-like objects or mappings so that cached
JSON records and the typed models can be evaluated by the same code.

The most important invariant is *fail closed*: a report, price or truth value
that was not available by ``as_of`` cannot create a mature outcome.
"""

from __future__ import annotations

import inspect
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

try:  # models may be generated alongside this module during installation.
    from .models import ClaimOutcome
except (ImportError, AttributeError):  # pragma: no cover - compatibility path
    ClaimOutcome = None  # type: ignore[assignment,misc]


CHINA_TZ = timezone(timedelta(hours=8))
DEFAULT_HORIZONS = (20, 60, 120, 250)
DEFAULT_RATING_THRESHOLDS = {
    horizon: 0.05 * math.sqrt(horizon / 250.0) for horizon in DEFAULT_HORIZONS
}


class EvaluationError(ValueError):
    """Base class for invalid or unverifiable evaluation input."""


class FutureDataError(EvaluationError):
    """Raised when a caller attempts to use information after ``as_of``."""


def _get(record: Any, *names: str, default: Any = None) -> Any:
    if record is None:
        return default
    for name in names:
        if isinstance(record, Mapping) and name in record:
            value = record[name]
        else:
            value = getattr(record, name, None)
        if value is not None:
            return value
    return default


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, ArithmeticError):
        return None
    return result if math.isfinite(result) else None


def _date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        raise EvaluationError("date must not be empty")
    return date.fromisoformat(text[:10])


def _datetime(value: Any, *, date_at: time = time.max) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, date_at)
    else:
        text_value = str(value or "").strip()
        if not text_value:
            raise EvaluationError("timestamp must not be empty")
        if "T" not in text_value and " " not in text_value:
            result = datetime.combine(date.fromisoformat(text_value[:10]), date_at)
        else:
            result = datetime.fromisoformat(text_value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=CHINA_TZ)
    return result


def _date_only(value: Any) -> bool:
    if isinstance(value, datetime):
        return False
    if isinstance(value, date):
        return True
    text_value = str(value or "").strip()
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", text_value))


def _trade_date(item: Any) -> date:
    return _date(_get(item, "trade_date", "date", default=item))


def _available_at(item: Any, *, fallback_date: date | None = None) -> datetime | None:
    value = _get(item, "available_at", "truth_available_at", "evaluated_at")
    if value is not None:
        return _datetime(value)
    if fallback_date is not None:
        return datetime.combine(fallback_date, time.max, tzinfo=CHINA_TZ)
    return None


def _construct(model: Any, payload: dict[str, Any]) -> Any:
    """Construct a model while tolerating older/newer dataclass signatures."""

    if model is None:
        return payload
    try:
        parameters = inspect.signature(model).parameters
        filtered = {key: value for key, value in payload.items() if key in parameters}
        return model(**filtered)
    except (TypeError, ValueError):
        return payload


def _outcome(**payload: Any) -> Any:
    return _construct(ClaimOutcome, payload)


def _as_of(value: Any) -> datetime:
    if value is None:
        raise EvaluationError("as_of is required for point-in-time evaluation")
    return _datetime(value)


def _market_open(value: str | time) -> time:
    if isinstance(value, time):
        return value
    hour, minute = (int(part) for part in value.split(":", 1))
    return time(hour, minute)


def resolve_report_t0(
    report_or_time: Any,
    trading_days: Iterable[Any],
    market_open: str | time = "09:30",
) -> datetime:
    """Resolve the first executable daily-bar entry time.

    A date-only report is always assigned to the *next* trading-day open.  An
    explicitly timed report may use a same-day open only when it was already
    available no later than that open.  This distinction prevents the common
    look-ahead error of treating an undated intraday publication as pre-open.
    """

    days = sorted({_trade_date(item) for item in trading_days})
    if not days:
        raise EvaluationError("trading_days must not be empty")

    quality = str(_get(report_or_time, "timestamp_quality", default="") or "").lower()
    published = _get(report_or_time, "published_at", "report_date", "date")
    available = _get(report_or_time, "available_at")
    raw = available if available is not None else published
    if raw is None and isinstance(report_or_time, (str, date, datetime)):
        raw = report_or_time
    if raw is None:
        raise EvaluationError("report publication/availability time is missing")

    is_date_level = quality in {"date", "date_only", "day"}
    if not quality:
        is_date_level = _date_only(raw)
    reference_date = _date(published if is_date_level and published is not None else raw)
    open_time = _market_open(market_open)

    if is_date_level:
        candidates = [day for day in days if day > reference_date]
    else:
        available_time = _datetime(raw, date_at=open_time)
        candidates = [
            day
            for day in days
            if datetime.combine(day, open_time, tzinfo=CHINA_TZ) >= available_time
        ]
    if not candidates:
        raise EvaluationError("no executable trading day after report availability")
    return datetime.combine(candidates[0], open_time, tzinfo=CHINA_TZ)


def _adjusted_price(bar: Any, field: str) -> float | None:
    adjusted = _float(_get(bar, f"adjusted_{field}", f"adj_{field}"))
    if adjusted is not None:
        return adjusted
    raw = _float(_get(bar, field))
    if raw is None:
        return None
    factor = _float(_get(bar, "adjustment_factor", "adj_factor"))
    return raw * factor if factor is not None else raw


def _price_basis(bar: Any, field: str) -> str:
    if _float(_get(bar, f"adjusted_{field}", f"adj_{field}")) is not None:
        return "adjusted"
    if _float(_get(bar, "adjustment_factor", "adj_factor")) is not None:
        return "adjusted"
    return "raw"


def compound_excess_return(market_return: float, benchmark_return: float) -> float:
    """Geometrically compound relative performance, not simple subtraction."""

    if benchmark_return <= -1.0:
        raise EvaluationError("benchmark return must be greater than -100%")
    return (1.0 + market_return) / (1.0 + benchmark_return) - 1.0


def compute_forward_returns(
    report_or_time: Any,
    bars: Iterable[Any],
    benchmark_bars: Iterable[Any] | None = None,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    as_of: Any | None = None,
) -> dict[int, dict[str, Any]]:
    """Compute open-to-close forward and compound-excess returns.

    Horizon ``h`` uses the close of the ``h``-th trading session beginning at
    t0 (therefore list offset ``h - 1``).  Missing endpoints or benchmark bars
    produce an explicit immature record rather than a partial return.
    """

    ordered = sorted(list(bars), key=_trade_date)
    materialized_benchmark = list(benchmark_bars or [])
    if not ordered:
        return {
            int(h): {"horizon_days": int(h), "mature": False, "exclusion_reason": "missing_market_bars"}
            for h in horizons
        }
    evaluation_time = _datetime(as_of) if as_of is not None else None
    if evaluation_time is not None:
        for label, sequence in (
            ("market", ordered),
            ("benchmark", materialized_benchmark),
        ):
            for bar in sequence:
                fetched_raw = _get(bar, "fetched_at")
                if fetched_raw is None:
                    raise FutureDataError(
                        f"{label} bar lacks fetched_at provenance"
                    )
                if _datetime(fetched_raw) > evaluation_time:
                    raise FutureDataError(
                        f"{label} bar was fetched after as_of"
                    )
    executable_calendar = [bar for bar in ordered if not bool(_get(bar, "suspended", default=False))]
    if not executable_calendar:
        return {
            int(h): {"horizon_days": int(h), "mature": False, "exclusion_reason": "no_executable_market_bars"}
            for h in horizons
        }
    t0 = resolve_report_t0(report_or_time, executable_calendar)
    if evaluation_time is not None and t0 > evaluation_time:
        raise FutureDataError("report t0 is after as_of")

    start_index = next((index for index, bar in enumerate(ordered) if _trade_date(bar) == t0.date()), None)
    if start_index is None:  # defensive; resolve_report_t0 used the same calendar
        raise EvaluationError("t0 is absent from instrument bars")
    entry = ordered[start_index]
    entry_price = _adjusted_price(entry, "open")
    entry_basis = _price_basis(entry, "open")
    if entry_price is None or entry_price <= 0:
        raise EvaluationError("entry open price must be positive")

    benchmark_by_day = {
        _trade_date(bar): bar for bar in sorted(materialized_benchmark, key=_trade_date)
    }
    result: dict[int, dict[str, Any]] = {}
    for raw_horizon in horizons:
        horizon = int(raw_horizon)
        if horizon <= 0:
            raise EvaluationError("horizons must be positive")
        end_index = start_index + horizon - 1
        base = {
            "horizon_days": horizon,
            "t0": t0,
            "start_date": t0.date(),
            "mature": False,
            "exclusion_reason": "",
        }
        if end_index >= len(ordered):
            base["exclusion_reason"] = "unmatured_horizon"
            result[horizon] = base
            continue
        end_bar = ordered[end_index]
        end_date = _trade_date(end_bar)
        end_available = _available_at(end_bar, fallback_date=end_date)
        if evaluation_time is not None and end_available is not None and end_available > evaluation_time:
            base.update(end_date=end_date, exclusion_reason="future_market_data")
            result[horizon] = base
            continue
        exit_price = _adjusted_price(end_bar, "close")
        if exit_price is None or exit_price <= 0:
            base.update(end_date=end_date, exclusion_reason="invalid_exit_price")
            result[horizon] = base
            continue
        if _price_basis(end_bar, "close") != entry_basis:
            base.update(end_date=end_date, exclusion_reason="inconsistent_adjustment_basis")
            result[horizon] = base
            continue
        market_return = exit_price / entry_price - 1.0
        benchmark_return: float | None = None
        excess_return: float | None = None
        if benchmark_bars is not None:
            benchmark_start = benchmark_by_day.get(t0.date())
            benchmark_end = benchmark_by_day.get(end_date)
            if benchmark_start is None or benchmark_end is None:
                base.update(end_date=end_date, exclusion_reason="missing_aligned_benchmark")
                result[horizon] = base
                continue
            benchmark_start_price = _adjusted_price(benchmark_start, "open")
            benchmark_end_price = _adjusted_price(benchmark_end, "close")
            if (
                benchmark_start_price is None
                or benchmark_start_price <= 0
                or benchmark_end_price is None
                or benchmark_end_price <= 0
            ):
                base.update(end_date=end_date, exclusion_reason="invalid_benchmark_price")
                result[horizon] = base
                continue
            if _price_basis(benchmark_start, "open") != _price_basis(benchmark_end, "close"):
                base.update(end_date=end_date, exclusion_reason="inconsistent_benchmark_adjustment_basis")
                result[horizon] = base
                continue
            benchmark_available = _available_at(benchmark_end, fallback_date=end_date)
            if evaluation_time is not None and benchmark_available and benchmark_available > evaluation_time:
                base.update(end_date=end_date, exclusion_reason="future_benchmark_data")
                result[horizon] = base
                continue
            benchmark_return = benchmark_end_price / benchmark_start_price - 1.0
            excess_return = compound_excess_return(market_return, benchmark_return)

        base.update(
            end_date=end_date,
            entry_price=entry_price,
            exit_price=exit_price,
            market_return=market_return,
            benchmark_return=benchmark_return,
            excess_return=excess_return,
            market_truth_source=str(_get(end_bar, "source", default="") or "market_bars"),
            mature=True,
            exclusion_reason="",
        )
        result[horizon] = base
    return result


def rating_economic_threshold(
    horizon_days: int,
    thresholds: Mapping[int, float] | None = None,
) -> float:
    """Return a monotone economic, rather than statistical-zero, threshold."""

    horizon = int(horizon_days)
    if horizon <= 0:
        raise EvaluationError("horizon_days must be positive")
    if thresholds is None:
        return 0.05 * math.sqrt(horizon / 250.0)
    configured = dict(DEFAULT_RATING_THRESHOLDS)
    configured.update({int(key): float(value) for key, value in thresholds.items()})
    if horizon in configured:
        return configured[horizon]
    points = sorted(configured)
    if horizon <= points[0]:
        return configured[points[0]] * math.sqrt(horizon / points[0])
    if horizon >= points[-1]:
        return configured[points[-1]] * math.sqrt(horizon / points[-1])
    upper = next(point for point in points if point > horizon)
    lower = points[points.index(upper) - 1]
    weight = (horizon - lower) / (upper - lower)
    return configured[lower] + weight * (configured[upper] - configured[lower])


def evaluate_rating(
    direction: int | float,
    excess_return: float,
    horizon_days: int,
    thresholds: Mapping[int, float] | None = None,
) -> dict[str, Any]:
    direction_value = 1 if float(direction) > 0 else -1 if float(direction) < 0 else 0
    realized = float(excess_return)
    threshold = rating_economic_threshold(horizon_days, thresholds)
    if direction_value > 0:
        margin = realized - threshold
    elif direction_value < 0:
        margin = -realized - threshold
    else:
        margin = threshold - abs(realized)
    return {
        "direction": direction_value,
        "threshold": threshold,
        "directional_margin": margin,
        "hit": margin >= 0.0,
        "error": max(0.0, -margin),
    }


def _interval(value_min: Any, value_max: Any) -> tuple[float, float] | None:
    low = _float(value_min)
    high = _float(value_max)
    if low is None and high is None:
        return None
    low = high if low is None else low
    high = low if high is None else high
    assert low is not None and high is not None
    return (min(low, high), max(low, high))


def target_price_metrics(
    value_min: Any,
    value_max: Any = None,
    prices: Iterable[Any] | None = None,
    *,
    starting_price: Any = None,
    realized_price: Any = None,
    highs: Iterable[Any] | None = None,
    lows: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """Evaluate a target interval using interval ALE and path touch status."""

    target = _interval(value_min, value_max)
    if target is None or target[0] <= 0:
        raise EvaluationError("positive target price or interval is required")
    price_items = list(prices or [])
    observed_highs = [
        value
        for value in (_float(_get(item, "high", default=item)) for item in price_items)
        if value is not None
    ]
    observed_lows = [
        value
        for value in (_float(_get(item, "low", default=item)) for item in price_items)
        if value is not None
    ]
    observed_highs.extend(value for value in (_float(item) for item in (highs or [])) if value is not None)
    observed_lows.extend(value for value in (_float(item) for item in (lows or [])) if value is not None)
    if realized_price is None and price_items:
        realized_price = _get(price_items[-1], "close", default=price_items[-1])
    realized = _float(realized_price)
    start = _float(starting_price)
    if start is None and price_items:
        start = _float(_get(price_items[0], "open", "close", default=price_items[0]))

    low, high = target
    touched: bool | None = None
    if observed_highs or observed_lows:
        max_high = max(observed_highs) if observed_highs else None
        min_low = min(observed_lows) if observed_lows else None
        if start is not None and start < low:
            touched = max_high is not None and max_high >= low
        elif start is not None and start > high:
            touched = min_low is not None and min_low <= high
        else:
            touched = bool(start is not None and low <= start <= high)
            if not touched and max_high is not None and min_low is not None:
                touched = max_high >= low and min_low <= high

    ale_interval: float | None = None
    ale_midpoint: float | None = None
    if realized is not None and realized > 0:
        boundary = low if realized < low else high if realized > high else realized
        ale_interval = abs(math.log(realized / boundary))
        midpoint = math.sqrt(low * high)
        ale_midpoint = abs(math.log(realized / midpoint))
    return {
        "target_min": low,
        "target_max": high,
        "starting_price": start,
        "realized_price": realized,
        "ale": ale_interval,
        "ale_interval": ale_interval,
        "ale_midpoint": ale_midpoint,
        "touched": touched,
        "hit": touched,
    }


def smape(forecast: Any, actual: Any) -> float | None:
    """Symmetric MAPE in [0, 2]; return None when values are unavailable."""

    forecast_value = _float(forecast)
    actual_value = _float(actual)
    if forecast_value is None or actual_value is None:
        return None
    denominator = abs(forecast_value) + abs(actual_value)
    return 0.0 if denominator == 0.0 else 2.0 * abs(forecast_value - actual_value) / denominator


def wape(forecasts: Iterable[Any], actuals: Iterable[Any]) -> float | None:
    """Weighted absolute percentage error; undefined for zero actual mass."""

    pairs: list[tuple[float, float]] = []
    for raw_forecast, raw_actual in zip(forecasts, actuals):
        forecast_value = _float(raw_forecast)
        actual_value = _float(raw_actual)
        if forecast_value is not None and actual_value is not None:
            pairs.append((forecast_value, actual_value))
    if not pairs:
        return None
    denominator = sum(abs(actual) for _, actual in pairs)
    if denominator == 0.0:
        return None
    return sum(abs(forecast - actual) for forecast, actual in pairs) / denominator


def _normalized_title(value: Any) -> str:
    text = re.sub(r"\d{4}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?", "", str(value or "").lower())
    text = re.sub(r"(?:周报|日报|月报|点评|更新|维持|第\s*\d+\s*期)", "", text)
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def _analyst_key(report: Any) -> str:
    value = _get(report, "analyst", "analysts", default="")
    if isinstance(value, (list, tuple, set)):
        return "|".join(sorted(str(item).strip() for item in value if str(item).strip()))
    return str(value or "").strip()


def _report_time(report: Any) -> datetime:
    return _datetime(_get(report, "available_at", "published_at", "report_date"))


def deduplicate_reports(
    reports: Iterable[Any],
    episode_days: int = 60,
    *,
    keep: str = "first",
) -> list[Any]:
    """Keep the first report in an unchanged source/subject stance episode."""

    if episode_days < 0:
        raise EvaluationError("episode_days must be non-negative")
    if keep not in {"first", "last"}:
        raise EvaluationError("keep must be 'first' or 'last'")
    ordered = sorted(list(reports), key=lambda item: (_report_time(item), str(_get(item, "report_id", default=""))))
    kept: list[Any] = []
    last_by_series: dict[tuple[Any, ...], tuple[datetime, tuple[Any, ...], int]] = {}
    seen_ids: set[str] = set()
    for report in ordered:
        report_id = str(_get(report, "report_id", default=""))
        if report_id and report_id in seen_ids:
            continue
        seen_ids.add(report_id)
        series = (
            str(_get(report, "broker", "broker_code", default="")) or report_id,
            _analyst_key(report),
            str(_get(report, "dimension", default="")),
            str(_get(report, "subject_id", "industry_id", default="")),
        )
        stance = (
            str(_get(report, "rating", "rating_norm", "rating_raw", default="")).lower(),
            str(_get(report, "rating_change", default="")).lower(),
            _float(_get(report, "target_price_min", "target_min")),
            _float(_get(report, "target_price_max", "target_max")),
            _normalized_title(_get(report, "title", default="")),
        )
        current_time = _report_time(report)
        previous = last_by_series.get(series)
        if previous and stance == previous[1] and current_time - previous[0] <= timedelta(days=episode_days):
            if keep == "last":
                kept[previous[2]] = report
            last_by_series[series] = (current_time, stance, previous[2])
            continue
        kept.append(report)
        last_by_series[series] = (current_time, stance, len(kept) - 1)
    return kept


def episode_deduplicate(
    items: Iterable[Any],
    reports: Iterable[Any] | Mapping[str, Any] | None = None,
    episode_days: int = 60,
    *,
    keep: str = "first",
) -> list[Any]:
    """Deduplicate same-report claims and unchanged forecast episodes.

    For repeated claims within one report/topic/horizon the highest confidence
    extraction is retained.  Across reports the first independently executable
    stance is retained until direction/value changes or the episode times out.
    """

    values = list(items)
    if not values:
        return []
    if keep not in {"first", "last"}:
        raise EvaluationError("keep must be 'first' or 'last'")
    if _get(values[0], "claim_id") is None:
        return deduplicate_reports(values, episode_days=episode_days, keep=keep)

    if isinstance(reports, Mapping):
        report_by_id = dict(reports)
    else:
        report_by_id = {
            str(_get(report, "report_id", default="")): report for report in (reports or [])
        }
    best_within_report: dict[tuple[Any, ...], Any] = {}
    for claim in values:
        key = (
            str(_get(claim, "report_id", default="")),
            str(_get(claim, "dimension", default="")),
            str(_get(claim, "subject_id", default="")),
            str(_get(claim, "target_type", default="")).lower(),
            int(_get(claim, "horizon_days", default=0) or 0),
            str(_get(claim, "forecast_period", default="")),
        )
        old = best_within_report.get(key)
        old_confidence = _float(_get(old, "extraction_confidence", default=0.0)) or 0.0
        new_confidence = _float(_get(claim, "extraction_confidence", default=0.0)) or 0.0
        if old is None or new_confidence > old_confidence:
            best_within_report[key] = claim

    ordered = sorted(
        best_within_report.values(),
        key=lambda item: (_datetime(_get(item, "available_at")), str(_get(item, "claim_id", default=""))),
    )
    kept: list[Any] = []
    last_by_series: dict[tuple[Any, ...], tuple[datetime, tuple[Any, ...], int]] = {}
    for claim in ordered:
        report = report_by_id.get(str(_get(claim, "report_id", default="")))
        series = (
            str(_get(report, "broker", "broker_code", default=""))
            or str(_get(claim, "report_id", default="")),
            _analyst_key(report),
            str(_get(claim, "dimension", default="")),
            str(_get(claim, "subject_id", default="")),
            str(_get(claim, "target_type", default="")).lower(),
            int(_get(claim, "horizon_days", default=0) or 0),
        )
        stance = (
            int(_get(claim, "direction", default=0) or 0),
            _float(_get(claim, "value_min")),
            _float(_get(claim, "value_max")),
            str(_get(claim, "benchmark", default="")),
            str(_get(claim, "forecast_period", default="")),
        )
        current_time = _datetime(_get(claim, "available_at"))
        previous = last_by_series.get(series)
        if previous and stance == previous[1] and current_time - previous[0] <= timedelta(days=episode_days):
            if keep == "last":
                kept[previous[2]] = claim
            last_by_series[series] = (current_time, stance, previous[2])
            continue
        kept.append(claim)
        last_by_series[series] = (current_time, stance, len(kept) - 1)
    return kept


def _truth_payload(
    truth: Any,
) -> tuple[
    float | None,
    datetime | None,
    str,
    str,
    str,
    float | None,
    str,
]:
    if truth is None:
        return None, None, "", "", "", None, ""
    value = _float(_get(truth, "realized_value", "value", "actual", default=truth))
    available = _available_at(truth)
    source = str(_get(truth, "truth_source", "source", default=""))
    unit = str(_get(truth, "unit", default="") or "").strip()
    basis = str(_get(truth, "basis", default="") or "").strip()
    change_value = _float(_get(truth, "change_value"))
    change_basis = str(_get(truth, "change_basis", default="") or "").strip()
    return value, available, source, unit, basis, change_value, change_basis


def _truth_contract_exclusion(claim: Any, truth: Any) -> str:
    if _get(truth, "evidence_verified") is not True:
        return "truth_evidence_not_verified"
    content_hash = str(_get(truth, "content_hash", default="") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", content_hash):
        return "invalid_truth_content_hash"
    evidence_url = str(_get(truth, "evidence_url", default="") or "").strip().lower()
    if not evidence_url.startswith(("https://", "http://")):
        return "missing_truth_evidence_url"
    if _get(truth, "first_release") is not True or _get(truth, "revision") is not False:
        return "truth_is_not_official_first_release"
    claim_id = str(_get(claim, "claim_id", default="")).strip()
    bound_claim_id = str(_get(truth, "claim_id", default="") or "").strip()
    if bound_claim_id and bound_claim_id != claim_id:
        return "truth_claim_id_mismatch"
    identity_fields = ("dimension", "subject_id", "target_type", "forecast_period")
    truth_identity = tuple(
        str(_get(truth, field, default="") or "").strip() for field in identity_fields
    )
    if not all(truth_identity):
        return "missing_truth_identity_locator"
    claim_identity = tuple(
        str(_get(claim, field, default="") or "").strip() for field in identity_fields
    )
    if tuple(value.casefold() for value in truth_identity) != tuple(
        value.casefold() for value in claim_identity
    ):
        return "truth_identity_mismatch"
    claim_unit = str(_get(claim, "unit", default="") or "").strip()
    truth_unit = str(_get(truth, "unit", default="") or "").strip()
    if not claim_unit or not truth_unit:
        return "missing_truth_unit_contract"
    if claim_unit.casefold() != truth_unit.casefold():
        return "truth_unit_mismatch"
    claim_basis = str(_get(claim, "benchmark", default="") or "").strip()
    truth_basis = str(_get(truth, "basis", default="") or "").strip()
    if not claim_basis or not truth_basis:
        return "missing_truth_basis_contract"
    if claim_basis.casefold() != truth_basis.casefold():
        return "truth_basis_mismatch"
    return ""


def _immature(claim: Any, reason: str, as_of: datetime, **extra: Any) -> Any:
    payload = {
        "claim_id": str(_get(claim, "claim_id", default="")),
        "truth_source": str(extra.pop("truth_source", "")),
        "truth_available_at": extra.pop("truth_available_at", None),
        "realized_value": extra.pop("realized_value", None),
        "market_return": extra.pop("market_return", None),
        "benchmark_return": extra.pop("benchmark_return", None),
        "market_truth_source": str(extra.pop("market_truth_source", "")),
        "market_benchmark_id": str(extra.pop("market_benchmark_id", "")),
        "market_benchmark_kind": str(extra.pop("market_benchmark_kind", "")),
        "truth_unit": str(extra.pop("truth_unit", "")),
        "truth_basis": str(extra.pop("truth_basis", "")),
        "truth_change_value": extra.pop("truth_change_value", None),
        "truth_change_basis": str(extra.pop("truth_change_basis", "")),
        "error": extra.pop("error", None),
        "hit": extra.pop("hit", None),
        "mature": False,
        "exclusion_reason": reason,
        "evaluated_at": as_of,
        "horizon_days": int(_get(claim, "horizon_days", default=0) or 0),
    }
    payload.update(extra)
    return _outcome(**payload)


def evaluate_claim(
    claim: Any,
    *,
    report: Any | None = None,
    bars: Iterable[Any] = (),
    benchmark_bars: Iterable[Any] | None = None,
    truth: Any | None = None,
    as_of: Any,
    rating_thresholds: Mapping[int, float] | None = None,
    numeric_tolerance: float = 0.0,
    eps_hit_threshold: float = 0.20,
    market_benchmark_id: str = "",
    market_benchmark_kind: str = "",
) -> Any:
    """Evaluate one claim without allowing future or incomplete evidence."""

    evaluation_time = _as_of(as_of)
    materialized_bars = list(bars)
    materialized_benchmark_bars = (
        list(benchmark_bars) if benchmark_bars is not None else None
    )
    usable_benchmark_bars = materialized_benchmark_bars or None
    claim_available = _available_at(claim)
    if claim_available is None:
        return _immature(claim, "missing_claim_available_at", evaluation_time)
    if claim_available > evaluation_time:
        return _immature(claim, "future_claim", evaluation_time)
    target_type = str(_get(claim, "target_type", default="")).strip().lower()
    horizon = int(_get(claim, "horizon_days", default=0) or 0)

    market_types = {
        "rating",
        "stock_rating",
        "rating_change",
        "industry_rating",
        "market_direction",
        "price_direction",
        "policy_direction",
        "target_price",
    }
    if target_type in market_types:
        if horizon <= 0:
            return _immature(claim, "missing_horizon", evaluation_time)
        dimension = str(_get(claim, "dimension", default="")).lower()
        benchmark_required = dimension in {"industry", "stock"} or bool(
            str(_get(claim, "benchmark", default="")).strip()
        )
        if benchmark_required and not materialized_benchmark_bars:
            reason = (
                "missing_industry_benchmark"
                if market_benchmark_kind == "missing_industry"
                else "missing_required_benchmark"
            )
            return _immature(
                claim,
                reason,
                evaluation_time,
                market_benchmark_id=market_benchmark_id,
                market_benchmark_kind=market_benchmark_kind,
            )
        anchor = report if report is not None else claim
        try:
            returns = compute_forward_returns(
                anchor,
                materialized_bars,
                benchmark_bars=usable_benchmark_bars,
                horizons=(horizon,),
                as_of=evaluation_time,
            )[horizon]
        except FutureDataError:
            return _immature(claim, "future_market_data", evaluation_time)
        except EvaluationError as exc:
            return _immature(claim, f"market_data_error:{exc}", evaluation_time)
        if not returns.get("mature"):
            return _immature(
                claim,
                str(returns.get("exclusion_reason") or "unmatured_horizon"),
                evaluation_time,
                market_benchmark_id=market_benchmark_id,
                market_benchmark_kind=market_benchmark_kind,
            )
        market_return = returns.get("market_return")
        benchmark_return = returns.get("benchmark_return")
        realized_excess = returns.get("excess_return")
        if realized_excess is None:
            realized_excess = market_return
        if target_type == "target_price":
            ordered = sorted(materialized_bars, key=_trade_date)
            start = returns["start_date"]
            end = returns["end_date"]
            path = [bar for bar in ordered if start <= _trade_date(bar) <= end]
            basis_ratios = []
            for bar in path:
                raw_close = _float(_get(bar, "close"))
                adjusted_close = _float(_get(bar, "adjusted_close", "adj_close"))
                explicit_factor = _float(_get(bar, "adjustment_factor", "adj_factor"))
                ratio = explicit_factor
                if ratio is None and raw_close and adjusted_close is not None:
                    ratio = adjusted_close / raw_close
                if ratio is not None:
                    basis_ratios.append(ratio)
            if basis_ratios and max(basis_ratios) - min(basis_ratios) > 1e-8 * max(1.0, abs(basis_ratios[0])):
                return _immature(
                    claim,
                    "corporate_action_price_basis_change",
                    evaluation_time,
                    market_return=market_return,
                    benchmark_return=benchmark_return,
                    market_truth_source=str(
                        returns.get("market_truth_source") or "market_bars"
                    ),
                    market_benchmark_id=market_benchmark_id,
                    market_benchmark_kind=market_benchmark_kind,
                )
            raw_start = _float(_get(path[0], "open")) if path else None
            raw_end = _float(_get(path[-1], "close")) if path else None
            try:
                metrics = target_price_metrics(
                    _get(claim, "value_min"),
                    _get(claim, "value_max"),
                    path,
                    starting_price=raw_start,
                    realized_price=raw_end,
                )
            except EvaluationError as exc:
                return _immature(
                    claim,
                    f"target_price_error:{exc}",
                    evaluation_time,
                    market_return=market_return,
                    benchmark_return=benchmark_return,
                    market_truth_source=str(
                        returns.get("market_truth_source") or "market_bars"
                    ),
                    market_benchmark_id=market_benchmark_id,
                    market_benchmark_kind=market_benchmark_kind,
                )
            starting_price = metrics.get("starting_price")
            target_low = metrics.get("target_min")
            target_high = metrics.get("target_max")
            if (
                starting_price is not None
                and target_low is not None
                and target_high is not None
                and float(target_low) <= float(starting_price) <= float(target_high)
            ):
                return _immature(
                    claim,
                    "target_already_reached_at_t0",
                    evaluation_time,
                    market_return=market_return,
                    benchmark_return=benchmark_return,
                    market_truth_source=str(
                        returns.get("market_truth_source") or "market_bars"
                    ),
                    market_exclusion_reason="target_already_reached_at_t0",
                    market_benchmark_id=market_benchmark_id,
                    market_benchmark_kind=market_benchmark_kind,
                )
            hit = metrics["hit"]
            error = metrics["ale"]
            realized_value = metrics["realized_price"]
        else:
            rating = evaluate_rating(
                int(_get(claim, "direction", default=0) or 0),
                float(realized_excess),
                horizon,
                thresholds=rating_thresholds,
            )
            hit = rating["hit"]
            error = rating["error"]
            realized_value = realized_excess
        return _outcome(
            claim_id=str(_get(claim, "claim_id", default="")),
            truth_source="market_bars",
            truth_available_at=_available_at(
                next(bar for bar in materialized_bars if _trade_date(bar) == returns["end_date"]),
                fallback_date=returns["end_date"],
            ),
            realized_value=realized_value,
            market_return=market_return,
            benchmark_return=benchmark_return,
            error=error,
            hit=hit,
            mature=True,
            exclusion_reason="",
            evaluated_at=evaluation_time,
            horizon_days=horizon,
            fundamental_hit=None,
            market_hit=hit,
            market_excess_return=realized_excess,
            market_truth_source=str(
                returns.get("market_truth_source") or "market_bars"
            ),
            market_benchmark_id=market_benchmark_id,
            market_benchmark_kind=market_benchmark_kind,
        )

    (
        actual,
        truth_available,
        truth_source,
        truth_unit,
        truth_basis,
        truth_change_value,
        truth_change_basis,
    ) = _truth_payload(truth)
    if actual is None:
        return _immature(claim, "missing_truth", evaluation_time)
    contract_exclusion = _truth_contract_exclusion(claim, truth)
    if contract_exclusion:
        return _immature(
            claim,
            contract_exclusion,
            evaluation_time,
            realized_value=actual,
            truth_available_at=truth_available,
            truth_source=truth_source,
            truth_unit=truth_unit,
            truth_basis=truth_basis,
            truth_change_value=truth_change_value,
            truth_change_basis=truth_change_basis,
            market_benchmark_id=market_benchmark_id,
            market_benchmark_kind=market_benchmark_kind,
        )
    if truth_available is None:
        return _immature(claim, "missing_truth_available_at", evaluation_time, realized_value=actual)
    if truth_available <= claim_available:
        return _immature(
            claim,
            "truth_not_after_claim",
            evaluation_time,
            realized_value=actual,
            truth_available_at=truth_available,
            truth_source=truth_source,
        )
    if truth_available > evaluation_time:
        return _immature(
            claim,
            "future_truth",
            evaluation_time,
            realized_value=actual,
            truth_available_at=truth_available,
            truth_source=truth_source,
        )
    target = _interval(_get(claim, "value_min"), _get(claim, "value_max"))
    direction = int(_get(claim, "direction", default=0) or 0)
    if target_type in {"eps", "eps_forecast", "earnings", "profit"}:
        if target is None:
            return _immature(claim, "missing_forecast_value", evaluation_time)
        forecast = (target[0] + target[1]) / 2.0
        error = smape(forecast, actual)
        hit = error is not None and error <= eps_hit_threshold
    elif target is not None:
        low, high = target
        if low <= actual <= high:
            error = 0.0
        else:
            boundary = low if actual < low else high
            error = abs(actual - boundary)
        hit = error <= float(numeric_tolerance)
    elif direction:
        if truth_change_value is None or not truth_change_basis:
            return _immature(
                claim,
                "missing_directional_change_truth",
                evaluation_time,
                realized_value=actual,
                truth_available_at=truth_available,
                truth_source=truth_source,
                truth_unit=truth_unit,
                truth_basis=truth_basis,
                market_benchmark_id=market_benchmark_id,
                market_benchmark_kind=market_benchmark_kind,
            )
        claim_basis = str(_get(claim, "benchmark", default="") or "").strip()
        if truth_change_basis.casefold() != claim_basis.casefold():
            return _immature(
                claim,
                "truth_change_basis_mismatch",
                evaluation_time,
                realized_value=actual,
                truth_available_at=truth_available,
                truth_source=truth_source,
                truth_unit=truth_unit,
                truth_basis=truth_basis,
                truth_change_value=truth_change_value,
                truth_change_basis=truth_change_basis,
                market_benchmark_id=market_benchmark_id,
                market_benchmark_kind=market_benchmark_kind,
            )
        error = max(0.0, -(direction * truth_change_value))
        hit = direction * truth_change_value > 0.0
    else:
        return _immature(claim, "unscorable_claim", evaluation_time)
    market_return = None
    benchmark_return = None
    market_excess_return = None
    market_hit = None
    market_truth_source = ""
    market_exclusion_reason = "missing_market_bars"
    fundamental_market_benchmark_required = (
        str(_get(claim, "dimension", default="")).lower() in {"industry", "stock"}
        or bool(str(_get(claim, "benchmark", default="")).strip())
    )
    if horizon > 0 and materialized_bars:
        try:
            market_metrics = compute_forward_returns(
                report if report is not None else claim,
                materialized_bars,
                benchmark_bars=usable_benchmark_bars,
                horizons=(horizon,),
                as_of=evaluation_time,
            )[horizon]
            if market_metrics.get("mature"):
                market_return = market_metrics.get("market_return")
                benchmark_return = market_metrics.get("benchmark_return")
                market_excess_return = market_metrics.get("excess_return")
                market_truth_source = str(
                    market_metrics.get("market_truth_source") or "market_bars"
                )
                if market_excess_return is None and not fundamental_market_benchmark_required:
                    market_excess_return = market_return
                if fundamental_market_benchmark_required and benchmark_return is None:
                    market_exclusion_reason = (
                        "missing_industry_benchmark"
                        if market_benchmark_kind == "missing_industry"
                        else "missing_required_benchmark"
                    )
                elif market_excess_return is not None and direction != 0:
                    market_hit = evaluate_rating(
                        direction,
                        float(market_excess_return),
                        horizon,
                        thresholds=rating_thresholds,
                    )["hit"]
                    market_exclusion_reason = ""
                elif direction == 0:
                    market_exclusion_reason = "claim_has_no_market_direction"
            else:
                market_exclusion_reason = str(
                    market_metrics.get("exclusion_reason") or "unmatured_market_horizon"
                )
        except EvaluationError as exc:
            # A fundamental truth remains mature independently of market data.
            market_exclusion_reason = f"market_data_error:{exc}"
    return _outcome(
        claim_id=str(_get(claim, "claim_id", default="")),
        truth_source=truth_source,
        truth_available_at=truth_available,
        realized_value=actual,
        market_return=market_return,
        benchmark_return=benchmark_return,
        error=error,
        hit=hit,
        mature=True,
        exclusion_reason="",
        evaluated_at=evaluation_time,
        horizon_days=horizon,
        fundamental_hit=hit,
        market_hit=market_hit,
        market_excess_return=market_excess_return,
        market_exclusion_reason=market_exclusion_reason,
        market_truth_source=market_truth_source,
        market_benchmark_id=market_benchmark_id,
        market_benchmark_kind=market_benchmark_kind,
        truth_unit=truth_unit,
        truth_basis=truth_basis,
        truth_change_value=truth_change_value,
        truth_change_basis=truth_change_basis,
    )


def evaluate_claims(
    claims: Iterable[Any],
    *,
    reports: Iterable[Any] | Mapping[str, Any] = (),
    bars_by_subject: Mapping[str, Iterable[Any]] | None = None,
    benchmark_bars: Iterable[Any] | Mapping[str, Iterable[Any]] | None = None,
    truths: Mapping[str, Any] | None = None,
    as_of: Any,
    deduplicate: bool = True,
    episode_days: int = 60,
    **kwargs: Any,
) -> list[Any]:
    """Batch counterpart of :func:`evaluate_claim` with episode controls."""

    if isinstance(reports, Mapping):
        report_by_id = dict(reports)
    else:
        report_by_id = {
            str(_get(report, "report_id", default="")): report for report in reports
        }
    materialized_subject_bars = {
        str(key): list(value) for key, value in (bars_by_subject or {}).items()
    }
    materialized_benchmark_bars = (
        {str(key): list(value) for key, value in benchmark_bars.items()}
        if isinstance(benchmark_bars, Mapping)
        else (list(benchmark_bars) if benchmark_bars is not None else None)
    )
    selected = list(claims)
    if deduplicate:
        selected = episode_deduplicate(selected, report_by_id, episode_days=episode_days)
    output: list[Any] = []
    for claim in selected:
        subject = str(_get(claim, "subject_id", default=""))
        claim_id = str(_get(claim, "claim_id", default=""))
        if isinstance(materialized_benchmark_bars, Mapping):
            benchmark = materialized_benchmark_bars.get(
                str(_get(claim, "benchmark", default="")), ()
            )
        else:
            benchmark = materialized_benchmark_bars
        output.append(
            evaluate_claim(
                claim,
                report=report_by_id.get(str(_get(claim, "report_id", default=""))),
                bars=materialized_subject_bars.get(subject, ()),
                benchmark_bars=benchmark,
                truth=(truths or {}).get(claim_id),
                as_of=as_of,
                **kwargs,
            )
        )
    return output


__all__ = [
    "CHINA_TZ",
    "DEFAULT_HORIZONS",
    "DEFAULT_RATING_THRESHOLDS",
    "EvaluationError",
    "FutureDataError",
    "compound_excess_return",
    "compute_forward_returns",
    "deduplicate_reports",
    "episode_deduplicate",
    "evaluate_claim",
    "evaluate_claims",
    "evaluate_rating",
    "rating_economic_threshold",
    "resolve_report_t0",
    "smape",
    "target_price_metrics",
    "wape",
]
