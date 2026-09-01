"""Bounded, permission-safe production retrieval."""

from .engine import Neo4jRetrievalEngine, RetrievalUnavailable
from .metrics import RetrievalMetrics, evaluate_retrieval_dataset
from .models import (
    Citation,
    RetrievalLimits,
    RetrievalRequest,
    RetrievalResult,
    RetrievalTrace,
    RetrievedChunk,
    VersionFilter,
)
from .ranking import (
    ContextSelection,
    reciprocal_rank_fusion,
    resource_allocation_score,
    select_context,
    stable_deduplicate,
)

__all__ = [
    "Citation",
    "ContextSelection",
    "Neo4jRetrievalEngine",
    "RetrievalLimits",
    "RetrievalMetrics",
    "RetrievalRequest",
    "RetrievalResult",
    "RetrievalTrace",
    "RetrievalUnavailable",
    "RetrievedChunk",
    "VersionFilter",
    "evaluate_retrieval_dataset",
    "reciprocal_rank_fusion",
    "resource_allocation_score",
    "select_context",
    "stable_deduplicate",
]
