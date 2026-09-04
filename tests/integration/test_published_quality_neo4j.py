"""Disposable-Neo4j checks for active governed-graph quality audits."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import dataclasses
import ipaddress
import os
import unittest
from urllib.parse import urlparse

import neo4j

from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import relationship_property_value_id
from graphrag_prod.domain.models import RelationshipPropertyValue, TypedLiteralValue
from graphrag_prod.graph.provenance import Neo4jProvenanceStore
from graphrag_prod.graph.published_quality import (
    Neo4jPublishedGraphQualityService,
    PublishedGraphQualityAuthorizationError,
    PublishedGraphQualityConflict,
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
    Cardinality,
    EntityTypeDefinition,
    Neo4jTBoxStore,
    PropertyDataType,
    PropertyDefinition,
    RelationshipTypeDefinition,
    TBoxStatus,
    TBoxVersion,
)
from tests.fixtures.domain import make_bundle
from tests.fixtures.knowledge import KNOWLEDGE_TIME, make_knowledge_batch


REVIEWED_AT = KNOWLEDGE_TIME + timedelta(hours=1)
PUBLISHED_AT = REVIEWED_AT + timedelta(hours=1)


class PublishedGraphQualityNeo4jTests(unittest.TestCase):
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
        self.tenant_id = "tenant-published-quality"
        self.bundle = make_bundle(tenant_id=self.tenant_id)
        self.tbox = TBoxVersion(
            tenant_id=self.tenant_id,
            key="published-quality",
            version=1,
            status=TBoxStatus.DRAFT,
            entity_types=(
                EntityTypeDefinition("Company", ("ticker", "llm-candidate")),
                EntityTypeDefinition(
                    "Product",
                    ("apple-product", "llm-candidate"),
                ),
            ),
            relationship_types=(
                RelationshipTypeDefinition(
                    "OFFERS",
                    ("Company",),
                    ("Product",),
                    properties=(
                        PropertyDefinition(
                            "BASIS",
                            PropertyDataType.STRING,
                            False,
                            Cardinality.ZERO_OR_ONE,
                        ),
                    ),
                    source_cardinality=Cardinality.ONE,
                    target_cardinality=Cardinality.ONE,
                ),
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
                profile_id: 'published-quality-integration:v1',
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
            snapshot_id=f"{self.tenant_id}:quality-snapshot:v1",
            created_at=datetime(2025, 2, 3, 4, 6, tzinfo=UTC),
            database_=self.database,
        )
        self.candidate_batch = make_knowledge_batch(
            authoritative=False,
            tenant_id=self.tenant_id,
            ontology_version_id=self.tbox.tbox_id,
        )
        self.authoritative_batch = make_knowledge_batch(
            tenant_id=self.tenant_id,
            ontology_version_id=self.tbox.tbox_id,
        )
        self.store = Neo4jKnowledgeStore(self.driver, self.database)
        self.store.persist_llm_candidates(self.candidate_batch)
        self.review = Neo4jKnowledgeReviewService(self.driver, self.database)
        self.publication = Neo4jKnowledgePublicationService(
            self.driver,
            self.database,
        )
        self.principal = Principal(
            "expert:quality",
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
        self.quality = Neo4jPublishedGraphQualityService(
            self.driver,
            self.database,
        )

    def tearDown(self) -> None:
        self.driver.execute_query(
            "MATCH (node) DETACH DELETE node",
            database_=self.database,
        )

    def _relationship_property(self) -> RelationshipPropertyValue:
        assertion = self.candidate_batch.assertions[0]
        extractor = assertion.trust.extractor_version or ""
        start = self.bundle.chunk.text.index("offers")
        end = start + len("offers")
        literal = TypedLiteralValue(
            datatype="STRING",
            typed_value="offers",
            raw_value="offers",
            canonical_value="offers",
        )
        return RelationshipPropertyValue(
            property_value_id=relationship_property_value_id(
                self.tenant_id,
                "OFFERS",
                "BASIS",
                literal.identity_reference,
                assertion.evidence.chunk_id,
                start,
                end,
                extractor,
                self.tbox.tbox_id,
            ),
            tenant_id=self.tenant_id,
            relationship_type="OFFERS",
            name="BASIS",
            literal_semantics=literal,
            evidence_chunk_id=assertion.evidence.chunk_id,
            evidence_char_start=start,
            evidence_char_end=end,
            evidence_text="offers",
            extractor_version=extractor,
            schema_version=self.tbox.tbox_id,
            confidence=0.97,
        )

    def _publish(self):  # type: ignore[no-untyped-def]
        mention_requests = tuple(
            ReviewRequest(
                ReviewRecordKind.ENTITY_MENTION,
                candidate.record_id,
                1,
                GovernanceStatus.APPROVED,
                REVIEWED_AT,
                "Endpoint identity and exact evidence verified.",
                MentionEdit(authoritative.entity, candidate.confidence),
            )
            for candidate, authoritative in zip(
                self.candidate_batch.mentions,
                self.authoritative_batch.mentions,
                strict=True,
            )
        )
        candidate = self.candidate_batch.assertions[0]
        authoritative = self.authoritative_batch.assertions[0]
        assertion_request = ReviewRequest(
            ReviewRecordKind.ASSERTION,
            candidate.record_id,
            1,
            GovernanceStatus.APPROVED,
            REVIEWED_AT,
            "Relationship, qualifier, endpoints, and evidence verified.",
            AssertionEdit(
                authoritative.subject,
                "OFFERS",
                candidate.subject_mention_revision_id,
                candidate.confidence,
                object_entity=authoritative.object_entity,
                object_mention_revision_id=candidate.object_mention_revision_id,
                relationship_properties=(self._relationship_property(),),
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

    def test_clean_active_publication_passes_and_is_repeatable(self) -> None:
        publication = self._publish()

        first = self.quality.audit(self.principal)
        second = self.quality.audit(self.principal)

        self.assertTrue(first.passed, first.to_json())
        self.assertEqual(first.total_issue_count, 0)
        self.assertEqual(first, second)
        self.assertEqual(first.publication_id, publication.publication_id)
        self.assertEqual(
            dict(first.counts),
            {
                "assertions": 1,
                "canonical_entities": 2,
                "entity_mentions": 2,
                "literal_assertions": 0,
                "relationship_assertions": 1,
                "revisions": 3,
            },
        )

    def test_partial_acl_is_rejected_without_partial_statistics(self) -> None:
        self._publish()
        outsider = dataclasses.replace(
            self.principal,
            principal_id="expert:outsider",
            groups=frozenset({"legal"}),
        )

        with self.assertRaises(PublishedGraphQualityAuthorizationError):
            self.quality.audit(outsider)

    def test_navigation_and_relationship_property_tampering_is_detected(self) -> None:
        publication = self._publish()
        clean = self.quality.audit(self.principal)
        self.assertTrue(clean.passed, clean.to_json())
        self.driver.execute_query(
            """
            MATCH (mention:EntityMention {
                tenant_id: $tenant_id,
                governed_publication_id: $publication_id
            })
            WITH mention ORDER BY mention.mention_id LIMIT 1
            SET mention.surface = 'tampered-surface'
            WITH mention
            MATCH (assertion:Assertion {
                tenant_id: $tenant_id,
                governed_publication_id: $publication_id
            })-[:HAS_RELATIONSHIP_PROPERTY]->
                  (value:RelationshipPropertyValue)
            SET assertion.predicate = 'TAMPERED_PREDICATE',
                assertion.literal_canonical_value = 'forged-typed-value',
                assertion.relationship_properties_json = '[]',
                value.property_value_id = 'tampered-property-value-id',
                value.confidence = 0.5
            """,
            tenant_id=self.tenant_id,
            publication_id=publication.publication_id,
            database_=self.database,
        )

        report = self.quality.audit(self.principal)
        codes = {issue.code for issue in report.issues}

        self.assertFalse(report.passed)
        self.assertTrue(
            {
                "ACTIVE_MENTION_PROJECTION_INVALID",
                "ACTIVE_ASSERTION_PROJECTION_INVALID",
                "RELATIONSHIP_PROPERTY_MATERIALIZATION_INVALID",
            }
            <= codes,
            report.to_json(),
        )
        serialized = report.to_json()
        self.assertNotIn("tampered-surface", serialized)
        self.assertNotIn("forged-typed-value", serialized)

    def test_missing_relationship_revision_fails_endpoint_cardinality(self) -> None:
        publication = self._publish()
        self.driver.execute_query(
            """
            MATCH (publication:KnowledgePublication {
                tenant_id: $tenant_id,
                publication_id: $publication_id
            })-[membership:PUBLISHES_KNOWLEDGE_REVISION]->
                  (:GovernedAssertionRevision)
            DELETE membership
            """,
            tenant_id=self.tenant_id,
            publication_id=publication.publication_id,
            database_=self.database,
        )

        report = self.quality.audit(self.principal)
        codes = {issue.code for issue in report.issues}

        self.assertIn("PUBLICATION_MANIFEST_MISMATCH", codes)
        self.assertIn("RELATIONSHIP_ENDPOINT_CARDINALITY_INVALID", codes)

    def test_no_active_publication_or_missing_tbox_binding_fails_closed(self) -> None:
        with self.assertRaises(PublishedGraphQualityConflict):
            self.quality.audit(self.principal)

        publication = self._publish()
        self.driver.execute_query(
            """
            MATCH (publication:KnowledgePublication {
                tenant_id: $tenant_id,
                publication_id: $publication_id
            })-[binding:USES_TBOX_VERSION]->(:TBoxVersion)
            DELETE binding
            """,
            tenant_id=self.tenant_id,
            publication_id=publication.publication_id,
            database_=self.database,
        )
        with self.assertRaises(PublishedGraphQualityConflict):
            self.quality.audit(self.principal)


if __name__ == "__main__":
    unittest.main()
