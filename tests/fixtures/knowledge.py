"""Build governed A-Box records aligned with the provenance test fixture."""

from __future__ import annotations

from datetime import UTC, datetime

from graphrag_prod.knowledge import (
    ABoxRecordBatch,
    AssertionRecord,
    EntityIdentity,
    EntityMentionRecord,
    EvidenceReference,
    RecordRevision,
    authoritative_import_trust,
    knowledge_record_id,
    llm_candidate_trust,
)

from .domain import make_bundle


KNOWLEDGE_TIME = datetime(2025, 2, 3, 4, 5, tzinfo=UTC)


def make_knowledge_batch(
    *,
    authoritative: bool = True,
    tenant_id: str = "tenant-knowledge",
    ontology_version_id: str = "tbox-company-v1",
) -> ABoxRecordBatch:
    bundle = make_bundle(tenant_id=tenant_id)
    trust = (
        authoritative_import_trust(
            ontology_version_id=ontology_version_id,
            imported_by="expert:alice",
            imported_at=KNOWLEDGE_TIME,
        )
        if authoritative
        else llm_candidate_trust(
            ontology_version_id=ontology_version_id,
            extractor_version="dashscope-extractor:v1",
            prompt_version="company-tbox:v1",
            extracted_at=KNOWLEDGE_TIME,
        )
    )
    entities = tuple(
        EntityIdentity(
            entity_id=entity.entity_id,
            tenant_id=entity.tenant_id,
            entity_type=entity.entity_type,
            canonical_key=entity.canonical_key,
            canonical_name=entity.canonical_name,
            aliases=entity.aliases,
        )
        for entity in bundle.entities
    )
    entity_by_id = {entity.entity_id: entity for entity in entities}
    mentions = tuple(
        EntityMentionRecord(
            revision=RecordRevision.next(
                knowledge_record_id(
                    tenant_id,
                    "ENTITY_MENTION",
                    mention.mention_id,
                ),
                0,
            ),
            tenant_id=tenant_id,
            entity=entity_by_id[mention.entity_id],
            evidence=EvidenceReference(
                tenant_id=tenant_id,
                document_id=bundle.document.document_id,
                version_id=bundle.version.version_id,
                chunk_id=bundle.chunk.chunk_id,
                char_start=mention.char_start,
                char_end=mention.char_end,
                quoted_text=mention.surface,
                access_policy_id=bundle.chunk.access_policy_id,
                access_policy_version=bundle.chunk.access_policy_version,
                access_groups=bundle.chunk.access_groups,
            ),
            confidence=1.0 if authoritative else mention.confidence,
            trust=trust,
            created_at=KNOWLEDGE_TIME,
        )
        for mention in bundle.mentions
    )
    mention_by_entity = {mention.entity.entity_id: mention for mention in mentions}
    assertion_source = bundle.assertion
    assert assertion_source is not None
    assertion = AssertionRecord(
        revision=RecordRevision.next(
            knowledge_record_id(
                tenant_id,
                "ASSERTION",
                assertion_source.assertion_id,
            ),
            0,
        ),
        tenant_id=tenant_id,
        subject=entity_by_id[assertion_source.subject_entity_id],
        predicate=assertion_source.predicate,
        evidence=EvidenceReference(
            tenant_id=tenant_id,
            document_id=bundle.document.document_id,
            version_id=bundle.version.version_id,
            chunk_id=bundle.chunk.chunk_id,
            char_start=assertion_source.evidence_char_start,
            char_end=assertion_source.evidence_char_end,
            quoted_text=bundle.version.normalized_text[
                assertion_source.evidence_char_start : assertion_source.evidence_char_end
            ],
            access_policy_id=bundle.chunk.access_policy_id,
            access_policy_version=bundle.chunk.access_policy_version,
            access_groups=bundle.chunk.access_groups,
        ),
        subject_mention_revision_id=mention_by_entity[
            assertion_source.subject_entity_id
        ].revision_id,
        object_entity=entity_by_id[assertion_source.object_entity_id or ""],
        object_mention_revision_id=mention_by_entity[
            assertion_source.object_entity_id or ""
        ].revision_id,
        confidence=1.0 if authoritative else assertion_source.confidence,
        trust=trust,
        created_at=KNOWLEDGE_TIME,
    )
    return ABoxRecordBatch(
        tenant_id=tenant_id,
        mentions=mentions,
        assertions=(assertion,),
    )
