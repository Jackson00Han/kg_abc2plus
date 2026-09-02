"""Trusted, batched Neo4j bootstrap ingestion for pre-built plans.

This module is deliberately narrower than :mod:`service`.  It accepts only an
already governed and validated :class:`IngestionPlan`, performs no provider
calls, and commits one complete document-version per transaction.  It is an
offline initial-load accelerator, not a replacement for resumable incremental
ingestion.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import math
from typing import Any, Callable, Iterable, Mapping, Protocol

from neo4j import unit_of_work

from graphrag_prod.domain import active_retrieval_scope, ingestion_job_id

from .models import Checkpoint, IngestionPlan, _fingerprint
from .service import IngestionConflict, Neo4jIngestionService, SystemClock


class Clock(Protocol):
    def now(self) -> datetime: ...


Failpoint = Callable[[Checkpoint, dict[str, Any]], None]


def _noop_failpoint(checkpoint: Checkpoint, context: dict[str, Any]) -> None:
    del checkpoint, context


@dataclass(frozen=True, slots=True)
class InitialLoadResult:
    """Durable outcome for one atomically loaded document-version."""

    job_id: str
    tenant_id: str
    document_id: str
    version_id: str
    snapshot_id: str
    outcome: str
    corpus_revision: int
    chunk_count: int
    embedding_count: int


@dataclass(frozen=True, slots=True)
class _Payload:
    job: dict[str, Any]
    profile: dict[str, Any]
    policy: dict[str, Any]
    document: dict[str, Any]
    version: dict[str, Any]
    snapshot: dict[str, Any]
    chunks: tuple[dict[str, Any], ...]
    embeddings: tuple[dict[str, Any], ...]
    entities: tuple[dict[str, Any], ...]
    mentions: tuple[dict[str, Any], ...]
    assertions: tuple[dict[str, Any], ...]
    entity_memberships: tuple[dict[str, Any], ...]
    findings: tuple[dict[str, Any], ...]


_NODE_SHAPES = {
    "job": ("IngestionJob", "job_id"),
    "profile": ("GraphPipelineProfile", "profile_id"),
    "policy": ("GraphGovernancePolicy", "policy_id"),
    "document": ("Document", "document_id"),
    "version": ("DocumentVersion", "version_id"),
    "snapshot": ("KnowledgeSnapshot", "snapshot_id"),
    "chunks": ("Chunk", "chunk_id"),
    "embeddings": ("ChunkEmbedding", "embedding_id"),
    "entities": ("Entity", "entity_id"),
    "mentions": ("EntityMention", "mention_id"),
    "assertions": ("Assertion", "assertion_id"),
    "findings": ("GraphGovernanceFinding", "finding_id"),
}


# One plan is one atomic document-version transaction.  These hard ceilings
# keep a trusted-but-malformed offline plan from becoming an unbounded Bolt
# parameter or Neo4j transaction.  The production-reference workload uses 50
# Chunks per version and remains comfortably below every limit.
MAX_CHUNKS_PER_PLAN = 256
MAX_EMBEDDINGS_PER_PLAN = 1_024
MAX_GRAPH_ROWS_PER_PLAN = 8_192
DEFAULT_INITIAL_LOAD_TRANSACTION_TIMEOUT_SECONDS = 60.0


def _present(values: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _node_row(
    identifier: str,
    immutable: Mapping[str, Any],
    *,
    create_extra: Mapping[str, Any] | None = None,
    replay_extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    absent = sorted(key for key, value in immutable.items() if value is None)
    immutable_present = _present(immutable)
    properties = {
        **immutable_present,
        **_present(create_extra or {}),
    }
    replay = {
        **immutable_present,
        **_present(replay_extra or {}),
    }
    replay_absent = sorted(
        {
            *absent,
            *(
                key
                for key, value in (replay_extra or {}).items()
                if value is None
            ),
        }
    )
    return {
        "identifier": identifier,
        "immutable": immutable_present,
        "absent": absent,
        "properties": properties,
        "replay": replay,
        "replay_absent": replay_absent,
    }


def _unique_rows(
    values: Iterable[dict[str, Any]],
    *,
    kind: str,
) -> tuple[dict[str, Any], ...]:
    unique: dict[str, dict[str, Any]] = {}
    for value in values:
        identifier = str(value["identifier"])
        previous = unique.setdefault(identifier, value)
        if previous != value:
            raise ValueError(f"conflicting duplicate {kind} {identifier}")
    return tuple(unique[key] for key in sorted(unique))


def _build_payload(plan: IngestionPlan, now: datetime) -> _Payload:
    """Convert one validated plan into bounded UNWIND row groups."""

    if not isinstance(plan, IngestionPlan):
        raise TypeError("bulk initial load requires an IngestionPlan")
    if not plan.bundles:
        raise ValueError("bulk initial load requires at least one bundle")
    chunk_count = len(plan.bundles)
    embedding_count = sum(len(bundle.all_embeddings) for bundle in plan.bundles)
    graph_row_count = (
        6
        + chunk_count
        + embedding_count
        + sum(len(bundle.entities) for bundle in plan.bundles)
        + sum(len(bundle.mentions) for bundle in plan.bundles)
        + sum(len(bundle.all_assertions) for bundle in plan.bundles)
        + len(plan.governance_findings)
    )
    if chunk_count > MAX_CHUNKS_PER_PLAN:
        raise ValueError(
            f"bulk initial load exceeds {MAX_CHUNKS_PER_PLAN} Chunks per plan"
        )
    if embedding_count > MAX_EMBEDDINGS_PER_PLAN:
        raise ValueError(
            "bulk initial load exceeds "
            f"{MAX_EMBEDDINGS_PER_PLAN} ChunkEmbeddings per plan"
        )
    if graph_row_count > MAX_GRAPH_ROWS_PER_PLAN:
        raise ValueError(
            "bulk initial load exceeds "
            f"{MAX_GRAPH_ROWS_PER_PLAN} graph rows per plan"
        )
    if any(not bundle.all_embeddings for bundle in plan.bundles):
        raise ValueError("bulk initial load requires an embedding for every Chunk")
    if any(
        not embedding.vector
        for bundle in plan.bundles
        for embedding in bundle.all_embeddings
    ):
        raise ValueError("bulk initial load requires materialized embedding vectors")

    first = plan.bundles[0]
    document = first.document
    version = first.version
    snapshot = plan.snapshot
    job_identifier = ingestion_job_id(
        plan.tenant_id,
        "INITIAL_LOAD",
        plan.operation_key,
    )
    job_immutable = {
        "job_id": job_identifier,
        "tenant_id": plan.tenant_id,
        "operation": "INITIAL_LOAD",
        "operation_key": plan.operation_key,
        "idempotency_key": plan.operation_key,
        "request_fingerprint": plan.request_fingerprint,
        "document_id": plan.document_id,
        "target_version_id": plan.version_id,
        "target_snapshot_id": snapshot.snapshot_id,
        "expected_active_snapshot_id": plan.expected_active_snapshot_id or "",
        "source_generation": plan.source_generation,
        "expected_tasks": len(plan.bundles),
        "max_attempts": 1,
    }
    job = _node_row(
        job_identifier,
        job_immutable,
        create_extra={
            "status": "RUNNING",
            "phase": "PUBLISH",
            "attempts": 1,
            "completed_tasks": 0,
            "created_at": now,
            "updated_at": now,
        },
    )

    profile_properties = asdict(plan.profile)
    profile = _node_row(
        plan.profile.profile_id,
        profile_properties,
    )
    policy_properties = {
        "policy_id": plan.governance_policy.policy_id,
        "policy_version": plan.governance_policy.policy_version,
        "payload_hash": plan.governance_policy.payload_hash,
        "payload": plan.governance_policy.canonical_payload,
    }
    policy = _node_row(plan.governance_policy.policy_id, policy_properties)

    document_identity = {
        "document_id": document.document_id,
        "tenant_id": document.tenant_id,
        "canonical_uri": document.canonical_uri,
        "source_name": document.source_name,
    }
    document_state = {
        "title": document.title,
        "access_policy_id": document.access_policy_id,
        "access_policy_version": document.access_policy_version,
        "access_groups": sorted(document.access_groups),
        "generation": plan.source_generation,
    }
    document_row = _node_row(
        document.document_id,
        document_identity,
        create_extra={"created_at": document.created_at, **document_state},
        replay_extra={"created_at": document.created_at, **document_state},
    )

    version_identity = {
        "version_id": version.version_id,
        "document_id": version.document_id,
        "tenant_id": version.tenant_id,
        "checksum": version.checksum,
        "original_checksum": version.original_checksum,
        "normalized_text": version.normalized_text,
        "version_number": version.version_number,
        "mime_type": version.mime_type,
        "language": version.language,
        "published_at": version.published_at,
    }
    version_row = _node_row(
        version.version_id,
        version_identity,
        create_extra={
            "ingested_at": version.ingested_at,
            "first_ingested_at": version.ingested_at,
        },
        replay_extra={
            "ingested_at": version.ingested_at,
            "first_ingested_at": version.ingested_at,
        },
    )

    snapshot_identity = asdict(snapshot)
    snapshot_identity.pop("created_at")
    snapshot_identity.update(
        {
            "governance_policy_id": plan.governance_policy.policy_id,
            "governance_policy_version": plan.governance_policy.policy_version,
        }
    )
    snapshot_row = _node_row(
        snapshot.snapshot_id,
        snapshot_identity,
        create_extra={
            "created_at": snapshot.created_at,
            "build_state": "PUBLISHED",
            "actual_chunk_count": len(plan.bundles),
            "verified_at": now,
            "published_at": now,
        },
        replay_extra={
            "created_at": snapshot.created_at,
            "build_state": "PUBLISHED",
            "actual_chunk_count": len(plan.bundles),
        },
    )

    chunk_rows: list[dict[str, Any]] = []
    embedding_rows: list[dict[str, Any]] = []
    entity_rows: list[dict[str, Any]] = []
    mention_rows: list[dict[str, Any]] = []
    assertion_rows: list[dict[str, Any]] = []
    membership_rows: list[dict[str, Any]] = []
    for bundle in sorted(plan.bundles, key=lambda item: item.chunk.ordinal):
        chunk = bundle.chunk
        chunk_identity = {
            "chunk_id": chunk.chunk_id,
            "version_id": chunk.version_id,
            "document_id": chunk.document_id,
            "tenant_id": chunk.tenant_id,
            "ordinal": chunk.ordinal,
            "text": chunk.text,
            "checksum": chunk.checksum,
            "char_start": chunk.char_start,
            "char_end": chunk.char_end,
            "page_number": chunk.page_number,
            "section": chunk.section,
            "splitter_version": chunk.splitter_version,
        }
        chunk_access = {
            "access_policy_id": chunk.access_policy_id,
            "access_policy_version": chunk.access_policy_version,
            "access_groups": sorted(chunk.access_groups),
        }
        chunk_replay = {
            **chunk_access,
            "retrieval_scope": active_retrieval_scope(
                chunk.tenant_id,
                chunk.access_groups,
            ),
        }
        chunk_rows.append(
            _node_row(
                chunk.chunk_id,
                chunk_identity,
                create_extra=chunk_access,
                replay_extra=chunk_replay,
            )
        )
        for embedding in bundle.all_embeddings:
            embedding_identity = {
                "embedding_id": embedding.embedding_id,
                "tenant_id": embedding.tenant_id,
                "chunk_id": embedding.chunk_id,
                "embedding_space_id": embedding.embedding_space_id,
                "provider": embedding.provider,
                "model": embedding.model,
                "revision": embedding.revision,
                "dimensions": embedding.dimensions,
                "normalization": embedding.normalization,
                "vector": list(embedding.vector),
                "vector_checksum": embedding.vector_checksum,
            }
            embedding_rows.append(
                _node_row(
                    embedding.embedding_id,
                    embedding_identity,
                    create_extra={
                        "created_at": embedding.created_at,
                        "cosine_indexable": True,
                    },
                    replay_extra={
                        "created_at": embedding.created_at,
                        "cosine_indexable": True,
                    },
                )
            )
        for entity in bundle.entities:
            entity_identity = {
                "entity_id": entity.entity_id,
                "tenant_id": entity.tenant_id,
                "entity_type": entity.entity_type,
                "canonical_key": entity.canonical_key,
            }
            entity_rows.append(
                _node_row(
                    entity.entity_id,
                    entity_identity,
                    create_extra={
                        "canonical_name": entity.canonical_name,
                        "aliases": list(entity.aliases),
                        "governance_status": "ACCEPTED",
                        "governance_policy_id": plan.governance_policy.policy_id,
                    },
                )
            )
            membership_rows.append(
                {
                    "identifier": entity.entity_id,
                    "canonical_name": entity.canonical_name,
                    "aliases": list(entity.aliases),
                }
            )
        for mention in bundle.mentions:
            mention_identity = asdict(mention)
            mention_rows.append(
                _node_row(mention.mention_id, mention_identity)
            )
        for assertion in bundle.all_assertions:
            assertion_identity = {
                "assertion_id": assertion.assertion_id,
                "tenant_id": assertion.tenant_id,
                "subject_entity_id": assertion.subject_entity_id,
                "object_entity_id": assertion.object_entity_id,
                "predicate": assertion.predicate,
                "object_kind": (
                    "entity" if assertion.object_entity_id else "literal"
                ),
                "literal_value": assertion.literal_value or "",
                "evidence_chunk_id": assertion.evidence_chunk_id,
                "evidence_char_start": assertion.evidence_char_start,
                "evidence_char_end": assertion.evidence_char_end,
                "extractor_version": assertion.extractor_version,
                "schema_version": assertion.schema_version,
                "confidence": assertion.confidence,
            }
            assertion_rows.append(
                _node_row(
                    assertion.assertion_id,
                    assertion_identity,
                    create_extra={"accepted": assertion.accepted},
                    replay_extra={"accepted": assertion.accepted},
                )
            )

    finding_rows: list[dict[str, Any]] = []
    for finding in plan.governance_findings:
        finding_identifier = (
            "ingestion-finding:"
            f"{_fingerprint([snapshot.snapshot_id, finding])}"
        )
        properties = {
            "finding_id": finding_identifier,
            "snapshot_id": snapshot.snapshot_id,
            **asdict(finding),
        }
        finding_rows.append(_node_row(finding_identifier, properties))

    return _Payload(
        job=job,
        profile=profile,
        policy=policy,
        document=document_row,
        version=version_row,
        snapshot=snapshot_row,
        chunks=_unique_rows(chunk_rows, kind="Chunk"),
        embeddings=_unique_rows(embedding_rows, kind="ChunkEmbedding"),
        entities=_unique_rows(entity_rows, kind="Entity"),
        mentions=_unique_rows(mention_rows, kind="EntityMention"),
        assertions=_unique_rows(assertion_rows, kind="Assertion"),
        entity_memberships=tuple(
            {
                "identifier": identifier,
                "canonical_name": values["canonical_name"],
                "aliases": values["aliases"],
            }
            for identifier, values in sorted(
                {
                    item["identifier"]: item for item in membership_rows
                }.items()
            )
        ),
        findings=_unique_rows(finding_rows, kind="GraphGovernanceFinding"),
    )


def _result_value(record: Any, key: str) -> Any:
    try:
        return record[key]
    except (KeyError, TypeError):
        data = record.data() if hasattr(record, "data") else dict(record)
        return data[key]


def _merge_rows(
    tx: Any,
    *,
    label: str,
    id_property: str,
    rows: tuple[dict[str, Any], ...],
) -> None:
    if not rows:
        return
    record = tx.run(
        f"""
        /* bulk-initial-load:merge:{label} */
        UNWIND $rows AS row
        MERGE (node:{label} {{{id_property}: row.identifier}})
        ON CREATE SET node = row.properties
        WITH node, row,
             all(
                 key IN keys(row.immutable)
                 WHERE node[key] = row.immutable[key]
             ) AND all(
                 key IN row.absent
                 WHERE node[key] IS NULL
             ) AS compatible
        RETURN count(node) AS matched,
               sum(CASE WHEN compatible THEN 1 ELSE 0 END) AS compatible
        """,
        rows=list(rows),
    ).single()
    if (
        record is None
        or int(_result_value(record, "matched")) != len(rows)
        or int(_result_value(record, "compatible")) != len(rows)
    ):
        raise IngestionConflict(f"immutable {label} conflicts with stable ID")


def _verify_rows(
    tx: Any,
    *,
    label: str,
    id_property: str,
    rows: tuple[dict[str, Any], ...],
) -> None:
    if not rows:
        return
    record = tx.run(
        f"""
        /* bulk-initial-load:verify:{label} */
        UNWIND $rows AS row
        MATCH (node:{label} {{{id_property}: row.identifier}})
        WITH node, row,
             all(
                 key IN keys(row.replay)
                 WHERE node[key] = row.replay[key]
             ) AND all(
                 key IN row.replay_absent
                 WHERE node[key] IS NULL
             ) AS compatible
        RETURN count(node) AS matched,
               sum(CASE WHEN compatible THEN 1 ELSE 0 END) AS compatible
        """,
        rows=list(rows),
    ).single()
    if (
        record is None
        or int(_result_value(record, "matched")) != len(rows)
        or int(_result_value(record, "compatible")) != len(rows)
    ):
        raise IngestionConflict(f"replayed {label} state is incomplete or conflicting")


class Neo4jBulkInitialLoader:
    """Atomically bootstrap governed plans using bounded batch queries."""

    def __init__(
        self,
        driver: Any,
        database: str = "neo4j",
        *,
        clock: Clock | None = None,
        failpoint: Failpoint | None = None,
        transaction_timeout_seconds: float = (
            DEFAULT_INITIAL_LOAD_TRANSACTION_TIMEOUT_SECONDS
        ),
    ) -> None:
        if driver is None:
            raise ValueError("driver must not be None")
        if not isinstance(database, str) or not database.strip():
            raise ValueError("database must not be empty")
        if any(character in database for character in ("\x00", "\r", "\n")):
            raise ValueError("database contains a forbidden control character")
        if (
            isinstance(transaction_timeout_seconds, bool)
            or not isinstance(transaction_timeout_seconds, (int, float))
            or not math.isfinite(float(transaction_timeout_seconds))
            or not 0 < float(transaction_timeout_seconds) <= 300
        ):
            raise ValueError(
                "transaction_timeout_seconds must be a finite number "
                "between 0 and 300"
            )
        self.driver = driver
        self.database = database.strip()
        self.clock = clock or SystemClock()
        self.failpoint = failpoint or _noop_failpoint
        self.transaction_timeout_seconds = float(transaction_timeout_seconds)
        self._transaction_work = unit_of_work(
            metadata={
                "component": "graphrag-bulk-initial-load",
                "operation": "document-version",
            },
            timeout=self.transaction_timeout_seconds,
        )(self._ingest_tx)

    def ingest(self, plan: IngestionPlan) -> InitialLoadResult:
        """Load one plan in one transaction, or verify an exact replay."""

        now = self.clock.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("bulk initial-load clock must be timezone-aware")
        payload = _build_payload(plan, now)
        with self.driver.session(database=self.database) as session:
            return session.execute_write(
                self._transaction_work,
                plan,
                payload,
                now,
                self.failpoint,
            )

    @classmethod
    def _ingest_tx(
        cls,
        tx: Any,
        plan: IngestionPlan,
        payload: _Payload,
        now: datetime,
        failpoint: Failpoint,
    ) -> InitialLoadResult:
        state = tx.run(
            """
            /* bulk-initial-load:lock-state */
            MERGE (state:TenantCorpusState {tenant_id: $tenant_id})
            ON CREATE SET state.corpus_revision = 0,
                          state.created_at = $now,
                          state.lifecycle_mode = 'OFFLINE_INITIAL_LOAD'
            SET state.__corpus_write_lock = randomUUID()
            WITH state
            REMOVE state.__corpus_write_lock
            WITH state
            OPTIONAL MATCH (document:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            OPTIONAL MATCH (tombstone:DocumentTombstone {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            OPTIONAL MATCH (document)-[:ACTIVE_SNAPSHOT]->(
                active_snapshot:KnowledgeSnapshot
            )
            OPTIONAL MATCH (document)-[:ACTIVE_VERSION]->(
                active_version:DocumentVersion
            )
            RETURN state.lifecycle_mode AS lifecycle_mode,
                   state.corpus_revision AS corpus_revision,
                   document.document_id AS existing_document_id,
                   coalesce(document.generation, tombstone.generation, 0)
                       AS source_generation,
                   document.access_policy_id AS access_policy_id,
                   document.access_policy_version AS access_policy_version,
                   document.access_groups AS access_groups,
                   collect(DISTINCT active_snapshot.snapshot_id)
                       AS active_snapshot_ids,
                   collect(DISTINCT active_version.version_id)
                       AS active_version_ids,
                   collect(DISTINCT active_version.version_number)
                       AS active_version_numbers
            """,
            tenant_id=plan.tenant_id,
            document_id=plan.document_id,
            now=now,
        ).single()
        if state is None:
            raise IngestionConflict("tenant corpus state could not be locked")
        if _result_value(state, "lifecycle_mode") != "OFFLINE_INITIAL_LOAD":
            raise IngestionConflict(
                "bulk initial load is disabled after managed ingestion begins"
            )
        if int(_result_value(state, "source_generation")) != plan.source_generation:
            raise IngestionConflict(
                "document generation changed; bulk load cannot resurrect deletion"
            )
        active_snapshots = tuple(_result_value(state, "active_snapshot_ids"))
        active_versions = tuple(_result_value(state, "active_version_ids"))
        active_version_numbers = tuple(
            int(value) for value in _result_value(state, "active_version_numbers")
        )
        if len(active_snapshots) > 1 or len(active_versions) > 1:
            raise IngestionConflict("document has multiple active graph pointers")
        current_snapshot = active_snapshots[0] if active_snapshots else None
        current_version = active_versions[0] if active_versions else None
        if (current_snapshot is None) != (current_version is None):
            raise IngestionConflict(
                "active snapshot/version pointers are inconsistent"
            )

        cls._merge_and_label_job(tx, payload.job)
        if current_snapshot == plan.snapshot.snapshot_id:
            if current_version != plan.version_id:
                raise IngestionConflict(
                    "active snapshot points at a different active version"
                )
            cls._verify_exact_replay(tx, plan, payload)
            revision = int(_result_value(state, "corpus_revision"))
            return cls._result(plan, payload, "UNCHANGED", revision)
        if current_snapshot != plan.expected_active_snapshot_id:
            raise IngestionConflict(
                "active snapshot differs from expected_active_snapshot_id"
            )
        if active_version_numbers and (
            plan.bundles[0].version.version_number < active_version_numbers[0]
        ):
            raise IngestionConflict("bulk initial load cannot roll back a version")
        cls._validate_document_access(state, plan)

        cls._merge_plan_nodes(tx, payload)
        cls._validate_and_publish_access(tx, plan, payload)
        cls._link_graph(tx, plan, payload)
        cls._verify_loaded_graph(tx, plan, payload)
        failpoint(
            Checkpoint.BEFORE_PUBLISH,
            {
                "document_id": plan.document_id,
                "job_id": payload.job["identifier"],
                "snapshot_id": plan.snapshot.snapshot_id,
                "tenant_id": plan.tenant_id,
            },
        )
        cls._publish_active_snapshot(
            tx,
            plan,
            current_snapshot=current_snapshot,
            now=now,
        )
        revision = cls._advance_revision(tx, plan.tenant_id, now)
        outcome = "CREATED" if current_snapshot is None else (
            "UPDATED" if current_version != plan.version_id else "REPROCESSED"
        )
        tx.run(
            """
            MATCH (job:IngestionJob:InitialLoadJob {job_id: $job_id})
            SET job.status = 'SUCCEEDED',
                job.phase = 'COMPLETE',
                job.outcome = $outcome,
                job.completed_tasks = job.expected_tasks,
                job.corpus_revision = $revision,
                job.completed_at = $now,
                job.updated_at = $now
            """,
            job_id=payload.job["identifier"],
            outcome=outcome,
            revision=revision,
            now=now,
        ).consume()
        return cls._result(plan, payload, outcome, revision)

    @staticmethod
    def _result(
        plan: IngestionPlan,
        payload: _Payload,
        outcome: str,
        revision: int,
    ) -> InitialLoadResult:
        return InitialLoadResult(
            job_id=payload.job["identifier"],
            tenant_id=plan.tenant_id,
            document_id=plan.document_id,
            version_id=plan.version_id,
            snapshot_id=plan.snapshot.snapshot_id,
            outcome=outcome,
            corpus_revision=revision,
            chunk_count=len(payload.chunks),
            embedding_count=len(payload.embeddings),
        )

    @staticmethod
    def _merge_and_label_job(tx: Any, job: dict[str, Any]) -> None:
        _merge_rows(
            tx,
            label="IngestionJob",
            id_property="job_id",
            rows=(job,),
        )
        tx.run(
            """
            MATCH (job:IngestionJob {job_id: $job_id})
            SET job:InitialLoadJob
            """,
            job_id=job["identifier"],
        ).consume()

    @staticmethod
    def _validate_document_access(state: Any, plan: IngestionPlan) -> None:
        current = _result_value(state, "access_policy_version")
        if current is None:
            return
        document = plan.bundles[0].document
        current_version = int(current)
        if document.access_policy_version < current_version:
            raise IngestionConflict("access policy version is stale")
        if document.access_policy_version == current_version and (
            document.access_policy_id != _result_value(state, "access_policy_id")
            or sorted(document.access_groups)
            != sorted(_result_value(state, "access_groups") or ())
        ):
            raise IngestionConflict(
                "access policy changed without a new policy version"
            )

    @staticmethod
    def _merge_plan_nodes(tx: Any, payload: _Payload) -> None:
        for name in ("profile", "policy", "document", "version", "snapshot"):
            label, id_property = _NODE_SHAPES[name]
            _merge_rows(
                tx,
                label=label,
                id_property=id_property,
                rows=(getattr(payload, name),),
            )
        for name in (
            "chunks",
            "embeddings",
            "entities",
            "mentions",
            "assertions",
            "findings",
        ):
            label, id_property = _NODE_SHAPES[name]
            _merge_rows(
                tx,
                label=label,
                id_property=id_property,
                rows=getattr(payload, name),
            )

    @staticmethod
    def _validate_and_publish_access(
        tx: Any,
        plan: IngestionPlan,
        payload: _Payload,
    ) -> None:
        document = plan.bundles[0].document
        record = tx.run(
            """
            /* bulk-initial-load:publish-access */
            UNWIND $rows AS row
            MATCH (chunk:Chunk {chunk_id: row.identifier})
            WITH chunk, row,
                 chunk.access_policy_version IS NULL
                 OR chunk.access_policy_version < row.replay.access_policy_version
                 OR (
                     chunk.access_policy_version = row.replay.access_policy_version
                     AND chunk.access_policy_id = row.replay.access_policy_id
                     AND chunk.access_groups = row.replay.access_groups
                 ) AS compatible
            SET chunk.access_policy_id = CASE
                    WHEN compatible THEN row.replay.access_policy_id
                    ELSE chunk.access_policy_id
                END,
                chunk.access_policy_version = CASE
                    WHEN compatible THEN row.replay.access_policy_version
                    ELSE chunk.access_policy_version
                END,
                chunk.access_groups = CASE
                    WHEN compatible THEN row.replay.access_groups
                    ELSE chunk.access_groups
                END,
                chunk.retrieval_scope = CASE
                    WHEN compatible THEN row.replay.retrieval_scope
                    ELSE chunk.retrieval_scope
                END
            RETURN count(chunk) AS matched,
                   sum(CASE WHEN compatible THEN 1 ELSE 0 END) AS compatible
            """,
            rows=list(payload.chunks),
        ).single()
        if (
            record is None
            or int(_result_value(record, "matched")) != len(payload.chunks)
            or int(_result_value(record, "compatible")) != len(payload.chunks)
        ):
            raise IngestionConflict("Chunk access state conflicts with publication")
        tx.run(
            """
            MATCH (document:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            SET document.title = $title,
                document.access_policy_id = $policy_id,
                document.access_policy_version = $policy_version,
                document.access_groups = $groups,
                document.generation = $generation
            """,
            tenant_id=plan.tenant_id,
            document_id=plan.document_id,
            title=document.title,
            policy_id=document.access_policy_id,
            policy_version=document.access_policy_version,
            groups=sorted(document.access_groups),
            generation=plan.source_generation,
        ).consume()
        if payload.entities:
            tx.run(
                """
                UNWIND $rows AS row
                MATCH (entity:Entity {entity_id: row.identifier})
                SET entity.canonical_name = coalesce(
                        entity.canonical_name,
                        row.properties.canonical_name
                    ),
                    entity.aliases = coalesce(
                        entity.aliases,
                        row.properties.aliases
                    ),
                    entity.governance_status = coalesce(
                        entity.governance_status,
                        'ACCEPTED'
                    ),
                    entity.governance_policy_id = coalesce(
                        entity.governance_policy_id,
                        $policy_id
                    )
                """,
                rows=list(payload.entities),
                policy_id=plan.governance_policy.policy_id,
            ).consume()

    @classmethod
    def _link_graph(
        cls,
        tx: Any,
        plan: IngestionPlan,
        payload: _Payload,
    ) -> None:
        core = tx.run(
            """
            MATCH (document:Document {document_id: $document_id})
            MATCH (version:DocumentVersion {version_id: $version_id})
            MATCH (snapshot:KnowledgeSnapshot {snapshot_id: $snapshot_id})
            MATCH (profile:GraphPipelineProfile {profile_id: $profile_id})
            MATCH (policy:GraphGovernancePolicy {policy_id: $policy_id})
            MATCH (job:IngestionJob:InitialLoadJob {job_id: $job_id})
            WHERE document.tenant_id = $tenant_id
              AND version.tenant_id = $tenant_id
              AND snapshot.tenant_id = $tenant_id
              AND version.document_id = document.document_id
              AND snapshot.document_id = document.document_id
              AND snapshot.version_id = version.version_id
            MERGE (document)-[:HAS_VERSION]->(version)
            MERGE (snapshot)-[:OF_VERSION]->(version)
            MERGE (snapshot)-[:USES_PROFILE]->(profile)
            MERGE (snapshot)-[:USES_GOVERNANCE_POLICY]->(policy)
            MERGE (job)-[:BUILDS]->(snapshot)
            RETURN count(snapshot) AS linked
            """,
            tenant_id=plan.tenant_id,
            document_id=plan.document_id,
            version_id=plan.version_id,
            snapshot_id=plan.snapshot.snapshot_id,
            profile_id=plan.profile.profile_id,
            policy_id=plan.governance_policy.policy_id,
            job_id=payload.job["identifier"],
        ).single()
        if core is None or int(_result_value(core, "linked")) != 1:
            raise IngestionConflict("document/version/snapshot provenance is invalid")

        cls._link_chunks(tx, plan, payload)
        cls._link_embeddings(tx, plan, payload)
        cls._link_entities(tx, plan, payload)
        cls._link_mentions(tx, plan, payload)
        cls._link_assertions(tx, plan, payload)
        cls._link_findings(tx, plan, payload)

    @staticmethod
    def _link_chunks(tx: Any, plan: IngestionPlan, payload: _Payload) -> None:
        record = tx.run(
            """
            UNWIND $rows AS row
            MATCH (version:DocumentVersion {version_id: $version_id})
            MATCH (snapshot:KnowledgeSnapshot {snapshot_id: $snapshot_id})
            MATCH (chunk:Chunk {chunk_id: row.identifier})
            WHERE chunk.tenant_id = $tenant_id
              AND chunk.version_id = version.version_id
              AND snapshot.version_id = version.version_id
            MERGE (version)-[:HAS_CHUNK]->(chunk)
            MERGE (snapshot)-[:INCLUDES_CHUNK]->(chunk)
            RETURN count(chunk) AS linked
            """,
            rows=list(payload.chunks),
            tenant_id=plan.tenant_id,
            version_id=plan.version_id,
            snapshot_id=plan.snapshot.snapshot_id,
        ).single()
        if record is None or int(_result_value(record, "linked")) != len(
            payload.chunks
        ):
            raise IngestionConflict("Chunk provenance or membership is incomplete")

    @staticmethod
    def _link_embeddings(tx: Any, plan: IngestionPlan, payload: _Payload) -> None:
        if not payload.embeddings:
            return
        record = tx.run(
            """
            UNWIND $rows AS row
            MATCH (chunk:Chunk {chunk_id: row.properties.chunk_id})
            MATCH (embedding:ChunkEmbedding {embedding_id: row.identifier})
            WHERE chunk.tenant_id = $tenant_id
              AND embedding.tenant_id = $tenant_id
              AND embedding.chunk_id = chunk.chunk_id
            MERGE (chunk)-[:HAS_EMBEDDING]->(embedding)
            RETURN count(embedding) AS linked
            """,
            rows=list(payload.embeddings),
            tenant_id=plan.tenant_id,
        ).single()
        if record is None or int(_result_value(record, "linked")) != len(
            payload.embeddings
        ):
            raise IngestionConflict("ChunkEmbedding provenance is incomplete")

    @staticmethod
    def _link_entities(tx: Any, plan: IngestionPlan, payload: _Payload) -> None:
        if not payload.entity_memberships:
            return
        record = tx.run(
            """
            UNWIND $rows AS row
            MATCH (snapshot:KnowledgeSnapshot {snapshot_id: $snapshot_id})
            MATCH (entity:Entity {entity_id: row.identifier})
            WHERE entity.tenant_id = $tenant_id
            MERGE (snapshot)-[membership:INCLUDES_ENTITY]->(entity)
            ON CREATE SET membership.canonical_name = row.canonical_name,
                          membership.aliases = row.aliases
            WITH entity, membership, row
            RETURN count(entity) AS linked,
                   sum(CASE WHEN
                       membership.canonical_name = row.canonical_name
                       AND membership.aliases = row.aliases
                   THEN 1 ELSE 0 END) AS compatible
            """,
            rows=list(payload.entity_memberships),
            tenant_id=plan.tenant_id,
            snapshot_id=plan.snapshot.snapshot_id,
        ).single()
        expected = len(payload.entity_memberships)
        if (
            record is None
            or int(_result_value(record, "linked")) != expected
            or int(_result_value(record, "compatible")) != expected
        ):
            raise IngestionConflict("Entity snapshot membership conflicts")

    @staticmethod
    def _link_mentions(tx: Any, plan: IngestionPlan, payload: _Payload) -> None:
        if not payload.mentions:
            return
        record = tx.run(
            """
            UNWIND $rows AS row
            MATCH (snapshot:KnowledgeSnapshot {snapshot_id: $snapshot_id})
            MATCH (mention:EntityMention {mention_id: row.identifier})
            MATCH (chunk:Chunk {chunk_id: row.properties.chunk_id})
            MATCH (entity:Entity {entity_id: row.properties.entity_id})
            WHERE mention.tenant_id = $tenant_id
              AND chunk.tenant_id = $tenant_id
              AND entity.tenant_id = $tenant_id
              AND mention.chunk_id = chunk.chunk_id
              AND mention.entity_id = entity.entity_id
            MERGE (mention)-[:IN_CHUNK]->(chunk)
            MERGE (mention)-[:REFERS_TO]->(entity)
            MERGE (snapshot)-[membership:INCLUDES_MENTION]->(mention)
            ON CREATE SET membership.entity_id = row.properties.entity_id,
                          membership.confidence = row.properties.confidence
            WITH mention, membership, row
            RETURN count(mention) AS linked,
                   sum(CASE WHEN
                       membership.entity_id = row.properties.entity_id
                       AND membership.confidence = row.properties.confidence
                   THEN 1 ELSE 0 END) AS compatible
            """,
            rows=list(payload.mentions),
            tenant_id=plan.tenant_id,
            snapshot_id=plan.snapshot.snapshot_id,
        ).single()
        expected = len(payload.mentions)
        if (
            record is None
            or int(_result_value(record, "linked")) != expected
            or int(_result_value(record, "compatible")) != expected
        ):
            raise IngestionConflict("EntityMention provenance conflicts")

    @staticmethod
    def _link_assertions(tx: Any, plan: IngestionPlan, payload: _Payload) -> None:
        if not payload.assertions:
            return
        common = tx.run(
            """
            UNWIND $rows AS row
            MATCH (snapshot:KnowledgeSnapshot {snapshot_id: $snapshot_id})
            MATCH (assertion:Assertion {assertion_id: row.identifier})
            MATCH (subject:Entity {entity_id: row.properties.subject_entity_id})
            MATCH (chunk:Chunk {chunk_id: row.properties.evidence_chunk_id})
            WHERE assertion.tenant_id = $tenant_id
              AND subject.tenant_id = $tenant_id
              AND chunk.tenant_id = $tenant_id
              AND assertion.subject_entity_id = subject.entity_id
              AND assertion.evidence_chunk_id = chunk.chunk_id
            MERGE (assertion)-[:SUBJECT]->(subject)
            MERGE (assertion)-[:EVIDENCED_BY]->(chunk)
            MERGE (snapshot)-[membership:INCLUDES_ASSERTION]->(assertion)
            ON CREATE SET membership.confidence = row.properties.confidence,
                          membership.accepted = row.properties.accepted
            WITH assertion, membership, row
            RETURN count(assertion) AS linked,
                   sum(CASE WHEN
                       membership.confidence = row.properties.confidence
                       AND membership.accepted = row.properties.accepted
                   THEN 1 ELSE 0 END) AS compatible
            """,
            rows=list(payload.assertions),
            tenant_id=plan.tenant_id,
            snapshot_id=plan.snapshot.snapshot_id,
        ).single()
        expected = len(payload.assertions)
        if (
            common is None
            or int(_result_value(common, "linked")) != expected
            or int(_result_value(common, "compatible")) != expected
        ):
            raise IngestionConflict("Assertion provenance conflicts")
        entity_objects = tuple(
            row
            for row in payload.assertions
            if row["properties"].get("object_entity_id") is not None
        )
        if entity_objects:
            linked = tx.run(
                """
                UNWIND $rows AS row
                MATCH (assertion:Assertion {assertion_id: row.identifier})
                MATCH (object:Entity {entity_id: row.properties.object_entity_id})
                WHERE assertion.tenant_id = $tenant_id
                  AND object.tenant_id = $tenant_id
                MERGE (assertion)-[:OBJECT]->(object)
                RETURN count(assertion) AS linked
                """,
                rows=list(entity_objects),
                tenant_id=plan.tenant_id,
            ).single()
            if linked is None or int(_result_value(linked, "linked")) != len(
                entity_objects
            ):
                raise IngestionConflict("Assertion object provenance is incomplete")

    @staticmethod
    def _link_findings(tx: Any, plan: IngestionPlan, payload: _Payload) -> None:
        if not payload.findings:
            return
        record = tx.run(
            """
            UNWIND $rows AS row
            MATCH (snapshot:KnowledgeSnapshot {snapshot_id: $snapshot_id})
            MATCH (finding:GraphGovernanceFinding {finding_id: row.identifier})
            MERGE (snapshot)-[:HAS_GOVERNANCE_FINDING]->(finding)
            RETURN count(finding) AS linked
            """,
            rows=list(payload.findings),
            snapshot_id=plan.snapshot.snapshot_id,
        ).single()
        if record is None or int(_result_value(record, "linked")) != len(
            payload.findings
        ):
            raise IngestionConflict("governance finding membership is incomplete")

    @classmethod
    def _verify_exact_replay(
        cls,
        tx: Any,
        plan: IngestionPlan,
        payload: _Payload,
    ) -> None:
        for name in ("job", "profile", "policy", "document", "version", "snapshot"):
            label, id_property = _NODE_SHAPES[name]
            _verify_rows(
                tx,
                label=label,
                id_property=id_property,
                rows=(getattr(payload, name),),
            )
        for name in (
            "chunks",
            "embeddings",
            "entities",
            "mentions",
            "assertions",
            "findings",
        ):
            label, id_property = _NODE_SHAPES[name]
            _verify_rows(
                tx,
                label=label,
                id_property=id_property,
                rows=getattr(payload, name),
            )
        terminal_job = tx.run(
            """
            MATCH (job:IngestionJob:InitialLoadJob {job_id: $job_id})
            WHERE job.status = 'SUCCEEDED'
              AND job.phase = 'COMPLETE'
              AND job.outcome IN ['CREATED', 'UPDATED', 'REPROCESSED']
              AND job.completed_tasks = job.expected_tasks
              AND job.corpus_revision IS NOT NULL
              AND job.completed_at IS NOT NULL
            RETURN count(job) AS count
            """,
            job_id=payload.job["identifier"],
        ).single()
        if terminal_job is None or int(_result_value(terminal_job, "count")) != 1:
            raise IngestionConflict(
                "replayed initial-load job is not durably complete"
            )
        cls._verify_loaded_graph(tx, plan, payload)

    @classmethod
    def _verify_loaded_graph(
        cls,
        tx: Any,
        plan: IngestionPlan,
        payload: _Payload,
    ) -> None:
        manifest = Neo4jIngestionService._snapshot_manifest_from_graph_tx(
            tx,
            plan.snapshot.snapshot_id,
        )
        if _fingerprint(manifest) != plan.snapshot.manifest_hash:
            raise IngestionConflict("bulk-loaded snapshot manifest conflicts")
        if len(manifest) != len(payload.chunks):
            raise IngestionConflict("bulk-loaded snapshot Chunk count is incomplete")

        counts = tx.run(
            """
            MATCH (document:Document {document_id: $document_id})
                  -[:HAS_VERSION]->(version:DocumentVersion {
                      version_id: $version_id
                  })
            MATCH (snapshot:KnowledgeSnapshot {snapshot_id: $snapshot_id})
                  -[:OF_VERSION]->(version)
            MATCH (snapshot)-[:USES_PROFILE]->(:GraphPipelineProfile {
                profile_id: $profile_id
            })
            MATCH (snapshot)-[:USES_GOVERNANCE_POLICY]->(
                :GraphGovernancePolicy {policy_id: $policy_id}
            )
            MATCH (:IngestionJob:InitialLoadJob {job_id: $job_id})
                  -[:BUILDS]->(snapshot)
            OPTIONAL MATCH (version)-[:HAS_CHUNK]->(chunk:Chunk)
            WHERE chunk.chunk_id IN $chunk_ids
            OPTIONAL MATCH (snapshot)-[:INCLUDES_CHUNK]->(
                snapshot_chunk:Chunk
            )
            WHERE snapshot_chunk.chunk_id IN $chunk_ids
            OPTIONAL MATCH (chunk)-[:HAS_EMBEDDING]->(
                embedding:ChunkEmbedding
            )
            WHERE embedding.embedding_id IN $embedding_ids
            RETURN count(DISTINCT chunk) AS version_chunks,
                   count(DISTINCT snapshot_chunk) AS snapshot_chunks,
                   count(DISTINCT embedding) AS embeddings
            """,
            document_id=plan.document_id,
            version_id=plan.version_id,
            snapshot_id=plan.snapshot.snapshot_id,
            profile_id=plan.profile.profile_id,
            policy_id=plan.governance_policy.policy_id,
            job_id=payload.job["identifier"],
            chunk_ids=[row["identifier"] for row in payload.chunks],
            embedding_ids=[row["identifier"] for row in payload.embeddings],
        ).single()
        if counts is None or (
            int(_result_value(counts, "version_chunks")) != len(payload.chunks)
            or int(_result_value(counts, "snapshot_chunks"))
            != len(payload.chunks)
            or int(_result_value(counts, "embeddings"))
            != len(payload.embeddings)
        ):
            raise IngestionConflict("bulk-loaded core provenance is incomplete")

        cls._verify_mention_paths(tx, plan, payload)
        cls._verify_assertion_paths(tx, plan, payload)

    @staticmethod
    def _verify_mention_paths(
        tx: Any,
        plan: IngestionPlan,
        payload: _Payload,
    ) -> None:
        if not payload.mentions:
            return
        record = tx.run(
            """
            UNWIND $rows AS row
            MATCH (snapshot:KnowledgeSnapshot {snapshot_id: $snapshot_id})
                  -[:INCLUDES_MENTION]->(
                      mention:EntityMention {mention_id: row.identifier}
                  )-[:IN_CHUNK]->(
                      chunk:Chunk {chunk_id: row.properties.chunk_id}
                  )
            MATCH (mention)-[:REFERS_TO]->(
                entity:Entity {entity_id: row.properties.entity_id}
            )
            MATCH (snapshot)-[:INCLUDES_ENTITY]->(entity)
            WHERE mention.tenant_id = $tenant_id
              AND chunk.tenant_id = $tenant_id
              AND entity.tenant_id = $tenant_id
            RETURN count(mention) AS linked
            """,
            rows=list(payload.mentions),
            snapshot_id=plan.snapshot.snapshot_id,
            tenant_id=plan.tenant_id,
        ).single()
        if record is None or int(_result_value(record, "linked")) != len(
            payload.mentions
        ):
            raise IngestionConflict("bulk-loaded mention provenance is incomplete")

    @staticmethod
    def _verify_assertion_paths(
        tx: Any,
        plan: IngestionPlan,
        payload: _Payload,
    ) -> None:
        if not payload.assertions:
            return
        record = tx.run(
            """
            UNWIND $rows AS row
            MATCH (snapshot:KnowledgeSnapshot {snapshot_id: $snapshot_id})
                  -[:INCLUDES_ASSERTION]->(
                      assertion:Assertion {assertion_id: row.identifier}
                  )-[:SUBJECT]->(:Entity)
            MATCH (assertion)-[:EVIDENCED_BY]->(
                :Chunk {chunk_id: row.properties.evidence_chunk_id}
            )
            OPTIONAL MATCH (assertion)-[:OBJECT]->(object:Entity)
            WITH assertion, object, row
            WHERE row.properties.object_entity_id IS NULL
               OR object.entity_id = row.properties.object_entity_id
            RETURN count(assertion) AS linked
            """,
            rows=list(payload.assertions),
            snapshot_id=plan.snapshot.snapshot_id,
        ).single()
        if record is None or int(_result_value(record, "linked")) != len(
            payload.assertions
        ):
            raise IngestionConflict("bulk-loaded assertion provenance is incomplete")

    @staticmethod
    def _publish_active_snapshot(
        tx: Any,
        plan: IngestionPlan,
        *,
        current_snapshot: str | None,
        now: datetime,
    ) -> None:
        if current_snapshot is not None:
            tx.run(
                """
                MATCH (:KnowledgeSnapshot {snapshot_id: $snapshot_id})
                      -[:INCLUDES_CHUNK]->(chunk:Chunk)
                WHERE NOT chunk.chunk_id IN $new_chunk_ids
                REMOVE chunk.retrieval_scope
                """,
                snapshot_id=current_snapshot,
                new_chunk_ids=sorted(
                    bundle.chunk.chunk_id for bundle in plan.bundles
                ),
            ).consume()
        record = tx.run(
            """
            MATCH (document:Document {
                document_id: $document_id,
                tenant_id: $tenant_id
            })
            MATCH (snapshot:KnowledgeSnapshot {snapshot_id: $snapshot_id})
            MATCH (version:DocumentVersion {version_id: $version_id})
            OPTIONAL MATCH (document)-[old_snapshot:ACTIVE_SNAPSHOT]->(
                :KnowledgeSnapshot
            )
            DELETE old_snapshot
            WITH DISTINCT document, snapshot, version
            OPTIONAL MATCH (document)-[old_version:ACTIVE_VERSION]->(
                :DocumentVersion
            )
            DELETE old_version
            MERGE (document)-[:ACTIVE_SNAPSHOT]->(snapshot)
            MERGE (document)-[:ACTIVE_VERSION]->(version)
            SET snapshot.build_state = 'PUBLISHED',
                snapshot.actual_chunk_count = $chunk_count,
                snapshot.verified_at = coalesce(snapshot.verified_at, $now),
                snapshot.published_at = coalesce(snapshot.published_at, $now)
            RETURN count(snapshot) AS published
            """,
            document_id=plan.document_id,
            tenant_id=plan.tenant_id,
            snapshot_id=plan.snapshot.snapshot_id,
            version_id=plan.version_id,
            chunk_count=len(plan.bundles),
            now=now,
        ).single()
        if record is None or int(_result_value(record, "published")) != 1:
            raise IngestionConflict("bulk snapshot publication failed")
        if current_snapshot is not None:
            tx.run(
                """
                MATCH (snapshot:KnowledgeSnapshot {snapshot_id: $snapshot_id})
                WHERE NOT (:Document)-[:ACTIVE_SNAPSHOT]->(snapshot)
                SET snapshot.build_state = 'RETIRED',
                    snapshot.retired_at = coalesce(snapshot.retired_at, $now)
                """,
                snapshot_id=current_snapshot,
                now=now,
            ).consume()

    @staticmethod
    def _advance_revision(tx: Any, tenant_id: str, now: datetime) -> int:
        record = tx.run(
            """
            MATCH (state:TenantCorpusState {tenant_id: $tenant_id})
            SET state.corpus_revision = state.corpus_revision + 1,
                state.updated_at = $now
            WITH state
            OPTIONAL MATCH (state)-[pointer:ACTIVE_EMBEDDING_INDEX]->(
                generation:EmbeddingIndexGeneration
            )
            DELETE pointer
            SET generation.state = 'STALE',
                generation.stale_at = $now,
                generation.updated_at = $now
            RETURN state.corpus_revision AS revision
            """,
            tenant_id=tenant_id,
            now=now,
        ).single()
        if record is None:
            raise IngestionConflict("tenant corpus revision could not advance")
        return int(_result_value(record, "revision"))


def utc_now() -> datetime:
    """Small public clock helper for external bootstrap orchestration."""

    return datetime.now(UTC)
