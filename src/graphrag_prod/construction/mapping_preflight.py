"""Bounded, provider-free mapping previews; these are not governed A-Box records.

Adapters consume the unchanged normalized document and a caller-supplied mapping.
The mapping is data, never Python, XPath evaluation, a URL or a filesystem path.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from .parser import ParsedDocument
from graphrag_prod.ontology.models import TBoxVersion
from graphrag_prod.domain.models import Chunk

MAX_MAPPING_BYTES = 262_144
MAX_REPORT_BYTES = 4_194_304
VERSION = "document-mapping-preflight:v1"


def bounded_json(value: Any, maximum: int) -> str:
    """Reject oversized/deep/non-JSON configuration before adapter traversal."""
    stack = [(value, 0)]
    count = 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if depth > 32 or count > 100_000:
            raise ValueError("mapping JSON exceeds structural budget")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("mapping keys must be strings")
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend((child, depth + 1) for child in item)
        elif item is not None and not isinstance(item, (str, int, float, bool)):
            raise ValueError("mapping must contain JSON values")
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > maximum:
        raise ValueError("mapping JSON exceeds byte budget")
    return encoded


def validate_mapping_profile(value: Any) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("version"), str):
        raise ValueError("mapping_profile requires an explicit version")
    bounded_json(value, MAX_MAPPING_BYTES)
    return value


def preview_mapping(parsed: ParsedDocument, mapping: dict, tbox: TBoxVersion) -> dict:
    validate_mapping_profile(mapping)
    # Explicit adapter dispatch, independent of filenames and domain vocabularies.
    if parsed.mime_type in {"application/xml", "text/xml"}:
        from .xml_mapping import preflight_xml_mapping
        report = preflight_xml_mapping(parsed.normalized_text, mapping=mapping, tbox=tbox,
                                       source_checksum=parsed.normalized_checksum)
    elif parsed.mime_type == "text/markdown":
        from .markdown_mapping import preview_markdown_mapping
        report = preview_markdown_mapping(parsed.normalized_text, mapping, tbox=tbox)
    else:
        raise ValueError("this MIME type has no declarative mapping preview adapter")
    report.update({"preflight_contract": VERSION,
                   "source_checksum": parsed.normalized_checksum,
                   "original_checksum": parsed.original_checksum,
                   "mapping_checksum": hashlib.sha256(bounded_json(mapping, MAX_MAPPING_BYTES).encode()).hexdigest(),
                   "ontology_checksum": tbox.checksum, "tbox_id": tbox.tbox_id,
                   "parser_version": parsed.parser_version or parsed.splitter_signature,
                   "mime_type": parsed.mime_type,
                   "governance_disposition": "PREVIEW_ONLY",
                   "persisted_graph_records": 0})
    bounded_json(report, MAX_REPORT_BYTES)
    return report


def preview_summary(report: dict) -> dict:
    """Small job projection; full evidence/configuration remains in the audit."""
    result = {key: report[key] for key in (
        "preflight_contract", "version", "valid", "complete", "source_checksum",
        "original_checksum", "mapping_checksum", "ontology_checksum", "tbox_id",
        "parser_version", "mime_type", "governance_disposition", "persisted_graph_records",
        "semantic_context", "ontology_ready", "ontology_readiness",
    ) if key in report}
    result.update(entity_count=len(report.get("entities", [])),
                  relationship_count=len(report.get("relationships", [])),
                  unresolved_reference_count=len(report.get("unresolved_references", [])),
                  diagnostic_count=len(report.get("diagnostics", [])))
    result["unresolved_relationship_count"] = sum(
        item.get("status") == "UNRESOLVED" for item in report.get("relationships", []))
    result["diagnostics"] = report.get("diagnostics", [])[:100]
    result["diagnostics_truncated"] = result["diagnostic_count"] > 100
    return result


def bind_preview_evidence(report: dict, chunks: tuple[Chunk, ...]) -> dict:
    """Resolve every located span to immutable source chunks, without A-Box writes.

    A structural proof can have several anchors; each is checked independently.
    No generated text, synthetic endpoint mention or broadened LLM span is used.
    """
    result = deepcopy(report)
    if not chunks or any(not isinstance(chunk, Chunk) for chunk in chunks):
        raise ValueError("mapping evidence requires immutable source chunks")
    ordered = sorted(chunks, key=lambda item: item.char_start)
    scope = (ordered[0].tenant_id, ordered[0].document_id, ordered[0].version_id,
             ordered[0].access_policy_id, ordered[0].access_policy_version, ordered[0].access_groups)
    cursor, ids = 0, set()
    for chunk in ordered:
        if (chunk.char_start != cursor or chunk.chunk_id in ids
                or (chunk.tenant_id, chunk.document_id, chunk.version_id,
                    chunk.access_policy_id, chunk.access_policy_version, chunk.access_groups) != scope
                or hashlib.sha256(chunk.text.encode()).hexdigest() != chunk.checksum):
            raise ValueError("mapping chunks must form one complete authorized document version")
        cursor = chunk.char_end
        ids.add(chunk.chunk_id)
    text = "".join(chunk.text for chunk in ordered)
    if hashlib.sha256(text.encode()).hexdigest() != report.get("source_checksum"):
        raise ValueError("mapping report source checksum does not match retained chunks")
    stack = [result]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            stack.extend(list(value.values()))
            if {"char_start", "char_end", "text"} <= value.keys():
                start, end = value["char_start"], value["char_end"]
                if (type(start) is not int or type(end) is not int
                        or not 0 <= start <= end <= len(text) or value["text"] != text[start:end]):
                    raise ValueError("mapping evidence does not reproduce the source span")
                references = []
                for chunk in chunks:
                    left, right = max(start, chunk.char_start), min(end, chunk.char_end)
                    if left < right:
                        references.append({"chunk_id": chunk.chunk_id,
                            "document_id": chunk.document_id, "version_id": chunk.version_id,
                            "chunk_checksum": chunk.checksum,
                            "char_start": left, "char_end": right,
                            "chunk_char_start": left - chunk.char_start,
                            "chunk_char_end": right - chunk.char_start})
                value["source_references"] = references
                if sum(ref["char_end"] - ref["char_start"] for ref in references) != end - start:
                    raise ValueError("mapping evidence crosses missing source chunks")
        elif isinstance(value, list):
            stack.extend(value)
    bounded_json(result, MAX_REPORT_BYTES)
    return result
