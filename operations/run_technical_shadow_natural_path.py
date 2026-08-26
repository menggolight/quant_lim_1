"""Run the earliest real, naturally non-zero Technical Shadow execution path."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

from operations.diagnose_technical_shadow_exposure import (
    DIAGNOSTIC_DAYS,
    PURPOSE as DIAGNOSTIC_PURPOSE,
)
from operations.run_technical_shadow_mvp import (
    ALPHA_LOOKBACK_SESSIONS,
    BaoStockTechnicalShadowSource,
    CHINA_TZ,
    CapturedData,
    DEFAULT_CONFIG,
    TechnicalShadowRunError,
    _digest,
    _load_config,
    run_replay,
)


PRELUDE_SESSIONS = 5
MAX_DECISION_SESSIONS = 20
DEFAULT_OUTPUT_ROOT = Path("data/tmp/technical-shadow-natural-path")
DIAGNOSTIC_MANIFEST_SCHEMA = (
    "technical-shadow-exposure-diagnostic-manifest.v1"
)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TechnicalShadowRunError(
            f"invalid_exposure_diagnostic_artifact:{path.name}"
        ) from exc


def load_verified_exposure_diagnostic(
    run_root: Path,
    *,
    expected_strategy_id: str,
    expected_config_sha256: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    root = run_root.resolve()
    manifest_path = root / "run_manifest.json"
    manifest = _read_json(manifest_path)
    if manifest.get("schema_version") != DIAGNOSTIC_MANIFEST_SCHEMA:
        raise TechnicalShadowRunError("wrong_exposure_diagnostic_schema")
    if manifest.get("purpose") != DIAGNOSTIC_PURPOSE:
        raise TechnicalShadowRunError("wrong_exposure_diagnostic_purpose")
    if manifest.get("strategy_id") != expected_strategy_id:
        raise TechnicalShadowRunError("wrong_exposure_diagnostic_strategy")
    if manifest.get("config_sha256") != expected_config_sha256:
        raise TechnicalShadowRunError("exposure_diagnostic_config_drifted")
    provider = manifest.get("provider", {})
    if (
        provider.get("provider_id") != "baostock"
        or provider.get("provider_kind") != "real_provider"
        or bool(provider.get("synthetic"))
    ):
        raise TechnicalShadowRunError("exposure_diagnostic_not_real_baostock")
    if not manifest.get("safety") or any(manifest["safety"].values()):
        raise TechnicalShadowRunError("exposure_diagnostic_safety_drifted")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise TechnicalShadowRunError("exposure_diagnostic_artifacts_missing")
    required_artifacts = {"exposure_daily.jsonl", "exposure_summary.json"}
    if not required_artifacts.issubset(artifacts):
        raise TechnicalShadowRunError(
            "exposure_diagnostic_required_artifacts_unbound"
        )
    for relative, expected in artifacts.items():
        candidate = (root / str(relative)).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise TechnicalShadowRunError(
                "exposure_diagnostic_artifact_path_escape"
            ) from exc
        if not candidate.is_file():
            raise TechnicalShadowRunError(
                f"exposure_diagnostic_artifact_missing:{relative}"
            )
        actual = sha256(candidate.read_bytes()).hexdigest()
        if actual != str(expected):
            raise TechnicalShadowRunError(
                f"exposure_diagnostic_artifact_hash_mismatch:{relative}"
            )

    summary = _read_json(root / "exposure_summary.json")
    if _digest(summary) != manifest.get("summary_sha256"):
        raise TechnicalShadowRunError("exposure_diagnostic_summary_hash_mismatch")
    daily_path = root / "exposure_daily.jsonl"
    try:
        daily = [
            json.loads(line)
            for line in daily_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TechnicalShadowRunError(
            "invalid_exposure_diagnostic_daily_jsonl"
        ) from exc
    if len(daily) != DIAGNOSTIC_DAYS:
        raise TechnicalShadowRunError("exposure_diagnostic_not_120_days")
    dates = [date.fromisoformat(str(row["decision_date"])) for row in daily]
    if dates != sorted(set(dates)):
        raise TechnicalShadowRunError("exposure_diagnostic_dates_not_unique_sorted")
    manifest_sha = sha256(manifest_path.read_bytes()).hexdigest()
    return summary, daily, manifest_sha


def select_natural_window(
    *,
    daily: Sequence[Mapping[str, Any]],
    captured: CapturedData,
    prelude_sessions: int = PRELUDE_SESSIONS,
    max_decision_sessions: int = MAX_DECISION_SESSIONS,
) -> tuple[CapturedData, int, dict[str, Any]]:
    if prelude_sessions < PRELUDE_SESSIONS:
        raise TechnicalShadowRunError("natural_path_requires_five_session_prelude")
    if not 1 <= max_decision_sessions <= MAX_DECISION_SESSIONS:
        raise TechnicalShadowRunError("natural_path_decision_limit_invalid")
    nonzero = [
        row
        for row in daily
        if not bool(row.get("data_fail_closed"))
        and float(row.get("target_gross_exposure", 0)) > 0
    ]
    if not nonzero:
        raise TechnicalShadowRunError(
            "no_natural_nonzero_exposure_use_isolated_execution_diagnostic"
        )
    anchor = date.fromisoformat(str(nonzero[0]["decision_date"]))
    sessions = tuple(captured.sessions)
    if sessions != tuple(sorted(set(sessions))):
        raise TechnicalShadowRunError("natural_path_sessions_not_unique_sorted")
    try:
        anchor_index = sessions.index(anchor)
    except ValueError as exc:
        raise TechnicalShadowRunError(
            "natural_path_anchor_not_in_fresh_capture"
        ) from exc
    start_index = anchor_index - prelude_sessions
    if start_index < ALPHA_LOOKBACK_SESSIONS:
        raise TechnicalShadowRunError("natural_path_prelude_history_insufficient")
    last_executable_decision_index = len(sessions) - 2
    end_index = min(
        start_index + max_decision_sessions - 1,
        last_executable_decision_index,
    )
    decision_count = end_index - start_index + 1
    selected_sessions = sessions[
        start_index - ALPHA_LOOKBACK_SESSIONS:end_index + 2
    ]
    expected = ALPHA_LOOKBACK_SESSIONS + decision_count + 1
    if len(selected_sessions) != expected:
        raise TechnicalShadowRunError("natural_path_selected_calendar_mismatch")
    selection = {
        "run_mode": "natural_execution_path_acceptance",
        "selection_anchor_semantics": (
            "earliest_nonzero_flat_cash_counterfactual_target_in_verified_120_day_diagnostic"
        ),
        "selection_anchor_date": anchor.isoformat(),
        "prelude_session_count": prelude_sessions,
        "first_decision_date": sessions[start_index].isoformat(),
        "maximum_decision_count": max_decision_sessions,
        "last_possible_decision_date": sessions[end_index].isoformat(),
        "stop_policy": (
            "first_sell_from_selection_anchor_onward_or_20_decisions"
        ),
        "prelude_trades_count_for_account_state": True,
        "prelude_sells_count_for_stop": False,
        "strategy_signal_forced": False,
        "exposure_overridden": False,
        "alpha_overridden": False,
    }
    return replace(captured, sessions=selected_sessions), decision_count, selection


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exposure-diagnostic-run", type=Path, required=True)
    parser.add_argument("--initial-cash", type=Decimal, default=Decimal("10000"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.initial_cash <= 0:
        raise TechnicalShadowRunError("initial_cash_must_be_positive")
    config = _load_config(args.config)
    _, daily, diagnostic_manifest_sha = load_verified_exposure_diagnostic(
        args.exposure_diagnostic_run,
        expected_strategy_id=config["strategy_id"],
        expected_config_sha256=_digest(config),
    )
    source = BaoStockTechnicalShadowSource()
    captured = source.capture(
        instrument_ids=config["universe"]["instrument_ids"],
        benchmark_id=config["data"]["benchmark_id"],
        recent_completed_sessions=DIAGNOSTIC_DAYS + PRELUDE_SESSIONS,
        lookback_days=int(config["data"]["calendar_lookback_days"]),
        now=datetime.now(CHINA_TZ),
    )
    selected, decision_count, selection = select_natural_window(
        daily=daily,
        captured=captured,
    )
    selection["exposure_diagnostic_manifest_sha256"] = diagnostic_manifest_sha
    run_root, summary = run_replay(
        config=config,
        captured=selected,
        recent_completed_sessions=decision_count,
        initial_cash=args.initial_cash,
        output_root=args.output_root,
        stop_after_first_sell=True,
        sell_stop_eligible_from_decision_date=date.fromisoformat(
            selection["selection_anchor_date"]
        ),
        run_context=selection,
    )
    print(json.dumps(
        {
            "output_directory": str(run_root.resolve()),
            "buy_observed": summary["buy_count"] > 0,
            "sell_observed": summary["sell_count"] > 0,
            "summary": summary,
        },
        ensure_ascii=False,
        sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MAX_DECISION_SESSIONS",
    "PRELUDE_SESSIONS",
    "load_verified_exposure_diagnostic",
    "select_natural_window",
]
