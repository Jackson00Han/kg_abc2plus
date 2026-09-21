"""Compiler for the seven-section property-graph authoring profile.

Domain names, combinations and topology rules come exclusively from the source.
The full source remains immutable JSON; normalized T-Box properties are the
executable projection, not a replacement for descriptions or governance data.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from .source import MAX_SOURCE_BYTES, _properties, canonical_json, source_version_number

PROFILE = "property_graph_authoring_v1"
MAX_SOURCE_CONSTRAINTS = 1024
_SECTIONS = {"metadata", "scope", "layers", "property_definitions", "entity_types", "relation_types", "constraints"}
_PROPERTY_FIELDS = {"display_name", "value_type", "required", "description", "definition", "allowed_values", "value_descriptions", "unit", "format", "const", "minimum", "maximum", "min_items", "max_items", "required_when"}
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")


def _error(message: str, path: str) -> None:
    from .yaml_source import OntologySourceError  # noqa: PLC0415
    raise OntologySourceError(message, path=path)


def _object(value: object, path: str, *, required: set[str] | None = None, optional: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        _error("该字段必须是命名对象。", path)
    if required is not None:
        missing = required - set(value)
        if missing:
            _error("缺少必需字段：" + ", ".join(sorted(missing)), path)
        unknown = set(value) - required - (optional or set())
        if unknown:
            _error("不支持的声明字段：" + ", ".join(sorted(unknown)), path + "." + sorted(unknown)[0])
    return value


def _text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip() or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value):
        _error("该字段必须是非空文字。", path)
    return value


def _name(value: object, path: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        _error("类型和属性标识须以英文字母开头，只包含字母、数字及下划线。", path)
    return value


def _list(value: object, path: str, *, maximum: int = 1024, minimum: int = 0) -> list[Any]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        _error(f"该字段必须是长度在 {minimum}–{maximum} 之间的列表。", path)
    return value


def _names(value: object, path: str, declared: Mapping[str, Any], *, minimum: int = 1) -> list[str]:
    result = _list(value, path, minimum=minimum)
    if any(not isinstance(item, str) or item not in declared for item in result):
        _error("引用了未声明的类型或属性。", path)
    if len(result) != len(set(result)):
        _error("列表存在重复引用。", path)
    return result


def _true_flags(value: dict[str, Any], fields: set[str], path: str) -> None:
    for field in fields:
        if value[field] is not True:
            _error("此编译配置要求该完整性约束显式为 true。", f"{path}.{field}")


def _compile_properties(declarations: dict[str, Any], path: str, *, excluded: str | None = None) -> list[dict[str, Any]]:
    normalized_properties = {}
    if len(declarations) > 256:
        _error("单一类型的属性声明超过预算。", path)
    if len({name.casefold() for name in declarations}) != len(declarations):
        _error("属性标识不能仅有大小写区别。", path)
    for name, item in declarations.items():
        location = f"{path}.{name}"
        _name(name, location)
        prop = _object(item, location, required={"value_type", "required"}, optional=_PROPERTY_FIELDS - {"value_type", "required"})
        _text(prop["value_type"], location + ".value_type")
        if not isinstance(prop["required"], bool):
            _error("属性 required 必须是 true 或 false。", location + ".required")
        for text_field in ("display_name", "description", "definition", "unit"):
            if text_field in prop:
                _text(prop[text_field], f"{location}.{text_field}")
        if "value_descriptions" in prop:
            descriptions = _object(prop["value_descriptions"], location + ".value_descriptions")
            allowed = prop.get("allowed_values", [])
            if not isinstance(allowed, list) or set(descriptions) != {str(value) for value in allowed}:
                _error("枚举含义必须与 allowed_values 一一对应。", location + ".value_descriptions")
            for value, description in descriptions.items():
                _text(description, location + ".value_descriptions." + value)
        normalized = {key: value for key, value in prop.items() if key not in {"display_name", "description", "value_descriptions", "format"}}
        if prop.get("description"):
            normalized["definition"] = prop["description"]
        if "format" in prop:
            if prop["value_type"] != "string" or not isinstance(prop["format"], str) or prop["format"] not in {"date-time", "date", "uri"}:
                _error("只支持 string 属性的 date-time、date 或 uri 格式。", location + ".format")
            normalized["value_type"] = {"date-time": "datetime", "date": "date", "uri": "uri"}[prop["format"]]
        normalized_properties[name] = normalized
    # Validate together so required_when can reference another declared field.
    try:
        compiled_properties = _properties(normalized_properties)
    except (ValueError, TypeError) as exc:
        location = next((f"{path}.{name}" for name in declarations if re.search(rf"\b{re.escape(name)}\b", str(exc))), path)
        _error(str(exc), location)
    if excluded:
        for prop in compiled_properties:
            condition = json.loads(prop["constraints_json"]).get("required_when")
            if condition and condition["property"] == excluded:
                _error("普通属性的条件约束不能读取由平台管理的身份槽。", path + "." + prop["name"])
    return [prop for prop in compiled_properties if prop["name"] != excluded]


def compile_property_graph_source(source: Mapping[str, Any]) -> dict[str, Any]:
    source = _object(source, "$", required=_SECTIONS)
    encoded = canonical_json(source)
    if len(encoded.encode("utf-8")) > MAX_SOURCE_BYTES:
        _error("规范化后的本体超过容量预算。", "$")
    metadata = _object(source["metadata"], "$.metadata", required={"ontology_id", "ontology_version", "title", "model_kind"}, optional={"status", "default_language", "description"})
    for key in metadata:
        _text(metadata[key], "$.metadata." + key)
    if metadata["model_kind"] != "property_graph":
        _error("不支持的本体图模型。", "$.metadata.model_kind")
    if not re.fullmatch(r"[a-z][a-z0-9._-]{0,127}", metadata["ontology_id"]):
        _error("本体标识须以小写字母开头，可包含小写字母、数字、点、下划线或连字符。", "$.metadata.ontology_id")
    try:
        version = source_version_number(metadata["ontology_version"])
    except ValueError as exc:
        _error(str(exc), "$.metadata.ontology_version")
    scope = _object(source["scope"], "$.scope", required={"domain", "included_topics", "excluded_topics"})
    _text(scope["domain"], "$.scope.domain")
    for section in ("included_topics", "excluded_topics"):
        for index, topic in enumerate(_list(scope[section], "$.scope." + section)):
            _text(topic, f"$.scope.{section}[{index}]")
    layers = _object(source["layers"], "$.layers", required={"levels"})
    levels = _object(layers["levels"], "$.layers.levels")
    if not 1 <= len(levels) <= 32:
        _error("来源等级数量须在 1–32 之间。", "$.layers.levels")
    for name, level in levels.items():
        path = "$.layers.levels." + name
        _name(name, path)
        item = _object(level, path, required={"display_name", "rank", "description"})
        _text(item["display_name"], path + ".display_name")
        _text(item["description"], path + ".description")
        if type(item["rank"]) is not int or not 0 <= item["rank"] <= 1000:
            _error("来源等级 rank 必须是 0–1000 的整数。", path + ".rank")
    definitions = _object(source["property_definitions"], "$.property_definitions", required={"entity_common"})
    common = _object(definitions["entity_common"], "$.property_definitions.entity_common", required={"applies_to", "properties"})
    if common["applies_to"] != "all_entity_types":
        _error("通用属性当前只支持 all_entity_types。", "$.property_definitions.entity_common.applies_to")
    common_props = _object(common["properties"], "$.property_definitions.entity_common.properties")
    entities = _object(source["entity_types"], "$.entity_types")
    relations = _object(source["relation_types"], "$.relation_types")
    if not 1 <= len(entities) <= 256 or not 1 <= len(relations) <= 512:
        _error("实体类型须为 1–256 个，关系类型须为 1–512 个。", "$")
    for names, path in ((entities, "$.entity_types"), (relations, "$.relation_types")):
        if len({name.casefold() for name in names}) != len(names):
            _error("类型标识不能仅有大小写区别。", path)
    constraints = _object(source["constraints"], "$.constraints")
    if len(constraints) > MAX_SOURCE_CONSTRAINTS:
        _error(f"本体约束不能超过 {MAX_SOURCE_CONSTRAINTS} 条。", "$.constraints")
    required_constraints = {"entity_identity", "reference_integrity", "ownership_cardinality", "engineering_structure"}
    if not required_constraints <= set(constraints):
        _error("缺少身份、引用、归属数量或工程结构约束。", "$.constraints")
    identity = _object(constraints["entity_identity"], "$.constraints.entity_identity", required={"description", "id_property", "non_empty", "unique_in_graph", "not_sufficient_alone"})
    _text(identity["description"], "$.constraints.entity_identity.description")
    _true_flags(identity, {"non_empty", "unique_in_graph"}, "$.constraints.entity_identity")
    id_property = _name(identity["id_property"], "$.constraints.entity_identity.id_property")
    id_definition = _object(common_props.get(id_property), "$.property_definitions.entity_common.properties." + id_property)
    if id_definition.get("value_type") != "string" or id_definition.get("required") is not True:
        _error("身份属性必须声明为全体实体必填的 string 属性。", "$.constraints.entity_identity.id_property")
    _compile_properties(common_props, "$.property_definitions.entity_common.properties", excluded=id_property)
    compiled_entities, all_props = [], {}
    for name, declaration in entities.items():
        path = "$.entity_types." + name
        _name(name, path)
        entity = _object(declaration, path, required={"display_name", "definition", "properties"})
        _text(entity["display_name"], path + ".display_name")
        _text(entity["definition"], path + ".definition")
        props = _object(entity["properties"], path + ".properties")
        if set(props) & set(common_props):
            _error("类型专有属性不能覆盖通用属性。", path + ".properties")
        all_props[name] = {**common_props, **props}
        compiled = _compile_properties(all_props[name], path + ".properties", excluded=id_property)
        compiled_entities.append({"name": name, "canonical_key_namespaces": list(dict.fromkeys(["llm-candidate", "source", name.casefold()])), "properties": compiled, "identity_properties": [], "description": entity["definition"].replace("\n", " "), "instance_allowed": True})
    all_property_names = {key: None for props in all_props.values() for key in props}
    _names(identity["not_sufficient_alone"], "$.constraints.entity_identity.not_sufficient_alone", all_property_names, minimum=0)
    compiled_relations = []
    for name, declaration in relations.items():
        path = "$.relation_types." + name
        _name(name, path)
        relation = _object(declaration, path, required={"display_name", "definition", "allowed_type_pairs"}, optional={"properties"})
        _text(relation["display_name"], path + ".display_name")
        _text(relation["definition"], path + ".definition")
        pairs = []
        for index, pair in enumerate(_list(relation["allowed_type_pairs"], path + ".allowed_type_pairs", minimum=1)):
            location = f"{path}.allowed_type_pairs[{index}]"
            pair = _object(pair, location, required={"from", "to"})
            for endpoint in ("from", "to"):
                if not isinstance(pair[endpoint], str) or pair[endpoint] not in entities:
                    _error("关系端点引用了未声明的实体类型。", location + "." + endpoint)
            entry = [pair["from"], pair["to"]]
            if entry in pairs:
                _error("关系端点组合不能重复。", location)
            pairs.append(entry)
        props = _object(relation.get("properties", {}), path + ".properties")
        compiled_relations.append({"name": name, "source_types": sorted({pair[0] for pair in pairs}), "target_types": sorted({pair[1] for pair in pairs}), "properties": _compile_properties(props, path + ".properties"), "description": relation["definition"].replace("\n", " "), "allowed_type_pairs": pairs, "instance_allowed": True, "source_cardinality": "ZERO_OR_MORE", "target_cardinality": "ZERO_OR_MORE"})
    _validate_constraints(constraints, entities, relations, all_props)
    return {"key": metadata["ontology_id"], "version": version, "description": metadata["title"], "entity_types": compiled_entities, "relationship_types": compiled_relations, "hierarchies": [], "source_contract_json": encoded}


def _validate_constraints(constraints: dict[str, Any], entities: dict[str, Any], relations: dict[str, Any], properties: dict[str, Any]) -> None:
    from .source import validate_property_constraints  # noqa: PLC0415
    known = {"entity_identity", "reference_integrity", "ownership_cardinality", "engineering_structure"}
    for name, declaration in constraints.items():
        path = "$.constraints." + name
        _name(name, path)
        if name in known:
            continue
        combo = _object(declaration, path, required={"description", "entity_type", "allowed_combinations"})
        _text(combo["description"], path + ".description")
        entity_type = combo["entity_type"]
        if not isinstance(entity_type, str) or entity_type not in entities:
            _error("联合属性约束引用了未声明的实体类型。", path + ".entity_type")
        rows = _list(combo["allowed_combinations"], path + ".allowed_combinations", minimum=1)
        fields, seen = None, set()
        definitions = {item["name"]: item for item in _compile_properties(properties[entity_type], path)}
        for index, row in enumerate(rows):
            location = f"{path}.allowed_combinations[{index}]"
            row = _object(row, location)
            if not row or set(row) - set(properties[entity_type]):
                _error("联合属性组合必须引用本类型已声明的属性。", location)
            if fields is None:
                fields = set(row)
            if set(row) != fields:
                _error("所有允许组合必须包含相同的属性集合。", location)
            marker = canonical_json(row)
            if marker in seen:
                _error("允许的联合属性组合不能重复。", location)
            seen.add(marker)
            for key, value in row.items():
                value_type = properties[entity_type][key]["value_type"]
                acceptable = {"string": (str,), "number": (int, float), "integer": (int,), "boolean": (bool,)}.get(value_type)
                if acceptable is None or type(value) not in acceptable:
                    _error("联合属性约束当前只接受与字段类型一致的标量。", location + "." + key)
                try:
                    validate_property_constraints(definitions[key]["constraints_json"], value)
                except ValueError as exc:
                    _error(str(exc), location + "." + key)
    reference = _object(constraints["reference_integrity"], "$.constraints.reference_integrity", required={"description", "entity_type_must_be_declared", "relation_type_must_be_declared", "endpoints_must_exist", "enforce_allowed_type_pairs"})
    _text(reference["description"], "$.constraints.reference_integrity.description")
    _true_flags(reference, set(reference) - {"description"}, "$.constraints.reference_integrity")
    ownership = _object(constraints["ownership_cardinality"], "$.constraints.ownership_cardinality", required={"description", "count_basis", "rules"})
    _text(ownership["description"], "$.constraints.ownership_cardinality.description")
    if ownership["count_basis"] != "distinct_related_entities":
        _error("归属数量必须按不同关联对象计数。", "$.constraints.ownership_cardinality.count_basis")
    seen_rules = set()
    for index, rule in enumerate(_list(ownership["rules"], "$.constraints.ownership_cardinality.rules")):
        path = f"$.constraints.ownership_cardinality.rules[{index}]"
        rule = _object(rule, path, required={"id", "entity_types", "relation_type", "direction", "min_count", "max_count"})
        _name(rule["id"], path + ".id")
        if rule["id"] in seen_rules:
            _error("数量约束的 id 不能重复。", path + ".id")
        seen_rules.add(rule["id"])
        types = _names(rule["entity_types"], path + ".entity_types", entities)
        relation = rule["relation_type"]
        if not isinstance(relation, str) or relation not in relations:
            _error("数量约束引用了未声明的关系。", path + ".relation_type")
        if not isinstance(rule["direction"], str) or rule["direction"] not in {"incoming", "outgoing"}:
            _error("方向只支持 incoming 或 outgoing。", path + ".direction")
        pair_key = "to" if rule["direction"] == "incoming" else "from"
        allowed = {pair[pair_key] for pair in relations[relation]["allowed_type_pairs"]}
        if set(types) - allowed:
            _error("数量约束的类型与关系方向不一致。", path + ".entity_types")
        if type(rule["min_count"]) is not int or not 0 <= rule["min_count"] <= 10000 or (rule["max_count"] is not None and (type(rule["max_count"]) is not int or not rule["min_count"] <= rule["max_count"] <= 10000)):
            _error("数量上下限必须是有序的非负整数；上限可为 null。", path)
    path = "$.constraints.engineering_structure"
    structure = _object(constraints["engineering_structure"], path, required={"description", "project_root_type", "project_member_types", "ownership_relations", "require_one_project", "acyclic_relations", "non_repeatable_relations"})
    _text(structure["description"], path + ".description")
    _names([structure["project_root_type"]], path + ".project_root_type", entities)
    members = _names(structure["project_member_types"], path + ".project_member_types", entities, minimum=0)
    if structure["project_root_type"] in members:
        _error("工程根类型不能同时声明为需归属的成员类型。", path + ".project_member_types")
    for key in ("ownership_relations", "acyclic_relations", "non_repeatable_relations"):
        _names(structure[key], path + "." + key, relations, minimum=0)
    _true_flags(structure, {"require_one_project"}, path)


def property_graph_import_report(source: dict[str, Any]) -> dict[str, Any]:
    return {"profile": PROFILE, "source_version": source["metadata"]["ontology_version"], "blocked_entity_types": [], "blocked_relationship_types": [], "identity_mapping": {"source_property": source["constraints"]["entity_identity"]["id_property"], "target": "platform_entity_id", "requires_audited_source_identity_mapping": True}, "executable_constraints": list(source["constraints"]), "preserved_sections": sorted(source), "scope": "结构、类型、枚举、格式、联合属性、身份、引用与工程归属约束由本体声明驱动；文字定义与来源等级原样保留供审校，不执行诊断、不提升来源等级，不将上传视作审核或发布。"}
