from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from operations.diagnose_technical_shadow_exposure import (
    SAFETY,
    _digest,
    _evaluate_policy,
    _summarize,
    _thresholds,
    build_exposure_diagnostic,
    write_exposure_diagnostic,
)
from operations.run_technical_shadow_mvp import (
    CapturedData,
    TechnicalShadowRunError,
    _load_config,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "configs" / "a_share_technical_shadow_mvp.v1.json"


def _row(instrument_id: str, day: date, close: float) -> dict:
    return {
        "instrument_id": instrument_id,
        "trading_date": day.isoformat(),
        "open": close,
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
        "preclose": close,
        "volume": 1_000_000.0,
        "amount": close * 1_000_000.0,
        "adjustment": "none",
        "trading_status": "traded",
        "is_st": False,
        "available_at": f"{day.isoformat()}T15:30:00+08:00",
    }


def _captured(config: dict, *, decision_count: int = 2) -> CapturedData:
    session_count = 120 + decision_count + 1
    start = date(2025, 1, 1)
    sessions = tuple(start + timedelta(days=index) for index in range(session_count))
    benchmark_id = config["data"]["benchmark_id"]
    benchmark = tuple(
        _row(benchmark_id, day, 100.0 + index * 0.10)
        for index, day in enumerate(sessions)
    )
    stocks = {}
    for offset, instrument_id in enumerate(config["universe"]["instrument_ids"]):
        stocks[instrument_id] = tuple(
            _row(
                instrument_id,
                day,
                10.0 + offset * 0.10 + index * (0.01 + offset * 0.00001),
            )
            for index, day in enumerate(sessions)
        )
    return CapturedData(
        provider_id="mock",
        provider_kind="test_fixture",
        adapter_version="test-baostock-read-only",
        synthetic=True,
        captured_at="2026-08-26T18:00:00+08:00",
        sessions=sessions,
        stock_rows=stocks,
        benchmark_rows=benchmark,
        receipts={
            "calendar": {
                "provider_id": "mock",
                "provider_kind": "test_fixture",
                "synthetic": True,
            }
        },
    )


def _summary_row(index: int, state: str) -> dict:
    matched_rule = {
        "RISK_OFF": "risk_off",
        "DEFENSIVE": "defensive",
        "NEUTRAL": "neutral",
        "RISK_ON": "risk_on",
    }[state]
    return {
        "decision_date": (date(2026, 1, 1) + timedelta(days=index)).isoformat(),
        "benchmark_trend": -0.01 + index * 0.01,
        "market_breadth": 0.35 + index * 0.08,
        "realized_volatility": 0.15 + index * 0.01,
        "market_drawdown": -0.05 + index * 0.005,
        "account_drawdown": 0.0,
        "condition_results": {
            "risk_off": {
                "benchmark_trend": state == "RISK_OFF",
                "market_breadth": False,
                "account_drawdown": False,
            },
            "defensive": {
                "market_breadth": state == "DEFENSIVE",
                "realized_volatility": False,
                "account_drawdown": False,
            },
            "risk_on": {
                "benchmark_trend": state == "RISK_ON",
                "market_breadth": state == "RISK_ON",
                "realized_volatility": state == "RISK_ON",
                "account_drawdown": True,
            },
        },
        "matched_rule": matched_rule,
        "previous_state": None,
        "candidate_state": state,
        "pending_state": None,
        "hysteresis_count": 0,
        "final_state": state,
        "target_gross_exposure": {
            "RISK_OFF": 0.0,
            "DEFENSIVE": 0.30,
            "NEUTRAL": 0.60,
            "RISK_ON": 1.0,
        }[state],
        "reason_codes": [f"exposure_{state.lower()}"],
        "data_fail_closed": False,
        "eligible_stock_count": 60,
        "implementation_crosscheck_passed": True,
    }


class FrozenPolicyBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = _load_config(CONFIG_PATH)
        cls.policy = cls.config["exposure"]

    def evaluate(self, *, trend: float, breadth: float, volatility: float, drawdown: float):
        return _evaluate_policy(
            benchmark_trend=trend,
            market_breadth=breadth,
            realized_volatility=volatility,
            account_drawdown=drawdown,
            policy=self.policy,
        )

    def test_frozen_thresholds_preserve_exact_operators(self):
        thresholds = _thresholds(self.policy)
        self.assertEqual(thresholds["risk_off"]["benchmark_trend"], {"operator": "<=", "value": 0.0})
        self.assertEqual(thresholds["risk_off"]["market_breadth"], {"operator": "<", "value": 0.40})
        self.assertEqual(thresholds["defensive"]["market_breadth"], {"operator": "<", "value": 0.50})
        self.assertEqual(thresholds["defensive"]["realized_volatility"], {"operator": ">", "value": 0.30})
        self.assertEqual(thresholds["risk_on"]["market_breadth"], {"operator": ">=", "value": 0.60})
        self.assertEqual(thresholds["risk_on"]["realized_volatility"], {"operator": "<=", "value": 0.20})
        self.assertEqual(
            thresholds["market_drawdown"],
            {"operator": None, "value": None, "used_by_policy": False},
        )

    def test_rule_priority_and_boundary_semantics(self):
        conditions, matched, state = self.evaluate(
            trend=0.0, breadth=0.30, volatility=0.40, drawdown=-0.10
        )
        self.assertEqual((matched, state), ("risk_off", "RISK_OFF"))
        self.assertTrue(all(conditions["risk_off"].values()))

        conditions, matched, state = self.evaluate(
            trend=0.01, breadth=0.40, volatility=0.10, drawdown=0.0
        )
        self.assertFalse(conditions["risk_off"]["market_breadth"])
        self.assertEqual((matched, state), ("defensive", "DEFENSIVE"))

        _, matched, state = self.evaluate(
            trend=0.01, breadth=0.50, volatility=0.30, drawdown=0.0
        )
        self.assertEqual((matched, state), ("neutral", "NEUTRAL"))

        conditions, matched, state = self.evaluate(
            trend=0.01, breadth=0.60, volatility=0.20, drawdown=-0.029
        )
        self.assertTrue(all(conditions["risk_on"].values()))
        self.assertEqual((matched, state), ("risk_on", "RISK_ON"))

        _, matched, state = self.evaluate(
            trend=0.01, breadth=0.60, volatility=0.20, drawdown=-0.03
        )
        self.assertEqual((matched, state), ("neutral", "NEUTRAL"))


class ExposureDiagnosticArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = _load_config(CONFIG_PATH)

    def test_daily_contract_and_no_hysteresis_are_explicit(self):
        captured = _captured(self.config, decision_count=2)
        daily, summary = build_exposure_diagnostic(
            config=self.config,
            captured=captured,
            decision_count=2,
            initial_cash=Decimal("10000"),
        )
        self.assertEqual(len(daily), 2)
        self.assertEqual(daily[-1]["decision_date"], captured.sessions[-1].isoformat())
        required = {
            "decision_date",
            "benchmark_trend",
            "market_breadth",
            "realized_volatility",
            "market_drawdown",
            "account_drawdown",
            "thresholds",
            "condition_results",
            "matched_rule",
            "previous_state",
            "candidate_state",
            "pending_state",
            "hysteresis_count",
            "final_state",
            "target_gross_exposure",
            "reason_codes",
        }
        for index, row in enumerate(daily):
            self.assertTrue(required.issubset(row))
            self.assertIsNone(row["pending_state"])
            self.assertEqual(row["hysteresis_count"], 0)
            self.assertEqual(row["candidate_state"], row["final_state"])
            self.assertTrue(row["implementation_crosscheck_passed"])
            self.assertFalse(row["market_drawdown_used_by_policy"])
            if index == 0:
                self.assertIsNone(row["previous_state"])
            else:
                self.assertEqual(row["previous_state"], daily[index - 1]["final_state"])
        self.assertFalse(summary["hysteresis_enabled"])
        self.assertEqual(summary["hysteresis_policy"], "none")
        self.assertEqual(
            summary["account_path"],
            "flat_cash_counterfactual_for_market_policy_diagnosis",
        )
        self.assertFalse(summary["account_drawdown_is_strategy_replay"])
        self.assertTrue(summary["implementation_checks_all_passed"])
        self.assertFalse(summary["fixed_bug_behavioral_impact_on_frozen_policy"])
        self.assertFalse(summary["fixed_bug_is_risk_off_root_cause"])

    def test_non_monotonic_or_duplicate_sessions_are_rejected(self):
        captured = _captured(self.config, decision_count=2)
        sessions = list(captured.sessions)
        sessions[-1] = sessions[-2]
        captured = CapturedData(
            provider_id=captured.provider_id,
            provider_kind=captured.provider_kind,
            adapter_version=captured.adapter_version,
            synthetic=captured.synthetic,
            captured_at=captured.captured_at,
            sessions=tuple(sessions),
            stock_rows=captured.stock_rows,
            benchmark_rows=captured.benchmark_rows,
            receipts=captured.receipts,
        )
        with self.assertRaisesRegex(
            TechnicalShadowRunError,
            "sessions_not_strictly_increasing_unique",
        ):
            build_exposure_diagnostic(
                config=self.config,
                captured=captured,
                decision_count=2,
            )

    def test_summary_counts_switches_streaks_and_reachability(self):
        states = ["RISK_OFF", "RISK_OFF", "DEFENSIVE", "RISK_ON", "RISK_ON", "NEUTRAL"]
        daily = [_summary_row(index, state) for index, state in enumerate(states)]
        summary = _summarize(
            daily,
            policy=self.config["exposure"],
            thresholds=_thresholds(self.config["exposure"]),
        )
        self.assertEqual(summary["state_distribution"]["RISK_OFF"]["days"], 2)
        self.assertEqual(summary["state_distribution"]["RISK_ON"]["days"], 2)
        self.assertAlmostEqual(summary["state_distribution"]["DEFENSIVE"]["proportion"], 1 / 6)
        self.assertEqual(summary["state_switch_count"], 3)
        self.assertEqual(summary["longest_consecutive_risk_off"], 2)
        self.assertEqual(summary["longest_consecutive_risk_on"], 2)
        self.assertEqual(summary["unobserved_states_in_window"], [])
        self.assertEqual(summary["structurally_unreachable_states"], [])
        self.assertEqual(
            set(summary["structurally_reachable_states"]),
            {"RISK_OFF", "DEFENSIVE", "NEUTRAL", "RISK_ON"},
        )
        self.assertFalse(summary["unreachable_state_found"])
        self.assertEqual(len(summary["nonzero_position_dates"]), 4)
        self.assertEqual(summary["unit_direction_or_mapping_errors"], [])

    def test_descriptive_market_drawdown_failure_keeps_terminal_day(self):
        captured = _captured(self.config, decision_count=1)
        benchmark = [dict(row) for row in captured.benchmark_rows]
        benchmark[-2]["close"] = None
        captured = CapturedData(
            provider_id=captured.provider_id,
            provider_kind=captured.provider_kind,
            adapter_version=captured.adapter_version,
            synthetic=captured.synthetic,
            captured_at=captured.captured_at,
            sessions=captured.sessions,
            stock_rows=captured.stock_rows,
            benchmark_rows=tuple(benchmark),
            receipts=captured.receipts,
        )
        daily, _ = build_exposure_diagnostic(
            config=self.config,
            captured=captured,
            decision_count=1,
        )
        self.assertEqual(len(daily), 1)
        self.assertTrue(daily[0]["data_fail_closed"])
        self.assertIsNone(daily[0]["market_drawdown"])
        self.assertIn(
            "market_drawdown_unavailable_descriptive_only",
            daily[0]["reason_codes"],
        )

    def test_write_is_create_only_and_manifest_hashes_every_prior_artifact(self):
        captured = _captured(self.config, decision_count=2)
        daily, summary = build_exposure_diagnostic(
            config=self.config,
            captured=captured,
            decision_count=2,
        )
        with tempfile.TemporaryDirectory() as directory:
            output_root = Path(directory)
            run_root = write_exposure_diagnostic(
                config=self.config,
                captured=captured,
                daily=daily,
                summary=summary,
                output_root=output_root,
                run_id="fixed",
            )
            manifest_path = run_root / "run_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["decision_day_count"], 2)
            self.assertEqual(manifest["summary_sha256"], _digest(summary))
            self.assertFalse(manifest["hysteresis_enabled"])
            self.assertFalse(manifest["market_drawdown_used_by_policy"])
            self.assertTrue(all(value is False for value in manifest["safety"].values()))
            self.assertEqual(manifest["safety"], SAFETY)
            for relative_path, expected_hash in manifest["artifacts"].items():
                payload = (run_root / relative_path).read_bytes()
                self.assertEqual(hashlib.sha256(payload).hexdigest(), expected_hash)
            self.assertEqual(len(list((run_root / "daily").glob("*.exposure.json"))), 2)
            with self.assertRaisesRegex(
                TechnicalShadowRunError, "create_only_run_directory_exists"
            ):
                write_exposure_diagnostic(
                    config=self.config,
                    captured=captured,
                    daily=daily,
                    summary=summary,
                    output_root=output_root,
                    run_id="fixed",
                )


if __name__ == "__main__":
    unittest.main()
