"""Governed, audit-preserving logical retirement for active documents.

The legacy ingestion ``DELETE`` operation intentionally removes source and
derived nodes.  This module implements the different lifecycle operation used
by governed corpora: it removes only active retrieval pointers and indexed
scope, while retaining every source version, Chunk, extraction, review
revision, and publication record for audit.

Retirement is deliberately server-scoped.  Tenant identity and access groups
come exclusively from :class:`~graphrag_prod.domain.access.Principal`; callers
cannot supply an alternate tenant or ACL policy.  One managed write
transaction locks the tenant corpus and publication state, rechecks all
authorization, compare-and-swap, and governance blockers, then advances the
corpus revision together with vector-generation invalidation.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from neo4j import unit_of_work

from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import ingestion_job_id

DOCUMENT_LIFECYCLE_CAPABILITY = "knowledge:lifecycle"
DOCUMENT_RETIREMENT_OPERATION = "RETIRE"
DEFAULT_RETIREMENT_TRANSACTION_TIMEOUT_SECONDS = 30.0

_BLOCKING_REVIEW_STATUSES = ("APPROVED", "CANDIDATE", "QUARANTINED")
_ACTIVE_DOCUMENT_BLOCKERS = (
    ("active_publication_reference", "ACTIVE_KNOWLEDGE_PUBLICATION"),
    ("reviewable_revision_reference", "CURRENT_REVIEW"),
    ("active_construction_reference", "ACTIVE_CONSTRUCTION_JOB"),
    ("active_ingestion_reference", "ACTIVE_INGESTION_JOB"),
)
_MAX_SAFE_TEXT = 512
MAX_ACTIVE_DOCUMENT_LIST_LIMIT = 100


class SessionDriver(Protocol):
    def session(self, **kwargs: object) -> Any: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class DocumentRetirementError(RuntimeError):
    """Base for sanitized retirement failures."""

    code = "DOCUMENT_RETIREMENT_ERROR"


class DocumentRetirementUnavailable(DocumentRetirementError, PermissionError):
    """The target is absent, cross-tenant, incompletely visible, or forbidden."""

    code = "DOCUMENT_RETIREMENT_UNAVAILABLE"

    def __init__(self) -> None:
        # One message intentionally covers missing capability, absent IDs,
        # cross-tenant IDs, and partial ACL visibility to prevent an oracle.
        super().__init__("document retirement target is unavailable")


class DocumentRetirementConflict(DocumentRetirementError):
    """The requested CAS or immutable idempotency binding no longer matches."""

    code = "DOCUMENT_RETIREMENT_CONFLICT"

    def __init__(self) -> None:
        super().__init__("document retirement state is conflicted")


class DocumentRetirementBlocked(DocumentRetirementError):
    """Live governed knowledge still depends on the document."""

    code = "DOCUMENT_RETIREMENT_BLOCKED"

    def __init__(self) -> None:
        super().__init__(
            "document retirement is blocked by active or reviewable knowledge"
        )


class DocumentRetirementBackendUnavailable(DocumentRetirementError):
    """A backend failure whose public text never includes driver details."""

    code = "DOCUMENT_RETIREMENT_BACKEND_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("document retirement is temporarily unavailable")


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > _MAX_SAFE_TEXT
        or any(character in normalized for character in ("\x00", "\r", "\n"))
    ):
        raise ValueError(f"{name} is outside its safe text boundary")
    return normalized


def _aware(value: object, name: str) -> datetime:
    to_native = getattr(value, "to_native", None)
    if callable(to_native):
        value = to_native()
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be a timezone-aware datetime") from exc
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value


def _fingerprint(value: dict[str, object]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DocumentRetirementRequest:
    """Client CAS values for one tenant-derived retirement operation."""

    document_id: str
    operation_key: str
    expected_active_snapshot_id: str
    source_generation: int

    def __post_init__(self) -> None:
        for field_name in (
            "document_id",
            "operation_key",
            "expected_active_snapshot_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), field_name),
            )
        if (
            isinstance(self.source_generation, bool)
            or not isinstance(self.source_generation, int)
            or self.source_generation < 0
        ):
            raise ValueError("source_generation must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class DocumentRetirementResult:
    """Stable result returned identically for an exact idempotent replay."""

    retirement_id: str
    tenant_id: str
    document_id: str
    retired_snapshot_id: str
    retired_version_id: str
    source_generation_before: int
    source_generation_after: int
    corpus_revision: int
    retired_at: datetime
    status: str = "RETIRED"


@dataclass(frozen=True, slots=True)
class DocumentLifecycleView:
    """Safe metadata for one fully visible, provenance-closed active source."""

    tenant_id: str
    document_id: str
    title: str
    source_name: str
    canonical_uri: str
    source_generation: int
    active_snapshot_id: str
    active_version_id: str
    chunk_count: int
    access_policy_id: str
    access_policy_version: int
    access_groups: tuple[str, ...]
    blocker_codes: tuple[str, ...]

    @property
    def blocked(self) -> bool:
        return bool(self.blocker_codes)


_LIST_ACTIVE_DOCUMENTS_QUERY = """
/* governed-document-retirement:list-active */
MATCH (document:Document {tenant_id: $tenant_id})
      -[snapshot_pointer:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot)
