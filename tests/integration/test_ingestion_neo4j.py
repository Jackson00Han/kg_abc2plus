"""Real-Neo4j lifecycle, recovery, deletion, and isolation tests."""

from __future__ import annotations

from collections import Counter
import dataclasses
from datetime import timedelta
import ipaddress
import os
import unittest
from urllib.parse import urlparse

import neo4j

from graphrag_prod.domain import pipeline_profile_id
from graphrag_prod.graph.provenance import Neo4jProvenanceStore
from graphrag_prod.graph.schema import apply_schema, verify_schema
from graphrag_prod.ingestion import (
    Checkpoint,
    IngestionConflict,
    IngestionInterrupted,
    IngestionPlan,
    JobLeaseConflict,
    JobPhase,
    JobStatus,
    Neo4jIngestionService,
)
from graphrag_prod.ingestion.models import default_artifact_input_hash
from tests.fixtures.ingestion import (
    CHUNKS_V2,
    FixedClock,
    make_plan,
    make_principal,
)


class InterruptOnce:
    """Raise exactly once at a committed workflow checkpoint."""

    def __init__(self, checkpoint: Checkpoint) -> None:
        self.checkpoint = checkpoint
        self.fired = False

    def __call__(self, checkpoint: Checkpoint, context: dict[str, object]) -> None:
        del context
        if checkpoint is self.checkpoint and not self.fired:
            self.fired = True
            raise IngestionInterrupted(f"injected interruption at {checkpoint.value}")


class DeleteDocumentAtCheckpoint:
    """Commit a competing delete during one post-commit ingestion callback."""

    def __init__(
        self,
        checkpoint: Checkpoint,
        delete_service: Neo4jIngestionService,
        *,
        tenant_id: str,
        document_id: str,
        expected_active_snapshot_id: str,
        source_generation: int,
    ) -> None:
        self.checkpoint = checkpoint
        self.delete_service = delete_service
        self.tenant_id = tenant_id
        self.document_id = document_id
        self.expected_active_snapshot_id = expected_active_snapshot_id
        self.source_generation = source_generation
        self.fired = False
        self.delete_result = None

    def __call__(self, checkpoint: Checkpoint, context: dict[str, object]) -> None:
        del context
        if checkpoint is self.checkpoint and not self.fired:
            self.fired = True
            self.delete_result = self.delete_service.delete_document(
                tenant_id=self.tenant_id,
                document_id=self.document_id,
                operation_key="delete-during-inflight-direct-ingest",
                expected_active_snapshot_id=self.expected_active_snapshot_id,
                source_generation=self.source_generation,
            )


