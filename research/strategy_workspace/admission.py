"""Two-stage Paper admission for the frozen A-share Top-2 experiment.

Historical research can issue only a ``paper_admitted`` certificate. A
separate forward-only Paper track record, started after that certificate,
must survive twelve completed months in the controlled append-only Paper
ledger. Only a freshly verified and sealed ledger path can become a
``manual_real_money_candidate`` after source-authenticated forward signals and
daily PIT risk marks exist. Those adapters remain fail-closed today. Neither
state carries execution authority.
"""

from __future__ import annotations

import re
from dataclasses import InitVar, dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from .a_share_backtest import (
    CONTROLLED_EXECUTION_BAR_ADAPTER_VERIFIED,
    FORMAL_BACKTEST_SCOPE,
    UNMANAGED_MIDEA_INSTRUMENT_ID,
    AShareBacktestComparison,
    AShareBacktestError,
    HistoricalGateResult,
    PolicyGateResult,
    a_share_backtest_comparison_content_sha256,
    formal_signal_bindings_from_evaluation,
)
from .choice_gate import (
    ChoiceCapability,
    ChoiceCapabilityReceipt,
    ChoiceGateStatus,
    evaluate_choice_quality_growth_gate,
)
from .contracts import canonical_sha256
from .evaluation import (
    EvaluationResult,
    evaluation_result_content_sha256,
    trading_calendar_content_sha256,
)
from .experiment import ExperimentSpecV2
from .top_decile_backtest import (
    RESEARCH_CAPITAL,
    TOP_DECILE_LEDGER_VERSION,
    TopDecileCostLedgerResult,
    _configuration_payload,
)


PAPER_ADMISSION_VERSION = "strategy-workspace-paper-admission.v4"
PAPER_ADMITTED_STATUS = "paper_admitted"
MANUAL_CANDIDATE_STATUS = "manual_real_money_candidate"
REJECTED_STATUS = "paper_admission_rejected"
PAPER_TRACK_REJECTED_STATUS = "paper_track_rejected"
HISTORICAL_GATE_BUILDER_VERSION = "strategy-workspace-historical-gates.v2"
CHINA_STANDARD_TIME = timezone(timedelta(hours=8))
LIVE_NOT_SUPPORTED_CODE = "live_not_supported"
ZERO = Decimal("0")
CONTROLLED_TOP_DECILE_PRICE_ADAPTER_VERIFIED = False
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PAPER_CERTIFICATE_ISSUER_TOKEN = object()

REQUIRED_HISTORICAL_GATE_IDS = (
    "data_pit_complete",
    "top_decile_net_absolute_positive",
    "top_decile_net_active_positive",
    "top2_net_absolute_positive",
    "top2_net_active_positive",
    "oos_rank_ic_stable",
    "corrected_significant_factor_count_gte_2",
    "positive_semiannual_windows_gte_3_of_4",
    "stress_active_return_non_negative",
    "max_drawdown_lte_12pct",
    "annualized_one_way_turnover_lte_4",
)

REQUIRED_FORWARD_PAPER_GATE_IDS = (
    "paper_data_and_signal_pit_complete",
    "paper_next_session_open_execution_reconciled",
    "paper_costs_and_positions_reconciled",
    "paper_risk_limits_all_passed",
    "paper_configuration_unchanged",
    "paper_manual_only_live_blocked",
)


class PaperAdmissionError(ValueError):
    """Raised for malformed or internally inconsistent admission evidence."""


class LiveNotSupportedError(PaperAdmissionError):
    code = LIVE_NOT_SUPPORTED_CODE


def _hash(value: Any, field_name: str) -> str:
    result = str(value).strip()
    if _SHA256_RE.fullmatch(result) is None:
        raise PaperAdmissionError(f"{field_name} must be a lowercase SHA-256")
    return result


def _completed_months(start: date, end: date) -> int:
    months = (end.year - start.year) * 12 + end.month - start.month
    if end.day < start.day:
        months -= 1
    return months


def _require_paper_mode(requested_mode: str) -> None:
    mode = str(requested_mode).strip().upper()
    if mode == "LIVE":
        raise LiveNotSupportedError("live_not_supported: LIVE execution is not supported")
    if mode != "PAPER":
        raise PaperAdmissionError("requested_mode must be PAPER; LIVE is unsupported")


def _validate_gate_shape(
    gates: Sequence[PolicyGateResult], context: str
) -> tuple[str, ...]:
    if any(not isinstance(item, PolicyGateResult) for item in gates):
        raise PaperAdmissionError(f"{context} gates are malformed")
    gate_ids = tuple(item.gate_id for item in gates)
    if not gate_ids or len(gate_ids) != len(set(gate_ids)):
        raise PaperAdmissionError(
            f"{context} gates must be non-empty and uniquely named"
        )
    return gate_ids


def _exact_gate_reasons(
    gates: Sequence[PolicyGateResult],
    required_ids: Sequence[str],
    context: str,
) -> list[str]:
    actual = {item.gate_id for item in gates}
    required = set(required_ids)
    reasons = [
        f"{context}_gate_missing:{gate_id}"
        for gate_id in sorted(required - actual)
    ]
    reasons.extend(
        f"{context}_gate_unexpected:{gate_id}"
        for gate_id in sorted(actual - required)
    )
    reasons.extend(
        f"{context}_gate_failed:{item.gate_id}"
        for item in gates
        if item.passed is not True
    )
    return reasons


def _evaluation_binding_sha256(result: EvaluationResult) -> str:
    """Bind the typed evaluator output without trusting a caller hash."""

    if not isinstance(result, EvaluationResult):
        raise PaperAdmissionError("evaluation_result must be an EvaluationResult")
    return evaluation_result_content_sha256(result)


