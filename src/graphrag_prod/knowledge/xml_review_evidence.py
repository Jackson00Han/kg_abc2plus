"""Pure, exhaustive proof for explicitly authorized XML candidate review.

This module does not approve, resolve, publish, call a model or access a store.
It reconstructs the expected source-mapped output, compares every input record,
and emits an auditable manifest for the existing authenticated review API.
"""
from __future__ import annotations

from datetime import UTC, datetime

from graphrag_prod.construction.xml_executor import XmlMappingExtractor
from .models import AssertionRecord, EntityMentionRecord

VERSION = "xml-mapping-review-proof:v1"


def verify_xml_mapping_records(*, extractor, chunk, profile, mentions, assertions,
                               authoritative=False, resolved_identities=None):
    """Compare all source-mapped records before an explicitly requested review.

    ``resolved_identities`` optionally pins prior user-authorized identity
    resolutions by original entity ID. It never authorizes name-based merging;
    values must equal the source-scoped targets returned by this proof.
    Inputs must be the complete authorized current records for this build.
    """
    from graphrag_prod.construction.workflow import _to_abox_batch

    if not isinstance(extractor, XmlMappingExtractor):
        raise TypeError("XML review proof requires its prepared deterministic extractor")
    if type(authoritative) is not bool:
        raise TypeError("authority is the authenticated source declaration, not model input")
    mentions, assertions = tuple(mentions), tuple(assertions)
    if any(not isinstance(m, EntityMentionRecord) for m in mentions) or any(
        not isinstance(a, AssertionRecord) for a in assertions
    ):
        raise TypeError("XML review proof requires decoded governed records")
    audited = extractor.extract_audited(artifact_id="proof-only", input_hash="proof-only",
                                        chunk=chunk, profile=profile)
    expected = _to_abox_batch(audited, chunk=chunk, extracted_at=datetime.now(UTC),
                              authoritative=authoritative)
    if expected is None:
        raise ValueError("XML mapped document contains no reviewable source objects")
    from .mapped_review_evidence import verify_mapped_record_batches
    summary = extractor.summary()
    return verify_mapped_record_batches(
        expected_batches=(expected,), live_mentions=mentions, live_assertions=assertions,
        tbox=extractor.active_tbox, semantic_context=summary["semantic_context"],
        resolved_identities=resolved_identities,
        proof_metadata={"adapter": VERSION, "source_checksum": chunk.checksum,
                        "mapping_checksum": extractor.plan_checksum,
                        "deferred_property_count": summary["deferred_property_count"],
                        "unresolved_references": summary["unresolved_references"]})
