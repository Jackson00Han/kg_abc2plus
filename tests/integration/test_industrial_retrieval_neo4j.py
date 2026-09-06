"""Two-source real-Neo4j proof of governed graph RA and industrial scope isolation."""

from dataclasses import replace
from datetime import timedelta
import unittest

from graphrag_prod.domain import Principal
from graphrag_prod.industrial.corpus import CorpusEntity, CorpusRelationship, CorpusSection
from graphrag_prod.industrial.loading import Neo4jIndustrialLoader, _LoaderLease, industrial_loader_principal
from graphrag_prod.industrial.retrieval import IndustrialScope, Neo4jIndustrialScopeResolver
from graphrag_prod.retrieval import Neo4jRetrievalEngine, RetrievalLimits, RetrievalRequest, VersionFilter
from graphrag_prod.retrieval.engine import RerankAttemptedFailure
from graphrag_prod.retrieval.reranking import RerankResponse, RerankScore
from tests.fixtures.industrial_loading import NOW, PROFILE, FixtureEmbedder, tiny_corpus
from tests.integration import test_industrial_loading_neo4j as loader_tests


class _ReverseReranker:
    def __init__(self, after=None):
        self.after = after
        self.calls = 0
        self.candidates = ()

    def rerank(self, query_text, candidates):
        self.calls += 1
        self.candidates = candidates
        if self.after:
            self.after()
        return RerankResponse(
            scores=tuple(RerankScore(c.chunk_id, i, None) for i, c in reversed(tuple(enumerate(candidates)))),
            model="deterministic-fixture-ranking", endpoint="https://rerank.example.test/rank",
            instruct=None, request_id="fixture-request", input_tokens=20, duration_ms=1.0,
            input_checksum="a" * 64, output_checksum="b" * 64, input_bytes=1000,
            pair_utf8_bytes=1000, candidate_count=len(candidates),
            rendered_input_checksums=tuple(c.checksum for c in candidates),
            source_checksums=tuple(c.checksum for c in candidates),
        )


def related_corpus():
    corpus = tiny_corpus()
    field = corpus.documents[1]
    text = field.text + "ProjectModel is classified as ProjectBusbar. ProjectAsset is an instance of ProjectModel.\n"
    field = replace(field, text=text, sections=(CorpusSection("installation", "Installation", 0, len(text)),),
                    asset_keys=("project-asset",))
    return replace(corpus, documents=(corpus.documents[0], field),
        entities=corpus.entities + (CorpusEntity("project-model", "ProductModel", "ProjectModel", "industrial:project-model",
                                                 "field-assets", "installation"),),
        relationships=corpus.relationships + (
            CorpusRelationship("model-class", "project-model", "CLASSIFIED_AS", "busbar-class", "field-assets", "installation"),
            CorpusRelationship("asset-model", "project-asset", "INSTANCE_OF", "project-model", "field-assets", "installation")))


