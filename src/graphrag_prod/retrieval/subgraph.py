"""Trust-aware, evidence-backed subgraph projection for selected Chunks.

This module performs deterministic filtering and ordering only.  It does not
introduce another relevance score or combine authority with retrieval scores.
Every returned graph item is bound to the active governed publication and to
exact, currently authorized source evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import math
from typing import Any, Iterable, Mapping

from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import content_checksum
from graphrag_prod.knowledge.models import EntityIdentity
from graphrag_prod.knowledge.trust import (
    AuthorityLevel,
    GovernanceStatus,
    KnowledgeOrigin,
)


HARD_MAX_SELECTED_CHUNKS = 50
HARD_MAX_ENTITIES = 500
HARD_MAX_ASSERTIONS = 500
HARD_MAX_PATHS = 500
HARD_MAX_MENTIONS_PER_ENTITY = 20
HARD_MAX_MENTION_ROWS = 2_000
HARD_MAX_CHUNK_CHARS = 50_000
HARD_MAX_EVIDENCE_CHARS = 500_000


class SubgraphTrustPolicy(StrEnum):
    """Deterministic authority filters over already published revisions."""

    PUBLISHED_SECONDARY_INCLUSIVE = "PUBLISHED_SECONDARY_INCLUSIVE"
    AUTHORITATIVE_ONLY = "AUTHORITATIVE_ONLY"

    @property
    def authority_levels(self) -> tuple[AuthorityLevel, ...]:
        if self is SubgraphTrustPolicy.AUTHORITATIVE_ONLY:
            return (AuthorityLevel.AUTHORITATIVE,)
        return (AuthorityLevel.AUTHORITATIVE, AuthorityLevel.SECONDARY)


class SubgraphProjectionError(RuntimeError):
    """The database returned an inconsistent or out-of-bound projection."""


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


def _positive_integer(value: object, name: str, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return value


def _nonnegative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _bounded_confidence(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0.0 <= float(value) <= 1.0
    ):
        raise ValueError("confidence must be between zero and one")
    return float(value)


def _checksum(value: object, name: str) -> str:
    normalized = _required_text(value, name).lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{name} must be a SHA-256 digest")
    return normalized


def _native_datetime(value: object, name: str) -> datetime | None:
    if value is None:
        return None
    if hasattr(value, "to_native"):
        value = value.to_native()
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise ValueError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


@dataclass(frozen=True, slots=True)
class EvidenceSubgraphLimits:
    max_selected_chunks: int = 20
    max_entities: int = 100
    max_assertions: int = 100
    max_paths: int = 100
    max_mentions_per_entity: int = 10
    max_chunk_chars: int = 20_000
    max_total_evidence_chars: int = 100_000

    def __post_init__(self) -> None:
        for name, maximum in (
            ("max_selected_chunks", HARD_MAX_SELECTED_CHUNKS),
            ("max_entities", HARD_MAX_ENTITIES),
            ("max_assertions", HARD_MAX_ASSERTIONS),
            ("max_paths", HARD_MAX_PATHS),
            ("max_mentions_per_entity", HARD_MAX_MENTIONS_PER_ENTITY),
            ("max_chunk_chars", HARD_MAX_CHUNK_CHARS),
            ("max_total_evidence_chars", HARD_MAX_EVIDENCE_CHARS),
        ):
            _positive_integer(getattr(self, name), name, maximum)

    @property
    def assertion_row_limit(self) -> int:
        return self.max_assertions

    @property
    def mention_row_limit(self) -> int:
        return min(
            self.max_entities * self.max_mentions_per_entity,
            HARD_MAX_MENTION_ROWS,
        )


@dataclass(frozen=True, slots=True)
class SubgraphCitation:
    tenant_id: str
    chunk_id: str
    chunk_checksum: str
    chunk_text: str
    document_id: str
    document_title: str
    canonical_uri: str
    source_name: str
    version_id: str
    version_checksum: str
    version_number: int
    ordinal: int
    char_start: int
    char_end: int
    page_number: int | None
    section: str | None
    published_at: datetime | None

    def __post_init__(self) -> None:
        for name in (
            "tenant_id",
            "chunk_id",
            "document_id",
            "document_title",
            "canonical_uri",
            "source_name",
            "version_id",
        ):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        if not isinstance(self.chunk_text, str) or not self.chunk_text:
            raise ValueError("chunk_text must not be empty")
        object.__setattr__(
            self,
            "chunk_checksum",
            _checksum(self.chunk_checksum, "chunk_checksum"),
        )
        object.__setattr__(
            self,
            "version_checksum",
            _checksum(self.version_checksum, "version_checksum"),
        )
        if content_checksum(self.chunk_text) != self.chunk_checksum:
            raise ValueError("chunk_checksum must match chunk_text")
        _positive_integer(self.version_number, "version_number", 2_147_483_647)
        _nonnegative_integer(self.ordinal, "ordinal")
        _nonnegative_integer(self.char_start, "char_start")
        if (
            isinstance(self.char_end, bool)
            or not isinstance(self.char_end, int)
            or self.char_end <= self.char_start
            or self.char_end - self.char_start != len(self.chunk_text)
        ):
            raise ValueError("citation Chunk range is invalid")
        if self.page_number is not None:
            _positive_integer(self.page_number, "page_number", 2_147_483_647)
        if self.section is not None:
            object.__setattr__(
                self,
                "section",
                _required_text(self.section, "section"),
            )
        _native_datetime(self.published_at, "published_at")


@dataclass(frozen=True, slots=True)
class SubgraphProvenance:
    publication_id: str
    record_id: str
    revision_id: str
    ontology_version_id: str
    origin: KnowledgeOrigin
    authority: AuthorityLevel
    status: GovernanceStatus
    confidence: float
    extractor_version: str | None
    prompt_version: str | None

    def __post_init__(self) -> None:
        for name in (
            "publication_id",
            "record_id",
            "revision_id",
            "ontology_version_id",
        ):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        if not isinstance(self.origin, KnowledgeOrigin):
            raise TypeError("origin must be KnowledgeOrigin")
        if not isinstance(self.authority, AuthorityLevel):
            raise TypeError("authority must be AuthorityLevel")
        if self.status is not GovernanceStatus.PUBLISHED:
            raise ValueError("subgraph revisions must be PUBLISHED")
        if self.authority is AuthorityLevel.AUTHORITATIVE and self.origin not in {
            KnowledgeOrigin.EXPERT_IMPORT,
            KnowledgeOrigin.EXPERT_CREATED,
        }:
            raise ValueError("authoritative subgraph data requires an expert origin")
        if self.authority is AuthorityLevel.SECONDARY and self.origin in {
            KnowledgeOrigin.EXPERT_IMPORT,
            KnowledgeOrigin.EXPERT_CREATED,
        }:
            raise ValueError("secondary subgraph data cannot claim an expert origin")
        object.__setattr__(self, "confidence", _bounded_confidence(self.confidence))
        for name in ("extractor_version", "prompt_version"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _required_text(value, name))


@dataclass(frozen=True, slots=True)
class SubgraphEvidence:
    citation: SubgraphCitation
    char_start: int
    char_end: int
    quoted_text: str
    provenance: SubgraphProvenance

    def __post_init__(self) -> None:
        _nonnegative_integer(self.char_start, "evidence char_start")
        if (
            isinstance(self.char_end, bool)
            or not isinstance(self.char_end, int)
            or not self.citation.char_start
            <= self.char_start
            < self.char_end
            <= self.citation.char_end
        ):
            raise ValueError("evidence range is outside its citation Chunk")
        if not isinstance(self.quoted_text, str) or not self.quoted_text:
            raise ValueError("quoted evidence must not be empty")
        relative_start = self.char_start - self.citation.char_start
        relative_end = self.char_end - self.citation.char_start
        if self.citation.chunk_text[relative_start:relative_end] != self.quoted_text:
            raise ValueError("quoted evidence does not match exact Chunk text")


@dataclass(frozen=True, slots=True)
class SubgraphEntityNode:
    entity: EntityIdentity
    evidence: tuple[SubgraphEvidence, ...]

    def __post_init__(self) -> None:
        if not self.evidence:
            raise ValueError("subgraph Entity requires published mention evidence")
        if len({item.provenance.revision_id for item in self.evidence}) != len(
            self.evidence
        ):
            raise ValueError("subgraph Entity evidence revisions must be unique")
        if any(
            item.citation.tenant_id != self.entity.tenant_id
            for item in self.evidence
        ):
            raise ValueError("subgraph Entity evidence must share its tenant")

    @property
    def authority_levels(self) -> tuple[AuthorityLevel, ...]:
        return tuple(sorted({item.provenance.authority for item in self.evidence}))

    @property
    def statuses(self) -> tuple[GovernanceStatus, ...]:
        return tuple(sorted({item.provenance.status for item in self.evidence}))

    @property
    def origins(self) -> tuple[KnowledgeOrigin, ...]:
        return tuple(sorted({item.provenance.origin for item in self.evidence}))


@dataclass(frozen=True, slots=True)
class SubgraphAssertion:
    record_id: str
    revision_id: str
    predicate: str
    subject_entity_id: str
    subject_mention_revision_id: str
    object_kind: str
    object_entity_id: str | None
    object_mention_revision_id: str | None
    literal_value: str | None
    evidence: SubgraphEvidence

    def __post_init__(self) -> None:
        for name in (
            "record_id",
            "revision_id",
            "predicate",
            "subject_entity_id",
            "subject_mention_revision_id",
        ):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        if (
            self.record_id != self.evidence.provenance.record_id
            or self.revision_id != self.evidence.provenance.revision_id
        ):
            raise ValueError("assertion identity must match its evidence provenance")
        if self.object_kind not in {"entity", "literal"}:
            raise ValueError("assertion object_kind must be entity or literal")
        if self.object_kind == "entity":
            object.__setattr__(
                self,
                "object_entity_id",
                _required_text(self.object_entity_id, "object_entity_id"),
            )
            object.__setattr__(
                self,
                "object_mention_revision_id",
                _required_text(
                    self.object_mention_revision_id,
                    "object_mention_revision_id",
                ),
            )
            if self.literal_value is not None:
                raise ValueError("entity assertion must not carry literal_value")
        else:
            if (
                self.object_entity_id is not None
                or self.object_mention_revision_id is not None
            ):
                raise ValueError("literal assertion must not carry an object Entity")
            object.__setattr__(
                self,
                "literal_value",
                _required_text(self.literal_value, "literal_value"),
            )
            if self.literal_value not in self.evidence.quoted_text:
                raise ValueError("literal value must occur in assertion evidence")


@dataclass(frozen=True, slots=True)
class SubgraphPath:
    subject_entity_id: str
    assertion_revision_id: str
    predicate: str
    object_entity_id: str | None
    literal_value: str | None
    evidence: SubgraphEvidence

    def __post_init__(self) -> None:
        for name in (
            "subject_entity_id",
            "assertion_revision_id",
            "predicate",
        ):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        if self.assertion_revision_id != self.evidence.provenance.revision_id:
            raise ValueError("path must reference its assertion evidence")
        has_entity = self.object_entity_id is not None
        has_literal = self.literal_value is not None
        if has_entity == has_literal:
            raise ValueError("path requires exactly one Entity or literal object")
        if has_entity:
            object.__setattr__(
                self,
                "object_entity_id",
                _required_text(self.object_entity_id, "object_entity_id"),
            )
        else:
            object.__setattr__(
                self,
                "literal_value",
                _required_text(self.literal_value, "literal_value"),
            )


@dataclass(frozen=True, slots=True)
class EvidenceSubgraph:
    trust_policy: SubgraphTrustPolicy
    entities: tuple[SubgraphEntityNode, ...]
    relationship_assertions: tuple[SubgraphAssertion, ...]
    literal_assertions: tuple[SubgraphAssertion, ...]
    paths: tuple[SubgraphPath, ...]
    matched_chunk_ids: tuple[str, ...]
    publication_ids: tuple[str, ...]

    @property
    def assertions(self) -> tuple[SubgraphAssertion, ...]:
        return (*self.relationship_assertions, *self.literal_assertions)


_MENTION_QUERY = """
// governed-subgraph:mentions
MATCH (state:KnowledgePublicationState {tenant_id: $tenant_id})
      -[:ACTIVE_KNOWLEDGE_PUBLICATION]->
      (publication:KnowledgePublication {tenant_id: $tenant_id, status: 'ACTIVE'})
