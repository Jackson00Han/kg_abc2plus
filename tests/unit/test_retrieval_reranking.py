"""Two read phases, one external ranking call, and complete post-call reauthorization."""

from dataclasses import replace
import hashlib
import unittest

from graphrag_prod.domain import Principal
from graphrag_prod.retrieval import RetrievalLimits, RetrievalRequest, RetrievalUnavailable, VersionFilter
from graphrag_prod.retrieval.engine import (
    ADJACENT_QUERY, BM25_RECALL_QUERY, CANDIDATE_VECTOR_QUERY, CORPUS_STATE_QUERY,
    GRAPH_EXPANSION_QUERY, HYDRATE_QUERY, VECTOR_RECALL_QUERY,
    Neo4jRetrievalEngine, RerankAttemptedFailure, _PUBLISHED_VERSIONS_QUERY,
)
from graphrag_prod.retrieval.reranking import RerankCandidate, RerankResponse, RerankScore, RerankingError


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def response(candidates, *, unscored=False):
    return RerankResponse(
        scores=tuple(RerankScore(c.chunk_id, i, None if unscored else i / len(candidates))
                     for i, c in reversed(tuple(enumerate(candidates)))),
        model="test-reranker", endpoint="https://rerank.example.test/rank", instruct=None,
        request_id="test-request", input_tokens=120, duration_ms=5.0,
        input_checksum="a" * 64, output_checksum="b" * 64, input_bytes=1000,
        pair_utf8_bytes=900, candidate_count=len(candidates),
        source_checksums=tuple(c.checksum for c in candidates),
        rendered_input_checksums=tuple(c.checksum for c in candidates),
    )


class FixtureDriver:
    def __init__(self, count=3):
        self.sessions = 0
        self.in_tx = False
        self.reads = 0
        self.queries = []
        self.revoked = set()
        self.state = dict(corpus_revision=4, generation_id="g1", embedding_space_id="space1", dimensions=2,
                          knowledge_publication_id="pub1", knowledge_activation_generation=1)
        self.rows = {}
        for i in range(count):
            identifier = f"chunk-{i:03}"
            text = f"Equipment record {i}: documented observation and configuration evidence. " * 2
            self.rows[identifier] = dict(chunk_id=identifier, text=text, chunk_checksum=digest(text),
                ordinal=i, char_start=0, char_end=len(text), page_number=None, section="Observation",
                document_id=f"doc-{i:03}", canonical_uri=f"urn:test:doc-{i:03}", source_name="Test record",
                document_title=f"Equipment {i}", version_id=f"version-{i:03}", version_checksum=digest(text),
                version_number=1, published_at=None)

    def session(self, **kwargs):
        return FixtureSession(self)

    def run(self, query, **parameters):
        self.queries.append((self.reads, query, parameters))
        if query == CORPUS_STATE_QUERY:
            return [dict(self.state)]
        if query == _PUBLISHED_VERSIONS_QUERY:
            return [dict(version_id=row["version_id"]) for row in self.rows.values()]
        if query in (VECTOR_RECALL_QUERY, BM25_RECALL_QUERY, CANDIDATE_VECTOR_QUERY):
            candidates = parameters.get("candidate_ids", self.rows)
            return [dict(chunk_id=k, score=0.99 - i / 1000) for i, k in enumerate(self.rows)
                    if k in candidates and k not in self.revoked][:parameters["limit"]]
        if query in (GRAPH_EXPANSION_QUERY, ADJACENT_QUERY):
            return []
        if query == HYDRATE_QUERY:
            return [dict(self.rows[k]) for k in parameters["chunk_ids"] if k in self.rows and k not in self.revoked]
        raise AssertionError(query)


class FixtureSession:
    def __init__(self, driver):
        self.driver = driver

    def __enter__(self):
        self.driver.sessions += 1
        return self

    def __exit__(self, *args):
        self.driver.sessions -= 1

    def execute_read(self, work, *args):
        self.driver.in_tx = True
        self.driver.reads += 1
        try:
            return work(self.driver, *args)
        finally:
            self.driver.in_tx = False


class FixtureReranker:
    def __init__(self, driver, *, unscored=False, after=None, error=None):
        self.driver, self.after, self.error, self.unscored = driver, after, error, unscored
        self.calls = []

    def rerank(self, query_text, candidates):
        if self.driver.in_tx or self.driver.sessions:
            raise AssertionError("external provider must run outside both read sessions")
        self.calls.append((query_text, candidates))
        if self.error:
            raise self.error
        if self.after:
            self.after(self.driver)
        return response(candidates, unscored=self.unscored)


