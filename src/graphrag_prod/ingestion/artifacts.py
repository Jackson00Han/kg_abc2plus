"""Portable artifact codecs used before provider calls and during publication."""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from graphrag_prod.domain.ids import (
    assertion_id,
    chunk_embedding_id,
    entity_id,
    mention_id,
)
from graphrag_prod.domain.models import (
    Assertion,
    Chunk,
    ChunkEmbedding,
    Entity,
    EntityMention,
    GraphPipelineProfile,
    TypedLiteralValue,
)
from graphrag_prod.graph.provenance import ProvenanceBundle


def encode_extraction(bundle: ProvenanceBundle) -> dict[str, Any]:
    """Encode derivations relative to a chunk so unchanged text can be reused."""
    entities = {entity.entity_id: entity for entity in bundle.entities}
    encoded_mentions = [
        {
            "entity_type": mention.entity_type,
            "canonical_key": entities[mention.entity_id].canonical_key,
            "surface": mention.surface,
            "relative_start": mention.char_start - bundle.chunk.char_start,
            "relative_end": mention.char_end - bundle.chunk.char_start,
            "confidence": mention.confidence,
        }
        for mention in bundle.mentions
    ]
    encoded_assertions = [
        {
            "subject_type": entities[assertion.subject_entity_id].entity_type,
            "subject_key": entities[assertion.subject_entity_id].canonical_key,
            "predicate": assertion.predicate,
            "object_type": (
                entities[assertion.object_entity_id].entity_type
                if assertion.object_entity_id
                else None
            ),
            "object_key": (
                entities[assertion.object_entity_id].canonical_key
                if assertion.object_entity_id
                else None
            ),
            "literal_value": assertion.literal_value,
            "literal_semantics": (
                None
                if assertion.literal_semantics is None
                else assertion.literal_semantics.to_mapping()
            ),
            "relative_start": assertion.evidence_char_start - bundle.chunk.char_start,
            "relative_end": assertion.evidence_char_end - bundle.chunk.char_start,
            "confidence": assertion.confidence,
            "accepted": assertion.accepted,
        }
        for assertion in bundle.all_assertions
    ]
    return {
        "format_version": 2,
        "entities": [
            {
                "entity_type": entity.entity_type,
                "canonical_key": entity.canonical_key,
                "canonical_name": entity.canonical_name,
                "aliases": list(entity.aliases),
            }
            for entity in sorted(
                bundle.entities,
                key=lambda item: (
                    item.entity_type,
                    item.canonical_key,
                    item.canonical_name,
                    item.aliases,
                ),
            )
        ],
        # Stable artifact ordering must not depend on IDs that are rebound for
        # each source Chunk. Otherwise byte-identical provider inputs can map
        # to different immutable artifact payloads across document versions.
        "mentions": sorted(
            encoded_mentions,
            key=lambda item: (
                item["relative_start"],
                item["relative_end"],
                item["entity_type"],
                item["canonical_key"],
                item["surface"],
                item["confidence"],
            ),
        ),
        "assertions": sorted(
            encoded_assertions,
            key=lambda item: (
                item["relative_start"],
                item["relative_end"],
                item["subject_type"],
                item["subject_key"],
                item["predicate"],
                item["object_type"] or "",
                item["object_key"] or "",
                item["literal_value"] or "",
                json.dumps(
                    item["literal_semantics"],
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                item["confidence"],
                item["accepted"],
            ),
        ),
    }


def decode_extraction(
    payload: dict[str, Any],
    *,
    tenant_id: str,
    chunk: Chunk,
    profile: GraphPipelineProfile,
) -> tuple[tuple[Entity, ...], tuple[EntityMention, ...], tuple[Assertion, ...]]:
    """Rebind a cached relative artifact to a stable chunk in a new version."""
    format_version = payload.get("format_version")
    if format_version not in {1, 2}:
        raise ValueError("unsupported extraction artifact format")
    entities: list[Entity] = []
    entities_by_key: dict[tuple[str, str], Entity] = {}
    for item in payload.get("entities", []):
        key = (str(item["entity_type"]), str(item["canonical_key"]))
        if key in entities_by_key:
            raise ValueError("extraction artifact contains duplicate entity keys")
        entity = Entity(
            entity_id=entity_id(tenant_id, *key),
            tenant_id=tenant_id,
            entity_type=key[0],
            canonical_key=key[1],
            canonical_name=str(item["canonical_name"]),
            aliases=tuple(str(value) for value in item.get("aliases", [])),
        )
        entities.append(entity)
        entities_by_key[key] = entity

    mentions: list[EntityMention] = []
    for item in payload.get("mentions", []):
        key = (str(item["entity_type"]), str(item["canonical_key"]))
        entity = entities_by_key.get(key)
        if entity is None:
            raise ValueError("mention references an absent artifact entity")
        start = chunk.char_start + int(item["relative_start"])
        end = chunk.char_start + int(item["relative_end"])
        surface = str(item["surface"])
        mentions.append(
            EntityMention(
                mention_id=mention_id(
                    chunk.chunk_id,
                    entity.entity_type,
                    start,
                    end,
                    surface,
                    profile.extractor_signature,
                ),
                tenant_id=tenant_id,
                chunk_id=chunk.chunk_id,
                entity_id=entity.entity_id,
                entity_type=entity.entity_type,
                surface=surface,
                char_start=start,
                char_end=end,
                extractor_version=profile.extractor_signature,
                confidence=float(item["confidence"]),
            )
        )

    assertions: list[Assertion] = []
    for item in payload.get("assertions", []):
        subject = entities_by_key.get(
            (str(item["subject_type"]), str(item["subject_key"]))
        )
        if subject is None:
            raise ValueError("assertion subject is absent from artifact entities")
        object_key = item.get("object_key")
        object_entity = None
        if object_key is not None:
            object_entity = entities_by_key.get(
                (str(item["object_type"]), str(object_key))
            )
            if object_entity is None:
                raise ValueError("assertion object is absent from artifact entities")
        literal = item.get("literal_value")
        if object_entity is not None and literal is not None:
            raise ValueError("artifact assertion cannot contain entity and literal objects")
        if object_entity is None and (
            not isinstance(literal, str) or not literal.strip()
        ):
            raise ValueError("artifact literal assertion requires a non-empty string")
        literal_semantics = (
            TypedLiteralValue.from_mapping(item["literal_semantics"])
            if format_version == 2 and item.get("literal_semantics") is not None
            else None
        )
        if object_entity is not None and literal_semantics is not None:
            raise ValueError("artifact entity assertion cannot carry literal semantics")
        start = chunk.char_start + int(item["relative_start"])
        end = chunk.char_start + int(item["relative_end"])
        predicate = str(item["predicate"])
        object_kind = "entity" if object_entity is not None else "literal"
        object_reference = (
            object_entity.entity_id
            if object_entity is not None
            else (
                literal_semantics.identity_reference
                if literal_semantics is not None
                else str(literal or "")
            )
        )
        assertions.append(
            Assertion(
                assertion_id=assertion_id(
                    tenant_id,
                    subject.entity_id,
                    predicate,
                    object_kind,
                    object_reference,
                    chunk.chunk_id,
                    start,
                    end,
                    profile.extractor_signature,
                    profile.schema_signature,
                ),
                tenant_id=tenant_id,
                subject_entity_id=subject.entity_id,
                predicate=predicate,
                evidence_chunk_id=chunk.chunk_id,
                evidence_char_start=start,
                evidence_char_end=end,
                extractor_version=profile.extractor_signature,
                schema_version=profile.schema_signature,
                confidence=float(item["confidence"]),
                accepted=bool(item["accepted"]),
                object_entity_id=(
                    None if object_entity is None else object_entity.entity_id
                ),
                literal_value=None if object_entity is not None else literal,
                literal_semantics=(
                    None if object_entity is not None else literal_semantics
                ),
            )
        )
    return tuple(entities), tuple(mentions), tuple(assertions)


def encode_embedding(embedding: ChunkEmbedding) -> dict[str, Any]:
    return {
        "format_version": 1,
        "vector": list(embedding.vector),
        "vector_checksum": embedding.vector_checksum,
        "dimensions": embedding.dimensions,
    }


def decode_embedding(
    payload: dict[str, Any],
    *,
    tenant_id: str,
    chunk: Chunk,
    embedding_space_id: str,
    provider: str,
    model: str,
    revision: str,
    dimensions: int,
    normalization: str,
    created_at: datetime,
) -> ChunkEmbedding:
    if payload.get("format_version") != 1:
        raise ValueError("unsupported embedding artifact format")
    if int(payload.get("dimensions", -1)) != dimensions:
        raise ValueError("embedding artifact dimensions do not match profile")
    embedding = ChunkEmbedding(
        embedding_id=chunk_embedding_id(chunk.chunk_id, embedding_space_id),
        tenant_id=tenant_id,
        chunk_id=chunk.chunk_id,
        embedding_space_id=embedding_space_id,
        provider=provider,
        model=model,
        revision=revision,
        dimensions=dimensions,
        normalization=normalization,
        created_at=created_at,
        vector=tuple(payload.get("vector", ())),
    )
    if not embedding.vector or embedding.vector_checksum != payload.get("vector_checksum"):
        raise ValueError("embedding artifact vector checksum is invalid")
    return embedding
