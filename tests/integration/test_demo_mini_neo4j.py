"""Real database reset, ACL and citations on the six-chunk Chinese demo."""
from dataclasses import asdict
import ipaddress
import os
from urllib.parse import urlparse
import unittest

import neo4j

from graphrag_prod.domain import Principal
from graphrag_prod.domain.ids import embedding_space_id
from graphrag_prod.playground.demo_corpus import load_demo_corpus
from graphrag_prod.retrieval import Neo4jRetrievalEngine, RetrievalRequest, RetrievalLimits
from scripts.playground_reset_store import capture_reset_embeddings, reset_playground_corpus
from scripts.run_playground import _load_corpus, _reuse_corpus


class DemoMiniNeo4jTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.getenv('GRAPHRAG_ALLOW_DISPOSABLE_DB') != '1':
            raise RuntimeError('disposable database opt-in required')
        uri = os.environ['TEST_NEO4J_URI']
        if not ipaddress.ip_address(urlparse(uri).hostname).is_loopback:
            raise RuntimeError('loopback database required')
        cls.database = os.environ['TEST_NEO4J_DATABASE']
        cls.driver = neo4j.GraphDatabase.driver(uri, auth=(os.environ['TEST_NEO4J_USER'], os.environ['TEST_NEO4J_PASSWORD']))
        rows, _, _ = cls.driver.execute_query('MATCH (n) RETURN count(n) AS count', database_=cls.database)
        if rows[0]['count']:
            cls.driver.close()
            raise RuntimeError('initially empty disposable database required')

    @classmethod
    def tearDownClass(cls):
        cls.driver.execute_query('MATCH (n) DETACH DELETE n', database_=cls.database)
        cls.driver.close()

    def test_two_resets_restore_citations_and_permissions_without_provider_calls(self):
        fixture = load_demo_corpus()
        chunks = tuple(b.chunk for p in fixture.plans for b in p.bundles)

        class OfflineEmbedder:
            provider, model, revision, dimensions, normalization = 'test', 'demo-one-hot', 'v1', 128, 'l2'
            embedding_space_id = embedding_space_id(provider, model, revision, dimensions, normalization)
            calls = 0
            vectors = {c.text: tuple(float(i == index) for i in range(128)) for index, c in enumerate(chunks)}

            def embed_documents(self, texts):
                self.calls += 1
                return [self.vectors[t] for t in texts]

        embedder = OfflineEmbedder()
        _load_corpus(self.driver, self.database, embedder, corpus_profile='demo-mini-zh-v1')
        cached = capture_reset_embeddings(self.driver, self.database, fixture, embedder)
        calls = embedder.calls
        original = {c.chunk_id: c.text for c in chunks}
        for iteration in range(2):
            self.driver.execute_query("CREATE (:DemoResetSentinel {kind:'old-ontology-and-job', round:$round})", round=iteration, database_=self.database)
            reset_playground_corpus(self.driver, self.database, cached)
            _reuse_corpus(self.driver, self.database, embedder, corpus_profile='demo-mini-zh-v1')
            self.assertEqual(embedder.calls, calls)
            rows, _, _ = self.driver.execute_query('MATCH (n:DemoResetSentinel) RETURN count(n) AS count', database_=self.database)
            self.assertEqual(rows[0]['count'], 0)
            rows, _, _ = self.driver.execute_query('MATCH (c:Chunk) RETURN c.chunk_id AS id, c.text AS text', database_=self.database)
            self.assertEqual({r['id']: r['text'] for r in rows}, original)
            engine = Neo4jRetrievalEngine(self.driver, self.database)
            for question in fixture.build.questions:
                principal = question['principal']
                relevant = question['relevance']
                target = next((c for c in chunks if c.chunk_id in relevant), chunks[4])
                result = engine.retrieve(RetrievalRequest(
                    query_text=question['query'], query_vector=embedder.vectors[target.text],
                    query_embedding_space_id=embedder.embedding_space_id,
                    principal=Principal(principal['principal_id'], principal['tenant_id'], frozenset(principal['groups'])),
                    limits=RetrievalLimits(top_k=5, candidate_limit=50, minimum_vector_score=0.75),
                ))
                selected = {c.citation.chunk_id for c in result.chunks}
                self.assertFalse(selected.intersection(question['forbidden_chunk_ids']))
                if relevant:
                    self.assertIn(target.chunk_id, selected)
                for evidence in result.chunks:
                    source = next(c for c in chunks if c.chunk_id == evidence.citation.chunk_id)
                    self.assertEqual(evidence.text, source.text)
                    citation = asdict(evidence.citation)
                    self.assertEqual(citation['char_start'], source.char_start)
                    self.assertEqual(citation['char_end'], source.char_end)
