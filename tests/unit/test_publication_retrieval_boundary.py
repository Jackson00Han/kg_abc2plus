"""Published source scopes and answer contexts survive switching only as a unit."""

from dataclasses import replace
import unittest

from graphrag_prod.domain import Principal, retrieval_scope_token
from graphrag_prod.graph.browse_models import GraphViewChanged
from graphrag_prod.retrieval.engine import (
    ADJACENT_QUERY, BM25_RECALL_QUERY, CORPUS_STATE_QUERY, GRAPH_EXPANSION_QUERY, HYDRATE_QUERY,
    MAX_PUBLISHED_SOURCE_VERSIONS, Neo4jRetrievalEngine, RetrievalUnavailable,
    _PUBLISHED_VERSIONS_QUERY, _partitioned_lucene_query,
)
from tests.unit import test_retrieval_reranking as fixture


class PublicationRetrievalBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.driver = fixture.FixtureDriver()
        self.request = fixture.RetrievalRerankingTests().request()
        self.engine = Neo4jRetrievalEngine(self.driver)

    def test_drafts_are_not_retrieved_without_a_publication(self):
        self.driver.state.update(knowledge_publication_id=None, knowledge_activation_generation=0)
        result = self.engine.retrieve(self.request)
        self.assertEqual(result.chunks, ())
        self.assertEqual(result.trace.selected_chunk_ids, ())
        self.assertTrue(self.driver.queries)
        self.assertTrue(all(query == CORPUS_STATE_QUERY for _, query, _ in self.driver.queries))

    def test_unpublished_workspace_needs_no_index_and_empty_context_revalidates(self):
        self.driver.state.update(knowledge_publication_id=None, knowledge_activation_generation=0,
                                 generation_id=None, embedding_space_id=None, dimensions=None)
        result = self.engine.retrieve(self.request)
        self.assertEqual(result.chunks, ())
        self.assertIsNone(result.trace.embedding_generation_id)
        self.assertEqual(result.trace.method, 'no published knowledge')
        self.engine.validate_result(self.request.principal, result)
        self.driver.state.update(knowledge_publication_id='pub1', knowledge_activation_generation=1)
        with self.assertRaises(GraphViewChanged):
            self.engine.validate_result(self.request.principal, result)

    def test_published_embedding_space_must_match_the_query_index(self):
        self.driver.state.update(knowledge_manifest_version=4, knowledge_source_document_count=1,
                                 knowledge_embedding_space_id='original-space')
        with self.assertRaises(RetrievalUnavailable):
            self.engine.retrieve(self.request)
        self.driver.state['knowledge_embedding_space_id'] = None
        with self.assertRaises(RetrievalUnavailable):
            self.engine.retrieve(self.request)
        self.driver.state['knowledge_embedding_space_id'] = 'space1'
        self.assertTrue(self.engine.retrieve(self.request).chunks)

    def test_every_query_phase_receives_the_captured_activation(self):
        result = self.engine.retrieve(self.request)
        self.assertTrue(result.chunks)
        for _, query, parameters in self.driver.queries:
            if query != CORPUS_STATE_QUERY:
                self.assertEqual(parameters['knowledge_publication_id'], 'pub1')
                self.assertEqual(parameters['knowledge_activation_generation'], 1)
        scope_at = next(i for i, (_, query, _) in enumerate(self.driver.queries)
                        if query == _PUBLISHED_VERSIONS_QUERY)
        bm25_at = next(i for i, (_, query, _) in enumerate(self.driver.queries)
                       if query == BM25_RECALL_QUERY)
        self.assertLess(scope_at, bm25_at)
        scope = self.driver.queries[scope_at][2]
        self.assertEqual(scope['source_limit'], MAX_PUBLISHED_SOURCE_VERSIONS + 1)
        lucene = self.driver.queries[bm25_at][2]['lucene_query']
        for row in self.driver.rows.values():
            self.assertIn(retrieval_scope_token('version', row['version_id']), lucene)

    def test_bm25_has_no_unrestricted_empty_scope_fallback(self):
        self.assertEqual(_partitioned_lucene_query('循环水泵', 'pump-tenant', frozenset({'readers'}), ()), '')
        lucene = _partitioned_lucene_query('循环水泵 +(22)', 'pump-tenant', frozenset({'readers'}), ('v1',))
        self.assertIn('publication_scope:' + retrieval_scope_token('version', 'v1'), lucene)
        self.assertNotIn('grscopeactive', lucene)
        self.assertNotIn('v1', lucene)
        self.assertNotIn('+(', lucene)

    def test_source_scope_limit_fails_closed(self):
        driver = fixture.FixtureDriver(MAX_PUBLISHED_SOURCE_VERSIONS + 1)
        with self.assertRaises(RetrievalUnavailable):
            Neo4jRetrievalEngine(driver).retrieve(self.request)

    def test_answer_context_remains_valid_only_with_exact_source_and_pin(self):
        result = self.engine.retrieve(self.request)
        self.engine.validate_result(self.request.principal, result)
        variants = (
            lambda: self.driver.state.update(knowledge_publication_id='pub2', knowledge_activation_generation=2),
            # A switch away and back is still a changed activation.
            lambda: self.driver.state.update(knowledge_activation_generation=3),
            lambda: self.driver.state.update(generation_id='new-embedding-index'),
            lambda: self.driver.state.update(corpus_revision=5),
            lambda: self.driver.revoked.add(result.chunks[0].citation.chunk_id),
            lambda: self.driver.rows[result.chunks[0].citation.chunk_id].update(text='修订后的循环水泵文本'),
        )
        for mutate in variants:
            with self.subTest(mutation=mutate):
                self.setUp()
                result = self.engine.retrieve(self.request)
                mutate()
                with self.assertRaises(GraphViewChanged):
                    self.engine.validate_result(self.request.principal, result)

    def test_answer_context_rejects_another_tenant_and_altered_payload(self):
        result = self.engine.retrieve(self.request)
        other = Principal('reader', 'other-pump-tenant', self.request.principal.groups)
        with self.assertRaises(GraphViewChanged):
            self.engine.validate_result(other, result)
        tampered = replace(result, chunks=(replace(result.chunks[0], text='不属于原文的功率'), *result.chunks[1:]))
        with self.assertRaises(GraphViewChanged):
            self.engine.validate_result(self.request.principal, tampered)

    def test_normal_source_supersession_is_distinct_from_explicit_withdrawal(self):
        for query in (HYDRATE_QUERY, ADJACENT_QUERY, GRAPH_EXPANSION_QUERY, _PUBLISHED_VERSIONS_QUERY):
            self.assertIn("build_state IN ['PUBLISHED', 'RETIRED']", query)
            self.assertIn('retirement_id IS NULL', query)
            self.assertIn('version.retired_at IS NULL', query)
            self.assertNotIn('snapshot.retired_at IS NULL', query)
            self.assertNotIn('ACTIVE_VERSION', query)
            self.assertNotIn('ACTIVE_SNAPSHOT', query)

    def test_graph_expansion_degree_uses_only_published_authorized_mentions(self):
        self.assertEqual(GRAPH_EXPANSION_QUERY.count('GovernedEntityMentionRevision'), 3)
        self.assertNotIn(':EntityMention ', GRAPH_EXPANSION_QUERY)
        for name in ('seed', 'degree', 'candidate'):
            self.assertIn(f'{name}_mention.access_groups', GRAPH_EXPANSION_QUERY)
            self.assertIn(f'{name}_publication)-[:PUBLISHES_KNOWLEDGE_REVISION]', GRAPH_EXPANSION_QUERY)


if __name__ == '__main__':
    unittest.main()
