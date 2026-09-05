#!/usr/bin/env python3
"""Assemble and serve the local GraphRAG retrieval Playground."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import ipaddress
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import time
from typing import Any, Mapping
from urllib.parse import urlparse
import webbrowser

from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import neo4j
from neo4j import Query
import uvicorn

from graphrag_prod.api import (
    APISettings,
    GraphRAGApplicationBackend,
    GraphRAGQueryOperations,
    JWTAuthConfig,
    JWTAuthenticator,
    Neo4jKnowledgeOperations,
    ProviderUsage,
    QueryEmbedding,
    create_app,
)
from graphrag_prod.api.contracts import ReadinessResponse
from graphrag_prod.api.runtime import (
    AuthorizationError,
    BackendResult,
    DependencyUnavailableError,
    RuntimePolicy,
)
from graphrag_prod.construction import (
    ConstructionConfig,
    ExtractionLimits,
    Neo4jKnowledgeConstructionWorkflow,
    OpenAICompatibleOntologyExtractor,
)
from graphrag_prod.domain import Principal
from graphrag_prod.domain.ids import chunk_embedding_id, embedding_space_id
from graphrag_prod.domain.models import ChunkEmbedding
from graphrag_prod.generation import GroundedGenerationService
from graphrag_prod.graph.schema import apply_schema, verify_schema
from graphrag_prod.ingestion import (
    EmbeddingProfile,
    Neo4jEmbeddingIndexManager,
    Neo4jIncrementalPipeline,
    Neo4jIngestionService,
)
from graphrag_prod.playground import (
    PLAYGROUND_AUDIENCE,
    PLAYGROUND_ISSUER,
    PLAYGROUND_RETRIEVAL_LIMITS,
    PLAYGROUND_TOKEN_LIFETIME_SECONDS,
    PlaygroundCatalog,
    attach_playground_routes,
    require_loopback_host,
)
from graphrag_prod.retrieval import (
    Neo4jEvidenceSubgraphProjector,
    Neo4jRetrievalEngine,
    RetrievalBackendTimeout,
    RetrievalRequest as DomainRetrievalRequest,
)
from tests.fixtures.dev_corpus import load_dev_corpus_fixture


_PLAYGROUND_CONSTRUCTION_LIMITS = {
    "max_document_bytes": 5 * 1024 * 1024,
    "max_chunks": 4,
    "max_model_calls": 4,
    "max_total_extraction_chars": 16_000,
    "deadline_seconds": 90.0,
    "per_model_call_timeout_seconds": 30.0,
    "max_model_output_tokens": 2_048,
}
_PLAYGROUND_EXTRACTION_PROMPT = "industrial-property-graph-extraction:v2-token-spans"


def _build_playground_extractor(client, model, tbox):
    """DashScope extraction is bounded structured work, not deep reasoning."""
    return OpenAICompatibleOntologyExtractor(
        client=client.with_options(max_retries=0, timeout=30.0),
        model=model,
        active_tbox=tbox,
        prompt_version=_PLAYGROUND_EXTRACTION_PROMPT,
        response_format_mode="none",
        seed=None,
        enable_thinking=False,
        include_span_hints=True,
        limits=ExtractionLimits(
            max_response_chars=16_384,
            max_output_tokens=2_048,
            timeout_seconds=30.0,
        ),
    )


class _ReadOnlyDocuments:
    """Keep generic lifecycle routes closed; governed upload uses construction."""

    @staticmethod
    def ingest(*_args: object, **_kwargs: object) -> BackendResult:
        raise AuthorizationError()

    @staticmethod
    def delete(*_args: object, **_kwargs: object) -> BackendResult:
        raise AuthorizationError()

    @staticmethod
    def get_job(*_args: object, **_kwargs: object) -> BackendResult:
        raise AuthorizationError()


class _Neo4jReadiness:
    """Verify that retrieval can use one current, online vector space per tenant."""

    _CHECK_NAMES = ("neo4j", "embedding_generations", "vector_indexes")

    def __init__(
        self,
        driver: neo4j.Driver,
        database: str,
        *,
        expected_tenant_ids: tuple[str, ...],
    ) -> None:
        self.driver = driver
        self.database = database
        tenant_ids = tuple(sorted({item.strip() for item in expected_tenant_ids}))
        if (
            not tenant_ids
            or len(tenant_ids) != len(expected_tenant_ids)
            or any(not item for item in tenant_ids)
        ):
            raise ValueError("expected_tenant_ids must be unique and non-empty")
        self.expected_tenant_ids = tenant_ids

    @classmethod
    def _result(cls, checks: Mapping[str, str]) -> BackendResult:
        ready = all(checks.get(name) == "ok" for name in cls._CHECK_NAMES)
        return BackendResult(
            ReadinessResponse(
                status="ready" if ready else "not_ready",
                checks={name: checks.get(name, "error") for name in cls._CHECK_NAMES},
            )
        )

    def check(self) -> BackendResult:
        checks = {name: "error" for name in self._CHECK_NAMES}
        try:
            records, _, _ = self.driver.execute_query(
                Query(
                    """
                    MATCH (state:TenantCorpusState)
                    OPTIONAL MATCH (state)-[:ACTIVE_EMBEDDING_INDEX]->(
                        pointer:EmbeddingIndexGeneration
                    )
                    OPTIONAL MATCH (active:EmbeddingIndexGeneration {
                        tenant_id: state.tenant_id,
                        state: 'ACTIVE'
                    })
                    WITH state,
                         collect(DISTINCT pointer{
                             .generation_id,
                             .index_name,
                             .state,
                             .corpus_revision
                         }) AS pointers,
                         collect(DISTINCT active.generation_id) AS active_ids
                    RETURN state.tenant_id AS tenant_id,
                           state.corpus_revision AS corpus_revision,
                           pointers,
                           active_ids
                    ORDER BY tenant_id
                    """,
                    timeout=2.0,
                ),
                database_=self.database,
            )
        except Exception:
            return self._result(checks)
        checks["neo4j"] = "ok"

        try:
            generations: dict[str, Mapping[str, Any]] = {}
            for record in records:
                tenant_id = record["tenant_id"]
                pointers = record["pointers"]
                active_ids = record["active_ids"]
                if (
                    not isinstance(tenant_id, str)
                    or tenant_id in generations
                    or not isinstance(pointers, (list, tuple))
                    or len(pointers) != 1
                    or not isinstance(active_ids, (list, tuple))
                    or len(active_ids) != 1
                ):
                    raise ValueError("invalid active generation cardinality")
                generation = dict(pointers[0])
                if (
                    generation.get("generation_id") != active_ids[0]
                    or generation.get("state") != "ACTIVE"
                    or isinstance(record["corpus_revision"], bool)
                    or not isinstance(record["corpus_revision"], int)
                    or isinstance(generation.get("corpus_revision"), bool)
                    or not isinstance(generation.get("corpus_revision"), int)
                    or generation["corpus_revision"] != record["corpus_revision"]
                    or not isinstance(generation.get("index_name"), str)
                    or not generation["index_name"]
                ):
                    raise ValueError("invalid active generation state")
                generations[tenant_id] = generation
            if tuple(sorted(generations)) != self.expected_tenant_ids:
                raise ValueError("tenant corpus state coverage mismatch")
            index_names = tuple(
                generation["index_name"] for generation in generations.values()
            )
            if len(set(index_names)) != len(index_names):
                raise ValueError("active generations share a vector index")
        except (KeyError, TypeError, ValueError):
            return self._result(checks)
        checks["embedding_generations"] = "ok"

        try:
            index_records, _, _ = self.driver.execute_query(
                Query(
                    """
                    SHOW INDEXES YIELD name, type, state
                    WHERE name IN $index_names
                    RETURN name, type, state
                    """,
                    timeout=2.0,
                ),
                index_names=list(index_names),
                database_=self.database,
            )
            indexes = {
                record["name"]: (record["type"], record["state"])
                for record in index_records
            }
            if (
                set(indexes) != set(index_names)
                or len(indexes) != len(index_records)
                or any(shape != ("VECTOR", "ONLINE") for shape in indexes.values())
            ):
                raise ValueError("active vector index is unavailable")
        except Exception:
            return self._result(checks)
        checks["vector_indexes"] = "ok"
        return self._result(checks)


class _PlaygroundKnowledgeOperations(Neo4jKnowledgeOperations):
    """Keep the live vector generation aligned after document construction."""

    def __init__(
        self,
        *,
        driver: neo4j.Driver,
        construction: Any,
        embedder: _OpenAICompatibleEmbedder,
        database: str,
    ) -> None:
        super().__init__(driver=driver, construction=construction, database=database)
        self._driver = driver
        self._database = database
        self._embedder = embedder
        self._generation_lock = threading.Lock()

    def construct(self, principal: Principal, request: Any) -> BackendResult:
        # One local process serializes construction plus the matching index CAS.
        # This does not weaken the database-side source/publication CAS rules.
        with self._generation_lock:
            try:
                result = super().construct(principal, request)
            except Exception:
                # The evidence snapshot is intentionally published before LLM
                # extraction.  A provider failure may therefore advance the
                # corpus even though the API call fails; repair index parity
                # without masking the original bounded error.
                try:
                    self._refresh_embedding_generation(principal.tenant_id)
                except Exception:
                    pass
                raise
            self._refresh_embedding_generation(principal.tenant_id)
            return result

    def retire_document(
        self,
        principal: Principal,
        document_id: str,
        request: Any,
    ) -> BackendResult:
        # Retirement invalidates the active generation in the same database
        # transaction. An exact request replay remains safe if provider-backed
        # index rebuilding fails after the durable retirement commits.
        with self._generation_lock:
            result = super().retire_document(principal, document_id, request)
            try:
                self._refresh_embedding_generation(principal.tenant_id)
            except Exception as error:
                # Return no stale success while keeping provider/driver detail
                # out of the public API. The UI retains the exact operation key
                # so a replay can finish generation repair safely.
                raise DependencyUnavailableError() from error
            return result

    def _refresh_embedding_generation(self, tenant_id: str) -> None:
        manager = Neo4jEmbeddingIndexManager(self._driver, self._database)
        active = manager.active_generation(tenant_id)
        expected_active_generation_id: str | None
        if active is None:
            # Incremental ingestion deliberately detaches and marks the old
            # generation STALE before this adapter can prepare the replacement.
            # Recover the latest matching generation as the version baseline;
            # activation still uses a null CAS expectation and therefore fails
            # closed if another writer cut over meanwhile.
            previous_rows, _, _ = self._driver.execute_query(
                "MATCH (generation:EmbeddingIndexGeneration {"
                "tenant_id: $tenant_id, embedding_space_id: $embedding_space_id}) "
                "RETURN generation.generation_id AS generation_id, "
                "generation.generation_version AS generation_version, "
                "generation.corpus_revision AS corpus_revision "
                "ORDER BY generation.generation_version DESC LIMIT 2",
                tenant_id=tenant_id,
                embedding_space_id=self._embedder.embedding_space_id,
                database_=self._database,
            )
            if not previous_rows:
                raise RuntimeError(
                    "tenant has no prior Playground embedding generation"
                )
            baseline = previous_rows[0]
            baseline_generation_version = int(baseline["generation_version"])
            baseline_corpus_revision = int(baseline["corpus_revision"])
            expected_active_generation_id = None
        else:
            baseline_generation_version = active.generation_version
            baseline_corpus_revision = int(active.corpus_revision or 0)
            expected_active_generation_id = active.generation_id
        records, _, _ = self._driver.execute_query(
            "MATCH (state:TenantCorpusState {tenant_id: $tenant_id}) "
            "RETURN state.corpus_revision AS corpus_revision",
            tenant_id=tenant_id,
            database_=self._database,
        )
        if len(records) != 1:
            raise RuntimeError("tenant corpus revision is unavailable")
        if (
            active is not None
            and int(records[0]["corpus_revision"]) == baseline_corpus_revision
        ):
            return
        vector = (1.0,) + (0.0,) * (self._embedder.dimensions - 1)
        profile = ChunkEmbedding(
            embedding_id=chunk_embedding_id(
                "local-playground-generation-profile",
                self._embedder.embedding_space_id,
            ),
            tenant_id=tenant_id,
            chunk_id="local-playground-generation-profile",
            embedding_space_id=self._embedder.embedding_space_id,
            provider=self._embedder.provider,
            model=self._embedder.model,
            revision=self._embedder.revision,
            dimensions=self._embedder.dimensions,
            normalization=self._embedder.normalization,
            created_at=datetime.now(UTC),
            vector=vector,
        )
        target = manager.prepare(
            tenant_id=tenant_id,
            embedding_profile=profile,
            generation_version=baseline_generation_version + 1,
        )
        coverage = manager.coverage(target.generation_id)
        if not coverage.complete:
            raise RuntimeError("uploaded Chunk embedding coverage is incomplete")
        manager.activate(
            target.generation_id,
            expected_active_generation_id=expected_active_generation_id,
        )


class _DisabledAnswerModel:
    """Defensive adapter; Playground JWTs never authorize answer generation."""

    @staticmethod
    def generate(*_args: object, **_kwargs: object) -> object:
        raise AuthorizationError()


class _OpenAICompatibleEmbedder:
    """Embed queries and corpus text in one versioned provider vector space."""

    def __init__(
        self,
        client: OpenAI,
        *,
        provider: str,
        model: str,
        revision: str,
        dimensions: int,
    ) -> None:
        self.client = client
        self.provider = provider
        self.model = model
        self.revision = revision
        self.dimensions = dimensions
        self.normalization = "provider-default"
        self.embedding_space_id = embedding_space_id(
            provider,
            model,
            revision,
            dimensions,
            self.normalization,
        )

    def _request(self, texts: list[str]) -> list[tuple[float, ...]]:
        response = self.client.embeddings.create(
            input=texts,
            model=self.model,
            dimensions=self.dimensions,
            encoding_format="float",
        )
        ordered = sorted(response.data, key=lambda item: item.index)
        vectors = [tuple(float(value) for value in item.embedding) for item in ordered]
        if len(vectors) != len(texts):
            raise RuntimeError("embedding provider returned an unexpected result count")
        if any(len(vector) != self.dimensions for vector in vectors):
            raise RuntimeError("embedding provider returned an unexpected dimension")
        return vectors

    def embed_documents(self, texts: list[str]) -> list[tuple[float, ...]]:
        vectors: list[tuple[float, ...]] = []
        for offset in range(0, len(texts), 10):
            vectors.extend(self._request(texts[offset : offset + 10]))
        return vectors

    def __call__(
        self,
        *,
        chunk: object,
        **_kwargs: object,
    ) -> tuple[float, ...]:
        """Provide the Stage 3 keyword-call shape for uploaded Chunks."""

        text = getattr(chunk, "text", None)
        if not isinstance(text, str) or not text:
            raise ValueError("embedding Chunk must contain text")
        return self._request([text])[0]

    def embed(self, query_text: str, *, tenant_id: str):
        del tenant_id
        vector = self._request([query_text])[0]
        return QueryEmbedding(
            vector=vector,
            embedding_space_id=self.embedding_space_id,
            usage=ProviderUsage(model_calls=1),
        )


def _loopback_neo4j_uri(value: str) -> str:
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
        address = ipaddress.ip_address(hostname or "")
    except ValueError as error:
        raise ValueError(
            "Playground Neo4j URI must contain a loopback IP address"
        ) from error
    if parsed.scheme not in {"bolt", "neo4j"} or not address.is_loopback:
        raise ValueError("Playground Neo4j must use bolt/neo4j on a loopback address")
    if parsed.username or parsed.password or parsed.path not in {"", "/"}:
        raise ValueError("Playground Neo4j URI must not contain credentials or a path")
    if parsed.query or parsed.fragment or parsed.port is None:
        raise ValueError(
            "Playground Neo4j URI must contain only an explicit host and port"
        )
    return value


def _required_environment(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise RuntimeError(f"missing required local Playground setting: {name}")
    return value.strip()


def _empty_database(driver: neo4j.Driver, database: str) -> None:
    records, _, _ = driver.execute_query(
        "MATCH (node) WITH count(node) AS nodes "
        "OPTIONAL MATCH ()-[relationship]->() "
        "RETURN nodes, count(relationship) AS relationships",
        database_=database,
    )
    if (
        len(records) != 1
        or records[0]["nodes"] != 0
        or records[0]["relationships"] != 0
    ):
        raise RuntimeError(
            "refusing to initialize the Playground in a non-empty Neo4j database"
        )


def _load_corpus(
    driver: neo4j.Driver,
    database: str,
    embedder: _OpenAICompatibleEmbedder,
):
    print("[1/5] Verifying the versioned dev-corpus-v1 fixture", flush=True)
    fixture = load_dev_corpus_fixture()

    print("[2/5] Applying the production graph schema", flush=True)
    _empty_database(driver, database)
    apply_schema(driver, database)
    driver.execute_query("CALL db.awaitIndexes(60)", database_=database)
    errors = verify_schema(driver, database)
    if errors:
        raise RuntimeError("Playground schema verification failed: " + "; ".join(errors))

    print("[3/5] Ingesting 10 documents and 120 traceable Chunks", flush=True)
    service = Neo4jIngestionService(
        driver,
        database,
        worker_id="local-playground-loader",
    )
    for plan in fixture.plans:
        result = service.ingest(plan)
        if result.active_snapshot_id != plan.snapshot.snapshot_id:
            raise RuntimeError(f"Playground Snapshot did not activate: {plan.document_id}")

    print(
        f"      Generating {embedder.dimensions}-dimensional {embedder.model} embeddings",
        flush=True,
    )
    manager = Neo4jEmbeddingIndexManager(driver, database)
    tenant_bundles: dict[str, list[Any]] = {}
    for plan in fixture.plans:
        tenant_bundles.setdefault(plan.tenant_id, []).extend(plan.bundles)
    created_at = datetime.now(UTC)
    for tenant_id, bundles in sorted(tenant_bundles.items()):
        vectors = embedder.embed_documents([bundle.chunk.text for bundle in bundles])
        embeddings = tuple(
            ChunkEmbedding(
                embedding_id=chunk_embedding_id(
                    bundle.chunk.chunk_id,
                    embedder.embedding_space_id,
                ),
                tenant_id=tenant_id,
                chunk_id=bundle.chunk.chunk_id,
                embedding_space_id=embedder.embedding_space_id,
                provider=embedder.provider,
                model=embedder.model,
                revision=embedder.revision,
                dimensions=embedder.dimensions,
                normalization=embedder.normalization,
                created_at=created_at,
                vector=vector,
            )
            for bundle, vector in zip(bundles, vectors, strict=True)
        )
        if manager.materialize(embeddings) != len(embeddings):
            raise RuntimeError(f"Playground embedding materialization failed: {tenant_id}")
        generation = manager.prepare(
            tenant_id=tenant_id,
            embedding_profile=embeddings[0],
            generation_version=1,
        )
        coverage = manager.coverage(generation.generation_id)
        if not coverage.complete:
            raise RuntimeError(f"Playground embedding coverage is incomplete: {tenant_id}")
        manager.activate(
            generation.generation_id,
            expected_active_generation_id=None,
        )
    driver.execute_query(
        "CALL db.index.fulltext.awaitEventuallyConsistentIndexRefresh()",
        database_=database,
    )
    return fixture


def _warm_retrieval(
    engine: Neo4jRetrievalEngine,
    fixture: Any,
    embedder: _OpenAICompatibleEmbedder,
) -> None:
    """Compile one complete bounded retrieval path for each tenant."""

    print("[4/5] Warming the bounded retrieval path for both tenants", flush=True)
    questions_by_tenant: dict[str, Mapping[str, Any]] = {}
    for question in fixture.build.questions:
        tenant_id = str(question["principal"]["tenant_id"])
        if bool(question["answerable"]):
            questions_by_tenant.setdefault(tenant_id, question)
    expected_tenants = {plan.tenant_id for plan in fixture.plans}
    if set(questions_by_tenant) != expected_tenants:
        raise RuntimeError("Playground warm-up questions do not cover every tenant")

    for tenant_id, question in sorted(questions_by_tenant.items()):
        principal = question["principal"]
        query_embedding = embedder.embed(str(question["query"]), tenant_id=tenant_id)
        request = DomainRetrievalRequest(
            query_text=str(question["query"]),
            query_vector=query_embedding.vector,
            principal=Principal(
                principal_id=str(principal["principal_id"]),
                tenant_id=tenant_id,
                groups=frozenset(str(item) for item in principal["groups"]),
            ),
            query_embedding_space_id=query_embedding.embedding_space_id,
            limits=PLAYGROUND_RETRIEVAL_LIMITS,
        )
        try:
            result = engine.retrieve(request)
        except RetrievalBackendTimeout:
            print(
                f"      Retrieval warm-up timed out for {tenant_id}; "
                "retrying once with the same bounds and query embedding",
                flush=True,
            )
            result = engine.retrieve(request)
        if result.trace.tenant_id != tenant_id or not result.chunks:
            raise RuntimeError(f"Playground retrieval warm-up failed: {tenant_id}")


def build_playground_app(
    driver: neo4j.Driver,
    database: str,
    *,
    signing_key: bytes,
    embedder: _OpenAICompatibleEmbedder,
    extraction_model: str = "qwen-plus",
):
    fixture = _load_corpus(driver, database, embedder)
    catalog = PlaygroundCatalog(
        fixture,
        signing_key,
        embedding_metadata={
            "provider": embedder.provider,
            "model": embedder.model,
            "dimensions": embedder.dimensions,
            "warning": "External provider embeddings; usage may incur cost.",
        },
        capabilities={
            "document_upload": True,
            "ontology_governance": True,
            "human_review": True,
            "knowledge_publication": True,
            "published_graph_quality": True,
            "published_graph_quality_history": True,
            "active_abox_inventory": True,
            "document_retirement": True,
            "evidence_subgraph": True,
            "extraction_provider": {
                "protocol": "openai-compatible",
                "model": extraction_model,
                "purpose": "ontology-constrained extraction only",
                "enable_thinking": False,
                "span_hints": "unicode-token-spans-v1",
            },
            "construction_limits": dict(_PLAYGROUND_CONSTRUCTION_LIMITS),
        },
    )
    retrieval_engine = Neo4jRetrievalEngine(
        driver,
        database,
        transaction_timeout_seconds=30.0,
    )
    _warm_retrieval(retrieval_engine, fixture, embedder)
    query_operations = GraphRAGQueryOperations(
        retrieval_engine,
        embedder,
        GroundedGenerationService(_DisabledAnswerModel()),
        subgraph_projector=Neo4jEvidenceSubgraphProjector(driver, database),
    )
    prompt_signature = _PLAYGROUND_EXTRACTION_PROMPT
    construction = Neo4jKnowledgeConstructionWorkflow(
        driver=driver,
        database=database,
        pipeline=Neo4jIncrementalPipeline(
            driver,
            database,
            worker_id="local-playground-construction",
        ),
        embedding_provider=embedder,
        embedding_profile=EmbeddingProfile(
            embedder.provider,
            embedder.model,
            embedder.revision,
            embedder.dimensions,
            embedder.normalization,
        ),
        extractor_factory=lambda tbox: _build_playground_extractor(
            embedder.client, extraction_model, tbox,
        ),
        config=ConstructionConfig(
            extractor_signature=f"openai-compatible:{extraction_model}:v2",
            prompt_signature=prompt_signature,
            max_chunks=_PLAYGROUND_CONSTRUCTION_LIMITS["max_chunks"],
            max_model_calls=_PLAYGROUND_CONSTRUCTION_LIMITS["max_model_calls"],
            max_total_extraction_chars=_PLAYGROUND_CONSTRUCTION_LIMITS[
                "max_total_extraction_chars"
            ],
            deadline_seconds=_PLAYGROUND_CONSTRUCTION_LIMITS["deadline_seconds"],
        ),
    )
    knowledge_operations = _PlaygroundKnowledgeOperations(
        driver=driver,
        database=database,
        construction=construction,
        embedder=embedder,
    )
    backend = GraphRAGApplicationBackend(
        documents=_ReadOnlyDocuments(),
        queries=query_operations,
        readiness=_Neo4jReadiness(
            driver,
            database,
            expected_tenant_ids=tuple(
                sorted({plan.tenant_id for plan in fixture.plans})
            ),
        ),
        knowledge=knowledge_operations,
    )
    app = create_app(
        authenticator=JWTAuthenticator(
            JWTAuthConfig(
                issuer=PLAYGROUND_ISSUER,
                audience=PLAYGROUND_AUDIENCE,
                secret=signing_key,
                leeway_seconds=0,
                max_lifetime_seconds=PLAYGROUND_TOKEN_LIFETIME_SECONDS,
            )
        ),
        backend=backend,
        settings=APISettings(
            service_name="sample-graphrag-local-playground",
            version="1.1.0",
            expose_openapi=True,
        ),
        runtime_policy=RuntimePolicy(
            max_workers=8,
            max_queue_size=8,
            # A small document can require several sequential extraction calls;
            # every provider call is independently capped by the extractor.
            timeout_seconds=105.0,
            max_attempts=1,
        ),
        shutdown_callbacks=(driver.close,),
    )
    attach_playground_routes(app, catalog)
    app.state.playground_gold_questions = tuple(fixture.build.questions)
    return app


def _run_http_check(app: Any) -> None:
    """Exercise the browser's authenticated API path and all reviewed cases."""

    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        page = client.get("/playground")
        if page.status_code != 200 or "GraphRAG Local Playground" not in page.text:
            raise RuntimeError("Playground page check failed")
        bootstrap_response = client.get("/playground/bootstrap")
        if bootstrap_response.status_code != 200:
            raise RuntimeError("Playground bootstrap check failed")
        bootstrap = bootstrap_response.json()
        personas = {item["id"]: item for item in bootstrap["personas"]}
        tokens: dict[str, str] = {}
        for persona_id in personas:
            response = client.post(
                "/playground/session",
                json={"persona_id": persona_id},
            )
            if response.status_code != 200:
                raise RuntimeError(f"Playground session check failed: {persona_id}")
            tokens[persona_id] = str(response.json()["access_token"])

        retrieval_limits = bootstrap["defaults"]["retrieval_limits"]
        questions = bootstrap["questions"]
        default_question = next(
            item
            for item in questions
            if item["id"] == bootstrap["defaults"]["question_id"]
        )
        default_persona_id = default_question["recommended_persona_id"]
        headers = {"Authorization": f"Bearer {tokens[default_persona_id]}"}
        retrieval_response = client.post(
            "/v1/retrieval",
            headers=headers,
            json={
                "query_text": default_question["query"],
                "limits": retrieval_limits,
                "include_graph": True,
            },
        )
        if retrieval_response.status_code != 200 or not retrieval_response.json()["chunks"]:
            raise RuntimeError("Playground default retrieval check failed")
        if "graph" not in retrieval_response.json():
            raise RuntimeError("Playground graph projection contract check failed")
        default_chunk_ids = {
            item["citation"]["chunk_id"]
            for item in retrieval_response.json()["chunks"]
        }

        answer_response = client.post(
            "/v1/answers",
            headers=headers,
            json={
                "query_text": default_question["query"],
                "retrieval_limits": retrieval_limits,
            },
        )
        if answer_response.status_code != 403:
            raise RuntimeError("Playground token unexpectedly authorized answers")

        ontology_response = client.get("/v1/ontologies", headers=headers)
        if ontology_response.status_code != 200 or ontology_response.json() != {"items": []}:
            raise RuntimeError("Playground ontology workspace check failed")

        failures: list[str] = []
        actual_rankings: list[dict[str, Any]] = []
        actual_selected: list[dict[str, Any]] = []
        for question in questions:
            persona_id = question["recommended_persona_id"]
            response = client.post(
                "/v1/retrieval",
                headers={"Authorization": f"Bearer {tokens[persona_id]}"},
                json={
                    "query_text": question["query"],
                    "limits": retrieval_limits,
                },
            )
            if response.status_code != 200:
                failures.append(str(question["id"]))
                continue
            payload = response.json()
            trace = payload["trace"]
            minimum_channels = int(trace["limits"]["minimum_rrf_channels"])
            ranking = [
                str(hit["chunk_id"])
                for hit in trace["final_ranking"]
                if len(hit["ranks"]) >= minimum_channels
            ]
            visible_resources = [
                {
                    "stage": stage,
                    "kind": "chunk",
                    "id": str(hit["chunk_id"]),
                }
                for stage in (
                    "vector_recall",
                    "bm25_recall",
                    "seed_ranking",
                    "graph_expansion",
                    "candidate_vector_ranking",
                    "final_ranking",
                )
                for hit in trace[stage]
            ]
            visible_resources.extend(
                {
                    "stage": "selected_context",
                    "kind": "chunk",
                    "id": str(chunk_id),
                }
                for chunk_id in trace["selected_chunk_ids"]
            )
            actual_rankings.append(
                {
                    "id": str(question["id"]),
                    "ranking": ranking,
                    "visible_resources": visible_resources,
                }
            )
            actual_selected.append(
                {
                    "id": str(question["id"]),
                    "ranking": [str(value) for value in trace["selected_chunk_ids"]],
                    "visible_resources": visible_resources,
                }
            )
        if failures:
            raise RuntimeError(
                "Playground reviewed retrieval cases failed: " + ", ".join(failures)
            )
        from graphrag_prod.retrieval.metrics import evaluate_retrieval_results

        gold_questions = list(app.state.playground_gold_questions)
        ranking_metrics = evaluate_retrieval_results(gold_questions, actual_rankings)
        selected_metrics = evaluate_retrieval_results(gold_questions, actual_selected)
        if (
            ranking_metrics.evidence_recall_at_5 < 0.80
            or ranking_metrics.mrr < 0.80
            or ranking_metrics.unauthorized_exposure_count != 0
            or selected_metrics.evidence_recall_at_5 < 0.80
            or selected_metrics.mrr < 0.80
            or selected_metrics.unauthorized_exposure_count != 0
        ):
            raise RuntimeError(
                "Playground reviewed retrieval metrics failed: "
                f"ranking={ranking_metrics}; selected={selected_metrics}"
            )
        print(
            "      Provider retrieval smoke metrics: "
            f"ranking={ranking_metrics}; selected={selected_metrics}",
            flush=True,
        )

        custom_response = client.post(
            "/v1/retrieval",
            headers=headers,
            json={
                "query_text": "Northstar revenue 2024",
                "limits": bootstrap["defaults"]["retrieval_limits"],
            },
        )
        if custom_response.status_code != 200:
            raise RuntimeError("Playground custom hybrid check failed")
        custom_trace = custom_response.json()["trace"]
        if not custom_trace["vector_recall"] or not custom_trace["bm25_recall"]:
            raise RuntimeError("Playground custom query did not use hybrid recall")

        default_tenant = personas[default_persona_id]["tenant_id"]
        other_persona_id = next(
            persona_id
            for persona_id, persona in personas.items()
            if persona["tenant_id"] != default_tenant
        )
        altered_response = client.post(
            "/v1/retrieval",
            headers={"Authorization": f"Bearer {tokens[other_persona_id]}"},
            json={
                "query_text": default_question["query"],
                "limits": retrieval_limits,
            },
        )
        altered_payload = altered_response.json()
        altered_chunk_ids = {
            item["citation"]["chunk_id"] for item in altered_payload.get("chunks", [])
        }
        if (
            altered_response.status_code != 200
            or altered_payload.get("trace", {}).get("tenant_id")
            != personas[other_persona_id]["tenant_id"]
            or altered_chunk_ids.intersection(default_chunk_ids)
        ):
            raise RuntimeError("Playground altered-persona ACL check failed")

        readiness = client.get("/health/ready")
        if readiness.status_code != 200 or readiness.json().get("status") != "ready":
            raise RuntimeError("Playground readiness check failed")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    parser.add_argument(
        "--check",
        action="store_true",
        help="run the authenticated HTTP smoke suite and exit",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    host = require_loopback_host(args.host)
    if not 1 <= args.port <= 65_535:
        raise SystemExit("--port must be between 1 and 65535")

    load_dotenv(ROOT / ".env")
    api_key = _required_environment("OPENAI_API_KEY")
    base_url = _required_environment("OPENAI_BASE_URL")
    embedding_model = _required_environment("EMBEDDING_MODEL")
    extraction_model = _required_environment("MODEL_NAME")
    try:
        embedding_dimensions = int(_required_environment("EMBEDDING_DIMENSIONS"))
    except ValueError as error:
        raise SystemExit("EMBEDDING_DIMENSIONS must be an integer") from error
    if embedding_dimensions not in {64, 128, 256, 512, 768, 1024, 1536, 2048}:
        raise SystemExit("EMBEDDING_DIMENSIONS is not supported by text-embedding-v4")
    parsed_provider_url = urlparse(base_url)
    allowed_dashscope_hosts = {
        "dashscope.aliyuncs.com",
        "dashscope-intl.aliyuncs.com",
        "dashscope-us.aliyuncs.com",
    }
    if (
        parsed_provider_url.scheme != "https"
        or parsed_provider_url.hostname not in allowed_dashscope_hosts
        or parsed_provider_url.username is not None
        or parsed_provider_url.password is not None
    ):
        raise SystemExit("OPENAI_BASE_URL must be an official DashScope HTTPS endpoint")
    embedder = _OpenAICompatibleEmbedder(
        OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=30.0,
            max_retries=2,
        ),
        provider="dashscope-openai-compatible",
        model=embedding_model,
        revision="api-v1",
        dimensions=embedding_dimensions,
    )

    if _required_environment("PLAYGROUND_ALLOW_DISPOSABLE_DB") != "1":
        raise SystemExit(
            "PLAYGROUND_ALLOW_DISPOSABLE_DB=1 is required before fixture data may be loaded"
        )
    uri = _loopback_neo4j_uri(_required_environment("PLAYGROUND_NEO4J_URI"))
    username = _required_environment("PLAYGROUND_NEO4J_USER")
    password = _required_environment("PLAYGROUND_NEO4J_PASSWORD")
    database = os.getenv("PLAYGROUND_NEO4J_DATABASE", "neo4j").strip()
    if database != "neo4j":
        raise SystemExit("the disposable Playground supports only the neo4j database")

    driver = neo4j.GraphDatabase.driver(
        uri,
        auth=(username, password),
        connection_timeout=5.0,
        max_connection_pool_size=16,
    )
    try:
        deadline = time.monotonic() + 90.0
        while True:
            try:
                driver.verify_connectivity()
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise RuntimeError("local Playground Neo4j did not become ready")
                time.sleep(1.0)

        app = build_playground_app(
            driver,
            database,
            signing_key=secrets.token_bytes(32),
            embedder=embedder,
            extraction_model=extraction_model,
        )
    except Exception:
        driver.close()
        raise

    if args.check:
        print("[5/5] Running all 49 reviewed HTTP cases", flush=True)
        _run_http_check(app)
        print(
            "Playground check passed: 49/49 HTTP cases, provider smoke metrics, "
            "hybrid recall, and ACL",
            flush=True,
        )
        return

    url = f"http://{f'[{host}]' if ':' in host else host}:{args.port}/playground"
    print(f"[5/5] Playground ready: {url}", flush=True)
    print(
        "      Press Ctrl-C to stop; the shell launcher removes its container.",
        flush=True,
    )
    if not args.no_open:
        timer = threading.Timer(0.8, webbrowser.open, args=(url,))
        timer.daemon = True
        timer.start()
    uvicorn.run(app, host=host, port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
