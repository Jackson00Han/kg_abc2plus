"""Graph loading and export follow the current rendered view, using pump fixtures."""

from pathlib import Path
import subprocess
import unittest


APP = Path("src/graphrag_prod/playground/static/industrial/app.mjs")


class GraphResultStateTests(unittest.TestCase):
    def run_js(self, scenario):
        source = APP.read_text()
        functions = source[source.index("function updateStats("):source.index("function currentPersona(")]
        functions += source[source.index("async function loadGraph("):source.index("function detailField(")]
        functions += source[source.index('$("export-graph").addEventListener('):source.index('$("next-page").addEventListener(')]
        harness = r'''
import assert from 'node:assert/strict';
const downloads=[],exports=[],revoked=[],timers=[],pending=[],errors=[],loading=[];
class Node {
  constructor(text='') {this.textContent=text;this.children=[];this.hidden=false;this.style={};this.parentElement={hidden:false};this.listeners={};}
  append(...items){this.children.push(...items);}
  replaceChildren(...items){this.children=items;}
  addEventListener(name,callback){this.listeners[name]=callback;}
  click(){if(this.tag==='a')downloads.push({href:this.href,name:this.download});else return this.listeners.click?.();}
}
const nodes=new Map(['node-count','node-count-label','edge-count','edge-count-label','type-count',
  'type-legend','graph-key','graph-timing','next-page','canvas-caption','export-graph'].map(id=>[id,new Node()]));
const $=id=>{assert.ok(nodes.has(id),id);return nodes.get(id);};
const clear=node=>{node.replaceChildren();return node;};
const element=(tag,cls,text='')=>Object.assign(new Node(text),{tag,className:cls});
globalThis.document={querySelector:selector=>{assert.equal(selector,'.graph-key');return $('graph-key');},
  createTextNode:text=>new Node(text)};
globalThis.URL={createObjectURL:blob=>{exports.push(blob);return 'blob:pump-graph';},revokeObjectURL:url=>revoked.push(url)};
globalThis.setTimeout=callback=>timers.push(callback);
const TYPE_COLORS={},TYPE_LABELS={},VIEWS={all:{label:'实例图谱'}};
let view='all',page=null,pageQuery=null,graphEpoch=0,selectionEpoch=0,renderedNodes=0;
const graph={clear(){renderedNodes=0;},cy:{nodes:()=>({length:renderedNodes})},
  setPage(result,mode){renderedNodes=mode==='ontology'?result.schema.entity_types.length:result.nodes.length;
    return {nodes:renderedNodes,edges:result.edges.length};},exportPNG:()=>({type:'image/png'})};
const client={epoch:1,request:()=>new Promise((resolve,reject)=>pending.push({resolve,reject}))};
const resetInspector=()=>{},loadingGraph=(...args)=>loading.push(args),graphBody=()=>({}),
  syncGraphFilters=()=>false,selectedGraphFilter=()=>null,safeError=error=>error.message,
  message=text=>errors.push(text),toast=()=>{};
const pumpPage={nodes:[{entity_id:'pump',entity_type:'Equipment'},{entity_id:'seal',entity_type:'Component'}],
  edges:[{source:'pump',target:'seal',predicate:'CONTAINS'}],
  schema:{entity_types:[{name:'Equipment'},{name:'Component'}]},page:{has_more:false}};
'''
        result = subprocess.run(
            ["node", "--input-type=module", "-e", harness + functions + scenario],
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_loading_and_failure_remove_previous_counts_legend_and_timing(self):
        self.run_js(r'''
for(const id of ['node-count','edge-count','type-count'])$(id).textContent='99';
$('type-legend').append(new Node('旧类型'));$('graph-timing').textContent='50 ms';
const task=loadGraph();
for(const id of ['node-count','edge-count','type-count'])assert.equal($(id).textContent,'—');
assert.equal($('type-legend').children.length,0);assert.equal($('graph-timing').textContent,'');
pending[0].reject(new Error('图谱请求失败'));await task;
assert.equal(errors.at(-1),'图谱请求失败');
for(const id of ['node-count','edge-count','type-count'])assert.equal($(id).textContent,'—');
assert.equal($('type-legend').children.length,0);assert.equal(renderedNodes,0);
''')

    def test_ontology_statistics_hide_duplicate_type_count_and_instance_restores_it(self):
        self.run_js(r'''
page=pumpPage;view='ontology';updateStats({nodes:2,edges:1});
assert.equal($('node-count-label').textContent,'声明的节点类型');
assert.equal($('node-count').textContent,2);
assert.equal($('type-count').parentElement.hidden,true);
view='all';updateStats({nodes:2,edges:1});
assert.equal($('node-count-label').textContent,'当前可见节点');
assert.equal($('type-count').parentElement.hidden,false);
assert.equal($('type-count').textContent,2);
''')

    def test_ontology_export_works_without_instances_and_empty_canvas_does_not_export(self):
        self.run_js(r'''
page={...pumpPage,nodes:[],edges:[]};view='ontology';graph.setPage(page,view);
$('export-graph').click();
assert.deepEqual(downloads,[{href:'blob:pump-graph',name:'industrial-ontology.png'}]);
assert.equal(exports.length,1);timers.forEach(callback=>callback());
assert.deepEqual(revoked,['blob:pump-graph']);
graph.clear();$('export-graph').click();
assert.equal(downloads.length,1);assert.equal(exports.length,1);
''')
