"""Human identity decisions are not dependent on domain identifiers."""
from dataclasses import replace
import unittest
from types import SimpleNamespace

from graphrag_prod.api.knowledge_contracts import ReviewDecisionInput, ReviewBatchResponse
from graphrag_prod.knowledge.identity_review import independent_entity, identity_actions
from graphrag_prod.knowledge.entity_resolution import ResolutionOutcome
from tests.unit.test_api_entity_resolution import CANDIDATE, TBOX


class IdentityReviewTests(unittest.TestCase):
    def test_action_does_not_depend_on_match_or_identity_properties(self):
        candidate = CANDIDATE
        definition = SimpleNamespace(canonical_key_namespaces=('llm-candidate',))
        for outcome in ResolutionOutcome:
            action = identity_actions(candidate, definition, [SimpleNamespace(outcome=outcome)])
            self.assertTrue(action['independent']['allowed'])
            self.assertTrue(action['independent']['requires_reason'])
        action = identity_actions(candidate, TBOX.entity_types[0], [])
        self.assertFalse(action['independent']['allowed'])

    def test_independent_ids_are_stable_per_source_and_never_name_based(self):
        first = CANDIDATE
        target = independent_entity(first)
        self.assertEqual(target, independent_entity(first))
        self.assertNotEqual(target.entity_id, first.entity.entity_id)
        other = replace(first, revision=type(first.revision).next('other-mention', 0))
        self.assertNotEqual(target.entity_id, independent_entity(other).entity_id)
        self.assertEqual(target.canonical_name, first.entity.canonical_name)

    def test_batch_response_preserves_pending_dependency_status(self):
        response = ReviewBatchResponse(outcomes=[dict(record_kind='ASSERTION', record_id='fact',
            previous_revision_id='before', revision_id='after', revision=2, status='CANDIDATE')])
        self.assertEqual(response.outcomes[0].status, 'CANDIDATE')

    def test_service_returns_latest_dependent_outcome_after_selected_records(self):
        from contextlib import contextmanager
        from graphrag_prod.knowledge.review import (
            Neo4jKnowledgeReviewService, ReviewRequest, ReviewRecordKind, ReviewOutcome)
        from graphrag_prod.knowledge.trust import GovernanceStatus
        from tests.unit.test_api_entity_resolution import _principal, NOW
        mention = ReviewOutcome(ReviewRecordKind.ENTITY_MENTION, 'source', 'm1', 'm2', 2,
                                GovernanceStatus.APPROVED)
        fact2 = ReviewOutcome(ReviewRecordKind.ASSERTION, 'fact', 'f1', 'f2', 2,
                              GovernanceStatus.CANDIDATE)
        fact3 = replace(fact2, previous_revision_id='f2', revision_id='f3', revision=3)
        class Driver:
            @contextmanager
            def session(self, **kwargs):
                yield SimpleNamespace(execute_write=lambda *args: (mention, fact2, fact3))
        request = ReviewRequest(ReviewRecordKind.ENTITY_MENTION, 'source', 1,
            GovernanceStatus.APPROVED, NOW, '原文支持独立对象', identity_action='INDEPENDENT')
        result = Neo4jKnowledgeReviewService(Driver(), 'neo4j').review_batch(_principal(), (request,))
        self.assertEqual(result.outcomes, (mention, fact3))

    def test_contract_rejects_ambiguous_action_and_oversized_notes(self):
        payload = dict(record_kind='ENTITY_MENTION', record_id='source', expected_revision=1,
                       decision='APPROVED', notes='原文指向独立对象', identity_action='INDEPENDENT')
        self.assertEqual(ReviewDecisionInput(**payload).identity_action, 'INDEPENDENT')
        for changes in ({'record_kind': 'ASSERTION'}, {'decision': 'REJECTED'},
                        {'identity_action': 'GUESS'}, {'notes': 'x'*2001},
                        {'identity_action': None, 'identity_group': 'group'}):
            with self.assertRaises(ValueError):
                ReviewDecisionInput(**(payload | changes))
