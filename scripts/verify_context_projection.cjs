/** Existing-job context repair acceptance. Default is read-only; repair permits one exact POST. */
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
const {chromium}=require('playwright');
const job=process.env.CONTEXT_JOB_ID || process.env.AUTO_REVIEW_JOB_ID;
assert.ok(job,'Set CONTEXT_JOB_ID to the authorized existing construction job.');
const mode=process.env.CONTEXT_QA_MODE || 'inspect';
assert.ok(['repair','inspect'].includes(mode),'CONTEXT_QA_MODE must be repair or inspect.');
const recordId=process.env.CONTEXT_ASSERTION_RECORD_ID || '';
const previewRevisions=JSON.parse(process.env.CONTEXT_PREVIEW_REVISION_IDS || '[]');
assert.ok(Array.isArray(previewRevisions) && previewRevisions.every(value=>typeof value==='string' && value.length>0));
if(previewRevisions.length)assert.equal(previewRevisions.length,7,'Provide the selected Project mention and its six literal facts.');
const base='http://127.0.0.1:8002';
const output=path.resolve(process.env.BROWSER_QA_OUTPUT || `.local/browser-qa/context-projection-${mode}`);
const jobPath=`/v1/knowledge/construction-jobs/${encodeURIComponent(job)}`;
const repairPath=`${jobPath}/auto-review:run`;
const readPosts=new Set(['/playground/session','/v1/knowledge/graph:query','/v1/knowledge/graph:evidence',
  '/v1/knowledge/sources:query','/v1/knowledge/sources:read','/v1/knowledge/publications:compare',
  '/v1/knowledge/quality/reviews:query','/v1/knowledge/review-evidence','/v1/knowledge/publications:preview']);
const expected={groups:139,mentions:310,assertions:Number(process.env.CONTEXT_EXPECT_ASSERTIONS || 922),
  added:Number(process.env.CONTEXT_EXPECT_ADDED || 24)};
// Playwright request diagnostics include request headers; never persist them.
const safeError=error=>String(error?.message || 'Browser verification failed').split(/\r?\n/)[0]
  .replace(/Bearer\s+\S+/gi,'Bearer [redacted]').slice(0,500);