MATCH (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->
      (mention:GovernedEntityMentionRevision {
          tenant_id: $tenant_id,
          governance_status: 'PUBLISHED'
      })-[:IN_CHUNK]->(chunk:Chunk {tenant_id: $tenant_id})
MATCH (mention)-[:REFERS_TO]->(entity:Entity {tenant_id: $tenant_id})
MATCH (document:Document {tenant_id: $tenant_id})
      -[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot {
          tenant_id: $tenant_id,
          build_state: 'PUBLISHED'
      })-[:INCLUDES_CHUNK]->(chunk)
MATCH (document)-[:ACTIVE_VERSION]->(version:DocumentVersion {tenant_id: $tenant_id})
MATCH (snapshot)-[:OF_VERSION]->(version)
MATCH (publication)-[:USES_KNOWLEDGE_SNAPSHOT]->(snapshot)
MATCH (:TBoxCatalog {tenant_id: $tenant_id})-[:ACTIVE_TBOX_VERSION]->
      (tbox:TBoxVersion {tenant_id: $tenant_id, status: 'PUBLISHED'})
MATCH (tbox)-[:DECLARES_ENTITY_TYPE]->(entity_type:TBoxEntityType)
WHERE chunk.chunk_id IN $chunk_ids
  AND size(chunk.text) <= $max_chunk_chars
  AND mention.authority_level IN $authority_levels
  AND mention.ontology_version_id = tbox.tbox_id
  AND mention.entity_id = entity.entity_id
  AND mention.entity_type = entity.entity_type
  AND entity_type.name = entity.entity_type
  AND mention.document_id = document.document_id
  AND mention.version_id = version.version_id
  AND mention.chunk_id = chunk.chunk_id
  AND mention.access_policy_id = chunk.access_policy_id
  AND mention.access_policy_version = chunk.access_policy_version
  AND mention.access_groups = chunk.access_groups
  AND chunk.char_start <= mention.evidence_char_start
  AND mention.evidence_char_start < mention.evidence_char_end
  AND mention.evidence_char_end <= chunk.char_end
  AND substring(
      chunk.text,
      mention.evidence_char_start - chunk.char_start,
      mention.evidence_char_end - mention.evidence_char_start
  ) = mention.evidence_text
  AND any(group IN $groups WHERE group IN mention.access_groups)
  AND any(group IN $groups WHERE group IN chunk.access_groups)
  AND any(group IN $groups WHERE group IN document.access_groups)
RETURN publication.publication_id AS publication_id,
       entity {
           .entity_id, .tenant_id, .entity_type, .canonical_key,
           .canonical_name, .aliases
       } AS entity,
       mention {.*} AS mention,
       {
           tenant_id: chunk.tenant_id,
           chunk_id: chunk.chunk_id,
           chunk_checksum: chunk.checksum,
           chunk_text: chunk.text,
           document_id: document.document_id,
           document_title: document.title,
           canonical_uri: document.canonical_uri,
           source_name: document.source_name,
           version_id: version.version_id,
           version_checksum: version.checksum,
           version_number: version.version_number,
           ordinal: chunk.ordinal,
           char_start: chunk.char_start,
           char_end: chunk.char_end,
           page_number: chunk.page_number,
           section: chunk.section,
           published_at: version.published_at
       } AS citation
ORDER BY entity.entity_id, mention.revision_id
LIMIT $mention_limit
"""


_ASSERTION_QUERY = """
// governed-subgraph:assertions
MATCH (state:KnowledgePublicationState {tenant_id: $tenant_id})
      -[:ACTIVE_KNOWLEDGE_PUBLICATION]->
      (publication:KnowledgePublication {tenant_id: $tenant_id, status: 'ACTIVE'})
MATCH (:TBoxCatalog {tenant_id: $tenant_id})-[:ACTIVE_TBOX_VERSION]->
      (tbox:TBoxVersion {tenant_id: $tenant_id, status: 'PUBLISHED'})
MATCH (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->
      (seed_mention:GovernedEntityMentionRevision {
          tenant_id: $tenant_id,
          governance_status: 'PUBLISHED'
      })-[:IN_CHUNK]->(seed_chunk:Chunk {tenant_id: $tenant_id})
MATCH (seed_mention)-[:REFERS_TO]->(
      seed_entity:Entity {tenant_id: $tenant_id})
MATCH (seed_document:Document {tenant_id: $tenant_id})
      -[:ACTIVE_SNAPSHOT]->(seed_snapshot:KnowledgeSnapshot {
          tenant_id: $tenant_id,
          build_state: 'PUBLISHED'
      })-[:INCLUDES_CHUNK]->(seed_chunk)
MATCH (seed_document)-[:ACTIVE_VERSION]->(
      seed_version:DocumentVersion {tenant_id: $tenant_id})
MATCH (seed_snapshot)-[:OF_VERSION]->(seed_version)
MATCH (publication)-[:USES_KNOWLEDGE_SNAPSHOT]->(seed_snapshot)
MATCH (tbox)-[:DECLARES_ENTITY_TYPE]->(seed_type:TBoxEntityType)
WHERE seed_chunk.chunk_id IN $chunk_ids
  AND size(seed_chunk.text) <= $max_chunk_chars
  AND seed_mention.authority_level IN $authority_levels
  AND seed_mention.ontology_version_id = tbox.tbox_id
  AND seed_mention.entity_id = seed_entity.entity_id
  AND seed_mention.entity_type = seed_entity.entity_type
  AND seed_type.name = seed_entity.entity_type
  AND seed_mention.document_id = seed_document.document_id
  AND seed_mention.version_id = seed_version.version_id
  AND seed_mention.chunk_id = seed_chunk.chunk_id
  AND seed_mention.access_policy_id = seed_chunk.access_policy_id
  AND seed_mention.access_policy_version = seed_chunk.access_policy_version
  AND seed_mention.access_groups = seed_chunk.access_groups
  AND seed_chunk.char_start <= seed_mention.evidence_char_start
  AND seed_mention.evidence_char_start < seed_mention.evidence_char_end
  AND seed_mention.evidence_char_end <= seed_chunk.char_end
  AND substring(
      seed_chunk.text,
      seed_mention.evidence_char_start - seed_chunk.char_start,
      seed_mention.evidence_char_end - seed_mention.evidence_char_start
  ) = seed_mention.evidence_text
  AND any(group IN $groups WHERE group IN seed_mention.access_groups)
  AND any(group IN $groups WHERE group IN seed_chunk.access_groups)
  AND any(group IN $groups WHERE group IN seed_document.access_groups)
WITH publication, tbox, seed_entity,
     min(seed_chunk.chunk_id) AS seed_chunk_id
ORDER BY seed_entity.entity_id
LIMIT $seed_entity_limit
MATCH (seed_entity)<-[:SUBJECT|OBJECT]-
      (assertion:GovernedAssertionRevision {
          tenant_id: $tenant_id,
          governance_status: 'PUBLISHED'
      })
MATCH (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->(assertion)
MATCH (assertion)-[:EVIDENCED_BY]->(chunk:Chunk {tenant_id: $tenant_id})
MATCH (assertion)-[:SUBJECT]->(subject:Entity {tenant_id: $tenant_id})
OPTIONAL MATCH (assertion)-[:OBJECT]->(object:Entity {tenant_id: $tenant_id})
MATCH (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->
      (subject_mention:GovernedEntityMentionRevision {
          tenant_id: $tenant_id,
          governance_status: 'PUBLISHED'
      })-[:IN_CHUNK]->(chunk)
MATCH (subject_mention)-[:REFERS_TO]->(subject)
OPTIONAL MATCH (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->
      (object_mention:GovernedEntityMentionRevision {
          tenant_id: $tenant_id,
          governance_status: 'PUBLISHED'
      })-[:IN_CHUNK]->(chunk)
WHERE object_mention.revision_id = assertion.object_mention_revision_id
OPTIONAL MATCH (object_mention)-[:REFERS_TO]->(object)
MATCH (document:Document {tenant_id: $tenant_id})
      -[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot {
          tenant_id: $tenant_id,
          build_state: 'PUBLISHED'
      })-[:INCLUDES_CHUNK]->(chunk)
MATCH (document)-[:ACTIVE_VERSION]->(version:DocumentVersion {
    tenant_id: $tenant_id
})
MATCH (snapshot)-[:OF_VERSION]->(version)
MATCH (publication)-[:USES_KNOWLEDGE_SNAPSHOT]->(snapshot)
MATCH (tbox)-[:DECLARES_ENTITY_TYPE]->(subject_type:TBoxEntityType)
OPTIONAL MATCH (tbox)-[:DECLARES_ENTITY_TYPE]->(object_type:TBoxEntityType)
WHERE object IS NOT NULL AND object_type.name = object.entity_type
WITH publication, tbox, seed_entity, seed_chunk_id, assertion, chunk,
     subject, object, subject_mention, object_mention, document, snapshot,
     version, subject_type, object_type
WHERE (seed_entity = subject OR seed_entity = object)
  AND size(chunk.text) <= $max_chunk_chars
  AND assertion.authority_level IN $authority_levels
  AND subject_mention.authority_level IN $authority_levels
  AND (
      assertion.object_kind = 'literal'
      OR object_mention.authority_level IN $authority_levels
  )
  AND assertion.ontology_version_id = tbox.tbox_id
  AND subject_mention.ontology_version_id = tbox.tbox_id
  AND (
      assertion.object_kind = 'literal'
      OR object_mention.ontology_version_id = tbox.tbox_id
  )
  AND assertion.subject_entity_id = subject.entity_id
  AND assertion.subject_mention_revision_id = subject_mention.revision_id
  AND subject_mention.entity_id = subject.entity_id
  AND subject_type.name = subject.entity_type
  AND (
      (
          assertion.object_kind = 'literal'
          AND object IS NULL
          AND assertion.object_entity_id IS NULL
          AND assertion.object_mention_revision_id IS NULL
          AND assertion.literal_value <> ''
          AND assertion.evidence_text CONTAINS assertion.literal_value
          AND EXISTS {
              MATCH (subject_type)-[:DECLARES_PROPERTY]->(
                    property:TBoxPropertyDefinition)
              WHERE property.name = assertion.predicate
          }
      )
      OR (
          assertion.object_kind = 'entity'
          AND object IS NOT NULL
          AND object_mention IS NOT NULL
          AND assertion.object_entity_id = object.entity_id
          AND assertion.object_mention_revision_id = object_mention.revision_id
          AND object_mention.entity_id = object.entity_id
          AND object_type.name = object.entity_type
          AND EXISTS {
              MATCH (tbox)-[:DECLARES_RELATIONSHIP_TYPE]->(
                    relationship_type:TBoxRelationshipType)
              WHERE relationship_type.name = assertion.predicate
                AND subject.entity_type IN relationship_type.source_types
                AND object.entity_type IN relationship_type.target_types
          }
      )
  )
  AND assertion.document_id = document.document_id
  AND assertion.version_id = version.version_id
  AND assertion.chunk_id = chunk.chunk_id
  AND subject_mention.document_id = document.document_id
  AND subject_mention.version_id = version.version_id
  AND subject_mention.chunk_id = chunk.chunk_id
  AND (
      assertion.object_kind = 'literal'
      OR (
          object_mention.document_id = document.document_id
          AND object_mention.version_id = version.version_id
          AND object_mention.chunk_id = chunk.chunk_id
      )
  )
  AND assertion.access_policy_id = chunk.access_policy_id
  AND assertion.access_policy_version = chunk.access_policy_version
  AND assertion.access_groups = chunk.access_groups
  AND subject_mention.access_policy_id = chunk.access_policy_id
  AND subject_mention.access_policy_version = chunk.access_policy_version
  AND subject_mention.access_groups = chunk.access_groups
  AND (
      assertion.object_kind = 'literal'
      OR (
          object_mention.access_policy_id = chunk.access_policy_id
          AND object_mention.access_policy_version = chunk.access_policy_version
          AND object_mention.access_groups = chunk.access_groups
      )
  )
  AND chunk.char_start <= assertion.evidence_char_start
  AND assertion.evidence_char_start < assertion.evidence_char_end
  AND assertion.evidence_char_end <= chunk.char_end
  AND substring(
      chunk.text,
      assertion.evidence_char_start - chunk.char_start,
      assertion.evidence_char_end - assertion.evidence_char_start
  ) = assertion.evidence_text
  AND assertion.evidence_char_start <= subject_mention.evidence_char_start
  AND subject_mention.evidence_char_start < subject_mention.evidence_char_end
  AND subject_mention.evidence_char_end <= assertion.evidence_char_end
  AND substring(
      chunk.text,
      subject_mention.evidence_char_start - chunk.char_start,
      subject_mention.evidence_char_end - subject_mention.evidence_char_start
  ) = subject_mention.evidence_text
  AND (
      assertion.object_kind = 'literal'
      OR (
          assertion.evidence_char_start <= object_mention.evidence_char_start
          AND object_mention.evidence_char_start < object_mention.evidence_char_end
          AND object_mention.evidence_char_end <= assertion.evidence_char_end
          AND substring(
              chunk.text,
              object_mention.evidence_char_start - chunk.char_start,
              object_mention.evidence_char_end - object_mention.evidence_char_start
          ) = object_mention.evidence_text
      )
  )
  AND any(group IN $groups WHERE group IN assertion.access_groups)
  AND any(group IN $groups WHERE group IN subject_mention.access_groups)
  AND (
      assertion.object_kind = 'literal'
      OR any(group IN $groups WHERE group IN object_mention.access_groups)
  )
  AND any(group IN $groups WHERE group IN chunk.access_groups)
  AND any(group IN $groups WHERE group IN document.access_groups)
WITH publication, assertion, subject, object, subject_mention,
     object_mention, chunk, document, version,
     min(seed_chunk_id) AS seed_chunk_id,
     min(seed_entity.entity_id) AS seed_entity_id
ORDER BY assertion.revision_id
LIMIT $assertion_limit
RETURN publication.publication_id AS publication_id,
       seed_chunk_id,
       seed_entity_id,
       assertion {.*} AS assertion,
       subject {
           .entity_id, .tenant_id, .entity_type, .canonical_key,
           .canonical_name, .aliases
       } AS subject,
       object {
           .entity_id, .tenant_id, .entity_type, .canonical_key,
           .canonical_name, .aliases
       } AS object,
       subject_mention {.*} AS subject_mention,
       object_mention {.*} AS object_mention,
       {
           tenant_id: chunk.tenant_id,
           chunk_id: chunk.chunk_id,
           chunk_checksum: chunk.checksum,
           chunk_text: chunk.text,
           document_id: document.document_id,
           document_title: document.title,
           canonical_uri: document.canonical_uri,
           source_name: document.source_name,
           version_id: version.version_id,
           version_checksum: version.checksum,
           version_number: version.version_number,
           ordinal: chunk.ordinal,
           char_start: chunk.char_start,
           char_end: chunk.char_end,
           page_number: chunk.page_number,
           section: chunk.section,
           published_at: version.published_at
       } AS citation
"""


class Neo4jEvidenceSubgraphProjector:
    """Project an authorized 1-hop evidence graph without existence leakage."""

    def __init__(self, driver: object, database: str = "neo4j") -> None:
        if driver is None:
            raise ValueError("driver must not be None")
        self.driver = driver
        self.database = _required_text(database, "database")

    def project(
        self,
        principal: Principal,
        selected_chunk_ids: tuple[str, ...],
        *,
        limits: EvidenceSubgraphLimits | None = None,
        trust_policy: SubgraphTrustPolicy = (
            SubgraphTrustPolicy.PUBLISHED_SECONDARY_INCLUSIVE
        ),
    ) -> EvidenceSubgraph:
        selected_limits = limits or EvidenceSubgraphLimits()
        chunk_ids = self._chunk_ids(selected_chunk_ids, selected_limits)
        if not isinstance(trust_policy, SubgraphTrustPolicy):
            raise TypeError("trust_policy must be SubgraphTrustPolicy")
        parameters: dict[str, object] = {
            "tenant_id": principal.tenant_id,
            "groups": sorted(principal.groups),
            "chunk_ids": list(chunk_ids),
            "authority_levels": [
                item.value for item in trust_policy.authority_levels
            ],
            "max_chunk_chars": selected_limits.max_chunk_chars,
            "seed_entity_limit": selected_limits.max_entities,
        }
        with self.driver.session(  # type: ignore[attr-defined]
            database=self.database
        ) as session:
            assertion_rows = tuple(
                session.run(
                    _ASSERTION_QUERY,
                    **parameters,
                    assertion_limit=selected_limits.assertion_row_limit,
                )
            )
            mention_rows = tuple(
                session.run(
                    _MENTION_QUERY,
                    **parameters,
                    mention_limit=selected_limits.mention_row_limit,
                )
            )
        return self._project_rows(
            principal,
            frozenset(chunk_ids),
            assertion_rows,
            mention_rows,
            selected_limits,
            trust_policy,
        )

    @staticmethod
    def _chunk_ids(
        values: tuple[str, ...], limits: EvidenceSubgraphLimits
    ) -> tuple[str, ...]:
        if not isinstance(values, tuple):
            raise TypeError("selected_chunk_ids must be a tuple")
        if not values:
            raise ValueError("selected_chunk_ids must not be empty")
        if len(values) > limits.max_selected_chunks:
            raise ValueError("selected_chunk_ids exceed the configured limit")
        normalized = tuple(_required_text(value, "chunk_id") for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("selected_chunk_ids must be unique")
        return tuple(sorted(normalized))

    @classmethod
    def _project_rows(
        cls,
        principal: Principal,
        selected_chunk_ids: frozenset[str],
        assertion_rows: Iterable[Mapping[str, object]],
        mention_rows: Iterable[Mapping[str, object]],
        limits: EvidenceSubgraphLimits,
        trust_policy: SubgraphTrustPolicy,
    ) -> EvidenceSubgraph:
        entity_state: dict[str, tuple[EntityIdentity, dict[str, SubgraphEvidence]]] = {}
        assertions: dict[str, SubgraphAssertion] = {}
        citations: dict[str, SubgraphCitation] = {}
        publication_ids: set[str] = set()
        active_publication_id: str | None = None
        reserved_evidence: set[tuple[str, str]] = set()
        evidence_chars = 0

        def citation(
            row: Mapping[str, object],
            *,
            require_selected: bool,
        ) -> SubgraphCitation:
            nonlocal evidence_chars
            value = cls._citation(cls._mapping(row.get("citation"), "citation"))
            cls._check_citation_boundary(
                value,
                principal,
                selected_chunk_ids,
                limits,
                require_selected=require_selected,
            )
            previous = citations.get(value.chunk_id)
            if previous is not None and previous != value:
                raise SubgraphProjectionError("conflicting citation state")
            if previous is None:
                citations[value.chunk_id] = value
            return value

        def reserve(items: tuple[SubgraphEvidence, ...]) -> bool:
            nonlocal evidence_chars
            new_items = tuple(
                item
                for item in items
                if (item.provenance.revision_id, item.citation.chunk_id)
                not in reserved_evidence
            )
            new_citations = {
                item.citation.chunk_id: item.citation
                for item in new_items
                if ("CHUNK", item.citation.chunk_id) not in reserved_evidence
            }
            cost = sum(len(item.quoted_text) for item in new_items) + sum(
                len(item.chunk_text) for item in new_citations.values()
            )
            if evidence_chars + cost > limits.max_total_evidence_chars:
                return False
            evidence_chars += cost
            for item in new_items:
                reserved_evidence.add(
                    (item.provenance.revision_id, item.citation.chunk_id)
                )
            for chunk_id in new_citations:
                reserved_evidence.add(("CHUNK", chunk_id))
            return True

        def entity_capacity(entities: tuple[EntityIdentity, ...]) -> bool:
            new_ids = {item.entity_id for item in entities} - set(entity_state)
            return len(entity_state) + len(new_ids) <= limits.max_entities

        def add_entity(entity: EntityIdentity, evidence: SubgraphEvidence) -> None:
            current = entity_state.get(entity.entity_id)
            if current is None:
                entity_state[entity.entity_id] = (
                    entity,
                    {evidence.provenance.revision_id: evidence},
                )
                return
            if current[0] != entity:
                raise SubgraphProjectionError("conflicting canonical Entity state")
            previous = current[1].get(evidence.provenance.revision_id)
            if previous is not None and previous != evidence:
                raise SubgraphProjectionError("conflicting Entity evidence state")
            if (
                previous is None
                and len(current[1]) < limits.max_mentions_per_entity
            ):
                current[1][evidence.provenance.revision_id] = evidence

        def can_attach_entity_evidence(
            entity: EntityIdentity,
            evidence: SubgraphEvidence,
        ) -> bool:
            current = entity_state.get(entity.entity_id)
            if current is None:
                return True
            return (
                evidence.provenance.revision_id in current[1]
                or len(current[1]) < limits.max_mentions_per_entity
            )

        # Seed Entities and their exact selected-Chunk evidence are admitted
        # first.  Expansion rows cannot manufacture a graph disconnected from
        # the retrieval result merely by naming a plausible Entity ID.
        for raw_row in mention_rows:
            row = cls._mapping(raw_row, "mention row")
            publication_id = _required_text(
                row.get("publication_id"), "publication_id"
            )
            if active_publication_id is None:
                active_publication_id = publication_id
            elif publication_id != active_publication_id:
                raise SubgraphProjectionError(
                    "multiple active knowledge publications were returned"
                )
            citation_value = citation(row, require_selected=True)
            mention_map = cls._mapping(row.get("mention"), "mention")
            cls._check_record_boundary(mention_map, citation_value, principal)
            mention_evidence = cls._evidence(
                mention_map,
                citation_value,
                publication_id,
            )
            cls._check_trust(mention_evidence.provenance, trust_policy)
            entity = cls._identity(
                cls._mapping(row.get("entity"), "entity"),
                principal.tenant_id,
            )
            if mention_map.get("entity_id") != entity.entity_id:
                raise SubgraphProjectionError(
                    "mention crossed its Entity binding"
                )
            if (
                not entity_capacity((entity,))
                or not can_attach_entity_evidence(entity, mention_evidence)
                or not reserve((mention_evidence,))
            ):
                continue
            add_entity(entity, mention_evidence)
            publication_ids.add(publication_id)

        for raw_row in assertion_rows:
            if len(assertions) >= limits.assertion_row_limit:
                break
            row = cls._mapping(raw_row, "assertion row")
            publication_id = _required_text(
                row.get("publication_id"), "publication_id"
            )
            if active_publication_id is None:
                active_publication_id = publication_id
            elif publication_id != active_publication_id:
                raise SubgraphProjectionError(
                    "multiple active knowledge publications were returned"
                )
            seed_chunk_id = _required_text(
                row.get("seed_chunk_id"), "seed_chunk_id"
            )
            seed_entity_id = _required_text(
                row.get("seed_entity_id"), "seed_entity_id"
            )
            if seed_chunk_id not in selected_chunk_ids:
                raise SubgraphProjectionError(
                    "assertion expansion crossed its selected Chunk seed"
                )
            seed_state = entity_state.get(seed_entity_id)
            if seed_state is None or not any(
                item.citation.chunk_id == seed_chunk_id
                for item in seed_state[1].values()
            ):
                continue
            citation_value = citation(row, require_selected=False)
            assertion_map = cls._mapping(row.get("assertion"), "assertion")
            cls._check_record_boundary(assertion_map, citation_value, principal)
            assertion_evidence = cls._evidence(
                assertion_map,
                citation_value,
                publication_id,
            )
            cls._check_trust(assertion_evidence.provenance, trust_policy)
            subject = cls._identity(
                cls._mapping(row.get("subject"), "subject"), principal.tenant_id
            )
            subject_mention_map = cls._mapping(
                row.get("subject_mention"), "subject_mention"
            )
            cls._check_record_boundary(
                subject_mention_map,
                citation_value,
                principal,
            )
            if subject_mention_map.get("entity_id") != subject.entity_id:
                raise SubgraphProjectionError(
                    "subject mention crossed its Entity binding"
                )
            subject_evidence = cls._evidence(
                subject_mention_map,
                citation_value,
                publication_id,
            )
            cls._check_trust(subject_evidence.provenance, trust_policy)
            object_kind = _required_text(
                assertion_map.get("object_kind"), "object_kind"
            )
            object_entity: EntityIdentity | None = None
            object_evidence: SubgraphEvidence | None = None
            if object_kind == "entity":
                object_entity = cls._identity(
                    cls._mapping(row.get("object"), "object"),
                    principal.tenant_id,
                )
                object_evidence = cls._evidence(
                    cls._mapping(row.get("object_mention"), "object_mention"),
                    citation_value,
                    publication_id,
                )
                object_mention_map = cls._mapping(
                    row.get("object_mention"), "object_mention"
                )
                cls._check_record_boundary(
                    object_mention_map,
                    citation_value,
                    principal,
                )
                if object_mention_map.get("entity_id") != object_entity.entity_id:
                    raise SubgraphProjectionError(
                        "object mention crossed its Entity binding"
                    )
                cls._check_trust(object_evidence.provenance, trust_policy)
            elif object_kind != "literal":
                raise SubgraphProjectionError("stored assertion object_kind is invalid")

            if seed_entity_id not in {
                subject.entity_id,
                None if object_entity is None else object_entity.entity_id,
            }:
                raise SubgraphProjectionError(
                    "assertion expansion crossed its seed Entity"
                )

            endpoints = (subject,) + (() if object_entity is None else (object_entity,))
            evidence_items = (assertion_evidence, subject_evidence) + (
                () if object_evidence is None else (object_evidence,)
            )
            endpoint_evidence = ((subject, subject_evidence),) + (
                ()
                if object_entity is None or object_evidence is None
                else ((object_entity, object_evidence),)
            )
            if (
                not entity_capacity(endpoints)
                or not all(
                    can_attach_entity_evidence(entity, evidence)
                    for entity, evidence in endpoint_evidence
                )
                or not reserve(evidence_items)
            ):
                continue
            assertion = SubgraphAssertion(
                record_id=assertion_evidence.provenance.record_id,
                revision_id=assertion_evidence.provenance.revision_id,
                predicate=_required_text(assertion_map.get("predicate"), "predicate"),
                subject_entity_id=subject.entity_id,
                subject_mention_revision_id=subject_evidence.provenance.revision_id,
                object_kind=object_kind,
                object_entity_id=(
                    None if object_entity is None else object_entity.entity_id
                ),
                object_mention_revision_id=(
                    None
                    if object_evidence is None
                    else object_evidence.provenance.revision_id
                ),
                literal_value=(
                    assertion_map.get("literal_value")
                    if object_kind == "literal"
                    else None
                ),
                evidence=assertion_evidence,
            )
            cls._validate_assertion_bindings(
                assertion,
                assertion_map,
                subject_evidence,
                object_evidence,
            )
            previous = assertions.get(assertion.revision_id)
            if previous is not None:
                if previous != assertion:
                    raise SubgraphProjectionError(
                        "conflicting assertion revision state"
                    )
                continue
            assertions[assertion.revision_id] = assertion
            add_entity(subject, subject_evidence)
            if object_entity is not None and object_evidence is not None:
                add_entity(object_entity, object_evidence)
            publication_ids.add(publication_id)

        nodes = tuple(
            SubgraphEntityNode(
                entity=identity,
                evidence=tuple(
                    evidence_by_id[key] for key in sorted(evidence_by_id)
                ),
            )
            for identity, evidence_by_id in sorted(
                entity_state.values(), key=lambda item: item[0].entity_id
            )
        )
        ordered_assertions = tuple(
            assertions[key] for key in sorted(assertions)
        )
        relationship_assertions = tuple(
            item for item in ordered_assertions if item.object_kind == "entity"
        )
        literal_assertions = tuple(
            item for item in ordered_assertions if item.object_kind == "literal"
        )
        paths = tuple(
            SubgraphPath(
                subject_entity_id=item.subject_entity_id,
                assertion_revision_id=item.revision_id,
                predicate=item.predicate,
                object_entity_id=item.object_entity_id,
                literal_value=item.literal_value,
                evidence=item.evidence,
            )
            for item in ordered_assertions[: limits.max_paths]
        )
        matched_chunk_ids = tuple(
            sorted(
                {
                    evidence.citation.chunk_id
                    for node in nodes
                    for evidence in node.evidence
                }
                | {
                    assertion.evidence.citation.chunk_id
                    for assertion in ordered_assertions
                }
            )
        )
        return EvidenceSubgraph(
            trust_policy=trust_policy,
            entities=nodes,
            relationship_assertions=relationship_assertions,
            literal_assertions=literal_assertions,
            paths=paths,
            matched_chunk_ids=matched_chunk_ids,
            publication_ids=tuple(sorted(publication_ids)),
        )

    @staticmethod
    def _mapping(value: object, name: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise SubgraphProjectionError(f"stored {name} is not an object")
        return value

    @staticmethod
    def _identity(
        values: Mapping[str, Any], expected_tenant_id: str
    ) -> EntityIdentity:
        entity = EntityIdentity(
            entity_id=values["entity_id"],
            tenant_id=values["tenant_id"],
            entity_type=values["entity_type"],
            canonical_key=values["canonical_key"],
            canonical_name=values["canonical_name"],
            aliases=tuple(values.get("aliases") or ()),
        )
        if entity.tenant_id != expected_tenant_id:
            raise SubgraphProjectionError("stored Entity crossed the tenant boundary")
        return entity

    @staticmethod
    def _citation(values: Mapping[str, Any]) -> SubgraphCitation:
        return SubgraphCitation(
            tenant_id=values["tenant_id"],
            chunk_id=values["chunk_id"],
            chunk_checksum=values["chunk_checksum"],
            chunk_text=values["chunk_text"],
            document_id=values["document_id"],
            document_title=values["document_title"],
            canonical_uri=values["canonical_uri"],
            source_name=values["source_name"],
            version_id=values["version_id"],
            version_checksum=values["version_checksum"],
            version_number=values["version_number"],
            ordinal=values["ordinal"],
            char_start=values["char_start"],
            char_end=values["char_end"],
            page_number=values.get("page_number"),
            section=values.get("section"),
            published_at=_native_datetime(values.get("published_at"), "published_at"),
        )

    @staticmethod
    def _provenance(
        values: Mapping[str, Any], publication_id: str
    ) -> SubgraphProvenance:
        try:
            origin = KnowledgeOrigin(values["origin"])
            authority = AuthorityLevel(values["authority_level"])
            status = GovernanceStatus(values["governance_status"])
        except (KeyError, ValueError) as exc:
            raise SubgraphProjectionError("stored trust metadata is invalid") from exc
        return SubgraphProvenance(
            publication_id=publication_id,
            record_id=values["record_id"],
            revision_id=values["revision_id"],
            ontology_version_id=values["ontology_version_id"],
            origin=origin,
            authority=authority,
            status=status,
            confidence=values["confidence"],
            extractor_version=values.get("extractor_version"),
            prompt_version=values.get("prompt_version"),
        )

    @classmethod
    def _evidence(
        cls,
        values: Mapping[str, Any],
        citation: SubgraphCitation,
        publication_id: str,
    ) -> SubgraphEvidence:
        return SubgraphEvidence(
            citation=citation,
            char_start=values["evidence_char_start"],
            char_end=values["evidence_char_end"],
            quoted_text=values["evidence_text"],
            provenance=cls._provenance(values, publication_id),
        )

    @staticmethod
    def _check_citation_boundary(
        citation: SubgraphCitation,
        principal: Principal,
        selected_chunk_ids: frozenset[str],
        limits: EvidenceSubgraphLimits,
        *,
        require_selected: bool,
    ) -> None:
        if (
            citation.tenant_id != principal.tenant_id
            or (
                require_selected
                and citation.chunk_id not in selected_chunk_ids
            )
            or len(citation.chunk_text) > limits.max_chunk_chars
        ):
            raise SubgraphProjectionError(
                "citation crossed the authorized query boundary"
            )

    @staticmethod
    def _check_trust(
        provenance: SubgraphProvenance,
        policy: SubgraphTrustPolicy,
    ) -> None:
        if provenance.authority not in policy.authority_levels:
            raise SubgraphProjectionError("revision crossed the trust-policy boundary")

    @staticmethod
    def _check_record_boundary(
        values: Mapping[str, Any],
        citation: SubgraphCitation,
        principal: Principal,
    ) -> None:
        if (
            values.get("tenant_id") != principal.tenant_id
            or values.get("document_id") != citation.document_id
            or values.get("version_id") != citation.version_id
            or values.get("chunk_id") != citation.chunk_id
        ):
            raise SubgraphProjectionError("revision crossed its source boundary")
        groups = values.get("access_groups")
        if (
            not isinstance(groups, (list, tuple))
            or any(not isinstance(group, str) for group in groups)
            or not principal.groups.intersection(groups)
        ):
            raise SubgraphProjectionError("revision crossed its ACL boundary")

    @staticmethod
    def _validate_assertion_bindings(
        assertion: SubgraphAssertion,
        values: Mapping[str, Any],
        subject_evidence: SubgraphEvidence,
        object_evidence: SubgraphEvidence | None,
    ) -> None:
        if values.get("subject_entity_id") != assertion.subject_entity_id:
            raise SubgraphProjectionError("assertion subject binding is inconsistent")
        if (
            values.get("subject_mention_revision_id")
            != subject_evidence.provenance.revision_id
        ):
            raise SubgraphProjectionError("assertion subject mention is inconsistent")
        if not (
            assertion.evidence.char_start
            <= subject_evidence.char_start
            < subject_evidence.char_end
            <= assertion.evidence.char_end
        ):
            raise SubgraphProjectionError(
                "subject mention is outside assertion evidence"
            )
        if assertion.object_kind == "entity":
            if values.get("object_entity_id") != assertion.object_entity_id:
                raise SubgraphProjectionError(
                    "assertion object binding is inconsistent"
                )
            if object_evidence is None or (
                values.get("object_mention_revision_id")
                != object_evidence.provenance.revision_id
            ):
                raise SubgraphProjectionError(
                    "assertion object mention is inconsistent"
                )
            if not (
                assertion.evidence.char_start
                <= object_evidence.char_start
                < object_evidence.char_end
                <= assertion.evidence.char_end
            ):
                raise SubgraphProjectionError(
                    "object mention is outside assertion evidence"
                )
