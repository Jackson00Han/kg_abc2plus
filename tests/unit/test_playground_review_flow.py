"""Dependency-guided review behavior executed from the actual page JavaScript."""
import unittest

from tests.unit import test_playground_resolution as resolution_checks


class PlaygroundGuidedReviewTests(unittest.TestCase):
    def run_ui(self, scenario: str) -> None:
        resolution_checks.PlaygroundResolutionTests().run_ui(scenario)

    def test_grouping_uses_entity_ids_and_preserves_independent_mentions(self) -> None:
        self.run_ui(r'''
const a=item('a'), b=item('b'), homonym=item('c');
a.entity.entity_id='pump-101'; b.entity.entity_id='pump-101'; homonym.entity.entity_id='pump-202';
a.entity.canonical_name=b.entity.canonical_name=homonym.entity.canonical_name='循环水泵';
state.reviews=[a,b,homonym];
renderReviews();
assert.equal((elements.reviewList.innerHTML.match(/class="review-entity-card"/g)||[]).length,2);
assert.ok(elements.reviewList.innerHTML.includes('2 条来源提及'));
assert.ok(elements.reviewList.innerHTML.includes('review-record-a'));
assert.ok(elements.reviewList.innerHTML.includes('review-record-b'));
assert.notEqual(reviewGroupKey(a),reviewGroupKey(homonym));
assert.ok(!elements.reviewList.innerHTML.includes('data-review-next'));
// An empty queue is not evidence that any knowledge was reviewed or approved.
state.reviews=[];
renderReviews();
assert.ok(elements.reviewList.innerHTML.includes('当前没有待确认记录'));
assert.ok(elements.reviewList.innerHTML.includes('请先上传文档并抽取'));
assert.ok(!elements.reviewList.innerHTML.includes('data-review-next'));
assert.ok(!elements.reviewList.innerHTML.includes('class="review-complete"'));
// Deferred records need investigation and alone do not justify a publish action.
const paused=item('paused',1,'ASSERTION'); paused.trust.status='QUARANTINED';
state.reviews=[paused];
renderReviews();
assert.ok(elements.reviewList.innerHTML.includes('当前只有暂缓记录'));
assert.ok(elements.reviewList.innerHTML.includes('本次没有已批准的待发布内容'));
assert.ok(!elements.reviewList.innerHTML.includes('data-review-next'));
state.approvedRevisions.add('actually-approved-revision');
renderReviews();
assert.ok(elements.reviewList.innerHTML.includes('data-review-next'));
assert.ok(elements.reviewList.innerHTML.includes('暂缓记录不参与发布'));
assert.ok(elements.reviewList.innerHTML.includes('暂缓记录仍需核查'));
state.reviews=[];
renderReviews();
assert.ok(elements.reviewList.innerHTML.includes('data-review-next'));
assert.ok(!elements.reviewList.innerHTML.includes('当前没有待确认记录'));
// Existing approvals must not suggest skipping other still-actionable records.
for (const pending of [item('pending-entity'),item('pending-fact',1,'ASSERTION')]) {
  state.reviews=[pending]; state.reviewPhase='identities';
  renderReviews();
  assert.ok(!elements.reviewList.innerHTML.includes('data-review-next'));
}
state.reviews=[]; state.approvedRevisions.clear();
renderReviews();
assert.ok(!elements.reviewList.innerHTML.includes('data-review-next'));
assert.ok(elements.reviewList.innerHTML.includes('当前没有待确认记录'));
''')

    def test_approval_requires_current_identity_or_ready_fact_assessment(self) -> None:
        self.run_ui(r'''
const entity=item('pump'), fact=item('fact',1,'ASSERTION');
state.reviews=[entity,fact]; reviewModel();
state.resolutions.set('pump',{revision:1,status:'ready',identityEpoch:0,reviewEpoch:0,suggestions:[{outcome:'NO_MATCH'}]});
assert.equal(reviewApproval(entity).allowed,true);
state.reviewEpoch+=1;
assert.equal(reviewApproval(entity).allowed,false);
state.resolutions.get('pump').reviewEpoch=1;
state.resolutions.get('pump').suggestions=[{outcome:'AUTO_LINK',target:{entity_id:'expert-pump'}}];
assert.equal(reviewApproval(entity).allowed,false);
assert.ok(reviewApproval(entity).note.includes('已有实体'));
state.reviewAssessments.set('fact',{revision:1,identityEpoch:0,reviewEpoch:1,status:'BLOCKED',summary:'先确认设备',dependencies:[{role:'subject',mention_record_id:'pump',name:'泵',ready:false}],matches:[]});
assert.equal(reviewApproval(fact).allowed,false);
assert.ok(assessmentMarkup(fact,1).includes('data-review-dependency="pump"'));
state.reviewAssessments.get('fact').status='READY';
assert.equal(reviewApproval(fact).allowed,true);
state.reviewAssessments.get('fact').status='DUPLICATE';
assert.equal(reviewApproval(fact).allowed,false);
state.reviewAssessments.get('fact').status='CONFLICT';
assert.equal(reviewApproval(fact).allowed,false);
''')

    def test_assessment_workers_are_bounded_and_discard_old_identity_results(self) -> None:
        self.run_ui(r'''
const pending=[];
apiRequest=(url)=>new Promise(resolve=>pending.push({url,resolve}));
state.reviews=['a','b','c','d'].map(id=>item(id,1,'ASSERTION'));
for(const record of state.reviews) void queueAssessment(record);
assert.equal(pending.length,2); assert.equal(state.assessmentActive,2);
state.identityEpoch+=1; invalidateReviewResolutions();
state.reviews=[item('new',1,'ASSERTION')];
void queueAssessment(state.reviews[0]);
assert.equal(pending.length,2);
pending[0].resolve({record_id:'a',revision:1,status:'READY'}); await flush();
assert.equal(pending.length,3);
pending[1].resolve({record_id:'b',revision:1,status:'READY'});
pending[2].resolve({record_id:'new',revision:1,status:'READY',summary:'ready'}); await flush();
assert.equal(state.assessmentActive,0);
assert.equal(state.reviewAssessments.size,1);
assert.equal(state.reviewAssessments.get('new').status,'READY');
''')

    def test_duplicate_dismissal_is_explicit_and_audited(self) -> None:
        self.run_ui(r'''
const fact=item('fact',2,'ASSERTION');state.reviews=[fact];reviewModel();
state.reviewAssessments.set('fact',{revision:2,identityEpoch:0,reviewEpoch:0,status:'DUPLICATE',matches:[{record:{revision_id:'authority-revision'}}]});
const sent=[];
apiRequest=async(url,options)=>{sent.push({url,body:JSON.parse(options.body)});return {outcomes:[]};};
loadReviews=async()=>{};
assert.equal(sent.length,0);
await keepExistingFact(0);
assert.equal(sent.length,1);
const decision=sent[0].body.decisions[0];
assert.equal(decision.expected_revision,2);
assert.equal(decision.decision,'REJECTED');
assert.equal(decision.duplicate_of_revision_id,'authority-revision');
assert.ok(decision.notes.includes('本次原文与审核记录保留'));
state.identityEpoch+=1;
await keepExistingFact(0);
assert.equal(sent.length,1);
''')

    def test_fact_table_shows_units_times_and_comparison_without_raw_json_wall(self) -> None:
        self.run_ui(r'''
const fact=item('power',1,'ASSERTION');
fact.subject={entity_id:'pump',canonical_name:'泵'};fact.object_entity=null;fact.predicate='RatedPower';
fact.literal_semantics={canonical_value:'37.5',canonical_unit:'kW',valid_from:'2026-01-01',valid_to:'2026-12-31',observed_at:'2026-06-01'};
state.reviews=[fact];reviewModel();state.reviewPhase='facts';
state.reviewAssessments.set('power',{revision:1,identityEpoch:0,reviewEpoch:0,status:'DUPLICATE',summary:'已有权威事实',matches:[{record:{...fact,revision_id:'expert'}}],dependencies:[]});
renderReviews();
const html=elements.reviewList.innerHTML;
assert.ok(html.includes('review-facts'));
assert.ok(html.includes('额定功率：37.5 kW'));
assert.ok(html.includes('2026-12-31'));
assert.ok(html.includes('保留已有事实，不重复入图'));
assert.ok(html.includes('<details class="review-technical"'));
assert.ok(html.includes('data-review-editor="0" disabled'));
''')

    def test_blocked_batch_never_sends_partial_approval(self) -> None:
        self.run_ui(r'''
state.reviews=[item('a',1,'ASSERTION'),item('b',1,'ASSERTION')];reviewModel();
for(const record of state.reviews) state.reviewAssessments.set(record.record_id,{revision:1,identityEpoch:0,reviewEpoch:0,status:record.record_id==='a'?'READY':'BLOCKED',summary:'先确认实体'});
await submitReviews('APPROVED',[0,1]);
assert.equal(requests.length,0);
// Pending rows can still be selected for explicit defer/reject.
assert.ok(!reviewSelection(state.reviews[1],1).includes('disabled'));
// A previous READY result cannot authorize newly edited content.
const editor={disabled:false,value:JSON.stringify(reviewEdit(state.reviews[0]))};
elements.reviewList.querySelector=selector=>selector.includes('data-review-editor="0"') ? editor : null;
await submitReviews('APPROVED',[0],true);
assert.equal(requests.length,0);
await keepExistingFact(0);
assert.equal(requests.length,0,'duplicate action must not discard the draft');
state.reviews[0]=item('mention');
state.resolutions.set('mention',{...resolution('mention'),identityEpoch:0,reviewEpoch:0,status:'ready'});
await applyResolution(0,0,{});
assert.equal(requests.length,0,'link action must not discard the draft');
assert.equal(confirmations().promptCount,0);
''')
