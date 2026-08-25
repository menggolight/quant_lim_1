"""Run one bounded Tushare SDK-versus-HTTP diagnostic.

The default mode is an offline plan.  ``--live`` is the only path that reads
``TUSHARE_TOKEN`` or imports the Tushare SDK.  This companion runner is kept
outside the sealed 22-endpoint capability-probe bundle so historical receipts
remain replayable against their original implementation hash.
"""

from __future__ import annotations

import argparse
import importlib
import os
import platform
import re
import socket
import ssl
import sys
import threading
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from agent.tushare_capability_probe import (
    GitMetadata,
    TushareCapabilityProbeError,
    _call_with_suppressed_sdk_output,
    _default_git_metadata,
    _default_sdk_loader,
    _directory_identity,
    _endpoint_name,
    _guard_bytes,
    _new_run_id,
    _parameter_sets,
    _path_inside_repository,
    _read_tushare_token,
    _require_directory_identity,
    _require_no_reparse_ancestors,
    _require_tushare_probe_policy,
    _safe_run_id,
    _sdk_method_name,
    _write_create_only,
)
from research.market_data.provider_access import (
    DEFAULT_PROVIDER_ACCESS_POLICY_PATH,
    load_provider_access_policy,
)
from research.market_data.tushare_capability import (
    ProbeConfig,
    canonical_json_bytes,
    load_probe_config,
    sha256_bytes,
    strict_json_loads,
)
from research.market_data.tushare_diagnostic import (
    DiagnosticChannelResultV1,
    TushareDiagnosticError,
    build_diagnostic_receipt,
    classify_message_category,
    normalize_upstream_code,
    safe_exception_type,
    verify_diagnostic_receipt,
)
from research.market_data.tushare_diagnostic_postmortem import (
    build_diagnostic_postmortem_receipt,
    verify_diagnostic_postmortem_receipt,
)


DIAGNOSTIC_VERSION = "tushare-single-endpoint-diagnostic-v1"
PLAN_SCHEMA_VERSION = "tushare-single-endpoint-diagnostic-plan-v1"
IMPLEMENTATION_BUNDLE_VERSION = "tushare-single-endpoint-diagnostic-bundle-v1"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPOSITORY_ROOT / "configs" / "tushare_capability_probe.v1.json"
DEFAULT_OUTPUT_ROOT = (
    REPOSITORY_ROOT / "data" / "tmp" / "tushare-capability" / "diagnostics"
)
OFFICIAL_HTTPS_API_URL = "https://api.tushare.pro"
SUPPORTED_ENDPOINTS = ("trade_cal", "daily")
MAXIMUM_SESSION_REQUEST_BUDGET = 4
PLANNED_SINGLE_ENDPOINT_REQUESTS = 2
MAXIMUM_RESPONSE_BYTES = 1_048_576
REQUEST_TIMEOUT_SECONDS = 30
_RUNTIME_CREDENTIAL_ENVELOPE = re.compile(r"[A-Za-z0-9_-]{20,256}")

_DIAGNOSTIC_IMPLEMENTATION_BUNDLE_PATHS = (
    "agent/tushare_single_endpoint_diagnostic.py",
    "research/market_data/tushare_diagnostic.py",
    "research/market_data/tushare_diagnostic_postmortem.py",
    "research/market_data/tushare_capability.py",
    "research/market_data/provider_access.py",
    "research/market_data/providers/base.py",
    "research/market_data/validation.py",
    "schemas/tushare_single_endpoint_diagnostic_receipt.v1.json",
    "schemas/tushare_single_endpoint_diagnostic_postmortem.v1.json",
    "schemas/tushare_single_endpoint_diagnostic_postmortem.v2.json",
    "schemas/tushare_single_endpoint_diagnostic_postmortem.v3.json",
    "schemas/provider_access_policy.v1.json",
    "configs/tushare_capability_probe.v1.json",
    "configs/provider_access.v1.json",
)
_SDK_TRANSPORT_LOCK = threading.RLock()
_SOCKET_NETWORK_GATE_LOCK = threading.RLock()
_BUDGET_SLOT_SCHEMA_VERSION = "tushare-diagnostic-round-budget-slot-v1"
_ROUND_FAILURE_MARKER_SCHEMA_VERSION = "tushare-diagnostic-round-failure-v1"
_ROUND_FAILURE_MARKER_NAME = ".p0-round-failure.json"
_RUNNER_FAILURE_WINDOW = "after_budget_reservation_before_receipt_publish"
_ROUND_EXECUTION_LOCK_NAME = ".p0-round-execution.lock"


class TushareSingleEndpointDiagnosticError(RuntimeError):
    """Raised when the diagnostic boundary cannot produce trusted evidence."""


class _RequestBudgetExceeded(TushareSingleEndpointDiagnosticError):
    """Raised before a second outbound request can leave one channel."""


class _ProtocolViolation(TushareSingleEndpointDiagnosticError):
    """Raised for an unsafe or malformed response without retaining its body."""


@contextmanager
def _exclusive_round_execution(
    budget_root: Path | str,
) -> Iterator[Path]:
    """Serialize the whole live round state transition across processes.

    The lock is held from before reservation through either receipt publication
    or failure-marker publication.  A crashed process releases the OS lock;
    its incomplete slot still cannot authorize ``daily`` without a verified
    terminal ``trade_cal`` receipt.
    """

    root = _path_inside_repository(
        budget_root,
        repository_root=REPOSITORY_ROOT,
        label="diagnostic round lock root",
    )
    root.mkdir(parents=True, exist_ok=True)
    root = _path_inside_repository(
        root,
        repository_root=REPOSITORY_ROOT,
        label="diagnostic round lock root",
    )
    _require_no_reparse_ancestors(root, "diagnostic round lock root")
    identity = _directory_identity(root)
    lock_path = root / _ROUND_EXECUTION_LOCK_NAME
    _require_no_reparse_ancestors(lock_path, "diagnostic round lock")
    handle = lock_path.open("a+b")
    acquired = False
    unlock: Callable[[], None] | None = None
    try:
        _require_directory_identity(root, identity)
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise TushareSingleEndpointDiagnosticError(
                    "another diagnostic process owns the P0 round"
                ) from exc

            def unlock() -> None:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise TushareSingleEndpointDiagnosticError(
                    "another diagnostic process owns the P0 round"
                ) from exc

            def unlock() -> None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

        acquired = True
        _require_directory_identity(root, identity)
        yield root
    finally:
        if acquired and unlock is not None:
            try:
                unlock()
            finally:
                handle.close()
        else:
            handle.close()


