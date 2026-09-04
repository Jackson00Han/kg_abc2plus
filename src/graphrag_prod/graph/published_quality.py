"""Bounded, tenant-safe quality audits for the active governed graph.

The legacy :mod:`graphrag_prod.graph.quality` service audits teaching-era
materializations.  This module deliberately starts from the active
``KnowledgePublication`` and its exact immutable T-Box instead.  Source text
is compared inside Neo4j and is never projected into the report.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from neo4j import unit_of_work

from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import assertion_id, mention_id
from graphrag_prod.domain.models import (
    RelationshipPropertyValue,
    TypedLiteralValue,
    canonical_relationship_object_reference,
)
from graphrag_prod.ontology.models import (
    EntityTypeDefinition,
    PropertyDefinition,
    RelationshipTypeDefinition,
    TBoxStatus,
    TBoxVersion,
)

from .quality import IssueSeverity

PUBLISHED_QUALITY_CAPABILITIES = frozenset({"knowledge:quality", "knowledge:review"})
PUBLISHED_QUALITY_RULESET_VERSION = "published-governed-graph-quality-v1"

_MAX_REVISIONS = 50_000
_MAX_ENTITIES = 50_000
_MAX_ISSUES = 5_000
_MAX_SAMPLE_SIZE = 200
_SHA256_LENGTH = 64


class SessionDriver(Protocol):
    def session(self, **kwargs: object) -> Any: ...


class PublishedGraphQualityError(RuntimeError):
    """Base class whose public messages never contain backend details."""

    code = "PUBLISHED_GRAPH_QUALITY_ERROR"

    def __init__(self, message: str = "published graph quality audit failed") -> None:
        super().__init__(message)


class PublishedGraphQualityConflict(PublishedGraphQualityError):
    code = "ACTIVE_PUBLICATION_CONFLICT"

    def __init__(self) -> None:
        super().__init__(
            "the active governed-graph publication is unavailable or conflicted"
        )


class PublishedGraphQualityAuthorizationError(
    PublishedGraphQualityError, PermissionError
):
    code = "COMPLETE_PUBLICATION_ACCESS_REQUIRED"

    def __init__(self) -> None:
        super().__init__(
            "principal is not authorized to audit the complete active publication"
        )


class PublishedGraphQualityLimitExceeded(PublishedGraphQualityError):
    code = "PUBLISHED_GRAPH_QUALITY_LIMIT_EXCEEDED"

    def __init__(self) -> None:
        super().__init__("active publication exceeds the graph-quality audit bound")


class PublishedGraphQualityUnavailable(PublishedGraphQualityError):
    code = "PUBLISHED_GRAPH_QUALITY_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("published graph quality audit is temporarily unavailable")


@dataclass(frozen=True, slots=True)
class PublishedGraphQualityLimits:
    """Server-owned resource and output bounds for one audit."""

    max_revisions: int = 10_000
    max_entities: int = 10_000
    max_issues: int = 1_000
    sample_size: int = 20
    anomalous_hub_degree: int = 1_000
    transaction_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("max_revisions", self.max_revisions, _MAX_REVISIONS),
            ("max_entities", self.max_entities, _MAX_ENTITIES),
            ("max_issues", self.max_issues, _MAX_ISSUES),
            ("sample_size", self.sample_size, _MAX_SAMPLE_SIZE),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if not 1 <= value <= maximum:
                raise ValueError(f"{name} must be between 1 and {maximum}")
        if (
            isinstance(self.anomalous_hub_degree, bool)
            or not isinstance(self.anomalous_hub_degree, int)
            or self.anomalous_hub_degree < 2
        ):
            raise ValueError("anomalous_hub_degree must be an integer of at least 2")
        timeout = self.transaction_timeout_seconds
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or not 0.0 < float(timeout) <= 300.0
        ):
            raise ValueError(
                "transaction_timeout_seconds must be finite and between 0 and 300"
            )
        object.__setattr__(self, "transaction_timeout_seconds", float(timeout))


@dataclass(frozen=True, slots=True)
class PublishedGraphQualityIssue:
    issue_id: str
    code: str
    severity: IssueSeverity
    object_kind: str
    object_id: str
    detail: str


@dataclass(frozen=True, slots=True)
class PublishedGraphReviewSampleItem:
    object_kind: str
    object_id: str
    issue_codes: tuple[str, ...]
    evidence_chunk_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PublishedGraphQualityReport:
    run_id: str
    ruleset_version: str
    tenant_id: str
    publication_id: str
    publication_generation: int
    manifest_hash: str
    ontology_version_id: str
    tbox_checksum: str
    corpus_revision: int
    graph_digest: str
    counts: tuple[tuple[str, int], ...]
    total_issue_count: int
    total_error_count: int
    issues_truncated: bool
    issues: tuple[PublishedGraphQualityIssue, ...]
    review_sample: tuple[PublishedGraphReviewSampleItem, ...]

    @property
    def passed(self) -> bool:
        return self.total_error_count == 0

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["counts"] = dict(self.counts)
        value["passed"] = self.passed
        return value

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)


@dataclass(frozen=True, slots=True)
class _PublicationBoundary:
    tenant_id: str
    publication_id: str
    generation: int
    manifest_hash: str
    ontology_version_id: str
    corpus_revision: int
    manifest_revision_ids: tuple[str, ...]
    tbox: TBoxVersion


def _stable_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _SHA256_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _text(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


_STATE_QUERY = """
// published-quality:state
MATCH (state:KnowledgePublicationState {tenant_id: $tenant_id})
      -[active_link:ACTIVE_KNOWLEDGE_PUBLICATION]->
      (publication:KnowledgePublication {tenant_id: $tenant_id})
WHERE publication.status = 'ACTIVE'
WITH state, publication, count(active_link) AS active_link_count
ORDER BY publication.publication_id
LIMIT 2
OPTIONAL MATCH (corpus:TenantCorpusState {tenant_id: $tenant_id})
OPTIONAL MATCH (publication)-[binding:USES_TBOX_VERSION]->(tbox:TBoxVersion)
WHERE tbox.tenant_id = $tenant_id
  AND tbox.tbox_id = publication.ontology_version_id
WITH state, corpus, publication, active_link_count,
     count(binding) AS exact_tbox_link_count,
     head(collect(tbox {
         .tbox_id, .tenant_id, .key, .version, .status, .checksum,
         .definition_json
     })) AS tbox
