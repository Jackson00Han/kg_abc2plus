"""Neo4j retrieval engine with query-time authorization on every path."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, fields, replace
from datetime import datetime
import hashlib
import json
import math
import re
from typing import Any

from neo4j import unit_of_work
from neo4j.exceptions import (
    ConnectionAcquisitionTimeoutError,
    DriverError,
    Neo4jError,
    ServiceUnavailable,
    SessionExpired,
    TransientError,
)

from graphrag_prod.domain import retrieval_scope_token

from .models import (
    Citation,
    RetrievalRequest,
    RetrievalResult,
    RetrievalTrace,
    RetrievedChunk,
    TraceDecision,
    TraceHit,
)
from .ranking import (
    reciprocal_rank_fusion,
    resource_allocation_score,
    select_context,
    stable_deduplicate,
)
from .reranking import (
    CandidateReranker, MAX_RERANK_CANDIDATES, RerankCandidate,
    RerankResponse, RerankTrace, RerankingError,
)


FULLTEXT_INDEX_NAME = "graphrag_chunk_text_v2"
METHOD = "vector cosine + BM25 + RRF(k=60) + Resource Allocation"
_LUCENE_TERM = re.compile(r"[^\W_]+", re.UNICODE)
_TRANSACTION_TIMEOUT_CODES = frozenset(
    {
        "Neo.ClientError.Transaction.TransactionTimedOut",
        "Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration",
        "Neo.TransientError.Transaction.TransactionTimedOut",
        "Neo.TransientError.Transaction.TransactionTimedOutClientConfiguration",
        "Neo.ClientError.Transaction.LockAcquisitionTimeout",
        "Neo.TransientError.Transaction.LockAcquisitionTimeout",
    }
)
_CORPUS_STATE_ATTEMPTS = 2


class RetrievalUnavailable(RuntimeError):
    """The tenant has no compatible, active retrieval state."""


class _CorpusStateChanged(RuntimeError):
    """Discard a read whose active corpus identity changed between statements."""


class RetrievalBackendError(RuntimeError):
    """A sanitized, deterministic Neo4j retrieval failure."""

    def __init__(self) -> None:
        super().__init__("the retrieval store could not complete the query")


class RetrievalBackendUnavailable(RetrievalBackendError):
    """The retrieval store is temporarily unavailable."""


class RetrievalBackendTimeout(RetrievalBackendError):
    """The retrieval store exceeded a configured deadline."""


class RerankAttemptedFailure(RetrievalBackendError):
    """A model call may already have been charged; callers must not auto-retry."""

    def __init__(self, *, timeout: bool = False) -> None:
        self.timeout = timeout
        RuntimeError.__init__(self, "candidate reranking failed after a provider attempt")


@dataclass(frozen=True, slots=True)
class _PreparedRerank:
    request: RetrievalRequest
    result: RetrievalResult
    candidates: tuple[RerankCandidate, ...]


CORPUS_STATE_QUERY = """
MATCH (state:TenantCorpusState {tenant_id: $tenant_id})
OPTIONAL MATCH (state)-[:ACTIVE_EMBEDDING_INDEX]->(
    generation:EmbeddingIndexGeneration {tenant_id: $tenant_id, state: 'ACTIVE'}
)
WHERE generation.corpus_revision = state.corpus_revision
OPTIONAL MATCH (publication_state:KnowledgePublicationState {tenant_id: $tenant_id})
OPTIONAL MATCH (publication_state)-[:ACTIVE_KNOWLEDGE_PUBLICATION]->(
    publication:KnowledgePublication {tenant_id: $tenant_id, status: 'ACTIVE'}
)
RETURN state.corpus_revision AS corpus_revision,
       generation.generation_id AS generation_id,
       generation.embedding_space_id AS embedding_space_id,
       generation.dimensions AS dimensions,
       publication.publication_id AS knowledge_publication_id,
       coalesce(publication_state.activation_generation, 0) AS knowledge_activation_generation
"""


_ACTIVE_CORPUS_GUARD = """
CALL () {
    MATCH (state:TenantCorpusState {tenant_id: $tenant_id})
          -[:ACTIVE_EMBEDDING_INDEX]->(generation:EmbeddingIndexGeneration {
              generation_id: $generation_id,
              tenant_id: $tenant_id,
              state: 'ACTIVE'
          })
    USING INDEX state:TenantCorpusState(tenant_id)
    WHERE state.corpus_revision = $corpus_revision
      AND generation.corpus_revision = $corpus_revision
    RETURN true AS active_corpus_guard
}
"""


VECTOR_RECALL_QUERY = """
CALL () {
    MATCH (state:TenantCorpusState {tenant_id: $tenant_id})
          -[:ACTIVE_EMBEDDING_INDEX]->(generation:EmbeddingIndexGeneration {
              generation_id: $generation_id,
              tenant_id: $tenant_id,
              state: 'ACTIVE'
          })
    USING INDEX state:TenantCorpusState(tenant_id)
    WHERE state.corpus_revision = $corpus_revision
      AND generation.corpus_revision = state.corpus_revision
      AND generation.embedding_space_id = $embedding_space_id
      AND generation.dimensions = $dimensions
    RETURN generation.embedding_space_id AS active_embedding_space_id,
           generation.dimensions AS active_dimensions
}
MATCH (document:Document {tenant_id: $tenant_id})
      -[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot {
          tenant_id: $tenant_id, build_state: 'PUBLISHED'
      })-[:INCLUDES_CHUNK]->(chunk:Chunk {tenant_id: $tenant_id})
MATCH (document)-[:ACTIVE_VERSION]->(version:DocumentVersion {
    tenant_id: $tenant_id
})
MATCH (snapshot)-[:OF_VERSION]->(version)
MATCH (chunk)-[:HAS_EMBEDDING]->(embedding:ChunkEmbedding {
    tenant_id: $tenant_id
})
WHERE embedding.embedding_space_id = active_embedding_space_id
  AND embedding.chunk_id = chunk.chunk_id
  AND embedding.cosine_indexable = true
  AND embedding.vector IS NOT NULL
  AND size(embedding.vector) = active_dimensions
  AND size($query_vector) = active_dimensions
  AND any(group IN document.access_groups WHERE group IN $groups)
  AND any(group IN chunk.access_groups WHERE group IN $groups)
  AND chunk.document_id = document.document_id
  AND chunk.version_id = version.version_id
  AND chunk.access_policy_id = document.access_policy_id
  AND chunk.access_policy_version = document.access_policy_version
  AND (size($document_ids) = 0 OR document.document_id IN $document_ids)
  AND (size($version_ids) = 0 OR version.version_id IN $version_ids)
  AND ($published_before IS NULL OR version.published_at <= $published_before)
