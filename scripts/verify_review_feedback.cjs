/**
 * Browser-only pump review fixture: real read-only page/source requests, mocked
 * 3 -> 2 review mutation and reordered queue. No knowledge write reaches server.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {chromium} = require('playwright');

const base = process.env.BROWSER_QA_URL || 'http://127.0.0.1:8002';
const origin = new URL(base).origin;
const output = path.resolve(process.env.BROWSER_QA_OUTPUT || '.local/browser-qa/review-feedback');
const report = {status:'running',base,dataScope:null,knowledgeWrites:0,mockedKnowledgeWrites:0,viewports:[],screenshots:[],pageErrors:[],failedResponses:[],blockedRequests:[],routeErrors:[]};
const readPosts = new Set(['/playground/session','/v1/knowledge/graph:query','/v1/knowledge/graph:evidence','/v1/knowledge/sources:query','/v1/knowledge/sources:read','/v1/knowledge/review-evidence','/v1/knowledge/publications:compare','/v1/knowledge/quality/reviews:query']);
const readGets = /^\/v1\/(?:ontologies|knowledge\/(?:construction-jobs|review-queue|publication-candidates|publications|entity-resolution|review-assessments|records|property-assignment|publication-inventory|documents|quality))(?:\/|$)/;
const gate=()=>{let release;const promise=new Promise(resolve=>{release=resolve;});return {promise,release};};
const unpack=value=>value.data || value;
const safeError=error=>String(error?.message || error).split('\n')[0];
const reply=(route,value)=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(value)});

(async()=>{
  assert.ok(['localhost','127.0.0.1','[::1]'].includes(new URL(base).hostname));
  await fs.mkdir(output,{recursive:true});
  const browser=await chromium.launch({headless:true});
  report.browser=browser.version();report.playwright=require('playwright/package.json').version;
  const contexts=[],gates=[];let activePage=null,closing=false;
  try {
    for(const viewport of [{width:1280,height:800},{width:1440,height:1000},{width:1920,height:1080}]) {
      report.stage=`bootstrap-${viewport.width}`;
      const context=await browser.newContext({viewport,deviceScaleFactor:1});contexts.push(context);
      const bootstrapResponse=await context.request.get(base+'/playground/bootstrap');
      assert.ok(bootstrapResponse.ok());const bootstrap=await bootstrapResponse.json();
      assert.equal(bootstrap.data_scope,'pump-only');report.dataScope=bootstrap.data_scope;
      let source,otherSource,records,realResolution,fixtureTarget,removed=false,holdRefresh=null;
      const matchGate=gate(),saveGate=gate();gates.push(matchGate,saveGate);
      const page=await context.newPage();activePage=page;
      page.on('pageerror',error=>report.pageErrors.push(error.message));
      page.on('response',response=>{if(response.status()>=400)report.failedResponses.push({path:new URL(response.url()).pathname,status:response.status()});});
      page.on('dialog',dialog=>dialog.accept(dialog.type()==='prompt'?'浏览器隔离夹具：已核对循环水泵原文及设备编号。':undefined));
      await context.route('**/*',async route=>{
        const request=route.request(),url=new URL(request.url()),method=request.method();
        try {
        if(url.origin!==origin) {report.blockedRequests.push({method,path:url.pathname});return route.abort('blockedbyclient');}
        if(method==='GET' && url.pathname==='/v1/knowledge/review-queue') {
          if(!records) {
            const response=await route.fetch();assert.ok(response.ok(),'current real review queue must be readable');
            const payload=unpack(await response.json());
            source=(payload.items || []).find(item=>item.record_kind==='ENTITY_MENTION' && /循环.*泵|循环水泵/.test(item.entity?.canonical_name || ''));
            assert.ok(source,'current pump workspace needs at least one readable pump mention for this browser-only fixture');
            otherSource=(payload.items || []).find(item=>item.record_kind==='ENTITY_MENTION' && item.entity?.entity_id!==source.entity.entity_id && item.entity?.canonical_name!==source.entity.canonical_name);
            assert.ok(otherSource,'need a second real pump-package entity to verify that the review does not jump to another group');
            const copy=(original,id)=>({...structuredClone(original),record_id:id,revision_id:id+'-1',revision:1,trust:{...original.trust,status:'CANDIDATE'}});
            records=[copy(source,'qa-pump-1'),copy(source,'qa-pump-2'),copy(source,'qa-pump-3'),copy(otherSource,'qa-other-1')];
            report.fixtureSources={pumpRecordId:source.record_id,pumpName:source.entity.canonical_name,otherRecordId:otherSource.record_id,otherName:otherSource.entity.canonical_name};
          }
          if(holdRefresh) await holdRefresh.promise;
          return reply(route,{items:removed?[records[3],records[2],records[1]]:records});
        }
        if(method==='GET' && /^\/v1\/knowledge\/entity-resolution\/qa-/.test(url.pathname)) {
          const id=decodeURIComponent(url.pathname.split('/').at(-1));
          const record=records.find(item=>item.record_id===id);assert.ok(record);
          if(!realResolution && id==='qa-pump-1') {
            const response=await route.fetch({url:base+'/v1/knowledge/entity-resolution/'+encodeURIComponent(source.record_id)+'?expected_revision='+source.revision});
            assert.ok(response.ok(),'real pump matching read must succeed');realResolution=unpack(await response.json());
          }
          await matchGate.promise;
          const target=realResolution?.suggestions?.find(item=>item.target)?.target || realResolution?.review_targets?.find(item=>item.selectable)?.entity || source.entity;
          fixtureTarget=realResolution?.review_targets?.find(item=>item.selectable && item.entity.entity_id===target.entity_id) || {entity:target,record_id:source.record_id,revision:source.revision,status:'APPROVED',selectable:true,evidence:source.evidence,identity_properties:[],reason:'浏览器隔离夹具中的精确目标记录。'};
          return reply(route,{record_id:id,revision:1,identity_properties:realResolution?.identity_properties || [],dependent_facts:[],review_targets:[fixtureTarget],identity_actions:{independent:{allowed:true,note:'浏览器夹具',requires_reason:true}},suggestions:[{outcome:'AUTO_LINK',target,reason:'浏览器响应夹具：用于验证确认反馈及审核位置，不执行真实身份变更。',confidence:1,rule_version:'browser-fixture',matcher_version:'browser-fixture',evidence:[]}]});
        }
        if(method==='POST' && url.pathname==='/v1/knowledge/entity-resolution:apply') {
          const payload=request.postDataJSON();assert.equal(payload.record_id,'qa-pump-1');assert.equal(payload.expected_revision,1);
          assert.equal(payload.target_record_id,fixtureTarget.record_id);assert.equal(payload.target_expected_revision,fixtureTarget.revision);report.automaticConfirmationPinsTarget=true;
          report.mockedKnowledgeWrites++;await saveGate.promise;removed=true;
          return reply(route,{outcomes:[{record_id:'qa-pump-1',record_kind:'ENTITY_MENTION',previous_revision_id:'qa-pump-1-1',revision_id:'qa-pump-1-2',revision:2,status:'APPROVED'}]});
        }
        // Fixture IDs must never reach evidence/record APIs as if they were real data.
        if(request.postData()?.includes('qa-pump-') || request.postData()?.includes('qa-other-')) {
          report.blockedRequests.push({method,path:url.pathname});return route.abort('blockedbyclient');
        }
        const code=url.pathname==='/industrial' || url.pathname.startsWith('/industrial/assets/');
        const allowedRead=['GET','HEAD'].includes(method) && (code || url.pathname==='/playground/bootstrap' || url.pathname==='/favicon.ico' || /^\/playground\/(?:assets\/)?industrial-demo-v1\//.test(url.pathname) || readGets.test(url.pathname));
        if(!(allowedRead || method==='POST' && readPosts.has(url.pathname))) {
          report.blockedRequests.push({method,path:url.pathname});return route.abort('blockedbyclient');
        }
        return await route.continue();
        } catch(error) {
          if(!closing) report.routeErrors.push({method,path:url.pathname,error:safeError(error)});
          await route.abort('failed').catch(()=>{});
        }
      });
      report.stage=`initial-graph-${viewport.width}`;
      const initialGraph=page.waitForResponse(response=>new URL(response.url()).pathname==='/v1/knowledge/graph:query');
      await page.goto(base+'/industrial');assert.ok((await initialGraph).ok());
      await page.locator('.graph-toolbar').waitFor({state:'visible'});
      report.stage=`review-view-${viewport.width}`;
      await page.locator('[data-panel="build"]').click();
      await page.locator('[data-step-target="step-review"]').click();
      const first=page.locator('[data-review-record="qa-pump-1"]');
      await first.waitFor({state:'visible'});
      const details=first.locator('[data-resolution-details]');
      if(!await details.evaluate(node=>node.open)) await details.locator(':scope > summary').click();
      await first.locator('button.button-pending').waitFor({state:'visible'});
      const name=await first.locator('button.button-pending').evaluate(node=>getComputedStyle(node,'::before').animationName);
      assert.equal(name,'spin');
      report.stage=`matching-result-${viewport.width}`;
      matchGate.release();
      const apply=first.locator('[data-resolution-apply]').first();await apply.waitFor({state:'visible'});
      report.stage=`pending-confirmation-${viewport.width}`;
      await apply.scrollIntoViewIfNeeded();await apply.click();
      await page.waitForFunction(()=>document.querySelector('[data-resolution-apply][aria-busy="true"]'));
      assert.equal(await page.locator('.operation-feedback:visible,.operation-inline:visible').count(),0);
      assert.equal(await apply.isDisabled(),true);
      assert.equal(await apply.getAttribute('aria-busy'),'true');
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      let filename=`saving-${viewport.width}.png`;
      await page.screenshot({path:path.join(output,filename)});report.screenshots.push(filename);
      report.stage=`remaining-group-${viewport.width}`;
      saveGate.release();
      await first.waitFor({state:'detached'});
      const remaining=page.locator('[data-review-record="qa-pump-2"]');
      await remaining.waitFor({state:'visible'});
      await page.waitForFunction(()=>document.querySelectorAll('[data-review-group-key]')[0]?.querySelector('[data-review-record]')?.dataset.reviewRecord==='qa-pump-2');
      assert.deepEqual(await page.locator('[data-review-record]').evaluateAll(nodes=>nodes.map(node=>node.dataset.reviewRecord)),['qa-pump-2','qa-pump-3','qa-other-1']);
      const box=await remaining.boundingBox();assert.ok(box && box.y>=90 && box.y<viewport.height/2,`remaining same-group mention should stay in the review viewport: ${JSON.stringify(box)}`);
      await page.waitForFunction(()=>!document.querySelector('[data-resolution-apply][aria-busy="true"]'));
      assert.equal(await remaining.locator('[data-review-existing]').isEnabled(),true);
      filename=`remaining-${viewport.width}.png`;
      await page.screenshot({path:path.join(output,filename)});report.screenshots.push(filename);
      await page.evaluate(()=>scrollTo(0,document.documentElement.scrollHeight));
      await page.locator('[data-review-record="qa-other-1"]').scrollIntoViewIfNeeded();
      assert.ok(await page.locator('[data-review-record="qa-other-1"]').isVisible());
      await remaining.scrollIntoViewIfNeeded();
      if(viewport.width===1920) {
        await page.evaluate(()=>document.documentElement.requestFullscreen());
        await remaining.scrollIntoViewIfNeeded();filename='remaining-fullscreen.png';
        await page.screenshot({path:path.join(output,filename)});report.screenshots.push(filename);
        await page.evaluate(()=>document.exitFullscreen());
        await page.emulateMedia({reducedMotion:'reduce'});holdRefresh=gate();gates.push(holdRefresh);
        await page.locator('#review-refresh-button').click();
        await page.locator('#review-refresh-button[aria-busy="true"]').waitFor({state:'visible'});
        assert.equal(await page.locator('#review-refresh-button').evaluate(node=>getComputedStyle(node,'::before').animationName),'none');
        holdRefresh.release();await page.waitForFunction(()=>!document.querySelector('#review-refresh-button').hasAttribute('aria-busy'));
        report.reducedMotion=true;
      }
      report.viewports.push({...viewport,stableOrder:true,sameGroupAnchor:true,pendingAndRecovery:true});
      await context.unrouteAll({behavior:'ignoreErrors'});
      await context.close();
    }
    assert.deepEqual(report.pageErrors,[]);assert.deepEqual(report.failedResponses,[]);assert.deepEqual(report.blockedRequests,[]);assert.deepEqual(report.routeErrors,[]);
    assert.equal(report.mockedKnowledgeWrites,3);report.status='passed';
  } catch(error) {
    report.status='failed';report.error=safeError(error);
    if(activePage && !activePage.isClosed()) {
      try {await activePage.screenshot({path:path.join(output,'failure.png'),timeout:5000});report.screenshots.push('failure.png');} catch(_) {}
    }
    throw error;
  }
  finally {
    closing=true;for(const item of gates)item.release();
    await Promise.allSettled(contexts.map(context=>context.unrouteAll({behavior:'ignoreErrors'})));
    await browser.close();await fs.writeFile(path.join(output,'results.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify({status:report.status,output,screenshots:report.screenshots,knowledgeWrites:report.knowledgeWrites,mockedKnowledgeWrites:report.mockedKnowledgeWrites}));
  }
})().catch(error=>{console.error(safeError(error));process.exitCode=1;});
