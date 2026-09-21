"""Bounded, declarative Markdown section/table preflight over unchanged text.

This module produces a review report, never A-Box records or publishable facts.
Type and identity assignments belong to a separately versioned mapping; values
and exact evidence always come from the supplied document. No source is opened
and no unresolved reference is repaired by inventing a node.
"""
from __future__ import annotations

from bisect import bisect_right
import hashlib
import json
import re
from typing import Any

VERSION = "markdown-section-mapping:v1"
REPORT_VERSION = "markdown-section-preview:v1"
MAX_SOURCE_BYTES = 1_000_000
MAX_MAPPING_BYTES = 200_000
MAX_SECTIONS = 512
MAX_ROWS = 2000
MAX_COLUMNS = 64
MAX_DIAGNOSTICS = 100
MAX_PREVIEW_EVIDENCE_BYTES = 1_500_000
_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+)$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_EXPLICIT_ID = re.compile(r"^\[([^\]\r\n]{1,256})\]\s+(.+)$")
_SEPARATOR = re.compile(r"^:?-{3,}:?$")


class MarkdownMappingError(ValueError):
    """A source or mapping cannot be interpreted without guessing."""

    def __init__(self, code: str, path: str, message: str):
        self.code, self.path, self.message = code, path, message
        super().__init__(f"{code}: {message}")


def _fail(code: str, path: str, message: str) -> None:
    raise MarkdownMappingError(code, path, message)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _text(value: Any, path: str, *, limit: int = 512) -> str:
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > limit or any(ord(c) < 32 for c in value)):
        _fail("MAPPING_TEXT_INVALID", path, "需要有界、非空且无控制字符的文本。")
    return value


def _object(value: Any, required: set[str], optional: set[str], path: str) -> dict:
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - optional:
        _fail("MAPPING_SCHEMA_INVALID", path, "映射字段缺失或包含不支持的字段。")
    return value


def _span(text: str, start: int, end: int, **location: Any) -> dict:
    return {"char_start": start, "char_end": end, "text": text[start:end], **location}


def _lines(text: str) -> list[tuple[int, str, bool]]:
    """Keep offsets and suppress all fenced-code content, including delimiters."""
    result, offset, fence = [], 0, None
    for line in text.splitlines(keepends=True):
        marker = _FENCE.match(line)
        in_code = fence is not None or marker is not None
        if fence is not None:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not line[marker.end():].strip():
                fence = None
        elif marker:
            fence = marker[1]
        result.append((offset, line, in_code))
        offset += len(line)
    if fence is not None:
        _fail("MARKDOWN_FENCE_UNCLOSED", "$", "代码围栏没有闭合，无法可靠识别后续章节。")
    return result


def _cells(text: str, offset: int, line: str) -> list[dict] | None:
    """Locate pipe cells; escaped pipes and code-span pipes are cell content."""
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return None
    separators, index, code_ticks = [], 0, 0
    while index < len(line):
        char = line[index]
        if char == "\\":
            index += 2
            continue
        if char == "`":
            stop = index + 1
            while stop < len(line) and line[stop] == "`":
                stop += 1
            count = stop - index
            code_ticks = 0 if code_ticks == count else (count if not code_ticks else code_ticks)
            index = stop
            continue
        if char == "|" and not code_ticks:
            separators.append(index)
        index += 1
    if len(separators) < 2 or code_ticks:
        _fail("MARKDOWN_TABLE_INVALID", f"char:{offset}", "表格分隔符或行内代码无法可靠解析。")
    if len(separators) - 1 > MAX_COLUMNS:
        _fail("MARKDOWN_TABLE_LIMIT", f"char:{offset}", "表格列数超过上限。")
    result = []
    for left, right in zip(separators, separators[1:]):
        start, end = left + 1, right
        while start < end and line[start].isspace():
            start += 1
        while end > start and line[end - 1].isspace():
            end -= 1
        result.append(_span(text, offset + start, offset + end))
    return result


