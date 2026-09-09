"""Real HTTP facade enforces both audit visibility and reviewer write permission."""
from datetime import UTC, datetime
from unittest.mock import Mock, patch
import unittest
from fastapi.testclient import TestClient
from graphrag_prod.api.backend import GraphRAGApplicationBackend, GraphRAGQueryOperations, QueryEmbedding
from graphrag_prod.api.knowledge import Neo4jKnowledgeOperations
from graphrag_prod.generation import GroundedGenerationService
from tests.e2e.test_api import _app, _headers, _token
from tests.unit.test_api_backend import RecordingAnswerModel, RecordingDocuments, RecordingEmbedder, RecordingReadiness, RecordingRetrievalEngine, _retrieval_result

class QualityReviewAPISecurityTests(unittest.TestCase):
    def test_review_write_needs_both_scopes_and_cannot_spoof_actor_or_tenant(self):
        knowledge=Neo4jKnowledgeOperations(driver=Mock(),construction=Mock())
        queries=GraphRAGQueryOperations(RecordingRetrievalEngine(_retrieval_result()),RecordingEmbedder(QueryEmbedding((1.0,0.0),'space-v1')),GroundedGenerationService(RecordingAnswerModel()))
        backend=GraphRAGApplicationBackend(documents=RecordingDocuments(),queries=queries,readiness=RecordingReadiness(),knowledge=knowledge)
        body=dict(run_id='run-1',issue_id='issue-1',operation_key='op-1',decision='NEEDS_CORRECTION',notes='Verify the source.')
        calls=[]
        def record(principal,request):
            calls.append(principal)
            return dict(review_id='review-1',run_id=request.run_id,issue_id=request.issue_id,decision=request.decision,notes=request.notes,recorded_by=principal.principal_id,recorded_at=datetime.now(UTC),publication_id='pub-1',publication_generation=1)
        with patch('graphrag_prod.graph.quality_review.Neo4jQualityReviewService.record',side_effect=record):
            with TestClient(_app(backend)) as client:
                self.assertEqual(client.post('/v1/knowledge/quality/reviews',json=body).status_code,401)
                for scope in ('knowledge:quality','knowledge:review','retrieval:read'):
                    response=client.post('/v1/knowledge/quality/reviews',headers=_headers(_token(scope=scope)),json=body)
                    self.assertEqual(response.status_code,403,response.text)
                headers=_headers(_token(subject='reviewer',scope='knowledge:quality knowledge:review'))
                for field in ('tenant_id','recorded_by','recorded_at'):
                    self.assertEqual(client.post('/v1/knowledge/quality/reviews',headers=headers,json={**body,field:'spoof'}).status_code,422)
                result=client.post('/v1/knowledge/quality/reviews',headers=headers,json=body)
                self.assertEqual(result.status_code,200,result.text)
                self.assertEqual(result.json()['recorded_by'],'reviewer')
        self.assertEqual(len(calls),1)