class RetrievalRerankingTests(unittest.TestCase):
    def request(self, **changes):
        base = RetrievalRequest("What is the equipment condition?", (1.0, 0.0),
            Principal("engineer", "tenant-test", frozenset({"engineering"})), "space1",
            limits=RetrievalLimits(top_k=1, anchor_k=1, adjacent_window=0))
        return replace(base, **changes)

    def test_one_external_call_reorders_full_source_candidates_without_mixing_rrf(self):
        driver = FixtureDriver()
        provider = FixtureReranker(driver)
        result = Neo4jRetrievalEngine(driver, reranker=provider).retrieve(self.request())
        self.assertEqual(driver.reads, 2)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(result.chunks[0].citation.chunk_id, "chunk-002")
        self.assertEqual(result.chunks[0].score, 2 / 3)
        self.assertEqual(result.trace.final_ranking[0].chunk_id, "chunk-000")
        self.assertNotEqual(result.chunks[0].score, result.trace.final_ranking[0].score)
        self.assertEqual(result.trace.reranking.ranked_chunk_ids, ("chunk-002", "chunk-001", "chunk-000"))
        for c in provider.calls[0][1]:
            original = driver.rows[c.chunk_id]
            self.assertEqual((c.text, c.checksum, c.source_title, c.source_section, c.document_id, c.version_id),
                (original["text"], original["chunk_checksum"], original["document_title"], original["section"], original["document_id"], original["version_id"]))
        final_hydrate = [p for read, query, p in driver.queries if read == 2 and query == HYDRATE_QUERY][0]
        self.assertEqual(set(final_hydrate["chunk_ids"]), set(driver.rows))
        self.assertEqual(final_hydrate["tenant_id"], "tenant-test")
        self.assertEqual(final_hydrate["groups"], ["engineering"])

    def test_listwise_rank_is_canonical_and_does_not_invent_scores(self):
        driver = FixtureDriver()
        result = Neo4jRetrievalEngine(driver, reranker=FixtureReranker(driver, unscored=True)).retrieve(self.request())
        self.assertEqual(result.chunks[0].citation.chunk_id, "chunk-002")
        self.assertIsNone(result.chunks[0].score)
        self.assertTrue(all(s.score is None for s in result.trace.reranking.response.scores))

    def test_source_and_publication_changes_fail_after_one_call_without_paid_retry(self):
        changes = (
            lambda d: d.state.update(corpus_revision=5),
            lambda d: d.state.update(generation_id="g2"),
            lambda d: d.state.update(knowledge_publication_id="pub2", knowledge_activation_generation=2),
            lambda d: d.state.update(knowledge_activation_generation=3),
            lambda d: d.revoked.add("chunk-000"),
            lambda d: d.rows["chunk-000"].update(document_title="Changed record identity"),
        )
        for change in changes:
            with self.subTest(change=change):
                driver = FixtureDriver()
                provider = FixtureReranker(driver, after=change)
                with self.assertRaisesRegex(RerankAttemptedFailure, "after a provider attempt"):
                    Neo4jRetrievalEngine(driver, reranker=provider).retrieve(self.request())
                self.assertEqual(len(provider.calls), 1)
                self.assertEqual(driver.reads, 2)

    def test_trace_only_chunk_is_reauthorized_even_when_not_sent_to_model(self):
        driver = FixtureDriver()
        provider = FixtureReranker(driver, after=lambda d: d.revoked.add("chunk-002"))
        with self.assertRaises(RerankAttemptedFailure):
            Neo4jRetrievalEngine(driver, reranker=provider, rerank_candidate_limit=2).retrieve(self.request())
        self.assertEqual(len(provider.calls[0][1]), 2)
        self.assertNotIn("chunk-002", {c.chunk_id for c in provider.calls[0][1]})

    def test_candidate_and_context_caps_remain_independent(self):
        driver = FixtureDriver(55)
        provider = FixtureReranker(driver)
        request = self.request(limits=RetrievalLimits(top_k=1, anchor_k=1, adjacent_window=0,
            vector_recall_k=60, bm25_recall_k=60, candidate_limit=100, max_context_chars=256))
        result = Neo4jRetrievalEngine(driver, reranker=provider).retrieve(request)
        self.assertEqual(len(provider.calls[0][1]), 50)
        self.assertEqual(len(result.chunks), 1)
        self.assertLessEqual(result.trace.context_chars, 256)
        self.assertEqual(result.trace.selected_chunk_ids, ("chunk-049",))
        self.assertTrue(any(d.reason == "rerank_candidate_limit" for d in result.trace.decisions))

    def test_provider_failure_never_silently_falls_back(self):
        for error in (RerankingError("sanitized"), TimeoutError(), ValueError("invalid")):
            with self.subTest(error=type(error).__name__):
                driver = FixtureDriver()
                provider = FixtureReranker(driver, error=error)
                with self.assertRaises(RerankAttemptedFailure) as caught:
                    Neo4jRetrievalEngine(driver, reranker=provider).retrieve(self.request())
                self.assertEqual(caught.exception.timeout, isinstance(error, TimeoutError))
                self.assertEqual(len(provider.calls), 1)
                self.assertEqual(driver.reads, 1)

    def test_empty_scope_never_calls_model_and_omitted_provider_preserves_legacy_order(self):
        driver = FixtureDriver()
        provider = FixtureReranker(driver)
        result = Neo4jRetrievalEngine(driver, reranker=provider).retrieve(self.request(version_filter=VersionFilter(match_none=True)))
        self.assertEqual(result.chunks, ())
        self.assertEqual(result.trace.reranking.status, "SKIPPED_EMPTY")
        self.assertFalse(provider.calls)
        legacy = Neo4jRetrievalEngine(FixtureDriver()).retrieve(self.request())
        self.assertEqual(legacy.chunks[0].citation.chunk_id, "chunk-000")
        self.assertIsNone(legacy.trace.reranking)

    def test_server_scope_context_changes_only_provider_query(self):
        driver = FixtureDriver()
        provider = FixtureReranker(driver)
        context = "User-selected equipment scope: HVX-A01"
        request = self.request(rerank_context=context)
        Neo4jRetrievalEngine(driver, reranker=provider).retrieve(request)
        self.assertEqual(provider.calls[0][0], request.query_text + "\n" + context)
        bm25 = next(p for _, q, p in driver.queries if q == BM25_RECALL_QUERY)
        self.assertNotIn("HVX", bm25["lucene_query"])

    def test_candidate_checksum_and_rank_identity_validation(self):
        with self.assertRaises(ValueError):
            RerankCandidate("chunk-1", "raw evidence", "0" * 64)
        for score in (True, float("nan"), float("inf")):
            with self.subTest(score=score), self.assertRaises(ValueError):
                RerankScore("chunk-1", 0, score)
        driver = FixtureDriver()
        provider = FixtureReranker(driver)
        original = provider.rerank
        def wrong_identity(query, candidates):
            raw = original(query, candidates)
            return replace(raw, scores=(replace(raw.scores[0], chunk_id="foreign-chunk"), *raw.scores[1:]))
        provider.rerank = wrong_identity
        with self.assertRaises(RerankAttemptedFailure):
            Neo4jRetrievalEngine(driver, reranker=provider).retrieve(self.request())
        self.assertEqual(driver.reads, 1)

    def test_change_during_final_hydration_is_detected_by_last_state_check(self):
        driver = FixtureDriver()
        provider = FixtureReranker(driver)
        original_run = driver.run
        def run(query, **parameters):
            result = original_run(query, **parameters)
            if driver.reads == 2 and query == HYDRATE_QUERY:
                driver.state["knowledge_activation_generation"] += 1
            return result
        driver.run = run
        with self.assertRaises(RerankAttemptedFailure):
            Neo4jRetrievalEngine(driver, reranker=provider).retrieve(self.request())
        self.assertEqual(len(provider.calls), 1)

    def test_listwise_normalization_can_only_stably_remove_duplicates_from_complete_rank(self):
        raw = response(tuple(RerankCandidate(str(i), f"text {i}", digest(f"text {i}")) for i in range(3)), unscored=True)
        valid = replace(raw, normalization_method="stable-deduplicate-complete-permutation:v1",
            raw_permutation=(2, 1, 1, 0), normalization_removed_count=1, raw_output_checksum="c" * 64)
        self.assertEqual(tuple(s.index for s in valid.scores), (2, 1, 0))
        for permutation in ((2, 1, 1), (2, 0, 1), (2, 1, 4, 0), (2, True, 1, 0)):
            with self.subTest(permutation=permutation), self.assertRaises(ValueError):
                replace(valid, raw_permutation=permutation)


if __name__ == "__main__":
    unittest.main()
