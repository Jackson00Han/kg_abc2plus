"""Execute reviewed Markdown table mappings through strict candidate validation.

This adapter never calls a provider. A table row is the contiguous fact proof;
the separately retained preflight binds definition and context sections to the
same immutable document. Missing external definitions remain explicit gaps.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from typing import Any

from .extraction import (
    ExtractionFinding, ExtractionRejected, OpenAICompatibleOntologyExtractor,
)
from .markdown_mapping import preview_markdown_mapping
from graphrag_prod.knowledge.trust import KnowledgeOrigin

VERSION = "markdown-reviewed-mapping-extraction:v1"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _reject(code: str, path: str, detail: str) -> None:
    raise ExtractionRejected((ExtractionFinding(code, "REJECT", path, detail),))


class _MarkdownValidator(OpenAICompatibleOntologyExtractor):
    def _entity_identity(self, candidate, ordered_mentions, chunk):
        entity = self.mapped_entities[candidate.reference]
        # The reviewed source identity is scoped to this tenant/document and
        # ontology. Names and short references never merge different sources.
        key = self.provisional_namespace + ":" + _digest({
            "version": VERSION, "tenant": chunk.tenant_id,
            "document": chunk.document_id, "ontology": self.active_tbox.tbox_id,
            "mapping_id": self.mapping_id, "identity": entity["identity"],
            "type": candidate.entity_type,
        })
        names = [p["value"] for p in entity["properties"]
                 if p["property"] == "display_name"]
        # Display names are copied from the definition table even when the
        # current row contains only an explicit source reference to that table.
        name = names[0] if names else entity["section_id"]
        return key, name, ()


class MarkdownMappingExecutor:
    """Produce bounded source-row payloads, preserving unresolved references."""

    def __init__(self, parsed: Any, tbox: Any, mapping: dict):
        if parsed.mime_type != "text/markdown":
            _reject("MAPPING_MIME_INVALID", "$", "Markdown mapping requires text/markdown")
        self.parsed, self.tbox = parsed, tbox
        # Freeze caller-owned configuration before its checksum enters identity.
        self.mapping = json.loads(_canonical(mapping))
        self.report = preview_markdown_mapping(parsed.normalized_text, self.mapping, tbox=tbox)
        if not self.report["valid"]:
            _reject("MARKDOWN_MAPPING_INVALID", "$", _canonical(self.report["diagnostics"])[:4096])
        readiness = self.report["ontology_readiness"]
        if not readiness["checked"] or readiness["findings"]:
            _reject("MARKDOWN_ONTOLOGY_NOT_READY", "$", _canonical(readiness["findings"])[:4096])
        if self.report["source_checksum"] != parsed.normalized_checksum:
            _reject("MAPPING_SOURCE_MISMATCH", "$", "mapping source checksum differs from parsed source")
        self.entities = {item["identity"]: item for item in self.report["entities"]}
        self.plan_checksum = self.report["mapping_checksum"]

    def _local_span(self, span: dict, chunk: Any) -> dict:
        start, end = span["char_start"], span["char_end"]
        if not chunk.char_start <= start < end <= chunk.char_end:
            _reject("MARKDOWN_TABLE_ROW_SPLIT", "$", "mapped table row crosses retained chunk boundaries")
        left, right = start - chunk.char_start, end - chunk.char_start
        if chunk.text[left:right] != span["text"]:
            _reject("MAPPING_SOURCE_MISMATCH", "$", "mapped span differs from retained chunk")
        return {"text": span["text"], "start": left, "end": right}

    def payload(self, chunk: Any) -> tuple[dict, dict, list[dict]]:
        if (chunk.tenant_id != self.tbox.tenant_id
                or self.parsed.normalized_text[chunk.char_start:chunk.char_end] != chunk.text):
            _reject("MAPPING_SOURCE_MISMATCH", "$", "chunk is outside the authorized mapped source")
        result = {"entities": [], "relationships": [], "property_facts": []}
        references, definitions, omitted = {}, {}, []

        def include(item: dict) -> bool:
            span = item["evidence"]
            if span["char_end"] <= chunk.char_start or span["char_start"] >= chunk.char_end:
                return False
            self._local_span(span, chunk)
            return True

        def entity(identity: str, span: dict) -> str:
            definition = self.entities[identity]
            if identity not in references:
                ref = "e" + str(len(references))
                references[identity] = ref
                definitions[ref] = definition
                result["entities"].append({"ref": ref, "type": definition["entity_type"], "mentions": []})
            ref = references[identity]
            mention = {**self._local_span(span, chunk), "confidence": 1.0}
            item = result["entities"][int(ref[1:])]
            if mention not in item["mentions"]:
                item["mentions"].append(mention)
            return ref

        def property_value(value: dict, row: dict) -> dict:
            # Values remain their exact original cell spelling. The shared
            # validator enforces literal, enum, unit and cardinality rules.
            return {"property": value["property"], "raw_literal": value["value"],
                    "unit": None, "valid_from": None, "valid_to": None,
                    "observed_at": None, "evidence": self._local_span(row, chunk),
                    "confidence": 1.0}

        for item in self.report["entities"]:
            if not include(item):
                continue
            names = [p for p in item["properties"] if p["property"] == "display_name"]
            if not names:
                # No universal guess at a subject name from arbitrary columns.
                _reject("MARKDOWN_IDENTITY_MENTION_MISSING", item["section_id"],
                        "mapped entity needs an explicit display_name cell for its source mention")
            own = entity(item["identity"], names[0]["evidence"])
            for value in item["properties"]:
                result["property_facts"].append({"entity_ref": own, **property_value(value, item["evidence"])})
        for item in self.report["relationships"]:
            if not include(item):
                continue
            if item["status"] != "RESOLVED":
                omitted.append({"section_id": item["section_id"],
                                "reason": "external_definition_not_supplied",
                                "source_reference": item["source_reference"],
                                "target_reference": item["target_reference"]})
                continue
            source = entity(item["source_identity"], item["source_evidence"])
            target = entity(item["target_identity"], item["target_evidence"])
            result["relationships"].append({
                "type": item["relationship_type"], "source_ref": source, "target_ref": target,
                "evidence": self._local_span(item["evidence"], chunk), "confidence": 1.0,
                "properties": [property_value(p, item["evidence"]) for p in item["properties"]],
            })
        return result, definitions, omitted


class MarkdownMappingExtractor:
    """Reviewed mapping adapter; validation yields candidates, never approval."""

    max_validation_attempts = 1
    deterministic_batches = True
    max_document_model_calls = 0

    def __init__(self, base: OpenAICompatibleOntologyExtractor, parsed: Any, mapping: dict):
        self.base, self.parsed = base, parsed
        self.active_tbox, self.limits = base.active_tbox, base.limits
        self.prompt_version = base.prompt_version
        self.model = "deterministic-reviewed-markdown-mapping"
        self.executor = MarkdownMappingExecutor(parsed, self.active_tbox, mapping)
        self.plan_checksum = self.executor.plan_checksum

    @property
    def request_policy_signature(self) -> str:
        return _digest({"version": VERSION, "source": self.parsed.normalized_checksum,
                        "mapping": self.plan_checksum, "ontology": self.active_tbox.checksum})

    @property
    def semantic_context(self) -> dict:
        return json.loads(_canonical(self.executor.report["semantic_context"]))

    def prepare_document(self, *, read: Any, persist: Any, before_model_call: Any) -> None:
        # The workflow retains full mapping/source context in its existing
        # document preflight. Keep a compact, idempotent execution provenance.
        signature = self.request_policy_signature
        saved = read(signature)
        expected = {"version": VERSION, "signature": signature, "status": "VALIDATED_MAPPING",
                    "source_checksum": self.parsed.normalized_checksum,
                    "ontology_checksum": self.active_tbox.checksum,
                    "mapping_checksum": self.plan_checksum, "model_calls": 0,
                    "semantic_context": self.executor.report["semantic_context"],
                    "mapping_profile": self.executor.mapping,
                    "summary": self.summary()}
        if saved is None:
            persist(signature, expected)
        elif saved != expected:
            _reject("MAPPING_AUDIT_INVALID", "$", "saved Markdown mapping scope differs")

    def extract_audited(self, *, artifact_id: str, input_hash: str, chunk: Any, profile: Any):
        payload, definitions, omitted = self.executor.payload(chunk)
        validator = _MarkdownValidator(client=self.base.client, model=self.model,
            active_tbox=self.active_tbox, prompt_version=self.prompt_version, limits=self.limits)
        validator.mapped_entities = definitions
        validator.mapping_id = self.executor.mapping["mapping_id"]
        result = validator.revalidate_saved_response(_canonical(payload), chunk=chunk, profile=profile)
        findings = [ExtractionFinding("MARKDOWN_MAPPING_APPLIED", "INFO", "$",
                    "mapping=" + self.plan_checksum + "; provider_calls=0")]
        context = self.executor.report["semantic_context"]
        if context:
            findings.append(ExtractionFinding("MAPPING_SEMANTIC_CONTEXT", "INFO", "$", _canonical(context)))
        for item in omitted:
            findings.append(ExtractionFinding("MARKDOWN_EXTERNAL_REFERENCE_UNRESOLVED", "INFO",
                item["section_id"], _canonical(item)))
        return replace(result, origin=KnowledgeOrigin.MAPPED,
                       findings=result.findings + tuple(findings))

    def summary(self) -> dict:
        report = self.executor.report
        return {"mapping_checksum": self.plan_checksum, "version": VERSION,
                "record_count": len(report["entities"]),
                "relationship_count": sum(r["status"] == "RESOLVED" for r in report["relationships"]),
                "unresolved_relationship_count": sum(r["status"] != "RESOLVED" for r in report["relationships"]),
                "unresolved_references": report["unresolved_references"],
                "semantic_context": report["semantic_context"],
                "coverage": report["coverage"], "complete": report["complete"],
                "provider_calls": 0}
