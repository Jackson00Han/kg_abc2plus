"""Real Neo4j checks for T-Box-constrained governed A-Box persistence."""

from __future__ import annotations

import dataclasses
import ipaddress
import os
import unittest
from urllib.parse import urlparse

import neo4j

from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import knowledge_snapshot_id
from graphrag_prod.domain.models import TypedLiteralValue
from graphrag_prod.graph.provenance import Neo4jProvenanceStore
from graphrag_prod.graph.schema import apply_schema, verify_schema
from graphrag_prod.knowledge import (
    GovernanceStatus,
    KnowledgeConflict,
    KnowledgeEvidenceError,
    KnowledgeSchemaError,
    Neo4jKnowledgeStore,
)
from graphrag_prod.ontology import (
    Cardinality,
    EntityTypeDefinition,
    Neo4jTBoxStore,
    PropertyDataType,
    PropertyDefinition,
    RelationshipTypeDefinition,
    TBoxStatus,
    TBoxVersion,
)
from tests.fixtures.domain import make_bundle
from tests.fixtures.knowledge import make_knowledge_batch


class Neo4jKnowledgeStoreIntegrationTests(unittest.TestCase):
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
        errors = verify_schema(cls.driver, cls.database)
        if errors:
            cls.driver.close()
            raise RuntimeError(f"schema verification failed: {errors}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.driver.close()

    def setUp(self) -> None:
        self.driver.execute_query(
            "MATCH (node) DETACH DELETE node",
            database_=self.database,
        )
        self.tenant_id = "tenant-knowledge"
        tbox = TBoxVersion(
            tenant_id=self.tenant_id,
            key="company",
            version=1,
            status=TBoxStatus.DRAFT,
            entity_types=(
                EntityTypeDefinition(
                    "Company",
                    ("ticker", "llm-candidate"),
                    properties=(
                        PropertyDefinition(
                            "DISPLAY_NAME",
                            PropertyDataType.STRING,
                            False,
                            Cardinality.ZERO_OR_ONE,
                        ),
                    ),
                ),
                EntityTypeDefinition(
                    "Product",
                    ("apple-product", "llm-candidate"),
                ),
            ),
            relationship_types=(
                RelationshipTypeDefinition(
                    "OFFERS",
                    ("Company",),
                    ("Product",),
                ),
            ),
        )
        tbox_store = Neo4jTBoxStore(self.driver, self.database)
        tbox_store.import_version(tbox)
        tbox_store.publish(
            self.tenant_id,
            tbox.tbox_id,
            expected_active_tbox_id=None,
        )
        self.tbox_id = tbox.tbox_id
        self.bundle = make_bundle(tenant_id=self.tenant_id)
        Neo4jProvenanceStore(self.driver, self.database).write_bundle(self.bundle)
        profile_id = "knowledge-store-integration:v1"
        self.snapshot_id = knowledge_snapshot_id(
            self.bundle.version.version_id,
            profile_id,
        )
        self.driver.execute_query(
            """
            MATCH (document:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })-[:ACTIVE_VERSION]->(version:DocumentVersion {
                tenant_id: $tenant_id,
                version_id: $version_id
            })-[:HAS_CHUNK]->(chunk:Chunk {
                tenant_id: $tenant_id,
                chunk_id: $chunk_id
            })
            CREATE (snapshot:KnowledgeSnapshot {
                snapshot_id: $snapshot_id,
                tenant_id: $tenant_id,
                document_id: $document_id,
                version_id: $version_id,
                profile_id: $profile_id,
                build_state: 'PUBLISHED'
            })
            MERGE (document)-[:ACTIVE_SNAPSHOT]->(snapshot)
            MERGE (snapshot)-[:OF_VERSION]->(version)
            MERGE (snapshot)-[:INCLUDES_CHUNK]->(chunk)
            """,
            tenant_id=self.tenant_id,
            document_id=self.bundle.document.document_id,
            version_id=self.bundle.version.version_id,
            chunk_id=self.bundle.chunk.chunk_id,
            snapshot_id=self.snapshot_id,
            profile_id=profile_id,
            database_=self.database,
        )
        self.store = Neo4jKnowledgeStore(self.driver, self.database)
        self.principal = Principal(
            "expert:alice",
            self.tenant_id,
            frozenset({"finance-readers"}),
        )

    def tearDown(self) -> None:
        self.driver.execute_query(
            "MATCH (node) DETACH DELETE node",
            database_=self.database,
        )

    def test_authoritative_abox_round_trips_through_exact_source_and_acl(self) -> None:
        batch = make_knowledge_batch(
            tenant_id=self.tenant_id,
            ontology_version_id=self.tbox_id,
        )
        result = self.store.import_authoritative(batch)
        self.assertEqual(result.mention_count, 2)
        self.assertEqual(result.assertion_count, 1)
        self.assertEqual(
            self.store.list_entity_mentions(self.principal),
            tuple(sorted(batch.mentions, key=lambda item: item.record_id)),
        )
        self.assertEqual(self.store.list_assertions(self.principal), batch.assertions)

        wrong_group = Principal(
            "mallory",
            self.tenant_id,
            frozenset({"legal"}),
        )
        wrong_tenant = Principal(
            "mallory",
            "other-tenant",
            frozenset({"finance-readers"}),
        )
        self.assertEqual(self.store.list_entity_mentions(wrong_group), ())
        self.assertEqual(self.store.list_assertions(wrong_tenant), ())
        with self.assertRaises(KnowledgeConflict):
            self.store.import_authoritative(batch)

    def test_llm_candidates_are_not_canonical_graph_or_default_read_data(self) -> None:
        batch = make_knowledge_batch(
            authoritative=False,
            tenant_id=self.tenant_id,
            ontology_version_id=self.tbox_id,
        )
        before, _, _ = self.driver.execute_query(
            "MATCH (entity:Entity) RETURN count(entity) AS count",
            database_=self.database,
        )
        self.store.persist_llm_candidates(batch)
        after, _, _ = self.driver.execute_query(
            "MATCH (entity:Entity) RETURN count(entity) AS count",
            database_=self.database,
        )
        graph_links, _, _ = self.driver.execute_query(
            """
            MATCH (revision)
            WHERE revision:GovernedEntityMentionRevision
               OR revision:GovernedAssertionRevision
            OPTIONAL MATCH (revision)-[link:REFERS_TO|SUBJECT|OBJECT]->(:Entity)
            RETURN count(link) AS count
            """,
            database_=self.database,
        )
        self.assertEqual(before[0]["count"], after[0]["count"])
        self.assertEqual(graph_links[0]["count"], 0)
        self.assertEqual(self.store.list_entity_mentions(self.principal), ())
        self.assertEqual(
            self.store.list_entity_mentions(
                self.principal,
                statuses=(GovernanceStatus.CANDIDATE,),
            ),
            tuple(sorted(batch.mentions, key=lambda item: item.record_id)),
        )

    def test_evidence_mismatch_rolls_back_before_record_heads_exist(self) -> None:
        batch = make_knowledge_batch(
            tenant_id=self.tenant_id,
            ontology_version_id=self.tbox_id,
        )
        mention = batch.mentions[0]
        changed_evidence = dataclasses.replace(
            mention.evidence,
            quoted_text="X" * len(mention.evidence.quoted_text),
        )
        changed_mention = dataclasses.replace(mention, evidence=changed_evidence)
        invalid = dataclasses.replace(
            batch,
            mentions=(changed_mention, *batch.mentions[1:]),
        )
        with self.assertRaises(KnowledgeEvidenceError):
            self.store.import_authoritative(invalid)
        records, _, _ = self.driver.execute_query(
            "MATCH (head:KnowledgeRecordHead) RETURN count(head) AS count",
            database_=self.database,
        )
        self.assertEqual(records[0]["count"], 0)

    def test_typed_literal_round_trips_as_flat_auditable_properties(self) -> None:
        batch = make_knowledge_batch(
            tenant_id=self.tenant_id,
            ontology_version_id=self.tbox_id,
        )
        source = batch.assertions[0]
        literal = TypedLiteralValue(
            datatype="STRING",
            typed_value="Apple",
            raw_value="Apple",
            canonical_value="Apple",
        )
        assertion = dataclasses.replace(
            source,
            predicate="DISPLAY_NAME",
            object_entity=None,
            object_mention_revision_id=None,
            literal_value="Apple",
            literal_semantics=literal,
        )
        typed_batch = dataclasses.replace(batch, assertions=(assertion,))

        self.store.import_authoritative(typed_batch)

        returned = self.store.get_assertion(self.principal, assertion.record_id)
        self.assertEqual(returned, assertion)
        records, _, _ = self.driver.execute_query(
            """
            MATCH (revision:GovernedAssertionRevision {
                tenant_id: $tenant_id,
                revision_id: $revision_id
            })
            RETURN revision.literal_datatype AS datatype,
                   revision.literal_typed_value AS typed_value,
                   revision.literal_raw_value AS raw_value,
                   revision.literal_canonical_value AS canonical_value
            """,
            tenant_id=self.tenant_id,
            revision_id=assertion.revision_id,
            database_=self.database,
        )
        self.assertEqual(
            dict(records[0]),
            {
                "datatype": "STRING",
                "typed_value": "Apple",
                "raw_value": "Apple",
                "canonical_value": "Apple",
            },
        )

    def test_untyped_literal_cannot_use_legacy_decoder_to_bypass_new_write(
        self,
    ) -> None:
        batch = make_knowledge_batch(
            tenant_id=self.tenant_id,
            ontology_version_id=self.tbox_id,
        )
        source = batch.assertions[0]
        assertion = dataclasses.replace(
            source,
            predicate="DISPLAY_NAME",
            object_entity=None,
            object_mention_revision_id=None,
            literal_value="Apple",
            literal_semantics=None,
        )
        untyped_batch = dataclasses.replace(batch, assertions=(assertion,))

        with self.assertRaisesRegex(KnowledgeSchemaError, "typed semantics"):
            self.store.import_authoritative(untyped_batch)

        records, _, _ = self.driver.execute_query(
            "MATCH (head:KnowledgeRecordHead) RETURN count(head) AS count",
            database_=self.database,
        )
        self.assertEqual(records[0]["count"], 0)

    def test_historical_chunk_cannot_be_imported_as_authoritative_evidence(
        self,
    ) -> None:
        self.driver.execute_query(
            """
            MATCH (:Document {
                tenant_id: $tenant_id,
                document_id: $document_id
            })-[active:ACTIVE_SNAPSHOT]->(:KnowledgeSnapshot {
                tenant_id: $tenant_id,
                snapshot_id: $snapshot_id
            })
            DELETE active
            """,
            tenant_id=self.tenant_id,
            document_id=self.bundle.document.document_id,
            snapshot_id=self.snapshot_id,
            database_=self.database,
        )
        batch = make_knowledge_batch(
            tenant_id=self.tenant_id,
            ontology_version_id=self.tbox_id,
        )
        with self.assertRaises(KnowledgeEvidenceError):
            self.store.import_authoritative(batch)
        records, _, _ = self.driver.execute_query(
            "MATCH (head:KnowledgeRecordHead) RETURN count(head) AS count",
            database_=self.database,
        )
        self.assertEqual(records[0]["count"], 0)


if __name__ == "__main__":
    unittest.main()
