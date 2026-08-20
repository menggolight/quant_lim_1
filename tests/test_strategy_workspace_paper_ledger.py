from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from research.strategy_workspace import admission as admission_module
from research.strategy_workspace.admission import (
    PAPER_ADMITTED_STATUS,
    PAPER_TRACK_REJECTED_STATUS,
    PaperAdmissionCertificate,
    PaperAdmissionError,
    evaluate_manual_real_money_candidate,
)
from research.strategy_workspace.contracts import canonical_json_bytes, canonical_sha256
from research.strategy_workspace.paper_ledger import (
    PaperDecisionDraft,
    PaperExecution,
    PaperLedgerError,
    PaperPosition,
    PaperTarget,
    UnmanagedExternalMark,
    append_paper_decision,
    create_or_verify_paper_ledger,
    derive_paper_track_record,
    seal_paper_ledger,
    verify_paper_ledger,
)


CST = timezone(timedelta(hours=8))


def trading_dates(start: date, end: date) -> tuple[date, ...]:
    result = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return tuple(result)


def certificate(config: str = "6" * 64) -> PaperAdmissionCertificate:
    return PaperAdmissionCertificate(
        certificate_id="paper-admission-test",
        issued_at=datetime(2023, 12, 1, 16, 0, tzinfo=CST),
        status=PAPER_ADMITTED_STATUS,
        data_sha256="1" * 64,
        choice_receipt_sha256="2" * 64,
        evaluation_sha256="3" * 64,
        experiment_sha256="4" * 64,
        code_sha256="5" * 64,
        backtest_sha256="7" * 64,
        top_decile_result_sha256="8" * 64,
        historical_gate_sha256="9" * 64,
        configuration_sha256=config,
        base_result_sha256="a" * 64,
        stress_result_sha256="b" * 64,
        history_start=date(2018, 1, 2),
        history_end=date(2023, 11, 30),
        history_decision_point_count=70,
        max_drawdown=Decimal("0.08"),
        annualized_one_way_turnover=Decimal("2"),
        _issuer_token=admission_module._PAPER_CERTIFICATE_ISSUER_TOKEN,
    )


def empty_draft(decision_day: date, sequence: int) -> PaperDecisionDraft:
    return PaperDecisionDraft(
        decision_id=f"paper-{sequence:02d}",
        decision_at=datetime.combine(decision_day, datetime.min.time(), CST).replace(hour=15, minute=5),
        data_available_at=datetime.combine(decision_day, datetime.min.time(), CST).replace(hour=14, minute=50),
        signal_generated_at=datetime.combine(decision_day, datetime.min.time(), CST).replace(hour=15, minute=1),
        signal_sha256=f"{sequence % 10}" * 64,
        model_result_sha256=f"{(sequence + 1) % 10}" * 64,
        source_bundle_sha256=f"{(sequence + 2) % 10}" * 64,
        targets=(),
        executions=(),
        positions=(),
        cash=Decimal("10000"),
        external_midea=UnmanagedExternalMark(
            "000333.SZ", 100, "主要消费", Decimal("50")
        ),
    )


