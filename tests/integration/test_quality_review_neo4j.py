"""Human audit outcomes on a real governed publication; replay and stale scope checks."""
from dataclasses import replace
import unittest
from graphrag_prod.graph.quality_review import Neo4jQualityReviewService
from graphrag_prod.graph.published_quality_history import Neo4jPublishedGraphQualityHistoryService, PublishedGraphQualityHistoryConflict
from graphrag_prod.graph.published_quality import PublishedGraphQualityAuthorizationError
from graphrag_prod.api.quality_review_contracts import QualityReviewRequest, QualityReviewListResponse
from tests.integration import test_published_quality_neo4j as fixture

class QualityReviewNeo4jTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): fixture.PublishedGraphQualityNeo4jTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls): cls.driver.close()
    def setUp(self): fixture.PublishedGraphQualityNeo4jTests.setUp(self)
    def tearDown(self): fixture.PublishedGraphQualityNeo4jTests.tearDown(self)
    _relationship_property=fixture.PublishedGraphQualityNeo4jTests._relationship_property
    _publish=fixture.PublishedGraphQualityNeo4jTests._publish

    def test_decision_persists_replays_preserves_report_and_rejects_changed_version(self):
        self._publish()
        self.driver.execute_query('MATCH (:KnowledgeSnapshot)-[r:INCLUDES_ENTITY]->(:Entity) DELETE r',database_=self.database)
        history=Neo4jPublishedGraphQualityHistoryService(self.driver,self.database,auditor=self.quality)
        run=history.audit_and_record(self.principal)
        self.assertTrue(run.report.issues)
        request=QualityReviewRequest(run_id=run.run_id,issue_id=run.report.issues[0].issue_id,operation_key='human-check-1',decision='NEEDS_CORRECTION',notes='Checked source and confirmed missing graph membership.')
        reviews=Neo4jQualityReviewService(history)
        first=reviews.record(self.principal,request)
        self.assertEqual(reviews.record(self.principal,request),first)
        result=QualityReviewListResponse.model_validate(reviews.list(self.principal,run.run_id))
        self.assertEqual(len(result.items),1)
        self.assertTrue(result.object_labels)
        self.assertEqual(history.get_run(self.principal,run.run_id).record_hash,run.record_hash)
        with self.assertRaises(PublishedGraphQualityHistoryConflict): reviews.record(self.principal,request.model_copy(update={'notes':'Different body with same operation key'}))
        with self.assertRaises(PublishedGraphQualityAuthorizationError): reviews.list(replace(self.principal,groups=frozenset({'other'})),run.run_id)
        self.driver.execute_query('MATCH (s:TenantCorpusState {tenant_id:$tenant}) SET s.corpus_revision=s.corpus_revision+1',tenant=self.tenant_id,database_=self.database)
        with self.assertRaises(PublishedGraphQualityHistoryConflict): reviews.record(self.principal,request.model_copy(update={'operation_key':'human-check-2'}))
        self.assertEqual(reviews.record(self.principal,request),first)
        self.driver.execute_query("MATCH (r:QualityReviewDecision) SET r.payload_json='{}'",database_=self.database)
        with self.assertRaises(PublishedGraphQualityHistoryConflict): reviews.list(self.principal,run.run_id)
