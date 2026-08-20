from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.choice_quality_growth_batch import build_parser, main
from research.market_data import choice_quality_growth_batch as batch
from research.market_data.choice_quality_growth_batch import (
    CSD_FIELDS,
    CALENDAR_BLOCKER,
    CSS_FIELDS,
    CSS_LIST_DATE_FIELDS,
    CSS_STATE_FIELDS,
    INDUSTRY_BLOCKER,
    PRICE_BASES,
    SECTOR_CODE,
    collect_choice_quality_growth_batch,
    derive_choice_quality_growth_decision_grid,
    fixed_choice_quality_growth_plan,
    verify_choice_quality_growth_batch,
)
from research.market_data.contracts import MarketDataRequest, canonical_json_bytes
from research.market_data.providers.base import (
    NetworkBlockedError,
    ProviderPayload,
    ProviderQueryError,
    UnsupportedDatasetError,
)
from research.market_data.providers.choice import ChoiceProvider


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
NOW = datetime(2026, 8, 19, 10, 0, tzinfo=CHINA_TZ)


def response(**values):
    return SimpleNamespace(ErrorCode=0, ErrorMsg="success", **values)


class FixedClient:
    def __init__(self, *, empty_state_dates: bool = False) -> None:
        self.start_calls = []
        self.stop_calls = 0
        self.csd_calls = []
        self.css_calls = []
        self.sector_calls = []
        self.tradedates_calls = []
        self.empty_state_dates = empty_state_dates

    def start(self, options, callback):
        self.start_calls.append((options, callback))
        return response()

    def stop(self):
        self.stop_calls += 1
        return response()

    def tradedates(self, *args):
        self.tradedates_calls.append(args)
        return response(
            Codes=[""],
            Indicators=["TRADEDATE"],
            Dates=["2018-01-02"],
            Data=["2018-01-02"],
        )

    def sector(self, *args):
        self.sector_calls.append(args)
        codes = [f"{600000 + index:06d}.SH" for index in range(800)]
        data = []
        for index, code in enumerate(codes):
            data.extend((code, f"样本{index:03d}"))
        return response(
            Codes=codes,
            Indicators=["SECUCODE", "SECURITYSHORTNAME"],
            Dates=["2018-01-02"],
            Data=data,
        )

    def csd(self, *args):
        self.csd_calls.append(args)
        code = args[0]
        values = {
            "OPEN": "10",
            "HIGH": "11",
            "LOW": "9",
            "CLOSE": "10.5",
            "PRECLOSE": "10",
            "VOLUME": "1000",
            "AMOUNT": "10500",
            "TRADESTATUS": "交易",
            "ISSTSTOCK": "0",
            "HIGHLIMIT": "1",
            "LOWLIMIT": "0",
        }
        return response(
            Codes=[code],
            Indicators=list(CSD_FIELDS),
            Dates=["2018-01-02"],
            Data={code: [[values[field]] for field in CSD_FIELDS]},
        )

    def css(self, *args):
        self.css_calls.append(args)
        codes = args[0].split(",")
        indicators = args[1].split(",")
        values = {
            "LIMITUPPRICE": "11",
            "LIMITDOWNPRICE": "9",
            "TRADESTATUS": "交易",
            "ISSTSTOCK": "0",
            "LISTDATE": "2000-01-01",
        }
        is_state = indicators == list(CSS_STATE_FIELDS)
        return response(
            Codes=codes,
            Indicators=indicators,
            Dates=([] if self.empty_state_dates else ["2018-01-02"])
            if is_state
            else [],
            Data={code: [values[field] for field in indicators] for code in codes},
        )


