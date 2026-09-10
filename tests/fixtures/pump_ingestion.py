"""Source-only ingestion fixture from the authorized circulating-pump package."""
from datetime import UTC, datetime
import json
from pathlib import Path

from graphrag_prod.domain import (
    Chunk, ChunkEmbedding, Document, DocumentVersion, GraphPipelineProfile,
    chunk_embedding_id, chunk_id, content_checksum, document_id, embedding_space_id,
    pipeline_profile_id, version_id,
)
from graphrag_prod.graph.provenance import ProvenanceBundle
from graphrag_prod.ontology import TBoxVersion
from graphrag_prod.ingestion import IngestionPlan
from graphrag_prod.ingestion.models import default_artifact_input_hash

ROOT = Path(__file__).resolve().parents[2]


def make_plan(*, tenant_id: str = 'pump-workspace-test') -> IngestionPlan:
    text = (ROOT / 'src/graphrag_prod/playground/static/industrial-demo-v1/maintenance_report.txt').read_text()
    now = datetime(2026, 9, 10, tzinfo=UTC)
    uri = 'urn:local:pump-workspace-test:maintenance_report.txt'
    checksum = content_checksum(text)
    doc_id = document_id(tenant_id, uri)
    ver_id = version_id(doc_id, checksum, checksum)
    splitter = 'pump-source-whole-document:v1'
    chunk_key = chunk_id(ver_id, splitter, 0, 0, len(text), checksum)
    document = Document(doc_id, tenant_id, uri, '循环水泵检修记录', 'industrial-demo-v1',
                        tenant_id + ':members', 1, frozenset({'members'}), now)
    version = DocumentVersion(ver_id, doc_id, tenant_id, checksum, checksum, text,
                              1, 'text/plain', 'zh', now, now)
    chunk = Chunk(chunk_key, ver_id, doc_id, tenant_id, document.access_policy_id,
                  1, document.access_groups, 0, text, checksum, 0, len(text), None,
                  None, splitter)
    space = embedding_space_id('fixture', 'pump-deterministic', 'v1', 4, 'none')
    embedding = ChunkEmbedding(chunk_embedding_id(chunk_key, space), tenant_id,
                               chunk_key, space, 'fixture', 'pump-deterministic',
                               'v1', 4, 'none', now, (1.0, 0.0, 0.0, 0.0))
    bundle = ProvenanceBundle(document, version, chunk, embedding, (), (), None,
                              activate_version=False)
    definition = json.loads((ROOT / 'src/graphrag_prod/playground/static/industrial-demo-v1/ontology.json').read_text())
    policy = TBoxVersion.from_mapping({**definition, 'tenant_id': tenant_id, 'status': 'PUBLISHED'}).compile_governance_policy()
    signatures = ('unicode-nfc:v1', splitter, 'source-only:v1', 'no-extraction:v1',
                  policy.policy_id, 'workspace-test:v1')
    profile = GraphPipelineProfile(pipeline_profile_id(*signatures), *signatures)
    return IngestionPlan.build(
        operation_key='pump-workspace-source-v1', profile=profile,
        governance_policy=policy, bundles=(bundle,), expected_active_snapshot_id=None,
        source_generation=0, artifact_input_hashes={chunk_key: default_artifact_input_hash(bundle)},
        created_at=now,
    )
