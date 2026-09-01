"""Deterministic multi-chunk fixtures for incremental-ingestion tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib

from graphrag_prod.domain import (
    Assertion,
    Chunk,
    ChunkEmbedding,
    Document,
    DocumentVersion,
    Entity,
    EntityMention,
    GraphPipelineProfile,
    Principal,
    assertion_id,
    canonicalize_uri,
    chunk_embedding_id,
    chunk_id,
    content_checksum,
    document_id,
    embedding_space_id,
    entity_id,
    mention_id,
    pipeline_profile_id,
    version_id,
)
from graphrag_prod.graph.provenance import ProvenanceBundle
from graphrag_prod.ingestion.models import (
    IngestionPlan,
    default_artifact_input_hash,
)


FIXED_TIME = datetime(2024, 10, 1, 12, 0, tzinfo=UTC)
SPLITTER_SIGNATURE = "fixture-chunks:v1"
EXTRACTOR_SIGNATURE = "deterministic-extractor:v1"
SCHEMA_SIGNATURE = "company-metrics:v1"


@dataclass(frozen=True, slots=True)
class ChunkSpec:
    text: str
    literal: str
    predicate: str


CHUNKS_V1 = (
    ChunkSpec("Apple revenue is 391.", "391", "REPORTS_REVENUE"),
    ChunkSpec("Apple margin is 46.2.", "46.2", "REPORTS_MARGIN"),
    ChunkSpec("Apple cash is 29.9.", "29.9", "REPORTS_CASH"),
)

CHUNKS_V2 = (
    CHUNKS_V1[0],
    ChunkSpec("Apple margin is 47.0.", "47.0", "REPORTS_MARGIN"),
    CHUNKS_V1[2],
)


@dataclass(slots=True)
class FixedClock:
    """Clock whose time advances only when a test explicitly requests it."""

    current: datetime = FIXED_TIME

    def now(self) -> datetime:
        return self.current

    def advance(self, *, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


def make_profile() -> GraphPipelineProfile:
    signatures = (
        "unicode-nfc:v1",
        SPLITTER_SIGNATURE,
        EXTRACTOR_SIGNATURE,
        "literal-metrics-prompt:sha256:fixture",
        SCHEMA_SIGNATURE,
        "git:stage3-fixture",
    )
    return GraphPipelineProfile(pipeline_profile_id(*signatures), *signatures)


def make_principal(tenant_id: str = "tenant-stage3") -> Principal:
    return Principal("fixture-reader", tenant_id, frozenset({"knowledge-readers"}))


def _vector(text: str, *, reverse: bool) -> tuple[float, ...]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    vector = tuple(round(value / 255.0, 8) for value in digest[:4])
    return tuple(reversed(vector)) if reverse else vector


def make_bundles(
    *,
    tenant_id: str = "tenant-stage3",
    canonical_uri: str = "https://example.com/knowledge/apple",
    chunk_specs: tuple[ChunkSpec, ...] = CHUNKS_V1,
    version_number: int = 1,
    reverse_vectors: bool = False,
) -> tuple[ProvenanceBundle, ...]:
    """Build one immutable version split into exact, contiguous fixture chunks."""
    uri = canonicalize_uri(canonical_uri)
    normalized_text = "".join(spec.text for spec in chunk_specs)
    normalized_checksum = content_checksum(normalized_text)
    document_identifier = document_id(tenant_id, uri)
    version_identifier = version_id(
        document_identifier,
        normalized_checksum,
        normalized_checksum,
    )
    subject_identifier = entity_id(tenant_id, "Company", "ticker:AAPL")
    subject = Entity(
        entity_id=subject_identifier,
        tenant_id=tenant_id,
        entity_type="Company",
        canonical_key="ticker:AAPL",
        canonical_name="Apple Inc.",
        aliases=("Apple",),
    )
    document = Document(
        document_id=document_identifier,
        tenant_id=tenant_id,
        canonical_uri=uri,
        title=f"Apple fixture version {version_number}",
        source_name="stage3-deterministic-fixture",
        access_policy_id=f"{tenant_id}:knowledge-readers",
        access_policy_version=1,
        access_groups=frozenset({"knowledge-readers"}),
        created_at=FIXED_TIME,
    )
    version_time = FIXED_TIME + timedelta(days=version_number - 1)
    version = DocumentVersion(
        version_id=version_identifier,
        document_id=document_identifier,
        tenant_id=tenant_id,
        checksum=normalized_checksum,
        original_checksum=normalized_checksum,
        normalized_text=normalized_text,
        version_number=version_number,
        mime_type="text/plain",
        language="en",
        published_at=version_time,
        ingested_at=version_time,
    )
    space_identifier = embedding_space_id(
        "fixture",
        "deterministic-four-dimensional",
        "v1",
        4,
        "none",
    )

    bundles: list[ProvenanceBundle] = []
    char_start = 0
    for ordinal, spec in enumerate(chunk_specs):
        char_end = char_start + len(spec.text)
        chunk_checksum = content_checksum(spec.text)
        chunk_identifier = chunk_id(
            version_identifier,
            SPLITTER_SIGNATURE,
            ordinal,
            char_start,
            char_end,
            chunk_checksum,
        )
        subject_start = char_start + spec.text.index("Apple")
        subject_end = subject_start + len("Apple")
        mention_identifier = mention_id(
            chunk_identifier,
            "Company",
            subject_start,
            subject_end,
            "Apple",
            EXTRACTOR_SIGNATURE,
        )
        assertion_identifier = assertion_id(
            tenant_id,
            subject_identifier,
            spec.predicate,
            "literal",
            spec.literal,
            chunk_identifier,
            char_start,
            char_end,
            EXTRACTOR_SIGNATURE,
            SCHEMA_SIGNATURE,
        )
        embedding_identifier = chunk_embedding_id(
            chunk_identifier,
            space_identifier,
        )
        chunk = Chunk(
            chunk_id=chunk_identifier,
            version_id=version_identifier,
            document_id=document_identifier,
            tenant_id=tenant_id,
            access_policy_id=document.access_policy_id,
            access_policy_version=document.access_policy_version,
            access_groups=document.access_groups,
            ordinal=ordinal,
            text=spec.text,
            checksum=chunk_checksum,
            char_start=char_start,
            char_end=char_end,
            page_number=1,
            section=f"Metric {ordinal + 1}",
            splitter_version=SPLITTER_SIGNATURE,
        )
        embedding = ChunkEmbedding(
            embedding_id=embedding_identifier,
            tenant_id=tenant_id,
            chunk_id=chunk_identifier,
            embedding_space_id=space_identifier,
            provider="fixture",
            model="deterministic-four-dimensional",
            revision="v1",
            dimensions=4,
            normalization="none",
            created_at=version_time,
            vector=_vector(spec.text, reverse=reverse_vectors),
        )
        assertion = Assertion(
            assertion_id=assertion_identifier,
            tenant_id=tenant_id,
            subject_entity_id=subject_identifier,
            predicate=spec.predicate,
            evidence_chunk_id=chunk_identifier,
            evidence_char_start=char_start,
            evidence_char_end=char_end,
            extractor_version=EXTRACTOR_SIGNATURE,
            schema_version=SCHEMA_SIGNATURE,
            confidence=1.0,
            accepted=True,
            literal_value=spec.literal,
        )
        use_additional_fields = ordinal == 1
        bundles.append(
            ProvenanceBundle(
                document=document,
                version=version,
                chunk=chunk,
                embedding=None if use_additional_fields else embedding,
                entities=(subject,),
                mentions=(
                    EntityMention(
                        mention_id=mention_identifier,
                        tenant_id=tenant_id,
                        chunk_id=chunk_identifier,
                        entity_id=subject_identifier,
                        entity_type="Company",
                        surface="Apple",
                        char_start=subject_start,
                        char_end=subject_end,
                        extractor_version=EXTRACTOR_SIGNATURE,
                        confidence=1.0,
                    ),
                ),
                assertion=None if use_additional_fields else assertion,
                activate_version=False,
                additional_embeddings=(embedding,) if use_additional_fields else (),
                additional_assertions=(assertion,) if use_additional_fields else (),
            )
        )
        char_start = char_end
    return tuple(bundles)


def make_plan(
    *,
    operation_key: str = "upsert-apple-v1",
    tenant_id: str = "tenant-stage3",
    canonical_uri: str = "https://example.com/knowledge/apple",
    chunk_specs: tuple[ChunkSpec, ...] = CHUNKS_V1,
    version_number: int = 1,
    expected_active_snapshot_id: str | None = None,
    source_generation: int = 0,
    reverse_vectors: bool = False,
) -> IngestionPlan:
    bundles = make_bundles(
        tenant_id=tenant_id,
        canonical_uri=canonical_uri,
        chunk_specs=chunk_specs,
        version_number=version_number,
        reverse_vectors=reverse_vectors,
    )
    return IngestionPlan.build(
        operation_key=operation_key,
        profile=make_profile(),
        bundles=bundles,
        expected_active_snapshot_id=expected_active_snapshot_id,
        source_generation=source_generation,
        artifact_input_hashes={
            bundle.chunk.chunk_id: default_artifact_input_hash(bundle)
            for bundle in bundles
        },
        created_at=FIXED_TIME + timedelta(days=version_number - 1),
    )
