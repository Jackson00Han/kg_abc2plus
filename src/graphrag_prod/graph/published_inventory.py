"""Safe, bounded inventory of the active governed A-Box publication.

The inventory is an operational projection, not a second source of truth.  It
starts from the unique active :class:`KnowledgePublication`, requires the
public governed-graph quality audit to pass, and then projects only identifiers,
trust metadata, canonical entity labels, typed literal values, and exact source
locations.  Source/evidence text is deliberately never returned.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from neo4j import unit_of_work

from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.models import RelationshipPropertyValue

from .published_quality import (
    PUBLISHED_QUALITY_CAPABILITIES,
    Neo4jPublishedGraphQualityService,
    PublishedGraphQualityAuthorizationError,
    PublishedGraphQualityConflict,
    PublishedGraphQualityError,
    PublishedGraphQualityLimitExceeded,
    PublishedGraphQualityReport,
)


MAX_ACTIVE_PUBLICATION_INVENTORY_ITEMS = 500


class SessionDriver(Protocol):
    def session(self, **kwargs: object) -> Any: ...


class PublishedQualityService(Protocol):
    def audit(self, principal: Principal) -> PublishedGraphQualityReport: ...


class ActivePublicationInventoryError(RuntimeError):
    """Base error whose public message never contains graph or source data."""

    code = "ACTIVE_PUBLICATION_INVENTORY_ERROR"

    def __init__(self, message: str = "active publication inventory failed") -> None:
        super().__init__(message)


class ActivePublicationInventoryAuthorizationError(
    ActivePublicationInventoryError, PermissionError
):
    code = "COMPLETE_PUBLICATION_ACCESS_REQUIRED"

    def __init__(self) -> None:
        super().__init__(
            "principal is not authorized to inspect the complete active publication"
        )


class ActivePublicationInventoryConflict(ActivePublicationInventoryError):
    code = "ACTIVE_PUBLICATION_INVENTORY_CONFLICT"

    def __init__(self) -> None:
        super().__init__(
            "the active governed publication is unavailable, incomplete, or conflicted"
        )


class ActivePublicationInventoryLimitExceeded(ActivePublicationInventoryError):
    code = "ACTIVE_PUBLICATION_INVENTORY_LIMIT_EXCEEDED"

    def __init__(self) -> None:
        super().__init__("active publication exceeds an inventory safety bound")


class ActivePublicationInventoryUnavailable(ActivePublicationInventoryError):
    code = "ACTIVE_PUBLICATION_INVENTORY_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("active publication inventory is temporarily unavailable")


@dataclass(frozen=True, slots=True)
class InventoryEntitySummary:
    entity_id: str
    entity_type: str
    canonical_key: str
    display_name: str


@dataclass(frozen=True, slots=True)
class InventoryLiteralSummary:
    """Safe fact value projection; raw evidence text is intentionally absent."""

    value: str
    datatype: str | None = None
    typed_value: str | int | float | bool | None = None
    canonical_value: str | None = None
    canonical_unit: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    observed_at: str | None = None


@dataclass(frozen=True, slots=True)
class InventoryRelationshipPropertySummary:
    property_value_id: str
    name: str
    confidence: float
    literal: InventoryLiteralSummary
    evidence_chunk_id: str
    evidence_chunk_ordinal: int
    evidence_char_start: int
    evidence_char_end: int


@dataclass(frozen=True, slots=True)
class InventoryAssertionSummary:
    subject: InventoryEntitySummary
    predicate: str
    object_kind: str
    object_entity: InventoryEntitySummary | None = None
    literal: InventoryLiteralSummary | None = None
    relationship_properties: tuple[InventoryRelationshipPropertySummary, ...] = ()


@dataclass(frozen=True, slots=True)
class ActivePublicationInventoryItem:
    record_id: str
    revision_id: str
    record_kind: str
    governance_status: str
    origin: str
    authority_level: str
    confidence: float
    ontology_key: str
    document_id: str
    version_id: str
    chunk_id: str
    evidence_chunk_ordinal: int
    evidence_char_start: int
    evidence_char_end: int
    entity: InventoryEntitySummary | None = None
    assertion: InventoryAssertionSummary | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ActivePublicationInventory:
    tenant_id: str
    publication_id: str
    publication_generation: int
    manifest_hash: str
    ontology_version_id: str
    document_id: str | None
    total_record_count: int
    matching_record_count: int
    truncated: bool
    items: tuple[ActivePublicationInventoryItem, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["items"] = [item.to_dict() for item in self.items]
        return value


_MANIFEST_QUERY = """
// active-publication-inventory:manifest
MATCH (publication:KnowledgePublication {
    tenant_id: $tenant_id,
    publication_id: $publication_id,
    generation: $publication_generation,
    manifest_hash: $manifest_hash,
    ontology_version_id: $ontology_version_id,
    status: 'ACTIVE'
})
WHERE COUNT {
          MATCH (:KnowledgePublicationState {tenant_id: $tenant_id})
      } = 1
  AND COUNT {
          MATCH (:KnowledgePublicationState {tenant_id: $tenant_id})
                -[:ACTIVE_KNOWLEDGE_PUBLICATION]->
                (:KnowledgePublication {tenant_id: $tenant_id})
      } = 1
  AND COUNT {
          MATCH (:KnowledgePublicationState {tenant_id: $tenant_id})
                -[:ACTIVE_KNOWLEDGE_PUBLICATION]->(publication)
      } = 1
  AND COUNT {
          MATCH (publication)-[:USES_TBOX_VERSION]->(:TBoxVersion)
      } = 1
  AND COUNT {
          MATCH (publication)-[:USES_TBOX_VERSION]->(:TBoxVersion {
              tenant_id: $tenant_id,
              tbox_id: $ontology_version_id,
              checksum: $tbox_checksum
          })
      } = 1
