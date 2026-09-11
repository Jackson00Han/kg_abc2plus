"""HTTP authorization, validation and dispatch for property ownership review."""
from datetime import UTC, datetime
from types import SimpleNamespace
import time
import unittest

from fastapi.testclient import TestClient
import jwt

from graphrag_prod.api import GraphRAGApplicationBackend, JWTAuthConfig, JWTAuthenticator, create_app
from graphrag_prod.api.knowledge import Neo4jKnowledgeOperations
from graphrag_prod.api.runtime import OperationKind


class PropertyAssignmentHTTPTests(unittest.TestCase):
    def test_routes_enforce_scope_revision_and_audited_explicit_selection(self):
        calls=[]
        def assignment(principal, request, *, reviewed_at=None):
            calls.append((principal,request,reviewed_at))
            if reviewed_at is None:
                return dict(record_id=request.record_id,revision=request.expected_revision,current=None,items=[],truncated=False)
            return dict(outcomes=[dict(record_kind='ASSERTION',record_id=request.record_id,
                previous_revision_id='power-1',revision_id='power-2',revision=2,status='QUARANTINED')])
        knowledge=Neo4jKnowledgeOperations(driver=object(),database='neo4j',
            construction=SimpleNamespace(run=lambda:None),reviews=SimpleNamespace(property_assignment=assignment),
            clock=lambda:datetime.now(UTC))
        noop=lambda *args:None
        backend=GraphRAGApplicationBackend(documents=SimpleNamespace(ingest=noop,delete=noop,get_job=noop),
            queries=SimpleNamespace(retrieve=noop,answer=noop),readiness=SimpleNamespace(check=noop),knowledge=knowledge)
        secret='pump-assignment-local-test-auth-key-with-distinct-bytes!'
        auth=JWTAuthenticator(JWTAuthConfig(issuer='https://identity.test',audience='pump-api',secret=secret))
        def headers(scope):
            now=int(time.time())
            return {'Authorization':'Bearer '+jwt.encode(dict(iss='https://identity.test',aud='pump-api',
                sub='reviewer',iat=now,exp=now+300,tenant_id='pump-test',groups=['members'],scope=scope),secret,algorithm='HS256')}
        url='/v1/knowledge/property-assignment/power'
        payload=dict(record_id='power',expected_revision=1,target_entity_id='pump-202',target_record_id='mention-202',
            target_expected_revision=2,notes='原文设备编号为 BC-P-202，额定功率 22 kW。')
        with TestClient(create_app(authenticator=auth,backend=backend)) as client:
            for scope in ['ontology:read','knowledge:publish']:
                self.assertEqual(client.get(url,params={'expected_revision':1},headers=headers(scope)).status_code,403)
                self.assertEqual(client.post('/v1/knowledge/property-assignment:apply',json=payload,headers=headers(scope)).status_code,403)
            self.assertEqual(calls,[])
            self.assertEqual(client.get(url,params={'expected_revision':0},headers=headers('knowledge:review')).status_code,422)
            response=client.get(url,params={'expected_revision':1,'query':'BC-P-202'},headers=headers('knowledge:review'))
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(calls[0][1].query,'BC-P-202')
            self.assertIsNone(calls[0][2])
            for extra in [{'notes':' '},{'target_expected_revision':0},{'tenant_id':'other-tenant'},{'notes':'x'*2001}]:
                self.assertEqual(client.post('/v1/knowledge/property-assignment:apply',json={**payload,**extra},headers=headers('knowledge:review')).status_code,422)
            response=client.post('/v1/knowledge/property-assignment:apply',json=payload,headers=headers('knowledge:review'))
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['outcomes'][0]['status'],'QUARANTINED')
            principal,request,reviewed_at=calls[-1]
            self.assertEqual(principal.tenant_id,'pump-test')
            self.assertEqual(request.target_entity_id,'pump-202')
            self.assertIsNotNone(reviewed_at)
        self.assertFalse(OperationKind.PROPERTY_ASSIGNMENT.is_write)
        self.assertTrue(OperationKind.PROPERTY_ASSIGNMENT.is_retry_safe)
        self.assertTrue(OperationKind.PROPERTY_ASSIGNMENT_APPLY.is_write)
        self.assertFalse(OperationKind.PROPERTY_ASSIGNMENT_APPLY.is_retry_safe)
