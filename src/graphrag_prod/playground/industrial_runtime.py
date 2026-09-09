"""Opt-in industrial service composition without rebuilding the existing corpus."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any
from neo4j import Query

from graphrag_prod.api.backend import GraphRAGQueryOperations
from graphrag_prod.api.contracts import IndustrialScopeRequest, RetrievalLimitsRequest
from graphrag_prod.industrial.construction import (
    INDUSTRIAL_TENANT, INDUSTRIAL_TBOX_KEY, INDUSTRIAL_RERANK_PROFILE, INDUSTRIAL_UPLOAD_CHUNK_CHARS,
)
from graphrag_prod.industrial.retrieval import Neo4jIndustrialScopeResolver
from graphrag_prod.ingestion import Neo4jEmbeddingIndexManager
from graphrag_prod.ontology.models import TBoxStatus
from graphrag_prod.ontology.store import Neo4jTBoxStore
from graphrag_prod.retrieval import Neo4jRetrievalEngine, Neo4jEvidenceSubgraphProjector, RetrievalLimits
from graphrag_prod.retrieval.rerank_provider import create_reranker

INDUSTRIAL_RETRIEVAL_LIMITS = RetrievalLimits(
    top_k=5, seed_k=3, graph_entities_per_seed=8, graph_edges_per_seed=40,
    graph_candidates_per_seed=8, candidate_limit=50, anchor_k=5, minimum_vector_score=0.75,
)


def industrial_personas():
    from .runtime import PlaygroundPersona, PLAYGROUND_SCOPES
    read = ("retrieval:read", "ontology:read", "knowledge:graph:read")
    definitions = (
        (8, "工业 · 公开读者", ("public",), read),
        (9, "工业 · 工程师", ("engineering", "public"), read + ("knowledge:construct", "knowledge:review")),
        (10, "工业 · 维护人员", ("maintenance", "public"), read + ("knowledge:construct",)),
        (11, "工业 · 知识管理员", ("engineering", "maintenance", "public"), PLAYGROUND_SCOPES),
    )
    return tuple(PlaygroundPersona(f"persona-{number:02d}", f"local-industrial-{number:02d}",
                                  label, INDUSTRIAL_TENANT, groups, scopes)
                 for number, label, groups, scopes in definitions)


def industrial_bootstrap() -> dict[str, Any]:
    return {
        "enabled": True, "tenant_id": INDUSTRIAL_TENANT, "default_persona_id": "persona-11",
        "persona_ids": ["persona-08", "persona-09", "persona-10", "persona-11"],
        "tbox_key": INDUSTRIAL_TBOX_KEY,
        "families": [{"key": "canalis-kt", "label": "Canalis KT 母线槽"},
                     {"key": "evopact-hvx-up24", "label": "EvoPacT HVX ≤24 kV 真空断路器"}],
        "retrieval_limits": asdict(INDUSTRIAL_RETRIEVAL_LIMITS),
        "rerank_profile": INDUSTRIAL_RERANK_PROFILE,
        "upload_uri_prefix": f"industrial-upload://{INDUSTRIAL_TENANT}/",
        "upload_chunk_chars": INDUSTRIAL_UPLOAD_CHUNK_CHARS,
        "upload_source_kind": "USER_UPLOAD", "source_inventory_endpoint": "/v1/industrial/sources:query",
        "authority_notice": "项目策划参考资料、合成案例和用户上传分开展示；不代表施耐德认证。",
    }


def verify_existing_industrial_runtime(driver: Any, database: str, embedding_space_id: str) -> None:
    tbox = Neo4jTBoxStore(driver, database).active(INDUSTRIAL_TENANT, INDUSTRIAL_TBOX_KEY)
    if tbox is None or tbox.status is not TBoxStatus.PUBLISHED:
        raise RuntimeError("industrial runtime requires the previously loaded published ontology")
    manager = Neo4jEmbeddingIndexManager(driver, database)
    generation = manager.active_generation(INDUSTRIAL_TENANT)
    if (generation is None or generation.state != "ACTIVE"
            or generation.embedding_space_id != embedding_space_id):
        raise RuntimeError("industrial runtime requires its initialized configured embedding generation")
    coverage = manager.coverage(generation.generation_id)
    if not coverage.complete or coverage.total_chunks == 0:
        raise RuntimeError("industrial runtime embedding coverage is incomplete")
    with driver.session(database=database) as session:
        row = session.run(Query("""
            MATCH (run:IndustrialCorpusLoad {tenant_id:$tenant,phase:'COMPLETE'})
            MATCH (state:TenantCorpusState {tenant_id:$tenant})
            MATCH (:KnowledgePublicationState {tenant_id:$tenant})-[:ACTIVE_KNOWLEDGE_PUBLICATION]->
                  (publication:KnowledgePublication {tenant_id:$tenant,status:'ACTIVE'})
            WHERE state.corpus_revision=$revision AND publication.ontology_version_id=$tbox_id
            RETURN run.load_id AS load_id LIMIT 1
            """, timeout=2.0), tenant=INDUSTRIAL_TENANT, revision=generation.corpus_revision,
            tbox_id=tbox.tbox_id).single()
    if row is None:
        raise RuntimeError("industrial runtime requires a completed industrial corpus load")


class TenantQueryOperations:
    """The authenticated tenant selects its engine and mandatory default scope."""
    def __init__(self, legacy: Any, industrial: Any) -> None:
        self.legacy, self.industrial = legacy, industrial

    def _dispatch(self, method: str, principal: Any, request: Any):
        if principal.tenant_id != INDUSTRIAL_TENANT:
            return getattr(self.legacy, method)(principal, request)
        updates = {}
        if request.industrial_scope is None:
            updates["industrial_scope"] = IndustrialScopeRequest()
        limits_field = "limits" if method == "retrieve" else "retrieval_limits"
        if getattr(request, limits_field) == RetrievalLimitsRequest():
            updates[limits_field] = RetrievalLimitsRequest.model_validate(asdict(INDUSTRIAL_RETRIEVAL_LIMITS))
        if updates:
            request = request.model_copy(update=updates)
        return getattr(self.industrial, method)(principal, request)

    def retrieve(self, principal: Any, request: Any):
        return self._dispatch("retrieve", principal, request)

    def answer(self, principal: Any, request: Any):
        return self._dispatch("answer", principal, request)


def build_industrial_query_operations(driver: Any, database: str, *, embedder: Any,
                                     generation_service: Any, api_key: str) -> GraphRAGQueryOperations:
    return GraphRAGQueryOperations(
        Neo4jRetrievalEngine(driver, database, transaction_timeout_seconds=30.0,
                            reranker=create_reranker(api_key=api_key, profile=INDUSTRIAL_RERANK_PROFILE)),
        embedder, generation_service,
        subgraph_projector=Neo4jEvidenceSubgraphProjector(driver, database),
        industrial_scope_resolver=Neo4jIndustrialScopeResolver(driver, database),
        include_industrial_rerank_context=True,
    )