def _top_decile_result_payload(result: TopDecileCostLedgerResult) -> dict[str, Any]:
    return {
        "schema_version": result.schema_version,
        "research_scope": result.research_scope,
        "research_capital": result.research_capital,
        "model": result.model,
        "split": result.split,
        "benchmark_id": result.benchmark_id,
        "decision_dates": result.decision_dates,
        "base": result.base,
        "stress": result.stress,
        "gate_results": result.gate_results,
        "configuration_sha256": result.configuration_sha256,
        "evaluation_sha256": result.evaluation_sha256,
        "trading_calendar_sha256": result.trading_calendar_sha256,
        "price_data_sha256": result.price_data_sha256,
        "benchmark_data_sha256": result.benchmark_data_sha256,
        "input_bundle_sha256": result.input_bundle_sha256,
    }


def _computed_gate(
    gate_id: str,
    passed: bool,
    observed: Any,
    limit: str,
) -> PolicyGateResult:
    return PolicyGateResult(gate_id, bool(passed), str(observed), limit)


def build_historical_gate_result(
    *,
    backtest_result: AShareBacktestComparison,
    top_decile_result: TopDecileCostLedgerResult,
    evaluation_result: EvaluationResult,
    experiment_spec: ExperimentSpecV2,
    choice_receipt: ChoiceCapabilityReceipt,
) -> HistoricalGateResult:
    """Compute the exact 11 historical gates from controlled typed results.

    No gate booleans or historical summary are accepted from the caller.  The
    evaluator's raw Rank-IC/factor rows, the two cost ledgers and the Choice
    receipt are the only inputs to these admission booleans.
    """

    if not isinstance(backtest_result, AShareBacktestComparison):
        raise PaperAdmissionError(
            "backtest_result must be an AShareBacktestComparison"
        )
    if not isinstance(top_decile_result, TopDecileCostLedgerResult):
        raise PaperAdmissionError(
            "top_decile_result must be a TopDecileCostLedgerResult"
        )
    if not isinstance(evaluation_result, EvaluationResult):
        raise PaperAdmissionError("evaluation_result must be an EvaluationResult")
    if not isinstance(experiment_spec, ExperimentSpecV2):
        raise PaperAdmissionError("experiment_spec must be an ExperimentSpecV2")
    if not isinstance(choice_receipt, ChoiceCapabilityReceipt):
        raise PaperAdmissionError(
            "choice_receipt must be a ChoiceCapabilityReceipt"
        )
    if top_decile_result.schema_version != TOP_DECILE_LEDGER_VERSION:
        raise PaperAdmissionError("top-decile ledger version is unsupported")
    if (
        RESEARCH_CAPITAL != Decimal("1000000")
        or top_decile_result.research_capital != RESEARCH_CAPITAL
    ):
        raise PaperAdmissionError("top-decile research capital must remain 1000000")
    expected_top_decile_configuration = canonical_sha256(
        _configuration_payload()
    )
    if top_decile_result.configuration_sha256 != expected_top_decile_configuration:
        raise PaperAdmissionError("top-decile frozen configuration SHA-256 mismatch")
    if canonical_sha256(_top_decile_result_payload(top_decile_result)) != (
        top_decile_result.result_sha256
    ):
        raise PaperAdmissionError("top-decile ledger SHA-256 mismatch")
    if (
        backtest_result.research_scope != FORMAL_BACKTEST_SCOPE
        or backtest_result.formal_signal_binding is not True
    ):
        raise PaperAdmissionError(
            "diagnostic/raw Top2 backtests cannot enter historical admission"
        )
    if (
        backtest_result.controlled_execution_bar_adapter_verified
        is not CONTROLLED_EXECUTION_BAR_ADAPTER_VERIFIED
    ):
        raise PaperAdmissionError(
            "controlled execution-bar adapter state cannot be caller supplied"
        )
    expected_external_hash = canonical_sha256(
        [
            {
                "instrument_id": UNMANAGED_MIDEA_INSTRUMENT_ID,
                "quantity": 100,
                "ownership": "unmanaged_external",
            }
        ]
    )
    if backtest_result.unmanaged_external_sha256 != expected_external_hash:
        raise PaperAdmissionError(
            "formal Top2 must bind exactly unmanaged Midea 000333.SZ x100"
        )
    expected_backtest_hash = a_share_backtest_comparison_content_sha256(
        backtest_result
    )
    if expected_backtest_hash != backtest_result.backtest_sha256:
        raise PaperAdmissionError("Top2 backtest SHA-256 mismatch")
    for scenario in (backtest_result.base, backtest_result.stress):
        if abs(
            scenario.net_active_return
            - (scenario.net_return - scenario.benchmark_total_return)
        ) > Decimal("0.00000001"):
            raise PaperAdmissionError("Top2 active return does not reconcile")

    evaluation_hash = _evaluation_binding_sha256(evaluation_result)
    if (
        experiment_spec.spec_sha256
        != evaluation_result.experiment_spec_sha256
        or experiment_spec.experiment_id != evaluation_result.experiment_id
    ):
        raise PaperAdmissionError(
            "ExperimentSpecV2 is not bound to the supplied EvaluationResult"
        )
    experiment_content = experiment_spec.to_content_dict()
    target_contract = experiment_content["target"]
    benchmark_contract = experiment_content["benchmark"]
    if not isinstance(target_contract, Mapping) or not isinstance(
        benchmark_contract, Mapping
    ):
        raise PaperAdmissionError("experiment target/benchmark contract is malformed")
    if backtest_result.evaluation_sha256 != evaluation_hash:
        raise PaperAdmissionError(
            "formal Top2 is not bound to the supplied EvaluationResult"
        )
    if backtest_result.experiment_spec_sha256 != experiment_spec.spec_sha256:
        raise PaperAdmissionError(
            "formal Top2 is not bound to the supplied ExperimentSpecV2"
        )
    if (
        backtest_result.evaluation_source_bundle_sha256
        != evaluation_result.source_bundle_sha256
    ):
        raise PaperAdmissionError(
            "formal Top2 source bundle differs from EvaluationResult"
        )
    try:
        expected_signal_bindings = formal_signal_bindings_from_evaluation(
            evaluation_result
        )
    except AShareBacktestError as exc:
        raise PaperAdmissionError(
            f"formal Top2 ranking completeness failed: {exc}"
        ) from exc
    if backtest_result.formal_signal_bindings != expected_signal_bindings:
        raise PaperAdmissionError(
            "formal Top2 member/ranking receipts do not match EvaluationResult"
        )
    binding_dates = tuple(item.decision_date for item in expected_signal_bindings)
    if backtest_result.base.decision_dates != binding_dates:
        raise PaperAdmissionError(
            "formal Top2 decision dates do not match ranking receipts"
        )
    expected_calendar_hash = str(
        target_contract["trading_calendar_content_sha256"]
    )
    expected_benchmark_series_hash = str(
        benchmark_contract["total_return_series_content_sha256"]
    )
    if backtest_result.trading_calendar_sha256 != expected_calendar_hash:
        raise PaperAdmissionError(
            "formal Top2 calendar hash does not match ExperimentSpecV2"
        )
    if top_decile_result.trading_calendar_sha256 != expected_calendar_hash:
        raise PaperAdmissionError(
            "Top-Decile calendar hash does not match ExperimentSpecV2"
        )
    if backtest_result.benchmark_series_sha256 != expected_benchmark_series_hash:
        raise PaperAdmissionError(
            "formal Top2 benchmark series hash does not match ExperimentSpecV2"
        )
    if not backtest_result.base.nav:
        raise PaperAdmissionError("formal Top2 NAV cannot be empty")
    execution_dates = (
        (binding_dates[0],)
        + tuple(item.trading_date for item in backtest_result.base.nav)
    )
    try:
        execution_calendar_hash = trading_calendar_content_sha256(execution_dates)
    except ValueError as exc:
        raise PaperAdmissionError(
            "formal Top2 execution calendar is malformed"
        ) from exc
    if backtest_result.execution_calendar_sha256 != execution_calendar_hash:
        raise PaperAdmissionError(
            "formal Top2 execution calendar SHA-256 mismatch"
        )
    if (
        backtest_result.formal_window_start != backtest_result.base.start_date
        or backtest_result.formal_window_end != backtest_result.base.end_date
        or backtest_result.base.start_date != top_decile_result.base.start_date
        or backtest_result.base.end_date != top_decile_result.base.end_date
        or backtest_result.stress.start_date != top_decile_result.stress.start_date
        or backtest_result.stress.end_date != top_decile_result.stress.end_date
    ):
        raise PaperAdmissionError(
            "Top2 and Top-Decile formal execution windows differ"
        )
    if (
        backtest_result.base.benchmark_data_sha256
        != top_decile_result.benchmark_data_sha256
    ):
        raise PaperAdmissionError(
            "Top2 and Top-Decile execution benchmark content differs"
        )
    if top_decile_result.evaluation_sha256 != evaluation_hash:
        raise PaperAdmissionError(
            "top-decile ledger is not bound to the supplied evaluation"
        )
    if backtest_result.base.decision_dates != backtest_result.stress.decision_dates:
        raise PaperAdmissionError("base/stress Top2 decision dates differ")
    if top_decile_result.base.decision_dates != top_decile_result.stress.decision_dates:
        raise PaperAdmissionError("base/stress Top-Decile decision dates differ")
    if top_decile_result.decision_dates != top_decile_result.base.decision_dates:
        raise PaperAdmissionError("Top-Decile decision dates are internally inconsistent")
    if backtest_result.base.decision_dates != top_decile_result.decision_dates:
        raise PaperAdmissionError(
            "Top2 and Top-Decile must use the same frozen decision dates"
        )
    if (
        backtest_result.base.configuration_sha256
        != backtest_result.stress.configuration_sha256
    ):
        raise PaperAdmissionError("base/stress Top2 configuration differs")
    if (
        backtest_result.base.benchmark_id != backtest_result.stress.benchmark_id
        or backtest_result.base.benchmark_data_sha256
        != backtest_result.stress.benchmark_data_sha256
    ):
        raise PaperAdmissionError("base/stress Top2 benchmark differs")

    benchmark_item = next(
        item
        for item in choice_receipt.capabilities
        if item.capability is ChoiceCapability.TOTAL_RETURN_BENCHMARK
    )
    if len(benchmark_item.subject_ids) != 1:
        raise PaperAdmissionError("Choice benchmark must bind exactly one subject")
    benchmark_id = benchmark_item.subject_ids[0]
    if (
        backtest_result.base.benchmark_id != benchmark_id
        or top_decile_result.benchmark_id != benchmark_id
    ):
        raise PaperAdmissionError(
            "Top2 and Top-Decile benchmark must match the Choice receipt"
        )

    choice_gate = evaluate_choice_quality_growth_gate(choice_receipt)
    controlled_execution_bars = (
        CONTROLLED_EXECUTION_BAR_ADAPTER_VERIFIED is True
        and backtest_result.controlled_execution_bar_adapter_verified is True
    )
    controlled_top_decile_prices = (
        CONTROLLED_TOP_DECILE_PRICE_ADAPTER_VERIFIED is True
    )
    data_pit_complete = (
        choice_gate.status is ChoiceGateStatus.CONTRACT_SATISFIED
        and choice_gate.contract_satisfied
        and not choice_gate.missing_capabilities
        and not choice_gate.missing_fields
        and not choice_gate.policy_violations
        and choice_gate.formal_truth_eligibility is True
        and controlled_execution_bars
        and controlled_top_decile_prices
    )
    data_blockers: list[str] = []
    if not controlled_execution_bars:
        data_blockers.append("blocked_missing_controlled_stock_bar_bundle")
    if not controlled_top_decile_prices:
        data_blockers.append("blocked_missing_controlled_top_decile_price_bundle")
    rank_values = tuple(
        Decimal(str(item.rank_ic))
        for item in evaluation_result.rank_ic
        if item.model == "ridge_alpha_1"
        and item.split in {"validation", "locked_test", "audit"}
    )
    mean_rank_ic = (
        sum(rank_values, ZERO) / Decimal(len(rank_values))
        if rank_values
        else None
    )
    positive_fraction = (
        Decimal(sum(item > ZERO for item in rank_values))
        / Decimal(len(rank_values))
        if rank_values
        else None
    )
    rank_stable = (
        mean_rank_ic is not None
        and mean_rank_ic > ZERO
        and positive_fraction is not None
        and positive_fraction >= Decimal("0.5")
    )
    significant_factor_ids = {
        split: {
            item.factor_id
            for item in evaluation_result.factor_tests
            if item.split == split
            and item.status == "estimated"
            and item.coefficient is not None
            and item.coefficient > 0.0
            and item.holm_p_value is not None
            and item.holm_p_value <= 0.05
        }
        for split in ("locked_test", "audit")
    }
    corrected_factor_count = len(
        significant_factor_ids["locked_test"]
        & significant_factor_ids["audit"]
    )
    positive_half_years = sum(
        item.net_active_return > ZERO
        for item in top_decile_result.base.half_year_windows
    )
    worst_stress_active = min(
        top_decile_result.stress.net_active_return,
        backtest_result.stress.net_active_return,
    )
    max_drawdown = max(
        top_decile_result.base.max_drawdown,
        top_decile_result.stress.max_drawdown,
        backtest_result.base.max_drawdown,
        backtest_result.stress.max_drawdown,
    )
    turnover = max(
        top_decile_result.base.annualized_one_way_turnover,
        top_decile_result.stress.annualized_one_way_turnover,
        backtest_result.base.annualized_one_way_turnover,
        backtest_result.stress.annualized_one_way_turnover,
    )
    gates = (
        _computed_gate(
            "data_pit_complete",
            data_pit_complete,
            (
                f"{choice_gate.status.value};"
                f"formal_truth={choice_gate.formal_truth_eligibility};"
                f"bar_adapters={','.join(data_blockers) or 'verified'}"
            ),
            "contract_satisfied_and_formal_truth_and_controlled_price_adapters",
        ),
        _computed_gate(
            "top_decile_net_absolute_positive",
            top_decile_result.base.net_absolute_return > ZERO,
            top_decile_result.base.net_absolute_return,
            ">0",
        ),
        _computed_gate(
            "top_decile_net_active_positive",
            top_decile_result.base.net_active_return > ZERO,
            top_decile_result.base.net_active_return,
            ">0",
        ),
        _computed_gate(
            "top2_net_absolute_positive",
            backtest_result.base.net_return > ZERO,
            backtest_result.base.net_return,
            ">0",
        ),
        _computed_gate(
            "top2_net_active_positive",
            backtest_result.base.net_active_return > ZERO,
            backtest_result.base.net_active_return,
            ">0",
        ),
        _computed_gate(
            "oos_rank_ic_stable",
            rank_stable,
            f"mean={mean_rank_ic};positive_fraction={positive_fraction}",
            "mean>0;positive_fraction>=0.5",
        ),
        _computed_gate(
            "corrected_significant_factor_count_gte_2",
            corrected_factor_count >= 2,
            corrected_factor_count,
            ">=2",
        ),
        _computed_gate(
            "positive_semiannual_windows_gte_3_of_4",
            len(top_decile_result.base.half_year_windows) == 4
            and positive_half_years >= 3,
            f"{positive_half_years}/{len(top_decile_result.base.half_year_windows)}",
            ">=3/4",
        ),
        _computed_gate(
            "stress_active_return_non_negative",
            worst_stress_active >= ZERO,
            worst_stress_active,
            ">=0",
        ),
        _computed_gate(
            "max_drawdown_lte_12pct",
            max_drawdown <= Decimal("0.12"),
            max_drawdown,
            "<=0.12",
        ),
        _computed_gate(
            "annualized_one_way_turnover_lte_4",
            turnover <= Decimal("4"),
            turnover,
            "<=4",
        ),
    )
    if tuple(item.gate_id for item in gates) != REQUIRED_HISTORICAL_GATE_IDS:
        raise AssertionError("historical gate builder drifted from the frozen contract")
    seed = {
        "builder_version": HISTORICAL_GATE_BUILDER_VERSION,
        "backtest_sha256": backtest_result.backtest_sha256,
        "top_decile_result_sha256": top_decile_result.result_sha256,
        "evaluation_sha256": evaluation_hash,
        "experiment_spec_sha256": experiment_spec.spec_sha256,
        "choice_receipt_sha256": choice_receipt.receipt_sha256,
        "gates": gates,
    }
    return HistoricalGateResult(
        start_date=backtest_result.base.start_date,
        end_date=backtest_result.base.end_date,
        decision_dates=backtest_result.base.decision_dates,
        configuration_hashes=tuple(
            backtest_result.base.configuration_sha256
            for _ in backtest_result.base.decision_dates
        ),
        max_drawdown=max_drawdown,
        annualized_one_way_turnover=turnover,
        gate_results=gates,
        base_result_sha256=backtest_result.base.result_sha256,
        stress_result_sha256=backtest_result.stress.result_sha256,
        backtest_sha256=backtest_result.backtest_sha256,
        top_decile_result_sha256=top_decile_result.result_sha256,
        evaluation_sha256=evaluation_hash,
        choice_receipt_sha256=choice_receipt.receipt_sha256,
        aggregate_sha256=canonical_sha256(seed),
    )


