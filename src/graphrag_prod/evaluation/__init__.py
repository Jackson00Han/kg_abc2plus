"""Versioned datasets, metrics, reports, and regression gates."""

from .answers import (
    GroundedAnswerMetrics,
    answer_gate_failures,
    calculate_answer_metrics,
    evaluate_answer_results,
    validate_gold_dataset,
)
from .datasets import load_gold_dataset
from .knowledge_quality import (
    KNOWLEDGE_BASELINE_SCHEMA_VERSION,
    KNOWLEDGE_GATE_POLICY_SCHEMA_VERSION,
    KNOWLEDGE_GOLD_SCHEMA_VERSION,
    KNOWLEDGE_PREDICTION_SCHEMA_VERSION,
    KNOWLEDGE_REPORT_SCHEMA_VERSION,
    build_knowledge_quality_report,
    knowledge_baseline_candidate,
)
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
    "KNOWLEDGE_BASELINE_SCHEMA_VERSION",
    "KNOWLEDGE_GATE_POLICY_SCHEMA_VERSION",
    "KNOWLEDGE_GOLD_SCHEMA_VERSION",
    "KNOWLEDGE_PREDICTION_SCHEMA_VERSION",
    "KNOWLEDGE_REPORT_SCHEMA_VERSION",
    "PRODUCTION_ANSWER_RETRIEVAL_LIMITS",
    "PRODUCTION_OBSERVATION_SCHEMA_VERSION",
    "PRODUCTION_REFERENCE_CONFIG_SCHEMA_VERSION",
    "PRODUCTION_REFERENCE_CONFIG_VERSION",
    "PRODUCTION_REPORT_SCHEMA_VERSION",
    "GroundedAnswerMetrics",
    "answer_gate_failures",
    "build_evaluation_report",
    "build_knowledge_quality_report",
    "build_production_candidate_report",
    "calculate_answer_metrics",
    "evaluate_answer_results",
    "evaluate_graph_results",
    "evaluate_operational_observations",
    "knowledge_baseline_candidate",
    "load_gold_dataset",
    "nearest_rank_percentile",
    "resolve_production_answer_retrieval_limits",
    "validate_gold_dataset",
]
