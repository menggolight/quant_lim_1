from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from research.market_data.contracts import canonical_json_bytes
from research.market_data.index_evidence import (
    ALL_INDEX_IDS,
    BENCHMARK_INDEX_ID,
    CN_EQUITY_SESSION,
    CONFIRM_INDEX_IDS,
    CSI_INDUSTRY_UNIVERSE,
    HTTPSResponse,
    INDEX_LEVEL,
    SCREEN_INDEX_IDS,
    IndexEvidenceError,
    IndexEvidenceRequest,
    IndexEvidenceService,
    IndexEvidenceStorageError,
    frozen_universe_records,
    load_index_panel,
)
from research.market_data.providers.base import (
    ProviderQueryError,
    ProviderQuotaExceededError,
)
from research.market_data.providers.choice_index import (
    CHOICE_ALIAS_STATUS,
    CHOICE_INDEX_ALIASES,
    ChoiceIndexProvider,
)
from research.market_data.providers.csi_official import CSIOfficialProvider
from research.market_data.providers import sse_calendar as sse_calendar_module
from research.market_data.providers.sse_calendar import SSECalendarProvider
from research.market_data.validation import validate_json_schema


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
NOW = datetime(2026, 8, 12, 16, 0, tzinfo=CHINA_TZ)


def request(
    dataset_type=INDEX_LEVEL,
    *,
    index_ids=("000986.CSI",),
    start="2026-08-10",
    end="2026-08-11",
    mode="historical_backfill",
    requested_at=NOW,
):
    return IndexEvidenceRequest(
        dataset_type=dataset_type,
        requested_at=requested_at,
        retrieval_mode=mode,
        index_ids=index_ids if dataset_type != CN_EQUITY_SESSION else (),
        start_date=None if dataset_type == CSI_INDUSTRY_UNIVERSE else start,
        end_date=None if dataset_type == CSI_INDUSTRY_UNIVERSE else end,
        evidence_cutoff_at=requested_at if mode == "offline_replay" else None,
    )


def choice_result(error_code=0, error_message="success", **values):
    defaults = {"Codes": [], "Indicators": [], "Dates": [], "Data": []}
    defaults.update(values)
    return SimpleNamespace(ErrorCode=error_code, ErrorMsg=error_message, **defaults)


def choice_calendar(days=("2026-08-10", "2026-08-11")):
    return choice_result(
        Codes=[""], Indicators=["TRADEDATE"], Dates=list(days), Data=list(days)
    )


def choice_levels(alias, days=("2026-08-10", "2026-08-11")):
    columns = [
        ["100", "101"],
        ["102", "103"],
        ["99", "100"],
        ["101", "102"],
        ["99", "101"],
        ["1000", "1100"],
        ["100000", "110000"],
    ]
    columns = [column[: len(days)] for column in columns]
    return choice_result(
        Codes=[alias],
        Indicators=["OPEN", "HIGH", "LOW", "CLOSE", "PRECLOSE", "VOLUME", "AMOUNT"],
        Dates=list(days),
        Data={alias: columns},
    )


class FakeChoice:
    def __init__(self, responses=None):
        self.responses = list(responses or ())
        self.calls = []

    def start(self, *args):
        self.calls.append(("start", args))
        return choice_result()

    def tradedates(self, *args):
        self.calls.append(("tradedates", args))
        return choice_calendar()

    def csd(self, *args):
        self.calls.append(("csd", args))
        if self.responses:
            return self.responses.pop(0)
        return choice_levels(args[0])

    def stop(self, *args):
        self.calls.append(("stop", args))
        return choice_result()

    def porder(self, *_):
        raise AssertionError("state-changing SDK method must never be called")


class RoutingTransport:
    def __init__(self, router, *, final_host):
        self.router = router
        self.final_host = final_host
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        body = canonical_json_bytes(self.router(url))
        return HTTPSResponse(
            final_url=f"https://{self.final_host}/fixed",
            status=200,
            headers={"Content-Type": "application/json; charset=utf-8", "ETag": "v1"},
            body=body,
        )


class HTMLRoutingTransport:
    def __init__(self, pages):
        self.pages = dict(pages)
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        if url not in self.pages:
            raise AssertionError(f"unexpected SSE URL: {url}")
        return HTTPSResponse(
            final_url=url,
            status=200,
            headers={"Content-Type": "text/html; charset=utf-8", "ETag": "fixture-v2"},
            body=self.pages[url].encode("utf-8"),
        )


