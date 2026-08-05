import ast
import hashlib
import importlib.util
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from trading.brokers.htsc_mquant_shadow import (
    HtscMQuantShadowAdapter,
    SnapshotValidationError,
)
from trading.brokers.models import BrokerOrder, BrokerTrade
from trading.brokers.reconcile import ShadowReconciler, StrategyOwnershipLedger
from trading.huatai_shadow_probe import load_shadow_probe_config, probe
from trading.models import Position


ROOT = Path(__file__).resolve().parents[1]
TZ = timezone(timedelta(hours=8))
COMPLETED = datetime(2026, 7, 14, 10, 0, tzinfo=TZ)
NOW = COMPLETED + timedelta(seconds=5)
BINDING = "htsc-local-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
FINGERPRINT = "hmac-sha256:" + ("b" * 64)
SHAPE_ID = "local-shape-sha256:" + ("e" * 64)


def canonical_hash(payload):
    body = dict(payload)
    body.pop("payload_sha256", None)
    encoded = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def base_payload():
    payload = {
        "schema_version": "htsc-mquant-shadow/1",
        "capabilities": {"read_only": True, "orders_enabled": False},
        "source": {
            "broker": "HTSC",
            "adapter": "mquant",
            "environment": "real_account_read_only",
            "api_shape_id": SHAPE_ID,
            "account_binding_id": BINDING,
            "account_fingerprint": FINGERPRINT,
            "session_id": "session-20260714-001",
        },
        "capture": {
            "sequence": 1,
            "started_at": "2026-07-14T10:00:00+08:00",
            "completed_at": "2026-07-14T10:00:00+08:00",
            "complete": True,
            "consistency": "sequential_non_atomic",
            "sections": {
                "funds": True,
                "positions": True,
                "open_orders": True,
                "today_orders": True,
                "trades": True,
            },
            "pagination": {
                "open_orders": {
                    "page_count": 1,
                    "reported_total_count": 0,
                    "returned_count": 0,
                    "is_last": True,
                },
                "today_orders": {
                    "page_count": 1,
                    "reported_total_count": 0,
                    "returned_count": 0,
                    "is_last": True,
                },
                "trades": {
                    "page_count": 1,
                    "reported_total_count": 0,
                    "returned_count": 0,
                    "is_last": True,
                },
            },
            "errors": [],
            "warnings": ["public API contract requires current-client verification"],
        },
        "funds": {
            "available_cash": "5000.00",
            "frozen_cash": "0.00",
            "hold_cash": "5000.00",
            "total_value": "21000.00",
            "market_value": "16000.00",
            "transferable_cash": "5000.00",
        },
        "positions": [
            {
                "symbol": "000333.SZ",
                "total_quantity": 200,
                "sellable_quantity": 200,
                "today_quantity": 0,
                "frozen_quantity": 0,
                "price": "80.00",
                "market_value": "16000.00",
                "hold_cost": "70.00",
            }
        ],
        "open_orders": [],
        "today_orders": [],
        "trades": [],
    }
    payload["payload_sha256"] = canonical_hash(payload)
    return payload


