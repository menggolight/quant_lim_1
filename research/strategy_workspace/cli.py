"""Small public CLI for factor preregistration and diagnostic backtests."""

from __future__ import annotations

import argparse
import csv
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
import hashlib
import json
from pathlib import Path
import platform
import sys
from typing import Any, Mapping, Sequence

import numpy as np

from .backtest import (
    BACKTEST_ENGINE_VERSION,
    BacktestConfig,
    CostModel,
    run_backtest,
)
from .catalog import DEFAULT_CATALOG_SHA256, DEFAULT_FACTOR_CATALOG, get_factor
from .choice_gate import (
    ChoiceCapabilityReceipt,
    evaluate_choice_quality_growth_gate,
)
from .contracts import DiscoveryStatus, ThesisSpec, canonical_sha256
from .discovery import freeze_plan, generate_candidates
from .experiment import ExperimentSpecV2, write_new_experiment_spec
from .industry import (
    INDUSTRY_ADAPTER_VERSION,
    build_relative_momentum_scores,
    build_relative_momentum_signals,
    load_csi_industry_evidence,
)
from .policy import load_quality_growth_policy
from .status import build_current_status


DEFAULT_HYPOTHESIS_PATH = Path(
    "configs/factor_hypotheses/csi11_relative_momentum.v1.json"
)
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONTROLLED_INDEX_EVIDENCE_ROOT = REPOSITORY_ROOT / "data" / "factor_evidence"
EXPECTED_DIAGNOSTIC_ARTIFACTS = frozenset(
    {
        "result.json",
        "report.md",
        "nav.csv",
        "trades.csv",
        "skips.csv",
        "signals.csv",
        "factor_scores.csv",
    }
)


