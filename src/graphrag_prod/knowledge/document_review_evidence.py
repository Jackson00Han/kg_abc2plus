"""Exact source context for reviewing ordinary document extraction candidates.

A local extraction identity is a proposal, never a structural mapping proof.
Each document assertion is reviewed individually; sample-based mapping approval
is reserved for the existing, fully checked declarative JSON mapping path.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class EvidenceWindow:
    start: int
    end: int


def install_document_candidates(proof, mentions, facts, source_identity_type):
    """Attach bounded source windows only when there is no declarative mapping."""
    if proof.summary:
        return
    proof.document_candidates = True
    proof.summary = {"mapping_checksum": "document-candidate-evidence:v1"}
    grouped = {}
    for record in mentions:
        evidence = record.evidence
        if (evidence.document_id != proof.document_id or
            proof.text[evidence.char_start:evidence.char_end] != evidence.quoted_text):
            continue
        grouped.setdefault(record.entity.entity_id, []).append(record)
    for entity_id, records in grouped.items():
        lower = min(r.evidence.char_start for r in records)
        upper = max(r.evidence.char_end for r in records)
        # Include the original defining clause and its enclosing syntax. This is
        # model context; assertion truth still depends on its own exact evidence.
        start, end = max(0, lower-384), min(len(proof.text), upper+384)
        if end-start > 8192:
            continue
        source = source_identity_type(
            "document-candidate", entity_id, records[0].entity.entity_type,
            EvidenceWindow(start, end), {"id_field": "candidate_reference", "structural_identity": False},
        )
        for record in records:
            proof.by_record[record.record_id] = source


def document_fact_mapping(proof, fact, mentions_by_revision):
    evidence = fact.evidence
    if (evidence.document_id != proof.document_id or
        proof.text[evidence.char_start:evidence.char_end] != evidence.quoted_text or
        fact.context_property_evidence is not None):
        return None
    endpoints = [(fact.subject_mention_revision_id, fact.subject)]
    if fact.object_entity:
        endpoints.append((fact.object_mention_revision_id, fact.object_entity))
    for revision_id, entity in endpoints:
        mention = mentions_by_revision.get(revision_id)
        if (mention is None or mention.entity.entity_id != entity.entity_id or
            mention.evidence.document_id != evidence.document_id or
            mention.evidence.version_id != evidence.version_id):
            return None
        e = mention.evidence
        if (e.chunk_id != evidence.chunk_id or not
            evidence.char_start <= e.char_start < e.char_end <= evidence.char_end or
            proof.text[e.char_start:e.char_end] != e.quoted_text):
            return None
    return {"subject_type": fact.subject.entity_type, "predicate": fact.predicate,
            "object_type": fact.object_entity.entity_type if fact.object_entity else None,
            "kind": "DOCUMENT_ASSERTION", "record_id": fact.record_id}
