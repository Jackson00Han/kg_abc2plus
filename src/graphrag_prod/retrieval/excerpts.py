"""Exact, bounded source windows; never a new Chunk or a relevance score."""

from __future__ import annotations

import hashlib
import re
from typing import Any


EXCERPT_POLICY = "exact-source-window:v1"
MAX_EXCERPT_CHARS = 6_000
_QUERY_TERMS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*|[^\W\d_]+", re.UNICODE)
_DIGEST = re.compile(r"[0-9a-f]{64}")


def source_window(text: str, query: str, max_chars: int) -> tuple[int, int]:
    """Return one contiguous window near the first literal query occurrence.

    Query order is only a deterministic locator inside an already ranked
    source. It never alters candidate scores/ranks. No normalization changes
    the original Unicode offsets; no match returns an explicitly partial head.
    """
    if type(max_chars) is not int or max_chars <= 0 or not isinstance(text, str) or not text:
        raise ValueError("source window requires nonempty text and a positive character budget")
    if len(text) <= max_chars:
        return 0, len(text)
    terms = [query.strip(), *[match.group() for match in _QUERY_TERMS.finditer(query)][:64]]
    anchor = 0
    for term in dict.fromkeys(terms):
        if not 2 <= len(term) <= max_chars:
            continue
        pattern = re.escape(term)
        if term.isascii():
            pattern = r"(?<![A-Za-z0-9_])" + pattern + r"(?![A-Za-z0-9_])"
        hit = re.search(pattern, text, re.IGNORECASE)
        if hit is not None:
            anchor = max(0, hit.start() - (max_chars - len(hit.group())) // 2)
            break
    start = min(anchor, len(text) - max_chars)
    return start, start + max_chars


def validate_excerpt_metadata(citation: Any) -> None:
    """Keep full-source and excerpt digests/ranges unambiguously separate."""
    values = (citation.source_char_start, citation.source_char_end, citation.excerpt_checksum)
    if type(citation.is_excerpt) is not bool:
        raise ValueError("excerpt flag must be a boolean")
    if not citation.is_excerpt:
        if any(value is not None for value in values):
            raise ValueError("full Chunk citations cannot contain excerpt metadata")
        return
    start, end, checksum = values
    if (type(start) is not int or type(end) is not int
            or not 0 <= start <= citation.char_start < citation.char_end <= end
            or (start, end) == (citation.char_start, citation.char_end)
            or not isinstance(checksum, str) or not _DIGEST.fullmatch(checksum)):
        raise ValueError("excerpt citation must identify an exact proper source subrange")


def context_record(record: dict[str, Any], query: str, max_chars: int) -> dict[str, Any]:
    """Project a verified authorized record; preserve its immutable checksum."""
    text = record.get("text")
    start, end = record.get("char_start"), record.get("char_end")
    if (not isinstance(text, str) or not text
            or type(start) is not int or type(end) is not int or start < 0
            or end - start != len(text)
            or hashlib.sha256(text.encode("utf-8")).hexdigest() != record.get("chunk_checksum")):
        raise ValueError("source Chunk text differs from its immutable range or checksum")
    if len(text) <= max_chars:
        return record
    left, right = source_window(text, query, min(MAX_EXCERPT_CHARS, max_chars))
    excerpt = text[left:right]
    return {**record, "text": excerpt, "char_start": start + left, "char_end": start + right,
            "is_excerpt": True, "source_char_start": start, "source_char_end": end,
            "excerpt_checksum": hashlib.sha256(excerpt.encode("utf-8")).hexdigest()}


def restore_excerpt(record: dict[str, Any], citation: Any) -> dict[str, Any]:
    """Revalidate the complete source before reproducing a captured excerpt."""
    validate_excerpt_metadata(citation)
    context_record(record, "", len(record["text"]))  # Full-source integrity check.
    if not citation.is_excerpt:
        return record
    if (record["char_start"], record["char_end"], record["chunk_checksum"]) != (
            citation.source_char_start, citation.source_char_end, citation.chunk_checksum):
        raise ValueError("excerpt original source identity changed")
    left = citation.char_start - record["char_start"]
    right = citation.char_end - record["char_start"]
    text = record["text"][left:right]
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != citation.excerpt_checksum:
        raise ValueError("excerpt does not reproduce its exact source text")
    return {**record, "text": text, "char_start": citation.char_start, "char_end": citation.char_end,
            "is_excerpt": True, "source_char_start": citation.source_char_start,
            "source_char_end": citation.source_char_end, "excerpt_checksum": citation.excerpt_checksum}


def rerank_record(record: dict[str, Any], query: str) -> dict[str, Any]:
    """Keep provider evidence inside its byte limit; preserve full-source identity.

    Reserve the remaining 1,200 bytes of the existing 3,600-byte provider item
    cap for source metadata and the explicit excerpt marker. The provider still
    validates the fully rendered request and fails closed on oversized metadata.
    """
    text = record["text"]
    context_record(record, query, len(text))
    if len(text.encode("utf-8")) <= 2_400:
        return record
    low, high, width = 1, min(len(text) - 1, 2_400), 1
    while low <= high:
        middle = (low + high) // 2
        left, right = source_window(text, query, middle)
        if len(text[left:right].encode("utf-8")) <= 2_400:
            width, low = middle, middle + 1
        else:
            high = middle - 1
    return context_record(record, query, width)