def parse_markdown_sections(text: str, *, heading_level: int = 2) -> dict:
    """Locate complete sections and single-line pipe tables without rewriting.

    Unlabelled headings use their full title as the selector. Explicit [IDs]
    are convenience selectors, never persistent entity identities. This first
    profile accepts one heading level. Additional levels after the preamble
    require an explicitly extended profile instead of silently hiding sections.
    """
    if not isinstance(text, str) or not text.strip():
        _fail("MARKDOWN_SOURCE_EMPTY", "$", "Markdown 内容不能为空。")
    if len(text.encode("utf-8")) > MAX_SOURCE_BYTES:
        _fail("MARKDOWN_SOURCE_LIMIT", "$", "Markdown 文件超过解析字节上限。")
    if isinstance(heading_level, bool) or heading_level not in range(1, 7):
        _fail("MAPPING_HEADING_LEVEL", "$", "标题层级必须为 1 至 6。")
    lines = _lines(text)
    headings = []
    ids = set()
    for index, (offset, line, in_code) in enumerate(lines):
        match = None if in_code else _HEADING.match(line)
        if match is None:
            continue
        if len(match[1]) != heading_level:
            if headings:
                _fail("MARKDOWN_UNMAPPED_HEADING_LEVEL", f"char:{offset}", "映射章节中出现其他层级标题，需要显式扩展章节映射。")
            continue
        heading_text = re.sub(r"[ \t]+#+[ \t]*$", "", match[2]).strip()
        explicit = _EXPLICIT_ID.match(heading_text)
        identity, title = (explicit[1], explicit[2]) if explicit else (heading_text, heading_text)
        _text(identity, f"char:{offset}", limit=256)
        if identity in ids:
            _fail("MARKDOWN_DUPLICATE_SECTION", identity, "重复章节标识，不能按名称合并。")
        ids.add(identity)
        headings.append((index, offset, identity, title))
    if not headings or len(headings) > MAX_SECTIONS:
        _fail("MARKDOWN_SECTION_LIMIT", "$", "未找到指定层级章节，或章节数超过上限。")
    sections, row_count = [], 0
    for number, (first_line, start, identity, title) in enumerate(headings):
        stop_line = headings[number + 1][0] if number + 1 < len(headings) else len(lines)
        end = headings[number + 1][1] if number + 1 < len(headings) else len(text)
        tables, index = [], first_line + 1
        while index < stop_line:
            offset, line, in_code = lines[index]
            cells = None if in_code else _cells(text, offset, line)
            if cells is None:
                index += 1
                continue
            if index + 1 >= stop_line:
                _fail("MARKDOWN_TABLE_INVALID", identity, "表格缺少表头分隔行。")
            next_offset, next_line, next_code = lines[index + 1]
            delimiters = None if next_code else _cells(text, next_offset, next_line)
            if (delimiters is None or len(delimiters) != len(cells)
                    or not all(_SEPARATOR.fullmatch(c["text"]) for c in delimiters)):
                _fail("MARKDOWN_TABLE_INVALID", identity, "只支持带明确表头分隔行的管道表格。")
            headers = [c["text"] for c in cells]
            if any(not h for h in headers) or len(set(headers)) != len(headers):
                _fail("MARKDOWN_TABLE_HEADER", identity, "表头不能为空或重复。")
            rows, table_start = [], offset
            index += 2
            while index < stop_line:
                row_offset, row_line, row_code = lines[index]
                row_cells = None if row_code else _cells(text, row_offset, row_line)
                if row_cells is None:
                    break
                if len(row_cells) != len(headers):
                    _fail("MARKDOWN_TABLE_WIDTH", identity, "表格行列数与表头不一致。")
                rows.append({"evidence": _span(text, row_offset, row_offset + len(row_line.rstrip("\r\n"))),
                             "cells": dict(zip(headers, row_cells, strict=True))})
                row_count += 1
                if row_count > MAX_ROWS:
                    _fail("MARKDOWN_TABLE_LIMIT", identity, "表格总行数超过上限。")
                index += 1
            tables.append({"headers": headers, "header_evidence": cells, "rows": rows,
                           "char_start": table_start,
                           "char_end": lines[index][0] if index < stop_line else end})
        sections.append({"section_id": identity, "title": title,
                         "evidence": _span(text, start, end, section_id=identity), "tables": tables})
    return {"source_checksum": _checksum(text), "source_checksum_basis": "supplied-utf8-text",
            "preamble": _span(text, 0, headings[0][1]), "sections": sections}


