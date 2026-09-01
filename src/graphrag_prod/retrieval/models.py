"""Immutable contracts for bounded retrieval and exact citations."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import math
from numbers import Real
from typing import Any

from graphrag_prod.domain.access import Principal


def _text(value: str, name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


def _positive(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


@dataclass(frozen=True, slots=True)
class VersionFilter:
    """Optional restrictions applied in every retrieval path.

    Version IDs still have to be active. This filter cannot make a retired
    version visible.
    """

    document_ids: frozenset[str] = field(default_factory=frozenset)
    version_ids: frozenset[str] = field(default_factory=frozenset)
    published_at_or_before: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "document_ids",
            frozenset(_text(value, "document_id") for value in self.document_ids),
        )
        object.__setattr__(
            self,
            "version_ids",
            frozenset(_text(value, "version_id") for value in self.version_ids),
        )
        cutoff = self.published_at_or_before
        if cutoff is not None and (cutoff.tzinfo is None or cutoff.utcoffset() is None):
            raise ValueError("published_at_or_before must be timezone-aware")


@dataclass(frozen=True, slots=True)
class RetrievalLimits:
    """All query, candidate, expansion, and context bounds in one contract."""

    top_k: int = 5
    vector_recall_k: int = 20
    bm25_recall_k: int = 20
    bm25_scan_k: int = 100
    seed_k: int = 5
    graph_entities_per_seed: int = 20
    graph_edges_per_seed: int = 100
    graph_candidates_per_seed: int = 20
    candidate_limit: int = 100
    anchor_k: int = 3
    adjacent_window: int = 1
    max_context_chars: int = 12_000
    rrf_rank_constant: int = 60
    minimum_vector_score: float = 0.0
    minimum_bm25_score: float = 0.0
    minimum_rrf_channels: int = 1
    deduplicate_content: bool = True

    def __post_init__(self) -> None:
        for name in (
            "top_k",
            "vector_recall_k",
            "bm25_recall_k",
            "bm25_scan_k",
            "seed_k",
            "graph_entities_per_seed",
            "graph_edges_per_seed",
            "graph_candidates_per_seed",
            "candidate_limit",
            "anchor_k",
            "max_context_chars",
            "rrf_rank_constant",
            "minimum_rrf_channels",
        ):
            _positive(getattr(self, name), name)
        if (
            isinstance(self.adjacent_window, bool)
            or not isinstance(self.adjacent_window, int)
            or self.adjacent_window < 0
        ):
            raise ValueError("adjacent_window must be a non-negative integer")
        if self.bm25_scan_k < self.bm25_recall_k:
            raise ValueError("bm25_scan_k must cover bm25_recall_k")
        if self.seed_k > self.candidate_limit:
            raise ValueError("seed_k must not exceed candidate_limit")
        if self.anchor_k > self.top_k:
            raise ValueError("anchor_k must not exceed top_k")
        if self.minimum_rrf_channels > 2:
            raise ValueError("minimum_rrf_channels cannot exceed two")
        vector_floor = _finite(self.minimum_vector_score, "minimum_vector_score")
        object.__setattr__(self, "minimum_vector_score", vector_floor)
        if not 0.0 <= vector_floor <= 1.0:
            raise ValueError("minimum_vector_score must be between zero and one")
        bm25_floor = _finite(self.minimum_bm25_score, "minimum_bm25_score")
        object.__setattr__(self, "minimum_bm25_score", bm25_floor)
        if bm25_floor < 0.0:
            raise ValueError("minimum_bm25_score must not be negative")
        if not isinstance(self.deduplicate_content, bool):
            raise ValueError("deduplicate_content must be boolean")


@dataclass(frozen=True, slots=True)
class RetrievalRequest:
    query_text: str
    query_vector: tuple[float, ...]
    principal: Principal
    query_embedding_space_id: str
    limits: RetrievalLimits = field(default_factory=RetrievalLimits)
    version_filter: VersionFilter = field(default_factory=VersionFilter)

    def __post_init__(self) -> None:
        object.__setattr__(self, "query_text", _text(self.query_text, "query_text"))
        object.__setattr__(
            self,
            "query_embedding_space_id",
            _text(self.query_embedding_space_id, "query_embedding_space_id"),
        )
        vector = tuple(_finite(value, "query_vector value") for value in self.query_vector)
        if not vector or not any(value != 0.0 for value in vector):
            raise ValueError("query_vector must be non-zero")
        object.__setattr__(self, "query_vector", vector)


@dataclass(frozen=True, slots=True)
class Citation:
    chunk_id: str
    chunk_checksum: str
    document_id: str
    canonical_uri: str
    source_name: str
    version_id: str
    version_checksum: str
    version_number: int
    ordinal: int
    char_start: int
    char_end: int
    page_number: int | None
    section: str | None


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    text: str
    citation: Citation
    role: str
    score: float | None
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TraceHit:
    chunk_id: str
    rank: int
    score: float | None
    ranks: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class TraceDecision:
    chunk_id: str
    decision: str
    reason: str


@dataclass(frozen=True, slots=True)
class RetrievalTrace:
    trace_id: str
    method: str
    tenant_id: str
    corpus_revision: int
    embedding_generation_id: str
    embedding_space_id: str
    vector_recall: tuple[TraceHit, ...]
    bm25_recall: tuple[TraceHit, ...]
    seed_ranking: tuple[TraceHit, ...]
    graph_expansion: tuple[TraceHit, ...]
    candidate_vector_ranking: tuple[TraceHit, ...]
    final_ranking: tuple[TraceHit, ...]
    decisions: tuple[TraceDecision, ...]
    selected_chunk_ids: tuple[str, ...]
    context_chars: int
    limits: RetrievalLimits
    version_filter: VersionFilter

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["version_filter"] = {
            "document_ids": sorted(self.version_filter.document_ids),
            "version_ids": sorted(self.version_filter.version_ids),
            "published_at_or_before": (
                None
                if self.version_filter.published_at_or_before is None
                else self.version_filter.published_at_or_before.isoformat()
            ),
        }
        return payload


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    chunks: tuple[RetrievedChunk, ...]
    trace: RetrievalTrace

    @property
    def citations(self) -> tuple[Citation, ...]:
        return tuple(chunk.citation for chunk in self.chunks)
