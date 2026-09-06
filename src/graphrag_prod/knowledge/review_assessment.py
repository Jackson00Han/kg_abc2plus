"""Bounded, read-only dependency and authoritative-fact review guidance.

Recommendations never mutate knowledge. Explicit duplicate dismissal reuses this
same assessment inside the review transaction's tenant corpus lock.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from neo4j import unit_of_work

from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.models import TypedLiteralValue

from .models import AssertionRecord, EntityIdentity, EntityMentionRecord, EvidenceReference
from .review import (
    KNOWLEDGE_REVIEW_CAPABILITY,
    KnowledgeReviewUnavailable,
    Neo4jKnowledgePublicationService,
    ReviewRecordKind,
    _CURRENT_REVIEW_QUERY,
    _positive_integer,
    _require_capability,
    _required_text,
)
from .store import KnowledgeConflict, Neo4jKnowledgeStore, _stored_assertion, _stored_mention
from .trust import GovernanceStatus

MAX_COMPARISON_FACTS = 100
ASSESSMENT_TRANSACTION_TIMEOUT_SECONDS = 25.0


@dataclass(frozen=True, slots=True)
class ReviewDependency:
    role: str
    mention_record_id: str | None
    mention_revision_id: str | None
    name: str
    status: str
    ready: bool
    evidence: EvidenceReference | None = None


@dataclass(frozen=True, slots=True)
class AuthoritativeFactMatch:
    publication_id: str
    record: AssertionRecord


@dataclass(frozen=True, slots=True)
class ReviewAssessment:
    record_id: str
    revision_id: str
    revision: int
    status: str
    reason_code: str
    summary: str
    dependencies: tuple[ReviewDependency, ...]
    matches: tuple[AuthoritativeFactMatch, ...] = ()
    truncated: bool = False


def literal_signature(value: TypedLiteralValue | None) -> tuple[object, ...] | None:
    """Compare normalized meaning, preserving validity and observation scope."""
    if value is None:
        return None
    return (
        value.datatype, value.canonical_value, value.canonical_unit,
        value.valid_from, value.valid_to, value.observed_at,
    )


def fact_signature(record: AssertionRecord) -> tuple[object, ...] | None:
    if record.object_entity is None and record.literal_semantics is None:
        return None
    properties = tuple(sorted(
        ((value.name, literal_signature(value.literal_semantics))
         for value in record.relationship_properties), key=repr,
    ))
    return (
        record.trust.ontology_version_id, record.subject.entity_id,
        record.predicate,
        None if record.object_entity is None else record.object_entity.entity_id,
        literal_signature(record.literal_semantics), properties,
    )


def classify_facts(
    candidate: AssertionRecord,
    matches: tuple[AuthoritativeFactMatch, ...],
) -> tuple[str, str, str, tuple[AuthoritativeFactMatch, ...]]:
    signature = fact_signature(candidate)
    if signature is None:
        return "UNAVAILABLE", "MISSING_TYPED_SEMANTICS", "缺少规范化属性语义，请先检查原始内容。", ()
    comparable = tuple(value for value in matches if (
        value.record.subject.entity_id == candidate.subject.entity_id
        and value.record.predicate == candidate.predicate
        and value.record.trust.ontology_version_id == candidate.trust.ontology_version_id
        and (candidate.object_entity is None or (
            value.record.object_entity is not None
            and value.record.object_entity.entity_id == candidate.object_entity.entity_id
        ))
    ))
    duplicates = tuple(value for value in comparable if fact_signature(value.record) == signature)
    differences = tuple(value for value in comparable if fact_signature(value.record) != signature)
    if differences:
        return "CONFLICT", "AUTHORITATIVE_VALUES_DIFFER", "已有权威事实的值、单位、时间或关系属性不同，请核对来源后处理。", comparable
    if duplicates:
        return "DUPLICATE", "EXACT_AUTHORITATIVE_DUPLICATE", "与已发布权威事实一致，可保留已有事实并记录本次来源。", duplicates
    return "READY", "NO_VISIBLE_AUTHORITATIVE_DUPLICATE", "关联实体已确认，当前可访问的权威事实中未发现重复，请核对原文后批准。", ()


# Explicit planning boundaries keep the source/authority pattern from becoming
# one combinatorial join search on a cold Neo4j query cache (dev-mini: 1 CPU).
# The limit is applied only after every source and ACL predicate.
_AUTHORITY_QUERY = """
MATCH (:KnowledgePublicationState {tenant_id: $tenant_id})
      -[:ACTIVE_KNOWLEDGE_PUBLICATION]->(publication:KnowledgePublication {
          tenant_id: $tenant_id, status: 'ACTIVE'
      })-[:PUBLISHES_KNOWLEDGE_REVISION]->(revision:GovernedAssertionRevision {
          tenant_id: $tenant_id, authority_level: 'AUTHORITATIVE',
          governance_status: 'PUBLISHED', subject_entity_id: $subject_entity_id,
          predicate: $predicate, ontology_version_id: $ontology_version_id
      })
