"""Executed business browser projections and version/identity cancellation contracts."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[2]
MODEL = (ROOT / 'src/graphrag_prod/playground/static/knowledge/model.mjs').as_uri()
BROWSER = (ROOT / 'src/graphrag_prod/playground/static/knowledge/browser.mjs').as_uri()


class KnowledgeBrowserTests(unittest.TestCase):
    def test_every_ordinary_and_industrial_persona_issues_a_valid_unique_scope_token(self):
        from graphrag_prod.api.auth import JWTAuthConfig, JWTAuthenticator
        from graphrag_prod.playground import PlaygroundCatalog, PLAYGROUND_ISSUER, PLAYGROUND_AUDIENCE
        from tests.fixtures.dev_corpus import load_dev_corpus_fixture
        key=b'local-browser-persona-token-regression-key-2026'
        catalog=PlaygroundCatalog(load_dev_corpus_fixture(),key,enable_industrial=True)
        authenticator=JWTAuthenticator(JWTAuthConfig(issuer=PLAYGROUND_ISSUER,audience=PLAYGROUND_AUDIENCE,secret=key))
        self.assertEqual(len(catalog.personas),11)
        for persona in catalog.personas:
            with self.subTest(persona=persona.persona_id):
                self.assertEqual(len(persona.scopes),len(set(persona.scopes)))
                identity=authenticator.verify_identity(catalog.issue_session(persona.persona_id)['access_token'])
                self.assertEqual(identity.principal.tenant_id,persona.tenant_id)
                self.assertEqual(identity.scopes,frozenset(persona.scopes))
                self.assertIn('knowledge:graph:read',identity.scopes)

    def test_browser_scope_refresh_clear_and_identity_invalidation(self):
        self.js(f"import {{mountBrowser}} from {BROWSER!r};" + """
const nodes=new Map();
class Element {
  constructor(){this.value='';this.hidden=false;this.children=new Map();this.open=false;}
  set innerHTML(value){this.html=value;this.children.clear();}get innerHTML(){return this.html;}
  querySelector(key){if(!this.children.has(key))this.children.set(key,new Element());return this.children.get(key);}
  querySelectorAll(){return [];}setAttribute(){}addEventListener(){}append(){}appendChild(){}
  replaceChildren(){this.html='';this.textContent='';}showModal(){this.open=true;}close(){this.open=false;}
}
globalThis.document={getElementById(id){if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);},createElement:()=>new Element(),body:new Element()};
document.getElementById('kb-graph').hidden=true;
let epoch=0,calls=[],defer=null;
const response={view_token:'v',pin:{publication_id:'pub',publication_generation:2},schema:{relationship_types:[]},nodes:[{entity_id:'pump',label:'循环泵',entity_type:'InstalledAsset',mention_revision_ids:['mention']}],edges:[],literals:[],page:{has_more:false}};
const browser=mountBrowser({api:async(url,options)=>{calls.push(JSON.parse(options.body));if(defer)return await new Promise(resolve=>defer=resolve);return response;},epoch:()=>epoch,maintain(){}});
await browser.load({document_ids:['doc'],version_ids:['version']},'设备手册');
assert.ok(document.getElementById('kb-scope').textContent.includes('设备手册'));
assert.equal(document.getElementById('kb-clear-scope').hidden,false);
await document.getElementById('kb-refresh').onclick();assert.deepEqual(calls[1].version_filter,{document_ids:['doc'],version_ids:['version']});
await document.getElementById('kb-clear-scope').onclick();assert.deepEqual(calls[2].version_filter,{});assert.equal(document.getElementById('kb-clear-scope').hidden,true);
browser.select('pump');assert.ok(document.getElementById('kb-dossier').innerHTML.includes('循环泵'));
defer=true;const pending=browser.load();epoch++;browser.reset();defer(response);await pending;
assert.equal(browser.getDirectory(),null);assert.equal(document.getElementById('kb-list').innerHTML,'');assert.ok(!document.getElementById('kb-dossier').textContent.includes('循环泵'));
""")

    def test_maintenance_dialog_cancels_stale_preview_and_rechecks_removal_selection(self):
        path=(ROOT / 'src/graphrag_prod/playground/static/knowledge/maintenance-actions.mjs').as_uri()
        # A stale displayed publication must not unapprove a newer review head.
        page=(ROOT / 'src/graphrag_prod/playground/static/index.html').as_uri()
        self.js(f"""
