"""Durable provider orchestration and cache-before-compute tests."""

from __future__ import annotations

from collections import Counter
import dataclasses
from dataclasses import dataclass
import hashlib
import ipaddress
import os
import unittest
from urllib.parse import urlparse

import neo4j

from graphrag_prod.domain import (
    Assertion,
    ChunkEmbedding,
    Entity,
    EntityMention,
    assertion_id,
    chunk_embedding_id,
    derivation_artifact_id,
    entity_id,
    ingestion_job_id,
    mention_id,
)
from graphrag_prod.graph.schema import apply_schema, verify_schema
from graphrag_prod.ingestion import (
    Checkpoint,
    ChunkSeed,
    EmbeddingProfile,
    ExtractionOutput,
    IncrementalIngestionRequest,
    IngestionConflict,
    IngestionInterrupted,
    JobStatus,
    Neo4jEmbeddingIndexManager,
    Neo4jIngestionService,
    Neo4jIncrementalPipeline,
)
from tests.fixtures.ingestion import (
    CHUNKS_V1,
    CHUNKS_V2,
    FIXED_TIME,
    FixedClock,
    make_governance_policy,
    make_profile,
)


@dataclass(frozen=True, slots=True)
class ProviderCall:
    artifact_id: str
    input_hash: str
    chunk_id: str
    ordinal: int


class ProviderProbe:
    """Record the durable job/task state visible at provider boundaries."""

    def __init__(
        self,
        driver: neo4j.Driver,
        database: str,
        preparation_job_id: str,
    ) -> None:
        self.driver = driver
        self.database = database
        self.preparation_job_id = preparation_job_id
        self.observations: list[dict[str, object]] = []

    def record(self) -> None:
        if self.observations:
            return
        records, _, _ = self.driver.execute_query(
            """
            MATCH (job:IngestionJob {job_id: $job_id})
            OPTIONAL MATCH (job)-[:HAS_TASK]->(task:IngestionTask)
            RETURN job.operation AS operation,
                   job.status AS status,
                   job.phase AS phase,
                   job.expected_tasks AS expected_tasks,
                   job.attempts AS attempts,
                   job.lease_token AS lease_token,
                   count(task) AS task_count,
                   collect(task.status) AS task_statuses
            """,
            job_id=self.preparation_job_id,
            database_=self.database,
        )
        if records:
            self.observations.append(dict(records[0]))


class RecordingExtractionProvider:
    def __init__(
        self,
        probe: ProviderProbe,
        *,
        fail_on_call: int | None = None,
    ) -> None:
        self.probe = probe
        self.fail_on_call = fail_on_call
        self.failed_once = False
        self.calls: list[ProviderCall] = []

    def __call__(
        self,
        *,
        artifact_id: str,
        input_hash: str,
        chunk,
        profile,
    ) -> ExtractionOutput:
        self.probe.record()
        expected_artifact_id = derivation_artifact_id(
            chunk.tenant_id,
            "EXTRACTION",
            input_hash,
            profile.profile_id,
        )
        if artifact_id != expected_artifact_id:
            raise AssertionError("extraction provider received an unstable artifact ID")
        self.calls.append(
            ProviderCall(artifact_id, input_hash, chunk.chunk_id, chunk.ordinal)
        )
        if (
            self.fail_on_call is not None
            and len(self.calls) == self.fail_on_call
            and not self.failed_once
        ):
            self.failed_once = True
            raise RuntimeError("transient extraction provider failure")

        subject_identifier = entity_id(
            chunk.tenant_id,
            "Company",
            "ticker:AAPL",
        )
        subject = Entity(
            entity_id=subject_identifier,
            tenant_id=chunk.tenant_id,
            entity_type="Company",
            canonical_key="ticker:AAPL",
            canonical_name="Apple Inc.",
            aliases=("Apple",),
        )
        relative_start = chunk.text.index("Apple")
        mention_start = chunk.char_start + relative_start
        mention_end = mention_start + len("Apple")
        predicate_by_keyword = {
            "revenue": "REPORTS_REVENUE",
            "margin": "REPORTS_MARGIN",
            "cash": "REPORTS_CASH",
        }
        predicate = next(
            value
            for keyword, value in predicate_by_keyword.items()
            if keyword in chunk.text
        )
        literal = chunk.text.rsplit(" ", 1)[-1].rstrip(".")
        return ExtractionOutput(
            entities=(subject,),
            mentions=(
                EntityMention(
                    mention_id=mention_id(
                        chunk.chunk_id,
                        "Company",
                        mention_start,
                        mention_end,
                        "Apple",
                        profile.extractor_signature,
                    ),
                    tenant_id=chunk.tenant_id,
                    chunk_id=chunk.chunk_id,
                    entity_id=subject_identifier,
                    entity_type="Company",
                    surface="Apple",
                    char_start=mention_start,
                    char_end=mention_end,
                    extractor_version=profile.extractor_signature,
                    confidence=1.0,
                ),
            ),
            assertions=(
                Assertion(
                    assertion_id=assertion_id(
                        chunk.tenant_id,
                        subject_identifier,
                        predicate,
                        "literal",
                        literal,
                        chunk.chunk_id,
                        chunk.char_start,
                        chunk.char_end,
                        profile.extractor_signature,
                        profile.schema_signature,
                    ),
                    tenant_id=chunk.tenant_id,
                    subject_entity_id=subject_identifier,
                    predicate=predicate,
                    evidence_chunk_id=chunk.chunk_id,
                    evidence_char_start=chunk.char_start,
                    evidence_char_end=chunk.char_end,
                    extractor_version=profile.extractor_signature,
                    schema_version=profile.schema_signature,
                    confidence=1.0,
                    accepted=True,
                    literal_value=literal,
                ),
            ),
        )


