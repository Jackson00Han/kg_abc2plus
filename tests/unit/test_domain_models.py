"""Stable identity, model invariant, access, and provenance tests."""

from __future__ import annotations

import dataclasses
from pathlib import Path
import unittest

from graphrag_prod.domain.access import (
    Principal,
    active_retrieval_scope,
    can_access,
    retrieval_scope_token,
)
from graphrag_prod.domain.ids import (
    assertion_id,
    canonicalize_uri,
    chunk_embedding_id,
    content_checksum,
    document_id,
    embedding_space_id,
    entity_id,
    mention_id,
    version_id,
)
from graphrag_prod.domain.models import Assertion, DocumentVersion
from tests.fixtures.domain import authorized_principal, make_bundle


class StableIdentityTests(unittest.TestCase):
    def test_fixture_ids_match_golden_scheme_version(self) -> None:
        bundle = make_bundle()
        self.assertEqual(bundle.document.document_id, "d77d5401-e632-5611-af5f-e4d81ebfb161")
        self.assertEqual(bundle.version.version_id, "a46ef91e-285c-5826-a366-26eed95d682c")
        self.assertEqual(bundle.chunk.chunk_id, "093286a2-0e7c-57bf-b565-793151f47c07")
        self.assertEqual(bundle.embedding.embedding_id, "efd55b8f-6338-52d8-991a-d0f6f54be856")
        self.assertEqual(bundle.mentions[0].mention_id, "48d8cc4c-ba39-5a01-9c99-552d0a5c9969")
        self.assertEqual(bundle.assertion.assertion_id, "46baaea4-5b00-5a12-83e3-996706574129")

    def test_uri_canonicalization_and_document_id_are_stable(self) -> None:
        canonical = canonicalize_uri("HTTPS://Example.COM:443/filings/apple-2024/#x")
        self.assertEqual(canonical, "https://example.com/filings/apple-2024")
        self.assertEqual(document_id("tenant-a", canonical), document_id("tenant-a", canonical))
        self.assertNotEqual(document_id("tenant-a", canonical), document_id("tenant-b", canonical))

    def test_content_or_tenant_changes_identity(self) -> None:
        document = document_id("tenant-a", "https://example.com/report")
        first = version_id(document, content_checksum("first"))
        second = version_id(document, content_checksum("second"))
        self.assertNotEqual(first, second)
        normalized = content_checksum("normalized")
        self.assertNotEqual(
            version_id(document, normalized, content_checksum(b"original-a")),
            version_id(document, normalized, content_checksum(b"original-b")),
        )
        self.assertNotEqual(
            entity_id("tenant-a", "Company", "registry:42"),
            entity_id("tenant-b", "Company", "registry:42"),
        )

    def test_checksum_inputs_must_be_sha256(self) -> None:
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            version_id("document-id", "not-a-checksum")

    def test_derived_profile_changes_derived_identity(self) -> None:
        bundle = make_bundle()
        mention_v2 = mention_id(
            bundle.chunk.chunk_id,
            "Company",
            0,
            5,
            "Apple",
            "deterministic-extractor:v2",
        )
        self.assertNotEqual(mention_v2, bundle.mentions[0].mention_id)
        assertion_v2 = assertion_id(
            bundle.document.tenant_id,
            bundle.assertion.subject_entity_id,
            bundle.assertion.predicate,
            "entity",
            bundle.assertion.object_entity_id or "",
            bundle.chunk.chunk_id,
            0,
            len(bundle.chunk.text),
            bundle.assertion.extractor_version,
            "company-filings:v2",
        )
        self.assertNotEqual(assertion_v2, bundle.assertion.assertion_id)
        new_space = embedding_space_id("test", "deterministic-embedding", "v2", 4, "l2")
        self.assertNotEqual(
            chunk_embedding_id(bundle.chunk.chunk_id, new_space),
            bundle.embedding.embedding_id,
        )


