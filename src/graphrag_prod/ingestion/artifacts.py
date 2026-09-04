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
    relationship_property_value_id,
)
from graphrag_prod.domain.models import (
    Assertion,
    Chunk,
    ChunkEmbedding,
    Entity,
    EntityMention,
    GraphPipelineProfile,
    RelationshipPropertyValue,
    TypedLiteralValue,
    canonical_relationship_object_reference,
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
            "relationship_properties": [
                {
                    "name": item.name,
                    "literal_semantics": item.literal_semantics.to_mapping(),
                    "relative_start": (
                        item.evidence_char_start - bundle.chunk.char_start
                    ),
                    "relative_end": item.evidence_char_end - bundle.chunk.char_start,
                    "evidence_text": item.evidence_text,
                    "confidence": item.confidence,
                }
                for item in assertion.relationship_properties
            ],
            "relative_start": assertion.evidence_char_start - bundle.chunk.char_start,
            "relative_end": assertion.evidence_char_end - bundle.chunk.char_start,
            "confidence": assertion.confidence,
            "accepted": assertion.accepted,
        }
        for assertion in bundle.all_assertions
    ]
    return {
        "format_version": 3,
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
                json.dumps(
                    item["relationship_properties"],
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
    if format_version not in {1, 2, 3}:
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
        if format_version in {1, 2} and "relationship_properties" in item:
            raise ValueError(
                "relationship properties require extraction artifact format 3"
            )
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
            if format_version >= 2 and item.get("literal_semantics") is not None
            else None
        )
        if object_entity is not None and literal_semantics is not None:
            raise ValueError("artifact entity assertion cannot carry literal semantics")
        start = chunk.char_start + int(item["relative_start"])
        end = chunk.char_start + int(item["relative_end"])
        predicate = str(item["predicate"])
        relationship_properties: tuple[RelationshipPropertyValue, ...] = ()
        if format_version == 3:
            raw_properties = item.get("relationship_properties", [])
            if not isinstance(raw_properties, list):
                raise ValueError("artifact relationship_properties must be an array")
            decoded_properties: list[RelationshipPropertyValue] = []
            expected_fields = {
                "name",
                "literal_semantics",
                "relative_start",
                "relative_end",
                "evidence_text",
                "confidence",
            }
            for raw_property in raw_properties:
                if not isinstance(raw_property, dict) or set(raw_property) != expected_fields:
                    raise ValueError(
                        "artifact relationship-property fields do not match the contract"
                    )
                property_name = raw_property["name"]
                if (
                    not isinstance(property_name, str)
                    or not property_name
                    or property_name != property_name.strip()
                ):
                    raise ValueError(
                        "artifact relationship-property name must be exact non-empty text"
                    )
                property_literal = TypedLiteralValue.from_mapping(
                    raw_property["literal_semantics"]
                )
                relative_start = raw_property["relative_start"]
                relative_end = raw_property["relative_end"]
                if (
                    isinstance(relative_start, bool)
                    or not isinstance(relative_start, int)
                    or isinstance(relative_end, bool)
                    or not isinstance(relative_end, int)
                ):
                    raise ValueError(
                        "artifact relationship-property offsets must be integers"
                    )
                property_start = chunk.char_start + relative_start
                property_end = chunk.char_start + relative_end
                relative_property_start = property_start - chunk.char_start
                relative_property_end = property_end - chunk.char_start
                evidence_text = raw_property["evidence_text"]
                if not isinstance(evidence_text, str) or (
                    property_start < chunk.char_start
                    or property_end > chunk.char_end
                    or chunk.text[relative_property_start:relative_property_end]
                    != evidence_text
                ):
                    raise ValueError(
                        "artifact relationship-property evidence does not match its Chunk"
                    )
                decoded_properties.append(
                    RelationshipPropertyValue(
                        property_value_id=relationship_property_value_id(
                            tenant_id,
                            predicate,
                            property_name,
                            property_literal.identity_reference,
                            chunk.chunk_id,
                            property_start,
                            property_end,
                            profile.extractor_signature,
                            profile.schema_signature,
                        ),
                        tenant_id=tenant_id,
                        relationship_type=predicate,
                        name=property_name,
                        literal_semantics=property_literal,
                        evidence_chunk_id=chunk.chunk_id,
                        evidence_char_start=property_start,
                        evidence_char_end=property_end,
                        evidence_text=evidence_text,
                        extractor_version=profile.extractor_signature,
                        schema_version=profile.schema_signature,
                        confidence=float(raw_property["confidence"]),
                    )
                )
            relationship_properties = tuple(decoded_properties)
        if object_entity is None and relationship_properties:
            raise ValueError(
                "artifact literal assertion cannot carry relationship properties"
            )
        if any(
            value.evidence_char_start < start or value.evidence_char_end > end
            for value in relationship_properties
        ):
            raise ValueError(
                "artifact relationship-property evidence lies outside its assertion"
            )
        object_kind = "entity" if object_entity is not None else "literal"
        object_reference = (
            canonical_relationship_object_reference(
                object_entity.entity_id,
                relationship_properties,
            )
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
                relationship_properties=relationship_properties,
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
