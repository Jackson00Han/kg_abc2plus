"""Prepare current publication search scope on the existing pump workbench.

Default is read-only validation. --apply adds the v3 fulltext index and derived
scope tokens; it does not publish, switch versions, or alter source content.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))

from neo4j import GraphDatabase, unit_of_work
from restart_workbench import private_environment
from graphrag_prod.domain import Principal
from graphrag_prod.knowledge.publication_sources import (
    load_publication_sources_tx, require_embedding_coverage_tx,
    prepare_publication_index_tx,
)
from graphrag_prod.knowledge.review import Neo4jKnowledgeReviewService


def prepare(tx, principal, apply):
    if apply:
        Neo4jKnowledgeReviewService._lock_tenant_corpus_tx(tx, principal.tenant_id, datetime.now(UTC))
    rows = list(tx.run('''
        MATCH (:KnowledgePublicationState {tenant_id:$tenant})-[:ACTIVE_KNOWLEDGE_PUBLICATION]->
          (p:KnowledgePublication {tenant_id:$tenant,status:'ACTIVE'})
        RETURN p{.*} AS publication LIMIT 2
    ''', tenant=principal.tenant_id))
    if len(rows)>1:
        raise RuntimeError('multiple current publications; refusing migration')
    if not rows:
        return {'status':'no_publication','documents':0,'chunks':0}
    properties=dict(rows[0]['publication'])
    sources=load_publication_sources_tx(tx,principal,properties)
    require_embedding_coverage_tx(tx,principal,sources,
        properties.get('embedding_space_id') or properties.get('legacy_embedding_space_id'))
    if apply:
        prepare_publication_index_tx(tx,principal,properties,bind_legacy=True)
    return {'status':'prepared' if apply else 'validated',
        'publication_id':properties['publication_id'],
        'documents':len(sources),'chunks':sum(len(s['chunks']) for s in sources)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tenant-id',required=True)
    parser.add_argument('--group',required=True,action='append')
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    cfg=private_environment()
    if cfg.get('PLAYGROUND_PUMP_ONLY')!='1':
        raise RuntimeError('pump-only workbench required')
    principal=Principal('local-publication-scope-preparation',args.tenant_id,
        frozenset(args.group),frozenset({'knowledge:publish'}))
    with GraphDatabase.driver(cfg['PLAYGROUND_NEO4J_URI'],
        auth=(cfg['PLAYGROUND_NEO4J_USER'],cfg['PLAYGROUND_NEO4J_PASSWORD'])) as driver:
        if args.apply:
            schema=(ROOT/'src/graphrag_prod/graph/migrations/014_publication_source_scope.cypher').read_text()
            driver.execute_query(schema,database_=cfg['PLAYGROUND_NEO4J_DATABASE'])
            driver.execute_query('CALL db.awaitIndex($name,60)',name='graphrag_chunk_text_v3',
                database_=cfg['PLAYGROUND_NEO4J_DATABASE'])
        with driver.session(database=cfg['PLAYGROUND_NEO4J_DATABASE'],
            default_access_mode='WRITE' if args.apply else 'READ') as session:
            work=unit_of_work(timeout=30,metadata={'component':'publication-scope-preparation'})(prepare)
            run=session.execute_write if args.apply else session.execute_read
            result=run(work,principal,args.apply)
        print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    main()
