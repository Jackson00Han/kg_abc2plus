"""Closed-manifest validation for the source-contract extraction profile.

No graph inference or diagnostic execution occurs here. Type-endpoint and
externally derived declarations are denied by the ordinary T-Box guards before
this validator runs. Missing bindings remain blockers, never default facts.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any


def validate_source_publication(
    source: dict[str, Any],
    entities: dict[str, Any],
    values: dict[str, dict[str, Any]],
    relationships: list[Any],
    registry: dict[str, Any] | None = None,
) -> None:
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
