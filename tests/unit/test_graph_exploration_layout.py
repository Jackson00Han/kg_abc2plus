"""Graph selection details stay below the canvas and respect stale-response fences."""
from pathlib import Path
import subprocess
import unittest

STATIC = Path('src/graphrag_prod/playground/static/industrial')


class GraphExplorationLayoutTests(unittest.TestCase):
    def run_js(self, scenario):
        source = (STATIC / 'app.mjs').read_text()
        functions = source[source.index('function resetInspector('):source.index('function loadingGraph(')]
        functions += source[source.index('function detailField('):source.index('function renderEvidence(')]
        functions += source[source.index('async function showLiteral('):source.index('async function loadSources(')]
        harness = r'''
import assert from 'node:assert/strict';
class Node {
  constructor(text='') { this.textContent=text; this.children=[]; this.hidden=false; this.open=false; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children=nodes; }
}
const nodes=new Map(['graph-inspector','inspector-summary','inspector-content'].map(id=>[id,new Node()]));
const $=id=>{assert.ok(nodes.has(id),id);return nodes.get(id);};
const clear=node=>{node.replaceChildren();return node;};
const element=(tag,cls,text='')=>new Node(text);
const button=(text,fn)=>Object.assign(new Node(text),{click:fn});
const tag=text=>new Node(text), authorityTag=tag, empty=tag;
const TYPE_LABELS={},PREDICATE_LABELS={};
let selectionEpoch=0,graphEpoch=1,page={view_token:'pump-view',pin:{},nodes:[],literals:[],schema:{}};
const pending=[];
const client={epoch:1,request(path,body){return new Promise(resolve=>pending.push({resolve,path,body}));}};
const samePin=()=>true,safeError=error=>error.message,renderEvidence=item=>new Node(item.text);
const deferred=()=>new Promise(resolve=>setImmediate(resolve));
const texts=node=>[node.textContent,...node.children.flatMap(texts)].join('\n');
const selected={kind:'node',entity:{entity_id:'pump',label:'循环水泵',entity_type:'Pump',mention_revision_ids:['pump-revision']}};
'''
        result = subprocess.run(['node', '--input-type=module', '-e', harness + functions + scenario],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_selection_opens_details_and_reset_prevents_late_evidence_reappearing(self):
        self.run_js(r'''
resetInspector();assert.equal($('graph-inspector').hidden,true);
const task=selectGraph(selected);
assert.equal($('graph-inspector').hidden,false);assert.equal($('graph-inspector').open,true);
assert.equal($('inspector-summary').textContent,'循环水泵 · 详情与来源');
assert.equal(pending[0].path,'/v1/knowledge/graph:evidence');
assert.deepEqual(pending[0].body,{view_token:'pump-view',revision_ids:['pump-revision']});
await selectGraph(null);
pending[0].resolve({pin:{},items:[{text:'late pump evidence'}]});await task;
assert.equal($('graph-inspector').hidden,true);assert.equal($('graph-inspector').open,false);
assert.equal($('inspector-content').children.length,0);
''')

    def test_collapsing_does_not_reopen_on_evidence_completion_and_new_selection_opens(self):
        self.run_js(r'''
const task=selectGraph(selected);$('graph-inspector').open=false;
pending[0].resolve({pin:{},items:[{text:'循环水泵原文'}]});await task;
assert.equal($('graph-inspector').open,false);
assert.ok(texts($('inspector-content')).includes('循环水泵原文'));
await selectGraph({kind:'edge',definition:{name:'CONTAINS',source_types:['Pump'],target_types:['Part']}});
assert.equal($('graph-inspector').open,true);
assert.equal($('inspector-summary').textContent,'CONTAINS · 详情与来源');
assert.ok(texts($('inspector-content')).includes('源类型'));
assert.ok(!texts($('inspector-content')).includes('循环水泵原文'));
''')

    def test_switch_to_schema_invalidates_pending_literal_response(self):
        self.run_js(r'''
const task=showLiteral({predicate:'pump_status',value:'测试状态',revision_id:'pump-revision'},page);
assert.equal($('inspector-summary').textContent,'pump_status · 详情与来源');
await selectGraph({kind:'node',definition:{name:'Pump'}});
pending[0].resolve({pin:{},items:[{text:'stale literal evidence'}]});await task;
assert.ok(!texts($('inspector-content')).includes('stale literal evidence'));
assert.equal($('inspector-summary').textContent,'Pump · 详情与来源');
''')

    def test_view_details_requires_selection_and_moves_focus_only_when_requested(self):
        self.run_js(r'''
const calls=[];
$('inspector-summary').scrollIntoView=options=>calls.push(['scroll',options]);
$('inspector-summary').focus=options=>calls.push(['focus',options]);
resetInspector();viewSelectedDetails();assert.equal(calls.length,0);
openInspector('循环水泵');assert.equal(calls.length,0);
$('graph-inspector').open=false;
viewSelectedDetails();
assert.equal($('graph-inspector').open,true);
assert.deepEqual(calls,[['scroll',{block:'start',behavior:'auto'}],['focus',{preventScroll:true}]]);
''')

    def test_served_markup_keeps_details_after_canvas_without_permanent_metadata(self):
        html = (STATIC / 'index.html').read_text()
        source = (STATIC / 'app.mjs').read_text()
        for text in ('id="graph-version"', 'heading-marker', 'stats-note', '按身份授权的局部视图', 'inspector-empty'):
            self.assertNotIn(text, html)
        self.assertNotIn('$("graph-version")', source)
        self.assertIn('<details id="graph-inspector" class="inspector" hidden>', html)
        self.assertLess(html.index('class="graph-footer"'), html.index('id="graph-inspector"'))
        self.assertIn('点选后在图谱下方查看详情与来源', html)
