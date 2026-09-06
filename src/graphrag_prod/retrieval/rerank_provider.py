"""Fixed-provider reranking with an isolated, terminable HTTPS request.

This client orders complete, already-authorized Chunk texts. It cannot discover
sources, widen permissions, truncate evidence, retry, or select a fallback.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from ._rerank_transport import (
    ENDPOINT, MODEL, INSTRUCT, MAX_CANDIDATES, MAX_ITEM_UTF8_BYTES,
    MAX_PAIR_UTF8_BYTES, MAX_RESPONSE_BYTES, MAX_WORKER_REQUEST_BYTES, MAX_INSTRUCT_UTF8_BYTES,
    EXIT_NETWORK, EXIT_HTTP, EXIT_AUTH, EXIT_RATE_LIMIT, EXIT_RESPONSE_LIMIT,
    canonical_bytes, parse_json, valid_api_key, validate_request,
)
from .reranking import RerankCandidate, RerankResponse, RerankScore, RerankingError


RERANK_MODEL = MODEL
RERANK_ENDPOINT = ENDPOINT
RERANK_INSTRUCT = INSTRUCT
RERANK_PROFILE = "dashscope-beijing-qwen3-rerank-default-qa:v1"
DEFAULT_PROFILE = "default-qa-v1"
CONTEXTUAL_PROFILE = "contextual-qa-v1"
CONTEXTUAL_INSTRUCT = (
    "Given an industrial equipment question and its user-selected scope, rank source passages by how directly "
    "they provide evidence needed to answer every requested part. Relevant evidence includes exact recorded "
    "facts and conditions, contradictions, missing information, and source applicability. A passage that "
    "disproves a premise or establishes that a requested value is unconfirmed is useful evidence. Use source "
    "titles to resolve which record a passage belongs to. Prefer passages with the requested evidence over "
    "passages that only repeat an equipment identifier or discuss the same broad topic."
)
MAX_DEADLINE_SECONDS = 30.0
_CALL_LOCK = threading.Lock()
_WORKER_MODULE = "graphrag_prod.retrieval._rerank_transport"


class RerankProviderError(RerankingError):
    """A fixed public category without remote text or exception details."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _checksum(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True, slots=True)
class RerankProviderProfile:
    instruct: str | None = None
    include_source_context: bool = False

    def __post_init__(self) -> None:
        if type(self.include_source_context) is not bool:
            raise ValueError("source context flag must be boolean")
        if self.instruct is not None and (
            not isinstance(self.instruct, str) or not self.instruct.strip()
            or len(self.instruct.encode("utf-8")) > MAX_INSTRUCT_UTF8_BYTES
            or any(ord(char) < 32 for char in self.instruct)
        ):
            raise ValueError("explicit rerank instruction exceeds bounds")

    @property
    def rendering_version(self) -> str:
        return "source-title-section:v1" if self.include_source_context else "raw-chunk:v1"

    @property
    def profile_id(self) -> str:
        if self.instruct is None and not self.include_source_context:
            return DEFAULT_PROFILE
        if self.instruct == CONTEXTUAL_INSTRUCT and self.include_source_context:
            return CONTEXTUAL_PROFILE
        settings = {"instruct": self.instruct, "rendering_version": self.rendering_version}
        return "dashscope-beijing-qwen3-rerank:" + _checksum(canonical_bytes(settings))


def get_provider_profile(profile: str | RerankProviderProfile = DEFAULT_PROFILE) -> RerankProviderProfile:
    if isinstance(profile, RerankProviderProfile):
        return profile
    if profile == DEFAULT_PROFILE:
        return RerankProviderProfile()
    if profile == CONTEXTUAL_PROFILE:
        return RerankProviderProfile(CONTEXTUAL_INSTRUCT, True)
    raise RerankProviderError("RERANK_INVALID_PROFILE")


def provider_configuration(profile: str | RerankProviderProfile = DEFAULT_PROFILE) -> dict[str, Any]:
    if isinstance(profile, str) and profile.startswith("listwise-"):
        from .listwise_provider import listwise_configuration
        return listwise_configuration(profile)
    profile = get_provider_profile(profile)
    return {
        "profile": profile.profile_id, "model": MODEL, "model_version_kind": "provider_alias",
        "endpoint": ENDPOINT, "region": "cn-beijing", "instruct": profile.instruct,
        "instruction_mode": "provider_default_qa" if profile.instruct is None else "explicit",
        "rendering_version": profile.rendering_version, "attempts": 1, "concurrency": 1,
        "query_context_version": "user-selected-equipment:v1" if profile.include_source_context else None,
        "deadline_seconds": MAX_DEADLINE_SECONDS, "max_candidates": MAX_CANDIDATES,
        "max_item_utf8_bytes": MAX_ITEM_UTF8_BYTES,
        "max_instruct_utf8_bytes": MAX_INSTRUCT_UTF8_BYTES,
        "max_pair_utf8_bytes": MAX_PAIR_UTF8_BYTES,
        "max_response_bytes": MAX_RESPONSE_BYTES, "fallback": None,
        "input_budget_unit": "UTF8_bytes_not_tokens",
    }


