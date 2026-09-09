"""Source browse/read on an owned disposable database, including policy revocation."""
from dataclasses import replace
import unittest
from graphrag_prod.domain import Principal
from graphrag_prod.graph.browse_models import GraphViewChanged
from graphrag_prod.knowledge.source_library import Neo4jSourceLibrary
from tests.fixtures.ingestion import make_plan
from tests.integration import test_document_retirement_neo4j as fixture

class SourceLibraryNeo4jTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): fixture.Neo4jDocumentRetirementIntegrationTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls): cls.driver.close()
    def setUp(self): fixture.Neo4jDocumentRetirementIntegrationTests.setUp(self)
    def tearDown(self): fixture.Neo4jDocumentRetirementIntegrationTests.tearDown(self)

    def test_ingested_source_without_published_graph_is_readable_and_version_pinned(self):
        plan=make_plan();self.ingestion.ingest(plan)
        service=Neo4jSourceLibrary(self.driver,self.database)
        reader=Principal('source-reader',plan.tenant_id,frozenset({'knowledge-readers'}),frozenset({'retrieval:read'}))
        result=service.read(reader);self.assertEqual(len(result['items']),1)
        item=result['items'][0];self.assertFalse(item['has_published_knowledge'])
        chunk=service.read(reader,document_id=item['document_id'],version_id=item['version_id'])
        self.assertTrue(chunk['text']);self.assertEqual(chunk['ordinal'],0)
        with self.assertRaises(GraphViewChanged): service.read(reader,document_id=item['document_id'],version_id='wrong-version')
        self.assertEqual(service.read(replace(reader,tenant_id='other'))['items'],[])
        self.assertEqual(service.read(replace(reader,groups=frozenset({'other'})))['items'],[])
        self.driver.execute_query("MATCH (c:Chunk {document_id:$id}) SET c.access_policy_version=999",id=item['document_id'],database_=self.database)
        self.assertEqual(service.read(reader)['items'],[])
        with self.assertRaises(GraphViewChanged): service.read(reader,document_id=item['document_id'],version_id=item['version_id'])

    def test_corrupted_text_is_not_returned_and_partial_acl_hides_complete_source(self):
        plan=make_plan();self.ingestion.ingest(plan)
        service=Neo4jSourceLibrary(self.driver,self.database)
        reader=Principal('source-reader',plan.tenant_id,frozenset({'knowledge-readers'}),frozenset({'retrieval:read'}))
        item=service.read(reader)['items'][0]
        self.driver.execute_query("MATCH (c:Chunk {document_id:$id,ordinal:0}) SET c.text='corrupted'",id=item['document_id'],database_=self.database)
        with self.assertRaises(GraphViewChanged): service.read(reader,document_id=item['document_id'],version_id=item['version_id'])
        self.driver.execute_query("MATCH (c:Chunk {document_id:$id,ordinal:1}) SET c.access_groups=['restricted']",id=item['document_id'],database_=self.database)
        self.assertEqual(service.read(reader)['items'],[])