import vm from 'node:vm';import fs from 'node:fs';
const html=fs.readFileSync(new URL({page!r}),'utf8');
const source=html.slice(html.indexOf('async function correctPublishedRecord('),html.indexOf('function publicationSelection('));
const calls=[],state={{identityEpoch:0,reviewBusy:false,publicationBusy:false}};
const context=vm.createContext({{state,elements:{{reviewList:{{querySelectorAll:()=>[]}}}},setReviewBusy:value=>state.reviewBusy=value,apiRequest:async(url,options)=>{{calls.push({{url,options}});return {{items:[{{record_id:'fact',revision_id:'new-reviewed',trust:{{status:'APPROVED'}}}}]}};}}}});
vm.runInContext(source,context);
await assert.rejects(vm.runInContext("correctPublishedRecord('fact','old-published')",context),/已有更新的审核版本/);
assert.equal(calls.length,1);assert.equal(calls[0].options,undefined);assert.equal(state.reviewBusy,false);
""")
        self.js(f"import {{mountActions}} from {path!r};" + """
class Element {
  constructor(){this.children=new Map();this.open=false;this.dataset={};}
  set innerHTML(value){this.html=value;this.children.clear();}get innerHTML(){return this.html;}
  querySelector(key){if(!this.children.has(key))this.children.set(key,new Element());return this.children.get(key);}
  querySelectorAll(key){if(key==='[data-compare]'){const b=this.querySelector(key);b.dataset.compare='0';return [b];}return [];}
  setAttribute(){}addEventListener(){}replaceChildren(){this.children.clear();this.html='';}showModal(){this.open=true;}close(){this.open=false;}
}
const dialogs=[];globalThis.document={createElement:()=>{const d=new Element();dialogs.push(d);return d;},body:{append(){}}};
let epoch=0,commits=0,current=true;const calls=[];
const actions=mountActions({api:(url,options)=>new Promise(resolve=>calls.push({url,body:JSON.parse(options.body),resolve})),epoch:()=>epoch,browser:{},navigate(){},correctRecord(){},rollback(){throw new Error('must not roll back');},toast(){}});
actions.removals([{entity:{display_name:'Pump'}}],()=>commits++,()=>current);
const d=dialogs[0];current=false;d.querySelector('[data-stage]').onclick();assert.equal(commits,0);assert.ok(d.open);
current=true;d.querySelector('[data-stage]').onclick();assert.equal(commits,1);assert.equal(d.open,false);
const container=new Element();actions.history([{publication_id:'old',status:'HISTORICAL',generation:1,published_revision_ids:[]},{publication_id:'active',status:'ACTIVE',generation:2,published_revision_ids:[]}],container);
container.querySelector('[data-compare]').onclick();assert.equal(calls[0].body.expected_active_publication_id,'active');
actions.reset();epoch++;calls[0].resolve({added:[],removed:[],changed:[],unchanged_count:0});await new Promise(resolve=>setImmediate(resolve));
assert.equal(d.open,false);assert.equal(d.innerHTML,'');
""")

    def test_rollback_entry_requires_target_preview_and_explicit_confirmation(self):
        path=(ROOT / 'src/graphrag_prod/playground/static/knowledge/maintenance-actions.mjs').as_uri()
        self.js(f"import {{mountActions}} from {path!r};" + """
