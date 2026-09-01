"""Cache-before-compute orchestration for durable incremental ingestion."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from graphrag_prod.domain.ids import (
    canonicalize_uri,
    chunk_embedding_id,
    chunk_id,
    content_checksum,
    derivation_artifact_id,
    document_id,
    embedding_space_id,
    ingestion_job_id,
    ingestion_task_id,
    knowledge_snapshot_id,
    version_id,
)
from graphrag_prod.domain.models import (
    Assertion,
    Chunk,
    ChunkEmbedding,
    Document,
    DocumentVersion,
    Entity,
    EntityMention,
    GraphPipelineProfile,
)
from graphrag_prod.graph.provenance import ProvenanceBundle
from graphrag_prod.graph.governance import GraphGovernancePolicy

from .artifacts import (
    decode_embedding,
    decode_extraction,
    encode_embedding,
    encode_extraction,
)
from .embedding import Neo4jEmbeddingIndexManager
from .models import (
    IngestionPlan,
    IngestionResult,
    JobStatus,
    _fingerprint,
    chunk_artifact_input_hash,
)
from .service import IngestionConflict, Neo4jIngestionService


def _required(value: str, name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


@dataclass(frozen=True, slots=True)
class ChunkSeed:
    ordinal: int
    text: str
    char_start: int
    char_end: int
    page_number: int | None = None
    section: str | None = None

    def __post_init__(self) -> None:
        if self.ordinal < 0:
            raise ValueError("chunk seed ordinal must not be negative")
        if self.text == "":
            raise ValueError("chunk seed text must not be empty")
        if self.char_start < 0 or self.char_end <= self.char_start:
            raise ValueError("chunk seed character range is invalid")
        if len(self.text) != self.char_end - self.char_start:
            raise ValueError("chunk seed text length must match its range")
        if self.page_number is not None and self.page_number <= 0:
            raise ValueError("chunk seed page_number must be positive")
        if self.section is not None and not self.section.strip():
            raise ValueError("chunk seed section must not be blank")


@dataclass(frozen=True, slots=True)
class EmbeddingProfile:
    provider: str
    model: str
    revision: str
    dimensions: int
    normalization: str

    def __post_init__(self) -> None:
        for name in ("provider", "model", "revision", "normalization"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        if isinstance(self.dimensions, bool) or self.dimensions <= 0:
            raise ValueError("embedding dimensions must be positive")

    @property
    def embedding_space_id(self) -> str:
        return embedding_space_id(
            self.provider,
            self.model,
            self.revision,
            self.dimensions,
            self.normalization,
        )


@dataclass(frozen=True, slots=True)
class ExtractionOutput:
    entities: tuple[Entity, ...]
    mentions: tuple[EntityMention, ...]
    assertions: tuple[Assertion, ...]


class ExtractionProvider(Protocol):
    def __call__(
        self,
        *,
        artifact_id: str,
        input_hash: str,
        chunk: Chunk,
        profile: GraphPipelineProfile,
    ) -> ExtractionOutput: ...


class EmbeddingProvider(Protocol):
    def __call__(
        self,
        *,
        artifact_id: str,
        input_hash: str,
        chunk: Chunk,
        profile: EmbeddingProfile,
    ) -> tuple[float, ...]: ...


@dataclass(frozen=True, slots=True)
class IncrementalIngestionRequest:
    operation_key: str
    tenant_id: str
    canonical_uri: str
    title: str
    source_name: str
    mime_type: str
    language: str
    published_at: datetime | None
    access_policy_id: str
    access_policy_version: int
    access_groups: frozenset[str]
    source_generation: int
    expected_active_snapshot_id: str | None
    chunks: tuple[ChunkSeed, ...]
    profile: GraphPipelineProfile
    governance_policy: GraphGovernancePolicy
    embedding_profile: EmbeddingProfile
    version_number: int
    ingested_at: datetime
    original_checksum: str | None = None
    max_attempts: int = 3

    def __post_init__(self) -> None:
        for name in (
            "operation_key",
            "tenant_id",
            "title",
            "source_name",
            "mime_type",
            "language",
            "access_policy_id",
        ):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        object.__setattr__(self, "canonical_uri", canonicalize_uri(self.canonical_uri))
        groups = frozenset(value.strip() for value in self.access_groups if value.strip())
        if not groups:
            raise ValueError("access_groups require an explicit group")
        object.__setattr__(self, "access_groups", groups)
        if self.access_policy_version <= 0:
            raise ValueError("access_policy_version must be positive")
        if self.source_generation < 0 or self.version_number <= 0:
            raise ValueError("source_generation and version_number are invalid")
        if self.max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if self.governance_policy.policy_id != self.profile.schema_signature:
            raise ValueError(
                "governance policy_id must match the pipeline schema signature"
            )
        _aware(self.ingested_at, "ingested_at")
        if self.published_at is not None:
            _aware(self.published_at, "published_at")
        if not self.chunks:
            raise ValueError("incremental request requires at least one chunk")
        if len({seed.ordinal for seed in self.chunks}) != len(self.chunks):
            raise ValueError("incremental request contains duplicate chunk ordinals")
        if tuple(sorted(seed.ordinal for seed in self.chunks)) != tuple(
            range(len(self.chunks))
        ):
            raise ValueError("chunk seed ordinals must be contiguous from zero")
        self.normalized_text
        if self.original_checksum is not None:
            normalized = self.original_checksum.strip().lower()
            if len(normalized) != 64 or any(
                character not in "0123456789abcdef" for character in normalized
            ):
                raise ValueError("original_checksum must be lowercase SHA-256")
            object.__setattr__(self, "original_checksum", normalized)

    @property
    def normalized_text(self) -> str:
        end = max(seed.char_end for seed in self.chunks)
        characters: list[str | None] = [None] * end
        for seed in self.chunks:
            for offset, character in enumerate(seed.text, start=seed.char_start):
                existing = characters[offset]
                if existing is not None and existing != character:
                    raise ValueError("overlapping chunk seeds contain conflicting text")
                characters[offset] = character
        if any(character is None for character in characters):
            raise ValueError("chunk seeds must cover the normalized source without gaps")
        return "".join(character for character in characters if character is not None)

    @property
    def document_id(self) -> str:
        return document_id(self.tenant_id, self.canonical_uri)

    @property
    def version_id(self) -> str:
        checksum = content_checksum(self.normalized_text)
        return version_id(
            self.document_id,
            checksum,
            self.original_checksum or checksum,
        )

    @property
    def snapshot_id(self) -> str:
        return knowledge_snapshot_id(self.version_id, self.profile.profile_id)

    @property
    def preparation_job_id(self) -> str:
        return ingestion_job_id(self.tenant_id, "PREPARE_UPSERT", self.operation_key)

    @property
    def request_fingerprint(self) -> str:
        return _fingerprint(self)

    def domain_inputs(self) -> tuple[Document, DocumentVersion, tuple[Chunk, ...]]:
        checksum = content_checksum(self.normalized_text)
        document = Document(
            document_id=self.document_id,
            tenant_id=self.tenant_id,
            canonical_uri=self.canonical_uri,
            title=self.title,
            source_name=self.source_name,
            access_policy_id=self.access_policy_id,
            access_policy_version=self.access_policy_version,
            access_groups=self.access_groups,
            created_at=self.ingested_at,
        )
        version = DocumentVersion(
            version_id=self.version_id,
            document_id=self.document_id,
            tenant_id=self.tenant_id,
            checksum=checksum,
            original_checksum=self.original_checksum or checksum,
            normalized_text=self.normalized_text,
            version_number=self.version_number,
            mime_type=self.mime_type,
            language=self.language,
            published_at=self.published_at,
            ingested_at=self.ingested_at,
        )
        chunks = tuple(
            Chunk(
                chunk_id=chunk_id(
                    version.version_id,
                    self.profile.splitter_signature,
                    seed.ordinal,
                    seed.char_start,
                    seed.char_end,
                    content_checksum(seed.text),
                ),
                version_id=version.version_id,
                document_id=document.document_id,
                tenant_id=document.tenant_id,
                access_policy_id=document.access_policy_id,
                access_policy_version=document.access_policy_version,
                access_groups=document.access_groups,
                ordinal=seed.ordinal,
                text=seed.text,
                checksum=content_checksum(seed.text),
                char_start=seed.char_start,
                char_end=seed.char_end,
                page_number=seed.page_number,
                section=seed.section,
                splitter_version=self.profile.splitter_signature,
            )
            for seed in sorted(self.chunks, key=lambda item: item.ordinal)
        )
        return document, version, chunks


class Neo4jIncrementalPipeline:
    """Persist work first, reuse immutable artifacts, then publish one snapshot."""

    def __init__(
        self,
        driver: Any,
        database: str = "neo4j",
        *,
        worker_id: str | None = None,
        clock: Any | None = None,
        lease_seconds: int = 60,
    ) -> None:
        self.service = Neo4jIngestionService(
            driver,
            database,
            worker_id=worker_id,
            clock=clock,
            lease_seconds=lease_seconds,
        )
        self.driver = driver
        self.database = database

    def run(
        self,
        request: IncrementalIngestionRequest,
        extraction_provider: ExtractionProvider,
        embedding_provider: EmbeddingProvider,
    ) -> IngestionResult:
        document, version, chunks = request.domain_inputs()
        now = self.service.clock.now()
        with self.driver.session(database=self.database) as session:
            session.execute_write(self._ensure_preparation_tx, request, chunks, now)
            job = session.execute_write(
                self.service._claim_job_tx,
                request.preparation_job_id,
                now,
            )
        if job.status is JobStatus.FAILED_PERMANENT:
            raise IngestionConflict("provider preparation exhausted its retry budget")
        lease_token = job.lease_token
        if job.status not in {JobStatus.SUCCEEDED, JobStatus.NOOP} and lease_token is None:
            raise RuntimeError("claimed provider preparation has no fencing token")

        current_task_id: str | None = None
        try:
            bundles: list[ProvenanceBundle] = []
            for chunk in chunks:
                current_task_id = ingestion_task_id(
                    request.preparation_job_id,
                    chunk.chunk_id,
                )
                if lease_token is not None:
                    with self.driver.session(database=self.database) as session:
                        session.execute_write(
                            self._start_task_tx,
                            self.service,
                            request.preparation_job_id,
                            current_task_id,
                            lease_token,
                            self.service.clock.now(),
                        )
                input_hash = chunk_artifact_input_hash(chunk)
                extraction_id = derivation_artifact_id(
                    request.tenant_id,
                    "EXTRACTION",
                    input_hash,
                    request.profile.profile_id,
                )
                extraction_payload = self._read_artifact(
                    request.tenant_id,
                    extraction_id,
                    "EXTRACTION",
                    input_hash,
                    request.profile.profile_id,
                )
                if extraction_payload is None:
                    if lease_token is None:
                        raise IngestionConflict(
                            "terminal preparation is missing an extraction artifact"
                        )
                    output = extraction_provider(
                        artifact_id=extraction_id,
                        input_hash=input_hash,
                        chunk=chunk,
                        profile=request.profile,
                    )
                    validation_bundle = self._extraction_bundle(
                        document,
                        version,
                        chunk,
                        output,
                    )
                    extraction_payload = encode_extraction(validation_bundle)
                    self._persist_artifact(
                        request,
                        current_task_id,
                        lease_token,
                        extraction_id,
                        "EXTRACTION",
                        input_hash,
                        request.profile.profile_id,
                        extraction_payload,
                    )
                elif lease_token is not None:
                    self._link_artifact(
                        request.preparation_job_id,
                        current_task_id,
                        lease_token,
                        extraction_id,
                    )
                entities, mentions, assertions = decode_extraction(
                    extraction_payload,
                    tenant_id=request.tenant_id,
                    chunk=chunk,
                    profile=request.profile,
                )

                embedding_input = chunk.checksum
                embedding_id_value = derivation_artifact_id(
                    request.tenant_id,
                    "EMBEDDING",
                    embedding_input,
                    request.embedding_profile.embedding_space_id,
                )
                embedding_payload = self._read_artifact(
                    request.tenant_id,
                    embedding_id_value,
                    "EMBEDDING",
                    embedding_input,
                    request.embedding_profile.embedding_space_id,
                )
                if embedding_payload is None:
                    if lease_token is None:
                        raise IngestionConflict(
                            "terminal preparation is missing an embedding artifact"
                        )
                    vector = embedding_provider(
                        artifact_id=embedding_id_value,
                        input_hash=embedding_input,
                        chunk=chunk,
                        profile=request.embedding_profile,
                    )
                    embedding = ChunkEmbedding(
                        embedding_id=chunk_embedding_id(
                            chunk.chunk_id,
                            request.embedding_profile.embedding_space_id,
                        ),
                        tenant_id=request.tenant_id,
                        chunk_id=chunk.chunk_id,
                        embedding_space_id=request.embedding_profile.embedding_space_id,
                        provider=request.embedding_profile.provider,
                        model=request.embedding_profile.model,
                        revision=request.embedding_profile.revision,
                        dimensions=request.embedding_profile.dimensions,
                        normalization=request.embedding_profile.normalization,
                        created_at=request.ingested_at,
                        vector=tuple(vector),
                    )
                    if not embedding.vector:
                        raise ValueError("embedding provider returned an empty vector")
                    embedding_payload = encode_embedding(embedding)
                    self._persist_artifact(
                        request,
                        current_task_id,
                        lease_token,
                        embedding_id_value,
                        "EMBEDDING",
                        embedding_input,
                        request.embedding_profile.embedding_space_id,
                        embedding_payload,
                    )
                elif lease_token is not None:
                    self._link_artifact(
                        request.preparation_job_id,
                        current_task_id,
                        lease_token,
                        embedding_id_value,
                    )
                embedding = decode_embedding(
                    embedding_payload,
                    tenant_id=request.tenant_id,
                    chunk=chunk,
                    embedding_space_id=request.embedding_profile.embedding_space_id,
                    provider=request.embedding_profile.provider,
                    model=request.embedding_profile.model,
                    revision=request.embedding_profile.revision,
                    dimensions=request.embedding_profile.dimensions,
                    normalization=request.embedding_profile.normalization,
                    created_at=request.ingested_at,
                )
                bundle = ProvenanceBundle(
                    document=document,
                    version=version,
                    chunk=chunk,
                    embedding=embedding,
                    entities=entities,
                    mentions=mentions,
                    assertion=assertions[0] if assertions else None,
                    additional_assertions=assertions[1:],
                    activate_version=False,
                )
                bundles.append(bundle)
                if lease_token is not None:
                    with self.driver.session(database=self.database) as session:
                        session.execute_write(
                            self._complete_task_tx,
                            self.service,
                            request.preparation_job_id,
                            current_task_id,
                            lease_token,
                            self.service.clock.now(),
                        )

            plan = IngestionPlan.build(
                operation_key=request.operation_key,
                profile=request.profile,
                governance_policy=request.governance_policy,
                bundles=tuple(bundles),
                expected_active_snapshot_id=request.expected_active_snapshot_id,
                source_generation=request.source_generation,
                artifact_input_hashes={
                    bundle.chunk.chunk_id: chunk_artifact_input_hash(bundle.chunk)
                    for bundle in bundles
                },
                created_at=request.ingested_at,
                max_attempts=request.max_attempts,
            )
            result = self.service.ingest(plan)
            materialized_embeddings = tuple(
                embedding
                for bundle in bundles
                for embedding in bundle.all_embeddings
            )
            if materialized_embeddings:
                Neo4jEmbeddingIndexManager(
                    self.driver,
                    self.database,
                ).materialize_if_snapshot_active(
                    materialized_embeddings,
                    snapshot_id=plan.snapshot.snapshot_id,
                    source_generation=plan.source_generation,
                )
            if lease_token is not None:
                with self.driver.session(database=self.database) as session:
                    session.execute_write(
                        self.service._finish_job_tx,
                        request.preparation_job_id,
                        JobStatus.SUCCEEDED,
                        "ARTIFACTS_READY",
                        lease_token,
                        self.service.clock.now(),
                    )
            return result
        except Exception as error:
            if lease_token is not None:
                if current_task_id is not None:
                    self._mark_task_failed(
                        request.preparation_job_id,
                        current_task_id,
                        lease_token,
                        type(error).__name__[:80],
                    )
                self.service._record_failure(
                    request.preparation_job_id,
                    lease_token,
                    error,
                    retryable=not isinstance(error, (IngestionConflict, ValueError)),
                )
            raise

    @staticmethod
    def _extraction_bundle(
        document: Document,
        version: DocumentVersion,
        chunk: Chunk,
        output: ExtractionOutput,
    ) -> ProvenanceBundle:
        if not isinstance(output, ExtractionOutput):
            raise TypeError("extraction provider must return ExtractionOutput")
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

    @staticmethod
    def _ensure_preparation_tx(
        tx: Any,
        request: IncrementalIngestionRequest,
        chunks: tuple[Chunk, ...],
        now: datetime,
    ) -> None:
        Neo4jIngestionService._lock_tenant_corpus_state_tx(
            tx,
            request.tenant_id,
            now,
        )
        Neo4jIngestionService._assert_source_generation_tx(
            tx,
            request.tenant_id,
            request.document_id,
            request.source_generation,
        )
        properties = {
            "job_id": request.preparation_job_id,
            "tenant_id": request.tenant_id,
            "operation": "PREPARE_UPSERT",
            "operation_key": request.operation_key,
            "idempotency_key": request.operation_key,
            "request_fingerprint": request.request_fingerprint,
            "document_id": request.document_id,
            "target_version_id": request.version_id,
            "target_snapshot_id": request.snapshot_id,
            "expected_active_snapshot_id": request.expected_active_snapshot_id or "",
            "source_generation": request.source_generation,
            "expected_tasks": len(chunks),
            "max_attempts": request.max_attempts,
        }
        Neo4jIngestionService._ensure_generic_job_tx(tx, properties, now)
        for chunk in chunks:
            task_id = ingestion_task_id(request.preparation_job_id, chunk.chunk_id)
            input_hash = chunk_artifact_input_hash(chunk)
            record = tx.run(
                """
                MERGE (task:IngestionTask {task_id: $task_id})
                ON CREATE SET task.job_id = $job_id,
                              task.chunk_id = $chunk_id,
                              task.input_hash = $input_hash,
                              task.status = 'PENDING',
                              task.created_at = $now
                WITH task
                MATCH (job:IngestionJob {job_id: $job_id})
                MERGE (job)-[:HAS_TASK]->(task)
                RETURN task.job_id = $job_id
                   AND task.chunk_id = $chunk_id
                   AND task.input_hash = $input_hash AS compatible
                """,
                task_id=task_id,
                job_id=request.preparation_job_id,
                chunk_id=chunk.chunk_id,
                input_hash=input_hash,
                now=now,
            ).single()
            if record is None or not record["compatible"]:
                raise IngestionConflict("provider task identity conflicts")

    @staticmethod
    def _start_task_tx(
        tx: Any,
        service: Neo4jIngestionService,
        job_id: str,
        task_id: str,
        lease_token: str,
        now: datetime,
    ) -> None:
        Neo4jIncrementalPipeline._assert_task_source_tx(
            tx,
            service,
            job_id,
            task_id,
            lease_token,
            now,
        )
        tx.run(
            """
            MATCH (:IngestionJob {job_id: $job_id})-[:HAS_TASK]->(
                task:IngestionTask {task_id: $task_id}
            )
            SET task.status = CASE
                    WHEN task.status = 'DERIVED' THEN 'DERIVED'
                    ELSE 'RUNNING'
                END,
                task.started_at = coalesce(task.started_at, $now),
                task.updated_at = $now,
                task.last_error_code = ''
            """,
            job_id=job_id,
            task_id=task_id,
            now=now,
        ).consume()

    @staticmethod
    def _complete_task_tx(
        tx: Any,
        service: Neo4jIngestionService,
        job_id: str,
        task_id: str,
        lease_token: str,
        now: datetime,
    ) -> None:
        Neo4jIncrementalPipeline._assert_task_source_tx(
            tx,
            service,
            job_id,
            task_id,
            lease_token,
            now,
        )
        tx.run(
            """
            MATCH (job:IngestionJob {job_id: $job_id})-[:HAS_TASK]->(
                task:IngestionTask {task_id: $task_id}
            )
            SET task.status = 'DERIVED',
                task.completed_at = coalesce(task.completed_at, $now),
                task.updated_at = $now
            WITH job
            MATCH (job)-[:HAS_TASK]->(completed:IngestionTask {status: 'DERIVED'})
            WITH job, count(DISTINCT completed) AS completed_count
            SET job.completed_tasks = completed_count, job.updated_at = $now
            """,
            job_id=job_id,
            task_id=task_id,
            now=now,
        ).consume()

    def _read_artifact(
        self,
        tenant_id: str,
        artifact_id: str,
        kind: str,
        input_hash: str,
        profile_id: str,
    ) -> dict[str, Any] | None:
        with self.driver.session(database=self.database) as session:
            record = session.run(
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
        if record is None:
            return None
        if (
            record["kind"] != kind
            or record["input_hash"] != input_hash
            or record["profile_id"] != profile_id
        ):
            raise IngestionConflict("derivation artifact identity conflicts")
        payload = json.loads(record["payload_json"])
        if _fingerprint(payload) != record["output_checksum"]:
            raise IngestionConflict("derivation artifact payload checksum is invalid")
        return payload

    def _persist_artifact(
        self,
        request: IncrementalIngestionRequest,
        task_id: str,
        lease_token: str | None,
        artifact_id: str,
        kind: str,
        input_hash: str,
        profile_id: str,
        payload: dict[str, Any],
    ) -> None:
        if lease_token is None:
            raise IngestionConflict("terminal preparation is missing an artifact")
        with self.driver.session(database=self.database) as session:
            session.execute_write(
                self._persist_artifact_tx,
                self.service,
                request.preparation_job_id,
                task_id,
                lease_token,
                request.tenant_id,
                artifact_id,
                kind,
                input_hash,
                profile_id,
                payload,
                self.service.clock.now(),
            )

    @staticmethod
    def _persist_artifact_tx(
        tx: Any,
        service: Neo4jIngestionService,
        job_id: str,
        task_id: str,
        lease_token: str,
        tenant_id: str,
        artifact_id: str,
        kind: str,
        input_hash: str,
        profile_id: str,
        payload: dict[str, Any],
        now: datetime,
    ) -> None:
        Neo4jIncrementalPipeline._assert_task_source_tx(
            tx,
            service,
            job_id,
            task_id,
            lease_token,
            now,
        )
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        immutable = {
            "artifact_id": artifact_id,
            "tenant_id": tenant_id,
            "kind": kind,
            "input_hash": input_hash,
            "profile_id": profile_id,
            "output_checksum": _fingerprint(payload),
            "payload_json": payload_json,
        }
        record = tx.run(
            """
            MERGE (artifact:DerivationArtifact {artifact_id: $artifact_id})
            ON CREATE SET artifact = $immutable, artifact.created_at = $now
            RETURN all(
                key IN keys($immutable)
                WHERE artifact[key] = $immutable[key]
            ) AS compatible
            """,
            artifact_id=artifact_id,
            immutable=immutable,
            now=now,
        ).single()
        if record is None or not record["compatible"]:
            raise IngestionConflict("derivation artifact checksum conflicts")
        link = tx.run(
            """
            MATCH (task:IngestionTask {task_id: $task_id, job_id: $job_id})
            MATCH (artifact:DerivationArtifact {artifact_id: $artifact_id})
            MERGE (task)-[:USED_ARTIFACT]->(artifact)
            RETURN artifact.artifact_id AS artifact_id
            """,
            task_id=task_id,
            job_id=job_id,
            artifact_id=artifact_id,
        ).single()
        if link is None:
            raise IngestionConflict("provider task disappeared before artifact link")

    def _link_artifact(
        self,
        job_id: str,
        task_id: str,
        lease_token: str,
        artifact_id: str,
    ) -> None:
        with self.driver.session(database=self.database) as session:
            session.execute_write(
                self._link_artifact_tx,
                self.service,
                job_id,
                task_id,
                lease_token,
                artifact_id,
                self.service.clock.now(),
            )

    @staticmethod
    def _link_artifact_tx(
        tx: Any,
        service: Neo4jIngestionService,
        job_id: str,
        task_id: str,
        lease_token: str,
        artifact_id: str,
        now: datetime,
    ) -> None:
        Neo4jIncrementalPipeline._assert_task_source_tx(
            tx,
            service,
            job_id,
            task_id,
            lease_token,
            now,
        )
        record = tx.run(
            """
            MATCH (task:IngestionTask {task_id: $task_id, job_id: $job_id})
            MATCH (artifact:DerivationArtifact {artifact_id: $artifact_id})
            MERGE (task)-[:USED_ARTIFACT]->(artifact)
            RETURN artifact.artifact_id AS artifact_id
            """,
            task_id=task_id,
            job_id=job_id,
            artifact_id=artifact_id,
        ).single()
        if record is None:
            raise IngestionConflict("cached derivation artifact disappeared")

    @staticmethod
    def _assert_task_source_tx(
        tx: Any,
        service: Neo4jIngestionService,
        job_id: str,
        task_id: str,
        lease_token: str,
        now: datetime,
    ) -> None:
        service._assert_owned_job_tx(tx, job_id, lease_token, now)
        job = tx.run(
            """
            MATCH (job:IngestionJob {job_id: $job_id})
            RETURN job.tenant_id AS tenant_id,
                   job.document_id AS document_id,
                   job.source_generation AS source_generation
            """,
            job_id=job_id,
        ).single()
        if job is None:
            raise IngestionConflict("provider job is missing or was deleted")
        service._lock_tenant_corpus_state_tx(tx, job["tenant_id"], now)
        service._assert_source_generation_tx(
            tx,
            job["tenant_id"],
            job["document_id"],
            int(job["source_generation"]),
        )
        task = tx.run(
            """
            MATCH (:IngestionJob {job_id: $job_id})-[:HAS_TASK]->(
                task:IngestionTask {task_id: $task_id}
            )
            RETURN task.task_id AS task_id
            """,
            job_id=job_id,
            task_id=task_id,
        ).single()
        if task is None:
            raise IngestionConflict("provider task is missing or was deleted")

    def _mark_task_failed(
        self,
        job_id: str,
        task_id: str,
        lease_token: str,
        error_code: str,
    ) -> None:
        with self.driver.session(database=self.database) as session:
            session.execute_write(
                self._mark_task_failed_tx,
                job_id,
                task_id,
                lease_token,
                error_code,
                self.service.clock.now(),
            )

    @staticmethod
    def _mark_task_failed_tx(
        tx: Any,
        job_id: str,
        task_id: str,
        lease_token: str,
        error_code: str,
        now: datetime,
    ) -> None:
        tx.run(
            """
            MATCH (job:IngestionJob {
                job_id: $job_id,
                status: 'RUNNING',
                lease_token: $lease_token
            })-[:HAS_TASK]->(task:IngestionTask {task_id: $task_id})
            SET task.status = 'RETRY_WAIT',
                task.last_error_code = $error_code,
                task.updated_at = $now
            """,
            job_id=job_id,
            task_id=task_id,
            lease_token=lease_token,
            error_code=error_code,
            now=now,
        ).consume()
