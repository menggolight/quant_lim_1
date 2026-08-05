from __future__ import annotations

import json
import tempfile
import unittest
from http.client import RemoteDisconnected
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.eastmoney_source_probe import (
    _error_result,
    _validated_output_path,
    build_parser,
    run_probe,
)
from research.broker_report_audit.sources import SourceError
from research.broker_report_audit.models import CHINA_TZ


class _MarketSource:
    def __init__(self, _client: object) -> None:
        self.last_issues: list[dict[str, object]] = []

    def daily_bars(self, *_args: object, **_kwargs: object) -> tuple[object, ...]:
        return (
            SimpleNamespace(
                trade_date=date(2026, 8, 4),
                adjusted_close=79,
                source="eastmoney_public.push2his",
                content_hash="a" * 64,
            ),
            SimpleNamespace(
                trade_date=date(2026, 8, 5),
                adjusted_close=80,
                source="eastmoney_public.push2his",
                content_hash="a" * 64,
            ),
        )


class _IndustrySource:
    def __init__(self, _client: object) -> None:
        pass

    def fetch_snapshot(self, **_kwargs: object) -> object:
        return SimpleNamespace(
            expected_total=2,
            records=(
                SimpleNamespace(board_id="BK1"),
                SimpleNamespace(board_id="BK2"),
            ),
            pages_fetched=1,
            first_fetched_at=datetime(2026, 8, 6, 10, 0, tzinfo=CHINA_TZ),
            last_fetched_at=datetime(2026, 8, 6, 10, 0, tzinfo=CHINA_TZ),
            all_from_cache=False,
            source="eastmoney_public.push2",
            source_url="https://17.push2.eastmoney.com/api/qt/clist/get",
            content_hash="b" * 64,
        )


class EastmoneySourceProbeTests(unittest.TestCase):
    def test_failure_output_preserves_the_root_transport_error_type(self) -> None:
        try:
            try:
                raise RemoteDisconnected("remote closed")
            except RemoteDisconnected as root:
                raise SourceError("request failed") from root
        except SourceError as exc:
            result = _error_result(exc)
        self.assertEqual(result["error_type"], "SourceError")
        self.assertEqual(result["root_error_type"], "RemoteDisconnected")

    def test_cli_requires_explicit_expected_last_trading_date(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([])

    def test_probe_refuses_controlled_paths_and_non_probe_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protected = root / "signals"
            with patch(
                "agent.eastmoney_source_probe.PROTECTED_OUTPUT_ROOTS",
                (protected,),
            ):
                with self.assertRaisesRegex(ValueError, "controlled observations"):
                    _validated_output_path(protected / "sealed.json")

            occupied = root / "important.json"
            original = '{"schema_version":"market-observation-v0.1"}\n'
            occupied.write_text(original, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-probe"):
                run_probe(
                    stock_id="000333.SZ",
                    start_date=date(2026, 8, 1),
                    end_date=date(2026, 8, 5),
                    expected_last_date=date(2026, 8, 5),
                    cache_directory=root / "cache",
                    output_path=occupied,
                    timeout=1,
                    rate_limit_seconds=0,
                )
            self.assertEqual(occupied.read_text(encoding="utf-8"), original)

    def test_probe_writes_a_non_trading_diagnostic_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "probe.json"
            with patch(
                "agent.eastmoney_source_probe.EastmoneyMarketSource",
                _MarketSource,
            ), patch(
                "agent.eastmoney_source_probe.EastmoneyIndustryBoardSource",
                _IndustrySource,
            ):
                result, exit_code = run_probe(
                    stock_id="000333.SZ",
                    start_date=date(2026, 8, 1),
                    end_date=date(2026, 8, 5),
                    expected_last_date=date(2026, 8, 5),
                    cache_directory=root / "cache",
                    output_path=output,
                    timeout=1,
                    rate_limit_seconds=0,
                )
            written = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["overall_status"], "passed")
        self.assertEqual(written["safety_status"], "research_only_not_trade_eligible")
        self.assertEqual(written["checks"]["industry_board"]["unique_board_count"], 2)
        self.assertNotIn("orders", written)

    def test_wrong_last_date_fails_even_when_transport_returns_bars(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch(
                "agent.eastmoney_source_probe.EastmoneyMarketSource",
                _MarketSource,
            ), patch(
                "agent.eastmoney_source_probe.EastmoneyIndustryBoardSource",
                _IndustrySource,
            ):
                result, exit_code = run_probe(
                    stock_id="000333.SZ",
                    start_date=date(2026, 8, 1),
                    end_date=date(2026, 8, 6),
                    expected_last_date=date(2026, 8, 6),
                    cache_directory=root / "cache",
                    output_path=root / "probe.json",
                    timeout=1,
                    rate_limit_seconds=0,
                )
        self.assertEqual(exit_code, 1)
        self.assertEqual(result["overall_status"], "failed")
        self.assertEqual(result["checks"]["market_history"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
