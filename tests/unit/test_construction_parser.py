"""Bounded document parsing and deterministic ChunkSeed tests."""

from __future__ import annotations

from dataclasses import dataclass
import json
import unittest

from graphrag_prod.construction import (
    BoundedDocumentParser,
    ChunkingConfig,
    DocumentParseError,
    ParserLimits,
    default_mime_types,
    split_gapless,
)


class ConstructionParserTests(unittest.TestCase):
    def test_json_collection_records_keep_exact_complete_spans(self) -> None:
        records = [
            {"id": f"device-{index}", "name": "电气设备", "note": 'escaped \\\" }, [ text',
             "nested": [{"unit": "A", "value": index}]}
            for index in range(5)
        ]
        for value in (records, {"metadata": {"version": 1}, "devices": records, "other": []}):
            source = json.dumps(value, ensure_ascii=False, indent=2)
            parser = BoundedDocumentParser(
                chunking=ChunkingConfig(max_chars=250), json_record_boundaries=True,
            )
            parsed = parser.parse(source.encode(), mime_type="application/json")
            self.assertEqual("".join(chunk.text for chunk in parsed.chunks), source)
            self.assertTrue(all(len(chunk.text) <= 250 for chunk in parsed.chunks))
            self.assertIn("json-record-boundaries:v1", parsed.splitter_signature)
            for index in range(5):
                # Exact nested record fields must remain in the same Chunk.
                owner = next(chunk for chunk in parsed.chunks if f'"device-{index}"' in chunk.text)
                self.assertIn(f'"value": {index}', owner.text)
            self.assertEqual(parser.parse(source.encode(), mime_type="application/json"), parsed)

    def test_json_record_splitter_bounds_oversized_values_and_leaves_other_formats_unchanged(self) -> None:
        source = json.dumps({"metadata": "x" * 300, "records": [{"id": "last-record"}]}, indent=2)
        parser = BoundedDocumentParser(chunking=ChunkingConfig(max_chars=80), json_record_boundaries=True)
        parsed = parser.parse(source.encode(), mime_type="application/json")
        self.assertTrue(all(len(chunk.text) <= 80 for chunk in parsed.chunks))
        self.assertEqual("".join(chunk.text for chunk in parsed.chunks), source)
        self.assertIn('"last-record"', parsed.chunks[-1].text)
        for value in ("[]", "{}", "42", '"text"'):
            parsed = parser.parse(value.encode(), mime_type="application/json")
            self.assertEqual("".join(chunk.text for chunk in parsed.chunks), value)
        ordinary = BoundedDocumentParser(chunking=ChunkingConfig(max_chars=80))
        self.assertEqual(parser.parse(b"ordinary text", mime_type="text/plain"),
                         ordinary.parse(b"ordinary text", mime_type="text/plain"))
        with self.assertRaisesRegex(DocumentParseError, "duplicate key"):
            parser.parse(b'{"items":[],"items":[]}', mime_type="application/json")

    def test_builtin_formats_are_strict_utf8_and_normalized(self) -> None:
        parser = BoundedDocumentParser(chunking=ChunkingConfig(max_chars=8))
        source = b"\xef\xbb\xbfCafe\xcc\x81\r\nstatus"
        parsed = parser.parse(source, mime_type="text/plain; charset=UTF-8")

        self.assertEqual(parsed.mime_type, "text/plain")
        self.assertEqual(parsed.normalized_text, "Caf\u00e9\nstatus")
        self.assertEqual("".join(item.text for item in parsed.chunks), parsed.normalized_text)
        self.assertEqual(parsed.chunks[0].char_start, 0)
        self.assertEqual(parsed.chunks[-1].char_end, len(parsed.normalized_text))
        self.assertEqual(
            default_mime_types(),
            frozenset(
                {"text/plain", "text/markdown", "text/csv", "application/json"}
            ),
        )

        with self.assertRaisesRegex(DocumentParseError, "valid UTF-8"):
            parser.parse(b"\xff", mime_type="text/markdown")
        with self.assertRaisesRegex(DocumentParseError, "control character"):
            parser.parse(b"hello\x00world", mime_type="text/plain")

    def test_size_and_mime_allowlist_are_enforced_before_parsing(self) -> None:
        parser = BoundedDocumentParser(
            limits=ParserLimits(max_source_bytes=4, max_normalized_chars=4)
        )
        with self.assertRaisesRegex(DocumentParseError, "byte limit"):
            parser.parse(b"12345", mime_type="text/plain")
        with self.assertRaisesRegex(DocumentParseError, "unsupported MIME"):
            parser.parse(b"abc", mime_type="application/pdf")
        with self.assertRaisesRegex(DocumentParseError, "UTF-8 charset"):
            parser.parse(b"abc", mime_type="text/plain; charset=latin-1")
        with self.assertRaises(TypeError):
            parser.parse("abc", mime_type="text/plain")  # type: ignore[arg-type]

    def test_json_and_csv_are_validated_with_resource_limits(self) -> None:
        parser = BoundedDocumentParser(
            limits=ParserLimits(
                max_source_bytes=1_000,
                max_normalized_chars=1_000,
                max_json_depth=3,
                max_csv_rows=2,
            )
        )
        parsed = parser.parse(b'{"asset":{"id":"P-1"}}', mime_type="application/json")
        self.assertEqual(parsed.normalized_text, '{"asset":{"id":"P-1"}}')
        with self.assertRaisesRegex(DocumentParseError, "duplicate key"):
            parser.parse(b'{"id":1,"id":2}', mime_type="application/json")
        with self.assertRaisesRegex(DocumentParseError, "nesting limit"):
            parser.parse(b'{"a":{"b":{"c":1}}}', mime_type="application/json")

        csv_document = parser.parse(b"asset,status\nP-1,online\n", mime_type="text/csv")
        self.assertEqual(csv_document.mime_type, "text/csv")
        with self.assertRaisesRegex(DocumentParseError, "row limit"):
            parser.parse(b"a\nb\nc\n", mime_type="text/csv")

    def test_chunking_is_repeatable_exact_and_gapless(self) -> None:
        text = "First paragraph.\n\nSecond paragraph is longer. Final sentence."
        config = ChunkingConfig(max_chars=24, minimum_boundary_ratio=0.5)
        first = split_gapless(text, config=config)
        second = split_gapless(text, config=config)

        self.assertEqual(first, second)
        self.assertEqual("".join(item.text for item in first), text)
        self.assertEqual(
            [(item.ordinal, item.char_start, item.char_end) for item in first],
            [
                (index, item.char_start, item.char_end)
                for index, item in enumerate(first)
            ],
        )
        self.assertTrue(all(len(item.text) <= config.max_chars for item in first))
        for left, right in zip(first, first[1:], strict=False):
            self.assertEqual(left.char_end, right.char_start)

    def test_custom_parser_plugins_are_explicit_and_post_bounded(self) -> None:
        @dataclass(frozen=True)
        class FuturePdfParser:
            mime_types: frozenset[str] = frozenset({"application/pdf"})

            def parse(self, payload: bytes) -> str:
                return "expanded text"

        parser = BoundedDocumentParser(
            plugins=(FuturePdfParser(),),
            limits=ParserLimits(max_source_bytes=100, max_normalized_chars=8),
        )
        self.assertEqual(parser.allowed_mime_types, frozenset({"application/pdf"}))
        with self.assertRaisesRegex(DocumentParseError, "character limit"):
            parser.parse(b"%PDF", mime_type="application/pdf")


if __name__ == "__main__":
    unittest.main()
