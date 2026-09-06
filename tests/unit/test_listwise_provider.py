"""Pure-order ranking, declared duplicate handling, and private raw capture checks."""

from dataclasses import replace
import json
import unittest

import httpx

from graphrag_prod.retrieval._rerank_transport import canonical_bytes, parse_json
from graphrag_prod.retrieval._listwise_transport import perform_listwise_request, validate_listwise_request
from graphrag_prod.retrieval.listwise_provider import (
    MODEL, ENDPOINT, LISTWISE_PROFILE, LISTWISE_V3_PROFILE, SYSTEM_EVIDENCE_V1,
    NORMALIZATION, DashScopeListwiseReranker, decode_listwise_response,
)
from graphrag_prod.retrieval.rerank_provider import (
    RerankProviderError, create_reranker, provider_configuration, rerank_cache_identity,
    response_metadata, validate_cached_response,
)
from tests.unit.test_rerank_provider import candidates, checksum


def response(ranking=None):
    return {"id": "chatcmpl-fixture-123", "object": "chat.completion", "model": MODEL,
        "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": json.dumps({"ranking": [1, 0] if ranking is None else ranking})}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 12, "total_tokens": 132}}


class ListwiseProviderTests(unittest.TestCase):
    def test_selected_profile_wire_request_matches_explicit_experiment(self):
        calls = []
        raw = canonical_bytes(response())

        def transport(envelope, deadline):
            calls.append((parse_json(envelope), deadline))
            return raw

        reader = create_reranker("fixture-key", profile=LISTWISE_PROFILE, transport=transport)
        self.assertIsInstance(reader, DashScopeListwiseReranker)
        result, private_raw = reader.rerank_with_raw("查询\nUser-selected equipment scope: A-01", candidates())
        self.assertEqual(private_raw, raw)
        self.assertEqual(len(calls), 1)
        request = calls[0][0]["request"]
        self.assertEqual(request["model"], MODEL)
        self.assertEqual(request["messages"][0], {"role": "system", "content": SYSTEM_EVIDENCE_V1})
        content = json.loads(request["messages"][1]["content"])
        self.assertEqual(content["query"], "查询\nUser-selected equipment scope: A-01")
        self.assertEqual(content["passages"][0], {"index": 0, "content":
            "Source document: 控制记录\nSource section: 事件\nPassage:\n" + candidates()[0].text})
        self.assertEqual(request["temperature"], 0)
        self.assertEqual(request["max_tokens"], 1_024)
        self.assertIs(request["enable_thinking"], False)
        self.assertEqual(request["response_format"], {"type": "json_object"})
        self.assertEqual([score.index for score in result.scores], [1, 0])
        self.assertTrue(all(score.score is None for score in result.scores))
        self.assertEqual(result.raw_permutation, (1, 0))
        self.assertEqual(result.normalization_removed_count, 0)
        self.assertEqual(result.normalization_method, NORMALIZATION)
        self.assertEqual(result.raw_output_checksum, checksum(raw.decode()))
        self.assertEqual(result.input_tokens, 120)
        self.assertEqual(result.output_tokens, 12)
        self.assertEqual(response_metadata(result, profile=LISTWISE_PROFILE)["usage"], {
            "prompt_tokens": 120, "completion_tokens": 12, "total_tokens": 132})
        self.assertEqual(provider_configuration(LISTWISE_PROFILE)["query_context_version"], "user-selected-equipment:v1")
        self.assertEqual(provider_configuration(LISTWISE_PROFILE)["endpoint"], ENDPOINT)

    def test_duplicate_removal_is_stable_complete_and_recorded(self):
        raw = canonical_bytes(response([1, 0, 1]))
        result = decode_listwise_response(raw, "query", candidates(), duration_ms=2)
        self.assertEqual(result.raw_permutation, (1, 0, 1))
        self.assertEqual([score.index for score in result.scores], [1, 0])
        self.assertEqual(result.normalization_removed_count, 1)
        validate_cached_response("query", candidates(), result, profile=LISTWISE_PROFILE, raw_response_json=raw.decode())
        clean = decode_listwise_response(canonical_bytes(response([1, 0])), "query", candidates(), duration_ms=2)
        self.assertNotEqual(result.raw_output_checksum, clean.raw_output_checksum)
        self.assertNotEqual(result.output_checksum, clean.output_checksum)

    def test_missing_out_of_range_or_bool_indices_are_never_repaired(self):
        for invalid in ([1], [1, 1], [1, 0, 2], [1, False], [1, 0.0], [1, -1],
                        [1, 0, 1, 0, 1], ["1", 0], []):
            with self.subTest(ranking=invalid):
                with self.assertRaises(RerankProviderError):
                    decode_listwise_response(canonical_bytes(response(invalid)), "query", candidates(), duration_ms=1)

    def test_incomplete_completion_and_wrong_usage_fail_once_without_fallback(self):
        mutations = (
            lambda item: item.update(model="another-model"),
            lambda item: item["choices"][0].update(finish_reason="length"),
            lambda item: item["choices"][0].update(index=False),
            lambda item: item["choices"].append(item["choices"][0]),
            lambda item: item["choices"][0]["message"].update(content='{"ranking":[1,0],"explanation":"extra"}'),
            lambda item: item["choices"][0]["message"].update(content='{"ranking":[1,0],"ranking":[1,0]}'),
            lambda item: item["choices"][0]["message"].update(content='```json\n{"ranking":[1,0]}\n```'),
            lambda item: item["choices"][0]["message"].update(refusal="cannot rank"),
            lambda item: item["choices"][0]["message"].update(reasoning_content="unexpected hidden output"),
            lambda item: item["usage"].update(prompt_tokens=True),
            lambda item: item["usage"].update(prompt_tokens=None),
            lambda item: item["usage"].update(total_tokens=999),
            lambda item: item["usage"].update(completion_tokens=1_025, total_tokens=1_145),
        )
        for mutate in mutations:
            item = response()
            mutate(item)
            calls = []

            def transport(*args):
                calls.append(args)
                return canonical_bytes(item)

            with self.subTest(mutation=mutate):
                with self.assertRaisesRegex(RerankProviderError, "RERANK_INVALID_RESPONSE"):
                    DashScopeListwiseReranker("fixture-key", transport=transport).rerank("query", candidates())
                self.assertEqual(len(calls), 1)

    def test_private_original_payload_and_normalized_hashes_are_both_verified(self):
        raw = canonical_bytes(response([1, 0, 1]))
        result = decode_listwise_response(raw, "query", candidates(), duration_ms=2)
        with self.assertRaisesRegex(RerankProviderError, "RERANK_RAW_RESPONSE_REQUIRED"):
            validate_cached_response("query", candidates(), result, profile=LISTWISE_PROFILE)
        for values, changed, body in (
            (candidates(), replace(result, output_checksum="0" * 64), raw.decode()),
            (candidates(), result, raw.decode() + " "),
            (candidates(), result, canonical_bytes(response([0, 1, 0])).decode()),
            (tuple(reversed(candidates())), result, raw.decode()),
            ((replace(candidates()[0], source_title="另一个标题"), candidates()[1]), result, raw.decode()),
        ):
            with self.subTest(body=body[-10:]):
                with self.assertRaises(RerankProviderError):
                    validate_cached_response("query", values, changed, profile=LISTWISE_PROFILE, raw_response_json=body)
        self.assertNotEqual(rerank_cache_identity("query", candidates(), profile=LISTWISE_PROFILE),
                            rerank_cache_identity("query", candidates(), profile=LISTWISE_V3_PROFILE))
        with self.assertRaises(RerankProviderError):
            validate_cached_response("query", candidates(), result, profile=LISTWISE_V3_PROFILE, raw_response_json=raw.decode())

    def test_transport_validator_rejects_changed_model_endpoint_controls_and_budget(self):
        calls = []

        def transport(envelope, _):
            calls.append(parse_json(envelope)["request"])
            return canonical_bytes(response())

        DashScopeListwiseReranker("fixture-key", transport=transport).rerank("query", candidates())
        request = calls[0]
        self.assertGreater(validate_listwise_request(request), 0)
        for field, value in (("model", "other"), ("temperature", True), ("max_tokens", 2_048),
                             ("enable_thinking", True), ("response_format", {"type": "text"})):
            with self.assertRaises(ValueError):
                validate_listwise_request({**request, field: value})
        with self.assertRaises(RerankProviderError):
            create_reranker("fixture-key", profile="listwise-unknown")

    def test_listwise_read_timeout_allows_model_work_inside_parent_deadline(self):
        requests = []

        class Stream(httpx.SyncByteStream):
            def __iter__(self):
                yield canonical_bytes(response())

        def handler(request):
            requests.append(request)
            self.assertEqual(str(request.url), ENDPOINT)
            self.assertEqual(request.extensions["timeout"], {
                "connect": 5.0, "read": 25.0, "write": 5.0, "pool": 1.0})
            return httpx.Response(200, stream=Stream())

        def transport(envelope, deadline):
            self.assertLessEqual(deadline, 30)
            value = parse_json(envelope)
            return perform_listwise_request(value["api_key"], value["request"],
                                             transport=httpx.MockTransport(handler))

        result = DashScopeListwiseReranker("fixture-key", transport=transport).rerank("query", candidates())
        self.assertEqual(len(requests), 1)
        self.assertEqual(result.candidate_count, 2)


if __name__ == "__main__":
    unittest.main()
