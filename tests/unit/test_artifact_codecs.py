"""Portable derivation-artifact codec tests."""

from __future__ import annotations

from dataclasses import replace
import copy
import unittest

from graphrag_prod.domain import (
    Assertion,
    Entity,
    EntityMention,
    TypedLiteralValue,
    assertion_id,
    entity_id,
    mention_id,
)
from graphrag_prod.ingestion.artifacts import decode_extraction, encode_extraction
from graphrag_prod.ingestion.models import default_artifact_input_hash
from tests.fixtures.ingestion import make_bundles, make_profile


class ArtifactCodecTests(unittest.TestCase):
    @staticmethod
    def _enrich(bundle, *, reverse: bool):
        profile = make_profile()
        company = bundle.entities[0]
        product = Entity(
            entity_id=entity_id(bundle.chunk.tenant_id, "Product", "product:revenue"),
            tenant_id=bundle.chunk.tenant_id,
            entity_type="Product",
            canonical_key="product:revenue",
            canonical_name="Revenue",
        )
        start = bundle.chunk.char_start + bundle.chunk.text.index("revenue")
        end = start + len("revenue")
        product_mention = EntityMention(
            mention_id=mention_id(
                bundle.chunk.chunk_id,
                product.entity_type,
                start,
                end,
                "revenue",
                profile.extractor_signature,
            ),
            tenant_id=bundle.chunk.tenant_id,
            chunk_id=bundle.chunk.chunk_id,
            entity_id=product.entity_id,
            entity_type=product.entity_type,
            surface="revenue",
            char_start=start,
            char_end=end,
            extractor_version=profile.extractor_signature,
            confidence=1.0,
        )
        relation = Assertion(
            assertion_id=assertion_id(
                bundle.chunk.tenant_id,
                company.entity_id,
                "OFFERS",
                "entity",
                product.entity_id,
                bundle.chunk.chunk_id,
                bundle.chunk.char_start,
                bundle.chunk.char_end,
                profile.extractor_signature,
                profile.schema_signature,
            ),
            tenant_id=bundle.chunk.tenant_id,
            subject_entity_id=company.entity_id,
            predicate="OFFERS",
            evidence_chunk_id=bundle.chunk.chunk_id,
            evidence_char_start=bundle.chunk.char_start,
            evidence_char_end=bundle.chunk.char_end,
            extractor_version=profile.extractor_signature,
            schema_version=profile.schema_signature,
            confidence=1.0,
            accepted=True,
            object_entity_id=product.entity_id,
        )
        original = bundle.assertion
        if original is None:
            raise AssertionError("fixture bundle requires its metric assertion")
        mentions = (*bundle.mentions, product_mention)
        entities = (*bundle.entities, product)
        return replace(
            bundle,
            entities=tuple(reversed(entities)) if reverse else entities,
            mentions=tuple(reversed(mentions)) if reverse else mentions,
            assertion=relation if reverse else original,
            additional_assertions=(original if reverse else relation,),
        )

    def test_identical_inputs_encode_identically_and_rebind_stable_ids(self) -> None:
        first = self._enrich(
            make_bundles(canonical_uri="https://example.com/first")[0],
            reverse=False,
        )
        second = self._enrich(
            make_bundles(canonical_uri="https://example.com/second")[0],
            reverse=True,
        )
        profile = make_profile()
        self.assertNotEqual(first.chunk.chunk_id, second.chunk.chunk_id)
        self.assertEqual(first.chunk.text, second.chunk.text)
        self.assertEqual(
            default_artifact_input_hash(first),
            default_artifact_input_hash(second),
        )

        payload = encode_extraction(first)
        self.assertEqual(payload, encode_extraction(second))
        first_decoded = decode_extraction(
            payload,
            tenant_id=first.chunk.tenant_id,
            chunk=first.chunk,
            profile=profile,
        )
        second_decoded = decode_extraction(
            payload,
            tenant_id=second.chunk.tenant_id,
            chunk=second.chunk,
            profile=profile,
        )
        self.assertEqual(
            {entity.entity_id for entity in first_decoded[0]},
            {entity.entity_id for entity in second_decoded[0]},
        )
        self.assertTrue(
            {mention.mention_id for mention in first_decoded[1]}.isdisjoint(
                mention.mention_id for mention in second_decoded[1]
            )
        )
        self.assertTrue(
            {assertion.assertion_id for assertion in first_decoded[2]}.isdisjoint(
                assertion.assertion_id for assertion in second_decoded[2]
            )
        )
        self.assertTrue(
            all(mention.chunk_id == first.chunk.chunk_id for mention in first_decoded[1])
        )
        self.assertTrue(
            all(
                assertion.evidence_chunk_id == second.chunk.chunk_id
                for assertion in second_decoded[2]
            )
        )

    def test_v2_round_trips_typed_literal_semantics_and_v1_remains_readable(
        self,
    ) -> None:
        bundle = make_bundles()[0]
        original = bundle.assertion
        if original is None:
            raise AssertionError("fixture bundle requires its metric assertion")
        literal = TypedLiteralValue(
            datatype="INTEGER",
            typed_value=391,
            raw_value="391",
            canonical_value="391",
        )
        typed = replace(
            original,
            assertion_id=assertion_id(
                original.tenant_id,
                original.subject_entity_id,
                original.predicate,
                "literal",
                literal.identity_reference,
                original.evidence_chunk_id,
                original.evidence_char_start,
                original.evidence_char_end,
                original.extractor_version,
                original.schema_version,
            ),
            literal_semantics=literal,
        )
        typed_bundle = replace(bundle, assertion=typed)

        payload = encode_extraction(typed_bundle)
        self.assertEqual(payload["format_version"], 2)
        decoded = decode_extraction(
            payload,
            tenant_id=bundle.chunk.tenant_id,
            chunk=bundle.chunk,
            profile=make_profile(),
        )
        self.assertEqual(decoded[2][0].literal_semantics, literal)
        self.assertEqual(decoded[2][0].object_reference, literal.identity_reference)

        legacy = encode_extraction(bundle)
        legacy["format_version"] = 1
        for item in legacy["assertions"]:
            item.pop("literal_semantics")
        legacy_decoded = decode_extraction(
            legacy,
            tenant_id=bundle.chunk.tenant_id,
            chunk=bundle.chunk,
            profile=make_profile(),
        )
        self.assertIsNone(legacy_decoded[2][0].literal_semantics)
        self.assertEqual(legacy_decoded[2][0].literal_value, "391")

        tampered = copy.deepcopy(payload)
        tampered["assertions"][0]["literal_semantics"][
            "canonical_value"
        ] = "392"
        with self.assertRaisesRegex(ValueError, "must match"):
            decode_extraction(
                tampered,
                tenant_id=bundle.chunk.tenant_id,
                chunk=bundle.chunk,
                profile=make_profile(),
            )

        malformed = copy.deepcopy(legacy)
        malformed["assertions"][0]["literal_value"] = None
        with self.assertRaisesRegex(ValueError, "literal assertion"):
            decode_extraction(
                malformed,
                tenant_id=bundle.chunk.tenant_id,
                chunk=bundle.chunk,
                profile=make_profile(),
            )


if __name__ == "__main__":
    unittest.main()