def _render(candidate: RerankCandidate, profile: RerankProviderProfile) -> str:
    if not profile.include_source_context:
        return candidate.text
    title = candidate.source_title or ""
    section = candidate.source_section or ""
    return f"Source document: {title}\nSource section: {section}\nPassage:\n{candidate.text}"


def _request(query_text: str, candidates: tuple[RerankCandidate, ...],
             profile: str | RerankProviderProfile = DEFAULT_PROFILE) -> tuple[dict[str, Any], int]:
    profile = get_provider_profile(profile)
    if (not isinstance(candidates, tuple) or not 1 <= len(candidates) <= MAX_CANDIDATES
            or any(not isinstance(item, RerankCandidate) for item in candidates)):
        raise RerankProviderError("RERANK_INVALID_INPUT")
    identifiers = [item.chunk_id for item in candidates]
    if (len(set(identifiers)) != len(identifiers)
            or any(not isinstance(value, str) or not value.strip() for value in identifiers)):
        raise RerankProviderError("RERANK_INVALID_INPUT")
    try:
        for candidate in candidates:
            if (not isinstance(candidate.text, str)
                    or candidate.checksum != _checksum(candidate.text.encode("utf-8"))):
                raise RerankProviderError("RERANK_SOURCE_CHECKSUM")
        request = {"model": MODEL, "query": query_text,
                   "documents": [_render(item, profile) for item in candidates], "top_n": len(candidates)}
        if profile.instruct is not None:
            request["instruct"] = profile.instruct
        paired = validate_request(request)
    except (TypeError, ValueError, UnicodeError):
        raise RerankProviderError("RERANK_INPUT_LIMIT") from None
    return request, paired


def rerank_cache_identity(query_text: str, candidates: tuple[RerankCandidate, ...], *,
                          profile: str | RerankProviderProfile = DEFAULT_PROFILE) -> dict[str, Any]:
    """Bind cache reuse to full authorized inputs, preserving candidate order."""
    if isinstance(profile, str) and profile.startswith("listwise-"):
        from .listwise_provider import listwise_cache_identity
        return listwise_cache_identity(query_text, candidates, profile=profile)
    profile = get_provider_profile(profile)
    request, paired = _request(query_text, candidates, profile)
    return {
        "profile": profile.profile_id, "model": MODEL, "endpoint": ENDPOINT,
        "instruct": profile.instruct, "rendering_version": profile.rendering_version,
        "instruction_mode": "provider_default_qa" if profile.instruct is None else "explicit",
        "query_checksum": _checksum(query_text.encode("utf-8")),
        "candidates": [{"chunk_id": item.chunk_id, "checksum": item.checksum,
                        "document_id": getattr(item, "document_id", None),
                        "version_id": getattr(item, "version_id", None),
                        "rendered_checksum": _checksum(rendered.encode("utf-8"))}
                       for item, rendered in zip(candidates, request["documents"], strict=True)],
        "request_checksum": _checksum(canonical_bytes(request)),
        "pair_utf8_bytes": paired,
    }


def response_metadata(response: RerankResponse, *,
                      profile: str | RerankProviderProfile = DEFAULT_PROFILE) -> dict[str, Any]:
    """JSON-safe actual usage; bytes and price estimates are separate concepts."""
    if isinstance(profile, str) and profile.startswith("listwise-"):
        from .listwise_provider import listwise_response_metadata
        return listwise_response_metadata(response, profile=profile)
    profile = get_provider_profile(profile)
    if response.instruct != profile.instruct or response.rendering_version != profile.rendering_version:
        raise RerankProviderError("RERANK_PROFILE_MISMATCH")
    return {
        "profile": profile.profile_id, "model": response.model, "model_version_kind": "provider_alias",
        "endpoint": response.endpoint, "instruct": response.instruct,
        "instruction_mode": "provider_default_qa" if response.instruct is None else "explicit",
        "rendering_version": response.rendering_version,
        "rendered_input_checksums": list(response.rendered_input_checksums),
        "source_checksums": list(response.source_checksums), "request_id": response.request_id,
        "usage": {"total_tokens": response.input_tokens}, "duration_ms": response.duration_ms,
        "input_checksum": response.input_checksum, "output_checksum": response.output_checksum,
        "input_bytes": response.input_bytes, "pair_utf8_bytes": response.pair_utf8_bytes,
        "candidate_count": response.candidate_count,
    }


