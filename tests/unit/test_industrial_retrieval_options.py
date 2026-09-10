"""Executable bounds, defaults and evidence-only retrieval controls."""

import unittest

from graphrag_prod.api.contracts import RetrievalLimitsRequest
from tests.unit import test_industrial_web


class IndustrialRetrievalOptionsTests(unittest.TestCase):
    run_module = test_industrial_web.IndustrialModuleTests.run_module

    def run_options(self, scenario, **fixture):
        self.run_module(
            "const options = await import(input.retrieval_options);\n" + scenario,
            retrieval_options=(test_industrial_web.STATIC / "retrieval-options.mjs").as_uri(),
            defaults=RetrievalLimitsRequest().model_dump(),
            **fixture,
        )

    def run_dom(self, scenario):
        self.run_options(r"""
class TestNode {
  constructor(tag='div') {
    this.tagName=tag.toUpperCase();this.children=[];this.listeners=new Map();
    this.value='';this.checked=false;this.disabled=false;this.open=false;this.dataset={};
    this.textContent='';
  }
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=[...children];}
  addEventListener(name,listener){
    const listeners=this.listeners.get(name)||[];listeners.push(listener);this.listeners.set(name,listeners);
  }
  emit(name){for(const listener of this.listeners.get(name)||[])listener({target:this});}
  click(){if(!this.disabled)this.emit('click');}
}
globalThis.document={createElement:tag=>new TestNode(tag)};
const container=new TestNode();let changes=0;
const defaults={...input.defaults,top_k:7,adjacent_window:0,deduplicate_content:false};
const industrial={...input.defaults,top_k:4,anchor_k:2,candidate_limit:50};
const mounted=options.mountRetrievalOptions(container,{
  bootstrap:{defaults:{retrieval_limits:defaults},industrial:{retrieval_limits:industrial}},
  onChange(){changes++;},
});
const walk=node=>[node,...node.children.flatMap(walk)];
const control=name=>walk(container).find(node=>node.name===name);
const action=text=>walk(container).find(node=>node.tagName==='BUTTON'&&node.textContent===text);
""" + scenario)

    def test_default_omits_limits_so_explicit_industrial_scope_uses_server_budget(self):
        self.run_options(r"""
const result=options.parseRetrievalOptions();
assert.deepEqual(result,{include_graph:false,graph_trust_policy:'PUBLISHED_SECONDARY_INCLUSIVE'});
assert.equal(result.limits,undefined);assert.equal(result.version_filter,undefined);
assert.equal(result.generation_limits,undefined);assert.equal(result.question,undefined);
// Disabled drafts, including invalid numeric input, do not override service defaults.
assert.deepEqual(options.parseRetrievalOptions({customLimits:false,limits:{top_k:5000}}),result);
""")

    def test_every_numeric_field_matches_current_api_bounds(self):
        schema = RetrievalLimitsRequest.model_json_schema()["properties"]
        bounds = {
            key: {"min": value.get("minimum", min(value.get("enum", [0]))),
                  "max": value.get("maximum", max(value.get("enum", [0])))}
            for key, value in schema.items() if key != "deduplicate_content"
        }
        self.run_options(r"""
assert.deepEqual(options.RETRIEVAL_LIMIT_FIELDS.map(field=>field.key).sort(),Object.keys(input.bounds).sort());
for(const field of options.RETRIEVAL_LIMIT_FIELDS){
  assert.equal(field.min,input.bounds[field.key].min,field.key);
  assert.equal(field.max,input.bounds[field.key].max,field.key);
  for(const invalid of ['',null,undefined,true,NaN,Infinity,field.min-1,field.max+1]){
    assert.throws(()=>options.parseRetrievalOptions({customLimits:true,limits:{...input.defaults,[field.key]:invalid}}),
      Error,field.key+' rejects '+invalid);
  }
  if(field.step===1){
    assert.throws(()=>options.parseRetrievalOptions({customLimits:true,limits:{...input.defaults,[field.key]:field.min+0.5}}));
  }
}
const result=options.parseRetrievalOptions({customLimits:true,limits:input.defaults});
assert.deepEqual(result.limits,input.defaults);
const fractional=options.parseRetrievalOptions({customLimits:true,limits:{...input.defaults,
  adjacent_window:0,minimum_vector_score:0.123456,minimum_bm25_score:1.00001,deduplicate_content:false}});
assert.equal(fractional.limits.minimum_vector_score,0.123456);
assert.equal(fractional.limits.minimum_bm25_score,1.00001);
assert.equal(fractional.limits.adjacent_window,0);
assert.equal(fractional.limits.deduplicate_content,false);
""", bounds=bounds)

    def test_relational_budget_constraints_reject_before_network_request(self):
        self.run_options(r"""
for(const invalid of [
  {bm25_scan_k:19,bm25_recall_k:20},
  {seed_k:10,candidate_limit:9},
  {anchor_k:4,top_k:3},
  {deduplicate_content:'false'},
]){
  assert.throws(()=>options.parseRetrievalOptions({customLimits:true,limits:{...input.defaults,...invalid}}));
}
""")

    def test_version_ids_are_unique_bounded_and_preserve_cutoff_timezone(self):
        self.run_options(r"""
const result=options.parseRetrievalOptions({
  documentIds:'doc-a,doc-a， doc-b\ndoc-c',versionIds:'version-a version-a',
  publishedBefore:'2040-03-05T08:00:00+08:00',
});
assert.deepEqual(result.version_filter,{
  document_ids:['doc-a','doc-b','doc-c'],version_ids:['version-a'],published_at_or_before:'2040-03-05T00:00:00.000Z',
});
const ids=Array.from({length:100},(_,i)=>'doc-'+i).join(',');
assert.equal(options.parseRetrievalOptions({documentIds:ids}).version_filter.document_ids.length,100);
assert.throws(()=>options.parseRetrievalOptions({documentIds:ids,versionIds:'version-one'}),/合计/);
assert.throws(()=>options.parseRetrievalOptions({documentIds:ids+',extra'}),/100/);
assert.throws(()=>options.parseRetrievalOptions({versionIds:'v'.repeat(257)}),/256/);
assert.throws(()=>options.parseRetrievalOptions({documentIds:'invalid\u0000id'}),/控制字符/);
assert.throws(()=>options.parseRetrievalOptions({publishedBefore:'nonsense'}),/截止时间/);
""")

    def test_graph_policy_only_accepts_supported_values(self):
        self.run_options(r"""
assert.deepEqual(options.parseRetrievalOptions({includeGraph:true,graphTrustPolicy:'AUTHORITATIVE_ONLY'}),{
  include_graph:true,graph_trust_policy:'AUTHORITATIVE_ONLY',
});
assert.throws(()=>options.parseRetrievalOptions({graphTrustPolicy:'CANDIDATE_INCLUSIVE'}));
""")

    def test_mount_prefills_bootstrap_but_does_not_override_service_until_customized(self):
        self.run_dom(r"""
assert.equal(changes,0);
assert.equal(control('top_k').value,'7');assert.equal(control('adjacent_window').value,'0');
assert.equal(control('deduplicate_content').checked,false);
assert.equal(control('top_k').disabled,true);
assert.equal(mounted.requestOptions().limits,undefined);
control('customLimits').checked=true;control('customLimits').emit('change');
assert.equal(control('top_k').disabled,false);
assert.deepEqual(mounted.requestOptions().limits,defaults);
assert.equal(changes,1);
action('载入工业预算').click();
assert.deepEqual(mounted.requestOptions().limits,industrial);
assert.equal(changes,2);
action('载入通用预算').click();
assert.deepEqual(mounted.requestOptions().limits,defaults);
""")

    def test_graph_toggle_and_filter_edits_read_live_form_values_and_clear_results(self):
        self.run_dom(r"""
assert.equal(control('graphTrustPolicy').disabled,true);
control('includeGraph').checked=true;control('includeGraph').emit('change');
assert.equal(control('graphTrustPolicy').disabled,false);
control('graphTrustPolicy').value='AUTHORITATIVE_ONLY';control('graphTrustPolicy').emit('change');
control('documentIds').value='doc-one';control('documentIds').emit('input');
control('versionIds').value='version-one';control('versionIds').emit('input');
control('publishedBefore').value='2040-03-05T08:00';control('publishedBefore').emit('input');
assert.equal(changes,5);
const result=mounted.requestOptions();
assert.equal(result.include_graph,true);assert.equal(result.graph_trust_policy,'AUTHORITATIVE_ONLY');
assert.deepEqual(result.version_filter.document_ids,['doc-one']);
assert.deepEqual(result.version_filter.version_ids,['version-one']);
assert.equal(result.version_filter.published_at_or_before,new Date('2040-03-05T08:00').toISOString());
const host=new TestNode();mounted.renderDiagnostics({trace:{private:'old result'}},host);
control('documentIds').value='doc-two';control('documentIds').emit('input');
assert.equal(host.children.length,0);
assert.deepEqual(result.version_filter.document_ids,['doc-one']);
assert.deepEqual(mounted.requestOptions().version_filter.document_ids,['doc-two']);
""")

    def test_diagnostics_are_collapsed_text_and_reset_clears_sensitive_content(self):
        self.run_dom(r"""
const host=new TestNode();
const result={trace:{reason:'<img src=x onerror=alert(1)>'},chunks:[{text:'protected original context'}],graph:{nodes:[]}};
mounted.renderDiagnostics(result,host);
const details=walk(host).filter(node=>node.tagName==='DETAILS');
assert.ok(details.length>=3);assert.ok(details.every(node=>!node.open));
const pre=walk(host).filter(node=>node.tagName==='PRE');
assert.equal(pre.length,1);
assert.equal(pre[0].textContent,JSON.stringify(result,null,2));
assert.ok(pre.every(node=>node.children.length===0));
control('documentIds').value='private-document';control('versionIds').value='private-version';
control('includeGraph').checked=true;action('载入工业预算').click();
mounted.renderDiagnostics(result,host);
const before=changes;mounted.reset();
assert.equal(changes,before+1);assert.equal(host.children.length,0);
assert.equal(control('documentIds').value,'');assert.equal(control('versionIds').value,'');
assert.equal(control('includeGraph').checked,false);assert.equal(control('graphTrustPolicy').disabled,true);
assert.equal(control('customLimits').checked,false);assert.equal(control('top_k').disabled,true);
assert.equal(control('top_k').value,'7');assert.equal(mounted.requestOptions().limits,undefined);
""")

    def test_reranking_status_distinguishes_executed_skipped_and_missing(self):
        self.run_options(r"""
assert.equal(options.rerankingLabel(), '排序信息未返回');
assert.match(options.rerankingLabel({reranking:null}), /未启用/);
assert.match(options.rerankingLabel({reranking:{status:'SKIPPED_EMPTY'}}), /已跳过/);
assert.equal(options.rerankingLabel({reranking:{status:'RERANKED'}}), '已完成模型重排');
assert.equal(options.rerankingLabel({reranking:{status:'unexpected'}}), '重排状态未知');
""")

    def test_scores_use_correct_stage_and_preserve_scoreless_listwise_ranking(self):
        self.run_dom(r"""
const host=new TestNode();
const hit=(id,rank,score)=>({chunk_id:id,rank,score});
const result={chunks:[
  {citation:{chunk_id:'pump-anchor',document_title:'循环水泵测试'},role:'anchor'},
  {citation:{chunk_id:'pump-adjacent',document_title:'<img src=x onerror=alert(1)>'},role:'adjacent'},
],trace:{
  vector_recall:[hit('pump-anchor',1,0.9)],candidate_vector_ranking:[hit('pump-anchor',2,0.8)],
  bm25_recall:[hit('pump-anchor',1,2.5)],final_ranking:[hit('pump-anchor',1,0.032522)],
  graph_expansion:[hit('pump-anchor',1,0.5),hit('pump-anchor',2,0.4)],
  context_chars:500,limits:input.defaults,decisions:[],
  reranking:{status:'RERANKED',ranked_chunk_ids:['pump-anchor'],response:{
    model:'fixture-listwise',candidate_count:1,scores:[{chunk_id:'pump-anchor',index:0,score:null}],
  }},
}};
const original=JSON.stringify(result);mounted.renderDiagnostics(result,host);
const rows=walk(host).filter(node=>node.tagName==='TR').slice(1);
assert.match(rows[0].children[0].textContent,/1 · 核心片段/);
assert.equal(rows[0].children[1].textContent,'0.800000 · 第 2 名');
assert.equal(rows[0].children[2].textContent,'2.500000 · 第 1 名');
assert.equal(rows[0].children[3].textContent,'0.032522');
assert.equal(rows[0].children[4].textContent,'第 1 名 · —');
assert.match(rows[1].children[0].textContent,/相邻补全/);
assert.equal(rows[1].children[0].children.length,0);
assert.ok(rows[1].children.slice(1).every(node=>node.textContent==='—'));
assert.ok(walk(host).some(node=>node.textContent.includes('Resource Allocation · 1 个片段')));
assert.equal(JSON.stringify(result),original);
""")

    def test_limits_visible_outside_details_and_rejection_counts_are_deduplicated(self):
        self.run_dom(r"""
const host=new TestNode();mounted.renderDiagnostics({chunks:[],trace:{decisions:[
  {chunk_id:'pump-a',decision:'rejected',reason:'character_budget'},
  {chunk_id:'pump-a',decision:'rejected',reason:'character_budget'},
  {chunk_id:'pump-b',decision:'rejected',reason:'duplicate_content'},
],limits:input.defaults,reranking:{status:'SKIPPED_EMPTY'}}},host);
assert.equal(host.children[0].tagName,'P');
assert.match(host.children[0].textContent,/不代表知识库内的全部相关证据/);
assert.ok(walk(host).some(node=>node.textContent==='上下文字符预算不足：1 个片段'));
assert.ok(walk(host).some(node=>node.textContent.includes('已跳过模型重排')));
assert.ok(!walk(host).some(node=>node.textContent.includes('已完成模型重排')));
mounted.reset();assert.equal(host.children.length,0);
""")

    def test_missing_trace_does_not_claim_engines_executed_or_zero_scores(self):
        self.run_dom(r"""
const host=new TestNode();mounted.renderDiagnostics({chunks:[]},host);
assert.ok(walk(host).some(node=>node.textContent.includes('无法确认引擎执行情况')));
assert.equal(walk(host).filter(node=>node.tagName==='TABLE').length,0);
assert.equal(walk(host).filter(node=>node.tagName==='PRE').length,1);
mounted.renderDiagnostics(null,host);assert.equal(host.children.length,0);
""")

    def test_retry_clears_old_diagnostics_before_request_and_failure(self):
        source = (test_industrial_web.STATIC / "app.mjs").read_text()
        search_function = source[source.index("async function search(event)"):source.index("async function copyContexts()")]
        self.run_options(r"""
const nodes=new Map(['question','search-submit','search-summary','search-results','search-diagnostics',
  'copy-contexts','search-family','search-asset','search-references'].map(id=>[id,{
    value:id==='question'?'循环水泵的检查依据':'',checked:false,children:['old result'],textContent:'',
    replaceChildren(){this.children=[];},append(value){this.children.push(value);},
    classList:{add(){},remove(){}},
  }]));
const $=id=>nodes.get(id);
const pending=deferred();let requests=0;
const client={epoch:1,request(){requests++;return pending.promise;}};
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
const run=new AsyncFunction('$','client','retrievalOptions','clear','empty','safeError','selectedIndustrialScope',
  'rerankingLabel','onStarted',
  'let searchBusy=false, searchEpoch=0, searchContexts=[], searchSource=null;'+input.search_function+
  '; const work=search({preventDefault(){}}); onStarted(); await work;');
await run($,client,{requestOptions:()=>({})},node=>node.replaceChildren(),text=>text,error=>error.message,()=>null,
  options.rerankingLabel,()=>{
    assert.equal(requests,1);
    assert.equal($('search-diagnostics').children.length,0);
    pending.reject(new Error('fixture retrieval unavailable'));
  });
assert.equal($('search-diagnostics').children.length,0);
assert.equal($('search-summary').textContent,'fixture retrieval unavailable');
assert.equal($('search-submit').disabled,false);
""", search_function=search_function)


if __name__ == "__main__":
    unittest.main()