def sse_article(title, published, paragraphs):
    body = "".join(f"<p>{paragraph}</p>" for paragraph in paragraphs)
    return (
        "<!doctype html><html><body><div class='article-infor'>"
        f"<h2>{title}</h2><div class='article_opt'><i>{published}</i></div>"
        f"<div class='allZoom'>{body}</div></div></body></html>"
    )


SSE_2026 = (
    "（一）元旦：1月1日（星期四）至1月3日（星期六）休市，1月5日（星期一）起照常开市。另外，1月4日（星期日）为周末休市。",
    "（二）春节：2月15日（星期日）至2月23日（星期一）休市，2月24日（星期二）起照常开市。另外，2月14日（星期六）、2月28日（星期六）为周末休市。",
    "（三）清明节：4月4日（星期六）至4月6日（星期一）休市，4月7日（星期二）起照常开市。",
    "（四）劳动节：5月1日（星期五）至5月5日（星期二）休市，5月6日（星期三）起照常开市。另外，5月9日（星期六）为周末休市。",
    "（五）端午节：6月19日（星期五）至6月21日（星期日）休市，6月22日（星期一）起照常开市。",
    "（六）中秋节：9月25日（星期五）至9月27日（星期日）休市，9月28日（星期一）起照常开市。",
    "（七）国庆节：10月1日（星期四）至10月7日（星期三）休市，10月8日（星期四）起照常开市。另外，9月20日（星期日）、10月10日（星期六）为周末休市。",
)
SSE_2019 = (
    "（一）元旦：2018年12月30日（星期日）至2019年1月1日（星期二）休市，1月2日（星期三）起照常开市。另外，2018年12月29日（星期六）为周末休市。",
    "（二）春节：2月4日（星期一）至2月10日（星期日）休市，2月11日（星期一）起照常开市。另外，2月2日（星期六）、2月3日（星期日）为周末休市。",
    "（三）清明节：4月5日（星期五）至4月7日（星期日）休市，4月8日（星期一）起照常开市。",
    "（四）劳动节：5月1日（星期三）休市，5月2日（星期四）起照常开市。",
    "（五）端午节：6月7日（星期五）至6月9日（星期日）休市，6月10日（星期一）起照常开市。",
    "（六）中秋节：9月13日（星期五）至9月15日（星期日）休市，9月16日（星期一）起照常开市。",
    "（七）国庆节：10月1日（星期二）至10月7日（星期一）休市，10月8日（星期二）起照常开市。另外，9月29日（星期日）、10月12日（星期六）为周末休市。",
)
SSE_2020 = (
    "（一）元旦：1月1日（星期三）休市，1月2日（星期四）起照常开市。",
    "（二）春节：1月24日（星期五）至1月30日（星期四）休市，1月31日（星期五）起照常开市。另外，1月19日（星期日）、2月1日（星期六）为周末休市。",
    "（三）清明节：4月4日（星期六）至4月6日（星期一）休市，4月7日（星期二）起照常开市。",
    "（四）劳动节：5月1日（星期五）至5月5日（星期二）休市，5月6日（星期三）起照常开市。另外，4月26日（星期日）、5月9日（星期六）为周末休市。",
    "（五）端午节：6月25日（星期四）至6月27日（星期六）休市，6月29日（星期一）起照常开市。另外，6月28日（星期日）为周末休市。",
    "（六）国庆节、中秋节：10月1日（星期四）至10月8日（星期四）休市，10月9日（星期五）起照常开市。另外，9月27日（星期日）、10月10日（星期六）为周末休市。",
)