WITH DISTINCT chunk, embedding
WITH chunk, vector.similarity.cosine(embedding.vector, $query_vector) AS score
WHERE score >= $minimum_score
RETURN chunk.chunk_id AS chunk_id, score
ORDER BY score DESC, chunk_id
LIMIT $limit
"""


BM25_RECALL_QUERY = _ACTIVE_CORPUS_GUARD + """
CALL db.index.fulltext.queryNodes(
    $index_name, $lucene_query, {limit: $scan_limit}
)
YIELD node AS chunk, score
MATCH (document:Document {tenant_id: $tenant_id})
      -[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot {
          tenant_id: $tenant_id, build_state: 'PUBLISHED'
      })-[:INCLUDES_CHUNK]->(chunk)
MATCH (document)-[:ACTIVE_VERSION]->(version:DocumentVersion {
    tenant_id: $tenant_id
})
MATCH (snapshot)-[:OF_VERSION]->(version)
WHERE chunk:Chunk
  AND chunk.tenant_id = $tenant_id
  AND score >= $minimum_score
  AND any(group IN document.access_groups WHERE group IN $groups)
  AND any(group IN chunk.access_groups WHERE group IN $groups)
  AND chunk.document_id = document.document_id
  AND chunk.version_id = version.version_id
  AND chunk.access_policy_id = document.access_policy_id
  AND chunk.access_policy_version = document.access_policy_version
  AND (size($document_ids) = 0 OR document.document_id IN $document_ids)
  AND (size($version_ids) = 0 OR version.version_id IN $version_ids)
  AND ($published_before IS NULL OR version.published_at <= $published_before)
RETURN DISTINCT chunk.chunk_id AS chunk_id, score
ORDER BY score DESC, chunk_id
LIMIT $limit
"""


GRAPH_EXPANSION_QUERY = _ACTIVE_CORPUS_GUARD + """
MATCH (seed_document:Document {tenant_id: $tenant_id})
      -[:ACTIVE_SNAPSHOT]->(seed_snapshot:KnowledgeSnapshot {
          tenant_id: $tenant_id, build_state: 'PUBLISHED'
      })-[:INCLUDES_CHUNK]->(seed:Chunk {
          tenant_id: $tenant_id, chunk_id: $seed_id
      })
MATCH (seed_document)-[:ACTIVE_VERSION]->(seed_version:DocumentVersion {
    tenant_id: $tenant_id
})
MATCH (seed_snapshot)-[:OF_VERSION]->(seed_version)
MATCH (seed_snapshot)-[seed_membership:INCLUDES_MENTION]->(
    seed_mention:EntityMention {tenant_id: $tenant_id}
)-[:IN_CHUNK]->(seed)
MATCH (seed_mention)-[:REFERS_TO]->(entity:Entity {tenant_id: $tenant_id})
MATCH (seed_snapshot)-[:INCLUDES_ENTITY]->(entity)
WHERE any(group IN seed_document.access_groups WHERE group IN $groups)
  AND any(group IN seed.access_groups WHERE group IN $groups)
  AND seed.document_id = seed_document.document_id
  AND seed.version_id = seed_version.version_id
  AND seed.access_policy_id = seed_document.access_policy_id
  AND seed.access_policy_version = seed_document.access_policy_version
  AND (size($document_ids) = 0 OR seed_document.document_id IN $document_ids)
  AND (size($version_ids) = 0 OR seed_version.version_id IN $version_ids)
  AND ($published_before IS NULL OR seed_version.published_at <= $published_before)
  AND coalesce(entity.governance_status, 'ACCEPTED') IN
      ['ACCEPTED', 'ACCEPTED_BY_REVIEW']
WITH DISTINCT seed, entity, seed_membership.confidence AS mention_confidence
ORDER BY mention_confidence DESC, entity.entity_id
LIMIT $entity_limit
CALL (entity) {
    MATCH (degree_document:Document {tenant_id: $tenant_id})
          -[:ACTIVE_SNAPSHOT]->(degree_snapshot:KnowledgeSnapshot {
              tenant_id: $tenant_id, build_state: 'PUBLISHED'
          })-[:INCLUDES_CHUNK]->(linked:Chunk {tenant_id: $tenant_id})
    MATCH (degree_document)-[:ACTIVE_VERSION]->(degree_version:DocumentVersion {
        tenant_id: $tenant_id
    })
    MATCH (degree_snapshot)-[:OF_VERSION]->(degree_version)
    MATCH (degree_snapshot)-[:INCLUDES_MENTION]->(
        degree_mention:EntityMention {tenant_id: $tenant_id}
    )-[:IN_CHUNK]->(linked)
    MATCH (degree_mention)-[:REFERS_TO]->(entity)
    MATCH (degree_snapshot)-[:INCLUDES_ENTITY]->(entity)
    WHERE any(group IN degree_document.access_groups WHERE group IN $groups)
      AND any(group IN linked.access_groups WHERE group IN $groups)
      AND linked.document_id = degree_document.document_id
      AND linked.version_id = degree_version.version_id
      AND linked.access_policy_id = degree_document.access_policy_id
      AND linked.access_policy_version = degree_document.access_policy_version
      AND (size($document_ids) = 0 OR degree_document.document_id IN $document_ids)
      AND (size($version_ids) = 0 OR degree_version.version_id IN $version_ids)
      AND ($published_before IS NULL OR degree_version.published_at <= $published_before)
    RETURN count(DISTINCT linked) AS entity_degree
}
MATCH (candidate_document:Document {tenant_id: $tenant_id})
      -[:ACTIVE_SNAPSHOT]->(candidate_snapshot:KnowledgeSnapshot {
          tenant_id: $tenant_id, build_state: 'PUBLISHED'
      })-[:INCLUDES_CHUNK]->(candidate:Chunk {tenant_id: $tenant_id})
