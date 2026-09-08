"""Restore the disposable Playground from verified public fixture embeddings.

This adapter is deliberately outside the production API. The local reset
controller must hold exclusive admission before invoking the destructive reset.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Mapping

from neo4j import Query

from graphrag_prod.domain import ChunkEmbedding
from graphrag_prod.domain.ids import chunk_embedding_id, content_checksum


def _require_startup_chunks(chunks: tuple[Any, ...], dataset_id: str = "dev-corpus-v1") -> None:
    from tests.fixtures.dev_corpus import load_dev_corpus_fixture

    if dataset_id == "dev-corpus-v1":
        fixture = load_dev_corpus_fixture()
    elif dataset_id == "demo-mini-zh-v1":
        from graphrag_prod.playground.demo_corpus import load_demo_corpus
        fixture = load_demo_corpus()
    else:
        raise ValueError("unknown reset corpus")
    expected = tuple(bundle.chunk for plan in fixture.plans for bundle in plan.bundles)
    if chunks != expected:
        raise ValueError("reset requires the complete unchanged startup fixture")


@dataclass(frozen=True)
class CachedFixtureEmbedder:
    provider: str
    model: str
    revision: str
    dimensions: int
    normalization: str
    embedding_space_id: str
    chunks: tuple[Any, ...]
    vectors: Mapping[str, tuple[float, ...]]
    dataset_id: str = "dev-corpus-v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "chunks", tuple(self.chunks))
        object.__setattr__(self, "vectors", MappingProxyType({
            key: tuple(vector) for key, vector in self.vectors.items()
        }))
        self.validate()

    def validate(self) -> None:
        _require_startup_chunks(self.chunks, self.dataset_id)
        if (not self.chunks or type(self.dimensions) is not int
                or len({chunk.chunk_id for chunk in self.chunks}) != len(self.chunks)
                or set(self.vectors) != {chunk.text for chunk in self.chunks}):
            raise ValueError("reset fixture embedding cache is incomplete")
        for chunk in self.chunks:
            if content_checksum(chunk.text) != chunk.checksum:
                raise ValueError("reset fixture source checksum changed")
            embedding = ChunkEmbedding(
                embedding_id=chunk_embedding_id(chunk.chunk_id, self.embedding_space_id),
                tenant_id=chunk.tenant_id, chunk_id=chunk.chunk_id,
                embedding_space_id=self.embedding_space_id, provider=self.provider,
                model=self.model, revision=self.revision, dimensions=self.dimensions,
                normalization=self.normalization, created_at=datetime.now(UTC),
                vector=self.vectors[chunk.text],
            )
            if not embedding.vector:
                raise ValueError("reset fixture vector is missing")

    def embed_documents(self, texts: list[str]) -> list[tuple[float, ...]]:
        # No live provider is reachable from the destructive restoration phase.
        try:
            return [self.vectors[text] for text in texts]
        except KeyError:
            raise ValueError("reset only accepts the verified startup fixture") from None


def capture_reset_embeddings(driver, database: str, fixture, embedder) -> CachedFixtureEmbedder:
    """Read and validate only the versioned fixture's vectors, before deletion."""
    chunks = tuple(bundle.chunk for plan in fixture.plans for bundle in plan.bundles)
    dataset_id = fixture.build.manifest["dataset_id"]
    _require_startup_chunks(chunks, dataset_id)
    expected = {chunk.chunk_id: chunk for chunk in chunks}
    if len(expected) != len(chunks) or not chunks:
        raise ValueError("reset fixture contains invalid chunk identities")
    rows, _, _ = driver.execute_query(
        Query(
            """
            UNWIND $chunks AS expected
            OPTIONAL MATCH (chunk:Chunk {
                chunk_id: expected.chunk_id, tenant_id: expected.tenant_id
            })-[:HAS_EMBEDDING]->(embedding:ChunkEmbedding {
                embedding_space_id: $embedding_space_id, tenant_id: expected.tenant_id
            })
            RETURN expected.chunk_id AS expected_chunk_id,
                   chunk.text AS text, chunk.checksum AS checksum,
                   embedding{.*} AS embedding
            LIMIT $limit
            """, timeout=10.0,
        ),
        chunks=[{"chunk_id": chunk.chunk_id, "tenant_id": chunk.tenant_id} for chunk in chunks],
        embedding_space_id=embedder.embedding_space_id, limit=len(chunks) + 1,
        database_=database,
    )
    if len(rows) != len(chunks):
        raise ValueError("reset fixture embedding coverage is invalid")
    seen: set[str] = set()
    vectors: dict[str, tuple[float, ...]] = {}
    profile = {key: getattr(embedder, key) for key in (
        "provider", "model", "revision", "dimensions", "normalization", "embedding_space_id",
    )}
    for row in rows:
        chunk = expected.get(row["expected_chunk_id"])
        data = row["embedding"]
        if (chunk is None or chunk.chunk_id in seen or not isinstance(data, dict)
                or row["text"] != chunk.text or row["checksum"] != chunk.checksum
                or any(data.get(key) != value for key, value in profile.items())
                or type(data.get("dimensions")) is not int
                or data.get("chunk_id") != chunk.chunk_id
                or data.get("tenant_id") != chunk.tenant_id
                or data.get("embedding_id") != chunk_embedding_id(chunk.chunk_id, embedder.embedding_space_id)):
            raise ValueError("reset fixture provenance or embedding profile is incompatible")
        embedding = ChunkEmbedding(
            embedding_id=data["embedding_id"], tenant_id=chunk.tenant_id,
            chunk_id=chunk.chunk_id, created_at=datetime.now(UTC),
            vector=data.get("vector", ()), **profile,
        )
        if not embedding.vector or embedding.vector_checksum != data.get("vector_checksum"):
            raise ValueError("reset fixture vector checksum is invalid")
        if chunk.text in vectors and vectors[chunk.text] != embedding.vector:
            raise ValueError("identical fixture text has conflicting vectors")
        seen.add(chunk.chunk_id)
        vectors[chunk.text] = embedding.vector
    return CachedFixtureEmbedder(chunks=chunks, vectors=vectors, dataset_id=dataset_id, **profile)


def reset_playground_corpus(driver, database: str, cached: CachedFixtureEmbedder) -> None:
    """Restore the whole exclusively held disposable database, never a tenant API."""
    if not isinstance(cached, CachedFixtureEmbedder):
        raise ValueError("a verified reset fixture cache is required")
    cached.validate()
    from scripts.run_playground import _load_corpus, _reuse_corpus

    driver.execute_query(Query("MATCH (node) DETACH DELETE node", timeout=30.0), database_=database)
    options = {} if cached.dataset_id == "dev-corpus-v1" else {"corpus_profile": cached.dataset_id}
    _load_corpus(driver, database, cached, **options)
    _reuse_corpus(driver, database, cached, **options)
