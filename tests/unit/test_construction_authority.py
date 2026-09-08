"""Source authority is fixed at construction, independently of human review."""
from dataclasses import replace
import unittest

from graphrag_prod.construction import ConstructionAuthorizationError, ConstructionConflict
from graphrag_prod.domain.access import Principal
from graphrag_prod.knowledge import AuthorityLevel, GovernanceStatus, KnowledgeOrigin
from tests.unit.test_construction_workflow import _Extractor, _metadata, _tbox, _workflow, NOW, SOURCE


class ConstructionAuthorityTests(unittest.TestCase):
    def principal(self, authority=True):
        return Principal('operator', 'tenant-industrial', frozenset({'engineers'}),
                         frozenset({'knowledge:construct', 'knowledge:import'} if authority else {'knowledge:construct'}))

    def test_authoritative_extraction_is_authoritative_before_review_and_replays(self):
        extractor = _Extractor(_tbox())
        workflow, _, knowledge, _ = _workflow(extractor=extractor)
        metadata = replace(_metadata(), knowledge_scope='AUTHORITATIVE')
        first = workflow.run(self.principal(), SOURCE, metadata)
        batch = knowledge.last_batch
        batch.require_llm_candidates()
        for record in (*batch.mentions, *batch.assertions):
            self.assertEqual(record.trust.origin, KnowledgeOrigin.AUTHORITATIVE_EXTRACTED)
            self.assertEqual(record.trust.authority, AuthorityLevel.AUTHORITATIVE)
            self.assertEqual(record.trust.status, GovernanceStatus.CANDIDATE)
            reviewed = record.trust.transition_to(GovernanceStatus.APPROVED, reviewed_by='operator', reviewed_at=NOW)
            self.assertEqual(reviewed.authority, record.trust.authority)
        replay = workflow.run(self.principal(), SOURCE, metadata)
        self.assertEqual(replay.job_id, first.job_id)
        self.assertEqual(replay.chunks[0].mention_record_ids, first.chunks[0].mention_record_ids)
        self.assertTrue(replay.chunks[0].replayed)
        self.assertEqual(extractor.calls, 1)

    def test_business_review_cannot_promote_authority(self):
        workflow, _, knowledge, _ = _workflow(extractor=_Extractor(_tbox()))
        workflow.run(self.principal(), SOURCE, _metadata())
        for record in (*knowledge.last_batch.mentions, *knowledge.last_batch.assertions):
            reviewed = record.trust.transition_to(GovernanceStatus.APPROVED, reviewed_by='operator', reviewed_at=NOW)
            self.assertEqual(reviewed.authority, AuthorityLevel.SECONDARY)
            with self.assertRaises(ValueError):
                replace(reviewed, authority=AuthorityLevel.AUTHORITATIVE)

    def test_authoritative_scope_requires_capability_before_source_or_provider_work(self):
        extractor = _Extractor(_tbox())
        workflow, audit, _, pipeline = _workflow(extractor=extractor)
        with self.assertRaises(ConstructionAuthorizationError):
            workflow.run(self.principal(False), SOURCE, replace(_metadata(), knowledge_scope='AUTHORITATIVE'))
        self.assertEqual(extractor.calls, 0)
        self.assertFalse(pipeline.requests)
        self.assertFalse(audit.jobs)

    def test_changed_scope_cannot_replay_same_operation_or_use_business_artifacts(self):
        extractor = _Extractor(_tbox())
        workflow, _, knowledge, _ = _workflow(extractor=extractor)
        workflow.run(self.principal(), SOURCE, _metadata())
        old_ids = {r.record_id for r in knowledge.last_batch.mentions}
        with self.assertRaises(ConstructionConflict):
            workflow.run(self.principal(), SOURCE, replace(_metadata(), knowledge_scope='AUTHORITATIVE'))
        workflow.run(self.principal(), SOURCE, replace(_metadata(operation_key='new-authority-operation'), knowledge_scope='AUTHORITATIVE'))
        self.assertFalse(old_ids.intersection(r.record_id for r in knowledge.last_batch.mentions))
        self.assertEqual(extractor.calls, 2)

    def test_authoritative_low_confidence_stays_quarantined(self):
        workflow, _, knowledge, _ = _workflow(extractor=_Extractor(_tbox(), status=GovernanceStatus.QUARANTINED))
        workflow.run(self.principal(), SOURCE, replace(_metadata(), knowledge_scope='AUTHORITATIVE'))
        knowledge.last_batch.require_llm_quarantined()
        self.assertEqual(knowledge.quarantine_writes, 1)
        self.assertEqual(knowledge.last_batch.mentions[0].trust.authority, AuthorityLevel.AUTHORITATIVE)

    def test_scope_is_closed_and_source_only_cannot_claim_extracted_authority(self):
        for values in ({'knowledge_scope': 'unknown'}, {'knowledge_scope': 'AUTHORITATIVE', 'extraction_mode': 'SOURCE_ONLY'}):
            with self.assertRaises(ValueError):
                replace(_metadata(), **values)