def write_payload(path: Path, payload: dict) -> None:
    payload["payload_sha256"] = canonical_hash(payload)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class HtscMQuantShadowAdapterTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "snapshot.json"

    def tearDown(self):
        self.directory.cleanup()

    def adapter(
        self,
        expected=BINDING,
        expected_fingerprint=FINGERPRINT,
        expected_shape=SHAPE_ID,
        max_age_seconds=15,
    ):
        return HtscMQuantShadowAdapter(
            self.path,
            expected_account_binding_id=expected,
            expected_account_fingerprint=expected_fingerprint,
            expected_api_shape_id=expected_shape,
            max_age_seconds=max_age_seconds,
        )

    def test_valid_read_only_snapshot_is_loaded_as_broker_facts(self):
        write_payload(self.path, base_payload())

        snapshot = self.adapter().read_snapshot(NOW)

        self.assertTrue(snapshot.account_binding_matched)
        self.assertTrue(snapshot.shape_checked)
        self.assertFalse(snapshot.source_authenticated)
        self.assertEqual(snapshot.capture_consistency, "sequential_non_atomic")
        self.assertEqual(snapshot.funds.available_cash, Decimal("5000.00"))
        self.assertEqual(snapshot.positions["000333.SZ"].quantity, 200)
        self.assertEqual(snapshot.open_orders, ())

    def test_first_enrollment_read_is_explicitly_unverified(self):
        write_payload(self.path, base_payload())

        snapshot = self.adapter(
            expected=None,
            expected_fingerprint=None,
            expected_shape=None,
        ).read_snapshot(NOW)

        self.assertFalse(snapshot.account_binding_matched)

    def test_hash_tampering_is_rejected(self):
        payload = base_payload()
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        payload["funds"]["available_cash"] = "999999.00"
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        with self.assertRaisesRegex(SnapshotValidationError, "hash mismatch"):
            self.adapter().read_snapshot(NOW)

    def test_excessively_wide_sequential_capture_window_is_rejected(self):
        payload = base_payload()
        payload["capture"]["started_at"] = "2026-07-14T09:59:40+08:00"
        write_payload(self.path, payload)

        with self.assertRaisesRegex(SnapshotValidationError, "window is too long"):
            self.adapter().read_snapshot(NOW)

    def test_incomplete_stale_future_and_wrong_identity_fail_closed(self):
        cases = []
        incomplete = base_payload()
        incomplete["capture"]["sections"]["positions"] = False
        cases.append((incomplete, NOW, BINDING, FINGERPRINT, "section is incomplete"))
        errored = base_payload()
        errored["capture"]["errors"] = ["get_positions_ex returned None"]
        cases.append((errored, NOW, BINDING, FINGERPRINT, "contains errors"))
        orders_enabled = base_payload()
        orders_enabled["capabilities"]["orders_enabled"] = True
        cases.append((orders_enabled, NOW, BINDING, FINGERPRINT, "enables orders"))
        cases.append(
            (
                base_payload(),
                COMPLETED + timedelta(seconds=16),
                BINDING,
                FINGERPRINT,
                "stale",
            )
        )
        cases.append(
            (
                base_payload(),
                COMPLETED - timedelta(seconds=6),
                BINDING,
                FINGERPRINT,
                "future",
            )
        )
        cases.append(
            (
                base_payload(),
                NOW,
                "htsc-local-cccccccccccccccccccccccccccccccc",
                FINGERPRINT,
                "binding mismatch",
            )
        )
        cases.append(
            (
                base_payload(),
                NOW,
                BINDING,
                "hmac-sha256:" + ("d" * 64),
                "fingerprint mismatch",
            )
        )

        for payload, now, expected, expected_fingerprint, message in cases:
            with self.subTest(message=message):
                write_payload(self.path, payload)
                with self.assertRaisesRegex(SnapshotValidationError, message):
                    self.adapter(
                        expected=expected,
                        expected_fingerprint=expected_fingerprint,
                    ).read_snapshot(now)

    def test_binary_float_amount_and_duplicate_position_are_rejected(self):
        numeric = base_payload()
        numeric["funds"]["available_cash"] = 5000.0
        write_payload(self.path, numeric)
        with self.assertRaisesRegex(SnapshotValidationError, "decimal string"):
            self.adapter().read_snapshot(NOW)

        duplicate = base_payload()
        duplicate["positions"].append(dict(duplicate["positions"][0]))
        write_payload(self.path, duplicate)
        with self.assertRaisesRegex(SnapshotValidationError, "duplicate broker position"):
            self.adapter().read_snapshot(NOW)

    def test_unknown_fields_and_pagination_count_mismatch_are_rejected(self):
        extra = base_payload()
        extra["unexpected_live_flag"] = True
        write_payload(self.path, extra)
        with self.assertRaisesRegex(SnapshotValidationError, "unknown fields"):
            self.adapter().read_snapshot(NOW)

        mismatch = base_payload()
        mismatch["capture"]["pagination"]["trades"]["returned_count"] = 1
        write_payload(self.path, mismatch)
        with self.assertRaisesRegex(SnapshotValidationError, "pagination count mismatch"):
            self.adapter().read_snapshot(NOW)

        reported_mismatch = base_payload()
        reported_mismatch["capture"]["pagination"]["trades"][
            "reported_total_count"
        ] = 1
        write_payload(self.path, reported_mismatch)
        with self.assertRaisesRegex(
            SnapshotValidationError, "pagination reported count mismatch"
        ):
            self.adapter().read_snapshot(NOW)

    def test_submission_and_cancellation_capabilities_do_not_exist(self):
        adapter = self.adapter()
        self.assertFalse(hasattr(adapter, "submit_order"))
        self.assertFalse(hasattr(adapter, "cancel_order"))


