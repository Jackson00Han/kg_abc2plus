"""Generic source context scopes, exact dual evidence, and bounded model plans."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from graphrag_prod.construction.context_mapping import (
    VERSION, OpenAICompatibleContextMapper, _check_binding, build_context_payload, compile_context_properties,
    context_locations, validate_context_binding, validate_context_plan,
)
from graphrag_prod.construction.structured import StructuredMappingExtractor, collections, locate_json
from graphrag_prod.domain.ids import content_checksum
from graphrag_prod.construction.workflow import _to_abox_batch
from graphrag_prod.knowledge.models import EvidenceReference
from graphrag_prod.ontology.models import Cardinality, PropertyDataType, PropertyDefinition
from tests.unit.test_construction_extraction import _profile, _tbox
from tests.unit.test_structured_mapping import base, chunk_for, fixture, parsed


def context_fixture(*, nested=False, local_conflict=False, wrong_identity=False, local_value=...):
    data, original = fixture(3)
    data['metadata'] = {'facility': 'AREA-7', 'identity': '{facility}/{serial}',
                        'source_location': {'facility': 'description of source field, not its value'}}
    for i, row in enumerate(data['machines']):
        row['serial'] = 'A' + str(i)
        row['key'] = 'AREA-7/' + row['serial']
    if wrong_identity:
        data['machines'][1]['key'] = 'AREA-8/A1'
    if local_conflict or local_value is not ...:
        data['machines'][1]['facilityCode'] = 'AREA-8' if local_value is ... else local_value
        original['collections'][1]['ignored_fields']['/facilityCode'] = 'review source conflict separately'
    scope, prefix = '', ''
    if nested:
        data = {'sites': {'north': data, 'south': {'metadata': {'facility': 'AREA-8'},
                 'different_objects': [{'key':'S-1'}]}}}
        prefix, scope = '/sites/north', '/sites/north'
        for rule in original['collections']:
            rule['path'] = prefix + rule['path']
            for rel in rule['relations']:
                rel['target_collection'] = prefix + rel['target_collection']
        original['collections'].append({'path':'/sites/south/different_objects','id_field':'/key',
            'entity_type':'Asset','type_field':None,'type_map':{},'properties':[],'relations':[],'ignored_fields':{}})
    tbox = _tbox()
    definition = PropertyDefinition('facilityCode', PropertyDataType.STRING, True, Cardinality.ONE,
                                    description='Facility identifier for this object')
    tbox = replace(tbox, entity_types=tuple(replace(e, properties=(*e.properties, definition))
                   if e.name == 'Asset' else e for e in tbox.entity_types))
    document = parsed(data, 500)
    model, _ = base(original, tbox)
    extractor = StructuredMappingExtractor(model, document)
    extractor.prepare_document(read=lambda _:None,persist=lambda *_:None,before_model_call=lambda:None)
    mentions, facts, chunks = [], [], []
    for seed in document.chunks:
        chunk = chunk_for(seed); chunks.append(chunk)
        output=extractor.extract_audited(artifact_id='a',input_hash='h',chunk=chunk,profile=_profile())
        batch=_to_abox_batch(output,chunk=chunk,extracted_at=datetime(2026,9,14,tzinfo=timezone.utc))
        if batch: mentions.extend(batch.mentions); facts.extend(batch.assertions)
    rule={'collection':prefix+'/machines','property':'facilityCode','value_path':prefix+'/metadata/facility',
          'scope_path':scope,'entity_types':['Asset'],
          'binding':{'mode':'IDENTITY_TEMPLATE','template_path':prefix+'/metadata/identity',
                     'context_variable':'facility','identity_field':'/key','record_variables':{'serial':'/serial'}},
          'reason':'来源身份模板明确设施标识与对象编号的范围。'}
    plan={'version':VERSION,'rules':[rule]}
    return document,tbox,extractor.summary(),mentions,facts,chunks,plan


class ContextMappingTests(unittest.TestCase):
    def test_compilation_parses_and_hashes_source_once_but_external_guard_revalidates(self):
        d,t,s,m,f,c,p=context_fixture()
        module='graphrag_prod.construction.context_mapping.'
        with patch(module+'locate_json',wraps=locate_json) as parse, \
             patch(module+'collections',wraps=collections) as groups, \
             patch(module+'context_locations',wraps=context_locations) as locations, \
             patch(module+'content_checksum',wraps=content_checksum) as checksum:
            result=compile_context_properties(d.normalized_text,t,s,m,f,c,p)
            self.assertEqual(len(result.specs),3)
            self.assertEqual(parse.call_count,1)
            self.assertEqual(groups.call_count,1)
            self.assertEqual(locations.call_count,len(s['collections']))
            self.assertEqual(checksum.call_count,1)
            spec=result.specs[0]
            validate_context_binding(d.normalized_text,spec.context_property_evidence,
                subject_type='Asset',predicate='facilityCode',raw_literal=spec.raw_literal)
            self.assertEqual(parse.call_count,2)
            self.assertEqual(checksum.call_count,2)

    def test_compilation_deadline_stops_before_parse_and_during_record_iteration(self):
        d,t,s,m,f,c,p=context_fixture()
        module='graphrag_prod.construction.context_mapping.'
        with patch(module+'time.monotonic',return_value=10),patch(module+'locate_json') as parse:
            with self.assertRaises(TimeoutError):
                compile_context_properties(d.normalized_text,t,s,m,f,c,p,deadline=9)
            parse.assert_not_called()
        expired=False
        def binding(*args,**kwargs):
            nonlocal expired
            result=_check_binding(*args,**kwargs)
            expired=True
            return result
        with patch(module+'time.monotonic',side_effect=lambda:10 if expired else 0), \
             patch(module+'_check_binding',side_effect=binding):
            with self.assertRaises(TimeoutError):
                compile_context_properties(d.normalized_text,t,s,m,f,c,p,deadline=5)
            self.assertTrue(expired)

    def test_explicit_unmapped_null_or_empty_local_field_never_inherits(self):
        for value in (None,''):
            with self.subTest(value=value):
                d,t,s,m,f,c,p=context_fixture(local_value=value)
                result=compile_context_properties(d.normalized_text,t,s,m,f,c,p)
                self.assertEqual(len(result.specs),2)
                self.assertEqual(result.issues[0]['code'],'CONTEXT_LOCAL_CONFLICT')
                self.assertEqual(result.issues[0]['entity_count'],1)
                self.assertNotIn('AREA-7/A1',{spec.context_property_evidence.source_identity for spec in result.specs})

    def test_fully_present_local_properties_are_not_context_mapping_targets(self):
        d,t,s,m,_,_,plan=context_fixture()
        payload=build_context_payload(d.normalized_text,t,s,m)
        machines=next(c for c in payload['collections'] if c['collection']=='/machines')
        props={p['name'] for o in machines['ontology'] for p in o['properties']}
        self.assertIn('facilityCode',props)
        for local in machines['local_properties']:
            self.assertNotIn(local['property'],props)
        invalid=deepcopy(plan);invalid['rules'][0]['property']=machines['local_properties'][0]['property']
        with self.assertRaises(ValueError):validate_context_plan(invalid,payload)

    def test_declared_but_absent_local_value_is_not_invented_from_context(self):
        d,t,s,m,_,_,plan=context_fixture()
        summary=deepcopy(s)
        target=next(c for c in summary['collections'] if c['collection']=='/machines')
        target['properties'].append({'field':'/optionalFacility','property':'facilityCode'})
        payload=build_context_payload(d.normalized_text,t,summary,m)
        with self.assertRaises(ValueError):validate_context_plan(plan,payload)

    def test_record_field_cannot_shadow_the_source_context_template_variable(self):
        d,t,s,m,_,_,plan=context_fixture(wrong_identity=True)
        plan['rules'][0]['binding']['record_variables']['facility']='/serial'
        with self.assertRaises(ValueError):
            validate_context_plan(plan,build_context_payload(d.normalized_text,t,s,m))

    def test_root_and_parent_context_generate_exact_separate_evidence(self):
        for nested in (False,True):
            with self.subTest(nested=nested):
                document,tbox,summary,mentions,facts,chunks,plan=context_fixture(nested=nested)
                compiled=compile_context_properties(document.normalized_text,tbox,summary,mentions,facts,chunks,plan)
                self.assertEqual(len(compiled.specs),3)
                self.assertEqual(compiled.issues,())
                if not nested:
                    self.assertTrue(any(s.evidence.chunk_id != s.context_property_evidence.value_evidence.chunk_id for s in compiled.specs))
                for spec in compiled.specs:
                    self.assertNotEqual(spec.evidence.char_start,spec.context_property_evidence.value_evidence.char_start)
                    self.assertEqual(spec.raw_literal,'"AREA-7"')
                    self.assertEqual(spec.source_encoding,'JSON_STRING')
                    self.assertIn(spec.subject_mention.evidence.quoted_text,spec.evidence.quoted_text)
                    self.assertEqual(spec.context_property_evidence.value_evidence.quoted_text,spec.raw_literal)
                    validate_context_binding(document.normalized_text,spec.context_property_evidence,
                        subject_type='Asset',predicate='facilityCode',raw_literal=spec.raw_literal)

    def test_mismatched_record_identity_only_leaves_affected_object_unapplied(self):
        d,t,s,m,f,c,p=context_fixture(wrong_identity=True)
        result=compile_context_properties(d.normalized_text,t,s,m,f,c,p)
        self.assertEqual(len(result.specs),2)
        self.assertEqual(result.issues[0]['code'],'CONTEXT_IDENTITY_CONFLICT')
        self.assertEqual(result.issues[0]['entity_count'],1)

    def test_explicit_local_conflict_is_never_overwritten(self):
        d,t,s,m,f,c,p=context_fixture(local_conflict=True)
        result=compile_context_properties(d.normalized_text,t,s,m,f,c,p)
        self.assertEqual(len(result.specs),2)
        self.assertEqual(result.issues[0]['code'],'CONTEXT_LOCAL_CONFLICT')
        self.assertEqual(result.issues[0]['entity_count'],1)

    def test_sibling_context_cannot_be_used_as_global_default(self):
        d,t,s,m,f,c,p=context_fixture(nested=True)
        locations=context_locations(locate_json(d.normalized_text),'/sites/north/machines')
        self.assertNotIn('/sites/south/metadata/facility',locations)
        invalid=deepcopy(p); invalid['rules'][0]['value_path']='/sites/south/metadata/facility'
        with self.assertRaises(ValueError):
            compile_context_properties(d.normalized_text,t,s,m,f,c,invalid)

    def test_multiple_applicable_values_stay_uncertain_without_partial_choice(self):
        d,t,s,m,f,c,p=context_fixture()
        other=deepcopy(p['rules'][0]);other['value_path']='/metadata/source_location/facility'
        other['binding']={'mode':'ANCESTOR_DEFAULT'}
        p['rules'].append(other)
        result=compile_context_properties(d.normalized_text,t,s,m,f,c,p)
        self.assertEqual(result.specs,())
        self.assertTrue(all(i['code']=='CONTEXT_AMBIGUOUS_SCOPE' for i in result.issues))

    def test_primary_and_context_chunk_access_must_agree(self):
        d,t,s,m,f,c,p=context_fixture()
        changed=[replace(x,access_groups=frozenset({'private'})) if x.char_start==0 else x for x in c]
        result=compile_context_properties(d.normalized_text,t,s,m,f,changed,p)
        self.assertEqual(result.specs,())
        self.assertEqual(result.issues[0]['code'],'CONTEXT_EVIDENCE_SCOPE_MISMATCH')

    def test_tampered_source_value_path_predicate_and_rule_are_rejected(self):
        d,t,s,m,f,c,p=context_fixture()
        spec=compile_context_properties(d.normalized_text,t,s,m,f,c,p).specs[0]
        for text,ctx,predicate in [
            (d.normalized_text.replace('AREA-7','AREA-9'),spec.context_property_evidence,'facilityCode'),
            (d.normalized_text,replace(spec.context_property_evidence,value_pointer='/metadata/source_location/facility'),'facilityCode'),
            (d.normalized_text,spec.context_property_evidence,'serialNumber')]:
            with self.assertRaises(ValueError):
                validate_context_binding(text,ctx,subject_type='Asset',predicate=predicate,raw_literal=spec.raw_literal)

    def test_plain_ancestor_default_is_generic_and_does_not_generate_record_fields(self):
        d,t,s,m,f,c,p=context_fixture()
        p['rules'][0]['binding']={'mode':'ANCESTOR_DEFAULT'}
        result=compile_context_properties(d.normalized_text,t,s,m,f,c,p)
        self.assertEqual(len(result.specs),3)
        self.assertEqual({x.property_name for x in result.specs},{'facilityCode'})
        payload=build_context_payload(d.normalized_text,t,s,m)
        with self.assertRaises(ValueError):
            validate_context_plan({**p,'code':'anything'},payload)


class ContextMapperModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_unsupported_field_does_not_discard_independent_valid_rule(self):
        d,t,s,m,f,c,plan=context_fixture()
        invalid=deepcopy(plan['rules'][0]);invalid['property']='undeclaredProperty'
        proposal={**plan,'rules':[plan['rules'][0],invalid]}
        calls=[]
        async def create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(
                content=json.dumps(proposal),refusal=None))])
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        result=await OpenAICompatibleContextMapper(client=client,model='fixture').plan(build_context_payload(d.normalized_text,t,s,m))
        self.assertEqual(result['mapping'],plan)
        self.assertEqual(len(calls),1)
        self.assertEqual(result['audit']['attempts'][0]['rejected_rules'],[{'index':1,'code':'CONTEXT_UNKNOWN_PROPERTY'}])
        self.assertEqual(len(compile_context_properties(d.normalized_text,t,s,m,f,c,result['mapping']).specs),3)

    async def test_model_repair_is_bounded_and_keeps_exact_audit(self):
        d,t,s,m,f,c,p=context_fixture()
        payload=build_context_payload(d.normalized_text,t,s,m)
        replies=[{'version':VERSION,'rules':[{'code':'invented'}]},p]
        requests=[]
        async def create(**kwargs):
            requests.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(
                content=json.dumps(replies.pop(0)),refusal=None))])
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        result=await OpenAICompatibleContextMapper(client=client,model='fixture').plan(payload)
        self.assertEqual(result['status'],'COMPLETE')
        self.assertEqual(len(requests),2)
        self.assertEqual([x['status'] for x in result['audit']['attempts']],['REJECTED','VALIDATED_PROPOSAL'])
        self.assertEqual(result['mapping'],p)

    async def test_provider_failure_is_unavailable_not_an_empty_approved_mapping(self):
        async def create(**_): raise TimeoutError('sensitive-provider-detail')
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        result=await OpenAICompatibleContextMapper(client=client,model='fixture').plan({'collections':[]})
        self.assertEqual(result['status'],'UNAVAILABLE')
        self.assertIsNone(result['mapping'])
        self.assertNotIn('sensitive-provider-detail',json.dumps(result))
