"""Cumulative publication capacity through real evidence and publication services.

The only source sentence is from the authorized fictitious pump kit. Repeated
occurrences create distinct exact evidence ranges, not invented source facts.
No external model or user database is used.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import ipaddress
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from urllib.parse import urlparse
from uuid import uuid4

from neo4j import GraphDatabase

from graphrag_prod.construction import BoundedDocumentParser
from graphrag_prod.construction.literals import TBoxLiteralNormalizer
from graphrag_prod.domain import GraphPipelineProfile, Principal
from graphrag_prod.domain.ids import (
    chunk_embedding_id, chunk_id, content_checksum, document_id, entity_id, pipeline_profile_id, version_id,
)
from graphrag_prod.graph.schema import apply_schema, verify_schema
from graphrag_prod.ingestion import IngestionPlan, Neo4jEmbeddingIndexManager, Neo4jIngestionService
from graphrag_prod.ingestion.models import default_artifact_input_hash
from graphrag_prod.knowledge.models import (
    ABoxRecordBatch, AssertionRecord, EntityIdentity, EntityMentionRecord,
    EvidenceReference, RecordRevision, authoritative_import_trust, knowledge_record_id,
)
from graphrag_prod.knowledge.review import Neo4jKnowledgePublicationService
from graphrag_prod.knowledge.source_library import Neo4jSourceLibrary
from graphrag_prod.knowledge.store import Neo4jKnowledgeStore
from graphrag_prod.ontology import Neo4jTBoxStore, TBoxVersion
from graphrag_prod.retrieval import Neo4jRetrievalEngine, RetrievalLimits, RetrievalRequest, VersionFilter
from tests.fixtures.pump_ingestion import make_plan


PACKAGE = Path(__file__).resolve().parents[2] / "src/graphrag_prod/playground/static/industrial-demo-v1"
NOW = datetime(2026, 9, 13, tzinfo=UTC)


class PublicationCapacityNeo4jTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("GRAPHRAG_ALLOW_DISPOSABLE_DB") != "1":
            raise RuntimeError("disposable database opt-in is required")
        uri = os.environ["TEST_NEO4J_URI"]
        if not ipaddress.ip_address(urlparse(uri).hostname).is_loopback:
            raise RuntimeError("a loopback disposable database is required")
        cls.database = os.environ["TEST_NEO4J_DATABASE"]
        cls.driver = GraphDatabase.driver(
            uri, auth=(os.environ["TEST_NEO4J_USER"], os.environ["TEST_NEO4J_PASSWORD"]),
            notifications_min_severity="OFF", max_transaction_retry_time=0,
        )
        cls.driver.verify_connectivity()
        apply_schema(cls.driver, cls.database)
        cls.driver.execute_query("CALL db.awaitIndexes(60)", database_=cls.database)
        if verify_schema(cls.driver, cls.database):
            raise RuntimeError("schema verification failed")

    @classmethod
    def tearDownClass(cls):
        cls.driver.close()

    def setUp(self):
        self.tenant = "pump-publication-capacity-" + uuid4().hex
        self.principal = Principal(
            "capacity-reviewer", self.tenant, frozenset({"members"}),
            frozenset({"knowledge:publish", "knowledge:graph:read", "retrieval:read"}),
        )
        self.timings = {}
        definition = json.loads((PACKAGE / "ontology.json").read_text())
        self.tbox = TBoxVersion.from_mapping({**definition, "tenant_id": self.tenant, "status": "DRAFT"})
        tboxes = Neo4jTBoxStore(self.driver, self.database)
        tboxes.import_version(self.tbox)
        tboxes.publish(self.tenant, self.tbox.tbox_id, expected_active_tbox_id=None)
        self.ingestion = Neo4jIngestionService(
            self.driver, self.database, worker_id="pump-capacity-fixture",
            clock=SimpleNamespace(now=lambda: NOW),
        )
        self.store = Neo4jKnowledgeStore(self.driver, self.database)
        self.publications = Neo4jKnowledgePublicationService(self.driver, self.database)
        self.sources = Neo4jSourceLibrary(self.driver, self.database)
        self.engine = Neo4jRetrievalEngine(self.driver, self.database)

    def tearDown(self):
        self.driver.execute_query(
            "MATCH (n {tenant_id:$tenant}) DETACH DELETE n",
            tenant=self.tenant, database_=self.database,
        )
        print(json.dumps({"test": "publication-capacity-300-plus-279", "seconds": self.timings}, sort_keys=True), flush=True)

    def timed(self, name, operation):
        print(json.dumps({"phase": name, "status": "started"}), flush=True)
        start = time.monotonic()
        try:
            return operation()
        finally:
            self.timings[name] = round(time.monotonic() - start, 3)
            print(json.dumps({"phase": name, "seconds": self.timings[name]}), flush=True)

    def add_source(self, name, repetitions):
        line = next(line for line in (PACKAGE / "authoritative_source.txt").read_text().splitlines()
                    if line.startswith("设备：")) + "\n"
        text = line * repetitions
        parsed = BoundedDocumentParser().parse(text.encode(), mime_type="text/plain")
        base_plan = make_plan(tenant_id=self.tenant)
        base = base_plan.bundles[0]
        uri = "urn:test:pump-publication-capacity:" + name
        doc = replace(base.document, document_id=document_id(self.tenant, uri), canonical_uri=uri,
                      title="循环水泵容量验证来源 " + name)
        ver = version_id(doc.document_id, parsed.normalized_checksum, parsed.original_checksum)
        version = replace(base.version, version_id=ver, document_id=doc.document_id,
                          normalized_text=parsed.normalized_text, checksum=parsed.normalized_checksum,
                          original_checksum=parsed.original_checksum)
        bundles = []
        for seed in parsed.chunks:
            checksum = content_checksum(seed.text)
            key = chunk_id(ver, parsed.splitter_signature, seed.ordinal, seed.char_start, seed.char_end, checksum)
            chunk = replace(base.chunk, chunk_id=key, version_id=ver, document_id=doc.document_id,
                            text=seed.text, checksum=checksum, ordinal=seed.ordinal,
                            char_start=seed.char_start, char_end=seed.char_end,
                            splitter_version=parsed.splitter_signature)
            embedding = replace(base.embedding, chunk_id=key,
                                embedding_id=chunk_embedding_id(key, base.embedding.embedding_space_id))
            bundles.append(replace(base, document=doc, version=version, chunk=chunk, embedding=embedding))
        signatures = (base_plan.profile.normalizer_signature, parsed.splitter_signature,
                      base_plan.profile.extractor_signature, base_plan.profile.prompt_signature,
                      base_plan.profile.schema_signature, base_plan.profile.code_signature)
        profile = GraphPipelineProfile(pipeline_profile_id(*signatures), *signatures)
        plan = IngestionPlan.build(
            operation_key="capacity-source:" + ver, profile=profile,
            governance_policy=base_plan.governance_policy, bundles=tuple(bundles),
            expected_active_snapshot_id=None, source_generation=0,
            artifact_input_hashes={b.chunk.chunk_id: default_artifact_input_hash(b) for b in bundles},
            created_at=NOW,
        )
        self.ingestion.ingest(plan)
        canonical_key = "equipment-id:BC-P-101"
        entity = EntityIdentity(entity_id(self.tenant, "Equipment", canonical_key), self.tenant,
                                "Equipment", canonical_key, "循环水泵", ())
        trust = authoritative_import_trust(ontology_version_id=self.tbox.tbox_id,
                                           imported_by=self.principal.principal_id, imported_at=NOW)
        definitions = {p.name: p for e in self.tbox.entity_types if e.name == "Equipment" for p in e.properties}
        normalizer = TBoxLiteralNormalizer()
        literals = {
            "EquipmentCode": normalizer.normalize(definitions["EquipmentCode"], raw_value="BC-P-101", raw_unit=None,
                                                   valid_from=None, valid_to=None, observed_at=None),
            "RatedPower": normalizer.normalize(definitions["RatedPower"], raw_value="37.5", raw_unit="kW",
                                                valid_from=None, valid_to=None, observed_at=None),
        }
        mentions, assertions = [], []
        for ordinal in range(repetitions):
            start, end = ordinal * len(line), (ordinal + 1) * len(line)
            chunk = next(b.chunk for b in bundles if b.chunk.char_start <= start and end <= b.chunk.char_end)
            evidence = EvidenceReference(self.tenant, doc.document_id, ver, chunk.chunk_id, start, end, line,
                                        chunk.access_policy_id, chunk.access_policy_version, chunk.access_groups)
            mention_start = start + line.index(entity.canonical_name)
            mention_evidence = replace(evidence, char_start=mention_start,
                                       char_end=mention_start + len(entity.canonical_name),
                                       quoted_text=entity.canonical_name)
            origin = f"{ver}:{ordinal}"
            mention = EntityMentionRecord(
                RecordRevision.next(knowledge_record_id(self.tenant, "ENTITY_MENTION", origin), 0),
                self.tenant, entity, mention_evidence, 1.0, trust, NOW,
            )
            mentions.append(mention)
            for predicate, literal in literals.items():
                assertions.append(AssertionRecord(
                    RecordRevision.next(knowledge_record_id(self.tenant, "ASSERTION", origin + ":" + predicate), 0),
                    self.tenant, entity, predicate, evidence, mention.revision_id, 1.0, trust, NOW,
                    literal_value=literal.raw_value, literal_semantics=literal,
                ))
        batch = ABoxRecordBatch(self.tenant, tuple(mentions), tuple(assertions))
        written = self.store.import_authoritative(batch)
        self.assertEqual(written.mention_count + written.assertion_count, repetitions * 3)
        return SimpleNamespace(document=doc, version=version, snapshot_id=plan.snapshot.snapshot_id,
                               chunks=tuple(b.chunk for b in bundles), embedding=bundles[0].embedding,
                               revision_ids=tuple(r.revision_id for r in (*mentions, *assertions)))

    def test_300_then_279_records_preserve_both_sources_in_one_active_publication(self):
        first_source = self.timed("prepare_300", lambda: self.add_source("first", 100))
        second_source = self.timed("prepare_279", lambda: self.add_source("second", 93))
        index = Neo4jEmbeddingIndexManager(self.driver, self.database)
        generation = self.timed("prepare_embedding_index", lambda: index.prepare(
            tenant_id=self.tenant, embedding_profile=first_source.embedding, generation_version=1))
        index.activate(generation.generation_id, expected_active_generation_id=None)

        all_ids = (*first_source.revision_ids, *second_source.revision_ids)
        self.assertEqual(len(all_ids), 579)
        with self.assertRaisesRegex(ValueError, "exceeds"):
            self.publications.publish(self.principal, all_ids[:501], expected_active_publication_id=None,
                                      published_at=NOW + timedelta(minutes=1))
        self.assertIsNone(self.publications.active(self.principal))

        def publish(source, expected, number):
            options = dict(expected_active_publication_id=expected, published_at=NOW + timedelta(minutes=number))
            preview = self.timed(f"preview_{len(source.revision_ids)}", lambda: self.publications.publish(
                self.principal, source.revision_ids, preview_only=True, **options))
            self.assertEqual(len(preview["records_after"]), 300 if expected is None else 579)
            publication = self.timed(f"publish_{len(source.revision_ids)}", lambda: self.publications.publish(
                self.principal, source.revision_ids, expected_preview_hash=preview["preview_hash"], **options))
            self.assertEqual(publication.manifest_hash, preview["manifest_hash"])
            return publication

        first = publish(first_source, None, 2)
        self.assertEqual(len(first.published_revision_ids), 300)
        second = publish(second_source, first.publication_id, 3)
        self.assertEqual(len(second.published_revision_ids), 579)
        self.assertEqual(second.source_document_count, 2)
        self.assertEqual(second.source_chunk_count, len(first_source.chunks) + len(second_source.chunks))
        self.assertTrue(set(first.published_revision_ids) <= set(second.published_revision_ids))
        active = self.timed("active_manifest_read", lambda: self.publications.active(self.principal))
        self.assertEqual(active.publication_id, second.publication_id)
        self.assertEqual(len(active.published_revision_ids), 579)
        sources = self.timed("formal_source_list", lambda: self.sources.read(self.principal))
        self.assertEqual({item["document_id"] for item in sources["items"]},
                         {first_source.document.document_id, second_source.document.document_id})
        for name, source in (("first", first_source), ("second", second_source)):
            result = self.timed("formal_retrieval_" + name, lambda: self.engine.retrieve(RetrievalRequest(
                query_text="循环水泵 BC-P-101 37.5 kW", query_vector=source.embedding.vector,
                query_embedding_space_id=source.embedding.embedding_space_id, principal=self.principal,
                limits=RetrievalLimits(top_k=2, anchor_k=2),
                version_filter=VersionFilter(document_ids=frozenset({source.document.document_id})),
            )))
            self.assertTrue(result.chunks)
            self.assertEqual(result.trace.knowledge_publication_id, second.publication_id)
            self.assertEqual({item.citation.document_id for item in result.chunks}, {source.document.document_id})
            for item in result.chunks:
                self.assertEqual(source.version.normalized_text[item.citation.char_start:item.citation.char_end], item.text)


def load_tests(loader, standard_tests, pattern):
    # These related checks share one disposable server and run sequentially.
    # The member matrix remains separately runnable from its own test module.
    from tests.integration.test_publication_member_guard_neo4j import PublicationMemberGuardNeo4jTests
    standard_tests.addTests(loader.loadTestsFromTestCase(PublicationMemberGuardNeo4jTests))
    return standard_tests


if __name__ == "__main__":
    unittest.main()
