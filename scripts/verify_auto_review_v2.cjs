/** Synthetic review/identity resume fixtures; every knowledge write is intercepted. */
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
const {chromium}=require('playwright');
const base='http://127.0.0.1:8002', output=path.resolve('.local/browser-qa/auto-review-v2');
const reads=new Set(['/playground/session','/v1/knowledge/graph:query','/v1/knowledge/graph:evidence',
 '/v1/knowledge/sources:query','/v1/knowledge/sources:read','/v1/knowledge/publications:compare',
 '/v1/knowledge/quality/reviews:query','/v1/knowledge/review-evidence']);
const quote='测试项目的接头 J1，完整名称为 Test.Section.Joint1。';
const trust={authority:'AUTHORITATIVE',origin:'AUTHORITATIVE_EXTRACTED',status:'CANDIDATE'};
const entity={entity_id:'fixture-j1',entity_type:'Joint',canonical_name:'TEST/J1',canonical_key:'fixture:j1',aliases:[]};
const evidence={version_id:'fixture-v1',chunk_id:'fixture-chunk',char_start:0,char_end:quote.length,quoted_text:quote};
const mention={record_id:'fixture-mention',revision:1,revision_id:'fixture-mention-r1',record_kind:'ENTITY_MENTION',entity,trust,evidence};
const fact={record_id:'fixture-fact',revision:1,revision_id:'fixture-fact-r1',record_kind:'ASSERTION',subject:entity,
 predicate:'full_name',literal_value:'Test.Section.Joint1',trust,evidence};
let queue=[mention,fact], human=false, resumes=0;
const summary={job_id:'fixture-job-v2',run_id:'fixture-run-v2',status:'COMPLETED',stage:'DONE',
 policy_version:'evidence-auto-review:v2',initiated_by:'fixture-reviewer',reviewed_by:'fixture-service',
 model_calls:1,updated_at:'2026-09-14',items:[
 {record_id:mention.record_id,input_revision:1,record_kind:'ENTITY_MENTION',decision:'NEEDS_HUMAN',reason:'请核对接头所属项目。'},
 {record_id:fact.record_id,input_revision:1,record_kind:'ASSERTION',decision:'BLOCKED',reason:'等待所属实体身份确认。'}],
 counts:{entity_groups:1,approved_groups:0,approved_mentions:0,approved_assertions:0,manual_groups:1,manual_assertions:0,blocked_assertions:1,incomplete:0}};
const job={job_id:summary.job_id,version_id:'fixture-v1',document_id:'fixture-document',status:'COMPLETED',extraction_mode:'LLM',
 completed_chunks:1,expected_chunks:1,auto_review:summary,chunks:[{chunk_id:'fixture-chunk',status:'CANDIDATE',
 mention_record_ids:[mention.record_id],assertion_record_ids:[fact.record_id],finding_codes:[],validation_attempts:[]}]};
