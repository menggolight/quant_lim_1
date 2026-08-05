from __future__ import annotations

import json
import socket
import ssl
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import URLError
from urllib.request import ProxyHandler

from research.broker_report_audit.models import CHINA_TZ
from research.broker_report_audit.sources import (
    EASTMONEY_IPV4_ONLY_HOSTS,
    CachedHttpClient,
    EastmoneyIndustryBoardSource,
    EastmoneySource,
    HttpResponse,
    IncompleteSourceBatchError,
    MalformedResponseError,
    SourceError,
    _SelectiveIPv4HTTPSConnection,
    _create_ipv4_connection,
    _normalise_network_host,
    _selective_create_connection,
)
from research.broker_report_audit.storage import ContentAddressedHttpCache


NOW = datetime(2026, 8, 6, 10, 0, tzinfo=CHINA_TZ)


def response(
    payload: object,
    *,
    page: int = 1,
    from_cache: bool = False,
    fetched_at: datetime | None = None,
) -> HttpResponse:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    digest_character = format(page % 16, "x")
    return HttpResponse(
        url="https://push2.eastmoney.com/api/qt/clist/get",
        status=200,
        headers={"Content-Type": "application/json; charset=utf-8"},
        body=body,
        fetched_at=fetched_at or NOW + timedelta(seconds=page),
        content_hash=digest_character * 64,
        from_cache=from_cache,
    )


class _NetworkResponse:
    def __init__(self, body: bytes = b'{"ok":true}') -> None:
        self.status = 200
        self.headers = {"Content-Type": "application/json"}
        self._body = body

    def getcode(self) -> int:
        return self.status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_NetworkResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class StrictJsonTests(unittest.TestCase):
    def make_response(self, payload: bytes) -> HttpResponse:
        return HttpResponse(
            url="https://example.test/data",
            status=200,
            headers={"Content-Type": "application/json"},
            body=payload,
            fetched_at=NOW,
            content_hash="a" * 64,
            from_cache=False,
        )

    def test_plain_json_and_single_jsonp_callback_are_supported(self) -> None:
        self.assertEqual(self.make_response(b'{"ok":true}').json(), {"ok": True})
        self.assertEqual(
            self.make_response(b'jQuery_123({"ok":true});').json(),
            {"ok": True},
        )

    def test_ambiguous_or_non_standard_json_is_rejected(self) -> None:
        invalid = (
            b"",
            b"<html>captcha</html>",
            b'{"value":NaN}',
            b'{"key":1,"key":2}',
            b'prefix junk({"ok":true}) suffix',
        )
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(MalformedResponseError):
                    self.make_response(payload).json()


