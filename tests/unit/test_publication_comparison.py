"""Business comparison retains parallel records and rejects ambiguous identity."""
from unittest.mock import Mock
import unittest
from graphrag_prod.knowledge.publication_comparison import compare_records, publication_comparison
from graphrag_prod.knowledge.review import KnowledgePublicationConflict


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
        publication=Mock(publication_id='active',status='ACTIVE',published_revision_ids=tuple(str(i) for i in range(501)))
        service.history.return_value=[publication]
        with self.assertRaises(KnowledgePublicationConflict):publication_comparison(service,Mock(),'active','active')
        service.driver.session.assert_not_called()
