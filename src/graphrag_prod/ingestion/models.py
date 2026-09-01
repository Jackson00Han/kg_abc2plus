"""Immutable request and status records for resumable ingestion."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from graphrag_prod.domain.ids import (
    content_checksum,
    ingestion_job_id,
    knowledge_snapshot_id,
)
from graphrag_prod.domain.models import Chunk, GraphPipelineProfile, KnowledgeSnapshot
from graphrag_prod.graph.governance import (
    GovernanceFinding,
    GraphGovernancePolicy,
)
from graphrag_prod.graph.provenance import ProvenanceBundle


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    SUCCEEDED = "SUCCEEDED"
    NOOP = "NOOP"
    FAILED_PERMANENT = "FAILED_PERMANENT"


class JobPhase(StrEnum):
    PLAN = "PLAN"
    STAGE = "STAGE"
    VERIFY = "VERIFY"
    PUBLISH = "PUBLISH"
    CLEANUP = "CLEANUP"
    COMPLETE = "COMPLETE"


class Checkpoint(StrEnum):
    AFTER_JOB_CLAIM = "AFTER_JOB_CLAIM"
    AFTER_SNAPSHOT_STAGE = "AFTER_SNAPSHOT_STAGE"
    AFTER_CHUNK_STAGE = "AFTER_CHUNK_STAGE"
    BEFORE_VERIFY = "BEFORE_VERIFY"
    BEFORE_PUBLISH = "BEFORE_PUBLISH"
    AFTER_PUBLISH = "AFTER_PUBLISH"
    BEFORE_DELETE = "BEFORE_DELETE"
    AFTER_DELETE = "AFTER_DELETE"
    BEFORE_EMBEDDING_SWITCH = "BEFORE_EMBEDDING_SWITCH"
    AFTER_EMBEDDING_SWITCH = "AFTER_EMBEDDING_SWITCH"
    AFTER_EMBEDDING_MEMBERSHIP_CHECK = "AFTER_EMBEDDING_MEMBERSHIP_CHECK"


def _required(value: str, name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {
            field.name: _jsonable(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        converted = [_jsonable(item) for item in value]
        return sorted(converted, key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in sorted(value.items())}
    if isinstance(value, StrEnum):
        return value.value
    return value


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def snapshot_manifest(bundles: tuple[ProvenanceBundle, ...]) -> tuple[dict[str, Any], ...]:
    """Return the exact graph-output projection used to seal a snapshot.

    Stable IDs already cover most immutable identity inputs.  The manifest
    additionally seals output fields that are deliberately absent from those
    IDs, so an active-snapshot fast path cannot mistake changed derivation or
    source-location state for an exact replay.  Embeddings remain outside this
    graph manifest because vector generations have an independent lifecycle.
    """
    return tuple(
        sorted(
            (
                {
                    "chunk_id": bundle.chunk.chunk_id,
                    "page_number": bundle.chunk.page_number,
                    "section": bundle.chunk.section,
                    "entities": sorted(
                        (
                            {
                                "entity_id": entity.entity_id,
                                "canonical_name": entity.canonical_name,
                                "aliases": list(entity.aliases),
                            }
                            for entity in bundle.entities
                        ),
                        key=lambda item: item["entity_id"],
                    ),
                    "mentions": sorted(
                        (
                            {
                                "mention_id": mention.mention_id,
                                "entity_id": mention.entity_id,
                                "confidence": mention.confidence,
                            }
                            for mention in bundle.mentions
                        ),
                        key=lambda item: item["mention_id"],
                    ),
                    "assertions": sorted(
                        (
                            {
                                "assertion_id": assertion.assertion_id,
                                "confidence": assertion.confidence,
                                "accepted": assertion.accepted,
                            }
                            for assertion in bundle.all_assertions
                        ),
                        key=lambda item: item["assertion_id"],
                    ),
                }
                for bundle in bundles
            ),
            key=lambda item: item["chunk_id"],
        )
    )


@dataclass(frozen=True, slots=True)
class IngestionPlan:
    operation_key: str
    profile: GraphPipelineProfile
    governance_policy: GraphGovernancePolicy
    snapshot: KnowledgeSnapshot
    bundles: tuple[ProvenanceBundle, ...]
    governance_findings: tuple[GovernanceFinding, ...]
    expected_active_snapshot_id: str | None
    source_generation: int
    artifact_input_hashes: tuple[tuple[str, str], ...]
    max_attempts: int = 3

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation_key", _required(self.operation_key, "operation_key"))
        if not self.bundles:
            raise ValueError("ingestion plan requires at least one chunk bundle")
        if self.source_generation < 0:
            raise ValueError("source_generation must not be negative")
        if self.max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if self.governance_policy.policy_id != self.profile.schema_signature:
            raise ValueError(
                "governance policy_id must match the pipeline schema signature"
            )

        documents = {bundle.document.document_id for bundle in self.bundles}
        versions = {bundle.version.version_id for bundle in self.bundles}
        tenants = {bundle.document.tenant_id for bundle in self.bundles}
        chunks = {bundle.chunk.chunk_id for bundle in self.bundles}
        if len(documents) != 1 or len(versions) != 1 or len(tenants) != 1:
            raise ValueError("all ingestion bundles must share one tenant/document/version")
        if len(chunks) != len(self.bundles):
            raise ValueError("ingestion plan contains duplicate chunk bundles")
        first = self.bundles[0]
        if any(bundle.document != first.document for bundle in self.bundles[1:]):
            raise ValueError("all ingestion bundles must share identical document state")
        if any(bundle.version != first.version for bundle in self.bundles[1:]):
            raise ValueError("all ingestion bundles must share identical version state")
        ordinals = [bundle.chunk.ordinal for bundle in self.bundles]
        if len(set(ordinals)) != len(ordinals):
            raise ValueError("ingestion plan contains duplicate chunk ordinals")
        entity_state: dict[str, Any] = {}
        for bundle in self.bundles:
            for entity in bundle.entities:
                previous = entity_state.setdefault(entity.entity_id, entity)
                if previous != entity:
                    raise ValueError(
                        "ingestion plan contains conflicting shared entity state"
                    )
        if any(bundle.activate_version for bundle in self.bundles):
            raise ValueError("staging bundles must not activate a version")
        if self.snapshot.document_id not in documents:
            raise ValueError("snapshot document does not match ingestion bundles")
        if self.snapshot.version_id not in versions:
            raise ValueError("snapshot version does not match ingestion bundles")
        if self.snapshot.tenant_id not in tenants:
            raise ValueError("snapshot tenant does not match ingestion bundles")
        if self.snapshot.profile_id != self.profile.profile_id:
            raise ValueError("snapshot profile does not match ingestion profile")
        if self.snapshot.expected_chunk_count != len(self.bundles):
            raise ValueError("snapshot expected_chunk_count does not match bundles")
        if self.snapshot.manifest_hash != _fingerprint(snapshot_manifest(self.bundles)):
            raise ValueError("snapshot manifest_hash does not match bundles")
        if any(
            bundle.chunk.splitter_version != self.profile.splitter_signature
            for bundle in self.bundles
        ):
            raise ValueError("chunk splitter does not match graph pipeline profile")
        if any(
            mention.extractor_version != self.profile.extractor_signature
            for bundle in self.bundles
            for mention in bundle.mentions
        ):
            raise ValueError("mention extractor does not match graph pipeline profile")
        if any(
            assertion.extractor_version != self.profile.extractor_signature
            or assertion.schema_version != self.profile.schema_signature
            for bundle in self.bundles
            for assertion in bundle.all_assertions
        ):
            raise ValueError("assertion derivation does not match graph pipeline profile")
        for bundle in self.bundles:
            governed = self.governance_policy.govern_bundle(bundle)
            if governed.bundle != bundle:
                raise ValueError("ingestion bundles must be governed before planning")

        artifact_hashes = dict(self.artifact_input_hashes)
        if len(artifact_hashes) != len(self.artifact_input_hashes):
            raise ValueError("duplicate artifact input entries")
        if set(artifact_hashes) != chunks:
            raise ValueError("every chunk requires one complete artifact input hash")
        for input_hash in artifact_hashes.values():
            if len(input_hash) != 64 or any(
                character not in "0123456789abcdef" for character in input_hash
            ):
                raise ValueError("artifact input hashes must be lowercase SHA-256")

    @property
    def tenant_id(self) -> str:
        return self.snapshot.tenant_id

    @property
    def document_id(self) -> str:
        return self.snapshot.document_id

    @property
    def version_id(self) -> str:
        return self.snapshot.version_id

    @property
    def job_id(self) -> str:
        return ingestion_job_id(self.tenant_id, "UPSERT", self.operation_key)

    @property
    def request_fingerprint(self) -> str:
        return _fingerprint(
            {
                "operation": "UPSERT",
                "profile": self.profile,
                "governance_policy": self.governance_policy,
                "snapshot": self.snapshot,
                "bundles": self.bundles,
                "governance_findings": self.governance_findings,
                "expected_active_snapshot_id": self.expected_active_snapshot_id,
                "source_generation": self.source_generation,
                "artifact_input_hashes": self.artifact_input_hashes,
            }
        )

    @classmethod
    def build(
        cls,
        *,
        operation_key: str,
        profile: GraphPipelineProfile,
        governance_policy: GraphGovernancePolicy,
        bundles: tuple[ProvenanceBundle, ...],
        expected_active_snapshot_id: str | None,
        source_generation: int,
        artifact_input_hashes: dict[str, str],
        created_at: datetime,
        max_attempts: int = 3,
    ) -> IngestionPlan:
        if not bundles:
            raise ValueError("ingestion plan requires at least one chunk bundle")
        if governance_policy.policy_id != profile.schema_signature:
            raise ValueError(
                "governance policy_id must match the pipeline schema signature"
            )
        governed = tuple(governance_policy.govern_bundle(bundle) for bundle in bundles)
        bundles = tuple(result.bundle for result in governed)
        findings = tuple(
            finding for result in governed for finding in result.findings
        )
        first = bundles[0]
        manifest_hash = _fingerprint(snapshot_manifest(bundles))
        snapshot_identifier = knowledge_snapshot_id(
            first.version.version_id,
            profile.profile_id,
        )
        snapshot = KnowledgeSnapshot(
            snapshot_id=snapshot_identifier,
            tenant_id=first.document.tenant_id,
            document_id=first.document.document_id,
            version_id=first.version.version_id,
            profile_id=profile.profile_id,
            manifest_hash=manifest_hash,
            expected_chunk_count=len(bundles),
            created_at=created_at,
        )
        return cls(
            operation_key=operation_key,
            profile=profile,
            governance_policy=governance_policy,
            snapshot=snapshot,
            bundles=bundles,
            governance_findings=findings,
            expected_active_snapshot_id=expected_active_snapshot_id,
            source_generation=source_generation,
            artifact_input_hashes=tuple(sorted(artifact_input_hashes.items())),
            max_attempts=max_attempts,
        )


@dataclass(frozen=True, slots=True)
class JobView:
    job_id: str
    tenant_id: str
    operation: str
    operation_key: str
    request_fingerprint: str
    status: JobStatus
    phase: JobPhase
    document_id: str
    target_version_id: str | None
    target_snapshot_id: str | None
    expected_active_snapshot_id: str | None
    source_generation: int
    attempts: int
    max_attempts: int
    completed_tasks: int
    expected_tasks: int
    lease_owner: str | None
    lease_token: str | None
    lease_expires_at: datetime | None
    outcome: str | None
    last_error_code: str | None


@dataclass(frozen=True, slots=True)
class IngestionResult:
    job: JobView
    snapshot_id: str | None
    active_snapshot_id: str | None


def default_artifact_input_hash(bundle: ProvenanceBundle) -> str:
    """Hash the complete configured provider input for simple isolated chunks."""
    return chunk_artifact_input_hash(bundle.chunk)


def chunk_artifact_input_hash(chunk: Chunk) -> str:
    """Hash all source fields supplied to a chunk-scoped derivation provider."""
    return content_checksum(
        json.dumps(
            {
                "text": chunk.text,
                "char_start": chunk.char_start,
                "char_end": chunk.char_end,
                "page_number": chunk.page_number,
                "section": chunk.section,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