class RecordingEmbeddingProvider:
    def __init__(self, probe: ProviderProbe) -> None:
        self.probe = probe
        self.calls: list[ProviderCall] = []

    def __call__(
        self,
        *,
        artifact_id: str,
        input_hash: str,
        chunk,
        profile,
    ) -> tuple[float, ...]:
        self.probe.record()
        expected_artifact_id = derivation_artifact_id(
            chunk.tenant_id,
            "EMBEDDING",
            input_hash,
            profile.embedding_space_id,
        )
        if artifact_id != expected_artifact_id:
            raise AssertionError("embedding provider received an unstable artifact ID")
        self.calls.append(
            ProviderCall(artifact_id, input_hash, chunk.chunk_id, chunk.ordinal)
        )
        digest = hashlib.sha256(chunk.text.encode("utf-8")).digest()
        return tuple(round(value / 255.0, 8) for value in digest[: profile.dimensions])


class DeleteActiveDocumentBeforeExtractionReturns:
    """Return stale provider output after a competing accepted deletion."""

    def __init__(
        self,
        delegate: RecordingExtractionProvider,
        delete_service,
        *,
        tenant_id: str,
        document_id: str,
        expected_active_snapshot_id: str | None,
        source_generation: int,
    ) -> None:
        self.delegate = delegate
        self.delete_service = delete_service
        self.tenant_id = tenant_id
        self.document_id = document_id
        self.expected_active_snapshot_id = expected_active_snapshot_id
        self.source_generation = source_generation
        self.fired = False
        self.delete_result = None

    def __call__(
        self,
        *,
        artifact_id: str,
        input_hash: str,
        chunk,
        profile,
    ) -> ExtractionOutput:
        output = self.delegate(
            artifact_id=artifact_id,
            input_hash=input_hash,
            chunk=chunk,
            profile=profile,
        )
        if not self.fired:
            self.fired = True
            self.delete_result = self.delete_service.delete_document(
                tenant_id=self.tenant_id,
                document_id=self.document_id,
                operation_key="delete-during-pipeline-provider",
                expected_active_snapshot_id=self.expected_active_snapshot_id,
                source_generation=self.source_generation,
            )
        return output


def _chunk_seeds(specs) -> tuple[ChunkSeed, ...]:
    seeds: list[ChunkSeed] = []
    char_start = 0
    for ordinal, spec in enumerate(specs):
        char_end = char_start + len(spec.text)
        seeds.append(
            ChunkSeed(
                ordinal=ordinal,
                text=spec.text,
                char_start=char_start,
                char_end=char_end,
                page_number=1,
                section=f"Metric {ordinal + 1}",
            )
        )
        char_start = char_end
    return tuple(seeds)


def _request(
    *,
    operation_key: str,
    specs=CHUNKS_V1,
    version_number: int = 1,
    expected_active_snapshot_id: str | None = None,
    tenant_id: str = "tenant-stage3-pipeline",
    canonical_uri: str = "https://example.com/knowledge/provider-pipeline",
) -> IncrementalIngestionRequest:
    return IncrementalIngestionRequest(
        operation_key=operation_key,
        tenant_id=tenant_id,
        canonical_uri=canonical_uri,
        title=f"Provider pipeline version {version_number}",
        source_name="stage3-provider-fixture",
        version_number=version_number,
        mime_type="text/plain",
        language="en",
        published_at=FIXED_TIME,
        ingested_at=FIXED_TIME,
        original_checksum=None,
        access_policy_id=f"{tenant_id}:knowledge-readers",
        access_policy_version=1,
        access_groups=frozenset({"knowledge-readers"}),
        source_generation=0,
        expected_active_snapshot_id=expected_active_snapshot_id,
        chunks=_chunk_seeds(specs),
        profile=make_profile(),
        governance_policy=make_governance_policy(),
        embedding_profile=EmbeddingProfile(
            provider="fixture",
            model="durable-provider-four-dimensional",
            revision="v1",
            dimensions=4,
            normalization="none",
        ),
        max_attempts=3,
    )