class Element {
  constructor(){this.children=new Map();this.value='';this.disabled=true;this.open=false;}
  set innerHTML(value){this.html=value;this.children.clear();}get innerHTML(){return this.html;}
  querySelector(key){if(!this.children.has(key))this.children.set(key,new Element());return this.children.get(key);}
  querySelectorAll(){return [];}setAttribute(){}addEventListener(){}
  replaceChildren(){this.children.clear();this.html='';}showModal(){this.open=true;}close(){this.open=false;}
}
const dialog=new Element();globalThis.document={createElement:()=>dialog,body:{append(){}}};
let epoch=0,comparisons=[],rollbacks=[],finishRollback,failComparison=false;
const actions=mountActions({epoch:()=>epoch,browser:{},toast(){},api:async(url,options)=>{
  comparisons.push({url,body:JSON.parse(options.body)});
  if(failComparison)throw {status:409};
  return {added:[],removed:[],changed:[],unchanged_count:2};
},rollback:(...args)=>{rollbacks.push(args);return new Promise((resolve,reject)=>{finishRollback={resolve,reject};});}});
const container=new Element(),active={publication_id:'current',status:'ACTIVE',generation:2,published_revision_ids:[],created_at:'2026-09-09T00:00:00Z'},old={...active,publication_id:'old',status:'SUPERSEDED',generation:1};
actions.history([],container);assert.match(container.innerHTML,/首次发布/);assert.match(container.innerHTML,/data-open-rollback disabled/);
await container.querySelector('[data-open-rollback]').onclick();assert.equal(comparisons.length,0);
actions.history([active],container);assert.match(container.innerHTML,/暂无历史版本可回滚/);
actions.history([old],container);assert.match(container.innerHTML,/没有可访问的生效版本/);
actions.history([active,old],container);
const select=container.querySelector('[data-rollback-target]'),button=container.querySelector('[data-open-rollback]');
select.value='current';select.onchange();assert.equal(button.disabled,true);
select.value='old';select.onchange();assert.equal(button.disabled,false);
await button.onclick();assert.deepEqual(comparisons[0],{url:'/v1/knowledge/publications:compare',body:{target_publication_id:'old',expected_active_publication_id:'current'}});
assert.equal(rollbacks.length,0);assert.match(dialog.querySelector('[data-comparison]').innerHTML,/确认回滚到第 1 版/);
const confirm=dialog.querySelector('[data-confirm]');confirm.disabled=false;
const pending=confirm.onclick();await confirm.onclick();assert.equal(rollbacks.length,1);assert.deepEqual(rollbacks[0],['old','current']);
finishRollback.resolve();await pending;assert.equal(dialog.open,false);
await button.onclick();const stale=dialog.querySelector('[data-confirm]');stale.disabled=false;epoch++;
await stale.onclick();assert.equal(rollbacks.length,1);
await button.onclick();const failing=dialog.querySelector('[data-confirm]');failing.disabled=false;
const failed=failing.onclick();finishRollback.reject({status:409});await failed;
assert.equal(failing.disabled,true);assert.match(dialog.querySelector('[data-status]').textContent,/请刷新后重试/);
await failing.onclick();assert.equal(rollbacks.length,2);
failComparison=true;await button.onclick();assert.equal(dialog.querySelector('[data-comparison]').innerHTML,undefined);
assert.equal(rollbacks.length,2);assert.match(dialog.querySelector('[data-status]').textContent,/请刷新后重试/);
""")

    def test_maintenance_comparison_preserves_raw_units_and_escapes_business_content(self):
        path=(ROOT / 'src/graphrag_prod/playground/static/knowledge/maintenance-actions.mjs').as_uri()
        self.js(f"import {{factTitle,comparisonMarkup}} from {path!r};" + """
