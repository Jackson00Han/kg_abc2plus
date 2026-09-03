"""Tenant-safe document-to-review construction orchestration.

The workflow intentionally publishes only source Documents, immutable Versions,
Chunks, and Embeddings through the canonical ingestion pipeline.  Ontology-
constrained model output is persisted separately as append-only CANDIDATE or
QUARANTINED A-Box records, ready for explicit human review.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from typing import Any, Callable, Protocol

from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import (
    canonicalize_uri,
    content_checksum,
    derivation_artifact_id,
    document_id,
    ingestion_job_id,
    ingestion_task_id,
    knowledge_snapshot_id,
    pipeline_profile_id,
    version_id,
)
from graphrag_prod.domain.models import (
    Assertion,
    Chunk,
    Document,
    DocumentVersion,
    Entity,
    EntityMention,
    GraphPipelineProfile,
)
from graphrag_prod.graph.provenance import ProvenanceBundle
from graphrag_prod.ingestion.artifacts import decode_extraction, encode_extraction
from graphrag_prod.ingestion.models import IngestionResult, _fingerprint
from graphrag_prod.ingestion.pipeline import (
    EmbeddingProfile,
    EmbeddingProvider,
    ExtractionOutput,
    IncrementalIngestionRequest,
)
from graphrag_prod.knowledge.models import (
    ABoxRecordBatch,
    AssertionRecord,
    EntityIdentity,
    EntityMentionRecord,
    EvidenceReference,
    RecordRevision,
    knowledge_record_id,
    llm_candidate_trust,
    llm_quarantined_trust,
)
from graphrag_prod.knowledge.store import KnowledgeConflict, Neo4jKnowledgeStore
from graphrag_prod.knowledge.trust import (
    AuthorityLevel,
    GovernanceStatus,
    KnowledgeOrigin,
)
from graphrag_prod.ontology.models import TBoxStatus, TBoxVersion
from graphrag_prod.ontology.store import Neo4jTBoxStore

from .extraction import (
    AuditedExtraction,
    ExtractionFinding,
    ExtractionRejected,
)
from .parser import BoundedDocumentParser, ParsedDocument


AUDIT_ARTIFACT_KIND = "ONTOLOGY_EXTRACTION_AUDIT"
KNOWLEDGE_CONSTRUCTION_CAPABILITY = "knowledge:construct"
CANONICAL_EMPTY_EXTRACTOR_SIGNATURE = "canonical-empty-extraction:v1"
CANONICAL_EMPTY_PROMPT_SIGNATURE = "no-model-prompt:v1"


def _required(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


def _aware(value: datetime | None, name: str) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _require_construction_capability(principal: Principal) -> None:
    if KNOWLEDGE_CONSTRUCTION_CAPABILITY not in principal.capabilities:
        raise ConstructionAuthorizationError(
            "principal lacks the knowledge-construction capability"
        )


def _profile(
    *,
    splitter_signature: str,
    extractor_signature: str,
    prompt_signature: str,
    schema_signature: str,
    code_signature: str,
    normalizer_signature: str,
) -> GraphPipelineProfile:
    values = (
        normalizer_signature,
        splitter_signature,
        extractor_signature,
        prompt_signature,
        schema_signature,
        code_signature,
    )
    return GraphPipelineProfile(pipeline_profile_id(*values), *values)


@dataclass(frozen=True, slots=True)
class ConstructionMetadata:
    """Caller-supplied source metadata; tenant and ACL are intentionally absent."""

    operation_key: str
    canonical_uri: str
    title: str
    source_name: str
    mime_type: str
    language: str
    tbox_key: str
    published_at: datetime | None = None
    max_attempts: int = 3

    def __post_init__(self) -> None:
        for name in (
            "operation_key",
            "title",
            "source_name",
            "mime_type",
            "language",
            "tbox_key",
        ):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        object.__setattr__(self, "canonical_uri", canonicalize_uri(self.canonical_uri))
        _aware(self.published_at, "published_at")
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or self.max_attempts <= 0
        ):
            raise ValueError("max_attempts must be a positive integer")


@dataclass(frozen=True, slots=True)
class ConstructionConfig:
    """Pinned construction signatures used in stable artifacts and snapshots."""

    extractor_signature: str
    prompt_signature: str
    code_signature: str = "knowledge-construction:v1"
    normalizer_signature: str = "unicode-nfc:v1"

    def __post_init__(self) -> None:
        for name in (
            "extractor_signature",
            "prompt_signature",
            "code_signature",
            "normalizer_signature",
        ):
            object.__setattr__(self, name, _required(getattr(self, name), name))


@dataclass(frozen=True, slots=True)
class ObservedDocumentState:
    """Tenant-scoped lifecycle state observed before a CAS publication attempt."""

    expected_active_snapshot_id: str | None
    source_generation: int
    version_number: int
    access_policy_id: str
    access_policy_version: int
    access_groups: frozenset[str]

    def __post_init__(self) -> None:
        if self.source_generation < 0:
            raise ValueError("source_generation must not be negative")
        if self.version_number <= 0 or self.access_policy_version <= 0:
            raise ValueError("version and access-policy versions must be positive")
        object.__setattr__(
            self,
            "access_policy_id",
            _required(self.access_policy_id, "access_policy_id"),
        )
        if not self.access_groups:
            raise ValueError("access_groups must not be empty")


@dataclass(frozen=True, slots=True)
class ConstructionJobState:
    job_id: str
    tenant_id: str
    operation_key: str
    request_fingerprint: str
    document_id: str
    version_id: str
    snapshot_id: str
    tbox_id: str
    expected_active_snapshot_id: str | None
    source_generation: int
    version_number: int
    access_policy_id: str
    access_policy_version: int
    access_groups: frozenset[str]
    created_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "job_id",
            "tenant_id",
            "operation_key",
            "request_fingerprint",
            "document_id",
            "version_id",
            "snapshot_id",
            "tbox_id",
            "access_policy_id",
        ):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        if self.source_generation < 0:
            raise ValueError("source_generation must not be negative")
        if self.version_number <= 0 or self.access_policy_version <= 0:
            raise ValueError("version and access-policy versions must be positive")
        if not self.access_groups:
            raise ValueError("access_groups must not be empty")
        _aware(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class ConstructionChunkResult:
    chunk_id: str
    artifact_id: str
    status: str
    finding_codes: tuple[str, ...]
    mention_record_ids: tuple[str, ...]
    assertion_record_ids: tuple[str, ...]
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class KnowledgeConstructionResult:
    job_id: str
    tenant_id: str
    document_id: str
    version_id: str
    snapshot_id: str
    tbox_id: str
    ingestion: IngestionResult
    chunks: tuple[ConstructionChunkResult, ...]


class ConstructionConflict(RuntimeError):
    """Stable workflow identity or immutable audit data conflicts."""


class ConstructionAuthorizationError(PermissionError):
    """The principal cannot update an existing source identity."""


class IncrementalPipeline(Protocol):
    def run(
        self,
        request: IncrementalIngestionRequest,
        extraction_provider: Callable[..., ExtractionOutput],
        embedding_provider: EmbeddingProvider,
    ) -> IngestionResult: ...


class TBoxStore(Protocol):
    def active(self, tenant_id: str, key: str) -> TBoxVersion | None: ...


class OntologyExtractor(Protocol):
    active_tbox: TBoxVersion
    model: str
    prompt_version: str

    def extract_audited(
        self,
        *,
        artifact_id: str,
        input_hash: str,
        chunk: Chunk,
        profile: GraphPipelineProfile,
    ) -> AuditedExtraction: ...


class KnowledgeStore(Protocol):
    def persist_llm_candidates(self, batch: ABoxRecordBatch) -> object: ...

    def persist_llm_quarantined(self, batch: ABoxRecordBatch) -> object: ...

    def get_entity_mention(
        self,
        principal: Principal,
        record_id: str,
        *,
        statuses: tuple[GovernanceStatus, ...],
    ) -> EntityMentionRecord | None: ...

    def get_assertion(
        self,
        principal: Principal,
        record_id: str,
        *,
        statuses: tuple[GovernanceStatus, ...],
    ) -> AssertionRecord | None: ...


class ConstructionAuditStore(Protocol):
    def observe_document(
        self,
        principal: Principal,
        *,
        document_id_value: str,
        version_id_value: str,
        canonical_uri: str,
        source_name: str,
    ) -> ObservedDocumentState: ...

    def ensure_job(
        self,
        state: ConstructionJobState,
        *,
        expected_chunks: int,
    ) -> ConstructionJobState: ...

    def read_artifact(
        self,
        *,
        tenant_id: str,
        artifact_id: str,
        input_hash: str,
        profile_id: str,
    ) -> dict[str, Any] | None: ...

    def persist_artifact(
        self,
        *,
        tenant_id: str,
        artifact_id: str,
        input_hash: str,
        profile_id: str,
        payload: dict[str, Any],
        created_at: datetime,
    ) -> None: ...

    def read_outcome(
        self,
        principal: Principal,
        *,
        job_id: str,
        chunk_id: str,
    ) -> ConstructionChunkResult | None: ...

    def persist_outcome(
        self,
        principal: Principal,
        *,
        job_id: str,
        result: ConstructionChunkResult,
        access_groups: frozenset[str],
        artifact_input_hash: str,
        artifact_profile_id: str,
        completed_at: datetime,
    ) -> None: ...

    def complete_job(
        self,
        *,
        tenant_id: str,
        job_id: str,
        completed_at: datetime,
    ) -> None: ...

    def record_retryable_failure(
        self,
        *,
        tenant_id: str,
        job_id: str,
        chunk_id: str,
        findings: tuple[ExtractionFinding, ...],
        failed_at: datetime,
    ) -> None: ...


class Neo4jConstructionAuditStore:
    """Durable, tenant-scoped construction jobs, artifacts, and outcomes."""

    def __init__(self, driver: Any, database: str = "neo4j") -> None:
        self.driver = driver
        self.database = database

    def observe_document(
        self,
        principal: Principal,
        *,
        document_id_value: str,
        version_id_value: str,
        canonical_uri: str,
        source_name: str,
    ) -> ObservedDocumentState:
        _require_construction_capability(principal)
        with self.driver.session(database=self.database) as session:
            row = session.run(
                """
                OPTIONAL MATCH (document:Document {
                    tenant_id: $tenant_id,
                    document_id: $document_id
                })
                OPTIONAL MATCH (document)-[:ACTIVE_SNAPSHOT]->(
                    active_snapshot:KnowledgeSnapshot {tenant_id: $tenant_id}
                )
                OPTIONAL MATCH (document)-[:HAS_VERSION]->(
                    known_version:DocumentVersion {tenant_id: $tenant_id}
                )
                WITH document, active_snapshot,
                     max(known_version.version_number) AS max_version_number
                OPTIONAL MATCH (document)-[:HAS_VERSION]->(
                    target_version:DocumentVersion {
                        tenant_id: $tenant_id,
                        version_id: $version_id
                    }
                )
                OPTIONAL MATCH (tombstone:DocumentTombstone {
                    tenant_id: $tenant_id,
                    document_id: $document_id
                })
                RETURN document.document_id AS existing_document_id,
                       document.canonical_uri AS canonical_uri,
                       document.source_name AS source_name,
                       document.access_policy_id AS access_policy_id,
                       document.access_policy_version AS access_policy_version,
                       document.access_groups AS access_groups,
                       active_snapshot.snapshot_id AS active_snapshot_id,
                       coalesce(document.generation, tombstone.generation, 0)
                           AS source_generation,
                       target_version.version_number AS target_version_number,
                       coalesce(max_version_number, 0) AS max_version_number,
                       CASE WHEN document IS NULL THEN true ELSE any(
                           group IN $principal_groups
                           WHERE group IN document.access_groups
                       ) END AS authorized
                """,
                tenant_id=principal.tenant_id,
                document_id=document_id_value,
                version_id=version_id_value,
                principal_groups=sorted(principal.groups),
            ).single()
        if row is None:
            raise ConstructionConflict("document lifecycle state is unavailable")
        exists = row["existing_document_id"] is not None
        if exists and not row["authorized"]:
            raise ConstructionAuthorizationError("source is unavailable to this principal")
        if exists and (
            row["canonical_uri"] != canonical_uri or row["source_name"] != source_name
        ):
            raise ConstructionConflict("source identity conflicts with the stored Document")
        if exists:
            groups = frozenset(row["access_groups"] or ())
            policy_id = row["access_policy_id"]
            policy_version = int(row["access_policy_version"])
        else:
            groups = principal.groups
            policy_id = "principal-acl:" + content_checksum(
                json.dumps(
                    [principal.tenant_id, sorted(groups)],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            policy_version = 1
        target_number = row["target_version_number"]
        return ObservedDocumentState(
            expected_active_snapshot_id=row["active_snapshot_id"],
            source_generation=int(row["source_generation"]),
            version_number=(
                int(target_number)
                if target_number is not None
                else int(row["max_version_number"]) + 1
            ),
            access_policy_id=policy_id,
            access_policy_version=policy_version,
            access_groups=groups,
        )

    def ensure_job(
        self,
        state: ConstructionJobState,
        *,
        expected_chunks: int,
    ) -> ConstructionJobState:
        identity = {
            "job_id": state.job_id,
            "tenant_id": state.tenant_id,
            "operation_key": state.operation_key,
            "request_fingerprint": state.request_fingerprint,
            "document_id": state.document_id,
            "version_id": state.version_id,
            "snapshot_id": state.snapshot_id,
            "tbox_id": state.tbox_id,
            "expected_chunks": expected_chunks,
        }
        captured_lifecycle = {
            "expected_active_snapshot_id": state.expected_active_snapshot_id or "",
            "source_generation": state.source_generation,
            "version_number": state.version_number,
            "access_policy_id": state.access_policy_id,
            "access_policy_version": state.access_policy_version,
            "access_groups": sorted(state.access_groups),
        }
        with self.driver.session(database=self.database) as session:
            row = session.execute_write(
                self._ensure_job_tx,
                identity,
                captured_lifecycle,
                state.created_at,
            )
        if row is None or not row["compatible"]:
            raise ConstructionConflict("construction idempotency key conflicts")
        stored = dict(row["job"])
        return ConstructionJobState(
            job_id=stored["job_id"],
            tenant_id=stored["tenant_id"],
            operation_key=stored["operation_key"],
            request_fingerprint=stored["request_fingerprint"],
            document_id=stored["document_id"],
            version_id=stored["version_id"],
            snapshot_id=stored["snapshot_id"],
            tbox_id=stored["tbox_id"],
            expected_active_snapshot_id=stored["expected_active_snapshot_id"] or None,
            source_generation=int(stored["source_generation"]),
            version_number=int(stored["version_number"]),
            access_policy_id=stored["access_policy_id"],
            access_policy_version=int(stored["access_policy_version"]),
            access_groups=frozenset(stored["access_groups"]),
            created_at=_native_datetime(stored["created_at"], "created_at"),
        )

    @staticmethod
    def _ensure_job_tx(
        tx: Any,
        identity: dict[str, object],
        captured_lifecycle: dict[str, object],
        created_at: datetime,
    ) -> Any:
        return tx.run(
            """
            MERGE (job:KnowledgeConstructionJob {job_id: $job_id})
            ON CREATE SET job = $identity,
                          job += $captured_lifecycle,
                          job.status = 'RUNNING',
                          job.completed_chunks = 0,
                          job.created_at = $created_at,
                          job.updated_at = $created_at
            RETURN all(
                       key IN keys($identity)
                       WHERE job[key] = $identity[key]
                   ) AS compatible,
                   job{.*} AS job
            """,
            job_id=identity["job_id"],
            identity=identity,
            captured_lifecycle=captured_lifecycle,
            created_at=created_at,
        ).single()

    def read_artifact(
        self,
        *,
        tenant_id: str,
        artifact_id: str,
        input_hash: str,
        profile_id: str,
    ) -> dict[str, Any] | None:
        with self.driver.session(database=self.database) as session:
            row = session.run(
                """
                MATCH (artifact:DerivationArtifact {
                    tenant_id: $tenant_id,
                    artifact_id: $artifact_id
                })
                RETURN artifact.kind AS kind,
                       artifact.input_hash AS input_hash,
                       artifact.profile_id AS profile_id,
                       artifact.payload_json AS payload_json,
                       artifact.output_checksum AS output_checksum
                """,
                tenant_id=tenant_id,
                artifact_id=artifact_id,
            ).single()
        if row is None:
            return None
        if (
            row["kind"] != AUDIT_ARTIFACT_KIND
            or row["input_hash"] != input_hash
            or row["profile_id"] != profile_id
        ):
            raise ConstructionConflict("ontology extraction artifact identity conflicts")
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ConstructionConflict("ontology extraction artifact JSON is invalid") from exc
        if not isinstance(payload, dict) or _fingerprint(payload) != row["output_checksum"]:
            raise ConstructionConflict("ontology extraction artifact checksum is invalid")
        return payload

    def persist_artifact(
        self,
        *,
        tenant_id: str,
        artifact_id: str,
        input_hash: str,
        profile_id: str,
        payload: dict[str, Any],
        created_at: datetime,
    ) -> None:
        immutable = {
            "artifact_id": artifact_id,
            "tenant_id": tenant_id,
            "kind": AUDIT_ARTIFACT_KIND,
            "input_hash": input_hash,
            "profile_id": profile_id,
            "output_checksum": _fingerprint(payload),
            "payload_json": json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        }
        with self.driver.session(database=self.database) as session:
            row = session.execute_write(
                lambda tx: tx.run(
                    """
                    MERGE (artifact:DerivationArtifact {artifact_id: $artifact_id})
                    ON CREATE SET artifact = $immutable,
                                  artifact.created_at = $created_at
                    RETURN all(
                        key IN keys($immutable)
                        WHERE artifact[key] = $immutable[key]
                    ) AS compatible
                    """,
                    artifact_id=artifact_id,
                    immutable=immutable,
                    created_at=created_at,
                ).single()
            )
        if row is None or not row["compatible"]:
            raise ConstructionConflict("ontology extraction artifact checksum conflicts")

    def read_outcome(
        self,
        principal: Principal,
        *,
        job_id: str,
        chunk_id: str,
    ) -> ConstructionChunkResult | None:
        _require_construction_capability(principal)
        with self.driver.session(database=self.database) as session:
            row = session.run(
                """
                MATCH (job:KnowledgeConstructionJob {
                    tenant_id: $tenant_id,
                    job_id: $job_id
                })-[:HAS_CHUNK_OUTCOME]->(outcome:KnowledgeConstructionChunkOutcome {
                    tenant_id: $tenant_id,
                    chunk_id: $chunk_id
                })-[:FOR_CHUNK]->(chunk:Chunk {
                    tenant_id: $tenant_id,
                    chunk_id: $chunk_id
                })
                MATCH (outcome)-[:USED_ARTIFACT]->(artifact:DerivationArtifact {
                    tenant_id: $tenant_id
                })
                MATCH (document:Document {tenant_id: $tenant_id})-[:HAS_VERSION]->
                      (version:DocumentVersion {tenant_id: $tenant_id})-[:HAS_CHUNK]->
                      (chunk)
                WHERE document.document_id = job.document_id
                  AND version.version_id = job.version_id
                  AND chunk.document_id = document.document_id
                  AND chunk.version_id = version.version_id
                  AND artifact.artifact_id = outcome.artifact_id
                  AND artifact.kind = $artifact_kind
                  AND artifact.input_hash = outcome.artifact_input_hash
                  AND artifact.profile_id = outcome.artifact_profile_id
                  AND any(group IN $groups WHERE group IN outcome.access_groups)
                  AND any(group IN $groups WHERE group IN chunk.access_groups)
                  AND any(group IN $groups WHERE group IN document.access_groups)
                RETURN outcome.result_json AS result_json
                """,
                tenant_id=principal.tenant_id,
                job_id=job_id,
                chunk_id=chunk_id,
                groups=sorted(principal.groups),
                artifact_kind=AUDIT_ARTIFACT_KIND,
            ).single()
        if row is None:
            return None
        try:
            payload = json.loads(row["result_json"])
            return _chunk_result_from_payload(payload, replayed=True)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ConstructionConflict("stored construction outcome is invalid") from exc

    def persist_outcome(
        self,
        principal: Principal,
        *,
        job_id: str,
        result: ConstructionChunkResult,
        access_groups: frozenset[str],
        artifact_input_hash: str,
        artifact_profile_id: str,
        completed_at: datetime,
    ) -> None:
        _require_construction_capability(principal)
        if not access_groups:
            raise ValueError("access_groups must not be empty")
        if not access_groups & principal.groups:
            raise ConstructionAuthorizationError("source is unavailable to this principal")
        outcome_id = ingestion_task_id(job_id, result.chunk_id)
        payload = _chunk_result_payload(result)
        immutable = {
            "outcome_id": outcome_id,
            "tenant_id": principal.tenant_id,
            "job_id": job_id,
            "chunk_id": result.chunk_id,
            "artifact_id": result.artifact_id,
            "artifact_input_hash": _required(
                artifact_input_hash,
                "artifact_input_hash",
            ),
            "artifact_profile_id": _required(
                artifact_profile_id,
                "artifact_profile_id",
            ),
            "status": result.status,
            "access_groups": sorted(access_groups),
            "result_checksum": _fingerprint(payload),
            "result_json": json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        }
        with self.driver.session(database=self.database) as session:
            session.execute_write(
                self._persist_outcome_tx,
                immutable,
                sorted(principal.groups),
                completed_at,
            )

    @staticmethod
    def _persist_outcome_tx(
        tx: Any,
        immutable: dict[str, object],
        groups: list[str],
        completed_at: datetime,
    ) -> None:
        row = tx.run(
            """
            MATCH (job:KnowledgeConstructionJob {
                tenant_id: $tenant_id,
                job_id: $job_id
            })
            MATCH (document:Document {tenant_id: $tenant_id})-[:HAS_VERSION]->
                  (version:DocumentVersion {tenant_id: $tenant_id})-[:HAS_CHUNK]->
                  (chunk:Chunk {tenant_id: $tenant_id, chunk_id: $chunk_id})
            WHERE document.document_id = job.document_id
              AND version.version_id = job.version_id
              AND chunk.document_id = document.document_id
              AND chunk.version_id = version.version_id
              AND $access_groups = chunk.access_groups
              AND any(group IN $groups WHERE group IN document.access_groups)
              AND any(group IN $groups WHERE group IN chunk.access_groups)
            MATCH (artifact:DerivationArtifact {
                tenant_id: $tenant_id,
                artifact_id: $artifact_id
            })
            WHERE artifact.kind = $artifact_kind
              AND artifact.input_hash = $artifact_input_hash
              AND artifact.profile_id = $artifact_profile_id
            MERGE (outcome:KnowledgeConstructionChunkOutcome {
                outcome_id: $outcome_id
            })
            ON CREATE SET outcome = $immutable,
                          outcome.completed_at = $completed_at
            MERGE (job)-[:HAS_CHUNK_OUTCOME]->(outcome)
            MERGE (outcome)-[:FOR_CHUNK]->(chunk)
            MERGE (outcome)-[:USED_ARTIFACT]->(artifact)
            WITH outcome, job
            SET job.completed_chunks = size([
                    (job)-[:HAS_CHUNK_OUTCOME]->(item) | item
                ]),
                job.status = CASE
                    WHEN job.status = 'COMPLETED' THEN 'COMPLETED'
                    ELSE 'RUNNING'
                END,
                job.updated_at = $completed_at
            RETURN all(
                key IN keys($immutable)
                WHERE outcome[key] = $immutable[key]
            ) AS compatible
            """,
            **immutable,
            immutable=immutable,
            groups=groups,
            artifact_kind=AUDIT_ARTIFACT_KIND,
            completed_at=completed_at,
        ).single()
        # These exceptions must occur inside execute_write so Neo4j rolls back
        # any MERGE/link performed earlier in this transaction.
        if row is None:
            raise ConstructionAuthorizationError("source is unavailable to this principal")
        if not row["compatible"]:
            raise ConstructionConflict("construction Chunk outcome conflicts")

    def complete_job(
        self,
        *,
        tenant_id: str,
        job_id: str,
        completed_at: datetime,
    ) -> None:
        with self.driver.session(database=self.database) as session:
            row = session.execute_write(
                lambda tx: tx.run(
                    """
                    MATCH (job:KnowledgeConstructionJob {
                        tenant_id: $tenant_id,
                        job_id: $job_id
                    })
                    OPTIONAL MATCH (job)-[:HAS_CHUNK_OUTCOME]->(
                        outcome:KnowledgeConstructionChunkOutcome {
                            tenant_id: $tenant_id
                        }
                    )
                    OPTIONAL MATCH (outcome)-[:USED_ARTIFACT]->(
                        artifact:DerivationArtifact {tenant_id: $tenant_id}
                    )
                    WITH job,
                         count(DISTINCT outcome) AS completed,
                         count(DISTINCT CASE
                             WHEN artifact.artifact_id = outcome.artifact_id
                              AND artifact.kind = $artifact_kind
                              AND artifact.input_hash = outcome.artifact_input_hash
                              AND artifact.profile_id = outcome.artifact_profile_id
                             THEN outcome
                         END) AS artifact_backed
                    WHERE completed = job.expected_chunks
                      AND artifact_backed = job.expected_chunks
                    SET job.status = 'COMPLETED',
                        job.completed_chunks = completed,
                        job.completed_at = coalesce(job.completed_at, $completed_at),
                        job.updated_at = $completed_at
                    RETURN job.job_id AS job_id
                    """,
                    tenant_id=tenant_id,
                    job_id=job_id,
                    artifact_kind=AUDIT_ARTIFACT_KIND,
                    completed_at=completed_at,
                ).single()
            )
        if row is None:
            raise ConstructionConflict("construction job has incomplete Chunk outcomes")

    def record_retryable_failure(
        self,
        *,
        tenant_id: str,
        job_id: str,
        chunk_id: str,
        findings: tuple[ExtractionFinding, ...],
        failed_at: datetime,
    ) -> None:
        with self.driver.session(database=self.database) as session:
            session.run(
                """
                MATCH (job:KnowledgeConstructionJob {
                    tenant_id: $tenant_id,
                    job_id: $job_id
                })
                WITH job, job.status = 'COMPLETED' AS already_completed
                SET job.status = CASE
                        WHEN already_completed THEN 'COMPLETED'
                        ELSE 'RETRY_WAIT'
                    END,
                    job.failed_chunk_id = CASE
                        WHEN already_completed THEN job.failed_chunk_id
                        ELSE $chunk_id
                    END,
                    job.last_finding_codes = CASE
                        WHEN already_completed THEN job.last_finding_codes
                        ELSE $finding_codes
                    END,
                    job.updated_at = $failed_at
                """,
                tenant_id=tenant_id,
                job_id=job_id,
                chunk_id=chunk_id,
                finding_codes=[item.code for item in findings],
                failed_at=failed_at,
            ).consume()


def _native_datetime(value: object, name: str) -> datetime:
    if hasattr(value, "to_native"):
        value = value.to_native()
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ConstructionConflict(f"stored {name} is not timezone-aware")
    return value


def _chunk_result_payload(result: ConstructionChunkResult) -> dict[str, object]:
    return {
        "format_version": 1,
        "chunk_id": result.chunk_id,
        "artifact_id": result.artifact_id,
        "status": result.status,
        "finding_codes": list(result.finding_codes),
        "mention_record_ids": list(result.mention_record_ids),
        "assertion_record_ids": list(result.assertion_record_ids),
    }


def _chunk_result_from_payload(
    payload: dict[str, Any],
    *,
    replayed: bool,
) -> ConstructionChunkResult:
    if payload.get("format_version") != 1:
        raise ValueError("unsupported construction outcome format")
    return ConstructionChunkResult(
        chunk_id=_required(payload["chunk_id"], "chunk_id"),
        artifact_id=_required(payload["artifact_id"], "artifact_id"),
        status=_required(payload["status"], "status"),
        finding_codes=tuple(_required(item, "finding code") for item in payload["finding_codes"]),
        mention_record_ids=tuple(
            _required(item, "mention record ID") for item in payload["mention_record_ids"]
        ),
        assertion_record_ids=tuple(
            _required(item, "assertion record ID") for item in payload["assertion_record_ids"]
        ),
        replayed=replayed,
    )


def _audit_input_hash(
    chunk: Chunk,
    tbox: TBoxVersion,
    extractor: OntologyExtractor,
    profile: GraphPipelineProfile,
) -> str:
    return content_checksum(
        json.dumps(
            {
                "chunk_id": chunk.chunk_id,
                "chunk_checksum": chunk.checksum,
                "tbox_id": tbox.tbox_id,
                "tbox_checksum": tbox.checksum,
                "model": extractor.model,
                "extractor_signature": profile.extractor_signature,
                "prompt_signature": profile.prompt_signature,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def _output_bundle(
    document: Document,
    version: DocumentVersion,
    chunk: Chunk,
    output: ExtractionOutput,
) -> ProvenanceBundle:
    return ProvenanceBundle(
        document=document,
        version=version,
        chunk=chunk,
        embedding=None,
        entities=output.entities,
        mentions=output.mentions,
        assertion=output.assertions[0] if output.assertions else None,
        additional_assertions=output.assertions[1:],
        activate_version=False,
    )


def _audited_payload(
    result: AuditedExtraction,
    *,
    document: Document,
    version: DocumentVersion,
    chunk: Chunk,
    extracted_at: datetime,
) -> dict[str, Any]:
    return {
        "format_version": 1,
        "disposition": result.status.value,
        "ontology_version_id": result.ontology_version_id,
        "ontology_checksum": result.ontology_checksum,
        "extractor_version": result.extractor_version,
        "prompt_version": result.prompt_version,
        "model": result.model,
        "extracted_at": extracted_at.isoformat(),
        "findings": [
            {
                "code": item.code,
                "action": item.action,
                "path": item.path,
                "detail": item.detail,
            }
            for item in result.findings
        ],
        "output": encode_extraction(
            _output_bundle(document, version, chunk, result.output)
        ),
    }


def _rejected_payload(
    findings: tuple[ExtractionFinding, ...],
    *,
    tbox: TBoxVersion,
    extractor: OntologyExtractor,
    profile: GraphPipelineProfile,
    rejected_at: datetime,
) -> dict[str, Any]:
    return {
        "format_version": 1,
        "disposition": GovernanceStatus.REJECTED.value,
        "ontology_version_id": tbox.tbox_id,
        "ontology_checksum": tbox.checksum,
        "extractor_version": profile.extractor_signature,
        "prompt_version": profile.prompt_signature,
        "model": extractor.model,
        "extracted_at": rejected_at.isoformat(),
        "findings": [
            {
                "code": item.code,
                "action": item.action,
                "path": item.path,
                "detail": item.detail,
            }
            for item in findings
        ],
    }


def _decode_audited_payload(
    payload: dict[str, Any],
    *,
    tbox: TBoxVersion,
    extractor: OntologyExtractor,
    chunk: Chunk,
    profile: GraphPipelineProfile,
) -> tuple[AuditedExtraction | None, tuple[ExtractionFinding, ...], datetime]:
    if payload.get("format_version") != 1:
        raise ConstructionConflict("unsupported ontology extraction audit format")
    if (
        payload.get("ontology_version_id") != tbox.tbox_id
        or payload.get("ontology_checksum") != tbox.checksum
        or payload.get("extractor_version") != profile.extractor_signature
        or payload.get("prompt_version") != profile.prompt_signature
        or payload.get("model") != extractor.model
    ):
        raise ConstructionConflict("ontology extraction audit configuration conflicts")
    try:
        findings = tuple(
            ExtractionFinding(
                code=item["code"],
                action=item["action"],
                path=item["path"],
                detail=item["detail"],
            )
            for item in payload["findings"]
        )
        extracted_at = _native_datetime(payload["extracted_at"], "extracted_at")
        disposition = GovernanceStatus(payload["disposition"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ConstructionConflict("ontology extraction audit metadata is invalid") from exc
    if disposition is GovernanceStatus.REJECTED:
        return None, findings, extracted_at
    if disposition not in {GovernanceStatus.CANDIDATE, GovernanceStatus.QUARANTINED}:
        raise ConstructionConflict("ontology extraction audit disposition is invalid")
    try:
        entities, mentions, assertions = decode_extraction(
            payload["output"],
            tenant_id=chunk.tenant_id,
            chunk=chunk,
            profile=profile,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ConstructionConflict("ontology extraction audit output is invalid") from exc
    audited = AuditedExtraction(
        output=ExtractionOutput(entities, mentions, assertions),
        origin=KnowledgeOrigin.LLM_EXTRACTED,
        authority=AuthorityLevel.SECONDARY,
        status=disposition,
        ontology_version_id=tbox.tbox_id,
        ontology_checksum=tbox.checksum,
        extractor_version=profile.extractor_signature,
        prompt_version=profile.prompt_signature,
        model=extractor.model,
        findings=findings,
    )
    return audited, findings, extracted_at
def _identity(entity: Entity) -> EntityIdentity:
    return EntityIdentity(
        entity_id=entity.entity_id,
        tenant_id=entity.tenant_id,
        entity_type=entity.entity_type,
        canonical_key=entity.canonical_key,
        canonical_name=entity.canonical_name,
        aliases=entity.aliases,
    )


def _evidence(
    chunk: Chunk,
    *,
    char_start: int,
    char_end: int,
) -> EvidenceReference:
    relative_start = char_start - chunk.char_start
    relative_end = char_end - chunk.char_start
    if relative_start < 0 or relative_end > len(chunk.text):
        raise ConstructionConflict("model output evidence lies outside its Chunk")
    return EvidenceReference(
        tenant_id=chunk.tenant_id,
        document_id=chunk.document_id,
        version_id=chunk.version_id,
        chunk_id=chunk.chunk_id,
        char_start=char_start,
        char_end=char_end,
        quoted_text=chunk.text[relative_start:relative_end],
        access_policy_id=chunk.access_policy_id,
        access_policy_version=chunk.access_policy_version,
        access_groups=chunk.access_groups,
    )


def _to_abox_batch(
    audited: AuditedExtraction,
    *,
    chunk: Chunk,
    extracted_at: datetime,
) -> ABoxRecordBatch | None:
    if not audited.output.mentions:
        if audited.output.entities or audited.output.assertions:
            raise ConstructionConflict("extraction output has graph data without mentions")
        return None
    if audited.status is GovernanceStatus.CANDIDATE:
        trust = llm_candidate_trust(
            ontology_version_id=audited.ontology_version_id,
            extractor_version=audited.extractor_version,
            prompt_version=audited.prompt_version,
            extracted_at=extracted_at,
        )
    elif audited.status is GovernanceStatus.QUARANTINED:
        trust = llm_quarantined_trust(
            ontology_version_id=audited.ontology_version_id,
            extractor_version=audited.extractor_version,
            prompt_version=audited.prompt_version,
            extracted_at=extracted_at,
        )
    else:
        raise ConstructionConflict("only candidate or quarantined output can form A-Box records")

    entities = {item.entity_id: _identity(item) for item in audited.output.entities}
    mentions: list[EntityMentionRecord] = []
    source_mentions: dict[str, EntityMention] = {}
    for source in audited.output.mentions:
        entity = entities.get(source.entity_id)
        if entity is None:
            raise ConstructionConflict("model mention references an absent entity")
        record_id_value = knowledge_record_id(
            chunk.tenant_id,
            "ENTITY_MENTION",
            source.mention_id,
        )
        mentions.append(
            EntityMentionRecord(
                revision=RecordRevision.next(record_id_value, 0),
                tenant_id=chunk.tenant_id,
                entity=entity,
                evidence=_evidence(
                    chunk,
                    char_start=source.char_start,
                    char_end=source.char_end,
                ),
                confidence=source.confidence,
                trust=trust,
                created_at=extracted_at,
            )
        )
        source_mentions[source.mention_id] = source
    records_by_source_id = {
        source.mention_id: record
        for source, record in zip(audited.output.mentions, mentions, strict=True)
    }

    def endpoint(assertion: Assertion, entity_id_value: str) -> EntityMentionRecord:
        candidates = [
            records_by_source_id[source.mention_id]
            for source in source_mentions.values()
            if source.entity_id == entity_id_value
            and assertion.evidence_char_start <= source.char_start
            and source.char_end <= assertion.evidence_char_end
        ]
        if not candidates:
            raise ConstructionConflict("assertion evidence lacks an endpoint mention")
        return min(
            candidates,
            key=lambda item: (
                item.evidence.char_start,
                item.evidence.char_end,
                item.record_id,
            ),
        )

    assertions: list[AssertionRecord] = []
    for source in audited.output.assertions:
        subject = entities.get(source.subject_entity_id)
        if subject is None:
            raise ConstructionConflict("assertion subject references an absent entity")
        subject_mention = endpoint(source, source.subject_entity_id)
        object_entity = (
            None
            if source.object_entity_id is None
            else entities.get(source.object_entity_id)
        )
        if source.object_entity_id is not None and object_entity is None:
            raise ConstructionConflict("assertion object references an absent entity")
        object_mention = (
            None
            if object_entity is None
            else endpoint(source, object_entity.entity_id)
        )
        record_id_value = knowledge_record_id(
            chunk.tenant_id,
            "ASSERTION",
            source.assertion_id,
        )
        assertions.append(
            AssertionRecord(
                revision=RecordRevision.next(record_id_value, 0),
                tenant_id=chunk.tenant_id,
                subject=subject,
                predicate=source.predicate,
                evidence=_evidence(
                    chunk,
                    char_start=source.evidence_char_start,
                    char_end=source.evidence_char_end,
                ),
                subject_mention_revision_id=subject_mention.revision_id,
                confidence=source.confidence,
                trust=trust,
                created_at=extracted_at,
                object_entity=object_entity,
                object_mention_revision_id=(
                    None if object_mention is None else object_mention.revision_id
                ),
                literal_value=source.literal_value,
                literal_semantics=source.literal_semantics,
            )
        )
    return ABoxRecordBatch(
        tenant_id=chunk.tenant_id,
        mentions=tuple(mentions),
        assertions=tuple(assertions),
    )


_ALL_GOVERNANCE_STATUSES = tuple(GovernanceStatus)


def _same_mention_lineage(
    stored: EntityMentionRecord,
    expected: EntityMentionRecord,
) -> bool:
    return (
        stored.record_id == expected.record_id
        and stored.tenant_id == expected.tenant_id
        and stored.entity == expected.entity
        and stored.evidence == expected.evidence
        and stored.trust.origin == expected.trust.origin
        and stored.trust.authority == expected.trust.authority
        and stored.trust.ontology_version_id == expected.trust.ontology_version_id
        and stored.trust.extractor_version == expected.trust.extractor_version
        and stored.trust.prompt_version == expected.trust.prompt_version
    )


def _same_assertion_lineage(
    stored: AssertionRecord,
    expected: AssertionRecord,
) -> bool:
    return (
        stored.record_id == expected.record_id
        and stored.tenant_id == expected.tenant_id
        and stored.subject == expected.subject
        and stored.predicate == expected.predicate
        and stored.evidence == expected.evidence
        and stored.object_entity == expected.object_entity
        and stored.literal_value == expected.literal_value
        and stored.literal_semantics == expected.literal_semantics
        and stored.trust.origin == expected.trust.origin
        and stored.trust.authority == expected.trust.authority
        and stored.trust.ontology_version_id == expected.trust.ontology_version_id
        and stored.trust.extractor_version == expected.trust.extractor_version
        and stored.trust.prompt_version == expected.trust.prompt_version
    )


def _batch_is_already_persisted(
    store: KnowledgeStore,
    principal: Principal,
    batch: ABoxRecordBatch,
) -> bool:
    observed: list[bool] = []
    compatible = True
    for expected in batch.mentions:
        stored = store.get_entity_mention(
            principal,
            expected.record_id,
            statuses=_ALL_GOVERNANCE_STATUSES,
        )
        observed.append(stored is not None)
        compatible = compatible and (
            stored is None or _same_mention_lineage(stored, expected)
        )
    for expected in batch.assertions:
        stored = store.get_assertion(
            principal,
            expected.record_id,
            statuses=_ALL_GOVERNANCE_STATUSES,
        )
        observed.append(stored is not None)
        compatible = compatible and (
            stored is None or _same_assertion_lineage(stored, expected)
        )
    if not compatible or (any(observed) and not all(observed)):
        raise KnowledgeConflict("construction replay found conflicting A-Box record lineage")
    return bool(observed) and all(observed)


class Neo4jKnowledgeConstructionWorkflow:
    """Publish upload evidence, then construct review-gated property-graph data."""

    def __init__(
        self,
        *,
        driver: Any,
        pipeline: IncrementalPipeline,
        embedding_provider: EmbeddingProvider,
        embedding_profile: EmbeddingProfile,
        extractor_factory: Callable[[TBoxVersion], OntologyExtractor],
        config: ConstructionConfig,
        database: str = "neo4j",
        parser: BoundedDocumentParser | None = None,
        tbox_store: TBoxStore | None = None,
        knowledge_store: KnowledgeStore | None = None,
        audit_store: ConstructionAuditStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.embedding_provider = embedding_provider
        self.embedding_profile = embedding_profile
        self.extractor_factory = extractor_factory
        self.config = config
        self.parser = parser or BoundedDocumentParser()
        self.tbox_store = tbox_store or Neo4jTBoxStore(driver, database)
        self.knowledge_store = knowledge_store or Neo4jKnowledgeStore(driver, database)
        self.audit_store = audit_store or Neo4jConstructionAuditStore(driver, database)
        self.clock = clock or (lambda: datetime.now(UTC))

    def run(
        self,
        principal: Principal,
        payload: bytes,
        metadata: ConstructionMetadata,
    ) -> KnowledgeConstructionResult:
        if not isinstance(principal, Principal):
            raise TypeError("principal must be an authenticated Principal")
        _require_construction_capability(principal)
        if not isinstance(metadata, ConstructionMetadata):
            raise TypeError("metadata must be ConstructionMetadata")
        parsed = self.parser.parse(payload, mime_type=metadata.mime_type)
        tbox = self.tbox_store.active(principal.tenant_id, metadata.tbox_key)
        if tbox is None or tbox.status is not TBoxStatus.PUBLISHED:
            raise ConstructionConflict("tenant has no active PUBLISHED T-Box for this key")
        extractor = self.extractor_factory(tbox)
        if (
            extractor.active_tbox.tenant_id != principal.tenant_id
            or extractor.active_tbox.tbox_id != tbox.tbox_id
            or extractor.active_tbox.checksum != tbox.checksum
        ):
            raise ConstructionConflict("extractor is not pinned to the active tenant T-Box")
        if extractor.prompt_version != self.config.prompt_signature:
            raise ConstructionConflict("extractor prompt version differs from configuration")

        governance = tbox.compile_governance_policy()
        ingestion_profile = _profile(
            splitter_signature=parsed.splitter_signature,
            extractor_signature=CANONICAL_EMPTY_EXTRACTOR_SIGNATURE,
            prompt_signature=CANONICAL_EMPTY_PROMPT_SIGNATURE,
            schema_signature=governance.policy_id,
            code_signature=self.config.code_signature,
            normalizer_signature=self.config.normalizer_signature,
        )
        extraction_profile = _profile(
            splitter_signature=parsed.splitter_signature,
            extractor_signature=self.config.extractor_signature,
            prompt_signature=self.config.prompt_signature,
            schema_signature=governance.policy_id,
            code_signature=self.config.code_signature,
            normalizer_signature=self.config.normalizer_signature,
        )
        document_id_value = document_id(principal.tenant_id, metadata.canonical_uri)
        version_id_value = version_id(
            document_id_value,
            parsed.normalized_checksum,
            parsed.original_checksum,
        )
        snapshot_id_value = knowledge_snapshot_id(
            version_id_value,
            ingestion_profile.profile_id,
        )
        observed = self.audit_store.observe_document(
            principal,
            document_id_value=document_id_value,
            version_id_value=version_id_value,
            canonical_uri=metadata.canonical_uri,
            source_name=metadata.source_name,
        )
        request_fingerprint = content_checksum(
            json.dumps(
                {
                    "tenant_id": principal.tenant_id,
                    "operation_key": metadata.operation_key,
                    "canonical_uri": metadata.canonical_uri,
                    "title": metadata.title,
                    "source_name": metadata.source_name,
                    "mime_type": parsed.mime_type,
                    "language": metadata.language,
                    "published_at": (
                        None
                        if metadata.published_at is None
                        else metadata.published_at.isoformat()
                    ),
                    "original_checksum": parsed.original_checksum,
                    "normalized_checksum": parsed.normalized_checksum,
                    "tbox_id": tbox.tbox_id,
                    "tbox_checksum": tbox.checksum,
                    "ingestion_profile_id": ingestion_profile.profile_id,
                    "extraction_profile_id": extraction_profile.profile_id,
                    "principal_groups": sorted(principal.groups),
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        now = self.clock()
        if _aware(now, "clock result") is None:
            raise AssertionError("unreachable")
        job = self.audit_store.ensure_job(
            ConstructionJobState(
                job_id=ingestion_job_id(
                    principal.tenant_id,
                    "KNOWLEDGE_CONSTRUCTION",
                    metadata.operation_key,
                ),
                tenant_id=principal.tenant_id,
                operation_key=metadata.operation_key,
                request_fingerprint=request_fingerprint,
                document_id=document_id_value,
                version_id=version_id_value,
                snapshot_id=snapshot_id_value,
                tbox_id=tbox.tbox_id,
                expected_active_snapshot_id=observed.expected_active_snapshot_id,
                source_generation=observed.source_generation,
                version_number=observed.version_number,
                access_policy_id=observed.access_policy_id,
                access_policy_version=observed.access_policy_version,
                access_groups=observed.access_groups,
                created_at=now,
            ),
            expected_chunks=len(parsed.chunks),
        )
        ingestion_request = self._ingestion_request(
            metadata,
            parsed,
            tbox,
            ingestion_profile,
            job,
        )
        ingestion = self.pipeline.run(
            ingestion_request,
            _empty_canonical_extraction,
            self.embedding_provider,
        )
        document, version, chunks = ingestion_request.domain_inputs()
        results = tuple(
            self._process_chunk(
                principal=principal,
                job=job,
                tbox=tbox,
                extractor=extractor,
                profile=extraction_profile,
                document=document,
                version=version,
                chunk=chunk,
            )
            for chunk in chunks
        )
        self.audit_store.complete_job(
            tenant_id=principal.tenant_id,
            job_id=job.job_id,
            completed_at=self.clock(),
        )
        return KnowledgeConstructionResult(
            job_id=job.job_id,
            tenant_id=principal.tenant_id,
            document_id=document.document_id,
            version_id=version.version_id,
            snapshot_id=ingestion_request.snapshot_id,
            tbox_id=tbox.tbox_id,
            ingestion=ingestion,
            chunks=results,
        )

    def _ingestion_request(
        self,
        metadata: ConstructionMetadata,
        parsed: ParsedDocument,
        tbox: TBoxVersion,
        profile: GraphPipelineProfile,
        job: ConstructionJobState,
    ) -> IncrementalIngestionRequest:
        return IncrementalIngestionRequest(
            operation_key=f"knowledge-construction:{metadata.operation_key}",
            tenant_id=job.tenant_id,
            canonical_uri=metadata.canonical_uri,
            title=metadata.title,
            source_name=metadata.source_name,
            mime_type=parsed.mime_type,
            language=metadata.language,
            published_at=metadata.published_at,
            access_policy_id=job.access_policy_id,
            access_policy_version=job.access_policy_version,
            access_groups=job.access_groups,
            source_generation=job.source_generation,
            expected_active_snapshot_id=job.expected_active_snapshot_id,
            chunks=parsed.chunks,
            profile=profile,
            governance_policy=tbox.compile_governance_policy(),
            embedding_profile=self.embedding_profile,
            version_number=job.version_number,
            ingested_at=job.created_at,
            original_checksum=parsed.original_checksum,
            max_attempts=metadata.max_attempts,
        )

    def _process_chunk(
        self,
        *,
        principal: Principal,
        job: ConstructionJobState,
        tbox: TBoxVersion,
        extractor: OntologyExtractor,
        profile: GraphPipelineProfile,
        document: Document,
        version: DocumentVersion,
        chunk: Chunk,
    ) -> ConstructionChunkResult:
        completed = self.audit_store.read_outcome(
            principal,
            job_id=job.job_id,
            chunk_id=chunk.chunk_id,
        )
        if completed is not None:
            return completed
        input_hash = _audit_input_hash(chunk, tbox, extractor, profile)
        artifact_id = derivation_artifact_id(
            principal.tenant_id,
            AUDIT_ARTIFACT_KIND,
            input_hash,
            profile.profile_id,
        )
        payload = self.audit_store.read_artifact(
            tenant_id=principal.tenant_id,
            artifact_id=artifact_id,
            input_hash=input_hash,
            profile_id=profile.profile_id,
        )
        if payload is None:
            extracted_at = self.clock()
            try:
                audited = extractor.extract_audited(
                    artifact_id=artifact_id,
                    input_hash=input_hash,
                    chunk=chunk,
                    profile=profile,
                )
            except ExtractionRejected as exc:
                if any(item.code == "MODEL_CALL_FAILED" for item in exc.findings):
                    self.audit_store.record_retryable_failure(
                        tenant_id=principal.tenant_id,
                        job_id=job.job_id,
                        chunk_id=chunk.chunk_id,
                        findings=exc.findings,
                        failed_at=extracted_at,
                    )
                    raise
                payload = _rejected_payload(
                    exc.findings,
                    tbox=tbox,
                    extractor=extractor,
                    profile=profile,
                    rejected_at=extracted_at,
                )
            else:
                payload = _audited_payload(
                    audited,
                    document=document,
                    version=version,
                    chunk=chunk,
                    extracted_at=extracted_at,
                )
            self.audit_store.persist_artifact(
                tenant_id=principal.tenant_id,
                artifact_id=artifact_id,
                input_hash=input_hash,
                profile_id=profile.profile_id,
                payload=payload,
                created_at=extracted_at,
            )
        audited, findings, extracted_at = _decode_audited_payload(
            payload,
            tbox=tbox,
            extractor=extractor,
            chunk=chunk,
            profile=profile,
        )
        batch = (
            None
            if audited is None
            else _to_abox_batch(audited, chunk=chunk, extracted_at=extracted_at)
        )
        if batch is not None and not _batch_is_already_persisted(
            self.knowledge_store,
            principal,
            batch,
        ):
            if audited is not None and audited.status is GovernanceStatus.CANDIDATE:
                self.knowledge_store.persist_llm_candidates(batch)
            elif audited is not None and audited.status is GovernanceStatus.QUARANTINED:
                self.knowledge_store.persist_llm_quarantined(batch)
            else:
                raise ConstructionConflict("invalid A-Box persistence lane")
        status = (
            GovernanceStatus.REJECTED.value
            if audited is None
            else (
                "EMPTY"
                if batch is None
                else audited.status.value
            )
        )
        result = ConstructionChunkResult(
            chunk_id=chunk.chunk_id,
            artifact_id=artifact_id,
            status=status,
            finding_codes=tuple(item.code for item in findings),
            mention_record_ids=(
                () if batch is None else tuple(item.record_id for item in batch.mentions)
            ),
            assertion_record_ids=(
                () if batch is None else tuple(item.record_id for item in batch.assertions)
            ),
        )
        self.audit_store.persist_outcome(
            principal,
            job_id=job.job_id,
            result=result,
            access_groups=chunk.access_groups,
            artifact_input_hash=input_hash,
            artifact_profile_id=profile.profile_id,
            completed_at=self.clock(),
        )
        return result


def _empty_canonical_extraction(**_kwargs: object) -> ExtractionOutput:
    """The canonical ingestion graph must never receive unreviewed model data."""

    return ExtractionOutput(entities=(), mentions=(), assertions=())


__all__ = [
    "AUDIT_ARTIFACT_KIND",
    "CANONICAL_EMPTY_EXTRACTOR_SIGNATURE",
    "CANONICAL_EMPTY_PROMPT_SIGNATURE",
    "KNOWLEDGE_CONSTRUCTION_CAPABILITY",
    "ConstructionAuthorizationError",
    "ConstructionChunkResult",
    "ConstructionConfig",
    "ConstructionConflict",
    "ConstructionJobState",
    "ConstructionMetadata",
    "KnowledgeConstructionResult",
    "Neo4jConstructionAuditStore",
    "Neo4jKnowledgeConstructionWorkflow",
    "ObservedDocumentState",
]
