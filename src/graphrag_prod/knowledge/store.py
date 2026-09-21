"""Neo4j persistence for append-only governed A-Box record revisions."""

from __future__ import annotations

from graphrag_prod.domain.facts import decode_fact_distinction

from dataclasses import dataclass
from datetime import datetime
import json
import hashlib
from typing import Any, Iterable, Protocol

from graphrag_prod.domain.facts import literal_signature
from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.models import RelationshipPropertyValue, TypedLiteralValue
from graphrag_prod.ontology.models import (
    Cardinality,
    PropertyDataType,
    PropertyDefinition,
)

from .models import (
    ABoxRecordBatch,
    AssertionRecord,
    ContextPropertyEvidence,
    EntityIdentity,
    EntityMentionRecord,
    EvidenceReference,
    RecordRevision,
)
from .trust import (
    AuthorityLevel,
    GovernanceStatus,
    KnowledgeOrigin,
    SYSTEM_CANDIDATE_NAMESPACE,
    TrustMetadata,
)


MAX_RECORDS_PER_WRITE = 1_000
MAX_RECORDS_PER_READ = 500
CONTEXT_PROPERTY_KEYS = (
    "context_property_evidence_version", "context_property_evidence_json",
    "context_source_checksum", "context_mapping_checksum", "context_chunk_id",
    "context_char_start", "context_char_end", "context_evidence_text",
)


