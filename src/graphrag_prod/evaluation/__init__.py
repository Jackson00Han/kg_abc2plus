"""Versioned datasets, metrics, reports, and regression gates."""

from .answers import (
    GroundedAnswerMetrics,
    answer_gate_failures,
    calculate_answer_metrics,
    evaluate_answer_results,
    validate_gold_dataset,
)
from .datasets import load_gold_dataset
from .metrics import (
    evaluate_graph_results,
    evaluate_operational_observations,
    nearest_rank_percentile,
)
from .runner import build_evaluation_report

__all__ = [
    "GroundedAnswerMetrics",
    "answer_gate_failures",
    "calculate_answer_metrics",
    "evaluate_answer_results",
    "validate_gold_dataset",
    "build_evaluation_report",
    "evaluate_graph_results",
    "evaluate_operational_observations",
    "load_gold_dataset",
    "nearest_rank_percentile",
]
