"""Human review and atomic publication for governed A-Box revisions.

Review never mutates extracted knowledge in place. Every decision or expert
edit advances the logical record head to a new immutable revision under an
optimistic compare-and-swap precondition. Publication is a separate,
manifested operation that materializes only approved revisions into the
canonical GraphRAG navigation graph.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Iterable, Mapping, Protocol
from uuid import uuid5

from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import (
    ID_NAMESPACE,
    assertion_id as canonical_assertion_id,
    mention_id as canonical_mention_id,
)
from graphrag_prod.domain.models import RelationshipPropertyValue, TypedLiteralValue
from graphrag_prod.ontology.models import Cardinality

from .models import (
    AssertionRecord,
    EntityIdentity,
    EntityMentionRecord,
    RecordRevision,
)
from .store import (
    KnowledgeConflict,
    KnowledgeStoreError,
    Neo4jKnowledgeStore,
    _property_definition,
    _stored_assertion,
    _stored_mention,
    _validate_literal_semantics,
)
from .trust import GovernanceStatus


MAX_REVIEW_BATCH = 100
MAX_REVIEW_QUEUE = 200
MAX_PUBLICATION_RECORDS = 500
_PUBLICATION_ID_SCHEME = "knowledge-publication:v2"
KNOWLEDGE_REVIEW_CAPABILITY = "knowledge:review"
KNOWLEDGE_PUBLISH_CAPABILITY = "knowledge:publish"


class ReviewRecordKind(StrEnum):
    ENTITY_MENTION = "ENTITY_MENTION"
    ASSERTION = "ASSERTION"


class KnowledgeReviewUnavailable(KnowledgeStoreError):
    """The target is absent, unauthorized, or bound to stale evidence."""


class KnowledgePublicationConflict(KnowledgeStoreError):
    """A publication CAS, manifest, or immutable identity conflicts."""


class KnowledgeAuthorizationError(PermissionError, KnowledgeStoreError):
    """The principal lacks the explicit capability for this operation."""


def _require_capability(principal: Principal, capability: str) -> None:
    if capability not in principal.capabilities:
        raise KnowledgeAuthorizationError(
            f"principal lacks the {capability!r} capability"
        )


def _required_text(value: object, name: str, *, maximum: int = 4_000) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    if len(normalized) > maximum or "\x00" in normalized:
        raise ValueError(f"{name} exceeds its safe text boundary")
    return normalized


def _positive_integer(value: object, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return value


def _aware(value: object, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("confidence must be a number")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise ValueError("confidence must be between zero and one")
    return result


def _stable_id(kind: str, *parts: object) -> str:
    payload = json.dumps(
        [kind, *parts],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return str(uuid5(ID_NAMESPACE, payload))


def _manifest_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _native_datetime(value: object, name: str) -> datetime:
    if hasattr(value, "to_native"):
        value = value.to_native()
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _aware(value, name)


@dataclass(frozen=True, slots=True)
class MentionEdit:
    entity: EntityIdentity
    confidence: float

    def __post_init__(self) -> None:
        if not isinstance(self.entity, EntityIdentity):
            raise TypeError("mention edit entity must be EntityIdentity")
        object.__setattr__(self, "confidence", _confidence(self.confidence))


@dataclass(frozen=True, slots=True)
class AssertionEdit:
    subject: EntityIdentity
    predicate: str
    subject_mention_revision_id: str
    confidence: float
    object_entity: EntityIdentity | None = None
    object_mention_revision_id: str | None = None
    literal_value: str | None = None
    literal_semantics: TypedLiteralValue | None = None
    relationship_properties: tuple[RelationshipPropertyValue, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.subject, EntityIdentity):
            raise TypeError("assertion edit subject must be EntityIdentity")
        object.__setattr__(
            self,
            "predicate",
            _required_text(self.predicate, "predicate"),
        )
        object.__setattr__(
            self,
            "subject_mention_revision_id",
            _required_text(
                self.subject_mention_revision_id,
                "subject_mention_revision_id",
            ),
        )
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        has_entity = self.object_entity is not None
        has_literal = self.literal_value is not None
        if has_entity == has_literal:
            raise ValueError(
                "assertion edit requires exactly one entity or literal object"
            )
        if has_entity:
            if not isinstance(self.object_entity, EntityIdentity):
                raise TypeError("assertion edit object must be EntityIdentity")
            if self.literal_semantics is not None:
                raise ValueError("entity assertion edit must not carry literal semantics")
            properties = tuple(self.relationship_properties)
            if any(
                not isinstance(item, RelationshipPropertyValue)
                for item in properties
            ):
                raise TypeError(
                    "relationship_properties must contain RelationshipPropertyValue values"
                )
            object.__setattr__(self, "relationship_properties", properties)
            object.__setattr__(
                self,
                "object_mention_revision_id",
                _required_text(
                    self.object_mention_revision_id,
                    "object_mention_revision_id",
                ),
            )
        elif self.object_mention_revision_id is not None:
            raise ValueError(
                "literal assertion edit cannot reference an object mention"
            )
        else:
            if self.relationship_properties:
                raise ValueError(
                    "literal assertion edit cannot carry relationship properties"
                )
            object.__setattr__(
                self,
                "literal_value",
                _required_text(self.literal_value, "literal_value"),
            )
            if self.literal_semantics is None:
                raise ValueError(
                    "literal assertion edits require typed semantics"
                )
            if not isinstance(self.literal_semantics, TypedLiteralValue):
                raise TypeError("literal_semantics must be TypedLiteralValue")
            if self.literal_semantics.raw_value != self.literal_value:
                raise ValueError("literal edit must preserve its typed raw_value")


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    record_kind: ReviewRecordKind
    record_id: str
    expected_revision: int
    decision: GovernanceStatus
    reviewed_at: datetime
    notes: str
    edit: MentionEdit | AssertionEdit | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.record_kind, ReviewRecordKind):
            raise TypeError("record_kind must be ReviewRecordKind")
        object.__setattr__(
            self,
            "record_id",
            _required_text(self.record_id, "record_id"),
        )
        _positive_integer(
            self.expected_revision,
            "expected_revision",
            2_147_483_647,
        )
        if not isinstance(self.decision, GovernanceStatus):
            raise TypeError("decision must be GovernanceStatus")
        if self.decision not in {
            GovernanceStatus.APPROVED,
            GovernanceStatus.REJECTED,
            GovernanceStatus.QUARANTINED,
        }:
            raise ValueError(
                "review decision must be APPROVED, REJECTED, or QUARANTINED"
            )
        _aware(self.reviewed_at, "reviewed_at")
        object.__setattr__(
            self,
            "notes",
            _required_text(self.notes, "review notes"),
        )
        if self.edit is not None:
            expected = (
                MentionEdit
                if self.record_kind is ReviewRecordKind.ENTITY_MENTION
                else AssertionEdit
            )
            if not isinstance(self.edit, expected):
                raise TypeError("review edit does not match record_kind")


@dataclass(frozen=True, slots=True)
class ReviewQueueItem:
    record_kind: ReviewRecordKind
    record: EntityMentionRecord | AssertionRecord


@dataclass(frozen=True, slots=True)
class ReviewOutcome:
    record_kind: ReviewRecordKind
    record_id: str
    previous_revision_id: str
    revision_id: str
    revision: int
    status: GovernanceStatus


@dataclass(frozen=True, slots=True)
class ReviewBatchResult:
    tenant_id: str
    outcomes: tuple[ReviewOutcome, ...]


@dataclass(frozen=True, slots=True)
class KnowledgePublicationView:
    publication_id: str
    tenant_id: str
    ontology_version_id: str
    generation: int
    manifest_hash: str
    source_revision_ids: tuple[str, ...]
    published_revision_ids: tuple[str, ...]
    removed_record_ids: tuple[str, ...]
    replaced_record_ids: tuple[str, ...]
    status: str
    created_by: str
    created_at: datetime
    activated_at: datetime | None
    rolled_back_by: str | None = None
    rolled_back_at: datetime | None = None


def _publication_view(properties: dict[str, Any]) -> KnowledgePublicationView:
    return KnowledgePublicationView(
        publication_id=properties["publication_id"],
        tenant_id=properties["tenant_id"],
        ontology_version_id=_publication_ontology_id(properties),
        generation=int(properties["generation"]),
        manifest_hash=properties["manifest_hash"],
        source_revision_ids=tuple(properties.get("source_revision_ids", ())),
        published_revision_ids=tuple(
            properties.get("published_revision_ids", ())
        ),
        removed_record_ids=tuple(properties.get("removed_record_ids", ())),
        replaced_record_ids=tuple(
            properties.get("replaced_record_ids", ())
        ),
        status=properties["status"],
        created_by=properties["created_by"],
        created_at=_native_datetime(properties["created_at"], "created_at"),
        activated_at=(
            None
            if properties.get("activated_at") is None
            else _native_datetime(
                properties["activated_at"],
                "activated_at",
            )
        ),
        rolled_back_by=properties.get("rolled_back_by"),
        rolled_back_at=(
            None
            if properties.get("rolled_back_at") is None
            else _native_datetime(
                properties["rolled_back_at"],
                "rolled_back_at",
            )
        ),
    )


def _publication_ontology_id(properties: Mapping[str, Any]) -> str:
    try:
        return _required_text(
            properties["ontology_version_id"],
            "publication ontology_version_id",
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise KnowledgePublicationConflict(
            "knowledge publication is not bound to an immutable T-Box"
        ) from exc


def _active_revision_query(
    kind: ReviewRecordKind,
    *,
    one_record: bool,
) -> str:
    if kind is ReviewRecordKind.ENTITY_MENTION:
        label = "GovernedEntityMentionRevision"
        chunk_edge = "IN_CHUNK"
        record_kind = "ENTITY_MENTION"
    else:
        label = "GovernedAssertionRevision"
        chunk_edge = "EVIDENCED_BY"
        record_kind = "ASSERTION"
    record_filter = (
        "AND head.record_id = $record_id "
        "AND revision.revision = $expected_revision"
        if one_record
        else "AND revision.governance_status IN $statuses"
    )
    return f"""
        MATCH (head:KnowledgeRecordHead {{
            tenant_id: $tenant_id,
            record_kind: '{record_kind}'
        }})-[:CURRENT_REVISION]->(revision:{label})
              -[:{chunk_edge}]->(chunk:Chunk {{tenant_id: $tenant_id}})
        MATCH (document:Document {{tenant_id: $tenant_id}})
              -[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot {{
                  tenant_id: $tenant_id,
                  build_state: 'PUBLISHED'
              }})-[:INCLUDES_CHUNK]->(chunk)
        MATCH (document)-[:ACTIVE_VERSION]->(version:DocumentVersion {{
            tenant_id: $tenant_id
        }})
        MATCH (snapshot)-[:OF_VERSION]->(version)
        MATCH (:TBoxCatalog {{tenant_id: $tenant_id}})
              -[:ACTIVE_TBOX_VERSION]->(tbox:TBoxVersion {{
                  tenant_id: $tenant_id,
                  status: 'PUBLISHED'
              }})
        WHERE revision.ontology_version_id = tbox.tbox_id
          {record_filter}
          AND revision.document_id = document.document_id
          AND revision.version_id = version.version_id
          AND revision.chunk_id = chunk.chunk_id
          AND revision.access_policy_id = chunk.access_policy_id
          AND revision.access_policy_version = chunk.access_policy_version
          AND revision.access_groups = chunk.access_groups
          AND substring(
              chunk.text,
              revision.evidence_char_start - chunk.char_start,
              revision.evidence_char_end - revision.evidence_char_start
          ) = revision.evidence_text
          AND any(group IN $groups WHERE group IN revision.access_groups)
          AND any(group IN $groups WHERE group IN chunk.access_groups)
          AND any(group IN $groups WHERE group IN document.access_groups)
        RETURN revision {{.*}} AS revision
        ORDER BY revision.created_at, revision.record_id
        LIMIT $limit
    """


_REVIEW_QUERY = {
    kind: _active_revision_query(kind, one_record=False)
    for kind in ReviewRecordKind
}
_CURRENT_REVIEW_QUERY = {
    kind: _active_revision_query(kind, one_record=True)
    for kind in ReviewRecordKind
}

_DEPENDENT_ASSERTION_QUERY = """
MATCH (head:KnowledgeRecordHead {
    tenant_id: $tenant_id,
    record_kind: 'ASSERTION'
})-[:CURRENT_REVISION]->(revision:GovernedAssertionRevision {
    tenant_id: $tenant_id
})-[:EVIDENCED_BY]->(chunk:Chunk {tenant_id: $tenant_id})
MATCH (document:Document {tenant_id: $tenant_id})
      -[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot {
          tenant_id: $tenant_id,
          build_state: 'PUBLISHED'
      })-[:INCLUDES_CHUNK]->(chunk)
