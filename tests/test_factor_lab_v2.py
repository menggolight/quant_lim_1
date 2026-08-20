from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from research.factor_lab import EvidenceBundle, ExperimentRunner, FactorLabError
from research.factor_lab.cli import build_parser, main
from research.factor_lab.engine import Instrument, _Evaluation, _validate_hypothesis


ROOT = Path(__file__).resolve().parents[1]
V2_CONFIG = (
    ROOT
    / "configs"
    / "factor_hypotheses"
    / "csi11_relative_momentum.csi_same_source_holdout.v2.json"
)
SCREEN_RECEIPT = (
    ROOT
    / "data"
    / "factor_evidence"
    / "v1_screen_csi_candidate_20260813"
    / "evidence"
    / "csi_official"
    / "index_level"
    / "d50777535d908e6cea4b39bed649d44b11994bc8cd926392a008e27d2d78f99a"
    / "339158f31248c69bfdbfcd024378db72014bf674202a469a754c2d6b372f0fd8.receipt"
)


class FactorLabV2AdversarialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.v1 = ExperimentRunner()
        self.v2 = ExperimentRunner(V2_CONFIG)

    def test_v2_is_separate_and_does_not_mutate_v1_contract(self) -> None:
        self.assertEqual(self.v1.hypothesis_card["hypothesis_id"], "csi11-relative-momentum-v1")
        self.assertEqual(
            self.v2.hypothesis_card["hypothesis_id"],
            "csi11-relative-momentum-csi-same-source-holdout-v2",
        )
        self.assertEqual(self.v1.hypothesis["source_policy"]["screen"]["source_id"], "choice")
        self.assertEqual(self.v2.hypothesis["source_policy"]["screen"]["source_id"], "csi")
        self.assertEqual(
            self.v2.hypothesis["source_policy"]["reconciliation"],
            {"mode": "not_applicable_same_source"},
        )
        self.assertEqual(self.v1.hypothesis["stages"], self.v2.hypothesis["stages"])
        self.assertEqual(self.v1.hypothesis["statistics"], self.v2.hypothesis["statistics"])
        self.assertEqual(self.v1.hypothesis["safety"], self.v2.hypothesis["safety"])

    def test_v2_rejects_choice_screen_policy_and_reconciliation_thresholds(self) -> None:
        forged = copy.deepcopy(self.v2.hypothesis)
        forged["source_policy"]["screen"] = copy.deepcopy(
            self.v1.hypothesis["source_policy"]["screen"]
        )
        with self.assertRaisesRegex(FactorLabError, "source_policy.screen"):
            _validate_hypothesis(forged)
        forged = copy.deepcopy(self.v2.hypothesis)
        forged["source_policy"]["reconciliation"] = copy.deepcopy(
            self.v1.hypothesis["source_policy"]["reconciliation"]
        )
        with self.assertRaisesRegex(FactorLabError, "not_applicable_same_source"):
            _validate_hypothesis(forged)

    def test_v2_rejects_current_generation_in_legacy_screen(self) -> None:
        legacy = self.v2.hypothesis["universe"]["source_index_ids"]["csi_legacy_screen"]
        instruments = []
        for item in self.v2.industry_items:
            canonical_id = str(item["canonical_id"])
            source_id = str(legacy[canonical_id])
            if canonical_id == "CSI_ENERGY":
                source_id = "932077"
            instruments.append(
                Instrument(source_id, canonical_id, "industry", str(item["name"]))
            )
        instruments.append(
            Instrument("000985", self.v2.benchmark_id, "benchmark", "中证全指")
        )
        bundle = EvidenceBundle(
            raw={},
            bundle_id="mixed-generation",
            stage="screen",
            source={},
            receipt={},
            instruments=tuple(instruments),
            calendar=(),
            bars=(),
            evidence_sha256="0" * 64,
        )
        with self.assertRaisesRegex(FactorLabError, "series mapping"):
            self.v2._validate_universe(bundle)

    def test_v2_reconciliation_artifact_is_explicitly_not_applicable(self) -> None:
        rows, passed = self.v2._reconcile_sources(None, None)  # type: ignore[arg-type]
        self.assertTrue(passed)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["passed"], "not_applicable")
        self.assertEqual(rows[0]["reason"], "not_applicable_same_source")

    def test_existing_source_owned_screen_receipt_verifies_without_network(self) -> None:
        if not SCREEN_RECEIPT.is_file():
            self.skipTest("controlled CSI legacy evidence is not present in this checkout")
        _, identity, records = self.v2._index_service_receipt(
            SCREEN_RECEIPT, ROOT / "data" / "factor_evidence"
        )
        self.assertEqual(identity["source"], "csi")
        self.assertEqual(identity["provider_id"], "csi_official")
        self.assertEqual(identity["controlled_transport"], "index_evidence_service")
        self.assertEqual(
            identity["request_fingerprint"],
            "d50777535d908e6cea4b39bed649d44b11994bc8cd926392a008e27d2d78f99a",
        )
        self.assertEqual(len(records), 18058)
        self.assertEqual({row["index_id"] for row in records}, set(
            self.v2.hypothesis_card["screen_index_ids"] + ["000985.CSI"]
        ))

    def test_v2_cli_inventory_and_preregister_remain_research_only(self) -> None:
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            self.assertEqual(main(["inventory", "--config", str(V2_CONFIG)]), 0)
        inventory = json.loads(output.getvalue())
        self.assertEqual(
            inventory["hypothesis_id"],
            "csi11-relative-momentum-csi-same-source-holdout-v2",
        )
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            manifest = self.v2.preregister(run_dir)
            self.assertFalse(manifest["paper_eligibility"])
            self.assertFalse(manifest["trade_eligibility"])
            self.assertEqual(manifest["live_execution_status"], "live_not_supported")
            self.assertIn(
                "not_applicable_same_source",
                (run_dir / "source_reconciliation.csv").read_text(encoding="utf-8"),
            )

    def test_cli_collects_repeated_screen_receipts_but_v1_rejects_them(self) -> None:
        parser = build_parser()
        parsed = parser.parse_args(
            [
                "screen",
                "--index-receipt", "first.receipt",
                "--index-receipt", "second.receipt",
                "--calendar-receipt", "calendar.json",
                "--evidence-root", "evidence",
                "--output-dir", "run",
            ]
        )
        self.assertEqual(parsed.index_receipt, [Path("first.receipt"), Path("second.receipt")])
        confirm = parser.parse_args(
            [
                "confirm",
                "--index-receipt", "confirm.json",
                "--calendar-receipt", "calendar.json",
                "--evidence-root", "evidence",
                "--screen-index-receipt", "first.receipt",
                "--screen-index-receipt", "second.receipt",
                "--screen-calendar-receipt", "calendar.json",
                "--screen-evidence-root", "evidence",
                "--screen-run", "screen-run",
                "--output-dir", "run",
            ]
        )
        self.assertEqual(len(confirm.screen_index_receipt), 2)
        with self.assertRaisesRegex(FactorLabError, "only by V2 screen"):
            self.v1.load_probe_evidence(
                stage="screen",
                index_receipt=["first.receipt", "second.receipt"],
                calendar_receipt="calendar.json",
                evidence_root=".",
            )

    def _shard_fixture(self, *, overlap: bool = False):
        source_ids = sorted(
            set(self.v2.hypothesis["universe"]["source_index_ids"]["csi_legacy_screen"].values())
            | {"000985"}
        )

        def identity(start: str, end: str, suffix: str) -> dict[str, object]:
            return {
                "source": "csi",
                "provider_id": "csi_official",
                "provider_adapter_identity": "research.market_data.providers.csi_official.CSIOfficialProvider",
                "adapter_version": "csi-official-adapter-v1",
                "upstream_source": "csindex.source_owned_https",
                "dataset_type": "index_level",
                "retrieval_mode": "historical_backfill",
                "request_fingerprint": suffix * 64,
                "request": {
                    "index_ids": [f"{value}.CSI" for value in source_ids],
                    "start_date": start,
                    "end_date": end,
                },
                "fetched_at": "2026-08-13T12:00:00+08:00",
                "point_in_time_status": "historical_backfill_not_original_capture",
                "normalized_content_sha256": ("a" if suffix == "1" else "b") * 64,
                "bundle_sha256": ("c" if suffix == "1" else "d") * 64,
                "controlled_transport": "index_evidence_service",
            }

        def records(day: str) -> list[dict[str, object]]:
            return [
                {
                    "schema_version": "index-level-v1",
                    "index_id": f"{source_id}.CSI",
                    "trading_date": day,
                    "open": "100", "high": "101", "low": "99", "close": "100",
                    "currency": "CNY", "basis": "index_points_unadjusted",
                    "available_at": f"{day}T15:30:00+08:00",
                    "availability_status": "policy_estimated",
                    "source_record_id": f"csi-index-perf:{source_id}:{day}",
                }
                for source_id in source_ids
            ]

        second_day = "2020-01-02" if overlap else "2020-01-03"
        calendar_identity = {
            "source": "sse", "provider_id": "sse_calendar",
            "provider_adapter_identity": "research.market_data.providers.sse_calendar.SSECalendarProvider",
            "adapter_version": "sse-calendar-adapter-v1", "dataset_type": "cn_equity_session",
            "fetched_at": "2026-08-13T12:00:00+08:00",
        }
        calendar = [
            {
                "schema_version": "cn-equity-session-v1", "calendar_date": day,
                "is_trading_day": True,
                "session_open_at": f"{day}T09:30:00+08:00",
                "session_close_at": f"{day}T15:00:00+08:00",
                "available_at": f"{day}T16:00:00+08:00",
                "availability_status": "known_at_capture", "source_record_id": f"sse:{day}",
            }
            for day in ("2020-01-02", "2020-01-03")
        ]
        return [
            ({}, identity("2020-01-02", "2020-01-02", "1"), records("2020-01-02")),
            ({}, identity(second_day, second_day, "2"), records(second_day)),
            ({}, calendar_identity, calendar),
        ]

    def test_v2_merges_verified_ordered_shards_and_binds_all_hashes(self) -> None:
        with mock.patch.object(self.v2, "_controlled_receipt", side_effect=self._shard_fixture()):
            bundle = self.v2.load_probe_evidence(
                stage="screen",
                index_receipt=["first.receipt", "second.receipt"],
                calendar_receipt="calendar.json",
                evidence_root=".",
            )
        self.assertEqual(len(bundle.bars), 24)
        self.assertEqual(bundle.source_bundle_sha256, ("c" * 64, "d" * 64))
        aggregate = [
            self.v2._window_row(
                row_type="aggregate", stage="screen", candidate_id=candidate_id,
                window_id="ALL", week_count=0, passed="false", selected_winner="false",
            )
            for candidate_id in ("RM20", "RM60", "RM120")
        ]
        evaluation = _Evaluation(
            stage="screen", expected_week_count=260, selected_week_ends=[], observations=[],
            weekly_metrics=[], window_metrics=aggregate, exceptions=[], summaries={},
            coverage=0.0, valid_weeks_by_window={}, selected_winner=None,
        )
        with tempfile.TemporaryDirectory() as directory:
            manifest = self.v2._publish(
                Path(directory) / "screen", stage="screen", bundle=bundle,
                evaluation=evaluation, selected_winner=None,
                status={
                    "hypothesis": "frozen", "data": "insufficient_coverage",
                    "statistics": "screen_failed",
                    "source_authentication": "official_historical_backfill_integrity_only",
                    "research_admission": "diagnostic_not_admitted",
                    "safety": "research_only_no_trading_bridge",
                },
            )
            self.assertEqual(manifest["source_bundle_sha256"], ["c" * 64, "d" * 64])
            self.v2.verify(Path(directory) / "screen")

    def test_v2_rejects_overlapping_shard_windows(self) -> None:
        with mock.patch.object(
            self.v2, "_controlled_receipt", side_effect=self._shard_fixture(overlap=True)
        ), self.assertRaisesRegex(FactorLabError, "chronological and non-overlapping"):
            self.v2.load_probe_evidence(
                stage="screen",
                index_receipt=["first.receipt", "second.receipt"],
                calendar_receipt="calendar.json",
                evidence_root=".",
            )


if __name__ == "__main__":
    unittest.main()
