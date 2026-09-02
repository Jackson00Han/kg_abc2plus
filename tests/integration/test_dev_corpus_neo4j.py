"""Real-Neo4j validation for the representative Stage 5A corpus."""

from __future__ import annotations

from dataclasses import asdict
import ipaddress
import os
import time
import unittest
from urllib.parse import urlparse

from fastapi.testclient import TestClient
import jwt
import neo4j

from graphrag_prod.api.app import create_app
from graphrag_prod.api.auth import JWTAuthConfig, JWTAuthenticator
from graphrag_prod.api.backend import (
    GraphRAGApplicationBackend,
    GraphRAGQueryOperations,
    QueryEmbedding,
)
from graphrag_prod.api.contracts import ReadinessResponse, RetrievalResponse
from graphrag_prod.api.runtime import BackendResult
from graphrag_prod.domain import Principal
from graphrag_prod.generation import (
    AnswerModelRequest,
    AnswerStatus,
    GenerationRequest,
    GroundedGenerationService,
)
from graphrag_prod.graph.schema import apply_schema, verify_schema
from graphrag_prod.ingestion import (
    Neo4jEmbeddingIndexManager,
    Neo4jIngestionService,
)
from graphrag_prod.retrieval import (
    Neo4jRetrievalEngine,
    RetrievalLimits,
    RetrievalRequest,
)
from graphrag_prod.retrieval.metrics import evaluate_retrieval_items
from scripts.evaluate_grounded_answers import evaluate_answer_results
from tests.fixtures.dev_corpus import DevCorpusFixture, load_dev_corpus_fixture


class _StaticAnswerModel:
    """Return one adjudicated payload through the real untrusted-model boundary."""

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.requests: list[AnswerModelRequest] = []

    def generate(self, request: AnswerModelRequest) -> object:
        self.requests.append(request)
        return self.payload


class _FixtureQueryEmbedder:
    """Resolve a fixture query vector inside the trusted server boundary."""

    def __init__(self, fixture: DevCorpusFixture) -> None:
        self.fixture = fixture
        self.calls: list[tuple[str, str]] = []

    def embed(self, query_text: str, *, tenant_id: str) -> QueryEmbedding:
        matches = [
            question
            for question in self.fixture.build.questions
            if question["query"] == query_text
        ]
        if len(matches) != 1:
            raise AssertionError("API query must resolve to exactly one fixture vector")
        question = matches[0]
        if question["principal"]["tenant_id"] != tenant_id:
            raise AssertionError("JWT tenant must own the selected fixture query")
        self.calls.append((query_text, tenant_id))
        return QueryEmbedding(
            tuple(self.fixture.query_vector(question)),
            self.fixture.build.manifest["embedding_profile"][
                "embedding_space_id"
            ],
        )


class _RecordingRetrievalEngine:
    """Record the trusted domain request, then delegate to real Neo4j."""

    def __init__(self, engine: Neo4jRetrievalEngine) -> None:
        self.engine = engine
        self.requests: list[RetrievalRequest] = []

    def retrieve(self, request: RetrievalRequest):
        self.requests.append(request)
        return self.engine.retrieve(request)