WITH DISTINCT publication, revision
MATCH (:KnowledgeRecordHead {tenant_id: $tenant_id,
    record_kind: 'ASSERTION', record_id: revision.record_id})
MATCH (revision)-[:EVIDENCED_BY]->(chunk:Chunk {tenant_id: $tenant_id})
WITH DISTINCT publication, revision, chunk
MATCH (document:Document {tenant_id: $tenant_id, document_id: revision.document_id})
      -[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot {
          tenant_id: $tenant_id, build_state: 'PUBLISHED'
      })-[:INCLUDES_CHUNK]->(chunk)
MATCH (document)-[:ACTIVE_VERSION]->(version:DocumentVersion {
    tenant_id: $tenant_id, version_id: revision.version_id
})
MATCH (snapshot)-[:OF_VERSION]->(version)
MATCH (publication)-[:USES_KNOWLEDGE_SNAPSHOT]->(snapshot)
WITH DISTINCT publication, revision, chunk, document, version
MATCH (:TBoxCatalog {tenant_id: $tenant_id})-[:ACTIVE_TBOX_VERSION]->(tbox:TBoxVersion {
    tenant_id: $tenant_id, tbox_id: $ontology_version_id, status: 'PUBLISHED'
})
WHERE revision.ontology_version_id = tbox.tbox_id
  AND revision.document_id = document.document_id
  AND revision.version_id = version.version_id
  AND revision.chunk_id = chunk.chunk_id
  AND revision.access_policy_id = chunk.access_policy_id
  AND revision.access_policy_version = chunk.access_policy_version
  AND revision.access_groups = chunk.access_groups
  AND chunk.char_start <= revision.evidence_char_start
  AND revision.evidence_char_start < revision.evidence_char_end
  AND revision.evidence_char_end <= chunk.char_end
  AND substring(chunk.text, revision.evidence_char_start - chunk.char_start,
                revision.evidence_char_end - revision.evidence_char_start) = revision.evidence_text
  AND any(group IN $groups WHERE group IN revision.access_groups)
  AND any(group IN $groups WHERE group IN chunk.access_groups)
  AND any(group IN $groups WHERE group IN document.access_groups)