def _validate_mapping(mapping: dict) -> dict:
    _object(mapping, {"version", "mapping_id", "mapping_version", "heading_level", "preamble_action",
                      "semantic_context", "global_context_sections", "sections"}, {"provenance"}, "$")
    if mapping["version"] != VERSION or mapping["preamble_action"] != "context":
        _fail("MAPPING_VERSION_INVALID", "$", "映射版本或前言处置方式不受支持。")
    for key in ("mapping_id", "mapping_version"):
        _text(mapping[key], "/" + key)
    if not isinstance(mapping["semantic_context"], dict) or len(_canonical(mapping["semantic_context"])) > 4096:
        _fail("MAPPING_CONTEXT_INVALID", "/semantic_context", "语义上下文必须为有界 JSON 对象。")
    context = mapping["semantic_context"]
    if context.get("content_layer") == "simulated_knowledge" and (
            context.get("runtime_event") is not False
            or context.get("industrial_validation") != "not_independently_verified"):
        _fail("MAPPING_SIMULATION_BOUNDARY", "/semantic_context", "模拟内容必须保持非运行事件及未经独立工业验证的边界。")
    if not isinstance(mapping["global_context_sections"], list) or len(mapping["global_context_sections"]) > MAX_SECTIONS:
        _fail("MAPPING_CONTEXT_INVALID", "/global_context_sections", "上下文章节必须为有界列表。")
    if not isinstance(mapping["sections"], list) or not 0 < len(mapping["sections"]) <= MAX_SECTIONS:
        _fail("MAPPING_SECTION_LIMIT", "/sections", "需要有界章节映射列表。")
    rules, identities = {}, set()
    for index, rule in enumerate(mapping["sections"]):
        path = f"/sections/{index}"
        if not isinstance(rule, dict):
            _fail("MAPPING_SCHEMA_INVALID", path, "章节映射必须为对象。")
        action = rule.get("action")
        common = {"section_id", "action", "context_sections"}
        action_fields = {
            "entity": {"entity_type", "identity", "property_columns"},
            "relationship": {"relationship_type", "source_column", "target_column", "type_column", "property_columns", "context_columns"},
            "context": {"reason"}, "excluded_context": {"reason"},
        }
        if action not in action_fields:
            _fail("MAPPING_ACTION_INVALID", path, "未知章节处置方式。")
        _object(rule, common | action_fields[action], set(), path)
        identity = _text(rule["section_id"], path + "/section_id", limit=256)
        if identity in rules:
            _fail("MAPPING_DUPLICATE_SECTION", path, "同一章节不能重复配置。")
        if not isinstance(rule["context_sections"], list) or len(rule["context_sections"]) > MAX_SECTIONS:
            _fail("MAPPING_CONTEXT_INVALID", path, "上下文章节列表无效。")
        for target in rule["context_sections"]:
            _text(target, path + "/context_sections", limit=256)
        if action in {"context", "excluded_context"}:
            _text(rule["reason"], path + "/reason", limit=1024)
        else:
            kind_field = "entity_type" if action == "entity" else "relationship_type"
            _text(rule[kind_field], path + "/" + kind_field)
            columns = rule["property_columns"]
            if not isinstance(columns, dict) or len(columns) > MAX_COLUMNS:
                _fail("MAPPING_COLUMN_INVALID", path, "属性列映射必须为有界对象。")
            for column, prop in columns.items():
                _text(column, path + "/property_columns")
                _text(prop, path + "/property_columns/" + _pointer(column))
            if len(set(columns.values())) != len(columns):
                _fail("MAPPING_DUPLICATE_PROPERTY", path, "同一属性不能由多个列静默覆盖。")
            if action == "entity":
                stable = _text(rule["identity"], path + "/identity")
                if stable in identities:
                    _fail("MAPPING_DUPLICATE_IDENTITY", path, "实体审校身份重复，不能静默合并。")
                identities.add(stable)
            else:
                for field in ("source_column", "target_column", "type_column"):
                    _text(rule[field], path + "/" + field)
                ignored = rule["context_columns"]
                if not isinstance(ignored, dict) or len(ignored) > MAX_COLUMNS:
                    _fail("MAPPING_COLUMN_INVALID", path, "关系上下文列必须说明保留原因。")
                for column, reason in ignored.items():
                    _text(column, path + "/context_columns")
                    _text(reason, path + "/context_columns/" + _pointer(column))
                selected = [rule["source_column"], rule["target_column"], rule["type_column"], *columns, *ignored]
                if len(set(selected)) != len(selected):
                    _fail("MAPPING_COLUMN_CONFLICT", path, "列不能同时用于多个映射用途。")
        rules[identity] = {**rule, "mapping_pointer": path}
    for identity in mapping["global_context_sections"]:
        _text(identity, "/global_context_sections", limit=256)
    for rule in rules.values():
        for identity in [*mapping["global_context_sections"], *rule["context_sections"]]:
            if identity not in rules or rules[identity]["action"] not in {"context", "excluded_context"}:
                _fail("MAPPING_CONTEXT_UNRESOLVED", identity, "引用的上下文章节未配置为上下文。")
    return rules


