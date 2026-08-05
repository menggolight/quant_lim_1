#!/usr/bin/env python3
"""Render a self-contained, read-only HTML dashboard for market observations."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DEFAULT_OUTPUT = Path("data") / "reports" / "market_observation" / "latest.html"
SUPPORTED_SCHEMA_VERSIONS = {"market-observation-v0.1"}
SUPPORTED_STATUSES = {"diagnostic_only_not_admitted"}
SUPPORTED_MANIFEST_VERSIONS = {"market-observation-manifest-v0.2"}
SUPPORTED_PIPELINE_PRODUCERS = {"market-observation-pipeline-v0.1"}

STATUS_LABELS = {
    "diagnostic_only_not_admitted": "诊断观察 · 未准入",
    "heuristic_baseline_not_alpha": "启发式基线 · 非 Alpha",
    "research_only_not_trade_eligible": "研究用途 · 不可交易",
}

STATE_LABELS = {
    "leading": "领先",
    "neutral_strong": "中性偏强",
    "improving_watch": "改善观察",
    "neutral": "中性",
    "neutral_weak": "中性偏弱",
    "lagging_rebound": "落后 / 反弹",
    "lagging_conflict": "落后 / 冲突",
    "lagging_active": "落后 / 活跃",
    "relative_strength_fundamentals_unconfirmed": "相对强势 · 基本面待确认",
    "industry_leading_stock_not_confirming": "行业领先 · 个股未确认",
    "industry_and_stock_mild_confirmation_with_margin_conflict": "温和确认 · 息差冲突",
    "relative_strength_profit_elasticity_unconfirmed": "相对强势 · 利润弹性待确认",
    "volume_fundamentals_strong_margin_and_price_weak": "量强 · 利润率与价格弱",
    "fundamental_strength_recent_price_concentration": "基本面强 · 涨幅集中",
}

MACRO_LABELS = {
    "neutral_defensive": "中性偏防御",
    "accommodative": "宽松",
    "weak": "偏弱",
    "weakening": "走弱",
    "contained": "可控",
    "low": "低",
    "short_term_repair_medium_term_unconfirmed": "短线修复 · 中期未确认",
    "short_term_broad_repair_medium_term_unconfirmed": "短线普修 · 中期未确认",
    "low_to_medium": "中低",
    "observe_only": "仅观察",
}

QUALITY_LABELS = {
    "success": "成功",
    "partial_success": "部分成功",
    "missing": "缺失",
    "not_admitted": "未准入",
    "failed_after_3_attempts_not_used": "连续失败 · 未使用",
    "partial_snapshot_then_pagination_failure_not_used": "仅部分截面 · 未使用",
}

QUALITY_NAMES = {
    "official_industry_source": "中证行业行情",
    "official_macro_sources": "官方宏观数据",
    "tencent_market_history": "股票与宽基行情",
    "eastmoney_market_history": "东方财富历史行情",
    "eastmoney_industry_board": "东方财富行业榜",
    "point_in_time_constituent_breadth": "时点成分股广度",
    "official_trade_calendar_adapter": "正式交易日历适配器",
    "formal_factor_eligibility": "正式因子准入",
}

COMPARISON_FIELD_LABELS = {
    "macro_environment": "宏观环境",
    "market_state": "市场状态",
    "risk_budget_observation": "风险观察",
    "research_action": "研究动作",
}


class ObservationValidationError(ValueError):
    """Raised when an observation is unsafe or structurally invalid."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ObservationValidationError(f"duplicate JSON key is not allowed: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ObservationValidationError(f"non-finite JSON number is not allowed: {value}")


def parse_json_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ObservationValidationError(f"cannot parse {label}") from exc
    if not isinstance(payload, dict):
        raise ObservationValidationError(f"{label} root must be an object")
    return payload


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _escape(value: Any, fallback: str = "—") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return html.escape(text, quote=True) if text else fallback


def _label(value: Any, mapping: dict[str, str]) -> str:
    if value is None:
        return "—"
    key = str(value)
    return _escape(mapping.get(key, key))


def _pct(value: Any, digits: int = 2) -> str:
    number = _number(value)
    if number is None:
        return "—"
    if number == 0:
        return f"{number:.{digits}f}%"
    return f"{number:+.{digits}f}%"


def _pp(value: Any, digits: int = 2, approximate: bool = True) -> str:
    number = _number(value)
    if number is None:
        return "—"
    prefix = "≈ " if approximate else ""
    formatted = f"{number:.{digits}f}" if number == 0 else f"{number:+.{digits}f}"
    return f"{prefix}{formatted} 个百分点"


def _decimal(value: Any, digits: int = 2) -> str:
    number = _number(value)
    if number is None:
        return "—"
    return f"{number:.{digits}f}"


