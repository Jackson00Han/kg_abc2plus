"""Disposable-Neo4j tests for governed logical document retirement."""

from __future__ import annotations

import ipaddress
import os
import unittest
from urllib.parse import urlparse

import neo4j

from graphrag_prod.domain.access import Principal
from graphrag_prod.graph.schema import apply_schema, verify_schema
from graphrag_prod.ingestion.retirement import (
    DocumentRetirementConflict,
    DocumentRetirementRequest,
    Neo4jDocumentRetirementService,
)
from graphrag_prod.ingestion.service import Neo4jIngestionService
from tests.fixtures.ingestion import FixedClock, make_plan


class Neo4jDocumentRetirementIntegrationTests(unittest.TestCase):
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
            raise RuntimeError("integration tests accept only loopback Neo4j")
        cls.database = os.environ["TEST_NEO4J_DATABASE"]
        cls.driver = neo4j.GraphDatabase.driver(
            uri,
            auth=(
                os.environ["TEST_NEO4J_USER"],
                os.environ["TEST_NEO4J_PASSWORD"],
            ),
        )
        cls.driver.verify_connectivity()
        records, _, _ = cls.driver.execute_query(
            "MATCH (node) RETURN count(node) AS count",
            database_=cls.database,
        )
        if int(records[0]["count"]) != 0:
            cls.driver.close()
            raise RuntimeError("disposable Neo4j database must start empty")
        apply_schema(cls.driver, cls.database)
        cls.driver.execute_query("CALL db.awaitIndexes(60)", database_=cls.database)
        errors = verify_schema(cls.driver, cls.database)
        if errors:
            cls.driver.close()
            raise RuntimeError(f"schema verification failed: {errors}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.driver.close()

    def setUp(self) -> None:
        self.driver.execute_query(
            "MATCH (node) DETACH DELETE node",
            database_=self.database,
        )
        self.clock = FixedClock()
        self.ingestion = Neo4jIngestionService(
            self.driver,
            self.database,
            worker_id="retirement-integration-ingestion",
            clock=self.clock,
        )
        self.retirement = Neo4jDocumentRetirementService(
            self.driver,
            self.database,
            clock=self.clock,
        )

    def tearDown(self) -> None:
        self.driver.execute_query(
            "MATCH (node) DETACH DELETE node",
            database_=self.database,
        )

    def _records(self, query: str, **parameters: object) -> list[neo4j.Record]:
        records, _, _ = self.driver.execute_query(
            query,
            parameters_=parameters,
            database_=self.database,
        )
        return records

    def test_retirement_is_hidden_audited_idempotent_and_reactivatable(self) -> None:
        tenant_id = "tenant-retirement-integration"
        v1 = make_plan(
            tenant_id=tenant_id,
            operation_key="retirement-source-v1",
        )
        self.ingestion.ingest(v1)
        embedding_space_id = v1.bundles[0].all_embeddings[0].embedding_space_id
        self.driver.execute_query(
            """
            MATCH (state:TenantCorpusState {tenant_id: $tenant_id})
            CREATE (generation:EmbeddingIndexGeneration {
                generation_id: $generation_id,
                tenant_id: $tenant_id,
                embedding_space_id: $embedding_space_id,
                generation_version: 1,
                state: 'ACTIVE'
            })
            CREATE (state)-[:ACTIVE_EMBEDDING_INDEX]->(generation)
            """,
            tenant_id=tenant_id,
            generation_id="retirement-test-generation",
            embedding_space_id=embedding_space_id,
            database_=self.database,
        )
        principal = Principal(
            principal_id="retirement-operator",
            tenant_id=tenant_id,
            groups=frozenset({"knowledge-readers"}),
            capabilities=frozenset({"knowledge:lifecycle"}),
        )
        active_views = self.retirement.list_active_documents(principal)
        self.assertEqual(len(active_views), 1)
        self.assertEqual(active_views[0].document_id, v1.document_id)
        self.assertEqual(active_views[0].chunk_count, len(v1.bundles))
        self.assertFalse(active_views[0].blocked)
        request = DocumentRetirementRequest(
            document_id=v1.document_id,
            operation_key="retire-source-v1",
            expected_active_snapshot_id=v1.snapshot.snapshot_id,
            source_generation=0,
        )

        first = self.retirement.retire(principal, request)
        replay = self.retirement.retire(principal, request)

        self.assertEqual(first, replay)
        self.assertEqual(self.retirement.list_active_documents(principal), ())
        self.assertEqual(first.corpus_revision, 2)
        retired = self._records(
            """
            MATCH (document:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })-[:HAS_VERSION]->(version:DocumentVersion)
              -[:HAS_CHUNK]->(chunk:Chunk)
            MATCH (snapshot:KnowledgeSnapshot {
                tenant_id: $tenant_id,
                snapshot_id: $snapshot_id
            })-[:INCLUDES_CHUNK]->(chunk)
            MATCH (tombstone:DocumentTombstone {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            MATCH (event:IngestionJob {
                tenant_id: $tenant_id,
                document_id: $document_id,
                operation: 'RETIRE'
            })
            MATCH (state:TenantCorpusState {tenant_id: $tenant_id})
            MATCH (generation:EmbeddingIndexGeneration {
                generation_id: $generation_id
            })
            OPTIONAL MATCH (document)-[active_snapshot:ACTIVE_SNAPSHOT]->()
            OPTIONAL MATCH (document)-[active_version:ACTIVE_VERSION]->()
            OPTIONAL MATCH (state)-[active_index:ACTIVE_EMBEDDING_INDEX]->()
            RETURN document.lifecycle_status AS document_status,
                   document.generation AS document_generation,
                   snapshot.build_state AS snapshot_state,
                   version.lifecycle_status AS version_status,
                   count(DISTINCT chunk) AS chunks,
                   count(DISTINCT CASE
                       WHEN chunk.retrieval_scope IS NOT NULL THEN chunk
                   END) AS scoped_chunks,
                   count(DISTINCT active_snapshot) AS active_snapshots,
                   count(DISTINCT active_version) AS active_versions,
                   count(DISTINCT active_index) AS active_indexes,
                   generation.state AS embedding_state,
                   tombstone.lifecycle_status AS tombstone_status,
                   tombstone.generation AS tombstone_generation,
                   tombstone.corpus_revision AS retirement_revision,
                   event.status AS event_status,
                   event.outcome AS event_outcome,
                   state.corpus_revision AS corpus_revision
            """,
            tenant_id=tenant_id,
            document_id=v1.document_id,
            snapshot_id=v1.snapshot.snapshot_id,
            generation_id="retirement-test-generation",
        )[0]
        self.assertEqual(retired["document_status"], "RETIRED")
        self.assertEqual(retired["document_generation"], 1)
        self.assertEqual(retired["snapshot_state"], "RETIRED")
        self.assertEqual(retired["version_status"], "RETIRED")
        self.assertEqual(retired["chunks"], len(v1.bundles))
        self.assertEqual(retired["scoped_chunks"], 0)
        self.assertEqual(retired["active_snapshots"], 0)
        self.assertEqual(retired["active_versions"], 0)
        self.assertEqual(retired["active_indexes"], 0)
        self.assertEqual(retired["embedding_state"], "STALE")
        self.assertEqual(retired["tombstone_status"], "RETIRED")
        self.assertEqual(retired["tombstone_generation"], 1)
        self.assertEqual(retired["retirement_revision"], 2)
        self.assertEqual(retired["event_status"], "SUCCEEDED")
        self.assertEqual(retired["event_outcome"], "RETIRED")
        self.assertEqual(retired["corpus_revision"], 2)

        # Reactivating identical content/profile deliberately reuses the same
        # Version and Snapshot IDs. Current lifecycle markers must be cleared,
        # while the immutable first retirement event remains linked.
        v2 = make_plan(
            tenant_id=tenant_id,
            operation_key="reactivate-identical-source",
            expected_active_snapshot_id=None,
            source_generation=1,
        )
        self.assertEqual(v2.version_id, v1.version_id)
        self.assertEqual(v2.snapshot.snapshot_id, v1.snapshot.snapshot_id)
        reactivated = self.ingestion.ingest(v2)
        self.assertEqual(reactivated.active_snapshot_id, v2.snapshot.snapshot_id)
        active = self._records(
            """
            MATCH (document:Document {
                tenant_id: $tenant_id,
                document_id: $document_id,
                lifecycle_status: 'ACTIVE'
            })-[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot {
                snapshot_id: $snapshot_id,
                build_state: 'PUBLISHED'
            })
            MATCH (document)-[:ACTIVE_VERSION]->(version:DocumentVersion {
                version_id: $version_id
            })-[:HAS_CHUNK]->(chunk:Chunk)
            MATCH (tombstone:DocumentTombstone {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            MATCH (tombstone)-[:HAS_RETIREMENT_EVENT]->(event:IngestionJob {
                job_id: $retirement_id
            })
            MATCH (event)-[:RETIRED_DOCUMENT]->(document)
            MATCH (event)-[:RETIRED_SNAPSHOT]->(snapshot)
            MATCH (event)-[:RETIRED_VERSION]->(version)
            RETURN document.retirement_id IS NULL AS retirement_id_cleared,
                   document.retirement_request_fingerprint IS NULL
                       AS fingerprint_cleared,
                   document.retired_at IS NULL AS retired_at_cleared,
                   document.generation AS document_generation,
                   snapshot.retirement_id IS NULL
                       AS snapshot_retirement_cleared,
                   snapshot.retired_at IS NULL AS snapshot_retired_at_cleared,
                   version.lifecycle_status AS version_status,
                   version.retirement_id IS NULL AS version_retirement_cleared,
                   version.retired_at IS NULL AS version_retired_at_cleared,
                   count(DISTINCT CASE
                       WHEN chunk.retrieval_scope IS NOT NULL THEN chunk
                   END) AS scoped_chunks,
                   tombstone.retirement_id AS retained_retirement_id,
                   event.retired_by_principal_id AS retained_actor,
                   event.retired_at AS retained_retired_at
            """,
            tenant_id=tenant_id,
            document_id=v2.document_id,
            snapshot_id=v2.snapshot.snapshot_id,
            version_id=v2.version_id,
            retirement_id=first.retirement_id,
        )[0]
        self.assertTrue(active["retirement_id_cleared"])
        self.assertTrue(active["fingerprint_cleared"])
        self.assertTrue(active["retired_at_cleared"])
        self.assertTrue(active["snapshot_retirement_cleared"])
        self.assertTrue(active["snapshot_retired_at_cleared"])
        self.assertEqual(active["version_status"], "ACTIVE")
        self.assertTrue(active["version_retirement_cleared"])
        self.assertTrue(active["version_retired_at_cleared"])
        self.assertEqual(active["document_generation"], 1)
        self.assertEqual(active["scoped_chunks"], len(v2.bundles))
        self.assertEqual(active["retained_retirement_id"], first.retirement_id)
        self.assertEqual(active["retained_actor"], principal.principal_id)
        self.assertEqual(active["retained_retired_at"].to_native(), first.retired_at)

        self.clock.advance(seconds=60)
        second_principal = Principal(
            principal_id="retirement-operator-two",
            tenant_id=tenant_id,
            groups=frozenset({"knowledge-readers"}),
            capabilities=frozenset({"knowledge:lifecycle"}),
        )
        second_request = DocumentRetirementRequest(
            document_id=v2.document_id,
            operation_key="retire-identical-source-second-cycle",
            expected_active_snapshot_id=v2.snapshot.snapshot_id,
            source_generation=1,
        )
        second = self.retirement.retire(second_principal, second_request)
        self.assertEqual(
            self.retirement.retire(second_principal, second_request), second
        )
        history = self._records(
            """
            MATCH (document:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            MATCH (tombstone:DocumentTombstone {
                tenant_id: $tenant_id,
                document_id: $document_id
            })-[:HAS_RETIREMENT_EVENT]->(event:IngestionJob {operation: 'RETIRE'})
            MATCH (event)-[:RETIRED_DOCUMENT]->(document)
            MATCH (event)-[:RETIRED_SNAPSHOT]->(:KnowledgeSnapshot {
                snapshot_id: $snapshot_id
            })
            MATCH (event)-[:RETIRED_VERSION]->(:DocumentVersion {
                version_id: $version_id
            })
            RETURN event.operation_key AS operation_key,
                   event.source_generation AS generation_before,
                   event.source_generation_after AS generation_after,
                   event.retired_by_principal_id AS actor,
                   event.retired_at AS retired_at,
                   tombstone.retirement_id AS current_retirement_id,
                   document.generation AS current_generation
            ORDER BY generation_before
            """,
            tenant_id=tenant_id,
            document_id=v2.document_id,
            snapshot_id=v2.snapshot.snapshot_id,
            version_id=v2.version_id,
        )
        self.assertEqual(len(history), 2)
        self.assertEqual(
            [row["generation_before"] for row in history],
            [0, 1],
        )
        self.assertEqual(
            [row["generation_after"] for row in history],
            [1, 2],
        )
        self.assertEqual(
            [row["actor"] for row in history],
            [principal.principal_id, second_principal.principal_id],
        )
        self.assertNotEqual(history[0]["retired_at"], history[1]["retired_at"])
        self.assertTrue(
            all(row["current_retirement_id"] == second.retirement_id for row in history)
        )
        self.assertTrue(all(row["current_generation"] == 2 for row in history))

    def test_broken_active_ownership_fails_closed_and_keeps_retrieval_scope(
        self,
    ) -> None:
        tenant_id = "tenant-retirement-corrupt-provenance"
        plan = make_plan(tenant_id=tenant_id, operation_key="corrupt-source-v1")
        self.ingestion.ingest(plan)
        principal = Principal(
            principal_id="corruption-auditor",
            tenant_id=tenant_id,
            groups=frozenset({"knowledge-readers"}),
            capabilities=frozenset({"knowledge:lifecycle"}),
        )
        hidden_chunk_id = plan.bundles[0].chunk.chunk_id
        self.driver.execute_query(
            """
            MATCH (chunk:Chunk {chunk_id: $chunk_id})
            SET chunk.access_groups = ['restricted-other-group']
            """,
            chunk_id=hidden_chunk_id,
            database_=self.database,
        )
        self.assertEqual(self.retirement.list_active_documents(principal), ())
        self.driver.execute_query(
            """
            MATCH (chunk:Chunk {chunk_id: $chunk_id})
            SET chunk.access_groups = ['knowledge-readers']
            """,
            chunk_id=hidden_chunk_id,
            database_=self.database,
        )
        self.assertEqual(len(self.retirement.list_active_documents(principal)), 1)
        self.driver.execute_query(
            """
            MATCH (document:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })-[ownership:HAS_VERSION]->(version:DocumentVersion {
                version_id: $version_id
            })
            DELETE ownership
            """,
            tenant_id=tenant_id,
            document_id=plan.document_id,
            version_id=plan.version_id,
            database_=self.database,
        )
        with self.assertRaises(DocumentRetirementConflict):
            self.retirement.retire(
                principal,
                DocumentRetirementRequest(
                    document_id=plan.document_id,
                    operation_key="retire-corrupt-source",
                    expected_active_snapshot_id=plan.snapshot.snapshot_id,
                    source_generation=0,
                ),
            )

        state = self._records(
            """
            MATCH (document:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            OPTIONAL MATCH (document)-[snapshot_pointer:ACTIVE_SNAPSHOT]->()
            OPTIONAL MATCH (document)-[version_pointer:ACTIVE_VERSION]->()
            OPTIONAL MATCH (chunk:Chunk {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            RETURN count(DISTINCT snapshot_pointer) AS snapshot_pointers,
                   count(DISTINCT version_pointer) AS version_pointers,
                   count(DISTINCT CASE
                       WHEN chunk.retrieval_scope IS NOT NULL THEN chunk
                   END) AS scoped_chunks,
                   COUNT {
                       MATCH (:DocumentTombstone {
                           tenant_id: $tenant_id,
                           document_id: $document_id
                       })
                   } AS tombstones,
                   COUNT {
                       MATCH (:IngestionJob {
                           tenant_id: $tenant_id,
                           document_id: $document_id,
                           operation: 'RETIRE'
                       })
                   } AS retirement_events
            """,
            tenant_id=tenant_id,
            document_id=plan.document_id,
        )[0]
        self.assertEqual(state["snapshot_pointers"], 1)
        self.assertEqual(state["version_pointers"], 1)
        self.assertEqual(state["scoped_chunks"], len(plan.bundles))
        self.assertEqual(state["tombstones"], 0)
        self.assertEqual(state["retirement_events"], 0)

    def test_tampered_retirement_event_replay_fails_closed(self) -> None:
        tenant_id = "tenant-retirement-tamper"
        plan = make_plan(tenant_id=tenant_id, operation_key="tamper-source-v1")
        self.ingestion.ingest(plan)
        principal = Principal(
            principal_id="tamper-auditor",
            tenant_id=tenant_id,
            groups=frozenset({"knowledge-readers"}),
            capabilities=frozenset({"knowledge:lifecycle"}),
        )
        request = DocumentRetirementRequest(
            document_id=plan.document_id,
            operation_key="retire-tamper-source",
            expected_active_snapshot_id=plan.snapshot.snapshot_id,
            source_generation=0,
        )
        result = self.retirement.retire(principal, request)
        self.driver.execute_query(
            """
            MATCH (event:IngestionJob {job_id: $retirement_id})
            SET event.idempotency_key = 'tampered-key',
                event.target_snapshot_id = 'tampered-snapshot'
            """,
            retirement_id=result.retirement_id,
            database_=self.database,
        )

        with self.assertRaises(DocumentRetirementConflict):
            self.retirement.retire(principal, request)

        state = self._records(
            """
            MATCH (corpus:TenantCorpusState {tenant_id: $tenant_id})
            MATCH (document:Document {
                tenant_id: $tenant_id,
                document_id: $document_id,
                lifecycle_status: 'RETIRED'
            })
            RETURN corpus.corpus_revision AS corpus_revision,
                   document.generation AS generation,
                   COUNT { MATCH (document)-[:ACTIVE_SNAPSHOT]->() }
                       AS active_snapshots,
                   COUNT { MATCH (document)-[:ACTIVE_VERSION]->() }
                       AS active_versions
            """,
            tenant_id=tenant_id,
            document_id=plan.document_id,
        )[0]
        self.assertEqual(state["corpus_revision"], result.corpus_revision)
        self.assertEqual(state["generation"], 1)
        self.assertEqual(state["active_snapshots"], 0)
        self.assertEqual(state["active_versions"], 0)


if __name__ == "__main__":
    unittest.main()
