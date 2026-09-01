"""Vector-space generation coverage and atomic cutover tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import dataclasses
import ipaddress
import os
from threading import Event
import unittest
from urllib.parse import urlparse

import neo4j

from graphrag_prod.domain import (
    chunk_embedding_id,
    embedding_space_id,
)
from graphrag_prod.graph.provenance import Neo4jProvenanceStore
from graphrag_prod.graph.schema import apply_schema, verify_schema
from graphrag_prod.ingestion import (
    Checkpoint,
    IngestionConflict,
    IngestionInterrupted,
    Neo4jEmbeddingIndexManager,
    Neo4jIngestionService,
)
from tests.fixtures.ingestion import CHUNKS_V2, FixedClock, make_plan


class PauseAfterEmbeddingMembershipCheck:
    """Expose the read/write boundary without blocking the test thread."""

    def __init__(self) -> None:
        self.checked = Event()
        self.resume = Event()
        self.fired = False

    def __call__(self, checkpoint: Checkpoint, context: dict[str, object]) -> None:
        del context
        if (
            checkpoint is Checkpoint.AFTER_EMBEDDING_MEMBERSHIP_CHECK
            and not self.fired
        ):
            self.fired = True
            self.checked.set()
            if not self.resume.wait(timeout=10):
                raise AssertionError("embedding membership race hook timed out")


class Neo4jEmbeddingGenerationIntegrationTests(unittest.TestCase):
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
        if records[0]["count"] != 0:
            cls.driver.close()
            raise RuntimeError("disposable Neo4j database must start empty")
        apply_schema(cls.driver, cls.database)
        cls.driver.execute_query("CALL db.awaitIndexes(60)", database_=cls.database)
        schema_errors = verify_schema(cls.driver, cls.database)
        if schema_errors:
            cls.driver.close()
            raise RuntimeError(f"schema verification failed: {schema_errors}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.driver.close()

    def setUp(self) -> None:
        self.driver.execute_query(
            "MATCH (node) DETACH DELETE node",
            database_=self.database,
        )
        self.clock = FixedClock()
        self.service = Neo4jIngestionService(
            self.driver,
            self.database,
            worker_id="embedding-generation-test-worker",
            clock=self.clock,
        )
        self.manager = Neo4jEmbeddingIndexManager(self.driver, self.database)

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

    @staticmethod
    def _embeddings(plan) -> tuple:
        return tuple(
            embedding
            for bundle in plan.bundles
            for embedding in bundle.all_embeddings
        )

    def test_generation_requires_complete_coverage_and_activates_atomically(self) -> None:
        plan = make_plan(operation_key="embedding-generation-base")
        self.service.ingest(plan)
        embeddings = self._embeddings(plan)

        # materialize is intentionally repeatable: the vectors already written
        # by ingestion must be accepted as the same immutable values.
        self.assertEqual(self.manager.materialize(embeddings), len(embeddings))
        prepared = self.manager.prepare(
            tenant_id=plan.tenant_id,
            embedding_profile=embeddings[0],
            generation_version=1,
        )
        self.assertEqual(prepared.state, "READY")
        self.assertEqual(prepared.embedding_space_id, embeddings[0].embedding_space_id)
        coverage = self.manager.coverage(prepared.generation_id)
        self.assertEqual(coverage.total_chunks, len(plan.bundles))
        self.assertEqual(coverage.covered_chunks, len(plan.bundles))
        self.assertTrue(coverage.complete)

        activated = self.manager.activate(
            prepared.generation_id,
            expected_active_generation_id=None,
        )
        self.assertEqual(activated.state, "ACTIVE")
        self.assertIsNotNone(activated.corpus_revision)
        self.assertEqual(
            self.manager.active_generation(plan.tenant_id).generation_id,
            activated.generation_id,
        )

        pointer = self._records(
            """
            MATCH (state:TenantCorpusState {tenant_id: $tenant_id})
                  -[:ACTIVE_EMBEDDING_INDEX]->(generation)
            RETURN count(generation) AS pointers,
                   collect(generation.embedding_space_id) AS spaces
            """,
            tenant_id=plan.tenant_id,
        )[0]
        self.assertEqual(pointer["pointers"], 1)
        self.assertEqual(pointer["spaces"], [embeddings[0].embedding_space_id])

    def test_legacy_writer_cannot_bypass_managed_acl_or_vector_lifecycle(self) -> None:
        plan = make_plan(operation_key="legacy-writer-managed-boundary")
        self.service.ingest(plan)
        embeddings = self._embeddings(plan)
        prepared = self.manager.prepare(
            tenant_id=plan.tenant_id,
            embedding_profile=embeddings[0],
            generation_version=1,
        )
        active = self.manager.activate(
            prepared.generation_id,
            expected_active_generation_id=None,
        )
        original = plan.bundles[0]
        changed_groups = frozenset({"legacy-bypass"})
        legacy_mutation = dataclasses.replace(
            original,
            document=dataclasses.replace(
                original.document,
                access_policy_version=original.document.access_policy_version + 1,
                access_groups=changed_groups,
            ),
            chunk=dataclasses.replace(
                original.chunk,
                access_policy_version=original.chunk.access_policy_version + 1,
                access_groups=changed_groups,
            ),
        )

        with self.assertRaisesRegex(ValueError, "legacy write_bundle is disabled"):
            Neo4jProvenanceStore(self.driver, self.database).write_bundle(
                legacy_mutation
            )

        unchanged = self._records(
            """
            MATCH (document:Document {document_id: $document_id})
            MATCH (chunk:Chunk {chunk_id: $chunk_id})
            MATCH (state:TenantCorpusState {tenant_id: $tenant_id})
                  -[:ACTIVE_EMBEDDING_INDEX]->(generation)
            RETURN document.access_policy_version AS document_policy_version,
                   document.access_groups AS document_groups,
                   chunk.access_policy_version AS chunk_policy_version,
                   chunk.access_groups AS chunk_groups,
                   generation.generation_id AS generation_id,
                   generation.state AS generation_state
            """,
            tenant_id=plan.tenant_id,
            document_id=plan.document_id,
            chunk_id=original.chunk.chunk_id,
        )[0]
        self.assertEqual(
            unchanged["document_policy_version"],
            original.document.access_policy_version,
        )
        self.assertEqual(
            unchanged["chunk_policy_version"],
            original.chunk.access_policy_version,
        )
        self.assertEqual(
            set(unchanged["document_groups"]),
            set(original.document.access_groups),
        )
        self.assertEqual(
            set(unchanged["chunk_groups"]),
            set(original.chunk.access_groups),
        )
        self.assertEqual(unchanged["generation_id"], active.generation_id)
        self.assertEqual(unchanged["generation_state"], "ACTIVE")

    def test_corrupt_zero_vector_is_not_counted_as_generation_coverage(self) -> None:
        plan = make_plan(operation_key="embedding-generation-zero-vector")
        self.service.ingest(plan)
        embeddings = self._embeddings(plan)
        corrupt = embeddings[0]

        self.driver.execute_query(
            """
            MATCH (:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })-[:ACTIVE_SNAPSHOT]->(:KnowledgeSnapshot)
              -[:INCLUDES_CHUNK]->(chunk:Chunk {
                  chunk_id: $chunk_id
              })-[:HAS_EMBEDDING]->(embedding:ChunkEmbedding {
                  embedding_id: $embedding_id
              })
            SET embedding.vector = $zero_vector,
                embedding.cosine_indexable = true
            """,
            tenant_id=plan.tenant_id,
            document_id=plan.document_id,
            chunk_id=corrupt.chunk_id,
            embedding_id=corrupt.embedding_id,
            zero_vector=[0.0] * corrupt.dimensions,
            database_=self.database,
        )
        prepared = self.manager.prepare(
            tenant_id=plan.tenant_id,
            embedding_profile=embeddings[0],
            generation_version=1,
        )

        coverage = self.manager.coverage(prepared.generation_id)
        self.assertEqual(coverage.total_chunks, len(plan.bundles))
        self.assertEqual(coverage.covered_chunks, len(plan.bundles) - 1)
        self.assertFalse(coverage.complete)
        with self.assertRaisesRegex(IngestionConflict, "coverage is incomplete"):
            self.manager.activate(
                prepared.generation_id,
                expected_active_generation_id=None,
            )
        self.assertIsNone(self.manager.active_generation(plan.tenant_id))

    def test_reprepare_active_generation_never_labels_staging_embeddings(
        self,
    ) -> None:
        active_plan = make_plan(operation_key="embedding-reprepare-active-v1")
        self.service.ingest(active_plan)
        active_embeddings = self._embeddings(active_plan)
        generation = self.manager.prepare(
            tenant_id=active_plan.tenant_id,
            embedding_profile=active_embeddings[0],
            generation_version=1,
        )
        generation = self.manager.activate(
            generation.generation_id,
            expected_active_generation_id=None,
        )

        def interrupt_after_first_chunk(
            checkpoint: Checkpoint,
            context: dict[str, object],
        ) -> None:
            del context
            if checkpoint is Checkpoint.AFTER_CHUNK_STAGE:
                raise IngestionInterrupted("leave one unpublished chunk staged")

        staged_plan = make_plan(
            operation_key="embedding-reprepare-staging-v2",
            chunk_specs=CHUNKS_V2,
            version_number=2,
            expected_active_snapshot_id=active_plan.snapshot.snapshot_id,
        )
        interrupted_service = Neo4jIngestionService(
            self.driver,
            self.database,
            worker_id="embedding-reprepare-staging-worker",
            clock=self.clock,
            failpoint=interrupt_after_first_chunk,
        )
        with self.assertRaisesRegex(IngestionInterrupted, "unpublished"):
            interrupted_service.ingest(staged_plan)

        staged_embedding_ids = {
            record["embedding_id"]
            for record in self._records(
                """
                MATCH (snapshot:KnowledgeSnapshot {
                    snapshot_id: $snapshot_id,
                    build_state: 'BUILDING'
                })-[:INCLUDES_CHUNK]->(chunk:Chunk)
                MATCH (chunk)-[:HAS_EMBEDDING]->(embedding:ChunkEmbedding)
                RETURN embedding.embedding_id AS embedding_id
                """,
                snapshot_id=staged_plan.snapshot.snapshot_id,
            )
        }
        self.assertTrue(staged_embedding_ids)

        reprepared = self.manager.prepare(
            tenant_id=active_plan.tenant_id,
            embedding_profile=active_embeddings[0],
            generation_version=1,
        )
        self.assertEqual(reprepared.generation_id, generation.generation_id)
        self.assertEqual(reprepared.state, "ACTIVE")

        active_embedding_ids = {
            record["embedding_id"]
            for record in self._records(
                """
                MATCH (:Document {
                    tenant_id: $tenant_id,
                    document_id: $document_id
                })-[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot)
                MATCH (snapshot)-[:INCLUDES_CHUNK]->(chunk:Chunk)
                MATCH (chunk)-[:HAS_EMBEDDING]->(embedding:ChunkEmbedding)
                RETURN embedding.embedding_id AS embedding_id
                """,
                tenant_id=active_plan.tenant_id,
                document_id=active_plan.document_id,
            )
        }
        labelled_embedding_ids = {
            record["embedding_id"]
            for record in self._records(
                f"""
                MATCH (embedding:ChunkEmbedding:{reprepared.label_name})
                RETURN embedding.embedding_id AS embedding_id
                """
            )
        }
        self.assertEqual(labelled_embedding_ids, active_embedding_ids)
        self.assertTrue(staged_embedding_ids.isdisjoint(labelled_embedding_ids))

    def test_snapshot_publish_invalidates_generation_and_spaces_never_mix(self) -> None:
        v1 = make_plan(operation_key="embedding-cutover-v1")
        self.service.ingest(v1)
        old_embeddings = self._embeddings(v1)
        old_generation = self.manager.prepare(
            tenant_id=v1.tenant_id,
            embedding_profile=old_embeddings[0],
            generation_version=1,
        )
        old_generation = self.manager.activate(
            old_generation.generation_id,
            expected_active_generation_id=None,
        )

        v2 = make_plan(
            operation_key="embedding-cutover-v2",
            chunk_specs=CHUNKS_V2,
            version_number=2,
            expected_active_snapshot_id=v1.snapshot.snapshot_id,
        )
        self.service.ingest(v2)

        # Corpus publication and vector selection share the tenant-state lock.
        # A generation verified against v1 must never remain selected for v2.
        self.assertIsNone(self.manager.active_generation(v1.tenant_id))
        stale = self.manager.get_generation(old_generation.generation_id)
        self.assertEqual(stale.state, "STALE")
        state = self._records(
            """
            MATCH (state:TenantCorpusState {tenant_id: $tenant_id})
            OPTIONAL MATCH (state)-[:ACTIVE_EMBEDDING_INDEX]->(active)
            RETURN state.corpus_revision AS corpus_revision,
                   count(active) AS active_pointers
            """,
            tenant_id=v1.tenant_id,
        )[0]
        self.assertGreater(state["corpus_revision"], old_generation.corpus_revision)
        self.assertEqual(state["active_pointers"], 0)

        new_space = embedding_space_id(
            "fixture",
            "migration-four-dimensional",
            "v2",
            4,
            "none",
        )
        new_embeddings = tuple(
            dataclasses.replace(
                embedding,
                embedding_id=chunk_embedding_id(embedding.chunk_id, new_space),
                embedding_space_id=new_space,
                model="migration-four-dimensional",
                revision="v2",
                vector=tuple(reversed(embedding.vector)),
            )
            for embedding in self._embeddings(v2)
        )

        uncovered = self.manager.prepare(
            tenant_id=v2.tenant_id,
            embedding_profile=new_embeddings[0],
            generation_version=2,
        )
        self.assertEqual(
            self.manager.coverage(uncovered.generation_id).covered_chunks,
            0,
        )
        with self.assertRaisesRegex(IngestionConflict, "coverage is incomplete"):
            self.manager.activate(
                uncovered.generation_id,
                expected_active_generation_id=None,
            )
        self.assertIsNone(self.manager.active_generation(v2.tenant_id))

        self.assertEqual(
            self.manager.materialize(new_embeddings),
            len(new_embeddings),
        )
        prepared = self.manager.prepare(
            tenant_id=v2.tenant_id,
            embedding_profile=new_embeddings[0],
            generation_version=2,
        )
        self.assertTrue(self.manager.coverage(prepared.generation_id).complete)
        active = self.manager.activate(
            prepared.generation_id,
            expected_active_generation_id=None,
        )
        self.assertEqual(active.embedding_space_id, new_space)

        wrong_space = self._records(
            f"""
            MATCH (embedding:ChunkEmbedding:{active.label_name})
            WHERE embedding.embedding_space_id <> $embedding_space_id
            RETURN count(embedding) AS count
            """,
            embedding_space_id=new_space,
        )[0]["count"]
        self.assertEqual(wrong_space, 0)
        self.assertEqual(
            self.manager.active_generation(v2.tenant_id).generation_id,
            active.generation_id,
        )

    def test_conditional_materialization_cannot_cross_document_snapshot_boundary(
        self,
    ) -> None:
        document_a = make_plan(
            operation_key="conditional-materialize-document-a",
            canonical_uri="https://example.com/knowledge/conditional-a",
        )
        document_b = make_plan(
            operation_key="conditional-materialize-document-b",
            canonical_uri="https://example.com/knowledge/conditional-b",
        )
        self.service.ingest(document_a)
        self.service.ingest(document_b)

        foreign_space = embedding_space_id(
            "fixture",
            "conditional-cross-document-four-dimensional",
            "v2",
            4,
            "none",
        )
        document_b_backfill = tuple(
            dataclasses.replace(
                embedding,
                embedding_id=chunk_embedding_id(embedding.chunk_id, foreign_space),
                embedding_space_id=foreign_space,
                model="conditional-cross-document-four-dimensional",
                revision="v2",
                vector=tuple(reversed(embedding.vector)),
            )
            for embedding in self._embeddings(document_b)
        )

        with self.assertRaisesRegex(IngestionConflict, "not an active tenant chunk"):
            self.manager.materialize_if_snapshot_active(
                document_b_backfill,
                snapshot_id=document_a.snapshot.snapshot_id,
                source_generation=document_a.source_generation,
            )

        residue = self._records(
            """
            OPTIONAL MATCH (embedding:ChunkEmbedding {
                tenant_id: $tenant_id,
                embedding_space_id: $embedding_space_id
            })
            WITH count(embedding) AS embeddings
            OPTIONAL MATCH (:Chunk)-[link:HAS_EMBEDDING]->(
                linked:ChunkEmbedding {
                    tenant_id: $tenant_id,
                    embedding_space_id: $embedding_space_id
                }
            )
            RETURN embeddings, count(link) AS links
            """,
            tenant_id=document_b.tenant_id,
            embedding_space_id=foreign_space,
        )[0]
        self.assertEqual(residue["embeddings"], 0)
        self.assertEqual(residue["links"], 0)

    def test_delete_and_embedding_backfill_serialize_without_orphan_embedding(
        self,
    ) -> None:
        plan = make_plan(operation_key="embedding-backfill-delete-race")
        self.service.ingest(plan)
        new_space = embedding_space_id(
            "fixture",
            "delete-race-four-dimensional",
            "v2",
            4,
            "none",
        )
        backfill = tuple(
            dataclasses.replace(
                embedding,
                embedding_id=chunk_embedding_id(embedding.chunk_id, new_space),
                embedding_space_id=new_space,
                model="delete-race-four-dimensional",
                revision="v2",
                vector=tuple(reversed(embedding.vector)),
            )
            for embedding in self._embeddings(plan)
        )
        pause = PauseAfterEmbeddingMembershipCheck()
        racing_manager = Neo4jEmbeddingIndexManager(
            self.driver,
            self.database,
            failpoint=pause,
        )
        delete_service = Neo4jIngestionService(
            self.driver,
            self.database,
            worker_id="embedding-backfill-delete-race-deleter",
            clock=self.clock,
        )

        materialize_error = None
        delete_completed_while_paused = False
        with ThreadPoolExecutor(max_workers=2) as executor:
            materialize_future = executor.submit(racing_manager.materialize, backfill)
            self.assertTrue(
                pause.checked.wait(timeout=10),
                "materialize never reached its active-membership boundary",
            )
            delete_future = executor.submit(
                delete_service.delete_document,
                tenant_id=plan.tenant_id,
                document_id=plan.document_id,
                operation_key="delete-during-embedding-backfill",
                expected_active_snapshot_id=plan.snapshot.snapshot_id,
                source_generation=plan.source_generation,
            )
            try:
                delete_future.result(timeout=1)
                delete_completed_while_paused = True
            except FutureTimeoutError:
                # Holding TenantCorpusState is also correct: deletion will
                # commit after materialization releases the corpus lock.
                pass
            finally:
                pause.resume.set()

            try:
                materialized = materialize_future.result(timeout=10)
            except IngestionConflict as error:
                materialize_error = error
                materialized = None
            deleted = delete_future.result(timeout=10)

        self.assertTrue(pause.fired)
        if delete_completed_while_paused:
            self.assertIsInstance(materialize_error, IngestionConflict)
            self.assertIsNone(materialized)
        else:
            self.assertIn(materialized, (None, len(backfill)))
        self.assertEqual(deleted.job.status.value, "SUCCEEDED")
        self.assertEqual(deleted.job.outcome, "DELETED")

        residue = self._records(
            """
            OPTIONAL MATCH (document:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            WITH count(document) AS documents
            OPTIONAL MATCH (version:DocumentVersion {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            WITH documents, count(version) AS versions
            OPTIONAL MATCH (chunk:Chunk {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            WITH documents, versions, count(chunk) AS chunks
            OPTIONAL MATCH (embedding:ChunkEmbedding {tenant_id: $tenant_id})
            WITH documents, versions, chunks, count(embedding) AS embeddings
            OPTIONAL MATCH (mention:EntityMention {tenant_id: $tenant_id})
            WITH documents, versions, chunks, embeddings, count(mention) AS mentions
            OPTIONAL MATCH (assertion:Assertion {tenant_id: $tenant_id})
            WITH documents, versions, chunks, embeddings, mentions,
                 count(assertion) AS assertions
            OPTIONAL MATCH (entity:Entity {tenant_id: $tenant_id})
            WITH documents, versions, chunks, embeddings, mentions, assertions,
                 count(entity) AS entities
            OPTIONAL MATCH (snapshot:KnowledgeSnapshot {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            WITH documents, versions, chunks, embeddings, mentions, assertions,
                 entities, count(snapshot) AS snapshots
            OPTIONAL MATCH (artifact:DerivationArtifact {tenant_id: $tenant_id})
            WITH documents, versions, chunks, embeddings, mentions, assertions,
                 entities, snapshots, count(artifact) AS artifacts
            OPTIONAL MATCH (job:IngestionJob {
                tenant_id: $tenant_id,
                document_id: $document_id
            })-[:HAS_TASK]->(task:IngestionTask)
            RETURN documents, versions, chunks, embeddings, mentions, assertions,
                   entities, snapshots, artifacts, count(DISTINCT task) AS tasks
            """,
            tenant_id=plan.tenant_id,
            document_id=plan.document_id,
        )[0]
        for key in (
            "documents",
            "versions",
            "chunks",
            "embeddings",
            "mentions",
            "assertions",
            "entities",
            "snapshots",
            "artifacts",
            "tasks",
        ):
            self.assertEqual(residue[key], 0, key)
        tombstones = self._records(
            """
            MATCH (tombstone:DocumentTombstone {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            RETURN tombstone.generation AS generation
            """,
            tenant_id=plan.tenant_id,
            document_id=plan.document_id,
        )
        self.assertEqual(len(tombstones), 1)
        self.assertEqual(tombstones[0]["generation"], 1)


if __name__ == "__main__":
    unittest.main()