CALL (publication) {
    MATCH (publication)-[membership:PUBLISHES_KNOWLEDGE_REVISION]->(revision)
    RETURN count(membership) AS membership_count,
           count(DISTINCT revision) AS distinct_revision_count,
           collect(DISTINCT revision.revision_id) AS membership_revision_ids,
           count(CASE
               WHEN revision.tenant_id = $tenant_id
                AND revision.governance_status = 'PUBLISHED'
                AND (revision:GovernedEntityMentionRevision
                     OR revision:GovernedAssertionRevision)
               THEN 1
           END) AS valid_revision_count
}
RETURN publication.published_revision_ids AS manifest_revision_ids,
       membership_count,
       distinct_revision_count,
       membership_revision_ids,
       valid_revision_count
"""


_ITEMS_QUERY = """
// active-publication-inventory:items
MATCH (:KnowledgePublicationState {tenant_id: $tenant_id})
      -[:ACTIVE_KNOWLEDGE_PUBLICATION]->
      (publication:KnowledgePublication {
          tenant_id: $tenant_id,
          publication_id: $publication_id,
          generation: $publication_generation,
          manifest_hash: $manifest_hash,
          ontology_version_id: $ontology_version_id,
          status: 'ACTIVE'
      })
MATCH (publication)-[published:PUBLISHES_KNOWLEDGE_REVISION]->(revision)
WITH publication, published, revision, labels(revision) AS revision_labels
ORDER BY CASE WHEN revision:GovernedEntityMentionRevision
              THEN 'ENTITY_MENTION' ELSE 'ASSERTION' END,
         revision.record_id,
         revision.revision_id
