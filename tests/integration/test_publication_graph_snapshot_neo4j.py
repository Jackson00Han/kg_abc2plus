"""Pump-only graph, ranking and evidence checks across publication switches."""

from dataclasses import replace
from datetime import timedelta
import unittest

from graphrag_prod.construction.literals import TBoxLiteralNormalizer
from graphrag_prod.domain.ids import entity_id
from graphrag_prod.graph.browse_models import GraphBrowseQuery, GraphViewChanged
from graphrag_prod.graph.browsing import Neo4jPublishedGraphBrowser
from graphrag_prod.knowledge.models import (
    ABoxRecordBatch, AssertionRecord, EntityIdentity, EntityMentionRecord,
    RecordRevision, knowledge_record_id,
)
from graphrag_prod.knowledge.review import ReviewRecordKind
from graphrag_prod.retrieval.engine import (
    ADJACENT_QUERY, CORPUS_STATE_QUERY, GRAPH_EXPANSION_QUERY, HYDRATE_QUERY,
)
from graphrag_prod.retrieval import RetrievalLimits, RetrievalRequest
from tests.integration import test_publication_snapshot_neo4j as fixture


class PublicationGraphSnapshotNeo4jTests(unittest.TestCase):
    # Reuse fixture setup without inheriting and rerunning its test methods.
    setUpClass = classmethod(fixture.PublicationSnapshotNeo4jTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.PublicationSnapshotNeo4jTests.tearDownClass.__func__)
    setUp = fixture.PublicationSnapshotNeo4jTests.setUp
    query = fixture.PublicationSnapshotNeo4jTests.query
    add_source = fixture.PublicationSnapshotNeo4jTests.add_source
    publish = fixture.PublicationSnapshotNeo4jTests.publish
    switch = fixture.PublicationSnapshotNeo4jTests.switch
    retrieve = fixture.PublicationSnapshotNeo4jTests.retrieve

    def add_site_and_power(self, source):
        """All values and relationships are explicitly in authoritative_source.txt."""
        equipment_mention = source.records[0]
        evidence = equipment_mention.evidence
        # Persist as candidates, then use the real reviewer and publisher paths.
        trust = fixture.llm_candidate_trust(ontology_version_id=self.tbox.tbox_id,
            extractor_version='pump-offline:v1', prompt_version='pump-offline:v1',
            extracted_at=fixture.NOW)
        def located(prefix, name):
            start = evidence.quoted_text.index(prefix + name) + len(prefix)
            return replace(evidence, char_start=evidence.char_start + start,
                char_end=evidence.char_start + start + len(name), quoted_text=name)

        equipment = EntityMentionRecord(
            RecordRevision.next(knowledge_record_id(self.tenant, 'ENTITY_MENTION', source.version.version_id + ':equipment-details'), 0),
            self.tenant, equipment_mention.entity, located('设备：', '循环水泵'), 1.0, trust, fixture.NOW)
        site_key = 'llm-candidate:pump-site-one'
        site = EntityIdentity(entity_id(self.tenant, 'Site', site_key), self.tenant,
            'Site', site_key, '北辰水处理一号泵站', ())
        mention = EntityMentionRecord(
            RecordRevision.next(knowledge_record_id(self.tenant, 'ENTITY_MENTION', source.version.version_id + ':site'), 0),
            self.tenant, site, located('场所：', '北辰水处理一号泵站'), 1.0, trust, fixture.NOW)
        relationship = AssertionRecord(
            RecordRevision.next(knowledge_record_id(self.tenant, 'ASSERTION', source.version.version_id + ':installed'), 0),
            self.tenant, equipment.entity, 'INSTALLED_AT', evidence, equipment.revision_id, 1.0, trust, fixture.NOW,
            object_entity=site, object_mention_revision_id=mention.revision_id)
        definition = next(p for e in self.tbox.entity_types if e.name == 'Equipment'
                          for p in e.properties if p.name == 'RatedPower')
        value = TBoxLiteralNormalizer().normalize(definition, raw_value='37.5', raw_unit='kW',
            valid_from=None, valid_to=None, observed_at=None)
        power = AssertionRecord(
            RecordRevision.next(knowledge_record_id(self.tenant, 'ASSERTION', source.version.version_id + ':power'), 0),
            self.tenant, equipment.entity, 'RatedPower', evidence, equipment.revision_id, 1.0, trust, fixture.NOW,
            literal_value=value.raw_value, literal_semantics=value)
        self.store.persist_llm_candidates(ABoxRecordBatch(self.tenant, (equipment, mention), (relationship, power)))
        for record, kind in ((equipment, ReviewRecordKind.ENTITY_MENTION), (mention, ReviewRecordKind.ENTITY_MENTION),
                             (relationship, ReviewRecordKind.ASSERTION), (power, ReviewRecordKind.ASSERTION)):
            decision = self.review.approve(self.principal, record_kind=kind, record_id=record.record_id,
                expected_revision=1, reviewed_at=fixture.NOW + timedelta(minutes=2),
                notes='核对循环水泵测试包原文中的泵站、安装关系及额定功率。')
            source.records.append(self.review.revision_history(self.principal, decision.record_id, limit=1)[0].record)
        return site

    def query_parameters(self):
        state = self.query(CORPUS_STATE_QUERY, tenant_id=self.tenant)[0]
        return dict(tenant_id=self.tenant, groups=sorted(self.principal.groups),
            corpus_revision=state['corpus_revision'], generation_id=state['generation_id'],
            embedding_space_id=state['embedding_space_id'],
            knowledge_publication_id=state['knowledge_publication_id'],
            knowledge_activation_generation=state['knowledge_activation_generation'],
            document_ids=[], version_ids=[], published_before=None)

    def test_entities_relationship_attributes_and_evidence_restore_together(self):
        baseline = self.add_source('homonym_report.txt', 'BC-P-202')
        v1 = self.publish(baseline)
        later = self.add_source('authoritative_source.txt', 'BC-P-101')
        site = self.add_site_and_power(later)
        browser = Neo4jPublishedGraphBrowser(self.driver, self.database)
        query = GraphBrowseQuery(page_size=100)
        draft_view = browser.query(self.principal, query)
        self.assertEqual({n['entity_id'] for n in draft_view['nodes']}, {baseline.entity.entity_id})
        v2 = self.publish(later)
        current = browser.query(self.principal, query)
        self.assertEqual({n['entity_id'] for n in current['nodes']},
                         {baseline.entity.entity_id, later.entity.entity_id, site.entity_id})
        self.assertEqual({e['predicate'] for e in current['edges']}, {'INSTALLED_AT'})
        self.assertEqual({e['predicate'] for e in current['literals']}, {'EquipmentCode', 'RatedPower'})
        evidence_id = current['edges'][0]['revision_id']
        evidence = browser.evidence(self.principal, (evidence_id,), view_token=current['view_token'])
        self.assertEqual(evidence['items'][0]['evidence']['citation']['version_id'], later.version.version_id)
        self.switch(v1)
        old = browser.query(self.principal, query)
        self.assertEqual({n['entity_id'] for n in old['nodes']}, {baseline.entity.entity_id})
        self.assertEqual(old['edges'], ())
        self.assertEqual({e['predicate'] for e in old['literals']}, {'EquipmentCode'})
        with self.assertRaises(GraphViewChanged):
            browser.evidence(self.principal, (evidence_id,), view_token=current['view_token'])
        self.switch(v2)
        restored = browser.query(self.principal, query)
        self.assertEqual(restored['nodes'], current['nodes'])
        self.assertEqual(restored['edges'], current['edges'])
        self.assertEqual(restored['literals'], current['literals'])
        self.assertGreater(restored['pin']['activation_generation'], current['pin']['activation_generation'])

    def test_graph_degree_and_hydration_ignore_unpublished_upload_head(self):
        first = self.add_source('authoritative_source.txt', 'BC-P-101')
        self.publish(first)
        second = self.add_source('maintenance_report.txt', 'BC-P-101')
        self.publish(second)
        draft = self.add_source('maintenance_report.txt', 'BC-P-101', previous=first)
        parameters = self.query_parameters()
        edges = self.query(GRAPH_EXPANSION_QUERY, **parameters,
                           seed_id=first.chunk.chunk_id, entity_limit=20, edge_limit=100)
        self.assertEqual({r['chunk_id'] for r in edges}, {second.chunk.chunk_id})
        self.assertEqual({r['entity_degree'] for r in edges}, {2})
        self.assertEqual(self.query(HYDRATE_QUERY, **parameters, chunk_ids=[draft.chunk.chunk_id]), [])
        self.assertEqual({r['chunk_id'] for r in self.query(HYDRATE_QUERY, **parameters,
            chunk_ids=[first.chunk.chunk_id, second.chunk.chunk_id])}, {first.chunk.chunk_id, second.chunk.chunk_id})
        # No neighbor from the new upload's snapshot may be used for this old anchor.
        self.assertEqual(self.query(ADJACENT_QUERY, **parameters, anchor_ids=[first.chunk.chunk_id],
            adjacent_window=1, limit=10), [])

    def test_bm25_scan_cutoff_is_applied_inside_published_source_scope(self):
        first = self.add_source('authoritative_source.txt', 'BC-P-101')
        self.publish(first)
        draft = self.add_source('homonym_report.txt', 'BC-P-202')
        self.query('CALL db.index.fulltext.awaitEventuallyConsistentIndexRefresh()')
        profile = fixture.make_plan(tenant_id=self.tenant).bundles[0].embedding
        # The draft contains repeated BC-P-202; neither it nor its code may take
        # the one-row BM25 recall window away from the published document.
        result = self.engine.retrieve(RetrievalRequest(query_text='循环水泵 BC-P-202',
            query_vector=profile.vector, query_embedding_space_id=profile.embedding_space_id,
            principal=self.principal, limits=RetrievalLimits(top_k=1, anchor_k=1,
                bm25_recall_k=1, bm25_scan_k=1)))
        self.assertEqual(tuple(hit.chunk_id for hit in result.trace.bm25_recall), (first.chunk.chunk_id,))
        self.assertNotIn(draft.chunk.chunk_id, result.trace.selected_chunk_ids)

    def test_pinned_context_rejects_same_version_reactivation_and_acl_change(self):
        first = self.add_source('authoritative_source.txt', 'BC-P-101')
        v1 = self.publish(first)
        captured = self.retrieve()
        self.engine.validate_result(self.principal, captured)
        second = self.add_source('homonym_report.txt', 'BC-P-202')
        v2 = self.publish(second)
        self.switch(v1)
        with self.assertRaises(GraphViewChanged):
            self.engine.validate_result(self.principal, captured)
        refreshed = self.retrieve()
        self.query('MATCH (d:Document {document_id:$id}) SET d.access_groups=["restricted"]',
                   id=first.document.document_id)
        with self.assertRaises(GraphViewChanged):
            self.engine.validate_result(self.principal, refreshed)


if __name__ == '__main__':
    unittest.main()