def _ontology_properties(kind: Any, values: list[dict], path: str, normalizer: Any) -> dict:
    from graphrag_prod.ontology.source import condition_matches
    declared = {p.name: p for p in kind.properties}
    actual = {}
    for value in values:
        prop = declared.get(value["property"])
        if prop is None:
            _fail("MAPPING_PROPERTY_UNDECLARED", path, "表格属性未在已启用本体中声明。")
        try:
            literal = normalizer.normalize(prop, raw_value=value["value"], raw_unit=None,
                                           valid_from=None, valid_to=None, observed_at=None)
        except ValueError as error:
            _fail("MAPPING_PROPERTY_INVALID", path + "/" + prop.name, str(error))
        actual[prop.name] = literal.typed_value
        value["canonical_value"] = literal.canonical_value
        value["datatype"] = literal.datatype
    for prop in declared.values():
        if prop.required and prop.name not in actual:
            _fail("MAPPING_REQUIRED_PROPERTY", path + "/" + prop.name, "来源表格缺少本体要求的必填属性，不能补猜。")
        condition = json.loads(prop.constraints_json or "{}").get("required_when")
        if condition and condition_matches(condition, actual) and prop.name not in actual:
            _fail("MAPPING_REQUIRED_PROPERTY", path + "/" + prop.name, "来源表格缺少有条件必填属性。")
    return actual


