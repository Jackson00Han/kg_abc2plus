"""Bounded, read-only duplicate-upload advice over currently accessible sources.

Exact checks use the existing immutable version checksums. Near-duplicate
advice uses ordinary character shingles and Jaccard similarity; it never makes
an identity, source-authority, publication, or document-merge decision.
"""

from __future__ import annotations

import math
from typing import Any

from neo4j import unit_of_work

from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import canonicalize_uri, content_checksum

from .parser import BoundedDocumentParser


MAX_EXACT_MATCHES = 20
MAX_SIMILARITY_CANDIDATES = 50
MAX_SIMILARITY_CHARACTERS = 200_000
SIMILARITY_THRESHOLD = 0.85
SHINGLE_LENGTH = 5
DIFFERENCE_CHARACTERS = 160
SIMILARITY_METHOD = "character-5-shingle-jaccard-v1"


class UploadPreflightUnavailable(RuntimeError):
    """The authorized source view changed or could not be verified."""


# DocumentVersion and KnowledgeSnapshot inherit source authorization through
# their one Document and complete Chunk membership; they have no independent
# ACL properties. Superseded sources remain eligible only when pinned by the
# CURRENT publication. A source withdrawal always overrides that pin.
_BOUNDARY = """
MATCH (document:Document {tenant_id: $tenant_id})-[:HAS_VERSION]->
      (version:DocumentVersion {tenant_id: $tenant_id})
MATCH (snapshot:KnowledgeSnapshot {tenant_id: $tenant_id})-[:OF_VERSION]->(version)
WHERE version.document_id = document.document_id
  AND snapshot.document_id = document.document_id
  AND snapshot.version_id = version.version_id
  AND version.normalized_text IS NOT NULL AND version.checksum IS NOT NULL
  AND version.original_checksum IS NOT NULL
  AND coalesce(document.lifecycle_status, 'ACTIVE') = 'ACTIVE'
  AND coalesce(version.lifecycle_status, 'ACTIVE') = 'ACTIVE'
  AND document.retirement_id IS NULL AND document.retired_at IS NULL
  AND document.retirement_request_fingerprint IS NULL
  AND document.retired_by_principal_id IS NULL
  AND document.retired_active_snapshot_id IS NULL
  AND document.retired_active_version_id IS NULL
  AND version.retirement_id IS NULL AND version.retired_at IS NULL
  AND version.retired_by_principal_id IS NULL
  AND snapshot.retirement_id IS NULL AND snapshot.retired_by_principal_id IS NULL
  AND snapshot.build_state IN ['PUBLISHED', 'RETIRED']
  AND any(g IN $principal_groups WHERE g IN coalesce(document.access_groups, []))
  AND ((snapshot.build_state = 'PUBLISHED'
        AND EXISTS { MATCH (document)-[:ACTIVE_SNAPSHOT]->(snapshot) }
        AND EXISTS { MATCH (document)-[:ACTIVE_VERSION]->(version) })
       OR EXISTS {
         MATCH (:KnowledgePublicationState {tenant_id: $tenant_id})
           -[:ACTIVE_KNOWLEDGE_PUBLICATION]->
           (:KnowledgePublication {tenant_id: $tenant_id, status: 'ACTIVE'})
           -[:USES_KNOWLEDGE_SNAPSHOT]->(snapshot)
       })
  AND COUNT { MATCH (snapshot)-[:OF_VERSION]->() } = 1
  AND COUNT { MATCH (:Document)-[:HAS_VERSION]->(version) } = 1
  AND snapshot.expected_chunk_count > 0
  AND snapshot.actual_chunk_count = snapshot.expected_chunk_count
  AND COUNT { MATCH (snapshot)-[:INCLUDES_CHUNK]->() } = snapshot.expected_chunk_count
  AND COUNT { MATCH (version)-[:HAS_CHUNK]->() } = snapshot.expected_chunk_count
  AND NOT EXISTS {
    MATCH (snapshot)-[:INCLUDES_CHUNK]->(chunk)
    WHERE NOT chunk:Chunk OR coalesce(chunk.tenant_id, '') <> $tenant_id
       OR coalesce(chunk.document_id, '') <> document.document_id
       OR coalesce(chunk.version_id, '') <> version.version_id
       OR coalesce(chunk.lifecycle_status, 'ACTIVE') <> 'ACTIVE'
       OR chunk.retirement_id IS NOT NULL OR chunk.retired_at IS NOT NULL
       OR chunk.retired_by_principal_id IS NOT NULL
       OR NOT any(g IN $principal_groups WHERE g IN coalesce(chunk.access_groups, []))
       OR chunk.access_groups <> document.access_groups
       OR chunk.access_policy_id IS NULL OR document.access_policy_id IS NULL
       OR chunk.access_policy_id <> document.access_policy_id
       OR chunk.access_policy_version IS NULL OR document.access_policy_version IS NULL
       OR chunk.access_policy_version <> document.access_policy_version
       OR NOT EXISTS { MATCH (version)-[:HAS_CHUNK]->(chunk) }
       OR COUNT { MATCH (:DocumentVersion)-[:HAS_CHUNK]->(chunk) } <> 1
       OR chunk.ordinal IS NULL OR chunk.ordinal < 0
       OR chunk.ordinal >= snapshot.expected_chunk_count
       OR chunk.char_start IS NULL OR chunk.char_end IS NULL
       OR chunk.text IS NULL OR chunk.checksum IS NULL
       OR chunk.char_start < 0 OR chunk.char_end <= chunk.char_start
       OR chunk.char_end > size(version.normalized_text)
       OR chunk.char_end - chunk.char_start <> size(chunk.text)
       OR substring(version.normalized_text, chunk.char_start,
                    chunk.char_end - chunk.char_start) <> chunk.text
       OR (chunk.ordinal = 0 AND chunk.char_start <> 0)
       OR (chunk.ordinal = snapshot.expected_chunk_count - 1
           AND chunk.char_end <> size(version.normalized_text))
       OR (chunk.ordinal > 0 AND NOT EXISTS {
           MATCH (snapshot)-[:INCLUDES_CHUNK]->(previous:Chunk)
           WHERE previous.ordinal = chunk.ordinal - 1
             AND previous.char_end = chunk.char_start
       })
       OR EXISTS {
           MATCH (snapshot)-[:INCLUDES_CHUNK]->(other:Chunk)
           WHERE other <> chunk AND other.ordinal = chunk.ordinal
       }
  }
  AND NOT EXISTS {
    MATCH (version)-[:HAS_CHUNK]->(member)
    WHERE NOT EXISTS { MATCH (snapshot)-[:INCLUDES_CHUNK]->(member) }
  }
"""

