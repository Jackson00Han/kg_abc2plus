"""Executable step-03/04 grouping, selection, and revision lifecycle checks."""

import unittest
from pathlib import Path

from tests.unit import test_playground_resolution as ui_checks


from tests.fixtures.workbench_ui import governance_source
SETUP = r'''
function source(id,revision=2,entityId='pump-1') {
  const record=item(id,revision);record.entity.entity_id=entityId;
  record.entity.canonical_name='循环水泵';record.trust={status:'APPROVED',authority:'AUTHORITATIVE',origin:'AUTHORITATIVE_EXTRACTED'};
  record.evidence={quoted_text:'循环水泵',chunk_id:'chunk',char_start:0,char_end:4};
  return {record};
}
'''


class PublicationGroupTests(unittest.TestCase):
    def run_ui(self, scenario, extra=''):
        ui_checks.PlaygroundResolutionTests().run_ui(extra+SETUP+scenario)

    def test_same_entity_sources_group_but_homonyms_and_fact_kinds_stay_distinct(self):
        self.run_ui(r'''
const first=source('a'), second=source('b',4), third=source('c');
const other=source('d',2,'pump-2');
const property={record:item('property',2,'ASSERTION')};property.record.subject=first.record.entity;
delete property.record.entity;property.record.object_entity=null;property.record.predicate='EquipmentCode';property.record.literal_value='BC-P-101';
const relation={record:item('relation',3,'ASSERTION')};relation.record.subject=first.record.entity;
delete relation.record.entity;relation.record.object_entity={entity_id:'site',canonical_name:'一号泵站'};relation.record.predicate='INSTALLED_AT';
state.publicationCandidates=[first,second,third,other,property,relation];
renderPublicationCandidates();
const html=elements.publicationCandidateList.innerHTML;
assert.equal((html.match(/data-publication-entity=/g)||[]).length,2);
assert.ok(html.includes('来源提及 3 · 属性 1 · 关系 1'));
assert.ok(html.includes('设备编码：BC-P-101'));assert.ok(html.includes('安装于：一号泵站'));
assert.ok(html.includes('当前记录版本：4'));assert.ok(html.includes('版本 ID：b-4'));
assert.ok(!html.includes('<strong>ENTITY_MENTION'));
assert.deepEqual(publicationCandidateGroups()[0].rows.map(row=>row.index),[0,1,2,4,5]);
assert.equal(state.publicationCandidates.length,6);
''')

    def test_checkbox_group_and_text_selection_agree_and_review_does_not_restore_deselection(self):
        self.run_ui(r'''
let controls=[], renders=0;
elements.publicationCandidateList={
  set innerHTML(value) {
    renders+=1;this.html=value;controls=[];
    for(const match of value.matchAll(/<input[^>]*data-publication-(candidate|group)="(\d+)"[^>]*>/g)) {
      const key=match[1]==='group'?'publicationGroup':'publicationCandidate';
      controls.push({dataset:{[key]:match[2]},checked:match[0].includes(' checked'),
        addEventListener(_,handler){this.change=handler;}});
    }
  },
  get innerHTML(){return this.html;},
  querySelectorAll(selector) {
    if(selector.startsWith('input,')) return controls;
    const key=selector==='[data-publication-group]'?'publicationGroup':selector==='[data-publication-candidate]'?'publicationCandidate':null;
    return key?controls.filter(input=>key in input.dataset):[];
  }
};
state.publicationCandidates=[source('a'),source('b',4),source('c')];
elements.publicationRevisions.value='a-2\nb-4\nc-2';
state.approvedRevisions=new Set(['a-2','b-4','c-2']);
renderPublicationCandidates();
const find=(key,value)=>controls.find(input=>input.dataset[key]===value);
assert.ok(find('publicationGroup','0').checked);
let input=find('publicationCandidate','1');input.checked=false;input.change();
assert.deepEqual(publicationSelection().approved_revision_ids,['a-2','c-2']);
assert.ok(find('publicationGroup','0').indeterminate);
input=find('publicationGroup','0');input.checked=false;input.change();
assert.throws(()=>publicationSelection());assert.equal(elements.publicationRevisions.value,'');
input=find('publicationGroup','0');input.checked=true;input.change();
assert.deepEqual(publicationSelection().approved_revision_ids,['a-2','b-4','c-2']);
elements.publicationRevisions.value='b-4';syncPublicationTextSelection();
assert.deepEqual(publicationSelection().approved_revision_ids,['b-4']);
assert.ok(!find('publicationCandidate','0').checked);
trackReviewedOutcomes([{previous_revision_id:'b-4',revision_id:'b-6',status:'APPROVED'}]);
assert.deepEqual(publicationSelection().approved_revision_ids,['b-6']);
assert.ok(!state.approvedRevisions.has('b-4'));assert.ok(state.approvedRevisions.has('a-2'));
assert.equal(renders,1); // Selection must preserve open source context and focus.
''')

    def test_late_candidate_refresh_cannot_restore_old_revision_or_revoked_identity(self):
        page=governance_source()
        loader=page[page.index('async function loadPublicationCandidates('):page.index('function renderHistory(')]
        self.run_ui(r'''
const old=source('a'), current=source('a',4);
state.publicationCandidates=[old];state.approvedRevisions.add('a-2');
elements.publicationRevisions.value='a-2';state.publicationPreview={preview:{preview_hash:'old'}};
const first=loadPublicationCandidates(), second=loadPublicationCandidates();
requests[1].resolve({items:[current]});await second;
assert.equal(state.publicationCandidates[0].record.revision,4);
assert.equal(elements.publicationRevisions.value,'');assert.equal(state.publicationPreview,null);
requests[0].resolve({items:[old]});await first;
assert.equal(state.publicationCandidates[0].record.revision,4);
const revoked=loadPublicationCandidates();state.identityEpoch+=1;state.publicationCandidates=[];
requests[2].resolve({items:[old]});await revoked;
assert.equal(state.publicationCandidates.length,0);
''',loader)

    def test_return_to_review_removes_dependent_versions_and_blocks_publish_during_save(self):
        page=governance_source()
        reopen=page[page.index('async function reopenPublicationCandidate('):page.index('function publicationSelection(')]
        self.run_ui(r'''
state.publicationCandidates=[source('a'),source('fact',3)];
state.approvedRevisions=new Set(['a-2','fact-3']);elements.publicationRevisions.value='a-2\nfact-3';
loadReviews=async()=>{};loadPublicationCandidates=async()=>{};renderReviews=()=>{};
globalThis.showConstructionFlow=()=>{};globalThis.document={getElementById:()=>null};
state.publicationBusy=true;
await submitReviews('QUARANTINED',[0]);await applyResolution(0,0,{});await keepExistingFact(0);
assert.equal(requests.length,0);state.publicationBusy=false;
elements.reviewList.querySelectorAll=selector=>selector==='[data-review-editor]'?[{disabled:false}]:[];
await reopenPublicationCandidate(0);assert.equal(requests.length,0);
elements.reviewList.querySelectorAll=()=>[];
const pending=reopenPublicationCandidate(0);
assert.ok(state.reviewBusy);await publishKnowledge();await previewKnowledgePublication();
assert.equal(requests.length,1);
assert.equal(JSON.parse(requests[0].options.body).decisions[0].expected_revision,2);
requests[0].resolve({outcomes:[{previous_revision_id:'a-2',revision_id:'a-3',status:'QUARANTINED'},
 {previous_revision_id:'fact-3',revision_id:'fact-4',status:'QUARANTINED'}]});
await pending;
assert.equal(elements.publicationRevisions.value,'');assert.equal(state.selectedCandidateRevisions.size,0);
assert.equal(state.approvedRevisions.size,0);assert.equal(state.reviewPhase,'paused');
assert.equal(state.reviewBusy,false);assert.equal(state.publicationPreview,null);
const oldIdentitySave=reopenPublicationCandidate(0);
state.identityEpoch+=1;state.reviewBusy=true;state.reviewPhase='facts';
elements.publicationRevisions.value='new-identity-selection';
state.publicationPreview={preview:{preview_hash:'new-identity'}};
requests[1].resolve({outcomes:[]});await oldIdentitySave;
assert.equal(state.reviewBusy,true);assert.equal(state.reviewPhase,'facts');
assert.equal(elements.publicationRevisions.value,'new-identity-selection');
assert.equal(state.publicationPreview.preview.preview_hash,'new-identity');
''',reopen)