@dataclass(frozen=True)
class PaperAdmissionCertificate:
    """Stage-A historical certificate; it authorizes only forward Paper."""

    certificate_id: str
    issued_at: datetime
    status: str
    data_sha256: str
    choice_receipt_sha256: str
    evaluation_sha256: str
    experiment_sha256: str
    code_sha256: str
    backtest_sha256: str
    top_decile_result_sha256: str
    historical_gate_sha256: str
    configuration_sha256: str
    base_result_sha256: str
    stress_result_sha256: str
    history_start: date
    history_end: date
    history_decision_point_count: int
    max_drawdown: Decimal
    annualized_one_way_turnover: Decimal
    certificate_type: str = "paper_admission_certificate"
    manual_execution_required: bool = True
    live_supported: bool = False
    execution_authority: str = "none"
    certificate_sha256: str = field(init=False)
    _issuer_token: InitVar[object] = None

    def __post_init__(self, _issuer_token: object) -> None:
        if _issuer_token is not _PAPER_CERTIFICATE_ISSUER_TOKEN:
            raise PaperAdmissionError(
                "PaperAdmissionCertificate can only be issued by the controlled Stage-A builder"
            )
        if not str(self.certificate_id).strip():
            raise PaperAdmissionError("certificate_id is required")
        if self.issued_at.tzinfo is None or self.issued_at.utcoffset() is None:
            raise PaperAdmissionError("issued_at must include a timezone")
        if self.status != PAPER_ADMITTED_STATUS:
            raise PaperAdmissionError(
                "historical certificate status is frozen to paper_admitted"
            )
        for name in (
            "data_sha256",
            "choice_receipt_sha256",
            "evaluation_sha256",
            "experiment_sha256",
            "code_sha256",
            "backtest_sha256",
            "top_decile_result_sha256",
            "historical_gate_sha256",
            "configuration_sha256",
            "base_result_sha256",
            "stress_result_sha256",
        ):
            object.__setattr__(self, name, _hash(getattr(self, name), name))
        if self.certificate_type != "paper_admission_certificate":
            raise PaperAdmissionError("certificate_type is frozen")
        if self.manual_execution_required is not True:
            raise PaperAdmissionError("manual execution cannot be disabled")
        if self.live_supported is not False or self.execution_authority != "none":
            raise PaperAdmissionError("LIVE and execution authority are unsupported")
        object.__setattr__(
            self,
            "certificate_sha256",
            canonical_sha256(self.to_content_dict()),
        )

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PAPER_ADMISSION_VERSION,
            "certificate_id": self.certificate_id,
            "issued_at": self.issued_at,
            "status": self.status,
            "certificate_type": self.certificate_type,
            "data_sha256": self.data_sha256,
            "choice_receipt_sha256": self.choice_receipt_sha256,
            "evaluation_sha256": self.evaluation_sha256,
            "experiment_sha256": self.experiment_sha256,
            "code_sha256": self.code_sha256,
            "backtest_sha256": self.backtest_sha256,
            "top_decile_result_sha256": self.top_decile_result_sha256,
            "historical_gate_sha256": self.historical_gate_sha256,
            "configuration_sha256": self.configuration_sha256,
            "base_result_sha256": self.base_result_sha256,
            "stress_result_sha256": self.stress_result_sha256,
            "history_start": self.history_start,
            "history_end": self.history_end,
            "history_decision_point_count": self.history_decision_point_count,
            "max_drawdown": self.max_drawdown,
            "annualized_one_way_turnover": self.annualized_one_way_turnover,
            "manual_execution_required": self.manual_execution_required,
            "live_supported": self.live_supported,
            "execution_authority": self.execution_authority,
        }

    def to_dict(self) -> dict[str, Any]:
        result = self.to_content_dict()
        result["certificate_sha256"] = self.certificate_sha256
        return result


