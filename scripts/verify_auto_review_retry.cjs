/** Retry only the user-authorized existing review, then inspect the real result. */
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
const {chromium}=require('playwright');
const job=process.env.AUTO_REVIEW_JOB_ID;assert.ok(job);
const inspect=process.env.AUTO_REVIEW_QA_MODE==='inspect';
const base='http://127.0.0.1:8002', output=path.resolve('.local/browser-qa/'+(inspect?'auto-review-final':'auto-review-retry'));
const retryPath=`/v1/knowledge/construction-jobs/${encodeURIComponent(job)}/auto-review:run`;
const allowed=new Set(['/playground/session','/v1/knowledge/graph:query','/v1/knowledge/graph:evidence',
  '/v1/knowledge/sources:query','/v1/knowledge/sources:read','/v1/knowledge/publications:compare',
  '/v1/knowledge/quality/reviews:query','/v1/knowledge/review-evidence',...(inspect?[]:[retryPath])]);
(async()=>{
 await fs.mkdir(output,{recursive:true});
 const report={status:'running',mode:inspect?'read-only':'retry',job_id:job,started_at:new Date().toISOString(),errors:[],blocked:[],screenshots:[]};
 const browser=await chromium.launch({headless:true});let page,timer;
 try {
  const context=await browser.newContext({viewport:{width:1440,height:1000}});
  const boot=await(await context.request.get(base+'/playground/bootstrap')).json();assert.equal(boot.data_scope,'pump-only');
  const workspace=boot.knowledge_bases.find(x=>x.name==='母线槽知识库');assert.ok(workspace);
  await context.route('**/*',async route=>{
   const r=route.request(),u=new URL(r.url());
   if(u.origin!==base||!(['GET','HEAD'].includes(r.method())||r.method()==='POST'&&allowed.has(u.pathname))){
    report.blocked.push({method:r.method(),path:u.pathname});return route.abort();
   }
   return route.continue();
  });
  page=await context.newPage();page.setDefaultTimeout(60000);page.on('pageerror',e=>report.errors.push(e.message));
  await page.goto(base+'/industrial');await page.locator('#knowledge-base').selectOption(workspace.knowledge_base_id);
  await page.locator('[data-panel="build"]').click();await page.locator('#build-tab-instances').click();
  await page.locator('#construction-jobs-refresh-button').click();
  const card=page.locator('#construction-job-list .governance-item').filter({hasText:job.slice(0,8)});
  const loaded=page.waitForResponse(r=>new URL(r.url()).pathname===`/v1/knowledge/construction-jobs/${job}`);
  await card.locator('[data-construction-job-detail]').click();const detail=await loaded;assert.ok(detail.ok(),await detail.text());
  const initialDetail=await detail.json();let body=initialDetail.auto_review;
  if(inspect)await fs.writeFile(path.resolve('.local/browser-qa/auto-review-upload/construction-response.json'),JSON.stringify(initialDetail,null,2));
  if(!inspect){
  const completion=page.waitForResponse(r=>new URL(r.url()).pathname===retryPath,{timeout:1500000});completion.catch(()=>{});
  await page.locator(`#construction-output [data-auto-review-retry="${job}"]`).click();
  timer=setInterval(async()=>{
   try{const progress=await page.locator('#construction-progress').innerText();console.log(JSON.stringify({at:new Date().toISOString(),progress}));
    await fs.writeFile(path.join(output,'progress.json'),JSON.stringify({at:new Date().toISOString(),progress}));}catch{}
  },15000);
  const result=await completion;body=await result.json();
  assert.ok(result.ok(),JSON.stringify(body));
  }
  await fs.writeFile(path.join(output,'review-response.json'),JSON.stringify(body,null,2));report.result=body;
  assert.equal(body.status,'COMPLETED',JSON.stringify({status:body.status,counts:body.counts}));assert.equal(body.counts.incomplete,0);
  assert.ok(body.counts.approved_mentions>19);assert.ok(body.counts.approved_assertions>57);
  await page.locator('#construction-progress').waitFor({state:'hidden'});
  const receipt=page.locator('#construction-output');
  await receipt.locator('[data-auto-review-status="COMPLETED"]').waitFor();
  assert.equal(body.issues.length,1);assert.equal(body.issues[0].property_name,'project_id');assert.equal(body.issues[0].entity_count,24);
  for(const width of [1280,1440,1920]){
   await page.setViewportSize({width,height:width===1280?800:1080});
   await receipt.locator('[data-auto-review-gaps]').scrollIntoViewIfNeeded();
   const name=`receipt-${width}.png`;await page.screenshot({path:path.join(output,name)});report.screenshots.push(name);
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   const gap=receipt.locator('.auto-review-gap-details');
   if(await gap.getAttribute('open')===null)await gap.locator(':scope > summary').click();
   await gap.locator('[data-auto-review-evidence]').first().click();
   const sourceResponse=page.waitForResponse(r=>new URL(r.url()).pathname==='/v1/knowledge/review-evidence');
   await gap.getByText('查看文档原文',{exact:true}).first().click();
   const sourceResult=await sourceResponse;assert.ok(sourceResult.ok());
   const source=await sourceResult.json(),characters=Array.from(source.text);
   assert.equal(characters.length,source.context_end-source.context_start,'Context text must preserve offsets, including whitespace.');
   assert.equal(characters.slice(source.char_start-source.context_start,source.char_end-source.context_start).join(''),source.quoted_text);
   await gap.locator('[data-evidence-context] strong').first().waitFor();
   assert.ok((await gap.locator('[data-evidence-context] blockquote').first().innerText()).length>0);
   await gap.locator('[data-auto-review-item]').first().scrollIntoViewIfNeeded();
   const evidenceName=`source-${width}.png`;await page.screenshot({path:path.join(output,evidenceName)});report.screenshots.push(evidenceName);
   await gap.locator(':scope > summary').click();
   await page.locator('[data-step-target="step-review"]').click();await page.locator('#review-refresh-button').click();
   await page.getByText('当前没有待审核记录，仍有必填字段缺口。',{exact:true}).waitFor();
   assert.equal(await page.locator('#review-list [data-review-record]').count(),0);
   assert.equal(await page.locator('#review-list [data-review-next]').count(),0);
   await page.locator('#review-list').scrollIntoViewIfNeeded();
   const queueName=`manual-queue-${width}.png`;await page.screenshot({path:path.join(output,queueName)});report.screenshots.push(queueName);
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   await page.locator('[data-step-target="source-upload-slot"]').click();
  }
  const refreshed=page.waitForResponse(r=>new URL(r.url()).pathname===`/v1/knowledge/construction-jobs/${job}`);
  await card.locator('[data-construction-job-detail]').click();
  const current=await refreshed;assert.ok(current.ok());
  await fs.writeFile(path.resolve('.local/browser-qa/auto-review-upload/construction-response.json'),JSON.stringify(await current.json(),null,2));
  assert.deepEqual(report.errors,[]);assert.deepEqual(report.blocked,[]);report.status='passed';
 }catch(e){report.status='failed';report.error=e.message;if(page)await page.screenshot({path:path.join(output,'failure.png')}).catch(()=>{});throw e;}
 finally{clearInterval(timer);report.finished_at=new Date().toISOString();await fs.writeFile(path.join(output,'report.json'),JSON.stringify(report,null,2));await browser.close();console.log(JSON.stringify({...report,result:report.result?{status:report.result.status,counts:report.result.counts,model_calls:report.result.model_calls}:undefined}));}
})().catch(e=>{console.error(e.message);process.exitCode=1;});
