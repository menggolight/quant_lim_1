from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from research.factor_lab import EvidenceBundle, ExperimentRunner, FactorLabError
from research.factor_lab.engine import (
    Bar,
    Instrument,
    _Evaluation,
    _sha256_value,
    _validate_hypothesis,
)


CHINA_TZ = timezone(timedelta(hours=8))


def _business_days(start: date, end: date) -> tuple[date, ...]:
    result: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return tuple(result)


class FactorLabAdversarialTests(unittest.TestCase):
    """Fail-closed checks for the user-frozen Factor Lab V1 boundaries."""

    def setUp(self) -> None:
        self.runner = ExperimentRunner()

    def _source_bundle(
        self,
        *,
        stage: str,
        source_id: str,
        source_authority: str,
        source_uri: str = "controlled://evidence",
        instruments: tuple[Instrument, ...] = (),
        bars: tuple[Bar, ...] = (),
        calendar: tuple[date, ...] = (),
        retrieved_at: str = "2026-08-13T18:00:00+08:00",
    ) -> EvidenceBundle:
        return EvidenceBundle(
            raw={},
            bundle_id="adversarial-bundle",
            stage=stage,
            source={
                "source_id": source_id,
                "source_authority": source_authority,
                "source_uri": source_uri,
                "adapter_version": "adversarial-v1",
                "retrieved_at": retrieved_at,
            },
            receipt={
                "transport": "factor_evidence_probe",
                "request_sha256": "1" * 64,
                "response_sha256": "2" * 64,
                "evidence_verified": True,
            },
            instruments=instruments,
            calendar=calendar,
            bars=bars,
            evidence_sha256="3" * 64,
        )

    def _confirm_instruments(
        self, *, omit: str | None = None, legacy_energy: bool = False
    ) -> tuple[Instrument, ...]:
        current = self.runner.hypothesis["universe"]["source_index_ids"][
            "csi_confirm"
        ]
        values = []
        for item in self.runner.industry_items:
            canonical_id = str(item["canonical_id"])
            if canonical_id == omit:
                continue
            source_id = str(current[canonical_id])
            if legacy_energy and canonical_id == "CSI_ENERGY":
                source_id = "000986"
            values.append(
                Instrument(
                    instrument_id=source_id,
                    canonical_id=canonical_id,
                    role="industry",
                    name=str(item["name"]),
                )
            )
        values.append(
            Instrument(
                instrument_id="000985",
                canonical_id=self.runner.benchmark_id,
                role="benchmark",
                name="中证全指",
            )
        )
        return tuple(values)

    @staticmethod
    def _minimal_json_bundle(*, trading_day: bool = True) -> dict[str, object]:
        day = "2023-01-03"
        return {
            "schema_version": "factor-lab-evidence-bundle.v1",
            "bundle_id": "minimal-adversarial",
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
                "evidence_verified": True,
            },
            "instruments": [
                {
                    "instrument_id": "000985",
                    "canonical_id": "CSI_ALL_SHARE",
                    "role": "benchmark",
                    "name": "中证全指",
                }
            ],
            "calendar": [
                {
                    "trading_date": day,
                    "is_trading_day": trading_day,
                    "available_at": "2023-01-03T09:00:00+08:00",
                    "source_record_id": "calendar-1",
                }
            ],
            "bars": [
                {
                    "instrument_id": "000985",
                    "trading_date": day,
                    "close": "100",
                    "available_at": "2023-01-03T15:30:00+08:00",
                    "source_record_id": "bar-1",
                }
            ],
        }

    def test_future_signal_available_after_decision_is_excluded(self) -> None:
        calendar = _business_days(date(2023, 1, 2), date(2023, 3, 31))
        decision_index = 20
        week_end = calendar[decision_index]
        needed = {
            calendar[0],
            week_end,
            calendar[decision_index + 1],
            calendar[decision_index + 20],
        }
        retrieved_at = datetime.combine(
            calendar[decision_index + 20], time(18), tzinfo=CHINA_TZ
        )
        bars: list[Bar] = []
        for canonical_id in (*self.runner.industry_ids, self.runner.benchmark_id):
            for trading_day in sorted(needed):
                available_at = datetime.combine(
                    trading_day, time(15, 30), tzinfo=CHINA_TZ
                )
                if trading_day == week_end:
                    available_at += timedelta(days=1)
                bars.append(
                    Bar(
                        instrument_id=canonical_id,
                        canonical_id=canonical_id,
                        trading_date=trading_day,
                        close=Decimal("100"),
                        available_at=available_at,
                        source_record_id=f"bar:{canonical_id}:{trading_day}",
                    )
                )
        bundle = self._source_bundle(
            stage="screen",
            source_id="choice",
            source_authority="licensed_secondary",
            calendar=calendar,
            bars=tuple(bars),
            retrieved_at=retrieved_at.isoformat(),
        )
        observations, exceptions, _ = self.runner._build_observations(
            bundle,
            "screen",
            [week_end],
            {week_end: "screen-W1"},
            candidate_ids={"RM20"},
        )
        self.assertEqual(observations, [])
        self.assertEqual([item["code"] for item in exceptions], ["future_available_signal"])

    def test_forged_official_transport_config_is_rejected(self) -> None:
        payload = copy.deepcopy(self.runner.hypothesis)
        payload["source_policy"]["official_transport_status"] = "authenticated"
        with self.assertRaisesRegex(FactorLabError, "cannot claim"):
            _validate_hypothesis(payload)

    def test_choice_cannot_fill_an_official_confirm_bundle_by_relabeling(self) -> None:
        day = date(2026, 8, 12)
        bundle = self._source_bundle(
            stage="confirm",
            source_id="csi",
            source_authority="official",
            source_uri="choice://gap-fill-disguised-as-csi",
            bars=(
                Bar(
                    instrument_id="932077",
                    canonical_id="CSI_ENERGY",
                    trading_date=day,
                    close=Decimal("100"),
                    available_at=datetime(2026, 8, 12, 15, 30, tzinfo=CHINA_TZ),
                    source_record_id="choice:932077:2026-08-12",
                ),
            ),
            calendar=(day,),
        )
        with self.assertRaisesRegex(FactorLabError, "Choice|official|source"):
            self.runner._validate_source(bundle, "confirm")

    def test_confirm_rejects_mixed_legacy_and_current_series(self) -> None:
        bundle = self._source_bundle(
            stage="confirm",
            source_id="csi",
            source_authority="official",
            instruments=self._confirm_instruments(legacy_energy=True),
        )
        with self.assertRaisesRegex(FactorLabError, "series|mapping|universe"):
            self.runner._validate_universe(bundle)

    def test_confirm_rejects_fewer_than_eleven_industries(self) -> None:
        bundle = self._source_bundle(
            stage="confirm",
            source_id="csi",
            source_authority="official",
            instruments=self._confirm_instruments(omit="CSI_REAL_ESTATE"),
        )
        with self.assertRaisesRegex(FactorLabError, "universe mismatch"):
            self.runner._validate_universe(bundle)

    def test_bar_on_non_trading_calendar_date_is_rejected(self) -> None:
        payload = self._minimal_json_bundle(trading_day=False)
        payload["calendar"].insert(  # type: ignore[union-attr]
            0,
            {
                "trading_date": "2023-01-02",
                "is_trading_day": True,
                "available_at": "2023-01-02T09:00:00+08:00",
                "source_record_id": "calendar-0",
            },
        )
        with self.assertRaisesRegex(FactorLabError, "not on an official trading session"):
            EvidenceBundle.from_json(payload)

    def test_duplicate_instrument_date_bar_is_rejected(self) -> None:
        payload = self._minimal_json_bundle()
        payload["bars"].append(dict(payload["bars"][0]))  # type: ignore[union-attr,index]
        with self.assertRaisesRegex(FactorLabError, "duplicate instrument/date"):
            EvidenceBundle.from_json(payload)

    def test_resigned_manifest_cannot_replace_the_frozen_winner(self) -> None:
        aggregate = []
        for candidate_id in ("RM20", "RM60", "RM120"):
            aggregate.append(
                self.runner._window_row(
                    row_type="aggregate",
                    stage="screen",
                    candidate_id=candidate_id,
                    window_id="ALL",
                    week_count=260,
                    mean_ic=0.10,
                    median_ic=0.10,
                    passed="true",
                    selected_winner=str(candidate_id == "RM20").lower(),
                )
            )
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
            valid_weeks_by_window={f"screen-W{i}": 52 for i in range(1, 6)},
            selected_winner="RM20",
        )
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "screen"
            self.runner._publish(
                run_dir,
                stage="screen",
                bundle=None,
                evaluation=evaluation,
                selected_winner="RM20",
                status={
                    "hypothesis": "frozen",
                    "data": "complete_case_passed",
                    "statistics": "screen_passed",
                    "source_authentication": "licensed_secondary_probe_integrity_only",
                    "research_admission": "diagnostic_not_admitted",
                    "safety": "research_only_no_trading_bridge",
                },
            )
            manifest_path = run_dir / "run_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["selected_winner"] = "RM60"
            manifest["run_id"] = _sha256_value(
                {key: value for key, value in manifest.items() if key != "run_id"}
            )
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(FactorLabError, "winner|semantic|manifest"):
                self.runner.verify(run_dir)

    def test_fourth_candidate_is_rejected_by_frozen_family(self) -> None:
        payload = copy.deepcopy(self.runner.hypothesis)
        payload["candidates"].append(
            {
                "candidate_id": "RM240",
                "lookback_sessions": 240,
                "expected_sign": "positive",
                "formula": "log_industry_close_return_minus_log_benchmark_close_return",
            }
        )
        with self.assertRaisesRegex(FactorLabError, "exactly RM20, RM60 and RM120"):
            _validate_hypothesis(payload)

    def test_screen_labels_must_mature_before_confirm_holdout_begins(self) -> None:
        calendar = _business_days(date(2017, 1, 2), date(2023, 4, 30))
        bundle = self._source_bundle(
            stage="screen",
            source_id="choice",
            source_authority="licensed_secondary",
            calendar=calendar,
        )
        selected, _ = self.runner._expected_weeks(bundle, "screen")
        self.assertEqual(len(selected), 260)
        latest = selected[-1]
        label_end = calendar[calendar.index(latest) + 20]
        screen_cutoff = date.fromisoformat(
            self.runner.hypothesis["time_policy"]["screen_signal_cutoff"]
        )
        self.assertLessEqual(label_end, screen_cutoff)

    def test_overlapping_labels_cannot_be_reconfigured_as_iid(self) -> None:
        payload = copy.deepcopy(self.runner.hypothesis)
        payload["statistics"]["block_length_weeks"] = 1
        payload["statistics"]["permutation"] = "iid_week_shuffle"
        with self.assertRaisesRegex(FactorLabError, "inference settings are frozen"):
            _validate_hypothesis(payload)

    def test_single_industry_driven_confirmation_fails_concentration_gate(self) -> None:
        summary = {
            "week_count": 104,
            "window_counts": {"confirm-W1": 52, "confirm-W2": 52},
            "mean_ic": 0.10,
            "positive_window_count": 2,
            "bootstrap_lower_95": 0.01,
            "mean_gross_spread": 0.01,
            "permutation_p_value": 0.01,
            "leave_one_industry_out": {
                industry_id: 0.05 for industry_id in self.runner.industry_ids
            },
            "max_industry_contribution_share": 0.90,
            "passed": False,
            "gate_reasons": [],
        }
        summaries = {"RM20": summary}
        self.runner._apply_gates(
            stage="confirm",
            summaries=summaries,
            coverage=1.0,
            valid_weeks_by_window={"confirm-W1": 52, "confirm-W2": 52},
        )
        self.assertFalse(summary["passed"])
        self.assertIn("industry_contribution_too_concentrated", summary["gate_reasons"])

    def test_missing_required_bar_field_is_rejected(self) -> None:
        payload = self._minimal_json_bundle()
        del payload["bars"][0]["source_record_id"]  # type: ignore[index]
        with self.assertRaisesRegex(FactorLabError, "missing=.*source_record_id"):
            EvidenceBundle.from_json(payload)

    def test_resigned_manifest_cannot_enable_live_or_fake_admission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "preregister"
            self.runner.preregister(run_dir)
            manifest_path = run_dir / "run_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["paper_eligibility"] = True
            manifest["trade_eligibility"] = True
            manifest["live_execution_status"] = "enabled"
            manifest["status"]["source_authentication"] = "official_verified"
            manifest["status"]["research_admission"] = "admitted"
            manifest["safety"] = {
                "mode": "live",
                "live": "supported",
                "trading_bridge": "enabled",
            }
            manifest["run_id"] = _sha256_value(
                {key: value for key, value in manifest.items() if key != "run_id"}
            )
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(FactorLabError, "live|safety|eligibility|admission"):
                self.runner.verify(run_dir)


if __name__ == "__main__":
    unittest.main()
