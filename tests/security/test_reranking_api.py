"""Reranking usage, trusted context and non-retryable paid-stage failures."""

import asyncio
from dataclasses import replace
import unittest

from fastapi.testclient import TestClient
from neo4j.exceptions import ServiceUnavailable

from graphrag_prod.api.backend import GraphRAGApplicationBackend, GraphRAGQueryOperations, QueryEmbedding
from graphrag_prod.api.contracts import RetrievalRequest
from graphrag_prod.api.runtime import (
    BoundedOperationRunner, DependencyTimeoutError, OperationKind,
    RerankingFailedError, RetryableBackendError, RuntimePolicy,
)
from graphrag_prod.domain import Principal
from graphrag_prod.generation import GroundedGenerationService
from graphrag_prod.industrial.retrieval import build_rerank_scope_context
from graphrag_prod.retrieval import Neo4jRetrievalEngine
from graphrag_prod.retrieval.engine import HYDRATE_QUERY
from graphrag_prod.retrieval.reranking import RerankingError
from tests.e2e.test_api import _app, _headers
from tests.security.test_industrial_retrieval_api import ScopeResolver, ScopedEngine
from tests.unit.test_api_backend import (
    RecordingAnswerModel, RecordingDocuments, RecordingEmbedder, RecordingReadiness, _envelope,
)
from tests.unit.test_retrieval_reranking import FixtureDriver, FixtureReranker


class RerankingAPISecurityTests(unittest.TestCase):
    def build(self, driver, provider):
        embedding = QueryEmbedding((1.0, 0.0), "space1")
        self.embedder = RecordingEmbedder(embedding)
        self.queries = GraphRAGQueryOperations(Neo4jRetrievalEngine(driver, reranker=provider),
            self.embedder, GroundedGenerationService(RecordingAnswerModel()))
        return GraphRAGApplicationBackend(documents=RecordingDocuments(), queries=self.queries,
                                         readiness=RecordingReadiness())

    def test_http_trace_exposes_separate_model_rank_and_actual_usage(self):
        driver = FixtureDriver()
        provider = FixtureReranker(driver, unscored=True)
        backend = self.build(driver, provider)
        with TestClient(_app(backend)) as client:
            result = client.post("/v1/retrieval", headers=_headers(), json={"query_text": "condition?", "include_graph": False})
        self.assertEqual(result.status_code, 200, result.text)
        trace = result.json()["trace"]
        self.assertEqual(trace["reranking"]["ranked_chunk_ids"], ["chunk-002", "chunk-001", "chunk-000"])
        self.assertIsNone(result.json()["chunks"][0]["score"])
        self.assertEqual(trace["reranking"]["response"]["input_tokens"], 120)
        self.assertIsNone(trace["reranking"]["response"]["prompt_tokens"])
        self.assertNotIn("raw_response_json", trace["reranking"]["response"])
        direct = self.queries.retrieve(Principal("p", "tenant-test", frozenset({"engineering"})),
                                       RetrievalRequest(query_text="condition?", include_graph=False))
        self.assertEqual(direct.usage.input_tokens, 120 + self.embedder.result.usage.input_tokens)
        self.assertEqual(direct.usage.model_calls, 1 + self.embedder.result.usage.model_calls)
        self.assertIn(("candidate_reranking", 5.0), direct.usage.stages)

    def test_max_attempts_three_never_repeats_paid_provider_failures(self):
        cases = ("provider", "timeout", "malformed", "acl", "finalize-db")
        for case in cases:
            with self.subTest(case=case):
                driver = FixtureDriver()
                provider = FixtureReranker(driver,
                    error=TimeoutError() if case == "timeout" else RerankingError("safe") if case == "provider" else None,
                    after=(lambda d: d.revoked.add("chunk-000")) if case == "acl" else None)
                if case == "malformed":
                    original = provider.rerank
                    provider.rerank = lambda q, c: replace(original(q, c), source_checksums=("e" * 64,) * len(c))
                if case == "finalize-db":
                    original_run = driver.run
                    def fail_final(query, **parameters):
                        if driver.reads == 2 and query == HYDRATE_QUERY:
                            raise ServiceUnavailable("private database detail")
                        return original_run(query, **parameters)
                    driver.run = fail_final
                backend = self.build(driver, provider)
                runner = BoundedOperationRunner(backend, policy=RuntimePolicy(max_attempts=3, timeout_seconds=5))
                try:
                    with self.assertRaises(DependencyTimeoutError if case == "timeout" else RerankingFailedError) as caught:
                        asyncio.run(runner.run(_envelope(OperationKind.RETRIEVAL,
                            {"query_text": "condition?", "include_graph": False})))
                    self.assertNotIsInstance(caught.exception, RetryableBackendError)
                    self.assertFalse(caught.exception.info.retryable)
                    self.assertEqual(caught.exception.info.status_code, 504 if case == "timeout" else 503)
                    self.assertEqual(len(provider.calls), 1)
                finally:
                    runner.close()

    def test_internal_context_is_not_an_http_field(self):
        driver = FixtureDriver()
        provider = FixtureReranker(driver)
        backend = self.build(driver, provider)
        with TestClient(_app(backend)) as client:
            for route in ("/v1/retrieval", "/v1/answers"):
                result = client.post(route, headers=_headers(), json={"query_text": "condition?", "rerank_context": "forged device"})
                self.assertEqual(result.status_code, 422)
        self.assertFalse(provider.calls)

    def test_scope_context_is_derived_from_validated_requested_keys_only_when_configured(self):
        self.assertEqual(build_rerank_scope_context(("asset-hvx-a01", "asset-bkt-b02")),
                         "User-selected equipment scope: BKT-B02, HVX-A01")
        self.assertIsNone(build_rerank_scope_context(()))
        for configured in (False, True):
            engine = ScopedEngine()
            operations = GraphRAGQueryOperations(engine, RecordingEmbedder(QueryEmbedding((1.0, 0.0), "space-v1")),
                GroundedGenerationService(RecordingAnswerModel()), industrial_scope_resolver=ScopeResolver(),
                include_industrial_rerank_context=configured)
            operations.retrieve(Principal("p", "tenant-alpha", frozenset({"engineering"})),
                RetrievalRequest.model_validate({"query_text": "condition?", "industrial_scope": {"asset_keys": ["asset-hvx-a01"]}}))
            self.assertEqual(engine.requests[0].rerank_context,
                             "User-selected equipment scope: HVX-A01" if configured else None)


if __name__ == "__main__":
    unittest.main()
