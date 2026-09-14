"""Source ontology replay and authenticated whole-document progress in the UI."""

import json
from pathlib import Path
import unittest

from tests.fixtures.workbench_ui import governance_source
from tests.unit import test_industrial_web, test_playground_flow


class OntologySourceUiTests(unittest.TestCase):
    run_module = test_industrial_web.IndustrialModuleTests.run_module

    def test_source_import_selects_actual_key_and_known_active_version(self):
        test_playground_flow.PlaygroundFlowUiTests.setUpClass()
        test_playground_flow.PlaygroundFlowUiTests().run_js(r"""
state.ontologies=[{key:'ai_power.busway.ontology',tbox_id:'existing',status:'PUBLISHED'}];
elements.ontologyEditor.value=JSON.stringify({metadata:{contract_id:'ai_power.knowledge_ontology',contract_version:'1.0.0',ontology_id:'ai_power.busway.ontology',ontology_version:'5.0.0'},entity_types:{},relation_types:{}});
const pending=importOntology();
const submitted=JSON.parse(requests[0].options.body);
assert.equal(submitted.expected_active_tbox_id,'existing');
assert.equal(submitted.activate,true);
requests[0].resolve({key:'ai_power.busway.ontology',version:5000000,source_contract:{metadata:{ontology_version:'5.0.0'}}});
await pending;
assert.equal($('document-tbox').value,'ai_power.busway.ontology');
""")

    def run_import_ui(self, scenario):
        test_playground_flow.PlaygroundFlowUiTests.setUpClass()
        root = Path(__file__).resolve().parents[2]
        sources = {
            "source": json.loads((root / "busway_files/ontology.source.json").read_text()),
            "topology": json.loads((root / "busway_files/topology.source.json").read_text()),
            "pump": json.loads((root / "src/graphrag_prod/playground/static/industrial-demo-v1/ontology.json").read_text()),
        }
        setup = "const files=" + json.dumps(sources, ensure_ascii=False) + ";"
        setup += "globalThis.MAX_UPLOAD_BYTES=5*1024*1024;"
        test_playground_flow.PlaygroundFlowUiTests().run_js(setup + scenario)

    def test_supported_formats_and_business_json_are_distinguished_by_content(self):
        self.run_import_ui(r"""
for (const value of [files.source,files.pump]) assert.equal(ontologyImportDefinition(value),value);
const source={...files.source,expected_checksum:'a'.repeat(64),rule_reference_registry:{registry_id:'rules'}};
const exported={schema:'graphrag-property-tbox-export-v1',checksum:source.expected_checksum,definition:source};
assert.equal(ontologyImportDefinition(exported),source);
for (const data of [files.topology,{metadata:{title:'equipment'},assets:[]},{ontology_reference:'busway',entities:[]}]) {
  assert.throws(()=>ontologyImportDefinition(data),/实例构建/);
}
assert.throws(()=>ontologyImportDefinition({...files.source,entity_types:[]}),/本体源文件不完整/);
assert.throws(()=>ontologyImportDefinition({...files.source,metadata:{...files.source.metadata,contract_version:'unknown'}}),/版本/);
assert.throws(()=>ontologyImportDefinition({...exported,checksum:'changed'}),/校验码/);
assert.throws(()=>ontologyImportDefinition({...files.pump,entity_types:{}}),/本体定义格式不完整/);
""")

    def test_wrong_file_blocks_submission_without_saving_previous_pump_and_recovers(self):
        self.run_import_ui(r"""
state.ontologies=[{key:files.pump.key,tbox_id:'pump-active',status:'PUBLISHED'}];
elements.ontologyEditor.value=JSON.stringify(files.pump);
state.ontologyRuleRegistry={registry_id:'old'};
const select=async (name,value)=>{
  const text=typeof value==='string'?value:JSON.stringify(value);
  $('ontology-file').files=[{name,size:text.length,text:async()=>text}];
  await loadOntologyFile();
};
await select('ontology.json',files.topology);
assert.equal($('ontology-import-button').disabled,true);
assert.equal($('ontology-input-error').hidden,false);
assert.match($('ontology-input-error').textContent,/实例构建/);
assert.deepEqual(JSON.parse(elements.ontologyEditor.value),files.topology);
assert.equal(state.ontologyRuleRegistry,null);
await importOntology();assert.equal(requests.length,0);
assert.equal(state.ontologies[0].tbox_id,'pump-active');
await select('topology.json',files.source);
assert.equal($('ontology-input-error').hidden,true);
assert.equal($('ontology-import-button').disabled,false);
assert.equal(requests.length,0);
await select('invalid.json','{broken');
assert.equal($('ontology-import-button').disabled,true);
assert.match($('ontology-input-error').textContent,/有效的 JSON/);
elements.ontologyEditor.value=JSON.stringify(files.source);validateOntologyEditor();
assert.equal($('ontology-import-button').disabled,false);
assert.equal($('ontology-input-error').hidden,true);
elements.ontologyEditor.value=JSON.stringify(files.topology);await importOntology();
assert.equal(requests.length,0);assert.match(state.ontologyInputError,/实例构建/);
""")

    def test_slow_old_file_cannot_replace_new_selection_or_manual_correction(self):
        self.run_import_ui(r"""
let finishOld;
$('ontology-file').files=[{name:'old.json',size:20,text:()=>new Promise(resolve=>{finishOld=resolve;})}];
const old=loadOntologyFile();assert.equal($('ontology-import-button').disabled,true);
await importOntology();assert.equal(requests.length,0);
$('ontology-file').files=[{name:'new.json',size:20,text:async()=>JSON.stringify(files.source)}];
await loadOntologyFile();
finishOld(JSON.stringify(files.topology));await old;
assert.deepEqual(JSON.parse(elements.ontologyEditor.value),files.source);
assert.equal($('ontology-file-name').textContent,'new.json');
assert.equal($('ontology-import-button').disabled,false);
$('ontology-file').files=[{name:'slow.json',size:20,text:()=>new Promise(resolve=>{finishOld=resolve;})}];
const pending=loadOntologyFile();
elements.ontologyEditor.value=JSON.stringify(files.pump);validateOntologyEditor();
finishOld(JSON.stringify(files.topology));await pending;
assert.deepEqual(JSON.parse(elements.ontologyEditor.value),files.pump);
assert.equal($('ontology-import-button').disabled,false);
""")

    def test_late_capability_refresh_preserves_user_input_and_validates_untouched_default(self):
        source = governance_source()
        refresh = source[source.index('async function refreshCapabilities()'):source.index('  function activate(name)')]
        helpers = source[source.index('function ontologyImportDefinition('):source.index('async function loadOntologyRuleReferences(')]
        root = Path(__file__).resolve().parents[2]
        default = json.loads((root / 'src/graphrag_prod/playground/static/industrial-demo-v1/ontology.json').read_text())
        self.run_module(r"""
const make = new Function('input','deferred',`
  const state={ontologies:[],identityEpoch:1,ontologyFileSelection:0,
    ontologyFileLoading:false,ontologySaving:false,ontologyInputError:'previous input error'};
  const client={session:true,epoch:1}; let loadedIdentity=null,loadingIdentity=null;
  const fields=new Map();
  const $=id=>{if(!fields.has(id))fields.set(id,{value:'',textContent:'',hidden:false,
    disabled:true,attributes:{},setAttribute(name,value){this.attributes[name]=value;}});return fields.get(id);};
  const elements={ontologyEditor:$('ontology-editor')};
  const host={querySelectorAll:()=>[]};
  const bootstrap={capabilities:{},defaults:{industrial_tbox_template:input.default}};
  const pending=deferred();
  const loadOntologies=()=>pending.promise;
  const renderDocumentAccessGroups=()=>{},renderDemoKit=()=>{},currentPersona=()=>({});
  const renderConstructionOntology=()=>{},renderBuildView=()=>{},refreshABoxPreparation=()=>{},updateConstructionMode=()=>{};
  const loadConstructionJobs=async()=>{},loadReviews=async()=>{},loadPublicationCandidates=async()=>{},loadHistory=async()=>{};
  const editableOntology=value=>value,showToast=()=>{},MAX_UPLOAD_BYTES=5*1024*1024;
  ${input.helpers}
  ${input.refresh}
  return {state,$,elements,pending,refreshCapabilities,loadOntologyFile,validateOntologyEditor};
`);
const edited={...input.default,key:'user-selected-ontology'};
for (const mode of ['file','manual','invalid-manual']) {
  const view=make(input,deferred);
  const refreshing=view.refreshCapabilities();
  if(mode==='file') {
    const text=JSON.stringify(edited);
    view.$('ontology-file').files=[{name:'user-selected.json',size:text.length,text:async()=>text}];
    await view.loadOntologyFile();
  } else {
    view.elements.ontologyEditor.value=mode==='manual'?JSON.stringify(edited):'{broken';
    view.validateOntologyEditor();
  }
  const before={text:view.elements.ontologyEditor.value,error:view.state.ontologyInputError,
    disabled:view.$('ontology-import-button').disabled,selection:view.state.ontologyFileSelection};
  assert.ok(before.selection>0);
  view.pending.resolve(); await refreshing;
  assert.equal(view.elements.ontologyEditor.value,before.text,mode+' must retain the user input');
  assert.equal(view.state.ontologyInputError,before.error,mode+' must retain validation state');
  assert.equal(view.$('ontology-import-button').disabled,before.disabled);
  if(mode==='file') assert.equal(view.$('ontology-file-name').textContent,'user-selected.json');
}
const untouched=make(input,deferred);
const first=untouched.refreshCapabilities();
untouched.pending.resolve();await first;
assert.deepEqual(JSON.parse(untouched.elements.ontologyEditor.value),input.default);
assert.equal(untouched.state.ontologyInputError,'');
assert.equal(untouched.$('ontology-input-error').hidden,true);
assert.equal(untouched.$('ontology-import-button').disabled,false);
assert.equal(untouched.elements.ontologyEditor.attributes['aria-invalid'],'false');
""", refresh=refresh, helpers=helpers, default=default)

    def test_source_export_and_next_version_preserve_full_definition(self):
        source = governance_source()
        code = source[source.index('function editableOntology('):source.index('function loadOntologyIntoEditor(')]
        self.run_module(r"""
const source={metadata:{ontology_version:'5.0.0',status:'DRAFT'},governance_constraints:{evidence:{required:true}}};
const registry={registry_id:'external-rules',version:1,documents:[]};
const item={key:'busway',version:5000000,checksum:'checksum',source_contract_json:JSON.stringify(source),rule_reference_registry:registry};
const state={ontologies:[item]};
const edit=new Function('state','item',input.code+';return [editableOntology(item),editableOntology(item,true)];');
const [replay,next]=edit(state,item);
assert.equal(replay.expected_checksum,'checksum');
assert.equal(next.expected_checksum,undefined);
assert.equal(next.metadata.ontology_version,'5.0.1');
assert.deepEqual(next.governance_constraints,source.governance_constraints);
assert.deepEqual(replay.rule_reference_registry,registry);
assert.deepEqual(next.rule_reference_registry,registry);
assert.equal(JSON.parse(item.source_contract_json).metadata.ontology_version,'5.0.0');
""", code=code)

    def test_progress_matches_operation_and_ignores_stale_identity(self):
        source = governance_source()
        code = source[source.index('function watchConstructionProgress('):source.index('function reviewEdit(')]
        self.run_module(r"""
const state={identityEpoch:1};const display={hidden:true,textContent:''};const callbacks=[];const reads=[];
const api=()=>{const d=deferred();reads.push(d);return d.promise;};
const watch=new Function('state','$','apiRequest','setTimeout','clearTimeout',input.code+';return watchConstructionProgress;')(
 state,()=>display,api,fn=>{callbacks.push(fn);return callbacks.length;},()=>{});
const stop=watch('mine',1,'Starting');
const polling=callbacks.shift()();
reads[0].resolve({items:[{operation_key:'someone-else',completed_chunks:99,expected_chunks:99,status:'COMPLETED'},{operation_key:'mine',completed_chunks:3,expected_chunks:59,status:'RUNNING'}]});
await polling;
assert.ok(display.textContent.includes('3 / 59'));
const stale=callbacks.shift()();state.identityEpoch=2;display.textContent='new identity';
reads[1].resolve({items:[{operation_key:'mine',completed_chunks:59,expected_chunks:59,status:'COMPLETED'}]});
await stale;stop();
assert.equal(display.textContent,'new identity');
assert.equal(callbacks.length,0);
""", code=code)