MATCH (document)-[:ACTIVE_VERSION]->(version:DocumentVersion {
    tenant_id: $tenant_id
})
MATCH (snapshot)-[:OF_VERSION]->(version)
MATCH (:TBoxCatalog {tenant_id: $tenant_id})
      -[:ACTIVE_TBOX_VERSION]->(tbox:TBoxVersion {
          tenant_id: $tenant_id,
          tbox_id: $ontology_version_id,
          status: 'PUBLISHED'
      })
WHERE revision.ontology_version_id = tbox.tbox_id
  AND revision.governance_status IN ['CANDIDATE', 'QUARANTINED']
  AND (
      revision.subject_mention_revision_id = $mention_revision_id OR
      revision.object_mention_revision_id = $mention_revision_id
  )
  AND revision.document_id = document.document_id
  AND revision.version_id = version.version_id
  AND revision.chunk_id = chunk.chunk_id
  AND revision.access_policy_id = chunk.access_policy_id
  AND revision.access_policy_version = chunk.access_policy_version
  AND revision.access_groups = chunk.access_groups
  AND substring(
      chunk.text,
      revision.evidence_char_start - chunk.char_start,
      revision.evidence_char_end - revision.evidence_char_start
  ) = revision.evidence_text
  AND any(group IN $groups WHERE group IN revision.access_groups)
  AND any(group IN $groups WHERE group IN chunk.access_groups)
  AND any(group IN $groups WHERE group IN document.access_groups)
