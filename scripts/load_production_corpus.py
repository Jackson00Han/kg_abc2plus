#!/usr/bin/env python3
"""Ingest and verify the deterministic Stage 9 production-reference corpus.

Initial bootstrap uses one governed atomic transaction per document version;
normal incremental deletion still uses the managed lifecycle service.  Only
aggregate counts, stable IDs, digests, and timings enter validation evidence.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Iterable, Iterator
from urllib.parse import urlparse

import neo4j

from graphrag_prod.domain import (
    Chunk,
    ChunkEmbedding,
    Document,
    DocumentVersion,
    Entity,
    EntityMention,
    GraphPipelineProfile,
    embedding_index_generation_id,
    ingestion_job_id,
)
from graphrag_prod.graph.governance import load_governance_policy
from graphrag_prod.graph.provenance import ProvenanceBundle
from graphrag_prod.graph.schema import apply_schema, verify_schema
from graphrag_prod.ingestion import (
    Checkpoint,
    IngestionInterrupted,
    IngestionPlan,
    JobStatus,
    Neo4jBulkInitialLoader,
    Neo4jEmbeddingIndexManager,
    Neo4jIngestionService,
)
from graphrag_prod.ingestion.models import default_artifact_input_hash
from scripts.build_load_corpus import (
    ACTIVE_VERSION_NUMBER,
    CHUNKS_PER_VERSION,
    CODE_SIGNATURE,
    EMBEDDING_SPACE_ID,
    EXTRACTOR_SIGNATURE,
    NORMALIZER_SIGNATURE,
    PIPELINE_PROFILE_ID,
    PRIMARY_TENANT_ID,
    PROMPT_SIGNATURE,
    SCHEMA_SIGNATURE,
    SPLITTER_SIGNATURE,
    VersionBundle,
    build_manifest,
    iter_version_bundles,
)


ROOT = Path(__file__).resolve().parents[1]
_IDENTITY_KEYS = (
    "embedding_id",
    "chunk_id",
    "mention_id",
    "assertion_id",
    "entity_id",
    "snapshot_id",
    "version_id",
    "document_id",
    "generation_id",
    "task_id",
    "artifact_id",
    "job_id",
    "finding_id",
    "issue_id",
    "run_id",
    "decision_id",
    "policy_id",
    "profile_id",
    "tenant_id",
)
_COMMIT_ID = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("load-v1 timestamps must be timezone-aware")
    return parsed


def _profile() -> GraphPipelineProfile:
    return GraphPipelineProfile(
        profile_id=PIPELINE_PROFILE_ID,
        normalizer_signature=NORMALIZER_SIGNATURE,
        splitter_signature=SPLITTER_SIGNATURE,
        extractor_signature=EXTRACTOR_SIGNATURE,
        prompt_signature=PROMPT_SIGNATURE,
        schema_signature=SCHEMA_SIGNATURE,
        code_signature=CODE_SIGNATURE,
    )


def _plan(
    bundle: VersionBundle,
    *,
    expected_active_snapshot_id: str | None,
) -> IngestionPlan:
    source = bundle.document
    created_at = _timestamp(source["created_at"])
    ingested_at = _timestamp(source["ingested_at"])
    document = Document(
        document_id=source["document_id"],
        tenant_id=source["tenant_id"],
        canonical_uri=source["canonical_uri"],
        title=source["title"],
        source_name=source["source_name"],
        access_policy_id=source["access_policy_id"],
        access_policy_version=source["access_policy_version"],
        access_groups=frozenset(source["access_groups"]),
        created_at=created_at,
    )
    version = DocumentVersion(
        version_id=source["version_id"],
        document_id=source["document_id"],
        tenant_id=source["tenant_id"],
        checksum=source["version_checksum"],
        original_checksum=source["original_checksum"],
        normalized_text=source["normalized_text"],
        version_number=source["version_number"],
        mime_type=source["mime_type"],
        language=source["language"],
        published_at=_timestamp(source["published_at"]),
        ingested_at=ingested_at,
    )
    bundles: list[ProvenanceBundle] = []
    entity = Entity(
        entity_id=bundle.entity["entity_id"],
        tenant_id=bundle.entity["tenant_id"],
        entity_type=bundle.entity["entity_type"],
        canonical_key=bundle.entity["canonical_key"],
        canonical_name=bundle.entity["canonical_name"],
        aliases=tuple(bundle.entity["aliases"]),
    )
    mentions_by_chunk = {
        item["chunk_id"]: EntityMention(
            mention_id=item["mention_id"],
            tenant_id=item["tenant_id"],
            chunk_id=item["chunk_id"],
            entity_id=item["entity_id"],
            entity_type=item["entity_type"],
            surface=item["surface"],
            char_start=item["char_start"],
            char_end=item["char_end"],
            extractor_version=item["extractor_version"],
            confidence=item["confidence"],
        )
        for item in bundle.mentions
    }
    for item in bundle.chunks:
        chunk = Chunk(
            chunk_id=item["chunk_id"],
            version_id=item["version_id"],
            document_id=item["document_id"],
            tenant_id=item["tenant_id"],
            access_policy_id=item["access_policy_id"],
            access_policy_version=item["access_policy_version"],
            access_groups=frozenset(item["access_groups"]),
            ordinal=item["ordinal"],
            text=item["text"],
            checksum=item["checksum"],
            char_start=item["char_start"],
            char_end=item["char_end"],
            page_number=item["page_number"],
            section=item["section"],
            splitter_version=item["splitter_version"],
        )
        embedding = ChunkEmbedding(
            embedding_id=item["embedding_id"],
            tenant_id=item["tenant_id"],
            chunk_id=item["chunk_id"],
            embedding_space_id=item["embedding_space_id"],
            provider=item["embedding_provider"],
            model=item["embedding_model"],
            revision=item["embedding_revision"],
            dimensions=item["embedding_dimensions"],
            normalization=item["embedding_normalization"],
            created_at=ingested_at,
            vector=tuple(item["vector"]),
        )
        bundles.append(
            ProvenanceBundle(
                document=document,
                version=version,
                chunk=chunk,
                embedding=embedding,
                entities=(entity,),
                mentions=(mentions_by_chunk[item["chunk_id"]],),
                assertion=None,
                activate_version=False,
            )
        )
    bundle_tuple = tuple(bundles)
    profile = _profile()
    policy = load_governance_policy(
        ROOT / "contracts" / "graph_governance.v1.json",
        profile.schema_signature,
    )
    return IngestionPlan.build(
        operation_key=source["operation_key"],
        profile=profile,
        governance_policy=policy,
        bundles=bundle_tuple,
        expected_active_snapshot_id=expected_active_snapshot_id,
        # Source generation fences deletion/recreation, not ordinary versions.
        source_generation=0,
        artifact_input_hashes={
            item.chunk.chunk_id: default_artifact_input_hash(item)
            for item in bundle_tuple
        },
        created_at=ingested_at,
    )


def iter_load_plans(
    *,
    tenant_id: str | None = None,
    active_only: bool = False,
) -> Iterator[IngestionPlan]:
    """Convert the streamed corpus into governed lifecycle plans."""

    previous_by_document: dict[str, str] = {}
    for bundle in iter_version_bundles(tenant_id=tenant_id):
        document_id = bundle.document["document_id"]
        plan = _plan(
            bundle,
            expected_active_snapshot_id=previous_by_document.get(document_id),
        )
        previous_by_document[document_id] = plan.snapshot.snapshot_id
        if active_only and bundle.document["version_number"] != ACTIVE_VERSION_NUMBER:
            continue
        yield plan


class _InterruptOnce:
    def __init__(self, checkpoint: Checkpoint) -> None:
        self.checkpoint = checkpoint
        self.fired = False

    def __call__(self, checkpoint: Checkpoint, _context: dict[str, object]) -> None:
        if checkpoint is self.checkpoint and not self.fired:
            self.fired = True
            raise IngestionInterrupted(
                f"Stage 9 injected interruption at {checkpoint.value}"
            )


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_jsonable(item) for item in value)
    if isinstance(value, datetime):
        return value.isoformat()
    to_native = getattr(value, "to_native", None)
    if callable(to_native):
        return _jsonable(to_native())
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _identity(labels: list[str], properties: dict[str, Any]) -> str:
    for key in _IDENTITY_KEYS:
        value = properties.get(key)
        if value is not None:
            return f"{','.join(sorted(labels))}:{key}:{value}"
    raise ValueError(f"business node lacks a stable identity: {sorted(labels)}")


def _canonical_node_payload(
    labels: list[str],
    raw_properties: dict[str, Any],
) -> dict[str, Any]:
    properties = dict(raw_properties)
    vector = properties.pop("vector", None)
    if vector is not None:
        vector_values = tuple(float(value) for value in vector)
        vector_payload = json.dumps(
            vector_values,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        actual_checksum = hashlib.sha256(vector_payload.encode("utf-8")).hexdigest()
        if properties.get("vector_checksum") != actual_checksum:
            raise ValueError("stored embedding vector does not match vector_checksum")
        properties["verified_vector_sha256"] = actual_checksum
        properties["vector_dimensions"] = len(vector_values)

    # The acceptance contract excludes retry counters and volatile lifecycle
    # timestamps, not the durable Job/Task/Artifact records themselves.
    if set(labels) & {"IngestionJob", "IngestionTask", "DerivationArtifact"}:
        for key in tuple(properties):
            if key in {
                "attempts",
                "claimed_by",
                "last_error",
                "lease_owner",
            } or key.endswith("_at"):
                properties.pop(key, None)
    return {
        "identity": _identity(labels, properties),
        "labels": sorted(labels),
        "properties": _jsonable(properties),
    }


def canonical_graph_state(
    driver: neo4j.Driver,
    database: str,
    *,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Hash restorable business state without volatile durable-job metadata."""

    digest = hashlib.sha256()
    node_count = 0
    relationship_count = 0
    label_counts: dict[str, int] = {}
    with driver.session(database=database) as session:
        nodes = session.run(
            """
            MATCH (node)
            WHERE $tenant_id IS NULL OR node.tenant_id = $tenant_id
            RETURN labels(node) AS labels, properties(node) AS properties
            """,
            tenant_id=tenant_id,
        )
        node_payloads: list[dict[str, Any]] = []
        for record in nodes:
            labels = sorted(record["labels"])
            payload = _canonical_node_payload(labels, dict(record["properties"]))
            node_payloads.append(payload)
            for label in labels:
                label_counts[label] = label_counts.get(label, 0) + 1
        for payload in sorted(
            node_payloads,
            key=lambda item: (
                item["labels"],
                item["identity"],
                json.dumps(item["properties"], sort_keys=True),
            ),
        ):
            digest.update(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
            digest.update(b"\n")
            node_count += 1

        relationships = session.run(
            """
            MATCH (source)-[relationship]->(target)
            WHERE $tenant_id IS NULL
               OR source.tenant_id = $tenant_id
               OR target.tenant_id = $tenant_id
            RETURN labels(source) AS source_labels,
                   properties(source) AS source_properties,
                   type(relationship) AS relationship_type,
                   properties(relationship) AS relationship_properties,
                   labels(target) AS target_labels,
                   properties(target) AS target_properties
            """,
            tenant_id=tenant_id,
        )
        relationship_payloads: list[dict[str, Any]] = []
        for record in relationships:
            source_labels = sorted(record["source_labels"])
            target_labels = sorted(record["target_labels"])
            source_id = _identity(
                source_labels,
                dict(record["source_properties"]),
            )
            target_id = _identity(
                target_labels,
                dict(record["target_properties"]),
            )
            relationship_payloads.append(
                {
                    "source": source_id,
                    "target": target_id,
                    "type": record["relationship_type"],
                    "properties": _jsonable(
                        dict(record["relationship_properties"])
                    ),
                }
            )
        for payload in sorted(
            relationship_payloads,
            key=lambda item: (
                item["source"],
                item["type"],
                item["target"],
                json.dumps(item["properties"], sort_keys=True),
            ),
        ):
            digest.update(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
            digest.update(b"\n")
            relationship_count += 1
    return {
        "business_node_count": node_count,
        "business_relationship_count": relationship_count,
        "label_counts": dict(sorted(label_counts.items())),
        "sha256": digest.hexdigest(),
    }


def _single_count(
    driver: neo4j.Driver,
    database: str,
    query: str,
    **parameters: object,
) -> int:
    records, _, _ = driver.execute_query(
        query,
        parameters_=parameters,
        database_=database,
    )
    if len(records) != 1:
        raise RuntimeError("count query did not return one record")
    return int(records[0]["count"])


def _recovered_job_projection(
    driver: neo4j.Driver,
    database: str,
    job_id: str,
) -> dict[str, Any]:
    records, _, _ = driver.execute_query(
        """
        MATCH (job:IngestionJob:InitialLoadJob {job_id: $job_id})
        RETURN job.job_id AS job_id,
               job.tenant_id AS tenant_id,
               job.document_id AS document_id,
               job.operation AS operation,
               job.operation_key AS operation_key,
               job.idempotency_key AS idempotency_key,
               job.request_fingerprint AS request_fingerprint,
               job.status AS status,
               job.phase AS phase,
               job.outcome AS outcome,
               job.target_version_id AS target_version_id,
               job.target_snapshot_id AS target_snapshot_id,
               job.expected_active_snapshot_id AS expected_active_snapshot_id,
               job.source_generation AS source_generation,
               job.completed_tasks AS completed_tasks,
               job.expected_tasks AS expected_tasks,
               job.attempts AS attempts,
               job.max_attempts AS max_attempts,
               job.corpus_revision AS corpus_revision
        """,
        job_id=job_id,
        database_=database,
    )
    if len(records) != 1:
        raise RuntimeError("recovered initial-load job is not uniquely durable")
    record = records[0]
    snapshot_records, _, _ = driver.execute_query(
        """
        MATCH (:IngestionJob:InitialLoadJob {job_id: $job_id})
              -[:BUILDS]->(snapshot:KnowledgeSnapshot)
        RETURN snapshot.snapshot_id AS snapshot_id,
               snapshot.manifest_hash AS manifest_hash,
               snapshot.expected_chunk_count AS expected_chunk_count
        ORDER BY snapshot.snapshot_id
        """,
        job_id=job_id,
        database_=database,
    )
    if len(snapshot_records) != 1:
        raise RuntimeError("recovered initial-load job lacks one BUILDS target")
    snapshot = snapshot_records[0]
    target_snapshot_id = str(record["target_snapshot_id"])
    return {
        "attempts": int(record["attempts"]),
        "built_chunk_count": _single_count(
            driver,
            database,
            """
            MATCH (:KnowledgeSnapshot {snapshot_id: $snapshot_id})
                  -[:INCLUDES_CHUNK]->(chunk:Chunk)
            RETURN count(DISTINCT chunk) AS count
            """,
            snapshot_id=target_snapshot_id,
        ),
        "built_embedding_count": _single_count(
            driver,
            database,
            """
            MATCH (:KnowledgeSnapshot {snapshot_id: $snapshot_id})
                  -[:INCLUDES_CHUNK]->(:Chunk)
                  -[:HAS_EMBEDDING]->(embedding:ChunkEmbedding)
            RETURN count(DISTINCT embedding) AS count
            """,
            snapshot_id=target_snapshot_id,
        ),
        "built_snapshot_id": str(snapshot["snapshot_id"]),
        "completed_tasks": int(record["completed_tasks"]),
        "corpus_revision": int(record["corpus_revision"]),
        "document_id": str(record["document_id"]),
        "expected_active_snapshot_id": str(record["expected_active_snapshot_id"]),
        "expected_tasks": int(record["expected_tasks"]),
        "idempotency_key": str(record["idempotency_key"]),
        "job_id": str(record["job_id"]),
        "max_attempts": int(record["max_attempts"]),
        "operation": str(record["operation"]),
        "operation_key": str(record["operation_key"]),
        "outcome": str(record["outcome"]),
        "phase": str(record["phase"]),
        "request_fingerprint": str(record["request_fingerprint"]),
        "snapshot_expected_chunk_count": int(snapshot["expected_chunk_count"]),
        "snapshot_manifest_hash": str(snapshot["manifest_hash"]),
        "source_generation": int(record["source_generation"]),
        "status": str(record["status"]),
        "target_snapshot_id": target_snapshot_id,
        "target_version_id": str(record["target_version_id"]),
        "tenant_id": str(record["tenant_id"]),
    }


def _load_settings() -> tuple[str, str, str, str]:
    names = (
        "TEST_NEO4J_URI",
        "TEST_NEO4J_USER",
        "TEST_NEO4J_PASSWORD",
        "TEST_NEO4J_DATABASE",
    )
    values = tuple(os.getenv(name, "") for name in names)
    missing = [name for name, value in zip(names, values) if not value]
    if missing:
        raise RuntimeError(f"missing disposable Neo4j settings: {missing}")
    if os.getenv("GRAPHRAG_ALLOW_DISPOSABLE_DB") != "1":
        raise RuntimeError("GRAPHRAG_ALLOW_DISPOSABLE_DB=1 is required")
    host = urlparse(values[0]).hostname
    if host is None or not ipaddress.ip_address(host).is_loopback:
        raise RuntimeError("Stage 9 accepts only a loopback disposable Neo4j URI")
    return values  # type: ignore[return-value]


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, values: Iterable[dict[str, Any]]) -> None:
    path.write_text(
        "".join(
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
            for value in values
        ),
        encoding="utf-8",
    )


def run_load(output_dir: Path, *, transaction_timeout_seconds: float) -> None:
    uri, user, password, database = _load_settings()
    output_dir.mkdir(parents=True, exist_ok=False)
    driver = neo4j.GraphDatabase.driver(
        uri,
        auth=(user, password),
        max_connection_pool_size=32,
        connection_acquisition_timeout=5,
    )
    fault_rows: list[dict[str, Any]] = []
    try:
        driver.verify_connectivity()
        initial_count = _single_count(
            driver,
            database,
            "MATCH (node) RETURN count(node) AS count",
        )
        if initial_count != 0:
            raise RuntimeError("Stage 9 load database must start empty")
        schema_started = time.monotonic_ns()
        apply_schema(driver, database)
        driver.execute_query("CALL db.awaitIndexes(120)", database_=database)
        schema_errors = verify_schema(driver, database)
        if schema_errors:
            raise RuntimeError(f"schema verification failed: {schema_errors}")
        schema_finished = time.monotonic_ns()

        loader = Neo4jBulkInitialLoader(
            driver,
            database,
            transaction_timeout_seconds=transaction_timeout_seconds,
        )
        ingestion_started = time.monotonic_ns()
        successful_versions = 0
        submitted_chunks = 0
        recovered_plan_id: str | None = None
        recovered_plan: IngestionPlan | None = None
        interrupted_job_count: int | None = None
        interrupted_task_node_count: int | None = None
        interrupted_before_state_sha256: str | None = None
        interrupted_after_state_sha256: str | None = None
        for plan in iter_load_plans():
            is_recovery_case = (
                plan.tenant_id != PRIMARY_TENANT_ID
                and plan.bundles[0].version.version_number == 1
                and recovered_plan_id is None
            )
            if is_recovery_case:
                before_interruption = canonical_graph_state(driver, database)
                interrupted = Neo4jBulkInitialLoader(
                    driver,
                    database,
                    failpoint=_InterruptOnce(Checkpoint.BEFORE_PUBLISH),
                    transaction_timeout_seconds=transaction_timeout_seconds,
                )
                started = time.monotonic_ns()
                try:
                    interrupted.ingest(plan)
                except IngestionInterrupted:
                    pass
                else:
                    raise RuntimeError("the ingestion interruption did not fire")
                after_interruption = canonical_graph_state(driver, database)
                if after_interruption != before_interruption:
                    raise RuntimeError(
                        "interrupted bulk transaction left partial state"
                    )
                expected_recovery_job_id = ingestion_job_id(
                    plan.tenant_id,
                    "INITIAL_LOAD",
                    plan.operation_key,
                )
                interrupted_job_count = _single_count(
                    driver,
                    database,
                    """
                    MATCH (job:IngestionJob {job_id: $job_id})
                    RETURN count(job) AS count
                    """,
                    job_id=expected_recovery_job_id,
                )
                interrupted_task_node_count = _single_count(
                    driver,
                    database,
                    """
                    MATCH (task:IngestionTask {job_id: $job_id})
                    RETURN count(task) AS count
                    """,
                    job_id=expected_recovery_job_id,
                )
                if interrupted_job_count or interrupted_task_node_count:
                    raise RuntimeError(
                        "interrupted bulk transaction retained lifecycle state"
                    )
                interrupted_before_state_sha256 = before_interruption["sha256"]
                interrupted_after_state_sha256 = after_interruption["sha256"]
                resumed = loader.ingest(plan)
                finished = time.monotonic_ns()
                if (
                    resumed.outcome != "CREATED"
                    or resumed.job_id != expected_recovery_job_id
                ):
                    raise RuntimeError("interrupted ingestion did not recover")
                recovered_plan_id = resumed.job_id
                recovered_plan = plan
                fault_rows.append(
                    {
                        "domain_status": None,
                        "error_code": None,
                        "finished_ns": finished,
                        "http_status": 200,
                        "latency_ms": (finished - started) / 1_000_000,
                        "passed": True,
                        "reason": None,
                        "scenario_id": "interrupted_ingestion",
                        "started_ns": started,
                    }
                )
                result = resumed
            else:
                result = loader.ingest(plan)
            if result.outcome not in {"CREATED", "UPDATED", "REPROCESSED"}:
                raise RuntimeError(f"load plan did not succeed: {plan.job_id}")
            successful_versions += 1
            submitted_chunks += len(plan.bundles)
        ingestion_finished = time.monotonic_ns()
        if (
            recovered_plan_id is None
            or recovered_plan is None
            or interrupted_job_count is None
            or interrupted_task_node_count is None
            or interrupted_before_state_sha256 is None
            or interrupted_after_state_sha256 is None
        ):
            raise RuntimeError("interrupted-ingestion recovery evidence is incomplete")

        manifest = build_manifest()

        # The bulk loader is intentionally disabled as soon as managed index
        # lifecycle work begins. Prove exact replay while every tenant is still
        # in OFFLINE_INITIAL_LOAD mode, then cross that boundary once.
        before_replay = canonical_graph_state(driver, database)
        replay_started = time.monotonic_ns()
        replayed_versions = 0
        for plan in iter_load_plans(active_only=True):
            replayed = loader.ingest(plan)
            if replayed.outcome != "UNCHANGED":
                raise RuntimeError(f"active replay changed job state: {plan.job_id}")
            replayed_versions += 1
        replay_finished = time.monotonic_ns()
        after_replay = canonical_graph_state(driver, database)
        mismatch_count = int(before_replay != after_replay)
        if mismatch_count:
            raise RuntimeError("exact replay changed canonical published graph state")
        fault_rows.append(
            {
                "domain_status": None,
                "error_code": None,
                "finished_ns": replay_finished,
                "http_status": 200,
                "latency_ms": (replay_finished - replay_started) / 1_000_000,
                "passed": True,
                "reason": None,
                "scenario_id": "idempotency",
                "started_ns": replay_started,
            }
        )

        manager = Neo4jEmbeddingIndexManager(driver, database)
        active_generations: dict[str, str] = {}
        embedding_generation_coverage: dict[str, dict[str, Any]] = {}
        tenant_ids = manifest["coverage"]["tenants"]
        for tenant_id in tenant_ids:
            example = next(iter_load_plans(tenant_id=tenant_id, active_only=True))
            profile = example.bundles[0].all_embeddings[0]
            generation = manager.prepare(
                tenant_id=tenant_id,
                embedding_profile=profile,
                generation_version=1,
            )
            coverage = manager.coverage(generation.generation_id)
            if not coverage.complete:
                raise RuntimeError(
                    f"embedding generation coverage is incomplete: {tenant_id}"
                )
            activated = manager.activate(
                generation.generation_id,
                expected_active_generation_id=None,
            )
            expected_generation_id = embedding_index_generation_id(
                tenant_id,
                EMBEDDING_SPACE_ID,
                1,
            )
            active = manager.active_generation(tenant_id)
            if (
                generation.generation_id != expected_generation_id
                or activated.generation_id != expected_generation_id
                or active is None
                or active.generation_id != expected_generation_id
            ):
                raise RuntimeError(
                    f"active embedding generation identity drifted: {tenant_id}"
                )
            active_generations[tenant_id] = activated.generation_id
            embedding_generation_coverage[tenant_id] = {
                "covered_chunks": coverage.covered_chunks,
                "generation_id": activated.generation_id,
                "total_chunks": coverage.total_chunks,
            }
        expected_embedding_coverage = {
            tenant_id: {
                "covered_chunks": (
                    manifest["coverage"]["documents_by_tenant"][tenant_id]
                    * CHUNKS_PER_VERSION
                ),
                "generation_id": embedding_index_generation_id(
                    tenant_id,
                    EMBEDDING_SPACE_ID,
                    1,
                ),
                "total_chunks": (
                    manifest["coverage"]["documents_by_tenant"][tenant_id]
                    * CHUNKS_PER_VERSION
                ),
            }
            for tenant_id in tenant_ids
        }
        if embedding_generation_coverage != expected_embedding_coverage:
            raise RuntimeError(
                "embedding generation coverage does not match load-v1"
            )
        driver.execute_query(
            "CALL db.index.fulltext.awaitEventuallyConsistentIndexRefresh()",
            database_=database,
        )
        query_ready_ns = time.monotonic_ns()

        query_ready_state = canonical_graph_state(driver, database)
        expected_graph_shape = manifest["graph_expectations"][
            "after_generation_activation"
        ]
        observed_graph_shape = {
            "business_node_count": query_ready_state["business_node_count"],
            "business_relationship_count": query_ready_state[
                "business_relationship_count"
            ],
            "label_counts": query_ready_state["label_counts"],
        }
        if observed_graph_shape != expected_graph_shape:
            raise RuntimeError(
                "loaded graph shape does not match committed load-v1 expectations: "
                f"expected={expected_graph_shape}, observed={observed_graph_shape}"
            )
        total_documents = _single_count(
            driver,
            database,
            "MATCH (document:Document) RETURN count(document) AS count",
        )
        database_versions = _single_count(
            driver,
            database,
            "MATCH (version:DocumentVersion) RETURN count(version) AS count",
        )
        total_active = _single_count(
            driver,
            database,
            """
            MATCH (:Document)-[:ACTIVE_SNAPSHOT]->(:KnowledgeSnapshot)
                  -[:INCLUDES_CHUNK]->(chunk:Chunk)
            RETURN count(DISTINCT chunk) AS count
            """,
        )
        primary_active = _single_count(
            driver,
            database,
            """
            MATCH (:Document {tenant_id: $tenant_id})
                  -[:ACTIVE_SNAPSHOT]->(:KnowledgeSnapshot)
                  -[:INCLUDES_CHUNK]->(chunk:Chunk {tenant_id: $tenant_id})
            RETURN count(DISTINCT chunk) AS count
            """,
            tenant_id=PRIMARY_TENANT_ID,
        )
        primary_visible = _single_count(
            driver,
            database,
            """
            MATCH (:Document {tenant_id: $tenant_id})
                  -[:ACTIVE_SNAPSHOT]->(:KnowledgeSnapshot)
                  -[:INCLUDES_CHUNK]->(chunk:Chunk {tenant_id: $tenant_id})
            WHERE any(group IN chunk.access_groups WHERE group IN $groups)
            RETURN count(DISTINCT chunk) AS count
            """,
            tenant_id=PRIMARY_TENANT_ID,
            groups=manifest["coverage"]["load_principal_groups"],
        )
        primary_visible_embeddings = _single_count(
            driver,
            database,
            """
            MATCH (:Document {tenant_id: $tenant_id})
                  -[:ACTIVE_SNAPSHOT]->(:KnowledgeSnapshot)
                  -[:INCLUDES_CHUNK]->(chunk:Chunk {tenant_id: $tenant_id})
                  -[:HAS_EMBEDDING]->(embedding:ChunkEmbedding {
                      tenant_id: $tenant_id,
                      embedding_space_id: $embedding_space_id
                  })
            WHERE any(group IN chunk.access_groups WHERE group IN $groups)
            RETURN count(DISTINCT embedding) AS count
            """,
            tenant_id=PRIMARY_TENANT_ID,
            embedding_space_id=EMBEDDING_SPACE_ID,
            groups=manifest["coverage"]["load_principal_groups"],
        )
        total_historical = _single_count(
            driver,
            database,
            """
            MATCH (document:Document)-[:HAS_VERSION]->(version:DocumentVersion)
                  -[:HAS_CHUNK]->(chunk:Chunk)
            WHERE NOT (document)-[:ACTIVE_VERSION]->(version)
            RETURN count(DISTINCT chunk) AS count
            """,
        )
        counts = manifest["counts"]
        expected_load_state = {
            "completed_versions": counts["versions"],
            "database_documents": counts["documents"],
            "database_versions": counts["versions"],
            "primary_tenant_active_chunks": manifest["coverage"][
                "primary_tenant"
            ]["active_chunks"],
            "replayed_active_versions": counts["documents"],
            "submitted_chunks": counts["total_chunks"],
            "total_active_chunks": counts["active_chunks"],
            "total_historical_chunks": counts["historical_chunks"],
        }
        observed_load_state = {
            "completed_versions": successful_versions,
            "database_documents": total_documents,
            "database_versions": database_versions,
            "primary_tenant_active_chunks": primary_active,
            "replayed_active_versions": replayed_versions,
            "submitted_chunks": submitted_chunks,
            "total_active_chunks": total_active,
            "total_historical_chunks": total_historical,
        }
        if observed_load_state != expected_load_state:
            raise RuntimeError(
                "materialized load corpus does not match its committed manifest: "
                f"expected={expected_load_state}, observed={observed_load_state}"
            )

        acl_coverage = {
            "access_groups": sorted(
                manifest["coverage"]["load_principal_groups"]
            ),
            "cross_tenant_active_chunks": total_active - primary_active,
            "cross_tenant_active_embeddings": total_active - primary_active,
            "denied_same_tenant_active_chunks": primary_active - primary_visible,
            "denied_same_tenant_active_embeddings": (
                primary_active - primary_visible_embeddings
            ),
            "tenant_id": PRIMARY_TENANT_ID,
            "total_same_tenant_active_chunks": primary_active,
            "total_same_tenant_active_embeddings": primary_active,
            "visible_same_tenant_active_chunks": primary_visible,
            "visible_same_tenant_active_embeddings": primary_visible_embeddings,
        }
        expected_acl_coverage = manifest["coverage"]["load_principal_acl"]
        if acl_coverage != expected_acl_coverage:
            raise RuntimeError(
                "load principal visibility does not match the committed manifest: "
                f"expected={expected_acl_coverage}, observed={acl_coverage}"
            )

        recovered_job = _recovered_job_projection(
            driver,
            database,
            recovered_plan_id,
        )
        recovered_job_task_node_count = _single_count(
            driver,
            database,
            """
            MATCH (task:IngestionTask {job_id: $job_id})
            RETURN count(task) AS count
            """,
            job_id=recovered_plan_id,
        )
        recovered_job_linked_task_count = _single_count(
            driver,
            database,
            """
            MATCH (:IngestionJob {job_id: $job_id})-[:HAS_TASK]->(task)
            RETURN count(task) AS count
            """,
            job_id=recovered_plan_id,
        )
        expected_recovered_job = {
            "attempts": 1,
            "built_chunk_count": len(recovered_plan.bundles),
            "built_embedding_count": sum(
                len(bundle.all_embeddings) for bundle in recovered_plan.bundles
            ),
            "built_snapshot_id": recovered_plan.snapshot.snapshot_id,
            "completed_tasks": len(recovered_plan.bundles),
            "corpus_revision": 1,
            "document_id": recovered_plan.document_id,
            "expected_active_snapshot_id": (
                recovered_plan.expected_active_snapshot_id or ""
            ),
            "expected_tasks": len(recovered_plan.bundles),
            "idempotency_key": recovered_plan.operation_key,
            "job_id": recovered_plan_id,
            "max_attempts": 1,
            "operation": "INITIAL_LOAD",
            "operation_key": recovered_plan.operation_key,
            "outcome": "CREATED",
            "phase": "COMPLETE",
            "request_fingerprint": recovered_plan.request_fingerprint,
            "snapshot_expected_chunk_count": (
                recovered_plan.snapshot.expected_chunk_count
            ),
            "snapshot_manifest_hash": recovered_plan.snapshot.manifest_hash,
            "source_generation": recovered_plan.source_generation,
            "status": "SUCCEEDED",
            "target_snapshot_id": recovered_plan.snapshot.snapshot_id,
            "target_version_id": recovered_plan.version_id,
            "tenant_id": recovered_plan.tenant_id,
        }
        if (
            recovered_job != expected_recovered_job
            or recovered_job_task_node_count != 0
            or recovered_job_linked_task_count != 0
        ):
            raise RuntimeError(
                "recovered bulk initial-load lifecycle evidence is inconsistent"
            )

        duration_seconds = (ingestion_finished - ingestion_started) / 1_000_000_000
        _write_json(
            output_dir / "ingestion.json",
            {
                "acl_coverage": acl_coverage,
                "active_generations": active_generations,
                "clean_start": True,
                "completed_versions": successful_versions,
                "database_documents": total_documents,
                "database_versions": database_versions,
                "duration_seconds": duration_seconds,
                "failed_versions": 0,
                "finished_ns": ingestion_finished,
                "idempotency_mismatch_count": mismatch_count,
                "idempotency_after_state_sha256": after_replay["sha256"],
                "idempotency_before_state_sha256": before_replay["sha256"],
                "interrupted_after_state_sha256": interrupted_after_state_sha256,
                "interrupted_before_state_sha256": interrupted_before_state_sha256,
                "interrupted_job_count": interrupted_job_count,
                "interrupted_task_node_count": interrupted_task_node_count,
                "initial_load_transaction_timeout_seconds": (
                    transaction_timeout_seconds
                ),
                "embedding_generation_coverage": embedding_generation_coverage,
                "primary_tenant_active_chunks": primary_active,
                "primary_visible_chunks": primary_visible,
                "recovered_job": recovered_job,
                "recovered_job_id": recovered_plan_id,
                "recovered_job_linked_task_count": (
                    recovered_job_linked_task_count
                ),
                "recovered_job_task_node_count": recovered_job_task_node_count,
                "recovery_checkpoint": Checkpoint.BEFORE_PUBLISH.value,
                "recovery_task_tracking_mode": "aggregate_job_counters",
                "replayed_active_versions": replayed_versions,
                "replay_finished_ns": replay_finished,
                "replay_started_ns": replay_started,
                "schema_apply_ms": (schema_finished - schema_started) / 1_000_000,
                "schema_version": "production-ingestion-observation-v2",
                "started_ns": ingestion_started,
                "submitted_chunks": submitted_chunks,
                "throughput_chunks_per_second": submitted_chunks / duration_seconds,
                "total_active_chunks": total_active,
                "total_historical_chunks": total_historical,
                "total_versions": successful_versions,
                "query_ready_ns": query_ready_ns,
            },
        )
        _write_json(
            output_dir / "graph-state.json",
            {
                "after_idempotent_replay": after_replay,
                "before_idempotent_replay": before_replay,
                "idempotency_mismatch_count": mismatch_count,
                "query_ready_state": query_ready_state,
                "schema_version": "canonical-graph-state-observation-v2",
            },
        )
        _write_json(output_dir / "load-manifest.json", manifest)
        _write_jsonl(output_dir / "faults.jsonl", fault_rows)
    finally:
        driver.close()


def run_delete(output_dir: Path) -> None:
    uri, user, password, database = _load_settings()
    driver = neo4j.GraphDatabase.driver(uri, auth=(user, password))
    try:
        manifest = build_manifest()
        candidate = manifest["coverage"]["deletion_candidate"]
        delete_tenant = candidate["tenant_id"]
        target_plans = tuple(
            plan
            for plan in iter_load_plans(tenant_id=delete_tenant)
            if plan.document_id == candidate["document_id"]
        )
        if len(target_plans) != 2:
            raise RuntimeError("deletion candidate must resolve to exactly two versions")
        active_targets = tuple(
            plan
            for plan in target_plans
            if plan.version_id == candidate["active_version_id"]
        )
        if len(active_targets) != 1:
            raise RuntimeError("deletion candidate active Version does not resolve")
        target = active_targets[0]
        if target.snapshot.snapshot_id != candidate["active_snapshot_id"]:
            raise RuntimeError("deletion candidate active Snapshot drifted")
        active_chunk_ids = {bundle.chunk.chunk_id for bundle in target.bundles}
        if not set(candidate["active_chunk_ids"]) <= active_chunk_ids:
            raise RuntimeError("deletion candidate Chunk canaries drifted")

        version_ids = {plan.version_id for plan in target_plans}
        snapshot_ids = {plan.snapshot.snapshot_id for plan in target_plans}
        chunk_ids = {
            bundle.chunk.chunk_id for plan in target_plans for bundle in plan.bundles
        }
        embedding_ids = {
            embedding.embedding_id
            for plan in target_plans
            for bundle in plan.bundles
            for embedding in bundle.all_embeddings
        }
        mention_ids = {
            mention.mention_id
            for plan in target_plans
            for bundle in plan.bundles
            for mention in bundle.mentions
        }
        assertion_ids = {
            assertion.assertion_id
            for plan in target_plans
            for bundle in plan.bundles
            for assertion in bundle.all_assertions
        }
        entity_ids = {
            entity.entity_id
            for plan in target_plans
            for bundle in plan.bundles
            for entity in bundle.entities
        }
        finding_records, _, _ = driver.execute_query(
            """
            MATCH (snapshot:KnowledgeSnapshot)-[:HAS_GOVERNANCE_FINDING]->(
                finding:GraphGovernanceFinding
            )
            WHERE snapshot.snapshot_id IN $snapshot_ids
            RETURN collect(DISTINCT finding.finding_id) AS finding_ids
            """,
            snapshot_ids=sorted(snapshot_ids),
            database_=database,
        )
        finding_ids = set(finding_records[0]["finding_ids"])
        identity_sets = {
            "Document": ("document_id", {target.document_id}),
            "DocumentVersion": ("version_id", version_ids),
            "KnowledgeSnapshot": ("snapshot_id", snapshot_ids),
            "Chunk": ("chunk_id", chunk_ids),
            "ChunkEmbedding": ("embedding_id", embedding_ids),
            "EntityMention": ("mention_id", mention_ids),
            "Assertion": ("assertion_id", assertion_ids),
            "Entity": ("entity_id", entity_ids),
            "GraphGovernanceFinding": ("finding_id", finding_ids),
        }

        def label_counts() -> dict[str, int]:
            return {
                label: _single_count(
                    driver,
                    database,
                    f"MATCH (node:{label} {{tenant_id: $tenant_id}}) "
                    "RETURN count(node) AS count",
                    tenant_id=delete_tenant,
                )
                for label in identity_sets
            }

        before_counts = label_counts()
        tenant_records, _, _ = driver.execute_query(
            "MATCH (node) WHERE node.tenant_id IS NOT NULL "
            "RETURN DISTINCT node.tenant_id AS tenant_id ORDER BY tenant_id",
            database_=database,
        )
        preserved_tenant_ids = tuple(
            str(record["tenant_id"])
            for record in tenant_records
            if str(record["tenant_id"]) != delete_tenant
        )
        if len(preserved_tenant_ids) < 4:
            raise RuntimeError("deletion validation lacks cross-tenant coverage")
        protected_tenants_before = {
            tenant_id: canonical_graph_state(
                driver,
                database,
                tenant_id=tenant_id,
            )
            for tenant_id in preserved_tenant_ids
        }
        started = time.monotonic_ns()
        result = Neo4jIngestionService(
            driver,
            database,
            worker_id="stage9-delete-validation",
        ).delete_document(
            tenant_id=target.tenant_id,
            document_id=target.document_id,
            operation_key="stage9-production-delete-validation",
            expected_active_snapshot_id=target.snapshot.snapshot_id,
            source_generation=0,
        )
        finished = time.monotonic_ns()
        if result.job.status is not JobStatus.SUCCEEDED:
            raise RuntimeError("Stage 9 delete did not reach terminal success")
        residue_by_label: dict[str, int] = {}
        for label, (id_property, identifiers) in identity_sets.items():
            if not identifiers:
                residue_by_label[label] = 0
                continue
            residue_by_label[label] = _single_count(
                driver,
                database,
                f"MATCH (node:{label}) WHERE node.{id_property} IN $identifiers "
                "RETURN count(node) AS count",
                identifiers=sorted(identifiers),
            )
        residue_count = sum(residue_by_label.values())
        after_counts = label_counts()
        expected_removed = {
            label: len(identifiers)
            for label, (_, identifiers) in identity_sets.items()
        }
        observed_removed = {
            label: before_counts[label] - after_counts[label]
            for label in identity_sets
        }
        protected_tenants_after = {
            tenant_id: canonical_graph_state(
                driver,
                database,
                tenant_id=tenant_id,
            )
            for tenant_id in preserved_tenant_ids
        }
        tombstones, _, _ = driver.execute_query(
            """
            MATCH (tombstone:DocumentTombstone {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            RETURN tombstone.generation AS generation,
                   tombstone.deleted_by_job_id AS job_id
            """,
            tenant_id=delete_tenant,
            document_id=target.document_id,
            database_=database,
        )
        tombstone_valid = (
            len(tombstones) == 1
            and int(tombstones[0]["generation"]) == 1
            and tombstones[0]["job_id"] == result.job.job_id
        )
        initial_job_ids = tuple(
            ingestion_job_id(
                plan.tenant_id,
                "INITIAL_LOAD",
                plan.operation_key,
            )
            for plan in target_plans
        )
        expected_job_ids = {*initial_job_ids, result.job.job_id}
        audit_records, _, _ = driver.execute_query(
            """
            MATCH (job:IngestionJob {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            WHERE job.job_id IN $job_ids
            RETURN job.job_id AS job_id,
                   job.tenant_id AS tenant_id,
                   job.document_id AS document_id,
                   job.operation AS operation,
                   job.operation_key AS operation_key,
                   job.status AS status,
                   job.phase AS phase,
                   job.outcome AS outcome,
                   job.target_version_id AS target_version_id,
                   job.target_snapshot_id AS target_snapshot_id,
                   job.completed_tasks AS completed_tasks,
                   job.expected_tasks AS expected_tasks
            ORDER BY job.job_id
            """,
            tenant_id=delete_tenant,
            document_id=target.document_id,
            job_ids=sorted(expected_job_ids),
            database_=database,
        )
        durable_audit_jobs = [
            {
                "job_id": str(record["job_id"]),
                "tenant_id": str(record["tenant_id"]),
                "document_id": str(record["document_id"]),
                "operation": str(record["operation"]),
                "operation_key": str(record["operation_key"]),
                "outcome": str(record["outcome"]),
                "phase": str(record["phase"]),
                "status": str(record["status"]),
                "target_version_id": str(record["target_version_id"]),
                "target_snapshot_id": str(record["target_snapshot_id"]),
                "completed_tasks": int(record["completed_tasks"]),
                "expected_tasks": int(record["expected_tasks"]),
            }
            for record in audit_records
        ]
        audit_by_id = {record["job_id"]: record for record in audit_records}
        initial_jobs_valid = all(
            job_id in audit_by_id
            and audit_by_id[job_id]["operation"] == "INITIAL_LOAD"
            and audit_by_id[job_id]["status"] == "SUCCEEDED"
            and audit_by_id[job_id]["phase"] == "COMPLETE"
            and audit_by_id[job_id]["outcome"]
            in {"CREATED", "UPDATED", "REPROCESSED"}
            and int(audit_by_id[job_id]["completed_tasks"])
            == int(audit_by_id[job_id]["expected_tasks"])
            for job_id in initial_job_ids
        )
        delete_audit = audit_by_id.get(result.job.job_id)
        delete_job_valid = (
            delete_audit is not None
            and delete_audit["operation"] == "DELETE"
            and delete_audit["status"] == "SUCCEEDED"
            and delete_audit["phase"] == "COMPLETE"
            and delete_audit["outcome"] == "DELETED"
            and int(delete_audit["completed_tasks"]) == 0
            and int(delete_audit["expected_tasks"]) == 0
        )
        durable_audit_records_retained = (
            set(audit_by_id) == expected_job_ids
            and initial_jobs_valid
            and delete_job_valid
        )
        if (
            residue_count
            or observed_removed != expected_removed
            or protected_tenants_before != protected_tenants_after
            or not tombstone_valid
            or not durable_audit_records_retained
        ):
            raise RuntimeError("delete left source residue or changed another tenant")
        state = canonical_graph_state(driver, database)
        _write_json(
            output_dir / "deletion.json",
            {
                "deletion_residue_count": residue_count,
                "delete_job_id": result.job.job_id,
                "document_id": target.document_id,
                "domain_status": None,
                "durable_audit_job_count": len(durable_audit_jobs),
                "durable_audit_job_ids": sorted(expected_job_ids),
                "durable_audit_jobs": durable_audit_jobs,
                "durable_audit_records_retained": (
                    durable_audit_records_retained
                ),
                "error_code": None,
                "expected_removed_counts": expected_removed,
                "finished_ns": finished,
                "http_status": 200,
                "latency_ms": (finished - started) / 1_000_000,
                "observed_removed_counts": observed_removed,
                "other_tenant_preserved": True,
                "passed": True,
                "preserved_tenant_ids": list(preserved_tenant_ids),
                "reason": None,
                "residue_by_label": residue_by_label,
                "scenario_id": "deletion",
                "schema_version": "production-deletion-observation-v1",
                "started_ns": started,
                "tenant_id": delete_tenant,
                "tombstone_generation": 1,
                "tombstone_deleted_by_job_id": str(tombstones[0]["job_id"]),
                "target_active_chunk_ids": sorted(
                    candidate["active_chunk_ids"]
                ),
                "target_active_snapshot_id": candidate["active_snapshot_id"],
                "target_active_version_id": candidate["active_version_id"],
            },
        )
        _write_json(output_dir / "post-delete-graph-state.json", state)
    finally:
        driver.close()


def run_inspect(
    output: Path,
    *,
    actual_image: str,
    actual_repo_digest: str,
    code_commit: str,
) -> None:
    if not actual_image.strip() or any(
        character in actual_image for character in ("\x00", "\r", "\n")
    ):
        raise ValueError("actual image identity is invalid")
    if _SHA256.fullmatch(actual_repo_digest) is None:
        raise ValueError("actual image RepoDigest is invalid")
    if _COMMIT_ID.fullmatch(code_commit) is None:
        raise ValueError("code commit must be a full lowercase Git object ID")
    uri, user, password, database = _load_settings()
    driver = neo4j.GraphDatabase.driver(uri, auth=(user, password))
    try:
        driver.verify_connectivity()
        nodes = _single_count(
            driver,
            database,
            "MATCH (node) RETURN count(node) AS count",
        )
        relationships = _single_count(
            driver,
            database,
            "MATCH ()-[relationship]->() RETURN count(relationship) AS count",
        )
        _write_json(
            output,
            {
                "actual_neo4j_image": actual_image,
                "actual_neo4j_repo_digest": actual_repo_digest,
                "code_commit": code_commit,
                "database_initial_node_count": nodes,
                "database_initial_relationship_count": relationships,
                "schema_version": "production-container-inspection-v2",
            },
        )
    finally:
        driver.close()


def run_fingerprint(output: Path) -> None:
    uri, user, password, database = _load_settings()
    driver = neo4j.GraphDatabase.driver(uri, auth=(user, password))
    try:
        driver.verify_connectivity()
        schema_errors = verify_schema(driver, database)
        if schema_errors:
            raise RuntimeError(f"restored schema verification failed: {schema_errors}")
        state = canonical_graph_state(driver, database)
        state["schema_and_indexes_verified"] = True
        _write_json(output, state)
    finally:
        driver.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--load", action="store_true")
    action.add_argument("--delete-test", action="store_true")
    action.add_argument("--fingerprint", action="store_true")
    action.add_argument("--inspect", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--actual-image")
    parser.add_argument("--actual-repo-digest")
    parser.add_argument("--code-commit")
    parser.add_argument("--initial-load-transaction-timeout-seconds", type=float)
    args = parser.parse_args()
    if args.load or args.delete_test:
        if (
            args.output_dir is None
            or args.output is not None
            or (
                args.load
                and args.initial_load_transaction_timeout_seconds is None
            )
            or (
                args.delete_test
                and args.initial_load_transaction_timeout_seconds is not None
            )
            or any(
                value is not None
                for value in (
                    args.actual_image,
                    args.actual_repo_digest,
                    args.code_commit,
                )
            )
        ):
            parser.error(
                "--load requires --output-dir and "
                "--initial-load-transaction-timeout-seconds; "
                "--delete-test requires only --output-dir"
            )
        if args.load:
            run_load(
                args.output_dir,
                transaction_timeout_seconds=(
                    args.initial_load_transaction_timeout_seconds
                ),
            )
        else:
            if not args.output_dir.is_dir():
                parser.error("--delete-test output directory must already exist")
            run_delete(args.output_dir)
    elif args.fingerprint:
        if (
            args.output is None
            or args.output_dir is not None
            or args.initial_load_transaction_timeout_seconds is not None
            or any(
                value is not None
                for value in (
                    args.actual_image,
                    args.actual_repo_digest,
                    args.code_commit,
                )
            )
        ):
            parser.error("--fingerprint requires only --output")
        run_fingerprint(args.output)
    else:
        if (
            args.output is None
            or args.output_dir is not None
            or args.initial_load_transaction_timeout_seconds is not None
            or any(
                value is None
                for value in (
                    args.actual_image,
                    args.actual_repo_digest,
                    args.code_commit,
                )
            )
        ):
            parser.error(
                "--inspect requires --output, --actual-image, "
                "--actual-repo-digest, and --code-commit"
            )
        run_inspect(
            args.output,
            actual_image=args.actual_image,
            actual_repo_digest=args.actual_repo_digest,
            code_commit=args.code_commit,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
