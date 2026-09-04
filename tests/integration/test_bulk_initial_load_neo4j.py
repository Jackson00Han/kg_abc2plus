"""Real-Neo4j tests for atomic batched initial bootstrap ingestion."""

from __future__ import annotations

import dataclasses
import ipaddress
import os
import unittest
from urllib.parse import urlparse

import neo4j

from graphrag_prod.domain import active_retrieval_scope
from graphrag_prod.graph.schema import apply_schema, verify_schema
from graphrag_prod.ingestion import (
    Checkpoint,
    IngestionConflict,
    IngestionInterrupted,
    JobPhase,
    JobStatus,
    Neo4jBulkInitialLoader,
    Neo4jEmbeddingIndexManager,
    Neo4jIngestionService,
)
from graphrag_prod.retrieval import (
    Neo4jRetrievalEngine,
    RetrievalLimits,
    RetrievalRequest,
)
from tests.fixtures.ingestion import (
    CHUNKS_V2,
    FixedClock,
    make_plan,
    make_principal,
)


class Neo4jBulkInitialLoadIntegrationTests(unittest.TestCase):
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
        self.loader = Neo4jBulkInitialLoader(
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

    @staticmethod
    def _with_partitioned_chunk_access(plan):
        document_groups = frozenset({"knowledge-readers", "audit-readers"})
        chunk_groups = (
            frozenset({"knowledge-readers"}),
            frozenset({"knowledge-readers", "audit-readers"}),
            frozenset({"knowledge-readers"}),
        )
        if len(chunk_groups) != len(plan.bundles):
            raise ValueError("fixture expects exactly three Chunks")
        return dataclasses.replace(
            plan,
            bundles=tuple(
                dataclasses.replace(
                    bundle,
                    document=dataclasses.replace(
                        bundle.document,
                        access_groups=document_groups,
                    ),
                    chunk=dataclasses.replace(
                        bundle.chunk,
                        access_groups=chunk_groups[index],
                    ),
                )
                for index, bundle in enumerate(plan.bundles)
            ),
        )

    def test_create_replay_v2_and_retrieval_compatibility(self) -> None:
        tenant_id = "tenant-bulk-lifecycle"
        v1 = self._with_partitioned_chunk_access(make_plan(tenant_id=tenant_id))
        created = self.loader.ingest(v1)
        self.assertEqual(created.outcome, "CREATED")
        self.assertEqual(created.corpus_revision, 1)
        self.assertEqual(created.chunk_count, 3)
        self.assertEqual(created.embedding_count, 3)

        replay = self.loader.ingest(v1)
        self.assertEqual(replay.outcome, "UNCHANGED")
        self.assertEqual(replay.corpus_revision, 1)

        v2 = self._with_partitioned_chunk_access(
            make_plan(
                operation_key="bulk-apple-v2",
                tenant_id=tenant_id,
                chunk_specs=CHUNKS_V2,
                version_number=2,
                expected_active_snapshot_id=v1.snapshot.snapshot_id,
            )
        )
        updated = self.loader.ingest(v2)
        self.assertEqual(updated.outcome, "UPDATED")
        self.assertEqual(updated.corpus_revision, 2)
        replay_v2 = self.loader.ingest(v2)
        self.assertEqual(replay_v2.outcome, "UNCHANGED")
        self.assertEqual(replay_v2.corpus_revision, 2)
        job = Neo4jIngestionService(
            self.driver,
            self.database,
            worker_id="bulk-job-reader",
            clock=self.clock,
        ).get_job_for_tenant(tenant_id, replay_v2.job_id)
        self.assertEqual(job.operation, "INITIAL_LOAD")
        self.assertEqual(job.operation_key, v2.operation_key)
        self.assertEqual(job.status, JobStatus.SUCCEEDED)
        self.assertEqual(job.phase, JobPhase.COMPLETE)
        self.assertEqual(job.max_attempts, 1)

        state = self._records(
            """
            MATCH (state:TenantCorpusState {tenant_id: $tenant_id})
            MATCH (document:Document {tenant_id: $tenant_id})
                  -[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot)
            MATCH (document)-[:ACTIVE_VERSION]->(version:DocumentVersion)
            MATCH (snapshot)-[:OF_VERSION]->(version)
            OPTIONAL MATCH (old:KnowledgeSnapshot {
                snapshot_id: $old_snapshot_id
            })
            RETURN state.corpus_revision AS revision,
                   state.lifecycle_mode AS lifecycle_mode,
                   snapshot.snapshot_id AS snapshot_id,
                   version.version_id AS version_id,
                   old.build_state AS old_state
            """,
            tenant_id=tenant_id,
            old_snapshot_id=v1.snapshot.snapshot_id,
        )[0]
        self.assertEqual(int(state["revision"]), 2)
        self.assertEqual(state["lifecycle_mode"], "OFFLINE_INITIAL_LOAD")
        self.assertEqual(state["snapshot_id"], v2.snapshot.snapshot_id)
        self.assertEqual(state["version_id"], v2.version_id)
        self.assertEqual(state["old_state"], "RETIRED")

        provenance = self._records(
            """
            MATCH (document:Document {tenant_id: $tenant_id})
                  -[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot)
                  -[:INCLUDES_CHUNK]->(chunk:Chunk)
            MATCH (document)-[:ACTIVE_VERSION]->(version:DocumentVersion)
            MATCH (snapshot)-[:OF_VERSION]->(version)
            MATCH (version)-[:HAS_CHUNK]->(chunk)
            MATCH (chunk)-[:HAS_EMBEDDING]->(embedding:ChunkEmbedding)
            MATCH (snapshot)-[mention_membership:INCLUDES_MENTION]->(
                mention:EntityMention
            )-[:IN_CHUNK]->(chunk)
            MATCH (mention)-[:REFERS_TO]->(entity:Entity)
            MATCH (snapshot)-[:INCLUDES_ENTITY]->(entity)
            WHERE chunk.access_policy_id = document.access_policy_id
              AND chunk.access_policy_version = document.access_policy_version
              AND all(
                  group IN chunk.access_groups
                  WHERE group IN document.access_groups
              )
              AND embedding.vector IS NOT NULL
              AND embedding.cosine_indexable = true
              AND mention_membership.entity_id = entity.entity_id
            RETURN count(DISTINCT chunk) AS chunks,
                   count(DISTINCT embedding) AS embeddings,
                   count(DISTINCT mention) AS mentions,
                   count(DISTINCT entity) AS entities
            """,
            tenant_id=tenant_id,
        )[0]
        self.assertEqual(int(provenance["chunks"]), 3)
        self.assertEqual(int(provenance["embeddings"]), 3)
        self.assertEqual(int(provenance["mentions"]), 3)
        self.assertEqual(int(provenance["entities"]), 1)

        chunk_access = self._records(
            """
            MATCH (:KnowledgeSnapshot {snapshot_id: $snapshot_id})
                  -[:INCLUDES_CHUNK]->(chunk:Chunk)
            RETURN chunk.chunk_id AS chunk_id,
                   chunk.access_policy_id AS policy_id,
                   chunk.access_policy_version AS policy_version,
                   chunk.access_groups AS groups,
                   chunk.retrieval_scope AS retrieval_scope
            ORDER BY chunk.chunk_id
            """,
            snapshot_id=v2.snapshot.snapshot_id,
        )
        expected_chunks = {
            bundle.chunk.chunk_id: bundle.chunk for bundle in v2.bundles
        }
        self.assertEqual(
            {row["chunk_id"] for row in chunk_access},
            set(expected_chunks),
        )
        for row in chunk_access:
            chunk = expected_chunks[row["chunk_id"]]
            self.assertEqual(row["policy_id"], chunk.access_policy_id)
            self.assertEqual(row["policy_version"], chunk.access_policy_version)
            self.assertEqual(set(row["groups"]), set(chunk.access_groups))
            self.assertEqual(
                row["retrieval_scope"],
                active_retrieval_scope(chunk.tenant_id, chunk.access_groups),
            )

        embedding_manager = Neo4jEmbeddingIndexManager(
            self.driver,
            self.database,
        )
        embedding = v2.bundles[0].all_embeddings[0]
        generation = embedding_manager.prepare(
            tenant_id=tenant_id,
            embedding_profile=embedding,
            generation_version=1,
        )
        embedding_manager.activate(
            generation.generation_id,
            expected_active_generation_id=None,
        )
        self.driver.execute_query(
            "CALL db.index.fulltext.awaitEventuallyConsistentIndexRefresh()",
            database_=self.database,
        )
        request = RetrievalRequest(
            "Apple margin",
            v2.bundles[1].all_embeddings[0].vector,
            make_principal(tenant_id),
            embedding.embedding_space_id,
            RetrievalLimits(top_k=3, anchor_k=1),
        )
        result = Neo4jRetrievalEngine(
            self.driver,
            self.database,
        ).retrieve(request)
        self.assertTrue(result.chunks)
        self.assertTrue(result.trace.graph_expansion)
        self.assertTrue(
            all(
                item.citation.version_id == v2.version_id
                for item in result.chunks
            )
        )
        with self.assertRaisesRegex(
            IngestionConflict,
            "disabled after managed ingestion begins",
        ):
            self.loader.ingest(v2)

    def test_bulk_fails_closed_without_clearing_retirement_markers(self) -> None:
        tenant_id = "tenant-bulk-retirement-guard"
        v1 = make_plan(tenant_id=tenant_id)
        self.loader.ingest(v1)
        self.driver.execute_query(
            """
            MATCH (document:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            SET document.lifecycle_status = 'RETIRED',
                document.retirement_id = 'unanchored-retirement',
                document.retirement_request_fingerprint = 'unanchored-fingerprint',
                document.retired_at = datetime('2026-01-02T00:00:00Z'),
                document.retired_by_principal_id = 'unknown-operator',
                document.retired_active_snapshot_id = $snapshot_id,
                document.retired_active_version_id = $version_id
            """,
            tenant_id=tenant_id,
            document_id=v1.document_id,
            snapshot_id=v1.snapshot.snapshot_id,
            version_id=v1.version_id,
            database_=self.database,
        )
        v2 = make_plan(
            tenant_id=tenant_id,
            operation_key="bulk-retirement-guard-v2",
            chunk_specs=CHUNKS_V2,
            version_number=2,
            expected_active_snapshot_id=v1.snapshot.snapshot_id,
        )

        with self.assertRaisesRegex(
            IngestionConflict,
            "cannot clear managed retirement audit state",
        ):
            self.loader.ingest(v2)

        state = self._records(
            """
            MATCH (corpus:TenantCorpusState {tenant_id: $tenant_id})
            MATCH (document:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })-[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot)
            RETURN corpus.corpus_revision AS corpus_revision,
                   document.lifecycle_status AS lifecycle_status,
                   document.retirement_id AS retirement_id,
                   document.retirement_request_fingerprint AS fingerprint,
                   document.retired_by_principal_id AS actor,
                   snapshot.snapshot_id AS active_snapshot_id,
                   COUNT {
                       MATCH (:DocumentVersion {version_id: $new_version_id})
                   } AS new_version_count
            """,
            tenant_id=tenant_id,
            document_id=v1.document_id,
            new_version_id=v2.version_id,
        )[0]
        self.assertEqual(state["corpus_revision"], 1)
        self.assertEqual(state["lifecycle_status"], "RETIRED")
        self.assertEqual(state["retirement_id"], "unanchored-retirement")
        self.assertEqual(state["fingerprint"], "unanchored-fingerprint")
        self.assertEqual(state["actor"], "unknown-operator")
        self.assertEqual(state["active_snapshot_id"], v1.snapshot.snapshot_id)
        self.assertEqual(state["new_version_count"], 0)

    def test_stable_id_conflict_rolls_back_the_document_transaction(self) -> None:
        plan = make_plan(tenant_id="tenant-bulk-conflict")
        chunk_id = plan.bundles[0].chunk.chunk_id
        self.driver.execute_query(
            "CREATE (:Chunk {chunk_id: $chunk_id, tenant_id: 'forged'})",
            chunk_id=chunk_id,
            database_=self.database,
        )
        with self.assertRaisesRegex(IngestionConflict, "stable ID"):
            self.loader.ingest(plan)
        counts = self._records(
            """
            MATCH (node)
            RETURN count(node) AS nodes,
                   count(CASE WHEN node:TenantCorpusState THEN node END) AS states,
                   count(CASE WHEN node:InitialLoadJob THEN node END) AS jobs,
                   count(CASE WHEN node:Document THEN node END) AS documents
            """
        )[0]
        self.assertEqual(int(counts["nodes"]), 1)
        self.assertEqual(int(counts["states"]), 0)
        self.assertEqual(int(counts["jobs"]), 0)
        self.assertEqual(int(counts["documents"]), 0)

    def test_interrupted_transaction_rolls_back_and_exact_retry_recovers(self) -> None:
        plan = make_plan(tenant_id="tenant-bulk-interrupted")

        def interrupt(checkpoint: Checkpoint, _context: dict[str, object]) -> None:
            if checkpoint is Checkpoint.BEFORE_PUBLISH:
                raise IngestionInterrupted("injected bulk interruption")

        interrupted = Neo4jBulkInitialLoader(
            self.driver,
            self.database,
            clock=self.clock,
            failpoint=interrupt,
        )
        with self.assertRaisesRegex(IngestionInterrupted, "bulk interruption"):
            interrupted.ingest(plan)
        counts = self._records(
            """
            MATCH (node)
            RETURN count(node) AS nodes,
                   count(CASE WHEN node:TenantCorpusState THEN node END) AS states,
                   count(CASE WHEN node:IngestionJob THEN node END) AS jobs,
                   count(CASE WHEN node:IngestionTask THEN node END) AS tasks,
                   count(CASE WHEN node:Document THEN node END) AS documents,
                   count(CASE WHEN node:KnowledgeSnapshot THEN node END) AS snapshots
            """
        )[0]
        self.assertEqual(
            {field: int(counts[field]) for field in counts.keys()},
            {
                "nodes": 0,
                "states": 0,
                "jobs": 0,
                "tasks": 0,
                "documents": 0,
                "snapshots": 0,
            },
        )

        recovered = self.loader.ingest(plan)
        self.assertEqual(recovered.outcome, "CREATED")
        self.assertEqual(recovered.corpus_revision, 1)
        replayed = self.loader.ingest(plan)
        self.assertEqual(replayed.outcome, "UNCHANGED")
        self.assertEqual(replayed.corpus_revision, 1)
        durable = self._records(
            """
            MATCH (job:IngestionJob:InitialLoadJob {job_id: $job_id})
            MATCH (job)-[:BUILDS]->(snapshot:KnowledgeSnapshot)
            OPTIONAL MATCH (job)-[:HAS_TASK]->(task:IngestionTask)
            RETURN job{.*} AS job,
                   snapshot.snapshot_id AS built_snapshot_id,
                   snapshot.manifest_hash AS snapshot_manifest_hash,
                   count(DISTINCT task) AS linked_task_count
            """,
            job_id=recovered.job_id,
        )
        self.assertEqual(len(durable), 1)
        job = dict(durable[0]["job"])
        self.assertEqual(
            {
                field: job[field]
                for field in (
                    "attempts",
                    "completed_tasks",
                    "corpus_revision",
                    "document_id",
                    "expected_active_snapshot_id",
                    "expected_tasks",
                    "idempotency_key",
                    "job_id",
                    "max_attempts",
                    "operation",
                    "operation_key",
                    "outcome",
                    "phase",
                    "request_fingerprint",
                    "source_generation",
                    "status",
                    "target_snapshot_id",
                    "target_version_id",
                    "tenant_id",
                )
            },
            {
                "attempts": 1,
                "completed_tasks": len(plan.bundles),
                "corpus_revision": 1,
                "document_id": plan.document_id,
                "expected_active_snapshot_id": "",
                "expected_tasks": len(plan.bundles),
                "idempotency_key": plan.operation_key,
                "job_id": recovered.job_id,
                "max_attempts": 1,
                "operation": "INITIAL_LOAD",
                "operation_key": plan.operation_key,
                "outcome": "CREATED",
                "phase": "COMPLETE",
                "request_fingerprint": plan.request_fingerprint,
                "source_generation": plan.source_generation,
                "status": "SUCCEEDED",
                "target_snapshot_id": plan.snapshot.snapshot_id,
                "target_version_id": plan.version_id,
                "tenant_id": plan.tenant_id,
            },
        )
        self.assertEqual(
            durable[0]["built_snapshot_id"], plan.snapshot.snapshot_id
        )
        self.assertEqual(
            durable[0]["snapshot_manifest_hash"], plan.snapshot.manifest_hash
        )
        self.assertEqual(int(durable[0]["linked_task_count"]), 0)

    def test_exact_replay_rejects_drift_without_advancing_revision(self) -> None:
        tenant_id = "tenant-bulk-replay-drift"
        plan = make_plan(tenant_id=tenant_id)
        self.loader.ingest(plan)
        self.driver.execute_query(
            """
            MATCH (chunk:Chunk {chunk_id: $chunk_id})
            SET chunk.text = 'tampered'
            """,
            chunk_id=plan.bundles[0].chunk.chunk_id,
            database_=self.database,
        )
        with self.assertRaisesRegex(IngestionConflict, "replayed Chunk"):
            self.loader.ingest(plan)
        state = self._records(
            """
            MATCH (state:TenantCorpusState {tenant_id: $tenant_id})
            RETURN state.corpus_revision AS revision
            """,
            tenant_id=tenant_id,
        )[0]
        self.assertEqual(int(state["revision"]), 1)

    def test_exact_replay_rejects_missing_retrieval_partition(self) -> None:
        plan = make_plan(tenant_id="tenant-bulk-replay-scope")
        self.loader.ingest(plan)
        self.driver.execute_query(
            """
            MATCH (chunk:Chunk {chunk_id: $chunk_id})
            REMOVE chunk.retrieval_scope
            """,
            chunk_id=plan.bundles[0].chunk.chunk_id,
            database_=self.database,
        )
        with self.assertRaisesRegex(IngestionConflict, "replayed Chunk"):
            self.loader.ingest(plan)

    def test_exact_replay_rejects_nonterminal_job_state(self) -> None:
        plan = make_plan(tenant_id="tenant-bulk-replay-job")
        result = self.loader.ingest(plan)
        self.driver.execute_query(
            """
            MATCH (job:IngestionJob {job_id: $job_id})
            SET job.status = 'RUNNING'
            """,
            job_id=result.job_id,
            database_=self.database,
        )
        with self.assertRaisesRegex(IngestionConflict, "not durably complete"):
            self.loader.ingest(plan)

    def test_loader_does_not_replace_managed_incremental_ingestion(self) -> None:
        tenant_id = "tenant-bulk-managed"
        plan = make_plan(tenant_id=tenant_id)
        Neo4jIngestionService(
            self.driver,
            self.database,
            worker_id="managed-before-bulk",
            clock=self.clock,
        ).ingest(plan)
        with self.assertRaisesRegex(IngestionConflict, "managed ingestion"):
            self.loader.ingest(plan)
        states = self._records(
            """
            MATCH (state:TenantCorpusState {tenant_id: $tenant_id})
            RETURN state.lifecycle_mode AS lifecycle_mode,
                   state.corpus_revision AS revision
            """,
            tenant_id=tenant_id,
        )
        self.assertEqual(states[0]["lifecycle_mode"], "MANAGED_INCREMENTAL")
        self.assertEqual(int(states[0]["revision"]), 1)


if __name__ == "__main__":
    unittest.main()