class CliError(ValueError):
    """Raised when a CLI operation cannot produce a controlled artifact."""


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _jsonable(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            _jsonable(value),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_new(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(content)
    except FileExistsError as exc:
        raise CliError(f"refusing to overwrite existing artifact: {path}") from exc


def _load_json_object(path: str | Path, *, label: str) -> Mapping[str, Any]:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CliError(f"cannot read {label}: {source}") from exc
    if not isinstance(payload, Mapping):
        raise CliError(f"{label} root must be an object")
    return payload


def _require_new_directory(path: Path) -> None:
    if path.exists():
        raise CliError(f"output directory already exists: {path}")
    path.mkdir(parents=True, exist_ok=False)


def _load_frozen_plan(path: str | Path, *, required_factor_id: str) -> Mapping[str, Any]:
    plan_path = Path(path)
    try:
        plan_raw = plan_path.read_bytes()
        payload = json.loads(plan_raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CliError(f"cannot read frozen discovery plan: {plan_path}") from exc
    if not isinstance(payload, Mapping):
        raise CliError("frozen discovery plan root must be an object")
    if payload.get("catalog_sha256") != DEFAULT_CATALOG_SHA256:
        raise CliError("frozen discovery plan catalog_sha256 mismatch")
    plan = payload.get("plan")
    if not isinstance(plan, Mapping):
        raise CliError("frozen discovery plan payload is missing")
    if plan.get("status") != DiscoveryStatus.FROZEN.value:
        raise CliError("diagnostic requires a frozen_research_only plan")
    declared_sha = plan.get("plan_sha256")
    if not isinstance(declared_sha, str):
        raise CliError("frozen discovery plan plan_sha256 is missing")
    content = dict(plan)
    content.pop("plan_sha256", None)
    if canonical_sha256(content) != declared_sha:
        raise CliError("frozen discovery plan plan_sha256 mismatch")
    factors = plan.get("factors")
    if not isinstance(factors, list) or not factors:
        raise CliError("frozen discovery plan factors must be non-empty")
    validated_factors: dict[str, Mapping[str, Any]] = {}
    for factor in factors:
        if not isinstance(factor, Mapping):
            raise CliError("frozen discovery plan contains an invalid factor")
        factor_id = str(factor.get("factor_id") or "")
        try:
            catalog_factor = get_factor(factor_id)
        except ValueError as exc:
            raise CliError(f"frozen plan contains unknown factor_id: {factor_id}") from exc
        if dict(factor) != catalog_factor.to_dict():
            raise CliError(f"frozen factor definition mismatch: {factor_id}")
        validated_factors[factor_id] = factor
    if required_factor_id not in validated_factors:
        raise CliError(f"frozen plan does not contain required factor: {required_factor_id}")
    if plan.get("horizon_days") != 20:
        raise CliError("v1 CSI diagnostic requires a frozen 20-day horizon")
    return {
        "plan_file_sha256": _sha256_bytes(plan_raw),
        "plan_sha256": declared_sha,
        "catalog_sha256": DEFAULT_CATALOG_SHA256,
        "factor": dict(validated_factors[required_factor_id]),
    }


def _source_code_hashes() -> dict[str, str]:
    source_dir = Path(__file__).resolve().parent
    files = {
        "research/strategy_workspace/attribution.py": source_dir / "attribution.py",
        "research/strategy_workspace/backtest.py": source_dir / "backtest.py",
        "research/strategy_workspace/industry.py": source_dir / "industry.py",
        "research/strategy_workspace/catalog.py": source_dir / "catalog.py",
        "research/strategy_workspace/contracts.py": source_dir / "contracts.py",
        "research/strategy_workspace/cli.py": source_dir / "cli.py",
        "research/market_data/index_evidence.py": (
            REPOSITORY_ROOT / "research" / "market_data" / "index_evidence.py"
        ),
    }
    return {name: _sha256_bytes(path.read_bytes()) for name, path in files.items()}


def _command_catalog(_: argparse.Namespace) -> int:
    payload = {
        "schema_version": "strategy-workspace-factor-catalog.v1",
        "catalog_sha256": DEFAULT_CATALOG_SHA256,
        "factors": [item.to_dict() for item in DEFAULT_FACTOR_CATALOG],
    }
    sys.stdout.buffer.write(_json_bytes(payload))
    return 0


def _command_quality_status(args: argparse.Namespace) -> int:
    """Bind executed real-provider probes without promoting failed connectivity."""

    policy = load_quality_growth_policy(args.policy)
    status = build_current_status(
        policy,
        daily_bar_probe=args.daily_bar_probe,
        trade_calendar_probe=args.trade_calendar_probe,
        historical_sector_probe=args.historical_sector_probe,
    )
    _write_new(Path(args.output), _json_bytes(status.to_dict()))
    print(status.formal_status)
    return 2


def _command_choice_gate(args: argparse.Namespace) -> int:
    """Evaluate a controlled Choice capability receipt; never probe or log in."""

    receipt = ChoiceCapabilityReceipt.from_dict(
        _load_json_object(args.receipt, label="Choice capability receipt")
    )
    evaluation = evaluate_choice_quality_growth_gate(receipt)
    payload = {
        "schema_version": "strategy-workspace-choice-gate-evaluation.v1",
        "strategy_id": "a-share-small-account-quality-growth-v1",
        "evaluation": evaluation.to_dict(),
        "interpretation": {
            "contract_satisfied_is_not_live_connectivity": True,
            "contract_satisfied_is_not_formal_truth_admission": True,
        },
        "safety": {
            "paper_eligibility": False,
            "trade_eligibility": False,
            "real_money_list_allowed": False,
            "live": "not_supported",
        },
    }
    payload["evaluation_sha256"] = canonical_sha256(payload)
    _write_new(Path(args.output), _json_bytes(payload))
    print(evaluation.status.value)
    return 0 if evaluation.contract_satisfied else 2


def _command_freeze_experiment(args: argparse.Namespace) -> int:
    """Freeze one append-only ExperimentSpec v2 before reading future labels."""

    spec = ExperimentSpecV2.create(
        _load_json_object(args.input, label="experiment specification input")
    )
    write_new_experiment_spec(args.output, spec)
    print(spec.spec_sha256)
    return 0


def _command_fallback_sample(args: argparse.Namespace) -> int:
    """Reject the legacy caller-authored receipt/hash shortcut.

    The controlled replacement replays both archived workbooks and receipts:
    ``python -m agent.current_industry_import freeze-sample``.
    """

    raise CliError(
        "uncontrolled current-universe JSON is disabled; use "
        "agent.current_industry_import freeze-sample with verified membership "
        "and industry import directories"
    )


def _command_preregister(args: argparse.Namespace) -> int:
    thesis = ThesisSpec(
        thesis_id=args.thesis_id,
        viewpoint=args.viewpoint,
        mechanisms=tuple(args.mechanism),
        horizon_days=args.horizon_days,
    )
    generated = generate_candidates(thesis)
    plan = generated if generated.status is DiscoveryStatus.BLOCKED else freeze_plan(generated)
    payload = {
        "catalog_sha256": DEFAULT_CATALOG_SHA256,
        "plan": plan.to_dict(),
        "safety": {
            "mode": "research_only",
            "paper_eligibility": False,
            "live": "not_supported",
        },
    }
    _write_new(Path(args.output), _json_bytes(payload))
    print(plan.status.value)
    return 2 if plan.status is DiscoveryStatus.BLOCKED else 0


def _rows_bytes(fieldnames: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> bytes:
    from io import StringIO

    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(fieldnames), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({name: _jsonable(row.get(name)) for name in fieldnames})
    return stream.getvalue().encode("utf-8")


def _metrics_report(payload: Mapping[str, Any]) -> str:
    metrics = payload["metrics"]
    source = payload["source"]
    costs = payload["configuration"]["costs"]
    cost_breakdown = payload["cost_breakdown"]
    initial_cash = float(payload["configuration"]["initial_cash"])
    total_cost = float(metrics["total_cost"])
    lines = [
        "# CSI 行业相对动量诊断回测",
        "",
        "- 状态：`diagnostic_only_non_tradable_index`",
        "- 安全边界：`research_only_no_trading_bridge`",
        f"- 数据准入：`{source['admission_status']}`",
        f"- 受控存储重放：`{str(source['controlled_storage_verified']).lower()}`",
        f"- 时点状态：`{source['point_in_time_status']}`",
        f"- 因子：`RM{payload['configuration']['lookback_sessions']}`",
        f"- 冻结计划：`{payload['research_binding']['plan_sha256']}`",
        "- 预注册时序：`retrospective_binding_not_fresh_holdout`",
        f"- 调仓：每 {payload['configuration']['rebalance_sessions']} 个交易日，最多 {payload['configuration']['top_n']} 个行业",
        "",
        "## 结果",
        "",
        f"- 区间：{metrics['start_date']} 至 {metrics['end_date']}",
        f"- 成本算术加回收益（非零成本反事实）：{float(metrics['cost_addback_return']):.4%}",
        f"- 净收益：{float(metrics['net_return']):.4%}",
        f"- 基准收益：{float(metrics['benchmark_return']):.4%}" if metrics["benchmark_return"] is not None else "- 基准收益：不可用",
        f"- 主动收益：{float(metrics['active_return']):.4%}" if metrics["active_return"] is not None else "- 主动收益：不可用",
        f"- 年化收益：{float(metrics['annualized_return']):.4%}" if metrics["annualized_return"] is not None else "- 年化收益：不可用",
        f"- 最大回撤：{float(metrics['max_drawdown']):.4%}",
        f"- 累计双边换手：{float(metrics['turnover']):.4f}",
        f"- 仿真成本压力金额：{metrics['total_cost']} 元（占初始资金 {total_cost / initial_cash:.4%}）",
        f"  - 佣金：{cost_breakdown['commission']} 元",
        f"  - 卖出税：{cost_breakdown['sell_tax']} 元",
        f"  - 过户费：{cost_breakdown['transfer_fee']} 元",
        f"  - 滑点：{cost_breakdown['slippage']} 元",
        "",
        "## 成本假设",
        "",
        f"- 初始仿真资金：{payload['configuration']['initial_cash']} 元，现金保留比例：{payload['configuration']['cash_reserve_ratio']}",
        f"- 佣金率：{costs['commission_rate']}，单笔最低：{costs['minimum_commission']}",
        f"- 卖出税率：{costs['sell_tax_rate']}，双边过户费率：{costs['transfer_fee_rate']}",
        f"- 单边滑点：{costs['slippage_bps']} bps",
        "- 以上费率作为整段历史的恒定反事实假设，不是逐日历史费率回放。",
        "",
        "## 解释边界",
        "",
        "行业指数不是可交易证券；本报告只验证历史指数数据上的仿真账本和成本压力。",
        "它不是 ETF 回测、统计准入、Paper 准入或买入建议。",
        "",
    ]
    return "\n".join(lines)


def _command_csi_diagnostic(args: argparse.Namespace) -> int:
    if args.lookback_sessions != 20:
        raise CliError("v1 CSI diagnostic is precommitted to the RM20 baseline")
    required_factor_id = f"RM{args.lookback_sessions}"
    frozen_plan = _load_frozen_plan(
        args.plan,
        required_factor_id=required_factor_id,
    )
    evidence = load_csi_industry_evidence(
        args.evidence,
        args.hypothesis,
        mapping_key=args.mapping_key,
        evidence_root=CONTROLLED_INDEX_EVIDENCE_ROOT,
    )
    signals = build_relative_momentum_signals(
        evidence,
        lookback_sessions=args.lookback_sessions,
        rebalance_sessions=args.rebalance_sessions,
        top_n=args.top_n,
    )
    factor_scores = build_relative_momentum_scores(
        evidence,
        lookback_sessions=args.lookback_sessions,
        rebalance_sessions=args.rebalance_sessions,
        top_n=args.top_n,
    )
    config = BacktestConfig(
        initial_cash=Decimal(args.initial_cash),
        cash_reserve_ratio=Decimal(args.cash_reserve_ratio),
        max_positions=args.top_n,
        costs=CostModel(
            commission_rate=Decimal(args.commission_rate),
            minimum_commission=Decimal(args.minimum_commission),
            sell_tax_rate=Decimal(args.sell_tax_rate),
            transfer_fee_rate=Decimal(args.transfer_fee_rate),
            slippage_bps=Decimal(args.slippage_bps),
        ),
    )
    result = run_backtest(
        signals,
        evidence.bars,
        benchmark=evidence.benchmark,
        config=config,
    )
    cost_breakdown = {
        "commission": sum((item.commission for item in result.trades), Decimal("0")),
        "sell_tax": sum((item.sell_tax for item in result.trades), Decimal("0")),
        "transfer_fee": sum(
            (item.transfer_fee for item in result.trades), Decimal("0")
        ),
        "slippage": sum(
            (item.slippage_cost for item in result.trades), Decimal("0")
        ),
    }
    payload = {
        "schema_version": "strategy-workspace-diagnostic-result.v1",
        "status": "diagnostic_only_non_tradable_index",
        "source": {
            "evidence_sha256": evidence.evidence_sha256,
            "hypothesis_sha256": evidence.hypothesis_sha256,
            "controlled_storage_verified": evidence.controlled_storage_verified,
            "receipt_verified": evidence.receipt_verified,
            "receipt_sha256": evidence.receipt_sha256,
            "admission_status": evidence.admission_status,
            "point_in_time_status": evidence.point_in_time_status,
            "benchmark_id": evidence.benchmark_id,
            "industry_ids": list(evidence.industry_ids),
        },
        "configuration": {
            "lookback_sessions": args.lookback_sessions,
            "rebalance_sessions": args.rebalance_sessions,
            "top_n": args.top_n,
            "mapping_key": args.mapping_key,
            "initial_cash": str(config.initial_cash),
            "cash_reserve_ratio": str(config.cash_reserve_ratio),
            "costs": _jsonable(config.costs),
        },
        "research_binding": {
            **frozen_plan,
            "selection_policy": "v1_fixed_rm20_baseline",
            "temporal_status": "retrospective_binding_not_fresh_holdout",
        },
        "runtime": {
            "backtest_engine_version": BACKTEST_ENGINE_VERSION,
            "industry_adapter_version": INDUSTRY_ADAPTER_VERSION,
            "python_version": platform.python_version(),
            "numpy_version": np.__version__,
            "source_code_sha256": _source_code_hashes(),
        },
        "signal_count": len(signals),
        "cost_breakdown": _jsonable(cost_breakdown),
        "metrics": _jsonable(result.metrics),
        "attribution": _jsonable(result.attribution.summary),
        "ending_positions": _jsonable(result.ending_positions),
        "safety": {
            "paper_eligibility": False,
            "trade_eligibility": False,
            "live": "not_supported",
        },
    }

    nav_rows = [_jsonable(item) for item in result.nav]
    trade_rows = [_jsonable(item) for item in result.trades]
    skip_rows = [_jsonable(item) for item in result.skips]
    signal_rows = [
        {
            "signal_id": item.signal_id,
            "signal_date": item.signal_date,
            "factor_id": required_factor_id,
            "selected_instrument_ids": "|".join(item.instrument_ids),
            "execution_policy": "next_controlled_session_close",
        }
        for item in signals
    ]
    factor_score_rows = [_jsonable(item) for item in factor_scores]
    artifacts = {
        "result.json": _json_bytes(payload),
        "report.md": _metrics_report(payload).encode("utf-8"),
        "nav.csv": _rows_bytes(
            ("trading_date", "net_nav", "cost_addback_nav", "cash", "market_value", "cumulative_cost", "benchmark_close"),
            nav_rows,
        ),
        "trades.csv": _rows_bytes(
            (
                "trade_id", "signal_id", "signal_date", "execution_date", "instrument_id", "side",
                "quantity", "lot_size", "reference_close", "fill_price", "notional", "commission",
                "sell_tax", "transfer_fee", "slippage_cost", "total_cost", "cash_after",
            ),
            trade_rows,
        ),
        "skips.csv": _rows_bytes(
            ("signal_id", "signal_date", "execution_date", "instrument_id", "side", "reason_code", "detail"),
            skip_rows,
        ),
        "signals.csv": _rows_bytes(
            (
                "signal_id",
                "signal_date",
                "factor_id",
                "selected_instrument_ids",
                "execution_policy",
            ),
            signal_rows,
        ),
        "factor_scores.csv": _rows_bytes(
            (
                "signal_id",
                "signal_date",
                "factor_id",
                "instrument_id",
                "score",
                "rank",
                "selected",
                "input_available_at_max",
            ),
            factor_score_rows,
        ),
    }
    manifest_content = {
        "schema_version": "strategy-workspace-run-manifest.v1",
        "run_id": canonical_sha256(payload),
        "status": payload["status"],
        "source_evidence_sha256": evidence.evidence_sha256,
        "source_hypothesis_sha256": evidence.hypothesis_sha256,
        "discovery_plan_sha256": frozen_plan["plan_sha256"],
        "catalog_sha256": frozen_plan["catalog_sha256"],
        "runtime": payload["runtime"],
        "artifacts": {name: _sha256_bytes(content) for name, content in artifacts.items()},
        "safety": payload["safety"],
    }
    artifacts["run_manifest.json"] = _json_bytes(manifest_content)

    output = Path(args.output)
    _require_new_directory(output)
    for name, content in artifacts.items():
        _write_new(output / name, content)
    print(manifest_content["run_id"])
    return 0


def _command_verify(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    resolved_run_dir = run_dir.resolve()
    manifest_path = run_dir / "run_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CliError(f"cannot read manifest: {manifest_path}") from exc
    if not isinstance(manifest, Mapping):
        raise CliError("manifest root must be an object")
    expected = manifest.get("artifacts")
    if not isinstance(expected, Mapping) or not expected:
        raise CliError("manifest artifacts must be a non-empty object")
    manifest_schema = manifest.get("schema_version")
    if manifest_schema != "strategy-workspace-run-manifest.v1":
        raise CliError("unsupported manifest schema_version")
    failures = []
    if set(expected) != EXPECTED_DIAGNOSTIC_ARTIFACTS:
        failures.append("diagnostic_artifact_set_mismatch")
    for name, expected_sha in expected.items():
        name = str(name)
        relative_name = Path(name)
        if (
            not name
            or relative_name.is_absolute()
            or relative_name.name != name
            or not isinstance(expected_sha, str)
            or len(expected_sha) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha)
        ):
            failures.append(f"invalid_manifest_artifact:{name}")
            continue
        artifact = (run_dir / relative_name).resolve()
        if artifact.parent != resolved_run_dir:
            failures.append(f"artifact_outside_run_dir:{name}")
            continue
        try:
            actual_sha = _sha256_bytes(artifact.read_bytes())
        except OSError:
            failures.append(f"missing:{name}")
            continue
        if actual_sha != expected_sha:
            failures.append(f"sha256_mismatch:{name}")
    result_path = run_dir / "result.json"
    try:
        result_payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        failures.append("invalid_result_json")
    else:
        if not isinstance(result_payload, Mapping):
            failures.append("result_root_not_object")
        elif canonical_sha256(result_payload) != manifest.get("run_id"):
            failures.append("run_id_mismatch")
        if isinstance(result_payload, Mapping) and result_payload.get("status") != manifest.get("status"):
            failures.append("status_mismatch")
        source = result_payload.get("source") if isinstance(result_payload, Mapping) else None
        source_sha = source.get("evidence_sha256") if isinstance(source, Mapping) else None
        if source_sha != manifest.get("source_evidence_sha256"):
            failures.append("source_evidence_sha256_mismatch")
        hypothesis_sha = (
            source.get("hypothesis_sha256") if isinstance(source, Mapping) else None
        )
        if hypothesis_sha != manifest.get("source_hypothesis_sha256"):
            failures.append("source_hypothesis_sha256_mismatch")
        binding = (
            result_payload.get("research_binding")
            if isinstance(result_payload, Mapping)
            else None
        )
        if not isinstance(binding, Mapping):
            failures.append("research_binding_missing")
        else:
            if binding.get("plan_sha256") != manifest.get("discovery_plan_sha256"):
                failures.append("discovery_plan_sha256_mismatch")
            if binding.get("catalog_sha256") != manifest.get("catalog_sha256"):
                failures.append("catalog_sha256_mismatch")
        result_runtime = (
            result_payload.get("runtime")
            if isinstance(result_payload, Mapping)
            else None
        )
        if result_runtime != manifest.get("runtime"):
            failures.append("runtime_mismatch")
        if isinstance(result_payload, Mapping) and result_payload.get("safety") != manifest.get("safety"):
            failures.append("safety_mismatch")
    if failures:
        print(json.dumps({"verified": False, "failures": failures}, ensure_ascii=False))
        return 2
    print(json.dumps({"verified": True, "run_id": manifest.get("run_id")}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quant-strategy")
    subparsers = parser.add_subparsers(dest="command", required=True)

    catalog_parser = subparsers.add_parser("catalog", help="show the frozen factor catalog")
    catalog_parser.set_defaults(handler=_command_catalog)

    quality_status = subparsers.add_parser(
        "quality-status",
        help="bind real Choice probe artifacts and report the fail-closed strategy status",
    )
    quality_status.add_argument(
        "--policy", default=str(Path("configs/strategy_quality_growth.v1.json"))
    )
    quality_status.add_argument("--daily-bar-probe", required=True)
    quality_status.add_argument("--trade-calendar-probe", required=True)
    quality_status.add_argument("--historical-sector-probe", required=True)
    quality_status.add_argument("--output", required=True)
    quality_status.set_defaults(handler=_command_quality_status)

    choice_gate = subparsers.add_parser(
        "choice-gate",
        help="evaluate a controlled Choice quality-growth capability receipt",
    )
    choice_gate.add_argument("--receipt", required=True)
    choice_gate.add_argument("--output", required=True)
    choice_gate.set_defaults(handler=_command_choice_gate)

    freeze_experiment = subparsers.add_parser(
        "freeze-experiment",
        help="validate and append-only freeze an ExperimentSpec v2",
    )
    freeze_experiment.add_argument("--input", required=True)
    freeze_experiment.add_argument("--output", required=True)
    freeze_experiment.set_defaults(handler=_command_freeze_experiment)

    fallback_sample = subparsers.add_parser(
        "fallback-sample",
        help="freeze a non-PIT, non-Paper 60-name diagnostic sample",
    )
    fallback_sample.add_argument("--universe", required=True)
    fallback_sample.add_argument("--output", required=True)
    fallback_sample.set_defaults(handler=_command_fallback_sample)

    preregister = subparsers.add_parser("preregister", help="freeze a bounded discovery plan")
    preregister.add_argument("--thesis-id", required=True)
    preregister.add_argument("--viewpoint", required=True)
    preregister.add_argument("--mechanism", action="append", required=True)
    preregister.add_argument("--horizon-days", type=int, default=20)
    preregister.add_argument("--output", required=True)
    preregister.set_defaults(handler=_command_preregister)

    diagnostic = subparsers.add_parser(
        "csi-diagnostic", help="run a non-tradable CSI industry index diagnostic"
    )
    diagnostic.add_argument("--evidence", required=True)
    diagnostic.add_argument("--plan", required=True)
    diagnostic.add_argument("--hypothesis", default=str(DEFAULT_HYPOTHESIS_PATH))
    diagnostic.add_argument("--mapping-key", default="choice_screen")
    diagnostic.add_argument("--lookback-sessions", type=int, choices=(20,), default=20)
    diagnostic.add_argument("--rebalance-sessions", type=int, default=20)
    diagnostic.add_argument("--top-n", type=int, choices=(1, 2, 3), default=3)
    diagnostic.add_argument("--initial-cash", default="1000000")
    diagnostic.add_argument("--cash-reserve-ratio", default="0.10")
    diagnostic.add_argument("--commission-rate", default="0.00018")
    diagnostic.add_argument("--minimum-commission", default="5")
    diagnostic.add_argument("--sell-tax-rate", default="0.0005")
    diagnostic.add_argument("--transfer-fee-rate", default="0.00001")
    diagnostic.add_argument("--slippage-bps", default="5")
    diagnostic.add_argument("--output", required=True)
    diagnostic.set_defaults(handler=_command_csi_diagnostic)

    verify = subparsers.add_parser("verify", help="verify hashes in one controlled run")
    verify.add_argument("--run-dir", required=True)
    verify.set_defaults(handler=_command_verify)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (CliError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


__all__ = ["build_parser", "main"]
