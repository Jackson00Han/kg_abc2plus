/** Read-only browser acceptance using isolated auto-review response fixtures. */
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
const {chromium}=require('playwright');
const base='http://127.0.0.1:8002';
const output=path.resolve('.local/browser-qa/auto-review');
const readPosts=new Set(['/playground/session','/v1/knowledge/graph:query','/v1/knowledge/graph:evidence',
  '/v1/knowledge/sources:query','/v1/knowledge/sources:read','/v1/knowledge/publications:compare',
  '/v1/knowledge/quality/reviews:query','/v1/knowledge/review-evidence']);
const jobId='auto-review-visual-fixture';
const quote='项目 P 的设备 D01，唯一编号 asset-001。';
const mention=(id,status)=>({record_id:id,revision:1,revision_id:id+'-revision-1',record_kind:'ENTITY_MENTION',
  entity:{entity_id:id+'-entity',entity_type:'Device',canonical_name:id==='uncertain'?'设备 D01 · 所属项目待确认':'项目 P / 设备 D01',canonical_key:'fixture:'+id,aliases:[]},
  trust:{authority:'AUTHORITATIVE',origin:'AUTHORITATIVE_EXTRACTED',status},confidence:1,
  evidence:{chunk_id:'visual-fixture-chunk',char_start:0,char_end:quote.length,quoted_text:quote},
  created_at:'2026-09-13T12:00:00Z',reviewed_by:status==='APPROVED'?'auto-review-service':null});
const approved=mention('approved','APPROVED'),uncertain=mention('uncertain','CANDIDATE');
let manualQueue=[uncertain];
const contextQuote='"P"';
const contextRecord={record_id:'context-property',revision_id:'context-property-r1',revision:1,record_kind:'ASSERTION',
  subject:approved.entity,predicate:'project_id',literal_value:'P',trust:approved.trust,evidence:approved.evidence,
  context_property_evidence:{source_checksum:'a'.repeat(64),mapping_checksum:'b'.repeat(64),scope_pointer:'/metadata',
    collection_pointer:'/assets',record_pointer:'/assets/0',identity_pointer:'/assets/0/asset_ref',value_pointer:'/metadata/project_id',
    source_identity:'asset-001',property_name:'project_id',binding_json:'PRIVATE_BINDING_NOT_FOR_UI',
    value_evidence:{chunk_id:'context-value-chunk',char_start:0,char_end:contextQuote.length,quoted_text:contextQuote}}};
const automatic={job_id:jobId,run_id:'visual-review-run',status:'COMPLETED',stage:'DONE',
  policy_version:'evidence-auto-review:v2',initiated_by:'visual-fixture-user',reviewed_by:'auto-review-service',
  counts:{entity_groups:139,approved_groups:138,approved_mentions:308,approved_assertions:910,
    manual_groups:1,manual_assertions:2,blocked_assertions:3,incomplete:0},
  model_calls:2,updated_at:'2026-09-13T12:00:00Z',truncated:false,
  items:[{record_id:'approved',record_kind:'ENTITY_MENTION',decision:'AUTO_APPROVED',
    reason_code:'IDENTITY_CONFIRMED',reason:'来源记录标识、所属项目与原文一致；重复提及已绑定同一实体。',
    target_entity_id:approved.entity.entity_id,evidence_ids:['visual-fixture-evidence']},
    {record_id:'uncertain',input_revision:1,record_kind:'ENTITY_MENTION',decision:'NEEDS_HUMAN',reason_code:'IDENTITY_SCOPE_MISSING',
      reason:'两个项目中都存在设备 D01，当前原文未明确它属于哪个项目。',evidence_ids:['visual-fixture-evidence-2']} ]};
const job={job_id:jobId,operation_key:'visual-fixture-operation',document_id:'visual-fixture-document',
  version_id:'visual-fixture-version',status:'COMPLETED',extraction_mode:'LLM',completed_chunks:20,expected_chunks:20,
  updated_at:'2026-09-13T12:00:00Z',created_at:'2026-09-13T11:50:00Z',auto_review:automatic,
  chunks:[{chunk_id:'visual-fixture-chunk',artifact_id:'visual-fixture-artifact',status:'CANDIDATE',
    finding_codes:['STRUCTURED_MAPPING_APPLIED'],mention_record_ids:['approved','uncertain'],assertion_record_ids:[],validation_attempts:[]}]};
