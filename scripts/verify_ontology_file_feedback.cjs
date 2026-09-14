/** Read-only file-selection QA. Business writes are blocked, including ontology import. */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {chromium} = require('playwright');
const base = 'http://127.0.0.1:8002';
const output = path.resolve('.local/browser-qa/ontology-file-feedback');
const readPosts = new Set(['/playground/session', '/v1/knowledge/graph:query',
  '/v1/knowledge/graph:evidence', '/v1/knowledge/sources:query', '/v1/knowledge/sources:read',
  '/v1/knowledge/publications:compare', '/v1/knowledge/quality/reviews:query', '/v1/retrieval']);

(async () => {
  await fs.mkdir(output, {recursive:true});
  const report = {screenshots:[], blockedRequests:[], pageErrors:[], failedResponses:[]};
  const browser = await chromium.launch({headless:true});
  try {
    const context = await browser.newContext({viewport:{width:1440,height:1000}});
    const bootstrap = await (await context.request.get(base+'/playground/bootstrap')).json();
    assert.equal(bootstrap.data_scope,'pump-only');
    const kb = bootstrap.knowledge_bases.find(x=>x.name==='母线槽知识库');
    assert.ok(kb,'Use the existing workspace; never create one for this check.');
    const persona = bootstrap.personas.find(x=>x.knowledge_base_id===kb.knowledge_base_id && x.role==='admin');
    const session = await (await context.request.post(base+'/playground/session',{data:{persona_id:persona.id}})).json();
    const headers = {Authorization:'Bearer '+session.access_token};
    const state = async () => {
      const response = await context.request.get(base+`/playground/workspaces/${kb.knowledge_base_id}/reset-preview`,{headers});
      assert.ok(response.ok());
      const versions = await context.request.get(base+'/v1/ontologies',{headers});
      assert.ok(versions.ok());
      return {preview:await response.json(),versions:await versions.json()};
    };
    const before = await state();
    await context.route('**/*',async route=>{
      const r=route.request(),url=new URL(r.url());
      if(url.origin!==base || !(['GET','HEAD'].includes(r.method()) || r.method()==='POST' && readPosts.has(url.pathname))) {
        report.blockedRequests.push({method:r.method(),path:url.pathname});
        return route.abort('blockedbyclient');
      }
      return route.continue();
    });
    const page = await context.newPage();
    page.on('pageerror',error=>report.pageErrors.push(error.message));
    page.on('response',r=>{if(r.status()>=400) report.failedResponses.push({path:new URL(r.url()).pathname,status:r.status()});});
    await page.goto(base+'/industrial');
    await page.locator('#knowledge-base').selectOption(kb.knowledge_base_id);
    await page.locator('[data-panel="build"]').click();
    await page.locator('#build-tab-ontology').click();
    await page.locator('#ontology-editor').waitFor({state:'visible'});
    const choose = async file => {
      await page.locator('#ontology-file').setInputFiles(path.resolve(file));
      const text = await fs.readFile(file,'utf8');
      await page.waitForFunction(value=>document.querySelector('#ontology-editor').value===value,text);
    };
    await choose('src/graphrag_prod/playground/static/industrial-demo-v1/ontology.json');
    assert.equal(await page.locator('#ontology-import-button').isEnabled(),true);
    await choose('busway_files/topology.source.json');
    await page.getByRole('alert').filter({hasText:'实例构建'}).waitFor({state:'visible'});
    assert.equal(await page.locator('#ontology-import-button').isDisabled(),true);
    for(const width of [1280,1440,1920]) {
      await page.setViewportSize({width,height:width===1280?900:1080});
      await page.evaluate(()=>window.scrollTo(0,0));
      const name=`wrong-file-${width}.png`;
      await page.screenshot({path:path.join(output,name)});report.screenshots.push(name);
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
    }
    await page.locator('#ontology-import-button').scrollIntoViewIfNeeded();
    await page.screenshot({path:path.join(output,'wrong-file-scrolled.png')});report.screenshots.push('wrong-file-scrolled.png');
    await choose('busway_files/ontology.source.json');
    assert.equal(await page.locator('#ontology-input-error').isHidden(),true);
    assert.equal(await page.locator('#ontology-import-button').isEnabled(),true);
    await page.locator('#ontology-editor').fill(await fs.readFile('busway_files/topology.source.json','utf8'));
    assert.equal(await page.locator('#ontology-import-button').isDisabled(),true);
    assert.match(await page.locator('#ontology-input-error').textContent(),/实例构建/);
    await page.locator('#ontology-reset').click();
    assert.equal(await page.locator('#ontology-input-error').isHidden(),true);
    assert.equal(await page.locator('#ontology-import-button').isEnabled(),true);
    await choose('busway_files/ontology.source.json');
    await page.evaluate(()=>window.scrollTo(0,0));
    await page.screenshot({path:path.join(output,'corrected-file.png')});report.screenshots.push('corrected-file.png');
    assert.deepEqual(await state(),before,'The file selection checks must not change any business state.');
    assert.deepEqual(report.blockedRequests,[]);assert.deepEqual(report.pageErrors,[]);assert.deepEqual(report.failedResponses,[]);
    report.status='passed';report.businessStateUnchanged=true;report.knowledgeBaseId=kb.knowledge_base_id;
    await context.close();
  } catch(error) {
    report.status='failed';report.error=error.stack;process.exitCode=1;
  } finally {
    await browser.close();
    await fs.writeFile(path.join(output,'report.json'),JSON.stringify(report,null,2)+'\n');
    console.log(JSON.stringify(report));
  }
})();
