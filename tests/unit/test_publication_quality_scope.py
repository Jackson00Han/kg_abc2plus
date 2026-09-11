"""Published pump snapshots stay independent of newer working revisions."""
import json
from dataclasses import replace
from pathlib import Path
import unittest

from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import mention_id
from graphrag_prod.graph.published_inventory import (
    ActivePublicationInventoryConflict, Neo4jActivePublicationInventoryService,
    _decode_item, _ITEMS_QUERY,
)
from graphrag_prod.graph.published_quality import (
    Neo4jPublishedGraphQualityService, PublishedGraphQualityLimits,
    _audit_revision, _IssueCollector, _publication_boundary, _REVISIONS_QUERY,
    _expected_navigation_mention_id,
)
from graphrag_prod.graph.published_quality_history import (
    _publication_acl_requirements, _report_document, _report_from_json,
    PublishedGraphQualityHistoryConflict,
)
from graphrag_prod.ontology import TBoxVersion
from graphrag_prod.knowledge.review import _stable_id
from tests.unit.test_published_quality import _state, _driver
from tests.unit.test_published_inventory import _Tx


class PublicationQualityScopeTests(unittest.TestCase):
    def setUp(self):
        self.principal = Principal('pump-reviewer', 'tenant-industrial',
            frozenset({'members'}), frozenset({'knowledge:quality'}))
        package = Path(__file__).resolve().parents[2] / 'src/graphrag_prod/playground/static/industrial-demo-v1'
        self.tbox = TBoxVersion.from_mapping({**json.loads((package/'ontology.json').read_text()),
            'tenant_id': self.principal.tenant_id, 'status': 'PUBLISHED'})

    def empty_report(self):
        state = _state((), tbox_value=self.tbox)
        return Neo4jPublishedGraphQualityService(
            _driver(state=state, revisions=[], entities=[])).audit(self.principal)

    def test_empty_publication_has_valid_quality_and_inventory(self):
        report = self.empty_report()
        self.assertTrue(report.passed)
        self.assertEqual(dict(report.counts)['revisions'], 0)
        manifest = dict(manifest_revision_ids=[], membership_count=0,
            distinct_revision_count=0, membership_revision_ids=[], valid_revision_count=0)
        result = Neo4jActivePublicationInventoryService._list_tx(
            _Tx(manifest, [], []), self.principal, report, None, 100)
        self.assertEqual(result.items, ())
        self.assertEqual(result.total_record_count, 0)

    def test_old_published_revision_allows_newer_working_head_but_not_corruption(self):
        tenant = self.principal.tenant_id
        entity = dict(entity_id='pump-bc-p-101', tenant_id=tenant, entity_type='Equipment',
            canonical_key='equipment:BC-P-101', canonical_name='循环水泵')
        revision = dict(record_id='pump-record', revision_id='pump-revision-2', revision=2,
            tenant_id=tenant, governance_status='PUBLISHED', origin='EXPERT_IMPORT',
            authority_level='AUTHORITATIVE', confidence=1.0, ontology_version_id=self.tbox.tbox_id,
            document_id='pump-document', version_id='pump-document-v1', chunk_id='pump-chunk-v1',
            evidence_char_start=0, evidence_char_end=4, entity_id=entity['entity_id'],
            entity_type='Equipment', canonical_key=entity['canonical_key'], canonical_name='循环水泵')
        row = dict(revision=revision, revision_labels=['GovernedEntityMentionRevision'],
            publication_record_kind='ENTITY_MENTION', head_count=1, current_pointer_count=1,
            matching_current_count=1, head_tenant_id=tenant, head_record_kind='ENTITY_MENTION',
            head_current_revision=5, evidence_link_count=1, evidence_chunk_count=1,
            evidence_document_count=1, valid_evidence_path_count=1, evidence_chunk_ordinal=0,
            mention_entity_link_count=1, mention_entity=entity, navigation_mention_count=1,
            mention_membership_count=1, valid_mention_projection_count=1,
            subject_link_count=0, object_link_count=0)
        self.assertEqual(_decode_item(row, tenant).revision_id, 'pump-revision-2')
        boundary = _publication_boundary([_state(('pump-revision-2',), tbox_value=self.tbox)],
            self.principal, PublishedGraphQualityLimits())
        row.update(labels=row['revision_labels'], publication_record_kinds=['ENTITY_MENTION'],
            publication_membership_count=1, active_snapshot_count=1, evidence_chunk_id='pump-chunk-v1')
        def head_codes():
            issues = _IssueCollector('pump-quality-test', 100)
            _audit_revision(row, boundary=boundary, entity_types={}, relationship_types={},
                issues=issues, literal_counts={})
            return {issue.code for issue in issues.values}
        self.assertNotIn('HEAD_CURRENT_REVISION_INVALID', head_codes())
        row['head_current_revision'] = 1
        with self.assertRaises(ActivePublicationInventoryConflict):
            _decode_item(row, tenant)
        self.assertIn('HEAD_CURRENT_REVISION_INVALID', head_codes())
        row['head_current_revision'] = 5
        row['matching_current_count'] = 0
        with self.assertRaises(ActivePublicationInventoryConflict):
            _decode_item(row, tenant)
        self.assertIn('HEAD_CURRENT_REVISION_INVALID', head_codes())

    def test_empty_audit_keeps_acl_and_requires_both_empty_manifests(self):
        report = self.empty_report()
        boundary = dict(acl_requirements=[], publication_revision_count=0,
            publication_source_count=0)
        self.assertEqual(_publication_acl_requirements(boundary, report, self.principal),
            (('members',),))
        for counts in ({'publication_revision_count': 1}, {'publication_source_count': 1},
                       {'publication_source_count': None}):
            with self.subTest(counts=counts), self.assertRaises(PublishedGraphQualityHistoryConflict):
                _publication_acl_requirements({**boundary, **counts}, report, self.principal)

    def test_historical_quality_rulesets_remain_readable(self):
        report = self.empty_report()
        for version in (1, 2, 3):
            historical = replace(report, ruleset_version=f'published-governed-graph-quality-v{version}')
            encoded = json.dumps(_report_document(historical), ensure_ascii=False,
                sort_keys=True, separators=(',', ':'))
            self.assertEqual(_report_from_json(encoded).ruleset_version, historical.ruleset_version)

    def test_queries_bind_historical_source_without_restoring_withdrawal(self):
        for query in (_ITEMS_QUERY, _REVISIONS_QUERY):
            self.assertNotIn('ACTIVE_VERSION', query)
            self.assertNotIn('ACTIVE_SNAPSHOT', query)
            for clause in ('USES_KNOWLEDGE_SNAPSHOT', 'HAS_VERSION', 'HAS_CHUNK',
                           "snapshot.build_state IN ['PUBLISHED', 'RETIRED']",
                           'snapshot.retirement_id IS NULL', 'document.retirement_id IS NULL',
                           'WHERE group IN document.access_groups', 'WHERE group IN chunk.access_groups'):
                self.assertIn(clause, query)

    def test_property_assignment_mentions_keep_their_published_identity(self):
        revision = dict(chunk_id='pump-source-chunk', entity_type='Equipment',
            surface='循环水泵', extractor_version='pump-test:v1',
            evidence_char_start=0, evidence_char_end=4, record_id='pump-assignment')
        original = mention_id('pump-source-chunk', 'Equipment', 0, 4, '循环水泵', 'pump-test:v1')
        self.assertEqual(_expected_navigation_mention_id(revision), original)
        revision['assignment_source_revision_id'] = 'pump-power-original'
        assigned = _stable_id('property-assignment-mention:v1', original, 'pump-assignment')
        self.assertEqual(_expected_navigation_mention_id(revision), assigned)
        self.assertNotEqual(assigned, original)
        revision['assignment_source_revision_id'] = ''
        self.assertIsNone(_expected_navigation_mention_id(revision))
