#!/usr/bin/env python3
"""Score DeepVan daily signals and write a portfolio action memo.

This script does not fetch source content, log in, or trade. It expects an
operator or agent to provide already-structured JSON signals.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any


BUCKETS = ("qd", "quant", "defense", "a_share_alpha")

DEFAULT_WEIGHTS = {
    "qd": 72.0,
    "quant": 25.0,
    "defense": 3.0,
    "a_share_alpha": 0.0,
}

TARGET_RANGES = {
    "qd": (50.0, 70.0),
    "quant": (20.0, 35.0),
    "defense": (5.0, 15.0),
    "a_share_alpha": (0.0, 15.0),
}

ASSET_TO_BUCKETS = {
    "qd": ("qd",),
    "us_growth": ("qd",),
    "sp500": ("qd",),
    "nasdaq": ("qd",),
    "emerging_market": ("qd",),
    "quant": ("quant",),
    "defense": ("defense",),
    "utilities": ("defense",),
    "cash": ("defense",),
    "gold_resource": ("defense",),
    "a_share_alpha": ("a_share_alpha",),
    "ai_semiconductor": ("qd", "a_share_alpha"),
}

BUCKET_LABELS = {
    "qd": "QD / 海外",
    "quant": "A股量化",
    "defense": "防守仓",
    "a_share_alpha": "A股强因子",
}


@dataclass(frozen=True)
class Signal:
    title: str
    asset: str
    direction: str
    strength: int
    confidence: int
    horizon: str
    summary: str
    evidence: str
    counter_evidence: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Signal":
        strength = int(data.get("strength", 1))
        confidence = int(data.get("confidence", 1))
        return cls(
            title=str(data.get("title", "未命名信号")),
            asset=str(data.get("asset", "qd")),
            direction=str(data.get("direction", "watch")),
            strength=max(1, min(5, strength)),
            confidence=max(1, min(5, confidence)),
            horizon=str(data.get("horizon", "days")),
            summary=str(data.get("summary", "")),
            evidence=str(data.get("evidence", "")),
            counter_evidence=str(data.get("counter_evidence", "")),
        )


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def load_input(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def normalize_weights(data: dict[str, Any]) -> dict[str, float]:
    raw = data.get("current_weights") or DEFAULT_WEIGHTS
    weights: dict[str, float] = {}
    for bucket in BUCKETS:
        weights[bucket] = float(raw.get(bucket, DEFAULT_WEIGHTS[bucket]))
    return weights


def score_signals(signals: list[Signal]) -> dict[str, float]:
    scores = {
        "qd": 50.0,
        "quant": 50.0,
        "defense": 35.0,
        "a_share_alpha": 0.0,
    }

    for signal in signals:
        buckets = ASSET_TO_BUCKETS.get(signal.asset, ("qd",))
        weight = signal.strength * (0.6 + signal.confidence * 0.12)

        for bucket in buckets:
            if signal.direction in ("bullish", "risk_on", "rotate_to"):
                scores[bucket] += weight * 5.0
                if bucket != "defense":
                    scores["defense"] -= weight * 1.2
            elif signal.direction in ("bearish", "risk_off", "rotate_from"):
                scores[bucket] -= weight * 6.0
                scores["defense"] += weight * 4.0
            elif signal.direction == "hold":
                scores[bucket] += weight * 1.0
            elif signal.direction == "watch":
                scores[bucket] += weight * 0.5

    return {bucket: round(clamp(score), 1) for bucket, score in scores.items()}


def action_for_bucket(bucket: str, score: float, current_weight: float) -> tuple[str, str]:
    low, high = TARGET_RANGES[bucket]

    if bucket == "a_share_alpha":
        if current_weight > high:
            return (
                "不新增，优先降集中度",
                f"当前 {current_weight:.1f}% 已超过 {high:.0f}% 研究上限，利好只能影响降仓节奏",
            )
        if score >= 80:
            return ("强信号，可建 3%-5% 卫星仓", "必须有个股替代量化基金的证据和退出条件")
        if score >= 60:
            return ("观察候选，不直接买", "信号未达到强动作阈值")
        return ("不买", "个股增强分不足")

    if score >= 75:
        if current_weight < high:
            return ("小幅加仓", f"分数较强且未超过 {high:.0f}% 上限")
        return ("维持", "分数较强但当前仓位已接近或超过上限")

    if score >= 55:
        return ("维持", "信号支持当前暴露，但不要求新增风险")

    if score >= 40:
        if current_weight > high:
            return ("小幅降仓", "分数中性偏弱且当前仓位偏高")
        return ("观察", "等待更多确认")

    if bucket == "defense":
        if current_weight < low:
            return ("补防守", f"防守需求不低且当前低于 {low:.0f}% 下限")
        return ("维持防守", "防守仓已具备缓冲")

    return ("降风险", "信号偏弱或风险上升")


def score_label(score: float) -> str:
    if score >= 80:
        return "强动作"
    if score >= 60:
        return "明确偏向"
    if score >= 40:
        return "观察"
    if score >= 20:
        return "偏弱"
    return "不动作"


def render_markdown(data: dict[str, Any], scores: dict[str, float], weights: dict[str, float], signals: list[Signal]) -> str:
    day = str(data.get("date") or date.today().isoformat())
    source_mode = str(data.get("source_mode", "manual_summary"))
    lines: list[str] = []

    lines.append(f"# DeepVan Daily Action - {day}")
    lines.append("")
    lines.append(f"- 来源模式：{source_mode}")
    lines.append("- 说明：本文件是个人投研动作建议，不是自动交易指令。")
    weight_source = str(data.get("weight_source") or "signal_input")
    if weight_source == "portfolio_snapshot":
        snapshot_name = str(data.get("portfolio_snapshot_name") or "-")
        portfolio_as_of = str(data.get("portfolio_as_of") or "-")
        lines.append(f"- 持仓快照：{snapshot_name}（as_of: {portfolio_as_of}）")
    elif weight_source == "historical_default":
        lines.append("- 持仓权重来源：历史默认值，仅用于兼容；不得视为当前真实持仓。")
    else:
        lines.append("- 持仓权重来源：信号文件显式输入。")
    lines.append("")

    lines.append("## 今日结论")
    lines.append("")
    for bucket in BUCKETS:
        action, reason = action_for_bucket(bucket, scores[bucket], weights[bucket])
        lines.append(f"- {BUCKET_LABELS[bucket]}：{action}（{reason}）")
    lines.append("")

    lines.append("## 持仓硬约束")
    lines.append("")
    portfolio_risk_flags = [str(item) for item in data.get("portfolio_risk_flags", []) if str(item)]
    if portfolio_risk_flags:
        for flag in portfolio_risk_flags:
            lines.append(f"- {flag}")
    else:
        lines.append("- 未从当前输入识别到额外持仓硬约束。")
    lines.append("")

    lines.append("## 分数")
    lines.append("")
    lines.append("| 模块 | 当前暴露 | 分数 | 状态 | 建议 |")
    lines.append("|---|---:|---:|---|---|")
    for bucket in BUCKETS:
        action, _ = action_for_bucket(bucket, scores[bucket], weights[bucket])
        lines.append(
            f"| {BUCKET_LABELS[bucket]} | {weights[bucket]:.1f}% | {scores[bucket]:.1f} | {score_label(scores[bucket])} | {action} |"
        )
    lines.append("")

    lines.append("## 星球信号")
    lines.append("")
    if signals:
        lines.append("| 信号 | 资产 | 方向 | 强度 | 置信度 | 时效 | 摘要 |")
        lines.append("|---|---|---|---:|---:|---|---|")
        for signal in signals:
            lines.append(
                f"| {signal.title} | {signal.asset} | {signal.direction} | {signal.strength} | {signal.confidence} | {signal.horizon} | {signal.summary} |"
            )
    else:
        lines.append("- 无结构化信号，默认不动作。")
    lines.append("")

    lines.append("## 对抗式审查")
    lines.append("")
    lines.append("- 是否过度解读：单条低置信度信号只允许观察或小幅动作。")
    lines.append("- 是否已被价格反映：强主题仍需检查是否追在高位。")
    lines.append("- 是否有基金替代：A股个股必须证明优于继续持有量化基金。")
    lines.append("- 如果错了怎么退出：任何新增风险都需要明确反证条件。")
    lines.append("")

    lines.append("## 今日禁止动作")
    lines.append("")
    lines.append("- 不因单条情绪化发言追高 QD 或个股。")
    lines.append("- 不把 A股主题 beta 当作个股 alpha。")
    lines.append("- 不在没有退出条件时扩大仓位。")
    lines.append("")

    lines.append("## 明日反证")
    lines.append("")
    counter_points = [signal.counter_evidence for signal in signals if signal.counter_evidence]
    if counter_points:
        for point in counter_points:
            lines.append(f"- {point}")
    else:
        lines.append("- 检查今天信号是否被市场确认，若未确认则维持或降低动作强度。")
    lines.append("")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Score DeepVan daily action signals.")
    parser.add_argument("--input", required=True, help="Path to structured signal JSON.")
    parser.add_argument("--output", required=True, help="Path to write markdown action memo.")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)
    data = load_input(input_path)
    signals = [Signal.from_dict(item) for item in data.get("signals", [])]
    weights = normalize_weights(data)
    scores = score_signals(signals)
    markdown = render_markdown(data, scores, weights, signals)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    print(f"Wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