class Neo4jIngestionIntegrationTests(unittest.TestCase):
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
            worker_id="stage3-test-worker",
            clock=self.clock,
        )
        self.provenance = Neo4jProvenanceStore(self.driver, self.database)

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
    def _with_document_metadata(
        plan: IngestionPlan,
        *,
        operation_key: str,
        title: str,
        access_policy_id: str,
        access_policy_version: int,
        access_groups: frozenset[str],
    ) -> IngestionPlan:
        bundles = tuple(
            dataclasses.replace(
                bundle,
                document=dataclasses.replace(
                    bundle.document,
                    title=title,
                    access_policy_id=access_policy_id,
                    access_policy_version=access_policy_version,
                    access_groups=access_groups,
                ),
                chunk=dataclasses.replace(
                    bundle.chunk,
                    access_policy_id=access_policy_id,
                    access_policy_version=access_policy_version,
                    access_groups=access_groups,
                ),
            )
            for bundle in plan.bundles
        )
        return IngestionPlan.build(
            operation_key=operation_key,
            profile=plan.profile,
            bundles=bundles,
            expected_active_snapshot_id=plan.snapshot.snapshot_id,
            source_generation=plan.source_generation,
            artifact_input_hashes={
                bundle.chunk.chunk_id: default_artifact_input_hash(bundle)
                for bundle in bundles
            },
            created_at=plan.snapshot.created_at,
            max_attempts=plan.max_attempts,
        )

    @staticmethod
    def _with_source_identity_metadata(
        plan: IngestionPlan,
        *,
        operation_key: str,
        document_changes: dict[str, object] | None = None,
        version_changes: dict[str, object] | None = None,
    ) -> IngestionPlan:
        """Rebuild the same snapshot ID with altered immutable source metadata."""
        document_changes = document_changes or {}
        version_changes = version_changes or {}
        bundles = tuple(
            dataclasses.replace(
                bundle,
                document=dataclasses.replace(bundle.document, **document_changes),
                version=dataclasses.replace(bundle.version, **version_changes),
            )
            for bundle in plan.bundles
        )
        return IngestionPlan.build(
            operation_key=operation_key,
            profile=plan.profile,
            bundles=bundles,
            expected_active_snapshot_id=plan.snapshot.snapshot_id,
            source_generation=plan.source_generation,
            artifact_input_hashes={
                bundle.chunk.chunk_id: default_artifact_input_hash(bundle)
                for bundle in bundles
            },
            created_at=plan.snapshot.created_at,
            max_attempts=plan.max_attempts,
        )

    def _node_counts(self) -> dict[str, int]:
        records = self._records(
            "MATCH (node) UNWIND labels(node) AS label "
            "RETURN label, count(node) AS count ORDER BY label"
        )
        return {record["label"]: record["count"] for record in records}

    def _business_shape(self) -> tuple[tuple[tuple[str, ...], str], ...]:
        records = self._records(
            """
            MATCH (node)
            WHERE NOT node:IngestionJob
            RETURN labels(node) AS labels,
                   coalesce(
                       node.document_id,
                       node.version_id,
                       node.chunk_id,
                       node.embedding_id,
                       node.entity_id,
                       node.mention_id,
                       node.assertion_id,
                       node.profile_id,
                       node.snapshot_id,
                       node.task_id,
                       node.artifact_id,
                       node.generation_id,
                       node.tenant_id
                   ) AS identifier
            ORDER BY labels, identifier
            """
        )
        return tuple((tuple(record["labels"]), record["identifier"]) for record in records)

    def _document_projection(self, tenant_id: str, document_id: str) -> dict | None:
        records = self._records(
            """
            MATCH (document:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            OPTIONAL MATCH (document)-[:HAS_VERSION]->(version:DocumentVersion)
            OPTIONAL MATCH (version)-[:HAS_CHUNK]->(chunk:Chunk)
            OPTIONAL MATCH (chunk)-[:HAS_EMBEDDING]->(embedding:ChunkEmbedding)
            OPTIONAL MATCH (chunk)<-[:IN_CHUNK]-(mention:EntityMention)
            OPTIONAL MATCH (chunk)<-[:EVIDENCED_BY]-(assertion:Assertion)
            OPTIONAL MATCH (document)-[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot)
            OPTIONAL MATCH (document)-[:ACTIVE_VERSION]->(active_version:DocumentVersion)
            RETURN document.document_id AS document_id,
                   collect(DISTINCT version.version_id) AS version_ids,
                   collect(DISTINCT chunk.chunk_id) AS chunk_ids,
                   collect(DISTINCT embedding.embedding_id) AS embedding_ids,
                   collect(DISTINCT mention.mention_id) AS mention_ids,
                   collect(DISTINCT assertion.assertion_id) AS assertion_ids,
                   collect(DISTINCT snapshot.snapshot_id) AS active_snapshot_ids,
                   collect(DISTINCT active_version.version_id) AS active_version_ids
            """,
            tenant_id=tenant_id,
            document_id=document_id,
        )
        if not records or records[0]["document_id"] is None:
            return None
        return {
            key: tuple(sorted(value for value in records[0][key] if value))
            for key in (
                "version_ids",
                "chunk_ids",
                "embedding_ids",
                "mention_ids",
                "assertion_ids",
                "active_snapshot_ids",
                "active_version_ids",
            )
        }

    def _assert_plan_active(self, plan) -> None:
        active = self._records(
            """
            MATCH (document:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })-[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot)
            MATCH (document)-[:ACTIVE_VERSION]->(version:DocumentVersion)
            RETURN document.generation AS generation,
                   document.access_policy_id AS access_policy_id,
                   document.access_policy_version AS access_policy_version,
                   document.access_groups AS access_groups,
                   snapshot.snapshot_id AS snapshot_id,
                   snapshot.version_id AS snapshot_version_id,
                   snapshot.profile_id AS profile_id,
                   snapshot.manifest_hash AS manifest_hash,
                   snapshot.expected_chunk_count AS expected_chunk_count,
                   snapshot.actual_chunk_count AS actual_chunk_count,
                   snapshot.build_state AS build_state,
                   version.version_id AS version_id
            """,
            tenant_id=plan.tenant_id,
            document_id=plan.document_id,
        )
        self.assertEqual(len(active), 1)
        record = active[0]
        self.assertEqual(record["generation"], plan.source_generation)
        self.assertEqual(
            record["access_policy_id"],
            plan.bundles[0].document.access_policy_id,
        )
        self.assertEqual(
            record["access_policy_version"],
            plan.bundles[0].document.access_policy_version,
        )
        self.assertEqual(
            set(record["access_groups"]),
            set(plan.bundles[0].document.access_groups),
        )
        self.assertEqual(record["snapshot_id"], plan.snapshot.snapshot_id)
        self.assertEqual(record["snapshot_version_id"], plan.version_id)
        self.assertEqual(record["profile_id"], plan.profile.profile_id)
        self.assertEqual(record["manifest_hash"], plan.snapshot.manifest_hash)
        self.assertEqual(record["expected_chunk_count"], len(plan.bundles))
        self.assertEqual(record["actual_chunk_count"], len(plan.bundles))
        self.assertEqual(record["build_state"], "PUBLISHED")
        self.assertEqual(record["version_id"], plan.version_id)

        expected_members = {
            "INCLUDES_CHUNK": {
                bundle.chunk.chunk_id for bundle in plan.bundles
            },
            "INCLUDES_ENTITY": {
                entity.entity_id
                for bundle in plan.bundles
                for entity in bundle.entities
            },
            "INCLUDES_MENTION": {
                mention.mention_id
                for bundle in plan.bundles
                for mention in bundle.mentions
            },
            "INCLUDES_ASSERTION": {
                assertion.assertion_id
                for bundle in plan.bundles
                for assertion in bundle.all_assertions
            },
        }
        member_shapes = {
            "INCLUDES_CHUNK": ("Chunk", "chunk_id"),
            "INCLUDES_ENTITY": ("Entity", "entity_id"),
            "INCLUDES_MENTION": ("EntityMention", "mention_id"),
            "INCLUDES_ASSERTION": ("Assertion", "assertion_id"),
        }
        for relationship, expected in expected_members.items():
            label, property_name = member_shapes[relationship]
            records = self._records(
                f"""
                MATCH (snapshot:KnowledgeSnapshot {{snapshot_id: $snapshot_id}})
                OPTIONAL MATCH (snapshot)-[:{relationship}]->(member:{label})
                RETURN collect(DISTINCT member.{property_name}) AS identifiers
                """,
                snapshot_id=plan.snapshot.snapshot_id,
            )
            self.assertEqual(
                {item for item in records[0]["identifiers"] if item},
                expected,
                relationship,
            )

        embeddings = self._records(
            """
            MATCH (snapshot:KnowledgeSnapshot {snapshot_id: $snapshot_id})
                  -[:INCLUDES_CHUNK]->(chunk:Chunk)
                  -[:HAS_EMBEDDING]->(embedding:ChunkEmbedding)
            RETURN collect(DISTINCT embedding.embedding_id) AS identifiers
            """,
            snapshot_id=plan.snapshot.snapshot_id,
        )
        self.assertEqual(
            set(embeddings[0]["identifiers"]),
            {
                embedding.embedding_id
                for bundle in plan.bundles
                for embedding in bundle.all_embeddings
            },
        )

        version_links = self._records(
            """
            MATCH (snapshot:KnowledgeSnapshot {snapshot_id: $snapshot_id})
                  -[:OF_VERSION]->(version:DocumentVersion)
            RETURN version.version_id AS version_id
            """,
            snapshot_id=plan.snapshot.snapshot_id,
        )
        self.assertEqual(
            [record["version_id"] for record in version_links],
            [plan.version_id],
        )

        chunk_access = self._records(
            """
            UNWIND $chunk_ids AS chunk_id
            MATCH (chunk:Chunk {chunk_id: chunk_id})
            RETURN chunk.chunk_id AS chunk_id,
                   chunk.access_policy_id AS policy_id,
                   chunk.access_policy_version AS policy_version,
                   chunk.access_groups AS groups
            ORDER BY chunk.chunk_id
            """,
            chunk_ids=sorted(expected_members["INCLUDES_CHUNK"]),
        )
        self.assertEqual(len(chunk_access), len(plan.bundles))
        for chunk in chunk_access:
            self.assertEqual(
                chunk["policy_id"],
                plan.bundles[0].document.access_policy_id,
            )
            self.assertEqual(
                chunk["policy_version"],
                plan.bundles[0].document.access_policy_version,
            )
            self.assertEqual(
                set(chunk["groups"]),
                set(plan.bundles[0].document.access_groups),
            )

        principal = make_principal(plan.tenant_id)
        for bundle in plan.bundles:
            for assertion in bundle.all_assertions:
                evidence = self.provenance.get_assertion_evidence(
                    principal,
                    assertion.assertion_id,
                )
                self.assertEqual(len(evidence), 1)
                self.assertEqual(evidence[0].chunk_id, bundle.chunk.chunk_id)
                self.assertEqual(evidence[0].text, bundle.chunk.text)
                self.assertEqual(evidence[0].version_id, plan.version_id)
                self.assertEqual(evidence[0].object_reference, assertion.literal_value)

    def _assert_plan_hidden(self, plan) -> None:
        principal = make_principal(plan.tenant_id)
        for bundle in plan.bundles:
            for assertion in bundle.all_assertions:
                self.assertEqual(
                    self.provenance.get_assertion_evidence(
                        principal,
                        assertion.assertion_id,
                    ),
                    (),
                )

    def test_create_publishes_exact_snapshot_and_evidence(self) -> None:
        plan = make_plan()
        self.assertEqual(len(self.service.pending_artifact_ids(plan)), 6)

        result = self.service.ingest(plan)

        self.assertEqual(result.snapshot_id, plan.snapshot.snapshot_id)
        self.assertEqual(result.active_snapshot_id, plan.snapshot.snapshot_id)
        self.assertEqual(result.job.status, JobStatus.SUCCEEDED)
        self.assertEqual(result.job.phase, JobPhase.COMPLETE)
        self.assertEqual(result.job.outcome, "CREATED")
        self.assertEqual(result.job.attempts, 1)
        self.assertEqual(result.job.completed_tasks, 3)
        self.assertEqual(result.job.expected_tasks, 3)
        self.assertEqual(self.service.pending_artifact_ids(plan), ())
        self._assert_plan_active(plan)

        counts = self._node_counts()
        self.assertEqual(counts["Document"], 1)
        self.assertEqual(counts["DocumentVersion"], 1)
        self.assertEqual(counts["KnowledgeSnapshot"], 1)
        self.assertEqual(counts["Chunk"], 3)
        self.assertEqual(counts["ChunkEmbedding"], 3)
        self.assertEqual(counts["Entity"], 1)
        self.assertEqual(counts["EntityMention"], 3)
        self.assertEqual(counts["Assertion"], 3)
        self.assertEqual(counts["IngestionTask"], 3)
        self.assertEqual(counts["DerivationArtifact"], 6)

    def test_idempotency_conflict_and_unchanged_noop(self) -> None:
        plan = make_plan()
        created = self.service.ingest(plan)
        baseline = self._business_shape()

        retried = self.service.ingest(plan)
        self.assertEqual(retried.job.job_id, created.job.job_id)
        self.assertEqual(retried.job.attempts, 1)
        self.assertEqual(retried.job.outcome, "CREATED")
        self.assertEqual(self._business_shape(), baseline)

        conflicting = make_plan(
            operation_key=plan.operation_key,
            chunk_specs=CHUNKS_V2,
            version_number=2,
            expected_active_snapshot_id=plan.snapshot.snapshot_id,
        )
        with self.assertRaisesRegex(IngestionConflict, "idempotency key"):
            self.service.ingest(conflicting)
        self._assert_plan_active(plan)

        unchanged = make_plan(
            operation_key="same-snapshot-new-key",
            expected_active_snapshot_id=plan.snapshot.snapshot_id,
        )
        noop = self.service.ingest(unchanged)
        self.assertEqual(noop.job.status, JobStatus.NOOP)
        self.assertEqual(noop.job.outcome, "UNCHANGED")
        self.assertEqual(noop.active_snapshot_id, plan.snapshot.snapshot_id)
        self.assertEqual(self._business_shape(), baseline)
        self._assert_plan_active(plan)

    def test_update_is_hidden_until_after_chunk_interruption_recovers(self) -> None:
        v1 = make_plan()
        self.service.ingest(v1)
        v2 = make_plan(
            operation_key="upsert-apple-v2",
            chunk_specs=CHUNKS_V2,
            version_number=2,
            expected_active_snapshot_id=v1.snapshot.snapshot_id,
        )
        self.assertEqual(len(self.service.pending_artifact_ids(v2)), 2)

        interrupted_service = Neo4jIngestionService(
            self.driver,
            self.database,
            worker_id="interrupted-worker",
            clock=self.clock,
            failpoint=InterruptOnce(Checkpoint.AFTER_CHUNK_STAGE),
        )
        with self.assertRaisesRegex(IngestionInterrupted, "AFTER_CHUNK_STAGE"):
            interrupted_service.ingest(v2)

        interrupted_job = interrupted_service.get_job(v2.job_id)
        self.assertEqual(interrupted_job.status, JobStatus.RETRY_WAIT)
        self.assertEqual(interrupted_job.phase, JobPhase.STAGE)
        self.assertEqual(interrupted_job.attempts, 1)
        self.assertEqual(interrupted_job.completed_tasks, 1)
        self._assert_plan_active(v1)
        self._assert_plan_hidden(v2)
        self.assertEqual(len(self.service.pending_artifact_ids(v2)), 2)
        building = self._records(
            "MATCH (snapshot:KnowledgeSnapshot {snapshot_id: $snapshot_id}) "
            "RETURN snapshot.build_state AS state",
            snapshot_id=v2.snapshot.snapshot_id,
        )
        self.assertEqual(building[0]["state"], "BUILDING")

        resumed_service = Neo4jIngestionService(
            self.driver,
            self.database,
            worker_id="recovery-worker",
            clock=self.clock,
        )
        updated = resumed_service.ingest(v2)

        self.assertEqual(updated.job.status, JobStatus.SUCCEEDED)
        self.assertEqual(updated.job.outcome, "UPDATED")
        self.assertEqual(updated.job.attempts, 2)
        self.assertEqual(updated.job.completed_tasks, 3)
        self.assertEqual(resumed_service.pending_artifact_ids(v2), ())
        self._assert_plan_active(v2)
        self._assert_plan_hidden(v1)
        states = self._records(
            """
            UNWIND $snapshot_ids AS snapshot_id
            MATCH (snapshot:KnowledgeSnapshot {snapshot_id: snapshot_id})
            RETURN snapshot.snapshot_id AS snapshot_id,
                   snapshot.build_state AS state
            """,
            snapshot_ids=[v1.snapshot.snapshot_id, v2.snapshot.snapshot_id],
        )
        self.assertEqual(
            {record["snapshot_id"]: record["state"] for record in states},
            {
                v1.snapshot.snapshot_id: "RETIRED",
                v2.snapshot.snapshot_id: "PUBLISHED",
            },
        )
        self.assertEqual(self._node_counts()["DerivationArtifact"], 8)

    def test_delete_fences_inflight_direct_update_without_recreating_residue(self) -> None:
        v1 = make_plan(operation_key="delete-race-direct-v1")
        self.service.ingest(v1)
        v2 = make_plan(
            operation_key="delete-race-direct-v2",
            chunk_specs=CHUNKS_V2,
            version_number=2,
            expected_active_snapshot_id=v1.snapshot.snapshot_id,
        )
        delete_service = Neo4jIngestionService(
            self.driver,
            self.database,
            worker_id="delete-race-direct-deleter",
            clock=self.clock,
        )
        delete_at_first_chunk = DeleteDocumentAtCheckpoint(
            Checkpoint.AFTER_CHUNK_STAGE,
            delete_service,
            tenant_id=v1.tenant_id,
            document_id=v1.document_id,
            expected_active_snapshot_id=v1.snapshot.snapshot_id,
            source_generation=v1.source_generation,
        )
        update_service = Neo4jIngestionService(
            self.driver,
            self.database,
            worker_id="delete-race-direct-updater",
            clock=self.clock,
            failpoint=delete_at_first_chunk,
        )

        with self.assertRaisesRegex(IngestionConflict, "generation"):
            update_service.ingest(v2)
        self.assertTrue(delete_at_first_chunk.fired)
        self.assertIsNotNone(delete_at_first_chunk.delete_result)
        self.assertEqual(
            delete_at_first_chunk.delete_result.job.status,
            JobStatus.SUCCEEDED,
        )
        self.assertEqual(
            delete_at_first_chunk.delete_result.job.outcome,
            "DELETED",
        )
        self.assertEqual(
            update_service.get_job(v2.job_id).status,
            JobStatus.FAILED_PERMANENT,
        )

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
            tenant_id=v1.tenant_id,
            document_id=v1.document_id,
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
        tombstone = self._records(
            """
            MATCH (tombstone:DocumentTombstone {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            RETURN tombstone.generation AS generation,
                   tombstone.deleted_by_job_id AS deleted_by_job_id
            """,
            tenant_id=v1.tenant_id,
            document_id=v1.document_id,
        )
        self.assertEqual(len(tombstone), 1)
        self.assertEqual(tombstone[0]["generation"], 1)
        audit_operations = self._records(
            """
            MATCH (job:IngestionJob {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            RETURN collect(job.operation) AS operations
            """,
            tenant_id=v1.tenant_id,
            document_id=v1.document_id,
        )[0]["operations"]
        self.assertEqual(
            Counter(audit_operations),
            Counter({"UPSERT": 2, "DELETE": 1}),
        )

    def test_delete_cleans_snapshot_only_residue_after_retry_exhaustion(self) -> None:
        plan = make_plan(operation_key="delete-snapshot-only-residue")

        for attempt in range(plan.max_attempts):
            interrupted_service = Neo4jIngestionService(
                self.driver,
                self.database,
                worker_id=f"snapshot-only-failure-{attempt}",
                clock=self.clock,
                failpoint=InterruptOnce(Checkpoint.AFTER_SNAPSHOT_STAGE),
            )
            with self.assertRaisesRegex(IngestionInterrupted, "AFTER_SNAPSHOT_STAGE"):
                interrupted_service.ingest(plan)

        failed_job = self.service.get_job(plan.job_id)
        self.assertEqual(failed_job.status, JobStatus.FAILED_PERMANENT)
        residue_before = self._records(
            """
            OPTIONAL MATCH (document:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            WITH count(document) AS documents
            OPTIONAL MATCH (snapshot:KnowledgeSnapshot {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            RETURN documents, count(snapshot) AS snapshots
            """,
            tenant_id=plan.tenant_id,
            document_id=plan.document_id,
        )[0]
        self.assertEqual(residue_before["documents"], 0)
        self.assertEqual(residue_before["snapshots"], 1)

        deleted = self.service.delete_document(
            tenant_id=plan.tenant_id,
            document_id=plan.document_id,
            operation_key="delete-snapshot-only-residue-cleanup",
            expected_active_snapshot_id=None,
            source_generation=0,
        )

        self.assertEqual(deleted.job.status, JobStatus.SUCCEEDED)
        self.assertEqual(deleted.job.outcome, "DELETED")
        residue_after = self._records(
            """
            OPTIONAL MATCH (snapshot:KnowledgeSnapshot {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            WITH count(snapshot) AS snapshots
            OPTIONAL MATCH (job:IngestionJob {
                tenant_id: $tenant_id,
                document_id: $document_id
            })-[:HAS_TASK]->(task:IngestionTask)
            WITH snapshots, count(DISTINCT task) AS tasks
            OPTIONAL MATCH (tombstone:DocumentTombstone {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            RETURN snapshots, tasks, tombstone.generation AS generation
            """,
            tenant_id=plan.tenant_id,
            document_id=plan.document_id,
        )[0]
        self.assertEqual(residue_after["snapshots"], 0)
        self.assertEqual(residue_after["tasks"], 0)
        self.assertEqual(residue_after["generation"], 1)

    def test_after_publish_lost_response_recovers_as_terminal_success(self) -> None:
        plan = make_plan(operation_key="publish-response-loss")
        interrupted_service = Neo4jIngestionService(
            self.driver,
            self.database,
            worker_id="publish-worker",
            clock=self.clock,
            failpoint=InterruptOnce(Checkpoint.AFTER_PUBLISH),
        )
        with self.assertRaisesRegex(IngestionInterrupted, "AFTER_PUBLISH"):
            interrupted_service.ingest(plan)

        committed_job = interrupted_service.get_job(plan.job_id)
        self.assertEqual(committed_job.status, JobStatus.SUCCEEDED)
        self.assertEqual(committed_job.outcome, "CREATED")
        self.assertEqual(committed_job.attempts, 1)
        self._assert_plan_active(plan)
        baseline = self._business_shape()

        recovered = self.service.ingest(plan)
        self.assertEqual(recovered.job.status, JobStatus.SUCCEEDED)
        self.assertEqual(recovered.job.attempts, 1)
        self.assertEqual(recovered.active_snapshot_id, plan.snapshot.snapshot_id)
        self.assertEqual(self._business_shape(), baseline)

    def test_same_snapshot_metadata_update_is_atomic_and_policy_cas_is_strict(
        self,
    ) -> None:
        original = make_plan(operation_key="metadata-policy-original")
        self.service.ingest(original)
        revision_before = self._records(
            """
            MATCH (state:TenantCorpusState {tenant_id: $tenant_id})
            RETURN state.corpus_revision AS revision
            """,
            tenant_id=original.tenant_id,
        )[0]["revision"]

        updated = self._with_document_metadata(
            original,
            operation_key="metadata-policy-version-2",
            title="Apple fixture governed title",
            access_policy_id=f"{original.tenant_id}:legal-readers",
            access_policy_version=2,
            access_groups=frozenset({"legal-readers", "audit-readers"}),
        )
        result = self.service.ingest(updated)

        self.assertEqual(result.job.status, JobStatus.SUCCEEDED)
        self.assertEqual(result.job.outcome, "METADATA_UPDATED")
        self.assertEqual(result.snapshot_id, original.snapshot.snapshot_id)
        self.assertEqual(result.active_snapshot_id, original.snapshot.snapshot_id)
        projection = self._records(
            """
            MATCH (document:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })-[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot)
            MATCH (snapshot)-[:INCLUDES_CHUNK]->(chunk:Chunk)
            RETURN document.title AS title,
                   document.access_policy_id AS document_policy_id,
                   document.access_policy_version AS document_policy_version,
                   document.access_groups AS document_groups,
                   collect(DISTINCT chunk.access_policy_id) AS chunk_policy_ids,
                   collect(DISTINCT chunk.access_policy_version) AS chunk_policy_versions,
                   collect(DISTINCT chunk.access_groups) AS chunk_group_sets,
                   count(DISTINCT chunk) AS chunk_count
            """,
            tenant_id=updated.tenant_id,
            document_id=updated.document_id,
        )[0]
        self.assertEqual(projection["title"], updated.bundles[0].document.title)
        self.assertEqual(
            projection["document_policy_id"],
            updated.bundles[0].document.access_policy_id,
        )
        self.assertEqual(projection["document_policy_version"], 2)
        self.assertEqual(
            set(projection["document_groups"]),
            set(updated.bundles[0].document.access_groups),
        )
        self.assertEqual(
            projection["chunk_policy_ids"],
            [updated.bundles[0].document.access_policy_id],
        )
        self.assertEqual(projection["chunk_policy_versions"], [2])
        self.assertEqual(projection["chunk_count"], len(updated.bundles))
        self.assertEqual(len(projection["chunk_group_sets"]), 1)
        self.assertEqual(
            set(projection["chunk_group_sets"][0]),
            set(updated.bundles[0].document.access_groups),
        )
        revision_after = self._records(
            """
            MATCH (state:TenantCorpusState {tenant_id: $tenant_id})
            RETURN state.corpus_revision AS revision
            """,
            tenant_id=updated.tenant_id,
        )[0]["revision"]
        self.assertEqual(revision_after, revision_before + 1)

        same_version_different_policy = self._with_document_metadata(
            updated,
            operation_key="metadata-policy-version-reuse-conflict",
            title="This title must not leak through a rejected update",
            access_policy_id=f"{updated.tenant_id}:different-policy",
            access_policy_version=2,
            access_groups=frozenset({"different-readers"}),
        )
        with self.assertRaisesRegex(
            IngestionConflict,
            "access policy changed without a new version",
        ):
            self.service.ingest(same_version_different_policy)
        rejected_job = self.service.get_job(same_version_different_policy.job_id)
        self.assertEqual(rejected_job.status, JobStatus.FAILED_PERMANENT)

        unchanged = self._records(
            """
            MATCH (document:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })-[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot)
            MATCH (snapshot)-[:INCLUDES_CHUNK]->(chunk:Chunk)
            RETURN document.title AS title,
                   document.access_policy_id AS document_policy_id,
                   document.access_policy_version AS document_policy_version,
                   document.access_groups AS document_groups,
                   collect(DISTINCT chunk.access_policy_id) AS chunk_policy_ids,
                   collect(DISTINCT chunk.access_policy_version) AS chunk_policy_versions,
                   collect(DISTINCT chunk.access_groups) AS chunk_group_sets
            """,
            tenant_id=updated.tenant_id,
            document_id=updated.document_id,
        )[0]
        self.assertEqual(unchanged["title"], updated.bundles[0].document.title)
        self.assertEqual(
            unchanged["document_policy_id"],
            updated.bundles[0].document.access_policy_id,
        )
        self.assertEqual(unchanged["document_policy_version"], 2)
        self.assertEqual(
            set(unchanged["document_groups"]),
            set(updated.bundles[0].document.access_groups),
        )
        self.assertEqual(
            unchanged["chunk_policy_ids"],
            [updated.bundles[0].document.access_policy_id],
        )
        self.assertEqual(unchanged["chunk_policy_versions"], [2])
        self.assertEqual(len(unchanged["chunk_group_sets"]), 1)
        self.assertEqual(
            set(unchanged["chunk_group_sets"][0]),
            set(updated.bundles[0].document.access_groups),
        )

    def test_same_snapshot_fast_path_rejects_changed_source_identity_metadata(
        self,
    ) -> None:
        original = make_plan(operation_key="source-identity-original")
        self.service.ingest(original)

        def projection() -> dict[str, object]:
            record = self._records(
                """
                MATCH (document:Document {
                    tenant_id: $tenant_id,
                    document_id: $document_id
                })-[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot)
                MATCH (document)-[:ACTIVE_VERSION]->(version:DocumentVersion)
                MATCH (snapshot)-[:INCLUDES_CHUNK]->(chunk:Chunk)
                RETURN document.source_name AS source_name,
                       version.version_id AS version_id,
                       version.version_number AS version_number,
                       version.mime_type AS mime_type,
                       version.language AS language,
                       version.published_at AS published_at,
                       snapshot.snapshot_id AS snapshot_id,
                       count(DISTINCT chunk) AS chunk_count
                """,
                tenant_id=original.tenant_id,
                document_id=original.document_id,
            )
            self.assertEqual(len(record), 1)
            return dict(record[0])

        baseline = projection()
        document = original.bundles[0].document
        version = original.bundles[0].version
        self.assertIsNotNone(version.published_at)
        mutations = (
            ("source-name", {"source_name": f"{document.source_name}-changed"}, {}),
            ("version-number", {}, {"version_number": version.version_number + 1}),
            ("mime-type", {}, {"mime_type": "application/pdf"}),
            ("language", {}, {"language": "zh"}),
            (
                "published-at",
                {},
                {"published_at": version.published_at + timedelta(hours=1)},
            ),
        )
        for label, document_changes, version_changes in mutations:
            with self.subTest(label=label):
                conflicting = self._with_source_identity_metadata(
                    original,
                    operation_key=f"source-identity-conflict-{label}",
                    document_changes=document_changes,
                    version_changes=version_changes,
                )
                with self.assertRaises(IngestionConflict):
                    self.service.ingest(conflicting)
                self.assertEqual(
                    self.service.get_job(conflicting.job_id).status,
                    JobStatus.FAILED_PERMANENT,
                )
                self.assertEqual(projection(), baseline)

    def test_same_snapshot_mutation_requires_expected_pointer_but_replay_does_not(
        self,
    ) -> None:
        original = make_plan(operation_key="same-snapshot-cas-original")
        self.service.ingest(original)

        exact_none = make_plan(
            operation_key="same-snapshot-exact-replay-none",
            expected_active_snapshot_id=None,
        )
        none_replay_result = self.service.ingest(exact_none)
        self.assertEqual(none_replay_result.job.status, JobStatus.NOOP)
        self.assertEqual(none_replay_result.job.outcome, "UNCHANGED")

        stale_pointer = f"{original.snapshot.snapshot_id}-stale"
        exact_stale = make_plan(
            operation_key="same-snapshot-exact-replay-stale",
            expected_active_snapshot_id=stale_pointer,
        )
        stale_replay_result = self.service.ingest(exact_stale)
        self.assertEqual(stale_replay_result.job.status, JobStatus.NOOP)
        self.assertEqual(stale_replay_result.job.outcome, "UNCHANGED")

        desired = self._with_document_metadata(
            original,
            operation_key="same-snapshot-metadata-missing-cas",
            title="Mutation must require active snapshot CAS",
            access_policy_id=f"{original.tenant_id}:governed-readers",
            access_policy_version=2,
            access_groups=frozenset({"governed-readers"}),
        )
        missing_cas = dataclasses.replace(
            desired,
            expected_active_snapshot_id=None,
        )
        with self.assertRaisesRegex(IngestionConflict, "CAS"):
            self.service.ingest(missing_cas)
        self.assertEqual(
            self.service.get_job(missing_cas.job_id).status,
            JobStatus.FAILED_PERMANENT,
        )

        stale_cas = dataclasses.replace(
            desired,
            operation_key="same-snapshot-metadata-stale-cas",
            expected_active_snapshot_id=stale_pointer,
        )
        with self.assertRaisesRegex(IngestionConflict, "CAS"):
            self.service.ingest(stale_cas)
        self.assertEqual(
            self.service.get_job(stale_cas.job_id).status,
            JobStatus.FAILED_PERMANENT,
        )

        state = self._records(
            """
            MATCH (document:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })-[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot)
            MATCH (snapshot)-[:INCLUDES_CHUNK]->(chunk:Chunk)
            RETURN document.title AS title,
                   document.access_policy_id AS policy_id,
                   document.access_policy_version AS policy_version,
                   document.access_groups AS groups,
                   collect(DISTINCT chunk.access_policy_id) AS chunk_policy_ids,
                   collect(DISTINCT chunk.access_policy_version) AS chunk_policy_versions,
                   collect(DISTINCT chunk.access_groups) AS chunk_group_sets
            """,
            tenant_id=original.tenant_id,
            document_id=original.document_id,
        )[0]
        self.assertEqual(state["title"], original.bundles[0].document.title)
        self.assertEqual(
            state["policy_id"],
            original.bundles[0].document.access_policy_id,
        )
        self.assertEqual(state["policy_version"], 1)
        self.assertEqual(
            set(state["groups"]),
            set(original.bundles[0].document.access_groups),
        )
        self.assertEqual(
            state["chunk_policy_ids"],
            [original.bundles[0].document.access_policy_id],
        )
        self.assertEqual(state["chunk_policy_versions"], [1])
        self.assertEqual(len(state["chunk_group_sets"]), 1)
        self.assertEqual(
            set(state["chunk_group_sets"][0]),
            set(original.bundles[0].document.access_groups),
        )

    def test_expired_lease_takeover_fences_stale_failure_and_finish(self) -> None:
        plan = make_plan(operation_key="lease-fencing-takeover")
        worker_a = Neo4jIngestionService(
            self.driver,
            self.database,
            worker_id="lease-worker-a",
            clock=self.clock,
            lease_seconds=5,
        )
        worker_b = Neo4jIngestionService(
            self.driver,
            self.database,
            worker_id="lease-worker-b",
            clock=self.clock,
            lease_seconds=5,
        )
        with self.driver.session(database=self.database) as session:
            session.execute_write(worker_a._ensure_upsert_job_tx, plan, self.clock.now())
            claimed_a = session.execute_write(
                worker_a._claim_job_tx,
                plan.job_id,
                self.clock.now(),
            )
        self.assertEqual(claimed_a.status, JobStatus.RUNNING)
        self.assertIsNotNone(claimed_a.lease_token)

        self.clock.advance(seconds=6)
        with self.driver.session(database=self.database) as session:
            claimed_b = session.execute_write(
                worker_b._claim_job_tx,
                plan.job_id,
                self.clock.now(),
            )
        self.assertEqual(claimed_b.status, JobStatus.RUNNING)
        self.assertEqual(claimed_b.lease_owner, "lease-worker-b")
        self.assertIsNotNone(claimed_b.lease_token)
        self.assertNotEqual(claimed_b.lease_token, claimed_a.lease_token)
        self.assertEqual(claimed_b.attempts, 2)

        worker_a._record_failure(
            plan.job_id,
            claimed_a.lease_token,
            RuntimeError("late failure from stale worker"),
            retryable=True,
        )
        after_stale_failure = worker_b.get_job(plan.job_id)
        self.assertEqual(after_stale_failure.status, JobStatus.RUNNING)
        self.assertEqual(after_stale_failure.lease_owner, "lease-worker-b")
        self.assertEqual(after_stale_failure.lease_token, claimed_b.lease_token)

        with self.driver.session(database=self.database) as session:
            with self.assertRaisesRegex(JobLeaseConflict, "stale worker"):
                session.execute_write(
                    worker_a._finish_job_tx,
                    plan.job_id,
                    JobStatus.SUCCEEDED,
                    "STALE_WORKER_MUST_NOT_FINISH",
                    claimed_a.lease_token,
                    self.clock.now(),
                )
        after_stale_finish = worker_b.get_job(plan.job_id)
        self.assertEqual(after_stale_finish.status, JobStatus.RUNNING)
        self.assertEqual(after_stale_finish.lease_owner, "lease-worker-b")
        self.assertEqual(after_stale_finish.lease_token, claimed_b.lease_token)
        self.assertEqual(after_stale_finish.attempts, 2)

    def test_active_snapshot_membership_governs_shared_assertion_acceptance(
        self,
    ) -> None:
        profile_v1 = make_plan(operation_key="acceptance-profile-seed").profile
        prompt_v2 = "literal-metrics-prompt:sha256:acceptance-v2"
        code_v2 = "git:stage3-acceptance-v2"
        profile_v2 = dataclasses.replace(
            profile_v1,
            profile_id=pipeline_profile_id(
                profile_v1.normalizer_signature,
                profile_v1.splitter_signature,
                profile_v1.extractor_signature,
                prompt_v2,
                profile_v1.schema_signature,
                code_v2,
            ),
            prompt_signature=prompt_v2,
            code_signature=code_v2,
        )
        self.assertEqual(
            profile_v1.extractor_signature,
            profile_v2.extractor_signature,
        )
        self.assertEqual(profile_v1.schema_signature, profile_v2.schema_signature)
        self.assertNotEqual(profile_v1.profile_id, profile_v2.profile_id)

        # The reverse fixture carries different derived payloads for the same
        # text, so isolate it in its own two immutable artifact profiles. This
        # keeps this test focused on snapshot-scoped acceptance projection.
        prompt_v3 = "literal-metrics-prompt:sha256:acceptance-v3"
        code_v3 = "git:stage3-acceptance-v3"
        profile_v3 = dataclasses.replace(
            profile_v1,
            profile_id=pipeline_profile_id(
                profile_v1.normalizer_signature,
                profile_v1.splitter_signature,
                profile_v1.extractor_signature,
                prompt_v3,
                profile_v1.schema_signature,
                code_v3,
            ),
            prompt_signature=prompt_v3,
            code_signature=code_v3,
        )
        prompt_v4 = "literal-metrics-prompt:sha256:acceptance-v4"
        code_v4 = "git:stage3-acceptance-v4"
        profile_v4 = dataclasses.replace(
            profile_v1,
            profile_id=pipeline_profile_id(
                profile_v1.normalizer_signature,
                profile_v1.splitter_signature,
                profile_v1.extractor_signature,
                prompt_v4,
                profile_v1.schema_signature,
                code_v4,
            ),
            prompt_signature=prompt_v4,
            code_signature=code_v4,
        )
        self.assertEqual(
            len(
                {
                    profile_v1.profile_id,
                    profile_v2.profile_id,
                    profile_v3.profile_id,
                    profile_v4.profile_id,
                }
            ),
            4,
        )

        def with_profile_and_acceptance(
            source: IngestionPlan,
            *,
            operation_key: str,
            profile,
            accepted: bool,
            expected_active_snapshot_id: str | None,
        ) -> IngestionPlan:
            bundles = tuple(
                dataclasses.replace(
                    bundle,
                    assertion=(
                        None
                        if bundle.assertion is None
                        else dataclasses.replace(bundle.assertion, accepted=accepted)
                    ),
                    additional_assertions=tuple(
                        dataclasses.replace(assertion, accepted=accepted)
                        for assertion in bundle.additional_assertions
                    ),
                )
                for bundle in source.bundles
            )
            return IngestionPlan.build(
                operation_key=operation_key,
                profile=profile,
                bundles=bundles,
                expected_active_snapshot_id=expected_active_snapshot_id,
                source_generation=source.source_generation,
                artifact_input_hashes={
                    bundle.chunk.chunk_id: default_artifact_input_hash(bundle)
                    for bundle in bundles
                },
                created_at=source.snapshot.created_at,
                max_attempts=source.max_attempts,
            )

        def acceptance_projection(plan: IngestionPlan, assertion_id: str) -> dict:
            records = self._records(
                """
                MATCH (document:Document {
                    tenant_id: $tenant_id,
                    document_id: $document_id
                })-[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot)
                MATCH (snapshot)-[
                    membership:INCLUDES_ASSERTION
                ]->(assertion:Assertion {assertion_id: $assertion_id})
                RETURN snapshot.snapshot_id AS snapshot_id,
                       membership.accepted AS membership_accepted,
                       assertion.accepted AS shared_node_accepted
                """,
                tenant_id=plan.tenant_id,
                document_id=plan.document_id,
                assertion_id=assertion_id,
            )
            self.assertEqual(len(records), 1)
            return dict(records[0])

        principal = make_principal()

        true_source = make_plan(operation_key="acceptance-true-to-false-v1")
        true_v1 = with_profile_and_acceptance(
            true_source,
            operation_key="acceptance-true-to-false-v1-profile",
            profile=profile_v1,
            accepted=True,
            expected_active_snapshot_id=None,
        )
        self.service.ingest(true_v1)
        true_assertion_id = true_v1.bundles[0].all_assertions[0].assertion_id
        self.assertEqual(
            len(self.provenance.get_assertion_evidence(principal, true_assertion_id)),
            1,
        )
        false_v2 = with_profile_and_acceptance(
            true_source,
            operation_key="acceptance-true-to-false-v2-profile",
            profile=profile_v2,
            accepted=False,
            expected_active_snapshot_id=true_v1.snapshot.snapshot_id,
        )
        self.service.ingest(false_v2)
        self.assertEqual(true_v1.version_id, false_v2.version_id)
        self.assertEqual(
            true_assertion_id,
            false_v2.bundles[0].all_assertions[0].assertion_id,
        )
        false_projection = acceptance_projection(false_v2, true_assertion_id)
        self.assertEqual(false_projection["snapshot_id"], false_v2.snapshot.snapshot_id)
        self.assertFalse(false_projection["membership_accepted"])
        self.assertTrue(false_projection["shared_node_accepted"])
        self.assertEqual(
            self.provenance.get_assertion_evidence(principal, true_assertion_id),
            (),
        )

        false_source = make_plan(
            operation_key="acceptance-false-to-true-v1",
            canonical_uri="https://example.com/knowledge/apple-acceptance-reverse",
        )
        false_v1 = with_profile_and_acceptance(
            false_source,
            operation_key="acceptance-false-to-true-v1-profile",
            profile=profile_v3,
            accepted=False,
            expected_active_snapshot_id=None,
        )
        self.service.ingest(false_v1)
        false_assertion_id = false_v1.bundles[0].all_assertions[0].assertion_id
        self.assertEqual(
            self.provenance.get_assertion_evidence(principal, false_assertion_id),
            (),
        )
        true_v2 = with_profile_and_acceptance(
            false_source,
            operation_key="acceptance-false-to-true-v2-profile",
            profile=profile_v4,
            accepted=True,
            expected_active_snapshot_id=false_v1.snapshot.snapshot_id,
        )
        self.service.ingest(true_v2)
        self.assertEqual(false_v1.version_id, true_v2.version_id)
        self.assertEqual(
            false_assertion_id,
            true_v2.bundles[0].all_assertions[0].assertion_id,
        )
        true_projection = acceptance_projection(true_v2, false_assertion_id)
        self.assertEqual(true_projection["snapshot_id"], true_v2.snapshot.snapshot_id)
        self.assertTrue(true_projection["membership_accepted"])
        self.assertFalse(true_projection["shared_node_accepted"])
        evidence = self.provenance.get_assertion_evidence(
            principal,
            false_assertion_id,
        )
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].assertion_id, false_assertion_id)

    def test_delete_is_repeatable_and_tombstone_blocks_stale_resurrection(self) -> None:
        plan = make_plan(operation_key="delete-target-create")
        self.service.ingest(plan)

        deleted = self.service.delete_document(
            tenant_id=plan.tenant_id,
            document_id=plan.document_id,
            operation_key="delete-target",
            expected_active_snapshot_id=plan.snapshot.snapshot_id,
            source_generation=0,
        )
        self.assertEqual(deleted.job.status, JobStatus.SUCCEEDED)
        self.assertEqual(deleted.job.outcome, "DELETED")
        self.assertIsNone(deleted.active_snapshot_id)
        self.assertIsNone(
            self._document_projection(plan.tenant_id, plan.document_id)
        )
        self._assert_plan_hidden(plan)
        tombstone = self._records(
            """
            MATCH (tombstone:DocumentTombstone {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            RETURN tombstone.generation AS generation,
                   tombstone.deleted_by_job_id AS deleted_by_job_id
            """,
            tenant_id=plan.tenant_id,
            document_id=plan.document_id,
        )
        self.assertEqual(len(tombstone), 1)
        self.assertEqual(tombstone[0]["generation"], 1)
        self.assertEqual(tombstone[0]["deleted_by_job_id"], deleted.job.job_id)

        repeated = self.service.delete_document(
            tenant_id=plan.tenant_id,
            document_id=plan.document_id,
            operation_key="delete-target",
            expected_active_snapshot_id=plan.snapshot.snapshot_id,
            source_generation=0,
        )
        self.assertEqual(repeated.job.job_id, deleted.job.job_id)
        self.assertEqual(repeated.job.attempts, 1)
        self.assertEqual(repeated.job.outcome, "DELETED")

        already_absent = self.service.delete_document(
            tenant_id=plan.tenant_id,
            document_id=plan.document_id,
            operation_key="delete-target-again",
            expected_active_snapshot_id=None,
            source_generation=1,
        )
        self.assertEqual(already_absent.job.status, JobStatus.NOOP)
        self.assertEqual(already_absent.job.outcome, "ALREADY_ABSENT")

        stale_plan = make_plan(
            operation_key="stale-plan-after-delete",
            source_generation=0,
        )
        with self.assertRaisesRegex(IngestionConflict, "cannot resurrect"):
            self.service.ingest(stale_plan)
        self.assertEqual(
            self.service.get_job(stale_plan.job_id).status,
            JobStatus.FAILED_PERMANENT,
        )
        self.assertIsNone(
            self._document_projection(plan.tenant_id, plan.document_id)
        )

    def test_delete_preserves_other_documents_tenants_and_shared_entity(self) -> None:
        target = make_plan(operation_key="target-a")
        same_tenant = make_plan(
            operation_key="other-document",
            canonical_uri="https://example.com/knowledge/apple-other",
        )
        other_tenant = make_plan(
            operation_key="other-tenant-document",
            tenant_id="tenant-stage3-other",
        )
        self.service.ingest(target)
        self.service.ingest(same_tenant)
        self.service.ingest(other_tenant)

        same_tenant_before = self._document_projection(
            same_tenant.tenant_id,
            same_tenant.document_id,
        )
        other_tenant_before = self._document_projection(
            other_tenant.tenant_id,
            other_tenant.document_id,
        )
        shared_entity_id = target.bundles[0].entities[0].entity_id
        foreign_entity_id = other_tenant.bundles[0].entities[0].entity_id
        self.assertEqual(
            shared_entity_id,
            same_tenant.bundles[0].entities[0].entity_id,
        )
        self.assertNotEqual(shared_entity_id, foreign_entity_id)

        self.service.delete_document(
            tenant_id=target.tenant_id,
            document_id=target.document_id,
            operation_key="delete-only-target-a",
            expected_active_snapshot_id=target.snapshot.snapshot_id,
            source_generation=0,
        )

        self.assertIsNone(
            self._document_projection(target.tenant_id, target.document_id)
        )
        self.assertEqual(
            self._document_projection(
                same_tenant.tenant_id,
                same_tenant.document_id,
            ),
            same_tenant_before,
        )
        self.assertEqual(
            self._document_projection(
                other_tenant.tenant_id,
                other_tenant.document_id,
            ),
            other_tenant_before,
        )
        remaining_entities = self._records(
            """
            UNWIND $entity_ids AS entity_id
            MATCH (entity:Entity {entity_id: entity_id})
            RETURN collect(entity.entity_id) AS entity_ids
            """,
            entity_ids=[shared_entity_id, foreign_entity_id],
        )
        self.assertEqual(
            set(remaining_entities[0]["entity_ids"]),
            {shared_entity_id, foreign_entity_id},
        )
        self._assert_plan_active(same_tenant)
        self._assert_plan_active(other_tenant)
        self._assert_plan_hidden(target)


if __name__ == "__main__":
    unittest.main()
