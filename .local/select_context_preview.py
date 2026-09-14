"""Find the exact authorized repaired Project records for a read-only preview."""
import json,os,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from dotenv import load_dotenv
from neo4j import GraphDatabase
from verify_auto_review_result import read_rows
load_dotenv(ROOT/'.env');load_dotenv(ROOT/'.env.workbench.local',override=True)
baseline=json.loads((ROOT/'.local/context-projection-baseline.json').read_text())
with GraphDatabase.driver(os.environ['PLAYGROUND_NEO4J_URI'],auth=(os.environ['PLAYGROUND_NEO4J_USER'],os.environ['PLAYGROUND_NEO4J_PASSWORD']),notifications_min_severity='OFF') as driver:
 rows=read_rows(driver,os.getenv('PLAYGROUND_NEO4J_DATABASE','neo4j'),'''MATCH(:KnowledgeRecordHead {tenant_id:$tenant})
     -[:CURRENT_REVISION]->(r:GovernedAssertionRevision {tenant_id:$tenant,document_id:$document,version_id:$version,
         subject_entity_type:'Project',object_kind:'literal',governance_status:'APPROVED'})
     RETURN r.record_id AS record,r.revision_id AS revision,r.subject_mention_revision_id AS mention,
         r.predicate AS predicate,r.context_property_evidence_version IS NOT NULL AS context LIMIT 20''',**baseline['scope'])
 context=[r for r in rows if r['context']]
 if len(context)!=1 or len(rows)!=6:raise ValueError('expected one repaired Project with six literal facts')
 revisions=sorted({r['revision'] for r in rows}|{r['mention'] for r in rows})
 if len(revisions)!=7:raise ValueError('Project preview requires exact endpoint closure')
 report={'job_id':baseline['job_id'],'record_id':context[0]['record'],'preview_revision_ids':revisions}
 (ROOT/'.local/context-preview-selection.json').write_text(json.dumps(report,indent=2))
 print(json.dumps(report))
