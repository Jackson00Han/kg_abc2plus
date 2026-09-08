"""Small Chinese demonstration inputs, isolated from regression/load corpora.

Seed records are source-only. Governed graph facts are built explicitly through
normal upload/import/review/publication APIs using the three-file pump kit.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from graphrag_prod.domain import (
    Chunk, Document, DocumentVersion, GraphPipelineProfile,
    chunk_id, content_checksum, document_id, pipeline_profile_id, version_id,
)
from graphrag_prod.graph.provenance import ProvenanceBundle
from graphrag_prod.graph.governance import (
    GraphGovernancePolicy, EntityTypeRule, RelationshipRule, ENTITY_FIELDS, ASSERTION_FIELDS,
)
from graphrag_prod.ingestion.models import IngestionPlan, default_artifact_input_hash

DATASET_ID = "demo-mini-zh-v1"
DIRECTORY = Path(__file__).parent / "static" / DATASET_ID


@dataclass(frozen=True)
class DemoCorpus:
    build: Any
    plans: tuple[IngestionPlan, ...]


def load_demo_corpus() -> DemoCorpus:
    manifest = json.loads((DIRECTORY / "manifest.json").read_text(encoding="utf-8"))
    if manifest["dataset_id"] != DATASET_ID or manifest["version"] != "1.0.0":
        raise ValueError("unknown demonstration corpus version")
    now = datetime(2026, 9, 1, tzinfo=UTC)
    signatures = ("unicode-nfc:v1", "demo-exact-ranges:v1", "source-only:v1",
                  "no-extraction:v1", "demo-sources:v1", "demo-mini-zh:v1")
    profile = GraphPipelineProfile(pipeline_profile_id(*signatures), *signatures)
    policy = GraphGovernancePolicy(
        signatures[4], 1,
        (EntityTypeRule("Equipment", frozenset({"equipment-id"}), ENTITY_FIELDS, ENTITY_FIELDS),),
        (RelationshipRule("RatedPower", frozenset({"Equipment"}), "literal", frozenset(),
                          ASSERTION_FIELDS, ASSERTION_FIELDS),), 0.8, 0.8, 25,
    )
    plans = []
    chunks = []
    for item in manifest["documents"]:
        filename = item["filename"]
        if Path(filename).name != filename:
            raise ValueError("invalid demo source path")
        payload = (DIRECTORY / filename).read_bytes()
        checksum = content_checksum(payload)
        if checksum != item["sha256"]:
            raise ValueError("demo source checksum mismatch")
        text = payload.decode("utf-8")
        lengths = item["chunk_lengths"]
        if any(type(n) is not int or n <= 0 for n in lengths) or sum(lengths) != len(text):
            raise ValueError("demo source ranges are incomplete")
        tenant = item["tenant_id"]
        uri = f"urn:sample-graphrag:{DATASET_ID}:{filename}"
        doc_id = document_id(tenant, uri)
        ver_id = version_id(doc_id, checksum, checksum)
        groups = frozenset(item["groups"])
        document = Document(document_id=doc_id, tenant_id=tenant, canonical_uri=uri,
                            title=item["title"], source_name="中文最小演示（虚构）",
                            access_policy_id=f"{tenant}:demo", access_policy_version=1,
                            access_groups=groups, created_at=now)
        version = DocumentVersion(version_id=ver_id, document_id=doc_id, tenant_id=tenant,
                                  checksum=checksum, original_checksum=checksum,
                                  normalized_text=text, version_number=1, mime_type="text/plain",
                                  language="zh", published_at=now, ingested_at=now)
        bundles = []
        start = 0
        for ordinal, length in enumerate(lengths):
            end = start + length
            part = text[start:end]
            digest = content_checksum(part)
            chunk = Chunk(chunk_id=chunk_id(ver_id, signatures[1], ordinal, start, end, digest),
                          version_id=ver_id, document_id=doc_id, tenant_id=tenant,
                          access_policy_id=document.access_policy_id, access_policy_version=1,
                          access_groups=groups, ordinal=ordinal, text=part, checksum=digest,
                          char_start=start, char_end=end, page_number=None,
                          section=item["title"], splitter_version=signatures[1])
            chunks.append(chunk)
            bundles.append(ProvenanceBundle(document, version, chunk, None, (), (), None,
                                             activate_version=False))
            start = end
        plans.append(IngestionPlan.build(
            operation_key=f"{DATASET_ID}:{filename}", profile=profile, governance_policy=policy, bundles=tuple(bundles),
            expected_active_snapshot_id=None, source_generation=0,
            artifact_input_hashes={b.chunk.chunk_id: default_artifact_input_hash(b) for b in bundles},
            created_at=now,
        ))
    # All identities have an example, keeping role switching explicit and small.
    specs = (
        ("single_chunk-success-01", "single_chunk", "循环水泵 P-001 的额定功率是多少？", "demo-a", ["administrator", "public"], [0]),
        ("cross-chunk", "cross_chunk", "循环水泵 P-001 安装在哪里，包含哪个部件？", "demo-a", ["public"], [0, 1]),
        ("relationship", "graph_relationship", "循环水泵 P-001 与机械密封 M-001 有什么关系？", "demo-a", ["public"], [1]),
        ("exact-value", "exact_value", "2026-09-01 循环水泵 P-001 的巡检温度是多少？", "demo-a", ["public"], [2]),
        ("temporal", "temporal_conflict", "循环水泵 P-001 两次温度记录为何不同？", "demo-a", ["public"], [2, 3]),
        ("maintenance", "single_chunk", "循环水泵 P-001 的停机检修日期是什么？", "demo-a", ["maintenance", "public"], [4]),
        ("reviewer", "single_chunk", "循环水泵 P-001 的停机检修日期是什么？", "demo-a", ["public", "reviewer"], [4]),
        ("unauthorized", "unauthorized", "内部停机检修日期是什么？", "demo-a", ["public"], []),
        ("unanswerable", "unanswerable", "循环水泵的采购价格是多少？", "demo-a", ["public"], []),
        ("other-tenant", "single_chunk", "二号泵站循环水泵 P-002 的额定功率是多少？", "demo-b", ["public"], [5]),
    )
    questions = []
    for key, category, query, tenant, groups, relevant in specs:
        forbidden = [c.chunk_id for c in chunks if c.tenant_id != tenant or not c.access_groups.intersection(groups)]
        questions.append(dict(id=key, question_class=category, query=query,
                              case_type="success" if relevant else "negative", answerable=bool(relevant),
                              principal=dict(principal_id=f"demo:{key}", tenant_id=tenant, groups=groups),
                              relevance={chunks[i].chunk_id: 3 for i in relevant}, forbidden_chunk_ids=forbidden))
    manifest["counts"] = {"documents": len(plans), "active_chunks": len(chunks), "tenants": 2,
                          "questions": len(questions), "entities": 0, "assertions": 0}
    manifest["embedding_profile"] = {"provider": "runtime", "model": "configured-provider",
                                     "dimensions": 1024, "warning": "仅演示；不作为质量基线"}
    return DemoCorpus(SimpleNamespace(manifest=manifest, questions=tuple(questions)), tuple(plans))