@dataclass(frozen=True)
class PaperAdmissionDecision:
    status: str
    admitted: bool
    reasons: tuple[str, ...]
    certificate: PaperAdmissionCertificate | None
    live_supported: bool = False
    execution_authority: str = "none"

    def __post_init__(self) -> None:
        if self.live_supported is not False or self.execution_authority != "none":
            raise PaperAdmissionError("admission decisions can never authorize LIVE")
        if self.admitted:
            if self.status != PAPER_ADMITTED_STATUS or self.certificate is None:
                raise PaperAdmissionError("paper admission requires its certificate")
            if self.reasons:
                raise PaperAdmissionError(
                    "paper-admitted decisions cannot have reasons"
                )
        elif self.status != REJECTED_STATUS or self.certificate is not None:
            raise PaperAdmissionError("rejected decisions cannot carry a certificate")


@dataclass(frozen=True)
class PaperTrackRecord:
    """Forward-only Paper observations made after historical admission."""

    paper_certificate_sha256: str
    start_date: date
    end_date: date
    decision_dates: tuple[date, ...]
    controlled_trading_dates: tuple[date, ...]
    configuration_hashes: tuple[str, ...]
    max_drawdown: Decimal
    gate_results: tuple[PolicyGateResult, ...]
    track_record_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "paper_certificate_sha256",
            _hash(self.paper_certificate_sha256, "paper_certificate_sha256"),
        )
        decisions = tuple(self.decision_dates)
        calendar = tuple(self.controlled_trading_dates)
        if (
            not calendar
            or tuple(sorted(calendar)) != calendar
            or len(set(calendar)) != len(calendar)
        ):
            raise PaperAdmissionError(
                "controlled Paper calendar must be non-empty, unique, and chronological"
            )
        if self.start_date != calendar[0] or self.end_date != calendar[-1]:
            raise PaperAdmissionError(
                "Paper track window must equal the controlled calendar bounds"
            )
        calendar_index = {item: index for index, item in enumerate(calendar)}
        if any(item not in calendar_index for item in decisions):
            raise PaperAdmissionError(
                "Paper decisions must belong to the controlled trading calendar"
            )
        for previous, current in zip(decisions, decisions[1:]):
            if calendar_index[current] - calendar_index[previous] != 20:
                raise PaperAdmissionError(
                    "Paper decisions must be exactly 20 controlled sessions apart"
                )
        configs = tuple(
            _hash(item, "configuration_hash")
            for item in self.configuration_hashes
        )
        gates = tuple(self.gate_results)
        _validate_gate_shape(gates, "forward_paper")
        object.__setattr__(self, "decision_dates", decisions)
        object.__setattr__(self, "controlled_trading_dates", calendar)
        object.__setattr__(self, "configuration_hashes", configs)
        object.__setattr__(self, "gate_results", gates)
        object.__setattr__(
            self,
            "track_record_sha256",
            canonical_sha256(self.to_content_dict()),
        )

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PAPER_ADMISSION_VERSION,
            "paper_certificate_sha256": self.paper_certificate_sha256,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "decision_dates": self.decision_dates,
            "controlled_trading_dates": self.controlled_trading_dates,
            "configuration_hashes": self.configuration_hashes,
            "max_drawdown": self.max_drawdown,
            "gate_results": [
                {
                    "gate_id": item.gate_id,
                    "passed": item.passed,
                    "observed": item.observed,
                    "limit": item.limit,
                }
                for item in self.gate_results
            ],
        }


