"""JWT-bound industrial scopes use the production dispatch and query boundary."""

from dataclasses import replace
import unittest

from fastapi.testclient import TestClient

from graphrag_prod.api.backend import GraphRAGApplicationBackend, GraphRAGQueryOperations, QueryEmbedding
from graphrag_prod.api.contracts import RetrievalRequest
from graphrag_prod.api.runtime import DependencyUnavailableError
from graphrag_prod.domain import Principal
from graphrag_prod.generation import GroundedGenerationService
from graphrag_prod.industrial.retrieval import IndustrialScopeResolution, IndustrialScopeTrace
from graphrag_prod.retrieval import VersionFilter
from tests.e2e.test_api import _app, _headers, _token
from tests.unit.test_api_backend import (
    RecordingAnswerModel, RecordingDocuments, RecordingEmbedder, RecordingReadiness,
    RecordingRetrievalEngine, RecordingSubgraphProjector, _retrieval_result,
)


class ScopeResolver:
    def __init__(self, *, versions=()):
        self.versions = frozenset(versions)
        self.calls = []

    def resolve(self, principal, scope, *, version_filter):
        self.calls.append((principal, scope, version_filter))
        effective = VersionFilter(version_ids=self.versions,
            published_at_or_before=version_filter.published_at_or_before, match_none=not self.versions)
        return IndustrialScopeResolution(effective, IndustrialScopeTrace(scope, len(self.versions), 0, not self.versions))


class ScopedEngine:
    def __init__(self):
        self.requests = []

    def retrieve(self, request):
        self.requests.append(request)
        result = _retrieval_result(chunks=(), tenant_id=request.principal.tenant_id, version_filter=request.version_filter)
        return replace(result, trace=replace(result.trace, limits=request.limits))


class IndustrialRetrievalAPISecurityTests(unittest.TestCase):
    def setup_backend(self, resolver):
        self.engine = ScopedEngine()
        self.embedder = RecordingEmbedder(QueryEmbedding((1.0, 0.0), "space-v1"))
        self.model = RecordingAnswerModel()
        self.projector = RecordingSubgraphProjector()
        self.queries = GraphRAGQueryOperations(self.engine, self.embedder, GroundedGenerationService(self.model),
            subgraph_projector=self.projector, industrial_scope_resolver=resolver)
        return GraphRAGApplicationBackend(documents=RecordingDocuments(), queries=self.queries,
                                         readiness=RecordingReadiness())

    def test_both_http_routes_keep_jwt_scope_and_explicit_empty_result(self):
        resolver = ScopeResolver()
        backend = self.setup_backend(resolver)
        auth = _headers(_token(tenant_id="industrial-test", groups=("engineering",)))
        with TestClient(_app(backend)) as client:
            retrieval = client.post("/v1/retrieval", headers=auth, json={
                "query_text": "HVX-A01 状态", "industrial_scope": {"asset_keys": ["hvx-a01"]}})
            answer = client.post("/v1/answers", headers=auth, json={
                "query_text": "HVX-A01 状态", "industrial_scope": {"asset_keys": ["hvx-a01"]}})
        self.assertEqual(retrieval.status_code, 200, retrieval.text)
        self.assertEqual(answer.status_code, 200, answer.text)
        trace = retrieval.json()["trace"]
        self.assertTrue(trace["version_filter"]["match_none"])
        self.assertEqual(trace["industrial_scope"]["matched_documents"], 0)
        self.assertEqual(retrieval.json()["chunks"], [])
        self.assertEqual(retrieval.json()["graph"]["entities"], [])
        self.assertEqual(answer.json()["status"], "insufficient_context")
        self.assertEqual(len(resolver.calls), 2)
        for principal, scope, _ in resolver.calls:
            self.assertEqual(principal.tenant_id, "industrial-test")
            self.assertEqual(principal.groups, frozenset({"engineering"}))
            self.assertEqual(scope.asset_keys, ("hvx-a01",))
        self.assertFalse(self.model.requests)
        self.assertFalse(self.projector.calls)

    def test_http_scope_cannot_spoof_identity_or_coerce_filters(self):
        resolver = ScopeResolver()
        backend = self.setup_backend(resolver)
        malformed = ({"tenant_id": "victim"}, {"access_groups": ["maintenance"]},
                     {"asset_keys": "hvx-a01"}, {"asset_keys": ["hvx-a01", "hvx-a01"]},
                     {"asset_keys": ["HVX-A01"]}, {"include_references": 1},
                     {"family": "hvx-o"}, {"source_kinds": ["UNREVIEWED"]})
        with TestClient(_app(backend)) as client:
            for route in ("/v1/retrieval", "/v1/answers"):
                for scope in malformed:
                    with self.subTest(route=route, scope=scope):
                        result = client.post(route, headers=_headers(), json={"query_text": "q", "industrial_scope": scope})
                        self.assertEqual(result.status_code, 422, result.text)
                result = client.post(route, headers=_headers(), json={"query_text": "q", "version_filter": {"match_none": "true"}})
                self.assertEqual(result.status_code, 422)
        self.assertFalse(resolver.calls)
        self.assertFalse(self.embedder.calls)

    def test_scope_without_resolver_fails_closed_before_provider_and_legacy_omission_works(self):
        backend = self.setup_backend(None)
        with TestClient(_app(backend)) as client:
            scoped = client.post("/v1/retrieval", headers=_headers(), json={"query_text": "q", "industrial_scope": {}})
            self.assertEqual(scoped.status_code, 503)
            self.assertFalse(self.embedder.calls)
            legacy = client.post("/v1/retrieval", headers=_headers(), json={"query_text": "q"})
            self.assertEqual(legacy.status_code, 200, legacy.text)
            self.assertFalse(legacy.json()["trace"]["version_filter"]["match_none"])
            self.assertIsNone(legacy.json()["trace"]["industrial_scope"])

    def test_resolver_cannot_widen_explicit_version_filter(self):
        self.setup_backend(ScopeResolver(versions=("unexpected-version",)))
        with self.assertRaises(DependencyUnavailableError):
            self.queries.retrieve(Principal("p", "tenant-alpha", frozenset({"engineering"})),
                RetrievalRequest.model_validate({"query_text": "q", "industrial_scope": {},
                    "version_filter": {"version_ids": ["permitted-version"]}}))
        self.assertFalse(self.embedder.calls)

    def test_broken_engine_cannot_return_chunks_for_explicit_empty_filter(self):
        self.setup_backend(ScopeResolver())
        self.queries._retrieval_engine = RecordingRetrievalEngine(_retrieval_result(version_filter=VersionFilter(match_none=True)))
        with self.assertRaises(DependencyUnavailableError):
            self.queries.retrieve(Principal("p", "tenant-alpha", frozenset({"engineering"})),
                RetrievalRequest.model_validate({"query_text": "q", "industrial_scope": {}}))

    def test_engine_cannot_escape_resolved_version_ids_even_with_matching_trace(self):
        self.setup_backend(ScopeResolver(versions=("permitted-version",)))
        self.queries._retrieval_engine = RecordingRetrievalEngine(_retrieval_result(
            version_filter=VersionFilter(version_ids=frozenset({"permitted-version"}))))
        with self.assertRaises(DependencyUnavailableError):
            self.queries.retrieve(Principal("p", "tenant-alpha", frozenset({"engineering"})),
                RetrievalRequest.model_validate({"query_text": "q", "industrial_scope": {}}))


if __name__ == "__main__":
    unittest.main()