def context_evidence_guard(revision: str = "revision", *, version: str | None = None,
                           snapshot: str | None = None, document: str | None = None,
                           primary: str | None = None) -> str:
    """Cypher predicate: secondary source remains exact and equally authorized.

    The outer query remains responsible for the primary source lifecycle and
    principal ACL. Both evidences must use that same document/version/policy.
    """
    r = revision
    if version is not None:
        # Reuse the outer query's already authorized source boundary. Introducing
        # another free document/version/snapshot graph makes cold planning costly.
        membership = (f"""({snapshot}.tenant_id={r}.tenant_id
                AND {snapshot}.document_id={r}.document_id AND {snapshot}.version_id={r}.version_id
                AND {snapshot}.build_state IN ['PUBLISHED','RETIRED'] AND {snapshot}.retirement_id IS NULL
                AND EXISTS {{ MATCH ({snapshot})-[:INCLUDES_CHUNK]->(context_chunk) }})"""
            if snapshot is not None else f"""EXISTS {{
                MATCH (context_snapshot:KnowledgeSnapshot)-[:OF_VERSION]->({version})
                WHERE context_snapshot.tenant_id={r}.tenant_id
                  AND context_snapshot.document_id={r}.document_id
                  AND context_snapshot.version_id={r}.version_id
                  AND context_snapshot.build_state IN ['PUBLISHED','RETIRED']
                  AND context_snapshot.retirement_id IS NULL
                  AND EXISTS {{ MATCH (context_snapshot)-[:INCLUDES_CHUNK]->(context_chunk) }}
                  {f'AND EXISTS {{ MATCH (context_snapshot)-[:INCLUDES_CHUNK]->({primary}) }}' if primary else ''}
            }}""")
        document_guard = (f"""AND {document}.retirement_id IS NULL
                AND {document}.tenant_id={r}.tenant_id AND {document}.document_id={r}.document_id
                AND {document}.retirement_request_fingerprint IS NULL
                AND coalesce({document}.lifecycle_status,'ACTIVE')='ACTIVE'""" if document else "")
        return f"""({r}.context_property_evidence_json IS NULL OR (
          {r}.context_property_evidence_version='json-context-property:v1'
          AND COUNT {{ MATCH ({r})-[:CONTEXT_EVIDENCED_BY]->(:Chunk) }}=1
          AND {version}.checksum={r}.context_source_checksum
          AND {version}.tenant_id={r}.tenant_id AND {version}.document_id={r}.document_id
          AND {version}.version_id={r}.version_id
          AND {version}.retirement_id IS NULL
          AND coalesce({version}.lifecycle_status,'ACTIVE')='ACTIVE'
          {document_guard}
          AND EXISTS {{
            MATCH ({r})-[:CONTEXT_EVIDENCED_BY]->(context_chunk:Chunk)
            WHERE context_chunk.tenant_id={r}.tenant_id
              AND context_chunk.document_id={r}.document_id
              AND context_chunk.version_id={r}.version_id
              AND context_chunk.chunk_id={r}.context_chunk_id
              AND context_chunk.access_policy_id={r}.access_policy_id
              AND context_chunk.access_policy_version={r}.access_policy_version
              AND context_chunk.access_groups={r}.access_groups
              AND context_chunk.char_start<={r}.context_char_start
              AND {r}.context_char_start<{r}.context_char_end
              AND {r}.context_char_end<=context_chunk.char_end
              AND substring(context_chunk.text,{r}.context_char_start-context_chunk.char_start,
                            {r}.context_char_end-{r}.context_char_start)={r}.context_evidence_text
              AND EXISTS {{ MATCH ({version})-[:HAS_CHUNK]->(context_chunk) }}
              AND {membership}
          }}))"""
    return f"""({r}.context_property_evidence_json IS NULL OR (
      {r}.context_property_evidence_version = 'json-context-property:v1'
      AND COUNT {{ MATCH ({r})-[:CONTEXT_EVIDENCED_BY]->(:Chunk) }} = 1
      AND EXISTS {{
        MATCH ({r})-[:CONTEXT_EVIDENCED_BY]->(context_chunk:Chunk)
        WHERE context_chunk.tenant_id = {r}.tenant_id
          AND context_chunk.document_id = {r}.document_id
          AND context_chunk.version_id = {r}.version_id
          AND context_chunk.chunk_id = {r}.context_chunk_id
          AND context_chunk.access_policy_id = {r}.access_policy_id
          AND context_chunk.access_policy_version = {r}.access_policy_version
          AND context_chunk.access_groups = {r}.access_groups
          AND context_chunk.char_start <= {r}.context_char_start
          AND {r}.context_char_start < {r}.context_char_end
          AND {r}.context_char_end <= context_chunk.char_end
          AND substring(context_chunk.text, {r}.context_char_start - context_chunk.char_start,
                        {r}.context_char_end - {r}.context_char_start) = {r}.context_evidence_text
        WITH {r}, context_chunk LIMIT 1
        MATCH (context_version:DocumentVersion {{tenant_id:{r}.tenant_id,version_id:{r}.version_id}})
        WHERE context_version.document_id = {r}.document_id
          AND context_version.checksum = {r}.context_source_checksum
          AND context_version.retirement_id IS NULL
          AND coalesce(context_version.lifecycle_status,'ACTIVE') = 'ACTIVE'
          AND EXISTS {{ MATCH (context_version)-[:HAS_CHUNK]->(context_chunk) }}
        WITH {r}, context_chunk, context_version LIMIT 1
        MATCH (context_document:Document {{tenant_id:{r}.tenant_id,document_id:{r}.document_id}})
        WHERE context_document.retirement_id IS NULL
          AND context_document.retirement_request_fingerprint IS NULL
          AND coalesce(context_document.lifecycle_status,'ACTIVE') = 'ACTIVE'
          AND EXISTS {{ MATCH (context_document)-[:HAS_VERSION]->(context_version) }}
        WITH {r}, context_chunk, context_version LIMIT 1
        MATCH (context_snapshot:KnowledgeSnapshot)-[:INCLUDES_CHUNK]->(context_chunk)
        WHERE context_snapshot.tenant_id = {r}.tenant_id
          AND context_snapshot.document_id = {r}.document_id
          AND context_snapshot.version_id = {r}.version_id
          AND context_snapshot.build_state IN ['PUBLISHED','RETIRED']
          AND context_snapshot.retirement_id IS NULL
          AND EXISTS {{ MATCH (context_snapshot)-[:OF_VERSION]->(context_version) }}
          AND EXISTS {{ MATCH (context_snapshot)-[:INCLUDES_CHUNK]->(context_primary:Chunk)
              WHERE context_primary.chunk_id = coalesce({r}.chunk_id,{r}.evidence_chunk_id)
                AND context_primary.tenant_id = {r}.tenant_id }}
      }}))"""


def context_navigation_guard(navigation: str = "navigation", revision: str = "revision") -> str:
    """Match materialized context to the separately validated governed source."""
    return f"""(({revision}.context_property_evidence_json IS NULL
        AND COUNT {{ MATCH ({navigation})-[:CONTEXT_EVIDENCED_BY]->(:Chunk) }}=0)
        OR ({revision}.context_property_evidence_json IS NOT NULL
          AND COUNT {{ MATCH ({navigation})-[:CONTEXT_EVIDENCED_BY]->(:Chunk) }}=1
          AND EXISTS {{ MATCH ({navigation})-[:CONTEXT_EVIDENCED_BY]->(context_target:Chunk)
              WHERE context_target.tenant_id={revision}.tenant_id
                AND context_target.chunk_id={revision}.context_chunk_id }}))"""


