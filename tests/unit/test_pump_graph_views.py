"""Generic graph navigation driven only by the current ontology schema."""
from pathlib import Path
import json
import subprocess
import unittest

STATIC=Path('src/graphrag_prod/playground/static/industrial')

class PumpGraphViewTests(unittest.TestCase):
    def run_js(self, scenario):
        model=(STATIC/'graph-model.mjs').resolve().as_uri()
        script=f"import assert from 'node:assert/strict';\nimport {{VIEWS,graphEmptyState,ontologyGraphFilters}} from {model!r};\n"+scenario
        result=subprocess.run(['node','--input-type=module','-e',script],text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_filters_follow_current_pump_ontology_only(self):
        schema=json.loads(Path('src/graphrag_prod/playground/static/industrial-demo-v1/ontology.json').read_text())
        self.run_js('const schema='+json.dumps(schema)+';'+'''
assert.deepEqual(Object.keys(VIEWS),['all','ontology']);
const options=ontologyGraphFilters(schema);
assert.deepEqual(options.flatMap(o=>o.predicates).sort(),schema.relationship_types.map(r=>r.name).sort());
assert.ok(options.every(o=>o.group==='关系类型'));
assert.ok(options.some(o=>o.label==='包含部件'));
// A declared hierarchy is the only way to add a hierarchy choice.
const configured={relationship_types:[{name:'CUSTOM_PUMP_LINK'}],hierarchies:[
 {name:'PumpHierarchy',relationship_type:'CUSTOM_PUMP_LINK'},
 {name:'Unbound',relationship_type:'NOT_DECLARED'}]};
const dynamic=ontologyGraphFilters(configured);
assert.deepEqual(dynamic.map(o=>o.value),['hierarchy:PumpHierarchy','relation:CUSTOM_PUMP_LINK']);
assert.deepEqual(dynamic[0].predicates,['CUSTOM_PUMP_LINK']);
assert.deepEqual(ontologyGraphFilters({}),[]);
''')

    def test_selection_clears_with_schema_change_and_ontology_ignores_filter(self):
        source=(STATIC/'app.mjs').read_text()
        functions=source[source.index('function selectedGraphFilter('):source.index('async function loadGraph(')]
        self.run_js('''
let view='all', graphFilters=[], graphSource=null;
const node=()=>({value:'',children:[],append(...items){this.children.push(...items);}});
const fields={'graph-relation-filter':node(),family:node(),asset:node(),trust:node()};
const $=id=>fields[id];
const clear=n=>{n.children=[];return n;};
const element=()=>node();
const Option=function(label,value){this.label=label;this.value=value;};
const selectedIndustrialScope=()=>null;
'''+functions+'''
const schema={relationship_types:[{name:'CONTAINS'}]};
syncGraphFilters(schema);
assert.deepEqual(graphBody().predicates,[]);
fields['graph-relation-filter'].value='relation:CONTAINS';
assert.deepEqual(graphBody().predicates,['CONTAINS']);
view='ontology';syncGraphFilters(schema);
assert.deepEqual(graphBody().predicates,[]);
assert.equal(fields['graph-relation-filter'].disabled,true);
view='all';syncGraphFilters(schema);
assert.deepEqual(graphBody().predicates,['CONTAINS']);
assert.equal(syncGraphFilters({relationship_types:[]}),true);
assert.deepEqual(graphBody().predicates,[]);
assert.equal(fields['graph-relation-filter'].value,'');
''')

    def test_generic_controls_and_empty_state(self):
        markup=(STATIC/'index.html').read_text()
        for old in ['data-view="composition"','data-view="classification"','data-view="diagnostic"','data-view="connection"']:
            self.assertNotIn(old,markup)
        self.assertIn('id="graph-relation-filter"',markup)
        self.run_js('''
const filtered=graphEmptyState('all',{publication_id:'pump'},'包含部件');
assert.equal(filtered.showAll,true);
assert.ok(filtered.title.includes('包含部件'));
assert.ok(!filtered.title.includes('未发布'));
assert.ok(graphEmptyState('all',{publication_id:'pump'}).detail.includes('已有发布版本'));
assert.ok(graphEmptyState('all').title.includes('尚无'));
assert.equal(graphEmptyState('ontology',{},'包含部件').showAll,false);
''')
