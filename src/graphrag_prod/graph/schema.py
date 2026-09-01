"""Idempotent Neo4j schema migration and structural verification."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


MIGRATION_PATH = Path(__file__).with_name("migrations") / "001_provenance_schema.cypher"


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
        "chunk_ordinal_unique",
        "constraint",
        "UNIQUENESS",
        "Chunk",
        ("version_id", "ordinal"),
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
)


def migration_statements(path: Path = MIGRATION_PATH) -> tuple[str, ...]:
    """Load executable statements from the versioned migration source."""
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("//"):
            lines.append(stripped)
    joined = "\n".join(lines)
    return tuple(
        statement.strip() for statement in joined.split(";") if statement.strip()
    )


def apply_schema(driver: QueryDriver, database: str = "neo4j") -> None:
    """Apply the single versioned migration idempotently."""
    for statement in migration_statements():
        driver.execute_query(statement, database_=database)


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