MATCH (document)-[version_pointer:ACTIVE_VERSION]->(version:DocumentVersion)
WHERE coalesce(document.lifecycle_status, 'ACTIVE') = 'ACTIVE'
  AND document.retirement_id IS NULL
  AND document.retirement_request_fingerprint IS NULL
  AND document.retired_at IS NULL
  AND document.retired_by_principal_id IS NULL
  AND document.retired_active_snapshot_id IS NULL
  AND document.retired_active_version_id IS NULL
  AND any(
      group IN $principal_groups
      WHERE group IN coalesce(document.access_groups, [])
  )
  AND snapshot.tenant_id = $tenant_id
  AND snapshot.document_id = document.document_id
  AND snapshot.version_id = version.version_id
  AND snapshot.build_state = 'PUBLISHED'
  AND snapshot.retirement_id IS NULL
  AND snapshot.retired_at IS NULL
  AND snapshot.retired_by_principal_id IS NULL
  AND snapshot.expected_chunk_count > 0
  AND snapshot.actual_chunk_count = snapshot.expected_chunk_count
  AND version.tenant_id = $tenant_id
  AND version.document_id = document.document_id
  AND coalesce(version.lifecycle_status, 'ACTIVE') = 'ACTIVE'
  AND version.retirement_id IS NULL
  AND version.retired_at IS NULL
  AND version.retired_by_principal_id IS NULL
  AND COUNT { MATCH (document)-[:ACTIVE_SNAPSHOT]->(:KnowledgeSnapshot) } = 1
  AND COUNT { MATCH (document)-[:ACTIVE_VERSION]->(:DocumentVersion) } = 1
  AND COUNT { MATCH (document)-[:HAS_VERSION]->(version) } = 1
  AND COUNT { MATCH (snapshot)-[:OF_VERSION]->(:DocumentVersion) } = 1
  AND EXISTS { MATCH (snapshot)-[:OF_VERSION]->(version) }
  AND COUNT { MATCH (:Document)-[:ACTIVE_SNAPSHOT]->(snapshot) } = 1
  AND COUNT { MATCH (:Document)-[:ACTIVE_VERSION]->(version) } = 1
  AND COUNT {
      MATCH (snapshot)-[:INCLUDES_CHUNK]->(:Chunk)
  } = snapshot.expected_chunk_count
  AND COUNT {
      MATCH (version)-[:HAS_CHUNK]->(:Chunk)
  } = snapshot.expected_chunk_count
  AND NOT EXISTS {
      MATCH (document)-[:HAS_VERSION]->(owned_version:DocumentVersion)
            -[:HAS_CHUNK]->(owned_chunk:Chunk)
      WHERE coalesce(owned_version.tenant_id, '') <> $tenant_id
         OR coalesce(owned_version.document_id, '') <> document.document_id
         OR coalesce(owned_chunk.tenant_id, '') <> $tenant_id
         OR coalesce(owned_chunk.document_id, '') <> document.document_id
         OR NOT any(
             group IN $principal_groups
             WHERE group IN coalesce(owned_chunk.access_groups, [])
         )
  }
  AND NOT EXISTS {
      MATCH (document_chunk:Chunk {
          tenant_id: $tenant_id,
          document_id: document.document_id
      })
      WHERE NOT any(
          group IN $principal_groups
          WHERE group IN coalesce(document_chunk.access_groups, [])
      )
         OR NOT EXISTS {
             MATCH (document)-[:HAS_VERSION]->(
                 owner_version:DocumentVersion {
                     tenant_id: $tenant_id,
                     document_id: document.document_id
                 }
             )-[:HAS_CHUNK]->(document_chunk)
             WHERE document_chunk.version_id = owner_version.version_id
         }
  }
  AND NOT EXISTS {
      MATCH (snapshot)-[:INCLUDES_CHUNK]->(snapshot_member:Chunk)
      WHERE snapshot_member.tenant_id <> $tenant_id
         OR snapshot_member.document_id <> document.document_id
         OR snapshot_member.version_id <> version.version_id
         OR NOT any(
             group IN $principal_groups
             WHERE group IN coalesce(snapshot_member.access_groups, [])
         )
         OR NOT EXISTS { MATCH (version)-[:HAS_CHUNK]->(snapshot_member) }
  }
  AND NOT EXISTS {
      MATCH (version)-[:HAS_CHUNK]->(version_chunk:Chunk)
      WHERE NOT EXISTS { MATCH (snapshot)-[:INCLUDES_CHUNK]->(version_chunk) }
  }
