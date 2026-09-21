"""Bounded, permission-safe production retrieval."""

from .engine import (
    Neo4jRetrievalEngine,
    RetrievalBackendError,
    RetrievalBackendTimeout,
    RetrievalBackendUnavailable,
    RetrievalUnavailable,
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
    "reciprocal_rank_fusion",
    "resource_allocation_score",
    "select_context",
    "stable_deduplicate",
]
