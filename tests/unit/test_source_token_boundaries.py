"""Typed source-token consistency through provenance and governed A-Box records."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import json
import unittest

from graphrag_prod.construction import ExtractionRejected, OpenAICompatibleOntologyExtractor
from graphrag_prod.construction.workflow import (
    _audited_payload,
    _decode_audited_payload,
    _to_abox_batch,
)
from graphrag_prod.domain.ids import assertion_id, relationship_property_value_id
from graphrag_prod.domain.models import RelationshipPropertyValue, TypedLiteralValue
from graphrag_prod.domain.source_tokens import (
    LITERAL_BOUNDARY_POLICY,
    contains_exact_token,
)
from graphrag_prod.knowledge import AuthorityLevel, GovernanceStatus
from graphrag_prod.ontology.models import (
    Cardinality,
    EntityTypeDefinition,
    PropertyDataType,
    PropertyDefinition,
    RelationshipTypeDefinition,
    TBoxStatus,
    TBoxVersion,
)
from tests.fixtures.domain import make_bundle
from tests.unit.test_construction_extraction import _profile


SOURCE = "Apple offers iPhone，采用项目模拟配置。"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _context(source: str = SOURCE):
    bundle = make_bundle(tenant_id="tenant-industrial", source_text=source)
    tbox = TBoxVersion(
        tenant_id=bundle.document.tenant_id,
        key="source-token-test",
        version=1,
        status=TBoxStatus.PUBLISHED,
        entity_types=(
            EntityTypeDefinition(
                "Company", ("llm-candidate",),
                properties=(PropertyDefinition("kind", PropertyDataType.STRING, False, Cardinality.ZERO_OR_ONE),),
            ),
            EntityTypeDefinition("Product", ("llm-candidate",)),
        ),
        relationship_types=(
            RelationshipTypeDefinition(
                "OFFERS", ("Company",), ("Product",),
                properties=(PropertyDefinition("mode", PropertyDataType.STRING, False, Cardinality.ZERO_OR_ONE),),
            ),
        ),
    )
    profile = _profile()
    extractor = OpenAICompatibleOntologyExtractor(
        client=object(), model="offline-boundary-test", active_tbox=tbox,
        prompt_version=profile.prompt_signature,
    )
    evidence = {"text": source, "start": 0, "end": len(source)}
    literal = {
        "raw_literal": "项目模拟配置", "unit": None, "valid_from": None,
        "valid_to": None, "observed_at": None, "confidence": 1.0,
        "evidence": evidence,
    }
    payload = {
        "entities": [
            {"ref": ref, "type": kind, "mentions": [{
                "text": name, "start": source.index(name),
                "end": source.index(name) + len(name), "confidence": 1.0,
            }]}
            for ref, kind, name in (("e1", "Company", "Apple"), ("e2", "Product", "iPhone"))
        ],
        "relationships": [{
            "source_ref": "e1", "target_ref": "e2", "type": "OFFERS",
            "evidence": evidence, "confidence": 1.0,
            "properties": [{"property": "mode", **literal}],
        }],
        "property_facts": [{"entity_ref": "e1", "property": "kind", **literal}],
    }
    return bundle, tbox, profile, extractor, payload


def _typed(raw: str, datatype: str = "STRING", **values) -> TypedLiteralValue:
    typed = int(raw) if datatype == "INTEGER" else raw
    return TypedLiteralValue(
        datatype=datatype, typed_value=typed, raw_value=raw,
        canonical_value=raw, **values,
    )


def _literal_assertion(bundle, literal: TypedLiteralValue | None, raw: str):
    source = bundle.assertion
    result = replace(
        source, object_entity_id=None, literal_value=raw,
        literal_semantics=literal, relationship_properties=(), predicate="kind",
    )
    return replace(result, assertion_id=assertion_id(
        result.tenant_id, result.subject_entity_id, result.predicate,
        "literal", result.object_reference, result.evidence_chunk_id,
        result.evidence_char_start, result.evidence_char_end,
        result.extractor_version, result.schema_version,
    ))


def _relationship_property(bundle, literal: TypedLiteralValue):
    source = bundle.assertion
    return RelationshipPropertyValue(
        property_value_id=relationship_property_value_id(
            source.tenant_id, source.predicate, "mode", literal.identity_reference,
            source.evidence_chunk_id, 0, len(bundle.chunk.text),
            source.extractor_version, source.schema_version,
        ),
        tenant_id=source.tenant_id, relationship_type=source.predicate,
        name="mode", literal_semantics=literal,
        evidence_chunk_id=source.evidence_chunk_id, evidence_char_start=0,
        evidence_char_end=len(bundle.chunk.text), evidence_text=bundle.chunk.text,
        extractor_version=source.extractor_version, schema_version=source.schema_version,
    )


def _strict_cases():
    instant = "2026-01-01T00:00:00Z"
    return (
        ("1000", _typed("100", "INTEGER")),
        ("100 兆帕", _typed("100", "DECIMAL", raw_unit="帕", canonical_unit="Pa")),
        ("100 MPa", _typed("100", "DECIMAL", raw_unit="Pa", canonical_unit="Pa")),
        ("前甲A123乙后", _typed("甲A123乙")),
        ("前カタカナ後", _typed("カタカナ")),
        ("X项目配置", _typed("项目配置")),
        (instant, _typed("2026-01-01", "DATE")),
        ("采用项目模拟配置 1" + instant, _typed(
            "项目模拟配置", raw_observed_at=instant, observed_at=NOW,
        )),
    )


class SourceTokenDownstreamTests(unittest.TestCase):
    def test_typed_han_literals_survive_audit_encoding_and_abox_conversion(self):
        bundle, tbox, profile, extractor, payload = _context()
        audited = extractor.revalidate_saved_response(json.dumps(payload), chunk=bundle.chunk, profile=profile)
        encoded = _audited_payload(
            audited, document=bundle.document, version=bundle.version,
            chunk=bundle.chunk, extracted_at=NOW,
        )
        restored, findings, recorded_at = _decode_audited_payload(
            json.loads(json.dumps(encoded)), tbox=tbox, extractor=extractor,
            chunk=bundle.chunk, profile=profile,
        )
        self.assertEqual(findings, ())
        batch = _to_abox_batch(restored, chunk=bundle.chunk, extracted_at=recorded_at)
        self.assertEqual(len(batch.assertions), 2)
        entity_value = next(item for item in batch.assertions if item.literal_semantics)
        relation = next(item for item in batch.assertions if item.relationship_properties)
        self.assertEqual(entity_value.literal_value, "项目模拟配置")
        self.assertEqual(entity_value.literal_semantics.datatype, "STRING")
        self.assertEqual(relation.relationship_properties[0].literal_semantics.raw_value, "项目模拟配置")
        for item in batch.assertions:
            self.assertEqual(item.trust.status, GovernanceStatus.CANDIDATE)
            self.assertEqual(item.trust.authority, AuthorityLevel.SECONDARY)
            self.assertEqual(item.evidence.quoted_text, SOURCE)
            self.assertEqual((item.evidence.char_start, item.evidence.char_end), (0, len(SOURCE)))

    def test_untyped_han_is_rejected_by_provenance_and_governed_record(self):
        bundle, _, profile, extractor, payload = _context()
        audited = extractor.revalidate_saved_response(json.dumps(payload), chunk=bundle.chunk, profile=profile)
        untyped = _literal_assertion(bundle, None, "项目模拟配置")
        with self.assertRaisesRegex(ValueError, "literal assertion object"):
            replace(bundle, assertion=untyped)
        batch = _to_abox_batch(audited, chunk=bundle.chunk, extracted_at=NOW)
        typed = next(item for item in batch.assertions if item.literal_semantics)
        with self.assertRaisesRegex(ValueError, "literal_value must occur"):
            replace(typed, literal_semantics=None)

    def test_entity_literal_numeric_units_time_and_other_scripts_remain_strict(self):
        _, _, profile, extractor, payload = _context()
        initial_bundle = _context()[0]
        audited = extractor._validate_content(json.dumps(payload), chunk=initial_bundle.chunk, profile=profile)
        original_batch = _to_abox_batch(audited, chunk=initial_bundle.chunk, extracted_at=NOW)
        typed_record = next(item for item in original_batch.assertions if item.literal_semantics)
        for suffix, literal in _strict_cases():
            with self.subTest(suffix=suffix, datatype=literal.datatype):
                bundle = make_bundle(source_text="Apple offers iPhone. " + suffix)
                assertion = _literal_assertion(bundle, literal, literal.raw_value)
                with self.assertRaisesRegex(ValueError, "literal|source tokens"):
                    replace(bundle, assertion=assertion)
                evidence = replace(
                    typed_record.evidence, quoted_text=bundle.chunk.text,
                    char_end=len(bundle.chunk.text),
                )
                with self.assertRaisesRegex(ValueError, "literal|source tokens"):
                    replace(typed_record, evidence=evidence, literal_value=literal.raw_value,
                            literal_semantics=literal)

    def test_relationship_literal_numeric_units_time_and_other_scripts_remain_strict(self):
        for suffix, literal in _strict_cases():
            with self.subTest(suffix=suffix, datatype=literal.datatype):
                bundle = make_bundle(source_text="Apple offers iPhone. " + suffix)
                with self.assertRaisesRegex(ValueError, "source tokens"):
                    _relationship_property(bundle, literal)

    def test_relationship_typed_han_allows_exact_text_but_no_source_rewrite(self):
        bundle = make_bundle(source_text=SOURCE)
        value = _relationship_property(bundle, _typed("项目模拟配置"))
        self.assertEqual(value.evidence_text, SOURCE)
        with self.assertRaisesRegex(ValueError, "source tokens"):
            _relationship_property(bundle, _typed("项目模擬配置"))
        with self.assertRaisesRegex(ValueError, "evidence text must match"):
            replace(value, evidence_char_start=1)

    def test_saved_response_validation_rejects_types_limits_and_tenant_without_provider(self):
        bundle, _, profile, extractor, payload = _context()
        for content in (None, b"{}", {}, [], True, ""):
            with self.subTest(content_type=type(content).__name__):
                with self.assertRaisesRegex(ValueError, "non-empty string"):
                    extractor.revalidate_saved_response(content, chunk=bundle.chunk, profile=profile)
        extractor.limits = replace(extractor.limits, max_response_chars=2048)
        with self.assertRaises(ExtractionRejected) as oversized:
            extractor.revalidate_saved_response(" " * 2049, chunk=bundle.chunk, profile=profile)
        self.assertEqual(oversized.exception.findings[0].code, "RESPONSE_TOO_LARGE")
        with self.assertRaisesRegex(ValueError, "chunk tenant"):
            extractor.revalidate_saved_response("{}", chunk=replace(bundle.chunk, tenant_id="other-tenant"), profile=profile)

    def test_saved_success_text_is_rechecked_for_refs_and_exact_spans(self):
        bundle, _, profile, extractor, payload = _context()
        for field in ("ref", "span"):
            value = json.loads(json.dumps(payload))
            if field == "ref":
                value["entities"][0]["ref"] = "invalid ref"
                expected = "INVALID_LOCAL_REFERENCE"
            else:
                value["relationships"][0]["evidence"]["start"] = 1
                expected = "EVIDENCE_SPAN_MISMATCH"
            with self.subTest(field=field):
                with self.assertRaises(ExtractionRejected) as invalid:
                    extractor.revalidate_saved_response(json.dumps(value), chunk=bundle.chunk, profile=profile)
                self.assertIn(expected, {item.code for item in invalid.exception.findings})

    def test_shared_default_stays_strict_and_v6_boundary_version_is_unchanged(self):
        self.assertEqual(LITERAL_BOUNDARY_POLICY, "typed-string-cjk-adjacency-v1")
        self.assertFalse(contains_exact_token("采用项目模拟配置", "项目模拟配置"))
        self.assertTrue(contains_exact_token("采用项目模拟配置", "项目模拟配置", allow_cjk_adjacency=True))
        self.assertFalse(contains_exact_token("兆帕", "帕"))
        self.assertFalse(contains_exact_token("内容", ""))


if __name__ == "__main__":
    unittest.main()