MATCH (candidate_document)-[:ACTIVE_VERSION]->(
    candidate_version:DocumentVersion {tenant_id: $tenant_id}
)
MATCH (candidate_snapshot)-[:OF_VERSION]->(candidate_version)
MATCH (candidate_snapshot)-[:INCLUDES_MENTION]->(
    candidate_mention:EntityMention {tenant_id: $tenant_id}
)-[:IN_CHUNK]->(candidate)
MATCH (candidate_mention)-[:REFERS_TO]->(entity)
MATCH (candidate_snapshot)-[:INCLUDES_ENTITY]->(entity)
WHERE candidate.chunk_id <> seed.chunk_id
  AND any(group IN candidate_document.access_groups WHERE group IN $groups)
  AND any(group IN candidate.access_groups WHERE group IN $groups)
  AND candidate.document_id = candidate_document.document_id
  AND candidate.version_id = candidate_version.version_id
  AND candidate.access_policy_id = candidate_document.access_policy_id
  AND candidate.access_policy_version = candidate_document.access_policy_version
  AND (size($document_ids) = 0 OR candidate_document.document_id IN $document_ids)
  AND (size($version_ids) = 0 OR candidate_version.version_id IN $version_ids)
  AND ($published_before IS NULL OR candidate_version.published_at <= $published_before)
RETURN DISTINCT candidate.chunk_id AS chunk_id,
       entity.entity_id AS entity_id,
       entity.canonical_name AS entity_name,
       entity_degree
ORDER BY chunk_id, entity_id
LIMIT $edge_limit
"""


CANDIDATE_VECTOR_QUERY = """
CALL () {
    MATCH (state:TenantCorpusState {tenant_id: $tenant_id})
          -[:ACTIVE_EMBEDDING_INDEX]->(generation:EmbeddingIndexGeneration {
              generation_id: $generation_id,
              tenant_id: $tenant_id,
              state: 'ACTIVE'
          })
    USING INDEX state:TenantCorpusState(tenant_id)
    WHERE state.corpus_revision = $corpus_revision
      AND generation.corpus_revision = state.corpus_revision
      AND generation.embedding_space_id = $embedding_space_id
      AND generation.dimensions = $dimensions
    RETURN generation.embedding_space_id AS active_embedding_space_id,
           generation.dimensions AS active_dimensions
}
UNWIND $candidate_ids AS candidate_id
MATCH (chunk:Chunk {chunk_id: candidate_id})
USING INDEX chunk:Chunk(chunk_id)
MATCH (document:Document {tenant_id: $tenant_id})
      -[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot {
          tenant_id: $tenant_id, build_state: 'PUBLISHED'
      })-[:INCLUDES_CHUNK]->(chunk)
MATCH (document)-[:ACTIVE_VERSION]->(version:DocumentVersion {
    tenant_id: $tenant_id
})
MATCH (snapshot)-[:OF_VERSION]->(version)
MATCH (chunk)-[:HAS_EMBEDDING]->(embedding:ChunkEmbedding {
    tenant_id: $tenant_id
})
WHERE chunk.tenant_id = $tenant_id
  AND embedding.embedding_space_id = active_embedding_space_id
  AND embedding.chunk_id = chunk.chunk_id
  AND embedding.cosine_indexable = true
  AND embedding.vector IS NOT NULL
  AND size(embedding.vector) = active_dimensions
  AND size($query_vector) = active_dimensions
  AND any(group IN document.access_groups WHERE group IN $groups)
  AND any(group IN chunk.access_groups WHERE group IN $groups)
  AND chunk.document_id = document.document_id
  AND chunk.version_id = version.version_id
  AND chunk.access_policy_id = document.access_policy_id
  AND chunk.access_policy_version = document.access_policy_version
  AND (size($document_ids) = 0 OR document.document_id IN $document_ids)
  AND (size($version_ids) = 0 OR version.version_id IN $version_ids)
  AND ($published_before IS NULL OR version.published_at <= $published_before)
WITH DISTINCT chunk, embedding
WITH chunk, vector.similarity.cosine(embedding.vector, $query_vector) AS score
WHERE score >= $minimum_score
RETURN chunk.chunk_id AS chunk_id, score
ORDER BY score DESC, chunk_id
LIMIT $limit
"""


ADJACENT_QUERY = _ACTIVE_CORPUS_GUARD + """
UNWIND range(0, size($anchor_ids) - 1) AS anchor_position
WITH anchor_position, $anchor_ids[anchor_position] AS anchor_id
MATCH (document:Document {tenant_id: $tenant_id})
      -[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot {
          tenant_id: $tenant_id, build_state: 'PUBLISHED'
      })-[:INCLUDES_CHUNK]->(anchor:Chunk {
          tenant_id: $tenant_id, chunk_id: anchor_id
      })
MATCH (document)-[:ACTIVE_VERSION]->(version:DocumentVersion {
    tenant_id: $tenant_id
})
MATCH (snapshot)-[:OF_VERSION]->(version)
MATCH (snapshot)-[:INCLUDES_CHUNK]->(neighbor:Chunk {tenant_id: $tenant_id})
WHERE neighbor.chunk_id <> anchor.chunk_id
  AND abs(neighbor.ordinal - anchor.ordinal) <= $adjacent_window
  AND any(group IN document.access_groups WHERE group IN $groups)
  AND any(group IN anchor.access_groups WHERE group IN $groups)
  AND any(group IN neighbor.access_groups WHERE group IN $groups)
  AND anchor.document_id = document.document_id
  AND neighbor.document_id = document.document_id
  AND anchor.version_id = version.version_id
  AND neighbor.version_id = version.version_id
  AND anchor.access_policy_id = document.access_policy_id
  AND neighbor.access_policy_id = document.access_policy_id
  AND anchor.access_policy_version = document.access_policy_version
  AND neighbor.access_policy_version = document.access_policy_version
  AND (size($document_ids) = 0 OR document.document_id IN $document_ids)
  AND (size($version_ids) = 0 OR version.version_id IN $version_ids)
  AND ($published_before IS NULL OR version.published_at <= $published_before)
RETURN DISTINCT anchor_position, anchor.chunk_id AS anchor_id,
       neighbor.chunk_id AS chunk_id,
       abs(neighbor.ordinal - anchor.ordinal) AS distance,
       neighbor.ordinal AS ordinal
ORDER BY anchor_position, distance, ordinal, chunk_id
LIMIT $limit
"""


HYDRATE_QUERY = _ACTIVE_CORPUS_GUARD + """
UNWIND $chunk_ids AS requested_id
MATCH (document:Document {tenant_id: $tenant_id})
      -[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot {
          tenant_id: $tenant_id, build_state: 'PUBLISHED'
      })-[:INCLUDES_CHUNK]->(chunk:Chunk {
          tenant_id: $tenant_id, chunk_id: requested_id
      })
