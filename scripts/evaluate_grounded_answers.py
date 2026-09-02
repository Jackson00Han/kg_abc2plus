#!/usr/bin/env python3
"""CLI compatibility wrapper for grounded-answer evaluation."""

from graphrag_prod.evaluation.answers import (
    CITATION_LOCATION_FIELDS,
    STANDARD_REFUSAL_ANSWER,
    GroundedAnswerMetrics,
    answer_gate_failures,
    calculate_answer_metrics,
    evaluate_answer_results,
    load_jsonl,
    main,
    parse_args,
    validate_gold_dataset,
)

__all__ = [
    "CITATION_LOCATION_FIELDS",
    "STANDARD_REFUSAL_ANSWER",
    "GroundedAnswerMetrics",
    "answer_gate_failures",
    "calculate_answer_metrics",
    "evaluate_answer_results",
    "load_jsonl",
    "main",
    "parse_args",
    "validate_gold_dataset",
]


if __name__ == "__main__":
    raise SystemExit(main())
