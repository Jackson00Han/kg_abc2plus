"""Comparison is complete, read-only and authorized against real publications."""
from dataclasses import replace
from datetime import timedelta
import unittest
from graphrag_prod.knowledge.publication_comparison import publication_comparison
from graphrag_prod.knowledge.review import KnowledgePublicationConflict
from graphrag_prod.api.publication_comparison_contracts import PublicationComparisonResponse
from tests.integration import test_published_quality_neo4j as fixture


class PublicationComparisonNeo4jTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): fixture.PublishedGraphQualityNeo4jTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls): cls.driver.close()
    def setUp(self): fixture.PublishedGraphQualityNeo4jTests.setUp(self)
    def tearDown(self): fixture.PublishedGraphQualityNeo4jTests.tearDown(self)
    _relationship_property=fixture.PublishedGraphQualityNeo4jTests._relationship_property
    _publish=fixture.PublishedGraphQualityNeo4jTests._publish

    def test_published_correction_republishes_and_comparison_identifies_revision_change(self):
        first=self._publish()
        record_id=self.candidate_batch.assertions[0].record_id
        current=self.review.revision_history(self.principal,record_id,limit=1)[0].record
        paused=self.review.quarantine(self.principal,record_kind=fixture.ReviewRecordKind.ASSERTION,record_id=record_id,expected_revision=current.revision.revision,reviewed_at=fixture.PUBLISHED_AT+timedelta(seconds=1),notes='Recheck published relationship.')
        findings=self.quality.audit(self.principal)
        self.assertIn('HEAD_CURRENT_REVISION_INVALID',{i.code for i in findings.issues})
        approved=self.review.approve(self.principal,record_kind=fixture.ReviewRecordKind.ASSERTION,record_id=record_id,expected_revision=paused.revision,reviewed_at=fixture.PUBLISHED_AT+timedelta(seconds=2),notes='Source relationship verified.')
        second=self.publication.publish(self.principal,(approved.revision_id,),expected_active_publication_id=first.publication_id,replace_record_ids=(record_id,),published_at=fixture.PUBLISHED_AT+timedelta(seconds=3))
        self.assertTrue(self.quality.audit(self.principal).passed)
        result=PublicationComparisonResponse.model_validate(publication_comparison(self.publication,self.principal,first.publication_id,second.publication_id))
        self.assertEqual(len(result.changed),1)
        self.assertEqual(result.changed[0].before.record_id,record_id)
        self.assertEqual(result.changed[0].after.predicate,'OFFERS')

    def test_complete_current_and_historical_comparison_and_denied_scope(self):
        first=self._publish()
        same=PublicationComparisonResponse.model_validate(publication_comparison(self.publication,self.principal,first.publication_id,first.publication_id))
        self.assertEqual(same.unchanged_count,len(first.published_revision_ids))
        self.assertFalse(same.added or same.removed or same.changed)
        self.assertNotIn('evidence_text',same.model_dump_json())
        with self.assertRaises(KnowledgePublicationConflict):publication_comparison(self.publication,replace(self.principal,groups=frozenset({'other'})),first.publication_id,first.publication_id)
        with self.assertRaises(KnowledgePublicationConflict):publication_comparison(self.publication,self.principal,first.publication_id,'unknown')
