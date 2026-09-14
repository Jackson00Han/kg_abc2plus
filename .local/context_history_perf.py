"""Read-only, exact-record history and evidence query performance check."""
import hashlib
import json
import os
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace

from dotenv import load_dotenv
from neo4j import GraphDatabase, Query
from graphrag_prod.domain.access import Principal
from graphrag_prod.knowledge.review import _REVISION_HISTORY_QUERY, ReviewRecordKind
from graphrag_prod.knowledge.review_context import evidence_context_tx

root = Path(__file__).resolve().parents[1]
load_dotenv(root / '.env')
load_dotenv(root / '.env.workbench.local', override=True)
tenant = 'workspace-9bd58de9-958a-59a2-bd2f-329f9758dda4'
record_id = '1cd9c03c-c3cb-5f22-ba35-2b7db1502e91'
before = json.loads((root / '.local/context-history-query-before.json').read_text())
report = {'read_only': True, 'record_id': record_id, 'tenant_id': tenant,
          'mode': 'CYPHER replan=force; exact record, limit 100; actual rows execute, only hashes persisted',
          'measurements': []}
with GraphDatabase.driver(os.environ['PLAYGROUND_NEO4J_URI'],
        auth=(os.environ['PLAYGROUND_NEO4J_USER'], os.environ['PLAYGROUND_NEO4J_PASSWORD']),
        max_transaction_retry_time=0, notifications_min_severity='OFF') as driver:
    with driver.session(database=os.getenv('PLAYGROUND_NEO4J_DATABASE', 'neo4j'), default_access_mode='READ') as session:
        scope = session.run('MATCH (h:KnowledgeRecordHead {tenant_id:$tenant_id,record_id:$record_id})-[:CURRENT_REVISION]->(r) RETURN r.access_groups AS groups,r.revision AS revision', tenant_id=tenant, record_id=record_id).single(strict=True)
        params = {'tenant_id': tenant, 'record_id': record_id, 'groups': sorted(scope['groups']), 'limit': 100}
        for kind in ReviewRecordKind:
            for variant, body in [('before', before[kind.value]), ('partitioned', _REVISION_HISTORY_QUERY[kind])]:
                query = 'CYPHER replan=force ' + body
                started = perf_counter()
                result = session.run(Query(query, timeout=90), **params)
                rows = [dict(row['revision']) for row in result]
                summary = result.consume()
                elapsed = perf_counter() - started
                entry = {'kind': kind.value, 'variant': variant, 'query_sha256': hashlib.sha256(query.encode()).hexdigest(),
                         'wall_seconds': round(elapsed, 3), 'server_ms': summary.result_available_after,
                         'row_count': len(rows), 'rows_sha256': hashlib.sha256(json.dumps(rows,sort_keys=True,default=str).encode()).hexdigest()}
                report['measurements'].append(entry)
                print(json.dumps(entry), flush=True)
        for kind in ReviewRecordKind:
            matching = [item for item in report['measurements'] if item['kind']==kind.value]
            assert matching[0]['rows_sha256']==matching[1]['rows_sha256']
        principal = Principal('context-history-read-only-inspection',tenant,frozenset(scope['groups']),frozenset({'knowledge:review'}))
        report['evidence_roles'] = []
        for role in ['PRIMARY','CONTEXT_VALUE']:
            started = perf_counter()
            response = session.execute_read(evidence_context_tx,principal,SimpleNamespace(record_id=record_id,
                expected_revision=scope['revision'],view='context',offset=0,evidence_role=role))
            entry = {'role': role, 'wall_seconds':round(perf_counter()-started,3),
                     'exact_range':len(response['quoted_text'])==response['char_end']-response['char_start'],
                     'quoted_chars':len(response['quoted_text'])}
            report['evidence_roles'].append(entry)
            print(json.dumps(entry),flush=True)
report['row_equivalence'] = True
(root / '.local/context-history-performance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
