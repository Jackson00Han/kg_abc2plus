"""Validation of the versioned adjudicated graph-review dataset."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class GraphReviewMetrics:
    entity_precision: float
    relationship_precision: float
    entity_resolution_accuracy: float
    item_count: int

    def meets(self, target: float = 0.95) -> bool:
        return (
            self.entity_precision >= target
            and self.relationship_precision >= target
            and self.entity_resolution_accuracy >= target
        )


def evaluate_graph_review_dataset(path: Path) -> GraphReviewMetrics:
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("dataset_id") != "graph-review-v1" or not payload.get("version"):
        raise ValueError("graph review dataset identity/version is invalid")
    if not str(payload.get("owner", "")).strip():
        raise ValueError("graph review dataset requires an owner")
    items = payload.get("items")
    if not isinstance(items, list) or len(items) < 50:
        raise ValueError("graph-review-v1 requires at least 50 adjudicated items")
    identifiers = [item.get("id") for item in items]
    if any(not isinstance(value, str) or not value.strip() for value in identifiers):
        raise ValueError("every graph review item requires an ID")
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("graph review item IDs must be unique")
    kinds = {"entity", "relationship", "resolution"}
    if {item.get("kind") for item in items} != kinds:
        raise ValueError("graph review dataset must cover entity, relationship, and resolution")
    for kind in kinds:
        cases = [item for item in items if item.get("kind") == kind]
        if not any(bool(item.get("negative_case")) for item in cases):
            raise ValueError(f"graph review {kind} cases require a negative contrast")
        if not any(not bool(item.get("negative_case")) for item in cases):
            raise ValueError(f"graph review {kind} cases require a positive case")
        if any(not item.get("evidence_ids") for item in cases):
            raise ValueError(f"graph review {kind} cases require evidence IDs")

    accepted_entities = [
        item for item in items
        if item["kind"] == "entity" and item.get("system_accepted") is True
    ]
    accepted_relationships = [
        item for item in items
        if item["kind"] == "relationship" and item.get("system_accepted") is True
    ]
    resolution = [item for item in items if item["kind"] == "resolution"]
    if not accepted_entities or not accepted_relationships or not resolution:
        raise ValueError("graph review metric denominators must not be empty")
    entity_precision = sum(
        item.get("adjudicated_correct") is True for item in accepted_entities
    ) / len(accepted_entities)
    relationship_precision = sum(
        item.get("adjudicated_supported") is True for item in accepted_relationships
    ) / len(accepted_relationships)
    resolution_accuracy = sum(
        item.get("predicted_outcome") == item.get("expected_outcome")
        for item in resolution
    ) / len(resolution)
    return GraphReviewMetrics(
        entity_precision,
        relationship_precision,
        resolution_accuracy,
        len(items),
    )