WITH document, snapshot, version
ORDER BY document.document_id
LIMIT $limit
RETURN document.document_id AS document_id,
       document.title AS title,
       document.source_name AS source_name,
       document.canonical_uri AS canonical_uri,
       document.generation AS source_generation,
       snapshot.snapshot_id AS active_snapshot_id,
       version.version_id AS active_version_id,
       snapshot.expected_chunk_count AS chunk_count,
       document.access_policy_id AS access_policy_id,
       document.access_policy_version AS access_policy_version,
       document.access_groups AS access_groups,
       EXISTS {
           MATCH (:KnowledgePublicationState {tenant_id: $tenant_id})
                 -[:ACTIVE_KNOWLEDGE_PUBLICATION]->(
                     publication:KnowledgePublication {tenant_id: $tenant_id}
                 )
           WHERE EXISTS {
               MATCH (publication)-[:USES_KNOWLEDGE_SNAPSHOT]->(snapshot)
           }
              OR EXISTS {
                  MATCH (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->(revision)
                  WHERE revision.document_id = document.document_id
                     OR EXISTS {
                         MATCH (revision)-[:IN_CHUNK|EVIDENCED_BY]->(
                             evidence_chunk:Chunk {
                                 tenant_id: $tenant_id,
                                 document_id: document.document_id
                             }
                         )
                     }
              }
       } AS active_publication_reference,
       EXISTS {
           MATCH (:KnowledgeRecordHead {tenant_id: $tenant_id})
                 -[:CURRENT_REVISION]->(review_revision)
           WHERE review_revision.governance_status IN $blocking_review_statuses
             AND (
                 review_revision.document_id = document.document_id
                 OR EXISTS {
                     MATCH (review_revision)-[:IN_CHUNK|EVIDENCED_BY]->(
                         review_chunk:Chunk {
                             tenant_id: $tenant_id,
                             document_id: document.document_id
                         }
                     )
                 }
             )
       } AS reviewable_revision_reference,
       EXISTS {
           MATCH (construction_job:KnowledgeConstructionJob {
               tenant_id: $tenant_id,
               document_id: document.document_id
           })
           WHERE construction_job.status IN ['RUNNING', 'RETRY_WAIT']
       } AS active_construction_reference,
       EXISTS {
           MATCH (ingestion_job:IngestionJob {
               tenant_id: $tenant_id,
               document_id: document.document_id
           })
           WHERE ingestion_job.operation IN ['UPSERT', 'PREPARE_UPSERT']
             AND ingestion_job.status IN ['QUEUED', 'RUNNING', 'RETRY_WAIT']
             AND ingestion_job.source_generation = document.generation
       } AS active_ingestion_reference
"""


_LOCK_STATE_QUERY = """
/* governed-document-retirement:lock-state */
MATCH (document:Document {
    tenant_id: $tenant_id,
    document_id: $document_id
})
WHERE any(
          group IN $principal_groups
          WHERE group IN coalesce(document.access_groups, [])
      )
  AND NOT EXISTS {
      MATCH (document)-[:HAS_VERSION]->(owned_version:DocumentVersion)
            -[:HAS_CHUNK]->(owned_chunk:Chunk)
      WHERE coalesce(owned_version.tenant_id, '') <> $tenant_id
         OR coalesce(owned_version.document_id, '') <> $document_id
         OR coalesce(owned_chunk.tenant_id, '') <> $tenant_id
         OR coalesce(owned_chunk.document_id, '') <> $document_id
         OR NOT any(
             group IN $principal_groups
             WHERE group IN coalesce(owned_chunk.access_groups, [])
         )
  }
  AND NOT EXISTS {
      MATCH (document)-[:ACTIVE_SNAPSHOT]->(:KnowledgeSnapshot)
            -[:INCLUDES_CHUNK]->(snapshot_chunk:Chunk)
      WHERE coalesce(snapshot_chunk.tenant_id, '') <> $tenant_id
         OR coalesce(snapshot_chunk.document_id, '') <> $document_id
         OR NOT any(
             group IN $principal_groups
             WHERE group IN coalesce(snapshot_chunk.access_groups, [])
         )
  }
  AND NOT EXISTS {
      MATCH (document_chunk:Chunk {
          tenant_id: $tenant_id,
          document_id: $document_id
      })
      WHERE NOT any(
          group IN $principal_groups
          WHERE group IN coalesce(document_chunk.access_groups, [])
      )
  }
MERGE (corpus_state:TenantCorpusState {tenant_id: $tenant_id})
ON CREATE SET corpus_state.corpus_revision = 0,
              corpus_state.created_at = $now
SET corpus_state.__document_retirement_lock = randomUUID(),
    corpus_state.lifecycle_mode = 'MANAGED_INCREMENTAL'
WITH document, corpus_state
REMOVE corpus_state.__document_retirement_lock
MERGE (publication_state:KnowledgePublicationState {tenant_id: $tenant_id})
ON CREATE SET publication_state.publication_generation = 0,
              publication_state.activation_generation = 0,
              publication_state.created_at = $now
SET publication_state.__document_retirement_lock = randomUUID()
WITH document, corpus_state, publication_state
REMOVE publication_state.__document_retirement_lock
SET document.__document_retirement_lock = randomUUID()
WITH document, corpus_state
REMOVE document.__document_retirement_lock
WITH document, corpus_state
CALL (document) {
    OPTIONAL MATCH (document)-[pointer:ACTIVE_SNAPSHOT]->(
        snapshot:KnowledgeSnapshot
    )
    RETURN count(pointer) AS active_snapshot_pointer_count,
           collect(DISTINCT snapshot.snapshot_id) AS active_snapshot_ids,
           collect(DISTINCT snapshot.version_id) AS active_snapshot_version_ids,
           collect(DISTINCT snapshot.build_state) AS active_snapshot_states,
           collect(DISTINCT snapshot.tenant_id) AS active_snapshot_tenants,
           collect(DISTINCT snapshot.document_id) AS active_snapshot_documents
}
CALL (document) {
    OPTIONAL MATCH (document)-[pointer:ACTIVE_VERSION]->(
        version:DocumentVersion
    )
    RETURN count(pointer) AS active_version_pointer_count,
           collect(DISTINCT version.version_id) AS active_version_ids,
           collect(DISTINCT version.tenant_id) AS active_version_tenants,
           collect(DISTINCT version.document_id) AS active_version_documents
}
CALL (document) {
    OPTIONAL MATCH (document)-[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot)
          -[binding:OF_VERSION]->(version:DocumentVersion)
          <-[:ACTIVE_VERSION]-(document)
    RETURN count(binding) AS active_snapshot_version_binding_count
}
OPTIONAL MATCH (tombstone:DocumentTombstone {
    tenant_id: $tenant_id,
    document_id: $document_id
})
OPTIONAL MATCH (retirement_event:IngestionJob {
    tenant_id: $tenant_id,
    job_id: $retirement_id,
    operation: 'RETIRE'
})
CALL (retirement_event) {
    OPTIONAL MATCH (retirement_event)-[
        event_document_link:RETIRED_DOCUMENT
    ]->(event_document:Document)
    RETURN count(event_document_link) AS event_document_link_count,
           collect(event_document {
               .tenant_id,
               .document_id,
               .generation,
               .lifecycle_status,
               .retirement_id,
               .retirement_request_fingerprint,
               .retired_at,
               .retired_by_principal_id,
               .retired_active_snapshot_id,
               .retired_active_version_id
           }) AS event_documents
}
CALL (retirement_event) {
    OPTIONAL MATCH (retirement_event)-[
        event_snapshot_link:RETIRED_SNAPSHOT
    ]->(event_snapshot:KnowledgeSnapshot)
    RETURN count(event_snapshot_link) AS event_snapshot_link_count,
           collect(event_snapshot {
               .tenant_id,
               .document_id,
               .snapshot_id,
               .version_id,
               .build_state,
               .retirement_id,
               .retired_at,
               .retired_by_principal_id
           }) AS event_snapshots
}
CALL (retirement_event) {
    OPTIONAL MATCH (retirement_event)-[
        event_version_link:RETIRED_VERSION
    ]->(event_version:DocumentVersion)
    RETURN count(event_version_link) AS event_version_link_count,
           collect(event_version {
               .tenant_id,
               .document_id,
               .version_id,
               .lifecycle_status,
               .retirement_id,
               .retired_at,
               .retired_by_principal_id
           }) AS event_versions
}
CALL (retirement_event) {
    OPTIONAL MATCH (event_tombstone:DocumentTombstone)-[
        tombstone_event_link:HAS_RETIREMENT_EVENT
    ]->(retirement_event)
    RETURN count(tombstone_event_link) AS tombstone_event_link_count,
           collect(event_tombstone {
               .tenant_id,
               .document_id,
               .retirement_id
           }) AS event_tombstones
}
RETURN corpus_state.corpus_revision AS corpus_revision,
       document.generation AS source_generation,
       document.lifecycle_status AS lifecycle_status,
       document.retirement_id AS document_retirement_id,
       document.retirement_request_fingerprint
           AS document_retirement_request_fingerprint,
       document.retired_at AS document_retired_at,
       document.retired_by_principal_id AS document_retired_by_principal_id,
       document.retired_active_snapshot_id
           AS document_retired_active_snapshot_id,
       document.retired_active_version_id
           AS document_retired_active_version_id,
       active_snapshot_pointer_count,
       active_snapshot_ids,
       active_snapshot_version_ids,
       active_snapshot_states,
       active_snapshot_tenants,
       active_snapshot_documents,
       active_version_pointer_count,
       active_version_ids,
       active_version_tenants,
       active_version_documents,
       active_snapshot_version_binding_count,
       EXISTS {
           MATCH (document)-[:ACTIVE_SNAPSHOT]->(
               closed_snapshot:KnowledgeSnapshot
           )
           MATCH (document)-[:ACTIVE_VERSION]->(
               closed_version:DocumentVersion
           )
           WHERE closed_snapshot.tenant_id = $tenant_id
             AND closed_snapshot.document_id = $document_id
             AND closed_snapshot.version_id = closed_version.version_id
             AND closed_snapshot.build_state = 'PUBLISHED'
             AND closed_snapshot.retirement_id IS NULL
             AND closed_snapshot.retired_at IS NULL
             AND closed_snapshot.retired_by_principal_id IS NULL
             AND closed_version.tenant_id = $tenant_id
             AND closed_version.document_id = $document_id
             AND coalesce(closed_version.lifecycle_status, 'ACTIVE') = 'ACTIVE'
             AND closed_version.retirement_id IS NULL
             AND closed_version.retired_at IS NULL
             AND closed_version.retired_by_principal_id IS NULL
             AND closed_snapshot.expected_chunk_count > 0
             AND closed_snapshot.actual_chunk_count
                 = closed_snapshot.expected_chunk_count
             AND COUNT {
                 MATCH (document)-[:HAS_VERSION]->(closed_version)
             } = 1
             AND COUNT {
                 MATCH (:Document)-[:ACTIVE_SNAPSHOT]->(closed_snapshot)
             } = 1
             AND COUNT {
                 MATCH (:Document)-[:ACTIVE_VERSION]->(closed_version)
             } = 1
             AND COUNT {
                 MATCH (closed_snapshot)-[:OF_VERSION]->(:DocumentVersion)
             } = 1
             AND COUNT {
                 MATCH (closed_snapshot)-[:INCLUDES_CHUNK]->(:Chunk)
             } = closed_snapshot.expected_chunk_count
             AND COUNT {
                 MATCH (closed_version)-[:HAS_CHUNK]->(:Chunk)
             } = closed_snapshot.expected_chunk_count
             AND NOT EXISTS {
                 MATCH (closed_snapshot)-[:INCLUDES_CHUNK]->(
                     snapshot_member:Chunk
                 )
                 WHERE snapshot_member.tenant_id <> $tenant_id
                    OR snapshot_member.document_id <> $document_id
                    OR snapshot_member.version_id <> closed_version.version_id
                    OR NOT EXISTS {
                        MATCH (closed_version)-[:HAS_CHUNK]->(snapshot_member)
                    }
             }
             AND NOT EXISTS {
                 MATCH (closed_version)-[:HAS_CHUNK]->(version_chunk:Chunk)
                 WHERE NOT EXISTS {
                     MATCH (closed_snapshot)-[:INCLUDES_CHUNK]->(version_chunk)
                 }
             }
             AND NOT EXISTS {
                 MATCH (document_chunk:Chunk {
                     tenant_id: $tenant_id,
                     document_id: $document_id
                 })
                 WHERE NOT EXISTS {
                     MATCH (document)-[:HAS_VERSION]->(
                         owner_version:DocumentVersion {
                             tenant_id: $tenant_id,
                             document_id: $document_id
                         }
                     )-[:HAS_CHUNK]->(document_chunk)
                     WHERE document_chunk.version_id = owner_version.version_id
                 }
             }
       } AS active_provenance_closed,
       tombstone {
           .generation,
           .lifecycle_status,
           .retirement_id,
           .retirement_request_fingerprint,
           .retired_snapshot_id,
           .retired_version_id,
           .source_generation_before,
           .retired_by_principal_id,
           .corpus_revision,
           .retired_at
       } AS tombstone,
       retirement_event {
           .job_id,
           .tenant_id,
           .operation,
           .operation_key,
           .idempotency_key,
           .request_fingerprint,
           .document_id,
           .target_version_id,
           .target_snapshot_id,
           .expected_active_snapshot_id,
           .source_generation,
           .source_generation_after,
           .retired_by_principal_id,
           .retired_at,
           .outcome,
           .status,
           .phase,
           .finished_at,
           .corpus_revision
       } AS retirement_event,
       event_document_link_count,
       event_documents,
       event_snapshot_link_count,
       event_snapshots,
       event_version_link_count,
       event_versions,
       tombstone_event_link_count,
       event_tombstones,
       EXISTS {
           MATCH (:KnowledgePublicationState {tenant_id: $tenant_id})
                 -[:ACTIVE_KNOWLEDGE_PUBLICATION]->(
                     publication:KnowledgePublication {tenant_id: $tenant_id}
                 )
           WHERE EXISTS {
               MATCH (publication)-[:USES_KNOWLEDGE_SNAPSHOT]->(
                   used_snapshot:KnowledgeSnapshot {
                       tenant_id: $tenant_id,
                       document_id: $document_id
                   }
               )
           }
              OR EXISTS {
                  MATCH (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->(revision)
                  WHERE revision.document_id = $document_id
                     OR EXISTS {
                         MATCH (revision)-[:IN_CHUNK|EVIDENCED_BY]->(
                             evidence_chunk:Chunk {
                                 tenant_id: $tenant_id,
                                 document_id: $document_id
                             }
                         )
                     }
              }
       } AS active_publication_reference,
       EXISTS {
           MATCH (:KnowledgeRecordHead {tenant_id: $tenant_id})
                 -[:CURRENT_REVISION]->(review_revision)
           WHERE review_revision.governance_status IN $blocking_review_statuses
             AND (
                 review_revision.document_id = $document_id
                 OR EXISTS {
                     MATCH (review_revision)-[:IN_CHUNK|EVIDENCED_BY]->(
                         review_chunk:Chunk {
                             tenant_id: $tenant_id,
                             document_id: $document_id
                         }
                     )
                 }
             )
       } AS reviewable_revision_reference,
       EXISTS {
           MATCH (construction_job:KnowledgeConstructionJob {
               tenant_id: $tenant_id,
               document_id: $document_id
           })
           WHERE construction_job.status IN ['RUNNING', 'RETRY_WAIT']
       } AS active_construction_reference,
       EXISTS {
           MATCH (ingestion_job:IngestionJob {
               tenant_id: $tenant_id,
               document_id: $document_id
           })
           WHERE ingestion_job.operation IN ['UPSERT', 'PREPARE_UPSERT']
             AND ingestion_job.status IN ['QUEUED', 'RUNNING', 'RETRY_WAIT']
             AND ingestion_job.source_generation = document.generation
       } AS active_ingestion_reference
"""


_RETIRE_QUERY = """
/* governed-document-retirement:retire */
MATCH (corpus_state:TenantCorpusState {tenant_id: $tenant_id})
MATCH (document:Document {
    tenant_id: $tenant_id,
    document_id: $document_id
})-[snapshot_pointer:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot)
MATCH (document)-[version_pointer:ACTIVE_VERSION]->(version:DocumentVersion)
WITH corpus_state,
     document,
     collect(DISTINCT snapshot_pointer) AS snapshot_pointers,
     collect(DISTINCT snapshot) AS snapshots,
     collect(DISTINCT version_pointer) AS version_pointers,
     collect(DISTINCT version) AS versions
WHERE size(snapshot_pointers) = 1
  AND size(snapshots) = 1
  AND size(version_pointers) = 1
  AND size(versions) = 1
WITH corpus_state,
     document,
     head(snapshot_pointers) AS snapshot_pointer,
     head(snapshots) AS snapshot,
     head(version_pointers) AS version_pointer,
     head(versions) AS version
WHERE snapshot.snapshot_id = $expected_active_snapshot_id
  AND version.version_id = $expected_active_version_id
  AND document.generation = $source_generation
  AND coalesce(document.lifecycle_status, 'ACTIVE') = 'ACTIVE'
  AND document.retirement_id IS NULL
  AND document.retirement_request_fingerprint IS NULL
  AND document.retired_at IS NULL
  AND document.retired_by_principal_id IS NULL
  AND document.retired_active_snapshot_id IS NULL
  AND document.retired_active_version_id IS NULL
  AND snapshot.tenant_id = $tenant_id
  AND snapshot.document_id = $document_id
  AND snapshot.version_id = version.version_id
  AND snapshot.build_state = 'PUBLISHED'
  AND snapshot.retirement_id IS NULL
  AND snapshot.retired_at IS NULL
  AND snapshot.retired_by_principal_id IS NULL
  AND snapshot.expected_chunk_count > 0
  AND snapshot.actual_chunk_count = snapshot.expected_chunk_count
  AND version.tenant_id = $tenant_id
  AND version.document_id = $document_id
  AND coalesce(version.lifecycle_status, 'ACTIVE') = 'ACTIVE'
  AND version.retirement_id IS NULL
  AND version.retired_at IS NULL
  AND version.retired_by_principal_id IS NULL
  AND COUNT { MATCH (document)-[:HAS_VERSION]->(version) } = 1
  AND COUNT { MATCH (snapshot)-[:OF_VERSION]->(:DocumentVersion) } = 1
  AND EXISTS { MATCH (snapshot)-[:OF_VERSION]->(version) }
  AND COUNT { MATCH (:Document)-[:ACTIVE_SNAPSHOT]->(snapshot) } = 1
  AND COUNT { MATCH (:Document)-[:ACTIVE_VERSION]->(version) } = 1
  AND COUNT {
      MATCH (snapshot)-[:INCLUDES_CHUNK]->(:Chunk)
  } = snapshot.expected_chunk_count
  AND COUNT {
      MATCH (version)-[:HAS_CHUNK]->(:Chunk)
  } = snapshot.expected_chunk_count
  AND any(
      group IN $principal_groups
      WHERE group IN coalesce(document.access_groups, [])
  )
  AND NOT EXISTS {
      MATCH (document)-[:HAS_VERSION]->(owned_version:DocumentVersion)
            -[:HAS_CHUNK]->(owned_chunk:Chunk)
      WHERE coalesce(owned_version.tenant_id, '') <> $tenant_id
         OR coalesce(owned_version.document_id, '') <> $document_id
         OR coalesce(owned_chunk.tenant_id, '') <> $tenant_id
         OR coalesce(owned_chunk.document_id, '') <> $document_id
         OR NOT any(
             group IN $principal_groups
             WHERE group IN coalesce(owned_chunk.access_groups, [])
         )
  }
  AND NOT EXISTS {
      MATCH (snapshot)-[:INCLUDES_CHUNK]->(snapshot_chunk:Chunk)
      WHERE coalesce(snapshot_chunk.tenant_id, '') <> $tenant_id
         OR coalesce(snapshot_chunk.document_id, '') <> $document_id
         OR NOT any(
             group IN $principal_groups
             WHERE group IN coalesce(snapshot_chunk.access_groups, [])
         )
  }
  AND NOT EXISTS {
      MATCH (snapshot)-[:INCLUDES_CHUNK]->(snapshot_member:Chunk)
      WHERE snapshot_member.version_id <> version.version_id
         OR NOT EXISTS { MATCH (version)-[:HAS_CHUNK]->(snapshot_member) }
  }
  AND NOT EXISTS {
      MATCH (version)-[:HAS_CHUNK]->(version_chunk:Chunk)
      WHERE NOT EXISTS { MATCH (snapshot)-[:INCLUDES_CHUNK]->(version_chunk) }
  }
  AND NOT EXISTS {
      MATCH (document_chunk:Chunk {
          tenant_id: $tenant_id,
          document_id: $document_id
      })
      WHERE NOT any(
          group IN $principal_groups
          WHERE group IN coalesce(document_chunk.access_groups, [])
      )
         OR NOT EXISTS {
             MATCH (document)-[:HAS_VERSION]->(
                 owner_version:DocumentVersion {
                     tenant_id: $tenant_id,
                     document_id: $document_id
                 }
             )-[:HAS_CHUNK]->(document_chunk)
             WHERE document_chunk.version_id = owner_version.version_id
         }
  }
  AND NOT EXISTS {
      MATCH (:KnowledgePublicationState {tenant_id: $tenant_id})
            -[:ACTIVE_KNOWLEDGE_PUBLICATION]->(
                publication:KnowledgePublication {tenant_id: $tenant_id}
            )
      WHERE EXISTS {
          MATCH (publication)-[:USES_KNOWLEDGE_SNAPSHOT]->(snapshot)
      }
         OR EXISTS {
             MATCH (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->(revision)
             WHERE revision.document_id = $document_id
                OR EXISTS {
                    MATCH (revision)-[:IN_CHUNK|EVIDENCED_BY]->(
                        evidence_chunk:Chunk {
                            tenant_id: $tenant_id,
                            document_id: $document_id
                        }
                    )
                }
         }
  }
  AND NOT EXISTS {
      MATCH (:KnowledgeRecordHead {tenant_id: $tenant_id})
            -[:CURRENT_REVISION]->(review_revision)
      WHERE review_revision.governance_status IN $blocking_review_statuses
        AND (
            review_revision.document_id = $document_id
            OR EXISTS {
                MATCH (review_revision)-[:IN_CHUNK|EVIDENCED_BY]->(
                    review_chunk:Chunk {
                        tenant_id: $tenant_id,
                        document_id: $document_id
                    }
                )
            }
        )
  }
  AND NOT EXISTS {
      MATCH (construction_job:KnowledgeConstructionJob {
          tenant_id: $tenant_id,
          document_id: $document_id
      })
      WHERE construction_job.status IN ['RUNNING', 'RETRY_WAIT']
  }
  AND NOT EXISTS {
      MATCH (ingestion_job:IngestionJob {
          tenant_id: $tenant_id,
          document_id: $document_id
      })
      WHERE ingestion_job.operation IN ['UPSERT', 'PREPARE_UPSERT']
        AND ingestion_job.status IN ['QUEUED', 'RUNNING', 'RETRY_WAIT']
        AND ingestion_job.source_generation = document.generation
  }
WITH corpus_state, document, snapshot_pointer, snapshot, version_pointer, version
OPTIONAL MATCH (chunk:Chunk {
    tenant_id: $tenant_id,
    document_id: $document_id
})
WITH corpus_state,
     document,
     snapshot_pointer,
     snapshot,
     version_pointer,
     version,
     collect(DISTINCT chunk) AS chunks
FOREACH (chunk IN chunks | REMOVE chunk.retrieval_scope)
DELETE snapshot_pointer, version_pointer
SET document.lifecycle_status = 'RETIRED',
    document.retirement_id = $retirement_id,
    document.retirement_request_fingerprint = $request_fingerprint,
    document.retired_at = $now,
    document.retired_by_principal_id = $principal_id,
    document.retired_active_snapshot_id = snapshot.snapshot_id,
    document.retired_active_version_id = version.version_id,
    document.generation = $next_generation,
    snapshot.build_state = 'RETIRED',
    snapshot.retired_at = $now,
    snapshot.retired_by_principal_id = $principal_id,
    snapshot.retirement_id = $retirement_id,
    version.lifecycle_status = 'RETIRED',
    version.retired_at = $now,
    version.retired_by_principal_id = $principal_id,
    version.retirement_id = $retirement_id
MERGE (tombstone:DocumentTombstone {
    tenant_id: $tenant_id,
    document_id: $document_id
})
SET tombstone.generation = $next_generation,
    tombstone.lifecycle_status = 'RETIRED',
    tombstone.retirement_id = $retirement_id,
    tombstone.retirement_request_fingerprint = $request_fingerprint,
    tombstone.retired_snapshot_id = snapshot.snapshot_id,
    tombstone.retired_version_id = version.version_id,
    tombstone.source_generation_before = $source_generation,
    tombstone.retired_at = $now,
    tombstone.retired_by_principal_id = $principal_id
MERGE (retirement_event:IngestionJob {job_id: $retirement_id})
ON CREATE SET retirement_event = $event_properties
WITH corpus_state,
     document,
     snapshot,
     version,
     tombstone,
     retirement_event
WHERE all(
    key IN keys($event_immutable)
    WHERE retirement_event[key] = $event_immutable[key]
)
MERGE (tombstone)-[:HAS_RETIREMENT_EVENT]->(retirement_event)
MERGE (retirement_event)-[:RETIRED_DOCUMENT]->(document)
MERGE (retirement_event)-[:RETIRED_SNAPSHOT]->(snapshot)
MERGE (retirement_event)-[:RETIRED_VERSION]->(version)
SET corpus_state.corpus_revision = coalesce(corpus_state.corpus_revision, 0) + 1,
    corpus_state.updated_at = $now,
    tombstone.corpus_revision = corpus_state.corpus_revision,
    retirement_event.corpus_revision = corpus_state.corpus_revision
WITH corpus_state,
     document,
     snapshot,
     version,
     tombstone,
     retirement_event
OPTIONAL MATCH (corpus_state)-[index_pointer:ACTIVE_EMBEDDING_INDEX]->(
    embedding_generation:EmbeddingIndexGeneration
)
DELETE index_pointer
SET embedding_generation.state = 'STALE',
    embedding_generation.stale_at = $now,
    embedding_generation.updated_at = $now
RETURN retirement_event.job_id AS retirement_id,
       document.document_id AS document_id,
       snapshot.snapshot_id AS retired_snapshot_id,
       version.version_id AS retired_version_id,
       tombstone.source_generation_before AS source_generation_before,
       tombstone.generation AS source_generation_after,
       corpus_state.corpus_revision AS corpus_revision,
       tombstone.retired_at AS retired_at
"""


def _mapping(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "items"):
        return dict(value)  # type: ignore[arg-type]
    raise DocumentRetirementConflict()


def _one_text(values: object) -> str | None:
    if not isinstance(values, (list, tuple)) or len(values) != 1:
        return None
    value = values[0]
    return value if isinstance(value, str) and value else None


def _one_mapping(values: object) -> dict[str, Any] | None:
    if not isinstance(values, (list, tuple)) or len(values) != 1:
        return None
    try:
        return _mapping(values[0])
    except DocumentRetirementConflict:
        return None


class Neo4jDocumentRetirementService:
    """Logically retire one completely visible active document."""

    def __init__(
        self,
        driver: SessionDriver,
        database: str = "neo4j",
        *,
        clock: Clock | None = None,
        transaction_timeout_seconds: float = (
            DEFAULT_RETIREMENT_TRANSACTION_TIMEOUT_SECONDS
        ),
    ) -> None:
        if driver is None:
            raise ValueError("driver must not be None")
        self.driver = driver
        self.database = _required_text(database, "database")
        timeout = transaction_timeout_seconds
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or not 0.0 < float(timeout) <= 300.0
        ):
            raise ValueError(
                "transaction_timeout_seconds must be finite and between 0 and 300"
            )
        self.clock = clock or SystemClock()
        self.transaction_timeout_seconds = float(timeout)
        self._transaction_work = unit_of_work(
            metadata={
                "component": "graphrag-document-retirement",
                "operation": "logical-retirement",
            },
            timeout=self.transaction_timeout_seconds,
        )(self._retire_tx)
        self._list_transaction_work = unit_of_work(
            metadata={
                "component": "graphrag-document-retirement",
                "operation": "list-active-documents",
            },
            timeout=self.transaction_timeout_seconds,
        )(self._list_active_documents_tx)

    def list_active_documents(
        self,
        principal: Principal,
        *,
        limit: int = 50,
    ) -> tuple[DocumentLifecycleView, ...]:
        """List only fully authorized, provenance-closed active documents."""

        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal")
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        if not 1 <= limit <= MAX_ACTIVE_DOCUMENT_LIST_LIMIT:
            raise ValueError(
                f"limit must be between 1 and {MAX_ACTIVE_DOCUMENT_LIST_LIMIT}"
            )
        if DOCUMENT_LIFECYCLE_CAPABILITY not in principal.capabilities:
            raise DocumentRetirementUnavailable()
        try:
            with self.driver.session(database=self.database) as session:
                return session.execute_read(
                    self._list_transaction_work,
                    principal,
                    limit,
                )
        except DocumentRetirementError:
            raise
        except Exception as exc:
            raise DocumentRetirementBackendUnavailable() from exc

    def retire(
        self,
        principal: Principal,
        request: DocumentRetirementRequest,
    ) -> DocumentRetirementResult:
        """Retire an active source without deleting its audit graph."""

        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal")
        if not isinstance(request, DocumentRetirementRequest):
            raise TypeError("request must be a DocumentRetirementRequest")
        if DOCUMENT_LIFECYCLE_CAPABILITY not in principal.capabilities:
            raise DocumentRetirementUnavailable()
        now = _aware(self.clock.now(), "clock result")
        tenant_id = principal.tenant_id.strip()
        retirement_id = ingestion_job_id(
            tenant_id,
            DOCUMENT_RETIREMENT_OPERATION,
            request.operation_key,
        )
        request_fingerprint = _fingerprint(
            {
                "operation": DOCUMENT_RETIREMENT_OPERATION,
                "tenant_id": tenant_id,
                "principal_id": principal.principal_id.strip(),
                "document_id": request.document_id,
                "operation_key": request.operation_key,
                "expected_active_snapshot_id": (request.expected_active_snapshot_id),
                "source_generation": request.source_generation,
            }
        )
        try:
            with self.driver.session(database=self.database) as session:
                return session.execute_write(
                    self._transaction_work,
                    principal,
                    request,
                    retirement_id,
                    request_fingerprint,
                    now,
                )
        except DocumentRetirementError:
            raise
        except Exception as exc:
            # Neo4j messages can include connection strings, query fragments,
            # or property values. Preserve the cause, never its text.
            raise DocumentRetirementBackendUnavailable() from exc

    def _retire_tx(
        self,
        tx: Any,
        principal: Principal,
        request: DocumentRetirementRequest,
        retirement_id: str,
        request_fingerprint: str,
        now: datetime,
    ) -> DocumentRetirementResult:
        tenant_id = principal.tenant_id.strip()
        parameters = {
            "tenant_id": tenant_id,
            "document_id": request.document_id,
            "retirement_id": retirement_id,
            "principal_groups": sorted(principal.groups),
            "blocking_review_statuses": list(_BLOCKING_REVIEW_STATUSES),
            "now": now,
        }
        state = tx.run(_LOCK_STATE_QUERY, **parameters).single()
        if state is None:
            raise DocumentRetirementUnavailable()
        state = dict(state)

        replay = self._exact_replay(
            state,
            tenant_id=tenant_id,
            principal_id=principal.principal_id.strip(),
            request=request,
            retirement_id=retirement_id,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            return replay

        if state.get("retirement_event"):
            raise DocumentRetirementConflict()

        current_retirement_projection = (
            "document_retirement_id",
            "document_retirement_request_fingerprint",
            "document_retired_at",
            "document_retired_by_principal_id",
            "document_retired_active_snapshot_id",
            "document_retired_active_version_id",
        )
        if state.get("lifecycle_status") not in (None, "ACTIVE") or any(
            state.get(name) is not None for name in current_retirement_projection
        ):
            raise DocumentRetirementConflict()

        snapshot_id = _one_text(state.get("active_snapshot_ids"))
        version_id = _one_text(state.get("active_version_ids"))
        snapshot_version_id = _one_text(state.get("active_snapshot_version_ids"))
        if (
            state.get("active_snapshot_pointer_count") != 1
            or state.get("active_version_pointer_count") != 1
            or state.get("active_snapshot_version_binding_count") != 1
            or snapshot_id != request.expected_active_snapshot_id
            or version_id is None
            or snapshot_version_id != version_id
            or _one_text(state.get("active_snapshot_states")) != "PUBLISHED"
            or _one_text(state.get("active_snapshot_tenants")) != tenant_id
            or _one_text(state.get("active_snapshot_documents")) != request.document_id
            or _one_text(state.get("active_version_tenants")) != tenant_id
            or _one_text(state.get("active_version_documents")) != request.document_id
            or state.get("source_generation") != request.source_generation
            or state.get("active_provenance_closed") is not True
        ):
            raise DocumentRetirementConflict()

        if any(
            bool(state.get(name))
            for name in (
                "active_publication_reference",
                "reviewable_revision_reference",
                "active_construction_reference",
                "active_ingestion_reference",
            )
        ):
            raise DocumentRetirementBlocked()

        next_generation = request.source_generation + 1
        event_immutable = {
            "job_id": retirement_id,
            "tenant_id": tenant_id,
            "operation": DOCUMENT_RETIREMENT_OPERATION,
            "operation_key": request.operation_key,
            "idempotency_key": request.operation_key,
            "request_fingerprint": request_fingerprint,
            "document_id": request.document_id,
            "target_version_id": version_id,
            "target_snapshot_id": snapshot_id,
            "expected_active_snapshot_id": request.expected_active_snapshot_id,
            "source_generation": request.source_generation,
            "source_generation_after": next_generation,
            "retired_by_principal_id": principal.principal_id.strip(),
            "retired_at": now,
        }
        event_properties = {
            **event_immutable,
            "status": "SUCCEEDED",
            "phase": "COMPLETE",
            "attempts": 1,
            "max_attempts": 1,
            "completed_tasks": 0,
            "expected_tasks": 0,
            "lease_owner": "",
            "lease_token": "",
            "outcome": "RETIRED",
            "last_error_code": "",
            "created_at": now,
            "started_at": now,
            "finished_at": now,
            "updated_at": now,
        }
        mutation = tx.run(
            _RETIRE_QUERY,
            **parameters,
            expected_active_snapshot_id=snapshot_id,
            expected_active_version_id=version_id,
            source_generation=request.source_generation,
            next_generation=next_generation,
            request_fingerprint=request_fingerprint,
            principal_id=principal.principal_id.strip(),
            event_immutable=event_immutable,
            event_properties=event_properties,
        ).single()
        if mutation is None:
            # Every security/CAS/governance condition is repeated by the write
            # query. A missing row therefore fails closed and rolls back all
            # writes, including any MERGE executed earlier in that query.
            raise DocumentRetirementConflict()
        result = self._result(dict(mutation), tenant_id=tenant_id)
        if (
            result.retirement_id != retirement_id
            or result.document_id != request.document_id
            or result.retired_snapshot_id != snapshot_id
            or result.retired_version_id != version_id
            or result.source_generation_before != request.source_generation
            or result.source_generation_after != next_generation
            or result.retired_at != now
        ):
            raise DocumentRetirementConflict()
        return result

    @staticmethod
    def _list_active_documents_tx(
        tx: Any,
        principal: Principal,
        limit: int,
    ) -> tuple[DocumentLifecycleView, ...]:
        records = tx.run(
            _LIST_ACTIVE_DOCUMENTS_QUERY,
            tenant_id=principal.tenant_id.strip(),
            principal_groups=sorted(principal.groups),
            blocking_review_statuses=list(_BLOCKING_REVIEW_STATUSES),
            limit=limit,
        )
        return tuple(
            Neo4jDocumentRetirementService._lifecycle_view(
                dict(record),
                tenant_id=principal.tenant_id.strip(),
            )
            for record in records
        )

    @staticmethod
    def _lifecycle_view(
        row: dict[str, Any],
        *,
        tenant_id: str,
    ) -> DocumentLifecycleView:
        try:
            raw_groups = row["access_groups"]
            if not isinstance(raw_groups, (list, tuple)):
                raise TypeError("access_groups must be a list")
            access_groups = tuple(
                sorted({_required_text(group, "access_group") for group in raw_groups})
            )
            if not access_groups:
                raise ValueError("access_groups must not be empty")
            source_generation = int(row["source_generation"])
            chunk_count = int(row["chunk_count"])
            access_policy_version = int(row["access_policy_version"])
            if source_generation < 0 or chunk_count <= 0 or access_policy_version <= 0:
                raise ValueError("document lifecycle counters are invalid")
            blocker_codes = tuple(
                code
                for field, code in _ACTIVE_DOCUMENT_BLOCKERS
                if bool(row.get(field))
            )
            return DocumentLifecycleView(
                tenant_id=tenant_id,
                document_id=_required_text(row["document_id"], "document_id"),
                title=_required_text(row["title"], "title"),
                source_name=_required_text(row["source_name"], "source_name"),
                canonical_uri=_required_text(row["canonical_uri"], "canonical_uri"),
                source_generation=source_generation,
                active_snapshot_id=_required_text(
                    row["active_snapshot_id"], "active_snapshot_id"
                ),
                active_version_id=_required_text(
                    row["active_version_id"], "active_version_id"
                ),
                chunk_count=chunk_count,
                access_policy_id=_required_text(
                    row["access_policy_id"], "access_policy_id"
                ),
                access_policy_version=access_policy_version,
                access_groups=access_groups,
                blocker_codes=blocker_codes,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DocumentRetirementConflict() from exc

    @staticmethod
    def _exact_replay(
        state: dict[str, Any],
        *,
        tenant_id: str,
        principal_id: str,
        request: DocumentRetirementRequest,
        retirement_id: str,
        request_fingerprint: str,
    ) -> DocumentRetirementResult | None:
        event = _mapping(state.get("retirement_event"))
        tombstone = _mapping(state.get("tombstone"))
        has_current_projection = any(
            (
                state.get("lifecycle_status") == "RETIRED",
                state.get("document_retirement_id"),
                state.get("document_retirement_request_fingerprint"),
                tombstone.get("retirement_id") == retirement_id,
            )
        )
        if not event and not has_current_projection:
            return None

        event_document = _one_mapping(state.get("event_documents"))
        event_snapshot = _one_mapping(state.get("event_snapshots"))
        event_version = _one_mapping(state.get("event_versions"))
        event_tombstone = _one_mapping(state.get("event_tombstones"))
        try:
            retired_at = _aware(event.get("retired_at"), "event.retired_at")
            document_retired_at = _aware(
                state.get("document_retired_at"), "document.retired_at"
            )
            tombstone_retired_at = _aware(
                tombstone.get("retired_at"), "tombstone.retired_at"
            )
            event_finished_at = _aware(event.get("finished_at"), "event.finished_at")
            snapshot_retired_at = _aware(
                (event_snapshot or {}).get("retired_at"), "snapshot.retired_at"
            )
            version_retired_at = _aware(
                (event_version or {}).get("retired_at"), "version.retired_at"
            )
            event_document_retired_at = _aware(
                (event_document or {}).get("retired_at"),
                "event_document.retired_at",
            )
        except ValueError as exc:
            raise DocumentRetirementConflict() from exc
        target_version_id = event.get("target_version_id")
        next_generation = request.source_generation + 1
        exact = (
            state.get("lifecycle_status") == "RETIRED"
            and state.get("document_retirement_id") == retirement_id
            and state.get("document_retirement_request_fingerprint")
            == request_fingerprint
            and state.get("document_retired_by_principal_id") == principal_id
            and state.get("document_retired_active_snapshot_id")
            == request.expected_active_snapshot_id
            and state.get("document_retired_active_version_id") == target_version_id
            and document_retired_at == retired_at
            and state.get("active_snapshot_pointer_count") == 0
            and state.get("active_version_pointer_count") == 0
            and state.get("source_generation") == next_generation
            and event.get("job_id") == retirement_id
            and event.get("tenant_id") == tenant_id
            and event.get("operation") == DOCUMENT_RETIREMENT_OPERATION
            and event.get("operation_key") == request.operation_key
            and event.get("idempotency_key") == request.operation_key
            and event.get("request_fingerprint") == request_fingerprint
            and event.get("document_id") == request.document_id
            and event.get("target_snapshot_id") == request.expected_active_snapshot_id
            and isinstance(target_version_id, str)
            and bool(target_version_id)
            and event.get("expected_active_snapshot_id")
            == request.expected_active_snapshot_id
            and event.get("source_generation") == request.source_generation
            and event.get("source_generation_after") == next_generation
            and event.get("retired_by_principal_id") == principal_id
            and event.get("status") == "SUCCEEDED"
            and event.get("phase") == "COMPLETE"
            and event.get("outcome") == "RETIRED"
            and event_finished_at == retired_at
            and tombstone.get("generation") == next_generation
            and tombstone.get("lifecycle_status") == "RETIRED"
            and tombstone.get("retirement_id") == retirement_id
            and tombstone.get("retirement_request_fingerprint") == request_fingerprint
            and tombstone.get("retired_snapshot_id")
            == request.expected_active_snapshot_id
            and tombstone.get("source_generation_before") == request.source_generation
            and tombstone.get("retired_version_id") == target_version_id
            and tombstone.get("retired_by_principal_id") == principal_id
            and tombstone_retired_at == retired_at
            and tombstone.get("corpus_revision") == event.get("corpus_revision")
            and state.get("event_document_link_count") == 1
            and event_document is not None
            and event_document.get("tenant_id") == tenant_id
            and event_document.get("document_id") == request.document_id
            and event_document.get("generation") == next_generation
            and event_document.get("lifecycle_status") == "RETIRED"
            and event_document.get("retirement_id") == retirement_id
            and event_document.get("retirement_request_fingerprint")
            == request_fingerprint
            and event_document.get("retired_by_principal_id") == principal_id
            and event_document.get("retired_active_snapshot_id")
            == request.expected_active_snapshot_id
            and event_document.get("retired_active_version_id") == target_version_id
            and event_document_retired_at == retired_at
            and state.get("event_snapshot_link_count") == 1
            and event_snapshot is not None
            and event_snapshot.get("tenant_id") == tenant_id
            and event_snapshot.get("document_id") == request.document_id
            and event_snapshot.get("snapshot_id") == request.expected_active_snapshot_id
            and event_snapshot.get("version_id") == target_version_id
            and event_snapshot.get("build_state") == "RETIRED"
            and event_snapshot.get("retirement_id") == retirement_id
            and event_snapshot.get("retired_by_principal_id") == principal_id
            and snapshot_retired_at == retired_at
            and state.get("event_version_link_count") == 1
            and event_version is not None
            and event_version.get("tenant_id") == tenant_id
            and event_version.get("document_id") == request.document_id
            and event_version.get("version_id") == target_version_id
            and event_version.get("lifecycle_status") == "RETIRED"
            and event_version.get("retirement_id") == retirement_id
            and event_version.get("retired_by_principal_id") == principal_id
            and version_retired_at == retired_at
            and state.get("tombstone_event_link_count") == 1
            and event_tombstone is not None
            and event_tombstone.get("tenant_id") == tenant_id
            and event_tombstone.get("document_id") == request.document_id
            and event_tombstone.get("retirement_id") == retirement_id
        )
        if not exact:
            raise DocumentRetirementConflict()
        return DocumentRetirementResult(
            retirement_id=retirement_id,
            tenant_id=tenant_id,
            document_id=request.document_id,
            retired_snapshot_id=tombstone["retired_snapshot_id"],
            retired_version_id=tombstone["retired_version_id"],
            source_generation_before=request.source_generation,
            source_generation_after=tombstone["generation"],
            corpus_revision=tombstone["corpus_revision"],
            retired_at=_aware(tombstone.get("retired_at"), "retired_at"),
        )

    @staticmethod
    def _result(
        row: dict[str, Any],
        *,
        tenant_id: str,
    ) -> DocumentRetirementResult:
        try:
            return DocumentRetirementResult(
                retirement_id=_required_text(row["retirement_id"], "retirement_id"),
                tenant_id=tenant_id,
                document_id=_required_text(row["document_id"], "document_id"),
                retired_snapshot_id=_required_text(
                    row["retired_snapshot_id"], "retired_snapshot_id"
                ),
                retired_version_id=_required_text(
                    row["retired_version_id"], "retired_version_id"
                ),
                source_generation_before=int(row["source_generation_before"]),
                source_generation_after=int(row["source_generation_after"]),
                corpus_revision=int(row["corpus_revision"]),
                retired_at=_aware(row["retired_at"], "retired_at"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DocumentRetirementConflict() from exc
