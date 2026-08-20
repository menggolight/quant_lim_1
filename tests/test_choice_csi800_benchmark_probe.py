from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.choice_csi800_benchmark_probe import (
    collect_fixed_probe,
    main,
    verify_metadata_binding,
)
from research.market_data.contracts import canonical_json_bytes, sha256_bytes
from research.market_data.providers.base import ProviderError, ProviderQueryError
from research.market_data.providers.choice import ChoiceProvider


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
NOW = datetime(2026, 8, 19, 15, 0, tzinfo=CHINA_TZ)
EXPECTED_INDICATORS = ChoiceProvider._CSI800_BENCHMARK_PROBE_INDICATORS


def response(**values):
    return SimpleNamespace(ErrorCode=0, ErrorMsg="success", **values)


class FixedBenchmarkClient:
    def __init__(
        self,
        *,
        empty_total_return_dates=False,
        indicator_drift=False,
        fixed_dates=None,
        fixed_values=None,
    ):
        self.empty_total_return_dates = empty_total_return_dates
        self.indicator_drift = indicator_drift
        self.fixed_dates = fixed_dates
        self.fixed_values = fixed_values
        self.start_calls = []
        self.stop_calls = 0
        self.csd_calls = []

    def start(self, options, callback):
        self.start_calls.append((options, callback))
        return response()

    def stop(self):
        self.stop_calls += 1
        return response()

    def csd(self, instrument_id, indicators, start_date, end_date, options):
        self.csd_calls.append(
            (instrument_id, indicators, start_date, end_date, options)
        )
        dates = (
            list(self.fixed_dates)
            if self.fixed_dates is not None
            else (
                []
                if self.empty_total_return_dates and instrument_id == "H00906.CSI"
                else ["2026-08-18"]
            )
        )
        returned_indicators = list(EXPECTED_INDICATORS)
        if self.indicator_drift:
            returned_indicators[-1] = "TURN"
        values = self.fixed_values or [
            "5000",
            "5100",
            "4900",
            "5050",
            "5000",
            "100",
            "1000",
        ]
        return response(
            Codes=[instrument_id],
            Indicators=returned_indicators,
            Dates=dates,
            Data={
                instrument_id: [
                    ([value] * len(dates) if dates else []) for value in values
                ]
            },
        )


def provider_for(client):
    return ChoiceProvider(
        sdk_loader=lambda: SimpleNamespace(c=client),
        clock=lambda: NOW,
    )


