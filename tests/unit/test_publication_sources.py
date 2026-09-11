"""Release scopes retain exact pump evidence and reject incomplete activation."""
from copy import deepcopy
import hashlib
import unittest
from unittest.mock import patch

from graphrag_prod.domain.access import Principal
from graphrag_prod.knowledge.review import KnowledgePublicationConflict
from graphrag_prod.knowledge.publication_sources import (
    MAX_SOURCE_CHARACTERS, canonical_json, compare_source_summaries,
    load_sources_tx, load_publication_sources_tx, prepare_publication_index_tx,
    require_embedding_coverage_tx, source_manifest_hash,
)

TEXT = '循环水泵 BC-P-202 额定功率 22.0 kW'
DIGEST = hashlib.sha256(TEXT.encode()).hexdigest()
PRINCIPAL = Principal('reviewer', 'pump-test', frozenset({'pump-readers'}), frozenset({'knowledge:publish'}))


class Rows(list):
    def single(self):
        return self[0] if self else None

    def consume(self):
        return None


class Tx:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def run(self, query, **parameters):
        self.calls.append((query, parameters))
        return Rows(self.results.pop(0))


def source_results():
    return (
        [{'source_characters': len(TEXT), 'chunk_characters': len(TEXT), 'chunks': 1}],
        [{'version_id': 'v1', 'checksum': DIGEST, 'text': TEXT}],
        [{'snapshot_id': 's1', 'document_id': 'd1', 'version_id': 'v1',
          'snapshot_manifest_hash': 'b' * 64, 'version_checksum': DIGEST,
          'expected_chunk_count': 1, 'title': '循环水泵', 'chunk_id': 'c1',
          'chunk_checksum': DIGEST, 'char_start': 0, 'char_end': len(TEXT), 'chunk_text': TEXT}],
    )


def sources():
    return load_sources_tx(Tx(*source_results()), PRINCIPAL, ('s1',))


