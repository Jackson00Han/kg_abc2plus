"""Executable automatic-review receipts, progress and explicit retry boundaries."""

import unittest

from tests.fixtures.workbench_ui import governance_source
from tests.unit import test_industrial_web, test_playground_demo_ui


class AutoReviewUiTests(unittest.TestCase):
    run_module = test_industrial_web.IndustrialModuleTests.run_module

    def run_receipt(self, scenario):
        test_playground_demo_ui.PlaygroundDemoUiTests.setUpClass()
        test_playground_demo_ui.PlaygroundDemoUiTests().run_js(scenario)

    def run_review(self, scenario):
        source = governance_source()
        code = source[source.index('function autoReviewSummary('):source.index('function constructionMappingSummary(')]
        self.run_module(r"""
const state={identityEpoch:1,constructionBusy:false,autoReviewBusy:false,reviewBusy:false,publicationBusy:false};
const fields=new Map();
const $=id=>{if(!fields.has(id))fields.set(id,{hidden:true,textContent:'',innerHTML:'',querySelectorAll:()=>[]});return fields.get(id);};
const requests=[],callbacks=new Map();let nextTimer=0,reloads=0,publicationReads=0;
const api=(url,options={})=>{const d=deferred();requests.push({url,options,...d});
 if(url==='/v1/knowledge/construction-jobs/job-1' && !state.controlDetailReads)return Promise.resolve({job_id:'job-1',auto_review:state.detailResponse || receipt});
 return d.promise;};
const make=new Function('state','$','apiRequest','setTimeout','clearTimeout','loadReviews','loadPublicationCandidates',`
const escapeHtml=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const shortId=String,showToast=()=>{};
const elements={constructionOutput:{querySelectorAll:()=>[]}};
${input.code}
return {autoReviewSummary,autoReviewMarkup,rememberAutoReview,retryAutoReview,watchAutoReviewProgress,constructionJobDetail,refreshConstructionReceipt,renderConstructionNext};`);
const view=make(state,$,api,fn=>{callbacks.set(++nextTimer,fn);return nextTimer;},id=>callbacks.delete(id),
 async()=>{reloads++;await state.queueRefreshGate;},async()=>{publicationReads++;});
const receipt={job_id:'job-1',run_id:'run-1',status:'COMPLETED',stage:'DONE',policy_version:'review-v1',
 initiated_by:'user',reviewed_by:'auto-review-service',model_calls:2,updated_at:'2026-09-13',
 counts:{entity_groups:3,approved_groups:2,approved_mentions:5,approved_assertions:10,
 manual_groups:1,manual_assertions:2,blocked_assertions:3,incomplete:0},items:[]};
await new (Object.getPrototypeOf(async function(){}).constructor)(
 'view','state','$','requests','callbacks','receipt','assert','flush','getCounts','deferred',input.case_script)(
 view,state,$,requests,callbacks,receipt,assert,flush,()=>({reloads,publicationReads}),deferred);
""", code=code, case_script=scenario)

    def test_receipt_distinguishes_extraction_and_review_and_preserves_source_audit(self):
        self.run_receipt(r"""
const payload=result();payload.job_id='job';payload.extraction_mode='LLM';payload.chunks[0].status='CANDIDATE';
payload.auto_review={job_id:'job',run_id:'run',status:'COMPLETED',stage:'DONE',reviewed_by:'service',
 policy_version:'v1',model_calls:1,counts:{entity_groups:3,approved_groups:2,approved_mentions:7,
 approved_assertions:30,manual_groups:1,manual_assertions:2,blocked_assertions:4,incomplete:0},
 raw_response:'PRIVATE_MODEL_RESPONSE',items:[{record_id:'approved',record_kind:'ENTITY_MENTION',
 decision:'AUTO_APPROVED',reason:'可靠标识与原文一致',target_entity_id:'entity-1',evidence_ids:['evidence-1'],
 raw_response:'PRIVATE_MODEL_RESPONSE'},{record_id:'uncertain',record_kind:'ENTITY_MENTION',
 decision:'NEEDS_HUMAN',reason:'<img src=x onerror=alert(1)>',evidence_ids:[]}]};
showConstructionResult(payload);
const html=elements.constructionOutput.innerHTML;
assert.match(html,/抽取通过/);assert.match(html,/自动预审完成/);
assert.match(html,/自动通过 <b>2<\/b> 个实体组 · <b>7<\/b> 条提及 · <b>30<\/b> 条事实/);
assert.match(html,/人工判断 <b>1<\/b> 个实体组 · <b>2<\/b> 条事实/);
assert.match(html,/等待身份确认 <b>4<\/b> 条事实/);
assert.match(html,/自动确认不等于发布/);
assert.match(html,/data-auto-review-correct="approved"/);
assert.ok(!html.includes('data-auto-review-correct="uncertain"'));
assert.ok(!html.includes('PRIVATE_MODEL_RESPONSE'));assert.ok(!html.includes('<img'));
assert.match(html,/&lt;img/);assert.equal(requests.length,0);
assert.equal(constructionSummary().auto_review.items[0].record_id,'uncertain');
assert.equal(constructionSummary().auto_review.items[1].evidence_ids[0],'evidence-1');
assert.equal($('review-auto-summary').hidden,false);
""")

    def test_bounded_summary_prioritizes_uncertain_items_without_losing_counts(self):
        self.run_review(r"""
const input={...receipt,secret:'NEVER_SHOW',counts:{...receipt.counts,approved_assertions:900,incomplete:-1},
 items:Array.from({length:160},(_,i)=>({record_id:'r'+i,record_kind:'ASSERTION',decision:'AUTO_APPROVED',reason:'ok',secret:'NEVER_SHOW'}))};
input.items.push({record_id:'needs-human',record_kind:'ENTITY_MENTION',decision:'NEEDS_HUMAN',reason:'标识矛盾'});
const summary=view.autoReviewSummary(input);
assert.equal(summary.items.length,100);assert.equal(summary.items[0].record_id,'needs-human');
assert.equal(summary.truncated,true);assert.equal(summary.counts.approved_assertions,900);
assert.equal(summary.counts.incomplete,0);assert.ok(!JSON.stringify(summary).includes('NEVER_SHOW'));
assert.match(view.autoReviewMarkup(summary),/全部未决记录仍保留在人工队列/);
assert.equal(view.autoReviewSummary({status:'unexpected'}),null);
""")

    def test_required_mapping_gap_remains_visible_when_all_existing_records_pass(self):
        self.run_receipt(r"""
const payload=result();payload.job_id='job';payload.extraction_mode='LLM';
payload.auto_review={job_id:'job',run_id:'run',status:'COMPLETED',stage:'DONE',reviewed_by:'service',
 policy_version:'v1',counts:{approved_groups:139,approved_mentions:310,approved_assertions:915,
 manual_groups:0,manual_assertions:0,blocked_assertions:0,incomplete:0},items:[],
 issues:[{code:'PROPERTY_REQUIRED',property_name:'project_id',entity_count:24,
 entity_ids:Array.from({length:24},(_,i)=>'asset-'+i),record_ids:Array.from({length:24},(_,i)=>'mention-'+i),
 reason:'项目字段未映射到资产，需核对作用域。',source_paths:['/metadata/project_id','<img src=x onerror=alert(1)>'],raw_value:'PRIVATE_CONTEXT_VALUE'}]};
showConstructionResult(payload);
for(const html of [elements.constructionOutput.innerHTML,$('review-auto-summary').innerHTML]) {
 assert.match(html,/自动审核完成，发布前仍需处理映射缺口/);
 assert.match(html,/待补字段 \/ 映射缺口 1 项（影响24 个实体）/);
 assert.match(html,/未入图字段不能默认对所有实体生效/);
 assert.match(html,/project_id · 影响 24 个实体/);
 assert.match(html,/data-auto-review-evidence="mention-0"/);
 assert.ok(!html.includes('data-auto-review-correct'));
 assert.ok(!html.includes('<img'));assert.match(html,/&lt;img/);
 assert.ok(!html.includes('PRIVATE_CONTEXT_VALUE'));
}
assert.equal(constructionSummary().auto_review.issues[0].entity_count,24);
assert.equal(requests.length,0);
""")

    def test_empty_human_queue_does_not_hide_required_gap_or_offer_publication_as_next_step(self):
        source=governance_source()
        start=source.index('function renderReviews(')
        code=source[start:source.index('\n      function ',start+1)]
        self.run_module(r"""
const make=new Function('state','elements',`
const disconnectReviewDetails=()=>{},reviewModel=()=>{},captureReviewUi=()=>new Map();
const reviewProgress=()=>({identities:0,facts:0,paused:0});
const bindReviewActions=()=>{},updateReviewBulkActions=()=>{};
${input.code}
return renderReviews;`);
const state={reviews:[],approvedRevisions:new Set(['approved-revision']),latestAutoReview:{issues:[{code:'PROPERTY_REQUIRED'}]}};
const elements={reviewList:{innerHTML:''}};make(state,elements)();
assert.match(elements.reviewList.innerHTML,/当前没有待审核记录，仍有必填字段缺口/);
assert.match(elements.reviewList.innerHTML,/修正来源映射或资料后重新构建/);
assert.ok(!elements.reviewList.innerHTML.includes('data-review-next'));
assert.ok(!elements.reviewList.innerHTML.includes('当前批次已完成复核'));
""",code=code)

    def test_mapping_gap_counts_deduplicate_entities_and_label_truncated_targets(self):
        self.run_review(r"""
const issue={code:'PROPERTY_REQUIRED',property_name:'project_id',entity_count:2,
 entity_ids:['a','b'],record_ids:['ra','rb'],source_paths:[],reason:'缺少字段'};
const summary=view.autoReviewSummary({...receipt,issues:[issue,{...issue,property_name:'site_id'}]});
assert.match(view.autoReviewMarkup(summary),/缺口 2 项（影响2 个实体）/);
const truncated=view.autoReviewSummary({...receipt,issues:[{...issue,entity_count:24}]});
assert.match(view.autoReviewMarkup(truncated),/影响至少 2 个实体/);
assert.match(view.autoReviewMarkup(truncated),/project_id · 影响 24 个实体/);
assert.match(view.autoReviewMarkup(truncated),/影响实体总数以上方统计为准/);
""")

    def test_retry_is_single_explicit_operation_and_refreshes_current_queues(self):
        self.run_review(r"""
view.rememberAutoReview({...receipt,status:'PARTIAL',counts:{...receipt.counts,incomplete:3}});
const pending=view.retryAutoReview('job-1');
await view.retryAutoReview('job-1');
assert.equal(requests.length,1);assert.equal(requests[0].url,'/v1/knowledge/construction-jobs/job-1/auto-review:run');
assert.equal(requests[0].options.method,'POST');assert.deepEqual(JSON.parse(requests[0].options.body),{retry:true});
requests[0].resolve(receipt);await pending;
assert.equal(state.autoReviewBusy,false);assert.equal(state.latestAutoReview.status,'COMPLETED');
assert.deepEqual(getCounts(),{reloads:1,publicationReads:1});assert.equal(callbacks.size,0);
assert.ok(!requests.some(request=>request.url.includes('publish') || request.url.includes(':construct')));
""")

    def test_progress_reads_review_stages_and_ignores_stale_identity(self):
        self.run_review(r"""
const stop=view.watchAutoReviewProgress('job-1',1);
let [id,callback]=callbacks.entries().next().value;callbacks.delete(id);const first=callback();
assert.equal(requests[0].url,'/v1/knowledge/construction-jobs/job-1/auto-review');
assert.equal(requests[0].options.method,undefined);
requests[0].resolve({...receipt,status:'RUNNING',stage:'FACTS'});await first;
assert.match($('construction-progress').textContent,/正在审核属性与关系/);
[id,callback]=callbacks.entries().next().value;callbacks.delete(id);const second=callback();
state.identityEpoch=2;$('construction-progress').textContent='另一个知识库';
requests[1].resolve(receipt);await second;stop();
assert.equal($('construction-progress').textContent,'另一个知识库');assert.equal(callbacks.size,0);
""")

    def test_retry_poll_ignores_previous_terminal_receipt_until_the_new_run_is_visible(self):
        self.run_review(r"""
const previous={...receipt,updated_at:'2099-01-01T01:00:00Z',issues:[{code:'PROPERTY_REQUIRED',
 property_name:'site_id',entity_count:1,entity_ids:['asset'],record_ids:['mention'],source_paths:[]}]};
view.rememberAutoReview(previous);
const stop=view.watchAutoReviewProgress('job-1',1);
async function poll(value){
 const [id,callback]=callbacks.entries().next().value;callbacks.delete(id);const pending=callback();
 requests.at(-1).resolve(value);await pending;
}
await poll(previous);
assert.equal($('construction-progress').textContent,'正在准备上下文补全…');
assert.equal(state.latestAutoReview.status,'COMPLETED');
await poll({...previous,status:'RUNNING',stage:'CONTEXT',updated_at:'2099-01-01T01:00:01Z'});
assert.equal(state.latestAutoReview.status,'RUNNING');
assert.match($('construction-progress').textContent,/正在核对文档上下文/);
await poll(previous);
assert.equal(state.latestAutoReview.status,'RUNNING','an old terminal receipt must not replace this run');
assert.match($('construction-progress').textContent,/正在核对文档上下文/);
await poll({...receipt,updated_at:'2099-01-01T01:00:02Z'});
assert.equal(state.latestAutoReview.status,'COMPLETED');
assert.equal($('construction-progress').textContent,'自动预审完成');stop();
""")

    def test_new_terminal_server_version_is_accepted_when_a_fast_run_skips_a_running_poll(self):
        self.run_review(r"""
view.rememberAutoReview({...receipt,updated_at:'2001-01-01T00:00:00Z'});
const stop=view.watchAutoReviewProgress('job-1',1);
const [id,callback]=callbacks.entries().next().value;callbacks.delete(id);const polling=callback();
requests[0].resolve({...receipt,updated_at:'2001-01-01T00:00:01Z'});await polling;
assert.equal($('construction-progress').textContent,'自动预审完成');stop();
""")

    def test_retry_result_from_previous_workspace_cannot_update_new_workspace(self):
        self.run_review(r"""
const pending=view.retryAutoReview('job-1');state.identityEpoch=2;
state.latestAutoReview={job_id:'different-job'};state.autoReviewBusy=false;
requests[0].resolve(receipt);await pending;
assert.equal(state.latestAutoReview.job_id,'different-job');
assert.deepEqual(getCounts(),{reloads:0,publicationReads:0});
""")

    def test_failed_retry_restores_last_saved_result_and_keeps_retry_available(self):
        self.run_review(r"""
const previous={...receipt,status:'PARTIAL',counts:{...receipt.counts,incomplete:2}};
view.rememberAutoReview(previous);
const pending=view.retryAutoReview('job-1');
view.rememberAutoReview({...previous,status:'RUNNING',stage:'FACTS'});
requests[0].reject(new Error('provider unavailable'));await pending;
assert.equal(state.latestAutoReview.status,'PARTIAL');assert.equal(state.latestAutoReview.counts.incomplete,2);
assert.equal(state.autoReviewBusy,false);assert.match($('review-auto-summary').innerHTML,/重试未完成的预审/);
assert.deepEqual(getCounts(),{reloads:0,publicationReads:0});
""")

    def test_late_running_poll_cannot_replace_terminal_retry_during_slow_queue_refresh(self):
        self.run_review(r"""
const gate=deferred();state.queueRefreshGate=gate.promise;
view.rememberAutoReview({...receipt,status:'PARTIAL'});
const pending=view.retryAutoReview('job-1');
const [id,callback]=callbacks.entries().next().value;callbacks.delete(id);const polling=callback();
assert.equal(requests.length,2);
requests[0].resolve(receipt);await flush();
assert.equal(state.latestAutoReview.status,'COMPLETED');
requests[1].resolve({...receipt,status:'RUNNING',stage:'FACTS',counts:{...receipt.counts,incomplete:9}});
await polling;
assert.equal(state.latestAutoReview.status,'COMPLETED','late progress must not replace the completed operation');
assert.equal(state.latestAutoReview.counts.incomplete,0);
assert.equal($('construction-progress').hidden,true);
assert.equal($('construction-progress').textContent,'');
assert.equal(callbacks.size,0);
gate.resolve();await pending;
assert.equal(state.autoReviewBusy,false);assert.equal(state.latestAutoReview.status,'COMPLETED');
""")

    def test_context_results_are_separate_from_original_mapping_and_clear_all_gap_navigation(self):
        self.run_receipt(r"""
const payload=result();payload.job_id='job';payload.extraction_mode='LLM';payload.chunks[0].status='CANDIDATE';
payload.chunks[0].mention_record_ids=['device-record'];
payload.chunks[0].mapping_summary={mapping_checksum:'a'.repeat(64),record_count:24,
 collections:[{collection:'/sensors',id_field:'/sensor_ref',entity_types:['Sensor'],record_count:24,
 properties:[],relations:[],retained_fields:['/facility/site_ref']}]};
const review={job_id:'job',run_id:'run',status:'COMPLETED',stage:'DONE',counts:{approved_groups:24,
 approved_mentions:24,approved_assertions:24},items:[],issues:[{code:'PROPERTY_REQUIRED',property_name:'facility_id',
 entity_ids:['sensor-1'],record_ids:['device-record'],entity_count:1,reason:'缺少场地属性',source_paths:['/facility/site_ref']}]};
showConstructionResult({...payload,auto_review:review});
assert.match(elements.constructionOutput.innerHTML,/补全上下文并审核/);
assert.match($('construction-next-button').textContent,/待补字段/);
const repaired={...review,issues:[],context_mapping:{status:'COMPLETED',added_assertions:24,applied:24,
 uncertain:0,overridden:0,issues:[],binding_json:'PRIVATE_BINDING',rules:[{source_path:'/facility/site_ref',
 target_collection:'/sensors',property_name:'facility_id',scope_path:'/facility',binding_mode:'document_scope',
 status:'APPLIED',applied:24,uncertain:0,overridden:0,reason:'场地作用范围已确认',raw_response:'PRIVATE_MODEL'}]}};
showConstructionResult({...payload,auto_review:repaired});
assert.match(elements.constructionOutput.innerHTML,/文档上下文补全 · 已完成/);
assert.match(elements.constructionOutput.innerHTML,/新增 24 条属性/);
assert.ok(!elements.constructionOutput.innerHTML.includes('data-auto-review-gaps'));
assert.ok(!$('review-auto-summary').innerHTML.includes('data-auto-review-gaps'));
assert.ok(!elements.constructionOutput.innerHTML.includes('PRIVATE_'));
assert.equal($('construction-next-button').textContent,'下一步：查看已确认内容 →');
assert.match($('construction-next-note').textContent,/检查发布预览/);
assert.equal(constructionSummary().chunks[0].mapping_summary.mapping_checksum,'a'.repeat(64));
assert.equal(constructionSummary().chunks[0].mapping_summary.collections[0].properties.length,0);
assert.equal(requests.length,0);
""")

    def test_context_scope_distinguishes_document_root_from_missing_path(self):
        self.run_review(r"""
for(const [scope,label] of [['','文档根节点'],[undefined,'未确认'],[null,'未确认'],['/facility','/facility']]){
 const rule={source_path:'/metadata/site_id',target_collection:'/sensors',property_name:'site_id',
  binding_mode:'IDENTITY_TEMPLATE',status:'COMPLETED',applied:2,uncertain:0,overridden:0,reason:'来源模板一致'};
 if(scope!==undefined)rule.scope_path=scope;
 const summary=view.autoReviewSummary({...receipt,context_mapping:{status:'COMPLETED',added_assertions:2,
  applied:2,uncertain:0,overridden:0,issues:[],rules:[rule]}});
 const html=view.autoReviewMarkup(summary);
 assert.ok(html.includes('作用域：'+label+' · 绑定方式：来源身份模板'));
 assert.equal(summary.context_mapping.rules[0].scope_path,scope??null);
 if(scope!=='' && scope!='/facility')assert.ok(!html.includes('文档根节点'));
}
""")

    def test_partial_context_conflicts_remain_visible_with_escaped_rules(self):
        self.run_review(r"""
const conflict={code:'CONTEXT_LOCAL_CONFLICT',property_name:'facility_id',entity_ids:['sensor-2'],record_ids:['r-2'],
 entity_count:1,reason:'局部值与文档范围冲突',source_paths:['/facility/site_ref']};
const value=view.autoReviewSummary({...receipt,issues:[conflict],context_mapping:{status:'PARTIAL',
 added_assertions:3,applied:3,uncertain:1,overridden:2,issues:[conflict],rules:[{
 source_path:'<img src=x>',target_collection:'/sensors',property_name:'facility_id',scope_path:'/facility',
 binding_mode:'document_scope',status:'UNCERTAIN',applied:3,uncertain:1,overridden:2,reason:'<script>alert(1)</script>',raw_response:'PRIVATE'}]}});
const html=view.autoReviewMarkup(value);
assert.match(html,/文档上下文补全 · 部分完成/);assert.match(html,/待核对 1 项 · 保留局部值 2 项/);
assert.match(html,/映射缺口 1 项/);assert.match(html,/局部值与文档范围冲突/);
assert.equal((html.match(/data-auto-review-gaps/g)||[]).length,1);
assert.ok(!html.includes('<script>'));assert.ok(!html.includes('<img'));assert.match(html,/&lt;img/);
assert.ok(!html.includes('PRIVATE'));assert.match(html,/补全上下文并审核/);
""")

    def test_same_job_refresh_removes_background_resolved_gap_without_writing(self):
        self.run_review(r"""
state.controlDetailReads=true;
view.rememberAutoReview({...receipt,issues:[{code:'PROPERTY_REQUIRED',property_name:'site_id',entity_count:1,
 entity_ids:['a'],record_ids:['ra'],source_paths:[],reason:'未映射'}]});
const refreshed=view.refreshConstructionReceipt('job-1');
assert.equal(requests[0].options.method,undefined);
requests[0].resolve({job_id:'job-1',auto_review:{...receipt,issues:[]}});await refreshed;
assert.equal(state.latestAutoReview.issues.length,0);
assert.ok(!$('review-auto-summary').innerHTML.includes('data-auto-review-gaps'));
const again=view.refreshConstructionReceipt('job-1');assert.equal(requests.length,2);
requests[1].resolve({job_id:'job-1',auto_review:receipt});await again;
assert.ok(requests.every(request=>request.options.method===undefined));
""")

    def test_parallel_job_detail_reads_share_only_the_pending_request(self):
        self.run_review(r"""
state.controlDetailReads=true;
const automatic=view.refreshConstructionReceipt('job-1');
const clicked=view.constructionJobDetail('job-1');
const duplicate=view.constructionJobDetail('job-1');
assert.equal(clicked,duplicate);assert.equal(requests.length,1);
requests[0].resolve({job_id:'job-1',auto_review:receipt});
await Promise.all([automatic,clicked,duplicate]);
assert.equal(state.constructionDetailRequests.size,0);
const fresh=view.constructionJobDetail('job-1');assert.equal(requests.length,2);
requests[1].resolve({job_id:'job-1',auto_review:receipt});await fresh;
""")

    def test_job_detail_deduplication_cannot_cross_identity_or_review_generation(self):
        self.run_review(r"""
state.controlDetailReads=true;
const old=view.refreshConstructionReceipt('job-1');
state.identityEpoch++;
const otherIdentity=view.refreshConstructionReceipt('job-1');
state.autoReviewEpoch=(state.autoReviewEpoch || 0)+1;
const afterRepair=view.refreshConstructionReceipt('job-1');
assert.equal(requests.length,3);
requests[0].resolve({job_id:'job-1',auto_review:{...receipt,run_id:'old-identity'}});
requests[1].resolve({job_id:'job-1',auto_review:{...receipt,run_id:'old-generation'}});
assert.equal(await old,null);assert.equal(await otherIdentity,null);
assert.equal(state.constructionDetailRequests.size,1);
requests[2].resolve({job_id:'job-1',auto_review:receipt});await afterRepair;
assert.equal(state.latestAutoReview.run_id,'run-1');
assert.equal(state.constructionDetailRequests.size,0);
""")

    def test_failed_shared_detail_request_is_not_cached_for_a_later_retry(self):
        self.run_review(r"""
state.controlDetailReads=true;
const first=view.constructionJobDetail('job-1'),second=view.constructionJobDetail('job-1');
const outcomes=Promise.allSettled([first,second]);
requests[0].reject(new Error('暂不可用'));
assert.ok((await outcomes).every(value=>value.status==='rejected'));
assert.equal(state.constructionDetailRequests.size,0);
const next=view.constructionJobDetail('job-1');assert.equal(requests.length,2);
requests[1].resolve({job_id:'job-1',auto_review:receipt});await next;
""")

    def test_identical_detail_refresh_preserves_open_receipt_evidence(self):
        self.run_review(r"""
state.constructionReceipt={job_id:'job-1',auto_review:receipt};
// This narrow helper harness deliberately has no showConstructionResult: an unchanged
// read must update the summary/navigation without replacing the receipt DOM.
const refreshed=await view.refreshConstructionReceipt('job-1',{renderReceipt:true});
assert.deepEqual(refreshed,state.constructionReceipt);
assert.equal(state.latestAutoReview.run_id,'run-1');
assert.equal(requests.length,1);assert.equal(requests[0].options.method,undefined);
""")

    def test_old_job_detail_cannot_restore_gap_after_context_repair(self):
        self.run_review(r"""
state.controlDetailReads=true;
const old=view.refreshConstructionReceipt('job-1');
state.controlDetailReads=false;
const repair=view.retryAutoReview('job-1');requests[1].resolve({...receipt,issues:[]});await repair;
requests[0].resolve({job_id:'job-1',auto_review:{...receipt,issues:[{code:'PROPERTY_REQUIRED',property_name:'old_gap',
 entity_ids:['old'],record_ids:['old'],entity_count:1,reason:'stale',source_paths:[]}]}});
assert.equal(await old,null);assert.equal(state.latestAutoReview.issues.length,0);
assert.equal(requests.filter(request=>request.options.method==='POST').length,1);
""")

    def test_context_phase_is_shown_before_identity_and_facts(self):
        self.run_review(r"""
const stop=view.watchAutoReviewProgress('job-1',1);
const [id,callback]=callbacks.entries().next().value;callbacks.delete(id);const polling=callback();
requests[0].resolve({...receipt,status:'RUNNING',stage:'CONTEXT'});await polling;
assert.equal(state.latestAutoReview.stage,'CONTEXT');
assert.match($('construction-progress').textContent,/文档上下文与适用范围/);stop();
""")

    def test_context_property_shows_two_original_evidence_roles_without_binding_json(self):
        source=governance_source()
        code=source[source.index('function reviewEvidence('):source.index('function reviewAssessment(')]
        self.run_module(r"""
const make=new Function('apiRequest',`
const state={identityEpoch:1},shortId=String;
const escapeHtml=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const foregroundAction=(_label,fn)=>fn;
${input.code}
return {reviewEvidence,loadEvidenceContext};`);
const requests=[];
const view=make(async(url,options)=>{
 const body=JSON.parse(options.body);requests.push({url,body});
 const text=body.evidence_role==='CONTEXT_VALUE'?'"site-A"':'sensor-1';
 return {text,context_start:0,context_end:text.length,char_start:0,char_end:text.length,
 document_title:'Source',version_id:'v1',chunk_id:'c1',view:'paragraph',document_accessible:false};
});
const record={record_id:'property-1',revision:3,context_property_evidence:{value_pointer:'<img src=x>',
 scope_pointer:'/facility',record_pointer:'/sensors/0',identity_pointer:'/sensors/0/id',binding_json:'PRIVATE_BINDING',
 value_evidence:{chunk_id:'context-chunk',char_start:15,char_end:23,quoted_text:'"site-A"'}}};
const html=view.reviewEvidence({chunk_id:'entity-chunk',char_start:1,char_end:9,quoted_text:'sensor-1'},'原文',record);
assert.match(html,/>实体原文</);assert.match(html,/>上下文字段原文</);
assert.match(html,/data-evidence-role="CONTEXT_VALUE"/);assert.ok(!html.includes('PRIVATE_BINDING'));
assert.ok(!html.includes('<img'));assert.match(html,/&lt;img/);
for(const role of [undefined,'CONTEXT_VALUE']){
 const panel={innerHTML:'',querySelectorAll:()=>[],querySelector:()=>null};
 const details={dataset:{evidenceRecord:'property-1',evidenceRevision:'3',evidenceRole:role},isConnected:true,querySelector:()=>panel};
 await view.loadEvidenceContext(details);
 assert.match(panel.innerHTML,/<mark>/);
}
assert.equal(requests[0].body.evidence_role,undefined);
assert.equal(requests[1].body.evidence_role,'CONTEXT_VALUE');
assert.ok(requests.every(request=>request.url==='/v1/knowledge/review-evidence'));
""",code=code)


    def test_stale_and_unversioned_reasons_never_look_like_current_findings(self):
        source = governance_source()
        start = source.index('function autoReviewReasonMarkup(')
        code = source[start:source.index('\n      function ', start + 1)]
        self.run_module(r"""
const state={latestAutoReview:{items:[{record_id:'r',input_revision:2,decision:'NEEDS_HUMAN',reason:'<疑点>'}]}};
const make=new Function('state',`const escapeHtml=s=>String(s).replaceAll('<','&lt;');${input.code};return autoReviewReasonMarkup;`);
const render=make(state),item={record_id:'r',revision:2,trust:{status:'CANDIDATE'}};
assert.match(render(item),/模型存疑，待人工复核/);assert.match(render(item),/&lt;疑点>/);
assert.equal(render({...item,revision:3}),'');
assert.equal(render({...item,trust:{status:'APPROVED'}}),'');
state.latestAutoReview.items[0].input_revision=null;assert.equal(render(item),'');
state.latestAutoReview.items[0].input_revision=2;state.latestAutoReview.items[0].decision='INCOMPLETE';
assert.match(render(item),/自动审核未完成/);
""", code=code)

    def test_identity_resume_sends_only_selected_confirmed_record_ids(self):
        self.run_review(r"""
view.rememberAutoReview(receipt);
const pending=view.retryAutoReview('job-1',['mention-1']);await flush();
const request=requests.find(r=>r.url.endsWith('auto-review:run'));
assert.deepEqual(JSON.parse(request.options.body),{retry:true,resume_identity_record_ids:['mention-1']});
request.resolve(receipt);await pending;
assert.equal(state.autoReviewBusy,false);
assert.match(view.autoReviewMarkup(receipt),/续审未决记录/);
""")

    def test_identity_resume_never_selects_an_unrelated_job(self):
        source=governance_source()
        start=source.index('function identityResumeRequest(')
        code=source[start:source.index('\n      async function ',start+1)]
        self.run_module(r"""
const state={latestAutoReview:{job_id:'job-a',status:'COMPLETED',items:[{record_id:'m-a'}]}};
const select=new Function('state',`${input.code};return identityResumeRequest;`)(state);
const item={record_id:'m-a',record_kind:'ENTITY_MENTION'};
assert.deepEqual(select([item]),{jobId:'job-a',ids:['m-a']});
assert.equal(select([{...item,record_id:'m-b'}]),null);
assert.equal(select([{...item,record_kind:'ASSERTION'}]),null);
state.latestAutoReview.status='RUNNING';assert.equal(select([item]),null);
""",code=code)


if __name__ == '__main__':
    unittest.main()