class Neo4jIncrementalPipelineIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        required = (
            "TEST_NEO4J_URI",
            "TEST_NEO4J_USER",
            "TEST_NEO4J_PASSWORD",
            "TEST_NEO4J_DATABASE",
        )
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            raise RuntimeError(f"missing disposable Neo4j settings: {missing}")
        if os.getenv("GRAPHRAG_ALLOW_DISPOSABLE_DB") != "1":
            raise RuntimeError("GRAPHRAG_ALLOW_DISPOSABLE_DB=1 is required")
        uri = os.environ["TEST_NEO4J_URI"]
        host = urlparse(uri).hostname
        if host is None or not ipaddress.ip_address(host).is_loopback:
            raise RuntimeError("integration tests only accept a loopback Neo4j URI")
        cls.database = os.environ["TEST_NEO4J_DATABASE"]
        cls.driver = neo4j.GraphDatabase.driver(
            uri,
            auth=(
                os.environ["TEST_NEO4J_USER"],
                os.environ["TEST_NEO4J_PASSWORD"],
            ),
        )
        cls.driver.verify_connectivity()
        records, _, _ = cls.driver.execute_query(
            "MATCH (node) RETURN count(node) AS count",
            database_=cls.database,
        )
        if records[0]["count"] != 0:
            cls.driver.close()
            raise RuntimeError("disposable Neo4j database must start empty")
        apply_schema(cls.driver, cls.database)
        cls.driver.execute_query("CALL db.awaitIndexes(60)", database_=cls.database)
        errors = verify_schema(cls.driver, cls.database)
        if errors:
            cls.driver.close()
            raise RuntimeError(f"schema verification failed: {errors}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.driver.close()

    def setUp(self) -> None:
        self.driver.execute_query(
            "MATCH (node) DETACH DELETE node",
            database_=self.database,
        )
        self.pipeline = Neo4jIncrementalPipeline(
            self.driver,
            self.database,
            worker_id="provider-pipeline-test-worker",
        )

    def tearDown(self) -> None:
        self.driver.execute_query(
            "MATCH (node) DETACH DELETE node",
            database_=self.database,
        )

    def _records(self, query: str, **parameters: object) -> list[neo4j.Record]:
        records, _, _ = self.driver.execute_query(
            query,
            parameters_=parameters,
            database_=self.database,
        )
        return records

    def _providers(
        self,
        request: IncrementalIngestionRequest,
        *,
        fail_extraction_on_call: int | None = None,
    ) -> tuple[ProviderProbe, RecordingExtractionProvider, RecordingEmbeddingProvider]:
        prepare_job_id = ingestion_job_id(
            request.tenant_id,
            "PREPARE_UPSERT",
            request.operation_key,
        )
        probe = ProviderProbe(self.driver, self.database, prepare_job_id)
        return (
            probe,
            RecordingExtractionProvider(
                probe,
                fail_on_call=fail_extraction_on_call,
            ),
            RecordingEmbeddingProvider(probe),
        )

    def test_provider_failure_is_durable_and_retry_computes_only_missing_artifacts(
        self,
    ) -> None:
        request = _request(operation_key="durable-provider-retry")
        probe, extraction, embedding = self._providers(
            request,
            fail_extraction_on_call=2,
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "transient extraction provider failure",
        ):
            self.pipeline.run(
                request,
                extraction_provider=extraction,
                embedding_provider=embedding,
            )

        self.assertEqual(len(probe.observations), 1)
        first_boundary = probe.observations[0]
        self.assertEqual(first_boundary["operation"], "PREPARE_UPSERT")
        self.assertEqual(first_boundary["status"], JobStatus.RUNNING.value)
        self.assertEqual(first_boundary["expected_tasks"], len(request.chunks))
        self.assertEqual(first_boundary["attempts"], 1)
        self.assertTrue(first_boundary["lease_token"])
        self.assertEqual(first_boundary["task_count"], len(request.chunks))
        self.assertEqual(
            Counter(first_boundary["task_statuses"]),
            Counter({"RUNNING": 1, "PENDING": len(request.chunks) - 1}),
        )

        prepare_job_id = ingestion_job_id(
            request.tenant_id,
            "PREPARE_UPSERT",
            request.operation_key,
        )
        failed = self._records(
            """
            MATCH (job:IngestionJob {job_id: $job_id})
            OPTIONAL MATCH (job)-[:HAS_TASK]->(:IngestionTask)
                           -[:USED_ARTIFACT]->(artifact:DerivationArtifact)
            RETURN job.status AS status,
                   job.attempts AS attempts,
                   job.lease_token AS lease_token,
                   collect(DISTINCT artifact.artifact_id) AS artifact_ids
            """,
            job_id=prepare_job_id,
        )[0]
        self.assertEqual(failed["status"], JobStatus.RETRY_WAIT.value)
        self.assertEqual(failed["attempts"], 1)
        self.assertFalse(failed["lease_token"])
        self.assertGreaterEqual(len(failed["artifact_ids"]), 1)

        result = self.pipeline.run(
            request,
            extraction_provider=extraction,
            embedding_provider=embedding,
        )
        self.assertEqual(result.job.status, JobStatus.SUCCEEDED)
        self.assertEqual(result.active_snapshot_id, result.snapshot_id)

        completed = self._records(
            """
            MATCH (job:IngestionJob {job_id: $job_id})
            OPTIONAL MATCH (job)-[:HAS_TASK]->(task:IngestionTask)
                           -[:USED_ARTIFACT]->(artifact:DerivationArtifact)
            RETURN job.status AS status,
                   job.attempts AS attempts,
                   count(DISTINCT task) AS task_count,
                   count(DISTINCT artifact) AS artifact_count
            """,
            job_id=prepare_job_id,
        )[0]
        self.assertEqual(completed["status"], JobStatus.SUCCEEDED.value)
        self.assertEqual(completed["attempts"], 2)
        self.assertEqual(completed["task_count"], len(request.chunks))
        self.assertEqual(completed["artifact_count"], len(request.chunks) * 2)

        extraction_counts = Counter(call.artifact_id for call in extraction.calls)
        self.assertEqual(sorted(extraction_counts.values()), [1, 1, 2])
        embedding_counts = Counter(call.artifact_id for call in embedding.calls)
        self.assertEqual(sorted(embedding_counts.values()), [1, 1, 1])
        self.assertEqual(
            {call.ordinal for call in embedding.calls},
            {0, 1, 2},
        )

        active = self._records(
            """
            MATCH (document:Document {tenant_id: $tenant_id})
                  -[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot)
            RETURN snapshot.snapshot_id AS snapshot_id,
                   snapshot.build_state AS build_state,
                   snapshot.actual_chunk_count AS chunk_count
            """,
            tenant_id=request.tenant_id,
        )
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["snapshot_id"], result.snapshot_id)
        self.assertEqual(active[0]["build_state"], "PUBLISHED")
        self.assertEqual(active[0]["chunk_count"], len(request.chunks))

    def test_ingest_interruption_reuses_stable_artifacts_after_clock_advance(
        self,
    ) -> None:
        clock = FixedClock()
        pipeline = Neo4jIncrementalPipeline(
            self.driver,
            self.database,
            worker_id="pipeline-stable-artifact-retry-worker",
            clock=clock,
        )
        request = _request(operation_key="pipeline-stable-artifact-retry")
        probe, extraction, embedding = self._providers(request)
        interrupted = False

        def interrupt_once(
            checkpoint: Checkpoint,
            context: dict[str, object],
        ) -> None:
            nonlocal interrupted
            del context
            if checkpoint is Checkpoint.AFTER_SNAPSHOT_STAGE and not interrupted:
                interrupted = True
                raise IngestionInterrupted("interrupt after provider artifacts persist")

        pipeline.service.failpoint = interrupt_once
        with self.assertRaisesRegex(IngestionInterrupted, "provider artifacts"):
            pipeline.run(
                request,
                extraction_provider=extraction,
                embedding_provider=embedding,
            )
        self.assertEqual(len(extraction.calls), len(request.chunks))
        self.assertEqual(len(embedding.calls), len(request.chunks))

        clock.advance(seconds=120)
        recovered = pipeline.run(
            request,
            extraction_provider=extraction,
            embedding_provider=embedding,
        )
        self.assertEqual(recovered.job.status, JobStatus.SUCCEEDED)
        self.assertEqual(recovered.active_snapshot_id, request.snapshot_id)
        self.assertEqual(len(extraction.calls), len(request.chunks))
        self.assertEqual(len(embedding.calls), len(request.chunks))

        clock.advance(seconds=120)
        replayed = pipeline.run(
            request,
            extraction_provider=extraction,
            embedding_provider=embedding,
        )
        self.assertEqual(replayed.snapshot_id, recovered.snapshot_id)
        self.assertEqual(replayed.job.job_id, recovered.job.job_id)
        self.assertEqual(replayed.job.status, recovered.job.status)
        self.assertEqual(len(extraction.calls), len(request.chunks))
        self.assertEqual(len(embedding.calls), len(request.chunks))

    def test_new_version_reuses_unchanged_artifacts_before_provider_calls(self) -> None:
        v1 = _request(operation_key="cache-before-compute-v1")
        _, extract_v1, embed_v1 = self._providers(v1)
        first = self.pipeline.run(
            v1,
            extraction_provider=extract_v1,
            embedding_provider=embed_v1,
        )
        self.assertEqual(len(extract_v1.calls), 3)
        self.assertEqual(len(embed_v1.calls), 3)

        v2 = _request(
            operation_key="cache-before-compute-v2",
            specs=CHUNKS_V2,
            version_number=2,
            expected_active_snapshot_id=first.snapshot_id,
        )
        _, extract_v2, embed_v2 = self._providers(v2)
        second = self.pipeline.run(
            v2,
            extraction_provider=extract_v2,
            embedding_provider=embed_v2,
        )

        # Only the changed middle chunk reaches either expensive provider.
        self.assertEqual([call.ordinal for call in extract_v2.calls], [1])
        self.assertEqual([call.ordinal for call in embed_v2.calls], [1])
        self.assertNotEqual(first.snapshot_id, second.snapshot_id)
        self.assertEqual(second.active_snapshot_id, second.snapshot_id)

        prepare_job_id = ingestion_job_id(
            v2.tenant_id,
            "PREPARE_UPSERT",
            v2.operation_key,
        )
        reused = self._records(
            """
            MATCH (job:IngestionJob {job_id: $job_id})-[:HAS_TASK]->(
                task:IngestionTask
            )
            OPTIONAL MATCH (task)-[:USED_ARTIFACT]->(artifact:DerivationArtifact)
            RETURN count(DISTINCT task) AS task_count,
                   count(DISTINCT artifact) AS artifact_count,
                   collect(DISTINCT artifact.kind) AS kinds
            """,
            job_id=prepare_job_id,
        )[0]
        self.assertEqual(reused["task_count"], 3)
        self.assertEqual(reused["artifact_count"], 6)
        self.assertEqual(set(reused["kinds"]), {"EXTRACTION", "EMBEDDING"})

        artifact_count = self._records(
            """
            MATCH (artifact:DerivationArtifact {tenant_id: $tenant_id})
            RETURN count(artifact) AS count
            """,
            tenant_id=v2.tenant_id,
        )[0]["count"]
        self.assertEqual(artifact_count, 8)

        snapshots = self._records(
            """
            MATCH (snapshot:KnowledgeSnapshot {tenant_id: $tenant_id})
            RETURN snapshot.snapshot_id AS snapshot_id,
                   snapshot.build_state AS state
            """,
            tenant_id=v2.tenant_id,
        )
        self.assertEqual(
            {record["snapshot_id"]: record["state"] for record in snapshots},
            {
                first.snapshot_id: "RETIRED",
                second.snapshot_id: "PUBLISHED",
            },
        )

    def test_embedding_profile_migration_materializes_without_rebuilding_snapshot(
        self,
    ) -> None:
        v1 = _request(operation_key="pipeline-embedding-space-v1")
        _, extract_v1, embed_v1 = self._providers(v1)
        first = self.pipeline.run(
            v1,
            extraction_provider=extract_v1,
            embedding_provider=embed_v1,
        )
        migrated = dataclasses.replace(
            v1,
            operation_key="pipeline-embedding-space-v2",
            expected_active_snapshot_id=first.snapshot_id,
            embedding_profile=dataclasses.replace(
                v1.embedding_profile,
                revision="v2",
            ),
        )
        _, extract_v2, embed_v2 = self._providers(migrated)

        second = self.pipeline.run(
            migrated,
            extraction_provider=extract_v2,
            embedding_provider=embed_v2,
        )

        self.assertEqual(second.snapshot_id, first.snapshot_id)
        self.assertEqual(second.active_snapshot_id, first.snapshot_id)
        self.assertEqual(extract_v2.calls, [])
        self.assertEqual(len(embed_v2.calls), len(migrated.chunks))
        migrated_space = migrated.embedding_profile.embedding_space_id
        materialized = self._records(
            """
            MATCH (:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })-[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot)
              -[:INCLUDES_CHUNK]->(chunk:Chunk)
            OPTIONAL MATCH (chunk)-[:HAS_EMBEDDING]->(
                embedding:ChunkEmbedding {
                    tenant_id: $tenant_id,
                    embedding_space_id: $embedding_space_id
                }
            )
            RETURN snapshot.snapshot_id AS snapshot_id,
                   count(DISTINCT chunk) AS chunks,
                   count(DISTINCT embedding) AS embeddings
            """,
            tenant_id=migrated.tenant_id,
            document_id=migrated.document_id,
            embedding_space_id=migrated_space,
        )[0]
        self.assertEqual(materialized["snapshot_id"], first.snapshot_id)
        self.assertEqual(materialized["chunks"], len(migrated.chunks))
        self.assertEqual(materialized["embeddings"], len(migrated.chunks))

        manager = Neo4jEmbeddingIndexManager(self.driver, self.database)
        _, _, migrated_chunks = migrated.domain_inputs()
        profile_chunk = migrated_chunks[0]
        embedding_profile = ChunkEmbedding(
            embedding_id=chunk_embedding_id(profile_chunk.chunk_id, migrated_space),
            tenant_id=migrated.tenant_id,
            chunk_id=profile_chunk.chunk_id,
            embedding_space_id=migrated_space,
            provider=migrated.embedding_profile.provider,
            model=migrated.embedding_profile.model,
            revision=migrated.embedding_profile.revision,
            dimensions=migrated.embedding_profile.dimensions,
            normalization=migrated.embedding_profile.normalization,
            created_at=FIXED_TIME,
            vector=(1.0, 0.0, 0.0, 0.0),
        )
        prepared = manager.prepare(
            tenant_id=migrated.tenant_id,
            embedding_profile=embedding_profile,
            generation_version=2,
        )
        coverage = manager.coverage(prepared.generation_id)
        self.assertEqual(coverage.total_chunks, len(migrated.chunks))
        self.assertEqual(coverage.covered_chunks, len(migrated.chunks))
        self.assertTrue(coverage.complete)

    def test_replaying_retired_terminal_pipeline_job_preserves_newer_snapshot(
        self,
    ) -> None:
        v1 = _request(operation_key="pipeline-retired-terminal-v1")
        _, extract_v1, embed_v1 = self._providers(v1)
        first = self.pipeline.run(
            v1,
            extraction_provider=extract_v1,
            embedding_provider=embed_v1,
        )
        v2 = _request(
            operation_key="pipeline-retired-terminal-v2",
            specs=CHUNKS_V2,
            version_number=2,
            expected_active_snapshot_id=first.snapshot_id,
        )
        _, extract_v2, embed_v2 = self._providers(v2)
        second = self.pipeline.run(
            v2,
            extraction_provider=extract_v2,
            embedding_provider=embed_v2,
        )
        _, replay_extraction, replay_embedding = self._providers(v1)

        replay = self.pipeline.run(
            v1,
            extraction_provider=replay_extraction,
            embedding_provider=replay_embedding,
        )

        self.assertEqual(replay.snapshot_id, first.snapshot_id)
        self.assertEqual(replay.job.job_id, first.job.job_id)
        self.assertEqual(replay.job.status, first.job.status)
        self.assertEqual(replay.job.outcome, first.job.outcome)
        self.assertEqual(replay_extraction.calls, [])
        self.assertEqual(replay_embedding.calls, [])
        active = self._records(
            """
            MATCH (:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })-[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot)
            RETURN snapshot.snapshot_id AS snapshot_id
            """,
            tenant_id=v1.tenant_id,
            document_id=v1.document_id,
        )
        self.assertEqual([record["snapshot_id"] for record in active], [second.snapshot_id])

    def test_delete_fences_stale_pipeline_provider_output_without_resurrection(
        self,
    ) -> None:
        v1 = _request(operation_key="pipeline-provider-delete-race-v1")
        _, extraction_v1, embedding_v1 = self._providers(v1)
        active_v1 = self.pipeline.run(
            v1,
            extraction_provider=extraction_v1,
            embedding_provider=embedding_v1,
        )
        v2 = _request(
            operation_key="pipeline-provider-delete-race-v2",
            specs=CHUNKS_V2,
            version_number=2,
            expected_active_snapshot_id=active_v1.snapshot_id,
        )
        _, extraction_v2, embedding_v2 = self._providers(v2)
        delete_service = Neo4jIngestionService(
            self.driver,
            self.database,
            worker_id="pipeline-provider-delete-race-deleter",
        )
        deleting_extraction = DeleteActiveDocumentBeforeExtractionReturns(
            extraction_v2,
            delete_service,
            tenant_id=v1.tenant_id,
            document_id=v1.document_id,
            expected_active_snapshot_id=active_v1.snapshot_id,
            source_generation=v1.source_generation,
        )

        with self.assertRaisesRegex(
            IngestionConflict,
            "(?:generation|missing|deleted)",
        ):
            self.pipeline.run(
                v2,
                extraction_provider=deleting_extraction,
                embedding_provider=embedding_v2,
            )
        self.assertTrue(deleting_extraction.fired)
        self.assertIsNotNone(deleting_extraction.delete_result)
        self.assertEqual(
            deleting_extraction.delete_result.job.status,
            JobStatus.SUCCEEDED,
        )
        self.assertEqual(deleting_extraction.delete_result.job.outcome, "DELETED")
        self.assertEqual(len(extraction_v2.calls), 1)
        self.assertEqual(embedding_v2.calls, [])

        preparation_job = self.pipeline.service.get_job(v2.preparation_job_id)
        self.assertEqual(preparation_job.status, JobStatus.FAILED_PERMANENT)
        residue = self._records(
            """
            OPTIONAL MATCH (document:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            WITH count(document) AS documents
            OPTIONAL MATCH (version:DocumentVersion {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            WITH documents, count(version) AS versions
            OPTIONAL MATCH (chunk:Chunk {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            WITH documents, versions, count(chunk) AS chunks
            OPTIONAL MATCH (embedding:ChunkEmbedding {tenant_id: $tenant_id})
            WITH documents, versions, chunks, count(embedding) AS embeddings
            OPTIONAL MATCH (mention:EntityMention {tenant_id: $tenant_id})
            WITH documents, versions, chunks, embeddings, count(mention) AS mentions
            OPTIONAL MATCH (assertion:Assertion {tenant_id: $tenant_id})
            WITH documents, versions, chunks, embeddings, mentions,
                 count(assertion) AS assertions
            OPTIONAL MATCH (entity:Entity {tenant_id: $tenant_id})
            WITH documents, versions, chunks, embeddings, mentions, assertions,
                 count(entity) AS entities
            OPTIONAL MATCH (snapshot:KnowledgeSnapshot {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            WITH documents, versions, chunks, embeddings, mentions, assertions,
                 entities, count(snapshot) AS snapshots
            OPTIONAL MATCH (artifact:DerivationArtifact {tenant_id: $tenant_id})
            WITH documents, versions, chunks, embeddings, mentions, assertions,
                 entities, snapshots, count(artifact) AS artifacts
            OPTIONAL MATCH (job:IngestionJob {
                tenant_id: $tenant_id,
                document_id: $document_id
            })-[:HAS_TASK]->(task:IngestionTask)
            RETURN documents, versions, chunks, embeddings, mentions, assertions,
                   entities, snapshots, artifacts, count(DISTINCT task) AS tasks
            """,
            tenant_id=v1.tenant_id,
            document_id=v1.document_id,
        )[0]
        for key in (
            "documents",
            "versions",
            "chunks",
            "embeddings",
            "mentions",
            "assertions",
            "entities",
            "snapshots",
            "artifacts",
            "tasks",
        ):
            self.assertEqual(residue[key], 0, key)

        tombstone = self._records(
            """
            MATCH (tombstone:DocumentTombstone {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            RETURN tombstone.generation AS generation,
                   tombstone.deleted_by_job_id AS deleted_by_job_id
            """,
            tenant_id=v1.tenant_id,
            document_id=v1.document_id,
        )
        self.assertEqual(len(tombstone), 1)
        self.assertEqual(tombstone[0]["generation"], 1)
        audit_operations = self._records(
            """
            MATCH (job:IngestionJob {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            RETURN collect(job.operation) AS operations
            """,
            tenant_id=v1.tenant_id,
            document_id=v1.document_id,
        )[0]["operations"]
        self.assertEqual(
            Counter(audit_operations),
            Counter({"PREPARE_UPSERT": 2, "UPSERT": 1, "DELETE": 1}),
        )

    def test_delete_fences_first_create_provider_output_without_resurrection(
        self,
    ) -> None:
        request = _request(operation_key="pipeline-first-create-delete-race")
        _, extraction, embedding = self._providers(request)
        delete_service = Neo4jIngestionService(
            self.driver,
            self.database,
            worker_id="pipeline-first-create-delete-race-deleter",
        )
        deleting_extraction = DeleteActiveDocumentBeforeExtractionReturns(
            extraction,
            delete_service,
            tenant_id=request.tenant_id,
            document_id=request.document_id,
            expected_active_snapshot_id=None,
            source_generation=request.source_generation,
        )

        with self.assertRaisesRegex(
            IngestionConflict,
            "(?:generation|missing|deleted)",
        ):
            self.pipeline.run(
                request,
                extraction_provider=deleting_extraction,
                embedding_provider=embedding,
            )

        self.assertTrue(deleting_extraction.fired)
        self.assertEqual(
            deleting_extraction.delete_result.job.status,
            JobStatus.SUCCEEDED,
        )
        self.assertEqual(deleting_extraction.delete_result.job.outcome, "DELETED")
        self.assertEqual(len(extraction.calls), 1)
        self.assertEqual(embedding.calls, [])
        preparation = self.pipeline.service.get_job(request.preparation_job_id)
        self.assertEqual(preparation.status, JobStatus.FAILED_PERMANENT)

        residue = self._records(
            """
            OPTIONAL MATCH (document:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            WITH count(document) AS documents
            OPTIONAL MATCH (version:DocumentVersion {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            WITH documents, count(version) AS versions
            OPTIONAL MATCH (chunk:Chunk {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            WITH documents, versions, count(chunk) AS chunks
            OPTIONAL MATCH (embedding:ChunkEmbedding {tenant_id: $tenant_id})
            WITH documents, versions, chunks, count(embedding) AS embeddings
            OPTIONAL MATCH (mention:EntityMention {tenant_id: $tenant_id})
            WITH documents, versions, chunks, embeddings, count(mention) AS mentions
            OPTIONAL MATCH (assertion:Assertion {tenant_id: $tenant_id})
            WITH documents, versions, chunks, embeddings, mentions,
                 count(assertion) AS assertions
            OPTIONAL MATCH (entity:Entity {tenant_id: $tenant_id})
            WITH documents, versions, chunks, embeddings, mentions, assertions,
                 count(entity) AS entities
            OPTIONAL MATCH (snapshot:KnowledgeSnapshot {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            WITH documents, versions, chunks, embeddings, mentions, assertions,
                 entities, count(snapshot) AS snapshots
            OPTIONAL MATCH (artifact:DerivationArtifact {tenant_id: $tenant_id})
            WITH documents, versions, chunks, embeddings, mentions, assertions,
                 entities, snapshots, count(artifact) AS artifacts
            OPTIONAL MATCH (:IngestionJob {
                tenant_id: $tenant_id,
                document_id: $document_id
            })-[:HAS_TASK]->(task:IngestionTask)
            RETURN documents, versions, chunks, embeddings, mentions, assertions,
                   entities, snapshots, artifacts, count(DISTINCT task) AS tasks
            """,
            tenant_id=request.tenant_id,
            document_id=request.document_id,
        )[0]
        for key in (
            "documents",
            "versions",
            "chunks",
            "embeddings",
            "mentions",
            "assertions",
            "entities",
            "snapshots",
            "artifacts",
            "tasks",
        ):
            self.assertEqual(residue[key], 0, key)
        tombstone = self._records(
            """
            MATCH (tombstone:DocumentTombstone {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            RETURN tombstone.generation AS generation
            """,
            tenant_id=request.tenant_id,
            document_id=request.document_id,
        )
        self.assertEqual([record["generation"] for record in tombstone], [1])

    def test_pipeline_document_delete_removes_work_and_provenance_only_in_tenant(
        self,
    ) -> None:
        target = _request(operation_key="pipeline-delete-target")
        _, target_extraction, target_embedding = self._providers(target)
        target_result = self.pipeline.run(
            target,
            extraction_provider=target_extraction,
            embedding_provider=target_embedding,
        )

        other = _request(
            operation_key="pipeline-delete-other-tenant",
            tenant_id="tenant-stage3-pipeline-other",
        )
        _, other_extraction, other_embedding = self._providers(other)
        other_result = self.pipeline.run(
            other,
            extraction_provider=other_extraction,
            embedding_provider=other_embedding,
        )

        deleted = self.pipeline.service.delete_document(
            tenant_id=target.tenant_id,
            document_id=target.document_id,
            operation_key="pipeline-delete-target-operation",
            expected_active_snapshot_id=target_result.snapshot_id,
            source_generation=target.source_generation,
        )
        self.assertEqual(deleted.job.status, JobStatus.SUCCEEDED)
        self.assertEqual(deleted.job.outcome, "DELETED")

        target_counts = self._records(
            """
            OPTIONAL MATCH (job:IngestionJob {
                tenant_id: $tenant_id,
                document_id: $document_id
            })-[:HAS_TASK]->(task:IngestionTask)
            WITH count(DISTINCT task) AS tasks
            OPTIONAL MATCH (artifact:DerivationArtifact {tenant_id: $tenant_id})
            WITH tasks, count(DISTINCT artifact) AS artifacts
            OPTIONAL MATCH (version:DocumentVersion {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            WITH tasks, artifacts, count(DISTINCT version) AS versions
            OPTIONAL MATCH (chunk:Chunk {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            WITH tasks, artifacts, versions, count(DISTINCT chunk) AS chunks
            OPTIONAL MATCH (embedding:ChunkEmbedding {tenant_id: $tenant_id})
            WITH tasks, artifacts, versions, chunks,
                 count(DISTINCT embedding) AS embeddings
            OPTIONAL MATCH (mention:EntityMention {tenant_id: $tenant_id})
            WITH tasks, artifacts, versions, chunks, embeddings,
                 count(DISTINCT mention) AS mentions
            OPTIONAL MATCH (assertion:Assertion {tenant_id: $tenant_id})
            WITH tasks, artifacts, versions, chunks, embeddings, mentions,
                 count(DISTINCT assertion) AS assertions
            OPTIONAL MATCH (entity:Entity {tenant_id: $tenant_id})
            RETURN tasks, artifacts, versions, chunks, embeddings, mentions,
                   assertions, count(DISTINCT entity) AS entities
            """,
            tenant_id=target.tenant_id,
            document_id=target.document_id,
        )[0]
        for key in (
            "tasks",
            "artifacts",
            "versions",
            "chunks",
            "embeddings",
            "mentions",
            "assertions",
            "entities",
        ):
            self.assertEqual(target_counts[key], 0, key)
        audit_jobs = self._records(
            """
            MATCH (job:IngestionJob {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            RETURN collect(job.operation) AS operations
            """,
            tenant_id=target.tenant_id,
            document_id=target.document_id,
        )[0]["operations"]
        self.assertEqual(
            Counter(audit_jobs),
            Counter({"PREPARE_UPSERT": 1, "UPSERT": 1, "DELETE": 1}),
        )

        other_projection = self._records(
            """
            MATCH (document:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })-[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot)
            MATCH (snapshot)-[:INCLUDES_CHUNK]->(chunk:Chunk)
            OPTIONAL MATCH (chunk)-[:HAS_EMBEDDING]->(embedding:ChunkEmbedding)
            OPTIONAL MATCH (chunk)<-[:IN_CHUNK]-(mention:EntityMention)
            OPTIONAL MATCH (chunk)<-[:EVIDENCED_BY]-(assertion:Assertion)
            RETURN snapshot.snapshot_id AS snapshot_id,
                   count(DISTINCT chunk) AS chunks,
                   count(DISTINCT embedding) AS embeddings,
                   count(DISTINCT mention) AS mentions,
                   count(DISTINCT assertion) AS assertions
            """,
            tenant_id=other.tenant_id,
            document_id=other.document_id,
        )[0]
        self.assertEqual(other_projection["snapshot_id"], other_result.snapshot_id)
        self.assertEqual(other_projection["chunks"], len(other.chunks))
        self.assertEqual(other_projection["embeddings"], len(other.chunks))
        self.assertEqual(other_projection["mentions"], len(other.chunks))
        self.assertEqual(other_projection["assertions"], len(other.chunks))
        other_counts = self._records(
            """
            MATCH (artifact:DerivationArtifact {tenant_id: $tenant_id})
            WITH count(artifact) AS artifacts
            MATCH (job:IngestionJob {
                tenant_id: $tenant_id,
                document_id: $document_id
            })-[:HAS_TASK]->(task:IngestionTask)
            RETURN artifacts, count(DISTINCT task) AS tasks
            """,
            tenant_id=other.tenant_id,
            document_id=other.document_id,
        )[0]
        self.assertEqual(other_counts["artifacts"], len(other.chunks) * 2)
        self.assertEqual(other_counts["tasks"], len(other.chunks) * 2)


if __name__ == "__main__":
    unittest.main()
