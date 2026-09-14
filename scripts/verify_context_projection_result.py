"""Read-only verification of the authorized existing topology context repair.

Use --baseline before running the user-authorized UI action. Reports contain
record IDs and checksums, never raw source or model prompts. No knowledge writes.
"""
from __future__ import annotations
import argparse,json,os,sys
from pathlib import Path
from urllib.parse import urlsplit
from verify_auto_review_result import ROOT,read_rows,canonical,checksum,verify


def snapshot(driver,database,job_id):
    jobs=read_rows(driver,database,"""MATCH(j:KnowledgeConstructionJob {job_id:$job})
        RETURN j.tenant_id AS tenant,j.document_id AS document,j.version_id AS version LIMIT 2""",job=job_id)
    if len(jobs)!=1:raise ValueError('exact job scope unavailable')
    scope=jobs[0]
    records=read_rows(driver,database,"""MATCH(:KnowledgeRecordHead {tenant_id:$tenant})-[:CURRENT_REVISION]->(r {
        tenant_id:$tenant,document_id:$document,version_id:$version})
        WHERE r:GovernedEntityMentionRevision OR r:GovernedAssertionRevision
        RETURN r{.*} AS revision LIMIT 6001""",**scope)
    if len(records)>6000:raise ValueError('record budget exceeded')
    outcomes=read_rows(driver,database,"""MATCH(j:KnowledgeConstructionJob {tenant_id:$tenant,job_id:$job,
        document_id:$document,version_id:$version})-[:HAS_CHUNK_OUTCOME]->(o)
        RETURN o.result_json AS payload ORDER BY o.result_json LIMIT 2001""",**scope,job=job_id)
    if len(outcomes)>2000:raise ValueError('chunk budget exceeded')
    return scope,records,{'job_id':job_id,'scope':scope,
        'heads':{r['revision']['record_id']:checksum(canonical(r['revision'])) for r in records},
        'outcomes_checksum':checksum(canonical(outcomes)),
        'source_file_checksum':checksum((ROOT/'busway_files/topology.source.json').read_text())}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',action='store_true')
    parser.add_argument('--job-id',default='a85f4970-47a0-5105-95e7-b2425b5a5fc8')
    args=parser.parse_args()
    from dotenv import load_dotenv
    from neo4j import GraphDatabase
    from graphrag_prod.knowledge.store import _stored_assertion
    from graphrag_prod.construction.context_mapping import validate_context_binding
    load_dotenv(ROOT/'.env');load_dotenv(ROOT/'.env.workbench.local',override=True)
    uri=os.environ['PLAYGROUND_NEO4J_URI']
    if urlsplit(uri).hostname not in {'127.0.0.1','localhost','::1'}:raise ValueError('local DB required')
    baseline_path=ROOT/'.local/context-projection-baseline.json'
    with GraphDatabase.driver(uri,auth=(os.environ['PLAYGROUND_NEO4J_USER'],os.environ['PLAYGROUND_NEO4J_PASSWORD']),
                              max_transaction_retry_time=0,notifications_min_severity='OFF') as driver:
        database=os.getenv('PLAYGROUND_NEO4J_DATABASE','neo4j')
        scope,rows,current=snapshot(driver,database,args.job_id)
        if args.baseline:
            if baseline_path.exists():raise ValueError('baseline already exists; never overwrite original baseline')
            baseline_path.write_text(json.dumps(current,indent=2))
            print(json.dumps({'read_only':True,'baseline_records':len(rows),'saved':str(baseline_path)}));return
        before=json.loads(baseline_path.read_text())
        receipt=json.loads((ROOT/'.local/browser-qa/auto-review-upload/construction-response.json').read_text())
        report=verify(driver,database,receipt)
        checks=report['checks']
        checks.update(original_scope_preserved=current['scope']==before['scope'],
            original_heads_unchanged=all(current['heads'].get(rid)==digest for rid,digest in before['heads'].items()),
            original_chunk_outcomes_unchanged=current['outcomes_checksum']==before['outcomes_checksum'],
            original_file_unchanged=current['source_file_checksum']==before['source_file_checksum'])
        new=[r['revision'] for r in rows if r['revision']['record_id'] not in before['heads']]
        source=read_rows(driver,database,"""MATCH(v:DocumentVersion {tenant_id:$tenant,document_id:$document,version_id:$version})
            RETURN v.normalized_text AS text LIMIT 2""",**scope)[0]['text']
        source_data=json.loads(source)
        expected={r['asset_ref'] for r in source_data['assets']}
        added=[]
        for row in new:
            assertion=_stored_assertion(row);context=assertion.context_property_evidence
            if context is None:raise ValueError('new record lacks context proof')
            validate_context_binding(source,context,subject_type=assertion.subject.entity_type,
                                     predicate=assertion.predicate,raw_literal=assertion.literal_value)
            added.append(assertion)
        checks['exact_context_mapping_for_every_asset']=bool(added) and len(added)==len(expected) and {
            a.context_property_evidence.source_identity for a in added}==expected and all(
            a.predicate=='project_id' and a.literal_semantics.typed_value==source_data['metadata']['project_id'] for a in added)
        checks['context_values_have_separate_evidence']=all(a.evidence.chunk_id!=a.context_property_evidence.value_evidence.chunk_id for a in added)
        secondary=read_rows(driver,database,"""MATCH(:KnowledgeRecordHead {tenant_id:$tenant})-[:CURRENT_REVISION]->(r {
            tenant_id:$tenant,document_id:$document,version_id:$version})
            WHERE r.record_id IN $ids
            OPTIONAL MATCH(r)-[:CONTEXT_EVIDENCED_BY]->(c:Chunk)
            RETURN r.record_id AS id,count(c) AS links,collect(c{.*}) AS chunks LIMIT 6001""",**scope,ids=[a.record_id for a in added])
        valid_edges=len(secondary)==len(added)
        by_id={a.record_id:a for a in added}
        for item in secondary:
            a=by_id[item['id']];e=a.context_property_evidence.value_evidence
            valid_edges &= item['links']==1
            if item['links']!=1:continue
            c=item['chunks'][0]
            valid_edges &= all(c[k]==getattr(e,k) for k in ('tenant_id','document_id','version_id','chunk_id','access_policy_id','access_policy_version'))
            valid_edges &= set(c['access_groups'])==e.access_groups and c['text'][e.char_start-c['char_start']:e.char_end-c['char_start']]==e.quoted_text
        checks['secondary_evidence_edges_exact_and_authorized']=bool(valid_edges)
        checks['context_approved_without_manual_queue']=all(a.trust.status.value=='APPROVED' for a in added) and not receipt['auto_review']['issues']
        attempts=read_rows(driver,database,"""MATCH(:KnowledgeConstructionJob {tenant_id:$tenant,job_id:$job,
            document_id:$document,version_id:$version})-[:HAS_CONTEXT_MAPPING_ATTEMPT]->(a)
            RETURN a.payload_json AS payload,a.checksum AS checksum LIMIT 17""",**scope,job=args.job_id)
        if len(attempts)>16:raise ValueError('audit budget exceeded')
        checks['context_mapper_audit_valid']=bool(attempts) and all(checksum(a['payload'])==a['checksum'] for a in attempts)
        report.update(passed=all(checks.values()),added_context_assertions=len(added),
            context_model_calls=sum(len(json.loads(a['payload']).get('audit',{}).get('attempts',[])) for a in attempts),
            context_mapping=receipt['auto_review']['context_mapping'])
        output=ROOT/'.local/context-projection-acceptance.json';output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps(report,ensure_ascii=False))
        if not report['passed']:sys.exit(1)

if __name__=='__main__':main()
