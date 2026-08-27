"""Run one immutable, stateful Technical Shadow day with real BaoStock data.

This entry point publishes a manual D+1 plan only.  It cannot submit orders and
does not alter Paper, trade, real-money, or LIVE admission.
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import time as time_module
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from operations.run_technical_shadow_mvp import (
    ALPHA_LOOKBACK_SESSIONS,
    CHINA_TZ,
    STRATEGY_ID,
    BaoStockTechnicalShadowSource,
    CapturedData,
    TechnicalShadowRunError,
    _canonical_bytes,
    _cash_reason_codes,
    _decimal,
    _digest,
    _execute_targets,
    _execution_cost,
    _ledger_transaction_accounting,
    _load_config,
    _money,
    _money_text,
    _plan_targets,
    validate_source_provenance,
)
from research.market_data.providers.baostock import BaoStockProvider, to_baostock_code
from research.strategy_workspace.technical_alpha_shadow_v1 import (
    rank_technical_alpha_shadow,
)
from research.strategy_workspace.technical_exposure_shadow_v1 import (
    compute_technical_shadow_exposure,
)


DEFAULT_CONFIG = Path("configs/a_share_technical_shadow_mvp.v1.json")
DEFAULT_SEED = Path("configs/technical_shadow_daily_seed.v1.json")
DEFAULT_STATE_ROOT = Path("data/portfolio/technical-shadow-daily")
DEFAULT_REPORT_ROOT = Path("data/tmp/technical-shadow-daily")
LEGACY_INITIAL_STATE_DATE = date(2026, 8, 25)
SHADOW_ACCOUNT_ID = "technical-shadow-account-v1"
MODE = "stateful_daily"
DECISION_CUTOFF = time(15, 30)
EXECUTION_OPEN = time(9, 30)
DEFAULT_READY_WAIT_MINUTES = 120
DEFAULT_POLL_INTERVAL_SECONDS = 300
DEFAULT_MAX_POLLS = 25
DAILY_SAFETY = {
    "strategy_signal": False,
    "alpha_evidence": False,
    "trade_recommendation": False,
    "paper_eligibility": False,
    "trade_eligibility": False,
    "real_money_list_allowed": False,
    "automatic_order_submission": False,
    "live_supported": False,
}
CANCELLATION_CONDITIONS = (
    "provider_or_data_receipt_validation_failed",
    "previous_state_or_hash_chain_mismatch",
    "execution_date_or_trade_calendar_changed",
    "execution_open_missing_or_instrument_not_tradable",
    "st_stock_buy_forbidden",
    "buy_price_above_maximum_buy_price",
    "cash_or_whole_lot_unavailable",
    "t_plus_one_sell_quantity_unavailable",
    "automatic_order_submission_forbidden",
)


class TechnicalShadowDailyError(TechnicalShadowRunError):
    """Fail-closed daily state/immutability error."""


@dataclass(frozen=True)
class NextSessionEvidence:
    execution_date: date
    receipt: Mapping[str, Any]
    execution_window_status: str = "OPEN"


@dataclass(frozen=True)
class ReadinessResult:
    status: str
    state_date: date
    latest_completed_trading_date: date | None
    latest_benchmark_date: date | None
    strategy_date: date | None
    execution_date: date | None
    checked_at: datetime
    deadline_at: datetime | None
    reason_codes: tuple[str, ...]
    receipt: Mapping[str, Any]


def _file_sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TechnicalShadowDailyError(f"json_object_required:{path}")
    return value


def _strict_row_map(
    rows: Sequence[Mapping[str, Any]], *, instrument_id: str
) -> dict[date, Mapping[str, Any]]:
    mapped: dict[date, Mapping[str, Any]] = {}
    for row in rows:
        day = date.fromisoformat(str(row["trading_date"]))
        if day in mapped:
            raise TechnicalShadowDailyError(
                f"duplicate_trading_date:{instrument_id}:{day.isoformat()}"
            )
        mapped[day] = row
    return mapped


def _validate_row_cutoff(row: Mapping[str, Any], decision_date: date) -> None:
    trading_date = date.fromisoformat(str(row["trading_date"]))
    if trading_date > decision_date:
        raise TechnicalShadowDailyError("future_trading_date_rejected")
    available_at = datetime.fromisoformat(str(row["available_at"]))
    if available_at.tzinfo is None:
        raise TechnicalShadowDailyError("available_at_timezone_required")
    cutoff = datetime.combine(decision_date, DECISION_CUTOFF, CHINA_TZ)
    if available_at.astimezone(CHINA_TZ) > cutoff:
        raise TechnicalShadowDailyError("future_available_at_rejected")


def _load_seed(path: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    seed = _json(path)
    if (
        seed.get("schema_version") != "technical-shadow-daily-state-seed.v1"
        or seed.get("strategy_id") != STRATEGY_ID
        or seed.get("mode") != MODE
        or seed.get("shadow_account_id") != SHADOW_ACCOUNT_ID
    ):
        raise TechnicalShadowDailyError("daily_seed_identity_mismatch")
    if seed.get("config_sha256") != _digest(config):
        raise TechnicalShadowDailyError("daily_seed_config_sha256_mismatch")
    if seed.get("safety") != DAILY_SAFETY:
        raise TechnicalShadowDailyError("daily_seed_safety_mismatch")
    state = seed.get("state")
    if not isinstance(state, dict):
        raise TechnicalShadowDailyError("daily_seed_state_missing")
    required = {
        "state_date", "previous_trading_date", "previous_record_sha256",
        "cash", "positions", "sellable_quantities", "peak_nav", "drawdown",
        "exposure_state", "pending_state", "hysteresis_count",
    }
    if not required <= set(state):
        raise TechnicalShadowDailyError("daily_seed_state_incomplete")
    if _decimal(state["cash"]) < 0 or _decimal(state["peak_nav"]) <= 0:
        raise TechnicalShadowDailyError("daily_seed_account_invalid")
    return seed


def _verified_legacy_slot(path: Path) -> tuple[dict[str, Any], str]:
    manifest_path = path / "manifest.json"
    if not manifest_path.is_file():
        raise TechnicalShadowDailyError(f"partial_daily_slot_requires_manual_recovery:{path}")
    manifest = _json(manifest_path)
    if manifest.get("schema_version") != "technical-shadow-daily-manifest.v1":
        raise TechnicalShadowDailyError("daily_manifest_schema_mismatch")
    manifest_base = {
        key: value for key, value in manifest.items()
        if key != "manifest_payload_sha256"
    }
    if manifest.get("manifest_payload_sha256") != _digest(manifest_base):
        raise TechnicalShadowDailyError("daily_manifest_payload_sha256_mismatch")
    if (
        manifest.get("strategy_id") != STRATEGY_ID
        or manifest.get("shadow_account_id") != SHADOW_ACCOUNT_ID
        or manifest.get("mode") != MODE
        or manifest.get("safety") != DAILY_SAFETY
        or manifest.get("automatic_order_submission") is not False
    ):
        raise TechnicalShadowDailyError("daily_manifest_identity_or_safety_mismatch")
    expected_files = set(manifest.get("artifacts", {})) | {"manifest.json"}
    actual_files = {
        artifact.relative_to(path).as_posix()
        for artifact in path.rglob("*") if artifact.is_file()
    }
    if actual_files != expected_files:
        raise TechnicalShadowDailyError("daily_manifest_artifact_set_mismatch")
    for relative, expected in manifest.get("artifacts", {}).items():
        artifact = path / relative
        if not artifact.is_file() or _file_sha256(artifact) != expected:
            raise TechnicalShadowDailyError(f"daily_artifact_integrity_failed:{relative}")
    state = _json(path / "state.json")
    state_base = {key: value for key, value in state.items() if key != "record_sha256"}
    if state.get("record_sha256") != _digest(state_base):
        raise TechnicalShadowDailyError("daily_state_record_sha256_mismatch")
    plan = _json(path / "next_session_plan.json")
    plan_base = {key: value for key, value in plan.items() if key != "plan_payload_sha256"}
    if plan.get("plan_payload_sha256") != _digest(plan_base):
        raise TechnicalShadowDailyError("daily_plan_payload_sha256_mismatch")
    if (
        manifest.get("account_record_sha256") != state.get("record_sha256")
        or plan.get("based_on_account_record_sha256") != state.get("record_sha256")
        or state.get("safety") != DAILY_SAFETY
        or plan.get("safety") != DAILY_SAFETY
        or plan.get("automatic_order_submission") is not False
    ):
        raise TechnicalShadowDailyError("daily_state_plan_manifest_binding_mismatch")
    return manifest, _file_sha256(manifest_path)


def _latest_data_complete_capture(
    captured: CapturedData,
) -> CapturedData:
    """Trim a calendar-leading capture to the latest benchmark-complete close."""

    benchmark_dates = {
        date.fromisoformat(str(row["trading_date"]))
        for row in captured.benchmark_rows
        if row.get("close") is not None
    }
    completed = [day for day in captured.sessions if day in benchmark_dates]
    if not completed:
        raise TechnicalShadowDailyError("no_baostock_data_complete_strategy_date")
    latest = completed[-1]
    sessions = tuple(day for day in captured.sessions if day <= latest)
    if len(sessions) < 121:
        raise TechnicalShadowDailyError("latest_complete_capture_has_insufficient_history")
    return CapturedData(
        provider_id=captured.provider_id,
        provider_kind=captured.provider_kind,
        adapter_version=captured.adapter_version,
        synthetic=captured.synthetic,
        captured_at=captured.captured_at,
        sessions=sessions,
        stock_rows=captured.stock_rows,
        benchmark_rows=captured.benchmark_rows,
        receipts=captured.receipts,
    )


PERSISTENT_ARTIFACTS = {
    "state.json", "next_session_plan.json", "prior_plan_application.json",
    "lineage.json",
}


def _verified_persistent_slot(path: Path) -> tuple[dict[str, Any], str]:
    manifest_path = path / "manifest.json"
    if not manifest_path.is_file():
        raise TechnicalShadowDailyError(
            f"partial_persistent_slot_requires_manual_recovery:{path}"
        )
    manifest = _json(manifest_path)
    if manifest.get("schema_version") != "technical-shadow-persistent-state-manifest.v1":
        raise TechnicalShadowDailyError("persistent_manifest_schema_mismatch")
    manifest_base = {
        key: value for key, value in manifest.items()
        if key != "manifest_payload_sha256"
    }
    if manifest.get("manifest_payload_sha256") != _digest(manifest_base):
        raise TechnicalShadowDailyError("persistent_manifest_payload_sha256_mismatch")
    if (
        manifest.get("strategy_id") != STRATEGY_ID
        or manifest.get("shadow_account_id") != SHADOW_ACCOUNT_ID
        or manifest.get("mode") != MODE
        or manifest.get("safety") != DAILY_SAFETY
        or manifest.get("automatic_order_submission") is not False
    ):
        raise TechnicalShadowDailyError("persistent_manifest_identity_or_safety_mismatch")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != PERSISTENT_ARTIFACTS:
        raise TechnicalShadowDailyError("persistent_manifest_artifact_contract_mismatch")
    actual_files = {
        artifact.relative_to(path).as_posix()
        for artifact in path.rglob("*") if artifact.is_file()
    }
    if actual_files != PERSISTENT_ARTIFACTS | {"manifest.json"}:
        raise TechnicalShadowDailyError("persistent_manifest_artifact_set_mismatch")
    for relative, expected in artifacts.items():
        artifact = path / relative
        if _file_sha256(artifact) != expected:
            raise TechnicalShadowDailyError(
                f"persistent_artifact_integrity_failed:{relative}"
            )
    state = _json(path / "state.json")
    state_base = {key: value for key, value in state.items() if key != "record_sha256"}
    if state.get("record_sha256") != _digest(state_base):
        raise TechnicalShadowDailyError("persistent_state_record_sha256_mismatch")
    plan = _json(path / "next_session_plan.json")
    plan_base = {key: value for key, value in plan.items() if key != "plan_payload_sha256"}
    if plan.get("plan_payload_sha256") != _digest(plan_base):
        raise TechnicalShadowDailyError("persistent_plan_payload_sha256_mismatch")
    application = _json(path / "prior_plan_application.json")
    lineage = _json(path / "lineage.json")
    slot_date = date.fromisoformat(path.name)
    if (
        state.get("state_date") != slot_date.isoformat()
        or plan.get("decision_date") != slot_date.isoformat()
        or manifest.get("state_date") != slot_date.isoformat()
        or manifest.get("account_record_sha256") != state.get("record_sha256")
        or manifest.get("plan_payload_sha256") != plan.get("plan_payload_sha256")
        or state.get("prior_plan_application_sha256") != _digest(application)
        or plan.get("based_on_account_record_sha256") != state.get("record_sha256")
        or lineage.get("state_date") != slot_date.isoformat()
        or state.get("safety") != DAILY_SAFETY
        or plan.get("safety") != DAILY_SAFETY
        or plan.get("automatic_order_submission") is not False
        or manifest.get("previous_trading_date")
        != state.get("previous_trading_date")
        or manifest.get("previous_record_sha256")
        != state.get("previous_record_sha256")
        or manifest.get("lineage_kind") != lineage.get("kind")
        or manifest.get("execution_date") != plan.get("execution_date")
        or plan.get("valid_only_for_execution_date") != plan.get("execution_date")
        or application.get("execution_date") != state.get("state_date")
        or application.get("decision_date") != state.get("previous_trading_date")
        or lineage.get("schema_version")
        != "technical-shadow-persistent-lineage.v1"
        or lineage.get("strategy_id") != STRATEGY_ID
        or lineage.get("shadow_account_id") != SHADOW_ACCOUNT_ID
        or lineage.get("mode") != MODE
        or lineage.get("safety") != DAILY_SAFETY
    ):
        raise TechnicalShadowDailyError("persistent_state_plan_manifest_binding_mismatch")
    if lineage.get("kind") == "verified_legacy_slot_migration":
        source_artifacts = lineage.get("source_artifacts")
        if not isinstance(source_artifacts, dict) or any(
            artifacts[name] != source_artifacts.get(name)
            for name in (
                "state.json", "next_session_plan.json",
                "prior_plan_application.json",
            )
        ):
            raise TechnicalShadowDailyError("persistent_genesis_source_hash_mismatch")
    generated_at = manifest.get("generated_at")
    execution_open_at = manifest.get("execution_open_at")
    if generated_at is not None or execution_open_at is not None:
        if not isinstance(generated_at, str) or not isinstance(execution_open_at, str):
            raise TechnicalShadowDailyError("persistent_forward_time_binding_incomplete")
        generated = datetime.fromisoformat(generated_at)
        execution_open = datetime.fromisoformat(execution_open_at)
        if generated.tzinfo is None or execution_open.tzinfo is None or generated >= execution_open:
            raise TechnicalShadowDailyError("persistent_forward_window_not_open")
        if (
            plan.get("generated_at") != generated_at
            or plan.get("execution_open_at") != execution_open_at
            or plan.get("execution_window_status") != "OPEN"
            or manifest.get("execution_window_status") != "OPEN"
        ):
            raise TechnicalShadowDailyError("persistent_forward_plan_time_mismatch")
    return manifest, _file_sha256(manifest_path)


def _verified_state_chain(
    state_root: Path, *, expected_config_sha256: str | None = None,
) -> list[tuple[date, Path, dict[str, Any], str]]:
    if not state_root.is_dir():
        raise TechnicalShadowDailyError("persistent_state_not_initialized")
    children = list(state_root.iterdir())
    if not children:
        raise TechnicalShadowDailyError("persistent_state_not_initialized")
    slots: list[tuple[date, Path, dict[str, Any], str]] = []
    for child in children:
        if not child.is_dir():
            raise TechnicalShadowDailyError("persistent_state_root_unknown_content")
        try:
            slot_date = date.fromisoformat(child.name)
        except ValueError as exc:
            raise TechnicalShadowDailyError("persistent_state_root_unknown_content") from exc
        manifest, manifest_sha = _verified_persistent_slot(child)
        if (
            expected_config_sha256 is not None
            and manifest.get("config_sha256") != expected_config_sha256
        ):
            raise TechnicalShadowDailyError("persistent_config_sha256_mismatch")
        slots.append((slot_date, child, manifest, manifest_sha))
    slots.sort(key=lambda item: item[0])
    for index, (slot_date, slot, _manifest, _manifest_sha) in enumerate(slots):
        lineage = _json(slot / "lineage.json")
        if index == 0:
            if (
                slot_date != LEGACY_INITIAL_STATE_DATE
                or lineage.get("kind") != "verified_legacy_slot_migration"
                or lineage.get("source_manifest_sha256")
                != _manifest.get("report_manifest_sha256")
            ):
                raise TechnicalShadowDailyError("persistent_genesis_lineage_mismatch")
            continue
        previous_date, previous_slot, previous_manifest, previous_manifest_sha = slots[index - 1]
        state = _json(slot / "state.json")
        previous_state = _json(previous_slot / "state.json")
        if (
            lineage.get("kind") != "previous_persistent_state"
            or lineage.get("previous_state_date") != previous_date.isoformat()
            or lineage.get("previous_manifest_sha256") != previous_manifest_sha
            or lineage.get("previous_state_sha256") != _file_sha256(previous_slot / "state.json")
            or lineage.get("previous_plan_sha256") != _file_sha256(previous_slot / "next_session_plan.json")
            or lineage.get("previous_record_sha256") != previous_state.get("record_sha256")
            or state.get("previous_trading_date") != previous_date.isoformat()
            or state.get("previous_record_sha256") != previous_state.get("record_sha256")
            or previous_manifest.get("state_date") != previous_date.isoformat()
            or _manifest.get("config_sha256") != previous_manifest.get("config_sha256")
            or lineage.get("previous_config_sha256")
            != previous_manifest.get("config_sha256")
        ):
            raise TechnicalShadowDailyError("persistent_hash_chain_mismatch")
    return slots


def _workspace_relative(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError as exc:
        raise TechnicalShadowDailyError("migration_source_must_be_inside_workspace") from exc


def _write_complete_slot(
    *, root: Path, payloads: Mapping[str, Any], manifest: Mapping[str, Any],
    verifier: Callable[[Path], tuple[dict[str, Any], str]],
) -> tuple[str, bool]:
    expected = {name: _payload_bytes(value) for name, value in payloads.items()}
    expected["manifest.json"] = _payload_bytes(manifest)
    if root.exists():
        verifier(root)
        actual = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*") if path.is_file()
        }
        if actual != set(expected):
            raise TechnicalShadowDailyError("immutable_conflict:file_set_changed")
        for relative, raw in expected.items():
            if (root / relative).read_bytes() != raw:
                raise TechnicalShadowDailyError(f"immutable_conflict:{relative}")
        return _file_sha256(root / "manifest.json"), True
    root.parent.mkdir(parents=True, exist_ok=True)
    try:
        root.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise TechnicalShadowDailyError("daily_slot_reservation_conflict") from exc
    for relative in sorted(payloads):
        with (root / relative).open("xb") as stream:
            stream.write(expected[relative])
    with (root / "manifest.json").open("xb") as stream:
        stream.write(expected["manifest.json"])
    _verified, verified_sha = verifier(root)
    return verified_sha, False


def initialize_persistent_state(
    *, source_slot: Path, state_root: Path, config: Mapping[str, Any],
) -> tuple[Path, dict[str, Any]]:
    if source_slot.name != LEGACY_INITIAL_STATE_DATE.isoformat():
        raise TechnicalShadowDailyError("migration_requires_controlled_2026_08_25_slot")
    legacy_manifest, legacy_manifest_sha = _verified_legacy_slot(source_slot)
    if legacy_manifest.get("config_sha256") != _digest(config):
        raise TechnicalShadowDailyError("migration_config_sha256_mismatch")
    legacy_state = _json(source_slot / "state.json")
    legacy_plan = _json(source_slot / "next_session_plan.json")
    if (
        legacy_manifest.get("strategy_date")
        != LEGACY_INITIAL_STATE_DATE.isoformat()
        or legacy_state.get("state_date")
        != LEGACY_INITIAL_STATE_DATE.isoformat()
        or legacy_plan.get("decision_date")
        != LEGACY_INITIAL_STATE_DATE.isoformat()
        or legacy_manifest.get("account_record_sha256")
        != legacy_state.get("record_sha256")
        or legacy_plan.get("based_on_account_record_sha256")
        != legacy_state.get("record_sha256")
    ):
        raise TechnicalShadowDailyError("migration_source_date_or_binding_mismatch")
    target = state_root / LEGACY_INITIAL_STATE_DATE.isoformat()
    if state_root.exists():
        children = list(state_root.iterdir())
        if children and (len(children) != 1 or children[0].resolve() != target.resolve()):
            raise TechnicalShadowDailyError("migration_requires_empty_persistent_root")
        if target.exists():
            manifest, manifest_sha = _verified_persistent_slot(target)
            lineage = _json(target / "lineage.json")
            if (
                lineage.get("source_manifest_sha256") != legacy_manifest_sha
                or manifest.get("config_sha256") != _digest(config)
                or any(
                    _file_sha256(target / name)
                    != legacy_manifest["artifacts"].get(name)
                    for name in (
                        "state.json", "next_session_plan.json",
                        "prior_plan_application.json",
                    )
                )
            ):
                raise TechnicalShadowDailyError("immutable_conflict:migration_source_changed")
            return target, {
                "status": "already_initialized",
                "state_date": LEGACY_INITIAL_STATE_DATE.isoformat(),
                "persistent_state_directory": str(target.resolve()),
                "manifest_sha256": manifest_sha,
                "automatic_order_submission": False,
            }
    copied_names = (
        "state.json", "next_session_plan.json", "prior_plan_application.json",
    )
    payloads: dict[str, Any] = {}
    for name in copied_names:
        raw = (source_slot / name).read_bytes()
        if sha256(raw).hexdigest() != legacy_manifest["artifacts"].get(name):
            raise TechnicalShadowDailyError(f"migration_source_changed_after_verify:{name}")
        payloads[name] = raw
    lineage = {
        "schema_version": "technical-shadow-persistent-lineage.v1",
        "strategy_id": STRATEGY_ID,
        "shadow_account_id": SHADOW_ACCOUNT_ID,
        "mode": MODE,
        "kind": "verified_legacy_slot_migration",
        "state_date": LEGACY_INITIAL_STATE_DATE.isoformat(),
        "source_slot": _workspace_relative(source_slot),
        "source_manifest_sha256": legacy_manifest_sha,
        "source_manifest_payload_sha256": legacy_manifest["manifest_payload_sha256"],
        "source_artifacts": legacy_manifest["artifacts"],
        "source_predecessor": legacy_manifest["predecessor"],
        "safety": DAILY_SAFETY,
    }
    payloads["lineage.json"] = lineage
    artifacts = {
        name: sha256(_payload_bytes(value)).hexdigest()
        for name, value in sorted(payloads.items())
    }
    state = _json(source_slot / "state.json")
    plan = _json(source_slot / "next_session_plan.json")
    manifest_base = {
        "schema_version": "technical-shadow-persistent-state-manifest.v1",
        "strategy_id": STRATEGY_ID,
        "shadow_account_id": SHADOW_ACCOUNT_ID,
        "mode": MODE,
        "state_date": LEGACY_INITIAL_STATE_DATE.isoformat(),
        "execution_date": plan["execution_date"],
        "config_sha256": _digest(config),
        "account_record_sha256": state["record_sha256"],
        "plan_payload_sha256": plan["plan_payload_sha256"],
        "previous_trading_date": state["previous_trading_date"],
        "previous_record_sha256": state["previous_record_sha256"],
        "lineage_kind": "verified_legacy_slot_migration",
        "artifacts": artifacts,
        "report_manifest_kind": "technical-shadow-daily-manifest.v1",
        "report_manifest_sha256": legacy_manifest_sha,
        "generated_at": None,
        "execution_open_at": None,
        "historical_pit_csi800": False,
        "automatic_order_submission": False,
        "safety": DAILY_SAFETY,
    }
    manifest = dict(manifest_base)
    manifest["manifest_payload_sha256"] = _digest(manifest_base)
    if _file_sha256(source_slot / "manifest.json") != legacy_manifest_sha:
        raise TechnicalShadowDailyError("migration_source_manifest_changed_after_verify")
    manifest_sha, _ = _write_complete_slot(
        root=target, payloads=payloads, manifest=manifest,
        verifier=_verified_persistent_slot,
    )
    return target, {
        "status": "initialized",
        "state_date": LEGACY_INITIAL_STATE_DATE.isoformat(),
        "persistent_state_directory": str(target.resolve()),
        "manifest_sha256": manifest_sha,
        "automatic_order_submission": False,
    }


def _load_previous_context(
    *, strategy_date: date, state_root: Path, previous_session: date,
    config_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    slots = _verified_state_chain(
        state_root, expected_config_sha256=config_sha256
    )
    if slots[-1][0] > strategy_date:
        raise TechnicalShadowDailyError("strategy_date_precedes_persistent_head")
    prior_slots = [item for item in slots if item[0] < strategy_date]
    if not prior_slots:
        raise TechnicalShadowDailyError("strategy_date_has_no_persistent_predecessor")
    previous_date, previous_root, manifest, manifest_sha = prior_slots[-1]
    state = _json(previous_root / "state.json")
    plan = _json(previous_root / "next_session_plan.json")
    flat_cash_gap = (
        previous_date != previous_session
        and state.get("positions") == {}
        and plan.get("plan_status") == "NO_ACTION_CASH"
        and date.fromisoformat(str(plan.get("execution_date"))) < strategy_date
        and plan.get("target_positions") == {}
        and not any(
            row.get("action") in {"BUY", "SELL"}
            for row in plan.get("actions", [])
        )
    )
    if previous_date != previous_session and not flat_cash_gap:
        raise TechnicalShadowDailyError("daily_state_gap_after_persistent_head")
    if slots[-1][0] not in {previous_date, strategy_date}:
        raise TechnicalShadowDailyError("persistent_head_date_conflict")
    if (
        plan.get("execution_date") != strategy_date.isoformat()
        and not flat_cash_gap
    ):
        raise TechnicalShadowDailyError("daily_state_gap_or_plan_execution_mismatch")
    return state, plan, {
        "kind": "previous_persistent_state",
        "previous_state_date": previous_date.isoformat(),
        "previous_manifest_sha256": manifest_sha,
        "previous_state_sha256": _file_sha256(previous_root / "state.json"),
        "previous_plan_sha256": _file_sha256(previous_root / "next_session_plan.json"),
        "previous_record_sha256": state["record_sha256"],
        "previous_manifest_payload_sha256": manifest["manifest_payload_sha256"],
        "previous_config_sha256": manifest["config_sha256"],
        "flat_cash_gap": flat_cash_gap,
    }


def query_next_baostock_session(*, after_date: date) -> NextSessionEvidence:
    sdk = importlib.import_module("baostock")
    login = sdk.login()
    BaoStockProvider._check_result(login, "login")
    try:
        start = after_date + timedelta(days=1)
        end = after_date + timedelta(days=14)
        result = sdk.query_trade_dates(
            start_date=start.isoformat(), end_date=end.isoformat()
        )
        fields, raw_rows = BaoStockProvider._query_rows(
            result, "query_trade_dates:next_session"
        )
        if set(fields) != {"calendar_date", "is_trading_day"}:
            raise TechnicalShadowDailyError("baostock_next_calendar_contract_changed")
        rows = [dict(zip(fields, row, strict=True)) for row in raw_rows]
        sessions = [
            date.fromisoformat(row["calendar_date"])
            for row in rows if row["is_trading_day"] == "1"
        ]
        if not sessions:
            raise TechnicalShadowDailyError("next_trading_session_unavailable")
        receipt = {
            "provider_id": "baostock",
            "provider_kind": "real_provider",
            "adapter_version": BaoStockTechnicalShadowSource.adapter_version,
            "request": {"start_date": start.isoformat(), "end_date": end.isoformat()},
            "fields": fields,
            "rows": raw_rows,
            "raw_content_sha256": _digest({"fields": fields, "rows": raw_rows}),
        }
        return NextSessionEvidence(execution_date=sessions[0], receipt=receipt)
    finally:
        try:
            sdk.logout()
        except Exception:
            pass


def check_baostock_readiness(
    *, state_date: date, state_record_sha256: str, benchmark_id: str,
    now: datetime, allow_flat_cash_gap: bool = False,
    sdk_loader: Callable[[str], Any] = importlib.import_module,
) -> ReadinessResult:
    if now.tzinfo is None:
        raise TechnicalShadowDailyError("readiness_now_timezone_required")
    checked_at = now.astimezone(CHINA_TZ)
    completed_through = (
        checked_at.date()
        if checked_at.time() >= DECISION_CUTOFF
        else checked_at.date() - timedelta(days=1)
    )
    sdk = sdk_loader("baostock")
    calendar_fields: list[str] = []
    calendar_rows: list[list[str]] = []
    benchmark_fields: list[str] = []
    benchmark_rows: list[list[str]] = []
    logged_in = False
    try:
        login = sdk.login()
        BaoStockProvider._check_result(login, "login")
        logged_in = True
        calendar_end = max(checked_at.date(), state_date) + timedelta(days=14)
        calendar_result = sdk.query_trade_dates(
            start_date=state_date.isoformat(), end_date=calendar_end.isoformat()
        )
        calendar_fields, calendar_rows = BaoStockProvider._query_rows(
            calendar_result, "query_trade_dates:technical_shadow_readiness"
        )
        if calendar_fields != ["calendar_date", "is_trading_day"]:
            raise TechnicalShadowDailyError("baostock_readiness_calendar_contract_changed")
        benchmark_end = max(state_date, completed_through)
        benchmark_result = sdk.query_history_k_data_plus(
            to_baostock_code(benchmark_id),
            "date,code,close,tradestatus",
            start_date=state_date.isoformat(),
            end_date=benchmark_end.isoformat(),
            frequency="d", adjustflag="3",
        )
        benchmark_fields, benchmark_rows = BaoStockProvider._query_rows(
            benchmark_result, "query_history:technical_shadow_readiness_benchmark"
        )
        if benchmark_fields != ["date", "code", "close", "tradestatus"]:
            raise TechnicalShadowDailyError("baostock_readiness_benchmark_contract_changed")
    except Exception as exc:
        receipt = {
            "schema_version": "technical-shadow-data-readiness-receipt.v1",
            "provider_id": "baostock", "provider_kind": "real_provider",
            "state_date": state_date.isoformat(),
            "state_record_sha256": state_record_sha256,
            "benchmark_id": benchmark_id,
            "checked_at": checked_at.isoformat(),
            "completed_through": completed_through.isoformat(),
            "request_count": int(logged_in),
            "error_type": type(exc).__name__,
            "automatic_order_submission": False,
        }
        return ReadinessResult(
            status="DATA_NOT_READY", state_date=state_date,
            latest_completed_trading_date=None, latest_benchmark_date=None,
            strategy_date=None, execution_date=None, checked_at=checked_at,
            deadline_at=None,
            reason_codes=("readiness_provider_or_contract_failure",),
            receipt=receipt,
        )
    finally:
        if logged_in:
            try:
                sdk.logout()
            except Exception:
                pass

    calendar_dates: list[date] = []
    all_calendar_dates: list[date] = []
    seen_calendar: set[date] = set()
    for raw in calendar_rows:
        row = dict(zip(calendar_fields, raw, strict=True))
        day = date.fromisoformat(row["calendar_date"])
        if day in seen_calendar:
            raise TechnicalShadowDailyError("readiness_calendar_duplicate_date")
        seen_calendar.add(day)
        all_calendar_dates.append(day)
        if row["is_trading_day"] == "1":
            calendar_dates.append(day)
        elif row["is_trading_day"] != "0":
            raise TechnicalShadowDailyError("readiness_calendar_flag_invalid")
    if all_calendar_dates != sorted(all_calendar_dates):
        raise TechnicalShadowDailyError("readiness_calendar_dates_unsorted")
    if state_date not in calendar_dates:
        raise TechnicalShadowDailyError("persistent_state_date_not_in_trade_calendar")
    benchmark_dates: list[date] = []
    benchmark_status: dict[date, str] = {}
    benchmark_close: dict[date, str] = {}
    expected_code = to_baostock_code(benchmark_id)
    for raw in benchmark_rows:
        row = dict(zip(benchmark_fields, raw, strict=True))
        if row["code"] != expected_code:
            raise TechnicalShadowDailyError("readiness_benchmark_code_mismatch")
        day = date.fromisoformat(row["date"])
        if day in benchmark_status:
            raise TechnicalShadowDailyError("readiness_benchmark_duplicate_date")
        try:
            close = float(row["close"])
        except (TypeError, ValueError) as exc:
            raise TechnicalShadowDailyError("readiness_benchmark_close_invalid") from exc
        if not math.isfinite(close) or close <= 0:
            raise TechnicalShadowDailyError("readiness_benchmark_close_invalid")
        benchmark_dates.append(day)
        benchmark_status[day] = row["tradestatus"]
        benchmark_close[day] = row["close"]
    if benchmark_dates != sorted(benchmark_dates):
        raise TechnicalShadowDailyError("readiness_benchmark_dates_unsorted")
    latest_benchmark = benchmark_dates[-1] if benchmark_dates else None
    completed_sessions = [day for day in calendar_dates if day <= completed_through]
    latest_completed = completed_sessions[-1] if completed_sessions else None
    new_completed = [day for day in completed_sessions if day > state_date]
    status = "DATA_NOT_READY"
    strategy_date: date | None = None
    execution_date: date | None = None
    reasons: tuple[str, ...]
    if latest_benchmark is not None and latest_benchmark < state_date:
        reasons = ("benchmark_behind_persistent_state",)
    elif not new_completed and latest_benchmark == state_date:
        status = "ALREADY_PROCESSED"
        reasons = ("no_new_completed_trading_session",)
    elif len(new_completed) > 1 and not allow_flat_cash_gap:
        reasons = ("persistent_state_gap_requires_manual_recovery",)
    elif new_completed:
        candidate = new_completed[-1]
        next_sessions = [day for day in calendar_dates if day > candidate]
        if latest_benchmark != candidate:
            reasons = ("benchmark_not_published_for_new_session",)
        elif candidate not in benchmark_status or benchmark_status[candidate] != "1":
            reasons = ("benchmark_session_not_trading",)
        elif not next_sessions:
            reasons = ("next_trading_session_unavailable",)
        else:
            next_session = next_sessions[0]
            execution_open = datetime.combine(next_session, EXECUTION_OPEN, CHINA_TZ)
            if checked_at >= execution_open:
                reasons = ("execution_window_missed_no_old_plan",)
            else:
                status = "DATA_READY"
                strategy_date = candidate
                execution_date = next_session
                reasons = ("flat_cash_no_action_gap_skipped",) if len(new_completed) > 1 else (
                    "calendar_and_benchmark_advanced_one_session",
                )
    else:
        reasons = ("benchmark_or_calendar_state_inconsistent",)
    receipt = {
        "schema_version": "technical-shadow-data-readiness-receipt.v1",
        "provider_id": "baostock", "provider_kind": "real_provider",
        "state_date": state_date.isoformat(),
        "state_record_sha256": state_record_sha256,
        "benchmark_id": benchmark_id,
        "checked_at": checked_at.isoformat(),
        "completed_through": completed_through.isoformat(),
        "latest_completed_trading_date": latest_completed.isoformat() if latest_completed else None,
        "latest_benchmark_date": latest_benchmark.isoformat() if latest_benchmark else None,
        "strategy_date": strategy_date.isoformat() if strategy_date else None,
        "execution_date": execution_date.isoformat() if execution_date else None,
        "allow_flat_cash_gap": allow_flat_cash_gap,
        "skipped_completed_sessions": [
            day.isoformat() for day in new_completed[:-1]
        ] if status == "DATA_READY" and len(new_completed) > 1 else [],
        "calendar_request": {
            "start_date": state_date.isoformat(),
            "end_date": (max(checked_at.date(), state_date) + timedelta(days=14)).isoformat(),
        },
        "benchmark_request": {
            "instrument_id": benchmark_id,
            "fields": benchmark_fields,
            "start_date": state_date.isoformat(),
            "end_date": max(state_date, completed_through).isoformat(),
            "frequency": "d", "adjustflag": "3",
        },
        "calendar_raw_content_sha256": _digest({"fields": calendar_fields, "rows": calendar_rows}),
        "benchmark_raw_content_sha256": _digest({"fields": benchmark_fields, "rows": benchmark_rows}),
        "benchmark_candidate_close": benchmark_close.get(strategy_date) if strategy_date else None,
        "reason_codes": list(reasons),
        "automatic_order_submission": False,
    }
    return ReadinessResult(
        status=status, state_date=state_date,
        latest_completed_trading_date=latest_completed,
        latest_benchmark_date=latest_benchmark,
        strategy_date=strategy_date, execution_date=execution_date,
        checked_at=checked_at, deadline_at=None,
        reason_codes=reasons, receipt=receipt,
    )


def wait_until_ready(
    *, check: Callable[[], ReadinessResult], deadline: datetime,
    poll_interval_seconds: int, max_polls: int,
    clock: Callable[[], datetime] = lambda: datetime.now(CHINA_TZ),
    sleeper: Callable[[float], None] = time_module.sleep,
) -> ReadinessResult:
    if deadline.tzinfo is None:
        raise TechnicalShadowDailyError("deadline_timezone_required")
    if poll_interval_seconds <= 0 or max_polls <= 0:
        raise TechnicalShadowDailyError("bounded_polling_parameters_invalid")
    result: ReadinessResult | None = None
    local_deadline = deadline.astimezone(CHINA_TZ)
    for index in range(max_polls):
        before_check = clock().astimezone(CHINA_TZ)
        if before_check >= local_deadline and result is not None:
            return ReadinessResult(
                **{
                    **result.__dict__, "status": "DATA_NOT_READY",
                    "deadline_at": local_deadline,
                    "reason_codes": tuple(result.reason_codes)
                    + ("deadline_reached",),
                }
            )
        result = check()
        result = ReadinessResult(
            **{**result.__dict__, "deadline_at": local_deadline}
        )
        after_check = clock().astimezone(CHINA_TZ)
        if after_check >= local_deadline:
            return ReadinessResult(
                **{
                    **result.__dict__, "status": "DATA_NOT_READY",
                    "reason_codes": tuple(result.reason_codes)
                    + ("deadline_reached",),
                }
            )
        if result.status == "DATA_READY":
            return result
        remaining = (local_deadline - after_check).total_seconds()
        if remaining <= 0:
            return result
        if index == max_polls - 1:
            break
        sleeper(min(float(poll_interval_seconds), remaining))
    if result is None:
        raise TechnicalShadowDailyError("bounded_polling_did_not_run")
    return ReadinessResult(
        **{
            **result.__dict__,
            "status": "DATA_NOT_READY",
            "deadline_at": local_deadline,
            "reason_codes": tuple(result.reason_codes) + ("max_polls_reached",),
        }
    )


def _stable_data_receipt(
    *, captured: CapturedData, strategy_date: date,
    execution_evidence: NextSessionEvidence, config: Mapping[str, Any],
    generated_at: datetime,
    readiness_receipt: Mapping[str, Any] | None,
) -> dict[str, Any]:
    sessions = tuple(day for day in captured.sessions if day <= strategy_date)[-121:]
    if len(sessions) != 121 or sessions[-1] != strategy_date:
        raise TechnicalShadowDailyError("daily_requires_121_sessions_ending_strategy_date")
    instrument_ids = list(config["universe"]["instrument_ids"])
    benchmark = [
        dict(row) for row in captured.benchmark_rows
        if date.fromisoformat(str(row["trading_date"])) in set(sessions)
    ]
    stocks: dict[str, list[dict[str, Any]]] = {}
    for instrument_id in instrument_ids:
        stocks[instrument_id] = [
            dict(row) for row in captured.stock_rows.get(instrument_id, ())
            if date.fromisoformat(str(row["trading_date"])) in set(sessions)
        ]
    receipt_summaries = {}
    for key, receipt in sorted(captured.receipts.items()):
        receipt_summaries[key] = {
            field: receipt.get(field)
            for field in (
                "receipt_type", "provider_id", "provider_kind", "adapter_version",
                "synthetic", "instrument_id", "is_benchmark", "request",
                "record_count", "raw_content_sha256", "normalized_content_sha256",
            )
            if field in receipt
        }
    payload = {
        "schema_version": "technical-shadow-daily-data-receipt.v1",
        "strategy_id": STRATEGY_ID,
        "mode": MODE,
        "strategy_date": strategy_date.isoformat(),
        "execution_date": execution_evidence.execution_date.isoformat(),
        "execution_window_status": execution_evidence.execution_window_status,
        "generated_at": generated_at.isoformat(),
        "execution_open_at": datetime.combine(
            execution_evidence.execution_date, EXECUTION_OPEN, CHINA_TZ
        ).isoformat(),
        "decision_cutoff_at": datetime.combine(
            strategy_date, DECISION_CUTOFF, CHINA_TZ
        ).isoformat(),
        "provider": {
            "provider_id": captured.provider_id,
            "provider_kind": captured.provider_kind,
            "adapter_version": captured.adapter_version,
            "synthetic": captured.synthetic,
        },
        "sessions": [day.isoformat() for day in sessions],
        "benchmark_records": benchmark,
        "stock_records": stocks,
        "source_receipt_summaries": receipt_summaries,
        "next_session_calendar_receipt": execution_evidence.receipt,
        "readiness_receipt": readiness_receipt,
        "universe_basis": config["universe"]["basis"],
        "historical_pit_csi800": False,
    }
    payload["data_content_sha256"] = _digest(payload)
    return payload


def _market_drawdown(rows: Sequence[Mapping[str, Any]]) -> float:
    closes = [float(row["close"]) for row in rows if row.get("close") is not None]
    if not closes or max(closes) <= 0:
        raise TechnicalShadowDailyError("benchmark_drawdown_unavailable")
    return closes[-1] / max(closes) - 1.0


def _exposure_conditions(
    exposure: Mapping[str, Any], policy: Mapping[str, Any]
) -> dict[str, Any]:
    trend = exposure.get("benchmark_trend")
    breadth = exposure.get("market_breadth")
    volatility = exposure.get("realized_volatility")
    drawdown = exposure.get("account_drawdown")
    if any(value is None for value in (trend, breadth, volatility, drawdown)):
        return {
            "risk_off": {"data_fail_closed": True},
            "defensive": {"data_fail_closed": True},
            "risk_on": {"data_fail_closed": True},
        }
    return {
        "risk_off": {
            "benchmark_trend_lte_max": trend <= float(policy["risk_off"]["benchmark_trend_max"]),
            "market_breadth_lt_max": breadth < float(policy["risk_off"]["breadth_max"]),
            "account_drawdown_lte_max_loss": drawdown <= float(policy["risk_off"]["account_drawdown_max_loss"]),
        },
        "defensive": {
            "market_breadth_lt_trigger": breadth < float(policy["defensive"]["breadth_trigger_below"]),
            "realized_volatility_gt_max": volatility > float(policy["defensive"]["realized_vol_max"]),
            "account_drawdown_lte_max_loss": drawdown <= float(policy["defensive"]["account_drawdown_max_loss"]),
        },
        "risk_on": {
            "benchmark_trend_gt_min": trend > float(policy["risk_on"]["benchmark_trend_min"]),
            "market_breadth_gte_min": breadth >= float(policy["risk_on"]["breadth_min"]),
            "realized_volatility_lte_max": volatility <= float(policy["risk_on"]["realized_vol_max"]),
            "account_drawdown_gt_min": drawdown > float(policy["risk_on"]["account_drawdown_min"]),
        },
    }


def _consume_lots(
    lots: list[dict[str, Any]], instrument_id: str, quantity: int, day: date
) -> None:
    remaining = quantity
    for lot in sorted(
        (item for item in lots if item["instrument_id"] == instrument_id),
        key=lambda item: (item["acquired_session"], item["lot_id"]),
    ):
        if date.fromisoformat(str(lot["sellable_from_session"])) > day:
            continue
        consumed = min(int(lot["quantity"]), remaining)
        lot["quantity"] = int(lot["quantity"]) - consumed
        remaining -= consumed
        if remaining == 0:
            break
    if remaining:
        raise TechnicalShadowDailyError("t_plus_one_lot_accounting_mismatch")
    lots[:] = [item for item in lots if int(item["quantity"]) > 0]


def _apply_previous_plan(
    *, state: Mapping[str, Any], plan: Mapping[str, Any] | None,
    strategy_date: date, next_session: date,
    stock_maps: Mapping[str, Mapping[date, Mapping[str, Any]]],
    config: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    positions = {str(key): int(value) for key, value in state.get("positions", {}).items()}
    cash = _money(_decimal(state["cash"]))
    lots = [dict(item) for item in state.get("position_lots", [])]
    if plan is None:
        same_close = str(state["state_date"]) == strategy_date.isoformat()
        return {
            "cash": cash, "positions": positions, "position_lots": lots,
            "fills": [], "transaction_cost": Decimal("0.00"),
        }, {
            "status": (
                "BOOTSTRAP_ALREADY_VALUED_CLOSE"
                if same_close else "NO_PRIOR_PLAN_CASH_CARRY_FORWARD"
            ),
            "decision_date": state["previous_trading_date"] if same_close else state["state_date"],
            "execution_date": strategy_date.isoformat(),
            "fills": [],
            "reason_codes": [
                "controlled_replay_state_already_includes_strategy_date_close"
                if same_close else "no_prior_daily_plan_no_retrospective_execution"
            ],
        }

    plan_status = str(plan.get("plan_status", ""))
    if plan_status.startswith("CANCELLED_"):
        return {
            "cash": cash, "positions": positions, "position_lots": lots,
            "fills": [], "transaction_cost": Decimal("0.00"),
        }, {
            "status": "NOT_APPLIED_CANCELLED_PLAN",
            "decision_date": plan.get("decision_date"),
            "execution_date": strategy_date.isoformat(),
            "opening_cash": _money_text(cash),
            "opening_positions": dict(sorted(positions.items())),
            "closing_cash_after_open_execution": _money_text(cash),
            "closing_positions_after_open_execution": dict(sorted(positions.items())),
            "fills": [],
            "transaction_summary": {
                "commission": "0.00", "stamp_duty": "0.00",
                "transfer_fee": "0.00", "explicit_fee": "0.00",
                "slippage_cost": "0.00", "total_transaction_cost": "0.00",
                "cash_delta": "0.00",
            },
            "ledger_fills": [],
            "reason_codes": ["cancelled_plan_is_non_executable"],
        }
    if plan_status not in {"READY", "NO_ACTION_CASH"}:
        raise TechnicalShadowDailyError("previous_plan_status_not_executable")
    if (
        plan_status == "NO_ACTION_CASH"
        and plan.get("execution_date") != strategy_date.isoformat()
        and positions == {}
        and plan.get("target_positions") == {}
    ):
        carried_nav = _money_text(_decimal(state["nav"]))
        return {
            "cash": cash, "positions": positions, "position_lots": lots,
            "fills": [], "transaction_cost": Decimal("0.00"),
        }, {
            "status": "MISSED_SESSION_CARRY_FORWARD",
            "decision_date": plan.get("decision_date"),
            "execution_date": strategy_date.isoformat(),
            "missed_session_date": plan.get("execution_date"),
            "generated_late": True,
            "forward_evidence": False,
            "state_carry_forward": True,
            "opening_cash": _money_text(cash),
            "opening_positions": {},
            "opening_nav": carried_nav,
            "closing_cash_after_open_execution": _money_text(cash),
            "closing_positions_after_open_execution": {},
            "closing_nav_before_current_close": carried_nav,
            "fills": [],
            "orders": [],
            "transaction_summary": {
                "commission": "0.00", "stamp_duty": "0.00",
                "transfer_fee": "0.00", "explicit_fee": "0.00",
                "slippage_cost": "0.00", "total_transaction_cost": "0.00",
                "cash_delta": "0.00",
            },
            "ledger_fills": [],
            "reason_codes": [
                "missed_session_recorded_without_plan_or_retrospective_execution",
                "flat_cash_account_state_carried_forward_unchanged",
            ],
        }

    targets = {str(key): int(value) for key, value in plan["target_positions"].items()}
    sellable = {
        instrument_id: sum(
            int(lot["quantity"]) for lot in lots
            if lot["instrument_id"] == instrument_id
            and date.fromisoformat(str(lot["sellable_from_session"])) <= strategy_date
        )
        for instrument_id in positions
    }
    effective_targets = dict(targets)
    t1_cancellations: list[dict[str, Any]] = []
    for instrument_id, current in positions.items():
        requested_target = targets.get(instrument_id, 0)
        requested_sell = max(current - requested_target, 0)
        allowed_sell = min(requested_sell, sellable.get(instrument_id, 0))
        if allowed_sell < requested_sell:
            effective_targets[instrument_id] = current - allowed_sell
            t1_cancellations.append({
                "action": "SELL_CANCELLED",
                "instrument_id": instrument_id,
                "target_quantity": requested_target,
                "simulated_quantity": 0,
                "reason_codes": ["t_plus_one_sell_quantity_unavailable"],
            })
    execution_rows = {
        instrument_id: rows[strategy_date]
        for instrument_id, rows in stock_maps.items() if strategy_date in rows
    }
    maximum_by_instrument = {
        str(row["instrument_id"]): _decimal(row["maximum_buy_price"])
        for row in plan.get("actions", [])
        if row.get("action") == "BUY"
        and row.get("instrument_id")
        and row.get("maximum_buy_price") is not None
    }
    price_cancellations: list[dict[str, Any]] = []
    for instrument_id, target in targets.items():
        current = positions.get(instrument_id, 0)
        if target <= current or instrument_id not in maximum_by_instrument:
            continue
        row = execution_rows.get(instrument_id)
        if row is None or row.get("open") is None:
            continue
        actual = _execution_cost(
            side="BUY", quantity=target - current,
            open_price=_decimal(row["open"]), config=config,
        )
        if actual["execution_price"] > maximum_by_instrument[instrument_id]:
            effective_targets[instrument_id] = current
            price_cancellations.append({
                "action": "BUY_CANCELLED", "instrument_id": instrument_id,
                "target_quantity": target, "simulated_quantity": 0,
                "reference_price": _money_text(actual["reference_price"]),
                "execution_price": None,
                "maximum_buy_price": _money_text(maximum_by_instrument[instrument_id]),
                "reason_codes": ["buy_price_above_maximum_buy_price"],
            })
    before = dict(positions)
    positions, cash, fills, transaction_cost = _execute_targets(
        targets=effective_targets,
        positions=dict(positions),
        cash=cash,
        execution_rows=execution_rows,
        config=config,
        buy_order=[str(item) for item in plan.get("selected_instruments", [])],
    )
    fills.extend(t1_cancellations)
    fills.extend(price_cancellations)
    for fill in fills:
        if fill["action"] == "SELL":
            _consume_lots(
                lots, str(fill["instrument_id"]), int(fill["simulated_quantity"]),
                strategy_date,
            )
        elif fill["action"] == "BUY":
            instrument_id = str(fill["instrument_id"])
            lot_id = _digest({
                "instrument_id": instrument_id,
                "acquired_session": strategy_date.isoformat(),
                "quantity": int(fill["simulated_quantity"]),
                "execution_price": fill["execution_price"],
                "predecessor": state["previous_record_sha256"],
            })
            lots.append({
                "lot_id": lot_id,
                "instrument_id": instrument_id,
                "quantity": int(fill["simulated_quantity"]),
                "acquired_session": strategy_date.isoformat(),
                "sellable_from_session": next_session.isoformat(),
                "acquisition_fill_sha256": _digest(fill),
            })
    for instrument_id, quantity in positions.items():
        lot_quantity = sum(
            int(item["quantity"]) for item in lots
            if item["instrument_id"] == instrument_id
        )
        if lot_quantity != quantity:
            raise TechnicalShadowDailyError(
                f"position_lot_reconciliation_failed:{instrument_id}"
            )
    summary, ledger_fills = _ledger_transaction_accounting(fills)
    application = {
        "status": "APPLIED",
        "decision_date": plan["decision_date"],
        "execution_date": strategy_date.isoformat(),
        "opening_cash": _money_text(_decimal(state["cash"])),
        "opening_positions": before,
        "closing_cash_after_open_execution": _money_text(cash),
        "closing_positions_after_open_execution": dict(sorted(positions.items())),
        "fills": fills,
        "transaction_summary": summary,
        "ledger_fills": ledger_fills,
        "reason_codes": ["previous_immutable_plan_applied_at_real_open"],
    }
    return {
        "cash": cash, "positions": positions, "position_lots": lots,
        "fills": fills, "transaction_cost": transaction_cost,
    }, application


def _planned_actions(
    *, targets: Mapping[str, int], positions: Mapping[str, int],
    selected: Sequence[str], close_by_id: Mapping[str, Decimal],
    config: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    costs: list[dict[str, Any]] = []
    for instrument_id in sorted(set(positions) | set(targets)):
        current = int(positions.get(instrument_id, 0))
        target = int(targets.get(instrument_id, 0))
        delta = target - current
        reference = close_by_id.get(instrument_id)
        if delta == 0:
            if current:
                actions.append({
                    "action": "HOLD", "instrument_id": instrument_id,
                    "current_quantity": current, "target_quantity": target,
                    "quantity": 0,
                    "reference_price": _money_text(reference) if reference else None,
                    "maximum_buy_price": None,
                    "commission": "0.00", "stamp_duty": "0.00",
                    "transfer_fee": "0.00", "explicit_fee": "0.00",
                    "slippage_cost": "0.00", "total_transaction_cost": "0.00",
                    "reason_codes": ["incumbent_within_hold_band"],
                })
            continue
        side = "BUY" if delta > 0 else "SELL"
        quantity = abs(delta)
        if reference is None:
            actions.append({
                "action": f"{side}_CANCELLED", "instrument_id": instrument_id,
                "current_quantity": current, "target_quantity": target,
                "quantity": 0, "reference_price": None,
                "maximum_buy_price": None,
                "commission": "0.00", "stamp_duty": "0.00",
                "transfer_fee": "0.00", "explicit_fee": "0.00",
                "slippage_cost": "0.00", "total_transaction_cost": "0.00",
                "reason_codes": ["decision_reference_price_unavailable"],
            })
            continue
        estimate = _execution_cost(
            side=side, quantity=quantity, open_price=reference, config=config
        )
        action = {
            "action": side, "instrument_id": instrument_id,
            "current_quantity": current, "target_quantity": target,
            "quantity": quantity,
            "reference_price": _money_text(estimate["reference_price"]),
            "maximum_buy_price": (
                _money_text(estimate["execution_price"]) if side == "BUY" else None
            ),
            "notional_at_reference_price": _money_text(estimate["notional_at_reference_price"]),
            "notional_at_execution_price": _money_text(estimate["notional_at_execution_price"]),
            "commission": _money_text(estimate["commission"]),
            "stamp_duty": _money_text(estimate["stamp_duty"]),
            "transfer_fee": _money_text(estimate["transfer_fee"]),
            "explicit_fee": _money_text(estimate["explicit_fee"]),
            "slippage_cost": _money_text(estimate["slippage_cost"]),
            "total_transaction_cost": _money_text(estimate["total_transaction_cost"]),
            "reason_codes": ["manual_d_plus_1_open_plan_not_order"],
        }
        actions.append(action)
        costs.append(action)
    if not actions:
        actions.append({
            "action": "CASH", "instrument_id": None,
            "current_quantity": 0, "target_quantity": 0, "quantity": 0,
            "reference_price": None, "maximum_buy_price": None,
            "commission": "0.00", "stamp_duty": "0.00",
            "transfer_fee": "0.00", "explicit_fee": "0.00",
            "slippage_cost": "0.00", "total_transaction_cost": "0.00",
            "reason_codes": ["residual_cash_preserved"],
        })
    summary = {
        "commission": _money_text(sum((_decimal(row["commission"]) for row in costs), Decimal("0"))),
        "stamp_duty": _money_text(sum((_decimal(row["stamp_duty"]) for row in costs), Decimal("0"))),
        "transfer_fee": _money_text(sum((_decimal(row["transfer_fee"]) for row in costs), Decimal("0"))),
        "explicit_fee": _money_text(sum((_decimal(row["explicit_fee"]) for row in costs), Decimal("0"))),
        "slippage_cost": _money_text(sum((_decimal(row["slippage_cost"]) for row in costs), Decimal("0"))),
        "total_transaction_cost": _money_text(sum((_decimal(row["total_transaction_cost"]) for row in costs), Decimal("0"))),
    }
    return actions, summary


def _daily_report(
    *, strategy_date: date, execution_date: date,
    state: Mapping[str, Any], exposure: Mapping[str, Any],
    plan: Mapping[str, Any], application: Mapping[str, Any],
) -> str:
    lines = [
        f"# Technical Shadow 每日报告 {strategy_date.isoformat()}", "",
        f"- 决策日 / 人工计划执行日：`{strategy_date}` / `{execution_date}`",
        f"- 模式：`{MODE}`；市场状态：`{exposure['final_state']}`",
        f"- 当前现金 / NAV：`{state['cash']}` / `{state['nav']}`",
        f"- 当前持仓：`{state['positions']}`",
        f"- 目标总仓位：`{exposure['target_gross_exposure']:.2%}`；目标持仓：`{plan['target_positions']}`",
        f"- 前一计划应用：`{application['status']}`",
        "", "## Exposure 输入、阈值与命中条件", "",
    ]
    for key, item in exposure["inputs"].items():
        lines.append(
            f"- `{key}` = `{item['value']}`；used_by_policy=`{str(item['used_by_policy']).lower()}`；threshold=`{item.get('threshold')}`"
        )
    lines.extend([
        f"- 条件：`{exposure['condition_results']}`",
        f"- 命中规则 / 最终状态：`{exposure['matched_rule']}` / `{exposure['final_state']}`",
        "", "## BUY / SELL / HOLD / CASH 人工计划", "",
        "| 动作 | 标的 | 当前数 | 目标数 | 计划数 | 参考价 | 最大买入价 | 显式费用 | 滑点 | 总成本 | 原因 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ])
    for row in plan["actions"]:
        lines.append(
            "| {action} | {instrument} | {current} | {target} | {quantity} | {reference} | {maximum} | {explicit} | {slippage} | {total} | {reason} |".format(
                action=row["action"], instrument=row.get("instrument_id") or "CASH",
                current=row.get("current_quantity", 0), target=row.get("target_quantity", 0),
                quantity=row.get("quantity", 0), reference=row.get("reference_price") or "-",
                maximum=row.get("maximum_buy_price") or "-", explicit=row.get("explicit_fee", "0.00"),
                slippage=row.get("slippage_cost", "0.00"), total=row.get("total_transaction_cost", "0.00"),
                reason=",".join(row.get("reason_codes", [])),
            )
        )
    lines.extend([
        "", f"- 计划成本拆分：`{plan['cost_summary']}`",
        f"- no-trade 原因：`{plan['no_trade_reason_codes']}`",
        f"- 取消条件：`{plan['cancellation_conditions']}`",
        "- 该文件不是订单；不会连接券商或自动提交。",
        "- 当前股票池不是历史 PIT 中证800；本策略不具备 Paper、交易、真实资金或 LIVE 准入。",
        "",
    ])
    return "\n".join(lines)


def _payload_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.replace("\r\n", "\n").encode("utf-8")
    return _canonical_bytes(value)


REPORT_ARTIFACTS = {
    "data_receipt.json", "ranking.json", "exposure.json",
    "portfolio_decision.json", "daily_report.md",
}


def _verified_report_slot(path: Path) -> tuple[dict[str, Any], str]:
    manifest_path = path / "report_manifest.json"
    if not manifest_path.is_file():
        raise TechnicalShadowDailyError(
            f"partial_report_slot_requires_manual_recovery:{path}"
        )
    manifest = _json(manifest_path)
    if manifest.get("schema_version") != "technical-shadow-daily-report-manifest.v1":
        raise TechnicalShadowDailyError("daily_report_manifest_schema_mismatch")
    base = {
        key: value for key, value in manifest.items()
        if key != "manifest_payload_sha256"
    }
    if manifest.get("manifest_payload_sha256") != _digest(base):
        raise TechnicalShadowDailyError("daily_report_manifest_payload_sha256_mismatch")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != REPORT_ARTIFACTS:
        raise TechnicalShadowDailyError("daily_report_artifact_contract_mismatch")
    actual = {
        item.relative_to(path).as_posix()
        for item in path.rglob("*") if item.is_file()
    }
    if actual != REPORT_ARTIFACTS | {"report_manifest.json"}:
        raise TechnicalShadowDailyError("daily_report_artifact_set_mismatch")
    for relative, expected in artifacts.items():
        if _file_sha256(path / relative) != expected:
            raise TechnicalShadowDailyError(
                f"daily_report_artifact_integrity_failed:{relative}"
            )
    if (
        manifest.get("strategy_id") != STRATEGY_ID
        or manifest.get("mode") != MODE
        or manifest.get("safety") != DAILY_SAFETY
        or manifest.get("automatic_order_submission") is not False
    ):
        raise TechnicalShadowDailyError("daily_report_identity_or_safety_mismatch")
    return manifest, _file_sha256(manifest_path)


def _roots_are_separate(state_root: Path, report_root: Path) -> bool:
    state = state_root.resolve()
    report = report_root.resolve()
    return state != report and not state.is_relative_to(report) and not report.is_relative_to(state)


def _publish_split_create_only(
    *, persistent_root: Path, report_root: Path,
    persistent_payloads: Mapping[str, Any], persistent_manifest: Mapping[str, Any],
    report_payloads: Mapping[str, Any], report_manifest: Mapping[str, Any],
) -> tuple[str, bool]:
    if not _roots_are_separate(persistent_root.parent, report_root.parent):
        raise TechnicalShadowDailyError("persistent_and_report_roots_must_be_separate")
    expected_persistent = {
        name: _payload_bytes(value) for name, value in persistent_payloads.items()
    }
    expected_persistent["manifest.json"] = _payload_bytes(persistent_manifest)
    expected_report = {
        name: _payload_bytes(value) for name, value in report_payloads.items()
    }
    expected_report["report_manifest.json"] = _payload_bytes(report_manifest)
    if not persistent_root.exists() and report_root.exists():
        _verified_report_slot(report_root)
        actual_report = {
            item.relative_to(report_root).as_posix()
            for item in report_root.rglob("*") if item.is_file()
        }
        if actual_report != set(expected_report):
            raise TechnicalShadowDailyError("immutable_conflict:report_file_set_changed")
        for relative, raw in expected_report.items():
            if (report_root / relative).read_bytes() != raw:
                raise TechnicalShadowDailyError(f"immutable_conflict:report:{relative}")
    if persistent_root.exists():
        _verified_persistent_slot(persistent_root)
        actual = {
            item.relative_to(persistent_root).as_posix()
            for item in persistent_root.rglob("*") if item.is_file()
        }
        if actual != set(expected_persistent):
            raise TechnicalShadowDailyError("immutable_conflict:persistent_file_set_changed")
        for relative, raw in expected_persistent.items():
            if (persistent_root / relative).read_bytes() != raw:
                raise TechnicalShadowDailyError(f"immutable_conflict:persistent:{relative}")
        if report_root.exists():
            _verified_report_slot(report_root)
            actual_report = {
                item.relative_to(report_root).as_posix()
                for item in report_root.rglob("*") if item.is_file()
            }
            if actual_report != set(expected_report):
                raise TechnicalShadowDailyError("immutable_conflict:report_file_set_changed")
            for relative, raw in expected_report.items():
                if (report_root / relative).read_bytes() != raw:
                    raise TechnicalShadowDailyError(f"immutable_conflict:report:{relative}")
        return _file_sha256(persistent_root / "manifest.json"), True

    persistent_root.parent.mkdir(parents=True, exist_ok=True)
    try:
        persistent_root.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise TechnicalShadowDailyError("persistent_slot_reservation_conflict") from exc
    for relative in sorted(persistent_payloads):
        with (persistent_root / relative).open("xb") as stream:
            stream.write(expected_persistent[relative])

    if report_root.exists():
        _verified_report_slot(report_root)
        actual_report = {
            item.relative_to(report_root).as_posix()
            for item in report_root.rglob("*") if item.is_file()
        }
        if actual_report != set(expected_report):
            raise TechnicalShadowDailyError("immutable_conflict:report_file_set_changed")
        for relative, raw in expected_report.items():
            if (report_root / relative).read_bytes() != raw:
                raise TechnicalShadowDailyError(f"immutable_conflict:report:{relative}")
    else:
        report_root.parent.mkdir(parents=True, exist_ok=True)
        try:
            report_root.mkdir(exist_ok=False)
        except FileExistsError as exc:
            raise TechnicalShadowDailyError("report_slot_reservation_conflict") from exc
        for relative in sorted(report_payloads):
            with (report_root / relative).open("xb") as stream:
                stream.write(expected_report[relative])
        with (report_root / "report_manifest.json").open("xb") as stream:
            stream.write(expected_report["report_manifest.json"])

    with (persistent_root / "manifest.json").open("xb") as stream:
        stream.write(expected_persistent["manifest.json"])
    _verified_report_slot(report_root)
    _verified, verified_sha = _verified_persistent_slot(persistent_root)
    return verified_sha, False


def run_daily(
    *, config: Mapping[str, Any], captured: CapturedData,
    execution_evidence: NextSessionEvidence, state_root: Path,
    report_root: Path, generated_at: datetime | None = None,
    readiness_receipt: Mapping[str, Any] | None = None,
    allow_test_provider: bool = False,
) -> tuple[Path, dict[str, Any]]:
    validate_source_provenance(
        provider_id=captured.provider_id,
        provider_kind=captured.provider_kind,
        synthetic=captured.synthetic,
    )
    if not allow_test_provider and (
        captured.provider_id != "baostock"
        or captured.provider_kind != "real_provider"
        or captured.synthetic
    ):
        raise TechnicalShadowDailyError("real_baostock_provider_required")
    if len(captured.sessions) < ALPHA_LOOKBACK_SESSIONS + 1:
        raise TechnicalShadowDailyError("captured_sessions_insufficient")
    if len(set(captured.sessions)) != len(captured.sessions) or tuple(sorted(captured.sessions)) != captured.sessions:
        raise TechnicalShadowDailyError("captured_calendar_duplicate_or_unsorted")
    strategy_date = captured.sessions[-1]
    if execution_evidence.execution_date <= strategy_date:
        raise TechnicalShadowDailyError("execution_date_must_follow_strategy_date")
    config_sha256 = _digest(config)
    slots = _verified_state_chain(
        state_root, expected_config_sha256=config_sha256
    )
    existing = next((item for item in slots if item[0] == strategy_date), None)
    if existing is not None:
        existing_generated = existing[2].get("generated_at")
        if existing_generated is None:
            raise TechnicalShadowDailyError("persistent_forward_generated_at_missing")
        generated_at = datetime.fromisoformat(str(existing_generated))
    elif generated_at is None:
        if not allow_test_provider:
            raise TechnicalShadowDailyError("real_forward_generated_at_required")
        generated_at = datetime.combine(strategy_date, DECISION_CUTOFF, CHINA_TZ)
    if generated_at.tzinfo is None:
        raise TechnicalShadowDailyError("generated_at_timezone_required")
    generated_at = generated_at.astimezone(CHINA_TZ)
    execution_open_at = datetime.combine(
        execution_evidence.execution_date, EXECUTION_OPEN, CHINA_TZ
    )
    if (
        execution_evidence.execution_window_status != "OPEN"
        or generated_at >= execution_open_at
    ):
        raise TechnicalShadowDailyError("execution_window_missed_no_old_plan")
    if readiness_receipt is not None:
        if (
            readiness_receipt.get("strategy_date") != strategy_date.isoformat()
            or readiness_receipt.get("execution_date")
            != execution_evidence.execution_date.isoformat()
        ):
            raise TechnicalShadowDailyError("readiness_full_capture_date_mismatch")
    prior_state, prior_plan, predecessor = _load_previous_context(
        strategy_date=strategy_date, state_root=state_root,
        previous_session=captured.sessions[-2], config_sha256=config_sha256,
    )
    prior_date = date.fromisoformat(str(prior_state["state_date"]))
    skipped_sessions = [
        day.isoformat() for day in captured.sessions
        if prior_date < day < strategy_date
    ]
    if predecessor.get("flat_cash_gap"):
        if not skipped_sessions:
            raise TechnicalShadowDailyError("flat_cash_gap_sessions_missing")
        if readiness_receipt is None or readiness_receipt.get(
            "skipped_completed_sessions"
        ) != skipped_sessions:
            raise TechnicalShadowDailyError("flat_cash_gap_readiness_binding_mismatch")
        predecessor["skipped_trading_dates"] = skipped_sessions
    elif skipped_sessions:
        raise TechnicalShadowDailyError("unexpected_intervening_trading_sessions")
    prior_state_date = date.fromisoformat(str(prior_state["state_date"]))
    if (
        prior_state_date != strategy_date
        and prior_state_date != captured.sessions[-2]
        and not predecessor.get("flat_cash_gap")
    ):
        raise TechnicalShadowDailyError(
            "previous_controlled_date_not_previous_session"
        )
    if predecessor.get("previous_plan_sha256") != _file_sha256(
        state_root / str(prior_plan["decision_date"]) / "next_session_plan.json"
    ):
        raise TechnicalShadowDailyError("previous_plan_hash_mismatch")
    if prior_plan.get("safety") != DAILY_SAFETY:
        raise TechnicalShadowDailyError("previous_plan_safety_mismatch")

    instrument_ids = list(config["universe"]["instrument_ids"])
    stock_maps = {
        item: _strict_row_map(captured.stock_rows.get(item, ()), instrument_id=item)
        for item in instrument_ids
    }
    benchmark_map = _strict_row_map(
        captured.benchmark_rows, instrument_id=str(config["data"]["benchmark_id"])
    )
    if readiness_receipt is not None:
        ready_close = readiness_receipt.get("benchmark_candidate_close")
        captured_ready = benchmark_map.get(strategy_date)
        if (
            ready_close is None
            or captured_ready is None
            or captured_ready.get("close") is None
            or _decimal(ready_close) != _decimal(captured_ready["close"])
            or captured_ready.get("trading_status") != "traded"
        ):
            raise TechnicalShadowDailyError("readiness_full_benchmark_toctou_mismatch")
    sessions = tuple(day for day in captured.sessions if day <= strategy_date)[-121:]
    if len(sessions) != 121 or sessions[-1] != strategy_date:
        raise TechnicalShadowDailyError("daily_121_session_window_unavailable")
    for day in sessions:
        if day not in benchmark_map:
            raise TechnicalShadowDailyError("benchmark_missing_required_session")
        _validate_row_cutoff(benchmark_map[day], strategy_date)
    for item in instrument_ids:
        for row in captured.stock_rows.get(item, ()):
            if date.fromisoformat(str(row["trading_date"])) <= strategy_date:
                _validate_row_cutoff(row, strategy_date)

    account, application = _apply_previous_plan(
        state=prior_state, plan=prior_plan, strategy_date=strategy_date,
        next_session=execution_evidence.execution_date,
        stock_maps=stock_maps, config=config,
    )
    positions = account["positions"]
    cash = account["cash"]
    close_by_id = {
        item: _decimal(stock_maps[item][strategy_date]["close"])
        for item in positions
        if strategy_date in stock_maps[item]
        and stock_maps[item][strategy_date].get("close") is not None
    }
    if set(positions) != set(close_by_id):
        raise TechnicalShadowDailyError("held_position_close_unavailable")
    nav = _money(cash + sum(close_by_id[item] * quantity for item, quantity in positions.items()))
    peak_nav = max(_money(_decimal(prior_state["peak_nav"])), nav)
    drawdown = float(nav / peak_nav - Decimal("1"))

    stock_slices = {
        item: tuple(stock_maps[item][day] for day in sessions if day in stock_maps[item])
        for item in instrument_ids
    }
    benchmark_slice = tuple(benchmark_map[day] for day in sessions)
    ranking_rows = rank_technical_alpha_shadow(
        decision_date=strategy_date, sessions=sessions,
        instrument_ids=instrument_ids, stock_rows=stock_slices,
        benchmark_rows=benchmark_slice,
        winsor_lower_quantile=float(config["alpha"]["winsor_lower_quantile"]),
        winsor_upper_quantile=float(config["alpha"]["winsor_upper_quantile"]),
    )
    ranking = {
        "schema_version": "technical-shadow-daily-ranking.v1",
        "strategy_id": STRATEGY_ID, "mode": MODE,
        "strategy_date": strategy_date.isoformat(),
        "universe_basis": config["universe"]["basis"],
        "historical_pit_csi800": False, "rows": ranking_rows,
        "safety": DAILY_SAFETY,
    }
    ranking["ranking_payload_sha256"] = _digest(ranking)

    eligible_ids = {
        str(row["instrument_id"]) for row in ranking_rows if row["eligibility"]
    }
    exposure_core = compute_technical_shadow_exposure(
        benchmark_rows=benchmark_slice,
        eligible_stock_rows=[stock_slices[item] for item in instrument_ids if item in eligible_ids],
        current_nav=float(nav), peak_nav=float(peak_nav), policy=config["exposure"],
    )
    market_drawdown = _market_drawdown(benchmark_slice)
    condition_results = _exposure_conditions(exposure_core, config["exposure"])
    final_state = str(exposure_core["market_state"])
    thresholds = {
        "risk_off": config["exposure"]["risk_off"],
        "defensive": config["exposure"]["defensive"],
        "risk_on": config["exposure"]["risk_on"],
    }
    exposure = {
        "schema_version": "technical-shadow-daily-exposure.v1",
        "strategy_id": STRATEGY_ID, "mode": MODE,
        "strategy_date": strategy_date.isoformat(),
        "inputs": {
            "benchmark_trend": {"value": exposure_core["benchmark_trend"], "used_by_policy": True, "threshold": thresholds},
            "market_breadth": {"value": exposure_core["market_breadth"], "used_by_policy": True, "threshold": thresholds},
            "realized_volatility": {"value": exposure_core["realized_volatility"], "used_by_policy": True, "threshold": thresholds},
            "market_drawdown": {"value": market_drawdown, "used_by_policy": False, "threshold": None},
            "account_drawdown": {"value": exposure_core["account_drawdown"], "used_by_policy": True, "threshold": thresholds},
            "eligible_stock_count": {"value": len(eligible_ids), "used_by_policy": False, "threshold": None},
        },
        "thresholds": thresholds,
        "condition_results": condition_results,
        "matched_rule": f"{final_state.lower()}_rule" if not exposure_core["data_fail_closed"] else "data_fail_closed",
        "previous_state": prior_state["exposure_state"],
        "candidate_state": final_state,
        "pending_state": None,
        "hysteresis_count": 0,
        "final_state": final_state,
        "target_gross_exposure": float(exposure_core["target_gross_exposure"]),
        "data_fail_closed": bool(exposure_core["data_fail_closed"]),
        "reason_codes": list(exposure_core["reason_codes"]),
        "safety": DAILY_SAFETY,
    }
    exposure["exposure_payload_sha256"] = _digest(exposure)

    decision_close_by_id = {
        item: _decimal(stock_maps[item][strategy_date]["close"])
        for item in instrument_ids
        if strategy_date in stock_maps[item]
        and stock_maps[item][strategy_date].get("close") is not None
    }
    targets, selected = _plan_targets(
        ranking=ranking_rows, positions=positions, nav=nav,
        target_exposure=float(exposure["target_gross_exposure"]),
        max_positions=int(config["portfolio"]["max_positions"]),
        max_weight=_decimal(config["portfolio"]["max_position_weight"]),
        lot_size=int(config["portfolio"]["lot_size"]),
        close_by_id=decision_close_by_id,
    )
    actions, cost_summary = _planned_actions(
        targets=targets, positions=positions, selected=selected,
        close_by_id=decision_close_by_id, config=config,
    )
    no_trade_reasons = _cash_reason_codes(
        ranking=ranking_rows, positions=positions, selected=selected,
        target_exposure=float(exposure["target_gross_exposure"]),
    )
    has_planned_trade = any(row["action"] in {"BUY", "SELL"} for row in actions)
    if execution_evidence.execution_window_status == "MISSED" and has_planned_trade:
        cancelled_actions: list[dict[str, Any]] = []
        for row in actions:
            changed = dict(row)
            if row["action"] in {"BUY", "SELL"}:
                changed["requested_quantity_before_cancellation"] = row["quantity"]
                changed["action"] = f"{row['action']}_CANCELLED"
                changed["quantity"] = 0
                changed["commission"] = "0.00"
                changed["stamp_duty"] = "0.00"
                changed["transfer_fee"] = "0.00"
                changed["explicit_fee"] = "0.00"
                changed["slippage_cost"] = "0.00"
                changed["total_transaction_cost"] = "0.00"
                changed["reason_codes"] = list(row["reason_codes"]) + [
                    "missed_d_plus_1_open_cutoff_no_retrospective_plan"
                ]
            cancelled_actions.append(changed)
        actions = cancelled_actions
        cost_summary = {
            "commission": "0.00", "stamp_duty": "0.00",
            "transfer_fee": "0.00", "explicit_fee": "0.00",
            "slippage_cost": "0.00", "total_transaction_cost": "0.00",
        }
        no_trade_reasons = list(no_trade_reasons) + [
            "MISSED_D_PLUS_1_OPEN_CUTOFF"
        ]

    sellable = {
        item: sum(
            int(lot["quantity"]) for lot in account["position_lots"]
            if lot["instrument_id"] == item
            and date.fromisoformat(str(lot["sellable_from_session"])) <= strategy_date
        )
        for item in positions
    }
    cumulative_explicit = _money(
        _decimal(prior_state.get("cumulative_explicit_fee", "0"))
        + _decimal(application.get("transaction_summary", {}).get("explicit_fee", "0"))
    )
    cumulative_slippage = _money(
        _decimal(prior_state.get("cumulative_slippage_cost", "0"))
        + _decimal(application.get("transaction_summary", {}).get("slippage_cost", "0"))
    )
    state_base = {
        "schema_version": "technical-shadow-daily-account-state.v1",
        "strategy_id": STRATEGY_ID, "shadow_account_id": SHADOW_ACCOUNT_ID,
        "mode": MODE, "state_date": strategy_date.isoformat(),
        "previous_trading_date": (
            prior_state["previous_trading_date"]
            if prior_plan is None and prior_state["state_date"] == strategy_date.isoformat()
            else prior_state["state_date"]
        ),
        "previous_record_sha256": prior_state["previous_record_sha256"] if prior_plan is None else prior_state["record_sha256"],
        "cash": _money_text(cash), "positions": dict(sorted(positions.items())),
        "position_lots": sorted(account["position_lots"], key=lambda item: item["lot_id"]),
        "sellable_quantities": dict(sorted((key, value) for key, value in sellable.items() if value)),
        "nav": _money_text(nav), "peak_nav": _money_text(peak_nav),
        "drawdown": drawdown, "exposure_state": final_state,
        "pending_state": None, "hysteresis_count": 0,
        "cumulative_explicit_fee": _money_text(cumulative_explicit),
        "cumulative_slippage_cost": _money_text(cumulative_slippage),
        "cumulative_transaction_cost": _money_text(cumulative_explicit + cumulative_slippage),
        "prior_plan_application_sha256": _digest(application),
        "safety": DAILY_SAFETY,
    }
    state = dict(state_base)
    state["record_sha256"] = _digest(state_base)

    plan = {
        "schema_version": "technical-shadow-next-session-plan.v1",
        "strategy_id": STRATEGY_ID, "shadow_account_id": SHADOW_ACCOUNT_ID,
        "mode": MODE, "plan_type": "manual_shadow_plan_not_order",
        "plan_status": (
            "CANCELLED_MISSED_D_PLUS_1_OPEN_CUTOFF"
            if execution_evidence.execution_window_status == "MISSED" and has_planned_trade
            else "NO_ACTION_CASH" if not has_planned_trade else "READY"
        ),
        "execution_window_status": execution_evidence.execution_window_status,
        "generated_at": generated_at.isoformat(),
        "execution_open_at": execution_open_at.isoformat(),
        "decision_date": strategy_date.isoformat(),
        "execution_date": execution_evidence.execution_date.isoformat(),
        "valid_only_for_execution_date": execution_evidence.execution_date.isoformat(),
        "based_on_account_record_sha256": state["record_sha256"],
        "ranking_payload_sha256": ranking["ranking_payload_sha256"],
        "exposure_payload_sha256": exposure["exposure_payload_sha256"],
        "target_gross_exposure": exposure["target_gross_exposure"],
        "selected_instruments": selected,
        "target_positions": dict(sorted(targets.items())),
        "actions": actions, "cost_summary": cost_summary,
        "no_trade_reason_codes": no_trade_reasons,
        "cancellation_conditions": list(CANCELLATION_CONDITIONS),
        "lot_size": int(config["portfolio"]["lot_size"]),
        "max_positions": int(config["portfolio"]["max_positions"]),
        "max_position_weight": config["portfolio"]["max_position_weight"],
        "automatic_order_submission": False,
        "safety": DAILY_SAFETY,
    }
    plan["plan_payload_sha256"] = _digest(plan)

    action_counts = {
        action: sum(row["action"] == action for row in actions)
        for action in ("BUY", "SELL", "HOLD", "CASH")
    }
    portfolio_decision = {
        "schema_version": "technical-shadow-daily-portfolio-decision.v1",
        "strategy_id": STRATEGY_ID, "mode": MODE,
        "strategy_date": strategy_date.isoformat(),
        "execution_date": execution_evidence.execution_date.isoformat(),
        "generated_at": generated_at.isoformat(),
        "execution_open_at": execution_open_at.isoformat(),
        "current_cash": state["cash"], "current_positions": state["positions"],
        "current_nav": state["nav"], "target_positions": plan["target_positions"],
        "target_gross_exposure": exposure["target_gross_exposure"],
        "action_counts": action_counts, "actions": actions,
        "prior_plan_application": application,
        "no_trade_reason_codes": no_trade_reasons,
        "automatic_order_submission": False, "safety": DAILY_SAFETY,
    }
    portfolio_decision["decision_payload_sha256"] = _digest(portfolio_decision)

    data_receipt = _stable_data_receipt(
        captured=captured, strategy_date=strategy_date,
        execution_evidence=execution_evidence, config=config,
        generated_at=generated_at, readiness_receipt=readiness_receipt,
    )
    report = _daily_report(
        strategy_date=strategy_date, execution_date=execution_evidence.execution_date,
        state=state, exposure=exposure, plan=plan, application=application,
    )
    report_payloads: dict[str, Any] = {
        "data_receipt.json": data_receipt,
        "ranking.json": ranking,
        "exposure.json": exposure,
        "portfolio_decision.json": portfolio_decision,
        "daily_report.md": report,
    }
    report_artifacts = {
        name: sha256(_payload_bytes(value)).hexdigest()
        for name, value in sorted(report_payloads.items())
    }
    report_manifest_base = {
        "schema_version": "technical-shadow-daily-report-manifest.v1",
        "strategy_id": STRATEGY_ID, "shadow_account_id": SHADOW_ACCOUNT_ID,
        "mode": MODE, "strategy_date": strategy_date.isoformat(),
        "execution_date": execution_evidence.execution_date.isoformat(),
        "generated_at": generated_at.isoformat(),
        "execution_open_at": execution_open_at.isoformat(),
        "config_sha256": _digest(config),
        "account_record_sha256": state["record_sha256"],
        "state_sha256": sha256(_payload_bytes(state)).hexdigest(),
        "plan_payload_sha256": plan["plan_payload_sha256"],
        "plan_sha256": sha256(_payload_bytes(plan)).hexdigest(),
        "artifacts": report_artifacts,
        "provider": data_receipt["provider"],
        "historical_pit_csi800": False,
        "automatic_order_submission": False, "safety": DAILY_SAFETY,
    }
    report_manifest = dict(report_manifest_base)
    report_manifest["manifest_payload_sha256"] = _digest(report_manifest_base)
    report_manifest_sha = sha256(_payload_bytes(report_manifest)).hexdigest()

    lineage = {
        "schema_version": "technical-shadow-persistent-lineage.v1",
        "strategy_id": STRATEGY_ID, "shadow_account_id": SHADOW_ACCOUNT_ID,
        "mode": MODE, "state_date": strategy_date.isoformat(),
        **predecessor,
        "safety": DAILY_SAFETY,
    }
    persistent_payloads: dict[str, Any] = {
        "state.json": state,
        "next_session_plan.json": plan,
        "prior_plan_application.json": application,
        "lineage.json": lineage,
    }
    persistent_artifacts = {
        name: sha256(_payload_bytes(value)).hexdigest()
        for name, value in sorted(persistent_payloads.items())
    }
    persistent_manifest_base = {
        "schema_version": "technical-shadow-persistent-state-manifest.v1",
        "strategy_id": STRATEGY_ID, "shadow_account_id": SHADOW_ACCOUNT_ID,
        "mode": MODE, "state_date": strategy_date.isoformat(),
        "execution_date": execution_evidence.execution_date.isoformat(),
        "generated_at": generated_at.isoformat(),
        "execution_open_at": execution_open_at.isoformat(),
        "execution_window_status": "OPEN",
        "config_sha256": _digest(config),
        "account_record_sha256": state["record_sha256"],
        "plan_payload_sha256": plan["plan_payload_sha256"],
        "previous_trading_date": state["previous_trading_date"],
        "previous_record_sha256": state["previous_record_sha256"],
        "lineage_kind": "previous_persistent_state",
        "previous_manifest_sha256": predecessor["previous_manifest_sha256"],
        "artifacts": persistent_artifacts,
        "report_manifest_kind": "technical-shadow-daily-report-manifest.v1",
        "report_manifest_sha256": report_manifest_sha,
        "provider": data_receipt["provider"],
        "historical_pit_csi800": False,
        "automatic_order_submission": False, "safety": DAILY_SAFETY,
    }
    persistent_manifest = dict(persistent_manifest_base)
    persistent_manifest["manifest_payload_sha256"] = _digest(
        persistent_manifest_base
    )
    persistent_slot = state_root / strategy_date.isoformat()
    report_slot = report_root / strategy_date.isoformat()
    manifest_sha, idempotent = _publish_split_create_only(
        persistent_root=persistent_slot, report_root=report_slot,
        persistent_payloads=persistent_payloads,
        persistent_manifest=persistent_manifest,
        report_payloads=report_payloads, report_manifest=report_manifest,
    )
    result = {
        "status": "idempotent_existing" if idempotent else "created",
        "strategy_date": strategy_date.isoformat(),
        "execution_date": execution_evidence.execution_date.isoformat(),
        "exposure_state": final_state,
        "action_counts": action_counts,
        "current_cash": state["cash"], "current_positions": state["positions"],
        "current_nav": state["nav"], "plan_cost_summary": cost_summary,
        "execution_window_status": "OPEN",
        "generated_at": generated_at.isoformat(),
        "persistent_state_directory": str(persistent_slot.resolve()),
        "report_directory": str(report_slot.resolve()),
        "manifest_sha256": manifest_sha,
        "idempotent": idempotent,
        "automatic_order_submission": False,
    }
    return persistent_slot, result


def _head_idempotent_result(
    *, state_root: Path, config_sha256: str,
    readiness: ReadinessResult | None = None,
) -> dict[str, Any]:
    state_date, slot, manifest, manifest_sha = _verified_state_chain(
        state_root, expected_config_sha256=config_sha256
    )[-1]
    state = _json(slot / "state.json")
    plan = _json(slot / "next_session_plan.json")
    action_counts = {
        action: sum(row.get("action") == action for row in plan.get("actions", []))
        for action in ("BUY", "SELL", "HOLD", "CASH")
    }
    return {
        "status": "idempotent_existing",
        "readiness_status": "ALREADY_PROCESSED",
        "state_date": state_date.isoformat(),
        "strategy_date": state_date.isoformat(),
        "execution_date": plan["execution_date"],
        "execution_window_status": plan["execution_window_status"],
        "exposure_state": state["exposure_state"],
        "action_counts": action_counts,
        "current_cash": state["cash"],
        "current_positions": state["positions"],
        "current_nav": state["nav"],
        "plan_cost_summary": plan["cost_summary"],
        "persistent_state_directory": str(slot.resolve()),
        "manifest_sha256": manifest_sha,
        "generated_at": manifest.get("generated_at"),
        "readiness_checked_at": (
            readiness.checked_at.isoformat() if readiness is not None else None
        ),
        "idempotent": True,
        "automatic_order_submission": False,
    }


def _readiness_output(result: ReadinessResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "state_date": result.state_date.isoformat(),
        "latest_completed_trading_date": (
            result.latest_completed_trading_date.isoformat()
            if result.latest_completed_trading_date else None
        ),
        "latest_benchmark_date": (
            result.latest_benchmark_date.isoformat()
            if result.latest_benchmark_date else None
        ),
        "strategy_date": result.strategy_date.isoformat() if result.strategy_date else None,
        "execution_date": result.execution_date.isoformat() if result.execution_date else None,
        "checked_at": result.checked_at.isoformat(),
        "deadline_at": result.deadline_at.isoformat() if result.deadline_at else None,
        "reason_codes": list(result.reason_codes),
        "automatic_order_submission": False,
    }


def _parse_deadline(value: str | None, *, start: datetime) -> datetime:
    if value is None:
        return start + timedelta(minutes=DEFAULT_READY_WAIT_MINUTES)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise TechnicalShadowDailyError("deadline_timezone_required")
    parsed = parsed.astimezone(CHINA_TZ)
    if parsed <= start:
        raise TechnicalShadowDailyError("deadline_must_be_in_future")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    parser.add_argument("--report-root", type=Path, default=DEFAULT_REPORT_ROOT)
    parser.add_argument("--initialize-state-from", type=Path)
    parser.add_argument("--readiness-only", action="store_true")
    parser.add_argument("--when-ready", action="store_true")
    parser.add_argument("--deadline-at")
    parser.add_argument(
        "--poll-interval-seconds", type=int,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
    )
    parser.add_argument("--max-polls", type=int, default=DEFAULT_MAX_POLLS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = _load_config(args.config)
    if not _roots_are_separate(args.state_root, args.report_root):
        raise TechnicalShadowDailyError("persistent_and_report_roots_must_be_separate")
    if args.initialize_state_from is not None:
        _, result = initialize_persistent_state(
            source_slot=args.initialize_state_from,
            state_root=args.state_root,
            config=config,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0

    config_sha256 = _digest(config)
    head_slots = _verified_state_chain(
        args.state_root, expected_config_sha256=config_sha256
    )
    state_date, state_slot, _state_manifest, _state_manifest_sha = head_slots[-1]
    state = _json(state_slot / "state.json")
    head_plan = _json(state_slot / "next_session_plan.json")
    allow_flat_cash_gap = (
        state.get("positions") == {}
        and head_plan.get("plan_status") == "NO_ACTION_CASH"
        and head_plan.get("target_positions") == {}
        and not any(
            row.get("action") in {"BUY", "SELL"}
            for row in head_plan.get("actions", [])
        )
    )
    start = datetime.now(CHINA_TZ)

    def readiness_check() -> ReadinessResult:
        return check_baostock_readiness(
            state_date=state_date,
            state_record_sha256=state["record_sha256"],
            benchmark_id=str(config["data"]["benchmark_id"]),
            now=datetime.now(CHINA_TZ),
            allow_flat_cash_gap=allow_flat_cash_gap,
        )

    if args.when_ready:
        deadline = _parse_deadline(args.deadline_at, start=start)
        readiness = wait_until_ready(
            check=readiness_check, deadline=deadline,
            poll_interval_seconds=args.poll_interval_seconds,
            max_polls=args.max_polls,
        )
    else:
        if args.deadline_at is not None:
            raise TechnicalShadowDailyError("deadline_requires_when_ready")
        readiness = readiness_check()

    if args.readiness_only:
        print(json.dumps(_readiness_output(readiness), ensure_ascii=False, sort_keys=True))
        return 0

    if readiness.status == "ALREADY_PROCESSED":
        print(json.dumps(
            _head_idempotent_result(
                state_root=args.state_root, config_sha256=config_sha256,
                readiness=readiness,
            ),
            ensure_ascii=False, sort_keys=True,
        ))
        return 0
    if readiness.status != "DATA_READY":
        print(json.dumps(_readiness_output(readiness), ensure_ascii=False, sort_keys=True))
        return 0
    if readiness.strategy_date is None or readiness.execution_date is None:
        raise TechnicalShadowDailyError("data_ready_dates_missing")

    source = BaoStockTechnicalShadowSource()
    captured = source.capture(
        instrument_ids=config["universe"]["instrument_ids"],
        benchmark_id=config["data"]["benchmark_id"],
        recent_completed_sessions=1,
        lookback_days=int(config["data"]["calendar_lookback_days"]),
        now=datetime.now(CHINA_TZ),
        completed_through=readiness.strategy_date,
    )
    captured = _latest_data_complete_capture(captured)
    if captured.sessions[-1] != readiness.strategy_date:
        raise TechnicalShadowDailyError("full_capture_did_not_end_at_ready_strategy_date")
    current_head = _verified_state_chain(
        args.state_root, expected_config_sha256=config_sha256
    )[-1]
    current_state = _json(current_head[1] / "state.json")
    if (
        current_head[0] != state_date
        or current_state["record_sha256"] != state["record_sha256"]
    ):
        if current_head[0] == readiness.strategy_date:
            print(json.dumps(
                _head_idempotent_result(
                    state_root=args.state_root,
                    config_sha256=config_sha256,
                ),
                ensure_ascii=False, sort_keys=True,
            ))
            return 0
        raise TechnicalShadowDailyError("persistent_head_changed_during_capture")
    generated_at = datetime.now(CHINA_TZ)
    execution_open_at = datetime.combine(
        readiness.execution_date, EXECUTION_OPEN, CHINA_TZ
    )
    if generated_at >= execution_open_at:
        raise TechnicalShadowDailyError("execution_window_missed_during_full_capture")
    evidence = NextSessionEvidence(
        execution_date=readiness.execution_date,
        receipt=readiness.receipt,
        execution_window_status="OPEN",
    )
    _, result = run_daily(
        config=config, captured=captured, execution_evidence=evidence,
        state_root=args.state_root, report_root=args.report_root,
        generated_at=generated_at, readiness_receipt=readiness.receipt,
    )
    result["readiness_status"] = readiness.status
    result["readiness_checked_at"] = readiness.checked_at.isoformat()
    result["deadline_at"] = (
        readiness.deadline_at.isoformat() if readiness.deadline_at else None
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "NextSessionEvidence", "ReadinessResult", "TechnicalShadowDailyError",
    "check_baostock_readiness", "initialize_persistent_state", "run_daily",
    "wait_until_ready", "query_next_baostock_session",
]
