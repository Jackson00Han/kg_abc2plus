"""Neo4j-backed, at-least-once ingestion with immutable staging and CAS publish."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Protocol
from uuid import uuid4

from graphrag_prod.domain.ids import (
    derivation_artifact_id,
    ingestion_task_id,
)
from graphrag_prod.graph.provenance import Neo4jProvenanceStore, ProvenanceBundle

from .models import (
    Checkpoint,
    IngestionPlan,
    IngestionResult,
    JobPhase,
    JobStatus,
    JobView,
    _fingerprint,
)
from .artifacts import encode_embedding, encode_extraction


class IngestionConflict(RuntimeError):
    """A request violates idempotency, generation, or active-snapshot CAS."""


class JobLeaseConflict(RuntimeError):
    """Another non-expired worker owns the ingestion job."""


class IngestionInterrupted(RuntimeError):
    """Retryable interruption used by workers and deterministic fault tests."""


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


Failpoint = Callable[[Checkpoint, dict[str, Any]], None]


def _noop_failpoint(checkpoint: Checkpoint, context: dict[str, Any]) -> None:
    del checkpoint, context


def _optional(value: Any) -> Any | None:
    return None if value in (None, "") else value


def _native_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    to_native = getattr(value, "to_native", None)
    return to_native() if callable(to_native) else value


def _job_view(record: Any) -> JobView:
    data = record.data() if hasattr(record, "data") else dict(record)
    return JobView(
        job_id=data["job_id"],
        tenant_id=data["tenant_id"],
        operation=data["operation"],
        operation_key=data["operation_key"],
        request_fingerprint=data["request_fingerprint"],
        status=JobStatus(data["status"]),
        phase=JobPhase(data["phase"]),
        document_id=data["document_id"],
        target_version_id=_optional(data.get("target_version_id")),
        target_snapshot_id=_optional(data.get("target_snapshot_id")),
        expected_active_snapshot_id=_optional(
            data.get("expected_active_snapshot_id")
        ),
        source_generation=int(data.get("source_generation", 0)),
        attempts=int(data.get("attempts", 0)),
        max_attempts=int(data.get("max_attempts", 1)),
        completed_tasks=int(data.get("completed_tasks", 0)),
        expected_tasks=int(data.get("expected_tasks", 0)),
        lease_owner=_optional(data.get("lease_owner")),
        lease_token=_optional(data.get("lease_token")),
        lease_expires_at=_native_datetime(data.get("lease_expires_at")),
        outcome=_optional(data.get("outcome")),
        last_error_code=_optional(data.get("last_error_code")),
    )


class Neo4jIngestionService:
    """Orchestrate deterministic units; external providers stay outside transactions."""

    def __init__(
        self,
        driver: Any,
        database: str = "neo4j",
        *,
        worker_id: str | None = None,
        clock: Clock | None = None,
        lease_seconds: int = 60,
        failpoint: Failpoint | None = None,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.driver = driver
        self.database = database
        self.worker_id = (worker_id or f"worker-{uuid4()}").strip()
        if not self.worker_id:
            raise ValueError("worker_id must not be empty")
        self.clock = clock or SystemClock()
        self.lease_seconds = lease_seconds
        self.failpoint = failpoint or _noop_failpoint

    def ingest(self, plan: IngestionPlan) -> IngestionResult:
        """Create, resume, or no-op one immutable snapshot build."""
        now = self.clock.now()
        self._validate_time(now)
        with self.driver.session(database=self.database) as session:
            session.execute_write(self._ensure_upsert_job_tx, plan, now)
            claimed = session.execute_write(self._claim_job_tx, plan.job_id, now)

        if claimed.status in {JobStatus.SUCCEEDED, JobStatus.NOOP}:
            return self._result_for_job(claimed.job_id)
        if claimed.status is JobStatus.FAILED_PERMANENT:
            raise IngestionConflict("ingestion job exhausted its retry budget")
        lease_token = claimed.lease_token
        if lease_token is None:
            raise RuntimeError("claimed ingestion job has no fencing token")

        published = False
        try:
            self.failpoint(Checkpoint.AFTER_JOB_CLAIM, {"job_id": plan.job_id})
            active_snapshot, generation = self._active_state(
                plan.tenant_id,
                plan.document_id,
            )
            if generation != plan.source_generation:
                raise IngestionConflict(
                    "document generation changed; stale work cannot resurrect a deletion"
                )
            if active_snapshot == plan.snapshot.snapshot_id:
                with self.driver.session(database=self.database) as session:
                    session.execute_write(
                        self._finish_existing_snapshot_tx,
                        plan,
                        lease_token,
                        self.clock.now(),
                    )
                return self._result_for_job(plan.job_id)
            if active_snapshot != plan.expected_active_snapshot_id:
                raise IngestionConflict(
                    "active snapshot differs from expected_active_snapshot_id"
                )

            with self.driver.session(database=self.database) as session:
                session.execute_write(
                    self._stage_snapshot_tx,
                    plan,
                    lease_token,
                    self.clock.now(),
                )
            self.failpoint(
                Checkpoint.AFTER_SNAPSHOT_STAGE,
                {"job_id": plan.job_id, "snapshot_id": plan.snapshot.snapshot_id},
            )

            for bundle in sorted(plan.bundles, key=lambda item: item.chunk.ordinal):
                staged = self._stage_bundle(plan, bundle, lease_token)
                if staged:
                    self.failpoint(
                        Checkpoint.AFTER_CHUNK_STAGE,
                        {"job_id": plan.job_id, "chunk_id": bundle.chunk.chunk_id},
                    )

            self.failpoint(Checkpoint.BEFORE_VERIFY, {"job_id": plan.job_id})
            with self.driver.session(database=self.database) as session:
                session.execute_write(
                    self._verify_snapshot_tx,
                    plan,
                    lease_token,
                    self.clock.now(),
                )

            self.failpoint(Checkpoint.BEFORE_PUBLISH, {"job_id": plan.job_id})
            with self.driver.session(database=self.database) as session:
                session.execute_write(
                    self._publish_snapshot_tx,
                    plan,
                    lease_token,
                    self.clock.now(),
                )
            published = True
            self.failpoint(Checkpoint.AFTER_PUBLISH, {"job_id": plan.job_id})
            return self._result_for_job(plan.job_id)
        except Exception as error:
            if not published:
                retryable = not isinstance(error, (IngestionConflict, ValueError))
                self._record_failure(
                    plan.job_id,
                    lease_token,
                    error,
                    retryable=retryable,
                )
            raise

    def get_job(self, job_id: str) -> JobView:
        with self.driver.session(database=self.database) as session:
            record = session.run(
                "MATCH (job:IngestionJob {job_id: $job_id}) RETURN job{.*} AS job",
                job_id=job_id,
            ).single()
        if record is None:
            raise KeyError(f"unknown ingestion job: {job_id}")
        return _job_view(record["job"])

    def get_job_for_tenant(self, tenant_id: str, job_id: str) -> JobView:
        """Read a durable job only inside its authenticated tenant boundary.

        This is the public/application read path.  ``get_job`` remains an
        internal workflow primitive because a worker already holds a stable,
        tenant-derived job identifier while completing an ingestion result.
        """
        tenant_id = tenant_id.strip()
        job_id = job_id.strip()
        if not tenant_id or not job_id:
            raise ValueError("tenant_id and job_id are required")
        with self.driver.session(database=self.database) as session:
            record = session.run(
                """
                MATCH (job:IngestionJob {
                    tenant_id: $tenant_id,
                    job_id: $job_id
                })
                RETURN job{.*} AS job
                """,
                tenant_id=tenant_id,
                job_id=job_id,
            ).single()
        if record is None:
            raise KeyError("unknown ingestion job in tenant")
        return _job_view(record["job"])

    def pending_artifact_ids(self, plan: IngestionPlan) -> tuple[str, ...]:
        """Return content/profile artifacts that require expensive provider work."""
        identifiers: list[str] = []
        input_hashes = dict(plan.artifact_input_hashes)
        for bundle in plan.bundles:
            input_hash = input_hashes[bundle.chunk.chunk_id]
            identifiers.append(
                derivation_artifact_id(
                    plan.tenant_id,
                    "EXTRACTION",
                    input_hash,
                    plan.profile.profile_id,
                )
            )
            identifiers.extend(
                derivation_artifact_id(
                    plan.tenant_id,
                    "EMBEDDING",
                    bundle.chunk.checksum,
                    embedding.embedding_space_id,
                )
                for embedding in bundle.all_embeddings
            )
        if not identifiers:
            return ()
        with self.driver.session(database=self.database) as session:
            records = session.run(
                """
                UNWIND $artifact_ids AS artifact_id
                OPTIONAL MATCH (artifact:DerivationArtifact {artifact_id: artifact_id})
                RETURN artifact_id, artifact IS NOT NULL AS exists
                ORDER BY artifact_id
                """,
                artifact_ids=sorted(set(identifiers)),
            )
            return tuple(
                record["artifact_id"] for record in records if not record["exists"]
            )

    def delete_document(
        self,
        *,
        tenant_id: str,
        document_id: str,
        operation_key: str,
        expected_active_snapshot_id: str | None,
        source_generation: int,
        max_attempts: int = 3,
    ) -> IngestionResult:
        """Atomically hide and physically remove one tenant-scoped document graph."""
        tenant_id = tenant_id.strip()
        document_id = document_id.strip()
        operation_key = operation_key.strip()
        if not tenant_id or not document_id or not operation_key:
            raise ValueError("tenant_id, document_id, and operation_key are required")
        if source_generation < 0 or max_attempts <= 0:
            raise ValueError("generation and max_attempts are invalid")
        from graphrag_prod.domain.ids import ingestion_job_id

        job_id = ingestion_job_id(tenant_id, "DELETE", operation_key)
        fingerprint = _fingerprint(
            {
                "operation": "DELETE",
                "tenant_id": tenant_id,
                "document_id": document_id,
                "expected_active_snapshot_id": expected_active_snapshot_id,
                "source_generation": source_generation,
            }
        )
        now = self.clock.now()
        with self.driver.session(database=self.database) as session:
            session.execute_write(
                self._ensure_generic_job_tx,
                {
                    "job_id": job_id,
                    "tenant_id": tenant_id,
                    "operation": "DELETE",
                    "operation_key": operation_key,
                    "idempotency_key": operation_key,
                    "request_fingerprint": fingerprint,
                    "document_id": document_id,
                    "target_version_id": "",
                    "target_snapshot_id": "",
                    "expected_active_snapshot_id": expected_active_snapshot_id or "",
                    "source_generation": source_generation,
                    "expected_tasks": 0,
                    "max_attempts": max_attempts,
                },
                now,
            )
            claimed = session.execute_write(self._claim_job_tx, job_id, now)
        if claimed.status in {JobStatus.SUCCEEDED, JobStatus.NOOP}:
            return self._result_for_job(job_id)
        if claimed.status is JobStatus.FAILED_PERMANENT:
            raise IngestionConflict("deletion job exhausted its retry budget")
        lease_token = claimed.lease_token
        if lease_token is None:
            raise RuntimeError("claimed deletion job has no fencing token")
        deleted = False
        try:
            self.failpoint(Checkpoint.AFTER_JOB_CLAIM, {"job_id": job_id})
            self.failpoint(Checkpoint.BEFORE_DELETE, {"job_id": job_id})
            with self.driver.session(database=self.database) as session:
                session.execute_write(
                    self._delete_document_tx,
                    job_id,
                    lease_token,
                    tenant_id,
                    document_id,
                    expected_active_snapshot_id,
                    source_generation,
                    self.clock.now(),
                )
            deleted = True
            self.failpoint(Checkpoint.AFTER_DELETE, {"job_id": job_id})
            return self._result_for_job(job_id)
        except Exception as error:
            if not deleted:
                retryable = not isinstance(error, (IngestionConflict, ValueError))
                self._record_failure(
                    job_id,
                    lease_token,
                    error,
                    retryable=retryable,
                )
            raise

    @staticmethod
    def _validate_time(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")

    @staticmethod
    def _ensure_upsert_job_tx(tx: Any, plan: IngestionPlan, now: datetime) -> None:
        first = plan.bundles[0]
        properties = {
            "job_id": plan.job_id,
            "tenant_id": plan.tenant_id,
            "operation": "UPSERT",
            "operation_key": plan.operation_key,
            "idempotency_key": plan.operation_key,
            "request_fingerprint": plan.request_fingerprint,
            "document_id": plan.document_id,
            "target_version_id": plan.version_id,
            "target_snapshot_id": plan.snapshot.snapshot_id,
            "expected_active_snapshot_id": plan.expected_active_snapshot_id or "",
            "source_generation": plan.source_generation,
            "expected_tasks": len(plan.bundles),
            "max_attempts": plan.max_attempts,
            "desired_title": first.document.title,
            "desired_access_policy_id": first.document.access_policy_id,
            "desired_access_policy_version": first.document.access_policy_version,
            "desired_access_groups": sorted(first.document.access_groups),
        }
        Neo4jIngestionService._ensure_generic_job_tx(tx, properties, now)

    @staticmethod
    def _ensure_generic_job_tx(
        tx: Any,
        properties: dict[str, Any],
        now: datetime,
    ) -> None:
        immutable = dict(properties)
        all_properties = {
            **immutable,
            "status": JobStatus.QUEUED.value,
            "phase": JobPhase.PLAN.value,
            "attempts": 0,
            "completed_tasks": 0,
            "lease_owner": "",
            "lease_token": "",
            "outcome": "",
            "last_error_code": "",
            "created_at": now,
            "updated_at": now,
        }
        record = tx.run(
            """
            MERGE (job:IngestionJob {job_id: $job_id})
            ON CREATE SET job = $all_properties
            RETURN all(
                key IN keys($immutable)
                WHERE job[key] = $immutable[key]
            ) AS compatible
            """,
            job_id=properties["job_id"],
            all_properties=all_properties,
            immutable=immutable,
        ).single()
        if record is None or not record["compatible"]:
            raise IngestionConflict(
                "idempotency key is already bound to a different request"
            )

    def _claim_job_tx(self, tx: Any, job_id: str, now: datetime) -> JobView:
        expires_at = now + timedelta(seconds=self.lease_seconds)
        lease_token = str(uuid4())
        record = tx.run(
            """
            MATCH (job:IngestionJob {job_id: $job_id})
            SET job.__lease_lock = randomUUID()
            WITH job
            REMOVE job.__lease_lock
            RETURN job{.*} AS job
            """,
            job_id=job_id,
        ).single()
        if record is None:
            raise KeyError(f"unknown ingestion job: {job_id}")
        current = _job_view(record["job"])
        if current.status in {JobStatus.SUCCEEDED, JobStatus.NOOP}:
            return current
        if current.status is JobStatus.FAILED_PERMANENT:
            raise IngestionConflict("ingestion job failed permanently")
        if (
            current.status is JobStatus.RUNNING
            and current.lease_expires_at is not None
            and current.lease_expires_at > now
        ):
            raise JobLeaseConflict("ingestion job has a non-expired worker lease")
        if current.attempts >= current.max_attempts:
            tx.run(
                """
                MATCH (job:IngestionJob {job_id: $job_id})
                SET job.status = $status,
                    job.last_error_code = 'ATTEMPTS_EXHAUSTED',
                    job.lease_owner = '',
                    job.lease_token = '',
                    job.lease_expires_at = null,
                    job.updated_at = $now
                """,
                job_id=job_id,
                status=JobStatus.FAILED_PERMANENT.value,
                now=now,
            ).consume()
            exhausted = True
        else:
            tx.run(
                """
                MATCH (job:IngestionJob {job_id: $job_id})
                SET job.status = $status,
                    job.phase = CASE
                        WHEN job.phase = $complete THEN $plan
                        ELSE job.phase
                    END,
                    job.attempts = job.attempts + 1,
                    job.lease_owner = $worker_id,
                    job.lease_token = $lease_token,
                    job.lease_expires_at = $expires_at,
                    job.started_at = coalesce(job.started_at, $now),
                    job.updated_at = $now,
                    job.last_error_code = ''
                """,
                job_id=job_id,
                status=JobStatus.RUNNING.value,
                complete=JobPhase.COMPLETE.value,
                plan=JobPhase.PLAN.value,
                worker_id=self.worker_id,
                lease_token=lease_token,
                expires_at=expires_at,
                now=now,
            ).consume()
            exhausted = False
        refreshed = tx.run(
            "MATCH (job:IngestionJob {job_id: $job_id}) RETURN job{.*} AS job",
            job_id=job_id,
        ).single()
        view = _job_view(refreshed["job"])
        if exhausted:
            return view
        return view

    def _stage_snapshot_tx(
        self,
        tx: Any,
        plan: IngestionPlan,
        lease_token: str,
        now: datetime,
    ) -> None:
        self._assert_owned_job_tx(tx, plan.job_id, lease_token, now)
        self._lock_tenant_corpus_state_tx(tx, plan.tenant_id, now)
        self._assert_source_generation_tx(
            tx,
            plan.tenant_id,
            plan.document_id,
            plan.source_generation,
        )
        profile = plan.profile
        profile_properties = asdict(profile)
        Neo4jProvenanceStore._merge_node(
            tx,
            "GraphPipelineProfile",
            "profile_id",
            profile.profile_id,
            profile_properties,
        )
        governance_policy = plan.governance_policy
        governance_policy_properties = {
            "policy_id": governance_policy.policy_id,
            "policy_version": governance_policy.policy_version,
            "payload_hash": governance_policy.payload_hash,
            "payload": governance_policy.canonical_payload,
        }
        Neo4jProvenanceStore._merge_node(
            tx,
            "GraphGovernancePolicy",
            "policy_id",
            governance_policy.policy_id,
            governance_policy_properties,
        )
        snapshot = plan.snapshot
        snapshot_identity = asdict(snapshot)
        snapshot_identity.pop("created_at")
        snapshot_identity["governance_policy_id"] = plan.governance_policy.policy_id
        snapshot_identity["governance_policy_version"] = (
            plan.governance_policy.policy_version
        )
        Neo4jProvenanceStore._merge_node(
            tx,
            "KnowledgeSnapshot",
            "snapshot_id",
            snapshot.snapshot_id,
            snapshot_identity,
            {"build_state": "BUILDING"},
            update_mutable=False,
            on_create_properties={"created_at": snapshot.created_at},
        )
        tx.run(
            """
            MATCH (job:IngestionJob {job_id: $job_id})
            MATCH (snapshot:KnowledgeSnapshot {snapshot_id: $snapshot_id})
            MATCH (profile:GraphPipelineProfile {profile_id: $profile_id})
            MATCH (policy:GraphGovernancePolicy {policy_id: $policy_id})
            MERGE (job)-[:BUILDS]->(snapshot)
            MERGE (snapshot)-[:USES_PROFILE]->(profile)
            MERGE (snapshot)-[:USES_GOVERNANCE_POLICY]->(policy)
            SET job.phase = $phase, job.updated_at = $now
            """,
            job_id=plan.job_id,
            snapshot_id=snapshot.snapshot_id,
            profile_id=profile.profile_id,
            policy_id=governance_policy.policy_id,
            phase=JobPhase.STAGE.value,
            now=now,
        ).consume()
        for finding in plan.governance_findings:
            finding_properties = {
                "finding_id": f"ingestion-finding:{_fingerprint([snapshot.snapshot_id, finding])}",
                "snapshot_id": snapshot.snapshot_id,
                **asdict(finding),
            }
            Neo4jProvenanceStore._merge_node(
                tx,
                "GraphGovernanceFinding",
                "finding_id",
                finding_properties["finding_id"],
                finding_properties,
            )
            tx.run(
                """
                MATCH (snapshot:KnowledgeSnapshot {snapshot_id: $snapshot_id})
                MATCH (finding:GraphGovernanceFinding {finding_id: $finding_id})
                MERGE (snapshot)-[:HAS_GOVERNANCE_FINDING]->(finding)
                """,
                snapshot_id=snapshot.snapshot_id,
                finding_id=finding_properties["finding_id"],
            ).consume()

    def _stage_bundle(
        self,
        plan: IngestionPlan,
        bundle: ProvenanceBundle,
        lease_token: str,
    ) -> bool:
        with self.driver.session(database=self.database) as session:
            return bool(
                session.execute_write(
                    self._stage_bundle_tx,
                    plan,
                    bundle,
                    lease_token,
                    self.clock.now(),
                )
            )

    def _stage_bundle_tx(
        self,
        tx: Any,
        plan: IngestionPlan,
        bundle: ProvenanceBundle,
        lease_token: str,
        now: datetime,
    ) -> bool:
        self._assert_owned_job_tx(tx, plan.job_id, lease_token, now)
        self._lock_tenant_corpus_state_tx(tx, plan.tenant_id, now)
        self._assert_source_generation_tx(
            tx,
            plan.tenant_id,
            plan.document_id,
            plan.source_generation,
        )
        task_id = ingestion_task_id(plan.job_id, bundle.chunk.chunk_id)
        existing = tx.run(
            "MATCH (task:IngestionTask {task_id: $task_id}) RETURN task.status AS status",
            task_id=task_id,
        ).single()
        if existing is not None and existing["status"] == "STAGED":
            return False

        Neo4jProvenanceStore._write_bundle_tx(
            tx,
            bundle,
            staging_job_id=plan.job_id,
        )
        snapshot_id = plan.snapshot.snapshot_id
        tx.run(
            """
            MATCH (snapshot:KnowledgeSnapshot {snapshot_id: $snapshot_id})
            MATCH (version:DocumentVersion {version_id: $version_id})
            MATCH (chunk:Chunk {chunk_id: $chunk_id})
            MERGE (snapshot)-[:OF_VERSION]->(version)
            MERGE (snapshot)-[:INCLUDES_CHUNK]->(chunk)
            """,
            snapshot_id=snapshot_id,
            version_id=bundle.version.version_id,
            chunk_id=bundle.chunk.chunk_id,
        ).consume()
        self._link_membership_tx(tx, snapshot_id, bundle)

        input_hash = dict(plan.artifact_input_hashes)[bundle.chunk.chunk_id]
        task_properties = {
            "task_id": task_id,
            "job_id": plan.job_id,
            "chunk_id": bundle.chunk.chunk_id,
            "input_hash": input_hash,
            "status": "STAGED",
            "completed_at": now,
        }
        Neo4jProvenanceStore._merge_node(
            tx,
            "IngestionTask",
            "task_id",
            task_id,
            task_properties,
        )
        self._merge_artifacts_tx(tx, plan, bundle, now)
        tx.run(
            """
            MATCH (job:IngestionJob {job_id: $job_id})
            MATCH (task:IngestionTask {task_id: $task_id})
            MATCH (snapshot:KnowledgeSnapshot {snapshot_id: $snapshot_id})
            MATCH (chunk:Chunk {chunk_id: $chunk_id})
            MERGE (job)-[:HAS_TASK]->(task)
            MERGE (task)-[:MATERIALIZED]->(chunk)
            MERGE (task)-[:FOR_SNAPSHOT]->(snapshot)
            WITH job
            MATCH (job)-[:HAS_TASK]->(completed:IngestionTask {status: 'STAGED'})
            WITH job, count(DISTINCT completed) AS completed_count
            SET job.completed_tasks = completed_count,
                job.updated_at = $now
            """,
            job_id=plan.job_id,
            task_id=task_id,
            snapshot_id=snapshot_id,
            chunk_id=bundle.chunk.chunk_id,
            now=now,
        ).consume()
        return True

    @staticmethod
    def _link_membership_tx(
        tx: Any,
        snapshot_id: str,
        bundle: ProvenanceBundle,
    ) -> None:
        mappings = (
            (
                "INCLUDES_ENTITY",
                "Entity",
                "entity_id",
                [
                    {
                        "identifier": item.entity_id,
                        "state": {
                            "canonical_name": item.canonical_name,
                            "aliases": list(item.aliases),
                        },
                    }
                    for item in bundle.entities
                ],
            ),
            (
                "INCLUDES_MENTION",
                "EntityMention",
                "mention_id",
                [
                    {
                        "identifier": item.mention_id,
                        "state": {
                            "entity_id": item.entity_id,
                            "confidence": item.confidence,
                        },
                    }
                    for item in bundle.mentions
                ],
            ),
            (
                "INCLUDES_ASSERTION",
                "Assertion",
                "assertion_id",
                [
                    {
                        "identifier": item.assertion_id,
                        "state": {
                            "confidence": item.confidence,
                            "accepted": item.accepted,
                        },
                    }
                    for item in bundle.all_assertions
                ],
            ),
        )
        for relationship, label, property_name, members in mappings:
            if not members:
                continue
            record = tx.run(
                f"""
                MATCH (snapshot:KnowledgeSnapshot {{snapshot_id: $snapshot_id}})
                UNWIND $members AS expected
                MATCH (member:{label} {{{property_name}: expected.identifier}})
                MERGE (snapshot)-[membership:{relationship}]->(member)
                ON CREATE SET membership += expected.state
                WITH membership, expected
                RETURN count(*) AS linked,
                       count(CASE WHEN all(
                           key IN keys(expected.state)
                           WHERE membership[key] = expected.state[key]
                       ) THEN 1 END) AS compatible
                """,
                snapshot_id=snapshot_id,
                members=members,
            ).single()
            if (
                record is None
                or int(record["linked"]) != len(members)
                or int(record["compatible"]) != len(members)
            ):
                raise IngestionConflict(
                    "snapshot member is missing or has conflicting build state"
                )

    @staticmethod
    def _artifact_payloads(
        plan: IngestionPlan,
        bundle: ProvenanceBundle,
    ) -> tuple[dict[str, Any], ...]:
        input_hash = dict(plan.artifact_input_hashes)[bundle.chunk.chunk_id]
        payloads = [
            {
                "kind": "EXTRACTION",
                "input_hash": input_hash,
                "profile_id": plan.profile.profile_id,
                "payload": encode_extraction(bundle),
            }
        ]
        for embedding in bundle.all_embeddings:
            payloads.append(
                {
                    "kind": "EMBEDDING",
                    "input_hash": bundle.chunk.checksum,
                    "profile_id": embedding.embedding_space_id,
                    "payload": encode_embedding(embedding),
                }
            )
        return tuple(payloads)

    @classmethod
    def _merge_artifacts_tx(
        cls,
        tx: Any,
        plan: IngestionPlan,
        bundle: ProvenanceBundle,
        now: datetime,
    ) -> None:
        task_id = ingestion_task_id(plan.job_id, bundle.chunk.chunk_id)
        for spec in cls._artifact_payloads(plan, bundle):
            payload_json = json.dumps(
                spec["payload"],
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            output_checksum = _fingerprint(spec["payload"])
            artifact_id = derivation_artifact_id(
                plan.tenant_id,
                spec["kind"],
                spec["input_hash"],
                spec["profile_id"],
            )
            properties = {
                "artifact_id": artifact_id,
                "tenant_id": plan.tenant_id,
                "kind": spec["kind"],
                "input_hash": spec["input_hash"],
                "profile_id": spec["profile_id"],
                "output_checksum": output_checksum,
                "payload_json": payload_json,
                "created_at": now,
            }
            immutable = {key: value for key, value in properties.items() if key != "created_at"}
            record = tx.run(
                """
                MERGE (artifact:DerivationArtifact {artifact_id: $artifact_id})
                ON CREATE SET artifact = $properties
                RETURN all(
                    key IN keys($immutable)
                    WHERE artifact[key] = $immutable[key]
                ) AS compatible
                """,
                artifact_id=artifact_id,
                properties=properties,
                immutable=immutable,
            ).single()
            if record is None or not record["compatible"]:
                raise IngestionConflict("derivation artifact checksum conflict")
            tx.run(
                """
                MATCH (task:IngestionTask {task_id: $task_id})
                MATCH (artifact:DerivationArtifact {artifact_id: $artifact_id})
                MERGE (task)-[:USED_ARTIFACT]->(artifact)
                """,
                task_id=task_id,
                artifact_id=artifact_id,
            ).consume()

    def _verify_snapshot_tx(
        self,
        tx: Any,
        plan: IngestionPlan,
        lease_token: str,
        now: datetime,
    ) -> None:
        self._assert_owned_job_tx(tx, plan.job_id, lease_token, now)
        self._lock_tenant_corpus_state_tx(tx, plan.tenant_id, now)
        self._assert_source_generation_tx(
            tx,
            plan.tenant_id,
            plan.document_id,
            plan.source_generation,
        )
        snapshot_record = tx.run(
            """
            MATCH (snapshot:KnowledgeSnapshot {snapshot_id: $snapshot_id})
            SET snapshot.__verify_lock = randomUUID()
            WITH snapshot
            REMOVE snapshot.__verify_lock
            RETURN snapshot.manifest_hash AS manifest_hash,
                   snapshot.expected_chunk_count AS expected_chunk_count
            """,
            snapshot_id=plan.snapshot.snapshot_id,
        ).single()
        if snapshot_record is None:
            raise IngestionConflict("target snapshot is missing")
        task_record = tx.run(
            """
            MATCH (job:IngestionJob {job_id: $job_id})
            OPTIONAL MATCH (job)-[:HAS_TASK]->(task:IngestionTask {status: 'STAGED'})
            RETURN count(DISTINCT task) AS completed
            """,
            job_id=plan.job_id,
        ).single()
        if task_record["completed"] != len(plan.bundles):
            raise IngestionInterrupted("snapshot tasks are incomplete")

        manifest = self._snapshot_manifest_from_graph_tx(
            tx,
            plan.snapshot.snapshot_id,
        )
        if _fingerprint(manifest) != plan.snapshot.manifest_hash:
            raise IngestionConflict("staged snapshot manifest does not match request")
        if len(manifest) != plan.snapshot.expected_chunk_count:
            raise IngestionConflict("staged snapshot chunk count is incomplete")
        tx.run(
            """
            MATCH (job:IngestionJob {job_id: $job_id})
            MATCH (snapshot:KnowledgeSnapshot {snapshot_id: $snapshot_id})
            SET snapshot.build_state = 'READY',
                snapshot.actual_chunk_count = $count,
                snapshot.verified_at = $now,
                job.phase = $phase,
                job.updated_at = $now
            """,
            job_id=plan.job_id,
            snapshot_id=plan.snapshot.snapshot_id,
            count=len(manifest),
            now=now,
            phase=JobPhase.VERIFY.value,
        ).consume()

    @staticmethod
    def _snapshot_manifest_from_graph_tx(
        tx: Any,
        snapshot_id: str,
    ) -> tuple[dict[str, Any], ...]:
        records = tx.run(
            """
            MATCH (snapshot:KnowledgeSnapshot {snapshot_id: $snapshot_id})
                  -[:INCLUDES_CHUNK]->(chunk:Chunk)
            OPTIONAL MATCH (snapshot)-[
                mention_membership:INCLUDES_MENTION
            ]->(mention:EntityMention)
            WHERE mention.chunk_id = chunk.chunk_id
            WITH snapshot, chunk,
                 collect(DISTINCT CASE WHEN mention IS NULL THEN null ELSE {
                     mention_id: mention.mention_id,
                     entity_id: mention_membership.entity_id,
                     confidence: mention_membership.confidence
                 } END) AS mentions
            OPTIONAL MATCH (snapshot)-[
                assertion_membership:INCLUDES_ASSERTION
            ]->(assertion:Assertion)
            WHERE assertion.evidence_chunk_id = chunk.chunk_id
            WITH snapshot, chunk, mentions,
                 collect(DISTINCT CASE WHEN assertion IS NULL THEN null ELSE {
                     assertion_id: assertion.assertion_id,
                     confidence: assertion_membership.confidence,
                     accepted: assertion_membership.accepted
                 } END) AS assertions
            OPTIONAL MATCH (snapshot)-[
                entity_membership:INCLUDES_ENTITY
            ]->(entity:Entity)
            WHERE EXISTS {
                MATCH (snapshot)-[:INCLUDES_MENTION]->(entity_mention:EntityMention)
                      -[:REFERS_TO]->(entity)
                WHERE entity_mention.chunk_id = chunk.chunk_id
            }
            RETURN chunk.chunk_id AS chunk_id,
                   chunk.page_number AS page_number,
                   chunk.section AS section,
                   collect(DISTINCT CASE WHEN entity IS NULL THEN null ELSE {
                       entity_id: entity.entity_id,
                       canonical_name: entity_membership.canonical_name,
                       aliases: entity_membership.aliases
                   } END) AS entities,
                   mentions,
                   assertions
            ORDER BY chunk.chunk_id
            """,
            snapshot_id=snapshot_id,
        )
        return tuple(
            {
                "chunk_id": record["chunk_id"],
                "page_number": record["page_number"],
                "section": record["section"],
                "entities": sorted(
                    (dict(item) for item in record["entities"] if item),
                    key=lambda item: item["entity_id"],
                ),
                "mentions": sorted(
                    (dict(item) for item in record["mentions"] if item),
                    key=lambda item: item["mention_id"],
                ),
                "assertions": sorted(
                    (dict(item) for item in record["assertions"] if item),
                    key=lambda item: item["assertion_id"],
                ),
            }
            for record in records
        )

    def _publish_snapshot_tx(
        self,
        tx: Any,
        plan: IngestionPlan,
        lease_token: str,
        now: datetime,
    ) -> None:
        self._assert_owned_job_tx(tx, plan.job_id, lease_token, now)
        snapshot = tx.run(
            """
            MATCH (snapshot:KnowledgeSnapshot {snapshot_id: $snapshot_id})
            RETURN snapshot.build_state AS state
            """,
            snapshot_id=plan.snapshot.snapshot_id,
        ).single()
        if snapshot is None or snapshot["state"] not in {"READY", "PUBLISHED"}:
            raise IngestionConflict("only a verified snapshot can be published")

        self._lock_tenant_corpus_state_tx(tx, plan.tenant_id, now)
        state = self._locked_document_state_tx(
            tx,
            plan.tenant_id,
            plan.document_id,
        )
        if state["generation"] != plan.source_generation:
            raise IngestionConflict("document generation changed before publish")
        current_snapshot = _optional(state.get("active_snapshot_id"))
        if current_snapshot == plan.snapshot.snapshot_id:
            raise IngestionConflict("active snapshot changed during publication")
        if current_snapshot != plan.expected_active_snapshot_id:
            raise IngestionConflict("active snapshot CAS failed")

        document = plan.bundles[0].document
        current_policy_version = state.get("access_policy_version")
        if current_policy_version is not None:
            if document.access_policy_version < current_policy_version:
                raise IngestionConflict("access policy version is stale")
            if document.access_policy_version == current_policy_version and (
                document.access_policy_id != state.get("access_policy_id")
                or sorted(document.access_groups) != sorted(state.get("access_groups") or [])
            ):
                raise IngestionConflict("access policy changed without a new version")

        chunk_ids = sorted(bundle.chunk.chunk_id for bundle in plan.bundles)
        membership = tx.run(
            """
            MATCH (snapshot:KnowledgeSnapshot {snapshot_id: $snapshot_id})
                  -[:INCLUDES_CHUNK]->(chunk:Chunk)
            RETURN collect(DISTINCT chunk.chunk_id) AS chunk_ids
            """,
            snapshot_id=plan.snapshot.snapshot_id,
        ).single()
        if sorted(membership["chunk_ids"]) != chunk_ids:
            raise IngestionConflict("snapshot membership changed before publish")

        tx.run(
            """
            MATCH (document:Document {document_id: $document_id, tenant_id: $tenant_id})
            SET document.title = $title,
                document.access_policy_id = $policy_id,
                document.access_policy_version = $policy_version,
                document.access_groups = $groups,
                document.generation = $generation
            WITH document
            UNWIND $chunk_ids AS chunk_id
            MATCH (chunk:Chunk {chunk_id: chunk_id, tenant_id: $tenant_id})
            SET chunk.access_policy_id = $policy_id,
                chunk.access_policy_version = $policy_version,
                chunk.access_groups = $groups
            """,
            document_id=plan.document_id,
            tenant_id=plan.tenant_id,
            title=document.title,
            policy_id=document.access_policy_id,
            policy_version=document.access_policy_version,
            groups=sorted(document.access_groups),
            generation=plan.source_generation,
            chunk_ids=chunk_ids,
        ).consume()

        # Entity identity is shared. A new governed publication may fill an
        # absent profile/status, but never clears a human quarantine or
        # overwrites a profile supported by another active document.
        for entity in {
            entity.entity_id: entity
            for bundle in plan.bundles
            for entity in bundle.entities
        }.values():
            tx.run(
                """
                MATCH (entity:Entity {entity_id: $entity_id, tenant_id: $tenant_id})
                SET entity.canonical_name = coalesce(
                        entity.canonical_name,
                        $canonical_name
                    ),
                    entity.aliases = coalesce(entity.aliases, $aliases),
                    entity.governance_status = coalesce(
                        entity.governance_status,
                        'ACCEPTED'
                    ),
                    entity.governance_policy_id = coalesce(
                        entity.governance_policy_id,
                        $governance_policy_id
                    )
                """,
                entity_id=entity.entity_id,
                tenant_id=entity.tenant_id,
                canonical_name=entity.canonical_name,
                aliases=list(entity.aliases),
                governance_policy_id=plan.governance_policy.policy_id,
            ).consume()

        old_snapshot_id = current_snapshot
        tx.run(
            """
            MATCH (document:Document {document_id: $document_id, tenant_id: $tenant_id})
            MATCH (snapshot:KnowledgeSnapshot {snapshot_id: $snapshot_id})
            MATCH (version:DocumentVersion {version_id: $version_id})
            OPTIONAL MATCH (document)-[old_snapshot:ACTIVE_SNAPSHOT]->(:KnowledgeSnapshot)
            DELETE old_snapshot
            WITH DISTINCT document, snapshot, version
            OPTIONAL MATCH (document)-[old_version:ACTIVE_VERSION]->(:DocumentVersion)
            DELETE old_version
            MERGE (document)-[:ACTIVE_SNAPSHOT]->(snapshot)
            MERGE (document)-[:ACTIVE_VERSION]->(version)
            SET snapshot.build_state = 'PUBLISHED', snapshot.published_at = $now
            """,
            document_id=plan.document_id,
            tenant_id=plan.tenant_id,
            snapshot_id=plan.snapshot.snapshot_id,
            version_id=plan.version_id,
            now=now,
        ).consume()
        if old_snapshot_id:
            tx.run(
                """
                MATCH (snapshot:KnowledgeSnapshot {snapshot_id: $snapshot_id})
                WHERE NOT (:Document)-[:ACTIVE_SNAPSHOT]->(snapshot)
                SET snapshot.build_state = 'RETIRED', snapshot.retired_at = $now
                """,
                snapshot_id=old_snapshot_id,
                now=now,
            ).consume()
        outcome = (
            "CREATED"
            if current_snapshot is None
            else (
                "UPDATED"
                if state.get("active_version_id") != plan.version_id
                else "REPROCESSED"
            )
        )
        self._finish_job_tx(
            tx,
            plan.job_id,
            JobStatus.SUCCEEDED,
            outcome,
            lease_token,
            now,
        )
        self._advance_corpus_revision_tx(tx, plan.tenant_id, now)

    def _finish_existing_snapshot_tx(
        self,
        tx: Any,
        plan: IngestionPlan,
        lease_token: str,
        now: datetime,
    ) -> None:
        """Apply an explicit metadata/policy update or finish an exact replay."""
        self._assert_owned_job_tx(tx, plan.job_id, lease_token, now)
        self._lock_tenant_corpus_state_tx(tx, plan.tenant_id, now)
        state = self._locked_document_state_tx(tx, plan.tenant_id, plan.document_id)
        if int(state["generation"]) != plan.source_generation:
            raise IngestionConflict("document generation changed before replay")
        if _optional(state.get("active_snapshot_id")) != plan.snapshot.snapshot_id:
            raise IngestionConflict("active snapshot changed before replay")
        self._assert_active_source_identity_tx(tx, plan)
        stored = tx.run(
            """
            MATCH (snapshot:KnowledgeSnapshot {snapshot_id: $snapshot_id})
            RETURN snapshot.manifest_hash AS manifest_hash,
                   snapshot.expected_chunk_count AS expected_chunk_count
            """,
            snapshot_id=plan.snapshot.snapshot_id,
        ).single()
        if (
            stored is None
            or stored["manifest_hash"] != plan.snapshot.manifest_hash
            or int(stored["expected_chunk_count"]) != len(plan.bundles)
        ):
            raise IngestionConflict("active snapshot identity has different content")

        document = plan.bundles[0].document
        current_policy_version = int(state["access_policy_version"])
        if document.access_policy_version < current_policy_version:
            raise IngestionConflict("access policy version is stale")
        if document.access_policy_version == current_policy_version and (
            document.access_policy_id != state.get("access_policy_id")
            or sorted(document.access_groups)
            != sorted(state.get("access_groups") or [])
        ):
            raise IngestionConflict("access policy changed without a new version")
        changed = (
            document.title != state.get("title")
            or document.access_policy_version != current_policy_version
            or document.access_policy_id != state.get("access_policy_id")
            or sorted(document.access_groups)
            != sorted(state.get("access_groups") or [])
        )
        if not changed:
            self._finish_job_tx(
                tx,
                plan.job_id,
                JobStatus.NOOP,
                "UNCHANGED",
                lease_token,
                now,
            )
            return
        if plan.expected_active_snapshot_id != plan.snapshot.snapshot_id:
            raise IngestionConflict("active snapshot CAS failed for metadata update")

        tx.run(
            """
            MATCH (document:Document {tenant_id: $tenant_id, document_id: $document_id})
                  -[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot)
            SET document.title = $title,
                document.access_policy_id = $policy_id,
                document.access_policy_version = $policy_version,
                document.access_groups = $groups
            WITH snapshot
            MATCH (snapshot)-[:INCLUDES_CHUNK]->(chunk:Chunk)
            SET chunk.access_policy_id = $policy_id,
                chunk.access_policy_version = $policy_version,
                chunk.access_groups = $groups
            """,
            tenant_id=plan.tenant_id,
            document_id=plan.document_id,
            title=document.title,
            policy_id=document.access_policy_id,
            policy_version=document.access_policy_version,
            groups=sorted(document.access_groups),
        ).consume()
        self._finish_job_tx(
            tx,
            plan.job_id,
            JobStatus.SUCCEEDED,
            "METADATA_UPDATED",
            lease_token,
            now,
        )
        self._advance_corpus_revision_tx(tx, plan.tenant_id, now)

    @staticmethod
    def _assert_active_source_identity_tx(tx: Any, plan: IngestionPlan) -> None:
        """Reject stable IDs presented with different immutable source state."""
        document = plan.bundles[0].document
        version = plan.bundles[0].version
        document_identity = {
            "document_id": document.document_id,
            "tenant_id": document.tenant_id,
            "canonical_uri": document.canonical_uri,
            "source_name": document.source_name,
        }
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
        }
        record = tx.run(
            """
            MATCH (document:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })-[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot {
                snapshot_id: $snapshot_id
            })
            MATCH (document)-[:ACTIVE_VERSION]->(version:DocumentVersion {
                version_id: $version_id
            })
            WHERE EXISTS {
                MATCH (snapshot)-[:OF_VERSION]->(version)
            }
            RETURN all(
                       key IN keys($document_identity)
                       WHERE document[key] = $document_identity[key]
                   ) AS document_compatible,
                   all(
                       key IN keys($version_identity)
                       WHERE version[key] = $version_identity[key]
                   ) AS version_compatible,
                   CASE
                       WHEN $published_at IS NULL
                       THEN version.published_at IS NULL
                       ELSE version.published_at = $published_at
                   END AS publication_compatible
            """,
            tenant_id=plan.tenant_id,
            document_id=plan.document_id,
            snapshot_id=plan.snapshot.snapshot_id,
            version_id=plan.version_id,
            document_identity=document_identity,
            version_identity=version_identity,
            published_at=version.published_at,
        ).single()
        if (
            record is None
            or not record["document_compatible"]
            or not record["version_compatible"]
            or not record["publication_compatible"]
        ):
            raise IngestionConflict(
                "active source identity has different immutable document/version state"
            )

    def _assert_owned_job_tx(
        self,
        tx: Any,
        job_id: str,
        lease_token: str,
        now: datetime,
    ) -> None:
        expires_at = now + timedelta(seconds=self.lease_seconds)
        record = tx.run(
            """
            MATCH (job:IngestionJob {job_id: $job_id})
            SET job.__lease_write_lock = randomUUID()
            WITH job
            REMOVE job.__lease_write_lock
            RETURN job.status AS status,
                   job.lease_owner AS owner,
                   job.lease_token AS token,
                   job.lease_expires_at AS expires_at
            """,
            job_id=job_id,
        ).single()
        if record is None:
            raise KeyError(f"unknown ingestion job: {job_id}")
        lease_expiry = _native_datetime(record["expires_at"])
        if (
            record["status"] != JobStatus.RUNNING.value
            or record["owner"] != self.worker_id
            or record["token"] != lease_token
            or lease_expiry is None
            or lease_expiry <= now
        ):
            raise JobLeaseConflict("worker does not hold the active job lease")
        tx.run(
            """
            MATCH (job:IngestionJob {job_id: $job_id, lease_token: $lease_token})
            SET job.lease_expires_at = $expires_at, job.updated_at = $now
            """,
            job_id=job_id,
            lease_token=lease_token,
            expires_at=expires_at,
            now=now,
        ).consume()

    def _active_state(
        self,
        tenant_id: str,
        document_id: str,
    ) -> tuple[str | None, int]:
        with self.driver.session(database=self.database) as session:
            record = session.run(
                """
                OPTIONAL MATCH (document:Document {
                    tenant_id: $tenant_id,
                    document_id: $document_id
                })
                OPTIONAL MATCH (document)-[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot)
                OPTIONAL MATCH (tombstone:DocumentTombstone {
                    tenant_id: $tenant_id,
                    document_id: $document_id
                })
                RETURN snapshot.snapshot_id AS snapshot_id,
                       coalesce(document.generation, tombstone.generation, 0) AS generation
                """,
                tenant_id=tenant_id,
                document_id=document_id,
            ).single()
        return _optional(record["snapshot_id"]), int(record["generation"])

    @staticmethod
    def _locked_document_state_tx(
        tx: Any,
        tenant_id: str,
        document_id: str,
    ) -> dict[str, Any]:
        record = tx.run(
            """
            MATCH (document:Document {tenant_id: $tenant_id, document_id: $document_id})
            SET document.__publish_lock = randomUUID()
            WITH document
            REMOVE document.__publish_lock
            WITH document
            OPTIONAL MATCH (document)-[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot)
            OPTIONAL MATCH (document)-[:ACTIVE_VERSION]->(version:DocumentVersion)
            OPTIONAL MATCH (tombstone:DocumentTombstone {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            RETURN snapshot.snapshot_id AS active_snapshot_id,
                   version.version_id AS active_version_id,
                   coalesce(document.generation, tombstone.generation, 0) AS generation,
                   document.access_policy_id AS access_policy_id,
                   document.access_policy_version AS access_policy_version,
                   document.access_groups AS access_groups,
                   document.title AS title
            """,
            tenant_id=tenant_id,
            document_id=document_id,
        ).single()
        if record is None:
            raise IngestionConflict("staged document disappeared before publish")
        return dict(record)

    @staticmethod
    def _lock_tenant_corpus_state_tx(
        tx: Any,
        tenant_id: str,
        now: datetime,
    ) -> int:
        record = tx.run(
            """
            MERGE (state:TenantCorpusState {tenant_id: $tenant_id})
            ON CREATE SET state.corpus_revision = 0, state.created_at = $now
            SET state.__corpus_write_lock = randomUUID(),
                state.lifecycle_mode = 'MANAGED_INCREMENTAL'
            WITH state
            REMOVE state.__corpus_write_lock
            RETURN state.corpus_revision AS revision
            """,
            tenant_id=tenant_id,
            now=now,
        ).single()
        return int(record["revision"])

    @staticmethod
    def _assert_source_generation_tx(
        tx: Any,
        tenant_id: str,
        document_id: str,
        expected_generation: int,
    ) -> None:
        record = tx.run(
            """
            OPTIONAL MATCH (document:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            OPTIONAL MATCH (tombstone:DocumentTombstone {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            RETURN coalesce(
                document.generation,
                tombstone.generation,
                0
            ) AS generation
            """,
            tenant_id=tenant_id,
            document_id=document_id,
        ).single()
        if record is None or int(record["generation"]) != expected_generation:
            raise IngestionConflict(
                "document generation changed; stale work cannot write after deletion"
            )

    @staticmethod
    def _advance_corpus_revision_tx(tx: Any, tenant_id: str, now: datetime) -> None:
        """Advance corpus state and atomically invalidate a now-stale vector view."""
        tx.run(
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
            """,
            tenant_id=tenant_id,
            now=now,
        ).consume()

    def _finish_noop(self, job_id: str, outcome: str, lease_token: str) -> None:
        with self.driver.session(database=self.database) as session:
            session.execute_write(
                self._finish_job_tx,
                job_id,
                JobStatus.NOOP,
                outcome,
                lease_token,
                self.clock.now(),
            )

    @staticmethod
    def _finish_job_tx(
        tx: Any,
        job_id: str,
        status: JobStatus,
        outcome: str,
        lease_token: str,
        now: datetime,
    ) -> None:
        record = tx.run(
            """
            MATCH (job:IngestionJob {
                job_id: $job_id,
                status: $running,
                lease_token: $lease_token
            })
            SET job.status = $status,
                job.phase = $phase,
                job.outcome = $outcome,
                job.lease_owner = '',
                job.lease_token = '',
                job.lease_expires_at = null,
                job.finished_at = coalesce(job.finished_at, $now),
                job.updated_at = $now,
                job.last_error_code = ''
            RETURN job.job_id AS job_id
            """,
            job_id=job_id,
            running=JobStatus.RUNNING.value,
            lease_token=lease_token,
            status=status.value,
            phase=JobPhase.COMPLETE.value,
            outcome=outcome,
            now=now,
        ).single()
        if record is None:
            raise JobLeaseConflict("stale worker cannot finish ingestion job")

    def _record_failure(
        self,
        job_id: str,
        lease_token: str,
        error: Exception,
        *,
        retryable: bool,
    ) -> None:
        error_code = type(error).__name__[:80]
        with self.driver.session(database=self.database) as session:
            session.execute_write(
                self._record_failure_tx,
                job_id,
                lease_token,
                error_code,
                retryable,
                self.clock.now(),
            )

    @staticmethod
    def _record_failure_tx(
        tx: Any,
        job_id: str,
        lease_token: str,
        error_code: str,
        retryable: bool,
        now: datetime,
    ) -> None:
        record = tx.run(
            "MATCH (job:IngestionJob {job_id: $job_id, lease_token: $lease_token}) "
            "RETURN job{.*} AS job",
            job_id=job_id,
            lease_token=lease_token,
        ).single()
        if record is None:
            return
        job = _job_view(record["job"])
        if job.status in {JobStatus.SUCCEEDED, JobStatus.NOOP}:
            return
        final = not retryable or job.attempts >= job.max_attempts
        tx.run(
            """
            MATCH (job:IngestionJob {
                job_id: $job_id,
                status: $running,
                lease_token: $lease_token
            })
            SET job.status = $status,
                job.last_error_code = $error_code,
                job.lease_owner = '',
                job.lease_token = '',
                job.lease_expires_at = null,
                job.updated_at = $now,
                job.retryable = $retryable
            """,
            job_id=job_id,
            running=JobStatus.RUNNING.value,
            lease_token=lease_token,
            status=(
                JobStatus.FAILED_PERMANENT.value
                if final
                else JobStatus.RETRY_WAIT.value
            ),
            error_code=error_code,
            now=now,
            retryable=not final,
        ).consume()

    def _result_for_job(self, job_id: str) -> IngestionResult:
        job = self.get_job(job_id)
        active_snapshot, _ = self._active_state(job.tenant_id, job.document_id)
        return IngestionResult(
            job=job,
            snapshot_id=job.target_snapshot_id,
            active_snapshot_id=active_snapshot,
        )

    def _delete_document_tx(
        self,
        tx: Any,
        job_id: str,
        lease_token: str,
        tenant_id: str,
        document_id: str,
        expected_active_snapshot_id: str | None,
        source_generation: int,
        now: datetime,
    ) -> None:
        self._assert_owned_job_tx(tx, job_id, lease_token, now)
        self._lock_tenant_corpus_state_tx(tx, tenant_id, now)
        record = tx.run(
            """
            OPTIONAL MATCH (document:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            OPTIONAL MATCH (document)-[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot)
            OPTIONAL MATCH (tombstone:DocumentTombstone {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            OPTIONAL MATCH (work_job:IngestionJob {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            WHERE work_job.operation IN ['UPSERT', 'PREPARE_UPSERT']
            OPTIONAL MATCH (work_job)-[:HAS_TASK]->(work_task:IngestionTask)
            OPTIONAL MATCH (staged_snapshot:KnowledgeSnapshot {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            RETURN document IS NOT NULL AS exists,
                   snapshot.snapshot_id AS active_snapshot_id,
                   coalesce(document.generation, tombstone.generation, 0) AS generation,
                   count(DISTINCT staged_snapshot) > 0
                   OR count(DISTINCT work_task) > 0
                   OR count(DISTINCT CASE
                       WHEN work_job.status IN ['QUEUED', 'RUNNING', 'RETRY_WAIT']
                        AND work_job.source_generation = coalesce(
                            document.generation,
                            tombstone.generation,
                            0
                        )
                       THEN work_job
                   END) > 0 AS registered_work
            """,
            tenant_id=tenant_id,
            document_id=document_id,
        ).single()
        document_exists = bool(record["exists"])
        registered_work = bool(record["registered_work"])
        if not document_exists and not registered_work:
            self._finish_job_tx(
                tx,
                job_id,
                JobStatus.NOOP,
                "ALREADY_ABSENT",
                lease_token,
                now,
            )
            return
        if document_exists:
            state = self._locked_document_state_tx(tx, tenant_id, document_id)
        else:
            state = {
                "generation": int(record["generation"]),
                "active_snapshot_id": None,
            }
        if int(state["generation"]) != source_generation:
            raise IngestionConflict("document generation changed before deletion")
        active = _optional(state.get("active_snapshot_id"))
        if active != expected_active_snapshot_id:
            raise IngestionConflict("active snapshot CAS failed for deletion")

        next_generation = source_generation + 1
        tx.run(
            """
            MERGE (tombstone:DocumentTombstone {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            SET tombstone.generation = $generation,
                tombstone.deleted_at = $now,
                tombstone.deleted_by_job_id = $job_id
            """,
            tenant_id=tenant_id,
            document_id=document_id,
            generation=next_generation,
            now=now,
            job_id=job_id,
        ).consume()

        # Every destructive query is tenant/document scoped and remains in this
        # transaction. A late error restores both the active pointer and data.
        deletion_queries = (
            """
            MATCH (:Document {tenant_id: $tenant_id, document_id: $document_id})
                  -[:HAS_VERSION]->(:DocumentVersion)-[:HAS_CHUNK]->(chunk:Chunk)
                  <-[:EVIDENCED_BY]-(assertion:Assertion)
            DETACH DELETE assertion
            """,
            """
            MATCH (:Document {tenant_id: $tenant_id, document_id: $document_id})
                  -[:HAS_VERSION]->(:DocumentVersion)-[:HAS_CHUNK]->(chunk:Chunk)
                  <-[:IN_CHUNK]-(mention:EntityMention)
            DETACH DELETE mention
            """,
            """
            MATCH (:Document {tenant_id: $tenant_id, document_id: $document_id})
                  -[:HAS_VERSION]->(:DocumentVersion)-[:HAS_CHUNK]->(chunk:Chunk)
                  -[:HAS_EMBEDDING]->(embedding:ChunkEmbedding)
            DETACH DELETE embedding
            """,
            """
            MATCH (document:Document {tenant_id: $tenant_id, document_id: $document_id})
                  -[:HAS_VERSION]->(version:DocumentVersion)-[:HAS_CHUNK]->(chunk:Chunk)
            DETACH DELETE chunk
            """,
            """
            MATCH (document:Document {tenant_id: $tenant_id, document_id: $document_id})
                  -[:HAS_VERSION]->(version:DocumentVersion)
            DETACH DELETE version
            """,
            """
            MATCH (snapshot:KnowledgeSnapshot {
                tenant_id: $tenant_id,
                document_id: $document_id
            })
            DETACH DELETE snapshot
            """,
            """
            MATCH (document:Document {tenant_id: $tenant_id, document_id: $document_id})
            DETACH DELETE document
            """,
        )
        for query in deletion_queries:
            tx.run(query, tenant_id=tenant_id, document_id=document_id).consume()
        tx.run(
            """
            MATCH (job:IngestionJob {
                tenant_id: $tenant_id,
                document_id: $document_id
            })-[:HAS_TASK]->(task:IngestionTask)
            WHERE job.operation IN ['UPSERT', 'PREPARE_UPSERT']
            DETACH DELETE task
            """,
            tenant_id=tenant_id,
            document_id=document_id,
        ).consume()
        tx.run(
            """
            MATCH (artifact:DerivationArtifact {tenant_id: $tenant_id})
            WHERE NOT (:IngestionTask)-[:USED_ARTIFACT]->(artifact)
            DETACH DELETE artifact
            """,
            tenant_id=tenant_id,
        ).consume()
        tx.run(
            """
            MATCH (entity:Entity {tenant_id: $tenant_id})
            WHERE NOT EXISTS {
                MATCH (:EntityMention)-[:REFERS_TO]->(entity)
            }
              AND NOT EXISTS {
                MATCH (:Assertion)-[:SUBJECT]->(entity)
            }
              AND NOT EXISTS {
                MATCH (:Assertion)-[:OBJECT]->(entity)
            }
            DETACH DELETE entity
            """,
            tenant_id=tenant_id,
        ).consume()
        if document_exists:
            self._advance_corpus_revision_tx(tx, tenant_id, now)
        self._finish_job_tx(
            tx,
            job_id,
            JobStatus.SUCCEEDED,
            "DELETED",
            lease_token,
            now,
        )