MATCH (document)-[:ACTIVE_VERSION]->(version:DocumentVersion {
    tenant_id: $tenant_id
})
MATCH (snapshot)-[:OF_VERSION]->(version)
WHERE any(group IN document.access_groups WHERE group IN $groups)
  AND any(group IN chunk.access_groups WHERE group IN $groups)
  AND chunk.document_id = document.document_id
  AND chunk.version_id = version.version_id
  AND chunk.access_policy_id = document.access_policy_id
  AND chunk.access_policy_version = document.access_policy_version
  AND (size($document_ids) = 0 OR document.document_id IN $document_ids)
  AND (size($version_ids) = 0 OR version.version_id IN $version_ids)
  AND ($published_before IS NULL OR version.published_at <= $published_before)
RETURN DISTINCT chunk.chunk_id AS chunk_id,
       chunk.text AS text,
       chunk.checksum AS chunk_checksum,
       chunk.ordinal AS ordinal,
       chunk.char_start AS char_start,
       chunk.char_end AS char_end,
       chunk.page_number AS page_number,
       chunk.section AS section,
       document.document_id AS document_id,
       document.canonical_uri AS canonical_uri,
       document.source_name AS source_name,
       document.title AS document_title,
       version.version_id AS version_id,
       version.checksum AS version_checksum,
       version.version_number AS version_number,
       version.published_at AS published_at
