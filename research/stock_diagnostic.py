"""Seal and verify forward, research-only stock diagnostic case cards.

This module deliberately does not fetch prices, rank stocks, or connect to any
trading component.  It freezes an already-computed finite screen so later
observations cannot replace a failed case or loosen the entry gates.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

from research.reproducibility import git_worktree_state


SCHEMA_VERSION = "stock-diagnostic-observation-v1"
MANIFEST_VERSION = "stock-diagnostic-manifest-v1"
PRODUCER = "stock-diagnostic-observation-cli-v1"
DEFAULT_SCHEMA = Path("schemas/stock_diagnostic_observation.v1.json")
REQUIRED_GATES = (
    "return_20d_gt_0",
    "return_60d_gt_0",
    "close_gt_ma20",
    "close_gt_ma60",
)
SAFETY = {
    "research_action": "observe_only",
    "trade_action": None,
    "paper_eligibility": False,
    "trade_eligibility": False,
    "formal_factor_eligibility": False,
    "live_execution_status": "live_not_supported",
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_INSTRUMENT_ID = re.compile(r"^[0-9]{6}\.(?:SH|SZ)$")


class StockDiagnosticError(ValueError):
    """Raised when a diagnostic card cannot be trusted."""


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(raw: bytes) -> str:
    return sha256(raw).hexdigest()


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StockDiagnosticError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise StockDiagnosticError(f"{label} must be a JSON object")
    return value, raw


def _parse_time(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise StockDiagnosticError(f"{field} must be a timezone-aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StockDiagnosticError(f"{field} is not a valid ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StockDiagnosticError(f"{field} must include a timezone offset")
    return parsed


def _require_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise StockDiagnosticError(f"{label} fields mismatch; missing={missing}, extra={extra}")


def _decimal(value: object, field: str) -> Decimal:
    if not isinstance(value, str):
        raise StockDiagnosticError(f"{field} must be a decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise StockDiagnosticError(f"{field} is not a decimal") from exc
    if not result.is_finite():
        raise StockDiagnosticError(f"{field} must be finite")
    return result


def _computed_gates(metrics: Mapping[str, Any], label: str) -> dict[str, bool]:
    _require_keys(
        metrics,
        {"close", "return_20d_pct", "return_60d_pct", "ma20", "ma60"},
        f"{label}.metrics",
    )
    close = _decimal(metrics["close"], f"{label}.close")
    return_20 = _decimal(metrics["return_20d_pct"], f"{label}.return_20d_pct")
    return_60 = _decimal(metrics["return_60d_pct"], f"{label}.return_60d_pct")
    ma20 = _decimal(metrics["ma20"], f"{label}.ma20")
    ma60 = _decimal(metrics["ma60"], f"{label}.ma60")
    if min(close, ma20, ma60) <= 0:
        raise StockDiagnosticError(f"{label} close and moving averages must be positive")
    return {
        "return_20d_gt_0": return_20 > 0,
        "return_60d_gt_0": return_60 > 0,
        "close_gt_ma20": close > ma20,
        "close_gt_ma60": close > ma60,
    }


def _validate_gate_results(
    claimed: object, metrics: Mapping[str, Any], label: str
) -> tuple[dict[str, bool], bool]:
    if not isinstance(claimed, dict):
        raise StockDiagnosticError(f"{label}.gate_results must be an object")
    _require_keys(claimed, set(REQUIRED_GATES), f"{label}.gate_results")
    if any(not isinstance(claimed[key], bool) for key in REQUIRED_GATES):
        raise StockDiagnosticError(f"{label}.gate_results must contain booleans")
    computed = _computed_gates(metrics, label)
    if claimed != computed:
        raise StockDiagnosticError(f"{label}.gate_results do not match frozen metrics")
    return computed, all(computed.values())


def _validate_candidate(candidate: object, index: int) -> tuple[str, bool]:
    if not isinstance(candidate, dict):
        raise StockDiagnosticError(f"candidate_pool[{index}] must be an object")
    _require_keys(
        candidate,
        {
            "instrument_id", "name", "snapshot_metrics", "gate_results",
            "snapshot_status", "selection_reason", "official_evidence",
            "invalidation_conditions",
        },
        f"candidate_pool[{index}]",
    )
    instrument_id = candidate["instrument_id"]
    if not isinstance(instrument_id, str) or _INSTRUMENT_ID.fullmatch(instrument_id) is None:
        raise StockDiagnosticError(f"candidate_pool[{index}].instrument_id is invalid")
    if not isinstance(candidate["name"], str) or not candidate["name"].strip():
        raise StockDiagnosticError(f"candidate_pool[{index}].name is required")
    metrics = candidate["snapshot_metrics"]
    if not isinstance(metrics, dict):
        raise StockDiagnosticError(f"candidate_pool[{index}].snapshot_metrics must be an object")
    _, passed = _validate_gate_results(candidate["gate_results"], metrics, f"candidate_pool[{index}]")
    expected_status = "selected_diagnostic_positive" if passed else "excluded_gate_failed"
    if candidate["snapshot_status"] != expected_status:
        raise StockDiagnosticError(
            f"candidate_pool[{index}].snapshot_status must be {expected_status}"
        )
    for field in ("selection_reason",):
        if not isinstance(candidate[field], str) or not candidate[field].strip():
            raise StockDiagnosticError(f"candidate_pool[{index}].{field} is required")
    for field in ("official_evidence", "invalidation_conditions"):
        if not isinstance(candidate[field], list):
            raise StockDiagnosticError(f"candidate_pool[{index}].{field} must be an array")
    for evidence_index, evidence in enumerate(candidate["official_evidence"]):
        if not isinstance(evidence, dict):
            raise StockDiagnosticError(
                f"candidate_pool[{index}].official_evidence[{evidence_index}] must be an object"
            )
        _require_keys(
            evidence,
            {"source_authority", "title", "published_at", "url", "evidence_status"},
            f"candidate_pool[{index}].official_evidence[{evidence_index}]",
        )
        if evidence["source_authority"] not in {"issuer", "exchange"}:
            raise StockDiagnosticError("official evidence source_authority is invalid")
        if evidence["evidence_status"] != "reference_only_not_archived":
            raise StockDiagnosticError("official evidence cannot claim archived source proof")
        if not isinstance(evidence["url"], str) or not evidence["url"].startswith("https://"):
            raise StockDiagnosticError("official evidence URL must use HTTPS")
    return instrument_id, passed


def _validate_safety(value: object) -> None:
    if value != SAFETY:
        raise StockDiagnosticError("safety boundary must remain research-only and LIVE unsupported")


def validate_observation(value: Mapping[str, Any]) -> None:
    """Validate semantic invariants that the JSON Schema cannot prove alone."""

    _require_keys(
        value,
        {
            "schema_version", "observation_id", "created_at", "decision_time",
            "information_cutoff_at", "status", "thesis", "selection_policy",
            "candidate_pool", "pre_entry_check", "evaluation", "safety", "pipeline",
        },
        "observation",
    )
    if value["schema_version"] != SCHEMA_VERSION:
        raise StockDiagnosticError("unsupported schema_version")
    observation_id = value["observation_id"]
    if not isinstance(observation_id, str) or re.fullmatch(r"[a-z0-9][a-z0-9-]{7,127}", observation_id) is None:
        raise StockDiagnosticError("observation_id is invalid")
    if value["status"] != "diagnostic_only_not_admitted":
        raise StockDiagnosticError("status must remain diagnostic_only_not_admitted")
    created_at = _parse_time(value["created_at"], "created_at")
    decision_time = _parse_time(value["decision_time"], "decision_time")
    cutoff = _parse_time(value["information_cutoff_at"], "information_cutoff_at")
    if cutoff >= decision_time:
        raise StockDiagnosticError("information_cutoff_at must precede decision_time")
    if created_at > decision_time:
        raise StockDiagnosticError("created_at cannot be after decision_time")

    thesis = value["thesis"]
    if not isinstance(thesis, dict):
        raise StockDiagnosticError("thesis must be an object")
    _require_keys(
        thesis,
        {"theme", "statement", "horizon_trading_sessions", "user_selection_constraint", "independence_warning"},
        "thesis",
    )
    if thesis["theme"] != "innovation_drug" or thesis["horizon_trading_sessions"] != 60:
        raise StockDiagnosticError("V1 thesis is frozen to innovation_drug and 60 sessions")

    policy = value["selection_policy"]
    if not isinstance(policy, dict):
        raise StockDiagnosticError("selection_policy must be an object")
    _require_keys(
        policy,
        {"snapshot_session", "price_source", "price_source_admission", "price_basis", "gates", "replacement_policy"},
        "selection_policy",
    )
    if policy["price_source_admission"] != "diagnostic_aggregator_not_registry_admitted":
        raise StockDiagnosticError("price source must remain diagnostic")
    if policy["replacement_policy"] != "no_replacement_after_snapshot":
        raise StockDiagnosticError("candidate replacement is forbidden")
    if set(policy["gates"]) != set(REQUIRED_GATES) or len(policy["gates"]) != 4:
        raise StockDiagnosticError("selection gates are frozen")

    candidates = value["candidate_pool"]
    if not isinstance(candidates, list) or len(candidates) < 2:
        raise StockDiagnosticError("candidate_pool must contain at least two cases")
    ids: list[str] = []
    original_selected: set[str] = set()
    for index, candidate in enumerate(candidates):
        instrument_id, passed = _validate_candidate(candidate, index)
        if instrument_id in ids:
            raise StockDiagnosticError("candidate_pool contains duplicate instrument_id")
        ids.append(instrument_id)
        if passed:
            original_selected.add(instrument_id)
    if len(original_selected) != 2:
        raise StockDiagnosticError("this frozen case card must preserve exactly two snapshot selections")

    pre_entry = value["pre_entry_check"]
    if not isinstance(pre_entry, dict):
        raise StockDiagnosticError("pre_entry_check must be an object")
    _require_keys(
        pre_entry,
        {"checked_at", "latest_complete_session", "source", "source_admission", "checks", "active_candidate_ids", "policy"},
        "pre_entry_check",
    )
    checked_at = _parse_time(pre_entry["checked_at"], "pre_entry_check.checked_at")
    if checked_at < decision_time:
        raise StockDiagnosticError("pre_entry_check cannot precede decision_time")
    if pre_entry["source_admission"] != "diagnostic_aggregator_not_registry_admitted":
        raise StockDiagnosticError("pre-entry source must remain diagnostic")
    if pre_entry["policy"] != "retain_original_cases_but_only_all_gate_passers_remain_active":
        raise StockDiagnosticError("pre-entry retention policy is frozen")
    checks = pre_entry["checks"]
    if not isinstance(checks, list) or not checks:
        raise StockDiagnosticError("pre_entry_check.checks must be non-empty")
    checked_ids: set[str] = set()
    computed_active: set[str] = set()
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            raise StockDiagnosticError(f"pre_entry_check.checks[{index}] must be an object")
        _require_keys(
            check,
            {"instrument_id", "metrics", "gate_results", "status", "reason"},
            f"pre_entry_check.checks[{index}]",
        )
        instrument_id = check["instrument_id"]
        if instrument_id not in original_selected or instrument_id in checked_ids:
            raise StockDiagnosticError("pre-entry checks must cover each original selection exactly once")
        checked_ids.add(instrument_id)
        metrics = check["metrics"]
        if not isinstance(metrics, dict):
            raise StockDiagnosticError("pre-entry metrics must be an object")
        _, passed = _validate_gate_results(
            check["gate_results"], metrics, f"pre_entry_check.checks[{index}]"
        )
        expected_status = "active_diagnostic_positive" if passed else "pre_entry_gate_failed"
        if check["status"] != expected_status:
            raise StockDiagnosticError(f"pre-entry status must be {expected_status}")
        if passed:
            computed_active.add(instrument_id)
    if checked_ids != original_selected:
        raise StockDiagnosticError("pre-entry checks must cover all original selections")
    active_ids = pre_entry["active_candidate_ids"]
    if not isinstance(active_ids, list) or set(active_ids) != computed_active or len(active_ids) != len(computed_active):
        raise StockDiagnosticError("active_candidate_ids do not match pre-entry gates")

    evaluation = value["evaluation"]
    if not isinstance(evaluation, dict):
        raise StockDiagnosticError("evaluation must be an object")
    _require_keys(
        evaluation,
        {
            "entry_session", "entry_status", "entry_price_policy", "horizon_trading_sessions",
            "exit_price_policy", "primary_benchmark_id", "context_benchmark_ids", "label_definition",
            "price_return_basis", "early_falsification_conditions", "early_failure_retention_policy",
            "maturity_outcomes",
        },
        "evaluation",
    )
    if evaluation["entry_status"] != "pending_official_close_capture":
        raise StockDiagnosticError("entry close is not yet captured")
    if evaluation["horizon_trading_sessions"] != 60:
        raise StockDiagnosticError("evaluation horizon must remain 60 sessions")
    if evaluation["primary_benchmark_id"] != "932082.CSI":
        raise StockDiagnosticError("primary benchmark must remain CSI health care 932082.CSI")
    if evaluation["early_failure_retention_policy"] != "append_failure_and_still_evaluate_fixed_60_session_endpoint":
        raise StockDiagnosticError("failed cases must remain in the fixed endpoint evaluation")
    if set(evaluation["maturity_outcomes"]) != {
        "direction_observed", "direction_not_observed", "data_insufficient"
    }:
        raise StockDiagnosticError("maturity outcome vocabulary is frozen")
    _validate_safety(value["safety"])

    pipeline = value["pipeline"]
    if not isinstance(pipeline, dict):
        raise StockDiagnosticError("pipeline must be an object")
    _require_keys(
        pipeline,
        {"producer", "standard_cli_generated", "sealed_at", "schema_path", "schema_sha256", "draft_sha256"},
        "pipeline",
    )
    if pipeline["producer"] != PRODUCER or pipeline["standard_cli_generated"] is not True:
        raise StockDiagnosticError("card was not generated by the standard CLI")
    sealed_at = _parse_time(pipeline["sealed_at"], "pipeline.sealed_at")
    if sealed_at < decision_time or sealed_at < checked_at:
        raise StockDiagnosticError("sealed_at cannot precede decision or pre-entry check")
    for field in ("schema_sha256", "draft_sha256"):
        if not isinstance(pipeline[field], str) or _SHA256.fullmatch(pipeline[field]) is None:
            raise StockDiagnosticError(f"pipeline.{field} must be SHA-256")


def _draft_to_sealed(
    draft: Mapping[str, Any], *, schema_path: Path, schema_sha256: str,
    draft_sha256: str, sealed_at: str,
) -> dict[str, Any]:
    if "pipeline" in draft:
        raise StockDiagnosticError("draft must not provide pipeline attestation")
    sealed = dict(draft)
    sealed["pipeline"] = {
        "producer": PRODUCER,
        "standard_cli_generated": True,
        "sealed_at": sealed_at,
        "schema_path": schema_path.as_posix(),
        "schema_sha256": schema_sha256,
        "draft_sha256": draft_sha256,
    }
    return sealed


def seal(
    draft_path: Path,
    *,
    schema_path: Path = DEFAULT_SCHEMA,
    signals_dir: Path = Path("data/signals"),
    actions_dir: Path = Path("data/actions"),
    workspace: Path = Path("."),
    now: datetime | None = None,
) -> tuple[Path, Path]:
    draft, draft_raw = _read_json(draft_path, "draft")
    schema, schema_raw = _read_json(schema_path, "schema")
    if schema.get("$id") != "https://quant.local/schemas/stock_diagnostic_observation.v1.json":
        raise StockDiagnosticError("unexpected stock diagnostic schema")
    timestamp = now or datetime.now().astimezone()
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise StockDiagnosticError("sealing clock must include a timezone offset")
    sealed = _draft_to_sealed(
        draft,
        schema_path=schema_path,
        schema_sha256=_sha256_bytes(schema_raw),
        draft_sha256=_sha256_bytes(draft_raw),
        sealed_at=timestamp.isoformat(timespec="seconds"),
    )
    validate_observation(sealed)
    observation_id = str(sealed["observation_id"])
    output_path = signals_dir / f"{observation_id}.sealed.json"
    manifest_path = actions_dir / f"{observation_id}.manifest.json"
    if output_path.exists() or manifest_path.exists():
        raise StockDiagnosticError("refusing to overwrite an existing sealed card or manifest")
    sealed_raw = _canonical_json_bytes(sealed)
    commit, dirty, git_diff_sha256 = git_worktree_state(workspace.resolve())
    if dirty is None:
        raise StockDiagnosticError("Git working-tree state is unavailable")
    if dirty and git_diff_sha256 is None:
        raise StockDiagnosticError("dirty worktree cannot be sealed without git_diff_sha256")
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "observation_id": observation_id,
        "producer": PRODUCER,
        "standard_cli_generated": True,
        "sealed_at": sealed["pipeline"]["sealed_at"],
        "status": sealed["status"],
        "repository_commit": commit,
        "working_tree_dirty_at_generation": dirty,
        "git_diff_sha256": git_diff_sha256,
        "inputs": [
            {"role": "draft", "path": draft_path.as_posix(), "sha256": _sha256_bytes(draft_raw)},
            {"role": "schema", "path": schema_path.as_posix(), "sha256": _sha256_bytes(schema_raw)},
        ],
        "outputs": [
            {"role": "sealed_observation", "path": output_path.as_posix(), "sha256": _sha256_bytes(sealed_raw)}
        ],
        "safety": SAFETY,
    }
    signals_dir.mkdir(parents=True, exist_ok=True)
    actions_dir.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(sealed_raw)
    manifest_path.write_bytes(_canonical_json_bytes(manifest))
    return output_path, manifest_path


def verify(
    observation_path: Path,
    manifest_path: Path,
    *,
    schema_path: Path = DEFAULT_SCHEMA,
) -> dict[str, Any]:
    observation, observation_raw = _read_json(observation_path, "sealed observation")
    manifest, _ = _read_json(manifest_path, "manifest")
    schema, schema_raw = _read_json(schema_path, "schema")
    validate_observation(observation)
    if schema.get("$id") != "https://quant.local/schemas/stock_diagnostic_observation.v1.json":
        raise StockDiagnosticError("unexpected stock diagnostic schema")
    _require_keys(
        manifest,
        {
            "manifest_version", "observation_id", "producer", "standard_cli_generated",
            "sealed_at", "status", "repository_commit", "working_tree_dirty_at_generation",
            "git_diff_sha256", "inputs", "outputs", "safety",
        },
        "manifest",
    )
    if manifest["manifest_version"] != MANIFEST_VERSION or manifest["producer"] != PRODUCER:
        raise StockDiagnosticError("manifest producer/version mismatch")
    if manifest["standard_cli_generated"] is not True:
        raise StockDiagnosticError("manifest is not standard-CLI generated")
    if manifest["observation_id"] != observation["observation_id"]:
        raise StockDiagnosticError("manifest observation_id mismatch")
    if manifest["sealed_at"] != observation["pipeline"]["sealed_at"]:
        raise StockDiagnosticError("manifest sealed_at mismatch")
    if manifest["status"] != "diagnostic_only_not_admitted":
        raise StockDiagnosticError("manifest status was elevated")
    _validate_safety(manifest["safety"])
    if observation["pipeline"]["schema_sha256"] != _sha256_bytes(schema_raw):
        raise StockDiagnosticError("schema hash mismatch")
    inputs = manifest["inputs"]
    outputs = manifest["outputs"]
    if not isinstance(inputs, list) or len(inputs) != 2 or not isinstance(outputs, list) or len(outputs) != 1:
        raise StockDiagnosticError("manifest input/output set mismatch")
    input_by_role = {item.get("role"): item for item in inputs if isinstance(item, dict)}
    output = outputs[0]
    if set(input_by_role) != {"draft", "schema"}:
        raise StockDiagnosticError("manifest roles mismatch")
    if input_by_role["schema"].get("sha256") != _sha256_bytes(schema_raw):
        raise StockDiagnosticError("manifest schema hash mismatch")
    if input_by_role["draft"].get("sha256") != observation["pipeline"]["draft_sha256"]:
        raise StockDiagnosticError("manifest draft hash mismatch")
    if output.get("role") != "sealed_observation" or output.get("sha256") != _sha256_bytes(observation_raw):
        raise StockDiagnosticError("sealed observation hash mismatch")
    if Path(str(output.get("path"))).name != observation_path.name:
        raise StockDiagnosticError("manifest output path mismatch")
    return {
        "verification_status": "verified_diagnostic_only",
        "observation_id": observation["observation_id"],
        "active_candidate_ids": observation["pre_entry_check"]["active_candidate_ids"],
        "entry_status": observation["evaluation"]["entry_status"],
        "paper_eligibility": False,
        "trade_eligibility": False,
        "live_execution_status": "live_not_supported",
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Seal or verify a research-only stock diagnostic card")
    subparsers = parser.add_subparsers(dest="command", required=True)
    seal_parser = subparsers.add_parser("seal")
    seal_parser.add_argument("--draft", type=Path, required=True)
    seal_parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    seal_parser.add_argument("--signals-dir", type=Path, default=Path("data/signals"))
    seal_parser.add_argument("--actions-dir", type=Path, default=Path("data/actions"))
    seal_parser.add_argument("--workspace", type=Path, default=Path("."))
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--observation", type=Path, required=True)
    verify_parser.add_argument("--manifest", type=Path, required=True)
    verify_parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "seal":
            observation_path, manifest_path = seal(
                args.draft,
                schema_path=args.schema,
                signals_dir=args.signals_dir,
                actions_dir=args.actions_dir,
                workspace=args.workspace,
            )
            result = {
                "status": "sealed_diagnostic_only",
                "observation": observation_path.as_posix(),
                "manifest": manifest_path.as_posix(),
            }
        else:
            result = verify(args.observation, args.manifest, schema_path=args.schema)
    except StockDiagnosticError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["StockDiagnosticError", "main", "seal", "validate_observation", "verify"]