def preview_markdown_mapping(text: str, mapping: dict, *, tbox: Any = None) -> dict:
    """Return a JSON-safe, source-only report; errors never yield graph output.

    ``valid`` describes parsing and mapping coverage. Ontology problems are
    separate readiness findings so incomplete evidence can still be retained
    source-only. Missing external references make ``complete`` false. A valid
    report is still PREVIEW_ONLY and never authorizes graph ingestion,
    identity merging, review, publication, or source-authority promotion.
    """
    report = {"version": REPORT_VERSION, "valid": False, "complete": False,
              "disposition": "PREVIEW_ONLY", "source_checksum": None,
              "source_checksum_basis": "supplied-utf8-text", "mapping_checksum": None,
              "ontology_checksum": getattr(tbox, "checksum", None),
              "entities": [], "relationships": [], "unresolved_references": [],
              "coverage": [], "semantic_context": {}, "diagnostics": [],
              "ontology_readiness": {"checked": tbox is not None, "ready": False, "findings": []}}
    try:
        if isinstance(text, str):
            report["source_checksum"] = _checksum(text)
        encoded_mapping = _canonical(mapping)
        if len(encoded_mapping.encode("utf-8")) > MAX_MAPPING_BYTES:
            _fail("MAPPING_SIZE_LIMIT", "$", "映射配置超过字节上限。")
        report["mapping_checksum"] = _checksum(encoded_mapping)
        rules = _validate_mapping(mapping)
        parsed = parse_markdown_sections(text, heading_level=mapping["heading_level"])
        sections = {section["section_id"]: section for section in parsed["sections"]}
        report["semantic_context"] = json.loads(_canonical(mapping["semantic_context"]))
        report["coverage"] = [{"section_id": None, "action": "context", "reason": "document_preamble",
                               "char_start": 0, "char_end": parsed["preamble"]["char_end"]}]
        report["coverage"].extend({"section_id": key, "action": rules.get(key, {}).get("action", "unmapped"),
                                   "char_start": section["evidence"]["char_start"],
                                   "char_end": section["evidence"]["char_end"],
                                   "mapping_pointer": rules.get(key, {}).get("mapping_pointer")}
                                  for key, section in sections.items())
        if set(sections) != set(rules):
            missing = sorted(set(rules) - set(sections))
            unknown = sorted(set(sections) - set(rules))
            _fail("MARKDOWN_SECTION_COVERAGE", "$", f"章节覆盖不完整；未映射 {len(unknown)} 章: {unknown[:10]}；来源缺失 {len(missing)} 章: {missing[:10]}。")
        entity_defs = {e.name: e for e in tbox.entity_types if e.instance_allowed} if tbox else {}
        relation_defs = {r.name: r for r in tbox.relationship_types if r.instance_allowed} if tbox else {}
        normalizer = None
        if tbox is not None:
            from .literals import TBoxLiteralNormalizer
            normalizer = TBoxLiteralNormalizer()
        else:
            report["diagnostics"].append({"severity": "warning", "code": "ONTOLOGY_NOT_CHECKED",
                                           "path": "$", "message": "未提供已启用本体，类型与属性约束尚未校验。"})
        entities, relationships = [], []
        evidence_bytes = 0
        entity_values = {}
        ontology_findings = report["ontology_readiness"]["findings"]

        def ontology_issue(code: str, path: str, message: str) -> None:
            if len(ontology_findings) < MAX_DIAGNOSTICS:
                ontology_findings.append({"severity": "error", "code": code, "path": path, "message": message})

        def check_properties(definition: Any, properties: list[dict], identity: str) -> dict:
            try:
                return _ontology_properties(definition, properties, identity, normalizer)
            except MarkdownMappingError as error:
                ontology_issue(error.code, error.path, error.message)
                return {}

        for identity, section in sections.items():
            rule = rules[identity]
            if rule["action"] in {"context", "excluded_context"}:
                continue
            if len(section["tables"]) != 1 or len(section["tables"][0]["rows"]) != 1:
                _fail("MARKDOWN_SECTION_TABLE_COUNT", identity, "当前章节映射要求恰好一个表格及一行数据。")
            table, path = section["tables"][0], rule["mapping_pointer"]
            row = table["rows"][0]
            required_columns = set(rule["property_columns"])
            if rule["action"] == "relationship":
                required_columns |= {rule["source_column"], rule["target_column"], rule["type_column"], *rule["context_columns"]}
            if set(table["headers"]) != required_columns:
                _fail("MARKDOWN_COLUMN_COVERAGE", identity, "所有表格列必须明确映射或保留为上下文，不允许缺列或新增未知列。")
            properties, empty_properties = [], []
            for column, prop in rule["property_columns"].items():
                cell = row["cells"][column]
                if not cell["text"]:
                    empty_properties.append({"property": prop, "evidence": {**cell, "column": column},
                                             "mapping_pointer": path + "/property_columns/" + _pointer(column)})
                    continue
                properties.append({"property": prop, "value": cell["text"], "evidence": {**cell, "column": column},
                                   "mapping_pointer": path + "/property_columns/" + _pointer(column)})
            context_ids = list(dict.fromkeys([*mapping["global_context_sections"], *rule["context_sections"]]))
            supporting = [section["evidence"], *(sections[key]["evidence"] for key in context_ids if key != identity)]
            common = {"section_id": identity, "properties": properties, "empty_properties": empty_properties,
                      "evidence": row["evidence"],
                      "supporting_evidence": supporting, "mapping_pointer": path,
                      "semantic_context": report["semantic_context"]}
            evidence_bytes += len(_canonical(common).encode("utf-8"))
            if evidence_bytes > MAX_PREVIEW_EVIDENCE_BYTES:
                _fail("MARKDOWN_PREVIEW_LIMIT", identity, "章节证据展开超过预检预算；请缩小本次映射范围。")
            if rule["action"] == "entity":
                kind = rule["entity_type"]
                if tbox is not None:
                    if kind not in entity_defs:
                        ontology_issue("MAPPING_ENTITY_TYPE_UNDECLARED", identity, "实体类型不在本体允许的实例类型中。")
                    else:
                        entity_values[rule["identity"]] = check_properties(entity_defs[kind], properties, identity)
                entities.append({**common, "identity": rule["identity"], "entity_type": kind,
                                 "identity_evidence": {"mapping_pointer": path + "/identity"},
                                 "type_evidence": {"mapping_pointer": path + "/entity_type"}})
            else:
                kind = rule["relationship_type"]
                if row["cells"][rule["type_column"]]["text"] != kind:
                    _fail("MAPPING_RELATION_TYPE_CHANGED", identity, "关系表声明的类型与受审校映射不一致。")
                if tbox is not None:
                    if kind not in relation_defs:
                        ontology_issue("MAPPING_RELATION_TYPE_UNDECLARED", identity, "关系类型不在本体允许的实例类型中。")
                    else:
                        check_properties(relation_defs[kind], properties, identity)
                source, target = (row["cells"][rule[field]] for field in ("source_column", "target_column"))
                if not source["text"] or not target["text"]:
                    _fail("MAPPING_REFERENCE_EMPTY", identity, "关系端点引用不能为空。")
                relationships.append({**common, "relationship_type": kind, "source_reference": source["text"],
                                      "target_reference": target["text"], "source_evidence": source,
                                      "target_evidence": target, "type_evidence": row["cells"][rule["type_column"]],
                                      "context_evidence": [{**row["cells"][column], "column": column, "reason": reason}
                                                           for column, reason in rule["context_columns"].items()]})
        by_section = {entity["section_id"]: entity for entity in entities}
        by_identity = {entity["identity"]: entity for entity in entities}
        for section_id, entity in by_section.items():
            if section_id in by_identity and by_identity[section_id] is not entity:
                _fail("MAPPING_REFERENCE_AMBIGUOUS", section_id, "章节标识与另一实体审校身份冲突。")
        references = {**by_identity, **by_section}
        unresolved = {}
        for relation in relationships:
            endpoints = []
            for side in ("source", "target"):
                reference = relation[side + "_reference"]
                entity = references.get(reference)
                endpoints.append(entity)
                relation[side + "_identity"] = entity["identity"] if entity else None
                if entity is None:
                    entry = unresolved.setdefault(reference, {"reference": reference, "reason": "external_definition_not_supplied", "uses": []})
                    entry["uses"].append({"section_id": relation["section_id"], "side": side,
                                          "evidence": relation[side + "_evidence"]})
            relation["status"] = "RESOLVED" if all(endpoints) else "UNRESOLVED"
            if all(endpoints) and relation["relationship_type"] in relation_defs and not relation_defs[relation["relationship_type"]].allows_instances(
                    endpoints[0]["entity_type"], endpoints[1]["entity_type"]):
                ontology_issue("MAPPING_ENDPOINT_TYPE_INVALID", relation["section_id"], "关系方向或端点类型不符合本体。")
        if tbox is not None and getattr(tbox, "source_contract_json", None):
            source = json.loads(tbox.source_contract_json)
            for name, constraint in source.get("constraints", {}).items():
                if not isinstance(constraint, dict) or "allowed_combinations" not in constraint:
                    continue
                for entity in entities:
                    if entity["entity_type"] != constraint.get("entity_type"):
                        continue
                    values = entity_values.get(entity["identity"], {})
                    if not any(all(values.get(key) == expected for key, expected in combination.items())
                               for combination in constraint["allowed_combinations"]):
                        ontology_issue("MAPPING_PROPERTY_COMBINATION_INVALID", entity["section_id"], "属性搭配不满足本体约束: " + name)
        report["ontology_readiness"]["ready"] = tbox is not None and not ontology_findings and not unresolved
        report.update(entities=entities, relationships=relationships,
                      unresolved_references=list(unresolved.values()), valid=True,
                      complete=report["ontology_readiness"]["ready"])
        if ontology_findings:
            report["diagnostics"].append({"severity": "warning", "code": "MAPPING_ONTOLOGY_NOT_READY",
                                           "path": "$", "message": "原文及映射结构可保留；本体约束存在待处理问题，不能进入图谱候选。"})
        if unresolved:
            report["diagnostics"].append({"severity": "warning", "code": "MAPPING_EXTERNAL_REFERENCES_UNRESOLVED",
                                           "path": "$", "message": f"存在 {len(unresolved)} 个缺失定义的外部引用；已保留关系用途，未创建占位实体。"})
    except MarkdownMappingError as error:
        report["diagnostics"].append({"severity": "error", "code": error.code,
                                       "path": error.path, "message": error.message})
    except (TypeError, ValueError, KeyError, RecursionError, UnicodeError):
        report["diagnostics"].append({"severity": "error", "code": "MAPPING_INPUT_INVALID",
                                       "path": "$", "message": "输入不符合有界章节映射契约。"})
    line_starts = [0, *(match.end() for match in re.finditer("\n", text))] if isinstance(text, str) else [0]
    for diagnostic in report["diagnostics"]:
        if diagnostic["path"].startswith("char:"):
            offset = int(diagnostic["path"][5:])
            diagnostic["line"] = bisect_right(line_starts, offset)
    report["diagnostics"] = report["diagnostics"][:MAX_DIAGNOSTICS]
    return report
