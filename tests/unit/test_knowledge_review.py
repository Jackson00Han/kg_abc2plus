"""Boundary tests for governed human review and publication contracts."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
import unittest

from graphrag_prod.domain.access import Principal
from graphrag_prod.knowledge.review import (
    KNOWLEDGE_PUBLISH_CAPABILITY,
    KNOWLEDGE_REVIEW_CAPABILITY,
    MAX_PUBLICATION_RECORDS,
    MAX_REVIEW_BATCH,
    AssertionEdit,
    KnowledgeAuthorizationError,
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


if __name__ == "__main__":
    unittest.main()
