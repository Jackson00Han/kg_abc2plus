"""Property ownership correction using only the authorized pump test package."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import ipaddress
import json
import os
from pathlib import Path
import unittest
from urllib.parse import urlparse

from neo4j import GraphDatabase

from graphrag_prod.api.knowledge_contracts import (
    PropertyAssignmentRequest, PropertyAssignmentApplyRequest,
    PropertyAssignmentResponse, ReviewBatchResponse,
)
from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import content_checksum, document_id, version_id, chunk_id, entity_id, chunk_embedding_id
from graphrag_prod.graph.provenance import Neo4jProvenanceStore
from graphrag_prod.ingestion import Neo4jEmbeddingIndexManager
from graphrag_prod.graph.schema import apply_schema
from graphrag_prod.graph.published_quality import Neo4jPublishedGraphQualityService
from graphrag_prod.knowledge.models import (
    EntityIdentity, EntityMentionRecord, EvidenceReference, AssertionRecord,
    RecordRevision, knowledge_record_id, ABoxRecordBatch, llm_candidate_trust,
)
from graphrag_prod.knowledge.store import Neo4jKnowledgeStore, KnowledgeConflict
from graphrag_prod.knowledge.review import (
    Neo4jKnowledgeReviewService, Neo4jKnowledgePublicationService,
    KnowledgeReviewUnavailable, KnowledgeAuthorizationError, ReviewRecordKind,
)
from graphrag_prod.knowledge.review_assessment import Neo4jReviewAssessmentService
from graphrag_prod.knowledge.trust import GovernanceStatus
from graphrag_prod.construction.literals import TBoxLiteralNormalizer
from graphrag_prod.ontology import TBoxVersion, Neo4jTBoxStore
from tests.fixtures.pump_ingestion import make_plan

PACKAGE = Path(__file__).resolve().parents[2] / 'src/graphrag_prod/playground/static/industrial-demo-v1'
NOW = datetime(2026, 9, 11, tzinfo=UTC)


class PropertyAssignmentNeo4jTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get('GRAPHRAG_ALLOW_DISPOSABLE_DB') != '1':
            raise RuntimeError('disposable database opt-in required')
        uri = os.environ['TEST_NEO4J_URI']
        if not ipaddress.ip_address(urlparse(uri).hostname).is_loopback:
            raise RuntimeError('loopback test database required')
        cls.database = os.environ['TEST_NEO4J_DATABASE']
        cls.driver = GraphDatabase.driver(uri, auth=(os.environ['TEST_NEO4J_USER'], os.environ['TEST_NEO4J_PASSWORD']),notifications_min_severity='OFF')
        rows, _, _ = cls.driver.execute_query('MATCH (n) RETURN count(n) AS count', database_=cls.database)
        if rows[0]['count']:
            cls.driver.close()
            raise RuntimeError('refusing nonempty database')
        apply_schema(cls.driver, cls.database)

    @classmethod
    def tearDownClass(cls):
        cls.driver.close()

    def setUp(self):
        self.driver.execute_query('MATCH (n) DETACH DELETE n', database_=self.database)
        self.tenant = 'pump-property-assignment-test'
        self.principal = Principal('reviewer', self.tenant, frozenset({'members'}),
            frozenset({'knowledge:review', 'knowledge:publish'}))
        self.review = Neo4jKnowledgeReviewService(self.driver, self.database)
        self.store = Neo4jKnowledgeStore(self.driver, self.database)
        self.tbox = TBoxVersion.from_mapping({**json.loads((PACKAGE/'ontology.json').read_text()),
            'tenant_id': self.tenant, 'status': 'DRAFT'})
        tboxes = Neo4jTBoxStore(self.driver, self.database)
        tboxes.import_version(self.tbox)
        tboxes.publish(self.tenant, self.tbox.tbox_id, expected_active_tbox_id=None)
        self.a, self.code_a = self.add_source('authoritative_source.txt', 'BC-P-101')
        self.b, self.code_b = self.add_source('homonym_report.txt', 'BC-P-202')
        index = Neo4jEmbeddingIndexManager(self.driver, self.database)
        generation = index.prepare(tenant_id=self.tenant,
            embedding_profile=make_plan(tenant_id=self.tenant).bundles[0].embedding,generation_version=1)
        index.activate(generation.generation_id,expected_active_generation_id=None)
        # Use opaque identity keys to verify identity-code display and search.
        self.a = self.approve(self.a, ReviewRecordKind.ENTITY_MENTION)
        self.b = self.approve(self.b, ReviewRecordKind.ENTITY_MENTION)
        self.code_a = self.approve(self.code_a, ReviewRecordKind.ASSERTION)
        self.code_b = self.approve(self.code_b, ReviewRecordKind.ASSERTION)
        # Reproduce a confirmed but wrong subject in the homonym source.
        self.fact = self.make_fact(self.b, 'RatedPower', '22.0', 'kW', 'wrong-power')
        wrong_mention = replace(self.b, revision=RecordRevision.next(
            knowledge_record_id(self.tenant,'ENTITY_MENTION','incorrect-source-owner'),0),
            entity=self.a.entity, trust=self.fact.trust)
        name_start=self.b.evidence.quoted_text.index('循环水泵')
        wrong_mention=replace(wrong_mention,evidence=replace(self.b.evidence,
            char_start=name_start,char_end=name_start+len('循环水泵'),quoted_text='循环水泵'))
        sibling_mention = replace(self.b, revision=RecordRevision.next(
            knowledge_record_id(self.tenant,'ENTITY_MENTION','sibling-source-owner'),0), trust=self.fact.trust)
        self.fact = replace(self.fact, subject=self.a.entity, subject_mention_revision_id=wrong_mention.revision_id)
        self.sibling = self.make_fact(sibling_mention, 'EquipmentCode', 'BC-P-202', None, 'sibling')
        self.store.persist_llm_candidates(ABoxRecordBatch(self.tenant,
            (wrong_mention,sibling_mention), (self.fact,self.sibling)))
        self.approve(wrong_mention, ReviewRecordKind.ENTITY_MENTION)

    def approve(self, record, kind):
        outcome = self.review.approve(self.principal, record_kind=kind, record_id=record.record_id,
            expected_revision=record.revision.revision, reviewed_at=NOW+timedelta(minutes=4),
            notes='已核对循环水泵测试包中的设备编号。')
        return self.review.revision_history(self.principal, outcome.record_id, limit=1)[0].record

    def add_source(self, name, code):
        base = make_plan(tenant_id=self.tenant).bundles[0]
        text = (PACKAGE/name).read_text()
        checksum = content_checksum(text)
        doc = replace(base.document, document_id=document_id(self.tenant, 'urn:test:assignment:'+name),
            canonical_uri='urn:test:assignment:'+name, title=name)
        ver_id = version_id(doc.document_id, checksum, checksum)
        version = replace(base.version, version_id=ver_id, document_id=doc.document_id,
            normalized_text=text, checksum=checksum, original_checksum=checksum)
        ck_id = chunk_id(ver_id, base.chunk.splitter_version, 0, 0, len(text), checksum)
        chunk = replace(base.chunk, chunk_id=ck_id, version_id=ver_id, document_id=doc.document_id,
            text=text, checksum=checksum, char_end=len(text))
        embedding = replace(base.embedding, chunk_id=ck_id,
            embedding_id=chunk_embedding_id(ck_id, base.embedding.embedding_space_id))
        bundle = replace(base, document=doc, version=version, chunk=chunk, embedding=embedding, activate_version=True)
        Neo4jProvenanceStore(self.driver, self.database).write_bundle(bundle)
        self.driver.execute_query('''MATCH (d:Document {document_id:$doc})-[:ACTIVE_VERSION]->(v:DocumentVersion)
            MATCH (v)-[:HAS_CHUNK]->(c:Chunk)
            CREATE (s:KnowledgeSnapshot {snapshot_id:$snapshot, tenant_id:$tenant, document_id:$doc,
                version_id:$version, profile_id:'pump-assignment:v1', build_state:'PUBLISHED', created_at:$now,
                expected_chunk_count:1,actual_chunk_count:1,manifest_hash:$manifest})
            CREATE (s)-[:OF_VERSION]->(v) CREATE (s)-[:INCLUDES_CHUNK]->(c)
            CREATE (d)-[:ACTIVE_SNAPSHOT]->(s)''', doc=doc.document_id, version=ver_id,
            snapshot='assignment:'+name, tenant=self.tenant, now=NOW,manifest=checksum, database_=self.database)
        key='llm-candidate:opaque-'+name.split('.')[0]
        entity = EntityIdentity(entity_id(self.tenant, 'Equipment', key), self.tenant, 'Equipment', key, '循环水泵', ())
        evidence = EvidenceReference(self.tenant, doc.document_id, ver_id, ck_id, 0, len(text), text,
            chunk.access_policy_id, chunk.access_policy_version, chunk.access_groups)
        trust = llm_candidate_trust(ontology_version_id=self.tbox.tbox_id, extractor_version='pump-offline:v1',
            prompt_version='pump-offline:v1', extracted_at=NOW)
        mention = EntityMentionRecord(RecordRevision.next(knowledge_record_id(self.tenant,'ENTITY_MENTION',name),0),
            self.tenant, entity, evidence, 1.0, trust, NOW)
        fact = self.make_fact(mention, 'EquipmentCode', code, None, name+'-code')
        self.store.persist_llm_candidates(ABoxRecordBatch(self.tenant, (mention,), (fact,)))
        return mention, fact

    def make_fact(self, mention, predicate, value, unit, key):
        definition = next(p for e in self.tbox.entity_types if e.name=='Equipment' for p in e.properties if p.name==predicate)
        literal = TBoxLiteralNormalizer().normalize(definition, raw_value=value, raw_unit=unit, valid_from=None, valid_to=None, observed_at=None)
        trust = llm_candidate_trust(ontology_version_id=self.tbox.tbox_id, extractor_version='pump-offline:v1',
            prompt_version='pump-offline:v1', extracted_at=NOW)
        return AssertionRecord(RecordRevision.next(knowledge_record_id(self.tenant,'ASSERTION',key),0),
            self.tenant, mention.entity, predicate, mention.evidence, mention.revision_id, 1.0, trust, NOW,
            literal_value=literal.raw_value, literal_semantics=literal)

    def selection(self, target=None, fact=None):
        target, fact = target or self.b, fact or self.fact
        return PropertyAssignmentApplyRequest(record_id=fact.record_id, expected_revision=fact.revision.revision,
            target_entity_id=target.entity.entity_id, target_record_id=target.record_id,
            target_expected_revision=target.revision.revision, notes='原文明确写明 BC-P-202，22 kW 属于该设备。')

    def read(self, query=''):
        return self.review.property_assignment(self.principal, PropertyAssignmentRequest(
            record_id=self.fact.record_id, expected_revision=1, query=query))

    def test_correction_retains_evidence_history_and_only_moves_selected_property(self):
        choices=self.read()
        PropertyAssignmentResponse.model_validate(choices)
        self.assertEqual(choices['current']['entity']['entity_id'], self.a.entity.entity_id)
        self.assertEqual({x['identity_properties'][0]['value'] for x in choices['items']}, {'BC-P-101','BC-P-202'})
        self.assertEqual([x['entity']['entity_id'] for x in self.read('BC-P-202')['items']], [self.b.entity.entity_id])
        before=Neo4jReviewAssessmentService(self.driver,self.database).assess(self.principal,self.fact.record_id,1)
        self.assertEqual(before.status,'READY')
        result=self.review.property_assignment(self.principal, self.selection(), reviewed_at=NOW+timedelta(minutes=2))
        ReviewBatchResponse.model_validate(result)
        mention_outcome=next(x for x in result['outcomes'] if x['record_kind']=='ENTITY_MENTION')
        derived=self.review.revision_history(self.principal,mention_outcome['record_id'],limit=1)[0].record
        self.assertEqual(derived.assignment_source_revision_id,self.fact.revision_id)
        self.assertEqual(derived.trust.extractor_version,self.fact.trust.extractor_version)
        history=self.review.revision_history(self.principal, self.fact.record_id, limit=10)
        updated, original = (x.record for x in history)
        self.assertEqual(original, self.fact)
        self.assertEqual(updated.subject, self.b.entity)
        self.assertEqual(updated.evidence, original.evidence)
        self.assertEqual(updated.literal_semantics, original.literal_semantics)
        self.assertEqual(updated.trust.status, GovernanceStatus.QUARANTINED)
        self.assertEqual(updated.trust.authority, original.trust.authority)
        self.assertIn(self.a.entity.entity_id, updated.trust.review_notes)
        self.assertIn(self.b.entity.entity_id, updated.trust.review_notes)
        self.assertEqual(self.review.revision_history(self.principal,self.sibling.record_id,limit=1)[0].record,self.sibling)
        self.assertEqual(self.review.revision_history(self.principal,self.b.record_id,limit=1)[0].record,self.b)
        assessment=Neo4jReviewAssessmentService(self.driver,self.database).assess(self.principal,updated.record_id,2)
        self.assertEqual(assessment.status,'READY')
        approved=self.approve(updated, ReviewRecordKind.ASSERTION)
        self.assertEqual(approved.subject,self.b.entity)
        publication = Neo4jKnowledgePublicationService(self.driver, self.database)
        candidates = publication.candidates(self.principal, limit=100)
        manifest = publication.publish(self.principal, tuple(x.item.record.revision_id for x in candidates),
            expected_active_publication_id=None, published_at=NOW+timedelta(minutes=5))
        self.assertIsNotNone(manifest)
        rows, _, _ = self.driver.execute_query(
            'MATCH (a:GovernedAssertionRevision {record_id:$record, governance_status:"PUBLISHED"}) RETURN a.subject_entity_id AS subject',
            record=self.fact.record_id, database_=self.database)
        self.assertEqual({row['subject'] for row in rows}, {self.b.entity.entity_id})
        quality = Neo4jPublishedGraphQualityService(self.driver, self.database).audit(self.principal)
        self.assertTrue(quality.passed, [(issue.code, issue.detail) for issue in quality.issues])
        with self.assertRaises((KnowledgeConflict,KnowledgeReviewUnavailable)):
            self.review.property_assignment(self.principal,self.selection(),reviewed_at=NOW+timedelta(minutes=3))

    def test_target_revision_identity_conflict_and_acl_are_enforced_atomically(self):
        for changes in ({'target_expected_revision':99}, {'target_entity_id':self.a.entity.entity_id}):
            with self.assertRaises((KnowledgeConflict,KnowledgeReviewUnavailable)):
                self.review.property_assignment(self.principal,self.selection().model_copy(update=changes),reviewed_at=NOW+timedelta(minutes=2))
        # An identity-number attribute itself cannot move to a different known number.
        with self.assertRaises(KnowledgeConflict):
            self.review.property_assignment(self.principal,self.selection(self.a,self.sibling),reviewed_at=NOW+timedelta(minutes=2))
        for principal in (replace(self.principal,tenant_id='other-tenant'), replace(self.principal,groups=frozenset({'private'}))):
            with self.assertRaises(KnowledgeReviewUnavailable):
                self.review.property_assignment(principal,PropertyAssignmentRequest(record_id=self.fact.record_id,expected_revision=1))
            with self.assertRaises((KnowledgeConflict,KnowledgeReviewUnavailable)):
                self.review.property_assignment(principal,self.selection(),reviewed_at=NOW+timedelta(minutes=2))
        with self.assertRaises(KnowledgeAuthorizationError):
            self.review.property_assignment(replace(self.principal,capabilities=frozenset()),self.selection(),reviewed_at=NOW)
        self.assertEqual(len(self.review.revision_history(self.principal,self.fact.record_id,limit=10)),1)

    def test_inaccessible_and_retired_targets_do_not_appear_or_accept_assignment(self):
        self.driver.execute_query('MATCH (d:Document {document_id:$id}) SET d.access_groups=["private"]',
            id=self.b.evidence.document_id,database_=self.database)
        # The source is the homonym document too, so use a fact from the other visible source.
        visible=self.make_fact(self.a,'RatedPower','37.5','kW','visible-power')
        visible_mention=replace(self.a,revision=RecordRevision.next(
            knowledge_record_id(self.tenant,'ENTITY_MENTION','visible-power-source'),0),trust=visible.trust)
        visible=replace(visible,subject_mention_revision_id=visible_mention.revision_id)
        self.store.persist_llm_candidates(ABoxRecordBatch(self.tenant,(visible_mention,),(visible,)))
        request=PropertyAssignmentRequest(record_id=visible.record_id,expected_revision=1)
        choices=self.review.property_assignment(self.principal,request)
        self.assertNotIn(self.b.entity.entity_id,[x['entity']['entity_id'] for x in choices['items']])
        with self.assertRaises((KnowledgeConflict,KnowledgeReviewUnavailable)):
            self.review.property_assignment(self.principal,self.selection(self.b,visible),reviewed_at=NOW+timedelta(minutes=2))
        self.driver.execute_query('MATCH (d:Document {document_id:$id}) SET d.access_groups=["members"] WITH d MATCH (d)-[p:ACTIVE_SNAPSHOT]->() DELETE p',
            id=self.b.evidence.document_id,database_=self.database)
        self.assertNotIn(self.b.entity.entity_id,[x['entity']['entity_id'] for x in self.review.property_assignment(self.principal,request)['items']])
        with self.assertRaises((KnowledgeConflict,KnowledgeReviewUnavailable)):
            self.review.property_assignment(self.principal,self.selection(self.b,visible),reviewed_at=NOW+timedelta(minutes=2))
