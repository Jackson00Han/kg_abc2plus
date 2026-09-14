"""Public context receipts expose bounded references, not mapper prompts."""
import unittest
from types import SimpleNamespace
from pydantic import ValidationError
from graphrag_prod.api.auto_review_contracts import AutoReviewResponse
from graphrag_prod.api.knowledge import _review_record_payload
from graphrag_prod.api.knowledge_contracts import ReviewEvidenceRequest,ReviewRecordResponse
from graphrag_prod.knowledge.review import ReviewRecordKind
from tests.e2e.test_auto_review_api import receipt
from tests.unit.test_context_property_evidence import context_fixture

class ContextMappingAPITests(unittest.TestCase):
    def test_assertion_response_preserves_both_evidence_locations_without_mapping_prompt(self):
        _,_,assertion=context_fixture()
        response=ReviewRecordResponse.model_validate(_review_record_payload(SimpleNamespace(record=assertion,record_kind=ReviewRecordKind.ASSERTION)))
        context=response.context_property_evidence
        self.assertEqual(context.value_pointer,'/metadata/project_id')
        self.assertEqual(context.value_evidence.quoted_text,'"Plant A"')
        self.assertNotEqual(response.evidence.chunk_id,context.value_evidence.chunk_id)
        self.assertNotIn('binding_json',response.model_dump_json())
        payload=response.model_dump()
        payload['context_property_evidence']['binding_json']='{}'
        with self.assertRaises(ValidationError):ReviewRecordResponse.model_validate(payload)

    def test_evidence_role_does_not_accept_client_source_positions(self):
        request=ReviewEvidenceRequest(record_id='a-1',expected_revision=1,evidence_role='CONTEXT_VALUE')
        self.assertEqual(request.evidence_role,'CONTEXT_VALUE')
        self.assertEqual(ReviewEvidenceRequest(record_id='a-1',expected_revision=1).evidence_role,'PRIMARY')
        with self.assertRaises(ValidationError):
            ReviewEvidenceRequest(record_id='a-1',expected_revision=1,evidence_role='CONTEXT_VALUE',char_start=1)

    def test_context_summary_is_bounded_and_separate_from_review_decisions(self):
        issue=dict(code='CONTEXT_IDENTITY_CONFLICT',property_name='facilityCode',entity_count=1,
                   reason='局部身份与上下文不一致。',source_paths=['/metadata/facility'])
        summary=dict(status='PARTIAL',added_assertions=2,applied=2,uncertain=1,overridden=0,rules=[],issues=[issue])
        response=AutoReviewResponse(**receipt(),context_mapping=summary)
        self.assertEqual(response.context_mapping.uncertain,1)
        self.assertEqual(response.counts.approved_mentions,3)
        for changes in ({'raw_response':'private'},{'issues':[issue]*101}):
            with self.assertRaises(ValidationError):
                AutoReviewResponse(**receipt(),context_mapping={**summary,**changes})

if __name__=='__main__':unittest.main()
