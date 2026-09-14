"""Business comparison retains parallel records and rejects ambiguous identity."""
from unittest.mock import Mock
import unittest
from graphrag_prod.knowledge.publication_comparison import compare_records, publication_comparison
from graphrag_prod.knowledge.review import KnowledgePublicationConflict
from graphrag_prod.knowledge.publication_guard import MAX_PUBLICATION_MANIFEST_RECORDS
from graphrag_prod.api.publication_comparison_contracts import PublicationComparisonResponse


class PublicationComparisonTests(unittest.TestCase):
    def test_record_identity_and_revision_changes_not_display_names(self):
        a=dict(record_id='a', revision_id='a1', subject_name='Pump', literal_value='1', unit='MPa')
        b={**a, 'record_id':'b', 'revision_id':'b1'}
        c={**a, 'revision_id':'a2', 'literal_value':'2'}
        result=compare_records([a,b],[c])
        self.assertEqual(result['removed'],[b])
        self.assertEqual(result['changed'],[{'before':a,'after':c}])
        self.assertEqual(result['unchanged_count'],0)
        self.assertEqual(compare_records([a],[a])['unchanged_count'],1)
        with self.assertRaises(KnowledgePublicationConflict):compare_records([a,a],[c])

    def test_unknown_or_unbounded_publications_fail_before_metadata_query(self):
        service=Mock();service.history.return_value=[]
        with self.assertRaises(KnowledgePublicationConflict):publication_comparison(service,Mock(),'target','active')
        service.driver.session.assert_not_called()
        publication=Mock(publication_id='active',status='ACTIVE',published_revision_ids=tuple(str(i) for i in range(MAX_PUBLICATION_MANIFEST_RECORDS + 1)))
        service.history.return_value=[publication]
        with self.assertRaises(KnowledgePublicationConflict):publication_comparison(service,Mock(),'active','active')
        service.driver.session.assert_not_called()

    def test_comparison_keeps_complete_579_record_manifest_and_rechecks_it(self):
        service = Mock()
        revisions = tuple(f'revision-{i}' for i in range(579))
        old = Mock(publication_id='old', status='ACTIVE', generation=1,
                   published_revision_ids=revisions[:300], source_snapshot_ids=())
        new = Mock(publication_id='new', status='RETIRED', generation=2,
                   published_revision_ids=revisions, source_snapshot_ids=())
        service.history.return_value = [old, new]
        rows = [dict(publication_id=publication, revision_id=revision,
            record_id=revision, record_kind='ENTITY_MENTION', subject_name='Equipment',
            predicate=None, object_name=None, literal_value=None, unit=None,
            valid_from=None, valid_to=None, observed_at=None, document_title='Fixture',
            qualifiers=None)
            for publication, ids in (('old', revisions[:300]), ('new', revisions))
            for revision in ids]
        session = Mock()
        session.run.side_effect = [rows, [], [], rows]
        service.driver.session.return_value.__enter__ = Mock(return_value=session)
        service.driver.session.return_value.__exit__ = Mock(return_value=False)
        result = publication_comparison(service, Mock(tenant_id='tenant', groups=('readers',)), 'new', 'old')
        response = PublicationComparisonResponse.model_validate(result)
        self.assertEqual(len(response.added), 279)
        self.assertEqual(response.unchanged_count, 300)
        self.assertEqual(service.history.call_count, 2)
        self.assertEqual(session.run.call_count, 4)
        self.assertEqual(session.run.call_args.kwargs['record_limit'], 2 * MAX_PUBLICATION_MANIFEST_RECORDS + 1)
