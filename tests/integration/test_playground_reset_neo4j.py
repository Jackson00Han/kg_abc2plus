"""Reset the entire disposable Playground while preserving its public seed."""

from __future__ import annotations

import ipaddress
import os
import unittest
from urllib.parse import urlparse

import neo4j

from graphrag_prod.graph.schema import verify_schema
from scripts import playground_reset_store, run_playground
from tests.fixtures.dev_corpus import load_dev_corpus_fixture


class _FixtureOnlyEmbedder:
    """Use committed fixture vectors; no external provider exists in this test."""

    def __init__(self, fixture):
        profile = fixture.build.manifest["embedding_profile"]
        for key in ("provider", "model", "revision", "dimensions", "normalization", "embedding_space_id"):
            setattr(self, key, profile[key])
        self.vectors = {
            bundle.chunk.text: bundle.embedding.vector
            for plan in fixture.plans for bundle in plan.bundles
        }
        self.calls = 0

    def embed_documents(self, texts):
        self.calls += 1
        return [self.vectors[text] for text in texts]


class PlaygroundResetNeo4jTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        required = ("TEST_NEO4J_URI", "TEST_NEO4J_USER", "TEST_NEO4J_PASSWORD", "TEST_NEO4J_DATABASE")
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            raise RuntimeError(f"missing disposable Neo4j settings: {missing}")
        if os.getenv("GRAPHRAG_ALLOW_DISPOSABLE_DB") != "1":
            raise RuntimeError("GRAPHRAG_ALLOW_DISPOSABLE_DB=1 is required")
        uri = os.environ["TEST_NEO4J_URI"]
        host = urlparse(uri).hostname
        if host is None or not ipaddress.ip_address(host).is_loopback:
            raise RuntimeError("reset integration tests only accept loopback Neo4j")
        cls.database = os.environ["TEST_NEO4J_DATABASE"]
        cls.driver = neo4j.GraphDatabase.driver(
            uri, auth=(os.environ["TEST_NEO4J_USER"], os.environ["TEST_NEO4J_PASSWORD"]),
        )
        cls.driver.verify_connectivity()
        rows, _, _ = cls.driver.execute_query("MATCH (n) RETURN count(n) AS count", database_=cls.database)
        if rows[0]["count"]:
            cls.driver.close()
            raise RuntimeError("reset integration tests require an initially empty disposable database")
        cls.fixture = load_dev_corpus_fixture()
        cls.embedder = _FixtureOnlyEmbedder(cls.fixture)
        run_playground._load_corpus(cls.driver, cls.database, cls.embedder)
        run_playground._reuse_corpus(cls.driver, cls.database, cls.embedder)

    @classmethod
    def tearDownClass(cls):
        cls.driver.execute_query("MATCH (n) DETACH DELETE n", database_=cls.database)
        cls.driver.close()

    def _query(self, query, **parameters):
        rows, _, _ = self.driver.execute_query(query, **parameters, database_=self.database)
        return rows

    def _index_identity(self):
        return [dict(row) for row in self._query(
            "SHOW INDEXES YIELD id, name, type, labelsOrTypes, properties "
            "RETURN id, name, type, labelsOrTypes, properties ORDER BY name"
        )]

    def _assert_restored_seed(self):
        expected_documents = {
            plan.document_id: (plan.tenant_id, plan.bundles[0].version.normalized_text,
                               plan.bundles[0].version.checksum)
            for plan in self.fixture.plans
        }
        documents = self._query(
            "MATCH (d:Document)-[:HAS_VERSION]->(v:DocumentVersion) "
            "RETURN d.document_id AS id, d.tenant_id AS tenant, "
            "v.normalized_text AS text, v.checksum AS checksum"
        )
        self.assertEqual(len(documents), 10)
        self.assertEqual({row["id"]: (row["tenant"], row["text"], row["checksum"]) for row in documents},
                         expected_documents)
        expected_chunks = {
            bundle.chunk.chunk_id: (bundle.chunk.tenant_id, bundle.chunk.text, bundle.chunk.checksum)
            for plan in self.fixture.plans for bundle in plan.bundles
        }
        chunks = self._query(
            "MATCH (c:Chunk) RETURN c.chunk_id AS id, c.tenant_id AS tenant, "
            "c.text AS text, c.checksum AS checksum"
        )
        self.assertEqual(len(chunks), 120)
        self.assertEqual({row["id"]: (row["tenant"], row["text"], row["checksum"]) for row in chunks},
                         expected_chunks)
        generations = self._query(
            "MATCH (s:TenantCorpusState)-[:ACTIVE_EMBEDDING_INDEX]->(g:EmbeddingIndexGeneration) "
            "RETURN s.tenant_id AS tenant, g.embedding_space_id AS space, g.index_name AS name"
        )
        self.assertEqual({row["tenant"] for row in generations}, {"tenant-alpha", "tenant-beta"})
        self.assertEqual(len(generations), 2)
        for generation in generations:
            tenant = generation["tenant"]
            bundle = next(plan.bundles[0] for plan in self.fixture.plans if plan.tenant_id == tenant)
            self.assertEqual(generation["space"], self.embedder.embedding_space_id)
            hits = self._query(
                "CALL db.index.vector.queryNodes($index_name, 120, $vector) "
                "YIELD node, score MATCH (c:Chunk)-[:HAS_EMBEDDING]->(node) "
                "WHERE node.tenant_id = $tenant AND c.tenant_id = $tenant "
                "AND c.chunk_id = $chunk_id "
                "RETURN c.text AS text, node.vector_checksum AS checksum, score",
                index_name=generation["name"], vector=list(bundle.embedding.vector),
                tenant=tenant, chunk_id=bundle.chunk.chunk_id,
            )
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["text"], bundle.chunk.text)
            self.assertEqual(hits[0]["checksum"], bundle.embedding.vector_checksum)
            self.assertAlmostEqual(hits[0]["score"], 1.0, places=6)
        self.assertEqual(verify_schema(self.driver, self.database), [])

    def test_corrupt_seed_vector_refuses_capture_without_changing_existing_work(self):
        bundle = self.fixture.plans[0].bundles[0]
        self._query(
            "CREATE (:KnowledgeConstructionJob:ResetTestSentinel "
            "{job_id: 'reset-corrupt-job', tenant_id: 'tenant-alpha'})"
        )
        before = self._query("MATCH (n) RETURN count(n) AS count")[0]["count"]
        try:
            self._query(
                "MATCH (e:ChunkEmbedding {embedding_id: $id}) SET e.vector_checksum = 'invalid'",
                id=bundle.embedding.embedding_id,
            )
            with self.assertRaises(ValueError):
                playground_reset_store.capture_reset_embeddings(
                    self.driver, self.database, self.fixture, self.embedder,
                )
            self.assertEqual(self._query("MATCH (n) RETURN count(n) AS count")[0]["count"], before)
            self.assertEqual(self._query("MATCH (n:ResetTestSentinel) RETURN count(n) AS count")[0]["count"], 1)
            self.assertEqual(self._query(
                "MATCH (e:ChunkEmbedding {embedding_id: $id}) RETURN e.vector_checksum AS checksum",
                id=bundle.embedding.embedding_id,
            )[0]["checksum"], "invalid")
        finally:
            self._query(
                "MATCH (e:ChunkEmbedding {embedding_id: $id}) SET e.vector_checksum = $checksum",
                id=bundle.embedding.embedding_id, checksum=bundle.embedding.vector_checksum,
            )
            self._query("MATCH (n:ResetTestSentinel) DETACH DELETE n")

    def test_reset_removes_work_preserves_indexes_and_restores_both_tenants_twice(self):
        cached = playground_reset_store.capture_reset_embeddings(
            self.driver, self.database, self.fixture, self.embedder,
        )
        provider_calls = self.embedder.calls
        before_indexes = self._index_identity()
        fixture_chunk = self.fixture.plans[0].bundles[0].chunk.chunk_id
        # These deliberately connected lifecycle sentinels cover documents,
        # immutable revisions, failed jobs, ontology, publications and audits.
        # The reset must not preserve a forgotten record kind or an orphan.
        self._query(
            "MATCH (seed:Chunk {chunk_id: $seed}) "
            "CREATE (d:Document:ResetTestSentinel {document_id:'reset-upload', tenant_id:'tenant-alpha'}), "
            "(v:DocumentVersion:ResetTestSentinel {version_id:'reset-upload-v1', tenant_id:'tenant-alpha'}), "
            "(c:Chunk:ResetTestSentinel {chunk_id:'reset-upload-c1', tenant_id:'tenant-alpha'}), "
            "(t:TBoxVersion:ResetTestSentinel {tbox_id:'reset-tbox', tenant_id:'tenant-alpha'}), "
            "(h:KnowledgeRecordHead:ResetTestSentinel {record_id:'reset-record', tenant_id:'tenant-alpha'}), "
            "(m:GovernedEntityMentionRevision:ResetTestSentinel {revision_id:'reset-mention', tenant_id:'tenant-alpha'}), "
            "(a:GovernedAssertionRevision:ResetTestSentinel {revision_id:'reset-fact', tenant_id:'tenant-alpha'}), "
            "(p:KnowledgePublication:ResetTestSentinel {publication_id:'reset-publication', tenant_id:'tenant-alpha'}), "
            "(s:KnowledgePublicationState:ResetTestSentinel {tenant_id:'tenant-alpha'}), "
            "(j:KnowledgeConstructionJob:ResetTestSentinel {job_id:'reset-failed-job', tenant_id:'tenant-beta', status:'FAILED'}), "
            "(o:KnowledgeConstructionChunkOutcome:ResetTestSentinel {outcome_id:'reset-outcome', tenant_id:'tenant-beta'}), "
            "(q:PublishedGraphQualityRun:ResetTestSentinel {run_id:'reset-quality', tenant_id:'tenant-alpha'}), "
            "(i:PublishedGraphQualityIssue:ResetTestSentinel {issue_id:'reset-issue', tenant_id:'tenant-alpha'}), "
            "(d)-[:HAS_VERSION]->(v), (v)-[:HAS_CHUNK]->(c), "
            "(h)-[:CURRENT_REVISION]->(m), (a)-[:EVIDENCE]->(seed), "
            "(p)-[:INCLUDES]->(a), (s)-[:ACTIVE_PUBLICATION]->(p), "
            "(j)-[:HAS_OUTCOME]->(o), (q)-[:HAS_ISSUE]->(i)",
            seed=fixture_chunk,
        )
        self.assertEqual(self._query("MATCH (n:ResetTestSentinel) RETURN count(n) AS count")[0]["count"], 13)
        for reset_number in (1, 2):
            with self.subTest(reset_number=reset_number):
                playground_reset_store.reset_playground_corpus(self.driver, self.database, cached)
                self.assertEqual(self._query("MATCH (n:ResetTestSentinel) RETURN count(n) AS count")[0]["count"], 0)
                self._assert_restored_seed()
                self.assertEqual(self._index_identity(), before_indexes)
                self.assertEqual(self.embedder.calls, provider_calls)
                refreshed = playground_reset_store.capture_reset_embeddings(
                    self.driver, self.database, self.fixture, self.embedder,
                )
                self.assertEqual(dict(refreshed.vectors), dict(cached.vectors))


if __name__ == "__main__":
    unittest.main()
