/** User-authorized ontology + topology import in the existing reset busway workspace. */
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
const {chromium}=require('playwright');
const base='http://127.0.0.1:8002';
const phase=process.env.AUTO_REVIEW_QA_PHASE||'topology';
assert.ok(['ontology','topology'].includes(phase));
const output=path.resolve('.local/browser-qa/auto-review-upload');
const allowed=new Set(['/playground/session','/v1/knowledge/graph:query','/v1/knowledge/graph:evidence',
  '/v1/knowledge/sources:query','/v1/knowledge/sources:read','/v1/knowledge/publications:compare',
  '/v1/knowledge/quality/reviews:query', '/v1/knowledge:preflight',
  ...(phase==='ontology'?['/v1/ontologies:import']:['/v1/knowledge:construct'])]);
(async()=>{
  await fs.mkdir(output,{recursive:true});
  const report={phase,status:'running',started_at:new Date().toISOString(),errors:[],blocked:[],screenshots:[]};
  const browser=await chromium.launch({headless:true});
  let page,timer;
  try {
    const context=await browser.newContext({viewport:{width:1440,height:1000}});
    const bootstrap=await(await context.request.get(base+'/playground/bootstrap')).json();
    assert.equal(bootstrap.data_scope,'pump-only');
    const workspace=bootstrap.knowledge_bases.find(x=>x.name==='母线槽知识库');assert.ok(workspace);
    report.workspace_id=workspace.knowledge_base_id;report.generation=workspace.generation;
    await context.route('**/*',async route=>{
      const r=route.request(),u=new URL(r.url());
      if(u.origin!==base || !(['GET','HEAD'].includes(r.method()) || r.method()==='POST'&&allowed.has(u.pathname))) {
        report.blocked.push({method:r.method(),path:u.pathname});return route.abort();
      }
      return route.continue();
    });
    page=await context.newPage();page.setDefaultTimeout(60000);
    page.on('request',r=>{
      if(new URL(r.url()).pathname==='/v1/knowledge:construct')
        fs.writeFile(path.join(output,'construction-request.json'),r.postData()).catch(()=>{});
    });
    page.on('pageerror',e=>report.errors.push(e.message));
    await page.goto(base+'/industrial');
    await page.locator('#knowledge-base').selectOption(workspace.knowledge_base_id);
    await page.locator('[data-panel="build"]').click();
    if(phase==='ontology') {
      await page.locator('#build-tab-ontology').click();
      await page.locator('#ontology-file').setInputFiles(path.resolve('busway_files/ontology.source.json'));
      const source=await fs.readFile('busway_files/ontology.source.json','utf8');
      await page.waitForFunction(text=>document.getElementById('ontology-editor').value===text,source);
      await page.locator('.ontology-reference-picker summary').click();
      await page.locator('#ontology-rule-file').setInputFiles(path.resolve('busway_files/rule.references.json'));
      await page.locator('#ontology-rule-file-name').getByText('rule.references.json').waitFor();
      const completed=page.waitForResponse(r=>new URL(r.url()).pathname==='/v1/ontologies:import',{timeout:180000});
      await page.locator('#ontology-import-button').click();
      const response=await completed,body=await response.json();
      assert.ok(response.ok(),JSON.stringify(body));assert.equal(body.status,'PUBLISHED');
      report.result={key:body.key,version:body.version,tbox_id:body.tbox_id,status:body.status};
    } else {
      await page.locator('#build-tab-instances').click();
      await page.locator('#ontology-selection').evaluate(e=>{e.open=true;});
      await page.locator('#document-tbox').fill('ai_power.busway.ontology');
      await page.locator('#ontology-selection').evaluate(e=>{e.open=false;});
      await page.locator('#document-knowledge-scope').selectOption('AUTHORITATIVE');
      await page.locator('#document-file').setInputFiles(path.resolve('busway_files/topology.source.json'));
      await page.locator('#document-title').fill('母线槽工程拓扑与逻辑测点');
      assert.equal(await page.locator('#document-extraction-mode').inputValue(),'LLM');
      const completion=page.waitForResponse(r=>new URL(r.url()).pathname==='/v1/knowledge:construct',{timeout:4350000});
      completion.catch(()=>{});
      const preflight=page.waitForResponse(r=>new URL(r.url()).pathname==='/v1/knowledge:preflight');
      await page.locator('#construct-button').click();
      const checked=await preflight;assert.ok(checked.ok(),await checked.text());
      const preflightBody=await checked.json();
      assert.equal(preflightBody.exact_matches.length,0,'Do not silently create another upload; inspect the existing job first.');
      timer=setInterval(async()=>{
        try {
          const progress=await page.locator('#construction-progress').innerText();
          console.log(JSON.stringify({at:new Date().toISOString(),progress}));
          await fs.writeFile(path.join(output,'progress.json'),JSON.stringify({at:new Date().toISOString(),progress}));
        }catch{}
      },15000);
      const response=await completion,body=await response.json();
      await fs.writeFile(path.join(output,'construction-response.json'),JSON.stringify(body,null,2));
      assert.ok(response.ok(),JSON.stringify(body));assert.ok(body.auto_review,'Automatic review receipt required');
      report.result={job_id:body.job_id,auto_review:body.auto_review};
      assert.ok(['COMPLETED','PARTIAL'].includes(body.auto_review.status),JSON.stringify(body.auto_review));
      assert.ok(body.auto_review.counts.approved_mentions>0,'At least deterministic identities must be approved');
      assert.ok(body.auto_review.counts.approved_assertions>0,'At least deterministic facts must be approved');
      await page.locator('#construct-button').waitFor({state:'visible'});
      await page.waitForFunction(()=>!document.getElementById('construct-button').disabled);
      await page.locator('#construction-output').scrollIntoViewIfNeeded();
    }
    for(const width of [1280,1440,1920]) {
      await page.setViewportSize({width,height:width===1280?800:1080});
      const name=`${phase}-${width}.png`;await page.screenshot({path:path.join(output,name)});report.screenshots.push(name);
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Horizontal overflow');
    }
    assert.deepEqual(report.errors,[]);assert.deepEqual(report.blocked,[]);report.status='passed';
  }catch(e){report.status='failed';report.error=e.message;if(page)await page.screenshot({path:path.join(output,phase+'-failure.png')}).catch(()=>{});throw e;}
  finally{clearInterval(timer);report.finished_at=new Date().toISOString();await fs.writeFile(path.join(output,phase+'-report.json'),JSON.stringify(report,null,2));await browser.close();console.log(JSON.stringify(report));}
})().catch(e=>{console.error(e.message);process.exitCode=1;});
