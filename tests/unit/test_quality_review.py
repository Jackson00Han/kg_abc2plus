"""Human quality decisions retain explicit identity, immutable content and scope."""
from hashlib import sha256
from types import SimpleNamespace
import unittest
from pydantic import ValidationError
from graphrag_prod.domain import Principal
from graphrag_prod.graph.quality_review import Neo4jQualityReviewService, review_identity, _payload
from graphrag_prod.graph.published_quality import PublishedGraphQualityAuthorizationError
from graphrag_prod.graph.published_quality_history import PublishedGraphQualityHistoryConflict
from graphrag_prod.api.quality_review_contracts import QualityReviewRequest

class QualityReviewTests(unittest.TestCase):
    def test_notes_and_dual_permissions_are_required(self):
        base=dict(run_id='run',issue_id='issue',operation_key='op',decision='NO_CHANGE_REQUIRED',notes='Checked source.')
        for change in ({'notes':''},{'notes':' '*3},{'decision':'PASS'},{'tenant_id':'victim'},{'notes':'x'*2001}):
            with self.assertRaises(ValidationError): QualityReviewRequest.model_validate({**base,**change})
        for scopes in ({'knowledge:quality'},{'knowledge:review'},set()):
            who=Principal('p','tenant',frozenset({'g'}),frozenset(scopes))
            with self.assertRaises(PublishedGraphQualityAuthorizationError): Neo4jQualityReviewService(SimpleNamespace()).record(who,QualityReviewRequest(**base))

    def test_operation_identity_is_tenant_and_actor_scoped(self):
        one=Principal('p','tenant',frozenset({'g'}));two=Principal('p','other',frozenset({'g'}));three=Principal('q','tenant',frozenset({'g'}))
        self.assertEqual(review_identity(one,'op'),review_identity(one,'op'))
        self.assertEqual(len({review_identity(w,'op') for w in (one,two,three)}),3)

    def test_tampered_disposition_payload_is_not_returned(self):
        encoded='{"run_id":"run"}'
        row={'payload':encoded,'checksum':sha256(encoded.encode()).hexdigest()}
        self.assertEqual(_payload(row)['run_id'],'run')
        row['payload']='{"run_id":"other"}'
        with self.assertRaises(PublishedGraphQualityHistoryConflict): _payload(row)
