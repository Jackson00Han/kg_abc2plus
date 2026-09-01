"""Build a complete, internally consistent provenance fixture."""

from __future__ import annotations

from datetime import UTC, datetime

from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import (
    assertion_id,
    chunk_embedding_id,
    chunk_id,
    content_checksum,
    document_id,
    embedding_space_id,
    entity_id,
    mention_id,
    version_id,
)
from graphrag_prod.domain.models import (
    Assertion,
    Chunk,
    ChunkEmbedding,
    Document,
    DocumentVersion,
    Entity,
    EntityMention,
)
from graphrag_prod.graph.provenance import ProvenanceBundle


SOURCE_TEXT = "Apple offers iPhone."
CANONICAL_URI = "https://example.com/filings/apple-2024"


def make_bundle(
    *,
    tenant_id: str = "tenant-stage2",
    source_text: str = SOURCE_TEXT,
    version_number: int = 1,
    activate_version: bool = True,
) -> ProvenanceBundle:
    now = datetime(2024, 10, 1, 12, 0, tzinfo=UTC)
    groups = frozenset({"finance-readers"})
    document_identifier = document_id(tenant_id, CANONICAL_URI)
    source_checksum = content_checksum(source_text)
    version_identifier = version_id(
        document_identifier,
        source_checksum,
        source_checksum,
    )
    chunk_identifier = chunk_id(
        version_identifier,
        "fixed-size:v1:size=500:overlap=100",
        0,
        0,
        len(source_text),
        source_checksum,
    )
    company_identifier = entity_id(tenant_id, "Company", "ticker:AAPL")
    product_identifier = entity_id(tenant_id, "Product", "apple-product:iphone")
    apple_start = source_text.index("Apple")
    apple_end = apple_start + len("Apple")
    iphone_start = source_text.index("iPhone")
    iphone_end = iphone_start + len("iPhone")
    company_mention_id = mention_id(
        chunk_identifier,
        "Company",
        apple_start,
        apple_end,
        "Apple",
        "deterministic-extractor:v1",
    )
    product_mention_id = mention_id(
        chunk_identifier,
        "Product",
        iphone_start,
        iphone_end,
        "iPhone",
        "deterministic-extractor:v1",
    )
    fact_identifier = assertion_id(
        tenant_id,
        company_identifier,
        "OFFERS",
        "entity",
        product_identifier,
        chunk_identifier,
        0,
        len(source_text),
        "deterministic-extractor:v1",
        "company-filings:v1",
    )
    space_identifier = embedding_space_id(
        "test",
        "deterministic-embedding",
        "v1",
        4,
        "l2",
    )
    embedding_identifier = chunk_embedding_id(chunk_identifier, space_identifier)

    return ProvenanceBundle(
        document=Document(
            document_id=document_identifier,
            tenant_id=tenant_id,
            canonical_uri=CANONICAL_URI,
            title="Apple 2024 filing fixture",
            source_name="deterministic-fixture",
            access_policy_id=f"{tenant_id}:finance-readers",
            access_policy_version=1,
            access_groups=groups,
            created_at=now,
        ),
        version=DocumentVersion(
            version_id=version_identifier,
            document_id=document_identifier,
            tenant_id=tenant_id,
            checksum=source_checksum,
            original_checksum=source_checksum,
            normalized_text=source_text,
            version_number=version_number,
            mime_type="text/plain",
            language="en",
            published_at=now,
            ingested_at=now,
        ),
        chunk=Chunk(
            chunk_id=chunk_identifier,
            version_id=version_identifier,
            document_id=document_identifier,
            tenant_id=tenant_id,
            access_policy_id=f"{tenant_id}:finance-readers",
            access_policy_version=1,
            access_groups=groups,
            ordinal=0,
            text=source_text,
            checksum=source_checksum,
            char_start=0,
            char_end=len(source_text),
            page_number=1,
            section="Products",
            splitter_version="fixed-size:v1:size=500:overlap=100",
        ),
        embedding=ChunkEmbedding(
            embedding_id=embedding_identifier,
            tenant_id=tenant_id,
            chunk_id=chunk_identifier,
            embedding_space_id=space_identifier,
            provider="test",
            model="deterministic-embedding",
            revision="v1",
            dimensions=4,
            normalization="l2",
            created_at=now,
        ),
        entities=(
            Entity(
                entity_id=company_identifier,
                tenant_id=tenant_id,
                entity_type="Company",
                canonical_key="ticker:AAPL",
                canonical_name="Apple Inc.",
                aliases=("Apple",),
            ),
            Entity(
                entity_id=product_identifier,
                tenant_id=tenant_id,
                entity_type="Product",
                canonical_key="apple-product:iphone",
                canonical_name="iPhone",
            ),
        ),
        mentions=(
            EntityMention(
                mention_id=company_mention_id,
                tenant_id=tenant_id,
                chunk_id=chunk_identifier,
                entity_id=company_identifier,
                entity_type="Company",
                surface="Apple",
                char_start=apple_start,
                char_end=apple_end,
                extractor_version="deterministic-extractor:v1",
                confidence=1.0,
            ),
            EntityMention(
                mention_id=product_mention_id,
                tenant_id=tenant_id,
                chunk_id=chunk_identifier,
                entity_id=product_identifier,
                entity_type="Product",
                surface="iPhone",
                char_start=iphone_start,
                char_end=iphone_end,
                extractor_version="deterministic-extractor:v1",
                confidence=1.0,
            ),
        ),
        assertion=Assertion(
            assertion_id=fact_identifier,
            tenant_id=tenant_id,
            subject_entity_id=company_identifier,
            predicate="OFFERS",
            object_entity_id=product_identifier,
            evidence_chunk_id=chunk_identifier,
            evidence_char_start=0,
            evidence_char_end=len(source_text),
            extractor_version="deterministic-extractor:v1",
            schema_version="company-filings:v1",
            confidence=1.0,
            accepted=True,
        ),
        activate_version=activate_version,
    )


def authorized_principal(tenant_id: str = "tenant-stage2") -> Principal:
    return Principal(
        principal_id="alice",
        tenant_id=tenant_id,
        groups=frozenset({"finance-readers"}),
    )
