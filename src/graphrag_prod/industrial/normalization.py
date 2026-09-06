"""Checksum-pinned PDF excerpts for offline industrial source preparation."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from typing import Any

from graphrag_prod.construction.parser import (
    BoundedDocumentParser, ChunkingConfig, ParserLimits,
)
from graphrag_prod.construction.pdf_parser import PdfDocumentParser, PdfParserLimits


OFFLINE_ORIGINAL_BYTES = 96 * 1024 * 1024


def normalize_industrial_source(
    source: Any,
    payload: bytes,
    *,
    selected_pages: tuple[int, ...],
    chunk_chars: int = 900,
) -> dict[str, Any]:
    """Bind a declared excerpt to its complete original and physical pages.

    This prepares evidence only. It does not create expert assertions, call an
    embedding/extraction provider, or grant a source publication authority.
    """
    if not source.sha256 or not source.byte_size:
        raise ValueError("source must be checksum-pinned before normalization")
    if len(payload) > OFFLINE_ORIGINAL_BYTES or len(payload) != source.byte_size:
        raise ValueError("source bytes do not match the bounded catalog")
    if hashlib.sha256(payload).hexdigest() != source.sha256:
        raise ValueError("source checksum differs from the catalog edition")
    if isinstance(chunk_chars, bool) or not isinstance(chunk_chars, int) or not 400 <= chunk_chars <= 2000:
        raise ValueError("chunk_chars must be between 400 and 2000")
    parser = BoundedDocumentParser(
        limits=ParserLimits(max_source_bytes=OFFLINE_ORIGINAL_BYTES),
        chunking=ChunkingConfig(max_chars=chunk_chars),
        plugins=(PdfDocumentParser(
            limits=PdfParserLimits(max_source_bytes=OFFLINE_ORIGINAL_BYTES),
            selected_pages=selected_pages,
        ),),
    )
    parsed = parser.parse(payload, mime_type="application/pdf")
    body: dict[str, Any] = {
        "schema_version": "industrial-normalized-source-v1",
        "source": asdict(source),
        "original_checksum": parsed.original_checksum,
        "normalized_checksum": parsed.normalized_checksum,
        "normalized_text": parsed.normalized_text,
        "parser_version": parsed.parser_version,
        "splitter_signature": parsed.splitter_signature,
        "selected_pages": list(parsed.selected_pages),
        "is_explicit_excerpt": True,
        "source_locations": [asdict(item) for item in parsed.source_locations],
        "chunks": [asdict(item) for item in parsed.chunks],
        "limitations": [
            "Only declared physical pages were normalized; other pages are outside this excerpt.",
            "Table geometry is extracted evidence, not confirmation of technical applicability.",
            "Diagrams and causal conclusions require separate review; no OCR is performed.",
        ],
    }
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {**body, "artifact_checksum": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}


def write_normalized_source(bundle: dict[str, Any], output_path: Path) -> None:
    """Write one deterministic external artifact, rejecting changed replays."""
    import os
    import tempfile

    output_path = output_path.resolve()
    payload = (json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if output_path.exists():
        if output_path.read_bytes() != payload:
            raise ValueError("normalization output already exists with different content")
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".normalizing-", dir=output_path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # Hard-link publication is atomic and does not overwrite a concurrent writer.
        try:
            os.link(name, output_path)
        except FileExistsError:
            if output_path.read_bytes() != payload:
                raise ValueError("concurrent normalization output differs") from None
    finally:
        Path(name).unlink(missing_ok=True)
