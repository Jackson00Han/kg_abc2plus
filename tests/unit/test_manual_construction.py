"""Human records are explicit, secondary, replayable and never model-generated."""
from dataclasses import replace
import unittest
from unittest.mock import Mock

from graphrag_prod.api.knowledge_contracts import KnowledgeConstructionRequest
from graphrag_prod.construction.manual import prepare_manual_record
from graphrag_prod.domain.access import Principal
from graphrag_prod.ontology import EntityTypeDefinition
from tests.unit.test_construction_workflow import _Extractor, _KnowledgeStore, _metadata, _tbox, _workflow


class _ManualStore(_KnowledgeStore):
    def persist_manual_candidates(self, batch):
        self.candidate_writes += 1
        self._persist(batch)


class ManualConstructionTests(unittest.TestCase):
    def fact(self):
        return {'kind': 'RELATIONSHIP', 'subject': {'entity_type': 'Company', 'canonical_name': 'Acme'},
                'predicate': 'OWNS', 'object_entity': {'entity_type': 'Asset', 'canonical_name': 'Pump-7'}}

    def test_human_relationship_is_saved_without_a_model_and_replays(self):
        tbox = _tbox()
        tbox = replace(tbox, entity_types=tuple(
            replace(t, canonical_key_namespaces=(*t.canonical_key_namespaces, 'llm-candidate'))
            for t in tbox.entity_types))
        extractor = _Extractor(tbox)
        workflow, _, knowledge, _ = _workflow(extractor=extractor, knowledge=_ManualStore())
        workflow.extractor_factory = Mock(side_effect=AssertionError('manual entry called model factory'))
        record = prepare_manual_record(self.fact())
        metadata = replace(_metadata(), canonical_uri='urn:graphrag:human:manual-1', source_name='人工补充记录',
                           extraction_mode='MANUAL', manual_record=record)
        principal = Principal('editor', tbox.tenant_id, frozenset({'engineers'}), frozenset({'knowledge:construct'}))
        result = workflow.run(principal, record.text.encode(), metadata)
        self.assertEqual(result.extraction_mode, 'MANUAL')
        self.assertEqual(len(knowledge.last_batch.assertions), 1)
        for item in (*knowledge.last_batch.mentions, *knowledge.last_batch.assertions):
            self.assertEqual(item.trust.origin.value, 'HUMAN_SUPPLEMENT')
            self.assertEqual(item.trust.authority.value, 'SECONDARY')
            self.assertIsNone(item.trust.extractor_version)
            self.assertIsNone(item.trust.prompt_version)
        self.assertEqual(workflow.run(principal, record.text.encode(), metadata).job_id, result.job_id)
        self.assertEqual(knowledge.candidate_writes, 1)
        workflow.extractor_factory.assert_not_called()

    def test_manual_fact_validates_closed_shape_and_exact_fields(self):
        record = prepare_manual_record(self.fact())
        self.assertEqual(record.text, 'Acme — OWNS → Pump-7')
        for value in ({**self.fact(), 'authority': 'AUTHORITATIVE'},
                      {**self.fact(), 'kind': 'ENTITY'},
                      {**self.fact(), 'kind': 'PROPERTY'}):
            with self.assertRaises(ValueError):
                prepare_manual_record(value)

    def test_manual_api_cannot_forge_document_authority_or_upload_content(self):
        body = dict(extraction_mode='MANUAL', operation_key='manual-operation-01',
                    canonical_uri='urn:graphrag:human:manual-operation-01',
                    source_name='人工补充记录', title='人工补充', mime_type='text/plain',
                    tbox_key='industrial-assets', access_groups=['engineers'], manual_fact=self.fact())
        parsed = KnowledgeConstructionRequest.model_validate(body)
        self.assertEqual(parsed.decoded_content().decode(), prepare_manual_record(self.fact()).text)
        for updates in ({'knowledge_scope': 'AUTHORITATIVE'}, {'content_base64': 'eA=='},
                        {'canonical_uri': 'urn:document:real'}, {'extraction_mode': 'LLM'}):
            with self.assertRaises(ValueError):
                KnowledgeConstructionRequest.model_validate({**body, **updates})

    def test_document_flow_cannot_use_manual_source_namespace(self):
        with self.assertRaises(ValueError):
            replace(_metadata(), canonical_uri='urn:graphrag:human:forged')

    def test_human_citation_is_explicit_and_cannot_claim_document_origin(self):
        from dataclasses import asdict
        from graphrag_prod.api.contracts import CitationResponse
        from graphrag_prod.generation.prompt import build_prompt, label_context
        from tests.unit.test_generation import _chunk
        chunk = _chunk('manual-chunk', 'Pump-7 is offline.')
        chunk = replace(chunk, citation=replace(chunk.citation,
            canonical_uri='urn:graphrag:human:record-1', source_name='人工补充记录'))
        response = CitationResponse.model_validate(asdict(chunk.citation))
        self.assertEqual(response.source_kind, 'HUMAN_RECORD')
        self.assertEqual(CitationResponse.model_validate(response.model_dump()), response)
        with self.assertRaises(ValueError):
            CitationResponse.model_validate({**response.model_dump(), 'source_kind':'DOCUMENT'})
        prompt = build_prompt('What is the status?', label_context((chunk,)))
        self.assertIn('"source_kind":"HUMAN_RECORD"', prompt)
        self.assertIn('never external documents', prompt)

    def test_manual_property_preserves_value_unit_and_time_in_actual_record(self):
        record = prepare_manual_record({'kind':'PROPERTY',
            'subject':{'entity_type':'Asset', 'canonical_name':'Pump-7'}, 'predicate':'pressure',
            'literal':{'raw_literal':'12.50','raw_unit':'kPa','raw_valid_from':'2026-09-08'}})
        import json
        value = json.loads(record.response_json)['property_facts'][0]
        for field in ('raw_literal', 'unit', 'valid_from'):
            self.assertIn(value[field], record.text)
        self.assertEqual(value['evidence']['text'], record.text)
        self.assertEqual(value['evidence']['end'], len(record.text))