class _SocketNetworkGate:
    """Deny SDK-side sockets except inside one counted HTTP send.

    The installed SDK is imported and initialized while this gate is closed.
    This prevents a changed dependency from opening an uncounted connection
    before the probe-only requests proxy is installed.  The gate is process
    scoped and serialized because it temporarily wraps Python's socket entry
    points; the requests call is synchronous on the same thread.
    """

    def __init__(self) -> None:
        self._local = threading.local()
        self._module_originals: dict[str, Any] = {}
        self._class_originals: dict[str, Any] = {}
        self._entered = False

    def _guard(self, original: Callable[..., Any]) -> Callable[..., Any]:
        def guarded(*args: Any, **kwargs: Any) -> Any:
            if not bool(getattr(self._local, "outbound_allowed", False)):
                raise _ProtocolViolation(
                    "network access is blocked outside a counted diagnostic send"
                )
            return original(*args, **kwargs)

        return guarded

    def __enter__(self) -> "_SocketNetworkGate":
        _SOCKET_NETWORK_GATE_LOCK.acquire()
        try:
            for name in (
                "create_connection",
                "create_server",
                "getaddrinfo",
                "gethostbyname",
                "gethostbyname_ex",
                "gethostbyaddr",
            ):
                original = getattr(socket, name, None)
                if callable(original):
                    self._module_originals[name] = original
                    setattr(socket, name, self._guard(original))
            for name in ("connect", "connect_ex", "sendto", "sendmsg"):
                original = getattr(socket.socket, name, None)
                if callable(original):
                    self._class_originals[name] = original
                    setattr(socket.socket, name, self._guard(original))
            self._entered = True
            return self
        except BaseException:
            self._restore()
            _SOCKET_NETWORK_GATE_LOCK.release()
            raise

    def _restore(self) -> None:
        for name, original in self._class_originals.items():
            setattr(socket.socket, name, original)
        for name, original in self._module_originals.items():
            setattr(socket, name, original)
        self._class_originals.clear()
        self._module_originals.clear()
        self._entered = False

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            self._restore()
        finally:
            _SOCKET_NETWORK_GATE_LOCK.release()

    @contextmanager
    def allow_one_counted_send(self) -> Iterator[None]:
        if not self._entered:
            raise TushareSingleEndpointDiagnosticError(
                "network gate is not installed"
            )
        previous = bool(getattr(self._local, "outbound_allowed", False))
        if previous:
            raise _ProtocolViolation("nested outbound network allowance is forbidden")
        self._local.outbound_allowed = True
        try:
            yield
        finally:
            self._local.outbound_allowed = previous


