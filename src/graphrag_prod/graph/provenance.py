"""Neo4j adapter for immutable, access-controlled provenance records."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Protocol

from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import (
    assertion_id as make_assertion_id,
    canonicalize_uri,
    chunk_embedding_id as make_chunk_embedding_id,
    chunk_id as make_chunk_id,
    document_id as make_document_id,
    entity_id as make_entity_id,
    mention_id as make_mention_id,
    version_id as make_version_id,
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


class SessionDriver(Protocol):
    def session(self, **kwargs: object) -> Any: ...


def _contains_exact_token(evidence: str, token: str) -> bool:
    start = 0
    while True:
        index = evidence.find(token, start)
        if index < 0:
            return False
        end = index + len(token)
        left_ok = (
            not token[0].isalnum()
            or index == 0
            or not (evidence[index - 1].isalnum() or evidence[index - 1] == "_")
        )
        right_ok = (
            not token[-1].isalnum()
            or end == len(evidence)
            or not (evidence[end].isalnum() or evidence[end] == "_")
        )
        if left_ok and right_ok:
            return True
        start = index + 1


@dataclass(frozen=True, slots=True)
class ProvenanceBundle:
    document: Document
    version: DocumentVersion
    chunk: Chunk
    embedding: ChunkEmbedding | None
    entities: tuple[Entity, ...]
    mentions: tuple[EntityMention, ...]
    assertion: Assertion | None
    activate_version: bool = True
    additional_embeddings: tuple[ChunkEmbedding, ...] = ()
    additional_assertions: tuple[Assertion, ...] = ()

    @property
    def all_embeddings(self) -> tuple[ChunkEmbedding, ...]:
        primary = () if self.embedding is None else (self.embedding,)
        return (*primary, *self.additional_embeddings)

    @property
    def all_assertions(self) -> tuple[Assertion, ...]:
        primary = () if self.assertion is None else (self.assertion,)
        return (*primary, *self.additional_assertions)

    def __post_init__(self) -> None:
        embeddings = self.all_embeddings
        assertions = self.all_assertions
        tenant_ids = {
            self.document.tenant_id,
            self.version.tenant_id,
            self.chunk.tenant_id,
            *(embedding.tenant_id for embedding in embeddings),
            *(assertion.tenant_id for assertion in assertions),
            *(entity.tenant_id for entity in self.entities),
            *(mention.tenant_id for mention in self.mentions),
        }
        if len(tenant_ids) != 1:
            raise ValueError("all provenance records must belong to one tenant")
        if self.version.document_id != self.document.document_id:
            raise ValueError("version must belong to the supplied document")
        if self.chunk.version_id != self.version.version_id:
            raise ValueError("chunk must belong to the supplied version")
        if self.chunk.document_id != self.document.document_id:
            raise ValueError("chunk document_id does not match the document")
        if len({embedding.embedding_id for embedding in embeddings}) != len(embeddings):
            raise ValueError("bundle contains duplicate embedding IDs")
        if len({assertion.assertion_id for assertion in assertions}) != len(assertions):
            raise ValueError("bundle contains duplicate assertion IDs")
        for embedding in embeddings:
            if embedding.chunk_id != self.chunk.chunk_id:
                raise ValueError("embedding must belong to the supplied chunk")
        if not self.chunk.access_groups <= self.document.access_groups:
            raise ValueError("chunk access cannot be broader than document access")
        if (
            self.chunk.access_policy_id != self.document.access_policy_id
            or self.chunk.access_policy_version != self.document.access_policy_version
        ):
            raise ValueError("chunk and document access policy versions must match")
        if not 0 <= self.chunk.char_start < self.chunk.char_end <= len(
            self.version.normalized_text
        ):
            raise ValueError("chunk range is outside the normalized source text")
        if (
            self.version.normalized_text[self.chunk.char_start : self.chunk.char_end]
            != self.chunk.text
        ):
            raise ValueError("chunk text does not match its source range")

        if canonicalize_uri(self.document.canonical_uri) != self.document.canonical_uri:
            raise ValueError("document canonical_uri is not canonical")
        if (
            make_document_id(self.document.tenant_id, self.document.canonical_uri)
            != self.document.document_id
        ):
            raise ValueError("document_id does not match its identity inputs")
        if (
            make_version_id(
                self.document.document_id,
                self.version.checksum,
                self.version.original_checksum,
            )
            != self.version.version_id
        ):
            raise ValueError("version_id does not match its identity inputs")
        if (
            make_chunk_id(
                self.version.version_id,
                self.chunk.splitter_version,
                self.chunk.ordinal,
                self.chunk.char_start,
                self.chunk.char_end,
                self.chunk.checksum,
            )
            != self.chunk.chunk_id
        ):
            raise ValueError("chunk_id does not match its identity inputs")
        for embedding in embeddings:
            if (
                make_chunk_embedding_id(
                    embedding.chunk_id,
                    embedding.embedding_space_id,
                )
                != embedding.embedding_id
            ):
                raise ValueError("embedding_id does not match its identity inputs")

        entities_by_id = {entity.entity_id: entity for entity in self.entities}
        if len(entities_by_id) != len(self.entities):
            raise ValueError("bundle contains duplicate entity IDs")
        for entity in self.entities:
            if (
                make_entity_id(
                    entity.tenant_id,
                    entity.entity_type,
                    entity.canonical_key,
                )
                != entity.entity_id
            ):
                raise ValueError("entity_id does not match its identity inputs")

        for mention in self.mentions:
            entity = entities_by_id.get(mention.entity_id)
            if mention.chunk_id != self.chunk.chunk_id:
                raise ValueError("mention must belong to the supplied chunk")
            if entity is None:
                raise ValueError("mention entity is absent from the bundle")
            if mention.entity_type != entity.entity_type:
                raise ValueError("mention and entity types do not match")
            if not self.chunk.char_start <= mention.char_start < mention.char_end <= self.chunk.char_end:
                raise ValueError("mention range is outside the chunk")
            if (
                self.version.normalized_text[mention.char_start : mention.char_end]
                != mention.surface
            ):
                raise ValueError("mention surface does not match source text")
            if (
                make_mention_id(
                    mention.chunk_id,
                    mention.entity_type,
                    mention.char_start,
                    mention.char_end,
                    mention.surface,
                    mention.extractor_version,
                )
                != mention.mention_id
            ):
                raise ValueError("mention_id does not match its identity inputs")

        mentioned_entity_ids = {mention.entity_id for mention in self.mentions}
        if mentioned_entity_ids != set(entities_by_id):
            raise ValueError("every derived entity requires a mention in the evidence chunk")
        for assertion in assertions:
            if assertion.subject_entity_id not in entities_by_id:
                raise ValueError("assertion subject is absent from the bundle")
            if (
                assertion.object_entity_id is not None
                and assertion.object_entity_id not in entities_by_id
            ):
                raise ValueError("assertion object is absent from the bundle")
            if assertion.evidence_chunk_id != self.chunk.chunk_id:
                raise ValueError("bundle chunk must be assertion evidence")
            if not (
                self.chunk.char_start
                <= assertion.evidence_char_start
                < assertion.evidence_char_end
                <= self.chunk.char_end
            ):
                raise ValueError("assertion evidence range is outside the chunk")

            required_endpoint_ids = {assertion.subject_entity_id}
            if assertion.object_entity_id is not None:
                required_endpoint_ids.add(assertion.object_entity_id)
            if not required_endpoint_ids <= mentioned_entity_ids:
                raise ValueError("assertion endpoints require entity mentions")
            for endpoint_id in required_endpoint_ids:
                if not any(
                    mention.entity_id == endpoint_id
                    and assertion.evidence_char_start <= mention.char_start
                    and mention.char_end <= assertion.evidence_char_end
                    for mention in self.mentions
                ):
                    raise ValueError("assertion endpoint mention is outside evidence span")
            evidence_text = self.version.normalized_text[
                assertion.evidence_char_start : assertion.evidence_char_end
            ]
            if (
                assertion.literal_value is not None
                and not _contains_exact_token(
                    evidence_text,
                    assertion.literal_value,
                )
            ):
                raise ValueError("literal assertion object is absent from its evidence span")
            if assertion.literal_semantics is not None:
                exact_tokens = (
                    assertion.literal_semantics.raw_value,
                    assertion.literal_semantics.raw_unit,
                    assertion.literal_semantics.raw_valid_from,
                    assertion.literal_semantics.raw_valid_to,
                    assertion.literal_semantics.raw_observed_at,
                )
                if any(
                    token is not None
                    and not _contains_exact_token(evidence_text, token)
                    for token in exact_tokens
                ):
                    raise ValueError(
                        "typed literal source tokens are absent from its evidence span"
                    )
            for property_value in assertion.relationship_properties:
                property_evidence = self.version.normalized_text[
                    property_value.evidence_char_start :
                    property_value.evidence_char_end
                ]
                if property_evidence != property_value.evidence_text:
                    raise ValueError(
                        "relationship property evidence does not match source text"
                    )

            object_kind = (
                "entity" if assertion.object_entity_id is not None else "literal"
            )
            object_reference = assertion.object_reference
            if (
                make_assertion_id(
                    assertion.tenant_id,
                    assertion.subject_entity_id,
                    assertion.predicate,
                    object_kind,
                    object_reference,
                    assertion.evidence_chunk_id,
                    assertion.evidence_char_start,
                    assertion.evidence_char_end,
                    assertion.extractor_version,
                    assertion.schema_version,
                )
                != assertion.assertion_id
            ):
                raise ValueError("assertion_id does not match its identity inputs")


@dataclass(frozen=True, slots=True)
class EvidenceView:
    assertion_id: str
    predicate: str
    subject_entity_id: str
    object_reference: str
    evidence_char_start: int
    evidence_char_end: int
    chunk_id: str
    chunk_checksum: str
    text: str
    char_start: int
    char_end: int
    page_number: int | None
    section: str | None
    version_id: str
    version_checksum: str
    version_number: int
    document_id: str
    canonical_uri: str
    source_name: str


def _properties(**values: Any) -> dict[str, Any]:
    """Neo4j maps omit None because assigning null removes a property."""
    return {key: value for key, value in values.items() if value is not None}


class Neo4jProvenanceStore:
    """Persist immutable provenance and read evidence through real graph paths."""

    def __init__(self, driver: SessionDriver, database: str = "neo4j") -> None:
        self.driver = driver
        self.database = database

    def write_bundle(self, bundle: ProvenanceBundle) -> None:
        with self.driver.session(database=self.database) as session:
            session.execute_write(self._write_bundle_tx, bundle)

    @staticmethod
    def _merge_node(
        tx: Any,
        label: str,
        id_property: str,
        identifier: str,
        immutable_properties: dict[str, Any],
        mutable_properties: dict[str, Any] | None = None,
        versioned_properties: dict[str, Any] | None = None,
        version_property: str | None = None,
        update_mutable: bool = True,
        on_create_properties: dict[str, Any] | None = None,
    ) -> None:
        if immutable_properties.get(id_property) != identifier:
            raise ValueError(f"{label} properties do not contain their stable ID")
        mutable_properties = mutable_properties or {}
        versioned_properties = versioned_properties or {}
        on_create_properties = on_create_properties or {}
        if bool(versioned_properties) != bool(version_property):
            raise ValueError(
                "versioned_properties and version_property must be supplied together"
            )
        if version_property and version_property not in versioned_properties:
            raise ValueError("versioned state does not contain its version property")

        all_properties = {
            **immutable_properties,
            **mutable_properties,
            **versioned_properties,
            **on_create_properties,
        }
        if versioned_properties:
            # The transient write takes a Neo4j node lock before the current
            # policy snapshot is read. The lock is held until this transaction
            # commits, so Document and Chunk policy changes form one atomic CAS.
            record = tx.run(
                f"""
                MERGE (node:{label} {{{id_property}: $identifier}})
                ON CREATE SET node = $all_properties
                WITH node
                SET node.__state_write_lock = randomUUID()
                WITH node,
                     all(
                         key IN keys($immutable_properties)
                         WHERE node[key] = $immutable_properties[key]
                     ) AS compatible,
                     node[$version_property] AS current_version,
                     all(
                         key IN keys($versioned_properties)
                         WHERE node[key] = $versioned_properties[key]
                     ) AS same_versioned_state
                REMOVE node.__state_write_lock
                RETURN compatible, current_version, same_versioned_state
                """,
                identifier=identifier,
                all_properties=all_properties,
                immutable_properties=immutable_properties,
                versioned_properties=versioned_properties,
                version_property=version_property,
            ).single()
        else:
            record = tx.run(
                f"""
                MERGE (node:{label} {{{id_property}: $identifier}})
                ON CREATE SET node = $all_properties
                RETURN all(
                    key IN keys($immutable_properties)
                    WHERE node[key] = $immutable_properties[key]
                ) AS compatible
                """,
                identifier=identifier,
                all_properties=all_properties,
                immutable_properties=immutable_properties,
            ).single()
        if record is None or not record["compatible"]:
            raise ValueError(f"immutable {label} conflicts with existing stable ID")

        if versioned_properties:
            incoming_version = versioned_properties[version_property]
            current_version = record["current_version"]
            if incoming_version < current_version:
                raise ValueError(f"stale {label} {version_property}")
            if incoming_version == current_version:
                if not record["same_versioned_state"]:
                    raise ValueError(
                        f"conflicting {label} state at {version_property} "
                        f"{incoming_version}"
                    )
            else:
                tx.run(
                    f"""
                    MATCH (node:{label} {{{id_property}: $identifier}})
                    SET node += $versioned_properties
                    """,
                    identifier=identifier,
                    versioned_properties=versioned_properties,
                ).consume()
        if mutable_properties and update_mutable:
            tx.run(
                f"""
                MATCH (node:{label} {{{id_property}: $identifier}})
                SET node += $mutable_properties
                """,
                identifier=identifier,
                mutable_properties=mutable_properties,
            ).consume()

    @classmethod
    def _write_bundle_tx(
        cls,
        tx: Any,
        bundle: ProvenanceBundle,
        staging_job_id: str | None = None,
    ) -> None:
        document = bundle.document
        version = bundle.version
        chunk = bundle.chunk

        if staging_job_id is None:
            # ``write_bundle`` is the Stage 2 compatibility writer.  It shares
            # the tenant corpus mutex so it cannot race the Stage 3 lifecycle,
            # and it permanently fails closed after managed ingestion starts.
            lifecycle = tx.run(
                """
                MERGE (state:TenantCorpusState {tenant_id: $tenant_id})
                ON CREATE SET state.corpus_revision = 0,
                              state.created_at = $now,
                              state.lifecycle_mode = 'LEGACY'
                SET state.__corpus_write_lock = randomUUID()
                WITH state
                REMOVE state.__corpus_write_lock
                WITH state
                OPTIONAL MATCH (snapshot:KnowledgeSnapshot {
                    tenant_id: $tenant_id,
                    document_id: $document_id
                })
                RETURN coalesce(
                           state.lifecycle_mode,
                           'MANAGED_INCREMENTAL'
                       ) AS lifecycle_mode,
                       count(snapshot) > 0 AS has_managed_snapshot
                """,
                tenant_id=document.tenant_id,
                document_id=document.document_id,
                now=version.ingested_at,
            ).single()
            if (
                lifecycle is None
                or lifecycle["lifecycle_mode"] != "LEGACY"
                or lifecycle["has_managed_snapshot"]
            ):
                raise ValueError(
                    "legacy write_bundle is disabled for a managed "
                    "incremental-ingestion tenant"
                )

        document_identity = _properties(
            document_id=document.document_id,
            tenant_id=document.tenant_id,
            canonical_uri=document.canonical_uri,
            source_name=document.source_name,
        )
        document_creation = _properties(created_at=document.created_at)
        document_profile = _properties(title=document.title)
        document_access = _properties(
            access_policy_id=document.access_policy_id,
            access_policy_version=document.access_policy_version,
            access_groups=sorted(document.access_groups),
        )
        if staging_job_id is None:
            cls._merge_node(
                tx,
                "Document",
                "document_id",
                document.document_id,
                document_identity,
                document_profile,
                document_access,
                "access_policy_version",
                on_create_properties=document_creation,
            )
        else:
            # Staging must not change the profile or ACL of the currently
            # published Document. Desired state is applied during publish.
            cls._merge_node(
                tx,
                "Document",
                "document_id",
                document.document_id,
                document_identity,
                on_create_properties=document_creation,
            )

        # A transient write obtains a lock on the logical document so two
        # concurrent publishers cannot both observe that no active version exists.
        active = tx.run(
            """
            MATCH (document:Document {document_id: $document_id})
            SET document.__provenance_write_lock = randomUUID()
            REMOVE document.__provenance_write_lock
            WITH document
            OPTIONAL MATCH (document)-[:ACTIVE_VERSION]->(active:DocumentVersion)
            WHERE active.version_id <> $version_id
            RETURN count(active) AS active_count
            """,
            document_id=document.document_id,
            version_id=version.version_id,
        ).single()
        if bundle.activate_version and active is not None and active["active_count"] > 0:
            raise ValueError("document already has a different active version")

        version_properties = _properties(
            version_id=version.version_id,
            document_id=version.document_id,
            tenant_id=version.tenant_id,
            checksum=version.checksum,
            original_checksum=version.original_checksum,
            normalized_text=version.normalized_text,
            version_number=version.version_number,
            mime_type=version.mime_type,
            language=version.language,
            published_at=version.published_at,
        )
        version_creation = _properties(
            ingested_at=version.ingested_at,
            first_ingested_at=version.ingested_at,
        )
        cls._merge_node(
            tx,
            "DocumentVersion",
            "version_id",
            version.version_id,
            version_properties,
            on_create_properties=version_creation,
        )
        tx.run(
            """
            MATCH (document:Document {document_id: $document_id})
            MATCH (version:DocumentVersion {version_id: $version_id})
            WHERE document.tenant_id = version.tenant_id
            MERGE (document)-[:HAS_VERSION]->(version)
            """,
            document_id=document.document_id,
            version_id=version.version_id,
        ).consume()
        if bundle.activate_version:
            tx.run(
                """
                MATCH (document:Document {document_id: $document_id})
                MATCH (version:DocumentVersion {version_id: $version_id})
                WHERE document.tenant_id = version.tenant_id
                MERGE (document)-[:ACTIVE_VERSION]->(version)
                """,
                document_id=document.document_id,
                version_id=version.version_id,
            ).consume()

        chunk_identity = _properties(
            chunk_id=chunk.chunk_id,
            version_id=chunk.version_id,
            document_id=chunk.document_id,
            tenant_id=chunk.tenant_id,
            ordinal=chunk.ordinal,
            text=chunk.text,
            checksum=chunk.checksum,
            char_start=chunk.char_start,
            char_end=chunk.char_end,
            page_number=chunk.page_number,
            section=chunk.section,
            splitter_version=chunk.splitter_version,
        )
        chunk_access = _properties(
            access_policy_id=chunk.access_policy_id,
            access_policy_version=chunk.access_policy_version,
            access_groups=sorted(chunk.access_groups),
        )
        if staging_job_id is None:
            cls._merge_node(
                tx,
                "Chunk",
                "chunk_id",
                chunk.chunk_id,
                chunk_identity,
                mutable_properties={"publication_state": "LEGACY_PUBLISHED"},
                versioned_properties=chunk_access,
                version_property="access_policy_version",
            )
        else:
            # A staged Chunk may be shared with the active snapshot when only
            # extraction changes. ACL state therefore moves at publish time.
            cls._merge_node(
                tx,
                "Chunk",
                "chunk_id",
                chunk.chunk_id,
                chunk_identity,
            )
        tx.run(
            """
            MATCH (version:DocumentVersion {version_id: $version_id})
            MATCH (chunk:Chunk {chunk_id: $chunk_id})
            WHERE version.tenant_id = chunk.tenant_id
              AND chunk.version_id = version.version_id
            MERGE (version)-[:HAS_CHUNK]->(chunk)
            """,
            version_id=version.version_id,
            chunk_id=chunk.chunk_id,
        ).consume()

        for embedding in bundle.all_embeddings:
            vector = tuple(getattr(embedding, "vector", ()))
            embedding_identity = _properties(
                embedding_id=embedding.embedding_id,
                tenant_id=embedding.tenant_id,
                chunk_id=embedding.chunk_id,
                embedding_space_id=embedding.embedding_space_id,
                provider=embedding.provider,
                model=embedding.model,
                revision=embedding.revision,
                dimensions=embedding.dimensions,
                normalization=embedding.normalization,
            )
            cls._merge_node(
                tx,
                "ChunkEmbedding",
                "embedding_id",
                embedding.embedding_id,
                embedding_identity,
                on_create_properties={"created_at": embedding.created_at},
            )
            if vector:
                vector_checksum = getattr(embedding, "vector_checksum", None)
                vector_record = tx.run(
                    """
                    MATCH (embedding:ChunkEmbedding {embedding_id: $embedding_id})
                    SET embedding.__vector_write_lock = randomUUID()
                    WITH embedding
                    REMOVE embedding.__vector_write_lock
                    RETURN embedding.vector_checksum AS current_checksum
                    """,
                    embedding_id=embedding.embedding_id,
                ).single()
                current_checksum = vector_record["current_checksum"]
                if current_checksum not in (None, vector_checksum):
                    raise ValueError("immutable ChunkEmbedding vector conflicts")
                if current_checksum is None:
                    tx.run(
                        """
                        MATCH (embedding:ChunkEmbedding {embedding_id: $embedding_id})
                        SET embedding.vector = $vector,
                            embedding.vector_checksum = $vector_checksum
                        """,
                        embedding_id=embedding.embedding_id,
                        vector=list(vector),
                        vector_checksum=vector_checksum,
                    ).consume()
                tx.run(
                    """
                    MATCH (embedding:ChunkEmbedding {embedding_id: $embedding_id})
                    SET embedding.cosine_indexable = true
                    """,
                    embedding_id=embedding.embedding_id,
                ).consume()
            tx.run(
                """
                MATCH (chunk:Chunk {chunk_id: $chunk_id})
                MATCH (embedding:ChunkEmbedding {embedding_id: $embedding_id})
                WHERE chunk.tenant_id = embedding.tenant_id
                  AND embedding.chunk_id = chunk.chunk_id
                MERGE (chunk)-[:HAS_EMBEDDING]->(embedding)
                """,
                chunk_id=chunk.chunk_id,
                embedding_id=embedding.embedding_id,
            ).consume()

        for entity in bundle.entities:
            entity_identity = _properties(
                entity_id=entity.entity_id,
                tenant_id=entity.tenant_id,
                entity_type=entity.entity_type,
                canonical_key=entity.canonical_key,
            )
            entity_profile = _properties(
                canonical_name=entity.canonical_name,
                aliases=list(entity.aliases),
            )
            if staging_job_id is None:
                cls._merge_node(
                    tx,
                    "Entity",
                    "entity_id",
                    entity.entity_id,
                    entity_identity,
                    entity_profile,
                )
            else:
                # Profile state from an unpublished/failed snapshot must not
                # become globally visible through a shared Entity identity.
                cls._merge_node(
                    tx,
                    "Entity",
                    "entity_id",
                    entity.entity_id,
                    entity_identity,
                )

        for mention in bundle.mentions:
            mention_properties = _properties(
                mention_id=mention.mention_id,
                tenant_id=mention.tenant_id,
                chunk_id=mention.chunk_id,
                entity_id=mention.entity_id,
                entity_type=mention.entity_type,
                surface=mention.surface,
                char_start=mention.char_start,
                char_end=mention.char_end,
                extractor_version=mention.extractor_version,
                confidence=mention.confidence,
            )
            cls._merge_node(
                tx,
                "EntityMention",
                "mention_id",
                mention.mention_id,
                mention_properties,
            )
            record = tx.run(
                """
                MATCH (mention:EntityMention {mention_id: $mention_id})
                MATCH (chunk:Chunk {chunk_id: $chunk_id})
                MATCH (entity:Entity {entity_id: $entity_id})
                WHERE mention.tenant_id = chunk.tenant_id
                  AND mention.tenant_id = entity.tenant_id
                  AND mention.chunk_id = chunk.chunk_id
                  AND mention.entity_id = entity.entity_id
                MERGE (mention)-[:IN_CHUNK]->(chunk)
                MERGE (mention)-[:REFERS_TO]->(entity)
                RETURN mention.mention_id AS mention_id
                """,
                mention_id=mention.mention_id,
                chunk_id=mention.chunk_id,
                entity_id=mention.entity_id,
            ).single()
            if record is None:
                raise ValueError("mention provenance path is invalid")

        for assertion in bundle.all_assertions:
            cls._write_assertion_tx(tx, assertion, staging_job_id)

    @classmethod
    def _write_assertion_tx(
        cls,
        tx: Any,
        assertion: Assertion,
        staging_job_id: str | None = None,
    ) -> None:
        object_kind = "entity" if assertion.object_entity_id else "literal"
        assertion_identity = _properties(
            assertion_id=assertion.assertion_id,
            tenant_id=assertion.tenant_id,
            subject_entity_id=assertion.subject_entity_id,
            object_entity_id=assertion.object_entity_id,
            predicate=assertion.predicate,
            object_kind=object_kind,
            # Keep the property present for one stable Cypher shape; entity
            # assertions use an empty sentinel that is ignored by object_kind.
            literal_value=assertion.literal_value or "",
            evidence_chunk_id=assertion.evidence_chunk_id,
            evidence_char_start=assertion.evidence_char_start,
            evidence_char_end=assertion.evidence_char_end,
            extractor_version=assertion.extractor_version,
            schema_version=assertion.schema_version,
            confidence=assertion.confidence,
        )
        if assertion.literal_semantics is not None:
            assertion_identity.update(
                assertion.literal_semantics.to_flat_properties()
            )
        assertion_identity.update(
            relationship_properties_format_version=1,
            relationship_properties_json=json.dumps(
                [item.to_mapping() for item in assertion.relationship_properties],
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        assertion_state = _properties(
            accepted=assertion.accepted,
            publication_state=(
                "LEGACY_PUBLISHED" if staging_job_id is None else None
            ),
        )
        cls._merge_node(
            tx,
            "Assertion",
            "assertion_id",
            assertion.assertion_id,
            assertion_identity,
            assertion_state,
            update_mutable=staging_job_id is None,
        )
        record = tx.run(
            """
            MATCH (assertion:Assertion {assertion_id: $assertion_id})
            MATCH (subject:Entity {entity_id: $subject_entity_id})
            WHERE assertion.tenant_id = subject.tenant_id
            MERGE (assertion)-[:SUBJECT]->(subject)
            RETURN assertion.assertion_id AS assertion_id
            """,
            assertion_id=assertion.assertion_id,
            subject_entity_id=assertion.subject_entity_id,
        ).single()
        if record is None:
            raise ValueError("assertion subject provenance is invalid")

        if assertion.object_entity_id:
            record = tx.run(
                """
                MATCH (assertion:Assertion {assertion_id: $assertion_id})
                MATCH (object:Entity {entity_id: $object_entity_id})
                WHERE assertion.tenant_id = object.tenant_id
                MERGE (assertion)-[:OBJECT]->(object)
                RETURN object.entity_id AS entity_id
                """,
                assertion_id=assertion.assertion_id,
                object_entity_id=assertion.object_entity_id,
            ).single()
            if record is None:
                raise ValueError("assertion object provenance is invalid")

        record = tx.run(
            """
            MATCH (assertion:Assertion {assertion_id: $assertion_id})
            MATCH (chunk:Chunk {chunk_id: $chunk_id})
            WHERE assertion.tenant_id = chunk.tenant_id
              AND assertion.evidence_chunk_id = chunk.chunk_id
            MERGE (assertion)-[:EVIDENCED_BY]->(chunk)
            RETURN chunk.chunk_id AS chunk_id
            """,
            assertion_id=assertion.assertion_id,
            chunk_id=assertion.evidence_chunk_id,
        ).single()
        if record is None:
            raise ValueError("assertion evidence is missing or cross-tenant")

    def get_assertion_evidence(
        self,
        principal: Principal,
        assertion_id: str,
    ) -> tuple[EvidenceView, ...]:
        """Return evidence only through an authorized provenance path."""
        with self.driver.session(database=self.database) as session:
            records = session.run(
                """
                MATCH (document:Document {
                    tenant_id: $tenant_id
                })-[:ACTIVE_VERSION]->(version:DocumentVersion {
                    tenant_id: $tenant_id
                })-[:HAS_CHUNK]->(chunk:Chunk {
                    tenant_id: $tenant_id
                })<-[:EVIDENCED_BY]-(assertion:Assertion {
                    assertion_id: $assertion_id,
                    tenant_id: $tenant_id
                })-[:SUBJECT]->(subject:Entity {tenant_id: $tenant_id})
                WHERE any(group IN document.access_groups WHERE group IN $groups)
                  AND any(group IN chunk.access_groups WHERE group IN $groups)
                  AND EXISTS {
                      MATCH (document)-[:HAS_VERSION]->(version)
                  }
                  AND version.document_id = document.document_id
                  AND chunk.document_id = document.document_id
                  AND chunk.version_id = version.version_id
                  AND chunk.access_policy_id = document.access_policy_id
                  AND chunk.access_policy_version = document.access_policy_version
                  AND coalesce(assertion.governance_status, 'ACCEPTED') IN
                      ['ACCEPTED', 'ACCEPTED_BY_REVIEW']
                  AND coalesce(subject.governance_status, 'ACCEPTED') IN
                      ['ACCEPTED', 'ACCEPTED_BY_REVIEW']
                  AND (
                      NOT EXISTS {
                          MATCH (document)-[:ACTIVE_SNAPSHOT]->(:KnowledgeSnapshot)
                      }
                      AND chunk.publication_state = 'LEGACY_PUBLISHED'
                      AND assertion.publication_state = 'LEGACY_PUBLISHED'
                      AND (
                          assertion.accepted = true
                          OR assertion.governance_status = 'ACCEPTED_BY_REVIEW'
                      )
                      OR EXISTS {
                          MATCH (document)-[:ACTIVE_SNAPSHOT]->(
                              snapshot:KnowledgeSnapshot
                          )-[assertion_membership:INCLUDES_ASSERTION]->(assertion)
                          WHERE EXISTS {
                              MATCH (snapshot)-[:OF_VERSION]->(version)
                          }
                            AND EXISTS {
                              MATCH (snapshot)-[:INCLUDES_CHUNK]->(chunk)
                          }
                            AND EXISTS {
                              MATCH (snapshot)-[:INCLUDES_ENTITY]->(subject)
                          }
                            AND (
                                assertion_membership.accepted = true
                                OR assertion.governance_status = 'ACCEPTED_BY_REVIEW'
                            )
                      }
                  )
                OPTIONAL MATCH (assertion)-[:OBJECT]->(object:Entity {
                    tenant_id: $tenant_id
                })
                WITH assertion, subject, object, chunk, version, document
                WHERE assertion.object_kind <> 'entity'
                   OR (object IS NOT NULL
                       AND coalesce(object.governance_status, 'ACCEPTED') IN
                           ['ACCEPTED', 'ACCEPTED_BY_REVIEW'])
                RETURN assertion.assertion_id AS assertion_id,
                       assertion.predicate AS predicate,
                       subject.entity_id AS subject_entity_id,
                       CASE assertion.object_kind
                           WHEN 'entity' THEN object.entity_id
                           ELSE assertion.literal_value
                       END AS object_reference,
                       assertion.evidence_char_start AS evidence_char_start,
                       assertion.evidence_char_end AS evidence_char_end,
                       chunk.chunk_id AS chunk_id,
                       chunk.checksum AS chunk_checksum,
                       chunk.text AS text,
                       chunk.char_start AS char_start,
                       chunk.char_end AS char_end,
                       chunk.page_number AS page_number,
                       chunk.section AS section,
                       version.version_id AS version_id,
                       version.checksum AS version_checksum,
                       version.version_number AS version_number,
                       document.document_id AS document_id,
                       document.canonical_uri AS canonical_uri,
                       document.source_name AS source_name
                ORDER BY chunk.ordinal
                """,
                assertion_id=assertion_id,
                tenant_id=principal.tenant_id,
                groups=sorted(principal.groups),
            )
            return tuple(EvidenceView(**record.data()) for record in records)
