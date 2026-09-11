"""Regression checks for stable review position and real operation feedback."""

import json
from pathlib import Path
import subprocess
import unittest

from tests.fixtures.workbench_ui import governance_source
from tests.unit import test_playground_resolution as ui_checks


class ReviewContinuityTests(unittest.TestCase):
    def run_ui(self, scenario):
        ui_checks.PlaygroundResolutionTests().run_ui(scenario)

    def test_remaining_pump_mentions_keep_group_and_source_order_after_refresh(self):
        self.run_ui(r'''
const pump=id=>({...item(id),entity:{entity_id:'pump',entity_type:'Equipment',canonical_name:'循环水泵'}});
const seal={...item('seal-1'),entity:{entity_id:'seal',entity_type:'Component',canonical_name:'机械密封'}};
state.reviews=[pump('pump-1'),seal,pump('pump-2'),pump('pump-3')];
const loading=loadReviews();
requests[0].resolve({items:[seal,pump('pump-3'),pump('pump-2')]});await loading;
assert.deepEqual(state.reviews.map(value=>value.record_id),['pump-2','pump-3','seal-1']);
assert.ok(elements.reviewList.innerHTML.indexOf('data-review-group-key="pump"')<elements.reviewList.innerHTML.indexOf('data-review-group-key="seal"'));
const homonym={...pump('other-pump'),entity:{entity_id:'other-pump',entity_type:'Equipment',canonical_name:'循环水泵'}};
const ordered=stableReviewOrder(state.reviews,[homonym,seal,pump('pump-3'),pump('pump-2')]);
assert.deepEqual(ordered.map(value=>value.record_id),['pump-2','pump-3','seal-1','other-pump']);
''')

    def test_removed_record_anchors_to_remaining_pump_without_resurrecting_it(self):
        self.run_ui(r'''
const positions={'pump-1':140,'pump-2':560,'pump-3':840,'seal':1100};
const rows=['pump-1','pump-2','pump-3','seal'].map(id=>({dataset:{reviewRecord:id},
  getBoundingClientRect:()=>({top:positions[id],bottom:positions[id]+380}),
  closest:()=>id==='seal'?sealCard:pumpCard}));
const pumpCard={dataset:{reviewGroupKey:'pump'},querySelectorAll:()=>rows.slice(0,3),getBoundingClientRect:()=>({top:110})};
const sealCard={dataset:{reviewGroupKey:'seal'},querySelectorAll:()=>[rows[3]],getBoundingClientRect:()=>({top:1070})};
elements.reviewList.querySelectorAll=selector=>selector==='[data-review-record]'?rows:[pumpCard,sealCard];
const anchor=captureReviewViewport('pump-1');assert.equal(anchor.recordId,'pump-1');
const remaining=[rows[1],rows[2],rows[3]];
elements.reviewList.querySelectorAll=selector=>selector==='[data-review-record]'?remaining:[pumpCard,sealCard];
positions['pump-2']=260;let scrolled;
globalThis.scrollBy=options=>{scrolled=options;};restoreReviewViewport(anchor);
assert.equal(scrolled.top,120);assert.equal(scrolled.behavior,'instant');
assert.equal(remaining.some(row=>row.dataset.reviewRecord==='pump-1'),false);
// If the reviewer moved to a different card while saving, preserve their current view.
positions['pump-2']=-1000;positions['pump-3']=-500;positions.seal=150;
assert.equal(captureReviewViewport('pump-2').recordId,'seal');
''')

    def test_review_unlocks_after_its_queue_without_waiting_for_other_panels(self):
        self.run_ui(r'''
state.reviews=[item('pump-1')];
globalThis.loadActiveDocuments=()=>new Promise(()=>{});
globalThis.loadPublicationCandidates=()=>new Promise(()=>{});
const saving=submitReviews('REJECTED',[0]);assert.equal(state.reviewBusy,true);
requests[0].resolve({outcomes:[{record_id:'pump-1',status:'REJECTED',previous_revision_id:'pump-1-1',revision_id:'pump-1-2'}]});
await flush();assert.ok(requests[1].url.includes('review-queue'));
requests[1].resolve({items:[]});await saving;
assert.equal(state.reviewBusy,false);assert.equal(state.reviews.length,0);
''')

    def test_only_unchanged_unrelated_completed_checks_are_reused(self):
        self.run_ui(r'''
const mention={...item('pump-1'),entity:{entity_id:'pump',entity_type:'Equipment',canonical_name:'循环水泵'}};
const fact=id=>({...item(id,1,'ASSERTION'),entity:undefined,subject:{entity_id:id==='pump-power'?'pump':'seal',entity_type:id==='pump-power'?'Equipment':'Component'},object_entity:null});
const affected=fact('pump-power'),unrelated=fact('seal-material');state.reviews=[mention,affected,unrelated];reviewModel();
for(const value of [affected,unrelated]) {
  state.reviewAssessments.set(value.record_id,{revision:1,identityEpoch:0,reviewEpoch:0,status:'READY',done:Promise.resolve()});
  state.propertyAssignments ||= new Map();
  state.propertyAssignments.set(value.record_id,{revision:1,identityEpoch:0,reviewEpoch:0,loading:false,items:[],open:false});
}
const kept=state.reviewAssessments.get('seal-material');
const impact=reviewRefreshImpact([{record_id:'pump-1'}]);
const loading=loadReviews({impact});requests[0].resolve({items:[unrelated,affected]});await loading;
assert.equal(state.reviewAssessments.get('seal-material'),kept);
assert.equal(kept.reviewEpoch,state.reviewEpoch);assert.equal(propertyAssignment(unrelated).loading,false);
assert.notEqual(state.reviewAssessments.get('pump-power'),kept);
assert.equal(reviewCheckUnaffected({...mention,record_id:'another-pump'},impact),false);
// A new source revision is never allowed to reuse an old completed check.
const again=loadReviews({impact});requests.find(request=>!request.settled&&request.url.includes('review-queue')).resolve({items:[{...unrelated,revision:2}]});await again;
assert.notEqual(state.reviewAssessments.get('seal-material'),kept);
''')

    def test_confirmed_automatic_suggestion_pins_selectable_target_from_same_response(self):
        self.run_ui(r'''
const source=item('pump');state.reviews=[source];
const match={...resolution('pump'),identityEpoch:0,reviewEpoch:0,status:'ready',review_targets:[
  {record_id:'other-mention',revision:9,entity:{entity_id:'other-pump'},selectable:true},
  {record_id:'published-pump-mention',revision:4,entity:{entity_id:'authority-pump'},selectable:true}]};
state.resolutions.set('pump',match);globalThis.prompt=()=> '已核对循环水泵编号';globalThis.confirm=()=>true;
const pending=applyResolution(0,0,{});
assert.equal(requests.length,1);const body=JSON.parse(requests[0].options.body);
assert.equal(body.target_entity_id,'authority-pump');assert.equal(body.target_record_id,'published-pump-mention');
assert.equal(body.target_expected_revision,4);assert.equal(body.expected_revision,1);
requests[0].reject(new Error('fixture rejected write'));await pending;assert.equal(state.reviewBusy,false);
''')

    def test_automatic_suggestion_without_selectable_exact_record_keeps_server_fallback(self):
        self.run_ui(r'''
for(const review_targets of [[],[{record_id:'blocked-pump',revision:4,entity:{entity_id:'authority-pump'},selectable:false}]]) {
  state.reviews=[item('pump')];state.resolutions.set('pump',{...resolution('pump'),identityEpoch:0,reviewEpoch:0,status:'ready',review_targets});
  globalThis.prompt=()=> '已核对原文';globalThis.confirm=()=>true;
  const pending=applyResolution(0,0,{}),request=requests.at(-1),body=JSON.parse(request.options.body);
  assert.equal(body.target_entity_id,'authority-pump');assert.equal(body.target_record_id,undefined);
  assert.equal(body.target_expected_revision,undefined);request.reject(new Error('fixture rejected write'));await pending;
}
''')

    def test_older_background_candidates_cannot_remove_new_approval(self):
        source = governance_source()
        function = source[source.index('      async function loadPublicationCandidates('):source.index('      function renderHistory(')]
        self.run_ui('const fetchCandidates = (' + function.strip() + ');' + r'''
state.approvedRevisions.add('previous');state.selectedCandidateRevisions.add('previous');
elements.publicationRevisions.value='previous';
const loading=fetchCandidates();
trackReviewedOutcomes([{record_id:'pump',previous_revision_id:'pump-1',revision_id:'pump-2',status:'APPROVED'}]);
requests[0].resolve({items:[]});await loading;
assert.ok(state.approvedRevisions.has('pump-2'));
assert.ok(elements.publicationRevisions.value.includes('pump-2'));
''')


