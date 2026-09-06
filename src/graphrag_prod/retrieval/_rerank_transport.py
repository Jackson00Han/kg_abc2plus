"""Private, single-request HTTPS worker for the fixed Beijing rerank endpoint.

The parent owns the wall-clock deadline and kills/reaps this process. Credentials
arrive on stdin, never in argv, stdout, stderr, or a provider error message.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import httpx


ENDPOINT = "https://dashscope.aliyuncs.com/compatible-api/v1/reranks"
MODEL = "qwen3-rerank"
# Omitted on the wire: the provider's documented default QA behavior. Explicit
# instruction experiments require a different profile and cache identity.
INSTRUCT = None
MAX_CANDIDATES = 50
MAX_ITEM_UTF8_BYTES = 3_600
MAX_PAIR_UTF8_BYTES = 90_000
MAX_RESPONSE_BYTES = 256 * 1024
MAX_WORKER_REQUEST_BYTES = 512 * 1024
MAX_INSTRUCT_UTF8_BYTES = 2_000

# Exit statuses convey only a fixed category, never a remote error body.
EXIT_NETWORK = 10
EXIT_HTTP = 11
EXIT_AUTH = 12
EXIT_RATE_LIMIT = 13
EXIT_RESPONSE_LIMIT = 14
EXIT_INPUT = 15


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def parse_json(payload: bytes) -> Any:
    def reject_constant(_: str) -> None:
        raise ValueError("non-finite JSON number")

    return json.loads(payload.decode("utf-8"), object_pairs_hook=unique_object,
                      parse_constant=reject_constant)


def validate_request(request: Any) -> int:
    """Return repeated-query UTF8 bytes; this value is not a token count."""
    required = {"model", "query", "documents", "top_n"}
    if (not isinstance(request, dict) or not required <= set(request)
            or set(request) - required - {"instruct"} or request["model"] != MODEL):
        raise ValueError("unsupported rerank request")
    instruction_bytes = 0
    if "instruct" in request:
        instruction = request["instruct"]
        if (not isinstance(instruction, str) or not instruction.strip()
                or any(ord(char) < 32 for char in instruction)):
            raise ValueError("invalid explicit instruction")
        instruction_bytes = len(instruction.encode("utf-8"))
        if instruction_bytes > MAX_INSTRUCT_UTF8_BYTES:
            raise ValueError("instruction exceeds UTF8 budget")
    documents = request["documents"]
    if (not isinstance(documents, list) or not 1 <= len(documents) <= MAX_CANDIDATES
            or type(request["top_n"]) is not int or request["top_n"] != len(documents)):
        raise ValueError("rerank candidates exceed bounds")
    lengths = []
    for text in [request["query"], *documents]:
        if not isinstance(text, str) or not text.strip() or "\x00" in text:
            raise ValueError("rerank text must be nonempty")
        length = len(text.encode("utf-8"))
        if length > MAX_ITEM_UTF8_BYTES:
            raise ValueError("rerank item exceeds UTF8 byte budget")
        lengths.append(length)
    paired = (lengths[0] + instruction_bytes) * len(documents) + sum(lengths[1:])
    if paired > MAX_PAIR_UTF8_BYTES:
        raise ValueError("rerank repeated-query UTF8 budget exceeded")
    return paired


def valid_api_key(value: Any) -> bool:
    return (isinstance(value, str) and 1 <= len(value) <= 4_096
            and value.isascii() and all(33 <= ord(char) <= 126 for char in value))


class TransportFailure(Exception):
    def __init__(self, status: int) -> None:
        super().__init__("rerank transport failed")
        self.status = status


def perform_request(api_key: str, request: dict[str, Any], *,
                    transport: httpx.BaseTransport | None = None) -> bytes:
    """Stream a bounded response without redirects, retries, or decompression."""
    validate_request(request)
    return _perform_http_request(api_key, request, ENDPOINT, transport=transport)


def _perform_http_request(api_key: str, request: dict[str, Any], endpoint: str, *,
                          read_timeout_seconds: float = 10.0,
                          transport: httpx.BaseTransport | None = None) -> bytes:
    """Shared internal transport; callers validate their fixed model/endpoint."""
    if not valid_api_key(api_key):
        raise TransportFailure(EXIT_INPUT)
    # A short per-operation timeout complements, but does not replace, the
    # parent's deadline covering connection, request write, and every read.
    try:
        with httpx.Client(
            transport=transport or httpx.HTTPTransport(retries=0),
            timeout=httpx.Timeout(connect=5.0, read=read_timeout_seconds, write=5.0, pool=1.0),
            follow_redirects=False, trust_env=False,
        ) as client:
            with client.stream("POST", endpoint, content=canonical_bytes(request),
                    headers={"Authorization": "Bearer " + api_key,
                             "Content-Type": "application/json",
                             "Accept": "application/json", "Accept-Encoding": "identity"}) as response:
                if response.status_code != 200:
                    code = (EXIT_AUTH if response.status_code in {401, 403} else
                            EXIT_RATE_LIMIT if response.status_code == 429 else EXIT_HTTP)
                    raise TransportFailure(code)
                if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
                    raise TransportFailure(EXIT_RESPONSE_LIMIT)
                length = response.headers.get("content-length")
                if length is not None and (not length.isdecimal() or int(length) > MAX_RESPONSE_BYTES):
                    raise TransportFailure(EXIT_RESPONSE_LIMIT)
                payload = bytearray()
                for part in response.iter_raw(chunk_size=16 * 1024):
                    if len(payload) + len(part) > MAX_RESPONSE_BYTES:
                        raise TransportFailure(EXIT_RESPONSE_LIMIT)
                    payload.extend(part)
                return bytes(payload)
    except httpx.HTTPError:
        raise TransportFailure(EXIT_NETWORK) from None


def main() -> int:
    try:
        encoded = sys.stdin.buffer.read(MAX_WORKER_REQUEST_BYTES + 1)
        if len(encoded) > MAX_WORKER_REQUEST_BYTES:
            return EXIT_INPUT
        value = parse_json(encoded)
        if not isinstance(value, dict) or set(value) != {"api_key", "request"}:
            return EXIT_INPUT
        response = perform_request(value["api_key"], value["request"])
        sys.stdout.buffer.write(response)
        sys.stdout.buffer.flush()
        return 0
    except TransportFailure as error:
        return error.status
    except Exception:
        # Never print remote input, a key, traceback, or an arbitrary exception.
        return EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
