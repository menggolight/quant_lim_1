from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from agent.current_sample_snapshot_v2 import build_parser
from research.market_data.contracts import (
    MarketDataRequest,
    canonical_json_bytes,
    sha256_bytes,
)
from research.market_data.providers.base import ProviderPayload
from research.market_data.providers.choice import ChoiceProvider, replay_choice_raw
from research.strategy_workspace.current_sample_snapshot import (
    CurrentSampleSnapshotError,
)
from research.strategy_workspace.current_sample_snapshot_v2 import (
    FIXED_BENCHMARK_ID,
    FIXED_CAPTURE_INFORMATION_CUTOFF_DATE,
    FIXED_SAMPLE_INFORMATION_CUTOFF_DATE,
    FIXED_SAMPLE_MARKET_SNAPSHOT_DATE,
    FIXED_TARGET_SNAPSHOT_DATE,
    SAFETY,
    _validated_sample,
    collect_current_sample_snapshot_v2,
    verify_current_sample_snapshot_v2,
)
from research.strategy_workspace.diagnostic import (
    DIAGNOSTIC_FACTOR_IDS,
    DIAGNOSTIC_STATUS,
)


def _sessions() -> tuple[date, ...]:
    result: list[date] = []
    current = FIXED_TARGET_SNAPSHOT_DATE
    while len(result) < 121:
        if current.weekday() < 5:
            result.append(current)
        current -= timedelta(days=1)
    return tuple(reversed(result))


class FakeProvider:
    adapter_version = ChoiceProvider.adapter_version

    def __init__(
        self,
        *,
        missing_instrument: str | None = None,
        fetched_at: datetime | None = None,
    ) -> None:
        self.missing_instrument = missing_instrument
        self.fetched_at = fetched_at or datetime(
            2026, 8, 20, 9, 0, tzinfo=timezone(timedelta(hours=8))
        )
        self.sessions = _sessions()
        self.calls = 0
        self.requests: list[tuple[str, date, date, str]] = []

    @contextmanager
    def diagnostic_session(self):
        yield self

    def fetch_quality_growth_calendar(
        self, start_date: date, end_date: date
    ) -> ProviderPayload:
        self.calls += 1
        self.requests.append(("calendar", start_date, end_date, "none"))
        request = MarketDataRequest(
            dataset_type="trade_calendar",
            start_date=start_date,
            end_date=end_date,
            retrieval_mode="historical_backfill",
            requested_at=self.fetched_at,
        )
        raw = canonical_json_bytes(
            {
                "operation": "tradedates",
                "request": request.fingerprint_payload(
                    ChoiceProvider.provider_id, ChoiceProvider.adapter_version
                ),
                "trade_calendar": {
                    "options": ChoiceProvider._calendar_options("CNSESH"),
                    "dates": [item.isoformat() for item in self.sessions],
                },
            }
        )
        return ProviderPayload(
            raw_content=raw,
            records=replay_choice_raw(request, raw, self.fetched_at),
            fetched_at=self.fetched_at,
            upstream_source=ChoiceProvider._CALENDAR_UPSTREAM,
            issues=(
                {
                    "code": "choice_calendar_secondary_not_official",
                    "severity": "info",
                    "message": "test",
                },
                *ChoiceProvider._quality_growth_issue(),
            ),
        )

    def fetch_quality_growth_csd(
        self,
        instrument_id: str,
        start_date: date,
        end_date: date,
        *,
        adjustment: str,
    ) -> ProviderPayload:
        self.calls += 1
        self.requests.append((instrument_id, start_date, end_date, adjustment))
        dates = list(self.sessions)
        if instrument_id == self.missing_instrument:
            dates.pop(30)
        offset = int(instrument_id[:6]) / 1000
        records = [
            {
                "instrument_id": instrument_id,
                "trading_date": item.isoformat(),
                "adjustment": adjustment,
                "open": str(10 + offset + index / 100),
                "high": str(11 + offset + index / 100),
                "low": str(9 + offset + index / 100),
                "close": str(10.5 + offset + index / 100),
                "preclose": str(10.49 + offset + index / 100),
                "volume": "100000",
                "amount": "1000000",
                "tradestatus": "正常交易",
                "isststock": "否",
                "highlimit": "0",
                "lowlimit": "0",
            }
            for index, item in enumerate(dates)
        ]
        raw = canonical_json_bytes(
            {
                "operation": "quality_growth_fixed_csd",
                "request": {
                    "instrument_id": instrument_id,
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    "adjustment": adjustment,
                    "indicators": list(
                        ChoiceProvider._QUALITY_GROWTH_CSD_INDICATORS
                    ),
                    "options": ChoiceProvider._quality_growth_csd_options("qfq"),
                },
                "records": records,
            }
        )
        return ProviderPayload(
            raw_content=raw,
            records=tuple(records),
            fetched_at=self.fetched_at,
            upstream_source="choice.eastmoney_emquantapi.csd.quality_growth_fixed",
            issues=ChoiceProvider._quality_growth_issue(),
        )

    def fetch_csi800_price_index_csd(
        self, start_date: date, end_date: date
    ) -> ProviderPayload:
        self.calls += 1
        self.requests.append((FIXED_BENCHMARK_ID, start_date, end_date, "none"))
        records = [
            {
                "series": "price",
                "instrument_id": FIXED_BENCHMARK_ID,
                "trading_date": item.isoformat(),
                "adjustment": "none",
                "fill_policy": "no_fill_returned_dates_only",
                "calendar_completeness_status": (
                    "requires_external_exchange_calendar_reconciliation"
                ),
                "point_in_time_eligible": False,
                "formal_truth_eligible": False,
                "open": str(1000 + index),
                "high": str(1002 + index),
                "low": str(999 + index),
                "close": str(1001 + index),
                "preclose": str(1000 + index),
                "volume": "10000000",
                "amount": "100000000",
            }
            for index, item in enumerate(self.sessions)
        ]
        response_dates = [item.isoformat() for item in self.sessions]
        raw = canonical_json_bytes(
            {
                "operation": "choice_fixed_csi800_benchmark_csd",
                "raw_semantics": "canonicalized_sdk_projection",
                "request": {
                    "series": "price",
                    "instrument_id": FIXED_BENCHMARK_ID,
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    "adjustment": "none",
                    "indicators": list(
                        ChoiceProvider._CSI800_BENCHMARK_PROBE_INDICATORS
                    ),
                    "options": ChoiceProvider._csi800_benchmark_probe_options(),
                    "fill_policy": "no_fill_returned_dates_only",
                    "fallback_allowed": False,
                },
                "response_dates": response_dates,
                "calendar_completeness_status": (
                    "requires_external_exchange_calendar_reconciliation"
                ),
                "records": records,
            }
        )
        return ProviderPayload(
            raw_content=raw,
            records=tuple(records),
            fetched_at=self.fetched_at,
            upstream_source=(
                "choice.eastmoney_emquantapi.csd.csi800_benchmark_fixed_range"
            ),
            issues=(
                {
                    "code": "choice_benchmark_calendar_reconciliation_required",
                    "severity": "warning",
                    "message": "test",
                },
                {
                    "code": "choice_benchmark_not_officially_authenticated",
                    "severity": "warning",
                    "message": "test",
                },
            ),
        )