class IncompleteBatchProvider(ChoiceProvider):
    def __init__(self) -> None:
        super().__init__(sdk_loader=lambda: None, clock=lambda: NOW)
        self.calendar_calls = 0
        self.membership_calls = 0

    @contextmanager
    def diagnostic_session(self):
        yield self

    def fetch_quality_growth_calendar(self, start_date, end_date):
        self.calendar_calls += 1
        records = []
        cursor = start_date
        while cursor <= end_date:
            is_session = cursor.weekday() < 5 and cursor != date(2018, 1, 1)
            records.append(
                {
                    "calendar_date": cursor.isoformat(),
                    "is_trading_day": is_session,
                    "available_at": NOW.isoformat(),
                    "availability_status": "unknown",
                    "source_record_id": "0" * 64,
                }
            )
            cursor += timedelta(days=1)
        request = MarketDataRequest(
            dataset_type="trade_calendar",
            start_date=start_date,
            end_date=end_date,
            retrieval_mode="historical_backfill",
            requested_at=NOW,
        )
        raw = {
            "operation": "tradedates",
            "request": request.fingerprint_payload(
                ChoiceProvider.provider_id, ChoiceProvider.adapter_version
            ),
            "trade_calendar": {
                "options": ChoiceProvider._calendar_options("CNSESH"),
                "dates": [
                    row["calendar_date"]
                    for row in records
                    if row["is_trading_day"]
                ],
            },
        }
        return ProviderPayload(
            raw_content=canonical_json_bytes(raw),
            records=tuple(records),
            fetched_at=NOW,
            upstream_source="choice.test.fixed_calendar",
        )

    def fetch_quality_growth_membership(self, membership_date):
        self.membership_calls += 1
        # Deliberately one short: exact 800 is a hard failure, never partial use.
        records = tuple(
            {
                "sector_code": SECTOR_CODE,
                "membership_date": membership_date.isoformat(),
                "instrument_id": f"{600000 + index:06d}.SH",
                "security_short_name": f"样本{index:03d}",
            }
            for index in range(799)
        )
        raw = {
            "operation": "quality_growth_fixed_csi800_sector",
            "request": {
                "sector_code": SECTOR_CODE,
                "membership_date": membership_date.isoformat(),
                "options": ChoiceProvider._QUALITY_GROWTH_SECTOR_OPTIONS,
            },
            "records": records,
        }
        return ProviderPayload(
            raw_content=canonical_json_bytes(raw),
            records=records,
            fetched_at=NOW,
            upstream_source="choice.test.fixed_sector",
        )


class ExecutionDateProbeProvider(IncompleteBatchProvider):
    def __init__(self) -> None:
        super().__init__()
        self.css_dates = []

    def fetch_quality_growth_membership(self, membership_date):
        self.membership_calls += 1
        records = tuple(
            {
                "sector_code": SECTOR_CODE,
                "membership_date": membership_date.isoformat(),
                "instrument_id": f"{600000 + index:06d}.SH",
                "security_short_name": f"样本{index:03d}",
            }
            for index in range(800)
        )
        raw = {
            "operation": "quality_growth_fixed_csi800_sector",
            "request": {
                "sector_code": SECTOR_CODE,
                "membership_date": membership_date.isoformat(),
                "options": ChoiceProvider._QUALITY_GROWTH_SECTOR_OPTIONS,
            },
            "records": records,
        }
        return ProviderPayload(
            raw_content=canonical_json_bytes(raw),
            records=records,
            fetched_at=NOW,
            upstream_source="choice.test.fixed_sector",
        )

    def fetch_quality_growth_css_list_date_batch(self, instrument_ids):
        records = tuple(
            {"instrument_id": item, "listdate": "2000-01-01"}
            for item in instrument_ids
        )
        raw = {
            "operation": "quality_growth_fixed_css_list_date_batch",
            "request": {
                "instrument_ids": list(instrument_ids),
                "indicators": list(CSS_LIST_DATE_FIELDS),
                "options": ChoiceProvider._quality_growth_list_date_options(),
            },
            "response_dates": [],
            "records": records,
        }
        return ProviderPayload(
            raw_content=canonical_json_bytes(raw),
            records=records,
            fetched_at=NOW,
            upstream_source="choice.test.fixed_list_date",
        )

    def fetch_quality_growth_css_state_batch(self, instrument_ids, trading_date):
        self.css_dates.append(trading_date)
        raise RuntimeError("deliberate eligibility stop")


