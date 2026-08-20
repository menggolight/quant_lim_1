#!/usr/bin/env python3
"""Seal a three-layer market observation and render controlled local dashboards."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from agent.market_observation_dashboard import (
    ObservationValidationError,
    parse_json_object,
    render_dashboard,
    validate_manifest,
    validate_observation,
    write_dashboard,
)
from research.market_data.storage import (
    MarketDataStorage,
    MarketDataStorageError,
    read_validated_batch,
)
from research.market_data.registry import (
    DEFAULT_STORAGE_ROOT as DEFAULT_MARKET_DATA_STORAGE_ROOT,
)
from research.reproducibility import git_worktree_state as _git_state


PIPELINE_VERSION = "market-observation-pipeline-v0.1"
MANIFEST_VERSION = "market-observation-manifest-v0.3"
LATEST_ALIAS_VERSION = "market-observation-latest-alias-v0.1"
DEFAULT_SCHEMA = Path("schemas") / "market_observation.v0.1.json"
DEFAULT_SIGNALS_DIR = Path("data") / "signals"
DEFAULT_MANIFEST_DIR = Path("data") / "actions"
DEFAULT_DASHBOARD_DIR = Path("data") / "reports" / "market_observation"

OVERALL_STATE_FIELDS = (
    "macro_environment",
    "market_state",
    "risk_budget_observation",
    "research_action",
)


@dataclass(frozen=True)
class PipelineOutputs:
    observation_path: Path
    manifest_path: Path
    snapshot_dashboard_path: Path
    latest_dashboard_path: Path
    latest_alias_path: Path


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    payload = parse_json_object(raw, f"{label}: {path}")
    return payload, raw


def _schema_error(path: str, message: str) -> ObservationValidationError:
    return ObservationValidationError(f"schema validation failed at {path}: {message}")


def _resolve_schema_ref(root_schema: dict[str, Any], reference: str) -> dict[str, Any]:
    if not reference.startswith("#/"):
        raise ObservationValidationError(f"unsupported external schema reference: {reference}")
    current: Any = root_schema
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or part not in current:
            raise ObservationValidationError(f"unresolvable schema reference: {reference}")
        current = current[part]
    if not isinstance(current, dict):
        raise ObservationValidationError(f"schema reference is not an object: {reference}")
    return current


def _matches_json_type(instance: Any, expected: str) -> bool:
    if expected == "null":
        return instance is None
    if expected == "boolean":
        return isinstance(instance, bool)
    if expected == "object":
        return isinstance(instance, dict)
    if expected == "array":
        return isinstance(instance, list)
    if expected == "string":
        return isinstance(instance, str)
    if expected == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if expected == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    raise ObservationValidationError(f"unsupported schema type: {expected}")


def _schema_matches(instance: Any, schema: dict[str, Any], root_schema: dict[str, Any]) -> bool:
    try:
        _validate_schema_node(instance, schema, root_schema, "$")
    except ObservationValidationError:
        return False
    return True


def _validate_schema_node(
    instance: Any,
    schema: dict[str, Any],
    root_schema: dict[str, Any],
    path: str,
) -> None:
    if "$ref" in schema:
        reference = schema["$ref"]
        if not isinstance(reference, str):
            raise _schema_error(path, "$ref must be a string")
        _validate_schema_node(instance, _resolve_schema_ref(root_schema, reference), root_schema, path)
        return

    if "const" in schema and instance != schema["const"]:
        raise _schema_error(path, f"must equal {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        raise _schema_error(path, "value is not in enum")

    expected_type = schema.get("type")
    if expected_type is not None:
        allowed_types = expected_type if isinstance(expected_type, list) else [expected_type]
        if not any(isinstance(item, str) and _matches_json_type(instance, item) for item in allowed_types):
            raise _schema_error(path, f"expected type {allowed_types}")

    for branch in schema.get("allOf", []):
        _validate_schema_node(instance, branch, root_schema, path)
    if "anyOf" in schema:
        if not any(_schema_matches(instance, branch, root_schema) for branch in schema["anyOf"]):
            raise _schema_error(path, "does not match anyOf")
    if "oneOf" in schema:
        matches = sum(_schema_matches(instance, branch, root_schema) for branch in schema["oneOf"])
        if matches != 1:
            raise _schema_error(path, f"must match exactly one oneOf branch, matched {matches}")
    if "if" in schema and _schema_matches(instance, schema["if"], root_schema):
        then_schema = schema.get("then")
        if isinstance(then_schema, dict):
            _validate_schema_node(instance, then_schema, root_schema, path)

    if isinstance(instance, dict):
        required = schema.get("required", [])
        for field in required:
            if field not in instance:
                raise _schema_error(path, f"missing required field {field}")
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for field, value in instance.items():
                if field in properties:
                    _validate_schema_node(value, properties[field], root_schema, f"{path}.{field}")
                elif schema.get("additionalProperties") is False:
                    raise _schema_error(path, f"unexpected field {field}")

    if isinstance(instance, list):
        minimum_items = schema.get("minItems")
        maximum_items = schema.get("maxItems")
        if isinstance(minimum_items, int) and len(instance) < minimum_items:
            raise _schema_error(path, f"requires at least {minimum_items} items")
        if isinstance(maximum_items, int) and len(instance) > maximum_items:
            raise _schema_error(path, f"allows at most {maximum_items} items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, value in enumerate(instance):
                _validate_schema_node(value, item_schema, root_schema, f"{path}[{index}]")

    if isinstance(instance, str):
        minimum_length = schema.get("minLength")
        maximum_length = schema.get("maxLength")
        pattern = schema.get("pattern")
        if isinstance(minimum_length, int) and len(instance) < minimum_length:
            raise _schema_error(path, f"minimum length is {minimum_length}")
        if isinstance(maximum_length, int) and len(instance) > maximum_length:
            raise _schema_error(path, f"maximum length is {maximum_length}")
        if isinstance(pattern, str) and re.search(pattern, instance) is None:
            raise _schema_error(path, f"does not match pattern {pattern}")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        exclusive_minimum = schema.get("exclusiveMinimum")
        exclusive_maximum = schema.get("exclusiveMaximum")
        if isinstance(minimum, (int, float)) and instance < minimum:
            raise _schema_error(path, f"must be >= {minimum}")
        if isinstance(maximum, (int, float)) and instance > maximum:
            raise _schema_error(path, f"must be <= {maximum}")
        if isinstance(exclusive_minimum, (int, float)) and instance <= exclusive_minimum:
            raise _schema_error(path, f"must be > {exclusive_minimum}")
        if isinstance(exclusive_maximum, (int, float)) and instance >= exclusive_maximum:
            raise _schema_error(path, f"must be < {exclusive_maximum}")


def validate_against_schema(instance: dict[str, Any], schema: dict[str, Any]) -> None:
    """Validate the JSON Schema subset used by market_observation.v0.1."""

    _validate_schema_node(instance, schema, schema, "$")


def _atomic_write(path: Path, raw: bytes, *, allow_replace: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = path.read_bytes()
        if existing == raw:
            return
        if not allow_replace:
            raise ObservationValidationError(f"refusing to overwrite non-identical controlled output: {path}")
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_bytes(raw)
    temporary.replace(path)


def _assert_no_conflict(path: Path, raw: bytes) -> None:
    if path.exists() and path.read_bytes() != raw:
        raise ObservationValidationError(f"controlled output collision: {path}")


def _parse_time(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ObservationValidationError(f"{field_name} must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise ObservationValidationError(f"{field_name} must include a timezone offset")
    return parsed


def _now() -> datetime:
    """Return the actual sealing clock; tests patch this boundary explicitly."""

    return datetime.now().astimezone()


def _state_map(observation: dict[str, Any]) -> dict[str, dict[str, str]]:
    industry = observation.get("industry") if isinstance(observation.get("industry"), dict) else {}
    stock = observation.get("stock") if isinstance(observation.get("stock"), dict) else {}
    sectors: dict[str, str] = {}
    for item in industry.get("sectors", []):
        if not isinstance(item, dict):
            continue
        code = item.get("code")
        state = item.get("state")
        if code and state:
            sectors[str(code)] = str(state)

    stocks: dict[str, str] = {}
    focus = stock.get("focus")
    if isinstance(focus, dict) and focus.get("stock_id") and focus.get("state"):
        stocks[str(focus["stock_id"])] = str(focus["state"])
    for item in stock.get("cross_industry_observation_samples", []):
        if not isinstance(item, dict):
            continue
        stock_id = item.get("stock_id")
        state = item.get("state")
        if stock_id and state:
            stocks[str(stock_id)] = str(state)
    return {"industry": sectors, "stock": stocks}


def _name_map(observation: dict[str, Any]) -> dict[str, str]:
    names: dict[str, str] = {}
    industry = observation.get("industry") if isinstance(observation.get("industry"), dict) else {}
    stock = observation.get("stock") if isinstance(observation.get("stock"), dict) else {}
    for item in industry.get("sectors", []):
        if isinstance(item, dict) and item.get("code"):
            names[str(item["code"])] = str(item.get("name") or item["code"])
    focus = stock.get("focus")
    if isinstance(focus, dict) and focus.get("stock_id"):
        names[str(focus["stock_id"])] = str(focus.get("name") or focus["stock_id"])
    for item in stock.get("cross_industry_observation_samples", []):
        if isinstance(item, dict) and item.get("stock_id"):
            names[str(item["stock_id"])] = str(item.get("name") or item["stock_id"])
    return names


def _map_changes(
    current: dict[str, str],
    previous: dict[str, str],
    names: dict[str, str],
) -> list[dict[str, str | None]]:
    changes: list[dict[str, str | None]] = []
    for subject_id in sorted(set(current) | set(previous)):
        before = previous.get(subject_id)
        after = current.get(subject_id)
        if before == after:
            continue
        change_type = "changed"
        if before is None:
            change_type = "added"
        elif after is None:
            change_type = "removed"
        changes.append(
            {
                "subject_id": subject_id,
                "subject_name": names.get(subject_id, subject_id),
                "from": before,
                "to": after,
                "change_type": change_type,
            }
        )
    return changes


def build_comparison(
    current: dict[str, Any],
    previous: dict[str, Any] | None,
    previous_hash: str | None,
    previous_sealed_at: str | None = None,
) -> dict[str, Any]:
    if previous is None:
        return {
            "status": "first_baseline",
            "previous_observation_id": None,
            "previous_as_of": None,
            "previous_sha256": None,
            "overall_state_changes": [],
            "industry_state_changes": [],
            "stock_state_changes": [],
            "new_conflicts": [],
            "resolved_conflicts": [],
            "has_material_change": False,
        }

    validate_observation(previous)
    current_decision = _parse_time(str(current["decision_time"]), "decision_time")
    previous_decision = _parse_time(str(previous["decision_time"]), "previous.decision_time")
    previous_generated = _parse_time(str(previous["generated_at"]), "previous.generated_at")
    if previous_sealed_at is None:
        raise ObservationValidationError("previous standard CLI sealed_at is required")
    previous_sealed = _parse_time(previous_sealed_at, "previous manifest sealed_at")
    if previous_decision >= current_decision:
        raise ObservationValidationError("previous observation must precede the current decision_time")
    if previous_generated > current_decision:
        raise ObservationValidationError("previous observation was not available at the current decision_time")
    if previous_sealed > current_decision:
        raise ObservationValidationError("previous sealed_at is after the current decision_time")
    if previous.get("observation_id") == current.get("observation_id"):
        raise ObservationValidationError("previous observation_id must differ from the current observation_id")
    if previous.get("market") != current.get("market"):
        raise ObservationValidationError("previous and current market must match")
    previous_industry = previous.get("industry") if isinstance(previous.get("industry"), dict) else {}
    current_industry = current.get("industry") if isinstance(current.get("industry"), dict) else {}
    for field in ("classification", "benchmark_id"):
        if previous_industry.get(field) != current_industry.get(field):
            raise ObservationValidationError(f"previous and current industry {field} must match")

    current_overall = current.get("overall") if isinstance(current.get("overall"), dict) else {}
    previous_overall = previous.get("overall") if isinstance(previous.get("overall"), dict) else {}
    overall_changes: list[dict[str, str | None]] = []
    for field in OVERALL_STATE_FIELDS:
        before = previous_overall.get(field)
        after = current_overall.get(field)
        if before != after:
            overall_changes.append(
                {
                    "field": field,
                    "from": str(before) if before is not None else None,
                    "to": str(after) if after is not None else None,
                }
            )

    current_states = _state_map(current)
    previous_states = _state_map(previous)
    names = {**_name_map(previous), **_name_map(current)}
    industry_changes = _map_changes(current_states["industry"], previous_states["industry"], names)
    stock_changes = _map_changes(current_states["stock"], previous_states["stock"], names)

    current_conflicts = [str(item).strip() for item in current.get("three_layer_conflicts", [])]
    previous_conflicts = [str(item).strip() for item in previous.get("three_layer_conflicts", [])]
    new_conflicts = [item for item in current_conflicts if item not in previous_conflicts]
    resolved_conflicts = [item for item in previous_conflicts if item not in current_conflicts]
    material = bool(overall_changes or industry_changes or stock_changes or new_conflicts or resolved_conflicts)
    return {
        "status": "compared",
        "previous_observation_id": previous.get("observation_id"),
        "previous_as_of": previous.get("as_of"),
        "previous_sha256": previous_hash,
        "overall_state_changes": overall_changes,
        "industry_state_changes": industry_changes,
        "stock_state_changes": stock_changes,
        "new_conflicts": new_conflicts,
        "resolved_conflicts": resolved_conflicts,
        "has_material_change": material,
    }


def _load_schema_contract(schema_path: Path, schema_version: str) -> tuple[dict[str, Any], bytes, str]:
    schema, raw = _read_json(schema_path, "market observation schema")
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    schema_version_rule = properties.get("schema_version") if isinstance(properties.get("schema_version"), dict) else {}
    if schema_version_rule.get("const") != schema_version:
        raise ObservationValidationError("schema contract does not match observation schema_version")
    return schema, raw, _sha256_bytes(raw)


def _market_data_batch_evidence(
    paths: Sequence[Path],
    *,
    decision_time: datetime,
    storage_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Verify storage-managed batches and return deterministic manifest evidence."""

    evidence: list[dict[str, Any]] = []
    inputs: list[dict[str, Any]] = []
    seen_paths: set[Path] = set()
    for path in sorted((Path(item).resolve() for item in paths), key=lambda item: item.as_posix()):
        if path in seen_paths:
            raise ObservationValidationError(f"duplicate market-data batch path: {path}")
        seen_paths.add(path)
        try:
            batch = read_validated_batch(path, storage_root=storage_root)
        except (MarketDataStorageError, OSError, ValueError) as exc:
            raise ObservationValidationError(
                f"market-data batch is not verified validated evidence: {path}: {exc}"
            ) from exc
        if batch.completeness_status != "complete" or batch.record_count < 1:
            raise ObservationValidationError("market-data batch must be complete and non-empty")
        if batch.synthetic:
            raise ObservationValidationError("synthetic market-data batch is forbidden")
        if batch.requested_at > decision_time or batch.fetched_at > decision_time:
            raise ObservationValidationError(
                "market-data batch was requested or fetched after decision_time"
            )
        if batch.available_at_max is None or batch.available_at_max > decision_time:
            raise ObservationValidationError(
                "market-data batch has evidence unavailable at decision_time"
            )
        file_hash = _sha256_bytes(path.read_bytes())
        receipt_path = MarketDataStorage.validated_receipt_path(path)
        receipt_hash = _sha256_bytes(receipt_path.read_bytes())
        metadata = batch.to_dict(include_records=False)
        metadata["batch_file_path"] = path.as_posix()
        metadata["batch_file_sha256"] = file_hash
        metadata["registry_receipt_path"] = receipt_path.as_posix()
        metadata["registry_receipt_sha256"] = receipt_hash
        evidence.append(metadata)
        inputs.append(
            {
                "role": "market_data_batch",
                "path": path.as_posix(),
                "sha256": file_hash,
                "batch_id": batch.batch_id,
            }
        )
        inputs.append(
            {
                "role": "market_data_registry_receipt",
                "path": receipt_path.as_posix(),
                "sha256": receipt_hash,
                "batch_id": batch.batch_id,
            }
        )
    evidence.sort(key=lambda item: (item["provider_id"], item["dataset_type"], item["batch_id"]))
    inputs.sort(key=lambda item: (item["batch_id"], item["role"], item["path"]))
    return evidence, inputs


