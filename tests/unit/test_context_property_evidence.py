"""Exact dual evidence is an explicit contract, never an endpoint exception."""
from dataclasses import replace
from datetime import timedelta
import json
import unittest

from graphrag_prod.construction.structured import canonical, digest, locate_json
from graphrag_prod.domain.ids import content_checksum
from graphrag_prod.domain.models import TypedLiteralValue
from graphrag_prod.knowledge.models import ABoxRecordBatch, ContextPropertyEvidence
from graphrag_prod.knowledge.trust import GovernanceStatus
from graphrag_prod.domain.access import Principal
from graphrag_prod.knowledge.store import (
    KnowledgeStoreError, _revision_properties, _entity_properties, _stored_assertion,
)
from tests.fixtures.knowledge import make_knowledge_batch


def context_fixture():
    source = canonical({"metadata": {"project_id": "Plant A"}, "assets": [{"id": "M1"}]})
    root = locate_json(source)
    target, value, identity = root.at("/assets/0"), root.at("/metadata/project_id"), root.at("/assets/0/id")
    batch = make_knowledge_batch(authoritative=False)
    assertion = batch.assertions[0]
    evidence = replace(assertion.evidence, char_start=target.start, char_end=target.end,
                       quoted_text=source[target.start:target.end])
    mention = next(m for m in batch.mentions if m.revision_id == assertion.subject_mention_revision_id)
    mention = replace(mention, evidence=replace(evidence, char_start=identity.start + 1,
        char_end=identity.end - 1, quoted_text=source[identity.start + 1:identity.end - 1]))
    rule = {"collection": "/assets", "property": "project_id", "value_path": "/metadata/project_id",
        "scope_path": "", "entity_types": [mention.entity.entity_type],
        "binding": {"mode": "ANCESTOR_DEFAULT"}, "reason": "Explicit document default."}
    context = ContextPropertyEvidence(source_checksum=content_checksum(source), mapping_checksum=digest(rule),
        scope_pointer="", collection_pointer="/assets", record_pointer="/assets/0", identity_pointer="/id",
        value_pointer="/metadata/project_id", source_identity="M1", property_name="project_id",
        binding_json=canonical(rule), value_evidence=replace(evidence, chunk_id="separate-root-chunk",
            char_start=value.start, char_end=value.end, quoted_text=source[value.start:value.end]))
    literal = TypedLiteralValue(datatype="STRING", typed_value="Plant A", raw_value='"Plant A"',
                                canonical_value="Plant A", source_encoding="JSON_STRING")
    assertion = replace(assertion, predicate="project_id", evidence=evidence, object_entity=None,
        object_mention_revision_id=None, literal_value=literal.raw_value, literal_semantics=literal,
        context_property_evidence=context)
    return source, mention, assertion


