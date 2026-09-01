"""Real Neo4j tests for schema constraints and provenance paths."""

from __future__ import annotations

import dataclasses
import ipaddress
import os
import unittest
from urllib.parse import urlparse

import neo4j

from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import chunk_embedding_id, embedding_space_id
from graphrag_prod.graph.provenance import Neo4jProvenanceStore
from graphrag_prod.graph.schema import apply_schema, verify_schema
from tests.fixtures.domain import authorized_principal, make_bundle


class Neo4jProvenanceIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        required = (
            "TEST_NEO4J_URI",
            "TEST_NEO4J_USER",
            "TEST_NEO4J_PASSWORD",
            "TEST_NEO4J_DATABASE",
        )
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            raise RuntimeError(f"missing disposable Neo4j settings: {missing}")
        if os.getenv("GRAPHRAG_ALLOW_DISPOSABLE_DB") != "1":
            raise RuntimeError("GRAPHRAG_ALLOW_DISPOSABLE_DB=1 is required")

        uri = os.environ["TEST_NEO4J_URI"]
        host = urlparse(uri).hostname
        if host is None or not ipaddress.ip_address(host).is_loopback:
            raise RuntimeError("integration tests only accept a loopback Neo4j URI")
        cls.database = os.environ["TEST_NEO4J_DATABASE"]
        cls.driver = neo4j.GraphDatabase.driver(
            uri,
            auth=(
                os.environ["TEST_NEO4J_USER"],
                os.environ["TEST_NEO4J_PASSWORD"],
            ),
        )
        cls.driver.verify_connectivity()
        records, _, _ = cls.driver.execute_query(
            "MATCH (node) RETURN count(node) AS count",
            database_=cls.database,
        )
        if records[0]["count"] != 0:
            cls.driver.close()
            raise RuntimeError("disposable Neo4j database must start empty")
        apply_schema(cls.driver, cls.database)
        apply_schema(cls.driver, cls.database)
        schema_errors = verify_schema(cls.driver, cls.database)
        if schema_errors:
            cls.driver.close()
            raise RuntimeError(f"schema verification failed: {schema_errors}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.driver.close()

    def setUp(self) -> None:
        self.driver.execute_query(
            "MATCH (node) DETACH DELETE node",
            database_=self.database,
        )
        self.store = Neo4jProvenanceStore(self.driver, self.database)

    def tearDown(self) -> None:
        self.driver.execute_query(
            "MATCH (node) DETACH DELETE node",
            database_=self.database,
        )

    def test_schema_is_structurally_valid_and_online(self) -> None:
        self.assertEqual(verify_schema(self.driver, self.database), [])

    def test_assertion_round_trips_to_exact_authorized_source(self) -> None:
        bundle = make_bundle()
        self.store.write_bundle(bundle)
        self.store.write_bundle(bundle)

        evidence = self.store.get_assertion_evidence(
            authorized_principal(),
            bundle.assertion.assertion_id,
        )
        self.assertEqual(len(evidence), 1)
        view = evidence[0]
        self.assertEqual(view.chunk_id, bundle.chunk.chunk_id)
        self.assertEqual(view.chunk_checksum, bundle.chunk.checksum)
        self.assertEqual(view.version_checksum, bundle.version.checksum)
        self.assertEqual(view.text, bundle.chunk.text)
        self.assertEqual(view.canonical_uri, bundle.document.canonical_uri)
        self.assertEqual(
            bundle.version.normalized_text[view.char_start : view.char_end],
            view.text,
        )

        wrong_group = Principal("mallory", bundle.document.tenant_id, frozenset({"legal"}))
        wrong_tenant = Principal("mallory", "other-tenant", frozenset({"finance-readers"}))
        self.assertEqual(
            self.store.get_assertion_evidence(
                wrong_group,
                bundle.assertion.assertion_id,
            ),
            (),
        )
        self.assertEqual(
            self.store.get_assertion_evidence(
                wrong_tenant,
                bundle.assertion.assertion_id,
            ),
            (),
        )

    def test_document_and_chunk_acl_filters_are_independently_required(self) -> None:
        bundle = make_bundle()
        self.store.write_bundle(bundle)
        principal = authorized_principal()

        self.driver.execute_query(
            "MATCH (chunk:Chunk {chunk_id: $chunk_id}) "
            "SET chunk.access_groups = ['chunk-only']",
            chunk_id=bundle.chunk.chunk_id,
            database_=self.database,
        )
        self.assertEqual(
            self.store.get_assertion_evidence(principal, bundle.assertion.assertion_id),
            (),
        )

        self.driver.execute_query(
            "MATCH (document:Document {document_id: $document_id}) "
            "SET document.access_groups = ['document-only'] "
            "WITH document MATCH (chunk:Chunk {chunk_id: $chunk_id}) "
            "SET chunk.access_groups = ['finance-readers']",
            document_id=bundle.document.document_id,
            chunk_id=bundle.chunk.chunk_id,
            database_=self.database,
        )
        self.assertEqual(
            self.store.get_assertion_evidence(principal, bundle.assertion.assertion_id),
            (),
        )

    def test_unpublished_or_unaccepted_evidence_is_invisible(self) -> None:
        unpublished = make_bundle(activate_version=False)
        self.store.write_bundle(unpublished)
        self.assertEqual(
            self.store.get_assertion_evidence(
                authorized_principal(),
                unpublished.assertion.assertion_id,
            ),
            (),
        )

        self.driver.execute_query("MATCH (node) DETACH DELETE node", database_=self.database)
        bundle = make_bundle()
        rejected = dataclasses.replace(
            bundle,
            assertion=dataclasses.replace(bundle.assertion, accepted=False),
        )
        self.store.write_bundle(rejected)
        self.assertEqual(
            self.store.get_assertion_evidence(
                authorized_principal(),
                rejected.assertion.assertion_id,
            ),
            (),
        )
        self.store.write_bundle(bundle)
        self.assertEqual(
            len(
                self.store.get_assertion_evidence(
                    authorized_principal(),
                    bundle.assertion.assertion_id,
                )
            ),
            1,
        )

    def test_access_policy_snapshot_can_change_atomically(self) -> None:
        bundle = make_bundle()
        self.store.write_bundle(bundle)
        new_groups = frozenset({"legal"})
        updated = dataclasses.replace(
            bundle,
            document=dataclasses.replace(
                bundle.document,
                access_policy_version=2,
                access_groups=new_groups,
            ),
            chunk=dataclasses.replace(
                bundle.chunk,
                access_policy_version=2,
                access_groups=new_groups,
            ),
        )
        self.store.write_bundle(updated)
        self.assertEqual(
            self.store.get_assertion_evidence(
                authorized_principal(),
                bundle.assertion.assertion_id,
            ),
            (),
        )
        legal = Principal("bob", bundle.document.tenant_id, new_groups)
        self.assertEqual(
            len(self.store.get_assertion_evidence(legal, bundle.assertion.assertion_id)),
            1,
        )

        with self.assertRaisesRegex(
            ValueError,
            "stale Document access_policy_version",
        ):
            self.store.write_bundle(bundle)
        self.assertEqual(
            self.store.get_assertion_evidence(
                authorized_principal(),
                bundle.assertion.assertion_id,
            ),
            (),
        )
        self.assertEqual(
            len(self.store.get_assertion_evidence(legal, bundle.assertion.assertion_id)),
            1,
        )

        conflicting_groups = frozenset({"executives"})
        conflicting = dataclasses.replace(
            updated,
            document=dataclasses.replace(
                updated.document,
                access_groups=conflicting_groups,
            ),
            chunk=dataclasses.replace(
                updated.chunk,
                access_groups=conflicting_groups,
            ),
        )
        with self.assertRaisesRegex(
            ValueError,
            "conflicting Document state at access_policy_version 2",
        ):
            self.store.write_bundle(conflicting)
        self.assertEqual(
            len(self.store.get_assertion_evidence(legal, bundle.assertion.assertion_id)),
            1,
        )

    def test_same_stable_id_with_changed_immutable_data_is_rejected(self) -> None:
        bundle = make_bundle()
        self.store.write_bundle(bundle)
        changed_chunk = dataclasses.replace(bundle.chunk, section="Changed section")
        changed_bundle = dataclasses.replace(bundle, chunk=changed_chunk)
        with self.assertRaisesRegex(ValueError, "immutable Chunk conflicts"):
            self.store.write_bundle(changed_bundle)

    def test_database_uniqueness_constraints_reject_every_duplicate_id(self) -> None:
        bundle = make_bundle()
        self.store.write_bundle(bundle)
        identities = (
            ("Document", "document_id", bundle.document.document_id),
            ("DocumentVersion", "version_id", bundle.version.version_id),
            ("Chunk", "chunk_id", bundle.chunk.chunk_id),
            ("ChunkEmbedding", "embedding_id", bundle.embedding.embedding_id),
            ("Entity", "entity_id", bundle.entities[0].entity_id),
            ("EntityMention", "mention_id", bundle.mentions[0].mention_id),
            ("Assertion", "assertion_id", bundle.assertion.assertion_id),
        )
        for label, property_name, identifier in identities:
            with self.subTest(label=label), self.assertRaises(
                neo4j.exceptions.ConstraintError
            ):
                self.driver.execute_query(
                    f"CREATE (node:{label} {{{property_name}: $identifier}})",
                    identifier=identifier,
                    database_=self.database,
                )

    def test_business_identity_constraints_reject_alias_ids(self) -> None:
        bundle = make_bundle()
        self.store.write_bundle(bundle)
        conflicting_creates = (
            (
                "CREATE (:Document {document_id: 'other', tenant_id: $tenant, "
                "canonical_uri: $uri})",
                {"tenant": bundle.document.tenant_id, "uri": bundle.document.canonical_uri},
            ),
            (
                "CREATE (:DocumentVersion {version_id: 'other-content', "
                "document_id: $document_id, checksum: $checksum, "
                "original_checksum: $original_checksum})",
                {
                    "document_id": bundle.document.document_id,
                    "checksum": bundle.version.checksum,
                    "original_checksum": bundle.version.original_checksum,
                },
            ),
            (
                "CREATE (:DocumentVersion {version_id: 'other-number', "
                "document_id: $document_id, version_number: $number})",
                {
                    "document_id": bundle.document.document_id,
                    "number": bundle.version.version_number,
                },
            ),
            (
                "CREATE (:Chunk {chunk_id: 'other', version_id: $version_id, "
                "splitter_version: $splitter_version, ordinal: $ordinal})",
                {
                    "version_id": bundle.version.version_id,
                    "splitter_version": bundle.chunk.splitter_version,
                    "ordinal": bundle.chunk.ordinal,
                },
            ),
            (
                "CREATE (:Entity {entity_id: 'other', tenant_id: $tenant, "
                "entity_type: $entity_type, canonical_key: $canonical_key})",
                {
                    "tenant": bundle.entities[0].tenant_id,
                    "entity_type": bundle.entities[0].entity_type,
                    "canonical_key": bundle.entities[0].canonical_key,
                },
            ),
        )
        for query, parameters in conflicting_creates:
            with self.subTest(query=query), self.assertRaises(
                neo4j.exceptions.ConstraintError
            ):
                self.driver.execute_query(
                    query,
                    parameters_=parameters,
                    database_=self.database,
                )

    def test_second_active_version_is_rejected_atomically(self) -> None:
        first = make_bundle()
        second = make_bundle(
            source_text="Apple offers iPhone worldwide.",
            version_number=2,
        )
        self.store.write_bundle(first)
        with self.assertRaisesRegex(ValueError, "active version"):
            self.store.write_bundle(second)
        records, _, _ = self.driver.execute_query(
            "MATCH (:Document {document_id: $document_id})-[:ACTIVE_VERSION]->"
            "(version:DocumentVersion) RETURN count(version) AS count",
            document_id=first.document.document_id,
            database_=self.database,
        )
        self.assertEqual(records[0]["count"], 1)
        records, _, _ = self.driver.execute_query(
            "MATCH (version:DocumentVersion) RETURN count(version) AS count",
            database_=self.database,
        )
        self.assertEqual(records[0]["count"], 1)

    def test_embedding_spaces_are_separate_records(self) -> None:
        bundle = make_bundle()
        self.store.write_bundle(bundle)
        space = embedding_space_id("test", "deterministic-embedding", "v2", 8, "l2")
        embedding = dataclasses.replace(
            bundle.embedding,
            embedding_id=chunk_embedding_id(bundle.chunk.chunk_id, space),
            embedding_space_id=space,
            revision="v2",
            dimensions=8,
        )
        self.store.write_bundle(dataclasses.replace(bundle, embedding=embedding))
        records, _, _ = self.driver.execute_query(
            "MATCH (:Chunk {chunk_id: $chunk_id})-[:HAS_EMBEDDING]->"
            "(embedding:ChunkEmbedding) "
            "RETURN count(embedding) AS count, "
            "count(DISTINCT embedding.embedding_space_id) AS spaces",
            chunk_id=bundle.chunk.chunk_id,
            database_=self.database,
        )
        self.assertEqual(records[0]["count"], 2)
        self.assertEqual(records[0]["spaces"], 2)
        with self.assertRaises(neo4j.exceptions.ConstraintError):
            self.driver.execute_query(
                "CREATE (:ChunkEmbedding {embedding_id: 'forged', "
                "chunk_id: $chunk_id, embedding_space_id: $space_id})",
                chunk_id=bundle.chunk.chunk_id,
                space_id=bundle.embedding.embedding_space_id,
                database_=self.database,
            )

    def test_rebuild_preserves_business_ids_and_evidence(self) -> None:
        bundle = make_bundle()
        stable_ids = (
            bundle.document.document_id,
            bundle.version.version_id,
            bundle.chunk.chunk_id,
            bundle.embedding.embedding_id,
            bundle.assertion.assertion_id,
        )
        self.store.write_bundle(bundle)
        self.driver.execute_query("MATCH (node) DETACH DELETE node", database_=self.database)
        rebuilt = make_bundle()
        self.store.write_bundle(rebuilt)
        self.assertEqual(
            stable_ids,
            (
                rebuilt.document.document_id,
                rebuilt.version.version_id,
                rebuilt.chunk.chunk_id,
                rebuilt.embedding.embedding_id,
                rebuilt.assertion.assertion_id,
            ),
        )
        self.assertEqual(
            len(
                self.store.get_assertion_evidence(
                    authorized_principal(),
                    rebuilt.assertion.assertion_id,
                )
            ),
            1,
        )

    def test_cross_tenant_bundle_is_rejected_before_database_write(self) -> None:
        bundle = make_bundle()
        foreign_embedding = dataclasses.replace(bundle.embedding, tenant_id="other")
        with self.assertRaisesRegex(ValueError, "one tenant"):
            dataclasses.replace(bundle, embedding=foreign_embedding)
        records, _, _ = self.driver.execute_query(
            "MATCH (node) RETURN count(node) AS count",
            database_=self.database,
        )
        self.assertEqual(records[0]["count"], 0)

    def test_entity_mentions_retain_exact_source_surface(self) -> None:
        bundle = make_bundle()
        self.store.write_bundle(bundle)
        records, _, _ = self.driver.execute_query(
            """
            MATCH (entity:Entity)<-[:REFERS_TO]-(mention:EntityMention)
                  -[:IN_CHUNK]->(chunk:Chunk)<-[:HAS_CHUNK]-(version:DocumentVersion)
            RETURN entity.entity_id AS entity_id,
                   mention.surface AS surface,
                   mention.char_start AS char_start,
                   mention.char_end AS char_end,
                   version.normalized_text AS normalized_text
            ORDER BY mention.char_start
            """,
            database_=self.database,
        )
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertEqual(
                record["normalized_text"][record["char_start"] : record["char_end"]],
                record["surface"],
            )


if __name__ == "__main__":
    unittest.main()
