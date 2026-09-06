"""Bounded, iterative DAG validation for complete governed publications."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Iterable

from .models import TBoxVersion


MAX_HIERARCHY_EDGES = 5_000
MAX_HIERARCHY_NODES = 5_000


class HierarchyValidationError(ValueError):
    """A declared hierarchy is cyclic, inconsistent or outside its budget."""


@dataclass(frozen=True, slots=True)
class HierarchyEdge:
    subject_id: str
    subject_type: str
    predicate: str
    object_id: str
    object_type: str

    def __post_init__(self) -> None:
        for name in ("subject_id", "subject_type", "predicate", "object_id", "object_type"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise HierarchyValidationError("hierarchy edge requires bounded non-empty identifiers")


@dataclass(frozen=True, slots=True)
class HierarchySummary:
    name: str
    node_count: int
    edge_count: int
    root_ids: tuple[str, ...]
    maximum_depth: int


def validate_hierarchy_edges(
    tbox: TBoxVersion,
    edges: Iterable[HierarchyEdge],
) -> tuple[HierarchySummary, ...]:
    """Validate declared hierarchies independently; never derive new facts.

Edges are oriented child -> parent. Duplicate evidence for the same edge is
    counted once. Kahn's algorithm detects cycles without recursive traversal.
    Completeness is the caller's responsibility; a retrieval subgraph is not a
    suitable input for publication validation.
    """
    if not isinstance(tbox, TBoxVersion):
        raise TypeError("hierarchy validation requires a TBoxVersion")
    contracts = {item.relationship_type: item for item in tbox.hierarchies}
    relationships = {item.name: item for item in tbox.relationship_types}
    typed_nodes: dict[str, str] = {}
    selected: dict[str, set[tuple[str, str]]] = {name: set() for name in contracts}
    for count, edge in enumerate(edges, start=1):
        if count > MAX_HIERARCHY_EDGES:
            raise HierarchyValidationError("hierarchy validation exceeds its edge budget")
        if not isinstance(edge, HierarchyEdge):
            raise TypeError("hierarchy validation requires HierarchyEdge values")
        if edge.predicate not in contracts:
            continue
        relationship = relationships[edge.predicate]
        if edge.subject_type not in relationship.source_types or edge.object_type not in relationship.target_types:
            raise HierarchyValidationError("hierarchy edge violates its endpoint type contract")
        for node_id, node_type in ((edge.subject_id, edge.subject_type), (edge.object_id, edge.object_type)):
            previous = typed_nodes.setdefault(node_id, node_type)
            if previous != node_type:
                raise HierarchyValidationError("hierarchy node has inconsistent identity types")
        if len(typed_nodes) > MAX_HIERARCHY_NODES:
            raise HierarchyValidationError("hierarchy validation exceeds its node budget")
        if edge.subject_id == edge.object_id:
            raise HierarchyValidationError("hierarchy contains a self-loop")
        selected[edge.predicate].add((edge.subject_id, edge.object_id))

    summaries = []
    for predicate, hierarchy in sorted(contracts.items()):
        unique_edges = selected[predicate]
        nodes = {node for pair in unique_edges for node in pair}
        parents = {node: set() for node in nodes}
        incoming = {node: 0 for node in nodes}
        depth = {node: 0 for node in nodes}
        for child, parent in unique_edges:
            parents[child].add(parent)
            incoming[parent] += 1
        queue = deque(sorted(node for node, degree in incoming.items() if degree == 0))
        visited = 0
        while queue:
            child = queue.popleft()
            visited += 1
            for parent in sorted(parents[child]):
                depth[parent] = max(depth[parent], depth[child] + 1)
                incoming[parent] -= 1
                if incoming[parent] == 0:
                    queue.append(parent)
        if visited != len(nodes):
            raise HierarchyValidationError("hierarchy contains a cycle")
        summaries.append(HierarchySummary(
            hierarchy.name, len(nodes), len(unique_edges),
            tuple(sorted(node for node in nodes if not parents[node])),
            max(depth.values(), default=0),
        ))
    return tuple(summaries)