(async()=>{
  await fs.mkdir(output,{recursive:true});
  const report={status:'running',mode:'isolated response fixtures; repair POST fulfilled locally; no knowledge writes',fixture_repairs:0,screenshots:[],errors:[],blocked:[]};
  const browser=await chromium.launch({headless:true});
  try {
    const context=await browser.newContext({viewport:{width:1440,height:1000}});
    const bootstrap=await (await context.request.get(base+'/playground/bootstrap')).json();
    assert.equal(bootstrap.data_scope,'pump-only');
    await context.route('**/*',async route=>{
      const request=route.request(),url=new URL(request.url());
      if(url.origin===base && url.pathname===`/v1/knowledge/construction-jobs/${jobId}/auto-review:run` && request.method()==='POST') {
        assert.deepEqual(request.postDataJSON(),{retry:true});report.fixture_repairs++;
        automatic.status='RUNNING';automatic.stage='CONTEXT';
        automatic.context_mapping={status:'RUNNING',added_assertions:0,applied:0,uncertain:0,overridden:0,rules:[],issues:[]};
        await new Promise(resolve=>setTimeout(resolve,1800));
        automatic.status='COMPLETED';automatic.stage='DONE';automatic.issues=[];
        automatic.context_mapping={status:'COMPLETED',added_assertions:24,applied:24,uncertain:0,overridden:0,issues:[],
          rules:[{source_path:'/metadata/project_id',target_collection:'/assets',property_name:'project_id',scope_path:'/metadata',
            binding_mode:'scoped_identity_prefix',status:'APPLIED',applied:24,uncertain:0,overridden:0,reason:'项目限定标识与文档范围一致，已逐个核对资产。'}]};
        automatic.counts.approved_assertions=939;
        automatic.items.push({record_id:'context-property',record_kind:'ASSERTION',decision:'AUTO_APPROVED',
          reason_code:'CONTEXT_VERIFIED',reason:'实体原文与上下文字段原文均已核验。',evidence_ids:['entity-evidence','context-value-evidence']});
        return route.fulfill({status:200,contentType:'application/json',json:automatic});
      }
      if(url.origin!==base || !(['GET','HEAD'].includes(request.method()) || request.method()==='POST' && readPosts.has(url.pathname))){
        report.blocked.push({method:request.method(),path:url.pathname});return route.abort();
      }
      const respond=json=>route.fulfill({status:200,contentType:'application/json',json});
      if(url.pathname==='/v1/knowledge/construction-jobs')return respond({items:[job]});
      if(url.pathname===`/v1/knowledge/construction-jobs/${jobId}`)return respond(job);
      if(url.pathname===`/v1/knowledge/construction-jobs/${jobId}/auto-review`)return respond(automatic);
      if(url.pathname==='/v1/knowledge/review-queue')return respond({items:manualQueue});
      if(url.pathname==='/v1/knowledge/publication-candidates')return respond({items:[{record:approved,requires_replacement:false}]});
      if(url.pathname.startsWith('/v1/knowledge/entity-resolution/'))return respond({record_id:'uncertain',revision:1,
        identity_properties:[],suggestions:[{outcome:'CONFLICT',reason_code:'IDENTITY_SCOPE_MISSING',reason:'项目归属不明确',target:null,evidence:[]}],
        identity_actions:{independent:{allowed:false,note:'需要补充项目归属证据'}}});
      if(url.pathname==='/v1/knowledge/records/approved/revisions')return respond({items:[approved]});
      if(url.pathname==='/v1/knowledge/records/context-property/revisions')return respond({items:[contextRecord]});
      if(/^\/v1\/knowledge\/records\/gap-\d+\/revisions$/.test(url.pathname)) {
        const id=url.pathname.split('/')[4],record=mention(id,'APPROVED');
        record.entity.canonical_name=`项目 P / 资产 ${Number(id.slice(4))+1}`;
        return respond({items:[record]});
      }
      if(url.pathname==='/v1/knowledge/review-evidence'){
        const isContext=request.postDataJSON()?.evidence_role==='CONTEXT_VALUE',text=isContext?contextQuote:quote;
        return respond({document_title:isContext?'上下文字段原文夹具':'自动预审浏览器验证夹具',
          version_id:'visual-fixture-version',chunk_id:isContext?'context-value-chunk':'visual-fixture-chunk',char_start:0,char_end:text.length,
          context_start:0,context_end:text.length,text,source_uri:'urn:fixture:auto-review',view:'paragraph',
          document_accessible:false,has_previous:false,has_next:false});
      }
      await route.continue();
    });
    const page=await context.newPage();page.on('pageerror',error=>report.errors.push(error.message));
    await page.goto(base+'/industrial');
    const workspace=bootstrap.knowledge_bases.find(k=>k.name==='母线槽知识库') || bootstrap.knowledge_bases[0];assert.ok(workspace);
    await page.locator('#knowledge-base').selectOption(workspace.knowledge_base_id);
    await page.locator('[data-panel="build"]').click();await page.locator('#build-tab-instances').click();
    await page.locator('#construction-jobs-refresh-button').click();
    await page.locator('[data-construction-job-detail]').first().click();
    const receipt=page.locator('#construction-output');
    await receipt.getByText('自动预审完成',{exact:true}).waitFor();
    for(const width of [1280,1440,1920]) {
      await page.setViewportSize({width,height:width===1280?800:1080});
      await receipt.scrollIntoViewIfNeeded();
      const details=receipt.locator('.auto-review-details');
      if(await details.getAttribute('open')!==null)await details.locator(':scope > summary').click();
      let file=`receipt-${width}.png`;await page.screenshot({path:path.join(output,file)});report.screenshots.push(file);
      await details.locator(':scope > summary').click();
      assert.equal(await details.locator('[data-auto-review-item]').count(),2);
      await details.locator('[data-auto-review-evidence="approved"]').click();
      await details.getByText('查看文档原文',{exact:true}).click();
      await details.getByText('自动预审浏览器验证夹具',{exact:true}).waitFor();
      await details.locator('[data-auto-review-item]').last().scrollIntoViewIfNeeded();
      assert.equal(await details.locator('[data-auto-review-correct]').count(),1);
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      file=`evidence-${width}.png`;await page.screenshot({path:path.join(output,file)});report.screenshots.push(file);
      await page.locator('[data-step-target="step-review"]').click();
      await page.locator('#review-list [data-review-record="uncertain"]').waitFor();
      assert.equal(await page.locator('#review-list [data-review-record]').count(),1);
      assert.equal(await page.locator('#review-list [data-review-record="approved"]').count(),0);
      await page.locator('#review-list [data-review-record="uncertain"]').scrollIntoViewIfNeeded();
      assert.ok((await page.locator('#review-list').innerText()).includes('当前原文未明确它属于哪个项目'));
      file=`human-queue-${width}.png`;await page.screenshot({path:path.join(output,file)});report.screenshots.push(file);
      await page.locator('[data-step-target="source-upload-slot"]').click();
    }
    // All generated facts can pass while required-but-unmapped fields still block publication.
    manualQueue=[];
    automatic.counts={...automatic.counts,approved_groups:139,approved_mentions:310,approved_assertions:915,
      manual_groups:0,manual_assertions:0,blocked_assertions:0};
    automatic.issues=[{code:'PROPERTY_REQUIRED',property_name:'project_id',entity_count:24,
      entity_ids:Array.from({length:24},(_,i)=>'asset-'+i),record_ids:Array.from({length:24},(_,i)=>'gap-'+i),
      reason:'资产缺少本体要求的项目属性；文档元数据中的同名字段尚未确认适用范围。',source_paths:['/metadata/project_id']}];
    await page.locator('[data-construction-job-detail]').first().click();
    for(const width of [1280,1440,1920]) {
      await page.setViewportSize({width,height:width===1280?800:1080});
      await receipt.locator('[data-auto-review-gaps]').scrollIntoViewIfNeeded();
      await receipt.getByText('自动审核完成，发布前仍需处理映射缺口',{exact:true}).waitFor();
      assert.ok((await receipt.innerText()).includes('待补字段 / 映射缺口 1 项（影响24 个实体）'));
      let file=`mapping-gap-${width}.png`;await page.screenshot({path:path.join(output,file)});report.screenshots.push(file);
      const gap=receipt.locator('.auto-review-gap-details');
      if(await gap.getAttribute('open')===null)await gap.locator(':scope > summary').click();
      await gap.locator('[data-auto-review-evidence="gap-0"]').click();
      await gap.getByText('查看文档原文',{exact:true}).first().click();
      await gap.getByText('自动预审浏览器验证夹具',{exact:true}).waitFor();
      assert.equal(await gap.locator('[data-auto-review-correct]').count(),0);
      await gap.locator('[data-auto-review-item]').first().scrollIntoViewIfNeeded();
      file=`mapping-gap-source-${width}.png`;await page.screenshot({path:path.join(output,file)});report.screenshots.push(file);
      await page.locator('[data-step-target="step-review"]').click();await page.locator('#review-refresh-button').click();
      await page.getByText('当前没有待审核记录，仍有必填字段缺口。',{exact:true}).waitFor();
      assert.equal(await page.locator('#review-list [data-review-record]').count(),0);
      assert.equal(await page.locator('#review-list [data-review-next]').count(),0);
      await page.locator('#review-list').scrollIntoViewIfNeeded();
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      file=`mapping-gap-empty-queue-${width}.png`;await page.screenshot({path:path.join(output,file)});report.screenshots.push(file);
      await page.locator('[data-step-target="source-upload-slot"]').click();
    }
    // Repair the same job through the real UI, with its POST intercepted above.
    const repaired=page.waitForResponse(response=>new URL(response.url()).pathname.endsWith('/auto-review:run'));
    await receipt.getByRole('button',{name:'补全上下文并审核',exact:true}).click();
    await page.locator('#construction-progress').getByText('正在核对文档上下文与适用范围',{exact:true}).waitFor();
    await repaired;await page.waitForFunction(()=>document.getElementById('construction-progress').hidden);
    await receipt.getByText('文档上下文补全 · 已完成',{exact:true}).waitFor();
    assert.equal(await receipt.locator('[data-auto-review-gaps]').count(),0);
    assert.ok((await page.locator('#construction-next-button').innerText()).includes('查看已确认内容'));
    for(const width of [1280,1440,1920]){
      await page.setViewportSize({width,height:width===1280?800:1080});
      await receipt.locator('[data-context-mapping-status]').scrollIntoViewIfNeeded();
      const mapping=receipt.locator('.context-mapping-summary > details');
      if(await mapping.getAttribute('open')===null)await mapping.locator(':scope > summary').click();
      let file=`context-repaired-${width}.png`;await page.screenshot({path:path.join(output,file)});report.screenshots.push(file);
      const details=receipt.locator('.auto-review-details');if(await details.getAttribute('open')===null)await details.locator(':scope > summary').click();
      await details.locator('[data-auto-review-evidence="context-property"]').click();
      const proof=details.locator('.context-property-evidence');
      await proof.getByText('实体原文',{exact:true}).click();await proof.getByText('上下文字段原文',{exact:true}).click();
      await proof.getByText('上下文字段原文夹具',{exact:true}).waitFor();
      assert.equal(await proof.locator('mark').count(),2);
      assert.ok(!(await proof.innerText()).includes('PRIVATE_BINDING_NOT_FOR_UI'));
      await proof.scrollIntoViewIfNeeded();
      file=`context-dual-evidence-${width}.png`;await page.screenshot({path:path.join(output,file)});report.screenshots.push(file);
      await page.locator('[data-step-target="step-review"]').click();await page.locator('#review-refresh-button').click();
      assert.equal(await page.locator('#review-auto-summary [data-auto-review-gaps]').count(),0);
      assert.equal(await page.locator('#review-list .review-mapping-gap').count(),0);
      await page.locator('#review-list').scrollIntoViewIfNeeded();
      file=`context-empty-queue-${width}.png`;await page.screenshot({path:path.join(output,file)});report.screenshots.push(file);
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      await page.locator('[data-step-target="source-upload-slot"]').click();
    }
    assert.equal(report.fixture_repairs,1);
    assert.deepEqual(report.errors,[]);assert.deepEqual(report.blocked,[]);report.status='passed';
  } catch(error){report.status='failed';report.error=error.message;throw error;}
  finally{await fs.writeFile(path.join(output,'report.json'),JSON.stringify(report,null,2));await browser.close();console.log(JSON.stringify(report));}
})().catch(error=>{console.error(error.message);process.exitCode=1;});
