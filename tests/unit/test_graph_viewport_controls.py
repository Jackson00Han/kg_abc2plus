"""Offline layout and fullscreen interaction contracts using a pump-only fixture."""

from pathlib import Path
import subprocess
import unittest


GRAPH_MODULE = Path(
    "src/graphrag_prod/playground/static/industrial/graph.mjs"
).resolve().as_uri()


class GraphViewportControlTests(unittest.TestCase):
    def run_js(self, scenario):
        harness = r'''
import assert from 'node:assert/strict';
class Node {
  constructor() {
    this.children=[];this.listeners=new Map();this.insertions=[];this.hidden=false;
    this.clientWidth=1000;this.clientHeight=565;this.textContent='';
  }
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;}
  setAttribute(name,value){this[name]=value;}
  addEventListener(name,callback){this.listeners.set(name,callback);}
  removeEventListener(name,callback){assert.equal(this.listeners.get(name),callback);this.listeners.delete(name);}
  insertAdjacentElement(position,node){this.insertions.push({position,node});}
  closest(){return this.parentElement;}
  click(){return this.listeners.get('click')?.();}
  remove(){this.removed=true;}
}
globalThis.document=new Node();
document.createElement=()=>new Node();document.fullscreenEnabled=false;document.fullscreenElement=null;
const frames=new Map();let nextFrame=0;
globalThis.requestAnimationFrame=callback=>{const id=++nextFrame;frames.set(id,callback);return id;};
globalThis.cancelAnimationFrame=id=>frames.delete(id);
const flushFrames=()=>{const pending=[...frames.values()];frames.clear();pending.forEach(callback=>callback());};
const changeFullscreen=element=>{
  document.fullscreenElement=element;document.listeners.get('fullscreenchange')?.();
};
document.exitFullscreen=async()=>changeFullscreen(null);
const workspace={requestFullscreen:async()=>changeFullscreen(workspace)};
globalThis.ResizeObserver=class {
  constructor(callback){this.callback=callback;}
  observe(){}disconnect(){this.disconnected=true;}
};
globalThis.cytoscape=options=>{
  let items=[];
  const cy={layouts:[],fitCalls:0,resizeCalls:0,on(){},batch(fn){fn();},
    add(elements){items.push(...elements);},nodes(){return items.filter(item=>!item.data.source);},
    edges(){return {length:items.filter(item=>item.data.source).length,toggleClass(){}};},
    elements(){return {remove(){items=[];}};},zoom(){return 1;},
    layout(options){return {run(){cy.layouts.push(options);}};},
    resize(){cy.resizeCalls++;},fit(items){cy.fitCalls++;cy.lastFitItems=items;cy.lastFitHeight=options.container.clientHeight;},
    center(){},destroy(){cy.destroyed=true;}
  };
  return cy;
};
const container=()=>Object.assign(new Node(),{parentElement:workspace});
const pumpPage={view_token:'pump-only-view',
  nodes:[{entity_id:'pump',entity_type:'Equipment',label:'循环水泵'},
    {entity_id:'seal',entity_type:'Component',label:'机械密封'}],
  edges:[{revision_id:'pump-contains-seal',source:'pump',target:'seal',predicate:'CONTAINS'}],
  literals:[],page:{returned_nodes:2,returned_edges:1,returned_literals:0},
  schema:{entity_types:[{name:'Equipment'},{name:'Component'}],
    relationship_types:[{name:'CONTAINS',source_types:['Equipment'],target_types:['Component']}]},
};
'''
        script = f"import {{IndustrialGraph}} from {GRAPH_MODULE!r};\n" + harness + scenario
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_default_layout_is_deterministic_and_manual_network_remains_available(self):
        self.run_js(r'''
const host=container();
const graph=new IndustrialGraph(host);
assert.deepEqual(graph.setPage(pumpPage),{nodes:2,edges:1});
assert.deepEqual(graph.cy.layouts.map(layout=>layout.name),['dagre']);
assert.equal(graph.cy.layouts[0].rankDir,'LR');
assert.equal(graph.cy.layouts[0].nodeDimensionsIncludeLabels,true);
assert.ok(graph.cy.layouts[0].rankSep>=90);
assert.equal(graph.cy.fitCalls,1);
assert.deepEqual(host.insertions.map(({position,node})=>[position,node.className]),[
  ['beforebegin','graph-local-tools'],['beforebegin','graph-focus-bar']]);
assert.equal(graph.focusBar.hidden,true);
graph.layoutButton.click();
assert.equal(graph.layoutMode,'network');
assert.deepEqual(graph.cy.layouts.slice(-2).map(layout=>layout.name),['dagre','cose']);
graph.setPage(pumpPage);
assert.equal(graph.layoutMode,'network');
graph.setPage(pumpPage,'ontology');
assert.equal(graph.layoutMode,'hierarchy');
assert.equal(graph.cy.layouts.at(-1).name,'dagre');
assert.equal(graph.labelsButton['aria-pressed'],'false');
graph.labelsButton.click();
graph.setPage(pumpPage,'ontology');
assert.equal(graph.labelsButton['aria-pressed'],'true');
graph.setPage(pumpPage,'all');
assert.equal(graph.labelsButton['aria-pressed'],'true');
''')

    def test_fullscreen_button_tracks_button_and_escape_exit_then_removes_listener(self):
        self.run_js(r'''
document.fullscreenEnabled=true;
const graph=new IndustrialGraph(container());
assert.equal(graph.fullscreenButton.textContent,'全屏');
assert.equal(graph.fullscreenButton['aria-pressed'],'false');
await graph.fullscreenButton.click();
assert.equal(document.fullscreenElement,workspace);
assert.equal(graph.fullscreenButton.textContent,'退出全屏');
assert.equal(graph.fullscreenButton['aria-pressed'],'true');
await graph.fullscreenButton.click();
assert.equal(document.fullscreenElement,null);
assert.equal(graph.fullscreenButton.textContent,'全屏');
await graph.fullscreenButton.click();
changeFullscreen(null); // Browser Esc exits independently of the button.
assert.equal(graph.fullscreenButton.textContent,'全屏');
assert.equal(graph.fullscreenButton['aria-pressed'],'false');
graph.destroy();
assert.equal(document.listeners.has('fullscreenchange'),false);
assert.equal(graph.resizeObserver.disconnected,true);
assert.equal(graph.cy.destroyed,true);
assert.equal(frames.size,0);
''')

    def test_fullscreen_resizes_fit_after_layout_but_normal_resizes_preserve_view(self):
        self.run_js(r'''
document.fullscreenEnabled=true;
const host=container(),graph=new IndustrialGraph(host);
graph.setPage(pumpPage);
const initialFits=graph.cy.fitCalls;
host.clientHeight=500;graph.resizeObserver.callback();
assert.equal(graph.cy.fitCalls,initialFits);
assert.equal(frames.size,0);
changeFullscreen({}); // Another workspace must not reset this graph's viewport.
assert.equal(frames.size,0);
changeFullscreen(workspace);
assert.equal(graph.cy.fitCalls,initialFits);
assert.equal(frames.size,1);
flushFrames();
assert.equal(graph.cy.fitCalls,initialFits+1);
host.clientHeight=320;graph.resizeObserver.callback();
host.clientHeight=260;graph.resizeObserver.callback();
assert.equal(frames.size,1); // Multiple size changes settle before one fit.
assert.equal(graph.cy.fitCalls,initialFits+1);
flushFrames();
assert.equal(graph.cy.fitCalls,initialFits+2);
assert.equal(graph.cy.lastFitHeight,260);
changeFullscreen(null);host.clientHeight=565;graph.resizeObserver.callback();
flushFrames();
assert.equal(graph.cy.fitCalls,initialFits+3);
assert.equal(graph.cy.lastFitHeight,565);
host.clientHeight=540;graph.resizeObserver.callback();
assert.equal(frames.size,0);
assert.equal(graph.cy.fitCalls,initialFits+3);
changeFullscreen(workspace);
assert.equal(frames.size,1);
graph.destroy();flushFrames();
assert.equal(graph.cy.fitCalls,initialFits+3);
assert.equal(frames.size,0);
''')

    def test_fullscreen_resize_preserves_explicit_neighborhood_focus(self):
        self.run_js(r'''
document.fullscreenEnabled=true;
const host=container(),graph=new IndustrialGraph(host);
graph.setPage(pumpPage);
changeFullscreen(workspace);flushFrames();
const neighborhood={name:'pump-and-seal'};
graph.focusItem={isNode:()=>true,closedNeighborhood:()=>neighborhood};
graph.fitFocus();
host.clientHeight=300;graph.resizeObserver.callback();flushFrames();
assert.equal(graph.cy.lastFitItems,neighborhood);
assert.equal(graph.cy.lastFitHeight,300);
// An explicit whole-canvas fit cancels the previous neighborhood target.
graph.fit();
host.clientHeight=260;graph.resizeObserver.callback();flushFrames();
assert.equal(graph.cy.lastFitItems,undefined);
''')

    def test_fullscreen_failure_reveals_feedback_without_a_selection(self):
        self.run_js(r'''
document.fullscreenEnabled=true;
const host=container();
host.parentElement={requestFullscreen:async()=>{throw new Error('denied');}};
const graph=new IndustrialGraph(host);
assert.equal(graph.focusBar.hidden,true);
await graph.fullscreenButton.click();
assert.equal(graph.focusBar.hidden,false);
assert.match(graph.focusStatus.textContent,/浏览器未允许全屏/);
assert.equal(graph.fullscreenButton['aria-pressed'],'false');
graph.resetTools();
assert.equal(graph.focusBar.hidden,true);
''')
