"""Factor-first, research-only quantitative strategy workspace."""

from .attribution import AttributionReport, build_attribution
from .backtest import (
    BacktestConfig,
    BacktestResult,
    BenchmarkClose,
    CostModel,
    DailyClose,
    FrozenSignal,
    run_backtest,
)
from .catalog import DEFAULT_CATALOG_SHA256, DEFAULT_FACTOR_CATALOG
from .choice_gate import ChoiceCapabilityReceipt, evaluate_choice_quality_growth_gate
from .contracts import DiscoveryPlan, FactorDefinition, ThesisSpec
from .discovery import freeze_plan, generate_candidates
from .evaluation import EvaluationResult, PitCrossSection, evaluate_pit_panel
from .experiment import ExperimentSpecV2, read_experiment_spec, write_new_experiment_spec
from .quality_growth import (
    QuarterlyFundamental,
    QualityGrowthSnapshot,
    compute_quality_growth_snapshot,
)
from .preprocessing import residualize_cross_section
from .regression import fit_ols, fit_ridge, fama_macbeth
from .top_decile_backtest import TopDecileCostLedgerResult, run_top_decile_cost_ledger
from .a_share_backtest import (
    AShareBacktestComparison,
    derive_formal_a_share_top2_close_signals,
    run_a_share_top2_backtest,
    run_formal_a_share_top2_backtest,
)
from .admission import (
    build_historical_gate_result,
    evaluate_manual_real_money_candidate,
    evaluate_paper_admission,
)
from .paper_ledger import (
    append_paper_decision,
    create_or_verify_paper_ledger,
    derive_paper_track_record,
    seal_paper_ledger,
    verify_paper_ledger,
)


__all__ = [
    "AttributionReport",
    "AShareBacktestComparison",
    "BacktestConfig",
    "BacktestResult",
    "BenchmarkClose",
    "CostModel",
    "ChoiceCapabilityReceipt",
    "DEFAULT_CATALOG_SHA256",
    "DEFAULT_FACTOR_CATALOG",
    "DailyClose",
    "DiscoveryPlan",
    "FactorDefinition",
    "FrozenSignal",
    "EvaluationResult",
    "ExperimentSpecV2",
    "PitCrossSection",
    "QuarterlyFundamental",
    "QualityGrowthSnapshot",
    "ThesisSpec",
    "TopDecileCostLedgerResult",
    "append_paper_decision",
    "build_attribution",
    "build_historical_gate_result",
    "compute_quality_growth_snapshot",
    "create_or_verify_paper_ledger",
    "derive_formal_a_share_top2_close_signals",
    "derive_paper_track_record",
    "evaluate_choice_quality_growth_gate",
    "evaluate_manual_real_money_candidate",
    "evaluate_paper_admission",
    "evaluate_pit_panel",
    "fama_macbeth",
    "fit_ols",
    "fit_ridge",
    "freeze_plan",
    "generate_candidates",
    "read_experiment_spec",
    "residualize_cross_section",
    "run_backtest",
    "run_a_share_top2_backtest",
    "run_formal_a_share_top2_backtest",
    "run_top_decile_cost_ledger",
    "seal_paper_ledger",
    "verify_paper_ledger",
    "write_new_experiment_spec",
]
