"""Industrial identity cleanup and stale-environment protection without a reset control."""

import unittest

from tests.fixtures.workbench_ui import STATIC, governance_markup, governance_source
from tests.unit import test_industrial_web, test_playground_flow


class PlaygroundResetUiTests(unittest.TestCase):
    run_module = test_industrial_web.IndustrialModuleTests.run_module

    def test_industrial_surface_has_no_destructive_full_database_reset_action(self):
        for source in (governance_markup(), (STATIC / "index.html").read_text(), (STATIC / "governance.mjs").read_text()):
            self.assertNotIn('id="local-reset-button"', source)
            self.assertNotIn('/playground/reset', source)
        self.assertIn('state.knowledgeBrowser?.reset()', governance_source())

    def test_optional_generation_configuration_never_selects_an_identity(self):
        self.run_module(r"""
const calls=[];const client=new core.WorkbenchClient((...args)=>calls.push(args));
client.configureBootstrap({});client.configureBootstrap({local_reset:{enabled:false}});
assert.equal(client.epoch,0);assert.equal(client.personaId,null);assert.equal(client.session,null);
assert.equal(client.environmentError,null);assert.deepEqual(calls,[]);
""")

    def test_running_and_failed_environment_block_business_without_submitting(self):
        self.run_module(r"""
for(const state of ['RUNNING','FAILED']) {
  const calls=[];const client=new core.WorkbenchClient(async(path,options)=>{
    calls.push(path);return response(session('admin'));
  });
  client.configureBootstrap({local_reset:{enabled:true,generation:'current',state}});
  await client.selectPersona('admin');
  await assert.rejects(client.request('/v1/knowledge/publications:publish',{}),
    error=>error.code===(state==='FAILED'?'PLAYGROUND_RESET_FAILED':'PLAYGROUND_RESET_RUNNING'));
  assert.deepEqual(calls,['/playground/session']);
}
""")

    def test_stale_environment_never_replays_a_mutation_or_acknowledges_a_new_generation(self):
        self.run_module(r"""
const calls=[];const client=new core.WorkbenchClient(async(path,options)=>{
  calls.push({path,options});return path==='/playground/session'?response(session('admin')):
    {ok:false,status:409,json:async()=>({code:'PLAYGROUND_RESET_STALE'})};
});
client.configureBootstrap({local_reset:{enabled:true,generation:'before',state:'READY'}});
await client.selectPersona('admin');
await assert.rejects(client.requestOptions('/v1/knowledge/reviews:batch',{method:'POST',body:'{}'}),
  error=>error.code==='PLAYGROUND_RESET_STALE');
assert.equal(calls[1].options.headers['X-Playground-Generation'],'before');
assert.throws(()=>client.configureBootstrap({local_reset:{enabled:true,generation:'after',state:'READY'}}));
await assert.rejects(client.requestOptions('/v1/knowledge/reviews:batch',{method:'POST',body:'{}'}));
assert.equal(calls.length,2);assert.equal(client.pageGeneration,'before');
""")

    def test_identity_reset_clears_private_drafts_receipts_outputs_and_operation_locks(self):
        source = governance_source()
        code = source[source.index('function setUploadKnowledgeScope('):source.index('function showConstructionFlow(')]
        code += source[source.index('function updateReviewBulkActions('):source.index('async function keepExistingFact(')]
        code += source[source.index('function reset()'):source.index("$('ontology-reset').addEventListener")]
        self.run_module(r"""
const nodes=new Map();
const node=()=>({value:'private draft',textContent:'private source',innerHTML:'private source',disabled:true,hidden:false,
  replaceChildren(){this.textContent='';this.innerHTML='';},querySelectorAll(){return[];}});
const $=id=>{if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);};
const names=['reviewList','aboxEditor','qualitySaveButton','qualitySaveOutput','qualityHistoryPublication',
  'qualityHistoryList','qualityHistoryDetail','publicationRevisions','publicationRemovals','inventorySummary',
  'inventoryList','qualityContent','documentLifecycleSummary','documentLifecycleList','ontologyList','historyList',
  'constructionJobList','constructionOutput','publicationOutput'];
const elements=Object.fromEntries(names.map(name=>[name,node()]));
const inputs=[$('document-file'),$('manual-subject-name'),elements.ontologyList];
const state={revisionHistories:new Map([['private',{}]]),selectedCandidateRevisions:new Set(['private']),
  approvedRevisions:new Set(['private']),manualOperation:{fingerprint:'private fact'},manualBusy:true,ontologySaving:true,
  uploadKnowledgeScope:'AUTHORITATIVE',constructionBusy:true,
  knowledgeBrowser:{reset(){}},qualityPresenter:{reset(){},invalidate(){}},maintenanceActions:{reset(){}}};
const run=new Function('state','elements','$','host',`
  let loadedIdentity='old',loadingIdentity={identity:'old'},lastBuildFlow='baseline',buildStep='review';
  function invalidatePublicationPreview(){state.publicationPreview=null;}
  function invalidateReviewResolutions(){state.reviews=[];state.resolutions=new Map();}
  function clearDemoSourceBinding(){state.demoSourceBinding=null;}
  function refreshABoxPreparation(){}function invalidateInventory(){state.activeInventory=null;}
  function renderPublicationCandidates(){}
  ${input.code}
  reset();return {loadedIdentity,loadingIdentity,lastBuildFlow,buildStep};`);
const result=run(state,elements,$,{querySelectorAll:()=>inputs});
assert.equal(result.loadedIdentity,null);assert.equal(result.loadingIdentity,null);
assert.equal(result.lastBuildFlow,'business');assert.equal(result.buildStep,'upload');
assert.equal(state.buildView,null);
assert.equal($('document-file-name').textContent,'尚未选择文件');
assert.equal($('ontology-selection').open,false);
assert.deepEqual(state.reviews,[]);assert.equal(state.resolutions.size,0);assert.equal(state.revisionHistories.size,0);
assert.equal(state.reviewLoading,false);assert.equal($('review-bulk-actions').hidden,true);
assert.equal(state.manualOperation,null);assert.equal(state.manualBusy,false);assert.equal(state.ontologySaving,false);
assert.equal(state.approvedRevisions.size,0);assert.equal(state.selectedCandidateRevisions.size,0);
assert.equal(elements.publicationRevisions.value,'');assert.equal(elements.publicationRemovals.value,'');
assert.equal($('manual-subject-name').value,'');assert.equal($('document-file').value,'');
assert.equal(state.uploadKnowledgeScope,'BUSINESS');assert.equal(state.constructionBusy,false);
assert.equal($('document-knowledge-scope').value,'BUSINESS');assert.equal($('document-knowledge-scope').disabled,false);
assert.ok(!$('manual-output').textContent.includes('private'));
assert.equal(elements.ontologyList.innerHTML,'');assert.equal(elements.historyList.innerHTML,'');
assert.equal(elements.constructionOutput.hidden,true);assert.equal(elements.publicationOutput.hidden,true);
assert.equal(elements.constructionOutput.textContent,'');assert.equal(elements.publicationOutput.textContent,'');
""", code=code)

    def test_old_manual_response_cannot_unlock_another_identity_operation(self):
        test_playground_flow.PlaygroundFlowUiTests.setUpClass()
        test_playground_flow.PlaygroundFlowUiTests().run_js(r"""
$('document-tbox').value='assets';$('manual-kind').value='ENTITY';
$('manual-subject-type').value='Equipment';$('manual-subject-name').value='Private pump';
const old=submitManualFact();requests[0].resolve({items:[{key:'assets',tbox_id:'tbox',status:'PUBLISHED'}]});await flush();
state.identityEpoch++;state.manualBusy=true;$('manual-save-button').disabled=true;
$('manual-output').textContent='new identity';
requests[1].resolve({chunks:[{mention_record_ids:['old']} ]});await old;
assert.equal(state.manualBusy,true);assert.equal($('manual-save-button').disabled,true);
assert.equal($('manual-output').textContent,'new identity');
""")

    def test_old_ontology_response_cannot_unlock_another_identity_operation(self):
        test_playground_flow.PlaygroundFlowUiTests.setUpClass()
        test_playground_flow.PlaygroundFlowUiTests().run_js(r"""
elements.ontologyEditor.value=JSON.stringify({key:'assets',version:1,entity_types:[],relationship_types:[]});
const old=importOntology();state.identityEpoch++;state.ontologySaving=true;$('ontology-import-button').disabled=true;
requests[0].resolve({tbox_id:'old-result'});await old;
assert.equal(state.ontologySaving,true);assert.equal($('ontology-import-button').disabled,true);
""")