RETURN revision {.*} AS revision
ORDER BY revision.record_id
LIMIT $limit
"""


class SessionDriver(Protocol):
    def session(self, **kwargs: object) -> Any: ...


class Neo4jKnowledgeReviewService:
    """Tenant/ACL-safe review queue and append-only decision workflow."""

    def __init__(self, driver: SessionDriver, database: str = "neo4j") -> None:
        self.driver = driver
        self.database = database

    def review_queue(
        self,
        principal: Principal,
        *,
        statuses: Iterable[GovernanceStatus] = (
            GovernanceStatus.CANDIDATE,
            GovernanceStatus.QUARANTINED,
        ),
        limit: int = 100,
    ) -> tuple[ReviewQueueItem, ...]:
        _require_capability(principal, KNOWLEDGE_REVIEW_CAPABILITY)
        limit = _positive_integer(limit, "limit", MAX_REVIEW_QUEUE)
        normalized_statuses = tuple(statuses)
        if not normalized_statuses or any(
            not isinstance(status, GovernanceStatus)
            for status in normalized_statuses
        ):
            raise TypeError("statuses must contain GovernanceStatus values")
        parameters = {
            "tenant_id": principal.tenant_id,
            "groups": sorted(principal.groups),
            "statuses": sorted(
                {status.value for status in normalized_statuses}
            ),
            "limit": limit,
        }
        items: list[ReviewQueueItem] = []
        with self.driver.session(database=self.database) as session:
            for kind in ReviewRecordKind:
                records = session.run(_REVIEW_QUERY[kind], **parameters)
                decoder = (
                    _stored_mention
                    if kind is ReviewRecordKind.ENTITY_MENTION
                    else _stored_assertion
                )
                items.extend(
                    ReviewQueueItem(
                        kind,
                        decoder(dict(row["revision"])),
                    )
                    for row in records
                )
        items.sort(
            key=lambda item: (
                item.record.created_at,
                item.record.record_id,
                item.record_kind.value,
            )
        )
        return tuple(items[:limit])

    def review_batch(
        self,
        principal: Principal,
        requests: tuple[ReviewRequest, ...],
    ) -> ReviewBatchResult:
        _require_capability(principal, KNOWLEDGE_REVIEW_CAPABILITY)
        if not requests:
            raise ValueError("review batch must not be empty")
        if len(requests) > MAX_REVIEW_BATCH:
            raise ValueError(
                f"review batch exceeds the {MAX_REVIEW_BATCH}-record limit"
            )
        record_ids = [request.record_id for request in requests]
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("review batch contains duplicate record IDs")
        order = {
            request.record_id: index
            for index, request in enumerate(requests)
        }
        work = tuple(
            sorted(
                requests,
                key=lambda request: (
                    request.record_kind is ReviewRecordKind.ASSERTION,
                    order[request.record_id],
                ),
            )
        )
        with self.driver.session(database=self.database) as session:
            outcomes = session.execute_write(
                self._review_batch_tx,
                principal,
                work,
            )
        outcome_by_record = {
            outcome.record_id: outcome for outcome in outcomes
        }
        return ReviewBatchResult(
            tenant_id=principal.tenant_id,
            outcomes=tuple(
                outcome_by_record[record_id] for record_id in record_ids
            ),
        )

    def apply_entity_resolution(
        self,
        principal: Principal,
        *,
        record_id: str,
        expected_revision: int,
        target: EntityIdentity,
        reviewed_at: datetime,
        notes: str,
    ) -> ReviewBatchResult:
        """Atomically link a mention and rebind every dependent candidate fact.

        Dependent assertions remain in their existing candidate/quarantine
        lane.  Only their immutable endpoint identity and mention revision are
        revised, so entity resolution never silently approves extracted facts.
        """

        _require_capability(principal, KNOWLEDGE_REVIEW_CAPABILITY)
        record_id = _required_text(record_id, "record_id")
        _positive_integer(expected_revision, "expected_revision", 2_147_483_647)
        if not isinstance(target, EntityIdentity):
            raise TypeError("resolution target must be an EntityIdentity")
        if target.tenant_id != principal.tenant_id:
            raise KnowledgeReviewUnavailable("review target is unavailable")
        reviewed_at = _aware(reviewed_at, "reviewed_at")
        notes = _required_text(notes, "review notes")
        with self.driver.session(database=self.database) as session:
            outcomes = session.execute_write(
                self._apply_entity_resolution_tx,
                principal,
                record_id,
                expected_revision,
                target,
                reviewed_at,
                notes,
            )
        return ReviewBatchResult(principal.tenant_id, outcomes)

    @classmethod
    def _apply_entity_resolution_tx(
        cls,
        tx: Any,
        principal: Principal,
        record_id: str,
        expected_revision: int,
        target: EntityIdentity,
        reviewed_at: datetime,
        notes: str,
    ) -> tuple[ReviewOutcome, ...]:
        mention_request = ReviewRequest(
            ReviewRecordKind.ENTITY_MENTION,
            record_id,
            expected_revision,
            GovernanceStatus.APPROVED,
            reviewed_at,
            notes,
            MentionEdit(target, 1.0),
        )
        cls._lock_tenant_corpus_tx(tx, principal.tenant_id, reviewed_at)
        cls._lock_review_head_tx(tx, principal, mention_request)
        current = cls._load_current_review_record_tx(tx, principal, mention_request)
        if not isinstance(current, EntityMentionRecord) or current.trust.status not in {
            GovernanceStatus.CANDIDATE,
            GovernanceStatus.QUARANTINED,
        }:
            raise KnowledgeReviewUnavailable("review target is unavailable")
        if current.entity.entity_type != target.entity_type:
            raise KnowledgeReviewUnavailable("review target is unavailable")

        rows = tuple(
            tx.run(
                _DEPENDENT_ASSERTION_QUERY,
                tenant_id=principal.tenant_id,
                groups=sorted(principal.groups),
                ontology_version_id=current.trust.ontology_version_id,
                mention_revision_id=current.revision_id,
                limit=MAX_REVIEW_BATCH,
            )
        )
        if len(rows) >= MAX_REVIEW_BATCH:
            raise KnowledgeReviewUnavailable(
                "entity resolution exceeds the bounded dependent-assertion limit"
            )
        dependents = tuple(
            _stored_assertion(dict(row["revision"])) for row in rows
        )
        for assertion in dependents:
            lock_request = ReviewRequest(
                ReviewRecordKind.ASSERTION,
                assertion.record_id,
                assertion.revision.revision,
                GovernanceStatus.REJECTED,
                reviewed_at,
                notes,
            )
            cls._lock_review_head_tx(tx, principal, lock_request)

        updated_mention = cls._reviewed_record_tx(
            tx,
            principal,
            current,
            mention_request,
        )
        assert isinstance(updated_mention, EntityMentionRecord)
        updated_mention = dataclasses.replace(
            updated_mention,
            confidence=current.confidence,
        )
        cls._validate_record_tbox_tx(tx, updated_mention)
        Neo4jKnowledgeStore._create_mention_revision_tx(
            tx,
            updated_mention,
            link_canonical_entity=False,
        )

        outcomes = [
            ReviewOutcome(
                ReviewRecordKind.ENTITY_MENTION,
                updated_mention.record_id,
                current.revision_id,
                updated_mention.revision_id,
                updated_mention.revision.revision,
                updated_mention.trust.status,
            )
        ]
        for assertion in dependents:
            is_subject = (
                assertion.subject_mention_revision_id == current.revision_id
            )
            is_object = (
                assertion.object_mention_revision_id == current.revision_id
            )
            if not is_subject and not is_object:
                raise KnowledgeReviewUnavailable("review target is unavailable")
            updated_assertion = dataclasses.replace(
                assertion,
                revision=RecordRevision.next(
                    assertion.record_id,
                    assertion.revision.revision,
                ),
                subject=target if is_subject else assertion.subject,
                subject_mention_revision_id=(
                    updated_mention.revision_id
                    if is_subject
                    else assertion.subject_mention_revision_id
                ),
                object_entity=(
                    target if is_object else assertion.object_entity
                ),
                object_mention_revision_id=(
                    updated_mention.revision_id
                    if is_object
                    else assertion.object_mention_revision_id
                ),
            )
            Neo4jKnowledgeStore._create_assertion_revision_tx(
                tx,
                updated_assertion,
                link_canonical_entities=False,
            )
            outcomes.append(
                ReviewOutcome(
                    ReviewRecordKind.ASSERTION,
                    updated_assertion.record_id,
                    assertion.revision_id,
                    updated_assertion.revision_id,
                    updated_assertion.revision.revision,
                    updated_assertion.trust.status,
                )
            )
        return tuple(outcomes)

    def approve(
        self,
        principal: Principal,
        *,
        record_kind: ReviewRecordKind,
        record_id: str,
        expected_revision: int,
        reviewed_at: datetime,
        notes: str,
        edit: MentionEdit | AssertionEdit | None = None,
    ) -> ReviewOutcome:
        return self.review_batch(
            principal,
            (
                ReviewRequest(
                    record_kind,
                    record_id,
                    expected_revision,
                    GovernanceStatus.APPROVED,
                    reviewed_at,
                    notes,
                    edit,
                ),
            ),
        ).outcomes[0]

    def reject(
        self,
        principal: Principal,
        *,
        record_kind: ReviewRecordKind,
        record_id: str,
        expected_revision: int,
        reviewed_at: datetime,
        notes: str,
    ) -> ReviewOutcome:
        return self.review_batch(
            principal,
            (
                ReviewRequest(
                    record_kind,
                    record_id,
                    expected_revision,
                    GovernanceStatus.REJECTED,
                    reviewed_at,
                    notes,
                ),
            ),
        ).outcomes[0]

    def quarantine(
        self,
        principal: Principal,
        *,
        record_kind: ReviewRecordKind,
        record_id: str,
        expected_revision: int,
        reviewed_at: datetime,
        notes: str,
    ) -> ReviewOutcome:
        return self.review_batch(
            principal,
            (
                ReviewRequest(
                    record_kind,
                    record_id,
                    expected_revision,
                    GovernanceStatus.QUARANTINED,
                    reviewed_at,
                    notes,
                ),
            ),
        ).outcomes[0]

    @classmethod
    def _review_batch_tx(
        cls,
        tx: Any,
        principal: Principal,
        requests: tuple[ReviewRequest, ...],
    ) -> tuple[ReviewOutcome, ...]:
        cls._lock_tenant_corpus_tx(
            tx,
            principal.tenant_id,
            min(request.reviewed_at for request in requests),
        )
        outcomes: list[ReviewOutcome] = []
        for request in requests:
            cls._lock_review_head_tx(tx, principal, request)
            current = cls._load_current_review_record_tx(
                tx,
                principal,
                request,
            )
            updated = cls._reviewed_record_tx(
                tx,
                principal,
                current,
                request,
            )
            # Candidate identities may intentionally use the provisional
            # ``llm-candidate`` namespace. Rejection and quarantine preserve
            # that evidence for audit; only materializable approval must be
            # resolved into the published T-Box identity contract.
            if request.decision is GovernanceStatus.APPROVED:
                cls._validate_record_tbox_tx(tx, updated)
            if isinstance(updated, EntityMentionRecord):
                Neo4jKnowledgeStore._create_mention_revision_tx(
                    tx,
                    updated,
                    link_canonical_entity=False,
                )
                kind = ReviewRecordKind.ENTITY_MENTION
            else:
                Neo4jKnowledgeStore._create_assertion_revision_tx(
                    tx,
                    updated,
                    link_canonical_entities=False,
                )
                kind = ReviewRecordKind.ASSERTION
            outcomes.append(
                ReviewOutcome(
                    record_kind=kind,
                    record_id=updated.record_id,
                    previous_revision_id=current.revision_id,
                    revision_id=updated.revision_id,
                    revision=updated.revision.revision,
                    status=updated.trust.status,
                )
            )
        return tuple(outcomes)

    @staticmethod
    def _lock_tenant_corpus_tx(
        tx: Any,
        tenant_id: str,
        now: datetime,
    ) -> None:
        row = tx.run(
            """
            MERGE (state:TenantCorpusState {tenant_id: $tenant_id})
            ON CREATE SET state.corpus_revision = 0,
                          state.created_at = $now
            SET state.__knowledge_review_lock = randomUUID()
            WITH state
            REMOVE state.__knowledge_review_lock
            RETURN state.tenant_id AS tenant_id
            """,
            tenant_id=tenant_id,
            now=now,
        ).single()
        if row is None:
            raise KnowledgeReviewUnavailable("review target is unavailable")

    @staticmethod
    def _lock_review_head_tx(
        tx: Any,
        principal: Principal,
        request: ReviewRequest,
    ) -> None:
        row = tx.run(
            """
            MATCH (head:KnowledgeRecordHead {
                tenant_id: $tenant_id,
                record_id: $record_id,
                record_kind: $record_kind
            })
            SET head.__human_review_lock = randomUUID()
            WITH head
            REMOVE head.__human_review_lock
            RETURN head.current_revision AS current_revision
            """,
            tenant_id=principal.tenant_id,
            record_id=request.record_id,
            record_kind=request.record_kind.value,
        ).single()
        if row is None:
            raise KnowledgeReviewUnavailable("review target is unavailable")
        if row["current_revision"] != request.expected_revision:
            raise KnowledgeConflict(
                "stale knowledge review revision compare-and-swap"
            )

    @staticmethod
    def _load_current_review_record_tx(
        tx: Any,
        principal: Principal,
        request: ReviewRequest,
    ) -> EntityMentionRecord | AssertionRecord:
        row = tx.run(
            _CURRENT_REVIEW_QUERY[request.record_kind],
            tenant_id=principal.tenant_id,
            groups=sorted(principal.groups),
            record_id=request.record_id,
            expected_revision=request.expected_revision,
            limit=1,
        ).single()
        if row is None:
            raise KnowledgeReviewUnavailable("review target is unavailable")
        properties = dict(row["revision"])
        if request.record_kind is ReviewRecordKind.ENTITY_MENTION:
            return _stored_mention(properties)
        return _stored_assertion(properties)

    @classmethod
    def _reviewed_record_tx(
        cls,
        tx: Any,
        principal: Principal,
        current: EntityMentionRecord | AssertionRecord,
        request: ReviewRequest,
    ) -> EntityMentionRecord | AssertionRecord:
        trust = current.trust.transition_to(
            request.decision,
            reviewed_by=principal.principal_id,
            reviewed_at=request.reviewed_at,
            review_notes=request.notes,
        )
        revision = RecordRevision.next(
            current.record_id,
            request.expected_revision,
        )
        if isinstance(current, EntityMentionRecord):
            if request.edit is not None:
                assert isinstance(request.edit, MentionEdit)
                if request.edit.entity.tenant_id != principal.tenant_id:
                    raise KnowledgeReviewUnavailable(
                        "review target is unavailable"
                    )
                return dataclasses.replace(
                    current,
                    revision=revision,
                    entity=request.edit.entity,
                    confidence=request.edit.confidence,
                    trust=trust,
                )
            return dataclasses.replace(
                current,
                revision=revision,
                trust=trust,
            )

        values: dict[str, Any] = {}
        if request.edit is not None:
            assert isinstance(request.edit, AssertionEdit)
            if request.edit.subject.tenant_id != principal.tenant_id or (
                request.edit.object_entity is not None
                and request.edit.object_entity.tenant_id
                != principal.tenant_id
            ):
                raise KnowledgeReviewUnavailable("review target is unavailable")
            values = {
                "subject": request.edit.subject,
                "predicate": request.edit.predicate,
                "subject_mention_revision_id": (
                    request.edit.subject_mention_revision_id
                ),
                "confidence": request.edit.confidence,
                "object_entity": request.edit.object_entity,
                "object_mention_revision_id": (
                    request.edit.object_mention_revision_id
                ),
                "literal_value": request.edit.literal_value,
                "literal_semantics": request.edit.literal_semantics,
                "relationship_properties": request.edit.relationship_properties,
            }
        updated = dataclasses.replace(
            current,
            revision=revision,
            trust=trust,
            **values,
        )
        if request.decision is GovernanceStatus.APPROVED:
            subject_mention = cls._approved_endpoint_tx(
                tx,
                principal,
                updated.subject_mention_revision_id,
                updated.evidence.chunk_id,
                updated.subject.entity_id,
            )
            object_mention = None
            if updated.object_entity is not None:
                object_mention = cls._approved_endpoint_tx(
                    tx,
                    principal,
                    updated.object_mention_revision_id or "",
                    updated.evidence.chunk_id,
                    updated.object_entity.entity_id,
                )
            updated = dataclasses.replace(
                updated,
                subject_mention_revision_id=subject_mention,
                object_mention_revision_id=object_mention,
            )
        return updated

    @staticmethod
    def _approved_endpoint_tx(
        tx: Any,
        principal: Principal,
        base_revision_id: str,
        chunk_id: str,
        entity_id: str,
    ) -> str:
        head = tx.run(
            """
            MATCH (base:GovernedEntityMentionRevision {
                tenant_id: $tenant_id,
                revision_id: $revision_id
            })
            MATCH (head:KnowledgeRecordHead {
                tenant_id: $tenant_id,
                record_id: base.record_id,
                record_kind: 'ENTITY_MENTION'
            })-[:CURRENT_REVISION]->(current:GovernedEntityMentionRevision)
            RETURN head.record_id AS record_id,
                   current.revision AS revision
            """,
            tenant_id=principal.tenant_id,
            revision_id=base_revision_id,
        ).single()
        if head is None:
            raise KnowledgeReviewUnavailable(
                "approved endpoint mention is unavailable"
            )
        row = tx.run(
            _CURRENT_REVIEW_QUERY[ReviewRecordKind.ENTITY_MENTION],
            tenant_id=principal.tenant_id,
            groups=sorted(principal.groups),
            record_id=head["record_id"],
            expected_revision=head["revision"],
            limit=1,
        ).single()
        if row is None:
            raise KnowledgeReviewUnavailable(
                "approved endpoint mention is unavailable"
            )
        mention = _stored_mention(dict(row["revision"]))
        if (
            mention.trust.status
            not in {GovernanceStatus.APPROVED, GovernanceStatus.PUBLISHED}
            or mention.evidence.chunk_id != chunk_id
            or mention.entity.entity_id != entity_id
        ):
            raise KnowledgeReviewUnavailable(
                "approved endpoint mention is unavailable"
            )
        return mention.revision_id

    @staticmethod
    def _validate_record_tbox_tx(
        tx: Any,
        record: EntityMentionRecord | AssertionRecord,
    ) -> None:
        if isinstance(record, EntityMentionRecord):
            namespace, separator, _ = record.entity.canonical_key.partition(":")
            row = tx.run(
                """
                MATCH (:TBoxCatalog {tenant_id: $tenant_id})
                      -[:ACTIVE_TBOX_VERSION]->(tbox:TBoxVersion {
                          tenant_id: $tenant_id,
                          tbox_id: $tbox_id,
                          status: 'PUBLISHED'
                      })-[:DECLARES_ENTITY_TYPE]->(type:TBoxEntityType {
                          name: $entity_type
                      })
                WHERE $namespace IN type.canonical_key_namespaces
                RETURN type.name AS name
                """,
                tenant_id=record.tenant_id,
                tbox_id=record.trust.ontology_version_id,
                entity_type=record.entity.entity_type,
                namespace=namespace.casefold() if separator else "",
            ).single()
            if row is None:
                raise KnowledgeReviewUnavailable(
                    "edited record is outside the active T-Box"
                )
            return

        if record.object_entity is None:
            row = tx.run(
                """
                MATCH (:TBoxCatalog {tenant_id: $tenant_id})
                      -[:ACTIVE_TBOX_VERSION]->(tbox:TBoxVersion {
                          tenant_id: $tenant_id,
                          tbox_id: $tbox_id,
                          status: 'PUBLISHED'
                })-[:DECLARES_ENTITY_TYPE]->(type:TBoxEntityType {
                          name: $subject_type
                      })-[:DECLARES_PROPERTY]->(property:TBoxPropertyDefinition {
                          name: $predicate
                      })
                RETURN properties(property) AS property
                """,
                tenant_id=record.tenant_id,
                tbox_id=record.trust.ontology_version_id,
                subject_type=record.subject.entity_type,
                predicate=record.predicate,
            ).single()
        else:
            row = tx.run(
                """
                MATCH (:TBoxCatalog {tenant_id: $tenant_id})
                      -[:ACTIVE_TBOX_VERSION]->(tbox:TBoxVersion {
                          tenant_id: $tenant_id,
                          tbox_id: $tbox_id,
                          status: 'PUBLISHED'
                      })-[:DECLARES_RELATIONSHIP_TYPE]->
                      (relationship:TBoxRelationshipType {
                          name: $predicate
                      })
                OPTIONAL MATCH (relationship)-[:DECLARES_PROPERTY]->
                               (property:TBoxPropertyDefinition)
                WHERE $subject_type IN relationship.source_types
                  AND $object_type IN relationship.target_types
                RETURN relationship.name AS name,
                       collect(
                           CASE WHEN property IS NULL THEN NULL
                           ELSE properties(property)
                           END
                       ) AS property_definitions
                """,
                tenant_id=record.tenant_id,
                tbox_id=record.trust.ontology_version_id,
                predicate=record.predicate,
                subject_type=record.subject.entity_type,
                object_type=record.object_entity.entity_type,
            ).single()
        if row is None:
            raise KnowledgeReviewUnavailable(
                "edited record is outside the active T-Box"
            )
        if record.object_entity is None:
            try:
                definition = _property_definition(dict(row["property"]))
                _validate_literal_semantics(
                    record.literal_semantics,
                    definition,
                )
            except (KeyError, TypeError, ValueError, KnowledgeStoreError) as exc:
                raise KnowledgeReviewUnavailable(
                    "edited literal violates the active T-Box"
                ) from exc
        else:
            try:
                definitions = {
                    definition.name: definition
                    for definition in (
                        _property_definition(dict(item))
                        for item in (row.get("property_definitions") or ())
                    )
                }
                counts: dict[str, int] = {}
                for value in record.relationship_properties:
                    definition = definitions.get(value.name)
                    if definition is None:
                        raise KnowledgeStoreError(
                            "relationship property is outside the active T-Box"
                        )
                    _validate_literal_semantics(
                        value.literal_semantics,
                        definition,
                    )
                    counts[value.name] = counts.get(value.name, 0) + 1
                for name, definition in definitions.items():
                    count = counts.get(name, 0)
                    if definition.cardinality.required and count == 0:
                        raise KnowledgeStoreError(
                            f"required relationship property {name} is absent"
                        )
                    if definition.cardinality.single_valued and count > 1:
                        raise KnowledgeStoreError(
                            f"relationship property {name} exceeds cardinality"
                        )
            except (KeyError, TypeError, ValueError, KnowledgeStoreError) as exc:
                raise KnowledgeReviewUnavailable(
                    "edited relationship properties violate the active T-Box"
                ) from exc


class Neo4jKnowledgePublicationService:
    """Manifest, activate, and roll back reviewed canonical graph material."""

    def __init__(self, driver: SessionDriver, database: str = "neo4j") -> None:
        self.driver = driver
        self.database = database

    def publish(
        self,
        principal: Principal,
        approved_revision_ids: tuple[str, ...],
        *,
        expected_active_publication_id: str | None,
        published_at: datetime,
        remove_record_ids: tuple[str, ...] = (),
        replace_record_ids: tuple[str, ...] = (),
    ) -> KnowledgePublicationView:
        """Atomically activate a complete, immutable knowledge-set manifest.

        The named revisions are current ``APPROVED`` records to publish or
        current ``PUBLISHED`` records (for example an authoritative import) to
        add.  The current active manifest is retained by default.  A retained
        logical record can be removed explicitly, or replaced by supplying a
        new revision together with its record ID in ``replace_record_ids``.
        """
        _require_capability(principal, KNOWLEDGE_PUBLISH_CAPABILITY)
        source_ids = self._validated_ids(
            approved_revision_ids,
            "approved_revision_ids",
            "publication input revision ID",
        )
        removed_ids = self._validated_ids(
            remove_record_ids,
            "remove_record_ids",
            "removed record ID",
        )
        replaced_ids = self._validated_ids(
            replace_record_ids,
            "replace_record_ids",
            "replaced record ID",
        )
        if set(removed_ids) & set(replaced_ids):
            raise ValueError(
                "a record cannot be both removed and replaced"
            )
        if len(source_ids) + len(removed_ids) + len(replaced_ids) > (
            MAX_PUBLICATION_RECORDS
        ):
            raise ValueError(
                "publication change set exceeds the "
                f"{MAX_PUBLICATION_RECORDS}-record limit"
            )
        if not source_ids and not removed_ids and not replaced_ids:
            raise ValueError("publication change set must not be empty")
        if replaced_ids and not source_ids:
            raise ValueError("replacement requires a replacement revision")
        published_at = _aware(published_at, "published_at")
        expected = (
            None
            if expected_active_publication_id is None
            else _required_text(
                expected_active_publication_id,
                "expected_active_publication_id",
            )
        )
        publication_id = _stable_id(
            _PUBLICATION_ID_SCHEME,
            principal.tenant_id,
            expected or "",
            source_ids,
            removed_ids,
            replaced_ids,
        )
        with self.driver.session(database=self.database) as session:
            session.execute_write(
                self._publish_tx,
                principal,
                source_ids,
                publication_id,
                expected,
                published_at,
                removed_ids,
                replaced_ids,
            )
        result = self.get(principal, publication_id)
        if result is None:
            raise KnowledgePublicationConflict(
                "published manifest is unavailable"
            )
        return result

    def rollback(
        self,
        principal: Principal,
        target_publication_id: str,
        *,
        expected_active_publication_id: str,
        rolled_back_at: datetime,
    ) -> KnowledgePublicationView:
        _require_capability(principal, KNOWLEDGE_PUBLISH_CAPABILITY)
        target_id = _required_text(
            target_publication_id,
            "target_publication_id",
        )
        expected = _required_text(
            expected_active_publication_id,
            "expected_active_publication_id",
        )
        rolled_back_at = _aware(rolled_back_at, "rolled_back_at")
        with self.driver.session(database=self.database) as session:
            session.execute_write(
                self._rollback_tx,
                principal,
                target_id,
                expected,
                rolled_back_at,
            )
        result = self.get(principal, target_id)
        if result is None:
            raise KnowledgePublicationConflict(
                "rollback target is unavailable"
            )
        return result

    def get(
        self,
        principal: Principal,
        publication_id: str,
    ) -> KnowledgePublicationView | None:
        _require_capability(principal, KNOWLEDGE_PUBLISH_CAPABILITY)
        publication_id = _required_text(publication_id, "publication_id")
        with self.driver.session(database=self.database) as session:
            row = session.run(
                """
                MATCH (publication:KnowledgePublication {
                    tenant_id: $tenant_id,
                    publication_id: $publication_id
                })
                WHERE EXISTS {
                    MATCH (publication)-[:USES_TBOX_VERSION]->(
                        bound_tbox:TBoxVersion {tenant_id: $tenant_id}
                    )
                    WHERE bound_tbox.tbox_id =
                          publication.ontology_version_id
                      AND bound_tbox.status IN ['PUBLISHED', 'RETIRED']
                }
                  AND NOT EXISTS {
                    MATCH (publication)-[:USES_TBOX_VERSION]->(
                        wrong_tbox:TBoxVersion
                    )
                    WHERE wrong_tbox.tenant_id <> $tenant_id
                       OR wrong_tbox.tbox_id <>
                          publication.ontology_version_id
                }
                  AND EXISTS {
                    MATCH (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->()
                }
                  AND NOT EXISTS {
                    MATCH (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->
                          (revision)
                    WHERE NOT EXISTS {
                        MATCH (revision)-[:IN_CHUNK|EVIDENCED_BY]->
                              (chunk:Chunk {tenant_id: $tenant_id})
                        MATCH (document:Document {
                            tenant_id: $tenant_id,
                            document_id: revision.document_id
                        })-[:ACTIVE_SNAPSHOT]->(
                            snapshot:KnowledgeSnapshot {
                                tenant_id: $tenant_id,
                                build_state: 'PUBLISHED'
                            }
                        )-[:INCLUDES_CHUNK]->(chunk)
                        MATCH (document)-[:ACTIVE_VERSION]->(
                            version:DocumentVersion {
                                tenant_id: $tenant_id,
                                version_id: revision.version_id
                            }
                        )
                        MATCH (snapshot)-[:OF_VERSION]->(version)
                        WHERE revision.tenant_id = $tenant_id
                          AND revision.chunk_id = chunk.chunk_id
                          AND revision.access_policy_id =
                              chunk.access_policy_id
                          AND revision.access_policy_version =
                              chunk.access_policy_version
                          AND revision.access_groups = chunk.access_groups
                          AND substring(
                              chunk.text,
                              revision.evidence_char_start - chunk.char_start,
                              revision.evidence_char_end -
                                  revision.evidence_char_start
                          ) = revision.evidence_text
                          AND any(
                              group IN $groups
                              WHERE group IN revision.access_groups
                          )
                          AND any(
                              group IN $groups
                              WHERE group IN chunk.access_groups
                          )
                          AND any(
                              group IN $groups
                              WHERE group IN document.access_groups
                          )
                    }
                }
                RETURN publication {.*} AS publication
                """,
                tenant_id=principal.tenant_id,
                publication_id=publication_id,
                groups=sorted(principal.groups),
            ).single()
        return (
            None
            if row is None
            else _publication_view(dict(row["publication"]))
        )

    def active(
        self,
        principal: Principal,
    ) -> KnowledgePublicationView | None:
        _require_capability(principal, KNOWLEDGE_PUBLISH_CAPABILITY)
        with self.driver.session(database=self.database) as session:
            row = session.run(
                """
                MATCH (:KnowledgePublicationState {
                    tenant_id: $tenant_id
                })-[:ACTIVE_KNOWLEDGE_PUBLICATION]->
                  (publication:KnowledgePublication {
                      tenant_id: $tenant_id,
                      status: 'ACTIVE'
                  })
                WHERE EXISTS {
                    MATCH (publication)-[:USES_TBOX_VERSION]->(
                        bound_tbox:TBoxVersion {tenant_id: $tenant_id}
                    )
                    WHERE bound_tbox.tbox_id =
                          publication.ontology_version_id
                      AND bound_tbox.status IN ['PUBLISHED', 'RETIRED']
                }
                  AND NOT EXISTS {
                    MATCH (publication)-[:USES_TBOX_VERSION]->(
                        wrong_tbox:TBoxVersion
                    )
                    WHERE wrong_tbox.tenant_id <> $tenant_id
                       OR wrong_tbox.tbox_id <>
                          publication.ontology_version_id
                }
                  AND EXISTS {
                    MATCH (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->()
                }
                  AND NOT EXISTS {
                    MATCH (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->
                          (revision)
                    WHERE NOT EXISTS {
                        MATCH (revision)-[:IN_CHUNK|EVIDENCED_BY]->
                              (chunk:Chunk {tenant_id: $tenant_id})
                        MATCH (document:Document {
                            tenant_id: $tenant_id,
                            document_id: revision.document_id
                        })-[:ACTIVE_SNAPSHOT]->(
                            snapshot:KnowledgeSnapshot {
                                tenant_id: $tenant_id,
                                build_state: 'PUBLISHED'
                            }
                        )-[:INCLUDES_CHUNK]->(chunk)
                        MATCH (document)-[:ACTIVE_VERSION]->(
                            version:DocumentVersion {
                                tenant_id: $tenant_id,
                                version_id: revision.version_id
                            }
                        )
                        MATCH (snapshot)-[:OF_VERSION]->(version)
                        WHERE revision.tenant_id = $tenant_id
                          AND revision.chunk_id = chunk.chunk_id
                          AND revision.access_policy_id =
                              chunk.access_policy_id
                          AND revision.access_policy_version =
                              chunk.access_policy_version
                          AND revision.access_groups = chunk.access_groups
                          AND substring(
                              chunk.text,
                              revision.evidence_char_start - chunk.char_start,
                              revision.evidence_char_end -
                                  revision.evidence_char_start
                          ) = revision.evidence_text
                          AND any(
                              group IN $groups
                              WHERE group IN revision.access_groups
                          )
                          AND any(
                              group IN $groups
                              WHERE group IN chunk.access_groups
                          )
                          AND any(
                              group IN $groups
                              WHERE group IN document.access_groups
                          )
                    }
                }
                RETURN publication {.*} AS publication
                """,
                tenant_id=principal.tenant_id,
                groups=sorted(principal.groups),
            ).single()
        return (
            None
            if row is None
            else _publication_view(dict(row["publication"]))
        )

    def history(
        self,
        principal: Principal,
        *,
        limit: int = 100,
    ) -> tuple[KnowledgePublicationView, ...]:
        _require_capability(principal, KNOWLEDGE_PUBLISH_CAPABILITY)
        limit = _positive_integer(limit, "limit", MAX_REVIEW_QUEUE)
        with self.driver.session(database=self.database) as session:
            rows = session.run(
                """
                MATCH (publication:KnowledgePublication {
                    tenant_id: $tenant_id
                })
                WHERE EXISTS {
                    MATCH (publication)-[:USES_TBOX_VERSION]->(
                        bound_tbox:TBoxVersion {tenant_id: $tenant_id}
                    )
                    WHERE bound_tbox.tbox_id =
                          publication.ontology_version_id
                      AND bound_tbox.status IN ['PUBLISHED', 'RETIRED']
                }
                  AND NOT EXISTS {
                    MATCH (publication)-[:USES_TBOX_VERSION]->(
                        wrong_tbox:TBoxVersion
                    )
                    WHERE wrong_tbox.tenant_id <> $tenant_id
                       OR wrong_tbox.tbox_id <>
                          publication.ontology_version_id
                }
                  AND EXISTS {
                    MATCH (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->()
                }
                  AND NOT EXISTS {
                    MATCH (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->
                          (revision)
                    WHERE NOT EXISTS {
                        MATCH (revision)-[:IN_CHUNK|EVIDENCED_BY]->
                              (chunk:Chunk {tenant_id: $tenant_id})
                        MATCH (document:Document {
                            tenant_id: $tenant_id,
                            document_id: revision.document_id
                        })-[:ACTIVE_SNAPSHOT]->(
                            snapshot:KnowledgeSnapshot {
                                tenant_id: $tenant_id,
                                build_state: 'PUBLISHED'
                            }
                        )-[:INCLUDES_CHUNK]->(chunk)
                        MATCH (document)-[:ACTIVE_VERSION]->(
                            version:DocumentVersion {
                                tenant_id: $tenant_id,
                                version_id: revision.version_id
                            }
                        )
                        MATCH (snapshot)-[:OF_VERSION]->(version)
                        WHERE revision.tenant_id = $tenant_id
                          AND revision.chunk_id = chunk.chunk_id
                          AND revision.access_policy_id =
                              chunk.access_policy_id
                          AND revision.access_policy_version =
                              chunk.access_policy_version
                          AND revision.access_groups = chunk.access_groups
                          AND substring(
                              chunk.text,
                              revision.evidence_char_start - chunk.char_start,
                              revision.evidence_char_end -
                                  revision.evidence_char_start
                          ) = revision.evidence_text
                          AND any(
                              group IN $groups
                              WHERE group IN revision.access_groups
                          )
                          AND any(
                              group IN $groups
                              WHERE group IN chunk.access_groups
                          )
                          AND any(
                              group IN $groups
                              WHERE group IN document.access_groups
                          )
                    }
                }
                RETURN publication {.*} AS publication
                ORDER BY publication.generation DESC
                LIMIT $limit
                """,
                tenant_id=principal.tenant_id,
                groups=sorted(principal.groups),
                limit=limit,
            )
            return tuple(
                _publication_view(dict(row["publication"])) for row in rows
            )

    @staticmethod
    def _validated_ids(
        values: tuple[str, ...],
        parameter_name: str,
        item_name: str,
    ) -> tuple[str, ...]:
        if not isinstance(values, tuple):
            raise TypeError(f"{parameter_name} must be a tuple")
        if len(values) > MAX_PUBLICATION_RECORDS:
            raise ValueError(
                "publication exceeds the "
                f"{MAX_PUBLICATION_RECORDS}-record limit"
            )
        normalized = tuple(
            _required_text(value, item_name)
            for value in values
        )
        if len(normalized) != len(set(normalized)):
            raise ValueError(
                f"{parameter_name} contains duplicate IDs"
            )
        return tuple(sorted(normalized))

    @classmethod
    def _publish_tx(
        cls,
        tx: Any,
        principal: Principal,
        source_ids: tuple[str, ...],
        publication_id: str,
        expected_active_id: str | None,
        now: datetime,
        removed_record_ids: tuple[str, ...],
        replaced_record_ids: tuple[str, ...],
    ) -> None:
        Neo4jKnowledgeReviewService._lock_tenant_corpus_tx(
            tx,
            principal.tenant_id,
            now,
        )
        state = cls._lock_publication_state_tx(
            tx,
            principal.tenant_id,
            now,
        )
        current_id = state["active_publication_id"]
        existing = tx.run(
            """
            OPTIONAL MATCH (publication:KnowledgePublication {
                tenant_id: $tenant_id,
                publication_id: $publication_id
            })
            RETURN publication {.*} AS publication
            """,
            tenant_id=principal.tenant_id,
            publication_id=publication_id,
        ).single()["publication"]
        if existing is not None:
            existing = dict(existing)
            if (
                tuple(existing.get("source_revision_ids", ())) != source_ids
                or tuple(existing.get("removed_record_ids", ()))
                != removed_record_ids
                or tuple(existing.get("replaced_record_ids", ()))
                != replaced_record_ids
                or existing.get("created_by") != principal.principal_id
            ):
                raise KnowledgePublicationConflict(
                    "immutable publication identity conflicts"
                )
            if current_id != publication_id:
                raise KnowledgePublicationConflict(
                    "existing publication is not active; use rollback"
                )
            existing = cls._load_completed_publication_tx(
                tx,
                principal.tenant_id,
                publication_id,
            )
            ontology_version_id = _publication_ontology_id(existing)
            records = cls._load_manifest_records_tx(
                tx,
                principal,
                tuple(existing.get("published_revision_ids", ())),
                ontology_version_id=ontology_version_id,
            )
            cls._validate_property_cardinality_tx(
                tx,
                principal.tenant_id,
                records,
                ontology_version_id=ontology_version_id,
                require_active_tbox=False,
            )
            return
        if current_id != expected_active_id:
            raise KnowledgePublicationConflict(
                "active knowledge publication CAS failed"
            )

        carried_entries: tuple[
            tuple[EntityMentionRecord | AssertionRecord, str], ...
        ] = ()
        if current_id is not None:
            current = cls._load_completed_publication_tx(
                tx,
                principal.tenant_id,
                current_id,
            )
            carried_entries = cls._load_manifest_record_entries_tx(
                tx,
                principal,
                tuple(current.get("published_revision_ids", ())),
                ontology_version_id=_publication_ontology_id(current),
            )

        carried_by_record = {
            record.record_id: (record, snapshot_id)
            for record, snapshot_id in carried_entries
        }
        if len(carried_by_record) != len(carried_entries):
            raise KnowledgePublicationConflict(
                "active publication contains duplicate logical records"
            )
        existing_record_ids = set(carried_by_record)
        changed_record_ids = set(removed_record_ids) | set(
            replaced_record_ids
        )
        if not changed_record_ids <= existing_record_ids:
            raise KnowledgePublicationConflict(
                "remove/replace targets must exist in the active publication"
            )
        for record_id in changed_record_ids:
            del carried_by_record[record_id]

        loaded = tuple(
            cls._load_current_publishable_tx(tx, principal, revision_id)
            for revision_id in source_ids
        )
        source_record_ids = [record.record_id for record, _ in loaded]
        if len(source_record_ids) != len(set(source_record_ids)):
            raise KnowledgePublicationConflict(
                "publication inputs contain duplicate logical records"
            )
        if set(source_record_ids) & set(removed_record_ids):
            raise KnowledgePublicationConflict(
                "removed records cannot also be publication inputs"
            )
        collisions = set(source_record_ids) & set(carried_by_record)
        if collisions:
            raise KnowledgePublicationConflict(
                "replacing an active record requires replace_record_ids"
            )

        published_records = [
            record for record, _ in carried_by_record.values()
        ]
        published_snapshots = {
            record.revision_id: snapshot_id
            for record, snapshot_id in carried_by_record.values()
        }
        # Approved assertions may reference mention revisions newly published
        # in this transaction or mention revisions carried from the active
        # manifest.  The translation map makes that endpoint contract exact.
        published_mention_ids = {
            record.revision_id: record.revision_id
            for record in published_records
            if isinstance(record, EntityMentionRecord)
        }
        source_mentions = sorted(
            (
                entry
                for entry in loaded
                if isinstance(entry[0], EntityMentionRecord)
            ),
            key=lambda entry: entry[0].revision_id,
        )
        source_assertions = sorted(
            (
                entry
                for entry in loaded
                if isinstance(entry[0], AssertionRecord)
            ),
            key=lambda entry: entry[0].revision_id,
        )
        for mention, snapshot_id in source_mentions:
            if mention.trust.status is GovernanceStatus.PUBLISHED:
                published = mention
            else:
                published = dataclasses.replace(
                    mention,
                    revision=RecordRevision.next(
                        mention.record_id,
                        mention.revision.revision,
                    ),
                    trust=mention.trust.transition_to(
                        GovernanceStatus.PUBLISHED
                    ),
                )
                Neo4jKnowledgeStore._merge_entity_tx(tx, published.entity)
                Neo4jKnowledgeStore._create_mention_revision_tx(
                    tx,
                    published,
                    link_canonical_entity=True,
                )
            published_records.append(published)
            published_snapshots[published.revision_id] = snapshot_id
            published_mention_ids[mention.revision_id] = (
                published.revision_id
            )

        for assertion, snapshot_id in source_assertions:
            if assertion.trust.status is GovernanceStatus.PUBLISHED:
                published = assertion
            else:
                if (
                    assertion.object_entity is None
                    and assertion.literal_semantics is None
                ):
                    raise KnowledgePublicationConflict(
                        "legacy untyped literal revisions cannot be newly published"
                    )
                required = {assertion.subject_mention_revision_id}
                if assertion.object_mention_revision_id is not None:
                    required.add(assertion.object_mention_revision_id)
                if not required <= set(published_mention_ids):
                    raise KnowledgePublicationConflict(
                        "publication must include every assertion endpoint mention"
                    )
                published = dataclasses.replace(
                    assertion,
                    revision=RecordRevision.next(
                        assertion.record_id,
                        assertion.revision.revision,
                    ),
                    subject_mention_revision_id=published_mention_ids[
                        assertion.subject_mention_revision_id
                    ],
                    object_mention_revision_id=(
                        None
                        if assertion.object_mention_revision_id is None
                        else published_mention_ids[
                            assertion.object_mention_revision_id
                        ]
                    ),
                    trust=assertion.trust.transition_to(
                        GovernanceStatus.PUBLISHED
                    ),
                )
                Neo4jKnowledgeStore._merge_entity_tx(tx, published.subject)
                if published.object_entity is not None:
                    Neo4jKnowledgeStore._merge_entity_tx(
                        tx,
                        published.object_entity,
                    )
                Neo4jKnowledgeStore._create_assertion_revision_tx(
                    tx,
                    published,
                    link_canonical_entities=True,
                )
            published_records.append(published)
            published_snapshots[published.revision_id] = snapshot_id

        if not published_records:
            raise KnowledgePublicationConflict(
                "publication cannot activate an empty knowledge set"
            )
        if len(published_records) > MAX_PUBLICATION_RECORDS:
            raise KnowledgePublicationConflict(
                "active publication exceeds the bounded manifest limit"
            )
        if len({record.record_id for record in published_records}) != len(
            published_records
        ):
            raise KnowledgePublicationConflict(
                "publication contains duplicate logical records"
            )
        final_mention_ids = {
            record.revision_id
            for record in published_records
            if isinstance(record, EntityMentionRecord)
        }
        for record in published_records:
            if not isinstance(record, AssertionRecord):
                continue
            required = {record.subject_mention_revision_id}
            if record.object_mention_revision_id is not None:
                required.add(record.object_mention_revision_id)
            if not required <= final_mention_ids:
                raise KnowledgePublicationConflict(
                    "active publication contains an assertion without its "
                    "endpoint mentions"
                )

        ontology_version_id = cls._validate_property_cardinality_tx(
            tx,
            principal.tenant_id,
            tuple(published_records),
            require_active_tbox=True,
        )

        published_records.sort(key=lambda record: record.revision_id)
        published_ids = tuple(
            record.revision_id for record in published_records
        )
        old_published_ids = tuple(
            sorted(record.revision_id for record, _ in carried_entries)
        )
        if published_ids == old_published_ids:
            raise KnowledgePublicationConflict(
                "publication change set does not change the active manifest"
            )
        manifest = {
            "tenant_id": principal.tenant_id,
            "ontology_version_id": ontology_version_id,
            "base_publication_id": current_id,
            "source_revision_ids": source_ids,
            "published_revision_ids": published_ids,
            "removed_record_ids": removed_record_ids,
            "replaced_record_ids": replaced_record_ids,
            "snapshot_ids": sorted(set(published_snapshots.values())),
        }
        manifest_hash = _manifest_hash(manifest)
        generation = int(state["publication_generation"]) + 1
        created = tx.run(
            """
            MATCH (state:KnowledgePublicationState {
                tenant_id: $tenant_id
            })
            CREATE (publication:KnowledgePublication {
                publication_id: $publication_id,
                tenant_id: $tenant_id,
                generation: $generation,
                manifest_hash: $manifest_hash,
                source_revision_ids: $source_revision_ids,
                published_revision_ids: $published_revision_ids,
                removed_record_ids: $removed_record_ids,
                replaced_record_ids: $replaced_record_ids,
                ontology_version_id: $ontology_version_id,
                base_publication_id: $base_publication_id,
                manifest_version: 3,
                status: 'BUILDING',
                created_by: $created_by,
                created_at: $now
            })
            CREATE (state)-[:HAS_KNOWLEDGE_PUBLICATION]->(publication)
            RETURN publication.publication_id AS publication_id
            """,
            publication_id=publication_id,
            tenant_id=principal.tenant_id,
            generation=generation,
            manifest_hash=manifest_hash,
            source_revision_ids=list(source_ids),
            published_revision_ids=list(published_ids),
            removed_record_ids=list(removed_record_ids),
            replaced_record_ids=list(replaced_record_ids),
            ontology_version_id=ontology_version_id,
            base_publication_id=current_id,
            created_by=principal.principal_id,
            now=now,
        ).single()
        if created is None:
            raise KnowledgePublicationConflict(
                "could not create publication manifest"
            )
        cls._link_manifest_tx(
            tx,
            principal.tenant_id,
            publication_id,
            published_records,
            published_snapshots,
            ontology_version_id,
        )
        cls._deactivate_materialization_tx(
            tx,
            principal.tenant_id,
            current_id,
        )
        cls._materialize_records_tx(
            tx,
            principal,
            publication_id,
            tuple(published_records),
        )
        cls._activate_publication_tx(
            tx,
            principal,
            publication_id,
            current_id,
            generation,
            int(state["activation_generation"]) + 1,
            now,
            action="PUBLISH",
        )

    @classmethod
    def _rollback_tx(
        cls,
        tx: Any,
        principal: Principal,
        target_id: str,
        expected_active_id: str,
        now: datetime,
    ) -> None:
        Neo4jKnowledgeReviewService._lock_tenant_corpus_tx(
            tx,
            principal.tenant_id,
            now,
        )
        state = cls._lock_publication_state_tx(
            tx,
            principal.tenant_id,
            now,
        )
        current_id = state["active_publication_id"]
        if current_id == target_id:
            if expected_active_id != target_id:
                raise KnowledgePublicationConflict(
                    "active knowledge publication CAS failed"
                )
            properties = cls._load_completed_publication_tx(
                tx,
                principal.tenant_id,
                target_id,
            )
            ontology_version_id = _publication_ontology_id(properties)
            # Even an idempotent rollback must not become an ACL or stale-
            # evidence bypass merely because the target is already active.
            records = cls._load_manifest_records_tx(
                tx,
                principal,
                tuple(properties.get("published_revision_ids", ())),
                ontology_version_id=ontology_version_id,
            )
            cls._validate_property_cardinality_tx(
                tx,
                principal.tenant_id,
                records,
                ontology_version_id=ontology_version_id,
                require_active_tbox=False,
            )
            return
        if current_id != expected_active_id:
            raise KnowledgePublicationConflict(
                "active knowledge publication CAS failed"
            )
        properties = cls._load_completed_publication_tx(
            tx,
            principal.tenant_id,
            target_id,
        )
        ontology_version_id = _publication_ontology_id(properties)
        records = cls._load_manifest_records_tx(
            tx,
            principal,
            tuple(properties.get("published_revision_ids", ())),
            ontology_version_id=ontology_version_id,
        )
        cls._validate_property_cardinality_tx(
            tx,
            principal.tenant_id,
            records,
            ontology_version_id=ontology_version_id,
            require_active_tbox=False,
        )
        cls._deactivate_materialization_tx(
            tx,
            principal.tenant_id,
            current_id,
        )
        cls._materialize_records_tx(
            tx,
            principal,
            target_id,
            records,
        )
        cls._activate_publication_tx(
            tx,
            principal,
            target_id,
            current_id,
            int(state["publication_generation"]),
            int(state["activation_generation"]) + 1,
            now,
            action="ROLLBACK",
        )

    @staticmethod
    def _validate_property_cardinality_tx(
        tx: Any,
        tenant_id: str,
        records: tuple[EntityMentionRecord | AssertionRecord, ...],
        *,
        ontology_version_id: str | None = None,
        require_active_tbox: bool = True,
    ) -> str:
        """Validate a complete manifest against one exact immutable T-Box.

        Fresh publications require the tenant's currently active published
        version. Historical replay and rollback validate against the version
        immutably bound to that publication, even after that T-Box is retired.
        """

        ontology_ids = {record.trust.ontology_version_id for record in records}
        if len(ontology_ids) != 1:
            raise KnowledgePublicationConflict(
                "one publication must use exactly one T-Box version"
            )
        ontology_id = next(iter(ontology_ids))
        if (
            ontology_version_id is not None
            and ontology_id != _required_text(
                ontology_version_id,
                "ontology_version_id",
            )
        ):
            raise KnowledgePublicationConflict(
                "publication revisions do not match its immutable T-Box"
            )
        rows = tuple(
            tx.run(
                """
                MATCH (tbox:TBoxVersion {
                    tenant_id: $tenant_id,
                    tbox_id: $tbox_id
                })-[:DECLARES_ENTITY_TYPE]->(type:TBoxEntityType)
                WHERE tbox.status IN ['PUBLISHED', 'RETIRED']
                  AND (
                      NOT $require_active_tbox
                      OR (
                          tbox.status = 'PUBLISHED'
                          AND EXISTS {
                              MATCH (:TBoxCatalog {
                                  tenant_id: $tenant_id
                              })-[:ACTIVE_TBOX_VERSION]->(tbox)
                          }
                      )
                  )
                OPTIONAL MATCH (type)-[:DECLARES_PROPERTY]->
                               (property:TBoxPropertyDefinition)
                RETURN type.name AS entity_type,
                       collect(
                           CASE WHEN property IS NULL THEN NULL
                           ELSE properties(property)
                           END
                       ) AS property_definitions
                """,
                tenant_id=tenant_id,
                tbox_id=ontology_id,
                require_active_tbox=require_active_tbox,
            )
        )
        try:
            definitions = {
                row["entity_type"]: {
                    definition.name: definition
                    for definition in (
                        _property_definition(dict(item))
                        for item in (row["property_definitions"] or ())
                    )
                }
                for row in rows
            }
        except (KeyError, TypeError, ValueError, KnowledgeStoreError) as exc:
            raise KnowledgePublicationConflict(
                "active T-Box property contract is invalid"
            ) from exc
        if not definitions:
            raise KnowledgePublicationConflict(
                "required T-Box is unavailable or has no entity definitions"
            )

        entities: dict[str, EntityIdentity] = {}
        literal_counts: dict[tuple[str, str], int] = {}
        relationship_records: list[AssertionRecord] = []
        for record in records:
            if isinstance(record, EntityMentionRecord):
                entities[record.entity.entity_id] = record.entity
                continue
            entities[record.subject.entity_id] = record.subject
            if record.object_entity is not None:
                entities[record.object_entity.entity_id] = record.object_entity
                relationship_records.append(record)
                continue
            property_definition = definitions.get(
                record.subject.entity_type,
                {},
            ).get(record.predicate)
            if property_definition is None:
                raise KnowledgePublicationConflict(
                    "literal assertion is outside the active T-Box"
                )
            try:
                _validate_literal_semantics(
                    record.literal_semantics,
                    property_definition,
                    allow_persisted_legacy=(
                        record.trust.status is GovernanceStatus.PUBLISHED
                    ),
                )
            except KnowledgeStoreError as exc:
                raise KnowledgePublicationConflict(
                    "literal assertion violates the active T-Box"
                ) from exc
            key = (record.subject.entity_id, record.predicate)
            literal_counts[key] = literal_counts.get(key, 0) + 1

        for entity in entities.values():
            entity_definitions = definitions.get(entity.entity_type)
            if entity_definitions is None:
                raise KnowledgePublicationConflict(
                    "publication entity type is outside the active T-Box"
                )
            for name, definition in entity_definitions.items():
                count = literal_counts.get((entity.entity_id, name), 0)
                if definition.cardinality.required and count == 0:
                    raise KnowledgePublicationConflict(
                        f"required property {entity.entity_type}.{name} is absent"
                    )
                if definition.cardinality.single_valued and count > 1:
                    raise KnowledgePublicationConflict(
                        f"property {entity.entity_type}.{name} exceeds its "
                        "single-valued cardinality"
                    )

        relationship_rows = tuple(
            tx.run(
                """
                MATCH (tbox:TBoxVersion {
                    tenant_id: $tenant_id,
                    tbox_id: $tbox_id
                })
                WHERE tbox.status IN ['PUBLISHED', 'RETIRED']
                  AND (
                      NOT $require_active_tbox
                      OR (
                          tbox.status = 'PUBLISHED'
                          AND EXISTS {
                              MATCH (:TBoxCatalog {
                                  tenant_id: $tenant_id
                              })-[:ACTIVE_TBOX_VERSION]->(tbox)
                          }
                      )
                  )
                OPTIONAL MATCH (tbox)-[:DECLARES_RELATIONSHIP_TYPE]->
                               (relationship:TBoxRelationshipType)
                OPTIONAL MATCH (relationship)-[:DECLARES_PROPERTY]->
                               (property:TBoxPropertyDefinition)
                RETURN relationship.name AS name,
                       relationship.source_types AS source_types,
                       relationship.target_types AS target_types,
                       coalesce(
                           relationship.source_cardinality,
                           'ZERO_OR_MORE'
                       ) AS source_cardinality,
                       coalesce(
                           relationship.target_cardinality,
                           'ZERO_OR_MORE'
                       ) AS target_cardinality,
                       collect(
                           CASE WHEN property IS NULL THEN NULL
                           ELSE properties(property)
                           END
                       ) AS property_definitions
                """,
                tenant_id=tenant_id,
                tbox_id=ontology_id,
                require_active_tbox=require_active_tbox,
            )
        )
        try:
            relationship_definitions = {
                row.get("name"): {
                    "source_types": frozenset(row.get("source_types") or ()),
                    "target_types": frozenset(row.get("target_types") or ()),
                    "source_cardinality": Cardinality(
                        row.get("source_cardinality")
                        or Cardinality.ZERO_OR_MORE.value
                    ),
                    "target_cardinality": Cardinality(
                        row.get("target_cardinality")
                        or Cardinality.ZERO_OR_MORE.value
                    ),
                    "properties": {
                        definition.name: definition
                        for definition in (
                            _property_definition(dict(item))
                            for item in (row.get("property_definitions") or ())
                        )
                    },
                }
                for row in relationship_rows
                if row.get("name") is not None
            }
        except (KeyError, TypeError, ValueError, KnowledgeStoreError) as exc:
            raise KnowledgePublicationConflict(
                "bound T-Box relationship contract is invalid"
            ) from exc

        outgoing: dict[tuple[str, str], set[str]] = {}
        incoming: dict[tuple[str, str], set[str]] = {}
        for record in relationship_records:
            assert record.object_entity is not None
            contract = relationship_definitions.get(record.predicate)
            if contract is None:
                raise KnowledgePublicationConflict(
                    "relationship assertion is outside the bound T-Box"
                )
            if (
                record.subject.entity_type not in contract["source_types"]
                or record.object_entity.entity_type not in contract["target_types"]
            ):
                raise KnowledgePublicationConflict(
                    "relationship assertion violates its bound T-Box domain/range"
                )

            property_counts: dict[str, int] = {}
            for value in record.relationship_properties:
                definition = contract["properties"].get(value.name)
                if definition is None:
                    raise KnowledgePublicationConflict(
                        f"relationship property {record.predicate}.{value.name} "
                        "is outside the bound T-Box"
                    )
                try:
                    _validate_literal_semantics(
                        value.literal_semantics,
                        definition,
                    )
                except KnowledgeStoreError as exc:
                    raise KnowledgePublicationConflict(
                        f"relationship property {record.predicate}.{value.name} "
                        "violates the bound T-Box"
                    ) from exc
                property_counts[value.name] = property_counts.get(value.name, 0) + 1
            for name, definition in contract["properties"].items():
                count = property_counts.get(name, 0)
                if definition.cardinality.required and count == 0:
                    raise KnowledgePublicationConflict(
                        f"required relationship property {record.predicate}.{name} "
                        "is absent"
                    )
                if definition.cardinality.single_valued and count > 1:
                    raise KnowledgePublicationConflict(
                        f"relationship property {record.predicate}.{name} exceeds "
                        "its single-valued cardinality"
                    )

            outgoing.setdefault(
                (record.predicate, record.subject.entity_id),
                set(),
            ).add(record.object_entity.entity_id)
            incoming.setdefault(
                (record.predicate, record.object_entity.entity_id),
                set(),
            ).add(record.subject.entity_id)

        # Endpoint cardinality is deliberately a closed-world publication
        # invariant. ``records`` is the complete final manifest (carried plus
        # new revisions after removals/replacements), so a missing required
        # edge is meaningful here.  Bounded/ACL-filtered retrieval subgraphs
        # must never reuse this validation.
        for predicate, contract in relationship_definitions.items():
            source_cardinality = contract["source_cardinality"]
            target_cardinality = contract["target_cardinality"]
            for entity in entities.values():
                if entity.entity_type in contract["source_types"]:
                    count = len(outgoing.get((predicate, entity.entity_id), set()))
                    if source_cardinality.required and count == 0:
                        raise KnowledgePublicationConflict(
                            f"required relationship {predicate} is absent from "
                            f"source entity {entity.entity_id}"
                        )
                    if source_cardinality.single_valued and count > 1:
                        raise KnowledgePublicationConflict(
                            f"relationship {predicate} exceeds source endpoint "
                            "single-valued cardinality"
                        )
                if entity.entity_type in contract["target_types"]:
                    count = len(incoming.get((predicate, entity.entity_id), set()))
                    if target_cardinality.required and count == 0:
                        raise KnowledgePublicationConflict(
                            f"required relationship {predicate} is absent at "
                            f"target entity {entity.entity_id}"
                        )
                    if target_cardinality.single_valued and count > 1:
                        raise KnowledgePublicationConflict(
                            f"relationship {predicate} exceeds target endpoint "
                            "single-valued cardinality"
                        )
        return ontology_id

    @staticmethod
    def _lock_publication_state_tx(
        tx: Any,
        tenant_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        row = tx.run(
            """
            MERGE (state:KnowledgePublicationState {
                tenant_id: $tenant_id
            })
            ON CREATE SET state.publication_generation = 0,
                          state.activation_generation = 0,
                          state.created_at = $now
            SET state.__publication_cas_lock = randomUUID()
            WITH state
            REMOVE state.__publication_cas_lock
            WITH state
            OPTIONAL MATCH (state)-[:ACTIVE_KNOWLEDGE_PUBLICATION]->
                  (active:KnowledgePublication)
            RETURN coalesce(
                       state.publication_generation,
                       0
                   ) AS publication_generation,
                   coalesce(
                       state.activation_generation,
                       0
                   ) AS activation_generation,
                   active.publication_id AS active_publication_id
            """,
            tenant_id=tenant_id,
            now=now,
        ).single()
        if row is None:
            raise KnowledgePublicationConflict(
                "could not lock knowledge publication state"
            )
        return dict(row)

    @staticmethod
    def _load_completed_publication_tx(
        tx: Any,
        tenant_id: str,
        publication_id: str,
    ) -> dict[str, Any]:
        target = tx.run(
            """
            MATCH (publication:KnowledgePublication {
                tenant_id: $tenant_id,
                publication_id: $publication_id
            })
            MATCH (publication)-[:USES_TBOX_VERSION]->(tbox:TBoxVersion {
                tenant_id: $tenant_id
            })
            WHERE publication.ontology_version_id = tbox.tbox_id
              AND tbox.status IN ['PUBLISHED', 'RETIRED']
              AND NOT EXISTS {
                  MATCH (publication)-[:USES_TBOX_VERSION]->(other:TBoxVersion)
                  WHERE other.tenant_id <> $tenant_id
                     OR other.tbox_id <> publication.ontology_version_id
              }
            RETURN publication {.*} AS publication
            """,
            tenant_id=tenant_id,
            publication_id=publication_id,
        ).single()
        if target is None:
            raise KnowledgeReviewUnavailable(
                "rollback target is unavailable"
            )
        properties = dict(target["publication"])
        if properties.get("status") not in {"ACTIVE", "RETIRED"}:
            raise KnowledgePublicationConflict(
                "rollback target is not a completed publication"
            )
        return properties

    @staticmethod
    def _load_current_publishable_tx(
        tx: Any,
        principal: Principal,
        revision_id: str,
    ) -> tuple[EntityMentionRecord | AssertionRecord, str]:
        return Neo4jKnowledgePublicationService._load_revision_tx(
            tx,
            principal,
            revision_id,
            require_current=True,
            required_statuses=(
                GovernanceStatus.APPROVED,
                GovernanceStatus.PUBLISHED,
            ),
            ontology_version_id=None,
            require_active_tbox=True,
        )

    @staticmethod
    def _load_revision_tx(
        tx: Any,
        principal: Principal,
        revision_id: str,
        *,
        require_current: bool,
        required_statuses: tuple[GovernanceStatus, ...],
        ontology_version_id: str | None,
        require_active_tbox: bool,
    ) -> tuple[EntityMentionRecord | AssertionRecord, str]:
        row = tx.run(
            """
            MATCH (revision {tenant_id: $tenant_id, revision_id: $revision_id})
            WHERE revision:GovernedEntityMentionRevision
               OR revision:GovernedAssertionRevision
            MATCH (head:KnowledgeRecordHead {
                tenant_id: $tenant_id,
                record_id: revision.record_id
            })
            OPTIONAL MATCH (head)-[:CURRENT_REVISION]->(current)
            MATCH (revision)-[:IN_CHUNK|EVIDENCED_BY]->
                  (chunk:Chunk {tenant_id: $tenant_id})
            MATCH (document:Document {tenant_id: $tenant_id})
                  -[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot {
                      tenant_id: $tenant_id,
                      build_state: 'PUBLISHED'
                  })-[:INCLUDES_CHUNK]->(chunk)
            MATCH (document)-[:ACTIVE_VERSION]->(version:DocumentVersion {
                tenant_id: $tenant_id
            })
            MATCH (snapshot)-[:OF_VERSION]->(version)
            MATCH (tbox:TBoxVersion {tenant_id: $tenant_id})
            WHERE revision.governance_status IN $required_statuses
              AND revision.ontology_version_id = tbox.tbox_id
              AND tbox.status IN ['PUBLISHED', 'RETIRED']
              AND (
                  $ontology_version_id IS NULL
                  OR tbox.tbox_id = $ontology_version_id
              )
              AND (
                  NOT $require_active_tbox
                  OR (
                      tbox.status = 'PUBLISHED'
                      AND EXISTS {
                          MATCH (:TBoxCatalog {tenant_id: $tenant_id})
                                -[:ACTIVE_TBOX_VERSION]->(tbox)
                      }
                  )
              )
              AND (
                  NOT $require_current
                  OR current.revision_id = revision.revision_id
              )
              AND revision.document_id = document.document_id
              AND revision.version_id = version.version_id
              AND revision.chunk_id = chunk.chunk_id
              AND revision.access_policy_id = chunk.access_policy_id
              AND revision.access_policy_version =
                  chunk.access_policy_version
              AND revision.access_groups = chunk.access_groups
              AND substring(
                  chunk.text,
                  revision.evidence_char_start - chunk.char_start,
                  revision.evidence_char_end -
                      revision.evidence_char_start
              ) = revision.evidence_text
              AND any(
                  group IN $groups
                  WHERE group IN revision.access_groups
              )
              AND any(
                  group IN $groups
                  WHERE group IN chunk.access_groups
              )
              AND any(
                  group IN $groups
                  WHERE group IN document.access_groups
              )
            RETURN revision {.*} AS revision,
                   labels(revision) AS labels,
                   snapshot.snapshot_id AS snapshot_id
            """,
            tenant_id=principal.tenant_id,
            revision_id=revision_id,
            groups=sorted(principal.groups),
            require_current=require_current,
            required_statuses=[status.value for status in required_statuses],
            ontology_version_id=ontology_version_id,
            require_active_tbox=require_active_tbox,
        ).single()
        if row is None:
            raise KnowledgeReviewUnavailable(
                "knowledge publication input is unavailable"
            )
        labels = set(row["labels"])
        properties = dict(row["revision"])
        if "GovernedEntityMentionRevision" in labels:
            record = _stored_mention(properties)
        elif "GovernedAssertionRevision" in labels:
            record = _stored_assertion(properties)
        else:
            raise KnowledgePublicationConflict(
                "publication manifest references an invalid revision"
            )
        return record, row["snapshot_id"]

    @classmethod
    def _load_manifest_records_tx(
        cls,
        tx: Any,
        principal: Principal,
        revision_ids: tuple[str, ...],
        *,
        ontology_version_id: str,
    ) -> tuple[EntityMentionRecord | AssertionRecord, ...]:
        return tuple(
            record
            for record, _ in cls._load_manifest_record_entries_tx(
                tx,
                principal,
                revision_ids,
                ontology_version_id=ontology_version_id,
            )
        )

    @classmethod
    def _load_manifest_record_entries_tx(
        cls,
        tx: Any,
        principal: Principal,
        revision_ids: tuple[str, ...],
        *,
        ontology_version_id: str,
    ) -> tuple[
        tuple[EntityMentionRecord | AssertionRecord, str], ...
    ]:
        if not revision_ids or len(revision_ids) > MAX_PUBLICATION_RECORDS:
            raise KnowledgePublicationConflict(
                "publication manifest size is invalid"
            )
        entries = tuple(
            cls._load_revision_tx(
                tx,
                principal,
                revision_id,
                require_current=False,
                required_statuses=(GovernanceStatus.PUBLISHED,),
                ontology_version_id=ontology_version_id,
                require_active_tbox=False,
            )
            for revision_id in revision_ids
        )
        if len({record.revision_id for record, _ in entries}) != len(entries):
            raise KnowledgePublicationConflict(
                "publication manifest contains duplicate revisions"
            )
        return entries

    @staticmethod
    def _link_manifest_tx(
        tx: Any,
        tenant_id: str,
        publication_id: str,
        records: list[EntityMentionRecord | AssertionRecord],
        snapshots: dict[str, str],
        ontology_version_id: str,
    ) -> None:
        rows = [
            {
                "revision_id": record.revision_id,
                "record_kind": (
                    ReviewRecordKind.ENTITY_MENTION.value
                    if isinstance(record, EntityMentionRecord)
                    else ReviewRecordKind.ASSERTION.value
                ),
                "snapshot_id": snapshots[record.revision_id],
            }
            for record in records
        ]
        result = tx.run(
            """
            MATCH (publication:KnowledgePublication {
                tenant_id: $tenant_id,
                publication_id: $publication_id
            })
            MATCH (tbox:TBoxVersion {
                tenant_id: $tenant_id,
                tbox_id: $ontology_version_id,
                status: 'PUBLISHED'
            })
            MATCH (:TBoxCatalog {tenant_id: $tenant_id})
                  -[:ACTIVE_TBOX_VERSION]->(tbox)
            MERGE (publication)-[:USES_TBOX_VERSION]->(tbox)
            WITH publication
            UNWIND $rows AS row
            MATCH (revision {
                tenant_id: $tenant_id,
                revision_id: row.revision_id,
                governance_status: 'PUBLISHED'
            })
            WHERE revision:GovernedEntityMentionRevision
               OR revision:GovernedAssertionRevision
            MATCH (snapshot:KnowledgeSnapshot {
                tenant_id: $tenant_id,
                snapshot_id: row.snapshot_id,
                build_state: 'PUBLISHED'
            })
            CREATE (publication)-[:PUBLISHES_KNOWLEDGE_REVISION {
                record_kind: row.record_kind
            }]->(revision)
            MERGE (publication)-[:USES_KNOWLEDGE_SNAPSHOT]->(snapshot)
            RETURN count(DISTINCT revision) AS count
            """,
            tenant_id=tenant_id,
            publication_id=publication_id,
            ontology_version_id=ontology_version_id,
            rows=rows,
        ).single()
        if result is None or result["count"] != len(records):
            raise KnowledgePublicationConflict(
                "publication manifest could not bind every revision"
            )

    @staticmethod
    def _deactivate_materialization_tx(
        tx: Any,
        tenant_id: str,
        publication_id: str | None,
    ) -> None:
        if publication_id is None:
            return
        for relationship_type in (
            "INCLUDES_MENTION",
            "INCLUDES_ENTITY",
            "INCLUDES_ASSERTION",
        ):
            tx.run(
                f"""
                MATCH (:KnowledgeSnapshot {{tenant_id: $tenant_id}})-[
                    membership:{relationship_type} {{
                        governed_publication_id: $publication_id
                    }}
                ]->({{tenant_id: $tenant_id}})
                DELETE membership
                """,
                tenant_id=tenant_id,
                publication_id=publication_id,
            ).consume()
        for label in ("EntityMention", "Assertion"):
            tx.run(
                f"""
                MATCH (navigation:{label} {{
                    tenant_id: $tenant_id,
                    governed_publication_id: $publication_id
                }})
                WHERE NOT (:KnowledgeSnapshot)-[]->(navigation)
                DETACH DELETE navigation
                """,
                tenant_id=tenant_id,
                publication_id=publication_id,
            ).consume()
        tx.run(
            """
            MATCH (value:RelationshipPropertyValue {tenant_id: $tenant_id})
            WHERE NOT (:Assertion)-[:HAS_RELATIONSHIP_PROPERTY]->(value)
            DETACH DELETE value
            """,
            tenant_id=tenant_id,
        ).consume()

    @classmethod
    def _materialize_records_tx(
        cls,
        tx: Any,
        principal: Principal,
        publication_id: str,
        records: tuple[EntityMentionRecord | AssertionRecord, ...],
    ) -> None:
        mentions = sorted(
            (
                record
                for record in records
                if isinstance(record, EntityMentionRecord)
            ),
            key=lambda record: record.revision_id,
        )
        assertions = sorted(
            (
                record
                for record in records
                if isinstance(record, AssertionRecord)
            ),
            key=lambda record: record.revision_id,
        )
        for mention in mentions:
            cls._materialize_mention_tx(
                tx,
                principal,
                publication_id,
                mention,
            )
        for assertion in assertions:
            cls._materialize_assertion_tx(
                tx,
                principal,
                publication_id,
                assertion,
            )

    @staticmethod
    def _materialize_mention_tx(
        tx: Any,
        principal: Principal,
        publication_id: str,
        mention: EntityMentionRecord,
    ) -> None:
        extractor = (
            mention.trust.extractor_version
            or f"{mention.trust.origin.value}:reviewed"
        )
        mention_id = canonical_mention_id(
            mention.evidence.chunk_id,
            mention.entity.entity_type,
            mention.evidence.char_start,
            mention.evidence.char_end,
            mention.surface,
            extractor,
        )
        properties = {
            "mention_id": mention_id,
            "tenant_id": mention.tenant_id,
            "chunk_id": mention.evidence.chunk_id,
            "entity_id": mention.entity.entity_id,
            "entity_type": mention.entity.entity_type,
            "surface": mention.surface,
            "char_start": mention.evidence.char_start,
            "char_end": mention.evidence.char_end,
            "extractor_version": extractor,
            "confidence": mention.confidence,
            "governance_status": "ACCEPTED_BY_REVIEW",
            "governed_revision_id": mention.revision_id,
            "governed_publication_id": publication_id,
            "authority_level": mention.trust.authority.value,
        }
        row = tx.run(
            """
            MATCH (revision:GovernedEntityMentionRevision {
                tenant_id: $tenant_id,
                revision_id: $revision_id,
                governance_status: 'PUBLISHED'
            })-[:IN_CHUNK]->(chunk:Chunk {
                tenant_id: $tenant_id,
                chunk_id: $chunk_id
            })
            MATCH (revision)-[:REFERS_TO]->(entity:Entity {
                tenant_id: $tenant_id,
                entity_id: $entity_id
            })
            MATCH (document:Document {
                tenant_id: $tenant_id,
                document_id: revision.document_id
            })-[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot {
                tenant_id: $tenant_id,
                build_state: 'PUBLISHED'
            })-[:INCLUDES_CHUNK]->(chunk)
            MATCH (document)-[:ACTIVE_VERSION]->(:DocumentVersion {
                tenant_id: $tenant_id,
                version_id: revision.version_id
            })
            MATCH (snapshot)-[:OF_VERSION]->(:DocumentVersion {
                tenant_id: $tenant_id,
                version_id: revision.version_id
            })
            WHERE any(
                group IN $groups
                WHERE group IN revision.access_groups
            )
              AND any(
                  group IN $groups
                  WHERE group IN document.access_groups
              )
              AND revision.access_policy_id = chunk.access_policy_id
              AND revision.access_policy_version =
                  chunk.access_policy_version
              AND revision.access_groups = chunk.access_groups
              AND substring(
                  chunk.text,
                  revision.evidence_char_start - chunk.char_start,
                  revision.evidence_char_end -
                      revision.evidence_char_start
              ) = revision.evidence_text
            MERGE (navigation:EntityMention {mention_id: $mention_id})
            ON CREATE SET navigation = $properties
            WITH revision, chunk, entity, snapshot, navigation,
                 all(
                     key IN keys($properties)
                     WHERE navigation[key] = $properties[key]
                 ) AS compatible
            MERGE (navigation)-[:IN_CHUNK]->(chunk)
            MERGE (navigation)-[:REFERS_TO]->(entity)
            CREATE (snapshot)-[:INCLUDES_MENTION {
                governed_publication_id: $publication_id,
                confidence: revision.confidence
            }]->(navigation)
            MERGE (snapshot)-[:INCLUDES_ENTITY {
                governed_publication_id: $publication_id,
                entity_id: entity.entity_id
            }]->(entity)
            SET entity.governance_status = coalesce(
                    entity.governance_status,
                    'ACCEPTED_BY_REVIEW'
                )
            RETURN compatible,
                   snapshot.snapshot_id AS snapshot_id
            """,
            tenant_id=principal.tenant_id,
            groups=sorted(principal.groups),
            revision_id=mention.revision_id,
            chunk_id=mention.evidence.chunk_id,
            entity_id=mention.entity.entity_id,
            mention_id=mention_id,
            publication_id=publication_id,
            properties=properties,
        ).single()
        if row is None or not row["compatible"]:
            raise KnowledgePublicationConflict(
                "canonical mention materialization conflicts or is stale"
            )

    @staticmethod
    def _materialize_assertion_tx(
        tx: Any,
        principal: Principal,
        publication_id: str,
        assertion: AssertionRecord,
    ) -> None:
        extractor = (
            assertion.trust.extractor_version
            or f"{assertion.trust.origin.value}:reviewed"
        )
        assertion_id = canonical_assertion_id(
            assertion.tenant_id,
            assertion.subject.entity_id,
            assertion.predicate,
            assertion.object_kind,
            assertion.object_reference,
            assertion.evidence.chunk_id,
            assertion.evidence.char_start,
            assertion.evidence.char_end,
            extractor,
            assertion.trust.ontology_version_id,
        )
        properties = {
            "assertion_id": assertion_id,
            "tenant_id": assertion.tenant_id,
            "subject_entity_id": assertion.subject.entity_id,
            "object_entity_id": (
                None
                if assertion.object_entity is None
                else assertion.object_entity.entity_id
            ),
            "predicate": assertion.predicate,
            "object_kind": assertion.object_kind,
            "literal_value": assertion.literal_value or "",
            "document_id": assertion.evidence.document_id,
            "version_id": assertion.evidence.version_id,
            "evidence_chunk_id": assertion.evidence.chunk_id,
            "evidence_char_start": assertion.evidence.char_start,
            "evidence_char_end": assertion.evidence.char_end,
            "evidence_text": assertion.evidence.quoted_text,
            "access_policy_id": assertion.evidence.access_policy_id,
            "access_policy_version": assertion.evidence.access_policy_version,
            "access_groups": sorted(assertion.evidence.access_groups),
            "extractor_version": extractor,
            "schema_version": assertion.trust.ontology_version_id,
            "confidence": assertion.confidence,
            "accepted": True,
            "governance_status": "ACCEPTED_BY_REVIEW",
            "publication_state": "GOVERNED_PUBLISHED",
            "governed_revision_id": assertion.revision_id,
            "governed_publication_id": publication_id,
            "authority_level": assertion.trust.authority.value,
        }
        if assertion.literal_semantics is not None:
            properties.update(assertion.literal_semantics.to_flat_properties())
        properties.update(
            relationship_properties_format_version=1,
            relationship_properties_json=json.dumps(
                [item.to_mapping() for item in assertion.relationship_properties],
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        row = tx.run(
            """
            MATCH (revision:GovernedAssertionRevision {
                tenant_id: $tenant_id,
                revision_id: $revision_id,
                governance_status: 'PUBLISHED'
            })-[:EVIDENCED_BY]->(chunk:Chunk {
                tenant_id: $tenant_id,
                chunk_id: $chunk_id
            })
            MATCH (revision)-[:SUBJECT]->(subject:Entity {
                tenant_id: $tenant_id,
                entity_id: $subject_entity_id
            })
            OPTIONAL MATCH (revision)-[:OBJECT]->(object:Entity {
                tenant_id: $tenant_id
            })
            MATCH (document:Document {
                tenant_id: $tenant_id,
                document_id: revision.document_id
            })-[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot {
                tenant_id: $tenant_id,
                build_state: 'PUBLISHED'
            })-[:INCLUDES_CHUNK]->(chunk)
            MATCH (document)-[:ACTIVE_VERSION]->(:DocumentVersion {
                tenant_id: $tenant_id,
                version_id: revision.version_id
            })
            MATCH (snapshot)-[:OF_VERSION]->(:DocumentVersion {
                tenant_id: $tenant_id,
                version_id: revision.version_id
            })
            WHERE any(
                group IN $groups
                WHERE group IN revision.access_groups
            )
              AND any(
                  group IN $groups
                  WHERE group IN document.access_groups
              )
              AND revision.access_policy_id = chunk.access_policy_id
              AND revision.access_policy_version =
                  chunk.access_policy_version
              AND revision.access_groups = chunk.access_groups
              AND substring(
                  chunk.text,
                  revision.evidence_char_start - chunk.char_start,
                  revision.evidence_char_end -
                      revision.evidence_char_start
              ) = revision.evidence_text
              AND (
                  revision.object_kind = 'literal'
                  OR object.entity_id = $object_entity_id
              )
            MERGE (navigation:Assertion {assertion_id: $assertion_id})
            ON CREATE SET navigation = $properties
            WITH revision, chunk, subject, object, snapshot, navigation,
                 all(
                     key IN keys($properties)
                     WHERE navigation[key] = $properties[key]
                 ) AS compatible
            MERGE (navigation)-[:SUBJECT]->(subject)
            MERGE (navigation)-[:EVIDENCED_BY]->(chunk)
            FOREACH (_ IN CASE WHEN object IS NULL THEN [] ELSE [1] END |
                MERGE (navigation)-[:OBJECT]->(object)
            )
            CREATE (snapshot)-[:INCLUDES_ASSERTION {
                governed_publication_id: $publication_id,
                confidence: revision.confidence,
                accepted: true
            }]->(navigation)
            RETURN compatible,
                   snapshot.snapshot_id AS snapshot_id
            """,
            tenant_id=principal.tenant_id,
            groups=sorted(principal.groups),
            revision_id=assertion.revision_id,
            chunk_id=assertion.evidence.chunk_id,
            subject_entity_id=assertion.subject.entity_id,
            object_entity_id=(
                None
                if assertion.object_entity is None
                else assertion.object_entity.entity_id
            ),
            assertion_id=assertion_id,
            publication_id=publication_id,
            properties={
                key: value
                for key, value in properties.items()
                if value is not None
            },
        ).single()
        if row is None or not row["compatible"]:
            raise KnowledgePublicationConflict(
                "canonical assertion materialization conflicts or is stale"
            )
        if assertion.relationship_properties:
            property_rows = tuple(
                {
                    "property_value_id": item.property_value_id,
                    "properties": {
                        "property_value_id": item.property_value_id,
                        "tenant_id": assertion.tenant_id,
                        "relationship_type": item.relationship_type,
                        "name": item.name,
                        "evidence_chunk_id": item.evidence_chunk_id,
                        "evidence_char_start": item.evidence_char_start,
                        "evidence_char_end": item.evidence_char_end,
                        "evidence_text": item.evidence_text,
                        "extractor_version": item.extractor_version,
                        "schema_version": item.schema_version,
                        "confidence": item.confidence,
                        "document_id": assertion.evidence.document_id,
                        "version_id": assertion.evidence.version_id,
                        "access_policy_id": assertion.evidence.access_policy_id,
                        "access_policy_version": (
                            assertion.evidence.access_policy_version
                        ),
                        "access_groups": sorted(assertion.evidence.access_groups),
                        **item.literal_semantics.to_flat_properties(),
                    },
                }
                for item in assertion.relationship_properties
            )
            property_result = tx.run(
                """
                MATCH (assertion:Assertion {
                    tenant_id: $tenant_id,
                    assertion_id: $assertion_id,
                    governed_publication_id: $publication_id
                })-[:EVIDENCED_BY]->(chunk:Chunk {
                    tenant_id: $tenant_id,
                    chunk_id: $chunk_id
                })
                UNWIND $rows AS row
                MERGE (value:RelationshipPropertyValue {
                    property_value_id: row.property_value_id
                })
                ON CREATE SET value = row.properties
                WITH assertion, chunk, value, row,
                     all(
                         key IN keys(row.properties)
                         WHERE value[key] = row.properties[key]
                     ) AS compatible
                WHERE compatible
                  AND value.tenant_id = $tenant_id
                  AND value.evidence_chunk_id = chunk.chunk_id
                  AND assertion.evidence_char_start <= value.evidence_char_start
                  AND value.evidence_char_start < value.evidence_char_end
                  AND value.evidence_char_end <= assertion.evidence_char_end
                  AND substring(
                      chunk.text,
                      value.evidence_char_start - chunk.char_start,
                      value.evidence_char_end - value.evidence_char_start
                  ) = value.evidence_text
                  AND value.document_id = assertion.document_id
                  AND value.version_id = assertion.version_id
                  AND value.access_policy_id = assertion.access_policy_id
                  AND value.access_policy_version = assertion.access_policy_version
                  AND value.access_groups = assertion.access_groups
                MERGE (assertion)-[:HAS_RELATIONSHIP_PROPERTY]->(value)
                MERGE (value)-[:EVIDENCED_BY]->(chunk)
                RETURN count(value) AS count
                """,
                tenant_id=assertion.tenant_id,
                assertion_id=assertion_id,
                publication_id=publication_id,
                chunk_id=assertion.evidence.chunk_id,
                rows=property_rows,
            ).single()
            if (
                property_result is None
                or property_result["count"] != len(property_rows)
            ):
                raise KnowledgePublicationConflict(
                    "relationship-property materialization conflicts or is stale"
                )

    @staticmethod
    def _activate_publication_tx(
        tx: Any,
        principal: Principal,
        publication_id: str,
        previous_publication_id: str | None,
        publication_generation: int,
        activation_generation: int,
        now: datetime,
        *,
        action: str,
    ) -> None:
        activation_id = _stable_id(
            "knowledge-publication-activation:v1",
            principal.tenant_id,
            activation_generation,
            publication_id,
            previous_publication_id or "",
            action,
        )
        row = tx.run(
            """
            MATCH (state:KnowledgePublicationState {
                tenant_id: $tenant_id
            })
            MATCH (target:KnowledgePublication {
                tenant_id: $tenant_id,
                publication_id: $publication_id
            })
            OPTIONAL MATCH (state)-[
                old_pointer:ACTIVE_KNOWLEDGE_PUBLICATION
            ]->(old:KnowledgePublication)
            DELETE old_pointer
            WITH DISTINCT state, target, old
            CREATE (state)-[:ACTIVE_KNOWLEDGE_PUBLICATION]->(target)
            CREATE (activation:KnowledgePublicationActivation {
                activation_id: $activation_id,
                tenant_id: $tenant_id,
                activation_generation: $activation_generation,
                action: $action,
                actor_id: $actor_id,
                activated_at: $now,
                from_publication_id: $previous_publication_id,
                to_publication_id: $publication_id
            })
            CREATE (state)-[:HAS_PUBLICATION_ACTIVATION]->(activation)
            CREATE (activation)-[:ACTIVATED_PUBLICATION]->(target)
            FOREACH (_ IN CASE WHEN old IS NULL THEN [] ELSE [1] END |
                CREATE (activation)-[:DEACTIVATED_PUBLICATION]->(old)
            )
            SET state.publication_generation = $publication_generation,
                state.activation_generation = $activation_generation,
                state.updated_at = $now,
                target.status = 'ACTIVE',
                target.activated_at = $now
            REMOVE target.retired_at
            FOREACH (_ IN CASE
                WHEN old IS NOT NULL AND old <> target THEN [1]
                ELSE []
            END |
                SET old.status = 'RETIRED',
                    old.retired_at = $now,
                    old.rolled_back_by = CASE
                        WHEN $action = 'ROLLBACK' THEN $actor_id
                        ELSE old.rolled_back_by
                    END,
                    old.rolled_back_at = CASE
                        WHEN $action = 'ROLLBACK' THEN $now
                        ELSE old.rolled_back_at
                    END
            )
            RETURN target.publication_id AS publication_id
            """,
            tenant_id=principal.tenant_id,
            publication_id=publication_id,
            previous_publication_id=previous_publication_id,
            publication_generation=publication_generation,
            activation_generation=activation_generation,
            activation_id=activation_id,
            action=action,
            actor_id=principal.principal_id,
            now=now,
        ).single()
        if row is None:
            raise KnowledgePublicationConflict(
                "knowledge publication activation failed"
            )
