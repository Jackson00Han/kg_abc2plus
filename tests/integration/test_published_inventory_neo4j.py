"""Disposable-Neo4j checks for the active publication A-Box inventory."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import dataclasses
import ipaddress
import os
import unittest
from urllib.parse import urlparse

import neo4j

from graphrag_prod.domain.access import Principal
from graphrag_prod.graph.provenance import Neo4jProvenanceStore
from graphrag_prod.graph.published_inventory import (
    ActivePublicationInventoryAuthorizationError,
    ActivePublicationInventoryConflict,
    Neo4jActivePublicationInventoryService,
)
from graphrag_prod.graph.schema import apply_schema, verify_schema
from graphrag_prod.knowledge.review import (
    KNOWLEDGE_PUBLISH_CAPABILITY,
    KNOWLEDGE_REVIEW_CAPABILITY,
    AssertionEdit,
    MentionEdit,
    Neo4jKnowledgePublicationService,
    Neo4jKnowledgeReviewService,
    ReviewRecordKind,
    ReviewRequest,
)
from graphrag_prod.knowledge.store import Neo4jKnowledgeStore
from graphrag_prod.knowledge.trust import GovernanceStatus
from graphrag_prod.ontology import (
    EntityTypeDefinition,
    Neo4jTBoxStore,
    RelationshipTypeDefinition,
    TBoxStatus,
    TBoxVersion,
)
from tests.fixtures.domain import make_bundle
from tests.fixtures.knowledge import KNOWLEDGE_TIME, make_knowledge_batch


REVIEWED_AT = KNOWLEDGE_TIME + timedelta(hours=1)
PUBLISHED_AT = REVIEWED_AT + timedelta(hours=1)


class PublishedInventoryNeo4jTests(unittest.TestCase):
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
            notifications_min_severity="OFF",
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
        self.tenant_id = "tenant-published-inventory"
        self.bundle = make_bundle(tenant_id=self.tenant_id)
        self.tbox = TBoxVersion(
            tenant_id=self.tenant_id,
            key="published-inventory",
            version=1,
            status=TBoxStatus.DRAFT,
            entity_types=(
                EntityTypeDefinition("Company", ("ticker", "llm-candidate")),
                EntityTypeDefinition("Product", ("apple-product", "llm-candidate")),
            ),
            relationship_types=(
                RelationshipTypeDefinition("OFFERS", ("Company",), ("Product",)),
            ),
        )
        tbox_store = Neo4jTBoxStore(self.driver, self.database)
        tbox_store.import_version(self.tbox)
        tbox_store.publish(
            self.tenant_id,
            self.tbox.tbox_id,
            expected_active_tbox_id=None,
        )
        Neo4jProvenanceStore(self.driver, self.database).write_bundle(self.bundle)
        self.driver.execute_query(
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
            CREATE (snapshot:KnowledgeSnapshot {
                snapshot_id: $snapshot_id,
                tenant_id: $tenant_id,
                document_id: $document_id,
                version_id: $version_id,
                profile_id: 'published-inventory-integration:v1',
                build_state: 'PUBLISHED',
                created_at: $created_at
            })
            CREATE (snapshot)-[:OF_VERSION]->(version)
            CREATE (snapshot)-[:INCLUDES_CHUNK]->(chunk)
            CREATE (document)-[:ACTIVE_SNAPSHOT]->(snapshot)
            """,
            tenant_id=self.tenant_id,
            document_id=self.bundle.document.document_id,
            version_id=self.bundle.version.version_id,
            chunk_id=self.bundle.chunk.chunk_id,
            snapshot_id=f"{self.tenant_id}:inventory-snapshot:v1",
            created_at=datetime(2025, 2, 3, 4, 6, tzinfo=UTC),
            database_=self.database,
        )
        self.candidates = make_knowledge_batch(
            authoritative=False,
            tenant_id=self.tenant_id,
            ontology_version_id=self.tbox.tbox_id,
        )
        self.authoritative = make_knowledge_batch(
            tenant_id=self.tenant_id,
            ontology_version_id=self.tbox.tbox_id,
        )
        Neo4jKnowledgeStore(self.driver, self.database).persist_llm_candidates(
            self.candidates
        )
        self.review = Neo4jKnowledgeReviewService(self.driver, self.database)
        self.publication = Neo4jKnowledgePublicationService(
            self.driver,
            self.database,
        )
        self.principal = Principal(
            "expert:inventory",
            self.tenant_id,
            frozenset({"finance-readers"}),
            frozenset(
                {
                    KNOWLEDGE_REVIEW_CAPABILITY,
                    KNOWLEDGE_PUBLISH_CAPABILITY,
                    "knowledge:quality",
                }
            ),
        )
        self.inventory = Neo4jActivePublicationInventoryService(
            self.driver,
            self.database,
        )

    def tearDown(self) -> None:
        self.driver.execute_query(
            "MATCH (node) DETACH DELETE node",
            database_=self.database,
        )

    def _publish(self):  # type: ignore[no-untyped-def]
        mention_requests = tuple(
            ReviewRequest(
                ReviewRecordKind.ENTITY_MENTION,
                candidate.record_id,
                1,
                GovernanceStatus.APPROVED,
                REVIEWED_AT,
                "Canonical identity and evidence verified.",
                MentionEdit(authoritative.entity, candidate.confidence),
            )
            for candidate, authoritative in zip(
                self.candidates.mentions,
                self.authoritative.mentions,
                strict=True,
            )
        )
        candidate = self.candidates.assertions[0]
        authoritative = self.authoritative.assertions[0]
        assertion_request = ReviewRequest(
            ReviewRecordKind.ASSERTION,
            candidate.record_id,
            1,
            GovernanceStatus.APPROVED,
            REVIEWED_AT,
            "Predicate, endpoints, and evidence verified.",
            AssertionEdit(
                authoritative.subject,
                "OFFERS",
                candidate.subject_mention_revision_id,
                candidate.confidence,
                object_entity=authoritative.object_entity,
                object_mention_revision_id=candidate.object_mention_revision_id,
            ),
        )
        outcomes = self.review.review_batch(
            self.principal,
            (*mention_requests, assertion_request),
        ).outcomes
        return self.publication.publish(
            self.principal,
            tuple(outcome.revision_id for outcome in outcomes),
            expected_active_publication_id=None,
            published_at=PUBLISHED_AT,
        )

    def test_lists_exact_active_manifest_without_source_text(self) -> None:
        publication = self._publish()

        result = self.inventory.list_active(self.principal, limit=10)

        self.assertEqual(result.publication_id, publication.publication_id)
        self.assertEqual(result.total_record_count, 3)
        self.assertEqual(result.matching_record_count, 3)
        self.assertFalse(result.truncated)
        self.assertEqual(
            tuple(item.record_kind for item in result.items),
            ("ASSERTION", "ENTITY_MENTION", "ENTITY_MENTION"),
        )
        assertion = result.items[0]
        self.assertEqual(assertion.ontology_key, "OFFERS")
        self.assertEqual(
            assertion.assertion.subject.display_name,  # type: ignore[union-attr]
            "Apple Inc.",
        )
        self.assertEqual(
            assertion.assertion.object_entity.display_name,  # type: ignore[union-attr]
            "iPhone",
        )
        serialized = repr(result.to_dict())
        self.assertNotIn(self.bundle.chunk.text, serialized)
        self.assertNotIn("evidence_text", serialized)
        self.assertNotIn("quoted_text", serialized)

        absent = self.inventory.list_active(
            self.principal,
            document_id="authorized-but-absent-document",
        )
        self.assertEqual(absent.matching_record_count, 0)
        self.assertEqual(absent.items, ())

    def test_partial_acl_and_materialization_tampering_fail_closed(self) -> None:
        publication = self._publish()
        partial = dataclasses.replace(
            self.principal,
            principal_id="expert:partial",
            groups=frozenset({"legal"}),
        )
        with self.assertRaises(ActivePublicationInventoryAuthorizationError):
            self.inventory.list_active(
                partial,
                document_id=self.bundle.document.document_id,
            )

        self.driver.execute_query(
            """
            MATCH (mention:EntityMention {
                tenant_id: $tenant_id,
                governed_publication_id: $publication_id
            })
            WITH mention ORDER BY mention.mention_id LIMIT 1
            SET mention.surface = 'tampered-source-fragment'
            """,
            tenant_id=self.tenant_id,
            publication_id=publication.publication_id,
            database_=self.database,
        )
        with self.assertRaises(ActivePublicationInventoryConflict) as raised:
            self.inventory.list_active(self.principal)
        self.assertNotIn("tampered-source-fragment", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