const fact={subject_name:'Pump <script>',record_kind:'ASSERTION',predicate:'RatedPower',literal_value:'37.5',unit:'kW',document_title:'Manual'};
assert.equal(factTitle(fact),'Pump <script> · 额定功率 · 37.5 kW');
const html=comparisonMarkup({added:[fact],removed:[],changed:[{before:fact,after:{...fact,literal_value:'40'}}],unchanged_count:2});
assert.ok(html.includes('37.5 kW'));assert.ok(html.includes('40 kW'));assert.ok(html.includes('新增 1 条'));
assert.ok(!html.includes('<script>'));assert.ok(html.includes('&lt;script&gt;'));
""")

    def js(self, scenario):
        script = f"import assert from 'node:assert/strict'; import * as m from {MODEL!r}; import {{evidenceMarkup}} from {BROWSER!r};\n" + scenario
        result = subprocess.run(['node','--input-type=module','-e',script], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_entities_group_by_id_keep_parallel_facts_and_search_business_code(self):
        self.js("""
const nodes=[{entity_id:'a',label:'循环水泵',entity_type:'Equipment',mention_revision_ids:['m1','m2']},{entity_id:'b',label:'循环水泵',entity_type:'Equipment',mention_revision_ids:['m3']}];
const fact={subject:'a',revision_id:'r1',predicate:'EquipmentCode',value:'BC-P-101'};
const rows=m.dossiers([{view_token:'v',nodes,edges:[],literals:[fact,{...fact,revision_id:'r2'}]}]);
assert.equal(rows.length,2);assert.equal(rows.find(n=>n.entity_id==='a').properties.length,2);
assert.equal(m.entityPage(rows,{query:'BC-P-101'}).total,1);
assert.equal(m.entityPage(rows,{size:1,page:1}).items.length,1);
assert.equal(rows.find(n=>n.entity_id==='a').mention_revision_ids.length,2);
""")

    def test_collection_requires_every_page_and_rejects_changed_or_looping_cursor(self):
        self.js("""
const first={view_token:'v',nodes:[],edges:[],literals:[],pin:{},schema:{},page:{has_more:true,next_cursor:'next'}};
let calls=0;const api=async(url,options)=>{calls++; const body=JSON.parse(options.body);if(calls===2)assert.equal(body.cursor,'next');return calls===1?first:{...first,page:{has_more:false}};};
assert.equal((await m.readDirectory(api)).pages.length,2);
await assert.rejects(m.readDirectory(async()=>first),/分页无效/);
await assert.rejects(m.readDirectory(async()=>first,{},()=>false),/已取消/);
let n=0;await assert.rejects(m.readDirectory(async()=>++n===1?first:{...first,view_token:'changed'}),/版本发生变化/);
""")

    def test_evidence_uses_unicode_offsets_escapes_markup_and_rejects_wrong_quote(self):
        self.js("""
const item={evidence:{citation:{chunk_text:'😀<泵>功率',char_start:10,document_title:'<script>'},char_start:12,char_end:13,quoted_text:'泵'},origin:'AUTHORITATIVE_EXTRACTED'};
const html=evidenceMarkup(item);assert.ok(html.includes('<mark>泵</mark>'));assert.ok(html.includes('&lt;script&gt;'));assert.ok(!html.includes('<script>'));
item.evidence.quoted_text='错';assert.throws(()=>evidenceMarkup(item),/位置校验失败/);
""")

    def test_raw_values_temporal_conditions_and_units_are_not_conflated(self):
        self.js("""
