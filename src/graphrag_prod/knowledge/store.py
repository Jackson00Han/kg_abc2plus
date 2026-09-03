"""Neo4j persistence for append-only governed A-Box record revisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Protocol

from graphrag_prod.domain.access import Principal

from .models import (
    ABoxRecordBatch,
    AssertionRecord,
    EntityIdentity,
    EntityMentionRecord,
    EvidenceReference,
    RecordRevision,
)
from .trust import (
    AuthorityLevel,
    GovernanceStatus,
    KnowledgeOrigin,
    TrustMetadata,
)


MAX_RECORDS_PER_WRITE = 1_000
MAX_RECORDS_PER_READ = 500


class SessionDriver(Protocol):
    def session(self, **kwargs: object) -> Any: ...


class KnowledgeStoreError(RuntimeError):
    """Base error for governed A-Box persistence."""


class KnowledgeConflict(KnowledgeStoreError):
    """The immutable identity or compare-and-swap precondition conflicts."""


class KnowledgeEvidenceError(KnowledgeStoreError):
    """The claimed exact source evidence is absent or no longer matches."""


class KnowledgeSchemaError(KnowledgeStoreError):
    """A record is not allowed by its exact published T-Box version."""


@dataclass(frozen=True, slots=True)
class KnowledgeWriteResult:
    tenant_id: str
    ontology_version_id: str
    mention_count: int
    assertion_count: int
    revision_ids: tuple[str, ...]


def _properties(**values: object) -> dict[str, object]:
    return {key: value for key, value in values.items() if value is not None}


def _trust_properties(trust: TrustMetadata) -> dict[str, object]:
    return _properties(
        origin=trust.origin.value,
        authority_level=trust.authority.value,
        governance_status=trust.status.value,
        ontology_version_id=trust.ontology_version_id,
        extractor_version=trust.extractor_version,
        prompt_version=trust.prompt_version,
        trust_created_at=trust.created_at,
        reviewed_by=trust.reviewed_by,
        reviewed_at=trust.reviewed_at,
        review_notes=trust.review_notes,
    )


def _evidence_properties(evidence: EvidenceReference) -> dict[str, object]:
    return {
        "document_id": evidence.document_id,
        "version_id": evidence.version_id,
        "chunk_id": evidence.chunk_id,
        "evidence_char_start": evidence.char_start,
        "evidence_char_end": evidence.char_end,
        "evidence_text": evidence.quoted_text,
        "access_policy_id": evidence.access_policy_id,
        "access_policy_version": evidence.access_policy_version,
        "access_groups": sorted(evidence.access_groups),
    }


def _revision_properties(record: EntityMentionRecord | AssertionRecord) -> dict[str, object]:
    return {
        "revision_id": record.revision_id,
        "record_id": record.record_id,
        "revision": record.revision.revision,
        "previous_revision": record.revision.expected_previous_revision,
        "tenant_id": record.tenant_id,
        "created_at": record.created_at,
        "confidence": record.confidence,
        **_evidence_properties(record.evidence),
        **_trust_properties(record.trust),
    }


def _entity_properties(entity: EntityIdentity, prefix: str = "") -> dict[str, object]:
    return {
        f"{prefix}entity_id": entity.entity_id,
        f"{prefix}entity_type": entity.entity_type,
        f"{prefix}canonical_key": entity.canonical_key,
        f"{prefix}canonical_name": entity.canonical_name,
        f"{prefix}aliases": list(entity.aliases),
    }


def _native_datetime(value: object, name: str) -> datetime:
    if hasattr(value, "to_native"):
        value = value.to_native()
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise KnowledgeStoreError(f"stored {name} is not a datetime")
    return value


def _stored_trust(properties: dict[str, Any]) -> TrustMetadata:
    return TrustMetadata(
        origin=KnowledgeOrigin(properties["origin"]),
        authority=AuthorityLevel(properties["authority_level"]),
        status=GovernanceStatus(properties["governance_status"]),
        ontology_version_id=properties["ontology_version_id"],
        created_at=_native_datetime(properties["trust_created_at"], "trust_created_at"),
        extractor_version=properties.get("extractor_version"),
        prompt_version=properties.get("prompt_version"),
        reviewed_by=properties.get("reviewed_by"),
        reviewed_at=(
            None
            if properties.get("reviewed_at") is None
            else _native_datetime(properties["reviewed_at"], "reviewed_at")
        ),
        review_notes=properties.get("review_notes"),
    )


def _stored_revision(properties: dict[str, Any]) -> RecordRevision:
    return RecordRevision(
        record_id=properties["record_id"],
        revision_id=properties["revision_id"],
        revision=properties["revision"],
        expected_previous_revision=properties["previous_revision"],
    )


def _stored_evidence(properties: dict[str, Any]) -> EvidenceReference:
    return EvidenceReference(
        tenant_id=properties["tenant_id"],
        document_id=properties["document_id"],
        version_id=properties["version_id"],
        chunk_id=properties["chunk_id"],
        char_start=properties["evidence_char_start"],
        char_end=properties["evidence_char_end"],
        quoted_text=properties["evidence_text"],
        access_policy_id=properties["access_policy_id"],
        access_policy_version=properties["access_policy_version"],
        access_groups=frozenset(properties["access_groups"]),
    )


def _stored_entity(
    properties: dict[str, Any],
    *,
    tenant_id: str,
    prefix: str = "",
) -> EntityIdentity:
    return EntityIdentity(
        entity_id=properties[f"{prefix}entity_id"],
        tenant_id=tenant_id,
        entity_type=properties[f"{prefix}entity_type"],
        canonical_key=properties[f"{prefix}canonical_key"],
        canonical_name=properties[f"{prefix}canonical_name"],
        aliases=tuple(properties.get(f"{prefix}aliases", ())),
    )


def _stored_mention(properties: dict[str, Any]) -> EntityMentionRecord:
    tenant_id = properties["tenant_id"]
    return EntityMentionRecord(
        revision=_stored_revision(properties),
        tenant_id=tenant_id,
        entity=_stored_entity(properties, tenant_id=tenant_id),
        evidence=_stored_evidence(properties),
        confidence=properties["confidence"],
        trust=_stored_trust(properties),
        created_at=_native_datetime(properties["created_at"], "created_at"),
    )


def _stored_assertion(properties: dict[str, Any]) -> AssertionRecord:
    tenant_id = properties["tenant_id"]
    object_kind = properties["object_kind"]
    if object_kind not in {"entity", "literal"}:
        raise KnowledgeStoreError("stored assertion object_kind is invalid")
    return AssertionRecord(
        revision=_stored_revision(properties),
        tenant_id=tenant_id,
        subject=_stored_entity(properties, tenant_id=tenant_id, prefix="subject_"),
        predicate=properties["predicate"],
        evidence=_stored_evidence(properties),
        subject_mention_revision_id=properties["subject_mention_revision_id"],
        confidence=properties["confidence"],
        trust=_stored_trust(properties),
        created_at=_native_datetime(properties["created_at"], "created_at"),
        object_entity=(
            _stored_entity(properties, tenant_id=tenant_id, prefix="object_")
            if object_kind == "entity"
            else None
        ),
        object_mention_revision_id=(
            properties.get("object_mention_revision_id")
            if object_kind == "entity"
            else None
        ),
        literal_value=(properties.get("literal_value") if object_kind == "literal" else None),
    )


def _normalized_statuses(
    statuses: Iterable[GovernanceStatus] | None,
) -> list[str]:
    values = tuple(statuses) if statuses is not None else (GovernanceStatus.PUBLISHED,)
    if not values:
        raise ValueError("statuses must not be empty")
    if any(not isinstance(value, GovernanceStatus) for value in values):
        raise TypeError("statuses must contain GovernanceStatus values")
    return sorted({value.value for value in values})


def _read_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer")
    if not 1 <= limit <= MAX_RECORDS_PER_READ:
        raise ValueError(f"limit must be between 1 and {MAX_RECORDS_PER_READ}")
    return limit


_MENTION_READ_QUERY = """
MATCH (head:KnowledgeRecordHead)-[:CURRENT_REVISION]->
      (revision:GovernedEntityMentionRevision)-[:IN_CHUNK]->(chunk:Chunk)
