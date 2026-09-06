"""Bounded industrial scope, explicit empty filters and publication consistency."""

from datetime import UTC, datetime
import unittest

from graphrag_prod.domain import Principal
from graphrag_prod.industrial.retrieval import (
    ASSET_SCOPE_QUERY, SOURCE_SCOPE_QUERY, IndustrialScope,
    Neo4jIndustrialScopeResolver, _ScopeChanged, _STATE_QUERY,
)
from graphrag_prod.retrieval import RetrievalRequest, RetrievalUnavailable, VersionFilter
from graphrag_prod.retrieval.engine import CORPUS_STATE_QUERY, Neo4jRetrievalEngine, _CorpusStateChanged
from graphrag_prod.retrieval.subgraph import Neo4jEvidenceSubgraphProjector


PRINCIPAL = Principal("engineer", "industrial-test", frozenset({"engineering"}))


def source(key, *, assets=(), family="canalis-kt", kind="SYNTHETIC_FIELD_RECORD"):
    return dict(document_id="document-" + key, version_id="version-" + key,
                asset_keys=list(assets), family=family, source_kind=kind)


class ScopeTransaction:
    def __init__(self, *, assets=(), sources=(), revisions=(4, 4)):
        self.assets, self.sources = assets, sources
        self.revisions = iter(revisions)
        self.calls = []

    def run(self, query, **params):
        self.calls.append((query, params))
        if query == _STATE_QUERY:
            return [{"corpus_revision": next(self.revisions)}]
        return self.assets if query == ASSET_SCOPE_QUERY else self.sources


class IndustrialScopeTests(unittest.TestCase):
    def resolve(self, tx, scope=IndustrialScope(), version_filter=VersionFilter()):
        return Neo4jIndustrialScopeResolver._resolve_tx(tx, PRINCIPAL, scope, version_filter)

    def test_strict_scope_and_empty_filter_types(self):
        for kwargs in ({"family": "hvx-o"}, {"asset_keys": ["asset-a"]},
                       {"asset_keys": ("asset-a", "asset-a")}, {"asset_keys": ("HVX-A01",)},
                       {"asset_keys": tuple(f"asset-{n}" for n in range(9))},
                       {"include_references": 1}, {"source_kinds": ("EXPERT",)}):
            with self.subTest(kwargs=kwargs), self.assertRaises((TypeError, ValueError)):
                IndustrialScope(**kwargs)
        for value in (0, 1, "false", None):
            with self.subTest(value=value), self.assertRaises(TypeError):
                VersionFilter(match_none=value)
        self.assertFalse(VersionFilter().match_none)
        self.assertEqual(IndustrialScope(asset_keys=("asset-b", "asset-a")).asset_keys,
                         ("asset-a", "asset-b"))

    def test_missing_or_partially_inaccessible_asset_cannot_fall_back_to_references(self):
        for visible in ((), (source("a", assets=("asset-a",)),)):
            tx = ScopeTransaction(assets=visible, sources=(source("manual", kind="CURATED_REFERENCE"),))
            result = self.resolve(tx, IndustrialScope(asset_keys=("asset-a", "asset-b")))
            self.assertTrue(result.version_filter.match_none)
            self.assertEqual(result.trace.matched_documents, 0)
            self.assertNotIn(SOURCE_SCOPE_QUERY, [q for q, _ in tx.calls])

    def test_authorized_asset_controls_reference_family_and_immutable_version_intersection(self):
        cutoff = datetime(2026, 1, 1, tzinfo=UTC)
        field = source("a", assets=("asset-a",))
        manual = source("manual", kind="CURATED_REFERENCE")
        tx = ScopeTransaction(assets=(field,), sources=(manual,))
        original = VersionFilter(document_ids=frozenset({"document-a", "document-manual"}),
                                 published_at_or_before=cutoff)
        result = self.resolve(tx, IndustrialScope(asset_keys=("asset-a",), source_kinds=("CURATED_REFERENCE",)), original)
        self.assertEqual(result.version_filter.version_ids, frozenset({"version-manual"}))
        self.assertFalse(result.version_filter.document_ids)
        self.assertEqual(result.version_filter.published_at_or_before, cutoff)
        self.assertEqual(result.trace.reference_documents, 1)
        for query, params in tx.calls:
            self.assertEqual(params["tenant_id"], PRINCIPAL.tenant_id)
            if query != _STATE_QUERY:
                self.assertEqual(params["groups"], ["engineering"])
                self.assertEqual(params["document_ids"], ["document-a", "document-manual"])
                self.assertEqual(params["published_before"], cutoff)
                self.assertEqual(params["limit"], 101)
        scope_params = next(p for q, p in tx.calls if q == SOURCE_SCOPE_QUERY)
        self.assertEqual(scope_params["families"], ["canalis-kt"])
        self.assertEqual(scope_params["source_kinds"], ["CURATED_REFERENCE"])

    def test_omitted_family_remains_industrial_only_and_reference_switch_is_forwarded(self):
        tx = ScopeTransaction()
        result = self.resolve(tx, IndustrialScope(include_references=False))
        params = next(p for q, p in tx.calls if q == SOURCE_SCOPE_QUERY)
        self.assertEqual(params["families"], ["canalis-kt", "evopact-hvx-up24"])
        self.assertFalse(params["include_references"])
        self.assertTrue(result.version_filter.match_none)
        self.assertEqual(set(result.trace.as_dict()), {"requested", "matched_documents", "reference_documents",
                         "match_none", "reference_only_sources_omitted", "policy_version"})

    def test_empty_input_short_circuits_before_database_and_cannot_broaden(self):
        resolver = Neo4jIndustrialScopeResolver(object())
        result = resolver.resolve(PRINCIPAL, IndustrialScope(), version_filter=VersionFilter(match_none=True))
        self.assertTrue(result.version_filter.match_none)
        self.assertFalse(result.version_filter.version_ids)

    def test_scope_overflow_duplicate_versions_and_revision_change_fail_closed(self):
        for rows in (tuple(source(str(n)) for n in range(101)),
                     (source("a"), {**source("b"), "version_id": "version-a"})):
            with self.subTest(count=len(rows)), self.assertRaises(RetrievalUnavailable):
                self.resolve(ScopeTransaction(sources=rows))
        with self.assertRaises(_ScopeChanged):
            self.resolve(ScopeTransaction(revisions=(4, 5)))


