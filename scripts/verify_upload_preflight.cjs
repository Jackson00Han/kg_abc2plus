/** Real read-only pump preflight; the explicit construction is simulated and never sent. */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {chromium} = require('playwright');
const base = process.env.BROWSER_QA_URL || 'http://127.0.0.1:8002';
const origin = new URL(base).origin;
const output = path.resolve(process.env.BROWSER_QA_OUTPUT || '.local/browser-qa/upload-preflight');
const report = {status:'running', screenshots:[], pageErrors:[], blocked:[], knowledgeWrites:0, simulatedConstructs:0};
const readPosts = new Set(['/playground/session','/v1/knowledge:preflight', '/v1/knowledge/graph:query', '/v1/knowledge/graph:evidence', '/v1/knowledge/sources:query', '/v1/knowledge/sources:read', '/v1/knowledge/review-evidence']);
const readGets = /^\/v1\/(?:ontologies|knowledge\/(?:construction-jobs|review-queue|publication-candidates|publications|entity-resolution|review-assessments|records|property-assignment|publication-inventory|documents|quality))(?:\/|$)/;
(async()=>{
  assert.ok(['127.0.0.1','localhost'].includes(new URL(base).hostname));
  await fs.mkdir(output,{recursive:true});
  const source = await fs.readFile('src/graphrag_prod/playground/static/industrial-demo-v1/authoritative_source.txt');
  const changed = Buffer.from(source.toString('utf8').replaceAll('BC-P-101','BC-P-202'));
  assert.notDeepEqual(source, changed);
  const browser = await chromium.launch({headless:true});
  report.browser = browser.version();
  let page;
  try {
    const context = await browser.newContext({viewport:{width:1440,height:1000},deviceScaleFactor:1});
    const bootstrap = await (await context.request.get(base+'/playground/bootstrap')).json();
    assert.equal(bootstrap.data_scope,'pump-only');
    let allowSimulatedConstruct = false;
    await context.route('**/*',async route=>{
      const req=route.request(), url=new URL(req.url());
      if(url.origin===origin && req.method()==='POST' && url.pathname==='/v1/knowledge:construct' && allowSimulatedConstruct){
        const payload=req.postDataJSON();
        assert.match(payload.preflight_token,/^[a-f0-9]{64}$/);
        report.simulatedConstructs++;
        await new Promise(resolve=>setTimeout(resolve,1100));
        return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:{code:'dependency_unavailable',message:'测试：服务暂不可用，请稍后重试',retryable:true}})});
      }
      const allowedGet=['GET','HEAD'].includes(req.method()) && (url.pathname==='/industrial'||url.pathname.startsWith('/industrial/assets/')||url.pathname==='/playground/bootstrap'||url.pathname==='/favicon.ico'||/^\/playground\/(?:assets\/)?industrial-demo-v1\//.test(url.pathname)||readGets.test(url.pathname));
      if(url.origin!==origin || !(allowedGet || req.method()==='POST' && readPosts.has(url.pathname))){report.blocked.push({method:req.method(),path:url.pathname});return route.abort('blockedbyclient');}
      await route.continue();
    });
    page=await context.newPage();
    page.on('pageerror',e=>report.pageErrors.push(e.message));
    await page.goto(base+'/industrial');
    await page.locator('.graph-toolbar').waitFor({state:'visible'});
    await page.locator('[data-panel="build"]').click();
    const upload=page.locator('#construct-button'), box=page.locator('#construction-output');
    await page.locator('#document-file').setInputFiles({name:'renamed-pump-source.txt',mimeType:'text/plain',buffer:source});
    await page.locator('#document-title').fill('循环水泵资料重复检查');
    const checked=page.waitForResponse(r=>new URL(r.url()).pathname==='/v1/knowledge:preflight');
    await upload.click();
    await page.waitForFunction(()=>document.querySelector('#construct-button')?.getAttribute('aria-busy')==='true');
    assert.equal(await page.locator('.operation-feedback:visible').count(),0);
    assert.equal(await upload.getAttribute('aria-busy'),'true');
    const response=await checked;
    assert.ok(response.ok(),await response.text());
    const preflight=await response.json();
    assert.ok(preflight.exact_matches.length>0,'Current pump knowledge must already contain authorized source bytes');
    await box.getByText('这份资料已经存在，已暂停重复构建',{exact:true}).waitFor();
    assert.equal(report.simulatedConstructs,0);
    report.exactMatches=preflight.exact_matches.length;
    report.exactRenamedFileBlocked=true;
    for(const viewport of [{width:1280,height:800},{width:1440,height:1000},{width:1920,height:1080}]){
      await page.setViewportSize(viewport);
      await box.scrollIntoViewIfNeeded();
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      assert.ok(await page.evaluate(()=>document.querySelector('.upload-submit-bar').compareDocumentPosition(document.querySelector('#construction-output'))&Node.DOCUMENT_POSITION_FOLLOWING));
      const file=`exact-${viewport.width}.png`;await page.screenshot({path:path.join(output,file)});report.screenshots.push(file);
    }
    const existingRead=page.waitForResponse(r=>new URL(r.url()).pathname==='/v1/knowledge/sources:read');
    await box.locator('[data-upload-existing]').first().click();
    assert.ok((await existingRead).ok());
    report.existingSourceRead=true;
    // Return to the upload panel after the source drawer opens.
    const close=page.locator('[data-source-close]');
    if(await close.isVisible()) await close.click();
    await page.keyboard.press('Escape');
    await page.locator('[data-panel="build"]').click();
    await page.locator('#document-file').setInputFiles({name:'pump-number-updated.txt',mimeType:'text/plain',buffer:changed});
    const similarChecked=page.waitForResponse(r=>new URL(r.url()).pathname==='/v1/knowledge:preflight');
    await upload.click();
    const similarResponse=await similarChecked;
    assert.ok(similarResponse.ok(),await similarResponse.text());
    const similar=await similarResponse.json();
    assert.equal(similar.exact_matches.length,0);
    assert.ok(similar.similar_matches.length>0);
    await box.getByText('发现高度相似的资料，请先核对变化',{exact:true}).waitFor();
    assert.ok((await box.textContent()).includes('BC-P-202'));
    assert.ok((await box.textContent()).includes('BC-P-101'));
    report.similarity=similar.similar_matches[0].similarity;
    await box.scrollIntoViewIfNeeded();
    await page.screenshot({path:path.join(output,'similar-difference.png')});report.screenshots.push('similar-difference.png');
    await page.evaluate(()=>document.documentElement.requestFullscreen());
    await page.screenshot({path:path.join(output,'fullscreen-difference.png')});report.screenshots.push('fullscreen-difference.png');
    await page.evaluate(()=>document.exitFullscreen());
    allowSimulatedConstruct=true;
    const simulated=page.waitForResponse(r=>new URL(r.url()).pathname==='/v1/knowledge:construct');
    await box.locator('[data-upload-continue]').click();
    await page.waitForRequest(r=>new URL(r.url()).pathname==='/v1/knowledge:construct');
    assert.equal(await upload.getAttribute('aria-busy'),'true');
    await page.screenshot({path:path.join(output,'construct-pending.png')});report.screenshots.push('construct-pending.png');
    assert.equal((await simulated).status(),503);
    await page.waitForFunction(()=>!document.querySelector('#construct-button').disabled);
    assert.equal(await upload.getAttribute('aria-busy'),null);
    assert.equal(report.simulatedConstructs,1);
    report.failureRestoresControls=true;
    assert.deepEqual(report.blocked,[]);
    assert.deepEqual(report.pageErrors,[]);
    report.status='passed';
  }catch(error){report.status='failed';report.error=error.message;if(page && !page.isClosed()){await page.screenshot({path:path.join(output,'failure.png')});report.visibleMessage=await page.locator('#construction-output').textContent().catch(()=>null);}throw error;}
  finally{await browser.close();await fs.writeFile(path.join(output,'results.json'),JSON.stringify(report,null,2));console.log(JSON.stringify({status:report.status,output,error:report.error}));}
})().catch(error=>{console.error(error);process.exitCode=1;});
