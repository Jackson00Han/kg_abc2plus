"""Offline industrial source material and evidence-safe expert import binding.

The kit contains fictional, project-authorized test material only. Reading or
binding it never imports, reviews, or publishes any knowledge. Runtime source
IDs must come from a successful authorized upload and are still validated by
the ordinary authoritative-import API against its current document state.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from graphrag_prod.api.knowledge_contracts import AuthoritativeImportRequest


INDUSTRIAL_DEMO_DIRECTORY = Path(__file__).parent / "static" / "industrial-demo-v1"


def _read_json(filename: str) -> dict[str, Any]:
    return json.loads((INDUSTRIAL_DEMO_DIRECTORY / filename).read_text(encoding="utf-8"))


def get_industrial_demo_kit() -> dict[str, Any]:
    """Return independent JSON-ready metadata, source texts, and editable drafts."""

    kit = _read_json("manifest.json")
    kit["ontology"] = _read_json("ontology.json")
    kit["authoritative_import_template"] = _read_json(
        "authoritative_instances.template.json"
    )
    for file in kit["files"]:
        payload = (INDUSTRIAL_DEMO_DIRECTORY / file["filename"]).read_bytes()
        if hashlib.sha256(payload).hexdigest() != file["sha256"]:
            raise ValueError("industrial demo source checksum mismatch")
        file["text"] = payload.decode("utf-8")
        if len(file["text"]) != file["characters"]:
            raise ValueError("industrial demo source character count mismatch")
    return kit


def build_authoritative_import(
    *,
    tbox_id: str,
    document_id: str,
    version_id: str,
    source_bytes: bytes,
    chunks: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Bind exact source spans to real IDs without estimating chunk positions.

    ``chunks`` must contain the one uploaded Chunk, including ``chunk_id``,
    document-relative ``char_start``/``char_end``, and its exact ``text``. The
    kit intentionally fits within the default 1,200-character chunk budget.
    Changed files, missing ranges, or a different splitter layout are refused.
    This pure helper cannot establish permissions or active-version status;
    the ordinary import API enforces those when the human submits the draft.
    """

    kit = get_industrial_demo_kit()
    source = next(file for file in kit["files"] if file["id"] == "authoritative_source")
    if not isinstance(source_bytes, bytes):
        raise TypeError("source_bytes must be bytes")
    if hashlib.sha256(source_bytes).hexdigest() != source["sha256"]:
        raise ValueError("uploaded authoritative source checksum mismatch")
    if len(chunks) != 1 or not isinstance(chunks[0], Mapping):
        raise ValueError("authoritative source requires exactly one complete Chunk")
    chunk = chunks[0]
    if (
        type(chunk.get("char_start")) is not int
        or type(chunk.get("char_end")) is not int
        or chunk["char_start"] != 0
        or chunk["char_end"] != len(source["text"])
        or chunk.get("text") != source["text"]
    ):
        raise ValueError("uploaded Chunk must exactly cover the authoritative source")

    bindings = {
        "ontology_version_id": tbox_id,
        "document_id": document_id,
        "version_id": version_id,
        "chunk_id": chunk.get("chunk_id"),
    }
    for name, value in bindings.items():
        if (
            not isinstance(value, str)
            or not value.strip()
            or value != value.strip()
            or value in kit["placeholders"].values()
        ):
            raise ValueError(f"{name} must be a real runtime identifier")

    payload = kit["authoritative_import_template"]
    payload["ontology_version_id"] = tbox_id
    for item in payload["mentions"] + payload["assertions"]:
        evidence = item["evidence"]
        start, end = evidence["char_start"], evidence["char_end"]
        if not 0 <= start < end <= len(source["text"]):
            raise ValueError("expert evidence range is outside the authoritative source")
        if source["text"][start:end] != evidence["quoted_text"]:
            raise ValueError("expert evidence does not match the authoritative source")
        for name in ("document_id", "version_id", "chunk_id"):
            evidence[name] = bindings[name]
    # Validate exactly the same transport schema as the real import endpoint.
    AuthoritativeImportRequest.model_validate(payload)
    return payload
