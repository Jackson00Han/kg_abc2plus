"""Context display and explicit identity selection boundaries."""

from tests.fixtures.workbench_ui import governance_source

import unittest
from pathlib import Path

from graphrag_prod.api.knowledge_contracts import EntityResolutionApplyRequest
from graphrag_prod.knowledge.review_context import _conflict, context_window
from tests.unit import test_playground_resolution as ui_checks


class ReviewContextTests(unittest.TestCase):
    def test_missing_identity_is_uncertainty_but_different_values_conflict(self):
        first = {'EquipmentCode': {('string', 'BC-P-101', None)}}
        second = {'EquipmentCode': {('string', 'BC-P-102', None)}}
        self.assertFalse(_conflict({}, first))
        self.assertFalse(_conflict(first, first))
        self.assertTrue(_conflict(first, second))
        with self.assertRaises(ValueError):
            EntityResolutionApplyRequest(record_id='source', expected_revision=1,
                target_entity_id='target', target_record_id='target-record', notes='same entity')

    def test_context_retains_paragraph_unicode_positions_and_bounded_pages(self):
        text = '前一段。\n设备编码 BC-P-101 的循环水泵正在运行。\n下一段。'
        start = text.index('循环水泵')
        left, right = context_window(text, start, start+4, view='paragraph')
        self.assertEqual(text[left:right], '设备编码 BC-P-101 的循环水泵正在运行。')
        self.assertEqual(text[start:start+4], '循环水泵')
        self.assertEqual(context_window('x'*17000, 100, 104, view='document', offset=8000), (8000,16000))

    def test_actual_page_highlights_context_and_offers_unpublished_targets(self):
        page = governance_source()
        escape = next(line for line in page.splitlines() if 'const escapeHtml =' in line).replace('const escapeHtml =', 'escapeHtml =')
        ui_checks.PlaygroundResolutionTests().run_ui(escape + r'''
const html=evidenceContextMarkup({text:'😀设备 BC-P-101 的循环水泵正在运行',char_start:14,char_end:18,context_start:0,context_end:23,document_title:'台账<script>',version_id:'v1',chunk_id:'c1',document_accessible:true,total_characters:23,view:'paragraph'});
assert.ok(html.includes('<mark>循环水泵</mark>'));
assert.ok(html.includes('BC-P-101'));assert.ok(!html.includes('<script>'));
assert.ok(html.includes('展开前后文'));assert.ok(html.includes('查看完整文档'));
const candidate=item('循环水泵');
const target={record_id:'confirmed',revision:2,entity:{entity_id:'target-entity',canonical_name:'循环水泵',entity_type:'Equipment',canonical_key:'code:101'},status:'APPROVED',selectable:true,evidence:{quoted_text:'设备编码 BC-P-101 的循环水泵',chunk_id:'chunk',char_start:0,char_end:20},reason:'核对上下文'};
const markup=manualResolutionMarkup({review_targets:[target]},0);
assert.ok(markup.includes('已确认，尚未发布'));assert.ok(markup.includes('确认归入此实体'));
assert.ok(markup.includes('data-evidence-record="confirmed"'));
assert.ok(markup.includes('data-evidence-revision="2"'));
state.reviews=[candidate];
state.resolutions.set(candidate.record_id,{...resolution(candidate.record_id,1,'CONFLICT'),
  review_targets:[target],status:'ready',identityEpoch:0,reviewEpoch:0});
globalThis.prompt=()=> '双方原文确认同一设备';globalThis.confirm=()=> true;
const applying=applyResolution(0,-1,{},0);
assert.equal(requests[0].url,'/v1/knowledge/entity-resolution:apply');
const body=JSON.parse(requests[0].options.body);
assert.equal(body.target_entity_id,'target-entity');
assert.equal(body.target_record_id,'confirmed');assert.equal(body.target_expected_revision,2);
requests[0].reject(new Error('target changed; retry required'));
await applying;
assert.equal(state.reviewBusy,false);
''')

    def test_context_response_cannot_reappear_after_identity_changes(self):
        ui_checks.PlaygroundResolutionTests().run_ui(r'''
const panel={innerHTML:'',querySelectorAll:()=>[]};
const details={dataset:{evidenceRecord:'source',evidenceRevision:'2'},isConnected:true,querySelector:()=>panel};
const loading=loadEvidenceContext(details);
assert.deepEqual(JSON.parse(requests[0].options.body),{record_id:'source',expected_revision:2,view:'paragraph',offset:0});
state.identityEpoch+=1;
requests[0].resolve({text:'protected source'});
await loading;
assert.ok(!panel.innerHTML.includes('protected source'));
''')
