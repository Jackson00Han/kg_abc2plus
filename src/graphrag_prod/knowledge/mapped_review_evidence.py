"""Pure exhaustive checks for reviewed source mappings, without knowledge writes."""
from __future__ import annotations
from dataclasses import asdict, replace
import hashlib
import json
from graphrag_prod.domain.ids import entity_id
from .models import AssertionRecord, EntityMentionRecord
from .trust import GovernanceStatus, KnowledgeOrigin

VERSION = "mapped-record-review-proof:v1"
MAX_RECORDS = 6_000


def _digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def verify_mapped_record_batches(*, expected_batches, live_mentions, live_assertions,
                                tbox, semantic_context, resolved_identities=None,
                                proof_metadata=None):
    """Compare every current record with a provider-free source reconstruction.

    Produces review suggestions only. A source-scoped identity resolution is
    accepted solely if it matches the independently generated proof target.
    """
    expected_batches = tuple(expected_batches)
    expected_mentions_list = tuple(m for b in expected_batches for m in b.mentions)
    expected_assertions_list = tuple(a for b in expected_batches for a in b.assertions)
    mentions, assertions = tuple(live_mentions), tuple(live_assertions)
    if not expected_mentions_list or max(len(mentions)+len(assertions),
            len(expected_mentions_list)+len(expected_assertions_list)) > MAX_RECORDS:
        raise ValueError("mapped review proof is empty or exceeds its record budget")
    if any(not isinstance(m, EntityMentionRecord) for m in mentions) or any(
        not isinstance(a, AssertionRecord) for a in assertions
    ):
        raise TypeError("mapped review proof requires decoded governed records")
    originals = (*expected_mentions_list, *expected_assertions_list)
    origins = {r.trust.origin for r in originals}
    scopes = {(r.tenant_id, r.evidence.document_id, r.evidence.version_id,
               r.trust.ontology_version_id) for r in originals}
    if (len(scopes) != 1 or next(iter(scopes))[0] != tbox.tenant_id
            or next(iter(scopes))[3] != tbox.tbox_id or len(origins) != 1
            or not origins <= {KnowledgeOrigin.MAPPED, KnowledgeOrigin.AUTHORITATIVE_MAPPED}):
        raise ValueError("expected mapped output must have one source, ontology and trust scope")
    if not isinstance(semantic_context, dict) or semantic_context.get("runtime_event") is not False:
        raise ValueError("mapped semantic context must explicitly exclude runtime events")
    expected_mentions = {m.record_id: m for m in expected_mentions_list}
    expected_assertions = {a.record_id: a for a in expected_assertions_list}
    actual_mentions = {m.record_id: m for m in mentions}
    actual_assertions = {a.record_id: a for a in assertions}
    if (len(expected_mentions) != len(expected_mentions_list)
            or len(expected_assertions) != len(expected_assertions_list)
            or len(actual_mentions) != len(mentions) or len(actual_assertions) != len(assertions)
            or set(actual_mentions) != set(expected_mentions)
            or set(actual_assertions) != set(expected_assertions)):
        raise ValueError("mapped source candidates are missing, duplicated or contain unexpected records")
    approved_namespace = "source"
    type_namespaces = {e.name: e.canonical_key_namespaces for e in tbox.entity_types}
    targets = {}
    for item in expected_mentions_list:
        current = item.entity
        if approved_namespace not in type_namespaces[current.entity_type]:
            raise ValueError("ontology does not permit an independently source-scoped mapped identity")
        suffix = current.canonical_key.partition(":")[2]
        key = approved_namespace + ":" + suffix
        targets[current.entity_id] = replace(current, canonical_key=key,
            entity_id=entity_id(current.tenant_id, current.entity_type, key))
    resolved = dict(resolved_identities or {})
    if any(key not in targets or value != targets[key] for key, value in resolved.items()):
        raise ValueError("resolved mapped identity does not match the source-scoped proof target")

    def check_trust(record, want):
        trust = record.trust
        expected_trust = want.trust
        if (record.tenant_id != tbox.tenant_id or trust.origin is not expected_trust.origin
                or trust.authority is not expected_trust.authority
                or trust.ontology_version_id != tbox.tbox_id
                or trust.extractor_version != expected_trust.extractor_version
                or trust.prompt_version != expected_trust.prompt_version
                or trust.status not in {GovernanceStatus.CANDIDATE, GovernanceStatus.APPROVED, GovernanceStatus.PUBLISHED}):
            raise ValueError("mapped record trust or governance scope differs from its mapped construction")

    def canonical(expected_entity):
        return resolved.get(expected_entity.entity_id, expected_entity)

    for record_id, want in expected_mentions.items():
        actual = actual_mentions[record_id]
        check_trust(actual, want)
        if (actual.entity != canonical(want.entity) or actual.evidence != want.evidence
                or actual.confidence != want.confidence):
            raise ValueError("mapped mention identity, source evidence or confidence differs from reconstruction")
    expected_mentions_by_revision = {m.revision_id: m for m in expected_mentions_list}

    def endpoint(actual_revision_id, expected_revision_id, expected_entity):
        want = expected_mentions_by_revision[expected_revision_id]
        current = actual_mentions[want.record_id]
        if actual_revision_id != current.revision_id or current.entity != canonical(expected_entity):
            raise ValueError("mapped assertion endpoint does not reference its verified current mention")

    for record_id, want in expected_assertions.items():
        actual = actual_assertions[record_id]
        check_trust(actual, want)
        if (actual.subject != canonical(want.subject)
                or actual.object_entity != (canonical(want.object_entity) if want.object_entity else None)
                or actual.predicate != want.predicate or actual.evidence != want.evidence
                or actual.literal_value != want.literal_value
                or actual.literal_semantics != want.literal_semantics
                or actual.relationship_properties != want.relationship_properties
                or actual.confidence != want.confidence
                or actual.context_property_evidence != want.context_property_evidence
                or actual.fact_distinction != want.fact_distinction):
            raise ValueError("mapped assertion differs from its exact source-mapped reconstruction")
        endpoint(actual.subject_mention_revision_id, want.subject_mention_revision_id, want.subject)
        if want.object_entity:
            endpoint(actual.object_mention_revision_id, want.object_mention_revision_id, want.object_entity)
    resolutions = [{"record_id": m.record_id, "expected_revision": m.revision.revision,
                    "source_entity_id": expected_mentions[m.record_id].entity.entity_id,
                    "target": asdict(targets[expected_mentions[m.record_id].entity.entity_id])}
                   for m in mentions if m.entity != targets[expected_mentions[m.record_id].entity.entity_id]]
    scope = next(iter(scopes))
    manifest = {**dict(proof_metadata or {}), "version": VERSION,
                "verified": True, "review_applied": False,
                "document_id": scope[1], "version_id": scope[2], "tenant_id": scope[0],
                "ontology_checksum": tbox.checksum,
                "origin": next(iter(origins)).value,
                "authority": originals[0].trust.authority.value,
                "record_count": len(mentions) + len(assertions),
                "mention_record_ids": sorted(actual_mentions),
                "assertion_record_ids": sorted(actual_assertions),
                "identity_resolutions": resolutions,
                "revisions": {r.record_id: r.revision.revision for r in (*mentions, *assertions)},
                "semantic_context": semantic_context, "model_calls": 0}
    manifest["proof_checksum"] = _digest(manifest)
    return manifest
