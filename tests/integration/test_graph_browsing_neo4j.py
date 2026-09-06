"""Reader graph views on a disposable, tiny governed industrial corpus."""

from dataclasses import replace
import unittest
import json
from time import monotonic

from graphrag_prod.api.backend import GraphRAGQueryOperations, QueryEmbedding
from graphrag_prod.api.contracts import RetrievalRequest as ApiRetrievalRequest
from graphrag_prod.api.graph_contracts import GraphBrowseResponse, GraphEvidenceResponseEnvelope
from graphrag_prod.api.runtime import GraphViewChangedError
from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import entity_id
from graphrag_prod.generation import GroundedGenerationService
from graphrag_prod.graph.browse_models import GraphBrowseQuery, GraphReadPin, GraphViewChanged
from graphrag_prod.graph.browsing import Neo4jPublishedGraphBrowser
from graphrag_prod.industrial.corpus import CorpusSection
from graphrag_prod.industrial.loading import Neo4jIndustrialLoader, _LoaderLease, industrial_loader_principal, _prepare_industrial_load
from graphrag_prod.retrieval import Neo4jEvidenceSubgraphProjector, Neo4jRetrievalEngine, RetrievalRequest, VersionFilter
from tests.fixtures.industrial_loading import NOW, PROFILE, FixtureEmbedder, tiny_corpus
from tests.integration import test_industrial_loading_neo4j as loader_tests
from tests.unit.test_api_backend import RecordingAnswerModel, RecordingEmbedder, RecordingRetrievalEngine


def crossing_seed_corpus():
    """Find a deterministic four-chunk fixture whose independent minima cross."""
    original = tiny_corpus()
    field = original.documents[1]
    identities = {item.key: entity_id(original.tenant_id, item.type_name, item.identity) for item in original.entities}
    lower_entity = min(identities["project-asset"], identities["project-site"])
    for suffix in range(64):
        joint_key, asset_key, site_key = (f"joint-{suffix}", f"asset-only-{suffix}", f"site-only-{suffix}")
        joint = replace(field, key=joint_key)
        asset_text = "SYNTHETIC_FIELD_RECORD 合成\nProjectAsset register entry.\n"
        site_text = "SYNTHETIC_FIELD_RECORD 合成\nProjectSite register entry.\n"
        asset = replace(field, key=asset_key, title="Asset register", text=asset_text, sections=(CorpusSection("installation", "Installation", 0, len(asset_text)),))
        site = replace(field, key=site_key, title="Site register", text=site_text, sections=(CorpusSection("installation", "Installation", 0, len(site_text)),))
        entities = tuple(replace(item, document_key=asset_key if item.key == "project-asset" else site_key) if item.key in {"project-asset", "project-site"} else item for item in original.entities)
        relationships = tuple(replace(item, document_key=joint_key) if item.key == "asset-site" else item for item in original.relationships)
        corpus = replace(original, documents=(original.documents[0], joint, asset, site), entities=entities, relationships=relationships)
        prepared = _prepare_industrial_load(corpus, PROFILE, ingested_at=NOW)
        chunks = {source.key: source.request.domain_inputs()[2][0].chunk_id for source in prepared.sources}
        a, b, c = chunks[asset_key], chunks[site_key], chunks[joint_key]
        wrong_pair = (lower_entity, min(a, b, c))
        valid_pairs = {(mention.entity.entity_id, mention.evidence.chunk_id) for batch in prepared.batches for mention in batch.mentions}
        if c > max(a, b) and wrong_pair not in valid_pairs:
            return corpus, tuple(chunks.values()), wrong_pair, valid_pairs
    raise AssertionError("could not construct crossed seed ordering fixture")


