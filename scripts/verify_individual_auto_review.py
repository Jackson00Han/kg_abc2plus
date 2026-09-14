#!/usr/bin/env python3
"""Live review-protocol probe using synthetic data only; never reads the knowledge DB."""
import asyncio
from dataclasses import asdict
import json
from pathlib import Path

from dotenv import dotenv_values
from openai import AsyncOpenAI
from graphrag_prod.knowledge.auto_review_model import OpenAICompatibleAutoReviewer, VERSION

ROOT = Path(__file__).resolve().parents[1]


async def main():
    config = {**dotenv_values(ROOT / '.env'), **dotenv_values(ROOT / '.env.workbench.local')}
    output = ROOT / '.local' / 'auto-review-v2' / 'live-model.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    # Synthetic records deliberately use different names for different objects.
    source = [{"asset_ref": f"TEST/SECTION/J{i}", "full_name": f"Test.Section.Joint{i}"}
              for i in (1, 2, 3)]
    records = [{"record_id": f"fact-{i}", "evidence_ids": [f"source-{i}"],
                "subject": {"entity_id": row["asset_ref"], "entity_type": "Joint"},
                "predicate": "full_name", "object": None,
                "literal": {"datatype": "STRING", "canonical_value": row["full_name"]}}
               for i, row in enumerate(source, 1)]
    evidence = [{"evidence_id": f"source-{i}", "text": json.dumps(row), "document_version_id": "synthetic-v1"}
                for i, row in enumerate(source, 1)]
    common = {"evidence": evidence, "ontology": {"entity_types": [{"name": "Joint",
        "properties": [{"name": "full_name", "datatype": "STRING", "description": "完整名称"}]}]},
        "rules": {"identity_endpoints_approved": True, "program_checks_every_record": True}}
    mapping = {**common, "records": [{"record_id": "mapping:joint-name",
        "evidence_ids": [e["evidence_id"] for e in evidence],
        "mapping": {"subject_type": "Joint", "predicate": "full_name", "field": "/full_name"},
        "samples": [{**r, "sample_record_id": r["record_id"]} for r in records]}]}
    records[2]["literal"]["canonical_value"] = "Wrong.Name"
    facts = {**common, "records": records}
    report = {"version": VERSION, "data": "synthetic only", "knowledge_writes": 0,
              "model": config['MODEL_NAME'], "results": {}, "status": "running"}
    async with AsyncOpenAI(api_key=config['OPENAI_API_KEY'], base_url=config['OPENAI_BASE_URL'],
                           max_retries=0) as client:
        reviewer = OpenAICompatibleAutoReviewer(client=client, model=config['MODEL_NAME'])
        # Isolate the mapping samples from the deliberately incorrect fact probe.
        mapping["records"][0]["samples"][-1]["literal"] = {
            "datatype": "STRING", "canonical_value": source[2]["full_name"]}
        for kind, payload in (("mapping", mapping), ("facts", facts)):
            result = await reviewer.review(kind, payload)
            report["results"][kind] = {"status": result.status,
                "decisions": [asdict(d) for d in result.decisions],
                "calls": len(result.audit.get('attempts', [])),
                "failure_code": result.audit.get('failure_code')}
    mapping_ok = [(d['record_id'], d['action']) for d in report['results']['mapping']['decisions']] == [
        ('mapping:joint-name', 'VALID')]
    facts_ok = [(d['record_id'], d['action']) for d in report['results']['facts']['decisions']] == [
        ('fact-1', 'APPROVE'), ('fact-2', 'APPROVE'), ('fact-3', 'UNCERTAIN')]
    report['status'] = 'passed' if mapping_ok and facts_ok else 'failed'
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    output.chmod(0o600)
    print(json.dumps({'status': report['status'], 'output': str(output)}, ensure_ascii=False))
    if report['status'] != 'passed':
        raise SystemExit(1)


if __name__ == '__main__':
    asyncio.run(main())