ORDER BY chunk_id
"""


def _query_terms(query_text: str) -> str:
    """Produce literal word terms so user text cannot become Lucene syntax."""
    return " ".join(_LUCENE_TERM.findall(query_text))


def _partitioned_lucene_query(
    query_text: str,
    tenant_id: str,
    groups: frozenset[str],
) -> str:
    """Apply tenant, active-version, and ACL filters inside BM25 recall."""

    terms = _query_terms(query_text)
    if not terms:
        return ""
    tenant = retrieval_scope_token("tenant", tenant_id)
    group_terms = " OR ".join(
        f"retrieval_scope:{retrieval_scope_token('group', group)}^0"
        for group in sorted(groups)
    )
    return (
        "retrieval_scope:grscopeactive^0 "
        f"AND retrieval_scope:{tenant}^0 "
        f"AND ({group_terms}) AND text:({terms})"
    )


def _records(tx: Any, query: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(record) for record in tx.run(query, **parameters)]


def _native_datetime(value: Any) -> datetime | None:
    """Convert Neo4j temporal values without inventing missing provenance."""
    if value is None:
        return None
    to_native = getattr(value, "to_native", None)
    return to_native() if callable(to_native) else value


def _ids(records: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(str(record["chunk_id"]) for record in records)


def _content_deduplication_key(record: dict[str, Any]) -> tuple[str, str]:
    """Preserve immutable Version provenance while removing local duplicates."""
    return str(record["version_id"]), str(record["chunk_checksum"])


def _trace_hits(
    records: list[dict[str, Any]],
    *,
    score_key: str = "score",
) -> tuple[TraceHit, ...]:
    return tuple(
        TraceHit(
            chunk_id=str(record["chunk_id"]),
            rank=rank,
            score=(None if record.get(score_key) is None else float(record[score_key])),
        )
        for rank, record in enumerate(records, start=1)
    )


def _retrieved_chunk(
    record: dict[str, Any], *, role: str, score: float | None, reasons: tuple[str, ...],
) -> RetrievedChunk:
    citation = Citation(
        chunk_id=str(record["chunk_id"]),
        chunk_checksum=str(record["chunk_checksum"]),
        document_id=str(record["document_id"]),
        canonical_uri=str(record["canonical_uri"]),
        source_name=str(record["source_name"]),
        version_id=str(record["version_id"]),
        version_checksum=str(record["version_checksum"]),
        version_number=int(record["version_number"]),
        ordinal=int(record["ordinal"]),
        char_start=int(record["char_start"]),
        char_end=int(record["char_end"]),
        page_number=None if record.get("page_number") is None else int(record["page_number"]),
        section=None if record.get("section") is None else str(record["section"]),
        document_title=None if record.get("document_title") is None else str(record["document_title"]),
        published_at=_native_datetime(record.get("published_at")),
    )
    return RetrievedChunk(str(record["text"]), citation, role, score, reasons)


class Neo4jRetrievalEngine:
    """Run a complete bounded retrieval in one consistent read transaction."""

    def __init__(
        self,
        driver: Any,
        database: str = "neo4j",
        *,
        transaction_timeout_seconds: float = 60.0,
        reranker: CandidateReranker | None = None,
        rerank_candidate_limit: int = MAX_RERANK_CANDIDATES,
    ) -> None:
        if driver is None:
            raise ValueError("driver must not be None")
        if not isinstance(database, str) or not database.strip():
            raise ValueError("database must not be empty")
        if any(character in database for character in ("\x00", "\r", "\n")):
            raise ValueError("database contains a forbidden control character")
        if (
            isinstance(transaction_timeout_seconds, bool)
            or not isinstance(transaction_timeout_seconds, (int, float))
            or not math.isfinite(float(transaction_timeout_seconds))
            or not 0.0 < float(transaction_timeout_seconds) <= 300.0
        ):
            raise ValueError(
                "transaction_timeout_seconds must be a finite number "
                "between zero and 300"
            )
        self.driver = driver
        self.database = database.strip()
        self.transaction_timeout_seconds = float(transaction_timeout_seconds)
        if reranker is not None and not callable(getattr(reranker, "rerank", None)):
            raise TypeError("reranker must provide rerank()")
        if type(rerank_candidate_limit) is not int or not 1 <= rerank_candidate_limit <= MAX_RERANK_CANDIDATES:
            raise ValueError("rerank_candidate_limit must be between 1 and 50")
        self._reranker = reranker
        self.rerank_candidate_limit = rerank_candidate_limit
        self._transaction_work = unit_of_work(
            metadata={"component": "graphrag-retrieval", "operation": "retrieve"},
            timeout=self.transaction_timeout_seconds,
        )(self._retrieve_tx)
        self._prepare_work = unit_of_work(
            metadata={"component": "graphrag-retrieval", "operation": "prepare-rerank"},
            timeout=self.transaction_timeout_seconds,
        )(self._prepare_rerank_tx)
        self._finalize_work = unit_of_work(
            metadata={"component": "graphrag-retrieval", "operation": "finalize-rerank"},
            timeout=self.transaction_timeout_seconds,
        )(self._finalize_rerank_tx)

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        try:
            if self._reranker is not None:
                return self._retrieve_reranked(request)
            with self.driver.session(database=self.database) as session:
                for attempt in range(_CORPUS_STATE_ATTEMPTS):
                    try:
                        return session.execute_read(self._transaction_work, request)
                    except _CorpusStateChanged as error:
                        if attempt + 1 == _CORPUS_STATE_ATTEMPTS:
                            raise RetrievalUnavailable(
                                "tenant corpus changed repeatedly during retrieval"
                            ) from error
                raise AssertionError("bounded corpus-state attempts were exhausted")
        except ConnectionAcquisitionTimeoutError as error:
            raise RetrievalBackendTimeout() from error
        except Neo4jError as error:
            if getattr(error, "code", None) in _TRANSACTION_TIMEOUT_CODES:
                raise RetrievalBackendTimeout() from error
            if isinstance(error, TransientError):
                raise RetrievalBackendUnavailable() from error
            raise RetrievalBackendError() from error
        except (ServiceUnavailable, SessionExpired) as error:
            raise RetrievalBackendUnavailable() from error
        except DriverError as error:
            raise RetrievalBackendUnavailable() from error

    def _retrieve_reranked(self, request: RetrievalRequest) -> RetrievalResult:
        prepared = None
        with self.driver.session(database=self.database) as session:
            for attempt in range(_CORPUS_STATE_ATTEMPTS):
                try:
                    prepared = session.execute_read(self._prepare_work, request, self.rerank_candidate_limit)
                    break
                except _CorpusStateChanged as error:
                    if attempt + 1 == _CORPUS_STATE_ATTEMPTS:
                        raise RetrievalUnavailable("tenant corpus changed repeatedly before reranking") from error
        if not isinstance(prepared, _PreparedRerank):
            raise RetrievalUnavailable("reranking candidate preparation is invalid")
        if not prepared.candidates:
            if prepared.result.chunks:
                raise RetrievalUnavailable("context has no authorized reranking candidates")
            trace = replace(prepared.result.trace, reranking=RerankTrace(
                "SKIPPED_EMPTY", self.rerank_candidate_limit, (), None,
            ))
            return replace(prepared.result, trace=trace)

        try:
            return self._rank_prepared(prepared)
        except Exception as error:
            timeout = (
                isinstance(error, (TimeoutError, RetrievalBackendTimeout, ConnectionAcquisitionTimeoutError))
                or getattr(error, "code", None) in _TRANSACTION_TIMEOUT_CODES
            )
            raise RerankAttemptedFailure(timeout=timeout) from error

    def _rank_prepared(self, prepared: _PreparedRerank) -> RetrievalResult:
        request = prepared.request
        # The provider is outside both managed transactions. A transaction retry
        # can never repeat the paid call, and no provider failure falls back.
        try:
            query = request.query_text + ("\n" + request.rerank_context if request.rerank_context else "")
            response = self._reranker.rerank(query, prepared.candidates)
        except (TimeoutError, RerankingError) as error:
            if isinstance(error, TimeoutError) or getattr(error, "code", None) == "RERANK_TIMEOUT":
                raise RetrievalBackendTimeout() from error
            raise RetrievalUnavailable("candidate reranking failed") from error
        except Exception as error:
            raise RetrievalUnavailable("candidate reranking failed") from error
        if not isinstance(response, RerankResponse):
            raise RetrievalUnavailable("candidate reranking returned an invalid response")
        candidate_ids = tuple(candidate.chunk_id for candidate in prepared.candidates)
        expected_checksums = tuple(candidate.checksum for candidate in prepared.candidates)
        if response.source_checksums != expected_checksums or len(response.rendered_input_checksums) != len(candidate_ids):
            raise RetrievalUnavailable("reranking response differs from its exact source inputs")
        # Provider rank order is canonical. Pointwise model scores are optional
        # observations; a listwise provider may return ranks without scores.
        ordered_scores = response.scores
        ordered_ids = tuple(item.chunk_id for item in ordered_scores)
        try:
            trace = RerankTrace("RERANKED", self.rerank_candidate_limit, candidate_ids, response, ordered_ids)
        except (TypeError, ValueError, IndexError) as error:
            raise RetrievalUnavailable("reranking output does not identify its exact candidates") from error
        try:
            with self.driver.session(database=self.database) as session:
                return session.execute_read(self._finalize_work, prepared, trace)
        except _CorpusStateChanged as error:
            # Do not spend a second paid call after a publication, embedding,
            # version or access change during external model execution.
            raise RetrievalUnavailable("retrieval state changed during reranking; issue a fresh request") from error

    @staticmethod
    def _prepare_rerank_tx(tx: Any, request: RetrievalRequest, candidate_limit: int) -> _PreparedRerank:
        capture: dict[str, Any] = {"limit": candidate_limit}
        result = Neo4jRetrievalEngine._retrieve_tx(tx, request, _candidate_capture=capture)
        return _PreparedRerank(request, result, capture.get("candidates", ()))

    @staticmethod
    def _check_rerank_state(tx: Any, prepared: _PreparedRerank, parameters: dict[str, Any]) -> None:
        rows = _records(tx, CORPUS_STATE_QUERY, parameters)
        if len(rows) != 1:
            raise _CorpusStateChanged()
        row, trace = rows[0], prepared.result.trace
        if (
            row.get("corpus_revision") != trace.corpus_revision
            or row.get("generation_id") != trace.embedding_generation_id
            or row.get("embedding_space_id") != trace.embedding_space_id
            or row.get("dimensions") != len(prepared.request.query_vector)
            or Neo4jRetrievalEngine._publication_identity(row)
            != (trace.knowledge_publication_id, trace.knowledge_activation_generation)
        ):
            raise _CorpusStateChanged()

    @staticmethod
    def _finalize_rerank_tx(tx: Any, prepared: _PreparedRerank, reranking: RerankTrace) -> RetrievalResult:
        request, previous = prepared.request, prepared.result.trace
        limits, version_filter = request.limits, request.version_filter
        common = {
            "tenant_id": request.principal.tenant_id, "groups": sorted(request.principal.groups),
            "document_ids": sorted(version_filter.document_ids), "version_ids": sorted(version_filter.version_ids),
            "published_before": version_filter.published_at_or_before,
            "corpus_revision": previous.corpus_revision, "generation_id": previous.embedding_generation_id,
            "embedding_space_id": previous.embedding_space_id, "dimensions": len(request.query_vector),
        }
        Neo4jRetrievalEngine._check_rerank_state(tx, prepared, common)
        # The response exposes recall/rejection IDs as well as selected evidence.
        # Recheck every one under current snapshot/version and document+chunk ACL.
        visible_ids = set(previous.selected_chunk_ids)
        for stage in (previous.vector_recall, previous.bm25_recall, previous.seed_ranking,
                      previous.graph_expansion, previous.candidate_vector_ranking,
                      previous.final_ranking, previous.decisions):
            visible_ids.update(item.chunk_id for item in stage)
        visible_ids.update(reranking.candidate_chunk_ids)
        rows = _records(tx, HYDRATE_QUERY, {**common, "chunk_ids": sorted(visible_ids)})
        hydrated = {str(row["chunk_id"]): row for row in rows}
        if len(hydrated) != len(rows) or set(hydrated) != visible_ids:
            raise _CorpusStateChanged()
        for candidate in prepared.candidates:
            row = hydrated[candidate.chunk_id]
            if (row["text"] != candidate.text or row["chunk_checksum"] != candidate.checksum
                    or row["document_id"] != candidate.document_id or row["version_id"] != candidate.version_id
                    or row.get("document_title") != candidate.source_title or row.get("section") != candidate.source_section):
                raise _CorpusStateChanged()

        ordered_ids = reranking.ranked_chunk_ids
        scores = {item.chunk_id: item.score for item in reranking.response.scores}
        anchors = ordered_ids[:limits.anchor_k]
        adjacent_records = _records(tx, ADJACENT_QUERY, {
            **common, "anchor_ids": list(anchors), "adjacent_window": limits.adjacent_window,
            "limit": limits.anchor_k * max(1, limits.adjacent_window) * 2,
        }) if anchors and limits.adjacent_window else []
        adjacent_ids = _ids(adjacent_records)
        if adjacent_ids:
            adjacent_rows = _records(tx, HYDRATE_QUERY, {**common, "chunk_ids": list(adjacent_ids)})
            if {str(row["chunk_id"]) for row in adjacent_rows} != set(adjacent_ids):
                raise _CorpusStateChanged()
            hydrated.update({str(row["chunk_id"]): row for row in adjacent_rows})
        Neo4jRetrievalEngine._check_rerank_state(tx, prepared, common)

        # Provider rank determines candidate order. Optional scores are neither
        # added to RRF nor interpreted as fact confidence or cross-query probabilities.
        content_keys = {identifier: _content_deduplication_key(row) for identifier, row in hydrated.items()}
        combined, duplicates = stable_deduplicate((*ordered_ids, *adjacent_ids), content_keys,
                                                  deduplicate_content=limits.deduplicate_content)
        ranked_set = set(ordered_ids)
        adjacency = tuple(identifier for identifier in combined if identifier not in ranked_set)
        selection = select_context(ranked_ids=ordered_ids, anchor_ids=anchors, adjacent_ids=adjacency,
            char_lengths={identifier: len(str(row["text"])) for identifier, row in hydrated.items()},
            max_chunks=limits.top_k, max_chars=limits.max_context_chars)
        decisions = [item for item in previous.decisions
                     if item.reason in {"insufficient_rrf_channels", "final_authorization_or_version_check"}]
        decisions.extend(TraceDecision(identifier, "rejected", "rerank_candidate_limit")
                         for identifier in (item.chunk_id for item in previous.final_ranking)
                         if identifier not in ranked_set)
        decisions.extend(TraceDecision(identifier, "rejected", "duplicate_content") for identifier in duplicates)
        decisions.extend(TraceDecision(identifier, "rejected", reason) for identifier, reason in selection.skipped)
        role_by_id = dict(selection.roles)
        positions = {identifier: index for index, identifier in enumerate(ordered_ids, start=1)}
        adjacent_anchors = {str(row["chunk_id"]): str(row["anchor_id"]) for row in adjacent_records}
        chunks = []
        for identifier in selection.chunk_ids:
            role = role_by_id[identifier]
            reasons = ((f"reranker {reranking.response.model} rank {positions[identifier]}",)
                       if identifier in positions else (f"adjacent to {adjacent_anchors[identifier]}",))
            chunks.append(_retrieved_chunk(hydrated[identifier], role=role, score=scores.get(identifier), reasons=reasons))
            decisions.append(TraceDecision(identifier, "selected", role))
        trace_id = hashlib.sha256((previous.trace_id + "\0" + reranking.response.input_checksum
                                  + "\0" + reranking.response.output_checksum).encode("utf-8")).hexdigest()
        trace = replace(previous, trace_id=trace_id, method=previous.method + " + candidate reranking",
                        decisions=tuple(decisions), selected_chunk_ids=selection.chunk_ids,
                        context_chars=selection.total_chars, reranking=reranking)
        return RetrievalResult(tuple(chunks), trace)

    @staticmethod
    def _retrieve_tx(
        tx: Any, request: RetrievalRequest, *, _candidate_capture: dict[str, Any] | None = None,
    ) -> RetrievalResult:
        principal = request.principal
        limits = request.limits
        version_filter = request.version_filter
        common: dict[str, Any] = {
            "tenant_id": principal.tenant_id,
            "groups": sorted(principal.groups),
            "document_ids": sorted(version_filter.document_ids),
            "version_ids": sorted(version_filter.version_ids),
            "published_before": version_filter.published_at_or_before,
        }
        state_records = _records(tx, CORPUS_STATE_QUERY, common)
        if not state_records:
            raise RetrievalUnavailable("tenant corpus state is unavailable")
        if len(state_records) != 1:
            raise RetrievalUnavailable("tenant has multiple active embedding generations")
        state = state_records[0]
        if state.get("generation_id") is None:
            raise RetrievalUnavailable("tenant has no active embedding generation")
        if request.query_embedding_space_id != str(state["embedding_space_id"]):
            raise RetrievalUnavailable("query vector space is not the active generation")
        dimensions = int(state["dimensions"])
        if len(request.query_vector) != dimensions:
            raise RetrievalUnavailable(
                f"query vector dimension {len(request.query_vector)} does not match {dimensions}"
            )
        corpus_revision = int(state["corpus_revision"])
        publication_identity = Neo4jRetrievalEngine._publication_identity(state)
        common.update(
            {
                "corpus_revision": corpus_revision,
                "dimensions": dimensions,
                "embedding_space_id": str(state["embedding_space_id"]),
                "generation_id": str(state["generation_id"]),
                "query_vector": list(request.query_vector),
            }
        )

        vector_records = [] if version_filter.match_none else _records(
            tx,
            VECTOR_RECALL_QUERY,
            {
                **common,
                "minimum_score": limits.minimum_vector_score,
                "limit": limits.vector_recall_k,
            },
        )
        lucene_query = _partitioned_lucene_query(
            request.query_text,
            principal.tenant_id,
            principal.groups,
        )
        bm25_records = (
            _records(
                tx,
                BM25_RECALL_QUERY,
                {
                    **common,
                    "index_name": FULLTEXT_INDEX_NAME,
                    "lucene_query": lucene_query,
                    "scan_limit": limits.bm25_scan_k,
                    "minimum_score": limits.minimum_bm25_score,
                    "limit": limits.bm25_recall_k,
                },
            )
            if lucene_query and not version_filter.match_none
            else []
        )
        vector_ids = _ids(vector_records)
        bm25_ids = _ids(bm25_records)
        seed_order, seed_scores, seed_positions = reciprocal_rank_fusion(
            {"vector": vector_ids, "bm25": bm25_ids},
            rank_constant=limits.rrf_rank_constant,
        )
        seed_ids = seed_order[: limits.seed_k]

        graph_records: list[dict[str, Any]] = []
        graph_reasons: defaultdict[str, list[str]] = defaultdict(list)
        graph_candidate_order: list[str] = []
        for seed_id in seed_ids:
            edges = _records(
                tx,
                GRAPH_EXPANSION_QUERY,
                {
                    **common,
                    "seed_id": seed_id,
                    "entity_limit": limits.graph_entities_per_seed,
                    "edge_limit": limits.graph_edges_per_seed,
                },
            )
            by_candidate: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
            for edge in edges:
                by_candidate[str(edge["chunk_id"])].append(edge)
            scored: list[tuple[str, float, list[dict[str, Any]]]] = []
            for chunk_id, candidate_edges in by_candidate.items():
                score = resource_allocation_score(
                    [int(edge["entity_degree"]) for edge in candidate_edges]
                )
                scored.append((chunk_id, score, candidate_edges))
            scored.sort(key=lambda item: (-item[1], item[0]))
            for rank, (chunk_id, score, candidate_edges) in enumerate(
                scored[: limits.graph_candidates_per_seed], start=1
            ):
                entity_ids = sorted(str(edge["entity_id"]) for edge in candidate_edges)
                reason = (
                    f"RA from {seed_id} rank {rank}; shared entities "
                    + ",".join(entity_ids)
                )
                graph_reasons[chunk_id].append(reason)
                graph_candidate_order.append(chunk_id)
                graph_records.append(
                    {
                        "chunk_id": chunk_id,
                        "score": score,
                        "reason": reason,
                    }
                )

        candidate_ids, _ = stable_deduplicate(
            (*vector_ids, *bm25_ids, *graph_candidate_order),
            {},
            deduplicate_content=False,
        )
        candidate_ids = candidate_ids[: limits.candidate_limit]
        candidate_vector_records = (
            _records(
                tx,
                CANDIDATE_VECTOR_QUERY,
                {
                    **common,
                    "candidate_ids": list(candidate_ids),
                    "minimum_score": limits.minimum_vector_score,
                    "limit": limits.candidate_limit,
                },
            )
            if candidate_ids
            else []
        )
        candidate_vector_ids = _ids(candidate_vector_records)
        final_order, final_scores, final_positions = reciprocal_rank_fusion(
            {"vector": candidate_vector_ids, "bm25": bm25_ids},
            rank_constant=limits.rrf_rank_constant,
        )
        gated_order = tuple(
            chunk_id
            for chunk_id in final_order
            if len(final_positions[chunk_id]) >= limits.minimum_rrf_channels
        )
        decisions: list[TraceDecision] = [
            TraceDecision(chunk_id, "rejected", "insufficient_rrf_channels")
            for chunk_id in final_order
            if chunk_id not in gated_order
        ]
        pre_anchor_ids = gated_order[: limits.anchor_k]
        adjacent_records = (
            _records(
                tx,
                ADJACENT_QUERY,
                {
                    **common,
                    "anchor_ids": list(pre_anchor_ids),
                    "adjacent_window": limits.adjacent_window,
                    "limit": limits.anchor_k
                    * max(1, limits.adjacent_window)
                    * 2,
                },
            )
            if pre_anchor_ids and limits.adjacent_window
            else []
        )
        adjacent_ids = _ids(adjacent_records)
        hydrate_ids, _ = stable_deduplicate(
            (*gated_order, *adjacent_ids), {}, deduplicate_content=False
        )
        hydrated_records = (
            _records(
                tx,
                HYDRATE_QUERY,
                {**common, "chunk_ids": list(hydrate_ids)},
            )
            if hydrate_ids
            else []
        )
        final_state_records = _records(tx, CORPUS_STATE_QUERY, common)
        if len(final_state_records) != 1:
            raise _CorpusStateChanged()
        final_state = final_state_records[0]
        try:
            final_identity = (
                int(final_state["corpus_revision"]),
                str(final_state["generation_id"]),
                str(final_state["embedding_space_id"]),
                int(final_state["dimensions"]),
            )
        except (KeyError, TypeError, ValueError):
            raise _CorpusStateChanged() from None
        captured_identity = (
            corpus_revision,
            str(state["generation_id"]),
            str(state["embedding_space_id"]),
            dimensions,
        )
        if (
            final_identity != captured_identity
            or Neo4jRetrievalEngine._publication_identity(final_state) != publication_identity
        ):
            raise _CorpusStateChanged()
        hydrated = {str(record["chunk_id"]): record for record in hydrated_records}
        for chunk_id in hydrate_ids:
            if chunk_id not in hydrated:
                decisions.append(
                    TraceDecision(chunk_id, "rejected", "final_authorization_or_version_check")
                )

        content_keys = {
            chunk_id: _content_deduplication_key(record)
            for chunk_id, record in hydrated.items()
        }
        deduped_ranking, duplicate_ids = stable_deduplicate(
            tuple(chunk_id for chunk_id in gated_order if chunk_id in hydrated),
            content_keys,
            deduplicate_content=limits.deduplicate_content,
        )
        for chunk_id in duplicate_ids:
            decisions.append(TraceDecision(chunk_id, "rejected", "duplicate_content"))
        if _candidate_capture is not None:
            _candidate_capture["candidates"] = tuple(
                RerankCandidate(
                    chunk_id, str(hydrated[chunk_id]["text"]), str(hydrated[chunk_id]["chunk_checksum"]),
                    source_title=hydrated[chunk_id].get("document_title"),
                    source_section=hydrated[chunk_id].get("section"),
                    document_id=str(hydrated[chunk_id]["document_id"]),
                    version_id=str(hydrated[chunk_id]["version_id"]),
                )
                for chunk_id in deduped_ranking[:_candidate_capture["limit"]]
            )
        anchor_ids = deduped_ranking[: limits.anchor_k]
        hydrated_adjacency = tuple(
            chunk_id for chunk_id in adjacent_ids if chunk_id in hydrated
        )
        combined_order, adjacency_duplicates = stable_deduplicate(
            (*deduped_ranking, *hydrated_adjacency),
            content_keys,
            deduplicate_content=limits.deduplicate_content,
        )
        ranked_set = set(deduped_ranking)
        deduped_adjacency = tuple(
            chunk_id for chunk_id in combined_order if chunk_id not in ranked_set
        )
        for chunk_id in adjacency_duplicates:
            decisions.append(TraceDecision(chunk_id, "rejected", "duplicate_content"))
        selection = select_context(
            ranked_ids=deduped_ranking,
            anchor_ids=anchor_ids,
            adjacent_ids=deduped_adjacency,
            char_lengths={key: len(str(value["text"])) for key, value in hydrated.items()},
            max_chunks=limits.top_k,
            max_chars=limits.max_context_chars,
        )
        for chunk_id, reason in selection.skipped:
            decisions.append(TraceDecision(chunk_id, "rejected", reason))
        role_by_id = dict(selection.roles)
        adjacency_anchor = {
            str(record["chunk_id"]): str(record["anchor_id"])
            for record in adjacent_records
        }
        result_chunks: list[RetrievedChunk] = []
        for chunk_id in selection.chunk_ids:
            record = hydrated[chunk_id]
            role = role_by_id[chunk_id]
            reasons = [
                f"{channel} rank {rank}"
                for channel, rank in sorted(final_positions.get(chunk_id, {}).items())
            ]
            reasons.extend(graph_reasons.get(chunk_id, []))
            if role == "adjacent":
                reasons.append(f"adjacent to {adjacency_anchor[chunk_id]}")
            result_chunks.append(_retrieved_chunk(
                record, role=role, score=final_scores.get(chunk_id), reasons=tuple(reasons),
            ))
            decisions.append(TraceDecision(chunk_id, "selected", role))

        seed_trace = tuple(
            TraceHit(
                chunk_id=chunk_id,
                rank=rank,
                score=seed_scores[chunk_id],
                ranks=tuple(sorted(seed_positions[chunk_id].items())),
            )
            for rank, chunk_id in enumerate(seed_order, start=1)
        )
        graph_trace = tuple(
            TraceHit(
                chunk_id=str(record["chunk_id"]),
                rank=rank,
                score=float(record["score"]),
                reasons=(str(record["reason"]),),
            )
            for rank, record in enumerate(graph_records, start=1)
        )
        final_trace = tuple(
            TraceHit(
                chunk_id=chunk_id,
                rank=rank,
                score=final_scores[chunk_id],
                ranks=tuple(sorted(final_positions[chunk_id].items())),
                reasons=tuple(graph_reasons.get(chunk_id, ())),
            )
            for rank, chunk_id in enumerate(final_order, start=1)
        )
        trace_id = Neo4jRetrievalEngine._trace_id(
            request,
            corpus_revision,
            str(state["generation_id"]),
            publication_identity=publication_identity,
        )
        trace = RetrievalTrace(
            trace_id=trace_id,
            method=METHOD.replace("k=60", f"k={limits.rrf_rank_constant}"),
            tenant_id=principal.tenant_id,
            corpus_revision=corpus_revision,
            embedding_generation_id=str(state["generation_id"]),
            embedding_space_id=str(state["embedding_space_id"]),
            vector_recall=_trace_hits(vector_records),
            bm25_recall=_trace_hits(bm25_records),
            seed_ranking=seed_trace,
            graph_expansion=graph_trace,
            candidate_vector_ranking=_trace_hits(candidate_vector_records),
            final_ranking=final_trace,
            decisions=tuple(decisions),
            selected_chunk_ids=selection.chunk_ids,
            context_chars=selection.total_chars,
            limits=limits,
            version_filter=version_filter,
            knowledge_publication_id=publication_identity[0],
            knowledge_activation_generation=publication_identity[1],
        )
        return RetrievalResult(tuple(result_chunks), trace)

    @staticmethod
    def _publication_identity(state: dict[str, Any]) -> tuple[str | None, int]:
        publication_id = state.get("knowledge_publication_id")
        generation = state.get("knowledge_activation_generation", 0)
        if (
            publication_id is not None
            and (not isinstance(publication_id, str) or not publication_id.strip())
        ) or type(generation) is not int or generation < 0:
            raise RetrievalUnavailable("tenant knowledge publication state is invalid")
        return publication_id, generation

    @staticmethod
    def _trace_id(
        request: RetrievalRequest,
        corpus_revision: int,
        generation_id: str,
        *, publication_identity: tuple[str | None, int] = (None, 0),
    ) -> str:
        cutoff: datetime | None = request.version_filter.published_at_or_before
        payload = {
            "query": request.query_text,
            "tenant_id": request.principal.tenant_id,
            "principal_id": request.principal.principal_id,
            "groups": sorted(request.principal.groups),
            "query_vector": list(request.query_vector),
            "query_embedding_space_id": request.query_embedding_space_id,
            "corpus_revision": corpus_revision,
            "generation_id": generation_id,
            "documents": sorted(request.version_filter.document_ids),
            "versions": sorted(request.version_filter.version_ids),
            "published_before": None if cutoff is None else cutoff.isoformat(),
            "match_none": request.version_filter.match_none,
            "knowledge_publication_id": publication_identity[0],
            "knowledge_activation_generation": publication_identity[1],
            "limits": {
                item.name: getattr(request.limits, item.name)
                for item in fields(request.limits)
            },
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
