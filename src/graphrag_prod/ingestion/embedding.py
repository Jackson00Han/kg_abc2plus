"""Isolated Neo4j vector-index generations with verified atomic cutover."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from graphrag_prod.domain.ids import embedding_index_generation_id
from graphrag_prod.domain.models import ChunkEmbedding
from graphrag_prod.graph.provenance import Neo4jProvenanceStore

from .models import Checkpoint
from .service import IngestionConflict, Neo4jIngestionService


_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")


@dataclass(frozen=True, slots=True)
class EmbeddingGenerationView:
    generation_id: str
    tenant_id: str
    embedding_space_id: str
    generation_version: int
    index_name: str
    label_name: str
    dimensions: int
    similarity: str
    state: str
    corpus_revision: int | None


@dataclass(frozen=True, slots=True)
class EmbeddingCoverage:
    total_chunks: int
    covered_chunks: int

    @property
    def complete(self) -> bool:
        return self.total_chunks == self.covered_chunks


def _generation_view(data: dict[str, Any]) -> EmbeddingGenerationView:
    return EmbeddingGenerationView(
        generation_id=data["generation_id"],
        tenant_id=data["tenant_id"],
        embedding_space_id=data["embedding_space_id"],
        generation_version=int(data["generation_version"]),
        index_name=data["index_name"],
        label_name=data["label_name"],
        dimensions=int(data["dimensions"]),
        similarity=data["similarity"],
        state=data["state"],
        corpus_revision=(
            None
            if data.get("corpus_revision") is None
            else int(data["corpus_revision"])
        ),
    )


def _derived_identifiers(generation_id: str) -> tuple[str, str]:
    token = generation_id.replace("-", "")
    label = f"EmbeddingGeneration_{token[:24]}"
    index = f"graphrag_vec_{token[:24]}"
    if not _SAFE_IDENTIFIER.fullmatch(label) or not _SAFE_IDENTIFIER.fullmatch(index):
        raise ValueError("derived vector index identifiers are invalid")
    return label, index


class Neo4jEmbeddingIndexManager:
    """Prepare/backfill one vector space and atomically select it per tenant."""

    def __init__(
        self,
        driver: Any,
        database: str = "neo4j",
        *,
        failpoint: Callable[[Checkpoint, dict[str, Any]], None] | None = None,
    ) -> None:
        self.driver = driver
        self.database = database
        self.failpoint = failpoint or (lambda checkpoint, context: None)

    def prepare(
        self,
        *,
        tenant_id: str,
        embedding_profile: ChunkEmbedding,
        generation_version: int,
    ) -> EmbeddingGenerationView:
        tenant_id = tenant_id.strip()
        if not tenant_id:
            raise ValueError("tenant_id must not be empty")
        if embedding_profile.tenant_id != tenant_id:
            raise ValueError("embedding profile tenant does not match")
        generation_id = embedding_index_generation_id(
            tenant_id,
            embedding_profile.embedding_space_id,
            generation_version,
        )
        label_name, index_name = _derived_identifiers(generation_id)
        now = datetime.now(UTC)
        properties = {
            "generation_id": generation_id,
            "tenant_id": tenant_id,
            "embedding_space_id": embedding_profile.embedding_space_id,
            "generation_version": generation_version,
            "index_name": index_name,
            "label_name": label_name,
            "dimensions": embedding_profile.dimensions,
            "similarity": "cosine",
        }
        with self.driver.session(database=self.database) as session:
            session.execute_write(self._ensure_generation_tx, properties, now)

        # Identifiers are UUID-derived and validated above; no user-provided
        # string is interpolated into Cypher schema/label positions.
        self.driver.execute_query(
            f"""
            CREATE VECTOR INDEX {index_name} IF NOT EXISTS
            FOR (embedding:{label_name}) ON (embedding.vector)
            OPTIONS {{indexConfig: {{
                `vector.dimensions`: {embedding_profile.dimensions},
                `vector.similarity_function`: 'cosine'
            }}}}
            """,
            database_=self.database,
        )
        self.driver.execute_query(
            "CALL db.awaitIndex($index_name, $timeout_seconds)",
            index_name=index_name,
            timeout_seconds=300,
            database_=self.database,
        )
        self._verify_index(index_name, label_name, embedding_profile.dimensions)
        with self.driver.session(database=self.database) as session:
            session.execute_write(
                self._finalize_prepare_tx,
                generation_id,
                tenant_id,
                embedding_profile.embedding_space_id,
                label_name,
                embedding_profile.dimensions,
                datetime.now(UTC),
            )
        return self.get_generation(generation_id)

    def materialize(self, embeddings: tuple[ChunkEmbedding, ...]) -> int:
        """Persist supplied vectors for active chunks before preparing a generation."""
        if not embeddings:
            return 0
        self._validate_materialization_batch(embeddings)
        with self.driver.session(database=self.database) as session:
            return int(
                session.execute_write(
                    self._materialize_tx,
                    embeddings,
                    self.failpoint,
                    datetime.now(UTC),
                    None,
                    None,
                )
            )

    def materialize_if_snapshot_active(
        self,
        embeddings: tuple[ChunkEmbedding, ...],
        *,
        snapshot_id: str,
        source_generation: int,
    ) -> int:
        """Backfill vectors only while their exact source snapshot is active.

        This conditional form is used by replayable pipeline jobs.  A source
        superseded or deleted after graph publication makes the backfill an
        atomic no-op instead of turning an already-terminal upsert into a
        failure.  The generic ``materialize`` method remains fail-closed for
        explicit migrations targeting inactive chunks.
        """
        if not embeddings:
            return 0
        snapshot_id = snapshot_id.strip()
        if not snapshot_id:
            raise ValueError("snapshot_id must not be empty")
        if (
            isinstance(source_generation, bool)
            or not isinstance(source_generation, int)
            or source_generation < 0
        ):
            raise ValueError("source_generation must be a non-negative integer")
        self._validate_materialization_batch(embeddings)
        with self.driver.session(database=self.database) as session:
            return int(
                session.execute_write(
                    self._materialize_tx,
                    embeddings,
                    self.failpoint,
                    datetime.now(UTC),
                    snapshot_id,
                    source_generation,
                )
            )

    @staticmethod
    def _validate_materialization_batch(
        embeddings: tuple[ChunkEmbedding, ...],
    ) -> None:
        first = embeddings[0]
        profile = (
            first.tenant_id,
            first.embedding_space_id,
            first.provider,
            first.model,
            first.revision,
            first.dimensions,
            first.normalization,
        )
        if any(
            (
                item.tenant_id,
                item.embedding_space_id,
                item.provider,
                item.model,
                item.revision,
                item.dimensions,
                item.normalization,
            )
            != profile
            for item in embeddings[1:]
        ):
            raise ValueError("embedding migration batch must use one vector space")
        if any(not item.vector for item in embeddings):
            raise ValueError("embedding migration requires materialized vectors")
        if len({item.embedding_id for item in embeddings}) != len(embeddings):
            raise ValueError("embedding migration contains duplicate IDs")

    def activate(
        self,
        generation_id: str,
        *,
        expected_active_generation_id: str | None,
    ) -> EmbeddingGenerationView:
        target = self.get_generation(generation_id)
        if target.state not in {"READY", "ACTIVE"}:
            raise IngestionConflict("embedding generation is not ready")
        with self.driver.session(database=self.database) as session:
            session.execute_write(
                self._activate_tx,
                target,
                expected_active_generation_id,
                datetime.now(UTC),
            )
        return self.get_generation(generation_id)

    def coverage(self, generation_id: str) -> EmbeddingCoverage:
        target = self.get_generation(generation_id)
        with self.driver.session(database=self.database) as session:
            record = session.run(
                f"""
                MATCH (document:Document {{tenant_id: $tenant_id}})
                      -[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot)
                      -[:INCLUDES_CHUNK]->(chunk:Chunk)
                OPTIONAL MATCH (chunk)-[:HAS_EMBEDDING]->(
                    embedding:ChunkEmbedding:{target.label_name}
                )
                WHERE embedding.tenant_id = $tenant_id
                  AND embedding.embedding_space_id = $embedding_space_id
                  AND embedding.cosine_indexable = true
                  AND embedding.vector IS NOT NULL
                  AND size(embedding.vector) = $dimensions
                  AND any(value IN embedding.vector WHERE value <> 0.0)
                RETURN count(DISTINCT chunk) AS total,
                       count(DISTINCT CASE
                           WHEN embedding IS NOT NULL THEN chunk
                       END) AS covered
                """,
                tenant_id=target.tenant_id,
                embedding_space_id=target.embedding_space_id,
                dimensions=target.dimensions,
            ).single()
        return EmbeddingCoverage(int(record["total"]), int(record["covered"]))

    def active_generation(self, tenant_id: str) -> EmbeddingGenerationView | None:
        with self.driver.session(database=self.database) as session:
            record = session.run(
                """
                MATCH (:TenantCorpusState {tenant_id: $tenant_id})
                      -[:ACTIVE_EMBEDDING_INDEX]->(
                          generation:EmbeddingIndexGeneration
                      )
                RETURN generation{.*} AS generation
                """,
                tenant_id=tenant_id,
            ).single()
        return (
            None
            if record is None
            else _generation_view(dict(record["generation"]))
        )

    def get_generation(self, generation_id: str) -> EmbeddingGenerationView:
        with self.driver.session(database=self.database) as session:
            record = session.run(
                """
                MATCH (generation:EmbeddingIndexGeneration {
                    generation_id: $generation_id
                })
                RETURN generation{.*} AS generation
                """,
                generation_id=generation_id,
            ).single()
        if record is None:
            raise KeyError(f"unknown embedding generation: {generation_id}")
        return _generation_view(dict(record["generation"]))

    @staticmethod
    def _ensure_generation_tx(
        tx: Any,
        properties: dict[str, Any],
        now: datetime,
    ) -> None:
        record = tx.run(
            """
            MERGE (generation:EmbeddingIndexGeneration {
                generation_id: $generation_id
            })
            ON CREATE SET generation = $properties,
                          generation.state = 'BUILDING',
                          generation.created_at = $now
            RETURN all(
                key IN keys($properties)
                WHERE generation[key] = $properties[key]
            ) AS compatible
            """,
            generation_id=properties["generation_id"],
            properties=properties,
            now=now,
        ).single()
        if record is None or not record["compatible"]:
            raise IngestionConflict("embedding generation identity conflicts")

    @staticmethod
    def _finalize_prepare_tx(
        tx: Any,
        generation_id: str,
        tenant_id: str,
        embedding_space_id: str,
        label_name: str,
        dimensions: int,
        now: datetime,
    ) -> None:
        """Seal exact active-corpus label membership under the corpus mutex."""
        Neo4jIngestionService._lock_tenant_corpus_state_tx(tx, tenant_id, now)
        generation = tx.run(
            """
            MATCH (generation:EmbeddingIndexGeneration {
                generation_id: $generation_id,
                tenant_id: $tenant_id
            })
            RETURN generation.state AS state
            """,
            generation_id=generation_id,
            tenant_id=tenant_id,
        ).single()
        if generation is None:
            raise IngestionConflict("embedding generation disappeared during prepare")
        if generation["state"] == "ACTIVE":
            # Activation already rebuilt exact membership while holding this
            # same lock.  A late/repeated prepare must not bulk-label staging
            # vectors into the live index.
            return

        tx.run(
            f"""
            MATCH (embedding:ChunkEmbedding:{label_name} {{tenant_id: $tenant_id}})
            REMOVE embedding:{label_name}
            """,
            tenant_id=tenant_id,
        ).consume()
        tx.run(
            f"""
            MATCH (document:Document {{tenant_id: $tenant_id}})
                  -[:ACTIVE_SNAPSHOT]->(:KnowledgeSnapshot)
                  -[:INCLUDES_CHUNK]->(chunk:Chunk)
                  -[:HAS_EMBEDDING]->(embedding:ChunkEmbedding {{
                      tenant_id: $tenant_id,
                      embedding_space_id: $embedding_space_id
                  }})
            WHERE embedding.chunk_id = chunk.chunk_id
              AND embedding.cosine_indexable = true
              AND embedding.vector IS NOT NULL
              AND size(embedding.vector) = $dimensions
              AND any(value IN embedding.vector WHERE value <> 0.0)
            SET embedding:{label_name}
            """,
            tenant_id=tenant_id,
            embedding_space_id=embedding_space_id,
            dimensions=dimensions,
        ).consume()
        tx.run(
            """
            MATCH (generation:EmbeddingIndexGeneration {
                generation_id: $generation_id,
                tenant_id: $tenant_id
            })
            SET generation.state = 'READY',
                generation.ready_at = coalesce(generation.ready_at, $now),
                generation.updated_at = $now
            """,
            generation_id=generation_id,
            tenant_id=tenant_id,
            now=now,
        ).consume()

    @staticmethod
    def _materialize_tx(
        tx: Any,
        embeddings: tuple[ChunkEmbedding, ...],
        failpoint: Callable[[Checkpoint, dict[str, Any]], None],
        now: datetime,
        required_snapshot_id: str | None,
        required_source_generation: int | None,
    ) -> int:
        tenant_id = embeddings[0].tenant_id
        Neo4jIngestionService._lock_tenant_corpus_state_tx(tx, tenant_id, now)
        if required_snapshot_id is not None:
            active = tx.run(
                """
                MATCH (document:Document {
                    tenant_id: $tenant_id,
                    generation: $source_generation
                })-[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot {
                    tenant_id: $tenant_id,
                    snapshot_id: $snapshot_id
                })
                RETURN snapshot.snapshot_id AS snapshot_id
                """,
                tenant_id=tenant_id,
                source_generation=required_source_generation,
                snapshot_id=required_snapshot_id,
            ).single()
            if active is None:
                return 0
        for embedding in embeddings:
            active = tx.run(
                """
                MATCH (:Document {tenant_id: $tenant_id})
                      -[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot)
                      -[:INCLUDES_CHUNK]->(chunk:Chunk {
                          chunk_id: $chunk_id,
                          tenant_id: $tenant_id
                      })
                WHERE $required_snapshot_id IS NULL
                   OR snapshot.snapshot_id = $required_snapshot_id
                RETURN chunk.chunk_id AS chunk_id
                """,
                tenant_id=embedding.tenant_id,
                chunk_id=embedding.chunk_id,
                required_snapshot_id=required_snapshot_id,
            ).single()
            if active is None:
                raise IngestionConflict(
                    "embedding migration target is not an active tenant chunk"
                )
            failpoint(
                Checkpoint.AFTER_EMBEDDING_MEMBERSHIP_CHECK,
                {
                    "tenant_id": embedding.tenant_id,
                    "chunk_id": embedding.chunk_id,
                },
            )
            identity = {
                "embedding_id": embedding.embedding_id,
                "tenant_id": embedding.tenant_id,
                "chunk_id": embedding.chunk_id,
                "embedding_space_id": embedding.embedding_space_id,
                "provider": embedding.provider,
                "model": embedding.model,
                "revision": embedding.revision,
                "dimensions": embedding.dimensions,
                "normalization": embedding.normalization,
            }
            Neo4jProvenanceStore._merge_node(
                tx,
                "ChunkEmbedding",
                "embedding_id",
                embedding.embedding_id,
                identity,
                on_create_properties={"created_at": embedding.created_at},
            )
            record = tx.run(
                """
                MATCH (embedding:ChunkEmbedding {embedding_id: $embedding_id})
                SET embedding.__vector_write_lock = randomUUID()
                WITH embedding
                REMOVE embedding.__vector_write_lock
                RETURN embedding.vector_checksum AS vector_checksum
                """,
                embedding_id=embedding.embedding_id,
            ).single()
            if record["vector_checksum"] not in (None, embedding.vector_checksum):
                raise IngestionConflict("immutable embedding vector conflicts")
            linked = tx.run(
                """
                MATCH (chunk:Chunk {chunk_id: $chunk_id, tenant_id: $tenant_id})
                MATCH (embedding:ChunkEmbedding {embedding_id: $embedding_id})
                SET embedding.vector = coalesce(embedding.vector, $vector),
                    embedding.vector_checksum = coalesce(
                        embedding.vector_checksum,
                        $vector_checksum
                    ),
                    embedding.cosine_indexable = true
                MERGE (chunk)-[:HAS_EMBEDDING]->(embedding)
                RETURN chunk.chunk_id AS chunk_id
                """,
                chunk_id=embedding.chunk_id,
                tenant_id=embedding.tenant_id,
                embedding_id=embedding.embedding_id,
                vector=list(embedding.vector),
                vector_checksum=embedding.vector_checksum,
            ).single()
            if linked is None:
                raise IngestionConflict(
                    "embedding migration target disappeared before vector link"
                )
        return len(embeddings)

    def _activate_tx(
        self,
        tx: Any,
        target: EmbeddingGenerationView,
        expected_active_generation_id: str | None,
        now: datetime,
    ) -> None:
        state_record = tx.run(
            """
            MERGE (state:TenantCorpusState {tenant_id: $tenant_id})
            ON CREATE SET state.corpus_revision = 0, state.created_at = $now
            SET state.__embedding_switch_lock = randomUUID()
            WITH state
            REMOVE state.__embedding_switch_lock
            WITH state
            OPTIONAL MATCH (state)-[:ACTIVE_EMBEDDING_INDEX]->(
                active:EmbeddingIndexGeneration
            )
            RETURN state.corpus_revision AS corpus_revision,
                   active.generation_id AS active_generation_id,
                   coalesce(
                       state.embedding_generation_version,
                       active.generation_version
                   ) AS active_generation_version
            """,
            tenant_id=target.tenant_id,
            now=now,
        ).single()
        current_id = state_record["active_generation_id"]
        if current_id == target.generation_id:
            return
        if current_id != expected_active_generation_id:
            raise IngestionConflict("active embedding generation CAS failed")
        current_version = state_record["active_generation_version"]
        if current_version is not None and target.generation_version < current_version:
            raise IngestionConflict("embedding generation cannot roll back")

        # Refresh exact membership while holding the same tenant state lock used
        # by snapshot publication. No newly published Chunk can escape coverage.
        tx.run(
            f"""
            MATCH (embedding:ChunkEmbedding:{target.label_name} {{
                tenant_id: $tenant_id
            }})
            REMOVE embedding:{target.label_name}
            """,
            tenant_id=target.tenant_id,
        ).consume()
        tx.run(
            f"""
            MATCH (document:Document {{tenant_id: $tenant_id}})
                  -[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot)
                  -[:INCLUDES_CHUNK]->(chunk:Chunk)
            MATCH (chunk)-[:HAS_EMBEDDING]->(embedding:ChunkEmbedding {{
                tenant_id: $tenant_id,
                embedding_space_id: $embedding_space_id
            }})
            WHERE embedding.chunk_id = chunk.chunk_id
              AND embedding.cosine_indexable = true
              AND embedding.vector IS NOT NULL
              AND size(embedding.vector) = $dimensions
              AND any(value IN embedding.vector WHERE value <> 0.0)
            SET embedding:{target.label_name}
            """,
            tenant_id=target.tenant_id,
            embedding_space_id=target.embedding_space_id,
            dimensions=target.dimensions,
        ).consume()
        coverage = tx.run(
            f"""
            MATCH (document:Document {{tenant_id: $tenant_id}})
                  -[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot)
                  -[:INCLUDES_CHUNK]->(chunk:Chunk)
            OPTIONAL MATCH (chunk)-[:HAS_EMBEDDING]->(
                embedding:ChunkEmbedding:{target.label_name}
            )
            WHERE embedding.chunk_id = chunk.chunk_id
              AND embedding.embedding_space_id = $embedding_space_id
              AND embedding.cosine_indexable = true
              AND embedding.vector IS NOT NULL
              AND size(embedding.vector) = $dimensions
              AND any(value IN embedding.vector WHERE value <> 0.0)
            RETURN count(DISTINCT chunk) AS total,
                   count(DISTINCT CASE
                       WHEN embedding IS NOT NULL THEN chunk
                   END) AS covered
            """,
            tenant_id=target.tenant_id,
            embedding_space_id=target.embedding_space_id,
            dimensions=target.dimensions,
        ).single()
        if coverage["total"] != coverage["covered"]:
            raise IngestionConflict("embedding generation coverage is incomplete")

        tx.run(
            """
            MATCH (state:TenantCorpusState {tenant_id: $tenant_id})
            MATCH (target:EmbeddingIndexGeneration {generation_id: $generation_id})
            OPTIONAL MATCH (state)-[old_pointer:ACTIVE_EMBEDDING_INDEX]->(
                old:EmbeddingIndexGeneration
            )
            DELETE old_pointer
            WITH DISTINCT state, target, old
            MERGE (state)-[:ACTIVE_EMBEDDING_INDEX]->(target)
            SET state.embedding_generation_version = target.generation_version,
                target.state = 'ACTIVE',
                target.activated_at = $now,
                target.corpus_revision = state.corpus_revision,
                target.updated_at = $now
            FOREACH (_ IN CASE
                WHEN old IS NOT NULL AND old <> target THEN [1]
                ELSE []
            END | SET old.state = 'RETIRED', old.retired_at = $now)
            """,
            tenant_id=target.tenant_id,
            generation_id=target.generation_id,
            now=now,
        ).consume()

    def _verify_index(
        self,
        index_name: str,
        label_name: str,
        dimensions: int,
    ) -> None:
        records, _, _ = self.driver.execute_query(
            """
            SHOW INDEXES YIELD name, type, entityType, labelsOrTypes,
                               properties, state, options
            WHERE name = $index_name
            RETURN name, type, entityType, labelsOrTypes, properties, state, options
            """,
            index_name=index_name,
            database_=self.database,
        )
        if len(records) != 1:
            raise IngestionConflict("vector index was not created")
        record = records[0]
        shape = (
            record["type"],
            record["entityType"],
            tuple(record["labelsOrTypes"]),
            tuple(record["properties"]),
            record["state"],
        )
        expected = (
            "VECTOR",
            "NODE",
            (label_name,),
            ("vector",),
            "ONLINE",
        )
        if shape != expected:
            raise IngestionConflict(
                f"vector index shape mismatch: expected {expected}, got {shape}"
            )
        index_config = dict(record["options"].get("indexConfig", {}))
        actual_dimensions = index_config.get("vector.dimensions")
        if actual_dimensions is not None and int(actual_dimensions) != dimensions:
            raise IngestionConflict("vector index dimensions do not match generation")
        actual_similarity = index_config.get("vector.similarity_function")
        if actual_similarity is None or str(actual_similarity).lower() != "cosine":
            raise IngestionConflict("vector index similarity does not match generation")
