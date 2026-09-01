"""Domain fixtures backed by the versioned Stage 5A development corpus."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from graphrag_prod.domain import (
    Assertion,
    Chunk,
    ChunkEmbedding,
    Document,
    DocumentVersion,
    Entity,
    EntityMention,
    GraphPipelineProfile,
    chunk_embedding_id,
)
from graphrag_prod.graph.governance import load_governance_policy
from graphrag_prod.graph.provenance import ProvenanceBundle
from graphrag_prod.ingestion.models import (
    IngestionPlan,
    default_artifact_input_hash,
)
from scripts.build_dev_corpus import (
    DEFAULT_DATASET_DIR,
    DatasetBuild,
    build_dataset,
    check_dataset,
)


ROOT = Path(__file__).parents[2]


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("development-corpus timestamps must be timezone-aware")
    return parsed


@dataclass(frozen=True, slots=True)
class DevCorpusFixture:
    """Fully materialized deterministic inputs for real-Neo4j validation."""

    build: DatasetBuild
    plans: tuple[IngestionPlan, ...]
    chunks_by_id: dict[str, dict[str, Any]]
    chunks_by_key: dict[str, dict[str, Any]]
    documents_by_id: dict[str, dict[str, Any]]
    questions_by_id: dict[str, dict[str, Any]]
    vectors_by_id: dict[str, tuple[float, ...]]
    source_texts: dict[str, str]

    def question(self, item_id: str) -> dict[str, Any]:
        return self.questions_by_id[item_id]

    def query_vector(self, question: dict[str, Any]) -> tuple[float, ...]:
        return self.vectors_by_id[question["vector_id"]]


def load_dev_corpus_fixture(
    dataset_dir: Path = DEFAULT_DATASET_DIR,
) -> DevCorpusFixture:
    """Verify committed bytes, then convert them to production domain records."""
    build = build_dataset()
    drift = check_dataset(dataset_dir, build)
    if drift:
        raise ValueError("development corpus is not reproducible: " + "; ".join(drift))

    profile_data = build.manifest["pipeline_profile"]
    profile = GraphPipelineProfile(
        profile_id=profile_data["profile_id"],
        normalizer_signature=profile_data["normalizer_signature"],
        splitter_signature=profile_data["splitter_signature"],
        extractor_signature=profile_data["extractor_signature"],
        prompt_signature=profile_data["prompt_signature"],
        schema_signature=profile_data["schema_signature"],
        code_signature=profile_data["code_signature"],
    )
    governance_policy = load_governance_policy(
        ROOT / "contracts" / "graph_governance.v1.json",
        profile.schema_signature,
    )
    embedding_profile = build.manifest["embedding_profile"]
    vectors_by_id = {
        item["id"]: tuple(float(value) for value in item["vector"])
        for item in build.vectors
    }
    entities_by_id = {
        item["entity_id"]: Entity(
            entity_id=item["entity_id"],
            tenant_id=item["tenant_id"],
            entity_type=item["entity_type"],
            canonical_key=item["canonical_key"],
            canonical_name=item["canonical_name"],
            aliases=tuple(item["aliases"]),
        )
        for item in build.entities
    }
    chunks_by_document: dict[str, list[dict[str, Any]]] = {}
    for item in build.chunks:
        chunks_by_document.setdefault(item["document_id"], []).append(item)

    plans: list[IngestionPlan] = []
    source_texts: dict[str, str] = {}
    for document_data in build.documents:
        source_path = document_data["source_path"]
        normalized_text = build.files[source_path].decode("utf-8")
        source_texts[source_path] = normalized_text
        created_at = _timestamp(document_data["created_at"])
        ingested_at = _timestamp(document_data["ingested_at"])
        published_at = _timestamp(document_data["published_at"])
        document = Document(
            document_id=document_data["document_id"],
            tenant_id=document_data["tenant_id"],
            canonical_uri=document_data["canonical_uri"],
            title=document_data["title"],
            source_name=document_data["source_name"],
            access_policy_id=document_data["access_policy_id"],
            access_policy_version=document_data["access_policy_version"],
            access_groups=frozenset(document_data["access_groups"]),
            created_at=created_at,
        )
        version = DocumentVersion(
            version_id=document_data["version_id"],
            document_id=document.document_id,
            tenant_id=document.tenant_id,
            checksum=document_data["checksum"],
            original_checksum=document_data["original_checksum"],
            normalized_text=normalized_text,
            version_number=document_data["version_number"],
            mime_type=document_data["mime_type"],
            language=document_data["language"],
            published_at=published_at,
            ingested_at=ingested_at,
        )
        bundles: list[ProvenanceBundle] = []
        for chunk_data in sorted(
            chunks_by_document[document.document_id],
            key=lambda item: item["ordinal"],
        ):
            text = normalized_text[
                chunk_data["char_start"] : chunk_data["char_end"]
            ]
            chunk = Chunk(
                chunk_id=chunk_data["chunk_id"],
                version_id=version.version_id,
                document_id=document.document_id,
                tenant_id=document.tenant_id,
                access_policy_id=chunk_data["access_policy_id"],
                access_policy_version=chunk_data["access_policy_version"],
                access_groups=frozenset(chunk_data["access_groups"]),
                ordinal=chunk_data["ordinal"],
                text=text,
                checksum=chunk_data["checksum"],
                char_start=chunk_data["char_start"],
                char_end=chunk_data["char_end"],
                page_number=chunk_data["page_number"],
                section=chunk_data["section"],
                splitter_version=chunk_data["splitter_version"],
            )
            mentions = tuple(
                EntityMention(
                    mention_id=item["mention_id"],
                    tenant_id=document.tenant_id,
                    chunk_id=chunk.chunk_id,
                    entity_id=item["entity_id"],
                    entity_type=item["entity_type"],
                    surface=item["surface"],
                    char_start=item["char_start"],
                    char_end=item["char_end"],
                    extractor_version=item["extractor_version"],
                    confidence=item["confidence"],
                )
                for item in chunk_data["mentions"]
            )
            assertions = tuple(
                Assertion(
                    assertion_id=item["assertion_id"],
                    tenant_id=item["tenant_id"],
                    subject_entity_id=item["subject_entity_id"],
                    predicate=item["predicate"],
                    evidence_chunk_id=item["evidence_chunk_id"],
                    evidence_char_start=item["evidence_char_start"],
                    evidence_char_end=item["evidence_char_end"],
                    extractor_version=item["extractor_version"],
                    schema_version=item["schema_version"],
                    confidence=item["confidence"],
                    accepted=item["accepted"],
                    object_entity_id=item["object_entity_id"],
                    literal_value=item["literal_value"],
                )
                for item in chunk_data["assertions"]
            )
            mentioned_ids = {mention.entity_id for mention in mentions}
            entities = tuple(
                entities_by_id[entity_id]
                for entity_id in sorted(mentioned_ids)
            )
            embedding = ChunkEmbedding(
                embedding_id=chunk_embedding_id(
                    chunk.chunk_id,
                    embedding_profile["embedding_space_id"],
                ),
                tenant_id=document.tenant_id,
                chunk_id=chunk.chunk_id,
                embedding_space_id=embedding_profile["embedding_space_id"],
                provider=embedding_profile["provider"],
                model=embedding_profile["model"],
                revision=embedding_profile["revision"],
                dimensions=embedding_profile["dimensions"],
                normalization=embedding_profile["normalization"],
                created_at=ingested_at,
                vector=vectors_by_id[chunk.chunk_id],
            )
            bundles.append(
                ProvenanceBundle(
                    document=document,
                    version=version,
                    chunk=chunk,
                    embedding=embedding,
                    entities=entities,
                    mentions=mentions,
                    assertion=assertions[0] if assertions else None,
                    additional_assertions=assertions[1:],
                    activate_version=False,
                )
            )
        bundle_tuple = tuple(bundles)
        plans.append(
            IngestionPlan.build(
                operation_key=f"{build.manifest['dataset_id']}:{document_data['document_key']}",
                profile=profile,
                governance_policy=governance_policy,
                bundles=bundle_tuple,
                expected_active_snapshot_id=None,
                source_generation=0,
                artifact_input_hashes={
                    bundle.chunk.chunk_id: default_artifact_input_hash(bundle)
                    for bundle in bundle_tuple
                },
                created_at=ingested_at,
            )
        )

    return DevCorpusFixture(
        build=build,
        plans=tuple(plans),
        chunks_by_id={item["chunk_id"]: item for item in build.chunks},
        chunks_by_key={item["chunk_key"]: item for item in build.chunks},
        documents_by_id={item["document_id"]: item for item in build.documents},
        questions_by_id={item["id"]: item for item in build.questions},
        vectors_by_id=vectors_by_id,
        source_texts=source_texts,
    )