assert.equal(m.valueLabel({value:'1',semantics:{raw_value:'1',raw_unit:'MPa',canonical_value:'1000000',canonical_unit:'Pa'}}),'1 MPa');
assert.equal(m.valueLabel({value:'37.5',semantics:{raw_value:'37.5',canonical_value:'37.5',canonical_unit:'kW'}}),'37.5 kW');
assert.equal(m.valueLabel({value:'1 MPa',semantics:{raw_value:'1 MPa',canonical_value:'1000000',canonical_unit:'Pa'}}),'1 MPa');
assert.ok(m.contextLabel({semantics:{observed_at:'2026-09-09'}}).includes('2026-09-09'));
assert.ok(m.contextLabel({semantics:{raw_observed_at:'2026年9月9日',observed_at:'2026-09-09'}}).includes('2026年9月9日'));
assert.equal(m.label('InstalledAsset'),'设备实例');assert.equal(m.label('MAY_INDICATE'),'可能关联');
""")

    def test_browser_assets_are_allowlisted_and_not_cached(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from graphrag_prod.playground import PlaygroundCatalog, attach_playground_routes
        from tests.fixtures.dev_corpus import load_dev_corpus_fixture
        app = FastAPI()
        attach_playground_routes(app, PlaygroundCatalog(load_dev_corpus_fixture(), b"local-browser-test-key-at-least-thirty-two-bytes"))
        with TestClient(app) as client:
            for name in ("browser.mjs", "model.mjs", "browser.css", "sources.mjs", "graph-view.mjs", "maintenance.mjs", "maintenance-actions.mjs"):
                response = client.get("/playground/assets/" + name)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(client.get("/playground/assets/.env").status_code, 404)
            self.assertEqual(client.get("/playground/assets/unknown.mjs").status_code, 404)

    def test_graph_loading_pins_next_page_and_cancels_stale_identity(self):
        path = (ROOT / 'src/graphrag_prod/playground/static/knowledge/graph-view.mjs').as_uri()
        self.js(f"import {{GraphSession}} from {path!r};" + """
let epoch=0,calls=[];const session=new GraphSession((url,options)=>new Promise(resolve=>calls.push({body:JSON.parse(options.body),resolve})),()=>epoch);
const query={seed_entity_ids:['pump'],hops:2,direction:'incoming'};
const first=session.read(query,{token:'v'});assert.equal(calls[0].body.view_token,'v');
calls[0].resolve({view_token:'v',page:{has_more:true,next_cursor:'c'}});await first;
const next=session.next();assert.equal(calls[1].body.cursor,'c');assert.deepEqual(calls[1].body.seed_entity_ids,['pump']);assert.equal(calls[1].body.hops,2);
epoch++;calls[1].resolve({view_token:'v',page:{has_more:false}});assert.equal(await next,null);assert.equal(session.page,null);
const old=session.read(query);session.clear();calls[2].resolve({view_token:'v',page:{has_more:false}});assert.equal(await old,null);
const changed=session.read(query,{token:'v'});calls[3].resolve({view_token:'other'});await assert.rejects(changed,/版本发生变化/);
""")

    def test_quality_display_names_explain_property_degree_and_keep_human_separate(self):
        path = (ROOT / 'src/graphrag_prod/playground/static/knowledge/maintenance.mjs').as_uri()
        self.js(f"import {{qualityMarkup}} from {path!r};" + """
const report={publication_generation:1,passed:true,total_issue_count:1,total_error_count:0,counts:{canonical_entities:3,literal_assertions:2,relationship_assertions:0},issues:[{issue_id:'issue',object_id:'entity',object_kind:'Entity',code:'ISOLATED_ENTITY',severity:'WARNING'}]};
const html=qualityMarkup(report,{labels:{entity:'北辰一号泵站'}});
assert.ok(html.includes('北辰一号泵站'));assert.ok(html.includes('未参与任何已发布属性或关系'));assert.ok(html.includes('自动规则通过不表示事实完整'));
assert.ok(!html.includes('确定性人工复核样本'));
const sampled=qualityMarkup({...report,review_sample:[{object_id:'entity',object_kind:'Entity',issue_codes:['ISOLATED_ENTITY'],evidence_chunk_ids:['chunk']} ]},{labels:{entity:'北辰一号泵站'}});
assert.ok(sampled.includes('建议人工核查'));assert.ok(sampled.includes('不代表整个知识库的准确率'));assert.ok(sampled.includes('data-quality-sample="0"'));assert.ok(sampled.includes('核查：尚无属性或关系的实体'));
const history=qualityMarkup(report,{historical:true,records:[{issue_id:'issue',decision:'NO_CHANGE_REQUIRED',recorded_by:'reviewer',recorded_at:'2026-09-09T00:00:00Z',notes:'<unsafe>'}]});
assert.ok(history.includes('已核查，无需修正'));assert.ok(history.includes('&lt;unsafe&gt;'));assert.ok(!history.includes('data-quality-review='));
""")
