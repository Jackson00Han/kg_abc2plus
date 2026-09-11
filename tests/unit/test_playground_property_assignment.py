"""Executable UI cases for per-property subject corrections."""
import unittest

from tests.unit import test_playground_resolution


class PropertyAssignmentUITests(unittest.TestCase):
    def run_ui(self, scenario):
        test_playground_resolution.PlaygroundResolutionTests().run_ui(scenario)

    def test_current_owner_and_homonymous_choices_display_codes_without_changing_value(self):
        self.run_ui(r'''
const fact=item('power',1,'ASSERTION');delete fact.object_entity;
fact.subject={entity_id:'pump-101',canonical_name:'循环水泵',canonical_key:'equipment-id:opaque-a',entity_type:'Equipment'};
fact.predicate='RatedPower';fact.literal_value='22';state.reviews=[fact];
const target=(id,code)=>({record_id:'mention-'+id,revision:2,entity:{entity_id:id,canonical_name:'循环水泵',canonical_key:'equipment-id:'+id,entity_type:'Equipment'},identity_properties:[{name:'EquipmentCode',value:code}],selectable:true,evidence:{quoted_text:'设备编码 '+code}});
const a=target('pump-101','BC-P-101'),b=target('pump-202','BC-P-202');
globalThis.apiRequest=async()=>({record_id:fact.record_id,revision:1,current:a,items:[a,b],truncated:false});
await loadPropertyAssignment(fact);
assert.equal(fact.subject.entity_id,'pump-101');
let html=propertyAssignmentMarkup(fact,0);
assert.ok(html.includes('当前归属'));assert.ok(html.includes('BC-P-101'));assert.ok(html.includes('更改归属'));
assert.ok(!html.includes('data-assignment-select'));
await loadPropertyAssignment(fact,true);
html=propertyAssignmentMarkup(fact,0);
assert.ok(html.includes('BC-P-101'));assert.ok(html.includes('BC-P-202'));
assert.ok(html.includes('value="pump-101" selected'));
assert.ok(html.includes('保存归属并重新检查'));
assert.equal(reviewIsEditing(0),true);
propertyAssignment(fact).selected='pump-202';
assert.equal(fact.subject.entity_id,'pump-101');assert.equal(fact.literal_value,'22');
propertyAssignment(fact).open=false;
assert.equal(reviewIsEditing(0),false);
assert.equal(propertyAssignmentMarkup({...fact,object_entity:b.entity},0),'');
''')

    def test_save_is_revision_bound_requires_reason_and_reloads_without_approving_fact(self):
        self.run_ui(r'''
const fact=item('power',1,'ASSERTION');delete fact.object_entity;
fact.subject={entity_id:'pump-101',canonical_name:'循环水泵'};state.reviews=[fact];
const target={record_id:'mention-202',revision:4,entity:{entity_id:'pump-202',canonical_name:'循环水泵'},identity_properties:[],selectable:true};
const calls=[];
globalThis.apiRequest=async(url,options)=>{calls.push({url,options});return url.endsWith(':apply')?{outcomes:[{record_kind:'ASSERTION',record_id:'power',previous_revision_id:'power-1',revision_id:'power-2',revision:2,status:'QUARANTINED'}]}:{record_id:'power',revision:1,current:null,items:[target],truncated:false};};
await loadPropertyAssignment(fact,true);
const entry=propertyAssignment(fact);entry.selected='pump-202';
await savePropertyAssignment(0);assert.equal(calls.length,1);
entry.notes='原文明确写明 BC-P-202，额定功率 22 kW。';
globalThis.loadReviews=async()=>{};
await savePropertyAssignment(0);
assert.equal(calls.length,2);assert.equal(calls[1].url,'/v1/knowledge/property-assignment:apply');
const body=JSON.parse(calls[1].options.body);
assert.equal(body.record_id,'power');assert.equal(body.expected_revision,1);
assert.equal(body.target_record_id,'mention-202');assert.equal(body.target_expected_revision,4);
assert.equal(body.target_entity_id,'pump-202');assert.ok(body.notes.includes('BC-P-202'));
assert.equal(body.assertion_edit,undefined);assert.equal(body.decision,undefined);
assert.equal(state.reviewPhase,'paused');assert.equal(state.reviewBusy,false);
assert.equal(propertyAssignment(fact),null);
assert.ok(!state.approvedRevisions.has('power-2'));
''')

    def test_stale_search_results_and_identity_switches_cannot_replace_current_choices(self):
        self.run_ui(r'''
const fact=item('power',1,'ASSERTION');delete fact.object_entity;
fact.subject={entity_id:'pump-101',canonical_name:'循环水泵'};state.reviews=[fact];
const first=loadPropertyAssignment(fact,true,'101');
const second=loadPropertyAssignment(fact,true,'202');
requests[1].resolve({record_id:'power',revision:1,current:null,items:[{entity:{entity_id:'pump-202'},selectable:true}],truncated:false});
await second;
requests[0].resolve({record_id:'power',revision:1,current:null,items:[{entity:{entity_id:'pump-101'},selectable:true}],truncated:false});
await first;
assert.equal(propertyAssignment(fact).query,'202');assert.equal(propertyAssignment(fact).items[0].entity.entity_id,'pump-202');
const third=loadPropertyAssignment(fact,true);
state.identityEpoch++;
requests[2].resolve({record_id:'power',revision:1,current:null,items:[],truncated:false});await third;
assert.equal(propertyAssignment(fact),null);
''')

    def test_open_assignment_prevents_bulk_approval_and_server_error_preserves_draft(self):
        self.run_ui(r'''
const fact=item('power',1,'ASSERTION');delete fact.object_entity;
fact.subject={entity_id:'pump-101',canonical_name:'循环水泵'};state.reviews=[fact];
const read=loadPropertyAssignment(fact,true);
requests[0].resolve({record_id:'power',revision:1,current:null,items:[{record_id:'target',revision:2,entity:{entity_id:'pump-202'},selectable:true}],truncated:false});await read;
const entry=propertyAssignment(fact);entry.selected='pump-202';entry.notes='核对原文编号。';
await submitReviews('APPROVED',[0]);assert.equal(requests.length,1);
const saving=savePropertyAssignment(0);requests[1].reject(new Error('target changed'));await saving;
assert.equal(propertyAssignment(fact).notes,'核对原文编号。');assert.equal(propertyAssignment(fact).open,true);
assert.equal(fact.subject.entity_id,'pump-101');assert.equal(state.reviewBusy,false);
''')