class HtscShadowReconciliationTest(unittest.TestCase):
    def raw_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            payload = base_payload()
            payload["positions"].extend(
                [
                    {
                        "symbol": "ETF_A",
                        "total_quantity": 200,
                        "sellable_quantity": 200,
                        "today_quantity": 0,
                        "frozen_quantity": 0,
                        "price": "2.00",
                        "market_value": "400.00",
                        "hold_cost": "1.90",
                    },
                    {
                        "symbol": "ETF_OTHER",
                        "total_quantity": 500,
                        "sellable_quantity": 500,
                        "today_quantity": 0,
                        "frozen_quantity": 0,
                        "price": "1.00",
                        "market_value": "500.00",
                        "hold_cost": "1.00",
                    },
                ]
            )
            write_payload(path, payload)
            return HtscMQuantShadowAdapter(
                path,
                expected_account_binding_id=BINDING,
                expected_account_fingerprint=FINGERPRINT,
                expected_api_shape_id=SHAPE_ID,
            ).read_snapshot(NOW)

    def ledger(self):
        return StrategyOwnershipLedger(
            strategy_id="small-account-etf-v0",
            account_binding_id=BINDING,
            strategy_cash=Decimal("1000"),
            baseline_complete=True,
            managed_instrument_ids=("ETF_A",),
            baseline_quantities={"ETF_A": 100},
            strategy_positions={"ETF_A": Position("ETF_A", 100, 100)},
        )

    def test_long_term_and_unowned_assets_cannot_be_adopted_without_audited_store(self):
        result = ShadowReconciler().reconcile(self.raw_snapshot(), self.ledger())

        self.assertFalse(result.allowed)
        self.assertIsNone(result.account)
        self.assertIn("audited_ownership_store_not_implemented", result.block_codes)

    def test_unverified_identity_open_orders_cash_shortfall_and_manual_trade_block(self):
        raw = self.raw_snapshot()
        open_order = BrokerOrder(
            broker_order_id="order-1",
            entrust_no="entrust-1",
            instrument_id="000333.SZ",
            side="BUY",
            status="0",
            quantity=100,
            filled_quantity=0,
            withdrawn_quantity=0,
            entrust_price=Decimal("80"),
            average_price=Decimal("0"),
            created_at=NOW,
        )
        unknown_trade = BrokerTrade(
            broker_trade_id="trade-unknown",
            broker_order_id="order-x",
            entrust_no="entrust-x",
            instrument_id="ETF_A",
            side="BUY",
            quantity=100,
            price=Decimal("2"),
            business_balance=Decimal("200"),
            real_type="0",
            traded_at=NOW,
        )
        low_funds = replace(raw.funds, available_cash=Decimal("999"))
        unsafe = replace(
            raw,
            account_binding_matched=False,
            shape_checked=False,
            open_orders=(open_order,),
            trades=(unknown_trade,),
            funds=low_funds,
        )

        result = ShadowReconciler().reconcile(unsafe, self.ledger())

        self.assertFalse(result.allowed)
        self.assertIsNone(result.account)
        self.assertIn("broker_account_binding_unmatched", result.block_codes)
        self.assertIn("broker_api_shape_unchecked", result.block_codes)
        self.assertIn("broker_snapshot_source_unauthenticated", result.block_codes)
        self.assertIn("broker_capture_not_atomic", result.block_codes)
        self.assertIn("broker_open_orders_present", result.block_codes)
        self.assertIn("broker_cash_below_strategy_ledger", result.block_codes)
        self.assertIn("ownership_ambiguous:unknown_managed_trade", result.block_codes)

    def test_whitelist_never_overrides_the_ownership_baseline(self):
        ledger = replace(self.ledger(), baseline_quantities={"ETF_A": 0})

        result = ShadowReconciler().reconcile(self.raw_snapshot(), ledger)

        self.assertFalse(result.allowed)
        self.assertIn("ownership_ambiguous:ETF_A", result.block_codes)