class OperationFeedbackTests(unittest.TestCase):
    def test_button_lifetime_concurrency_errors_and_identity_reset(self):
        source = governance_source()
        source = source[source.index('  function beginOperationFeedback('):source.index('  function uploadContext(')]
        core = Path('src/graphrag_prod/playground/static/industrial/core.mjs').read_text()
        helper = core[core.index('const pendingButtons'):core.index('export function clear(node)')].replace('export ', '')
        harness = r'''
const assert=require('node:assert/strict');const vm=require('node:vm');
const {source,helper}=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const node=()=>({isConnected:true,disabled:false,classList:{values:new Set(),add(x){this.values.add(x);},remove(x){this.values.delete(x);}},attrs:{},setAttribute(key,value){this.attrs[key]=value;},getAttribute(key){return this.attrs[key];},removeAttribute(key){delete this.attrs[key];}});
const button=node(), other=node(), requests=[];
const context=vm.createContext({state:{},document:{contains:()=>true},button,other,assert,requests,
  client:{epoch:0,requestOptions:(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}))},
  onPublished:async()=>{},showToast(){},setReviewBusy(){}});
vm.runInContext(helper+source,context);
(async()=>{await vm.runInContext(`(async()=>{
const first=beginButtonFeedback(button),second=beginButtonFeedback(button);
assert.equal(button.disabled,true);assert.equal(button.attrs['aria-busy'],'true');
first();first();assert.equal(button.attrs['aria-busy'],'true');second();
assert.equal(button.disabled,false);assert.equal(button.attrs['aria-busy'],undefined);
const action=foregroundAction('',()=>apiRequest('/v1/knowledge/reviews:batch',{method:'POST'}));
const pending=action({currentTarget:button});assert.equal(button.disabled,true);
assert.equal(button.classList.values.has('button-pending'),true);
await action({currentTarget:button});assert.equal(requests.length,1);
requests[0].reject(new Error('保存失败'));await assert.rejects(pending,/保存失败/);
assert.equal(button.disabled,false);assert.equal(button.attrs['aria-busy'],undefined);
const automatic=apiRequest('/v1/knowledge/graph:query',{method:'POST',feedback:'正在读取'});
assert.equal(button.attrs['aria-busy'],undefined);assert.equal(other.attrs['aria-busy'],undefined);
assert.equal(requests[1].options.feedback,undefined);requests[1].resolve({});await automatic;
const stale=beginButtonFeedback(button);clearButtonFeedback();
assert.equal(button.disabled,false);const current=beginButtonFeedback(button);stale();
assert.equal(button.attrs['aria-busy'],'true');current();assert.equal(button.disabled,false);
})()`,context);})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', harness], input=json.dumps({'source':source,'helper':helper}), text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_animation_has_reduced_motion_fallback_and_no_floating_indicator(self):
        css = Path('src/graphrag_prod/playground/static/industrial/workbench.css').read_text()
        self.assertIn('button.button-pending::before', css)
        self.assertIn('@media(prefers-reduced-motion:reduce)', css)
        self.assertIn('animation:none', css)
        governance = Path('src/graphrag_prod/playground/static/industrial/governance.css').read_text()
        self.assertNotIn('.operation-feedback', governance)
        self.assertNotIn('.operation-inline', governance)


if __name__ == '__main__':
    unittest.main()