class PaperLedgerTests(unittest.TestCase):
    def test_certificate_cannot_be_caller_constructed_or_cloned(self) -> None:
        with self.assertRaisesRegex(PaperAdmissionError, "controlled Stage-A"):
            replace(certificate())

    def test_complete_forward_ledger_derives_exact_stage_b_evidence(self) -> None:
        calendar = trading_dates(date(2024, 1, 2), date(2025, 3, 31))
        cert = certificate()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "paper.jsonl"
            with patch(
                "research.strategy_workspace.paper_ledger._now",
                return_value=datetime(2023, 12, 15, 16, 0, tzinfo=CST),
            ):
                created = create_or_verify_paper_ledger(
                    path, cert, controlled_trading_dates=calendar
                )
            self.assertEqual(len(created.decisions), 0)
            for sequence, index in enumerate(range(0, 281, 20), 1):
                decision_day = calendar[index]
                execution_day = calendar[index + 1]
                with patch(
                    "research.strategy_workspace.paper_ledger._now",
                    return_value=datetime.combine(
                        execution_day, datetime.min.time(), CST
                    ).replace(hour=15, minute=30),
                ):
                    append_paper_decision(
                        path, cert, empty_draft(decision_day, sequence)
                    )
            seal_time = datetime(2025, 2, 5, 16, 0, tzinfo=CST)
            with patch(
                "research.strategy_workspace.paper_ledger._now",
                return_value=seal_time,
            ):
                sealed = seal_paper_ledger(path, cert)
                summary = derive_paper_track_record(path, cert, as_of=seal_time)
            self.assertIsNotNone(sealed.seal)
            self.assertTrue(summary.complete, summary.reasons)
            self.assertGreaterEqual(summary.completed_months, 12)
            self.assertGreaterEqual(summary.decision_count, 12)
            self.assertEqual(summary.missing_decision_months, ())
            self.assertTrue(all(item.passed for item in summary.track_record.gate_results))
            self.assertFalse(summary.live_supported)
            self.assertEqual(summary.execution_authority, "none")
            candidate = evaluate_manual_real_money_candidate(
                cert,
                path,
                as_of=seal_time,
            )
            self.assertFalse(candidate.eligible)
            self.assertEqual(candidate.status, PAPER_TRACK_REJECTED_STATUS)
            self.assertIn(
                "blocked_missing_controlled_paper_signal_adapter",
                candidate.reasons,
            )
            self.assertIn(
                "blocked_missing_daily_paper_risk_marks",
                candidate.reasons,
            )
            self.assertEqual(candidate.paper_ledger_seal_sha256, summary.seal_sha256)
            self.assertEqual(candidate.paper_ledger_file_sha256, summary.ledger_file_sha256)
            self.assertFalse(candidate.live_supported)

            with self.assertRaisesRegex(PaperAdmissionError, "ledger path"):
                evaluate_manual_real_money_candidate(
                    cert,
                    summary.track_record,
                    as_of=seal_time,
                )

    def test_manual_veto_reserves_cash_and_cannot_be_filled(self) -> None:
        veto = PaperTarget(
            slot=1,
            instrument_id="600000.SH",
            csi_level1_industry="金融",
            action="ENTER",
            model_target_quantity=100,
            final_target_quantity=0,
            lot_size=100,
            predicted_return=Decimal("0.01"),
            percentile=Decimal("0.99"),
            target_weight=Decimal("0"),
            manual_veto=True,
            manual_veto_reason="人工否决后留现金",
            reserved_cash=Decimal("4000"),
        )
        self.assertTrue(veto.manual_veto)
        with self.assertRaisesRegex(PaperLedgerError, "broker_statement_sha256"):
            PaperExecution(
                execution_id="fake-fill",
                instrument_id="600000.SH",
                side="BUY",
                status="FILLED",
                requested_quantity=100,
                filled_quantity=100,
                execution_session=date(2024, 1, 3),
                executed_at=datetime(2024, 1, 3, 9, 30, tzinfo=CST),
                reference_open=Decimal("10"),
                fill_price=Decimal("10.01"),
                commission=Decimal("5"),
                transfer_fee=Decimal("0.01"),
                slippage_cost=Decimal("1"),
            )

    def test_replay_and_backfill_are_rejected(self) -> None:
        calendar = trading_dates(date(2024, 1, 2), date(2024, 4, 30))
        cert = certificate()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "paper.jsonl"
            with patch(
                "research.strategy_workspace.paper_ledger._now",
                return_value=datetime(2023, 12, 15, 16, 0, tzinfo=CST),
            ):
                create_or_verify_paper_ledger(path, cert, controlled_trading_dates=calendar)
            execution_day = calendar[1]
            append_time = datetime.combine(execution_day, datetime.min.time(), CST).replace(hour=15, minute=30)
            with patch("research.strategy_workspace.paper_ledger._now", return_value=append_time):
                append_paper_decision(path, cert, empty_draft(calendar[0], 1))
                with self.assertRaisesRegex(PaperLedgerError, "20 controlled sessions"):
                    append_paper_decision(path, cert, empty_draft(calendar[0], 1))
            with patch(
                "research.strategy_workspace.paper_ledger._now",
                return_value=append_time + timedelta(days=2),
            ):
                with self.assertRaisesRegex(PaperLedgerError, "without backfill"):
                    append_paper_decision(path, cert, empty_draft(calendar[20], 2))

    def test_hash_chain_tamper_and_configuration_drift_fail_closed(self) -> None:
        calendar = trading_dates(date(2024, 1, 2), date(2024, 4, 30))
        cert = certificate()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "paper.jsonl"
            with patch(
                "research.strategy_workspace.paper_ledger._now",
                return_value=datetime(2023, 12, 15, 16, 0, tzinfo=CST),
            ):
                create_or_verify_paper_ledger(path, cert, controlled_trading_dates=calendar)
            with self.assertRaisesRegex(PaperLedgerError, "certificate binding"):
                create_or_verify_paper_ledger(
                    path,
                    certificate("c" * 64),
                    controlled_trading_dates=calendar,
                )
            lines = path.read_text(encoding="utf-8").splitlines()
            payload = json.loads(lines[0])
            payload["content"]["initial_cash"] = "9999.00"
            path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(PaperLedgerError, "initial cash|SHA-256"):
                verify_paper_ledger(path, certificate=cert)

    def test_rehashed_fake_fill_snapshot_is_rejected_by_semantic_replay(self) -> None:
        calendar = trading_dates(date(2024, 1, 2), date(2024, 4, 30))
        cert = certificate()
        execution_day = calendar[1]
        target = PaperTarget(
            slot=1,
            instrument_id="600000.SH",
            csi_level1_industry="金融",
            action="ENTER",
            model_target_quantity=100,
            final_target_quantity=100,
            lot_size=100,
            predicted_return=Decimal("0.01"),
            percentile=Decimal("0.99"),
            target_weight=Decimal("0.1"),
        )
        fill = PaperExecution(
            execution_id="manual-fill-1",
            instrument_id="600000.SH",
            side="BUY",
            status="FILLED",
            requested_quantity=100,
            filled_quantity=100,
            execution_session=execution_day,
            executed_at=datetime.combine(execution_day, datetime.min.time(), CST).replace(hour=9, minute=30),
            reference_open=Decimal("10"),
            fill_price=Decimal("10.01"),
            commission=Decimal("5"),
            transfer_fee=Decimal("0.01"),
            slippage_cost=Decimal("1"),
            broker_statement_sha256="d" * 64,
            reconciliation_sha256="e" * 64,
        )
        base = empty_draft(calendar[0], 1)
        draft = PaperDecisionDraft(
            decision_id=base.decision_id,
            decision_at=base.decision_at,
            data_available_at=base.data_available_at,
            signal_generated_at=base.signal_generated_at,
            signal_sha256=base.signal_sha256,
            model_result_sha256=base.model_result_sha256,
            source_bundle_sha256=base.source_bundle_sha256,
            targets=(target,),
            executions=(fill,),
            positions=(PaperPosition("600000.SH", "金融", 100, Decimal("10")),),
            cash=Decimal("8993.99"),
            external_midea=base.external_midea,
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "paper.jsonl"
            with patch(
                "research.strategy_workspace.paper_ledger._now",
                return_value=datetime(2023, 12, 15, 16, 0, tzinfo=CST),
            ):
                create_or_verify_paper_ledger(path, cert, controlled_trading_dates=calendar)
            append_time = datetime.combine(execution_day, datetime.min.time(), CST).replace(hour=15, minute=30)
            with patch("research.strategy_workspace.paper_ledger._now", return_value=append_time):
                append_paper_decision(path, cert, draft)
            records = [json.loads(item) for item in path.read_text(encoding="utf-8").splitlines()]
            records[1]["content"]["cash"] = "9999.00"
            records[1]["record_sha256"] = canonical_sha256({
                "record_type": records[1]["record_type"],
                "previous_record_sha256": records[1]["previous_record_sha256"],
                "content": records[1]["content"],
            })
            path.write_bytes(b"".join(canonical_json_bytes(item) + b"\n" for item in records))
            with self.assertRaisesRegex(PaperLedgerError, "cash does not reconcile"):
                verify_paper_ledger(path, certificate=cert, as_of=append_time)

    def test_unsealed_or_short_ledger_cannot_be_complete(self) -> None:
        calendar = trading_dates(date(2024, 1, 2), date(2024, 4, 30))
        cert = certificate()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "paper.jsonl"
            with patch(
                "research.strategy_workspace.paper_ledger._now",
                return_value=datetime(2023, 12, 15, 16, 0, tzinfo=CST),
            ):
                create_or_verify_paper_ledger(path, cert, controlled_trading_dates=calendar)
            append_time = datetime.combine(calendar[1], datetime.min.time(), CST).replace(hour=15, minute=30)
            with patch("research.strategy_workspace.paper_ledger._now", return_value=append_time):
                append_paper_decision(path, cert, empty_draft(calendar[0], 1))
                with self.assertRaisesRegex(PaperLedgerError, "sealed ledger"):
                    derive_paper_track_record(path, cert, as_of=append_time)
                seal_paper_ledger(path, cert, reason="terminated")
                summary = derive_paper_track_record(path, cert, as_of=append_time)
            self.assertFalse(summary.complete)
            self.assertIn("forward_paper_shorter_than_12_completed_months", summary.reasons)


if __name__ == "__main__":
    unittest.main()