class PublicationSourcesTests(unittest.TestCase):
    def test_manifest_retains_exact_sources_and_embedding_configuration(self):
        original = sources()
        first = source_manifest_hash(original, 'space1')
        self.assertEqual(first, source_manifest_hash(deepcopy(original), 'space1'))
        changed = deepcopy(original)
        changed[0]['chunks'][0]['char_end'] += 1
        self.assertNotEqual(first, source_manifest_hash(changed, 'space1'))
        self.assertNotEqual(first, source_manifest_hash(original, 'space2'))
        self.assertEqual(original[0]['chunks'][0]['checksum'], DIGEST)

    def test_document_and_chunk_tampering_are_rejected(self):
        document = deepcopy(source_results())
        document[1][0]['text'] += '篡改'
        with self.assertRaises(KnowledgePublicationConflict):
            load_sources_tx(Tx(*document), PRINCIPAL, ('s1',))
        chunk = deepcopy(source_results())
        chunk[2][0]['chunk_text'] += '篡改'
        with self.assertRaises(KnowledgePublicationConflict):
            load_sources_tx(Tx(*chunk), PRINCIPAL, ('s1',))

    def test_missing_and_duplicate_chunks_never_form_partial_release(self):
        missing = deepcopy(source_results())
        missing[2][0]['expected_chunk_count'] = 2
        with self.assertRaises(KnowledgePublicationConflict):
            load_sources_tx(Tx(*missing), PRINCIPAL, ('s1',))
        duplicate = deepcopy(source_results())
        duplicate[2].append(deepcopy(duplicate[2][0]))
        with self.assertRaises(KnowledgePublicationConflict):
            load_sources_tx(Tx(*duplicate), PRINCIPAL, ('s1',))
        with self.assertRaises(KnowledgePublicationConflict):
            load_sources_tx(Tx(*source_results()), PRINCIPAL, ('s1', 'missing'))

    def test_full_text_budget_is_checked_before_reading_text(self):
        tx = Tx([{'source_characters': MAX_SOURCE_CHARACTERS + 1, 'chunk_characters': 0, 'chunks': 1}])
        with self.assertRaises(KnowledgePublicationConflict):
            load_sources_tx(tx, PRINCIPAL, ('s1',))
        self.assertEqual(len(tx.calls), 1)

    def test_v4_seal_rejects_modified_scope_and_accepts_legacy_bindings(self):
        manifest = sources()
        properties = dict(publication_id='p1', manifest_version=4,
                          source_manifest_json=canonical_json(manifest), source_snapshot_ids=['s1'],
                          source_manifest_hash=source_manifest_hash(manifest, 'space1'),
                          embedding_space_id='space1', source_document_count=1, source_chunk_count=1)
        outer = dict(tenant_id=PRINCIPAL.tenant_id, ontology_version_id=None,
                     base_publication_id=None, source_revision_ids=[], published_revision_ids=[],
                     removed_record_ids=[], replaced_record_ids=[], snapshot_ids=['s1'])
        properties['manifest_hash'] = hashlib.sha256(canonical_json(dict(
            outer, source_manifest_hash=properties['source_manifest_hash'], embedding_space_id='space1')).encode()).hexdigest()
        legacy = {'publication_id': 'p1', 'manifest_version': 3,
                  'manifest_hash': hashlib.sha256(canonical_json(outer).encode()).hexdigest()}
        def tx():
            return Tx([{'snapshot_id': 's1', 'tenant_id': PRINCIPAL.tenant_id}], *source_results())
        self.assertEqual(load_publication_sources_tx(tx(), PRINCIPAL, properties), manifest)
        self.assertEqual(load_publication_sources_tx(tx(), PRINCIPAL, legacy), manifest)
        properties['source_chunk_count'] = 2
        with self.assertRaises(KnowledgePublicationConflict):
            load_publication_sources_tx(tx(), PRINCIPAL, properties)

    def test_wrong_tenant_binding_is_rejected_before_source_read(self):
        tx = Tx([{'snapshot_id': 's1', 'tenant_id': 'another-tenant'}])
        with self.assertRaises(KnowledgePublicationConflict):
            load_publication_sources_tx(tx, PRINCIPAL, {'publication_id': 'p1'})
        self.assertEqual(len(tx.calls), 1)

    def test_restore_requires_ready_compatible_complete_embeddings(self):
        manifest = sources()
        cases = (
            (Tx([]), 'INDEX_NOT_READY'),
            (Tx([{'space': 'other', 'dimensions': 3}]), 'EMBEDDING_SPACE_CHANGED'),
            (Tx([{'space': 'space1', 'dimensions': 3}], [{'covered': 0}]), 'INDEX_NOT_READY'),
        )
        for tx, code in cases:
            with self.subTest(code=code), self.assertRaises(KnowledgePublicationConflict) as failure:
                require_embedding_coverage_tx(tx, PRINCIPAL, manifest, 'space1')
            self.assertEqual(failure.exception.issue.reason, code)
        self.assertEqual(require_embedding_coverage_tx(
            Tx([{'space': 'space1', 'dimensions': 3}], [{'covered': 1}]), PRINCIPAL, manifest, 'space1'), 'space1')

    def test_empty_release_needs_no_index_and_versions_compare_by_identity(self):
        tx = Tx()
        self.assertEqual(load_sources_tx(tx, PRINCIPAL, ()), ())
        self.assertIsNone(require_embedding_coverage_tx(tx, PRINCIPAL, ()))
        old = {'document_id': 'd1', 'version_id': 'v1', 'title': '循环水泵'}
        new = {**old, 'version_id': 'v2'}
        scope = compare_source_summaries([old], [new])
        self.assertEqual(scope, {'added': [new], 'removed': [old], 'unchanged_count': 0})

    def test_legacy_activation_binding_does_not_rewrite_original_manifest(self):
        tx = Tx([])
        with (patch('graphrag_prod.knowledge.publication_sources.load_publication_sources_tx', return_value=sources()),
              patch('graphrag_prod.knowledge.publication_sources.require_embedding_coverage_tx', return_value='space1'),
              patch('graphrag_prod.knowledge.publication_sources.index_publication_sources_tx')):
            prepare_publication_index_tx(tx, PRINCIPAL, {'publication_id': 'p1', 'manifest_version': 3}, bind_legacy=True)
        self.assertEqual(tx.calls[0][1]['space'], 'space1')
        self.assertNotIn('manifest_hash', tx.calls[0][0])
        self.assertNotIn('ACTIVE_KNOWLEDGE_PUBLICATION', tx.calls[0][0])
