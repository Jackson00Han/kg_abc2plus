"""Verify authorized topology without writing knowledge; optionally call the LLM."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-model", action="store_true", help="call the configured model; no database writes")
    args = parser.parse_args()
    from graphrag_prod.construction.structured import StructuredDocumentParser, StructuredMappingExtractor
    from graphrag_prod.api.knowledge_contracts import ConstructionChunkResponse
    from tests.unit.test_structured_mapping import base, chunk_for, topology_plan
    from tests.unit.test_construction_extraction import _profile
    from tests.unit.test_ontology_source import tbox

    output = ROOT / ".local/structured-mapping"
    output.mkdir(parents=True, exist_ok=True)
    parsed = StructuredDocumentParser().parse((ROOT / "busway_files/topology.source.json").read_bytes(), mime_type="application/json")
    if args.live_model:
        from dotenv import load_dotenv
        from openai import OpenAI
        from scripts.run_playground import _build_playground_extractor
        load_dotenv(ROOT / ".env")
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], base_url=os.environ["OPENAI_BASE_URL"], max_retries=0)
        model = _build_playground_extractor(client, os.environ["MODEL_NAME"], tbox())
    else:
        model, _ = base(topology_plan(), tbox())
    extractor = StructuredMappingExtractor(model, parsed)
    calls, audits = [], []

    def persist(signature, payload):
        name = f"verification-mapping-{signature}.json"
        (output / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        audits.append(name)

    start = time.monotonic()
    extractor.prepare_document(read=lambda _: None, persist=persist,
                               before_model_call=lambda: calls.append(time.monotonic()))
    mapping_seconds = time.monotonic() - start
    execution_start = time.monotonic()
    chunks, entities, properties, relations = [], set(), 0, 0
    for seed in parsed.chunks:
        audited = extractor.extract_audited(artifact_id="qa-fixture", input_hash="qa-fixture",
                                            chunk=chunk_for(seed), profile=_profile())
        result = audited.output
        entities.update(e.entity_id for e in result.entities)
        properties += sum(a.literal_semantics is not None for a in result.assertions)
        relations += sum(a.object_entity_id is not None for a in result.assertions)
        chunks.append(ConstructionChunkResponse.model_validate({
            "chunk_id": f"qa-{seed.ordinal}", "artifact_id": "qa-fixture",
            "status": audited.status.value if result.entities else "EMPTY",
            "finding_codes": [f.code for f in audited.findings], "replayed": False,
            "mention_record_ids": [m.mention_id for m in result.mentions],
            "assertion_record_ids": [a.assertion_id for a in result.assertions],
            "mapping_summary": extractor.summary() if seed.ordinal == 0 else None,
        }).model_dump(mode="json"))
    (output / "ui-receipt.json").write_text(json.dumps({"chunks": chunks, "status": "COMPLETED",
        "completed_chunks": len(chunks), "expected_chunks": len(chunks), "extraction_mode": "LLM"}, ensure_ascii=False))
    report = {"live_model": args.live_model, "model_calls": len(calls) if args.live_model else 0,
              "mapping_attempts": len(calls), "mapping_seconds": round(mapping_seconds, 3),
              "execution_seconds": round(time.monotonic()-execution_start, 3),
              "records": extractor.summary()["record_count"], "entities": len(entities),
              "properties": properties, "relations": relations, "chunks": len(chunks),
              "mapping_audits": audits, "knowledge_writes": 0}
    (output / ("verification-live.json" if args.live_model else "verification-offline.json")).write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == "__main__":
    main()
