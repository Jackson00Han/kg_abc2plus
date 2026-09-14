"""Formal readers discard incomplete releases, including post-hydration changes."""

from types import SimpleNamespace
import unittest

from graphrag_prod.graph.browse_models import GRAPH_STATE_QUERY, GraphViewChanged, read_graph_state
from graphrag_prod.knowledge.publication_guard import (
    MAX_PUBLICATION_CHANGE_RECORDS, MAX_PUBLICATION_MANIFEST_RECORDS, publication_members_guard,
)
from graphrag_prod.knowledge.review import MAX_PUBLICATION_RECORDS as WRITE_LIMIT
from graphrag_prod.knowledge.source_library import Neo4jSourceLibrary, _BOUNDARY
from graphrag_prod.retrieval.engine import (
    ADJACENT_QUERY, BM25_RECALL_QUERY, CANDIDATE_VECTOR_QUERY, CORPUS_STATE_QUERY,
    GRAPH_EXPANSION_QUERY, HYDRATE_QUERY, VECTOR_RECALL_QUERY,
    Neo4jRetrievalEngine, RerankAttemptedFailure, _PUBLISHED_VERSIONS_QUERY,
)
from tests.unit import test_retrieval_reranking as fixture


class PublicationMemberGuardTests(unittest.TestCase):
    def setUp(self):
        self.driver = fixture.FixtureDriver()
        self.request = fixture.RetrievalRerankingTests().request()

    def test_incomplete_publication_exposes_no_chunks_or_recall_ids(self):
        self.driver.state["knowledge_manifest_complete"] = False
        result = Neo4jRetrievalEngine(self.driver).retrieve(self.request)
        self.assertEqual(result.chunks, ())
        self.assertEqual(result.trace.selected_chunk_ids, ())
        self.assertEqual(result.trace.vector_recall, ())
        self.assertEqual(result.trace.knowledge_publication_id, "pub1")
        self.assertNotEqual(result.trace.method, "no published knowledge")
        self.assertTrue(all(query == CORPUS_STATE_QUERY for _, query, _ in self.driver.queries))

    def test_removing_member_after_hydration_discards_captured_text(self):
        original_run = self.driver.run

        def run(query, **parameters):
            rows = original_run(query, **parameters)
            if query == HYDRATE_QUERY:
                self.driver.state["knowledge_manifest_complete"] = False
            return rows

        self.driver.run = run
        result = Neo4jRetrievalEngine(self.driver).retrieve(self.request)
        self.assertEqual(result.chunks, ())
        self.assertEqual(result.trace.selected_chunk_ids, ())
        self.assertEqual(self.driver.reads, 2)

    def test_answer_context_revalidation_checks_members_before_exposing_text(self):
        engine = Neo4jRetrievalEngine(self.driver)
        result = engine.retrieve(self.request)
        self.assertTrue(result.chunks)
        self.driver.state["knowledge_manifest_complete"] = False
        with self.assertRaises(GraphViewChanged):
            engine.validate_result(self.request.principal, result)

    def test_rerank_cannot_return_a_publication_damaged_during_provider_call(self):
        reranker = fixture.FixtureReranker(
            self.driver,
            after=lambda driver: driver.state.update(knowledge_manifest_complete=False),
        )
        engine = Neo4jRetrievalEngine(self.driver, reranker=reranker)
        with self.assertRaises(RerankAttemptedFailure):
            engine.retrieve(self.request)
        self.assertEqual(len(reranker.calls), 1)

    def test_graph_and_source_pin_reject_incomplete_members_before_reading_evidence(self):
        state = {"publication": {"publication_id": "pub"}, "publication_members_complete": False}
        calls = []

        def run(query, **parameters):
            calls.append(query)
            return [state]

        tx = SimpleNamespace(run=run)
        with self.assertRaises(GraphViewChanged):
            read_graph_state(tx, "tenant")
        with self.assertRaises(GraphViewChanged):
            Neo4jSourceLibrary._read_tx(tx, {"tenant_id": "tenant"}, True, 30)
        self.assertTrue(all(query == GRAPH_STATE_QUERY for query in calls))

    def test_every_formal_data_path_uses_the_same_bounded_member_predicate(self):
        self.assertEqual(MAX_PUBLICATION_CHANGE_RECORDS, WRITE_LIMIT)
        guard = publication_members_guard("read_publication")
        for query in (VECTOR_RECALL_QUERY, BM25_RECALL_QUERY, GRAPH_EXPANSION_QUERY,
                      CANDIDATE_VECTOR_QUERY, ADJACENT_QUERY, HYDRATE_QUERY,
                      _PUBLISHED_VERSIONS_QUERY):
            self.assertIn(guard, query)
        for query in (CORPUS_STATE_QUERY, GRAPH_STATE_QUERY, _BOUNDARY):
            self.assertIn(publication_members_guard("publication"), query)
        self.assertEqual(guard.count(f"LIMIT {MAX_PUBLICATION_MANIFEST_RECORDS + 1}"), 2)
        self.assertIn("RETURN DISTINCT publication_member.revision_id", guard)
        self.assertIn("publication_member.tenant_id = $tenant_id", guard)
        self.assertNotIn("size(read_publication.published_revision_ids) > 0", guard)
        with self.assertRaises(ValueError):
            publication_members_guard("publication) DELETE publication")


if __name__ == "__main__":
    unittest.main()