MATCH (document:Document)-[:HAS_VERSION]->(version:DocumentVersion)-[:HAS_CHUNK]->(chunk)
WHERE head.tenant_id = $tenant_id
  AND revision.tenant_id = $tenant_id
  AND document.tenant_id = $tenant_id
  AND version.tenant_id = $tenant_id
  AND chunk.tenant_id = $tenant_id
  AND ($record_id IS NULL OR head.record_id = $record_id)
  AND revision.governance_status IN $statuses
  AND revision.document_id = document.document_id
  AND revision.version_id = version.version_id
  AND revision.chunk_id = chunk.chunk_id
  AND revision.access_policy_id = chunk.access_policy_id
  AND revision.access_policy_version = chunk.access_policy_version
  AND revision.access_groups = chunk.access_groups
  AND any(group IN $groups WHERE group IN revision.access_groups)
  AND any(group IN $groups WHERE group IN chunk.access_groups)
  AND any(group IN $groups WHERE group IN document.access_groups)
RETURN revision {.*} AS revision
ORDER BY revision.created_at DESC, revision.record_id ASC
LIMIT $limit
"""


_ASSERTION_READ_QUERY = """
MATCH (head:KnowledgeRecordHead)-[:CURRENT_REVISION]->
      (revision:GovernedAssertionRevision)-[:EVIDENCED_BY]->(chunk:Chunk)
