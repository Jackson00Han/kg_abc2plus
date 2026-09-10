"""Run only against an empty, explicitly disposable loopback Neo4j database."""
import ipaddress
import os
import unittest
from urllib.parse import urlparse
from uuid import uuid4

from neo4j import GraphDatabase
from graphrag_prod.api.auth import AuthenticationError, JWTAuthConfig
from graphrag_prod.graph.schema import apply_schema
from graphrag_prod.ingestion import Neo4jEmbeddingIndexManager, Neo4jIngestionService
from graphrag_prod.playground.runtime import PLAYGROUND_AUDIENCE, PLAYGROUND_ISSUER, PlaygroundPersona
from graphrag_prod.playground.workspaces import (
    CreateWorkspace, ResetWorkspace, Neo4jWorkspaceStore, WorkspaceRegistry,
    WorkspaceAuthenticator, prepare_workspace_tenant,
)
from tests.fixtures.pump_ingestion import make_plan


class WorkspaceNeo4jTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get('GRAPHRAG_ALLOW_DISPOSABLE_DB') != '1':
            raise RuntimeError('explicit disposable database required')
        uri = os.environ['TEST_NEO4J_URI']
        if not ipaddress.ip_address(urlparse(uri).hostname).is_loopback:
            raise RuntimeError('loopback required')
        cls.database = os.environ['TEST_NEO4J_DATABASE']
        cls.driver = GraphDatabase.driver(uri, auth=(os.environ['TEST_NEO4J_USER'], os.environ['TEST_NEO4J_PASSWORD']))
        rows, _, _ = cls.driver.execute_query('MATCH (n) RETURN count(n) AS count', database_=cls.database)
        if rows[0]['count']: raise RuntimeError('database must be empty')
        apply_schema(cls.driver, cls.database)

    @classmethod
    def tearDownClass(cls):
        cls.driver.close()

    def test_persistent_project_isolation_and_reset(self):
        store = Neo4jWorkspaceStore(self.driver, self.database)
        seeds = (PlaygroundPersona('persona-01', 'seed', 'seed', 'seed-a', ('public',), ()),)
        store.initialize(seeds)
        key = b'workspace-integration-signing-key-123456789'
        profile = make_plan().bundles[0].all_embeddings[0]
        registry = WorkspaceRegistry(store, key, lambda tenant: prepare_workspace_tenant(self.driver, self.database, profile, tenant))
        auth = WorkspaceAuthenticator(JWTAuthConfig(issuer=PLAYGROUND_ISSUER, audience=PLAYGROUND_AUDIENCE, secret=key), registry)
        def identity(project):
            token = registry.issue_session(registry.personas(project)[0].persona_id)['access_token']
            return auth.verify_identity(token), token
        seed = store.list()[0]
        admin, _ = identity(seed)
        request = CreateWorkspace(name='项目甲', operation_id=str(uuid4()))
        a = registry.create(admin, request)
        self.assertEqual(registry.create(admin, request), a)
        b = registry.create(admin, CreateWorkspace(name='项目乙', operation_id=str(uuid4())))
        self.assertNotEqual(a['active_tenant_id'], b['active_tenant_id'])
        manager = Neo4jEmbeddingIndexManager(self.driver, self.database)
        self.assertEqual(manager.active_generation(a['active_tenant_id']).embedding_space_id, profile.embedding_space_id)
        self.assertEqual(store.counts(a['active_tenant_id'])['documents'], 0)
        service = Neo4jIngestionService(self.driver, self.database, worker_id='workspace-test')
        for project in (a, b):
            service.ingest(make_plan(tenant_id=project['active_tenant_id']))
            self.assertEqual(store.counts(project['active_tenant_id'])['documents'], 1)
        a_admin, old_token = identity(a)
        reset = ResetWorkspace(name=a['name'], operation_id=str(uuid4()), expected_generation=1,
                               confirmation='RESET_CURRENT_KNOWLEDGE_BASE')
        after = registry.reset(a_admin, a['knowledge_base_id'], reset)
        self.assertEqual(after['generation'], 2)
        self.assertTrue(all(value == 0 for value in store.counts(after['active_tenant_id']).values()))
        self.assertEqual(store.counts(b['active_tenant_id'])['documents'], 1)
        self.assertEqual(store.counts(a['active_tenant_id'])['documents'], 1)  # immutable archive
        with self.assertRaises(AuthenticationError): auth.verify_identity(old_token)
        fresh, _ = identity(after)
        self.assertEqual(registry.reset(fresh, a['knowledge_base_id'], reset), after)
        store.initialize(seeds)  # restart does not restore an archived namespace
        reopened = Neo4jWorkspaceStore(self.driver, self.database)
        self.assertEqual(reopened.get(a['knowledge_base_id'])['active_tenant_id'], after['active_tenant_id'])
        self.assertEqual(len(reopened.list()), 3)
        archives = store.rows('MATCH (a:WorkbenchKnowledgeBaseArchive) RETURN a.tenant_id AS tenant')
        self.assertEqual([row['tenant'] for row in archives], [a['active_tenant_id']])