class ChoiceCsi800BenchmarkProbeTests(unittest.TestCase):
    def test_range_methods_keep_alias_fields_none_basis_and_no_fill_fixed(self):
        client = FixedBenchmarkClient()
        provider = provider_for(client)
        with provider.diagnostic_session():
            price = provider.fetch_csi800_price_index_csd(
                date(2026, 8, 1), date(2026, 8, 18)
            )
            total_return = provider.fetch_csi800_total_return_csd(
                date(2026, 8, 1), date(2026, 8, 18)
            )

        self.assertEqual([item[0] for item in client.csd_calls], ["000906.SH", "H00906.CSI"])
        for call in client.csd_calls:
            self.assertEqual(call[1], ",".join(EXPECTED_INDICATORS))
            self.assertEqual(call[2:4], ("2026-08-01", "2026-08-18"))
            self.assertIn("AdjustFlag=1", call[4])
            self.assertIn("filldata=0", call[4])
        self.assertEqual(price.records[0]["series"], "price")
        self.assertEqual(price.records[0]["instrument_id"], "000906.SH")
        self.assertEqual(total_return.records[0]["series"], "total_return")
        self.assertEqual(total_return.records[0]["instrument_id"], "H00906.CSI")
        self.assertEqual(price.records[0]["fill_policy"], "no_fill_returned_dates_only")
        self.assertEqual(price.records[0]["adjustment"], "none")
        self.assertEqual(price.records[0]["close"], "5050")
        projection = json.loads(price.raw_content)
        self.assertFalse(projection["request"]["fallback_allowed"])
        self.assertEqual(
            projection["calendar_completeness_status"],
            "requires_external_exchange_calendar_reconciliation",
        )

    def test_range_methods_reject_bad_windows_before_csd(self):
        client = FixedBenchmarkClient()
        provider = provider_for(client)
        with provider.diagnostic_session():
            with self.assertRaisesRegex(ProviderQueryError, "date values"):
                provider.fetch_csi800_price_index_csd(
                    datetime(2026, 8, 1, tzinfo=CHINA_TZ), date(2026, 8, 18)
                )
            with self.assertRaisesRegex(ProviderQueryError, "exceeds end_date"):
                provider.fetch_csi800_price_index_csd(
                    date(2026, 8, 19), date(2026, 8, 18)
                )
            with self.assertRaisesRegex(ProviderQueryError, "safety cap"):
                provider.fetch_csi800_total_return_csd(
                    date(2010, 1, 1), date(2026, 8, 18)
                )
        self.assertEqual(client.csd_calls, [])

    def test_range_methods_reject_nonpositive_extreme_and_invalid_ohlc(self):
        cases = (
            (["0", "5100", "4900", "5050", "5000", "100", "1000"], "positive"),
            (["5000", "5100", "4900", "5050", "5000", "-1", "1000"], "non-negative"),
            (["1e100000", "5100", "4900", "5050", "5000", "100", "1000"], "bounded decimal"),
            (["5000", "4999", "4900", "5050", "5000", "100", "1000"], "OHLC ordering"),
        )
        for values, message in cases:
            with self.subTest(message=message):
                provider = provider_for(FixedBenchmarkClient(fixed_values=values))
                with provider.diagnostic_session():
                    with self.assertRaisesRegex(ProviderQueryError, message):
                        provider.fetch_csi800_price_index_csd(
                            date(2026, 8, 18), date(2026, 8, 18)
                        )

    def test_total_return_allows_only_volume_and_amount_to_be_missing(self):
        real_shape = [
            "5000",
            "5100",
            "4900",
            "5050",
            "5000",
            None,
            "",
        ]
        total_provider = provider_for(
            FixedBenchmarkClient(fixed_values=real_shape)
        )
        with total_provider.diagnostic_session():
            payload = total_provider.fetch_csi800_total_return_csd(
                date(2026, 8, 18), date(2026, 8, 18)
            )
        self.assertIsNone(payload.records[0]["volume"])
        self.assertIsNone(payload.records[0]["amount"])
        self.assertEqual(payload.records[0]["close"], "5050")

        price_provider = provider_for(
            FixedBenchmarkClient(fixed_values=real_shape)
        )
        with price_provider.diagnostic_session():
            with self.assertRaisesRegex(ProviderQueryError, "volume.*bounded decimal"):
                price_provider.fetch_csi800_price_index_csd(
                    date(2026, 8, 18), date(2026, 8, 18)
                )

        missing_close = [
            "5000",
            "5100",
            "4900",
            None,
            "5000",
            None,
            None,
        ]
        total_provider = provider_for(
            FixedBenchmarkClient(fixed_values=missing_close)
        )
        with total_provider.diagnostic_session():
            with self.assertRaisesRegex(ProviderQueryError, "close.*bounded decimal"):
                total_provider.fetch_csi800_total_return_csd(
                    date(2026, 8, 18), date(2026, 8, 18)
                )

    def test_range_methods_reject_empty_duplicate_and_out_of_window_dates(self):
        cases = (
            ([], "returned no dates"),
            (["2026-08-18", "2026-08-18"], "unique and ascending"),
            (["2026-08-17"], "outside the request"),
        )
        for dates, message in cases:
            with self.subTest(dates=dates):
                provider = provider_for(FixedBenchmarkClient(fixed_dates=dates))
                with provider.diagnostic_session():
                    with self.assertRaisesRegex(ProviderError, message):
                        provider.fetch_csi800_total_return_csd(
                            date(2026, 8, 18), date(2026, 8, 18)
                        )

    def test_provider_queries_only_two_fixed_aliases_one_day_none_basis(self):
        client = FixedBenchmarkClient()
        provider = provider_for(client)
        with provider.diagnostic_session():
            payload = provider.fetch_csi800_benchmark_probe()

        self.assertEqual(client.stop_calls, 1)
        self.assertEqual([item[0] for item in client.csd_calls], ["000906.SH", "H00906.CSI"])
        for call in client.csd_calls:
            self.assertEqual(call[1], ",".join(EXPECTED_INDICATORS))
            self.assertEqual(call[2:4], ("2026-08-18", "2026-08-18"))
            self.assertIn("AdjustFlag=1", call[4])
            self.assertIn("filldata=0", call[4])
        projection = json.loads(payload.raw_content)
        self.assertEqual(
            projection["request"]["excluded_distinct_series"],
            [{"instrument_id": "N00906.CSI", "series": "net_return"}],
        )
        self.assertFalse(projection["request"]["fallback_allowed"])
        self.assertTrue(projection["historical_date_proven_for_all_series"])
        self.assertEqual({row["series"] for row in payload.records}, {"price", "total_return"})
        self.assertTrue(all(not row["formal_truth_eligible"] for row in payload.records))

    def test_missing_total_return_date_is_preserved_without_pit_promotion(self):
        provider = provider_for(FixedBenchmarkClient(empty_total_return_dates=True))
        with provider.diagnostic_session():
            payload = provider.fetch_csi800_benchmark_probe()
        projection = json.loads(payload.raw_content)
        self.assertFalse(projection["historical_date_proven_for_all_series"])
        total_return = projection["series_results"][1]
        self.assertEqual(total_return["response"]["dates"], [])
        self.assertFalse(total_return["date_evidence"]["historical_date_proven"])
        self.assertEqual(total_return["records"], [])
        self.assertEqual(payload.issues[0]["code"], "choice_benchmark_response_date_not_proven")

    def test_indicator_contract_drift_fails_closed(self):
        client = FixedBenchmarkClient(indicator_drift=True)
        provider = provider_for(client)
        with provider.diagnostic_session():
            with self.assertRaisesRegex(ProviderQueryError, "indicator contract"):
                provider.fetch_csi800_benchmark_probe()
        self.assertEqual(len(client.csd_calls), 1)

    def test_metadata_verifier_binds_file_hash_records_and_lines(self):
        ispe_records = [
            {"UNIQUECODE": "000906.SH", "CODE": "000906"},
            {"UNIQUECODE": "H00906.CSI", "CODE": "H00906"},
            {"UNIQUECODE": "N00906.CSI", "CODE": "N00906"},
        ]
        relation_lines = (
            b"009006039$1$000906.SH$label\n"
            b"009006039$2$H00906.CSI$label\n"
            b"009006039$3$N00906.CSI$label\n"
        )
        ispe_raw = canonical_json_bytes({"ISPE_BLOCKTREE": ispe_records})
        ispe_specs = tuple(
            {
                "role": role,
                "record_index_zero_based": index,
                "expected": record,
            }
            for index, (role, record) in enumerate(
                zip(("price", "total_return", "net_return"), ispe_records)
            )
        )
        relation_specs = tuple(
            {
                "role": role,
                "line_number_one_based": index + 1,
                "line_sha256": sha256_bytes(line),
                "expected_ascii_fields": tuple(
                    item.decode("ascii") for item in line.split(b"$")[:3]
                ),
            }
            for index, (role, line) in enumerate(
                zip(
                    ("price", "total_return", "net_return"),
                    relation_lines.splitlines(),
                )
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            ispe_path = Path(directory) / "ispe.json"
            relation_path = Path(directory) / "relation.dat"
            ispe_path.write_bytes(ispe_raw)
            relation_path.write_bytes(relation_lines)
            kwargs = {
                "ispe_path": ispe_path,
                "relation_path": relation_path,
                "expected_ispe_sha256": sha256_bytes(ispe_raw),
                "expected_relation_sha256": sha256_bytes(relation_lines),
                "ispe_record_specs": ispe_specs,
                "relation_line_specs": relation_specs,
            }
            receipt = verify_metadata_binding(**kwargs)
            self.assertEqual(receipt["status"], "verified_local_choice_metadata_snapshot")
            relation_path.write_bytes(relation_lines + b"tamper\n")
            with self.assertRaisesRegex(ValueError, "SHA-256 drifted"):
                verify_metadata_binding(**kwargs)

    def test_cli_writes_diagnostic_only_artifact_and_does_not_overwrite(self):
        client = FixedBenchmarkClient()
        provider = provider_for(client)
        metadata = {
            "status": "verified_local_choice_metadata_snapshot",
            "semantics": "local_choice_alias_mapping_not_official_csi_authentication",
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "benchmark.json"
            stdout = io.StringIO()
            with patch(
                "agent.choice_csi800_benchmark_probe.verify_fixed_metadata_binding",
                return_value=metadata,
            ), patch(
                "agent.choice_csi800_benchmark_probe.ChoiceProvider",
                return_value=provider,
            ), patch("sys.stdout", stdout):
                self.assertEqual(main(["--output", str(output)]), 0)
            artifact = json.loads(output.read_text(encoding="utf-8"))
            first_call_count = len(client.csd_calls)
            with patch("sys.stdout", io.StringIO()):
                self.assertEqual(main(["--output", str(output)]), 2)

        self.assertEqual(len(client.csd_calls), first_call_count)
        self.assertEqual(artifact["metadata_binding"], metadata)
        self.assertFalse(artifact["official_benchmark_identity_authenticated"])
        self.assertFalse(artifact["trade_eligible"])
        rendered = json.dumps(artifact).casefold()
        self.assertNotIn("logininfo", rendered)
        self.assertNotIn("username", rendered)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "captured_diagnostic_only")


if __name__ == "__main__":
    unittest.main()
