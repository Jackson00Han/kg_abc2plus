"""Bounded, path-free parsing and deterministic source chunking.

The parser accepts source bytes rather than filesystem paths.  Format support is
provided by an explicit MIME registry so richer PDF/DOCX parsers can be added
without weakening the default allowlist or letting a filename select a parser.
"""

from __future__ import annotations

import csv
import io
import json
import math
import unicodedata
from dataclasses import dataclass
from typing import Any, Protocol

from graphrag_prod.domain.ids import content_checksum
from graphrag_prod.ingestion.pipeline import ChunkSeed


DEFAULT_MAX_SOURCE_BYTES = 5 * 1024 * 1024
_BUILTIN_MIME_TYPES = frozenset(
    {
        "application/json",
        "text/csv",
        "text/markdown",
        "text/plain",
    }
)
_ALLOWED_TEXT_CONTROLS = frozenset({"\n", "\t"})


class DocumentParseError(ValueError):
    """A source failed the bounded parser contract."""


@dataclass(frozen=True, slots=True)
class ParserLimits:
    """Resource limits applied before and after a format parser runs."""

    max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES
    max_normalized_chars: int = DEFAULT_MAX_SOURCE_BYTES
    max_json_depth: int = 64
    max_json_nodes: int = 100_000
    max_csv_rows: int = 100_000
    max_csv_cells: int = 1_000_000
    max_csv_field_chars: int = 100_000

    def __post_init__(self) -> None:
        for name in (
            "max_source_bytes",
            "max_normalized_chars",
            "max_json_depth",
            "max_json_nodes",
            "max_csv_rows",
            "max_csv_cells",
            "max_csv_field_chars",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class ChunkingConfig:
    """Deterministic, non-overlapping character-boundary splitter settings."""

    max_chars: int = 1_200
    minimum_boundary_ratio: float = 0.6
    version: str = "bounded-boundary:v1"

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_chars, bool)
            or not isinstance(self.max_chars, int)
            or self.max_chars <= 0
        ):
            raise ValueError("max_chars must be a positive integer")
        if not isinstance(self.minimum_boundary_ratio, (int, float)) or isinstance(
            self.minimum_boundary_ratio, bool
        ):
            raise ValueError("minimum_boundary_ratio must be numeric")
        if not 0.0 <= float(self.minimum_boundary_ratio) <= 1.0:
            raise ValueError("minimum_boundary_ratio must be between zero and one")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("chunker version must not be empty")

    @property
    def signature(self) -> str:
        ratio = format(float(self.minimum_boundary_ratio), ".6g")
        return f"{self.version}:max={self.max_chars}:min-ratio={ratio}"


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """Normalized source plus an exact, gapless ChunkSeed projection."""

    mime_type: str
    normalized_text: str
    original_checksum: str
    normalized_checksum: str
    splitter_signature: str
    chunks: tuple[ChunkSeed, ...]

    def __post_init__(self) -> None:
        if not self.chunks:
            raise ValueError("parsed document requires at least one chunk")
        if tuple(seed.ordinal for seed in self.chunks) != tuple(
            range(len(self.chunks))
        ):
            raise ValueError("parsed document chunk ordinals must be contiguous")
        if self.chunks[0].char_start != 0:
            raise ValueError("parsed document chunks must begin at character zero")
        for left, right in zip(self.chunks, self.chunks[1:], strict=False):
            if left.char_end != right.char_start:
                raise ValueError("parsed document chunks must be gapless")
        if self.chunks[-1].char_end != len(self.normalized_text):
            raise ValueError("parsed document chunks must cover the complete source")
        if "".join(seed.text for seed in self.chunks) != self.normalized_text:
            raise ValueError("parsed document chunks must reproduce normalized_text")
        if content_checksum(self.normalized_text) != self.normalized_checksum:
            raise ValueError("normalized checksum does not match normalized_text")


