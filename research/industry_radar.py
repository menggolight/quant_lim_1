#!/usr/bin/env python3
"""Deterministic R0 baseline for cross-industry change detection.

R0 is an attention and state-classification baseline. It does not forecast
returns and must not be presented as alpha or as an automatic trade signal.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPO_ROOT / "configs" / "industry_radar.r0.json"
CHINA_TZ = timezone(timedelta(hours=8))


class IndustryRadarError(ValueError):
    """Base exception for invalid radar inputs."""


class FutureDataError(IndustryRadarError):
    """Raised when a feature was not legally available at decision time."""


STATE_LABELS = {
    "emerging": "启动观察",
    "strengthening": "趋势与基本面共振",
    "price_only": "仅价格确认",
    "crowded": "强势但拥挤/背离",
    "weakening": "转弱",
    "bottoming": "筑底观察",
    "mixed": "证据混合",
}

COMPONENT_LABELS = {
    "trend": "相对趋势",
    "breadth": "市场广度",
    "participation": "交易参与度",
    "fundamental": "产业基本面",
    "crowding": "拥挤风险",
}


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise IndustryRadarError(f"JSON root must be an object: {path}")
    return payload


def load_config(path: Path | None = None) -> dict[str, Any]:
    return load_json(path or DEFAULT_CONFIG_PATH)


def _parse_timestamp(value: str | date | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.max, tzinfo=CHINA_TZ)
    else:
        text_value = str(value).strip()
        if not text_value:
            raise IndustryRadarError("Timestamp must not be empty")
        if "T" not in text_value:
            parsed = datetime.combine(date.fromisoformat(text_value), time.max, tzinfo=CHINA_TZ)
        else:
            parsed = datetime.fromisoformat(text_value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CHINA_TZ)
    return parsed


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return sum(items) / len(items) if items else None


def rank_percentiles(values: dict[str, float]) -> dict[str, float]:
    """Return ascending percentile ranks with average ranks for ties."""

    if not values:
        return {}
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    count = len(ordered)
    if count == 1:
        return {ordered[0][0]: 50.0}

    result: dict[str, float] = {}
    index = 0
    while index < count:
        end = index + 1
        while end < count and ordered[end][1] == ordered[index][1]:
            end += 1
        average_index = (index + end - 1) / 2.0
        percentile = average_index / (count - 1) * 100.0
        for tied_index in range(index, end):
            result[ordered[tied_index][0]] = percentile
        index = end
    return result


def _feature_value(
    record: dict[str, Any],
    feature_name: str,
    decision_time: datetime,
) -> float | None:
    features = record.get("features") or {}
    item = features.get(feature_name)
    if item is None:
        return None
    if not isinstance(item, dict):
        raise IndustryRadarError(
            f"{record.get('industry_id', '?')}.{feature_name} must include value and available_at"
        )
    if "value" not in item or "available_at" not in item or not item.get("source"):
        raise IndustryRadarError(
            f"{record.get('industry_id', '?')}.{feature_name} lacks value, available_at, or source"
        )
    available_at = _parse_timestamp(str(item["available_at"]))
    if available_at > decision_time:
        raise FutureDataError(
            f"{record.get('industry_id', '?')}.{feature_name} available at "
            f"{available_at.isoformat()} after decision time {decision_time.isoformat()}"
        )
    try:
        return float(item["value"])
    except (TypeError, ValueError) as exc:
        raise IndustryRadarError(
            f"{record.get('industry_id', '?')}.{feature_name} is not numeric"
        ) from exc


def classify_state(
    components: dict[str, float | None],
    direction_score: float,
    previous: dict[str, Any] | None,
    thresholds: dict[str, float],
) -> str:
    trend = components.get("trend")
    breadth = components.get("breadth")
    fundamental = components.get("fundamental")
    crowding = components.get("crowding")
    if trend is None or breadth is None:
        return "mixed"

    strong = float(thresholds["strong"])
    weak = float(thresholds["weak"])
    breadth_confirmed = float(thresholds["breadth_confirmed"])
    fundamental_confirmed = float(thresholds["fundamental_confirmed"])
    crowded = float(thresholds["crowded"])
    divergence = float(thresholds["breadth_divergence"])
    bottoming_ceiling = float(thresholds["bottoming_trend_ceiling"])
    material_change = float(thresholds["material_score_change"])
    previous_direction = float((previous or {}).get("direction_score", direction_score))

    if (
        trend >= strong
        and crowding is not None
        and crowding >= crowded
        and trend - breadth >= divergence
    ):
        return "crowded"
    if (
        trend <= bottoming_ceiling
        and breadth >= breadth_confirmed
        and fundamental is not None
        and fundamental >= fundamental_confirmed
    ):
        return "bottoming"
    if direction_score <= weak and trend <= weak and breadth <= weak + 5.0:
        return "weakening"
    if trend >= strong and breadth >= breadth_confirmed:
        if fundamental is None or fundamental < fundamental_confirmed:
            return "price_only"
        return "strengthening"
    if (
        breadth >= breadth_confirmed
        and fundamental is not None
        and fundamental >= fundamental_confirmed
        and (trend < strong or direction_score - previous_direction >= material_change)
    ):
        return "emerging"
    if trend >= strong:
        return "price_only"
    return "mixed"


def _attention_score(
    components: dict[str, float | None],
    direction_score: float,
    state: str,
    previous: dict[str, Any] | None,
) -> float:
    trend = float(components.get("trend") or 50.0)
    breadth = float(components.get("breadth") or 50.0)
    crowding = float(components.get("crowding") or 0.0)
    extreme = abs(direction_score - 50.0) * 2.0
    divergence = abs(trend - breadth)
    previous_direction = (previous or {}).get("direction_score")
    score_change = (
        min(100.0, abs(direction_score - float(previous_direction)) * 4.0)
        if previous_direction is not None
        else 0.0
    )
    crowding_alert = crowding if state == "crowded" or crowding >= 70.0 else 0.0
    attention = 0.30 * extreme + 0.30 * score_change + 0.20 * divergence + 0.20 * crowding_alert
    previous_state = str((previous or {}).get("state") or "")
    if previous_state and previous_state != state:
        attention += 20.0
    return round(_clamp(attention), 1)


def _evidence(
    components: dict[str, float | None],
    state: str,
) -> tuple[list[str], list[str], str]:
    evidence_for: list[str] = []
    evidence_against: list[str] = []
    for name in ("trend", "breadth", "participation", "fundamental"):
        value = components.get(name)
        label = COMPONENT_LABELS[name]
        if value is None:
            evidence_against.append(f"{label}数据缺失")
        elif value >= 60.0:
            evidence_for.append(f"{label}位于同组 {value:.0f} 分位")
        elif value <= 40.0:
            evidence_against.append(f"{label}仅处于同组 {value:.0f} 分位")
    crowding = components.get("crowding")
    if crowding is not None and crowding >= 70.0:
        evidence_against.append(f"拥挤风险处于同组 {crowding:.0f} 分位")

    invalidations = {
        "emerging": "若广度重新跌破同组中位且基本面修正转负，则取消启动观察。",
        "strengthening": "若相对趋势和广度同步跌破同组中位，或盈利修正转负，则共振判断失效。",
        "price_only": "只有基本面随后确认才能升级；若广度收窄则降级为拥挤或证据混合。",
        "crowded": "若广度重新扩散且拥挤指标回落，可解除拥挤警报；若价格转弱则升级风险。",
        "weakening": "若广度先于价格回升且基本面停止下修，可转为筑底观察。",
        "bottoming": "若广度和基本面改善未能持续两个观察期，则筑底判断失效。",
        "mixed": "等待至少两个独立证据层同向变化，不因单一价格或事件升级状态。",
    }
    return evidence_for, evidence_against, invalidations[state]


def score_payload(
    payload: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_config = config or load_config()
    model_id = str(resolved_config.get("model_id") or "industry-radar-r0")
    decision_time = _parse_timestamp(str(payload.get("decision_time") or payload.get("as_of") or ""))
    records = payload.get("industries") or []
    if not isinstance(records, list) or not records:
        raise IndustryRadarError("industries must be a non-empty list")

    feature_groups = resolved_config["feature_groups"]
    unique_features = sorted({name for names in feature_groups.values() for name in names})
    minimum_count = int(resolved_config["minimums"]["industries_per_cohort"])
    cohorts: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    raw_values: dict[str, dict[str, float | None]] = {}

    for record in records:
        if not isinstance(record, dict):
            raise IndustryRadarError("Each industry record must be an object")
        industry_id = str(record.get("industry_id") or "").strip()
        market = str(record.get("market") or "").strip()
        classification = str(record.get("classification") or "").strip()
        if not industry_id or not market or not classification:
            raise IndustryRadarError("industry_id, market and classification are required")
        if industry_id in raw_values:
            raise IndustryRadarError(f"Duplicate industry_id: {industry_id}")
        cohorts[(market, classification)].append(record)
        raw_values[industry_id] = {
            name: _feature_value(record, name, decision_time) for name in unique_features
        }

    percentile_values: dict[str, dict[str, float]] = {industry_id: {} for industry_id in raw_values}
    for cohort_key, cohort_records in cohorts.items():
        if len(cohort_records) < minimum_count:
            raise IndustryRadarError(
                f"Cohort {cohort_key[0]}/{cohort_key[1]} has {len(cohort_records)} industries; "
                f"minimum is {minimum_count}"
            )
        for feature_name in unique_features:
            cohort_values = {
                str(record["industry_id"]): raw_values[str(record["industry_id"])][feature_name]
                for record in cohort_records
                if raw_values[str(record["industry_id"])][feature_name] is not None
            }
            ranks = rank_percentiles({key: float(value) for key, value in cohort_values.items()})
            for industry_id, rank in ranks.items():
                percentile_values[industry_id][feature_name] = rank

    direction_weights = {key: float(value) for key, value in resolved_config["direction_weights"].items()}
    thresholds = {key: float(value) for key, value in resolved_config["state_thresholds"].items()}
    minimum_confidence = float(resolved_config["minimums"]["confidence_for_directional_state"])
    results: list[dict[str, Any]] = []

    for record in records:
        industry_id = str(record["industry_id"])
        ranks = percentile_values[industry_id]
        components: dict[str, float | None] = {}
        for group_name, feature_names in feature_groups.items():
            components[group_name] = _mean(
                ranks[name] for name in feature_names if name in ranks
            )

        weighted = [
            (components[name], weight)
            for name, weight in direction_weights.items()
            if components.get(name) is not None
        ]
        if not weighted:
            raise IndustryRadarError(f"{industry_id} has no directional features")
        direction_score = sum(float(value) * weight for value, weight in weighted) / sum(
            weight for _, weight in weighted
        )
        available_count = sum(1 for value in raw_values[industry_id].values() if value is not None)
        confidence_score = available_count / len(unique_features) * 100.0
        previous = record.get("previous") if isinstance(record.get("previous"), dict) else None
        state = classify_state(components, direction_score, previous, thresholds)
        if confidence_score < minimum_confidence and state not in ("mixed", "price_only"):
            state = "mixed"
        evidence_for, evidence_against, invalidation = _evidence(components, state)

        result = {
            "industry_id": industry_id,
            "industry_name": str(record.get("industry_name") or industry_id),
            "market": str(record["market"]),
            "classification": str(record["classification"]),
            "state": state,
            "state_label": STATE_LABELS[state],
            "direction_score": round(direction_score, 1),
            "attention_score": _attention_score(components, direction_score, state, previous),
            "confidence_score": round(confidence_score, 1),
            "components": {
                key: round(value, 1) if value is not None else None
                for key, value in components.items()
            },
            "evidence_for": evidence_for,
            "evidence_against": evidence_against,
            "invalidation": invalidation,
            "changed_from": previous,
        }
        results.append(result)

    results.sort(
        key=lambda item: (
            float(item["attention_score"]),
            float(item["direction_score"]),
            str(item["industry_id"]),
        ),
        reverse=True,
    )
    return {
        "model_id": model_id,
        "model_status": str(resolved_config.get("status") or "heuristic_baseline_not_alpha"),
        "as_of": str(payload.get("as_of") or ""),
        "decision_time": decision_time.isoformat(),
        "industry_count": len(results),
        "industries": results,
    }


def _format_component(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def render_markdown(result: dict[str, Any]) -> str:
    lines = [
        f"# 行业变化雷达 - {result.get('as_of') or '-'}",
        "",
        f"- 模型：{result.get('model_id', 'industry-radar-r0')}",
        f"- 决策时间：{result.get('decision_time', '-')}",
        "- 定位：确定性关注基线，不是收益预测或自动交易信号；高关注不等于看多。",
        "",
        "## 状态变化榜",
        "",
        "| 关注 | 行业 | 市场 | 状态 | 方向 | 趋势 | 广度 | 基本面 | 拥挤 | 置信度 |",
        "|---:|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for item in result.get("industries") or []:
        components = item["components"]
        lines.append(
            f"| {item['attention_score']:.1f} | {item['industry_name']} | {item['market']} | "
            f"{item['state_label']} | {item['direction_score']:.1f} | "
            f"{_format_component(components.get('trend'))} | "
            f"{_format_component(components.get('breadth'))} | "
            f"{_format_component(components.get('fundamental'))} | "
            f"{_format_component(components.get('crowding'))} | {item['confidence_score']:.1f} |"
        )

    lines.extend(["", "## 重点证据", ""])
    for item in (result.get("industries") or [])[:5]:
        lines.append(f"### {item['industry_name']} · {item['state_label']}")
        lines.append("")
        for evidence in item.get("evidence_for") or ["暂无同向确认。"]:
            lines.append(f"- 支持：{evidence}")
        for evidence in item.get("evidence_against") or ["暂无显著冲突。"]:
            lines.append(f"- 反证：{evidence}")
        lines.append(f"- 失效条件：{item['invalidation']}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the R0 cross-industry change radar.")
    parser.add_argument("--input", required=True, help="Point-in-time industry feature JSON.")
    parser.add_argument("--output", required=True, help="Markdown report path.")
    parser.add_argument("--json-output", help="Optional structured result path.")
    parser.add_argument("--config", help="Optional R0 config path.")
    args = parser.parse_args()

    payload = load_json(Path(args.input))
    config = load_config(Path(args.config)) if args.config else load_config()
    result = score_payload(payload, config=config)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_markdown(result), encoding="utf-8")
    if args.json_output:
        json_output = Path(args.json_output)
        json_output.parent.mkdir(parents=True, exist_ok=True)
        json_output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