@dataclass(frozen=True)
class ManualCandidateDecision:
    status: str
    eligible: bool
    reasons: tuple[str, ...]
    paper_certificate_sha256: str
    paper_track_record_sha256: str
    paper_ledger_seal_sha256: str
    paper_ledger_file_sha256: str
    manual_execution_required: bool = True
    live_supported: bool = False
    execution_authority: str = "none"

    def __post_init__(self) -> None:
        if self.live_supported is not False or self.execution_authority != "none":
            raise PaperAdmissionError("manual candidates can never authorize LIVE")
        if self.manual_execution_required is not True:
            raise PaperAdmissionError("manual execution cannot be disabled")
        _hash(self.paper_ledger_seal_sha256, "paper_ledger_seal_sha256")
        _hash(self.paper_ledger_file_sha256, "paper_ledger_file_sha256")
        expected = (
            MANUAL_CANDIDATE_STATUS
            if self.eligible
            else PAPER_TRACK_REJECTED_STATUS
        )
        if self.status != expected:
            raise PaperAdmissionError(
                "manual candidate decision status is inconsistent"
            )
        if self.eligible == bool(self.reasons):
            raise PaperAdmissionError("candidate reasons are inconsistent")


def evaluate_paper_admission(
    *,
    backtest_result: AShareBacktestComparison,
    top_decile_result: TopDecileCostLedgerResult,
    choice_receipt: ChoiceCapabilityReceipt,
    evaluation_result: EvaluationResult,
    experiment_spec: ExperimentSpecV2,
    experiment_sha256: str,
    code_sha256: str,
    requested_mode: str = "PAPER",
) -> PaperAdmissionDecision:
    """Stage A: derive exact historical gates and, only if green, admit Paper."""

    _require_paper_mode(requested_mode)
    history = build_historical_gate_result(
        backtest_result=backtest_result,
        top_decile_result=top_decile_result,
        evaluation_result=evaluation_result,
        experiment_spec=experiment_spec,
        choice_receipt=choice_receipt,
    )
    data_hash = _hash(choice_receipt.receipt_sha256, "choice receipt SHA-256")
    evaluation_hash = _evaluation_binding_sha256(evaluation_result)
    experiment_hash = _hash(experiment_sha256, "experiment_sha256")
    if (
        experiment_hash != evaluation_result.experiment_spec_sha256
        or experiment_hash != experiment_spec.spec_sha256
    ):
        raise PaperAdmissionError(
            "experiment_sha256 must equal the formal EvaluationResult and ExperimentSpecV2 hash"
        )
    code_hash = _hash(code_sha256, "code_sha256")
    backtest_hash = _hash(history.backtest_sha256, "backtest_sha256")
    base_hash = _hash(history.base_result_sha256, "base_result_sha256")
    stress_hash = _hash(history.stress_result_sha256, "stress_result_sha256")
    if history.end_date < history.start_date:
        raise PaperAdmissionError("history dates are reversed")
    # Certificate issue time is generated here, not supplied by the caller.
    # Otherwise an old timestamp could make a backfilled Paper record appear
    # to have started after admission.
    timestamp = datetime.now(timezone.utc)
    if history.end_date > timestamp.astimezone(CHINA_STANDARD_TIME).date():
        raise PaperAdmissionError(
            "historical window ends after the controlled issue time"
        )
    decisions = tuple(history.decision_dates)
    if (
        tuple(sorted(decisions)) != decisions
        or len(set(decisions)) != len(decisions)
    ):
        raise PaperAdmissionError(
            "historical decision dates must be unique and chronological"
        )
    if (
        not decisions
        or decisions[0] >= history.start_date
        or any(item < history.start_date for item in decisions[1:])
        or any(item > history.end_date for item in decisions)
    ):
        raise PaperAdmissionError(
            "only the first close signal may precede the next-open history window"
        )
    config_hashes = tuple(
        _hash(item, "configuration_hash")
        for item in history.configuration_hashes
    )
    if len(config_hashes) != len(decisions):
        raise PaperAdmissionError(
            "each historical decision must bind one configuration hash"
        )
    _validate_gate_shape(history.gate_results, "historical")

    reasons: list[str] = []
    if history.backtest_sha256 != backtest_result.backtest_sha256:
        reasons.append("controlled_backtest_hash_mismatch")
    if history.base_result_sha256 != backtest_result.base.result_sha256:
        reasons.append("controlled_base_result_hash_mismatch")
    if history.stress_result_sha256 != backtest_result.stress.result_sha256:
        reasons.append("controlled_stress_result_hash_mismatch")
    if decisions != backtest_result.base.decision_dates:
        reasons.append("controlled_backtest_decision_dates_mismatch")
    if backtest_result.base.decision_dates != backtest_result.stress.decision_dates:
        reasons.append("base_stress_decision_dates_mismatch")
    if (
        history.start_date != backtest_result.base.start_date
        or history.end_date != backtest_result.base.end_date
        or backtest_result.base.start_date != backtest_result.stress.start_date
        or backtest_result.base.end_date != backtest_result.stress.end_date
    ):
        reasons.append("controlled_backtest_history_window_mismatch")
    if set(config_hashes) != {backtest_result.base.configuration_sha256} or (
        backtest_result.base.configuration_sha256
        != backtest_result.stress.configuration_sha256
    ):
        reasons.append("controlled_backtest_configuration_hash_mismatch")
    if max(
        backtest_result.base.max_drawdown,
        backtest_result.stress.max_drawdown,
        top_decile_result.base.max_drawdown,
        top_decile_result.stress.max_drawdown,
    ) != Decimal(str(history.max_drawdown)):
        reasons.append("controlled_ledgers_drawdown_mismatch")
    if max(
        backtest_result.base.annualized_one_way_turnover,
        backtest_result.stress.annualized_one_way_turnover,
        top_decile_result.base.annualized_one_way_turnover,
        top_decile_result.stress.annualized_one_way_turnover,
    ) != Decimal(str(history.annualized_one_way_turnover)):
        reasons.append("controlled_ledgers_turnover_mismatch")
    if _completed_months(history.start_date, history.end_date) < 12:
        reasons.append("historical_window_shorter_than_12_completed_months")
    if len(decisions) < 12:
        reasons.append("fewer_than_12_historical_decision_points")
    if len(set(config_hashes)) != 1:
        reasons.append("historical_configuration_hash_changed")
    max_drawdown = Decimal(str(history.max_drawdown))
    turnover = Decimal(str(history.annualized_one_way_turnover))
    if max_drawdown < ZERO or turnover < ZERO:
        raise PaperAdmissionError("historical risk metrics must be non-negative")
    if max_drawdown > Decimal("0.12"):
        reasons.append("historical_max_drawdown_above_12pct")
    if turnover > Decimal("4"):
        reasons.append("historical_annualized_one_way_turnover_above_4")
    reasons.extend(
        _exact_gate_reasons(
            history.gate_results,
            REQUIRED_HISTORICAL_GATE_IDS,
            "historical",
        )
    )
    reasons = list(dict.fromkeys(reasons))
    if reasons:
        return PaperAdmissionDecision(
            REJECTED_STATUS, False, tuple(reasons), None
        )

    configuration_hash = config_hashes[0]
    seed: Mapping[str, Any] = {
        "version": PAPER_ADMISSION_VERSION,
        "data_sha256": data_hash,
        "choice_receipt_sha256": data_hash,
        "evaluation_sha256": evaluation_hash,
        "experiment_sha256": experiment_hash,
        "code_sha256": code_hash,
        "backtest_sha256": backtest_hash,
        "top_decile_result_sha256": history.top_decile_result_sha256,
        "historical_gate_sha256": history.aggregate_sha256,
        "configuration_sha256": configuration_hash,
        "issued_at": timestamp,
    }
    certificate = PaperAdmissionCertificate(
        certificate_id=f"paper-admission-{canonical_sha256(seed)[:24]}",
        issued_at=timestamp,
        status=PAPER_ADMITTED_STATUS,
        data_sha256=data_hash,
        choice_receipt_sha256=data_hash,
        evaluation_sha256=evaluation_hash,
        experiment_sha256=experiment_hash,
        code_sha256=code_hash,
        backtest_sha256=backtest_hash,
        top_decile_result_sha256=history.top_decile_result_sha256,
        historical_gate_sha256=history.aggregate_sha256,
        configuration_sha256=configuration_hash,
        base_result_sha256=base_hash,
        stress_result_sha256=stress_hash,
        history_start=history.start_date,
        history_end=history.end_date,
        history_decision_point_count=len(decisions),
        max_drawdown=max_drawdown,
        annualized_one_way_turnover=turnover,
        _issuer_token=_PAPER_CERTIFICATE_ISSUER_TOKEN,
    )
    return PaperAdmissionDecision(
        PAPER_ADMITTED_STATUS, True, (), certificate
    )


