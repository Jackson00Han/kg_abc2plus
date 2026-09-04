"""Disposable-Neo4j checks for the governed physical-delete guard."""

from __future__ import annotations

import ipaddress
import os
import unittest
from urllib.parse import urlparse

import neo4j

from graphrag_prod.graph.schema import apply_schema, verify_schema
from graphrag_prod.ingestion import (
    IngestionConflict,
    JobStatus,
    Neo4jIngestionService,
)
from tests.fixtures.ingestion import FixedClock, make_plan


class GovernedDeleteGuardNeo4jTests(unittest.TestCase):
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
        self.service = Neo4jIngestionService(
            self.driver,
            self.database,
            worker_id="governed-delete-guard-test-worker",
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

    def test_governed_revision_and_publication_history_block_without_mutation(
        self,
    ) -> None:
        plan = make_plan(operation_key="governed-delete-publication-source")
        self.service.ingest(plan)
        chunk_id = plan.bundles[0].chunk.chunk_id
        self.driver.execute_query(
            """
            MATCH (document:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })-[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot {
                tenant_id: $tenant_id,
                snapshot_id: $snapshot_id
            })-[:INCLUDES_CHUNK]->(chunk:Chunk {
                tenant_id: $tenant_id,
                chunk_id: $chunk_id
            })
            CREATE (head:KnowledgeRecordHead {
                record_id: $record_id,
                tenant_id: $tenant_id,
                record_kind: 'ENTITY_MENTION',
                current_revision: 1
            })
            CREATE (revision:GovernedEntityMentionRevision {
                revision_id: $revision_id,
                record_id: $record_id,
                revision: 1,
                tenant_id: $tenant_id,
                document_id: $document_id,
                version_id: $version_id,
                chunk_id: $chunk_id,
                governance_status: 'PUBLISHED'
            })
            CREATE (head)-[:CURRENT_REVISION]->(revision)
            CREATE (revision)-[:IN_CHUNK]->(chunk)
            CREATE (state:KnowledgePublicationState {
                tenant_id: $tenant_id,
                publication_generation: 1,
                activation_generation: 1
            })
            CREATE (publication:KnowledgePublication {
                publication_id: $publication_id,
                tenant_id: $tenant_id,
                generation: 1,
                status: 'ACTIVE',
                published_revision_ids: [$revision_id]
            })
            CREATE (state)-[:HAS_KNOWLEDGE_PUBLICATION]->(publication)
            CREATE (state)-[:ACTIVE_KNOWLEDGE_PUBLICATION]->(publication)
            CREATE (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->(revision)
            CREATE (publication)-[:USES_KNOWLEDGE_SNAPSHOT]->(snapshot)
            CREATE (activation:KnowledgePublicationActivation {
                activation_id: $activation_id,
                tenant_id: $tenant_id,
                activation_generation: 1,
                action: 'PUBLISH'
            })
            CREATE (state)-[:HAS_PUBLICATION_ACTIVATION]->(activation)
            CREATE (activation)-[:ACTIVATED_PUBLICATION]->(publication)
            """,
            tenant_id=plan.tenant_id,
            document_id=plan.document_id,
            version_id=plan.version_id,
            snapshot_id=plan.snapshot.snapshot_id,
            chunk_id=chunk_id,
            record_id="governed-delete-record",
            revision_id="governed-delete-revision",
            publication_id="governed-delete-publication",
            activation_id="governed-delete-activation",
            database_=self.database,
        )

        with self.assertRaisesRegex(IngestionConflict, "logical retirement"):
            self.service.delete_document(
                tenant_id=plan.tenant_id,
                document_id=plan.document_id,
                operation_key="blocked-governed-delete",
                expected_active_snapshot_id=plan.snapshot.snapshot_id,
                source_generation=0,
            )

        preserved = self._records(
            """
            MATCH (document:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })-[:ACTIVE_VERSION]->(version:DocumentVersion {
                tenant_id: $tenant_id,
                version_id: $version_id
            })-[:HAS_CHUNK]->(chunk:Chunk {
                tenant_id: $tenant_id,
                chunk_id: $chunk_id
            })
            MATCH (document)-[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot {
                tenant_id: $tenant_id,
                snapshot_id: $snapshot_id,
                build_state: 'PUBLISHED'
            })-[:INCLUDES_CHUNK]->(chunk)
            MATCH (revision:GovernedEntityMentionRevision {
                tenant_id: $tenant_id,
                revision_id: $revision_id
            })-[:IN_CHUNK]->(chunk)
            MATCH (publication:KnowledgePublication {
                tenant_id: $tenant_id,
                publication_id: $publication_id
            })-[:PUBLISHES_KNOWLEDGE_REVISION]->(revision)
            MATCH (publication)-[:USES_KNOWLEDGE_SNAPSHOT]->(snapshot)
            MATCH (:KnowledgePublicationActivation {
                tenant_id: $tenant_id,
                activation_id: $activation_id
            })-[:ACTIVATED_PUBLICATION]->(publication)
            RETURN document.generation AS generation,
                   chunk.retrieval_scope IS NOT NULL AS searchable,
                   NOT EXISTS {
                       MATCH (:DocumentTombstone {
                           tenant_id: $tenant_id,
                           document_id: $document_id
                       })
                   } AS no_tombstone
            """,
            tenant_id=plan.tenant_id,
            document_id=plan.document_id,
            version_id=plan.version_id,
            snapshot_id=plan.snapshot.snapshot_id,
            chunk_id=chunk_id,
            revision_id="governed-delete-revision",
            publication_id="governed-delete-publication",
            activation_id="governed-delete-activation",
        )
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0]["generation"], 0)
        self.assertTrue(preserved[0]["searchable"])
        self.assertTrue(preserved[0]["no_tombstone"])
        blocked_job = self._records(
            """
            MATCH (job:IngestionJob {
                tenant_id: $tenant_id,
                document_id: $document_id,
                operation: 'DELETE'
            })
            RETURN job.status AS status,
                   job.last_error_code AS last_error_code
            """,
            tenant_id=plan.tenant_id,
            document_id=plan.document_id,
        )[0]
        self.assertEqual(blocked_job["status"], JobStatus.FAILED_PERMANENT.value)
        self.assertEqual(blocked_job["last_error_code"], "IngestionConflict")

        # Damage the governed-record side deliberately.  The surviving
        # historical publication -> source Snapshot binding must independently
        # prevent physical deletion and preserve activation history.
        self.driver.execute_query(
            """
            MATCH (head:KnowledgeRecordHead {
                tenant_id: $tenant_id,
                record_id: $record_id
            })
            DETACH DELETE head
            WITH count(*) AS ignored
            MATCH (revision:GovernedEntityMentionRevision {
                tenant_id: $tenant_id,
                revision_id: $revision_id
            })
            DETACH DELETE revision
            """,
            tenant_id=plan.tenant_id,
            record_id="governed-delete-record",
            revision_id="governed-delete-revision",
            database_=self.database,
        )
        with self.assertRaisesRegex(IngestionConflict, "logical retirement"):
            self.service.delete_document(
                tenant_id=plan.tenant_id,
                document_id=plan.document_id,
                operation_key="blocked-publication-history-delete",
                expected_active_snapshot_id=plan.snapshot.snapshot_id,
                source_generation=0,
            )
        publication_history = self._records(
            """
            MATCH (publication:KnowledgePublication {
                tenant_id: $tenant_id,
                publication_id: $publication_id
            })-[:USES_KNOWLEDGE_SNAPSHOT]->(snapshot:KnowledgeSnapshot {
                tenant_id: $tenant_id,
                snapshot_id: $snapshot_id
            })
            MATCH (:KnowledgePublicationActivation {
                tenant_id: $tenant_id,
                activation_id: $activation_id
            })-[:ACTIVATED_PUBLICATION]->(publication)
            MATCH (:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })-[:ACTIVE_SNAPSHOT]->(snapshot)
            RETURN count(publication) AS publications
            """,
            tenant_id=plan.tenant_id,
            document_id=plan.document_id,
            publication_id="governed-delete-publication",
            snapshot_id=plan.snapshot.snapshot_id,
            activation_id="governed-delete-activation",
        )[0]
        self.assertEqual(publication_history["publications"], 1)

    def test_corrupt_relationship_property_edge_blocks_and_is_preserved(self) -> None:
        plan = make_plan(operation_key="governed-delete-property-source")
        self.service.ingest(plan)
        chunk_id = plan.bundles[0].chunk.chunk_id
        self.driver.execute_query(
            """
            MATCH (:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })-[:HAS_VERSION]->(:DocumentVersion)-[:HAS_CHUNK]->(chunk:Chunk {
                tenant_id: $tenant_id,
                chunk_id: $chunk_id
            })
            CREATE (value:RelationshipPropertyValue {
                property_value_id: $property_value_id,
                tenant_id: $tenant_id,
                evidence_chunk_id: $chunk_id
            })
            CREATE (value)-[:EVIDENCED_BY]->(chunk)
            """,
            tenant_id=plan.tenant_id,
            document_id=plan.document_id,
            chunk_id=chunk_id,
            property_value_id="corrupt-governed-property-value",
            database_=self.database,
        )

        with self.assertRaisesRegex(IngestionConflict, "logical retirement"):
            self.service.delete_document(
                tenant_id=plan.tenant_id,
                document_id=plan.document_id,
                operation_key="blocked-property-delete",
                expected_active_snapshot_id=plan.snapshot.snapshot_id,
                source_generation=0,
            )

        preserved = self._records(
            """
            MATCH (document:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })-[:HAS_VERSION]->(:DocumentVersion)-[:HAS_CHUNK]->(chunk:Chunk {
                tenant_id: $tenant_id,
                chunk_id: $chunk_id
            })
            MATCH (value:RelationshipPropertyValue {
                tenant_id: $tenant_id,
                property_value_id: $property_value_id
            })-[:EVIDENCED_BY]->(chunk)
            RETURN count(document) AS documents,
                   count(value) AS values,
                   NOT EXISTS {
                       MATCH (:DocumentTombstone {
                           tenant_id: $tenant_id,
                           document_id: $document_id
                       })
                   } AS no_tombstone
            """,
            tenant_id=plan.tenant_id,
            document_id=plan.document_id,
            chunk_id=chunk_id,
            property_value_id="corrupt-governed-property-value",
        )[0]
        self.assertEqual(preserved["documents"], 1)
        self.assertEqual(preserved["values"], 1)
        self.assertTrue(preserved["no_tombstone"])

        # The denormalized identity is an independent fail-closed path when a
        # damaged materialization has already lost its evidence edge.
        self.driver.execute_query(
            """
            MATCH (value:RelationshipPropertyValue {
                tenant_id: $tenant_id,
                property_value_id: $property_value_id
            })
            DETACH DELETE value
            CREATE (:RelationshipPropertyValue {
                property_value_id: $direct_property_value_id,
                tenant_id: $tenant_id,
                document_id: $document_id,
                evidence_chunk_id: $chunk_id
            })
            """,
            tenant_id=plan.tenant_id,
            document_id=plan.document_id,
            chunk_id=chunk_id,
            property_value_id="corrupt-governed-property-value",
            direct_property_value_id="corrupt-direct-property-value",
            database_=self.database,
        )
        with self.assertRaisesRegex(IngestionConflict, "logical retirement"):
            self.service.delete_document(
                tenant_id=plan.tenant_id,
                document_id=plan.document_id,
                operation_key="blocked-direct-property-delete",
                expected_active_snapshot_id=plan.snapshot.snapshot_id,
                source_generation=0,
            )
        direct_value = self._records(
            """
            MATCH (:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            MATCH (value:RelationshipPropertyValue {
                tenant_id: $tenant_id,
                document_id: $document_id,
                property_value_id: $property_value_id
            })
            RETURN count(value) AS values
            """,
            tenant_id=plan.tenant_id,
            document_id=plan.document_id,
            property_value_id="corrupt-direct-property-value",
        )[0]
        self.assertEqual(direct_value["values"], 1)

    def test_foreign_tenant_same_document_id_does_not_block_or_leak(self) -> None:
        plan = make_plan(operation_key="governed-delete-tenant-scope")
        self.service.ingest(plan)
        self.driver.execute_query(
            """
            CREATE (:GovernedEntityMentionRevision {
                revision_id: 'foreign-governed-revision',
                record_id: 'foreign-governed-record',
                revision: 1,
                tenant_id: 'foreign-tenant',
                document_id: $document_id
            })
            CREATE (:KnowledgePublication {
                publication_id: 'foreign-publication',
                tenant_id: 'foreign-tenant',
                generation: 1,
                status: 'RETIRED'
            })
            CREATE (:RelationshipPropertyValue {
                property_value_id: 'foreign-property-value',
                tenant_id: 'foreign-tenant',
                document_id: $document_id
            })
            """,
            document_id=plan.document_id,
            database_=self.database,
        )

        deleted = self.service.delete_document(
            tenant_id=plan.tenant_id,
            document_id=plan.document_id,
            operation_key="tenant-closed-physical-delete",
            expected_active_snapshot_id=plan.snapshot.snapshot_id,
            source_generation=0,
        )

        self.assertEqual(deleted.job.status, JobStatus.SUCCEEDED)
        self.assertEqual(deleted.job.outcome, "DELETED")
        counts = self._records(
            """
            OPTIONAL MATCH (document:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            WITH count(document) AS target_documents
            MATCH (revision:GovernedEntityMentionRevision {
                tenant_id: 'foreign-tenant',
                revision_id: 'foreign-governed-revision'
            })
            MATCH (publication:KnowledgePublication {
                tenant_id: 'foreign-tenant',
                publication_id: 'foreign-publication'
            })
            MATCH (value:RelationshipPropertyValue {
                tenant_id: 'foreign-tenant',
                property_value_id: 'foreign-property-value'
            })
            RETURN target_documents,
                   count(DISTINCT revision) AS foreign_revisions,
                   count(DISTINCT publication) AS foreign_publications,
                   count(DISTINCT value) AS foreign_values
            """,
            tenant_id=plan.tenant_id,
            document_id=plan.document_id,
        )[0]
        self.assertEqual(counts["target_documents"], 0)
        self.assertEqual(counts["foreign_revisions"], 1)
        self.assertEqual(counts["foreign_publications"], 1)
        self.assertEqual(counts["foreign_values"], 1)


if __name__ == "__main__":
    unittest.main()
