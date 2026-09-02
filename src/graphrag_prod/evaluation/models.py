"""Immutable result models for the unified evaluation workflow."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class GraphEvaluationMetrics:
    item_count: int
    entity_precision: float
    relationship_precision: float
    entity_resolution_accuracy: float
    case_outcome_accuracy: float

    def as_dict(self) -> dict[str, int | float]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class OperationalMetrics:
    ingestion_success_rate: float
    idempotency_mismatch_count: int
    deletion_residue_count: int
    recovery_success_rate: float
    retrieval_p95_ms: float
    answer_p95_ms: float
    retrieval_throughput_rps: float
    server_error_rate: float
    model_call_count: int
    input_token_count: int
    output_token_count: int
    estimated_cost_usd: float
    mean_answer_cost_usd: float
    answer_sample_count: int
    retrieval_sample_count: int

    def as_dict(self) -> dict[str, int | float]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class GoldDataset:
    manifest: dict[str, Any]
    questions: tuple[dict[str, Any], ...]
    answers: tuple[dict[str, Any], ...]
    chunks: tuple[dict[str, Any], ...]
    graph_items: tuple[dict[str, Any], ...]
    conflict_answers: tuple[dict[str, Any], ...]
    conflict_sources: tuple[dict[str, Any], ...]
    repository_root: str

    @property
    def all_answer_items(self) -> tuple[dict[str, Any], ...]:
        return self.answers + self.conflict_answers
