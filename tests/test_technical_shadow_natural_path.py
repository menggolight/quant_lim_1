from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from operations.diagnose_technical_shadow_exposure import PURPOSE, SAFETY
from operations.run_technical_shadow_mvp import (
    CapturedData,
    TechnicalShadowRunError,
    _digest,
)
from operations.run_technical_shadow_natural_path import (
    load_verified_exposure_diagnostic,
    select_natural_window,
)


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


class NaturalPathSelectionTests(unittest.TestCase):
    strategy_id = "a-share-technical-shadow-mvp-v1"
    config_sha256 = "f" * 64

    def test_verified_diagnostic_and_five_day_prelude_window(self):
        session_start = date(2025, 1, 1)
        sessions = tuple(
            session_start + timedelta(days=index) for index in range(246)
        )
        diagnostic_rows = [
            {
                "decision_date": day.isoformat(),
                "target_gross_exposure": 0.30 if index == 3 else 0.0,
                "data_fail_closed": False,
            }
            for index, day in enumerate(sessions[-120:])
        ]
        summary = {"decision_day_count": 120}
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary)
            summary_raw = _canonical(summary)
            daily_raw = b"".join(_canonical(row) for row in diagnostic_rows)
            (run_root / "exposure_summary.json").write_bytes(summary_raw)
            (run_root / "exposure_daily.jsonl").write_bytes(daily_raw)
            artifacts = {
                "exposure_daily.jsonl": hashlib.sha256(daily_raw).hexdigest(),
                "exposure_summary.json": hashlib.sha256(summary_raw).hexdigest(),
            }
            manifest = {
                "schema_version": "technical-shadow-exposure-diagnostic-manifest.v1",
                "purpose": PURPOSE,
                "strategy_id": self.strategy_id,
                "config_sha256": self.config_sha256,
                "provider": {
                    "provider_id": "baostock",
                    "provider_kind": "real_provider",
                    "synthetic": False,
                },
                "artifacts": artifacts,
                "summary_sha256": _digest(summary),
                "safety": SAFETY,
            }
            (run_root / "run_manifest.json").write_bytes(_canonical(manifest))
            _, loaded_daily, _ = load_verified_exposure_diagnostic(
                run_root,
                expected_strategy_id=self.strategy_id,
                expected_config_sha256=self.config_sha256,
            )
            captured = CapturedData(
                provider_id="baostock",
                provider_kind="real_provider",
                adapter_version="test",
                synthetic=False,
                captured_at="2026-08-26T18:00:00+08:00",
                sessions=sessions,
                stock_rows={},
                benchmark_rows=(),
                receipts={},
            )
            selected, count, selection = select_natural_window(
                daily=loaded_daily,
                captured=captured,
            )
            anchor_index = sessions.index(sessions[-117])
            expected_first_decision = sessions[anchor_index - 5]
            self.assertEqual(count, 20)
            self.assertEqual(len(selected.sessions), 141)
            self.assertEqual(selected.sessions[120], expected_first_decision)
            self.assertEqual(selection["prelude_session_count"], 5)
            self.assertFalse(selection["strategy_signal_forced"])
            self.assertFalse(selection["exposure_overridden"])
            self.assertFalse(selection["alpha_overridden"])

    def test_tampered_diagnostic_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary)
            summary = {"decision_day_count": 120}
            summary_raw = _canonical(summary)
            daily_raw = b"{}\n"
            (run_root / "exposure_summary.json").write_bytes(summary_raw)
            (run_root / "exposure_daily.jsonl").write_bytes(daily_raw)
            manifest = {
                "schema_version": "technical-shadow-exposure-diagnostic-manifest.v1",
                "purpose": PURPOSE,
                "strategy_id": self.strategy_id,
                "config_sha256": self.config_sha256,
                "provider": {
                    "provider_id": "baostock",
                    "provider_kind": "real_provider",
                    "synthetic": False,
                },
                "artifacts": {
                    "exposure_summary.json": "0" * 64,
                    "exposure_daily.jsonl": hashlib.sha256(daily_raw).hexdigest(),
                },
                "summary_sha256": _digest(summary),
                "safety": SAFETY,
            }
            (run_root / "run_manifest.json").write_bytes(_canonical(manifest))
            with self.assertRaisesRegex(
                TechnicalShadowRunError,
                "artifact_hash_mismatch",
            ):
                load_verified_exposure_diagnostic(
                    run_root,
                    expected_strategy_id=self.strategy_id,
                    expected_config_sha256=self.config_sha256,
                )

    def test_diagnostic_must_bind_current_frozen_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary)
            summary = {"decision_day_count": 120}
            summary_raw = _canonical(summary)
            daily_rows = [
                {
                    "decision_date": (
                        date(2026, 1, 1) + timedelta(days=index)
                    ).isoformat(),
                    "target_gross_exposure": 0.0,
                    "data_fail_closed": False,
                }
                for index in range(120)
            ]
            daily_raw = b"".join(_canonical(row) for row in daily_rows)
            (run_root / "exposure_summary.json").write_bytes(summary_raw)
            (run_root / "exposure_daily.jsonl").write_bytes(daily_raw)
            manifest = {
                "schema_version": "technical-shadow-exposure-diagnostic-manifest.v1",
                "purpose": PURPOSE,
                "strategy_id": self.strategy_id,
                "config_sha256": "0" * 64,
                "provider": {
                    "provider_id": "baostock",
                    "provider_kind": "real_provider",
                    "synthetic": False,
                },
                "artifacts": {
                    "exposure_summary.json": hashlib.sha256(summary_raw).hexdigest(),
                    "exposure_daily.jsonl": hashlib.sha256(daily_raw).hexdigest(),
                },
                "summary_sha256": _digest(summary),
                "safety": SAFETY,
            }
            (run_root / "run_manifest.json").write_bytes(_canonical(manifest))
            with self.assertRaisesRegex(
                TechnicalShadowRunError,
                "config_drifted",
            ):
                load_verified_exposure_diagnostic(
                    run_root,
                    expected_strategy_id=self.strategy_id,
                    expected_config_sha256=self.config_sha256,
                )

    def test_no_nonzero_target_requires_isolated_diagnostic(self):
        sessions = tuple(
            date(2025, 1, 1) + timedelta(days=index) for index in range(246)
        )
        captured = CapturedData(
            provider_id="baostock",
            provider_kind="real_provider",
            adapter_version="test",
            synthetic=False,
            captured_at="2026-08-26T18:00:00+08:00",
            sessions=sessions,
            stock_rows={},
            benchmark_rows=(),
            receipts={},
        )
        daily = [
            {
                "decision_date": day.isoformat(),
                "target_gross_exposure": 0.0,
                "data_fail_closed": False,
            }
            for day in sessions[-120:]
        ]
        with self.assertRaisesRegex(
            TechnicalShadowRunError,
            "use_isolated_execution_diagnostic",
        ):
            select_natural_window(daily=daily, captured=captured)


if __name__ == "__main__":
    unittest.main()
