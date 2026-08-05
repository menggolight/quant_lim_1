"""Command-line orchestration for the broker-report audit V1.

Concrete ingestion and analytics modules are imported only inside command
functions.  This keeps ``python -m research.broker_report_audit --help`` and an
offline empty-store run available even when an optional adapter is absent.
"""

from __future__ import annotations

import argparse
import csv
import inspect
import json
import sys
from dataclasses import dataclass, field, is_dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .reporting import (
    ARTIFACT_FILENAMES,
    ReportBundle,
    build_dashboard_data,
    write_report_bundle,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "configs" / "broker_report_audit.v1.json"
CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
VALID_DIMENSIONS = ("macro", "industry", "stock")
CANONICAL_CSI300_INSTRUMENT_ID = "000300.SH"


class ConfigurationError(ValueError):
    """Raised when the frozen V1 configuration is unsafe or incomplete."""


@dataclass(frozen=True)
class RuntimePaths:
    config: Path
    database: Path
    cache_directory: Path
    output_directory: Path


@dataclass
class PipelineState:
    reports: list[Any]
    claims: list[Any]
    outcomes: list[Any]
    skill_snapshots: list[Any]
    factor_observations: list[Any]
    truth_observations: list[Any] = field(default_factory=list)
    daily_bars: list[Any] = field(default_factory=list)


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    """Load and validate the fail-closed V1 configuration."""

    resolved = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not resolved.is_absolute():
        resolved = REPO_ROOT / resolved
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"Cannot load config {resolved}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigurationError("Config root must be a JSON object")
    if payload.get("research_only") is not True:
        raise ConfigurationError("research_only must remain true")
    if payload.get("automatic_trading_enabled") is not False:
        raise ConfigurationError("automatic_trading_enabled must remain false")
    llm = payload.get("llm")
    if not isinstance(llm, dict) or llm.get("enabled") is not False:
        raise ConfigurationError("Regular operation requires llm.enabled=false")
    if int(llm.get("maximum_reports_per_run", 0)) > 20:
        raise ConfigurationError("LLM exception-mode limit cannot exceed 20 reports")
    aliases = payload.get("broker_aliases")
    if not isinstance(aliases, dict) or len(aliases) != 18:
        raise ConfigurationError("broker_aliases must contain the frozen 18 broker groups")
    dimensions = payload.get("dimensions")
    if not isinstance(dimensions, dict) or set(dimensions) != set(VALID_DIMENSIONS):
        raise ConfigurationError("Config must define macro, industry and stock dimensions")
    outputs = payload.get("outputs")
    if not isinstance(outputs, list) or tuple(outputs) != ARTIFACT_FILENAMES:
        raise ConfigurationError("Config output contract does not match V1 artifact set")
    sources = payload.get("sources")
    market = sources.get("market") if isinstance(sources, Mapping) else None
    if not isinstance(market, Mapping):
        raise ConfigurationError("sources.market must configure the market truth adapter")
    provider = str(market.get("provider") or "").strip().lower()
    allowlist = market.get("truth_source_allowlist")
    if (
        not provider
        or not isinstance(allowlist, list)
        or provider not in {str(item).strip().lower() for item in allowlist}
    ):
        raise ConfigurationError(
            "sources.market.provider must be present in truth_source_allowlist"
        )
    frozen_values = {
        "schema_version": (payload.get("schema_version"), "1.0"),
        "status": (
            payload.get("status"),
            "research_only_not_trade_eligible",
        ),
        "timezone": (payload.get("timezone"), "Asia/Shanghai"),
        "model_id": (payload.get("model_id"), "broker-report-audit-v1"),
        "dates.sample_start": (payload.get("dates", {}).get("sample_start"), "2024-07-01"),
        "dates.sample_end": (payload.get("dates", {}).get("sample_end"), "2025-06-30"),
        "dates.evaluation_as_of": (payload.get("dates", {}).get("evaluation_as_of"), "2026-08-04"),
        "dates.factor_backfill_start": (payload.get("dates", {}).get("factor_backfill_start"), "2019-01-01"),
        "dates.factor_backfill_end": (payload.get("dates", {}).get("factor_backfill_end"), "2025-06-30"),
        "dates.skill_lookback_years": (
            payload.get("dates", {}).get("skill_lookback_years"),
            5,
        ),
        "sources.market.provider": (
            payload.get("sources", {}).get("market", {}).get("provider"),
            "eastmoney_public.push2his",
        ),
        "sources.market.truth_source_allowlist": (
            payload.get("sources", {}).get("market", {}).get(
                "truth_source_allowlist"
            ),
            ["eastmoney_public.push2his"],
        ),
        "sources.market.csi300_instrument_id": (
            payload.get("sources", {}).get("market", {}).get(
                "csi300_instrument_id"
            ),
            CANONICAL_CSI300_INSTRUMENT_ID,
        ),
        "horizons.macro_audit_trading_days": (
            payload.get("horizons", {}).get("macro_audit_trading_days"),
            [60],
        ),
        "horizons.industry_audit_trading_days": (
            payload.get("horizons", {}).get("industry_audit_trading_days"),
            [120],
        ),
        "horizons.stock_audit_trading_days": (
            payload.get("horizons", {}).get("stock_audit_trading_days"),
            [120],
        ),
        "horizons.factor_target_trading_days": (
            payload.get("horizons", {}).get("factor_target_trading_days"),
            [20, 60],
        ),
        "factor_research.input_contract_version": (
            payload.get("factor_research", {}).get("input_contract_version"),
            "broker-report-factor-input.v1",
        ),
        "factor_research.label_field": (
            payload.get("factor_research", {}).get("label_field"),
            "stock_excess_vs_industry_20d",
        ),
        "factor_research.secondary_label_field": (
            payload.get("factor_research", {}).get("secondary_label_field"),
            "stock_excess_vs_industry_60d",
        ),
        "factor_research.model": (payload.get("factor_research", {}).get("model"), "ridge"),
        "factor_research.rebalance_frequency": (
            payload.get("factor_research", {}).get("rebalance_frequency"),
            "weekly",
        ),
        "factor_research.minimum_stocks_per_rebalance_date": (
            payload.get("factor_research", {}).get("minimum_stocks_per_rebalance_date"),
            20,
        ),
        "factor_research.minimum_industries_per_rebalance_date": (
            payload.get("factor_research", {}).get("minimum_industries_per_rebalance_date"),
            3,
        ),
        "factor_research.minimum_train_rebalance_dates": (
            payload.get("factor_research", {}).get("minimum_train_rebalance_dates"),
            104,
        ),
        "factor_research.minimum_validation_rebalance_dates": (
            payload.get("factor_research", {}).get("minimum_validation_rebalance_dates"),
            13,
        ),
        "factor_research.minimum_test_rebalance_dates": (
            payload.get("factor_research", {}).get("minimum_test_rebalance_dates"),
            13,
        ),
        "factor_research.train_months": (payload.get("factor_research", {}).get("train_months"), 36),
        "factor_research.validation_months": (payload.get("factor_research", {}).get("validation_months"), 6),
        "factor_research.test_months": (payload.get("factor_research", {}).get("test_months"), 6),
        "factor_research.step_months": (payload.get("factor_research", {}).get("step_months"), 6),
        "factor_research.final_frozen_test_months": (
            payload.get("factor_research", {}).get("final_frozen_test_months"),
            12,
        ),
        "horizons.purge_embargo_trading_days": (
            payload.get("horizons", {}).get("purge_embargo_trading_days"),
            120,
        ),
        "skill.maximum_lookback_years": (
            payload.get("skill", {}).get("maximum_lookback_years"),
            5,
        ),
        "skill.half_life_days": (
            payload.get("skill", {}).get("half_life_days"),
            730,
        ),
        "skill.sensitivity_half_life_days": (
            payload.get("skill", {}).get("sensitivity_half_life_days"),
            365,
        ),
        "skill.rank_statistic": (
            payload.get("skill", {}).get("rank_statistic"),
            "conservative_lower_bound",
        ),
        "acceptance.manual_metadata_samples_per_dimension": (
            payload.get("acceptance", {}).get("manual_metadata_samples_per_dimension"),
            30,
        ),
        "acceptance.minimum_extraction_precision": (
            payload.get("acceptance", {}).get("minimum_extraction_precision"),
            0.95,
        ),
        "deep_read.maximum_limit": (
            payload.get("deep_read", {}).get("maximum_limit"),
            20,
        ),
    }
    for name, (actual, expected) in frozen_values.items():
        if actual != expected:
            raise ConfigurationError(
                f"{name} is frozen for broker-report-audit-v1: expected {expected!r}"
            )
    factor_research = payload.get("factor_research", {})
    if tuple(factor_research.get("interactions", ())) != (
        "macro_x_industry",
        "industry_x_stock",
    ):
        raise ConfigurationError("V1 allows exactly macro_x_industry and industry_x_stock")
    if factor_research.get("allow_three_way_interaction") is not False:
        raise ConfigurationError("V1 forbids three-way interactions")
    if tuple(factor_research.get("models", ())) != ("ridge", "logistic"):
        raise ConfigurationError("V1 model family is frozen to ridge/logistic")
    if tuple(factor_research.get("baselines", ())) != ("B0", "B1", "B2"):
        raise ConfigurationError("V1 baselines are frozen to B0/B1/B2")
    if factor_research.get("candidate") != "M1":
        raise ConfigurationError("V1 candidate model is frozen to M1")
    admission = factor_research.get("admission", {})
    expected_admission = {
        "minimum_out_of_sample_windows": 4,
        "minimum_positive_incremental_windows": 3,
        "mean_rank_ic_must_be_positive": True,
        "cost_adjusted_return_required": True,
        "single_industry_dominance_forbidden": True,
    }
    if not isinstance(admission, Mapping) or any(
        admission.get(name) != expected
        for name, expected in expected_admission.items()
    ):
        raise ConfigurationError("V1 walk-forward admission rules are frozen")
    if payload.get("llm", {}).get("exception_mode_enabled") is not False:
        raise ConfigurationError("V1 requires LLM exception mode to remain disabled")
    return payload


def _path(value: Path | str | None, fallback: str) -> Path:
    resolved = Path(value) if value is not None else Path(fallback)
    if not resolved.is_absolute():
        resolved = REPO_ROOT / resolved
    return resolved


def resolve_runtime_paths(
    config: Mapping[str, Any],
    *,
    config_path: Path | str | None = None,
    db_path: Path | str | None = None,
    cache_directory: Path | str | None = None,
    output_directory: Path | str | None = None,
) -> RuntimePaths:
    configured = config.get("paths")
    if not isinstance(configured, Mapping):
        raise ConfigurationError("Config paths must be an object")
    return RuntimePaths(
        config=_path(config_path, str(DEFAULT_CONFIG_PATH)),
        database=_path(db_path, str(configured.get("database") or "data/research_reports/broker_report_audit.sqlite3")),
        cache_directory=_path(cache_directory, str(configured.get("cache_directory") or "data/cache/broker_report_audit")),
        output_directory=_path(output_directory, str(configured.get("output_directory") or "data/reports/broker_report_audit")),
    )


def parse_dimensions(values: str | Sequence[str] | None) -> tuple[str, ...]:
    if values is None:
        return VALID_DIMENSIONS
    tokens = [values] if isinstance(values, str) else list(values)
    selected: list[str] = []
    for token in tokens:
        for part in str(token).split(","):
            dimension = part.strip().lower()
            if not dimension:
                continue
            if dimension not in VALID_DIMENSIONS:
                raise ConfigurationError(
                    f"Unknown dimension {dimension!r}; choose from {','.join(VALID_DIMENSIONS)}"
                )
            if dimension not in selected:
                selected.append(dimension)
    if not selected:
        raise ConfigurationError("At least one dimension is required")
    return tuple(selected)


def _date_value(value: Any, name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an ISO date") from exc


def _decision_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=CHINA_TZ)
        return parsed
    return datetime.combine(_date_value(value, "as_of"), time.max, tzinfo=CHINA_TZ)


def _as_of_text(value: Any) -> str:
    return _decision_time(value).date().isoformat()


def _field(record: Any, *names: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        for name in names:
            if name in record and record[name] is not None:
                return record[name]
        return default
    for name in names:
        if hasattr(record, name):
            value = getattr(record, name)
            if value is not None:
                return value
    return default


def _identifier(record: Any, *names: str) -> str:
    return str(_field(record, *names, default="") or "")


def _issue(
    code: str,
    message: str,
    *,
    stage: str,
    dimension: str = "",
    report_id: str = "",
    claim_id: str = "",
    severity: str = "warning",
    details: Any = None,
) -> dict[str, Any]:
    return {
        "severity": severity,
        "stage": stage,
        "code": code,
        "dimension": dimension,
        "report_id": report_id,
        "claim_id": claim_id,
        "message": message,
        "details": details or {},
    }


def _invoke_supported(function: Callable[..., Any], **kwargs: Any) -> Any:
    """Pass only keyword parameters declared by a narrow adapter interface."""

    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return function(**kwargs)
    has_var_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    if has_var_kwargs:
        selected = kwargs
    else:
        selected = {
            name: value
            for name, value in kwargs.items()
            if name in signature.parameters
            and signature.parameters[name].kind
            in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        }
    return function(**selected)


def _construct_supported(factory: Callable[..., Any], **kwargs: Any) -> Any:
    return _invoke_supported(factory, **kwargs)


def _iter_store(
    store: Any,
    method_name: str,
    issues: list[dict[str, Any]],
    **kwargs: Any,
) -> list[Any]:
    method = getattr(store, method_name, None)
    if method is None:
        issues.append(
            _issue(
                "STORE_INTERFACE_MISSING",
                f"AuditStore missing {method_name}; related output remains empty.",
                stage="storage",
                severity="error",
            )
        )
        return []
    try:
        return list(_invoke_supported(method, **kwargs))
    except Exception as exc:  # fail closed and preserve a report bundle
        issues.append(
            _issue(
                "STORE_READ_FAILED",
                f"{method_name} failed; related output remains empty.",
                stage="storage",
                severity="error",
                details={"error_type": type(exc).__name__, "error": str(exc)},
            )
        )
        return []


def _load_state(store: Any, decision: datetime, issues: list[dict[str, Any]]) -> PipelineState:
    return PipelineState(
        reports=_iter_store(
            store,
            "iter_reports",
            issues,
            available_by=decision,
            version_as_of=decision,
        ),
        claims=_iter_store(store, "iter_claims", issues, available_by=decision),
        outcomes=_iter_store(store, "iter_outcomes", issues, evaluated_by=decision),
        skill_snapshots=_iter_store(store, "iter_skill_snapshots", issues, as_of=decision),
        factor_observations=_iter_store(store, "iter_factor_observations", issues, as_of=decision),
        truth_observations=_iter_store(
            store,
            "iter_truth_observations",
            issues,
            decision_time=decision,
            first_release=None,
        ),
        daily_bars=_iter_store(
            store,
            "iter_daily_bars",
            issues,
            available_by=decision,
            version_as_of=decision,
        ),
    )


def _unpack_records(result: Any) -> tuple[list[Any], list[Any]]:
    """Accept list/generator, result objects, or ``(records, issues)`` adapters."""

    if result is None:
        return [], []
    if isinstance(result, Mapping):
        records = result.get("reports", result.get("records", result.get("items", [])))
        issues = result.get("exceptions", result.get("issues", []))
        return list(records or []), list(issues or [])
    for attribute in ("reports", "records", "items"):
        if hasattr(result, attribute):
            records = getattr(result, attribute)
            issues = getattr(result, "exceptions", getattr(result, "issues", ()))
            return list(records or []), list(issues or [])
    if (
        isinstance(result, tuple)
        and len(result) == 2
        and isinstance(result[0], (list, tuple))
        and isinstance(result[1], (list, tuple))
    ):
        return list(result[0]), list(result[1])
    if isinstance(result, (str, bytes)):
        return [], [
            _issue(
                "INVALID_ADAPTER_RESULT",
                "Adapter returned text instead of records.",
                stage="ingestion",
                severity="error",
            )
        ]
    try:
        return list(result), []
    except TypeError:
        return [result], []


def load_factor_research_rows(path: Path | str) -> list[dict[str, Any]]:
    """Load auditable Walk-forward rows from JSON, JSONL, or CSV."""

    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = REPO_ROOT / resolved
    if not resolved.is_file():
        raise ConfigurationError(f"Factor research input does not exist: {resolved}")
    suffix = resolved.suffix.lower()
    try:
        if suffix == ".json":
            payload = json.loads(resolved.read_text(encoding="utf-8"))
            if isinstance(payload, Mapping):
                payload = payload.get("rows", payload.get("observations", []))
            rows = list(payload) if isinstance(payload, list) else []
        elif suffix in {".jsonl", ".ndjson"}:
            rows = [
                json.loads(line)
                for line in resolved.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        elif suffix == ".csv":
            with resolved.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        else:
            raise ConfigurationError("Factor research input must be JSON, JSONL/NDJSON, or CSV")
    except (OSError, json.JSONDecodeError, csv.Error) as exc:
        raise ConfigurationError(f"Cannot parse factor research input {resolved}: {exc}") from exc
    if not all(isinstance(row, Mapping) for row in rows):
        raise ConfigurationError("Every factor research row must be an object/record")
    return [dict(row) for row in rows]


def load_trading_calendar(path: Path | str) -> tuple[date, ...]:
    """Load an explicit exchange calendar; never fabricate weekdays."""

    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = REPO_ROOT / resolved
    if not resolved.is_file():
        raise ConfigurationError(f"Trading calendar does not exist: {resolved}")
    suffix = resolved.suffix.lower()
    try:
        if suffix == ".json":
            payload = json.loads(resolved.read_text(encoding="utf-8-sig"))
            if isinstance(payload, Mapping):
                keys = [key for key in ("trading_calendar", "dates") if key in payload]
                if len(keys) != 1 or set(payload) != {keys[0]}:
                    raise ConfigurationError(
                        "Trading-calendar JSON object must contain only trading_calendar or dates"
                    )
                values = payload[keys[0]]
            else:
                values = payload
            if not isinstance(values, list):
                raise ConfigurationError("Trading-calendar JSON must contain an array")
        elif suffix in {".jsonl", ".ndjson"}:
            values = [
                json.loads(line)
                for line in resolved.read_text(encoding="utf-8-sig").splitlines()
                if line.strip()
            ]
        elif suffix == ".csv":
            with resolved.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle, strict=True)
                headers = reader.fieldnames or []
                if headers not in (["trade_date"], ["date"]):
                    raise ConfigurationError(
                        "Trading-calendar CSV must have exactly one trade_date or date column"
                    )
                values = list(reader)
        else:
            raise ConfigurationError(
                "Trading calendar must be JSON, JSONL/NDJSON, or CSV"
            )
    except (OSError, UnicodeError, json.JSONDecodeError, csv.Error) as exc:
        raise ConfigurationError(f"Cannot parse trading calendar {resolved}: {exc}") from exc
    if not values:
        raise ConfigurationError("Trading calendar must not be empty")
    parsed: list[date] = []
    for index, value in enumerate(values, start=1):
        raw = value
        if isinstance(value, Mapping):
            available = [key for key in ("trade_date", "date") if key in value]
            if len(available) != 1 or set(value) != {available[0]}:
                raise ConfigurationError(
                    f"Trading-calendar row {index} must contain exactly trade_date or date"
                )
            raw = value[available[0]]
        if isinstance(raw, datetime) or not isinstance(raw, (str, date)):
            raise ConfigurationError(
                f"Trading-calendar row {index} must be an ISO date"
            )
        parsed.append(_date_value(raw, f"trading_calendar[{index}]"))
    if len(set(parsed)) != len(parsed):
        raise ConfigurationError("Trading calendar contains duplicate dates")
    return tuple(sorted(parsed))


def _configured_trading_calendar(
    config: Mapping[str, Any],
    *,
    config_path: Path,
    issues: list[dict[str, Any]],
) -> tuple[date, ...]:
    research = config.get("factor_research", {})
    raw_path = (
        str(research.get("trading_calendar_path") or "").strip()
        if isinstance(research, Mapping)
        else ""
    )
    if not raw_path:
        issues.append(
            _issue(
                "EXCHANGE_CALENDAR_NOT_CONFIGURED",
                "未配置真实交易所日历；日期级研报仅保留覆盖，不能用工作日近似进入正式评分。",
                stage="availability",
            )
        )
        return ()
    resolved = Path(raw_path)
    if not resolved.is_absolute():
        resolved = config_path.parent / resolved
    try:
        return load_trading_calendar(resolved)
    except ConfigurationError as exc:
        issues.append(
            _issue(
                "EXCHANGE_CALENDAR_LOAD_FAILED",
                "真实交易所日历无法读取；日期级研报保持不可正式评分。",
                stage="availability",
                severity="error",
                details={"path": str(resolved), "error": str(exc)},
            )
        )
        return ()


def _normalise_adapter_issues(values: Iterable[Any], *, stage: str, dimension: str = "") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for value in values:
        if isinstance(value, Mapping):
            rows.append(dict(value))
        else:
            rows.append(
                _issue(
                    "ADAPTER_WARNING",
                    str(value),
                    stage=stage,
                    dimension=dimension,
                )
            )
    return rows


def _ingest_online(
    store: Any,
    *,
    config: Mapping[str, Any],
    dimensions: Sequence[str],
    start_date: date,
    end_date: date,
    decision: datetime,
    cache_directory: Path,
    issues: list[dict[str, Any]],
    collection_scope: str = "audit_sample",
    trading_calendar: Sequence[date] = (),
) -> None:
    """Fetch public report metadata through the optional cached source adapter."""

    try:
        from .sources import (
            EASTMONEY_IPV4_ONLY_HOSTS,
            CachedHttpClient,
            EastmoneySource,
        )
        from .storage import HttpCache
    except (ImportError, AttributeError) as exc:
        issues.append(
            _issue(
                "INGESTION_MODULE_UNAVAILABLE",
                "Public-source adapter is unavailable; existing store data only.",
                stage="ingestion",
                severity="error",
                details={"error_type": type(exc).__name__, "error": str(exc)},
            )
        )
        return

    cache: Any = None
    client: Any = None
    try:
        cache = HttpCache(cache_directory)
        client = _construct_supported(
            CachedHttpClient,
            cache=cache,
            offline=False,
            as_of=decision,
            user_agent="broker-report-audit-v1/research-only",
            ipv4_only_hosts=EASTMONEY_IPV4_ONLY_HOSTS,
            rate_limit_seconds=2.0,
            max_retries=1,
        )
        source = _construct_supported(
            EastmoneySource,
            client=client,
            config=config,
            broker_aliases=config.get("broker_aliases", {}),
            trading_calendar=trading_calendar,
        )
        fetch = getattr(source, "fetch_reports", None) or getattr(source, "iter_reports", None)
        if fetch is None:
            raise AttributeError("EastmoneySource has no fetch_reports/iter_reports")
        for dimension in dimensions:
            try:
                result = _invoke_supported(
                    fetch,
                    dimension=dimension,
                    start_date=start_date,
                    end_date=end_date,
                    as_of=decision,
                )
                reports, source_issues = _unpack_records(result)
                issues.extend(
                    _normalise_adapter_issues(source_issues, stage="ingestion", dimension=dimension)
                )
                if reports:
                    scoped_reports: list[Any] = []
                    for report in reports:
                        metadata = _field(report, "metadata", default={})
                        tagged_metadata = (
                            dict(metadata) if isinstance(metadata, Mapping) else {}
                        )
                        tagged_metadata["collection_scope"] = collection_scope
                        if is_dataclass(report):
                            scoped_reports.append(
                                replace(report, metadata=tagged_metadata)
                            )
                        elif isinstance(report, Mapping):
                            scoped_reports.append(
                                {**dict(report), "metadata": tagged_metadata}
                            )
                        else:
                            scoped_reports.append(report)
                    store.upsert_reports(scoped_reports)
            except Exception as exc:
                issues.append(
                    _issue(
                        "SOURCE_FETCH_FAILED",
                        "该维度公开研报采集失败；保留缓存/数据库已有记录，不补造数据。",
                        stage="ingestion",
                        dimension=dimension,
                        severity="error",
                        details={"error_type": type(exc).__name__, "error": str(exc)},
                    )
                )
    finally:
        if client is not None and hasattr(client, "close"):
            try:
                client.close()
            except Exception:
                pass
        if cache is not None and hasattr(cache, "close"):
            try:
                cache.close()
            except Exception:
                pass


def _report_text(report: Any) -> str | None:
    metadata = _field(report, "metadata", default={})
    if not isinstance(metadata, Mapping):
        return None
    for key in ("pdf_text", "full_text", "text", "content", "summary", "abstract"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _deduplicate_reports(reports: Iterable[Any], issues: list[dict[str, Any]]) -> list[Any]:
    records = list(reports)
    try:
        from .evaluation import deduplicate_reports

        return list(deduplicate_reports(records))
    except Exception as exc:
        issues.append(
            _issue(
                "REPORT_DEDUPLICATION_FAILED",
                "Episode 去重失败；为避免重复加权，本次不新增抽取结果。",
                stage="deduplication",
                severity="error",
                details={"error_type": type(exc).__name__, "error": str(exc)},
            )
        )
        return []


def _active_extraction_versions() -> tuple[str, str, str]:
    """Return the exact extractor/PDF-parser/prompt contract used this run."""

    from .extractors import EXTRACTOR_VERSION

    try:
        import pypdf
    except ImportError:
        parser_version = ""
    else:
        parser_version = f"pypdf-{pypdf.__version__}+{EXTRACTOR_VERSION}"
    return EXTRACTOR_VERSION, parser_version, "none"


def _active_extractor_bundle_sha256() -> str:
    """Hash the executable rule/lexicon source, not just a manual version label."""

    from .extractors import extractor_bundle_sha256

    return extractor_bundle_sha256()


def _extract_claims(
    store: Any,
    *,
    reports: Iterable[Any],
    existing_claims: Iterable[Any],
    config: Mapping[str, Any],
    issues: list[dict[str, Any]],
    text_by_report_id: Mapping[str, str] | None = None,
    retry_completed: bool = False,
    emit_unscorable: bool = True,
) -> set[str]:
    try:
        from .extractors import RuleBasedExtractor
    except (ImportError, AttributeError) as exc:
        issues.append(
            _issue(
                "EXTRACTOR_MODULE_UNAVAILABLE",
                "规则抽取器不可用；不从标题推断预测。",
                stage="extraction",
                severity="error",
                details={"error_type": type(exc).__name__, "error": str(exc)},
            )
        )
        return set()
    _extractor_version, parser_version, prompt_version = _active_extraction_versions()
    extractor = _construct_supported(
        RuleBasedExtractor,
        config=config,
        horizons=config.get("horizons", {}),
        parser_version=parser_version,
        prompt_version=prompt_version,
    )
    # Extraction is deterministic and claim storage is immutable/idempotent.
    # Re-run every current report so a changed executable rule bundle can never
    # borrow an old report-level "completed" flag without re-extracting.
    del existing_claims, retry_completed
    successful: set[str] = set()
    for report in reports:
        report_id = _identifier(report, "report_id", "id")
        try:
            text = (
                text_by_report_id.get(report_id)
                if text_by_report_id is not None and report_id in text_by_report_id
                else _report_text(report)
            )
            result = _invoke_supported(extractor.extract, report=report, text=text)
            claims, extraction_issues = _unpack_records(result)
            issues.extend(
                _normalise_adapter_issues(
                    extraction_issues,
                    stage="extraction",
                    dimension=_identifier(report, "dimension"),
                )
            )
            if claims:
                store.upsert_claims(claims)
                successful.add(report_id)
            elif emit_unscorable:
                issues.append(
                    _issue(
                        "UNSCORABLE_REPORT",
                        "未抽取到同时具备变量、方向和期限的可证伪预测。",
                        stage="extraction",
                        dimension=_identifier(report, "dimension"),
                        report_id=report_id,
                    )
                )
        except Exception as exc:
            issues.append(
                _issue(
                    "REPORT_EXTRACTION_FAILED",
                    "该报告规则抽取失败；不进入正式评分。",
                    stage="extraction",
                    dimension=_identifier(report, "dimension"),
                    report_id=report_id,
                    severity="error",
                    details={"error_type": type(exc).__name__, "error": str(exc)},
                )
            )
    return successful


def _extract_pdf_texts(
    reports: Iterable[Any],
    *,
    store: Any | None = None,
    cache_directory: Path,
    offline: bool,
    decision: datetime,
    issues: list[dict[str, Any]],
) -> dict[str, str]:
    """Fetch/cache PDFs and extract local text without exposing PDF payloads."""

    report_rows = list(reports)
    if not report_rows:
        return {}
    try:
        import pypdf

        from .extractors import EXTRACTOR_VERSION
        from .sources import (
            EASTMONEY_IPV4_ONLY_HOSTS,
            CachedHttpClient,
            EastmoneySource,
            OfflineCacheMiss,
        )
        from .storage import ExtractionCache, HttpCache
    except (ImportError, AttributeError) as exc:
        for report in report_rows:
            issues.append(
                _issue(
                    "PDF_EXTRACTION_FAILED",
                    "本地 PDF 解析依赖不可用；未从标题推断缺失预测。",
                    stage="pdf_extraction",
                    dimension=_identifier(report, "dimension"),
                    report_id=_identifier(report, "report_id", "id"),
                    severity="error",
                    details={"error_type": type(exc).__name__, "error": str(exc)},
                )
            )
        return {}

    parser_version = f"pypdf-{pypdf.__version__}+{EXTRACTOR_VERSION}"
    prompt_version = "none"
    texts: dict[str, str] = {}
    http_cache: Any = None
    extraction_cache: Any = None
    try:
        http_cache = HttpCache(cache_directory)
        extraction_cache = ExtractionCache(cache_directory / "extractions")
        client = CachedHttpClient(
            http_cache,
            offline=offline,
            as_of=decision,
            user_agent="broker-report-audit-v1/research-only",
            ipv4_only_hosts=EASTMONEY_IPV4_ONLY_HOSTS,
            rate_limit_seconds=2.0,
            max_retries=1,
        )
        source = EastmoneySource(client)
        for report in report_rows:
            report_id = _identifier(report, "report_id", "id")
            try:
                response = source.fetch_pdf(report)
                pdf_hash = sha256(response.body).hexdigest()
                if pdf_hash != response.content_hash:
                    raise ValueError("PDF response content hash mismatch")
                cached = extraction_cache.get(pdf_hash, parser_version, prompt_version)
                if cached is not None:
                    text = cached.payload.decode("utf-8")
                else:
                    reader = pypdf.PdfReader(BytesIO(response.body), strict=False)
                    if reader.is_encrypted:
                        reader.decrypt("")
                    text = "\n\n".join((page.extract_text() or "").strip() for page in reader.pages).strip()
                    if not text:
                        raise ValueError("PDF contains no extractable text")
                    extraction_cache.put(
                        pdf_hash,
                        parser_version,
                        prompt_version,
                        text,
                        created_at=response.fetched_at,
                    )
                if not text.strip():
                    raise ValueError("cached PDF extraction is empty")
                if store is not None:
                    if not is_dataclass(report):
                        raise TypeError("PDF provenance requires a dataclass ResearchReport")
                    # The PDF-enriched report is a new captured version.  Its
                    # provenance cannot pre-date the HTTP response that supplied
                    # the bytes, otherwise an historical replay could see a PDF
                    # that was only downloaded in the future.
                    report_fetched_at = _field(report, "fetched_at")
                    store.upsert_report(
                        replace(
                            report,
                            pdf_sha256=pdf_hash,
                            fetched_at=max(report_fetched_at, response.fetched_at),
                        )
                    )
                texts[report_id] = text
            except OfflineCacheMiss as exc:
                issues.append(
                    _issue(
                        "PDF_EXTRACTION_NOT_RUN",
                        "离线缓存中没有该 PDF；未运行正文抽取。",
                        stage="pdf_extraction",
                        dimension=_identifier(report, "dimension"),
                        report_id=report_id,
                        details={"error": str(exc)},
                    )
                )
            except Exception as exc:
                issues.append(
                    _issue(
                        "PDF_EXTRACTION_FAILED",
                        "PDF 下载、校验或本地文本抽取失败；该报告不进入正式评分。",
                        stage="pdf_extraction",
                        dimension=_identifier(report, "dimension"),
                        report_id=report_id,
                        severity="error",
                        details={"error_type": type(exc).__name__, "error": str(exc)},
                    )
                )
    finally:
        if extraction_cache is not None:
            extraction_cache.close()
        if http_cache is not None:
            http_cache.close()
    return texts


def _report_has_resolvable_pdf(report: Any) -> bool:
    if _identifier(report, "pdf_url").strip():
        return True
    return (
        _identifier(report, "source").strip().lower().startswith("eastmoney")
        and bool(_identifier(report, "source_url").strip())
    )


def _candidate_claims(claims: Iterable[Any], config: Mapping[str, Any]) -> list[Any]:
    acceptance = config.get("acceptance", {})
    threshold = float(acceptance.get("minimum_extraction_precision", 0.95))
    return [
        claim
        for claim in claims
        if float(_field(claim, "extraction_confidence", default=0.0) or 0.0) >= threshold
        and _field(claim, "target_type")
        and _field(claim, "forecast_period")
        and int(_field(claim, "horizon_days", default=0) or 0) > 0
    ]


def _extractor_dimension_admitted(config: Mapping[str, Any], dimension: str) -> bool:
    acceptance = config.get("acceptance", {})
    validation = acceptance.get("extractor_validation", {}) if isinstance(acceptance, Mapping) else {}
    row = validation.get(dimension, {}) if isinstance(validation, Mapping) else {}
    if not isinstance(row, Mapping):
        return False
    try:
        sample_count = int(row.get("sample_count") or 0)
        precision = float(row["field_precision"]) if row.get("field_precision") is not None else None
        metadata_match = float(row["metadata_match_rate"]) if row.get("metadata_match_rate") is not None else None
    except (TypeError, ValueError):
        return False
    minimum_samples = int(acceptance.get("manual_metadata_samples_per_dimension", 30))
    minimum_precision = float(acceptance.get("minimum_extraction_precision", 0.95))
    expected_manifest_hash = str(
        acceptance.get("validation_manifest_sha256") or ""
    ).strip().lower()
    try:
        from .validation import VALIDATION_CONTRACT_VERSION

        extractor_version, parser_version, prompt_version = _active_extraction_versions()
        extractor_bundle_sha256 = _active_extractor_bundle_sha256()
    except (ImportError, AttributeError):
        return False
    return (
        bool(expected_manifest_hash)
        and str(row.get("manifest_sha256") or "").strip().lower()
        == expected_manifest_hash
        and row.get("validation_contract_version") == VALIDATION_CONTRACT_VERSION
        and row.get("extractor_version") == extractor_version
        and row.get("extractor_bundle_sha256") == extractor_bundle_sha256
        and row.get("parser_version") == parser_version
        and row.get("prompt_version") == prompt_version
        and row.get("passed") is True
        and sample_count >= minimum_samples
        and precision is not None
        and precision >= minimum_precision
        and metadata_match is not None
        and metadata_match >= 1.0
    )


def _failed_extractor_validation_state(reason: str) -> dict[str, dict[str, Any]]:
    return {
        dimension: {
            "sample_count": 0,
            "metadata_match_rate": None,
            "field_precision": None,
            "passed": False,
            "manifest_status": reason,
        }
        for dimension in VALID_DIMENSIONS
    }


def _hydrate_extractor_validation(
    config: dict[str, Any],
    *,
    decision: datetime,
    config_path: Path,
    population_reports: Iterable[Any],
    population_claims: Iterable[Any],
    issues: list[dict[str, Any]],
) -> None:
    acceptance = config.get("acceptance", {})
    if not isinstance(acceptance, dict):
        raise ConfigurationError("acceptance must be an object")
    manifest_value = str(acceptance.get("validation_manifest_path") or "").strip()
    expected_hash = str(
        acceptance.get("validation_manifest_sha256") or ""
    ).strip().lower()
    if not manifest_value or not expected_hash:
        acceptance["extractor_validation"] = _failed_extractor_validation_state(
            "manifest_missing"
        )
        issues.append(
            _issue(
                "EXTRACTOR_VALIDATION_MANIFEST_MISSING",
                "未配置逐条人工验证manifest及其外部SHA256锚点；静态passed汇总不能解锁正式评分。",
                stage="extraction_validation",
                severity="error",
            )
        )
        return
    manifest_path = Path(manifest_value)
    if not manifest_path.is_absolute():
        manifest_path = config_path.parent / manifest_path
    try:
        import pypdf

        from .extractors import EXTRACTOR_VERSION
        from .validation import load_validation_manifest

        parser_version = f"pypdf-{pypdf.__version__}+{EXTRACTOR_VERSION}"
        validation = load_validation_manifest(
            manifest_path,
            expected_sha256=expected_hash,
            as_of=decision,
            population_reports=list(population_reports),
            population_claims=list(population_claims),
            expected_extractor_version=EXTRACTOR_VERSION,
            expected_extractor_bundle_sha256=_active_extractor_bundle_sha256(),
            expected_parser_version=parser_version,
            expected_prompt_version="none",
            minimum_samples_per_dimension=int(
                acceptance.get("manual_metadata_samples_per_dimension", 30)
            ),
            minimum_field_precision=float(
                acceptance.get("minimum_extraction_precision", 0.95)
            ),
        )
        acceptance["extractor_validation"] = validation
    except Exception as exc:
        acceptance["extractor_validation"] = _failed_extractor_validation_state(
            "manifest_invalid"
        )
        issues.append(
            _issue(
                "EXTRACTOR_VALIDATION_MANIFEST_INVALID",
                "逐条人工验证manifest缺失、哈希不匹配、证据不完整或晚于研究时点；三维全部fail closed。",
                stage="extraction_validation",
                severity="error",
                details={
                    "path": str(manifest_path),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
        )


def _claims_with_current_evidence(
    claims: Iterable[Any],
    *,
    reports: Iterable[Any] | Mapping[str, Any] = (),
    extractor_version: str | None = None,
    parser_version: str | None = None,
    prompt_version: str | None = None,
) -> list[Any]:
    report_values = (
        list(reports.values()) if isinstance(reports, Mapping) else list(reports)
    )
    report_by_id = {
        _identifier(report, "report_id", "id"): report for report in report_values
    }
    try:
        from .validation import validate_claim_evidence_bindings
    except (ImportError, AttributeError):
        return []

    active_extractor, active_parser, active_prompt = _active_extraction_versions()
    expected_extractor = str(extractor_version or active_extractor)
    expected_parser = str(parser_version or active_parser)
    expected_prompt = str(prompt_version or active_prompt)

    bound: list[Any] = []
    for claim in claims:
        report = report_by_id.get(_identifier(claim, "report_id"))
        if report is None:
            continue
        timestamp_quality = str(
            _field(report, "timestamp_quality", default="") or ""
        ).strip().lower()
        if timestamp_quality in {
            "date_only_calendar_unverified",
            "date_only_local_calendar_unverified",
            "date_only_next_weekday_open",
        }:
            # A weekday approximation is useful for coverage diagnostics only.
            # It cannot establish the executable timestamp around exchange
            # holidays, so fail closed before outcomes, skill or factors.
            continue
        try:
            validate_claim_evidence_bindings(
                [claim],
                [report],
                expected_extractor_version=expected_extractor,
                expected_extractor_bundle_sha256=(
                    _active_extractor_bundle_sha256()
                ),
                expected_parser_version=expected_parser,
                expected_prompt_version=expected_prompt,
            )
        except Exception:
            continue
        bound.append(claim)
    return bound


def _eligible_claims(
    claims: Iterable[Any],
    config: Mapping[str, Any],
    *,
    reports: Iterable[Any] | Mapping[str, Any] = (),
) -> list[Any]:
    """Return only claims bound to the currently validated source versions.

    A dimension-level pass is necessary but not sufficient: every individual
    claim must still match the current report/PDF hash and the exact approved
    extractor, parser and prompt versions.  Legacy or superseded claims remain
    readable for diagnostics but cannot update outcomes, skills or factors.
    """

    candidates = _candidate_claims(claims, config)
    bound = _claims_with_current_evidence(candidates, reports=reports)
    return [
        claim
        for claim in bound
        if _extractor_dimension_admitted(config, _identifier(claim, "dimension"))
    ]


def _append_extractor_validation_issues(
    config: Mapping[str, Any],
    dimensions: Sequence[str],
    issues: list[dict[str, Any]],
) -> None:
    validation = config.get("acceptance", {}).get("extractor_validation", {})
    for dimension in dimensions:
        if _extractor_dimension_admitted(config, dimension):
            continue
        row = validation.get(dimension, {}) if isinstance(validation, Mapping) else {}
        issues.append(
            _issue(
                "EXTRACTOR_VALIDATION_NOT_PASSED",
                "该维度规则抽取器尚未通过至少30份人工抽查、元数据100%一致和字段精确率95%的准入门槛；预测仅披露，不进入正式评分、技能或因子。",
                stage="extraction_validation",
                dimension=dimension,
                details=dict(row) if isinstance(row, Mapping) else {},
            )
        )


def _market_source_config(config: Mapping[str, Any]) -> Mapping[str, Any]:
    sources = config.get("sources", {})
    if not isinstance(sources, Mapping):
        return {}
    market = sources.get("market", {})
    return market if isinstance(market, Mapping) else {}


def _official_truth_source_allowlist(
    config: Mapping[str, Any],
    dimension: str | None = None,
) -> set[str]:
    sources = config.get("sources", {})
    if not isinstance(sources, Mapping):
        return set()
    section_names = (
        ({"macro": "macro_truth", "industry": "industry_truth", "stock": "stock_truth"}[dimension],)
        if dimension in VALID_DIMENSIONS
        else ("macro_truth", "industry_truth", "stock_truth")
    )
    identifiers: set[str] = set()
    for section_name in section_names:
        section = sources.get(section_name, [])
        for item in section if isinstance(section, list) else []:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("id") or "").strip():
                identifiers.add(str(item["id"]).strip())
            aliases = item.get("truth_source_aliases", [])
            if isinstance(aliases, list):
                identifiers.update(
                    str(alias).strip() for alias in aliases if str(alias).strip()
                )
    return identifiers


def _official_truth_source_domains(config: Mapping[str, Any]) -> dict[str, set[str]]:
    """Bind every configured truth-source alias to its declared official host."""

    from urllib.parse import urlsplit

    sources = config.get("sources", {})
    if not isinstance(sources, Mapping):
        return {}
    result: dict[str, set[str]] = {}
    for section_name in ("macro_truth", "industry_truth", "stock_truth"):
        section = sources.get(section_name, [])
        for item in section if isinstance(section, list) else []:
            if not isinstance(item, Mapping):
                continue
            hostname = (urlsplit(str(item.get("url") or "")).hostname or "").lower()
            if not hostname:
                continue
            names = [str(item.get("id") or "").strip()]
            aliases = item.get("truth_source_aliases", [])
            if isinstance(aliases, list):
                names.extend(str(value).strip() for value in aliases)
            for name in names:
                if name:
                    result.setdefault(name, set()).add(hostname)
    return result


def _resolve_truth_input_paths(
    config: Mapping[str, Any],
    supplied: Sequence[Path | str] | Path | str | None,
) -> tuple[Path, ...]:
    sources = config.get("sources", {})
    configured = sources.get("truth_input_paths", []) if isinstance(sources, Mapping) else []
    values: list[Path | str] = []
    if isinstance(configured, Sequence) and not isinstance(configured, (str, bytes)):
        values.extend(configured)
    elif configured:
        raise ConfigurationError("sources.truth_input_paths must be an array")
    if supplied is not None:
        if isinstance(supplied, (str, Path)):
            values.append(supplied)
        else:
            values.extend(supplied)
    resolved: list[Path] = []
    seen: set[str] = set()
    for value in values:
        path = Path(value)
        if not path.is_absolute():
            path = REPO_ROOT / path
        canonical = str(path.resolve(strict=False)).casefold()
        if canonical not in seen:
            resolved.append(path.resolve(strict=False))
            seen.add(canonical)
    return tuple(resolved)


def _import_truth_inputs(
    store: Any,
    *,
    config: Mapping[str, Any],
    paths: Sequence[Path],
    issues: list[dict[str, Any]],
) -> None:
    if not paths:
        return
    try:
        from .sources import import_truth_observations
    except (ImportError, AttributeError) as exc:
        issues.append(
            _issue(
                "TRUTH_IMPORT_MODULE_UNAVAILABLE",
                "官方真值导入模块不可用；本次不导入任何本地真值。",
                stage="truth_import",
                severity="error",
                details={"error_type": type(exc).__name__, "error": str(exc)},
            )
        )
        return
    insert = getattr(store, "insert_truth_observations", None)
    if insert is None:
        issues.append(
            _issue(
                "TRUTH_STORE_INTERFACE_MISSING",
                "AuditStore 缺少 append-only 真值写入接口；本次不导入真值。",
                stage="truth_import",
                severity="error",
            )
        )
        return
    allowlist = _official_truth_source_allowlist(config)
    for path in paths:
        try:
            observations = tuple(
                import_truth_observations(
                    path,
                    official_source_allowlist=allowlist,
                    official_source_domains=_official_truth_source_domains(config),
                )
            )
            mismatches = [
                observation
                for observation in observations
                if _identifier(observation, "dimension")
                and _identifier(observation, "truth_source").upper()
                not in {
                    value.upper()
                    for value in _official_truth_source_allowlist(
                        config, _identifier(observation, "dimension")
                    )
                }
            ]
            if mismatches:
                issues.append(
                    _issue(
                        "TRUTH_INPUT_SOURCE_DIMENSION_MISMATCH",
                        "真值文件中的来源不属于该维度配置的官方白名单；整份文件拒绝导入。",
                        stage="truth_import",
                        severity="error",
                        details={
                            "path": str(path),
                            "observation_ids": [
                                _identifier(item, "observation_id")
                                for item in mismatches
                            ],
                        },
                    )
                )
                continue
            inserted = int(insert(observations) or 0)
            revision_count = sum(
                1
                for observation in observations
                if _field(observation, "revision") is True
                or _field(observation, "first_release") is False
            )
            if revision_count:
                issues.append(
                    _issue(
                        "TRUTH_REVISIONS_STORED_BUT_EXCLUDED",
                        "修订值已作为附表证据 append-only 保留，但不会进入正式评价。",
                        stage="truth_import",
                        severity="info",
                        details={"path": str(path), "count": revision_count},
                    )
                )
            replayed = len(observations) - inserted
            issues.append(
                _issue(
                    "TRUTH_INPUT_IMPORTED_DIAGNOSTIC_ONLY",
                    "本地真值及证据字节已按不可变版本导入，但无法证明其确为官方URL响应；仅作诊断，不进入正式评分。",
                    stage="truth_import",
                    severity="info",
                    details={
                        "path": str(path),
                        "observations": len(observations),
                        "inserted": inserted,
                        "replayed": replayed,
                        "first_releases": len(observations) - revision_count,
                    },
                )
            )
        except Exception as exc:
            issues.append(
                _issue(
                    "TRUTH_INPUT_REJECTED",
                    "官方真值文件校验或 append-only 导入失败；该文件未进入本次评价。",
                    stage="truth_import",
                    severity="error",
                    details={
                        "path": str(path),
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
            )


def _market_issue_once(
    issues: list[dict[str, Any]],
    code: str,
    message: str,
    *,
    claim: Any,
    report: Any | None,
    details: Mapping[str, Any] | None = None,
    stage: str = "market_data",
) -> None:
    report_id = _identifier(claim, "report_id")
    key = (code, report_id, _identifier(claim, "dimension"))
    if any(
        (item.get("code"), item.get("report_id"), item.get("dimension")) == key
        for item in issues
    ):
        return
    issues.append(
        _issue(
            code,
            message,
            stage=stage,
            dimension=_identifier(claim, "dimension"),
            report_id=report_id or _identifier(report, "report_id", "id"),
            claim_id=_identifier(claim, "claim_id"),
            details=dict(details or {}),
        )
    )


def _mapped_industry_instrument(
    report: Any | None,
    claim: Any,
    config: Mapping[str, Any],
) -> tuple[str, str]:
    """Resolve an explicitly configured industry index/board instrument.

    Eastmoney ``BK`` board identifiers are already executable identifiers for
    ``EastmoneyMarketSource``.  Other report-industry identifiers are not
    guessed: a six-digit industry taxonomy code could otherwise be silently
    misread as a stock or exchange index.
    """

    del config  # config strings are not point-in-time mapping evidence
    candidates = (
        _identifier(report, "industry_id"),
        _identifier(report, "subject_id")
        if _identifier(report, "dimension") == "industry"
        else "",
    )
    for candidate in candidates:
        raw = candidate.strip()
        if not raw:
            continue
        if raw.upper().startswith("BK"):
            return raw.upper(), raw
    return "", next((value.strip() for value in candidates if value.strip()), "")


def _explicit_market_instrument(value: str, config: Mapping[str, Any]) -> str:
    del config  # aliases in a mutable config are not evidence-bound mappings
    raw = value.strip()
    if not raw:
        return ""
    lowered = raw.casefold()
    if lowered in {
        "annual_report_basic_eps",
        "cninfo_annual_report",
        "official_first_release",
    }:
        return ""
    upper = raw.upper()
    if upper.startswith("BK") or upper.startswith(("SH", "SZ", "BJ")):
        return upper
    if "." in upper or (len(upper) == 6 and upper.isdigit()) or upper.startswith(("0.", "1.", "2.", "90.")):
        return upper
    return ""


def _market_route_for_claim(
    claim: Any,
    report: Any | None,
    config: Mapping[str, Any],
    issues: list[dict[str, Any]],
) -> tuple[str, str, str, str]:
    """Return subject, formal benchmark, benchmark kind and auxiliary benchmark."""

    dimension = _identifier(claim, "dimension").strip().lower()
    target_type = _identifier(claim, "target_type").strip().lower()
    subject_id = _identifier(claim, "subject_id").strip()
    market = _market_source_config(config)
    csi300 = CANONICAL_CSI300_INSTRUMENT_ID
    explicit_benchmark = _explicit_market_instrument(
        _identifier(claim, "benchmark"), config
    )

    if dimension == "stock":
        if not subject_id:
            _market_issue_once(
                issues,
                "STOCK_MARKET_MAPPING_MISSING",
                "个股预测缺少可执行证券代码，未抓取主体行情。",
                claim=claim,
                report=report,
            )
            return "", "", "", ""
        industry_instrument, industry_key = _mapped_industry_instrument(
            report, claim, config
        )
        if industry_instrument:
            return subject_id, industry_instrument, "industry", ""
        _market_issue_once(
            issues,
            "STOCK_INDUSTRY_BENCHMARK_MISSING",
            "个股所属行业无法映射到可执行行业指数；沪深300仅作辅助行情，不生成正式个股market_hit或技能。",
            claim=claim,
            report=report,
            details={
                "industry_id": industry_key,
                "auxiliary_benchmark": csi300,
                "ignored_claim_benchmark": explicit_benchmark,
            },
        )
        return subject_id, "", "missing_industry", csi300

    if dimension == "industry":
        industry_instrument, industry_key = _mapped_industry_instrument(
            report, claim, config
        )
        if not industry_instrument:
            _market_issue_once(
                issues,
                "INDUSTRY_MARKET_MAPPING_MISSING",
                "行业对象无法映射到可执行行业指数/板块代码；未抓取主体行情，也未伪造行业收益。",
                claim=claim,
                report=report,
                details={"industry_id": industry_key},
            )
            return "", csi300, "csi300", ""
        return industry_instrument, csi300, "csi300", ""

    if dimension == "macro":
        # Only an instrument explicitly stated by the evidence-bound claim can
        # define the asset being forecast. A mutable config mapping is not
        # point-in-time market truth.
        if explicit_benchmark:
            return explicit_benchmark, "", "macro_asset", ""
        return "", "", "", ""

    return "", "", "", ""


def _market_window(claim: Any, decision: datetime) -> tuple[date, date]:
    available = _field(claim, "available_at")
    start_anchor = _date_value(available, "claim.available_at")
    horizon = max(1, int(_field(claim, "horizon_days", default=1) or 1))
    start = start_anchor - timedelta(days=7)
    calendar_span = (horizon * 7 + 4) // 5 + 21
    end = min(decision.date(), start_anchor + timedelta(days=calendar_span))
    return start, end


def _current_feed_start(config: Mapping[str, Any], decision: datetime) -> date:
    dimensions = config.get("dimensions", {})
    horizons = [
        int(item.get("main_horizon_trading_days", 0) or 0)
        for item in dimensions.values()
        if isinstance(item, Mapping)
    ] if isinstance(dimensions, Mapping) else []
    maximum = max(horizons or [120])
    calendar_span = (maximum * 7 + 4) // 5 + 21
    return decision.date() - timedelta(days=calendar_span)


def _collection_scope(report: Any) -> str:
    metadata = _field(report, "metadata", default={})
    return (
        str(metadata.get("collection_scope") or "").strip()
        if isinstance(metadata, Mapping)
        else ""
    )


def _ingest_market_bars(
    store: Any,
    *,
    reports: Iterable[Any],
    claims: Iterable[Any],
    config: Mapping[str, Any],
    decision: datetime,
    cache_directory: Path,
    offline: bool,
    issues: list[dict[str, Any]],
) -> None:
    """Cache subject and benchmark bars needed by admitted claims."""

    report_by_id = {
        _identifier(report, "report_id", "id"): report for report in reports
    }
    windows: dict[str, tuple[date, date]] = {}
    for claim in _eligible_claims(claims, config, reports=report_by_id):
        report = report_by_id.get(_identifier(claim, "report_id"))
        subject, benchmark, _benchmark_kind, auxiliary = _market_route_for_claim(
            claim, report, config, issues
        )
        start, end = _market_window(claim, decision)
        for instrument_id in (subject, benchmark, auxiliary):
            if not instrument_id:
                continue
            previous = windows.get(instrument_id)
            windows[instrument_id] = (
                min(previous[0], start) if previous else start,
                max(previous[1], end) if previous else end,
            )
    if offline or not windows:
        return

    try:
        from .sources import (
            EASTMONEY_IPV4_ONLY_HOSTS,
            CachedHttpClient,
            EastmoneyMarketSource,
        )
        from .storage import HttpCache
    except (ImportError, AttributeError) as exc:
        issues.append(
            _issue(
                "MARKET_SOURCE_MODULE_UNAVAILABLE",
                "东方财富行情适配器不可用；仅保留数据库已有行情。",
                stage="market_data",
                severity="error",
                details={"error_type": type(exc).__name__, "error": str(exc)},
            )
        )
        return

    cache: Any = None
    client: Any = None
    try:
        cache = HttpCache(cache_directory)
        client = _construct_supported(
            CachedHttpClient,
            cache=cache,
            offline=False,
            as_of=decision,
            user_agent="broker-report-audit-v1/research-only",
            ipv4_only_hosts=EASTMONEY_IPV4_ONLY_HOSTS,
            rate_limit_seconds=2.0,
            max_retries=1,
        )
        market_config = _market_source_config(config)
        provider = str(
            market_config.get("provider") or "eastmoney_public.push2his"
        ).strip()
        source_name = (
            provider[: -len(".push2his")]
            if provider.lower().endswith(".push2his")
            else provider
        )
        source = _construct_supported(
            EastmoneyMarketSource,
            client=client,
            source_name=source_name or "eastmoney_public",
        )
        for instrument_id, (start, end) in sorted(windows.items()):
            try:
                bars = list(
                    _invoke_supported(
                        source.daily_bars,
                        instrument_id=instrument_id,
                        start_date=start,
                        end_date=end,
                        as_of=decision,
                        adjust="qfq",
                        refresh=False,
                    )
                    or []
                )
                issues.extend(
                    _normalise_adapter_issues(
                        getattr(source, "last_issues", ()), stage="market_data"
                    )
                )
                if bars:
                    store.upsert_daily_bars(bars)
                else:
                    issues.append(
                        _issue(
                            "MARKET_BARS_EMPTY",
                            "行情源未返回可见交易日数据；该对象暂不评价市场结果。",
                            stage="market_data",
                            details={
                                "instrument_id": instrument_id,
                                "start_date": start.isoformat(),
                                "end_date": end.isoformat(),
                            },
                        )
                    )
            except Exception as exc:
                issues.append(
                    _issue(
                        "MARKET_BAR_FETCH_FAILED",
                        "东方财富主体或基准行情抓取失败；保留缓存/数据库已有行情，不补造价格。",
                        stage="market_data",
                        severity="error",
                        details={
                            "instrument_id": instrument_id,
                            "start_date": start.isoformat(),
                            "end_date": end.isoformat(),
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        },
                    )
                )
    finally:
        if client is not None and hasattr(client, "close"):
            try:
                client.close()
            except Exception:
                pass
        if cache is not None and hasattr(cache, "close"):
            try:
                cache.close()
            except Exception:
                pass


def _outcome_result_sufficient(
    claim: Any,
    outcome: Any | None,
    config: Mapping[str, Any],
) -> bool:
    """Return whether an existing row is terminal enough to skip reevaluation."""

    if outcome is None or _field(outcome, "mature") is not True:
        return False
    if _identifier(outcome, "exclusion_reason"):
        return False
    if _is_market_target(claim):
        if not _market_truth_is_trusted(outcome, config, claim):
            return False
        market_hit = _field(outcome, "market_hit")
        if market_hit is None and _is_market_truth_source(
            _identifier(outcome, "truth_source")
        ):
            market_hit = _field(outcome, "hit")
        return (
            market_hit is not None
            and bool(
                _identifier(outcome, "market_truth_source")
                or _is_market_truth_source(_identifier(outcome, "truth_source"))
            )
            and _field(outcome, "truth_available_at") is not None
            and (
                _field(outcome, "market_return") is not None
                or _field(outcome, "realized_value") is not None
            )
        )

    fundamental_complete = (
        _field(outcome, "realized_value") is not None
        and _field(outcome, "truth_available_at") is not None
        and bool(_identifier(outcome, "truth_source"))
        and _field(outcome, "fundamental_hit", "hit") is not None
    )
    if not fundamental_complete:
        return False
    if not _fundamental_truth_is_trusted(claim, outcome, config):
        return False
    if _identifier(claim, "dimension") == "macro":
        return True
    if (
        _field(outcome, "market_return") is not None
        and _field(outcome, "benchmark_return") is not None
    ):
        return True
    market_reason = _identifier(outcome, "market_exclusion_reason")
    transient_reasons = {
        "missing_market_bars",
        "missing_required_benchmark",
        "missing_industry_benchmark",
        "unmatured_market_horizon",
        "no_executable_market_bars",
        "future_market_data",
        "future_benchmark_data",
    }
    return bool(market_reason) and market_reason not in transient_reasons


def _truth_locator(record: Any) -> tuple[str, str, str, str]:
    return tuple(
        _identifier(record, name).strip().casefold()
        for name in ("dimension", "subject_id", "target_type", "forecast_period")
    )  # type: ignore[return-value]


def _match_first_release_truths(
    claims: Iterable[Any],
    observations: Iterable[Any],
    config: Mapping[str, Any],
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    """Bind official first releases, preferring an exact claim-id binding."""

    rows = [
        observation
        for observation in observations
        if _field(observation, "first_release") is True
        and _field(observation, "revision") is not True
    ]
    direct: dict[str, list[Any]] = {}
    locator: dict[tuple[str, str, str, str], list[Any]] = {}
    for observation in rows:
        bound_claim = _identifier(observation, "claim_id").strip()
        if bound_claim:
            direct.setdefault(bound_claim, []).append(observation)
        else:
            key = _truth_locator(observation)
            if all(key):
                locator.setdefault(key, []).append(observation)

    matched: dict[str, Any] = {}
    for claim in claims:
        claim_id = _identifier(claim, "claim_id")
        dimension = _identifier(claim, "dimension")
        allowed = {
            value.upper()
            for value in _official_truth_source_allowlist(config, dimension)
        }
        candidates = list(direct.get(claim_id, ()))
        binding = "claim_id"
        if not candidates:
            candidates = list(locator.get(_truth_locator(claim), ()))
            binding = "field_locator"
        valid: list[Any] = []
        for observation in candidates:
            if _field(observation, "evidence_verified") is not True:
                _market_issue_once(
                    issues,
                    "UNVERIFIED_TRUTH_EVIDENCE_EXCLUDED",
                    "真值未绑定本地证据字节哈希与配置的官方域名，已拒绝正式评分。",
                    claim=claim,
                    report=None,
                    details={
                        "observation_id": _identifier(
                            observation, "observation_id"
                        )
                    },
                    stage="truth",
                )
                continue
            source = _identifier(observation, "truth_source")
            if source.upper() not in allowed:
                _market_issue_once(
                    issues,
                    "UNTRUSTED_TRUTH_OBSERVATION_EXCLUDED",
                    "真值来源不属于该预测维度的官方白名单，已排除。",
                    claim=claim,
                    report=None,
                    details={
                        "truth_source": source,
                        "observation_id": _identifier(
                            observation, "observation_id"
                        ),
                    },
                    stage="truth",
                )
                continue
            observation_locator = _truth_locator(observation)
            if not all(observation_locator):
                _market_issue_once(
                    issues,
                    "TRUTH_IDENTITY_INCOMPLETE",
                    "真值缺少完整dimension/subject/target/forecast_period定位器；即使claim_id命中也拒绝正式评分。",
                    claim=claim,
                    report=None,
                    details={
                        "observation_id": _identifier(
                            observation, "observation_id"
                        )
                    },
                    stage="truth",
                )
                continue
            claim_available = _field(claim, "available_at")
            truth_available = _field(observation, "available_at")
            if (
                claim_available is None
                or truth_available is None
                or truth_available <= claim_available
            ):
                _market_issue_once(
                    issues,
                    "TRUTH_NOT_AFTER_CLAIM",
                    "真值首次发布时间不晚于预测可执行时间；该记录不是可评价的事前预测。",
                    claim=claim,
                    report=None,
                    details={
                        "observation_id": _identifier(
                            observation, "observation_id"
                        )
                    },
                    stage="truth",
                )
                continue
            if binding == "claim_id":
                if observation_locator != _truth_locator(claim):
                    _market_issue_once(
                        issues,
                        "TRUTH_CLAIM_LOCATOR_CONFLICT",
                        "真值 claim_id 与其字段定位器指向不同预测，已排除该真值。",
                        claim=claim,
                        report=None,
                        details={
                            "observation_id": _identifier(
                                observation, "observation_id"
                            )
                        },
                        stage="truth",
                    )
                    continue
            claim_unit = _identifier(claim, "unit").strip()
            claim_basis = _identifier(claim, "benchmark").strip()
            truth_unit = _identifier(observation, "unit").strip()
            truth_basis = _identifier(observation, "basis").strip()
            if (
                not claim_unit
                or not claim_basis
                or truth_unit.casefold() != claim_unit.casefold()
                or truth_basis.casefold() != claim_basis.casefold()
            ):
                _market_issue_once(
                    issues,
                    "TRUTH_UNIT_OR_BASIS_MISMATCH",
                    "真值单位或统计口径与预测合同不一致，已拒绝正式评分。",
                    claim=claim,
                    report=None,
                    details={
                        "claim_unit": claim_unit,
                        "truth_unit": truth_unit,
                        "claim_basis": claim_basis,
                        "truth_basis": truth_basis,
                        "observation_id": _identifier(
                            observation, "observation_id"
                        ),
                    },
                    stage="truth",
                )
                continue
            has_numeric_target = (
                _field(claim, "value_min") is not None
                or _field(claim, "value_max") is not None
            )
            if (
                int(_field(claim, "direction", default=0) or 0)
                and not has_numeric_target
                and (
                    _field(observation, "change_value") is None
                    or not _identifier(observation, "change_basis")
                )
            ):
                _market_issue_once(
                    issues,
                    "DIRECTIONAL_TRUTH_CHANGE_MISSING",
                    "方向型基本面预测缺少change_value/change_basis，已拒绝用水平值代替变化量评分。",
                    claim=claim,
                    report=None,
                    details={
                        "observation_id": _identifier(
                            observation, "observation_id"
                        )
                    },
                    stage="truth",
                )
                continue
            valid.append(observation)
        if not valid:
            continue
        realized_values = {
            _field(observation, "realized_value") for observation in valid
        }
        if len(realized_values) > 1:
            _market_issue_once(
                issues,
                "CONFLICTING_FIRST_RELEASE_TRUTH",
                "同一预测匹配到相互冲突的首次发布值；未选择任一结果进入正式评价。",
                claim=claim,
                report=None,
                details={
                    "binding": binding,
                    "observation_ids": sorted(
                        _identifier(item, "observation_id") for item in valid
                    ),
                },
                stage="truth",
            )
            continue
        selected = min(
            valid,
            key=lambda item: (
                _field(item, "available_at"),
                _identifier(item, "observation_id"),
            ),
        )
        matched[claim_id] = selected
        if len(valid) > 1:
            _market_issue_once(
                issues,
                "EQUIVALENT_FIRST_RELEASE_TRUTH_DEDUPLICATED",
                "同一预测匹配到多个等值首次发布记录；按最早可用时点确定性选取一条。",
                claim=claim,
                report=None,
                details={
                    "binding": binding,
                    "selected_observation_id": _identifier(
                        selected, "observation_id"
                    ),
                },
                stage="truth",
            )
    return matched


def _outcome_matches_truth(outcome: Any, observation: Any) -> bool:
    return (
        _identifier(outcome, "truth_source").casefold()
        == _identifier(observation, "truth_source").casefold()
        and _field(outcome, "truth_available_at")
        == _field(observation, "available_at", "truth_available_at")
        and _field(outcome, "realized_value")
        == _field(observation, "realized_value")
        and _identifier(outcome, "truth_unit").casefold()
        == _identifier(observation, "unit").casefold()
        and _identifier(outcome, "truth_basis").casefold()
        == _identifier(observation, "basis").casefold()
        and _field(outcome, "truth_change_value")
        == _field(observation, "change_value")
        and _identifier(outcome, "truth_change_basis").casefold()
        == _identifier(observation, "change_basis").casefold()
    )


def _evaluate_missing_outcomes(
    store: Any,
    *,
    reports: Iterable[Any],
    claims: Iterable[Any],
    outcomes: Iterable[Any],
    config: Mapping[str, Any],
    decision: datetime,
    issues: list[dict[str, Any]],
) -> None:
    try:
        from .evaluation import episode_deduplicate, evaluate_claims
    except (ImportError, AttributeError) as exc:
        issues.append(
            _issue(
                "EVALUATOR_MODULE_UNAVAILABLE",
                "评价模块不可用；已有真值保持不变。",
                stage="evaluation",
                severity="error",
                details={"error_type": type(exc).__name__, "error": str(exc)},
            )
        )
        return
    report_rows = list(reports)
    report_by_id = {
        _identifier(report, "report_id", "id"): report for report in report_rows
    }
    existing_by_id = {
        _identifier(outcome, "claim_id"): outcome
        for outcome in outcomes
        if _identifier(outcome, "claim_id")
    }
    eligible = list(
        episode_deduplicate(
            _eligible_claims(claims, config, reports=report_by_id),
            reports=report_by_id,
        )
    )
    first_releases = _iter_store(
        store,
        "iter_truth_observations",
        issues,
        decision_time=decision,
        first_release=True,
    )
    revisions = _iter_store(
        store,
        "iter_truth_observations",
        issues,
        decision_time=decision,
        first_release=False,
    )
    if revisions:
        issues.append(
            _issue(
                "TRUTH_REVISIONS_EXCLUDED_FROM_EVALUATION",
                "数据库中的修订真值仅进入附表证据，不参与正式评价。",
                stage="truth",
                severity="info",
                details={"count": len(revisions)},
            )
        )
    matched_truths = _match_first_release_truths(
        eligible, first_releases, config, issues
    )
    pending: list[Any] = []
    for claim in eligible:
        claim_id = _identifier(claim, "claim_id")
        existing = existing_by_id.get(claim_id)
        # Market outcomes are deterministic derivatives of PIT bar versions.
        # Recompute them on every run; a stored source string or return cannot
        # attest that the same bars were available at this decision cutoff.
        if _is_market_target(claim):
            pending.append(claim)
            continue
        if not _outcome_result_sufficient(claim, existing, config):
            pending.append(claim)
            continue
        matched = matched_truths.get(claim_id)
        if matched is None or not _outcome_matches_truth(existing, matched):
            pending.append(claim)
    if not pending:
        return
    routes: dict[str, tuple[str, str, str, str]] = {}
    instrument_ids: set[str] = set()
    for claim in pending:
        claim_id = _identifier(claim, "claim_id")
        route = _market_route_for_claim(
            claim,
            report_by_id.get(_identifier(claim, "report_id")),
            config,
            issues,
        )
        routes[claim_id] = route
        instrument_ids.update(
            item for item in (route[0], route[1], route[3]) if item
        )
    bars_by_subject: dict[str, list[Any]] = {}
    for instrument_id in sorted(instrument_ids):
        bars_by_subject[instrument_id] = [
            bar
            for bar in _iter_store(
                store,
                "iter_daily_bars",
                issues,
                instrument_id=instrument_id,
                available_by=decision,
                version_as_of=decision,
            )
            if _is_market_truth_source(_identifier(bar, "source"), config)
        ]
    try:
        evaluated: list[Any] = []
        for claim in pending:
            claim_id = _identifier(claim, "claim_id")
            subject_id, benchmark_id, benchmark_kind, _auxiliary = routes.get(
                claim_id, ("", "", "", "")
            )
            truths: dict[str, Any] = {}
            if claim_id in matched_truths:
                truths[claim_id] = matched_truths[claim_id]
            evaluated.extend(
                list(
                    _invoke_supported(
                        evaluate_claims,
                        claims=[claim],
                        reports=report_by_id,
                        bars_by_subject={
                            _identifier(claim, "subject_id"): bars_by_subject.get(
                                subject_id, []
                            )
                        },
                        benchmark_bars=bars_by_subject.get(benchmark_id, [])
                        if benchmark_id
                        else None,
                        truths=truths,
                        as_of=decision,
                        deduplicate=False,
                        market_benchmark_id=benchmark_id,
                        market_benchmark_kind=benchmark_kind,
                    )
                    or []
                )
            )
        if evaluated:
            store.upsert_outcomes(evaluated)
    except Exception as exc:
        issues.append(
            _issue(
                "CLAIM_EVALUATION_FAILED",
                "批量评价失败；不覆盖已有结果，也不推断命中。",
                stage="evaluation",
                severity="error",
                details={"error_type": type(exc).__name__, "error": str(exc)},
            )
        )


def _official_truth_source_ids(config: Mapping[str, Any], dimension: str) -> set[str]:
    sources = config.get("sources", {})
    section_name = {
        "macro": "macro_truth",
        "industry": "industry_truth",
        "stock": "stock_truth",
    }[dimension]
    section = sources.get(section_name, []) if isinstance(sources, Mapping) else []
    identifiers: set[str] = set()
    for item in section if isinstance(section, list) else []:
        if isinstance(item, Mapping) and item.get("id"):
            identifiers.add(str(item["id"]).strip().upper())
            aliases = item.get("truth_source_aliases", [])
            if isinstance(aliases, list):
                identifiers.update(str(alias).strip().upper() for alias in aliases if str(alias).strip())
    return identifiers


def _is_market_target(claim: Any) -> bool:
    target = _identifier(claim, "target_type").strip().lower()
    return target in {
        "target_price",
        "market_direction",
        "stock_rating",
        "industry_rating",
        "rating",
        "rating_change",
    }


def _is_market_truth_source(
    value: str,
    config: Mapping[str, Any] | None = None,
) -> bool:
    source = value.strip().lower()
    configured: set[str] = set()
    if config is not None:
        allowlist = _market_source_config(config).get("truth_source_allowlist", [])
        if isinstance(allowlist, Sequence) and not isinstance(allowlist, (str, bytes)):
            configured = {
                str(item).strip().lower() for item in allowlist if str(item).strip()
            }
    return (
        source
        in {
            "market_bars",
            "daily_bars",
            "price_bars",
            "market_return",
            "eastmoney_public.push2his",
        }
        or source in configured
    )


def _fundamental_truth_is_trusted(claim: Any, outcome: Any, config: Mapping[str, Any]) -> bool:
    dimension = _identifier(claim, "dimension")
    truth_source = _identifier(outcome, "truth_source").strip()
    if not dimension or not truth_source:
        return False
    if truth_source.upper() not in _official_truth_source_ids(config, dimension):
        return False
    claim_unit = _identifier(claim, "unit").strip()
    claim_basis = _identifier(claim, "benchmark").strip()
    if (
        not claim_unit
        or not claim_basis
        or _identifier(outcome, "truth_unit").strip().casefold()
        != claim_unit.casefold()
        or _identifier(outcome, "truth_basis").strip().casefold()
        != claim_basis.casefold()
    ):
        return False
    has_numeric_target = (
        _field(claim, "value_min") is not None
        or _field(claim, "value_max") is not None
    )
    if int(_field(claim, "direction", default=0) or 0) and not has_numeric_target:
        if (
            _field(outcome, "truth_change_value") is None
            or not _identifier(outcome, "truth_change_basis")
        ):
            return False
    return True


def _market_truth_is_trusted(
    outcome: Any,
    config: Mapping[str, Any] | None = None,
    claim: Any | None = None,
) -> bool:
    market_source = _identifier(outcome, "market_truth_source").strip()
    if not market_source and _is_market_truth_source(
        _identifier(outcome, "truth_source"), config
    ):
        market_source = _identifier(outcome, "truth_source")
    if not market_source or not _is_market_truth_source(market_source, config):
        return False
    if _identifier(outcome, "market_exclusion_reason"):
        return False
    if claim is not None:
        dimension = _identifier(claim, "dimension").strip().lower()
        benchmark_id = _identifier(outcome, "market_benchmark_id").strip()
        benchmark_kind = _identifier(outcome, "market_benchmark_kind").strip().lower()
        if dimension == "stock" and (
            benchmark_kind != "industry" or not benchmark_id
        ):
            return False
        if dimension == "industry":
            if (
                benchmark_kind != "csi300"
                or benchmark_id != CANONICAL_CSI300_INSTRUMENT_ID
            ):
                return False
    return (
        _field(outcome, "market_hit") is not None
        or _field(outcome, "market_return") is not None
        or _field(outcome, "market_excess_return") is not None
    )


def _same_optional_number(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    try:
        return abs(float(left) - float(right)) <= 1e-12 * max(
            1.0, abs(float(left)), abs(float(right))
        )
    except (TypeError, ValueError, ArithmeticError):
        return False


def _market_outcome_matches_current_evidence(
    outcome: Any,
    claim: Any,
    *,
    reports: Iterable[Any],
    daily_bars: Iterable[Any],
    config: Mapping[str, Any],
    as_of: datetime | None,
    issues: list[dict[str, Any]],
) -> bool:
    """Independently reconstruct a market result from strict PIT bar versions.

    Stored source strings and returns are never sufficient attestations.  This
    recomputation binds skill eligibility to the exact reports, routes and bars
    visible at the current decision cutoff.
    """

    if as_of is None:
        return False
    try:
        from .evaluation import evaluate_claim
    except (ImportError, AttributeError):
        return False
    report_by_id = {
        _identifier(report, "report_id", "id"): report for report in reports
    }
    report = report_by_id.get(_identifier(claim, "report_id"))
    route_issues: list[dict[str, Any]] = []
    subject_id, benchmark_id, benchmark_kind, _auxiliary = _market_route_for_claim(
        claim, report, config, route_issues
    )
    if not subject_id:
        return False
    if _identifier(claim, "dimension").lower() in {"stock", "industry"} and not benchmark_id:
        return False
    bars_by_id: dict[str, list[Any]] = {}
    for bar in daily_bars:
        available_at = _field(bar, "available_at")
        fetched_at = _field(bar, "fetched_at")
        if (
            available_at is None
            or fetched_at is None
            or available_at > as_of
            or fetched_at > as_of
            or not _is_market_truth_source(_identifier(bar, "source"), config)
        ):
            continue
        bars_by_id.setdefault(_identifier(bar, "instrument_id"), []).append(bar)
    reconstructed = _invoke_supported(
        evaluate_claim,
        claim=claim,
        report=report,
        bars=bars_by_id.get(subject_id, []),
        benchmark_bars=bars_by_id.get(benchmark_id, []) if benchmark_id else None,
        truth=None,
        as_of=as_of,
        market_benchmark_id=benchmark_id,
        market_benchmark_kind=benchmark_kind,
    )
    if _field(reconstructed, "mature") is not True:
        return False
    for name in (
        "truth_available_at",
        "hit",
        "mature",
        "fundamental_hit",
        "market_hit",
        "market_exclusion_reason",
        "market_truth_source",
        "market_benchmark_id",
        "market_benchmark_kind",
    ):
        if _field(outcome, name) != _field(reconstructed, name):
            return False
    for name in (
        "realized_value",
        "market_return",
        "benchmark_return",
        "error",
        "market_excess_return",
    ):
        if not _same_optional_number(
            _field(outcome, name), _field(reconstructed, name)
        ):
            return False
    return True


def _truth_is_trusted(claim: Any, outcome: Any, config: Mapping[str, Any]) -> bool:
    if _is_market_target(claim):
        return _market_truth_is_trusted(outcome, config, claim)
    return _fundamental_truth_is_trusted(claim, outcome, config)


def _trusted_outcomes(
    outcomes: Iterable[Any],
    claims: Iterable[Any],
    config: Mapping[str, Any],
    issues: list[dict[str, Any]] | None = None,
    *,
    truth_observations: Iterable[Any] = (),
    reports: Iterable[Any] = (),
    daily_bars: Iterable[Any] = (),
    as_of: datetime | None = None,
) -> list[Any]:
    claim_by_id = {
        _identifier(claim, "claim_id", "id"): claim
        for claim in claims
        if _identifier(claim, "claim_id", "id")
    }
    match_issues = issues if issues is not None else []
    matched_truths = _match_first_release_truths(
        claim_by_id.values(), truth_observations, config, match_issues
    )
    trusted: list[Any] = []
    for outcome in outcomes:
        claim_id = _identifier(outcome, "claim_id")
        claim = claim_by_id.get(claim_id)
        truth_binding_valid = True
        if claim is not None:
            if _is_market_target(claim):
                truth_binding_valid = _market_outcome_matches_current_evidence(
                    outcome,
                    claim,
                    reports=reports,
                    daily_bars=daily_bars,
                    config=config,
                    as_of=as_of,
                    issues=match_issues,
                )
            else:
                matched_truth = matched_truths.get(claim_id)
                truth_binding_valid = (
                    matched_truth is not None
                    and _outcome_matches_truth(outcome, matched_truth)
                )
        if (
            claim is not None
            and truth_binding_valid
            and _truth_is_trusted(claim, outcome, config)
        ):
            if _is_market_target(claim):
                selected_hit = _field(outcome, "market_hit")
                if selected_hit is None and _is_market_truth_source(
                    _identifier(outcome, "truth_source"), config
                ):
                    selected_hit = _field(outcome, "hit")
                selected = {
                    "claim_id": claim_id,
                    "truth_source": _identifier(outcome, "market_truth_source") or "market_bars",
                    # Skill is available when the outcome truth became public,
                    # not when a later audit happened to evaluate the row.
                    "truth_available_at": _field(
                        outcome, "truth_available_at", "evaluated_at"
                    ),
                    "hit": selected_hit,
                    "mature": _field(outcome, "mature") is True,
                    "evaluated_at": _field(outcome, "evaluated_at"),
                }
            else:
                selected = {
                    "claim_id": claim_id,
                    "truth_source": _identifier(outcome, "truth_source"),
                    "truth_available_at": _field(outcome, "truth_available_at"),
                    "hit": _field(outcome, "fundamental_hit", "hit"),
                    "mature": _field(outcome, "mature") is True,
                    "evaluated_at": _field(outcome, "evaluated_at"),
                }
            if selected["hit"] is not None:
                trusted.append(selected)
            continue
        has_result = (
            _field(outcome, "mature") is True
            and (
                _field(outcome, "hit") is not None
                or _field(outcome, "realized_value") is not None
            )
        )
        if issues is not None and claim is not None and has_result:
            issues.append(
                _issue(
                    "UNTRUSTED_TRUTH_EXCLUDED_FROM_SKILL",
                    "该结果的真值源不满足目标类型与维度的准入规则，已从技能和因子估计排除。",
                    stage="truth",
                    dimension=_identifier(claim, "dimension"),
                    report_id=_identifier(claim, "report_id"),
                    claim_id=claim_id,
                    details={
                        "target_type": _identifier(claim, "target_type"),
                        "truth_source": _identifier(outcome, "truth_source"),
                    },
                )
            )
    return trusted


def _outcomes_for_reporting(
    outcomes: Iterable[Any],
    claims: Iterable[Any],
    config: Mapping[str, Any],
    *,
    truth_observations: Iterable[Any] = (),
    reports: Iterable[Any] = (),
    daily_bars: Iterable[Any] = (),
    as_of: datetime | None = None,
) -> list[dict[str, Any]]:
    claim_by_id = {
        _identifier(claim, "claim_id", "id"): claim
        for claim in claims
        if _identifier(claim, "claim_id", "id")
    }
    matched_truths = _match_first_release_truths(
        claim_by_id.values(), truth_observations, config, []
    )
    fields = (
        "claim_id",
        "truth_source",
        "truth_available_at",
        "realized_value",
        "market_return",
        "benchmark_return",
        "error",
        "hit",
        "mature",
        "exclusion_reason",
        "evaluated_at",
        "fundamental_hit",
        "market_hit",
        "market_excess_return",
        "market_exclusion_reason",
        "market_truth_source",
        "market_benchmark_id",
        "market_benchmark_kind",
        "truth_unit",
        "truth_basis",
        "truth_change_value",
        "truth_change_basis",
    )
    rendered: list[dict[str, Any]] = []
    for outcome in outcomes:
        row = {name: _field(outcome, name) for name in fields}
        claim = claim_by_id.get(_identifier(outcome, "claim_id"))
        matched_truth = matched_truths.get(_identifier(outcome, "claim_id"))
        fundamental_trusted = (
            claim is not None
            and matched_truth is not None
            and _outcome_matches_truth(outcome, matched_truth)
            and _fundamental_truth_is_trusted(claim, outcome, config)
        )
        market_trusted = (
            claim is not None
            and _market_truth_is_trusted(outcome, config, claim)
            and _market_outcome_matches_current_evidence(
                outcome,
                claim,
                reports=reports,
                daily_bars=daily_bars,
                config=config,
                as_of=as_of,
                issues=[],
            )
        )
        row["fundamental_truth_eligible"] = fundamental_trusted
        row["market_truth_eligible"] = market_trusted
        if not fundamental_trusted and (_field(outcome, "truth_source") or _field(outcome, "mature") is True):
            row["fundamental_hit"] = None
            if claim is None or not (_is_market_target(claim) and market_trusted):
                row["hit"] = None
            if not _is_market_truth_source(
                _identifier(outcome, "truth_source"), config
            ):
                row["exclusion_reason"] = row.get("exclusion_reason") or "untrusted_fundamental_truth_source"
        if not market_trusted:
            row["market_hit"] = None
            if _field(outcome, "market_hit") is not None:
                row["market_exclusion_reason"] = (
                    row.get("market_exclusion_reason")
                    or "untrusted_market_benchmark"
                )
        rendered.append(row)
    return rendered


def _append_official_truth_issues(
    *,
    state: PipelineState,
    config: Mapping[str, Any],
    dimensions: Sequence[str],
    issues: list[dict[str, Any]],
) -> None:
    """Require imported official first-release truth for fundamental scoring."""

    for dimension in dimensions:
        trusted_ids = _official_truth_source_ids(config, dimension)
        has_trusted = any(
            _identifier(observation, "dimension") == dimension
            and _field(observation, "first_release") is True
            and _field(observation, "revision") is False
            and _field(observation, "evidence_verified") is True
            and _identifier(observation, "truth_source").upper() in trusted_ids
            for observation in state.truth_observations
        )
        observed_untrusted: set[str] = set()
        for observation in state.truth_observations:
            if _identifier(observation, "dimension") != dimension:
                continue
            truth_source = _identifier(observation, "truth_source").strip()
            if not truth_source:
                continue
            if (
                truth_source.upper() not in trusted_ids
                or _field(observation, "evidence_verified") is not True
            ):
                observed_untrusted.add(truth_source)
        if not has_trusted:
            issues.append(
                _issue(
                    "OFFICIAL_TRUTH_SOURCE_NOT_CONFIGURED",
                    "没有导入该维度的官方首次发布真值；现有行情或 provisional EPS 不得充当正式基本面真值。",
                    stage="truth",
                    dimension=dimension,
                    severity="warning",
                    details={
                        "accepted_source_ids": sorted(trusted_ids),
                        "observed_untrusted_sources": sorted(observed_untrusted),
                    },
                )
            )


def _refresh_skill_snapshots(
    store: Any,
    *,
    reports: Iterable[Any],
    claims: Iterable[Any],
    outcomes: Iterable[Any],
    truth_observations: Iterable[Any] = (),
    daily_bars: Iterable[Any] = (),
    config: Mapping[str, Any],
    decision: datetime,
    issues: list[dict[str, Any]],
) -> list[Any]:
    try:
        from .skills import build_skill_snapshots
    except (ImportError, AttributeError) as exc:
        issues.append(
            _issue(
                "SKILL_MODULE_UNAVAILABLE",
                "技能估计模块不可用；不生成来源能力排名。",
                stage="skill",
                severity="error",
                details={"error_type": type(exc).__name__, "error": str(exc)},
            )
        )
        return []
    skill_config = config.get("skill", {})
    report_rows = list(reports)
    try:
        trusted_outcomes = _trusted_outcomes(
            outcomes,
            claims,
            config,
            issues,
            truth_observations=truth_observations,
            reports=report_rows,
            daily_bars=daily_bars,
            as_of=decision,
        )
        snapshots = list(
            _invoke_supported(
                build_skill_snapshots,
                outcomes=trusted_outcomes,
                claims=_eligible_claims(claims, config, reports=report_rows),
                reports=report_rows,
                as_of=decision,
                half_life_days=float(skill_config.get("half_life_days", 730)),
                sensitivity_half_life_days=float(skill_config.get("sensitivity_half_life_days", 365)),
                lookback_years=float(skill_config.get("maximum_lookback_years", 5)),
            )
            or []
        )
        if snapshots:
            store.upsert_skill_snapshots(snapshots)
        return snapshots
    except Exception as exc:
        issues.append(
            _issue(
                "SKILL_ESTIMATION_FAILED",
                "技能估计失败；不生成或沿用未经本次时点验证的排名。",
                stage="skill",
                severity="error",
                details={"error_type": type(exc).__name__, "error": str(exc)},
            )
        )
        return []


def _latest_skill_snapshots(snapshots: Iterable[Any]) -> list[Any]:
    latest: dict[tuple[str, ...], Any] = {}
    for snapshot in snapshots:
        key = tuple(
            _identifier(snapshot, name)
            for name in (
                "broker",
                "analyst",
                "team",
                "dimension",
                "target_type",
                "horizon_days",
                "market_state",
                "industry_id",
            )
        )
        previous = latest.get(key)
        if previous is None or str(_field(snapshot, "as_of") or "") > str(_field(previous, "as_of") or ""):
            latest[key] = snapshot
    return [latest[key] for key in sorted(latest)]


def _admissible_skill_snapshots(
    state: PipelineState,
    config: Mapping[str, Any],
    *,
    as_of: datetime | None = None,
) -> list[Any]:
    """Reject stored snapshots whose source reports are not still auditable."""

    eligible_claims = _eligible_claims(
        state.claims, config, reports=state.reports
    )
    trusted_claim_ids = {
        _identifier(outcome, "claim_id")
        for outcome in _trusted_outcomes(
            state.outcomes,
            state.claims,
            config,
            truth_observations=state.truth_observations,
            reports=state.reports,
            daily_bars=state.daily_bars,
            as_of=as_of,
        )
    }
    valid_cells: set[tuple[str, str, str]] = set()
    for claim in eligible_claims:
        claim_id = _identifier(claim, "claim_id", "id")
        if claim_id not in trusted_claim_ids:
            continue
        valid_cells.add(
            (
                _identifier(claim, "report_id"),
                _identifier(claim, "target_type"),
                _identifier(claim, "horizon_days"),
            )
        )
    admitted: list[Any] = []
    for snapshot in _latest_skill_snapshots(state.skill_snapshots):
        if not _extractor_dimension_admitted(config, _identifier(snapshot, "dimension")):
            continue
        source_ids = _field(snapshot, "source_report_ids", default=()) or ()
        if isinstance(source_ids, str):
            source_ids = [item for item in source_ids.split("|") if item]
        target = _identifier(snapshot, "target_type")
        horizon = _identifier(snapshot, "horizon_days")
        if source_ids and all((str(report_id), target, horizon) in valid_cells for report_id in source_ids):
            admitted.append(snapshot)
    return admitted


def _latest_factor_observations(observations: Iterable[Any]) -> list[Any]:
    latest: dict[str, Any] = {}
    for observation in observations:
        stock_id = _identifier(observation, "stock_id")
        previous = latest.get(stock_id)
        if previous is None or str(_field(observation, "as_of") or "") > str(_field(previous, "as_of") or ""):
            latest[stock_id] = observation
    return [latest[key] for key in sorted(latest)]


def _build_factor_observations(
    store: Any,
    *,
    state: PipelineState,
    config: Mapping[str, Any],
    decision: datetime,
    issues: list[dict[str, Any]],
    specifications: Iterable[Mapping[str, Any]] | None = None,
) -> list[Any]:
    try:
        from .factors import build_factor_observations
    except (ImportError, AttributeError) as exc:
        issues.append(
            _issue(
                "FACTOR_MODULE_UNAVAILABLE",
                "三层因子模块不可用；不生成替代分数。",
                stage="factor",
                severity="error",
                details={"error_type": type(exc).__name__, "error": str(exc)},
            )
        )
        return []
    resolved_specifications = list(
        specifications
        if specifications is not None
        else config.get("factor_research", {}).get("specifications", [])
    )
    if not resolved_specifications:
        issues.append(
            _issue(
                "OBJECTIVE_FACTOR_SOURCE_NOT_CONFIGURED",
                "未提供带 available_at 的宏观、行业和个股客观因子规格；不把缺失值补成零。",
                stage="factor",
            )
        )
        return []
    try:
        trusted_outcomes = _trusted_outcomes(
            state.outcomes,
            state.claims,
            config,
            issues,
            truth_observations=state.truth_observations,
            reports=state.reports,
            daily_bars=state.daily_bars,
            as_of=decision,
        )
        observations = list(
            _invoke_supported(
                build_factor_observations,
                specifications=resolved_specifications,
                reports=state.reports,
                claims=_eligible_claims(
                    state.claims, config, reports=state.reports
                ),
                outcomes=trusted_outcomes,
                outcomes_are_trusted=True,
                skill_snapshots=_admissible_skill_snapshots(
                    state, config, as_of=decision
                ),
                snapshots=_admissible_skill_snapshots(
                    state, config, as_of=decision
                ),
                as_of=decision,
                objective_factors={},
                config=config,
            )
            or []
        )
        if observations:
            store.upsert_factor_observations(observations)
        return observations
    except Exception as exc:
        issues.append(
            _issue(
                "FACTOR_BUILD_FAILED",
                "三层因子构建失败或客观因子缺失；不补零、不进入策略。",
                stage="factor",
                severity="error",
                details={"error_type": type(exc).__name__, "error": str(exc)},
            )
        )
        return []


def _build_internal_factor_batch(
    store: Any,
    *,
    state: PipelineState,
    config: Mapping[str, Any],
    decision: datetime,
    issues: list[dict[str, Any]],
    specifications: Iterable[Mapping[str, Any]],
    trading_calendar: Iterable[Any],
) -> Any | None:
    """Build the only label batch that may be used as admission evidence."""

    resolved_specifications = list(specifications)
    calendar_rows = list(trading_calendar)
    if not resolved_specifications or not calendar_rows:
        return None
    try:
        from .factors import (
            build_factor_observations,
            build_internal_factor_research_rows,
        )

        trusted_outcomes = _trusted_outcomes(
            state.outcomes,
            state.claims,
            config,
            issues,
            truth_observations=state.truth_observations,
            reports=state.reports,
            daily_bars=state.daily_bars,
            as_of=decision,
        )
        components = list(
            _invoke_supported(
                build_factor_observations,
                specifications=resolved_specifications,
                reports=state.reports,
                claims=_eligible_claims(
                    state.claims, config, reports=state.reports
                ),
                outcomes=trusted_outcomes,
                outcomes_are_trusted=True,
                skill_snapshots=_admissible_skill_snapshots(
                    state, config, as_of=decision
                ),
                snapshots=_admissible_skill_snapshots(
                    state, config, as_of=decision
                ),
                as_of=decision,
                objective_factors={},
                config=config,
                return_components=True,
            )
            or []
        )
        bars = list(
            store.iter_daily_bars(
                available_by=decision,
                version_as_of=decision,
            )
        )
        dates = config.get("dates", {})
        batch = build_internal_factor_research_rows(
            components,
            resolved_specifications,
            bars,
            trading_calendar=calendar_rows,
            evaluation_as_of=decision,
            sample_start=dates.get("factor_backfill_start"),
            sample_end=dates.get("factor_backfill_end"),
            horizons=tuple(
                config.get("horizons", {}).get(
                    "factor_target_trading_days", (20, 60)
                )
            ),
        )
        if batch.exclusions:
            issues.append(
                _issue(
                    "INTERNAL_LABEL_ROWS_EXCLUDED",
                    "部分内部因子行因PIT行业映射、周频时点或精确行情端点不足而被排除。",
                    stage="walk_forward",
                    severity="info",
                    details={
                        "count": len(batch.exclusions),
                        "examples": list(batch.exclusions[:20]),
                    },
                )
            )
        if not batch.rows:
            issues.append(
                _issue(
                    "INTERNAL_LABEL_RECOMPUTE_EMPTY",
                    "本地因子、显式PIT行业映射和日行情未形成任何成熟20/60日标签；M1保持不准入。",
                    stage="walk_forward",
                    severity="warning",
                )
            )
            return None
        return batch
    except Exception as exc:
        issues.append(
            _issue(
                "INTERNAL_LABEL_RECOMPUTE_FAILED",
                "内部标签重算失败；禁止把外部标签升级为正式准入证据。",
                stage="walk_forward",
                severity="error",
                details={"error_type": type(exc).__name__, "error": str(exc)},
            )
        )
        return None


def _run_walk_forward(
    rows: Iterable[Mapping[str, Any]],
    *,
    config: Mapping[str, Any],
    issues: list[dict[str, Any]],
    evaluation_as_of: datetime,
    trading_calendar: Iterable[Any] | None = None,
    internal_batch: Any | None = None,
) -> Mapping[str, Any] | None:
    materialized = list(rows)
    external_materialized = list(materialized)
    evidence_verified = False
    internal_evidence_hash = ""
    if internal_batch is not None:
        try:
            from .factors import (
                InternalFactorResearchBatch,
                validate_internal_factor_research_batch,
            )

            if not isinstance(internal_batch, InternalFactorResearchBatch):
                raise TypeError("internal_batch was not created by the local recomputation path")
            validate_internal_factor_research_batch(internal_batch)
            allowed_sources = {
                str(value).strip().lower()
                for value in _market_source_config(config).get(
                    "truth_source_allowlist", ()
                )
                if str(value).strip()
            }
            batch_sources = {
                str(value).strip().lower()
                for value in internal_batch.bar_sources
                if str(value).strip()
            }
            if not batch_sources or not batch_sources.issubset(allowed_sources):
                raise ValueError(
                    "internal label bars are not bound to configured market truth sources"
                )
            materialized = [dict(row) for row in internal_batch.rows]
            trading_calendar = internal_batch.trading_calendar
            internal_evidence_hash = internal_batch.evidence_hash
            # Internal recomputation verifies the arithmetic and hashes, not
            # the authenticity of an arbitrary local calendar, objective
            # factor or point-in-time industry mapping.  Until those inputs
            # are resolved from store-backed controlled adapters, the batch is
            # diagnostic and cannot unlock admission.
            evidence_verified = False
            issues.append(
                _issue(
                    "INTERNAL_DERIVATION_SOURCE_PROVENANCE_UNVERIFIED",
                    "标签已在本地重算并完成哈希校验，但交易日历、客观因子和行业映射仍来自外部规格；不得作为正式准入证据。",
                    stage="walk_forward",
                    severity="warning",
                )
            )
            if external_materialized:
                issues.append(
                    _issue(
                        "EXTERNAL_FACTOR_INPUT_DIAGNOSTIC_ONLY",
                        "检测到外部因子文件；正式准入仅使用本地重算标签，外部行不会改变admission。",
                        stage="walk_forward",
                        severity="info",
                    )
                )
        except Exception as exc:
            issues.append(
                _issue(
                    "INTERNAL_LABEL_BATCH_REJECTED",
                    "内部标签批次身份或证据无效；降级为外部诊断模式。",
                    stage="walk_forward",
                    severity="error",
                    details={"error_type": type(exc).__name__, "error": str(exc)},
                )
            )
            materialized = external_materialized
            evidence_verified = False
    if not materialized:
        issues.append(
            _issue(
                "WALK_FORWARD_INPUT_NOT_CONFIGURED",
                "没有历史因子特征与20日标签输入；Walk-forward 保持 not_evaluated/not_admitted。",
                stage="walk_forward",
            )
        )
        return None
    try:
        from .factors import validate_walk_forward_input_rows, walk_forward_evaluate
    except (ImportError, AttributeError) as exc:
        issues.append(
            _issue(
                "WALK_FORWARD_MODULE_UNAVAILABLE",
                "Walk-forward 模块不可用；不以样本内表现代替。",
                stage="walk_forward",
                severity="error",
                details={"error_type": type(exc).__name__, "error": str(exc)},
            )
        )
        return None
    research = config.get("factor_research", {})
    try:
        primary_label = str(
            research.get("label_field") or "stock_excess_vs_industry_20d"
        )
        secondary_field = str(
            research.get("secondary_label_field")
            or "stock_excess_vs_industry_60d"
        )
        dates = config.get("dates", {})
        materialized = list(
            validate_walk_forward_input_rows(
                materialized,
                sample_start=dates.get("factor_backfill_start"),
                sample_end=dates.get("factor_backfill_end"),
                evaluation_as_of=evaluation_as_of,
                label_field=primary_label,
                secondary_label_field=secondary_field,
                date_field=str(research.get("date_field") or "as_of"),
                industry_field=str(research.get("industry_field") or "industry_id"),
                require_internal_label_provenance=evidence_verified,
            )
        )
    except Exception as exc:
        issues.append(
            _issue(
                "WALK_FORWARD_INPUT_CONTRACT_FAILED",
                "历史因子行未通过PIT、快照哈希、相对行业标签或研究时点合同；仅返回not_admitted。",
                stage="walk_forward",
                severity="error",
                details={"error_type": type(exc).__name__, "error": str(exc)},
            )
        )
        return {
            "status": "not_admitted",
            "admitted": False,
            "reason": "walk_forward_input_contract_failed",
        }
    try:
        calendar_rows = list(trading_calendar or ())
        if not calendar_rows:
            issues.append(
                _issue(
                    "TRADING_CALENDAR_NOT_CONFIGURED",
                    "未提供真实交易日历；仅日频连续输入可诊断，禁止用工作日近似冒充周频purge/embargo证据。",
                    stage="walk_forward",
                    severity="info",
                )
            )
        common_kwargs = {
            "date_field": str(research.get("date_field") or "as_of"),
            "industry_field": str(research.get("industry_field") or "industry_id"),
            "model": str(research.get("model") or "ridge"),
            "cost_bps": float(research.get("cost_bps", 10.0)),
            "train_months": int(research.get("train_months", 36)),
            "validation_months": int(research.get("validation_months", 6)),
            "test_months": int(research.get("test_months", 6)),
            "step_months": int(research.get("step_months", 6)),
            "purge_embargo_days": int(
                config.get("horizons", {}).get(
                    "purge_embargo_trading_days",
                    research.get("purge_embargo_days", 120),
                )
            ),
            "frozen_months": int(research.get("final_frozen_test_months", 12)),
            "trading_calendar": calendar_rows or None,
            "admission_evidence_verified": evidence_verified,
            "rebalance_frequency": str(
                research.get("rebalance_frequency") or "weekly"
            ),
            "minimum_stocks_per_rebalance_date": int(
                research.get("minimum_stocks_per_rebalance_date", 20)
            ),
            "minimum_industries_per_rebalance_date": int(
                research.get("minimum_industries_per_rebalance_date", 3)
            ),
            "minimum_train_rebalance_dates": int(
                research.get("minimum_train_rebalance_dates", 104)
            ),
            "minimum_validation_rebalance_dates": int(
                research.get("minimum_validation_rebalance_dates", 13)
            ),
            "minimum_test_rebalance_dates": int(
                research.get("minimum_test_rebalance_dates", 13)
            ),
        }
        result = _invoke_supported(
            walk_forward_evaluate,
            rows=materialized,
            label_field=primary_label,
            **common_kwargs,
        )
        if not isinstance(result, Mapping):
            return None
        primary = dict(result)
        primary["label_evidence"] = {
            "mode": "internal_recomputed" if evidence_verified else "external_diagnostic_only",
            "evidence_hash": internal_evidence_hash,
        }
        if any(row.get(secondary_field) not in (None, "") for row in materialized):
            auxiliary = _invoke_supported(
                walk_forward_evaluate,
                rows=materialized,
                label_field=secondary_field,
                **common_kwargs,
            )
            primary["auxiliary_60d"] = (
                dict(auxiliary)
                if isinstance(auxiliary, Mapping)
                else {"status": "not_evaluated", "reason": "invalid_auxiliary_result"}
            )
        else:
            primary["auxiliary_60d"] = {
                "status": "not_evaluated",
                "reason": f"missing_label_field:{secondary_field}",
            }
        return primary
    except Exception as exc:
        issues.append(
            _issue(
                "WALK_FORWARD_EVALUATION_FAILED",
                "Walk-forward 输入不足或契约不合法；M1 不准入。",
                stage="walk_forward",
                severity="error",
                details={"error_type": type(exc).__name__, "error": str(exc)},
            )
        )
        return None


def _rank_deep_reads(
    *,
    state: PipelineState,
    config: Mapping[str, Any],
    decision: datetime,
    limit: int,
    issues: list[dict[str, Any]],
) -> list[Any]:
    try:
        from .factors import rank_deep_reads
    except (ImportError, AttributeError) as exc:
        issues.append(
            _issue(
                "DEEP_READ_MODULE_UNAVAILABLE",
                "深读排序模块不可用；不按标题猜测优先级。",
                stage="deep_read",
                severity="error",
                details={"error_type": type(exc).__name__, "error": str(exc)},
            )
        )
        return []
    try:
        trusted_outcomes = _trusted_outcomes(
            state.outcomes,
            state.claims,
            config,
            issues,
            truth_observations=state.truth_observations,
            reports=state.reports,
            daily_bars=state.daily_bars,
            as_of=decision,
        )
        return list(
            _invoke_supported(
                rank_deep_reads,
                reports=state.reports,
                claims=_claims_with_current_evidence(
                    _candidate_claims(state.claims, config),
                    reports=state.reports,
                ),
                outcomes=trusted_outcomes,
                outcomes_are_trusted=True,
                skill_snapshots=_admissible_skill_snapshots(
                    state, config, as_of=decision
                ),
                snapshots=_admissible_skill_snapshots(
                    state, config, as_of=decision
                ),
                factor_observations=_latest_factor_observations(state.factor_observations),
                factors=_latest_factor_observations(state.factor_observations),
                as_of=decision,
                limit=limit,
                config=config,
            )
            or []
        )
    except Exception as exc:
        issues.append(
            _issue(
                "DEEP_READ_RANK_FAILED",
                "深读排序失败；返回空队列而非虚假名次。",
                stage="deep_read",
                severity="error",
                details={"error_type": type(exc).__name__, "error": str(exc)},
            )
        )
        return []


def _report_date(report: Any) -> date | None:
    value = _field(report, "published_at", "available_at", "report_date")
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _sample_state(
    state: PipelineState,
    *,
    sample_start: date,
    sample_end: date,
) -> PipelineState:
    reports = [
        report
        for report in state.reports
        if (_report_date(report) is not None and sample_start <= _report_date(report) <= sample_end)  # type: ignore[operator]
    ]
    report_ids = {_identifier(report, "report_id", "id") for report in reports}
    claims = [claim for claim in state.claims if _identifier(claim, "report_id") in report_ids]
    claim_ids = {_identifier(claim, "claim_id", "id") for claim in claims}
    outcomes = [outcome for outcome in state.outcomes if _identifier(outcome, "claim_id") in claim_ids]
    return PipelineState(
        reports=reports,
        claims=claims,
        outcomes=outcomes,
        skill_snapshots=_latest_skill_snapshots(state.skill_snapshots),
        factor_observations=_latest_factor_observations(state.factor_observations),
        truth_observations=state.truth_observations,
        daily_bars=state.daily_bars,
    )


def _write_state_bundle(
    *,
    state: PipelineState,
    config: Mapping[str, Any],
    paths: RuntimePaths,
    as_of: str,
    command: str,
    issues: Iterable[Any],
    deep_read_candidates: Iterable[Any] = (),
    walk_forward_result: Mapping[str, Any] | None = None,
    dashboard: Mapping[str, Any] | None = None,
    additional_input_snapshot: Mapping[str, Any] | None = None,
    parameters: Mapping[str, Any] | None = None,
) -> ReportBundle:
    resolved_issues = list(issues)
    dates = config.get("dates", {})
    reporting_state = PipelineState(
        reports=state.reports,
        claims=state.claims,
        outcomes=state.outcomes,
        skill_snapshots=_admissible_skill_snapshots(
            state, config, as_of=_decision_time(as_of)
        ),
        factor_observations=state.factor_observations,
        truth_observations=state.truth_observations,
        daily_bars=state.daily_bars,
    )
    sample = _sample_state(
        reporting_state,
        sample_start=_date_value(dates.get("sample_start"), "dates.sample_start"),
        sample_end=_date_value(dates.get("sample_end"), "dates.sample_end"),
    )
    formally_eligible_claims = _eligible_claims(
        sample.claims, config, reports=sample.reports
    )
    reporting_outcomes = _outcomes_for_reporting(
        sample.outcomes,
        formally_eligible_claims,
        config,
        truth_observations=sample.truth_observations,
        reports=sample.reports,
        daily_bars=sample.daily_bars,
        as_of=_decision_time(as_of),
    )
    evidence_snapshot = {
        "truth_observations": sample.truth_observations,
        "daily_bar_versions": sample.daily_bars,
        "caller_additional_inputs": additional_input_snapshot or {},
    }
    return write_report_bundle(
        paths.output_directory,
        as_of=as_of,
        command=command,
        config=config,
        reports=sample.reports,
        claims=sample.claims,
        outcomes=reporting_outcomes,
        skill_snapshots=sample.skill_snapshots,
        factor_observations=sample.factor_observations,
        walk_forward_result=walk_forward_result,
        dashboard=dashboard,
        deep_read_candidates=deep_read_candidates,
        exceptions=resolved_issues,
        parameters=parameters,
        additional_input_snapshot=evidence_snapshot,
    )


def run_audit(
    *,
    dimensions: str | Sequence[str] | None = None,
    as_of: Any = None,
    offline: bool = False,
    config_path: Path | str | None = None,
    db_path: Path | str | None = None,
    cache_directory: Path | str | None = None,
    output_directory: Path | str | None = None,
    truth_input_paths: Sequence[Path | str] | Path | str | None = None,
) -> ReportBundle:
    """Collect, extract, evaluate and render the independent audit tables."""

    config = load_config(config_path)
    paths = resolve_runtime_paths(
        config,
        config_path=config_path,
        db_path=db_path,
        cache_directory=cache_directory,
        output_directory=output_directory,
    )
    resolved_as_of = as_of or config.get("dates", {}).get("evaluation_as_of")
    decision = _decision_time(resolved_as_of)
    as_of_text = decision.date().isoformat()
    selected_dimensions = parse_dimensions(dimensions)
    dates = config.get("dates", {})
    sample_start = _date_value(dates.get("sample_start"), "dates.sample_start")
    sample_end = _date_value(dates.get("sample_end"), "dates.sample_end")
    issues: list[dict[str, Any]] = []
    resolved_truth_inputs = _resolve_truth_input_paths(config, truth_input_paths)
    report_trading_calendar = _configured_trading_calendar(
        config, config_path=paths.config, issues=issues
    )

    from .storage import AuditStore

    with AuditStore(paths.database, decision_time=decision) as store:
        _import_truth_inputs(
            store,
            config=config,
            paths=resolved_truth_inputs,
            issues=issues,
        )
        if offline:
            issues.append(
                _issue(
                    "OFFLINE_MODE",
                    "已禁用网络；仅使用本地数据库和缓存可见数据。",
                    stage="ingestion",
                    severity="info",
                )
            )
        else:
            _ingest_online(
                store,
                config=config,
                dimensions=selected_dimensions,
                start_date=sample_start,
                end_date=sample_end,
                decision=decision,
                cache_directory=paths.cache_directory,
                issues=issues,
                collection_scope="audit_sample",
                trading_calendar=report_trading_calendar,
            )
            _ingest_online(
                store,
                config=config,
                dimensions=selected_dimensions,
                start_date=_current_feed_start(config, decision),
                end_date=decision.date(),
                decision=decision,
                cache_directory=paths.cache_directory,
                issues=issues,
                collection_scope="current",
                trading_calendar=report_trading_calendar,
            )
        state = _load_state(store, decision, issues)
        selected_reports = [
            report
            for report in state.reports
            if _identifier(report, "dimension") in selected_dimensions
            and _report_date(report) is not None
            and sample_start <= _report_date(report) <= sample_end  # type: ignore[operator]
        ]
        current_start = _current_feed_start(config, decision)
        current_reports = [
            report
            for report in state.reports
            if _identifier(report, "dimension") in selected_dimensions
            and _collection_scope(report) == "current"
            and _report_date(report) is not None
            and current_start <= _report_date(report) <= decision.date()  # type: ignore[operator]
        ]
        reports_for_extraction = [
            *selected_reports,
            *current_reports,
        ]
        deduplicated = _deduplicate_reports(reports_for_extraction, issues)
        _extract_claims(
            store,
            reports=deduplicated,
            existing_claims=state.claims,
            config=config,
            issues=issues,
            emit_unscorable=False,
        )
        state = _load_state(store, decision, issues)
        # A structured rating/EPS/target-price claim covers only that topic. It
        # must never suppress extraction of demand, inventory, earnings-change
        # or other falsifiable claims in the same PDF. The extractor performs
        # claim-level report/topic/period/horizon deduplication.
        pdf_reports = [
            report
            for report in deduplicated
            if _report_has_resolvable_pdf(report)
        ]
        pdf_texts = _extract_pdf_texts(
            pdf_reports,
            store=store,
            cache_directory=paths.cache_directory,
            offline=offline,
            decision=decision,
            issues=issues,
        )
        if pdf_texts:
            state = _load_state(store, decision, issues)
            enriched_pdf_reports = [
                report
                for report in state.reports
                if _identifier(report, "report_id", "id") in pdf_texts
            ]
            _extract_claims(
                store,
                reports=enriched_pdf_reports,
                existing_claims=state.claims,
                config=config,
                issues=issues,
                text_by_report_id=pdf_texts,
                retry_completed=True,
                emit_unscorable=False,
            )
        state = _load_state(store, decision, issues)
        validation_population = [
            report
            for report in state.reports
            if _report_date(report) is not None
            and sample_start <= _report_date(report) <= sample_end  # type: ignore[operator]
        ]
        _hydrate_extractor_validation(
            config,
            decision=decision,
            config_path=paths.config,
            population_reports=validation_population,
            population_claims=_claims_with_current_evidence(
                state.claims, reports=validation_population
            ),
            issues=issues,
        )
        _append_extractor_validation_issues(
            config, selected_dimensions, issues
        )
        final_claim_report_ids = {
            _identifier(claim, "report_id") for claim in state.claims
        }
        for report in deduplicated:
            report_id = _identifier(report, "report_id", "id")
            if report_id not in final_claim_report_ids:
                issues.append(
                    _issue(
                        "UNSCORABLE_REPORT",
                        "元数据与已运行的可用正文抽取均未得到同时具备变量、方向和期限的可证伪预测。",
                        stage="extraction",
                        dimension=_identifier(report, "dimension"),
                        report_id=report_id,
                    )
                )
        selected_reports = [
            report
            for report in state.reports
            if _identifier(report, "dimension") in selected_dimensions
            and _report_date(report) is not None
            and sample_start <= _report_date(report) <= sample_end  # type: ignore[operator]
        ]
        selected_report_ids = {
            _identifier(report, "report_id", "id") for report in selected_reports
        }
        selected_claims = [
            claim for claim in state.claims if _identifier(claim, "report_id") in selected_report_ids
        ]
        _ingest_market_bars(
            store,
            reports=selected_reports,
            claims=selected_claims,
            config=config,
            decision=decision,
            cache_directory=paths.cache_directory,
            offline=offline,
            issues=issues,
        )
        _evaluate_missing_outcomes(
            store,
            reports=selected_reports,
            claims=selected_claims,
            outcomes=state.outcomes,
            config=config,
            decision=decision,
            issues=issues,
        )
        state = _load_state(store, decision, issues)
        _refresh_skill_snapshots(
            store,
            reports=state.reports,
            claims=state.claims,
            outcomes=state.outcomes,
            truth_observations=state.truth_observations,
            daily_bars=state.daily_bars,
            config=config,
            decision=decision,
            issues=issues,
        )
        state = _load_state(store, decision, issues)
        _append_official_truth_issues(
            state=state,
            config=config,
            dimensions=selected_dimensions,
            issues=issues,
        )

    return _write_state_bundle(
        state=state,
        config=config,
        paths=paths,
        as_of=as_of_text,
        command="audit",
        issues=issues,
        parameters={
            "dimensions": list(selected_dimensions),
            "as_of": as_of_text,
            "offline": bool(offline),
            "truth_inputs": [str(path) for path in resolved_truth_inputs],
            "current_feed_start": _current_feed_start(config, decision).isoformat(),
        },
    )


def build_factor(
    *,
    as_of: Any = None,
    config_path: Path | str | None = None,
    db_path: Path | str | None = None,
    cache_directory: Path | str | None = None,
    output_directory: Path | str | None = None,
    factor_specifications: Iterable[Mapping[str, Any]] | None = None,
    factor_input_path: Path | str | None = None,
    factor_research_rows: Iterable[Mapping[str, Any]] | None = None,
    trading_calendar_path: Path | str | None = None,
) -> ReportBundle:
    """Build auditable three-layer observations from data already in the store."""

    config = load_config(config_path)
    paths = resolve_runtime_paths(
        config,
        config_path=config_path,
        db_path=db_path,
        cache_directory=cache_directory,
        output_directory=output_directory,
    )
    resolved_as_of = as_of or config.get("dates", {}).get("evaluation_as_of")
    decision = _decision_time(resolved_as_of)
    as_of_text = decision.date().isoformat()
    issues: list[dict[str, Any]] = []
    specifications = list(
        factor_specifications
        if factor_specifications is not None
        else config.get("factor_research", {}).get("specifications", [])
    )
    research_rows: list[dict[str, Any]] = []
    trading_calendar: tuple[date, ...] = ()
    internal_batch: Any | None = None
    if factor_research_rows is not None:
        research_rows = [dict(row) for row in factor_research_rows]
    else:
        configured_input = factor_input_path
        if configured_input is None:
            configured_input = config.get("factor_research", {}).get("input_path") or None
        if configured_input is not None:
            try:
                research_rows = load_factor_research_rows(configured_input)
            except ConfigurationError as exc:
                issues.append(
                    _issue(
                        "WALK_FORWARD_INPUT_LOAD_FAILED",
                        "历史因子输入无法读取；Walk-forward 保持不准入。",
                        stage="walk_forward",
                        severity="error",
                        details={"error": str(exc)},
                    )
                )
    configured_calendar = trading_calendar_path
    if configured_calendar is None:
        configured_calendar = (
            config.get("factor_research", {}).get("trading_calendar_path") or None
        )
        if configured_calendar is not None:
            configured_path = Path(configured_calendar)
            if not configured_path.is_absolute():
                configured_calendar = paths.config.parent / configured_path
    if configured_calendar is not None:
        try:
            trading_calendar = load_trading_calendar(configured_calendar)
        except ConfigurationError as exc:
            issues.append(
                _issue(
                    "TRADING_CALENDAR_LOAD_FAILED",
                    "显式交易日历无法读取；不得用工作日近似替代周频purge/embargo。",
                    stage="walk_forward",
                    severity="error",
                    details={"error": str(exc)},
                )
            )

    from .storage import AuditStore

    with AuditStore(paths.database, decision_time=decision) as store:
        state = _load_state(store, decision, issues)
        dates = config.get("dates", {})
        validation_population = [
            report
            for report in state.reports
            if _report_date(report) is not None
            and _date_value(dates.get("sample_start"), "dates.sample_start")
            <= _report_date(report)
            <= _date_value(dates.get("sample_end"), "dates.sample_end")  # type: ignore[operator]
        ]
        _hydrate_extractor_validation(
            config,
            decision=decision,
            config_path=paths.config,
            population_reports=validation_population,
            population_claims=_claims_with_current_evidence(
                state.claims, reports=validation_population
            ),
            issues=issues,
        )
        _append_extractor_validation_issues(config, VALID_DIMENSIONS, issues)
        _refresh_skill_snapshots(
            store,
            reports=state.reports,
            claims=state.claims,
            outcomes=state.outcomes,
            truth_observations=state.truth_observations,
            daily_bars=state.daily_bars,
            config=config,
            decision=decision,
            issues=issues,
        )
        state = _load_state(store, decision, issues)
        _build_factor_observations(
            store,
            state=state,
            config=config,
            decision=decision,
            issues=issues,
            specifications=specifications,
        )
        state = _load_state(store, decision, issues)
        internal_batch = _build_internal_factor_batch(
            store,
            state=state,
            config=config,
            decision=decision,
            issues=issues,
            specifications=specifications,
            trading_calendar=trading_calendar,
        )

    walk_forward_result = _run_walk_forward(
        research_rows,
        config=config,
        issues=issues,
        evaluation_as_of=decision,
        trading_calendar=trading_calendar,
        internal_batch=internal_batch,
    )
    dashboard = build_dashboard_data(
        [*specifications, *research_rows, *state.factor_observations]
    )

    return _write_state_bundle(
        state=state,
        config=config,
        paths=paths,
        as_of=as_of_text,
        command="build-factor",
        issues=issues,
        walk_forward_result=walk_forward_result,
        dashboard=dashboard,
        additional_input_snapshot={
            "factor_specifications": specifications,
            "factor_research_rows": research_rows,
            "trading_calendar": [day.isoformat() for day in trading_calendar],
            "internal_label_evidence_hash": (
                getattr(internal_batch, "evidence_hash", "") if internal_batch else ""
            ),
        },
        parameters={
            "as_of": as_of_text,
            "factor_specification_count": len(specifications),
            "factor_research_row_count": len(research_rows),
            "trading_calendar_count": len(trading_calendar),
            "internal_factor_research_row_count": (
                len(getattr(internal_batch, "rows", ())) if internal_batch else 0
            ),
        },
    )


def deep_read(
    *,
    as_of: Any = None,
    limit: int = 20,
    offline: bool = False,
    config_path: Path | str | None = None,
    db_path: Path | str | None = None,
    cache_directory: Path | str | None = None,
    output_directory: Path | str | None = None,
) -> ReportBundle:
    """Build the current evidence-gated deep-read queue from local records."""

    config = load_config(config_path)
    paths = resolve_runtime_paths(
        config,
        config_path=config_path,
        db_path=db_path,
        cache_directory=cache_directory,
        output_directory=output_directory,
    )
    maximum = int(config.get("deep_read", {}).get("maximum_limit", 20))
    if limit < 0:
        raise ConfigurationError("deep-read limit cannot be negative")
    resolved_limit = min(int(limit), maximum, 20)
    resolved_as_of = as_of or config.get("dates", {}).get("evaluation_as_of")
    decision = _decision_time(resolved_as_of)
    as_of_text = decision.date().isoformat()
    issues: list[dict[str, Any]] = []
    report_trading_calendar = _configured_trading_calendar(
        config, config_path=paths.config, issues=issues
    )

    from .storage import AuditStore

    with AuditStore(paths.database, decision_time=decision) as store:
        if offline:
            issues.append(
                _issue(
                    "OFFLINE_MODE",
                    "已禁用网络；深读榜仅使用本地当前报告与PDF缓存。",
                    stage="ingestion",
                    severity="info",
                )
            )
        else:
            _ingest_online(
                store,
                config=config,
                dimensions=VALID_DIMENSIONS,
                start_date=_current_feed_start(config, decision),
                end_date=decision.date(),
                decision=decision,
                cache_directory=paths.cache_directory,
                issues=issues,
                collection_scope="current",
                trading_calendar=report_trading_calendar,
            )
        state = _load_state(store, decision, issues)
        current_start = _current_feed_start(config, decision)
        current_reports = [
            report
            for report in state.reports
            if _report_date(report) is not None
            and current_start <= _report_date(report) <= decision.date()  # type: ignore[operator]
        ]
        deduplicated = _deduplicate_reports(current_reports, issues)
        _extract_claims(
            store,
            reports=deduplicated,
            existing_claims=state.claims,
            config=config,
            issues=issues,
            emit_unscorable=False,
        )
        state = _load_state(store, decision, issues)
        pdf_reports = [
            report for report in deduplicated if _report_has_resolvable_pdf(report)
        ]
        pdf_texts = _extract_pdf_texts(
            pdf_reports,
            store=store,
            cache_directory=paths.cache_directory,
            offline=offline,
            decision=decision,
            issues=issues,
        )
        if pdf_texts:
            state = _load_state(store, decision, issues)
            enriched_pdf_reports = [
                report
                for report in state.reports
                if _identifier(report, "report_id", "id") in pdf_texts
            ]
            _extract_claims(
                store,
                reports=enriched_pdf_reports,
                existing_claims=state.claims,
                config=config,
                issues=issues,
                text_by_report_id=pdf_texts,
                retry_completed=True,
                emit_unscorable=False,
            )
        state = _load_state(store, decision, issues)
    dates = config.get("dates", {})
    validation_population = [
        report
        for report in state.reports
        if _report_date(report) is not None
        and _date_value(dates.get("sample_start"), "dates.sample_start")
        <= _report_date(report)
        <= _date_value(dates.get("sample_end"), "dates.sample_end")  # type: ignore[operator]
    ]
    _hydrate_extractor_validation(
        config,
        decision=decision,
        config_path=paths.config,
        population_reports=validation_population,
        population_claims=_claims_with_current_evidence(
            state.claims, reports=validation_population
        ),
        issues=issues,
    )
    _append_extractor_validation_issues(config, VALID_DIMENSIONS, issues)
    candidates = _rank_deep_reads(
        state=state,
        config=config,
        decision=decision,
        limit=resolved_limit,
        issues=issues,
    )
    dashboard = build_dashboard_data([*state.factor_observations, *candidates])
    return _write_state_bundle(
        state=state,
        config=config,
        paths=paths,
        as_of=as_of_text,
        command="deep-read",
        issues=issues,
        deep_read_candidates=candidates,
        dashboard=dashboard,
        parameters={
            "as_of": as_of_text,
            "limit": resolved_limit,
            "offline": bool(offline),
            "current_feed_start": _current_feed_start(config, decision).isoformat(),
        },
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m research.broker_report_audit",
        description="Audit public broker reports across macro, industry and stock dimensions.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--as-of", help="Research cutoff date (YYYY-MM-DD); defaults to config.")
    common.add_argument("--config", help="V1 JSON config path.")
    common.add_argument("--db", help="SQLite audit store path.")
    common.add_argument("--cache-dir", help="Content-addressed HTTP cache directory.")
    common.add_argument("--output-dir", help="Directory for the fixed report bundle.")

    audit_parser = commands.add_parser("audit", parents=[common], help="Collect/evaluate the three audit dimensions.")
    audit_parser.add_argument(
        "--dimensions",
        nargs="+",
        default=list(VALID_DIMENSIONS),
        help="Comma-separated or space-separated: macro, industry, stock.",
    )
    audit_parser.add_argument("--offline", action="store_true", help="Forbid all network access.")
    audit_parser.add_argument(
        "--truth-input",
        action="append",
        default=None,
        help=(
            "Repeatable local JSON/JSONL/CSV truth manifest; local files and "
            "revisions are stored for diagnostics but cannot self-certify formal truth."
        ),
    )

    factor_parser = commands.add_parser("build-factor", parents=[common], help="Build three-layer report factors from local data.")
    factor_parser.add_argument(
        "--factor-input",
        help="Optional JSON/JSONL/CSV feature-and-label rows for Walk-forward evaluation.",
    )
    factor_parser.add_argument(
        "--trading-calendar",
        help="Optional JSON/JSONL/CSV explicit exchange trading dates.",
    )
    deep_parser = commands.add_parser("deep-read", parents=[common], help="Rank the current evidence-gated reading queue.")
    deep_parser.add_argument("--limit", type=int, default=20, help="Maximum queue length; hard-capped at 20.")
    deep_parser.add_argument("--offline", action="store_true", help="Forbid all network access.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "audit":
            bundle = run_audit(
                dimensions=args.dimensions,
                as_of=args.as_of,
                offline=args.offline,
                config_path=args.config,
                db_path=args.db,
                cache_directory=args.cache_dir,
                output_directory=args.output_dir,
                truth_input_paths=args.truth_input,
            )
        elif args.command == "build-factor":
            bundle = build_factor(
                as_of=args.as_of,
                config_path=args.config,
                db_path=args.db,
                cache_directory=args.cache_dir,
                output_directory=args.output_dir,
                factor_input_path=args.factor_input,
                trading_calendar_path=args.trading_calendar,
            )
        else:
            bundle = deep_read(
                as_of=args.as_of,
                limit=args.limit,
                offline=args.offline,
                config_path=args.config,
                db_path=args.db,
                cache_directory=args.cache_dir,
                output_directory=args.output_dir,
            )
    except ConfigurationError as exc:
        parser.error(str(exc))
        return 2
    except Exception as exc:
        print(f"broker-report-audit failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"run_id={bundle.run_id}")
    print(f"output_directory={bundle.output_directory}")
    for name in ARTIFACT_FILENAMES:
        print(f"wrote={bundle.paths[name]}")
    return 0


__all__ = [
    "ConfigurationError",
    "DEFAULT_CONFIG_PATH",
    "PipelineState",
    "RuntimePaths",
    "build_factor",
    "build_parser",
    "deep_read",
    "load_config",
    "load_factor_research_rows",
    "load_trading_calendar",
    "main",
    "parse_dimensions",
    "resolve_runtime_paths",
    "run_audit",
]
