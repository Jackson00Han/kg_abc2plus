"""Disposable-Neo4j checks for review, publication, and rollback."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
import ipaddress
import json
import os
import unittest
from urllib.parse import urlparse

import neo4j

from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import (
    assertion_id,
    mention_id,
    relationship_property_value_id,
)
from graphrag_prod.domain.models import RelationshipPropertyValue, TypedLiteralValue
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
    HierarchyDefinition,
    HierarchyKind,
    Neo4jTBoxStore,
    Cardinality,
    PropertyDataType,
    PropertyDefinition,
    RelationshipTypeDefinition,
    TBoxStatus,
    TBoxVersion,
)
from graphrag_prod.retrieval import Neo4jEvidenceSubgraphProjector
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
                    ("ticker", "llm-candidate"),
                    properties=(
                        PropertyDefinition(
                            "DISPLAY_NAME",
                            PropertyDataType.STRING,
                            False,
                            Cardinality.ZERO_OR_ONE,
                        ),
                    ),
                ),
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
                ),
                RelationshipTypeDefinition(
                    "QUALIFIED_OFFERS",
                    ("Company",),
                    ("Product",),
                    properties=(
                        PropertyDefinition(
                            "BASIS",
                            PropertyDataType.STRING,
                            True,
                            Cardinality.ONE,
                        ),
                    ),
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
        self.tbox = tbox
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

    def test_hierarchy_cycle_rejects_complete_publication_atomically_then_valid_set_publishes(self) -> None:
        # This deliberately malformed approved graph exercises structural
        # publication rejection. It does not claim the reverse source relation
        # is semantically entailed or represent an industrial diagnostic rule.
        tbox = dataclasses.replace(
            self.tbox, version=2,
            relationship_types=(RelationshipTypeDefinition(
                "OFFERS", ("Company", "Product"), ("Company", "Product"),
            ),),
            hierarchies=(HierarchyDefinition(
                "TestComposition", "OFFERS", HierarchyKind.COMPOSITION,
                ("Company", "Product"),
            ),),
        )
        tboxes = Neo4jTBoxStore(self.driver, self.database)
        tboxes.import_version(tbox)
        tboxes.publish(self.tenant_id, tbox.tbox_id, expected_active_tbox_id=self.tbox_id)
        batch = self._distinct_authoritative_batch()
        mentions = tuple(dataclasses.replace(
            item, trust=dataclasses.replace(item.trust, ontology_version_id=tbox.tbox_id),
        ) for item in batch.mentions)
        forward = dataclasses.replace(
            batch.assertions[0], trust=dataclasses.replace(batch.assertions[0].trust, ontology_version_id=tbox.tbox_id),
        )
        reverse = dataclasses.replace(
            forward,
            revision=RecordRevision.next(knowledge_record_id(self.tenant_id, "ASSERTION", "hierarchy-cycle"), 0),
            subject=forward.object_entity, object_entity=forward.subject,
            subject_mention_revision_id=forward.object_mention_revision_id,
            object_mention_revision_id=forward.subject_mention_revision_id,
        )
        malformed = ABoxRecordBatch(self.tenant_id, mentions, (forward, reverse))
        self.store.import_authoritative(malformed)
        with self.assertRaisesRegex(KnowledgePublicationConflict, "hierarchy"):
            self.publication.publish(
                self.principal,
                tuple(item.revision_id for item in (*mentions, forward, reverse)),
                expected_active_publication_id=None, published_at=PUBLISHED_AT,
            )
        self.assertEqual(self.publication.history(self.principal), ())
        rows, _, _ = self.driver.execute_query(
            "MATCH (revision:GovernedAssertionRevision {record_id: $record_id}) "
            "RETURN collect(revision.governance_status) AS statuses",
            record_id=forward.record_id, database_=self.database,
        )
        self.assertEqual(list(rows[0]["statuses"]), ["PUBLISHED"])
        valid_ids = tuple(item.revision_id for item in (*mentions, forward))
        valid = self.publication.publish(
            self.principal, valid_ids,
            expected_active_publication_id=None, published_at=PUBLISHED_AT,
        )
        self.assertEqual(len(valid.published_revision_ids), 3)
        self.assertEqual(self.publication.publish(
            self.principal, valid_ids, expected_active_publication_id=None,
            published_at=PUBLISHED_AT,
        ), valid)

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

    def _prepare_review_assessment(self):
        from graphrag_prod.knowledge.review_assessment import Neo4jReviewAssessmentService

        service = Neo4jReviewAssessmentService(self.driver, self.database)
        assertion = self.batch.assertions[0]
        blocked = service.assess(self.principal, assertion.record_id, 1)
        self.assertEqual(blocked.status, "BLOCKED")
        self.assertEqual(len(blocked.dependencies), 2)
        self.assertFalse(any(value.ready for value in blocked.dependencies))
        authoritative = self._distinct_authoritative_batch()
        self.store.import_authoritative(authoritative)
        publication = self.publication.publish(
            self.principal,
            tuple(record.revision_id for record in (*authoritative.mentions, *authoritative.assertions)),
            expected_active_publication_id=None, published_at=PUBLISHED_AT,
        )
        for mention, target in zip(self.batch.mentions, self.authoritative_batch.mentions, strict=True):
            self.review.apply_entity_resolution(
                self.principal, record_id=mention.record_id, expected_revision=1,
                target=target.entity, reviewed_at=REVIEWED_AT, notes="Confirm source identity.",
            )
        current = self.store.get_assertion(self.principal, assertion.record_id, statuses=(GovernanceStatus.CANDIDATE,))
        result = service.assess(self.principal, current.record_id, current.revision.revision)
        self.assertEqual(result.status, "DUPLICATE")
        self.assertTrue(all(value.ready for value in result.dependencies))
        self.assertEqual(len(result.matches), 1)
        self.assertEqual(result.matches[0].publication_id, publication.publication_id)
        return service, current, result, publication

    def test_review_assessment_requires_endpoints_and_duplicate_dismissal_retains_audit(self):
        service, current, result, publication = self._prepare_review_assessment()
        before, _, _ = self.driver.execute_query("MATCH (n) RETURN count(n) AS count", database_=self.database)
        self.assertEqual(service.assess(self.principal, current.record_id, current.revision.revision), result)
        after, _, _ = self.driver.execute_query("MATCH (n) RETURN count(n) AS count", database_=self.database)
        self.assertEqual(before[0]["count"], after[0]["count"])
        request = ReviewRequest(
            ReviewRecordKind.ASSERTION, current.record_id, current.revision.revision,
            GovernanceStatus.REJECTED, REVIEWED_AT, "Keep the existing fact and this source audit.",
            duplicate_of_revision_id=result.matches[0].record.revision_id,
        )
        outcome = self.review.review_batch(self.principal, (request,)).outcomes[0]
        self.assertEqual(outcome.status, GovernanceStatus.REJECTED)
        history = self.review.revision_history(self.principal, current.record_id)
        self.assertIn("KEEP_EXISTING_AUTHORITATIVE_FACT", history[0].record.trust.review_notes)
        self.assertIn(result.matches[0].record.revision_id, history[0].record.trust.review_notes)
        self.assertEqual(history[0].record.evidence, current.evidence)
        self.assertEqual(history[-1].record.trust.status, GovernanceStatus.CANDIDATE)
        self.assertEqual(self.publication.active(self.principal).publication_id, publication.publication_id)
        with self.assertRaises(KnowledgeConflict):
            self.review.review_batch(self.principal, (request,))

    def test_review_assessment_hides_other_acl_and_rechecks_removed_authority(self):
        service, current, result, publication = self._prepare_review_assessment()
        for principal in (
            dataclasses.replace(self.principal, groups=frozenset({"legal"})),
            dataclasses.replace(self.principal, tenant_id="another-tenant"),
        ):
            with self.subTest(principal=principal.principal_id), self.assertRaises(KnowledgeReviewUnavailable):
                service.assess(principal, current.record_id, current.revision.revision)
        with self.assertRaises(KnowledgeConflict):
            service.assess(self.principal, current.record_id, 1)
        authority = result.matches[0].record
        self.driver.execute_query(
            "MATCH (r:GovernedAssertionRevision {revision_id: $revision_id}) SET r.access_groups = ['legal']",
            revision_id=authority.revision_id, database_=self.database,
        )
        hidden = service.assess(self.principal, current.record_id, current.revision.revision)
        self.assertEqual(hidden.matches, ())
        self.driver.execute_query(
            "MATCH (r:GovernedAssertionRevision {revision_id: $revision_id}) SET r.access_groups = $groups",
            revision_id=authority.revision_id, groups=sorted(authority.evidence.access_groups), database_=self.database,
        )
        self.review.quarantine(
            self.principal, record_kind=ReviewRecordKind.ASSERTION,
            record_id=authority.record_id, expected_revision=authority.revision.revision,
            reviewed_at=REVIEWED_AT, notes="Pending expert re-evaluation, active publication unchanged.",
        )
        # A later record head does not silently remove the fact from the
        # currently active publication. Its immutable revision still compares.
        still_active = service.assess(self.principal, current.record_id, current.revision.revision)
        self.assertEqual(still_active.status, "DUPLICATE")
        self.assertEqual(still_active.matches[0].record.revision_id, authority.revision_id)
        self.publication.publish(
            self.principal, (), expected_active_publication_id=publication.publication_id,
            published_at=PUBLISHED_AT + timedelta(minutes=1),
            remove_record_ids=(result.matches[0].record.record_id,),
        )
        self.assertEqual(service.assess(self.principal, current.record_id, current.revision.revision).status, "READY")
        request = ReviewRequest(
            ReviewRecordKind.ASSERTION, current.record_id, current.revision.revision,
            GovernanceStatus.REJECTED, REVIEWED_AT, "Previously duplicate.",
            duplicate_of_revision_id=result.matches[0].record.revision_id,
        )
        with self.assertRaises(KnowledgeConflict):
            self.review.review_batch(self.principal, (request,))
        self.assertEqual(self.store.get_assertion(
            self.principal, current.record_id, statuses=(GovernanceStatus.CANDIDATE,),
        ).revision_id, current.revision_id)

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

        assertion = self.batch.assertions[0]
        edit = AssertionEdit(
            assertion.subject, assertion.predicate, assertion.subject_mention_revision_id,
            0.96, object_entity=assertion.object_entity,
            object_mention_revision_id=assertion.object_mention_revision_id,
        )
        first_request = ReviewRequest(
            ReviewRecordKind.ASSERTION, assertion.record_id, 1,
            GovernanceStatus.QUARANTINED, REVIEWED_AT,
            "First explicit correction; inspect again before approval.", edit,
        )
        self.review.review_batch(self.principal, (first_request,))
        second_request = dataclasses.replace(first_request, expected_revision=2,
                                             notes="Second explicit correction.", edit=dataclasses.replace(edit, confidence=0.94))
        self.review.review_batch(self.principal, (second_request,))
        history = self.review.revision_history(self.principal, assertion.record_id)
        self.assertEqual([item.record.revision.revision for item in history], [3, 2, 1])
        self.assertEqual(history[0].record.trust.status, GovernanceStatus.QUARANTINED)
        self.assertEqual(history[0].record.confidence, 0.94)
        self.assertEqual(history[0].record.trust.review_notes, second_request.notes)
        self.assertEqual(history[1].record.trust.review_notes, first_request.notes)
        self.assertTrue(all(item.record.evidence == assertion.evidence for item in history))
        self.assertEqual(history[0].record.trust.authority, AuthorityLevel.SECONDARY)
        with self.assertRaises(KnowledgeConflict):
            self.review.review_batch(self.principal, (second_request,))
        current_request = dataclasses.replace(second_request, expected_revision=3)
        with self.assertRaisesRegex(ValueError, "illegal governance transition"):
            self.review.review_batch(self.principal, (dataclasses.replace(current_request, edit=None),))
        for invalid_edit in (
            dataclasses.replace(edit, predicate="UNDECLARED"),
            dataclasses.replace(edit, subject=assertion.object_entity),
        ):
            with self.subTest(predicate=invalid_edit.predicate, entity_type=invalid_edit.subject.entity_type):
                with self.assertRaises(KnowledgeReviewUnavailable):
                    self.review.review_batch(self.principal, (dataclasses.replace(current_request, edit=invalid_edit),))
        self.assertEqual(self.store.get_assertion(
            self.principal, assertion.record_id, statuses=(GovernanceStatus.QUARANTINED,),
        ).revision.revision, 3)
        approved_mention = self.store.get_entity_mention(
            self.principal, mention.record_id, statuses=(GovernanceStatus.APPROVED,),
        )
        isolated = self.review.quarantine(
            self.principal, record_kind=ReviewRecordKind.ENTITY_MENTION,
            record_id=mention.record_id, expected_revision=approved_mention.revision.revision,
            reviewed_at=REVIEWED_AT, notes="Recheck entity details.",
        )
        mention_request = ReviewRequest(
            ReviewRecordKind.ENTITY_MENTION, mention.record_id, isolated.revision,
            GovernanceStatus.QUARANTINED, REVIEWED_AT, "Correct isolated entity details.",
            MentionEdit(approved_mention.entity, 0.92),
        )
        self.review.review_batch(self.principal, (mention_request,))
        corrected = self.store.get_entity_mention(
            self.principal, mention.record_id, statuses=(GovernanceStatus.QUARANTINED,),
        )
        self.assertEqual(corrected.revision.revision, isolated.revision + 1)
        self.assertEqual(corrected.confidence, 0.92)
        self.assertEqual(corrected.evidence, approved_mention.evidence)
        self.assertEqual(corrected.trust.review_notes, mention_request.notes)

    def test_entity_resolution_atomically_rebinds_both_assertion_endpoints(
        self,
    ) -> None:
        assertion = self.batch.assertions[0]
        subject_mention = next(
            item
            for item in self.batch.mentions
            if item.revision_id == assertion.subject_mention_revision_id
        )
        object_mention = next(
            item
            for item in self.batch.mentions
            if item.revision_id == assertion.object_mention_revision_id
        )
        subject_target = next(
            item.entity
            for item in self.authoritative_batch.mentions
            if item.entity.entity_type == subject_mention.entity.entity_type
        )
        object_target = next(
            item.entity
            for item in self.authoritative_batch.mentions
            if item.entity.entity_type == object_mention.entity.entity_type
        )

        subject_result = self.review.apply_entity_resolution(
            self.principal,
            record_id=subject_mention.record_id,
            expected_revision=1,
            target=subject_target,
            reviewed_at=REVIEWED_AT,
            notes="Exact authoritative subject identity verified.",
        )
        object_result = self.review.apply_entity_resolution(
            self.principal,
            record_id=object_mention.record_id,
            expected_revision=1,
            target=object_target,
            reviewed_at=REVIEWED_AT,
            notes="Exact authoritative object identity verified.",
        )

        self.assertEqual(
            [item.status for item in subject_result.outcomes],
            [GovernanceStatus.APPROVED, GovernanceStatus.CANDIDATE],
        )
        self.assertEqual(
            [item.status for item in object_result.outcomes],
            [GovernanceStatus.APPROVED, GovernanceStatus.CANDIDATE],
        )
        linked_subject = self.store.get_entity_mention(
            self.principal,
            subject_mention.record_id,
            statuses=(GovernanceStatus.APPROVED,),
        )
        linked_object = self.store.get_entity_mention(
            self.principal,
            object_mention.record_id,
            statuses=(GovernanceStatus.APPROVED,),
        )
        rebound = self.store.get_assertion(
            self.principal,
            assertion.record_id,
            statuses=(GovernanceStatus.CANDIDATE,),
        )
        assert linked_subject is not None
        assert linked_object is not None
        assert rebound is not None
        self.assertEqual(linked_subject.entity, subject_target)
        self.assertEqual(linked_object.entity, object_target)
        self.assertEqual(rebound.subject, subject_target)
        self.assertEqual(rebound.object_entity, object_target)
        self.assertEqual(
            rebound.subject_mention_revision_id,
            linked_subject.revision_id,
        )
        self.assertEqual(
            rebound.object_mention_revision_id,
            linked_object.revision_id,
        )
        self.assertEqual(rebound.revision.revision, 3)
        self.assertEqual(rebound.trust.status, GovernanceStatus.CANDIDATE)

        rows, _, _ = self.driver.execute_query(
            """
            MATCH (head:KnowledgeRecordHead {
                tenant_id: $tenant_id,
                record_id: $record_id,
                record_kind: 'ASSERTION'
            })-[:CURRENT_REVISION]->(current:GovernedAssertionRevision)
            MATCH (current)-[:SUPERSEDES]->(second)-[:SUPERSEDES]->(original)
            RETURN current.revision AS current_revision,
                   second.revision AS second_revision,
                   original.revision AS original_revision,
                   current.governance_status AS current_status
            """,
            tenant_id=self.tenant_id,
            record_id=assertion.record_id,
            database_=self.database,
        )
        self.assertEqual(
            dict(rows[0]),
            {
                "current_revision": 3,
                "second_revision": 2,
                "original_revision": 1,
                "current_status": "CANDIDATE",
            },
        )
        with self.assertRaises(KnowledgeConflict):
            self.review.apply_entity_resolution(
                self.principal,
                record_id=subject_mention.record_id,
                expected_revision=1,
                target=subject_target,
                reviewed_at=REVIEWED_AT,
                notes="Stale entity-resolution retry.",
            )

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

    def _database_snapshot(self):
        from graphrag_prod.knowledge.publication_preview import json_value
        nodes, _, _ = self.driver.execute_query(
            "MATCH (n) RETURN elementId(n) AS id, labels(n) AS labels, properties(n) AS properties ORDER BY id",
            database_=self.database,
        )
        edges, _, _ = self.driver.execute_query(
            "MATCH (a)-[r]->(b) RETURN elementId(r) AS id, elementId(a) AS source, "
            "elementId(b) AS target, type(r) AS type, properties(r) AS properties ORDER BY id",
            database_=self.database,
        )
        return json_value([list(map(dict, nodes)), list(map(dict, edges))])

    def test_confirmed_unpublished_identity_accepts_other_mentions_without_publishing(self):
        from graphrag_prod.domain.ids import entity_id
        from graphrag_prod.api.knowledge_contracts import ConfirmedResolutionTarget

        original = self.batch.mentions[0]
        self.review.approve(self.principal, record_kind=ReviewRecordKind.ENTITY_MENTION,
            record_id=original.record_id, expected_revision=1,
            reviewed_at=REVIEWED_AT, notes='Confirm first mention identity.')
        target = self.store.get_entity_mention(self.principal, original.record_id,
            statuses=(GovernanceStatus.APPROVED,))
        candidates = []
        for index in range(2):
            key = f'llm-candidate:additional-{index}'
            mention = dataclasses.replace(original,
                revision=RecordRevision.next(f'additional-mention-{index}', 0),
                entity=dataclasses.replace(original.entity, canonical_key=key,
                    entity_id=entity_id(self.tenant_id, original.entity.entity_type, key)))
            self.store.persist_llm_candidates(ABoxRecordBatch(self.tenant_id, (mention,), ()))
            candidates.append(mention)
        for candidate in candidates:
            response = self.review.resolution_targets(self.principal, candidate, target.entity.canonical_name)
            selected = next(row for row in response['items']
                if row['entity']['entity_id'] == target.entity.entity_id)
            ConfirmedResolutionTarget.model_validate(selected)
            self.assertTrue(selected['selectable'])
            self.assertEqual(selected['status'], 'APPROVED')
            outcome = self.review.apply_entity_resolution(self.principal,
                record_id=candidate.record_id, expected_revision=1, target=target.entity,
                target_record_id=selected['record_id'], target_expected_revision=selected['revision'],
                reviewed_at=REVIEWED_AT, notes='Context confirms the same entity.')
            self.assertEqual(outcome.outcomes[0].status, GovernanceStatus.APPROVED)
            linked = self.store.get_entity_mention(self.principal, candidate.record_id,
                statuses=(GovernanceStatus.APPROVED,))
            self.assertEqual(linked.entity.entity_id, target.entity.entity_id)
            self.assertEqual(linked.evidence, candidate.evidence)
            self.assertEqual(linked.trust.authority, candidate.trust.authority)
        self.assertEqual(self.publication.history(self.principal), ())
        self.assertEqual(self.store.get_assertion(self.principal, self.batch.assertions[0].record_id,
            statuses=(GovernanceStatus.CANDIDATE,)).trust.status, GovernanceStatus.CANDIDATE)
        outsider = dataclasses.replace(self.principal, groups=frozenset({'other'}))
        self.assertEqual(self.review.resolution_targets(outsider, candidates[0])['items'], [])
        fresh = dataclasses.replace(candidates[0], revision=RecordRevision.next('stale-target-source', 0))
        self.store.persist_llm_candidates(ABoxRecordBatch(self.tenant_id, (fresh,), ()))
        self.assertEqual(len(self.review.resolution_targets(self.principal, fresh)['items']), 1)
        # An identity fact from the original source must still block a conflict
        # when the deduplicated target is represented by a later linked source.
        self.driver.execute_query(
            'MATCH (t:TBoxVersion {tbox_id:$tbox_id})-[:DECLARES_ENTITY_TYPE]->'
            '(d:TBoxEntityType {name:"Company"}) SET d.identity_properties=["DISPLAY_NAME"]',
            tbox_id=self.tbox_id, database_=self.database)
        identity_facts = []
        for mention, value in ((target, 'Apple'), (fresh, 'iPhone')):
            identity_facts.append(dataclasses.replace(self.batch.assertions[0],
                revision=RecordRevision.next(f'identity:{mention.record_id}', 0),
                subject=mention.entity, subject_mention_revision_id=mention.revision_id,
                predicate='DISPLAY_NAME', object_entity=None, object_mention_revision_id=None,
                literal_value=value, literal_semantics=TypedLiteralValue(
                    datatype='STRING', typed_value=value, raw_value=value, canonical_value=value)))
        def seed_identity_facts(tx):
            for fact in identity_facts:
                self.store._validate_evidence_tx(tx, fact.evidence, origin=fact.trust.origin)
                self.store._lock_head_tx(tx, fact.revision, self.tenant_id, 'ASSERTION', fact.created_at)
                self.store._create_assertion_revision_tx(tx, fact, link_canonical_entities=False)
        with self.driver.session(database=self.database) as session:
            session.execute_write(seed_identity_facts)
        conflicting = self.review.resolution_targets(self.principal, fresh)['items'][0]
        self.assertNotEqual(conflicting['record_id'], target.record_id)
        self.assertFalse(conflicting['selectable'])
        self.assertEqual(conflicting['identity_properties'][0]['value'], 'Apple')
        before = self._database_snapshot()
        with self.assertRaises(KnowledgeConflict):
            self.review.apply_entity_resolution(self.principal,
                record_id=fresh.record_id, expected_revision=1, target=target.entity,
                target_record_id=conflicting['record_id'], target_expected_revision=conflicting['revision'],
                reviewed_at=REVIEWED_AT, notes='Known identity conflict must block manual linking.')
        self.assertEqual(self._database_snapshot(), before)
        self.review.quarantine(self.principal, record_kind=ReviewRecordKind.ENTITY_MENTION,
            record_id=target.record_id, expected_revision=target.revision.revision,
            reviewed_at=REVIEWED_AT, notes='Revise selected target after it was displayed.')
        before = self._database_snapshot()
        with self.assertRaises(KnowledgeConflict):
            self.review.apply_entity_resolution(self.principal,
                record_id=fresh.record_id, expected_revision=1, target=target.entity,
                target_record_id=target.record_id, target_expected_revision=target.revision.revision,
                reviewed_at=REVIEWED_AT, notes='Stale target must not be accepted.')
        self.assertEqual(self._database_snapshot(), before)

    def test_review_context_returns_paragraph_and_pinned_document_with_acl(self):
        from graphrag_prod.api.knowledge_contracts import ReviewEvidenceRequest, ReviewEvidenceResponse

        mention = self.batch.mentions[0]
        request = ReviewEvidenceRequest(record_id=mention.record_id, expected_revision=1)
        result = self.review.evidence_context(self.principal, request)
        ReviewEvidenceResponse.model_validate(result)
        self.assertGreater(len(result['text']), len(mention.evidence.quoted_text))
        begin = result['char_start'] - result['context_start']
        end = result['char_end'] - result['context_start']
        self.assertEqual(result['text'][begin:end], mention.evidence.quoted_text)
        document = self.review.evidence_context(self.principal, request.model_copy(update={'view':'document'}))
        self.assertEqual(document['text'], self.bundle.version.normalized_text[:8000])
        for denied in (dataclasses.replace(self.principal, groups=frozenset({'other'})),
                       dataclasses.replace(self.principal, tenant_id='another-tenant')):
            with self.assertRaises(KnowledgeReviewUnavailable):
                self.review.evidence_context(denied, request)
        with self.assertRaises(KnowledgeReviewUnavailable):
            self.review.evidence_context(self.principal, request.model_copy(update={'expected_revision':99}))
        self.driver.execute_query(
            'MATCH (v:DocumentVersion {version_id:$version_id, tenant_id:$tenant_id}) '
            'CREATE (v)-[:HAS_CHUNK]->(:Chunk {chunk_id:"restricted-neighbor", '
            'tenant_id:$tenant_id, access_groups:["other"]})',
            tenant_id=self.tenant_id, version_id=self.bundle.version.version_id, database_=self.database)
        limited = self.review.evidence_context(self.principal, request)
        self.assertFalse(limited['document_accessible'])
        with self.assertRaises(KnowledgeReviewUnavailable):
            self.review.evidence_context(self.principal, request.model_copy(update={'view':'document'}))

    def test_reopening_entity_invalidates_dependent_approval_without_publishing(self):
        self._approve_all()
        mention = self.store.get_entity_mention(self.principal, self.batch.mentions[0].record_id,
            statuses=(GovernanceStatus.APPROVED,))
        self.review.quarantine(self.principal, record_kind=ReviewRecordKind.ENTITY_MENTION,
            record_id=mention.record_id, expected_revision=mention.revision.revision,
            reviewed_at=REVIEWED_AT, notes="Return from preview to correct identity.")
        fact = self.store.get_assertion(self.principal, self.batch.assertions[0].record_id,
            statuses=(GovernanceStatus.QUARANTINED,))
        self.assertIsNotNone(fact)
        self.assertEqual(fact.trust.authority, self.batch.assertions[0].trust.authority)
        self.assertEqual(fact.evidence, self.batch.assertions[0].evidence)
        current = self.store.get_entity_mention(self.principal, mention.record_id,
            statuses=(GovernanceStatus.QUARANTINED,))
        self.assertEqual(fact.subject_mention_revision_id, current.revision_id)
        self.assertEqual(self.publication.history(self.principal), ())

    def test_shared_entity_sources_publish_once_with_one_effective_revision_per_record(self):
        revisions = list(self._approve_all())
        target = self.store.get_entity_mention(self.principal, self.batch.mentions[0].record_id,
            statuses=(GovernanceStatus.APPROVED,))
        for index in range(2):
            # Independent extraction records of the same evidence must retain
            # distinct provenance, while resolving to one canonical entity.
            source = dataclasses.replace(self.batch.mentions[0],
                revision=RecordRevision.next(f'publication-source-{index}', 0),
                trust=dataclasses.replace(self.batch.mentions[0].trust,
                    extractor_version=f'publication-source-extractor:v{index + 2}'))
            self.store.persist_llm_candidates(ABoxRecordBatch(self.tenant_id, (source,), ()))
            outcome = self.review.apply_entity_resolution(self.principal,
                record_id=source.record_id, expected_revision=1, target=target.entity,
                target_record_id=target.record_id, target_expected_revision=target.revision.revision,
                reviewed_at=REVIEWED_AT, notes='Both contexts identify the same entity.').outcomes[0]
            if index == 1:
                old_revision_id = outcome.revision_id
                self.review.quarantine(self.principal, record_kind=ReviewRecordKind.ENTITY_MENTION,
                    record_id=source.record_id, expected_revision=2, reviewed_at=REVIEWED_AT,
                    notes='Return this source to step 03 for another review.')
                self.review.approve(self.principal, record_kind=ReviewRecordKind.ENTITY_MENTION,
                    record_id=source.record_id, expected_revision=3, reviewed_at=REVIEWED_AT,
                    notes='Confirm this source again after review.')
                current = self.store.get_entity_mention(self.principal, source.record_id,
                    statuses=(GovernanceStatus.APPROVED,))
                self.assertEqual(current.revision.revision, 4)
                revisions.append(current.revision_id)
            else:
                revisions.append(outcome.revision_id)
        before = self._database_snapshot()
        with self.assertRaises((KnowledgeReviewUnavailable, KnowledgePublicationConflict)):
            self.publication.publish(self.principal, (*revisions, old_revision_id),
                expected_active_publication_id=None, published_at=PUBLISHED_AT, preview_only=True)
        self.assertEqual(self._database_snapshot(), before)
        preview = self.publication.publish(self.principal, tuple(revisions),
            expected_active_publication_id=None, published_at=PUBLISHED_AT, preview_only=True)
        self.assertEqual(self._database_snapshot(), before)
        shared = [entity for entity in preview['entities_after']
            if entity['entity_id'] == target.entity.entity_id]
        self.assertEqual(len(shared), 1)
        self.assertEqual(len(shared[0]['evidence_ids']), 3)
        self.assertEqual(len(preview['records_after']), 5)
        self.assertEqual(len(preview['relationship_changes']), 1)
        publication = self.publication.publish(self.principal, tuple(revisions),
            expected_active_publication_id=None, published_at=PUBLISHED_AT,
            expected_preview_hash=preview['preview_hash'])
        nodes, _, _ = self.driver.execute_query(
            'MATCH (e:Entity {entity_id:$entity_id}) RETURN count(e) AS count',
            entity_id=target.entity.entity_id, database_=self.database)
        self.assertEqual(nodes[0]['count'], 1)
        rows, _, _ = self.driver.execute_query(
            'MATCH (:KnowledgePublication {publication_id:$publication_id})'
            '-[:PUBLISHES_KNOWLEDGE_REVISION]->(r) '
            'RETURN r.record_id AS record_id, count(r) AS count',
            publication_id=publication.publication_id, database_=self.database)
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row['count'] == 1 for row in rows))

    def test_publication_preview_rolls_back_and_matches_exact_published_manifest(self):
        revisions = self._approve_all()
        before = self._database_snapshot()
        kwargs = dict(expected_active_publication_id=None, published_at=PUBLISHED_AT)
        preview = self.publication.publish(self.principal, revisions, preview_only=True, **kwargs)
        self.assertEqual(self._database_snapshot(), before)
        repeated = self.publication.publish(self.principal, revisions, preview_only=True,
            expected_active_publication_id=None, published_at=PUBLISHED_AT + timedelta(minutes=1))
        self.assertEqual(preview, repeated)
        self.assertEqual(preview["instances_after"]["summary"], {
            "entity_count": 2, "property_count": 0, "relationship_count": 1})
        self.assertEqual({item["entity_id"] for item in preview["instances_after"]["entities"]},
                         {item["entity_id"] for item in preview["entities_after"]})
        self.assertEqual(len(preview["entity_changes"]), 2)
        self.assertEqual(len(preview["relationship_changes"]), 1)
        self.assertEqual(len(preview["records_after"]), 3)
        self.assertTrue(all(row["quoted_text"] for row in preview["evidence"]))
        published = self.publication.publish(self.principal, revisions,
            expected_preview_hash=preview["preview_hash"], **kwargs)
        self.assertEqual(published.manifest_hash, preview["manifest_hash"])
        self.assertEqual(set(published.published_revision_ids), {
            row["revision"]["revision_id"] for row in preview["records_after"]})
        replay = self.publication.publish(self.principal, revisions,
            expected_preview_hash=preview["preview_hash"], **kwargs)
        self.assertEqual(replay.publication_id, published.publication_id)

    def test_publication_preview_mismatch_and_missing_dependencies_leave_no_writes(self):
        revisions = self._approve_all()
        before = self._database_snapshot()
        for values, options in [(revisions, {"expected_preview_hash": "0" * 64}),
                                ((revisions[-1],), {"preview_only": True})]:
            with self.assertRaises(KnowledgePublicationConflict):
                self.publication.publish(self.principal, values,
                    expected_active_publication_id=None, published_at=PUBLISHED_AT, **options)
            self.assertEqual(self._database_snapshot(), before)

    def test_publication_preview_removal_preserves_other_records_and_source_history(self):
        revisions = self._approve_all()
        first = self.publication.publish(self.principal, revisions,
            expected_active_publication_id=None, published_at=PUBLISHED_AT)
        before = self._database_snapshot()
        preview = self.publication.publish(self.principal, (), preview_only=True,
            expected_active_publication_id=first.publication_id, published_at=PUBLISHED_AT,
            remove_record_ids=(self.batch.assertions[0].record_id,))
        self.assertEqual(self._database_snapshot(), before)
        self.assertEqual(preview["entity_changes"], [])
        self.assertEqual(preview["relationship_changes"][0]["operation"], "REMOVE")
        self.assertEqual(len(preview["records_after"]), 2)
        outsider = dataclasses.replace(self.principal, groups=frozenset({"other"}))
        with self.assertRaises((KnowledgePublicationConflict, KnowledgeReviewUnavailable, KnowledgeAuthorizationError)):
            self.publication.publish(outsider, (), preview_only=True,
                expected_active_publication_id=first.publication_id, published_at=PUBLISHED_AT,
                remove_record_ids=(self.batch.assertions[0].record_id,))
        self.assertEqual(self._database_snapshot(), before)

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

    def test_relationship_property_cardinality_and_materialization(self) -> None:
        candidate = self.batch.assertions[0]
        authoritative = self.authoritative_batch.assertions[0]
        extractor = candidate.trust.extractor_version or ""

        def property_value(text: str, confidence: float = 1.0):
            start = self.bundle.chunk.text.index(text)
            end = start + len(text)
            literal = TypedLiteralValue(
                datatype="STRING",
                typed_value=text,
                raw_value=text,
                canonical_value=text,
            )
            return RelationshipPropertyValue(
                property_value_id=relationship_property_value_id(
                    self.tenant_id,
                    "QUALIFIED_OFFERS",
                    "BASIS",
                    literal.identity_reference,
                    candidate.evidence.chunk_id,
                    start,
                    end,
                    extractor,
                    self.tbox_id,
                ),
                tenant_id=self.tenant_id,
                relationship_type="QUALIFIED_OFFERS",
                name="BASIS",
                literal_semantics=literal,
                evidence_chunk_id=candidate.evidence.chunk_id,
                evidence_char_start=start,
                evidence_char_end=end,
                evidence_text=text,
                extractor_version=extractor,
                schema_version=self.tbox_id,
                confidence=confidence,
            )

        value = property_value("offers", 0.97)
        second = property_value("Apple")
        relationship = dataclasses.replace(
            candidate,
            predicate="QUALIFIED_OFFERS",
            relationship_properties=(value,),
        )
        validate_manifest = (
            Neo4jKnowledgePublicationService._validate_property_cardinality_tx
        )

        with self.driver.session(database=self.database) as session:
            for invalid, message in (
                (
                    dataclasses.replace(relationship, relationship_properties=()),
                    "required relationship property",
                ),
                (
                    dataclasses.replace(
                        relationship,
                        relationship_properties=(value, second),
                    ),
                    "single-valued cardinality",
                ),
            ):
                with self.assertRaisesRegex(
                    KnowledgePublicationConflict,
                    message,
                ):
                    session.execute_read(
                        validate_manifest,
                        self.tenant_id,
                        (*self.batch.mentions, invalid),
                    )

        requests = tuple(
            ReviewRequest(
                ReviewRecordKind.ENTITY_MENTION,
                record.record_id,
                1,
                GovernanceStatus.APPROVED,
                REVIEWED_AT,
                "Endpoint verified.",
                MentionEdit(expert.entity, record.confidence),
            )
            for record, expert in zip(
                self.batch.mentions,
                self.authoritative_batch.mentions,
                strict=True,
            )
        ) + (
            ReviewRequest(
                ReviewRecordKind.ASSERTION,
                candidate.record_id,
                1,
                GovernanceStatus.APPROVED,
                REVIEWED_AT,
                "Relationship qualifier and exact evidence verified.",
                AssertionEdit(
                    authoritative.subject,
                    "QUALIFIED_OFFERS",
                    candidate.subject_mention_revision_id,
                    candidate.confidence,
                    object_entity=authoritative.object_entity,
                    object_mention_revision_id=candidate.object_mention_revision_id,
                    relationship_properties=(value,),
                ),
            ),
        )
        outcomes = self.review.review_batch(self.principal, requests).outcomes
        publication = self.publication.publish(
            self.principal,
            tuple(item.revision_id for item in outcomes),
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
            }]->(assertion:Assertion)-[:HAS_RELATIONSHIP_PROPERTY]->
                  (value:RelationshipPropertyValue)-[:EVIDENCED_BY]->
                  (chunk:Chunk)
            WHERE assertion.governed_revision_id = revision.revision_id
            RETURN assertion.predicate AS predicate,
                   assertion.relationship_properties_json AS assertion_json,
                   assertion.relationship_properties_format_version
                       AS assertion_format_version,
                   revision.relationship_properties_json AS revision_json,
                   revision.relationship_properties_format_version
                       AS revision_format_version,
                   value.property_value_id AS property_value_id,
                   value.relationship_type AS relationship_type,
                   value.name AS name,
                   value.literal_datatype AS datatype,
                   value.literal_raw_value AS raw_value,
                   value.literal_canonical_value AS canonical_value,
                   value.confidence AS confidence,
                   value.evidence_char_start AS evidence_start,
                   value.evidence_char_end AS evidence_end,
                   value.evidence_text AS evidence_text,
                   substring(
                       chunk.text,
                       value.evidence_char_start - chunk.char_start,
                       value.evidence_char_end - value.evidence_char_start
                   ) AS source_text,
                   value.tenant_id = revision.tenant_id AS same_tenant,
                   value.document_id = revision.document_id AS same_document,
                   value.version_id = revision.version_id AS same_version,
                   value.evidence_chunk_id = revision.chunk_id AS same_chunk,
                   value.access_policy_id = revision.access_policy_id AS same_policy,
                   value.access_groups = revision.access_groups AS same_groups,
                   revision.evidence_char_start <= value.evidence_char_start
                       AND value.evidence_char_end <= revision.evidence_char_end
                       AS inside_parent
            """,
            publication_id=publication.publication_id,
            database_=self.database,
        )
        self.assertEqual(len(rows), 1)
        row = dict(rows[0])
        expected_json = json.dumps(
            [value.to_mapping()],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        self.assertEqual(row["predicate"], "QUALIFIED_OFFERS")
        self.assertEqual(row["assertion_json"], expected_json)
        self.assertEqual(row["assertion_format_version"], 1)
        self.assertEqual(row["revision_json"], expected_json)
        self.assertEqual(row["revision_format_version"], 1)
        self.assertEqual(row["property_value_id"], value.property_value_id)
        self.assertEqual(row["relationship_type"], "QUALIFIED_OFFERS")
        self.assertEqual(row["name"], "BASIS")
        self.assertEqual(row["datatype"], "STRING")
        self.assertEqual(row["raw_value"], "offers")
        self.assertEqual(row["canonical_value"], "offers")
        self.assertEqual(row["confidence"], 0.97)
        self.assertEqual(row["evidence_start"], 6)
        self.assertEqual(row["evidence_end"], 12)
        self.assertEqual(row["evidence_text"], "offers")
        self.assertEqual(row["source_text"], "offers")
        for flag in (
            "same_tenant",
            "same_document",
            "same_version",
            "same_chunk",
            "same_policy",
            "same_groups",
            "inside_parent",
        ):
            self.assertTrue(row[flag], flag)

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
        # Simulate publications created before the immutable binding migration
        # and prove the replay-safe migration derives only a single exact
        # ontology version from the immutable revision manifest.
        self.driver.execute_query(
            """
            MATCH (publication:KnowledgePublication)-[
                binding:USES_TBOX_VERSION
            ]->(:TBoxVersion)
            DELETE binding
            REMOVE publication.ontology_version_id
            """,
            database_=self.database,
        )
        apply_schema(self.driver, self.database)
        tbox_v2 = dataclasses.replace(self.tbox, version=2)
        tbox_store = Neo4jTBoxStore(self.driver, self.database)
        tbox_store.import_version(tbox_v2)
        tbox_store.publish(
            self.tenant_id,
            tbox_v2.tbox_id,
            expected_active_tbox_id=self.tbox_id,
        )
        active_after_tbox_upgrade = self.publication.active(self.principal)
        self.assertIsNotNone(active_after_tbox_upgrade)
        assert active_after_tbox_upgrade is not None
        self.assertEqual(
            active_after_tbox_upgrade.publication_id,
            publication_two.publication_id,
        )
        self.assertEqual(active_after_tbox_upgrade.ontology_version_id, self.tbox_id)
        bound, _, _ = self.driver.execute_query(
            """
            MATCH (publication:KnowledgePublication {
                publication_id: $publication_id
            })-[:USES_TBOX_VERSION]->(tbox:TBoxVersion)
            RETURN publication.ontology_version_id AS publication_tbox_id,
                   tbox.tbox_id AS bound_tbox_id,
                   tbox.status AS bound_tbox_status
            """,
            publication_id=publication_two.publication_id,
            database_=self.database,
        )
        self.assertEqual(
            dict(bound[0]),
            {
                "publication_tbox_id": self.tbox_id,
                "bound_tbox_id": self.tbox_id,
                "bound_tbox_status": "RETIRED",
            },
        )
        projected = Neo4jEvidenceSubgraphProjector(
            self.driver,
            self.database,
        ).project(
            self.principal,
            (self.bundle.chunk.chunk_id,),
        )
        self.assertEqual(len(projected.entities), 2)
        self.assertEqual(
            projected.publication_ids,
            (publication_two.publication_id,),
        )
        with self.assertRaises(KnowledgePublicationConflict):
            self.publication.publish(
                self.principal,
                (),
                expected_active_publication_id=publication_two.publication_id,
                published_at=PUBLISHED_AT + timedelta(minutes=2),
                remove_record_ids=(first.record_id,),
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
        self.assertEqual(restored.ontology_version_id, self.tbox_id)
        self.assertEqual(
            self.publication.active(
                self.principal
            ).publication_id,  # type: ignore[union-attr]
            publication_one.publication_id,
        )
        history = self.publication.history(self.principal)
        self.assertEqual(len(history), 2)
        self.assertEqual(
            {item.ontology_version_id for item in history},
            {self.tbox_id},
        )
        replayed = self.publication.publish(
            self.principal,
            (first_approved.revision_id,),
            expected_active_publication_id=None,
            published_at=PUBLISHED_AT + timedelta(minutes=3),
        )
        self.assertEqual(replayed.publication_id, publication_one.publication_id)
        self.assertEqual(replayed.ontology_version_id, self.tbox_id)
        projected_after_rollback = Neo4jEvidenceSubgraphProjector(
            self.driver,
            self.database,
        ).project(
            self.principal,
            (self.bundle.chunk.chunk_id,),
        )
        self.assertEqual(len(projected_after_rollback.entities), 1)
        self.assertEqual(
            projected_after_rollback.publication_ids,
            (publication_one.publication_id,),
        )

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
