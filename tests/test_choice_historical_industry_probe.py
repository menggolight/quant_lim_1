from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.choice_historical_industry_probe import (
    FIXED_DATES,
    FIXED_INSTRUMENTS,
    collect_fixed_probe,
    main,
)
from research.market_data.providers.base import ProviderQueryError
from research.market_data.providers.choice import ChoiceProvider


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
NOW = datetime(2026, 8, 19, 12, 0, tzinfo=CHINA_TZ)


def response(**values):
    return SimpleNamespace(ErrorCode=0, ErrorMsg="success", **values)


class FixedIndustryClient:
    def __init__(self, *, dates_by_request=None):
        self.dates_by_request = dates_by_request or {}
        self.css_calls = []
        self.start_calls = []
        self.stop_calls = 0

    def start(self, options, callback):
        self.start_calls.append((options, callback))
        return response()

    def stop(self):
        self.stop_calls += 1
        return response()

    def css(self, codes, indicators, options):
        self.css_calls.append((codes, indicators, options))
        requested_date = options.split("EndDate=", 1)[1].split(",", 1)[0]
        names = {
            "000001.SZ": "金融",
            "000333.SZ": "可选消费",
            "600519.SH": "主要消费",
        }
        return response(
            Codes=list(FIXED_INSTRUMENTS),
            Indicators=["HISCSIND"],
            Dates=self.dates_by_request.get(requested_date, [requested_date]),
            Data={code: [names[code]] for code in reversed(FIXED_INSTRUMENTS)},
        )


def provider_for(client):
    return ChoiceProvider(
        sdk_loader=lambda: SimpleNamespace(c=client),
        clock=lambda: NOW,
    )


class ChoiceHistoricalIndustryProbeTests(unittest.TestCase):
    def test_provider_uses_the_exact_frozen_read_only_contract(self):
        client = FixedIndustryClient()
        provider = provider_for(client)
        with provider.diagnostic_session():
            payload = provider.fetch_historical_csi_industry_probe(FIXED_DATES[0])

        self.assertEqual(len(client.start_calls), 1)
        self.assertEqual(client.stop_calls, 1)
        self.assertEqual(len(client.css_calls), 1)
        codes, indicator, options = client.css_calls[0]
        self.assertEqual(codes, ",".join(FIXED_INSTRUMENTS))
        self.assertEqual(indicator, "HISCSIND")
        self.assertEqual(
            options,
            "EndDate=2024-06-28,ClassiFication=1,"
            "Ispandas=0,RowIndex=1,RECVtimeout=30",
        )
        self.assertTrue(payload.records[0]["historical_date_proven"])
        projection = json.loads(payload.raw_content)
        self.assertEqual(
            projection["raw_semantics"], "canonicalized_sdk_projection"
        )
        self.assertTrue(projection["date_evidence"]["historical_date_proven"])
        self.assertFalse(payload.records[0]["point_in_time_eligible"])

    def test_missing_response_date_is_preserved_and_never_promoted_to_pit(self):
        client = FixedIndustryClient(dates_by_request={"2024-06-28": []})
        provider = provider_for(client)
        with provider.diagnostic_session():
            payload = provider.fetch_historical_csi_industry_probe(FIXED_DATES[0])

        projection = json.loads(payload.raw_content)
        self.assertEqual(projection["response"]["dates"], [])
        self.assertFalse(projection["date_evidence"]["historical_date_proven"])
        self.assertFalse(payload.records[0]["historical_date_proven"])
        self.assertEqual(
            payload.issues[0]["code"],
            "choice_historical_industry_response_date_not_proven",
        )
        self.assertFalse(payload.records[0]["formal_truth_eligible"])

    def test_date_outside_frozen_pair_is_rejected_before_css(self):
        client = FixedIndustryClient()
        provider = provider_for(client)
        with provider.diagnostic_session():
            with self.assertRaisesRegex(ProviderQueryError, "frozen allowlist"):
                provider.fetch_historical_csi_industry_probe(date(2025, 1, 2))
        self.assertEqual(client.css_calls, [])

    def test_cli_writes_complete_diagnostic_artifact_without_identity_data(self):
        client = FixedIndustryClient(
            dates_by_request={"2026-08-18": []}
        )
        provider = provider_for(client)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "probe.json"
            stdout = io.StringIO()
            with patch(
                "agent.choice_historical_industry_probe.ChoiceProvider",
                return_value=provider,
            ), patch("sys.stdout", stdout):
                self.assertEqual(main(["--output", str(output)]), 0)
            artifact = json.loads(output.read_text(encoding="utf-8"))
            rendered = output.read_text(encoding="utf-8").casefold()

        self.assertEqual(len(client.css_calls), 2)
        self.assertEqual(artifact["query_contract"]["instrument_ids"], list(FIXED_INSTRUMENTS))
        self.assertEqual(artifact["query_contract"]["indicator"], "HISCSIND")
        self.assertFalse(artifact["historical_date_proven_for_all_requests"])
        self.assertEqual(
            artifact["point_in_time_status"],
            "diagnostic_response_date_not_proven",
        )
        self.assertFalse(artifact["formal_truth_eligible"])
        self.assertNotIn("logininfo", rendered)
        self.assertNotIn("username", rendered)
        self.assertNotIn("password", rendered)
        summary = json.loads(stdout.getvalue())
        self.assertEqual(summary["status"], "captured_diagnostic_only")

    def test_cli_never_overwrites_an_existing_capture(self):
        provider = provider_for(FixedIndustryClient())
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "probe.json"
            output.write_text("original", encoding="utf-8")
            stdout = io.StringIO()
            with patch(
                "agent.choice_historical_industry_probe.ChoiceProvider",
                return_value=provider,
            ), patch("sys.stdout", stdout):
                self.assertEqual(main(["--output", str(output)]), 2)
            self.assertEqual(output.read_text(encoding="utf-8"), "original")
        self.assertEqual(provider._diagnostic_client, None)
        self.assertEqual(provider._sdk_loader().c.css_calls, [])
        self.assertEqual(json.loads(stdout.getvalue())["status"], "incomplete")


if __name__ == "__main__":
    unittest.main()
