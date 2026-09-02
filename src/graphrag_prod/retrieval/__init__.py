"""Bounded, permission-safe production retrieval."""

from .engine import (
    Neo4jRetrievalEngine,
    RetrievalBackendError,
    RetrievalBackendTimeout,
    RetrievalBackendUnavailable,
    RetrievalUnavailable,
)
from .metrics import (
    RetrievalMetrics,
    evaluate_retrieval_dataset,
    evaluate_retrieval_results,
)
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
    "RetrievalBackendError",
    "RetrievalBackendTimeout",
    "RetrievalBackendUnavailable",
    "RetrievalLimits",
    "RetrievalMetrics",
    "RetrievalRequest",
    "RetrievalResult",
    "RetrievalTrace",
    "RetrievalUnavailable",
    "RetrievedChunk",
    "VersionFilter",
    "evaluate_retrieval_dataset",
    "evaluate_retrieval_results",
    "reciprocal_rank_fusion",
    "resource_allocation_score",
    "select_context",
    "stable_deduplicate",
]
