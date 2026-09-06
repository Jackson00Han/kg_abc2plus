"""Review guidance compares meaning without merging identities or source evidence."""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from pydantic import ValidationError

from graphrag_prod.api.knowledge import Neo4jKnowledgeOperations
from graphrag_prod.api.knowledge_contracts import ReviewAssessmentRequest, ReviewDecisionInput
from graphrag_prod.api.runtime import OperationKind, ResourceNotFoundError
from graphrag_prod.domain import Principal, TypedLiteralValue
from graphrag_prod.knowledge.review import (
    KnowledgeReviewUnavailable, Neo4jKnowledgeReviewService, ReviewOutcome, ReviewRecordKind, ReviewRequest,
)
from graphrag_prod.knowledge.trust import GovernanceStatus
from graphrag_prod.knowledge.review_assessment import (
    AuthoritativeFactMatch, ReviewAssessment, ReviewDependency,
    classify_facts,
)
from tests.fixtures.knowledge import make_knowledge_batch


class ReviewAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.batch = make_knowledge_batch()
        self.relation = self.batch.assertions[0]
        self.literal = dataclasses.replace(
            self.relation, predicate="DISPLAY_NAME", object_entity=None,
            object_mention_revision_id=None, literal_value="Apple",
            literal_semantics=TypedLiteralValue(
                datatype="STRING", typed_value="Apple", raw_value="Apple", canonical_value="Apple",
            ),
        )

    def match(self, fact):
        return AuthoritativeFactMatch("publication-1", fact)

    def test_exact_relations_and_literals_are_duplicates_but_other_endpoints_are_new(self):
        for fact in (self.literal, self.relation):
            with self.subTest(kind=fact.predicate):
                self.assertEqual(classify_facts(fact, (self.match(fact),))[0], "DUPLICATE")
        other = dataclasses.replace(self.relation, object_entity=self.relation.subject)
        self.assertEqual(classify_facts(self.relation, (self.match(other),))[0], "READY")
        untyped = dataclasses.replace(self.literal, literal_semantics=None)
        self.assertEqual(classify_facts(untyped, (self.match(self.literal),))[0], "UNAVAILABLE")

    def test_normalized_units_values_and_times_must_all_match(self):
        evidence_text = "37.5 kW 37500 W 2025-01-01T00:00:00Z"
        evidence = dataclasses.replace(self.literal.evidence, quoted_text=evidence_text, char_end=self.literal.evidence.char_start + len(evidence_text))
        baseline = dataclasses.replace(self.literal, evidence=evidence, literal_value="37.5", literal_semantics=TypedLiteralValue(
            datatype="DECIMAL", typed_value="37.5", raw_value="37.5", raw_unit="kW",
            canonical_value="37.5", canonical_unit="kW",
        ))
        equivalent = dataclasses.replace(baseline, literal_value="37500", literal_semantics=TypedLiteralValue(
            datatype="DECIMAL", typed_value="37.5", raw_value="37500", raw_unit="W",
            canonical_value="37.5", canonical_unit="kW",
        ))
        self.assertEqual(classify_facts(baseline, (self.match(equivalent),))[0], "DUPLICATE")
        for update in ({"canonical_value": "38", "typed_value": "38"}, {"canonical_unit": "MW"},
                       {"observed_at": datetime(2025, 1, 1, tzinfo=UTC), "raw_observed_at": "2025-01-01T00:00:00Z"},
                       {"valid_from": datetime(2025, 1, 1, tzinfo=UTC), "raw_valid_from": "2025-01-01T00:00:00Z"}):
            with self.subTest(update=update):
                changed = dataclasses.replace(baseline, literal_semantics=dataclasses.replace(baseline.literal_semantics, **update))
                self.assertEqual(classify_facts(baseline, (self.match(changed),))[0], "CONFLICT")

    def test_relation_properties_different_or_missing_are_not_duplicates(self):
        # Semantic comparison only consumes named typed property values; their
        # evidence is independently checked by the DB assessment path.
        qualified = SimpleNamespace(**{field.name: getattr(self.relation, field.name)
                                      for field in dataclasses.fields(self.relation)})
        qualified.relationship_properties = (SimpleNamespace(name="BASIS", literal_semantics=self.literal.literal_semantics),)
        self.assertEqual(classify_facts(self.relation, (self.match(qualified),))[0], "CONFLICT")
        self.assertEqual(classify_facts(qualified, (self.match(qualified),))[0], "DUPLICATE")

    def test_conflicting_authority_takes_precedence_over_one_duplicate(self):
        changed = dataclasses.replace(self.literal, literal_value="iPhone", literal_semantics=TypedLiteralValue(
            datatype="STRING", typed_value="iPhone", raw_value="iPhone", canonical_value="iPhone",
        ))
        self.assertEqual(classify_facts(self.literal, (self.match(self.literal), self.match(changed)))[0], "CONFLICT")

    def test_duplicate_input_requires_rejection_without_an_edit(self):
        valid = dict(record_kind="ASSERTION", record_id="record-1", expected_revision=1,
                     decision="REJECTED", notes="retain source", duplicate_of_revision_id="revision-authority")
        self.assertEqual(ReviewDecisionInput(**valid).duplicate_of_revision_id, "revision-authority")
        for change in ({"decision": "APPROVED"}, {"record_kind": "ENTITY_MENTION"},
                       {"assertion_edit": {"subject": {"entity_type": "Company", "canonical_key": "ticker:AAPL", "canonical_name": "Apple"},
                                           "predicate": "DISPLAY_NAME", "subject_mention_revision_id": "mention-1", "confidence": 1.0,
                                           "literal": {"raw_literal": "Apple"}}}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                ReviewDecisionInput(**(valid | change))

        session = MagicMock()
        session.__enter__.return_value = session
        session.execute_write.return_value = (ReviewOutcome(
            ReviewRecordKind.ASSERTION, "record-1", "revision-1", "revision-2", 2, GovernanceStatus.REJECTED,
        ),)
        service = Neo4jKnowledgeReviewService(SimpleNamespace(session=lambda **_: session))
        principal = Principal("reviewer", self.batch.tenant_id, frozenset({"finance-readers"}), frozenset({"knowledge:review"}))
        request = ReviewRequest(ReviewRecordKind.ASSERTION, "record-1", 1, GovernanceStatus.REJECTED,
                                datetime.now(UTC), "Retain source", duplicate_of_revision_id="revision-authority")
        service.review_batch(principal, (request,))
        self.assertEqual(session.execute_write.call_args.args[0].timeout, 25.0)
        service.review_batch(principal, (dataclasses.replace(request, duplicate_of_revision_id=None),))
        self.assertIsNone(getattr(session.execute_write.call_args.args[0], "timeout", None))

    def test_adapter_preserves_dependency_evidence_and_safe_missing_boundary(self):
        mention = self.batch.mentions[0]
        value = ReviewAssessment(self.literal.record_id, self.literal.revision_id, 1, "BLOCKED",
                                 "ENDPOINTS_REQUIRE_REVIEW", "先确认实体", (ReviewDependency(
                                     "subject", mention.record_id, mention.revision_id,
                                     mention.entity.canonical_name, "CANDIDATE", False, mention.evidence,
                                 ),))
        service = SimpleNamespace(assess=lambda *_: value)
        adapter = Neo4jKnowledgeOperations(driver=object(), database="neo4j",
                                           construction=SimpleNamespace(run=lambda: None), assessment_service=service)
        principal = Principal("reviewer", self.batch.tenant_id, frozenset({"finance-readers"}), frozenset({"knowledge:review"}))
        request = ReviewAssessmentRequest(record_id=self.literal.record_id, expected_revision=1)
        payload = adapter.review_assessment(principal, request).payload.model_dump()
        self.assertEqual(payload["dependencies"][0]["evidence"]["quoted_text"], mention.evidence.quoted_text)
        value = dataclasses.replace(value, status="DUPLICATE", matches=(self.match(self.literal),))
        payload = adapter.review_assessment(principal, request).payload.model_dump()
        self.assertEqual(payload["matches"][0]["record"]["literal_semantics"]["canonical_value"], "Apple")
        self.assertEqual(payload["matches"][0]["record"]["evidence"]["quoted_text"], self.literal.evidence.quoted_text)
        with patch.object(service, "assess", side_effect=KnowledgeReviewUnavailable("not visible")):
            with self.assertRaises(ResourceNotFoundError):
                adapter.review_assessment(principal, request)
        self.assertFalse(OperationKind.KNOWLEDGE_REVIEW_ASSESSMENT.is_write)
        self.assertTrue(OperationKind.KNOWLEDGE_REVIEW_ASSESSMENT.is_retry_safe)