_METADATA = """
WITH DISTINCT document, version, snapshot ORDER BY snapshot.snapshot_id
WITH document, version, collect(snapshot.snapshot_id) AS _snapshot_ids
RETURN document.document_id AS document_id, version.version_id AS version_id,
       document.title AS title, document.canonical_uri AS canonical_uri,
       version.checksum AS checksum, version.original_checksum AS original_checksum,
       size(version.normalized_text) AS characters,
       document.access_policy_id AS _policy_id,
       document.access_policy_version AS _policy_version,
       document.access_groups AS _groups,
       coalesce(document.generation, 0) AS _generation, _snapshot_ids
ORDER BY CASE WHEN canonical_uri = $canonical_uri THEN 0 ELSE 1 END,
         document_id, version_id
LIMIT $limit
"""

_EXACT = _BOUNDARY + """
  AND (version.checksum = $checksum OR version.original_checksum = $original_checksum)
""" + _METADATA

_SIMILAR = _BOUNDARY + """
  AND version.checksum <> $checksum AND version.original_checksum <> $original_checksum
  AND size(version.normalized_text) >= $minimum_characters
  AND size(version.normalized_text) <= $maximum_characters
  AND size(version.normalized_text) <= $maximum_similarity_characters
""" + _METADATA

_TEXT = _BOUNDARY + """
  AND version.version_id IN $version_ids
  AND size(version.normalized_text) <= $maximum_similarity_characters
WITH DISTINCT version
RETURN version.version_id AS version_id, version.checksum AS checksum,
       version.normalized_text AS text
ORDER BY version_id
LIMIT $text_limit
"""


def _shingles(text: str) -> set[str]:
    if len(text) < SHINGLE_LENGTH:
        return {text} if text else set()
    return {text[start:start + SHINGLE_LENGTH]
            for start in range(len(text) - SHINGLE_LENGTH + 1)}


def _jaccard(left: set[str], right: set[str]) -> float:
    """The standard set intersection/union coefficient, without weighting."""
    intersection = len(left.intersection(right))
    union = len(left) + len(right) - intersection
    return intersection / union if union else 1.0


