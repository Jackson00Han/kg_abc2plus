"""Provider-neutral contracts for bounded, separately scored candidate reranking."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from typing import Protocol


MAX_RERANK_CANDIDATES = 50


class RerankingError(RuntimeError):
    """Sanitized provider failure; retrieval must not silently fall back."""

    code = "RERANK_ERROR"


def _text(value: object, name: str, limit: int = 512) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(c) < 32 for c in value)):
        raise ValueError(f"{name} must be bounded non-empty text")
    return value


def _checksum(value: object) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("reranking checksum must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class RerankCandidate:
    chunk_id: str
    text: str
    checksum: str
    source_title: str | None = None
    source_section: str | None = None
    document_id: str | None = None
    version_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.chunk_id, "chunk_id", 256)
        if not isinstance(self.text, str) or not self.text.strip() or len(self.text) > 30_000:
            raise ValueError("reranking requires intact bounded source text")
        _checksum(self.checksum)
        if hashlib.sha256(self.text.encode("utf-8")).hexdigest() != self.checksum:
            raise ValueError("reranking candidate text differs from its checksum")
        for name in ("source_title", "source_section", "document_id", "version_id"):
            value = getattr(self, name)
            if value is not None:
                _text(value, name, 1_000)


@dataclass(frozen=True, slots=True)
class RerankScore:
    chunk_id: str
    index: int
    score: float | None

    def __post_init__(self) -> None:
        _text(self.chunk_id, "chunk_id", 256)
        if type(self.index) is not int or not 0 <= self.index < MAX_RERANK_CANDIDATES:
            raise ValueError("reranking index is outside the candidate bound")
        if self.score is None:
            return
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)) or not math.isfinite(self.score):
            raise ValueError("reranking scores must be finite numbers")
        object.__setattr__(self, "score", float(self.score))


@dataclass(frozen=True, slots=True)
class RerankResponse:
    scores: tuple[RerankScore, ...]
    model: str
    endpoint: str
    instruct: str | None
    request_id: str
    input_tokens: int
    duration_ms: float
    input_checksum: str
    output_checksum: str
    input_bytes: int
    pair_utf8_bytes: int
    candidate_count: int
    rendering_version: str = "raw-chunk:v1"
    rendered_input_checksums: tuple[str, ...] = ()
    source_checksums: tuple[str, ...] = ()
    output_tokens: int = 0
    normalization_method: str = "none"
    normalization_removed_count: int = 0
    raw_output_checksum: str | None = None
    # Decoded original candidate indices (zero based), before stable deduplication.
    raw_permutation: tuple[int, ...] = ()
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None

    def __post_init__(self) -> None:
        if (not isinstance(self.scores, tuple) or not 1 <= len(self.scores) <= MAX_RERANK_CANDIDATES
                or any(not isinstance(item, RerankScore) for item in self.scores)):
            raise ValueError("reranking requires a bounded immutable score tuple")
        for name in ("model", "endpoint", "request_id"):
            _text(getattr(self, name), name)
        if self.instruct is not None:
            _text(self.instruct, "instruct", 2_000)
        if type(self.candidate_count) is not int or self.candidate_count != len(self.scores):
            raise ValueError("reranking candidate count differs from returned scores")
        if (len({s.chunk_id for s in self.scores}) != self.candidate_count
                or {s.index for s in self.scores} != set(range(self.candidate_count))):
            raise ValueError("reranking scores must cover unique candidate identities and indices")
        for value in (self.input_tokens, self.output_tokens, self.input_bytes, self.pair_utf8_bytes):
            if type(value) is not int or not 0 <= value <= 1_000_000:
                raise ValueError("reranking usage must be bounded non-negative integers")
        for value in (self.prompt_tokens, self.completion_tokens, self.total_tokens):
            if value is not None and (type(value) is not int or not 0 <= value <= 1_000_000):
                raise ValueError("observed reranking tokens must be bounded integers or absent")
        if self.prompt_tokens is not None and self.prompt_tokens != self.input_tokens:
            raise ValueError("input token counter differs from observed prompt tokens")
        if self.completion_tokens is not None and self.completion_tokens != self.output_tokens:
            raise ValueError("output token counter differs from observed completion tokens")
        if (self.prompt_tokens is not None and self.completion_tokens is not None and self.total_tokens is not None
                and self.prompt_tokens + self.completion_tokens != self.total_tokens):
            raise ValueError("observed token breakdown does not sum to the observed total")
        if isinstance(self.duration_ms, bool) or not isinstance(self.duration_ms, (int, float)) or not math.isfinite(self.duration_ms) or not 0 <= self.duration_ms <= 300_000:
            raise ValueError("reranking duration must be bounded and finite")
        for value in (self.input_checksum, self.output_checksum):
            _checksum(value)
        _text(self.rendering_version, "rendering_version")
        for values in (self.rendered_input_checksums, self.source_checksums):
            if not isinstance(values, tuple) or (values and len(values) != self.candidate_count):
                raise ValueError("reranking input digests must preserve candidate order and count")
            for value in values:
                _checksum(value)
        if type(self.normalization_removed_count) is not int or not 0 <= self.normalization_removed_count <= self.candidate_count:
            raise ValueError("reranking normalization count exceeds bounds")
        if self.raw_output_checksum is not None:
            _checksum(self.raw_output_checksum)
        if not isinstance(self.raw_permutation, tuple):
            raise ValueError("reranking raw permutation must be immutable")
        if self.normalization_method == "none":
            if self.raw_permutation or self.normalization_removed_count:
                raise ValueError("unnormalized ranking cannot claim removed entries")
        elif self.normalization_method == "stable-deduplicate-complete-permutation:v1":
            if (not self.candidate_count <= len(self.raw_permutation) <= 2 * self.candidate_count
                    or any(type(i) is not int or not 0 <= i < self.candidate_count for i in self.raw_permutation)
                    or self.raw_output_checksum is None):
                raise ValueError("raw ranking must contain only bounded known candidate indices")
            normalized = tuple(dict.fromkeys(self.raw_permutation))
            if (normalized != tuple(item.index for item in self.scores)
                    or self.normalization_removed_count != len(self.raw_permutation) - len(normalized)):
                raise ValueError("stable ranking normalization cannot add missing or reorder candidates")
        else:
            raise ValueError("unsupported reranking normalization policy")


class CandidateReranker(Protocol):
    def rerank(self, query_text: str, candidates: tuple[RerankCandidate, ...]) -> RerankResponse: ...


@dataclass(frozen=True, slots=True)
class RerankTrace:
    status: str
    candidate_limit: int
    candidate_chunk_ids: tuple[str, ...]
    response: RerankResponse | None
    ranked_chunk_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.candidate_limit) is not int or not 1 <= self.candidate_limit <= MAX_RERANK_CANDIDATES:
            raise ValueError("reranking candidate limit must be between 1 and 50")
        if (not isinstance(self.candidate_chunk_ids, tuple) or len(self.candidate_chunk_ids) > self.candidate_limit
                or len(set(self.candidate_chunk_ids)) != len(self.candidate_chunk_ids)):
            raise ValueError("reranking trace candidate IDs must be unique and bounded")
        for identifier in self.candidate_chunk_ids:
            _text(identifier, "candidate_id", 256)
        if self.status == "SKIPPED_EMPTY":
            if self.candidate_chunk_ids or self.response is not None or self.ranked_chunk_ids:
                raise ValueError("skipped reranking cannot include provider output")
        elif self.status == "RERANKED":
            if not isinstance(self.response, RerankResponse) or len(self.candidate_chunk_ids) != self.response.candidate_count:
                raise ValueError("reranking trace requires a complete provider response")
            if any(self.candidate_chunk_ids[s.index] != s.chunk_id for s in self.response.scores):
                raise ValueError("reranking score index does not identify its original candidate")
            if (not isinstance(self.ranked_chunk_ids, tuple)
                    or len(self.ranked_chunk_ids) != len(self.candidate_chunk_ids)
                    or set(self.ranked_chunk_ids) != set(self.candidate_chunk_ids)):
                raise ValueError("reranking final order must cover the candidate set")
        else:
            raise ValueError("unsupported reranking trace status")