def _metric_class(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "missing"
    if number > 0:
        return "positive"
    if number < 0:
        return "negative"
    return "flat"


def _data_number(value: Any) -> str:
    number = _number(value)
    return "" if number is None else str(number)


def _safe_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return html.escape(value.strip(), quote=True)


def _parse_iso_datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ObservationValidationError(f"{field_name} must be a non-empty ISO 8601 datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ObservationValidationError(f"{field_name} is not a valid ISO 8601 datetime") from exc
    if parsed.tzinfo is None:
        raise ObservationValidationError(f"{field_name} must include a timezone offset")
    return parsed


def _parse_iso_date(value: Any, field_name: str) -> date:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
        raise ObservationValidationError(f"{field_name} must be an ISO 8601 date")
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ObservationValidationError(f"{field_name} is not a valid calendar date") from exc


def _validate_available_at(
    value: Any,
    decision_time: datetime,
    field_name: str,
    *,
    required: bool,
) -> None:
    if not isinstance(value, str) or not value.strip():
        if required:
            raise ObservationValidationError(f"missing required availability field: {field_name}")
        return
    raw = value.strip()
    try:
        if len(raw) == 10:
            available_date = datetime.fromisoformat(raw).date()
            if available_date > decision_time.date():
                raise ObservationValidationError(f"future evidence is not allowed: {field_name}")
            if available_date == decision_time.date():
                raise ObservationValidationError(f"same-day date-only availability is ambiguous: {field_name}")
            return
        available_at = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ObservationValidationError(f"invalid availability value for {field_name}: {raw}") from exc
    if available_at.tzinfo is None:
        raise ObservationValidationError(f"{field_name} must include a timezone offset")
    if available_at > decision_time:
        raise ObservationValidationError(f"future evidence is not allowed: {field_name}")


def validate_observation(data: dict[str, Any]) -> None:
    """Fail closed on unsafe actions, malformed structures, and future evidence."""

    if not isinstance(data, dict):
        raise ObservationValidationError("observation root must be an object")
    for field in ("schema_version", "observation_id", "status", "as_of", "market_as_of", "decision_time", "generated_at"):
        if not isinstance(data.get(field), str) or not str(data[field]).strip():
            raise ObservationValidationError(f"missing required field: {field}")
    if data["schema_version"] not in SUPPORTED_SCHEMA_VERSIONS:
        raise ObservationValidationError(f"unsupported schema_version: {data['schema_version']}")
    if data["status"] not in SUPPORTED_STATUSES:
        raise ObservationValidationError(f"unsupported observation status: {data['status']}")

    market_as_of = _parse_iso_datetime(data["market_as_of"], "market_as_of")
    decision_time = _parse_iso_datetime(data["decision_time"], "decision_time")
    generated_at = _parse_iso_datetime(data["generated_at"], "generated_at")
    if market_as_of > decision_time:
        raise ObservationValidationError("market_as_of cannot follow decision_time")
    if generated_at < decision_time:
        raise ObservationValidationError("generated_at cannot precede decision_time")
    if str(data["as_of"]) != market_as_of.date().isoformat():
        raise ObservationValidationError("as_of must match the market_as_of calendar date")
    observation_id = str(data["observation_id"])
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{5,127}", observation_id):
        raise ObservationValidationError("observation_id contains unsafe characters")
    if str(data["as_of"]) not in observation_id:
        raise ObservationValidationError("observation_id must bind the as_of date")

    overall = data.get("overall")
    macro = data.get("macro")
    industry = data.get("industry")
    stock = data.get("stock")
    if not isinstance(overall, dict) or not isinstance(macro, dict) or not isinstance(industry, dict) or not isinstance(stock, dict):
        raise ObservationValidationError("overall, macro, industry, and stock must be objects")

    if overall.get("trade_action") is not None:
        raise ObservationValidationError("trade_action must remain null for a read-only observation dashboard")
    if overall.get("research_action") != "observe_only":
        raise ObservationValidationError("research_action must remain observe_only for schema v0.1")
    if macro.get("state") != overall.get("macro_environment"):
        raise ObservationValidationError("macro.state must equal overall.macro_environment")
    data_quality = data.get("data_quality")
    if not isinstance(data_quality, dict) or data_quality.get("formal_factor_eligibility") is not False:
        raise ObservationValidationError("formal_factor_eligibility must be explicitly false")

    sectors = industry.get("sectors")
    if not isinstance(sectors, list) or any(not isinstance(item, dict) for item in sectors):
        raise ObservationValidationError("industry.sectors must be a list of objects")
    samples = stock.get("cross_industry_observation_samples", [])
    if not isinstance(samples, list) or any(not isinstance(item, dict) for item in samples):
        raise ObservationValidationError("stock.cross_industry_observation_samples must be a list of objects")
    peers = stock.get("peer_prices", [])
    if not isinstance(peers, list) or any(not isinstance(item, dict) for item in peers):
        raise ObservationValidationError("stock.peer_prices must be a list of objects")

    sector_codes = [str(item.get("code") or "") for item in sectors]
    if any(not code for code in sector_codes) or len(sector_codes) != len(set(sector_codes)):
        raise ObservationValidationError("industry sector codes must be non-empty and unique")
    focus = _dict(stock.get("focus"))
    stock_ids = [str(focus.get("stock_id") or "")]
    stock_ids.extend(str(item.get("stock_id") or "") for item in samples)
    stock_ids.extend(str(item.get("stock_id") or "") for item in peers)
    if any(not stock_id for stock_id in stock_ids) or len(stock_ids) != len(set(stock_ids)):
        raise ObservationValidationError("stock IDs must be non-empty and unique across focus, samples, and peers")

    conflicts = data.get("three_layer_conflicts", [])
    if not isinstance(conflicts, list) or any(not isinstance(item, str) or not item.strip() for item in conflicts):
        raise ObservationValidationError("three_layer_conflicts must be a list of non-empty strings")
    normalized_conflicts = [item.strip() for item in conflicts]
    if len(normalized_conflicts) != len(set(normalized_conflicts)):
        raise ObservationValidationError("three_layer_conflicts must be unique")

    classification_as_of = _parse_iso_date(industry.get("classification_as_of"), "industry.classification_as_of")
    if classification_as_of > decision_time.date():
        raise ObservationValidationError("future industry classification is not allowed")

    macro_keys: list[tuple[str, str]] = []
    for observation in _list(macro.get("observations")):
        if not isinstance(observation, dict):
            raise ObservationValidationError("macro.observations must contain objects")
        metric = str(observation.get("metric") or "")
        period = str(observation.get("period") or "")
        if not metric or not period:
            raise ObservationValidationError("macro observations require metric and period")
        macro_keys.append((metric, period))
        _validate_available_at(
            observation.get("available_at"),
            decision_time,
            f"macro.observations[{metric}].available_at",
            required=True,
        )
    if len(macro_keys) != len(set(macro_keys)):
        raise ObservationValidationError("macro metric and period pairs must be unique")

    _validate_available_at(industry.get("available_at"), decision_time, "industry.available_at", required=True)
    _validate_available_at(stock.get("available_at"), decision_time, "stock.available_at", required=True)

    _validate_available_at(
        focus.get("price_available_at"),
        decision_time,
        "stock.focus.price_available_at",
        required=True,
    )
    _validate_available_at(
        focus.get("fundamental_available_at"),
        decision_time,
        "stock.focus.fundamental_available_at",
        required=True,
    )
    for index, sample in enumerate(samples):
        _validate_available_at(
            sample.get("price_available_at"),
            decision_time,
            f"stock.cross_industry_observation_samples[{index}].price_available_at",
            required=True,
        )
        _validate_available_at(
            sample.get("fundamental_available_at"),
            decision_time,
            f"stock.cross_industry_observation_samples[{index}].fundamental_available_at",
            required=True,
        )
    for index, peer in enumerate(peers):
        _validate_available_at(
            peer.get("price_available_at"),
            decision_time,
            f"stock.peer_prices[{index}].price_available_at",
            required=True,
        )

    pipeline = data.get("pipeline")
    if pipeline is not None:
        if not isinstance(pipeline, dict):
            raise ObservationValidationError("pipeline must be an object when present")
        sealed_at = _parse_iso_datetime(pipeline.get("sealed_at"), "pipeline.sealed_at")
        if sealed_at < decision_time or sealed_at < generated_at:
            raise ObservationValidationError("pipeline.sealed_at cannot precede decision_time or generated_at")


def load_observation(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    data = parse_json_object(raw, f"observation JSON: {path}")
    validate_observation(data)
    return data, hashlib.sha256(raw).hexdigest()


def validate_manifest(
    manifest_path: Path,
    observation: dict[str, Any],
    source_hash: str,
    input_path: Path,
) -> str:
    """Validate the diagnostic manifest and return its content hash."""

    raw = manifest_path.read_bytes()
    manifest = parse_json_object(raw, f"observation manifest: {manifest_path}")
    pipeline = _dict(observation.get("pipeline"))
    comparison = _dict(observation.get("comparison"))
    if pipeline.get("standard_cli_generated") is not True:
        raise ObservationValidationError("observation is not a sealed standard CLI product")
    if pipeline.get("producer") not in SUPPORTED_PIPELINE_PRODUCERS:
        raise ObservationValidationError("observation pipeline producer is unsupported")
    if comparison.get("status") not in {"first_baseline", "compared"}:
        raise ObservationValidationError("sealed observation is missing a valid comparison record")
    if manifest.get("manifest_version") not in SUPPORTED_MANIFEST_VERSIONS:
        raise ObservationValidationError("manifest_version is unsupported")
    if manifest.get("observation_id") != observation.get("observation_id"):
        raise ObservationValidationError("manifest observation_id does not match the observation")
    if manifest.get("status") != observation.get("status"):
        raise ObservationValidationError("manifest status does not match the observation")
    if manifest.get("as_of") != observation.get("as_of"):
        raise ObservationValidationError("manifest as_of does not match the observation")
    if manifest.get("generated_at") != observation.get("generated_at"):
        raise ObservationValidationError("manifest generated_at does not match the observation")
    if manifest.get("standard_cli_generated") is not True:
        raise ObservationValidationError("manifest is not a standard CLI product")
    if manifest.get("producer") != pipeline.get("producer"):
        raise ObservationValidationError("manifest producer does not match the sealed observation")
    if manifest.get("sealed_at") != pipeline.get("sealed_at"):
        raise ObservationValidationError("manifest sealed_at does not match the sealed observation")
    schema_entry = _dict(manifest.get("schema"))
    if schema_entry.get("schema_version") != observation.get("schema_version"):
        raise ObservationValidationError("manifest schema_version does not match the observation")
    if schema_entry.get("path") != pipeline.get("schema_path"):
        raise ObservationValidationError("manifest schema path does not match the sealed observation")
    if schema_entry.get("sha256") != pipeline.get("schema_sha256"):
        raise ObservationValidationError("manifest schema SHA-256 does not match the sealed observation")

    admission = _dict(manifest.get("admission"))
    required_blocked_admissions = {
        "source_data_admitted",
        "objective_factor_admitted",
        "research_report_factor_admitted",
        "paper_strategy_admitted",
        "live_trading_allowed",
    }
    if any(admission.get(field) is not False for field in required_blocked_admissions):
        raise ObservationValidationError("manifest admission fields must all remain false")
    if manifest.get("source_status") != observation.get("data_quality"):
        raise ObservationValidationError("manifest source_status does not match observation data_quality")

    manifest_comparison = _dict(manifest.get("comparison"))
    for field in ("status", "previous_observation_id", "previous_sha256"):
        if manifest_comparison.get(field) != comparison.get(field):
            raise ObservationValidationError(f"manifest comparison {field} does not match the observation")

    inputs = [_dict(item) for item in _list(manifest.get("inputs"))]
    draft_inputs = [item for item in inputs if item.get("role") == "draft_observation"]
    schema_inputs = [item for item in inputs if item.get("role") == "schema"]
    if len(draft_inputs) != 1 or draft_inputs[0].get("sha256") != pipeline.get("draft_sha256"):
        raise ObservationValidationError("manifest must bind exactly one draft observation hash")
    if (
        len(schema_inputs) != 1
        or schema_inputs[0].get("path") != pipeline.get("schema_path")
        or schema_inputs[0].get("sha256") != pipeline.get("schema_sha256")
    ):
        raise ObservationValidationError("manifest must bind exactly one matching Schema input")
    previous_observation_inputs = [item for item in inputs if item.get("role") == "previous_observation"]
    previous_manifest_inputs = [item for item in inputs if item.get("role") == "previous_manifest"]
    if comparison.get("status") == "first_baseline":
        if previous_observation_inputs or previous_manifest_inputs:
            raise ObservationValidationError("first baseline manifest must not bind previous inputs")
    elif (
        len(previous_observation_inputs) != 1
        or len(previous_manifest_inputs) != 1
        or previous_observation_inputs[0].get("sha256") != comparison.get("previous_sha256")
    ):
        raise ObservationValidationError("compared manifest must bind one previous observation and manifest")

    resolved_input = input_path.resolve()
    sealed_outputs = [
        _dict(item)
        for item in _list(manifest.get("outputs"))
        if _dict(item).get("role") == "sealed_observation"
    ]
    if len(sealed_outputs) != 1:
        raise ObservationValidationError("manifest must contain exactly one sealed_observation output")
    matching_output = False
    for entry in sealed_outputs:
        path_value = entry.get("path")
        if not isinstance(path_value, str):
            continue
        manifest_output_path = Path(path_value)
        if not manifest_output_path.is_absolute():
            manifest_output_path = Path.cwd() / manifest_output_path
        if (
            manifest_output_path.resolve() == resolved_input
            and entry.get("role") == "sealed_observation"
            and entry.get("sha256") == source_hash
        ):
            matching_output = True
            break
    if not matching_output:
        raise ObservationValidationError("manifest does not contain the observation file with its current SHA-256")
    return hashlib.sha256(raw).hexdigest()


def _find_macro_observation(data: dict[str, Any], metric: str) -> dict[str, Any]:
    for item in _list(_dict(data.get("macro")).get("observations")):
        if isinstance(item, dict) and item.get("metric") == metric:
            return item
    return {}


def _render_sector_rows(sectors: list[dict[str, Any]]) -> str:
    if not sectors:
        return '<tr class="empty-row"><td colspan="6">暂无行业数据；未将缺失值补成 0。</td></tr>'
    rows: list[str] = []
    for sector in sectors:
        state = str(sector.get("state") or "unknown")
        state_css = state if re.fullmatch(r"[a-z0-9_]+", state) else "unknown"
        absolute_20 = sector.get("return_20d_pct")
        absolute_60 = sector.get("return_60d_pct")
        excess_20 = sector.get("excess_20d_pct")
        excess_60 = sector.get("excess_60d_pct")
        ma20 = sector.get("above_prior_ma20")
        ma60 = sector.get("above_prior_ma60")
        turnover = sector.get("turnover_ratio")
        rows.append(
            f"""
            <tr data-excess-20d="{_data_number(excess_20)}" data-excess-60d="{_data_number(excess_60)}">
              <td><strong>{_escape(sector.get('name'))}</strong><small>{_escape(sector.get('code'))}</small></td>
              <td><span class="state-badge state-{state_css}">{_label(state, STATE_LABELS)}</span></td>
              <td class="metric-cell">
                <span class="metric-20d {_metric_class(absolute_20)}">{_pct(absolute_20)}</span>
                <span class="metric-60d {_metric_class(absolute_60)}">{_pct(absolute_60)}</span>
              </td>
              <td class="metric-cell strong-metric">
                <span class="metric-20d {_metric_class(excess_20)}">{_pct(excess_20)}</span>
                <span class="metric-60d {_metric_class(excess_60)}">{_pct(excess_60)}</span>
              </td>
              <td>
                <span class="metric-20d">{'上方' if ma20 is True else '下方' if ma20 is False else '—'}</span>
                <span class="metric-60d">{'上方' if ma60 is True else '下方' if ma60 is False else '—'}</span>
              </td>
              <td>{_decimal(turnover)}×</td>
            </tr>
            """.strip()
        )
    return "\n".join(rows)


def _render_sample_cards(samples: list[dict[str, Any]]) -> str:
    if not samples:
        return '<div class="empty-panel">暂无跨行业个股样本；未将缺失值补成 0。</div>'
    cards: list[str] = []
    for sample in samples:
        source_url = _safe_url(sample.get("fundamental_source"))
        source_link = f'<a href="{source_url}" target="_blank" rel="noreferrer">查看官方证据</a>' if source_url else ""
        excess_20 = sample.get("excess_20d_pct_points_approx")
        excess_60 = sample.get("excess_60d_pct_points_approx")
        cards.append(
            f"""
            <article class="sample-card">
              <div class="sample-head">
                <div><span class="eyebrow">{_escape(sample.get('industry'))}</span><h3>{_escape(sample.get('name'))}</h3><small>{_escape(sample.get('stock_id'))}</small></div>
                <span class="quote">{_decimal(sample.get('close'))}</span>
              </div>
              <p class="state-line">{_label(sample.get('state'), STATE_LABELS)}</p>
              <div class="sample-metrics">
                <div><small>20日收益</small><strong class="{_metric_class(sample.get('return_20d_pct'))}">{_pct(sample.get('return_20d_pct'))}</strong></div>
                <div><small>20日相对差*</small><strong class="{_metric_class(excess_20)}">{_pp(excess_20)}</strong></div>
                <div><small>60日相对差*</small><strong class="{_metric_class(excess_60)}">{_pp(excess_60)}</strong></div>
              </div>
              <p class="watch-reason">{_escape(sample.get('watch_reason'))}</p>
              <details>
                <summary>支持、反证与失效条件</summary>
                <dl>
                  <dt>支持</dt><dd>{_escape(sample.get('support'))}</dd>
                  <dt>反证</dt><dd>{_escape(sample.get('counter_evidence'))}</dd>
                  <dt>失效</dt><dd>{_escape(sample.get('invalidation'))}</dd>
                </dl>
                {source_link}
              </details>
            </article>
            """.strip()
        )
    return "\n".join(cards)


def _render_list(items: list[Any], css_class: str = "") -> str:
    if not items:
        return '<li class="muted">暂无记录</li>'
    class_attr = f' class="{html.escape(css_class, quote=True)}"' if css_class else ""
    return "\n".join(f"<li{class_attr}>{_escape(item)}</li>" for item in items)


def _render_deep_read(items: list[Any]) -> str:
    if not items:
        return '<li class="muted">暂无深读队列</li>'
    lines: list[str] = []
    for item in items:
        entry = _dict(item)
        lines.append(
            f"<li><span>{_escape(entry.get('priority'))}</span><div><strong>{_escape(entry.get('subject'))}</strong><p>{_escape(entry.get('reason'))}</p></div></li>"
        )
    return "\n".join(lines)


def _state_label(value: Any) -> str:
    if value is None:
        return "未取得"
    key = str(value)
    return _escape(MACRO_LABELS.get(key, STATE_LABELS.get(key, key)))


def _comparison_notice(comparison: dict[str, Any]) -> str:
    status = comparison.get("status")
    if status == "first_baseline":
        return "首次基线 · 暂无前次变化"
    if status != "compared":
        return "无可比基线 · 未默认变化为 0"
    previous_as_of = _escape(comparison.get("previous_as_of"))
    if comparison.get("has_material_change") is True:
        return f"较 {previous_as_of} 存在状态变化 · 详见下方"
    return f"较 {previous_as_of} 暂无已定义的状态变化"


def _render_comparison(comparison: dict[str, Any]) -> str:
    if comparison.get("status") == "first_baseline":
        return '<article class="panel"><h3>首次可比基线</h3><p class="muted">从下一期开始只突出状态迁移、新增冲突和已消失冲突。</p></article>'
    if comparison.get("status") != "compared":
        return '<article class="panel"><h3>无可比基线</h3><p class="muted">当前输入没有受控上一期，未将变化默认成 0。</p></article>'

    state_lines: list[str] = []
    for item in _list(comparison.get("overall_state_changes")):
        entry = _dict(item)
        field = str(entry.get("field") or "unknown")
        state_lines.append(
            f"<li><strong>{_escape(COMPARISON_FIELD_LABELS.get(field, field))}</strong>：{_state_label(entry.get('from'))} → {_state_label(entry.get('to'))}</li>"
        )
    for key, label in (("industry_state_changes", "行业"), ("stock_state_changes", "个股样本")):
        for item in _list(comparison.get(key)):
            entry = _dict(item)
            change_type = entry.get("change_type")
            if change_type == "added":
                detail = f"新增观察：{_state_label(entry.get('to'))}"
            elif change_type == "removed":
                detail = f"退出观察：原状态 {_state_label(entry.get('from'))}"
            else:
                detail = f"{_state_label(entry.get('from'))} → {_state_label(entry.get('to'))}"
            state_lines.append(f"<li><strong>{label} · {_escape(entry.get('subject_name'))}</strong>：{detail}</li>")

    new_conflicts = _list(comparison.get("new_conflicts"))
    resolved_conflicts = _list(comparison.get("resolved_conflicts"))
    changes_html = "\n".join(state_lines) if state_lines else '<li class="muted">宏观、行业与观察样本状态没有变化。</li>'
    return f"""
      <article class="panel">
        <h3>较 {_escape(comparison.get('previous_as_of'))} 的状态迁移</h3>
        <ul class="conflict-list">{changes_html}</ul>
      </article>
      <article class="panel">
        <h3>冲突变化</h3>
        <h4>新增</h4><ul class="conflict-list">{_render_list(new_conflicts)}</ul>
        <h4>已消失</h4><ul class="conflict-list">{_render_list(resolved_conflicts)}</ul>
      </article>
    """.strip()


def _render_quality(quality: dict[str, Any]) -> str:
    if not quality:
        return '<tr><td colspan="2">暂无数据质量记录</td></tr>'
    rows: list[str] = []
    for key, value in quality.items():
        raw = str(value).lower() if value is not None else "missing"
        if isinstance(value, bool):
            raw = "success" if value else "not_admitted"
        tone = "good" if raw == "success" else "warn" if "partial" in raw or raw == "missing" else "blocked"
        rows.append(
            f'<tr><td>{_escape(QUALITY_NAMES.get(key, key))}</td><td><span class="quality {tone}">{_escape(QUALITY_LABELS.get(raw, raw))}</span></td></tr>'
        )
    return "\n".join(rows)


def _render_source_links(data: dict[str, Any]) -> str:
    sources: list[tuple[str, str]] = []
    for item in _list(_dict(data.get("macro")).get("observations")):
        entry = _dict(item)
        url = _safe_url(entry.get("source"))
        if url:
            sources.append((str(entry.get("metric") or "宏观证据"), url))
    industry = _dict(data.get("industry"))
    methodology = _safe_url(industry.get("methodology_source"))
    if methodology:
        sources.append(("中证行业编制方案", methodology))
    template = industry.get("source_template")
    benchmark_id = industry.get("benchmark_id")
    if isinstance(template, str) and benchmark_id:
        benchmark_url = _safe_url(template.replace("{index_code}", str(benchmark_id)))
        if benchmark_url:
            sources.append(("中证全指官方序列", benchmark_url))

    unique: list[tuple[str, str]] = []
    seen: set[str] = set()
    for label, url in sources:
        if url not in seen:
            seen.add(url)
            unique.append((label, url))
    if not unique:
        return '<li class="muted">暂无可用来源链接</li>'
    return "\n".join(
        f'<li><a href="{url}" target="_blank" rel="noreferrer">{_escape(label)}</a></li>'
        for label, url in unique
    )


def render_dashboard(
    data: dict[str, Any],
    source_hash: str,
    *,
    manifest_verified: bool = False,
    manifest_hash: str | None = None,
) -> str:
    validate_observation(data)
    overall = _dict(data.get("overall"))
    macro = _dict(data.get("macro"))
    industry = _dict(data.get("industry"))
    stock = _dict(data.get("stock"))
    focus = _dict(stock.get("focus"))
    breadth = _dict(industry.get("cross_section"))
    csi300 = _find_macro_observation(data, "csi300_close")
    sectors = [item for item in _list(industry.get("sectors")) if isinstance(item, dict)]
    samples = [item for item in _list(stock.get("cross_industry_observation_samples")) if isinstance(item, dict)]
    conflicts = _list(data.get("three_layer_conflicts"))
    deep_read = _list(data.get("deep_read_queue"))
    quality = _dict(data.get("data_quality"))
    comparison = _dict(data.get("comparison"))
    status = str(data.get("status"))
    as_of = str(data.get("as_of"))

    focus_source = _safe_url(focus.get("fundamental_source"))
    focus_source_link = f'<a href="{focus_source}" target="_blank" rel="noreferrer">查看官方财报</a>' if focus_source else ""
    methodology_source = _safe_url(industry.get("methodology_source"))
    methodology_link = f'<a href="{methodology_source}" target="_blank" rel="noreferrer">中证行业口径</a>' if methodology_source else ""
    integrity_label = "文件完整性已核验 · 来源仍未准入" if manifest_verified else "文件完整性未核验 · 来源仍未准入"
    integrity_class = "good" if manifest_verified else "warn"
    comparison_notice = _comparison_notice(comparison)

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="宏观、行业、个股三层市场观察仪表盘；仅供研究，不形成交易动作。">
  <title>三层市场观察 · {_escape(as_of)}</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #14202b;
      --muted: #62707c;
      --paper: #f3f0e9;
      --card: #fffdf8;
      --line: #d9d5cb;
      --navy: #172632;
      --navy-2: #213847;
      --teal: #147d78;
      --red: #b8453c;
      --amber: #ad7416;
      --blue: #2e668b;
      --shadow: 0 12px 36px rgba(20, 32, 43, .08);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{ margin: 0; background: var(--paper); color: var(--ink); line-height: 1.55; }}
    a {{ color: var(--blue); text-underline-offset: 3px; }}
    button, summary, a {{ -webkit-tap-highlight-color: transparent; }}
    button:focus-visible, summary:focus-visible, a:focus-visible {{ outline: 3px solid rgba(46, 102, 139, .35); outline-offset: 3px; }}
    .hero {{ color: #f6f2e8; background: var(--navy); position: relative; overflow: hidden; }}
    .hero::before {{ content: ""; position: absolute; inset: 0; opacity: .14; background-image: linear-gradient(rgba(255,255,255,.12) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,.12) 1px, transparent 1px); background-size: 42px 42px; mask-image: linear-gradient(to right, black, transparent 80%); }}
    .hero-inner, main {{ width: min(1180px, calc(100% - 32px)); margin: 0 auto; }}
    .hero-inner {{ position: relative; padding: 42px 0 34px; }}
    .topline {{ display: flex; justify-content: space-between; align-items: center; gap: 16px; }}
    .status {{ display: inline-flex; align-items: center; gap: 8px; font-size: .78rem; letter-spacing: .08em; text-transform: uppercase; padding: 7px 11px; border: 1px solid rgba(255,255,255,.28); border-radius: 999px; background: rgba(255,255,255,.06); }}
    .status::before {{ content: ""; width: 8px; height: 8px; border-radius: 50%; background: #efb65b; box-shadow: 0 0 0 4px rgba(239,182,91,.13); }}
    .as-of {{ color: #b7c5cd; font-variant-numeric: tabular-nums; }}
    .freshness {{ display: block; margin-top: 4px; color: #9fc7bc; font-size: .78rem; text-align: right; }}
    .freshness.stale {{ color: #efb65b; font-weight: 750; }}
    h1 {{ margin: 34px 0 8px; max-width: 780px; font-family: ui-serif, "Songti SC", "STSong", serif; font-size: clamp(2.3rem, 6vw, 4.7rem); line-height: .98; letter-spacing: -.035em; font-weight: 650; }}
    .hero-copy {{ max-width: 760px; color: #c9d4da; font-size: clamp(1rem, 2vw, 1.18rem); }}
    .hero-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 1px; margin-top: 34px; border: 1px solid rgba(255,255,255,.14); background: rgba(255,255,255,.14); }}
    .hero-stat {{ min-height: 104px; padding: 18px; background: rgba(20,34,44,.9); }}
    .hero-stat small {{ display: block; color: #91a5b0; margin-bottom: 8px; }}
    .hero-stat strong {{ font-size: 1.15rem; }}
    main {{ padding: 28px 0 64px; }}
    .notice {{ display: flex; align-items: center; justify-content: space-between; gap: 20px; margin-bottom: 24px; padding: 15px 18px; background: #fff6dc; border: 1px solid #ead6a1; border-left: 5px solid var(--amber); }}
    .notice strong {{ color: #6d4a0d; }}
    .notice span {{ color: #7f6b43; font-size: .9rem; }}
    section {{ margin-top: 34px; }}
    .section-head {{ display: flex; justify-content: space-between; align-items: end; gap: 20px; margin-bottom: 14px; }}
    .section-head h2 {{ margin: 0; font-family: ui-serif, "Songti SC", "STSong", serif; font-size: clamp(1.55rem, 3vw, 2.2rem); }}
    .section-head p {{ margin: 4px 0 0; color: var(--muted); }}
    .cards {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; }}
    .card, .panel, .sample-card {{ background: var(--card); border: 1px solid var(--line); box-shadow: var(--shadow); }}
    .card {{ padding: 20px; min-height: 142px; }}
    .card small, .sample-card small {{ color: var(--muted); }}
    .card .value {{ display: block; margin: 12px 0 4px; font-family: ui-serif, "Songti SC", serif; font-size: 1.55rem; }}
    .card p {{ margin: 5px 0 0; color: var(--muted); font-size: .9rem; }}
    .split {{ display: grid; grid-template-columns: 1.15fr .85fr; gap: 16px; }}
    .panel {{ padding: 22px; }}
    .panel h3 {{ margin: 0 0 14px; }}
    .metric-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }}
    .mini {{ padding: 14px; background: #f4f6f5; border-top: 3px solid var(--teal); }}
    .mini small {{ display: block; color: var(--muted); }}
    .mini strong {{ display: block; margin-top: 5px; font-size: 1.1rem; }}
    .toggle {{ display: inline-flex; padding: 3px; background: #dedbd3; border-radius: 999px; }}
    .toggle button {{ border: 0; border-radius: 999px; background: transparent; color: #53606b; padding: 7px 13px; font: inherit; font-weight: 700; cursor: pointer; }}
    .toggle button[aria-pressed="true"] {{ background: var(--navy); color: white; box-shadow: 0 2px 9px rgba(20,32,43,.18); }}
    .table-wrap {{ overflow-x: auto; background: var(--card); border: 1px solid var(--line); box-shadow: var(--shadow); }}
    table {{ width: 100%; border-collapse: collapse; min-width: 760px; }}
    th, td {{ padding: 14px 16px; text-align: left; border-bottom: 1px solid #e6e2d9; font-variant-numeric: tabular-nums; }}
    th {{ position: sticky; top: 0; background: #ebe8e0; color: #53606b; font-size: .78rem; letter-spacing: .06em; text-transform: uppercase; }}
    tbody tr:last-child td {{ border-bottom: 0; }}
    tbody tr:hover {{ background: #f8f6f0; }}
    td small {{ display: block; color: var(--muted); }}
    .metric-cell {{ font-weight: 700; }}
    .strong-metric {{ font-size: 1.03rem; }}
    .metric-60d {{ display: none; }}
    body[data-horizon="60d"] .metric-20d {{ display: none; }}
    body[data-horizon="60d"] .metric-60d {{ display: inline; }}
    .positive {{ color: var(--teal); }}
    .negative {{ color: var(--red); }}
    .flat, .missing {{ color: var(--muted); }}
    .state-badge {{ display: inline-block; padding: 5px 9px; border-radius: 3px; background: #e7e5de; color: #42505b; font-size: .78rem; font-weight: 750; white-space: nowrap; }}
    .state-leading {{ background: #d9eee9; color: #12655f; }}
    .state-improving_watch, .state-neutral_strong {{ background: #e5edf2; color: #285d7d; }}
    .state-lagging_conflict, .state-lagging_rebound, .state-lagging_active {{ background: #f4dfda; color: #953b34; }}
    .samples {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 14px; }}
    .sample-card {{ padding: 20px; }}
    .sample-head {{ display: flex; justify-content: space-between; gap: 20px; align-items: start; }}
    .sample-head h3 {{ margin: 2px 0 0; font-family: ui-serif, "Songti SC", serif; font-size: 1.45rem; }}
    .eyebrow {{ color: var(--blue); font-size: .75rem; font-weight: 800; letter-spacing: .08em; }}
    .quote {{ font-size: 1.35rem; font-weight: 800; font-variant-numeric: tabular-nums; }}
    .state-line {{ color: var(--muted); font-weight: 700; }}
    .sample-metrics {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin: 16px 0; }}
    .sample-metrics div {{ padding: 10px; background: #f1f2ee; }}
    .sample-metrics small, .sample-metrics strong {{ display: block; }}
    .sample-metrics strong {{ overflow-wrap: anywhere; }}
    .watch-reason {{ min-height: 3.1em; }}
    details {{ border-top: 1px solid var(--line); padding-top: 11px; }}
    summary {{ cursor: pointer; color: var(--blue); font-weight: 750; }}
    dl {{ display: grid; grid-template-columns: 48px 1fr; gap: 7px 12px; font-size: .88rem; }}
    dt {{ font-weight: 800; }}
    dd {{ margin: 0; color: #50606b; }}
    .focus-card {{ display: grid; grid-template-columns: .8fr 1.2fr; gap: 18px; }}
    .focus-price {{ display: flex; flex-direction: column; justify-content: space-between; padding: 24px; background: var(--navy-2); color: white; }}
    .focus-price .big {{ font-family: ui-serif, "Songti SC", serif; font-size: 3rem; }}
    .focus-price p {{ color: #bfccd3; }}
    .focus-evidence {{ padding: 24px; background: var(--card); border: 1px solid var(--line); }}
    .evidence-cols {{ display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }}
    .evidence-cols h4 {{ margin: 0 0 8px; }}
    .evidence-cols ul, .conflict-list {{ margin: 0; padding-left: 20px; }}
    .evidence-cols li, .conflict-list li {{ margin: 8px 0; }}
    .deep-list {{ list-style: none; margin: 0; padding: 0; }}
    .deep-list li {{ display: grid; grid-template-columns: 34px 1fr; gap: 12px; padding: 12px 0; border-bottom: 1px solid var(--line); }}
    .deep-list li > span {{ display: grid; place-items: center; width: 28px; height: 28px; background: var(--navy); color: white; border-radius: 50%; font-weight: 800; }}
    .deep-list p {{ margin: 3px 0 0; color: var(--muted); }}
    .quality-table {{ min-width: 0; }}
    .quality {{ display: inline-block; padding: 4px 8px; border-radius: 999px; font-size: .76rem; font-weight: 800; }}
    .quality.good {{ background: #d9eee9; color: #12655f; }}
    .quality.warn {{ background: #faeccb; color: #7a550f; }}
    .quality.blocked {{ background: #f1deda; color: #8a3832; }}
    .source-list {{ columns: 2; padding-left: 20px; }}
    .source-list li {{ break-inside: avoid; margin: 7px 0; }}
    .meta {{ margin-top: 34px; padding-top: 18px; border-top: 1px solid var(--line); color: var(--muted); font-size: .78rem; display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; }}
    .muted {{ color: var(--muted); }}
    .empty-panel, .empty-row td {{ padding: 28px; color: var(--muted); text-align: center; }}
    @media (max-width: 900px) {{
      .hero-grid, .cards {{ grid-template-columns: repeat(2, 1fr); }}
      .split, .focus-card {{ grid-template-columns: 1fr; }}
    }}
    @media (max-width: 640px) {{
      .hero-inner, main {{ width: min(100% - 22px, 1180px); }}
      .hero-inner {{ padding-top: 24px; }}
      .topline, .notice, .section-head {{ align-items: flex-start; flex-direction: column; }}
      .hero-grid, .cards, .samples, .metric-grid, .evidence-cols {{ grid-template-columns: 1fr; }}
      .sample-metrics {{ grid-template-columns: 1fr; }}
      .sample-metrics div {{ padding: 8px; }}
      .source-list {{ columns: 1; }}
    }}
    @media print {{
      body {{ background: white; }}
      .hero {{ background: white; color: var(--ink); border-bottom: 2px solid var(--ink); }}
      .hero-copy, .as-of, .hero-stat small {{ color: var(--muted); }}
      .hero-stat {{ background: white; }}
      .toggle {{ display: none; }}
      .card, .panel, .sample-card, .table-wrap {{ box-shadow: none; break-inside: avoid; }}
    }}
  </style>
</head>
<body data-horizon="20d">
  <header class="hero">
    <div class="hero-inner">
      <div class="topline">
        <span class="status">{_escape(STATUS_LABELS.get(status, status))}</span>
        <div><span class="as-of">决策时点 {_escape(data.get('decision_time'))}</span><span class="freshness" id="freshness-status" aria-live="polite">正在检查数据时效…</span></div>
      </div>
      <h1>三层市场观察</h1>
      <p class="hero-copy">从宏观判断能承担多少风险，从行业判断资金和盈利可能流向哪里，再用个股检验行业结论。每个判断都保留反证，不用一个总分掩盖冲突。</p>
      <div class="hero-grid">
        <div class="hero-stat"><small>宏观环境</small><strong>{_label(overall.get('macro_environment'), MACRO_LABELS)}</strong></div>
        <div class="hero-stat"><small>市场状态</small><strong>{_label(overall.get('market_state'), MACRO_LABELS)}</strong></div>
        <div class="hero-stat"><small>风险观察 · 非仓位建议</small><strong>{_label(overall.get('risk_budget_observation'), MACRO_LABELS)}</strong></div>
        <div class="hero-stat"><small>研究动作</small><strong>{_label(overall.get('research_action'), MACRO_LABELS)}</strong></div>
      </div>
    </div>
  </header>

  <main>
    <div class="notice"><strong>未生成交易动作</strong><span>{comparison_notice}；观察样本 ≠ 推荐名单；缺失值显示“—”，不会补成 0。</span></div>

    <section aria-labelledby="change-title">
      <div class="section-head"><div><h2 id="change-title">与上一期相比</h2><p>只显示定义明确的状态迁移；没有上一期时不会默认成“无变化”。</p></div></div>
      <div class="split">{_render_comparison(comparison)}</div>
    </section>

    <section aria-labelledby="macro-title">
      <div class="section-head"><div><h2 id="macro-title">宏观与市场温度</h2><p>先判断当前环境是否支持扩大风险。</p></div></div>
      <div class="cards">
        <article class="card"><small>流动性</small><strong class="value">{_label(macro.get('liquidity'), MACRO_LABELS)}</strong><p>总量不紧，不等于信用扩张。</p></article>
        <article class="card"><small>信用传导</small><strong class="value">{_label(macro.get('credit_transmission'), MACRO_LABELS)}</strong><p>M1与居民融资仍需改善。</p></article>
        <article class="card"><small>增长动能</small><strong class="value">{_label(macro.get('growth_momentum'), MACRO_LABELS)}</strong><p>PMI与新订单是主要反证。</p></article>
        <article class="card"><small>权益风险偏好</small><strong class="value">{_label(macro.get('equity_risk_appetite'), MACRO_LABELS)}</strong><p>单日反弹不覆盖中期趋势。</p></article>
      </div>
      <div class="split" style="margin-top:14px">
        <article class="panel">
          <h3>宽基快照</h3>
          <div class="metric-grid">
            <div class="mini"><small>沪深300 · 20日</small><strong class="{_metric_class(csi300.get('return_20d_pct'))}">{_pct(csi300.get('return_20d_pct'))}</strong></div>
            <div class="mini"><small>沪深300 · 60日</small><strong class="{_metric_class(csi300.get('return_60d_pct'))}">{_pct(csi300.get('return_60d_pct'))}</strong></div>
            <div class="mini"><small>中证全指 · 当日</small><strong class="{_metric_class(industry.get('benchmark_return_1d_pct'))}">{_pct(industry.get('benchmark_return_1d_pct'))}</strong></div>
            <div class="mini"><small>中证全指 · 20日</small><strong class="{_metric_class(industry.get('benchmark_return_20d_pct'))}">{_pct(industry.get('benchmark_return_20d_pct'))}</strong></div>
            <div class="mini"><small>中证全指 · 60日</small><strong class="{_metric_class(industry.get('benchmark_return_60d_pct'))}">{_pct(industry.get('benchmark_return_60d_pct'))}</strong></div>
            <div class="mini"><small>成交 / 20日均值</small><strong>{_decimal(industry.get('benchmark_turnover_ratio_vs_prior_20d'))}×</strong></div>
          </div>
        </article>
        <article class="panel">
          <h3>行业横截面</h3>
          <div class="metric-grid">
            <div class="mini"><small>当日上涨</small><strong>{_escape(breadth.get('up_1d_count'))} / {_escape(breadth.get('sector_count'))}</strong></div>
            <div class="mini"><small>站上20日均值</small><strong>{_escape(breadth.get('above_prior_ma20_count'))} / {_escape(breadth.get('sector_count'))}</strong></div>
            <div class="mini"><small>站上60日均值</small><strong>{_escape(breadth.get('above_prior_ma60_count'))} / {_escape(breadth.get('sector_count'))}</strong></div>
          </div>
          <p class="muted">这里是11个一级行业之间的横截面，不是成分股广度。{methodology_link}</p>
        </article>
      </div>
    </section>

    <section aria-labelledby="industry-title">
      <div class="section-head">
        <div><h2 id="industry-title">行业相对强弱</h2><p><span id="horizon-copy">20日</span>收益；点击切换后按相对收益重新排序。</p></div>
        <div class="toggle" role="group" aria-label="行业观察期限">
          <button type="button" data-horizon-button="20d" aria-pressed="true">20日</button>
          <button type="button" data-horizon-button="60d" aria-pressed="false">60日</button>
        </div>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>行业</th><th>状态</th><th>绝对收益</th><th>相对中证全指</th><th>此前均线</th><th>成交比</th></tr></thead>
          <tbody id="sector-body">{_render_sector_rows(sectors)}</tbody>
        </table>
      </div>
      <p class="muted">行业“相对中证全指”使用复合相对收益：(1 + 行业收益) ÷ (1 + 基准收益) − 1；不是简单百分点差。</p>
    </section>

    <section aria-labelledby="samples-title">
      <div class="section-head"><div><h2 id="samples-title">跨行业个股温度计</h2><p>用个股验证行业结论；它们不是推荐名单。</p></div></div>
      <div class="samples">{_render_sample_cards(samples)}</div>
      <p class="muted">20日行业收益为基于2026-06-30官方权重的近似剔除个股结果；60日跨越调样，只能使用含个股指数，不是精确 leave-one-out。</p>
    </section>

    <section aria-labelledby="focus-title">
      <div class="section-head"><div><h2 id="focus-title">美的：价格与基本面验证</h2><p>这里只验证“价格是否获得基本面确认”，不纳入个人持仓，也不主导市场结论。</p></div></div>
      <div class="focus-card">
        <article class="focus-price">
          <div><span class="eyebrow">{_escape(focus.get('stock_id'))}</span><h3>{_escape(focus.get('name'))}</h3><small>基本面 {_escape(focus.get('fundamental_as_of'))} · 公布 {_escape(focus.get('fundamental_available_at'))}</small></div>
          <div><span class="big">{_decimal(focus.get('close'))}</span><p>{_label(focus.get('state'), STATE_LABELS)}</p></div>
          <div class="sample-metrics">
            <div><small>20日</small><strong class="{_metric_class(focus.get('return_20d_pct'))}">{_pct(focus.get('return_20d_pct'))}</strong></div>
            <div><small>60日</small><strong class="{_metric_class(focus.get('return_60d_pct'))}">{_pct(focus.get('return_60d_pct'))}</strong></div>
            <div><small>扣非同比</small><strong class="{_metric_class(focus.get('net_profit_ex_items_yoy_pct'))}">{_pct(focus.get('net_profit_ex_items_yoy_pct'))}</strong></div>
          </div>
        </article>
        <article class="focus-evidence">
          <div class="evidence-cols">
            <div><h4 class="positive">支持证据</h4><ul>{_render_list(_list(focus.get('supporting_evidence')))}</ul></div>
            <div><h4 class="negative">反证与缺口</h4><ul>{_render_list(_list(focus.get('counter_evidence')))}</ul></div>
          </div>
          <p>{focus_source_link}</p>
        </article>
      </div>
    </section>

    <section aria-labelledby="conflict-title">
      <div class="split">
        <article class="panel"><h2 id="conflict-title">三层冲突</h2><ul class="conflict-list">{_render_list(conflicts)}</ul></article>
        <article class="panel"><h2>深读顺序</h2><ol class="deep-list">{_render_deep_read(deep_read)}</ol></article>
      </div>
    </section>

    <section aria-labelledby="quality-title">
      <div class="split">
        <article class="panel"><h2 id="quality-title">数据质量</h2><p><span class="quality {integrity_class}">{integrity_label}</span></p><table class="quality-table"><tbody>{_render_quality(quality)}</tbody></table></article>
        <article class="panel"><h2>来源入口</h2><details><summary>展开官方与公开数据链接</summary><ul class="source-list">{_render_source_links(data)}</ul></details></article>
      </div>
      <div class="split" style="margin-top:16px">
        <article class="panel"><h2>尚未取得</h2><ul class="conflict-list">{_render_list(_list(macro.get('unknowns')))}</ul></article>
        <article class="panel"><h2>口径与局限</h2><ul class="conflict-list">{_render_list(_list(industry.get('limitations')) + _list(stock.get('limitations')))}</ul></article>
      </div>
      <details class="panel" style="margin-top:16px"><summary>展开宏观判断失效条件</summary><ul class="conflict-list">{_render_list(_list(macro.get('invalidation')))}</ul></details>
    </section>

    <div class="meta">
      <span>观察 {_escape(data.get('observation_id'))} · 生成时间 {_escape(data.get('generated_at'))}</span>
      <span>输入 SHA-256 {_escape(source_hash[:16])}… · 清单 {_escape((manifest_hash or '未核验')[:16])}{'…' if manifest_hash else ''} · {_escape(data.get('schema_version'))}</span>
      <span>哈希只证明内容一致性，不证明来源官方性。</span>
    </div>
  </main>
  <noscript><p class="notice">JavaScript 已关闭；全部数据仍可阅读，但行业期限切换不可用。</p></noscript>
  <script>
    (() => {{
      const body = document.body;
      const tableBody = document.getElementById('sector-body');
      const horizonCopy = document.getElementById('horizon-copy');
      const freshnessStatus = document.getElementById('freshness-status');
      const decisionTime = new Date({json.dumps(str(data.get('decision_time')))});
      const buttons = Array.from(document.querySelectorAll('[data-horizon-button]'));
      function updateFreshness() {{
        const ageHours = (Date.now() - decisionTime.getTime()) / 3600000;
        freshnessStatus.classList.remove('stale');
        if (!Number.isFinite(ageHours)) {{
          freshnessStatus.textContent = '无法判断数据时效';
          freshnessStatus.classList.add('stale');
        }} else if (ageHours < -0.25) {{
          freshnessStatus.textContent = '决策时点晚于本机时间，请检查系统时间';
          freshnessStatus.classList.add('stale');
        }} else if (ageHours > 36) {{
          freshnessStatus.textContent = `距决策时点 ${{Math.floor(ageHours)}} 小时；请确认是否休市，否则重新生成`;
          freshnessStatus.classList.add('stale');
        }} else {{
          freshnessStatus.textContent = `距决策时点 ${{Math.max(0, Math.floor(ageHours))}} 小时`;
        }}
      }}
      function applyHorizon(horizon) {{
        body.dataset.horizon = horizon;
        horizonCopy.textContent = horizon === '60d' ? '60日' : '20日';
        buttons.forEach((button) => button.setAttribute('aria-pressed', String(button.dataset.horizonButton === horizon)));
        const rows = Array.from(tableBody.querySelectorAll('tr[data-excess-20d]'));
        rows.sort((left, right) => {{
          const leftValue = Number.parseFloat(left.dataset[`excess${{horizon === '60d' ? '60d' : '20d'}}`]);
          const rightValue = Number.parseFloat(right.dataset[`excess${{horizon === '60d' ? '60d' : '20d'}}`]);
          const safeLeft = Number.isFinite(leftValue) ? leftValue : Number.NEGATIVE_INFINITY;
          const safeRight = Number.isFinite(rightValue) ? rightValue : Number.NEGATIVE_INFINITY;
          return safeRight - safeLeft;
        }});
        rows.forEach((row) => tableBody.appendChild(row));
      }}
      buttons.forEach((button) => button.addEventListener('click', () => applyHorizon(button.dataset.horizonButton)));
      updateFreshness();
      applyHorizon('20d');
    }})();
  </script>
</body>
</html>
"""


def write_dashboard(
    input_path: Path,
    output_path: Path,
    manifest_path: Path | None = None,
    *,
    allow_replace: bool = True,
) -> Path:
    data, source_hash = load_observation(input_path)
    manifest_hash = None
    if manifest_path is not None:
        manifest_hash = validate_manifest(manifest_path, data, source_hash, input_path)
    content = render_dashboard(
        data,
        source_hash,
        manifest_verified=manifest_path is not None,
        manifest_hash=manifest_hash,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = content.encode("utf-8")
    if output_path.exists():
        existing = output_path.read_bytes()
        if existing == encoded:
            return output_path
        if not allow_replace:
            raise ObservationValidationError(f"refusing to overwrite non-identical dashboard: {output_path}")
    temporary_path = output_path.with_suffix(f"{output_path.suffix}.tmp")
    temporary_path.write_bytes(encoded)
    temporary_path.replace(output_path)
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a three-layer market observation as a local HTML dashboard.")
    parser.add_argument("--input", type=Path, required=True, help="Explicit observation JSON path.")
    parser.add_argument("--manifest", type=Path, required=True, help="Standard CLI manifest used to verify path, role, ID, status, Schema, and SHA-256.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Standalone HTML output path.")
    args = parser.parse_args()

    output_path = write_dashboard(args.input, args.output, args.manifest)
    print(f"Wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