class EmptyRecallTransaction:
    def __init__(self, final_publication=None, final_generation=0, *, initial_publication=None, initial_generation=0):
        self.calls = []
        self.final_publication, self.final_generation = final_publication, final_generation
        self.initial_publication, self.initial_generation = initial_publication, initial_generation
        self.state_calls = 0

    def run(self, query, **params):
        self.calls.append(query)
        if query != CORPUS_STATE_QUERY:
            return []
        self.state_calls += 1
        return [dict(corpus_revision=4, generation_id="generation-v1", embedding_space_id="space-v1", dimensions=2,
                     knowledge_publication_id=self.final_publication if self.state_calls > 1 else self.initial_publication,
                     knowledge_activation_generation=self.final_generation if self.state_calls > 1 else self.initial_generation)]


class IndustrialRetrievalConsistencyTests(unittest.TestCase):
    def request(self, match_none=False):
        return RetrievalRequest("ProjectAsset", (1.0, 0.0), PRINCIPAL, "space-v1",
                                version_filter=VersionFilter(match_none=match_none))

    def test_match_none_performs_no_recall_but_preserves_active_generation_checks(self):
        tx = EmptyRecallTransaction()
        result = Neo4jRetrievalEngine._retrieve_tx(tx, self.request(True))
        self.assertEqual(tx.calls, [CORPUS_STATE_QUERY, CORPUS_STATE_QUERY])
        self.assertEqual(result.chunks, ())
        self.assertTrue(result.trace.version_filter.match_none)
        self.assertFalse(result.trace.vector_recall)
        self.assertFalse(result.trace.graph_expansion)
        legacy = EmptyRecallTransaction()
        Neo4jRetrievalEngine._retrieve_tx(legacy, self.request())
        self.assertGreater(len(legacy.calls), 2)

    def test_first_publication_and_rollback_generation_invalidate_captured_context(self):
        transactions = (
            EmptyRecallTransaction("publication-v1", 1),
            EmptyRecallTransaction("publication-v1", 3, initial_publication="publication-v1", initial_generation=1),
        )
        for tx in transactions:
            with self.subTest(initial=tx.initial_publication), self.assertRaises(_CorpusStateChanged):
                Neo4jRetrievalEngine._retrieve_tx(tx, self.request(True))

    def test_empty_subgraph_cannot_query_or_return_evidence(self):
        projector = Neo4jEvidenceSubgraphProjector(object())
        result = projector.project(PRINCIPAL, ("chunk-a",), version_filter=VersionFilter(match_none=True))
        self.assertEqual(result.entities, ())
        self.assertEqual(result.matched_chunk_ids, ())


if __name__ == "__main__":
    unittest.main()
