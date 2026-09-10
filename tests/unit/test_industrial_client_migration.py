"""Offline checks for the shared industrial governance request adapter."""

import unittest

from tests.unit import test_industrial_web


class IndustrialGovernanceClientTests(unittest.TestCase):
    run_module = test_industrial_web.IndustrialModuleTests.run_module

    def test_fetch_options_preserve_selected_identity_and_page_generation(self):
        self.run_module(r"""
const calls=[];
const client=new core.WorkbenchClient(async(path,options)=>{
  calls.push({path,options});
  return response(path==='/playground/session' ? session('reader') : {ok:true});
});
client.configureBootstrap({local_reset:{enabled:true,generation:'page-one',state:'READY'}});
await client.selectPersona('reader');
await client.requestOptions('/v1/knowledge/reviews:batch',{
  method:'POST',body:JSON.stringify({decisions:[]}),
  headers:{Authorization:'Bearer admin','X-Playground-Generation':'other-page','X-Request-ID':'request-one'},
});
assert.equal(calls.length,2);
assert.equal(calls[1].options.headers.Authorization,'Bearer reader-token');
assert.equal(calls[1].options.headers['X-Playground-Generation'],'page-one');
assert.equal(calls[1].options.headers['x-request-id'],'request-one');
assert.deepEqual(JSON.parse(calls[1].options.body),{decisions:[]});
assert.equal(client.session.identity.id,'reader');
await client.requestOptions('/v1/ontologies?limit=100');
assert.equal(calls[2].options.method,'GET');
assert.equal(calls[2].options.headers['X-Playground-Generation'],undefined);
""")

    def test_retained_industrial_environment_needs_no_reset_generation(self):
        self.run_module(r"""
const calls=[];
const client=new core.WorkbenchClient(async(path,options)=>{
  calls.push({path,options});return response(path==='/playground/session'?session('admin'):{ok:true});
});
client.configureBootstrap({industrial:{enabled:true}});
await client.selectPersona('admin');
await client.requestOptions('/v1/knowledge/publications:preview',{method:'POST',body:'{}'});
assert.equal(calls[1].options.headers['X-Playground-Generation'],undefined);
assert.equal(calls[1].options.headers.Authorization,'Bearer admin-token');
""")

    def test_bootstrap_change_does_not_acknowledge_old_forms_after_persona_change(self):
        self.run_module(r"""
const calls=[];
const client=new core.WorkbenchClient(async(path,options)=>{
  calls.push(path);return response(session(JSON.parse(options.body).persona_id));
});
client.configureBootstrap({local_reset:{enabled:true,generation:'old',state:'READY'}});
await client.selectPersona('admin');
assert.throws(()=>client.configureBootstrap({local_reset:{enabled:true,generation:'new',state:'READY'}}),
  error=>error.code==='PLAYGROUND_RESET_STALE');
await client.selectPersona('reader');
await assert.rejects(client.requestOptions('/v1/knowledge/reviews:batch',{method:'POST',body:'{}'}),
  error=>error.code==='PLAYGROUND_RESET_STALE');
assert.deepEqual(calls,['/playground/session','/playground/session']);
assert.equal(client.pageGeneration,'old');
""")

    def test_publication_issue_is_preserved_for_focused_correction(self):
        self.run_module(r"""
const issue={message:'请先确认主体实体',targets:[{record_id:'subject',record_kind:'ENTITY_MENTION'}]};
const client=new core.WorkbenchClient(async path=>path==='/playground/session'
  ? response(session('admin')) : {ok:false,status:409,json:async()=>({code:'publication_conflict',publication_issue:issue})});
await client.selectPersona('admin');
await assert.rejects(client.requestOptions('/v1/knowledge/publications:publish',{method:'POST',body:'{}'}),error=>{
  assert.deepEqual(error.publicationIssue,issue);
  assert.equal(error.message,issue.message);assert.equal(error.status,409);return true;
});
issue.targets=Array.from({length:51},()=>({record_id:'subject'}));
await assert.rejects(client.request('/v1/knowledge/publications:preview',{}),error=>!error.publicationIssue);
""")

    def test_reset_conflict_fences_late_reads_and_further_requests(self):
        self.run_module(r"""
const pending=deferred();let reads=0;
const client=new core.WorkbenchClient(async path=>{
  if(path==='/playground/session')return response(session('admin'));
  if(++reads===1)return pending.promise;
  return {ok:false,status:409,json:async()=>({code:'PLAYGROUND_RESET_STALE'})};
});
await client.selectPersona('admin');
const old=client.request('/v1/knowledge/publications');
const rejected=assert.rejects(old,error=>error.code==='PLAYGROUND_RESET_STALE');
await assert.rejects(client.requestOptions('/v1/knowledge/reviews:batch',{body:'{}'}),
  error=>error.code==='PLAYGROUND_RESET_STALE');
pending.resolve(response({items:['old protected context']}));await rejected;
await assert.rejects(client.request('/v1/knowledge/publications'),error=>error.code==='PLAYGROUND_RESET_STALE');
assert.equal(reads,2);
""")

    def test_fetch_style_abort_signal_is_forwarded_and_releases_controller(self):
        self.run_module(r"""
let captured;
const client=new core.WorkbenchClient(async(path,options)=>{
  if(path==='/playground/session')return response(session('reader'));
  captured=options.signal;
  return new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new DOMException('Stopped','AbortError'))));
});
await client.selectPersona('reader');
const controller=new AbortController();
const request=client.requestOptions('/v1/ontologies?limit=100',{signal:controller.signal});
const rejected=assert.rejects(request,error=>error.name==='AbortError');
controller.abort();await rejected;
assert.equal(captured.aborted,true);assert.equal(client.controllers.size,0);
""")
