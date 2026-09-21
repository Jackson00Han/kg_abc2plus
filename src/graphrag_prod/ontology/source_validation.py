"""Closed-manifest validation for the source-contract extraction profile.

No graph inference or diagnostic execution occurs here. Type-endpoint and
externally derived declarations are denied by the ordinary T-Box guards before
this validator runs. Missing bindings remain blockers, never default facts.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any


class SourcePublicationConstraintError(ValueError):
    """A constraint location inside the already authorized closed manifest."""

    def __init__(self, message: str, *, reason: str, entity_id: str, predicate: str | None = None):
        super().__init__(message)
        self.reason = reason
        self.entity_id = entity_id
        self.predicate = predicate


def validate_source_publication(
    source: dict[str, Any],
    entities: dict[str, Any],
    values: dict[str, dict[str, Any]],
    relationships: list[Any],
    registry: dict[str, Any] | None = None,
) -> None:
    if source.get("metadata", {}).get("model_kind") == "property_graph":
        validate_property_graph_publication(source, entities, values, relationships)
        return
    outgoing: dict[tuple[str, str], list[Any]] = defaultdict(list)
    incoming: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for edge in relationships:
        outgoing[(edge.subject.entity_id, edge.predicate)].append(edge)
        incoming[(edge.object_entity.entity_id, edge.predicate)].append(edge)
        subject = values.get(edge.subject.entity_id, {})
        target = values.get(edge.object_entity.entity_id, {})
        if source["relation_types"][edge.predicate]["statement_semantics"]["scope_kind"] == "instance":
            if subject.get("project_id") and target.get("project_id") and subject["project_id"] != target["project_id"]:
                raise ValueError("source relationship crosses project identity boundaries")
        if edge.predicate == "OBSERVES" and target.get("availability") == "computed_on_demand":
            raise ValueError("a configured source point cannot observe a computed-on-demand quantity")
        if edge.predicate == "POSSIBLE_CAUSE" and subject.get("diagnostic_scope_status") == "outside_current_product_scope":
            raise ValueError("outside-scope phenomenon cannot publish a possible-cause relation")
    # The contract defines business identity scope explicitly. Names and labels
    # never establish identity; duplicate scoped keys require audited resolution.
    unique: dict[tuple[Any, ...], str] = {}
    for identity, entity in entities.items():
        definition = source["identity_constraints"]["entity_identity"].get(entity.entity_type, {})
        business_keys = definition.get("business_key_properties", [])
        own_values = values.get(identity, {})
        if business_keys:
            scope = []
            for key in definition.get("uniqueness_scope", []):
                if key == "tenant_id":
                    continue  # Authenticated manifest already has one tenant.
                if key == "owner_entity_id":
                    edges = incoming[(identity, definition["owner_relation"])]
                    owners = {edge.subject.entity_id for edge in edges}
                    if len(owners) != 1:
                        raise ValueError("scoped source identity requires exactly one evidenced owner")
                    scope.append(next(iter(owners)))
                elif key in own_values:
                    scope.append(own_values[key])
                else:
                    raise ValueError(f"source identity scope {key} is absent")
            if any(key not in own_values for key in business_keys):
                raise ValueError("source business identity properties are incomplete")
            signature = ("business", tuple(business_keys), *scope, *(own_values[key] for key in business_keys))
            if signature in unique and unique[signature] != identity:
                raise ValueError("duplicate scoped business identity requires audited resolution")
            unique[signature] = identity
        for unique_fields in definition.get("additional_unique_property_sets", []):
            signature = (entity.entity_type, "additional", *(own_values.get(key) for key in unique_fields))
            if signature in unique and unique[signature] != identity:
                raise ValueError("duplicate domain knowledge key requires audited resolution")
            unique[signature] = identity
    # Structural composition is acyclic, independent of classification hierarchy.
    contains: dict[str, set[str]] = defaultdict(set)
    for edge in relationships:
        if edge.predicate == "CONTAINS":
            contains[edge.subject.entity_id].add(edge.object_entity.entity_id)
    visiting, visited = set(), set()
    def visit(identity: str) -> None:
        if identity in visiting:
            raise ValueError("source composition contains a cycle")
        if identity in visited:
            return
        visiting.add(identity)
        for target in contains.get(identity, ()):
            visit(target)
        visiting.remove(identity)
        visited.add(identity)
    for identity in tuple(contains):
        visit(identity)

    documents = {} if registry is None else {
        (document["document"], document["document_version"], document["profile_id"]): {clause["id"] for clause in document["clauses"]}
        for document in registry["documents"]
    }
    def attributes(edge: Any) -> dict[str, Any]:
        return {item.name: item.literal_semantics.typed_value for item in edge.relationship_properties}
    rule_bindings: dict[str, set[str]] = defaultdict(set)
    for edge in relationships:
        if edge.predicate != "EVALUATED_BY":
            continue
        target = values.get(edge.object_entity.entity_id, {})
        registered = documents.get(tuple(target.get(key) for key in ("document", "document_version", "profile_id")), set())
        clause = attributes(edge).get("rule_clause")
        if not clause or clause not in registered:
            raise ValueError("rule clause is absent from the ontology-bound external reference registry")
        rule_bindings[edge.subject.entity_id].add(clause)
    for identity, entity in entities.items():
        own_values = values.get(identity, {})
        if entity.entity_type == "Phenomenon":
            scope_edges = [edge for edge in outgoing[(identity, "APPLICABLE_TO_SCOPE")] if attributes(edge).get("scope_role") == "localization"]
            localization = {edge.object_entity.entity_id for edge in scope_edges}
            comparison = {edge.object_entity.entity_id for edge in outgoing[(identity, "APPLICABLE_TO_SCOPE")] if attributes(edge).get("scope_role") == "comparison_basis"}
            if localization & comparison:
                raise ValueError("localization scope must not be reused as comparison basis")
            if len(localization) != 1:
                raise ValueError("phenomenon requires exactly one explicit localization scope")
            if own_values.get("rule_evaluation_status") == "produced_by_rule" and not rule_bindings[identity]:
                raise ValueError("produced phenomenon requires a registered rule version/clause binding")
        if entity.entity_type == "RootCause" and not rule_bindings[identity]:
            raise ValueError("root-cause concept requires a registered rule version/clause binding")
    for edge in relationships:
        if edge.predicate == "CHARACTERIZED_BY":
            props = attributes(edge)
            if props.get("criterion_role") == "rule_criterion" and props.get("rule_clause") not in rule_bindings[edge.subject.entity_id]:
                raise ValueError("criterion clause must resolve through the phenomenon's registered rule binding")


def validate_property_graph_publication(
    source: dict[str, Any],
    entities: dict[str, Any],
    values: dict[str, dict[str, Any]],
    relationships: list[Any],
) -> None:
    """Execute uploaded constraints against a closed authorized graph manifest.

    This preserves source identity mappings and counts distinct graph objects,
    so additional evidence never becomes an additional engineering owner.
    Textual business descriptions remain review input, not executable rules.
    """
    from decimal import Decimal  # noqa: PLC0415
    from .source import canonical_json  # noqa: PLC0415
    constraints = source["constraints"]
    structure = constraints["engineering_structure"]
    outgoing: dict[tuple[str, str], set[str]] = defaultdict(set)
    incoming: dict[tuple[str, str], set[str]] = defaultdict(set)
    ownership: dict[str, set[str]] = defaultdict(set)
    acyclic: dict[str, set[str]] = defaultdict(set)
    structural_properties: dict[tuple[str, str, str], str] = {}
    for identity, entity in entities.items():
        if not isinstance(identity, str) or not identity.strip() or entity.entity_id != identity:
            raise ValueError("entity identity must resolve to its stable platform ID")
        if entity.entity_type not in source["entity_types"]:
            raise ValueError("source graph contains an undeclared entity type")
    for edge in relationships:
        subject, target = edge.subject.entity_id, edge.object_entity.entity_id
        if subject not in entities or target not in entities:
            raise ValueError("source graph relationship endpoint does not exist")
        relation = source["relation_types"].get(edge.predicate)
        if relation is None:
            raise ValueError("source graph contains an undeclared relationship type")
        pair = {"from": entities[subject].entity_type, "to": entities[target].entity_type}
        if pair not in relation["allowed_type_pairs"]:
            raise ValueError("source graph relationship endpoints violate allowed type pairs")
        # The graph represents one structural edge per pair; several assertion
        # records may legitimately preserve the independent sources for it.
        if edge.predicate in structure["non_repeatable_relations"]:
            signature = (subject, edge.predicate, target)
            properties = {item.name: item.literal_semantics.canonical_value for item in edge.relationship_properties}
            encoded = canonical_json(properties)
            if signature in structural_properties and structural_properties[signature] != encoded:
                raise ValueError("source structural relationship has conflicting duplicate properties")
            structural_properties[signature] = encoded
        outgoing[(subject, edge.predicate)].add(target)
        incoming[(target, edge.predicate)].add(subject)
        if edge.predicate in structure["ownership_relations"]:
            ownership[target].add(subject)
        if edge.predicate in structure["acyclic_relations"]:
            acyclic[subject].add(target)
    for name, constraint in constraints.items():
        if "allowed_combinations" not in constraint:
            continue
        combinations = constraint["allowed_combinations"]
        fields = tuple(combinations[0])
        declared = {**source["property_definitions"]["entity_common"]["properties"], **source["entity_types"][constraint["entity_type"]]["properties"]}
        def same(key: str, actual: Any, expected: Any) -> bool:
            if declared[key]["value_type"] in {"number", "integer"}:
                return not isinstance(actual, bool) and Decimal(str(actual)) == Decimal(str(expected))
            return type(actual) is type(expected) and actual == expected
        for identity, entity in entities.items():
            if entity.entity_type != constraint["entity_type"]:
                continue
            own = values.get(identity, {})
            if any(key not in own for key in fields) or not any(all(same(key, own[key], row[key]) for key in fields) for row in combinations):
                raise SourcePublicationConstraintError(
                    f"source property combination violates {name}",
                    reason="PROPERTY_COMBINATION_INVALID", entity_id=identity)
    for rule in constraints["ownership_cardinality"]["rules"]:
        adjacency = incoming if rule["direction"] == "incoming" else outgoing
        for identity, entity in entities.items():
            if entity.entity_type not in rule["entity_types"]:
                continue
            count = len(adjacency[(identity, rule["relation_type"])])
            if count < rule["min_count"] or (rule["max_count"] is not None and count > rule["max_count"]):
                raise SourcePublicationConstraintError(
                    f"source ownership cardinality violates {rule['id']}",
                    reason="RELATIONSHIP_REQUIRED" if count < rule["min_count"] else "RELATIONSHIP_CARDINALITY",
                    entity_id=identity, predicate=rule["relation_type"])
    # Iterative traversal avoids Python recursion limits on valid long chains.
    indegree: dict[str, int] = {identity: 0 for identity in entities}
    for children in acyclic.values():
        for identity in children:
            indegree[identity] += 1
    remaining = [identity for identity, degree in indegree.items() if degree == 0]
    visited = 0
    while remaining:
        identity = remaining.pop()
        visited += 1
        for child in acyclic.get(identity, ()):
            indegree[child] -= 1
            if indegree[child] == 0:
                remaining.append(child)
    if visited != len(entities):
        raise ValueError("source graph contains a forbidden structural cycle")
    if structure["require_one_project"]:
        root_type, member_types = structure["project_root_type"], set(structure["project_member_types"])
        for identity, entity in entities.items():
            if entity.entity_type not in member_types:
                continue
            pending, seen, roots = [identity], set(), set()
            while pending:
                candidate = pending.pop()
                if candidate in seen:
                    continue
                seen.add(candidate)
                if entities[candidate].entity_type == root_type:
                    roots.add(candidate)
                else:
                    pending.extend(ownership.get(candidate, ()))
            if len(roots) != 1:
                raise SourcePublicationConstraintError(
                    "source graph member requires exactly one evidenced project root",
                    reason="PROJECT_ROOT_UNRESOLVED", entity_id=identity)