class GraphBrowsingNeo4jTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        loader_tests.IndustrialLoadingNeo4jTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        cls.driver.close()

    def tearDown(self):
        self.driver.execute_query("MATCH (n) DETACH DELETE n", database_=self.database)

    def load(self, corpus):
        loader = Neo4jIndustrialLoader(self.driver, self.database)
        with _LoaderLease(self.driver, self.database, corpus.tenant_id) as lease:
            loader._run(corpus, PROFILE, FixtureEmbedder(), normalized_artifacts=(), catalog=None, original_cache=None, heartbeat=lease.refresh)
        who = industrial_loader_principal(corpus.tenant_id)
        return replace(who, capabilities=who.capabilities | {"knowledge:graph:read"})

    def test_reader_partial_views_exact_evidence_pagination_and_pin_changes(self):
        corpus = tiny_corpus()
        text = "SYNTHETIC_FIELD_RECORD 合成\nProjectAsset source-only inspection has no accepted graph assertions.\n"
        source_only = replace(corpus.documents[1], key="source-only-inspection", title="Source-only inspection", text=text,
                              sections=(CorpusSection("inspection", "Inspection", 0, len(text)),))
        corpus = replace(corpus, documents=(*corpus.documents, source_only))
        principal = self.load(corpus)
        browser = Neo4jPublishedGraphBrowser(self.driver, self.database)
        page = browser.query(principal, GraphBrowseQuery(page_size=1))
        pin = GraphReadPin(**page["pin"])
        revisions = []
        original = page
        while True:
            GraphBrowseResponse.model_validate(page)
            revisions.extend(item["revision_id"] for item in page["edges"])
            if not page["page"]["has_more"]:
                break
            page = browser.query(principal, GraphBrowseQuery(page_size=1), view_token=page["view_token"], cursor=page["page"]["next_cursor"])
        self.assertEqual(len(revisions), 2)
        self.assertEqual(len(set(revisions)), 2)
        engineering = replace(principal, groups=frozenset({"engineering"}))
        visible = browser.query(engineering, GraphBrowseQuery())
        self.assertEqual(len(visible["nodes"]), 2)
        self.assertEqual(len(visible["edges"]), 1)
        self.assertTrue(all(item["authority_levels"] == ("AUTHORITATIVE",) for item in visible["nodes"]))
        proof = browser.evidence(engineering, (visible["edges"][0]["revision_id"], "hidden-or-missing"), view_token=visible["view_token"])
        evidence = GraphEvidenceResponseEnvelope.model_validate(proof)
        self.assertEqual(len(evidence.items), 1)
        self.assertIsNotNone(evidence.items[0].reviewed_at)
        self.assertTrue(evidence.items[0].applicability.project_curated_not_company_approved)
        self.assertTrue(evidence.items[0].evidence.citation.chunk_text)
        empty = browser.query(replace(principal, tenant_id="foreign-tenant"), GraphBrowseQuery())
        self.assertFalse(empty["nodes"])
        self.assertIsNone(empty["pin"]["publication_id"])
        empty = browser.query(principal, GraphBrowseQuery(), version_filter=VersionFilter(match_none=True))
        self.assertFalse(empty["edges"])

        # Reuse captured retrieval once, then prove include_graph is bound to
        # that publication activation without repeating any paid provider call.
        request = RetrievalRequest("ProjectAsset", (1.0, 0.0, 0.0, 0.0), principal, PROFILE.embedding_space_id)
        retrieved = Neo4jRetrievalEngine(self.driver, self.database).retrieve(request)
        self.assertEqual(retrieved.trace.knowledge_tbox_checksum, pin.tbox_checksum)
        embedder = RecordingEmbedder(QueryEmbedding((1.0, 0.0, 0.0, 0.0), PROFILE.embedding_space_id))
        operations = GraphRAGQueryOperations(RecordingRetrievalEngine(retrieved), embedder, GroundedGenerationService(RecordingAnswerModel()),
                                           subgraph_projector=Neo4jEvidenceSubgraphProjector(self.driver, self.database))
        response = operations.retrieve(principal, ApiRetrievalRequest(query_text="ProjectAsset", include_graph=True))
        self.assertTrue(response.payload.graph.entities)
        source_rows = self.driver.execute_query(
            "MATCH (d:Document {tenant_id:$tenant,title:'Source-only inspection'})-[:ACTIVE_VERSION]->(v:DocumentVersion) "
            "MATCH (v)-[:HAS_CHUNK]->(c:Chunk) RETURN c.chunk_id AS chunk_id, v.version_id AS version_id",
            tenant=corpus.tenant_id, database_=self.database).records
        self.assertEqual(len(source_rows), 1)
        source_id, source_version = source_rows[0]["chunk_id"], source_rows[0]["version_id"]
        self.assertIn(source_id, retrieved.trace.selected_chunk_ids)
        self.assertEqual(self.driver.execute_query(
            "MATCH (:KnowledgePublication {status:'ACTIVE'})-[:PUBLISHES_KNOWLEDGE_REVISION]->(r) "
            "WHERE r.version_id=$version RETURN count(r) AS count", version=source_version, database_=self.database).records[0]["count"], 0)
        # Revoke only this source: all graph records remain visible and the
        # publication/corpus pins stay unchanged. The combined response must fail.
        def set_source_groups(groups):
            self.driver.execute_query(
                "MATCH (d:Document {tenant_id:$tenant})-[:ACTIVE_VERSION]->(v:DocumentVersion {version_id:$version}) "
                "MATCH (v)-[:HAS_CHUNK]->(c:Chunk) SET d.access_groups=$groups,c.access_groups=$groups",
                tenant=corpus.tenant_id, version=source_version, groups=groups, database_=self.database)
        set_source_groups(["phase-revoked"])
        try:
            self.assertEqual(len(browser.query(principal, GraphBrowseQuery())["edges"]), 2)
            with self.assertRaises(GraphViewChangedError):
                operations.retrieve(principal, ApiRetrievalRequest(query_text="ProjectAsset", include_graph=True))
        finally:
            set_source_groups(["maintenance"])
        self.driver.execute_query("MATCH (state:KnowledgePublicationState {tenant_id:$tenant}) SET state.activation_generation=state.activation_generation+1", tenant=corpus.tenant_id, database_=self.database)
        with self.assertRaises(GraphViewChanged):
            browser.query(principal, GraphBrowseQuery(page_size=1), view_token=original["view_token"])
        with self.assertRaises(GraphViewChangedError):
            operations.retrieve(principal, ApiRetrievalRequest(query_text="ProjectAsset", include_graph=True))
        fresh = browser.query(principal, GraphBrowseQuery())
        self.driver.execute_query("MATCH (doc:Document {tenant_id:$tenant}) WHERE 'maintenance' IN doc.access_groups "
            "MATCH (doc)-[:ACTIVE_SNAPSHOT]->(:KnowledgeSnapshot)-[:INCLUDES_CHUNK]->(chunk:Chunk) "
            "SET doc.access_groups=['revoked'],chunk.access_groups=['revoked']", tenant=corpus.tenant_id, database_=self.database)
        with self.assertRaises(GraphViewChanged):
            browser.evidence(principal, tuple(revisions), view_token=fresh["view_token"])

    def test_cross_document_seed_pair_remains_one_real_authorized_mention(self):
        corpus, chunk_ids, wrong_pair, valid_pairs = crossing_seed_corpus()
        self.assertNotIn(wrong_pair, valid_pairs, "fixture must expose the independent-minimum defect")
        principal = self.load(corpus)
        browser = Neo4jPublishedGraphBrowser(self.driver, self.database)
        started = monotonic()
        try:
            page = browser.query(principal, GraphBrowseQuery())
        finally:
            print(json.dumps({"check": "first_graph_browse_no_query_warmup", "elapsed_seconds": round(monotonic() - started, 3),
                              "transaction_timeout_seconds": 30.0}), flush=True)
        self.assertEqual(len(page["edges"]), 2)
        projected = Neo4jEvidenceSubgraphProjector(self.driver, self.database).project(principal, chunk_ids)
        self.assertEqual(len(projected.relationship_assertions), 2)

    def test_graph_queries_reject_inconsistent_source_identity_and_current_document_policy(self):
        corpus = tiny_corpus()
        principal = self.load(corpus)
        browser = Neo4jPublishedGraphBrowser(self.driver, self.database)
        publication = browser.query(principal, GraphBrowseQuery())
        chunks = tuple(row["chunk_id"] for row in self.driver.execute_query(
            "MATCH (c:Chunk {tenant_id:$tenant}) RETURN c.chunk_id AS chunk_id",
            tenant=corpus.tenant_id, database_=self.database).records)
        projector = Neo4jEvidenceSubgraphProjector(self.driver, self.database)
        for property_name, invalid in (("document_id", "wrong-document"), ("version_id", "wrong-version")):
            with self.subTest(property_name=property_name):
                # Only these two fixed field names enter this fixture query.
                self.driver.execute_query(f"MATCH (c:Chunk {{tenant_id:$tenant}}) SET c.original_field=c.{property_name}, c.{property_name}=$invalid+c.chunk_id",
                    tenant=corpus.tenant_id, invalid=invalid, database_=self.database)
                try:
                    self.assertFalse(browser.query(principal, GraphBrowseQuery())["nodes"])
                    self.assertFalse(projector.project(principal, chunks).entities)
                    with self.assertRaises(GraphViewChanged):
                        browser.query(principal, GraphBrowseQuery(), view_token=publication["view_token"])
                finally:
                    self.driver.execute_query(f"MATCH (c:Chunk {{tenant_id:$tenant}}) SET c.{property_name}=c.original_field REMOVE c.original_field",
                        tenant=corpus.tenant_id, database_=self.database)
        self.driver.execute_query("MATCH (d:Document {tenant_id:$tenant}) SET d.access_policy_version=d.access_policy_version+1",
            tenant=corpus.tenant_id, database_=self.database)
        self.assertFalse(browser.query(principal, GraphBrowseQuery())["nodes"])
        self.assertFalse(projector.project(principal, chunks).entities)


if __name__ == "__main__":
    unittest.main()