class HtscExporterSourceBoundaryTest(unittest.TestCase):
    @staticmethod
    def exporter_module():
        path = ROOT / "integrations" / "htsc_mquant" / "htsc_shadow_exporter.py"
        spec = importlib.util.spec_from_file_location("htsc_shadow_exporter_test", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def verifier_module():
        path = (
            ROOT
            / "integrations"
            / "htsc_mquant"
            / "inspect_local_sdk_shape.py"
        )
        spec = importlib.util.spec_from_file_location("htsc_shape_inspector_test", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_exporter_contains_no_order_or_cancel_call(self):
        path = ROOT / "integrations" / "htsc_mquant" / "htsc_shadow_exporter.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        forbidden = {"order", "order_normal", "orders", "cancel_order", "cancel_orders"}
        called = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                called.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                called.add(node.func.attr)
        self.assertTrue(forbidden.isdisjoint(called), forbidden & called)

    def test_exporter_emits_integer_quantities_and_decimal_strings(self):
        exporter = self.exporter_module()
        position = SimpleNamespace(
            security="510300.SH",
            total_amount=200.0,
            closeable_amount=100,
            today_amount=100,
            locked_amount=0,
            price=3.987,
            value=797.4,
            hold_cost=3.9,
        )

        record = exporter._position_record(position)

        self.assertEqual(record["total_quantity"], 200)
        self.assertIs(type(record["total_quantity"]), int)
        self.assertEqual(record["price"], "3.987")
        self.assertEqual(exporter._side_text("long"), "BUY")
        self.assertEqual(exporter._side_text("short"), "SELL")
        with self.assertRaises(exporter._SectionError):
            exporter._integer_value("1.5")
        with self.assertRaises(exporter._SectionError):
            exporter._side_text("unexpected")

    def test_exporter_continues_after_empty_nonterminal_page(self):
        exporter = self.exporter_module()
        calls = []

        def page_query(page_no, _page_size):
            calls.append(page_no)
            if page_no == 1:
                return (1, False, {})
            return (1, True, {"trade-1": {"id": "trade-1"}})

        records, pagination = exporter._read_paginated(
            "trades", page_query, lambda value: value, "id"
        )

        self.assertEqual(calls, [1, 2])
        self.assertEqual(records, [{"id": "trade-1"}])
        self.assertEqual(pagination["reported_total_count"], 1)
        self.assertEqual(pagination["returned_count"], 1)

    def test_exporter_output_round_trips_through_strict_adapter(self):
        exporter = self.exporter_module()
        binding = "htsc-local-0123456789abcdef0123456789abcdef"
        exporter._STATE.update(
            {
                "configured": True,
                "snapshot_path": "unused.json",
                "account_binding_id": binding,
                "account_binding_secret": "test-only-secret-with-at-least-32-characters",
                "account_type": "stock",
                "page_size": 500,
                "session_id": "0123456789abcdef0123456789abcdef",
                "sequence": 0,
            }
        )
        exporter.get_fund_info = lambda **_kwargs: SimpleNamespace(
            available_cash=244.84,
            frozen_cash=0,
            hold_cash=244.84,
            total_value=16324.84,
            market_value=16080,
            transferable_cash=244.84,
            fund_account="must-not-be-exported",
        )
        exporter.get_positions_ex = lambda **_kwargs: [
            SimpleNamespace(
                security="000333.SZ",
                total_amount=200,
                closeable_amount=200,
                today_amount=0,
                locked_amount=0,
                price=80.4,
                value=16080,
                hold_cost=78,
                stock_account="must-not-be-exported",
            )
        ]
        exporter.get_open_orders_ex = lambda **_kwargs: (0, True, {})
        exporter.get_orders_ex = lambda **_kwargs: (0, True, {})
        exporter.get_trades_ex = lambda **_kwargs: (0, True, {})

        payload = exporter._with_payload_hash(exporter._snapshot_payload())

        self.assertTrue(payload["capture"]["complete"])
        self.assertNotIn("must-not-be-exported", json.dumps(payload))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            snapshot = HtscMQuantShadowAdapter(
                path,
                expected_account_binding_id=binding,
                expected_account_fingerprint=payload["source"]["account_fingerprint"],
            ).read_snapshot(datetime.now(TZ))
        self.assertTrue(snapshot.account_binding_matched)
        self.assertFalse(snapshot.shape_checked)
        self.assertFalse(snapshot.source_authenticated)
        self.assertEqual(snapshot.positions["000333.SZ"].quantity, 200)
        self.assertEqual(snapshot.funds.available_cash, Decimal("244.84"))

    def test_local_sdk_shape_check_is_explicitly_not_source_authentication(self):
        verifier = self.verifier_module()
        with tempfile.TemporaryDirectory() as directory:
            api_path = Path(directory) / "MQuant_api.py"
            struct_path = Path(directory) / "MQuant_struct.py"
            api_path.write_text(
                "\n".join(
                    [
                        "def get_fund_info(account_type=None): pass",
                        "def get_positions_ex(account_type=None, symbol=''): pass",
                        "def get_open_orders_ex(page_no=1, page_size=500, only_this_inst=False, account_type=None): pass",
                        "def get_orders_ex(page_no=1, page_size=500, only_this_inst=False, account_type=None): pass",
                        "def get_trades_ex(page_no=1, page_size=500, only_this_inst=False, account_type=None, include_rejected_orders=True, include_withdraw_orders=True): pass",
                        "def run_timely(func, interval): pass",
                    ]
                ),
                encoding="utf-8",
            )
            class_lines = []
            for class_name, fields in verifier.REQUIRED_CLASS_FIELDS.items():
                class_lines.append("class {}:".format(class_name))
                class_lines.append("    def __init__(self):")
                for field in sorted(fields):
                    class_lines.append("        self.{} = None".format(field))
            struct_path.write_text("\n".join(class_lines), encoding="utf-8")

            checked = verifier.inspect_local_sdk_shape(api_path, struct_path)
            api_path.write_text("def get_fund_info(account_type=None): pass", encoding="utf-8")
            incomplete = verifier.inspect_local_sdk_shape(api_path, struct_path)

        self.assertTrue(checked["shape_checked"])
        self.assertRegex(checked["local_shape_id"], r"^local-shape-sha256:[0-9a-f]{64}$")
        self.assertFalse(checked["source_authenticated"])
        self.assertFalse(checked["runtime_loaded_proven"])
        self.assertFalse(incomplete["shape_checked"])
        self.assertIn("function:get_positions_ex", incomplete["missing"])


class HtscShadowProbeTest(unittest.TestCase):
    def test_config_cannot_enable_orders_with_string_or_boolean(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            base = {
                "config_version": "htsc-mquant-shadow-config/1",
                "status": "blocked_pending_client_authorization",
                "required_snapshot_schema": "htsc-mquant-shadow/1",
                "read_only": True,
                "orders_enabled": False,
                "expected_account_binding_id": None,
                "expected_account_fingerprint": None,
                "expected_api_shape_id": None,
                "snapshot_path": str(Path(directory) / "snapshot.json"),
                "max_snapshot_age_seconds": 15,
                "require_complete_snapshot": True,
                "require_payload_sha256": True,
            }
            for unsafe in (True, "false"):
                with self.subTest(unsafe=unsafe):
                    base["orders_enabled"] = unsafe
                    path.write_text(json.dumps(base), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_shadow_probe_config(path)

    def test_probe_reports_enrollment_without_claiming_verified_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "snapshot.json"
            config_path = Path(directory) / "config.json"
            write_payload(snapshot_path, base_payload())
            config_path.write_text(
                json.dumps(
                    {
                        "config_version": "htsc-mquant-shadow-config/1",
                        "status": "blocked_pending_client_authorization",
                        "required_snapshot_schema": "htsc-mquant-shadow/1",
                        "read_only": True,
                        "orders_enabled": False,
                        "expected_account_binding_id": None,
                        "expected_account_fingerprint": None,
                        "expected_api_shape_id": None,
                        "snapshot_path": str(snapshot_path),
                        "max_snapshot_age_seconds": 15,
                        "require_complete_snapshot": True,
                        "require_payload_sha256": True,
                    }
                ),
                encoding="utf-8",
            )

            result = probe(load_shadow_probe_config(config_path), NOW)

            self.assertEqual(
                result["probe_status"], "blocked_pending_client_authorization"
            )
            self.assertFalse(result["account_binding_matched"])
            self.assertFalse(result["shape_checked"])
            self.assertFalse(result["source_authenticated"])
            self.assertEqual(result["capture_consistency"], "sequential_non_atomic")
            self.assertFalse(result["orders_enabled"])


if __name__ == "__main__":
    unittest.main()
