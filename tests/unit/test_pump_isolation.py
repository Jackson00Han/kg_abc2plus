"""Pump-only profile checks; never open the quarantined corpus."""
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient
from graphrag_prod.playground.industrial_web import pump_only_page
from graphrag_prod.playground.workspaces import Neo4jWorkspaceStore

PROJECT = dict(knowledge_base_id='55599339-95c8-5ddc-ad01-d296cc3f2643', name='工业知识库',
    initial_tenant_id='industrial-schneider-demo',active_tenant_id='workspace-empty-pump', generation=2,
    admin_groups=['members','administrators'],user_groups=['members'])

class PumpIsolationTests(unittest.TestCase):
    def test_archived_seed_identities_are_excluded(self):
        store=Neo4jWorkspaceStore(Mock(),'neo4j',pump_only=True)
        self.assertTrue(store.visible(PROJECT))
        self.assertFalse(store.visible({**PROJECT,'active_tenant_id':'industrial-schneider-demo'}))
        self.assertFalse(store.visible({**PROJECT,'initial_tenant_id':'tenant-alpha'}))
        self.assertTrue(store.visible({'active_tenant_id':'new-project'}))

    def test_page_removes_out_of_scope_controls(self):
        html=Path('src/graphrag_prod/playground/static/industrial/index.html').read_text()
        rendered=pump_only_page(html)
        for forbidden in ('Canalis','EvoPacT','BKT-A01','HVX-A01','data-family='):
            self.assertNotIn(forbidden,rendered)
        self.assertIn('循环水泵',rendered)

    def test_runtime_never_loads_or_warms_excluded_data(self):
        from scripts import run_playground
        with (patch.object(run_playground,'_reuse_corpus') as reuse,
              patch.object(run_playground,'_load_corpus') as load,
              patch.object(run_playground,'_warm_retrieval') as warm,
              patch('graphrag_prod.playground.industrial_runtime.verify_existing_industrial_runtime') as verify,
              patch('graphrag_prod.playground.industrial_runtime.build_industrial_query_operations') as industrial,
              patch('graphrag_prod.playground.workspaces.Neo4jWorkspaceStore') as store):
            store.return_value.list.return_value=[PROJECT]
            app=run_playground.build_playground_app(Mock(),'neo4j',signing_key=b'pump-only-test-secret-123456789012345',
                embedder=Mock(provider='offline',model='fixture',dimensions=4),
                enable_industrial=True,reuse_existing_corpus=True,skip_provider_warmup=True,pump_only=True)
            with TestClient(app) as client:
                response=client.get('/playground/bootstrap');self.assertEqual(response.status_code,200)
                value=response.json()
                self.assertEqual(value['data_scope'],'pump-only')
                self.assertFalse(value['industrial']['enabled'])
                self.assertEqual(value['questions'],[])
                self.assertEqual(value['enabled_tenants'],['workspace-empty-pump'])
                self.assertEqual(len(value['personas']),2)
                self.assertEqual(client.get('/industrial/assets/demo/industrial-extension.txt').status_code,404)
                self.assertEqual(client.get('/playground/demo-files/ontology.json').status_code,200)
            for call in (reuse,load,warm,verify,industrial):call.assert_not_called()
            store.return_value.initialize.assert_not_called()
