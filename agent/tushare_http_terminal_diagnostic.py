"""Run exactly one crash-replayable Tushare ``trade_cal`` HTTP diagnostic.

This module is intentionally narrower than the earlier SDK-versus-HTTP
diagnostic.  Its public CLI has only three modes: an offline plan, one fixed
live HTTP request, and an offline replay.  It never imports the Tushare SDK,
never persists a credential or raw response, and cannot call ``daily`` or the
22-endpoint capability probe.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import threading
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence, TypeVar

from agent.tushare_capability_probe import (
    GitMetadata,
    _default_git_metadata,
    _directory_identity,
    _endpoint_name,
    _guard_bytes,
    _flush_process_stdio,
    _parameter_sets,
    _path_inside_repository,
    _read_tushare_token,
    _redirect_windows_standard_handles,
    _require_directory_identity,
    _require_no_reparse_ancestors,
    _require_tushare_probe_policy,
    _restore_windows_standard_handles,
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
    classify_message_category,
    normalize_upstream_code,
)
from research.market_data.tushare_http_terminal import (
    OFFICIAL_HTTPS_API_URL,
    TushareHttpDiagnosticEventV1,
    TushareHttpTerminalDiagnosticReceiptV1,
    append_http_diagnostic_event,
    build_http_run_created_event,
    build_http_terminal_diagnostic_receipt,
    verify_http_diagnostic_event,
    verify_http_terminal_diagnostic_receipt,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPOSITORY_ROOT / "configs" / "tushare_capability_probe.v1.json"
DEFAULT_RUN_DIRECTORY = (
    REPOSITORY_ROOT
    / "data"
    / "tmp"
    / "tushare-capability"
    / "http-terminal-once"
)
OFFLINE_PLAN_VERSION = "tushare-http-terminal-diagnostic-plan.v1"
IMPLEMENTATION_BUNDLE_VERSION = "tushare-http-terminal-diagnostic-bundle.v1"
ENDPOINT = "trade_cal"
CHANNEL = "http"
MAX_REQUESTS = 1
REQUEST_TIMEOUT_SECONDS = 30
MAXIMUM_RESPONSE_BYTES = 1_048_576
RECEIPT_NAME = "diagnostic_receipt.json"
EVENT_DIRECTORY_NAME = "journal"
REQUEST_ID = "http-trade-cal-request-1"
_CREDENTIAL_ENVELOPE = re.compile(r"[A-Za-z0-9_-]{20,256}")
_EVENT_FILE = re.compile(r"^(?P<sequence>[0-9]{3})_(?P<kind>[A-Z_]+)\.json$")
_ROUND_LOCK_NAME = ".http-terminal-once.lock"
_OUTPUT_DISCARD_LOCK = threading.RLock()
_T = TypeVar("_T")

_IMPLEMENTATION_BUNDLE_PATHS = (
    "agent/tushare_http_terminal_diagnostic.py",
    "research/market_data/tushare_http_terminal.py",
    "research/market_data/tushare_diagnostic.py",
    "research/market_data/tushare_capability.py",
    "research/market_data/provider_access.py",
    "schemas/tushare_http_diagnostic_event.v1.json",
    "schemas/tushare_http_terminal_diagnostic_receipt.v1.json",
    "schemas/provider_access_policy.v1.json",
    "configs/tushare_capability_probe.v1.json",
    "configs/provider_access.v1.json",
)


class TushareHttpTerminalRunnerError(RuntimeError):
    """Raised when the one-request diagnostic cannot be safely continued."""


class TushareHttpTerminalRoundBusyError(TushareHttpTerminalRunnerError):
    """Raised when another process currently owns the authorized round."""


class TushareHttpTerminalRoundClaimedError(TushareHttpTerminalRunnerError):
    """Raised when a second live invocation targets an already claimed round."""


@dataclass(frozen=True, slots=True)
class PublishedHttpTerminalDiagnostic:
    receipt: TushareHttpTerminalDiagnosticReceiptV1
    receipt_path: Path
    receipt_file_sha256: str
    replayed: bool


ArtifactWriter = Callable[[Path, bytes], None]
FaultInjector = Callable[[str], None]


def _no_fault(_point: str) -> None:
    return None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TushareHttpTerminalRunnerError(
            "diagnostic clock must return a timezone-aware datetime"
        )
    return value


def _credential_passes_local_preflight(value: Any) -> bool:
    return (
        type(value) is str
        and value == value.strip()
        and _CREDENTIAL_ENVELOPE.fullmatch(value) is not None
    )


@contextmanager
def _exclusive_authorized_round(lock_root: Path) -> Iterator[None]:
    """Serialize live and replay across processes for the fixed round."""

    lock_root.mkdir(parents=True, exist_ok=True)
    _require_no_reparse_ancestors(lock_root, "HTTP diagnostic lock root")
    identity = _directory_identity(lock_root)
    lock_path = lock_root / _ROUND_LOCK_NAME
    handle = lock_path.open("a+b")
    acquired = False
    unlock: Callable[[], None] | None = None
    try:
        _require_directory_identity(lock_root, identity)
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise TushareHttpTerminalRoundBusyError(
                    "another process owns the authorized HTTP diagnostic round"
                ) from exc

            def unlock() -> None:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise TushareHttpTerminalRoundBusyError(
                    "another process owns the authorized HTTP diagnostic round"
                ) from exc

            def unlock() -> None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

        acquired = True
        _require_directory_identity(lock_root, identity)
        yield
    finally:
        if acquired and unlock is not None:
            try:
                unlock()
            finally:
                handle.close()
        else:
            handle.close()


def _call_with_discarded_process_output(callback: Callable[[], _T]) -> _T:
    """Discard Python and process stdout/stderr without a disk-backed capture."""

    with _OUTPUT_DISCARD_LOCK, open(os.devnull, "w", encoding="utf-8") as text_null, open(
        os.devnull, "wb"
    ) as binary_null:
        original_stdout = sys.stdout
        original_stderr = sys.stderr
        _flush_process_stdio(original_stdout, original_stderr)
        saved_stdout = os.dup(1)
        saved_stderr = os.dup(2)
        windows_handle_state = None
        try:
            os.dup2(binary_null.fileno(), 1)
            os.dup2(binary_null.fileno(), 2)
            windows_handle_state = _redirect_windows_standard_handles(
                binary_null.fileno(), binary_null.fileno()
            )
            with redirect_stdout(text_null), redirect_stderr(text_null):
                return callback()
        finally:
            _flush_process_stdio(original_stdout, original_stderr)
            _restore_windows_standard_handles(windows_handle_state)
            os.dup2(saved_stdout, 1)
            os.dup2(saved_stderr, 2)
            os.close(saved_stdout)
            os.close(saved_stderr)


def _resolve_repository_file(path: Path | str, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = REPOSITORY_ROOT / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(REPOSITORY_ROOT.resolve())
    except ValueError as exc:
        raise TushareHttpTerminalRunnerError(
            f"{label} must stay inside the repository"
        ) from exc
    if not resolved.is_file():
        raise TushareHttpTerminalRunnerError(f"{label} is unavailable")
    return resolved


def _load_fixed_semantics(
    *,
    config_path: Path | str = DEFAULT_CONFIG_PATH,
    access_policy_path: Path | str = DEFAULT_PROVIDER_ACCESS_POLICY_PATH,
) -> tuple[dict[str, str], tuple[str, ...]]:
    config_file = _resolve_repository_file(config_path, "probe config")
    policy_file = _resolve_repository_file(access_policy_path, "provider policy")
    config: ProbeConfig = load_probe_config(config_file)
    policy = load_provider_access_policy(policy_file)
    _require_tushare_probe_policy(policy)
    spec = config.spec_for(ENDPOINT)
    parameter_sets = _parameter_sets(spec)
    if (
        _endpoint_name(spec) != ENDPOINT
        or _sdk_method_name(spec) != ENDPOINT
        or int(spec.max_calls) != MAX_REQUESTS
        or len(parameter_sets) != MAX_REQUESTS
    ):
        raise TushareHttpTerminalRunnerError(
            "trade_cal semantics differ from the fixed one-request contract"
        )
    parameters = dict(parameter_sets[0])
    if not parameters or any(
        type(key) is not str or type(value) is not str
        for key, value in parameters.items()
    ):
        raise TushareHttpTerminalRunnerError(
            "trade_cal parameters are not canonical strings"
        )
    fields = tuple(str(value) for value in spec.required_fields)
    if not fields:
        raise TushareHttpTerminalRunnerError("trade_cal required fields are empty")
    return parameters, fields


def build_http_terminal_plan(
    *,
    config_path: Path | str = DEFAULT_CONFIG_PATH,
    access_policy_path: Path | str = DEFAULT_PROVIDER_ACCESS_POLICY_PATH,
) -> dict[str, Any]:
    """Return the frozen plan without reading a credential or touching network."""

    parameters, fields = _load_fixed_semantics(
        config_path=config_path,
        access_policy_path=access_policy_path,
    )
    return {
        "schema_version": OFFLINE_PLAN_VERSION,
        "mode": "plan_offline_no_credentials",
        "scope": "single_http_terminal_diagnostic_only_not_admitted",
        "provider_id": "tushare",
        "endpoint": ENDPOINT,
        "channel": CHANNEL,
        "transport_target": OFFICIAL_HTTPS_API_URL,
        "semantic_parameters": parameters,
        "expected_fields": list(fields),
        "max_requests": MAX_REQUESTS,
        "planned_request_count": MAX_REQUESTS,
        "automatic_retries_allowed": False,
        "redirects_allowed": False,
        "credential_accessed": False,
        "sdk_imported": False,
        "network_accessed": False,
        "daily_allowed": False,
        "full_capability_probe_allowed": False,
        "raw_response_persisted": False,
        "formal_data_admission": False,
        "experiment_v3_impact": "none",
        "daily_signal_authority": "none",
        "paper_eligibility": False,
        "trade_eligibility": False,
        "automatic_order_submission": False,
        "live_supported": False,
    }


def compute_http_terminal_implementation_sha256(
    implementation_root: Path | str = REPOSITORY_ROOT,
) -> str:
    root = Path(implementation_root).resolve()
    files: list[dict[str, str]] = []
    for relative in _IMPLEMENTATION_BUNDLE_PATHS:
        path = root / relative
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise TushareHttpTerminalRunnerError(
                "HTTP diagnostic implementation bundle is incomplete"
            ) from exc
        files.append({"path": relative, "sha256": sha256_bytes(raw)})
    return sha256_bytes(
        canonical_json_bytes(
            {"bundle_version": IMPLEMENTATION_BUNDLE_VERSION, "files": files}
        )
    )


def _new_no_retry_session() -> Any:
    import importlib

    requests = importlib.import_module("requests")
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(max_retries=0)
    session.mount("https://", adapter)
    session.max_redirects = 0
    return session


def _read_bounded_response(response: Any) -> bytes:
    iterator = getattr(response, "iter_content", None)
    if not callable(iterator):
        raise TushareHttpTerminalRunnerError(
            "HTTP response does not support bounded streaming"
        )
    chunks: list[bytes] = []
    total = 0
    for chunk in iterator(chunk_size=65_536):
        if not isinstance(chunk, (bytes, bytearray)):
            raise TushareHttpTerminalRunnerError(
                "HTTP response emitted a non-byte chunk"
            )
        if not chunk:
            continue
        total += len(chunk)
        if total > MAXIMUM_RESPONSE_BYTES:
            raise TushareHttpTerminalRunnerError(
                "HTTP response exceeded the diagnostic size limit"
            )
        chunks.append(bytes(chunk))
    return b"".join(chunks)


def _classify_response(
    response: Any,
    *,
    expected_fields: Sequence[str],
    token: str,
) -> tuple[int, int | None, str]:
    status = getattr(response, "status_code", None)
    if type(status) is not int or not 100 <= status <= 599:
        raise TushareHttpTerminalRunnerError("HTTP response status is invalid")
    code: int | None = None
    message: str | None = None
    category = "unknown"
    try:
        content = _read_bounded_response(response)
        _guard_bytes(content, token)
        value = strict_json_loads(content, label="Tushare HTTP diagnostic response")
        if not isinstance(value, Mapping):
            raise TushareHttpTerminalRunnerError("HTTP response root is not an object")
        code = normalize_upstream_code(value.get("code"))
        raw_message = value.get("msg")
        message = raw_message if type(raw_message) is str else None
        category = classify_message_category(
            upstream_code=code,
            http_status=status,
            message=message,
            error=None,
            transport_status="response_received",
            channel=CHANNEL,
            secret=token,
        )
        if code == 0:
            data = value.get("data")
            fields = data.get("fields") if isinstance(data, Mapping) else None
            items = data.get("items") if isinstance(data, Mapping) else None
            if (
                not isinstance(fields, list)
                or not isinstance(items, list)
                or any(type(field) is not str for field in fields)
                or not set(expected_fields).issubset(set(fields))
                or any(
                    not isinstance(item, list) or len(item) != len(fields)
                    for item in items
                )
            ):
                category = "unknown"
    except Exception as exc:
        category = classify_message_category(
            upstream_code=code,
            http_status=status,
            message=message,
            error=exc,
            transport_status="response_received",
            channel=CHANNEL,
            secret=token,
        )
    if category not in {
        "success",
        "permission",
        "rate_limit",
        "authentication_account",
        "invalid_parameter",
        "server_internal",
        "unknown",
    }:
        category = "unknown"
    return status, code, category


def _event_filename(event: TushareHttpDiagnosticEventV1) -> str:
    return f"{event.sequence:03d}_{event.event_type}.json"


def _default_artifact_writer(path: Path, content: bytes) -> None:
    _write_create_only(path, content)


def _write_event(
    run_directory: Path,
    event: TushareHttpDiagnosticEventV1,
    *,
    token: str,
    writer: ArtifactWriter,
) -> None:
    content = canonical_json_bytes(event.to_dict())
    _guard_bytes(content, token)
    writer(run_directory / EVENT_DIRECTORY_NAME / _event_filename(event), content)


def _load_event_chain(run_directory: Path) -> tuple[TushareHttpDiagnosticEventV1, ...]:
    journal = run_directory / EVENT_DIRECTORY_NAME
    if not journal.is_dir():
        raise TushareHttpTerminalRunnerError("HTTP diagnostic journal is unavailable")
    paths = sorted(path for path in journal.iterdir() if path.is_file())
    events: list[TushareHttpDiagnosticEventV1] = []
    for path in paths:
        match = _EVENT_FILE.fullmatch(path.name)
        if match is None:
            if path.name.startswith(".probe-"):
                continue
            raise TushareHttpTerminalRunnerError(
                "HTTP diagnostic journal contains an unexpected artifact"
            )
        event = verify_http_diagnostic_event(path.read_bytes())
        if int(match.group("sequence")) != event.sequence or match.group("kind") != event.event_type:
            raise TushareHttpTerminalRunnerError(
                "HTTP diagnostic event filename differs from its content"
            )
        events.append(event)
    if not events:
        raise TushareHttpTerminalRunnerError("HTTP diagnostic journal is empty")
    # The domain append operation validates the full prefix.  Rebuilding each
    # transition also rejects gaps, reordered events, and changed hash links.
    rebuilt: list[TushareHttpDiagnosticEventV1] = [events[0]]
    if events[0].event_type != "RUN_CREATED":
        raise TushareHttpTerminalRunnerError("HTTP diagnostic lacks RUN_CREATED")
    for event in events[1:]:
        expected = append_http_diagnostic_event(
            tuple(rebuilt),
            event_type=event.event_type,
            recorded_at=event.recorded_at,
            http_status=event.http_status,
            upstream_code=event.upstream_code,
            sanitized_message_category=event.sanitized_message_category,
        )
        if expected != event:
            raise TushareHttpTerminalRunnerError(
                "HTTP diagnostic event chain cannot be replayed"
            )
        rebuilt.append(event)
    return tuple(events)


def _safe_run_directory(
    run_directory: Path | str,
    *,
    repository_root: Path,
) -> Path:
    resolved = _path_inside_repository(
        run_directory,
        repository_root=repository_root,
        label="HTTP diagnostic run directory",
    )
    _require_no_reparse_ancestors(resolved, "HTTP diagnostic run directory")
    return resolved


def _publish_terminal_receipt(
    *,
    run_directory: Path,
    events: Sequence[TushareHttpDiagnosticEventV1],
    clock: Callable[[], datetime],
    token: str,
    writer: ArtifactWriter,
    replayed: bool,
) -> PublishedHttpTerminalDiagnostic:
    chain = tuple(events)
    if chain[-1].event_type != "TERMINAL":
        terminal = append_http_diagnostic_event(
            chain,
            event_type="TERMINAL",
            recorded_at=_aware_now(clock),
        )
        _write_event(run_directory, terminal, token=token, writer=writer)
        chain = (*chain, terminal)
    receipt_path = run_directory / RECEIPT_NAME
    if receipt_path.exists():
        existing = receipt_path.read_bytes()
        verified = verify_http_terminal_diagnostic_receipt(existing)
        if verified.event_chain != chain:
            raise TushareHttpTerminalRunnerError(
                "existing terminal receipt differs from journal replay"
            )
        content = existing
        receipt = verified
    else:
        receipt = build_http_terminal_diagnostic_receipt(
            events=chain,
        )
        content = canonical_json_bytes(receipt.to_dict())
        _guard_bytes(content, token)
        writer(receipt_path, content)
        receipt = verify_http_terminal_diagnostic_receipt(receipt_path.read_bytes())
    return PublishedHttpTerminalDiagnostic(
        receipt=receipt,
        receipt_path=receipt_path,
        receipt_file_sha256=hashlib.sha256(content).hexdigest(),
        replayed=replayed,
    )


def _run_live_http_terminal_diagnostic_locked(
    *,
    token: str,
    run_directory: Path | str = DEFAULT_RUN_DIRECTORY,
    repository_root: Path | str = REPOSITORY_ROOT,
    config_path: Path | str = DEFAULT_CONFIG_PATH,
    access_policy_path: Path | str = DEFAULT_PROVIDER_ACCESS_POLICY_PATH,
    implementation_root: Path | str = REPOSITORY_ROOT,
    clock: Callable[[], datetime] = _utc_now,
    session_factory: Callable[[], Any] = _new_no_retry_session,
    git_metadata_provider: Callable[[], GitMetadata] = _default_git_metadata,
    artifact_writer: ArtifactWriter = _default_artifact_writer,
    fault_injector: FaultInjector = _no_fault,
) -> PublishedHttpTerminalDiagnostic:
    """Claim the fixed round, issue at most one HTTP request, and terminalize."""

    repository = Path(repository_root).resolve()
    parameters, expected_fields = _load_fixed_semantics(
        config_path=config_path,
        access_policy_path=access_policy_path,
    )
    diagnostic_code_sha256 = compute_http_terminal_implementation_sha256(
        implementation_root
    )
    git_metadata = git_metadata_provider()
    directory = _safe_run_directory(run_directory, repository_root=repository)
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory = _safe_run_directory(directory, repository_root=repository)
    try:
        directory.mkdir()
    except FileExistsError as exc:
        raise TushareHttpTerminalRoundClaimedError(
            "the authorized one-request HTTP diagnostic round is already claimed"
        ) from exc
    _require_no_reparse_ancestors(directory, "HTTP diagnostic run directory")
    identity = _directory_identity(directory)
    (directory / EVENT_DIRECTORY_NAME).mkdir()
    _require_directory_identity(directory, identity)

    started_at = _aware_now(clock)
    run_id = started_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    created = build_http_run_created_event(
        diagnostic_run_id=run_id,
        request_id=REQUEST_ID,
        recorded_at=started_at,
        runtime_semantic_parameters=parameters,
        diagnostic_code_sha256=diagnostic_code_sha256,
        git_commit=git_metadata.commit,
        git_worktree_status=git_metadata.worktree_status,
        expected_fields=expected_fields,
    )
    _write_event(directory, created, token=token, writer=artifact_writer)
    events: tuple[TushareHttpDiagnosticEventV1, ...] = (created,)

    if not _credential_passes_local_preflight(token):
        return _publish_terminal_receipt(
            run_directory=directory,
            events=events,
            clock=clock,
            token=token,
            writer=artifact_writer,
            replayed=False,
        )

    session: Any = None
    try:
        fault_injector("before_request_reserved")
        reserved = append_http_diagnostic_event(
            events,
            event_type="REQUEST_RESERVED",
            recorded_at=_aware_now(clock),
        )
        _write_event(directory, reserved, token=token, writer=artifact_writer)
        events = (*events, reserved)
        fault_injector("after_request_reserved")

        session = session_factory()
        payload = {
            "api_name": ENDPOINT,
            "token": token,
            "params": dict(parameters),
            "fields": "",
        }
        started = append_http_diagnostic_event(
            events,
            event_type="NETWORK_CALL_STARTED",
            recorded_at=_aware_now(clock),
        )
        _write_event(directory, started, token=token, writer=artifact_writer)
        events = (*events, started)

        persisted_before_send = _load_event_chain(directory)
        if (
            persisted_before_send != events
            or persisted_before_send[-1].event_type != "NETWORK_CALL_STARTED"
            or (directory / RECEIPT_NAME).exists()
        ):
            raise TushareHttpTerminalRunnerError(
                "persisted journal head changed before the HTTP request"
            )

        response = _call_with_discarded_process_output(
            lambda: session.post(
                OFFICIAL_HTTPS_API_URL,
                json=payload,
                timeout=REQUEST_TIMEOUT_SECONDS,
                allow_redirects=False,
                verify=True,
                stream=True,
            )
        )
        try:
            http_status, upstream_code, category = _classify_response(
                response,
                expected_fields=expected_fields,
                token=token,
            )
        finally:
            close_response = getattr(response, "close", None)
            if callable(close_response):
                close_response()
        received = append_http_diagnostic_event(
            events,
            event_type="RESPONSE_RECEIVED",
            recorded_at=_aware_now(clock),
            http_status=http_status,
            upstream_code=upstream_code,
            sanitized_message_category=category,
        )
        _write_event(directory, received, token=token, writer=artifact_writer)
        events = (*events, received)
        fault_injector("after_response_received")
    except Exception:
        # Never persist exception text. Reloading the create-only journal makes
        # a failed response checkpoint conservative: a started call without a
        # durable response becomes remote_execution_unknown_count=1.
        events = _load_event_chain(directory)
    finally:
        if session is not None:
            close_session = getattr(session, "close", None)
            if callable(close_session):
                close_session()

    return _publish_terminal_receipt(
        run_directory=directory,
        events=events,
        clock=clock,
        token=token,
        writer=artifact_writer,
        replayed=False,
    )


def _replay_http_terminal_diagnostic_locked(
    *,
    run_directory: Path | str = DEFAULT_RUN_DIRECTORY,
    repository_root: Path | str = REPOSITORY_ROOT,
    clock: Callable[[], datetime] = _utc_now,
    artifact_writer: ArtifactWriter = _default_artifact_writer,
) -> PublishedHttpTerminalDiagnostic:
    """Terminalize a persisted prefix without credentials, SDK, or network."""

    directory = _safe_run_directory(
        run_directory,
        repository_root=Path(repository_root).resolve(),
    )
    if not directory.is_dir():
        raise TushareHttpTerminalRunnerError("HTTP diagnostic run is unavailable")
    events = _load_event_chain(directory)
    return _publish_terminal_receipt(
        run_directory=directory,
        events=events,
        clock=clock,
        token="",
        writer=artifact_writer,
        replayed=True,
    )


def _run_live_http_terminal_diagnostic_for_test(
    *,
    token: str,
    run_directory: Path | str,
    repository_root: Path | str,
    config_path: Path | str = DEFAULT_CONFIG_PATH,
    access_policy_path: Path | str = DEFAULT_PROVIDER_ACCESS_POLICY_PATH,
    implementation_root: Path | str = REPOSITORY_ROOT,
    clock: Callable[[], datetime] = _utc_now,
    session_factory: Callable[[], Any] = _new_no_retry_session,
    git_metadata_provider: Callable[[], GitMetadata] = _default_git_metadata,
    artifact_writer: ArtifactWriter = _default_artifact_writer,
    fault_injector: FaultInjector = _no_fault,
) -> PublishedHttpTerminalDiagnostic:
    """Test seam for isolated roots; production callers use the fixed wrapper."""

    repository = Path(repository_root).resolve()
    directory = _safe_run_directory(run_directory, repository_root=repository)
    with _exclusive_authorized_round(directory.parent):
        return _run_live_http_terminal_diagnostic_locked(
            token=token,
            run_directory=directory,
            repository_root=repository,
            config_path=config_path,
            access_policy_path=access_policy_path,
            implementation_root=implementation_root,
            clock=clock,
            session_factory=session_factory,
            git_metadata_provider=git_metadata_provider,
            artifact_writer=artifact_writer,
            fault_injector=fault_injector,
        )


def run_live_http_terminal_diagnostic(
    *,
    token: str,
) -> PublishedHttpTerminalDiagnostic:
    """Run the sole production round at its code-owned fixed location."""

    return _run_live_http_terminal_diagnostic_for_test(
        token=token,
        run_directory=DEFAULT_RUN_DIRECTORY,
        repository_root=REPOSITORY_ROOT,
    )


def _replay_http_terminal_diagnostic_for_test(
    *,
    run_directory: Path | str,
    repository_root: Path | str,
    clock: Callable[[], datetime] = _utc_now,
    artifact_writer: ArtifactWriter = _default_artifact_writer,
) -> PublishedHttpTerminalDiagnostic:
    """Test seam for offline recovery under the same per-round lock."""

    repository = Path(repository_root).resolve()
    directory = _safe_run_directory(run_directory, repository_root=repository)
    with _exclusive_authorized_round(directory.parent):
        return _replay_http_terminal_diagnostic_locked(
            run_directory=directory,
            repository_root=repository,
            clock=clock,
            artifact_writer=artifact_writer,
        )


def replay_http_terminal_diagnostic() -> PublishedHttpTerminalDiagnostic:
    """Replay only the code-owned production round; this function never networks."""

    return _replay_http_terminal_diagnostic_for_test(
        run_directory=DEFAULT_RUN_DIRECTORY,
        repository_root=REPOSITORY_ROOT,
    )


def verify_no_token_leak(run_directory: Path | str, token: str) -> bool:
    """Scan the sealed tree for the exact credential without deriving it."""

    if not token:
        return True
    marker = token.encode("utf-8")
    root = Path(run_directory)
    for path in root.rglob("*"):
        if path.is_file() and marker in path.read_bytes():
            return False
    return True


def _safe_summary(result: PublishedHttpTerminalDiagnostic) -> dict[str, Any]:
    receipt = result.receipt
    return {
        "status": "terminal",
        "channel": CHANNEL,
        "endpoint": ENDPOINT,
        "terminal_reason": receipt.terminal_reason,
        "transport_status": receipt.transport_status,
        "http_status": receipt.http_status,
        "upstream_code": receipt.upstream_code,
        "sanitized_message_category": receipt.sanitized_message_category,
        "reserved_request_count": receipt.reserved_request_count,
        "network_call_started_count": receipt.network_call_started_count,
        "response_received_count": receipt.response_received_count,
        "terminal_result_count": receipt.terminal_result_count,
        "remote_execution_unknown_count": receipt.remote_execution_unknown_count,
        "budget_consumed_count": receipt.budget_consumed_count,
        "receipt_path": str(result.receipt_path),
        "receipt_file_sha256": result.receipt_file_sha256,
        "replayed": result.replayed,
    }


def _emit(value: Mapping[str, Any]) -> None:
    sys.stdout.buffer.write(canonical_json_bytes(dict(value)) + b"\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="One fixed crash-replayable Tushare HTTP trade_cal diagnostic"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="offline plan (default)")
    mode.add_argument("--live", action="store_true", help="one HTTP trade_cal request")
    mode.add_argument("--replay", action="store_true", help="offline terminal replay")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if not arguments.live and not arguments.replay:
        _emit(build_http_terminal_plan())
        return 0
    if arguments.replay:
        try:
            result = replay_http_terminal_diagnostic()
        except Exception:
            _emit({"status": "error", "error_category": "offline_replay_failed"})
            return 2
        _emit(_safe_summary(result))
        return 0

    token = _read_tushare_token()
    try:
        result = run_live_http_terminal_diagnostic(token=token)
    except (TushareHttpTerminalRoundBusyError, TushareHttpTerminalRoundClaimedError):
        _emit({"status": "error", "error_category": "authorized_round_unavailable"})
        return 2
    except Exception:
        # A one-shot writer failure after RUN_CREATED may be recoverable in the
        # same supervisor. This path never reads the credential again and never
        # sends network.
        try:
            result = replay_http_terminal_diagnostic()
        except Exception:
            _emit({"status": "error", "error_category": "terminal_receipt_unavailable"})
            return 2
    leak_free = verify_no_token_leak(result.receipt_path.parent, token)
    if not leak_free:
        _emit({"status": "error", "error_category": "credential_leak_detected"})
        return 2
    summary = _safe_summary(result)
    summary["token_leak_check"] = "passed"
    _emit(summary)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
