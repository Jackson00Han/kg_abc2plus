"""Bounded PDF extraction, numerical fidelity and exact physical source maps."""

from __future__ import annotations

from dataclasses import replace
import json
import subprocess
import unittest
from unittest.mock import patch
import zlib

from graphrag_prod.construction.parser import (
    BoundedDocumentParser,
    ChunkingConfig,
    DocumentParseError,
    NormalizedSource,
    ParserLimits,
    SourceLocation,
    Utf8TextParser,
    default_mime_types,
)
from graphrag_prod.construction.pdf_parser import PdfDocumentParser, PdfParserLimits
from graphrag_prod.domain.ids import content_checksum


def _text(value: str, x: int = 50, y: int = 740) -> bytes:
    escaped = value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return f"BT /F1 12 Tf {x} {y} Td ({escaped}) Tj ET\n".encode("ascii")


def _pdf(streams: tuple[bytes, ...], *, compressed: bool = False, encrypted: bool = False) -> bytes:
    """Tiny deterministic PDF fixture, assembled in memory without dependencies."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    page_ids = []
    for stream in streams:
        page_id = len(objects) + 1
        page_ids.append(page_id)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] /Resources << /Font << /F1 3 0 R >> >> /Contents {page_id + 1} 0 R >>".encode()
        )
        payload = zlib.compress(stream) if compressed else stream
        filter_entry = b" /Filter /FlateDecode" if compressed else b""
        objects.append(b"<< /Length " + str(len(payload)).encode() + filter_entry + b" >>\nstream\n" + payload + b"\nendstream")
    objects[1] = f"<< /Type /Pages /Count {len(page_ids)} /Kids [{' '.join(f'{number} 0 R' for number in page_ids)}] >>".encode()
    if encrypted:
        objects.append(b"<< /Filter /Standard /V 1 /R 2 /Length 40 /O <" + b"00" * 32 + b"> /U <" + b"00" * 32 + b"> /P -4 >>")
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    extra = f" /Encrypt {len(objects)} 0 R /ID [<{'00' * 16}> <{'00' * 16}>]" if encrypted else ""
    output.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R{extra} >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(output)


def _table_stream(*, merged: bool = False) -> bytes:
    lines = b"0.5 w\n"
    for x in (50, 230, 410):
        lines += f"{x} 560 m {x} 680 l S\n".encode()
    for y in (560, 600, 640, 680):
        left = 230 if merged and y == 600 else 50
        lines += f"{left} {y} m 410 {y} l S\n".encode()
    return b"".join((
        _text("Fictional electrical assets; values are test data", y=740),
        lines,
        _text("Device", 60, 655), _text("Rating", 240, 655),
        _text("TESTBUS-001", 60, 615), _text("1250 A", 240, 615),
        _text("TESTVCB-001", 60, 575), _text("24 kV", 240, 575),
    ))


class PdfParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = _pdf((
            _text("Maintenance history belongs to the identified device.")
            + _text("Historical and current observations require dates.", y=700),
            _table_stream(),
        ))

    def parser(self, **kwargs: object) -> BoundedDocumentParser:
        return BoundedDocumentParser(
            plugins=(Utf8TextParser(), PdfDocumentParser(**kwargs)),
            chunking=ChunkingConfig(max_chars=64),
        )

    def test_opt_in_preserves_default_allowlist_and_text_plugins(self) -> None:
        self.assertNotIn("application/pdf", default_mime_types())
        with self.assertRaisesRegex(DocumentParseError, "unsupported MIME"):
            BoundedDocumentParser().parse(self.payload, mime_type="application/pdf")
        result = self.parser().parse(b"ordinary source text", mime_type="text/plain")
        self.assertEqual(result.source_locations, ())
        self.assertIsNone(result.parser_version)

    def test_multipage_checksum_exact_gapless_chunks_and_determinism(self) -> None:
        first = self.parser().parse(self.payload, mime_type="application/pdf")
        second = self.parser().parse(self.payload, mime_type="application/pdf")
        self.assertEqual(first, second)
        self.assertEqual(first.original_checksum, content_checksum(self.payload))
        self.assertEqual(first.normalized_checksum, content_checksum(first.normalized_text))
        self.assertEqual(first.selected_pages, (1, 2))
        self.assertEqual("".join(seed.text for seed in first.chunks), first.normalized_text)
        pages = {item.page_number: item for item in first.source_locations if item.kind == "page"}
        self.assertEqual({seed.page_number for seed in first.chunks}, {1, 2})
        for index, seed in enumerate(first.chunks):
            self.assertEqual(seed.ordinal, index)
            self.assertEqual(seed.text, first.normalized_text[seed.char_start:seed.char_end])
            self.assertLessEqual(len(seed.text), 64)
            page = pages[seed.page_number]
            self.assertGreaterEqual(seed.char_start, page.char_start)
            self.assertLessEqual(seed.char_end, page.char_end)
        for left, right in zip(first.chunks, first.chunks[1:]):
            self.assertEqual(left.char_end, right.char_start)

    def test_table_cells_keep_exact_numbers_units_and_physical_positions(self) -> None:
        result = self.parser().parse(self.payload, mime_type="application/pdf")
        cells = {
            (item.row_number, item.column_number): (item, result.normalized_text[item.char_start:item.char_end])
            for item in result.source_locations if item.kind == "table_cell"
        }
        self.assertEqual(len(cells), 6)
        self.assertEqual(cells[(2, 2)][1], "1250 A")
        self.assertEqual(cells[(3, 2)][1], "24 kV")
        location = cells[(2, 2)][0]
        self.assertEqual(location.page_number, 2)
        self.assertEqual(location.table_number, 1)
        self.assertEqual(location.bbox, (230.0, 160.0, 410.0, 200.0))
        self.assertIn("TESTBUS-001\t1250 A\n", result.normalized_text)
        self.assertEqual(result.normalized_text.count("1250 A"), 1)
        self.assertEqual(result.normalized_text.count("Fictional electrical assets"), 1)

    def test_page_selection_is_explicit_and_keeps_original_page_and_checksum(self) -> None:
        result = self.parser(selected_pages=(2,)).parse(self.payload, mime_type="application/pdf")
        self.assertEqual(result.selected_pages, (2,))
        self.assertEqual(result.original_checksum, content_checksum(self.payload))
        self.assertEqual({seed.page_number for seed in result.chunks}, {2})
        self.assertNotIn("Maintenance history", result.normalized_text)
        self.assertIn("pages=2", result.splitter_signature)
        with self.assertRaisesRegex(DocumentParseError, "PDF_PAGE_SELECTION"):
            self.parser(selected_pages=(3,)).parse(self.payload, mime_type="application/pdf")

    def test_merged_cell_placeholder_never_invents_a_physical_cell_box(self) -> None:
        result = self.parser().parse(_pdf((_table_stream(merged=True),)), mime_type="application/pdf")
        cells = {
            (item.row_number, item.column_number): item
            for item in result.source_locations if item.kind == "table_cell"
        }
        self.assertNotIn((3, 1), cells)
        self.assertEqual(len(cells), 5)
        self.assertEqual(cells[(2, 1)].bbox, (50.0, 160.0, 230.0, 240.0))
        self.assertIn("\n\t24 kV\n", result.normalized_text)

    def test_sparse_selected_page_fails_entire_parse(self) -> None:
        payload = _pdf((_text("This page contains sufficient genuine machine readable text."), _text("2")))
        with self.assertRaisesRegex(DocumentParseError, "OCR_REQUIRED: physical page 2"):
            self.parser().parse(payload, mime_type="application/pdf")
        # Explicit selection is a derivative, never an implicit claim that page 2 was read.
        result = self.parser(selected_pages=(1,)).parse(payload, mime_type="application/pdf")
        self.assertEqual(result.selected_pages, (1,))

    def test_encrypted_and_malformed_documents_are_rejected(self) -> None:
        for payload in (b"not a pdf", b"%PDF-1.4\n%%EOF\n", self.payload[:-6]):
            with self.subTest(payload=payload[:20]):
                with self.assertRaisesRegex(DocumentParseError, "PDF_MALFORMED"):
                    self.parser().parse(payload, mime_type="application/pdf")
        with self.assertRaisesRegex(DocumentParseError, "PDF_ENCRYPTED"):
            self.parser().parse(_pdf((_text("encrypted fixture text"),), encrypted=True), mime_type="application/pdf")

    def test_source_and_normalized_bounds(self) -> None:
        with self.assertRaisesRegex(DocumentParseError, "byte limit"):
            self.parser(limits=PdfParserLimits(max_source_bytes=10)).parse(self.payload, mime_type="application/pdf")
        for limit in (PdfParserLimits(max_normalized_chars=30), PdfParserLimits(max_page_chars=10)):
            with self.subTest(limit=limit):
                with self.assertRaisesRegex(DocumentParseError, "PDF_LIMIT"):
                    self.parser(limits=limit).parse(self.payload, mime_type="application/pdf")
        with self.assertRaisesRegex(DocumentParseError, "character limit"):
            BoundedDocumentParser(
                plugins=(PdfDocumentParser(),), limits=ParserLimits(max_normalized_chars=10)
            ).parse(self.payload, mime_type="application/pdf")

    def test_page_table_source_map_and_decoded_stream_bounds(self) -> None:
        for limits in (
            PdfParserLimits(max_document_pages=1),
            PdfParserLimits(max_selected_pages=1),
            PdfParserLimits(max_table_cells=3),
            PdfParserLimits(max_source_locations=2),
            PdfParserLimits(max_page_objects=2),
            PdfParserLimits(max_decoded_stream_bytes=20),
        ):
            with self.subTest(limits=limits):
                with self.assertRaisesRegex(DocumentParseError, "PDF_LIMIT"):
                    self.parser(limits=limits).parse(self.payload, mime_type="application/pdf")
        compressed = _pdf((_text("Repeated reliable fixture text. " * 100),), compressed=True)
        with self.assertRaisesRegex(DocumentParseError, "decoded streams"):
            self.parser(limits=PdfParserLimits(max_decoded_stream_bytes=1_000)).parse(compressed, mime_type="application/pdf")

    def test_worker_deadline_and_abnormal_exit_fail_closed(self) -> None:
        with patch("graphrag_prod.construction.pdf_parser.subprocess.run", side_effect=subprocess.TimeoutExpired("worker", 0.1)):
            with self.assertRaisesRegex(DocumentParseError, "PDF_TIMEOUT"):
                PdfDocumentParser().parse(self.payload)
        with patch("graphrag_prod.construction.pdf_parser.subprocess.run", return_value=subprocess.CompletedProcess([], -9, stdout=b"")):
            with self.assertRaisesRegex(DocumentParseError, "PDF_WORKER_FAILED"):
                PdfDocumentParser().parse(self.payload)
        with patch("graphrag_prod.construction.pdf_parser.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps({"unexpected": True}).encode())):
            with self.assertRaisesRegex(DocumentParseError, "invalid worker response"):
                PdfDocumentParser().parse(self.payload)
        bad_map = {
            "text": "abc", "selected_pages": [1], "parser_version": "fixture:v1",
            "source_locations": [{"char_start": 0, "char_end": 4, "page_number": 1,
                                  "kind": "page", "bbox": [0, 0, 10, 10]}],
        }
        with patch("graphrag_prod.construction.pdf_parser.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(bad_map).encode())):
            with self.assertRaisesRegex(DocumentParseError, "PDF_WORKER_FAILED"):
                PdfDocumentParser().parse(self.payload)

    def test_invalid_limits_and_selection_are_rejected(self) -> None:
        for pages in ((), (0,), (True,), (2, 1), (1, 1), [1]):
            with self.subTest(pages=pages):
                with self.assertRaises(ValueError):
                    PdfDocumentParser(selected_pages=pages)
        with self.assertRaises(ValueError):
            PdfDocumentParser(limits=PdfParserLimits(max_selected_pages=1), selected_pages=(1, 2))
        for kwargs in ({"max_selected_pages": 0}, {"max_page_chars": True}, {"timeout_seconds": float("nan")}):
            with self.assertRaises(ValueError):
                PdfParserLimits(**kwargs)

    def test_rich_source_map_rejects_gaps_out_of_page_locations_and_mutability(self) -> None:
        page = SourceLocation(0, 3, 1, "page", (0., 0., 10., 10.))
        valid = NormalizedSource("abc", (page,), "fixture:v1", (1,))
        for kwargs in (
            {"source_locations": (replace(page, char_start=1),)},
            {"source_locations": (page, SourceLocation(2, 4, 1, "text", page.bbox))},
            {"source_locations": (page, SourceLocation(0, 2, 1, "text", (200., 200., 210., 210.)))},
            {"source_locations": [page]},
            {"selected_pages": (2,)},
            {"text": "a\r\n"},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    replace(valid, **kwargs)

    def test_source_location_scalar_types_fail_before_chunk_slicing(self) -> None:
        page = SourceLocation(0, 3, 1, "page", (0., 0., 10., 10.))
        for name in ("char_start", "char_end", "page_number", "table_number", "row_number", "column_number"):
            for value in (True, 1.5, "1"):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ValueError):
                        replace(page, **{name: value})
        for box in ((False, 0., 10., 10.), (0., 0., float("nan"), 10.)):
            with self.assertRaises(ValueError):
                replace(page, bbox=box)
        for pages in ((True,), (1.0,), (0,)):
            with self.assertRaises(ValueError):
                NormalizedSource("abc", (page,), "fixture:v1", pages)


if __name__ == "__main__":
    unittest.main()