def decode_response(payload: bytes, query_text: str, candidates: tuple[RerankCandidate, ...], *,
                    duration_ms: float, profile: str | RerankProviderProfile = DEFAULT_PROFILE) -> RerankResponse:
    """Validate live or externally cached raw output against exact input order."""
    profile = get_provider_profile(profile)
    request, paired = _request(query_text, candidates, profile)
    if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_RESPONSE_BYTES:
        raise RerankProviderError("RERANK_RESPONSE_LIMIT")
    if (isinstance(duration_ms, bool) or not isinstance(duration_ms, (int, float))
            or not math.isfinite(duration_ms) or duration_ms < 0):
        raise RerankProviderError("RERANK_INVALID_RESPONSE")
    try:
        raw = parse_json(payload)
        if (not isinstance(raw, dict) or raw.get("model") != MODEL
                or raw.get("object") != "list" or "output" in raw or "error" in raw or "code" in raw):
            raise ValueError("unexpected provider response")
        request_id = raw["id"]
        if (not isinstance(request_id, str) or not 1 <= len(request_id) <= 256
                or not request_id.isascii() or any(not (char.isalnum() or char in "._:-") for char in request_id)):
            raise ValueError("invalid request identity")
        usage = raw["usage"]
        if not isinstance(usage, dict) or type(usage.get("total_tokens")) is not int or usage["total_tokens"] < 0:
            raise ValueError("missing actual token usage")
        rows = raw["results"]
        if not isinstance(rows, list) or len(rows) != len(candidates):
            raise ValueError("incomplete result permutation")
        scores, seen = [], set()
        previous = math.inf
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("invalid ranked result")
            index, score = row["index"], row["relevance_score"]
            if (type(index) is not int or not 0 <= index < len(candidates) or index in seen
                    or isinstance(score, bool) or not isinstance(score, (int, float))
                    or not math.isfinite(score) or not 0 <= score <= 1 or score > previous):
                raise ValueError("invalid index or score")
            # If the provider happens to return text, it must not replace the
            # caller's source. This request does not ask for returned documents.
            if "document" in row and row["document"] != {"text": request["documents"][index]}:
                raise ValueError("returned source differs")
            seen.add(index)
            previous = float(score)
            scores.append(RerankScore(chunk_id=candidates[index].chunk_id, index=index, score=float(score)))
        encoded = canonical_bytes(request)
        normalized_output = {"object": "list", "model": MODEL, "id": request_id,
            "results": [{"index": item.index, "relevance_score": item.score} for item in scores],
            "usage": {"total_tokens": usage["total_tokens"]}}
        return RerankResponse(
            scores=tuple(scores), model=MODEL, endpoint=ENDPOINT, instruct=profile.instruct,
            request_id=request_id, input_tokens=usage["total_tokens"], duration_ms=float(duration_ms),
            input_checksum=_checksum(encoded), output_checksum=_checksum(canonical_bytes(normalized_output)),
            input_bytes=len(encoded), pair_utf8_bytes=paired, candidate_count=len(candidates),
            total_tokens=usage["total_tokens"],
            rendering_version=profile.rendering_version,
            rendered_input_checksums=tuple(_checksum(text.encode("utf-8")) for text in request["documents"]),
            source_checksums=tuple(item.checksum for item in candidates),
        )
    except (KeyError, TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        raise RerankProviderError("RERANK_INVALID_RESPONSE") from None


def validate_cached_response(query_text: str, candidates: tuple[RerankCandidate, ...],
                             response: RerankResponse, *,
                             profile: str | RerankProviderProfile = DEFAULT_PROFILE,
                             raw_response_json: str | None = None) -> None:
    """Recompute both input and normalized output identities, not just a cache envelope."""
    if isinstance(profile, str) and profile.startswith("listwise-"):
        from .listwise_provider import validate_listwise_cached_response
        return validate_listwise_cached_response(query_text, candidates, response, profile=profile,
                                                raw_response_json=raw_response_json)
    if not isinstance(response, RerankResponse):
        raise RerankProviderError("RERANK_INVALID_RESPONSE")
    normalized = {"object": "list", "model": response.model, "id": response.request_id,
        "results": [{"index": item.index, "relevance_score": item.score} for item in response.scores],
        "usage": {"total_tokens": response.input_tokens}}
    expected = decode_response(canonical_bytes(normalized), query_text, candidates,
                               duration_ms=response.duration_ms, profile=profile)
    if response != expected:
        raise RerankProviderError("RERANK_CACHE_MISMATCH")


def _worker_environment() -> dict[str, str]:
    # Avoid inheriting provider/database credentials or proxy overrides. The
    # exact source package path also prevents an unrelated installed checkout.
    environment = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL") if key in os.environ}
    environment.update(PYTHONPATH=str(Path(__file__).resolve().parents[2]),
                       PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    return environment


def _subprocess_request(envelope: bytes, timeout_seconds: float, *, worker_module: str = _WORKER_MODULE) -> bytes:
    """The deadline includes stdin writes; timeout kills and reaps the child."""
    started = time.monotonic()
    try:
        with subprocess.Popen(
            [sys.executable, "-m", worker_module], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=_worker_environment(),
        ) as worker:
            try:
                remaining = timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(worker.args, timeout_seconds)
                output, _ = worker.communicate(envelope, timeout=remaining)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.communicate()
                raise RerankProviderError("RERANK_TIMEOUT") from None
            if worker.returncode != 0:
                code = {
                    EXIT_NETWORK: "RERANK_NETWORK", EXIT_HTTP: "RERANK_HTTP",
                    EXIT_AUTH: "RERANK_AUTH", EXIT_RATE_LIMIT: "RERANK_RATE_LIMIT",
                    EXIT_RESPONSE_LIMIT: "RERANK_RESPONSE_LIMIT",
                }.get(worker.returncode, "RERANK_WORKER_FAILED")
                raise RerankProviderError(code)
    except OSError:
        raise RerankProviderError("RERANK_WORKER_FAILED") from None
    if len(output) > MAX_RESPONSE_BYTES:
        raise RerankProviderError("RERANK_RESPONSE_LIMIT")
    return output


class DashScopeReranker:
    """A single fixed model, one attempt, and one in-flight call per process.

    A smaller deadline and injected transport are useful for deterministic
    tests. The production transport is always a killable subprocess.
    """

    def __init__(self, api_key: str, *, deadline_seconds: float = MAX_DEADLINE_SECONDS,
                 transport: Callable[[bytes, float], bytes] | None = None,
                 profile: str | RerankProviderProfile = DEFAULT_PROFILE) -> None:
        if not valid_api_key(api_key):
            raise RerankProviderError("RERANK_INVALID_CREDENTIAL")
        if (isinstance(deadline_seconds, bool) or not isinstance(deadline_seconds, (int, float))
                or not math.isfinite(deadline_seconds) or not 0 < deadline_seconds <= MAX_DEADLINE_SECONDS):
            raise RerankProviderError("RERANK_INVALID_DEADLINE")
        self._api_key = api_key
        self.profile = get_provider_profile(profile)
        self._deadline_seconds = float(deadline_seconds)
        self._transport = transport or _subprocess_request

    def rerank(self, query_text: str, candidates: tuple[RerankCandidate, ...]) -> RerankResponse:
        started = time.monotonic()
        profile = self.profile
        request, _ = _request(query_text, candidates, profile)
        envelope = canonical_bytes({"api_key": self._api_key, "request": request})
        if len(envelope) > MAX_WORKER_REQUEST_BYTES:
            raise RerankProviderError("RERANK_INPUT_LIMIT")
        if not _CALL_LOCK.acquire(blocking=False):
            raise RerankProviderError("RERANK_BUSY")
        try:
            remaining = self._deadline_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise RerankProviderError("RERANK_TIMEOUT")
            try:
                output = self._transport(envelope, remaining)
            except RerankProviderError:
                raise
            except Exception:
                raise RerankProviderError("RERANK_TRANSPORT_FAILED") from None
            elapsed = time.monotonic() - started
            if elapsed > self._deadline_seconds:
                raise RerankProviderError("RERANK_TIMEOUT")
            return decode_response(output, query_text, candidates, duration_ms=elapsed * 1_000, profile=profile)
        finally:
            _CALL_LOCK.release()


def create_reranker(api_key: str, *, profile: str = DEFAULT_PROFILE, **kwargs: Any) -> Any:
    """Choose an explicitly registered experiment; never a runtime fallback."""
    if isinstance(profile, str) and profile.startswith("listwise-"):
        from .listwise_provider import DashScopeListwiseReranker
        return DashScopeListwiseReranker(api_key, profile=profile, **kwargs)
    return DashScopeReranker(api_key, profile=profile, **kwargs)
