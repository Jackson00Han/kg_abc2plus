"""Real-Neo4j Stage 5 retrieval and authorization tests."""

from __future__ import annotations

import dataclasses
from datetime import timedelta
import ipaddress
import os
import unittest
from urllib.parse import urlparse

import neo4j

from graphrag_prod.domain import Principal
from graphrag_prod.graph.schema import apply_schema, verify_schema
from graphrag_prod.ingestion import (
    Neo4jEmbeddingIndexManager,
    Neo4jIngestionService,
)
from graphrag_prod.retrieval import (
    Neo4jRetrievalEngine,
    RetrievalLimits,
    RetrievalRequest,
    RetrievalUnavailable,
    VersionFilter,
)
from tests.fixtures.ingestion import (
    CHUNKS_V2,
    FIXED_TIME,
    FixedClock,
    make_plan,
    make_principal,
)


class Neo4jRetrievalIntegrationTests(unittest.TestCase):
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
            "MATCH (node) RETURN count(node) AS count", database_=cls.database
        )
        if records[0]["count"] != 0:
            cls.driver.close()
            raise RuntimeError("disposable Neo4j database must start empty")
        apply_schema(cls.driver, cls.database)
        apply_schema(cls.driver, cls.database)
        errors = verify_schema(cls.driver, cls.database)
        if errors:
            cls.driver.close()
            raise RuntimeError(f"schema verification failed: {errors}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.driver.close()

    def setUp(self) -> None:
        self.driver.execute_query("MATCH (node) DETACH DELETE node", database_=self.database)
        self.clock = FixedClock()
        self.ingestion = Neo4jIngestionService(
            self.driver,
            self.database,
            worker_id="stage5-worker",
            clock=self.clock,
        )
        self.embedding = Neo4jEmbeddingIndexManager(self.driver, self.database)
        self.engine = Neo4jRetrievalEngine(self.driver, self.database)
        self.plan = make_plan(tenant_id="tenant-stage5")
        self.ingestion.ingest(self.plan)
        self._activate(self.plan, generation_version=1)

    def tearDown(self) -> None:
        self.driver.execute_query("MATCH (node) DETACH DELETE node", database_=self.database)

    @staticmethod
    def _embeddings(plan) -> tuple:
        return tuple(
            embedding
            for bundle in plan.bundles
            for embedding in bundle.all_embeddings
        )

    def _activate(self, plan, *, generation_version: int) -> None:
        embeddings = self._embeddings(plan)
        prepared = self.embedding.prepare(
            tenant_id=plan.tenant_id,
            embedding_profile=embeddings[0],
            generation_version=generation_version,
        )
        current = self.embedding.active_generation(plan.tenant_id)
        self.embedding.activate(
            prepared.generation_id,
            expected_active_generation_id=(
                None if current is None else current.generation_id
            ),
        )
        self.driver.execute_query(
            "CALL db.index.fulltext.awaitEventuallyConsistentIndexRefresh()",
            database_=self.database,
        )

    def _request(
        self,
        ordinal: int = 0,
        *,
        query_text: str | None = None,
        principal: Principal | None = None,
        limits: RetrievalLimits | None = None,
        version_filter: VersionFilter | None = None,
        vector: tuple[float, ...] | None = None,
    ) -> RetrievalRequest:
        embedding = self.plan.bundles[ordinal].all_embeddings[0]
        return RetrievalRequest(
            query_text or self.plan.bundles[ordinal].chunk.text,
            embedding.vector if vector is None else vector,
            principal or make_principal(self.plan.tenant_id),
            embedding.embedding_space_id,
            limits or RetrievalLimits(),
            version_filter or VersionFilter(),
        )

    def test_returns_stable_citations_bounded_context_and_structured_trace(self) -> None:
        request = self._request(
            query_text="Apple revenue",
            limits=RetrievalLimits(top_k=3, anchor_k=1, max_context_chars=70),
        )
        result = self.engine.retrieve(request)
        revenue_id = self.plan.bundles[0].chunk.chunk_id
        self.assertEqual(result.chunks[0].citation.chunk_id, revenue_id)
        self.assertLessEqual(len(result.chunks), 3)
        self.assertLessEqual(result.trace.context_chars, 70)
        self.assertTrue(result.trace.vector_recall)
        self.assertTrue(result.trace.bm25_recall)
        self.assertTrue(result.trace.graph_expansion)
        self.assertTrue(result.trace.final_ranking)
        self.assertEqual(result.trace.selected_chunk_ids, tuple(
            chunk.citation.chunk_id for chunk in result.chunks
        ))
        for chunk in result.chunks:
            citation = chunk.citation
            self.assertEqual(citation.document_id, self.plan.document_id)
            self.assertEqual(citation.version_id, self.plan.version_id)
            source = self.plan.bundles[citation.ordinal].version.normalized_text
            self.assertEqual(source[citation.char_start : citation.char_end], chunk.text)
        replay = self.engine.retrieve(request)
        self.assertEqual(result.trace.trace_id, replay.trace.trace_id)

    def test_all_stage_1_question_classes_have_real_retrieval_cases(self) -> None:
        ids = [bundle.chunk.chunk_id for bundle in self.plan.bundles]
        cases = (
            ("single_chunk", self._request(0, query_text="revenue"), {ids[0]}),
            ("cross_chunk", self._request(0, query_text="revenue margin"), {ids[0], ids[1]}),
            ("graph_relationship", self._request(0, query_text="revenue"), {ids[1]}),
            ("exact_value", self._request(1, query_text="46.2"), {ids[1]}),
        )
        for question_class, request, expected in cases:
            with self.subTest(question_class=question_class):
                result = self.engine.retrieve(request)
                visible = {chunk.citation.chunk_id for chunk in result.chunks}
                self.assertTrue(visible & expected)
        unanswerable = self.engine.retrieve(
            self._request(
                query_text="France capital unrelated",
                vector=tuple(-value for value in self._embeddings(self.plan)[0].vector),
                limits=RetrievalLimits(minimum_vector_score=0.99),
            )
        )
        self.assertEqual(unanswerable.chunks, ())
        with self.assertRaisesRegex(RetrievalUnavailable, "vector space"):
            self.engine.retrieve(
                dataclasses.replace(
                    self._request(query_text="revenue"),
                    query_embedding_space_id="different-space:same-dimensions",
                )
            )

        wrong_group = Principal("outsider", self.plan.tenant_id, frozenset({"legal"}))
        unauthorized = self.engine.retrieve(
            self._request(query_text="revenue", principal=wrong_group)
        )
        self.assertEqual(unauthorized.chunks, ())

        active = self.engine.retrieve(
            self._request(
                query_text="margin",
                version_filter=VersionFilter(version_ids=frozenset({self.plan.version_id})),
            )
        )
        self.assertTrue(active.chunks)

    def test_acl_is_enforced_in_recall_graph_adjacency_and_final_context(self) -> None:
        protected_id = self.plan.bundles[2].chunk.chunk_id
        self.driver.execute_query(
            "MATCH (chunk:Chunk {chunk_id: $chunk_id}) "
            "SET chunk.access_groups = ['protected-only']",
            chunk_id=protected_id,
            database_=self.database,
        )
        result = self.engine.retrieve(
            self._request(
                0,
                query_text="Apple revenue cash",
                limits=RetrievalLimits(top_k=3, anchor_k=1),
            )
        )
        trace_ids = {
            hit.chunk_id
            for stage in (
                result.trace.vector_recall,
                result.trace.bm25_recall,
                result.trace.graph_expansion,
                result.trace.candidate_vector_ranking,
                result.trace.final_ranking,
            )
            for hit in stage
        }
        self.assertNotIn(protected_id, trace_ids)
        self.assertNotIn(protected_id, result.trace.selected_chunk_ids)

        self.driver.execute_query(
            "MATCH (chunk:Chunk {chunk_id: $chunk_id}) "
            "SET chunk.access_groups = ['knowledge-readers'] "
            "WITH chunk MATCH (document:Document {document_id: chunk.document_id}) "
            "SET document.access_groups = ['protected-only']",
            chunk_id=protected_id,
            database_=self.database,
        )
        document_denied = self.engine.retrieve(self._request(query_text="revenue"))
        self.assertEqual(document_denied.chunks, ())

    def test_retired_and_filtered_versions_never_reappear(self) -> None:
        old_version_id = self.plan.version_id
        updated = make_plan(
            operation_key="stage5-update",
            tenant_id=self.plan.tenant_id,
            chunk_specs=CHUNKS_V2,
            version_number=2,
            expected_active_snapshot_id=self.plan.snapshot.snapshot_id,
            source_generation=0,
        )
        self.clock.advance(seconds=1)
        self.ingestion.ingest(updated)
        self.plan = updated
        self._activate(updated, generation_version=2)

        current = self.engine.retrieve(
            self._request(
                1,
                query_text="margin",
                version_filter=VersionFilter(version_ids=frozenset({updated.version_id})),
            )
        )
        self.assertTrue(current.chunks)
        self.assertTrue(all(
            chunk.citation.version_id == updated.version_id for chunk in current.chunks
        ))
        retired = self.engine.retrieve(
            self._request(
                1,
                query_text="margin",
                version_filter=VersionFilter(version_ids=frozenset({old_version_id})),
            )
        )
        self.assertEqual(retired.chunks, ())
        before_publication = self.engine.retrieve(
            self._request(
                1,
                query_text="margin",
                version_filter=VersionFilter(
                    published_at_or_before=FIXED_TIME + timedelta(hours=1)
                ),
            )
        )
        self.assertEqual(before_publication.chunks, ())

    def test_cross_tenant_retrieval_has_no_target_existence_signal(self) -> None:
        target_ids = {bundle.chunk.chunk_id for bundle in self.plan.bundles}
        other = make_plan(
            operation_key="other-tenant",
            tenant_id="tenant-stage5-other",
            canonical_uri="https://example.com/knowledge/other",
        )
        self.ingestion.ingest(other)
        self._activate(other, generation_version=1)
        self.plan = other
        result = self.engine.retrieve(
            self._request(
                query_text="Apple revenue",
                principal=make_principal(other.tenant_id),
            )
        )
        visible = {hit.chunk_id for hit in result.trace.final_ranking}
        visible.update(result.trace.selected_chunk_ids)
        self.assertTrue(visible)
        self.assertTrue(visible.isdisjoint(target_ids))


if __name__ == "__main__":
    unittest.main()
