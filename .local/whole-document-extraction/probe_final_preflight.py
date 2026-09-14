import logging,time,sys
from pathlib import Path
import httpx,jwt
from neo4j import GraphDatabase,unit_of_work
from scripts.restart_workbench import private_environment
from graphrag_prod.construction import BoundedDocumentParser
from graphrag_prod.construction import preflight as pf
from graphrag_prod.domain import Principal
logging.getLogger('neo4j').setLevel(logging.ERROR)
@unit_of_work(timeout=15.0)
def traced(tx,*args):
 class Trace:
  def run(self,query,**parameters):
   name=parameters.get('comparison_mode','POSSIBLE');start=time.monotonic()
   try:
    result=list(tx.run(query,**parameters));print({'phase':name,'seconds':round(time.monotonic()-start,3),'rows':len(result)},flush=True);return result
   except Exception as exc:
    print({'phase':name,'seconds':round(time.monotonic()-start,3),'error':type(exc).__name__,'code':getattr(exc,'code',None)},flush=True);raise
 return pf.Neo4jUploadPreflight._check_tx(Trace(),*args)
base='http://127.0.0.1:8002'
with httpx.Client(timeout=15,trust_env=False) as client:
 b=client.get(base+'/playground/bootstrap').json();w=next(w for w in b['knowledge_bases'] if w['name']=='母线槽知识库');p=next(p for p in b['personas'] if p.get('knowledge_base_id')==w['knowledge_base_id'] and p.get('role')=='admin')
 claims=jwt.decode(client.post(base+'/playground/session',json={'persona_id':p['id']}).json()['access_token'],options={'verify_signature':False})
principal=Principal(claims['sub'],claims['tenant_id'],frozenset(claims['groups']),frozenset(claims['scope'].split()));env=private_environment()
with GraphDatabase.driver(env['PLAYGROUND_NEO4J_URI'],auth=(env['PLAYGROUND_NEO4J_USER'],env['PLAYGROUND_NEO4J_PASSWORD']),max_transaction_retry_time=0,connection_timeout=5) as driver:
 reader=pf.Neo4jUploadPreflight(driver,env['PLAYGROUND_NEO4J_DATABASE'],BoundedDocumentParser(json_record_boundaries=True));reader._work=traced
 try:
  content=Path('busway_files/topology.source.json').read_bytes();content=content.replace('模型处理完成不保证完整建图'.encode(),'模型处理完成仍应核对建图'.encode(),1) if '--near' in sys.argv else content
  result=reader.check(principal,content,canonical_uri='urn:graphrag:readonly-preflight-diagnosis',mime_type='application/json');print({'status':'ok','exact':len(result['exact_matches']),'similar':len(result['similar_matches'])},flush=True)
 except Exception:print({'status':'failed'},flush=True)