LIMIT $row_limit
CALL (revision) {
    OPTIONAL MATCH (head:KnowledgeRecordHead {record_id: revision.record_id})
          -[pointer:CURRENT_REVISION]->(current)
    RETURN count(DISTINCT head) AS head_count,
           count(pointer) AS current_pointer_count,
           count(CASE WHEN current = revision THEN 1 END) AS matching_current_count,
           min(head.tenant_id) AS head_tenant_id,
           min(head.record_kind) AS head_record_kind,
           min(head.current_revision) AS head_current_revision
}
CALL (publication, revision) {
    OPTIONAL MATCH (revision)-[evidence:IN_CHUNK|EVIDENCED_BY]->(chunk:Chunk)
    OPTIONAL MATCH (document:Document {
        tenant_id: $tenant_id,
        document_id: revision.document_id
    })-[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot {
        tenant_id: $tenant_id,
        build_state: 'PUBLISHED'
    })-[:INCLUDES_CHUNK]->(chunk)
    OPTIONAL MATCH (document)-[:ACTIVE_VERSION]->(version:DocumentVersion)
    OPTIONAL MATCH (snapshot)-[:OF_VERSION]->(snapshot_version:DocumentVersion)
    WITH publication, revision, evidence, chunk, document, snapshot, version,
         snapshot_version,
         CASE WHEN evidence IS NOT NULL
                   AND chunk.tenant_id = $tenant_id
                   AND revision.version_id = version.version_id
                   AND revision.version_id = snapshot_version.version_id
                   AND revision.chunk_id = chunk.chunk_id
                   AND revision.access_policy_id = chunk.access_policy_id
                   AND revision.access_policy_version = chunk.access_policy_version
                   AND revision.access_groups = chunk.access_groups
                   AND revision.evidence_char_start >= chunk.char_start
                   AND revision.evidence_char_start < revision.evidence_char_end
                   AND revision.evidence_char_end <= chunk.char_end
                   AND substring(
                       chunk.text,
                       revision.evidence_char_start - chunk.char_start,
                       revision.evidence_char_end - revision.evidence_char_start
                   ) = revision.evidence_text
                   AND any(group IN $groups
                           WHERE group IN revision.access_groups)
                   AND any(group IN $groups
                           WHERE group IN chunk.access_groups)
                   AND any(group IN $groups
                           WHERE group IN document.access_groups)
                   AND EXISTS {
                       MATCH (publication)-[:USES_KNOWLEDGE_SNAPSHOT]->(snapshot)
                   }
              THEN 1 ELSE 0 END AS valid_path
    RETURN count(DISTINCT evidence) AS evidence_link_count,
           count(DISTINCT chunk) AS evidence_chunk_count,
           count(DISTINCT document) AS evidence_document_count,
           sum(valid_path) AS valid_evidence_path_count,
           min(chunk.ordinal) AS evidence_chunk_ordinal
}
CALL (revision) {
    OPTIONAL MATCH (revision)-[:REFERS_TO]->(entity:Entity)
    RETURN count(entity) AS mention_entity_link_count,
           head(collect(entity {
               .entity_id, .tenant_id, .entity_type, .canonical_key,
               .canonical_name
           })) AS mention_entity
}
CALL (revision) {
    OPTIONAL MATCH (revision)-[:SUBJECT]->(subject:Entity)
    RETURN count(subject) AS subject_link_count,
           head(collect(subject {
               .entity_id, .tenant_id, .entity_type, .canonical_key,
               .canonical_name
           })) AS subject_entity
}
CALL (revision) {
    OPTIONAL MATCH (revision)-[:OBJECT]->(object:Entity)
    RETURN count(object) AS object_link_count,
           head(collect(object {
               .entity_id, .tenant_id, .entity_type, .canonical_key,
               .canonical_name
           })) AS object_entity
}
CALL (publication, revision) {
    OPTIONAL MATCH (navigation:EntityMention {
        tenant_id: $tenant_id,
        governed_publication_id: publication.publication_id,
        governed_revision_id: revision.revision_id
    })
    OPTIONAL MATCH (publication)-[:USES_KNOWLEDGE_SNAPSHOT]->
          (snapshot:KnowledgeSnapshot)-[membership:INCLUDES_MENTION]->(navigation)
    WHERE membership.governed_publication_id = publication.publication_id
    WITH revision, navigation, membership,
         CASE WHEN navigation IS NOT NULL
                   AND navigation.chunk_id = revision.chunk_id
                   AND navigation.entity_id = revision.entity_id
                   AND navigation.entity_type = revision.entity_type
                   AND navigation.surface = revision.surface
                   AND navigation.char_start = revision.evidence_char_start
                   AND navigation.char_end = revision.evidence_char_end
                   AND navigation.extractor_version = coalesce(
                       revision.extractor_version,
                       revision.origin + ':reviewed'
                   )
                   AND navigation.confidence = revision.confidence
                   AND navigation.authority_level = revision.authority_level
                   AND navigation.governance_status = 'ACCEPTED_BY_REVIEW'
                   AND membership.confidence = revision.confidence
                   AND COUNT {
                       MATCH (navigation)-[:IN_CHUNK]->(target:Chunk)
                       WHERE target.tenant_id = $tenant_id
                         AND target.chunk_id = revision.chunk_id
                   } = 1
                   AND COUNT {
                       MATCH (navigation)-[:IN_CHUNK]->(:Chunk)
                   } = 1
                   AND COUNT {
                       MATCH (navigation)-[:REFERS_TO]->(target:Entity)
                       WHERE target.tenant_id = $tenant_id
                         AND target.entity_id = revision.entity_id
                         AND target.entity_type = revision.entity_type
                   } = 1
                   AND COUNT {
                       MATCH (navigation)-[:REFERS_TO]->(:Entity)
                   } = 1
              THEN 1 ELSE 0 END AS valid_projection
    RETURN count(DISTINCT navigation) AS navigation_mention_count,
           count(membership) AS mention_membership_count,
           sum(valid_projection) AS valid_mention_projection_count
}
CALL (publication, revision) {
    OPTIONAL MATCH (navigation:Assertion {
        tenant_id: $tenant_id,
        governed_publication_id: publication.publication_id,
        governed_revision_id: revision.revision_id
    })
    OPTIONAL MATCH (publication)-[:USES_KNOWLEDGE_SNAPSHOT]->
          (snapshot:KnowledgeSnapshot)-[membership:INCLUDES_ASSERTION]->(navigation)
    WHERE membership.governed_publication_id = publication.publication_id
    WITH revision, navigation, membership,
         CASE WHEN navigation IS NOT NULL
                   AND navigation.subject_entity_id = revision.subject_entity_id
                   AND (
                       (revision.object_kind = 'entity'
                        AND navigation.object_entity_id = revision.object_entity_id)
                       OR
                       (revision.object_kind = 'literal'
                        AND navigation.object_entity_id IS NULL
                        AND revision.object_entity_id IS NULL)
                   )
                   AND navigation.predicate = revision.predicate
                   AND navigation.object_kind = revision.object_kind
                   AND navigation.literal_value = coalesce(revision.literal_value, '')
                   AND navigation.document_id = revision.document_id
                   AND navigation.version_id = revision.version_id
                   AND navigation.evidence_chunk_id = revision.chunk_id
                   AND navigation.evidence_char_start = revision.evidence_char_start
                   AND navigation.evidence_char_end = revision.evidence_char_end
                   AND navigation.evidence_text = revision.evidence_text
                   AND navigation.access_policy_id = revision.access_policy_id
                   AND navigation.access_policy_version =
                       revision.access_policy_version
                   AND navigation.access_groups = revision.access_groups
                   AND navigation.extractor_version = coalesce(
                       revision.extractor_version,
                       revision.origin + ':reviewed'
                   )
                   AND navigation.confidence = revision.confidence
                   AND navigation.authority_level = revision.authority_level
                   AND navigation.schema_version = revision.ontology_version_id
                   AND navigation.governance_status = 'ACCEPTED_BY_REVIEW'
                   AND navigation.publication_state = 'GOVERNED_PUBLISHED'
                   AND navigation.accepted = true
                   AND membership.confidence = revision.confidence
                   AND membership.accepted = true
                   AND all(key IN [
                       'literal_datatype',
                       'literal_typed_value',
                       'literal_raw_value',
                       'literal_raw_unit',
                       'literal_canonical_value',
                       'literal_canonical_unit',
                       'literal_valid_from',
                       'literal_valid_to',
                       'literal_observed_at',
                       'literal_raw_valid_from',
                       'literal_raw_valid_to',
                       'literal_raw_observed_at',
                       'relationship_properties_format_version',
                       'relationship_properties_json'
                   ] WHERE
                       (navigation[key] IS NULL AND revision[key] IS NULL)
                       OR navigation[key] = revision[key]
                   )
                   AND COUNT {
                       MATCH (navigation)-[:SUBJECT]->(target:Entity)
                       WHERE target.tenant_id = $tenant_id
                         AND target.entity_id = revision.subject_entity_id
                         AND target.entity_type = revision.subject_entity_type
                   } = 1
                   AND COUNT {
                       MATCH (navigation)-[:SUBJECT]->(:Entity)
                   } = 1
                   AND COUNT {
                       MATCH (navigation)-[:EVIDENCED_BY]->(target:Chunk)
                       WHERE target.tenant_id = $tenant_id
                         AND target.chunk_id = revision.chunk_id
                   } = 1
                   AND COUNT {
                       MATCH (navigation)-[:EVIDENCED_BY]->(:Chunk)
                   } = 1
                   AND (
                       (revision.object_kind = 'entity'
                        AND COUNT {
                            MATCH (navigation)-[:OBJECT]->(target:Entity)
                            WHERE target.tenant_id = $tenant_id
                              AND target.entity_id = revision.object_entity_id
                              AND target.entity_type = revision.object_entity_type
                        } = 1
                        AND COUNT {
                            MATCH (navigation)-[:OBJECT]->(:Entity)
                        } = 1)
                       OR
                       (revision.object_kind = 'literal'
                        AND COUNT {
                            MATCH (navigation)-[:OBJECT]->(:Entity)
                        } = 0)
                   )
              THEN 1 ELSE 0 END AS valid_projection
    RETURN count(DISTINCT navigation) AS navigation_assertion_count,
           count(membership) AS assertion_membership_count,
           sum(valid_projection) AS valid_assertion_projection_count
}
RETURN revision {
           .record_id, .revision_id, .revision, .tenant_id,
           .governance_status, .origin, .authority_level, .confidence,
           .ontology_version_id,
           .document_id, .version_id, .chunk_id,
           .access_policy_id, .access_policy_version, .access_groups,
           .evidence_char_start, .evidence_char_end,
           .extractor_version,
           .entity_id, .entity_type, .canonical_key, .canonical_name,
           .subject_entity_id, .subject_entity_type,
           .subject_canonical_key, .subject_canonical_name,
           .predicate, .object_kind,
           .object_entity_id, .object_entity_type,
           .object_canonical_key, .object_canonical_name,
           .literal_value, .literal_datatype, .literal_typed_value,
           .literal_canonical_value, .literal_canonical_unit,
           .literal_valid_from, .literal_valid_to, .literal_observed_at,
           .relationship_properties_format_version,
           .relationship_properties_json
       } AS revision,
       revision_labels,
       published.record_kind AS publication_record_kind,
       head_count,
       current_pointer_count,
       matching_current_count,
       head_tenant_id,
       head_record_kind,
       head_current_revision,
       evidence_link_count,
       evidence_chunk_count,
       evidence_document_count,
       valid_evidence_path_count,
       evidence_chunk_ordinal,
       mention_entity_link_count,
       mention_entity,
       subject_link_count,
       subject_entity,
       object_link_count,
       object_entity,
       navigation_mention_count,
       mention_membership_count,
       valid_mention_projection_count,
       navigation_assertion_count,
       assertion_membership_count,
       valid_assertion_projection_count
