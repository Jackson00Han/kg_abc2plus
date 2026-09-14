"""Offline feasibility only: inspect two authorized, checksum-verified audits.

Does not call providers, APIs or Neo4j, and never changes stored extraction output.
Run from the repository root with .venv/bin/python.
"""
from __future__ import annotations

import copy
from dataclasses import fields
import json
from pathlib import Path
import re

from graphrag_prod.api.knowledge_contracts import OntologyImportRequest
from graphrag_prod.construction import OpenAICompatibleOntologyExtractor
from graphrag_prod.domain.models import Chunk
from graphrag_prod.ontology.models import TBoxVersion

ROOT = Path(__file__).resolve().parents[2]
AUDIT_PATH = ROOT / ".local/ontology-source/topology-two-failed-audit.json"
OUTPUT_PATH = ROOT / ".local/ontology-source/topology-span-feasibility.json"
CHUNK_IDS = {
    "62ba409c-636d-518c-a4fb-510a14ce5852",
    "149b7c0d-23ce-5c2f-acc2-6a0ab7ac323a",
}


def spans(text: str) -> dict:
    decoder = json.JSONDecoder()
    strings, containers, field_spans = [], [], []
    for match in re.finditer(r'"(?:[^"\\]|\\.)*"', text):
        strings.append({
            "start": match.start() + 1, "end": match.end() - 1,
            "raw_text": text[match.start() + 1:match.end() - 1],
            "quoted_start": match.start(), "quoted_end": match.end(),
        })
        remaining = re.match(r"\s*:\s*", text[match.end():])
        if remaining is None:
            continue
        value_start = match.end() + remaining.end()
        try:
            _, value_end = decoder.raw_decode(text, value_start)
        except ValueError:
            continue
        field_spans.append({
            "key": json.loads(match.group()),
            "start": match.start(), "end": value_end,
            "raw_text": text[match.start():value_end],
            "value_start": value_start, "value_end": value_end,
            "raw_value": text[value_start:value_end],
        })
    for start, char in enumerate(text):
        if char not in "{[" or any(
            value["quoted_start"] <= start < value["quoted_end"] for value in strings
        ):
            continue
        try:
            value, end = decoder.raw_decode(text, start)
        except ValueError:
            continue
        containers.append({
            "start": start, "end": end,
            "kind": "object" if isinstance(value, dict) else "array",
            "raw_text": text[start:end],
        })
    for value in strings + containers + field_spans:
        assert text[value["start"]:value["end"]] == value["raw_text"]
    for value in field_spans:
        assert text[value["value_start"]:value["value_end"]] == value["raw_value"]
    return {"json_string_spans": strings, "json_container_spans": containers,
            "json_field_spans": field_spans}


def main() -> None:
    audits = json.loads(AUDIT_PATH.read_text())
    assert len(audits) == 2 and {x["chunk"]["chunk_id"] for x in audits} == CHUNK_IDS
    source = json.loads((ROOT / "busway_files/ontology.source.json").read_text())
    source["rule_reference_registry"] = json.loads(
        (ROOT / "busway_files/rule.references.json").read_text()
    )
    request = OntologyImportRequest.model_validate(source)
    definition = request.model_dump(
        exclude={"activate", "expected_checksum", "expected_active_tbox_id"},
        exclude_none=True,
    )
    results = []
    for audit in audits:
        stored = audit["chunk"]
        chunk = Chunk(**{
            field.name: frozenset(stored[field.name]) if field.name == "access_groups"
            else stored.get(field.name) for field in fields(Chunk)
        })
        tbox = TBoxVersion.from_mapping({
            **definition, "tenant_id": chunk.tenant_id, "status": "PUBLISHED",
        })
        extractor = OpenAICompatibleOntologyExtractor(
            client=object(), model="offline-no-provider", active_tbox=tbox,
            prompt_version="offline-span-feasibility-only",
        )
        hints = spans(chunk.text)
        record = next(x for x in hints["json_container_spans"] if x["start"] == 0)
        original = json.loads(audit["attempts"][-1]["response"])
        before = extractor._validate_payload(original, chunk)[3]
        synthetic = copy.deepcopy(original)
        for fact in synthetic["property_facts"]:
            fact["evidence"] = {
                "start": record["start"], "end": record["end"],
                "text": record["raw_text"],
            }
            try:
                value = json.loads(fact["raw_literal"])
            except (ValueError, TypeError):
                continue
            if isinstance(value, (dict, list)):
                # Only restore original JSON whitespace; equality preserves values.
                exact = next(x for x in hints["json_container_spans"]
                             if json.loads(x["raw_text"]) == value)
                fact["raw_literal"] = exact["raw_text"]
        after = extractor._validate_payload(synthetic, chunk)[3]
        assert not after
        results.append({
            "chunk_id": chunk.chunk_id, "ordinal": chunk.ordinal,
            "original_response_checksum": audit["attempts"][-1]["response_checksum"],
            "model_calls": 0,
            "method": "offline range feasibility; synthetic edits are not measured model output",
            "before_findings": [x.code for x in before],
            "after_findings": [x.code for x in after],
            **hints, "synthetic_span_corrected_response": synthetic,
        })
        print(f"ordinal={chunk.ordinal}: {len(before)} -> {len(after)} findings; "
              f"strings={len(hints['json_string_spans'])}; "
              f"fields={len(hints['json_field_spans'])}; "
              f"record=[{record['start']},{record['end']}); model_calls=0")
    OUTPUT_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
