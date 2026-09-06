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
class SourceLocation:
    """An exact normalized-text range mapped to a physical source location.

    PDF boxes use pdfplumber points from the top-left of the extracted page.
    Table indexes, rows and columns are one-based. Empty cells have empty
    ranges; their location is retained instead of fabricating a value.
    """

    char_start: int
    char_end: int
    page_number: int
    kind: str
    bbox: tuple[float, float, float, float]
    table_number: int | None = None
    row_number: int | None = None
    column_number: int | None = None

    def __post_init__(self) -> None:
        for name in ("char_start", "char_end", "page_number"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"source location {name} must be an integer")
        if self.char_start < 0 or self.char_end < self.char_start:
            raise ValueError("source location range is invalid")
        if self.kind not in {"page", "text", "table_row", "table_cell"}:
            raise ValueError("unknown source location kind")
        if self.kind != "table_cell" and self.char_start == self.char_end:
            raise ValueError("source location must not be empty")
        if self.page_number <= 0:
            raise ValueError("source location page must be positive")
        if (
            not isinstance(self.bbox, tuple)
            or len(self.bbox) != 4
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                for value in self.bbox
            )
            or self.bbox[0] > self.bbox[2]
            or self.bbox[1] > self.bbox[3]
        ):
            raise ValueError("source location bounding box is invalid")
        required = {
            "table_row": ("table_number", "row_number"),
            "table_cell": ("table_number", "row_number", "column_number"),
        }.get(self.kind, ())
        for name in ("table_number", "row_number", "column_number"):
            value = getattr(self, name)
            if name in required and value is None:
                raise ValueError(f"{self.kind} requires {name}")
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True, slots=True)
class NormalizedSource:
    """Opt-in rich parser result; its offsets already refer to normalized text."""

    text: str
    source_locations: tuple[SourceLocation, ...]
    parser_version: str
    selected_pages: tuple[int, ...]

    def __post_init__(self) -> None:
        if _normalize_text(self.text) != self.text:
            raise ValueError("rich parser text must already be normalized")
        if not isinstance(self.source_locations, tuple) or any(
            not isinstance(item, SourceLocation) for item in self.source_locations
        ):
            raise ValueError("source locations must be immutable")
        if not isinstance(self.parser_version, str) or not self.parser_version.strip():
            raise ValueError("rich parser requires a version")
        if (
            not isinstance(self.selected_pages, tuple)
            or not self.selected_pages
            or any(
                isinstance(page, bool) or not isinstance(page, int) or page <= 0
                for page in self.selected_pages
            )
            or tuple(sorted(set(self.selected_pages))) != self.selected_pages
        ):
            raise ValueError("selected pages must be ordered and unique")
        pages = tuple(item for item in self.source_locations if item.kind == "page")
        if tuple(item.page_number for item in pages) != self.selected_pages:
            raise ValueError("page ranges must match selected pages")
        cursor = 0
        for page in pages:
            if page.char_start != cursor:
                raise ValueError("page ranges must be gapless")
            cursor = page.char_end
        if cursor != len(self.text):
            raise ValueError("page ranges must cover the complete normalized source")
        page_by_number = {page.page_number: page for page in pages}
        for item in self.source_locations:
            page = page_by_number.get(item.page_number)
            if page is None or not (
                page.char_start <= item.char_start <= item.char_end <= page.char_end
            ):
                raise ValueError("source location exceeds its page range")
            tolerance = 0.001  # Rounded PDF point coordinates, not pixel margins.
            if (
                item.bbox[0] < page.bbox[0] - tolerance
                or item.bbox[1] < page.bbox[1] - tolerance
                or item.bbox[2] > page.bbox[2] + tolerance
                or item.bbox[3] > page.bbox[3] + tolerance
            ):
                raise ValueError("source location bounding box exceeds its physical page")


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """Normalized source plus an exact, gapless ChunkSeed projection."""

    mime_type: str
    normalized_text: str
    original_checksum: str
    normalized_checksum: str
    splitter_signature: str
    chunks: tuple[ChunkSeed, ...]
    source_locations: tuple[SourceLocation, ...] = ()
    parser_version: str | None = None
    selected_pages: tuple[int, ...] = ()

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
        if self.source_locations:
            source = NormalizedSource(
                self.normalized_text,
                self.source_locations,
                self.parser_version or "",
                self.selected_pages,
            )
            pages = {
                item.page_number: item
                for item in source.source_locations
                if item.kind == "page"
            }
            for seed in self.chunks:
                page = pages.get(seed.page_number)
                if page is None or not (
                    page.char_start <= seed.char_start < seed.char_end <= page.char_end
                ):
                    raise ValueError("located chunk must stay within one physical page")
        elif self.parser_version is not None or self.selected_pages:
            raise ValueError("rich parser metadata requires source locations")


class DocumentParserPlugin(Protocol):
    """A parser selected solely through an explicitly registered MIME type."""

    @property
    def mime_types(self) -> frozenset[str]: ...

    def parse(self, payload: bytes) -> str | NormalizedSource: ...


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
        if not isinstance(parsed, (str, NormalizedSource)):
            raise TypeError("parser plugins must return text or NormalizedSource")
        located = parsed if isinstance(parsed, NormalizedSource) else None
        normalized = located.text if located else _normalize_text(parsed)
        if len(normalized) > self.limits.max_normalized_chars:
            raise DocumentParseError("normalized source exceeds the character limit")
        chunks = (
            split_located_gapless(located, config=self.chunking)
            if located
            else split_gapless(normalized, config=self.chunking)
        )
        return ParsedDocument(
            mime_type=canonical_mime,
            normalized_text=normalized,
            original_checksum=content_checksum(payload),
            normalized_checksum=content_checksum(normalized),
            splitter_signature=(
                f"{self.chunking.signature}|{located.parser_version}:"
                f"pages={','.join(str(page) for page in located.selected_pages)}"
                if located
                else self.chunking.signature
            ),
            chunks=chunks,
            source_locations=located.source_locations if located else (),
            parser_version=located.parser_version if located else None,
            selected_pages=located.selected_pages if located else (),
        )


def split_located_gapless(
    source: NormalizedSource, *, config: ChunkingConfig | None = None
) -> tuple[ChunkSeed, ...]:
    """Apply the existing splitter independently inside each selected page."""

    seeds: list[ChunkSeed] = []
    for page in source.source_locations:
        if page.kind != "page":
            continue
        for seed in split_gapless(
            source.text[page.char_start:page.char_end], config=config
        ):
            seeds.append(
                ChunkSeed(
                    ordinal=len(seeds),
                    text=seed.text,
                    char_start=page.char_start + seed.char_start,
                    char_end=page.char_start + seed.char_end,
                    page_number=page.page_number,
                    section=f"PDF physical page {page.page_number}",
                )
            )
    return tuple(seeds)


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
