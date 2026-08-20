"""Choice secondary market-outcome diagnostics for stock report claims.

This module is intentionally separate from the formal broker-report audit.
It may read a Registry-validated Choice secondary batch through the explicit
diagnostic gate, but it never creates :class:`TruthObservation`, never writes
the audit SQLite database, and never changes the fixed eleven formal outputs.

The diagnostic target is deliberately narrow: the next exchange session open
to the 120th exchange session close, using Choice ``qfq`` stock prices and an
unadjusted CSI 300 benchmark.  Absolute target-price achievement is not
evaluated because a report's nominal target cannot safely be compared with a
post-hoc forward-adjusted price level without an admitted adjustment bridge.
"""

from __future__ import annotations

import csv
import json
import math
import re
import time as time_module
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from research.market_data import MarketDataRegistry, MarketDataRequest
from research.market_data.providers.baostock import (
    normalize_a_share_stock_instrument,
)
from research.market_data.providers.base import safe_error_text
from research.reproducibility import git_worktree_state

from .evaluation import evaluate_rating
from .skills import estimate_skill
from .storage import AuditStore


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
UTC = timezone.utc
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DIAGNOSTIC_STATUS = "diagnostic_choice_secondary_not_admitted"
BENCHMARK_INSTRUMENT = "000300.SH"
EVALUATED_HORIZON_DAYS = 120
CANDIDATE_TARGET_TYPES = frozenset({"stock_rating", "target_price"})
ALLOWED_FAILURE_STATUSES = frozenset(
    {"dependency_missing", "network_blocked", "not_configured", "failed"}
)
CHOICE_ARTIFACT_FILENAMES = (
    "choice_claim_market_outcomes.csv",
    "choice_broker_accuracy.csv",
    "choice_analyst_accuracy.csv",
    "choice_source_coverage.csv",
    "choice_exceptions.csv",
    "choice_reading_queue.md",
    "run_manifest.json",
)

OUTCOME_COLUMNS = (
    "claim_id",
    "report_id",
    "subject_id",
    "instrument_id",
    "target_type",
    "direction",
    "value_min",
    "value_max",
    "absolute_target_achievement",
    "original_horizon_days",
    "evaluated_horizon_days",
    "claim_available_at",
    "broker_id",
    "broker",
    "analyst",
    "team",
    "report_title",
    "report_pdf_url",
    "report_pdf_sha256",
    "evidence_span",
    "claim_evidence_source_kind",
    "claim_evidence_source_hash",
    "pdf_evidence_verified",
    "t0",
    "end_date",
    "entry_open_qfq",
    "exit_close_qfq",
    "stock_return",
    "benchmark_return",
    "geometric_excess_return",
    "market_hit",
    "outcome_error",
    "rating_threshold",
    "consensus_cluster_size",
    "consensus_weight",
    "stock_batch_id",
    "benchmark_batch_id",
    "calendar_batch_id",
    "outcome_status",
    "exclusion_reason",
    "diagnostic_status",
    "truth_eligible",
)

SKILL_COLUMNS = (
    "entity_type",
    "entity_id",
    "entity_display",
    "target_type",
    "evaluated_horizon_days",
    "raw_observation_count",
    "raw_hit_rate",
    "consensus_discounted_weight",
    "effective_sample_size",
    "posterior_skill",
    "conservative_lower_bound",
    "rank_eligible",
    "rank",
    "skill_status",
    "exclusion_reason",
    "diagnostic_status",
)

COVERAGE_COLUMNS = (
    "target_type",
    "candidate_claim_count",
    "unique_instrument_count",
    "successful_market_outcome_count",
    "explicit_exclusion_count",
    "broker_skill_cell_count",
    "rank_eligible_broker_cell_count",
    "analyst_team_skill_cell_count",
    "rank_eligible_analyst_team_cell_count",
    "coverage_status",
    "diagnostic_status",
)

EXCEPTION_COLUMNS = (
    "exception_id",
    "stage",
    "status",
    "code",
    "entity_id",
    "claim_count",
    "message",
    "details",
)


class ChoiceDiagnosticError(RuntimeError):
    """Raised when the explicit diagnostic contract cannot be satisfied."""


@dataclass(frozen=True)
class ChoiceDiagnosticBundle:
    """One deterministic seven-file Choice diagnostic bundle."""

    run_id: str
    output_directory: Path
    paths: Mapping[str, Path]
    hashes: Mapping[str, str]
    pdf_candidates: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class ChoiceCollection:
    """Validated batches and truthful per-instrument failures for one run."""

    calendar_batch: Any | None
    benchmark_batch: Any | None
    stock_batches: Mapping[str, Any]
    failures: Mapping[str, Mapping[str, Any]]
    checkpoint_path: Path


class SlidingWindowRateLimiter:
    """A testable sliding-window limiter for actual provider requests."""

    def __init__(
        self,
        max_requests_per_minute: int = 300,
        *,
        clock: Callable[[], float] = time_module.monotonic,
        sleeper: Callable[[float], None] = time_module.sleep,
    ) -> None:
        if (
            type(max_requests_per_minute) is not int
            or max_requests_per_minute <= 0
            or max_requests_per_minute > 300
        ):
            raise ChoiceDiagnosticError(
                "max_requests_per_minute must be an integer in [1, 300]"
            )
        self.maximum = max_requests_per_minute
        self._clock = clock
        self._sleeper = sleeper
        self._calls: deque[float] = deque()

    def acquire(self) -> None:
        while True:
            now = float(self._clock())
            while self._calls and now - self._calls[0] >= 60.0:
                self._calls.popleft()
            if len(self._calls) < self.maximum:
                self._calls.append(now)
                return
            wait_seconds = max(0.0, 60.0 - (now - self._calls[0]))
            self._sleeper(wait_seconds)


def _get(record: Any, *names: str, default: Any = None) -> Any:
    if record is None:
        return default
    for name in names:
        value = record.get(name) if isinstance(record, Mapping) else getattr(record, name, None)
        if value is not None:
            return value
    return default


