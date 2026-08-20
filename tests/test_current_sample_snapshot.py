from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from agent.current_sample_snapshot import build_parser
from research.market_data.contracts import (
    MarketDataRequest,
    canonical_json_bytes,
    sha256_bytes,
)
from research.market_data.providers.base import ProviderPayload
from research.market_data.providers.choice import ChoiceProvider, replay_choice_raw
from research.strategy_workspace.current_sample_snapshot import (
    CurrentSampleSnapshotError,
    FIXED_BENCHMARK_ID,
    FIXED_CALENDAR_START,
    FIXED_SNAPSHOT_DATE,
    SAFETY,
    collect_current_sample_snapshot,
    verify_current_sample_snapshot,
)
from research.strategy_workspace.diagnostic import (
    DIAGNOSTIC_FACTOR_IDS,
    DIAGNOSTIC_STATUS,
)


def _sessions() -> tuple[date, ...]:
    result: list[date] = []
    current = FIXED_SNAPSHOT_DATE
    while len(result) < 121:
        if current.weekday() < 5:
            result.append(current)
        current -= timedelta(days=1)
    return tuple(reversed(result))


class FakeProvider:
    adapter_version = ChoiceProvider.adapter_version

    def __init__(self, *, missing_instrument: str | None = None) -> None:
        self.missing_instrument = missing_instrument
        self.calls = 0
        self.fetched_at = datetime(2026, 8, 19, 15, 30, tzinfo=timezone(timedelta(hours=8)))
        self.sessions = _sessions()

    @contextmanager
    def diagnostic_session(self):
        yield self

    def fetch_quality_growth_calendar(self, start_date: date, end_date: date) -> ProviderPayload:
        self.calls += 1
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
                    "indicators": list(ChoiceProvider._QUALITY_GROWTH_CSD_INDICATORS),
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
        records = [
            {
                "series": "price",
                "instrument_id": FIXED_BENCHMARK_ID,
                "trading_date": item.isoformat(),
                "adjustment": "none",
                "fill_policy": "no_fill_returned_dates_only",
                "calendar_completeness_status": "requires_external_exchange_calendar_reconciliation",
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
                    "indicators": list(ChoiceProvider._CSI800_BENCHMARK_PROBE_INDICATORS),
                    "options": ChoiceProvider._csi800_benchmark_probe_options(),
                    "fill_policy": "no_fill_returned_dates_only",
                    "fallback_allowed": False,
                },
                "response_dates": response_dates,
                "calendar_completeness_status": "requires_external_exchange_calendar_reconciliation",
                "records": records,
            }
        )
        return ProviderPayload(
            raw_content=raw,
            records=tuple(records),
            fetched_at=self.fetched_at,
            upstream_source="choice.eastmoney_emquantapi.csd.csi800_benchmark_fixed_range",
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


class CurrentSampleSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output = self.root / "snapshot"
        for name in ("membership", "industry", "sample"):
            (self.root / name).mkdir()
        self.instruments = [f"{index:06d}.SZ" for index in range(1, 61)]
        self.sample = {
            "status": DIAGNOSTIC_STATUS,
            "information_cutoff_date": "2026-08-19",
            "market_snapshot_date": FIXED_SNAPSHOT_DATE.isoformat(),
            "instrument_ids": self.instruments,
            "factor_ids": list(DIAGNOSTIC_FACTOR_IDS),
            "safety": dict(SAFETY),
            "representation": "diagnostic_equal_industry_coverage_not_csi800_representative",
            "sample_content_sha256": "a" * 64,
            "sample_payload_sha256": "b" * 64,
        }
        self.sample_manifest = {"manifest_payload_sha256": "c" * 64}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _validation_patch(self):
        return patch(
            "research.strategy_workspace.current_sample_snapshot._validated_sample",
            return_value=(self.sample, self.sample_manifest, "d" * 64),
        )

    def test_collect_and_offline_verify_exact_snapshot(self) -> None:
        provider = FakeProvider()
        with self._validation_patch():
            manifest = collect_current_sample_snapshot(
                provider,
                membership_dir=self.root / "membership",
                industry_dir=self.root / "industry",
                sample_dir=self.root / "sample",
                output_dir=self.output,
            )
            verified = verify_current_sample_snapshot(
                membership_dir=self.root / "membership",
                industry_dir=self.root / "industry",
                sample_dir=self.root / "sample",
                output_dir=self.output,
            )
        snapshot = json.loads((self.output / "factor_snapshot.json").read_text("utf-8"))
        self.assertEqual(len(snapshot["rows"]), 60)
        self.assertEqual(snapshot["factor_ids"], list(DIAGNOSTIC_FACTOR_IDS))
        self.assertFalse(snapshot["ranking_or_signal_generated"])
        self.assertEqual(snapshot["safety"], SAFETY)
        self.assertEqual(manifest, verified)
        self.assertEqual(provider.calls, 62)

    def test_missing_one_session_fails_closed(self) -> None:
        provider = FakeProvider(missing_instrument=self.instruments[17])
        with self._validation_patch(), self.assertRaises(CurrentSampleSnapshotError):
            collect_current_sample_snapshot(
                provider,
                membership_dir=self.root / "membership",
                industry_dir=self.root / "industry",
                sample_dir=self.root / "sample",
                output_dir=self.output,
            )
        self.assertFalse(self.output.exists())

    def test_existing_output_is_rejected_before_network(self) -> None:
        self.output.mkdir()
        provider = FakeProvider()
        with self.assertRaises(CurrentSampleSnapshotError):
            collect_current_sample_snapshot(
                provider,
                membership_dir=self.root / "membership",
                industry_dir=self.root / "industry",
                sample_dir=self.root / "sample",
                output_dir=self.output,
            )
        self.assertEqual(provider.calls, 0)

    def test_tampered_normalized_artifact_is_rejected(self) -> None:
        provider = FakeProvider()
        with self._validation_patch():
            collect_current_sample_snapshot(
                provider,
                membership_dir=self.root / "membership",
                industry_dir=self.root / "industry",
                sample_dir=self.root / "sample",
                output_dir=self.output,
            )
        target = self.output / "normalized" / "stocks" / f"{self.instruments[0]}.json"
        target.write_bytes(target.read_bytes().replace(b"10.501", b"10.999", 1))
        with self._validation_patch(), self.assertRaises(CurrentSampleSnapshotError):
            verify_current_sample_snapshot(
                membership_dir=self.root / "membership",
                industry_dir=self.root / "industry",
                sample_dir=self.root / "sample",
                output_dir=self.output,
            )

    def test_resigned_manifest_cannot_add_buy_list_or_eligibility(self) -> None:
        provider = FakeProvider()
        with self._validation_patch():
            collect_current_sample_snapshot(
                provider,
                membership_dir=self.root / "membership",
                industry_dir=self.root / "industry",
                sample_dir=self.root / "sample",
                output_dir=self.output,
            )
        buy_list = canonical_json_bytes({"orders": ["ATTACKER.SZ"]})
        (self.output / "buy_list.json").write_bytes(buy_list)
        manifest_path = self.output / "manifest.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["artifacts"]["buy_list.json"] = sha256_bytes(buy_list)
        manifest["paper_eligibility"] = True
        manifest.pop("manifest_payload_sha256")
        manifest["manifest_payload_sha256"] = sha256_bytes(canonical_json_bytes(manifest))
        manifest_path.write_bytes(canonical_json_bytes(manifest))
        with self._validation_patch(), self.assertRaises(CurrentSampleSnapshotError):
            verify_current_sample_snapshot(
                membership_dir=self.root / "membership",
                industry_dir=self.root / "industry",
                sample_dir=self.root / "sample",
                output_dir=self.output,
            )

    def test_output_cannot_overlap_a_controlled_input(self) -> None:
        provider = FakeProvider()
        with self._validation_patch(), self.assertRaises(CurrentSampleSnapshotError):
            collect_current_sample_snapshot(
                provider,
                membership_dir=self.root / "membership",
                industry_dir=self.root / "industry",
                sample_dir=self.root / "sample",
                output_dir=self.root / "membership" / "snapshot",
            )
        self.assertEqual(provider.calls, 0)

    def test_capture_after_information_cutoff_fails_closed(self) -> None:
        provider = FakeProvider()
        provider.fetched_at = datetime(
            2026, 8, 20, 9, 0, tzinfo=timezone(timedelta(hours=8))
        )
        with self._validation_patch(), self.assertRaises(CurrentSampleSnapshotError):
            collect_current_sample_snapshot(
                provider,
                membership_dir=self.root / "membership",
                industry_dir=self.root / "industry",
                sample_dir=self.root / "sample",
                output_dir=self.output,
            )
        self.assertFalse(self.output.exists())

    def test_cli_has_no_caller_selected_market_parameters(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "collect",
                    "--membership-dir", "m",
                    "--industry-dir", "i",
                    "--sample-dir", "s",
                    "--output-dir", "o",
                    "--benchmark", "ATTACKER.SH",
                ]
            )


if __name__ == "__main__":
    unittest.main()
