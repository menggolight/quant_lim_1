from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

from research.strategy_workspace.backtest import run_backtest
from research.strategy_workspace.catalog import get_factor
from research.strategy_workspace.cli import EXPECTED_DIAGNOSTIC_ARTIFACTS, main
from research.strategy_workspace.contracts import canonical_sha256
from research.strategy_workspace.industry import (
    IndustryEvidenceError,
    build_relative_momentum_signals,
    load_csi_industry_evidence,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROLLED_EVIDENCE_ROOT = REPO_ROOT / "data" / "factor_evidence"
CONTROLLED_EVIDENCE_PATH = (
    CONTROLLED_EVIDENCE_ROOT
    / "v2_screen_csi_20170103_20230310"
    / "evidence"
    / "csi_official"
    / "index_level"
    / "56a81eb7403bd2b4be1139305fba6e494d72d3126e2fe83ac4298195553ff807"
    / "cda0644b0dd74628955c1feb7a6d17d195103440c4e6ff1197a30030b713cd97.json"
)
CONTROLLED_HYPOTHESIS_PATH = (
    REPO_ROOT / "configs" / "factor_hypotheses" / "csi11_relative_momentum.v1.json"
)


class StrategyWorkspaceCliTests(unittest.TestCase):
    @staticmethod
    def _run_cli(argv: list[str]) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            return_code = main(argv)
        return return_code, stdout.getvalue(), stderr.getvalue()

    @staticmethod
    def _write_json(path: Path, payload: object) -> None:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )

    @classmethod
    def _preregister_rm20(cls, root: Path) -> Path:
        plan = root / "rm20-plan.json"
        code, stdout, stderr = cls._run_cli(
            [
                "preregister",
                "--thesis-id",
                "csi-rm20-baseline-v1",
                "--viewpoint",
                "Industry relative momentum may persist for one holding period.",
                "--mechanism",
                "price.momentum",
                "--horizon-days",
                "20",
                "--output",
                str(plan),
            ]
        )
        if code != 0 or stdout.strip() != "frozen_research_only" or stderr:
            raise AssertionError(
                f"could not preregister RM20 fixture: {code}, {stdout!r}, {stderr!r}"
            )
        return plan

    @classmethod
    def _synthetic_csi_bundle(cls, root: Path) -> tuple[Path, Path, list[date]]:
        sessions = [date(2026, 1, 2) + timedelta(days=index) for index in range(22)]
        records: list[dict[str, object]] = []
        for index, trading_date in enumerate(sessions):
            available_at = f"{trading_date.isoformat()}T16:00:00+08:00"
            records.extend(
                [
                    {
                        "index_id": "930001",
                        "trading_date": trading_date.isoformat(),
                        "available_at": available_at,
                        "close": 100 + 2 * index,
                    },
                    {
                        "index_id": "930002.CSI",
                        "trading_date": trading_date.isoformat(),
                        "available_at": available_at,
                        "close": 100 + index / 2,
                    },
                    {
                        "index_id": "000985",
                        "trading_date": trading_date.isoformat(),
                        "available_at": available_at,
                        "close": 100 + index,
                    },
                ]
            )
        evidence_path = root / "evidence.json"
        hypothesis_path = root / "hypothesis.json"
        cls._write_json(
            evidence_path,
            {
                "dataset_type": "index_level",
                "admission_status": "admitted_for_research",
                "point_in_time_status": "historical_backfill_not_original_capture",
                "records": records,
            },
        )
        cls._write_json(
            hypothesis_path,
            {
                "universe": {
                    "source_index_ids": {
                        "choice_screen": {
                            "industry_a": "930001",
                            "industry_b": "930002",
                        },
                        "benchmark": "000985",
                    }
                }
            },
        )
        return evidence_path, hypothesis_path, sessions

    def test_preregister_is_append_only_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            output = Path(raw_root) / "frozen-plan.json"
            arguments = [
                "preregister",
                "--thesis-id",
                "demand-persistence-v1",
                "--viewpoint",
                "Demand may persist into the next holding period.",
                "--mechanism",
                "price.momentum",
                "--horizon-days",
                "20",
                "--output",
                str(output),
            ]
            first_code, first_stdout, first_stderr = self._run_cli(arguments)
            original = output.read_bytes()
            second_code, second_stdout, second_stderr = self._run_cli(arguments)

            self.assertEqual(first_code, 0)
            self.assertEqual(first_stdout.strip(), "frozen_research_only")
            self.assertEqual(first_stderr, "")
            self.assertEqual(second_code, 2)
            self.assertEqual(second_stdout, "")
            self.assertIn("refusing to overwrite", second_stderr)
            self.assertEqual(output.read_bytes(), original)

    def test_unknown_mechanism_writes_explicit_blocked_research_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            output = Path(raw_root) / "blocked-plan.json"
            code, stdout, stderr = self._run_cli(
                [
                    "preregister",
                    "--thesis-id",
                    "unsupported-view-v1",
                    "--viewpoint",
                    "A view without an admitted factor mechanism.",
                    "--mechanism",
                    "fundamental.quality",
                    "--output",
                    str(output),
                ]
            )
            payload = json.loads(output.read_text(encoding="utf-8"))

            self.assertEqual(code, 2)
            self.assertEqual(stdout.strip(), "blocked")
            self.assertEqual(stderr, "")
            self.assertEqual(payload["plan"]["status"], "blocked")
            self.assertTrue(payload["plan"]["blocked_reasons"])
            self.assertFalse(payload["safety"]["paper_eligibility"])
            self.assertEqual(payload["safety"]["live"], "not_supported")

    def test_manifest_verify_accepts_exact_bytes_and_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            run_dir = Path(raw_root) / "run"
            run_dir.mkdir()
            result_payload = {
                "status": "diagnostic_only_non_tradable_index",
                "source": {
                    "evidence_sha256": "a" * 64,
                    "hypothesis_sha256": "b" * 64,
                },
                "research_binding": {
                    "plan_sha256": "c" * 64,
                    "catalog_sha256": "d" * 64,
                },
                "runtime": {"backtest_engine_version": "test.v1"},
                "safety": {
                    "paper_eligibility": False,
                    "trade_eligibility": False,
                    "live": "not_supported",
                },
            }
            result_bytes = (
                (
                    json.dumps(
                        result_payload,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n"
                ).encode("utf-8")
            )
            artifact_bytes = {
                "result.json": result_bytes,
                "report.md": b"# controlled diagnostic\n",
                "nav.csv": b"trading_date,net_nav\n",
                "trades.csv": b"trade_id\n",
                "skips.csv": b"signal_id\n",
                "signals.csv": b"signal_id,signal_date\n",
                "factor_scores.csv": b"signal_id,instrument_id,score\n",
            }
            self.assertEqual(set(artifact_bytes), EXPECTED_DIAGNOSTIC_ARTIFACTS)
            for name, content in artifact_bytes.items():
                (run_dir / name).write_bytes(content)
            self._write_json(
                run_dir / "run_manifest.json",
                {
                    "schema_version": "strategy-workspace-run-manifest.v1",
                    "run_id": canonical_sha256(result_payload),
                    "status": result_payload["status"],
                    "source_evidence_sha256": result_payload["source"][
                        "evidence_sha256"
                    ],
                    "source_hypothesis_sha256": result_payload["source"][
                        "hypothesis_sha256"
                    ],
                    "discovery_plan_sha256": result_payload["research_binding"][
                        "plan_sha256"
                    ],
                    "catalog_sha256": result_payload["research_binding"][
                        "catalog_sha256"
                    ],
                    "runtime": result_payload["runtime"],
                    "safety": result_payload["safety"],
                    "artifacts": {
                        name: sha256(content).hexdigest()
                        for name, content in artifact_bytes.items()
                    },
                },
            )

            valid_code, valid_stdout, valid_stderr = self._run_cli(
                ["verify", "--run-dir", str(run_dir)]
            )
            (run_dir / "report.md").write_bytes(b"# silently changed diagnostic\n")
            changed_code, changed_stdout, changed_stderr = self._run_cli(
                ["verify", "--run-dir", str(run_dir)]
            )

            self.assertEqual(valid_code, 0)
            self.assertTrue(json.loads(valid_stdout)["verified"])
            self.assertEqual(valid_stderr, "")
            self.assertEqual(changed_code, 2)
            self.assertFalse(json.loads(changed_stdout)["verified"])
            self.assertIn("sha256_mismatch:report.md", changed_stdout)
            self.assertEqual(changed_stderr, "")

    def test_manifest_verify_rejects_parent_and_absolute_path_entries(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            run_dir = root / "run"
            run_dir.mkdir()
            outside = root / "outside.txt"
            outside.write_bytes(b"secret outside controlled run")
            digest = sha256(outside.read_bytes()).hexdigest()

            for malicious_name in ("../outside.txt", str(outside.resolve())):
                with self.subTest(malicious_name=malicious_name):
                    self._write_json(
                        run_dir / "run_manifest.json",
                        {
                            "schema_version": "strategy-workspace-run-manifest.v1",
                            "run_id": "untrusted-manifest",
                            "artifacts": {
                                **{
                                    name: "0" * 64
                                    for name in EXPECTED_DIAGNOSTIC_ARTIFACTS
                                    if name != "report.md"
                                },
                                malicious_name: digest,
                            },
                        },
                    )
                    code, stdout, stderr = self._run_cli(
                        ["verify", "--run-dir", str(run_dir)]
                    )

                    self.assertEqual(code, 2)
                    self.assertNotIn('"verified": true', stdout.lower())
                    self.assertTrue(
                        "invalid_manifest_artifact" in stdout
                        or "artifact_outside_run_dir" in stdout
                    )

    def test_synthetic_csi_adapter_uses_close_known_signal_and_next_session(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            evidence_path, hypothesis_path, sessions = self._synthetic_csi_bundle(
                Path(raw_root)
            )
            evidence = load_csi_industry_evidence(evidence_path, hypothesis_path)
            signals = build_relative_momentum_signals(
                evidence,
                lookback_sessions=20,
                rebalance_sessions=20,
                top_n=1,
            )
            result = run_backtest(
                signals,
                evidence.bars,
                benchmark=evidence.benchmark,
                trading_calendar=sessions,
            )

            self.assertEqual(evidence.industry_ids, ("930001.CSI", "930002.CSI"))
            self.assertEqual(evidence.benchmark_id, "000985.CSI")
            self.assertEqual(
                evidence.admission_status, "not_admitted_unverified_evidence"
            )
            self.assertFalse(evidence.controlled_storage_verified)
            self.assertFalse(evidence.receipt_verified)
            self.assertIsNone(evidence.receipt_sha256)
            self.assertEqual(
                evidence.point_in_time_status,
                "historical_backfill_not_original_capture",
            )
            self.assertEqual(len(signals), 1)
            self.assertEqual(signals[0].signal_date, sessions[20])
            self.assertEqual(signals[0].instrument_ids, ("930001.CSI",))
            self.assertTrue(result.trades)
            self.assertTrue(
                all(trade.execution_date == sessions[21] for trade in result.trades)
            )
            self.assertTrue(
                all(trade.execution_date > trade.signal_date for trade in result.trades)
            )

    def test_csi_adapter_requires_same_day_timezone_aware_available_at(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            evidence_path, hypothesis_path, _ = self._synthetic_csi_bundle(
                Path(raw_root)
            )
            original = json.loads(evidence_path.read_text(encoding="utf-8"))
            invalid_values = (
                "2026-01-01T16:00:00+08:00",
                "2026-01-03T00:01:00+08:00",
                "2026-01-02T16:00:00",
                "2026-01-02T23:59:00-12:00",
            )
            for invalid_available_at in invalid_values:
                with self.subTest(available_at=invalid_available_at):
                    payload = json.loads(json.dumps(original))
                    payload["records"][0]["available_at"] = invalid_available_at
                    self._write_json(evidence_path, payload)
                    with self.assertRaisesRegex(
                        IndustryEvidenceError,
                        "available_at must (?:be on the signal trading_date|include a timezone offset|use the \\+08:00 offset)",
                    ):
                        load_csi_industry_evidence(evidence_path, hypothesis_path)

    def test_csi_diagnostic_cli_emits_non_tradable_status_and_verifiable_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            plan_path = self._preregister_rm20(root)
            output = root / "diagnostic-run"
            code, stdout, stderr = self._run_cli(
                [
                    "csi-diagnostic",
                    "--evidence",
                    str(CONTROLLED_EVIDENCE_PATH),
                    "--plan",
                    str(plan_path),
                    "--hypothesis",
                    str(CONTROLLED_HYPOTHESIS_PATH),
                    "--lookback-sessions",
                    "20",
                    "--rebalance-sessions",
                    "20",
                    "--top-n",
                    "1",
                    "--output",
                    str(output),
                ]
            )
            self.assertEqual(code, 0, stderr)
            self.assertRegex(stdout.strip(), r"^[0-9a-f]{64}$")
            self.assertEqual(stderr, "")
            payload = json.loads((output / "result.json").read_text(encoding="utf-8"))
            manifest = json.loads(
                (output / "run_manifest.json").read_text(encoding="utf-8")
            )
            verify_code, verify_stdout, verify_stderr = self._run_cli(
                ["verify", "--run-dir", str(output)]
            )

            self.assertEqual(payload["status"], "diagnostic_only_non_tradable_index")
            self.assertEqual(
                payload["source"]["admission_status"],
                "admitted_for_research",
            )
            self.assertTrue(payload["source"]["controlled_storage_verified"])
            self.assertTrue(payload["source"]["receipt_verified"])
            self.assertFalse(payload["safety"]["paper_eligibility"])
            self.assertFalse(payload["safety"]["trade_eligibility"])
            self.assertEqual(payload["safety"]["live"], "not_supported")
            self.assertEqual(manifest["status"], payload["status"])
            self.assertEqual(verify_code, 0)
            self.assertTrue(json.loads(verify_stdout)["verified"])
            self.assertEqual(verify_stderr, "")

    def test_csi_diagnostic_cli_rejects_evidence_outside_controlled_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            evidence_path, hypothesis_path, _ = self._synthetic_csi_bundle(root)
            plan_path = self._preregister_rm20(root)
            output = root / "must-not-exist"

            code, stdout, stderr = self._run_cli(
                [
                    "csi-diagnostic",
                    "--evidence",
                    str(evidence_path),
                    "--plan",
                    str(plan_path),
                    "--hypothesis",
                    str(hypothesis_path),
                    "--top-n",
                    "1",
                    "--output",
                    str(output),
                ]
            )

            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")
            self.assertIn("outside the controlled evidence root", stderr)
            self.assertFalse(output.exists())

    def test_rm20_catalog_formula_is_relative_to_frozen_benchmark(self) -> None:
        factor = get_factor("RM20")

        self.assertEqual(factor.required_fields, ("benchmark_close", "close"))
        self.assertEqual(
            factor.formula,
            "log(close/lag(close,20))-log(benchmark_close/lag(benchmark_close,20))",
        )


if __name__ == "__main__":
    unittest.main()