def verify_paper_admission_certificate(
    certificate: PaperAdmissionCertificate,
    *,
    data_sha256: str,
    choice_receipt_sha256: str,
    evaluation_sha256: str,
    experiment_sha256: str,
    code_sha256: str,
    backtest_sha256: str,
    top_decile_result_sha256: str,
    historical_gate_sha256: str,
) -> None:
    """Fail closed if a historical Paper certificate is stale or altered."""

    if not isinstance(certificate, PaperAdmissionCertificate):
        raise PaperAdmissionError("certificate type is invalid")
    if (
        canonical_sha256(certificate.to_content_dict())
        != certificate.certificate_sha256
    ):
        raise PaperAdmissionError("certificate SHA-256 mismatch")
    expected = {
        "data_sha256": _hash(data_sha256, "data_sha256"),
        "choice_receipt_sha256": _hash(
            choice_receipt_sha256, "choice_receipt_sha256"
        ),
        "evaluation_sha256": _hash(evaluation_sha256, "evaluation_sha256"),
        "experiment_sha256": _hash(experiment_sha256, "experiment_sha256"),
        "code_sha256": _hash(code_sha256, "code_sha256"),
        "backtest_sha256": _hash(backtest_sha256, "backtest_sha256"),
        "top_decile_result_sha256": _hash(
            top_decile_result_sha256, "top_decile_result_sha256"
        ),
        "historical_gate_sha256": _hash(
            historical_gate_sha256, "historical_gate_sha256"
        ),
    }
    for name, value in expected.items():
        if getattr(certificate, name) != value:
            raise PaperAdmissionError(f"certificate {name} mismatch")
    if (
        certificate.status != PAPER_ADMITTED_STATUS
        or certificate.certificate_type != "paper_admission_certificate"
        or certificate.live_supported is not False
        or certificate.manual_execution_required is not True
        or certificate.execution_authority != "none"
    ):
        raise PaperAdmissionError("certificate safety boundary was altered")


