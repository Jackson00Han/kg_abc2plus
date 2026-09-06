#!/usr/bin/env python3
"""Current-engine rerank capture with explicit live permission and exact caching.

The worker receives only public request fields and vectors. It never imports
industrial gold, source annotations or a scope resolver.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import stat
import sys
import tempfile
import time


MAX_CACHE_BYTES = 1024 * 1024


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate cache JSON field")
        result[key] = value
    return result


def _read_cache(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("rerank cache requires a regular file")
        data = stream.read(MAX_CACHE_BYTES + 1)
    if len(data) > MAX_CACHE_BYTES:
        raise ValueError("rerank cache exceeds byte bound")
    return json.loads(data, object_pairs_hook=_unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite cache value")))


def _write_cache(path, payload):
    encoded = _canonical(payload).encode("utf-8")
    if len(encoded) > MAX_CACHE_BYTES:
        raise ValueError("rerank cache exceeds byte bound")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".rerank-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if _read_cache(path) != payload:
                raise ValueError("concurrent immutable rerank cache differs") from None
    finally:
        Path(temporary).unlink(missing_ok=True)


def decode_cached_response(payload):
    from graphrag_prod.retrieval.reranking import RerankResponse, RerankScore
    fields = dict(payload)
    fields["scores"] = tuple(RerankScore(**row) for row in fields["scores"])
    for name, value in tuple(fields.items()):
        if name != "scores" and isinstance(value, list):
            fields[name] = tuple(value)
    return RerankResponse(**fields)


class CachedReranker:
    """Cache is consulted only when the engine supplies authorized candidates."""

    def __init__(self, cache, provider, *, profile, identity_builder, response_validator):
        self.cache, self.provider, self.profile = Path(cache), provider, profile
        self.identity_builder, self.response_validator = identity_builder, response_validator
        self.reset_usage()

    def reset_usage(self):
        self.usage = {"provider_calls": 0, "cache_hits": 0, "provider_reported_total_tokens": 0,
                      "provider_reported_prompt_tokens": 0, "provider_reported_completion_tokens": 0,
                      "calls_without_reported_tokens": 0, "calls_without_separate_token_usage": 0}
        self.events = []
        self.raw_response_json = None

    def rerank(self, query_text, candidates):
        if self.events or self.usage["provider_calls"]:
            raise ValueError("one retrieval request permits one rerank call")
        identity = self.identity_builder(query_text, candidates, profile=self.profile)
        key = _digest(identity)
        path = self.cache / (key + ".json")
        try:
            payload = _read_cache(path)
        except FileNotFoundError:
            payload = None
        if payload is not None:
            if (set(payload) not in ({"identity", "response", "cache_checksum"},
                                    {"identity", "response", "raw_response_json", "cache_checksum"}) or payload["identity"] != identity
                    or payload["cache_checksum"] != _digest({key: value for key, value in payload.items() if key != "cache_checksum"})):
                raise ValueError("rerank cache identity or checksum differs")
            response = decode_cached_response(payload["response"])
            self.raw_response_json = payload.get("raw_response_json")
            self.response_validator(query_text, candidates, response, profile=self.profile, raw_response_json=self.raw_response_json)
            self.usage["cache_hits"] += 1
        else:
            if self.provider is None:
                raise ValueError("missing rerank cache requires explicit live provider")
            self.usage["provider_calls"] += 1
            self.usage["calls_without_reported_tokens"] += 1
            if hasattr(self.provider, "rerank_with_raw"):
                response, raw_response = self.provider.rerank_with_raw(query_text, candidates)
                if not isinstance(raw_response, bytes) or len(raw_response) > MAX_CACHE_BYTES:
                    raise ValueError("private rerank provider response exceeds bounds")
                self.raw_response_json = raw_response.decode("utf-8")
            else:
                response = self.provider.rerank(query_text, candidates)
            self.response_validator(query_text, candidates, response, profile=self.profile, raw_response_json=self.raw_response_json)
            if type(getattr(response, "total_tokens", None)) is int:
                self.usage["provider_reported_total_tokens"] += response.total_tokens
                self.usage["calls_without_reported_tokens"] -= 1
            if type(getattr(response, "prompt_tokens", None)) is int and type(getattr(response, "completion_tokens", None)) is int:
                self.usage["provider_reported_prompt_tokens"] += response.prompt_tokens
                self.usage["provider_reported_completion_tokens"] += response.completion_tokens
            else:
                self.usage["calls_without_separate_token_usage"] += 1
            payload = {"identity": identity, "response": asdict(response)}
            if self.raw_response_json is not None:
                payload["raw_response_json"] = self.raw_response_json
            payload = json.loads(_canonical(payload))
            payload["cache_checksum"] = _digest(payload)
            _write_cache(path, payload)
        self.events.append({"identity": identity, "cache_key": key,
                            "cache_hit": self.usage["cache_hits"] == 1, "cache_checksum": payload["cache_checksum"]})
        return response


def _plain(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_plain(item) for item in value)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--check-import", action="store_true")
    parser.add_argument("--live-rerank", action="store_true")
    parser.add_argument("--rerank-cache", type=Path)
    parser.add_argument("--rerank-profile")
    args = parser.parse_args()
    source = (args.source_root.resolve() / "src").resolve()
    sys.path.insert(0, str(source))
    import neo4j
    from graphrag_prod.domain.access import Principal
    from graphrag_prod.retrieval.engine import Neo4jRetrievalEngine
    from graphrag_prod.retrieval.models import RetrievalLimits, RetrievalRequest, VersionFilter
    from graphrag_prod.retrieval.rerank_provider import (
        DEFAULT_PROFILE, create_reranker, provider_configuration, rerank_cache_identity, validate_cached_response,
    )
    profile = args.rerank_profile or DEFAULT_PROFILE
    configuration = provider_configuration(profile=profile)
    for name, module in tuple(sys.modules.items()):
        if name == "graphrag_prod" or name.startswith("graphrag_prod."):
            file = getattr(module, "__file__", None)
            if file is not None and not Path(file).resolve().is_relative_to(source):
                raise RuntimeError("worker imported a different graphrag checkout")
    if any(name.startswith("graphrag_prod.industrial") for name in sys.modules):
        raise RuntimeError("rerank worker must not import industrial gold or resolver")
    engine_checksum = hashlib.sha256((source / "graphrag_prod/retrieval/engine.py").read_bytes()).hexdigest()
    if args.check_import:
        print(_canonical({"source_root": str(source), "implementation_checksum": engine_checksum,
                          "industrial_modules_imported": False, "reranker": configuration}), flush=True)
        return
    if not args.live_rerank or args.rerank_cache is None or args.rerank_cache.resolve().is_relative_to(args.source_root.resolve()):
        raise RuntimeError("current capture requires explicit live reranking and an external cache")
    provider = create_reranker(api_key=os.environ["OPENAI_API_KEY"], profile=profile)
    cached = CachedReranker(args.rerank_cache, provider, profile=profile,
                            identity_builder=rerank_cache_identity, response_validator=validate_cached_response)
    logging.getLogger("neo4j").setLevel(logging.CRITICAL)
    with neo4j.GraphDatabase.driver(os.environ["PLAYGROUND_NEO4J_URI"],
            auth=(os.environ["PLAYGROUND_NEO4J_USER"], os.environ["PLAYGROUND_NEO4J_PASSWORD"]),
            max_connection_pool_size=2, connection_acquisition_timeout=10.0) as driver:
        engine = Neo4jRetrievalEngine(driver, os.getenv("PLAYGROUND_NEO4J_DATABASE", "neo4j"),
                                      transaction_timeout_seconds=30.0, reranker=cached)
        for _ in range(128):
            line = sys.stdin.readline(1024 * 1024 + 1)
            if not line:
                return
            if len(line.encode("utf-8")) > 1024 * 1024 or not line.endswith("\n"):
                raise RuntimeError("worker request exceeds input bound")
            raw = json.loads(line)
            if raw["variant"] != "scoped_reranked":
                raise RuntimeError("current rerank worker received another variant")
            started = time.monotonic()
            cached.reset_usage()
            output = {"case_id": raw["case_id"], "variant": raw["variant"],
                "query_checksum": raw["query_checksum"], "query_vector_checksum": raw["query_vector_checksum"],
                "implementation_checksum": engine_checksum, "chunks": [], "trace": None, "error": None,
                "reranker": configuration, "rerank_context": raw.get("rerank_context")}
            try:
                filters = dict(raw["version_filter"])
                for key in ("document_ids", "version_ids"):
                    filters[key] = frozenset(filters.get(key, ()))
                if filters.get("published_at_or_before"):
                    filters["published_at_or_before"] = datetime.fromisoformat(filters["published_at_or_before"])
                request = RetrievalRequest(query_text=raw["query"], query_vector=tuple(raw["vector"]),
                    principal=Principal(raw["principal"]["principal_id"], raw["principal"]["tenant_id"], frozenset(raw["principal"]["groups"])),
                    query_embedding_space_id=raw["embedding_space_id"], limits=RetrievalLimits(**raw["limits"]), version_filter=VersionFilter(**filters),
                    rerank_context=raw.get("rerank_context"))
                result = engine.retrieve(request)
                output["chunks"] = [_plain(asdict(chunk)) for chunk in result.chunks]
                output["trace"] = result.trace.as_dict()
            except Exception as error:
                output["error"] = type(error).__name__
            output["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
            output["rerank_usage"] = cached.usage
            output["rerank_cache_events"] = cached.events
            output["rerank_provider_raw"] = cached.raw_response_json
            print(json.dumps(output, ensure_ascii=False, separators=(",", ":"), allow_nan=False), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"worker_error": type(error).__name__}), flush=True)
        raise SystemExit(2) from None