class ChoiceQualityGrowthProviderTests(unittest.TestCase):
    def test_fixed_batch_calls_do_not_open_arbitrary_field_or_none_surface(self):
        client = FixedClient()
        provider = ChoiceProvider(
            sdk_loader=lambda: SimpleNamespace(c=client), clock=lambda: NOW
        )
        with provider.diagnostic_session():
            membership = provider.fetch_quality_growth_membership(date(2018, 1, 2))
            qfq = provider.fetch_quality_growth_csd(
                "000333.SZ",
                date(2018, 1, 2),
                date(2018, 1, 2),
                adjustment="qfq",
            )
            none = provider.fetch_quality_growth_csd(
                "000333.SZ",
                date(2018, 1, 2),
                date(2018, 1, 2),
                adjustment="none",
            )
            css_state = provider.fetch_quality_growth_css_state_batch(
                ("000333.SZ", "600000.SH"), date(2018, 1, 2)
            )
            css_list_date = provider.fetch_quality_growth_css_list_date_batch(
                ("000333.SZ", "600000.SH")
            )
            with self.assertRaises(ProviderQueryError):
                provider.fetch_quality_growth_csd(
                    "000333.SZ",
                    date(2018, 1, 2),
                    date(2018, 1, 2),
                    adjustment="qfq",
                    indicators=("CLOSE",),
                )

        self.assertEqual(len(membership.records), 800)
        self.assertEqual(membership.records[0]["sector_code"], SECTOR_CODE)
        self.assertEqual(qfq.records[0]["adjustment"], "qfq")
        self.assertEqual(none.records[0]["adjustment"], "none")
        self.assertEqual(css_state.records[0]["limitupprice"], "11")
        self.assertEqual(css_list_date.records[0]["listdate"], "2000-01-01")
        self.assertEqual(client.sector_calls[0][0], SECTOR_CODE)
        self.assertEqual(client.sector_calls[0][1], "2018-01-02")
        self.assertEqual(client.csd_calls[0][1], ",".join(CSD_FIELDS))
        self.assertEqual(client.csd_calls[1][1], ",".join(CSD_FIELDS))
        self.assertIn("AdjustFlag=3", client.csd_calls[0][4])
        self.assertIn("AdjustFlag=1", client.csd_calls[1][4])
        self.assertEqual(client.css_calls[0][1], ",".join(CSS_STATE_FIELDS))
        self.assertIn("EndDate=2018-01-02", client.css_calls[0][2])
        self.assertNotIn("TradeDate=", client.css_calls[0][2])
        self.assertEqual(client.css_calls[1][1], ",".join(CSS_LIST_DATE_FIELDS))
        self.assertNotIn("EndDate=", client.css_calls[1][2])
        self.assertIn("filldata=1", client.csd_calls[0][4])
        self.assertEqual(client.stop_calls, 1)

        generic_none = MarketDataRequest(
            dataset_type="daily_bar",
            instrument_id="000333.SZ",
            start_date=date(2018, 1, 2),
            end_date=date(2018, 1, 2),
            adjustment="none",
            retrieval_mode="historical_backfill",
            requested_at=NOW,
        )
        with self.assertRaises(UnsupportedDatasetError):
            provider.fetch(generic_none)
        self.assertEqual(len(client.start_calls), 1)

    def test_fixed_batch_preserves_existing_network_error_classification(self):
        client = FixedClient()

        def failed_csd(*_args):
            raise ConnectionError("network unavailable")

        client.csd = failed_csd
        provider = ChoiceProvider(
            sdk_loader=lambda: SimpleNamespace(c=client), clock=lambda: NOW
        )
        with provider.diagnostic_session():
            with self.assertRaises(NetworkBlockedError):
                provider.fetch_quality_growth_csd(
                    "000333.SZ",
                    date(2018, 1, 2),
                    date(2018, 1, 2),
                    adjustment="qfq",
                )

    def test_css_state_requires_response_date_and_fixed_small_unique_batch(self):
        provider = ChoiceProvider(
            sdk_loader=lambda: SimpleNamespace(c=FixedClient(empty_state_dates=True)),
            clock=lambda: NOW,
        )
        with provider.diagnostic_session():
            with self.assertRaises(ProviderQueryError):
                provider.fetch_quality_growth_css_state_batch(
                    ("000333.SZ",), date(2018, 1, 2)
                )
            with self.assertRaises(ProviderQueryError):
                provider.fetch_quality_growth_css_list_date_batch(
                    ("000333.SZ", "000333.SZ")
                )
            with self.assertRaises(ProviderQueryError):
                provider.fetch_quality_growth_css_list_date_batch(
                    tuple(f"{index:06d}.SZ" for index in range(51))
                )

    def test_normalizer_treats_highlimit_lowlimit_as_flags_not_prices(self):
        task = batch._task(
            "csd",
            instrument_id="000333.SZ",
            start_date="2018-01-02",
            end_date="2018-01-02",
            price_basis="none",
        )
        payload = ProviderPayload(
            raw_content=b"raw",
            records=(
                {
                    "instrument_id": "000333.SZ",
                    "trading_date": "2018-01-02",
                    "adjustment": "none",
                    "open": "10",
                    "high": "11",
                    "low": "9",
                    "close": "10.5",
                    "preclose": "10",
                    "volume": "1000",
                    "amount": "10500",
                    "tradestatus": "交易",
                    "isststock": "0",
                    "highlimit": "1",
                    "lowlimit": "0",
                },
            ),
            fetched_at=NOW,
            upstream_source="choice.test",
        )
        normalized = batch._normalize_records(task, payload)[0]
        self.assertIs(normalized["high_limit_hit"], True)
        self.assertIs(normalized["low_limit_hit"], False)
        self.assertNotIn("high_limit_price", normalized)
        self.assertEqual(normalized["price_basis"], "none")

    def test_suspended_status_is_preserved_as_non_executable_mark(self):
        task = batch._task(
            "csd",
            instrument_id="000333.SZ",
            start_date="2018-01-02",
            end_date="2018-01-02",
            price_basis="none",
        )
        row = {
            "instrument_id": "000333.SZ",
            "trading_date": "2018-01-02",
            "adjustment": "none",
            "open": "10",
            "high": "10",
            "low": "10",
            "close": "10",
            "preclose": "10",
            "volume": "0",
            "amount": "0",
            "tradestatus": "全天停牌",
            "isststock": "0",
            "highlimit": "0",
            "lowlimit": "0",
        }
        normalized = batch._normalize_records(
            task,
            ProviderPayload(
                raw_content=b"diagnostic",
                records=(row,),
                fetched_at=NOW,
                upstream_source="choice.test",
            ),
        )[0]
        self.assertEqual(normalized["trading_status"], "suspended")
        self.assertNotEqual(normalized["trading_status"], "trading")

    def test_membership_801_duplicate_and_extreme_decimal_fail_closed(self):
        task = batch._task("membership", membership_date="2018-01-02")

        def payload(ids):
            return ProviderPayload(
                raw_content=b"diagnostic",
                records=tuple(
                    {
                        "sector_code": SECTOR_CODE,
                        "membership_date": "2018-01-02",
                        "instrument_id": item,
                        "security_short_name": f"样本{index}",
                    }
                    for index, item in enumerate(ids)
                ),
                fetched_at=NOW,
                upstream_source="choice.test",
            )

        ids = [f"{600000 + index:06d}.SH" for index in range(801)]
        with self.assertRaises(batch.ChoiceQualityGrowthBatchError):
            batch._normalize_records(task, payload(ids))
        duplicate = [f"{600000 + index:06d}.SH" for index in range(799)]
        duplicate.append(duplicate[-1])
        with self.assertRaises(batch.ChoiceQualityGrowthBatchError):
            batch._normalize_records(task, payload(duplicate))
        with self.assertRaises(batch.ChoiceQualityGrowthBatchError):
            batch._decimal_text("1e100000", "close", positive=True)

    def test_qfq_none_ratio_and_controlled_session_coverage_are_exact(self):
        def row(day, basis, *, high="11", preclose=None):
            return {
                "instrument_id": "000333.SZ",
                "trading_date": day,
                "price_basis": basis,
                "open": "10",
                "high": high,
                "low": "9",
                "close": "10.5",
                "preclose": preclose or (
                    "10.5" if day == "2018-01-03" else "10"
                ),
                "volume": "1000",
                "amount": "10500",
                "trading_status": "trading",
                "is_st": False,
                "high_limit_hit": False,
                "low_limit_hit": False,
            }

        none_rows = (row("2018-01-02", "none"), row("2018-01-03", "none"))
        qfq_bad = (
            row("2018-01-02", "qfq", high="12"),
            row("2018-01-03", "qfq"),
        )
        batch._validate_price_basis_pair(
            "000333.SZ",
            (row("2018-01-02", "qfq"), row("2018-01-03", "qfq")),
            none_rows,
            ("2018-01-02", "2018-01-03"),
        )
        with self.assertRaises(batch.ChoiceQualityGrowthBatchError):
            batch._validate_price_basis_pair(
                "000333.SZ", qfq_bad, none_rows, ("2018-01-02", "2018-01-03")
            )
        with self.assertRaises(batch.ChoiceQualityGrowthBatchError):
            batch._validate_price_basis_pair(
                "000333.SZ",
                (
                    row("2018-01-02", "qfq"),
                    row("2018-01-03", "qfq", preclose="9"),
                ),
                none_rows,
                ("2018-01-02", "2018-01-03"),
            )
        with self.assertRaises(batch.ChoiceQualityGrowthBatchError):
            batch._validate_price_basis_pair(
                "000333.SZ",
                (row("2018-01-02", "qfq"),),
                (row("2018-01-02", "none"),),
                ("2018-01-02", "2018-01-03"),
            )
        with self.assertRaises(batch.ChoiceQualityGrowthBatchError):
            batch._validate_price_basis_pair(
                "000333.SZ",
                (
                    row("2018-01-02", "qfq"),
                    row("2018-01-03", "qfq", preclose="9"),
                ),
                (
                    row("2018-01-02", "none"),
                    row("2018-01-03", "none", preclose="9"),
                ),
                ("2018-01-02", "2018-01-03"),
            )


