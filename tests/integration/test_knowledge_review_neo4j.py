"""Disposable-Neo4j checks for review, publication, and rollback."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
import ipaddress
import os
import unittest
from urllib.parse import urlparse

import neo4j

from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import assertion_id, mention_id
from graphrag_prod.domain.models import TypedLiteralValue
from graphrag_prod.graph.provenance import Neo4jProvenanceStore
from graphrag_prod.graph.schema import apply_schema, verify_schema
from graphrag_prod.knowledge.review import (
    KNOWLEDGE_PUBLISH_CAPABILITY,
    KNOWLEDGE_REVIEW_CAPABILITY,
    AssertionEdit,
    KnowledgeAuthorizationError,
    KnowledgePublicationConflict,
    KnowledgeReviewUnavailable,
    MentionEdit,
    Neo4jKnowledgePublicationService,
    Neo4jKnowledgeReviewService,
    ReviewRecordKind,
    ReviewRequest,
)
from graphrag_prod.knowledge.models import (
    ABoxRecordBatch,
    RecordRevision,
    knowledge_record_id,
)
from graphrag_prod.knowledge.store import KnowledgeConflict, Neo4jKnowledgeStore
from graphrag_prod.knowledge.trust import AuthorityLevel, GovernanceStatus
from graphrag_prod.ontology import (
    EntityTypeDefinition,
    Neo4jTBoxStore,
    Cardinality,
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


class Neo4jKnowledgeReviewIntegrationTests(unittest.TestCase):
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
        self.tenant_id = "tenant-knowledge"
        tbox = TBoxVersion(
            tenant_id=self.tenant_id,
            key="company",
            version=1,
            status=TBoxStatus.DRAFT,
            entity_types=(
                EntityTypeDefinition(
                    "Company",
                    ("ticker",),
                    properties=(
                        PropertyDefinition(
                            "DISPLAY_NAME",
                            PropertyDataType.STRING,
                            False,
                            Cardinality.ZERO_OR_ONE,
                        ),
                    ),
                ),
                EntityTypeDefinition("Product", ("apple-product",)),
            ),
            relationship_types=(
                RelationshipTypeDefinition(
                    "OFFERS",
                    ("Company",),
                    ("Product",),
                ),
            ),
        )
        tbox_store = Neo4jTBoxStore(self.driver, self.database)
        tbox_store.import_version(tbox)
        tbox_store.publish(
            self.tenant_id,
            tbox.tbox_id,
            expected_active_tbox_id=None,
        )
        self.tbox_id = tbox.tbox_id
        self.bundle = make_bundle(tenant_id=self.tenant_id)
        Neo4jProvenanceStore(self.driver, self.database).write_bundle(self.bundle)
        # Stage 2's compatibility writer intentionally stops before the
        # managed KnowledgeSnapshot lifecycle. Review/publication must fail
        # closed unless evidence is in the exact active published snapshot,
        # so promote this deterministic fixture to that production shape.
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
                profile_id: 'review-integration:v1',
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
            snapshot_id=f"{self.tenant_id}:review-snapshot:v1",
            created_at=datetime(2025, 2, 3, 4, 6, tzinfo=UTC),
            database_=self.database,
        )
        self.store = Neo4jKnowledgeStore(self.driver, self.database)
        self.review = Neo4jKnowledgeReviewService(self.driver, self.database)
        self.publication = Neo4jKnowledgePublicationService(
            self.driver,
            self.database,
        )
        self.principal = Principal(
            "expert:alice",
            self.tenant_id,
            frozenset({"finance-readers"}),
            frozenset(
                {
                    KNOWLEDGE_REVIEW_CAPABILITY,
                    KNOWLEDGE_PUBLISH_CAPABILITY,
                }
            ),
        )
        self.batch = make_knowledge_batch(
            authoritative=False,
            tenant_id=self.tenant_id,
            ontology_version_id=self.tbox_id,
        )
        self.authoritative_batch = make_knowledge_batch(
            tenant_id=self.tenant_id,
            ontology_version_id=self.tbox_id,
        )
        self.store.persist_llm_candidates(self.batch)

    def tearDown(self) -> None:
        self.driver.execute_query(
            "MATCH (node) DETACH DELETE node",
            database_=self.database,
        )

    def _approve_all(self) -> tuple[str, ...]:
        mention_requests = tuple(
            ReviewRequest(
                ReviewRecordKind.ENTITY_MENTION,
                record.record_id,
                1,
                GovernanceStatus.APPROVED,
                REVIEWED_AT,
                "Reviewed against the exact filing evidence.",
                MentionEdit(authoritative.entity, record.confidence),
            )
            for record, authoritative in zip(
                self.batch.mentions,
                self.authoritative_batch.mentions,
                strict=True,
            )
        )
        candidate_assertion = self.batch.assertions[0]
        authoritative_assertion = self.authoritative_batch.assertions[0]
        assertion_request = ReviewRequest(
            ReviewRecordKind.ASSERTION,
            candidate_assertion.record_id,
            1,
            GovernanceStatus.APPROVED,
            REVIEWED_AT,
            "Reviewed against the exact filing evidence.",
            AssertionEdit(
                authoritative_assertion.subject,
                authoritative_assertion.predicate,
                candidate_assertion.subject_mention_revision_id,
                candidate_assertion.confidence,
                object_entity=authoritative_assertion.object_entity,
                object_mention_revision_id=(
                    candidate_assertion.object_mention_revision_id
                ),
            ),
        )
        return tuple(
            outcome.revision_id
            for outcome in self.review.review_batch(
                self.principal,
                (*mention_requests, assertion_request),
            ).outcomes
        )

    def _distinct_authoritative_batch(self) -> ABoxRecordBatch:
        mention_revisions = {
            mention.revision_id: RecordRevision.next(
                knowledge_record_id(
                    self.tenant_id,
                    "ENTITY_MENTION",
                    f"authoritative:{mention.record_id}",
                ),
                0,
            )
            for mention in self.authoritative_batch.mentions
        }
        mentions = tuple(
            dataclasses.replace(
                mention,
                revision=mention_revisions[mention.revision_id],
            )
            for mention in self.authoritative_batch.mentions
        )
        assertion = self.authoritative_batch.assertions[0]
        authoritative_assertion = dataclasses.replace(
            assertion,
            revision=RecordRevision.next(
                knowledge_record_id(
                    self.tenant_id,
                    "ASSERTION",
                    f"authoritative:{assertion.record_id}",
                ),
                0,
            ),
            subject_mention_revision_id=mention_revisions[
                assertion.subject_mention_revision_id
            ].revision_id,
            object_mention_revision_id=mention_revisions[
                assertion.object_mention_revision_id or ""
            ].revision_id,
        )
        return dataclasses.replace(
            self.authoritative_batch,
            mentions=mentions,
            assertions=(authoritative_assertion,),
        )

    def test_queue_is_tenant_acl_safe_and_review_is_append_only_cas(self) -> None:
        reader = Principal(
            "reader",
            self.tenant_id,
            frozenset({"finance-readers"}),
        )
        with self.assertRaises(KnowledgeAuthorizationError):
            self.review.review_queue(reader)
        with self.assertRaises(KnowledgeAuthorizationError):
            self.publication.active(reader)

        queue = self.review.review_queue(self.principal)
        self.assertEqual(len(queue), 3)
        self.assertEqual(
            {item.record.trust.status for item in queue},
            {GovernanceStatus.CANDIDATE},
        )
        wrong_group = Principal(
            "outsider",
            self.tenant_id,
            frozenset({"legal"}),
            frozenset({KNOWLEDGE_REVIEW_CAPABILITY}),
        )
        wrong_tenant = Principal(
            "outsider",
            "another-tenant",
            frozenset({"finance-readers"}),
            frozenset({KNOWLEDGE_REVIEW_CAPABILITY}),
        )
        self.assertEqual(self.review.review_queue(wrong_group), ())
        self.assertEqual(self.review.review_queue(wrong_tenant), ())

        mention = self.batch.mentions[0]
        outcome = self.review.approve(
            self.principal,
            record_kind=ReviewRecordKind.ENTITY_MENTION,
            record_id=mention.record_id,
            expected_revision=1,
            reviewed_at=REVIEWED_AT,
            notes="Name and span verified by domain expert.",
            edit=MentionEdit(
                self.authoritative_batch.mentions[0].entity,
                0.99,
            ),
        )
        self.assertEqual(outcome.revision, 2)
        approved = self.store.get_entity_mention(
            self.principal,
            mention.record_id,
            statuses=(GovernanceStatus.APPROVED,),
        )
        assert approved is not None
        self.assertEqual(approved.confidence, 0.99)
        self.assertEqual(approved.trust.authority, AuthorityLevel.SECONDARY)
        self.assertEqual(approved.trust.reviewed_by, "expert:alice")
        self.assertEqual(approved.trust.reviewed_at, REVIEWED_AT)
        self.assertEqual(
            approved.trust.review_notes,
            "Name and span verified by domain expert.",
        )
        with self.assertRaises(KnowledgeConflict):
            self.review.approve(
                self.principal,
                record_kind=ReviewRecordKind.ENTITY_MENTION,
                record_id=mention.record_id,
                expected_revision=1,
                reviewed_at=REVIEWED_AT,
                notes="Stale retry.",
            )
        with self.assertRaises(KnowledgeReviewUnavailable):
            self.review.reject(
                wrong_tenant,
                record_kind=ReviewRecordKind.ENTITY_MENTION,
                record_id=self.batch.mentions[1].record_id,
                expected_revision=1,
                reviewed_at=REVIEWED_AT,
                notes="Cross-tenant attempt.",
            )

        revisions, _, _ = self.driver.execute_query(
            """
            MATCH (:KnowledgeRecordHead {record_id: $record_id})
                  -[:CURRENT_REVISION]->(current)
            MATCH (current)-[:SUPERSEDES]->(original)
            RETURN current.revision AS current_revision,
                   original.revision AS original_revision,
                   original.governance_status AS original_status
            """,
            record_id=mention.record_id,
            database_=self.database,
        )
        self.assertEqual(dict(revisions[0]), {
            "current_revision": 2,
            "original_revision": 1,
            "original_status": "CANDIDATE",
        })

    def test_reject_and_quarantine_are_audited_new_revisions(self) -> None:
        rejected = self.review.reject(
            self.principal,
            record_kind=ReviewRecordKind.ENTITY_MENTION,
            record_id=self.batch.mentions[0].record_id,
            expected_revision=1,
            reviewed_at=REVIEWED_AT,
            notes="Entity is not supported by the quoted span.",
        )
        quarantined = self.review.quarantine(
            self.principal,
            record_kind=ReviewRecordKind.ENTITY_MENTION,
            record_id=self.batch.mentions[1].record_id,
            expected_revision=1,
            reviewed_at=REVIEWED_AT,
            notes="Needs a product taxonomy specialist.",
        )
        self.assertEqual(rejected.status, GovernanceStatus.REJECTED)
        self.assertEqual(quarantined.status, GovernanceStatus.QUARANTINED)
        self.assertEqual(rejected.revision, 2)
        self.assertEqual(quarantined.revision, 2)
        queue = self.review.review_queue(
            self.principal,
            statuses=(GovernanceStatus.QUARANTINED,),
        )
        self.assertEqual(
            [item.record.record_id for item in queue],
            [self.batch.mentions[1].record_id],
        )

    def test_typed_literal_review_revalidates_and_publication_preserves_semantics(
        self,
    ) -> None:
        candidate_mention = self.batch.mentions[0]
        authoritative_mention = self.authoritative_batch.mentions[0]
        mention_outcome = self.review.approve(
            self.principal,
            record_kind=ReviewRecordKind.ENTITY_MENTION,
            record_id=candidate_mention.record_id,
            expected_revision=1,
            reviewed_at=REVIEWED_AT,
            notes="Company identity verified.",
            edit=MentionEdit(
                authoritative_mention.entity,
                candidate_mention.confidence,
            ),
        )
        candidate = self.batch.assertions[0]
        forged = TypedLiteralValue(
            datatype="STRING",
            typed_value="Apple",
            raw_value="Apple",
            canonical_value="forged-canonical-value",
        )
        with self.assertRaises(KnowledgeReviewUnavailable):
            self.review.approve(
                self.principal,
                record_kind=ReviewRecordKind.ASSERTION,
                record_id=candidate.record_id,
                expected_revision=1,
                reviewed_at=REVIEWED_AT,
                notes="Must reject a client-supplied canonical value.",
                edit=AssertionEdit(
                    authoritative_mention.entity,
                    "DISPLAY_NAME",
                    candidate.subject_mention_revision_id,
                    candidate.confidence,
                    literal_value="Apple",
                    literal_semantics=forged,
                ),
            )

        literal = dataclasses.replace(forged, canonical_value="Apple")
        assertion_outcome = self.review.approve(
            self.principal,
            record_kind=ReviewRecordKind.ASSERTION,
            record_id=candidate.record_id,
            expected_revision=1,
            reviewed_at=REVIEWED_AT,
            notes="Typed source literal verified.",
            edit=AssertionEdit(
                authoritative_mention.entity,
                "DISPLAY_NAME",
                candidate.subject_mention_revision_id,
                candidate.confidence,
                literal_value="Apple",
                literal_semantics=literal,
            ),
        )
        approved = self.store.get_assertion(
            self.principal,
            candidate.record_id,
            statuses=(GovernanceStatus.APPROVED,),
        )
        assert approved is not None
        self.assertEqual(approved.literal_semantics, literal)

        view = self.publication.publish(
            self.principal,
            (mention_outcome.revision_id, assertion_outcome.revision_id),
            expected_active_publication_id=None,
            published_at=PUBLISHED_AT,
        )
        rows, _, _ = self.driver.execute_query(
            """
            MATCH (:KnowledgePublication {publication_id: $publication_id})
                  -[:PUBLISHES_KNOWLEDGE_REVISION]->
                  (revision:GovernedAssertionRevision)
            MATCH (:KnowledgeSnapshot)-[:INCLUDES_ASSERTION {
                governed_publication_id: $publication_id
            }]->(assertion:Assertion)
            WHERE assertion.governed_revision_id = revision.revision_id
            RETURN revision.literal_datatype AS revision_datatype,
                   revision.literal_canonical_value AS revision_value,
                   assertion.literal_datatype AS assertion_datatype,
                   assertion.literal_canonical_value AS assertion_value
            """,
            publication_id=view.publication_id,
            database_=self.database,
        )
        self.assertEqual(
            dict(rows[0]),
            {
                "revision_datatype": "STRING",
                "revision_value": "Apple",
                "assertion_datatype": "STRING",
                "assertion_value": "Apple",
            },
        )

    def test_review_rejects_stale_exact_evidence(self) -> None:
        first_approved = self.review.approve(
            self.principal,
            record_kind=ReviewRecordKind.ENTITY_MENTION,
            record_id=self.batch.mentions[0].record_id,
            expected_revision=1,
            reviewed_at=REVIEWED_AT,
            notes="Approved before source state changed.",
            edit=MentionEdit(
                self.authoritative_batch.mentions[0].entity,
                self.batch.mentions[0].confidence,
            ),
        )
        self.driver.execute_query(
            """
            MATCH (chunk:Chunk {chunk_id: $chunk_id})
            SET chunk.text = 'Evidence changed after extraction.'
            """,
            chunk_id=self.bundle.chunk.chunk_id,
            database_=self.database,
        )
        self.assertEqual(self.review.review_queue(self.principal), ())
        with self.assertRaises(KnowledgeReviewUnavailable):
            self.review.approve(
                self.principal,
                record_kind=ReviewRecordKind.ENTITY_MENTION,
                record_id=self.batch.mentions[1].record_id,
                expected_revision=1,
                reviewed_at=REVIEWED_AT,
                notes="Must not approve stale source evidence.",
            )
        with self.assertRaises(KnowledgeReviewUnavailable):
            self.publication.publish(
                self.principal,
                (first_approved.revision_id,),
                expected_active_publication_id=None,
                published_at=PUBLISHED_AT,
            )

    def test_publish_materializes_only_approved_exact_navigation(self) -> None:
        with self.assertRaises(KnowledgeReviewUnavailable):
            self.publication.publish(
                self.principal,
                (self.batch.mentions[0].revision_id,),
                expected_active_publication_id=None,
                published_at=PUBLISHED_AT,
            )
        approved_ids = self._approve_all()
        wrong_group = Principal(
            "outsider",
            self.tenant_id,
            frozenset({"legal"}),
            frozenset({KNOWLEDGE_PUBLISH_CAPABILITY}),
        )
        wrong_tenant = Principal(
            "outsider",
            "another-tenant",
            frozenset({"finance-readers"}),
            frozenset({KNOWLEDGE_PUBLISH_CAPABILITY}),
        )
        for unauthorized in (wrong_group, wrong_tenant):
            with self.assertRaises(KnowledgeReviewUnavailable):
                self.publication.publish(
                    unauthorized,
                    approved_ids,
                    expected_active_publication_id=None,
                    published_at=PUBLISHED_AT,
                )
        publication_count, _, _ = self.driver.execute_query(
            "MATCH (publication:KnowledgePublication) "
            "RETURN count(publication) AS count",
            database_=self.database,
        )
        self.assertEqual(publication_count[0]["count"], 0)
        with self.assertRaises(KnowledgePublicationConflict):
            self.publication.publish(
                self.principal,
                (approved_ids[-1],),
                expected_active_publication_id=None,
                published_at=PUBLISHED_AT,
            )
        view = self.publication.publish(
            self.principal,
            approved_ids,
            expected_active_publication_id=None,
            published_at=PUBLISHED_AT,
        )
        self.assertEqual(view.status, "ACTIVE")
        self.assertEqual(view.generation, 1)
        self.assertEqual(view.source_revision_ids, tuple(sorted(approved_ids)))
        self.assertEqual(len(view.published_revision_ids), 3)
        self.assertEqual(self.publication.active(self.principal), view)
        self.assertEqual(
            self.publication.publish(
                self.principal,
                approved_ids,
                expected_active_publication_id=None,
                published_at=PUBLISHED_AT,
            ),
            view,
        )
        self.assertIsNone(self.publication.get(wrong_group, view.publication_id))
        self.assertIsNone(self.publication.active(wrong_group))

        rows, _, _ = self.driver.execute_query(
            """
            MATCH (publication:KnowledgePublication {
                publication_id: $publication_id
            })-[:PUBLISHES_KNOWLEDGE_REVISION]->(revision)
            MATCH (snapshot:KnowledgeSnapshot)
                  -[:INCLUDES_CHUNK]->(chunk:Chunk)
            WHERE chunk.chunk_id = revision.chunk_id
            OPTIONAL MATCH (snapshot)-[
                mention_membership:INCLUDES_MENTION {
                    governed_publication_id: $publication_id
                }
            ]->(mention:EntityMention)
            OPTIONAL MATCH (snapshot)-[
                assertion_membership:INCLUDES_ASSERTION {
                    governed_publication_id: $publication_id
                }
            ]->(assertion:Assertion)
            RETURN count(DISTINCT revision) AS revisions,
                   count(DISTINCT mention) AS mentions,
                   count(DISTINCT assertion) AS assertions,
                   collect(DISTINCT mention.mention_id) AS mention_ids,
                   collect(DISTINCT assertion.assertion_id) AS assertion_ids,
                   collect(DISTINCT revision.authority_level) AS authorities,
                   collect(DISTINCT revision.governance_status) AS statuses
            """,
            publication_id=view.publication_id,
            database_=self.database,
        )
        self.assertEqual(rows[0]["revisions"], 3)
        self.assertEqual(rows[0]["mentions"], 2)
        self.assertEqual(rows[0]["assertions"], 1)
        expected_mention_ids = {
            mention_id(
                mention.evidence.chunk_id,
                mention.entity.entity_type,
                mention.evidence.char_start,
                mention.evidence.char_end,
                mention.surface,
                mention.trust.extractor_version or "",
            )
            for mention in self.batch.mentions
        }
        self.assertEqual(set(rows[0]["mention_ids"]), expected_mention_ids)
        source_assertion = self.batch.assertions[0]
        authoritative_assertion = self.authoritative_batch.assertions[0]
        expected_assertion_id = assertion_id(
            self.tenant_id,
            authoritative_assertion.subject.entity_id,
            source_assertion.predicate,
            source_assertion.object_kind,
            authoritative_assertion.object_entity.entity_id,  # type: ignore[union-attr]
            source_assertion.evidence.chunk_id,
            source_assertion.evidence.char_start,
            source_assertion.evidence.char_end,
            source_assertion.trust.extractor_version or "",
            self.tbox_id,
        )
        self.assertEqual(rows[0]["assertion_ids"], [expected_assertion_id])
        self.assertEqual(rows[0]["authorities"], ["SECONDARY"])
        self.assertEqual(rows[0]["statuses"], ["PUBLISHED"])

        with self.assertRaises(KnowledgePublicationConflict):
            self.publication.publish(
                self.principal,
                approved_ids,
                expected_active_publication_id="stale-publication",
                published_at=PUBLISHED_AT,
            )

    def test_incremental_publication_carries_endpoints_and_supports_removal(
        self,
    ) -> None:
        mention_outcomes = self.review.review_batch(
            self.principal,
            tuple(
                ReviewRequest(
                    ReviewRecordKind.ENTITY_MENTION,
                    candidate.record_id,
                    1,
                    GovernanceStatus.APPROVED,
                    REVIEWED_AT,
                    "Endpoint identity verified.",
                    MentionEdit(authoritative.entity, candidate.confidence),
                )
                for candidate, authoritative in zip(
                    self.batch.mentions,
                    self.authoritative_batch.mentions,
                    strict=True,
                )
            ),
        ).outcomes
        first = self.publication.publish(
            self.principal,
            tuple(outcome.revision_id for outcome in mention_outcomes),
            expected_active_publication_id=None,
            published_at=PUBLISHED_AT,
        )
        self.assertEqual(len(first.published_revision_ids), 2)

        candidate = self.batch.assertions[0]
        authoritative = self.authoritative_batch.assertions[0]
        assertion = self.review.approve(
            self.principal,
            record_kind=ReviewRecordKind.ASSERTION,
            record_id=candidate.record_id,
            expected_revision=1,
            reviewed_at=REVIEWED_AT,
            notes="Relationship and both published endpoints verified.",
            edit=AssertionEdit(
                authoritative.subject,
                authoritative.predicate,
                candidate.subject_mention_revision_id,
                candidate.confidence,
                object_entity=authoritative.object_entity,
                object_mention_revision_id=(
                    candidate.object_mention_revision_id
                ),
            ),
        )
        second = self.publication.publish(
            self.principal,
            (assertion.revision_id,),
            expected_active_publication_id=first.publication_id,
            published_at=PUBLISHED_AT + timedelta(minutes=1),
        )
        self.assertEqual(second.source_revision_ids, (assertion.revision_id,))
        self.assertEqual(len(second.published_revision_ids), 3)
        self.assertTrue(
            set(first.published_revision_ids)
            < set(second.published_revision_ids)
        )

        counts, _, _ = self.driver.execute_query(
            """
            MATCH (snapshot:KnowledgeSnapshot {tenant_id: $tenant_id})
            OPTIONAL MATCH (snapshot)-[:INCLUDES_MENTION {
                governed_publication_id: $publication_id
            }]->(mention:EntityMention)
            OPTIONAL MATCH (snapshot)-[:INCLUDES_ASSERTION {
                governed_publication_id: $publication_id
            }]->(assertion:Assertion)
            RETURN count(DISTINCT mention) AS mentions,
                   count(DISTINCT assertion) AS assertions
            """,
            tenant_id=self.tenant_id,
            publication_id=second.publication_id,
            database_=self.database,
        )
        self.assertEqual(dict(counts[0]), {"mentions": 2, "assertions": 1})

        third = self.publication.publish(
            self.principal,
            (),
            expected_active_publication_id=second.publication_id,
            published_at=PUBLISHED_AT + timedelta(minutes=2),
            remove_record_ids=(candidate.record_id,),
        )
        self.assertEqual(third.source_revision_ids, ())
        self.assertEqual(third.removed_record_ids, (candidate.record_id,))
        self.assertEqual(
            set(third.published_revision_ids),
            set(first.published_revision_ids),
        )

    def test_authoritative_and_secondary_records_coexist_and_replace_explicitly(
        self,
    ) -> None:
        authoritative = self._distinct_authoritative_batch()
        self.store.import_authoritative(authoritative)
        candidate_first = self.batch.mentions[0]
        first_approved = self.review.approve(
            self.principal,
            record_kind=ReviewRecordKind.ENTITY_MENTION,
            record_id=candidate_first.record_id,
            expected_revision=1,
            reviewed_at=REVIEWED_AT,
            notes="Secondary company mention verified.",
            edit=MentionEdit(
                self.authoritative_batch.mentions[0].entity,
                candidate_first.confidence,
            ),
        )
        authoritative_ids = tuple(
            record.revision_id
            for record in (*authoritative.mentions, *authoritative.assertions)
        )
        first = self.publication.publish(
            self.principal,
            (*authoritative_ids, first_approved.revision_id),
            expected_active_publication_id=None,
            published_at=PUBLISHED_AT,
        )
        self.assertEqual(len(first.published_revision_ids), 4)

        authorities, _, _ = self.driver.execute_query(
            """
            MATCH (:KnowledgePublication {publication_id: $publication_id})
                  -[:PUBLISHES_KNOWLEDGE_REVISION]->(revision)
            RETURN collect(DISTINCT revision.authority_level) AS values
            """,
            publication_id=first.publication_id,
            database_=self.database,
        )
        self.assertEqual(
            set(authorities[0]["values"]),
            {"AUTHORITATIVE", "SECONDARY"},
        )

        candidate_second = self.batch.mentions[1]
        second_approved = self.review.approve(
            self.principal,
            record_kind=ReviewRecordKind.ENTITY_MENTION,
            record_id=candidate_second.record_id,
            expected_revision=1,
            reviewed_at=REVIEWED_AT,
            notes="Secondary product mention verified.",
            edit=MentionEdit(
                self.authoritative_batch.mentions[1].entity,
                candidate_second.confidence,
            ),
        )
        second = self.publication.publish(
            self.principal,
            (second_approved.revision_id,),
            expected_active_publication_id=first.publication_id,
            published_at=PUBLISHED_AT + timedelta(minutes=1),
            replace_record_ids=(candidate_first.record_id,),
        )
        self.assertEqual(
            second.replaced_record_ids,
            (candidate_first.record_id,),
        )
        self.assertEqual(len(second.published_revision_ids), 4)
        self.assertTrue(set(authoritative_ids) <= set(second.published_revision_ids))
        self.assertNotIn(
            next(
                revision_id
                for revision_id in first.published_revision_ids
                if revision_id not in authoritative_ids
            ),
            second.published_revision_ids,
        )

    def test_acl_revocation_hides_manifest_and_blocks_replay_or_rollback(self) -> None:
        approved_ids = self._approve_all()
        view = self.publication.publish(
            self.principal,
            approved_ids,
            expected_active_publication_id=None,
            published_at=PUBLISHED_AT,
        )
        self.assertEqual(
            self.publication.get(self.principal, view.publication_id),
            view,
        )

        self.driver.execute_query(
            """
            MATCH (document:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })-[:ACTIVE_SNAPSHOT]->(:KnowledgeSnapshot)
              -[:INCLUDES_CHUNK]->(chunk:Chunk {tenant_id: $tenant_id})
            SET document.access_policy_id = 'revoked-policy',
                document.access_policy_version = 2,
                document.access_groups = ['legal'],
                chunk.access_policy_id = 'revoked-policy',
                chunk.access_policy_version = 2,
                chunk.access_groups = ['legal']
            """,
            tenant_id=self.tenant_id,
            document_id=self.bundle.document.document_id,
            database_=self.database,
        )
        newly_authorized = Principal(
            "lawyer:bob",
            self.tenant_id,
            frozenset({"legal"}),
            frozenset({KNOWLEDGE_PUBLISH_CAPABILITY}),
        )
        for scoped_principal in (self.principal, newly_authorized):
            self.assertIsNone(
                self.publication.get(scoped_principal, view.publication_id)
            )
            self.assertIsNone(self.publication.active(scoped_principal))
            self.assertEqual(self.publication.history(scoped_principal), ())

        with self.assertRaises(KnowledgeReviewUnavailable):
            self.publication.publish(
                self.principal,
                approved_ids,
                expected_active_publication_id=None,
                published_at=PUBLISHED_AT,
            )
        with self.assertRaises(KnowledgeReviewUnavailable):
            self.publication.rollback(
                self.principal,
                view.publication_id,
                expected_active_publication_id=view.publication_id,
                rolled_back_at=PUBLISHED_AT + timedelta(minutes=1),
            )
        raw_state, _, _ = self.driver.execute_query(
            """
            MATCH (:KnowledgePublicationState {tenant_id: $tenant_id})
                  -[:ACTIVE_KNOWLEDGE_PUBLICATION]->(publication)
            RETURN publication.publication_id AS publication_id,
                   publication.status AS status
            """,
            tenant_id=self.tenant_id,
            database_=self.database,
        )
        self.assertEqual(
            dict(raw_state[0]),
            {"publication_id": view.publication_id, "status": "ACTIVE"},
        )

    def test_rollback_atomically_reactivates_manifest_without_deleting_audit(
        self,
    ) -> None:
        first = self.batch.mentions[0]
        second = self.batch.mentions[1]
        first_approved = self.review.approve(
            self.principal,
            record_kind=ReviewRecordKind.ENTITY_MENTION,
            record_id=first.record_id,
            expected_revision=1,
            reviewed_at=REVIEWED_AT,
            notes="First entity approved.",
            edit=MentionEdit(
                self.authoritative_batch.mentions[0].entity,
                first.confidence,
            ),
        )
        second_approved = self.review.approve(
            self.principal,
            record_kind=ReviewRecordKind.ENTITY_MENTION,
            record_id=second.record_id,
            expected_revision=1,
            reviewed_at=REVIEWED_AT,
            notes="Second entity approved.",
            edit=MentionEdit(
                self.authoritative_batch.mentions[1].entity,
                second.confidence,
            ),
        )
        publication_one = self.publication.publish(
            self.principal,
            (first_approved.revision_id,),
            expected_active_publication_id=None,
            published_at=PUBLISHED_AT,
        )
        publication_two = self.publication.publish(
            self.principal,
            (second_approved.revision_id,),
            expected_active_publication_id=publication_one.publication_id,
            published_at=PUBLISHED_AT + timedelta(minutes=1),
        )
        wrong_group = Principal(
            "outsider",
            self.tenant_id,
            frozenset({"legal"}),
            frozenset({KNOWLEDGE_PUBLISH_CAPABILITY}),
        )
        wrong_tenant = Principal(
            "outsider",
            "another-tenant",
            frozenset({"finance-readers"}),
            frozenset({KNOWLEDGE_PUBLISH_CAPABILITY}),
        )
        with self.assertRaises(KnowledgeReviewUnavailable):
            self.publication.rollback(
                wrong_group,
                publication_two.publication_id,
                expected_active_publication_id=publication_two.publication_id,
                rolled_back_at=PUBLISHED_AT + timedelta(minutes=2),
            )
        with self.assertRaises(KnowledgeReviewUnavailable):
            self.publication.rollback(
                wrong_group,
                publication_one.publication_id,
                expected_active_publication_id=publication_two.publication_id,
                rolled_back_at=PUBLISHED_AT + timedelta(minutes=2),
            )
        with self.assertRaises(KnowledgePublicationConflict):
            self.publication.rollback(
                wrong_tenant,
                publication_one.publication_id,
                expected_active_publication_id=publication_two.publication_id,
                rolled_back_at=PUBLISHED_AT + timedelta(minutes=2),
            )
        self.assertEqual(
            self.publication.active(
                self.principal
            ).publication_id,  # type: ignore[union-attr]
            publication_two.publication_id,
        )

        with self.assertRaises(KnowledgePublicationConflict):
            self.publication.rollback(
                self.principal,
                publication_one.publication_id,
                expected_active_publication_id="stale-publication",
                rolled_back_at=PUBLISHED_AT + timedelta(minutes=2),
            )
        restored = self.publication.rollback(
            self.principal,
            publication_one.publication_id,
            expected_active_publication_id=publication_two.publication_id,
            rolled_back_at=PUBLISHED_AT + timedelta(minutes=2),
        )
        self.assertEqual(restored.status, "ACTIVE")
        self.assertEqual(
            self.publication.active(
                self.principal
            ).publication_id,  # type: ignore[union-attr]
            publication_one.publication_id,
        )
        self.assertEqual(len(self.publication.history(self.principal)), 2)

        counts, _, _ = self.driver.execute_query(
            """
            MATCH (activation:KnowledgePublicationActivation {
                tenant_id: $tenant_id
            })
            WITH count(activation) AS activations
            MATCH (revision)
            WHERE revision:GovernedEntityMentionRevision
               OR revision:GovernedAssertionRevision
            RETURN activations,
                   count(revision) AS audit_revisions
            """,
            tenant_id=self.tenant_id,
            database_=self.database,
        )
        self.assertEqual(counts[0]["activations"], 3)
        self.assertEqual(counts[0]["audit_revisions"], 7)


if __name__ == "__main__":
    unittest.main()
