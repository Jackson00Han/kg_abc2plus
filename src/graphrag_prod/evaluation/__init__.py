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
from .production import (
    PRODUCTION_OBSERVATION_SCHEMA_VERSION,
    PRODUCTION_REPORT_SCHEMA_VERSION,
    build_production_candidate_report,
)
from .production_config import (
    PRODUCTION_ANSWER_RETRIEVAL_LIMITS,
    PRODUCTION_REFERENCE_CONFIG_SCHEMA_VERSION,
    PRODUCTION_REFERENCE_CONFIG_VERSION,
    resolve_production_answer_retrieval_limits,
)
from .runner import build_evaluation_report

__all__ = [
    "GroundedAnswerMetrics",
    "answer_gate_failures",
    "calculate_answer_metrics",
    "evaluate_answer_results",
    "validate_gold_dataset",
    "build_evaluation_report",
    "build_production_candidate_report",
    "evaluate_graph_results",
    "evaluate_operational_observations",
    "load_gold_dataset",
    "nearest_rank_percentile",
    "PRODUCTION_OBSERVATION_SCHEMA_VERSION",
    "PRODUCTION_ANSWER_RETRIEVAL_LIMITS",
    "PRODUCTION_REFERENCE_CONFIG_SCHEMA_VERSION",
    "PRODUCTION_REFERENCE_CONFIG_VERSION",
    "PRODUCTION_REPORT_SCHEMA_VERSION",
    "resolve_production_answer_retrieval_limits",
]