def _normalise(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _normalise(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _normalise(item) for key, item in value.items()}
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (tuple, list)):
        return [_normalise(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_normalise(item) for item in value), key=_canonical_json)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _normalise(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _aware(value: datetime | date | str, *, end_of_day: bool = False) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, time.max if end_of_day else time.min, CHINA_TZ)
    else:
        text_value = str(value or "").strip()
        if not text_value:
            raise ChoiceDiagnosticError("timestamp must not be empty")
        if "T" not in text_value and " " not in text_value:
            result = datetime.combine(
                date.fromisoformat(text_value[:10]),
                time.max if end_of_day else time.min,
                CHINA_TZ,
            )
        else:
            result = datetime.fromisoformat(text_value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ChoiceDiagnosticError("timestamps must include a timezone offset")
    return result


def _as_of(value: datetime | date | str) -> datetime:
    return _aware(value, end_of_day=True)


def _float(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, ArithmeticError):
        return None
    return result if math.isfinite(result) else None


def _date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.astimezone(CHINA_TZ).date()
    if isinstance(value, date):
        return value
    return _aware(str(value)).astimezone(CHINA_TZ).date()


def _broker_identity(report: Any) -> str:
    metadata = _get(report, "metadata", default={})
    family = str(metadata.get("_broker_family_id") or "").strip() if isinstance(metadata, Mapping) else ""
    return (
        str(_get(report, "broker_code", default="") or "").strip()
        or family
        or str(_get(report, "broker", default="") or "").strip()
    )


def select_choice_candidate_claims(
    claims: Iterable[Any],
    *,
    as_of: datetime | date | str,
    reports: Iterable[Any] | Mapping[str, Any] | None = None,
) -> list[Any]:
    """Return the frozen structured-claim population visible at the cutoff.

    The optional report population is mandatory in the standard runner.  It
    prevents later PDF-validation claims from being counted a second time for
    the same report semantics.
    """

    cutoff = _as_of(as_of)
    report_by_id: dict[str, Any] | None = None
    if reports is not None:
        report_values = reports.values() if isinstance(reports, Mapping) else reports
        report_by_id = {
            str(_get(report, "report_id", default="")): report
            for report in report_values
        }
        from .extractors import EXTRACTOR_VERSION, extractor_bundle_sha256

        active_bundle = extractor_bundle_sha256()
    selected = []
    for claim in claims:
        if str(_get(claim, "dimension", default="")).strip().lower() != "stock":
            continue
        if str(_get(claim, "target_type", default="")).strip().lower() not in CANDIDATE_TARGET_TYPES:
            continue
        if report_by_id is not None:
            report = report_by_id.get(str(_get(claim, "report_id", default="")))
            if report is None:
                continue
            if (
                str(_get(claim, "extractor_version", default="")) != EXTRACTOR_VERSION
                or str(_get(claim, "extractor_bundle_sha256", default="")).lower()
                != active_bundle
                or str(_get(claim, "evidence_source_kind", default=""))
                != "structured/source_record"
                or str(_get(claim, "evidence_source_hash", default="")).lower()
                != str(_get(report, "content_hash", default="")).lower()
                or str(_get(claim, "evidence_parser_version", default=""))
                != "source-record-v1"
                or str(_get(claim, "evidence_prompt_version", default="")) != "none"
            ):
                continue
        try:
            available = _aware(_get(claim, "available_at"))
        except Exception:
            # It remains in scope so the outcome table can carry the explicit
            # invalid-availability exclusion instead of silently dropping it.
            selected.append(claim)
            continue
        if available <= cutoff:
            selected.append(claim)
    return sorted(
        selected,
        key=lambda item: (
            str(_get(item, "available_at", default="")),
            str(_get(item, "claim_id", default="")),
        ),
    )


def _failure(exc: BaseException) -> dict[str, str]:
    status = str(getattr(exc, "status", "") or "").strip()
    if status not in ALLOWED_FAILURE_STATUSES:
        code = str(getattr(exc, "code", "") or "").strip()
        lowered = f"{type(exc).__name__} {safe_error_text(exc)}".casefold()
        if code == "10001012" or any(token in lowered for token in ("permission", "not configured", "not authorized", "无权限")):
            status = "not_configured"
        elif isinstance(exc, (ImportError, ModuleNotFoundError)) or "dependency" in lowered or "sdk" in lowered and "missing" in lowered:
            status = "dependency_missing"
        elif any(token in lowered for token in ("network", "timeout", "timed out", "connection", "dns", "网络", "超时")):
            status = "network_blocked"
        else:
            status = "failed"
    return {
        "status": status,
        "code": str(getattr(exc, "code", "") or status),
        "error_type": type(exc).__name__,
        "message": safe_error_text(exc),
    }


def _global_failure_key(failure: Mapping[str, Any]) -> str:
    """Return the circuit-breaker identity for account-wide failures."""

    status = str(failure.get("status") or "")
    code = str(failure.get("code") or "")
    if code == "quota_exhausted":
        return code
    if status in {"dependency_missing", "network_blocked", "not_configured"}:
        return status
    return ""


def _quota_truncated(failures: Mapping[str, Mapping[str, Any]]) -> bool:
    return any(
        str(failure.get("code") or "")
        in {"quota_exhausted", "choice_quota_exhausted_circuit_open"}
        for failure in failures.values()
    )


def _collection_rank_status(
    failures: Mapping[str, Mapping[str, Any]],
) -> str:
    if not failures:
        return ""
    if _quota_truncated(failures):
        return "partial_quota_truncated_not_rankable"
    return "partial_collection_not_rankable"


def _suppress_incomplete_collection_rankings(
    rows: Sequence[Mapping[str, Any]],
    *,
    status: str,
) -> list[dict[str, Any]]:
    """Retain coverage counts but suppress statistics from incomplete data."""

    return [
        {
            **dict(row),
            "raw_hit_rate": None,
            "posterior_skill": None,
            "conservative_lower_bound": None,
            "rank_eligible": False,
            "rank": None,
            "skill_status": status,
            "exclusion_reason": "choice_incomplete_nonrandom_instrument_coverage",
        }
        for row in rows
    ]


def _batch_evidence(batch: Any | None) -> dict[str, Any]:
    if batch is None:
        return {}
    return {
        name: _normalise(_get(batch, name))
        for name in (
            "batch_id",
            "provider_id",
            "upstream_source",
            "dataset_type",
            "schema_version",
            "adapter_version",
            "request_fingerprint",
            "request_payload",
            "retrieval_mode",
            "requested_at",
            "fetched_at",
            "raw_content_sha256",
            "normalized_content_sha256",
            "record_count",
            "completeness_status",
            "freshness_status",
            "admission_status",
            "point_in_time_status",
            "synthetic",
            "issues",
        )
    }


def _require_choice_batch(
    batch: Any,
    *,
    dataset_type: str,
    adjustment: str,
    instrument_id: str = "",
) -> Any:
    reasons: list[str] = []
    if str(_get(batch, "provider_id", default="")) != "choice":
        reasons.append("provider_mismatch")
    if str(_get(batch, "dataset_type", default="")) != dataset_type:
        reasons.append("dataset_mismatch")
    if str(_get(batch, "admission_status", default="")) != "validated_secondary_not_primary":
        reasons.append("diagnostic_admission_mismatch")
    if str(_get(batch, "completeness_status", default="")) != "complete":
        reasons.append("batch_incomplete")
    if _get(batch, "synthetic") is not False:
        reasons.append("synthetic_or_unknown")
    payload = _get(batch, "request_payload", default={})
    if not isinstance(payload, Mapping):
        reasons.append("request_payload_missing")
    else:
        if str(payload.get("adjustment") or "none") != adjustment:
            reasons.append("adjustment_mismatch")
        if instrument_id and str(payload.get("instrument_id") or "") != instrument_id:
            reasons.append("instrument_mismatch")
    if reasons:
        raise ChoiceDiagnosticError("Choice diagnostic batch rejected: " + "|".join(reasons))
    return batch


def _load_checkpoint(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"schema_version": "choice-diagnostic-checkpoint.v1", "entries": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ChoiceDiagnosticError(f"cannot read Choice checkpoint: {safe_error_text(exc)}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != "choice-diagnostic-checkpoint.v1"
        or not isinstance(payload.get("entries"), dict)
    ):
        raise ChoiceDiagnosticError("Choice checkpoint contract is invalid")
    return payload


def _save_checkpoint(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(raw)
    temporary.replace(path)


def _checkpoint_key(request: MarketDataRequest) -> str:
    return _sha256_text(
        _canonical_json(
            {
                "dataset_type": request.dataset_type,
                "instrument_id": request.instrument_id,
                "start_date": request.start_date,
                "end_date": request.end_date,
                "adjustment": request.adjustment,
                "parameters": {},
            }
        )
    )


def _diagnostic_fetch(registry: Any, request: MarketDataRequest) -> Any:
    fetcher = getattr(registry, "fetch_diagnostic", None)
    if not callable(fetcher):
        raise ChoiceDiagnosticError(
            "MarketDataRegistry does not expose the explicit fetch_diagnostic gate"
        )
    return fetcher(request, provider_id="choice")


def _request(
    *,
    dataset_type: str,
    requested_at: datetime,
    retrieval_mode: str,
    start_date: date,
    end_date: date,
    instrument_id: str = "",
    adjustment: str = "none",
) -> MarketDataRequest:
    return MarketDataRequest(
        dataset_type=dataset_type,
        requested_at=requested_at,
        retrieval_mode=retrieval_mode,
        instrument_id=instrument_id,
        start_date=start_date,
        end_date=end_date,
        adjustment=adjustment,
        evidence_cutoff_at=requested_at if retrieval_mode == "offline_replay" else None,
    )


def _fetch_with_resume(
    registry: Any,
    request: MarketDataRequest,
    *,
    online: bool,
    resume: bool,
    checkpoint: dict[str, Any],
    checkpoint_path: Path,
    limiter: SlidingWindowRateLimiter,
) -> tuple[Any | None, Mapping[str, Any] | None]:
    key = _checkpoint_key(request)
    entries = checkpoint["entries"]
    prior = entries.get(key) if isinstance(entries, dict) else None
    if resume and isinstance(prior, Mapping) and prior.get("status") == "success":
        replay = _request(
            dataset_type=request.dataset_type,
            requested_at=request.requested_at,
            retrieval_mode="offline_replay",
            instrument_id=request.instrument_id,
            start_date=request.start_date,  # type: ignore[arg-type]
            end_date=request.end_date,  # type: ignore[arg-type]
            adjustment=request.adjustment,
        )
        try:
            return _diagnostic_fetch(registry, replay), None
        except Exception as exc:
            if not online:
                return None, _failure(exc)
    if not online and resume and isinstance(prior, Mapping):
        prior_failure = prior.get("failure")
        prior_status = str(prior.get("status") or "")
        if (
            prior_status in ALLOWED_FAILURE_STATUSES
            and isinstance(prior_failure, Mapping)
        ):
            # A failed online request has no raw batch to replay. Preserve its
            # fail-closed status/code instead of relabelling it as a new cache
            # miss, which is essential for deterministic quota truncation.
            return None, {
                "status": prior_status,
                "code": str(prior_failure.get("code") or prior_status),
                "error_type": str(prior_failure.get("error_type") or ""),
                "message": safe_error_text(
                    prior_failure.get("message") or "prior provider request failed"
                ),
            }
    try:
        if online:
            limiter.acquire()
        batch = _diagnostic_fetch(registry, request)
        entries[key] = {
            "status": "success",
            "dataset_type": request.dataset_type,
            "instrument_id": request.instrument_id,
            "start_date": request.start_date.isoformat() if request.start_date else "",
            "end_date": request.end_date.isoformat() if request.end_date else "",
            "adjustment": request.adjustment,
            "batch_id": str(_get(batch, "batch_id", default="")),
            "normalized_content_sha256": str(_get(batch, "normalized_content_sha256", default="")),
        }
        _save_checkpoint(checkpoint_path, checkpoint)
        return batch, None
    except Exception as exc:
        failure = _failure(exc)
        if isinstance(prior, Mapping) and prior.get("status") == "success":
            entries[key] = {**dict(prior), "last_attempt": failure}
        else:
            entries[key] = {
                "status": failure["status"],
                "dataset_type": request.dataset_type,
                "instrument_id": request.instrument_id,
                "start_date": request.start_date.isoformat() if request.start_date else "",
                "end_date": request.end_date.isoformat() if request.end_date else "",
                "adjustment": request.adjustment,
                "failure": failure,
            }
        _save_checkpoint(checkpoint_path, checkpoint)
        return None, failure


def _calendar_days(batch: Any) -> list[date]:
    days = []
    for record in _get(batch, "records", default=()) or ():
        if _get(record, "is_trading_day") is True:
            days.append(date.fromisoformat(str(_get(record, "calendar_date"))))
    if days != sorted(set(days)):
        raise ChoiceDiagnosticError("Choice trade calendar is not unique and ascending")
    return days


def _execution_session(
    claim: Any,
    report: Any | None,
    sessions: Sequence[date],
) -> date:
    """Resolve t0 without advancing a date-only report twice."""

    available_day = _aware(_get(claim, "available_at")).astimezone(CHINA_TZ).date()
    timestamp_quality = str(
        _get(report, "timestamp_quality", default="") if report is not None else ""
    ).strip().lower()
    inclusive = timestamp_quality.startswith("date_only_")
    for day in sessions:
        if (inclusive and day >= available_day) or (
            not inclusive and day > available_day
        ):
            return day
    raise ChoiceDiagnosticError("no executable Choice session after claim availability")


def _reading_candidate_window(
    *, sample_end: date, as_of_day: date, recent_candidate_days: int
) -> tuple[date, date]:
    """Keep current reading candidates strictly after the skill sample."""

    nominal_start = as_of_day - timedelta(days=recent_candidate_days - 1)
    return max(nominal_start, sample_end + timedelta(days=1)), as_of_day


def collect_choice_market_data(
    registry: Any,
    claims: Sequence[Any],
    *,
    as_of: datetime | date | str,
    checkpoint_path: Path | str,
    offline: bool,
    resume: bool,
    max_requests_per_minute: int = 300,
    requested_at: datetime | None = None,
    limiter: SlidingWindowRateLimiter | None = None,
    reports: Iterable[Any] | Mapping[str, Any] | None = None,
) -> ChoiceCollection:
    """Collect within one bounded Choice login when the Registry supports it."""

    session_factory = getattr(registry, "diagnostic_session", None)
    arguments = {
        "as_of": as_of,
        "checkpoint_path": checkpoint_path,
        "offline": offline,
        "resume": resume,
        "max_requests_per_minute": max_requests_per_minute,
        "requested_at": requested_at,
        "limiter": limiter,
        "reports": reports,
    }
    if offline or not claims or not callable(session_factory):
        return _collect_choice_market_data_in_session(registry, claims, **arguments)
    collection: ChoiceCollection | None = None
    try:
        with session_factory(provider_id="choice"):
            collection = _collect_choice_market_data_in_session(
                registry, claims, **arguments
            )
    except Exception as exc:
        failure = _failure(exc)
        if collection is None:
            # Session entry failed before even the calendar could be fetched.
            # Keep the one-row request failure and let the outcome layer emit
            # an explicit exclusion for every candidate claim.
            return ChoiceCollection(
                None,
                None,
                {},
                {"trade_calendar": failure},
                Path(checkpoint_path),
            )
        # A stop failure must not erase already persisted, validated batches.
        # It remains visible in the manifest/exception bundle as a separate
        # session-lifecycle failure.
        failures = dict(collection.failures)
        failures["session_stop"] = failure
        return ChoiceCollection(
            collection.calendar_batch,
            collection.benchmark_batch,
            collection.stock_batches,
            failures,
            collection.checkpoint_path,
        )
    if collection is None:  # pragma: no cover - defensive context-manager guard
        raise ChoiceDiagnosticError("Choice diagnostic session returned no collection")
    return collection


def _collect_choice_market_data_in_session(
    registry: Any,
    claims: Sequence[Any],
    *,
    as_of: datetime | date | str,
    checkpoint_path: Path | str,
    offline: bool,
    resume: bool,
    max_requests_per_minute: int = 300,
    requested_at: datetime | None = None,
    limiter: SlidingWindowRateLimiter | None = None,
    reports: Iterable[Any] | Mapping[str, Any] | None = None,
) -> ChoiceCollection:
    """Collect whole Choice batches with per-instrument durable progress."""

    cutoff = _as_of(as_of)
    request_time = requested_at or datetime.now(UTC)
    if request_time.tzinfo is None or request_time.utcoffset() is None:
        raise ChoiceDiagnosticError("requested_at must include a timezone offset")
    checkpoint_file = Path(checkpoint_path)
    checkpoint = _load_checkpoint(checkpoint_file)
    if max_requests_per_minute < 2 or max_requests_per_minute > 300:
        raise ChoiceDiagnosticError(
            "Choice SDK data-call budget must be in [2, 300] per minute"
        )
    # One daily-bar Registry fetch invokes both ``csd`` and ``tradedates``.
    # Budget two SDK data calls for every Registry request (calendar requests
    # then consume less than their reservation), so total calls cannot exceed
    # the caller's <=300 SDK-call limit.
    registry_requests_per_minute = max(1, max_requests_per_minute // 2)
    rate_limiter = limiter or SlidingWindowRateLimiter(registry_requests_per_minute)
    failures: dict[str, Mapping[str, Any]] = {}
    if not claims:
        return ChoiceCollection(None, None, {}, failures, checkpoint_file)

    valid_claim_dates: list[date] = []
    for claim in claims:
        try:
            valid_claim_dates.append(_aware(_get(claim, "available_at")).astimezone(CHINA_TZ).date())
        except Exception:
            continue
    if not valid_claim_dates:
        failures["trade_calendar"] = {
            "status": "failed",
            "code": "all_claim_availability_invalid",
            "message": "all candidate claims have invalid available_at",
        }
        return ChoiceCollection(None, None, {}, failures, checkpoint_file)

    retrieval_mode = "offline_replay" if offline else "historical_backfill"
    calendar_request = _request(
        dataset_type="trade_calendar",
        requested_at=request_time,
        retrieval_mode=retrieval_mode,
        start_date=min(valid_claim_dates),
        end_date=cutoff.astimezone(CHINA_TZ).date(),
    )
    calendar_batch, calendar_failure = _fetch_with_resume(
        registry,
        calendar_request,
        online=not offline,
        resume=resume,
        checkpoint=checkpoint,
        checkpoint_path=checkpoint_file,
        limiter=rate_limiter,
    )
    if calendar_failure:
        failures["trade_calendar"] = calendar_failure
        return ChoiceCollection(None, None, {}, failures, checkpoint_file)
    try:
        calendar_batch = _require_choice_batch(
            calendar_batch, dataset_type="trade_calendar", adjustment="none"
        )
        sessions = _calendar_days(calendar_batch)
    except Exception as exc:
        failures["trade_calendar"] = _failure(exc)
        return ChoiceCollection(None, None, {}, failures, checkpoint_file)

    windows: dict[str, list[date]] = defaultdict(list)
    session_index = {day: index for index, day in enumerate(sessions)}
    report_by_id = (
        {}
        if reports is None
        else dict(reports)
        if isinstance(reports, Mapping)
        else {
            str(_get(report, "report_id", default="")): report
            for report in reports
        }
    )
    for claim in claims:
        try:
            report = report_by_id.get(str(_get(claim, "report_id", default="")))
            t0 = _execution_session(claim, report, sessions)
            end_index = session_index[t0] + EVALUATED_HORIZON_DAYS - 1
            if end_index >= len(sessions):
                continue
            instrument = normalize_a_share_stock_instrument(str(_get(claim, "subject_id", default="")))
            windows[instrument].extend((t0, sessions[end_index]))
        except (ValueError, ChoiceDiagnosticError):
            continue

    benchmark_batch = None
    if windows:
        global_start = min(min(days) for days in windows.values())
        global_end = max(max(days) for days in windows.values())
        benchmark_request = _request(
            dataset_type="daily_bar",
            requested_at=request_time,
            retrieval_mode=retrieval_mode,
            instrument_id=BENCHMARK_INSTRUMENT,
            start_date=global_start,
            end_date=global_end,
            adjustment="none",
        )
        benchmark_batch, failure = _fetch_with_resume(
            registry,
            benchmark_request,
            online=not offline,
            resume=resume,
            checkpoint=checkpoint,
            checkpoint_path=checkpoint_file,
            limiter=rate_limiter,
        )
        if failure:
            failures[BENCHMARK_INSTRUMENT] = failure
        else:
            try:
                benchmark_batch = _require_choice_batch(
                    benchmark_batch,
                    dataset_type="daily_bar",
                    adjustment="none",
                    instrument_id=BENCHMARK_INSTRUMENT,
                )
            except Exception as exc:
                failures[BENCHMARK_INSTRUMENT] = _failure(exc)
                benchmark_batch = None

    stock_batches: dict[str, Any] = {}
    global_failure_status = ""
    global_failure_key = ""
    consecutive_global_failures = 0
    for instrument, days in sorted(windows.items()):
        if consecutive_global_failures >= 3 and global_failure_status:
            quota_exhausted = global_failure_key == "quota_exhausted"
            failures[instrument] = {
                "status": global_failure_status,
                "code": (
                    "choice_quota_exhausted_circuit_open"
                    if quota_exhausted
                    else "choice_collection_circuit_open"
                ),
                "message": (
                    "Choice collection stopped after three consecutive account-, "
                    "network-, or quota-level failures; prior successful checkpoints "
                    "were preserved"
                ),
            }
            continue
        request = _request(
            dataset_type="daily_bar",
            requested_at=request_time,
            retrieval_mode=retrieval_mode,
            instrument_id=instrument,
            start_date=min(days),
            end_date=max(days),
            adjustment="qfq",
        )
        batch, failure = _fetch_with_resume(
            registry,
            request,
            online=not offline,
            resume=resume,
            checkpoint=checkpoint,
            checkpoint_path=checkpoint_file,
            limiter=rate_limiter,
        )
        if failure:
            failures[instrument] = failure
            failure_status = str(failure.get("status") or "")
            failure_key = _global_failure_key(failure)
            if failure_key:
                if failure_key == global_failure_key:
                    consecutive_global_failures += 1
                else:
                    global_failure_status = failure_status
                    global_failure_key = failure_key
                    consecutive_global_failures = 1
            else:
                global_failure_status = ""
                global_failure_key = ""
                consecutive_global_failures = 0
            continue
        try:
            stock_batches[instrument] = _require_choice_batch(
                batch,
                dataset_type="daily_bar",
                adjustment="qfq",
                instrument_id=instrument,
            )
            global_failure_status = ""
            global_failure_key = ""
            consecutive_global_failures = 0
        except Exception as exc:
            failure = _failure(exc)
            failures[instrument] = failure
            failure_status = str(failure.get("status") or "")
            failure_key = _global_failure_key(failure)
            if failure_key:
                if failure_key == global_failure_key:
                    consecutive_global_failures += 1
                else:
                    global_failure_status = failure_status
                    global_failure_key = failure_key
                    consecutive_global_failures = 1
            else:
                global_failure_status = ""
                global_failure_key = ""
                consecutive_global_failures = 0
    return ChoiceCollection(
        calendar_batch,
        benchmark_batch,
        stock_batches,
        failures,
        checkpoint_file,
    )


def _bar_by_date(batch: Any | None) -> dict[date, Mapping[str, Any]]:
    if batch is None:
        return {}
    result: dict[date, Mapping[str, Any]] = {}
    for record in _get(batch, "records", default=()) or ():
        day = date.fromisoformat(str(_get(record, "trading_date")))
        if day in result:
            raise ChoiceDiagnosticError(f"duplicate daily bar: {day.isoformat()}")
        result[day] = record
    return result


def _tradable(record: Mapping[str, Any] | None) -> bool:
    if record is None:
        return False
    status = str(_get(record, "trading_status", default="unknown")).strip().lower()
    if status == "suspended":
        return False
    volume = _float(_get(record, "volume"))
    return status == "traded" or (status == "unknown" and volume is not None and volume > 0.0)


def _base_outcome(claim: Any, report: Any | None) -> dict[str, Any]:
    return {
        "claim_id": str(_get(claim, "claim_id", default="")),
        "report_id": str(_get(claim, "report_id", default="")),
        "subject_id": str(_get(claim, "subject_id", default="")),
        "instrument_id": "",
        "target_type": str(_get(claim, "target_type", default="")).strip().lower(),
        "direction": _get(claim, "direction"),
        "value_min": _get(claim, "value_min"),
        "value_max": _get(claim, "value_max"),
        "absolute_target_achievement": (
            "absolute_target_achievement_not_evaluated"
            if str(_get(claim, "target_type", default="")).strip().lower() == "target_price"
            else "not_applicable"
        ),
        "original_horizon_days": _get(claim, "horizon_days"),
        "evaluated_horizon_days": EVALUATED_HORIZON_DAYS,
        "claim_available_at": _normalise(_get(claim, "available_at")),
        "broker_id": _broker_identity(report) if report else "",
        "broker": str(_get(report, "broker", default="") or ""),
        "analyst": str(_get(report, "analyst", default="") or ""),
        "team": str(_get(report, "team", default="") or ""),
        "report_title": str(_get(report, "title", default="") or ""),
        "report_pdf_url": str(_get(report, "pdf_url", default="") or ""),
        "report_pdf_sha256": str(_get(report, "pdf_sha256", default="") or ""),
        "evidence_span": str(_get(claim, "evidence_span", default="") or ""),
        "claim_evidence_source_kind": str(
            _get(claim, "evidence_source_kind", default="") or ""
        ),
        "claim_evidence_source_hash": str(
            _get(claim, "evidence_source_hash", default="") or ""
        ),
        "pdf_evidence_verified": bool(
            str(_get(claim, "evidence_source_kind", default="")) == "textual/pdf"
            and str(_get(claim, "evidence_source_hash", default="")).strip().lower()
            == str(_get(report, "pdf_sha256", default="") or "").strip().lower()
            and re.fullmatch(
                r"[0-9a-f]{64}",
                str(_get(report, "pdf_sha256", default="") or "").strip().lower(),
            )
            is not None
            and bool(str(_get(claim, "evidence_span", default="") or "").strip())
        ),
        "t0": "",
        "end_date": "",
        "entry_open_qfq": None,
        "exit_close_qfq": None,
        "stock_return": None,
        "benchmark_return": None,
        "geometric_excess_return": None,
        "market_hit": None,
        "outcome_error": None,
        "rating_threshold": None,
        "consensus_cluster_size": 0,
        "consensus_weight": 0.0,
        "stock_batch_id": "",
        "benchmark_batch_id": "",
        "calendar_batch_id": "",
        "outcome_status": "excluded",
        "exclusion_reason": "",
        "diagnostic_status": DIAGNOSTIC_STATUS,
        "truth_eligible": False,
        "truth_available_at": "",
    }


def compute_choice_market_outcomes(
    claims: Sequence[Any],
    reports: Iterable[Any] | Mapping[str, Any],
    collection: ChoiceCollection,
    *,
    as_of: datetime | date | str,
) -> list[dict[str, Any]]:
    """Produce exactly one success or explicit exclusion per candidate claim."""

    cutoff = _as_of(as_of)
    report_by_id = (
        dict(reports)
        if isinstance(reports, Mapping)
        else {str(_get(report, "report_id", default="")): report for report in reports}
    )
    calendar_id = str(_get(collection.calendar_batch, "batch_id", default="") or "")
    benchmark_id = str(_get(collection.benchmark_batch, "batch_id", default="") or "")
    sessions = _calendar_days(collection.calendar_batch) if collection.calendar_batch else []
    session_index = {day: index for index, day in enumerate(sessions)}
    benchmark_by_day = _bar_by_date(collection.benchmark_batch)
    stock_maps = {instrument: _bar_by_date(batch) for instrument, batch in collection.stock_batches.items()}
    rows: list[dict[str, Any]] = []

    for claim in claims:
        report = report_by_id.get(str(_get(claim, "report_id", default="")))
        row = _base_outcome(claim, report)
        row["calendar_batch_id"] = calendar_id
        row["benchmark_batch_id"] = benchmark_id

        def exclude(reason: str) -> None:
            row["outcome_status"] = "excluded"
            row["exclusion_reason"] = reason

        try:
            available = _aware(_get(claim, "available_at"))
        except Exception:
            exclude("invalid_claim_available_at")
            rows.append(row)
            continue
        if available > cutoff:
            exclude("future_claim")
            rows.append(row)
            continue
        if not sessions:
            failure = collection.failures.get("trade_calendar", {})
            exclude("trade_calendar_unavailable:" + str(failure.get("status") or "failed"))
            rows.append(row)
            continue
        try:
            instrument = normalize_a_share_stock_instrument(str(_get(claim, "subject_id", default="")))
        except ValueError:
            exclude("unsupported_a_share_instrument")
            rows.append(row)
            continue
        row["instrument_id"] = instrument
        try:
            t0 = _execution_session(claim, report, sessions)
        except ChoiceDiagnosticError:
            exclude("next_trading_session_unavailable")
            rows.append(row)
            continue
        end_index = session_index[t0] + EVALUATED_HORIZON_DAYS - 1
        if end_index >= len(sessions):
            exclude("unmatured_120_session_horizon")
            rows.append(row)
            continue
        end_day = sessions[end_index]
        row["t0"] = t0.isoformat()
        row["end_date"] = end_day.isoformat()
        batch = collection.stock_batches.get(instrument)
        if batch is None:
            failure = collection.failures.get(instrument, {})
            exclude("stock_batch_unavailable:" + str(failure.get("status") or "failed"))
            rows.append(row)
            continue
        row["stock_batch_id"] = str(_get(batch, "batch_id", default="") or "")
        stock_by_day = stock_maps[instrument]
        entry = stock_by_day.get(t0)
        exit_bar = stock_by_day.get(end_day)
        if not _tradable(entry):
            exclude("entry_session_suspended_or_missing")
            rows.append(row)
            continue
        if not _tradable(exit_bar):
            exclude("horizon_session_suspended_or_missing")
            rows.append(row)
            continue
        if collection.benchmark_batch is None:
            failure = collection.failures.get(BENCHMARK_INSTRUMENT, {})
            exclude("benchmark_batch_unavailable:" + str(failure.get("status") or "failed"))
            rows.append(row)
            continue
        benchmark_entry = benchmark_by_day.get(t0)
        benchmark_exit = benchmark_by_day.get(end_day)
        if not _tradable(benchmark_entry) or not _tradable(benchmark_exit):
            exclude("missing_aligned_csi300_benchmark")
            rows.append(row)
            continue
        entry_open = _float(_get(entry, "open"))
        exit_close = _float(_get(exit_bar, "close"))
        benchmark_open = _float(_get(benchmark_entry, "open"))
        benchmark_close = _float(_get(benchmark_exit, "close"))
        if any(value is None or value <= 0.0 for value in (entry_open, exit_close, benchmark_open, benchmark_close)):
            exclude("invalid_return_endpoint_price")
            rows.append(row)
            continue
        assert entry_open is not None and exit_close is not None
        assert benchmark_open is not None and benchmark_close is not None
        stock_return = exit_close / entry_open - 1.0
        benchmark_return = benchmark_close / benchmark_open - 1.0
        if benchmark_return <= -1.0:
            exclude("invalid_benchmark_return")
            rows.append(row)
            continue
        excess = (1.0 + stock_return) / (1.0 + benchmark_return) - 1.0
        row.update(
            {
                "entry_open_qfq": entry_open,
                "exit_close_qfq": exit_close,
                "stock_return": stock_return,
                "benchmark_return": benchmark_return,
                "geometric_excess_return": excess,
                "truth_available_at": _normalise(_get(exit_bar, "available_at")),
            }
        )
        direction = _get(claim, "direction")
        if isinstance(direction, bool) or direction not in (-1, 0, 1):
            exclude("invalid_claim_direction")
            rows.append(row)
            continue
        target_type = str(row["target_type"])
        if target_type == "target_price":
            if direction == 0:
                exclude("target_price_direction_missing")
                rows.append(row)
                continue
            row["market_hit"] = direction * excess > 0.0
            row["outcome_error"] = max(0.0, -(direction * excess))
        else:
            rating = evaluate_rating(direction, excess, EVALUATED_HORIZON_DAYS)
            row["market_hit"] = bool(rating["hit"])
            row["outcome_error"] = float(rating["error"])
            row["rating_threshold"] = float(rating["threshold"])
        row["outcome_status"] = "success"
        row["exclusion_reason"] = ""
        rows.append(row)

    cluster_counts = Counter(
        (
            str(row["subject_id"]),
            str(row["target_type"]),
            int(row["direction"] or 0),
            str(row["claim_available_at"])[:10],
        )
        for row in rows
        if row["outcome_status"] == "success"
    )
    for row in rows:
        key = (
            str(row["subject_id"]),
            str(row["target_type"]),
            int(row["direction"] or 0),
            str(row["claim_available_at"])[:10],
        )
        size = cluster_counts.get(key, 0)
        row["consensus_cluster_size"] = size
        row["consensus_weight"] = 1.0 / size if size else 0.0
    rows.sort(key=lambda row: (str(row["claim_available_at"]), str(row["claim_id"])))
    if len(rows) != len(claims) or len({str(row["claim_id"]) for row in rows}) != len(rows):
        raise ChoiceDiagnosticError("candidate claim coverage is not one-to-one")
    if any(
        row["outcome_status"] not in {"success", "excluded"}
        or row["outcome_status"] == "excluded" and not row["exclusion_reason"]
        for row in rows
    ):
        raise ChoiceDiagnosticError("every candidate claim needs success or an explicit exclusion")
    return rows


def _skill_record(row: Mapping[str, Any]) -> dict[str, Any]:
    record = {
        "report_id": row["report_id"],
        "subject_id": row["subject_id"],
        "dimension": "stock",
        "target_type": row["target_type"],
        "horizon_days": EVALUATED_HORIZON_DAYS,
        "direction": row["direction"],
        "available_at": row["claim_available_at"],
        "truth_available_at": row["truth_available_at"],
        "hit": row["market_hit"],
        "mature": True,
    }
    raw_consensus_weight = row.get("consensus_weight")
    try:
        consensus_weight = float(raw_consensus_weight)
    except (TypeError, ValueError):
        consensus_weight = 0.0
    # Direct unit callers and legacy diagnostic rows may not yet have passed
    # through the global clustering step.  Only opt into the precomputed path
    # when a valid external weight is actually present; otherwise the generic
    # skill estimator retains its deterministic local fallback.
    if math.isfinite(consensus_weight) and 0.0 < consensus_weight <= 1.0:
        record["consensus_weight"] = consensus_weight
    return record


def _rank_skill_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    peer_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["rank_eligible"]:
            peer_groups[(str(row["entity_type"]), str(row["target_type"]))].append(row)
    for peers in peer_groups.values():
        peers.sort(
            key=lambda row: (
                -float(row["conservative_lower_bound"]),
                -float(row["effective_sample_size"]),
                str(row["entity_id"]),
            )
        )
        prior_lower: float | None = None
        rank = 0
        for index, row in enumerate(peers, start=1):
            lower = float(row["conservative_lower_bound"])
            if prior_lower is None or lower != prior_lower:
                rank = index
                prior_lower = lower
            row["rank"] = rank
    return sorted(
        rows,
        key=lambda row: (
            str(row["target_type"]),
            str(row["entity_type"]),
            0 if row["rank_eligible"] else 1,
            int(row["rank"] or 10**9),
            str(row["entity_id"]),
        ),
    )


def _skill_table(
    outcomes: Sequence[Mapping[str, Any]],
    *,
    entity_types: Sequence[str],
    as_of: datetime | date | str,
    minimum_effective_sample_size: float,
) -> list[dict[str, Any]]:
    successes = [
        row
        for row in outcomes
        if row.get("outcome_status") == "success" and type(row.get("market_hit")) is bool
    ]
    global_priors: dict[str, float] = {}
    for target_type in sorted(CANDIDATE_TARGET_TYPES):
        records = [_skill_record(row) for row in successes if row["target_type"] == target_type]
        if records:
            consensus_field = (
                "consensus_weight"
                if all("consensus_weight" in record for record in records)
                else None
            )
            global_priors[target_type] = float(
                estimate_skill(
                    records,
                    as_of=as_of,
                    prior_mean=0.5,
                    prior_strength=8.0,
                    consensus_power=1.0,
                    precomputed_consensus_weight_field=consensus_field,
                )["posterior_skill"]
            )

    groups: dict[tuple[str, str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in successes:
        target_type = str(row["target_type"])
        if "broker" in entity_types and str(row.get("broker_id") or ""):
            groups[("broker", str(row["broker_id"]), str(row.get("broker") or row["broker_id"]), target_type)].append(row)
        broker_id = str(row.get("broker_id") or "").strip()
        broker_display = str(row.get("broker") or broker_id).strip()
        if (
            "analyst" in entity_types
            and broker_id
            and str(row.get("analyst") or "").strip()
        ):
            analyst = " ".join(str(row["analyst"]).split())
            analyst_id = f"{broker_id}|{analyst.casefold()}"
            groups[
                ("analyst", analyst_id, f"{broker_display} / {analyst}", target_type)
            ].append(row)
        if (
            "team" in entity_types
            and broker_id
            and str(row.get("team") or "").strip()
        ):
            team = " ".join(str(row["team"]).split())
            team_id = f"{broker_id}|{team.casefold()}"
            groups[("team", team_id, f"{broker_display} / {team}", target_type)].append(row)

    result: list[dict[str, Any]] = []
    for (entity_type, entity_id, display, target_type), cell in sorted(groups.items()):
        records = [_skill_record(row) for row in cell]
        consensus_field = (
            "consensus_weight"
            if all("consensus_weight" in record for record in records)
            else None
        )
        estimate = estimate_skill(
            records,
            as_of=as_of,
            prior_mean=global_priors.get(target_type, 0.5),
            prior_strength=5.0,
            consensus_power=1.0,
            precomputed_consensus_weight_field=consensus_field,
        )
        ess = float(estimate["effective_sample_size"])
        eligible = ess >= float(minimum_effective_sample_size)
        raw_hit_rate = sum(1 for row in cell if row["market_hit"] is True) / len(cell)
        result.append(
            {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "entity_display": display,
                "target_type": target_type,
                "evaluated_horizon_days": EVALUATED_HORIZON_DAYS,
                "raw_observation_count": int(estimate["raw_observation_count"]),
                "raw_hit_rate": raw_hit_rate,
                "consensus_discounted_weight": float(estimate["total_weight"]),
                "effective_sample_size": ess,
                "posterior_skill": (
                    float(estimate["posterior_skill"]) if eligible else None
                ),
                "conservative_lower_bound": (
                    float(estimate["conservative_lower_bound"])
                    if eligible
                    else None
                ),
                "rank_eligible": eligible,
                "rank": None,
                "skill_status": "ranked_diagnostic" if eligible else "coverage_only",
                "exclusion_reason": "" if eligible else "effective_sample_size_below_5",
                "diagnostic_status": DIAGNOSTIC_STATUS,
            }
        )
    return _rank_skill_rows(result)


def build_choice_skill_tables(
    outcomes: Sequence[Mapping[str, Any]],
    *,
    as_of: datetime | date | str,
    minimum_effective_sample_size: float = 5.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return separate broker and analyst/team diagnostic skill tables."""

    if minimum_effective_sample_size < 5.0:
        raise ChoiceDiagnosticError("diagnostic ranking ESS threshold cannot be below 5")
    return (
        _skill_table(
            outcomes,
            entity_types=("broker",),
            as_of=as_of,
            minimum_effective_sample_size=minimum_effective_sample_size,
        ),
        _skill_table(
            outcomes,
            entity_types=("analyst", "team"),
            as_of=as_of,
            minimum_effective_sample_size=minimum_effective_sample_size,
        ),
    )


def select_pdf_candidates(
    outcomes: Sequence[Mapping[str, Any]],
    broker_skills: Sequence[Mapping[str, Any]],
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Preselect at most 20 bounded PDF downloads using skill lower bounds."""

    resolved_limit = max(0, min(20, int(limit)))
    eligible = {
        (str(row["entity_id"]), str(row["target_type"])): float(row["conservative_lower_bound"])
        for row in broker_skills
        if row.get("rank_eligible") is True
    }
    candidates = []
    for row in outcomes:
        key = (str(row.get("broker_id") or ""), str(row.get("target_type") or ""))
        url = str(row.get("report_pdf_url") or "").strip()
        if row.get("outcome_status") != "success" or key not in eligible:
            continue
        if not url.startswith(("https://", "http://")):
            continue
        candidates.append(
            {
                "report_id": str(row["report_id"]),
                "claim_id": str(row["claim_id"]),
                "pdf_url": url,
                "target_type": str(row["target_type"]),
                "broker_id": key[0],
                "source_skill_lower_bound": eligible[key],
                "claim_available_at": str(row["claim_available_at"]),
            }
        )
    candidates.sort(key=lambda row: (str(row["report_id"]), str(row["claim_id"])))
    candidates.sort(key=lambda row: str(row["claim_available_at"]), reverse=True)
    candidates.sort(key=lambda row: float(row["source_skill_lower_bound"]), reverse=True)
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in candidates:
        if row["report_id"] in seen:
            continue
        seen.add(row["report_id"])
        unique.append(row)
        if len(unique) >= resolved_limit:
            break
    return unique


def select_recent_pdf_candidates(
    claims: Sequence[Any],
    reports: Iterable[Any] | Mapping[str, Any],
    broker_skills: Sequence[Mapping[str, Any]],
    *,
    limit: int = 20,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Select recent unscored reading candidates using historical skill only."""

    report_by_id = (
        dict(reports)
        if isinstance(reports, Mapping)
        else {
            str(_get(report, "report_id", default="")): report
            for report in reports
        }
    )
    eligible = {
        (str(row["entity_id"]), str(row["target_type"])): float(
            row["conservative_lower_bound"]
        )
        for row in broker_skills
        if row.get("rank_eligible") is True
    }
    candidates: list[dict[str, Any]] = []
    reading_rows: list[dict[str, Any]] = []
    for claim in claims:
        report = report_by_id.get(str(_get(claim, "report_id", default="")))
        if report is None:
            continue
        target_type = str(_get(claim, "target_type", default="")).strip().lower()
        broker_id = _broker_identity(report)
        skill = eligible.get((broker_id, target_type))
        pdf_url = str(_get(report, "pdf_url", default="") or "").strip()
        if skill is None or not pdf_url.startswith(("https://", "http://")):
            continue
        row = _base_outcome(claim, report)
        row["broker_id"] = broker_id
        reading_rows.append(row)
        candidates.append(
            {
                "report_id": str(_get(claim, "report_id", default="")),
                "claim_id": str(_get(claim, "claim_id", default="")),
                "pdf_url": pdf_url,
                "target_type": target_type,
                "broker_id": broker_id,
                "source_skill_lower_bound": skill,
                "claim_available_at": _normalise(_get(claim, "available_at")),
            }
        )
    candidates.sort(key=lambda row: (str(row["report_id"]), str(row["claim_id"])))
    candidates.sort(key=lambda row: str(row["claim_available_at"]), reverse=True)
    candidates.sort(key=lambda row: float(row["source_skill_lower_bound"]), reverse=True)
    selected: list[dict[str, Any]] = []
    selected_claims: set[str] = set()
    seen_reports: set[str] = set()
    for candidate in candidates:
        if candidate["report_id"] in seen_reports:
            continue
        seen_reports.add(candidate["report_id"])
        selected_claims.add(candidate["claim_id"])
        selected.append(candidate)
        if len(selected) >= max(0, min(20, int(limit))):
            break
    return selected, [
        row for row in reading_rows if str(row["claim_id"]) in selected_claims
    ]


def build_reading_queue(
    pdf_candidates: Sequence[Mapping[str, Any]],
    outcomes: Sequence[Mapping[str, Any]],
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Recommend only candidates whose actual PDF and evidence are present."""

    outcome_by_claim = {str(row["claim_id"]): row for row in outcomes}
    rows: list[dict[str, Any]] = []
    for candidate in pdf_candidates[:20]:
        outcome = outcome_by_claim.get(str(candidate.get("claim_id") or ""))
        if not outcome:
            continue
        pdf_hash = str(outcome.get("report_pdf_sha256") or "").strip().lower()
        evidence_span = str(outcome.get("evidence_span") or "").strip()
        if (
            not re.fullmatch(r"[0-9a-f]{64}", pdf_hash)
            or not evidence_span
            or outcome.get("pdf_evidence_verified") is not True
        ):
            continue
        target = str(outcome["target_type"])
        why_read = (
            f"该来源在 {target} 的 Choice 120日市场结果诊断中具有可排名的保守下界；"
            "需阅读原文核对论据，而不是把诊断相关性当作正式预测能力。"
        )
        might_change = "可能改变对该公司未来120个交易日相对沪深300表现及其证据强弱的判断。"
        if not why_read or not might_change:
            continue
        rows.append(
            {
                "rank": len(rows) + 1,
                "report_id": outcome["report_id"],
                "claim_id": outcome["claim_id"],
                "broker": outcome["broker"],
                "analyst": outcome["analyst"],
                "title": outcome["report_title"],
                "pdf_url": outcome["report_pdf_url"],
                "pdf_sha256": pdf_hash,
                "evidence_span": evidence_span,
                "why_read": why_read,
                "might_change": might_change,
                "diagnostic_status": DIAGNOSTIC_STATUS,
            }
        )
        if len(rows) >= max(0, min(5, int(limit))):
            break
    return rows


def _coverage_rows(
    outcomes: Sequence[Mapping[str, Any]],
    broker_skills: Sequence[Mapping[str, Any]],
    analyst_skills: Sequence[Mapping[str, Any]],
    *,
    incomplete_status: str = "",
) -> list[dict[str, Any]]:
    result = []
    for target_type in (*sorted(CANDIDATE_TARGET_TYPES), "all"):
        rows = list(outcomes) if target_type == "all" else [row for row in outcomes if row["target_type"] == target_type]
        broker = list(broker_skills) if target_type == "all" else [row for row in broker_skills if row["target_type"] == target_type]
        analysts = list(analyst_skills) if target_type == "all" else [row for row in analyst_skills if row["target_type"] == target_type]
        successes = sum(1 for row in rows if row["outcome_status"] == "success")
        result.append(
            {
                "target_type": target_type,
                "candidate_claim_count": len(rows),
                "unique_instrument_count": len(
                    {
                        str(row.get("instrument_id") or row.get("subject_id") or "")
                        for row in rows
                        if str(row.get("instrument_id") or row.get("subject_id") or "")
                    }
                ),
                "successful_market_outcome_count": successes,
                "explicit_exclusion_count": len(rows) - successes,
                "broker_skill_cell_count": len(broker),
                "rank_eligible_broker_cell_count": sum(1 for row in broker if row["rank_eligible"]),
                "analyst_team_skill_cell_count": len(analysts),
                "rank_eligible_analyst_team_cell_count": sum(1 for row in analysts if row["rank_eligible"]),
                "coverage_status": (
                    incomplete_status
                    if incomplete_status
                    else "market_outcomes_available"
                    if successes
                    else "no_scorable_market_outcomes"
                ),
                "diagnostic_status": DIAGNOSTIC_STATUS,
            }
        )
    return result


def _exception_rows(
    outcomes: Sequence[Mapping[str, Any]],
    failures: Mapping[str, Mapping[str, Any]],
    external_exceptions: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    rows = []
    reason_counts = Counter(str(row["exclusion_reason"]) for row in outcomes if row["outcome_status"] == "excluded")
    for reason, count in sorted(reason_counts.items()):
        payload = {
            "stage": "evaluation",
            "status": "failed",
            "code": reason.split(":", 1)[0],
            "entity_id": "",
            "claim_count": count,
            "message": "候选主张未形成可评分的Choice市场结果。",
            "details": {"exclusion_reason": reason},
        }
        rows.append({"exception_id": _sha256_text(_canonical_json(payload)), **payload})
    for entity_id, failure in sorted(failures.items()):
        payload = {
            "stage": "market_data",
            "status": str(failure.get("status") or "failed"),
            "code": str(failure.get("code") or failure.get("status") or "failed"),
            "entity_id": entity_id,
            "claim_count": sum(1 for row in outcomes if row["instrument_id"] == entity_id),
            "message": str(failure.get("message") or "Choice market-data request failed"),
            "details": dict(failure),
        }
        rows.append({"exception_id": _sha256_text(_canonical_json(payload)), **payload})
    for item in external_exceptions:
        payload = {
            "stage": str(item.get("stage") or "pdf"),
            "status": str(item.get("status") or "failed"),
            "code": str(item.get("code") or item.get("status") or "failed"),
            "entity_id": str(item.get("entity_id") or item.get("report_id") or ""),
            "claim_count": int(item.get("claim_count") or 0),
            "message": str(item.get("message") or "bounded PDF enrichment failed"),
            "details": dict(item.get("details") or item),
        }
        rows.append({"exception_id": _sha256_text(_canonical_json(payload)), **payload})
    return sorted(rows, key=lambda row: (str(row["stage"]), str(row["code"]), str(row["entity_id"])))


def _csv_cell(value: Any) -> Any:
    normalized = _normalise(value)
    if normalized is None:
        return ""
    if isinstance(normalized, bool):
        return "true" if normalized else "false"
    if isinstance(normalized, (dict, list)):
        return _canonical_json(normalized)
    return normalized


def _write_csv(path: Path, columns: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: _csv_cell(row.get(column)) for column in columns})


def _render_reading_queue(as_of: str, rows: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        f"# Choice 诊断性研报阅读清单 - {as_of[:10]}",
        "",
        f"- 状态：`{DIAGNOSTIC_STATUS}`",
        "- 上限：最多 5 份；仅保留已有原始 PDF SHA-256、证据段、why_read 与 might_change 的报告。",
        "- 边界：Choice 是聚合型 Secondary；本清单不是正式准确率结论或投资建议。",
        "",
    ]
    if not rows:
        lines.extend(
            [
                "## 暂无合格推荐",
                "",
                "当前没有同时满足诊断技能下界、原始 PDF 和证据完整性门槛的报告；不以标题补位。",
            ]
        )
        return "\n".join(lines) + "\n"
    lines.extend(
        [
            "| 顺序 | 券商/分析师 | 报告 | 为什么读 | 可能改变什么 |",
            "|---:|---|---|---|---|",
        ]
    )
    for row in rows:
        title = str(row["title"] or "-").replace("|", "\\|").replace("\n", " ")
        url = str(row["pdf_url"])
        title_cell = f"[{title}]({url})" if url.startswith(("https://", "http://")) else title
        source = "/".join(item for item in (str(row["broker"]), str(row["analyst"])) if item) or "-"
        lines.append(
            f"| {row['rank']} | {source.replace('|', '\\|')} | {title_cell} | "
            f"{str(row['why_read']).replace('|', '\\|')} | {str(row['might_change']).replace('|', '\\|')} |"
        )
    return "\n".join(lines) + "\n"


def write_choice_diagnostic_bundle(
    output_directory: Path | str,
    *,
    as_of: datetime | date | str,
    outcomes: Sequence[Mapping[str, Any]],
    broker_skills: Sequence[Mapping[str, Any]],
    analyst_skills: Sequence[Mapping[str, Any]],
    collection: ChoiceCollection,
    pdf_candidates: Sequence[Mapping[str, Any]],
    reading_queue: Sequence[Mapping[str, Any]],
    parameters: Mapping[str, Any] | None = None,
    external_exceptions: Sequence[Mapping[str, Any]] = (),
) -> ChoiceDiagnosticBundle:
    """Write the deterministic independent seven-file diagnostic bundle."""

    if len(pdf_candidates) > 20:
        raise ChoiceDiagnosticError("diagnostic PDF candidate set cannot exceed 20")
    if len(reading_queue) > 5:
        raise ChoiceDiagnosticError("diagnostic reading queue cannot exceed 5")
    incomplete_status = _collection_rank_status(collection.failures)
    quota_truncated = _quota_truncated(collection.failures)
    if incomplete_status:
        broker_skills = _suppress_incomplete_collection_rankings(
            broker_skills, status=incomplete_status
        )
        analyst_skills = _suppress_incomplete_collection_rankings(
            analyst_skills, status=incomplete_status
        )
        pdf_candidates = []
        reading_queue = []
        exception_code = (
            "choice_quota_truncated_not_rankable"
            if quota_truncated
            else "choice_collection_incomplete_not_rankable"
        )
        external_exceptions = (
            *external_exceptions,
            {
                "stage": "coverage",
                "status": "failed",
                "code": exception_code,
                "entity_id": "candidate_population",
                "claim_count": len(outcomes),
                "message": (
                    "Choice collection is incomplete under an ordered instrument "
                    "population; accuracy statistics, rankings, and reading "
                    "recommendations are suppressed"
                ),
                "details": {
                    "collection_incomplete": True,
                    "quota_truncated": quota_truncated,
                    "coverage_status": incomplete_status,
                },
            },
        )
    outcomes = [
        {
            **dict(row),
            "diagnostic_status": DIAGNOSTIC_STATUS,
            "truth_eligible": False,
        }
        for row in outcomes
    ]
    if any(
        row.get("outcome_status") not in {"success", "excluded"}
        or row.get("outcome_status") == "excluded"
        and not str(row.get("exclusion_reason") or "")
        for row in outcomes
    ):
        raise ChoiceDiagnosticError(
            "diagnostic outcomes must be success or carry an explicit exclusion"
        )
    broker_skills = [
        {**dict(row), "diagnostic_status": DIAGNOSTIC_STATUS}
        for row in broker_skills
    ]
    analyst_skills = [
        {**dict(row), "diagnostic_status": DIAGNOSTIC_STATUS}
        for row in analyst_skills
    ]
    resolved_parameters = dict(parameters or {})
    recommendation_limit = max(
        0, min(5, int(resolved_parameters.get("max_recommendations", 5)))
    )
    if len(reading_queue) > recommendation_limit:
        raise ChoiceDiagnosticError(
            "diagnostic reading queue exceeds the configured recommendation limit"
        )
    candidate_ids = {
        (str(row.get("report_id") or ""), str(row.get("claim_id") or ""))
        for row in pdf_candidates
    }
    reading_queue = [dict(row) for row in reading_queue]
    for index, row in enumerate(reading_queue, start=1):
        if (
            (str(row.get("report_id") or ""), str(row.get("claim_id") or ""))
            not in candidate_ids
            or int(row.get("rank") or 0) != index
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(row.get("pdf_sha256") or "").lower()
            )
            or not str(row.get("evidence_span") or "").strip()
            or not str(row.get("why_read") or "").strip()
            or not str(row.get("might_change") or "").strip()
            or row.get("diagnostic_status") != DIAGNOSTIC_STATUS
        ):
            raise ChoiceDiagnosticError(
                "diagnostic reading queue contains unbound or incomplete evidence"
            )
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    paths = {name: output / name for name in CHOICE_ARTIFACT_FILENAMES}
    coverage = _coverage_rows(
        outcomes,
        broker_skills,
        analyst_skills,
        incomplete_status=incomplete_status,
    )
    repository_commit, working_tree_dirty, git_diff_sha256 = git_worktree_state(
        REPOSITORY_ROOT
    )
    if working_tree_dirty is None:
        raise ChoiceDiagnosticError(
            "Git working-tree state is unavailable; diagnostic bundle generation is refused"
        )
    if working_tree_dirty and not git_diff_sha256:
        raise ChoiceDiagnosticError(
            "dirty working tree cannot generate a diagnostic bundle without git_diff_sha256"
        )
    exceptions = _exception_rows(
        outcomes,
        collection.failures,
        external_exceptions=external_exceptions,
    )
    _write_csv(paths["choice_claim_market_outcomes.csv"], OUTCOME_COLUMNS, list(outcomes))
    _write_csv(paths["choice_broker_accuracy.csv"], SKILL_COLUMNS, list(broker_skills))
    _write_csv(paths["choice_analyst_accuracy.csv"], SKILL_COLUMNS, list(analyst_skills))
    _write_csv(paths["choice_source_coverage.csv"], COVERAGE_COLUMNS, coverage)
    _write_csv(paths["choice_exceptions.csv"], EXCEPTION_COLUMNS, exceptions)
    paths["choice_reading_queue.md"].write_text(
        _render_reading_queue(_normalise(_as_of(as_of)), reading_queue),
        encoding="utf-8",
    )
    output_hashes = {
        name: _sha256_file(path)
        for name, path in paths.items()
        if name != "run_manifest.json"
    }
    source_batches = [
        evidence
        for evidence in (
            _batch_evidence(collection.calendar_batch),
            _batch_evidence(collection.benchmark_batch),
            *(_batch_evidence(batch) for _, batch in sorted(collection.stock_batches.items())),
        )
        if evidence
    ]
    source_batches.sort(key=_canonical_json)
    population_hash = _sha256_text(_canonical_json(list(outcomes)))
    run_identity = {
        "schema_version": "choice-market-diagnostic.v1",
        "diagnostic_status": DIAGNOSTIC_STATUS,
        "as_of": _normalise(_as_of(as_of)),
        "candidate_population_sha256": population_hash,
        "repository_commit": repository_commit,
        "working_tree_dirty_at_generation": working_tree_dirty,
        "git_diff_sha256": git_diff_sha256,
        "parameters": _normalise(resolved_parameters),
        "bounded_pdf_candidates": _normalise(list(pdf_candidates)),
        "source_batches": source_batches,
        "output_sha256": output_hashes,
    }
    run_id = _sha256_text(_canonical_json(run_identity))
    manifest = {
        **run_identity,
        "run_id": run_id,
        "formal_truth_eligible": False,
        "formal_audit_artifacts_modified": False,
        "provider_role": "validated_secondary_not_primary",
        "benchmark": BENCHMARK_INSTRUMENT,
        "stock_adjustment": "qfq",
        "benchmark_adjustment": "none",
        "evaluation_target": "next_trading_session_open_to_120th_session_close",
        "absolute_target_achievement": "not_evaluated",
        "collection_incomplete": bool(incomplete_status),
        "quota_truncated": quota_truncated,
        "counts": {
            "candidate_claims": len(outcomes),
            "successful_outcomes": sum(1 for row in outcomes if row["outcome_status"] == "success"),
            "explicit_exclusions": sum(1 for row in outcomes if row["outcome_status"] == "excluded"),
            "stock_batches": len(collection.stock_batches),
            "request_failures": len(collection.failures),
            "broker_skill_cells": len(broker_skills),
            "analyst_team_skill_cells": len(analyst_skills),
            "bounded_pdf_candidates": len(pdf_candidates),
            "reading_recommendations": len(reading_queue),
            "external_exceptions": len(external_exceptions),
        },
        "artifact_names": list(CHOICE_ARTIFACT_FILENAMES),
        "artifacts": [
            {"name": name, "sha256": output_hashes[name]}
            for name in CHOICE_ARTIFACT_FILENAMES
            if name != "run_manifest.json"
        ],
        "manifest_hash_policy": "manifest excludes its own hash",
    }
    paths["run_manifest.json"].write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    hashes = {**output_hashes, "run_manifest.json": _sha256_file(paths["run_manifest.json"])}
    return ChoiceDiagnosticBundle(
        run_id=run_id,
        output_directory=output,
        paths=paths,
        hashes=hashes,
        pdf_candidates=tuple(dict(item) for item in pdf_candidates),
    )


def run_choice_market_diagnostic(
    *,
    db_path: Path | str,
    cache_directory: Path | str,
    output_directory: Path | str,
    as_of: datetime | date | str,
    offline: bool = False,
    resume: bool = True,
    max_requests_per_minute: int = 300,
    max_pdf_candidates: int = 20,
    max_recommendations: int = 5,
    registry: Any | None = None,
    requested_at: datetime | None = None,
    config: Mapping[str, Any] | None = None,
    sample_start: date | str | None = None,
    sample_end: date | str | None = None,
    recent_candidate_days: int = 210,
    pdf_enricher: Callable[
        [Sequence[Mapping[str, Any]]], Mapping[str, Mapping[str, Any]]
    ]
    | None = None,
) -> ChoiceDiagnosticBundle:
    """Standard callable used by the ``diagnostic-market`` CLI command."""

    cutoff = _as_of(as_of)
    dates_policy = config.get("dates", {}) if isinstance(config, Mapping) else {}
    if not isinstance(dates_policy, Mapping):
        dates_policy = {}
    resolved_sample_start = date.fromisoformat(
        str(sample_start or dates_policy.get("sample_start") or "2024-07-01")
    )
    resolved_sample_end = date.fromisoformat(
        str(sample_end or dates_policy.get("sample_end") or "2025-06-30")
    )
    if resolved_sample_start > resolved_sample_end:
        raise ChoiceDiagnosticError("sample_start cannot follow sample_end")
    if type(recent_candidate_days) is not int or not 1 <= recent_candidate_days <= 366:
        raise ChoiceDiagnosticError("recent_candidate_days must be in [1, 366]")
    recent_end = cutoff.astimezone(CHINA_TZ).date()
    recent_start, recent_end = _reading_candidate_window(
        sample_end=resolved_sample_end,
        as_of_day=recent_end,
        recent_candidate_days=recent_candidate_days,
    )
    with AuditStore(db_path, decision_time=cutoff) as store:
        all_reports = list(
            store.iter_reports(available_by=cutoff, version_as_of=cutoff)
        )
        reports = [
            report
            for report in all_reports
            if resolved_sample_start
            <= _aware(_get(report, "published_at")).astimezone(CHINA_TZ).date()
            <= resolved_sample_end
        ]
        report_ids = {str(_get(report, "report_id", default="")) for report in reports}
        claims = select_choice_candidate_claims(
            (
                claim
                for claim in store.iter_claims(dimension="stock", available_by=cutoff)
                if str(_get(claim, "report_id", default="")) in report_ids
            ),
            as_of=cutoff,
            reports=reports,
        )
        recent_reports = [
            report
            for report in all_reports
            if str(_get(report, "dimension", default="")).lower() == "stock"
            and recent_start
            <= _aware(_get(report, "published_at")).astimezone(CHINA_TZ).date()
            <= recent_end
        ]
        recent_report_ids = {
            str(_get(report, "report_id", default="")) for report in recent_reports
        }
        recent_claims = select_choice_candidate_claims(
            (
                claim
                for claim in store.iter_claims(
                    dimension="stock", available_by=cutoff
                )
                if str(_get(claim, "report_id", default="")) in recent_report_ids
            ),
            as_of=cutoff,
            reports=recent_reports,
        )
    resolved_registry = registry or MarketDataRegistry.configured(
        storage_root=Path(cache_directory) / "market_data_registry"
    )
    collection = collect_choice_market_data(
        resolved_registry,
        claims,
        as_of=cutoff,
        checkpoint_path=Path(cache_directory) / "choice_diagnostic_checkpoint.json",
        offline=offline,
        resume=resume,
        max_requests_per_minute=max_requests_per_minute,
        requested_at=requested_at,
        reports=reports,
    )
    outcomes = compute_choice_market_outcomes(claims, reports, collection, as_of=cutoff)
    broker_skills, analyst_skills = build_choice_skill_tables(outcomes, as_of=cutoff)
    incomplete_status = _collection_rank_status(collection.failures)
    quota_truncated = _quota_truncated(collection.failures)
    if incomplete_status:
        broker_skills = _suppress_incomplete_collection_rankings(
            broker_skills, status=incomplete_status
        )
        analyst_skills = _suppress_incomplete_collection_rankings(
            analyst_skills, status=incomplete_status
        )
        pdf_candidates, reading_rows = [], []
    else:
        pdf_candidates, reading_rows = select_recent_pdf_candidates(
            recent_claims,
            recent_reports,
            broker_skills,
            limit=max_pdf_candidates,
        )
    pdf_exceptions: list[dict[str, Any]] = []
    if pdf_enricher is not None and not offline and pdf_candidates:
        try:
            enriched = pdf_enricher(tuple(pdf_candidates))
            if not isinstance(enriched, Mapping):
                raise ChoiceDiagnosticError("pdf_enricher must return a report_id mapping")
        except Exception as exc:
            failure = _failure(exc)
            enriched = {}
            pdf_exceptions.append(
                {
                    "stage": "pdf",
                    "status": failure["status"],
                    "code": failure["code"],
                    "entity_id": "bounded_set",
                    "claim_count": 0,
                    "message": failure["message"],
                    "details": failure,
                }
            )
        by_report: dict[str, dict[str, Any]] = {}
        for key, value in enriched.items():
            if not isinstance(value, Mapping):
                by_report[str(key)] = {
                    "status": "failed",
                    "code": "pdf_result_contract_invalid",
                    "message": "pdf_enricher result value is not a mapping",
                }
            else:
                by_report[str(key)] = dict(value)
        existing_pdf_by_report = {
            str(row["report_id"]): str(row.get("report_pdf_sha256") or "").strip().lower()
            for row in reading_rows
        }
        for candidate in pdf_candidates:
            report_id = str(candidate["report_id"])
            if report_id in by_report or re.fullmatch(
                r"[0-9a-f]{64}", existing_pdf_by_report.get(report_id, "")
            ):
                continue
            pdf_exceptions.append(
                {
                    "stage": "pdf",
                    "status": "failed",
                    "code": "pdf_result_missing",
                    "entity_id": report_id,
                    "claim_count": 1,
                    "message": "bounded PDF enrichment returned no result for the candidate",
                    "details": {"report_id": report_id},
                }
            )
        updated_reading_rows: list[dict[str, Any]] = []
        invalid_pdf_reports: set[str] = set()
        for outcome in reading_rows:
            update = by_report.get(str(outcome["report_id"]))
            resolved = dict(outcome)
            if update:
                pdf_hash = str(update.get("pdf_sha256") or "").strip().lower()
                if re.fullmatch(r"[0-9a-f]{64}", pdf_hash):
                    resolved["report_pdf_sha256"] = pdf_hash
                    extracted_span = str(update.get("evidence_span") or "").strip()
                    if extracted_span:
                        resolved["evidence_span"] = extracted_span
                        resolved["pdf_evidence_verified"] = True
                        resolved["claim_evidence_source_kind"] = "textual/pdf"
                        resolved["claim_evidence_source_hash"] = pdf_hash
                else:
                    report_id = str(outcome["report_id"])
                    if report_id not in invalid_pdf_reports:
                        invalid_pdf_reports.add(report_id)
                        pdf_exceptions.append(
                            {
                                "stage": "pdf",
                                "status": str(update.get("status") or "failed"),
                                "code": str(update.get("code") or "pdf_sha256_missing"),
                                "entity_id": report_id,
                                "claim_count": 1,
                                "message": str(update.get("message") or "bounded PDF did not produce a valid SHA-256"),
                                "details": update,
                            }
                        )
            updated_reading_rows.append(resolved)
        reading_rows = updated_reading_rows
    reading_queue = build_reading_queue(
        pdf_candidates, reading_rows, limit=max_recommendations
    )
    return write_choice_diagnostic_bundle(
        output_directory,
        as_of=cutoff,
        outcomes=outcomes,
        broker_skills=broker_skills,
        analyst_skills=analyst_skills,
        collection=collection,
        pdf_candidates=pdf_candidates,
        reading_queue=reading_queue,
        parameters={
            "sample_start": resolved_sample_start.isoformat(),
            "sample_end": resolved_sample_end.isoformat(),
            "reading_candidate_start": recent_start.isoformat(),
            "reading_candidate_end": recent_end.isoformat(),
            "reading_candidate_claim_count": len(recent_claims),
            "offline": bool(offline),
            "resume": bool(resume),
            "max_sdk_requests_per_minute": max_requests_per_minute,
            "max_pdf_candidates": max(0, min(20, int(max_pdf_candidates))),
            "max_recommendations": max(0, min(5, int(max_recommendations))),
            "collection_incomplete": bool(incomplete_status),
            "incomplete_coverage_status": incomplete_status,
            "quota_truncated": quota_truncated,
            "config_sha256": _sha256_text(_canonical_json(config or {})),
            "candidate_claim_contract": "active_structured_source_record_only",
            "candidate_claim_count": len(claims),
            "candidate_claim_population_sha256": _sha256_text(
                _canonical_json([_normalise(claim) for claim in claims])
            ),
        },
        external_exceptions=pdf_exceptions,
    )


__all__ = [
    "BENCHMARK_INSTRUMENT",
    "CANDIDATE_TARGET_TYPES",
    "CHOICE_ARTIFACT_FILENAMES",
    "ChoiceCollection",
    "ChoiceDiagnosticBundle",
    "ChoiceDiagnosticError",
    "DIAGNOSTIC_STATUS",
    "EVALUATED_HORIZON_DAYS",
    "SlidingWindowRateLimiter",
    "build_choice_skill_tables",
    "build_reading_queue",
    "collect_choice_market_data",
    "compute_choice_market_outcomes",
    "run_choice_market_diagnostic",
    "select_choice_candidate_claims",
    "select_pdf_candidates",
    "select_recent_pdf_candidates",
    "write_choice_diagnostic_bundle",
]
