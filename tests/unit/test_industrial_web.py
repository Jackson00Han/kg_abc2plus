"""Executable industrial UI identity, evidence, graph, and static boundaries."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from graphrag_prod.playground.industrial_web import attach_industrial_web


STATIC = Path(__file__).parents[2] / "src/graphrag_prod/playground/static/industrial"


class IndustrialModuleTests(unittest.TestCase):
    def run_module(self, scenario: str, **fixture: object) -> None:
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required for executable industrial UI checks")
        harness = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const core = await import(input.core);
const model = await import(input.model);
const construction = await import(input.construction);
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => {resolve=yes;reject=no;});
  return {promise, resolve, reject};
};
const response = value => ({ok:true,status:200,json:async()=>value});
const session = (id, token=id+'-token', expires=Math.floor(Date.now()/1000)+600) =>
  ({identity:{id,scopes:['knowledge:graph:read']},access_token:token,expires_at:expires});
const flush = () => new Promise(resolve => setImmediate(resolve));
const watchdog = setTimeout(() => {console.error('Industrial UI scenario timed out');process.exit(1);},5000);
try {
  const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
  await new AsyncFunction('core','model','construction','input','assert','deferred','response','session','flush',input.scenario)(
    core,model,construction,input,assert,deferred,response,session,flush);
} finally {clearTimeout(watchdog);}
"""
        result = subprocess.run(
            [node, "--input-type=module", "-e", harness],
            input=json.dumps({"core": (STATIC / "core.mjs").as_uri(),
                             "model": (STATIC / "graph-model.mjs").as_uri(),
                             "construction": (STATIC / "construction.mjs").as_uri(),
                             "scenario": scenario, **fixture}, ensure_ascii=False),
            text=True, capture_output=True, timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def run_construction(self, scenario: str) -> None:
        """Use the real workbench and its DOM listeners with controllable I/O."""
        self.run_module(r"""
class TestNode {
  constructor(tag='div') {
    this.tagName=tag;this.children=[];this.listeners=new Map();this.value='';
    this.textContent='';this.disabled=false;this.checked=false;this.files=[];
    const classes=new Set();
    this.classList={add:key=>classes.add(key),remove:key=>classes.delete(key),
      toggle:(key,on)=>on?classes.add(key):classes.delete(key)};
  }
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=[...children];}
  setAttribute(){}
  addEventListener(name,handler){this.listeners.set(name,handler);}
  querySelectorAll(tag){return this.children.flatMap(node=>[
    ...(node.tagName===tag?[node]:[]),...node.querySelectorAll(tag)]);}
  click(){if(!this.disabled)return this.listeners.get('click')?.({preventDefault(){}});}
}
const nodes=new Map();
globalThis.document={
  createElement:tag=>new TestNode(tag),
  createTextNode:text=>Object.assign(new TestNode('#text'),{textContent:text}),
  getElementById:id=>{if(!nodes.has(id))nodes.set(id,new TestNode());return nodes.get(id);},
  querySelectorAll:()=>[],
};
globalThis.Option=class extends TestNode {
  constructor(text,value){super('option');this.textContent=text;this.value=value;}
};
const stored=new Map();
globalThis.sessionStorage={getItem:key=>stored.get(key)??null,setItem:(key,value)=>stored.set(key,value)};
// Notification timers must not keep an otherwise finished Node scenario alive.
const nativeTimeout=globalThis.setTimeout;
globalThis.setTimeout=(...args)=>{const timer=nativeTimeout(...args);timer.unref();return timer;};
const makeRecord=id=>({
  record_kind:'ENTITY_MENTION',record_id:id,revision:1,revision_id:id+'-revision',
  entity:{entity_id:id+'-entity',entity_type:'Component',canonical_name:'合成接头 '+id},
  trust:{authority:'SECONDARY',origin:'LLM_EXTRACTED',status:'CANDIDATE'},confidence:0.9,
  evidence:{quoted_text:'合成接头待独立复核',char_start:0,char_end:10,chunk_id:id+'-chunk'},
});
const clickLabel=(card,label)=>{
  const target=card.querySelectorAll('button').find(node=>node.textContent===label);
  assert.ok(target,'Expected action '+label);return target.click();
};
const client={epoch:1,session:{identity:{id:'engineer',groups:['engineering']}},hasScope:()=>true};
""" + scenario)

    def run_upload(self, scenario: str) -> None:
        self.run_construction(r"""
const calls=[],outcomes=[];
const chunk=(status,mentions=[],assertions=[])=>({status,mention_record_ids:mentions,assertion_record_ids:assertions});
client.request=async(path,body)=>{
  calls.push({path,body});
  if(path.startsWith('/v1/knowledge/construction-jobs'))return {items:[]};
  assert.equal(path,'/v1/knowledge:construct');
  const value=outcomes.shift();if(value instanceof Error)throw value;return value;
};
const ui=new construction.ConstructionWorkbench(client);
const bytes=new TextEncoder().encode('合成登记：母线 QA-01 位于演示站点。');
nodes.get('upload-file').files=[{name:'inspection.txt',size:bytes.length,arrayBuffer:async()=>bytes.buffer}];
document.getElementById('upload-title').value='合成检查记录';
document.getElementById('upload-family').value='canalis-kt';
document.getElementById('upload-mode').value='LLM';
document.getElementById('upload-group').value='engineering';
const submit=()=>ui.upload({preventDefault(){}});
const writes=()=>calls.filter(call=>call.body);
const flatten=node=>[node.textContent,...node.children.map(flatten)].join(' ');
""" + scenario)

    def test_upload_zero_candidates_distinguishes_rejection_service_empty_and_source_only(self) -> None:
        self.run_upload(r"""
for(const [status,mode,expected] of [
  ['REJECTED','LLM','抽取校验未通过'],
  ['PROVIDER_ERROR','LLM','抽取服务失败'],
  ['EMPTY','LLM','未提取到可用事实'],
  ['SOURCE_ONLY','SOURCE_ONLY','未执行抽取'],
]) {
  nodes.get('upload-mode').value=mode;
  outcomes.push({job_id:'retained-'+status,extraction_mode:mode,chunks:[chunk(status)]});
  const count=writes().length;await submit();await flush();
  assert.equal(writes().length,count+1,'Terminal results must not trigger automatic retries');
  for(const node of [nodes.get('upload-status'),nodes.get('toast')]) {
    assert.ok(node.textContent.includes('来源'));
    assert.ok(node.textContent.includes('已入库'));
    assert.ok(node.textContent.includes(expected));
    assert.ok(node.textContent.includes('0 条候选记录'));
    assert.ok(!node.textContent.includes('复核后发布'));
    assert.ok(!node.textContent.includes('批准后才能发布'));
  }
  assert.ok(nodes.get('upload-status').textContent.includes('已留存'));
  assert.ok(nodes.get('upload-status').textContent.includes('复用本次操作'));
  assert.equal(ui.busy,false);
}
""")

    def test_upload_mixed_candidates_reports_partial_rejection_and_actual_record_count(self) -> None:
        self.run_upload(r"""
outcomes.push({job_id:'mixed',extraction_mode:'LLM',chunks:[
  chunk('CANDIDATE',['mention-one'],['assertion-one']),
  chunk('CANDIDATE',['mention-one'],[]),chunk('REJECTED'),
]});
await submit();
const text=nodes.get('upload-status').textContent;
assert.ok(text.includes('已生成 2 条待复核候选记录'));
assert.ok(text.includes('抽取校验未通过 1 个片段'));
assert.ok(text.includes('独立复核，批准后才能发布'));
assert.ok(!text.includes('已发布'));
assert.equal(writes().length,1);
""")

    def test_upload_quarantined_records_do_not_become_publishable_candidates(self) -> None:
        self.run_upload(r"""
outcomes.push({job_id:'quarantined',extraction_mode:'LLM',chunks:[chunk('QUARANTINED',['isolated-one'])]});
await submit();
const text=nodes.get('upload-status').textContent;
assert.ok(text.includes('0 条候选记录；1 条隔离记录'));
assert.ok(text.includes('需核对来源与校验问题'));
assert.ok(!text.includes('批准后才能发布'));
""")

    def test_upload_unknown_network_and_terminal_rejection_preserve_exact_operation(self) -> None:
        self.run_upload(r"""
outcomes.push(new Error('网络响应中断'));
await submit();
assert.equal(writes().length,1);
assert.ok(!nodes.get('upload-status').textContent.includes('来源资料已入库'));
assert.ok(nodes.get('upload-status').textContent.includes('查看任务记录'));
const original=writes()[0].body;
for(let i=0;i<2;i++) {
  outcomes.push({job_id:'same-failed-job',extraction_mode:'LLM',chunks:[chunk('REJECTED')]});
  await submit();
  assert.deepEqual(writes().at(-1).body,original);
}
assert.equal(writes().length,3,'Only explicit submissions may write');
assert.equal(stored.size,1);
""")

    def test_completed_job_list_shows_rejected_extraction_as_zero_candidates(self) -> None:
        self.run_construction(r"""
client.request=async()=>({items:[{job_id:'failed-extraction',status:'COMPLETED',
  created_at:'2026-09-06T18:12:41Z',expected_chunks:1,completed_chunks:1,extraction_mode:'LLM',
  chunks:[{status:'REJECTED',mention_record_ids:[],assertion_record_ids:[]}]}]});
const ui=new construction.ConstructionWorkbench(client);await ui.loadJobs();
const flatten=node=>[node.textContent,...node.children.map(flatten)].join(' ');
const text=flatten(nodes.get('construction-jobs'));
assert.ok(text.includes('处理结束 · 1/1 片段'));
assert.ok(text.includes('来源已入库；抽取校验未通过，0 条候选记录'));
assert.ok(!text.includes('批准后才能发布'));
""")

    def test_review_refresh_during_resolution_releases_lock_for_next_card(self) -> None:
        self.run_construction(r"""
const pending=[],record=makeRecord('one');
client.request=(path)=>{
  if(path.startsWith('/v1/knowledge/review-queue'))return Promise.resolve({items:[record]});
  const wait=deferred();pending.push(wait);return wait.promise;
};
const ui=new construction.ConstructionWorkbench(client);
await ui.loadReviews();
const oldCard=nodes.get('review-list').children[0];
const oldAction=clickLabel(oldCard,'核对已有实体');
assert.equal(ui.busy,true);
await ui.loadReviews();
const newCard=nodes.get('review-list').children[0];
assert.notEqual(oldCard,newCard);
pending[0].resolve({suggestions:[]});await oldAction;
assert.equal(ui.busy,false);
const newAction=clickLabel(newCard,'核对已有实体');
assert.equal(pending.length,2);assert.equal(ui.busy,true);
pending[1].resolve({suggestions:[]});await newAction;
assert.equal(ui.busy,false);
""")

    def test_old_review_completion_cannot_unlock_new_identity_operation(self) -> None:
        self.run_construction(r"""
const pending=[];
client.request=()=>{const wait=deferred();pending.push(wait);return wait.promise;};
const ui=new construction.ConstructionWorkbench(client);
const oldCard=ui.reviewCard(makeRecord('old'),client.epoch,ui.reviewEpoch);
const oldAction=clickLabel(oldCard,'核对已有实体');
client.epoch++;ui.reset();
const newCard=ui.reviewCard(makeRecord('new'),client.epoch,ui.reviewEpoch);
const newAction=clickLabel(newCard,'核对已有实体');
pending[0].resolve({suggestions:[]});await oldAction;
assert.equal(ui.busy,true);
await clickLabel(oldCard,'批准为当前实体');
assert.equal(pending.length,2);
pending[1].resolve({suggestions:[]});await newAction;
assert.equal(ui.busy,false);
""")

    def test_review_decision_keeps_lock_through_queue_refresh_then_releases(self) -> None:
        self.run_construction(r"""
const refreshed=deferred(),writes=[];
client.request=(path,body)=>{
  if(path.startsWith('/v1/knowledge/review-queue'))return refreshed.promise;
  writes.push({path,body});return Promise.resolve({});
};
const ui=new construction.ConstructionWorkbench(client);
const card=ui.reviewCard(makeRecord('approved'),client.epoch,ui.reviewEpoch);
card.querySelectorAll('textarea')[0].value='已对照来源确认设备身份和适用范围。';
const action=clickLabel(card,'批准为当前实体');await flush();
assert.equal(ui.busy,true);assert.equal(writes.length,1);
assert.deepEqual(writes[0],{path:'/v1/knowledge/reviews:batch',body:{decisions:[{
  record_kind:'ENTITY_MENTION',record_id:'approved',expected_revision:1,
  decision:'APPROVED',notes:'已对照来源确认设备身份和适用范围。'
}]}});
refreshed.resolve({items:[]});await action;
assert.equal(ui.busy,false);
""")

    def test_successful_publication_survives_graph_refresh_failure_without_resubmit(self) -> None:
        self.run_construction(r"""
const calls=[],graphCalls=[];
const chosen=makeRecord('chosen'),unselected=makeRecord('unselected');
const relation={...makeRecord('relation'),entity:undefined,subject:chosen.entity,
  object_entity:{entity_id:'object-entity',canonical_name:'合成母线段'},predicate:'PART_OF'};
client.request=async(path,body)=>{
  calls.push({path,body});
  if(path==='/v1/knowledge/publications:publish')return {publication_id:'new-publication',generation:7};
  if(path.startsWith('/v1/knowledge/publication-candidates'))return {items:[]};
  if(path.startsWith('/v1/knowledge/publications?'))return {items:[{publication_id:'new-publication',generation:7,status:'ACTIVE'}]};
  throw new Error('Unexpected request '+path);
};
const ui=new construction.ConstructionWorkbench(client,{onPublished:async ids=>{
  graphCalls.push(ids);throw new Error('图谱读取超时');
}});
ui.candidates=[{record:chosen,requires_replacement:false},{record:unselected,requires_replacement:false},
  {record:relation,requires_replacement:true}];
ui.selected=new Set([chosen.revision_id,relation.revision_id]);
ui.activePublication={publication_id:'previous-publication',generation:6};
await ui.publish();
assert.deepEqual(calls[0],{path:'/v1/knowledge/publications:publish',body:{
  approved_revision_ids:['chosen-revision','relation-revision'],
  expected_active_publication_id:'previous-publication',remove_record_ids:[],replace_record_ids:['relation']
}});
assert.deepEqual(graphCalls,[['chosen-entity','object-entity']]);
assert.ok(nodes.get('publish-status').textContent.includes('已发布知识版本 7'));
assert.ok(nodes.get('publish-status').textContent.includes('图谱刷新未完成'));
assert.ok(!nodes.get('publish-status').textContent.includes('发布失败'));
assert.equal(ui.activePublication.publication_id,'new-publication');
assert.equal(ui.selected.size,0);assert.equal(nodes.get('publish-submit').disabled,true);
assert.equal(ui.busy,false);
await ui.publish();assert.equal(calls.filter(call=>call.body).length,1);
""")

    def test_late_publication_refresh_does_not_clear_or_unlock_new_identity(self) -> None:
        self.run_construction(r"""
const callback=deferred(),newReview=deferred(),calls=[];
client.request=(path)=>{
  calls.push(path);
  if(path==='/v1/knowledge/publications:publish')return Promise.resolve({generation:7});
  if(path.includes('/entity-resolution/'))return newReview.promise;
  throw new Error('Old publication must not fetch new identity lists');
};
const ui=new construction.ConstructionWorkbench(client,{onPublished:()=>callback.promise});
const selected=makeRecord('old');ui.candidates=[{record:selected}];ui.selected.add(selected.revision_id);
const publication=ui.publish();await flush();
client.epoch++;ui.reset();nodes.get('publish-status').textContent='新身份的状态';
const current=makeRecord('current');ui.candidates=[{record:current}];ui.selected.add(current.revision_id);
const card=ui.reviewCard(current,client.epoch,ui.reviewEpoch);
const review=clickLabel(card,'核对已有实体');
callback.resolve();await publication;
assert.equal(ui.busy,true);assert.deepEqual([...ui.selected],['current-revision']);
assert.equal(nodes.get('publish-status').textContent,'新身份的状态');
assert.equal(calls.length,2);
newReview.resolve({suggestions:[]});await review;assert.equal(ui.busy,false);
""")

    def test_upload_read_finishing_after_reset_cannot_unlock_new_review(self) -> None:
        self.run_construction(r"""
const bytes=deferred(),reviewResult=deferred(),calls=[];
client.request=(path)=>{calls.push(path);return reviewResult.promise;};
const ui=new construction.ConstructionWorkbench(client);
nodes.get('upload-file').files=[{name:'inspection.txt',size:12,arrayBuffer:()=>bytes.promise}];
document.getElementById('upload-title').value='合成检查记录';
document.getElementById('upload-asset').value='BKT-A01';
document.getElementById('upload-family').value='CANALIS_KT';
document.getElementById('upload-mode').value='SOURCE_ONLY';
document.getElementById('upload-group').value='engineering';
const upload=ui.upload({preventDefault(){}});assert.equal(ui.busy,true);
client.epoch++;ui.reset();nodes.get('upload-status').textContent='新身份';
const card=ui.reviewCard(makeRecord('new'),client.epoch,ui.reviewEpoch);
const review=clickLabel(card,'核对已有实体');
bytes.resolve(new TextEncoder().encode('合成检查记录').buffer);await upload;
assert.equal(ui.busy,true);assert.equal(calls.length,1);
assert.ok(calls[0].includes('/entity-resolution/'));
assert.equal(nodes.get('upload-status').textContent,'新身份');
reviewResult.resolve({suggestions:[]});await review;assert.equal(ui.busy,false);
""")

    def test_python_codepoint_offsets_highlight_emoji_chinese_and_literal_markup(self) -> None:
        text = "记录🙂前文。母线𠮷号温升⚡复查。<img src=x onerror=alert(1)>后文"
        quote = "母线𠮷号温升⚡复查"
        start = text.index(quote)
        self.run_module(r"""
const citation=input.citation, evidence=input.evidence;
assert.deepEqual(core.exactEvidenceParts(citation,evidence), input.expected);
class Node {
  constructor(tag, text=''){this.tagName=tag;this.textContent=text;this.children=[];}
  append(...children){this.children.push(...children);}
}
globalThis.document={createElement:tag=>new Node(tag),createTextNode:text=>new Node('#text',text)};
const highlighted=core.evidenceQuote(citation,evidence);
assert.equal(highlighted.tagName,'pre');
assert.equal(highlighted.children.length,3);
assert.equal(highlighted.children[1].tagName,'mark');
assert.equal(highlighted.children[1].textContent,evidence.quoted_text);
assert.equal(highlighted.children.map(node=>node.textContent).join(''),citation.chunk_text);
assert.equal(highlighted.children[2].tagName,'#text');
assert.ok(highlighted.children[2].textContent.includes('<img src=x onerror=alert(1)>'));
assert.equal(highlighted.children.filter(node=>node.tagName==='img').length,0);
""", citation={"chunk_text": text, "char_start": 73, "char_end": 73 + len(text)},
             evidence={"char_start": 73 + start, "char_end": 73 + start + len(quote), "quoted_text": quote},
             expected=[text[:start], quote, text[start + len(quote):]])

    def test_quote_tampering_and_out_of_range_offsets_are_rejected(self) -> None:
        self.run_module(r"""
const citation={chunk_text:'前🙂母线异常后',char_start:10,char_end:17};
const evidence={char_start:12,char_end:16,quoted_text:'母线异常'};
assert.deepEqual(core.exactEvidenceParts(citation,evidence),['前🙂','母线异常','后']);
for(const changed of [
  {...evidence,quoted_text:'已确认故障'}, {...evidence,char_start:11},
  {...evidence,char_start:9}, {...evidence,char_end:18},
  {...evidence,char_end:12}, {...evidence,char_start:12.5}
])assert.throws(()=>core.exactEvidenceParts(citation,changed));
""")

    def test_out_of_order_persona_sessions_cannot_replace_current_identity(self) -> None:
        self.run_module(r"""
const pending=[];
const client=new core.WorkbenchClient((path,options)=>{
  const wait=deferred();pending.push({path,options,...wait});return wait.promise;
});
const old=client.selectPersona('maintenance');
const oldRejected=assert.rejects(old,error=>error.name==='StaleResponse');
const newer=client.selectPersona('public');
assert.equal(client.session,null);
pending[1].resolve(response(session('public')));await newer;
pending[0].resolve(response(session('maintenance')));await oldRejected;
assert.equal(client.session.identity.id,'public');
assert.equal(client.session.access_token,'public-token');
assert.deepEqual(pending.map(item=>JSON.parse(item.options.body).persona_id),['maintenance','public']);
assert.equal(pending.every(item=>item.options.cache==='no-store'),true);
""")

    def test_session_json_arriving_after_identity_change_is_ignored(self) -> None:
        self.run_module(r"""
const oldBody=deferred();let calls=0;
const client=new core.WorkbenchClient(async()=>++calls===1
  ? {ok:true,status:200,json:()=>oldBody.promise} : response(session('public')));
const old=client.selectPersona('maintenance');
const oldRejected=assert.rejects(old,error=>error.name==='StaleResponse');
await flush();await client.selectPersona('public');
oldBody.resolve(session('maintenance'));await oldRejected;
assert.equal(client.session.identity.id,'public');
""")

    def test_expired_identity_renewal_never_sends_data_request_for_new_identity(self) -> None:
        self.run_module(r"""
const pending=[];
const client=new core.WorkbenchClient((path,options)=>{
  const wait=deferred();pending.push({path,options,...wait});return wait.promise;
});
const first=client.selectPersona('maintenance');
pending[0].resolve(response(session('maintenance','expired',Math.floor(Date.now()/1000)-1)));await first;
const protectedRead=client.request('/v1/knowledge/graph:query',{page_size:10});
const rejected=assert.rejects(protectedRead,error=>error.name==='StaleResponse');
assert.equal(pending.length,2);
const newer=client.selectPersona('public');
pending[2].resolve(response(session('public')));await newer;
pending[1].resolve(response(session('maintenance','old-renewed')));await rejected;
assert.equal(client.session.access_token,'public-token');
assert.equal(pending.filter(item=>item.path!=='/playground/session').length,0);
""")

    def test_late_authorized_read_payload_is_aborted_and_never_rendered(self) -> None:
        self.run_module(r"""
const pending=[], rendered=[];
const client=new core.WorkbenchClient((path,options)=>{
  const wait=deferred();pending.push({path,options,...wait});return wait.promise;
});
const first=client.selectPersona('maintenance');pending[0].resolve(response(session('maintenance')));await first;
const read=client.request('/v1/knowledge/graph:evidence',{revision_ids:['restricted-revision'],view_token:'old-view'})
  .then(value=>rendered.push(value));
const rejected=assert.rejects(read,error=>error.name==='StaleResponse');
const body=deferred();pending[1].resolve({ok:true,status:200,json:()=>body.promise});await flush();
assert.equal(pending[1].options.headers.Authorization,'Bearer maintenance-token');
const newer=client.selectPersona('public');
assert.equal(pending[1].options.signal.aborted,true);
pending[2].resolve(response(session('public')));await newer;
body.resolve({items:[{quoted_text:'protected maintenance observation'}]});await rejected;
assert.deepEqual(rendered,[]);
assert.equal(client.controllers.size,0);
assert.equal(client.session.identity.id,'public');
""")

    def test_concurrent_expiring_requests_share_one_session_renewal(self) -> None:
        self.run_module(r"""
const renewal=deferred(),requests=[];let sessions=0;
const client=new core.WorkbenchClient((path,options)=>{
  requests.push({path,options});
  if(path==='/playground/session')return ++sessions===1
    ? Promise.resolve(response(session('engineering','expired',Math.floor(Date.now()/1000)-1))) : renewal.promise;
  return Promise.resolve(response({ok:true}));
});
await client.selectPersona('engineering');
const one=client.request('/v1/knowledge/graph:query',{page_size:1});
const two=client.request('/v1/industrial/sources:query',{limit:1});
assert.equal(sessions,2);
assert.equal(requests.length,2);
renewal.resolve(response(session('engineering','renewed')));await Promise.all([one,two]);
assert.equal(sessions,2);
assert.equal(requests.slice(2).every(item=>item.options.headers.Authorization==='Bearer renewed'),true);
""")

    def test_graph_endpoints_parallel_fact_identity_and_budget_boundaries(self) -> None:
        self.run_module(r"""
const node=id=>({entity_id:id,entity_type:'InstalledAsset',label:id});
const edge=id=>({revision_id:id,record_id:id,source:'a',target:'b',predicate:'CONNECTS_TO',authority_level:'SECONDARY'});
const page=(nodes=[node('a'),node('b')],edges=[edge('first'),edge('second')],literals=[])=>
  ({nodes,edges,literals,view_token:'view',page:{returned_nodes:nodes.length,returned_edges:edges.length,returned_literals:literals.length,has_more:false,next_cursor:null}});
const original=page();const before=JSON.stringify(original);const graph=model.graphElements(original);
assert.equal(graph.filter(item=>item.data.assertion).length,2);
assert.notEqual(graph[2].data.id,graph[3].data.id);
assert.equal(JSON.stringify(original),before);
assert.throws(()=>model.graphElements(page([node('a')])));
assert.throws(()=>model.graphElements(page([node('a'),node('a')],[])));
assert.throws(()=>model.graphElements(page(undefined,[edge('duplicate'),edge('duplicate')])));
assert.throws(()=>model.graphElements(page(undefined,[edge('same')],[{revision_id:'same',subject:'a',value:'1'}])));
assert.throws(()=>model.graphElements(page(undefined,[],[{revision_id:'literal',subject:'missing',value:'1'}])));
const nodes=Array.from({length:150},(_,i)=>node(i===0?'a':i===1?'b':String(i)));
const edges=Array.from({length:200},(_,i)=>edge('edge-'+i));
assert.equal(model.graphElements(page(nodes,edges)).length,350);
assert.throws(()=>model.graphElements(page([...nodes,node('overflow')],edges)));
assert.throws(()=>model.graphElements(page(nodes,[...edges,edge('overflow')])));
assert.throws(()=>model.graphElements(page(nodes,[],Array.from({length:201},(_,i)=>({revision_id:'literal-'+i,subject:'a',value:'1'})))));
""")

    def test_schema_declarations_are_separate_from_evidenced_instances(self) -> None:
        self.run_module(r"""
const schema={entity_types:[{name:'InstalledAsset'},{name:'Component'}],relationship_types:[
  {name:'PART_OF',source_types:['Component'],target_types:['InstalledAsset']}
]};
const declared=model.ontologyElements(schema);
assert.equal(declared.length,3);
assert.ok(declared.every(item=>item.data.definition&&!item.data.entity&&!item.data.assertion));
assert.equal(declared[2].data.source,declared[1].data.id);
assert.equal(declared[2].data.target,declared[0].data.id);
const actual=model.graphElements({nodes:[{entity_id:'Component',entity_type:'Component',label:'合成接头'}],edges:[],literals:[],view_token:'view',page:{returned_nodes:1,returned_edges:0,returned_literals:0}});
assert.notEqual(actual[0].data.id,declared[1].data.id);
assert.ok(actual[0].data.entity&&!actual[0].data.definition);
assert.throws(()=>model.ontologyElements({entity_types:Array.from({length:65},(_,i)=>({name:'Type'+i}))}));
const types=Array.from({length:15},(_,i)=>({name:'Type'+i}));
assert.throws(()=>model.ontologyElements({entity_types:types,relationship_types:[
  {name:'Rel',source_types:types.map(t=>t.name),target_types:types.map(t=>t.name)}
]}));
""")


class IndustrialStaticRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        attach_industrial_web(app)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_page_and_local_modules_have_restrictive_script_and_connection_policy(self) -> None:
        for path, media_type in (("/industrial", "text/html"),
                                 ("/industrial/assets/core.mjs", "text/javascript"),
                                 ("/industrial/assets/knowledge/browser.mjs", "text/javascript"),
                                 ("/industrial/assets/knowledge/maintenance.mjs", "text/javascript"),
                                 ("/industrial/assets/knowledge/browser.css", "text/css"),
                                 ("/industrial/assets/vendor/cytoscape.min.js", "text/javascript"),
                                 ("/industrial/assets/vendor/sources.v1.json", "application/json")):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.headers["content-type"].startswith(media_type))
                directives = {part.split()[0]: part.split()[1:]
                              for part in response.headers["content-security-policy"].split(";") if part.strip()}
                self.assertEqual(directives["script-src"], ["'self'"])
                self.assertEqual(directives["connect-src"], ["'self'"])
                self.assertEqual(directives["object-src"], ["'none'"])
                self.assertEqual(directives["base-uri"], ["'none'"])
                self.assertEqual(directives["frame-ancestors"], ["'none'"])
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                self.assertEqual(response.headers["x-frame-options"], "DENY")

    def test_traversal_encoded_separators_and_unapproved_extensions_are_not_served(self) -> None:
        for suffix in ("%2e%2e/index.html", "%2e%2e%2findex.html", "%252e%252e%252findex.html",
                       "vendor/%2e%2e/%2e%2e/index.html", "vendor%5c..%5cindex.html",
                       "vendor//cytoscape.min.js", "core.mjs%00", "中文.js", "README.md",
                       "index.html", "vendor/cytoscape-dagre.min.js.map", "missing.js",
                       "knowledge/index.html", "knowledge/core.mjs", "knowledge/private.json",
                       "knowledge/extra/browser.mjs", "knowledge/%2e%2e/index.html"):
            with self.subTest(suffix=suffix):
                response = self.client.get("/industrial/assets/" + suffix)
                self.assertEqual(response.status_code, 404)
                self.assertNotIn("Permission is hereby granted", response.text)
                self.assertNotIn("WorkbenchClient", response.text)

    def test_served_vendor_files_match_pinned_manifest(self) -> None:
        response = self.client.get("/industrial/assets/vendor/sources.v1.json")
        self.assertEqual(response.status_code, 200)
        manifest = response.json()
        self.assertEqual(manifest["runtime_network_dependencies"], [])
        for entry in manifest["files"]:
            with self.subTest(path=entry["path"]):
                actual = self.client.get("/industrial/assets/vendor/" + entry["path"])
                self.assertEqual(actual.status_code, 200)
                self.assertEqual(len(actual.content), entry["bytes"])
                self.assertEqual(hashlib.sha256(actual.content).hexdigest(), entry["sha256"])


if __name__ == "__main__":
    unittest.main()