def _reuse_existing_sealed_at(
    observation_path: Path,
    manifest_path: Path,
    *,
    draft_hash: str,
    schema: dict[str, Any],
    schema_path: Path,
    schema_hash: str,
    comparison: dict[str, Any],
    market_data_storage_root: Path,
) -> str | None:
    observation_exists = observation_path.exists()
    manifest_exists = manifest_path.exists()
    if observation_exists != manifest_exists:
        raise ObservationValidationError("controlled observation and manifest must either both exist or both be absent")
    if not observation_exists:
        return None

    existing, existing_raw = _read_json(observation_path, "existing sealed observation")
    validate_observation(existing)
    validate_against_schema(existing, schema)
    validate_manifest(
        manifest_path,
        existing,
        _sha256_bytes(existing_raw),
        observation_path,
        market_data_storage_root=market_data_storage_root,
    )
    pipeline = existing.get("pipeline")
    if not isinstance(pipeline, dict):
        raise ObservationValidationError("existing controlled observation is missing pipeline metadata")
    if pipeline.get("draft_sha256") != draft_hash:
        raise ObservationValidationError("existing controlled observation was sealed from a different draft")
    if pipeline.get("schema_path") != schema_path.as_posix() or pipeline.get("schema_sha256") != schema_hash:
        raise ObservationValidationError("existing controlled observation used a different Schema contract")
    if existing.get("comparison") != comparison:
        raise ObservationValidationError("existing controlled observation used a different comparison lineage")
    sealed_at = pipeline.get("sealed_at")
    if not isinstance(sealed_at, str):
        raise ObservationValidationError("existing controlled observation is missing sealed_at")
    _parse_time(sealed_at, "existing pipeline.sealed_at")
    return sealed_at