function validateEvidence(value,evidence){
  const characters=Array.from(value.text);
  assert.ok(characters.length>0,'Evidence must contain readable original text.');
  assert.equal(characters.length,value.context_end-value.context_start,'Original offsets must preserve whitespace.');
  const quote=characters.slice(value.char_start-value.context_start,value.char_end-value.context_start).join('');
  assert.equal(quote,value.quoted_text);
  assert.equal(quote,evidence.quoted_text);
  assert.equal(value.chunk_id,evidence.chunk_id);
}
(async()=>{
  await fs.mkdir(output,{recursive:true});
  const report={status:'running',mode,job_id:job,started_at:new Date().toISOString(),repair_posts:0,
    screenshots:[],errors:[],blocked:[],evidence_api_verified:false,evidence_ui_verified:false};
  const browser=await chromium.launch({headless:true});let page,timer,latestJobResponse;
  try {
    const context=await browser.newContext({viewport:{width:1440,height:1000}});
    const boot=await(await context.request.get(base+'/playground/bootstrap')).json();
    assert.equal(boot.data_scope,'pump-only');
    const workspace=boot.knowledge_bases.find(value=>value.name==='母线槽知识库');assert.ok(workspace);
    await context.route('**/*',async route=>{
      const request=route.request(),url=new URL(request.url());
      if(url.origin===base && request.method()==='POST' && url.pathname===repairPath && mode==='repair'){
        report.repair_posts++;
        if(report.repair_posts!==1){report.blocked.push({method:'POST',path:url.pathname,reason:'second repair prohibited'});return route.abort();}
        assert.deepEqual(request.postDataJSON(),{retry:true});
        return route.continue();
      }
      if(url.origin!==base || !(['GET','HEAD'].includes(request.method()) || request.method()==='POST' && readPosts.has(url.pathname))){
        report.blocked.push({method:request.method(),path:url.pathname});return route.abort();
      }
      return route.continue();
    });
    page=await context.newPage();page.setDefaultTimeout(60000);
    page.on('pageerror',error=>report.errors.push(error.message));
    page.on('response',response=>{if(new URL(response.url()).pathname===jobPath)latestJobResponse=response;});
    await page.goto(base+'/industrial');
    await page.locator('#knowledge-base').selectOption(workspace.knowledge_base_id);
    await page.locator('[data-panel="build"]').click();await page.locator('#build-tab-instances').click();
    await page.locator('#construction-jobs-refresh-button').click();
    const card=page.locator('#construction-job-list .governance-item').filter({hasText:job.slice(0,8)});
    await card.locator('[data-construction-job-detail]').click();
    const receipt=page.locator('#construction-output');
    await receipt.locator(`[data-auto-review-job="${job}"]`).waitFor();
    // An automatic background read and this click can share an already-started GET.
    const initial=latestJobResponse;assert.ok(initial);assert.ok(initial.ok(),await initial.text());
    const initialBody=await initial.json();
    // Reuse only this authenticated UI request's Authorization header; never log or persist it.
    const auth=(await initial.request().allHeaders()).authorization;assert.ok(auth);
    const headers={Authorization:auth};
    await fs.writeFile(path.join(output,'initial-construction-response.json'),JSON.stringify(initialBody,null,2));
    if(mode==='repair'){
      const completion=page.waitForResponse(response=>new URL(response.url()).pathname===repairPath,{timeout:1500000});
      completion.catch(()=>{});
      await receipt.getByRole('button',{name:'补全上下文并审核',exact:true}).click();
      timer=setInterval(async()=>{
        try {
          const progress=await page.locator('#construction-progress').evaluate(element=>({hidden:element.hidden,text:element.hidden?'':element.textContent}));
          console.log(JSON.stringify({at:new Date().toISOString(),progress}));
        }catch{}
      },15000);
      const response=await completion,body=await response.json();
      await fs.writeFile(path.join(output,'repair-response.json'),JSON.stringify(body,null,2));
      assert.ok(response.ok(),JSON.stringify({status:body.status,detail:body.detail}));
      await page.locator('#construction-progress').waitFor({state:'hidden'});
      await receipt.locator('[data-auto-review-status="COMPLETED"]').waitFor();
      await receipt.locator('[data-context-mapping-status="COMPLETED"]').waitFor();
      assert.equal(await receipt.locator('[data-auto-review-gaps]').count(),0,'Same-job repair must remove stale gaps.');
      assert.match(await page.locator('#construction-next-button').innerText(),/查看已确认内容/);
    }
    clearInterval(timer);
    // Read the latest detail after the mutation, preserving its original mapping and updated review receipt.
    const current=await context.request.get(base+jobPath,{headers});assert.ok(current.ok(),await current.text());
    const detail=await current.json(),body=detail.auto_review;
    await fs.writeFile(path.join(output,'construction-response.json'),JSON.stringify(detail,null,2));
    const preserved=path.resolve('.local/browser-qa/auto-review-upload');await fs.mkdir(preserved,{recursive:true});
    await fs.writeFile(path.join(preserved,'construction-response.json'),JSON.stringify(detail,null,2));
    report.result={status:body.status,counts:body.counts,context_mapping:body.context_mapping,issues:body.issues};
    assert.equal(body.status,'COMPLETED');assert.equal(body.counts.incomplete,0);
    assert.equal(body.counts.approved_groups,expected.groups);assert.equal(body.counts.approved_mentions,expected.mentions);
    assert.equal(body.counts.approved_assertions,expected.assertions);
    for(const name of ['manual_groups','manual_assertions','blocked_assertions'])assert.equal(body.counts[name],0,name);
    assert.deepEqual(body.issues,[]);assert.equal(body.context_mapping.status,'COMPLETED');
    assert.equal(body.context_mapping.added_assertions,expected.added);assert.deepEqual(body.context_mapping.issues,[]);
    if(mode==='repair')assert.deepEqual(detail.mapping_summary,initialBody.mapping_summary,'Repair must not rewrite the original extraction mapping.');
    if(previewRevisions.length){
      const publications=await context.request.get(base+'/v1/knowledge/publications?limit=100',{headers});
      assert.ok(publications.ok());const active=(await publications.json()).items.find(value=>value.status==='ACTIVE');
      const response=await context.request.post(base+'/v1/knowledge/publications:preview',{headers,data:{
        approved_revision_ids:previewRevisions,expected_active_publication_id:active?.publication_id || null,
        remove_record_ids:[],replace_record_ids:[]}});
      const preview=await response.json();await fs.writeFile(path.join(output,'project-publication-preview.json'),JSON.stringify(preview,null,2));
      assert.ok(response.ok(),JSON.stringify(preview));
      assert.ok(preview.preview_hash,'Successful read-only preview must return its governed manifest hash.');
      report.publication_preview={status:'passed',selected_revisions:previewRevisions.length,summary:preview.instances_after?.summary};
    }
    let record;
    if(recordId){
      const response=await context.request.get(base+`/v1/knowledge/records/${encodeURIComponent(recordId)}/revisions?limit=1`,{headers});
      assert.ok(response.ok(),await response.text());record=(await response.json()).items[0];
      assert.equal(record.record_kind,'ASSERTION');assert.equal(record.predicate,'project_id');
      assert.equal(record.trust.status,'APPROVED');assert.ok(record.context_property_evidence?.value_evidence);
      assert.ok(!Object.hasOwn(record.context_property_evidence,'binding_json'));
      await fs.writeFile(path.join(output,'context-record.json'),JSON.stringify(record,null,2));
      const evidenceResponses={};
      for(const [role,evidence] of [['PRIMARY',record.evidence],['CONTEXT_VALUE',record.context_property_evidence.value_evidence]]){
        const result=await context.request.post(base+'/v1/knowledge/review-evidence',{headers,data:{record_id:recordId,
          expected_revision:record.revision,evidence_role:role,view:'paragraph',offset:0}});
        assert.ok(result.ok(),await result.text());const value=await result.json();validateEvidence(value,evidence);
        evidenceResponses[role]=value;
      }
      assert.notEqual(evidenceResponses.PRIMARY.chunk_id,evidenceResponses.CONTEXT_VALUE.chunk_id);
      await fs.writeFile(path.join(output,'context-evidence.json'),JSON.stringify(evidenceResponses,null,2));
      report.evidence_api_verified=true;
    }
    for(const width of [1280,1440,1920]){
      await page.setViewportSize({width,height:width===1280?800:1080});
      await receipt.locator('[data-context-mapping-status="COMPLETED"]').scrollIntoViewIfNeeded();
      assert.equal(await receipt.locator('[data-auto-review-gaps]').count(),0);
      const mapping=receipt.locator('.context-mapping-summary > details');
      if(await mapping.getAttribute('open')===null)await mapping.locator(':scope > summary').click();
      assert.match(await mapping.innerText(),/作用域：文档根节点/);
      await mapping.locator('article').first().evaluate(element=>element.scrollIntoView({block:'center',inline:'nearest'}));
      let name=`receipt-${width}.png`;await page.screenshot({path:path.join(output,name)});report.screenshots.push(name);
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      if(record){
        const details=receipt.locator('.auto-review-details');
        if(await details.getAttribute('open')===null)await details.locator(':scope > summary').click();
        const target=details.locator(`[data-auto-review-evidence="${recordId}"]`);
        if(await target.count()){
          await target.click();const proof=target.locator('xpath=ancestor::article').locator('.context-property-evidence');
          for(const [label,role,evidence] of [['实体原文','PRIMARY',record.evidence],['上下文字段原文','CONTEXT_VALUE',record.context_property_evidence.value_evidence]]){
            const response=page.waitForResponse(value=>new URL(value.url()).pathname==='/v1/knowledge/review-evidence' &&
              (value.request().postDataJSON()?.evidence_role || 'PRIMARY')===role);
            await proof.getByText(label,{exact:true}).click();
            const result=await response;assert.ok(result.ok());validateEvidence(await result.json(),evidence);
            if(role==='PRIMARY'){
              await proof.locator('mark').first().waitFor();
              await proof.locator('mark').first().evaluate(element=>element.scrollIntoView({block:'start',inline:'nearest'}));
              name=`entity-evidence-${width}.png`;await page.screenshot({path:path.join(output,name)});report.screenshots.push(name);
            }
          }
          await proof.locator('mark').nth(1).waitFor();await proof.scrollIntoViewIfNeeded();
          name=`dual-evidence-${width}.png`;await page.screenshot({path:path.join(output,name)});report.screenshots.push(name);
          report.evidence_ui_verified=true;
        }else report.evidence_ui_limitation='Requested record is outside the bounded auto-review receipt and publication candidate list; real dual-role API verified separately.';
      }
      await page.locator('[data-step-target="step-review"]').click();
      const refreshedQueue=page.waitForResponse(response=>new URL(response.url()).pathname==='/v1/knowledge/review-queue');
      await page.locator('#review-refresh-button').click();
      const queue=await refreshedQueue;assert.ok(queue.ok());assert.deepEqual((await queue.json()).items,[]);
      await page.getByText('当前批次已完成复核。',{exact:true}).waitFor();
      assert.equal(await page.locator('#review-list [data-review-record]').count(),0);
      assert.equal(await page.locator('#review-auto-summary [data-auto-review-gaps]').count(),0);
      await page.locator('#review-list').scrollIntoViewIfNeeded();
      name=`manual-queue-${width}.png`;await page.screenshot({path:path.join(output,name)});report.screenshots.push(name);
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      await page.locator('[data-step-target="source-upload-slot"]').click();
    }
    assert.equal(report.repair_posts,mode==='repair'?1:0);
    assert.deepEqual(report.errors,[]);assert.deepEqual(report.blocked,[]);report.status='passed';
  }catch(error){report.status='failed';report.error=safeError(error);if(page)await page.screenshot({path:path.join(output,'failure.png')}).catch(()=>{});throw error;}
  finally{clearInterval(timer);report.finished_at=new Date().toISOString();await fs.writeFile(path.join(output,'report.json'),JSON.stringify(report,null,2));await browser.close();console.log(JSON.stringify(report));}
})().catch(error=>{console.error(safeError(error));process.exitCode=1;});
