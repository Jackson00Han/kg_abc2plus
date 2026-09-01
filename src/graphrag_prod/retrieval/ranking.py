"""Established ranking and deterministic context-selection primitives."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import math
from typing import Hashable, Mapping, Sequence


def reciprocal_rank_fusion(
    rankings: Mapping[str, Sequence[str]],
    *,
    rank_constant: int = 60,
) -> tuple[tuple[str, ...], dict[str, float], dict[str, dict[str, int]]]:
    """Fuse rankings with the original RRF sum of ``1 / (k + rank)``."""
    if isinstance(rank_constant, bool) or rank_constant <= 0:
        raise ValueError("rank_constant must be positive")
    scores: defaultdict[str, float] = defaultdict(float)
    positions: defaultdict[str, dict[str, int]] = defaultdict(dict)
    for channel, ranked_ids in rankings.items():
        if not channel.strip():
            raise ValueError("ranking channel must not be empty")
        seen: set[str] = set()
        for rank, chunk_id in enumerate(ranked_ids, start=1):
            if not chunk_id.strip():
                raise ValueError("ranking chunk_id must not be empty")
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            scores[chunk_id] += 1.0 / (rank_constant + rank)
            positions[chunk_id][channel] = rank
    ordered = tuple(sorted(scores, key=lambda item: (-scores[item], item)))
    return ordered, dict(scores), {key: dict(value) for key, value in positions.items()}


def resource_allocation_score(entity_degrees: Sequence[int]) -> float:
    """Return the standard Resource Allocation score for shared neighbors."""
    if any(
        isinstance(degree, bool) or not isinstance(degree, int) or degree <= 0
        for degree in entity_degrees
    ):
        raise ValueError("entity degrees must be positive integers")
    return math.fsum(1.0 / degree for degree in entity_degrees)


def stable_deduplicate(
    ordered_ids: Sequence[str],
    content_keys: Mapping[str, Hashable],
    *,
    deduplicate_content: bool = True,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Keep first rank occurrence and optionally the first exact content key."""
    kept: list[str] = []
    removed: list[str] = []
    seen_ids: set[str] = set()
    seen_content: set[Hashable] = set()
    for chunk_id in ordered_ids:
        if chunk_id in seen_ids:
            removed.append(chunk_id)
            continue
        seen_ids.add(chunk_id)
        key = content_keys.get(chunk_id)
        if deduplicate_content and key is not None and key in seen_content:
            removed.append(chunk_id)
            continue
        kept.append(chunk_id)
        if key is not None:
            seen_content.add(key)
    return tuple(kept), tuple(removed)


@dataclass(frozen=True, slots=True)
class ContextSelection:
    chunk_ids: tuple[str, ...]
    roles: tuple[tuple[str, str], ...]
    skipped: tuple[tuple[str, str], ...]
    total_chars: int


def select_context(
    *,
    ranked_ids: Sequence[str],
    anchor_ids: Sequence[str],
    adjacent_ids: Sequence[str],
    char_lengths: Mapping[str, int],
    max_chunks: int,
    max_chars: int,
) -> ContextSelection:
    """Select whole chunks in anchor, adjacency, then rank-fill order."""
    if isinstance(max_chunks, bool) or max_chunks <= 0:
        raise ValueError("max_chunks must be positive")
    if isinstance(max_chars, bool) or max_chars <= 0:
        raise ValueError("max_chars must be positive")
    roles: dict[str, str] = {}
    skipped: list[tuple[str, str]] = []
    selected: list[str] = []
    considered: set[str] = set()
    total_chars = 0

    def consider(chunk_id: str, role: str) -> None:
        nonlocal total_chars
        if chunk_id in considered:
            return
        considered.add(chunk_id)
        if len(selected) >= max_chunks:
            skipped.append((chunk_id, "chunk_limit"))
            return
        length = char_lengths.get(chunk_id)
        if length is None or length <= 0:
            skipped.append((chunk_id, "missing_or_empty"))
            return
        if total_chars + length > max_chars:
            skipped.append((chunk_id, "character_budget"))
            return
        selected.append(chunk_id)
        roles[chunk_id] = role
        total_chars += length

    for chunk_id in anchor_ids:
        consider(chunk_id, "anchor")
    for chunk_id in adjacent_ids:
        consider(chunk_id, "adjacent")
    for chunk_id in ranked_ids:
        consider(chunk_id, "ranked")
    return ContextSelection(
        chunk_ids=tuple(selected),
        roles=tuple((chunk_id, roles[chunk_id]) for chunk_id in selected),
        skipped=tuple(skipped),
        total_chars=total_chars,
    )
