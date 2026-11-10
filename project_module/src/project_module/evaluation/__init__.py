"""Statistics, gate, summaries and figures for experiment runs."""

from project_module.evaluation.figures import plot_pareto, plot_session_accuracy
from project_module.evaluation.gate import GateConfig, GateResult, evaluate_gate
from project_module.evaluation.load import RunData, analyzable_streams, load_run
from project_module.evaluation.stats import (
    CI,
    compare_arms,
    cost_per_stream_arm,
    paired_bootstrap,
    per_stream_score,
    plan_main_scale,
)
from project_module.evaluation.summary import mechanical_summary, summarize_run

__all__ = [
    "CI",
    "GateConfig",
    "GateResult",
    "RunData",
    "analyzable_streams",
    "compare_arms",
    "cost_per_stream_arm",
    "evaluate_gate",
    "load_run",
    "mechanical_summary",
    "paired_bootstrap",
    "per_stream_score",
    "plan_main_scale",
    "plot_pareto",
    "plot_session_accuracy",
    "summarize_run",
]
