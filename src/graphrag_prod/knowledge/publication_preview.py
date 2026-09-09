"""Deterministic, source-preserving projection of a publication transaction."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from enum import Enum
import hashlib
import json
from typing import Any

from .models import AssertionRecord, EntityMentionRecord


def json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(json_value(item) for item in value)
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    return value


def standardized_record(record: EntityMentionRecord | AssertionRecord) -> dict[str, Any]:
    """Readable business fields with immutable revision and source references."""
    evidence_id = record.revision_id
    common = {
        "record_id": record.record_id,
        "revision_id": record.revision_id,
        "revision": record.revision.revision,
        "authority_level": record.trust.authority.value,
        "origin": record.trust.origin.value,
        "evidence_ids": [evidence_id],
    }
    if isinstance(record, EntityMentionRecord):
        return dict(common, kind="ENTITY", entity_id=record.entity.entity_id,
                    entity_type=record.entity.entity_type,
                    standard_name=record.entity.canonical_name,
                    aliases=list(record.entity.aliases))
    common["fact_distinction"] = record.fact_distinction.to_mapping() if record.fact_distinction else None
    if record.object_entity is not None:
        return dict(common, kind="RELATIONSHIP", relationship_id=record.fact_key, fact_key=record.fact_key,
                    source_entity_id=record.subject.entity_id,
                    relationship_type=record.predicate,
                    target_entity_id=record.object_entity.entity_id,
                    properties=json_value([asdict(item) for item in record.relationship_properties]))
    literal = record.literal_semantics
    return dict(common, kind="PROPERTY", property_id=record.record_id,
                entity_id=record.subject.entity_id, property_name=record.predicate,
                value=literal.canonical_value if literal else record.literal_value,
                unit=literal.canonical_unit if literal else None,
                literal=json_value(asdict(literal)) if literal else None)


def entity_views(records: tuple) -> dict[str, dict]:
    """Aggregate source mentions by identity without assigning a global grade."""
    result = {}
    for record in sorted(records, key=lambda r: r.revision_id):
        if not isinstance(record, EntityMentionRecord):
            continue
        identity = record.entity
        item = result.setdefault(identity.entity_id, {
            "entity_id": identity.entity_id, "entity_type": identity.entity_type,
            "standard_name": identity.canonical_name, "aliases": [],
            "knowledge_layers": [], "evidence_ids": [], "mention_revision_ids": [],
        })
        item["aliases"] = sorted(set(item["aliases"]) | set(identity.aliases))
        item["knowledge_layers"] = sorted(set(item["knowledge_layers"]) | {record.trust.authority.value})
        item["evidence_ids"].append(record.revision_id)
        item["mention_revision_ids"].append(record.revision_id)
    return result


def instance_snapshot(records: tuple, evidence: list[dict]) -> dict:
    """Read-only business projection; never a Neo4j persistence payload."""
    entities = entity_views(records)
    for entity in entities.values():
        entity["properties"] = []
    relationships = {}
    for record in sorted(records, key=lambda item: item.revision_id):
        if isinstance(record, EntityMentionRecord):
            continue
        item = standardized_record(record)
        if record.object_entity is None:
            entities[record.subject.entity_id]["properties"].append(item)
        else:
            for role, identity in (("source", record.subject), ("target", record.object_entity)):
                target = entities[identity.entity_id]
                item[role] = {key: target[key] for key in
                              ("entity_id", "entity_type", "standard_name")}
            group = relationships.get(record.fact_key)
            source = {key: item[key] for key in
                      ("record_id", "revision_id", "authority_level", "origin", "evidence_ids", "properties", "fact_distinction")}
            if group is None:
                group = dict(item, sources=[], evidence_ids=[], authority_levels=[])
                relationships[record.fact_key] = group
            group["sources"].append(source)
            group["evidence_ids"].append(record.revision_id)
            group["authority_levels"] = sorted(set(group["authority_levels"]) | {item["authority_level"]})
            group["source_count"] = len(group["sources"])
    return {
        "schema": "graphrag-instance-snapshot-v1",
        "status": "PREVIEW",
        "scope": "PUBLICATION_AFTER",
        "summary": {
            "entity_count": len(entities),
            "property_count": sum(len(item["properties"]) for item in entities.values()),
            "relationship_count": len(relationships),
        },
        "entities": [entities[key] for key in sorted(entities)],
        "relationships": [relationships[key] for key in sorted(relationships)],
        "evidence": evidence,
    }


def publication_preview(*, publication_id: str, ontology_version_id: str,
                        manifest_hash: str, base_publication_id: str | None,
                        before: tuple, after: tuple, source_revision_ids: tuple[str, ...],
                        removed_record_ids: tuple[str, ...], replaced_record_ids: tuple[str, ...]) -> dict:
    previous = {item.record_id: item for item in before}
    final = {item.record_id: item for item in after}
    payload = {
        "schema": "graphrag-publication-preview-v1",
        "publication_id": publication_id,
        "ontology_version_id": ontology_version_id,
        "base_publication_id": base_publication_id,
        "manifest_hash": manifest_hash,
        "source_revision_ids": list(source_revision_ids),
        "removed_record_ids": list(removed_record_ids),
        "replaced_record_ids": list(replaced_record_ids),
        "entity_changes": [], "property_changes": [], "relationship_changes": [],
        # All records, including unchanged records and exact endpoint revisions,
        # make the final manifest auditable without fetching a truncated queue.
        "records_after": [json_value(asdict(item)) for item in sorted(after, key=lambda r: r.revision_id)],
        "evidence": [],
    }
    for record_id in sorted(set(previous) | set(final)):
        old, new = previous.get(record_id), final.get(record_id)
        if old and new and old.revision_id == new.revision_id:
            continue
        record = new or old
        if isinstance(record, EntityMentionRecord):
            continue
        kind = "relationship" if record.object_entity is not None else "property"
        payload[f"{kind}_changes"].append({
            "operation": "CREATE" if old is None else "REMOVE" if new is None else "UPDATE",
            "record_id": record_id,
            "before": standardized_record(old) if old else None,
            "after": standardized_record(new) if new else None,
        })
    old_entities, new_entities = entity_views(before), entity_views(after)
    for entity_id in sorted(set(old_entities) | set(new_entities)):
        old, new = old_entities.get(entity_id), new_entities.get(entity_id)
        if old == new:
            continue
        payload["entity_changes"].append({
            "operation": "CREATE" if old is None else "REMOVE" if new is None else "UPDATE",
            "entity_id": entity_id, "before": old, "after": new,
            "changes": {
                "aliases_added": sorted(set((new or {}).get("aliases", [])) - set((old or {}).get("aliases", []))),
                "aliases_removed": sorted(set((old or {}).get("aliases", [])) - set((new or {}).get("aliases", []))),
            },
        })
    payload["entities_after"] = list(new_entities.values())
    for record in sorted(after, key=lambda r: r.revision_id):
        payload["evidence"].append(dict(
            json_value(asdict(record.evidence)), evidence_id=record.revision_id,
            origin=record.trust.origin.value, authority_level=record.trust.authority.value))
    payload["instances_after"] = dict(
        instance_snapshot(after, payload["evidence"]),
        publication_id=publication_id, ontology_version_id=ontology_version_id,
        manifest_hash=manifest_hash,
    )
    old_relations = {item["fact_key"]: item for item in instance_snapshot(before, [])["relationships"]}
    new_relations = {item["fact_key"]: item for item in payload["instances_after"]["relationships"]}
    payload["relationship_fact_changes"] = []
    for key in sorted(set(old_relations) | set(new_relations)):
        old, new = old_relations.get(key), new_relations.get(key)
        if old == new:
            continue
        old_ids, new_ids = set((old or {}).get("evidence_ids", [])), set((new or {}).get("evidence_ids", []))
        payload["relationship_fact_changes"].append({
            "fact_key": key, "operation": "CREATE" if old is None else "REMOVE" if new is None else "UPDATE",
            "before": old, "after": new,
            "sources_added": sorted(new_ids - old_ids), "sources_removed": sorted(old_ids - new_ids),
        })
    payload["preview_hash"] = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()
    return payload