RETURN publication {
           .publication_id, .tenant_id, .generation, .manifest_hash,
           .ontology_version_id, .status
       } AS publication,
       publication.published_revision_ids[0..$revision_limit]
           AS manifest_revision_ids,
       size(publication.published_revision_ids) AS manifest_revision_count,
       tbox,
       active_link_count,
       exact_tbox_link_count,
       COUNT {
           MATCH (publication)-[:USES_TBOX_VERSION]->(wrong:TBoxVersion)
           WHERE wrong.tenant_id <> $tenant_id
              OR wrong.tbox_id <> publication.ontology_version_id
       } AS wrong_tbox_link_count,
       coalesce(corpus.corpus_revision, 0) AS corpus_revision,
       NOT EXISTS {
           MATCH (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->(revision)
           WHERE revision.tenant_id <> $tenant_id
              OR none(group IN $groups
                      WHERE group IN coalesce(revision.access_groups, []))
       }
       AND NOT EXISTS {
           MATCH (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->(revision)
                 -[:IN_CHUNK|EVIDENCED_BY]->(chunk:Chunk)
           WHERE chunk.tenant_id <> $tenant_id
              OR none(group IN $groups
                      WHERE group IN coalesce(chunk.access_groups, []))
       }
       AND NOT EXISTS {
           MATCH (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->(revision)
           MATCH (document:Document {document_id: revision.document_id})
           WHERE document.tenant_id <> $tenant_id
              OR none(group IN $groups
                      WHERE group IN coalesce(document.access_groups, []))
       }
       AND NOT EXISTS {
           MATCH (publication)-[:USES_KNOWLEDGE_SNAPSHOT]->
                 (:KnowledgeSnapshot)-[:INCLUDES_CHUNK]->(chunk:Chunk)
           WHERE chunk.tenant_id <> $tenant_id
              OR none(group IN $groups
                      WHERE group IN coalesce(chunk.access_groups, []))
       }
       AND NOT EXISTS {
           MATCH (publication)-[:USES_KNOWLEDGE_SNAPSHOT]->
                 (snapshot:KnowledgeSnapshot)
           MATCH (document:Document)-[:ACTIVE_SNAPSHOT]->(snapshot)
           WHERE document.tenant_id <> $tenant_id
              OR snapshot.tenant_id <> $tenant_id
              OR none(group IN $groups
                      WHERE group IN coalesce(document.access_groups, []))
       } AS acl_complete
"""


_REVISIONS_QUERY = """
// published-quality:revisions
MATCH (state:KnowledgePublicationState {tenant_id: $tenant_id})
      -[:ACTIVE_KNOWLEDGE_PUBLICATION]->
      (publication:KnowledgePublication {
          tenant_id: $tenant_id,
          publication_id: $publication_id,
          generation: $publication_generation,
          manifest_hash: $manifest_hash,
          ontology_version_id: $ontology_version_id,
          status: 'ACTIVE'
      })
MATCH (publication)-[:USES_TBOX_VERSION]->(:TBoxVersion {
    tenant_id: $tenant_id,
    tbox_id: $ontology_version_id,
    checksum: $tbox_checksum
})
MATCH (publication)-[published:PUBLISHES_KNOWLEDGE_REVISION]->(revision)
WHERE (revision:GovernedEntityMentionRevision
       OR revision:GovernedAssertionRevision)
  AND revision.tenant_id = $tenant_id
  AND any(group IN $groups WHERE group IN revision.access_groups)
WITH publication, revision, labels(revision) AS labels,
     collect(DISTINCT published.record_kind) AS publication_record_kinds,
     count(published) AS publication_membership_count
ORDER BY revision.revision_id
LIMIT $revision_limit
CALL (revision) {
    OPTIONAL MATCH (head:KnowledgeRecordHead {record_id: revision.record_id})
    OPTIONAL MATCH (head)-[pointer:CURRENT_REVISION]->(current)
    RETURN count(DISTINCT head) AS head_count,
           count(pointer) AS current_pointer_count,
           count(CASE WHEN current.revision_id = revision.revision_id
                      THEN 1 END) AS matching_current_count,
           min(head.tenant_id) AS head_tenant_id,
           min(head.record_kind) AS head_record_kind,
           min(head.current_revision) AS head_current_revision
}
CALL (publication, revision) {
    OPTIONAL MATCH (revision)-[edge:IN_CHUNK|EVIDENCED_BY]->(chunk:Chunk)
    OPTIONAL MATCH (document:Document {document_id: revision.document_id})
          -[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot)
          -[:INCLUDES_CHUNK]->(chunk)
    OPTIONAL MATCH (document)-[:ACTIVE_VERSION]->(version:DocumentVersion)
    OPTIONAL MATCH (snapshot)-[:OF_VERSION]->(snapshot_version:DocumentVersion)
    WITH publication, revision, edge, chunk, document, snapshot, version,
         snapshot_version,
         CASE WHEN edge IS NOT NULL
                   AND chunk.tenant_id = $tenant_id
                   AND document.tenant_id = $tenant_id
                   AND version.tenant_id = $tenant_id
                   AND snapshot.tenant_id = $tenant_id
                   AND snapshot.build_state = 'PUBLISHED'
                   AND revision.document_id = document.document_id
                   AND revision.version_id = version.version_id
                   AND revision.version_id = snapshot_version.version_id
                   AND revision.chunk_id = chunk.chunk_id
                   AND revision.access_policy_id = chunk.access_policy_id
                   AND revision.access_policy_version = chunk.access_policy_version
                   AND revision.access_groups = chunk.access_groups
                   AND revision.evidence_char_start >= chunk.char_start
                   AND revision.evidence_char_start < revision.evidence_char_end
                   AND revision.evidence_char_end <= chunk.char_end
                   AND revision.evidence_text IS NOT NULL
                   AND substring(
                       chunk.text,
                       revision.evidence_char_start - chunk.char_start,
                       revision.evidence_char_end - revision.evidence_char_start
                   ) = revision.evidence_text
                   AND EXISTS {
                       MATCH (publication)-[:USES_KNOWLEDGE_SNAPSHOT]->(snapshot)
                   }
                   AND any(group IN $groups
                           WHERE group IN revision.access_groups)
                   AND any(group IN $groups
                           WHERE group IN chunk.access_groups)
                   AND any(group IN $groups
                           WHERE group IN document.access_groups)
              THEN 1 ELSE 0 END AS valid_path
    RETURN count(DISTINCT edge) AS evidence_link_count,
           count(DISTINCT chunk) AS evidence_chunk_count,
           min(chunk.chunk_id) AS evidence_chunk_id,
           count(DISTINCT document) AS evidence_document_count,
           count(DISTINCT snapshot) AS active_snapshot_count,
           sum(valid_path) AS valid_evidence_path_count
}
CALL (revision) {
    OPTIONAL MATCH (revision)-[:REFERS_TO]->(entity:Entity)
    RETURN count(entity) AS entity_link_count,
           min(entity.entity_id) AS linked_entity_id,
           min(entity.entity_type) AS linked_entity_type,
           min(entity.tenant_id) AS linked_entity_tenant_id
}
CALL (revision) {
    OPTIONAL MATCH (revision)-[:SUBJECT]->(subject:Entity)
    RETURN count(subject) AS subject_link_count,
           min(subject.entity_id) AS linked_subject_id,
           min(subject.entity_type) AS linked_subject_type,
           min(subject.tenant_id) AS linked_subject_tenant_id
}
CALL (revision) {
    OPTIONAL MATCH (revision)-[:OBJECT]->(object:Entity)
    RETURN count(object) AS object_link_count,
           min(object.entity_id) AS linked_object_id,
           min(object.entity_type) AS linked_object_type,
           min(object.tenant_id) AS linked_object_tenant_id
}
CALL (publication, revision) {
    OPTIONAL MATCH (revision)-[support:SUPPORTED_BY_MENTION]->
                   (mention:GovernedEntityMentionRevision)
    RETURN count(support) AS support_link_count,
           count(CASE
               WHEN mention.revision_id = revision.subject_mention_revision_id
                AND mention.entity_id = revision.subject_entity_id
                AND mention.chunk_id = revision.chunk_id
                AND mention.tenant_id = $tenant_id
                AND mention.governance_status = 'PUBLISHED'
                AND EXISTS {
                    MATCH (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->(mention)
                }
               THEN 1 END) AS matching_subject_mention_count,
           count(CASE
               WHEN mention.revision_id = revision.object_mention_revision_id
                AND mention.entity_id = revision.object_entity_id
                AND mention.chunk_id = revision.chunk_id
                AND mention.tenant_id = $tenant_id
                AND mention.governance_status = 'PUBLISHED'
                AND EXISTS {
                    MATCH (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->(mention)
                }
               THEN 1 END) AS matching_object_mention_count
}
CALL (publication, revision) {
    OPTIONAL MATCH (navigation:EntityMention {
        tenant_id: $tenant_id,
        governed_publication_id: publication.publication_id,
        governed_revision_id: revision.revision_id
    })
    OPTIONAL MATCH (publication)-[:USES_KNOWLEDGE_SNAPSHOT]->
          (membership_snapshot:KnowledgeSnapshot)
          -[mention_membership:INCLUDES_MENTION]->(navigation)
    WHERE mention_membership.governed_publication_id = publication.publication_id
    WITH publication, revision, navigation,
         collect(DISTINCT mention_membership) AS memberships,
         collect(DISTINCT membership_snapshot) AS membership_snapshots
    WITH publication, revision, navigation, memberships, membership_snapshots,
         CASE
               WHEN navigation IS NOT NULL
                AND size(memberships) = 1
                AND size(membership_snapshots) = 1
                AND memberships[0].governed_publication_id =
                    publication.publication_id
                AND memberships[0].confidence = revision.confidence
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
                AND navigation.governance_status = 'ACCEPTED_BY_REVIEW'
                AND navigation.authority_level = revision.authority_level
                AND COUNT {
                    MATCH (navigation)-[:IN_CHUNK]->(target:Chunk)
                    WHERE target.tenant_id = $tenant_id
                      AND target.chunk_id = revision.chunk_id
                } = 1
                AND COUNT { MATCH (navigation)-[:IN_CHUNK]->(:Chunk) } = 1
                AND COUNT {
                    MATCH (navigation)-[:REFERS_TO]->(target:Entity)
                    WHERE target.tenant_id = $tenant_id
                      AND target.entity_id = revision.entity_id
                      AND target.entity_type = revision.entity_type
                } = 1
                AND COUNT { MATCH (navigation)-[:REFERS_TO]->(:Entity) } = 1
               THEN 1 ELSE 0 END AS projection_valid
    RETURN count(DISTINCT navigation) AS navigation_mention_count,
           max(CASE WHEN navigation IS NULL THEN 0 ELSE size(memberships) END)
               AS active_mention_membership_count,
           min(navigation.mention_id) AS navigation_mention_id,
           sum(projection_valid) AS valid_navigation_mention_count
}
CALL (publication, revision) {
    OPTIONAL MATCH (navigation:Assertion {
        tenant_id: $tenant_id,
        governed_publication_id: publication.publication_id,
        governed_revision_id: revision.revision_id
    })
    OPTIONAL MATCH (publication)-[:USES_KNOWLEDGE_SNAPSHOT]->
          (membership_snapshot:KnowledgeSnapshot)
          -[assertion_membership:INCLUDES_ASSERTION]->(navigation)
    WHERE assertion_membership.governed_publication_id = publication.publication_id
    WITH publication, revision, navigation,
         collect(DISTINCT assertion_membership) AS memberships,
         collect(DISTINCT membership_snapshot) AS membership_snapshots
    WITH publication, revision, navigation, memberships, membership_snapshots,
         CASE
               WHEN navigation IS NOT NULL
                AND size(memberships) = 1
                AND size(membership_snapshots) = 1
                AND memberships[0].governed_publication_id =
                    publication.publication_id
                AND memberships[0].confidence = revision.confidence
                AND memberships[0].accepted = true
                AND navigation.evidence_chunk_id = revision.chunk_id
                AND navigation.evidence_char_start = revision.evidence_char_start
                AND navigation.evidence_char_end = revision.evidence_char_end
                AND navigation.evidence_text = revision.evidence_text
                AND navigation.extractor_version = coalesce(
                    revision.extractor_version,
                    revision.origin + ':reviewed'
                )
                AND navigation.schema_version = revision.ontology_version_id
                AND navigation.literal_value = coalesce(
                    revision.literal_value,
                    ''
                )
                AND navigation.accepted = true
                AND navigation.governance_status = 'ACCEPTED_BY_REVIEW'
                AND navigation.publication_state = 'GOVERNED_PUBLISHED'
                AND all(key IN [
                    'tenant_id',
                    'subject_entity_id',
                    'object_entity_id',
                    'predicate',
                    'object_kind',
                    'document_id',
                    'version_id',
                    'access_policy_id',
                    'access_policy_version',
                    'access_groups',
                    'confidence',
                    'authority_level',
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
                AND COUNT { MATCH (navigation)-[:SUBJECT]->(:Entity) } = 1
                AND COUNT {
                    MATCH (navigation)-[:EVIDENCED_BY]->(target:Chunk)
                    WHERE target.tenant_id = $tenant_id
                      AND target.chunk_id = revision.chunk_id
                } = 1
                AND COUNT { MATCH (navigation)-[:EVIDENCED_BY]->(:Chunk) } = 1
                AND (
                    (revision.object_kind = 'entity'
                     AND COUNT {
                         MATCH (navigation)-[:OBJECT]->(target:Entity)
                         WHERE target.tenant_id = $tenant_id
                           AND target.entity_id = revision.object_entity_id
                           AND target.entity_type = revision.object_entity_type
                     } = 1
                     AND COUNT { MATCH (navigation)-[:OBJECT]->(:Entity) } = 1)
                    OR
                    (revision.object_kind = 'literal'
                     AND COUNT { MATCH (navigation)-[:OBJECT]->(:Entity) } = 0)
                )
               THEN 1 ELSE 0 END AS projection_valid
    RETURN count(DISTINCT navigation) AS navigation_assertion_count,
           max(CASE WHEN navigation IS NULL THEN 0 ELSE size(memberships) END)
               AS active_assertion_membership_count,
           min(navigation.assertion_id) AS navigation_assertion_id,
           sum(projection_valid) AS valid_navigation_assertion_count
}
CALL (publication, revision) {
    OPTIONAL MATCH (navigation:Assertion {
        tenant_id: $tenant_id,
        governed_publication_id: publication.publication_id,
        governed_revision_id: revision.revision_id
    })-[property_link:HAS_RELATIONSHIP_PROPERTY]->
          (value:RelationshipPropertyValue)
    OPTIONAL MATCH (value)-[:EVIDENCED_BY]->(property_chunk:Chunk)
    WITH publication, revision, navigation, property_link, value, property_chunk,
         CASE WHEN value IS NOT NULL
                   AND value.tenant_id = $tenant_id
                   AND value.relationship_type = revision.predicate
                   AND value.property_value_id IS NOT NULL
                   AND value.name IS NOT NULL
                   AND value.schema_version = revision.ontology_version_id
                   AND value.extractor_version IS NOT NULL
                   AND value.evidence_chunk_id = revision.chunk_id
                   AND value.document_id = revision.document_id
                   AND value.version_id = revision.version_id
                   AND COUNT {
                       MATCH (value)-[:EVIDENCED_BY]->(:Chunk)
                   } = 1
                   AND property_chunk.chunk_id = revision.chunk_id
                   AND property_chunk.tenant_id = $tenant_id
                   AND revision.evidence_char_start <= value.evidence_char_start
                   AND value.evidence_char_start < value.evidence_char_end
                   AND value.evidence_char_end <= revision.evidence_char_end
                   AND substring(
                       property_chunk.text,
                       value.evidence_char_start - property_chunk.char_start,
                       value.evidence_char_end - value.evidence_char_start
                   ) = value.evidence_text
                   AND value.literal_datatype IS NOT NULL
                   AND value.literal_typed_value IS NOT NULL
                   AND value.literal_raw_value IS NOT NULL
                   AND value.literal_canonical_value IS NOT NULL
                   AND value.evidence_text CONTAINS value.literal_raw_value
                   AND (value.literal_raw_unit IS NULL
                        OR value.evidence_text CONTAINS value.literal_raw_unit)
                   AND (value.literal_raw_valid_from IS NULL
                        OR value.evidence_text CONTAINS value.literal_raw_valid_from)
                   AND (value.literal_raw_valid_to IS NULL
                        OR value.evidence_text CONTAINS value.literal_raw_valid_to)
                   AND (value.literal_raw_observed_at IS NULL
                        OR value.evidence_text CONTAINS value.literal_raw_observed_at)
                   AND value.access_policy_id = revision.access_policy_id
                   AND value.access_policy_version = revision.access_policy_version
                   AND value.access_groups = revision.access_groups
                   AND EXISTS {
                       MATCH (publication)-[:USES_TBOX_VERSION]->
                             (:TBoxVersion)-[:DECLARES_RELATIONSHIP_TYPE]->
                             (relationship_type:TBoxRelationshipType)
                             -[:DECLARES_PROPERTY]->
                             (property:TBoxPropertyDefinition)
                       WHERE relationship_type.name = revision.predicate
                         AND property.name = value.name
                         AND property.datatype = value.literal_datatype
                         AND ((property.unit IS NULL
                               AND value.literal_raw_unit IS NULL
                               AND value.literal_canonical_unit IS NULL)
                              OR (property.unit IS NOT NULL
                                  AND value.literal_raw_unit IS NOT NULL
                                  AND value.literal_canonical_unit IS NOT NULL))
                   }
              THEN 1 ELSE 0 END AS valid_property
    WITH revision, property_link, value, valid_property
    ORDER BY value.property_value_id
    RETURN count(DISTINCT property_link) AS materialized_property_link_count,
           count(DISTINCT value) AS materialized_property_count,
           count(DISTINCT CASE WHEN valid_property = 1 THEN value END)
               AS valid_materialized_property_count,
           collect(DISTINCT CASE WHEN value IS NULL THEN NULL ELSE value {
               .property_value_id,
               .tenant_id,
               .relationship_type,
               .name,
               .literal_datatype,
               .literal_typed_value,
               .literal_raw_value,
               .literal_raw_unit,
               .literal_canonical_value,
               .literal_canonical_unit,
               .literal_valid_from,
               .literal_valid_to,
               .literal_observed_at,
               .literal_raw_valid_from,
               .literal_raw_valid_to,
               .literal_raw_observed_at,
               .evidence_chunk_id,
               .evidence_char_start,
               .evidence_char_end,
               .extractor_version,
               .schema_version,
               .confidence,
               .document_id,
               .version_id,
               .access_policy_id,
               .access_policy_version,
               .access_groups
           } END) AS materialized_property_values
}
RETURN revision {
           .revision_id, .record_id, .revision, .tenant_id,
           .ontology_version_id, .governance_status, .origin,
           .authority_level, .document_id, .version_id, .chunk_id,
           .access_policy_id, .access_policy_version, .access_groups,
           .evidence_char_start, .evidence_char_end, .confidence,
           .extractor_version, .surface,
           .entity_id, .entity_type, .canonical_key,
           .subject_entity_id, .subject_entity_type, .subject_canonical_key,
           .predicate, .object_kind, .object_entity_id,
           .object_entity_type, .object_canonical_key,
           .subject_mention_revision_id, .object_mention_revision_id,
           .literal_value, .literal_datatype, .literal_typed_value,
           .literal_raw_value, .literal_raw_unit, .literal_canonical_value,
           .literal_canonical_unit, .literal_valid_from, .literal_valid_to,
           .literal_observed_at, .literal_raw_valid_from,
           .literal_raw_valid_to, .literal_raw_observed_at,
           .relationship_properties_format_version,
           .relationship_properties_json
       } AS revision,
       labels, publication_record_kinds, publication_membership_count,
       CASE WHEN revision.object_kind <> 'literal' THEN true
            WHEN revision.literal_value IS NOT NULL
             AND revision.literal_value <> ''
             AND revision.literal_raw_value IS NOT NULL
             AND revision.evidence_text CONTAINS revision.literal_value
             AND revision.evidence_text CONTAINS revision.literal_raw_value
             AND (revision.literal_raw_unit IS NULL
                  OR revision.evidence_text CONTAINS revision.literal_raw_unit)
             AND (revision.literal_raw_valid_from IS NULL
                  OR revision.evidence_text CONTAINS revision.literal_raw_valid_from)
             AND (revision.literal_raw_valid_to IS NULL
                  OR revision.evidence_text CONTAINS revision.literal_raw_valid_to)
             AND (revision.literal_raw_observed_at IS NULL
                  OR revision.evidence_text CONTAINS revision.literal_raw_observed_at)
            THEN true ELSE false END AS literal_source_tokens_valid,
       head_count, current_pointer_count, matching_current_count,
       head_tenant_id, head_record_kind, head_current_revision,
       evidence_link_count, evidence_chunk_count, evidence_chunk_id,
       evidence_document_count, active_snapshot_count,
       valid_evidence_path_count,
       entity_link_count, linked_entity_id, linked_entity_type,
       linked_entity_tenant_id,
       subject_link_count, linked_subject_id, linked_subject_type,
       linked_subject_tenant_id,
       object_link_count, linked_object_id, linked_object_type,
       linked_object_tenant_id,
       support_link_count, matching_subject_mention_count,
       matching_object_mention_count,
       navigation_mention_count, active_mention_membership_count,
       navigation_mention_id, valid_navigation_mention_count,
       navigation_assertion_count, active_assertion_membership_count,
       navigation_assertion_id, valid_navigation_assertion_count,
       materialized_property_link_count, materialized_property_count,
       valid_materialized_property_count, materialized_property_values
ORDER BY revision.revision_id
"""


_ENTITIES_QUERY = """
// published-quality:entities
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
MATCH (publication)-[:USES_TBOX_VERSION]->(:TBoxVersion {
    tenant_id: $tenant_id,
    tbox_id: $ontology_version_id,
    checksum: $tbox_checksum
})
WHERE NOT EXISTS {
    MATCH (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->(hidden_revision)
    WHERE hidden_revision.tenant_id <> $tenant_id
       OR none(group IN $groups
               WHERE group IN coalesce(hidden_revision.access_groups, []))
}
  AND NOT EXISTS {
    MATCH (publication)-[:USES_KNOWLEDGE_SNAPSHOT]->
          (:KnowledgeSnapshot)-[:INCLUDES_CHUNK]->(hidden_chunk:Chunk)
    WHERE hidden_chunk.tenant_id <> $tenant_id
       OR none(group IN $groups
               WHERE group IN coalesce(hidden_chunk.access_groups, []))
}
CALL (publication) {
    MATCH (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->(revision)
          -[:REFERS_TO|SUBJECT|OBJECT]->(entity:Entity)
    WHERE revision.tenant_id = $tenant_id
      AND any(group IN $groups WHERE group IN revision.access_groups)
    RETURN entity
    UNION
    MATCH (publication)-[:USES_KNOWLEDGE_SNAPSHOT]->
          (snapshot:KnowledgeSnapshot)-[membership:INCLUDES_ENTITY]->
          (entity:Entity)
    WHERE snapshot.tenant_id = $tenant_id
      AND membership.governed_publication_id = publication.publication_id
    RETURN entity
}
WITH DISTINCT publication, entity
ORDER BY entity.entity_id
LIMIT $entity_limit
CALL (publication, entity) {
    OPTIONAL MATCH (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->
          (mention:GovernedEntityMentionRevision)-[:REFERS_TO]->(entity)
    RETURN count(DISTINCT mention) AS published_mention_count,
           min(mention.chunk_id) AS sample_chunk_id
}
CALL (publication, entity) {
    OPTIONAL MATCH (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->
          (:GovernedAssertionRevision)-[endpoint:SUBJECT|OBJECT]->(entity)
    RETURN count(endpoint) AS published_degree
}
CALL (publication, entity) {
    OPTIONAL MATCH (publication)-[:USES_KNOWLEDGE_SNAPSHOT]->
          (snapshot:KnowledgeSnapshot)-[membership:INCLUDES_ENTITY]->(entity)
    WHERE membership.governed_publication_id = publication.publication_id
    RETURN count(DISTINCT membership) AS active_membership_count
}
RETURN entity {
           .entity_id, .tenant_id, .entity_type, .canonical_key
       } AS entity,
       published_mention_count, published_degree,
       active_membership_count, sample_chunk_id
ORDER BY entity.entity_id
"""


_AUTHORITY_BY_ORIGIN = {
    "EXPERT_IMPORT": "AUTHORITATIVE",
    "EXPERT_CREATED": "AUTHORITATIVE",
    "LLM_EXTRACTED": "SECONDARY",
    "RULE_DERIVED": "SECONDARY",
    "FIXTURE": "SECONDARY",
}


class _IssueCollector:
    def __init__(self, run_seed: str, limit: int) -> None:
        self.run_seed = run_seed
        self.limit = limit
        self.total = 0
        self.errors = 0
        self.values: list[PublishedGraphQualityIssue] = []

    def add(
        self,
        code: str,
        severity: IssueSeverity,
        object_kind: str,
        object_id: object,
        detail: str,
    ) -> None:
        self.total += 1
        if severity is IssueSeverity.ERROR:
            self.errors += 1
        if len(self.values) >= self.limit:
            return
        normalized_id = _text(object_id) or "unknown"
        issue_id = "published-quality-issue:" + _stable_hash(
            [
                self.run_seed,
                code,
                severity.value,
                object_kind,
                normalized_id,
                detail,
            ]
        )
        self.values.append(
            PublishedGraphQualityIssue(
                issue_id=issue_id,
                code=code,
                severity=severity,
                object_kind=object_kind,
                object_id=normalized_id,
                detail=detail,
            )
        )


def _rows(result: object) -> list[dict[str, Any]]:
    try:
        values = list(result)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise PublishedGraphQualityConflict() from exc
    rows: list[dict[str, Any]] = []
    for value in values:
        try:
            rows.append(dict(value))  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise PublishedGraphQualityConflict() from exc
    return rows


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PublishedGraphQualityConflict()
    if any(not isinstance(key, str) for key in value):
        raise PublishedGraphQualityConflict()
    return dict(value)


def _decode_tbox(value: object) -> TBoxVersion:
    stored = _mapping(value)
    try:
        payload = json.loads(stored["definition_json"])
        if not isinstance(payload, dict):
            raise TypeError("definition is not an object")
        payload["status"] = stored["status"]
        result = TBoxVersion.from_mapping(payload)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PublishedGraphQualityConflict() from exc
    if (
        result.tbox_id != stored.get("tbox_id")
        or result.tenant_id != stored.get("tenant_id")
        or result.key != stored.get("key")
        or result.version != stored.get("version")
        or result.checksum != stored.get("checksum")
        or result.status not in {TBoxStatus.PUBLISHED, TBoxStatus.RETIRED}
    ):
        raise PublishedGraphQualityConflict()
    return result


def _publication_boundary(
    rows: Sequence[Mapping[str, Any]],
    principal: Principal,
    limits: PublishedGraphQualityLimits,
) -> _PublicationBoundary:
    # More than one active publication is corruption.  Do not pick one.
    if len(rows) != 1:
        raise PublishedGraphQualityConflict()
    row = dict(rows[0])
    # ACL is deliberately checked before manifest counts or graph content are
    # interpreted, so a partially-visible publication returns no statistics.
    if row.get("acl_complete") is not True:
        raise PublishedGraphQualityAuthorizationError()
    publication = _mapping(row.get("publication"))
    if (
        publication.get("tenant_id") != principal.tenant_id
        or publication.get("status") != "ACTIVE"
        or _text(publication.get("publication_id")) is None
        or _text(publication.get("ontology_version_id")) is None
    ):
        raise PublishedGraphQualityConflict()
    generation = _integer(publication.get("generation"))
    corpus_revision = _integer(row.get("corpus_revision"))
    if (
        generation is None
        or generation <= 0
        or corpus_revision is None
        or corpus_revision < 0
    ):
        raise PublishedGraphQualityConflict()
    manifest_hash = publication.get("manifest_hash")
    if not _is_digest(manifest_hash):
        raise PublishedGraphQualityConflict()
    active_links = _integer(row.get("active_link_count"))
    exact_links = _integer(row.get("exact_tbox_link_count"))
    wrong_links = _integer(row.get("wrong_tbox_link_count"))
    if active_links != 1 or exact_links != 1 or wrong_links != 0:
        raise PublishedGraphQualityConflict()
    manifest_count = _integer(row.get("manifest_revision_count"))
    manifest_values = row.get("manifest_revision_ids")
    if (
        manifest_count is None
        or manifest_count <= 0
        or isinstance(manifest_values, (str, bytes))
        or not isinstance(manifest_values, Sequence)
    ):
        raise PublishedGraphQualityConflict()
    if manifest_count > limits.max_revisions:
        raise PublishedGraphQualityLimitExceeded()
    manifest_ids = tuple(_text(item) or "" for item in manifest_values)
    if (
        len(manifest_ids) != manifest_count
        or any(not item for item in manifest_ids)
        or len(set(manifest_ids)) != len(manifest_ids)
    ):
        raise PublishedGraphQualityConflict()
    tbox = _decode_tbox(row.get("tbox"))
    if (
        tbox.tenant_id != principal.tenant_id
        or tbox.tbox_id != publication["ontology_version_id"]
    ):
        raise PublishedGraphQualityConflict()
    return _PublicationBoundary(
        tenant_id=principal.tenant_id,
        publication_id=str(publication["publication_id"]),
        generation=generation,
        manifest_hash=str(manifest_hash),
        ontology_version_id=tbox.tbox_id,
        corpus_revision=corpus_revision,
        manifest_revision_ids=manifest_ids,
        tbox=tbox,
    )


def _namespace_allowed(
    canonical_key: object,
    definition: EntityTypeDefinition | None,
) -> bool:
    if definition is None or not isinstance(canonical_key, str):
        return False
    namespace, separator, suffix = canonical_key.partition(":")
    return bool(
        separator
        and suffix
        and namespace.casefold() in definition.canonical_key_namespaces
    )


def _count(row: Mapping[str, Any], name: str) -> int:
    value = _integer(row.get(name))
    return -1 if value is None or value < 0 else value


def _literal_semantics(revision: dict[str, Any]) -> TypedLiteralValue | None:
    try:
        return TypedLiteralValue.from_flat_properties(revision)
    except (TypeError, ValueError):
        return None


def _relationship_properties(
    revision: Mapping[str, Any],
) -> tuple[RelationshipPropertyValue, ...] | None:
    version = revision.get("relationship_properties_format_version")
    payload = revision.get("relationship_properties_json")
    # Legacy published assertions with no relationship properties remain
    # valid.  A half-present codec is corruption.
    if version is None and payload is None:
        return ()
    if version != 1 or not isinstance(payload, str):
        return None
    try:
        decoded = json.loads(payload)
        if not isinstance(decoded, list):
            return None
        values = tuple(RelationshipPropertyValue.from_mapping(item) for item in decoded)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    identifiers = tuple(item.property_value_id for item in values)
    if len(identifiers) != len(set(identifiers)):
        return None
    return values


def _materialization_extractor(revision: Mapping[str, Any]) -> str | None:
    extractor = _text(revision.get("extractor_version"))
    if extractor is not None:
        return extractor
    origin = _text(revision.get("origin"))
    return None if origin is None else f"{origin}:reviewed"


def _expected_navigation_mention_id(
    revision: Mapping[str, Any],
) -> str | None:
    try:
        chunk_id = _text(revision.get("chunk_id"))
        entity_type = _text(revision.get("entity_type"))
        surface = _text(revision.get("surface"))
        extractor = _materialization_extractor(revision)
        char_start = _integer(revision.get("evidence_char_start"))
        char_end = _integer(revision.get("evidence_char_end"))
        if None in (
            chunk_id,
            entity_type,
            surface,
            extractor,
            char_start,
            char_end,
        ):
            return None
        return mention_id(
            str(chunk_id),
            str(entity_type),
            int(char_start),
            int(char_end),
            str(surface),
            str(extractor),
        )
    except (TypeError, ValueError):
        return None


def _expected_navigation_assertion_id(
    revision: Mapping[str, Any],
    properties: tuple[RelationshipPropertyValue, ...] | None,
) -> str | None:
    try:
        tenant_id = _text(revision.get("tenant_id"))
        subject_id = _text(revision.get("subject_entity_id"))
        predicate = _text(revision.get("predicate"))
        object_kind = _text(revision.get("object_kind"))
        chunk_id = _text(revision.get("chunk_id"))
        extractor = _materialization_extractor(revision)
        schema_version = _text(revision.get("ontology_version_id"))
        char_start = _integer(revision.get("evidence_char_start"))
        char_end = _integer(revision.get("evidence_char_end"))
        if None in (
            tenant_id,
            subject_id,
            predicate,
            object_kind,
            chunk_id,
            extractor,
            schema_version,
            char_start,
            char_end,
        ):
            return None
        if object_kind == "entity":
            object_id = _text(revision.get("object_entity_id"))
            if object_id is None or properties is None:
                return None
            object_reference = canonical_relationship_object_reference(
                object_id,
                properties,
            )
        elif object_kind == "literal":
            literal = _literal_semantics(dict(revision))
            if literal is not None:
                object_reference = literal.identity_reference
            else:
                object_reference = _text(revision.get("literal_value"))
                if object_reference is None:
                    return None
        else:
            return None
        return assertion_id(
            str(tenant_id),
            str(subject_id),
            str(predicate),
            str(object_kind),
            object_reference,
            str(chunk_id),
            int(char_start),
            int(char_end),
            str(extractor),
            str(schema_version),
        )
    except (TypeError, ValueError):
        return None


def _materialized_relationship_property_signatures(
    row: Mapping[str, Any],
) -> tuple[dict[str, Any], ...] | None:
    raw_values = row.get("materialized_property_values")
    if isinstance(raw_values, (str, bytes)) or not isinstance(raw_values, Sequence):
        return None
    values: list[dict[str, Any]] = []
    try:
        for raw_value in raw_values:
            stored = _mapping(raw_value)
            literal = TypedLiteralValue.from_flat_properties(stored)
            if literal is None:
                return None
            values.append(
                {
                    key: value
                    for key, value in stored.items()
                    if value is not None
                }
            )
    except (KeyError, TypeError, ValueError):
        return None
    identifiers = [item.get("property_value_id") for item in values]
    if len(identifiers) != len(set(identifiers)):
        return None
    return tuple(
        sorted(
            values,
            key=lambda item: str(item.get("property_value_id", "")),
        )
    )


def _expected_relationship_property_signatures(
    properties: Sequence[RelationshipPropertyValue],
    revision: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "property_value_id": item.property_value_id,
            "tenant_id": item.tenant_id,
            "relationship_type": item.relationship_type,
            "name": item.name,
            **item.literal_semantics.to_flat_properties(),
            "evidence_chunk_id": item.evidence_chunk_id,
            "evidence_char_start": item.evidence_char_start,
            "evidence_char_end": item.evidence_char_end,
            "extractor_version": item.extractor_version,
            "schema_version": item.schema_version,
            "confidence": item.confidence,
            "document_id": revision.get("document_id"),
            "version_id": revision.get("version_id"),
            "access_policy_id": revision.get("access_policy_id"),
            "access_policy_version": revision.get("access_policy_version"),
            "access_groups": revision.get("access_groups"),
        }
        for item in sorted(properties, key=lambda value: value.property_value_id)
    )


def _typed_literal_matches(
    literal: TypedLiteralValue,
    definition: PropertyDefinition,
) -> bool:
    if literal.datatype != definition.datatype.value:
        return False
    # Recompute, rather than trust, canonical units, values, and temporal
    # projections stored on either the immutable revision or navigation node.
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
        )
    except LiteralNormalizationError:
        return False
    return normalized == literal


def _property_value_matches(
    value: RelationshipPropertyValue,
    definition: PropertyDefinition,
) -> bool:
    return _typed_literal_matches(value.literal_semantics, definition)


def _audit_revision(
    row: Mapping[str, Any],
    *,
    boundary: _PublicationBoundary,
    entity_types: Mapping[str, EntityTypeDefinition],
    relationship_types: Mapping[str, RelationshipTypeDefinition],
    issues: _IssueCollector,
    literal_counts: dict[tuple[str, str], int],
) -> tuple[str, str | None]:
    revision = _mapping(row.get("revision"))
    revision_id = _text(revision.get("revision_id")) or "unknown"
    labels_value = row.get("labels")
    labels = (
        {str(item) for item in labels_value}
        if isinstance(labels_value, Sequence)
        and not isinstance(labels_value, (str, bytes))
        else set()
    )
    is_mention = "GovernedEntityMentionRevision" in labels
    is_assertion = "GovernedAssertionRevision" in labels
    if is_mention == is_assertion:
        issues.add(
            "INVALID_REVISION_LABEL",
            IssueSeverity.ERROR,
            "KnowledgeRevision",
            revision_id,
            "published revision must have exactly one governed record label",
        )
        kind = "UNKNOWN"
    else:
        kind = "ENTITY_MENTION" if is_mention else "ASSERTION"

    if revision.get("tenant_id") != boundary.tenant_id:
        issues.add(
            "REVISION_TENANT_MISMATCH",
            IssueSeverity.ERROR,
            "KnowledgeRevision",
            revision_id,
            "published revision is outside the publication tenant",
        )
    if revision.get("ontology_version_id") != boundary.ontology_version_id:
        issues.add(
            "REVISION_TBOX_MISMATCH",
            IssueSeverity.ERROR,
            "KnowledgeRevision",
            revision_id,
            "published revision is not bound to the publication T-Box",
        )
    if revision.get("governance_status") != "PUBLISHED":
        issues.add(
            "REVISION_STATUS_INVALID",
            IssueSeverity.ERROR,
            "KnowledgeRevision",
            revision_id,
            "publication contains a revision outside PUBLISHED status",
        )
    expected_authority = _AUTHORITY_BY_ORIGIN.get(str(revision.get("origin")))
    if (
        expected_authority is None
        or revision.get("authority_level") != expected_authority
    ):
        issues.add(
            "ORIGIN_AUTHORITY_INVALID",
            IssueSeverity.ERROR,
            "KnowledgeRevision",
            revision_id,
            "knowledge origin and authority level are inconsistent",
        )

    revision_number = _integer(revision.get("revision"))
    head_ok = (
        _count(row, "head_count") == 1
        and _count(row, "current_pointer_count") == 1
        and _count(row, "matching_current_count") == 1
        and row.get("head_tenant_id") == boundary.tenant_id
        and row.get("head_record_kind") == kind
        and revision_number is not None
        and row.get("head_current_revision") == revision_number
    )
    if not head_ok:
        issues.add(
            "HEAD_CURRENT_REVISION_INVALID",
            IssueSeverity.ERROR,
            "KnowledgeRevision",
            revision_id,
            "record head does not point uniquely to the published current revision",
        )
    edge_kinds = row.get("publication_record_kinds")
    if (
        _count(row, "publication_membership_count") != 1
        or not isinstance(edge_kinds, Sequence)
        or isinstance(edge_kinds, (str, bytes))
        or tuple(edge_kinds) != (kind,)
    ):
        issues.add(
            "PUBLICATION_RECORD_KIND_INVALID",
            IssueSeverity.ERROR,
            "KnowledgeRevision",
            revision_id,
            "publication membership has an invalid record-kind binding",
        )

    chunk_id = _text(revision.get("chunk_id"))
    evidence_ok = (
        _count(row, "evidence_link_count") == 1
        and _count(row, "evidence_chunk_count") == 1
        and _count(row, "evidence_document_count") == 1
        and _count(row, "active_snapshot_count") == 1
        and _count(row, "valid_evidence_path_count") == 1
        and chunk_id is not None
        and row.get("evidence_chunk_id") == chunk_id
    )
    if not evidence_ok:
        issues.add(
            "EVIDENCE_ACTIVE_SNAPSHOT_INVALID",
            IssueSeverity.ERROR,
            "KnowledgeRevision",
            revision_id,
            "source evidence is not exact, unique, ACL-consistent, and active",
        )

    if kind == "ENTITY_MENTION":
        entity_type = _text(revision.get("entity_type"))
        definition = entity_types.get(entity_type or "")
        if definition is None:
            issues.add(
                "ENTITY_TYPE_UNDECLARED",
                IssueSeverity.ERROR,
                "EntityMentionRevision",
                revision_id,
                "entity type is not declared by the bound T-Box",
            )
        if not _namespace_allowed(revision.get("canonical_key"), definition):
            issues.add(
                "ENTITY_NAMESPACE_INVALID",
                IssueSeverity.ERROR,
                "EntityMentionRevision",
                revision_id,
                "canonical entity namespace is not allowed by the bound T-Box",
            )
        if not (
            _count(row, "entity_link_count") == 1
            and row.get("linked_entity_id") == revision.get("entity_id")
            and row.get("linked_entity_type") == entity_type
            and row.get("linked_entity_tenant_id") == boundary.tenant_id
        ):
            issues.add(
                "MENTION_ENTITY_LINK_INVALID",
                IssueSeverity.ERROR,
                "EntityMentionRevision",
                revision_id,
                "mention does not uniquely link its declared canonical entity",
            )
        if not (
            _count(row, "navigation_mention_count") == 1
            and _count(row, "active_mention_membership_count") == 1
        ):
            issues.add(
                "ACTIVE_MENTION_MATERIALIZATION_INVALID",
                IssueSeverity.ERROR,
                "EntityMentionRevision",
                revision_id,
                "published mention is absent from the active knowledge snapshot",
            )
        expected_mention_id = _expected_navigation_mention_id(revision)
        if not (
            _count(row, "valid_navigation_mention_count") == 1
            and expected_mention_id is not None
            and row.get("navigation_mention_id") == expected_mention_id
        ):
            issues.add(
                "ACTIVE_MENTION_PROJECTION_INVALID",
                IssueSeverity.ERROR,
                "EntityMentionRevision",
                revision_id,
                "active mention properties or graph links differ from its "
                "immutable revision",
            )
        return kind, chunk_id

    if kind != "ASSERTION":
        return kind, chunk_id

    if not (
        _count(row, "navigation_assertion_count") == 1
        and _count(row, "active_assertion_membership_count") == 1
    ):
        issues.add(
            "ACTIVE_ASSERTION_MATERIALIZATION_INVALID",
            IssueSeverity.ERROR,
            "AssertionRevision",
            revision_id,
            "published assertion is absent or duplicated in the active snapshot",
        )

    relationship_properties = _relationship_properties(revision)
    expected_assertion_id = _expected_navigation_assertion_id(
        revision,
        relationship_properties,
    )
    if not (
        _count(row, "valid_navigation_assertion_count") == 1
        and expected_assertion_id is not None
        and row.get("navigation_assertion_id") == expected_assertion_id
    ):
        issues.add(
            "ACTIVE_ASSERTION_PROJECTION_INVALID",
            IssueSeverity.ERROR,
            "AssertionRevision",
            revision_id,
            "active assertion properties, typed fields, or graph links differ "
            "from its immutable revision",
        )

    subject_type = _text(revision.get("subject_entity_type"))
    subject_definition = entity_types.get(subject_type or "")
    if subject_definition is None or not _namespace_allowed(
        revision.get("subject_canonical_key"), subject_definition
    ):
        issues.add(
            "ASSERTION_SUBJECT_SCHEMA_INVALID",
            IssueSeverity.ERROR,
            "AssertionRevision",
            revision_id,
            "assertion subject violates the bound entity-type contract",
        )
    if not (
        _count(row, "subject_link_count") == 1
        and row.get("linked_subject_id") == revision.get("subject_entity_id")
        and row.get("linked_subject_type") == subject_type
        and row.get("linked_subject_tenant_id") == boundary.tenant_id
        and _count(row, "matching_subject_mention_count") == 1
    ):
        issues.add(
            "SUBJECT_MENTION_LINK_INVALID",
            IssueSeverity.ERROR,
            "AssertionRevision",
            revision_id,
            "assertion subject and supporting mention linkage is invalid",
        )

    predicate = _text(revision.get("predicate"))
    object_kind = revision.get("object_kind")
    if object_kind == "entity":
        relationship = relationship_types.get(predicate or "")
        object_type = _text(revision.get("object_entity_type"))
        object_definition = entity_types.get(object_type or "")
        if (
            relationship is None
            or subject_type not in relationship.source_types
            or object_type not in relationship.target_types
            or not _namespace_allowed(
                revision.get("object_canonical_key"), object_definition
            )
        ):
            issues.add(
                "RELATIONSHIP_PATTERN_INVALID",
                IssueSeverity.ERROR,
                "AssertionRevision",
                revision_id,
                "relationship type or its directed endpoint pattern is invalid",
            )
        if not (
            _count(row, "object_link_count") == 1
            and row.get("linked_object_id") == revision.get("object_entity_id")
            and row.get("linked_object_type") == object_type
            and row.get("linked_object_tenant_id") == boundary.tenant_id
            and _count(row, "matching_object_mention_count") == 1
            and _count(row, "support_link_count") >= 2
        ):
            issues.add(
                "OBJECT_MENTION_LINK_INVALID",
                IssueSeverity.ERROR,
                "AssertionRevision",
                revision_id,
                "assertion object and supporting mention linkage is invalid",
            )
        properties = relationship_properties
        if properties is None:
            issues.add(
                "RELATIONSHIP_PROPERTIES_INVALID",
                IssueSeverity.ERROR,
                "AssertionRevision",
                revision_id,
                "relationship-property payload is incomplete or invalid",
            )
        else:
            property_definitions = {
                item.name: item
                for item in (() if relationship is None else relationship.properties)
            }
            property_counts: dict[str, int] = {}
            properties_valid = True
            for value in properties:
                definition = property_definitions.get(value.name)
                property_counts[value.name] = property_counts.get(value.name, 0) + 1
                if (
                    definition is None
                    or value.tenant_id != boundary.tenant_id
                    or value.relationship_type != predicate
                    or value.evidence_chunk_id != chunk_id
                    or not _property_value_matches(value, definition)
                ):
                    properties_valid = False
            for name, definition in property_definitions.items():
                count = property_counts.get(name, 0)
                if definition.cardinality.required and count == 0:
                    properties_valid = False
                if definition.cardinality.single_valued and count > 1:
                    properties_valid = False
            if not properties_valid:
                issues.add(
                    "RELATIONSHIP_PROPERTY_SCHEMA_INVALID",
                    IssueSeverity.ERROR,
                    "AssertionRevision",
                    revision_id,
                    "relationship properties violate T-Box type or cardinality rules",
                )
            materialized_count = _count(row, "materialized_property_count")
            materialized_link_count = _count(row, "materialized_property_link_count")
            valid_materialized_count = _count(row, "valid_materialized_property_count")
            materialized = _materialized_relationship_property_signatures(row)
            if (
                materialized_count != len(properties)
                or valid_materialized_count != len(properties)
                or materialized_link_count != len(properties)
                or materialized is None
                or materialized
                != _expected_relationship_property_signatures(
                    properties,
                    revision,
                )
            ):
                issues.add(
                    "RELATIONSHIP_PROPERTY_MATERIALIZATION_INVALID",
                    IssueSeverity.ERROR,
                    "AssertionRevision",
                    revision_id,
                    "materialized relationship properties differ from their exact "
                    "reviewed payload or evidence",
                )
        return kind, chunk_id

    if object_kind == "literal":
        definition = None
        if subject_definition is not None and predicate is not None:
            definition = next(
                (
                    item
                    for item in subject_definition.properties
                    if item.name == predicate
                ),
                None,
            )
        literal = _literal_semantics(revision)
        literal_ok = (
            definition is not None
            and literal is not None
            and _typed_literal_matches(literal, definition)
            and row.get("literal_source_tokens_valid") is True
            and relationship_properties == ()
            and _count(row, "object_link_count") == 0
            and revision.get("object_entity_id") is None
            and revision.get("object_mention_revision_id") is None
            and _count(row, "support_link_count") == 1
            and _count(row, "materialized_property_link_count") == 0
            and _count(row, "materialized_property_count") == 0
            and _count(row, "valid_materialized_property_count") == 0
            and _materialized_relationship_property_signatures(row) == ()
        )
        if not literal_ok:
            issues.add(
                "TYPED_LITERAL_INVALID",
                IssueSeverity.ERROR,
                "AssertionRevision",
                revision_id,
                "literal assertion is incomplete or violates its T-Box property",
            )
        if predicate is not None and revision.get("subject_entity_id") is not None:
            key = (str(revision["subject_entity_id"]), predicate)
            literal_counts[key] = literal_counts.get(key, 0) + 1
        return kind, chunk_id

    issues.add(
        "ASSERTION_OBJECT_KIND_INVALID",
        IssueSeverity.ERROR,
        "AssertionRevision",
        revision_id,
        "assertion object kind must be entity or literal",
    )
    return kind, chunk_id


def _audit_entities(
    rows: Sequence[Mapping[str, Any]],
    *,
    boundary: _PublicationBoundary,
    entity_types: Mapping[str, EntityTypeDefinition],
    issues: _IssueCollector,
    hub_degree: int,
) -> dict[tuple[str, str], int]:
    canonical_groups: dict[tuple[str, str], list[str]] = {}
    entity_type_counts: dict[tuple[str, str], int] = {}
    for row in rows:
        entity = _mapping(row.get("entity"))
        entity_id = _text(entity.get("entity_id")) or "unknown"
        entity_type = _text(entity.get("entity_type"))
        definition = entity_types.get(entity_type or "")
        if (
            entity.get("tenant_id") != boundary.tenant_id
            or definition is None
            or not _namespace_allowed(entity.get("canonical_key"), definition)
        ):
            issues.add(
                "CANONICAL_ENTITY_SCHEMA_INVALID",
                IssueSeverity.ERROR,
                "Entity",
                entity_id,
                "canonical entity violates tenant, type, or namespace constraints",
            )
        canonical_key = _text(entity.get("canonical_key"))
        if entity_type and canonical_key:
            canonical_groups.setdefault(
                (entity_type.casefold(), canonical_key.casefold()), []
            ).append(entity_id)
            entity_type_counts[(entity_id, entity_type)] = 1
        mention_count = _count(row, "published_mention_count")
        degree = _count(row, "published_degree")
        if mention_count <= 0:
            issues.add(
                "ORPHAN_ENTITY",
                IssueSeverity.ERROR,
                "Entity",
                entity_id,
                "published canonical entity has no published evidence mention",
            )
        if _count(row, "active_membership_count") <= 0:
            issues.add(
                "ACTIVE_ENTITY_MEMBERSHIP_MISSING",
                IssueSeverity.ERROR,
                "Entity",
                entity_id,
                "published entity is absent from the active knowledge snapshot",
            )
        if degree == 0:
            issues.add(
                "ISOLATED_ENTITY",
                IssueSeverity.WARNING,
                "Entity",
                entity_id,
                "published entity has no relationship assertion",
            )
        elif degree >= hub_degree:
            issues.add(
                "ANOMALOUS_HUB",
                IssueSeverity.REVIEW,
                "Entity",
                entity_id,
                "published entity exceeds the configured relationship-degree threshold",
            )
    for entity_ids in canonical_groups.values():
        unique_ids = sorted(set(entity_ids))
        if len(unique_ids) > 1:
            for entity_id in unique_ids:
                issues.add(
                    "DUPLICATE_ENTITY",
                    IssueSeverity.ERROR,
                    "Entity",
                    entity_id,
                    "multiple entity IDs share one type and canonical key",
                )
    return entity_type_counts


def _audit_relationship_endpoint_cardinality(
    revision_rows: Sequence[Mapping[str, Any]],
    entity_rows: Sequence[Mapping[str, Any]],
    *,
    relationship_types: Mapping[str, RelationshipTypeDefinition],
    issues: _IssueCollector,
) -> None:
    """Apply closed-world endpoint cardinality to the complete publication.

    This deliberately mirrors the publication gate: repeated assertions for
    the same directed entity pair count once, while every active canonical
    entity in a declared source/target type participates in required checks.
    It must never be reused for an ACL-filtered retrieval subgraph.
    """

    entities: dict[str, str] = {}
    for row in entity_rows:
        entity = _mapping(row.get("entity"))
        entity_id = _text(entity.get("entity_id"))
        entity_type = _text(entity.get("entity_type"))
        if entity_id is not None and entity_type is not None:
            entities[entity_id] = entity_type

    outgoing: dict[tuple[str, str], set[str]] = defaultdict(set)
    incoming: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in revision_rows:
        revision = _mapping(row.get("revision"))
        predicate = _text(revision.get("predicate"))
        subject_id = _text(revision.get("subject_entity_id"))
        object_id = _text(revision.get("object_entity_id"))
        relationship = relationship_types.get(predicate or "")
        if (
            revision.get("object_kind") != "entity"
            or relationship is None
            or subject_id is None
            or object_id is None
            or entities.get(subject_id) not in relationship.source_types
            or entities.get(object_id) not in relationship.target_types
        ):
            continue
        outgoing[(relationship.name, subject_id)].add(object_id)
        incoming[(relationship.name, object_id)].add(subject_id)

    for relationship in sorted(
        relationship_types.values(),
        key=lambda item: item.name,
    ):
        for entity_id, entity_type in sorted(entities.items()):
            if entity_type in relationship.source_types:
                count = len(outgoing.get((relationship.name, entity_id), ()))
                if (
                    relationship.source_cardinality.required
                    and count == 0
                ) or (
                    relationship.source_cardinality.single_valued
                    and count > 1
                ):
                    issues.add(
                        "RELATIONSHIP_ENDPOINT_CARDINALITY_INVALID",
                        IssueSeverity.ERROR,
                        "Entity",
                        entity_id,
                        f"relationship {relationship.name} violates source "
                        "endpoint cardinality",
                    )
            if entity_type in relationship.target_types:
                count = len(incoming.get((relationship.name, entity_id), ()))
                if (
                    relationship.target_cardinality.required
                    and count == 0
                ) or (
                    relationship.target_cardinality.single_valued
                    and count > 1
                ):
                    issues.add(
                        "RELATIONSHIP_ENDPOINT_CARDINALITY_INVALID",
                        IssueSeverity.ERROR,
                        "Entity",
                        entity_id,
                        f"relationship {relationship.name} violates target "
                        "endpoint cardinality",
                    )


def _review_sample(
    issues: Sequence[PublishedGraphQualityIssue],
    evidence_by_object: Mapping[tuple[str, str], set[str]],
    *,
    run_id: str,
    limit: int,
) -> tuple[PublishedGraphReviewSampleItem, ...]:
    grouped: dict[tuple[str, str], set[str]] = {}
    for issue in issues:
        grouped.setdefault((issue.object_kind, issue.object_id), set()).add(issue.code)
    keys = sorted(
        grouped,
        key=lambda item: (_stable_hash([run_id, *item]), item),
    )[:limit]
    return tuple(
        PublishedGraphReviewSampleItem(
            object_kind=kind,
            object_id=object_id,
            issue_codes=tuple(sorted(grouped[(kind, object_id)])),
            evidence_chunk_ids=tuple(
                sorted(evidence_by_object.get((kind, object_id), set()))
            )[:3],
        )
        for kind, object_id in keys
    )


class Neo4jPublishedGraphQualityService:
    """Audit one complete active governed publication in one read transaction."""

    def __init__(
        self,
        driver: SessionDriver,
        database: str = "neo4j",
        *,
        limits: PublishedGraphQualityLimits | None = None,
    ) -> None:
        if driver is None:
            raise ValueError("driver must not be None")
        if not isinstance(database, str) or not database.strip():
            raise ValueError("database must not be empty")
        if any(character in database for character in ("\x00", "\r", "\n")):
            raise ValueError("database contains a forbidden control character")
        self.driver = driver
        self.database = database.strip()
        self.limits = limits or PublishedGraphQualityLimits()
        self._transaction_work = unit_of_work(
            metadata={
                "component": "graphrag-published-quality",
                "operation": "audit",
            },
            timeout=self.limits.transaction_timeout_seconds,
        )(self._audit_tx)

    def audit(self, principal: Principal) -> PublishedGraphQualityReport:
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal")
        if not (principal.capabilities & PUBLISHED_QUALITY_CAPABILITIES):
            raise PublishedGraphQualityAuthorizationError()
        try:
            with self.driver.session(database=self.database) as session:
                return session.execute_read(self._transaction_work, principal)
        except PublishedGraphQualityError:
            raise
        except Exception as exc:
            # Driver messages may contain connection strings, query fragments,
            # or data values.  Preserve the cause for logs, never its message.
            raise PublishedGraphQualityUnavailable() from exc

    def _audit_tx(
        self,
        tx: Any,
        principal: Principal,
    ) -> PublishedGraphQualityReport:
        state_rows = _rows(
            tx.run(
                _STATE_QUERY,
                tenant_id=principal.tenant_id,
                groups=sorted(principal.groups),
                revision_limit=self.limits.max_revisions + 1,
            )
        )
        boundary = _publication_boundary(state_rows, principal, self.limits)
        bound_parameters = {
            "tenant_id": boundary.tenant_id,
            "groups": sorted(principal.groups),
            "publication_id": boundary.publication_id,
            "publication_generation": boundary.generation,
            "manifest_hash": boundary.manifest_hash,
            "ontology_version_id": boundary.ontology_version_id,
            "tbox_checksum": boundary.tbox.checksum,
        }
        revision_rows = _rows(
            tx.run(
                _REVISIONS_QUERY,
                **bound_parameters,
                revision_limit=self.limits.max_revisions + 1,
            )
        )
        if len(revision_rows) > self.limits.max_revisions:
            raise PublishedGraphQualityLimitExceeded()
        entity_rows = _rows(
            tx.run(
                _ENTITIES_QUERY,
                **bound_parameters,
                entity_limit=self.limits.max_entities + 1,
            )
        )
        if len(entity_rows) > self.limits.max_entities:
            raise PublishedGraphQualityLimitExceeded()
        return self._report(boundary, revision_rows, entity_rows)

    def _report(
        self,
        boundary: _PublicationBoundary,
        revision_rows: Sequence[Mapping[str, Any]],
        entity_rows: Sequence[Mapping[str, Any]],
    ) -> PublishedGraphQualityReport:
        graph_digest = _stable_hash(
            {
                "ruleset": PUBLISHED_QUALITY_RULESET_VERSION,
                "revisions": sorted(
                    (_mapping(row.get("revision")) for row in revision_rows),
                    key=lambda item: str(item.get("revision_id", "")),
                ),
                "revision_checks": sorted(
                    (
                        {
                            key: value
                            for key, value in dict(row).items()
                            if key not in {"revision"}
                        }
                        for row in revision_rows
                    ),
                    key=lambda item: _stable_hash(item),
                ),
                "entities": sorted(
                    (dict(row) for row in entity_rows),
                    key=lambda item: str(
                        _mapping(item.get("entity")).get("entity_id", "")
                    ),
                ),
            }
        )
        run_seed = _stable_hash(
            {
                "ruleset": PUBLISHED_QUALITY_RULESET_VERSION,
                "tenant_id": boundary.tenant_id,
                "publication_id": boundary.publication_id,
                "publication_generation": boundary.generation,
                "manifest_hash": boundary.manifest_hash,
                "ontology_version_id": boundary.ontology_version_id,
                "tbox_checksum": boundary.tbox.checksum,
                "corpus_revision": boundary.corpus_revision,
                "graph_digest": graph_digest,
                "hub_degree": self.limits.anomalous_hub_degree,
            }
        )
        run_id = "published-graph-quality:" + run_seed
        collector = _IssueCollector(run_seed, self.limits.max_issues)
        entity_types = {item.name: item for item in boundary.tbox.entity_types}
        relationship_types = {
            item.name: item for item in boundary.tbox.relationship_types
        }
        evidence_by_object: dict[tuple[str, str], set[str]] = {}
        literal_counts: dict[tuple[str, str], int] = {}
        mention_count = 0
        assertion_count = 0
        relationship_count = 0
        literal_count = 0
        revision_ids: list[str] = []
        for row in sorted(
            revision_rows,
            key=lambda item: str(_mapping(item.get("revision")).get("revision_id", "")),
        ):
            revision = _mapping(row.get("revision"))
            revision_id = _text(revision.get("revision_id")) or "unknown"
            revision_ids.append(revision_id)
            kind, chunk_id = _audit_revision(
                row,
                boundary=boundary,
                entity_types=entity_types,
                relationship_types=relationship_types,
                issues=collector,
                literal_counts=literal_counts,
            )
            if kind == "ENTITY_MENTION":
                mention_count += 1
                object_kind = "EntityMentionRevision"
            elif kind == "ASSERTION":
                assertion_count += 1
                object_kind = "AssertionRevision"
                if revision.get("object_kind") == "entity":
                    relationship_count += 1
                elif revision.get("object_kind") == "literal":
                    literal_count += 1
            else:
                object_kind = "KnowledgeRevision"
            if chunk_id:
                evidence_by_object.setdefault((object_kind, revision_id), set()).add(
                    chunk_id
                )
        if len(revision_ids) != len(set(revision_ids)):
            collector.add(
                "DUPLICATE_PUBLICATION_REVISION",
                IssueSeverity.ERROR,
                "KnowledgePublication",
                boundary.publication_id,
                "publication contains duplicate revision identifiers",
            )
        if sorted(revision_ids) != sorted(boundary.manifest_revision_ids):
            collector.add(
                "PUBLICATION_MANIFEST_MISMATCH",
                IssueSeverity.ERROR,
                "KnowledgePublication",
                boundary.publication_id,
                "publication edges do not match the immutable revision manifest",
            )

        _audit_entities(
            entity_rows,
            boundary=boundary,
            entity_types=entity_types,
            issues=collector,
            hub_degree=self.limits.anomalous_hub_degree,
        )
        _audit_relationship_endpoint_cardinality(
            revision_rows,
            entity_rows,
            relationship_types=relationship_types,
            issues=collector,
        )
        for entity_row in entity_rows:
            entity = _mapping(entity_row.get("entity"))
            entity_id = _text(entity.get("entity_id"))
            entity_type = _text(entity.get("entity_type"))
            if entity_id is None or entity_type is None:
                continue
            definition = entity_types.get(entity_type)
            if definition is None:
                continue
            for property_definition in definition.properties:
                count = literal_counts.get((entity_id, property_definition.name), 0)
                if property_definition.cardinality.required and count == 0:
                    collector.add(
                        "REQUIRED_ENTITY_PROPERTY_MISSING",
                        IssueSeverity.ERROR,
                        "Entity",
                        entity_id,
                        "a required T-Box entity property has no published assertion",
                    )
                if property_definition.cardinality.single_valued and count > 1:
                    collector.add(
                        "ENTITY_PROPERTY_CARDINALITY_INVALID",
                        IssueSeverity.ERROR,
                        "Entity",
                        entity_id,
                        "a single-valued T-Box entity property has multiple assertions",
                    )
            sample_chunk_id = _text(entity_row.get("sample_chunk_id"))
            if sample_chunk_id:
                evidence_by_object.setdefault(("Entity", entity_id), set()).add(
                    sample_chunk_id
                )

        stored_issues = tuple(
            sorted(
                collector.values,
                key=lambda item: (
                    item.severity.value,
                    item.code,
                    item.object_kind,
                    item.object_id,
                    item.issue_id,
                ),
            )
        )
        counts = tuple(
            sorted(
                {
                    "assertions": assertion_count,
                    "canonical_entities": len(entity_rows),
                    "entity_mentions": mention_count,
                    "literal_assertions": literal_count,
                    "relationship_assertions": relationship_count,
                    "revisions": len(revision_rows),
                }.items()
            )
        )
        return PublishedGraphQualityReport(
            run_id=run_id,
            ruleset_version=PUBLISHED_QUALITY_RULESET_VERSION,
            tenant_id=boundary.tenant_id,
            publication_id=boundary.publication_id,
            publication_generation=boundary.generation,
            manifest_hash=boundary.manifest_hash,
            ontology_version_id=boundary.ontology_version_id,
            tbox_checksum=boundary.tbox.checksum,
            corpus_revision=boundary.corpus_revision,
            graph_digest=graph_digest,
            counts=counts,
            total_issue_count=collector.total,
            total_error_count=collector.errors,
            issues_truncated=collector.total > len(stored_issues),
            issues=stored_issues,
            review_sample=_review_sample(
                stored_issues,
                evidence_by_object,
                run_id=run_id,
                limit=self.limits.sample_size,
            ),
        )


# Short alias for callers that already name the implementation by its domain
# role; both names intentionally refer to the same Neo4j-backed service.
PublishedGraphQualityService = Neo4jPublishedGraphQualityService


__all__ = [
    "PUBLISHED_QUALITY_CAPABILITIES",
    "PUBLISHED_QUALITY_RULESET_VERSION",
    "Neo4jPublishedGraphQualityService",
    "PublishedGraphQualityAuthorizationError",
    "PublishedGraphQualityConflict",
    "PublishedGraphQualityError",
    "PublishedGraphQualityIssue",
    "PublishedGraphQualityLimitExceeded",
    "PublishedGraphQualityLimits",
    "PublishedGraphQualityReport",
    "PublishedGraphQualityService",
    "PublishedGraphQualityUnavailable",
    "PublishedGraphReviewSampleItem",
]
