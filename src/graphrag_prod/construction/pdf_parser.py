"""Opt-in PDF normalization with bounded isolated extraction and source maps.

PDFs are untrusted inputs. No OCR, model calls, embedded actions, or URL fetching
occur here. The default text parser registry is intentionally unchanged.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import io
import json
import math
import subprocess
import sys
from typing import Any

from graphrag_prod.construction.parser import (
    DEFAULT_MAX_SOURCE_BYTES,
    DocumentParseError,
    NormalizedSource,
    SourceLocation,
    _normalize_text,
)


PDF_PARSER_VERSION = "pdfplumber-source-map:v1"
# pdfplumber's documented font-relative word spacing avoids joining adjacent
# words in the small-font official manuals (a fixed 3pt tolerance is too wide).
_TEXT_SETTINGS = {"x_tolerance_ratio": 0.15}


@dataclass(frozen=True, slots=True)
class PdfParserLimits:
    """Per-process PDF bounds; large offline files require explicit byte limits."""

    max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES
    max_document_pages: int = 1_000
    max_selected_pages: int = 24
    max_decoded_stream_bytes: int = 32 * 1024 * 1024
    max_page_chars: int = 200_000
    max_normalized_chars: int = 500_000
    max_table_cells: int = 10_000
    max_source_locations: int = 50_000
    max_page_objects: int = 250_000
    min_page_alphanumeric_chars: int = 24
    min_image_page_alphanumeric_chars: int = 120
    timeout_seconds: float = 30.0
    max_worker_address_bytes: int = 1024 * 1024 * 1024

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if name == "timeout_seconds":
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value <= 0
                ):
                    raise ValueError("timeout_seconds must be finite and positive")
            elif isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


def _selection(pages: tuple[int, ...] | None, limit: int) -> None:
    if pages is None:
        return
    if (
        not isinstance(pages, tuple)
        or not pages
        or any(isinstance(page, bool) or not isinstance(page, int) or page < 1 for page in pages)
        or tuple(sorted(set(pages))) != pages
    ):
        raise ValueError("selected_pages must be an ordered tuple of unique positive pages")
    if len(pages) > limit:
        raise ValueError("selected_pages exceeds the configured selected page limit")


@dataclass(frozen=True, slots=True)
class PdfDocumentParser:
    """Explicit PDF plugin. Physical selected pages are never renumbered.

    ``None`` means all pages, subject to the selection cap. A tuple means only
    those declared pages; the original checksum still covers the complete PDF.
    """

    limits: PdfParserLimits = PdfParserLimits()
    selected_pages: tuple[int, ...] | None = None
    mime_types: frozenset[str] = frozenset({"application/pdf"})

    def __post_init__(self) -> None:
        _selection(self.selected_pages, self.limits.max_selected_pages)

    def parse(self, payload: bytes) -> NormalizedSource:
        if not isinstance(payload, bytes):
            raise TypeError("payload must be bytes")
        if len(payload) > self.limits.max_source_bytes:
            raise DocumentParseError("PDF_LIMIT: source exceeds the configured byte limit")
        if not payload.startswith(b"%PDF-") or b"%%EOF" not in payload[-1024:]:
            raise DocumentParseError("PDF_MALFORMED: invalid PDF envelope")
        config = json.dumps(
            {"limits": asdict(self.limits), "selected_pages": self.selected_pages},
            separators=(",", ":"),
        )
        try:
            result = subprocess.run(
                [sys.executable, "-m", __name__, "--worker", config],
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=self.limits.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            # subprocess.run kills and reaps the worker before raising.
            raise DocumentParseError("PDF_TIMEOUT: extraction exceeded its deadline") from exc
        if result.returncode != 0:
            raise DocumentParseError("PDF_WORKER_FAILED: extraction failed or exhausted resources")
        # Each location contains only bounded scalar fields, never source text.
        max_response_bytes = (
            self.limits.max_normalized_chars * 12
            + self.limits.max_source_locations * 1_024
            + 8_192
        )
        if len(result.stdout) > max_response_bytes:
            raise DocumentParseError("PDF_LIMIT: worker response exceeds configured bounds")
        try:
            response = json.loads(result.stdout)
            if "error" in response:
                raise DocumentParseError(response["error"])
            return NormalizedSource(
                text=response["text"],
                source_locations=tuple(
                    SourceLocation(**{**item, "bbox": tuple(item["bbox"])})
                    for item in response["source_locations"]
                ),
                parser_version=response["parser_version"],
                selected_pages=tuple(response["selected_pages"]),
            )
        except DocumentParseError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise DocumentParseError("PDF_WORKER_FAILED: invalid worker response") from exc


def _bbox(value: Any) -> tuple[float, float, float, float]:
    return tuple(round(float(number), 4) for number in value)  # type: ignore[return-value]


def _contains(box: Any, character: dict[str, Any]) -> bool:
    x = (character["x0"] + character["x1"]) / 2
    y = (character["top"] + character["bottom"]) / 2
    return box[0] <= x < box[2] and box[1] <= y < box[3]


def _extract_pdf(
    payload: bytes, limits: PdfParserLimits, selected_pages: tuple[int, ...] | None
) -> NormalizedSource:
    import pdfplumber
    from pdfminer.pdfpage import PDFPage
    from pdfminer.pdftypes import PDFStream

    # Includes content, font and form streams decoded by pdfminer. This checks
    # each decoded stream before layout processing; OS bounds/timeout cover a
    # decoder allocating while expanding an individual compressed stream.
    original_get_data = PDFStream.get_data
    decoded_streams: dict[int, Any] = {}
    decoded_bytes = 0

    def bounded_get_data(stream: Any) -> bytes:
        nonlocal decoded_bytes
        data = original_get_data(stream)
        marker = id(stream)
        if marker not in decoded_streams:
            # Retain each bounded stream so Python cannot recycle its id after
            # a page cache is closed and evade the cumulative decoded-byte cap.
            decoded_streams[marker] = stream
            decoded_bytes += len(data)
            if decoded_bytes > limits.max_decoded_stream_bytes:
                raise DocumentParseError("PDF_LIMIT: decoded streams exceed byte limit")
        return data

    PDFStream.get_data = bounded_get_data
    try:
        with pdfplumber.open(io.BytesIO(payload), unicode_norm="NFC") as pdf:
            if pdf.doc.encryption is not None:
                raise DocumentParseError("PDF_ENCRYPTED: encrypted PDFs are unsupported")
            count = 0
            for _ in PDFPage.create_pages(pdf.doc):
                count += 1
                if count > limits.max_document_pages:
                    raise DocumentParseError("PDF_LIMIT: document exceeds page limit")
            if count == 0:
                raise DocumentParseError("PDF_MALFORMED: document has no pages")
            pages = selected_pages or tuple(range(1, count + 1))
            if len(pages) > limits.max_selected_pages:
                raise DocumentParseError("PDF_LIMIT: explicitly select fewer physical pages")
            if pages[-1] > count:
                raise DocumentParseError("PDF_PAGE_SELECTION: selected page does not exist")
            # pdfplumber honors this physical-page selection when building Page objects.
            pdf.pages_to_parse = list(pages)
            return _normalize_pages(pdf.pages, pages, limits, pdfplumber)
    finally:
        PDFStream.get_data = original_get_data


def _normalize_pages(
    pdf_pages: Any, selected_pages: tuple[int, ...], limits: PdfParserLimits, pdfplumber: Any
) -> NormalizedSource:
    fragments: list[str] = []
    locations: list[SourceLocation] = []
    cursor = 0
    cell_count = 0

    def append(value: str) -> tuple[int, int]:
        nonlocal cursor
        start = cursor
        cursor += len(value)
        if cursor > limits.max_normalized_chars:
            raise DocumentParseError("PDF_LIMIT: normalized source exceeds character limit")
        fragments.append(value)
        return start, cursor

    def locate(start: int, end: int, page: int, kind: str, box: Any, **fields: Any) -> None:
        locations.append(SourceLocation(start, end, page, kind, _bbox(box), **fields))
        if len(locations) > limits.max_source_locations:
            raise DocumentParseError("PDF_LIMIT: source map exceeds location limit")

    for page in pdf_pages:
        page_start = cursor
        characters = page.chars
        if len(characters) > limits.max_page_chars:
            raise DocumentParseError("PDF_LIMIT: page exceeds character limit")
        if sum(len(items) for items in page.objects.values()) > limits.max_page_objects:
            raise DocumentParseError("PDF_LIMIT: page exceeds object limit")
        raw_text = "".join(character["text"] for character in characters)
        alphanumeric_count = sum(character.isalnum() for character in raw_text)
        large_image = any(
            (item["x1"] - item["x0"]) * (item["bottom"] - item["top"])
            >= page.width * page.height * 0.5
            for item in page.images
        )
        if (
            alphanumeric_count < limits.min_page_alphanumeric_chars
            or (large_image and alphanumeric_count < limits.min_image_page_alphanumeric_chars)
            or "(cid:" in raw_text
        ):
            raise DocumentParseError(
                f"OCR_REQUIRED: physical page {page.page_number} has insufficient reliable text; "
                "no selected page was omitted"
            )
        # Established line-based pdfplumber table detection, without speculative
        # reconstruction of borderless tables or diagram semantics.
        tables = sorted(page.find_tables(), key=lambda table: (table.bbox[1], table.bbox[0]))
        table_cells: list[tuple[Any, int, int, int, list[dict[str, Any]]]] = []
        table_rows: list[tuple[Any, int, int, list[int]]] = []
        for table_number, table in enumerate(tables, 1):
            for row_number, row in enumerate(table.rows, 1):
                indexes = []
                for column_number, box in enumerate(row.cells, 1):
                    cell_count += 1
                    if cell_count > limits.max_table_cells:
                        raise DocumentParseError("PDF_LIMIT: tables exceed cell limit")
                    indexes.append(len(table_cells))
                    table_cells.append((box, table_number, row_number, column_number, []))
                table_rows.append((row.bbox, table_number, row_number, indexes))
        outside_characters: set[int] = set()
        for character in characters:
            assigned = False
            for box, _, _, _, cell_characters in table_cells:
                if box is not None and _contains(box, character):
                    if assigned:
                        raise DocumentParseError("PDF_TABLE_AMBIGUOUS: overlapping text cells")
                    cell_characters.append(character)
                    assigned = True
            if not assigned:
                outside_characters.add(id(character))
        prose = page.filter(
            lambda item: item.get("object_type") != "char" or id(item) in outside_characters
        )
        blocks: list[tuple[float, float, str, Any]] = [
            (line["top"], line["x0"], "text", line)
            for line in prose.extract_text_lines(
                layout=False, return_chars=False, **_TEXT_SETTINGS
            )
        ]
        blocks.extend((box[1], box[0], "table_row", (box, table, row, indexes))
                      for box, table, row, indexes in table_rows)
        for _, _, kind, block in sorted(blocks, key=lambda item: (item[0], item[1], item[2])):
            if kind == "text":
                value = _normalize_text(block["text"])
                start, end = append(value)
                locate(start, end, page.page_number, "text",
                       (block["x0"], block["top"], block["x1"], block["bottom"]))
                append("\n")
            else:
                box, table_number, row_number, indexes = block
                row_start = cursor
                for offset, index in enumerate(indexes):
                    cell_box, _, _, column_number, cell_characters = table_cells[index]
                    if offset:
                        append("\t")
                    value = (
                        pdfplumber.utils.extract_text(cell_characters, **_TEXT_SETTINGS)
                        if cell_characters
                        else ""
                    )
                    value = _normalize_text(value) if value.strip() else ""
                    start, end = append(value)
                    # A None entry is an absent placeholder beneath/beside a
                    # merged cell, not a physical cell with the row's box.
                    # Preserve its delimiter but never invent a cell location.
                    if cell_box is not None:
                        locate(start, end, page.page_number, "table_cell", cell_box,
                               table_number=table_number, row_number=row_number,
                               column_number=column_number)
                append("\n")
                locate(row_start, cursor, page.page_number, "table_row", box,
                       table_number=table_number, row_number=row_number)
        if cursor == page_start:
            raise DocumentParseError(f"OCR_REQUIRED: physical page {page.page_number} produced no text")
        # The final newline belongs to this page, preserving gapless chunks.
        locate(page_start, cursor, page.page_number, "page", page.bbox)
        page.close()
    return NormalizedSource("".join(fragments), tuple(locations), PDF_PARSER_VERSION, selected_pages)


def _worker() -> None:
    config = json.loads(sys.argv[2])
    limits = PdfParserLimits(**config["limits"])
    pages = config["selected_pages"]
    selected_pages = tuple(pages) if pages is not None else None
    _selection(selected_pages, limits.max_selected_pages)
    try:
        import resource

        cpu_seconds = max(1, math.ceil(limits.timeout_seconds))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        # macOS does not reliably implement RLIMIT_AS. Its CPU/deadline and
        # incremental extraction bounds remain active; see the limitation doc.
        if sys.platform.startswith("linux"):
            resource.setrlimit(
                resource.RLIMIT_AS,
                (limits.max_worker_address_bytes, limits.max_worker_address_bytes),
            )
        payload = sys.stdin.buffer.read(limits.max_source_bytes + 1)
        if len(payload) > limits.max_source_bytes:
            raise DocumentParseError("PDF_LIMIT: source exceeds byte limit")
        result = _extract_pdf(payload, limits, selected_pages)
        response = asdict(result)
    except Exception as exc:
        # Provider/parser errors may contain protected source content. Emit only
        # a fixed category, and never write a traceback into logs or the API.
        response = {"error": _safe_error(exc)}
    sys.stdout.write(json.dumps(response, ensure_ascii=True, separators=(",", ":")))


def _safe_error(error: BaseException) -> str:
    """pdfplumber wraps pdfminer and our own decoder-bound exceptions."""

    current: BaseException | None = error
    for _ in range(8):
        if current is None:
            break
        if isinstance(current, DocumentParseError):
            return str(current)
        if type(current).__name__ in {"PDFPasswordIncorrect", "PDFEncryptionError"}:
            return "PDF_ENCRYPTED: encrypted PDFs are unsupported"
        current = current.__cause__ or current.__context__
    return "PDF_MALFORMED: PDF could not be parsed safely"


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--worker":
        raise SystemExit("This module is an internal bounded PDF worker")
    _worker()
