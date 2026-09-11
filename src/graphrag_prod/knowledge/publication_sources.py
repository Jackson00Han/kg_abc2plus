"""Immutable publication source manifests, with current authorization checks.

A release references existing immutable snapshots and chunks. Retiring a release
never retires its documents. Explicit source withdrawal remains a separate veto.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from graphrag_prod.domain.access import Principal, publication_retrieval_scope

MAX_SOURCE_SNAPSHOTS = 500
MAX_SOURCE_CHUNKS = 20_000
MAX_SOURCE_CHARACTERS = 5_000_000
MAX_CHUNK_CHARACTERS = 10_000_000

_SOURCE_QUERY = """
UNWIND $snapshot_ids AS snapshot_id
MATCH (s:KnowledgeSnapshot {tenant_id:$tenant_id,snapshot_id:snapshot_id})
      -[:OF_VERSION]->(v:DocumentVersion {tenant_id:$tenant_id})
MATCH (d:Document {tenant_id:$tenant_id})-[:HAS_VERSION]->(v)
WHERE s.document_id=d.document_id AND s.version_id=v.version_id
 AND v.document_id=d.document_id AND v.normalized_text IS NOT NULL
 AND v.checksum IS NOT NULL AND s.manifest_hash IS NOT NULL
 AND s.build_state IN ['PUBLISHED','RETIRED']
 AND coalesce(d.lifecycle_status,'ACTIVE')='ACTIVE'
 AND coalesce(v.lifecycle_status,'ACTIVE')='ACTIVE'
 AND d.retirement_id IS NULL AND s.retirement_id IS NULL AND v.retirement_id IS NULL
 AND d.retirement_request_fingerprint IS NULL
 AND d.retired_at IS NULL AND d.retired_by_principal_id IS NULL
 AND d.retired_active_snapshot_id IS NULL AND d.retired_active_version_id IS NULL
 AND s.retired_by_principal_id IS NULL
 AND v.retired_at IS NULL AND v.retired_by_principal_id IS NULL
 AND any(g IN $groups WHERE g IN coalesce(d.access_groups,[]))
 AND COUNT { MATCH (s)-[:OF_VERSION]->() }=1
 AND COUNT { MATCH (:Document)-[:HAS_VERSION]->(v) }=1
 AND s.expected_chunk_count>0 AND s.actual_chunk_count=s.expected_chunk_count
 AND COUNT { MATCH (s)-[:INCLUDES_CHUNK]->() }=s.expected_chunk_count
 AND COUNT { MATCH (v)-[:HAS_CHUNK]->() }=s.expected_chunk_count
 AND NOT EXISTS {
   MATCH (s)-[:INCLUDES_CHUNK]->(c)
   WHERE NOT c:Chunk OR coalesce(c.tenant_id,'')<>$tenant_id
      OR coalesce(c.document_id,'')<>d.document_id OR coalesce(c.version_id,'')<>v.version_id
      OR NOT any(g IN $groups WHERE g IN coalesce(c.access_groups,[]))
      OR coalesce(c.access_policy_id,'')<>coalesce(d.access_policy_id,'')
      OR c.access_policy_id IS NULL OR d.access_policy_id IS NULL
      OR coalesce(c.access_policy_version,-1)<>coalesce(d.access_policy_version,-2)
      OR c.access_policy_version IS NULL OR d.access_policy_version IS NULL
      OR c.access_groups<>d.access_groups
      OR NOT EXISTS { MATCH (v)-[:HAS_CHUNK]->(c) }
      OR COUNT { MATCH (:DocumentVersion)-[:HAS_CHUNK]->(c) }<>1
      OR c.char_start IS NULL OR c.char_end IS NULL OR c.text IS NULL OR c.checksum IS NULL
      OR c.char_start<0 OR c.char_end<=c.char_start
      OR c.char_end>size(v.normalized_text) OR c.char_end-c.char_start<>size(c.text)
      OR substring(v.normalized_text,c.char_start,c.char_end-c.char_start)<>c.text
 }
 AND NOT EXISTS {
   MATCH (v)-[:HAS_CHUNK]->(c) WHERE NOT EXISTS { MATCH (s)-[:INCLUDES_CHUNK]->(c) }
 }
MATCH (s)-[:INCLUDES_CHUNK]->(c:Chunk)
RETURN s.snapshot_id AS snapshot_id,d.document_id AS document_id,v.version_id AS version_id,
 s.manifest_hash AS snapshot_manifest_hash,v.checksum AS version_checksum,
 s.expected_chunk_count AS expected_chunk_count,d.title AS title,
 c.chunk_id AS chunk_id,c.checksum AS chunk_checksum,c.char_start AS char_start,c.char_end AS char_end,
 c.text AS chunk_text
