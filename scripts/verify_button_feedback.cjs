/** Pump-only loading QA: real read APIs, isolated review/preview/write responses. */
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');const path=require('node:path');
const {chromium}=require('playwright');
const base=process.env.BROWSER_QA_URL||'http://127.0.0.1:8002',origin=new URL(base).origin;
const output=path.resolve(process.env.BROWSER_QA_OUTPUT||'.local/browser-qa/button-feedback');
const report={status:'running',checks:[],screenshots:[],pageErrors:[],blocked:[],knowledgeWrites:0,simulatedWrites:0};
const gate=()=>{let release;const promise=new Promise(r=>release=r);return {promise,release};};
const readPosts=new Set(['/playground/session','/v1/knowledge/graph:query','/v1/knowledge/graph:evidence','/v1/knowledge/sources:query','/v1/knowledge/sources:read','/v1/knowledge/review-evidence','/v1/knowledge/quality/reviews:query','/v1/knowledge/publications:compare']);
const readGets=/^\/v1\/(?:ontologies|knowledge\/(?:construction-jobs|review-queue|publication-candidates|publications|entity-resolution|review-assessments|records|property-assignment|publication-inventory|documents|quality))(?:\/|$)/;
(async()=>{
 assert.ok(['127.0.0.1','localhost'].includes(new URL(base).hostname));await fs.mkdir(output,{recursive:true});
 const browser=await chromium.launch({headless:true});const context=await browser.newContext({viewport:{width:1440,height:1000},deviceScaleFactor:1});
 let page,hold=null;const allGates=[];
 try {
 const bootstrap=await (await context.request.get(base+'/playground/bootstrap')).json();assert.equal(bootstrap.data_scope,'pump-only');
 const admin=bootstrap.personas.find(p=>p.groups.includes('administrators'));
 const session=await (await context.request.post(base+'/playground/session',{data:{persona_id:admin.id}})).json();
 const headers={Authorization:'Bearer '+session.access_token};
 const candidates=await (await context.request.get(base+'/v1/knowledge/publication-candidates?limit=100',{headers})).json();
 const queue=await (await context.request.get(base+'/v1/knowledge/review-queue?status=CANDIDATE&status=QUARANTINED&limit=100',{headers})).json();
 const available=[...queue.items,...candidates.items.map(i=>i.record)];
 if(!available.some(r=>r.record_kind==='ENTITY_MENTION') || !available.some(r=>r.record_kind==='ASSERTION'&&!r.object_entity)){
   const inventory=await (await context.request.get(base+'/v1/knowledge/publication-inventory?limit=100',{headers})).json();
   for(const item of inventory.items.slice(0,30)){
     const revisions=await (await context.request.get(base+'/v1/knowledge/records/'+encodeURIComponent(item.record_id)+'/revisions?limit=100',{headers})).json();
     available.push(...revisions.items);
   }
 }
 const mention=available.find(r=>r.record_kind==='ENTITY_MENTION'&&r.entity?.entity_type==='Equipment');
 const fact=available.find(r=>r.record_kind==='ASSERTION'&&!r.object_entity);assert.ok(mention&&fact,'pump records required for isolated UI fixture');
 const records=[mention,fact].map((r,i)=>({...structuredClone(r),record_id:'qa-button-'+i,revision_id:'qa-button-'+i+'-1',revision:1,trust:{...r.trust,status:'CANDIDATE'}}));
 const startHold=pathname=>{const reached=gate(),done=gate();allGates.push(done);hold={pathname,reached,done};return hold;};
 const shot=async name=>{const file=name+'.png';await page.screenshot({path:path.join(output,file)});report.screenshots.push(file);};
 const noDuplicates=async()=>{
   assert.equal(await page.locator('.operation-feedback:visible,.operation-inline:visible,[data-review-operation]:visible,.loading-orbit:visible').count(),0);
   const pendingText=await page.locator('body').innerText();
   assert.doesNotMatch(pendingText,/正在(?:读取|加载|处理|构建|检查|校验|核对|保存|比较|检索|更新|验证)|等待身份匹配完成/);
 };
 const check=async(name,pathname,click,buttonSelector,options={})=>{
   report.stage=name;const task=startHold(pathname);await click();await Promise.race([task.reached.promise,new Promise((_,reject)=>{const timer=setTimeout(()=>reject(Error(name+' request not reached')),15000);timer.unref();})]);
   const button=page.locator(buttonSelector).first();await button.waitFor({state:'visible'});
   await button.scrollIntoViewIfNeeded();
   assert.equal(await button.getAttribute('aria-busy'),'true',name+' busy button');
   assert.equal(await button.isDisabled(),true,name+' disabled');await noDuplicates();
   assert.equal(await page.locator('button.button-pending:visible,button.busy:visible').count(),1,name+' one indicator');
   if(options.sizes){for(const width of [1280,1440,1920]){await page.setViewportSize({width,height:width===1280?800:1000});await button.scrollIntoViewIfNeeded();assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await shot(name+'-'+width);}}
   else await shot(name);
   if(options.fullscreen){await page.evaluate(()=>document.documentElement.requestFullscreen());await shot(name+'-fullscreen');await page.evaluate(()=>document.exitFullscreen());}
   hold=null;task.done.release();await page.waitForFunction(()=>![...document.querySelectorAll('button.button-pending,button.busy')].some(b=>b.getClientRects().length));
   report.checks.push(name);
 };
   await context.route('**/*',async route=>{
     const req=route.request(),url=new URL(req.url());let body=null,status=200;
     if(url.origin!==origin){report.blocked.push(url.pathname);return route.abort();}
     if(url.pathname==='/v1/knowledge/review-queue')body={items:records};
     else if(url.pathname==='/v1/knowledge/publication-candidates')body={items:records.map(record=>({record:{...record,trust:{...record.trust,status:'APPROVED'}},requires_replacement:false}))};
     else if(/^\/v1\/knowledge\/entity-resolution\/qa-button-/.test(url.pathname))body={record_id:records[0].record_id,revision:1,identity_properties:[],dependent_facts:[],review_targets:[],suggestions:[{outcome:'NO_MATCH',reason:'循环水泵浏览器夹具',target:null,evidence:[]}]};
     else if(/^\/v1\/knowledge\/review-assessments\/qa-button-/.test(url.pathname))body={record_id:records[1].record_id,revision:1,status:'READY',summary:'可以审核',dependencies:[],matches:[]};
     else if(/^\/v1\/knowledge\/property-assignment\/qa-button-/.test(url.pathname))body={record_id:records[1].record_id,revision:1,current:{entity:records[1].subject,identity_properties:[]},items:[{entity:records[1].subject,record_id:records[0].record_id,revision:1,selectable:true,identity_properties:[]}],truncated:false};
     else if(url.pathname==='/v1/knowledge:preflight')body={exact_matches:[],similar_matches:[],truncated:false,review_token:'a'.repeat(64)};
     else if(url.pathname==='/v1/knowledge/publications:preview')body={preview_hash:'pump-browser-only',entity_changes:[],property_changes:[],relationship_changes:[],instances_after:{summary:{entity_count:0,property_count:0,relationship_count:0}}};
     else if(req.method()==='POST'&&['/v1/knowledge/reviews:batch','/v1/knowledge/publications:publish','/v1/knowledge/quality/runs','/v1/ontologies','/v1/knowledge:construct'].includes(url.pathname)){
       report.simulatedWrites++;status=503;body={error:{code:'dependency_unavailable',message:'隔离夹具：服务暂不可用'}};
     }
     const allowed=['GET','HEAD'].includes(req.method())&&(url.pathname==='/industrial'||url.pathname.startsWith('/industrial/assets/')||url.pathname==='/playground/bootstrap'||url.pathname==='/favicon.ico'||/^\/playground\/(?:assets\/)?industrial-demo-v1\//.test(url.pathname)||readGets.test(url.pathname))||req.method()==='POST'&&readPosts.has(url.pathname);
     if(!body&&!allowed){report.blocked.push(req.method()+' '+url.pathname);return route.abort('blockedbyclient');}
     const selected=hold?.pathname===url.pathname?hold:null;
     const response=body?null:await route.fetch();
     if(selected){selected.reached.release();await selected.done.promise;}
     if(body)return route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
     return route.fulfill({response});
   });
   page=await context.newPage();page.on('pageerror',e=>report.pageErrors.push(e.message));
   const initial=page.waitForResponse(r=>new URL(r.url()).pathname==='/v1/knowledge/graph:query');await page.goto(base+'/industrial');assert.ok((await initial).ok());
   await page.waitForFunction(()=>!document.querySelector('#reload-graph').hasAttribute('aria-busy'));
   await check('graph-refresh','/v1/knowledge/graph:query',()=>page.locator('#reload-graph').click(),'#reload-graph');
   await page.locator('[data-panel="build"]').click();await page.locator('[data-step-target="step-publication"]').click();await page.locator('#step-publication [data-flow-go="browse"]').click();await page.locator('#kb-list [data-id]').first().waitFor();
   await check('knowledge-refresh','/v1/knowledge/graph:query',()=>page.locator('#kb-refresh').click(),'#kb-refresh',{sizes:true,fullscreen:true});
   await page.locator('#kb-list [data-id]').first().click();
   await check('knowledge-evidence','/v1/knowledge/graph:evidence',()=>page.locator('#kb-dossier [data-evidence]').first().click(),'#kb-dossier [data-evidence]');
   await page.locator('dialog[aria-label="来源依据"] [data-close]').click();
   await page.locator('[data-panel="sources"]').click();await page.locator('.source-card').first().waitFor();
   await check('source-list','/v1/knowledge/sources:query',()=>page.locator('#refresh-sources').click(),'#refresh-sources');
   await check('source-read','/v1/knowledge/sources:read',()=>page.locator('.source-card').first().getByRole('button',{name:'查看原文 →'}).click(),'.source-card button:has-text("查看原文 →")');
   await page.locator('dialog[open]').getByRole('button',{name:'关闭',exact:true}).click();
   await page.locator('[data-panel="build"]').click();
   await page.locator('[data-step-target="source-upload-slot"]').click();
   await page.locator('#document-file').setInputFiles('src/graphrag_prod/playground/static/industrial-demo-v1/maintenance_report.txt');
   await check('upload-build','/v1/knowledge:construct',()=>page.locator('#construct-button').click(),'#construct-button',{sizes:true});
   await page.locator('[data-step-target="step-review"]').click();
   await page.locator('[data-review-record="qa-button-0"]').waitFor();
   await check('review-refresh','/v1/knowledge/review-queue',()=>page.locator('#review-refresh-button').click(),'#review-refresh-button');
   await page.waitForFunction(()=>!document.querySelector('[data-resolution-load].button-pending'));
   await page.locator('[data-resolution-details="0"]').evaluate(el=>el.open=true);
   await check('entity-matching','/v1/knowledge/entity-resolution/qa-button-0',()=>page.locator('[data-resolution-load="0"]').click(),'[data-resolution-load="0"]');
   await check('review-save','/v1/knowledge/reviews:batch',()=>page.locator('[data-review-record="qa-button-0"] [data-review-action="REJECTED"]').click(),'[data-review-record="qa-button-0"] [data-review-action="REJECTED"]',{sizes:true});
   await page.locator('[data-review-phase="facts"]').click();
   await check('fact-check','/v1/knowledge/review-assessments/qa-button-1',()=>page.locator('[data-review-assess="1"]').click(),'[data-review-assess="1"]');
   await check('property-targets','/v1/knowledge/property-assignment/qa-button-1',()=>page.locator('[data-assignment-toggle="1"]').click(),'[data-assignment-search="1"]');
   await page.locator('[data-assignment-toggle="1"]').click();
   await page.locator('[data-step-target="step-publication"]').click();await page.locator('[data-publication-group]').first().waitFor();
   for(const b of await page.locator('[data-publication-group]').all())await b.check();
   await check('publication-preview','/v1/knowledge/publications:preview',()=>page.locator('#publication-preview-button').click(),'#publication-preview-button');
   await check('publication-submit','/v1/knowledge/publications:publish',()=>page.locator('#publication-button').click(),'#publication-button');
   await page.locator('[data-panel="maintenance"]').click();await page.locator('#quality-refresh-button').waitFor();
   await page.waitForFunction(()=>!document.querySelector('#quality-refresh-button').hasAttribute('aria-busy'));
   await check('quality-check','/v1/knowledge/quality',()=>page.locator('#quality-refresh-button').click(),'#quality-refresh-button');
   await page.locator('[data-flow-target="maintenance-history"]').click();
   await page.locator('[data-rollback-target]').waitFor();
   await page.locator('[data-rollback-target]').selectOption({index:1});
   await check('publication-compare','/v1/knowledge/publications:compare',()=>page.locator('[data-open-rollback]').click(),'[data-open-rollback]');
   await page.locator('dialog[aria-label="知识维护"] [data-close]').click();
   assert.deepEqual(report.pageErrors,[]);assert.deepEqual(report.blocked,[]);report.status='passed';
 }catch(e){report.status='failed';report.error=e.message;if(page)await page.screenshot({path:path.join(output,'failure.png')});throw e;}
 finally{for(const g of allGates)g.release();await context.unrouteAll({behavior:'ignoreErrors'});await browser.close();await fs.writeFile(path.join(output,'results.json'),JSON.stringify(report,null,2));console.log(JSON.stringify({status:report.status,stage:report.stage,output,error:report.error}));}
})().catch(e=>{console.error(e);process.exitCode=1;});
