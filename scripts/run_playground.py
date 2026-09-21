#!/usr/bin/env python3
"""Serve the existing local knowledge workbench without loading legacy corpora."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import ipaddress
import json
import os
import re
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
    UploadInProgressError,
)
from graphrag_prod.construction import (
    ConstructionConfig,
    ExtractionLimits,
    Neo4jKnowledgeConstructionWorkflow,
    OpenAICompatibleOntologyExtractor,
)
from graphrag_prod.domain import Principal
from graphrag_prod.domain.ids import embedding_space_id, chunk_embedding_id
from graphrag_prod.domain.models import ChunkEmbedding
from graphrag_prod.generation import GroundedGenerationService
from graphrag_prod.ingestion import (
    EmbeddingProfile,
    Neo4jEmbeddingIndexManager,
    Neo4jIncrementalPipeline,
)
from graphrag_prod.playground import (
    PLAYGROUND_AUDIENCE,
    PLAYGROUND_ISSUER,
    PLAYGROUND_TOKEN_LIFETIME_SECONDS,
    PlaygroundCatalog,
    attach_playground_routes,
    require_loopback_host,
)
from graphrag_prod.retrieval import (
    Neo4jEvidenceSubgraphProjector,
    Neo4jRetrievalEngine,
)
from graphrag_prod.playground.demo_corpus import load_demo_corpus
from graphrag_prod.construction.structured import StructuredDocumentParser, select_structured_extractor


_PLAYGROUND_CONSTRUCTION_LIMITS = json.loads(
    (ROOT / "contracts/profiles/workbench-construction.v2.json").read_text(encoding="utf-8")
)
_PLAYGROUND_EXTRACTION_PROMPT = "industrial-property-graph-extraction:v6-exact-json-spans"


def _build_playground_extractor(client, model, tbox):
    """DashScope extraction is bounded structured work, not deep reasoning."""
    return OpenAICompatibleOntologyExtractor(
        client=client.with_options(
            max_retries=0,
            timeout=_PLAYGROUND_CONSTRUCTION_LIMITS["per_model_call_timeout_seconds"],
        ),
        model=model,
        active_tbox=tbox,
        prompt_version=_PLAYGROUND_EXTRACTION_PROMPT,
        response_format_mode="json_object",
        seed=None,
        enable_thinking=False,
        include_span_hints=True,
        max_validation_attempts=_PLAYGROUND_CONSTRUCTION_LIMITS["max_validation_attempts"],
        limits=ExtractionLimits(
            max_response_chars=_PLAYGROUND_CONSTRUCTION_LIMITS["max_model_response_chars"],
            max_output_tokens=_PLAYGROUND_CONSTRUCTION_LIMITS["max_model_output_tokens"],
            timeout_seconds=_PLAYGROUND_CONSTRUCTION_LIMITS["per_model_call_timeout_seconds"],
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
                    WHERE state.tenant_id IN $enabled_tenants
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
                enabled_tenants=list(self.expected_tenant_ids),
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
        auto_review_service: Any | None = None,
        publications: Any | None = None,
    ) -> None:
        super().__init__(driver=driver, construction=construction, database=database,
                         auto_review_service=auto_review_service, publications=publications)
        self._driver = driver
        self._database = database
        self._embedder = embedder
        self._generation_lock = threading.Lock()

    def _prepare_source_index(self, principal: Principal) -> None:
        self._refresh_embedding_generation(principal.tenant_id)

    def publish(self, principal: Principal, request: Any, *, preview_only: bool = False) -> BackendResult:
        # Repair derived index state before opening the publication transaction.
        # Exact current-corpus coverage and the existing publication guards still
        # decide readiness; no candidate knowledge is published by this repair.
        from graphrag_prod.api.knowledge import _require_capability
        _require_capability(principal, "knowledge:publish")
        if not self._generation_lock.acquire(blocking=False):
            raise UploadInProgressError()
        try:
            try:
                self._refresh_embedding_generation(principal.tenant_id)
            except Exception as error:
                raise DependencyUnavailableError() from error
            return super().publish(principal, request, preview_only=preview_only)
        finally:
            self._generation_lock.release()

    def construct(self, principal: Principal, request: Any) -> BackendResult:
        # One local process serializes construction plus the matching index CAS.
        # This does not weaken the database-side source/publication CAS rules.
        if not self._generation_lock.acquire(blocking=False):
            raise UploadInProgressError()
        try:
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
        finally:
            self._generation_lock.release()

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
            # An interrupted prepare has no corpus revision yet. It may be
            # reused deterministically; it must not make int(None) block repair.
            baseline = previous_rows[0] if previous_rows else {}
            baseline_generation_version = int(baseline.get("generation_version") or 0)
            if baseline and baseline.get("corpus_revision") is None:
                baseline_generation_version -= 1
            baseline_corpus_revision = int(baseline.get("corpus_revision") or 0)
            expected_active_generation_id = None
        else:
            if active.embedding_space_id != self._embedder.embedding_space_id or active.dimensions != self._embedder.dimensions:
                raise RuntimeError("active index differs from configured embedding space")
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
            active is not None and active.state == "ACTIVE"
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


def _official_provider_base_url(value: str) -> str:
    """Accept DashScope and documented, workspace-scoped MaaS endpoints."""
    try:
        parsed = urlparse(value)
        host = parsed.hostname or ""
        legacy = host in {
            "dashscope.aliyuncs.com",
            "dashscope-intl.aliyuncs.com",
            "dashscope-us.aliyuncs.com",
        }
        workspace = re.fullmatch(
            r"ws-[a-z0-9]+\.(?:cn-beijing|ap-southeast-1|eu-central-1|"
            r"ap-northeast-1|cn-hongkong|us-east-1)\.maas\.aliyuncs\.com",
            host,
        ) is not None
        valid = (
            not any(character.isspace() or ord(character) < 32 for character in value)
            and parsed.scheme == "https"
            and (legacy or workspace)
            and parsed.username is None
            and parsed.password is None
            and parsed.port in {None, 443}
            and parsed.path.rstrip("/") == "/compatible-mode/v1"
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("OPENAI_BASE_URL must be an official DashScope or workspace MaaS HTTPS endpoint")
    return value.rstrip("/")


def _required_environment(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise RuntimeError(f"missing required local Playground setting: {name}")
    return value.strip()


def build_playground_app(
    driver: neo4j.Driver,
    database: str,
    *,
    signing_key: bytes,
    embedder: _OpenAICompatibleEmbedder,
    extraction_model: str = "qwen-plus",
    pump_only: bool = True,
):
    if pump_only is not True:
        raise ValueError("the current workbench requires PLAYGROUND_PUMP_ONLY=1")
    fixture = load_demo_corpus()
    from graphrag_prod.playground.workspaces import Neo4jWorkspaceStore
    visible_projects = Neo4jWorkspaceStore(driver, database, pump_only=True).list()
    if not visible_projects:
        raise ValueError("the workbench requires an existing isolated project")
    enabled_tenants = tuple(sorted(p["active_tenant_id"] for p in visible_projects))
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
            "source_only_upload": True,
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
                "response_format": "json_object",
                "prompt_version": _PLAYGROUND_EXTRACTION_PROMPT,
            },
            "construction_limits": dict(_PLAYGROUND_CONSTRUCTION_LIMITS),
        },
    )
    catalog.pump_only = True
    retrieval_engine = Neo4jRetrievalEngine(
        driver,
        database,
        transaction_timeout_seconds=30.0,
    )
    print("External provider warm-up skipped; uploads and queries use the configured provider.", flush=True)
    query_operations = GraphRAGQueryOperations(
        retrieval_engine,
        embedder,
        GroundedGenerationService(_DisabledAnswerModel()),
        subgraph_projector=Neo4jEvidenceSubgraphProjector(driver, database),
    )
    from graphrag_prod.api.graph import Neo4jGraphOperations
    from graphrag_prod.graph.browsing import Neo4jPublishedGraphBrowser
    from graphrag_prod.graph.visualization import GraphVisualizationPreparationQueue, prepare_publication_visualizations
    graph_operations = Neo4jGraphOperations(browser=Neo4jPublishedGraphBrowser(driver, database))
    graph_preparations = GraphVisualizationPreparationQueue(graph_operations.browser)
    prompt_signature = _PLAYGROUND_EXTRACTION_PROMPT
    construction_embedder = _OpenAICompatibleEmbedder(
        embedder.client.with_options(
            max_retries=0,
            timeout=_PLAYGROUND_CONSTRUCTION_LIMITS["per_embedding_call_timeout_seconds"],
        ),
        provider=embedder.provider, model=embedder.model, revision=embedder.revision,
        dimensions=embedder.dimensions,
    )
    construction = Neo4jKnowledgeConstructionWorkflow(
        driver=driver,
        database=database,
        parser=StructuredDocumentParser(),
        document_extractor_selector=select_structured_extractor,
        pipeline=Neo4jIncrementalPipeline(
            driver,
            database,
            worker_id="local-playground-construction",
        ),
        embedding_provider=construction_embedder,
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
            extractor_signature=f"openai-compatible:{extraction_model}:v3",
            prompt_signature=prompt_signature,
            max_chunks=_PLAYGROUND_CONSTRUCTION_LIMITS["max_chunks"],
            max_concurrency=_PLAYGROUND_CONSTRUCTION_LIMITS["max_concurrency"],
            embedding_call_timeout_seconds=_PLAYGROUND_CONSTRUCTION_LIMITS["per_embedding_call_timeout_seconds"],
            max_model_calls=_PLAYGROUND_CONSTRUCTION_LIMITS["max_model_calls"],
            max_total_extraction_chars=_PLAYGROUND_CONSTRUCTION_LIMITS[
                "max_total_extraction_chars"
            ],
            deadline_seconds=_PLAYGROUND_CONSTRUCTION_LIMITS["deadline_seconds"],
        ),
    )
    from graphrag_prod.knowledge.auto_review import Neo4jAutoReviewService
    from graphrag_prod.knowledge.auto_review_model import OpenAICompatibleAutoReviewer
    from graphrag_prod.knowledge.context_projection import Neo4jContextProjectionService
    from graphrag_prod.construction.context_mapping import OpenAICompatibleContextMapper
    from graphrag_prod.knowledge.review import Neo4jKnowledgePublicationService

    def prepare_published_graph(principal, publication):
        # Server-owned local workspace identities enumerate the relevant ACL
        # views; never widen the caller's groups to produce a public file.
        project = workspace_registry.store.active(principal.tenant_id)
        if project is None:
            return
        readers = tuple(Principal(persona.principal_id, persona.tenant_id,
                frozenset(persona.groups), frozenset(persona.scopes))
            for persona in workspace_registry.personas(project))
        graph_preparations.submit(readers, publication.publication_id)

    knowledge_operations = _PlaygroundKnowledgeOperations(
        driver=driver,
        database=database,
        construction=construction,
        embedder=embedder,
        publications=Neo4jKnowledgePublicationService(driver, database, on_activated=prepare_published_graph),
        auto_review_service=Neo4jAutoReviewService(
            driver, database,
            reviewer=OpenAICompatibleAutoReviewer(client=embedder.client, model=extraction_model, enable_thinking=False),
            context_projection=Neo4jContextProjectionService(driver, database,
                planner=OpenAICompatibleContextMapper(client=embedder.client, model=extraction_model, enable_thinking=False)),
        ),
    )
    backend = GraphRAGApplicationBackend(
        documents=_ReadOnlyDocuments(),
        queries=query_operations,
        readiness=_Neo4jReadiness(
            driver,
            database,
            expected_tenant_ids=enabled_tenants,
        ),
        knowledge=knowledge_operations,
        **({"graph": graph_operations} if graph_operations is not None else {}),
    )
    auth_config = JWTAuthConfig(
                issuer=PLAYGROUND_ISSUER,
                audience=PLAYGROUND_AUDIENCE,
                secret=signing_key,
                leeway_seconds=0,
                max_lifetime_seconds=PLAYGROUND_TOKEN_LIFETIME_SECONDS,
            )
    from graphrag_prod.playground.workspaces import (
        WorkspaceRegistry, WorkspaceAuthenticator, attach_workspace_routes, prepare_workspace_tenant,
    )
    workspace_store = Neo4jWorkspaceStore(driver, database, pump_only=True)
    workspace_registry = WorkspaceRegistry(
        workspace_store, signing_key,
        lambda tenant_id: prepare_workspace_tenant(driver, database, embedder, tenant_id),
    )
    catalog.workspaces = workspace_registry
    # One-time migration for existing active publications. These calls only read
    # governed knowledge and write private JSON presentation artifacts.
    prepared = 0
    for project in visible_projects:
        for persona in workspace_registry.personas(project):
            reader = Principal(persona.principal_id, persona.tenant_id,
                frozenset(persona.groups), frozenset(persona.scopes))
            prepared += prepare_publication_visualizations(graph_operations.browser, reader)
    print(f"Published graph visualization views prepared: {prepared}.", flush=True)
    backend = workspace_registry.wrap_backend(backend)
    authenticator = WorkspaceAuthenticator(auth_config, workspace_registry)
    app = create_app(
        authenticator=authenticator,
        backend=backend,
        settings=APISettings(
            service_name="sample-graphrag-local-playground",
            version="1.1.0",
            expose_openapi=True,
        ),
        runtime_policy=RuntimePolicy(
            max_workers=8,
            max_queue_size=8,
            # Whole-document construction has a separate, bounded deadline;
            # interactive reads retain their existing request timeout.
            timeout_seconds=105.0,
            construction_timeout_seconds=_PLAYGROUND_CONSTRUCTION_LIMITS["http_timeout_seconds"],
            max_attempts=1,
        ),
        shutdown_callbacks=(graph_preparations.close, driver.close),
    )
    app.state.pump_only = pump_only
    attach_playground_routes(app, catalog)
    attach_workspace_routes(app, workspace_registry, authenticator)
    return app


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8002)
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    return parser


def main() -> None:
    args = _parser().parse_args()
    host = require_loopback_host(args.host)
    if not 1 <= args.port <= 65_535:
        raise SystemExit("--port must be between 1 and 65535")

    load_dotenv(ROOT / ".env")
    pump_only = os.environ.get("PLAYGROUND_PUMP_ONLY") == "1"
    if not pump_only:
        raise SystemExit("PLAYGROUND_PUMP_ONLY=1 is required; use ./quick_start.command")
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
    try:
        base_url = _official_provider_base_url(base_url)
    except ValueError as error:
        raise SystemExit(str(error)) from error
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

    uri = _loopback_neo4j_uri(_required_environment("PLAYGROUND_NEO4J_URI"))
    username = _required_environment("PLAYGROUND_NEO4J_USER")
    password = _required_environment("PLAYGROUND_NEO4J_PASSWORD")
    database = os.getenv("PLAYGROUND_NEO4J_DATABASE", "neo4j").strip()
    if database != "neo4j":
        raise SystemExit("the local workbench supports only the existing neo4j database")

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
            pump_only=pump_only,
        )
    except Exception:
        driver.close()
        raise

    url = f"http://{f'[{host}]' if ':' in host else host}:{args.port}/industrial"
    print(f"[5/5] Playground ready: {url}", flush=True)
    print("Press Ctrl-C to stop; existing data is preserved.", flush=True)
    if not args.no_open:
        timer = threading.Timer(0.8, webbrowser.open, args=(url,))
        timer.daemon = True
        timer.start()
    uvicorn.run(app, host=host, port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