class DocumentParserPlugin(Protocol):
    """A parser selected solely through an explicitly registered MIME type."""

    @property
    def mime_types(self) -> frozenset[str]: ...

    def parse(self, payload: bytes) -> str: ...


def _decode_utf8(payload: bytes) -> str:
    try:
        # utf-8-sig accepts ordinary UTF-8 and removes a leading UTF-8 BOM.
        return payload.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise DocumentParseError("source must be valid UTF-8") from exc


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize(
        "NFC",
        value.replace("\r\n", "\n").replace("\r", "\n"),
    )
    if not normalized or not normalized.strip():
        raise DocumentParseError("source text must not be empty or whitespace-only")
    for character in normalized:
        if unicodedata.category(character) == "Cc" and character not in _ALLOWED_TEXT_CONTROLS:
            raise DocumentParseError("source contains a disallowed control character")
    return normalized


@dataclass(frozen=True, slots=True)
class Utf8TextParser:
    """Strict UTF-8 parser for plain text and Markdown."""

    mime_types: frozenset[str] = frozenset({"text/plain", "text/markdown"})

    def parse(self, payload: bytes) -> str:
        return _decode_utf8(payload)


@dataclass(frozen=True, slots=True)
class JsonDocumentParser:
    """UTF-8 JSON syntax validator with duplicate-key and shape bounds."""

    limits: ParserLimits
    mime_types: frozenset[str] = frozenset({"application/json"})

    def parse(self, payload: bytes) -> str:
        text = _decode_utf8(payload)

        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise DocumentParseError(f"JSON contains duplicate key {key!r}")
                result[key] = value
            return result

        def reject_constant(value: str) -> None:
            raise DocumentParseError(f"JSON constant {value!r} is not permitted")

        try:
            value = json.loads(
                text,
                object_pairs_hook=unique_object,
                parse_constant=reject_constant,
            )
        except DocumentParseError:
            raise
        except (json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise DocumentParseError("source is not valid bounded JSON") from exc

        nodes = 0
        stack: list[tuple[Any, int]] = [(value, 1)]
        while stack:
            item, depth = stack.pop()
            nodes += 1
            if nodes > self.limits.max_json_nodes:
                raise DocumentParseError("JSON exceeds the configured node limit")
            if depth > self.limits.max_json_depth:
                raise DocumentParseError("JSON exceeds the configured nesting limit")
            if isinstance(item, dict):
                stack.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                stack.extend((child, depth + 1) for child in item)
        return text


@dataclass(frozen=True, slots=True)
class CsvDocumentParser:
    """UTF-8 CSV syntax validator with row, cell, and field bounds."""

    limits: ParserLimits
    mime_types: frozenset[str] = frozenset({"text/csv"})

    def parse(self, payload: bytes) -> str:
        text = _decode_utf8(payload)
        rows = 0
        cells = 0
        try:
            reader = csv.reader(io.StringIO(text, newline=""), strict=True)
            for row in reader:
                rows += 1
                cells += len(row)
                if rows > self.limits.max_csv_rows:
                    raise DocumentParseError("CSV exceeds the configured row limit")
                if cells > self.limits.max_csv_cells:
                    raise DocumentParseError("CSV exceeds the configured cell limit")
                if any(
                    len(field) > self.limits.max_csv_field_chars for field in row
                ):
                    raise DocumentParseError("CSV exceeds the configured field limit")
        except csv.Error as exc:
            raise DocumentParseError("source is not valid CSV") from exc
        return text


def _canonical_mime_type(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DocumentParseError("mime_type must not be empty")
    components = [component.strip() for component in value.split(";")]
    base = components[0].lower()
    for parameter in components[1:]:
        if not parameter:
            continue
        name, separator, parameter_value = parameter.partition("=")
        if (
            separator != "="
            or name.strip().lower() != "charset"
            or parameter_value.strip().strip('"').lower() not in {"utf-8", "utf8"}
        ):
            raise DocumentParseError("only an explicit UTF-8 charset is supported")
    return base


class BoundedDocumentParser:
    """Parse bounded bytes using an explicit MIME plugin registry."""

    def __init__(
        self,
        *,
        limits: ParserLimits | None = None,
        chunking: ChunkingConfig | None = None,
        plugins: tuple[DocumentParserPlugin, ...] | None = None,
    ) -> None:
        self.limits = limits or ParserLimits()
        self.chunking = chunking or ChunkingConfig()
        selected_plugins = plugins or (
            Utf8TextParser(),
            JsonDocumentParser(self.limits),
            CsvDocumentParser(self.limits),
        )
        registry: dict[str, DocumentParserPlugin] = {}
        for plugin in selected_plugins:
            if not plugin.mime_types:
                raise ValueError("parser plugins must declare at least one MIME type")
            for declared in plugin.mime_types:
                mime_type = _canonical_mime_type(declared)
                if ";" in declared or mime_type != declared:
                    raise ValueError("parser plugin MIME types must be canonical")
                if mime_type in registry:
                    raise ValueError(f"duplicate parser for MIME type {mime_type}")
                registry[mime_type] = plugin
        self._registry = registry

    @property
    def allowed_mime_types(self) -> frozenset[str]:
        return frozenset(self._registry)

    def parse(self, payload: bytes, *, mime_type: str) -> ParsedDocument:
        if not isinstance(payload, bytes):
            raise TypeError("payload must be bytes")
        if not payload:
            raise DocumentParseError("source payload must not be empty")
        if len(payload) > self.limits.max_source_bytes:
            raise DocumentParseError("source exceeds the configured byte limit")
        canonical_mime = _canonical_mime_type(mime_type)
        plugin = self._registry.get(canonical_mime)
        if plugin is None:
            raise DocumentParseError(f"unsupported MIME type: {canonical_mime}")

        parsed = plugin.parse(payload)
        if not isinstance(parsed, str):
            raise TypeError("parser plugins must return text")
        normalized = _normalize_text(parsed)
        if len(normalized) > self.limits.max_normalized_chars:
            raise DocumentParseError("normalized source exceeds the character limit")
        chunks = split_gapless(normalized, config=self.chunking)
        return ParsedDocument(
            mime_type=canonical_mime,
            normalized_text=normalized,
            original_checksum=content_checksum(payload),
            normalized_checksum=content_checksum(normalized),
            splitter_signature=self.chunking.signature,
            chunks=chunks,
        )


def split_gapless(
    normalized_text: str,
    *,
    config: ChunkingConfig | None = None,
) -> tuple[ChunkSeed, ...]:
    """Split exact text without overlap, omissions, or delimiter rewriting."""

    selected = config or ChunkingConfig()
    if not isinstance(normalized_text, str):
        raise TypeError("normalized_text must be a string")
    if not normalized_text:
        raise ValueError("normalized_text must not be empty")

    boundaries = ("\n\n", "\n", ". ", "! ", "? ", "; ", ", ", " ")
    seeds: list[ChunkSeed] = []
    start = 0
    while start < len(normalized_text):
        hard_end = min(start + selected.max_chars, len(normalized_text))
        end = hard_end
        if hard_end < len(normalized_text):
            minimum = start + math.ceil(
                selected.max_chars * float(selected.minimum_boundary_ratio)
            )
            for delimiter in boundaries:
                position = normalized_text.rfind(delimiter, minimum, hard_end)
                if position >= minimum:
                    end = position + len(delimiter)
                    break
        if end <= start:
            end = hard_end
        seeds.append(
            ChunkSeed(
                ordinal=len(seeds),
                text=normalized_text[start:end],
                char_start=start,
                char_end=end,
            )
        )
        start = end
    return tuple(seeds)


def default_mime_types() -> frozenset[str]:
    """Return the immutable built-in allowlist without constructing a parser."""

    return _BUILTIN_MIME_TYPES
