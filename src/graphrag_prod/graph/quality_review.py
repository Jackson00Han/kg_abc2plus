"""Immutable human dispositions alongside (never replacing) automatic audit reports."""
from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from typing import Any
from neo4j import unit_of_work
from graphrag_prod.domain import Principal
from .browse_models import read_graph_state
from .published_quality import PublishedGraphQualityAuthorizationError
from .published_quality_history import PublishedGraphQualityHistoryConflict, PublishedGraphQualityHistoryUnavailable

def _payload(row):
    encoded=row['payload']
    if not isinstance(encoded,str) or len(encoded)>8000 or hashlib.sha256(encoded.encode()).hexdigest()!=row.get('checksum'):
        raise PublishedGraphQualityHistoryConflict()
    value=json.loads(encoded)
    if not isinstance(value,dict): raise PublishedGraphQualityHistoryConflict()
    return value


_DECISIONS = {'NEEDS_CORRECTION','NO_CHANGE_REQUIRED','PENDING'}
_NAMES = """
MATCH (p:KnowledgePublication {tenant_id:$tenant_id,publication_id:$publication_id})
      -[:PUBLISHES_KNOWLEDGE_REVISION]->(r)
WHERE r.tenant_id=$tenant_id AND any(g IN $groups WHERE g IN coalesce(r.access_groups,[]))
  AND (r.entity_id IN $ids OR r.revision_id IN $ids OR r.record_id IN $ids)
RETURN r.entity_id AS entity_id, r.revision_id AS revision_id,r.record_id AS record_id,
       r.canonical_name AS name,r.subject_canonical_name AS subject,r.predicate AS predicate
ORDER BY r.revision_id LIMIT 5001
"""


def review_identity(principal, operation_key):
    raw=json.dumps([principal.tenant_id,principal.principal_id,operation_key],ensure_ascii=False,separators=(',',':'))
    return 'quality-review:'+hashlib.sha256(raw.encode()).hexdigest()


class Neo4jQualityReviewService:
    def __init__(self, history):
        self.history=history
        self._read=unit_of_work(timeout=30.0,metadata={'component':'quality-review','operation':'read'})(self._list_tx)
        self._write=unit_of_work(timeout=30.0,metadata={'component':'quality-review','operation':'record'})(self._record_tx)

    @staticmethod
    def _authorize(principal,write=False):
        if 'knowledge:quality' not in principal.capabilities or (write and 'knowledge:review' not in principal.capabilities):
            raise PublishedGraphQualityAuthorizationError()

    def list(self,principal,run_id):
        self._authorize(principal)
        return self._execute(False,principal,run_id)

    def record(self,principal,request):
        self._authorize(principal,True)
        if request.decision not in _DECISIONS or not request.notes.strip():
            raise ValueError('quality decision requires an explanation')
        return self._execute(True,principal,request)

    def _execute(self,write,*args):
        try:
            with self.history.driver.session(database=self.history.database) as session:
                return (session.execute_write if write else session.execute_read)(self._write if write else self._read,*args)
        except (PublishedGraphQualityAuthorizationError,PublishedGraphQualityHistoryConflict):
            raise
        except Exception as error:
            raise PublishedGraphQualityHistoryUnavailable() from error

    def _run(self,tx,principal,run_id):
        result=self.history._get_tx(tx,principal,run_id)
        if result is None: raise PublishedGraphQualityHistoryConflict()
        return result

    def _list_tx(self,tx,principal,run_id):
        run=self._run(tx,principal,run_id)
        rows=list(tx.run('MATCH (r:QualityReviewDecision {tenant_id:$tenant_id,run_id:$run_id}) RETURN r.payload_json AS payload,r.payload_checksum AS checksum ORDER BY r.recorded_at DESC,r.review_id LIMIT 101',tenant_id=principal.tenant_id,run_id=run_id))
        ids=sorted({issue.object_id for issue in run.report.issues})
        names=list(tx.run(_NAMES,tenant_id=principal.tenant_id,publication_id=run.report.publication_id,groups=sorted(principal.groups),ids=ids))
        if len(names)>5000: raise PublishedGraphQualityHistoryConflict()
        labels={}
        for row in names:
            name=row.get('name') or ' · '.join(filter(None,(row.get('subject'),row.get('predicate'))))
            if name:
                for key in ('entity_id','revision_id','record_id'):
                    if row.get(key) in ids: labels.setdefault(row[key],name)
        if any(_payload(row).get('run_id')!=run_id for row in rows): raise PublishedGraphQualityHistoryConflict()
        again=self._run(tx,principal,run_id)
        if again.record_hash!=run.record_hash: raise PublishedGraphQualityHistoryConflict()
        return {'items':[_payload(row) for row in rows[:100]],'has_more':len(rows)>100,'object_labels':labels}

    def _record_tx(self,tx,principal,request):
        run=self._run(tx,principal,request.run_id)
        if request.issue_id not in {item.issue_id for item in run.report.issues}:
            raise PublishedGraphQualityHistoryConflict()
        identifier=review_identity(principal,request.operation_key)
        values=dict(review_id=identifier,run_id=request.run_id,issue_id=request.issue_id,decision=request.decision,notes=request.notes,recorded_by=principal.principal_id,publication_id=run.report.publication_id,publication_generation=run.report.publication_generation)
        existing=tx.run('MATCH (r:QualityReviewDecision {review_id:$id,tenant_id:$tenant_id}) RETURN r.payload_json AS payload,r.payload_checksum AS checksum',id=identifier,tenant_id=principal.tenant_id).single()
        if existing:
            payload=_payload(existing)
            if any(payload.get(k)!=v for k,v in values.items()): raise PublishedGraphQualityHistoryConflict()
            return payload
        # Hold the same publication-state lock used by activation. Re-read after
        # acquiring it: a concurrent activation may have committed while waiting.
        tx.run('MATCH (s:KnowledgePublicationState {tenant_id:$tenant_id}) SET s.activation_generation=s.activation_generation RETURN s.tenant_id',tenant_id=principal.tenant_id).consume()
        tx.run('MATCH (s:TenantCorpusState {tenant_id:$tenant_id}) SET s.corpus_revision=s.corpus_revision RETURN s.tenant_id',tenant_id=principal.tenant_id).consume()
        pin,_=read_graph_state(tx,principal.tenant_id)
        if (pin.publication_id,pin.publication_generation,pin.corpus_revision,pin.tbox_checksum)!=(run.report.publication_id,run.report.publication_generation,run.report.corpus_revision,run.report.tbox_checksum):
            raise PublishedGraphQualityHistoryConflict()
        values['recorded_at']=datetime.now(UTC).isoformat()
        encoded=json.dumps(values,ensure_ascii=False,sort_keys=True,separators=(',',':'))
        result=tx.run('MERGE (r:QualityReviewDecision {review_id:$id}) ON CREATE SET r.tenant_id=$tenant_id,r.run_id=$run_id,r.recorded_at=$at,r.payload_json=$payload,r.payload_checksum=$checksum RETURN r.tenant_id AS tenant_id,r.payload_json AS payload,r.payload_checksum AS checksum',id=identifier,tenant_id=principal.tenant_id,run_id=request.run_id,at=values['recorded_at'],payload=encoded,checksum=hashlib.sha256(encoded.encode()).hexdigest()).single()
        actual=_payload(result)
        if result['tenant_id']!=principal.tenant_id or any(actual.get(k)!=v for k,v in values.items() if k!='recorded_at'):
            raise PublishedGraphQualityHistoryConflict()
        self._run(tx,principal,request.run_id)
        return actual
