"""Pump-only source seal, historical revision, migration, and API projection checks."""
from datetime import timedelta
import hashlib
import unittest

from graphrag_prod.api.knowledge_contracts import PublicationPreviewResponse
from graphrag_prod.api.publication_comparison_contracts import PublicationComparisonResponse
from graphrag_prod.knowledge.publication_comparison import publication_comparison
from graphrag_prod.knowledge.publication_sources import canonical_json, prepare_publication_index_tx
from graphrag_prod.knowledge.review import KnowledgePublicationConflict, ReviewRecordKind
from tests.integration import test_publication_snapshot_neo4j as fixture


class PublicationSnapshotIntegrityNeo4jTests(unittest.TestCase):
    # Reuse only setup/helpers; the fixture's test methods are not inherited.
    setUpClass = classmethod(fixture.PublicationSnapshotNeo4jTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.PublicationSnapshotNeo4jTests.tearDownClass.__func__)
    setUp = fixture.PublicationSnapshotNeo4jTests.setUp
    query = fixture.PublicationSnapshotNeo4jTests.query
    add_source = fixture.PublicationSnapshotNeo4jTests.add_source
    publish = fixture.PublicationSnapshotNeo4jTests.publish
    switch = fixture.PublicationSnapshotNeo4jTests.switch
    retrieve = fixture.PublicationSnapshotNeo4jTests.retrieve

    def test_v4_seal_tampering_blocks_restore_without_changing_current_version(self):
        source = self.add_source('authoritative_source.txt', 'BC-P-101')
        v1 = self.publish(source)
        empty = self.publish(remove=[record.record_id for record in source.records])
        original = self.query('MATCH (p:KnowledgePublication {publication_id:$id}) RETURN p.source_manifest_json AS text',
                              id=v1.publication_id)[0]['text']
        self.query('MATCH (p:KnowledgePublication {publication_id:$id}) SET p.source_manifest_json="[]"',
                   id=v1.publication_id)
        self.assertIsNone(self.publications.get(self.principal, v1.publication_id))
        with self.assertRaises(KnowledgePublicationConflict):
            self.switch(v1)
        self.assertEqual(self.publications.active(self.principal).publication_id, empty.publication_id)
        self.assertEqual(self.retrieve().chunks, ())
        self.query('MATCH (p:KnowledgePublication {publication_id:$id}) SET p.source_manifest_json=$text',
                   id=v1.publication_id, text=original)
        self.switch(v1)
        self.assertEqual({item.citation.document_id for item in self.retrieve().chunks}, {source.document.document_id})

    def test_chunk_positions_cannot_use_substring_truncation_or_null(self):
        source = self.add_source('authoritative_source.txt', 'BC-P-101')
        v1 = self.publish(source)
        empty = self.publish(remove=[record.record_id for record in source.records])
        mutations = (
            {'start': 0, 'end': len(source.chunk.text) + 1},
            {'start': None, 'end': len(source.chunk.text)},
            {'start': 0, 'end': None},
        )
        for bounds in mutations:
            with self.subTest(bounds=bounds):
                self.query('MATCH (c:Chunk {chunk_id:$id}) SET c.char_start=$start,c.char_end=$end',
                           id=source.chunk.chunk_id, **bounds)
                self.assertIsNone(self.publications.get(self.principal, v1.publication_id))
                with self.assertRaises(KnowledgePublicationConflict):
                    self.switch(v1)
                self.assertEqual(self.publications.active(self.principal).publication_id, empty.publication_id)
        self.query('MATCH (c:Chunk {chunk_id:$id}) SET c.char_start=0,c.char_end=$end',
                   id=source.chunk.chunk_id, end=len(source.chunk.text))
        self.switch(v1)
        self.assertTrue(self.retrieve().chunks)

    def test_v3_migration_rebuilds_scope_and_keeps_original_manifest_hash(self):
        source = self.add_source('authoritative_source.txt', 'BC-P-101')
        v1 = self.publish(source)
        empty = self.publish(remove=[record.record_id for record in source.records])
        original = dict(self.query('MATCH (p:KnowledgePublication {publication_id:$id}) RETURN p{.*} AS p',
                                   id=v1.publication_id)[0]['p'])
        legacy_manifest = {name: original.get(name) for name in (
            'tenant_id', 'ontology_version_id', 'base_publication_id', 'source_revision_ids',
            'published_revision_ids', 'removed_record_ids', 'replaced_record_ids')}
        legacy_manifest['snapshot_ids'] = original['source_snapshot_ids']
        legacy_hash = hashlib.sha256(canonical_json(legacy_manifest).encode()).hexdigest()
        self.query('''MATCH (p:KnowledgePublication {publication_id:$id})
            SET p.manifest_version=3,p.manifest_hash=$hash
            REMOVE p.source_manifest_json,p.source_manifest_hash,p.source_document_count,
                   p.source_chunk_count,p.source_snapshot_ids,p.embedding_space_id''',
            id=v1.publication_id, hash=legacy_hash)
        self.query('MATCH (c:Chunk {chunk_id:$id}) REMOVE c.publication_scope', id=source.chunk.chunk_id)
        legacy = dict(self.query('MATCH (p:KnowledgePublication {publication_id:$id}) RETURN p{.*} AS p',
                                id=v1.publication_id)[0]['p'])
        with self.driver.session(database=self.database) as session:
            session.execute_write(prepare_publication_index_tx, self.principal, legacy)
        self.assertTrue(self.query('MATCH (c:Chunk {chunk_id:$id}) RETURN c.publication_scope AS scope',
                                   id=source.chunk.chunk_id)[0]['scope'])
        self.assertEqual(self.publications.active(self.principal).publication_id, empty.publication_id)
        self.assertEqual(self.publications.get(self.principal, v1.publication_id).source_document_count, 1)
        self.switch(v1)
        binding = self.query('''MATCH (p:KnowledgePublication {publication_id:$id})
            RETURN p.manifest_hash AS hash,p.legacy_embedding_space_id AS space,p.source_manifest_json AS manifest''',
            id=v1.publication_id)[0]
        self.assertEqual(binding['hash'], legacy_hash)
        self.assertEqual(binding['space'], source.embedding.embedding_space_id)
        self.assertIsNone(binding['manifest'])
        self.switch(empty)
        self.switch(v1)
        self.assertTrue(self.retrieve().chunks)

    def test_historical_fact_restores_after_current_review_head_advances(self):
        source = self.add_source('authoritative_source.txt', 'BC-P-101')
        v1 = self.publish(source)
        record_id = source.records[1].record_id
        current = self.review.revision_history(self.principal, record_id, limit=1)[0].record
        outcome = self.review.quarantine(self.principal, record_kind=ReviewRecordKind.ASSERTION,
            record_id=record_id, expected_revision=current.revision.revision,
            reviewed_at=fixture.NOW + timedelta(minutes=5), notes='复核循环水泵测试记录，保留当前已发布版本。')
        self.assertNotEqual(outcome.revision_id, current.revision_id)
        self.assertIn(current.revision_id, self.publications.get(self.principal, v1.publication_id).published_revision_ids)
        empty = self.publish(remove=[record.record_id for record in source.records])
        self.switch(v1)
        self.assertEqual(self.query('''MATCH (:KnowledgeRecordHead {record_id:$id})-[:CURRENT_REVISION]->(r)
            RETURN r.revision_id AS id''', id=record_id)[0]['id'], outcome.revision_id)
        self.assertTrue(self.retrieve().chunks)
        self.switch(empty)
        self.assertEqual(self.retrieve().chunks, ())

    def test_preview_and_comparison_report_exact_document_scope(self):
        first = self.add_source('authoritative_source.txt', 'BC-P-101')
        v1 = self.publish(first)
        second = self.add_source('homonym_report.txt', 'BC-P-202')
        preview = PublicationPreviewResponse.model_validate(self.publications.publish(self.principal,
            tuple(record.revision_id for record in second.records),
            expected_active_publication_id=v1.publication_id, published_at=fixture.NOW + timedelta(minutes=3),
            preview_only=True))
        self.assertEqual([(item.document_id, item.version_id) for item in preview.source_scope.added],
                         [(second.document.document_id, second.version.version_id)])
        self.assertEqual(preview.source_scope.removed, [])
        self.assertEqual(preview.source_scope.unchanged_count, 1)
        self.assertEqual(self.publications.active(self.principal).publication_id, v1.publication_id)
        self.assertEqual(self.query('MATCH (p:KnowledgePublication) RETURN count(p) AS count')[0]['count'], 1)
        v2 = self.publications.publish(self.principal, tuple(record.revision_id for record in second.records),
            expected_active_publication_id=v1.publication_id, published_at=fixture.NOW + timedelta(minutes=3),
            expected_preview_hash=preview.preview_hash)
        self.active_id = v2.publication_id
        comparison = PublicationComparisonResponse.model_validate(publication_comparison(
            self.publications, self.principal, v1.publication_id, v2.publication_id))
        self.assertEqual(comparison.source_scope.added, [])
        self.assertEqual([(item.document_id, item.version_id) for item in comparison.source_scope.removed],
                         [(second.document.document_id, second.version.version_id)])
        self.assertEqual(comparison.source_scope.unchanged_count, 1)

    def test_empty_preview_removes_text_and_instances_together(self):
        source = self.add_source('authoritative_source.txt', 'BC-P-101')
        v1 = self.publish(source)
        preview = PublicationPreviewResponse.model_validate(self.publications.publish(self.principal, (),
            remove_record_ids=tuple(record.record_id for record in source.records),
            expected_active_publication_id=v1.publication_id, published_at=fixture.NOW + timedelta(minutes=3),
            preview_only=True))
        self.assertEqual(preview.records_after, [])
        self.assertEqual(preview.source_scope.added, [])
        self.assertEqual([item.document_id for item in preview.source_scope.removed], [source.document.document_id])
        self.assertEqual(preview.source_scope.unchanged_count, 0)
        self.assertEqual(self.publications.active(self.principal).publication_id, v1.publication_id)
