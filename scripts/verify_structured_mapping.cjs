/** Read-only 8002 visual QA; inject an isolated receipt into a job-detail GET. */
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
const {chromium}=require('playwright');
const base='http://127.0.0.1:8002';
const output=path.resolve('.local/browser-qa/structured-mapping');
const readPosts=new Set(['/playground/session','/v1/knowledge/graph:query','/v1/knowledge/graph:evidence',
  '/v1/knowledge/sources:query','/v1/knowledge/sources:read','/v1/knowledge/publications:compare','/v1/knowledge/quality/reviews:query']);
(async()=>{
  await fs.mkdir(output,{recursive:true});
  const fixture=JSON.parse(await fs.readFile('.local/structured-mapping/ui-receipt.json','utf8'));
  const report={status:'running',mode:'isolated receipt fixture; real browser and workbench; no knowledge writes',screenshots:[],errors:[],blocked:[]};
  const browser=await chromium.launch({headless:true});
  try {
    const context=await browser.newContext({viewport:{width:1440,height:1000}});
    const bootstrap=await (await context.request.get(base+'/playground/bootstrap')).json();
    assert.equal(bootstrap.data_scope,'pump-only');
    await context.route('**/*',async route=>{
      const request=route.request(),url=new URL(request.url());
      if(url.origin!==base || !(['GET','HEAD'].includes(request.method()) || request.method()==='POST' && readPosts.has(url.pathname))){
        report.blocked.push({method:request.method(),path:url.pathname});return route.abort();
      }
      if(request.method()==='GET' && /^\/v1\/knowledge\/construction-jobs\/[^/]+$/.test(url.pathname)) {
        const response=await route.fetch();assert.ok(response.ok());
        return route.fulfill({response,json:{...await response.json(),...fixture}});
      }
      await route.continue();
    });
    const page=await context.newPage();
    page.on('pageerror',error=>report.errors.push(error.message));
    await page.goto(base+'/industrial');
    const workspace=bootstrap.knowledge_bases.find(k=>k.name==='母线槽知识库');assert.ok(workspace);
    await page.locator('#knowledge-base').selectOption(workspace.knowledge_base_id);
    await page.locator('[data-panel="build"]').click();
    await page.locator('#build-tab-instances').click();
    await page.locator('#construction-jobs-refresh-button').click();
    await page.locator('[data-construction-job-detail]').first().click();
    await page.getByText('结构化映射构建 · 139 条来源记录',{exact:true}).waitFor();
    for(const width of [1280,1440,1920]) {
      await page.setViewportSize({width,height:width===1280?800:1080});
      await page.locator('#construction-output').scrollIntoViewIfNeeded();
      const details=page.locator('#construction-output details').filter({has:page.getByText('查看字段映射与未入图字段',{exact:true})});
      if(await details.getAttribute('open')!==null)await details.locator('summary').click();
      let file=`receipt-${width}.png`;await page.screenshot({path:path.join(output,file)});report.screenshots.push(file);
      await details.locator('summary').click();
      assert.equal(await details.locator('article').count(),3);
      await details.locator('article').first().scrollIntoViewIfNeeded();
      assert.ok(await details.innerText().then(t=>t.includes('CONNECTS_TO_COMPONENT') && t.includes('仅保留原文')));
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      file=`mapping-${width}.png`;await page.screenshot({path:path.join(output,file)});report.screenshots.push(file);
      await details.locator('article').last().scrollIntoViewIfNeeded();
      file=`mapping-scroll-${width}.png`;await page.screenshot({path:path.join(output,file)});report.screenshots.push(file);
      await details.locator('summary').click();
    }
    await page.locator('#construction-validation-summary summary').click();
    assert.equal(await page.locator('#construction-validation-summary article').count(),20);
    await page.locator('#construction-validation-summary').scrollIntoViewIfNeeded();
    const file='validation-1920.png';await page.screenshot({path:path.join(output,file)});report.screenshots.push(file);
    assert.deepEqual(report.errors,[]);assert.deepEqual(report.blocked,[]);report.status='passed';
  } catch(error) {report.status='failed';report.error=error.message;throw error;}
  finally {await fs.writeFile(path.join(output,'report.json'),JSON.stringify(report,null,2));await browser.close();console.log(JSON.stringify(report));}
})().catch(error=>{console.error(error.message);process.exitCode=1;});
