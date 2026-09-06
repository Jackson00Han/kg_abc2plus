"""Prepared PDF excerpts retain source edition and reject changed artifacts."""

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from graphrag_prod.industrial.normalization import normalize_industrial_source, write_normalized_source
from tests.unit.test_pdf_parser import _pdf, _table_stream, _text


@dataclass(frozen=True)
class _Source:
    source_id: str
    sha256: str | None
    byte_size: int | None
    embedded_revision: str = "test-source-edition-v1"


class IndustrialNormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = _pdf((
            _text("Fictional source: this page is outside the selected excerpt."),
            _table_stream(),
        ))
        self.source = _Source("fixture", hashlib.sha256(self.payload).hexdigest(), len(self.payload))

    def test_excerpt_binds_complete_original_physical_page_and_values(self) -> None:
        result = normalize_industrial_source(self.source, self.payload, selected_pages=(2,))
        self.assertEqual(result["original_checksum"], self.source.sha256)
        self.assertEqual(result["selected_pages"], [2])
        self.assertEqual({item["page_number"] for item in result["chunks"]}, {2})
        self.assertNotIn("outside the selected", result["normalized_text"])
        self.assertIn("1250 A", result["normalized_text"])
        self.assertIn("24 kV", result["normalized_text"])
        digest = result.pop("artifact_checksum")
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(hashlib.sha256(encoded).hexdigest(), digest)

    def test_unpinned_or_changed_original_is_rejected_before_pdf_process(self) -> None:
        for source in (replace(self.source, sha256=None), replace(self.source, sha256="0" * 64), replace(self.source, byte_size=1)):
            with patch("graphrag_prod.industrial.normalization.PdfDocumentParser.parse") as parse:
                with self.assertRaises(ValueError):
                    normalize_industrial_source(source, self.payload, selected_pages=(1,))
                parse.assert_not_called()

    def test_artifact_write_is_idempotent_and_preserves_existing_different_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "normalized.json"
            write_normalized_source({"revision": 1}, path)
            original = path.read_bytes()
            write_normalized_source({"revision": 1}, path)
            with self.assertRaises(ValueError):
                write_normalized_source({"revision": 2}, path)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.iterdir()), [path])
