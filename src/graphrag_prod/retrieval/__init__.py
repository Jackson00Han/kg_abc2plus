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
from .subgraph import (
    EvidenceSubgraph,
    EvidenceSubgraphLimits,
    Neo4jEvidenceSubgraphProjector,
    SubgraphAssertion,
    SubgraphCitation,
    SubgraphEntityNode,
    SubgraphEvidence,
    SubgraphPath,
    SubgraphProjectionError,
    SubgraphProvenance,
    SubgraphTrustPolicy,
)

__all__ = [
    "Citation",
    "ContextSelection",
    "EvidenceSubgraph",
    "EvidenceSubgraphLimits",
    "Neo4jRetrievalEngine",
    "Neo4jEvidenceSubgraphProjector",
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
    "SubgraphAssertion",
    "SubgraphCitation",
    "SubgraphEntityNode",
    "SubgraphEvidence",
    "SubgraphPath",
    "SubgraphProjectionError",
    "SubgraphProvenance",
    "SubgraphTrustPolicy",
    "VersionFilter",
    "evaluate_retrieval_dataset",
    "evaluate_retrieval_results",
    "reciprocal_rank_fusion",
    "resource_allocation_score",
    "select_context",
    "stable_deduplicate",
]