def evaluate_manual_real_money_candidate(
    certificate: PaperAdmissionCertificate,
    paper_ledger_path: str | Path,
    *,
    as_of: datetime,
    requested_mode: str = "PAPER",
) -> ManualCandidateDecision:
    """Stage B: re-verify a sealed ledger and evaluate its forward record."""

    _require_paper_mode(requested_mode)
    if not isinstance(certificate, PaperAdmissionCertificate):
        raise PaperAdmissionError("certificate type is invalid")
    if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
        raise PaperAdmissionError("as_of must be a timezone-aware datetime")
    if not isinstance(paper_ledger_path, (str, Path)):
        raise PaperAdmissionError(
            "Stage B requires a controlled Paper ledger path, not a caller-built track record"
        )
    from .paper_ledger import PaperLedgerError, derive_paper_track_record

    try:
        summary = derive_paper_track_record(
            paper_ledger_path,
            certificate,
            as_of=as_of,
        )
    except PaperLedgerError as exc:
        raise PaperAdmissionError(f"controlled Paper ledger verification failed: {exc}") from exc
    track_record = summary.track_record
    if not isinstance(track_record, PaperTrackRecord):
        raise PaperAdmissionError("controlled ledger returned an invalid PaperTrackRecord")
    if certificate.issued_at > as_of:
        raise PaperAdmissionError("certificate was issued after evaluation as_of")
    if track_record.end_date > as_of.date() or any(
        item > as_of.date() for item in track_record.decision_dates
    ):
        raise PaperAdmissionError("Paper track record contains future dates")
    if any(item > as_of.date() for item in track_record.controlled_trading_dates):
        raise PaperAdmissionError("Paper controlled calendar contains future dates")
    if (
        canonical_sha256(certificate.to_content_dict())
        != certificate.certificate_sha256
    ):
        raise PaperAdmissionError("certificate SHA-256 mismatch")
    if (
        canonical_sha256(track_record.to_content_dict())
        != track_record.track_record_sha256
    ):
        raise PaperAdmissionError("Paper track record SHA-256 mismatch")

    reasons: list[str] = list(summary.reasons)
    if not summary.complete:
        reasons.append("controlled_paper_ledger_incomplete")
    # The append-only ledger proves internal chronology and accounting, but
    # its per-decision signal/model/source hashes are not yet emitted by a
    # standard, source-authenticated forward scoring adapter.  A caller can
    # therefore not use self-reported hashes to unlock real-money candidacy.
    reasons.append("blocked_missing_controlled_paper_signal_adapter")
    # Decision-point NAV alone can miss an intervening 12% drawdown.  Until
    # daily PIT marks and sticky freeze/exit retry events are replayed, the
    # forward risk gate is not sufficient for real-money candidacy.
    reasons.append("blocked_missing_daily_paper_risk_marks")
    if track_record.paper_certificate_sha256 != certificate.certificate_sha256:
        reasons.append("paper_track_certificate_mismatch")
    if track_record.start_date <= certificate.issued_at.date():
        reasons.append("paper_track_starts_before_admission_certificate")
    if track_record.end_date < track_record.start_date:
        raise PaperAdmissionError("Paper track dates are reversed")
    decisions = track_record.decision_dates
    if (
        tuple(sorted(decisions)) != decisions
        or len(set(decisions)) != len(decisions)
    ):
        raise PaperAdmissionError(
            "Paper decision dates must be unique and chronological"
        )
    if any(
        item < track_record.start_date or item > track_record.end_date
        for item in decisions
    ):
        raise PaperAdmissionError(
            "Paper decisions must lie inside the track window"
        )
    if len(track_record.configuration_hashes) != len(decisions):
        raise PaperAdmissionError(
            "each Paper decision must bind one configuration hash"
        )
    if _completed_months(track_record.start_date, track_record.end_date) < 12:
        reasons.append("forward_paper_shorter_than_12_completed_months")
    if len(decisions) < 12:
        reasons.append("fewer_than_12_forward_paper_decision_points")
    if len({(item.year, item.month) for item in decisions}) < 12:
        reasons.append("fewer_than_12_distinct_forward_paper_months")
    unique_configs = set(track_record.configuration_hashes)
    if unique_configs != {certificate.configuration_sha256}:
        reasons.append("forward_paper_configuration_changed_or_mismatched")
    max_drawdown = Decimal(str(track_record.max_drawdown))
    if max_drawdown < ZERO:
        raise PaperAdmissionError("Paper max_drawdown must be non-negative")
    if max_drawdown >= Decimal("0.12"):
        reasons.append("forward_paper_max_drawdown_not_below_12pct")
    reasons.extend(
        _exact_gate_reasons(
            track_record.gate_results,
            REQUIRED_FORWARD_PAPER_GATE_IDS,
            "forward_paper",
        )
    )
    reasons = list(dict.fromkeys(reasons))
    if reasons:
        return ManualCandidateDecision(
            PAPER_TRACK_REJECTED_STATUS,
            False,
            tuple(reasons),
            certificate.certificate_sha256,
            track_record.track_record_sha256,
            summary.seal_sha256,
            summary.ledger_file_sha256,
        )
    return ManualCandidateDecision(
        MANUAL_CANDIDATE_STATUS,
        True,
        (),
        certificate.certificate_sha256,
        track_record.track_record_sha256,
        summary.seal_sha256,
        summary.ledger_file_sha256,
    )


__all__ = [
    "CONTROLLED_TOP_DECILE_PRICE_ADAPTER_VERIFIED",
    "LIVE_NOT_SUPPORTED_CODE",
    "HISTORICAL_GATE_BUILDER_VERSION",
    "MANUAL_CANDIDATE_STATUS",
    "PAPER_ADMISSION_VERSION",
    "PAPER_ADMITTED_STATUS",
    "PAPER_TRACK_REJECTED_STATUS",
    "REJECTED_STATUS",
    "REQUIRED_FORWARD_PAPER_GATE_IDS",
    "REQUIRED_HISTORICAL_GATE_IDS",
    "LiveNotSupportedError",
    "ManualCandidateDecision",
    "PaperAdmissionCertificate",
    "PaperAdmissionDecision",
    "PaperAdmissionError",
    "PaperTrackRecord",
    "build_historical_gate_result",
    "evaluate_manual_real_money_candidate",
    "evaluate_paper_admission",
    "verify_paper_admission_certificate",
]
