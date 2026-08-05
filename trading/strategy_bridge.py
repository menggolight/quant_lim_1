"""Fail-closed boundary between research outputs and executable targets.

The industry radar is a research product.  It can help form a candidate
universe, but it is deliberately impossible to pass the current R0 report
through this module as a trading signal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Mapping

from trading.models import ExecutionMode


@dataclass(frozen=True)
class SignalEnvelope:
    signal_id: str
    model_id: str
    model_admission: str
    source_kind: str
    available_at: datetime
    frozen_at: datetime | None
    data_snapshot_hash: str
    synthetic: bool
    trade_eligible: bool
    target_weights: Mapping[str, Decimal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.synthetic) is not bool or type(self.trade_eligible) is not bool:
            raise ValueError("signal flags must be booleans")
        if self.available_at.tzinfo is None:
            raise ValueError("available_at must be timezone-aware")
        if self.frozen_at is not None and self.frozen_at.tzinfo is None:
            raise ValueError("frozen_at must be timezone-aware")


class SignalRejected(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _reject(code: str, message: str) -> None:
    raise SignalRejected(code, message)


def targets_from_signal(
    signal: SignalEnvelope,
    decision_time: datetime,
    mode: ExecutionMode,
    maximum_age: timedelta = timedelta(hours=24),
) -> dict[str, Decimal]:
    """Validate provenance/admission and return weights, never orders."""

    if signal.model_id == "industry-radar-r0" or signal.source_kind == "industry_radar":
        _reject("research_radar_not_trade_signal", "行业雷达只能产生研究候选，不能直接触发订单")
    if signal.synthetic:
        _reject("synthetic_signal", "合成数据只能验证管线")
    if signal.frozen_at is None:
        _reject("signal_not_frozen", "信号必须先冻结再进入交易规划")
    if not signal.data_snapshot_hash.strip():
        _reject("data_snapshot_untraceable", "信号缺少不可变数据快照哈希")
    if not signal.trade_eligible:
        _reject("signal_not_trade_eligible", "研究输出尚未通过交易准入")
    if signal.available_at > decision_time:
        _reject("future_data", "信号在决策时间尚不可见")
    if signal.frozen_at > decision_time:
        _reject("signal_frozen_in_future", "冻结时间晚于决策时间")
    if signal.frozen_at < signal.available_at:
        _reject("invalid_signal_timeline", "冻结时间早于数据可用时间")
    if decision_time - signal.available_at > maximum_age:
        _reject("signal_stale", "信号超过最大允许时效")

    permitted_admissions = {
        ExecutionMode.PAPER: {"approved_for_paper", "approved_for_shadow", "approved_for_live"},
        ExecutionMode.SHADOW: {"approved_for_shadow", "approved_for_live"},
        ExecutionMode.LIVE: {"approved_for_live"},
    }
    if signal.model_admission not in permitted_admissions[mode]:
        _reject(f"model_not_approved_for_{mode.value.lower()}", f"模型未获准进入 {mode.value} 阶段")

    targets = {instrument_id: Decimal(str(weight)) for instrument_id, weight in signal.target_weights.items()}
    if not targets:
        _reject("empty_targets", "信号没有目标权重")
    if any(weight < 0 for weight in targets.values()):
        _reject("negative_target_weight", "目标权重不得为负")
    if sum(targets.values(), Decimal("0")) > Decimal("1"):
        _reject("target_weight_sum_exceeded", "目标权重总和超过100%")
    return targets