class FactorMarketDataTests(unittest.TestCase):
    def setUp(self):
        patcher = patch(
            "research.market_data.provider_access.require_choice_network_access"
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_frozen_universe_is_23_unique_and_semantically_separate(self):
        self.assertEqual(len(ALL_INDEX_IDS), 23)
        self.assertEqual(len(set(ALL_INDEX_IDS)), 23)
        self.assertEqual(len(SCREEN_INDEX_IDS), 11)
        self.assertEqual(len(CONFIRM_INDEX_IDS), 11)
        self.assertEqual(BENCHMARK_INDEX_ID, "000985.CSI")
        self.assertNotIn("000992.CSI", ALL_INDEX_IDS)
        self.assertNotIn("932087.CSI", ALL_INDEX_IDS)
        records = frozen_universe_records(available_at=NOW.isoformat())
        self.assertEqual([row["series_role"] for row in records].count("screen"), 11)
        self.assertEqual([row["series_role"] for row in records].count("confirm"), 11)
        self.assertEqual([row["series_role"] for row in records].count("benchmark"), 1)
        mapping = {row["index_id"]: row["industry_key"] for row in records}
        self.assertEqual(mapping["932076.CSI"], "real_estate")
        self.assertEqual(mapping["000993.CSI"], "information_technology")
        self.assertEqual(mapping["931775.CSI"], "real_estate")
        self.assertTrue(all(row["source_status"] == "unverified_until_probe" for row in records))

    def test_request_rejects_bare_or_unknown_index_and_partial_universe(self):
        for bad in (("000986",), ("000992.CSI",), ("932087.CSI",)):
            with self.subTest(bad=bad), self.assertRaises(IndexEvidenceError):
                request(index_ids=bad)
        with self.assertRaisesRegex(IndexEvidenceError, "all 23"):
            request(CSI_INDUSTRY_UNIVERSE, index_ids=("000986.CSI",))

    def test_choice_aliases_are_fixed_separate_and_exactly_one_per_index(self):
        self.assertEqual(set(CHOICE_INDEX_ALIASES), set(ALL_INDEX_IDS))
        self.assertEqual(CHOICE_INDEX_ALIASES["000986.CSI"], "000986.CSI")
        self.assertEqual(CHOICE_ALIAS_STATUS, "unverified_until_probe")
        client = FakeChoice()
        payload = ChoiceIndexProvider(
            sdk_loader=lambda: SimpleNamespace(c=client), clock=lambda: NOW
        ).fetch(request(index_ids=("000986.CSI", "932077.CSI")))
        self.assertEqual(payload.availability_status, "policy_estimated")
        self.assertEqual(len([call for call in client.calls if call[0] == "tradedates"]), 1)
        self.assertEqual(
            [call[1][0] for call in client.calls if call[0] == "csd"],
            ["000986.CSI", "932077.CSI"],
        )
        self.assertEqual(payload.records[0]["available_at"], "2026-08-10T15:30:00+08:00")
        self.assertEqual(payload.records[0]["high"], "102")

    def test_choice_missing_calendar_day_fails_closed(self):
        client = FakeChoice(responses=[choice_levels("000986.CSI", days=("2026-08-10",))])
        provider = ChoiceIndexProvider(
            sdk_loader=lambda: SimpleNamespace(c=client), clock=lambda: NOW
        )
        with self.assertRaisesRegex(ProviderQueryError, "exactly equal"):
            provider.fetch(request())

    def test_choice_first_quota_error_stops_without_trying_another_alias(self):
        client = FakeChoice(
            responses=[choice_result(10001029, "data limit exceeded")]
        )
        provider = ChoiceIndexProvider(
            sdk_loader=lambda: SimpleNamespace(c=client), clock=lambda: NOW
        )
        with self.assertRaises(ProviderQuotaExceededError):
            provider.fetch(request(index_ids=("000986.CSI", "932077.CSI")))
        self.assertEqual(len([call for call in client.calls if call[0] == "csd"]), 1)
        self.assertEqual(client.calls[-1][0], "stop")

    def test_choice_sdk_allowlist_rejects_state_change(self):
        with self.assertRaisesRegex(ProviderQueryError, "read-only allowlist"):
            ChoiceIndexProvider._sdk_call(FakeChoice(), "porder")

    def test_injected_csi_transport_is_never_officially_admitted(self):
        transport = RoutingTransport(
            lambda url: {
                "data": [
                    {
                        "indexCode": "000986",
                        "tradeDate": "2026-08-10",
                        "open": "100",
                        "high": "102",
                        "low": "99",
                        "close": "101",
                    }
                ]
            },
            final_host="www.csindex.com.cn",
        )
        provider = CSIOfficialProvider(transport=transport, clock=lambda: NOW)
        with tempfile.TemporaryDirectory() as directory:
            bundle = IndexEvidenceService(directory).capture(
                provider,
                request(start="2026-08-10", end="2026-08-10"),
            )
            self.assertEqual(bundle.admission_status, "test_injected_not_admitted")
            self.assertEqual(bundle.transport_receipts[0]["transport_mode"], "test_injected_https")
            self.assertEqual(bundle.records[0]["availability_status"], "policy_estimated")

    def test_csi_close_only_stays_null_for_all_optional_ohl_fields(self):
        transport = RoutingTransport(
            lambda url: {"data": [{"indexCode": "000986", "tradeDate": "2026-08-10", "close": "101"}]},
            final_host="www.csindex.com.cn",
        )
        provider = CSIOfficialProvider(transport=transport, clock=lambda: NOW)
        payload = provider.fetch(request(start="2026-08-10", end="2026-08-10"))
        self.assertIsNone(payload.records[0]["open"])
        self.assertIsNone(payload.records[0]["high"])
        self.assertIsNone(payload.records[0]["low"])

    def test_csi_accepts_source_owned_compact_trade_date_shape(self):
        transport = RoutingTransport(
            lambda url: {
                "data": [
                    {
                        "indexCode": "000986",
                        "tradeDate": "20260810",
                        "open": 100.0,
                        "high": 102.0,
                        "low": 99.0,
                        "close": 101.0,
                    }
                ]
            },
            final_host="www.csindex.com.cn",
        )
        payload = CSIOfficialProvider(transport=transport, clock=lambda: NOW).fetch(
            request(start="2026-08-10", end="2026-08-10")
        )
        self.assertEqual(payload.records[0]["trading_date"], "2026-08-10")

    def test_injected_universe_verifies_names_but_remains_not_admitted(self):
        name_by_code = {
            row["official_index_code"]: row["industry_name_cn"]
            for row in frozen_universe_records(available_at=NOW.isoformat())
        }
        transport = RoutingTransport(
            lambda url: {
                "data": [
                    {
                        "indexCode": url.rsplit("/", 1)[1],
                        "indexName": name_by_code[url.rsplit("/", 1)[1]],
                    }
                ]
            },
            final_host="www.csindex.com.cn",
        )
        provider = CSIOfficialProvider(transport=transport, clock=lambda: NOW)
        with tempfile.TemporaryDirectory() as directory:
            bundle = IndexEvidenceService(directory).capture(
                provider,
                request(CSI_INDUSTRY_UNIVERSE, index_ids=ALL_INDEX_IDS),
            )
            self.assertEqual(len(bundle.records), 23)
            self.assertTrue(all(row["source_status"] == "verified_official_basic_info" for row in bundle.records))
            self.assertEqual(bundle.admission_status, "test_injected_not_admitted")

    def test_sse_calendar_requires_every_natural_day_and_strict_session_times(self):
        annual_url = sse_calendar_module._ANNUAL_NOTICE_URLS[2026]
        complete = HTMLRoutingTransport(
            {
                annual_url: sse_article(
                    "关于上海证券交易所2026年部分节假日休市安排的通知",
                    "2025-12-22",
                    SSE_2026,
                )
            }
        )
        provider = SSECalendarProvider(transport=complete, clock=lambda: NOW)
        with tempfile.TemporaryDirectory() as directory:
            bundle = IndexEvidenceService(directory).capture(
                provider,
                request(CN_EQUITY_SESSION, start="2026-08-09", end="2026-08-11"),
            )
            self.assertEqual(bundle.admission_status, "test_injected_not_admitted")
            self.assertIsNone(bundle.records[0]["session_open_at"])
            self.assertEqual(bundle.records[1]["session_open_at"], "2026-08-10T09:30:00+08:00")
            self.assertEqual(bundle.records[1]["session_close_at"], "2026-08-10T15:00:00+08:00")
            self.assertTrue(bundle.records[1]["available_at"].endswith("+08:00"))
            self.assertEqual(len(bundle.records), 3)
            self.assertEqual(len(bundle.transport_receipts), 1)
            self.assertEqual(complete.urls, [annual_url])

        incomplete = HTMLRoutingTransport(
            {
                annual_url: sse_article(
                    "关于上海证券交易所2026年部分节假日休市安排的通知",
                    "2025-12-22",
                    SSE_2026[:-1],
                )
            }
        )
        with self.assertRaisesRegex(ProviderQueryError, "complete holiday set"):
            SSECalendarProvider(transport=incomplete, clock=lambda: NOW).fetch(
                request(CN_EQUITY_SESSION, start="2026-08-09", end="2026-08-11")
            )

    def test_sse_calendar_applies_2019_and_2020_official_amendments(self):
        annual_2019 = sse_calendar_module._ANNUAL_NOTICE_URLS[2019]
        amendment_2019 = sse_calendar_module._AMENDMENT_URLS[2019][1]
        annual_2020 = sse_calendar_module._ANNUAL_NOTICE_URLS[2020]
        amendment_2020 = sse_calendar_module._AMENDMENT_URLS[2020][1]
        transport = HTMLRoutingTransport(
            {
                annual_2019: sse_article(
                    "关于上海证券交易所2019年全年休市安排的通知",
                    "2018-12-20",
                    SSE_2019,
                ),
                amendment_2019: sse_article(
                    "关于调整2019年劳动节休市安排的公告",
                    "2019-04-18",
                    (
                        "一、休市安排：5月1日（星期三）至5月4日（星期六）休市，5月6日（星期一）起照常开市。4月28日（星期日）和5月5日（星期日）为周末休市。",
                    ),
                ),
                annual_2020: sse_article(
                    "关于上海证券交易所2020年全年休市安排的通知",
                    "2019-12-20",
                    SSE_2020,
                ),
                amendment_2020: sse_article(
                    "关于调整2020年春节休市相关安排的公告",
                    "2020-01-27",
                    (
                        "一、延长2020年春节休市至2月2日（星期日），2月3日（星期一）正常开市。",
                        "二、原定于2020年1月31日实施的业务，原则上顺延至2月3日实施。",
                    ),
                ),
            }
        )
        provider = SSECalendarProvider(transport=transport, clock=lambda: NOW)
        payload_2019 = provider.fetch(
            request(CN_EQUITY_SESSION, start="2019-04-30", end="2019-05-06")
        )
        rows_2019 = {row["calendar_date"]: row for row in payload_2019.records}
        self.assertFalse(rows_2019["2019-05-02"]["is_trading_day"])
        self.assertFalse(rows_2019["2019-05-03"]["is_trading_day"])
        self.assertTrue(rows_2019["2019-05-06"]["is_trading_day"])
        self.assertEqual(len(payload_2019.transport_receipts), 2)

        payload_2020 = provider.fetch(
            request(CN_EQUITY_SESSION, start="2020-01-30", end="2020-02-03")
        )
        rows_2020 = {row["calendar_date"]: row for row in payload_2020.records}
        self.assertFalse(rows_2020["2020-01-31"]["is_trading_day"])
        self.assertTrue(rows_2020["2020-02-03"]["is_trading_day"])
        self.assertEqual(len(payload_2020.transport_receipts), 2)

    def test_offline_replay_verifies_raw_normalized_receipt_and_as_of(self):
        transport = RoutingTransport(
            lambda url: {"data": [{"indexCode": "000986", "tradeDate": "2026-08-10", "close": "101"}]},
            final_host="www.csindex.com.cn",
        )
        with tempfile.TemporaryDirectory() as directory:
            service = IndexEvidenceService(directory)
            online = request(start="2026-08-10", end="2026-08-10")
            captured = service.capture(CSIOfficialProvider(transport=transport, clock=lambda: NOW), online)
            offline = request(
                start="2026-08-10", end="2026-08-10", mode="offline_replay", requested_at=NOW
            )
            loaded = load_index_panel(directory, "csi_official", offline, NOW)
            self.assertEqual(loaded["evidence_id"], captured.evidence_id)
            self.assertEqual(loaded["source_id"], "csi_official")
            self.assertIn("raw_content_sha256", loaded)
            self.assertIn("normalized_content_sha256", loaded)
            self.assertIn("transport_receipts", loaded)

            raw_path = Path(directory) / "raw" / f"{captured.raw_content_sha256}.raw"
            original = raw_path.read_bytes()
            raw_path.write_bytes(original + b" ")
            with self.assertRaisesRegex(IndexEvidenceStorageError, "raw evidence hash"):
                load_index_panel(directory, "csi_official", offline, NOW)

    def test_schema_files_accept_contract_examples(self):
        root = Path(__file__).resolve().parents[1] / "schemas"
        index_record = {
            "schema_version": "index-level-v1",
            "index_id": "000986.CSI",
            "trading_date": "2026-08-10",
            "open": None,
            "high": None,
            "low": None,
            "close": "101",
            "currency": "CNY",
            "basis": "index_points_unadjusted",
            "available_at": "2026-08-10T15:30:00+08:00",
            "availability_status": "policy_estimated",
            "source_record_id": "x",
        }
        validate_json_schema(index_record, root / "index_level.v1.json")
        validate_json_schema(
            frozen_universe_records(available_at=NOW.isoformat())[0],
            root / "csi_industry_universe.v1.json",
        )


if __name__ == "__main__":
    unittest.main()
