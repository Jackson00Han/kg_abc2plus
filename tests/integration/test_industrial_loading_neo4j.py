"""Disposable Neo4j bootstrap replay, recovery, lease and exact-source isolation."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import ipaddress
import json
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

import neo4j

from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import content_checksum
from graphrag_prod.graph.schema import apply_schema
from graphrag_prod.industrial.corpus import build_corpus
from graphrag_prod.industrial.loading import (
    IndustrialLoadConflict, Neo4jIndustrialLoader, _LoaderLease,
    _prepare_industrial_load, industrial_loader_principal,
)
from graphrag_prod.industrial.normalization import normalize_industrial_source
from graphrag_prod.industrial.provenance import IndustrialProvenanceConflict, Neo4jIndustrialProvenanceStore
from graphrag_prod.industrial.sources import load_source_catalog
from graphrag_prod.ingestion import Neo4jEmbeddingIndexManager, Neo4jIngestionService
from graphrag_prod.knowledge.store import Neo4jKnowledgeStore
from graphrag_prod.knowledge.review import Neo4jKnowledgeReviewService, ReviewRecordKind
from graphrag_prod.knowledge.trust import GovernanceStatus
from tests.fixtures.industrial_loading import NOW, PROFILE, FixtureEmbedder, load_small_fixture, tiny_corpus
from tests.unit.test_pdf_parser import _pdf, _text


ROOT = Path(__file__).resolve().parents[2]


class IndustrialLoadingNeo4jTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.getenv("GRAPHRAG_ALLOW_DISPOSABLE_DB") != "1":
            raise RuntimeError("disposable database opt-in is required")
        uri = os.environ["TEST_NEO4J_URI"]
        if not ipaddress.ip_address(urlsplit(uri).hostname).is_loopback:
            raise RuntimeError("only loopback disposable Neo4j is allowed")
        cls.database = os.environ["TEST_NEO4J_DATABASE"]
        cls.driver = neo4j.GraphDatabase.driver(uri, auth=(os.environ["TEST_NEO4J_USER"], os.environ["TEST_NEO4J_PASSWORD"]))
        cls.driver.verify_connectivity()
        rows, _, _ = cls.driver.execute_query("MATCH (n) RETURN count(n) AS count", database_=cls.database)
        if rows[0]["count"]:
            cls.driver.close()
            raise RuntimeError("disposable Neo4j must start empty")
        apply_schema(cls.driver, cls.database)

    @classmethod
    def tearDownClass(cls):
        cls.driver.close()

    def tearDown(self):
        self.driver.execute_query("MATCH (n) DETACH DELETE n", database_=self.database)

    def loader(self, **kwargs):
        return Neo4jIndustrialLoader(self.driver, self.database, **kwargs)

    def counts(self):
        rows, _, _ = self.driver.execute_query(
            "MATCH (n) RETURN labels(n) AS labels,count(n) AS count ORDER BY labels", database_=self.database)
        return [(row["labels"], row["count"]) for row in rows]

    def test_replay_keeps_graph_publication_embedding_and_provider_calls_identical(self):
        embedder = FixtureEmbedder()
        first = load_small_fixture(self.loader(), embedder)
        counts = self.counts()
        calls = embedder.calls
        second = load_small_fixture(self.loader(), embedder)
        self.assertEqual(first, second)
        self.assertEqual(counts, self.counts())
        self.assertEqual(calls, embedder.calls)
        self.assertEqual(calls, 2)
        manager = Neo4jEmbeddingIndexManager(self.driver, self.database)
        active = manager.active_generation(first["tenant_id"])
        self.assertIsNotNone(active)
        self.assertEqual(active.embedding_space_id, PROFILE.embedding_space_id)
        self.assertTrue(manager.coverage(active.generation_id).complete)
        rows, _, _ = self.driver.execute_query(
            "MATCH (r:GovernedAssertionRevision {governance_status:'PUBLISHED'}) "
            "RETURN r.origin AS origin,r.authority_level AS authority", database_=self.database)
        self.assertEqual({(row["origin"], row["authority"]) for row in rows},
                         {("EXPERT_IMPORT", "AUTHORITATIVE"), ("RULE_DERIVED", "SECONDARY")})

    def test_interrupted_source_and_review_resume_without_duplicate_compute_or_records(self):
        embedder = FixtureEmbedder()
        for phase in ("source", "review"):
            interrupted = False
            def interrupt(current, key):
                nonlocal interrupted
                if current == phase and not interrupted:
                    interrupted = True
                    raise RuntimeError("deliberate isolated interruption")
            with self.assertRaises(RuntimeError):
                load_small_fixture(self.loader(checkpoint=interrupt), embedder)
        result = load_small_fixture(self.loader(), embedder)
        self.assertEqual(result["phase"], "COMPLETE")
        self.assertEqual(embedder.calls, 2)
        self.assertEqual(result["publication_records"], 6)
        rows, _, _ = self.driver.execute_query(
            "MATCH (run:IndustrialCorpusLoad) RETURN run.phase AS phase,run.error_type AS error", database_=self.database)
        self.assertEqual([(row["phase"], row["error"]) for row in rows], [("COMPLETE", None)])

    def test_prepared_embedding_generation_is_reused_after_interrupted_cutover(self):
        embedder = FixtureEmbedder()
        with patch("graphrag_prod.industrial.loading.Neo4jEmbeddingIndexManager.activate",
                   side_effect=RuntimeError("deliberate cutover interruption")):
            with self.assertRaises(RuntimeError):
                load_small_fixture(self.loader(), embedder)
        before, _, _ = self.driver.execute_query(
            "MATCH (g:EmbeddingIndexGeneration) RETURN g.generation_id AS id,g.state AS state",
            database_=self.database)
        self.assertEqual(len(before), 1)
        self.assertEqual(before[0]["state"], "READY")
        result = load_small_fixture(self.loader(), embedder)
        after, _, _ = self.driver.execute_query(
            "MATCH (g:EmbeddingIndexGeneration) RETURN g.generation_id AS id,g.state AS state",
            database_=self.database)
        self.assertEqual([(row["id"], row["state"]) for row in after], [(before[0]["id"], "ACTIVE")])
        self.assertEqual(result["embedding_generation_id"], before[0]["id"])
        self.assertEqual(embedder.calls, 2)

    def test_independent_approval_at_same_revision_is_preserved_and_blocks_bootstrap(self):
        def interrupt(phase, key):
            if phase == "batch":
                raise RuntimeError("pause with declared candidate batch persisted")
        with self.assertRaises(RuntimeError):
            load_small_fixture(self.loader(checkpoint=interrupt), FixtureEmbedder())
        plan = _prepare_industrial_load(tiny_corpus(), PROFILE, ingested_at=NOW)
        candidate = next(record for batch in plan.batches for record in batch.mentions
                         if record.trust.status is GovernanceStatus.CANDIDATE)
        expert = replace(industrial_loader_principal(), principal_id="independent-expert")
        current = Neo4jKnowledgeStore(self.driver, self.database).get_entity_mention(
            expert, candidate.record_id, statuses=(GovernanceStatus.CANDIDATE,))
        self.assertIsNotNone(current)
        outcome = Neo4jKnowledgeReviewService(self.driver, self.database).approve(
            expert, record_kind=ReviewRecordKind.ENTITY_MENTION, record_id=current.record_id,
            expected_revision=1, reviewed_at=datetime.now(UTC), notes="independent human review")
        with self.assertRaisesRegex(IndustrialLoadConflict, "independent reviewer"):
            load_small_fixture(self.loader(), FixtureEmbedder())
        preserved = Neo4jKnowledgeStore(self.driver, self.database).get_entity_mention(
            expert, current.record_id, statuses=(GovernanceStatus.APPROVED,))
        self.assertEqual(preserved.revision_id, outcome.revision_id)
        self.assertEqual(preserved.trust.reviewed_by, "independent-expert")

    def test_representative_corpus_public_loader_and_replay_remain_bounded_and_isolated(self):
        corpus = build_corpus()
        catalog = load_source_catalog(ROOT / "datasets/industrial-v1/sources.json")
        contract = json.loads((ROOT / "contracts/industrial_knowledge.v1.json").read_text())
        embedder = FixtureEmbedder()
        first = self.loader().load(corpus, PROFILE, embedder, catalog=catalog, contract=contract)
        calls = embedder.calls
        self.assertEqual(first["chunks"], corpus.chunk_count)
        self.assertGreaterEqual(first["chunks"], 300)
        self.assertEqual(first["documents"], len(corpus.documents))
        self.assertLessEqual(first["publication_records"], 500)
        self.assertLessEqual(calls, corpus.chunk_count)
        reader = Neo4jIndustrialProvenanceStore(self.driver, self.database)
        rows, _, _ = self.driver.execute_query(
            "MATCH (c:Chunk {tenant_id:$tenant}) RETURN c.chunk_id AS id,c.access_groups AS groups "
            "ORDER BY c.chunk_id LIMIT 40", tenant=corpus.tenant_id, database_=self.database)
        public = Principal("public-reader", corpus.tenant_id, frozenset({"public"}))
        foreign = Principal("foreign-reader", "industrial-other-tenant", frozenset({"public", "engineering", "maintenance"}))
        for row in rows:
            self.assertEqual(reader.for_chunk(public, row["id"]) is not None, "public" in row["groups"])
            self.assertIsNone(reader.for_chunk(foreign, row["id"]))
        second = self.loader().load(corpus, PROFILE, embedder, catalog=catalog, contract=contract)
        self.assertEqual(first, second)
        self.assertEqual(embedder.calls, calls)

    def test_current_source_provenance_obeys_tenant_group_acl_and_tamper_checks(self):
        load_small_fixture(self.loader(), FixtureEmbedder())
        plan = _prepare_industrial_load(tiny_corpus(), PROFILE, ingested_at=NOW)
        source = plan.sources[0]
        chunk_id = source.request.domain_inputs()[2][0].chunk_id
        store = Neo4jIndustrialProvenanceStore(self.driver, self.database)
        principal = industrial_loader_principal()
        metadata = store.for_chunk(principal, chunk_id)
        self.assertEqual(metadata["original_checksum"], source.request.original_checksum)
        self.assertIsNone(store.for_chunk(Principal("maintenance", principal.tenant_id, frozenset({"maintenance"})), chunk_id))
        self.assertIsNone(store.for_chunk(replace(principal, tenant_id="other-tenant"), chunk_id))
        self.driver.execute_query(
            "MATCH (v:DocumentVersion {version_id:$version}) SET v.industrial_provenance_json='{}'",
            version=source.request.version_id, database_=self.database)
        with self.assertRaises(IndustrialProvenanceConflict):
            store.for_chunk(principal, chunk_id)

    def test_acl_changed_under_same_version_never_returns_stale_provenance(self):
        load_small_fixture(self.loader(), FixtureEmbedder())
        request = _prepare_industrial_load(tiny_corpus(), PROFILE, ingested_at=NOW).sources[0].request
        chunk_id = request.domain_inputs()[2][0].chunk_id
        self.driver.execute_query(
            "MATCH (d:Document {document_id:$document})-[:HAS_VERSION]->(v:DocumentVersion)-[:HAS_CHUNK]->(c:Chunk) "
            "SET d.access_policy_version=2,c.access_policy_version=2,d.access_groups=['public'],c.access_groups=['public']",
            document=request.document_id, database_=self.database)
        with self.assertRaises(IndustrialProvenanceConflict):
            Neo4jIndustrialProvenanceStore(self.driver, self.database).for_chunk(industrial_loader_principal(), chunk_id)

    def test_immutable_query_facets_cannot_drift_from_canonical_provenance(self):
        result = load_small_fixture(self.loader(), FixtureEmbedder())
        request = _prepare_industrial_load(tiny_corpus(), PROFILE, ingested_at=NOW).sources[0].request
        rows, _, _ = self.driver.execute_query(
            "MATCH (d:Document {tenant_id:$tenant})-[:ACTIVE_VERSION]->(v:DocumentVersion {tenant_id:$tenant}) "
            "WHERE v.industrial_family='canalis-kt' AND any(g IN d.access_groups WHERE g IN $groups) "
            "RETURN v.industrial_contract_family AS family,v.industrial_asset_keys AS assets",
            tenant=result["tenant_id"], groups=["engineering"], database_=self.database)
        self.assertEqual([(row["family"], row["assets"]) for row in rows], [("CANALIS_KT", [])])
        self.driver.execute_query(
            "MATCH (v:DocumentVersion {version_id:$version}) SET v.industrial_family='evopact-hvx-up24'",
            version=request.version_id, database_=self.database)
        with self.assertRaisesRegex(IndustrialProvenanceConflict, "facets differ"):
            Neo4jIndustrialProvenanceStore(self.driver, self.database).for_chunk(
                industrial_loader_principal(), request.domain_inputs()[2][0].chunk_id)
        with self.assertRaisesRegex(IndustrialProvenanceConflict, "facets differ"):
            load_small_fixture(self.loader(), FixtureEmbedder())

    def test_db_lease_rejects_second_owner_and_allows_expired_crash_recovery(self):
        tenant = tiny_corpus().tenant_id
        with _LoaderLease(self.driver, self.database, tenant) as first:
            with self.assertRaisesRegex(IndustrialLoadConflict, "another industrial load"):
                with _LoaderLease(self.driver, self.database, tenant):
                    self.fail("second owner acquired a live lease")
            self.driver.execute_query(
                "MATCH (l:IndustrialLoaderLease {tenant_id:$tenant}) SET l.expires_at=$past",
                tenant=tenant, past=datetime.now(UTC) - timedelta(seconds=1), database_=self.database)
            with _LoaderLease(self.driver, self.database, tenant):
                with self.assertRaisesRegex(IndustrialLoadConflict, "lease was lost"):
                    first.refresh()
        with _LoaderLease(self.driver, self.database, tenant):
            pass

    def test_original_pdf_map_round_trip_and_source_deletion_completeness(self):
        payload = _pdf((_text("Official parser fixture physical page with 1250 A numeric evidence."),))
        catalog = load_source_catalog(ROOT / "datasets/industrial-v1/sources.json")
        source = replace(catalog.sources[0], sha256=content_checksum(payload), byte_size=len(payload),
                         physical_pages=1, selected_page_ranges=())
        catalog = replace(catalog, sources=(source,))
        artifact = normalize_industrial_source(source, payload, selected_pages=(1,), chunk_chars=400)
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            (cache / f"{source.source_id}.pdf").write_bytes(payload)
            load_small_fixture(self.loader(), FixtureEmbedder(), artifacts=(artifact,), catalog=catalog, original_cache=cache)
            plan = _prepare_industrial_load(tiny_corpus(), PROFILE, ingested_at=NOW,
                normalized_artifacts=(artifact,), catalog=catalog, original_cache=cache)
            request = plan.sources[-1].request
            chunk_id = request.domain_inputs()[2][0].chunk_id
            reader = Neo4jIndustrialProvenanceStore(self.driver, self.database)
            result = reader.for_chunk(industrial_loader_principal(), chunk_id)
            self.assertEqual(result["original_checksum"], content_checksum(payload))
            self.assertEqual(result["selected_pages"], [1])
            self.assertTrue(any(item["kind"] == "page" and item["page_number"] == 1 for item in result["source_locations"]))
            Neo4jIngestionService(self.driver, self.database).delete_document(
                tenant_id=request.tenant_id, document_id=request.document_id, operation_key="delete-official-fixture",
                expected_active_snapshot_id=request.snapshot_id, source_generation=0)
            self.assertIsNone(reader.for_chunk(industrial_loader_principal(), chunk_id))
            rows, _, _ = self.driver.execute_query(
                "MATCH (v:DocumentVersion {version_id:$version}) RETURN count(v) AS count",
                version=request.version_id, database_=self.database)
            self.assertEqual(rows[0]["count"], 0)


if __name__ == "__main__":
    unittest.main()