class IndustrialRetrievalNeo4jTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Reuse the explicit disposable/loopback/empty-database guard, not the
        # larger loader test methods or the representative corpus load.
        loader_tests.IndustrialLoadingNeo4jTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        cls.driver.close()

    def tearDown(self):
        self.driver.execute_query("MATCH (n) DETACH DELETE n", database_=self.database)

    def test_published_shared_entity_expands_to_other_source_and_scope_checks_every_visibility_boundary(self):
        corpus = related_corpus()
        loader = Neo4jIndustrialLoader(self.driver, self.database)
        embedder = FixtureEmbedder()
        with _LoaderLease(self.driver, self.database, corpus.tenant_id) as lease:
            loaded = loader._run(corpus, PROFILE, embedder, normalized_artifacts=(), catalog=None,
                                 original_cache=None, heartbeat=lease.refresh)
        self.assertEqual(embedder.calls, 2)
        principal = industrial_loader_principal()
        engine = Neo4jRetrievalEngine(self.driver, self.database)
        limits = RetrievalLimits(top_k=2, seed_k=1, anchor_k=2, adjacent_window=0)

        def retrieve(who, version_filter=VersionFilter()):
            return engine.retrieve(RetrievalRequest("ProjectBusbar", (1.0, 0.0, 0.0, 0.0), who,
                PROFILE.embedding_space_id, limits=limits, version_filter=version_filter))

        result = retrieve(principal)
        self.assertEqual(len(result.chunks), 2)
        self.assertTrue(result.trace.graph_expansion, "governed publication must materialize RA EntityMention edges")
        seed_id = result.trace.seed_ranking[0].chunk_id
        self.assertTrue(any(item.chunk_id != seed_id for item in result.trace.graph_expansion))
        self.assertEqual(result.trace.knowledge_publication_id, loaded["publication_id"])
        self.assertGreater(result.trace.knowledge_activation_generation, 0)
        rerank_request = RetrievalRequest("ProjectBusbar", (1.0, 0.0, 0.0, 0.0), principal,
            PROFILE.embedding_space_id, limits=replace(limits, top_k=1, anchor_k=1))
        provider = _ReverseReranker()
        reranked = Neo4jRetrievalEngine(self.driver, self.database, reranker=provider).retrieve(rerank_request)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(len(provider.candidates), 2)
        self.assertEqual(reranked.chunks[0].citation.chunk_id, provider.candidates[-1].chunk_id)
        self.assertIsNone(reranked.chunks[0].score)
        self.assertEqual(reranked.trace.knowledge_publication_id, loaded["publication_id"])
        # Simulate the same publication becoming active again while the model is
        # outside Neo4j. The monotonic activation generation must detect that ABA.
        def reactivate():
            self.driver.execute_query("MATCH (s:KnowledgePublicationState {tenant_id:$tenant}) "
                "SET s.activation_generation=s.activation_generation+1", tenant=corpus.tenant_id, database_=self.database)
        changed = _ReverseReranker(reactivate)
        with self.assertRaises(RerankAttemptedFailure):
            Neo4jRetrievalEngine(self.driver, self.database, reranker=changed).retrieve(rerank_request)
        self.assertEqual(changed.calls, 1)
        engineering = Principal("engineering-reader", corpus.tenant_id, frozenset({"engineering"}))
        maintenance = Principal("maintenance-reader", corpus.tenant_id, frozenset({"maintenance"}))
        for who in (engineering, maintenance):
            isolated = retrieve(who)
            self.assertEqual(len(isolated.chunks), 1)
            self.assertFalse(isolated.trace.graph_expansion)

        resolver = Neo4jIndustrialScopeResolver(self.driver, self.database)
        asset = IndustrialScope(asset_keys=("project-asset",))
        resolved = resolver.resolve(principal, asset)
        self.assertEqual(len(resolved.version_filter.version_ids), 2)
        self.assertEqual(resolved.trace.reference_documents, 1)
        self.assertEqual(len(retrieve(principal, resolved.version_filter).chunks), 2)
        field_only = resolver.resolve(principal, replace(asset, include_references=False))
        self.assertEqual(len(field_only.version_filter.version_ids), 1)
        def set_field_groups(groups):
            self.driver.execute_query("MATCH (d:Document)-[:ACTIVE_VERSION]->(v:DocumentVersion) "
                "WHERE v.version_id IN $versions MATCH (v)-[:HAS_CHUNK]->(c:Chunk) "
                "SET d.access_groups=$groups,c.access_groups=$groups",
                versions=sorted(field_only.version_filter.version_ids), groups=groups, database_=self.database)
        revoked = _ReverseReranker(lambda: set_field_groups(["revoked-test-group"]))
        try:
            with self.assertRaises(RerankAttemptedFailure):
                Neo4jRetrievalEngine(self.driver, self.database, reranker=revoked).retrieve(rerank_request)
            self.assertEqual(revoked.calls, 1)
        finally:
            set_field_groups(["maintenance"])
        for who, scope, version_filter in (
            (engineering, asset, VersionFilter()),
            (replace(principal, tenant_id="other-tenant"), asset, VersionFilter()),
            (principal, IndustrialScope(asset_keys=("missing-asset",)), VersionFilter()),
            (principal, replace(asset, family="evopact-hvx-up24"), VersionFilter()),
            (principal, asset, VersionFilter(published_at_or_before=NOW - timedelta(days=1))),
            (principal, asset, VersionFilter(version_ids=resolved.version_filter.version_ids - field_only.version_filter.version_ids)),
        ):
            with self.subTest(who=who.principal_id, scope=scope, version_filter=version_filter):
                denied = resolver.resolve(who, scope, version_filter=version_filter)
                self.assertTrue(denied.version_filter.match_none)
                self.assertEqual(denied.trace.matched_documents, 0)
        denied = resolver.resolve(engineering, asset)
        self.assertFalse(retrieve(engineering, denied.version_filter).chunks)

        # Inconsistent chunk ACL or an unpublished active snapshot must each
        # prevent asset resolution, even with a visible manual.
        query = "MATCH (v:DocumentVersion) WHERE v.version_id IN $versions MATCH (v)-[:HAS_CHUNK]->(c:Chunk) "
        self.driver.execute_query(query + "SET c.access_policy_version=c.access_policy_version+1",
            versions=sorted(field_only.version_filter.version_ids), database_=self.database)
        self.assertTrue(resolver.resolve(principal, asset).version_filter.match_none)
        self.driver.execute_query(query + "SET c.access_policy_version=c.access_policy_version-1",
            versions=sorted(field_only.version_filter.version_ids), database_=self.database)
        self.driver.execute_query("MATCH (d:Document)-[:ACTIVE_SNAPSHOT]->(s:KnowledgeSnapshot)-[:OF_VERSION]->(v:DocumentVersion) "
            "WHERE v.version_id IN $versions SET s.build_state='BUILDING'",
            versions=sorted(field_only.version_filter.version_ids), database_=self.database)
        self.assertTrue(resolver.resolve(principal, asset).version_filter.match_none)


if __name__ == "__main__":
    unittest.main()
