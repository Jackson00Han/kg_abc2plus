/** Pump-only UI regression. Real reads/preview; construction is fulfilled locally. */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {chromium} = require('playwright');
const base = process.env.BROWSER_QA_URL || 'http://127.0.0.1:8002';
const origin = new URL(base).origin;
const output = path.resolve(process.env.BROWSER_QA_OUTPUT || '.local/browser-qa/upload-state');
const report = {status:'running', screenshots:[], pageErrors:[], blocked:[], knowledgeWrites:0, simulatedConstructs:0};
const readPosts = new Set(['/playground/session', '/v1/knowledge/publications:preview', '/v1/knowledge/graph:query', '/v1/knowledge/graph:evidence', '/v1/knowledge/sources:query', '/v1/knowledge/sources:read', '/v1/knowledge/review-evidence']);
const readGets = /^\/v1\/(?:ontologies|knowledge\/(?:construction-jobs|review-queue|publication-candidates|publications|entity-resolution|review-assessments|records|property-assignment|publication-inventory|documents|quality))(?:\/|$)/;
(async()=>{
  assert.ok(['127.0.0.1','localhost'].includes(new URL(base).hostname));
  await fs.mkdir(output,{recursive:true});
  const kit = 'src/graphrag_prod/playground/static/industrial-demo-v1/';
  const source = await fs.readFile(kit+'authoritative_source.txt');
  const business = await fs.readFile(kit+'maintenance_report.txt');
  const browser = await chromium.launch({headless:true});
  let page, releaseConstruct, constructBody, onConstruct, failConstruct=false;
  const shoot=async name=>{await page.screenshot({path:path.join(output,name)});report.screenshots.push(name);};
  try {
    const context=await browser.newContext({viewport:{width:1440,height:1000},deviceScaleFactor:1});
    assert.equal((await (await context.request.get(base+'/playground/bootstrap')).json()).data_scope,'pump-only');
    await context.route('**/*',async route=>{
      const req=route.request(),url=new URL(req.url());
      const fulfill=body=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
      if(url.origin===origin && req.method()==='POST' && url.pathname==='/v1/knowledge:preflight')
        return fulfill({exact_matches:[],similar_matches:[],truncated:false,review_token:'a'.repeat(64)});
      if(url.origin===origin && req.method()==='POST' && url.pathname==='/v1/knowledge:construct') {
        constructBody=req.postDataJSON();report.simulatedConstructs++;
        await new Promise(resolve=>{releaseConstruct=resolve;onConstruct?.();});
        if(failConstruct)return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:{code:'dependency_unavailable',message:'测试：构建服务暂不可用'}})});
        return fulfill({job_id:'pump-ui-receipt',document_id:'pump-ui-document',version_id:'pump-ui-version',snapshot_id:'pump-ui-snapshot',extraction_mode:'LLM',status:'COMPLETED',chunks:[{chunk_id:'pump-ui-chunk',status:'CANDIDATE',mention_record_ids:['pump-ui-mention'],assertion_record_ids:[],validation_attempts:[],finding_codes:[]}]});
      }
      const allowedGet=['GET','HEAD'].includes(req.method()) && (url.pathname==='/industrial'||url.pathname.startsWith('/industrial/assets/')||url.pathname==='/playground/bootstrap'||url.pathname==='/favicon.ico'||/^\/playground\/(?:assets\/)?industrial-demo-v1\//.test(url.pathname)||readGets.test(url.pathname));
      if(url.origin!==origin || !(allowedGet || req.method()==='POST' && readPosts.has(url.pathname))){report.blocked.push({method:req.method(),path:url.pathname});return route.abort('blockedbyclient');}
      return route.continue();
    });
    page=await context.newPage();page.on('pageerror',e=>report.pageErrors.push(e.message));
    const initialGraph=page.waitForResponse(r=>new URL(r.url()).pathname==='/v1/knowledge/graph:query');
    await page.goto(base+'/industrial');assert.ok((await initialGraph).ok());await page.locator('.graph-toolbar').waitFor();
    await page.locator('[data-panel="build"]').click();
    await page.locator('[data-step-target="step-publication"]').waitFor();
    await page.waitForFunction(()=>document.querySelector('#document-tbox')?.value==='pump-maintenance-demo');
    const upload=page.locator('#construct-button'), box=page.locator('#construction-output'), scope=page.locator('#document-knowledge-scope');
    await scope.selectOption('AUTHORITATIVE');
    await page.locator('#document-file').setInputFiles({name:'authoritative_source.txt',mimeType:'text/plain',buffer:source});
    const submitted=page.waitForRequest(r=>new URL(r.url()).pathname==='/v1/knowledge:construct');
    await upload.click();await submitted;
    for(const viewport of [{width:1280,height:800},{width:1440,height:1000},{width:1920,height:1080}]){
      await page.setViewportSize(viewport);await upload.scrollIntoViewIfNeeded();
      assert.equal(await upload.getAttribute('aria-busy'),'true');
      assert.equal(await scope.isDisabled(),true);
      assert.equal(await page.locator('#document-file').isDisabled(),true);
      assert.equal(await page.locator('.operation-feedback:visible').count(),0);
      assert.equal(await box.isVisible(),false);
      assert.doesNotMatch(await page.locator('#construction-submit-note').textContent(),/正在|处理中|加载/);
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      await shoot(`pending-${viewport.width}.png`);
    }
    await page.evaluate(()=>document.documentElement.requestFullscreen());
    await shoot('pending-fullscreen.png');await page.evaluate(()=>document.exitFullscreen());
    releaseConstruct();
    await page.waitForFunction(()=>!document.querySelector('#construct-button').disabled);
    assert.match(await box.textContent(),/pump-ui-receipt/);
    assert.equal(constructBody.knowledge_scope,'AUTHORITATIVE');
    await scope.selectOption('BUSINESS');
    assert.equal(await box.isVisible(),false);assert.equal(await box.textContent(),'');
    assert.equal(await page.locator('#document-title').inputValue(),'');
    assert.equal(await page.locator('#document-uri').inputValue(),'');
    assert.equal(await page.locator('#document-file').inputValue(),'');
    assert.equal(await page.locator('#construction-next').isVisible(),false);
    for(const viewport of [{width:1280,height:800},{width:1440,height:1000},{width:1920,height:1080}]){
      await page.setViewportSize(viewport);await scope.scrollIntoViewIfNeeded();await shoot(`scope-reset-${viewport.width}.png`);
    }
    await page.locator('#document-file').setInputFiles({name:'maintenance_report.txt',mimeType:'text/plain',buffer:business});
    assert.equal(await page.locator('#document-uri').inputValue(),'urn:local:controlled-upload:maintenance_report.txt');
    assert.equal(await page.locator('#document-title').inputValue(),'maintenance_report');
    failConstruct=true;
    const routed=new Promise(resolve=>{onConstruct=resolve;});
    const failedRequest=page.waitForRequest(r=>new URL(r.url()).pathname==='/v1/knowledge:construct');
    await upload.click();await failedRequest;await routed;
    assert.equal(constructBody.knowledge_scope,'BUSINESS');
    assert.equal(constructBody.canonical_uri,'urn:local:controlled-upload:maintenance_report.txt');
    releaseConstruct();await page.waitForFunction(()=>!document.querySelector('#construct-button').disabled);
    assert.equal(await upload.getAttribute('aria-busy'),null);assert.equal(await scope.isDisabled(),false);
    assert.match(await box.textContent(),/暂时不可用/);
    // Read the real pending batch. The server preview is read-only and must keep
    // rejecting the existing mixed source versions, without publishing anything.
    await page.locator('[data-step-target="step-publication"]').first().click();
    await page.locator('[data-publication-group]').first().waitFor();
    for(const group of await page.locator('[data-publication-group]').all())await group.check();
    // Model the receipt left by the previous successful release from the screenshot.
    await page.evaluate(()=>{const output=document.querySelector('#publication-output');output.hidden=false;output.textContent='previous-publication-receipt';});
    const previewed=page.waitForResponse(r=>new URL(r.url()).pathname==='/v1/knowledge/publications:preview');
    await page.locator('#publication-preview-button').click();const response=await previewed;
    assert.equal(response.status(),409);const issue=(await response.json()).publication_issue;
    assert.equal(issue.reason,'SOURCE_VERSION_CONFLICT');
    await page.locator('#publication-preview [data-publication-upload]').waitFor();
    assert.equal(await page.locator('#publication-output').isVisible(),false);
    assert.equal(await page.locator('#publication-output').textContent(),'');
    assert.equal(await page.locator('#publication-button').isDisabled(),true);
    report.liveConflictReason=issue.reason;
    for(const viewport of [{width:1280,height:800},{width:1440,height:1000},{width:1920,height:1080}]){
      await page.setViewportSize(viewport);await page.locator('#publication-preview').scrollIntoViewIfNeeded();
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      await shoot(`conflict-${viewport.width}.png`);
    }
    await page.locator('#publication-preview [data-publication-upload]').click();
    await upload.waitFor();assert.equal(await page.locator('#upload-card').isVisible(),true);
    assert.deepEqual(report.pageErrors,[]);assert.deepEqual(report.blocked,[]);
    report.status='passed';
  } catch(error) {report.status='failed';report.error=error.message;if(page)await shoot('failure.png');throw error;}
  finally {releaseConstruct?.();await browser.close();await fs.writeFile(path.join(output,'results.json'),JSON.stringify(report,null,2));console.log(JSON.stringify({status:report.status,output,error:report.error}));}
})().catch(error=>{console.error(error);process.exitCode=1;});
