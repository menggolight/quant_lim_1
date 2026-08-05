"""CLI health probe for the read-only HTSC MQuant snapshot bridge."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from trading.brokers.htsc_mquant_shadow import (
    HtscMQuantShadowAdapter,
    SnapshotValidationError,
)


@dataclass(frozen=True)
class ShadowProbeConfig:
    status: str
    snapshot_path: Path
    expected_account_binding_id: str | None
    expected_account_fingerprint: str | None
    expected_api_shape_id: str | None
    max_snapshot_age_seconds: int


def _exact_boolean(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field} must be a JSON boolean")
    return value


def load_shadow_probe_config(path: Path | str) -> ShadowProbeConfig:
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    expected_keys = {
        "config_version",
        "status",
        "required_snapshot_schema",
        "read_only",
        "orders_enabled",
        "expected_account_binding_id",
        "expected_account_fingerprint",
        "expected_api_shape_id",
        "snapshot_path",
        "max_snapshot_age_seconds",
        "require_complete_snapshot",
        "require_payload_sha256",
    }
    if set(payload) != expected_keys:
        raise ValueError("HTSC shadow config fields do not match the required contract")
    if payload.get("config_version") != "htsc-mquant-shadow-config/1":
        raise ValueError("unsupported HTSC shadow config")
    status = payload.get("status")
    if status not in {
        "blocked_pending_client_authorization",
        "read_only",
        "shadow_only",
    }:
        raise ValueError("invalid HTSC shadow status")
    if _exact_boolean(payload.get("read_only"), "read_only") is not True:
        raise ValueError("HTSC shadow config must remain read-only")
    if _exact_boolean(payload.get("orders_enabled"), "orders_enabled") is not False:
        raise ValueError("HTSC shadow config cannot enable orders")
    if payload.get("required_snapshot_schema") != "htsc-mquant-shadow/1":
        raise ValueError("unexpected HTSC snapshot schema")
    expected = payload.get("expected_account_binding_id")
    if expected is not None and type(expected) is not str:
        raise ValueError("expected_account_binding_id must be a string or null")
    expected_fingerprint = payload.get("expected_account_fingerprint")
    if expected_fingerprint is not None and type(expected_fingerprint) is not str:
        raise ValueError("expected_account_fingerprint must be a string or null")
    expected_shape = payload.get("expected_api_shape_id")
    if expected_shape is not None and type(expected_shape) is not str:
        raise ValueError("expected_api_shape_id must be a string or null")
    if _exact_boolean(
        payload.get("require_complete_snapshot"), "require_complete_snapshot"
    ) is not True:
        raise ValueError("complete snapshots are mandatory")
    if _exact_boolean(
        payload.get("require_payload_sha256"), "require_payload_sha256"
    ) is not True:
        raise ValueError("payload hash verification is mandatory")
    max_age = payload.get("max_snapshot_age_seconds")
    if type(max_age) is not int or max_age <= 0:
        raise ValueError("max_snapshot_age_seconds must be a positive integer")
    snapshot_path = Path(str(payload.get("snapshot_path", "")))
    if not snapshot_path.is_absolute():
        raise ValueError("snapshot_path must be absolute")
    return ShadowProbeConfig(
        status,
        snapshot_path,
        expected,
        expected_fingerprint,
        expected_shape,
        max_age,
    )


def probe(config: ShadowProbeConfig, now: datetime) -> dict[str, Any]:
    snapshot = HtscMQuantShadowAdapter(
        config.snapshot_path,
        expected_account_binding_id=config.expected_account_binding_id,
        expected_account_fingerprint=config.expected_account_fingerprint,
        expected_api_shape_id=config.expected_api_shape_id,
        max_age_seconds=config.max_snapshot_age_seconds,
    ).read_snapshot(now)
    if config.status == "blocked_pending_client_authorization":
        probe_status = "blocked_pending_client_authorization"
    elif not snapshot.account_binding_matched:
        probe_status = "enrollment_only"
    elif not snapshot.shape_checked:
        probe_status = "blocked_shape_unchecked"
    else:
        probe_status = "validated_local_snapshot_untrusted_source"
    return {
        "probe_status": probe_status,
        "configured_stage": config.status,
        "orders_enabled": False,
        "account_binding_id": snapshot.account_binding_id,
        "account_fingerprint": snapshot.account_fingerprint,
        "account_binding_matched": snapshot.account_binding_matched,
        "api_shape_id": snapshot.api_shape_id,
        "shape_checked": snapshot.shape_checked,
        "source_authenticated": snapshot.source_authenticated,
        "capture_consistency": snapshot.capture_consistency,
        "completed_at": snapshot.completed_at.isoformat(),
        "sequence": snapshot.sequence,
        "available_cash": str(snapshot.funds.available_cash),
        "total_value": str(snapshot.funds.total_value),
        "position_count": len(snapshot.positions),
        "open_order_count": len(snapshot.open_orders),
        "today_order_count": len(snapshot.today_orders),
        "trade_count": len(snapshot.trades),
        "warnings": list(snapshot.warnings),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/htsc_mquant_shadow.example.json"),
    )
    parser.add_argument(
        "--now",
        help="ISO-8601 time for deterministic diagnostics; defaults to local current time",
    )
    args = parser.parse_args(argv)
    try:
        config = load_shadow_probe_config(args.config)
        now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else datetime.now().astimezone()
        if now.tzinfo is None:
            raise ValueError("--now must include a timezone")
        result = probe(config, now)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, SnapshotValidationError) as exc:
        print(
            json.dumps(
                {
                    "probe_status": "blocked",
                    "orders_enabled": False,
                    "reason": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