class DomainInvariantTests(unittest.TestCase):
    def test_complete_bundle_round_trip_invariants(self) -> None:
        bundle = make_bundle()
        self.assertEqual(
            bundle.version.normalized_text[
                bundle.chunk.char_start : bundle.chunk.char_end
            ],
            bundle.chunk.text,
        )
        self.assertNotIn("active", {field.name for field in dataclasses.fields(bundle.version)})
        self.assertNotIn("active", {field.name for field in dataclasses.fields(bundle.chunk)})

    def test_chunk_checksum_mismatch_is_rejected(self) -> None:
        bundle = make_bundle()
        with self.assertRaisesRegex(ValueError, "checksum must match text"):
            dataclasses.replace(bundle.chunk, checksum=content_checksum("wrong"))

    def test_version_checksum_mismatch_is_rejected(self) -> None:
        bundle = make_bundle()
        with self.assertRaisesRegex(ValueError, "checksum must match normalized_text"):
            DocumentVersion(
                **{
                    **dataclasses.asdict(bundle.version),
                    "checksum": content_checksum("wrong"),
                }
            )

    def test_broader_chunk_acl_is_rejected(self) -> None:
        bundle = make_bundle()
        chunk = dataclasses.replace(
            bundle.chunk,
            access_groups=frozenset({"finance-readers", "public"}),
        )
        with self.assertRaisesRegex(ValueError, "access cannot be broader"):
            dataclasses.replace(bundle, chunk=chunk)

    def test_cross_tenant_entity_is_rejected(self) -> None:
        bundle = make_bundle()
        foreign = dataclasses.replace(bundle.entities[0], tenant_id="other-tenant")
        with self.assertRaisesRegex(ValueError, "one tenant"):
            dataclasses.replace(
                bundle,
                entities=(foreign, bundle.entities[1]),
            )

    def test_mention_surface_mismatch_is_rejected(self) -> None:
        bundle = make_bundle()
        mention = dataclasses.replace(bundle.mentions[0], surface="Applf")
        with self.assertRaisesRegex(ValueError, "surface does not match"):
            dataclasses.replace(bundle, mentions=(mention, bundle.mentions[1]))

    def test_models_are_immutable(self) -> None:
        bundle = make_bundle()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            bundle.chunk.text = "changed"  # type: ignore[misc]

    def test_evidence_whitespace_is_preserved(self) -> None:
        source = "\n Apple offers iPhone. \n"
        bundle = make_bundle(source_text=source)
        self.assertEqual(bundle.chunk.text, source)
        self.assertEqual(bundle.version.normalized_text, source)

    def test_orphan_entity_is_rejected(self) -> None:
        bundle = make_bundle()
        with self.assertRaisesRegex(ValueError, "every derived entity requires"):
            dataclasses.replace(bundle, mentions=(bundle.mentions[0],))

    def test_literal_without_source_evidence_is_rejected(self) -> None:
        bundle = make_bundle()
        literal = "not-present"
        identifier = assertion_id(
            bundle.document.tenant_id,
            bundle.assertion.subject_entity_id,
            "HAS_VALUE",
            "literal",
            literal,
            bundle.chunk.chunk_id,
            0,
            len(bundle.chunk.text),
            bundle.assertion.extractor_version,
            bundle.assertion.schema_version,
        )
        assertion = Assertion(
            assertion_id=identifier,
            tenant_id=bundle.document.tenant_id,
            subject_entity_id=bundle.assertion.subject_entity_id,
            predicate="HAS_VALUE",
            literal_value=literal,
            evidence_chunk_id=bundle.chunk.chunk_id,
            evidence_char_start=0,
            evidence_char_end=len(bundle.chunk.text),
            extractor_version=bundle.assertion.extractor_version,
            schema_version=bundle.assertion.schema_version,
            confidence=1.0,
            accepted=True,
        )
        with self.assertRaisesRegex(ValueError, "literal assertion object is absent"):
            dataclasses.replace(bundle, assertion=assertion)

    def test_embedding_profile_mismatch_is_rejected(self) -> None:
        bundle = make_bundle()
        with self.assertRaisesRegex(ValueError, "embedding_space_id"):
            dataclasses.replace(bundle.embedding, revision="different")

    def test_missing_required_metadata_is_rejected(self) -> None:
        bundle = make_bundle()
        cases = (
            lambda: dataclasses.replace(bundle.document, tenant_id=""),
            lambda: dataclasses.replace(bundle.document, access_groups=frozenset()),
            lambda: dataclasses.replace(bundle.version, mime_type=""),
            lambda: dataclasses.replace(bundle.chunk, splitter_version=""),
            lambda: dataclasses.replace(bundle.embedding, model=""),
            lambda: dataclasses.replace(bundle.entities[0], canonical_key=""),
            lambda: dataclasses.replace(bundle.mentions[0], extractor_version=""),
            lambda: dataclasses.replace(bundle.assertion, evidence_chunk_id=""),
        )
        for construct in cases:
            with self.subTest(construct=construct), self.assertRaises(ValueError):
                construct()

    def test_production_package_has_no_element_id_dependency(self) -> None:
        package = Path(__file__).resolve().parents[2] / "src" / "graphrag_prod"
        offenders = [
            path
            for path in package.rglob("*.py")
            if "elementId" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [])


class AccessPolicyTests(unittest.TestCase):
    def test_access_is_same_tenant_and_group_only(self) -> None:
        principal = authorized_principal()
        self.assertTrue(
            can_access(principal, "tenant-stage2", frozenset({"finance-readers"}))
        )
        self.assertFalse(can_access(principal, "other", frozenset({"finance-readers"})))
        self.assertFalse(can_access(principal, "tenant-stage2", frozenset({"legal"})))

    def test_principal_requires_an_explicit_group(self) -> None:
        with self.assertRaisesRegex(ValueError, "groups"):
            Principal("alice", "tenant", frozenset())

    def test_active_retrieval_scope_is_hashed_stable_and_lucene_safe(self) -> None:
        scope = active_retrieval_scope(
            "tenant-stage2",
            frozenset({"legal", "finance-readers"}),
        )
        self.assertEqual(scope.split()[0], "grscopeactive")
        self.assertEqual(scope, active_retrieval_scope(
            "tenant-stage2",
            frozenset({"finance-readers", "legal"}),
        ))
        self.assertNotIn("tenant-stage2", scope)
        self.assertNotIn("finance-readers", scope)
        self.assertIn(retrieval_scope_token("group", "legal"), scope)
        with self.assertRaises(ValueError):
            retrieval_scope_token("unknown", "value")


if __name__ == "__main__":
    unittest.main()
