"""Read-only live mapper probe; never writes knowledge."""
import asyncio,json,os,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from dotenv import load_dotenv
from neo4j import GraphDatabase
from openai import OpenAI
from graphrag_prod.domain.access import Principal
from graphrag_prod.knowledge.auto_review import Neo4jAutoReviewService
from graphrag_prod.knowledge.context_projection import Neo4jContextProjectionService
from graphrag_prod.construction.context_mapping import build_context_payload,compile_context_properties,OpenAICompatibleContextMapper
from graphrag_prod.knowledge.models import EntityMentionRecord,AssertionRecord

async def main():
 load_dotenv(ROOT/'.env');load_dotenv(ROOT/'.env.workbench.local',override=True)
 job_id='a85f4970-47a0-5105-95e7-b2425b5a5fc8'
 with GraphDatabase.driver(os.environ['PLAYGROUND_NEO4J_URI'],auth=(os.environ['PLAYGROUND_NEO4J_USER'],os.environ['PLAYGROUND_NEO4J_PASSWORD']),max_transaction_retry_time=0,notifications_min_severity='OFF') as driver:
  database=os.getenv('PLAYGROUND_NEO4J_DATABASE','neo4j')
  with driver.session(database=database) as session:
   scope=session.run('MATCH(j:KnowledgeConstructionJob {job_id:$job}) RETURN j.tenant_id AS tenant,j.access_groups AS groups LIMIT 2',job=job_id).single(strict=True)
  principal=Principal('context-read-only-probe',scope['tenant'],frozenset(scope['groups']),frozenset({'knowledge:construct','knowledge:review'}))
  review=Neo4jAutoReviewService(driver,database);job=review._job(principal,job_id);data=review._load(principal,job)
  summary=next(c.mapping_summary for c in job.chunks if c.mapping_summary)
  mentions=[r for r in data['current'] if isinstance(r,EntityMentionRecord)]
  facts=[r for r in data['comparison'] if isinstance(r,AssertionRecord)]
  payload=build_context_payload(data['text'],data['tbox'],summary,mentions)
  model=OpenAICompatibleContextMapper(client=OpenAI(api_key=os.environ['OPENAI_API_KEY'],base_url=os.environ['OPENAI_BASE_URL'],max_retries=0),model=os.environ['MODEL_NAME'])
  cache=ROOT/'.local/context-live-probe-v3-private.json'
  proposal=json.loads(cache.read_text()) if cache.exists() else await model.plan(payload)
  cache.write_text(json.dumps(proposal,ensure_ascii=False,default=str))
  report={'read_only':True,'status':proposal['status'],'attempts':[{'status':a.get('status'),'seconds':a.get('seconds'),'error_code':a.get('error_code')} for a in proposal['audit']['attempts']]}
  if proposal['status']=='COMPLETE':
   chunks=Neo4jContextProjectionService(driver,database,planner=model)._chunks(principal,job,data['text'])
   result=compile_context_properties(data['text'],data['tbox'],summary,mentions,facts,chunks,proposal['mapping'])
   report.update(added_assertions=len(result.specs),issues=list(result.issues),rules=list(result.rules))
  print(json.dumps(report,ensure_ascii=False))
  (ROOT/'.local/context-live-probe.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
asyncio.run(main())
