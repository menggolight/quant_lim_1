"""Run a bounded, read-only Tushare capability probe.

The default CLI mode is an offline plan.  Only ``--live`` may read
``TUSHARE_TOKEN``, import the SDK, or call an upstream.  Evidence produced by
this module is capability-only: it never enters MarketData ``validated/`` and
cannot grant research, Paper, trading, or LIVE authority.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import ctypes
import importlib
import io
import logging
import os
import platform
import re
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from research.market_data.provider_access import (
    DEFAULT_PROVIDER_ACCESS_POLICY_PATH,
    ProviderAccessPolicy,
    load_provider_access_policy,
)
from research.market_data.providers.base import (
    DependencyMissingError,
    classify_unexpected_error,
    redact_sensitive_value,
    safe_error_text,
)
from research.market_data.tushare_capability import (
    ClassifiedEndpointError,
    CrossValidationOutcomeV1,
    EndpointResultV1,
    ProbeConfig,
    TushareCapabilityReceiptV1,
    build_capability_receipt,
    build_cross_validation_outcome,
    build_endpoint_result,
    canonical_json_bytes,
    classify_endpoint_error,
    load_probe_config,
    normalize_endpoint_result,
    normalize_parameters,
    replay_endpoint_raw,
    sha256_bytes,
    strict_json_loads,
    verify_capability_receipt,
)


PROBE_VERSION = "tushare-capability-probe-v1"
PLAN_SCHEMA_VERSION = "tushare-capability-plan-v1"
MANIFEST_SCHEMA_VERSION = "tushare-capability-raw-manifest-v1"
IMPLEMENTATION_BUNDLE_VERSION = "tushare-capability-implementation-bundle-v1"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPOSITORY_ROOT / "configs" / "tushare_capability_probe.v1.json"
DEFAULT_OUTPUT_ROOT = REPOSITORY_ROOT / "data" / "tmp" / "tushare-capability"
TOKEN_ENVIRONMENT_VARIABLE = "TUSHARE_TOKEN"

_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
_FIXED_SDK_METHODS = {
    "trade_cal": "trade_cal",
    "stock_basic": "stock_basic",
    "daily": "daily",
    "daily_basic": "daily_basic",
    "adj_factor": "adj_factor",
    "suspend_d": "suspend_d",
    "stk_limit": "stk_limit",
    "namechange": "namechange",
    "index_basic": "index_basic",
    "index_daily": "index_daily",
    "index_weight": "index_weight",
    "index_classify": "index_classify",
    "index_member_all": "index_member_all",
    "income": "income",
    "income_vip": "income_vip",
    "balancesheet": "balancesheet",
    "balancesheet_vip": "balancesheet_vip",
    "cashflow": "cashflow",
    "cashflow_vip": "cashflow_vip",
    "disclosure_date": "disclosure_date",
    "fina_indicator": "fina_indicator",
    "dividend": "dividend",
}
_FORBIDDEN_METHOD_MARKERS = ("factor_value", "news", "realtime", "minute", "order")
_DAILY_COMPARE_FIELDS = (
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "volume",
    "amount",
)
_IMPLEMENTATION_BUNDLE_PATHS = (
    "agent/tushare_capability_probe.py",
    "research/market_data/tushare_capability.py",
    "research/market_data/provider_access.py",
    "schemas/tushare_endpoint_result.v1.json",
    "schemas/tushare_capability_receipt.v1.json",
    "schemas/provider_access_policy.v1.json",
    "research/market_data/contracts.py",
    "research/market_data/providers/base.py",
    "research/market_data/providers/baostock.py",
    "research/market_data/validation.py",
    "configs/provider_access.v1.json",
    "configs/tushare_capability_probe.v1.json",
)
_SDK_OUTPUT_CAPTURE_LOCK = threading.RLock()


class TushareCapabilityProbeError(RuntimeError):
    """Raised when the probe boundary or persisted evidence is invalid."""


class _CredentialSafetyError(TushareCapabilityProbeError):
    """Raised when an untrusted boundary attempts to emit a credential."""


@dataclass(frozen=True)
class GitMetadata:
    commit: str
    worktree_status: str


@dataclass(frozen=True)
class _SuccessfulCall:
    endpoint: str
    parameters: Mapping[str, str]
    rows: tuple[Mapping[str, Any], ...]
    raw_payload: Any
    raw_relative_path: str
    raw_sha256: str


class _SdkCaptureStream:
    """In-memory text/binary stream used to suppress untrusted SDK output."""

    encoding = "utf-8"
    errors = "strict"

    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def write(self, value: str) -> int:
        text = str(value)
        self.buffer.write(text.encode(self.encoding, errors="backslashreplace"))
        return len(text)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False

    def getvalue(self) -> bytes:
        return self.buffer.getvalue()


def _flush_process_stdio(*streams: Any) -> None:
    """Best-effort flush of Python and C stdio before descriptor restoration."""

    for stream in streams:
        try:
            stream.flush()
        except Exception:
            pass
    libraries = ("ucrtbase", "msvcrt") if os.name == "nt" else (None,)
    for library in libraries:
        try:
            runtime = ctypes.CDLL(library)
            runtime.fflush(None)
            break
        except Exception:
            continue


def _redirect_windows_standard_handles(
    stdout_fd: int, stderr_fd: int
) -> tuple[Any, int | None, int | None, tuple[tuple[int, bool], ...]] | None:
    """Redirect Win32 GetStdHandle users in addition to CRT descriptors."""

    if os.name != "nt":
        return None
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetStdHandle.argtypes = [ctypes.c_ulong]
    kernel32.GetStdHandle.restype = ctypes.c_void_p
    kernel32.SetStdHandle.argtypes = [ctypes.c_ulong, ctypes.c_void_p]
    kernel32.SetStdHandle.restype = ctypes.c_int
    stdout_kind = ctypes.c_ulong(-11).value
    stderr_kind = ctypes.c_ulong(-12).value
    original_stdout_handle = kernel32.GetStdHandle(stdout_kind)
    original_stderr_handle = kernel32.GetStdHandle(stderr_kind)
    target_stdout_handle = msvcrt.get_osfhandle(stdout_fd)
    target_stderr_handle = msvcrt.get_osfhandle(stderr_fd)
    inheritability: list[tuple[int, bool]] = []
    for handle in (target_stdout_handle, target_stderr_handle):
        inherited = os.get_handle_inheritable(handle)
        inheritability.append((handle, inherited))
        if not inherited:
            os.set_handle_inheritable(handle, True)
    stdout_changed = False
    try:
        if not kernel32.SetStdHandle(stdout_kind, target_stdout_handle):
            raise OSError(ctypes.get_last_error(), "SetStdHandle(stdout) failed")
        stdout_changed = True
        if not kernel32.SetStdHandle(stderr_kind, target_stderr_handle):
            raise OSError(ctypes.get_last_error(), "SetStdHandle(stderr) failed")
    except BaseException:
        if stdout_changed:
            kernel32.SetStdHandle(stdout_kind, original_stdout_handle)
        for handle, inherited in inheritability:
            os.set_handle_inheritable(handle, inherited)
        raise
    return (
        kernel32,
        original_stdout_handle,
        original_stderr_handle,
        tuple(inheritability),
    )


def _restore_windows_standard_handles(state: tuple[Any, int | None, int | None, tuple[tuple[int, bool], ...]] | None) -> None:
    if state is None:
        return
    kernel32, stdout_handle, stderr_handle, inheritability = state
    stdout_kind = ctypes.c_ulong(-11).value
    stderr_kind = ctypes.c_ulong(-12).value
    kernel32.SetStdHandle(stdout_kind, stdout_handle)
    kernel32.SetStdHandle(stderr_kind, stderr_handle)
    for handle, inherited in inheritability:
        os.set_handle_inheritable(handle, inherited)


def _call_with_suppressed_sdk_output(
    callback: Callable[[], Any],
    *,
    secret: str,
    operation: str,
) -> tuple[Any, bool]:
    """Run an SDK boundary with Python plus process fd 1/2 output capture."""

    python_stdout = _SdkCaptureStream()
    python_stderr = _SdkCaptureStream()
    value: Any = None
    caught: BaseException | None = None
    with _SDK_OUTPUT_CAPTURE_LOCK, tempfile.TemporaryFile() as fd_stdout, tempfile.TemporaryFile() as fd_stderr:
        original_stdout = sys.stdout
        original_stderr = sys.stderr
        original_stream_handler_emit = logging.StreamHandler.emit

        def captured_stream_handler_emit(
            handler: logging.StreamHandler[Any], record: logging.LogRecord
        ) -> None:
            """Route handlers bound before Python redirection into the capture."""

            original_stream = handler.stream
            try:
                handler.stream = python_stderr
                original_stream_handler_emit(handler, record)
            finally:
                handler.stream = original_stream

        _flush_process_stdio(original_stdout, original_stderr)
        saved_stdout = os.dup(1)
        saved_stderr = os.dup(2)
        redirected_stdout = False
        redirected_stderr = False
        windows_handle_state = None
        try:
            os.dup2(fd_stdout.fileno(), 1)
            redirected_stdout = True
            os.dup2(fd_stderr.fileno(), 2)
            redirected_stderr = True
            windows_handle_state = _redirect_windows_standard_handles(
                fd_stdout.fileno(), fd_stderr.fileno()
            )
            logging.StreamHandler.emit = captured_stream_handler_emit
            try:
                with redirect_stdout(python_stdout), redirect_stderr(python_stderr):
                    try:
                        value = callback()
                    except BaseException as exc:  # re-raise process-control exceptions below
                        caught = exc
            finally:
                logging.StreamHandler.emit = original_stream_handler_emit
            _flush_process_stdio(original_stdout, original_stderr)
        finally:
            logging.StreamHandler.emit = original_stream_handler_emit
            _flush_process_stdio(original_stdout, original_stderr)
            _restore_windows_standard_handles(windows_handle_state)
            if redirected_stdout:
                os.dup2(saved_stdout, 1)
            if redirected_stderr:
                os.dup2(saved_stderr, 2)
            os.close(saved_stdout)
            os.close(saved_stderr)
        fd_stdout.seek(0)
        fd_stderr.seek(0)
        captured = (
            python_stdout.getvalue()
            + python_stderr.getvalue()
            + fd_stdout.read()
            + fd_stderr.read()
        )
    if secret and secret.encode("utf-8") in captured:
        raise _CredentialSafetyError(
            f"{operation} emitted credential-bearing stdout or stderr"
        ) from None
    if caught is not None:
        raise caught
    return value, bool(captured)


def compute_probe_implementation_bundle_sha256(
    repository_root: Path | str = REPOSITORY_ROOT,
) -> str:
    """Hash the fixed implementation and Schema bundle used by this probe."""

    root = Path(repository_root).resolve()
    files: list[dict[str, str]] = []
    for relative in _IMPLEMENTATION_BUNDLE_PATHS:
        path = root / Path(relative)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise TushareCapabilityProbeError(
                f"implementation bundle file is unavailable: {relative}"
            ) from exc
        files.append({"path": relative, "sha256": sha256_bytes(raw)})
    return sha256_bytes(
        canonical_json_bytes(
            {"bundle_version": IMPLEMENTATION_BUNDLE_VERSION, "files": files}
        )
    )


def _endpoint_name(spec: Any) -> str:
    value = getattr(spec.endpoint, "value", spec.endpoint)
    name = str(value).strip()
    if name not in _FIXED_SDK_METHODS:
        raise TushareCapabilityProbeError("endpoint is outside the fixed SDK allowlist")
    return name


def _sdk_method_name(spec: Any) -> str:
    endpoint = _endpoint_name(spec)
    expected = _FIXED_SDK_METHODS[endpoint]
    declared = str(getattr(spec, "sdk_method", expected) or "")
    if declared != expected or any(marker in declared for marker in _FORBIDDEN_METHOD_MARKERS):
        raise TushareCapabilityProbeError(
            "endpoint SDK method differs from the fixed read-only allowlist"
        )
    return expected


def _parameter_sets(spec: Any) -> tuple[Mapping[str, str], ...]:
    maximum = int(getattr(spec, "max_calls", 0))
    values = tuple(getattr(spec, "parameters", ()))
    if maximum <= 0 or not values:
        raise TushareCapabilityProbeError("endpoint has no bounded probe parameters")
    if len(values) > maximum:
        raise TushareCapabilityProbeError("endpoint parameters exceed max_calls")
    normalized: list[Mapping[str, str]] = []
    for value in values:
        normalized.append(normalize_parameters(spec.endpoint, value))
    return tuple(normalized)


def _require_tushare_probe_policy(policy: ProviderAccessPolicy) -> None:
    tushare = policy.tushare
    if (
        tushare.access_status != "capability_probe_only"
        or tushare.capability_probe_allowed is not True
        or tushare.formal_provider_allowed is not False
        or tushare.automatic_fallback_allowed is not False
        or tushare.partial_fallback_allowed is not False
    ):
        raise TushareCapabilityProbeError(
            "versioned provider access policy does not authorize a capability-only probe"
        )


def build_plan(
    config: ProbeConfig,
    *,
    access_policy: ProviderAccessPolicy | None = None,
    access_policy_path: Path | str = DEFAULT_PROVIDER_ACCESS_POLICY_PATH,
) -> dict[str, Any]:
    """Build a deterministic plan without reading credentials or loading SDKs."""

    policy = access_policy or load_provider_access_policy(access_policy_path)
    _require_tushare_probe_policy(policy)
    endpoints: list[dict[str, Any]] = []
    planned_tushare_requests = 0
    for spec in config.endpoints:
        if _endpoint_name(spec) == "daily" and dict(spec.raw_units) != {
            "amount": "thousand_CNY",
            "vol": "lots_of_100_shares",
        }:
            raise TushareCapabilityProbeError(
                "daily raw units differ from the fixed cross-validation conversion"
            )
        parameters = _parameter_sets(spec)
        planned_tushare_requests += len(parameters)
        endpoints.append(
            {
                "endpoint": _endpoint_name(spec),
                "sdk_method": _sdk_method_name(spec),
                "parameters": [dict(item) for item in parameters],
                "max_calls": int(spec.max_calls),
                "max_rows": int(spec.max_rows),
                "required_probe": bool(spec.required_probe),
                "pit_critical": bool(spec.pit_critical),
                "cross_validation_only": bool(spec.cross_validation_only),
                "expected_data_role": str(spec.expected_data_role),
                "migration_candidate_role": str(spec.migration_candidate_role),
            }
        )
    reserve = int(config.cross_validation_request_reserve)
    planned_total = planned_tushare_requests + reserve
    if planned_total > int(config.maximum_request_count):
        raise TushareCapabilityProbeError(
            "planned requests plus cross-validation reserve exceed the global cap"
        )
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "probe_version": PROBE_VERSION,
        "mode": "plan_offline_no_credentials",
        "scope": "capability_probe_only_not_admitted",
        "provider_id": "tushare",
        "provider_access_policy_schema_version": policy.schema_version,
        "provider_access_status": policy.tushare.access_status,
        "capability_probe_allowed": True,
        "formal_provider_allowed": False,
        "config_sha256": config.config_sha256,
        "endpoints": endpoints,
        "planned_tushare_request_count": planned_tushare_requests,
        "cross_validation_request_reserve": reserve,
        "planned_maximum_request_count": planned_total,
        "global_maximum_request_count": int(config.maximum_request_count),
        "minimum_interval_seconds": str(config.minimum_interval_seconds),
        "global_stop_after_consecutive_rate_limits": int(
            config.global_stop_after_consecutive_rate_limits
        ),
        "global_stop_on_permission_denied": bool(
            config.global_stop_on_permission_denied
        ),
        "credential_accessed": False,
        "sdk_imported": False,
        "network_accessed": False,
        "formal_data_admission": False,
        "validated_storage_write": False,
        "automatic_fallback": False,
        "live_supported": False,
    }


def _default_sdk_loader() -> Any:
    try:
        return importlib.import_module("tushare")
    except ModuleNotFoundError as exc:
        if exc.name not in {None, "tushare"}:
            raise DependencyMissingError(
                "Tushare SDK dependency import failed"
            ) from exc
        raise DependencyMissingError(
            "Tushare SDK is not installed; install the market-tushare extra"
        ) from exc
    except ImportError as exc:
        raise DependencyMissingError("Tushare SDK import failed") from exc


def _read_tushare_token() -> str:
    """Read the sole authorized credential source; never call in plan mode."""

    value = os.environ.get(TOKEN_ENVIRONMENT_VARIABLE)
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _default_git_metadata() -> GitMetadata:
    def run(*arguments: str) -> str:
        completed = subprocess.run(
            ("git", *arguments),
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return completed.stdout.strip()

    try:
        commit = run("rev-parse", "HEAD")
        dirty = bool(run("status", "--porcelain"))
    except (OSError, subprocess.SubprocessError):
        return GitMetadata(commit="unknown", worktree_status="unknown")
    return GitMetadata(commit=commit, worktree_status="dirty" if dirty else "clean")


def _aware_now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TushareCapabilityProbeError("clock must return a timezone-aware datetime")
    return value


def _new_run_id(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _safe_run_id(value: str) -> str:
    text = str(value)
    windows_stem = text.split(".", 1)[0].upper()
    windows_devices = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
    if (
        _SAFE_RUN_ID.fullmatch(text) is None
        or text in {".", ".."}
        or text != text.strip()
        or text.rstrip(" .") != text
        or windows_stem in windows_devices
    ):
        raise TushareCapabilityProbeError("unsafe probe_run_id")
    return text


def _path_inside_repository(
    path: Path | str,
    *,
    repository_root: Path,
    label: str,
) -> Path:
    repository = repository_root.resolve()
    allowed_lexical = repository / "data" / "tmp" / "tushare-capability"
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = repository / candidate
    candidate_lexical = Path(os.path.abspath(candidate))
    try:
        relative = candidate_lexical.relative_to(repository)
        candidate_lexical.relative_to(allowed_lexical)
    except ValueError as exc:
        raise TushareCapabilityProbeError(
            f"{label} must stay inside data/tmp/tushare-capability"
        ) from exc

    # Resolve only after inspecting the lexical path.  Otherwise an existing
    # symlink/junction at data/, tmp/, or tushare-capability/ disappears from
    # the path and can redirect writes into a forbidden repository directory.
    current = repository
    for part in relative.parts:
        current = current / part
        is_junction = getattr(current, "is_junction", lambda: False)
        if current.is_symlink() or is_junction():
            raise TushareCapabilityProbeError(
                f"{label} cannot contain symbolic links or junctions"
            )
        if not current.exists():
            break
        try:
            attributes = getattr(current.lstat(), "st_file_attributes", 0)
        except OSError as exc:
            raise TushareCapabilityProbeError(
                f"{label} path metadata is unavailable"
            ) from exc
        if bool(attributes & 0x400):
            raise TushareCapabilityProbeError(
                f"{label} cannot contain symbolic links or junctions"
            )

    allowed_root = allowed_lexical.resolve()
    resolved = candidate_lexical.resolve()
    try:
        allowed_root.relative_to(repository)
        resolved.relative_to(allowed_root)
    except ValueError as exc:
        raise TushareCapabilityProbeError(
            f"{label} must resolve inside data/tmp/tushare-capability"
        ) from exc
    return resolved


def _write_create_only(
    path: Path,
    content: bytes,
    *,
    controlled_root: Path | None = None,
    controlled_identity: tuple[int, int] | None = None,
) -> None:
    """Atomically publish fully flushed bytes without replacing a target."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if controlled_root is not None or controlled_identity is not None:
        if controlled_root is None or controlled_identity is None:
            raise TushareCapabilityProbeError(
                "controlled create-only write is missing its directory identity"
            )
        _require_directory_identity(controlled_root, controlled_identity)
        _require_no_reparse_ancestors(path.parent, "controlled artifact parent")
        try:
            path.parent.resolve().relative_to(controlled_root.resolve())
        except ValueError as exc:
            raise TushareCapabilityProbeError(
                "controlled artifact parent escapes the probe run"
            ) from exc
    descriptor, temporary_name = tempfile.mkstemp(prefix=".probe-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            written = handle.write(content)
            if written != len(content):
                raise TushareCapabilityProbeError("short evidence write")
            handle.flush()
            os.fsync(handle.fileno())
        if controlled_root is not None and controlled_identity is not None:
            _require_directory_identity(controlled_root, controlled_identity)
            _require_no_reparse_ancestors(path.parent, "controlled artifact parent")
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise TushareCapabilityProbeError(
                f"refusing to overwrite existing probe artifact: {path.name}"
            ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _is_reparse_entry(path: Path) -> bool:
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        return True
    try:
        return bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
    except OSError:
        return False


def _require_no_reparse_ancestors(path: Path | str, label: str) -> Path:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if _is_reparse_entry(current):
            raise TushareCapabilityProbeError(
                f"{label} cannot traverse a symbolic link, junction, or reparse point"
            )
        if not current.exists():
            break
    return absolute


def _directory_identity(path: Path) -> tuple[int, int]:
    if _is_reparse_entry(path) or not path.is_dir():
        raise TushareCapabilityProbeError(
            "controlled probe staging directory is unavailable"
        )
    metadata = path.stat()
    return metadata.st_dev, metadata.st_ino


def _require_directory_identity(
    path: Path,
    expected: tuple[int, int],
) -> None:
    _require_no_reparse_ancestors(path, "controlled probe staging path")
    if _directory_identity(path) != expected:
        raise TushareCapabilityProbeError(
            "controlled probe staging directory identity changed"
        )


def _publish_probe_run(
    *,
    root: Path,
    identifier: str,
    artifacts: Mapping[str, bytes],
    repository_root: Path,
    config: ProbeConfig,
    secret: str,
    access_policy_path: Path | str,
    implementation_root: Path | str,
    expected_plan: Mapping[str, Any],
) -> Path:
    """Publish a fully built run after every untrusted provider callback ended."""

    root = _path_inside_repository(
        root,
        repository_root=repository_root,
        label="output_root",
    )
    root.mkdir(parents=True, exist_ok=True)
    root = _path_inside_repository(
        root,
        repository_root=repository_root,
        label="output_root",
    )
    _require_no_reparse_ancestors(root, "output_root")
    final_directory = root / identifier
    try:
        final_directory.mkdir()
    except FileExistsError as exc:
        raise TushareCapabilityProbeError(
            "refusing to overwrite an existing probe run"
        ) from exc
    identity = _directory_identity(final_directory)
    ordered_paths = ["plan.json"] + sorted(
        path
        for path in artifacts
        if path not in {"plan.json", "manifest.json", "receipt.json"}
    ) + ["manifest.json", "receipt.json"]
    if set(ordered_paths) != set(artifacts) or len(ordered_paths) != len(artifacts):
        raise TushareCapabilityProbeError(
            "probe publication artifact set is invalid"
        )
    for relative in ordered_paths:
        relative_path = Path(relative)
        if (
            relative_path.is_absolute()
            or ".." in relative_path.parts
            or "\\" in relative
        ):
            raise TushareCapabilityProbeError(
                "probe publication artifact path is unsafe"
            )
        _require_directory_identity(final_directory, identity)
        content = artifacts[relative]
        _guard_bytes(content, secret)
        _write_create_only(
            final_directory / relative_path,
            content,
            controlled_root=final_directory,
            controlled_identity=identity,
        )
        _require_directory_identity(final_directory, identity)
        _require_no_reparse_ancestors(
            final_directory / relative_path,
            "probe publication artifact",
        )
    verify_probe_run(
        final_directory,
        config=config,
        secret=secret,
        access_policy_path=access_policy_path,
        implementation_root=implementation_root,
        _expected_plan=expected_plan,
    )
    _require_directory_identity(final_directory, identity)
    return final_directory


def _strict_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = strict_json_loads(raw)
    except Exception as exc:
        raise TushareCapabilityProbeError(f"invalid {label}") from exc
    if not isinstance(value, dict):
        raise TushareCapabilityProbeError(f"{label} root must be an object")
    if canonical_json_bytes(value) != raw:
        raise TushareCapabilityProbeError(f"{label} is not canonical JSON")
    return value


def _contains_secret(value: Any, secret: str) -> bool:
    if not secret:
        return False
    if isinstance(value, bytes):
        return secret.encode("utf-8") in value
    if isinstance(value, str):
        return secret in value
    if isinstance(value, Mapping):
        return any(
            _contains_secret(key, secret) or _contains_secret(item, secret)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_secret(item, secret) for item in value)
    return False


def _scrub_value(value: Any, secret: str) -> Any:
    if isinstance(value, str):
        return safe_error_text(value.replace(secret, "[REDACTED]")) if secret else safe_error_text(value)
    if isinstance(value, bytes):
        return value.replace(secret.encode("utf-8"), b"[REDACTED]") if secret else value
    if isinstance(value, Mapping):
        return redact_sensitive_value(
            {str(key): _scrub_value(item, secret) for key, item in value.items()}
        )
    if isinstance(value, tuple):
        return tuple(_scrub_value(item, secret) for item in value)
    if isinstance(value, list):
        return [_scrub_value(item, secret) for item in value]
    return value


def _sanitized_error(error: Exception, secret: str) -> Exception:
    message = str(error)
    if secret:
        message = message.replace(secret, "[REDACTED]")
    safe = safe_error_text(message)
    error_type = type(error)
    try:
        return error_type(safe)
    except Exception:
        classified = classify_unexpected_error(error)
        try:
            return type(classified)(safe)
        except Exception:
            return RuntimeError(safe)


def _guard_bytes(content: bytes, secret: str) -> None:
    if secret and secret.encode("utf-8") in content:
        raise TushareCapabilityProbeError(
            "refusing to persist or print credential-bearing evidence"
        )


def _object_dict(value: Any, label: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    serializer = getattr(value, "to_dict", None)
    if callable(serializer):
        payload = serializer()
        if isinstance(payload, Mapping):
            return dict(payload)
    raise TushareCapabilityProbeError(f"{label} does not expose a typed mapping")


def _error_results(
    config: ProbeConfig,
    *,
    status: str,
    instant: datetime,
    error: Exception | None = None,
) -> list[EndpointResultV1]:
    results: list[EndpointResultV1] = []
    classified_by_status = {
        "not_configured": ClassifiedEndpointError(
            "not_configured", "not_configured", "configuration", "not_configured"
        ),
        "dependency_missing": ClassifiedEndpointError(
            "dependency_missing", "not_applicable", "dependency", "dependency_missing"
        ),
    }
    selected_error: Exception | ClassifiedEndpointError | None = error
    if selected_error is None:
        selected_error = classified_by_status.get(status)
    if selected_error is None:
        raise TushareCapabilityProbeError("unsupported zero-request endpoint status")
    for spec in config.endpoints:
        for parameters in _parameter_sets(spec):
            results.append(
                build_endpoint_result(
                    spec,
                    requested_at=instant,
                    completed_at=instant,
                    sanitized_parameters=parameters,
                    request_count=0,
                    error=selected_error,
                    status=status,
                    notes=("no upstream request was attempted",),
                )
            )
    return results


def _parse_tushare_date(value: str) -> date:
    text = str(value).strip()
    if re.fullmatch(r"\d{8}", text):
        return datetime.strptime(text, "%Y%m%d").date()
    return date.fromisoformat(text)


def _build_baostock_daily_request(
    parameters: Mapping[str, str],
    requested_at: datetime,
) -> Any:
    from research.market_data.contracts import MarketDataRequest

    return MarketDataRequest(
        dataset_type="daily_bar",
        instrument_id=str(parameters.get("ts_code") or ""),
        start_date=_parse_tushare_date(str(parameters.get("start_date") or "")),
        end_date=_parse_tushare_date(str(parameters.get("end_date") or "")),
        retrieval_mode="historical_backfill",
        adjustment="none",
        requested_at=requested_at,
    )


def _replay_baostock_daily_raw(
    parameters: Mapping[str, str],
    requested_at: datetime,
    completed_at: datetime,
    raw_content: bytes,
) -> tuple[Mapping[str, Any], ...]:
    from research.market_data.providers.baostock import replay_baostock_raw

    request = _build_baostock_daily_request(parameters, requested_at)
    return replay_baostock_raw(request, raw_content, completed_at)


def _default_baostock_capture(
    parameters: Mapping[str, str],
    requested_at: datetime,
) -> tuple[tuple[Mapping[str, Any], ...], bytes]:
    from research.market_data.providers.baostock import BaoStockProvider

    request = _build_baostock_daily_request(parameters, requested_at)
    payload = BaoStockProvider().fetch(request)
    return tuple(dict(item) for item in payload.records), payload.raw_content


def _decimal(value: Any, *, multiplier: Decimal = Decimal("1")) -> Decimal | None:
    if value in {None, ""}:
        return None
    try:
        number = Decimal(str(value)) * multiplier
    except (InvalidOperation, ValueError) as exc:
        raise TushareCapabilityProbeError("daily comparison contains invalid numeric data") from exc
    if not number.is_finite():
        raise TushareCapabilityProbeError("daily comparison contains non-finite data")
    return number


def _daily_row(record: Mapping[str, Any], *, provider: str) -> dict[str, Any]:
    trading_date = str(record.get("trade_date") or record.get("trading_date") or "")
    if re.fullmatch(r"\d{8}", trading_date):
        trading_date = datetime.strptime(trading_date, "%Y%m%d").date().isoformat()
    volume_key = "vol" if provider == "tushare" else "volume"
    preclose_key = "pre_close" if provider == "tushare" else "preclose"
    volume_multiplier = Decimal("100") if provider == "tushare" else Decimal("1")
    amount_multiplier = Decimal("1000") if provider == "tushare" else Decimal("1")
    return {
        "trade_date": trading_date,
        "open": _decimal(record.get("open")),
        "high": _decimal(record.get("high")),
        "low": _decimal(record.get("low")),
        "close": _decimal(record.get("close")),
        "pre_close": _decimal(record.get(preclose_key)),
        "volume": _decimal(record.get(volume_key), multiplier=volume_multiplier),
        "amount": _decimal(record.get("amount"), multiplier=amount_multiplier),
    }


def compare_daily_samples(
    tushare_rows: Sequence[Mapping[str, Any]],
    baostock_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compare independent daily samples after explicit unit normalization."""

    tushare = {
        row["trade_date"]: row
        for row in (_daily_row(item, provider="tushare") for item in tushare_rows)
        if row["trade_date"]
    }
    baostock = {
        row["trade_date"]: row
        for row in (_daily_row(item, provider="baostock") for item in baostock_rows)
        if row["trade_date"]
    }
    overlap = sorted(set(tushare) & set(baostock))
    only_tushare = sorted(set(tushare) - set(baostock))
    only_baostock = sorted(set(baostock) - set(tushare))
    fields: dict[str, Any] = {
        "trade_date": {
            "matched_count": len(overlap),
            "tushare_only_count": len(only_tushare),
            "baostock_only_count": len(only_baostock),
        }
    }
    for field in _DAILY_COMPARE_FIELDS[1:]:
        differences: list[Decimal] = []
        missing_pair_count = 0
        mismatch_count = 0
        for trading_date in overlap:
            left = tushare[trading_date][field]
            right = baostock[trading_date][field]
            if left is None or right is None:
                missing_pair_count += 1
                continue
            difference = abs(left - right)
            differences.append(difference)
            if difference != 0:
                mismatch_count += 1
        fields[field] = {
            "compared_count": len(differences),
            "missing_pair_count": missing_pair_count,
            "mismatch_count": mismatch_count,
            "maximum_absolute_difference": (
                str(max(differences)) if differences else None
            ),
            "mean_absolute_difference": (
                str(sum(differences, Decimal("0")) / len(differences))
                if differences
                else None
            ),
        }
    return {
        "status": "compared_no_threshold" if overlap else "compared_no_overlap_no_threshold",
        "dataset": "daily_bar_small_sample",
        "providers": ["tushare", "baostock"],
        "independent_batches": True,
        "records_merged": False,
        "missing_values_filled_across_providers": False,
        "automatic_difference_threshold": None,
        "threshold_status": "not_configured",
        "unit_normalization": {
            "volume": {
                "tushare_raw_unit": "lots_of_100_shares",
                "baostock_raw_unit": "shares",
                "comparison_unit": "shares",
                "tushare_multiplier": "100",
            },
            "amount": {
                "tushare_raw_unit": "thousand_CNY",
                "baostock_raw_unit": "cny",
                "comparison_unit": "cny",
                "tushare_multiplier": "1000",
            },
        },
        "field_differences": fields,
    }


def _cross_validation_not_configured(reason: str, error: Exception | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "cross_validation_not_configured",
        "dataset": "daily_bar_small_sample",
        "providers": ["tushare", "baostock"],
        "independent_batches": True,
        "records_merged": False,
        "missing_values_filled_across_providers": False,
        "automatic_difference_threshold": None,
        "threshold_status": "not_configured",
        "reason": reason,
    }
    if error is not None:
        classified = classify_unexpected_error(error)
        result.update(
            {
                "failure_code": classified.code,
                "error": safe_error_text(classified),
            }
        )
    return result


def _result_status(result: EndpointResultV1) -> str:
    return str(getattr(result, "status", _object_dict(result, "endpoint result").get("status")))


def _manifest(
    *,
    run_id: str,
    plan_sha256: str,
    raw_artifacts: Sequence[Mapping[str, Any]],
    cross_validation: Mapping[str, Any],
    request_count: int,
) -> dict[str, Any]:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "probe_version": PROBE_VERSION,
        "probe_run_id": run_id,
        # The manifest is written before the receipt.  It must never claim the
        # run completed because a crash may leave this as the final artifact.
        "status": "raw_evidence_sealed_receipt_pending",
        "scope": "capability_probe_only_not_admitted",
        "plan": {"path": "plan.json", "sha256": plan_sha256},
        "raw_artifacts": [dict(item) for item in raw_artifacts],
        "cross_validation": dict(cross_validation),
        "request_count": request_count,
        "formal_data_admission": False,
        "validated_storage_write": False,
        "market_data_batch_created": False,
        "records_merged_across_providers": False,
        "automatic_fallback": False,
        "experiment_v3_impact": "none",
        "daily_signal_authority": "none",
        "paper_eligibility": False,
        "trade_eligibility": False,
        "live_supported": False,
    }


def _build_receipt_for_run(
    *,
    run_id: str,
    started_at: datetime,
    completed_at: datetime,
    sdk_version: str,
    credential_status: str,
    config: ProbeConfig,
    git: GitMetadata,
    endpoint_results: Sequence[EndpointResultV1],
    request_count: int,
    rate_limit_events: int,
    manifest_sha256: str,
    probe_code_sha256: str,
    cross_validation_outcome: CrossValidationOutcomeV1,
) -> TushareCapabilityReceiptV1:
    return build_capability_receipt(
        config,
        probe_run_id=run_id,
        started_at=started_at,
        completed_at=completed_at,
        sdk_version=sdk_version,
        python_version=platform.python_version(),
        credential_status=credential_status,
        probe_code_sha256=probe_code_sha256,
        git_commit=git.commit,
        git_worktree_status=git.worktree_status,
        endpoint_results=tuple(endpoint_results),
        cross_validation_outcome=cross_validation_outcome,
        request_count=request_count,
        rate_limit_events=rate_limit_events,
        raw_evidence_manifest_sha256=manifest_sha256,
    )


def run_live_probe(
    config: ProbeConfig,
    output_root: Path | str,
    *,
    sdk_loader: Callable[[], Any] | None = None,
    baostock_capture: Callable[
        [Mapping[str, str], datetime],
        tuple[tuple[Mapping[str, Any], ...], bytes],
    ]
    | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleeper: Callable[[float], None] = time.sleep,
    run_id: str | None = None,
    repository_root: Path = REPOSITORY_ROOT,
    implementation_root: Path | str = REPOSITORY_ROOT,
    git_metadata_loader: Callable[[], GitMetadata] = _default_git_metadata,
    access_policy_path: Path | str = DEFAULT_PROVIDER_ACCESS_POLICY_PATH,
) -> dict[str, Any]:
    """Execute one bounded live probe and persist a replayable create-only run."""

    policy = load_provider_access_policy(access_policy_path)
    _require_tushare_probe_policy(policy)
    started_at = _aware_now(clock)
    identifier = _safe_run_id(run_id or _new_run_id(started_at))
    plan = {
        **build_plan(config, access_policy=policy),
        "probe_run_id": identifier,
        "mode": "live_bounded_read_only",
    }
    plan_bytes = canonical_json_bytes(plan)
    plan_sha256 = sha256_bytes(plan_bytes)
    probe_code_sha256 = compute_probe_implementation_bundle_sha256(
        implementation_root
    )
    root = _path_inside_repository(
        output_root,
        repository_root=repository_root,
        label="output_root",
    )
    if os.path.lexists(root / identifier):
        raise TushareCapabilityProbeError(
            "refusing to overwrite an existing probe run"
        )

    token = _read_tushare_token()
    endpoint_results: list[EndpointResultV1] = []
    raw_artifacts: list[dict[str, Any]] = []
    pending_raw_artifacts: dict[str, bytes] = {}
    successful_calls: list[_SuccessfulCall] = []
    request_count = 0
    rate_limit_events = 0
    sdk_version = "not_loaded"
    credential_status = "configured" if token else "not_configured"
    global_stop = False
    consecutive_rate_limits = 0

    module: Any | None = None
    client: Any | None = None
    initialization_error: Exception | None = None
    if not token:
        endpoint_results.extend(
            _error_results(config, status="not_configured", instant=started_at)
        )
    else:
        try:
            def initialize_sdk() -> tuple[Any, str, Any]:
                loaded = (sdk_loader or _default_sdk_loader)()
                loaded_version = str(
                    getattr(loaded, "__version__", "unknown") or "unknown"
                )
                if _contains_secret(loaded_version, token):
                    raise _CredentialSafetyError(
                        "SDK metadata contains the configured credential"
                    )
                pro_api = getattr(loaded, "pro_api", None)
                if not callable(pro_api):
                    raise DependencyMissingError(
                        "Tushare SDK does not expose pro_api"
                    )
                return loaded, loaded_version, pro_api(token)

            (module, sdk_version, client), _ = _call_with_suppressed_sdk_output(
                initialize_sdk,
                secret=token,
                operation="Tushare SDK initialization",
            )
        except Exception as exc:
            initialization_error = _sanitized_error(exc, token)
            status = classify_endpoint_error(initialization_error).status
            if status == "rate_limited":
                # One initialization event is replicated across the frozen
                # endpoint plan; it is still a single observed rate-limit.
                rate_limit_events = 1
            endpoint_results.extend(
                _error_results(
                    config,
                    status=str(status),
                    instant=started_at,
                    error=initialization_error,
                )
            )

    if token and initialization_error is None and client is not None:
        endpoint_budget = int(config.maximum_request_count) - int(
            config.cross_validation_request_reserve
        )
        for spec in config.endpoints:
            endpoint_name = _endpoint_name(spec)
            method_name = _sdk_method_name(spec)
            for call_index, parameters in enumerate(_parameter_sets(spec), start=1):
                requested_at = _aware_now(clock)
                if global_stop or request_count >= endpoint_budget:
                    endpoint_results.append(
                        build_endpoint_result(
                            spec,
                            requested_at=requested_at,
                            completed_at=requested_at,
                            sanitized_parameters=parameters,
                            request_count=0,
                            status="not_run_after_global_stop",
                            notes=(
                                "global stop was active"
                                if global_stop
                                else "global request cap was reached",
                            ),
                        )
                    )
                    continue
                if request_count:
                    sleeper(float(config.minimum_interval_seconds))
                request_count += 1
                try:
                    def invoke_endpoint() -> Any:
                        method = getattr(client, method_name, None)
                        if not callable(method):
                            raise DependencyMissingError(
                                f"fixed Tushare SDK endpoint {endpoint_name} is unavailable"
                            )
                        response = method(**dict(parameters))
                        return normalize_endpoint_result(
                            spec,
                            response,
                            parameters,
                        )

                    normalized, output_suppressed = _call_with_suppressed_sdk_output(
                        invoke_endpoint,
                        secret=token,
                        operation=f"Tushare endpoint {endpoint_name}",
                    )
                    if _contains_secret(normalized.raw_payload, token) or _contains_secret(
                        normalized.rows, token
                    ):
                        raise _CredentialSafetyError(
                            "upstream payload contains the configured credential"
                        )
                    # The domain normalizer already produced the canonical raw
                    # bytes bound by normalized.raw_payload_sha256.  Persist
                    # exactly those bytes rather than hashing a second render.
                    raw_bytes = normalized.raw_payload
                    _guard_bytes(raw_bytes, token)
                    relative_path = f"raw/{endpoint_name}.{call_index:02d}.json"
                    if relative_path in pending_raw_artifacts:
                        raise TushareCapabilityProbeError(
                            "duplicate pending raw artifact path"
                        )
                    pending_raw_artifacts[relative_path] = raw_bytes
                    raw_sha256 = sha256_bytes(raw_bytes)
                    raw_artifacts.append(
                        {
                            "provider_id": "tushare",
                            "endpoint": endpoint_name,
                            "call_index": call_index,
                            "endpoint_result_index": len(endpoint_results),
                            "sanitized_parameters": dict(parameters),
                            "path": relative_path,
                            "sha256": raw_sha256,
                        }
                    )
                    result = build_endpoint_result(
                        spec,
                        requested_at=requested_at,
                        completed_at=_aware_now(clock),
                        sanitized_parameters=parameters,
                        request_count=1,
                        normalized=normalized,
                        notes=(
                            f"raw_evidence_path={relative_path}",
                            *(
                                ("sdk_stdout_stderr_suppressed",)
                                if output_suppressed
                                else ()
                            ),
                        ),
                    )
                    endpoint_results.append(result)
                    if _result_status(result) == "passed":
                        successful_calls.append(
                            _SuccessfulCall(
                                endpoint=endpoint_name,
                                parameters=parameters,
                                rows=tuple(dict(item) for item in normalized.rows),
                                raw_payload=raw_bytes,
                                raw_relative_path=relative_path,
                                raw_sha256=raw_sha256,
                            )
                        )
                    consecutive_rate_limits = 0
                except Exception as exc:
                    sanitized = _sanitized_error(exc, token)
                    result = build_endpoint_result(
                        spec,
                        requested_at=requested_at,
                        completed_at=_aware_now(clock),
                        sanitized_parameters=parameters,
                        request_count=1,
                        error=sanitized,
                    )
                    endpoint_results.append(result)
                    status = _result_status(result)
                    if isinstance(exc, _CredentialSafetyError):
                        global_stop = True
                    if status == "rate_limited":
                        rate_limit_events += 1
                        consecutive_rate_limits += 1
                        if consecutive_rate_limits >= int(
                            config.global_stop_after_consecutive_rate_limits
                        ):
                            global_stop = True
                    else:
                        consecutive_rate_limits = 0
                    if status == "permission_denied" and bool(
                        config.global_stop_on_permission_denied
                    ):
                        global_stop = True

    daily_call = next(
        (item for item in successful_calls if item.endpoint == "daily"),
        None,
    )
    cross_validation: dict[str, Any]
    if global_stop:
        cross_validation = _cross_validation_not_configured(
            "global stop prevented every subsequent upstream request"
        )
    elif daily_call is None:
        cross_validation = _cross_validation_not_configured(
            "no passed Tushare daily sample was available"
        )
    elif int(config.cross_validation_request_reserve) < 1 or request_count >= int(
        config.maximum_request_count
    ):
        cross_validation = _cross_validation_not_configured(
            "the bounded request cap left no cross-validation request"
        )
    else:
        request_count += 1
        cross_requested_at = _aware_now(clock)
        try:
            def invoke_baostock() -> tuple[
                tuple[Mapping[str, Any], ...], bytes
            ]:
                return (baostock_capture or _default_baostock_capture)(
                    daily_call.parameters,
                    cross_requested_at,
                )

            (rows, raw_content), _ = _call_with_suppressed_sdk_output(
                invoke_baostock,
                secret=token,
                operation="BaoStock cross-validation capture",
            )
            if _contains_secret(rows, token) or _contains_secret(raw_content, token):
                raise _CredentialSafetyError(
                    "BaoStock cross-validation payload contains the configured credential"
                )
            cross_completed_at = _aware_now(clock)
            if not isinstance(raw_content, bytes):
                raise TushareCapabilityProbeError(
                    "BaoStock raw evidence must use bytes"
                )
            replayed_baostock = _replay_baostock_daily_raw(
                daily_call.parameters,
                cross_requested_at,
                cross_completed_at,
                raw_content,
            )
            replayed_records = [dict(item) for item in replayed_baostock]
            normalized_baostock = replayed_records
            raw_payload = {
                "provider_id": "baostock",
                "dataset": "daily_bar",
                "parameters": dict(daily_call.parameters),
                "requested_at": cross_requested_at.isoformat(),
                "completed_at": cross_completed_at.isoformat(),
                "records": normalized_baostock,
                "provider_raw_base64": base64.b64encode(raw_content).decode("ascii"),
                "provider_raw_sha256": sha256_bytes(raw_content),
            }
            raw_bytes = canonical_json_bytes(raw_payload)
            _guard_bytes(raw_bytes, token)
            relative_path = "raw/cross_validation/baostock_daily.json"
            if relative_path in pending_raw_artifacts:
                raise TushareCapabilityProbeError(
                    "duplicate pending raw artifact path"
                )
            pending_raw_artifacts[relative_path] = raw_bytes
            raw_sha256 = sha256_bytes(raw_bytes)
            raw_artifacts.append(
                {
                    "provider_id": "baostock",
                    "dataset": "daily_bar",
                    "call_index": 1,
                    "path": relative_path,
                    "sha256": raw_sha256,
                }
            )
            cross_validation = compare_daily_samples(daily_call.rows, normalized_baostock)
            cross_validation.update(
                {
                    "requested_at": cross_requested_at.isoformat(),
                    "completed_at": cross_completed_at.isoformat(),
                    "tushare_raw_path": daily_call.raw_relative_path,
                    "tushare_raw_sha256": daily_call.raw_sha256,
                    "baostock_raw_path": relative_path,
                    "baostock_raw_sha256": raw_sha256,
                }
            )
        except Exception as exc:
            cross_validation = _cross_validation_not_configured(
                "BaoStock daily sample was unavailable",
                _sanitized_error(exc, token),
            )

    completed_at = _aware_now(clock)
    sealed_cross_validation = _scrub_value(cross_validation, token)
    comparison_payload_sha256 = sha256_bytes(
        canonical_json_bytes(sealed_cross_validation)
    )
    daily_raw_artifact = next(
        (
            item
            for item in raw_artifacts
            if item.get("provider_id") == "tushare"
            and item.get("endpoint") == "daily"
        ),
        None,
    )
    tushare_daily_raw_path = (
        str(daily_raw_artifact["path"])
        if isinstance(daily_raw_artifact, Mapping)
        else None
    )
    cross_request_count = request_count - sum(
        item.request_count for item in endpoint_results
    )
    if sealed_cross_validation.get("status") in {
        "compared_no_threshold",
        "compared_no_overlap_no_threshold",
    }:
        outcome_status = "compared"
        outcome_kwargs = {
            "tushare_raw_path": tushare_daily_raw_path,
            "baostock_raw_path": sealed_cross_validation.get("baostock_raw_path"),
            "baostock_raw_sha256": sealed_cross_validation.get(
                "baostock_raw_sha256"
            ),
        }
    elif cross_request_count == 1:
        outcome_status = "failed"
        outcome_kwargs = {
            "tushare_raw_path": tushare_daily_raw_path,
            "failure_code": sealed_cross_validation.get("failure_code"),
        }
    else:
        outcome_status = "not_attempted"
        outcome_kwargs = {
            "tushare_raw_path": tushare_daily_raw_path,
            "not_attempted_reason": (
                "global_stop"
                if global_stop
                else "daily_not_passed"
                if daily_call is None
                else "reserve_unavailable"
            ),
        }
    cross_validation_outcome = build_cross_validation_outcome(
        endpoint_results,
        status=outcome_status,
        comparison_payload_sha256=comparison_payload_sha256,
        **outcome_kwargs,
    )
    manifest = _manifest(
        run_id=identifier,
        plan_sha256=plan_sha256,
        raw_artifacts=raw_artifacts,
        cross_validation=sealed_cross_validation,
        request_count=request_count,
    )
    manifest_bytes = canonical_json_bytes(manifest)
    _guard_bytes(manifest_bytes, token)
    manifest_sha256 = sha256_bytes(manifest_bytes)
    git, _ = _call_with_suppressed_sdk_output(
        git_metadata_loader,
        secret=token,
        operation="probe Git metadata capture",
    )
    receipt = _build_receipt_for_run(
        run_id=identifier,
        started_at=started_at,
        completed_at=completed_at,
        sdk_version=str(_scrub_value(sdk_version, token)),
        credential_status=credential_status,
        config=config,
        git=git,
        endpoint_results=endpoint_results,
        request_count=request_count,
        rate_limit_events=rate_limit_events,
        manifest_sha256=manifest_sha256,
        probe_code_sha256=probe_code_sha256,
        cross_validation_outcome=cross_validation_outcome,
    )
    receipt_payload = _object_dict(receipt, "capability receipt")
    receipt_bytes = canonical_json_bytes(receipt_payload)
    _guard_bytes(receipt_bytes, token)
    publication_artifacts = {
        "plan.json": plan_bytes,
        **pending_raw_artifacts,
        "manifest.json": manifest_bytes,
        "receipt.json": receipt_bytes,
    }
    run_directory = _publish_probe_run(
        root=root,
        identifier=identifier,
        artifacts=publication_artifacts,
        repository_root=repository_root,
        config=config,
        secret=token,
        access_policy_path=access_policy_path,
        implementation_root=implementation_root,
        expected_plan=plan,
    )
    return {
        **receipt_payload,
        "run_directory": run_directory.relative_to(repository_root.resolve()).as_posix(),
        "manifest_sha256": manifest_sha256,
        "cross_validation": cross_validation,
    }


def _parse_cross_validation_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise TushareCapabilityProbeError(
            f"cross-validation {label} must be an ISO timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise TushareCapabilityProbeError(
            f"cross-validation {label} must be an ISO timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.isoformat() != value:
        raise TushareCapabilityProbeError(
            f"cross-validation {label} must be canonical and timezone-aware"
        )
    return parsed


def _verify_cross_validation_replay(
    cross_validation: Any,
    *,
    artifacts: Sequence[Mapping[str, Any]],
    artifact_bytes: Mapping[str, bytes],
    tushare_artifacts: Mapping[Any, Mapping[str, Any]],
    receipt: TushareCapabilityReceiptV1,
) -> None:
    if not isinstance(cross_validation, Mapping):
        raise TushareCapabilityProbeError(
            "manifest cross_validation must be an object"
        )
    status = cross_validation.get("status")
    outcome = receipt.cross_validation_outcome
    if outcome.comparison_payload_sha256 != sha256_bytes(
        canonical_json_bytes(dict(cross_validation))
    ):
        raise TushareCapabilityProbeError(
            "receipt cross-validation outcome payload hash mismatch"
        )
    compared_statuses = {
        "compared_no_threshold",
        "compared_no_overlap_no_threshold",
    }
    if outcome.status == "compared" and status not in compared_statuses:
        raise TushareCapabilityProbeError(
            "receipt cross-validation outcome cannot be downgraded from compared"
        )
    if outcome.status == "failed" and (
        status != "cross_validation_not_configured"
        or cross_validation.get("failure_code") != outcome.failure_code
    ):
        raise TushareCapabilityProbeError(
            "receipt failed cross-validation outcome differs from manifest"
        )
    if outcome.status == "not_attempted" and (
        status != "cross_validation_not_configured"
        or "failure_code" in cross_validation
        or "error" in cross_validation
    ):
        raise TushareCapabilityProbeError(
            "receipt not-attempted cross-validation outcome differs from manifest"
        )
    endpoint_request_count = sum(
        item.request_count for item in receipt.endpoint_results
    )
    reserve_request_count = receipt.request_count - endpoint_request_count
    if reserve_request_count not in {0, 1}:
        raise TushareCapabilityProbeError(
            "cross-validation request count differs from the single frozen reserve"
        )
    baostock_artifacts = [
        item for item in artifacts if item.get("provider_id") == "baostock"
    ]
    if status == "cross_validation_not_configured":
        if baostock_artifacts:
            raise TushareCapabilityProbeError(
                "unconfigured cross-validation unexpectedly has BaoStock raw evidence"
            )
        required = {
            "status",
            "dataset",
            "providers",
            "independent_batches",
            "records_merged",
            "missing_values_filled_across_providers",
            "automatic_difference_threshold",
            "threshold_status",
            "reason",
        }
        optional = {"failure_code", "error"}
        if not required <= set(cross_validation) or not set(cross_validation) <= (
            required | optional
        ):
            raise TushareCapabilityProbeError(
                "unconfigured cross-validation fields are invalid"
            )
        if set(cross_validation) & optional not in (set(), optional):
            raise TushareCapabilityProbeError(
                "unconfigured cross-validation failure evidence is incomplete"
            )
        if (
            cross_validation.get("dataset") != "daily_bar_small_sample"
            or cross_validation.get("providers") != ["tushare", "baostock"]
            or cross_validation.get("independent_batches") is not True
            or cross_validation.get("records_merged") is not False
            or cross_validation.get("missing_values_filled_across_providers")
            is not False
            or cross_validation.get("automatic_difference_threshold") is not None
            or cross_validation.get("threshold_status") != "not_configured"
            or not isinstance(cross_validation.get("reason"), str)
            or not str(cross_validation.get("reason")).strip()
        ):
            raise TushareCapabilityProbeError(
                "unconfigured cross-validation safety semantics are invalid"
            )
        has_failure_evidence = {
            "failure_code",
            "error",
        } <= set(cross_validation)
        if has_failure_evidence and (
            not isinstance(cross_validation.get("failure_code"), str)
            or not str(cross_validation.get("failure_code")).strip()
            or not isinstance(cross_validation.get("error"), str)
            or not str(cross_validation.get("error")).strip()
        ):
            raise TushareCapabilityProbeError(
                "unconfigured cross-validation failure evidence is invalid"
            )
        if (reserve_request_count == 1) is not has_failure_evidence:
            raise TushareCapabilityProbeError(
                "unconfigured cross-validation request count lacks matching failure evidence"
            )
        return
    if status not in compared_statuses:
        raise TushareCapabilityProbeError(
            "manifest cross-validation status is unsupported"
        )
    if len(baostock_artifacts) != 1:
        raise TushareCapabilityProbeError(
            "compared cross-validation requires exactly one BaoStock raw artifact"
        )
    if reserve_request_count != 1:
        raise TushareCapabilityProbeError(
            "compared cross-validation must consume exactly one reserved request"
        )
    passed_daily: list[tuple[int, EndpointResultV1, Mapping[str, Any]]] = []
    for index, result in enumerate(receipt.endpoint_results):
        if result.endpoint.value != "daily" or result.status != "passed":
            continue
        artifact = tushare_artifacts.get(index)
        if isinstance(artifact, Mapping):
            passed_daily.append((index, result, artifact))
    if len(passed_daily) != 1:
        raise TushareCapabilityProbeError(
            "compared cross-validation requires one passed Tushare daily sample"
        )
    _, daily_result, daily_artifact = passed_daily[0]
    baostock_artifact = baostock_artifacts[0]
    if (
        baostock_artifact.get("dataset") != "daily_bar"
        or baostock_artifact.get("call_index") != 1
    ):
        raise TushareCapabilityProbeError(
            "BaoStock raw artifact identity is invalid"
        )
    daily_path = str(daily_artifact.get("path") or "")
    baostock_path = str(baostock_artifact.get("path") or "")
    if (
        outcome.tushare_raw_path != daily_path
        or outcome.tushare_raw_sha256 != daily_artifact.get("sha256")
        or outcome.baostock_raw_path != baostock_path
        or outcome.baostock_raw_sha256 != baostock_artifact.get("sha256")
    ):
        raise TushareCapabilityProbeError(
            "receipt cross-validation outcome raw binding mismatch"
        )
    tushare_raw = _strict_object(
        artifact_bytes[daily_path],
        "Tushare daily cross-validation raw",
    )
    baostock_raw = _strict_object(
        artifact_bytes[baostock_path],
        "BaoStock daily cross-validation raw",
    )
    if set(baostock_raw) != {
        "provider_id",
        "dataset",
        "parameters",
        "requested_at",
        "completed_at",
        "records",
        "provider_raw_base64",
        "provider_raw_sha256",
    }:
        raise TushareCapabilityProbeError(
            "BaoStock cross-validation raw envelope is malformed"
        )
    records = baostock_raw.get("records")
    encoded_provider_raw = baostock_raw.get("provider_raw_base64")
    try:
        provider_raw = base64.b64decode(encoded_provider_raw, validate=True)
    except (TypeError, ValueError, binascii.Error) as exc:
        raise TushareCapabilityProbeError(
            "BaoStock provider raw bytes are not canonical base64"
        ) from exc
    if (
        baostock_raw.get("provider_id") != "baostock"
        or baostock_raw.get("dataset") != "daily_bar"
        or baostock_raw.get("parameters")
        != dict(daily_result.sanitized_parameters)
        or not isinstance(records, list)
        or any(not isinstance(item, Mapping) for item in records)
        or re.fullmatch(
            r"[0-9a-f]{64}",
            str(baostock_raw.get("provider_raw_sha256") or ""),
        )
        is None
        or base64.b64encode(provider_raw).decode("ascii") != encoded_provider_raw
        or sha256_bytes(provider_raw)
        != baostock_raw.get("provider_raw_sha256")
    ):
        raise TushareCapabilityProbeError(
            "BaoStock cross-validation raw semantics are invalid"
        )
    requested = _parse_cross_validation_timestamp(
        baostock_raw.get("requested_at"),
        "requested_at",
    )
    completed = _parse_cross_validation_timestamp(
        baostock_raw.get("completed_at"),
        "completed_at",
    )
    if completed < requested:
        raise TushareCapabilityProbeError(
            "cross-validation completed_at precedes requested_at"
        )
    try:
        replayed_records = [
            dict(item)
            for item in _replay_baostock_daily_raw(
                dict(daily_result.sanitized_parameters),
                requested,
                completed,
                provider_raw,
            )
        ]
    except Exception as exc:
        raise TushareCapabilityProbeError(
            "BaoStock provider raw bytes do not replay"
        ) from exc
    if canonical_json_bytes(records) != canonical_json_bytes(replayed_records):
        raise TushareCapabilityProbeError(
            "BaoStock envelope records differ from provider raw replay"
        )
    tushare_rows = tushare_raw.get("rows")
    if not isinstance(tushare_rows, list) or any(
        not isinstance(item, Mapping) for item in tushare_rows
    ):
        raise TushareCapabilityProbeError(
            "Tushare daily raw rows are malformed"
        )
    expected = compare_daily_samples(tushare_rows, replayed_records)
    expected.update(
        {
            "requested_at": baostock_raw["requested_at"],
            "completed_at": baostock_raw["completed_at"],
            "tushare_raw_path": daily_path,
            "tushare_raw_sha256": daily_artifact.get("sha256"),
            "baostock_raw_path": baostock_path,
            "baostock_raw_sha256": baostock_artifact.get("sha256"),
        }
    )
    if dict(cross_validation) != expected:
        raise TushareCapabilityProbeError(
            "manifest cross-validation differs from replayed daily samples"
        )


def verify_probe_run(
    run_directory: Path | str,
    *,
    config: ProbeConfig,
    secret: str = "",
    access_policy_path: Path | str = DEFAULT_PROVIDER_ACCESS_POLICY_PATH,
    implementation_root: Path | str = REPOSITORY_ROOT,
    _expected_plan: Mapping[str, Any] | None = None,
) -> TushareCapabilityReceiptV1:
    """Replay persisted hashes and the typed receipt without network access."""

    directory = _require_no_reparse_ancestors(
        Path(run_directory),
        "probe run directory",
    )
    if not directory.is_dir() or _is_reparse_entry(directory):
        raise TushareCapabilityProbeError("probe run directory is unavailable")
    plan_raw = (directory / "plan.json").read_bytes()
    manifest_raw = (directory / "manifest.json").read_bytes()
    receipt_raw = (directory / "receipt.json").read_bytes()
    for raw in (plan_raw, manifest_raw, receipt_raw):
        _guard_bytes(raw, secret)
    plan = _strict_object(plan_raw, "plan")
    manifest = _strict_object(manifest_raw, "manifest")
    if _expected_plan is None:
        policy = load_provider_access_policy(access_policy_path)
        _require_tushare_probe_policy(policy)
        expected_plan = {
            **build_plan(config, access_policy=policy),
            "probe_run_id": directory.name,
            "mode": "live_bounded_read_only",
        }
    else:
        expected_plan = dict(_expected_plan)
    if plan != expected_plan:
        raise TushareCapabilityProbeError(
            "persisted plan differs from current config and access policy"
        )
    expected_manifest_fields = {
        "schema_version",
        "probe_version",
        "probe_run_id",
        "status",
        "scope",
        "plan",
        "raw_artifacts",
        "cross_validation",
        "request_count",
        "formal_data_admission",
        "validated_storage_write",
        "market_data_batch_created",
        "records_merged_across_providers",
        "automatic_fallback",
        "experiment_v3_impact",
        "daily_signal_authority",
        "paper_eligibility",
        "trade_eligibility",
        "live_supported",
    }
    if set(manifest) != expected_manifest_fields:
        raise TushareCapabilityProbeError("manifest fields differ from the fixed contract")
    if (
        manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION
        or manifest.get("probe_version") != PROBE_VERSION
        or manifest.get("probe_run_id") != directory.name
        or plan.get("probe_run_id") != directory.name
        or manifest.get("status") != "raw_evidence_sealed_receipt_pending"
        or manifest.get("scope") != "capability_probe_only_not_admitted"
        or manifest.get("formal_data_admission") is not False
        or manifest.get("validated_storage_write") is not False
        or manifest.get("market_data_batch_created") is not False
        or manifest.get("records_merged_across_providers") is not False
        or manifest.get("automatic_fallback") is not False
        or manifest.get("experiment_v3_impact") != "none"
        or manifest.get("daily_signal_authority") != "none"
        or manifest.get("paper_eligibility") is not False
        or manifest.get("trade_eligibility") is not False
        or manifest.get("live_supported") is not False
    ):
        raise TushareCapabilityProbeError("manifest safety semantics are invalid")
    if manifest.get("plan") != {
        "path": "plan.json",
        "sha256": sha256_bytes(plan_raw),
    }:
        raise TushareCapabilityProbeError("manifest plan binding mismatch")
    declared_paths = {"plan.json", "manifest.json", "receipt.json"}
    artifacts = manifest.get("raw_artifacts")
    if not isinstance(artifacts, list):
        raise TushareCapabilityProbeError("manifest raw_artifacts must be an array")
    artifact_paths: set[str] = set()
    artifact_bytes: dict[str, bytes] = {}
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            raise TushareCapabilityProbeError("manifest raw artifact must be an object")
        relative = str(artifact.get("path") or "")
        provider_id = artifact.get("provider_id")
        expected_artifact_fields = (
            {
                "provider_id",
                "endpoint",
                "call_index",
                "endpoint_result_index",
                "sanitized_parameters",
                "path",
                "sha256",
            }
            if provider_id == "tushare"
            else {
                "provider_id",
                "dataset",
                "call_index",
                "path",
                "sha256",
            }
            if provider_id == "baostock"
            else set()
        )
        if not expected_artifact_fields or set(artifact) != expected_artifact_fields:
            raise TushareCapabilityProbeError(
                "manifest raw artifact fields differ from the fixed contract"
            )
        relative_path = Path(relative)
        if (
            not relative.startswith("raw/")
            or "\\" in relative
            or relative_path.is_absolute()
            or ".." in relative_path.parts
        ):
            raise TushareCapabilityProbeError(
                "manifest raw artifact path is unsafe"
            )
        if relative in artifact_paths:
            raise TushareCapabilityProbeError("manifest contains duplicate artifact paths")
        artifact_paths.add(relative)
        declared = directory / relative
        _require_no_reparse_ancestors(declared, "manifest raw artifact")
        candidate = declared.resolve()
        try:
            candidate.relative_to(directory.resolve())
        except ValueError as exc:
            raise TushareCapabilityProbeError("manifest artifact escapes the run") from exc
        if _is_reparse_entry(declared) or not candidate.is_file():
            raise TushareCapabilityProbeError("manifest raw artifact is unavailable")
        raw = candidate.read_bytes()
        artifact_bytes[relative] = raw
        _guard_bytes(raw, secret)
        _strict_object(raw, f"raw artifact {relative}")
        if sha256_bytes(raw) != artifact.get("sha256"):
            raise TushareCapabilityProbeError("manifest raw artifact hash mismatch")
        declared_paths.add(relative.replace("\\", "/"))
    discovered = tuple(directory.rglob("*"))
    if any(_is_reparse_entry(path) for path in discovered):
        raise TushareCapabilityProbeError(
            "probe run contains a symbolic link, junction, or reparse point"
        )
    actual_paths = {
        path.relative_to(directory).as_posix()
        for path in discovered
        if path.is_file()
    }
    if actual_paths != declared_paths:
        raise TushareCapabilityProbeError("probe run artifact set differs from manifest")
    receipt = verify_capability_receipt(
        receipt_raw,
        config=config,
    )
    receipt_payload = _object_dict(receipt, "capability receipt")
    if receipt_payload.get("raw_evidence_manifest_sha256") != sha256_bytes(manifest_raw):
        raise TushareCapabilityProbeError("receipt manifest binding mismatch")
    if (
        receipt.probe_run_id != directory.name
        or manifest.get("request_count") != receipt.request_count
    ):
        raise TushareCapabilityProbeError("receipt run metadata differs from manifest")
    if receipt.probe_code_sha256 != compute_probe_implementation_bundle_sha256(
        implementation_root
    ):
        raise TushareCapabilityProbeError(
            "receipt probe_code_sha256 differs from the current implementation bundle"
        )
    tushare_artifacts = {
        item.get("endpoint_result_index"): item
        for item in artifacts
        if item.get("provider_id") == "tushare"
    }
    if len(tushare_artifacts) != sum(
        item.get("provider_id") == "tushare" for item in artifacts
    ):
        raise TushareCapabilityProbeError("Tushare raw artifact indices are duplicated")
    for index, result in enumerate(receipt.endpoint_results):
        artifact = tushare_artifacts.get(index)
        if result.raw_payload_sha256 is None:
            if artifact is not None:
                raise TushareCapabilityProbeError(
                    "failed endpoint unexpectedly has a raw artifact"
                )
            continue
        if (
            not isinstance(artifact, Mapping)
            or artifact.get("endpoint") != result.endpoint.value
            or artifact.get("sanitized_parameters")
            != dict(result.sanitized_parameters)
            or artifact.get("sha256") != result.raw_payload_sha256
        ):
            raise TushareCapabilityProbeError(
                "endpoint result raw hash differs from manifest evidence"
            )
        replay_endpoint_raw(
            config.spec_for(result.endpoint),
            artifact_bytes[str(artifact["path"])],
            expected_result=result,
        )
    if set(tushare_artifacts) != {
        index
        for index, result in enumerate(receipt.endpoint_results)
        if result.raw_payload_sha256 is not None
    }:
        raise TushareCapabilityProbeError("manifest contains an unbound Tushare artifact")
    _verify_cross_validation_replay(
        manifest.get("cross_validation"),
        artifacts=artifacts,
        artifact_bytes=artifact_bytes,
        tushare_artifacts=tushare_artifacts,
        receipt=receipt,
    )
    return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--plan", action="store_true", help="Print an offline plan (default).")
    modes.add_argument("--live", action="store_true", help="Allow the bounded read-only probe.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument(
        "--provider-access-policy",
        type=Path,
        default=DEFAULT_PROVIDER_ACCESS_POLICY_PATH,
    )
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_probe_config(args.config)
        if not args.live:
            result = build_plan(config, access_policy_path=args.provider_access_policy)
            rendered = canonical_json_bytes(result) + b"\n"
            sys.stdout.write(rendered.decode("utf-8"))
            return 0
        result = run_live_probe(
            config,
            args.output_root,
            access_policy_path=args.provider_access_policy,
        )
        rendered = canonical_json_bytes(result) + b"\n"
        token = _read_tushare_token()
        _guard_bytes(rendered, token)
        sys.stdout.write(rendered.decode("utf-8"))
        return 0 if result.get("status") in {"passed", "partial"} else 1
    except Exception as exc:
        token = _read_tushare_token() if args.live else ""
        message = str(exc)
        if token:
            message = message.replace(token, "[REDACTED]")
        error = {
            "status": "failed",
            "scope": "capability_probe_only_not_admitted",
            "failure_code": getattr(exc, "code", "tushare_capability_probe_failed"),
            "error": safe_error_text(message),
            "formal_data_admission": False,
            "trade_eligibility": False,
            "live_supported": False,
        }
        rendered = canonical_json_bytes(error) + b"\n"
        _guard_bytes(rendered, token)
        sys.stdout.write(rendered.decode("utf-8"))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
