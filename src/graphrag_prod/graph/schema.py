"""Idempotent Neo4j schema migration and structural verification."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


MIGRATIONS_DIRECTORY = Path(__file__).with_name("migrations")
# Backwards-compatible handle for callers that need the original migration only.
MIGRATION_PATH = MIGRATIONS_DIRECTORY / "001_provenance_schema.cypher"


class QueryDriver(Protocol):
    def execute_query(self, query_: str, **kwargs: object) -> tuple[Any, Any, Any]: ...


@dataclass(frozen=True, slots=True)
class SchemaExpectation:
    name: str
    kind: str
    schema_type: str
    label: str
    properties: tuple[str, ...]


EXPECTED_SCHEMA = (
    SchemaExpectation(
        "document_id_unique",
        "constraint",
        "UNIQUENESS",
        "Document",
        ("document_id",),
    ),
    SchemaExpectation(
        "document_identity_unique",
        "constraint",
        "UNIQUENESS",
        "Document",
        ("tenant_id", "canonical_uri"),
    ),
    SchemaExpectation(
        "document_version_id_unique",
        "constraint",
        "UNIQUENESS",
        "DocumentVersion",
        ("version_id",),
    ),
    SchemaExpectation(
        "document_version_content_unique",
        "constraint",
        "UNIQUENESS",
        "DocumentVersion",
        ("document_id", "checksum", "original_checksum"),
    ),
    SchemaExpectation(
        "document_version_number_unique",
        "constraint",
        "UNIQUENESS",
        "DocumentVersion",
        ("document_id", "version_number"),
    ),
    SchemaExpectation(
        "chunk_id_unique",
        "constraint",
        "UNIQUENESS",
        "Chunk",
        ("chunk_id",),
    ),
    SchemaExpectation(
        "chunk_splitter_ordinal_unique",
        "constraint",
        "UNIQUENESS",
        "Chunk",
        ("version_id", "splitter_version", "ordinal"),
    ),
    SchemaExpectation(
        "chunk_embedding_id_unique",
        "constraint",
        "UNIQUENESS",
        "ChunkEmbedding",
        ("embedding_id",),
    ),
    SchemaExpectation(
        "chunk_embedding_space_unique",
        "constraint",
        "UNIQUENESS",
        "ChunkEmbedding",
        ("chunk_id", "embedding_space_id"),
    ),
    SchemaExpectation(
        "entity_id_unique",
        "constraint",
        "UNIQUENESS",
        "Entity",
        ("entity_id",),
    ),
    SchemaExpectation(
        "entity_identity_unique",
        "constraint",
        "UNIQUENESS",
        "Entity",
        ("tenant_id", "entity_type", "canonical_key"),
    ),
    SchemaExpectation(
        "entity_mention_id_unique",
        "constraint",
        "UNIQUENESS",
        "EntityMention",
        ("mention_id",),
    ),
    SchemaExpectation(
        "assertion_id_unique",
        "constraint",
        "UNIQUENESS",
        "Assertion",
        ("assertion_id",),
    ),
    SchemaExpectation(
        "relationship_property_value_id_unique",
        "constraint",
        "UNIQUENESS",
        "RelationshipPropertyValue",
        ("property_value_id",),
    ),
    SchemaExpectation(
        "relationship_property_value_access_lookup",
        "index",
        "RANGE",
        "RelationshipPropertyValue",
        ("tenant_id", "evidence_chunk_id"),
    ),
    SchemaExpectation(
        "graph_pipeline_profile_id_unique",
        "constraint",
        "UNIQUENESS",
        "GraphPipelineProfile",
        ("profile_id",),
    ),
    SchemaExpectation(
        "graph_pipeline_profile_identity_unique",
        "constraint",
        "UNIQUENESS",
        "GraphPipelineProfile",
        (
            "normalizer_signature",
            "splitter_signature",
            "extractor_signature",
            "prompt_signature",
            "schema_signature",
            "code_signature",
        ),
    ),
    SchemaExpectation(
        "knowledge_snapshot_id_unique",
        "constraint",
        "UNIQUENESS",
        "KnowledgeSnapshot",
        ("snapshot_id",),
    ),
    SchemaExpectation(
        "knowledge_snapshot_identity_unique",
        "constraint",
        "UNIQUENESS",
        "KnowledgeSnapshot",
        ("version_id", "profile_id"),
    ),
    SchemaExpectation(
        "ingestion_job_id_unique",
        "constraint",
        "UNIQUENESS",
        "IngestionJob",
        ("job_id",),
    ),
    SchemaExpectation(
        "ingestion_job_idempotency_unique",
        "constraint",
        "UNIQUENESS",
        "IngestionJob",
        ("tenant_id", "operation", "idempotency_key"),
    ),
    SchemaExpectation(
        "ingestion_task_id_unique",
        "constraint",
        "UNIQUENESS",
        "IngestionTask",
        ("task_id",),
    ),
    SchemaExpectation(
        "ingestion_task_identity_unique",
        "constraint",
        "UNIQUENESS",
        "IngestionTask",
        ("job_id", "chunk_id"),
    ),
    SchemaExpectation(
        "derivation_artifact_id_unique",
        "constraint",
        "UNIQUENESS",
        "DerivationArtifact",
        ("artifact_id",),
    ),
    SchemaExpectation(
        "derivation_artifact_identity_unique",
        "constraint",
        "UNIQUENESS",
        "DerivationArtifact",
        ("tenant_id", "kind", "input_hash", "profile_id"),
    ),
    SchemaExpectation(
        "embedding_index_generation_id_unique",
        "constraint",
        "UNIQUENESS",
        "EmbeddingIndexGeneration",
        ("generation_id",),
    ),
    SchemaExpectation(
        "embedding_index_generation_identity_unique",
        "constraint",
        "UNIQUENESS",
        "EmbeddingIndexGeneration",
        ("tenant_id", "embedding_space_id", "generation_version"),
    ),
    SchemaExpectation(
        "tenant_corpus_state_tenant_unique",
        "constraint",
        "UNIQUENESS",
        "TenantCorpusState",
        ("tenant_id",),
    ),
    SchemaExpectation(
        "document_tombstone_identity_unique",
        "constraint",
        "UNIQUENESS",
        "DocumentTombstone",
        ("tenant_id", "document_id"),
    ),
    SchemaExpectation(
        "graph_governance_policy_id_unique",
        "constraint",
        "UNIQUENESS",
        "GraphGovernancePolicy",
        ("policy_id",),
    ),
    SchemaExpectation(
        "graph_quality_run_id_unique",
        "constraint",
        "UNIQUENESS",
        "GraphQualityRun",
        ("run_id",),
    ),
    SchemaExpectation(
        "graph_quality_issue_id_unique",
        "constraint",
        "UNIQUENESS",
        "GraphQualityIssue",
        ("issue_id",),
    ),
    SchemaExpectation(
        "graph_review_decision_id_unique",
        "constraint",
        "UNIQUENESS",
        "GraphReviewDecision",
        ("decision_id",),
    ),
    SchemaExpectation(
        "graph_governance_finding_id_unique",
        "constraint",
        "UNIQUENESS",
        "GraphGovernanceFinding",
        ("finding_id",),
    ),
    SchemaExpectation(
        "entity_resolution_decision_id_unique",
        "constraint",
        "UNIQUENESS",
        "EntityResolutionDecision",
        ("decision_id",),
    ),
    SchemaExpectation(
        "tbox_catalog_identity_unique",
        "constraint",
        "UNIQUENESS",
        "TBoxCatalog",
        ("tenant_id", "key"),
    ),
    SchemaExpectation(
        "tbox_version_id_unique",
        "constraint",
        "UNIQUENESS",
        "TBoxVersion",
        ("tbox_id",),
    ),
    SchemaExpectation(
        "tbox_version_identity_unique",
        "constraint",
        "UNIQUENESS",
        "TBoxVersion",
        ("tenant_id", "key", "version"),
    ),
    SchemaExpectation(
        "tbox_entity_type_id_unique",
        "constraint",
        "UNIQUENESS",
        "TBoxEntityType",
        ("entity_type_id",),
    ),
    SchemaExpectation(
        "tbox_entity_type_identity_unique",
        "constraint",
        "UNIQUENESS",
        "TBoxEntityType",
        ("tbox_id", "name"),
    ),
    SchemaExpectation(
        "tbox_relationship_type_id_unique",
        "constraint",
        "UNIQUENESS",
        "TBoxRelationshipType",
        ("relationship_type_id",),
    ),
    SchemaExpectation(
        "tbox_relationship_type_identity_unique",
        "constraint",
        "UNIQUENESS",
        "TBoxRelationshipType",
        ("tbox_id", "name"),
    ),
    SchemaExpectation(
        "tbox_property_definition_id_unique",
        "constraint",
        "UNIQUENESS",
        "TBoxPropertyDefinition",
        ("property_definition_id",),
    ),
    SchemaExpectation(
        "tbox_property_definition_identity_unique",
        "constraint",
        "UNIQUENESS",
        "TBoxPropertyDefinition",
        ("tbox_id", "owner_kind", "owner_name", "name"),
    ),
    SchemaExpectation(
        "knowledge_record_head_id_unique",
        "constraint",
        "UNIQUENESS",
        "KnowledgeRecordHead",
        ("record_id",),
    ),
    SchemaExpectation(
        "governed_entity_mention_revision_id_unique",
        "constraint",
        "UNIQUENESS",
        "GovernedEntityMentionRevision",
        ("revision_id",),
    ),
    SchemaExpectation(
        "governed_entity_mention_revision_identity_unique",
        "constraint",
        "UNIQUENESS",
        "GovernedEntityMentionRevision",
        ("tenant_id", "record_id", "revision"),
    ),
    SchemaExpectation(
        "governed_assertion_revision_id_unique",
        "constraint",
        "UNIQUENESS",
        "GovernedAssertionRevision",
        ("revision_id",),
    ),
    SchemaExpectation(
        "governed_assertion_revision_identity_unique",
        "constraint",
        "UNIQUENESS",
        "GovernedAssertionRevision",
        ("tenant_id", "record_id", "revision"),
    ),
    SchemaExpectation(
        "knowledge_publication_state_tenant_unique",
        "constraint",
        "UNIQUENESS",
        "KnowledgePublicationState",
        ("tenant_id",),
    ),
    SchemaExpectation(
        "knowledge_publication_id_unique",
        "constraint",
        "UNIQUENESS",
        "KnowledgePublication",
        ("publication_id",),
    ),
    SchemaExpectation(
        "knowledge_publication_identity_unique",
        "constraint",
        "UNIQUENESS",
        "KnowledgePublication",
        ("tenant_id", "generation"),
    ),
    SchemaExpectation(
        "knowledge_publication_activation_id_unique",
        "constraint",
        "UNIQUENESS",
        "KnowledgePublicationActivation",
        ("activation_id",),
    ),
    SchemaExpectation(
        "knowledge_publication_activation_identity_unique",
        "constraint",
        "UNIQUENESS",
        "KnowledgePublicationActivation",
        ("tenant_id", "activation_generation"),
    ),
    SchemaExpectation(
        "knowledge_construction_job_id_unique",
        "constraint",
        "UNIQUENESS",
        "KnowledgeConstructionJob",
        ("job_id",),
    ),
    SchemaExpectation(
        "knowledge_construction_job_operation_unique",
        "constraint",
        "UNIQUENESS",
        "KnowledgeConstructionJob",
        ("tenant_id", "operation_key"),
    ),
    SchemaExpectation(
        "knowledge_construction_outcome_id_unique",
        "constraint",
        "UNIQUENESS",
        "KnowledgeConstructionChunkOutcome",
        ("outcome_id",),
    ),
    SchemaExpectation(
        "knowledge_construction_outcome_identity_unique",
        "constraint",
        "UNIQUENESS",
        "KnowledgeConstructionChunkOutcome",
        ("tenant_id", "job_id", "chunk_id"),
    ),
    SchemaExpectation(
        "published_graph_quality_run_id_unique",
        "constraint",
        "UNIQUENESS",
        "PublishedGraphQualityRun",
        ("run_id",),
    ),
    SchemaExpectation(
        "published_graph_quality_issue_id_unique",
        "constraint",
        "UNIQUENESS",
        "PublishedGraphQualityIssue",
        ("issue_id",),
    ),
    SchemaExpectation(
        "published_graph_quality_sample_id_unique",
        "constraint",
        "UNIQUENESS",
        "PublishedGraphQualityReviewSample",
        ("sample_id",),
    ),
    SchemaExpectation(
        "published_graph_quality_acl_requirement_id_unique",
        "constraint",
        "UNIQUENESS",
        "PublishedGraphQualityAclRequirement",
        ("requirement_id",),
    ),
    SchemaExpectation(
        "document_tenant_id",
        "index",
        "RANGE",
        "Document",
        ("tenant_id",),
    ),
    SchemaExpectation(
        "version_document_lookup",
        "index",
        "RANGE",
        "DocumentVersion",
        ("tenant_id", "document_id"),
    ),
    SchemaExpectation(
        "chunk_access_lookup",
        "index",
        "RANGE",
        "Chunk",
        ("tenant_id", "version_id"),
    ),
    SchemaExpectation(
        "embedding_space_lookup",
        "index",
        "RANGE",
        "ChunkEmbedding",
        ("tenant_id", "embedding_space_id"),
    ),
    SchemaExpectation(
        "entity_tenant_type",
        "index",
        "RANGE",
        "Entity",
        ("tenant_id", "entity_type"),
    ),
    SchemaExpectation(
        "assertion_access_lookup",
        "index",
        "RANGE",
        "Assertion",
        ("tenant_id", "accepted"),
    ),
    SchemaExpectation(
        "knowledge_snapshot_document_lookup",
        "index",
        "RANGE",
        "KnowledgeSnapshot",
        ("tenant_id", "document_id"),
    ),
    SchemaExpectation(
        "ingestion_job_status_lookup",
        "index",
        "RANGE",
        "IngestionJob",
        ("tenant_id", "status", "next_retry_at"),
    ),
    SchemaExpectation(
        "ingestion_job_lease_lookup",
        "index",
        "RANGE",
        "IngestionJob",
        ("status", "lease_expires_at"),
    ),
    SchemaExpectation(
        "ingestion_task_status_lookup",
        "index",
        "RANGE",
        "IngestionTask",
        ("job_id", "status"),
    ),
    SchemaExpectation(
        "embedding_generation_state_lookup",
        "index",
        "RANGE",
        "EmbeddingIndexGeneration",
        ("tenant_id", "embedding_space_id", "state"),
    ),
    SchemaExpectation(
        "document_tombstone_generation_lookup",
        "index",
        "RANGE",
        "DocumentTombstone",
        ("tenant_id", "document_id", "generation"),
    ),
    SchemaExpectation(
        "entity_governance_status_lookup",
        "index",
        "RANGE",
        "Entity",
        ("tenant_id", "governance_status"),
    ),
    SchemaExpectation(
        "assertion_governance_status_lookup",
        "index",
        "RANGE",
        "Assertion",
        ("tenant_id", "governance_status"),
    ),
    SchemaExpectation(
        "graph_quality_run_lookup",
        "index",
        "RANGE",
        "GraphQualityRun",
        ("tenant_id", "policy_id", "corpus_revision"),
    ),
    SchemaExpectation(
        "graph_review_target_lookup",
        "index",
        "RANGE",
        "GraphReviewDecision",
        ("tenant_id", "target_kind", "target_id"),
    ),
    SchemaExpectation(
        "entity_resolution_decision_lookup",
        "index",
        "RANGE",
        "EntityResolutionDecision",
        ("tenant_id", "outcome", "rule_id"),
    ),
    SchemaExpectation(
        "tbox_version_tenant_status_lookup",
        "index",
        "RANGE",
        "TBoxVersion",
        ("tenant_id", "key", "status"),
    ),
    SchemaExpectation(
        "tbox_entity_type_tenant_lookup",
        "index",
        "RANGE",
        "TBoxEntityType",
        ("tenant_id", "tbox_id"),
    ),
    SchemaExpectation(
        "tbox_relationship_type_tenant_lookup",
        "index",
        "RANGE",
        "TBoxRelationshipType",
        ("tenant_id", "tbox_id"),
    ),
    SchemaExpectation(
        "knowledge_record_head_lookup",
        "index",
        "RANGE",
        "KnowledgeRecordHead",
        ("tenant_id", "record_kind", "current_revision"),
    ),
    SchemaExpectation(
        "governed_entity_mention_review_lookup",
        "index",
        "RANGE",
        "GovernedEntityMentionRevision",
        ("tenant_id", "governance_status", "authority_level"),
    ),
    SchemaExpectation(
        "governed_entity_mention_evidence_lookup",
        "index",
        "RANGE",
        "GovernedEntityMentionRevision",
        ("tenant_id", "chunk_id"),
    ),
    SchemaExpectation(
        "governed_assertion_review_lookup",
        "index",
        "RANGE",
        "GovernedAssertionRevision",
        ("tenant_id", "governance_status", "authority_level"),
    ),
    SchemaExpectation(
        "governed_assertion_evidence_lookup",
        "index",
        "RANGE",
        "GovernedAssertionRevision",
        ("tenant_id", "chunk_id"),
    ),
    SchemaExpectation(
        "knowledge_publication_status_lookup",
        "index",
        "RANGE",
        "KnowledgePublication",
        ("tenant_id", "status", "generation"),
    ),
    SchemaExpectation(
        "knowledge_publication_activation_lookup",
        "index",
        "RANGE",
        "KnowledgePublicationActivation",
        ("tenant_id", "activated_at"),
    ),
    SchemaExpectation(
        "knowledge_construction_job_status_lookup",
        "index",
        "RANGE",
        "KnowledgeConstructionJob",
        ("tenant_id", "status", "updated_at"),
    ),
    SchemaExpectation(
        "knowledge_construction_outcome_status_lookup",
        "index",
        "RANGE",
        "KnowledgeConstructionChunkOutcome",
        ("tenant_id", "status", "completed_at"),
    ),
    SchemaExpectation(
        "knowledge_construction_outcome_chunk_lookup",
        "index",
        "RANGE",
        "KnowledgeConstructionChunkOutcome",
        ("tenant_id", "chunk_id"),
    ),
    SchemaExpectation(
        "published_graph_quality_run_lookup",
        "index",
        "RANGE",
        "PublishedGraphQualityRun",
        ("tenant_id", "publication_generation", "recorded_at"),
    ),
    SchemaExpectation(
        "graphrag_chunk_text_v1",
        "index",
        "FULLTEXT",
        "Chunk",
        ("text",),
    ),
    SchemaExpectation(
        "graphrag_chunk_text_v2",
        "index",
        "FULLTEXT",
        "Chunk",
        ("text", "retrieval_scope"),
    ),
    SchemaExpectation(
        "chunk_retrieval_scope_lookup",
        "index",
        "RANGE",
        "Chunk",
        ("tenant_id", "document_id", "version_id"),
    ),
)


def migration_paths(directory: Path = MIGRATIONS_DIRECTORY) -> tuple[Path, ...]:
    """Return all migration files in deterministic filename order."""
    paths = tuple(sorted(directory.glob("*.cypher"), key=lambda item: item.name))
    if not paths:
        raise FileNotFoundError(f"no Cypher migrations found in {directory}")
    return paths


def _statements_from(path: Path) -> tuple[str, ...]:
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("//"):
            lines.append(stripped)
    joined = "\n".join(lines)
    return tuple(
        statement.strip() for statement in joined.split(";") if statement.strip()
    )


def _without_retired_creates(statements: tuple[str, ...]) -> tuple[str, ...]:
    """Avoid recreating an object that a later replayed migration retires."""
    later_operation: dict[tuple[str, str], str] = {}
    replay_reversed: list[str] = []
    for statement in reversed(statements):
        parts = statement.split(maxsplit=3)
        is_schema_ddl = (
            len(parts) >= 3
            and parts[0] in {"CREATE", "DROP"}
            and parts[1] in {"CONSTRAINT", "INDEX"}
        )
        if not is_schema_ddl:
            replay_reversed.append(statement)
            continue
        action, kind, name = parts[:3]
        key = (kind, name)
        if action == "CREATE" and later_operation.get(key) == "DROP":
            continue
        later_operation[key] = action
        replay_reversed.append(statement)
    return tuple(reversed(replay_reversed))


def migration_statements(path: Path | None = None) -> tuple[str, ...]:
    """Load one legacy migration or a replay-safe sequence of all migrations."""
    paths = (path,) if path is not None else migration_paths()
    statements = tuple(
        statement for item in paths for statement in _statements_from(item)
    )
    if path is not None:
        return statements
    return _without_retired_creates(statements)


def apply_schema(driver: QueryDriver, database: str = "neo4j") -> None:
    """Apply all versioned migrations in deterministic order."""
    for statement in migration_statements():
        driver.execute_query(statement, database_=database)
    driver.execute_query(
        "CALL db.awaitIndexes($timeout_seconds)",
        timeout_seconds=300,
        database_=database,
    )


def _record_map(records: list[Any]) -> dict[str, dict[str, Any]]:
    return {record["name"]: dict(record) for record in records}


def verify_schema(driver: QueryDriver, database: str = "neo4j") -> list[str]:
    """Return structural schema errors; an empty list means valid and online."""
    constraint_records, _, _ = driver.execute_query(
        "SHOW CONSTRAINTS YIELD name, type, entityType, labelsOrTypes, properties "
        "RETURN name, type, entityType, labelsOrTypes, properties",
        database_=database,
    )
    index_records, _, _ = driver.execute_query(
        "SHOW INDEXES YIELD name, type, entityType, labelsOrTypes, properties, state "
        "RETURN name, type, entityType, labelsOrTypes, properties, state",
        database_=database,
    )
    actual_constraints = _record_map(constraint_records)
    actual_indexes = _record_map(index_records)
    errors: list[str] = []

    for expected in EXPECTED_SCHEMA:
        actual = (
            actual_constraints.get(expected.name)
            if expected.kind == "constraint"
            else actual_indexes.get(expected.name)
        )
        if actual is None:
            errors.append(f"missing {expected.kind}: {expected.name}")
            continue
        actual_shape = (
            actual.get("type"),
            actual.get("entityType"),
            tuple(actual.get("labelsOrTypes") or ()),
            tuple(actual.get("properties") or ()),
        )
        expected_shape = (
            expected.schema_type,
            "NODE",
            (expected.label,),
            expected.properties,
        )
        if actual_shape != expected_shape:
            errors.append(
                f"schema shape mismatch for {expected.name}: "
                f"expected {expected_shape}, got {actual_shape}"
            )
        if expected.kind == "index" and actual.get("state") != "ONLINE":
            errors.append(f"index is not online: {expected.name}")
    return errors
