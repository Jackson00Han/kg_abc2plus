"""Private bounded transport for the explicitly selected listwise experiment."""

from __future__ import annotations

import sys
from typing import Any

import httpx

from ._rerank_transport import (
    MAX_CANDIDATES, MAX_ITEM_UTF8_BYTES, MAX_PAIR_UTF8_BYTES,
    MAX_WORKER_REQUEST_BYTES, EXIT_INPUT, TransportFailure,
    canonical_bytes, parse_json, _perform_http_request,
)


ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
MODEL = "qwen3.8-max"
MAX_PROMPT_UTF8_BYTES = 90_000
MAX_SYSTEM_UTF8_BYTES = 4_000
READ_TIMEOUT_SECONDS = 25.0


def validate_listwise_request(request: Any) -> int:
    if (not isinstance(request, dict) or set(request) != {
        "model", "messages", "temperature", "max_tokens", "enable_thinking", "response_format",
    } or request["model"] != MODEL or type(request["temperature"]) is not int
            or request["temperature"] != 0 or type(request["max_tokens"]) is not int
            or request["max_tokens"] != 1_024 or request["enable_thinking"] is not False
            or request["response_format"] != {"type": "json_object"}):
        raise ValueError("unsupported listwise request")
    messages = request["messages"]
    if not isinstance(messages, list) or len(messages) != 2:
        raise ValueError("listwise request requires two messages")
    for item, role in zip(messages, ("system", "user"), strict=True):
        if (not isinstance(item, dict) or set(item) != {"role", "content"} or item["role"] != role
                or not isinstance(item["content"], str) or not item["content"].strip() or "\x00" in item["content"]):
            raise ValueError("invalid listwise message")
    if len(messages[0]["content"].encode("utf-8")) > MAX_SYSTEM_UTF8_BYTES:
        raise ValueError("listwise instruction exceeds bounds")
    if len(canonical_bytes(request)) > MAX_PROMPT_UTF8_BYTES:
        raise ValueError("listwise complete HTTP body exceeds UTF8 budget")
    source = parse_json(messages[1]["content"].encode("utf-8"))
    if not isinstance(source, dict) or set(source) != {"query", "passages"}:
        raise ValueError("invalid listwise source envelope")
    query, passages = source["query"], source["passages"]
    if not isinstance(passages, list) or not 1 <= len(passages) <= MAX_CANDIDATES:
        raise ValueError("invalid listwise candidate count")
    if not isinstance(query, str) or not query.strip() or "\x00" in query:
        raise ValueError("invalid listwise query")
    query_bytes = len(query.encode("utf-8"))
    if query_bytes > MAX_ITEM_UTF8_BYTES:
        raise ValueError("listwise query exceeds UTF8 budget")
    paired = query_bytes * len(passages)
    for index, item in enumerate(passages):
        if (not isinstance(item, dict) or set(item) != {"index", "content"}
                or type(item["index"]) is not int or item["index"] != index
                or not isinstance(item["content"], str) or not item["content"].strip() or "\x00" in item["content"]):
            raise ValueError("invalid listwise candidate")
        size = len(item["content"].encode("utf-8"))
        if size > MAX_ITEM_UTF8_BYTES:
            raise ValueError("listwise whole candidate exceeds UTF8 budget")
        paired += size
    if paired > MAX_PAIR_UTF8_BYTES:
        raise ValueError("listwise conservative repeated-query UTF8 budget exceeded")
    return paired


def perform_listwise_request(api_key: str, request: dict[str, Any], *,
                             transport: httpx.BaseTransport | None = None) -> bytes:
    validate_listwise_request(request)
    return _perform_http_request(api_key, request, ENDPOINT,
                                 read_timeout_seconds=READ_TIMEOUT_SECONDS, transport=transport)


def main() -> int:
    try:
        encoded = sys.stdin.buffer.read(MAX_WORKER_REQUEST_BYTES + 1)
        if len(encoded) > MAX_WORKER_REQUEST_BYTES:
            return EXIT_INPUT
        value = parse_json(encoded)
        if not isinstance(value, dict) or set(value) != {"api_key", "request"}:
            return EXIT_INPUT
        response = perform_listwise_request(value["api_key"], value["request"])
        sys.stdout.buffer.write(response)
        sys.stdout.buffer.flush()
        return 0
    except TransportFailure as error:
        return error.status
    except Exception:
        return EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