class ContextPropertyEvidenceTests(unittest.TestCase):
    def test_projector_returns_both_citations_and_reserves_secondary_budget(self):
        from graphrag_prod.retrieval.subgraph import Neo4jEvidenceSubgraphProjector, EvidenceSubgraphLimits, SubgraphTrustPolicy
        from graphrag_prod.retrieval.models import VersionFilter
        from graphrag_prod.api.backend import _subgraph_payload
        from graphrag_prod.api.contracts import EvidenceSubgraphResponse
        source, mention, assertion = context_fixture()
        trust = assertion.trust.transition_to(GovernanceStatus.APPROVED, reviewed_by="fixture-reviewer",
            reviewed_at=assertion.created_at + timedelta(seconds=1), review_notes="Verified source scope.").transition_to(GovernanceStatus.PUBLISHED)
        assertion, mention = replace(assertion, trust=trust), replace(mention, trust=trust)
        citation = dict(tenant_id=assertion.tenant_id, chunk_id=assertion.evidence.chunk_id,
            chunk_checksum=content_checksum(source), chunk_text=source,
            document_id=assertion.evidence.document_id, document_title="Fixture", canonical_uri="urn:context-fixture",
            source_name="fixture", version_id=assertion.evidence.version_id, version_checksum=content_checksum(source),
            version_number=1, ordinal=0, char_start=0, char_end=len(source), page_number=None, section=None, published_at=assertion.created_at)
        entity = _entity_properties(mention.entity)
        entity["tenant_id"] = mention.tenant_id
        mention_properties = {**_revision_properties(mention), **entity}
        assertion_properties = {**_revision_properties(assertion), **_entity_properties(assertion.subject, "subject_"),
            "predicate": assertion.predicate, "subject_mention_revision_id": assertion.subject_mention_revision_id,
            "object_kind": "literal", "literal_value": assertion.literal_value, **assertion.literal_semantics.to_flat_properties()}
        mention_row = {"publication_id": "publication", "entity": entity, "mention": mention_properties, "citation": citation}
        row = {"publication_id": "publication", "seed_chunk_id": citation["chunk_id"], "seed_entity_id": mention.entity.entity_id,
            "assertion": assertion_properties, "subject": entity, "subject_mention": mention_properties,
            "object": None, "object_mention": None, "citation": citation,
            "context_citation": {**citation, "chunk_id": assertion.context_property_evidence.value_evidence.chunk_id, "ordinal": 1}}
        principal = Principal("reader", assertion.tenant_id, assertion.evidence.access_groups)
        args = (principal, frozenset({citation["chunk_id"]}), (row,), (mention_row,))
        graph = Neo4jEvidenceSubgraphProjector._project_rows(*args, EvidenceSubgraphLimits(),
            SubgraphTrustPolicy.PUBLISHED_SECONDARY_INCLUSIVE, VersionFilter())
        self.assertEqual(len(graph.literal_assertions), 1)
        self.assertEqual(graph.literal_assertions[0].context_value_evidence.quoted_text, assertion.literal_value)
        self.assertEqual(set(graph.matched_chunk_ids), {citation["chunk_id"], row["context_citation"]["chunk_id"]})
        EvidenceSubgraphResponse.model_validate(_subgraph_payload(graph))
        # A budget that fits the primary Chunk cannot silently omit the second citation.
        limited = Neo4jEvidenceSubgraphProjector._project_rows(*args,
            EvidenceSubgraphLimits(max_total_evidence_chars=len(source) + len(mention.evidence.quoted_text)),
            SubgraphTrustPolicy.PUBLISHED_SECONDARY_INCLUSIVE, VersionFilter())
        self.assertEqual(limited.literal_assertions, ())

    def test_context_literal_uses_second_source_but_endpoint_remains_primary(self):
        _, mention, assertion = context_fixture()
        ABoxRecordBatch(assertion.tenant_id, (mention,), (assertion,))
        self.assertNotIn(assertion.literal_value, assertion.evidence.quoted_text)
        with self.assertRaises(ValueError):
            replace(assertion, context_property_evidence=None)
        with self.assertRaises(ValueError):
            ABoxRecordBatch(assertion.tenant_id,
                (replace(mention, evidence=replace(mention.evidence, chunk_id="wrong-chunk")),), (assertion,))

    def test_context_cannot_cross_policy_version_or_tenant(self):
        _, _, assertion = context_fixture()
        context = assertion.context_property_evidence
        for field, value in (("tenant_id", "other"), ("version_id", "other"),
                             ("access_groups", frozenset({"secret"})), ("access_policy_version", 99)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                replace(assertion, context_property_evidence=replace(context,
                    value_evidence=replace(context.value_evidence, **{field: value})))

    def test_scope_and_predicate_cannot_be_retargeted(self):
        _, _, assertion = context_fixture()
        context = assertion.context_property_evidence
        with self.assertRaises(ValueError):
            replace(assertion, predicate="other_property")
        for changes in ({"scope_pointer": "/other"}, {"record_pointer": "/other/0"},
                        {"value_pointer": "/assets/1/id"}, {"value_pointer": "/bad~2pointer"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(context, **changes)

    def test_context_codec_roundtrip_checks_flat_projection(self):
        _, _, assertion = context_fixture()
        encoded = assertion.context_property_evidence.to_mapping()
        self.assertEqual(ContextPropertyEvidence.from_mapping(encoded), assertion.context_property_evidence)
        properties = {**_revision_properties(assertion), **_entity_properties(assertion.subject, "subject_"),
            "predicate": assertion.predicate, "subject_mention_revision_id": assertion.subject_mention_revision_id,
            "object_kind": "literal", "literal_value": assertion.literal_value,
            **assertion.literal_semantics.to_flat_properties()}
        self.assertEqual(_stored_assertion(properties), assertion)
        with self.assertRaises(KnowledgeStoreError):
            _stored_assertion({**properties, "context_evidence_text": "forged"})

    def test_canonical_binding_is_bounded_and_strict(self):
        _, _, assertion = context_fixture()
        context = assertion.context_property_evidence
        for value in ("[]", '{"a": 1}', " " * 8193):
            with self.subTest(value=value[:20]), self.assertRaises(ValueError):
                replace(context, binding_json=value)


if __name__ == "__main__":
    unittest.main()
