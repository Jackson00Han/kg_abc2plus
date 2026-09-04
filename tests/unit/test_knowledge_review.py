"""Boundary tests for governed human review and publication contracts."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
import unittest
from unittest.mock import patch

from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import entity_id
from graphrag_prod.domain.models import TypedLiteralValue
from graphrag_prod.knowledge import EntityIdentity, RecordRevision, knowledge_record_id
from graphrag_prod.knowledge.review import (
    KNOWLEDGE_PUBLISH_CAPABILITY,
    KNOWLEDGE_REVIEW_CAPABILITY,
    MAX_PUBLICATION_RECORDS,
    MAX_REVIEW_BATCH,
    AssertionEdit,
    KnowledgeAuthorizationError,
    KnowledgePublicationConflict,
    MentionEdit,
    Neo4jKnowledgePublicationService,
    Neo4jKnowledgeReviewService,
    ReviewRecordKind,
    ReviewRequest,
)
from graphrag_prod.knowledge.trust import GovernanceStatus
from tests.fixtures.knowledge import make_knowledge_batch


REVIEWED_AT = datetime(2025, 2, 4, 5, 6, tzinfo=UTC)


class _NoSessionDriver:
    def session(self, **kwargs: object) -> object:
        raise AssertionError("invalid input must fail before opening Neo4j")


def _principal() -> Principal:
    return Principal(
        "expert:reviewer",
        "tenant-knowledge",
        frozenset({"finance-readers"}),
        frozenset(
            {
                KNOWLEDGE_REVIEW_CAPABILITY,
                KNOWLEDGE_PUBLISH_CAPABILITY,
            }
        ),
    )


class KnowledgeReviewContractTests(unittest.TestCase):
    def test_resolution_atomically_rebinds_dependents_without_approving_facts(self) -> None:
        candidate_batch = make_knowledge_batch(authoritative=False)
        assertion = candidate_batch.assertions[0]
        current = next(
            mention
            for mention in candidate_batch.mentions
            if mention.entity.entity_id == assertion.subject.entity_id
        )
        target = next(
            mention.entity
            for mention in make_knowledge_batch(authoritative=True).mentions
            if mention.entity.entity_type == current.entity.entity_type
        )

        class _Rows:
            def __iter__(self):  # type: ignore[no-untyped-def]
                return iter(({"revision": {}},))

        class _Tx:
            query = ""
            parameters: dict[str, object] = {}

            def run(self, query: str, **parameters: object) -> _Rows:
                self.query = query
                self.parameters = parameters
                return _Rows()

        tx = _Tx()
        created_mentions: list[object] = []
        created_assertions: list[object] = []

        with (
            patch.object(
                Neo4jKnowledgeReviewService,
                "_lock_tenant_corpus_tx",
            ),
            patch.object(
                Neo4jKnowledgeReviewService,
                "_lock_review_head_tx",
            ) as lock_head,
            patch.object(
                Neo4jKnowledgeReviewService,
                "_load_current_review_record_tx",
                return_value=current,
            ),
            patch.object(
                Neo4jKnowledgeReviewService,
                "_validate_record_tbox_tx",
            ),
            patch(
                "graphrag_prod.knowledge.review._stored_assertion",
                return_value=assertion,
            ),
            patch(
                "graphrag_prod.knowledge.review.Neo4jKnowledgeStore."
                "_create_mention_revision_tx",
                side_effect=lambda _tx, value, **_kwargs: created_mentions.append(value),
            ),
            patch(
                "graphrag_prod.knowledge.review.Neo4jKnowledgeStore."
                "_create_assertion_revision_tx",
                side_effect=lambda _tx, value, **_kwargs: created_assertions.append(value),
            ),
        ):
            outcomes = Neo4jKnowledgeReviewService._apply_entity_resolution_tx(
                tx,
                _principal(),
                current.record_id,
                current.revision.revision,
                target,
                REVIEWED_AT,
                "Verified exact identity properties.",
            )

        self.assertEqual(len(created_mentions), 1)
        self.assertEqual(created_mentions[0].entity, target)
        self.assertEqual(created_mentions[0].trust.status, GovernanceStatus.APPROVED)
        self.assertEqual(len(created_assertions), 1)
        rebound = created_assertions[0]
        self.assertEqual(rebound.subject, target)
        self.assertEqual(
            rebound.subject_mention_revision_id,
            created_mentions[0].revision_id,
        )
        self.assertEqual(rebound.trust.status, GovernanceStatus.CANDIDATE)
        self.assertEqual(rebound.object_entity, assertion.object_entity)
        self.assertEqual(
            [item.status for item in outcomes],
            [GovernanceStatus.APPROVED, GovernanceStatus.CANDIDATE],
        )
        self.assertEqual(lock_head.call_count, 2)
        for required in (
            "ACTIVE_SNAPSHOT",
            "ACTIVE_TBOX_VERSION",
            "revision.subject_mention_revision_id = $mention_revision_id",
            "any(group IN $groups WHERE group IN revision.access_groups)",
            "any(group IN $groups WHERE group IN chunk.access_groups)",
            "any(group IN $groups WHERE group IN document.access_groups)",
        ):
            self.assertIn(required, tx.query)

    def test_review_request_requires_bounded_auditable_decision(self) -> None:
        mention = make_knowledge_batch(authoritative=False).mentions[0]
        request = ReviewRequest(
            ReviewRecordKind.ENTITY_MENTION,
            mention.record_id,
            1,
            GovernanceStatus.APPROVED,
            REVIEWED_AT,
            "Verified against the filing.",
            MentionEdit(mention.entity, 0.99),
        )
        self.assertEqual(request.edit, MentionEdit(mention.entity, 0.99))

        with self.assertRaisesRegex(ValueError, "review notes"):
            dataclasses.replace(request, notes="  ")
        with self.assertRaisesRegex(ValueError, "review decision"):
            dataclasses.replace(request, decision=GovernanceStatus.PUBLISHED)
        with self.assertRaisesRegex(TypeError, "GovernanceStatus"):
            dataclasses.replace(request, decision="APPROVED")  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            dataclasses.replace(
                request,
                reviewed_at=datetime(2025, 2, 4, 5, 6),
            )

    def test_assertion_edit_requires_exactly_one_object_shape(self) -> None:
        assertion = make_knowledge_batch(authoritative=False).assertions[0]
        edit = AssertionEdit(
            assertion.subject,
            assertion.predicate,
            assertion.subject_mention_revision_id,
            0.97,
            object_entity=assertion.object_entity,
            object_mention_revision_id=assertion.object_mention_revision_id,
        )
        self.assertEqual(edit.object_entity, assertion.object_entity)

        with self.assertRaisesRegex(ValueError, "exactly one"):
            AssertionEdit(
                assertion.subject,
                assertion.predicate,
                assertion.subject_mention_revision_id,
                0.97,
            )
        with self.assertRaisesRegex(ValueError, "cannot reference"):
            AssertionEdit(
                assertion.subject,
                assertion.predicate,
                assertion.subject_mention_revision_id,
                0.97,
                object_mention_revision_id="not-allowed",
                literal_value="Apple",
            )
        with self.assertRaisesRegex(ValueError, "typed semantics"):
            AssertionEdit(
                assertion.subject,
                "DISPLAY_NAME",
                assertion.subject_mention_revision_id,
                0.97,
                literal_value="Apple",
            )

    def test_review_batch_rejects_empty_duplicate_and_oversized_work(self) -> None:
        service = Neo4jKnowledgeReviewService(_NoSessionDriver())
        mention = make_knowledge_batch(authoritative=False).mentions[0]
        request = ReviewRequest(
            ReviewRecordKind.ENTITY_MENTION,
            mention.record_id,
            1,
            GovernanceStatus.REJECTED,
            REVIEWED_AT,
            "Unsupported extraction.",
        )

        with self.assertRaisesRegex(ValueError, "must not be empty"):
            service.review_batch(_principal(), ())
        with self.assertRaisesRegex(ValueError, "duplicate record"):
            service.review_batch(_principal(), (request, request))
        oversized = tuple(
            dataclasses.replace(request, record_id=f"record-{index}")
            for index in range(MAX_REVIEW_BATCH + 1)
        )
        with self.assertRaisesRegex(ValueError, "exceeds"):
            service.review_batch(_principal(), oversized)

    def test_queue_and_publication_limits_fail_before_database_access(self) -> None:
        review = Neo4jKnowledgeReviewService(_NoSessionDriver())
        publication = Neo4jKnowledgePublicationService(_NoSessionDriver())
        with self.assertRaisesRegex(ValueError, "between"):
            review.review_queue(_principal(), limit=0)
        with self.assertRaisesRegex(TypeError, "tuple"):
            publication.publish(
                _principal(),
                ["revision"],  # type: ignore[arg-type]
                expected_active_publication_id=None,
                published_at=REVIEWED_AT,
            )
        with self.assertRaisesRegex(ValueError, "exceeds"):
            publication.publish(
                _principal(),
                tuple(
                    f"revision-{index}"
                    for index in range(MAX_PUBLICATION_RECORDS + 1)
                ),
                expected_active_publication_id=None,
                published_at=REVIEWED_AT,
            )

    def test_revision_history_uses_immutable_source_chain_and_acl(self) -> None:
        mention = make_knowledge_batch(authoritative=False).mentions[0]
        newer = dataclasses.replace(
            mention,
            revision=RecordRevision.next(mention.record_id, 1),
        )

        class _Rows(list):
            pass

        class _Session:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []

            def __enter__(self):  # type: ignore[no-untyped-def]
                return self

            def __exit__(self, *_args):  # type: ignore[no-untyped-def]
                return None

            def run(self, query: str, **parameters: object) -> _Rows:
                self.calls.append((query, parameters))
                if "GovernedEntityMentionRevision" in query:
                    return _Rows(
                        (
                            {"revision": {"revision": 1}},
                            {"revision": {"revision": 2}},
                        )
                    )
                return _Rows()

        class _Driver:
            def __init__(self) -> None:
                self.value = _Session()

            def session(self, **_kwargs: object) -> _Session:
                return self.value

        driver = _Driver()
        with patch(
            "graphrag_prod.knowledge.review._stored_mention",
            side_effect=lambda value: mention if value["revision"] == 1 else newer,
        ):
            history = Neo4jKnowledgeReviewService(driver).revision_history(
                _principal(),
                mention.record_id,
                limit=10,
            )
        self.assertEqual(
            [item.record.revision.revision for item in history],
            [2, 1],
        )
        query, parameters = driver.value.calls[0]
        for boundary in (
            "tenant_id: $tenant_id",
            "record_id: $record_id",
            "HAS_VERSION",
            "HAS_CHUNK",
            "tbox.status IN ['PUBLISHED', 'RETIRED']",
            "revision.access_policy_id = chunk.access_policy_id",
            "any(group IN $groups WHERE group IN document.access_groups)",
            "ORDER BY revision.revision DESC",
            "LIMIT $limit",
        ):
            self.assertIn(boundary, query)
        self.assertNotIn("ACTIVE_SNAPSHOT", query)
        self.assertNotIn("ACTIVE_TBOX_VERSION", query)
        self.assertEqual(parameters["tenant_id"], _principal().tenant_id)

    def test_publication_candidates_exclude_active_ids_but_keep_replacements(self) -> None:
        mention = make_knowledge_batch(authoritative=False).mentions[0]
        approved = dataclasses.replace(
            mention,
            trust=dataclasses.replace(
                mention.trust,
                status=GovernanceStatus.APPROVED,
                reviewed_by="expert:reviewer",
                reviewed_at=REVIEWED_AT,
                review_notes="Verified source identity.",
            ),
        )

        class _Rows(list):
            pass

        class _Session:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []

            def __enter__(self):  # type: ignore[no-untyped-def]
                return self

            def __exit__(self, *_args):  # type: ignore[no-untyped-def]
                return None

            def run(self, query: str, **parameters: object) -> _Rows:
                self.calls.append((query, parameters))
                if "GovernedEntityMentionRevision" in query:
                    return _Rows(
                        ({"revision": {"id": "approved"}, "requires_replacement": True},)
                    )
                return _Rows()

        class _Driver:
            def __init__(self) -> None:
                self.value = _Session()

            def session(self, **_kwargs: object) -> _Session:
                return self.value

        driver = _Driver()
        with patch(
            "graphrag_prod.knowledge.review._stored_mention",
            return_value=approved,
        ):
            candidates = Neo4jKnowledgePublicationService(driver).candidates(
                _principal(),
                limit=7,
            )
        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0].requires_replacement)
        self.assertEqual(candidates[0].item.record, approved)
        query, parameters = driver.value.calls[0]
        for boundary in (
            "CURRENT_REVISION",
            "revision.governance_status IN $statuses",
            "ACTIVE_TBOX_VERSION",
            "ACTIVE_SNAPSHOT",
            "NOT EXISTS",
            "PUBLISHES_KNOWLEDGE_REVISION]->(revision)",
            "active_revision.record_id = revision.record_id",
            "any(group IN $groups WHERE group IN revision.access_groups)",
        ):
            self.assertIn(boundary, query)
        self.assertLess(
            query.index("PUBLISHES_KNOWLEDGE_REVISION]->(revision)"),
            query.index("LIMIT $limit"),
        )
        self.assertEqual(
            set(parameters["statuses"]),
            {"APPROVED", "PUBLISHED"},
        )
        self.assertEqual(parameters["limit"], 7)

        with self.assertRaisesRegex(ValueError, "between"):
            Neo4jKnowledgePublicationService(_NoSessionDriver()).candidates(
                _principal(),
                limit=0,
            )

    def test_review_and_publication_require_explicit_capabilities(self) -> None:
        review = Neo4jKnowledgeReviewService(_NoSessionDriver())
        publication = Neo4jKnowledgePublicationService(_NoSessionDriver())
        reader = Principal(
            "reader",
            "tenant-knowledge",
            frozenset({"finance-readers"}),
        )
        with self.assertRaises(KnowledgeAuthorizationError):
            review.review_queue(reader)
        with self.assertRaises(KnowledgeAuthorizationError):
            publication.active(reader)

    def test_complete_publication_manifest_enforces_required_and_single_cardinality(
        self,
    ) -> None:
        class _Rows:
            def __iter__(self):  # type: ignore[no-untyped-def]
                return iter(
                    (
                        {
                            "entity_type": "Company",
                            "property_definitions": [
                                {
                                    "name": "DISPLAY_NAME",
                                    "datatype": "STRING",
                                    "required": True,
                                    "cardinality": "ONE",
                                }
                            ],
                        },
                        {
                            "entity_type": "Product",
                            "property_definitions": [],
                        },
                    )
                )

        class _Tx:
            def run(self, query: str, **parameters: object) -> _Rows:
                self.query = query
                self.parameters = parameters
                return _Rows()

        batch = make_knowledge_batch(authoritative=False)
        records = (*batch.mentions, *batch.assertions)
        tx = _Tx()
        with self.assertRaisesRegex(
            KnowledgePublicationConflict,
            "required property",
        ):
            Neo4jKnowledgePublicationService._validate_property_cardinality_tx(
                tx,
                batch.tenant_id,
                records,
            )

        relation = batch.assertions[0]
        literal_semantics = TypedLiteralValue(
            datatype="STRING",
            typed_value="Apple",
            raw_value="Apple",
            canonical_value="Apple",
        )
        literal = dataclasses.replace(
            relation,
            predicate="DISPLAY_NAME",
            object_entity=None,
            object_mention_revision_id=None,
            literal_value="Apple",
            literal_semantics=literal_semantics,
        )
        good_records = (*batch.mentions, literal)
        Neo4jKnowledgePublicationService._validate_property_cardinality_tx(
            tx,
            batch.tenant_id,
            good_records,
        )

        with self.assertRaisesRegex(
            KnowledgePublicationConflict,
            "single-valued",
        ):
            Neo4jKnowledgePublicationService._validate_property_cardinality_tx(
                tx,
                batch.tenant_id,
                (*good_records, literal),
            )

        legacy_approved = dataclasses.replace(literal, literal_semantics=None)
        with self.assertRaisesRegex(
            KnowledgePublicationConflict,
            "active T-Box",
        ):
            Neo4jKnowledgePublicationService._validate_property_cardinality_tx(
                tx,
                batch.tenant_id,
                (*batch.mentions, legacy_approved),
            )

        # A pre-contract published revision is permitted only at the explicit
        # manifest replay boundary; it remains readable but cannot be edited or
        # newly approved without typed semantics.
        authoritative = make_knowledge_batch(authoritative=True)
        legacy_published = dataclasses.replace(
            authoritative.assertions[0],
            predicate="DISPLAY_NAME",
            object_entity=None,
            object_mention_revision_id=None,
            literal_value="Apple",
            literal_semantics=None,
        )
        Neo4jKnowledgePublicationService._validate_property_cardinality_tx(
            tx,
            batch.tenant_id,
            (*authoritative.mentions, legacy_published),
        )

    def test_complete_manifest_enforces_distinct_relationship_endpoint_cardinality(
        self,
    ) -> None:
        class _Rows:
            def __init__(self, rows: tuple[dict[str, object], ...]) -> None:
                self.rows = rows

            def __iter__(self):  # type: ignore[no-untyped-def]
                return iter(self.rows)

        class _Tx:
            def __init__(
                self,
                source_cardinality: str,
                target_cardinality: str = "ZERO_OR_MORE",
            ) -> None:
                self.source_cardinality = source_cardinality
                self.target_cardinality = target_cardinality

            def run(self, query: str, **_parameters: object) -> _Rows:
                if "DECLARES_RELATIONSHIP_TYPE" in query:
                    return _Rows(
                        (
                            {
                                "name": "OFFERS",
                                "source_types": ["Company"],
                                "target_types": ["Product"],
                                "source_cardinality": self.source_cardinality,
                                "target_cardinality": self.target_cardinality,
                                "property_definitions": [],
                            },
                        )
                    )
                return _Rows(
                    (
                        {"entity_type": "Company", "property_definitions": []},
                        {"entity_type": "Product", "property_definitions": []},
                    )
                )

        batch = make_knowledge_batch(authoritative=False)
        relation = batch.assertions[0]
        original_product = next(
            item for item in batch.mentions if item.entity.entity_type == "Product"
        )
        second_product = EntityIdentity(
            entity_id=entity_id(
                batch.tenant_id,
                "Product",
                "apple-product:second",
            ),
            tenant_id=batch.tenant_id,
            entity_type="Product",
            canonical_key="apple-product:second",
            canonical_name="Second product",
        )
        mention_record_id = knowledge_record_id(
            batch.tenant_id,
            "ENTITY_MENTION",
            "second-product",
        )
        second_mention = dataclasses.replace(
            original_product,
            revision=RecordRevision.next(mention_record_id, 0),
            entity=second_product,
        )
        assertion_record_id = knowledge_record_id(
            batch.tenant_id,
            "ASSERTION",
            "second-offer",
        )
        second_relation = dataclasses.replace(
            relation,
            revision=RecordRevision.next(assertion_record_id, 0),
            object_entity=second_product,
            object_mention_revision_id=second_mention.revision_id,
        )

        with self.assertRaisesRegex(
            KnowledgePublicationConflict,
            "source endpoint single-valued",
        ):
            Neo4jKnowledgePublicationService._validate_property_cardinality_tx(
                _Tx("ZERO_OR_ONE"),
                batch.tenant_id,
                (*batch.mentions, second_mention, relation, second_relation),
            )

        original_company = next(
            item for item in batch.mentions if item.entity.entity_type == "Company"
        )
        second_company = EntityIdentity(
            entity_id=entity_id(
                batch.tenant_id,
                "Company",
                "ticker:second",
            ),
            tenant_id=batch.tenant_id,
            entity_type="Company",
            canonical_key="ticker:second",
            canonical_name="Second company",
        )
        company_record_id = knowledge_record_id(
            batch.tenant_id,
            "ENTITY_MENTION",
            "second-company",
        )
        second_company_mention = dataclasses.replace(
            original_company,
            revision=RecordRevision.next(company_record_id, 0),
            entity=second_company,
        )
        inbound_record_id = knowledge_record_id(
            batch.tenant_id,
            "ASSERTION",
            "second-company-offer",
        )
        second_inbound = dataclasses.replace(
            relation,
            revision=RecordRevision.next(inbound_record_id, 0),
            subject=second_company,
            subject_mention_revision_id=second_company_mention.revision_id,
        )
        with self.assertRaisesRegex(
            KnowledgePublicationConflict,
            "target endpoint single-valued",
        ):
            Neo4jKnowledgePublicationService._validate_property_cardinality_tx(
                _Tx("ZERO_OR_MORE", "ZERO_OR_ONE"),
                batch.tenant_id,
                (
                    *batch.mentions,
                    second_company_mention,
                    relation,
                    second_inbound,
                ),
            )

        # Multiple evidence revisions for the same canonical edge count as one
        # counterpart under the closed-world endpoint contract.
        duplicate_record_id = knowledge_record_id(
            batch.tenant_id,
            "ASSERTION",
            "duplicate-offer-evidence",
        )
        duplicate = dataclasses.replace(
            relation,
            revision=RecordRevision.next(duplicate_record_id, 0),
        )
        Neo4jKnowledgePublicationService._validate_property_cardinality_tx(
            _Tx("ONE"),
            batch.tenant_id,
            (*batch.mentions, relation, duplicate),
        )

        with self.assertRaisesRegex(
            KnowledgePublicationConflict,
            "required relationship OFFERS is absent",
        ):
            Neo4jKnowledgePublicationService._validate_property_cardinality_tx(
                _Tx("ONE"),
                batch.tenant_id,
                batch.mentions,
            )


if __name__ == "__main__":
    unittest.main()