def _recorded_path(entry: dict[str, Any], label: str) -> Path:
    path_value = entry.get("path")
    if not isinstance(path_value, str) or not path_value.strip():
        raise ObservationValidationError(f"latest alias {label} path is missing")
    path = Path(path_value)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _verify_recorded_file(entry: dict[str, Any], label: str) -> tuple[Path, str]:
    path = _recorded_path(entry, label)
    expected_hash = entry.get("sha256")
    if not isinstance(expected_hash, str) or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None:
        raise ObservationValidationError(f"latest alias {label} SHA-256 is invalid")
    if not path.is_file() or _sha256_bytes(path.read_bytes()) != expected_hash:
        raise ObservationValidationError(f"latest alias {label} file is missing or has changed")
    return path, expected_hash


def _validate_latest_update(
    *,
    latest_dashboard_path: Path,
    latest_alias_path: Path,
    current: dict[str, Any],
    observation_path: Path,
    observation_hash: str,
    manifest_path: Path,
    manifest_hash: str,
    snapshot_dashboard_path: Path,
    snapshot_hash: str,
    snapshot_raw: bytes,
    previous_path: Path | None,
    previous_manifest_path: Path | None,
    market_data_storage_root: Path,
) -> None:
    if not latest_alias_path.exists():
        if latest_dashboard_path.exists():
            raise ObservationValidationError("latest dashboard exists without controlled alias metadata")
        return
    if not latest_dashboard_path.exists():
        raise ObservationValidationError("latest alias exists but latest dashboard is missing")

    alias, _alias_raw = _read_json(latest_alias_path, "latest dashboard alias")
    if alias.get("alias_version") != LATEST_ALIAS_VERSION or alias.get("mutable") is not True:
        raise ObservationValidationError("latest alias version or mutability marker is invalid")
    records: dict[str, tuple[Path, str]] = {}
    for label in ("observation", "manifest", "snapshot_dashboard", "latest_dashboard"):
        entry = alias.get(label)
        if not isinstance(entry, dict):
            raise ObservationValidationError(f"latest alias {label} entry is missing")
        records[label] = _verify_recorded_file(entry, label)
    if records["latest_dashboard"][0] != latest_dashboard_path.resolve():
        raise ObservationValidationError("latest alias points to a different latest dashboard path")
    if records["latest_dashboard"][1] != records["snapshot_dashboard"][1]:
        raise ObservationValidationError("latest dashboard and recorded snapshot hashes must match")

    previous_observation_path, previous_observation_hash = records["observation"]
    previous_manifest_recorded_path, previous_manifest_hash = records["manifest"]
    previous_observation, previous_observation_raw = _read_json(
        previous_observation_path,
        "latest alias observation",
    )
    if _sha256_bytes(previous_observation_raw) != previous_observation_hash:
        raise ObservationValidationError("latest alias observation hash changed during validation")
    validate_observation(previous_observation)
    validate_manifest(
        previous_manifest_recorded_path,
        previous_observation,
        previous_observation_hash,
        previous_observation_path,
        market_data_storage_root=market_data_storage_root,
    )
    previous_pipeline = previous_observation.get("pipeline")
    if not isinstance(previous_pipeline, dict):
        raise ObservationValidationError("latest alias observation lacks pipeline metadata")
    for field, expected in (
        ("observation_id", previous_observation.get("observation_id")),
        ("as_of", previous_observation.get("as_of")),
        ("decision_time", previous_observation.get("decision_time")),
        ("sealed_at", previous_pipeline.get("sealed_at")),
    ):
        if alias.get(field) != expected:
            raise ObservationValidationError(f"latest alias {field} does not match its observation")

    previous_decision = _parse_time(str(previous_observation["decision_time"]), "latest alias decision_time")
    current_decision = _parse_time(str(current["decision_time"]), "current decision_time")
    if current_decision < previous_decision:
        raise ObservationValidationError("refusing to move latest dashboard backward in decision_time")

    if current_decision == previous_decision:
        expected_records = {
            "observation": (observation_path.resolve(), observation_hash),
            "manifest": (manifest_path.resolve(), manifest_hash),
            "snapshot_dashboard": (snapshot_dashboard_path.resolve(), snapshot_hash),
            "latest_dashboard": (latest_dashboard_path.resolve(), snapshot_hash),
        }
        if alias.get("observation_id") != current.get("observation_id") or any(
            records[label] != expected for label, expected in expected_records.items()
        ):
            raise ObservationValidationError("same decision_time can only be an identical idempotent latest update")
        if latest_dashboard_path.read_bytes() != snapshot_raw:
            raise ObservationValidationError("idempotent latest dashboard content does not match the snapshot")
        return

    comparison = current.get("comparison")
    if not isinstance(comparison, dict) or comparison.get("status") != "compared":
        raise ObservationValidationError("a newer latest dashboard must compare against the current alias")
    if (
        comparison.get("previous_observation_id") != alias.get("observation_id")
        or comparison.get("previous_sha256") != previous_observation_hash
        or previous_path is None
        or previous_manifest_path is None
        or previous_path.resolve() != previous_observation_path
        or previous_manifest_path.resolve() != previous_manifest_recorded_path
        or _sha256_bytes(previous_manifest_path.read_bytes()) != previous_manifest_hash
    ):
        raise ObservationValidationError("new latest observation is not chained to the current controlled alias")


