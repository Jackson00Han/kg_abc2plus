"""Reconstruct mapped Markdown candidates for an explicitly authorized review.

This pure verifier does not approve records, create identities, publish knowledge,
call a model or read a store. Missing external definitions remain in the proof.
"""
from __future__ import annotations

from datetime import UTC, datetime

from graphrag_prod.construction.markdown_executor import MarkdownMappingExtractor
from graphrag_prod.construction.mapping_preflight import bind_preview_evidence
from .mapped_review_evidence import verify_mapped_record_batches

VERSION = "markdown-mapping-review-proof:v1"


def verify_markdown_mapping_records(*, extractor, chunks, profile, mentions, assertions,
                                    resolved_identities=None):
    """Require the complete immutable source and exact current mapped records.

    The caller supplies authenticated current records and the persisted mapping;
    the returned CAS manifest must still pass the existing review transaction.
    """
    from graphrag_prod.construction.workflow import _to_abox_batch

    if not isinstance(extractor, MarkdownMappingExtractor):
        raise TypeError("Markdown proof requires its deterministic mapping extractor")
    chunks = tuple(chunks)
    # Recheck gapless source, checksums, tenant/version and ACL consistency before
    # any per-chunk reconstruction; a selected subsection is not the full proof.
    bind_preview_evidence(extractor.executor.report, chunks)
    batches = []
    for chunk in chunks:
        audited = extractor.extract_audited(artifact_id="proof-only", input_hash="proof-only",
                                            chunk=chunk, profile=profile)
        batch = _to_abox_batch(audited, chunk=chunk, extracted_at=datetime.now(UTC))
        if batch is not None:
            batches.append(batch)
    summary = extractor.summary()
    return verify_mapped_record_batches(
        expected_batches=batches, live_mentions=mentions, live_assertions=assertions,
        tbox=extractor.active_tbox, semantic_context=extractor.semantic_context,
        resolved_identities=resolved_identities,
        proof_metadata={"adapter": VERSION,
                        "source_checksum": extractor.parsed.normalized_checksum,
                        "mapping_checksum": extractor.plan_checksum,
                        "unresolved_relationship_count": summary["unresolved_relationship_count"],
                        "unresolved_references": summary["unresolved_references"]})
