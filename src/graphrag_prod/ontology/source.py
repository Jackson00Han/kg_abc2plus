"""Bounded compiler for the versioned knowledge-ontology source contract.

The complete authoring contract is retained as canonical JSON. The executable
profile admits source-grounded instance/concept assertions only; declarations
requiring type endpoints or an external derivation engine remain registered but
cannot be emitted as ordinary entity relationships.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

SOURCE_CONTRACT_ID = "ai_power.knowledge_ontology"
SOURCE_CONTRACT_VERSION = "1.0.0"
SOURCE_PROFILE = "governed_document_extraction_v1"
MAX_SOURCE_BYTES = 1_000_000
_TOP_LEVEL = frozenset({"metadata", "entity_types", "type_hierarchy", "relation_categories", "relation_types", "property_definitions", "identity_constraints", "statement_model", "governance_constraints", "annotations"})
_TYPES = {"string": "STRING", "integer": "INTEGER", "number": "DECIMAL", "boolean": "BOOLEAN", "date": "DATE", "datetime": "DATETIME", "duration": "DURATION", "uri": "URI", "object": "JSON", "array[string]": "JSON", "array[object]": "JSON"}
_PROPERTY_KEYS = frozenset({"value_type", "required", "allowed_values", "const", "minimum", "maximum", "min_items", "max_items", "required_when", "unit", "definition", "rationale", "semantic_status", "semantic_note", "semantics", "reference_kind", "item_reference_kind"})
_ENTITY_KEYS = frozenset({"abstract", "aliases", "category", "definition", "definition_basis", "display_name", "instantiable", "introduced_in", "layer", "property_schema", "purpose", "semantic_role", "source_mode"})
_RELATION_KEYS = frozenset({"aliases", "allowed_type_pairs", "canonical_storage", "cardinality", "category", "definition", "definition_authority", "definition_basis", "derivation_contract", "derivation_path", "direction", "display_name", "formation_note", "formation_policy", "from", "granularity_guard", "instance_expansion", "interpretation", "introduced_in", "inverse_query_aliases", "inverse_traversal", "layer", "materialization", "pair_semantics", "persistence", "property_schema", "purpose", "role", "scope_eligibility", "semantic_boundaries", "source_attribute", "statement_semantics", "to", "unresolved_scope_materialization"})
_CONSTRAINT_KEYS = frozenset({"value_type", "allowed_values", "const", "minimum", "maximum", "min_items", "max_items", "required_when"})


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def source_version_number(value: object) -> int:
    """A monotone, collision-free encoding for bounded stable x.y.z versions."""
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", value):
        raise ValueError("source ontology_version requires a stable major.minor.patch version")
    major, minor, patch = map(int, value.split("."))
    if major > 2146 or minor >= 1000 or patch >= 1000:
        raise ValueError("source version components exceed supported bounds")
    result = major * 1_000_000 + minor * 1000 + patch
    if result < 1:
        raise ValueError("source ontology version must be positive")
    return result


def condition_predicate(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]*) == ([A-Za-z][A-Za-z0-9_]*)", value)
        if match:
            return {"property": match[1], "operator": "equals", "value": match[2]}
    if isinstance(value, dict):
        if set(value) == {"property", "operator", "value"} and value["operator"] in {"equals", "in"}:
            if not isinstance(value["property"], str):
                raise ValueError("condition property must be a declared property name")
            if value["operator"] == "in" and not isinstance(value["value"], list):
                raise ValueError("in condition requires a value array")
            return value
        if len(value) == 1:
            name, values = next(iter(value.items()))
            if name.endswith("_in") and isinstance(values, list):
                return {"property": name[:-3], "operator": "in", "value": values}
    raise ValueError("unsupported conditional presence predicate")


def condition_matches(predicate: dict[str, Any], values: dict[str, Any]) -> bool:
    if predicate["property"] not in values:
        return False
    value = values[predicate["property"]]
    return value == predicate["value"] if predicate["operator"] == "equals" else value in predicate["value"]


def validate_property_constraints_schema(constraint: object, datatype: str) -> None:
    if not isinstance(constraint, dict) or set(constraint) - _CONSTRAINT_KEYS:
        raise ValueError("unknown property constraints")
    value_type = constraint.get("value_type")
    if value_type is not None and _TYPES.get(value_type) != datatype:
        raise ValueError("property constraint value_type must agree with datatype")
    if "required_when" in constraint:
        condition_predicate(constraint["required_when"])
    if "allowed_values" in constraint:
        values = constraint["allowed_values"]
        if not isinstance(values, list) or not 1 <= len(values) <= 1024 or any(isinstance(value, (dict, list)) or value is None for value in values):
            raise ValueError("allowed_values must be a nonempty bounded scalar array")
        if len({canonical_json(value) for value in values}) != len(values):
            raise ValueError("allowed_values must be unique")
    constrained_values = list(constraint.get("allowed_values", []))
    if "const" in constraint:
        constrained_values.append(constraint["const"])
    scalar_types = {"INTEGER": (int,), "FLOAT": (int, float), "DECIMAL": (int, float), "BOOLEAN": (bool,)}
    expected_types = scalar_types.get(datatype, (str,))
    if constrained_values and (datatype == "JSON" or any(type(value) not in expected_types for value in constrained_values)):
        raise ValueError("enum/const values must match the property datatype")
    if "const" in constraint and "allowed_values" in constraint and constraint["const"] not in constraint["allowed_values"]:
        raise ValueError("property constant is outside its enumeration")
    for key in ("minimum", "maximum"):
        if key in constraint and (datatype not in {"INTEGER", "DECIMAL", "FLOAT"} or type(constraint[key]) not in {int, float}):
            raise ValueError("numeric bounds require a numeric property and finite number")
    if constraint.get("minimum") is not None and constraint.get("maximum") is not None and constraint["minimum"] > constraint["maximum"]:
        raise ValueError("property minimum exceeds maximum")
    for key in ("min_items", "max_items"):
        if key in constraint and (value_type not in {"array[string]", "array[object]"} or type(constraint[key]) is not int or constraint[key] < 0):
            raise ValueError("collection bounds require a typed array and nonnegative integer")
    if constraint.get("max_items") is not None and constraint.get("min_items", 0) > constraint["max_items"]:
        raise ValueError("property min_items exceeds max_items")
    canonical_json(constraint)  # Reject non-finite JSON numeric values.


def validate_property_constraints(constraints_json: str | None, value: Any) -> None:
    if not constraints_json:
        return
    constraint = json.loads(constraints_json)
    value_type = constraint.get("value_type")
    if value_type in {"object", "array[string]", "array[object]"}:
        if isinstance(value, str):
            value = json.loads(value)
        if value_type == "object" and not isinstance(value, dict):
            raise ValueError("property requires a JSON object")
        if value_type.startswith("array["):
            item_type = str if value_type == "array[string]" else dict
            if not isinstance(value, list) or any(not isinstance(item, item_type) for item in value):
                raise ValueError(f"property requires {value_type}")
            if len(value) < constraint.get("min_items", 0) or (constraint.get("max_items") is not None and len(value) > constraint["max_items"]):
                raise ValueError("property array length violates its constraint")
    def same(first: Any, second: Any) -> bool:
        if value_type in {"integer", "number"}:
            from decimal import Decimal  # noqa: PLC0415
            return Decimal(str(first)) == Decimal(str(second))
        return type(first) is type(second) and first == second
    if "const" in constraint and not same(value, constraint["const"]):
        raise ValueError("property differs from its declared constant")
    if "allowed_values" in constraint and not any(same(value, item) for item in constraint["allowed_values"]):
        raise ValueError("property value is outside its declared enumeration")
    for key, compare in (("minimum", lambda a, b: a < b), ("maximum", lambda a, b: a > b)):
        if key in constraint:
            from decimal import Decimal  # noqa: PLC0415
            if compare(Decimal(str(value)), Decimal(str(constraint[key]))):
                raise ValueError(f"property violates {key}")


def validate_conditional_properties(definitions: Any, values: dict[str, Any]) -> None:
    for definition in definitions:
        constraints = json.loads(definition.constraints_json or "{}")
        predicate = constraints.get("required_when")
        if predicate and condition_matches(predicate, values) and definition.name not in values:
            raise ValueError(f"conditionally required property {definition.name} is absent")


def _properties(values: Mapping[str, Any]) -> list[dict[str, Any]]:
    result = []
    for name, source in values.items():
        unknown = set(source) - _PROPERTY_KEYS
        if unknown:
            raise ValueError(f"unsupported property constraints on {name}: {sorted(unknown)}")
        datatype = _TYPES.get(source.get("value_type"))
        if datatype is None:
            raise ValueError(f"unsupported property value_type on {name}")
        required = source.get("required", False)
        if not isinstance(required, bool):
            raise ValueError("property required must be boolean")
        constraint = {key: source[key] for key in _CONSTRAINT_KEYS if key in source}
        if "required_when" in constraint:
            constraint["required_when"] = condition_predicate(constraint["required_when"])
            if constraint["required_when"]["property"] not in values:
                raise ValueError("conditional presence references an undeclared property")
        validate_property_constraints_schema(constraint, datatype)
        definition = {"name": name, "datatype": datatype, "required": required, "cardinality": "ONE" if required else "ZERO_OR_ONE", "constraints_json": canonical_json(constraint)}
        if "unit" in source:
            definition["unit"] = source["unit"]
        if source.get("definition"):
            definition["description"] = source["definition"].replace("\n", " ")
        result.append(definition)
    return result


def _compile_source_contract(source: Mapping[str, Any]) -> dict[str, Any]:
    """Compile declarations; reject unknown shapes, unresolved refs and cycles."""
    if set(source) != _TOP_LEVEL:
        raise ValueError("source ontology must contain exactly the ten versioned contract sections")
    text = canonical_json(source)
    if len(text.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ValueError("source ontology exceeds the bounded import size")
    metadata = source["metadata"]
    if metadata.get("contract_id") != SOURCE_CONTRACT_ID or metadata.get("contract_version") != SOURCE_CONTRACT_VERSION:
        raise ValueError("unsupported source ontology contract/version")
    if metadata.get("model_kind") != "graph_native_property_graph" or metadata.get("canonical_relation_storage") != "forward_only":
        raise ValueError("unsupported source graph model or direction policy")
    if metadata.get("semantic_dependencies"):
        raise ValueError("external ontology dependencies must be resolved before activation")
    entities = source["entity_types"]
    relations = source["relation_types"]
    properties = source["property_definitions"]
    if not isinstance(entities, dict) or not isinstance(relations, dict) or not 1 <= len(entities) <= 256 or len(relations) > 512:
        raise ValueError("source types must be named objects")
    if set(properties["entity_types"]) != set(entities) or set(properties["relation_types"]) != set(relations):
        raise ValueError("property schema owners must exactly match declared types")
    hierarchy = source["type_hierarchy"]
    if hierarchy.get("disjoint_type_sets"):
        raise ValueError("disjoint type sets are not supported by this single-type profile")
    parents: dict[str, list[str]] = {name: [] for name in entities}
    for pair in hierarchy["subtype_of"]:
        child, parent = pair["child"], pair["parent"]
        if child not in entities or parent not in entities:
            raise ValueError("type inheritance contains an undeclared endpoint")
        parents[child].append(parent)
    inherited: dict[str, dict[str, Any]] = {}
    visiting: set[str] = set()
    def resolve(name: str) -> dict[str, Any]:
        if name in visiting:
            raise ValueError("type inheritance cycle")
        if name in inherited:
            return inherited[name]
        visiting.add(name)
        merged: dict[str, Any] = {}
        for parent in parents[name]:
            for prop, definition in resolve(parent).items():
                if prop in merged and merged[prop] != definition:
                    raise ValueError(f"conflicting inherited constraints: {name}.{prop}")
                merged[prop] = definition
        for prop, definition in properties["entity_types"][name].items():
            if prop in merged and merged[prop] != definition:
                # No weakening through an accidental last-wins merge. More
                # sophisticated intersections require a new compiler profile.
                raise ValueError(f"conflicting inherited constraints: {name}.{prop}")
            merged[prop] = definition
        visiting.remove(name)
        inherited[name] = merged
        return merged
    forbidden_values = set(source["governance_constraints"]["external_values"].get("forbidden_declared_entity_property_names", ()))
    compiled_entities = []
    for name, entity in entities.items():
        if not isinstance(entity, dict) or set(entity) - _ENTITY_KEYS:
            raise ValueError(f"unsupported entity declaration fields: {name}")
        if entity.get("property_schema") != {"$ref": f"#/property_definitions/entity_types/{name}"}:
            raise ValueError("unresolved entity property schema reference")
        abstract = entity.get("abstract")
        if not isinstance(abstract, bool) or entity.get("instantiable") is not (not abstract):
            raise ValueError("abstract and instantiable flags must agree")
        identity = source["identity_constraints"]["entity_identity"].get(name, {})
        # Scope-qualified native business properties only. Owner-/namespace-
        # based identity remains in the audit contract and is never guessed.
        identity_properties = []
        if identity.get("uniqueness_scope") == ["tenant_id", "project_id"]:
            identity_properties = ["project_id", *identity.get("business_key_properties", [])]
        resolved_properties = resolve(name)
        if forbidden_values & set(resolved_properties):
            raise ValueError("source ontology forbids storing external runtime/parameter value properties")
        declared_properties = _properties(resolved_properties)
        source_mode = entity.get("source_mode")
        if source_mode not in {None, "materialized", "declared", "derived"}:
            raise ValueError("unsupported entity formation mode")
        compiled_entities.append({"name": name, "canonical_key_namespaces": ["llm-candidate", "source", name.casefold()], "properties": declared_properties, "identity_properties": identity_properties, "description": entity["definition"].replace("\n", " "), "instance_allowed": not abstract and source_mode != "derived"})
    def descendants(names: list[str]) -> list[str]:
        def belongs(name: str, ancestor: str) -> bool:
            return name == ancestor or any(belongs(parent, ancestor) for parent in parents[name])
        if any(name not in entities for name in names):
            raise ValueError("relationship contains an undeclared type endpoint")
        return [name for name in entities if any(belongs(name, ancestor) for ancestor in names)]
    compiled_relations = []
    for name, relation in relations.items():
        if not isinstance(relation, dict) or set(relation) - _RELATION_KEYS:
            raise ValueError(f"unsupported relation declaration fields: {name}")
        if relation.get("property_schema") != {"$ref": f"#/property_definitions/relation_types/{name}"}:
            raise ValueError("unresolved relationship property schema reference")
        if relation.get("direction") != "directed" or relation.get("canonical_storage") != "forward_only":
            raise ValueError("only directed forward canonical relationships are supported")
        for end in ("from", "to"):
            if relation[end].get("endpoint_kind") not in {"entity", "entity_type"}:
                raise ValueError("unsupported relationship endpoint kind")
        semantics = relation["statement_semantics"]
        if semantics.get("scope_kind") not in {"instance", "concept_general", "type_general"} or semantics.get("modality") not in {"asserted", "possible", "required"} or semantics.get("statement_kind") not in {"fact_statement", "general_knowledge", "requirement", "candidate_explanation"}:
            raise ValueError("unsupported relationship statement semantics")
        allowed = (all(relation[end]["endpoint_kind"] == "entity" for end in ("from", "to")) and not relation.get("formation_policy") and relation.get("materialization") != "forbidden")
        pairs = [[a, b] for pair in relation.get("allowed_type_pairs", []) for a in descendants([pair["from"]]) for b in descendants([pair["to"]])]
        cardinalities = {}
        for field, source_field in (("source_cardinality", "outgoing_per_source"), ("target_cardinality", "incoming_per_target")):
            cardinality = relation["cardinality"][source_field]
            bounds = (cardinality["min"], cardinality["max"])
            lookup = {(0, None): "ZERO_OR_MORE", (1, None): "ONE_OR_MORE", (0, 1): "ZERO_OR_ONE", (1, 1): "ONE"}
            if bounds not in lookup:
                raise ValueError("unsupported relationship cardinality bounds")
            cardinalities[field] = lookup[bounds]
        compiled_relations.append({"name": name, "source_types": descendants(relation["from"]["allowed_types"]), "target_types": descendants(relation["to"]["allowed_types"]), "properties": _properties(properties["relation_types"][name]), "description": f"{relation['definition']} [scope={semantics['scope_kind']}; modality={semantics['modality']}]".replace("\n", " "), "instance_allowed": allowed, "allowed_type_pairs": pairs, **cardinalities})
    hierarchies = []
    for name, declaration in hierarchy.get("concept_hierarchies", {}).items():
        if declaration.get("direction") != "specific_to_general" or declaration.get("acyclic") is not True:
            raise ValueError("unsupported concept hierarchy semantics")
        hierarchies.append({"name": name, "relationship_type": declaration["relationship_type"], "kind": "CLASSIFICATION", "node_types": declaration["node_types"], "acyclic": True})
    return {"key": metadata["ontology_id"], "version": source_version_number(metadata["ontology_version"]), "description": metadata["title"], "entity_types": compiled_entities, "relationship_types": compiled_relations, "hierarchies": hierarchies, "source_contract_json": text}


def compile_source_contract(source: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return _compile_source_contract(source)
    except (KeyError, TypeError, IndexError, AttributeError, RecursionError) as exc:
        raise ValueError("source contract contains a malformed or unsupported declaration") from exc


def source_import_report(source_contract_json: str | None) -> dict[str, Any] | None:
    if not source_contract_json:
        return None
    source = json.loads(source_contract_json)
    compiled = compile_source_contract(source)
    return {"profile": SOURCE_PROFILE, "source_version": source["metadata"]["ontology_version"], "source_checksum": hashlib.sha256(source_contract_json.encode("utf-8")).hexdigest(), "source_checksum_basis": "canonical_json_utf8_sha256", "blocked_entity_types": [item["name"] for item in compiled["entity_types"] if not item["instance_allowed"]], "blocked_relationship_types": [item["name"] for item in compiled["relationship_types"] if not item["instance_allowed"]], "scope": "有证据的设备结构与概念知识；类型端点和外部派生关系仅保存定义，禁止作为普通实例事实抽取或发布。源文件完整保存；不执行诊断或外部派生。"}


def validate_rule_reference_registry(value: object) -> dict[str, Any]:
    """A versioned reference index, never executable rule bodies or parameters."""
    if not isinstance(value, dict) or set(value) != {"registry_id", "version", "documents"}:
        raise ValueError("rule reference registry requires registry_id, version and documents")
    if not isinstance(value["registry_id"], str) or not value["registry_id"] or value["version"] != 1:
        raise ValueError("invalid rule reference registry identity/version")
    documents = value["documents"]
    if not isinstance(documents, list) or not 1 <= len(documents) <= 100:
        raise ValueError("rule registry requires 1..100 document references")
    identities = set()
    for document in documents:
        if not isinstance(document, dict) or set(document) != {"document", "document_version", "profile_id", "sha256", "clauses"}:
            raise ValueError("rule registry contains unsupported document fields")
        for key in ("document", "document_version", "profile_id"):
            if not isinstance(document[key], str) or not 1 <= len(document[key]) <= 256 or re.search(r"[\x00-\x1f\x7f]", document[key]):
                raise ValueError("rule reference identity must be bounded text")
        identity = tuple(document[key] for key in ("document", "document_version", "profile_id"))
        if identity in identities:
            raise ValueError("duplicate rule document/version/profile")
        identities.add(identity)
        if not isinstance(document["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", document["sha256"]):
            raise ValueError("rule reference requires exact original-byte SHA-256")
        if not isinstance(document["clauses"], list) or not 1 <= len(document["clauses"]) <= 1000:
            raise ValueError("rule reference requires bounded clauses")
        seen = set()
        for clause in document["clauses"]:
            if not isinstance(clause, dict) or set(clause) != {"id", "line"} or not isinstance(clause["id"], str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", clause["id"]):
                raise ValueError("rule clause requires stable ID and original line locator")
            if clause["id"] in seen or type(clause["line"]) is not int or not 1 <= clause["line"] <= 10_000_000:
                raise ValueError("duplicate rule clause or invalid line locator")
            seen.add(clause["id"])
    return value