class _UnavailableDocumentOperations:
    """Prove retrieval/answer routes never invoke document operations."""

    def __init__(self) -> None:
        self.calls = 0

    def ingest(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("document ingestion must not be called")

    def delete(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("document deletion must not be called")

    def get_job(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("job lookup must not be called")


class _ReadyOperations:
    def check(self) -> BackendResult:
        return BackendResult(
            ReadinessResponse(status="ready", checks={"neo4j": "ok"})
        )


class DevCorpusNeo4jIntegrationTests(unittest.TestCase):
    """Ingest and query all 120 chunks without an external model provider."""

    @classmethod
    def setUpClass(cls) -> None:
        required = (
            "TEST_NEO4J_URI",
            "TEST_NEO4J_USER",
            "TEST_NEO4J_PASSWORD",
            "TEST_NEO4J_DATABASE",
        )
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            raise RuntimeError(f"missing disposable Neo4j settings: {missing}")
        if os.getenv("GRAPHRAG_ALLOW_DISPOSABLE_DB") != "1":
            raise RuntimeError("GRAPHRAG_ALLOW_DISPOSABLE_DB=1 is required")
        uri = os.environ["TEST_NEO4J_URI"]
        host = urlparse(uri).hostname
        if host is None or not ipaddress.ip_address(host).is_loopback:
            raise RuntimeError("integration tests only accept a loopback Neo4j URI")

        cls.database = os.environ["TEST_NEO4J_DATABASE"]
        cls.driver = neo4j.GraphDatabase.driver(
            uri,
            auth=(os.environ["TEST_NEO4J_USER"], os.environ["TEST_NEO4J_PASSWORD"]),
        )
        cls.driver.verify_connectivity()
        records, _, _ = cls.driver.execute_query(
            "MATCH (node) RETURN count(node) AS count",
            database_=cls.database,
        )
        if records[0]["count"] != 0:
            cls.driver.close()
            raise RuntimeError("disposable Neo4j database must start empty")
        apply_schema(cls.driver, cls.database)
        cls.driver.execute_query("CALL db.awaitIndexes(60)", database_=cls.database)
        errors = verify_schema(cls.driver, cls.database)
        if errors:
            cls.driver.close()
            raise RuntimeError(f"schema verification failed: {errors}")

        cls.fixture = load_dev_corpus_fixture()
        service = Neo4jIngestionService(
            cls.driver,
            cls.database,
            worker_id="stage5a-dev-corpus-worker",
        )
        for plan in cls.fixture.plans:
            result = service.ingest(plan)
            if result.active_snapshot_id != plan.snapshot.snapshot_id:
                cls.driver.close()
                raise RuntimeError(
                    f"development corpus snapshot did not activate: {plan.document_id}"
                )

        cls.embedding_manager = Neo4jEmbeddingIndexManager(cls.driver, cls.database)
        cls.active_generations = {}
        tenant_plans: dict[str, list] = {}
        for plan in cls.fixture.plans:
            tenant_plans.setdefault(plan.tenant_id, []).append(plan)
        for tenant_id, plans in sorted(tenant_plans.items()):
            profile = plans[0].bundles[0].all_embeddings[0]
            generation = cls.embedding_manager.prepare(
                tenant_id=tenant_id,
                embedding_profile=profile,
                generation_version=1,
            )
            coverage = cls.embedding_manager.coverage(generation.generation_id)
            if not coverage.complete:
                cls.driver.close()
                raise RuntimeError(
                    f"development corpus embedding coverage is incomplete: {tenant_id}"
                )
            cls.active_generations[tenant_id] = cls.embedding_manager.activate(
                generation.generation_id,
                expected_active_generation_id=None,
            )
        cls.driver.execute_query(
            "CALL db.index.fulltext.awaitEventuallyConsistentIndexRefresh()",
            database_=cls.database,
        )
        cls.engine = Neo4jRetrievalEngine(cls.driver, cls.database)
        cls.default_retrieval_results = {}
        cls.generation_retrieval_results = {}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.driver.execute_query(
            "MATCH (node) DETACH DELETE node",
            database_=cls.database,
        )
        cls.driver.close()

    @classmethod
    def _request(
        cls,
        question: dict,
        *,
        limits: RetrievalLimits | None = None,
    ) -> RetrievalRequest:
        principal = question["principal"]
        return RetrievalRequest(
            query_text=question["query"],
            query_vector=cls.fixture.query_vector(question),
            principal=Principal(
                principal["principal_id"],
                principal["tenant_id"],
                frozenset(principal["groups"]),
            ),
            query_embedding_space_id=cls.fixture.build.manifest[
                "embedding_profile"
            ]["embedding_space_id"],
            # Neo4j maps raw cosine from [-1, 1] into its [0, 1] score domain.
            # Fixture negatives are orthogonal (0.5) and positives score at
            # least (1 + 1/sqrt(2)) / 2 ~= 0.8535, so 0.75 is an auditable gate.
            limits=limits
            or RetrievalLimits(
                top_k=5,
                anchor_k=3,
                minimum_vector_score=0.75,
            ),
        )

    @staticmethod
    def _trace_ids(result) -> set[str]:
        ids = set(result.trace.selected_chunk_ids)
        for stage in (
            result.trace.vector_recall,
            result.trace.bm25_recall,
            result.trace.seed_ranking,
            result.trace.graph_expansion,
            result.trace.candidate_vector_ranking,
            result.trace.final_ranking,
        ):
            ids.update(hit.chunk_id for hit in stage)
        return ids

    @classmethod
    def _default_retrieval(cls, question: dict):
        result = cls.default_retrieval_results.get(question["id"])
        if result is None:
            result = cls.engine.retrieve(cls._request(question))
            cls.default_retrieval_results[question["id"]] = result
        return result

    @classmethod
    def _generation_retrieval(cls, question: dict):
        result = cls.generation_retrieval_results.get(question["id"])
        if result is None:
            result = cls.engine.retrieve(
                cls._request(
                    question,
                    limits=RetrievalLimits(
                        top_k=10,
                        anchor_k=5,
                        minimum_vector_score=0.75,
                    ),
                )
            )
            cls.generation_retrieval_results[question["id"]] = result
        return result

    @staticmethod
    def _answer_payload(gold: dict, retrieval_result) -> dict[str, object]:
        labels_by_chunk_id = {
            chunk.citation.chunk_id: f"S{position}"
            for position, chunk in enumerate(retrieval_result.chunks, start=1)
        }
        text_by_chunk_id = {
            chunk.citation.chunk_id: chunk.text
            for chunk in retrieval_result.chunks
        }
        claims: list[dict[str, object]] = []
        for claim in gold["claims"]:
            selected_ids = [
                chunk_id
                for chunk_id in claim["evidence_chunk_ids"]
                if chunk_id in labels_by_chunk_id
            ]
            if not selected_ids:
                raise AssertionError(
                    f"{gold['id']} lacks selected evidence for {claim['claim_id']}"
                )
            citation_ids = [labels_by_chunk_id[chunk_id] for chunk_id in selected_ids]
            claims.append(
                {
                    "text": claim["reference_text"],
                    "material": True,
                    "inference": claim["inference"],
                    "citation_ids": citation_ids,
                    "evidence": [
                        {
                            "citation_id": labels_by_chunk_id[chunk_id],
                            "quote": text_by_chunk_id[chunk_id],
                        }
                        for chunk_id in selected_ids
                    ],
                }
            )
        return {"status": "answered", "claims": claims, "conflicts": []}

    def test_ingests_declared_scale_and_activates_complete_generations(self) -> None:
        records, _, _ = self.driver.execute_query(
            """
            MATCH (chunk:Chunk)
            WITH count(chunk) AS chunks
            MATCH (document:Document)
            WITH chunks, count(document) AS documents
            MATCH (entity:Entity)
            RETURN chunks, documents, count(entity) AS entities
            """,
            database_=self.database,
        )
        counts = self.fixture.build.manifest["counts"]
        self.assertEqual(records[0]["chunks"], counts["active_chunks"])
        self.assertGreaterEqual(records[0]["chunks"], 100)
        self.assertEqual(records[0]["documents"], counts["documents"])
        self.assertEqual(records[0]["entities"], counts["entities"])

        for tenant_id, generation in self.active_generations.items():
            with self.subTest(tenant_id=tenant_id):
                active = self.embedding_manager.active_generation(tenant_id)
                self.assertEqual(active.generation_id, generation.generation_id)
                self.assertEqual(active.state, "ACTIVE")
                coverage = self.embedding_manager.coverage(generation.generation_id)
                expected = sum(
                    len(plan.bundles)
                    for plan in self.fixture.plans
                    if plan.tenant_id == tenant_id
                )
                self.assertEqual(coverage.total_chunks, expected)
                self.assertEqual(coverage.covered_chunks, expected)
                self.assertTrue(coverage.complete)

    def test_stored_vectors_and_checksums_match_all_fixture_embeddings(self) -> None:
        records, _, _ = self.driver.execute_query(
            """
            MATCH (chunk:Chunk)-[:HAS_EMBEDDING]->(embedding:ChunkEmbedding)
            WHERE embedding.embedding_space_id = $embedding_space_id
            RETURN chunk.chunk_id AS chunk_id,
                   embedding.chunk_id AS embedding_chunk_id,
                   embedding.vector_checksum AS vector_checksum,
                   embedding.vector AS vector
            ORDER BY chunk_id
            """,
            embedding_space_id=self.fixture.build.manifest["embedding_profile"][
                "embedding_space_id"
            ],
            database_=self.database,
        )
        expected = {
            bundle.chunk.chunk_id: bundle.all_embeddings[0]
            for plan in self.fixture.plans
            for bundle in plan.bundles
        }
        self.assertEqual(len(records), len(expected))
        self.assertEqual(len(records), 120)
        self.assertEqual({record["chunk_id"] for record in records}, set(expected))
        for record in records:
            chunk_id = record["chunk_id"]
            fixture_embedding = expected[chunk_id]
            with self.subTest(chunk_id=chunk_id):
                self.assertEqual(record["embedding_chunk_id"], chunk_id)
                self.assertEqual(
                    record["vector_checksum"],
                    fixture_embedding.vector_checksum,
                )
                self.assertEqual(len(record["vector"]), fixture_embedding.dimensions)
                for actual, declared in zip(
                    record["vector"],
                    fixture_embedding.vector,
                ):
                    self.assertAlmostEqual(actual, declared, places=7)

    def test_retrieval_has_stable_exact_citation_round_trips(self) -> None:
        question = self.fixture.question("exact_value-success-01")
        request = self._request(question)
        result = self.engine.retrieve(request)
        expected_ids = {
            chunk_id
            for chunk_id, grade in question["relevance"].items()
            if grade > 0
        }
        self.assertTrue(set(result.trace.selected_chunk_ids) & expected_ids)
        self.assertEqual(result.trace.trace_id, self.engine.retrieve(request).trace.trace_id)

        for retrieved in result.chunks:
            citation = retrieved.citation
            document = self.fixture.documents_by_id[citation.document_id]
            source = self.fixture.source_texts[document["source_path"]]
            expected = self.fixture.chunks_by_id[citation.chunk_id]
            self.assertEqual(citation.canonical_uri, document["canonical_uri"])
            self.assertEqual(citation.version_id, document["version_id"])
            self.assertEqual(citation.chunk_checksum, expected["checksum"])
            self.assertEqual(citation.ordinal, expected["ordinal"])
            self.assertEqual(citation.char_start, expected["char_start"])
            self.assertEqual(citation.char_end, expected["char_end"])
            self.assertEqual(
                source[citation.char_start : citation.char_end],
                retrieved.text,
            )

    def test_validation_relevance_gate_matches_declared_fixture_vectors(self) -> None:
        question = self.fixture.question("cross_chunk-boundary-02")
        query_vector = self.fixture.query_vector(question)
        relevant = {
            chunk_id
            for chunk_id, grade in question["relevance"].items()
            if grade > 0
        }
        principal = question["principal"]
        allowed = [
            chunk
            for chunk in self.fixture.build.chunks
            if chunk["tenant_id"] == principal["tenant_id"]
            and set(chunk["access_groups"]) & set(principal["groups"])
        ]
        scores = {
            chunk["chunk_id"]: sum(
                query_value * chunk_value
                for query_value, chunk_value in zip(
                    query_vector,
                    self.fixture.vectors_by_id[chunk["chunk_id"]],
                )
            )
            for chunk in allowed
        }
        self.assertGreater(min(scores[chunk_id] for chunk_id in relevant), 0.70)
        self.assertAlmostEqual(
            max(
                score
                for chunk_id, score in scores.items()
                if chunk_id not in relevant
            ),
            0.0,
        )
        neo4j_fixture_scores = {
            chunk_id: (score + 1.0) / 2.0
            for chunk_id, score in scores.items()
        }
        configured_floor = self._request(question).limits.minimum_vector_score
        self.assertEqual(configured_floor, 0.75)
        self.assertLess(0.5, configured_floor)
        self.assertGreater(
            min(neo4j_fixture_scores[chunk_id] for chunk_id in relevant),
            configured_floor,
        )
        scores, _, _ = self.driver.execute_query(
            """
            RETURN vector.similarity.cosine([1.0, 0.0], [0.0, 1.0])
                       AS orthogonal,
                   vector.similarity.cosine([1.0, 0.0], [1.0, 0.0])
                       AS identical
            """,
            database_=self.database,
        )
        self.assertEqual(scores[0]["orthogonal"], 0.5)
        self.assertEqual(scores[0]["identical"], 1.0)

    def test_actual_retrieval_meets_quality_targets(self) -> None:
        measured: list[dict] = []
        selected_context: list[dict] = []
        trace_diagnostics: dict[str, dict[str, object]] = {}

        def chunk_keys(chunk_ids) -> list[str]:
            return [
                self.fixture.chunks_by_id[chunk_id]["chunk_key"]
                for chunk_id in chunk_ids
            ]

        def scored_hits(hits) -> list[dict[str, object]]:
            return [
                {
                    "chunk_key": self.fixture.chunks_by_id[hit.chunk_id][
                        "chunk_key"
                    ],
                    "rank": hit.rank,
                    "score": hit.score,
                    "channel_ranks": dict(hit.ranks),
                }
                for hit in hits
            ]

        for question in self.fixture.build.questions:
            result = self._default_retrieval(question)
            trace_ids = self._trace_ids(result)
            forbidden = set(question["forbidden_chunk_ids"])
            accepted_final = [
                hit
                for hit in result.trace.final_ranking
                if len(hit.ranks) >= result.trace.limits.minimum_rrf_channels
            ]
            item = {
                "id": question["id"],
                "answerable": question["answerable"],
                "relevance": question["relevance"],
                "unauthorized_exposures": sorted(trace_ids & forbidden),
            }
            measured.append(
                {
                    **item,
                    # `final_ranking` intentionally retains rejected candidates
                    # for audit. Contract metrics use its post-RRF-gate subset,
                    # before adjacency and context-budget reordering.
                    "ranking": [hit.chunk_id for hit in accepted_final],
                }
            )
            selected_context.append(
                {**item, "ranking": list(result.trace.selected_chunk_ids)}
            )
            if question["id"] in {
                "cross_chunk-boundary-02",
                "cross_chunk-success-05",
            }:
                trace_diagnostics[question["id"]] = {
                    "vector": chunk_keys(
                        hit.chunk_id for hit in result.trace.vector_recall[:5]
                    ),
                    "bm25": chunk_keys(
                        hit.chunk_id for hit in result.trace.bm25_recall[:5]
                    ),
                    "seed": chunk_keys(
                        hit.chunk_id for hit in result.trace.seed_ranking[:5]
                    ),
                    "graph": chunk_keys(
                        hit.chunk_id for hit in result.trace.graph_expansion[:5]
                    ),
                    "candidate_vector": chunk_keys(
                        hit.chunk_id
                        for hit in result.trace.candidate_vector_ranking[:5]
                    ),
                    "candidate_vector_top10_scored": scored_hits(
                        result.trace.candidate_vector_ranking[:10]
                    ),
                    "final": chunk_keys(
                        hit.chunk_id for hit in result.trace.final_ranking[:5]
                    ),
                    "final_top10_scored": scored_hits(
                        result.trace.final_ranking[:10]
                    ),
                    "gated_final": chunk_keys(
                        hit.chunk_id for hit in accepted_final[:5]
                    ),
                    "selected": chunk_keys(result.trace.selected_chunk_ids[:5]),
                }
        metrics = evaluate_retrieval_items(measured)
        selected_metrics = evaluate_retrieval_items(selected_context)

        def recall_misses(items: list[dict]) -> list[str]:
            return [
                item["id"]
                for item in items
                if item["answerable"]
                and not (
                    set(item["ranking"][:5])
                    & {
                        chunk_id
                        for chunk_id, grade in item["relevance"].items()
                        if grade > 0
                    }
                )
            ]

        ranking_misses = recall_misses(measured)
        selected_misses = recall_misses(selected_context)
        failed_contract = (
            metrics.recall_at_5 < 0.90
            or metrics.mrr < 0.80
            or metrics.ndcg_at_5 < 0.85
            or metrics.unauthorized_exposure_count != 0
            or selected_metrics.recall_at_5 < 0.90
            or selected_metrics.unauthorized_exposure_count != 0
        )
        evidence = (
            f"ranking metrics={metrics}; ranking misses={ranking_misses}; "
            f"selected-context metrics={selected_metrics}; "
            f"selected-context misses={selected_misses}"
        )
        if failed_contract:
            evidence += f"; cross-chunk trace top5={trace_diagnostics}"
        print(f"\nStage 5A actual retrieval: {evidence}")
        self.assertEqual(metrics.item_count, 49)
        self.assertGreaterEqual(metrics.recall_at_5, 0.90, evidence)
        self.assertGreaterEqual(metrics.mrr, 0.80, evidence)
        self.assertGreaterEqual(metrics.ndcg_at_5, 0.85, evidence)
        self.assertEqual(metrics.unauthorized_exposure_count, 0, evidence)
        self.assertGreaterEqual(selected_metrics.recall_at_5, 0.90, evidence)
        self.assertEqual(
            selected_metrics.unauthorized_exposure_count,
            0,
            evidence,
        )

    def test_actual_grounded_answers_meet_stage_1_targets(self) -> None:
        actual: list[dict[str, object]] = []
        answers_by_id = {item["id"]: item for item in self.fixture.build.answers}
        for question in self.fixture.build.questions:
            with self.subTest(question=question["id"]):
                gold = answers_by_id[question["id"]]
                retrieval_result = self._generation_retrieval(question)
                payload = (
                    self._answer_payload(gold, retrieval_result)
                    if gold["expected_status"] == "answered"
                    else {
                        "status": "insufficient_context",
                        "claims": [],
                        "conflicts": [],
                    }
                )
                model = _StaticAnswerModel(payload)
                result = GroundedGenerationService(model).generate(
                    GenerationRequest(question["query"], retrieval_result.chunks)
                )
                self.assertIsNone(result.failure_code)
                self.assertEqual(result.status.value, gold["expected_status"])
                if result.status is AnswerStatus.ANSWERED:
                    self.assertEqual(len(model.requests), 1)
                self.assertTrue(
                    all(
                        protected.casefold() not in result.answer.casefold()
                        for protected in gold["forbidden_answer_terms"]
                    )
                )
                record = result.as_dict()
                record["id"] = question["id"]
                actual.append(record)

        metrics = evaluate_answer_results(self.fixture.build.answers, actual)
        print(f"\nStage 6 actual grounded answers: metrics={metrics}")
        self.assertEqual(metrics.item_count, 49)
        self.assertEqual(metrics.generation_failure_count, 0)
        self.assertGreaterEqual(metrics.supported_claim_rate, 0.95)
        self.assertGreaterEqual(metrics.citation_precision, 0.95)
        self.assertGreaterEqual(metrics.citation_coverage, 0.95)
        self.assertEqual(metrics.numerical_fidelity, 1.0)
        self.assertGreaterEqual(metrics.refusal_f1, 0.90)
        self.assertEqual(metrics.answer_correctness, 1.0)
        self.assertEqual(metrics.temporal_comparison_rate, 1.0)
        self.assertIsNone(metrics.conflict_handling_rate)

    def test_acl_is_enforced_at_every_stage_and_across_tenants(self) -> None:
        unauthorized = (
            question
            for question in self.fixture.build.questions
            if question["question_class"] == "unauthorized"
        )
        for question in unauthorized:
            with self.subTest(question=question["id"]):
                result = self.engine.retrieve(self._request(question))
                self.assertTrue(
                    self._trace_ids(result).isdisjoint(
                        question["forbidden_chunk_ids"]
                    )
                )
                principal_tenant = question["principal"]["tenant_id"]
                for chunk_id in self._trace_ids(result):
                    self.assertEqual(
                        self.fixture.chunks_by_id[chunk_id]["tenant_id"],
                        principal_tenant,
                    )

    def test_authenticated_api_uses_real_retrieval_and_grounded_generation(
        self,
    ) -> None:
        """Exercise the HTTP/JWT boundary through the loaded Neo4j corpus."""

        issuer = "stage7-integration-issuer"
        audience = "stage7-integration-api"
        # Deterministic, high-diversity test material; never a deployment secret.
        signing_key = bytes(range(32, 64))

        def headers(question: dict) -> dict[str, str]:
            now = int(time.time())
            principal = question["principal"]
            token = jwt.encode(
                {
                    "iss": issuer,
                    "aud": audience,
                    "sub": principal["principal_id"],
                    "tenant_id": principal["tenant_id"],
                    "groups": principal["groups"],
                    "scope": "retrieval:read answers:generate",
                    "iat": now,
                    "exp": now + 300,
                },
                signing_key,
                algorithm="HS256",
                headers={"typ": "JWT"},
            )
            return {"Authorization": f"Bearer {token}"}

        authenticator = JWTAuthenticator(
            JWTAuthConfig(
                issuer=issuer,
                audience=audience,
                secret=signing_key,
                leeway_seconds=0,
            )
        )

        def application(answer_model: _StaticAnswerModel):
            embedder = _FixtureQueryEmbedder(self.fixture)
            recording_engine = _RecordingRetrievalEngine(self.engine)
            documents = _UnavailableDocumentOperations()
            backend = GraphRAGApplicationBackend(
                documents=documents,
                queries=GraphRAGQueryOperations(
                    recording_engine,  # type: ignore[arg-type]
                    embedder,
                    GroundedGenerationService(answer_model),
                ),
                readiness=_ReadyOperations(),
            )
            return (
                create_app(authenticator=authenticator, backend=backend),
                embedder,
                recording_engine,
                documents,
            )

        exact = self.fixture.question("exact_value-success-01")
        expected = self._default_retrieval(exact)
        answers_by_id = {item["id"]: item for item in self.fixture.build.answers}
        answer_model = _StaticAnswerModel(
            self._answer_payload(answers_by_id[exact["id"]], expected)
        )
        app, embedder, recording_engine, documents = application(answer_model)
        retrieval_payload = {
            "query_text": exact["query"],
            "limits": {"minimum_vector_score": 0.75},
        }
        answer_payload = {
            "query_text": exact["query"],
            "retrieval_limits": {"minimum_vector_score": 0.75},
        }
        with TestClient(app) as client:
            ready = client.get("/health/ready")
            first = client.post(
                "/v1/retrieval",
                headers=headers(exact),
                json=retrieval_payload,
            )
            second = client.post(
                "/v1/retrieval",
                headers=headers(exact),
                json=retrieval_payload,
            )
            answer = client.post(
                "/v1/answers",
                headers=headers(exact),
                json=answer_payload,
            )
            calls_before_untrusted_vector = len(embedder.calls)
            untrusted_vector = client.post(
                "/v1/retrieval",
                headers=headers(exact),
                json={
                    "query_text": exact["query"],
                    "query_vector": list(self.fixture.query_vector(exact)),
                },
            )

        self.assertEqual(ready.status_code, 200, ready.text)
        self.assertEqual(ready.json(), {"status": "ready", "checks": {"neo4j": "ok"}})
        if first.status_code != 200 and recording_engine.requests:
            # Re-run the deterministic request through the public response
            # schema so a contract regression reports its exact safe fixture
            # location instead of only the deliberately generic HTTP error.
            diagnostic_result = self.engine.retrieve(recording_engine.requests[0])
            diagnostic_payload = asdict(diagnostic_result)
            diagnostic_payload["trace"]["version_filter"] = {
                "document_ids": tuple(
                    sorted(diagnostic_result.trace.version_filter.document_ids)
                ),
                "version_ids": tuple(
                    sorted(diagnostic_result.trace.version_filter.version_ids)
                ),
                "published_at_or_before": (
                    diagnostic_result.trace.version_filter.published_at_or_before
                ),
            }
            RetrievalResponse.model_validate(diagnostic_payload)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(first.json(), second.json())
        retrieval_body = first.json()
        self.assertEqual(retrieval_body["trace"]["trace_id"], expected.trace.trace_id)
        self.assertEqual(
            retrieval_body["trace"]["selected_chunk_ids"],
            list(expected.trace.selected_chunk_ids),
        )
        self.assertEqual(retrieval_body["trace"]["tenant_id"], "tenant-alpha")
        relevant_ids = set(exact["relevance"])
        self.assertTrue(
            relevant_ids
            & {
                chunk["citation"]["chunk_id"]
                for chunk in retrieval_body["chunks"]
            }
        )
        for chunk in retrieval_body["chunks"]:
            citation = chunk["citation"]
            fixture_chunk = self.fixture.chunks_by_id[citation["chunk_id"]]
            document = self.fixture.documents_by_id[citation["document_id"]]
            source = self.fixture.source_texts[document["source_path"]]
            self.assertEqual(citation["chunk_checksum"], fixture_chunk["checksum"])
            self.assertEqual(citation["version_id"], document["version_id"])
            self.assertEqual(
                source[citation["char_start"] : citation["char_end"]],
                chunk["text"],
            )

        expected_principal = exact["principal"]
        self.assertEqual(len(recording_engine.requests), 3)
        for request in recording_engine.requests:
            self.assertEqual(
                request.principal.principal_id,
                expected_principal["principal_id"],
            )
            self.assertEqual(
                request.principal.tenant_id,
                expected_principal["tenant_id"],
            )
            self.assertEqual(
                request.principal.groups,
                frozenset(expected_principal["groups"]),
            )
            self.assertEqual(
                request.query_vector,
                tuple(self.fixture.query_vector(exact)),
            )
        self.assertEqual(
            embedder.calls,
            [(exact["query"], "tenant-alpha")] * 3,
        )
        self.assertEqual(untrusted_vector.status_code, 422, untrusted_vector.text)
        self.assertEqual(
            untrusted_vector.json()["code"],
            "invalid_request",
        )
        self.assertEqual(len(embedder.calls), calls_before_untrusted_vector)
        self.assertEqual(documents.calls, 0)

        self.assertEqual(answer.status_code, 200, answer.text)
        answer_body = answer.json()
        self.assertEqual(answer_body["status"], "answered")
        self.assertIn("$72.1 billion", answer_body["answer"])
        self.assertIn("[S", answer_body["answer"])
        self.assertTrue(answer_body["claims"])
        self.assertTrue(answer_body["citations"])
        self.assertTrue(
            relevant_ids
            & {citation["chunk_id"] for citation in answer_body["citations"]}
        )
        self.assertEqual(len(answer_model.requests), 1)

        unauthorized = self.fixture.question("unauthorized-success-01")
        refusal_model = _StaticAnswerModel(
            {"status": "insufficient_context", "claims": [], "conflicts": []}
        )
        unauthorized_app, unauthorized_embedder, unauthorized_engine, unauthorized_docs = (
            application(refusal_model)
        )
        unauthorized_retrieval_payload = {
            "query_text": unauthorized["query"],
            "limits": {"minimum_vector_score": 0.75},
        }
        unauthorized_answer_payload = {
            "query_text": unauthorized["query"],
            "retrieval_limits": {"minimum_vector_score": 0.75},
        }
        with TestClient(unauthorized_app) as client:
            unauthorized_retrieval = client.post(
                "/v1/retrieval",
                headers=headers(unauthorized),
                json=unauthorized_retrieval_payload,
            )
            unauthorized_answer = client.post(
                "/v1/answers",
                headers=headers(unauthorized),
                json=unauthorized_answer_payload,
            )

        self.assertEqual(
            unauthorized_retrieval.status_code,
            200,
            unauthorized_retrieval.text,
        )
        unauthorized_body = unauthorized_retrieval.json()
        trace_ids = set(unauthorized_body["trace"]["selected_chunk_ids"])
        for stage in (
            "vector_recall",
            "bm25_recall",
            "seed_ranking",
            "graph_expansion",
            "candidate_vector_ranking",
            "final_ranking",
        ):
            trace_ids.update(hit["chunk_id"] for hit in unauthorized_body["trace"][stage])
        forbidden_ids = set(unauthorized["forbidden_chunk_ids"])
        self.assertTrue(trace_ids.isdisjoint(forbidden_ids))
        self.assertEqual(unauthorized_body["trace"]["tenant_id"], "tenant-alpha")
        self.assertNotIn("$52.8 billion", unauthorized_retrieval.text)
        self.assertEqual(unauthorized_answer.status_code, 200, unauthorized_answer.text)
        self.assertEqual(unauthorized_answer.json()["status"], "insufficient_context")
        self.assertEqual(unauthorized_answer.json()["citations"], [])
        self.assertNotIn("$52.8 billion", unauthorized_answer.text)
        self.assertEqual(len(unauthorized_engine.requests), 2)
        for request in unauthorized_engine.requests:
            self.assertEqual(
                request.principal.principal_id,
                unauthorized["principal"]["principal_id"],
            )
            self.assertEqual(request.principal.tenant_id, "tenant-alpha")
            self.assertEqual(request.principal.groups, frozenset({"alpha-public"}))
        self.assertEqual(
            unauthorized_embedder.calls,
            [(unauthorized["query"], "tenant-alpha")] * 2,
        )
        self.assertEqual(unauthorized_docs.calls, 0)

    def test_unanswerable_query_can_be_deterministically_gated(self) -> None:
        question = self.fixture.question("unanswerable-success-01")
        result = self.engine.retrieve(
            self._request(
                question,
                limits=RetrievalLimits(
                    top_k=5,
                    anchor_k=3,
                    minimum_vector_score=0.99,
                    minimum_bm25_score=1_000.0,
                ),
            )
        )
        self.assertEqual(result.chunks, ())
        self.assertEqual(result.trace.selected_chunk_ids, ())


if __name__ == "__main__":
    unittest.main()
