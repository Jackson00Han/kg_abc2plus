"""Real disposable Neo4j upload provenance, current ACL and source-only reading."""
from dataclasses import replace
import ipaddress
import os
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

import neo4j

from graphrag_prod.construction import ConstructionConfig, ConstructionConflict, ConstructionMetadata, Neo4jKnowledgeConstructionWorkflow
from graphrag_prod.domain import Principal
from graphrag_prod.graph.schema import apply_schema
from graphrag_prod.industrial.construction import INDUSTRIAL_TENANT, INDUSTRIAL_TBOX_KEY, IndustrialUploadContext, Neo4jIndustrialUploadPolicy, industrial_upload_parser
from graphrag_prod.industrial.loading import Neo4jIndustrialLoader, _LoaderLease
from graphrag_prod.industrial.provenance import IndustrialProvenanceConflict, Neo4jIndustrialProvenanceStore
from graphrag_prod.industrial.source_catalog import Neo4jIndustrialSourceCatalog
from graphrag_prod.ingestion import Neo4jIncrementalPipeline
from graphrag_prod.retrieval.engine import RetrievalUnavailable
from tests.fixtures.industrial_loading import NOW, PROFILE, FixtureEmbedder, tiny_corpus


class IndustrialUploadNeo4jTests(unittest.TestCase):
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

    def setUp(self):
        self.addCleanup(self._clear_fixture)
        self.embedder = FixtureEmbedder()
        corpus = tiny_corpus()
        docs = tuple(replace(doc, asset_keys=("project-asset",)) if doc.key == "field-assets" else doc
                     for doc in corpus.documents)
        corpus = replace(corpus, documents=docs)
        with _LoaderLease(self.driver, self.database, INDUSTRIAL_TENANT) as lease:
            self.baseline = Neo4jIndustrialLoader(self.driver, self.database)._run(
                corpus, PROFILE, self.embedder, normalized_artifacts=(), catalog=None, original_cache=None,
                heartbeat=lease.refresh)
        rows, _, _ = self.driver.execute_query(
            "MATCH (r:GovernedAssertionRevision {tenant_id:$tenant}) RETURN count(r) AS count",
            tenant=INDUSTRIAL_TENANT, database_=self.database)
        self.baseline_assertion_revisions = rows[0]["count"]
        self.principal = Principal("maintenance-uploader", INDUSTRIAL_TENANT, frozenset({"maintenance"}),
            frozenset({"knowledge:construct", "retrieval:read"}))
        self.policy = Neo4jIndustrialUploadPolicy(self.driver, self.database)
        self.workflow = Neo4jKnowledgeConstructionWorkflow(driver=self.driver, database=self.database,
            pipeline=Neo4jIncrementalPipeline(self.driver, self.database, worker_id="industrial-upload-test"),
            embedding_provider=self.embedder, embedding_profile=PROFILE,
            extractor_factory=lambda tbox: self.fail("source-only upload must not initialize an extractor"),
            config=ConstructionConfig("unused-source-only", "unused-source-only"),
            industrial_upload_policy=self.policy, industrial_parser=industrial_upload_parser(), clock=lambda: NOW)
        self.metadata = ConstructionMetadata("industrial-upload-operation-1",
            f"industrial-upload://{INDUSTRIAL_TENANT}/test-report", "User inspection report", "untrusted-user-label",
            "text/plain", "en", INDUSTRIAL_TBOX_KEY, frozenset({"maintenance"}), NOW,
            extraction_mode="SOURCE_ONLY", industrial_context=IndustrialUploadContext("canalis-kt", ("project-asset",)))
        self.text = "ProjectAsset 检查记录：母线槽温升记录尚未确认故障，等待核对实际运行条件与测量结果。🔧\n" * 45
        self.catalog = Neo4jIndustrialSourceCatalog(self.driver, self.database)

    def _clear_fixture(self):
        self.driver.execute_query("MATCH (n) DETACH DELETE n", database_=self.database)

    def test_construct_only_source_roundtrip_replay_adjacent_acl_and_baseline_preservation(self):
        result = self.workflow.run(self.principal, self.text.encode(), self.metadata)
        calls = self.embedder.calls
        replay = self.workflow.run(self.principal, self.text.encode(), self.metadata)
        self.assertEqual(result.job_id, replay.job_id)
        self.assertEqual(self.embedder.calls, calls)
        page = self.catalog.list(self.principal, family="canalis-kt", asset_keys=("project-asset",))
        uploaded = next(source for source in page.sources if source.source_kind == "USER_UPLOAD")
        self.assertEqual(uploaded.version_id, result.version_id)
        self.assertGreater(uploaded.chunk_count, 1)
        self.assertLessEqual(uploaded.chunk_count, 4)
        first = self.catalog.chunk(self.principal, chunk_id=uploaded.first_chunk_id)
        self.assertIsNone(first.previous_chunk_id)
        second = self.catalog.chunk(self.principal, chunk_id=first.next_chunk_id)
        self.assertEqual(second.previous_chunk_id, first.chunk_id)
        self.assertEqual(first.text, self.text[first.char_start:first.char_end])
        self.assertLessEqual(len(first.text), 900)
        self.assertEqual(first.provenance["source_origin"], "USER_PROVIDED_NOT_VERIFIED")
        original = Neo4jIndustrialProvenanceStore(self.driver, self.database).for_chunk(self.principal, first.chunk_id)
        self.assertEqual(original["construction_job_id"], result.job_id)
        self.assertEqual(original["construction_context"]["asset_keys"], ["project-asset"])
        hidden = replace(self.principal, principal_id="engineer", groups=frozenset({"engineering"}))
        self.assertIsNone(self.catalog.chunk(hidden, chunk_id=first.chunk_id))
        self.assertNotIn(result.version_id, {item.version_id for item in self.catalog.list(hidden).sources})
        rows, _, _ = self.driver.execute_query(
            "MATCH (p:KnowledgePublication {tenant_id:$tenant,publication_id:$publication}) "
            "RETURN p.status AS status", tenant=INDUSTRIAL_TENANT,
            publication=self.baseline["publication_id"], database_=self.database)
        self.assertEqual(rows[0]["status"], "ACTIVE")
        rows, _, _ = self.driver.execute_query(
            "MATCH (r:GovernedAssertionRevision {tenant_id:$tenant}) RETURN count(r) AS count",
            tenant=INDUSTRIAL_TENANT, database_=self.database)
        self.assertEqual(rows[0]["count"], self.baseline_assertion_revisions)

    def test_interrupted_provenance_resumes_without_reembedding_and_context_change_fails(self):
        with patch.object(self.policy, "persist", side_effect=IndustrialProvenanceConflict("interrupted provenance")):
            with self.assertRaises(ConstructionConflict):
                self.workflow.run(self.principal, self.text.encode(), self.metadata)
        calls = self.embedder.calls
        self.assertFalse(any(item.source_kind == "USER_UPLOAD" for item in self.catalog.list(self.principal).sources))
        result = self.workflow.run(self.principal, self.text.encode(), self.metadata)
        self.assertEqual(self.embedder.calls, calls)
        changed = replace(self.metadata, industrial_context=IndustrialUploadContext("canalis-kt"))
        with self.assertRaises(ConstructionConflict):
            self.workflow.run(self.principal, self.text.encode(), changed)
        self.assertEqual(self.embedder.calls, calls)
        self.assertTrue(any(item.version_id == result.version_id for item in self.catalog.list(self.principal).sources))

    def test_source_facet_tamper_and_acl_change_fail_closed(self):
        result = self.workflow.run(self.principal, self.text.encode(), self.metadata)
        chunk_id = result.chunks[0].chunk_id
        self.driver.execute_query("MATCH (v:DocumentVersion {tenant_id:$tenant,version_id:$version}) "
            "SET v.industrial_family='evopact-hvx-up24',v.industrial_contract_family='EVOPACT_HVX_UP_TO_24KV'",
            tenant=INDUSTRIAL_TENANT, version=result.version_id, database_=self.database)
        with self.assertRaises(IndustrialProvenanceConflict):
            self.catalog.chunk(self.principal, chunk_id=chunk_id)
        self.driver.execute_query("MATCH (d:Document {tenant_id:$tenant,document_id:$document}) "
            "SET d.access_groups=['engineering']", tenant=INDUSTRIAL_TENANT,
            document=result.document_id, database_=self.database)
        self.assertIsNone(self.catalog.chunk(self.principal, chunk_id=chunk_id))

    def test_target_chunk_revoked_between_reads_is_not_returned_with_visible_neighbors(self):
        from graphrag_prod.industrial import source_catalog as module
        result = self.workflow.run(self.principal, self.text.encode(), self.metadata)
        target = result.chunks[0].chunk_id
        original_run = module._run
        revoked = False
        def revoke_before_summary(session, query, **parameters):
            nonlocal revoked
            if "RETURN count(DISTINCT chunk)" in query and not revoked:
                revoked = True
                self.driver.execute_query(
                    "MATCH (chunk:Chunk {tenant_id:$tenant,chunk_id:$chunk}) SET chunk.access_groups=['engineering']",
                    tenant=INDUSTRIAL_TENANT, chunk=target, database_=self.database)
            return original_run(session, query, **parameters)
        with patch.object(module, "_run", side_effect=revoke_before_summary):
            with self.assertRaises(RetrievalUnavailable):
                self.catalog.chunk(self.principal, chunk_id=target)
        self.assertTrue(revoked)
        self.assertIsNotNone(self.catalog.chunk(self.principal, chunk_id=result.chunks[1].chunk_id))

    def test_inventory_rechecks_all_sources_after_individual_map_validation(self):
        result = self.workflow.run(self.principal, self.text.encode(), self.metadata)
        original_read = self.catalog.provenance.for_chunk
        revoked = False
        def read_then_revoke(principal, chunk_id):
            nonlocal revoked
            metadata = original_read(principal, chunk_id)
            if metadata is not None and metadata["document_id"] == result.document_id:
                revoked = True
                self.driver.execute_query(
                    "MATCH (document:Document {tenant_id:$tenant,document_id:$document}) SET document.access_groups=['engineering']",
                    tenant=INDUSTRIAL_TENANT, document=result.document_id, database_=self.database)
            return metadata
        with patch.object(self.catalog.provenance, "for_chunk", side_effect=read_then_revoke):
            with self.assertRaises(RetrievalUnavailable):
                self.catalog.list(self.principal)
        self.assertTrue(revoked)

    def test_source_page_repair_during_read_cannot_mix_old_map_and_new_label(self):
        result = self.workflow.run(self.principal, self.text.encode(), self.metadata)
        target = result.chunks[0].chunk_id
        original_read = self.catalog.provenance.for_chunk
        def read_then_change_page(principal, chunk_id):
            metadata = original_read(principal, chunk_id)
            self.driver.execute_query(
                "MATCH (chunk:Chunk {tenant_id:$tenant,chunk_id:$chunk}) SET chunk.page_number=3",
                tenant=INDUSTRIAL_TENANT, chunk=target, database_=self.database)
            return metadata
        with patch.object(self.catalog.provenance, "for_chunk", side_effect=read_then_change_page):
            with self.assertRaises(RetrievalUnavailable):
                self.catalog.chunk(self.principal, chunk_id=target)

    def test_publication_time_changed_by_one_nanosecond_during_read_fails_closed(self):
        from graphrag_prod.industrial import source_catalog as module
        result = self.workflow.run(self.principal, self.text.encode(), self.metadata)
        original_run = module._run
        changed = False
        def change_before_summary(session, query, **parameters):
            nonlocal changed
            if "RETURN count(DISTINCT chunk)" in query and not changed:
                changed = True
                self.driver.execute_query(
                    "MATCH (v:DocumentVersion {tenant_id:$tenant,version_id:$version}) "
                    "SET v.published_at=v.published_at+duration({nanoseconds:1})",
                    tenant=INDUSTRIAL_TENANT, version=result.version_id, database_=self.database)
            return original_run(session, query, **parameters)
        with patch.object(module, "_run", side_effect=change_before_summary):
            with self.assertRaises(RetrievalUnavailable):
                self.catalog.chunk(self.principal, chunk_id=result.chunks[0].chunk_id)
        self.assertTrue(changed)


if __name__ == "__main__":
    unittest.main()