def context_evidence_properties(assertion: AssertionRecord) -> dict[str, object]:
    context = assertion.context_property_evidence
    if context is None:
        return {}
    return {
        "context_property_evidence_version": context.version,
        "context_property_evidence_json": json.dumps(context.to_mapping(), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "context_source_checksum": context.source_checksum,
        "context_mapping_checksum": context.mapping_checksum,
        "context_chunk_id": context.value_evidence.chunk_id,
        "context_char_start": context.value_evidence.char_start,
        "context_char_end": context.value_evidence.char_end,
        "context_evidence_text": context.value_evidence.quoted_text,
    }


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
    properties: dict[str, object] = {
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
    if isinstance(record, EntityMentionRecord) and record.assignment_source_revision_id is not None:
        properties["assignment_source_revision_id"] = record.assignment_source_revision_id
    if isinstance(record, AssertionRecord):
        properties.update(context_evidence_properties(record))
        if record.fact_distinction is not None:
            properties["fact_distinction_json"] = json.dumps(record.fact_distinction.to_mapping(), ensure_ascii=False)
        if record.fact_key is not None:
            properties["fact_key"] = record.fact_key
        properties.update(
            relationship_properties_format_version=1,
            relationship_properties_json=json.dumps(
                [item.to_mapping() for item in record.relationship_properties],
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
    return properties


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
        assignment_source_revision_id=properties.get("assignment_source_revision_id"),
    )


def _stored_assertion(properties: dict[str, Any]) -> AssertionRecord:
    tenant_id = properties["tenant_id"]
    object_kind = properties["object_kind"]
    if object_kind not in {"entity", "literal"}:
        raise KnowledgeStoreError("stored assertion object_kind is invalid")
    context = None
    if properties.get("context_property_evidence_json") is not None:
        try:
            context = ContextPropertyEvidence.from_mapping(json.loads(properties["context_property_evidence_json"]))
            if properties.get("context_property_evidence_version") != context.version:
                raise ValueError("context codec version mismatch")
        except (KeyError, TypeError, ValueError) as exc:
            raise KnowledgeStoreError("stored context property evidence is invalid") from exc
    try:
        literal_semantics = TypedLiteralValue.from_flat_properties(properties)
    except (TypeError, ValueError) as exc:
        raise KnowledgeStoreError("stored typed literal semantics are invalid") from exc
    relationship_properties: tuple[RelationshipPropertyValue, ...] = ()
    properties_json = properties.get("relationship_properties_json")
    properties_version = properties.get("relationship_properties_format_version")
    if properties_json is not None or properties_version is not None:
        if properties_version != 1 or not isinstance(properties_json, str):
            raise KnowledgeStoreError(
                "stored relationship-property codec version is invalid"
            )
        try:
            decoded = json.loads(properties_json)
            if not isinstance(decoded, list):
                raise TypeError("relationship-property payload must be an array")
            relationship_properties = tuple(
                RelationshipPropertyValue.from_mapping(item) for item in decoded
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise KnowledgeStoreError(
                "stored relationship-property payload is invalid"
            ) from exc
    record = AssertionRecord(
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
        literal_semantics=(
            literal_semantics if object_kind == "literal" else None
        ),
        relationship_properties=relationship_properties,
        fact_distinction=decode_fact_distinction(properties.get("fact_distinction_json")),
        context_property_evidence=context,
    )
    if context is not None and any(properties.get(key) != value for key, value in context_evidence_properties(record).items()):
        raise KnowledgeStoreError("stored context evidence projection is inconsistent")
    if context is None and any(properties.get(key) is not None for key in CONTEXT_PROPERTY_KEYS):
        raise KnowledgeStoreError("stored context evidence codec is incomplete")
    return record


def _property_definition(properties: dict[str, Any]) -> PropertyDefinition:
    """Rebuild the immutable T-Box property contract returned by Neo4j."""

    try:
        return PropertyDefinition(
            name=properties["name"],
            datatype=PropertyDataType(properties["datatype"]),
            required=properties["required"],
            cardinality=Cardinality(properties["cardinality"]),
            unit=properties.get("unit"),
            constraints_json=properties.get("constraints_json"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise KnowledgeSchemaError(
            "published T-Box contains an invalid property definition"
        ) from exc


def _validate_literal_semantics(
    literal: TypedLiteralValue | None,
    definition: PropertyDefinition,
    *,
    allow_persisted_legacy: bool = False,
) -> None:
    """Recompute supplied semantics so canonical values are never trusted.

    ``None`` is only accepted at explicitly marked read/replay boundaries for
    assertions persisted before typed literal semantics existed.  New writes
    must never use that compatibility path.
    """

    if literal is None:
        if allow_persisted_legacy:
            return
        raise KnowledgeSchemaError(
            "new literal assertions require server-validated typed semantics"
        )
    # Import lazily to avoid a package initialization cycle: construction's
    # workflow itself depends on this persistence module.
    from graphrag_prod.construction.literals import (  # noqa: PLC0415
        LiteralNormalizationError,
        TBoxLiteralNormalizer,
    )

    try:
        normalizer = TBoxLiteralNormalizer()
        normalizer.validate_declared_unit(definition)
        normalized = normalizer.normalize(
            definition,
            raw_value=literal.raw_value,
            raw_unit=literal.raw_unit,
            valid_from=literal.raw_valid_from,
            valid_to=literal.raw_valid_to,
            observed_at=literal.raw_observed_at,
            source_encoding=literal.source_encoding,
        )
    except LiteralNormalizationError as exc:
        raise KnowledgeSchemaError(
            f"typed literal violates T-Box property semantics: {exc.code}"
        ) from exc
    if normalized != literal:
        raise KnowledgeSchemaError(
            "typed literal canonical or temporal values do not match server normalization"
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
  AND ($subject_entity_id IS NULL OR
       revision.subject_entity_id = $subject_entity_id)
  AND ($ontology_version_id IS NULL OR
       revision.ontology_version_id = $ontology_version_id)
  AND (size($predicates) = 0 OR revision.predicate IN $predicates)
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
_ASSERTION_READ_QUERY = _ASSERTION_READ_QUERY.replace(
    "RETURN revision {.*} AS revision", "AND " + context_evidence_guard(version="version", document="document", primary="chunk") + "\nRETURN revision {.*} AS revision"
)


class Neo4jKnowledgeStore:
    """Persist authoritative and model-derived A-Box records without mixing trust."""

    def __init__(self, driver: SessionDriver, database: str = "neo4j") -> None:
        self.driver = driver
        self.database = database

    def persist_context_candidates(
        self, principal: Principal, assertions: tuple[AssertionRecord, ...],
    ) -> KnowledgeWriteResult:
        """Append context candidates against existing mentions, never re-create them."""
        if "knowledge:construct" not in principal.capabilities:
            raise KnowledgeEvidenceError("context construction is not authorized")
        if not assertions or len(assertions) > MAX_RECORDS_PER_WRITE:
            raise ValueError("context candidate batch is empty or exceeds its bound")
        if len({r.record_id for r in assertions}) != len(assertions):
            raise ValueError("context candidate batch contains duplicate records")
        for record in assertions:
            if (record.tenant_id != principal.tenant_id
                    or not record.evidence.access_groups & principal.groups
                    or record.context_property_evidence is None
                    or record.trust.status is not GovernanceStatus.CANDIDATE
                    or record.revision.revision != 1
                    or record.trust.origin not in {KnowledgeOrigin.LLM_EXTRACTED, KnowledgeOrigin.AUTHORITATIVE_EXTRACTED}):
                raise KnowledgeEvidenceError("context candidate source or state is invalid")
            if (record.trust.origin is KnowledgeOrigin.AUTHORITATIVE_EXTRACTED
                    and "knowledge:import" not in principal.capabilities):
                raise KnowledgeEvidenceError("authoritative context construction is not authorized")
        with self.driver.session(database=self.database) as session:
            return session.execute_write(self._persist_context_candidates_tx, principal, assertions)

    @classmethod
    def _persist_context_candidates_tx(cls, tx: Any, principal: Principal,
                                      assertions: tuple[AssertionRecord, ...]) -> KnowledgeWriteResult:
        from .review import Neo4jKnowledgeReviewService
        Neo4jKnowledgeReviewService._lock_tenant_corpus_tx(tx, principal.tenant_id, min(r.created_at for r in assertions))
        mentions, pending, revision_ids = {}, [], []
        for record in assertions:
            row = tx.run("""
                MATCH (head:KnowledgeRecordHead {tenant_id:$tenant_id,record_kind:'ENTITY_MENTION'})
                  -[:CURRENT_REVISION]->(revision:GovernedEntityMentionRevision {revision_id:$revision_id})
                WHERE revision.tenant_id=$tenant_id
                  AND revision.governance_status IN ['CANDIDATE','APPROVED','PUBLISHED']
                  AND any(g IN $groups WHERE g IN revision.access_groups)
                RETURN revision{.*} AS revision
            """, tenant_id=principal.tenant_id, revision_id=record.subject_mention_revision_id,
                groups=sorted(principal.groups)).single()
            if row is None:
                raise KnowledgeConflict("context subject mention changed or is unavailable")
            mention = _stored_mention(dict(row["revision"]))
            if (mention.entity != record.subject or mention.trust.origin != record.trust.origin
                    or mention.trust.authority != record.trust.authority
                    or mention.trust.ontology_version_id != record.trust.ontology_version_id):
                raise KnowledgeEvidenceError("context candidate cannot change source identity or authority")
            cls._validate_evidence_tx(tx, mention.evidence, origin=mention.trust.origin)
            cls._validate_evidence_tx(tx, record.evidence, origin=record.trust.origin)
            cls.verify_context_property_tx(tx, record)
            existing = tx.run("""
                MATCH (r:GovernedAssertionRevision {tenant_id:$tenant_id,record_id:$record_id,revision:1})
                RETURN r{.*} AS revision
            """, tenant_id=principal.tenant_id, record_id=record.record_id).single()
            if existing is not None:
                stored = _stored_assertion(dict(existing["revision"]))
                base = tx.run("""MATCH (m:GovernedEntityMentionRevision {tenant_id:$tenant_id,revision_id:$id})
                    RETURN m.record_id AS record_id""", tenant_id=principal.tenant_id,
                    id=stored.subject_mention_revision_id).single()
                if (base is None or base["record_id"] != mention.record_id
                        or stored.evidence != record.evidence
                        or stored.predicate != record.predicate
                        or stored.literal_semantics != record.literal_semantics
                        or stored.context_property_evidence != record.context_property_evidence):
                    raise KnowledgeConflict("context candidate immutable identity conflicts")
                revision_ids.append(stored.revision_id)
                continue
            mentions[mention.revision_id] = mention
            pending.append(record)
            revision_ids.append(record.revision_id)
        if pending:
            batch = ABoxRecordBatch(principal.tenant_id, tuple(mentions.values()), tuple(pending))
            cls._validate_tbox_tx(tx, batch)
            for record in pending:
                cls._lock_head_tx(tx, record.revision, record.tenant_id, "ASSERTION", record.created_at)
                cls._create_assertion_revision_tx(tx, record, link_canonical_entities=False)
        return KnowledgeWriteResult(principal.tenant_id, assertions[0].trust.ontology_version_id,
                                    0, len(pending), tuple(revision_ids))

    @classmethod
    def verify_context_property_tx(cls, tx: Any, assertion: AssertionRecord) -> None:
        """Independently validate both source ranges and the declarative scope."""
        context = assertion.context_property_evidence
        if context is None:
            return
        cls._validate_evidence_tx(tx, assertion.evidence, origin=assertion.trust.origin)
        cls._validate_evidence_tx(tx, context.value_evidence, origin=assertion.trust.origin)
        row = tx.run("""
            MATCH (d:Document {tenant_id:$tenant_id,document_id:$document_id})
              -[:ACTIVE_VERSION]->(v:DocumentVersion {tenant_id:$tenant_id,version_id:$version_id})
            WHERE coalesce(d.lifecycle_status,'ACTIVE')='ACTIVE'
              AND coalesce(v.lifecycle_status,'ACTIVE')='ACTIVE'
              AND d.retirement_id IS NULL AND v.retirement_id IS NULL
            RETURN v.normalized_text AS text,v.checksum AS checksum
        """, tenant_id=assertion.tenant_id, document_id=assertion.evidence.document_id,
            version_id=assertion.evidence.version_id).single()
        if (row is None or row["checksum"] != context.source_checksum
                or not isinstance(row["text"], str)
                or hashlib.sha256(row["text"].encode()).hexdigest() != context.source_checksum):
            raise KnowledgeEvidenceError("context source version checksum changed")
        try:
            from graphrag_prod.construction.structured import locate_json
            from graphrag_prod.construction.context_mapping import validate_context_binding
            root = locate_json(row["text"])
            target = root.at(context.record_pointer)
            value = root.at(context.value_pointer)
            if (target is None or value is None or not isinstance(target.value, dict)
                    or (target.start, target.end) != (assertion.evidence.char_start, assertion.evidence.char_end)
                    or target.at(context.identity_pointer) is None
                    or target.at(context.identity_pointer).value != context.source_identity
                    or (value.start, value.end) != (context.value_evidence.char_start, context.value_evidence.char_end)):
                raise ValueError("context source paths do not match exact evidence")
            identity = target.at(context.identity_pointer)
            mention = tx.run("""MATCH (m:GovernedEntityMentionRevision {
                    tenant_id:$tenant_id,revision_id:$revision_id})
                RETURN m{.*} AS mention""", tenant_id=assertion.tenant_id,
                revision_id=assertion.subject_mention_revision_id).single()
            if mention is None:
                raise ValueError("context subject mention is unavailable")
            mention = dict(mention["mention"])
            expected_span = (identity.start + 1, identity.end - 1) if isinstance(identity.value, str) else (identity.start, identity.end)
            if (mention.get("entity_id") != assertion.subject.entity_id
                    or mention.get("chunk_id") != assertion.evidence.chunk_id
                    or mention.get("version_id") != assertion.evidence.version_id
                    or (mention.get("evidence_char_start"), mention.get("evidence_char_end")) != expected_span):
                raise ValueError("context subject does not belong to its declared source record")
            validate_context_binding(row["text"], context,
                subject_type=assertion.subject.entity_type, predicate=assertion.predicate,
                raw_literal=assertion.literal_value)
        except (TypeError, ValueError) as exc:
            raise KnowledgeEvidenceError("context source scope or binding is invalid") from exc

    def import_authoritative(self, batch: ABoxRecordBatch) -> KnowledgeWriteResult:
        """Import expert A-Box records that already passed authoritative review."""

        batch.require_authoritative_import()
        return self._write_batch(batch, publish_entity_profiles=True)

    def persist_llm_candidates(self, batch: ABoxRecordBatch) -> KnowledgeWriteResult:
        """Persist unreviewed LLM output in the candidate layer only."""

        batch.require_llm_candidates()
        return self._write_batch(batch, publish_entity_profiles=False)

    def persist_manual_candidates(self, batch: ABoxRecordBatch) -> KnowledgeWriteResult:
        """Persist explicitly submitted human facts without granting authority."""
        for record in (*batch.mentions, *batch.assertions):
            if (record.trust.origin is not KnowledgeOrigin.HUMAN_SUPPLEMENT
                    or record.trust.authority is not AuthorityLevel.SECONDARY
                    or record.trust.status is not GovernanceStatus.CANDIDATE
                    or record.trust.extractor_version is not None
                    or record.trust.prompt_version is not None):
                raise ValueError("manual persistence requires unreviewed human secondary records")
        return self._write_batch(batch, publish_entity_profiles=False)

    @staticmethod
    def require_rule_candidates(batch: ABoxRecordBatch) -> None:
        """Validate explicit rule provenance without relabelling it as LLM output."""
        for record in (*batch.mentions, *batch.assertions):
            trust = record.trust
            if (
                trust.origin is not KnowledgeOrigin.RULE_DERIVED
                or trust.authority is not AuthorityLevel.SECONDARY
                or trust.status is not GovernanceStatus.CANDIDATE
                or trust.extractor_version is None
            ):
                raise ValueError(
                    "rule persistence requires identified RULE_DERIVED + SECONDARY + CANDIDATE records"
                )

    def persist_rule_candidates(self, batch: ABoxRecordBatch) -> KnowledgeWriteResult:
        """Persist auditable rule output in the governed candidate layer only."""
        self.require_rule_candidates(batch)
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
            cls._validate_evidence_tx(tx, mention.evidence, origin=mention.trust.origin)
            cls._lock_head_tx(tx, mention.revision, mention.tenant_id, "ENTITY_MENTION", mention.created_at)
            if publish_entity_profiles:
                cls._merge_entity_tx(tx, mention.entity)
            cls._create_mention_revision_tx(
                tx,
                mention,
                link_canonical_entity=publish_entity_profiles,
            )

        for assertion in batch.assertions:
            cls._validate_evidence_tx(tx, assertion.evidence, origin=assertion.trust.origin)
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
                       coalesce(entity_type.instance_allowed, true) AS instance_allowed,
                       collect(
                           CASE WHEN property IS NULL THEN NULL
                           ELSE properties(property)
                           END
                       ) AS literal_properties
                """,
                tenant_id=batch.tenant_id,
                tbox_id=batch.ontology_version_id,
            )
        )
        entity_contracts = {
            row["name"]: {
                "namespaces": frozenset(row["namespaces"] or ()),
                "instance_allowed": row.get("instance_allowed", True),
                "literal_properties": {
                    definition.name: definition
                    for definition in (
                        _property_definition(dict(item))
                        for item in (row["literal_properties"] or ())
                    )
                },
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
                OPTIONAL MATCH (relationship_type)-[:DECLARES_PROPERTY]->
                               (property:TBoxPropertyDefinition)
                RETURN relationship_type.name AS name,
                       coalesce(relationship_type.instance_allowed, true) AS instance_allowed,
                       relationship_type.allowed_type_pairs_json AS allowed_type_pairs_json,
                       relationship_type.source_types AS source_types,
                       relationship_type.target_types AS target_types,
                       collect(
                           CASE WHEN property IS NULL THEN NULL
                           ELSE properties(property)
                           END
                       ) AS property_definitions
                """,
                tenant_id=batch.tenant_id,
                tbox_id=batch.ontology_version_id,
            )
        )
        relationship_contracts = {
            row["name"]: {
                "instance_allowed": row.get("instance_allowed", True),
                "allowed_type_pairs": json.loads(row.get("allowed_type_pairs_json") or "[]"),
                "source_types": frozenset(row["source_types"] or ()),
                "target_types": frozenset(row["target_types"] or ()),
                "properties": {
                    definition.name: definition
                    for definition in (
                        _property_definition(dict(item))
                        for item in (row.get("property_definitions") or ())
                    )
                },
            }
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
        origins = {
            record.trust.origin for record in (*batch.mentions, *batch.assertions)
        }
        if len(origins) != 1:
            raise KnowledgeSchemaError(
                "one A-Box write must use exactly one knowledge origin"
            )
        model_derived = next(iter(origins)) in {
            KnowledgeOrigin.LLM_EXTRACTED, KnowledgeOrigin.AUTHORITATIVE_EXTRACTED,
            KnowledgeOrigin.MAPPED, KnowledgeOrigin.AUTHORITATIVE_MAPPED,
            KnowledgeOrigin.HUMAN_SUPPLEMENT,
        }
        for entity in entities.values():
            contract = entity_contracts.get(entity.entity_type)
            if contract is None:
                raise KnowledgeSchemaError(
                    f"entity type {entity.entity_type!r} is not declared by the T-Box"
                )
            if not contract["instance_allowed"]:
                raise KnowledgeSchemaError("abstract or external-derived entity type cannot be written as an instance")
            namespace, separator, _ = entity.canonical_key.partition(":")
            normalized_namespace = namespace.casefold()
            namespace_is_declared = normalized_namespace in contract["namespaces"]
            allowed_namespace = namespace_is_declared and (
                normalized_namespace == SYSTEM_CANDIDATE_NAMESPACE
                if model_derived
                else normalized_namespace != SYSTEM_CANDIDATE_NAMESPACE
            )
            if not separator or not allowed_namespace:
                raise KnowledgeSchemaError(
                    f"canonical key namespace for {entity.entity_type!r} is not allowed"
                )

        literal_counts: dict[tuple[str, str], set[tuple]] = {}
        for assertion in batch.assertions:
            if assertion.object_entity is None:
                definitions = entity_contracts[assertion.subject.entity_type][
                    "literal_properties"
                ]
                definition = definitions.get(assertion.predicate)
                if definition is None:
                    raise KnowledgeSchemaError(
                        f"literal predicate {assertion.predicate!r} is not declared "
                        f"on {assertion.subject.entity_type!r}"
                    )
                _validate_literal_semantics(
                    assertion.literal_semantics,
                    definition,
                )
                key = (assertion.subject.entity_id, assertion.predicate)
                literal_counts.setdefault(key, set()).add(literal_signature(assertion.literal_semantics))
                if definition.cardinality.single_valued and len(literal_counts[key]) > 1:
                    raise KnowledgeSchemaError(
                        f"literal predicate {assertion.predicate!r} exceeds its "
                        "single-valued T-Box cardinality in this batch"
                    )
                continue
            relationship = relationship_contracts.get(assertion.predicate)
            if relationship is None:
                raise KnowledgeSchemaError(
                    f"relationship {assertion.predicate!r} is not declared by the T-Box"
                )
            if (
                not relationship["instance_allowed"]
                or (relationship["allowed_type_pairs"] and [assertion.subject.entity_type, assertion.object_entity.entity_type] not in relationship["allowed_type_pairs"])
                or assertion.subject.entity_type not in relationship["source_types"]
                or assertion.object_entity.entity_type
                not in relationship["target_types"]
            ):
                raise KnowledgeSchemaError(
                    f"relationship {assertion.predicate!r} violates its domain/range"
                )
            property_counts: dict[str, int] = {}
            for value in assertion.relationship_properties:
                definition = relationship["properties"].get(value.name)
                if definition is None:
                    raise KnowledgeSchemaError(
                        f"relationship property {assertion.predicate}.{value.name} "
                        "is not declared by the T-Box"
                    )
                _validate_literal_semantics(value.literal_semantics, definition)
                property_counts[value.name] = property_counts.get(value.name, 0) + 1
            from graphrag_prod.ontology.source import validate_conditional_properties  # noqa: PLC0415
            try:
                validate_conditional_properties(relationship["properties"].values(), {item.name: item.literal_semantics.typed_value for item in assertion.relationship_properties})
            except ValueError as exc:
                raise KnowledgeSchemaError(str(exc)) from exc
            for name, definition in relationship["properties"].items():
                count = property_counts.get(name, 0)
                if definition.cardinality.required and count == 0:
                    raise KnowledgeSchemaError(
                        f"required relationship property {assertion.predicate}.{name} "
                        "is absent"
                    )
                if definition.cardinality.single_valued and count > 1:
                    raise KnowledgeSchemaError(
                        f"relationship property {assertion.predicate}.{name} exceeds "
                        "its single-valued T-Box cardinality in this batch"
                    )

    @staticmethod
    def _validate_evidence_tx(tx: Any, evidence: EvidenceReference, *, origin: KnowledgeOrigin | None = None) -> None:
        row = tx.run(
            """
            MATCH (document:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })-[:ACTIVE_VERSION]->(version:DocumentVersion {
                tenant_id: $tenant_id,
                version_id: $version_id
            })
            MATCH (document)-[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot {
                tenant_id: $tenant_id,
                document_id: $document_id,
                version_id: $version_id,
                build_state: 'PUBLISHED'
            })-[:OF_VERSION]->(version)
            MATCH (document)-[:HAS_VERSION]->(version)-[:HAS_CHUNK]->
                  (chunk:Chunk {
                      tenant_id: $tenant_id,
                      chunk_id: $chunk_id
                  })
            MATCH (snapshot)-[:INCLUDES_CHUNK]->(chunk)
            WHERE version.document_id = document.document_id
              AND chunk.document_id = document.document_id
              AND chunk.version_id = version.version_id
            RETURN document.canonical_uri AS source_uri,
                   chunk.char_start AS chunk_char_start,
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
        if origin is not None:
            human_source = str(row.get("source_uri", "")).startswith("urn:graphrag:human:")
            if human_source != (origin is KnowledgeOrigin.HUMAN_SUPPLEMENT):
                raise KnowledgeEvidenceError("knowledge origin does not match the source kind")
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
        *,
        source_aliases: bool = False,
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
        if not source_aliases and stored_aliases is not None and tuple(stored_aliases) != entity.aliases:
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
        Neo4jKnowledgeStore.verify_context_property_tx(tx, assertion)
        # AssertionRecord deliberately remains able to decode legacy stored
        # revisions.  The write boundary is stricter: every newly-created
        # literal revision must carry the complete server-normalized contract.
        if (
            assertion.object_entity is None
            and assertion.literal_semantics is None
        ):
            raise KnowledgeSchemaError(
                "new literal assertion revisions require typed semantics"
            )
        properties = {
            **_revision_properties(assertion),
            **_entity_properties(assertion.subject, "subject_"),
            "predicate": assertion.predicate,
            "subject_mention_revision_id": assertion.subject_mention_revision_id,
            "object_kind": assertion.object_kind,
            "literal_value": assertion.literal_value,
            "object_mention_revision_id": assertion.object_mention_revision_id,
        }
        if assertion.literal_semantics is not None:
            properties.update(assertion.literal_semantics.to_flat_properties())
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
        if assertion.context_property_evidence is not None:
            linked = tx.run("""
                MATCH (r:GovernedAssertionRevision {tenant_id:$tenant_id,revision_id:$id})
                MATCH (c:Chunk {tenant_id:$tenant_id,chunk_id:$chunk_id})
                CREATE (r)-[:CONTEXT_EVIDENCED_BY]->(c)
                RETURN r.revision_id AS id
            """, tenant_id=assertion.tenant_id, id=assertion.revision_id,
                chunk_id=assertion.context_property_evidence.value_evidence.chunk_id).single()
            if linked is None:
                raise KnowledgeEvidenceError("context evidence link is unavailable")

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
            subject_entity_id=None,
            ontology_version_id=None,
            predicates=(),
            statuses=statuses,
            limit=limit,
        )

    def list_identity_property_assertions(
        self,
        principal: Principal,
        *,
        subject_entity_id: str,
        ontology_version_id: str,
        predicates: tuple[str, ...],
        statuses: Iterable[GovernanceStatus],
    ) -> tuple[AssertionRecord, ...]:
        """Read current candidate identity facts without scanning the tenant queue."""

        entity_id = subject_entity_id.strip()
        tbox_id = ontology_version_id.strip()
        normalized_predicates = tuple(
            dict.fromkeys(item.strip() for item in predicates if item.strip())
        )
        if not entity_id or not tbox_id or not normalized_predicates:
            raise ValueError(
                "identity-property lookup requires entity, T-Box, and predicates"
            )
        return self._read_assertions(
            principal,
            record_id=None,
            subject_entity_id=entity_id,
            ontology_version_id=tbox_id,
            predicates=normalized_predicates,
            statuses=statuses,
            limit=min(MAX_RECORDS_PER_READ, len(normalized_predicates) + 1),
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
            subject_entity_id=None,
            ontology_version_id=None,
            predicates=(),
            statuses=statuses,
            limit=1,
        )
        return records[0] if records else None

    def _read_assertions(
        self,
        principal: Principal,
        *,
        record_id: str | None,
        subject_entity_id: str | None,
        ontology_version_id: str | None,
        predicates: tuple[str, ...],
        statuses: Iterable[GovernanceStatus] | None,
        limit: int,
    ) -> tuple[AssertionRecord, ...]:
        if record_id is not None and not record_id:
            raise ValueError("record_id must not be empty")
        parameters = {
            "tenant_id": principal.tenant_id,
            "groups": sorted(principal.groups),
            "record_id": record_id,
            "subject_entity_id": subject_entity_id,
            "ontology_version_id": ontology_version_id,
            "predicates": list(predicates),
            "statuses": _normalized_statuses(statuses),
            "limit": _read_limit(limit),
        }
        with self.driver.session(database=self.database) as session:
            rows = session.run(_ASSERTION_READ_QUERY, **parameters)
            return tuple(_stored_assertion(dict(row["revision"])) for row in rows)