"""


_PROPERTY_VALUES_QUERY = """
// active-publication-inventory:relationship-properties
UNWIND $assertion_revision_ids AS revision_id
MATCH (:KnowledgePublicationState {tenant_id: $tenant_id})
      -[:ACTIVE_KNOWLEDGE_PUBLICATION]->
      (publication:KnowledgePublication {
          tenant_id: $tenant_id,
          publication_id: $publication_id,
          generation: $publication_generation,
          manifest_hash: $manifest_hash,
          ontology_version_id: $ontology_version_id,
          status: 'ACTIVE'
      })
MATCH (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->
      (revision:GovernedAssertionRevision {
          tenant_id: $tenant_id,
          revision_id: revision_id,
          governance_status: 'PUBLISHED'
      })
OPTIONAL MATCH (navigation:Assertion {
    tenant_id: $tenant_id,
    governed_publication_id: publication.publication_id,
    governed_revision_id: revision.revision_id
})
CALL (navigation) {
    OPTIONAL MATCH (navigation)-[property_link:HAS_RELATIONSHIP_PROPERTY]->
          (value:RelationshipPropertyValue)
    OPTIONAL MATCH (value)-[evidence:EVIDENCED_BY]->(chunk:Chunk)
    WITH property_link, value,
         count(evidence) AS evidence_link_count,
         count(DISTINCT chunk) AS evidence_chunk_count,
         collect(DISTINCT CASE WHEN chunk IS NULL THEN NULL ELSE chunk {
             .tenant_id, .chunk_id, .ordinal, .char_start, .char_end
         } END) AS evidence_chunks,
         CASE WHEN count(evidence) = 1
                   AND count(DISTINCT chunk) = 1
                   AND value IS NOT NULL
                   AND value.evidence_char_start >= min(chunk.char_start)
                   AND value.evidence_char_start < value.evidence_char_end
                   AND value.evidence_char_end <= max(chunk.char_end)
                   AND substring(
                       head(collect(chunk.text)),
                       value.evidence_char_start - min(chunk.char_start),
                       value.evidence_char_end - value.evidence_char_start
                   ) = value.evidence_text
              THEN true ELSE false END AS exact_evidence
    ORDER BY property_link.ordinal, value.property_value_id
    RETURN count(property_link) AS property_link_count,
           count(DISTINCT value) AS property_node_count,
           collect(CASE WHEN value IS NULL THEN NULL ELSE {
               ordinal: property_link.ordinal,
               node_properties: properties(value),
               evidence_link_count: evidence_link_count,
               evidence_chunk_count: evidence_chunk_count,
               evidence_chunks: evidence_chunks,
               exact_evidence: exact_evidence
           } END) AS property_values
}
RETURN revision.revision_id AS revision_id,
       navigation.assertion_id AS navigation_assertion_id,
       property_link_count,
       property_node_count,
       property_values
ORDER BY revision.revision_id, navigation.assertion_id
"""


def _required_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ActivePublicationInventoryConflict()
    return value.strip()


def _optional_filter(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("document_id must be a string or None")
    normalized = value.strip()
    if not normalized:
        raise ValueError("document_id must not be empty")
    if len(normalized) > 512 or any(
        character in normalized for character in ("\x00", "\r", "\n")
    ):
        raise ValueError("document_id is invalid")
    return normalized


def _limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("limit must be an integer")
    if not 1 <= value <= MAX_ACTIVE_PUBLICATION_INVENTORY_ITEMS:
        raise ValueError(
            "limit must be between 1 and "
            f"{MAX_ACTIVE_PUBLICATION_INVENTORY_ITEMS}"
        )
    return value


@dataclass(frozen=True, slots=True)
class _PropertyExpectation:
    ordinal: int
    node_properties: dict[str, Any]
    evidence_chunk_id: str
    evidence_chunk_ordinal: int
    summary: InventoryRelationshipPropertySummary


def _relationship_property_expectations(
    row: dict[str, Any],
) -> tuple[_PropertyExpectation, ...]:
    """Decode immutable revision JSON and bind it to the parent evidence scope."""

    revision_value = row.get("revision")
    revision = dict(revision_value) if revision_value is not None else {}
    version = revision.get("relationship_properties_format_version")
    payload = revision.get("relationship_properties_json")
    if version is None and payload is None:
        values: tuple[RelationshipPropertyValue, ...] = ()
    elif version == 1 and isinstance(payload, str):
        try:
            decoded = json.loads(payload)
            if not isinstance(decoded, list):
                raise TypeError("relationship-property payload must be an array")
            values = tuple(
                RelationshipPropertyValue.from_mapping(value) for value in decoded
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ActivePublicationInventoryConflict() from exc
    else:
        raise ActivePublicationInventoryConflict()

    ordered = tuple(
        sorted(
            values,
            key=lambda item: (
                item.name,
                item.literal_semantics.identity_reference,
                item.evidence_chunk_id,
                item.evidence_char_start,
                item.evidence_char_end,
                item.property_value_id,
            ),
        )
    )
    if ordered != values or len({item.property_value_id for item in values}) != len(
        values
    ):
        raise ActivePublicationInventoryConflict()
    if values and revision.get("object_kind") != "entity":
        raise ActivePublicationInventoryConflict()

    tenant_id = _required_text(revision.get("tenant_id"))
    predicate = _required_text(revision.get("predicate"))
    document_id = _required_text(revision.get("document_id"))
    version_id = _required_text(revision.get("version_id"))
    chunk_id = _required_text(revision.get("chunk_id"))
    schema_version = _required_text(revision.get("ontology_version_id"))
    extractor = revision.get("extractor_version")
    if extractor is None:
        extractor = f"{_required_text(revision.get('origin'))}:reviewed"
    extractor = _required_text(extractor)
    access_policy_id = _required_text(revision.get("access_policy_id"))
    access_policy_version = _count(revision, "access_policy_version")
    if access_policy_version < 1:
        raise ActivePublicationInventoryConflict()
    access_groups_value = revision.get("access_groups")
    if not isinstance(access_groups_value, (list, tuple)):
        raise ActivePublicationInventoryConflict()
    access_groups = tuple(
        _required_text(value) for value in access_groups_value
    )
    if not access_groups or len(access_groups) != len(set(access_groups)):
        raise ActivePublicationInventoryConflict()
    chunk_ordinal = _count(row, "evidence_chunk_ordinal")
    parent_start = _count(revision, "evidence_char_start")
    parent_end = _count(revision, "evidence_char_end")
    if parent_end <= parent_start:
        raise ActivePublicationInventoryConflict()

    expectations: list[_PropertyExpectation] = []
    for ordinal, value in enumerate(values):
        if not (
            value.tenant_id == tenant_id
            and value.relationship_type == predicate
            and value.evidence_chunk_id == chunk_id
            and parent_start
            <= value.evidence_char_start
            < value.evidence_char_end
            <= parent_end
            and value.extractor_version == extractor
            and value.schema_version == schema_version
        ):
            raise ActivePublicationInventoryConflict()
        expectations.append(
            _PropertyExpectation(
                ordinal=ordinal,
                node_properties={
                    "property_value_id": value.property_value_id,
                    "tenant_id": value.tenant_id,
                    "relationship_type": value.relationship_type,
                    "name": value.name,
                    "evidence_chunk_id": value.evidence_chunk_id,
                    "evidence_char_start": value.evidence_char_start,
                    "evidence_char_end": value.evidence_char_end,
                    "evidence_text": value.evidence_text,
                    "extractor_version": value.extractor_version,
                    "schema_version": value.schema_version,
                    "confidence": value.confidence,
                    "document_id": document_id,
                    "version_id": version_id,
                    "access_policy_id": access_policy_id,
                    "access_policy_version": access_policy_version,
                    "access_groups": list(access_groups),
                    **value.literal_semantics.to_flat_properties(),
                },
                evidence_chunk_id=chunk_id,
                evidence_chunk_ordinal=chunk_ordinal,
                summary=InventoryRelationshipPropertySummary(
                    property_value_id=value.property_value_id,
                    name=value.name,
                    confidence=value.confidence,
                    literal=InventoryLiteralSummary(
                        value=value.literal_semantics.raw_value,
                        datatype=value.literal_semantics.datatype,
                        typed_value=value.literal_semantics.typed_value,
                        canonical_value=value.literal_semantics.canonical_value,
                        canonical_unit=value.literal_semantics.canonical_unit,
                        valid_from=value.literal_semantics.to_mapping()["valid_from"],  # type: ignore[arg-type]
                        valid_to=value.literal_semantics.to_mapping()["valid_to"],  # type: ignore[arg-type]
                        observed_at=value.literal_semantics.to_mapping()["observed_at"],  # type: ignore[arg-type]
                    ),
                    evidence_chunk_id=chunk_id,
                    evidence_chunk_ordinal=chunk_ordinal,
                    evidence_char_start=value.evidence_char_start,
                    evidence_char_end=value.evidence_char_end,
                ),
            )
        )
    return tuple(expectations)


def _validate_relationship_property_materializations(
    tx: Any,
    *,
    parameters: dict[str, Any],
    item_rows: tuple[dict[str, Any], ...],
) -> dict[str, tuple[InventoryRelationshipPropertySummary, ...]]:
    expected: dict[str, tuple[_PropertyExpectation, ...]] = {}
    for row in item_rows:
        labels = row.get("revision_labels")
        if not isinstance(labels, (list, tuple)):
            raise ActivePublicationInventoryConflict()
        if "GovernedAssertionRevision" not in labels:
            continue
        revision = dict(row.get("revision") or {})
        revision_id = _required_text(revision.get("revision_id"))
        if revision_id in expected:
            raise ActivePublicationInventoryConflict()
        expected[revision_id] = _relationship_property_expectations(row)

    if not expected:
        return {}
    result = tx.run(
        _PROPERTY_VALUES_QUERY,
        **parameters,
        assertion_revision_ids=sorted(expected),
    )
    rows = tuple(dict(row) for row in result)
    if len(rows) != len(expected):
        raise ActivePublicationInventoryConflict()
    seen: set[str] = set()
    for row in rows:
        revision_id = _required_text(row.get("revision_id"))
        if revision_id in seen or revision_id not in expected:
            raise ActivePublicationInventoryConflict()
        seen.add(revision_id)
        values = expected[revision_id]
        actual_values = row.get("property_values")
        if not isinstance(actual_values, (list, tuple)):
            raise ActivePublicationInventoryConflict()
        if not (
            _required_text(row.get("navigation_assertion_id"))
            and _count(row, "property_link_count") == len(values)
            and _count(row, "property_node_count") == len(values)
            and len(actual_values) == len(values)
        ):
            raise ActivePublicationInventoryConflict()
        for expected_value, actual_value in zip(values, actual_values, strict=True):
            actual = dict(actual_value) if actual_value is not None else {}
            node_value = actual.get("node_properties")
            node_properties = dict(node_value) if node_value is not None else {}
            chunks = actual.get("evidence_chunks")
            if not isinstance(chunks, (list, tuple)) or len(chunks) != 1:
                raise ActivePublicationInventoryConflict()
            chunk = dict(chunks[0]) if chunks[0] is not None else {}
            if not (
                _count(actual, "ordinal") == expected_value.ordinal
                and node_properties == expected_value.node_properties
                and _count(actual, "evidence_link_count") == 1
                and _count(actual, "evidence_chunk_count") == 1
                and actual.get("exact_evidence") is True
                and chunk.get("tenant_id") == parameters["tenant_id"]
                and chunk.get("chunk_id") == expected_value.evidence_chunk_id
                and _count(chunk, "ordinal")
                == expected_value.evidence_chunk_ordinal
            ):
                raise ActivePublicationInventoryConflict()
    if seen != set(expected):
        raise ActivePublicationInventoryConflict()
    return {
        revision_id: tuple(value.summary for value in values)
        for revision_id, values in expected.items()
    }


def _count(row: dict[str, Any], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ActivePublicationInventoryConflict()
    return value


def _entity_summary(
    value: object,
    *,
    expected_tenant_id: str,
) -> InventoryEntitySummary:
    if not isinstance(value, dict):
        value = dict(value) if value is not None else {}
    tenant_id = _required_text(value.get("tenant_id"))
    if tenant_id != expected_tenant_id:
        raise ActivePublicationInventoryConflict()
    # Keeping tenant out of the item summary avoids a redundant scope field
    # being mistaken for an independently trustworthy identity boundary.
    return InventoryEntitySummary(
        entity_id=_required_text(value.get("entity_id")),
        entity_type=_required_text(value.get("entity_type")),
        canonical_key=_required_text(value.get("canonical_key")),
        display_name=_required_text(value.get("canonical_name")),
    )


def _literal_summary(revision: dict[str, Any]) -> InventoryLiteralSummary:
    value = _required_text(revision.get("literal_value"))
    datatype = revision.get("literal_datatype")
    typed = revision.get("literal_typed_value")
    canonical = revision.get("literal_canonical_value")
    if datatype is None and typed is None and canonical is None:
        return InventoryLiteralSummary(value=value)
    if not isinstance(datatype, str) or not datatype.strip():
        raise ActivePublicationInventoryConflict()
    if isinstance(typed, float) and not math.isfinite(typed):
        raise ActivePublicationInventoryConflict()
    if not isinstance(typed, (str, int, float, bool)):
        raise ActivePublicationInventoryConflict()
    if not isinstance(canonical, str) or not canonical:
        raise ActivePublicationInventoryConflict()
    optionals: dict[str, str | None] = {}
    for name in (
        "literal_canonical_unit",
        "literal_valid_from",
        "literal_valid_to",
        "literal_observed_at",
    ):
        item = revision.get(name)
        if item is not None and not isinstance(item, str):
            raise ActivePublicationInventoryConflict()
        optionals[name] = item
    return InventoryLiteralSummary(
        value=value,
        datatype=datatype.strip(),
        typed_value=typed,
        canonical_value=canonical,
        canonical_unit=optionals["literal_canonical_unit"],
        valid_from=optionals["literal_valid_from"],
        valid_to=optionals["literal_valid_to"],
        observed_at=optionals["literal_observed_at"],
    )


def _decode_item(
    row_value: object,
    tenant_id: str,
    relationship_properties: tuple[InventoryRelationshipPropertySummary, ...] = (),
) -> ActivePublicationInventoryItem:
    row = dict(row_value) if row_value is not None else {}
    revision_value = row.get("revision")
    revision = dict(revision_value) if revision_value is not None else {}
    labels = row.get("revision_labels")
    label_set = set(labels) if isinstance(labels, (list, tuple)) else set()
    is_mention = "GovernedEntityMentionRevision" in label_set
    is_assertion = "GovernedAssertionRevision" in label_set
    if is_mention == is_assertion:
        raise ActivePublicationInventoryConflict()
    kind = "ENTITY_MENTION" if is_mention else "ASSERTION"

    revision_tenant = _required_text(revision.get("tenant_id"))
    if revision_tenant != tenant_id:
        raise ActivePublicationInventoryConflict()
    revision_number = _count(revision, "revision")
    if not (
        row.get("publication_record_kind") == kind
        and _count(row, "head_count") == 1
        and _count(row, "current_pointer_count") == 1
        and _count(row, "matching_current_count") == 1
        and row.get("head_tenant_id") == tenant_id
        and row.get("head_record_kind") == kind
        and row.get("head_current_revision") == revision_number
        and _count(row, "evidence_link_count") == 1
        and _count(row, "evidence_chunk_count") == 1
        and _count(row, "evidence_document_count") == 1
        and _count(row, "valid_evidence_path_count") == 1
    ):
        raise ActivePublicationInventoryConflict()

    confidence = revision.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(float(confidence))
        or not 0.0 <= float(confidence) <= 1.0
    ):
        raise ActivePublicationInventoryConflict()

    entity: InventoryEntitySummary | None = None
    assertion: InventoryAssertionSummary | None = None
    if kind == "ENTITY_MENTION":
        if not (
            _count(row, "mention_entity_link_count") == 1
            and _count(row, "subject_link_count") == 0
            and _count(row, "object_link_count") == 0
            and _count(row, "navigation_mention_count") == 1
            and _count(row, "mention_membership_count") == 1
            and _count(row, "valid_mention_projection_count") == 1
        ):
            raise ActivePublicationInventoryConflict()
        entity = _entity_summary(
            row.get("mention_entity"),
            expected_tenant_id=tenant_id,
        )
        if not (
            entity.entity_id == revision.get("entity_id")
            and entity.entity_type == revision.get("entity_type")
            and entity.canonical_key == revision.get("canonical_key")
            and entity.display_name == revision.get("canonical_name")
        ):
            raise ActivePublicationInventoryConflict()
        ontology_key = entity.entity_type
    else:
        if not (
            _count(row, "mention_entity_link_count") == 0
            and _count(row, "subject_link_count") == 1
            and _count(row, "navigation_assertion_count") == 1
            and _count(row, "assertion_membership_count") == 1
            and _count(row, "valid_assertion_projection_count") == 1
        ):
            raise ActivePublicationInventoryConflict()
        subject = _entity_summary(
            row.get("subject_entity"),
            expected_tenant_id=tenant_id,
        )
        if not (
            subject.entity_id == revision.get("subject_entity_id")
            and subject.entity_type == revision.get("subject_entity_type")
            and subject.canonical_key == revision.get("subject_canonical_key")
            and subject.display_name == revision.get("subject_canonical_name")
        ):
            raise ActivePublicationInventoryConflict()
        predicate = _required_text(revision.get("predicate"))
        object_kind = revision.get("object_kind")
        if object_kind == "entity":
            if _count(row, "object_link_count") != 1:
                raise ActivePublicationInventoryConflict()
            object_entity = _entity_summary(
                row.get("object_entity"),
                expected_tenant_id=tenant_id,
            )
            if not (
                object_entity.entity_id == revision.get("object_entity_id")
                and object_entity.entity_type == revision.get("object_entity_type")
                and object_entity.canonical_key == revision.get("object_canonical_key")
                and object_entity.display_name
                == revision.get("object_canonical_name")
            ):
                raise ActivePublicationInventoryConflict()
            assertion = InventoryAssertionSummary(
                subject=subject,
                predicate=predicate,
                object_kind="entity",
                object_entity=object_entity,
                relationship_properties=relationship_properties,
            )
        elif object_kind == "literal":
            if _count(row, "object_link_count") != 0:
                raise ActivePublicationInventoryConflict()
            assertion = InventoryAssertionSummary(
                subject=subject,
                predicate=predicate,
                object_kind="literal",
                literal=_literal_summary(revision),
                relationship_properties=relationship_properties,
            )
        else:
            raise ActivePublicationInventoryConflict()
        ontology_key = predicate

    start = _count(revision, "evidence_char_start")
    end = _count(revision, "evidence_char_end")
    if end <= start:
        raise ActivePublicationInventoryConflict()
    return ActivePublicationInventoryItem(
        record_id=_required_text(revision.get("record_id")),
        revision_id=_required_text(revision.get("revision_id")),
        record_kind=kind,
        governance_status=_required_text(revision.get("governance_status")),
        origin=_required_text(revision.get("origin")),
        authority_level=_required_text(revision.get("authority_level")),
        confidence=float(confidence),
        ontology_key=ontology_key,
        document_id=_required_text(revision.get("document_id")),
        version_id=_required_text(revision.get("version_id")),
        chunk_id=_required_text(revision.get("chunk_id")),
        evidence_chunk_ordinal=_count(row, "evidence_chunk_ordinal"),
        evidence_char_start=start,
        evidence_char_end=end,
        entity=entity,
        assertion=assertion,
    )


class Neo4jActivePublicationInventoryService:
    """List a safe projection of one complete active publication."""

    def __init__(
        self,
        driver: SessionDriver,
        database: str = "neo4j",
        *,
        quality_service: PublishedQualityService | None = None,
        transaction_timeout_seconds: float = 30.0,
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
            or not 0.0 < float(transaction_timeout_seconds) <= 300.0
        ):
            raise ValueError(
                "transaction_timeout_seconds must be finite and between 0 and 300"
            )
        self.driver = driver
        self.database = database.strip()
        self.quality_service = quality_service or Neo4jPublishedGraphQualityService(
            driver,
            self.database,
        )
        self._transaction_work = unit_of_work(
            metadata={
                "component": "graphrag-active-publication-inventory",
                "operation": "list",
            },
            timeout=float(transaction_timeout_seconds),
        )(self._list_tx)

    def list_active(
        self,
        principal: Principal,
        *,
        document_id: str | None = None,
        limit: int = 100,
    ) -> ActivePublicationInventory:
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal")
        if not (principal.capabilities & PUBLISHED_QUALITY_CAPABILITIES):
            raise ActivePublicationInventoryAuthorizationError()
        normalized_document_id = _optional_filter(document_id)
        normalized_limit = _limit(limit)
        try:
            quality = self.quality_service.audit(principal)
        except PublishedGraphQualityAuthorizationError as exc:
            raise ActivePublicationInventoryAuthorizationError() from exc
        except PublishedGraphQualityLimitExceeded as exc:
            raise ActivePublicationInventoryLimitExceeded() from exc
        except PublishedGraphQualityConflict as exc:
            raise ActivePublicationInventoryConflict() from exc
        except PublishedGraphQualityError as exc:
            raise ActivePublicationInventoryUnavailable() from exc
        if not quality.passed or quality.tenant_id != principal.tenant_id:
            raise ActivePublicationInventoryConflict()
        try:
            with self.driver.session(database=self.database) as session:
                return session.execute_read(
                    self._transaction_work,
                    principal,
                    quality,
                    normalized_document_id,
                    normalized_limit,
                )
        except ActivePublicationInventoryError:
            raise
        except Exception as exc:
            raise ActivePublicationInventoryUnavailable() from exc

    @staticmethod
    def _list_tx(
        tx: Any,
        principal: Principal,
        quality: PublishedGraphQualityReport,
        document_id: str | None,
        limit: int,
    ) -> ActivePublicationInventory:
        parameters = {
            "tenant_id": principal.tenant_id,
            "groups": sorted(principal.groups),
            "publication_id": quality.publication_id,
            "publication_generation": quality.publication_generation,
            "manifest_hash": quality.manifest_hash,
            "ontology_version_id": quality.ontology_version_id,
            "tbox_checksum": quality.tbox_checksum,
        }
        manifest_result = tx.run(_MANIFEST_QUERY, **parameters)
        manifest_rows = [dict(row) for row in manifest_result]
        if len(manifest_rows) != 1:
            raise ActivePublicationInventoryConflict()
        manifest = manifest_rows[0]
        membership_count = _count(manifest, "membership_count")
        distinct_count = _count(manifest, "distinct_revision_count")
        valid_count = _count(manifest, "valid_revision_count")
        expected_count = dict(quality.counts).get("revisions")
        manifest_ids = manifest.get("manifest_revision_ids")
        membership_ids = manifest.get("membership_revision_ids")
        if not (
            isinstance(expected_count, int)
            and expected_count >= 1
            and membership_count == expected_count
            and distinct_count == expected_count
            and valid_count == expected_count
            and isinstance(manifest_ids, list)
            and isinstance(membership_ids, list)
            and len(manifest_ids) == len(set(manifest_ids)) == expected_count
            and all(isinstance(item, str) and item for item in manifest_ids)
            and sorted(manifest_ids) == sorted(membership_ids)
        ):
            raise ActivePublicationInventoryConflict()
        if membership_count > MAX_ACTIVE_PUBLICATION_INVENTORY_ITEMS:
            raise ActivePublicationInventoryLimitExceeded()

        # Pull and revalidate the complete manifest inside this transaction.
        # This closes the quality-audit/read transaction gap: a concurrent
        # mutation cannot hide in an unreturned page or document filter.
        rows = tx.run(
            _ITEMS_QUERY,
            **parameters,
            row_limit=MAX_ACTIVE_PUBLICATION_INVENTORY_ITEMS + 1,
        )
        item_rows = tuple(dict(row) for row in rows)
        property_summaries = _validate_relationship_property_materializations(
            tx,
            parameters=parameters,
            item_rows=item_rows,
        )
        if (
            sum(len(values) for values in property_summaries.values())
            > MAX_ACTIVE_PUBLICATION_INVENTORY_ITEMS
        ):
            raise ActivePublicationInventoryLimitExceeded()
        decoded = tuple(
            _decode_item(
                row,
                principal.tenant_id,
                property_summaries.get(
                    _required_text(dict(row.get("revision") or {}).get("revision_id")),
                    (),
                ),
            )
            for row in item_rows
        )
        if len(decoded) != membership_count:
            raise ActivePublicationInventoryConflict()
        if len({item.revision_id for item in decoded}) != len(decoded):
            raise ActivePublicationInventoryConflict()
        ordered = tuple(
            sorted(
                decoded,
                key=lambda item: (
                    item.record_kind,
                    item.record_id,
                    item.revision_id,
                ),
            )
        )
        if ordered != decoded:
            raise ActivePublicationInventoryConflict()
        filtered = tuple(
            item
            for item in decoded
            if document_id is None or item.document_id == document_id
        )
        matching_count = len(filtered)
        return ActivePublicationInventory(
            tenant_id=principal.tenant_id,
            publication_id=quality.publication_id,
            publication_generation=quality.publication_generation,
            manifest_hash=quality.manifest_hash,
            ontology_version_id=quality.ontology_version_id,
            document_id=document_id,
            total_record_count=membership_count,
            matching_record_count=matching_count,
            truncated=matching_count > limit,
            items=filtered[:limit],
        )