def _first_difference(before: str, after: str) -> dict[str, str] | None:
    """A linear first-difference window, preserving literal digits and units."""
    if before == after:
        return None
    index = 0
    while index < min(len(before), len(after)) and before[index] == after[index]:
        index += 1
    width = DIFFERENCE_CHARACTERS // 2
    start = max(0, index - width // 3)
    return {"before": before[start:start + width], "after": after[start:start + width]}


def _match(row: dict[str, Any], kind: str, similarity: float,
           difference: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "document_id": row["document_id"],
        "version_id": row["version_id"],
        "title": row["title"] or row["document_id"],
        "canonical_uri": row["canonical_uri"],
        "match_kind": kind,
        "similarity": similarity,
        "difference": difference,
    }


class Neo4jUploadPreflight:
    """Check visible current/published versions before expensive construction."""

    def __init__(self, driver: Any, database: str = "neo4j",
                 parser: BoundedDocumentParser | None = None) -> None:
        self.driver = driver
        self.database = database
        self.parser = parser or BoundedDocumentParser()
        self._work = unit_of_work(
            timeout=15.0,
            metadata={"component": "upload-preflight", "operation": "read"},
        )(self._check_tx)

    def check(self, principal: Principal, content: bytes, *, canonical_uri: str,
              mime_type: str = "text/plain") -> dict[str, Any]:
        if not isinstance(principal, Principal):
            raise TypeError("principal must be an authenticated Principal")
        if "knowledge:construct" not in principal.capabilities:
            raise PermissionError("upload preflight requires knowledge:construct")
        uri = canonicalize_uri(canonical_uri)
        parsed = self.parser.parse(content, mime_type=mime_type)
        characters = len(parsed.normalized_text)
        parameters = {
            "tenant_id": principal.tenant_id,
            "principal_groups": sorted(principal.groups),
            "canonical_uri": uri,
            "checksum": parsed.normalized_checksum,
            "original_checksum": parsed.original_checksum,
            "minimum_characters": math.ceil(characters * 0.8),
            "maximum_characters": math.floor(characters * 1.25),
            "maximum_similarity_characters": MAX_SIMILARITY_CHARACTERS,
        }
        with self.driver.session(database=self.database) as session:
            return session.execute_read(self._work, parameters, parsed.normalized_text)

    @staticmethod
    def _check_tx(tx: Any, parameters: dict[str, Any], text: str) -> dict[str, Any]:
        def read(query: str, **extra: Any) -> list[dict[str, Any]]:
            return [dict(row) for row in tx.run(query, **parameters, **extra)]

        exact = read(_EXACT, limit=MAX_EXACT_MATCHES + 1)
        reasons = []
        if len(exact) > MAX_EXACT_MATCHES:
            reasons.append("EXACT_RESULT_LIMIT")
        similarity_checked = len(text) <= MAX_SIMILARITY_CHARACTERS
        candidates = []
        matches = []
        if similarity_checked:
            candidates = read(_SIMILAR, limit=MAX_SIMILARITY_CANDIDATES + 1)
            if len(candidates) > MAX_SIMILARITY_CANDIDATES:
                reasons.append("SIMILARITY_CANDIDATE_LIMIT")
            selected = candidates[:MAX_SIMILARITY_CANDIDATES]
            if selected:
                rows = read(
                    _TEXT, version_ids=[row["version_id"] for row in selected],
                    text_limit=MAX_SIMILARITY_CANDIDATES,
                )
                by_id = {row["version_id"]: row for row in rows}
                if len(by_id) != len(selected):
                    raise UploadPreflightUnavailable("upload comparison sources changed")
                shingles = _shingles(text)
                for candidate in selected:
                    original = by_id.get(candidate["version_id"])
                    if (original is None or not isinstance(original["text"], str)
                            or len(original["text"]) > MAX_SIMILARITY_CHARACTERS
                            or original["checksum"] != candidate["checksum"]
                            or content_checksum(original["text"]) != candidate["checksum"]):
                        raise UploadPreflightUnavailable("upload comparison source is invalid")
                    similarity = _jaccard(shingles, _shingles(original["text"]))
                    if similarity >= SIMILARITY_THRESHOLD:
                        matches.append(_match(candidate, "SIMILAR", similarity,
                                              _first_difference(original["text"], text)))
            matches.sort(key=lambda row: (-row["similarity"], row["document_id"], row["version_id"]))
        else:
            reasons.append("INPUT_SIMILARITY_CHARACTER_LIMIT")

        # Read-committed transactions can observe an intervening ACL change or
        # withdrawal. Recheck the complete authorized selection before exposing
        # any document metadata, counts, scores, or short source excerpts.
        if exact != read(_EXACT, limit=MAX_EXACT_MATCHES + 1):
            raise UploadPreflightUnavailable("upload comparison sources changed")
        if similarity_checked and candidates != read(_SIMILAR, limit=MAX_SIMILARITY_CANDIDATES + 1):
            raise UploadPreflightUnavailable("upload comparison sources changed")
        return {
            "checksum": parameters["checksum"],
            "original_checksum": parameters["original_checksum"],
            "exact_matches": [_match(row, "EXACT", 1.0) for row in exact[:MAX_EXACT_MATCHES]],
            "similar_matches": matches,
            "similarity_checked": similarity_checked,
            "truncated": bool(reasons),
            "truncation_reasons": reasons,
            "compared_versions": min(len(candidates), MAX_SIMILARITY_CANDIDATES),
            "method": SIMILARITY_METHOD,
            "threshold": SIMILARITY_THRESHOLD,
        }