def _latest_alias_bytes(
    *,
    current: dict[str, Any],
    observation_path: Path,
    observation_hash: str,
    manifest_path: Path,
    manifest_hash: str,
    snapshot_dashboard_path: Path,
    snapshot_hash: str,
    latest_dashboard_path: Path,
) -> bytes:
    pipeline = current.get("pipeline")
    if not isinstance(pipeline, dict):
        raise ObservationValidationError("sealed observation pipeline metadata is missing")
    return _canonical_json_bytes(
        {
            "alias_version": LATEST_ALIAS_VERSION,
            "mutable": True,
            "observation_id": current["observation_id"],
            "as_of": current["as_of"],
            "decision_time": current["decision_time"],
            "sealed_at": pipeline["sealed_at"],
            "observation": {"path": observation_path.as_posix(), "sha256": observation_hash},
            "manifest": {"path": manifest_path.as_posix(), "sha256": manifest_hash},
            "snapshot_dashboard": {
                "path": snapshot_dashboard_path.as_posix(),
                "sha256": snapshot_hash,
            },
            "latest_dashboard": {"path": latest_dashboard_path.as_posix(), "sha256": snapshot_hash},
        }
    )


def run_pipeline(
    *,
    input_path: Path,
    previous_path: Path | None,
    previous_manifest_path: Path | None,
    first_baseline: bool,
    schema_path: Path,
    signals_dir: Path,
    manifest_dir: Path,
    dashboard_dir: Path,
    market_data_batch_paths: Sequence[Path] = (),
    market_data_storage_root: Path = DEFAULT_MARKET_DATA_STORAGE_ROOT,
    workspace: Path = Path("."),
) -> PipelineOutputs:
    has_previous = previous_path is not None or previous_manifest_path is not None
    if (previous_path is None) != (previous_manifest_path is None):
        raise ObservationValidationError("previous_path and previous_manifest_path must be provided together")
    if first_baseline == has_previous:
        raise ObservationValidationError("choose exactly one of first_baseline or the previous observation pair")

    draft, draft_raw = _read_json(input_path, "market observation draft")
    if "comparison" in draft or "pipeline" in draft:
        raise ObservationValidationError("draft must not supply computed comparison or pipeline fields")
    validate_observation(draft)
    schema, _schema_raw, schema_hash = _load_schema_contract(schema_path, str(draft["schema_version"]))
    validate_against_schema(draft, schema)
    draft_hash = _sha256_bytes(draft_raw)
    observation_id = str(draft["observation_id"])
    observation_path = signals_dir / f"{observation_id}.sealed.json"
    manifest_path = manifest_dir / f"{observation_id}.manifest.json"
    snapshot_dashboard_path = dashboard_dir / f"{observation_id}.html"
    latest_dashboard_path = dashboard_dir / "latest.html"
    latest_alias_path = dashboard_dir / "latest.alias.json"

    previous = None
    previous_hash = None
    previous_sealed_at = None
    previous_raw = None
    previous_manifest_raw = None
    if previous_path is not None:
        previous, previous_raw = _read_json(previous_path, "previous market observation")
        validate_observation(previous)
        validate_against_schema(previous, schema)
        previous_hash = _sha256_bytes(previous_raw)
        assert previous_manifest_path is not None
        validate_manifest(
            previous_manifest_path,
            previous,
            previous_hash,
            previous_path,
            market_data_storage_root=market_data_storage_root,
        )
        previous_manifest, previous_manifest_raw = _read_json(previous_manifest_path, "previous observation manifest")
        previous_sealed_at_value = previous_manifest.get("sealed_at")
        if not isinstance(previous_sealed_at_value, str):
            raise ObservationValidationError("previous manifest is missing standard CLI sealed_at")
        previous_sealed_at = previous_sealed_at_value

    sealed = dict(draft)
    sealed["comparison"] = build_comparison(sealed, previous, previous_hash, previous_sealed_at)
    sealed_at = _reuse_existing_sealed_at(
        observation_path,
        manifest_path,
        draft_hash=draft_hash,
        schema=schema,
        schema_path=schema_path,
        schema_hash=schema_hash,
        comparison=sealed["comparison"],
        market_data_storage_root=market_data_storage_root,
    )
    if sealed_at is None:
        sealing_time = _now()
        if sealing_time.tzinfo is None:
            raise ObservationValidationError("standard CLI sealing clock must include a timezone offset")
        sealed_at = sealing_time.isoformat()
    sealed_time = _parse_time(sealed_at, "pipeline.sealed_at")
    decision_time = _parse_time(str(sealed["decision_time"]), "decision_time")
    if sealed_time < decision_time:
        raise ObservationValidationError("standard CLI cannot seal an observation before decision_time")
    if sealed_time < _parse_time(str(sealed["generated_at"]), "generated_at"):
        raise ObservationValidationError("standard CLI cannot seal an observation before generated_at")
    sealed["pipeline"] = {
        "producer": PIPELINE_VERSION,
        "standard_cli_generated": True,
        "sealed_at": sealed_at,
        "schema_path": schema_path.as_posix(),
        "schema_sha256": schema_hash,
        "draft_sha256": draft_hash,
    }
    validate_observation(sealed)
    validate_against_schema(sealed, schema)
    sealed_raw = _canonical_json_bytes(sealed)
    # Render from the canonical serialization order. The source-link section
    # preserves first-seen order, so rendering the pre-serialization dict here
    # would differ from a later render of the sealed file.
    sealed_for_render = parse_json_object(sealed_raw, "canonical sealed observation")
    market_data_batches, market_data_inputs = _market_data_batch_evidence(
        market_data_batch_paths,
        decision_time=decision_time,
        storage_root=market_data_storage_root,
    )
    commit, dirty, git_diff_hash = _git_state(workspace)
    if dirty is None:
        raise ObservationValidationError(
            "Git working-tree state is unavailable; standard CLI sealing is refused"
        )
    if dirty is True and git_diff_hash is None:
        raise ObservationValidationError(
            "dirty working tree cannot be sealed without git_diff_sha256"
        )
    inputs: list[dict[str, Any]] = [
        {
            "role": "draft_observation",
            "path": input_path.as_posix(),
            "sha256": draft_hash,
        },
        {
            "role": "schema",
            "path": schema_path.as_posix(),
            "sha256": schema_hash,
        },
    ]
    inputs.extend(market_data_inputs)
    if previous_path is not None and previous_raw is not None:
        inputs.append(
            {
                "role": "previous_observation",
                "path": previous_path.as_posix(),
                "sha256": _sha256_bytes(previous_raw),
            }
        )
        assert previous_manifest_path is not None and previous_manifest_raw is not None
        inputs.append(
            {
                "role": "previous_manifest",
                "path": previous_manifest_path.as_posix(),
                "sha256": _sha256_bytes(previous_manifest_raw),
            }
        )
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "observation_id": observation_id,
        "status": sealed["status"],
        "as_of": sealed["as_of"],
        "generated_at": sealed["generated_at"],
        "sealed_at": sealed_at,
        "producer": PIPELINE_VERSION,
        "standard_cli_generated": True,
        "repository_commit": commit,
        "working_tree_dirty_at_generation": dirty,
        "git_diff_sha256": git_diff_hash,
        "market_data_storage_root": market_data_storage_root.resolve().as_posix(),
        "schema": {
            "path": schema_path.as_posix(),
            "sha256": schema_hash,
            "schema_version": sealed["schema_version"],
        },
        "inputs": inputs,
        "outputs": [
            {
                "role": "sealed_observation",
                "path": observation_path.as_posix(),
                "sha256": _sha256_bytes(sealed_raw),
            }
        ],
        "aliases": [
            {
                "role": "latest_dashboard",
                "path": latest_dashboard_path.as_posix(),
                "metadata_path": latest_alias_path.as_posix(),
                "mutable": True,
            }
        ],
        "source_status": sealed.get("data_quality", {}),
        "market_data_batches": market_data_batches,
        "comparison": {
            "status": sealed["comparison"]["status"],
            "previous_observation_id": sealed["comparison"]["previous_observation_id"],
            "previous_sha256": sealed["comparison"]["previous_sha256"],
        },
        "admission": {
            "source_data_admitted": False,
            "objective_factor_admitted": False,
            "research_report_factor_admitted": False,
            "paper_strategy_admitted": False,
            "live_trading_allowed": False,
        },
    }
    manifest_raw = _canonical_json_bytes(manifest)
    manifest_hash = _sha256_bytes(manifest_raw)
    snapshot_raw = render_dashboard(
        sealed_for_render,
        _sha256_bytes(sealed_raw),
        manifest_verified=True,
        manifest_hash=manifest_hash,
        market_data_batches=market_data_batches,
        manifest_version=MANIFEST_VERSION,
    ).encode("utf-8")
    observation_hash = _sha256_bytes(sealed_raw)
    snapshot_hash = _sha256_bytes(snapshot_raw)
    latest_alias_raw = _latest_alias_bytes(
        current=sealed_for_render,
        observation_path=observation_path,
        observation_hash=observation_hash,
        manifest_path=manifest_path,
        manifest_hash=manifest_hash,
        snapshot_dashboard_path=snapshot_dashboard_path,
        snapshot_hash=snapshot_hash,
        latest_dashboard_path=latest_dashboard_path,
    )
    _validate_latest_update(
        latest_dashboard_path=latest_dashboard_path,
        latest_alias_path=latest_alias_path,
        current=sealed_for_render,
        observation_path=observation_path,
        observation_hash=observation_hash,
        manifest_path=manifest_path,
        manifest_hash=manifest_hash,
        snapshot_dashboard_path=snapshot_dashboard_path,
        snapshot_hash=snapshot_hash,
        snapshot_raw=snapshot_raw,
        previous_path=previous_path,
        previous_manifest_path=previous_manifest_path,
        market_data_storage_root=market_data_storage_root,
    )
    _assert_no_conflict(observation_path, sealed_raw)
    _assert_no_conflict(manifest_path, manifest_raw)
    _assert_no_conflict(snapshot_dashboard_path, snapshot_raw)
    _atomic_write(observation_path, sealed_raw, allow_replace=False)
    _atomic_write(manifest_path, manifest_raw, allow_replace=False)
    write_dashboard(
        observation_path,
        snapshot_dashboard_path,
        manifest_path,
        allow_replace=False,
        market_data_storage_root=market_data_storage_root,
    )
    _atomic_write(latest_dashboard_path, snapshot_raw, allow_replace=True)
    _atomic_write(latest_alias_path, latest_alias_raw, allow_replace=True)
    return PipelineOutputs(
        observation_path=observation_path,
        manifest_path=manifest_path,
        snapshot_dashboard_path=snapshot_dashboard_path,
        latest_dashboard_path=latest_dashboard_path,
        latest_alias_path=latest_alias_path,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Seal a market observation and render dated/latest local dashboards.")
    parser.add_argument("--input", type=Path, required=True, help="Explicit draft observation JSON.")
    baseline = parser.add_mutually_exclusive_group(required=True)
    baseline.add_argument("--previous", type=Path, help="Previous sealed observation JSON.")
    baseline.add_argument("--first-baseline", action="store_true", help="Declare this as the first comparable observation.")
    parser.add_argument("--previous-manifest", type=Path, help="Manifest paired with --previous.")
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA, help="Versioned observation JSON Schema.")
    parser.add_argument("--signals-dir", type=Path, default=DEFAULT_SIGNALS_DIR)
    parser.add_argument("--manifest-dir", type=Path, default=DEFAULT_MANIFEST_DIR)
    parser.add_argument("--dashboard-dir", type=Path, default=DEFAULT_DASHBOARD_DIR)
    parser.add_argument(
        "--market-data-batch",
        action="append",
        type=Path,
        default=[],
        help="Validated market-data batch JSON; repeat for multiple batches.",
    )
    parser.add_argument(
        "--market-data-storage-root",
        type=Path,
        default=DEFAULT_MARKET_DATA_STORAGE_ROOT,
        help="Controlled raw/quarantine/validated market-data root.",
    )
    parser.add_argument("--workspace", type=Path, default=Path("."))
    args = parser.parse_args()

    outputs = run_pipeline(
        input_path=args.input,
        previous_path=args.previous,
        previous_manifest_path=args.previous_manifest,
        first_baseline=args.first_baseline,
        schema_path=args.schema,
        signals_dir=args.signals_dir,
        manifest_dir=args.manifest_dir,
        dashboard_dir=args.dashboard_dir,
        market_data_batch_paths=args.market_data_batch,
        market_data_storage_root=args.market_data_storage_root,
        workspace=args.workspace,
    )
    print(f"Observation: {outputs.observation_path}")
    print(f"Manifest: {outputs.manifest_path}")
    print(f"Dashboard: {outputs.latest_dashboard_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
