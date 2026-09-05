"""Cold-query, global-identity and provenance checks against disposable Neo4j."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
import ipaddress
import json
import os
from pathlib import Path
import time
import unittest
from urllib.parse import urlparse

import neo4j

from graphrag_prod.api.knowledge import Neo4jKnowledgeOperations
from graphrag_prod.api.knowledge_contracts import AuthoritativeImportRequest, PublicationRequest
from graphrag_prod.construction import (
    ConstructionConfig,
    ConstructionMetadata,
    Neo4jKnowledgeConstructionWorkflow,
)
from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import entity_id
from graphrag_prod.graph.schema import apply_schema, verify_schema
from graphrag_prod.ingestion.pipeline import EmbeddingProfile, Neo4jIncrementalPipeline
from graphrag_prod.knowledge.entity_resolution import (
    EntityResolutionService,
    IdentityPropertyValue,
    Neo4jAuthoritativeEntitySource,
    ResolutionOutcome,
)
from graphrag_prod.knowledge.models import EntityIdentity
from graphrag_prod.ontology import Neo4jTBoxStore, TBoxVersion
from graphrag_prod.playground.industrial_demo import (
    build_authoritative_import,
    get_industrial_demo_kit,
)


NOW = datetime(2026, 9, 6, tzinfo=UTC)
TRANSACTION_TIMEOUT_SECONDS = 30.0


class _TimedReadSession:
    def __init__(self, session: object) -> None:
        self.session = session

    def execute_read(self, work, *args, **kwargs):
        bounded = neo4j.unit_of_work(timeout=TRANSACTION_TIMEOUT_SECONDS)(work)
        return self.session.execute_read(bounded, *args, **kwargs)


class _TimedReadDriver:
    """Use the existing live transaction cap without modifying matcher queries."""

    def __init__(self, driver: object) -> None:
        self.driver = driver

    @contextmanager
    def session(self, **kwargs):
        with self.driver.session(**kwargs) as session:
            yield _TimedReadSession(session)


class Neo4jIdentityResolutionIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        required = ("TEST_NEO4J_URI", "TEST_NEO4J_USER", "TEST_NEO4J_PASSWORD", "TEST_NEO4J_DATABASE")
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
            auth=(os.environ["TEST_NEO4J_USER"], os.environ["TEST_NEO4J_PASSWORD"]),
            max_transaction_retry_time=0,
        )
        cls.driver.verify_connectivity()
        rows, _, _ = cls.driver.execute_query(
            "MATCH (node) RETURN count(node) AS count", database_=cls.database
        )
        if rows[0]["count"]:
            cls.driver.close()
            raise RuntimeError("disposable Neo4j database must start empty")
        apply_schema(cls.driver, cls.database)
        errors = verify_schema(cls.driver, cls.database)
        if errors:
            cls.driver.close()
            raise RuntimeError(f"schema verification failed: {errors}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.driver.close()

    def setUp(self) -> None:
        self.driver.execute_query("MATCH (node) DETACH DELETE node", database_=self.database)
        self.addCleanup(self._clear_disposable_database)
        self.kit = get_industrial_demo_kit()
        self.tenant = "tenant-identity-integration"
        self.principal = Principal(
            "expert:identity",
            self.tenant,
            frozenset({"engineers"}),
            frozenset({"knowledge:construct", "knowledge:import", "knowledge:publish"}),
        )
        draft = TBoxVersion.from_mapping({
            **self.kit["ontology"], "tenant_id": self.tenant, "status": "DRAFT"
        })
        tboxes = Neo4jTBoxStore(self.driver, self.database)
        tboxes.import_version(draft)
        self.tbox = tboxes.publish(self.tenant, draft.tbox_id, expected_active_tbox_id=None)

        def no_extractor(_tbox):
            self.fail("identity integration setup must not construct a model client")

        workflow = Neo4jKnowledgeConstructionWorkflow(
            driver=self.driver,
            database=self.database,
            pipeline=Neo4jIncrementalPipeline(self.driver, self.database, worker_id="identity-integration"),
            embedding_provider=lambda **kwargs: (0.6, 0.8),
            embedding_profile=EmbeddingProfile("offline", "identity-test", "v1", 2, "l2-unit"),
            extractor_factory=no_extractor,
            config=ConstructionConfig(
                extractor_signature="identity-integration:no-model:v1",
                prompt_signature="identity-integration:no-prompt:v1",
            ),
            clock=lambda: NOW,
        )
        source = next(item for item in self.kit["files"] if item["id"] == "authoritative_source")
        uploaded = workflow.run(
            self.principal,
            source["text"].encode("utf-8"),
            ConstructionMetadata(
                operation_key="identity-integration-source",
                canonical_uri=source["metadata"]["canonical_uri"],
                title=source["metadata"]["title"],
                source_name=source["metadata"]["source_name"],
                mime_type="text/plain",
                language="zh",
                tbox_key=self.tbox.key,
                access_groups=self.principal.groups,
                published_at=NOW,
                extraction_mode="SOURCE_ONLY",
            ),
        )
        self.document_id, self.version_id = uploaded.document_id, uploaded.version_id
        self.payload = build_authoritative_import(
            tbox_id=self.tbox.tbox_id,
            document_id=uploaded.document_id,
            version_id=uploaded.version_id,
            source_bytes=source["text"].encode("utf-8"),
            chunks=[{"chunk_id": uploaded.chunks[0].chunk_id, "char_start": 0,
                     "char_end": source["characters"], "text": source["text"]}],
        )
        self.operations = Neo4jKnowledgeOperations(
            driver=self.driver, database=self.database, construction=workflow, clock=lambda: NOW
        )
        self.publication_id = None
        self._publish_import(self.payload)
        self.source = Neo4jAuthoritativeEntitySource(_TimedReadDriver(self.driver), self.database)
        self.identity = (IdentityPropertyValue("EquipmentCode", "STRING", "BC-P-101"),)

    def _clear_disposable_database(self) -> None:
        self.driver.execute_query("MATCH (node) DETACH DELETE node", database_=self.database)

    def _publish_import(self, payload):
        imported = self.operations.authoritative_import(
            self.principal, AuthoritativeImportRequest.model_validate(payload)
        ).payload
        publication = self.operations.publish(self.principal, PublicationRequest(
            approved_revision_ids=imported.revision_ids,
            expected_active_publication_id=self.publication_id,
        )).payload
        self.publication_id = publication.publication_id

    def _match(self, *, principal=None, identity=None):
        return self.source.find_exact_identity_properties(
            principal or self.principal,
            ontology_version_id=self.tbox.tbox_id,
            entity_type="Equipment",
            identity_properties=self.identity if identity is None else identity,
        )

    def test_cold_unique_identity_finishes_within_live_cap_and_homonym_stays_separate(self) -> None:
        # Force compilation of the actual count/fetch queries, including in a
        # full suite that might already have exercised these same query strings.
        self.driver.execute_query("CALL db.clearQueryCaches()", database_=self.database)
        started = time.monotonic()
        match = self._match()
        elapsed = time.monotonic() - started
        observation_dir = os.getenv("GRAPHRAG_EVALUATION_OUTPUT_DIR")
        if observation_dir:
            output = Path(observation_dir) / "identity-resolution-cold.json"
            output.write_text(json.dumps({
                "query_cache_cleared": True,
                "count_and_fetch_seconds": elapsed,
                "transaction_timeout_seconds": TRANSACTION_TIMEOUT_SECONDS,
                "match_count": match.match_count,
            }, sort_keys=True) + "\n", encoding="utf-8")
        self.assertLess(elapsed, TRANSACTION_TIMEOUT_SECONDS, f"cold count/fetch took {elapsed:.3f}s")
        self.assertEqual(match.match_count, 1)
        self.assertEqual(match.target.entity.canonical_key, "equipment-id:bc-p-101")
        self.assertTrue(match.target.evidence)
        for evidence in match.target.evidence:
            self.assertEqual(evidence.document_id, self.document_id)
            self.assertEqual(evidence.version_id, self.version_id)
        key_match = self.source.find_exact_canonical_key(
            self.principal, ontology_version_id=self.tbox.tbox_id,
            entity_type="Equipment", canonical_key="equipment-id:bc-p-101",
        )
        alias_match = self.source.find_exact_governed_alias(
            self.principal, ontology_version_id=self.tbox.tbox_id,
            entity_type="Equipment", candidate_values=("北辰一号循环水泵",),
        )
        self.assertEqual(key_match.target.entity.entity_id, match.target.entity.entity_id)
        self.assertEqual(alias_match.target.entity.entity_id, match.target.entity.entity_id)
        service = EntityResolutionService(self.source, active_tbox=self.tbox)
        candidate = EntityIdentity(
            entity_id=entity_id(self.tenant, "Equipment", "llm-candidate:homonym"),
            tenant_id=self.tenant, entity_type="Equipment",
            canonical_key="llm-candidate:homonym", canonical_name="循环水泵", aliases=(),
        )
        outcome = service.suggest(self.principal, candidate, identity_properties=(
            IdentityPropertyValue("EquipmentCode", "STRING", "BC-P-202"),
        ))
        self.assertEqual(outcome[0].outcome, ResolutionOutcome.NO_MATCH)
        for identity in (
            (IdentityPropertyValue("EquipmentCode", "INTEGER", "BC-P-101"),),
            (IdentityPropertyValue("EquipmentCode", "STRING", "BC-P-101", "kW"),),
        ):
            with self.subTest(identity=identity):
                self.assertEqual(self._match(identity=identity).match_count, 0)

    def test_identity_authority_is_hidden_from_wrong_tenant_or_access_group(self) -> None:
        for principal in (
            replace(self.principal, tenant_id="tenant-outsider"),
            replace(self.principal, groups=frozenset({"public"})),
        ):
            with self.subTest(principal=principal):
                match = self._match(principal=principal)
                self.assertEqual(match.match_count, 0)
                self.assertIsNone(match.target)
        self.assertEqual(self._match().match_count, 1)

    def test_global_uniqueness_counts_entities_instead_of_mentions(self) -> None:
        original = next(item for item in self.payload["mentions"] if item["entity"]["entity_type"] == "Equipment")
        fact = next(item for item in self.payload["assertions"] if item["predicate"] == "EquipmentCode")
        second_mention = deepcopy(original)
        second_mention["source_key"] = "identity-test:second-mention"
        # Use a distinct, exact mention span instead of trying to assign a
        # second record to an already materialized identical navigation mention.
        alias = "北辰一号循环水泵"
        second_evidence = second_mention["evidence"]
        alias_start = second_evidence["char_start"] + second_evidence["quoted_text"].index(alias)
        second_evidence.update(
            char_start=alias_start, char_end=alias_start + len(alias), quoted_text=alias
        )
        second_fact = deepcopy(fact)
        second_fact["source_key"] = "identity-test:second-fact"
        second_fact["subject_mention_source_key"] = second_mention["source_key"]
        # Additional source mentions may support the same canonical entity;
        # keep its one existing identity fact to preserve T-Box cardinality.
        self._publish_import({"ontology_version_id": self.tbox.tbox_id,
                              "mentions": [second_mention], "assertions": []})
        self.assertEqual(self._match().match_count, 1)

        duplicate_mention = deepcopy(second_mention)
        duplicate_mention["source_key"] = "identity-test:duplicate-entity"
        duplicate_mention["entity"]["canonical_key"] = "equipment-id:separate-authority-key"
        duplicate_mention["entity"]["aliases"] = []
        primary_name = original["entity"]["canonical_name"]
        primary_start = original["evidence"]["char_start"] + original["evidence"]["quoted_text"].index(primary_name)
        duplicate_mention["evidence"].update(
            char_start=primary_start,
            char_end=primary_start + len(primary_name),
            quoted_text=primary_name,
        )
        duplicate_fact = deepcopy(second_fact)
        duplicate_fact["source_key"] = "identity-test:duplicate-identity-fact"
        duplicate_fact["subject_mention_source_key"] = duplicate_mention["source_key"]
        self._publish_import({"ontology_version_id": self.tbox.tbox_id,
                              "mentions": [duplicate_mention], "assertions": [duplicate_fact]})
        match = self._match()
        self.assertEqual(match.match_count, 2)
        self.assertIsNone(match.target)
        self.assertIsNone(match.matched_target_value)

    def test_corrupted_identity_evidence_or_inactive_source_cannot_match(self) -> None:
        rows, _, _ = self.driver.execute_query(
            "MATCH (fact:GovernedAssertionRevision {predicate: 'EquipmentCode'}) RETURN properties(fact) AS props",
            database_=self.database,
        )
        original = dict(rows[0]["props"])
        for damaged in (
            {"evidence_text": "not the source text"},
            {"subject_mention_revision_id": "unbound-mention"},
            {"access_groups": ["public"]},
            {"literal_datatype": "INTEGER"},
            {"literal_canonical_unit": "kW"},
        ):
            with self.subTest(damaged=damaged):
                self.driver.execute_query(
                    "MATCH (fact:GovernedAssertionRevision {revision_id: $id}) SET fact += $props",
                    id=original["revision_id"], props=damaged, database_=self.database,
                )
                try:
                    self.assertEqual(self._match().match_count, 0)
                finally:
                    self.driver.execute_query(
                        "MATCH (fact:GovernedAssertionRevision {revision_id: $id}) SET fact = $props",
                        id=original["revision_id"], props=original, database_=self.database,
                    )
        self.driver.execute_query(
            "MATCH (document:Document {document_id: $id})-[active:ACTIVE_VERSION]->() DELETE active",
            id=self.document_id, database_=self.database,
        )
        self.assertEqual(self._match().match_count, 0)


if __name__ == "__main__":
    unittest.main()
