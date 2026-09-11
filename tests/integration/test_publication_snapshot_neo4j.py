"""Unified publication text/graph lifecycle, using only the pump test package."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import ipaddress
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from urllib.parse import urlparse

from neo4j import GraphDatabase

from graphrag_prod.domain import Principal
from graphrag_prod.domain.ids import content_checksum, document_id, version_id, chunk_id, chunk_embedding_id, entity_id
from graphrag_prod.graph.schema import apply_schema, verify_schema
from graphrag_prod.graph.browse_models import GraphViewChanged, GraphBrowseQuery
from graphrag_prod.graph.browsing import Neo4jPublishedGraphBrowser
from graphrag_prod.graph.published_quality import Neo4jPublishedGraphQualityService
from graphrag_prod.graph.published_inventory import Neo4jActivePublicationInventoryService
from graphrag_prod.ingestion import IngestionPlan, Neo4jIngestionService, Neo4jEmbeddingIndexManager
from graphrag_prod.ingestion.models import default_artifact_input_hash
from graphrag_prod.knowledge.models import (
    EntityIdentity, EntityMentionRecord, EvidenceReference, AssertionRecord,
    RecordRevision, knowledge_record_id, ABoxRecordBatch, llm_candidate_trust,
)
from graphrag_prod.knowledge.store import Neo4jKnowledgeStore
from graphrag_prod.knowledge.review import (
    Neo4jKnowledgeReviewService, Neo4jKnowledgePublicationService,
    KnowledgePublicationConflict, KnowledgeReviewUnavailable, ReviewRecordKind,
)
from graphrag_prod.knowledge.source_library import Neo4jSourceLibrary
from graphrag_prod.construction.literals import TBoxLiteralNormalizer
from graphrag_prod.ontology import TBoxVersion, Neo4jTBoxStore
from graphrag_prod.retrieval import Neo4jRetrievalEngine, RetrievalLimits, RetrievalRequest, VersionFilter
from tests.fixtures.pump_ingestion import make_plan

PACKAGE = Path(__file__).resolve().parents[2] / 'src/graphrag_prod/playground/static/industrial-demo-v1'
NOW = datetime(2026, 9, 11, tzinfo=UTC)


class PublicationSnapshotNeo4jTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get('GRAPHRAG_ALLOW_DISPOSABLE_DB') != '1':
            raise RuntimeError('disposable test database opt-in required')
        uri = os.environ['TEST_NEO4J_URI']
        if not ipaddress.ip_address(urlparse(uri).hostname).is_loopback:
            raise RuntimeError('loopback test database required')
        cls.database = os.environ['TEST_NEO4J_DATABASE']
        cls.driver = GraphDatabase.driver(uri, auth=(os.environ['TEST_NEO4J_USER'], os.environ['TEST_NEO4J_PASSWORD']), notifications_min_severity='OFF')
        rows, _, _ = cls.driver.execute_query('MATCH (n) RETURN count(n) AS count', database_=cls.database)
        if rows[0]['count']:
            cls.driver.close()
            raise RuntimeError('refusing nonempty database')
        apply_schema(cls.driver, cls.database)
        cls.driver.execute_query('CALL db.awaitIndexes(60)', database_=cls.database)
        if verify_schema(cls.driver, cls.database):
            raise RuntimeError('schema verification failed')

    @classmethod
    def tearDownClass(cls):
        cls.driver.execute_query('MATCH (n) DETACH DELETE n', database_=cls.database)
        cls.driver.close()

    def setUp(self):
        self.query('MATCH (n) DETACH DELETE n')
        self.tenant = 'pump-publication-snapshot-test'
        self.principal = Principal('reviewer', self.tenant, frozenset({'members'}),
            frozenset({'knowledge:review','knowledge:publish','knowledge:graph:read','retrieval:read','knowledge:lifecycle','knowledge:quality:read'}))
        self.review = Neo4jKnowledgeReviewService(self.driver, self.database)
        self.store = Neo4jKnowledgeStore(self.driver, self.database)
        self.publications = Neo4jKnowledgePublicationService(self.driver, self.database)
        self.sources = Neo4jSourceLibrary(self.driver, self.database)
        self.engine = Neo4jRetrievalEngine(self.driver, self.database)
        self.index = Neo4jEmbeddingIndexManager(self.driver, self.database)
        self.ingestion = Neo4jIngestionService(self.driver, self.database,
            worker_id='pump-snapshot-test', clock=SimpleNamespace(now=lambda: NOW))
        self.tbox = TBoxVersion.from_mapping({**json.loads((PACKAGE/'ontology.json').read_text()),
            'tenant_id':self.tenant,'status':'DRAFT'})
        tboxes = Neo4jTBoxStore(self.driver, self.database)
        tboxes.import_version(self.tbox)
        tboxes.publish(self.tenant,self.tbox.tbox_id,expected_active_tbox_id=None)
        self.index_number = 0
        self.active_id = None

    def query(self, query, **parameters):
        return self.driver.execute_query(query, parameters_=parameters, database_=self.database)[0]

    def add_source(self, filename, code, *, previous=None):
        base_plan = make_plan(tenant_id=self.tenant)
        base = base_plan.bundles[0]
        text = (PACKAGE/filename).read_text()
        checksum = content_checksum(text)
        uri = previous.document.canonical_uri if previous else 'urn:test:publication:'+filename
        doc = replace(base.document, document_id=document_id(self.tenant,uri), canonical_uri=uri, title=filename)
        ver_id = version_id(doc.document_id,checksum,checksum)
        version = replace(base.version, version_id=ver_id, document_id=doc.document_id,
            normalized_text=text, checksum=checksum, original_checksum=checksum,
            version_number=previous.version.version_number+1 if previous else 1)
        ck_id=chunk_id(ver_id,base.chunk.splitter_version,0,0,len(text),checksum)
        chunk=replace(base.chunk,chunk_id=ck_id,version_id=ver_id,document_id=doc.document_id,
            text=text,checksum=checksum,char_end=len(text))
        embedding=replace(base.embedding,chunk_id=ck_id,embedding_id=chunk_embedding_id(ck_id,base.embedding.embedding_space_id))
        bundle=replace(base,document=doc,version=version,chunk=chunk,embedding=embedding)
        plan=IngestionPlan.build(operation_key='pump-version:'+ver_id,profile=base_plan.profile,
            governance_policy=base_plan.governance_policy,bundles=(bundle,),
            expected_active_snapshot_id=previous.snapshot_id if previous else None, source_generation=0,
            artifact_input_hashes={ck_id:default_artifact_input_hash(bundle)},created_at=NOW)
        self.ingestion.ingest(plan)
        self.index_number+=1
        active=self.index.active_generation(self.tenant)
        generation=self.index.prepare(tenant_id=self.tenant,embedding_profile=embedding,generation_version=self.index_number)
        self.index.activate(generation.generation_id,expected_active_generation_id=active.generation_id if active else None)
        evidence=EvidenceReference(self.tenant,doc.document_id,ver_id,ck_id,0,len(text),text,
            chunk.access_policy_id,chunk.access_policy_version,chunk.access_groups)
        key='llm-candidate:pump-'+code
        entity=EntityIdentity(entity_id(self.tenant,'Equipment',key),self.tenant,'Equipment',key,'循环水泵',())
        trust=llm_candidate_trust(ontology_version_id=self.tbox.tbox_id,extractor_version='pump-offline:v1',
            prompt_version='pump-offline:v1',extracted_at=NOW)
        mention=EntityMentionRecord(RecordRevision.next(knowledge_record_id(self.tenant,'ENTITY_MENTION',ver_id),0),
            self.tenant,entity,evidence,1.0,trust,NOW)
        definition=next(p for e in self.tbox.entity_types if e.name=='Equipment' for p in e.properties if p.name=='EquipmentCode')
        literal=TBoxLiteralNormalizer().normalize(definition,raw_value=code,raw_unit=None,valid_from=None,valid_to=None,observed_at=None)
        fact=AssertionRecord(RecordRevision.next(knowledge_record_id(self.tenant,'ASSERTION',ver_id),0),
            self.tenant,entity,'EquipmentCode',evidence,mention.revision_id,1.0,trust,NOW,
            literal_value=literal.raw_value,literal_semantics=literal)
        self.store.persist_llm_candidates(ABoxRecordBatch(self.tenant,(mention,),(fact,)))
        records=[]
        for record,kind in ((mention,ReviewRecordKind.ENTITY_MENTION),(fact,ReviewRecordKind.ASSERTION)):
            result=self.review.approve(self.principal,record_kind=kind,record_id=record.record_id,
                expected_revision=1,reviewed_at=NOW+timedelta(minutes=1),notes='已核对循环水泵测试包。')
            records.append(self.review.revision_history(self.principal,result.record_id,limit=1)[0].record)
        return SimpleNamespace(document=doc,version=version,chunk=chunk,embedding=embedding,
            snapshot_id=plan.snapshot.snapshot_id,records=records,entity=entity)

    def publish(self, *sources, remove=()):
        result=self.publications.publish(self.principal,tuple(r.revision_id for s in sources for r in s.records),
            expected_active_publication_id=self.active_id,published_at=NOW+timedelta(minutes=3),remove_record_ids=tuple(remove))
        self.active_id=result.publication_id
        return result

    def switch(self, target):
        result=self.publications.rollback(self.principal,target.publication_id,
            expected_active_publication_id=self.active_id,rolled_back_at=NOW+timedelta(minutes=4))
        self.active_id=result.publication_id
        return result

    def retrieve(self, source=None, principal=None):
        profile=make_plan(tenant_id=self.tenant).bundles[0].embedding
        return self.engine.retrieve(RetrievalRequest(query_text='循环水泵 BC-P-202 BC-P-101',
            query_vector=profile.vector,query_embedding_space_id=profile.embedding_space_id,
            principal=principal or self.principal,limits=RetrievalLimits(top_k=5),
            version_filter=VersionFilter(document_ids=frozenset({source.document.document_id}) if source else frozenset())))

    def test_draft_text_and_instances_follow_bidirectional_publication_switch(self):
        a=self.add_source('authoritative_source.txt','BC-P-101')
        self.assertEqual(self.retrieve().chunks,())
        self.assertEqual(self.sources.read(self.principal)['items'],[])
        v1=self.publish(a)
        b=self.add_source('homonym_report.txt','BC-P-202')
        self.assertEqual(self.retrieve(b).chunks,())
        self.assertEqual({x['document_id'] for x in self.sources.read(self.principal)['items']},{a.document.document_id})
        v2=self.publish(b)
        before=self.retrieve()
        self.assertEqual({c.citation.document_id for c in before.chunks},{a.document.document_id,b.document.document_id})
        self.assertIn(b.chunk.chunk_id,{h.chunk_id for h in before.trace.bm25_recall})
        embedding_count=self.query('MATCH (e:ChunkEmbedding) RETURN count(e) AS count')[0]['count']
        self.switch(v1)
        with self.assertRaises(GraphViewChanged): self.engine.validate_result(self.principal,before)
        self.assertEqual(self.retrieve(b).chunks,())
        with self.assertRaises(GraphViewChanged):
            self.sources.read(self.principal,document_id=b.document.document_id,version_id=b.version.version_id)
        self.assertEqual({c.citation.document_id for c in self.retrieve().chunks},{a.document.document_id})
        self.switch(v2)
        restored=self.retrieve()
        self.assertEqual({c.citation.document_id for c in restored.chunks},{a.document.document_id,b.document.document_id})
        self.assertEqual(self.query('MATCH (e:ChunkEmbedding) RETURN count(e) AS count')[0]['count'],embedding_count)
        self.assertEqual(self.query('MATCH (:KnowledgePublicationState)-[:HAS_PUBLICATION_ACTIVATION]->(a) RETURN count(a) AS count')[0]['count'],4)

    def test_same_document_old_text_restores_without_changing_upload_head(self):
        old=self.add_source('authoritative_source.txt','BC-P-101')
        v1=self.publish(old)
        new=self.add_source('homonym_report.txt','BC-P-202',previous=old)
        self.assertEqual({c.citation.version_id for c in self.retrieve().chunks},{old.version.version_id})
        v2=self.publish(new,remove=[r.record_id for r in old.records])
        self.assertEqual({c.citation.version_id for c in self.retrieve().chunks},{new.version.version_id})
        self.switch(v1)
        self.assertTrue(Neo4jPublishedGraphQualityService(self.driver,self.database).audit(self.principal).passed)
        self.assertTrue(Neo4jActivePublicationInventoryService(self.driver,self.database).list_active(self.principal).items)
        rows=self.sources.read(self.principal)['items']
        self.assertEqual([x['version_id'] for x in rows],[old.version.version_id])
        result=self.retrieve()
        self.assertEqual({c.citation.version_id for c in result.chunks},{old.version.version_id})
        self.assertIn(old.chunk.chunk_id,{h.chunk_id for h in result.trace.bm25_recall})
        self.assertEqual(self.query('MATCH (:Document)-[:ACTIVE_VERSION]->(v) RETURN v.version_id AS id')[0]['id'],new.version.version_id)
        self.switch(v2)
        self.assertEqual({c.citation.version_id for c in self.retrieve().chunks},{new.version.version_id})

    def test_empty_version_and_current_access_withdrawal_cannot_be_bypassed(self):
        a=self.add_source('authoritative_source.txt','BC-P-101')
        v1=self.publish(a)
        empty=self.publish(remove=[r.record_id for r in a.records])
        self.assertEqual(self.retrieve().chunks,())
        self.assertEqual(self.sources.read(self.principal)['items'],[])
        self.assertTrue(Neo4jPublishedGraphQualityService(self.driver,self.database).audit(self.principal).passed)
        self.assertEqual(Neo4jActivePublicationInventoryService(self.driver,self.database).list_active(self.principal).items,())
        self.switch(v1)
        self.assertTrue(self.retrieve().chunks)
        stranger=replace(self.principal,tenant_id='pump-other-tenant')
        self.assertEqual(self.sources.read(stranger)['items'],[])
        with self.assertRaises((KnowledgeReviewUnavailable,KnowledgePublicationConflict)):
            self.publications.rollback(stranger,v1.publication_id,expected_active_publication_id=v1.publication_id,rolled_back_at=NOW)
        self.switch(empty)
        self.query('MATCH (d:Document {document_id:$id}) SET d.access_groups=["restricted"]',id=a.document.document_id)
        with self.assertRaises((KnowledgeReviewUnavailable,KnowledgePublicationConflict)): self.switch(v1)
        self.query('MATCH (d:Document {document_id:$id}) SET d.access_groups=["members"],d.lifecycle_status="RETIRED",d.retirement_id="explicit-withdrawal"',id=a.document.document_id)
        with self.assertRaises((KnowledgeReviewUnavailable,KnowledgePublicationConflict)): self.switch(v1)
        self.assertEqual(self.sources.read(self.principal)['items'],[])

    def test_incomplete_vectors_and_changed_manifest_reject_activation(self):
        a=self.add_source('authoritative_source.txt','BC-P-101')
        v1=self.publish(a)
        empty=self.publish(remove=[r.record_id for r in a.records])
        self.query('MATCH (e:ChunkEmbedding {chunk_id:$id}) REMOVE e.vector',id=a.chunk.chunk_id)
        with self.assertRaises(KnowledgePublicationConflict): self.switch(v1)
        self.assertEqual(self.publications.active(self.principal).publication_id,empty.publication_id)