(async()=>{
 await fs.mkdir(output,{recursive:true});
 const report={status:'running',knowledge_writes:0,fixture_human_writes:0,fixture_resumes:0,screenshots:[],errors:[],blocked:[]};
 const browser=await chromium.launch({headless:true});
 const context=await browser.newContext({viewport:{width:1440,height:1000}});
 try {
  const bootstrap=await (await context.request.get(base+'/playground/bootstrap')).json();assert.equal(bootstrap.data_scope,'pump-only');
  await context.route('**/*',async route=>{
   const req=route.request(),url=new URL(req.url()),send=json=>route.fulfill({status:200,contentType:'application/json',json});
   if(url.origin===base && url.pathname==='/v1/knowledge/reviews:batch' && req.method()==='POST'){
    const body=req.postDataJSON();assert.equal(body.decisions.length,1);assert.equal(body.decisions[0].record_id,mention.record_id);
    assert.equal(body.decisions[0].identity_action,'INDEPENDENT');assert.equal(body.decisions[0].decision,'APPROVED');
    human=true;report.fixture_human_writes++;queue=[{...fact,revision:2,revision_id:'fixture-fact-r2'}];
    return send({outcomes:[{record_id:mention.record_id,record_kind:'ENTITY_MENTION',status:'APPROVED',revision:2,
      previous_revision_id:mention.revision_id,revision_id:'fixture-mention-r2'}]});
   }
   if(url.origin===base && url.pathname===`/v1/knowledge/construction-jobs/${job.job_id}/auto-review:run` && req.method()==='POST'){
    assert.ok(human);assert.deepEqual(req.postDataJSON(),{retry:true,resume_identity_record_ids:[mention.record_id]});
    resumes++;report.fixture_resumes++;queue=[];summary.items=[];
    summary.counts={...summary.counts,approved_assertions:1,manual_groups:0,blocked_assertions:0};return send(summary);
   }
   if(url.origin!==base || !(['GET','HEAD'].includes(req.method()) || req.method()==='POST' && reads.has(url.pathname))){
    report.blocked.push({method:req.method(),path:url.pathname});return route.abort();
   }
   if(url.pathname==='/v1/knowledge/construction-jobs')return send({items:[job]});
   if(url.pathname===`/v1/knowledge/construction-jobs/${job.job_id}`)return send(job);
   if(url.pathname===`/v1/knowledge/construction-jobs/${job.job_id}/auto-review`)return send(summary);
   if(url.pathname==='/v1/knowledge/review-queue')return send({items:queue});
   if(url.pathname==='/v1/knowledge/publication-candidates')return send({items:human?[{record:{...mention,revision:2,
    revision_id:'fixture-mention-r2',trust:{...trust,status:'APPROVED'}},requires_replacement:false}]:[]});
   if(url.pathname.startsWith('/v1/knowledge/entity-resolution/'))return send({record_id:mention.record_id,revision:1,
    identity_properties:[],dependent_facts:[fact],suggestions:[{outcome:'NO_MATCH',reason_code:'NO_MATCH',reason:'核对来源后可建档',target:null,evidence:[]}],
    identity_actions:{independent:{allowed:true,note:'请核对原文后确认。'}}});
   if(url.pathname.startsWith('/v1/knowledge/review-assessments/'))return send({record_id:fact.record_id,revision:human?2:1,
    status:human?'READY':'BLOCKED',summary:human?'所属实体已确认，可继续核对。':'等待所属实体确认。',dependencies:[],matches:[]});
   if(url.pathname.startsWith('/v1/knowledge/property-assignment/'))return send({record_id:fact.record_id,revision:human?2:1,
    current_subject:entity,current_subject_mention_record_id:mention.record_id,targets:[]});
   if(url.pathname==='/v1/knowledge/review-evidence')return send({document_title:'合成接头来源',version_id:'fixture-v1',chunk_id:'fixture-chunk',
    char_start:0,char_end:quote.length,context_start:0,context_end:quote.length,text:quote,source_uri:'urn:fixture:review-v2',
    view:'paragraph',document_accessible:false,has_previous:false,has_next:false});
   return route.continue();
  });
  const page=await context.newPage();page.on('pageerror',e=>report.errors.push(e.message));
  page.on('dialog',dialog=>dialog.accept(dialog.type()==='prompt'?'已核对合成来源中的项目和接头标识。':undefined));
  await page.goto(base+'/industrial');
  const workspace=bootstrap.knowledge_bases.find(k=>k.name==='母线槽知识库') || bootstrap.knowledge_bases[0];
  await page.locator('#knowledge-base').selectOption(workspace.knowledge_base_id);
  await page.locator('[data-panel="build"]').click();await page.locator('#build-tab-instances').click();
  await page.locator('#construction-jobs-refresh-button').click();await page.locator('[data-construction-job-detail]').first().click();
  await page.locator('[data-step-target="step-review"]').click();
  const row=page.locator(`[data-review-record="${mention.record_id}"]`);
  await row.getByText('模型存疑，待人工复核：',{exact:true}).waitFor();
  for(const width of [1280,1440,1920]){
   await page.setViewportSize({width,height:width===1280?800:1080});
   await row.scrollIntoViewIfNeeded();
   let name=`identity-${width}.png`;await page.screenshot({path:path.join(output,name)});report.screenshots.push(name);
   await row.locator('details.review-evidence > summary').click();await row.getByText('合成接头来源',{exact:true}).waitFor();
   name=`evidence-${width}.png`;await page.screenshot({path:path.join(output,name)});report.screenshots.push(name);
   await row.locator('details.review-evidence > summary').click();
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  }
  await page.evaluate(()=>document.documentElement.requestFullscreen());
  await page.screenshot({path:path.join(output,'fullscreen.png')});report.screenshots.push('fullscreen.png');
  assert.ok(await page.evaluate(()=>Boolean(document.fullscreenElement)));await page.evaluate(()=>document.exitFullscreen());
  const resumed=page.waitForResponse(r=>r.url().endsWith('/auto-review:run'));
  await row.locator('[data-review-action="APPROVED"]').click();await resumed;
  await page.locator('#review-list [data-review-record]').waitFor({state:'detached'});
  assert.equal(resumes,1);assert.equal(report.fixture_human_writes,1);
  await page.locator('#review-list').scrollIntoViewIfNeeded();
  await page.screenshot({path:path.join(output,'resumed.png')});report.screenshots.push('resumed.png');
  assert.deepEqual(report.blocked,[]);assert.deepEqual(report.errors,[]);report.status='passed';
 } catch(error){report.status='failed';report.error=error.message;throw error;}
 finally{await fs.writeFile(path.join(output,'report.json'),JSON.stringify(report,null,2));await browser.close();console.log(JSON.stringify(report));}
})().catch(error=>{console.error(error.message);process.exitCode=1;});
