"""Read-only, bounded business differences between two fully authorized publications."""
from hashlib import sha256
from neo4j import Query
from graphrag_prod.knowledge.review import KnowledgePublicationConflict
from .publication_sources import compare_source_summaries

_QUERY = """
MATCH (p:KnowledgePublication {tenant_id:$tenant_id})-[:PUBLISHES_KNOWLEDGE_REVISION]->(r)
MATCH (r)-[:IN_CHUNK|EVIDENCED_BY]->(c:Chunk {tenant_id:$tenant_id})
MATCH (d:Document {tenant_id:$tenant_id,document_id:r.document_id})-[:HAS_VERSION]->(v:DocumentVersion {tenant_id:$tenant_id,version_id:r.version_id})
MATCH (p)-[:USES_KNOWLEDGE_SNAPSHOT]->(s:KnowledgeSnapshot {tenant_id:$tenant_id})-[:INCLUDES_CHUNK]->(c)
MATCH (s)-[:OF_VERSION]->(v)
WHERE p.publication_id IN $ids AND r.tenant_id=$tenant_id
 AND s.build_state IN ['PUBLISHED','RETIRED']
 AND coalesce(d.lifecycle_status,'ACTIVE')='ACTIVE' AND coalesce(v.lifecycle_status,'ACTIVE')='ACTIVE'
 AND d.retirement_id IS NULL AND s.retirement_id IS NULL AND v.retirement_id IS NULL
 AND any(g IN $groups WHERE g IN coalesce(r.access_groups,[]))
 AND any(g IN $groups WHERE g IN coalesce(c.access_groups,[]))
 AND any(g IN $groups WHERE g IN coalesce(d.access_groups,[]))
 AND r.access_policy_id=c.access_policy_id AND r.access_policy_version=c.access_policy_version AND r.access_groups=c.access_groups
 AND c.version_id=v.version_id AND c.document_id=d.document_id
 AND r.evidence_char_start>=c.char_start AND r.evidence_char_end<=c.char_end
 AND substring(c.text,r.evidence_char_start-c.char_start,r.evidence_char_end-r.evidence_char_start)=r.evidence_text
RETURN p.publication_id AS publication_id,r.revision_id AS revision_id,r.record_id AS record_id,
 CASE WHEN r:GovernedEntityMentionRevision THEN 'ENTITY_MENTION' ELSE 'ASSERTION' END AS record_kind,
 coalesce(r.canonical_name,r.subject_canonical_name,'') AS subject_name,
 r.predicate AS predicate,r.object_canonical_name AS object_name,r.literal_value AS literal_value,
 r.literal_raw_unit AS unit,r.literal_raw_valid_from AS valid_from,r.literal_raw_valid_to AS valid_to,r.literal_raw_observed_at AS observed_at,
 d.title AS document_title,r.relationship_properties_json AS qualifiers
ORDER BY p.publication_id,r.record_id LIMIT 1001
"""


_SOURCE_QUERY = """
MATCH (p:KnowledgePublication {tenant_id:$tenant_id})-[:USES_KNOWLEDGE_SNAPSHOT]->(s:KnowledgeSnapshot {tenant_id:$tenant_id})
MATCH (s)-[:OF_VERSION]->(v:DocumentVersion {tenant_id:$tenant_id})
MATCH (d:Document {tenant_id:$tenant_id})-[:HAS_VERSION]->(v)
WHERE p.publication_id IN $ids AND any(g IN $groups WHERE g IN coalesce(d.access_groups,[]))
RETURN p.publication_id AS publication_id,s.snapshot_id AS snapshot_id,
       d.document_id AS document_id,v.version_id AS version_id,d.title AS title
ORDER BY publication_id,document_id,version_id LIMIT 1001
"""


def compare_records(before,after):
    old={r['record_id']:r for r in before};new={r['record_id']:r for r in after}
    if len(old)!=len(before) or len(new)!=len(after):raise KnowledgePublicationConflict('duplicate comparison record')
    changed=[{'before':old[key],'after':new[key]} for key in sorted(old.keys() & new.keys()) if old[key]!=new[key]]
    return dict(added=[new[key] for key in sorted(new.keys()-old.keys())],removed=[old[key] for key in sorted(old.keys()-new.keys())],changed=changed,unchanged_count=len(old.keys() & new.keys())-len(changed))


def publication_comparison(service,principal,target_id,expected_id):
    publications=service.history(principal,limit=100)
    by_id={p.publication_id:p for p in publications}
    if target_id not in by_id or expected_id not in by_id or by_id[expected_id].status!='ACTIVE':
        raise KnowledgePublicationConflict('publication comparison scope changed')
    selected={key:by_id[key] for key in {target_id,expected_id}}
    if any(len(p.published_revision_ids)>500 for p in selected.values()):
        raise KnowledgePublicationConflict('publication comparison exceeds its safety bound')
    params=dict(tenant_id=principal.tenant_id,groups=sorted(principal.groups),ids=sorted(selected))
    with service.driver.session(database=service.database) as session:
        rows=[dict(r) for r in session.run(Query(_QUERY,timeout=15.0),**params)]
        source_rows=[dict(r) for r in session.run(Query(_SOURCE_QUERY,timeout=15.0),**params)]
    if len(rows)>1000:raise KnowledgePublicationConflict('publication comparison exceeds its safety bound')
    for key,p in selected.items():
        actual=[r['revision_id'] for r in rows if r['publication_id']==key]
        if set(actual)!=set(p.published_revision_ids) or len(actual)!=len(p.published_revision_ids):
            raise KnowledgePublicationConflict('publication comparison is incomplete')
    for key,p in selected.items():
        actual=[r['snapshot_id'] for r in source_rows if r['publication_id']==key]
        if sorted(actual)!=sorted(p.source_snapshot_ids):
            raise KnowledgePublicationConflict('publication comparison source scope is incomplete')
    with service.driver.session(database=service.database) as session:
        source_again=[dict(r) for r in session.run(Query(_SOURCE_QUERY,timeout=15.0),**params)]
        again=[dict(r) for r in session.run(Query(_QUERY,timeout=15.0),**params)]
    if source_again!=source_rows or again!=rows or service.history(principal,limit=100)!=publications:
        raise KnowledgePublicationConflict('publication comparison changed during read')
    groups={key:[] for key in selected}
    for row in rows:
        key=row.pop('publication_id');qualifiers=row.pop('qualifiers') or ''
        row['qualifier_digest']=sha256(qualifiers.encode()).hexdigest()
        groups[key].append(row)
    source_groups={key:[] for key in selected}
    for row in source_rows:
        source_groups[row['publication_id']].append({name:row[name] for name in ('document_id','version_id','title')})
    return dict(source_scope=compare_source_summaries(source_groups[expected_id],source_groups[target_id]),expected_active_publication_id=expected_id,target_publication_id=target_id,target_generation=by_id[target_id].generation,**compare_records(groups[expected_id],groups[target_id]))
