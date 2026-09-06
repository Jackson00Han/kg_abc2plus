"""Provider boundary tests use local fixtures and never call a model."""

from dataclasses import replace
import hashlib
import json
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

import httpx

from graphrag_prod.retrieval import _rerank_transport as wire
from graphrag_prod.retrieval import rerank_provider as provider
from graphrag_prod.retrieval.reranking import RerankCandidate


def checksum(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def candidate(identifier, text, **metadata):
    return RerankCandidate(identifier, text, checksum(text), **metadata)


def candidates():
    return (candidate("chunk-a", "合成记录：收到远方请求，未发出合闸输出。\n", source_title="控制记录", source_section="事件"),
            candidate("chunk-b", "合成记录：未获得可适用的制造商阈值。", source_title="复核记录", source_section="缺少证据"))


def response():
    return {"object": "list", "model": "qwen3-rerank", "id": "request-fixture-1",
            "results": [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.2}],
            "usage": {"total_tokens": 84}}


class Parts(httpx.SyncByteStream):
    def __init__(self, *parts):
        self.parts = parts

    def __iter__(self):
        yield from self.parts


class RerankProviderTests(unittest.TestCase):
    def test_complete_intact_default_request_and_auditable_usage(self):
        calls = []

        def transport(envelope, deadline):
            calls.append((wire.parse_json(envelope), deadline))
            return wire.canonical_bytes(response())

        reader = provider.DashScopeReranker("fixture-key", transport=transport)
        original = candidates()
        result = reader.rerank("是否已经确认原因？", original)
        self.assertEqual(len(calls), 1)
        request = calls[0][0]["request"]
        self.assertEqual(set(request), {"model", "query", "documents", "top_n"})
        self.assertEqual(request["documents"], [item.text for item in original])
        self.assertEqual(request["top_n"], 2)
        self.assertLessEqual(calls[0][1], 30)
        self.assertEqual([score.chunk_id for score in result.scores], ["chunk-b", "chunk-a"])
        self.assertEqual(result.source_checksums, tuple(item.checksum for item in original))
        self.assertEqual(result.rendered_input_checksums, result.source_checksums)
        self.assertEqual(result.input_checksum, hashlib.sha256(wire.canonical_bytes(request)).hexdigest())
        metadata = provider.response_metadata(result)
        self.assertEqual(metadata["usage"], {"total_tokens": 84})
        self.assertEqual(metadata["input_bytes"], len(wire.canonical_bytes(request)))
        self.assertNotIn("fixture-key", json.dumps(metadata))
        self.assertEqual(metadata["model_version_kind"], "provider_alias")
        self.assertIsNone(provider.provider_configuration()["fallback"])
        provider.validate_cached_response("是否已经确认原因？", original, result)

    def test_context_profile_pins_rendering_and_preserves_source(self):
        calls = []

        def transport(envelope, _):
            calls.append(wire.parse_json(envelope)["request"])
            return wire.canonical_bytes(response())

        original = candidates()
        reader = provider.DashScopeReranker("fixture-key", profile=provider.CONTEXTUAL_PROFILE, transport=transport)
        result = reader.rerank("复核原因", original)
        expected = "Source document: 控制记录\nSource section: 事件\nPassage:\n" + original[0].text
        self.assertEqual(calls[0]["documents"][0], expected)
        self.assertEqual(calls[0]["instruct"], provider.CONTEXTUAL_INSTRUCT)
        self.assertEqual(result.rendered_input_checksums[0], checksum(expected))
        self.assertEqual(result.source_checksums[0], original[0].checksum)
        self.assertNotEqual(result.source_checksums[0], result.rendered_input_checksums[0])
        provider.validate_cached_response("复核原因", original, result, profile=provider.CONTEXTUAL_PROFILE)
        with self.assertRaises(provider.RerankProviderError):
            provider.validate_cached_response("复核原因", original, result)

    def test_cached_identity_rechecks_query_order_metadata_and_score_checksum(self):
        source = candidates()
        result = provider.decode_response(wire.canonical_bytes(response()), "query", source, duration_ms=2)
        base = provider.rerank_cache_identity("query", source)
        self.assertNotEqual(base, provider.rerank_cache_identity("other", source))
        self.assertNotEqual(base, provider.rerank_cache_identity("query", tuple(reversed(source))))
        self.assertNotEqual(base, provider.rerank_cache_identity("query", source, profile=provider.CONTEXTUAL_PROFILE))
        changed = (replace(source[0], source_title="另一个记录"), source[1])
        self.assertNotEqual(provider.rerank_cache_identity("query", source, profile=provider.CONTEXTUAL_PROFILE),
                            provider.rerank_cache_identity("query", changed, profile=provider.CONTEXTUAL_PROFILE))
        for query, values, item in (
            ("other", source, result), ("query", tuple(reversed(source)), result),
            ("query", source, replace(result, output_checksum="0" * 64)),
            ("query", source, replace(result, endpoint="https://example.invalid/reranks")),
            ("query", source, replace(result, source_checksums=tuple(reversed(result.source_checksums)))),
            ("query", source, replace(result, scores=(replace(result.scores[0], score=0.8), result.scores[1]))),
        ):
            with self.subTest(query=query, item=item.output_checksum):
                with self.assertRaises(provider.RerankProviderError):
                    provider.validate_cached_response(query, values, item)

    def test_equally_scored_provider_order_is_preserved(self):
        payload = response()
        for row in payload["results"]:
            row["relevance_score"] = 0.5
        result = provider.decode_response(wire.canonical_bytes(payload), "query", candidates(), duration_ms=1)
        self.assertEqual([item.index for item in result.scores], [1, 0])

    def test_malformed_or_incomplete_results_fail_without_fallback(self):
        mutations = (
            lambda raw: raw.update(model="other-model"),
            lambda raw: raw.update(object="wrong"),
            lambda raw: raw.update(output={"results": raw["results"]}),
            lambda raw: raw.update(error="remote-sensitive-value"),
            lambda raw: raw["results"].pop(),
            lambda raw: raw["results"][0].update(index=False),
            lambda raw: raw["results"][0].update(index=1.0),
            lambda raw: raw["results"][0].update(index=2),
            lambda raw: raw["results"][1].update(index=1),
            lambda raw: raw["results"][0].update(relevance_score=True),
            lambda raw: raw["results"][0].update(relevance_score=1.01),
            lambda raw: raw["results"][0].update(relevance_score=float("nan")),
            lambda raw: raw["results"][0].update(relevance_score=float("inf")),
            lambda raw: raw["results"][0].update(relevance_score=-0.1),
            lambda raw: raw["results"].reverse(),
            lambda raw: raw["results"][0].update(document={"text": "invented value"}),
            lambda raw: raw["usage"].update(total_tokens=True),
            lambda raw: raw["usage"].update(total_tokens=-1),
            lambda raw: raw.update(id="secret\nnew-line"),
        )
        for mutate in mutations:
            raw = response()
            mutate(raw)
            with self.subTest(mutation=mutate):
                with self.assertRaises(provider.RerankProviderError) as caught:
                    provider.decode_response(json.dumps(raw).encode(), "query", candidates(), duration_ms=1)
                self.assertNotIn("remote-sensitive-value", str(caught.exception))
        duplicate = wire.canonical_bytes(response()).replace(b'"object":"list"', b'"object":"list","object":"list"')
        with self.assertRaises(provider.RerankProviderError):
            provider.decode_response(duplicate, "query", candidates(), duration_ms=1)

    def test_whole_text_item_and_repeated_query_budgets_are_failures_not_truncation(self):
        calls = []
        reader = provider.DashScopeReranker("fixture-key", transport=lambda *args: calls.append(args))
        for query, values in (
            ("query", ()), ("query", (candidates()[0], candidates()[0])),
            ("query", tuple(candidate(str(i), "a") for i in range(51))),
            ("中" * 1_201, candidates()),
            ("query", (candidate("long", "中" * 1_201),)),
            ("q" * 2_000, tuple(candidate(str(i), "a") for i in range(50))),
        ):
            with self.assertRaises(provider.RerankProviderError):
                reader.rerank(query, values)
        self.assertEqual(calls, [])
        with self.assertRaises(ValueError):
            provider.RerankProviderProfile(instruct="x" * 2_001)
        with self.assertRaises(provider.RerankProviderError):
            provider.get_provider_profile("unreviewed-profile")
        request = {"model": wire.MODEL, "query": "q", "documents": ["d", "e"],
                   "top_n": 2, "instruct": "instruction"}
        self.assertEqual(wire.validate_request(request), 2 * (1 + len("instruction")) + 2)

    def test_concurrency_is_global_fail_fast_and_provider_errors_are_sanitized(self):
        entered, release = threading.Event(), threading.Event()
        errors = []

        def waiting(envelope, deadline):
            entered.set()
            release.wait(2)
            return wire.canonical_bytes(response())

        first = provider.DashScopeReranker("first-fixture", transport=waiting)

        def run():
            try:
                first.rerank("query", candidates())
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(entered.wait(1))
        try:
            second = provider.DashScopeReranker("second-fixture", transport=waiting)
            with self.assertRaisesRegex(provider.RerankProviderError, "RERANK_BUSY"):
                second.rerank("query", candidates())
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

        def failed(*args):
            raise RuntimeError("secret-fixture-in-provider-error")

        with self.assertRaisesRegex(provider.RerankProviderError, "^RERANK_TRANSPORT_FAILED$"):
            provider.DashScopeReranker("fixture-key", transport=failed).rerank("query", candidates())

    def test_real_subprocess_deadline_covers_blocked_stdin_and_reaps_child(self):
        real_popen, children, launches = subprocess.Popen, [], []

        def launch(args, **kwargs):
            launches.append((args, kwargs))
            child = real_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
            children.append(child)
            return child

        started = time.monotonic()
        with patch.object(provider.subprocess, "Popen", side_effect=launch):
            with self.assertRaisesRegex(provider.RerankProviderError, "RERANK_TIMEOUT"):
                provider._subprocess_request(b"x" * 262_144, 0.1)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        self.assertEqual(launches[0][0], [sys.executable, "-m", provider._WORKER_MODULE])
        self.assertNotIn("OPENAI_API_KEY", launches[0][1]["env"])
        self.assertEqual(launches[0][1]["stderr"], subprocess.DEVNULL)

    def test_stream_limits_redirects_encoding_and_errors_make_only_one_http_attempt(self):
        request = {"model": wire.MODEL, "query": "q", "documents": ["d"], "top_n": 1}
        scenarios = (
            (302, {"location": "https://example.invalid/private"}, (b"do not follow",), wire.EXIT_HTTP),
            (401, {}, (b"credential echoed in remote error",), wire.EXIT_AUTH),
            (429, {}, (b"retry later",), wire.EXIT_RATE_LIMIT),
            (200, {"content-encoding": "gzip"}, (b"compressed",), wire.EXIT_RESPONSE_LIMIT),
            (200, {"content-length": str(wire.MAX_RESPONSE_BYTES + 1)}, (b"x",), wire.EXIT_RESPONSE_LIMIT),
            (200, {}, (b"x" * wire.MAX_RESPONSE_BYTES, b"x"), wire.EXIT_RESPONSE_LIMIT),
        )
        for status, headers, parts, expected in scenarios:
            calls = []

            def handler(actual):
                calls.append(actual)
                return httpx.Response(status, headers=headers, stream=Parts(*parts))

            with self.subTest(status=status, headers=headers):
                with self.assertRaises(wire.TransportFailure) as caught:
                    wire.perform_request("fixture-key", request, transport=httpx.MockTransport(handler))
                self.assertEqual(caught.exception.status, expected)
                self.assertEqual(len(calls), 1)
                self.assertEqual(str(calls[0].url), wire.ENDPOINT)
                self.assertEqual(calls[0].headers["accept-encoding"], "identity")
                self.assertNotIn("credential", str(caught.exception))

    def test_stream_success_and_parent_exit_categories(self):
        request = {"model": wire.MODEL, "query": "q", "documents": ["d"], "top_n": 1}
        expected = b'{"fixture":true}'
        transport = httpx.MockTransport(lambda _: httpx.Response(200, stream=Parts(expected[:4], expected[4:])))
        self.assertEqual(wire.perform_request("fixture-key", request, transport=transport), expected)
        real_popen = subprocess.Popen

        def launch(args, **kwargs):
            return real_popen([sys.executable, "-c", "import sys; sys.exit(13)"], **kwargs)

        with patch.object(provider.subprocess, "Popen", side_effect=launch):
            with self.assertRaisesRegex(provider.RerankProviderError, "RERANK_RATE_LIMIT"):
                provider._subprocess_request(b"{}", 2)


if __name__ == "__main__":
    unittest.main()