@dataclass
class _WireObservation:
    diagnostic_attempted: bool = False
    request_count: int = 0
    requested_at: datetime | None = None
    completed_at: datetime | None = None
    transport_status: str = "not_attempted"
    http_status: int | None = None
    upstream_code: int | None = None
    message: str | None = None
    error: BaseException | None = None
    row_count: int = 0
    field_names: tuple[str, ...] = ()
    data_valid: bool = False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_version(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    if 1 <= len(text) <= 64 and all(
        character.isascii() and (character.isalnum() or character in ".+_-")
        for character in text
    ):
        return text
    return fallback


def _credential_passes_local_preflight(value: Any) -> bool:
    """Reject obviously malformed clipboard input before budget or network use.

    This is deliberately only a local safety envelope, not a claim that the
    credential is authentic or entitled to any Tushare API.  No derived token
    material is returned or persisted.
    """

    return (
        type(value) is str
        and value == value.strip()
        and _RUNTIME_CREDENTIAL_ENVELOPE.fullmatch(value) is not None
    )


def compute_diagnostic_implementation_bundle_sha256(
    repository_root: Path | str = REPOSITORY_ROOT,
) -> str:
    """Hash only this diagnostic's fixed code, policy, config and Schema bundle."""

    root = Path(repository_root).resolve()
    files: list[dict[str, str]] = []
    for relative in _DIAGNOSTIC_IMPLEMENTATION_BUNDLE_PATHS:
        path = root / relative
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise TushareSingleEndpointDiagnosticError(
                "diagnostic implementation bundle is incomplete"
            ) from exc
        files.append({"path": relative, "sha256": sha256_bytes(raw)})
    return sha256_bytes(
        canonical_json_bytes(
            {
                "bundle_version": IMPLEMENTATION_BUNDLE_VERSION,
                "files": files,
            }
        )
    )


def _budget_slot_payload(
    *,
    slot: int,
    endpoint: str,
    run_id: str,
    reserved_at: datetime,
) -> dict[str, Any]:
    return {
        "schema_version": _BUDGET_SLOT_SCHEMA_VERSION,
        "slot": slot,
        "endpoint": endpoint,
        "diagnostic_run_id": run_id,
        "reserved_at": reserved_at.isoformat(),
        "reserved_request_count": PLANNED_SINGLE_ENDPOINT_REQUESTS,
        "maximum_round_request_count": MAXIMUM_SESSION_REQUEST_BUDGET,
    }


def _read_budget_slot(path: Path, expected_slot: int) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        value = strict_json_loads(
            raw,
            label="Tushare diagnostic budget slot",
            require_canonical=True,
        )
    except Exception as exc:
        raise TushareSingleEndpointDiagnosticError(
            "diagnostic budget slot is unavailable or invalid"
        ) from exc
    if (
        not isinstance(value, Mapping)
        or set(value)
        != {
            "schema_version",
            "slot",
            "endpoint",
            "diagnostic_run_id",
            "reserved_at",
            "reserved_request_count",
            "maximum_round_request_count",
        }
        or value.get("schema_version") != _BUDGET_SLOT_SCHEMA_VERSION
        or type(value.get("slot")) is not int
        or value.get("slot") != expected_slot
        or value.get("endpoint") not in SUPPORTED_ENDPOINTS
        or type(value.get("diagnostic_run_id")) is not str
        or _safe_run_id(value["diagnostic_run_id"]) != value["diagnostic_run_id"]
        or type(value.get("reserved_at")) is not str
        or value.get("reserved_request_count") != PLANNED_SINGLE_ENDPOINT_REQUESTS
        or value.get("maximum_round_request_count")
        != MAXIMUM_SESSION_REQUEST_BUDGET
    ):
        raise TushareSingleEndpointDiagnosticError(
            "diagnostic budget slot differs from its frozen contract"
        )
    try:
        reserved_at = datetime.fromisoformat(value["reserved_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise TushareSingleEndpointDiagnosticError(
            "diagnostic budget timestamp is invalid"
        ) from exc
    if reserved_at.tzinfo is None:
        raise TushareSingleEndpointDiagnosticError(
            "diagnostic budget timestamp lacks a UTC offset"
        )
    return dict(value)


def _read_round_failure_marker(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        value = strict_json_loads(
            raw,
            label="Tushare diagnostic round failure marker",
            require_canonical=True,
        )
    except Exception as exc:
        raise TushareSingleEndpointDiagnosticError(
            "diagnostic round failure marker is unavailable or invalid"
        ) from exc
    expected = {
        "schema_version",
        "round_status",
        "evidence_origin",
        "diagnostic_run_id",
        "endpoint",
        "recorded_at",
        "runner_exception_type",
        "failure_window",
        "budget_slot_sha256",
        "failed_diagnostic_code_sha256",
        "maximum_round_request_count",
        "rerun_permitted",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != expected
        or value.get("schema_version") != _ROUND_FAILURE_MARKER_SCHEMA_VERSION
        or value.get("round_status") != "closed_after_runner_failure"
        or value.get("evidence_origin")
        not in {"runner_exception_boundary", "posthoc_observed_cli_failure"}
        or type(value.get("diagnostic_run_id")) is not str
        or _safe_run_id(value["diagnostic_run_id"]) != value["diagnostic_run_id"]
        or value.get("endpoint") not in SUPPORTED_ENDPOINTS
        or value.get("runner_exception_type") != "OtherError"
        or value.get("failure_window") != _RUNNER_FAILURE_WINDOW
        or type(value.get("budget_slot_sha256")) is not str
        or re.fullmatch(r"[0-9a-f]{64}", value["budget_slot_sha256"]) is None
        or type(value.get("failed_diagnostic_code_sha256")) is not str
        or re.fullmatch(r"[0-9a-f]{64}", value["failed_diagnostic_code_sha256"])
        is None
        or value.get("maximum_round_request_count")
        != MAXIMUM_SESSION_REQUEST_BUDGET
        or value.get("rerun_permitted") is not False
    ):
        raise TushareSingleEndpointDiagnosticError(
            "diagnostic round failure marker differs from its frozen contract"
        )
    try:
        failed_at = datetime.fromisoformat(value["recorded_at"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise TushareSingleEndpointDiagnosticError(
            "diagnostic round failure timestamp is invalid"
        ) from exc
    if failed_at.tzinfo is None:
        raise TushareSingleEndpointDiagnosticError(
            "diagnostic round failure timestamp lacks a UTC offset"
        )
    return dict(value)


def _publish_round_failure_marker(
    *,
    budget_slot_path: Path,
    endpoint: str,
    run_id: str,
    failed_at: datetime,
    runner_exception_type: str,
    failed_diagnostic_code_sha256: str,
    evidence_origin: str = "runner_exception_boundary",
) -> Path:
    """Close the P0 round after a reserved runner fails, without credentials."""

    if failed_at.tzinfo is None:
        raise TushareSingleEndpointDiagnosticError(
            "diagnostic round failure clock must be timezone-aware"
        )
    root = _path_inside_repository(
        budget_slot_path.parent,
        repository_root=REPOSITORY_ROOT,
        label="diagnostic round failure root",
    )
    _require_no_reparse_ancestors(root, "diagnostic round failure root")
    identity = _directory_identity(root)
    _require_no_reparse_ancestors(
        budget_slot_path, "diagnostic round failure budget slot"
    )
    slot_number = 1 if endpoint == "trade_cal" else 2
    slot = _read_budget_slot(budget_slot_path, slot_number)
    slot_bytes = budget_slot_path.read_bytes()
    if slot["diagnostic_run_id"] != run_id or slot["endpoint"] != endpoint:
        raise TushareSingleEndpointDiagnosticError(
            "runner failure differs from its reserved budget slot"
        )
    marker = {
        "schema_version": _ROUND_FAILURE_MARKER_SCHEMA_VERSION,
        "round_status": "closed_after_runner_failure",
        "evidence_origin": evidence_origin,
        "diagnostic_run_id": run_id,
        "endpoint": endpoint,
        "recorded_at": failed_at.isoformat(),
        "runner_exception_type": runner_exception_type,
        "failure_window": _RUNNER_FAILURE_WINDOW,
        "budget_slot_sha256": sha256_bytes(slot_bytes),
        "failed_diagnostic_code_sha256": failed_diagnostic_code_sha256,
        "maximum_round_request_count": MAXIMUM_SESSION_REQUEST_BUDGET,
        "rerun_permitted": False,
    }
    content = canonical_json_bytes(marker)
    target = root / _ROUND_FAILURE_MARKER_NAME
    _write_create_only(
        target,
        content,
        controlled_root=root,
        controlled_identity=identity,
    )
    _require_directory_identity(root, identity)
    if _read_round_failure_marker(target) != marker:
        raise TushareSingleEndpointDiagnosticError(
            "diagnostic round failure marker replay mismatch"
        )
    return target


def _reserve_round_budget(
    *,
    budget_root: Path | str,
    endpoint: str,
    run_id: str,
    reserved_at: datetime,
) -> Path:
    """Reserve the only permitted trade_cal slot, or optional daily slot.

    Each slot conservatively consumes two requests even if a channel fails
    before sending.  Slots are create-only and never released automatically,
    so repeated or concurrent CLI invocations cannot exceed four requests in
    this P0 diagnostic round.
    """

    root = _path_inside_repository(
        budget_root,
        repository_root=REPOSITORY_ROOT,
        label="diagnostic budget_root",
    )
    root.mkdir(parents=True, exist_ok=True)
    root = _path_inside_repository(
        root,
        repository_root=REPOSITORY_ROOT,
        label="diagnostic budget_root",
    )
    _require_no_reparse_ancestors(root, "diagnostic budget_root")
    identity = _directory_identity(root)
    first = root / ".p0-round-budget-slot-1.json"
    second = root / ".p0-round-budget-slot-2.json"
    if (root / _ROUND_FAILURE_MARKER_NAME).exists():
        raise TushareSingleEndpointDiagnosticError(
            "diagnostic round is closed after a runner failure"
        )

    if endpoint == "trade_cal":
        if first.exists() or second.exists():
            raise TushareSingleEndpointDiagnosticError(
                "trade_cal diagnostic has already consumed its round budget slot"
            )
        slot = 1
        target = first
    elif endpoint == "daily":
        if not first.is_file():
            raise TushareSingleEndpointDiagnosticError(
                "daily diagnostic requires a prior trade_cal budget slot"
            )
        first_value = _read_budget_slot(first, 1)
        if first_value["endpoint"] != "trade_cal" or second.exists():
            raise TushareSingleEndpointDiagnosticError(
                "daily diagnostic round budget is unavailable"
            )
        trade_cal_receipt_path = (
            root
            / _safe_run_id(first_value["diagnostic_run_id"])
            / "diagnostic_receipt.json"
        )
        try:
            _require_no_reparse_ancestors(
                trade_cal_receipt_path,
                "trade_cal terminal diagnostic receipt",
            )
            trade_cal_receipt = verify_diagnostic_receipt(
                trade_cal_receipt_path.read_bytes()
            )
        except Exception as exc:
            raise TushareSingleEndpointDiagnosticError(
                "daily diagnostic requires a verified terminal trade_cal receipt"
            ) from exc
        if (
            trade_cal_receipt.status != "completed"
            or trade_cal_receipt.endpoint != "trade_cal"
            or trade_cal_receipt.diagnostic_run_id
            != first_value["diagnostic_run_id"]
        ):
            raise TushareSingleEndpointDiagnosticError(
                "daily diagnostic trade_cal prerequisite is not terminal"
            )
        slot = 2
        target = second
    else:
        raise TushareSingleEndpointDiagnosticError(
            "endpoint is outside the diagnostic budget allowlist"
        )

    content = canonical_json_bytes(
        _budget_slot_payload(
            slot=slot,
            endpoint=endpoint,
            run_id=run_id,
            reserved_at=reserved_at,
        )
    )
    try:
        _write_create_only(
            target,
            content,
            controlled_root=root,
            controlled_identity=identity,
        )
    except TushareCapabilityProbeError as exc:
        raise TushareSingleEndpointDiagnosticError(
            "diagnostic round budget could not be reserved"
        ) from exc
    _require_directory_identity(root, identity)
    if _read_budget_slot(target, slot)["diagnostic_run_id"] != run_id:
        raise TushareSingleEndpointDiagnosticError(
            "diagnostic round budget reservation identity mismatch"
        )
    if (root / _ROUND_FAILURE_MARKER_NAME).exists():
        raise TushareSingleEndpointDiagnosticError(
            "diagnostic round closed while its budget slot was reserved"
        )
    return target


def _resolve_repository_file(path: Path | str, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = REPOSITORY_ROOT / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(REPOSITORY_ROOT.resolve())
    except ValueError as exc:
        raise TushareSingleEndpointDiagnosticError(
            f"{label} must stay inside the repository"
        ) from exc
    if not resolved.is_file():
        raise TushareSingleEndpointDiagnosticError(f"{label} is unavailable")
    return resolved


def _selected_spec(
    config: ProbeConfig,
    endpoint: str,
) -> tuple[Any, Mapping[str, str], tuple[str, ...]]:
    if endpoint not in SUPPORTED_ENDPOINTS:
        raise TushareSingleEndpointDiagnosticError(
            "endpoint is outside the diagnostic allowlist"
        )
    spec = config.spec_for(endpoint)
    if _endpoint_name(spec) != endpoint or _sdk_method_name(spec) != endpoint:
        raise TushareSingleEndpointDiagnosticError(
            "endpoint differs from its fixed read-only SDK method"
        )
    parameter_sets = _parameter_sets(spec)
    if int(spec.max_calls) != 1 or len(parameter_sets) != 1:
        raise TushareSingleEndpointDiagnosticError(
            "single-endpoint diagnostic requires exactly one frozen parameter set"
        )
    expected_fields = tuple(str(field) for field in spec.required_fields)
    return spec, dict(parameter_sets[0]), expected_fields


def build_diagnostic_plan(
    *,
    endpoint: str,
    config_path: Path | str = DEFAULT_CONFIG_PATH,
    access_policy_path: Path | str = DEFAULT_PROVIDER_ACCESS_POLICY_PATH,
) -> dict[str, Any]:
    """Build an offline plan without reading credentials or importing the SDK."""

    resolved_config = _resolve_repository_file(config_path, "config")
    resolved_policy = _resolve_repository_file(access_policy_path, "provider policy")
    config = load_probe_config(resolved_config)
    policy = load_provider_access_policy(resolved_policy)
    _require_tushare_probe_policy(policy)
    _, parameters, expected_fields = _selected_spec(config, endpoint)
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "diagnostic_version": DIAGNOSTIC_VERSION,
        "mode": "plan_offline_no_credentials",
        "scope": "single_endpoint_transport_diagnostic_only_not_admitted",
        "provider_id": "tushare",
        "endpoint": endpoint,
        "semantic_parameters": dict(parameters),
        "same_semantic_parameters": True,
        "expected_fields": list(expected_fields),
        "channels": [
            {"channel": "sdk", "transport_target": OFFICIAL_HTTPS_API_URL},
            {"channel": "http", "transport_target": OFFICIAL_HTTPS_API_URL},
        ],
        "planned_request_count": PLANNED_SINGLE_ENDPOINT_REQUESTS,
        "maximum_session_request_budget": MAXIMUM_SESSION_REQUEST_BUDGET,
        "round_budget_reservation_enforced": True,
        "permitted_round_sequence": ["trade_cal", "daily_optional"],
        "redirects_allowed": False,
        "automatic_retries_allowed": False,
        "credential_accessed": False,
        "sdk_imported": False,
        "network_accessed": False,
        "raw_response_persisted": False,
        "formal_data_admission": False,
        "experiment_v3_impact": "none",
        "daily_signal_authority": "none",
        "paper_eligibility": False,
        "trade_eligibility": False,
        "live_supported": False,
    }


def _exception_chain(error: BaseException) -> tuple[BaseException, ...]:
    values: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in values and len(values) < 8:
        values.append(current)
        current = current.__cause__ or current.__context__
    return tuple(values)


def _transport_status(error: BaseException) -> str:
    chain = _exception_chain(error)
    if any(isinstance(item, socket.gaierror) for item in chain):
        return "dns_failure"
    if any(isinstance(item, ssl.SSLError) for item in chain):
        return "tls_failure"
    if any(isinstance(item, (TimeoutError, socket.timeout)) for item in chain):
        return "timeout"
    names = {type(item).__name__ for item in chain}
    if names & {"Timeout", "ConnectTimeout", "ReadTimeout"}:
        return "timeout"
    if names & {"SSLError"}:
        return "tls_failure"
    if names & {"ConnectionError", "ProxyError", "NewConnectionError"}:
        return "connection_failure"
    if isinstance(error, (_ProtocolViolation, _RequestBudgetExceeded)):
        return "protocol_failure"
    return "unknown_failure"


def _begin_diagnostic(observation: _WireObservation, clock: Callable[[], datetime]) -> None:
    observation.diagnostic_attempted = True
    observation.requested_at = clock()
    if observation.requested_at.tzinfo is None:
        raise TushareSingleEndpointDiagnosticError(
            "diagnostic clock must return timezone-aware values"
        )


def _complete_diagnostic(
    observation: _WireObservation,
    clock: Callable[[], datetime],
) -> None:
    observation.completed_at = clock()
    if observation.completed_at.tzinfo is None:
        raise TushareSingleEndpointDiagnosticError(
            "diagnostic clock must return timezone-aware values"
        )


def _new_no_retry_session() -> Any:
    requests = importlib.import_module("requests")
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(max_retries=0)
    session.mount("https://", adapter)
    session.max_redirects = 0
    return session


def _read_bounded_response(response: Any) -> bytes:
    chunks: list[bytes] = []
    total = 0
    iterator = getattr(response, "iter_content", None)
    if not callable(iterator):
        raise _ProtocolViolation("response does not support bounded streaming")
    for chunk in iterator(chunk_size=65_536):
        if not isinstance(chunk, (bytes, bytearray)):
            raise _ProtocolViolation("response emitted a non-byte chunk")
        if not chunk:
            continue
        total += len(chunk)
        if total > MAXIMUM_RESPONSE_BYTES:
            close = getattr(response, "close", None)
            if callable(close):
                close()
            raise _ProtocolViolation("response exceeded the diagnostic size cap")
        chunks.append(bytes(chunk))
    content = b"".join(chunks)
    # The SDK consumes Response.text after the proxy returns.  Populate the
    # already bounded body without retaining any additional copy on disk.
    try:
        response._content = content
        response._content_consumed = True
    except Exception as exc:
        raise _ProtocolViolation("response cannot be safely replayed to the SDK") from exc
    return content


def _extract_envelope(
    content: bytes,
    *,
    expected_fields: Sequence[str],
) -> tuple[int | None, str | None, int, tuple[str, ...], bool]:
    """Extract bounded structural evidence; never return the raw payload."""

    if len(content) > MAXIMUM_RESPONSE_BYTES:
        raise _ProtocolViolation("response exceeded the diagnostic size cap")
    try:
        value = strict_json_loads(content, label="Tushare diagnostic response")
    except Exception as exc:
        raise _ProtocolViolation("response is not strict JSON") from exc
    if not isinstance(value, Mapping):
        raise _ProtocolViolation("response root is not an object")
    code = normalize_upstream_code(value.get("code"))
    message_value = value.get("msg")
    message = message_value if type(message_value) is str else None
    if code != 0:
        return code, message, 0, (), True

    data = value.get("data")
    if not isinstance(data, Mapping):
        return code, message, 0, (), False
    fields = data.get("fields")
    items = data.get("items")
    if (
        not isinstance(fields, list)
        or not isinstance(items, list)
        or len(fields) > 1_000
        or len(items) > 10_000
        or any(type(field) is not str for field in fields)
        or len(set(fields)) != len(fields)
        or any(
            not isinstance(item, list) or len(item) != len(fields)
            for item in items
        )
    ):
        return code, message, 0, (), False
    returned_fields = set(fields)
    if not set(expected_fields).issubset(returned_fields):
        return code, message, 0, (), False
    safe_fields = tuple(expected_fields)
    return code, message, len(items), safe_fields, bool(items and safe_fields)


def _capture_response(
    observation: _WireObservation,
    response: Any,
    *,
    expected_fields: Sequence[str],
) -> None:
    status = getattr(response, "status_code", None)
    if type(status) is not int or not 100 <= status <= 599:
        raise _ProtocolViolation("response status is invalid")
    observation.transport_status = "response_received"
    observation.http_status = status
    content = _read_bounded_response(response)
    code, message, row_count, field_names, data_valid = _extract_envelope(
        content,
        expected_fields=expected_fields,
    )
    observation.upstream_code = code
    observation.message = message
    observation.row_count = row_count
    observation.field_names = field_names
    observation.data_valid = data_valid


def _send_once(
    observation: _WireObservation,
    session: Any,
    *,
    payload: Mapping[str, Any],
    expected_fields: Sequence[str],
    network_gate: _SocketNetworkGate | None = None,
) -> Any:
    if observation.request_count != 0:
        raise _RequestBudgetExceeded("channel attempted more than one request")
    observation.request_count = 1
    try:
        allowance = (
            network_gate.allow_one_counted_send()
            if network_gate is not None
            else nullcontext()
        )
        with allowance:
            response = session.post(
                OFFICIAL_HTTPS_API_URL,
                json=dict(payload),
                timeout=REQUEST_TIMEOUT_SECONDS,
                allow_redirects=False,
                verify=True,
                stream=True,
            )
        _capture_response(
            observation,
            response,
            expected_fields=expected_fields,
        )
        return response
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        observation.error = exc
        if observation.transport_status == "not_attempted":
            observation.transport_status = _transport_status(exc)
        raise


def _payload(
    *,
    endpoint: str,
    token: str,
    parameters: Mapping[str, str],
) -> dict[str, Any]:
    return {
        "api_name": endpoint,
        "token": token,
        "params": dict(parameters),
        "fields": "",
    }


def _validate_sdk_payload(
    *,
    url: Any,
    supplied: Any,
    endpoint: str,
    token: str,
    parameters: Mapping[str, str],
) -> dict[str, Any]:
    expected_internal_url = f"{OFFICIAL_HTTPS_API_URL}/{endpoint}"
    if url != expected_internal_url or not isinstance(supplied, Mapping):
        raise _ProtocolViolation("SDK generated an unexpected request target")
    if set(supplied) != {"api_name", "token", "params", "fields"}:
        raise _ProtocolViolation("SDK generated an unexpected request shape")
    if (
        supplied.get("api_name") != endpoint
        or supplied.get("token") != token
        or supplied.get("fields") != ""
        or not isinstance(supplied.get("params"), Mapping)
    ):
        raise _ProtocolViolation("SDK request differs from the frozen call")
    sdk_parameters = dict(supplied["params"])
    if sdk_parameters.pop("ts_type_name", None) != OFFICIAL_HTTPS_API_URL:
        raise _ProtocolViolation("SDK private transport parameter is unexpected")
    if sdk_parameters != dict(parameters):
        raise _ProtocolViolation("SDK semantic parameters differ from HTTP")
    # The private SDK transport marker is intentionally removed before the
    # actual request.  Both live channels therefore send identical semantics.
    return _payload(endpoint=endpoint, token=token, parameters=parameters)


class _SdkRequestsProxy:
    def __init__(
        self,
        *,
        observation: _WireObservation,
        session: Any,
        endpoint: str,
        token: str,
        parameters: Mapping[str, str],
        expected_fields: Sequence[str],
        network_gate: _SocketNetworkGate,
    ) -> None:
        self._observation = observation
        self._session = session
        self._endpoint = endpoint
        self._token = token
        self._parameters = parameters
        self._expected_fields = expected_fields
        self._network_gate = network_gate

    def post(self, url: Any, **kwargs: Any) -> Any:
        if set(kwargs) != {"json", "timeout"}:
            raise _ProtocolViolation("SDK transport options differ from the frozen call")
        if kwargs.get("timeout") != REQUEST_TIMEOUT_SECONDS:
            raise _ProtocolViolation("SDK timeout differs from the frozen call")
        payload = _validate_sdk_payload(
            url=url,
            supplied=kwargs.get("json"),
            endpoint=self._endpoint,
            token=self._token,
            parameters=self._parameters,
        )
        return _send_once(
            self._observation,
            self._session,
            payload=payload,
            expected_fields=self._expected_fields,
            network_gate=self._network_gate,
        )


def _summarize_sdk_result(
    value: Any,
    *,
    expected_fields: Sequence[str],
) -> tuple[int, tuple[str, ...], bool]:
    shape = getattr(value, "shape", None)
    columns = getattr(value, "columns", None)
    if (
        not isinstance(shape, tuple)
        or len(shape) != 2
        or type(shape[0]) is not int
        or not 0 <= shape[0] <= 10_000
        or columns is None
    ):
        return 0, (), False
    try:
        returned = {str(column) for column in columns}
    except Exception:
        return 0, (), False
    if not set(expected_fields).issubset(returned):
        return 0, (), False
    safe_fields = tuple(expected_fields)
    return shape[0], safe_fields, bool(shape[0] and safe_fields)


def _channel_result(
    *,
    channel: str,
    endpoint: str,
    observation: _WireObservation,
    token: str,
    sdk_exception: BaseException | None,
) -> DiagnosticChannelResultV1:
    category = classify_message_category(
        upstream_code=observation.upstream_code,
        http_status=observation.http_status,
        message=observation.message,
        error=observation.error or sdk_exception,
        transport_status=observation.transport_status,
        channel=channel,
        secret=token,
    )
    response_is_2xx = (
        observation.http_status is not None
        and 200 <= observation.http_status <= 299
    )
    if observation.request_count == 0:
        outcome = "client_failed"
        category = "sdk_client" if channel == "sdk" else "unknown"
    elif observation.transport_status != "response_received":
        outcome = "transport_failed"
        category = "network_transport"
    elif (
        observation.upstream_code == 0
        and response_is_2xx
        and observation.data_valid
        and sdk_exception is None
    ):
        outcome = "passed"
        category = "success"
    elif observation.upstream_code not in {None, 0} or (
        not response_is_2xx and category != "success"
    ) or category in {
        "permission",
        "rate_limit",
        "authentication_account",
        "invalid_parameter",
        "server_internal",
    }:
        outcome = "upstream_rejected"
    else:
        outcome = "client_failed"
        category = "sdk_client" if channel == "sdk" else "unknown"

    row_count = observation.row_count if outcome == "passed" else 0
    field_names = observation.field_names if outcome == "passed" else ()
    return DiagnosticChannelResultV1(
        channel=channel,
        endpoint=endpoint,
        transport_target=OFFICIAL_HTTPS_API_URL,
        diagnostic_attempted=observation.diagnostic_attempted,
        request_count=observation.request_count,
        requested_at=observation.requested_at,
        completed_at=observation.completed_at,
        transport_status=observation.transport_status,
        http_status=observation.http_status,
        upstream_code=observation.upstream_code,
        sdk_exception_type=(
            safe_exception_type(sdk_exception or observation.error)
            if channel == "sdk"
            else None
        ),
        sanitized_message_category=category,
        outcome=outcome,
        row_count=row_count,
        field_names=field_names,
    )


def _default_sdk_client_module_loader() -> Any:
    return importlib.import_module("tushare.pro.client")


def _run_sdk_channel(
    *,
    endpoint: str,
    parameters: Mapping[str, str],
    expected_fields: Sequence[str],
    token: str,
    clock: Callable[[], datetime],
    sdk_loader: Callable[[], Any],
    sdk_client_module_loader: Callable[[], Any],
    session_factory: Callable[[], Any],
    network_gate: _SocketNetworkGate,
) -> tuple[DiagnosticChannelResultV1, str]:
    observation = _WireObservation()
    _begin_diagnostic(observation, clock)
    sdk_exception: BaseException | None = None
    sdk_version = "not_loaded"
    session: Any = None
    try:
        sdk, _ = _call_with_suppressed_sdk_output(
            sdk_loader,
            secret=token,
            operation="Tushare diagnostic SDK import",
        )
        sdk_version = _safe_version(getattr(sdk, "__version__", None), "unknown")
        client, _ = _call_with_suppressed_sdk_output(
            lambda: sdk.pro_api(token),
            secret=token,
            operation="Tushare diagnostic SDK initialization",
        )
        setattr(client, "_DataApi__http_url", OFFICIAL_HTTPS_API_URL)
        method = getattr(client, endpoint)
        client_module = sdk_client_module_loader()
        session = session_factory()
        proxy = _SdkRequestsProxy(
            observation=observation,
            session=session,
            endpoint=endpoint,
            token=token,
            parameters=parameters,
            expected_fields=expected_fields,
            network_gate=network_gate,
        )
        with _SDK_TRANSPORT_LOCK:
            original_requests = getattr(client_module, "requests")
            setattr(client_module, "requests", proxy)
            try:
                value, _ = _call_with_suppressed_sdk_output(
                    lambda: method(**dict(parameters)),
                    secret=token,
                    operation="Tushare diagnostic SDK call",
                )
            finally:
                setattr(client_module, "requests", original_requests)
        row_count, field_names, data_valid = _summarize_sdk_result(
            value,
            expected_fields=expected_fields,
        )
        if observation.upstream_code == 0:
            observation.row_count = row_count
            observation.field_names = field_names
            observation.data_valid = data_valid
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        sdk_exception = exc
        if observation.error is None:
            observation.error = exc
        if observation.request_count == 0:
            observation.transport_status = "not_attempted"
    finally:
        if session is not None:
            close = getattr(session, "close", None)
            if callable(close):
                close()
        _complete_diagnostic(observation, clock)
    return (
        _channel_result(
            channel="sdk",
            endpoint=endpoint,
            observation=observation,
            token=token,
            sdk_exception=sdk_exception,
        ),
        sdk_version,
    )


def _run_http_channel(
    *,
    endpoint: str,
    parameters: Mapping[str, str],
    expected_fields: Sequence[str],
    token: str,
    clock: Callable[[], datetime],
    session_factory: Callable[[], Any],
    network_gate: _SocketNetworkGate,
) -> DiagnosticChannelResultV1:
    observation = _WireObservation()
    _begin_diagnostic(observation, clock)
    session: Any = None
    try:
        session = session_factory()
        payload = _payload(endpoint=endpoint, token=token, parameters=parameters)
        try:
            _call_with_suppressed_sdk_output(
                lambda: _send_once(
                    observation,
                    session,
                    payload=payload,
                    expected_fields=expected_fields,
                    network_gate=network_gate,
                ),
                secret=token,
                operation="Tushare diagnostic direct HTTP call",
            )
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                raise
            if observation.error is None:
                observation.error = exc
            if observation.request_count == 0:
                observation.transport_status = "not_attempted"
    finally:
        if session is not None:
            close = getattr(session, "close", None)
            if callable(close):
                close()
        _complete_diagnostic(observation, clock)
    return _channel_result(
        channel="http",
        endpoint=endpoint,
        observation=observation,
        token=token,
        sdk_exception=None,
    )


def _publish_receipt(
    *,
    output_root: Path | str,
    run_id: str,
    receipt_bytes: bytes,
    token: str,
) -> Path:
    root = _path_inside_repository(
        output_root,
        repository_root=REPOSITORY_ROOT,
        label="diagnostic output_root",
    )
    root.mkdir(parents=True, exist_ok=True)
    root = _path_inside_repository(
        root,
        repository_root=REPOSITORY_ROOT,
        label="diagnostic output_root",
    )
    _require_no_reparse_ancestors(root, "diagnostic output_root")
    run_directory = root / _safe_run_id(run_id)
    try:
        run_directory.mkdir()
    except FileExistsError as exc:
        raise TushareSingleEndpointDiagnosticError(
            "refusing to overwrite an existing diagnostic run"
        ) from exc
    identity = _directory_identity(run_directory)
    target = run_directory / "diagnostic_receipt.json"
    _guard_bytes(receipt_bytes, token)
    _write_create_only(
        target,
        receipt_bytes,
        controlled_root=run_directory,
        controlled_identity=identity,
    )
    _require_directory_identity(run_directory, identity)
    persisted = target.read_bytes()
    _guard_bytes(persisted, token)
    verify_diagnostic_receipt(persisted)
    _require_directory_identity(run_directory, identity)
    return target


def _finalize_reserved_failure_postmortem_locked(
    *,
    endpoint: str,
    output_root: Path | str = DEFAULT_OUTPUT_ROOT,
    clock: Callable[[], datetime] = _utc_now,
    git_metadata_provider: Callable[[], GitMetadata] = _default_git_metadata,
) -> tuple[Any, Path]:
    """Seal one already-reserved failed run without credentials or network.

    The budget slot proves only a conservative reservation.  It cannot recover
    the in-memory channel observations lost by the failed runner, so the
    resulting postmortem keeps both channel result sets and the actual request
    count explicitly unavailable.
    """

    root = _path_inside_repository(
        output_root,
        repository_root=REPOSITORY_ROOT,
        label="diagnostic postmortem output_root",
    )
    root = _require_no_reparse_ancestors(
        root, "diagnostic postmortem output_root"
    )
    root_identity = _directory_identity(root)
    marker_path = root / _ROUND_FAILURE_MARKER_NAME
    slot_number = 1 if endpoint == "trade_cal" else 2
    slot_path = root / f".p0-round-budget-slot-{slot_number}.json"
    try:
        _require_directory_identity(root, root_identity)
        _require_no_reparse_ancestors(
            marker_path, "diagnostic postmortem failure marker"
        )
        marker = _read_round_failure_marker(marker_path)
        marker_bytes = marker_path.read_bytes()
        _require_no_reparse_ancestors(
            slot_path, "diagnostic postmortem budget slot"
        )
        slot_bytes = slot_path.read_bytes()
        _require_directory_identity(root, root_identity)
        _require_no_reparse_ancestors(
            slot_path, "diagnostic postmortem budget slot"
        )
        slot = strict_json_loads(
            slot_bytes,
            label="Tushare diagnostic budget slot",
            require_canonical=True,
        )
    except Exception as exc:
        raise TushareSingleEndpointDiagnosticError(
            "diagnostic postmortem budget evidence is unavailable"
        ) from exc
    if not isinstance(slot, Mapping) or slot.get("endpoint") != endpoint:
        raise TushareSingleEndpointDiagnosticError(
            "diagnostic postmortem endpoint differs from its budget slot"
        )
    if (
        marker["endpoint"] != endpoint
        or marker["diagnostic_run_id"] != slot.get("diagnostic_run_id")
        or marker["budget_slot_sha256"] != sha256_bytes(slot_bytes)
    ):
        raise TushareSingleEndpointDiagnosticError(
            "diagnostic postmortem failure marker differs from its budget slot"
        )

    run_directory = root / _safe_run_id(marker["diagnostic_run_id"])
    if run_directory.exists():
        if not run_directory.is_dir():
            raise TushareSingleEndpointDiagnosticError(
                "diagnostic postmortem run path is not a directory"
            )
        _require_no_reparse_ancestors(
            run_directory, "diagnostic postmortem run directory"
        )
        completed_receipt_path = run_directory / "diagnostic_receipt.json"
        _require_no_reparse_ancestors(
            completed_receipt_path, "completed diagnostic receipt"
        )
        if completed_receipt_path.exists():
            raise TushareSingleEndpointDiagnosticError(
                "completed diagnostic receipt exists; postmortem is forbidden"
            )

    recorded_at = clock()
    if recorded_at.tzinfo is None:
        raise TushareSingleEndpointDiagnosticError(
            "diagnostic postmortem clock must return a timezone-aware value"
        )
    git = git_metadata_provider()
    receipt = build_diagnostic_postmortem_receipt(
        budget_slot=slot,
        budget_slot_sha256=sha256_bytes(slot_bytes),
        failure_marker=marker,
        failure_marker_sha256=sha256_bytes(marker_bytes),
        recorded_at=recorded_at,
        git_commit=git.commit,
        git_worktree_status=git.worktree_status,
    )
    receipt_bytes = canonical_json_bytes(receipt.to_dict())
    verify_diagnostic_postmortem_receipt(receipt_bytes)

    if run_directory.exists():
        if not run_directory.is_dir():
            raise TushareSingleEndpointDiagnosticError(
                "diagnostic postmortem run path is not a directory"
            )
        _require_no_reparse_ancestors(
            run_directory, "diagnostic postmortem run directory"
        )
        if (run_directory / "diagnostic_receipt.json").exists():
            raise TushareSingleEndpointDiagnosticError(
                "completed diagnostic receipt appeared before postmortem publish"
            )
    else:
        run_directory.mkdir()
    identity = _directory_identity(run_directory)
    target = run_directory / "diagnostic_postmortem.sealed.v3.json"
    try:
        _write_create_only(
            target,
            receipt_bytes,
            controlled_root=run_directory,
            controlled_identity=identity,
        )
    except TushareCapabilityProbeError as exc:
        raise TushareSingleEndpointDiagnosticError(
            "refusing to overwrite an existing sealed diagnostic postmortem"
        ) from exc
    persisted = target.read_bytes()
    verify_diagnostic_postmortem_receipt(persisted)
    _require_directory_identity(run_directory, identity)
    return receipt, target


def finalize_reserved_failure_postmortem(
    *,
    endpoint: str,
    output_root: Path | str = DEFAULT_OUTPUT_ROOT,
    clock: Callable[[], datetime] = _utc_now,
    git_metadata_provider: Callable[[], GitMetadata] = _default_git_metadata,
) -> tuple[Any, Path]:
    """Seal one failed run while holding the same cross-process round lock."""

    with _exclusive_round_execution(output_root):
        return _finalize_reserved_failure_postmortem_locked(
            endpoint=endpoint,
            output_root=output_root,
            clock=clock,
            git_metadata_provider=git_metadata_provider,
        )


def _run_reserved_live_diagnostic(
    *,
    endpoint: str,
    token: str,
    output_root: Path | str,
    clock: Callable[[], datetime],
    sdk_loader: Callable[[], Any],
    sdk_client_module_loader: Callable[[], Any],
    session_factory: Callable[[], Any],
    git_metadata_provider: Callable[[], GitMetadata],
    parameters: Mapping[str, str],
    expected_fields: Sequence[str],
    config: ProbeConfig,
    diagnostic_code_sha256: str,
    started_at: datetime,
    run_id: str,
) -> tuple[Any, Path]:
    """Execute the two channels after a create-only budget slot exists."""

    with _SocketNetworkGate() as network_gate:
        sdk_result, sdk_version = _run_sdk_channel(
            endpoint=endpoint,
            parameters=parameters,
            expected_fields=expected_fields,
            token=token,
            clock=clock,
            sdk_loader=sdk_loader,
            sdk_client_module_loader=sdk_client_module_loader,
            session_factory=session_factory,
            network_gate=network_gate,
        )
        http_result = _run_http_channel(
            endpoint=endpoint,
            parameters=parameters,
            expected_fields=expected_fields,
            token=token,
            clock=clock,
            session_factory=session_factory,
            network_gate=network_gate,
        )
    total_requests = sdk_result.request_count + http_result.request_count
    if total_requests > PLANNED_SINGLE_ENDPOINT_REQUESTS:
        raise _RequestBudgetExceeded("single-endpoint request budget was exceeded")
    completed_at = clock()
    if completed_at.tzinfo is None:
        raise TushareSingleEndpointDiagnosticError(
            "diagnostic clock must return timezone-aware values"
        )
    git = git_metadata_provider()
    receipt = build_diagnostic_receipt(
        diagnostic_run_id=run_id,
        status="completed",
        started_at=started_at,
        completed_at=completed_at,
        endpoint=endpoint,
        semantic_parameters=parameters,
        sdk_version=sdk_version,
        python_version=_safe_version(platform.python_version(), "unknown"),
        credential_status="configured",
        config_sha256=config.config_sha256,
        diagnostic_code_sha256=diagnostic_code_sha256,
        git_commit=git.commit,
        git_worktree_status=git.worktree_status,
        channels=(sdk_result, http_result),
    )
    receipt_bytes = canonical_json_bytes(receipt.to_dict())
    _guard_bytes(receipt_bytes, token)
    verify_diagnostic_receipt(receipt_bytes)
    path = _publish_receipt(
        output_root=output_root,
        run_id=run_id,
        receipt_bytes=receipt_bytes,
        token=token,
    )
    return receipt, path


def run_live_diagnostic(
    *,
    endpoint: str,
    token: str,
    config_path: Path | str = DEFAULT_CONFIG_PATH,
    access_policy_path: Path | str = DEFAULT_PROVIDER_ACCESS_POLICY_PATH,
    output_root: Path | str = DEFAULT_OUTPUT_ROOT,
    budget_root: Path | str | None = None,
    clock: Callable[[], datetime] = _utc_now,
    sdk_loader: Callable[[], Any] = _default_sdk_loader,
    sdk_client_module_loader: Callable[[], Any] = _default_sdk_client_module_loader,
    session_factory: Callable[[], Any] = _new_no_retry_session,
    git_metadata_provider: Callable[[], GitMetadata] = _default_git_metadata,
) -> tuple[Any, Path]:
    """Run exactly one SDK diagnostic and one direct-HTTP diagnostic."""

    if not _credential_passes_local_preflight(token):
        raise TushareSingleEndpointDiagnosticError(
            "TUSHARE_TOKEN failed the local runtime credential preflight"
        )
    resolved_output_root = _path_inside_repository(
        output_root,
        repository_root=REPOSITORY_ROOT,
        label="diagnostic output_root",
    ).resolve()
    requested_budget_root = budget_root if budget_root is not None else output_root
    resolved_budget_root = _path_inside_repository(
        requested_budget_root,
        repository_root=REPOSITORY_ROOT,
        label="diagnostic budget_root",
    ).resolve()
    if resolved_output_root != resolved_budget_root:
        raise TushareSingleEndpointDiagnosticError(
            "live diagnostic output and round budget must share one fixed root"
        )
    resolved_config = _resolve_repository_file(config_path, "config")
    resolved_policy = _resolve_repository_file(access_policy_path, "provider policy")
    config = load_probe_config(resolved_config)
    policy = load_provider_access_policy(resolved_policy)
    _require_tushare_probe_policy(policy)
    _, parameters, expected_fields = _selected_spec(config, endpoint)
    diagnostic_code_sha256 = compute_diagnostic_implementation_bundle_sha256()
    started_at = clock()
    if started_at.tzinfo is None:
        raise TushareSingleEndpointDiagnosticError(
            "diagnostic clock must return timezone-aware values"
        )
    run_id = _new_run_id(started_at)
    with _exclusive_round_execution(resolved_budget_root):
        slot_path = _reserve_round_budget(
            budget_root=resolved_budget_root,
            endpoint=endpoint,
            run_id=run_id,
            reserved_at=started_at,
        )
        try:
            return _run_reserved_live_diagnostic(
                endpoint=endpoint,
                token=token,
                output_root=output_root,
                clock=clock,
                sdk_loader=sdk_loader,
                sdk_client_module_loader=sdk_client_module_loader,
                session_factory=session_factory,
                parameters=parameters,
                expected_fields=expected_fields,
                config=config,
                diagnostic_code_sha256=diagnostic_code_sha256,
                started_at=started_at,
                run_id=run_id,
                git_metadata_provider=git_metadata_provider,
            )
        except BaseException:
            # The marker records only the fixed outer-runner category.  Channel
            # exception types remain confined to their own completed receipt.
            try:
                failed_at = clock()
            except BaseException:
                failed_at = _utc_now()
            if not isinstance(failed_at, datetime) or failed_at.tzinfo is None:
                failed_at = _utc_now()
            _publish_round_failure_marker(
                budget_slot_path=slot_path,
                endpoint=endpoint,
                run_id=run_id,
                failed_at=failed_at,
                runner_exception_type="OtherError",
                failed_diagnostic_code_sha256=diagnostic_code_sha256,
            )
            raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one bounded Tushare SDK-versus-HTTP diagnostic"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="offline plan (default)")
    mode.add_argument("--live", action="store_true", help="run the two live channels")
    mode.add_argument(
        "--postmortem",
        action="store_true",
        help="seal an already-reserved failed run without credentials or network",
    )
    parser.add_argument("--endpoint", choices=SUPPORTED_ENDPOINTS, default="trade_cal")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument(
        "--provider-access-policy",
        default=str(DEFAULT_PROVIDER_ACCESS_POLICY_PATH),
    )
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    return parser


def _emit_safe(value: Mapping[str, Any], secret: str = "") -> None:
    content = canonical_json_bytes(value)
    _guard_bytes(content, secret)
    sys.stdout.buffer.write(content)
    sys.stdout.buffer.write(b"\n")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.postmortem:
            receipt, path = finalize_reserved_failure_postmortem(
                endpoint=args.endpoint,
                output_root=args.output_root,
            )
            relative = path.resolve().relative_to(REPOSITORY_ROOT.resolve()).as_posix()
            _emit_safe(
                {
                    "status": receipt.to_dict()["status"],
                    "endpoint": receipt.endpoint,
                    "actual_request_count": None,
                    "actual_request_count_lower_bound": receipt.actual_request_count_lower_bound,
                    "actual_request_count_upper_bound": receipt.actual_request_count_upper_bound,
                    "conclusion": receipt.conclusion,
                    "receipt_path": relative,
                    "credential_accessed": False,
                    "sdk_imported": False,
                    "network_accessed": False,
                    "rerun_permitted": False,
                    "full_probe_started": False,
                }
            )
            return 0
        plan = build_diagnostic_plan(
            endpoint=args.endpoint,
            config_path=args.config,
            access_policy_path=args.provider_access_policy,
        )
        if not args.live:
            _emit_safe(plan)
            return 0
        token = _read_tushare_token()
        if not token:
            _emit_safe(
                {
                    "status": "not_configured",
                    "credential_status": "not_configured",
                    "request_count": 0,
                    "round_budget_reserved": False,
                    "network_accessed": False,
                    "full_probe_started": False,
                }
            )
            return 2
        if not _credential_passes_local_preflight(token):
            _emit_safe(
                {
                    "status": "rejected_before_network",
                    "credential_status": "rejected_by_local_preflight",
                    "request_count": 0,
                    "round_budget_reserved": False,
                    "network_accessed": False,
                    "full_probe_started": False,
                }
            )
            return 2
        receipt, path = run_live_diagnostic(
            endpoint=args.endpoint,
            token=token,
            config_path=args.config,
            access_policy_path=args.provider_access_policy,
            output_root=args.output_root,
            budget_root=DEFAULT_OUTPUT_ROOT,
        )
        relative = path.resolve().relative_to(REPOSITORY_ROOT.resolve()).as_posix()
        _emit_safe(
            {
                "status": "completed",
                "endpoint": receipt.endpoint,
                "request_count": receipt.request_count,
                "maximum_session_request_budget": receipt.maximum_request_budget,
                "sdk": {
                    "transport_status": receipt.channels[0].transport_status,
                    "http_status": receipt.channels[0].http_status,
                    "upstream_code": receipt.channels[0].upstream_code,
                    "sdk_exception_type": receipt.channels[0].sdk_exception_type,
                    "sanitized_message_category": receipt.channels[0].sanitized_message_category,
                },
                "http": {
                    "transport_status": receipt.channels[1].transport_status,
                    "http_status": receipt.channels[1].http_status,
                    "upstream_code": receipt.channels[1].upstream_code,
                    "sdk_exception_type": receipt.channels[1].sdk_exception_type,
                    "sanitized_message_category": receipt.channels[1].sanitized_message_category,
                },
                "conclusion": receipt.conclusion,
                "receipt_path": relative,
                "stopped_after_single_endpoint": True,
            },
            token,
        )
        return 0
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        # Do not serialize exception text, args, request, response or traceback.
        _emit_safe(
            {
                "status": "failed",
                "error_category": "single_endpoint_diagnostic_failed",
                "exception_type": safe_exception_type(exc),
                "network_retry_started": False,
                "full_probe_started": False,
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