ORDER BY snapshot_id,chunk_id LIMIT $limit
"""


def _conflict(message: str, code: str = "SOURCE_SCOPE_UNAVAILABLE") -> Exception:
    from graphrag_prod.domain.publication_issue import PublicationIssue
    from .review import KnowledgePublicationConflict
    return KnowledgePublicationConflict(message, issue=PublicationIssue(code))


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def source_manifest_hash(sources: tuple[dict, ...], embedding_space_id: str | None) -> str:
    return hashlib.sha256(canonical_json({"sources": sources, "embedding_space_id": embedding_space_id}).encode()).hexdigest()


def load_sources_tx(tx: Any, principal: Principal, snapshot_ids: tuple[str, ...]) -> tuple[dict, ...]:
    """Authorize every chunk before allowing a whole document into a release."""
    ids = tuple(sorted(snapshot_ids))
    if len(ids) > MAX_SOURCE_SNAPSHOTS or len(set(ids)) != len(ids):
        raise _conflict("publication source snapshot scope is invalid")
    if not ids:
        return ()
    closure = _SOURCE_QUERY.split("MATCH (s)-[:INCLUDES_CHUNK]->(c:Chunk)\nRETURN", 1)[0]
    budget = tx.run(closure + """
        MATCH (s)-[:INCLUDES_CHUNK]->(c:Chunk)
        WITH collect(DISTINCT {version_id:v.version_id,size:size(v.normalized_text)}) AS versions,
             sum(size(c.text)) AS chunk_characters,count(c) AS chunks
        RETURN reduce(total=0,item IN versions | total+item.size) AS source_characters,
               chunk_characters,chunks
    """, tenant_id=principal.tenant_id, groups=sorted(principal.groups), snapshot_ids=list(ids)).single()
    if (budget is None or budget["source_characters"] > MAX_SOURCE_CHARACTERS
            or budget["chunk_characters"] > MAX_CHUNK_CHARACTERS or budget["chunks"] > MAX_SOURCE_CHUNKS):
        raise _conflict("publication source exceeds its text validation budget")
    versions = tuple(tx.run(closure + """
        RETURN v.version_id AS version_id,v.checksum AS checksum,v.normalized_text AS text
    """, tenant_id=principal.tenant_id, groups=sorted(principal.groups), snapshot_ids=list(ids)))
    if any(hashlib.sha256(row["text"].encode("utf-8")).hexdigest() != row["checksum"] for row in versions):
        raise _conflict("publication document checksum does not match its immutable text")
    rows = tuple(tx.run(_SOURCE_QUERY, tenant_id=principal.tenant_id,
                        groups=sorted(principal.groups), snapshot_ids=list(ids), limit=MAX_SOURCE_CHUNKS + 1))
    if len(rows) > MAX_SOURCE_CHUNKS:
        raise _conflict("publication source scope exceeds its chunk budget")
    grouped: dict[str, dict] = {}
    expected_counts = {}
    for row in rows:
        if hashlib.sha256(row["chunk_text"].encode("utf-8")).hexdigest() != row["chunk_checksum"]:
            raise _conflict("publication chunk checksum does not match its immutable text")
        expected_counts[row["snapshot_id"]] = int(row["expected_chunk_count"])
        entry = grouped.setdefault(row["snapshot_id"], {
            "snapshot_id": row["snapshot_id"], "document_id": row["document_id"],
            "version_id": row["version_id"], "snapshot_manifest_hash": row["snapshot_manifest_hash"],
            "version_checksum": row["version_checksum"], "chunks": [],
        })
        entry["chunks"].append({"chunk_id": row["chunk_id"], "checksum": row["chunk_checksum"],
                                "char_start": row["char_start"], "char_end": row["char_end"]})
        if len(entry["chunks"]) > int(row["expected_chunk_count"]):
            raise _conflict("publication source contains duplicate chunk membership")
    if tuple(sorted(grouped)) != ids:
        raise _conflict("publication source is withdrawn, incomplete, or inaccessible")
    sources = tuple(grouped[key] for key in ids)
    if len({item["document_id"] for item in sources}) != len(sources):
        raise _conflict("one publication must use exactly one snapshot per document", "SOURCE_VERSION_CONFLICT")
    for item in sources:
        chunk_ids = [chunk["chunk_id"] for chunk in item["chunks"]]
        if len(chunk_ids) != expected_counts[item["snapshot_id"]] or len(set(chunk_ids)) != len(chunk_ids):
            raise _conflict("publication source contains duplicate chunks")
    return sources


def load_publication_sources_tx(tx: Any, principal: Principal, properties: dict) -> tuple[dict, ...]:
    """v3 reads only its recorded bindings; v4 additionally verifies its seal."""
    rows = tuple(tx.run("""
        MATCH (p:KnowledgePublication {tenant_id:$tenant_id,publication_id:$publication_id})
        OPTIONAL MATCH (p)-[binding:USES_KNOWLEDGE_SNAPSHOT]->(s)
        RETURN s.snapshot_id AS snapshot_id,s.tenant_id AS tenant_id,
               binding IS NOT NULL AS bound,
               [(p)-[:PUBLISHES_KNOWLEDGE_REVISION]->(r) | r.revision_id] AS revision_ids
        LIMIT $limit
    """, tenant_id=principal.tenant_id, publication_id=properties["publication_id"], limit=MAX_SOURCE_SNAPSHOTS + 1))
    if not rows or any(row.get("bound", row["snapshot_id"] is not None) and row["snapshot_id"] is None for row in rows):
        raise _conflict("publication source binding is malformed")
    if any(sorted(row.get("revision_ids", ())) != sorted(properties.get("published_revision_ids", ())) for row in rows):
        raise _conflict("publication revision bindings differ from its manifest")
    bound = tuple(row["snapshot_id"] for row in rows if row["snapshot_id"] is not None)
    if any(row["snapshot_id"] is not None and row["tenant_id"] != principal.tenant_id for row in rows):
        raise _conflict("publication source crosses a tenant boundary")
    sources = load_sources_tx(tx, principal, bound)
    if properties.get("manifest_version", 3) not in {3, 4}:
        raise _conflict("publication uses an unsupported legacy source manifest")
    outer = {
        "tenant_id": principal.tenant_id, "ontology_version_id": properties.get("ontology_version_id"),
        "base_publication_id": properties.get("base_publication_id"),
        "source_revision_ids": properties.get("source_revision_ids", []),
        "published_revision_ids": properties.get("published_revision_ids", []),
        "removed_record_ids": properties.get("removed_record_ids", []),
        "replaced_record_ids": properties.get("replaced_record_ids", []),
        "snapshot_ids": sorted(bound),
    }
    if properties.get("manifest_version", 3) >= 4:
        outer.update(source_manifest_hash=properties.get("source_manifest_hash"), embedding_space_id=properties.get("embedding_space_id"))
    if hashlib.sha256(canonical_json(outer).encode()).hexdigest() != properties.get("manifest_hash"):
        raise _conflict("publication manifest hash no longer matches its source and revision bindings")
    if properties.get("manifest_version", 3) >= 4:
        encoded = canonical_json(sources)
        if (properties.get("source_manifest_json") != encoded
                or tuple(properties.get("source_snapshot_ids", ())) != tuple(item["snapshot_id"] for item in sources)
                or properties.get("source_manifest_hash") != source_manifest_hash(sources, properties.get("embedding_space_id"))
                or properties.get("source_document_count") != len(sources)
                or properties.get("source_chunk_count") != sum(len(item["chunks"]) for item in sources)):
            raise _conflict("publication source manifest no longer matches its immutable bindings")
    return sources


def require_embedding_coverage_tx(tx: Any, principal: Principal, sources: tuple[dict, ...],
                                  expected_space: str | None = None) -> str | None:
    """Use the current compatible index configuration, never downgrade it."""
    if not sources:
        return expected_space
    rows = tuple(tx.run("""
        MATCH (state:TenantCorpusState {tenant_id:$tenant_id})-[:ACTIVE_EMBEDDING_INDEX]->
              (g:EmbeddingIndexGeneration {tenant_id:$tenant_id,state:'ACTIVE'})
        WHERE g.corpus_revision=state.corpus_revision
        RETURN g.embedding_space_id AS space,g.dimensions AS dimensions LIMIT 2
    """, tenant_id=principal.tenant_id))
    if len(rows) != 1 or not rows[0]["space"] or not rows[0]["dimensions"]:
        raise _conflict("publication index is not ready; prepare embeddings before publishing or restoring", "INDEX_NOT_READY")
    space = rows[0]["space"]
    if expected_space is not None and space != expected_space:
        raise _conflict("publication embedding space does not match the current query configuration", "EMBEDDING_SPACE_CHANGED")
    chunk_ids = [chunk["chunk_id"] for source in sources for chunk in source["chunks"]]
    row = tx.run("""
        UNWIND $chunk_ids AS chunk_id
        MATCH (c:Chunk {tenant_id:$tenant_id,chunk_id:chunk_id})
        WHERE COUNT {
            MATCH (c)-[:HAS_EMBEDDING]->(e:ChunkEmbedding {tenant_id:$tenant_id,embedding_space_id:$space})
            WHERE e.chunk_id=c.chunk_id AND e.dimensions=$dimensions AND e.cosine_indexable=true
              AND size(e.vector)=$dimensions AND any(x IN e.vector WHERE x<>0.0)
              AND all(x IN e.vector WHERE x IS NOT NULL AND x=x AND abs(x)<=1.7976931348623157e308)
        }=1
          AND COUNT { MATCH (c)-[:HAS_EMBEDDING]->(:ChunkEmbedding {tenant_id:$tenant_id,embedding_space_id:$space}) }=1
        RETURN count(DISTINCT c) AS covered
    """, tenant_id=principal.tenant_id, chunk_ids=chunk_ids, space=space,
        dimensions=int(rows[0]["dimensions"])).single()
    if row is None or row["covered"] != len(chunk_ids):
        raise _conflict("publication index does not cover every source chunk", "INDEX_NOT_READY")
    return space


def index_publication_sources_tx(tx: Any, principal: Principal, sources: tuple[dict, ...]) -> None:
    """Derived search tokens persist for historical source versions as well."""
    chunk_ids = [chunk["chunk_id"] for source in sources for chunk in source["chunks"]]
    if not chunk_ids:
        return
    rows = tuple(tx.run("""
        UNWIND $chunk_ids AS chunk_id MATCH (c:Chunk {tenant_id:$tenant_id,chunk_id:chunk_id})
        RETURN c.chunk_id AS chunk_id,c.version_id AS version_id,c.access_groups AS groups
    """, tenant_id=principal.tenant_id, chunk_ids=chunk_ids))
    updates = [{"chunk_id": row["chunk_id"], "scope": publication_retrieval_scope(
        principal.tenant_id, row["version_id"], frozenset(row["groups"]))} for row in rows]
    tx.run("""
        UNWIND $rows AS row MATCH (c:Chunk {tenant_id:$tenant_id,chunk_id:row.chunk_id})
        SET c.publication_scope=row.scope
    """, tenant_id=principal.tenant_id, rows=updates).consume()


def source_summaries_tx(tx: Any, principal: Principal, sources: tuple[dict, ...]) -> list[dict]:
    if not sources:
        return []
    rows = tx.run("""
        UNWIND $sources AS item MATCH (d:Document {tenant_id:$tenant_id,document_id:item.document_id})
        WHERE any(g IN $groups WHERE g IN d.access_groups)
        RETURN d.document_id AS document_id,item.version_id AS version_id,d.title AS title
        ORDER BY document_id,version_id
    """, sources=[{"document_id": s["document_id"], "version_id": s["version_id"]} for s in sources],
        tenant_id=principal.tenant_id, groups=sorted(principal.groups))
    return [dict(row) for row in rows]


def compare_source_summaries(before: list[dict], after: list[dict]) -> dict:
    old = {(row["document_id"], row["version_id"]): row for row in before}
    new = {(row["document_id"], row["version_id"]): row for row in after}
    return {"added": [new[key] for key in sorted(new.keys() - old.keys())],
            "removed": [old[key] for key in sorted(old.keys() - new.keys())],
            "unchanged_count": len(old.keys() & new.keys())}


def prepare_publication_index_tx(tx: Any, principal: Principal, properties: dict,
                                 *, bind_legacy: bool = False) -> None:
    """Validate a release for serving and rebuild only derived search scope.

    Legacy releases acquire an explicit compatibility binding on activation;
    their original manifest and hash remain unchanged.
    """
    sources = load_publication_sources_tx(tx, principal, properties)
    space = require_embedding_coverage_tx(tx, principal, sources,
        properties.get("embedding_space_id") or properties.get("legacy_embedding_space_id"))
    index_publication_sources_tx(tx, principal, sources)
    if bind_legacy and properties.get("manifest_version", 3) < 4 and space is not None:
        tx.run("""
            MATCH (p:KnowledgePublication {tenant_id:$tenant_id,publication_id:$publication_id})
            SET p.legacy_embedding_space_id=coalesce(p.legacy_embedding_space_id,$space)
        """, tenant_id=principal.tenant_id, publication_id=properties["publication_id"], space=space).consume()
