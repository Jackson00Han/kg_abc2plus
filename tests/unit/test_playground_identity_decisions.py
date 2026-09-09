"""Execute the real page's generic independent-identity review workflow."""
import unittest
from tests.unit import test_playground_resolution as ui_checks

SETUP = r'''
function ready(id,outcome='CONFLICT') {
  return {...resolution(id,1,outcome),status:'ready',identityEpoch:0,reviewEpoch:0,
    identity_actions:{independent:{allowed:true,note:'可依据原文确认为独立实体',requires_reason:true}},
    dependent_facts:[],impact_token:'preview-'+id};
}
'''


class PlaygroundIdentityDecisionTests(unittest.TestCase):
    def run_ui(self, scenario):
        ui_checks.PlaygroundResolutionTests().run_ui(SETUP + scenario)

    def test_missing_identifier_and_similar_candidates_do_not_disable_independent_action(self):
        self.run_ui(r'''
const source=item('同名公司');state.reviews=[source];
for(const outcome of ['CONFLICT','AUTO_LINK','REVIEW','NO_MATCH']) {
  state.resolutions.set(source.record_id,ready(source.record_id,outcome));
  assert.equal(reviewApproval(source).allowed,true);
  const html=reviewActions(source,0);
  assert.ok(html.includes('确认为独立实体'));
  assert.ok(html.includes('data-review-existing="0"'));
  assert.ok(html.includes('data-review-note="0"'));
}
renderReviews();assert.ok(elements.reviewList.innerHTML.includes('待确认分组'));
assert.ok(elements.reviewList.innerHTML.includes('data-review-group'));
state.resolutions.get(source.record_id).identity_actions.independent={allowed:false,note:'原文失效'};
assert.equal(reviewApproval(source).allowed,false);
assert.ok(reviewActions(source,0).includes('原文失效'));
state.reviewEpoch+=1;assert.equal(reviewApproval(source).allowed,false);
''')

    def test_existing_identity_entry_only_opens_choices(self):
        self.run_ui(r'''
state.reviews=[item('source')];const details={open:false};
elements.reviewList.querySelector=selector=>selector==='[data-resolution-details="0"]' ? details : null;
openResolutionChoices(0);assert.equal(details.open,true);assert.equal(requests.length,0);
state.reviewBusy=true;details.open=false;openResolutionChoices(0);assert.equal(details.open,false);
''')

    def test_independent_submission_pins_source_reason_and_dependency_preview(self):
        self.run_ui(r'''
const a=item('source-a'),b=item('source-b');
a.entity.entity_id=b.entity.entity_id='same-extraction-group';
a.entity.canonical_name=b.entity.canonical_name='同名公司';
a.evidence.quoted_text='东区的同名公司负责运营。';
state.reviews=[a,b];state.resolutions.set(a.record_id,ready(a.record_id));
state.resolutions.set(b.record_id,ready(b.record_id));
state.resolutions.get(a.record_id).dependent_facts=[{record_id:'fact',revision:1,predicate:'LOCATION'}];
let preview='';globalThis.prompt=()=> '原文描述东区公司，与候选西区公司不同';
globalThis.confirm=text=>{preview=text;return true;};
const pending=submitReviews('APPROVED',[0]);
assert.equal(requests.length,1);const body=JSON.parse(requests[0].options.body);
assert.equal(body.decisions.length,1);assert.equal(body.decisions[0].identity_action,'INDEPENDENT');
assert.equal(body.decisions[0].expected_identity_impact,'preview-source-a');
assert.equal(body.decisions[0].identity_group,undefined);
assert.ok(body.decisions[0].notes.includes('东区'));
assert.ok(preview.includes('同组其他提及保持待确认'));assert.ok(preview.includes('更新 1 条'));
assert.ok(preview.includes('东区的同名公司负责运营。'));
requests[0].reject(new Error('stale preview'));await pending;
assert.equal(state.reviewBusy,false);assert.equal(state.reviews.length,2);
''')

    def test_explicit_grouping_is_distinct_from_separate_allocation(self):
        self.run_ui(r'''
state.reviews=[item('a'),item('b')];
for(const record of state.reviews) state.resolutions.set(record.record_id,ready(record.record_id,'REVIEW'));
globalThis.prompt=()=> '两条原文都明确描述同一对象';globalThis.confirm=()=>true;
let pending=submitReviews('APPROVED',[0,1],false,true);
let decisions=JSON.parse(requests[0].options.body).decisions;
assert.equal(decisions[0].identity_group,decisions[1].identity_group);
assert.ok(decisions[0].identity_group);requests[0].reject(new Error('test retry'));await pending;
pending=submitReviews('APPROVED',[0,1]);decisions=JSON.parse(requests[1].options.body).decisions;
assert.ok(decisions.every(item=>item.identity_group===undefined));
requests[1].reject(new Error('test retry'));await pending;
''')

    def test_cancel_empty_reason_and_group_contradictions_never_send_a_write(self):
        self.run_ui(r'''
state.reviews=[item('a'),item('b')];
for(const record of state.reviews) state.resolutions.set(record.record_id,ready(record.record_id));
for(const reason of [null,'','x'.repeat(2001)]) {
  globalThis.prompt=()=>reason;await submitReviews('APPROVED',[0]);
  assert.equal(requests.length,0);assert.equal(state.reviewBusy,false);
}
globalThis.prompt=()=> '原文确认';globalThis.confirm=()=>false;
await submitReviews('APPROVED',[0]);assert.equal(requests.length,0);
globalThis.confirm=()=>true;
state.resolutions.get('a').identity_properties=[{name:'registration',datatype:'STRING',canonical_value:'one'}];
state.resolutions.get('b').identity_properties=[{name:'registration',datatype:'STRING',canonical_value:'two'}];
await submitReviews('APPROVED',[0,1],false,true);assert.equal(requests.length,0);
state.reviews[1].entity.entity_type='Person';
await submitReviews('APPROVED',[0,1],false,true);assert.equal(requests.length,0);
''')

    def test_success_refreshes_queue_without_approving_dependent_facts(self):
        self.run_ui(r'''
const source=item('a');state.reviews=[source];state.resolutions.set('a',ready('a'));
globalThis.prompt=()=> '依据原文独立建档';globalThis.confirm=()=>true;
const pending=submitReviews('APPROVED',[0]);
requests[0].resolve({outcomes:[{record_id:'a',previous_revision_id:'a-1',revision_id:'a-2',status:'APPROVED'},
  {record_id:'fact',previous_revision_id:'fact-1',revision_id:'fact-2',status:'CANDIDATE'}]});
await flush();
assert.ok(requests[1].url.includes('review-queue'));
requests[1].resolve({items:[]});await pending;
assert.ok(state.approvedRevisions.has('a-2'));
assert.ok(!state.approvedRevisions.has('fact-2'));
assert.equal(state.reviewBusy,false);assert.equal(state.reviews.length,0);
''')