class CurrentSampleSnapshotV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output = self.root / "snapshot-v2"
        for name in ("membership", "industry", "sample"):
            (self.root / name).mkdir()
        self.instruments = [f"{index:06d}.SZ" for index in range(1, 61)]
        self.sample = {
            "status": DIAGNOSTIC_STATUS,
            "information_cutoff_date": (
                FIXED_SAMPLE_INFORMATION_CUTOFF_DATE.isoformat()
            ),
            "market_snapshot_date": FIXED_SAMPLE_MARKET_SNAPSHOT_DATE.isoformat(),
            "instrument_ids": self.instruments,
            "factor_ids": list(DIAGNOSTIC_FACTOR_IDS),
            "safety": dict(SAFETY),
            "representation": (
                "diagnostic_equal_industry_coverage_not_csi800_representative"
            ),
            "sample_content_sha256": "a" * 64,
            "sample_payload_sha256": "b" * 64,
        }
        self.sample_manifest = {"manifest_payload_sha256": "c" * 64}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _validation_patch(self):
        return patch(
            "research.strategy_workspace.current_sample_snapshot_v2._validated_sample",
            return_value=(self.sample, self.sample_manifest, "d" * 64),
        )

    def _collect(self, provider: FakeProvider | None = None):
        provider = provider or FakeProvider()
        with self._validation_patch():
            manifest = collect_current_sample_snapshot_v2(
                provider,
                membership_dir=self.root / "membership",
                industry_dir=self.root / "industry",
                sample_dir=self.root / "sample",
                output_dir=self.output,
            )
        return provider, manifest

    def test_collect_and_verify_bind_all_three_date_layers(self) -> None:
        provider, manifest = self._collect()
        with self._validation_patch():
            verified = verify_current_sample_snapshot_v2(
                membership_dir=self.root / "membership",
                industry_dir=self.root / "industry",
                sample_dir=self.root / "sample",
                output_dir=self.output,
            )
        plan = json.loads((self.output / "plan.json").read_text("utf-8"))
        snapshot = json.loads(
            (self.output / "factor_snapshot.json").read_text("utf-8")
        )
        self.assertEqual(manifest, verified)
        self.assertEqual(plan["sample_market_snapshot_date"], "2026-08-18")
        self.assertEqual(plan["sample_information_cutoff_date"], "2026-08-19")
        self.assertEqual(plan["capture_information_cutoff_date"], "2026-08-20")
        self.assertEqual(plan["target_snapshot_date"], "2026-08-19")
        self.assertEqual(plan["query_end"], "2026-08-19")
        self.assertEqual(snapshot["snapshot_date"], "2026-08-19")
        self.assertEqual(len(snapshot["rows"]), 60)
        self.assertTrue(
            all(item["trading_date"] == "2026-08-19" for item in snapshot["rows"])
        )
        self.assertFalse(snapshot["historical_backtest_run"])
        self.assertFalse(snapshot["ranking_or_signal_generated"])
        self.assertEqual(snapshot["safety"], SAFETY)
        self.assertEqual(provider.calls, 62)
        self.assertTrue(
            all(request[2] == FIXED_TARGET_SNAPSHOT_DATE for request in provider.requests)
        )

    def test_wrong_or_naive_capture_date_fails_closed(self) -> None:
        bad_times = (
            datetime(2026, 8, 19, 15, 30, tzinfo=timezone(timedelta(hours=8))),
            datetime(2026, 8, 21, 9, 0, tzinfo=timezone(timedelta(hours=8))),
            datetime(2026, 8, 20, 9, 0),
        )
        for fetched_at in bad_times:
            with self.subTest(fetched_at=fetched_at):
                provider = FakeProvider(fetched_at=fetched_at)
                with self._validation_patch(), self.assertRaises(
                    (CurrentSampleSnapshotError, ValueError)
                ):
                    collect_current_sample_snapshot_v2(
                        provider,
                        membership_dir=self.root / "membership",
                        industry_dir=self.root / "industry",
                        sample_dir=self.root / "sample",
                        output_dir=self.output,
                    )
                self.assertFalse(self.output.exists())

    def test_missing_one_session_fails_closed(self) -> None:
        provider = FakeProvider(missing_instrument=self.instruments[17])
        with self._validation_patch(), self.assertRaises(CurrentSampleSnapshotError):
            collect_current_sample_snapshot_v2(
                provider,
                membership_dir=self.root / "membership",
                industry_dir=self.root / "industry",
                sample_dir=self.root / "sample",
                output_dir=self.output,
            )
        self.assertFalse(self.output.exists())

    def test_sealed_sample_dates_cannot_be_promoted_to_target_dates(self) -> None:
        sample_path = self.root / "sample" / "sample.json"
        for field, wrong_value in (
            ("market_snapshot_date", "2026-08-19"),
            ("information_cutoff_date", "2026-08-20"),
        ):
            with self.subTest(field=field):
                payload = dict(self.sample)
                payload[field] = wrong_value
                sample_path.write_bytes(canonical_json_bytes(payload))
                with patch(
                    "research.strategy_workspace.current_sample_snapshot_v2.verify_controlled_sample",
                    return_value=self.sample_manifest,
                ), self.assertRaises(CurrentSampleSnapshotError):
                    _validated_sample(
                        self.root / "membership",
                        self.root / "industry",
                        self.root / "sample",
                    )

    def test_existing_output_is_rejected_before_provider_call(self) -> None:
        self.output.mkdir()
        provider = FakeProvider()
        with self._validation_patch(), self.assertRaises(CurrentSampleSnapshotError):
            collect_current_sample_snapshot_v2(
                provider,
                membership_dir=self.root / "membership",
                industry_dir=self.root / "industry",
                sample_dir=self.root / "sample",
                output_dir=self.output,
            )
        self.assertEqual(provider.calls, 0)

    def test_cli_exposes_no_market_or_eligibility_parameters(self) -> None:
        parser = build_parser()
        forbidden = (
            "--target-date",
            "--snapshot-date",
            "--capture-date",
            "--information-cutoff-date",
            "--end-date",
            "--benchmark",
            "--instrument",
            "--paper-eligibility",
        )
        for flag in forbidden:
            with self.subTest(flag=flag), self.assertRaises(SystemExit):
                parser.parse_args(
                    [
                        "collect",
                        "--membership-dir",
                        "m",
                        "--industry-dir",
                        "i",
                        "--sample-dir",
                        "s",
                        "--output-dir",
                        "o",
                        flag,
                        "attacker",
                    ]
                )

    def test_resigned_safety_tamper_is_rejected(self) -> None:
        self._collect()
        snapshot_path = self.output / "factor_snapshot.json"
        snapshot = json.loads(snapshot_path.read_text("utf-8"))
        snapshot["safety"]["trade_eligibility"] = True
        snapshot_path.write_bytes(canonical_json_bytes(snapshot))
        manifest_path = self.output / "manifest.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["artifacts"]["factor_snapshot.json"] = sha256_bytes(
            snapshot_path.read_bytes()
        )
        manifest_without_hash = {
            key: value
            for key, value in manifest.items()
            if key != "manifest_payload_sha256"
        }
        manifest["manifest_payload_sha256"] = sha256_bytes(
            canonical_json_bytes(manifest_without_hash)
        )
        manifest_path.write_bytes(canonical_json_bytes(manifest))
        with self._validation_patch(), self.assertRaises(CurrentSampleSnapshotError):
            verify_current_sample_snapshot_v2(
                membership_dir=self.root / "membership",
                industry_dir=self.root / "industry",
                sample_dir=self.root / "sample",
                output_dir=self.output,
            )


if __name__ == "__main__":
    unittest.main()
