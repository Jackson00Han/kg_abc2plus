"""Current, authorized source browsing independent of destructive lifecycle duties."""
from __future__ import annotations

from hashlib import sha256
from typing import Any

from neo4j import unit_of_work

from graphrag_prod.domain import Principal
from graphrag_prod.graph.browse_models import GraphViewChanged, GraphBrowseUnavailable
from graphrag_prod.ingestion.retirement import _LIST_ACTIVE_DOCUMENTS_QUERY

# Reuse the governed lifecycle's exact ownership, active-pointer and complete ACL
# boundary. Source reading grants no retirement or review capability.
_BOUNDARY = _LIST_ACTIVE_DOCUMENTS_QUERY.split('WITH document, snapshot, version')[0] + """
  AND ($document_id IS NULL OR document.document_id = $document_id)
  AND ($after IS NULL OR document.document_id > $after)
  AND ($version_id IS NULL OR version.version_id = $version_id)
  AND NOT EXISTS {
    MATCH (version)-[:HAS_CHUNK]->(member:Chunk)
    WHERE coalesce(member.access_policy_id,'') <> document.access_policy_id
       OR coalesce(member.access_policy_version,0) <> document.access_policy_version
       OR member.version_id <> version.version_id
  }
  AND (NOT document.canonical_uri STARTS WITH 'urn:graphrag:human:' OR (
    EXISTS {
      MATCH (:KnowledgeRecordHead {tenant_id:$tenant_id})-[:CURRENT_REVISION]->(hr)
      WHERE hr.version_id = version.version_id AND hr.origin = 'HUMAN_SUPPLEMENT'
    }
    AND NOT EXISTS {
      MATCH (:KnowledgeRecordHead {tenant_id:$tenant_id})-[:CURRENT_REVISION]->(hr)
      WHERE hr.version_id = version.version_id AND NOT EXISTS {
        MATCH (:KnowledgePublicationState {tenant_id:$tenant_id})-[:ACTIVE_KNOWLEDGE_PUBLICATION]->
          (:KnowledgePublication {tenant_id:$tenant_id,status:'ACTIVE'})-[:PUBLISHES_KNOWLEDGE_REVISION]->(hr)
        WHERE hr.origin='HUMAN_SUPPLEMENT' AND hr.authority_level='SECONDARY' AND hr.governance_status='PUBLISHED'
          AND any(g IN $principal_groups WHERE g IN coalesce(hr.access_groups,[]))
      }
    }
  ))
"""
_LIST = _BOUNDARY + """
WITH DISTINCT document, version, snapshot ORDER BY document.document_id LIMIT $limit
RETURN document.document_id AS document_id, document.title AS title,
 document.source_name AS source_name, document.canonical_uri AS canonical_uri,
 version.version_id AS version_id, version.version_number AS version_number,
 snapshot.expected_chunk_count AS chunk_count,
 EXISTS {
   MATCH (:KnowledgePublicationState {tenant_id:$tenant_id})-[:ACTIVE_KNOWLEDGE_PUBLICATION]->
    (:KnowledgePublication {tenant_id:$tenant_id,status:'ACTIVE'})-[:PUBLISHES_KNOWLEDGE_REVISION]->(r)
   WHERE r.version_id=version.version_id AND r.tenant_id=$tenant_id
     AND any(g IN $principal_groups WHERE g IN coalesce(r.access_groups,[]))
 } AS has_published_knowledge
ORDER BY document_id
"""
_CHUNK = _BOUNDARY + """
MATCH (version)-[:HAS_CHUNK]->(chunk:Chunk {tenant_id:$tenant_id,ordinal:$ordinal})
WHERE chunk.version_id=version.version_id AND chunk.document_id=document.document_id
 AND any(g IN $principal_groups WHERE g IN coalesce(chunk.access_groups,[]))
 AND chunk.char_start>=0 AND chunk.char_end>chunk.char_start
 AND chunk.char_end-chunk.char_start<=50000
 AND chunk.char_end<=size(version.normalized_text)
 AND chunk.text=substring(version.normalized_text,chunk.char_start,chunk.char_end-chunk.char_start)
RETURN document.document_id AS document_id, document.title AS title,
 document.source_name AS source_name, document.canonical_uri AS canonical_uri,
 version.version_id AS version_id,version.version_number AS version_number,
 snapshot.expected_chunk_count AS chunk_count,chunk.chunk_id AS chunk_id,
 chunk.ordinal AS ordinal,chunk.char_start AS char_start,chunk.char_end AS char_end,
 chunk.text AS text,chunk.checksum AS checksum,chunk.page_number AS page_number,chunk.section AS section
LIMIT 2
"""


class Neo4jSourceLibrary:
    def __init__(self, driver: Any, database: str = 'neo4j'):
        self.driver, self.database = driver, database
        self._work = unit_of_work(timeout=15.0, metadata={'component':'source-library','operation':'read'})(self._read_tx)

    def read(self, principal: Principal, *, document_id=None, version_id=None, ordinal=0, after=None, limit=30):
        if 'retrieval:read' not in principal.capabilities:
            raise PermissionError('source read requires retrieval permission')
        if type(limit) is not int or not 1 <= limit <= 100 or type(ordinal) is not int or not 0 <= ordinal <= 2**31-1:
            raise ValueError('invalid source read bounds')
        if any(value is not None and (not isinstance(value,str) or not 1<=len(value)<=256) for value in (document_id,version_id,after)) or (document_id is None)!=(version_id is None):
            raise ValueError('source detail requires a document and expected version')
        parameters=dict(tenant_id=principal.tenant_id,principal_groups=sorted(principal.groups),document_id=document_id,version_id=version_id,ordinal=ordinal,after=after,limit=limit+1)
        try:
            with self.driver.session(database=self.database) as session:
                return session.execute_read(self._work, parameters, document_id is not None, limit)
        except (GraphViewChanged,PermissionError):
            raise
        except Exception as error:
            raise GraphBrowseUnavailable('source read is temporarily unavailable') from error

    @staticmethod
    def _read_tx(tx, parameters, detail, limit):
        query = _CHUNK if detail else _LIST
        rows=[dict(row) for row in tx.run(query,**parameters)]
        if detail:
            if len(rows)!=1:
                raise GraphViewChanged()
            row=rows[0]
            if len(row['text'])!=row['char_end']-row['char_start'] or sha256(row['text'].encode()).hexdigest()!=row['checksum']:
                raise GraphViewChanged()
        # Neo4j read-committed isolation: check source/policy state again before return.
        if rows != [dict(row) for row in tx.run(query,**parameters)]:
            raise GraphViewChanged()
        return rows[0] if detail else dict(items=rows[:limit],has_more=len(rows)>limit,next_after=rows[limit-1]['document_id'] if len(rows)>limit else None)