class ChoiceQualityGrowthBatchTests(unittest.TestCase):
    def test_fixed_plan_and_internal_grid_are_not_caller_selectable(self):
        plan = fixed_choice_quality_growth_plan(date(2018, 3, 30))
        self.assertEqual(plan["sector_code"], SECTOR_CODE)
        self.assertEqual(tuple(plan["price_bases"]), PRICE_BASES)
        self.assertEqual(tuple(plan["csd_fields"]), CSD_FIELDS)
        self.assertEqual(tuple(plan["css_state_fields"]), CSS_STATE_FIELDS)
        self.assertEqual(
            tuple(plan["css_list_date_fields"]), CSS_LIST_DATE_FIELDS
        )
        self.assertEqual(plan["css_historical_date_parameter"], "EndDate")
        self.assertFalse(plan["source_authenticated"])
        self.assertEqual(plan["calendar_truth_status"], CALENDAR_BLOCKER)
        self.assertEqual(plan["industry_contract_status"], INDUSTRY_BLOCKER)
        sessions = []
        cursor = date(2018, 1, 1)
        while cursor <= date(2018, 3, 30):
            if cursor.weekday() < 5 and cursor != date(2018, 1, 1):
                sessions.append(cursor)
            cursor += timedelta(days=1)
        grid = derive_choice_quality_growth_decision_grid(
            sessions, date(2018, 3, 30)
        )
        self.assertEqual(grid[0], date(2018, 1, 2))
        indices = {day: index for index, day in enumerate(sessions)}
        self.assertTrue(
            all(indices[right] - indices[left] == 20 for left, right in zip(grid, grid[1:]))
        )
        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                [
                    "collect",
                    "--cutoff-date",
                    "2018-03-30",
                    "--as-of",
                    NOW.isoformat(),
                    "--output-root",
                    "out",
                    "--sector-code",
                    "caller-controlled",
                ]
            )

    def test_exact_799_is_incomplete_resume_skips_verified_calendar_and_tamper_fails(self):
        provider = IncompleteBatchProvider()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = collect_choice_quality_growth_batch(
                provider=provider,
                cutoff_date=date(2018, 2, 9),
                as_of=NOW,
                output_root=root,
                clock=lambda: NOW,
            )
            self.assertEqual(first.status, "incomplete")
            self.assertEqual(first.collection_status, "incomplete")
            self.assertIn("incomplete_fixed_batch_contract", first.blocking_reasons)
            manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
            self.assertFalse(manifest["formal_truth_eligible"])
            self.assertFalse(manifest["paper_eligible"])
            self.assertFalse(manifest["trade_eligible"])
            self.assertFalse(manifest["real_money_candidate"])
            self.assertEqual(manifest["live_execution_status"], "live_not_supported")
            self.assertEqual(manifest["admission_status"], INDUSTRY_BLOCKER)
            self.assertFalse(manifest["source_authenticated"])
            self.assertEqual(
                manifest["integrity_semantics"],
                "content_integrity_not_source_authentication",
            )
            self.assertIn(CALENDAR_BLOCKER, manifest["blocking_reasons"])
            before_verify = {
                item.relative_to(root).as_posix(): (item.stat().st_size, item.stat().st_mtime_ns)
                for item in root.rglob("*")
                if item.is_file()
            }
            verified = verify_choice_quality_growth_batch(first.manifest_path)
            self.assertTrue(verified.integrity_verified)
            after_verify = {
                item.relative_to(root).as_posix(): (item.stat().st_size, item.stat().st_mtime_ns)
                for item in root.rglob("*")
                if item.is_file()
            }
            self.assertEqual(after_verify, before_verify)
            self.assertEqual(provider.calendar_calls, 1)
            self.assertEqual(provider.membership_calls, 1)

            second = collect_choice_quality_growth_batch(
                provider=provider,
                cutoff_date=date(2018, 2, 9),
                as_of=NOW,
                output_root=root,
                resume=True,
                clock=lambda: NOW,
            )
            self.assertEqual(second.status, "incomplete")
            self.assertEqual(provider.calendar_calls, 1)
            self.assertEqual(provider.membership_calls, 2)

            checkpoint = json.loads((root / "checkpoint.json").read_text(encoding="utf-8"))
            calendar_ref = next(iter(checkpoint["completed"].values()))
            artifact = json.loads((root / calendar_ref["path"]).read_text(encoding="utf-8"))
            raw_path = root / artifact["raw_path"]
            raw_path.write_bytes(raw_path.read_bytes() + b"tamper")
            rejected = verify_choice_quality_growth_batch(second.manifest_path)
            self.assertFalse(rejected.integrity_verified)
            self.assertEqual(rejected.status, "invalid")

    def test_eligibility_snapshot_is_next_session_not_signal_close(self):
        provider = ExecutionDateProbeProvider()
        with tempfile.TemporaryDirectory() as directory:
            result = collect_choice_quality_growth_batch(
                provider=provider,
                cutoff_date=date(2018, 2, 9),
                as_of=NOW,
                output_root=Path(directory),
                clock=lambda: NOW,
            )
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(provider.css_dates, [date(2018, 1, 3)])

    def test_orphan_artifact_is_recovered_without_per_item_checkpoint_rewrite(self):
        provider = IncompleteBatchProvider()
        cutoff = date(2018, 2, 9)
        plan = fixed_choice_quality_growth_plan(cutoff)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = batch._BatchStore(root, plan)
            checkpoint = store.load_checkpoint(resume=False)
            task = batch._task(
                "calendar",
                start_date=batch.PRICE_START_DATE.isoformat(),
                end_date=cutoff.isoformat(),
            )
            artifact = store.capture(
                checkpoint,
                task,
                lambda: provider.fetch_quality_growth_calendar(
                    batch.PRICE_START_DATE, cutoff
                ),
            )
            task_id = artifact["task_id"]
            self.assertFalse((root / "checkpoint.json").exists())
            recovered = batch._BatchStore(root, plan).load_checkpoint(resume=True)
            self.assertIn(task_id, recovered["completed"])

    def test_failure_event_code_and_message_are_redacted(self):
        class SensitiveFailure(RuntimeError):
            code = "account=6222000000000000"

        with tempfile.TemporaryDirectory() as directory:
            store = batch._BatchStore(
                Path(directory), fixed_choice_quality_growth_plan(date(2018, 2, 9))
            )
            checkpoint = store.load_checkpoint(resume=False)
            task = batch._task("session", provider_id="choice")
            store.record_failure(
                checkpoint,
                task,
                SensitiveFailure("token=super-secret account=6222000000000000"),
            )
            task_id = batch._task_id(task)
            event = json.loads(
                (Path(directory) / "failure_events" / f"{task_id}.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(event["failure"]["error_code"], "batch_capture_failed")
            self.assertNotIn("super-secret", event["failure"]["error_message"])

    def test_blocked_cli_results_are_nonzero_and_distinct_from_invalid(self):
        complete = SimpleNamespace(
            manifest_path=Path("manifest.json"),
            manifest_sha256="0" * 64,
            status="blocked",
            collection_status="complete",
            blocking_reasons=(CALENDAR_BLOCKER,),
        )
        with patch(
            "agent.choice_quality_growth_batch.collect_choice_quality_growth_batch",
            return_value=complete,
        ):
            code = main(
                [
                    "collect",
                    "--cutoff-date",
                    "2018-03-30",
                    "--as-of",
                    NOW.isoformat(),
                    "--output-root",
                    "out",
                ]
            )
        self.assertEqual(code, 3)

    def test_manifest_symlink_is_rejected_before_following_reference(self):
        provider = IncompleteBatchProvider()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = collect_choice_quality_growth_batch(
                provider=provider,
                cutoff_date=date(2018, 2, 9),
                as_of=NOW,
                output_root=root,
                clock=lambda: NOW,
            )
            linked = result.manifest_path.with_name("linked.json")
            try:
                linked.symlink_to(result.manifest_path)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is unavailable")
            verification = verify_choice_quality_growth_batch(linked)
            self.assertFalse(verification.integrity_verified)

    def test_schema_freezes_all_permission_denials(self):
        schema_path = Path("schemas/choice_quality_growth_batch.v1.json")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        properties = schema["properties"]
        self.assertEqual(properties["formal_truth_eligible"]["const"], False)
        self.assertEqual(properties["paper_eligible"]["const"], False)
        self.assertEqual(properties["trade_eligible"]["const"], False)
        self.assertEqual(properties["real_money_candidate"]["const"], False)
        self.assertEqual(
            properties["live_execution_status"]["const"], "live_not_supported"
        )
        self.assertEqual(properties["source_authenticated"]["const"], False)
        blocker_values = properties["blocking_reasons"]["items"]["enum"]
        self.assertIn(CALENDAR_BLOCKER, blocker_values)


if __name__ == "__main__":
    unittest.main()