MATCH (document:Document)-[:HAS_VERSION]->(version:DocumentVersion)-[:HAS_CHUNK]->(chunk)
WHERE head.tenant_id = $tenant_id
  AND revision.tenant_id = $tenant_id
  AND document.tenant_id = $tenant_id
  AND version.tenant_id = $tenant_id
  AND chunk.tenant_id = $tenant_id
  AND ($record_id IS NULL OR head.record_id = $record_id)
  AND revision.governance_status IN $statuses
  AND revision.document_id = document.document_id
  AND revision.version_id = version.version_id
  AND revision.chunk_id = chunk.chunk_id
  AND revision.access_policy_id = chunk.access_policy_id
  AND revision.access_policy_version = chunk.access_policy_version
  AND revision.access_groups = chunk.access_groups
  AND any(group IN $groups WHERE group IN revision.access_groups)
  AND any(group IN $groups WHERE group IN chunk.access_groups)
  AND any(group IN $groups WHERE group IN document.access_groups)
RETURN revision {.*} AS revision
ORDER BY revision.created_at DESC, revision.record_id ASC
LIMIT $limit
"""


class Neo4jKnowledgeStore:
    """Persist authoritative and model-derived A-Box records without mixing trust."""

    def __init__(self, driver: SessionDriver, database: str = "neo4j") -> None:
        self.driver = driver
        self.database = database

    def import_authoritative(self, batch: ABoxRecordBatch) -> KnowledgeWriteResult:
        """Import expert A-Box records that already passed authoritative review."""

        batch.require_authoritative_import()
        return self._write_batch(batch, publish_entity_profiles=True)

    def persist_llm_candidates(self, batch: ABoxRecordBatch) -> KnowledgeWriteResult:
        """Persist unreviewed LLM output in the candidate layer only."""

        batch.require_llm_candidates()
        return self._write_batch(batch, publish_entity_profiles=False)

    def persist_llm_quarantined(self, batch: ABoxRecordBatch) -> KnowledgeWriteResult:
        """Persist below-threshold LLM output in the quarantine layer only."""

        batch.require_llm_quarantined()
        return self._write_batch(batch, publish_entity_profiles=False)

    def _write_batch(
        self,
        batch: ABoxRecordBatch,
        *,
        publish_entity_profiles: bool,
    ) -> KnowledgeWriteResult:
        count = len(batch.mentions) + len(batch.assertions)
        if count > MAX_RECORDS_PER_WRITE:
            raise ValueError(
                f"A-Box write exceeds the {MAX_RECORDS_PER_WRITE}-record limit"
            )
        with self.driver.session(database=self.database) as session:
            return session.execute_write(
                self._write_batch_tx,
                batch,
                publish_entity_profiles,
            )

    @classmethod
    def _write_batch_tx(
        cls,
        tx: Any,
        batch: ABoxRecordBatch,
        publish_entity_profiles: bool,
    ) -> KnowledgeWriteResult:
        cls._validate_tbox_tx(tx, batch)
        for mention in batch.mentions:
            cls._validate_evidence_tx(tx, mention.evidence)
            cls._lock_head_tx(tx, mention.revision, mention.tenant_id, "ENTITY_MENTION", mention.created_at)
            if publish_entity_profiles:
                cls._merge_entity_tx(tx, mention.entity)
            cls._create_mention_revision_tx(
                tx,
                mention,
                link_canonical_entity=publish_entity_profiles,
            )

        for assertion in batch.assertions:
            cls._validate_evidence_tx(tx, assertion.evidence)
            cls._lock_head_tx(tx, assertion.revision, assertion.tenant_id, "ASSERTION", assertion.created_at)
            if publish_entity_profiles:
                cls._merge_entity_tx(tx, assertion.subject)
                if assertion.object_entity is not None:
                    cls._merge_entity_tx(tx, assertion.object_entity)
            cls._create_assertion_revision_tx(
                tx,
                assertion,
                link_canonical_entities=publish_entity_profiles,
            )

        revision_ids = tuple(
            record.revision_id for record in (*batch.mentions, *batch.assertions)
        )
        return KnowledgeWriteResult(
            tenant_id=batch.tenant_id,
            ontology_version_id=batch.ontology_version_id,
            mention_count=len(batch.mentions),
            assertion_count=len(batch.assertions),
            revision_ids=revision_ids,
        )

    @staticmethod
    def _validate_tbox_tx(tx: Any, batch: ABoxRecordBatch) -> None:
        """Lock and validate against the exact tenant-owned published T-Box."""

        version = tx.run(
            """
            MATCH (tbox:TBoxVersion {
                tenant_id: $tenant_id,
                tbox_id: $tbox_id,
                status: 'PUBLISHED'
            })
            SET tbox.__abox_write_lock = randomUUID()
            WITH tbox
            REMOVE tbox.__abox_write_lock
            RETURN tbox.tbox_id AS tbox_id
            """,
            tenant_id=batch.tenant_id,
            tbox_id=batch.ontology_version_id,
        ).single()
        if version is None or version["tbox_id"] != batch.ontology_version_id:
            raise KnowledgeSchemaError(
                "the exact tenant T-Box version does not exist or is not PUBLISHED"
            )

        entity_rows = tuple(
            tx.run(
                """
                MATCH (tbox:TBoxVersion {
                    tenant_id: $tenant_id,
                    tbox_id: $tbox_id,
                    status: 'PUBLISHED'
                })-[:DECLARES_ENTITY_TYPE]->(entity_type:TBoxEntityType)
                OPTIONAL MATCH (entity_type)-[:DECLARES_PROPERTY]->
                               (property:TBoxPropertyDefinition)
                RETURN entity_type.name AS name,
                       entity_type.canonical_key_namespaces AS namespaces,
                       collect(property.name) AS literal_predicates
                """,
                tenant_id=batch.tenant_id,
                tbox_id=batch.ontology_version_id,
            )
        )
        entity_contracts = {
            row["name"]: {
                "namespaces": frozenset(row["namespaces"] or ()),
                "literal_predicates": frozenset(row["literal_predicates"] or ()),
            }
            for row in entity_rows
        }
        if not entity_contracts:
            raise KnowledgeSchemaError("published T-Box has no entity definitions")

        relationship_rows = tuple(
            tx.run(
                """
                MATCH (tbox:TBoxVersion {
                    tenant_id: $tenant_id,
                    tbox_id: $tbox_id,
                    status: 'PUBLISHED'
                })-[:DECLARES_RELATIONSHIP_TYPE]->
                  (relationship_type:TBoxRelationshipType)
                RETURN relationship_type.name AS name,
                       relationship_type.source_types AS source_types,
                       relationship_type.target_types AS target_types
                """,
                tenant_id=batch.tenant_id,
                tbox_id=batch.ontology_version_id,
            )
        )
        relationship_contracts = {
            row["name"]: (
                frozenset(row["source_types"] or ()),
                frozenset(row["target_types"] or ()),
            )
            for row in relationship_rows
        }

        entities = {
            mention.entity.entity_id: mention.entity for mention in batch.mentions
        }
        entities.update(
            {
                assertion.subject.entity_id: assertion.subject
                for assertion in batch.assertions
            }
        )
        entities.update(
            {
                assertion.object_entity.entity_id: assertion.object_entity
                for assertion in batch.assertions
                if assertion.object_entity is not None
            }
        )
        model_derived = all(
            record.trust.origin is KnowledgeOrigin.LLM_EXTRACTED
            for record in (*batch.mentions, *batch.assertions)
        )
        for entity in entities.values():
            contract = entity_contracts.get(entity.entity_type)
            if contract is None:
                raise KnowledgeSchemaError(
                    f"entity type {entity.entity_type!r} is not declared by the T-Box"
                )
            namespace, separator, _ = entity.canonical_key.partition(":")
            normalized_namespace = namespace.casefold()
            allowed_namespace = (
                normalized_namespace == "llm-candidate"
                if model_derived
                else normalized_namespace in contract["namespaces"]
            )
            if not separator or not allowed_namespace:
                raise KnowledgeSchemaError(
                    f"canonical key namespace for {entity.entity_type!r} is not allowed"
                )

        for assertion in batch.assertions:
            if assertion.object_entity is None:
                allowed = entity_contracts[assertion.subject.entity_type][
                    "literal_predicates"
                ]
                if assertion.predicate not in allowed:
                    raise KnowledgeSchemaError(
                        f"literal predicate {assertion.predicate!r} is not declared "
                        f"on {assertion.subject.entity_type!r}"
                    )
                continue
            endpoints = relationship_contracts.get(assertion.predicate)
            if endpoints is None:
                raise KnowledgeSchemaError(
                    f"relationship {assertion.predicate!r} is not declared by the T-Box"
                )
            source_types, target_types = endpoints
            if (
                assertion.subject.entity_type not in source_types
                or assertion.object_entity.entity_type not in target_types
            ):
                raise KnowledgeSchemaError(
                    f"relationship {assertion.predicate!r} violates its domain/range"
                )

    @staticmethod
    def _validate_evidence_tx(tx: Any, evidence: EvidenceReference) -> None:
        row = tx.run(
            """
            MATCH (document:Document {document_id: $document_id})-[:HAS_VERSION]->
                  (version:DocumentVersion {version_id: $version_id})-[:HAS_CHUNK]->
                  (chunk:Chunk {chunk_id: $chunk_id})
            WHERE document.tenant_id = $tenant_id
              AND version.tenant_id = $tenant_id
              AND chunk.tenant_id = $tenant_id
              AND version.document_id = document.document_id
              AND chunk.document_id = document.document_id
              AND chunk.version_id = version.version_id
            RETURN chunk.char_start AS chunk_char_start,
                   chunk.char_end AS chunk_char_end,
                   substring(
                       chunk.text,
                       $evidence_char_start - chunk.char_start,
                       $evidence_char_end - $evidence_char_start
                   ) AS evidence_text,
                   chunk.access_policy_id AS access_policy_id,
                   chunk.access_policy_version AS access_policy_version,
                   chunk.access_groups AS access_groups,
                   document.access_groups AS document_access_groups
            """,
            tenant_id=evidence.tenant_id,
            document_id=evidence.document_id,
            version_id=evidence.version_id,
            chunk_id=evidence.chunk_id,
            evidence_char_start=evidence.char_start,
            evidence_char_end=evidence.char_end,
        ).single()
        if row is None:
            raise KnowledgeEvidenceError("source document/version/Chunk path does not exist")
        groups = frozenset(row["access_groups"] or ())
        document_groups = frozenset(row["document_access_groups"] or ())
        matches = (
            row["chunk_char_start"] <= evidence.char_start
            and evidence.char_end <= row["chunk_char_end"]
            and row["evidence_text"] == evidence.quoted_text
            and row["access_policy_id"] == evidence.access_policy_id
            and row["access_policy_version"] == evidence.access_policy_version
            and groups == evidence.access_groups
            and evidence.access_groups <= document_groups
        )
        if not matches:
            raise KnowledgeEvidenceError(
                "exact evidence text, range, or access-policy snapshot does not match"
            )

    @staticmethod
    def _lock_head_tx(
        tx: Any,
        revision: RecordRevision,
        tenant_id: str,
        record_kind: str,
        created_at: datetime,
    ) -> None:
        row = tx.run(
            """
            MERGE (head:KnowledgeRecordHead {record_id: $record_id})
            ON CREATE SET head.tenant_id = $tenant_id,
                          head.record_kind = $record_kind,
                          head.current_revision = 0,
                          head.created_at = $created_at
            SET head.__cas_write_lock = randomUUID()
            WITH head
            REMOVE head.__cas_write_lock
            RETURN head.tenant_id = $tenant_id
                       AND head.record_kind = $record_kind AS compatible,
                   head.current_revision AS current_revision
            """,
            record_id=revision.record_id,
            tenant_id=tenant_id,
            record_kind=record_kind,
            created_at=created_at,
        ).single()
        if row is None or not row["compatible"]:
            raise KnowledgeConflict("logical record ID conflicts with another tenant or kind")
        if row["current_revision"] != revision.expected_previous_revision:
            raise KnowledgeConflict(
                "stale knowledge revision: expected head "
                f"{revision.expected_previous_revision}, got {row['current_revision']}"
            )

    @staticmethod
    def _merge_entity_tx(
        tx: Any,
        entity: EntityIdentity,
    ) -> None:
        row = tx.run(
            """
            MERGE (entity:Entity {entity_id: $entity_id})
            ON CREATE SET entity.tenant_id = $tenant_id,
                          entity.entity_type = $entity_type,
                          entity.canonical_key = $canonical_key
            SET entity.__identity_write_lock = randomUUID()
            WITH entity
            REMOVE entity.__identity_write_lock
            RETURN entity.tenant_id = $tenant_id
                       AND entity.entity_type = $entity_type
                       AND entity.canonical_key = $canonical_key AS compatible,
                   entity.canonical_name AS canonical_name,
                   entity.aliases AS aliases
            """,
            entity_id=entity.entity_id,
            tenant_id=entity.tenant_id,
            entity_type=entity.entity_type,
            canonical_key=entity.canonical_key,
        ).single()
        if row is None or not row["compatible"]:
            raise KnowledgeConflict("canonical Entity identity conflicts with its stable ID")
        stored_name = row["canonical_name"]
        stored_aliases = row["aliases"]
        if stored_name is not None and stored_name != entity.canonical_name:
            raise KnowledgeConflict("authoritative Entity canonical name conflicts")
        if stored_aliases is not None and tuple(stored_aliases) != entity.aliases:
            raise KnowledgeConflict("authoritative Entity aliases conflict")
        if stored_name is None or stored_aliases is None:
            tx.run(
                """
                MATCH (entity:Entity {entity_id: $entity_id})
                SET entity.canonical_name = $canonical_name,
                    entity.aliases = $aliases
                """,
                entity_id=entity.entity_id,
                canonical_name=entity.canonical_name,
                aliases=list(entity.aliases),
            ).consume()

    @staticmethod
    def _create_mention_revision_tx(
        tx: Any,
        mention: EntityMentionRecord,
        *,
        link_canonical_entity: bool,
    ) -> None:
        properties = {
            **_revision_properties(mention),
            **_entity_properties(mention.entity),
            "surface": mention.surface,
        }
        row = tx.run(
            """
            MATCH (head:KnowledgeRecordHead {record_id: $record_id})
            MATCH (chunk:Chunk {chunk_id: $chunk_id})
            OPTIONAL MATCH (entity:Entity {entity_id: $entity_id})
            OPTIONAL MATCH (head)-[old_pointer:CURRENT_REVISION]->(previous)
            WITH head, chunk, entity, old_pointer, previous
            WHERE head.tenant_id = $tenant_id
              AND head.record_kind = 'ENTITY_MENTION'
              AND head.current_revision = $expected_previous_revision
              AND chunk.tenant_id = $tenant_id
              AND (NOT $link_canonical_entity OR entity.tenant_id = $tenant_id)
              AND ($expected_previous_revision = 0 OR
                   previous.revision = $expected_previous_revision)
            CREATE (revision:GovernedEntityMentionRevision {revision_id: $revision_id})
            SET revision += $properties
            CREATE (revision)-[:IN_CHUNK]->(chunk)
            FOREACH (_ IN CASE WHEN $link_canonical_entity THEN [1] ELSE [] END |
                CREATE (revision)-[:REFERS_TO]->(entity)
            )
            FOREACH (_ IN CASE WHEN previous IS NULL THEN [] ELSE [1] END |
                CREATE (revision)-[:SUPERSEDES]->(previous)
            )
            FOREACH (_ IN CASE WHEN old_pointer IS NULL THEN [] ELSE [1] END |
                DELETE old_pointer
            )
            CREATE (head)-[:CURRENT_REVISION]->(revision)
            SET head.current_revision = $revision_number,
                head.updated_at = $created_at
            RETURN revision.revision_id AS revision_id
            """,
            record_id=mention.record_id,
            revision_id=mention.revision_id,
            revision_number=mention.revision.revision,
            expected_previous_revision=mention.revision.expected_previous_revision,
            tenant_id=mention.tenant_id,
            chunk_id=mention.evidence.chunk_id,
            entity_id=mention.entity.entity_id,
            link_canonical_entity=link_canonical_entity,
            created_at=mention.created_at,
            properties=properties,
        ).single()
        if row is None or row["revision_id"] != mention.revision_id:
            raise KnowledgeConflict("entity-mention revision compare-and-swap failed")

    @staticmethod
    def _create_assertion_revision_tx(
        tx: Any,
        assertion: AssertionRecord,
        *,
        link_canonical_entities: bool,
    ) -> None:
        properties = {
            **_revision_properties(assertion),
            **_entity_properties(assertion.subject, "subject_"),
            "predicate": assertion.predicate,
            "subject_mention_revision_id": assertion.subject_mention_revision_id,
            "object_kind": assertion.object_kind,
            "literal_value": assertion.literal_value,
            "object_mention_revision_id": assertion.object_mention_revision_id,
        }
        if assertion.object_entity is not None:
            properties.update(_entity_properties(assertion.object_entity, "object_"))
        properties = _properties(**properties)
        row = tx.run(
            """
            MATCH (head:KnowledgeRecordHead {record_id: $record_id})
            MATCH (chunk:Chunk {chunk_id: $chunk_id})
            OPTIONAL MATCH (subject:Entity {entity_id: $subject_entity_id})
            MATCH (subject_mention:GovernedEntityMentionRevision {
                revision_id: $subject_mention_revision_id
            })
            OPTIONAL MATCH (object:Entity {entity_id: $object_entity_id})
            OPTIONAL MATCH (object_mention:GovernedEntityMentionRevision {
                revision_id: $object_mention_revision_id
            })
            OPTIONAL MATCH (head)-[old_pointer:CURRENT_REVISION]->(previous)
            WITH head, chunk, subject, subject_mention, object, object_mention,
                 old_pointer, previous
            WHERE head.tenant_id = $tenant_id
              AND head.record_kind = 'ASSERTION'
              AND head.current_revision = $expected_previous_revision
              AND chunk.tenant_id = $tenant_id
              AND subject_mention.tenant_id = $tenant_id
              AND subject_mention.chunk_id = chunk.chunk_id
              AND subject_mention.entity_id = $subject_entity_id
              AND (NOT $link_canonical_entities OR
                   subject.tenant_id = $tenant_id)
              AND ($object_kind = 'literal' OR (
                    object_mention IS NOT NULL
                    AND object_mention.tenant_id = $tenant_id
                    AND object_mention.chunk_id = chunk.chunk_id
                    AND object_mention.entity_id = $object_entity_id
                    AND (NOT $link_canonical_entities OR
                         object.tenant_id = $tenant_id)
                  ))
              AND ($expected_previous_revision = 0 OR
                   previous.revision = $expected_previous_revision)
            CREATE (revision:GovernedAssertionRevision {revision_id: $revision_id})
            SET revision += $properties
            CREATE (revision)-[:EVIDENCED_BY]->(chunk)
            CREATE (revision)-[:SUPPORTED_BY_MENTION]->(subject_mention)
            FOREACH (_ IN CASE WHEN $link_canonical_entities THEN [1] ELSE [] END |
                CREATE (revision)-[:SUBJECT]->(subject)
            )
            FOREACH (_ IN CASE
                WHEN $link_canonical_entities AND object IS NOT NULL THEN [1]
                ELSE []
            END |
                CREATE (revision)-[:OBJECT]->(object)
            )
            FOREACH (_ IN CASE WHEN object_mention IS NULL THEN [] ELSE [1] END |
                CREATE (revision)-[:SUPPORTED_BY_MENTION]->(object_mention)
            )
            FOREACH (_ IN CASE WHEN previous IS NULL THEN [] ELSE [1] END |
                CREATE (revision)-[:SUPERSEDES]->(previous)
            )
            FOREACH (_ IN CASE WHEN old_pointer IS NULL THEN [] ELSE [1] END |
                DELETE old_pointer
            )
            CREATE (head)-[:CURRENT_REVISION]->(revision)
            SET head.current_revision = $revision_number,
                head.updated_at = $created_at
            RETURN revision.revision_id AS revision_id
            """,
            record_id=assertion.record_id,
            revision_id=assertion.revision_id,
            revision_number=assertion.revision.revision,
            expected_previous_revision=assertion.revision.expected_previous_revision,
            tenant_id=assertion.tenant_id,
            chunk_id=assertion.evidence.chunk_id,
            subject_entity_id=assertion.subject.entity_id,
            subject_mention_revision_id=assertion.subject_mention_revision_id,
            object_kind=assertion.object_kind,
            object_entity_id=(
                None
                if assertion.object_entity is None
                else assertion.object_entity.entity_id
            ),
            object_mention_revision_id=assertion.object_mention_revision_id,
            link_canonical_entities=link_canonical_entities,
            created_at=assertion.created_at,
            properties=properties,
        ).single()
        if row is None or row["revision_id"] != assertion.revision_id:
            raise KnowledgeConflict("assertion revision compare-and-swap failed")

    def list_entity_mentions(
        self,
        principal: Principal,
        *,
        statuses: Iterable[GovernanceStatus] | None = None,
        limit: int = 100,
    ) -> tuple[EntityMentionRecord, ...]:
        """List authorized current mention revisions; published-only by default."""

        return self._read_mentions(
            principal,
            record_id=None,
            statuses=statuses,
            limit=limit,
        )

    def get_entity_mention(
        self,
        principal: Principal,
        record_id: str,
        *,
        statuses: Iterable[GovernanceStatus] | None = None,
    ) -> EntityMentionRecord | None:
        """Read one authorized current mention revision without existence leakage."""

        records = self._read_mentions(
            principal,
            record_id=record_id.strip(),
            statuses=statuses,
            limit=1,
        )
        return records[0] if records else None

    def _read_mentions(
        self,
        principal: Principal,
        *,
        record_id: str | None,
        statuses: Iterable[GovernanceStatus] | None,
        limit: int,
    ) -> tuple[EntityMentionRecord, ...]:
        if record_id is not None and not record_id:
            raise ValueError("record_id must not be empty")
        parameters = {
            "tenant_id": principal.tenant_id,
            "groups": sorted(principal.groups),
            "record_id": record_id,
            "statuses": _normalized_statuses(statuses),
            "limit": _read_limit(limit),
        }
        with self.driver.session(database=self.database) as session:
            rows = session.run(_MENTION_READ_QUERY, **parameters)
            return tuple(_stored_mention(dict(row["revision"])) for row in rows)

    def list_assertions(
        self,
        principal: Principal,
        *,
        statuses: Iterable[GovernanceStatus] | None = None,
        limit: int = 100,
    ) -> tuple[AssertionRecord, ...]:
        """List authorized current assertion revisions; published-only by default."""

        return self._read_assertions(
            principal,
            record_id=None,
            statuses=statuses,
            limit=limit,
        )

    def get_assertion(
        self,
        principal: Principal,
        record_id: str,
        *,
        statuses: Iterable[GovernanceStatus] | None = None,
    ) -> AssertionRecord | None:
        """Read one authorized current assertion revision without existence leakage."""

        records = self._read_assertions(
            principal,
            record_id=record_id.strip(),
            statuses=statuses,
            limit=1,
        )
        return records[0] if records else None

    def _read_assertions(
        self,
        principal: Principal,
        *,
        record_id: str | None,
        statuses: Iterable[GovernanceStatus] | None,
        limit: int,
    ) -> tuple[AssertionRecord, ...]:
        if record_id is not None and not record_id:
            raise ValueError("record_id must not be empty")
        parameters = {
            "tenant_id": principal.tenant_id,
            "groups": sorted(principal.groups),
            "record_id": record_id,
            "statuses": _normalized_statuses(statuses),
            "limit": _read_limit(limit),
        }
        with self.driver.session(database=self.database) as session:
            rows = session.run(_ASSERTION_READ_QUERY, **parameters)
            return tuple(_stored_assertion(dict(row["revision"])) for row in rows)
