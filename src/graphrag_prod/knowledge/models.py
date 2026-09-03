"""Immutable records for governed property-graph A-Box knowledge.

Canonical ``Entity`` nodes deliberately carry identity only.  Authority and
governance belong to the source-backed mention/assertion revisions, because a
single canonical entity can be supported by both expert and model-derived
evidence.  Dynamic facts are represented as assertions rather than being
copied into an unverifiable entity property bag.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from uuid import uuid5

from graphrag_prod.domain.ids import ID_NAMESPACE, entity_id as make_entity_id
from graphrag_prod.domain.models import TypedLiteralValue

from .trust import (
    AuthorityLevel,
    GovernanceStatus,
    KnowledgeOrigin,
    TrustMetadata,
)


_RECORD_ID_SCHEME = "governed-knowledge-record:v1"
_REVISION_ID_SCHEME = "governed-knowledge-revision:v1"


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


def _exact_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value:
        raise ValueError(f"{name} must not be empty")
    return value


def _aware(value: object, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nonnegative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("confidence must be a number")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise ValueError("confidence must be between zero and one")
    return result


def _contains_exact_token(evidence: str, token: str) -> bool:
    start = 0
    while True:
        index = evidence.find(token, start)
        if index < 0:
            return False
        end = index + len(token)
        left_ok = (
            not token[0].isalnum()
            or index == 0
            or not (evidence[index - 1].isalnum() or evidence[index - 1] == "_")
        )
        right_ok = (
            not token[-1].isalnum()
            or end == len(evidence)
            or not (evidence[end].isalnum() or evidence[end] == "_")
        )
        if left_ok and right_ok:
            return True
        start = index + 1


def _groups(values: object) -> frozenset[str]:
    if not isinstance(values, frozenset):
        raise TypeError("access_groups must be a frozenset")
    normalized = frozenset(
        _required_text(value, "access group") for value in values
    )
    if not normalized:
        raise ValueError(
            "access_groups must not be empty (use an explicit public group)"
        )
    return normalized


def _stable_knowledge_id(kind: str, *parts: object) -> str:
    payload = json.dumps(
        [kind, *parts],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return str(uuid5(ID_NAMESPACE, payload))


def knowledge_record_id(tenant_id: str, record_kind: str, source_key: str) -> str:
    """Return a stable logical record ID supplied by an import/extraction job."""

    normalized_kind = _required_text(record_kind, "record_kind")
    if normalized_kind not in {"ENTITY_MENTION", "ASSERTION"}:
        raise ValueError("record_kind must be ENTITY_MENTION or ASSERTION")
    return _stable_knowledge_id(
        _RECORD_ID_SCHEME,
        _required_text(tenant_id, "tenant_id"),
        normalized_kind,
        _required_text(source_key, "source_key"),
    )


def knowledge_revision_id(record_id: str, revision: int) -> str:
    """Return the immutable ID for one append-only logical record revision."""

    return _stable_knowledge_id(
        _REVISION_ID_SCHEME,
        _required_text(record_id, "record_id"),
        _positive_integer(revision, "revision"),
    )


@dataclass(frozen=True, slots=True)
class RecordRevision:
    """Append-only revision identity plus its compare-and-swap precondition."""

    record_id: str
    revision_id: str
    revision: int
    expected_previous_revision: int

    def __post_init__(self) -> None:
        record_id = _required_text(self.record_id, "record_id")
        revision = _positive_integer(self.revision, "revision")
        previous = _nonnegative_integer(
            self.expected_previous_revision,
            "expected_previous_revision",
        )
        if revision != previous + 1:
            raise ValueError("revision must be expected_previous_revision + 1")
        expected_id = knowledge_revision_id(record_id, revision)
        if self.revision_id != expected_id:
            raise ValueError("revision_id does not match record_id and revision")
        object.__setattr__(self, "record_id", record_id)

    @classmethod
    def next(cls, record_id: str, expected_previous_revision: int) -> RecordRevision:
        previous = _nonnegative_integer(
            expected_previous_revision,
            "expected_previous_revision",
        )
        revision = previous + 1
        return cls(
            record_id=record_id,
            revision_id=knowledge_revision_id(record_id, revision),
            revision=revision,
            expected_previous_revision=previous,
        )


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    """Exact, ACL-bound evidence range in an immutable document Chunk."""

    tenant_id: str
    document_id: str
    version_id: str
    chunk_id: str
    char_start: int
    char_end: int
    quoted_text: str
    access_policy_id: str
    access_policy_version: int
    access_groups: frozenset[str]

    def __post_init__(self) -> None:
        for name in (
            "tenant_id",
            "document_id",
            "version_id",
            "chunk_id",
            "access_policy_id",
        ):
            object.__setattr__(
                self,
                name,
                _required_text(getattr(self, name), name),
            )
        _nonnegative_integer(self.char_start, "char_start")
        if (
            isinstance(self.char_end, bool)
            or not isinstance(self.char_end, int)
            or self.char_end <= self.char_start
        ):
            raise ValueError("evidence character range is invalid")
        quoted_text = _exact_text(self.quoted_text, "quoted_text")
        if len(quoted_text) != self.char_end - self.char_start:
            raise ValueError("quoted_text length must match its evidence range")
        object.__setattr__(self, "quoted_text", quoted_text)
        _positive_integer(self.access_policy_version, "access_policy_version")
        object.__setattr__(self, "access_groups", _groups(self.access_groups))


@dataclass(frozen=True, slots=True)
class EntityIdentity:
    """A tenant-scoped canonical identity, without dynamic fact properties."""

    entity_id: str
    tenant_id: str
    entity_type: str
    canonical_key: str
    canonical_name: str
    aliases: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for name in (
            "entity_id",
            "tenant_id",
            "entity_type",
            "canonical_key",
            "canonical_name",
        ):
            object.__setattr__(
                self,
                name,
                _required_text(getattr(self, name), name),
            )
        if (
            make_entity_id(self.tenant_id, self.entity_type, self.canonical_key)
            != self.entity_id
        ):
            raise ValueError("entity_id does not match its identity inputs")
        aliases = tuple(
            sorted({_required_text(alias, "alias") for alias in self.aliases})
        )
        object.__setattr__(self, "aliases", aliases)


@dataclass(frozen=True, slots=True)
class EntityMentionRecord:
    """One immutable revision of a source-backed entity mention."""

    revision: RecordRevision
    tenant_id: str
    entity: EntityIdentity
    evidence: EvidenceReference
    confidence: float
    trust: TrustMetadata
    created_at: datetime

    def __post_init__(self) -> None:
        tenant_id = _required_text(self.tenant_id, "tenant_id")
        object.__setattr__(self, "tenant_id", tenant_id)
        if self.entity.tenant_id != tenant_id or self.evidence.tenant_id != tenant_id:
            raise ValueError("mention identity and evidence must share one tenant")
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        created_at = _aware(self.created_at, "created_at")
        if self.trust.created_at != created_at:
            raise ValueError("record created_at must equal trust created_at")

    @property
    def record_id(self) -> str:
        return self.revision.record_id

    @property
    def revision_id(self) -> str:
        return self.revision.revision_id

    @property
    def surface(self) -> str:
        return self.evidence.quoted_text


@dataclass(frozen=True, slots=True)
class AssertionRecord:
    """One immutable, evidence-backed entity relation or literal fact revision."""

    revision: RecordRevision
    tenant_id: str
    subject: EntityIdentity
    predicate: str
    evidence: EvidenceReference
    subject_mention_revision_id: str
    confidence: float
    trust: TrustMetadata
    created_at: datetime
    object_entity: EntityIdentity | None = None
    object_mention_revision_id: str | None = None
    literal_value: str | None = None
    literal_semantics: TypedLiteralValue | None = None

    def __post_init__(self) -> None:
        tenant_id = _required_text(self.tenant_id, "tenant_id")
        object.__setattr__(self, "tenant_id", tenant_id)
        object.__setattr__(self, "predicate", _required_text(self.predicate, "predicate"))
        object.__setattr__(
            self,
            "subject_mention_revision_id",
            _required_text(
                self.subject_mention_revision_id,
                "subject_mention_revision_id",
            ),
        )
        if self.subject.tenant_id != tenant_id or self.evidence.tenant_id != tenant_id:
            raise ValueError("assertion identity and evidence must share one tenant")

        has_entity = self.object_entity is not None
        has_literal = self.literal_value is not None
        if has_entity == has_literal:
            raise ValueError("assertion requires exactly one entity or literal object")
        if has_entity:
            assert self.object_entity is not None
            if self.literal_semantics is not None:
                raise ValueError("entity assertion must not carry literal semantics")
            if self.object_entity.tenant_id != tenant_id:
                raise ValueError("assertion object must share the record tenant")
            object.__setattr__(
                self,
                "object_mention_revision_id",
                _required_text(
                    self.object_mention_revision_id,
                    "object_mention_revision_id",
                ),
            )
        elif self.object_mention_revision_id is not None:
            raise ValueError("literal assertion must not reference an object mention")
        else:
            literal = _required_text(self.literal_value, "literal_value")
            if not _contains_exact_token(self.evidence.quoted_text, literal):
                raise ValueError("literal_value must occur in the exact evidence text")
            object.__setattr__(self, "literal_value", literal)
            if self.literal_semantics is not None:
                if not isinstance(self.literal_semantics, TypedLiteralValue):
                    raise TypeError("literal_semantics must be TypedLiteralValue")
                if self.literal_semantics.raw_value != literal:
                    raise ValueError("literal_value must equal typed raw_value")
                tokens = (
                    self.literal_semantics.raw_unit,
                    self.literal_semantics.raw_valid_from,
                    self.literal_semantics.raw_valid_to,
                    self.literal_semantics.raw_observed_at,
                )
                if any(
                    token is not None
                    and not _contains_exact_token(self.evidence.quoted_text, token)
                    for token in tokens
                ):
                    raise ValueError(
                        "typed literal source tokens must occur in exact evidence text"
                    )

        object.__setattr__(self, "confidence", _confidence(self.confidence))
        created_at = _aware(self.created_at, "created_at")
        if self.trust.created_at != created_at:
            raise ValueError("record created_at must equal trust created_at")

    @property
    def record_id(self) -> str:
        return self.revision.record_id

    @property
    def revision_id(self) -> str:
        return self.revision.revision_id

    @property
    def object_kind(self) -> str:
        return "entity" if self.object_entity is not None else "literal"

    @property
    def object_reference(self) -> str:
        if self.object_entity is not None:
            return self.object_entity.entity_id
        if self.literal_semantics is not None:
            return self.literal_semantics.identity_reference
        return self.literal_value or ""


@dataclass(frozen=True, slots=True)
class ABoxRecordBatch:
    """A bounded set of mentions and assertions from one tenant/T-Box."""

    tenant_id: str
    mentions: tuple[EntityMentionRecord, ...]
    assertions: tuple[AssertionRecord, ...] = ()

    def __post_init__(self) -> None:
        tenant_id = _required_text(self.tenant_id, "tenant_id")
        object.__setattr__(self, "tenant_id", tenant_id)
        if not self.mentions:
            raise ValueError("A-Box batch requires at least one entity mention")
        all_records = (*self.mentions, *self.assertions)
        if any(record.tenant_id != tenant_id for record in all_records):
            raise ValueError("all A-Box records must share the batch tenant")
        record_ids = [record.record_id for record in all_records]
        revision_ids = [record.revision_id for record in all_records]
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("A-Box batch contains duplicate logical record IDs")
        if len(revision_ids) != len(set(revision_ids)):
            raise ValueError("A-Box batch contains duplicate revision IDs")
        ontology_versions = {record.trust.ontology_version_id for record in all_records}
        if len(ontology_versions) != 1:
            raise ValueError("one A-Box batch must use exactly one T-Box version")

        mentions_by_revision = {
            mention.revision_id: mention for mention in self.mentions
        }
        for assertion in self.assertions:
            subject_mention = mentions_by_revision.get(
                assertion.subject_mention_revision_id
            )
            if subject_mention is None or subject_mention.entity.entity_id != assertion.subject.entity_id:
                raise ValueError("assertion subject requires its matching mention revision")
            endpoint_mentions = [subject_mention]
            if assertion.object_entity is not None:
                object_mention = mentions_by_revision.get(
                    assertion.object_mention_revision_id or ""
                )
                if (
                    object_mention is None
                    or object_mention.entity.entity_id
                    != assertion.object_entity.entity_id
                ):
                    raise ValueError(
                        "assertion object requires its matching mention revision"
                    )
                endpoint_mentions.append(object_mention)
            for mention in endpoint_mentions:
                if mention.evidence.chunk_id != assertion.evidence.chunk_id:
                    raise ValueError("assertion endpoints must occur in its evidence Chunk")
                if not (
                    assertion.evidence.char_start
                    <= mention.evidence.char_start
                    < mention.evidence.char_end
                    <= assertion.evidence.char_end
                ):
                    raise ValueError(
                        "assertion endpoint mention falls outside its evidence range"
                    )

    @property
    def ontology_version_id(self) -> str:
        return self.mentions[0].trust.ontology_version_id

    def require_authoritative_import(self) -> None:
        """Fail unless every record is expert-imported and already published."""

        for record in (*self.mentions, *self.assertions):
            trust = record.trust
            if (
                trust.origin is not KnowledgeOrigin.EXPERT_IMPORT
                or trust.authority is not AuthorityLevel.AUTHORITATIVE
                or trust.status is not GovernanceStatus.PUBLISHED
            ):
                raise ValueError(
                    "authoritative A-Box imports require "
                    "EXPERT_IMPORT + AUTHORITATIVE + PUBLISHED records"
                )

    def require_llm_candidates(self) -> None:
        """Fail unless every record is a fully identified LLM candidate."""

        for record in (*self.mentions, *self.assertions):
            trust = record.trust
            if (
                trust.origin is not KnowledgeOrigin.LLM_EXTRACTED
                or trust.authority is not AuthorityLevel.SECONDARY
                or trust.status is not GovernanceStatus.CANDIDATE
                or trust.extractor_version is None
                or trust.prompt_version is None
            ):
                raise ValueError(
                    "LLM persistence requires identified "
                    "LLM_EXTRACTED + SECONDARY + CANDIDATE records"
                )

    def require_llm_quarantined(self) -> None:
        """Fail unless every record belongs to the explicit LLM quarantine lane."""

        for record in (*self.mentions, *self.assertions):
            trust = record.trust
            if (
                trust.origin is not KnowledgeOrigin.LLM_EXTRACTED
                or trust.authority is not AuthorityLevel.SECONDARY
                or trust.status is not GovernanceStatus.QUARANTINED
                or trust.extractor_version is None
                or trust.prompt_version is None
            ):
                raise ValueError(
                    "LLM quarantine persistence requires identified "
                    "LLM_EXTRACTED + SECONDARY + QUARANTINED records"
                )


def authoritative_import_trust(
    *,
    ontology_version_id: str,
    imported_by: str,
    imported_at: datetime,
    review_notes: str | None = None,
) -> TrustMetadata:
    """Build the mandatory trust metadata for an authoritative A-Box import."""

    return TrustMetadata(
        origin=KnowledgeOrigin.EXPERT_IMPORT,
        authority=AuthorityLevel.AUTHORITATIVE,
        status=GovernanceStatus.PUBLISHED,
        ontology_version_id=ontology_version_id,
        created_at=imported_at,
        reviewed_by=imported_by,
        reviewed_at=imported_at,
        review_notes=review_notes,
    )


def llm_candidate_trust(
    *,
    ontology_version_id: str,
    extractor_version: str,
    prompt_version: str,
    extracted_at: datetime,
) -> TrustMetadata:
    """Build the mandatory trust metadata for an unreviewed LLM extraction."""

    return TrustMetadata(
        origin=KnowledgeOrigin.LLM_EXTRACTED,
        authority=AuthorityLevel.SECONDARY,
        status=GovernanceStatus.CANDIDATE,
        ontology_version_id=ontology_version_id,
        created_at=extracted_at,
        extractor_version=extractor_version,
        prompt_version=prompt_version,
    )


def llm_quarantined_trust(
    *,
    ontology_version_id: str,
    extractor_version: str,
    prompt_version: str,
    extracted_at: datetime,
) -> TrustMetadata:
    """Build trust metadata for valid but below-threshold model output."""

    return TrustMetadata(
        origin=KnowledgeOrigin.LLM_EXTRACTED,
        authority=AuthorityLevel.SECONDARY,
        status=GovernanceStatus.QUARANTINED,
        ontology_version_id=ontology_version_id,
        created_at=extracted_at,
        extractor_version=extractor_version,
        prompt_version=prompt_version,
    )