RETURN DISTINCT revision {.*} AS revision, publication.publication_id AS publication_id
ORDER BY revision.created_at, revision.record_id
LIMIT $limit
"""


class Neo4jReviewAssessmentService:
    def __init__(self, driver: Any, database: str = "neo4j") -> None:
        self.driver = driver
        self.database = database
        self._read_work = unit_of_work(
            timeout=ASSESSMENT_TRANSACTION_TIMEOUT_SECONDS,
            metadata={"component": "review-assessment", "operation": "read"},
        )(self.assess_tx)

    def assess(self, principal: Principal, record_id: str, expected_revision: int) -> ReviewAssessment:
        _require_capability(principal, KNOWLEDGE_REVIEW_CAPABILITY)
        record_id = _required_text(record_id, "record_id")
        _positive_integer(expected_revision, "expected_revision", 2_147_483_647)
        with self.driver.session(database=self.database) as session:
            return session.execute_read(self._read_work, principal, record_id, expected_revision)

    @classmethod
    def assess_tx(cls, tx: Any, principal: Principal, record_id: str, expected_revision: int) -> ReviewAssessment:
        # Authorize before reporting a stale revision, avoiding existence leakage.
        query = _CURRENT_REVIEW_QUERY[ReviewRecordKind.ASSERTION].replace(
            "AND revision.revision = $expected_revision", ""
        )
        row = tx.run(query, tenant_id=principal.tenant_id, groups=sorted(principal.groups),
                     record_id=record_id, expected_revision=expected_revision, limit=1).single()
        if row is None:
            raise KnowledgeReviewUnavailable("review target is unavailable")
        record = _stored_assertion(dict(row["revision"]))
        if record.revision.revision != expected_revision:
            raise KnowledgeConflict("stale review assessment revision")
        cls._validate_evidence(tx, record)
        dependencies = [cls._dependency(tx, principal, record, "subject", record.subject,
                                        record.subject_mention_revision_id)]
        if record.object_entity is not None:
            dependencies.append(cls._dependency(tx, principal, record, "object", record.object_entity,
                                                record.object_mention_revision_id or ""))
        common = dict(record_id=record.record_id, revision_id=record.revision_id,
                      revision=record.revision.revision, dependencies=tuple(dependencies))
        if record.trust.status not in {GovernanceStatus.CANDIDATE, GovernanceStatus.QUARANTINED}:
            return ReviewAssessment(**common, status="UNAVAILABLE", reason_code="RECORD_ALREADY_REVIEWED",
                                    summary="这条记录已完成审核，请刷新队列查看最新状态。")
        if any(not item.ready for item in dependencies):
            return ReviewAssessment(**common, status="BLOCKED", reason_code="ENDPOINTS_REQUIRE_REVIEW",
                                    summary="请先确认下列关联实体，再审核这条事实。")
        rows = tuple(tx.run(_AUTHORITY_QUERY, tenant_id=principal.tenant_id,
                            groups=sorted(principal.groups), statuses=["PUBLISHED"],
                            subject_entity_id=record.subject.entity_id, predicate=record.predicate,
                            ontology_version_id=record.trust.ontology_version_id,
                            limit=MAX_COMPARISON_FACTS + 1))
        if len(rows) > MAX_COMPARISON_FACTS:
            return ReviewAssessment(**common, status="UNAVAILABLE", reason_code="COMPARISON_LIMIT_EXCEEDED",
                                    summary="相关权威事实超过本次比较上限，请保留候选并缩小处理范围。", truncated=True)
        matches = []
        for row in rows:
            authoritative = _stored_assertion(dict(row["revision"]))
            cls._validate_evidence(tx, authoritative)
            # Reject stale/unavailable authoritative endpoints rather than treating
            # a partial comparison as proof of novelty or equivalence.
            cls._published_endpoints(tx, principal, authoritative, row["publication_id"])
            matches.append(AuthoritativeFactMatch(row["publication_id"], authoritative))
        status, reason, summary, selected = classify_facts(record, tuple(matches))
        return ReviewAssessment(**common, status=status, reason_code=reason, summary=summary, matches=selected)

    @staticmethod
    def _validate_evidence(tx: Any, record: AssertionRecord) -> None:
        Neo4jKnowledgeStore._validate_evidence_tx(tx, record.evidence)
        for value in record.relationship_properties:
            Neo4jKnowledgeStore._validate_evidence_tx(tx, replace(
                record.evidence, char_start=value.evidence_char_start,
                char_end=value.evidence_char_end, quoted_text=value.evidence_text,
                chunk_id=value.evidence_chunk_id,
            ))

    @staticmethod
    def _dependency(tx: Any, principal: Principal, record: AssertionRecord, role: str,
                    entity: EntityIdentity, base_id: str) -> ReviewDependency:
        head = tx.run("""
            MATCH (base:GovernedEntityMentionRevision {tenant_id: $tenant_id, revision_id: $revision_id})
            MATCH (head:KnowledgeRecordHead {tenant_id: $tenant_id, record_id: base.record_id,
                record_kind: 'ENTITY_MENTION'})-[:CURRENT_REVISION]->(current:GovernedEntityMentionRevision)
            RETURN head.record_id AS record_id, current.revision AS revision
        """, tenant_id=principal.tenant_id, revision_id=base_id).single()
        unavailable = ReviewDependency(role, None, None, entity.canonical_name, "UNAVAILABLE", False)
        if head is None:
            return unavailable
        row = tx.run(_CURRENT_REVIEW_QUERY[ReviewRecordKind.ENTITY_MENTION],
                     tenant_id=principal.tenant_id, groups=sorted(principal.groups),
                     record_id=head["record_id"], expected_revision=head["revision"], limit=1).single()
        if row is None:
            return unavailable
        mention = _stored_mention(dict(row["revision"]))
        Neo4jKnowledgeStore._validate_evidence_tx(tx, mention.evidence)
        ready = (mention.trust.status in {GovernanceStatus.APPROVED, GovernanceStatus.PUBLISHED}
                 and mention.evidence.chunk_id == record.evidence.chunk_id
                 and mention.trust.ontology_version_id == record.trust.ontology_version_id
                 and mention.entity.entity_id == entity.entity_id)
        return ReviewDependency(role, mention.record_id, mention.revision_id,
                                mention.entity.canonical_name, mention.trust.status.value, ready, mention.evidence)

    @staticmethod
    def _published_endpoints(tx: Any, principal: Principal, record: AssertionRecord, publication_id: str) -> None:
        endpoints = [(record.subject_mention_revision_id, record.subject.entity_id)]
        if record.object_entity is not None:
            endpoints.append((record.object_mention_revision_id, record.object_entity.entity_id))
        for revision_id, entity_id in endpoints:
            mention, _ = Neo4jKnowledgePublicationService._load_revision_tx(
                tx, principal, revision_id or "", require_current=False,
                required_statuses=(GovernanceStatus.PUBLISHED,),
                ontology_version_id=record.trust.ontology_version_id, require_active_tbox=True,
            )
            if (not isinstance(mention, EntityMentionRecord)
                    or mention.entity.entity_id != entity_id
                    or mention.evidence.chunk_id != record.evidence.chunk_id):
                raise KnowledgeReviewUnavailable("authoritative comparison endpoint is unavailable")
            Neo4jKnowledgeStore._validate_evidence_tx(tx, mention.evidence)
            current_id = mention.revision_id
            row = tx.run("""
                MATCH (publication:KnowledgePublication {tenant_id: $tenant_id,
                    publication_id: $publication_id, status: 'ACTIVE'})
                    -[:PUBLISHES_KNOWLEDGE_REVISION]->(mention:GovernedEntityMentionRevision {
                        tenant_id: $tenant_id, revision_id: $revision_id, governance_status: 'PUBLISHED'
                    })
                RETURN mention.revision_id AS revision_id
            """, tenant_id=principal.tenant_id, publication_id=publication_id, revision_id=current_id).single()
            if row is None:
                raise KnowledgeReviewUnavailable("authoritative comparison endpoint is unavailable")
