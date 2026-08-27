import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.tushare_capability_probe import (
    DEFAULT_CONFIG_PATH,
    GitMetadata,
    TOKEN_ENVIRONMENT_VARIABLE,
    TushareCapabilityProbeError,
    build_plan,
    compare_daily_samples,
    compute_probe_implementation_bundle_sha256,
    main,
    run_live_probe,
    verify_probe_run,
)
from research.market_data.providers.base import DependencyMissingError
from research.market_data.contracts import (
    MarketDataRequest,
    canonical_json_bytes as market_data_canonical_json_bytes,
)
from research.market_data.providers.baostock import (
    BaoStockProvider,
    replay_baostock_raw,
)
from research.market_data.tushare_capability import load_probe_config
from research.market_data.tushare_capability import (
    canonical_json_bytes,
    canonical_sha256,
    requested_fields_for,
    sha256_bytes,
)


NOW = datetime(2026, 8, 24, 9, 30, tzinfo=timezone.utc)
SECRET = "probe-secret-must-never-appear"


class FakeFrame:
    def __init__(self, records):
        self._records = [dict(item) for item in records]
        self.columns = list(self._records[0]) if self._records else []

    @property
    def empty(self):
        return not self._records

    def __len__(self):
        return len(self._records)

    def to_dict(self, orient="dict"):
        if orient != "records":
            raise AssertionError("probe must request record-oriented data")
        return [dict(item) for item in self._records]


def daily_record(*, close="10.50", extra=None):
    result = {
        "ts_code": "000333.SZ",
        "trade_date": "20260803",
        "open": "10.00",
        "high": "11.00",
        "low": "9.80",
        "close": close,
        "pre_close": "9.90",
        "change": "0.60",
        "pct_chg": "6.0606",
        "vol": "123.00",
        "amount": "456.00",
    }
    if extra:
        result.update(extra)
    return result


def baostock_record():
    return {
        "instrument_id": "000333.SZ",
        "trading_date": "2026-08-03",
        "open": "10.00",
        "high": "11.00",
        "low": "9.80",
        "close": "10.50",
        "preclose": "9.90",
        "volume": "12300",
        "amount": "456000",
    }


def baostock_capture_fixture(parameters, requested_at):
    start = datetime.strptime(parameters["start_date"], "%Y%m%d").date()
    end = datetime.strptime(parameters["end_date"], "%Y%m%d").date()
    request = MarketDataRequest(
        dataset_type="daily_bar",
        instrument_id=parameters["ts_code"],
        start_date=start,
        end_date=end,
        retrieval_mode="historical_backfill",
        adjustment="none",
        requested_at=requested_at,
    )
    calendar_rows = []
    current = start
    while current <= end:
        calendar_rows.append(
            [current.isoformat(), "1" if current == start else "0"]
        )
        current += timedelta(days=1)
    raw = market_data_canonical_json_bytes(
        {
            "operation": "query_history_k_data_plus_with_trade_calendar_completeness",
            "request": request.fingerprint_payload(
                BaoStockProvider.provider_id,
                BaoStockProvider.adapter_version,
            ),
            "daily": {
                "fields": list(BaoStockProvider._DAILY_FIELDS),
                "rows": [
                    [
                        start.isoformat(),
                        "sz.000333",
                        "10.00",
                        "11.00",
                        "9.80",
                        "10.50",
                        "9.90",
                        "12300",
                        "456000",
                        "3",
                        "1",
                    ]
                ],
            },
            "trade_calendar": {
                "fields": ["calendar_date", "is_trading_day"],
                "rows": calendar_rows,
            },
        }
    )
    return replay_baostock_raw(request, raw, requested_at), raw


class FakeClient:
    def __init__(self, router=None):
        self.router = router or self._default_router
        self.calls = []
        self.factor_value_called = False

    @staticmethod
    def _default_router(endpoint, parameters):
        if endpoint == "daily":
            return FakeFrame([daily_record()])
        return FakeFrame([])

    def __getattr__(self, name):
        if name == "factor_value":
            def forbidden(**kwargs):
                self.factor_value_called = True
                raise AssertionError("precomputed factor endpoint must never run")

            return forbidden

        def call(**parameters):
            self.calls.append((name, dict(parameters)))
            return self.router(name, parameters)

        return call


class FakeModule:
    __version__ = "fixture-sdk-v1"

    def __init__(self, client):
        self.client = client
        self.received_token = None

    def pro_api(self, token):
        self.received_token = token
        return self.client


class TushareCapabilityProbeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_probe_config(DEFAULT_CONFIG_PATH)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = Path(self.temporary.name) / "repo"
        self.repository.mkdir()
        self.output = self.repository / "data" / "tmp" / "tushare-capability"

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def git_metadata():
        return GitMetadata(commit="b" * 40, worktree_status="dirty")

    def run_fixture(self, client=None, **kwargs):
        fake_client = client or FakeClient()
        module = FakeModule(fake_client)
        with patch.dict(os.environ, {TOKEN_ENVIRONMENT_VARIABLE: SECRET}, clear=False):
            result = run_live_probe(
                self.config,
                kwargs.pop("output_root", self.output),
                sdk_loader=kwargs.pop("sdk_loader", lambda: module),
                baostock_capture=kwargs.pop(
                    "baostock_capture",
                    baostock_capture_fixture,
                ),
                clock=kwargs.pop("clock", lambda: NOW),
                sleeper=kwargs.pop("sleeper", lambda seconds: None),
                run_id=kwargs.pop("run_id", "fixture-run"),
                repository_root=kwargs.pop("repository_root", self.repository),
                git_metadata_loader=kwargs.pop(
                    "git_metadata_loader", self.git_metadata
                ),
                **kwargs,
            )
        return result, fake_client, module

    def run_directory(self, run_id="fixture-run"):
        return self.output / run_id

    def assert_secret_absent_from_tree(self, root):
        encoded = SECRET.encode("utf-8")
        for path in root.rglob("*"):
            if path.is_file():
                self.assertNotIn(encoded, path.read_bytes(), path.as_posix())

    @staticmethod
    def resign_manifest_and_receipt(directory):
        manifest_path = directory / "manifest.json"
        receipt_path = directory / "receipt.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_bytes = canonical_json_bytes(manifest)
        manifest_path.write_bytes(manifest_bytes)
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["raw_evidence_manifest_sha256"] = sha256_bytes(manifest_bytes)
        receipt_content = dict(receipt)
        receipt_content.pop("receipt_sha256", None)
        receipt["receipt_sha256"] = canonical_sha256(receipt_content)
        receipt_path.write_bytes(canonical_json_bytes(receipt))

    def copy_implementation_bundle(self):
        from agent import tushare_capability_probe as module

        destination_root = self.repository / "implementation-bundle"
        for relative in module._IMPLEMENTATION_BUNDLE_PATHS:
            source = module.REPOSITORY_ROOT / relative
            destination = destination_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        return destination_root

    def create_junction(self, link, target):
        if os.name != "nt":
            self.skipTest("NTFS junction test requires Windows")
        target.mkdir(parents=True, exist_ok=True)
        link.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0 or not getattr(
            link,
            "is_junction",
            lambda: False,
        )():
            self.skipTest("NTFS directory junctions are unavailable")

    @staticmethod
    def remove_junction(link):
        if link.exists() or getattr(link, "is_junction", lambda: False)():
            os.rmdir(link)

    def test_plan_is_offline_and_does_not_read_token_or_import_sdk(self):
        stdout = io.StringIO()
        with patch(
            "agent.tushare_capability_probe._read_tushare_token",
            side_effect=AssertionError("plan must not read environment credentials"),
        ), patch(
            "agent.tushare_capability_probe._default_sdk_loader",
            side_effect=AssertionError("plan must not import the SDK"),
        ), redirect_stdout(stdout):
            self.assertEqual(main(["--plan", "--config", str(DEFAULT_CONFIG_PATH)]), 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["mode"], "plan_offline_no_credentials")
        self.assertFalse(result["credential_accessed"])
        self.assertFalse(result["sdk_imported"])
        self.assertFalse(result["network_accessed"])
        self.assertLessEqual(
            result["planned_maximum_request_count"],
            result["global_maximum_request_count"],
        )

    def test_plan_v2_binds_deterministic_requested_fields_and_hashes(self):
        from agent import tushare_capability_probe as module

        plan = build_plan(self.config)
        self.assertEqual(plan["schema_version"], "tushare-capability-plan-v2")
        self.assertEqual(plan["probe_version"], "tushare-capability-probe-v2")
        self.assertEqual(len(plan["endpoints"]), len(self.config.endpoints))
        for planned, spec in zip(
            plan["endpoints"],
            self.config.endpoints,
            strict=True,
        ):
            expected_fields = requested_fields_for(spec)
            expected_sha256 = sha256_bytes(
                canonical_json_bytes(
                    {
                        "schema_version": module.REQUESTED_FIELDS_EVIDENCE_VERSION,
                        "endpoint": spec.endpoint.value,
                        "requested_fields": list(expected_fields),
                    }
                )
            )
            self.assertEqual(planned["requested_fields"], list(expected_fields))
            self.assertEqual(
                planned["requested_fields_sha256"],
                expected_sha256,
            )
            self.assertNotIn(SECRET, ",".join(planned["requested_fields"]))

    def test_no_mode_defaults_to_offline_plan(self):
        stdout = io.StringIO()
        with patch(
            "agent.tushare_capability_probe._default_sdk_loader",
            side_effect=AssertionError("default mode must not import the SDK"),
        ), redirect_stdout(stdout):
            self.assertEqual(main(["--config", str(DEFAULT_CONFIG_PATH)]), 0)
        self.assertEqual(json.loads(stdout.getvalue())["mode"], "plan_offline_no_credentials")

    def test_versioned_access_policy_must_remain_probe_only(self):
        invalid = SimpleNamespace(
            schema_version="provider-access-policy-v1",
            tushare=SimpleNamespace(
                access_status="capability_probe_only",
                capability_probe_allowed=True,
                formal_provider_allowed=True,
                automatic_fallback_allowed=False,
                partial_fallback_allowed=False,
            ),
        )
        with self.assertRaisesRegex(TushareCapabilityProbeError, "does not authorize"):
            build_plan(self.config, access_policy=invalid)

    def test_fixed_allowlist_never_calls_precomputed_factor_endpoint(self):
        result, client, _ = self.run_fixture()
        called = {name for name, _ in client.calls}
        self.assertNotIn("factor_value", called)
        self.assertFalse(client.factor_value_called)
        self.assertEqual(set(called), {item.sdk_method for item in self.config.endpoints})
        self.assertFalse(result["formal_data_admission"])
        self.assertFalse(result["trade_eligibility"])

    def test_every_sdk_call_receives_exact_fields_outside_business_parameters(self):
        result, client, _ = self.run_fixture(run_id="explicit-requested-fields")
        self.assertEqual(len(client.calls), len(self.config.planned_calls()))
        for index, ((method_name, kwargs), (spec, parameters)) in enumerate(
            zip(
                client.calls,
                self.config.planned_calls(),
                strict=True,
            )
        ):
            self.assertEqual(method_name, spec.sdk_method)
            sent = dict(kwargs)
            self.assertEqual(
                sent.pop("fields"),
                ",".join(requested_fields_for(spec)),
            )
            self.assertEqual(sent, dict(parameters))
            self.assertNotIn(
                "fields",
                result["endpoint_results"][index]["sanitized_parameters"],
            )

        calls_by_endpoint = {
            endpoint: kwargs
            for endpoint, kwargs in client.calls
        }
        self.assertIn("exchange", calls_by_endpoint["stock_basic"]["fields"])
        self.assertIn("list_status", calls_by_endpoint["stock_basic"]["fields"])
        self.assertIn("delist_date", calls_by_endpoint["stock_basic"]["fields"])
        self.assertIn("pre_close", calls_by_endpoint["stk_limit"]["fields"])
        self.assertIn("actual_date", calls_by_endpoint["disclosure_date"]["fields"])
        for endpoint in (
            "income",
            "income_vip",
            "balancesheet",
            "balancesheet_vip",
            "cashflow",
            "cashflow_vip",
        ):
            self.assertEqual(
                calls_by_endpoint[endpoint]["fields"],
                "ts_code,ann_date,f_ann_date,end_date,report_type,comp_type",
            )

    def test_requested_fields_and_missing_normalization_are_receipt_bound(self):
        def router(endpoint, parameters):
            if endpoint == "daily":
                return FakeFrame(
                    [daily_record(extra={"nullable_metric": float("nan")})]
                )
            return FakeFrame([])

        result, _, _ = self.run_fixture(
            FakeClient(router),
            run_id="requested-fields-evidence",
        )
        for item, (spec, _) in zip(
            result["endpoint_results"],
            self.config.planned_calls(),
            strict=True,
        ):
            expected_fields = ",".join(requested_fields_for(spec))
            self.assertEqual(item["notes"][0], f"requested_fields={expected_fields}")
            self.assertTrue(item["notes"][1].startswith("requested_fields_sha256="))
            self.assertNotIn(SECRET, json.dumps(item["notes"]))
        daily = next(
            item for item in result["endpoint_results"] if item["endpoint"] == "daily"
        )
        self.assertIn("normalization=missing_scalar_normalized", daily["notes"])
        raw_path = (
            self.run_directory("requested-fields-evidence")
            / "raw"
            / "daily.01.json"
        )
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        self.assertIsNone(raw["rows"][0]["nullable_metric"])
        verify_probe_run(
            self.run_directory("requested-fields-evidence"),
            config=self.config,
            secret=SECRET,
        )

    def test_resigned_requested_fields_note_tamper_fails_verification(self):
        self.run_fixture(run_id="requested-fields-note-tamper")
        directory = self.run_directory("requested-fields-note-tamper")
        receipt_path = directory / "receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["endpoint_results"][0]["notes"][0] = "requested_fields=ts_code"
        receipt_path.write_bytes(canonical_json_bytes(receipt))
        self.resign_manifest_and_receipt(directory)
        with self.assertRaisesRegex(
            TushareCapabilityProbeError,
            "requested-fields evidence",
        ):
            verify_probe_run(directory, config=self.config, secret=SECRET)

    def test_resigned_missing_normalization_note_tamper_fails_verification(self):
        def router(endpoint, parameters):
            if endpoint == "income":
                return FakeFrame(
                    [
                        {
                            "ts_code": "000333.SZ",
                            "ann_date": "20250430",
                            "f_ann_date": "20250501",
                            "end_date": "20241231",
                            "report_type": "1",
                            "comp_type": "1",
                            "nullable_metric": float("nan"),
                        }
                    ]
                )
            return FakeFrame([])

        self.run_fixture(
            FakeClient(router),
            run_id="normalization-note-tamper",
        )
        directory = self.run_directory("normalization-note-tamper")
        receipt_path = directory / "receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        income = next(
            item
            for item in receipt["endpoint_results"]
            if item["endpoint"] == "income"
        )
        income["notes"].remove("normalization=missing_scalar_normalized")
        receipt_path.write_bytes(canonical_json_bytes(receipt))
        self.resign_manifest_and_receipt(directory)
        with self.assertRaisesRegex(
            TushareCapabilityProbeError,
            "normalization evidence",
        ):
            verify_probe_run(directory, config=self.config, secret=SECRET)

    def test_probe_receipt_cannot_unlock_experiment_v3_or_formal_alpha(self):
        from research.strategy_workspace.alpha_engine_v2 import (
            AlphaRunStatus,
            run_alpha_engine,
        )
        from research.strategy_workspace.experiment_v3_admission import (
            ExperimentV3AdmissionError,
            verify_experiment_v3_admission_receipt,
        )
        from tests.test_alpha_engine_v2 import _model_bundle, _snapshot

        self.run_fixture()
        receipt = json.loads(
            (self.run_directory() / "receipt.json").read_text(encoding="utf-8")
        )
        with self.assertRaises(ExperimentV3AdmissionError):
            verify_experiment_v3_admission_receipt(receipt, as_of=NOW)  # type: ignore[arg-type]

        model, registry, _ = _model_bundle()
        alpha = run_alpha_engine(
            _snapshot(),
            model,
            approved_factor_registry=registry,
            experiment_v3_admission_receipt=receipt,  # type: ignore[arg-type]
        )
        self.assertEqual(alpha.status, AlphaRunStatus.DATA_FAIL_CLOSED)
        self.assertTrue(
            all(
                "MISSING_TYPED_EXPERIMENT_V3_ADMISSION_RECEIPT"
                in row.exclusion_codes
                for row in alpha.rows
            )
        )

    def test_probe_writes_no_validated_or_strategy_publication_artifacts(self):
        self.run_fixture()
        forbidden = {
            "validated",
            "daily-publication-registry",
            "daily_publication_registry",
            "alpharanking",
            "exposuredecision",
            "portfolioconstruction",
            "portfoliointent",
            "nextsessionsignal",
        }
        names = {
            part.casefold()
            for path in self.run_directory().rglob("*")
            for part in path.relative_to(self.run_directory()).parts
        }
        self.assertTrue(forbidden.isdisjoint(names))

    def test_missing_token_does_not_load_sdk_and_is_structured(self):
        with patch.dict(os.environ, {TOKEN_ENVIRONMENT_VARIABLE: ""}, clear=False):
            result = run_live_probe(
                self.config,
                self.output,
                sdk_loader=lambda: self.fail("missing token must stop before SDK import"),
                clock=lambda: NOW,
                sleeper=lambda seconds: None,
                run_id="missing-token",
                repository_root=self.repository,
                git_metadata_loader=self.git_metadata,
            )
        self.assertEqual(result["credential_status"], "not_configured")
        self.assertEqual(result["request_count"], 0)
        self.assertTrue((self.run_directory("missing-token") / "receipt.json").is_file())

    def test_empty_token_does_not_expand_redaction_markers(self):
        stdout = io.StringIO()
        with patch.dict(
            os.environ,
            {TOKEN_ENVIRONMENT_VARIABLE: ""},
            clear=False,
        ), patch(
            "agent.tushare_capability_probe.run_live_probe",
            side_effect=RuntimeError("plain initialization failure"),
        ), redirect_stdout(stdout):
            self.assertEqual(
                main(
                    [
                        "--live",
                        "--config",
                        str(DEFAULT_CONFIG_PATH),
                        "--output-root",
                        str(self.output),
                    ]
                ),
                1,
            )
        failure = json.loads(stdout.getvalue())
        self.assertEqual(failure["error"], "plain initialization failure")
        self.assertNotIn("[REDACTED]", failure["error"])

    def test_token_is_stripped_before_sdk_initialization(self):
        module = FakeModule(FakeClient())
        with patch.dict(
            os.environ,
            {TOKEN_ENVIRONMENT_VARIABLE: f"  {SECRET}\t"},
            clear=False,
        ):
            run_live_probe(
                self.config,
                self.output,
                sdk_loader=lambda: module,
                baostock_capture=baostock_capture_fixture,
                clock=lambda: NOW,
                sleeper=lambda seconds: None,
                run_id="stripped-token",
                repository_root=self.repository,
                git_metadata_loader=self.git_metadata,
            )
        self.assertEqual(module.received_token, SECRET)

    def test_sdk_dependency_missing_is_structured_and_does_not_call_network(self):
        with patch.dict(os.environ, {TOKEN_ENVIRONMENT_VARIABLE: SECRET}, clear=False):
            result = run_live_probe(
                self.config,
                self.output,
                sdk_loader=lambda: (_ for _ in ()).throw(
                    DependencyMissingError("SDK unavailable")
                ),
                clock=lambda: NOW,
                sleeper=lambda seconds: None,
                run_id="sdk-missing",
                repository_root=self.repository,
                git_metadata_loader=self.git_metadata,
            )
        self.assertEqual(result["request_count"], 0)
        self.assertEqual(
            len(result["endpoint_results"]),
            len(self.config.planned_calls()),
        )
        self.assertTrue(
            all(
                item["status"] == "dependency_missing"
                and item["request_count"] == 0
                and item["failure_stage"] == "pre_request_initialization"
                for item in result["endpoint_results"]
            )
        )
        directory = self.run_directory("sdk-missing")
        self.assertTrue((directory / "manifest.json").is_file())
        self.assertTrue((directory / "receipt.json").is_file())

    def test_all_sdk_initialization_failures_seal_zero_request_receipts(self):
        cases = (
            ("permission", RuntimeError("permission denied"), "permission_denied"),
            ("rate-limit", RuntimeError("rate limit exceeded"), "rate_limited"),
            ("network", ConnectionError("connection refused"), "network_blocked"),
            ("runtime", RuntimeError("unexpected SDK initialization"), "failed"),
        )
        for label, failure, expected_status in cases:
            with self.subTest(label=label), patch.dict(
                os.environ,
                {TOKEN_ENVIRONMENT_VARIABLE: SECRET},
                clear=False,
            ):
                run_id = f"init-{label}"
                result = run_live_probe(
                    self.config,
                    self.output,
                    sdk_loader=lambda failure=failure: (_ for _ in ()).throw(
                        failure
                    ),
                    clock=lambda: NOW,
                    sleeper=lambda seconds: None,
                    run_id=run_id,
                    repository_root=self.repository,
                    git_metadata_loader=self.git_metadata,
                )
                self.assertEqual(result["request_count"], 0)
                self.assertEqual(
                    result["rate_limit_events"],
                    1 if expected_status == "rate_limited" else 0,
                )
                self.assertEqual(
                    len(result["endpoint_results"]),
                    len(self.config.planned_calls()),
                )
                self.assertTrue(
                    all(
                        item["status"] == expected_status
                        and item["request_count"] == 0
                        and item["failure_stage"]
                        == "pre_request_initialization"
                        for item in result["endpoint_results"]
                    )
                )
                directory = self.run_directory(run_id)
                self.assertTrue((directory / "manifest.json").is_file())
                self.assertTrue((directory / "receipt.json").is_file())
                verify_probe_run(directory, config=self.config, secret=SECRET)

    def test_sdk_loader_token_output_is_suppressed_and_sealed(self):
        def leaking_loader():
            print(f"loader credential={SECRET}")
            return FakeModule(FakeClient())

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result, _, _ = self.run_fixture(
                sdk_loader=leaking_loader,
                run_id="loader-output-leak",
            )
        self.assertNotIn(SECRET, stdout.getvalue())
        self.assertNotIn(SECRET, stderr.getvalue())
        self.assertEqual(result["request_count"], 0)
        self.assertTrue(
            all(item["status"] == "failed" for item in result["endpoint_results"])
        )
        directory = self.run_directory("loader-output-leak")
        self.assertTrue((directory / "receipt.json").is_file())
        self.assert_secret_absent_from_tree(directory)

    def test_pro_api_token_output_is_suppressed_and_sealed(self):
        class LeakingModule:
            __version__ = "fixture-sdk-v1"

            @staticmethod
            def pro_api(token):
                print(f"pro_api credential={token}", file=sys.stderr)
                return FakeClient()

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result, _, _ = self.run_fixture(
                sdk_loader=lambda: LeakingModule(),
                run_id="pro-api-output-leak",
            )
        self.assertNotIn(SECRET, stdout.getvalue())
        self.assertNotIn(SECRET, stderr.getvalue())
        self.assertEqual(result["request_count"], 0)
        self.assertTrue(
            all(item["status"] == "failed" for item in result["endpoint_results"])
        )
        directory = self.run_directory("pro-api-output-leak")
        self.assertTrue((directory / "receipt.json").is_file())
        self.assert_secret_absent_from_tree(directory)

    def test_endpoint_token_output_is_suppressed_and_globally_stops(self):
        def router(endpoint, parameters):
            print(f"endpoint credential={SECRET}")
            return FakeFrame([])

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result, client, _ = self.run_fixture(
                FakeClient(router),
                run_id="endpoint-output-leak",
            )
        self.assertNotIn(SECRET, stdout.getvalue())
        self.assertNotIn(SECRET, stderr.getvalue())
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result["endpoint_results"][0]["status"], "failed")
        self.assertTrue(
            all(
                item["status"] == "not_run_after_global_stop"
                for item in result["endpoint_results"][1:]
            )
        )
        directory = self.run_directory("endpoint-output-leak")
        self.assertTrue((directory / "receipt.json").is_file())
        self.assert_secret_absent_from_tree(directory)

    def test_endpoint_os_write_token_output_is_fd_captured(self):
        def router(endpoint, parameters):
            os.write(1, f"native-stdout={SECRET}".encode("utf-8"))
            os.write(2, f"native-stderr={SECRET}".encode("utf-8"))
            return FakeFrame([])

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result, client, _ = self.run_fixture(
                FakeClient(router),
                run_id="endpoint-fd-output-leak",
            )
        self.assertNotIn(SECRET, stdout.getvalue())
        self.assertNotIn(SECRET, stderr.getvalue())
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result["endpoint_results"][0]["status"], "failed")
        directory = self.run_directory("endpoint-fd-output-leak")
        self.assert_secret_absent_from_tree(directory)

    def test_endpoint_subprocess_token_output_is_fd_captured(self):
        child = (
            "import os;"
            f"os.write(1, {('child-stdout=' + SECRET).encode('utf-8')!r});"
            f"os.write(2, {('child-stderr=' + SECRET).encode('utf-8')!r})"
        )

        def router(endpoint, parameters):
            subprocess.run([sys.executable, "-c", child], check=True)
            return FakeFrame([])

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result, client, _ = self.run_fixture(
                FakeClient(router),
                run_id="endpoint-subprocess-output-leak",
            )
        self.assertNotIn(SECRET, stdout.getvalue())
        self.assertNotIn(SECRET, stderr.getvalue())
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result["endpoint_results"][0]["status"], "failed")
        directory = self.run_directory("endpoint-subprocess-output-leak")
        self.assert_secret_absent_from_tree(directory)

    def test_prebound_logging_stream_is_fd_captured(self):
        child = f"""
import logging
import sys
from agent import tushare_capability_probe as module

secret = {SECRET!r}
logger = logging.Logger("prebound-tushare-sdk-fixture")
logger.addHandler(logging.StreamHandler(sys.__stderr__))
try:
    module._call_with_suppressed_sdk_output(
        lambda: logger.error("prebound credential=%s", secret),
        secret=secret,
        operation="prebound logging fixture",
    )
except module.TushareCapabilityProbeError:
    print("credential output rejected")
else:
    raise SystemExit("credential output was not rejected")
"""
        completed = subprocess.run(
            [sys.executable, "-c", child],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            check=False,
        )
        secret_bytes = SECRET.encode("utf-8")
        self.assertEqual(completed.returncode, 0)
        self.assertFalse(
            secret_bytes in completed.stdout or secret_bytes in completed.stderr,
            "prebound logging escaped the SDK output capture",
        )
        self.assertIn(b"credential output rejected", completed.stdout)

    def test_direct_c_standard_handle_token_output_is_captured(self):
        child = f"""
import ctypes
import os
from agent import tushare_capability_probe as module

secret = {SECRET!r}
payload = ("native credential=" + secret).encode("utf-8")
def native_write():
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetStdHandle.argtypes = [ctypes.c_ulong]
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        kernel32.WriteFile.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.c_void_p,
        ]
        written = ctypes.c_ulong()
        buffer = ctypes.create_string_buffer(payload)
        if not kernel32.WriteFile(
            kernel32.GetStdHandle(ctypes.c_ulong(-11).value),
            buffer,
            len(payload),
            ctypes.byref(written),
            None,
        ):
            raise OSError(ctypes.get_last_error(), "WriteFile failed")
    else:
        runtime = ctypes.CDLL(None)
        if runtime.write(1, payload, len(payload)) < 0:
            raise OSError("native write failed")

try:
    module._call_with_suppressed_sdk_output(
        native_write,
        secret=secret,
        operation="native output fixture",
    )
except module.TushareCapabilityProbeError:
    print("credential output rejected")
else:
    raise SystemExit("credential output was not rejected")
"""
        completed = subprocess.run(
            [sys.executable, "-c", child],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            check=False,
        )
        secret_bytes = SECRET.encode("utf-8")
        self.assertEqual(completed.returncode, 0)
        self.assertFalse(
            secret_bytes in completed.stdout or secret_bytes in completed.stderr,
            "native standard-handle output escaped the SDK output capture",
        )
        self.assertIn(b"credential output rejected", completed.stdout)

    def test_token_in_sdk_error_never_enters_result_or_files(self):
        client = FakeClient(
            lambda endpoint, parameters: (_ for _ in ()).throw(
                RuntimeError(f"permission denied token={SECRET}")
            )
        )
        result, client, _ = self.run_fixture(client)
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(SECRET, rendered)
        self.assertEqual(len(client.calls), 1)
        self.assert_secret_absent_from_tree(self.run_directory())

    def test_cli_failure_stdout_and_stderr_never_expose_token(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.dict(
            os.environ, {TOKEN_ENVIRONMENT_VARIABLE: SECRET}, clear=False
        ), patch(
            "agent.tushare_capability_probe.run_live_probe",
            side_effect=RuntimeError(f"SDK exception token={SECRET}"),
        ), redirect_stdout(stdout), patch("sys.stderr", stderr):
            exit_code = main(
                [
                    "--live",
                    "--config",
                    str(DEFAULT_CONFIG_PATH),
                    "--output-root",
                    str(self.output),
                ]
            )
        self.assertEqual(exit_code, 1)
        self.assertNotIn(SECRET, stdout.getvalue())
        self.assertNotIn(SECRET, stderr.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())["status"], "failed")

    def test_token_in_return_payload_is_rejected_before_raw_hash_or_write(self):
        def router(endpoint, parameters):
            if endpoint == "daily":
                return FakeFrame([daily_record(extra={"provider_note": SECRET})])
            return FakeFrame([])

        result, _, _ = self.run_fixture(FakeClient(router))
        self.assertNotIn(SECRET, json.dumps(result, ensure_ascii=False))
        self.assert_secret_absent_from_tree(self.run_directory())
        daily_raw = list((self.run_directory() / "raw").glob("daily.*.json"))
        self.assertEqual(daily_raw, [])

    def test_three_consecutive_rate_limits_stop_all_later_calls(self):
        client = FakeClient(
            lambda endpoint, parameters: (_ for _ in ()).throw(
                RuntimeError("API rate limit exceeded")
            )
        )
        result, client, _ = self.run_fixture(client)
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(result["rate_limit_events"], 3)
        self.assertTrue(
            any(
                item["status"] == "not_run_after_global_stop"
                for item in result["endpoint_results"]
            )
        )

    def test_requests_are_rate_spaced_and_cross_validation_stays_under_cap(self):
        sleeps = []
        result, client, _ = self.run_fixture(sleeper=sleeps.append)
        self.assertEqual(len(sleeps), max(0, len(client.calls) - 1))
        self.assertTrue(
            all(value == float(self.config.minimum_interval_seconds) for value in sleeps)
        )
        self.assertLessEqual(result["request_count"], self.config.maximum_request_count)

    def test_baostock_cross_validation_is_independent_and_has_no_threshold(self):
        result, _, _ = self.run_fixture()
        comparison = result["cross_validation"]
        self.assertEqual(comparison["status"], "compared_no_threshold")
        self.assertTrue(comparison["independent_batches"])
        self.assertFalse(comparison["records_merged"])
        self.assertFalse(comparison["missing_values_filled_across_providers"])
        self.assertIsNone(comparison["automatic_difference_threshold"])
        self.assertEqual(
            comparison["field_differences"]["volume"]["maximum_absolute_difference"],
            "0.00",
        )
        self.assertNotEqual(
            comparison["tushare_raw_path"], comparison["baostock_raw_path"]
        )

    def test_baostock_unavailable_is_structured_not_configured(self):
        result, _, _ = self.run_fixture(
            baostock_capture=lambda parameters, requested_at: (_ for _ in ()).throw(
                DependencyMissingError("BaoStock SDK unavailable")
            )
        )
        self.assertEqual(
            result["cross_validation"]["status"],
            "cross_validation_not_configured",
        )
        self.assertFalse(result["cross_validation"]["records_merged"])

    def test_baostock_token_output_is_suppressed_and_not_persisted(self):
        def leaking_baostock(parameters, requested_at):
            print(f"baostock credential={SECRET}", file=sys.stderr)
            return baostock_capture_fixture(parameters, requested_at)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result, _, _ = self.run_fixture(
                baostock_capture=leaking_baostock,
                run_id="baostock-output-leak",
            )
        self.assertNotIn(SECRET, stdout.getvalue())
        self.assertNotIn(SECRET, stderr.getvalue())
        self.assertEqual(
            result["cross_validation"]["status"],
            "cross_validation_not_configured",
        )
        directory = self.run_directory("baostock-output-leak")
        self.assert_secret_absent_from_tree(directory)

    def test_default_baostock_capture_raw_replays_independently_of_fetch_time(self):
        from agent import tushare_capability_probe as module

        daily_spec = next(
            spec for spec in self.config.endpoints if spec.endpoint.value == "daily"
        )
        parameters = dict(daily_spec.parameters[0])
        rows, raw = baostock_capture_fixture(parameters, NOW)
        payload = SimpleNamespace(records=rows, raw_content=raw)
        with patch.object(BaoStockProvider, "fetch", return_value=payload):
            captured_rows, captured_raw = module._default_baostock_capture(
                parameters,
                NOW,
            )
        replayed_later = module._replay_baostock_daily_raw(
            parameters,
            NOW,
            NOW + timedelta(seconds=17),
            captured_raw,
        )
        self.assertEqual(
            canonical_json_bytes([dict(item) for item in captured_rows]),
            canonical_json_bytes([dict(item) for item in replayed_later]),
        )

    def test_resigned_baostock_records_and_comparison_tamper_fails_raw_replay(self):
        self.run_fixture(run_id="baostock-records-tamper")
        directory = self.run_directory("baostock-records-tamper")
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        artifact = next(
            item
            for item in manifest["raw_artifacts"]
            if item["provider_id"] == "baostock"
        )
        baostock_path = directory / artifact["path"]
        baostock = json.loads(baostock_path.read_text(encoding="utf-8"))
        baostock["records"][0]["close"] = "999.00"
        baostock_bytes = canonical_json_bytes(baostock)
        baostock_path.write_bytes(baostock_bytes)
        artifact["sha256"] = sha256_bytes(baostock_bytes)
        tushare_artifact = next(
            item
            for item in manifest["raw_artifacts"]
            if item.get("endpoint") == "daily"
        )
        tushare = json.loads(
            (directory / tushare_artifact["path"]).read_text(encoding="utf-8")
        )
        comparison = compare_daily_samples(tushare["rows"], baostock["records"])
        comparison.update(
            {
                "requested_at": baostock["requested_at"],
                "completed_at": baostock["completed_at"],
                "tushare_raw_path": tushare_artifact["path"],
                "tushare_raw_sha256": tushare_artifact["sha256"],
                "baostock_raw_path": artifact["path"],
                "baostock_raw_sha256": artifact["sha256"],
            }
        )
        manifest["cross_validation"] = comparison
        manifest_path.write_bytes(canonical_json_bytes(manifest))
        self.resign_manifest_and_receipt(directory)
        with self.assertRaisesRegex(
            TushareCapabilityProbeError,
            "records differ from provider raw replay|cross-validation outcome",
        ):
            verify_probe_run(directory, config=self.config, secret=SECRET)

    def test_daily_comparison_explicitly_converts_volume_and_amount_units(self):
        result = compare_daily_samples([daily_record()], [baostock_record()])
        self.assertEqual(result["field_differences"]["volume"]["mismatch_count"], 0)
        self.assertEqual(result["field_differences"]["amount"]["mismatch_count"], 0)
        self.assertEqual(
            result["unit_normalization"]["volume"]["tushare_multiplier"], "100"
        )
        self.assertEqual(
            result["unit_normalization"]["amount"]["tushare_multiplier"], "1000"
        )

    def test_daily_unit_drift_fails_before_any_live_request(self):
        endpoints = tuple(
            replace(spec, raw_units={"vol": "shares", "amount": "CNY"})
            if spec.endpoint.value == "daily"
            else spec
            for spec in self.config.endpoints
        )
        drifted = replace(self.config, endpoints=endpoints)
        with self.assertRaisesRegex(TushareCapabilityProbeError, "raw units"):
            build_plan(drifted)

    def test_run_directory_is_create_only_and_preserves_receipt_bytes(self):
        self.run_fixture()
        receipt_path = self.run_directory() / "receipt.json"
        receipt_before = receipt_path.read_bytes()
        with patch(
            "agent.tushare_capability_probe._read_tushare_token",
            side_effect=AssertionError(
                "existing run must fail before credential access"
            ),
        ), self.assertRaisesRegex(TushareCapabilityProbeError, "overwrite"):
            self.run_fixture(
                sdk_loader=lambda: self.fail(
                    "existing run must fail before SDK loading"
                )
            )
        self.assertEqual(receipt_path.read_bytes(), receipt_before)

    def test_windows_device_and_trailing_alias_run_ids_fail_before_credentials(self):
        unsafe = (
            "run.",
            "run ",
            "CON",
            "con.txt",
            "PRN",
            "AUX.log",
            "NUL",
            "COM1",
            "com9.json",
            "LPT1",
            "lpt9.txt",
        )
        for value in unsafe:
            with self.subTest(run_id=value), patch(
                "agent.tushare_capability_probe._read_tushare_token",
                side_effect=AssertionError(
                    "unsafe Windows run alias must fail before credential access"
                ),
            ):
                with self.assertRaisesRegex(
                    TushareCapabilityProbeError,
                    "unsafe probe_run_id",
                ):
                    run_live_probe(
                        self.config,
                        self.output,
                        sdk_loader=lambda: self.fail(
                            "unsafe run id must fail before SDK loading"
                        ),
                        clock=lambda: NOW,
                        sleeper=lambda seconds: None,
                        run_id=value,
                        repository_root=self.repository,
                        git_metadata_loader=self.git_metadata,
                    )

    @unittest.skipUnless(os.name == "nt", "Windows case-folding test")
    def test_run_directory_create_only_rejects_casefold_alias(self):
        self.run_fixture(run_id="CaseFoldRun")
        with self.assertRaisesRegex(TushareCapabilityProbeError, "overwrite"):
            self.run_fixture(run_id="casefoldrun")

    def test_output_root_rejects_every_directory_outside_probe_tmp_tree(self):
        forbidden_roots = (
            self.repository,
            self.repository / "data" / "tmp" / "other-probe",
            self.repository / "data" / "market_data" / "raw",
            self.repository / "data" / "market_data" / "quarantine",
            self.repository / "data" / "market_data" / "validated",
            self.repository / "research" / "strategy_workspace" / "publication",
            self.repository / "docs" / "publication",
        )
        for index, forbidden in enumerate(forbidden_roots):
            with self.subTest(path=forbidden), patch(
                "agent.tushare_capability_probe._read_tushare_token",
                side_effect=AssertionError(
                    "unsafe output root must fail before credential access"
                ),
            ):
                with self.assertRaisesRegex(
                    TushareCapabilityProbeError,
                    "data/tmp/tushare-capability",
                ):
                    run_live_probe(
                        self.config,
                        forbidden,
                        sdk_loader=lambda: self.fail(
                            "unsafe output root must fail before SDK loading"
                        ),
                        clock=lambda: NOW,
                        sleeper=lambda seconds: None,
                        run_id=f"forbidden-{index}",
                        repository_root=self.repository,
                        git_metadata_loader=self.git_metadata,
                    )

    def test_output_root_rejects_symlinked_probe_directory_when_supported(self):
        target = self.repository / "configs"
        target.mkdir()
        link = self.output
        link.parent.mkdir(parents=True)
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks unavailable: {type(exc).__name__}")
        with patch(
            "agent.tushare_capability_probe._read_tushare_token",
            side_effect=AssertionError(
                "symlinked output root must fail before credential access"
            ),
        ):
            with self.assertRaisesRegex(
                TushareCapabilityProbeError,
                "symbolic links or junctions",
            ):
                run_live_probe(
                    self.config,
                    self.output,
                    sdk_loader=lambda: self.fail(
                        "symlinked output root must fail before SDK loading"
                    ),
                    clock=lambda: NOW,
                    sleeper=lambda seconds: None,
                    run_id="symlink-root",
                    repository_root=self.repository,
                    git_metadata_loader=self.git_metadata,
                )

    def test_build_plan_hook_cannot_swap_an_existing_run_to_validated_junction(self):
        from agent import tushare_capability_probe as module

        run_id = "build-plan-junction"
        final = self.run_directory(run_id)
        outside = self.repository / "data" / "market_data" / "validated"
        original = module.build_plan

        def hooked_build_plan(*args, **kwargs):
            result = original(*args, **kwargs)
            self.create_junction(final, outside)
            return result

        try:
            with patch.object(
                module,
                "build_plan",
                side_effect=hooked_build_plan,
            ), patch.object(
                module,
                "_read_tushare_token",
                side_effect=AssertionError(
                    "pre-created final junction must fail before token access"
                ),
            ):
                with self.assertRaisesRegex(
                    TushareCapabilityProbeError,
                    "overwrite|junction|reparse",
                ):
                    run_live_probe(
                        self.config,
                        self.output,
                        clock=lambda: NOW,
                        sleeper=lambda seconds: None,
                        run_id=run_id,
                        repository_root=self.repository,
                        git_metadata_loader=self.git_metadata,
                    )
            self.assertEqual(list(outside.iterdir()), [])
        finally:
            self.remove_junction(final)

    def test_artifact_writer_junction_swap_fails_before_validated_write(self):
        from agent import tushare_capability_probe as module

        run_id = "writer-junction"
        final = self.run_directory(run_id)
        backup = self.output / f"{run_id}.original"
        outside = self.repository / "data" / "market_data" / "validated"
        outside.mkdir(parents=True)
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("unchanged", encoding="utf-8")
        original_write = module._write_create_only

        def swapping_write(path, content, **kwargs):
            original_write(path, content, **kwargs)
            if path.name == "plan.json":
                final.rename(backup)
                self.create_junction(final, outside)

        try:
            with patch.object(
                module,
                "_write_create_only",
                side_effect=swapping_write,
            ):
                with self.assertRaisesRegex(
                    TushareCapabilityProbeError,
                    "identity|junction|reparse|unavailable",
                ):
                    self.run_fixture(run_id=run_id)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
            self.assertEqual({path.name for path in outside.iterdir()}, {"sentinel.txt"})
            self.assertFalse((backup / "receipt.json").exists())
        finally:
            self.remove_junction(final)

    def test_raw_parent_junction_swap_fails_before_any_outside_bytes(self):
        from agent import tushare_capability_probe as module

        run_id = "raw-parent-junction"
        final = self.run_directory(run_id)
        raw_parent = final / "raw"
        raw_backup = final / "raw.original"
        outside = self.repository / "data" / "market_data" / "validated"
        outside.mkdir(parents=True)
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("unchanged", encoding="utf-8")
        original_write = module._write_create_only
        swapped = False

        def swapping_raw_parent(path, content, **kwargs):
            nonlocal swapped
            if not swapped and "raw" in path.relative_to(final).parts:
                raw_parent.mkdir(parents=True, exist_ok=True)
                raw_parent.rename(raw_backup)
                self.create_junction(raw_parent, outside)
                swapped = True
            return original_write(path, content, **kwargs)

        try:
            with patch.object(
                module,
                "_write_create_only",
                side_effect=swapping_raw_parent,
            ):
                with self.assertRaisesRegex(
                    TushareCapabilityProbeError,
                    "junction|reparse|identity|escapes",
                ):
                    self.run_fixture(run_id=run_id)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
            self.assertEqual({path.name for path in outside.iterdir()}, {"sentinel.txt"})
            self.assertFalse((final / "receipt.json").exists())
        finally:
            self.remove_junction(raw_parent)

    def test_verify_rejects_run_directory_replaced_by_junction(self):
        run_id = "verify-junction"
        self.run_fixture(run_id=run_id)
        final = self.run_directory(run_id)
        backup = self.output / f"{run_id}.original"
        outside = self.repository / "data" / "market_data" / "validated"
        final.rename(backup)
        try:
            self.create_junction(final, outside)
            with self.assertRaisesRegex(
                TushareCapabilityProbeError,
                "junction|reparse",
            ):
                verify_probe_run(final, config=self.config, secret=SECRET)
            self.assertEqual(list(outside.iterdir()), [])
        finally:
            self.remove_junction(final)
            if backup.exists() and not final.exists():
                backup.rename(final)

    def test_receipt_replay_detects_raw_tampering(self):
        self.run_fixture()
        raw_path = next((self.run_directory() / "raw").glob("*.json"))
        raw_path.write_bytes(b"{}")
        with self.assertRaises(TushareCapabilityProbeError):
            verify_probe_run(self.run_directory(), config=self.config, secret=SECRET)

    def test_replay_recomputes_cross_validation_after_resigned_manifest_tamper(self):
        self.run_fixture(run_id="cross-tamper")
        directory = self.run_directory("cross-tamper")
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        mismatch = manifest["cross_validation"]["field_differences"]["close"]
        mismatch["mismatch_count"] += 1
        manifest_path.write_bytes(canonical_json_bytes(manifest))
        self.resign_manifest_and_receipt(directory)
        with self.assertRaisesRegex(
            TushareCapabilityProbeError,
            "outcome payload hash mismatch|differs from replayed daily samples",
        ):
            verify_probe_run(directory, config=self.config, secret=SECRET)

    def test_replay_rejects_deleted_success_evidence_with_forged_failure(self):
        self.run_fixture(run_id="cross-delete-tamper")
        directory = self.run_directory("cross-delete-tamper")
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        baostock = next(
            item
            for item in manifest["raw_artifacts"]
            if item["provider_id"] == "baostock"
        )
        (directory / baostock["path"]).unlink()
        manifest["raw_artifacts"] = [
            item
            for item in manifest["raw_artifacts"]
            if item["provider_id"] != "baostock"
        ]
        manifest["cross_validation"] = {
            "status": "cross_validation_not_configured",
            "dataset": "daily_bar_small_sample",
            "providers": ["tushare", "baostock"],
            "independent_batches": True,
            "records_merged": False,
            "missing_values_filled_across_providers": False,
            "automatic_difference_threshold": None,
            "threshold_status": "not_configured",
            "reason": "forged BaoStock failure after deleting successful evidence",
            "failure_code": "forged_baostock_failure",
            "error": "forged failure text",
        }
        manifest_path.write_bytes(canonical_json_bytes(manifest))
        self.resign_manifest_and_receipt(directory)
        with self.assertRaisesRegex(
            TushareCapabilityProbeError,
            "outcome payload hash mismatch|cannot be downgraded from compared",
        ):
            verify_probe_run(directory, config=self.config, secret=SECRET)

    def test_replay_rejects_resigned_plan_semantic_tamper(self):
        self.run_fixture(run_id="plan-tamper")
        directory = self.run_directory("plan-tamper")
        plan_path = directory / "plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["minimum_interval_seconds"] = "0"
        plan_bytes = canonical_json_bytes(plan)
        plan_path.write_bytes(plan_bytes)
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["plan"]["sha256"] = sha256_bytes(plan_bytes)
        manifest_path.write_bytes(canonical_json_bytes(manifest))
        self.resign_manifest_and_receipt(directory)
        with self.assertRaisesRegex(
            TushareCapabilityProbeError,
            "differs from current config and access policy",
        ):
            verify_probe_run(directory, config=self.config, secret=SECRET)

    def test_replay_rejects_current_implementation_bundle_tamper(self):
        implementation_root = self.copy_implementation_bundle()
        self.run_fixture(
            run_id="bundle-tamper",
            implementation_root=implementation_root,
        )
        contract_path = (
            implementation_root
            / "research"
            / "market_data"
            / "providers"
            / "baostock.py"
        )
        contract_path.write_bytes(contract_path.read_bytes() + b"\n")
        with self.assertRaisesRegex(
            TushareCapabilityProbeError,
            "current implementation bundle",
        ):
            verify_probe_run(
                self.run_directory("bundle-tamper"),
                config=self.config,
                secret=SECRET,
                implementation_root=implementation_root,
            )

    def test_every_fixed_implementation_dependency_changes_bundle_commitment(self):
        from agent import tushare_capability_probe as module

        implementation_root = self.copy_implementation_bundle()
        baseline = compute_probe_implementation_bundle_sha256(implementation_root)
        for relative in module._IMPLEMENTATION_BUNDLE_PATHS:
            with self.subTest(relative=relative):
                path = implementation_root / relative
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                try:
                    self.assertNotEqual(
                        compute_probe_implementation_bundle_sha256(
                            implementation_root
                        ),
                        baseline,
                    )
                finally:
                    path.write_bytes(original)

    def test_interrupted_final_write_leaves_no_receipt(self):
        from agent import tushare_capability_probe as module

        original = module._write_create_only

        def interrupt(path, content, **kwargs):
            if path.name == "receipt.json":
                raise OSError("simulated interrupted final write")
            return original(path, content, **kwargs)

        with patch.object(module, "_write_create_only", side_effect=interrupt):
            with self.assertRaises(OSError):
                self.run_fixture(run_id="interrupted")
        directory = self.run_directory("interrupted")
        self.assertTrue((directory / "manifest.json").is_file())
        self.assertFalse((directory / "receipt.json").exists())
        manifest = json.loads(
            (directory / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["status"], "raw_evidence_sealed_receipt_pending")
        with self.assertRaises((OSError, TushareCapabilityProbeError)):
            verify_probe_run(directory, config=self.config, secret=SECRET)

    def test_process_interrupt_after_plan_leaves_uncommitted_plan_only_run(self):
        from agent import tushare_capability_probe as module

        original = module._write_create_only

        def interrupt(path, content, **kwargs):
            if path.parent.name == "raw":
                raise KeyboardInterrupt("simulated operator interrupt")
            return original(path, content, **kwargs)

        with patch.object(module, "_write_create_only", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_fixture(run_id="plan-only")
        directory = self.run_directory("plan-only")
        self.assertEqual(
            {path.name for path in directory.iterdir()},
            {"plan.json"},
        )
        plan = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
        self.assertNotEqual(plan.get("status"), "completed")
        with self.assertRaises(OSError):
            verify_probe_run(directory, config=self.config, secret=SECRET)

    def test_same_fixture_and_metadata_produce_identical_receipt_bytes(self):
        first_root = self.output / "one"
        second_root = self.output / "two"
        self.run_fixture(output_root=first_root, run_id="same")
        first = (first_root / "same" / "receipt.json").read_bytes()
        self.run_fixture(output_root=second_root, run_id="same")
        second = (second_root / "same" / "receipt.json").read_bytes()
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
