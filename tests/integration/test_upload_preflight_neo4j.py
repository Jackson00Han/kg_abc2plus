"""Real Neo4j upload boundaries and leases using only the permitted pump kit."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import ipaddress
import os
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
import unittest
from urllib.parse import urlparse
from uuid import uuid4

from neo4j import GraphDatabase

from graphrag_prod.construction.preflight import Neo4jUploadPreflight
from graphrag_prod.construction.upload_guard import Neo4jUploadGuard, UploadAlreadyRunning
from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import content_checksum, document_id, version_id, chunk_id, chunk_embedding_id
from graphrag_prod.graph.schema import apply_schema, verify_schema
from graphrag_prod.ingestion import IngestionPlan, Neo4jIngestionService
from graphrag_prod.ingestion.models import default_artifact_input_hash
from tests.fixtures.pump_ingestion import make_plan


KIT = Path(__file__).resolve().parents[2] / "src/graphrag_prod/playground/static/industrial-demo-v1"
PUMP = (KIT / "authoritative_source.txt").read_text(encoding="utf-8")
URI = "https://example.test/industrial-demo-v1/authoritative_source.txt"


class UploadPreflightNeo4jTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.getenv("GRAPHRAG_ALLOW_DISPOSABLE_DB") != "1":
            raise RuntimeError("disposable database opt-in required")
        uri = os.environ["TEST_NEO4J_URI"]
        if not ipaddress.ip_address(urlparse(uri).hostname).is_loopback:
            raise RuntimeError("loopback disposable database required")
        cls.database = os.environ["TEST_NEO4J_DATABASE"]
        cls.driver = GraphDatabase.driver(
            uri, auth=(os.environ["TEST_NEO4J_USER"], os.environ["TEST_NEO4J_PASSWORD"]),
            notifications_min_severity="OFF", max_transaction_retry_time=0,
        )
        cls.driver.verify_connectivity()
        apply_schema(cls.driver, cls.database)
        if errors := verify_schema(cls.driver, cls.database):
            raise RuntimeError(f"schema verification failed: {errors}")

    @classmethod
    def tearDownClass(cls):
        cls.driver.close()

    def setUp(self):
        self.tenant = "pump-upload-preflight-" + uuid4().hex
        self.tenants = {self.tenant}
        self.principal = Principal("reviewer", self.tenant, frozenset({"members"}),
                                   frozenset({"knowledge:construct"}))
        self.preflight = Neo4jUploadPreflight(self.driver, self.database)
        self.guard = Neo4jUploadGuard(self.driver, self.database)

    def tearDown(self):
        # Shared disposable server, but never erase another suite's records.
        self.query("MATCH (n) WHERE n.tenant_id IN $tenants DETACH DELETE n",
                   tenants=sorted(self.tenants))

    def query(self, query, **parameters):
        return self.driver.execute_query(query, parameters_=parameters, database_=self.database)[0]

    def source(self, *, text=PUMP, uri=URI, groups=frozenset({"members"}), previous=None, tenant=None):
        tenant = tenant or self.tenant
        self.tenants.add(tenant)
        base_plan = make_plan(tenant_id=tenant)
        base = base_plan.bundles[0]
        checksum = content_checksum(text)
        doc = replace(base.document, document_id=document_id(tenant, uri), canonical_uri=uri,
                      title="循环水泵设备台账", access_groups=groups,
                      access_policy_id=tenant + ":" + ":".join(sorted(groups)))
        version = replace(base.version, version_id=version_id(doc.document_id, checksum, checksum),
                          document_id=doc.document_id, checksum=checksum, original_checksum=checksum,
                          normalized_text=text, version_number=previous.version.version_number + 1 if previous else 1)
        bundles = []
        middle = len(text) // 2
        for ordinal, (start, end) in enumerate(((0, middle), (middle, len(text)))):
            piece = text[start:end]
            key = chunk_id(version.version_id, base.chunk.splitter_version, ordinal, start, end,
                           content_checksum(piece))
            chunk = replace(base.chunk, chunk_id=key, version_id=version.version_id,
                            document_id=doc.document_id, access_groups=groups,
                            access_policy_id=doc.access_policy_id, ordinal=ordinal, text=piece,
                            checksum=content_checksum(piece), char_start=start, char_end=end)
            embedding = replace(base.embedding, chunk_id=key,
                                embedding_id=chunk_embedding_id(key, base.embedding.embedding_space_id))
            bundles.append(replace(base, document=doc, version=version, chunk=chunk, embedding=embedding))
        plan = IngestionPlan.build(
            operation_key="pump-upload-fixture-" + uuid4().hex, profile=base_plan.profile,
            governance_policy=base_plan.governance_policy, bundles=tuple(bundles),
            expected_active_snapshot_id=previous.snapshot_id if previous else None, source_generation=0,
            artifact_input_hashes={b.chunk.chunk_id: default_artifact_input_hash(b) for b in bundles},
            created_at=base.version.ingested_at,
        )
        Neo4jIngestionService(self.driver, self.database).ingest(plan)
        return SimpleNamespace(document=doc, version=version, snapshot_id=plan.snapshot.snapshot_id,
                               chunks=tuple(bundle.chunk for bundle in bundles))

    def check(self, text=PUMP, principal=None):
        return self.preflight.check(principal or self.principal, text.encode(), canonical_uri=URI)

    def test_exact_renamed_normalized_and_near_uploads_use_current_source(self):
        source = self.source()
        result = self.preflight.check(self.principal, PUMP.replace("\n", "\r\n").encode(),
                                      canonical_uri=URI + ".renamed")
        self.assertEqual([item["document_id"] for item in result["exact_matches"]], [source.document.document_id])
        result = self.check(PUMP.replace("37.5 kW", "38.5 kW"))
        self.assertEqual(len(result["similar_matches"]), 1)
        self.assertIn("37.5 kW", result["similar_matches"][0]["difference"]["before"])
        self.assertIn("38.5 kW", result["similar_matches"][0]["difference"]["after"])

    def test_latest_upload_does_not_hide_old_source_pinned_by_active_publication(self):
        old = self.source()
        new = self.source(text=PUMP.replace("37.5 kW", "38.5 kW"), previous=old)
        self.assertFalse(self.check()["exact_matches"])
        self.query("""MATCH (s:KnowledgeSnapshot {snapshot_id:$snapshot})
            CREATE (state:KnowledgePublicationState {tenant_id:$tenant})
            CREATE (p:KnowledgePublication {tenant_id:$tenant,publication_id:$publication,status:'ACTIVE',generation:1})
            CREATE (state)-[:ACTIVE_KNOWLEDGE_PUBLICATION]->(p)
            CREATE (p)-[:USES_KNOWLEDGE_SNAPSHOT]->(s)""",
                   tenant=self.tenant, publication=uuid4().hex, snapshot=old.snapshot_id)
        self.assertEqual([item["version_id"] for item in self.check()["exact_matches"]], [old.version.version_id])
        self.query("MATCH (p:KnowledgePublication {tenant_id:$tenant}) SET p.status='RETIRED'", tenant=self.tenant)
        self.assertFalse(self.check()["exact_matches"])
        self.assertEqual(self.check()["similar_matches"][0]["version_id"], new.version.version_id)

    def test_cross_tenant_private_sources_and_one_inaccessible_chunk_never_leak(self):
        self.source(tenant=self.tenant + "-other")
        self.source(uri=URI + ".private", groups=frozenset({"private"}))
        self.assertFalse(self.check()["exact_matches"])
        self.assertFalse(self.check(PUMP.replace("37.5", "38.5"))["similar_matches"])
        own = self.source()
        self.query("MATCH (c:Chunk {chunk_id:$id}) SET c.access_groups=['private']", id=own.chunks[1].chunk_id)
        result = self.check()
        self.assertEqual(result["exact_matches"], [])
        self.assertEqual(result["compared_versions"], 0)
        self.assertFalse(result["truncated"])

    def test_withdrawal_broken_membership_and_bad_ranges_are_excluded(self):
        source = self.source()
        mutations = (
            ("MATCH (d:Document {document_id:$id}) SET d.retirement_id='withdrawn'", source.document.document_id,
             "MATCH (d:Document {document_id:$id}) REMOVE d.retirement_id"),
            ("MATCH (v:DocumentVersion {version_id:$id}) SET v.lifecycle_status='RETIRED'", source.version.version_id,
             "MATCH (v:DocumentVersion {version_id:$id}) REMOVE v.lifecycle_status"),
            ("MATCH (s:KnowledgeSnapshot {snapshot_id:$id}) SET s.retirement_id='withdrawn'", source.snapshot_id,
             "MATCH (s:KnowledgeSnapshot {snapshot_id:$id}) REMOVE s.retirement_id"),
            ("MATCH (c:Chunk {chunk_id:$id}) SET c.char_start=-1", source.chunks[0].chunk_id,
             "MATCH (c:Chunk {chunk_id:$id}) SET c.char_start=0"),
        )
        for mutation, identifier, restore in mutations:
            with self.subTest(mutation=mutation):
                self.query(mutation, id=identifier)
                self.assertFalse(self.check()["exact_matches"])
                self.query(restore, id=identifier)
                self.assertTrue(self.check()["exact_matches"])
        self.query("""MATCH (s:KnowledgeSnapshot {snapshot_id:$snapshot})-[r:INCLUDES_CHUNK]->
                    (c:Chunk {chunk_id:$chunk}) DELETE r""", snapshot=source.snapshot_id, chunk=source.chunks[1].chunk_id)
        self.assertFalse(self.check()["exact_matches"])

    def test_concurrent_claim_is_unique_and_release_allows_next_owner(self):
        barrier = Barrier(4)

        def claim():
            barrier.wait(timeout=10)
            try:
                return self.guard.claim(self.principal, content_checksum(PUMP), self.principal.groups)
            except UploadAlreadyRunning:
                return None

        with ThreadPoolExecutor(max_workers=4) as workers:
            leases = list(workers.map(lambda _: claim(), range(4)))
        owners = [lease for lease in leases if lease is not None]
        self.assertEqual(len(owners), 1)
        self.assertEqual(self.query("MATCH (r:UploadReservation {tenant_id:$tenant}) RETURN count(r) AS n",
                                    tenant=self.tenant)[0]["n"], 1)
        self.guard.release(owners[0])
        second = self.guard.claim(self.principal, content_checksum(PUMP), self.principal.groups)
        self.assertNotEqual(second.owner_token, owners[0].owner_token)
        self.guard.release(second)

    def test_expiry_recovery_fences_stale_release_and_scope_does_not_leak(self):
        checksum = content_checksum(PUMP)
        old = self.guard.claim(self.principal, checksum, self.principal.groups)
        self.query("""MATCH (r:UploadReservation {reservation_id:$id})
                   SET r.lease_expires_at=datetime()-duration({seconds:1})""", id=old.reservation_id)
        new = self.guard.claim(self.principal, checksum, self.principal.groups)
        self.guard.release(old)
        with self.assertRaises(UploadAlreadyRunning):
            self.guard.claim(self.principal, checksum, self.principal.groups)
        other = replace(self.principal, groups=frozenset({"members", "private"}))
        scoped = self.guard.claim(other, checksum, frozenset({"members"}))
        self.assertNotEqual(scoped.reservation_id, new.reservation_id)
        self.guard.release(scoped)
        self.guard.release(new)

    def test_context_releases_after_workflow_failure(self):
        with self.assertRaisesRegex(ValueError, "pump extraction failed"):
            with self.guard.hold(self.principal, content_checksum(PUMP), self.principal.groups):
                raise ValueError("pump extraction failed")
        with self.guard.hold(self.principal, content_checksum(PUMP), self.principal.groups):
            pass


if __name__ == "__main__":
    unittest.main()
