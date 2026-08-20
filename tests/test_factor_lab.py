from __future__ import annotations

import contextlib
import io
import json
import math
import tempfile
import unittest
from unittest import mock
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from agent.factor_evidence_probe import ProbeQuery, run_probe
from research.factor_lab import (
    ARTIFACT_FILENAMES,
    EvidenceBundle,
    ExperimentRunner,
    FactorLabError,
    FactorObservation,
    RelativeMomentumPlugin,
)
from research.factor_lab.cli import build_parser, main
from research.factor_lab.engine import _Evaluation, _holm_adjust


class FactorLabContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = ExperimentRunner()

    def test_inventory_freezes_three_candidates_and_no_trading_bridge(self) -> None:
        inventory = self.runner.inventory()
        candidates = inventory["plugin"]["candidates"]
        self.assertEqual([item["candidate_id"] for item in candidates], ["RM20", "RM60", "RM120"])
        self.assertEqual([item["lookback_sessions"] for item in candidates], [20, 60, 120])
        self.assertEqual(inventory["safety"]["live"], "not_supported")
        self.assertIn("source_authentication_blocked", inventory["admission_ceiling"])

    def test_relative_momentum_uses_frozen_log_formula(self) -> None:
        plugin = RelativeMomentumPlugin()
        observed = plugin.compute(
            plugin.specs[0],
            industry_now=Decimal("121"),
            industry_then=Decimal("100"),
            benchmark_now=Decimal("110"),
            benchmark_then=Decimal("100"),
        )
        self.assertAlmostEqual(observed, math.log(1.21) - math.log(1.10))

    def test_evidence_rejects_unknown_fields_and_caller_boolean_is_only_integrity(self) -> None:
        payload = self._minimal_bundle()
        payload["unexpected"] = "forged"
        with self.assertRaisesRegex(FactorLabError, "unknown"):
            EvidenceBundle.from_json(payload)
        clean = self._minimal_bundle()
        clean["receipt"]["evidence_verified"] = True
        bundle = EvidenceBundle.from_json(clean)
        self.assertTrue(bundle.receipt["evidence_verified"])
        self.assertEqual(
            self.runner.inventory()["official_transport_status"], "not_configured"
        )

    def test_preregister_writes_exact_nine_and_verify_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            manifest = self.runner.preregister(run_dir)
            self.assertEqual(
                {item.name for item in run_dir.iterdir() if item.is_file()},
                set(ARTIFACT_FILENAMES),
            )
            verified = self.runner.verify(run_dir)
            self.assertEqual(verified["run_id"], manifest["run_id"])
            self.assertEqual(
                manifest["status"]["research_admission"], "not_screened"
            )
            self.assertFalse(manifest["paper_eligibility"])
            self.assertFalse(manifest["trade_eligibility"])
            self.assertEqual(manifest["live_execution_status"], "live_not_supported")
            self.assertIn(manifest["reproducibility_status"], {"captured", "unavailable_fail_closed"})
            card = json.loads(
                (run_dir / "hypothesis_card.json").read_text(encoding="utf-8")
            )
            self.assertEqual(card["schema_version"], "factor-hypothesis-v1")
            self.assertEqual(
                manifest["subjective_thesis"]["status"],
                "user_view_not_provided",
            )
            with (run_dir / "factor_report.md").open("a", encoding="utf-8") as handle:
                handle.write("tamper\n")
            with self.assertRaisesRegex(FactorLabError, "artifact hash mismatch"):
                self.runner.verify(run_dir)

    def test_report_displays_complete_case_week_counts_not_decimal_one(self) -> None:
        aggregate = [
            self.runner._window_row(
                row_type="aggregate",
                stage="screen",
                candidate_id=candidate_id,
                window_id="ALL",
                week_count=260,
                passed="true",
                selected_winner=str(candidate_id == "RM20").lower(),
            )
            for candidate_id in ("RM20", "RM60", "RM120")
        ]
        evaluation = _Evaluation(
            stage="screen",
            expected_week_count=260,
            selected_week_ends=[],
            observations=[],
            weekly_metrics=[],
            window_metrics=aggregate,
            exceptions=[],
            summaries={},
            coverage=1.0,
            valid_weeks_by_window={f"screen-W{index}": 52 for index in range(1, 6)},
            selected_winner="RM20",
        )
        bundle = EvidenceBundle(
            raw={}, bundle_id="report-coverage", stage="screen", source={}, receipt={},
            instruments=(), calendar=(), bars=(), evidence_sha256="0" * 64,
        )
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.runner, "_evaluate", return_value=evaluation
        ):
            run_dir = Path(directory) / "screen"
            manifest = self.runner.screen(bundle, run_dir)
            report = (run_dir / "factor_report.md").read_text(encoding="utf-8")
        self.assertIn("完整样本周：`260 / 260`（complete-case）", report)
        self.assertIn(
            "各窗口完整样本周：`screen-W1=52；screen-W2=52；screen-W3=52；screen-W4=52；screen-W5=52`",
            report,
        )
        self.assertNotIn("数据周覆盖：`1`", report)
        self.assertEqual(manifest["status"]["data"], "complete_case_passed")

    def test_output_directory_is_append_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            self.runner.preregister(run_dir)
            with self.assertRaisesRegex(FactorLabError, "must be empty"):
                self.runner.preregister(run_dir)

    def test_holm_is_monotone_and_uses_full_family(self) -> None:
        adjusted = _holm_adjust({"RM20": 0.01, "RM60": 0.03, "RM120": 0.04})
        self.assertEqual(adjusted, {"RM20": 0.03, "RM60": 0.06, "RM120": 0.06})

    def test_winner_near_tie_chooses_shorter_but_not_when_gap_equals_point01(self) -> None:
        summaries = {
            "RM20": {
                "candidate_id": "RM20",
                "lookback_sessions": 20,
                "median_window_ic": 0.091,
                "passed": True,
            },
            "RM60": {
                "candidate_id": "RM60",
                "lookback_sessions": 60,
                "median_window_ic": 0.100,
                "passed": True,
            },
            "RM120": {
                "candidate_id": "RM120",
                "lookback_sessions": 120,
                "median_window_ic": 0.05,
                "passed": False,
            },
        }
        self.assertEqual(self.runner._choose_winner(summaries), "RM20")
        summaries["RM20"]["median_window_ic"] = 0.09
        self.assertEqual(self.runner._choose_winner(summaries), "RM60")

    def test_screen_week_plan_requires_labels_to_mature_by_2023_03_10(self) -> None:
        start = date(2015, 1, 1)
        end = date(2024, 1, 1)
        calendar = []
        current = start
        while current <= end:
            if current.weekday() < 5:
                calendar.append(current)
            current += timedelta(days=1)
        bundle = EvidenceBundle(
            raw={},
            bundle_id="calendar-only-test",
            stage="screen",
            source={},
            receipt={},
            instruments=(),
            calendar=tuple(calendar),
            bars=(),
            evidence_sha256="0" * 64,
        )
        selected, _ = self.runner._expected_weeks(bundle, "screen")
        self.assertEqual(len(selected), 260)
        last_index = calendar.index(selected[-1])
        self.assertLessEqual(calendar[last_index + 20], date(2023, 3, 10))
        self.assertGreater(calendar[last_index + 21], date(2023, 3, 10))

    def test_screen_drops_week_for_all_candidates_when_one_ic_is_undefined(self) -> None:
        week_end = date(2023, 3, 10)
        observations = []
        for candidate_id, constant in (("RM20", True), ("RM60", False), ("RM120", False)):
            for index, industry_id in enumerate(self.runner.industry_ids):
                observations.append(
                    FactorObservation(
                        stage="screen",
                        window_id="screen-W1",
                        week_end=week_end,
                        label_start=date(2023, 3, 13),
                        label_end=date(2023, 4, 7),
                        candidate_id=candidate_id,
                        lookback_sessions=int(candidate_id[2:]),
                        industry_id=industry_id,
                        industry_name=industry_id,
                        signal=1.0 if constant else float(index),
                        signal_rank=6.0 if constant else float(index + 1),
                        forward_excess_return_20d=float(index) / 100.0,
                        source_bundle_id="test",
                    )
                )
        metrics, exceptions = self.runner._build_weekly_metrics(observations, "screen")
        self.assertEqual(metrics, [])
        self.assertEqual(exceptions[0]["code"], "shared_candidate_week_invalid")

    def test_cli_exposes_all_six_commands(self) -> None:
        parser = build_parser()
        for command in ("inventory", "preregister", "screen", "confirm", "weekly", "verify"):
            if command == "inventory":
                parsed = parser.parse_args([command])
            elif command == "preregister":
                parsed = parser.parse_args([command, "--output-dir", "x"])
            elif command == "verify":
                parsed = parser.parse_args([command, "--run-dir", "x"])
            else:
                # Parsing the command itself is enough to prove registration;
                # required arguments are intentionally checked by argparse.
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        parser.parse_args([command])
                continue
            self.assertEqual(parsed.command, command)

    def test_cli_inventory_returns_machine_readable_json(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["inventory"]), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["schema_version"], "factor-lab-inventory.v1")

    def test_factor_lab_directly_consumes_probe_receipts(self) -> None:
        tz_text = "+08:00"
        requested_at = datetime.fromisoformat(f"2026-08-13T09:00:00{tz_text}")
        fetched_at = datetime.fromisoformat(f"2026-08-13T09:01:00{tz_text}")
        mappings = self.runner.hypothesis["universe"]["source_index_ids"]
        index_ids = sorted(
            set(mappings["choice_screen"].values())
            | set(mappings["choice_current_reconciliation"].values())
            | {mappings["benchmark"]}
        )
        index_records = tuple(
            {
                "schema_version": "index-level-v1",
                "index_id": f"{index_id}.CSI",
                "trading_date": "2026-08-12",
                "open": None,
                "high": None,
                "low": None,
                "close": str(100 + offset),
                "currency": "CNY",
                "basis": "index_points_unadjusted",
                "available_at": "2026-08-12T16:30:00+08:00",
                "availability_status": "policy_estimated",
                "source_record_id": f"choice:{index_id}:2026-08-12",
            }
            for offset, index_id in enumerate(index_ids)
        )
        calendar_records = (
            {
                "schema_version": "cn-equity-session-v1",
                "calendar_date": "2026-08-12",
                "is_trading_day": True,
                "session_open_at": "2026-08-12T09:30:00+08:00",
                "session_close_at": "2026-08-12T15:00:00+08:00",
                "available_at": "2026-08-12T16:31:00+08:00",
                "availability_status": "known_at_capture",
                "source_record_id": "sse:2026-08-12",
            },
        )

        class Provider:
            def __init__(self, provider_id: str, adapter_version: str, records: tuple[dict[str, object], ...]):
                self.provider_id = provider_id
                self.adapter_version = adapter_version
                self.records = records

            def fetch(self, request: object) -> object:
                del request
                return SimpleNamespace(
                    raw_content=json.dumps(self.records, ensure_ascii=False).encode("utf-8"),
                    records=self.records,
                    fetched_at=fetched_at,
                    upstream_source="controlled test transport",
                    availability_status="known",
                    point_in_time_status="known_as_captured",
                )

        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repo"
            root = repository / "data" / "factor-evidence"
            repository.mkdir()
            choice = run_probe(
                ProbeQuery(
                    source="choice",
                    mode="online",
                    start_date=date(2026, 8, 12),
                    end_date=date(2026, 8, 12),
                    requested_at=requested_at,
                ),
                root,
                provider_loader=lambda source: Provider("choice_index", "choice-index-v1", index_records),
                request_factory=lambda query: object(),
                repository_root=repository,
            )
            sse = run_probe(
                ProbeQuery(
                    source="sse",
                    mode="online",
                    start_date=date(2026, 8, 12),
                    end_date=date(2026, 8, 12),
                    requested_at=requested_at,
                ),
                root,
                provider_loader=lambda source: Provider("sse_calendar", "sse-calendar-v1", calendar_records),
                request_factory=lambda query: object(),
                repository_root=repository,
            )
            bundle = self.runner.load_probe_evidence(
                stage="screen",
                index_receipt=root / str(choice["receipt_path"]),
                calendar_receipt=root / str(sse["receipt_path"]),
                evidence_root=root,
            )
            self.assertEqual(bundle.stage, "screen")
            self.assertEqual(len(bundle.instruments), 23)
            self.assertEqual(len(bundle.bars), 23)

    @staticmethod
    def _minimal_bundle() -> dict[str, object]:
        day = "2023-01-03"
        instruments = [
            {
                "instrument_id": "source-benchmark",
                "canonical_id": "CSI_ALL_SHARE",
                "role": "benchmark",
                "name": "中证全指",
            }
        ]
        bars = [
            {
                "instrument_id": "source-benchmark",
                "trading_date": day,
                "close": "100",
                "available_at": "2023-01-03T15:01:00+08:00",
                "source_record_id": "bar-1",
            }
        ]
        return {
            "schema_version": "factor-lab-evidence-bundle.v1",
            "bundle_id": "minimal",
            "stage": "screen",
            "source": {
                "source_id": "choice",
                "source_authority": "licensed_secondary",
                "source_uri": "choice://probe",
                "adapter_version": "v1",
                "retrieved_at": "2023-01-04T00:00:00+08:00",
            },
            "receipt": {
                "transport": "factor_evidence_probe",
                "request_sha256": "1" * 64,
                "response_sha256": "2" * 64,
                "evidence_verified": False,
            },
            "instruments": instruments,
            "calendar": [
                {
                    "trading_date": day,
                    "is_trading_day": True,
                    "available_at": "2023-01-03T09:00:00+08:00",
                    "source_record_id": "calendar-1",
                }
            ],
            "bars": bars,
        }


if __name__ == "__main__":
    unittest.main()
