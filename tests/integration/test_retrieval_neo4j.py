"""Real-Neo4j Stage 5 retrieval and authorization tests."""

from __future__ import annotations

import dataclasses
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import ipaddress
import os
import threading
import unittest
from typing import Any
from urllib.parse import urlparse

import neo4j
from neo4j import unit_of_work

from graphrag_prod.domain import Principal, active_retrieval_scope
from graphrag_prod.graph.schema import apply_schema, verify_schema
from graphrag_prod.ingestion import (
    IngestionPlan,
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
from graphrag_prod.retrieval.engine import (
    BM25_RECALL_QUERY,
    CANDIDATE_VECTOR_QUERY,
    CORPUS_STATE_QUERY,
    FULLTEXT_INDEX_NAME,
    HYDRATE_QUERY,
    VECTOR_RECALL_QUERY,
    _partitioned_lucene_query,
)
from tests.fixtures.ingestion import (
    CHUNKS_V2,
    FIXED_TIME,
    FixedClock,
    make_plan,
    make_principal,
)


class _ReadBarrier:
    """Pause one statement in the first managed read transaction."""

    def __init__(self, query: str) -> None:
        self.query = query
        self.before_query = threading.Event()
        self.release_query = threading.Event()
        self.attempts = 0
        self.fired = False


class _BarrierTransaction:
    def __init__(self, transaction: Any, barrier: _ReadBarrier | None) -> None:
        self._transaction = transaction
        self._barrier = barrier

    def run(self, query: str, *args: Any, **kwargs: Any) -> Any:
        barrier = self._barrier
        if barrier is not None and not barrier.fired and query == barrier.query:
            barrier.fired = True
            barrier.before_query.set()
            if not barrier.release_query.wait(timeout=20):
                raise AssertionError("timed out waiting for concurrent corpus mutation")
        return self._transaction.run(query, *args, **kwargs)


class _BarrierSession:
    def __init__(self, session: Any, barrier: _ReadBarrier) -> None:
        self._session = session
        self._barrier = barrier

    def __enter__(self) -> _BarrierSession:
        self._session.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> Any:
        return self._session.__exit__(exc_type, exc, traceback)

    def execute_read(self, work: Any, *args: Any, **kwargs: Any) -> Any:
        self._barrier.attempts += 1
        barrier = self._barrier if self._barrier.attempts == 1 else None

        def guarded(transaction: Any, *work_args: Any, **work_kwargs: Any) -> Any:
            return work(
                _BarrierTransaction(transaction, barrier),
                *work_args,
                **work_kwargs,
            )

        guarded_work = unit_of_work(
            metadata=work.metadata,
            timeout=work.timeout,
        )(guarded)
        return self._session.execute_read(guarded_work, *args, **kwargs)


class _BarrierDriver:
    def __init__(self, driver: Any, barrier: _ReadBarrier) -> None:
        self._driver = driver
        self._barrier = barrier

    def session(self, **kwargs: Any) -> _BarrierSession:
        return _BarrierSession(self._driver.session(**kwargs), self._barrier)


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

    @staticmethod
    def _with_access_policy(
        plan: IngestionPlan,
        *,
        operation_key: str,
        access_group: str,
    ) -> IngestionPlan:
        policy_id = f"{plan.tenant_id}:{access_group}"
        access_groups = frozenset({access_group})
        bundles = tuple(
            dataclasses.replace(
                bundle,
                document=dataclasses.replace(
                    bundle.document,
                    access_policy_id=policy_id,
                    access_policy_version=2,
                    access_groups=access_groups,
                ),
                chunk=dataclasses.replace(
                    bundle.chunk,
                    access_policy_id=policy_id,
                    access_policy_version=2,
                    access_groups=access_groups,
                ),
            )
            for bundle in plan.bundles
        )
        return IngestionPlan.build(
            operation_key=operation_key,
            profile=plan.profile,
            governance_policy=plan.governance_policy,
            bundles=bundles,
            expected_active_snapshot_id=plan.snapshot.snapshot_id,
            source_generation=plan.source_generation,
            artifact_input_hashes=dict(plan.artifact_input_hashes),
            created_at=plan.snapshot.created_at,
            max_attempts=plan.max_attempts,
        )

    def _state(self) -> dict[str, Any]:
        records, _, _ = self.driver.execute_query(
            CORPUS_STATE_QUERY,
            tenant_id=self.plan.tenant_id,
            database_=self.database,
        )
        self.assertEqual(len(records), 1)
        return dict(records[0])

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
            source_bundle = self.plan.bundles[citation.ordinal]
            self.assertEqual(citation.document_id, self.plan.document_id)
            self.assertEqual(citation.document_title, source_bundle.document.title)
            self.assertEqual(citation.version_id, self.plan.version_id)
            self.assertEqual(citation.published_at, source_bundle.version.published_at)
            source = source_bundle.version.normalized_text
            self.assertEqual(source[citation.char_start : citation.char_end], chunk.text)
        replay = self.engine.retrieve(request)
        self.assertEqual(result.trace.trace_id, replay.trace.trace_id)

    def test_concurrent_publish_discards_mixed_read_and_retries_new_snapshot(
        self,
    ) -> None:
        initial_revision = int(self._state()["corpus_revision"])
        request = self._request(1, query_text="Apple margin")
        updated = make_plan(
            operation_key="concurrent-stage5-update",
            tenant_id=self.plan.tenant_id,
            chunk_specs=CHUNKS_V2,
            version_number=2,
            expected_active_snapshot_id=self.plan.snapshot.snapshot_id,
            source_generation=0,
        )
        barrier = _ReadBarrier(BM25_RECALL_QUERY)
        engine = Neo4jRetrievalEngine(
            _BarrierDriver(self.driver, barrier),
            self.database,
        )

        def publish() -> None:
            if not barrier.before_query.wait(timeout=20):
                raise AssertionError("retrieval did not reach the publication barrier")
            try:
                self.clock.advance(seconds=1)
                self.ingestion.ingest(updated)
                self._activate(updated, generation_version=2)
            finally:
                barrier.release_query.set()

        with ThreadPoolExecutor(max_workers=1) as executor:
            published = executor.submit(publish)
            result = engine.retrieve(request)
            published.result(timeout=30)

        current_state = self._state()
        self.assertEqual(barrier.attempts, 2)
        self.assertEqual(result.trace.corpus_revision, initial_revision + 1)
        self.assertEqual(
            result.trace.embedding_generation_id,
            str(current_state["generation_id"]),
        )
        self.assertTrue(result.chunks)
        self.assertTrue(
            all(chunk.citation.version_id == updated.version_id for chunk in result.chunks)
        )
        self.assertNotIn(
            self.plan.version_id,
            {chunk.citation.version_id for chunk in result.chunks},
        )

    def test_concurrent_acl_revocation_discards_stale_authorized_result(self) -> None:
        initial_revision = int(self._state()["corpus_revision"])
        request = self._request(query_text="Apple revenue")
        revoked = self._with_access_policy(
            self.plan,
            operation_key="concurrent-stage5-access-revocation",
            access_group="legal-readers",
        )
        barrier = _ReadBarrier(HYDRATE_QUERY)
        engine = Neo4jRetrievalEngine(
            _BarrierDriver(self.driver, barrier),
            self.database,
        )

        def revoke() -> None:
            if not barrier.before_query.wait(timeout=20):
                raise AssertionError("retrieval did not reach the authorization barrier")
            try:
                self.clock.advance(seconds=1)
                self.ingestion.ingest(revoked)
                self._activate(revoked, generation_version=2)
            finally:
                barrier.release_query.set()

        with ThreadPoolExecutor(max_workers=1) as executor:
            revoked_future = executor.submit(revoke)
            result = engine.retrieve(request)
            revoked_future.result(timeout=30)

        self.assertEqual(barrier.attempts, 2)
        self.assertEqual(result.trace.corpus_revision, initial_revision + 1)
        self.assertEqual(result.chunks, ())
        self.assertEqual(result.trace.selected_chunk_ids, ())
        self.assertFalse(result.trace.vector_recall)
        self.assertFalse(result.trace.bm25_recall)
        self.assertFalse(result.trace.graph_expansion)
        self.assertFalse(result.trace.final_ranking)

    def test_citation_metadata_cannot_be_spoofed_by_chunk_properties(self) -> None:
        self.driver.execute_query(
            """
            MATCH (chunk:Chunk {tenant_id: $tenant_id})
            SET chunk.document_title = 'forged chunk title',
                chunk.published_at = datetime('2099-01-01T00:00:00Z')
            """,
            tenant_id=self.plan.tenant_id,
            database_=self.database,
        )
        result = self.engine.retrieve(self._request(query_text="Apple revenue"))
        self.assertTrue(result.citations)
        for citation in result.citations:
            source_bundle = self.plan.bundles[citation.ordinal]
            self.assertEqual(citation.document_title, source_bundle.document.title)
            self.assertEqual(citation.published_at, source_bundle.version.published_at)
            self.assertNotEqual(citation.document_title, "forged chunk title")

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

        # Partition clauses select the authorized candidate window, but must
        # not change BM25 ordering when ACL lists have different lengths.
        first_id = self.plan.bundles[0].chunk.chunk_id
        second_id = self.plan.bundles[1].chunk.chunk_id
        tenant_id = self.plan.tenant_id
        self.driver.execute_query(
            """
            MATCH (chunk:Chunk)
            WHERE chunk.chunk_id IN $chunk_ids
            SET chunk.text = 'stagefive equalcontent'
            WITH chunk
            SET chunk.retrieval_scope = CASE chunk.chunk_id
                WHEN $first_id THEN $first_scope
                ELSE $second_scope
            END
            """,
            chunk_ids=[first_id, second_id],
            first_id=first_id,
            first_scope=active_retrieval_scope(
                tenant_id, frozenset({"knowledge-readers"})
            ),
            second_scope=active_retrieval_scope(
                tenant_id,
                frozenset({"knowledge-readers", "finance", "audit"}),
            ),
            database_=self.database,
        )
        self.driver.execute_query(
            "CALL db.index.fulltext.awaitEventuallyConsistentIndexRefresh()",
            database_=self.database,
        )
        records, _, _ = self.driver.execute_query(
            """
            CALL db.index.fulltext.queryNodes($index_name, $query)
            YIELD node, score
            WHERE node.chunk_id IN $chunk_ids
            RETURN node.chunk_id AS chunk_id, score
            ORDER BY chunk_id
            """,
            index_name=FULLTEXT_INDEX_NAME,
            query=_partitioned_lucene_query(
                "equalcontent",
                tenant_id,
                frozenset({"knowledge-readers", "finance", "audit"}),
            ),
            chunk_ids=[first_id, second_id],
            database_=self.database,
        )
        self.assertEqual(
            [record["chunk_id"] for record in records],
            sorted([first_id, second_id]),
        )
        self.assertAlmostEqual(records[0]["score"], records[1]["score"], places=12)

    def test_vector_index_overfetch_cannot_leak_or_be_crowded_out_by_acl(self) -> None:
        protected_bundle = self.plan.bundles[2]
        protected_id = protected_bundle.chunk.chunk_id
        self.driver.execute_query(
            "MATCH (chunk:Chunk {chunk_id: $chunk_id}) "
            "SET chunk.access_groups = ['protected-only']",
            chunk_id=protected_id,
            database_=self.database,
        )

        result = self.engine.retrieve(
            self._request(
                2,
                query_text="term-absent-from-fulltext-index",
                limits=RetrievalLimits(
                    top_k=1,
                    vector_recall_k=1,
                    bm25_recall_k=1,
                    bm25_scan_k=1,
                    seed_k=1,
                    candidate_limit=3,
                    anchor_k=1,
                    adjacent_window=0,
                ),
            )
        )

        self.assertEqual(len(result.trace.vector_recall), 1)
        self.assertNotEqual(result.trace.vector_recall[0].chunk_id, protected_id)
        self.assertNotIn(
            protected_id,
            {
                hit.chunk_id
                for stage in (
                    result.trace.vector_recall,
                    result.trace.candidate_vector_ranking,
                    result.trace.final_ranking,
                )
                for hit in stage
            },
        )
        self.assertNotIn(protected_id, result.trace.selected_chunk_ids)

    def test_exact_vector_queries_use_pinned_generation_and_candidate_id_seeks(
        self,
    ) -> None:
        state_records, _, _ = self.driver.execute_query(
            CORPUS_STATE_QUERY,
            tenant_id=self.plan.tenant_id,
            database_=self.database,
        )
        self.assertEqual(len(state_records), 1)
        state = state_records[0]
        principal = make_principal(self.plan.tenant_id)
        candidate_ids = [bundle.chunk.chunk_id for bundle in self.plan.bundles]
        protected_id = candidate_ids[-1]
        self.driver.execute_query(
            "MATCH (chunk:Chunk {chunk_id: $chunk_id}) "
            "SET chunk.access_groups = ['protected-only']",
            chunk_id=protected_id,
            database_=self.database,
        )
        parameters = {
            "tenant_id": self.plan.tenant_id,
            "groups": sorted(principal.groups),
            "document_ids": [],
            "version_ids": [],
            "published_before": None,
            "corpus_revision": int(state["corpus_revision"]),
            "dimensions": int(state["dimensions"]),
            "embedding_space_id": str(state["embedding_space_id"]),
            "generation_id": str(state["generation_id"]),
            "query_vector": list(self._embeddings(self.plan)[0].vector),
            "minimum_score": 0.0,
            "limit": len(candidate_ids),
            "candidate_ids": candidate_ids,
        }

        candidate_records, candidate_summary, _ = self.driver.execute_query(
            "PROFILE\n" + CANDIDATE_VECTOR_QUERY,
            parameters_=parameters,
            database_=self.database,
        )
        returned_ids = {str(record["chunk_id"]) for record in candidate_records}
        self.assertEqual(returned_ids, set(candidate_ids) - {protected_id})

        vector_records, vector_summary, _ = self.driver.execute_query(
            "PROFILE\n" + VECTOR_RECALL_QUERY,
            parameters_=parameters,
            database_=self.database,
        )
        self.assertNotIn(
            protected_id,
            {str(record["chunk_id"]) for record in vector_records},
        )

        def plan_nodes(profile):
            yield profile
            for child in profile.get("children", ()):
                yield from plan_nodes(child)

        candidate_plan = tuple(plan_nodes(candidate_summary.profile))
        chunk_seeks = [
            node
            for node in candidate_plan
            if str(node.get("operatorType", "")).startswith("NodeUniqueIndexSeek")
            and "chunk:Chunk(chunk_id)"
            in str(node.get("args", {}).get("Details", ""))
        ]
        self.assertEqual(len(chunk_seeks), 1)
        self.assertEqual(chunk_seeks[0].get("rows"), len(candidate_ids))
        vector_plan = tuple(plan_nodes(vector_summary.profile))
        state_seeks = [
            node
            for node in vector_plan
            if str(node.get("operatorType", "")).startswith("NodeUniqueIndexSeek")
            and "state:TenantCorpusState(tenant_id)"
            in str(node.get("args", {}).get("Details", ""))
        ]
        self.assertEqual(len(state_seeks), 1)
        self.assertEqual(state_seeks[0].get("rows"), 1)
        self.assertLessEqual(int(state_seeks[0].get("dbHits", 0)), 2)

        for field, value in (
            ("generation_id", "not-active"),
            ("corpus_revision", int(state["corpus_revision"]) + 1),
            ("embedding_space_id", "not-active"),
            ("dimensions", int(state["dimensions"]) + 1),
        ):
            with self.subTest(pinned_field=field):
                stale = {**parameters, field: value}
                records, _, _ = self.driver.execute_query(
                    CANDIDATE_VECTOR_QUERY,
                    parameters_=stale,
                    database_=self.database,
                )
                self.assertEqual(records, [])

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
