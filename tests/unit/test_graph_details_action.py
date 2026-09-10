"""Offline graph selection tests for the optional details navigation action."""

from pathlib import Path
import subprocess
import unittest


GRAPH_MODULE = Path(
    "src/graphrag_prod/playground/static/industrial/graph.mjs"
).resolve().as_uri()


class GraphDetailsActionTests(unittest.TestCase):
    def run_js(self, scenario):
        harness = r'''
import assert from 'node:assert/strict';
class Node {
  constructor() { this.children=[]; this.listeners={}; this.hidden=false; this.textContent=''; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children=nodes; }
  setAttribute(name,value) { this[name]=value; }
  addEventListener(name,callback) { this.listeners[name]=callback; }
  insertAdjacentElement(position,node) { this[position]=node; }
  click() { this.listeners.click?.(); }
}
globalThis.document={createElement:()=>new Node(),fullscreenEnabled:false};
globalThis.ResizeObserver=class { observe() {} disconnect() {} };
const collection={
  unselect(){return this;},removeClass(){return this;},addClass(){return this;},
  toggleClass(){return this;},remove(){return this;},nodes(){return {length:2};}
};
globalThis.cytoscape=()=>({
  on(){},batch(callback){callback();},edges(){return collection;},
  elements(){return collection;},zoom(){return 1;}
});
const pumpItem=(kind='node')=>({
  isNode:()=>kind==='node',select(){},closedNeighborhood:()=>collection,
  union:()=>collection,connectedNodes:()=>collection,
  data(key){
    const values={label:'循环水泵',displayLabel:'包含部件',
      ...(kind==='node'?{entity:{entity_id:'pump'}}:{assertion:{predicate:'CONTAINS'}})};
    return key?values[key]:values;
  }
});
'''
        script = f"import {{IndustrialGraph}} from {GRAPH_MODULE!r};\n" + harness + scenario
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_optional_action_does_not_appear_without_host_callback(self):
        self.run_js(r'''
const graph=new IndustrialGraph(new Node());
assert.equal(graph.detailsButton,undefined);
assert.ok(!graph.focusBar.children.some(node=>node.textContent==='查看详情 ↓'));
graph.select(pumpItem());
graph.clearFocus();
assert.equal(graph.focusItem,null);
''')

    def test_selection_reveals_action_and_only_explicit_click_navigates(self):
        self.run_js(r'''
const calls=[];
const graph=new IndustrialGraph(new Node(),{
  onSelect:selected=>calls.push(selected?.kind||'clear'),
  onViewDetails:()=>calls.push('view-details'),
});
assert.equal(graph.detailsButton.hidden,true);
assert.equal(graph.focusBar.hidden,true);
assert.ok(graph.focusBar.children.includes(graph.detailsButton));
graph.select(pumpItem());
assert.equal(graph.detailsButton.hidden,false);
assert.equal(graph.focusBar.hidden,false);
assert.deepEqual(calls,['node']);
graph.detailsButton.click();
assert.deepEqual(calls,['node','view-details']);
graph.select(pumpItem('edge'));
assert.equal(graph.detailsButton.hidden,false);
assert.match(graph.focusStatus.textContent,/来源与依据/);
assert.ok(!graph.focusStatus.textContent.includes('右侧'));
graph.detailsButton.click();
assert.deepEqual(calls,['node','view-details','edge','view-details']);
''')

    def test_clearing_selection_or_graph_hides_action_until_next_selection(self):
        self.run_js(r'''
const graph=new IndustrialGraph(new Node(),{onViewDetails:()=>{}});
graph.select(pumpItem());
graph.clearFocus();
assert.equal(graph.detailsButton.hidden,true);
assert.equal(graph.focusBar.hidden,true);
assert.equal(graph.focusItem,null);
graph.select(pumpItem('edge'));
assert.equal(graph.detailsButton.hidden,false);
graph.clear();
assert.equal(graph.detailsButton.hidden,true);
assert.equal(graph.focusItem,null);
graph.select(pumpItem());
assert.equal(graph.detailsButton.hidden,false);
graph.resetTools();
assert.equal(graph.detailsButton.hidden,true);
assert.equal(graph.focusBar.hidden,true);
''')