class IPv4TransportTests(unittest.TestCase):
    def test_host_allowlist_is_exact_and_normalised(self) -> None:
        self.assertEqual(
            _normalise_network_host("PUSH2.EASTMONEY.COM."),
            "push2.eastmoney.com",
        )
        for invalid in ("*.eastmoney.com", "host:443", "bad host", "https://host"):
            with self.subTest(host=invalid):
                with self.assertRaises(ValueError):
                    _normalise_network_host(invalid)

    def test_ipv4_connector_tries_all_a_records_and_closes_failed_socket(self) -> None:
        first = MagicMock()
        second = MagicMock()
        first.connect.side_effect = OSError("first A record failed")
        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.2", 443)),
        ]
        with patch(
            "research.broker_report_audit.sources.socket.getaddrinfo",
            return_value=addresses,
        ) as getaddrinfo, patch(
            "research.broker_report_audit.sources.socket.socket",
            side_effect=[first, second],
        ):
            connected = _create_ipv4_connection(
                ("push2.eastmoney.com", 443),
                timeout=5.0,
            )
        self.assertIs(connected, second)
        getaddrinfo.assert_called_once_with(
            "push2.eastmoney.com",
            443,
            family=socket.AF_INET,
            type=socket.SOCK_STREAM,
        )
        first.close.assert_called_once_with()
        second.connect.assert_called_once_with(("192.0.2.2", 443))

    def test_non_allowlisted_or_ipv6_proxy_endpoint_uses_system_connector(self) -> None:
        sentinel = MagicMock()
        with patch(
            "research.broker_report_audit.sources.socket.create_connection",
            return_value=sentinel,
        ) as create_connection, patch(
            "research.broker_report_audit.sources._create_ipv4_connection"
        ) as ipv4:
            ordinary = _selective_create_connection(
                ("proxy.example", 8080),
                3.0,
                None,
                ipv4_only_hosts=EASTMONEY_IPV4_ONLY_HOSTS,
            )
            ipv6_proxy = _selective_create_connection(
                ("::1", 8080),
                3.0,
                None,
                ipv4_only_hosts=EASTMONEY_IPV4_ONLY_HOSTS,
            )
        self.assertIs(ordinary, sentinel)
        self.assertIs(ipv6_proxy, sentinel)
        self.assertEqual(create_connection.call_count, 2)
        ipv4.assert_not_called()

    def test_exact_allowlisted_host_uses_ipv4_connector_only(self) -> None:
        sentinel = MagicMock()
        with patch(
            "research.broker_report_audit.sources._create_ipv4_connection",
            return_value=sentinel,
        ) as ipv4, patch(
            "research.broker_report_audit.sources.socket.create_connection"
        ) as system_connector:
            connected = _selective_create_connection(
                ("17.PUSH2.EASTMONEY.COM.", 443),
                4.0,
                None,
                ipv4_only_hosts=EASTMONEY_IPV4_ONLY_HOSTS,
            )
        self.assertIs(connected, sentinel)
        ipv4.assert_called_once_with(
            ("17.PUSH2.EASTMONEY.COM.", 443),
            4.0,
            None,
        )
        system_connector.assert_not_called()

    def test_https_keeps_original_dns_name_for_sni(self) -> None:
        context = MagicMock()
        raw_socket = MagicMock()
        wrapped_socket = MagicMock()
        context.wrap_socket.return_value = wrapped_socket
        connection = _SelectiveIPv4HTTPSConnection(
            "push2his.eastmoney.com",
            timeout=3.0,
            context=context,
            ipv4_only_hosts=EASTMONEY_IPV4_ONLY_HOSTS,
        )

        def connect_without_network(instance: object) -> None:
            setattr(instance, "sock", raw_socket)

        with patch.object(
            __import__("http.client").client.HTTPConnection,
            "connect",
            autospec=True,
            side_effect=connect_without_network,
        ):
            connection.connect()
        context.wrap_socket.assert_called_once_with(
            raw_socket,
            server_hostname="push2his.eastmoney.com",
        )
        self.assertIs(connection.sock, wrapped_socket)

    def test_client_injected_opener_preserves_cache_and_offline_replay(self) -> None:
        calls: list[str] = []

        def opener(request: object, *, timeout: float) -> _NetworkResponse:
            calls.append(getattr(request, "full_url"))
            self.assertEqual(timeout, 3.0)
            return _NetworkResponse()

        with tempfile.TemporaryDirectory() as directory:
            with ContentAddressedHttpCache(directory) as cache:
                client = CachedHttpClient(
                    cache,
                    timeout=3.0,
                    rate_limit_seconds=0,
                    request_opener=opener,
                )
                first = client.get("https://example.test/data")
                second = client.get("https://example.test/data")
                offline = CachedHttpClient(
                    cache,
                    offline=True,
                    request_opener=lambda *_args, **_kwargs: self.fail("network used"),
                ).get("https://example.test/data")
        self.assertEqual(calls, ["https://example.test/data"])
        self.assertFalse(first.from_cache)
        self.assertTrue(second.from_cache)
        self.assertTrue(offline.from_cache)

    def test_tls_verification_error_is_not_retried_or_cached(self) -> None:
        calls = 0
        sleeps: list[float] = []

        def opener(*_args: object, **_kwargs: object) -> _NetworkResponse:
            nonlocal calls
            calls += 1
            raise URLError(ssl.SSLCertVerificationError("hostname mismatch"))

        with tempfile.TemporaryDirectory() as directory:
            with ContentAddressedHttpCache(directory) as cache:
                client = CachedHttpClient(
                    cache,
                    max_retries=3,
                    rate_limit_seconds=0,
                    sleeper=sleeps.append,
                    request_opener=opener,
                )
                with self.assertRaisesRegex(SourceError, "TLS certificate"):
                    client.get("https://example.test/data")
                resolved_url = CachedHttpClient.canonical_url(
                    "https://example.test/data"
                )
                request_key = CachedHttpClient.request_key(
                    resolved_url,
                    {
                        "Accept": "application/json,text/html,application/pdf,*/*",
                        "User-Agent": client.user_agent,
                    },
                )
                self.assertEqual(tuple(cache.iter_versions(request_key)), ())
        self.assertEqual(calls, 1)
        self.assertEqual(sleeps, [])

    def test_transient_network_failure_is_bounded_to_two_attempts(self) -> None:
        calls = 0
        sleeps: list[float] = []

        def opener(*_args: object, **_kwargs: object) -> _NetworkResponse:
            nonlocal calls
            calls += 1
            raise ConnectionResetError("remote reset")

        with tempfile.TemporaryDirectory() as directory:
            with ContentAddressedHttpCache(directory) as cache:
                client = CachedHttpClient(
                    cache,
                    max_retries=1,
                    rate_limit_seconds=0,
                    sleeper=sleeps.append,
                    request_opener=opener,
                )
                with self.assertRaises(SourceError):
                    client.get("https://push2his.eastmoney.com/data", refresh=True)
        self.assertEqual(calls, 2)
        self.assertEqual(len(sleeps), 1)

    def test_selective_opener_keeps_default_proxy_handler(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with ContentAddressedHttpCache(directory) as cache:
                with patch(
                    "urllib.request.getproxies",
                    return_value={"https": "http://proxy.example:8080"},
                ):
                    client = CachedHttpClient(
                        cache,
                        ipv4_only_hosts=EASTMONEY_IPV4_ONLY_HOSTS,
                    )
                self.assertTrue(
                    any(isinstance(handler, ProxyHandler) for handler in client._opener.handlers)
                )
                client.close()


class _PagedIndustryClient:
    def __init__(self, pages: dict[int, HttpResponse | BaseException]) -> None:
        self.pages = pages
        self.calls: list[tuple[int, bool]] = []

    def get(
        self,
        _url: str,
        params: dict[str, str],
        *,
        refresh: bool,
        **_kwargs: object,
    ) -> HttpResponse:
        page = int(params["pn"])
        self.calls.append((page, refresh))
        value = self.pages[page]
        if isinstance(value, BaseException):
            raise value
        return value


def board_payload(total: int, rows: list[tuple[str, str, str]]) -> dict[str, object]:
    return {
        "rc": 0,
        "data": {
            "total": total,
            "diff": [
                {"f12": code, "f14": name, "f2": price, "f3": "1.25"}
                for code, name, price in rows
            ],
        },
    }


class IndustryBoardSourceTests(unittest.TestCase):
    def test_complete_two_page_snapshot_is_returned_atomically(self) -> None:
        client = _PagedIndustryClient(
            {
                1: response(board_payload(3, [("BK1", "行业一", "100"), ("BK2", "行业二", "90")]), page=1),
                2: response(board_payload(3, [("BK3", "行业三", "80")]), page=2),
            }
        )
        batch = EastmoneyIndustryBoardSource(client).fetch_snapshot(page_size=2)
        self.assertEqual(batch.expected_total, 3)
        self.assertEqual(batch.pages_fetched, 2)
        self.assertEqual([row.board_id for row in batch.records], ["BK1", "BK2", "BK3"])
        self.assertEqual(str(batch.records[0].metrics["last_price"]), "100")
        self.assertEqual(client.calls, [(1, True), (2, True)])
        self.assertFalse(batch.all_from_cache)

    def test_page_failure_is_raised_before_first_record_can_escape(self) -> None:
        client = _PagedIndustryClient(
            {
                1: response(board_payload(2, [("BK1", "行业一", "100")]), page=1),
                2: SourceError("page two failed"),
            }
        )
        with self.assertRaisesRegex(SourceError, "page two failed"):
            EastmoneyIndustryBoardSource(client).fetch_snapshot(page_size=1)

    def test_changed_total_duplicate_and_early_empty_page_fail_closed(self) -> None:
        cases = {
            "changed total": {
                1: response(board_payload(2, [("BK1", "行业一", "100")]), page=1),
                2: response(board_payload(3, [("BK2", "行业二", "90")]), page=2),
            },
            "duplicate": {
                1: response(board_payload(2, [("BK1", "行业一", "100")]), page=1),
                2: response(board_payload(2, [("BK1", "行业一", "100")]), page=2),
            },
            "early empty": {
                1: response(board_payload(2, [("BK1", "行业一", "100")]), page=1),
                2: response(board_payload(2, []), page=2),
            },
        }
        for name, pages in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(IncompleteSourceBatchError):
                    EastmoneyIndustryBoardSource(
                        _PagedIndustryClient(pages)
                    ).fetch_snapshot(page_size=1)

    def test_empty_or_cached_current_snapshot_is_not_success(self) -> None:
        empty = _PagedIndustryClient({1: response(board_payload(0, []), page=1)})
        with self.assertRaises(IncompleteSourceBatchError):
            EastmoneyIndustryBoardSource(empty).fetch_snapshot()

        cached = _PagedIndustryClient(
            {
                1: response(
                    board_payload(1, [("BK1", "行业一", "100")]),
                    page=1,
                    from_cache=True,
                )
            }
        )
        with self.assertRaises(IncompleteSourceBatchError):
            EastmoneyIndustryBoardSource(cached).fetch_snapshot(refresh=False)

    def test_explicit_offline_replay_is_marked_as_cached(self) -> None:
        cached = _PagedIndustryClient(
            {
                1: response(
                    board_payload(1, [("BK1", "行业一", "100")]),
                    page=1,
                    from_cache=True,
                )
            }
        )
        batch = EastmoneyIndustryBoardSource(cached).fetch_snapshot(
            refresh=False,
            require_live=False,
        )
        self.assertTrue(batch.all_from_cache)

    def test_mixed_cache_mode_or_wide_fetch_span_fails_closed(self) -> None:
        mixed = _PagedIndustryClient(
            {
                1: response(board_payload(2, [("BK1", "行业一", "100")]), page=1),
                2: response(
                    board_payload(2, [("BK2", "行业二", "90")]),
                    page=2,
                    from_cache=True,
                ),
            }
        )
        with self.assertRaises(IncompleteSourceBatchError):
            EastmoneyIndustryBoardSource(mixed).fetch_snapshot(
                page_size=1,
                require_live=False,
            )

        wide_span = _PagedIndustryClient(
            {
                1: response(
                    board_payload(2, [("BK1", "行业一", "100")]),
                    page=1,
                    fetched_at=NOW,
                ),
                2: response(
                    board_payload(2, [("BK2", "行业二", "90")]),
                    page=2,
                    fetched_at=NOW + timedelta(minutes=3),
                ),
            }
        )
        with self.assertRaises(IncompleteSourceBatchError):
            EastmoneyIndustryBoardSource(wide_span).fetch_snapshot(
                page_size=1,
                max_fetch_span_seconds=120,
            )


class _PagedReportClient:
    def __init__(self, pages: dict[int, dict[str, object] | BaseException]) -> None:
        self.pages = pages
        self.calls: list[int] = []

    def get(self, _url: str, params: dict[str, str], **_kwargs: object) -> HttpResponse:
        page = int(params["pageNo"])
        self.calls.append(page)
        value = self.pages[page]
        if isinstance(value, BaseException):
            raise value
        result = response(value, page=page)
        return HttpResponse(
            url="https://reportapi.eastmoney.com/report/list",
            status=result.status,
            headers=result.headers,
            body=result.body,
            fetched_at=result.fetched_at,
            content_hash=result.content_hash,
            from_cache=result.from_cache,
        )


def report_payload(total_pages: int, info_codes: list[str]) -> dict[str, object]:
    return {
        "TotalPage": total_pages,
        "currentYear": 2024,
        "data": [
            {
                "title": f"报告{code}",
                "orgSName": "测试券商",
                "publishDate": "2024-01-05 00:00:00",
                "infoCode": code,
                "stockCode": "000333",
                "stockName": "美的集团",
                "researcher": "分析师甲",
            }
            for code in info_codes
        ],
    }


class ReportPaginationTests(unittest.TestCase):
    def test_reports_are_not_yielded_until_all_pages_validate(self) -> None:
        source = EastmoneySource(
            _PagedReportClient(
                {
                    1: report_payload(2, ["ONE"]),
                    2: SourceError("second page failed"),
                }
            )
        )
        iterator = source.iter_reports("stock", "2024-01-01", "2024-01-31")
        with self.assertRaisesRegex(SourceError, "second page failed"):
            next(iterator)

    def test_stable_complete_report_pages_succeed(self) -> None:
        source = EastmoneySource(
            _PagedReportClient(
                {
                    1: report_payload(2, ["ONE"]),
                    2: report_payload(2, ["TWO"]),
                }
            )
        )
        reports = source.fetch_reports("stock", "2024-01-01", "2024-01-31")
        self.assertEqual([item.report_id for item in reports], [
            "eastmoney:stock:ONE",
            "eastmoney:stock:TWO",
        ])

    def test_duplicate_future_filtered_row_still_proves_broken_pagination(self) -> None:
        pages = {
            1: report_payload(2, ["FUTURE"]),
            2: report_payload(2, ["FUTURE"]),
        }
        source = EastmoneySource(_PagedReportClient(pages))
        with self.assertRaises(IncompleteSourceBatchError):
            source.fetch_reports(
                "stock",
                "2024-01-01",
                "2024-01-31",
                as_of=datetime(2024, 1, 5, 8, 0, tzinfo=CHINA_TZ),
            )

    def test_report_total_change_duplicate_empty_and_page_cap_fail_closed(self) -> None:
        cases = (
            (
                {1: report_payload(2, ["ONE"]), 2: report_payload(3, ["TWO"])},
                {},
            ),
            (
                {1: report_payload(2, ["ONE"]), 2: report_payload(2, ["ONE"])},
                {},
            ),
            (
                {1: report_payload(2, ["ONE"]), 2: report_payload(2, [])},
                {},
            ),
            (
                {1: report_payload(2, ["ONE"])},
                {"max_pages": 1},
            ),
        )
        for pages, kwargs in cases:
            with self.subTest(pages=pages, kwargs=kwargs):
                with self.assertRaises(IncompleteSourceBatchError):
                    EastmoneySource(_PagedReportClient(pages)).fetch_reports(
                        "stock",
                        "2024-01-01",
                        "2024-01-31",
                        **kwargs,
                    )


if __name__ == "__main__":
    unittest.main()
