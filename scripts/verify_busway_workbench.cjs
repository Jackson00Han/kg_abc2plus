/** User-authorized busway setup/import/extraction in a dedicated 8002 workspace. */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require('playwright');

const base = 'http://127.0.0.1:8002';
const output = path.resolve('.local/browser-qa/busway');
const phase = process.argv[2] || 'visual';
assert.ok(['setup', 'ontology', 'topology', 'topology-resume', 'knowledge', 'visual', 'review', 'receipt'].includes(phase));
const isTopology=phase==='topology' || phase==='topology-resume';
const workspaceName = '母线槽知识库';
const allowedPosts = new Set([
  '/playground/session', '/playground/workspaces', '/v1/ontologies:import',
  '/v1/knowledge:preflight', '/v1/knowledge:construct', '/v1/knowledge/graph:query',
  '/v1/knowledge/graph:evidence', '/v1/knowledge/sources:query', '/v1/knowledge/sources:read',
  '/v1/knowledge/publications:compare', '/v1/knowledge/quality/reviews:query', '/v1/retrieval',
]);

(async () => {
  await fs.mkdir(output, {recursive:true});
  const report = {phase, status:'running', screenshots:[], pageErrors:[], failedResponses:[], blockedRequests:[]};
  const browser = await chromium.launch({headless:true});
  let page;
  let progressTimer;
  try {
    const context = await browser.newContext({viewport:{width:1440,height:1000},deviceScaleFactor:1});
    const bootstrap = await (await context.request.get(base + '/playground/bootstrap')).json();
    assert.equal(bootstrap.data_scope, 'pump-only');
    report.dataScope = bootstrap.data_scope;
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.origin !== base || !(['GET','HEAD'].includes(request.method()) || request.method()==='POST' && allowedPosts.has(url.pathname))) {
        report.blockedRequests.push({method:request.method(),path:url.pathname});
        return route.abort('blockedbyclient');
      }
      if (phase==='topology-resume' && url.pathname==='/v1/knowledge:construct'
        && request.postDataJSON()?.operation_key!==report.operationKey) {
        report.blockedRequests.push({method:request.method(),path:url.pathname,reason:'resume operation key changed'});
        return route.abort('blockedbyclient');
      }
      await route.continue();
    });
    page = await context.newPage();
    page.setDefaultTimeout(30000);
    page.on('pageerror', e => report.pageErrors.push(e.message));
    const reviewDetailRequests=[];
    page.on('request',r=>{
      if (/\/v1\/knowledge\/(entity-resolution|review-assessments|property-assignment)\//.test(new URL(r.url()).pathname)) reviewDetailRequests.push(new URL(r.url()).pathname);
    });
    page.on('response', r => {if(r.status()>=400) report.failedResponses.push({path:new URL(r.url()).pathname,status:r.status()});});
    await page.goto(base + '/industrial');
    await page.locator('#knowledge-base').waitFor({state:'visible'});
    const existing = bootstrap.knowledge_bases.find(item => item.name===workspaceName);
    if (existing) {
      await page.locator('#knowledge-base').selectOption(existing.knowledge_base_id);
    } else {
      assert.equal(phase,'setup','Create the named workspace with the explicit setup phase first.');
      await page.locator('#create-knowledge-base').click();
      const dialog = page.getByRole('dialog',{name:'新建独立知识库'});
      await dialog.locator('input').fill(workspaceName);
      const created = page.waitForResponse(r => new URL(r.url()).pathname==='/playground/workspaces' && r.request().method()==='POST');
      await dialog.getByRole('button',{name:'创建并进入'}).click();
      assert.ok((await created).ok());
      await dialog.waitFor({state:'hidden'});
    }
    await page.waitForFunction(name => document.querySelector('#knowledge-base')?.selectedOptions[0]?.textContent===name, workspaceName);
    report.workspaceId = await page.locator('#knowledge-base').inputValue();
    if (phase!=='setup' && phase!=='ontology') {
      await page.waitForFunction(() => document.querySelector('#ontology-list')?.textContent.includes('ai_power.busway.ontology'));
    }
    await page.locator('[data-panel="build"]').click();
    await page.locator('#build-tab-ontology').click();
    await page.locator('#ontology-editor').waitFor({state:'visible'});

    if (phase==='ontology') {
      await page.locator('#ontology-file').setInputFiles(path.resolve('busway_files/ontology.source.json'));
      const source = await fs.readFile('busway_files/ontology.source.json','utf8');
      await page.waitForFunction(text => document.querySelector('#ontology-editor').value===text, source);
      await page.locator('.ontology-reference-picker summary').click();
      await page.locator('#ontology-rule-file').setInputFiles(path.resolve('busway_files/rule.references.json'));
      await page.locator('#ontology-rule-file-name').getByText('rule.references.json').waitFor();
      const saved = page.waitForResponse(r => new URL(r.url()).pathname==='/v1/ontologies:import',{timeout:90000});
      await page.locator('#ontology-import-button').click();
      const response = await saved, payload = await response.json();
      report.importStatus=response.status();
      if (!response.ok()) report.importError=payload;
      assert.ok(response.ok(),JSON.stringify(payload));
      assert.equal(payload.status,'PUBLISHED');
      assert.equal(payload.key,'ai_power.busway.ontology');
      report.ontology={key:payload.key,version:payload.version,status:payload.status,checksum:payload.checksum,capabilities:payload.import_capabilities};
      await page.locator('#ontology-list .trust-badge').getByText('已启用',{exact:true}).waitFor();
      const scope = page.locator('#ontology-list .ontology-capabilities summary');
      if (await scope.count()) await scope.first().click();
    }

    if (phase==='visual') {
      const original = JSON.parse(await fs.readFile('busway_files/ontology.source.json','utf8'));
      await page.locator('[data-load-tbox]').first().click();
      const replay = JSON.parse(await page.locator('#ontology-editor').inputValue());
      for (const key of Object.keys(original)) assert.deepEqual(replay[key],original[key]);
      assert.ok(replay.expected_checksum);
      assert.equal(replay.rule_reference_registry.documents.length,1);
      const download = page.waitForEvent('download');
      await page.locator('[data-download-tbox]').first().click();
      const exportedPath=path.join(output,'ontology-export.json');
      await (await download).saveAs(exportedPath);
      const exported=JSON.parse(await fs.readFile(exportedPath,'utf8'));
      for (const key of Object.keys(original)) assert.deepEqual(exported.definition[key],original[key]);
      await page.locator('[data-copy-tbox]').first().click();
      assert.equal(JSON.parse(await page.locator('#ontology-editor').inputValue()).metadata.ontology_version,'5.0.1');
      await page.locator('[data-load-tbox]').first().click();
      report.definitionReplay=true;report.exportPreservesSource=true;report.copyNextVersion='5.0.1';
    }

    if (phase==='review') {
      await page.waitForFunction(()=>document.querySelector('#review-list [data-review-record]'));
      assert.deepEqual(reviewDetailRequests,[],'Hidden review rows must not prefetch detail requests.');
      await page.locator('#build-tab-instances').click();
      const firstCheck=page.waitForResponse(r=>new URL(r.url()).pathname.includes('/v1/knowledge/entity-resolution/'));
      await page.locator('[data-step-target="step-review"]').click();
      await page.locator('#review-list [data-review-record]').first().scrollIntoViewIfNeeded();
      assert.ok((await firstCheck).ok());
      await page.locator('#review-list [data-review-record]').first().locator('.review-matching-basis').waitFor({state:'attached'});
      report.review={hiddenDetailRequests:0};
    }

    if (phase==='receipt') {
      await page.locator('#build-tab-instances').click();
      const expected=JSON.parse(await fs.readFile(path.join(output,'topology-job-progress.json'),'utf8'));
      assert.equal(expected.status,'COMPLETED');
      const card=page.locator('#construction-job-list .governance-item').filter({hasText:expected.job_id.slice(0,8)});
      await card.waitFor();
      const loaded=page.waitForResponse(r=>new URL(r.url()).pathname==='/v1/knowledge/construction-jobs/'+expected.job_id);
      await card.getByRole('button',{name:'查看任务明细'}).click();
      const response=await loaded,payload=await response.json();
      assert.ok(response.ok());assert.equal(payload.completed_chunks,75);assert.equal(payload.status,'COMPLETED');
      await page.waitForFunction(()=>document.querySelector('#construction-output details'));
      assert.equal(await page.locator('#construction-output details[open],#construction-validation-summary details[open]').count(),0);
      assert.ok((await page.locator('#construction-output').boundingBox()).height<400,'Receipt must keep the next action reachable.');
      report.receipt={jobId:payload.job_id,completed:75,detailsInitiallyCollapsed:true};
    }

    if (isTopology || phase==='knowledge') {
      await page.locator('#build-tab-instances').click();
      await page.locator('#ontology-selection').evaluate(e => {e.open=true;});
      await page.locator('#document-tbox').fill('ai_power.busway.ontology');
      await page.locator('#ontology-selection').evaluate(e => {e.open=false;});
      const file = isTopology ? 'topology.source.json' : 'busway.knowledge.md';
      await page.locator('#document-knowledge-scope').selectOption('BUSINESS');
      await page.locator('#document-file').setInputFiles(path.resolve('busway_files',file));
      await page.locator('#document-title').fill(isTopology ? '母线槽工程拓扑与逻辑测点' : '母线槽工程结构与诊断知识说明');
      assert.equal(await page.locator('#document-extraction-mode').inputValue(), 'LLM');
      if(phase==='topology-resume') {
        const previous=JSON.parse(await fs.readFile(path.join(output,'topology-job-progress.json'),'utf8'));
        assert.equal(previous.status,'RETRY_WAIT');
        const persona=bootstrap.personas.find(p=>p.knowledge_base_id===report.workspaceId && p.label==='管理员');
        // Restore this task's nonsecret idempotency receipt after the earlier
        // temporary browser context closed. Server still validates every field.
        await page.evaluate(async({personaId,operationKey})=>{
          const value=id=>document.getElementById(id).value.trim();
          const file=document.getElementById('document-file').files[0];
          const bytes=await file.arrayBuffer();
          const contentHash=[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(x=>x.toString(16).padStart(2,'0')).join('');
          const fingerprint=JSON.stringify({content_sha256:contentHash,industrial_context:null,persona_id:personaId,
            canonical_uri:value('document-uri'),title:value('document-title'),source_name:value('document-source'),mime_type:'application/json',
            language:value('document-language'),tbox_key:value('document-tbox'),access_groups:['administrators','members'],extraction_mode:'LLM',knowledge_scope:'BUSINESS'});
          sessionStorage.setItem('graphrag-construction-operation',JSON.stringify({fingerprint,operationKey}));
        },{personaId:persona.id,operationKey:previous.operation_key});
        report.resumesJob=previous.job_id;report.previousCompleted=previous.completed_chunks;report.operationKey=previous.operation_key;
      }
      const completed = page.waitForResponse(r => new URL(r.url()).pathname==='/v1/knowledge:construct',{timeout:4350000});
      completed.catch(()=>{});
      const started = page.waitForRequest(r => new URL(r.url()).pathname==='/v1/knowledge:construct',{timeout:90000});
      started.catch(()=>{});
      const preflight = page.waitForResponse(r => new URL(r.url()).pathname==='/v1/knowledge:preflight',{timeout:90000});
      await page.locator('#construct-button').click();
      const checked = await preflight;
      assert.ok(checked.ok(), JSON.stringify(await checked.json()));
      console.log(JSON.stringify({phase,status:'preflight-passed'}));
      // A rerun may require the existing duplicate-source review acknowledgment.
      const acknowledgment = page.locator('[data-upload-continue]');
      await Promise.race([page.locator('#construction-progress').waitFor({state:'visible',timeout:30000}),acknowledgment.waitFor({state:'attached',timeout:30000})]).catch(()=>{});
      if (await acknowledgment.count()) {
        if (!await acknowledgment.isVisible()) await acknowledgment.locator('..').evaluate(e => {e.open=true;});
        await acknowledgment.click();
        console.log(JSON.stringify({phase,status:'duplicate-review-acknowledged'}));
      }
      const startedRequest=await started;
      if (phase==='topology-resume') assert.equal(startedRequest.postDataJSON().operation_key,report.operationKey);
      await page.screenshot({path:path.join(output,phase+'-running.png')});
      report.screenshots.push(phase+'-running.png');
      console.log(JSON.stringify({phase,status:'extracting',workspaceId:report.workspaceId}));
      progressTimer=setInterval(async()=>{
        const progress=await page.locator('#construction-progress').textContent().catch(()=>null);
        console.log(JSON.stringify({phase,progress}));
      },30000);
      const response=await completed,payload=await response.json();
      clearInterval(progressTimer);
      report.extractionStatus=response.status();
      await fs.writeFile(path.join(output,phase+'-response.json'),JSON.stringify(payload,null,2));
      assert.ok(response.ok(),JSON.stringify(payload));
      if (phase==='topology-resume') assert.equal(payload.job_id,report.resumesJob,'Resume must keep the original construction job.');
      report.extraction={chunks:payload.chunks?.length,statuses:{},mentions:0,assertions:0};
      for(const chunk of payload.chunks||[]) {
        report.extraction.statuses[chunk.status]=(report.extraction.statuses[chunk.status]||0)+1;
        report.extraction.mentions+=(chunk.mention_record_ids||[]).length;
        report.extraction.assertions+=(chunk.assertion_record_ids||[]).length;
      }
      await page.locator('#construct-button').waitFor({state:'visible'});
      await page.waitForFunction(()=>!document.querySelector('#construct-button').disabled);
    }

    for (const width of [1280,1440,1920]) {
      await page.setViewportSize({width,height:width===1280?800:1080});
      await page.evaluate(()=>window.scrollTo(0,0));
      await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
      const metrics=await page.evaluate(()=>({viewport:innerWidth,page:document.documentElement.scrollWidth}));
      assert.ok(metrics.page<=metrics.viewport,'page must fit desktop width');
      const name=`${phase}-${width}.png`;
      await page.screenshot({path:path.join(output,name)});report.screenshots.push(name);
    }
    const scrollTarget = page.locator(phase==='receipt' ? '#construction-output' : phase==='review' ? '#review-list .review-entity-card:last-child' : isTopology || phase==='knowledge' ? '#construction-validation-summary' : '#ontology-list');
    await scrollTarget.scrollIntoViewIfNeeded({timeout:10000}).catch(()=>{});
    if (phase==='review') await scrollTarget.locator('.review-matching-basis').first().waitFor({state:'attached'});
    if (phase==='visual') await page.locator('.ontology-capabilities summary').first().click();
    await page.screenshot({path:path.join(output,phase+'-scrolled.png')});report.screenshots.push(phase+'-scrolled.png');
    if (phase==='receipt') {
      await page.locator('#construction-validation-summary details > summary').first().click();
      assert.equal(await page.locator('#construction-validation-summary article').count(),75);
      await page.locator('#construction-validation-summary').scrollIntoViewIfNeeded();
      await page.screenshot({path:path.join(output,'receipt-validation.png')});report.screenshots.push('receipt-validation.png');
    }
    if (phase==='review') {
      const factCheck=page.waitForResponse(r=>new URL(r.url()).pathname.includes('/v1/knowledge/review-assessments/'));
      await page.locator('[data-review-phase="facts"]').click();
      await page.locator('#review-list [data-review-record]').first().scrollIntoViewIfNeeded();
      assert.ok((await factCheck).ok());
      const firstFact=page.locator('#review-list [data-review-record]').first();
      await firstFact.locator('[data-assessment-panel] .review-check').waitFor({state:'attached'});
      assert.equal(await firstFact.locator('[data-review-action="APPROVED"]').first().isDisabled(),true,'Unconfirmed entity dependencies must keep approval disabled.');
      await page.screenshot({path:path.join(output,'review-facts.png')});report.screenshots.push('review-facts.png');
      report.review.detailRequests=reviewDetailRequests;
      assert.ok(reviewDetailRequests.length<30,'Visible review checks must remain bounded, without a full-queue request burst.');
    }
    assert.deepEqual(report.pageErrors,[]);
    assert.deepEqual(report.failedResponses,[],'All workbench requests must finish without HTTP errors.');
    assert.deepEqual(report.blockedRequests,[]);
    report.status='passed';
  } catch(e) {
    report.status='failed';report.error=e.message;
    if(page) {
      await page.screenshot({path:path.join(output,phase+'-failed.png')}).catch(()=>{});
      report.uiState=await page.evaluate(()=>({ontologySelected:document.querySelector('#build-tab-ontology')?.getAttribute('aria-selected'),instancesSelected:document.querySelector('#build-tab-instances')?.getAttribute('aria-selected'),foundationHidden:document.querySelector('#expert-foundation')?.hidden})).catch(()=>null);
    }
    throw e;
  } finally {
    clearInterval(progressTimer);
    await browser.close();
    await fs.writeFile(path.join(output,phase+'-results.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify(report));
  }
})().catch(e=>{console.error(e.message);process.exitCode=1;});
