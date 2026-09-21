"""Provider-free XML mapping execution through strict candidate governance.

An explicitly supplied, versioned mapping establishes source structure and
finite transformations. This adapter never infers connections or imports a
prebuilt graph. The retained complete XML is the bounded evidence batch, so
ancestor and reference endpoints remain exact source mentions.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json

from graphrag_prod.graph.governance import normalize_display_name
from graphrag_prod.knowledge.trust import KnowledgeOrigin

from .extraction import ExtractionFinding, ExtractionRejected, OpenAICompatibleOntologyExtractor
from .xml_mapping import preflight_xml_mapping, _NUMBER_UNIT

VERSION = "xml-mapping-extraction:v1"
MAX_SOURCE_CHARS = 65_536
MAX_CANDIDATE_ENTITIES = 500
MAX_CANDIDATE_RELATIONSHIPS = 1_000
MAX_CANDIDATE_PROPERTIES = 1_000
MAX_CANDIDATE_RECORDS = 1_000
MAX_CANDIDATE_CHARS = 2_000_000


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _reject(code, detail, path="$"):
    raise ExtractionRejected((ExtractionFinding(code, "REJECT", path, detail),))


class _XmlMappingValidator(OpenAICompatibleOntologyExtractor):
    def _entity_identity(self, candidate, ordered_mentions, chunk):
        item = self.source_entities[candidate.reference]
        key = self.provisional_namespace + ":" + _digest({
            "version": VERSION, "document": chunk.document_id,
            "ontology": self.active_tbox.tbox_id, "mapping_id": self.mapping_id,
            "collection": item["collection"], "source_identity": item["source_identity"],
            "type": candidate.entity_type,
        })
        labels = [p["value"] for p in item["properties"]
                  if p["property"] == "display_name" and isinstance(p["value"], str)]
        name = labels[0] if labels else item["identity_parts"][-1]
        return key, normalize_display_name(name), ()


class XmlMappingExtractor:
    """One deterministic document plan and one complete source evidence batch."""

    max_validation_attempts = 1
    max_document_model_calls = 0
    deterministic_batches = True
    complete_document_evidence = True
    max_source_chars = MAX_SOURCE_CHARS

    def __init__(self, base, parsed, mapping_profile):
        if not isinstance(base, OpenAICompatibleOntologyExtractor):
            raise TypeError("XML mapping requires the active ontology extractor")
        if parsed.mime_type not in {"application/xml", "text/xml"}:
            raise ValueError("XML mapping requires an XML document")
        if len(parsed.normalized_text) > MAX_SOURCE_CHARS:
            _reject("XML_MAPPING_SOURCE_LIMIT", "mapped XML exceeds the bounded complete-document evidence window")
        self.base, self.parsed = base, parsed
        self.active_tbox, self.prompt_version = base.active_tbox, base.prompt_version
        self.model = "deterministic-xml-mapping"
        self.mapping = deepcopy(mapping_profile)
        self.plan_checksum = _digest(self.mapping)
        self.report = None
        self.deferred_properties = []
        # These are deterministic record/output budgets, not provider budgets.
        self.limits = replace(base.limits, max_entities=MAX_CANDIDATE_ENTITIES,
                              max_relationships=MAX_CANDIDATE_RELATIONSHIPS,
                              max_property_facts=MAX_CANDIDATE_PROPERTIES,
                              max_response_chars=MAX_CANDIDATE_CHARS)

    @property
    def request_policy_signature(self):
        return _digest({"version": VERSION, "source": self.parsed.normalized_checksum,
                        "ontology": self.active_tbox.checksum, "mapping": self.plan_checksum,
                        "max_source_chars": MAX_SOURCE_CHARS,
                        "max_candidate_records": MAX_CANDIDATE_RECORDS,
                        "max_candidate_chars": MAX_CANDIDATE_CHARS,
                        "enum_policy": "retain-until-persisted-proof:v1"})

    def prepare_document(self, *, read, persist, before_model_call):
        report = preflight_xml_mapping(self.parsed.normalized_text, mapping=self.mapping,
                                       tbox=self.active_tbox,
                                       source_checksum=self.parsed.original_checksum)
        errors = [d for d in report.get("diagnostics", []) if d["severity"] == "error"]
        if not report.get("valid") or errors:
            findings = tuple(ExtractionFinding(d["code"], "REJECT", d["path"], d["message"])
                             for d in errors)
            raise ExtractionRejected(findings or (ExtractionFinding(
                "XML_MAPPING_INVALID", "REJECT", "$", "XML mapping is invalid"),))
        if any(item.get("code") != "XML_SEMANTIC_BINDING_DEFERRED"
               for item in report.get("unresolved_references", [])):
            _reject("XML_MAPPING_UNRESOLVED", "explicit XML references must resolve before candidate construction")
        self.deferred_properties = []
        definitions = {e.name: {p.name: p for p in e.properties}
                       for e in self.active_tbox.entity_types}
        direct_properties = 0
        for item in report["entities"]:
            for prop in item["properties"]:
                if prop["transform"] != "enum":
                    direct_properties += 1
                    continue
                if definitions[item["entity_type"]][prop["property"]].cardinality.required:
                    _reject("XML_REQUIRED_TRANSFORM_UNSUPPORTED",
                            "required enum property needs persisted mapping proof support", prop["evidence"]["path"])
                self.deferred_properties.append({"source_identity": item["source_identity"],
                    "property": prop["property"], "reason": "XML_ENUM_LITERAL_PROOF_REQUIRED",
                    "mapping_rule_index": prop["mapping_rule_index"]})
        if len(report["entities"]) + len(report["relationships"]) + direct_properties > MAX_CANDIDATE_RECORDS:
            _reject("XML_MAPPING_RECORD_LIMIT", "mapped XML exceeds the bounded candidate write size")
        signature = self.request_policy_signature
        audit = {"version": VERSION, "signature": signature, "status": "VALIDATED_MAPPING",
                 "source_checksum": self.parsed.normalized_checksum,
                 "ontology_checksum": self.active_tbox.checksum,
                 "mapping_checksum": self.plan_checksum, "mapping": self.mapping,
                 "report_checksum": _digest(report), "model_calls": 0,
                 "semantic_context": report["semantic_context"],
                 "diagnostics": report["diagnostics"],
                 "deferred_properties": self.deferred_properties,
                 "coverage": {k: v for k, v in report["coverage"].items() if k != "fields"}}
        existing = read(signature)
        if existing is not None and existing != audit:
            _reject("XML_MAPPING_AUDIT_CONFLICT", "saved mapping audit does not match its immutable source and ontology")
        if existing is None:
            persist(signature, audit)
        self.report = report

    def _payload(self, chunk):
        if self.report is None:
            _reject("XML_MAPPING_NOT_PREPARED", "mapping must be prepared in the authorized source scope")
        if (chunk.tenant_id != self.active_tbox.tenant_id or chunk.char_start != 0
                or chunk.char_end != len(self.parsed.normalized_text)
                or chunk.text != self.parsed.normalized_text
                or chunk.checksum != self.parsed.normalized_checksum):
            _reject("XML_MAPPING_EVIDENCE_WINDOW", "mapped XML requires one complete, unchanged, authorized source evidence batch")
        result = {"entities": [], "relationships": [], "property_facts": []}
        refs, source_entities, mentions = {}, {}, {}

        def span(*anchors):
            start = min(a["char_start"] for a in anchors)
            end = max(a["char_end"] for a in anchors)
            if not 0 <= start < end <= len(chunk.text):
                _reject("XML_MAPPING_EVIDENCE_INVALID", "mapping anchor is outside immutable source")
            return {"text": chunk.text[start:end], "start": start, "end": end}

        for item in self.report["entities"]:
            ref = "e" + str(len(refs))
            refs[item["source_identity"]] = ref
            source_entities[ref] = item
            # The last identity field belongs to the record itself; inherited
            # scope anchors are provenance, never synthetic local mentions.
            mention = item["identity_anchors"][-1]
            if not mention["path"].startswith(item["source_path"] + "/"):
                _reject("XML_MAPPING_IDENTITY_ANCHOR", "record must have its own source identity token")
            mentions[ref] = mention
            result["entities"].append({"ref": ref, "type": item["entity_type"],
                                       "mentions": [{"text": mention["text"],
                                                     "start": mention["char_start"],
                                                     "end": mention["char_end"], "confidence": 1.0}]})
            for prop in item["properties"]:
                raw, unit = prop["evidence"]["text"], None
                if prop["transform"] == "enum":
                    # Enum translation is a semantic mapping, not XML escaping.
                    # Until the persisted literal contract carries that proof,
                    # retain the rule/result in the audit instead of pretending
                    # its canonical word occurs in the engineering source.
                    continue
                if raw != prop["raw_value"]:
                    _reject("XML_LITERAL_ENCODING_UNSUPPORTED", "encoded XML scalar requires an explicit persisted decoding contract", prop["evidence"]["path"])
                if prop["transform"] == "quantity":
                    matched = _NUMBER_UNIT.fullmatch(raw)
                    if matched is None:
                        _reject("XML_QUANTITY_INVALID", "quantity does not contain its explicit number and unit")
                    raw, unit = matched.groups()
                result["property_facts"].append({"entity_ref": ref, "property": prop["property"],
                    "raw_literal": raw, "unit": unit, "valid_from": None, "valid_to": None,
                    "observed_at": None, "evidence": span(mention, prop["evidence"]), "confidence": 1.0})
        for item in self.report["relationships"]:
            source, target = refs[item["source"]], refs[item["target"]]
            result["relationships"].append({"type": item["type"], "source_ref": source,
                "target_ref": target, "evidence": span(mentions[source], mentions[target], item["evidence"]),
                "confidence": 1.0, "properties": []})
        return result, source_entities

    def extract_audited(self, *, artifact_id, input_hash, chunk, profile):
        payload, source_entities = self._payload(chunk)
        validator = _XmlMappingValidator(client=self.base.client, model=self.model,
            active_tbox=self.active_tbox, prompt_version=self.prompt_version, limits=self.limits)
        validator.source_entities = source_entities
        validator.mapping_id = self.mapping["mapping_id"]
        encoded = _canonical(payload)
        if len(encoded) > MAX_CANDIDATE_CHARS:
            _reject("XML_MAPPING_OUTPUT_LIMIT", "mapped candidate evidence exceeds its deterministic output budget")
        audited = validator.revalidate_saved_response(encoded, chunk=chunk, profile=profile)
        findings = [ExtractionFinding("XML_MAPPING_APPLIED", "INFO", "$", "mapping=" + self.plan_checksum)]
        findings.extend(ExtractionFinding(d["code"], "INFO", d["path"], d["message"])
                        for d in self.report["diagnostics"])
        if self.deferred_properties:
            findings.append(ExtractionFinding("XML_ENUM_LITERAL_PROOF_REQUIRED", "INFO", "$",
                f"{len(self.deferred_properties)} enum-transformed source properties retained in mapping audit pending persisted proof support"))
        return replace(audited, origin=KnowledgeOrigin.MAPPED,
                       findings=audited.findings + tuple(findings))

    def summary(self):
        if self.report is None:
            return None
        return {"mapping_checksum": self.plan_checksum,
                "record_count": len(self.report["entities"]),
                "relationship_count": len(self.report["relationships"]),
                "property_count": sum(len(e["properties"]) for e in self.report["entities"]),
                "deferred_property_count": len(self.deferred_properties),
                "deferred_properties": self.deferred_properties,
                "unresolved_references": self.report["unresolved_references"],
                "semantic_context": self.report["semantic_context"],
                "coverage": {k: v for k, v in self.report["coverage"].items() if k != "fields"},
                "model_calls": 0}
